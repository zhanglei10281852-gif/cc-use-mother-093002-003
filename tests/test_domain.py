import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from adaptive_exam.codecs import (
    bank_from_dict,
    bank_to_dict,
    evidence_from_dict,
    evidence_to_dict,
    graph_from_dict,
    graph_to_dict,
    result_from_dict,
    result_to_dict,
    rules_from_dict,
    rules_to_dict,
    session_from_dict,
    session_to_dict,
)
from adaptive_exam.fixtures import (
    build_bank_v1,
    build_graph_v1,
    build_rules_v1,
)
from adaptive_exam.models import (
    Difficulty,
    Evidence,
    EvidenceStatus,
    Item,
    ScoringRuleSet,
    Session,
    SessionResult,
    SessionState,
    SkillAssessment,
    SkillGraphVersion,
    SkillNode,
    SkillGraph,
)


class SkillGraphTests(unittest.TestCase):
    def test_rejects_missing_prerequisite(self):
        with self.assertRaises(ValueError):
            SkillGraph(
                SkillGraphVersion("G", "g", 1),
                [SkillNode("A", "甲", ("GHOST",))],
            )

    def test_rejects_cycle(self):
        with self.assertRaises(ValueError):
            SkillGraph(
                SkillGraphVersion("G", "g", 1),
                [
                    SkillNode("A", "甲", ("B",)),
                    SkillNode("B", "乙", ("A",)),
                ],
            )

    def test_descendants_and_closure(self):
        graph = build_graph_v1()
        self.assertEqual(
            graph.descendants("S1-PINYIN"),
            {"S2-LISTEN", "S3-READ", "S4-GRAMMAR"},
        )
        self.assertEqual(
            graph.closure(["S4-GRAMMAR"]),
            {"S1-PINYIN", "S3-READ", "S4-GRAMMAR"},
        )

    def test_duplicate_skill_rejected(self):
        with self.assertRaises(ValueError):
            SkillGraph(
                SkillGraphVersion("G", "g", 1),
                [SkillNode("A", "甲"), SkillNode("A", "甲二")],
            )


class ItemTests(unittest.TestCase):
    def test_item_requires_prompt(self):
        with self.assertRaises(ValueError):
            Item("I1", "S1", Difficulty.EASY, "", "x", 10)

    def test_exposure_limit_must_be_positive(self):
        with self.assertRaises(ValueError):
            Item("I1", "S1", Difficulty.EASY, "题", "x", 0)


class RuleSetTests(unittest.TestCase):
    def test_thresholds_must_be_ascending(self):
        with self.assertRaises(ValueError):
            ScoringRuleSet("R", 1, ("低", "高"), (3.0, 2.0))

    def test_level_for_threshold_inclusive(self):
        rules = build_rules_v1()
        self.assertEqual(rules.level_for(2.5), "初级")
        self.assertEqual(rules.level_for(4.9), "高级")
        self.assertEqual(rules.level_for(1.0), "入门")


class CodecRoundTripTests(unittest.TestCase):
    def test_graph_round_trip(self):
        graph = build_graph_v1()
        restored = graph_from_dict(graph_to_dict(graph))
        self.assertEqual(restored.version.key, graph.version.key)
        self.assertEqual(restored.skill_ids, graph.skill_ids)
        self.assertEqual(
            restored.node("S4-GRAMMAR").prerequisites, ("S3-READ",)
        )

    def test_bank_round_trip(self):
        bank = build_bank_v1()
        restored = bank_from_dict(bank_to_dict(bank))
        self.assertEqual(restored.version.key, bank.version.key)
        self.assertEqual(restored.get("I-001").prompt, "“八”的拼音是？")

    def test_rules_round_trip(self):
        rules = build_rules_v1()
        restored = rules_from_dict(rules_to_dict(rules))
        self.assertEqual(restored.key, rules.key)
        self.assertEqual(restored.levels, rules.levels)

    def test_full_session_round_trip(self):
        rules = build_rules_v1()
        session = Session(
            session_id="S-1", learner_id="L-1",
            graph_version_key="G:1", bank_version_key="B:1",
            rule_set_key=rules.key, created_at=1.0,
        )
        session.append_evidence(Evidence(
            item_id="I-001", skill_id="S1-PINYIN",
            difficulty=Difficulty.EASY, status=EvidenceStatus.CONFIRMED,
            presented_at=1.0, request_id="req-1",
            selection_reason="冷启动", token="tok",
            answer_request_id="req-2", answer_given="shi",
            is_correct=False, answered_at=2.0,
            contribution="下调",
        ))
        session.result = SessionResult(
            overall_theta=2.6, overall_sigma=0.7, level="初级",
            rule_set_key=rules.key, graph_version_key="G:1",
            bank_version_key="B:1", finished_at=3.0, item_count=1,
            skill_assessments=(SkillAssessment(
                "S1-PINYIN", 2.6, 0.7, False, False, 1, 0, 1
            ),),
        )
        session.result_history.append(session.result)
        restored = session_from_dict(session_to_dict(session))
        self.assertEqual(restored.session_id, "S-1")
        self.assertEqual(restored.state, SessionState.IN_PROGRESS)
        self.assertEqual(restored.evidence[0].answer_request_id, "req-2")
        self.assertEqual(restored.result.level, "初级")
        self.assertEqual(len(restored.result_history), 1)


if __name__ == "__main__":
    unittest.main()
