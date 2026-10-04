"""能力估计与定级引擎。

采用对教师可解释的简化 1PL（Rasch 型）模型：

* 每题作答在 (难度 b, 正确/错误) 上对技能能力 θ 做一次贝叶斯式更新；
* 更新规则为确定性的均值/方差递推，不依赖随机数，可逐题复算；
* 技能 θ 取该技能全部已确认证据的后验；先验来自规则集；
* 技能缺口除“高难度连续答错”的直接证据外，还沿先修关系向下传播：
  先修技能存在缺口时，后继技能的掌握判断不予确认。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .models import (
    Difficulty,
    Evidence,
    ScoringRuleSet,
    SessionResult,
    SkillAssessment,
    SkillGraph,
)

# 单条证据对方差的折减（信息量），越大说明一道题能消除越多不确定性。
_PARTIAL_K = 0.45
# 一次作答把均值向该题难度方向拉动的学习率。
_STEP = 0.55
# 判为“有把握”证据的 |θ-b| 带宽：难度显著高于当前估计仍答对/答错。
_CONFIDENT_BAND = 0.75


@dataclass
class _Belief:
    theta: float
    sigma: float
    successes: int
    failures: int
    confident_successes: int
    confident_failures: int


def _prior(rules: ScoringRuleSet) -> _Belief:
    return _Belief(
        theta=rules.theta_prior,
        sigma=float(Difficulty.VERY_HARD.value - Difficulty.VERY_EASY.value) / 2,
        successes=0,
        failures=0,
        confident_successes=0,
        confident_failures=0,
    )


def update_belief(belief: _Belief, b: float, correct: bool) -> _Belief:
    """根据一次作答更新技能后验。纯函数，便于测试与复算。"""
    expected_correct = 1.0 / (1.0 + math.exp(b - belief.theta))
    surprise = (1.0 if correct else 0.0) - expected_correct
    new_theta = belief.theta + _STEP * surprise
    new_theta = min(5.0, max(1.0, new_theta))
    new_sigma = math.sqrt(max(0.04, belief.sigma**2 - _PARTIAL_K))
    if correct:
        confident = b >= belief.theta - _CONFIDENT_BAND
        return _Belief(
            new_theta, new_sigma,
            belief.successes + 1, belief.failures,
            belief.confident_successes + (1 if confident else 0),
            belief.confident_failures,
        )
    confident = b <= belief.theta + _CONFIDENT_BAND
    return _Belief(
        new_theta, new_sigma,
        belief.successes, belief.failures + 1,
        belief.confident_successes,
        belief.confident_failures + (1 if confident else 0),
    )


def fold_evidence(
    evidence: list[Evidence], rules: ScoringRuleSet
) -> dict[str, _Belief]:
    """按技能折叠全部已确认证据，得到各技能当前后验信念。"""
    beliefs: dict[str, _Belief] = {}
    for ev in evidence:
        belief = beliefs.setdefault(ev.skill_id, _prior(rules))
        beliefs[ev.skill_id] = update_belief(belief, ev.difficulty.value,
                                             bool(ev.is_correct))
    return beliefs


def assess_skills(
    graph: SkillGraph,
    evidence: list[Evidence],
    rules: ScoringRuleSet,
) -> tuple[SkillAssessment, ...]:
    """按技能聚合证据并沿先修边传播缺口。

    传播规则：若先修技能被判定为缺口，则所有依赖它的技能即使答对较多，
    也只能标记为“未确认掌握”，并在 ``gap_propagated_from`` 中记录来源，
    方便教师向学员解释“为什么这部分暂时不给通过”。
    """
    beliefs = fold_evidence(evidence, rules)

    direct_gap: set[str] = set()
    direct_mastery: set[str] = set()
    for skill_id, belief in beliefs.items():
        if belief.confident_failures >= rules.confident_failures:
            direct_gap.add(skill_id)
        elif belief.confident_successes >= rules.confident_successes:
            direct_mastery.add(skill_id)

    # 缺口沿“先修 -> 后继”边传播（拓扑序保证先修先判定）。
    propagated: dict[str, tuple[str, ...]] = {}
    for skill_id in graph.skill_ids:
        node = graph.node(skill_id)
        sources: list[str] = []
        for prereq in node.prerequisites:
            if prereq in direct_gap or prereq in propagated:
                sources.append(prereq)
        if sources and skill_id not in direct_gap:
            propagated[skill_id] = tuple(sources)

    assessments: list[SkillAssessment] = []
    for skill_id in graph.skill_ids:
        belief = beliefs.get(skill_id)
        if belief is None:
            # 未测技能不做断言：既不算掌握也不算缺口。
            continue
        gap = skill_id in direct_gap or skill_id in propagated
        mastered = skill_id in direct_mastery and skill_id not in propagated
        assessments.append(SkillAssessment(
            skill_id=skill_id,
            theta=round(belief.theta, 4),
            sigma=round(belief.sigma, 4),
            mastered=mastered,
            gap=gap,
            support=belief.successes + belief.failures,
            successes=belief.successes,
            failures=belief.failures,
            gap_propagated_from=propagated.get(skill_id, ()),
        ))
    return tuple(assessments)


def score_session(
    graph: SkillGraph,
    evidence: list[Evidence],
    rules: ScoringRuleSet,
    finished_at: float,
    bank_version_key: str,
    regrade_of: str | None = None,
) -> SessionResult:
    """根据全部已确认证据生成不可变结果快照。"""
    confirmed = [e for e in evidence if e.status.name == "CONFIRMED"]
    assessments = assess_skills(graph, confirmed, rules)

    if assessments:
        # 以证据量加权汇总总体 θ；未测技能不计入。
        weight = lambda a: max(a.support, 1)  # noqa: E731
        total_w = sum(weight(a) for a in assessments)
        overall_theta = sum(a.theta * weight(a) for a in assessments) / total_w
        overall_sigma = math.sqrt(
            sum(a.sigma**2 * weight(a) for a in assessments) / total_w
        )
    else:
        overall_theta = rules.theta_prior
        overall_sigma = (Difficulty.VERY_HARD.value
                         - Difficulty.VERY_EASY.value) / 2

    return SessionResult(
        overall_theta=round(overall_theta, 4),
        overall_sigma=round(overall_sigma, 4),
        level=rules.level_for(overall_theta),
        rule_set_key=rules.key,
        graph_version_key=graph.version.key,
        bank_version_key=bank_version_key,
        finished_at=finished_at,
        item_count=len(confirmed),
        skill_assessments=assessments,
        regrade_of=regrade_of,
        level_basis=rules.explain_level(overall_theta),
    )


def explain_contribution(
    belief_before: _Belief | None,
    b: float,
    correct: bool,
    rules: ScoringRuleSet,
) -> str:
    """生成单题对能力判断贡献的中文说明。"""
    before = belief_before if belief_before is not None else _prior(rules)
    after = update_belief(before, b, correct)
    direction = "上调" if after.theta >= before.theta else "下调"
    verdict = "回答正确" if correct else "回答错误"
    return (
        f"{verdict}（难度 {b:.0f}），该技能能力估计由 {before.theta:.2f} "
        f"{direction}至 {after.theta:.2f}，不确定性降至 {after.sigma:.2f}"
    )
