"""自适应测评应用服务。

所有用例方法都以纯字典返回，便于直接序列化为 HTTP 响应；关键不变量：

* **幂等** —— 写操作必须携带请求标识 ``request_id``；同一标识重试返回
  首次结果，题目曝光额度绝不重复扣减。
* **断网续考** —— 已选出但未作答的题保持 PENDING；学员必须凭原请求
  标识取回同一题与同一令牌，换标识请求新题会被拒绝。
* **版本固化** —— 会话开始时钉住图谱/题库/规则修订号；结束结果与
  受控重评都按钉住版本计算，后续题库修订不影响旧结果。
* **审计留痕** —— 作废与重评必须有教师身份与理由，旧结果进入
  ``result_history`` 永久保留。
"""
from __future__ import annotations

import hashlib
import math
import threading
import time
from collections.abc import Callable

from .errors import (
    IdempotencyError,
    NotFoundError,
    PendingSelectionError,
    RegradePreconditionError,
    SessionClosedError,
    ValidationError,
)
from .models import (
    Evidence,
    EvidenceStatus,
    ItemBank,
    ScoringRuleSet,
    Session,
    SessionState,
    SkillGraph,
)
from .scoring import (
    _prior,
    explain_contribution,
    fold_evidence,
    score_session,
)
from .selector import select_next, should_stop
from .storage import ContentRegistry, ExposureLedger, SessionRepository


