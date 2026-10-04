"""测评会话聚合。

会话是只追加的事件流：投放、作答、作废、结测、重评都产生带逻辑序号的记录。
能力判断永远可以从“当前仍有效的已确认作答”重放得到，因此：

- 进程重启后，未结束的会话加载状态即可继续；
- 教师作废异常作答后，受控重评只是用同一套冻结版本规则重放剩余作答；
- 每次结测/重评结果进入结果谱系，旧结果标记为被取代但不被删除或改写。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .engine import ConfirmedResponse

IN_PROGRESS = "in_progress"
FINISHED = "finished"

CONFIRMED = "confirmed"
INVALIDATED = "invalidated"


class SessionError(ValueError):
    """会话状态或请求不合法。"""


@dataclass(frozen=True)
class CandidateView:
    item_id: str
    score: float
    info: float
    fit: float
    novelty: float
    tie_rank: int
    difficulty: float
    skill_ids: tuple[str, ...]


@dataclass(frozen=True)
class SelectionExplanation:
    sequence: int
    item_id: str
    ability_before: float
    uncertainty_before: float
    info: float
    fit: float
    novelty: float
    score: float
    candidates: tuple[CandidateView, ...]
    blocked_by_exposure: tuple[str, ...]
    already_presented: tuple[str, ...]
    sort_rule: str


@dataclass(frozen=True)
class Contribution:
    """单条作对各技能掌握度的边际贡献（delta 可正可负）。"""

    item_id: str
    correct: bool
    ability_before: float
    ability_after: float
    uncertainty_before: float
    uncertainty_after: float
    skill_deltas: dict[str, float]


@dataclass(frozen=True)
class ResponseRecord:
    sequence: int
    item_id: str
    answer: str
    correct: bool
    status: str = CONFIRMED
    invalidated_by: str | None = None
    invalidation_reason: str | None = None


@dataclass(frozen=True)
class FinalResult:
    result_index: int
    score: float
    level: str
    confirmed_count: int
    invalidated_count: int
    skill_mastery: dict[str, float]
    skill_variance: dict[str, float]
    skill_answers: dict[str, int]
    gaps: dict[str, str]  # 技能ID -> direct / inferred
    gap_chains: dict[str, tuple[str, ...]]
    version_refs: dict[str, str]
    produced_by: str = "finish"  # finish / regrade
    teacher_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class AuditEntry:
    sequence: int
    kind: str
    detail: dict[str, Any]


@dataclass
class Session:
    session_id: str
    learner_id: str | None
    version_refs: dict[str, str]
    status: str = IN_PROGRESS
    responses: list[ResponseRecord] = field(default_factory=list)
    selections: list[SelectionExplanation] = field(default_factory=list)
    contributions: list[Contribution] = field(default_factory=list)
    lineage: list[FinalResult] = field(default_factory=list)
    audit: list[AuditEntry] = field(default_factory=list)
    # 作答请求令牌 -> 题目ID（作答提交幂等）
    answers_by_token: dict[str, str] = field(default_factory=dict)
    clock: int = 0

    # ---- 派生 ----

    @property
    def presented_item_ids(self) -> frozenset[str]:
        return frozenset(s.item_id for s in self.selections)

    @property
    def confirmed_responses(self) -> tuple[ConfirmedResponse, ...]:
        return tuple(
            ConfirmedResponse(item_id=r.item_id, correct=r.correct)
            for r in self.responses
            if r.status == CONFIRMED
        )

    def find_response(self, item_id: str) -> ResponseRecord | None:
        for record in self.responses:
            if record.item_id == item_id:
                return record
        return None

    def current_result(self) -> FinalResult | None:
        return self.lineage[-1] if self.lineage else None

    # ---- 命令 ----

    def _tick(self) -> int:
        self.clock += 1
        return self.clock

    def require_in_progress(self) -> None:
        if self.status != IN_PROGRESS:
            raise SessionError(f"会话 {self.session_id} 已结束，不能继续作答")

    def record_selection(self, explanation: SelectionExplanation) -> None:
        self.require_in_progress()
        if explanation.item_id in self.presented_item_ids:
            raise SessionError(f"题目 {explanation.item_id} 已在本会话投放过")
        self.selections.append(explanation)
        self.audit.append(
            AuditEntry(
                self._tick(),
                "present",
                {
                    "sequence": explanation.sequence,
                    "item_id": explanation.item_id,
                    "score": explanation.score,
                },
            )
        )

    def record_response(self, item_id: str, answer: str, correct: bool) -> ResponseRecord:
        self.require_in_progress()
        if item_id not in self.presented_item_ids:
            raise SessionError(f"题目 {item_id} 未投放，不能提交作答")
        existing = self.find_response(item_id)
        if existing is not None and existing.status == CONFIRMED:
            raise SessionError(f"题目 {item_id} 已有确认作答，不能重复提交")
        # 作废后补交：作为新的确认记录追加，保留历史。
        sequence = self._tick()
        record = ResponseRecord(
            sequence=sequence,
            item_id=item_id,
            answer=answer,
            correct=correct,
        )
        self.responses.append(record)
        self.audit.append(
            AuditEntry(
                sequence,
                "answer",
                {"item_id": item_id, "correct": correct},
            )
        )
        return record

    def attach_contribution(self, contribution: Contribution) -> None:
        self.contributions.append(contribution)

    def invalidate_response(
        self, item_id: str, teacher_id: str, reason: str
    ) -> ResponseRecord:
        if not teacher_id or not reason:
            raise SessionError("作废必须提供教师标识与证据说明")
        record = self.find_response(item_id)
        if record is None:
            raise SessionError(f"题目 {item_id} 没有可作废的作答")
        if record.status == INVALIDATED:
            raise SessionError(f"题目 {item_id} 的作答已被作废")
        index = self.responses.index(record)
        invalidated = ResponseRecord(
            sequence=record.sequence,
            item_id=record.item_id,
            answer=record.answer,
            correct=record.correct,
            status=INVALIDATED,
            invalidated_by=teacher_id,
            invalidation_reason=reason,
        )
        self.responses[index] = invalidated
        self.audit.append(
            AuditEntry(
                self._tick(),
                "invalidate",
                {
                    "item_id": item_id,
                    "teacher_id": teacher_id,
                    "reason": reason,
                },
            )
        )
        return invalidated

    def finish(self, result: FinalResult) -> FinalResult:
        self.require_in_progress()
        self.status = FINISHED
        self.lineage.append(result)
        self.audit.append(
            AuditEntry(
                self._tick(),
                "finish",
                {"result_index": result.result_index, "level": result.level},
            )
        )
        return result

    def regrade(self, result: FinalResult) -> FinalResult:
        """受控重评：会话必须已结束，结果绑定的规则版本保持不变。"""
        if self.status != FINISHED:
            raise SessionError("只有已结束的会话可以受控重评")
        if result.version_refs != self.version_refs:
            raise SessionError("重评必须使用会话冻结的规则版本，禁止夹带新题库修订")
        self.lineage.append(result)
        self.audit.append(
            AuditEntry(
                self._tick(),
                "regrade",
                {
                    "result_index": result.result_index,
                    "teacher_id": result.teacher_id,
                    "reason": result.reason,
                    "level": result.level,
                    "score": result.score,
                },
            )
        )
        return result

    # ---- 序列化 ----

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Session":
        selections = [
            SelectionExplanation(
                **{
                    **{k: v for k, v in s.items() if k != "candidates"},
                    "candidates": tuple(CandidateView(**c) for c in s["candidates"]),
                }
            )
            for s in data["selections"]
        ]
        responses = [ResponseRecord(**r) for r in data["responses"]]
        contributions = [Contribution(**c) for c in data["contributions"]]
        lineage = []
        for result in data["lineage"]:
            row = dict(result)
            row["gap_chains"] = {
                k: tuple(v) for k, v in result["gap_chains"].items()
            }
            lineage.append(FinalResult(**row))
        audit = [AuditEntry(**a) for a in data["audit"]]
        return cls(
            session_id=data["session_id"],
            learner_id=data.get("learner_id"),
            version_refs=data["version_refs"],
            status=data["status"],
            responses=responses,
            selections=selections,
            contributions=contributions,
            lineage=lineage,
            audit=audit,
            answers_by_token=dict(data.get("answers_by_token", {})),
            clock=data["clock"],
        )
