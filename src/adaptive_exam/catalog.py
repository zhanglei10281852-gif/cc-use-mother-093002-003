"""版本目录：登记并解析图谱、题库、规则的各不可变修订。

目录只追加、不改写：题库修订不会覆盖旧修订，会话保存的是限定版本标识，
因此“后来的题库修订不得改写旧结果”由结构本身保证。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .bank import ItemBank, build_bank
from .contracts import VersionInfo
from .graph import SkillGraph, build_graph
from .rules import GradingRules, LevelBand, default_rules
from .versioning import qvid


class CatalogError(KeyError):
    """版本缺失或跨版本引用不合法。"""


@dataclass
class VersionCatalog:
    graphs: dict[str, SkillGraph] = field(default_factory=dict)
    banks: dict[str, ItemBank] = field(default_factory=dict)
    rules: dict[str, GradingRules] = field(default_factory=dict)
    # 题库限定版本 -> 配套图谱限定版本
    bank_graph: dict[str, str] = field(default_factory=dict)

    # ---- 登记 ----

    def register_graph(self, graph: SkillGraph) -> None:
        key = qvid(graph.version)
        if key in self.graphs:
            raise ValueError(f"图谱版本已存在: {key}")
        self.graphs[key] = graph

    def register_bank(self, bank: ItemBank, graph_qvid: str) -> None:
        """题库必须声明它配套的图谱版本；题目只能覆盖该图谱中的技能。"""
        graph = self.require_graph(graph_qvid)
        unknown = {
            skill_id
            for item in bank.items.values()
            for skill_id in item.skill_ids
        } - set(graph.skills)
        if unknown:
            raise ValueError(f"题库覆盖了图谱中不存在的技能: {sorted(unknown)}")
        key = qvid(bank.version)
        if key in self.banks:
            raise ValueError(f"题库版本已存在: {key}")
        self.banks[key] = bank
        self.bank_graph[key] = graph_qvid

    def register_rules(self, rules: GradingRules) -> None:
        key = qvid(rules.version)
        if key in self.rules:
            raise ValueError(f"规则版本已存在: {key}")
        self.rules[key] = rules

    # ---- 解析 ----

    def require_graph(self, key: str) -> SkillGraph:
        try:
            return self.graphs[key]
        except KeyError:
            raise CatalogError(f"未知图谱版本: {key}") from None

    def require_bank(self, key: str) -> ItemBank:
        try:
            return self.banks[key]
        except KeyError:
            raise CatalogError(f"未知题库版本: {key}") from None

    def require_rules(self, key: str) -> GradingRules:
        try:
            return self.rules[key]
        except KeyError:
            raise CatalogError(f"未知规则版本: {key}") from None

    def graph_for_bank(self, bank_qvid: str) -> SkillGraph:
        try:
            return self.require_graph(self.bank_graph[bank_qvid])
        except KeyError:
            raise CatalogError(f"题库未登记配套图谱: {bank_qvid}") from None

    def bundle(
        self, graph_qvid: str, bank_qvid: str, rules_qvid: str
    ) -> "VersionBundle":
        return VersionBundle(
            graph=self.require_graph(graph_qvid),
            bank=self.require_bank(bank_qvid),
            rules=self.require_rules(rules_qvid),
        )


@dataclass(frozen=True)
class VersionBundle:
    """一次会话冻结使用的三件套版本。"""

    graph: SkillGraph
    bank: ItemBank
    rules: GradingRules

    def as_reference(self) -> dict[str, str]:
        return {
            "graph_qvid": qvid(self.graph.version),
            "bank_qvid": qvid(self.bank.version),
            "rules_qvid": qvid(self.rules.version),
            "graph_hash": self.graph.content_hash,
            "bank_hash": self.bank.content_hash,
            "rules_hash": self.rules.content_hash,
        }


# ---- JSON 装载（供运维把版本化定义文件导入目录） ----

def _version(spec: dict) -> VersionInfo:
    return VersionInfo(spec["entity_id"], spec["display_name"], spec["revision"])


def load_catalog(path: str | Path) -> VersionCatalog:
    """从 JSON 定义文件装载目录。结构见 examples/catalog_seed.json。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    catalog = VersionCatalog()
    for spec in data.get("graphs", []):
        catalog.register_graph(
            build_graph(
                _version(spec),
                [
                    (
                        s["skill_id"],
                        s["display_name"],
                        tuple(s.get("prerequisites", [])),
                    )
                    for s in spec["skills"]
                ],
            )
        )
    for spec in data.get("banks", []):
        catalog.register_bank(
            build_bank(
                _version(spec),
                [
                    (
                        item["item_id"],
                        item["prompt"],
                        item["answer"],
                        tuple(item["skill_ids"]),
                        float(item["difficulty"]),
                        item.get("exposure_cap"),
                        int(item.get("tie_rank", 0)),
                    )
                    for item in spec["items"]
                ],
            ),
            spec["graph_qvid"],
        )
    for spec in data.get("rules", []):
        version = _version(spec)
        if "level_bands" in spec:
            rules = GradingRules(
                version=version,
                level_bands=tuple(
                    LevelBand(b["level"], float(b["min_score"]))
                    for b in spec["level_bands"]
                ),
                gap_threshold=float(spec.get("gap_threshold", 0.5)),
                evidence_k=float(spec.get("evidence_k", 0.8)),
                min_items=int(spec.get("min_items", 5)),
                max_items=int(spec.get("max_items", 20)),
                target_uncertainty=float(spec.get("target_uncertainty", 0.25)),
                initial_ability=float(spec.get("initial_ability", 0.5)),
            )
        else:
            rules = default_rules(version)
        catalog.register_rules(rules)
    return catalog
