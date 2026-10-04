import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from adaptive_exam.fixtures import (
    BANK_ID,
    GRAPH_ID,
    RULES_ID,
    DeterministicClock,
    build_bank_v1,
    build_graph_v1,
    build_rules_v1,
)
from adaptive_exam.services import ExamService


class CrashWindowTests(unittest.TestCase):
    """模拟进程在写盘间隙崩溃后的恢复语义。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.svc = ExamService(self.tmp, clock=DeterministicClock())
        self.svc.register_graph(build_graph_v1())
        self.svc.register_bank(build_bank_v1())
        self.svc.register_rules(build_rules_v1())
        self.svc.start_session(
            "S-1", "L-1", GRAPH_ID, BANK_ID, RULES_ID, 1, 1, 1
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reserve_is_idempotent(self):
        ok1, item1 = self.svc.ledger.reserve("S-1", "req-x", "I-002", 1000)
        ok2, item2 = self.svc.ledger.reserve("S-1", "req-x", "I-002", 1000)
        self.assertTrue(ok1 and ok2)
        self.assertEqual(item1, item2)
        self.assertEqual(self.svc.ledger.counts()["I-002"], 1)

    def test_reserve_different_sessions_same_request_id(self):
        self.svc.ledger.reserve("S-1", "dup", "I-002", 1000)
        ok, item = self.svc.ledger.reserve("S-2", "dup", "I-003", 1000)
        self.assertTrue(ok)
        self.assertEqual(item, "I-003")
        self.assertEqual(self.svc.ledger.counts()["I-002"], 1)
        self.assertEqual(self.svc.ledger.counts()["I-003"], 1)

    def test_reserve_at_cap_does_not_record(self):
        ok, _ = self.svc.ledger.reserve("S-1", "r", "I-002", 1)
        self.assertTrue(ok)
        ok2, item = self.svc.ledger.reserve("S-1", "r2", "I-002", 1)
        self.assertFalse(ok2)
        self.assertEqual(item, "I-002")
        self.assertEqual(self.svc.ledger.counts()["I-002"], 1)
        # 被拒请求没有留下占用记录，可以稍后用于另一题。
        ok3, item3 = self.svc.ledger.reserve("S-1", "r2", "I-001", 1000)
        self.assertTrue(ok3)
        self.assertEqual(item3, "I-001")

    def test_crash_after_ledger_before_session_resumes_same_item(self):
        # 模拟“台账已扣减、会话尚未写入证据”的崩溃窗口：
        # 直接以请求标识占用冷启动首选 I-002。
        ok, reserved = self.svc.ledger.reserve(
            "S-1", "sel-1", "I-002", 1000
        )
        self.assertTrue(ok)

        view = self.svc.request_next_item("S-1", "sel-1")
        self.assertTrue(view["retried"])
        self.assertEqual(view["item_id"], "I-002")
        # 曝光只扣了一次。
        self.assertEqual(self.svc.ledger.counts()["I-002"], 1)

        # 证据已补录为 PENDING，可正常作答。
        scored = self.svc.submit_answer(
            "S-1", "ans-1", view["token"], "shi"
        )
        self.assertTrue(scored["is_correct"])

    def test_restart_between_reserve_and_session_persists(self):
        self.svc.ledger.reserve("S-1", "sel-9", "I-002", 1000)
        # 进程重启：新服务实例打开同一目录。
        restarted = ExamService(self.tmp, clock=DeterministicClock())
        view = restarted.request_next_item("S-1", "sel-9")
        self.assertEqual(view["item_id"], "I-002")
        self.assertTrue(view["retried"])
        self.assertEqual(restarted.ledger.counts()["I-002"], 1)

    def test_crash_window_with_evidence_but_no_idem_record(self):
        # 正常选题后，人为删除幂等记录，模拟“会话已写、幂等未写”。
        first = self.svc.request_next_item("S-1", "sel-1")
        idem_dir = Path(self.tmp) / "idem" / "S-1"
        for f in idem_dir.glob("*.json"):
            f.unlink()

        view = self.svc.request_next_item("S-1", "sel-1")
        self.assertEqual(view["item_id"], first["item_id"])
        self.assertTrue(view["retried"])
        self.assertEqual(self.svc.ledger.counts()[first["item_id"]], 1)

    def test_concurrent_same_request_charges_exposure_once(self):
        results: list = []
        errors: list = []

        def worker():
            try:
                results.append(self.svc.request_next_item("S-1", "race-1"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertTrue(all(r["item_id"] == results[0]["item_id"]
                            for r in results))
        self.assertEqual(self.svc.ledger.counts()[results[0]["item_id"]], 1)
        session = self.svc.repo.get("S-1")
        self.assertEqual(len(session.evidence), 1)


if __name__ == "__main__":
    unittest.main()
