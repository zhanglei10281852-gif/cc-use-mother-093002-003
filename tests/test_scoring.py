import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from adaptive_exam.fixtures import build_bank_v1, build_graph_v1, build_rules_v1
from adaptive_exam.models import Difficulty, Evidence, EvidenceStatus
from adaptive_exam.scoring import (
    assess_skills,
    score_session,
    update_belief,
    _prior,
)


def _evidence(skill: str, difficulty: Difficulty, correct: bool,
              request_id: str) -> Evidence:
    return Evidence(
        item_id=f"{request_id}-{skill}-{difficulty.name}",
        skill_id=skill, difficulty=difficulty,
        status=EvidenceStatus.CONFIRMED, presented_at=0.0,
        request_id=request_id, selection_reason="", token="t",
        is_correct=correct, answered_at=1.0,
    )


class BeliefUpdateTests(unittest.TestCase):
    def test_easy_correct_moves_theta_up(self):
        rules = build_rules_v1()
        belief = _prior(rules)
        updated = update_belief(belief, Difficulty.VERY_EASY.value, True)
        self.assertGreater(updated.theta, belief.theta)
        self.assertLess(updated.sigma, belief.sigma)

    def test_hard_wrong_moves_theta_down(self):
        rules = build_rules_v1()
        belief = _prior(rules)
        updated = update_belief(belief, Difficulty.VERY_HARD.value, False)
        self.assertLess(updated.theta, belief.theta)

    def test_update_is_bounded(self):
        rules = build_rules_v1()
        belief = _prior(rules)
        for _ in range(50):
            belief = update_belief(belief, Difficulty.VERY_HARD.value, False)
        self.assertGreaterEqual(belief.theta, 1.0)
        for _ in range(50):
            belief = update_belief(belief, Difficulty.VERY_EASY.value, True)
        self.assertLessEqual(belief.theta, 5.0)


class GapPropagationTests(unittest.TestCase):
    def setUp(self):
        self.graph = build_graph_v1()
        self.rules = build_rules_v1()

    def test_prerequisite_gap_propagates_to_descendants(self):
        # S1 连续在高把握题上失败两次（b=2/3，先验 θ=3，均为把握题）。
        evidence = [
            _evidence("S1-PINYIN", Difficulty.MEDIUM, False, "r1"),
            _evidence("S1-PINYIN", Difficulty.EASY, False, "r2"),
            # S2 虽答对，但先修有缺口，掌握不得确认且记录传播来源。
            _evidence("S2-LISTEN", Difficulty.EASY, True, "r3"),
            _evidence("S2-LISTEN", Difficulty.MEDIUM, True, "r4"),
        ]
        assessments = {a.skill_id: a
                       for a in assess_skills(self.graph, evidence,
                                              self.rules)}
        self.assertTrue(assessments["S1-PINYIN"].gap)
        s2 = assessments["S2-LISTEN"]
        self.assertTrue(s2.gap)
        self.assertFalse(s2.mastered)
        self.assertIn("S1-PINYIN", s2.gap_propagated_from)

    def test_transitive_propagation_reaches_grammar(self):
        evidence = [
            _evidence("S1-PINYIN", Difficulty.EASY, False, "r1"),
            _evidence("S1-PINYIN", Difficulty.MEDIUM, False, "r2"),
        ]
        assessments = {a.skill_id: a
                       for a in assess_skills(self.graph, evidence,
                                              self.rules)}
        # S1 -> S3 -> S4：传递后继全部带缺口标记（一旦被测）。
        self.assertTrue(assessments["S1-PINYIN"].gap)

    def test_mastery_confirmed_without_gap(self):
        evidence = [
            _evidence("S2-LISTEN", Difficulty.MEDIUM, True, "r1"),
            _evidence("S2-LISTEN", Difficulty.HARD, True, "r2"),
        ]
        assessments = {a.skill_id: a
                       for a in assess_skills(self.graph, evidence,
                                              self.rules)}
        s2 = assessments["S2-LISTEN"]
        self.assertTrue(s2.mastered)
        self.assertFalse(s2.gap)


class ScoringTests(unittest.TestCase):
    def test_result_pins_versions(self):
        graph = build_graph_v1()
        rules = build_rules_v1()
        evidence = [_evidence("S1-PINYIN", Difficulty.EASY, True, "r1")]
        result = score_session(
            graph, evidence, rules, finished_at=9.0,
            bank_version_key="B-ASEAN-CN:1",
        )
        self.assertEqual(result.rule_set_key, "R-ASEAN-CN:1")
        self.assertEqual(result.bank_version_key, "B-ASEAN-CN:1")
        self.assertEqual(result.graph_version_key, "G-ASEAN-CN:1")
        self.assertEqual(result.item_count, 1)
        self.assertEqual(result.level, rules.level_for(result.overall_theta))

    def test_voided_evidence_excluded(self):
        graph = build_graph_v1()
        rules = build_rules_v1()
        voided = Evidence(
            item_id="v1", skill_id="S1-PINYIN",
            difficulty=Difficulty.HARD, status=EvidenceStatus.VOIDED,
            presented_at=0.0, request_id="r0", selection_reason="",
            token="t", is_correct=False, answered_at=1.0,
        )
        good = _evidence("S1-PINYIN", Difficulty.EASY, True, "r1")
        result = score_session(
            graph, [voided, good], rules, finished_at=9.0,
            bank_version_key="B-ASEAN-CN:1",
        )
        self.assertEqual(result.item_count, 1)


if __name__ == "__main__":
    unittest.main()
