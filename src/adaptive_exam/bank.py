"""版本化题库：题目、能力覆盖、难度与曝光限制。

每道题声明：
- 覆盖的技能集合（题目被答对视为这些技能的正面证据，答错为负面证据）；
- 标定难度（0.0 最易 ~ 1.0 最难）；
- 曝光上限：全系统累计可投放次数，以及同一学员会话内的去重。

题库是不可变值对象。修订题目意味着发布新修订号的新题库，
旧会话永远引用旧题库的限定版本标识与内容摘要。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from .contracts import VersionInfo
from .versioning import content_digest, qvid


@dataclass(frozen=True)
class Item:
    item_id: str
    prompt: str
    answer: str  # 标准答案（去除首尾空白后精确匹配）
    skill_ids: frozenset[str]
    difficulty: float  # [0.0, 1.0]
    exposure_cap: int | None = None  # None 表示不限
    # 并列时的稳定次序键（同分时按它排序，保证跨进程可复现）。
    tie_rank: int = 0

    def __post_init__(self) -> None:
        if not self.item_id or not self.prompt or not self.answer:
            raise ValueError("题目ID、题干与标准答案不能为空")
        if not self.skill_ids:
            raise ValueError(f"题目 {self.item_id} 至少覆盖一个技能")
        if not 0.0 <= self.difficulty <= 1.0:
            raise ValueError(f"题目 {self.item_id} 难度超出 [0,1]")
        if self.exposure_cap is not None and self.exposure_cap < 1:
            raise ValueError(f"题目 {self.item_id} 曝光上限必须为正整数")

    def is_correct(self, submitted: str) -> bool:
        return submitted.strip() == self.answer.strip()


@dataclass(frozen=True)
class ItemBank:
    version: VersionInfo
    items: Mapping[str, Item]
    description: str = ""

    def __post_init__(self) -> None:
        for item in self.items.values():
            if item.item_id not in self.items:
                raise ValueError(f"题库映射的键与题目ID不一致: {item.item_id}")

    @property
    def qualified_id(self) -> str:
        return qvid(self.version)

    @property
    def content_hash(self) -> str:
        payload = [
            {
                "item_id": item.item_id,
                "prompt": item.prompt,
                "answer": item.answer,
                "skill_ids": sorted(item.skill_ids),
                "difficulty": item.difficulty,
                "exposure_cap": item.exposure_cap,
                "tie_rank": item.tie_rank,
            }
            for item in sorted(self.items.values(), key=lambda i: i.item_id)
        ]
        return content_digest(payload)

    def items_for_skill(self, skill_id: str) -> tuple[Item, ...]:
        return tuple(
            sorted(
                (i for i in self.items.values() if skill_id in i.skill_ids),
                key=lambda i: i.item_id,
            )
        )


def build_bank(
    version: VersionInfo,
    item_defs: list[tuple[str, str, str, tuple[str, ...], float, int | None, int]],
) -> ItemBank:
    """按 ``(ID, 题干, 答案, 覆盖技能, 难度, 曝光上限, tie_rank)`` 构建题库。"""
    items: dict[str, Item] = {}
    for item_id, prompt, answer, skill_ids, difficulty, cap, tie_rank in item_defs:
        if item_id in items:
            raise ValueError(f"题目ID重复: {item_id}")
        items[item_id] = Item(
            item_id=item_id,
            prompt=prompt,
            answer=answer,
            skill_ids=frozenset(skill_ids),
            difficulty=difficulty,
            exposure_cap=cap,
            tie_rank=tie_rank,
        )
    return ItemBank(version=version, items=dict(sorted(items.items())))
