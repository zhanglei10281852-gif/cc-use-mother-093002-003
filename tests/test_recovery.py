import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from adaptive_exam.catalog import load_catalog
from adaptive_exam.persistence import ExposureRepository, SessionRepository
from adaptive_exam.service import ExamService

SEED = Path(__file__).parents[1] / "examples" / "catalog_seed.json"


class CrashWindowTests(unittest.TestCase):
    """模拟崩溃发生在“曝光预留已落盘、会话解释尚未写入”的窗口。"""

    def test_reservation_without_selection_is_backfilled_idempotently(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = load_catalog(SEED)
            sessions = SessionRepository(Path(tmp) / "sessions")
            exposures = ExposureRepository(Path(tmp) / "exposures.json")
            service = ExamService(catalog, sessions, exposures)
            session = service.start_session(
                "GRAPH-CN:1", "BANK-CN:1", "RULES-CN:1", session_id="S-CRASH"
            )

            # 手工制造：账本已有预留，会话里却还没有选题解释。
            chosen = service._advise(
                catalog.bundle("GRAPH-CN:1", "BANK-CN:1", "RULES-CN:1"),
                sessions.load("S-CRASH"),
            ).chosen.item
            exposures.reserve("S-CRASH", "req-orphan", chosen.item_id, chosen.exposure_cap)
            count_after = exposures.ledger.counts.get(chosen.item_id, 0)

            # “重启后”的新服务实例收到同一请求重试：补齐解释且不重复扣减。
            restarted = ExamService(
                catalog,
                SessionRepository(Path(tmp) / "sessions"),
                ExposureRepository(Path(tmp) / "exposures.json"),
            )
            view = restarted.next_item("S-CRASH", "req-orphan")
            self.assertEqual(view.item_id, chosen.item_id)
            self.assertTrue(view.idempotent_replay)
            ledger = ExposureRepository(Path(tmp) / "exposures.json")
            self.assertEqual(ledger.ledger.counts.get(chosen.item_id, 0), count_after)
            report = restarted.teacher_report("S-CRASH")
            self.assertEqual(len(report["selections"]), 1)


if __name__ == "__main__":
    unittest.main()
