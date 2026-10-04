"""版本化评分与自适应选题规则。

规则是不可变值对象：等级分段、技能缺口阈值、证据权重、
停测条件都来自某个具体修订。会话结束时把结果绑定到该修订，
此后即使发布新规则修订，旧结果也不会被改写。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .contracts import VersionInfo
from .versioning import content_digest, qvid


@dataclass(frozen=True)
class LevelBand:
    level: str
    min_score: float  # 取得该等级所需的最低总分

    def __post_init__(self) -> None:
        if not self.level:
            raise ValueError("等级名称不能为空")
        if not 0.0 <= self.min_score <= 1.0:
            raise ValueError(f"等级 {self.level} 的分数线超出 [0,1]")


@dataclass(frozen=True)
class GradingRules:
    version: VersionInfo
    level_bands: tuple[LevelBand, ...]
    gap_threshold: float = 0.5  # 技能掌握度低于此值记为缺口
    evidence_k: float = 0.8  # 单条作答的证据强度
    min_items: int = 5
    max_items: int = 20
    target_uncertainty: float = 0.25  # 总体不确定度低于此值可提前停测
    initial_ability: float = 0.5

    def __post_init__(self) -> None:
        if not self.level_bands:
            raise ValueError("至少需要一个等级分段")
        bands = sorted(self.level_bands, key=lambda b: b.min_score, reverse=True)
        if bands[-1].min_score > 0:
            raise ValueError("必须包含一个 0.0 分数线的兜底等级")
        if len({b.min_score for b in self.level_bands}) != len(self.level_bands):
            raise ValueError("等级分数线不能重复")
        if not 0.0 < self.gap_threshold <= 1.0:
            raise ValueError("缺口阈值必须在 (0,1]")
        if self.evidence_k <= 0:
            raise ValueError("证据强度必须为正")
        if not 1 <= self.min_items <= self.max_items:
            raise ValueError("题量上下界不合法")

    @property
    def qualified_id(self) -> str:
        return qvid(self.version)

    @property
    def content_hash(self) -> str:
        payload = {
            "level_bands": [
                {"level": b.level, "min_score": b.min_score}
                for b in sorted(self.level_bands, key=lambda b: b.min_score, reverse=True)
            ],
            "gap_threshold": self.gap_threshold,
            "evidence_k": self.evidence_k,
            "min_items": self.min_items,
            "max_items": self.max_items,
            "target_uncertainty": self.target_uncertainty,
            "initial_ability": self.initial_ability,
        }
        return content_digest(payload)

    def level_for(self, score: float) -> str:
        ordered = sorted(self.level_bands, key=lambda b: b.min_score, reverse=True)
        for band in ordered:
            if score + 1e-12 >= band.min_score:
                return band.level
        return ordered[-1].level


def default_rules(version: VersionInfo) -> GradingRules:
    """面向东盟职业院校中文诊断的默认六段制规则。"""
    return GradingRules(
        version=version,
        level_bands=(
            LevelBand("C2 精通", 0.90),
            LevelBand("C1 熟练", 0.75),
            LevelBand("B2 进阶", 0.60),
            LevelBand("B1 入门应用", 0.40),
            LevelBand("A2 基础", 0.20),
            LevelBand("A1 启蒙", 0.0),
        ),
        gap_threshold=0.5,
        evidence_k=0.8,
        min_items=5,
        max_items=20,
        target_uncertainty=0.25,
        initial_ability=0.5,
    )
