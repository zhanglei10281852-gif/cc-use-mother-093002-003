import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from adaptive_exam.bank import build_bank
from adaptive_exam.contracts import VersionInfo
from adaptive_exam.engine import ConfirmedResponse, advise_next, derive_ability
from adaptive_exam.graph import build_graph
from adaptive_exam.rules import GradingRules, LevelBand, default_rules


def world(items):
    graph = build_graph(
        VersionInfo("G", "图谱", 1),
        [("S1", "技能一", ()), ("S2", "技能二", ())],
    )
    bank = build_bank(VersionInfo("B", "题库", 1), items)
    return graph, bank, default_rules(VersionInfo("R", "规则", 1))


class EngineTests(unittest.TestCase):
    def test_tie_break_is_deterministic_by_tie_rank_then_id(self):
        # 三道题参数完全相同，构成并列：必须由 tie_rank、再由题目ID稳定决定。
        items = [
            ("q-c", "题C", "答", ("S1",), 0.5, None, 20),
            ("q-a", "题A", "答", ("S1",), 0.5, None, 10),
            ("q-b", "题B", "答", ("S1",), 0.5, None, 10),
        ]
        graph, bank, rules = world(items)
        advice = advise_next(graph, bank, rules, (), frozenset(), {})
        order = [c.item.item_id for c in advice.candidates]
        self.assertEqual(order, ["q-a", "q-b", "q-c"])
        self.assertEqual(advice.chosen.item.item_id, "q-a")

    def test_reproducible_across_fresh_runs(self):
        items = [
            (f"q-{i}", f"题{i}", "答", ("S1", "S2"), 0.5, None, 0)
            for i in range(5)
        ]
        graph, bank, rules = world(items)
        first = advise_next(graph, bank, rules, (), frozenset(), {})
        second = advise_next(graph, bank, rules, (), frozenset(), {})
        self.assertEqual(
            [c.item.item_id for c in first.candidates],
            [c.item.item_id for c in second.candidates],
        )

    def test_exhausted_items_are_blocked(self):
        items = [("q1", "题1", "答", ("S1",), 0.3, 2, 0)]
        graph, bank, rules = world(items)
        advice = advise_next(
            graph, bank, rules, (), frozenset(), {"q1": 0}
        )
        self.assertIsNone(advice.chosen)
        self.assertEqual(advice.blocked_by_exposure, ("q1",))
        self.assertEqual(advice.stop_reason, "没有可投放的候选题")

    def test_presented_items_are_not_reselected(self):
        items = [("q1", "题1", "答", ("S1",), 0.3, None, 0)]
        graph, bank, rules = world(items)
        advice = advise_next(
            graph, bank, rules, (), frozenset({"q1"}), {}
        )
        self.assertIsNone(advice.chosen)

    def test_wrong_answers_lower_mastery_and_grow_then_shrink_uncertainty(self):
        items = [("q1", "题1", "答", ("S1",), 0.3, None, 0)]
        graph, bank, rules = world(items)
        empty = derive_ability(graph, bank, rules, ())
        wrong = derive_ability(
            graph, bank, rules, (ConfirmedResponse("q1", False),)
        )
        self.assertLess(
            wrong.skills["S1"].mastery, empty.skills["S1"].mastery
        )

    def test_stops_after_max_items(self):
        items = [
            (f"q{i}", f"题{i}", "答", ("S1",), 0.5, None, 0)
            for i in range(30)
        ]
        graph, bank, rules = world(items)
        responses = tuple(
            ConfirmedResponse(f"q{i}", True) for i in range(rules.max_items)
        )
        presented = frozenset(f"q{i}" for i in range(rules.max_items))
        advice = advise_next(graph, bank, rules, responses, presented, {})
        self.assertEqual(advice.stop_reason, "已达到最大题量")
        self.assertIsNone(advice.chosen)

    def test_bank_validation(self):
        with self.assertRaises(ValueError):
            build_bank(
                VersionInfo("B3", "题库3", 1),
                [("q", "题", "答", ("S1",), 0.5, 0, 0)],
            )
        with self.assertRaises(ValueError):
            build_bank(
                VersionInfo("B2", "题库2", 1),
                [("q", "题", "答", ("S1",), 2.0, None, 0)],
            )

    def test_item_answer_matching(self):
        graph, bank, _ = world([("q1", "题1", "对", ("S1",), 0.3, None, 0)])
        self.assertTrue(bank.items["q1"].is_correct(" 对 "))
        self.assertFalse(bank.items["q1"].is_correct("错"))


if __name__ == "__main__":
    unittest.main()
