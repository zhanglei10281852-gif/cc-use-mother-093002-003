"""能力状态估计与下一题选择（纯函数核心）。

状态完全由“已确认作答”派生：进程重启后只要重放确认作答即可重建，
不依赖内存缓存，因此未结束的会话可以在重启后继续。

估计模型（Beta 共轭的简化形式）：
- 每个技能维护证据量 ``alpha``（答对累计）与 ``beta``（答错累计），
  初值来自规则的先验能力 ``initial_ability``，先验强度固定为 2。
- 掌握度 ``p = alpha / (alpha + beta)``；
- 方差 ``var = p(1-p)/(alpha+beta)``，不确定度取全部技能方差均值的平方根。

选题评分（越大越优先）：
``info + 0.30*fit + 0.15*novelty``
- info：候选题覆盖技能的当前方差之和（期望信息量）；
- fit：``1 - |题目难度 - 总体能力|``；
- novelty：覆盖技能中尚未取得作答的比例。
排序键为 ``(-score, tie_rank, item_id)``：同参数候选项构成并列，
由题目的 tie_rank、再由 item_id 稳定打破，跨进程结果一致。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .bank import Item, ItemBank
from .graph import SkillGraph
from .rules import GradingRules

PRIOR_STRENGTH = 2.0
FIT_WEIGHT = 0.30
NOVELTY_WEIGHT = 0.15


@dataclass(frozen=True)
class ConfirmedResponse:
    """重放用的最小作答记录（作废作答不参与）。"""

    item_id: str
    correct: bool


@dataclass(frozen=True)
class SkillState:
    skill_id: str
    alpha: float
    beta: float

    @property
    def mastery(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def variance(self) -> float:
        total = self.alpha + self.beta
        return self.mastery * (1.0 - self.mastery) / total


@dataclass(frozen=True)
class AbilitySnapshot:
    skills: dict[str, SkillState]
    answer_counts: dict[str, int]

    @property
    def overall_ability(self) -> float:
        if not self.skills:
            return 0.0
        return sum(s.mastery for s in self.skills.values()) / len(self.skills)

    @property
    def uncertainty(self) -> float:
        if not self.skills:
            return 0.0
        return math.sqrt(
            sum(s.variance for s in self.skills.values()) / len(self.skills)
        )

    def gap_skills(self, threshold: float) -> frozenset[str]:
        return frozenset(
            skill_id
            for skill_id, state in self.skills.items()
            if state.mastery < threshold
        )


def derive_ability(
    graph: SkillGraph,
    bank: ItemBank,
    rules: GradingRules,
    responses: tuple[ConfirmedResponse, ...],
) -> AbilitySnapshot:
    alpha = {
        skill_id: PRIOR_STRENGTH * rules.initial_ability
        for skill_id in graph.skills
    }
    beta = {
        skill_id: PRIOR_STRENGTH * (1.0 - rules.initial_ability)
        for skill_id in graph.skills
    }
    counts = {skill_id: 0 for skill_id in graph.skills}
    for response in responses:
        item = bank.items[response.item_id]
        for skill_id in item.skill_ids:
            if response.correct:
                alpha[skill_id] += rules.evidence_k
            else:
                beta[skill_id] += rules.evidence_k
            counts[skill_id] += 1
    return AbilitySnapshot(
        skills={
            skill_id: SkillState(skill_id, alpha[skill_id], beta[skill_id])
            for skill_id in sorted(graph.skills)
        },
        answer_counts=dict(sorted(counts.items())),
    )


@dataclass(frozen=True)
class ScoredItem:
    item: Item
    info: float
    fit: float
    novelty: float
    score: float


@dataclass(frozen=True)
class SelectionAdvice:
    chosen: ScoredItem | None
    candidates: tuple[ScoredItem, ...]
    ability: float
    uncertainty: float
    blocked_by_exposure: tuple[str, ...]
    already_presented: tuple[str, ...]
    stop_reason: str | None


def score_item(
    item: Item, snapshot: AbilitySnapshot, ability: float
) -> ScoredItem:
    info = sum(snapshot.skills[s].variance for s in item.skill_ids)
    fit = 1.0 - abs(item.difficulty - ability)
    novel = sum(
        1 for s in item.skill_ids if snapshot.answer_counts.get(s, 0) == 0
    ) / len(item.skill_ids)
    score = info + FIT_WEIGHT * fit + NOVELTY_WEIGHT * novel
    return ScoredItem(item=item, info=info, fit=fit, novelty=novel, score=score)


def should_stop(
    rules: GradingRules,
    response_count: int,
    snapshot: AbilitySnapshot,
    eligible_count: int,
) -> str | None:
    """返回停测原因；None 表示继续。"""
    if response_count >= rules.max_items:
        return "已达到最大题量"
    if eligible_count == 0:
        return "没有可投放的候选题"
    if response_count >= rules.min_items and snapshot.uncertainty <= rules.target_uncertainty:
        return "总体不确定度已低于目标值"
    return None


def advise_next(
    graph: SkillGraph,
    bank: ItemBank,
    rules: GradingRules,
    responses: tuple[ConfirmedResponse, ...],
    presented_items: frozenset[str],
    exposure_remaining: dict[str, int],
) -> SelectionAdvice:
    """计算下一题及完整的并列候选评分（供教师解释）。

    ``exposure_remaining``：题目ID -> 剩余曝光次数；
    值为 0 表示已用尽，缺省表示不限。
    """
    snapshot = derive_ability(graph, bank, rules, responses)
    ability = snapshot.overall_ability
    blocked: list[str] = []
    candidates: list[ScoredItem] = []
    for item in bank.items.values():
        if item.item_id in presented_items:
            continue
        remaining = exposure_remaining.get(item.item_id)
        if remaining is not None and remaining <= 0:
            blocked.append(item.item_id)
            continue
        candidates.append(score_item(item, snapshot, ability))
    candidates.sort(
        key=lambda scored: (
            -scored.score,
            scored.item.tie_rank,
            scored.item.item_id,
        )
    )
    stop_reason = should_stop(rules, len(responses), snapshot, len(candidates))
    chosen = candidates[0] if candidates and stop_reason is None else None
    return SelectionAdvice(
        chosen=chosen,
        candidates=tuple(candidates),
        ability=ability,
        uncertainty=snapshot.uncertainty,
        blocked_by_exposure=tuple(sorted(blocked)),
        already_presented=tuple(sorted(presented_items)),
        stop_reason=stop_reason,
    )
