"""应用服务层：会话编排、幂等取题/作答、作废、受控重评、教师报告。

所有写操作在单进程内顺序执行，并在落盘后才返回；
重试请求通过 request_id（取题）/ answer_token（作答）识别，绝不重复扣减。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from .catalog import VersionBundle, VersionCatalog
from .engine import (
    ConfirmedResponse,
    SelectionAdvice,
    advise_next,
    derive_ability,
)
from .persistence import (
    ExposureRepository,
    IdempotencyConflict,
    SessionRepository,
)
from .session import (
    CandidateView,
    Contribution,
    FinalResult,
    SelectionExplanation,
    Session,
    SessionError,
)

SORT_RULE = "按综合分降序；分数并列时依次按 tie_rank 升序、题目ID升序确定"


@dataclass(frozen=True)
class NextItemView:
    session_id: str
    request_id: str
    sequence: int
    item_id: str
    prompt: str
    difficulty: float
    skill_ids: tuple[str, ...]
    score: float
    info: float
    fit: float
    novelty: float
    ability_before: float
    uncertainty_before: float
    stop_reason: str | None
    idempotent_replay: bool


class ExamService:
    def __init__(
        self,
        catalog: VersionCatalog,
        sessions: SessionRepository,
        exposures: ExposureRepository,
    ):
        self.catalog = catalog
        self.sessions = sessions
        self.exposures = exposures

    # ---- 内部工具 ----

    def _load(self, session_id: str) -> Session:
        return self.sessions.load(session_id)

    def _bundle_for(self, session: Session) -> VersionBundle:
        return self.catalog.bundle(
            session.version_refs["graph_qvid"],
            session.version_refs["bank_qvid"],
            session.version_refs["rules_qvid"],
        )

    def _remaining_map(self, bundle: VersionBundle) -> dict[str, int]:
        caps = {
            item.item_id: item.exposure_cap
            for item in bundle.bank.items.values()
        }
        return self.exposures.remaining_map(caps)

    # ---- 会话生命周期 ----

    def start_session(
        self,
        graph_qvid: str,
        bank_qvid: str,
        rules_qvid: str,
        learner_id: str | None = None,
        session_id: str | None = None,
    ) -> Session:
        bundle = self.catalog.bundle(graph_qvid, bank_qvid, rules_qvid)
        session = Session(
            session_id=session_id or f"S-{uuid.uuid4().hex[:12]}",
            learner_id=learner_id,
            version_refs=bundle.as_reference(),
        )
        if self.sessions.exists(session.session_id):
            raise SessionError(f"会话已存在: {session.session_id}")
        self.sessions.save(session)
        return session

    # ---- 取下一题（幂等） ----

    def next_item(self, session_id: str, request_id: str) -> NextItemView:
        session = self._load(session_id)
        session.require_in_progress()
        bundle = self._bundle_for(session)

        # 幂等：同一请求重试 -> 放回当时预留并已选的题，不重复扣减曝光。
        reservation = self.exposures.lookup(session_id, request_id)
        replay = reservation is not None
        if reservation is not None:
            item_id = reservation.item_id
            explanation = self._find_selection(session, item_id)
            if explanation is None:
                # 预留已落盘但解释尚未写入（崩溃在两次落盘之间）：补齐解释。
                # 预留证明该题在额度用尽前已合法选中，因此回填时忽略曝光过滤。
                advice = self._advise(bundle, session, ignore_exposure=True)
                explanation = self._explanation_for(
                    advice, item_id, len(session.selections) + 1
                )
                session.record_selection(explanation)
                self.sessions.save(session)
            return self._view(session_id, request_id, explanation, bundle, replay=True)

        advice = self._advise(bundle, session)
        if advice.chosen is None:
            return self._stopped_view(session_id, request_id, advice)

        chosen_item = advice.chosen.item
        # 先扣曝光账本（带请求去重），再写会话；两边都持久化。
        self.exposures.reserve(
            session_id,
            request_id,
            chosen_item.item_id,
            chosen_item.exposure_cap,
        )
        explanation = self._explanation_for(
            advice, chosen_item.item_id, len(session.selections) + 1
        )
        session.record_selection(explanation)
        self.sessions.save(session)
        return self._view(session_id, request_id, explanation, bundle, replay=False)

    def _advise(
        self, bundle: VersionBundle, session: Session, *, ignore_exposure: bool = False
    ) -> SelectionAdvice:
        remaining = {} if ignore_exposure else self._remaining_map(bundle)
        return advise_next(
            graph=bundle.graph,
            bank=bundle.bank,
            rules=bundle.rules,
            responses=session.confirmed_responses,
            presented_items=session.presented_item_ids,
            exposure_remaining=remaining,
        )

    @staticmethod
    def _find_selection(
        session: Session, item_id: str
    ) -> SelectionExplanation | None:
        for selection in session.selections:
            if selection.item_id == item_id:
                return selection
        return None

    def _explanation_for(
        self, advice: SelectionAdvice, item_id: str, sequence: int
    ) -> SelectionExplanation:
        scored = next(
            candidate
            for candidate in advice.candidates
            if candidate.item.item_id == item_id
        )
        return SelectionExplanation(
            sequence=sequence,
            item_id=scored.item.item_id,
            ability_before=advice.ability,
            uncertainty_before=advice.uncertainty,
            info=scored.info,
            fit=scored.fit,
            novelty=scored.novelty,
            score=scored.score,
            candidates=tuple(
                CandidateView(
                    item_id=candidate.item.item_id,
                    score=candidate.score,
                    info=candidate.info,
                    fit=candidate.fit,
                    novelty=candidate.novelty,
                    tie_rank=candidate.item.tie_rank,
                    difficulty=candidate.item.difficulty,
                    skill_ids=tuple(sorted(candidate.item.skill_ids)),
                )
                for candidate in advice.candidates
            ),
            blocked_by_exposure=advice.blocked_by_exposure,
            already_presented=advice.already_presented,
            sort_rule=SORT_RULE,
        )

    def _view(
        self,
        session_id: str,
        request_id: str,
        explanation: SelectionExplanation,
        bundle: VersionBundle,
        replay: bool,
    ) -> NextItemView:
        item = bundle.bank.items[explanation.item_id]
        return NextItemView(
            session_id=session_id,
            request_id=request_id,
            sequence=explanation.sequence,
            item_id=item.item_id,
            prompt=item.prompt,
            difficulty=item.difficulty,
            skill_ids=tuple(sorted(item.skill_ids)),
            score=explanation.score,
            info=explanation.info,
            fit=explanation.fit,
            novelty=explanation.novelty,
            ability_before=explanation.ability_before,
            uncertainty_before=explanation.uncertainty_before,
            stop_reason=None,
            idempotent_replay=replay,
        )

    def _stopped_view(
        self, session_id: str, request_id: str, advice: SelectionAdvice
    ) -> NextItemView:
        return NextItemView(
            session_id=session_id,
            request_id=request_id,
            sequence=0,
            item_id="",
            prompt="",
            difficulty=0.0,
            skill_ids=(),
            score=0.0,
            info=0.0,
            fit=0.0,
            novelty=0.0,
            ability_before=advice.ability,
            uncertainty_before=advice.uncertainty,
            stop_reason=advice.stop_reason,
            idempotent_replay=False,
        )

    # ---- 提交作答（幂等） ----

    def submit_answer(
        self,
        session_id: str,
        item_id: str,
        answer: str,
        answer_token: str | None = None,
    ) -> dict[str, Any]:
        session = self._load(session_id)
        session.require_in_progress()
        if answer_token is not None and answer_token in session.answers_by_token:
            if session.answers_by_token[answer_token] != item_id:
                raise IdempotencyConflict(
                    f"作答令牌 {answer_token} 已用于其他题目"
                )
            record = session.find_response(item_id)
            return {"item_id": item_id, "correct": record.correct, "replay": True}

        bundle = self._bundle_for(session)
        item = bundle.bank.items.get(item_id)
        if item is None:
            raise SessionError(f"题库 {session.version_refs['bank_qvid']} 中没有题目 {item_id}")

        before = derive_ability(
            bundle.graph, bundle.bank, bundle.rules, session.confirmed_responses
        )
        record = session.record_response(item_id, answer, item.is_correct(answer))
        after = derive_ability(
            bundle.graph, bundle.bank, bundle.rules, session.confirmed_responses
        )
        deltas = {
            skill_id: after.skills[skill_id].mastery - before.skills[skill_id].mastery
            for skill_id in sorted(item.skill_ids)
        }
        session.attach_contribution(
            Contribution(
                item_id=item_id,
                correct=record.correct,
                ability_before=before.overall_ability,
                ability_after=after.overall_ability,
                uncertainty_before=before.uncertainty,
                uncertainty_after=after.uncertainty,
                skill_deltas=deltas,
            )
        )
        if answer_token is not None:
            session.answers_by_token[answer_token] = item_id
        self.sessions.save(session)
        return {"item_id": item_id, "correct": record.correct, "replay": False}

    # ---- 教师作废异常作答 ----

    def invalidate_response(
        self, session_id: str, item_id: str, teacher_id: str, reason: str
    ) -> Session:
        session = self._load(session_id)
        session.invalidate_response(item_id, teacher_id, reason)
        self.sessions.save(session)
        return session

    # ---- 结测与重评 ----

    def finish(self, session_id: str, force: bool = False) -> FinalResult:
        session = self._load(session_id)
        bundle = self._bundle_for(session)
        confirmed = session.confirmed_responses
        if not force and len(confirmed) < bundle.rules.min_items:
            raise SessionError(
                f"已确认作答 {len(confirmed)} 题，少于最少题量 "
                f"{bundle.rules.min_items}；如教师确认可强制结测"
            )
        result = self._build_result(session, bundle, produced_by="finish")
        session.finish(result)
        self.sessions.save(session)
        return result

    def regrade(
        self, session_id: str, teacher_id: str, reason: str
    ) -> tuple[FinalResult, FinalResult]:
        """受控重评：冻结版本不变，仅依据仍有效作答重算；旧结果保留在谱系中。"""
        if not teacher_id or not reason:
            raise SessionError("重评必须提供教师标识与申诉理由")
        session = self._load(session_id)
        bundle = self._bundle_for(session)
        new_result = self._build_result(
            session,
            bundle,
            produced_by="regrade",
            teacher_id=teacher_id,
            reason=reason,
        )
        previous = session.current_result()
        session.regrade(new_result)
        self.sessions.save(session)
        return previous, new_result

    def _build_result(
        self,
        session: Session,
        bundle: VersionBundle,
        produced_by: str,
        teacher_id: str | None = None,
        reason: str | None = None,
    ) -> FinalResult:
        snapshot = derive_ability(
            bundle.graph, bundle.bank, bundle.rules, session.confirmed_responses
        )
        direct_gaps = snapshot.gap_skills(bundle.rules.gap_threshold)
        propagated = bundle.graph.propagate_gaps(direct_gaps)
        chains = bundle.graph.gap_chains(direct_gaps)
        score = snapshot.overall_ability
        return FinalResult(
            result_index=len(session.lineage),
            score=round(score, 6),
            level=bundle.rules.level_for(score),
            confirmed_count=len(session.confirmed_responses),
            invalidated_count=sum(
                1 for r in session.responses if r.status == "invalidated"
            ),
            skill_mastery={k: round(v.mastery, 6) for k, v in snapshot.skills.items()},
            skill_variance={k: round(v.variance, 8) for k, v in snapshot.skills.items()},
            skill_answers=dict(snapshot.answer_counts),
            gaps=dict(sorted(propagated.items())),
            gap_chains={k: v for k, v in sorted(chains.items())},
            version_refs=dict(session.version_refs),
            produced_by=produced_by,
            teacher_id=teacher_id,
            reason=reason,
        )

    # ---- 教师可解释报告 ----

    def teacher_report(self, session_id: str) -> dict[str, Any]:
        """逐题选择依据 + 每项有效作答的贡献 + 版本绑定与结果谱系。"""
        session = self._load(session_id)
        bundle = self._bundle_for(session)
        contributions = self._replay_contributions(session, bundle)
        final = session.current_result()
        return {
            "session_id": session.session_id,
            "learner_id": session.learner_id,
            "status": session.status,
            "version_refs": session.version_refs,
            "selection_rule": SORT_RULE,
            "selections": [
                {
                    "sequence": s.sequence,
                    "item_id": s.item_id,
                    "ability_before": round(s.ability_before, 6),
                    "uncertainty_before": round(s.uncertainty_before, 6),
                    "score_breakdown": {
                        "info": round(s.info, 6),
                        "fit": round(s.fit, 6),
                        "novelty": round(s.novelty, 6),
                        "total": round(s.score, 6),
                    },
                    "candidates": [
                        {
                            "item_id": c.item_id,
                            "total": round(c.score, 6),
                            "info": round(c.info, 6),
                            "fit": round(c.fit, 6),
                            "novelty": round(c.novelty, 6),
                            "tie_rank": c.tie_rank,
                            "difficulty": c.difficulty,
                            "skill_ids": list(c.skill_ids),
                        }
                        for c in s.candidates
                    ],
                    "blocked_by_exposure": list(s.blocked_by_exposure),
                    "already_presented": list(s.already_presented),
                    "sort_rule": s.sort_rule,
                }
                for s in session.selections
            ],
            "responses": [
                {
                    "sequence": r.sequence,
                    "item_id": r.item_id,
                    "answer": r.answer,
                    "correct": r.correct,
                    "status": r.status,
                    "invalidated_by": r.invalidated_by,
                    "invalidation_reason": r.invalidation_reason,
                    "contribution": contributions.get(r.item_id),
                }
                for r in session.responses
            ],
            "result": _result_dict(final) if final else None,
            "result_lineage": [_result_dict(r) for r in session.lineage],
            "audit": [
                {"sequence": a.sequence, "kind": a.kind, "detail": a.detail}
                for a in session.audit
            ],
        }

    def _replay_contributions(
        self, session: Session, bundle: VersionBundle
    ) -> dict[str, dict[str, Any]]:
        """按当前仍有效作答重放，给出每题对能力判断的边际贡献。"""
        result: dict[str, dict[str, Any]] = {}
        history: list[ConfirmedResponse] = []
        before = derive_ability(bundle.graph, bundle.bank, bundle.rules, ())
        for record in session.responses:
            if record.status != "confirmed":
                continue
            history.append(
                ConfirmedResponse(item_id=record.item_id, correct=record.correct)
            )
            after = derive_ability(
                bundle.graph, bundle.bank, bundle.rules, tuple(history)
            )
            item = bundle.bank.items[record.item_id]
            result[record.item_id] = {
                "correct": record.correct,
                "ability_delta": round(
                    after.overall_ability - before.overall_ability, 6
                ),
                "uncertainty_delta": round(
                    after.uncertainty - before.uncertainty, 6
                ),
                "skill_mastery_delta": {
                    skill_id: round(
                        after.skills[skill_id].mastery
                        - before.skills[skill_id].mastery,
                        6,
                    )
                    for skill_id in sorted(item.skill_ids)
                },
            }
            before = after
        return result


def _result_dict(result: FinalResult) -> dict[str, Any]:
    return {
        "result_index": result.result_index,
        "produced_by": result.produced_by,
        "score": result.score,
        "level": result.level,
        "confirmed_count": result.confirmed_count,
        "invalidated_count": result.invalidated_count,
        "skill_mastery": result.skill_mastery,
        "skill_variance": result.skill_variance,
        "skill_answers": result.skill_answers,
        "gaps": result.gaps,
        "gap_chains": {k: list(v) for k, v in result.gap_chains.items()},
        "version_refs": result.version_refs,
        "teacher_id": result.teacher_id,
        "reason": result.reason,
    }
