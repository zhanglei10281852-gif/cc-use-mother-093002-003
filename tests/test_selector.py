import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from adaptive_exam.fixtures import build_bank_v1, build_graph_v1, build_rules_v1
from adaptive_exam.models import Difficulty, Evidence, EvidenceStatus
from adaptive_exam.selector import select_next, should_stop


def _confirmed(skill: str, difficulty: Difficulty, correct: bool,
               item_id: str) -> Evidence:
    return Evidence(
        item_id=item_id, skill_id=skill, difficulty=difficulty,
        status=EvidenceStatus.CONFIRMED, presented_at=0.0,
        request_id=f"req-{item_id}", selection_reason="", token="t",
        is_correct=correct, answered_at=1.0,
    )


class SelectorTests(unittest.TestCase):
    def setUp(self):
        self.graph = build_graph_v1()
        self.bank = build_bank_v1()
        self.rules = build_rules_v1()

    def select(self, confirmed=(), presented=frozenset(), exposures=None,
               gaps=frozenset()):
        return select_next(
            self.graph, self.bank, list(confirmed),
            presented if presented else frozenset(e.item_id for e in confirmed),
            exposures or {}, self.rules, frozenset(gaps),
        )

    def test_cold_start_picks_first_graph_skill_easy_item(self):
        selection = self.select()
        self.assertIsNotNone(selection)
        # 覆盖轮转取图谱首个技能 S1；目标难度 2.0 -> I-002 (EASY)。
        self.assertEqual(selection.item.skill_id, "S1-PINYIN")
        self.assertEqual(selection.item.difficulty, Difficulty.EASY)
        self.assertEqual(selection.item.item_id, "I-002")
        self.assertIn("冷启动", selection.reason)

    def test_deterministic_across_calls_and_shuffled_bank(self):
        first = self.select()
        second = self.select()
        self.assertEqual(first.item.item_id, second.item.item_id)

        # 以乱序题目重建题库，选择必须一致。
        from adaptive_exam.models import ItemBank, ItemBankVersion
        items = list(self.bank.items)
        shuffled = [items[i] for i in (3, 0, 9, 1, 11, 5, 8, 2, 10, 4, 7, 6)]
        reordered = ItemBank(
            ItemBankVersion(self.bank.version.entity_id,
                            self.bank.version.display_name, 1),
            shuffled, self.bank.graph_version_key,
        )
        again = select_next(
            self.graph, reordered, [], frozenset(), {}, self.rules
        )
        self.assertEqual(again.item.item_id, first.item.item_id)

    def test_tie_breaks_by_item_id(self):
        # S1 已有足够证据后轮转 S2；构造同距并列难度。
        confirmed = [
            _confirmed("S1-PINYIN", Difficulty.EASY, True, "I-002"),
        ]
        selection = self.select(
            confirmed, frozenset({"I-002"})
        )
        # S2 冷启动目标 2.0：I-101(EASY) 最近。
        self.assertEqual(selection.item.skill_id, "S2-LISTEN")
        self.assertEqual(selection.item.item_id, "I-101")

    def test_presented_and_exposure_excluded(self):
        # I-002 已呈现：I-001(b=1) 与 I-003(b=3) 到目标 2.0 等距，
        # 按设计偏难优先 -> I-003。
        selection = self.select(presented=frozenset({"I-002"}))
        self.assertEqual(selection.item.item_id, "I-003")

        # 再排除 I-003 后落到 I-001。
        selection = self.select(presented=frozenset({"I-002", "I-003"}))
        self.assertEqual(selection.item.item_id, "I-001")

        # I-001 也用尽全局曝光 -> 落到 I-004 (b=4)。
        selection = self.select(
            presented=frozenset({"I-002", "I-003"}),
            exposures={"I-001": 1000},
        )
        self.assertEqual(selection.item.item_id, "I-004")

    def test_gap_blocks_descendant_skills(self):
        # S1 缺口只阻塞其后继 S2/S3/S4，S1 自身仍可测量；
        # 当 S1 题目全部呈现后，应无任何候选。
        s1_items = frozenset(
            it.item_id for it in self.bank.items
            if it.skill_id == "S1-PINYIN"
        )
        selection = self.select(presented=s1_items, gaps={"S1-PINYIN"})
        self.assertIsNone(selection)

    def test_inactive_item_excluded(self):
        from adaptive_exam.fixtures import build_bank_v2_deactivate
        bank_v2 = build_bank_v2_deactivate()
        presented = frozenset(
            it.item_id for it in bank_v2.items
            if it.item_id not in {"I-303"}  # 仅留下线题未呈现
        )
        selection = select_next(
            self.graph, bank_v2, [], presented, {}, self.rules
        )
        # I-303 在 rev2 停用，即便未呈现也不能选。
        self.assertIsNone(selection)


class StopRuleTests(unittest.TestCase):
    def setUp(self):
        self.rules = build_rules_v1()

    def test_min_items_blocks_early_stop(self):
        stop, _ = should_stop(4, self.rules, 0.5, False)
        self.assertFalse(stop)
        stop, reason = should_stop(4, self.rules, 0.5, True)
        self.assertTrue(stop)
        self.assertIn("最少题量", reason)

    def test_convergence_and_max(self):
        stop, _ = should_stop(6, self.rules, 0.7, False)
        self.assertTrue(stop)
        stop, _ = should_stop(12, self.rules, 5.0, False)
        self.assertTrue(stop)
        stop, _ = should_stop(6, self.rules, 1.2, False)
        self.assertFalse(stop)


if __name__ == "__main__":
    unittest.main()
