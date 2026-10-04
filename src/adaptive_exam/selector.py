"""自适应下一题选择引擎。

选择过程是纯函数：同样的图谱版本、题库版本、已确认证据与曝光台账，
永远产出同一道题。并列候选题一律按题目标识字典序裁决，保证自动化
场景（重试、回放、进程重启）下选择结果可稳定复现。

策略（对教师可解释）：

1. 覆盖优先 —— 已采样证据最少的技能先测，避免高水平学员反复见到同类题；
2. 先修约束 —— 某技能的先修（含传递先修）被确认为缺口时，本轮不再
   向其后继出题，后继掌握也无从确认；技能自身一旦确认缺口，同样停止
   出题（继续出更难的题只会让初学者连续受挫）；
3. 难度匹配 —— 在目标技能内挑选难度最接近该技能当前能力估计的题，
   等距时优先偏难半档，再按题目标识破并；
4. 曝光约束 —— 已在本会话呈现、已停用、全库曝光用尽的题一律排除。

已判定（掌握或缺口）的技能在覆盖轮转中排在未判定技能之后：先把
尚未有结论的技能测完，再在已掌握技能上用更难的题探测上限。
"""
from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import Mapping

from .models import Item, ItemBank, ScoringRuleSet, SkillGraph
from .scoring import fold_evidence

# 冷启动目标难度：初学者先从容易题入手，避免开场连续受挫。
COLD_START_TARGET = 2.0


@dataclasses.dataclass(frozen=True)
class Selection:
    item: Item
    reason: str
    target_skill: str
    target_theta: float


def _eligible(
    item: Item,
    presented: frozenset[str],
    exposures: Mapping[str, int],
) -> bool:
    if not item.active:
        return False
    if item.item_id in presented:
        return False
    if exposures.get(item.item_id, 0) >= item.max_exposure:
        return False
    return True


def select_next(
    graph: SkillGraph,
    bank: ItemBank,
    confirmed_evidence: Sequence,
    presented_item_ids: frozenset[str],
    exposures: Mapping[str, int],
    rules: ScoringRuleSet,
    confirmed_gaps: frozenset[str] = frozenset(),
    confirmed_mastery: frozenset[str] = frozenset(),
) -> Selection | None:
    """返回下一题；无合格候选时返回 ``None``。

    ``confirmed_evidence`` 仅含已确认证据（用于能力估计与覆盖计数）；
    ``presented_item_ids`` 含会话内全部已呈现题目（pending/voided 也
    不可重复呈现）；``confirmed_gaps`` 的技能本身及其全部后继被阻塞；
    ``confirmed_mastery`` 的技能排在未判定技能之后。
    """
    beliefs = fold_evidence(list(confirmed_evidence), rules)

    blocked: set[str] = set(confirmed_gaps)
    for gap in confirmed_gaps:
        blocked |= graph.descendants(gap)

    support_by_skill: dict[str, int] = {}
    for ev in confirmed_evidence:
        support_by_skill[ev.skill_id] = support_by_skill.get(ev.skill_id, 0) + 1

    by_skill: dict[str, list[Item]] = {}
    for item in bank.items:
        if item.skill_id in blocked or not graph.contains(item.skill_id):
            continue
        if _eligible(item, presented_item_ids, exposures):
            by_skill.setdefault(item.skill_id, []).append(item)

    if not by_skill:
        return None

    order_index = {sid: i for i, sid in enumerate(graph.skill_ids)}

    def skill_key(skill_id: str) -> tuple[int, int, int, str]:
        # 先测未判定技能（0），已掌握技能最后（1）；
        # 组内证据最少优先；同量按图谱拓扑序；再以标识破并。
        adjudicated = 1 if skill_id in confirmed_mastery else 0
        return (adjudicated, support_by_skill.get(skill_id, 0),
                order_index[skill_id], skill_id)

    target_skill = min(by_skill, key=skill_key)
    candidates = by_skill[target_skill]
    belief = beliefs.get(target_skill)
    target_theta = belief.theta if belief is not None else COLD_START_TARGET
    support = support_by_skill.get(target_skill, 0)

    def item_key(item: Item) -> tuple[float, int, float, str]:
        distance = abs(item.b - target_theta)
        # 等距时偏难者优先；再按难度、最后按题目标识稳定破并。
        harder_first = 0 if item.b >= target_theta else 1
        return (round(distance, 6), harder_first, item.b, item.item_id)

    chosen = min(candidates, key=item_key)

    if belief is None:
        reason = (
            f"技能「{target_skill}」尚无证据，按覆盖优先与图谱先修顺序首先"
            f"测量；冷启动选择最接近入门难度（目标 {COLD_START_TARGET:.0f}）"
            f"的题目 {chosen.item_id}（难度 {chosen.difficulty.value}）"
        )
    else:
        reason = (
            f"技能「{target_skill}」现有 {support} 条已确认证据，能力估计 "
            f"{target_theta:.2f}，在覆盖轮转中轮到该技能；选择难度最接近"
            f"估计的题目 {chosen.item_id}（难度 {chosen.difficulty.value}，"
            f"目标 {target_theta:.2f}）"
        )
    return Selection(
        item=chosen,
        reason=reason,
        target_skill=target_skill,
        target_theta=round(target_theta, 4),
    )


def should_stop(
    confirmed_count: int,
    rules: ScoringRuleSet,
    weighted_sigma: float,
    no_candidate: bool,
) -> tuple[bool, str]:
    """统一的终止判定，供正常测评与重放共用。"""
    if confirmed_count < rules.min_items:
        if no_candidate:
            return True, f"可出题数已耗尽，但未达最少题量 {rules.min_items}"
        return False, "继续"
    if no_candidate:
        return True, "已无符合覆盖与曝光约束的候选题"
    if confirmed_count >= rules.max_items:
        return True, f"已达规则上限 {rules.max_items} 题"
    if weighted_sigma <= rules.convergence_std:
        return True, f"总体不确定性 {weighted_sigma:.2f} 已收敛至阈值内"
    return False, "继续"
