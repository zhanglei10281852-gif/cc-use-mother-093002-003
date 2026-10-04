"""可复现的演示/测试夹具：东盟职业院校中文诊断迷你内容包。"""
from __future__ import annotations

from .models import (
    Difficulty,
    Item,
    ItemBank,
    ItemBankVersion,
    ScoringRuleSet,
    SkillGraph,
    SkillGraphVersion,
    SkillNode,
)

GRAPH_ID = "G-ASEAN-CN"
BANK_ID = "B-ASEAN-CN"
RULES_ID = "R-ASEAN-CN"


def build_graph_v1() -> SkillGraph:
    return SkillGraph(
        SkillGraphVersion(GRAPH_ID, "东盟职业中文能力图谱", 1),
        [
            SkillNode("S1-PINYIN", "拼音与数字", ()),
            SkillNode("S2-LISTEN", "日常听力", ("S1-PINYIN",)),
            SkillNode("S3-READ", "日常阅读", ("S1-PINYIN",)),
            SkillNode("S4-GRAMMAR", "基础语法", ("S3-READ",)),
        ],
    )


def _item(item_id: str, skill: str, difficulty: Difficulty,
          prompt: str, answer: str, max_exposure: int = 1000) -> Item:
    return Item(item_id, skill, difficulty, prompt, answer, max_exposure)


def build_bank_v1() -> ItemBank:
    items = [
        # S1 拼音与数字
        _item("I-001", "S1-PINYIN", Difficulty.VERY_EASY,
              "“八”的拼音是？", "ba"),
        _item("I-002", "S1-PINYIN", Difficulty.EASY,
              "“十”的拼音是？", "shi"),
        _item("I-003", "S1-PINYIN", Difficulty.MEDIUM,
              "“四”的声调是第几声？", "4"),
        _item("I-004", "S1-PINYIN", Difficulty.HARD,
              "“月”的拼音是？", "yue"),
        # S2 日常听力（题面以文字呈现发音内容）
        _item("I-101", "S2-LISTEN", Difficulty.EASY,
              "听到“nǐ hǎo”，意思是？", "你好"),
        _item("I-102", "S2-LISTEN", Difficulty.MEDIUM,
              "“xiè xie”的意思是？", "谢谢"),
        _item("I-103", "S2-LISTEN", Difficulty.HARD,
              "“请问，地铁站怎么走？”最恰当的应答是？", "往前走右转"),
        # S3 日常阅读
        _item("I-201", "S3-READ", Difficulty.EASY,
              "“水”字的意思是？", "water"),
        _item("I-202", "S3-READ", Difficulty.MEDIUM,
              "“我要一杯咖啡”中说话人想要什么？", "咖啡"),
        _item("I-203", "S3-READ", Difficulty.HARD,
              "“食堂七点半开门”，食堂几点开门？", "七点半"),
        # S4 基础语法
        _item("I-301", "S4-GRAMMAR", Difficulty.MEDIUM,
              "填入：我___学生。", "是"),
        _item("I-302", "S4-GRAMMAR", Difficulty.HARD,
              "填入：昨天我___去学校。", "没"),
        _item("I-303", "S4-GRAMMAR", Difficulty.VERY_HARD,
              "改错：我把饭吃在食堂。", "我在食堂吃饭"),
    ]
    return ItemBank(
        ItemBankVersion(BANK_ID, "东盟职业中文诊断题库", 1),
        items,
        build_graph_v1().version.key,
    )


def build_bank_v2_deactivate() -> ItemBank:
    """题库修订 2：下线一道有歧义的题（旧会话仍引用 rev1）。"""
    bank_v1 = build_bank_v1()
    items = [
        Item(
            item_id=it.item_id,
            skill_id=it.skill_id,
            difficulty=it.difficulty,
            prompt=it.prompt,
            answer=it.answer,
            max_exposure=it.max_exposure,
            active=(it.item_id != "I-303"),
        )
        for it in bank_v1.items
    ]
    return ItemBank(
        ItemBankVersion(BANK_ID, "东盟职业中文诊断题库", 2),
        items,
        build_graph_v1().version.key,
    )


def build_rules_v1() -> ScoringRuleSet:
    return ScoringRuleSet(
        rule_set_id=RULES_ID,
        revision=1,
        levels=("入门", "初级", "中级", "高级"),
        thresholds=(1.5, 2.5, 3.5, 4.5),
        confident_successes=2,
        confident_failures=2,
        theta_prior=3.0,
        convergence_std=0.75,
        max_items=12,
        min_items=5,
    )


class DeterministicClock:
    """固定步进时钟，保证自动化场景时间戳可复现。"""

    def __init__(self, start: float = 1_700_000_000.0, step: float = 1.0):
        self.now = start
        self.step = step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value
