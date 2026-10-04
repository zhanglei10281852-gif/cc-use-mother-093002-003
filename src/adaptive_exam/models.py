"""自适应中文测评领域模型。

所有领域对象均为不可变值对象（frozen dataclass），聚合根
``Session`` 仅通过模块内的工厂函数与 ``services`` 层发生状态迁移。
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Sequence


# ---------------------------------------------------------------------------
# 版本化能力图谱
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillGraphVersion:
    """能力图谱的一个不可修订版本。

    同一 ``entity_id`` 下 ``revision`` 单调递增；旧版本必须永久保留，
    因为已结束的会话要按当时版本复算。
    """

    entity_id: str
    display_name: str
    revision: int

    def __post_init__(self) -> None:
        if not self.entity_id or not isinstance(self.entity_id, str):
            raise ValueError("图谱实体标识不能为空")
        if not self.display_name:
            raise ValueError("图谱显示名不能为空")
        if not isinstance(self.revision, int) or self.revision < 1:
            raise ValueError("图谱修订号必须为正整数")

    @property
    def key(self) -> str:
        return f"{self.entity_id}:{self.revision}"


@dataclass(frozen=True)
class SkillNode:
    """能力图谱中的一个技能节点（如 HSK1-听力-数字）。"""

    skill_id: str
    name: str
    prerequisites: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.skill_id:
            raise ValueError("技能标识不能为空")
        if not self.name:
            raise ValueError("技能名称不能为空")
        if any(not p for p in self.prerequisites):
            raise ValueError("先修技能标识不能为空串")
        if self.skill_id in self.prerequisites:
            raise ValueError(f"技能 {self.skill_id} 不能以自身为先修")


class SkillGraph:
    """某一版本的完整能力图谱：节点 + 有向无环先修关系。"""

    def __init__(self, version: SkillGraphVersion, nodes: Sequence[SkillNode]):
        nodes_by_id: dict[str, SkillNode] = {}
        order: list[str] = []
        for node in nodes:
            if node.skill_id in nodes_by_id:
                raise ValueError(f"技能 {node.skill_id} 在图谱中重复定义")
            nodes_by_id[node.skill_id] = node
            order.append(node.skill_id)
        for node in nodes:
            for prereq in node.prerequisites:
                if prereq not in nodes_by_id:
                    raise ValueError(
                        f"技能 {node.skill_id} 引用了不存在的先修技能 {prereq}"
                    )
        self._assert_acyclic(nodes_by_id)
        self.version = version
        self._nodes = nodes_by_id
        self._order = tuple(order)

    @staticmethod
    def _assert_acyclic(nodes: Mapping[str, SkillNode]) -> None:
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {sid: WHITE for sid in nodes}

        def visit(sid: str, stack: tuple[str, ...]) -> None:
            color[sid] = GRAY
            for prereq in nodes[sid].prerequisites:
                if color[prereq] == GRAY:
                    cycle = " -> ".join(stack + (prereq,))
                    raise ValueError(f"先修关系存在环：{cycle}")
                if color[prereq] == WHITE:
                    visit(prereq, stack + (prereq,))
            color[sid] = BLACK

        for sid in nodes:
            if color[sid] == WHITE:
                visit(sid, (sid,))

    @property
    def skill_ids(self) -> tuple[str, ...]:
        return self._order

    def node(self, skill_id: str) -> SkillNode:
        try:
            return self._nodes[skill_id]
        except KeyError as exc:
            raise KeyError(f"技能 {skill_id} 不存在于图谱 {self.version.key}") from exc

    def contains(self, skill_id: str) -> bool:
        return skill_id in self._nodes

    def descendants(self, skill_id: str) -> frozenset[str]:
        """返回直接或间接依赖 ``skill_id`` 的全部技能（缺口传播用）。"""
        result: set[str] = set()
        stack = [skill_id]
        while stack:
            current = stack.pop()
            for sid, node in self._nodes.items():
                if current in node.prerequisites and sid not in result:
                    result.add(sid)
                    stack.append(sid)
        return frozenset(result)

    def closure(self, skill_ids: Sequence[str]) -> frozenset[str]:
        """返回给定技能集合及其全部先修祖先。"""
        result: set[str] = set()
        stack = list(skill_ids)
        while stack:
            current = stack.pop()
            if current in result:
                continue
            result.add(current)
            stack.extend(self._nodes[current].prerequisites)
        return frozenset(result)


# ---------------------------------------------------------------------------
# 题库
# ---------------------------------------------------------------------------


class Difficulty(Enum):
    """题目难度档，值即 IRT 风格难度参数 b。"""

    VERY_EASY = 1
    EASY = 2
    MEDIUM = 3
    HARD = 4
    VERY_HARD = 5


@dataclass(frozen=True)
class ItemBankVersion:
    entity_id: str
    display_name: str
    revision: int

    def __post_init__(self) -> None:
        if not self.entity_id or not self.display_name or self.revision < 1:
            raise ValueError("题库版本信息不合法")

    @property
    def key(self) -> str:
        return f"{self.entity_id}:{self.revision}"


@dataclass(frozen=True)
class Item:
    """一道不可变诊断题。

    ``answer`` 为客观题正确答案（字符串化），评分仅比较字符串，
    因此题目一旦发布答案不允许就地修改，只能在新题库修订中更正。
    """

    item_id: str
    skill_id: str
    difficulty: Difficulty
    prompt: str
    answer: str
    max_exposure: int
    active: bool = True

    def __post_init__(self) -> None:
        if not self.item_id:
            raise ValueError("题目标识不能为空")
        if not self.skill_id:
            raise ValueError("题目必须绑定技能")
        if not isinstance(self.difficulty, Difficulty):
            raise ValueError("题目难度非法")
        if not self.prompt:
            raise ValueError("题面不能为空")
        if not self.answer:
            raise ValueError("题目答案不能为空")
        if not isinstance(self.max_exposure, int) or self.max_exposure < 1:
            raise ValueError("曝光上限必须为正整数")

    @property
    def b(self) -> float:
        return float(self.difficulty.value)


class ItemBank:
    """某一修订版题库；题目一经入库不可变。"""

    def __init__(self, version: ItemBankVersion, items: Sequence[Item],
                 graph_version_key: str):
        seen: set[str] = set()
        for item in items:
            if item.item_id in seen:
                raise ValueError(f"题目 {item.item_id} 在题库中重复定义")
            seen.add(item.item_id)
        self.version = version
        self.graph_version_key = graph_version_key
        self._items: dict[str, Item] = {it.item_id: it for it in items}

    def get(self, item_id: str) -> Item:
        try:
            return self._items[item_id]
        except KeyError as exc:
            raise KeyError(f"题目 {item_id} 不存在于题库 {self.version.key}") from exc

    def contains(self, item_id: str) -> bool:
        return item_id in self._items

    @property
    def items(self) -> tuple[Item, ...]:
        return tuple(self._items.values())


# ---------------------------------------------------------------------------
# 评分规则
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoringRuleSet:
    """一次测评所采用的评分与定级规则（随会话固化）。

    ``levels`` 为从低到高的等级名，相邻阈值为该等级的下界（含）；
    达到 ``confident_successes`` 次有把握的正确即可确认掌握，
    出现 ``confident_failures`` 次高难度错误即确认缺口。
    """

    rule_set_id: str
    revision: int
    levels: tuple[str, ...]
    thresholds: tuple[float, ...]
    confident_successes: int = 2
    confident_failures: int = 2
    theta_prior: float = 3.0
    convergence_std: float = 0.6
    max_items: int = 30
    min_items: int = 5

    def __post_init__(self) -> None:
        if not self.rule_set_id or self.revision < 1:
            raise ValueError("规则集标识或修订号非法")
        if len(self.levels) < 2:
            raise ValueError("等级表至少包含两个等级")
        if len(self.levels) != len(self.thresholds):
            raise ValueError("等级数与阈值数不一致")
        if list(self.thresholds) != sorted(self.thresholds):
            raise ValueError("等级阈值必须升序")
        if self.confident_successes < 1 or self.confident_failures < 1:
            raise ValueError("掌握/缺口确认次数必须为正")
        if not 1.0 <= self.theta_prior <= 5.0:
            raise ValueError("先验能力应落在难度量纲 1..5 内")
        if self.convergence_std <= 0:
            raise ValueError("收敛标准差必须为正")
        if self.min_items < 1 or self.max_items < self.min_items:
            raise ValueError("题量上下界非法")

    @property
    def key(self) -> str:
        return f"{self.rule_set_id}:{self.revision}"

    def level_for(self, theta: float) -> str:
        assigned = self.levels[0]
        for level, cutoff in zip(self.levels, self.thresholds):
            if theta >= cutoff:
                assigned = level
        return assigned

    def explain_level(self, theta: float) -> str:
        """生成“为何定为此等级”的中文依据。"""
        level = self.level_for(theta)
        idx = self.levels.index(level)
        lower = self.thresholds[idx]
        if idx + 1 < len(self.levels):
            upper = self.thresholds[idx + 1]
            band = f"[{lower:.1f}, {upper:.1f})"
        else:
            band = f"[{lower:.1f}, 5.0]"
        return (
            f"按规则集 {self.key}，总体能力 θ={theta:.2f} 落在等级"
            f"「{level}」的阈值区间 {band}（阈值在会话开始时已固化，"
            f"事后修订规则不改变本结果）"
        )


# ---------------------------------------------------------------------------
# 作答证据
# ---------------------------------------------------------------------------


class EvidenceStatus(Enum):
    CONFIRMED = "confirmed"   # 已判分且纳入能力判断
    PENDING = "pending"       # 已呈现，尚未收到作答
    VOIDED = "voided"         # 教师按证据作废，不参与评分


@dataclass(frozen=True)
class Evidence:
    """一道已呈现题目的完整证据链。"""

    item_id: str
    skill_id: str
    difficulty: Difficulty
    status: EvidenceStatus
    presented_at: float
    request_id: str
    selection_reason: str
    token: str
    answer_request_id: str | None = None
    answer_given: str | None = None
    is_correct: bool | None = None
    answered_at: float | None = None
    void_reason: str | None = None
    voided_by: str | None = None
    voided_at: float | None = None
    contribution: str | None = None  # 评分时写入的判分说明

    def scored(self) -> bool:
        return self.status is EvidenceStatus.CONFIRMED


# ---------------------------------------------------------------------------
# 测评会话
# ---------------------------------------------------------------------------


class SessionState(Enum):
    IN_PROGRESS = "in_progress"
    FINISHED = "finished"


@dataclass
class Session:
    """测评会话聚合根。

    会话在创建时绑定能力图谱、题库与规则集的具体修订号；结束后产生
    的结果快照不再随后续题库修订而变化。
    """

    session_id: str
    learner_id: str
    graph_version_key: str
    bank_version_key: str
    rule_set_key: str
    created_at: float
    state: SessionState = SessionState.IN_PROGRESS
    evidence: list[Evidence] = field(default_factory=list)
    result: "SessionResult | None" = None
    result_history: list["SessionResult"] = field(default_factory=list)
    regrade_count: int = 0
    audit_log: list[str] = field(default_factory=list)

    # ---- 派生查询 -------------------------------------------------------

    def by_item(self, item_id: str) -> Evidence | None:
        for ev in self.evidence:
            if ev.item_id == item_id:
                return ev
        return None

    def pending(self) -> Evidence | None:
        for ev in self.evidence:
            if ev.status is EvidenceStatus.PENDING:
                return ev
        return None

    def confirmed(self) -> list[Evidence]:
        return [e for e in self.evidence if e.status is EvidenceStatus.CONFIRMED]

    def replace_evidence(self, item_id: str, new: Evidence) -> None:
        for idx, ev in enumerate(self.evidence):
            if ev.item_id == item_id:
                self.evidence[idx] = new
                return
        raise KeyError(item_id)

    def append_evidence(self, ev: Evidence) -> None:
        self.evidence.append(ev)


@dataclass(frozen=True)
class SkillAssessment:
    skill_id: str
    theta: float
    sigma: float
    mastered: bool
    gap: bool
    support: int
    successes: int
    failures: int
    gap_propagated_from: tuple[str, ...] = ()


@dataclass(frozen=True)
class SessionResult:
    """会话结束时的不可变结果快照。"""

    overall_theta: float
    overall_sigma: float
    level: str
    rule_set_key: str
    graph_version_key: str
    bank_version_key: str
    finished_at: float
    item_count: int
    skill_assessments: tuple[SkillAssessment, ...]
    regrade_of: str | None = None  # 受控重评时记录原结果生成时间
    level_basis: str | None = None  # “为何定为此等级”的阈值依据

    def to_sketch(self) -> dict:
        """供持久化/展示的精简字典视图。"""
        return {
            "overall_theta": round(self.overall_theta, 4),
            "overall_sigma": round(self.overall_sigma, 4),
            "level": self.level,
            "rule_set_key": self.rule_set_key,
            "graph_version_key": self.graph_version_key,
            "bank_version_key": self.bank_version_key,
            "item_count": self.item_count,
            "finished_at": self.finished_at,
        }
