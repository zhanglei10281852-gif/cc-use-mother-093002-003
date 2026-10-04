import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from adaptive_exam.errors import ValidationError
from adaptive_exam.fixtures import (
    BANK_ID,
    GRAPH_ID,
    RULES_ID,
    DeterministicClock,
    build_bank_v1,
    build_graph_v1,
    build_rules_v1,
)
from adaptive_exam.models import Difficulty, Item, ItemBank, ItemBankVersion
from adaptive_exam.services import ExamService


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.svc = ExamService(self.tmp, clock=DeterministicClock())
        self.svc.register_graph(build_graph_v1())
        self.svc.register_bank(build_bank_v1())
        self.svc.register_rules(build_rules_v1())
        self.bank = build_bank_v1()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _start(self, sid):
        self.svc.start_session(
            sid, "L", GRAPH_ID, BANK_ID, RULES_ID, 1, 1, 1
        )

    def _play(self, sid, skill, answer_fn, max_rounds=12):
        """按题库答案/错误作答完成一场会话，返回 (题序, 结果)。"""
        order = []
        for n in range(1, max_rounds + 1):
            picked = self.svc.request_next_item(sid, f"sel-{n}")
            if picked.get("finished"):
                return order, picked["result"]
            item = self.bank.get(picked["item_id"])
            answer = answer_fn(item)
            scored = self.svc.submit_answer(
                sid, f"ans-{n}", picked["token"], answer
            )
            order.append(picked["item_id"])
            if scored.get("finished"):
                return order, scored["result"]
        self.svc.finish_session(sid, "fin")
        return order, self.svc.get_session(sid)["result"]


class AdaptationDifferentiationTests(_Base):
    def test_beginner_and_advanced_diverge_and_no_repeat(self):
        # 高水平学员：全对。
        self._start("STRONG")
        strong_order, strong_result = self._play(
            "STRONG", None, lambda it: it.answer
        )
        self.assertEqual(len(strong_order), len(set(strong_order)))
        self.assertIn(strong_result["level"], ("中级", "高级"))

        # 初学者：全错。
        self._start("WEAK")
        weak_order, weak_result = self._play(
            "WEAK", None, lambda it: f"错-{it.item_id}"
        )
        # 初学者达到最少题量、先修缺口确认后即止损，不再连续受挫，
        # 等级与能力估计明显低于高水平学员。
        self.assertLess(weak_result["overall_theta"],
                        strong_result["overall_theta"])
        self.assertLessEqual(len(weak_order), len(strong_order))
        self.assertNotEqual(strong_order, weak_order)

        # 初学者没有被反复推送更难的同技能题：S1 只有 2 条证据即止损。
        weak_explained = self.svc.explain_session("WEAK")
        s1_items = [it for it in weak_explained["items"]
                    if it["skill_id"] == "S1-PINYIN"]
        self.assertLessEqual(len(s1_items), 3)

    def test_level_basis_is_present_and_pinned(self):
        self._start("S")
        _, result = self._play("S", None, lambda it: it.answer)
        self.assertIn("阈值区间", result["level_basis"])
        self.assertIn(f"{RULES_ID}:1", result["level_basis"])


class ExplainabilityContentTests(_Base):
    def test_explain_shows_gap_and_mastery_per_skill(self):
        self._start("S")
        self._play("S", None, lambda it: it.answer)
        explanation = self.svc.explain_session("S")
        # 每题都有选择依据；已确认题都有贡献说明。
        for entry in explanation["items"]:
            self.assertTrue(entry["selection_reason"])
            if entry["status"] == "confirmed":
                self.assertIn("能力估计", entry["contribution"])
        # 结果含逐技能判定。
        skills = {s["skill_id"]: s
                  for s in explanation["result"]["skills"]}
        self.assertTrue(any(s["mastered"] for s in skills.values()))
        # 审计日志记录了结束与等级。
        self.assertTrue(
            any("会话结束" in line for line in explanation["audit_log"])
        )


class ContentValidationTests(_Base):
    def test_bank_referencing_unknown_skill_rejected(self):
        bad_bank = ItemBank(
            ItemBankVersion("B-BAD", "坏题库", 1),
            [Item("X1", "GHOST-SKILL", Difficulty.EASY, "题", "答", 10)],
            f"{GRAPH_ID}:1",
        )
        with self.assertRaises(ValidationError):
            self.svc.register_bank(bad_bank)

    def test_bank_for_other_graph_revision_rejected_at_start(self):
        # 建立第二版图谱（新增技能不影响结构），用 rev1 题库与 rev2
        # 图谱开考应被拒绝。
        from adaptive_exam.models import SkillGraph, SkillGraphVersion, SkillNode
        graph_v2 = SkillGraph(
            SkillGraphVersion(GRAPH_ID, "图谱", 2),
            [SkillNode("S1-PINYIN", "拼音", ()),
             SkillNode("S2-LISTEN", "听力", ("S1-PINYIN",)),
             SkillNode("S3-READ", "阅读", ("S1-PINYIN",)),
             SkillNode("S4-GRAMMAR", "语法", ("S3-READ",)),
             SkillNode("S5-WRITE", "写作", ("S4-GRAMMAR",))],
        )
        self.svc.register_graph(graph_v2)
        with self.assertRaises(ValidationError):
            self.svc.start_session(
                "S-X", "L", GRAPH_ID, BANK_ID, RULES_ID,
                graph_revision=2, bank_revision=1, rule_revision=1,
            )


if __name__ == "__main__":
    unittest.main()
