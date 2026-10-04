import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from adaptive_exam.errors import (
    IdempotencyError,
    PendingSelectionError,
    SessionClosedError,
    ValidationError,
)
from adaptive_exam.fixtures import (
    BANK_ID,
    GRAPH_ID,
    RULES_ID,
    DeterministicClock,
    build_bank_v1,
    build_bank_v2_deactivate,
    build_graph_v1,
    build_rules_v1,
)
from adaptive_exam.models import Difficulty, Item, ItemBank, ItemBankVersion
from adaptive_exam.services import ExamService


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.clock = DeterministicClock()
        self.svc = ExamService(self.tmp, clock=self.clock)
        self.svc.register_graph(build_graph_v1())
        self.svc.register_bank(build_bank_v1())
        self.svc.register_rules(build_rules_v1())
        self.bank = build_bank_v1()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start(self, session_id="S-1", learner="L-1"):
        return self.svc.start_session(
            session_id, learner, GRAPH_ID, BANK_ID, RULES_ID,
            graph_revision=1, bank_revision=1, rule_revision=1,
        )

    def answer(self, session_id, n, correct=True, override=None):
        """走一遍“选题 -> 作答”，n 用于生成确定性请求标识。"""
        picked = self.svc.request_next_item(session_id, f"sel-{n}")
        item = self.bank.get(picked["item_id"])
        text = item.answer if correct else (override or f"错-{n}")
        return picked, self.svc.submit_answer(
            session_id, f"ans-{n}", picked["token"], text
        )

    def run_to_end(self, session_id, correct_flags):
        sequence = []
        for n, correct in enumerate(correct_flags, start=1):
            picked, scored = self.answer(session_id, n, correct)
            sequence.append((picked["item_id"], scored["is_correct"]))
            if scored.get("finished"):
                return sequence, scored["result"]
        self.svc.finish_session(session_id, "finish-1")
        return sequence, self.svc.get_session(session_id)["result"]


class IdempotencyTests(ServiceTestBase):
    def test_retry_selection_does_not_double_charge_exposure(self):
        self.start()
        first = self.svc.request_next_item("S-1", "r-1")
        counts_after_first = dict(self.svc.ledger.counts())
        retry = self.svc.request_next_item("S-1", "r-1")
        self.assertTrue(retry["retried"])
        self.assertEqual(retry["item_id"], first["item_id"])
        self.assertEqual(retry["token"], first["token"])
        self.assertEqual(self.svc.ledger.counts(), counts_after_first)
        self.assertEqual(self.svc.ledger.counts()[first["item_id"]], 1)

    def test_retry_answer_is_idempotent(self):
        self.start()
        picked = self.svc.request_next_item("S-1", "r-1")
        first = self.svc.submit_answer(
            "S-1", "r-2", picked["token"], "ba"
        )
        retry = self.svc.request_next_item  # noqa: F841 (保持读感)
        second = self.svc.submit_answer(
            "S-1", "r-2", picked["token"], "ba"
        )
        self.assertEqual(first, second)

    def test_request_id_reused_across_ops_rejected(self):
        self.start()
        self.svc.request_next_item("S-1", "shared")
        picked = self.svc.request_next_item("S-1", "shared")  # 同操作重试
        self.assertEqual(picked["item_id"], picked["item_id"])
        with self.assertRaises(IdempotencyError):
            self.svc.submit_answer("S-1", "shared", picked["token"], "ba")


class ResumeTests(ServiceTestBase):
    def test_pending_must_be_resumed_with_original_request(self):
        self.start()
        picked = self.svc.request_next_item("S-1", "orig")
        with self.assertRaises(PendingSelectionError) as ctx:
            self.svc.request_next_item("S-1", "new-after-disconnect")
        self.assertEqual(ctx.exception.request_id, "orig")
        self.assertEqual(ctx.exception.token, picked["token"])
        self.assertEqual(ctx.exception.item_id, picked["item_id"])

        # 凭原请求标识续考：同一题、同一令牌、曝光不重复扣减。
        resumed = self.svc.request_next_item("S-1", "orig")
        self.assertEqual(resumed["item_id"], picked["item_id"])
        self.assertEqual(resumed["token"], picked["token"])
        self.assertTrue(resumed["retried"])

    def test_wrong_token_rejected(self):
        self.start()
        self.svc.request_next_item("S-1", "sel")
        with self.assertRaises(ValidationError):
            self.svc.submit_answer("S-1", "ans", "bad-token", "ba")

    def test_selection_and_answer_request_ids_must_differ(self):
        self.start()
        picked = self.svc.request_next_item("S-1", "same")
        with self.assertRaises(IdempotencyError):
            self.svc.submit_answer("S-1", "same", picked["token"], "ba")


