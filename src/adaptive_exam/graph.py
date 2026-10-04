"""版本化能力图谱：技能节点、先修关系与缺口传播。

先修关系的语义：掌握后继技能必须先掌握先修技能。
因此当某技能被判定为“缺口”时，其先修链上的技能也应被标记为
推断缺口（inferred），供教师解释学员为什么在某个高阶技能上失分。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from .contracts import VersionInfo
from .versioning import content_digest, qvid


@dataclass(frozen=True)
class Skill:
    skill_id: str
    display_name: str
    # 直接先修技能的 ID（有序；顺序仅用于稳定的解释输出）。
    prerequisites: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillGraph:
    version: VersionInfo
    skills: Mapping[str, Skill]
    description: str = ""

    def __post_init__(self) -> None:
        prereq_ids: set[str] = set()
        for skill in self.skills.values():
            if skill.skill_id not in self.skills:
                raise ValueError(f"技能映射的键与技能ID不一致: {skill.skill_id}")
            prereq_ids.update(skill.prerequisites)
        missing = prereq_ids - set(self.skills)
        if missing:
            raise ValueError(f"先修关系指向不存在的技能: {sorted(missing)}")
        self._assert_acyclic()

    def _assert_acyclic(self) -> None:
        visiting: set[str] = set()
        done: set[str] = set()

        def visit(node: str) -> None:
            if node in done:
                return
            if node in visiting:
                raise ValueError(f"先修关系存在环，涉及技能: {node}")
            visiting.add(node)
            for prereq in self.skills[node].prerequisites:
                visit(prereq)
            visiting.discard(node)
            done.add(node)

        for skill_id in sorted(self.skills):
            visit(skill_id)

    @property
    def qualified_id(self) -> str:
        return qvid(self.version)

    @property
    def content_hash(self) -> str:
        payload = [
            {
                "skill_id": s.skill_id,
                "display_name": s.display_name,
                "prerequisites": list(s.prerequisites),
            }
            for s in self.skills.values()
        ]
        return content_digest(payload)

    # ---- 缺口传播 ----

    def propagate_gaps(self, direct_gaps: frozenset[str] | set[str]) -> dict[str, str]:
        """把直接缺口沿先修链向上游传播。

        返回 ``技能ID -> "direct" | "inferred"``。
        推断出的先修技能即使没有直接作答证据也计入缺口，
        并且保留从哪个直接缺口传播而来的解释链（见 :meth:`gap_chains`）。
        """
        direct = set(direct_gaps)
        unknown = direct - set(self.skills)
        if unknown:
            raise ValueError(f"缺口包含不存在的技能: {sorted(unknown)}")
        result = {skill_id: "direct" for skill_id in direct}
        stack = list(direct)
        while stack:
            current = stack.pop()
            for prereq in self.skills[current].prerequisites:
                if result.get(prereq) != "direct":
                    if prereq not in result:
                        stack.append(prereq)
                    result[prereq] = "inferred"
        return result

    def gap_chains(
        self, direct_gaps: frozenset[str] | set[str]
    ) -> dict[str, tuple[str, ...]]:
        """返回每个推断缺口到最近直接缺口的一条解释链（按先修边反向）。"""
        direct = set(direct_gaps)
        chains: dict[str, tuple[str, ...]] = {s: (s,) for s in direct}
        stack = list(direct)
        while stack:
            current = stack.pop()
            for prereq in self.skills[current].prerequisites:
                if prereq not in chains:
                    chains[prereq] = chains[current] + (prereq,)
                    stack.append(prereq)
        return chains

    def closure(self, skill_ids: frozenset[str] | set[str]) -> frozenset[str]:
        """返回技能集合与其全部先修（用于题目可投放性判断）。"""
        result = set(skill_ids)
        stack = list(skill_ids)
        while stack:
            current = stack.pop()
            for prereq in self.skills[current].prerequisites:
                if prereq not in result:
                    result.add(prereq)
                    stack.append(prereq)
        return frozenset(result)


def build_graph(
    version: VersionInfo, skill_defs: list[tuple[str, str, tuple[str, ...]]]
) -> SkillGraph:
    """按 ``(ID, 名称, 先修ID元组)`` 列表构建图谱；输入顺序即稳定顺序。"""
    skills: dict[str, Skill] = {}
    for skill_id, display_name, prereqs in skill_defs:
        if skill_id in skills:
            raise ValueError(f"技能ID重复: {skill_id}")
        skills[skill_id] = Skill(skill_id, display_name, tuple(prereqs))
    return SkillGraph(version=version, skills=dict(sorted(skills.items())))