def _token(session_id: str, request_id: str, item_id: str) -> str:
    raw = f"{session_id}|{request_id}|{item_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class ExamService:
    """用例编排门面。线程安全（进程内锁）；持久化为原子 JSON 文件。"""

    def __init__(self, root: str, clock: Callable[[], float] = time.time):
        self.registry = ContentRegistry(root)
        self.ledger = ExposureLedger(root)
        self.repo = SessionRepository(root)
        self._clock = clock
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # 内容登记
    # ------------------------------------------------------------------ #

    def register_graph(self, graph: SkillGraph) -> dict:
        with self._lock:
            self.registry.add_graph(graph)
            return {"graph_version_key": graph.version.key}

    def register_bank(self, bank: ItemBank) -> dict:
        with self._lock:
            self._assert_bank_matches_graph(bank)
            self.registry.add_bank(bank)
            return {"bank_version_key": bank.version.key}

    def _assert_bank_matches_graph(self, bank: ItemBank) -> None:
        entity_id, revision = self._split_key(bank.graph_version_key)
        graph = self.registry.get_graph(entity_id, revision)
        for item in bank.items:
            if not graph.contains(item.skill_id):
                raise ValidationError(
                    f"题目 {item.item_id} 绑定的技能 {item.skill_id} "
                    f"不在图谱 {bank.graph_version_key} 中"
                )

    def register_rules(self, rules: ScoringRuleSet) -> dict:
        with self._lock:
            self.registry.add_rules(rules)
            return {"rule_set_key": rules.key}

    @staticmethod
    def _split_key(key: str) -> tuple[str, int]:
        try:
            entity, revision = key.rsplit(":", 1)
            return entity, int(revision)
        except (ValueError, AttributeError) as exc:
            raise ValidationError(f"版本标识格式非法：{key!r}") from exc

    # ------------------------------------------------------------------ #
    # 会话生命周期
    # ------------------------------------------------------------------ #

    def start_session(
        self,
        session_id: str,
        learner_id: str,
        graph_entity_id: str,
        bank_entity_id: str,
        rule_set_id: str,
        graph_revision: int | None = None,
        bank_revision: int | None = None,
        rule_revision: int | None = None,
    ) -> dict:
        with self._lock:
            if self.repo.exists(session_id):
                raise ValidationError(f"会话 {session_id} 已存在")
            graph = (self.registry.latest_graph(graph_entity_id)
                     if graph_revision is None
                     else self.registry.get_graph(graph_entity_id,
                                                  graph_revision))
            bank = (self.registry.latest_bank(bank_entity_id)
                    if bank_revision is None
                    else self.registry.get_bank(bank_entity_id,
                                                bank_revision))
            if bank.graph_version_key != graph.version.key:
                raise ValidationError(
                    f"题库 {bank.version.key} 基于图谱 "
                    f"{bank.graph_version_key}，与所选 {graph.version.key} 不符"
                )
            if rule_revision is None:
                raise ValidationError("规则修订号必须显式指定以固化版本")
            rules = self.registry.get_rules(rule_set_id, rule_revision)

            session = Session(
                session_id=session_id,
                learner_id=learner_id,
                graph_version_key=graph.version.key,
                bank_version_key=bank.version.key,
                rule_set_key=rules.key,
                created_at=self._clock(),
            )
            self.repo.save(session)
            return {
                "session_id": session_id,
                "learner_id": learner_id,
                "graph_version_key": graph.version.key,
                "bank_version_key": bank.version.key,
                "rule_set_key": rules.key,
                "created_at": session.created_at,
            }

    def _load_context(self, session_id: str):
        session = self.repo.get(session_id)
        g_id, g_rev = self._split_key(session.graph_version_key)
        b_id, b_rev = self._split_key(session.bank_version_key)
        r_id, r_rev = self._split_key(session.rule_set_key)
        return (session,
                self.registry.get_graph(g_id, g_rev),
                self.registry.get_bank(b_id, b_rev),
                self.registry.get_rules(r_id, r_rev))

    # ------------------------------------------------------------------ #
    # 选题（幂等 + 断网续考）
    # ------------------------------------------------------------------ #

    def request_next_item(self, session_id: str, request_id: str) -> dict:
        if not request_id:
            raise ValidationError("request_id 不能为空")
        with self._lock:
            session, graph, bank, rules = self._load_context(session_id)
            stored = self._idem_record(session_id, request_id, "next_item")
            if stored is not None:
                # 同请求重试（无论响应是否曾送达）：原样返回并标注重试，
                # 不再触碰曝光台账。
                return {**stored, "retried": True}

            if session.state is SessionState.FINISHED:
                raise SessionClosedError("会话已结束，不能再请求题目")

            pending = session.pending()
            if pending is not None:
                if pending.request_id != request_id:
                    # 必须凭原请求续考，防止跳题与重复扣减。
                    raise PendingSelectionError(
                        pending.request_id, pending.token, pending.item_id
                    )
                # 同一请求重试（含崩溃恢复）：原样返回，绝不再次扣曝光。
                return self._item_view(session, pending, retried=True)

            # 崩溃窗口：曝光已占用但证据未落盘（或证据在、幂等记录丢）。
            reserved = self.ledger.reserved_item_for(session_id, request_id)
            if reserved is not None:
                existing = session.by_item(reserved)
                if existing is not None:
                    return self._item_view(session, existing, retried=True)
                recovered_item = bank.get(reserved)
                now = self._clock()
                recovered_ev = Evidence(
                    item_id=recovered_item.item_id,
                    skill_id=recovered_item.skill_id,
                    difficulty=recovered_item.difficulty,
                    status=EvidenceStatus.PENDING,
                    presented_at=now,
                    request_id=request_id,
                    selection_reason=(
                        f"恢复请求 {request_id} 在连接中断前已占用曝光的"
                        f"题目 {recovered_item.item_id}（技能 "
                        f"{recovered_item.skill_id}，难度 "
                        f"{recovered_item.difficulty.value}）；未重复扣减曝光"
                    ),
                    token=_token(session_id, request_id,
                                 recovered_item.item_id),
                )
                session.append_evidence(recovered_ev)
                view = self._item_view(session, recovered_ev, retried=True)
                self.repo.save(session)
                self.repo.save_idempotency(
                    session_id, request_id, "next_item", view
                )
                return view

            selection = self._select_with_exposure(
                session, graph, bank, rules, request_id
            )
            if selection is None:
                # 无题可出：达到最少题量即正常结束，否则标记异常结束。
                return self._finish_due_to_exhaustion(
                    session, graph, bank, rules, request_id
                )

            item, reason = selection
            now = self._clock()
            token = _token(session_id, request_id, item.item_id)
            evidence = Evidence(
                item_id=item.item_id,
                skill_id=item.skill_id,
                difficulty=item.difficulty,
                status=EvidenceStatus.PENDING,
                presented_at=now,
                request_id=request_id,
                selection_reason=reason,
                token=token,
            )
            session.append_evidence(evidence)
            session.audit_log.append(
                f"[{now}] 呈现题目 {item.item_id}（请求 {request_id}，"
                f"曝光已扣减至 {self.ledger.counts().get(item.item_id, 0)}）"
            )
            # 先持久化会话再记幂等记录；崩溃在两者之间时，重试凭
            # PENDING 证据的 request_id 同样能识别，不会重复扣减。
            self.repo.save(session)
            view = self._item_view(session, evidence)
            self.repo.save_idempotency(
                session_id, request_id, "next_item", view
            )
            return view

    def _select_with_exposure(self, session, graph, bank, rules,
                              request_id: str):
        """选题并幂等占用全局曝光；占用竞争失败则带新台账重试。

        调用方已保证该请求此前没有占用记录（见 request_next_item 的
        崩溃窗口恢复分支）。
        """
        from .scoring import assess_skills

        def _choose():
            confirmed = session.confirmed()
            assessments = assess_skills(graph, confirmed, rules)
            gaps = frozenset(a.skill_id for a in assessments if a.gap)
            mastery = frozenset(a.skill_id for a in assessments
                                if a.mastered)
            return select_next(
                graph=graph,
                bank=bank,
                confirmed_evidence=confirmed,
                presented_item_ids=frozenset(
                    e.item_id for e in session.evidence
                ),
                exposures=self.ledger.counts(),
                rules=rules,
                confirmed_gaps=gaps,
                confirmed_mastery=mastery,
            )

        for _ in range(len(bank.items) + 1):
            selection = _choose()
            if selection is None:
                return None
            item = selection.item
            ok, billed_item = self.ledger.reserve(
                session.session_id, request_id,
                item.item_id, item.max_exposure
            )
            if not ok:
                continue  # 竞争中先达上限，带新计数重选。
            assert billed_item == item.item_id
            return item, selection.reason
        return None

    # ------------------------------------------------------------------ #
    # 作答判分
    # ------------------------------------------------------------------ #

    def submit_answer(
        self, session_id: str, request_id: str, token: str, answer: str
    ) -> dict:
        if not request_id or not token:
            raise ValidationError("request_id 与 token 均不能为空")
        if not isinstance(answer, str) or answer == "":
            raise ValidationError("作答内容不能为空")
        with self._lock:
            session, graph, bank, rules = self._load_context(session_id)
            stored = self._idem_record(session_id, request_id, "answer")
            if stored is not None:
                return stored
            if session.state is SessionState.FINISHED:
                # 崩溃窗口恢复：作答已落盘并触发了结束，但幂等记录未及写。
                recovered = self._recover_answer(session, request_id, token)
                if recovered is not None:
                    self.repo.save_idempotency(
                        session_id, request_id, "answer", recovered
                    )
                    return recovered
                raise SessionClosedError("会话已结束，不能再提交作答")

            recovered = self._recover_answer(session, request_id, token)
            if recovered is not None:
                self.repo.save_idempotency(
                    session_id, request_id, "answer", recovered
                )
                return recovered

            pending = session.pending()
            if pending is None:
                raise ValidationError("当前没有待作答的题目")
            if pending.request_id == request_id:
                raise IdempotencyError(
                    "作答请求标识不能与选题请求标识相同"
                )
            if pending.token != token:
                raise ValidationError(
                    "令牌与当前待作答题不符（可能来自过期的选题请求）"
                )

            item = bank.get(pending.item_id)
            correct = answer.strip() == item.answer.strip()
            now = self._clock()

            confirmed_before = session.confirmed()
            belief_before = fold_evidence(confirmed_before, rules).get(
                pending.skill_id, _prior(rules)
            )
            contribution = explain_contribution(
                belief_before, item.b, correct, rules
            )

            scored = Evidence(
                item_id=pending.item_id,
                skill_id=pending.skill_id,
                difficulty=pending.difficulty,
                status=EvidenceStatus.CONFIRMED,
                presented_at=pending.presented_at,
                request_id=pending.request_id,
                selection_reason=pending.selection_reason,
                token=pending.token,
                answer_request_id=request_id,
                answer_given=answer,
                is_correct=correct,
                answered_at=now,
                contribution=contribution,
            )
            session.replace_evidence(pending.item_id, scored)
            session.audit_log.append(
                f"[{now}] 题目 {item.item_id} 判分："
                f"{'正确' if correct else '错误'}"
            )

            response = {
                "session_id": session_id,
                "item_id": item.item_id,
                "is_correct": correct,
                "contribution": contribution,
            }

            stop, stop_reason = self._evaluate_stop(
                session, graph, bank, rules
            )
            if stop:
                self._finalize(session, graph, bank, rules, stop_reason)
                response["finished"] = True
                response["result"] = self._result_view(session)
            else:
                response["finished"] = False

            self.repo.save(session)
            self.repo.save_idempotency(
                session_id, request_id, "answer", response
            )
            return response

    def _recover_answer(self, session: Session, request_id: str,
                        token: str) -> dict | None:
        """识别“已落盘但幂等记录缺失”的作答重试（崩溃窗口恢复）。"""
        for ev in session.evidence:
            if (ev.answer_request_id == request_id
                    and ev.status is EvidenceStatus.CONFIRMED):
                if ev.token != token:
                    raise IdempotencyError(
                        f"请求标识 {request_id} 重试时携带的令牌与首次请求不一致"
                    )
                response = {
                    "session_id": session.session_id,
                    "item_id": ev.item_id,
                    "is_correct": bool(ev.is_correct),
                    "contribution": ev.contribution,
                }
                response["finished"] = session.state is SessionState.FINISHED
                if session.state is SessionState.FINISHED:
                    response["result"] = self._result_view(session)
                return response
        return None

    def _evaluate_stop(self, session, graph, bank, rules):
        """模拟“再选一次”判断是否无题可出，并计算总体不确定性。"""
        from .scoring import assess_skills

        confirmed = session.confirmed()
        count = len(confirmed)
        assessments = assess_skills(graph, confirmed, rules)
        gaps = frozenset(a.skill_id for a in assessments if a.gap)
        mastery = frozenset(a.skill_id for a in assessments if a.mastered)
        presented = frozenset(e.item_id for e in session.evidence)
        selection = select_next(
            graph=graph,
            bank=bank,
            confirmed_evidence=confirmed,
            presented_item_ids=presented,
            exposures=self.ledger.counts(),
            rules=rules,
            confirmed_gaps=gaps,
            confirmed_mastery=mastery,
        )
        if assessments:
            total_w = sum(max(a.support, 1) for a in assessments)
            weighted_sigma = math.sqrt(
                sum(a.sigma**2 * max(a.support, 1) for a in assessments)
                / total_w
            )
        else:
            weighted_sigma = (_prior(rules).sigma)
        return should_stop(count, rules, weighted_sigma, selection is None)

    def _finalize(self, session, graph, bank, rules, reason: str) -> None:
        result = score_session(
            graph=graph,
            evidence=session.evidence,
            rules=rules,
            finished_at=self._clock(),
            bank_version_key=bank.version.key,
        )
        session.state = SessionState.FINISHED
        session.result = result
        session.audit_log.append(
            f"[{result.finished_at}] 会话结束：{reason}；"
            f"等级 {result.level}（θ={result.overall_theta:.2f}，"
            f"规则 {result.rule_set_key}）"
        )

    def _finish_due_to_exhaustion(self, session, graph, bank, rules,
                                  request_id: str | None = None):
        stop, reason = self._evaluate_stop(session, graph, bank, rules)
        # 无题可出时无论是否达到最少题量都结束；理由中注明题量不足。
        self._finalize(session, graph, bank, rules, reason)
        self.repo.save(session)
        view = self._finished_view(session)
        if request_id is not None:
            self.repo.save_idempotency(
                session.session_id, request_id, "next_item", view
            )
        return view

    def finish_session(self, session_id: str, request_id: str) -> dict:
        """教师/学员主动交卷；幂等。"""
        if not request_id:
            raise ValidationError("request_id 不能为空")
        with self._lock:
            session, graph, bank, rules = self._load_context(session_id)
            stored = self._idem_record(session_id, request_id, "finish")
            if stored is not None:
                return stored
            if session.state is SessionState.FINISHED:
                return self._finished_view(session)
            if session.pending() is not None:
                raise ValidationError("仍有题目未作答，不能交卷")
            self._finalize(session, graph, bank, rules, "学员主动交卷")
            self.repo.save(session)
            view = self._finished_view(session)
            self.repo.save_idempotency(
                session_id, request_id, "finish", view
            )
            return view

    # ------------------------------------------------------------------ #
    # 教师作废 + 受控重评
    # ------------------------------------------------------------------ #

    def void_evidence(
        self,
        session_id: str,
        item_id: str,
        teacher_id: str,
        reason: str,
        request_id: str | None = None,
    ) -> dict:
        """按证据作废一道异常作答。

        会话进行中：证据转为 VOIDED，后续评分与选题不再使用它，已扣
        曝光不退还（避免作废成为绕过曝光控制的通道）。会话已结束：
        作废后立即执行一次受控重评，旧结果进入历史。``request_id``
        存在时该操作具备请求级幂等。
        """
        if not teacher_id or not reason:
            raise ValidationError("作废必须登记教师身份与理由")
        with self._lock:
            session, graph, bank, rules = self._load_context(session_id)
            if request_id is not None:
                stored = self._idem_record(session_id, request_id, "void")
                if stored is not None:
                    if stored.get("item_id") != item_id:
                        raise IdempotencyError(
                            f"请求标识 {request_id} 已用于作废另一道题"
                        )
                    return stored
            evidence = session.by_item(item_id)
            if evidence is None:
                raise NotFoundError(f"会话中不存在题目 {item_id} 的证据")
            if evidence.status is EvidenceStatus.VOIDED:
                raise ValidationError("该证据已作废，不能重复作废")
            if evidence.status is EvidenceStatus.PENDING:
                raise ValidationError("题目尚未作答，不能作废（请按断网续考处理）")

            now = self._clock()
            voided = Evidence(
                item_id=evidence.item_id,
                skill_id=evidence.skill_id,
                difficulty=evidence.difficulty,
                status=EvidenceStatus.VOIDED,
                presented_at=evidence.presented_at,
                request_id=evidence.request_id,
                selection_reason=evidence.selection_reason,
                token=evidence.token,
                answer_request_id=evidence.answer_request_id,
                answer_given=evidence.answer_given,
                is_correct=None,
                answered_at=evidence.answered_at,
                void_reason=reason,
                voided_by=teacher_id,
                voided_at=now,
                contribution=evidence.contribution,
            )
            session.replace_evidence(item_id, voided)
            session.audit_log.append(
                f"[{now}] 教师 {teacher_id} 作废题目 {item_id}：{reason}"
            )

            response = {"session_id": session_id, "item_id": item_id,
                        "voided": True}
            if session.state is SessionState.FINISHED:
                response["regrade"] = self._regrade_locked(
                    session, graph, bank, rules, teacher_id,
                    f"作废 {item_id} 后受控重评：{reason}"
                )
            self.repo.save(session)
            if request_id is not None:
                self.repo.save_idempotency(
                    session_id, request_id, "void", response
                )
            return response

    def regrade(
        self, session_id: str, teacher_id: str, reason: str,
        request_id: str | None = None,
    ) -> dict:
        """受控重评/申诉重算。

        只按会话钉住的图谱、题库、规则修订与当前有效证据重算；题库的
        后续修订绝不参与。原结果快照完整保留在结果历史中。
        """
        if not teacher_id or not reason:
            raise ValidationError("重评必须登记教师身份与理由")
        with self._lock:
            session, graph, bank, rules = self._load_context(session_id)
            if request_id is not None:
                stored = self._idem_record(session_id, request_id, "regrade")
                if stored is not None:
                    return stored
            if session.state is not SessionState.FINISHED:
                raise RegradePreconditionError("会话尚未结束，无需重评")
            result_view = self._regrade_locked(
                session, graph, bank, rules, teacher_id, reason
            )
            self.repo.save(session)
            response = {"session_id": session_id, "regrade": result_view}
            if request_id is not None:
                self.repo.save_idempotency(
                    session_id, request_id, "regrade", response
                )
            return response

    def _regrade_locked(
        self, session, graph, bank, rules, teacher_id, reason
    ) -> dict:
        if session.result is None:
            raise RegradePreconditionError("缺少原结果，无法重评")
        previous = session.result
        new_result = score_session(
            graph=graph,
            evidence=session.evidence,
            rules=rules,
            finished_at=self._clock(),
            bank_version_key=bank.version.key,
            regrade_of=str(previous.finished_at),
        )
        session.result_history.append(previous)
        session.result = new_result
        session.regrade_count += 1
        session.audit_log.append(
            f"[{new_result.finished_at}] 教师 {teacher_id} 受控重评（第 "
            f"{session.regrade_count} 次）：{reason}；等级 "
            f"{previous.level} -> {new_result.level}，"
            f"θ {previous.overall_theta:.2f} -> {new_result.overall_theta:.2f}；"
            f"仍按规则 {new_result.rule_set_key} / 题库 "
            f"{new_result.bank_version_key} 计算"
        )
        return self._result_view(session)

    # ------------------------------------------------------------------ #
    # 查询与解释
    # ------------------------------------------------------------------ #

    def get_session(self, session_id: str) -> dict:
        with self._lock:
            session, _, _, _ = self._load_context(session_id)
            return self._session_view(session)

    def explain_session(self, session_id: str) -> dict:
        """教师视角：逐题选择依据 + 每题对能力判断的贡献。"""
        with self._lock:
            session, graph, bank, rules = self._load_context(session_id)
            items = []
            for idx, ev in enumerate(session.evidence, start=1):
                entry = {
                    "order": idx,
                    "item_id": ev.item_id,
                    "skill_id": ev.skill_id,
                    "difficulty": ev.difficulty.value,
                    "status": ev.status.value,
                    "presented_at": ev.presented_at,
                    "request_id": ev.request_id,
                    "selection_reason": ev.selection_reason,
                    "answer_given": ev.answer_given,
                    "is_correct": ev.is_correct,
                    "contribution": ev.contribution,
                    "void_reason": ev.void_reason,
                    "voided_by": ev.voided_by,
                }
                items.append(entry)
            view = {
                "session_id": session_id,
                "learner_id": session.learner_id,
                "state": session.state.value,
                "pinned_versions": {
                    "graph": session.graph_version_key,
                    "bank": session.bank_version_key,
                    "rules": session.rule_set_key,
                },
                "items": items,
                "audit_log": list(session.audit_log),
            }
            if session.result is not None:
                view["result"] = self._result_view(session)
                view["result_history"] = [
                    self._result_dict(r) for r in session.result_history
                ]
            return view

    # ------------------------------------------------------------------ #
    # 视图与幂等辅助
    # ------------------------------------------------------------------ #

    def _idem_record(self, session_id, request_id, op) -> dict | None:
        """命中同操作幂等记录时返回首次响应；跨操作复用则报错。"""
        record = self.repo.load_idempotency(session_id, request_id)
        if record is None:
            return None
        if record["op"] != op:
            raise IdempotencyError(
                f"请求标识 {request_id} 已用于操作 {record['op']}，"
                f"不能再用于 {op}"
            )
        return record["response"]

    def _item_view(self, session: Session, evidence: Evidence,
                   retried: bool = False) -> dict:
        bank = self.registry.get_bank(
            *self._split_key(session.bank_version_key)
        )
        item = bank.get(evidence.item_id)
        return {
            "session_id": session.session_id,
            "request_id": evidence.request_id,
            "token": evidence.token,
            "item_id": item.item_id,
            "skill_id": item.skill_id,
            "difficulty": item.difficulty.value,
            "prompt": item.prompt,
            "selection_reason": evidence.selection_reason,
            "retried": retried,
        }

    def _session_view(self, session: Session) -> dict:
        return {
            "session_id": session.session_id,
            "learner_id": session.learner_id,
            "state": session.state.value,
            "graph_version_key": session.graph_version_key,
            "bank_version_key": session.bank_version_key,
            "rule_set_key": session.rule_set_key,
            "pending_item_id": (session.pending().item_id
                                if session.pending() else None),
            "confirmed_count": len(session.confirmed()),
            "result": self._result_view(session) if session.result else None,
        }

    def _finished_view(self, session: Session) -> dict:
        return {
            "session_id": session.session_id,
            "finished": True,
            "result": self._result_view(session),
        }

    @staticmethod
    def _result_dict(result) -> dict:
        return {
            "overall_theta": result.overall_theta,
            "overall_sigma": result.overall_sigma,
            "level": result.level,
            "rule_set_key": result.rule_set_key,
            "graph_version_key": result.graph_version_key,
            "bank_version_key": result.bank_version_key,
            "finished_at": result.finished_at,
            "item_count": result.item_count,
            "regrade_of": result.regrade_of,
            "level_basis": result.level_basis,
            "skills": [
                {
                    "skill_id": a.skill_id,
                    "theta": a.theta,
                    "sigma": a.sigma,
                    "mastered": a.mastered,
                    "gap": a.gap,
                    "support": a.support,
                    "successes": a.successes,
                    "failures": a.failures,
                    "gap_propagated_from": list(a.gap_propagated_from),
                }
                for a in result.skill_assessments
            ],
        }

    def _result_view(self, session: Session) -> dict | None:
        if session.result is None:
            return None
        return self._result_dict(session.result)