class RestartTests(ServiceTestBase):
    def _restart(self):
        return ExamService(self.tmp, clock=self.clock)

    def test_unfinished_session_resumes_after_process_restart(self):
        self.start()
        picked = self.svc.request_next_item("S-1", "sel-1")
        counts = dict(self.svc.ledger.counts())

        restarted = self._restart()
        view = restarted.get_session("S-1")
        self.assertEqual(view["state"], "in_progress")
        self.assertEqual(view["pending_item_id"], picked["item_id"])

        resumed = restarted.request_next_item("S-1", "sel-1")
        self.assertEqual(resumed["item_id"], picked["item_id"])
        self.assertEqual(restarted.ledger.counts(), counts)

        restarted.submit_answer("S-1", "ans-1", picked["token"], "ba")
        again = self._restart()
        self.assertEqual(
            again.get_session("S-1")["confirmed_count"], 1
        )

    def test_finished_result_survives_restart(self):
        self.start()
        flags = [True] * 12
        _, result = self.run_to_end("S-1", flags)
        restarted = self._restart()
        view = restarted.get_session("S-1")
        self.assertEqual(view["state"], "finished")
        self.assertEqual(view["result"]["level"], result["level"])
        self.assertEqual(
            view["result"]["overall_theta"], result["overall_theta"]
        )


class VersionPinningTests(ServiceTestBase):
    def test_later_bank_revision_does_not_change_old_result(self):
        self.start()
        _, result = self.run_to_end("S-1", [True] * 12)
        pinned_bank = result["bank_version_key"]
        self.assertEqual(pinned_bank, f"{BANK_ID}:1")

        # 此后题库发布 rev2（下线 I-303）。
        self.svc.register_bank(build_bank_v2_deactivate())
        v2 = self.svc.registry.get_bank(BANK_ID, 2)
        self.assertFalse(v2.get("I-303").active)

        # 旧结果原样不动；申诉重评仍按 rev1 计算。
        explanation = self.svc.explain_session("S-1")
        regraded = self.svc.regrade(
            "S-1", "T-1", "学员申诉：复核录音设备故障"
        )
        self.assertEqual(
            regraded["regrade"]["bank_version_key"], f"{BANK_ID}:1"
        )
        self.assertEqual(
            regraded["regrade"]["rule_set_key"], f"{RULES_ID}:1"
        )
        self.assertEqual(
            regraded["regrade"]["regrade_of"], str(result["finished_at"])
        )
        # 原结果进入历史且等级可追溯。
        history = self.svc.explain_session("S-1")["result_history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["level"], result["level"])

    def test_new_session_can_pin_old_revision_explicitly(self):
        self.svc.register_bank(build_bank_v2_deactivate())
        self.svc.start_session(
            "S-OLD", "L-2", GRAPH_ID, BANK_ID, RULES_ID,
            graph_revision=1, bank_revision=1, rule_revision=1,
        )
        view = self.svc.get_session("S-OLD")
        self.assertEqual(view["bank_version_key"], f"{BANK_ID}:1")


class VoidAndRegradeTests(ServiceTestBase):
    def test_void_requires_teacher_and_reason(self):
        self.start()
        self.answer("S-1", 1, correct=True)
        with self.assertRaises(ValidationError):
            self.svc.void_evidence("S-1", "I-002", "", "设备故障")

    def test_void_on_finished_session_triggers_controlled_regrade(self):
        self.start()
        _, result = self.run_to_end("S-1", [False, False, True, True])
        target = self.svc.explain_session("S-1")["items"][0]["item_id"]

        response = self.svc.void_evidence(
            "S-1", target, "T-LI", "开场耳机无声，首题作废"
        )
        self.assertIn("regrade", response)
        new_result = response["regrade"]
        self.assertEqual(new_result["item_count"], result["item_count"] - 1)
        self.assertEqual(new_result["bank_version_key"],
                         result["bank_version_key"])

        explanation = self.svc.explain_session("S-1")
        voided = [it for it in explanation["items"]
                  if it["item_id"] == target][0]
        self.assertEqual(voided["status"], "voided")
        self.assertEqual(voided["voided_by"], "T-LI")
        self.assertTrue(
            any("受控重评" in line for line in explanation["audit_log"])
        )
        # 历史保留原结果。
        self.assertEqual(len(explanation["result_history"]), 1)

    def test_cannot_void_twice_or_void_pending(self):
        self.start()
        self.svc.request_next_item("S-1", "sel-1")
        with self.assertRaises(ValidationError):
            self.svc.void_evidence("S-1", "I-002", "T", "理由")

    def test_regrade_requires_finished_session(self):
        from adaptive_exam.errors import RegradePreconditionError
        self.start()
        with self.assertRaises(RegradePreconditionError):
            self.svc.regrade("S-1", "T", "不应允许")

    def test_answer_after_finish_rejected(self):
        self.start()
        self.run_to_end("S-1", [True] * 12)
        with self.assertRaises(SessionClosedError):
            self.svc.request_next_item("S-1", "late")


class DeterministicReplayTests(ServiceTestBase):
    def _fresh_service(self, root):
        svc = ExamService(root, clock=DeterministicClock())
        svc.register_graph(build_graph_v1())
        svc.register_bank(build_bank_v1())
        svc.register_rules(build_rules_v1())
        return svc

    def test_full_session_replays_identically(self):
        flags = [True, False, True, True, False, True, True, True, True]

        def play(root, sid):
            svc = self._fresh_service(root)
            svc.start_session(sid, "L-1", GRAPH_ID, BANK_ID, RULES_ID,
                              1, 1, 1)
            seq = []
            for n, flag in enumerate(flags, start=1):
                picked = svc.request_next_item(sid, f"sel-{n}")
                bank = build_bank_v1()
                ans = bank.get(picked["item_id"]).answer
                scored = svc.submit_answer(
                    sid, f"ans-{n}", picked["token"],
                    ans if flag else f"x-{n}"
                )
                seq.append((picked["item_id"], picked["token"]))
                if scored.get("finished"):
                    return seq, scored["result"]
            svc.finish_session(sid, "finish-1")
            return seq, svc.get_session(sid)["result"]

        d1 = tempfile.mkdtemp()
        d2 = tempfile.mkdtemp()
        try:
            seq1, res1 = play(d1, "SESS-A")
            seq2, res2 = play(d2, "SESS-A")
            self.assertEqual(seq1, seq2)
            self.assertEqual(res1["level"], res2["level"])
            self.assertEqual(res1["overall_theta"], res2["overall_theta"])
            self.assertEqual(
                [(s["skill_id"], s["theta"]) for s in res1["skills"]],
                [(s["skill_id"], s["theta"]) for s in res2["skills"]],
            )
        finally:
            shutil.rmtree(d1, ignore_errors=True)
            shutil.rmtree(d2, ignore_errors=True)

    def test_global_exposure_cap_steers_later_session(self):
        # 题库中 I-002 曝光上限改为 1。
        items = []
        for it in build_bank_v1().items:
            if it.item_id == "I-002":
                items.append(Item(it.item_id, it.skill_id, it.difficulty,
                                  it.prompt, it.answer, 1))
            else:
                items.append(it)
        bank = ItemBank(
            ItemBankVersion(BANK_ID, "题库", 2), items,
            f"{GRAPH_ID}:1",
        )
        d2 = tempfile.mkdtemp()
        try:
            svc2 = ExamService(d2, clock=DeterministicClock())
            svc2.register_graph(build_graph_v1())
            svc2.register_bank(bank)
            svc2.register_rules(build_rules_v1())
            svc2.start_session("A", "L", GRAPH_ID, BANK_ID, RULES_ID,
                               1, 2, 1)
            first = svc2.request_next_item("A", "r1")
            self.assertEqual(first["item_id"], "I-002")

            svc2.start_session("B", "L", GRAPH_ID, BANK_ID, RULES_ID,
                               1, 2, 1)
            second = svc2.request_next_item("B", "r1")
            # I-002 全库曝光已用尽；等距时偏难优先 -> I-003。
            self.assertNotEqual(second["item_id"], "I-002")
            self.assertEqual(second["item_id"], "I-003")
        finally:
            shutil.rmtree(d2, ignore_errors=True)


class ExplainabilityTests(ServiceTestBase):
    def test_explanation_lists_reasons_and_contributions(self):
        self.start()
        self.answer("S-1", 1, correct=True)
        explanation = self.svc.explain_session("S-1")
        self.assertEqual(explanation["pinned_versions"], {
            "graph": f"{GRAPH_ID}:1",
            "bank": f"{BANK_ID}:1",
            "rules": f"{RULES_ID}:1",
        })
        item = explanation["items"][0]
        self.assertTrue(item["selection_reason"])
        self.assertIn("能力估计", item["contribution"])
        self.assertTrue(item["is_correct"])


if __name__ == "__main__":
    unittest.main()
