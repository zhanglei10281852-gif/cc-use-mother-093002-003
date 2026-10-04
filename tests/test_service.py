import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from adaptive_exam.bank import build_bank
from adaptive_exam.contracts import VersionInfo
from adaptive_exam.graph import build_graph
from adaptive_exam.catalog import VersionCatalog
from adaptive_exam.persistence import (
    ExposureRepository,
    ExposureExhausted,
    IdempotencyConflict,
    SessionRepository,
)
from adaptive_exam.rules import default_rules
from adaptive_exam.service import ExamService
from adaptive_exam.session import SessionError


def make_catalog(bank_revision: int = 1, extra_caps: dict | None = None):
    catalog = VersionCatalog()
    graph = build_graph(
        VersionInfo("GRAPH-CN", "中文图谱", 1),
        [
            ("PY", "拼音", ()),
            ("VB", "词汇", ("PY",)),
            ("RB", "阅读", ("VB",)),
        ],
    )
    catalog.register_graph(graph)

    caps = {
        "q-py": None, "q-vb": None, "q-rb": None, "q-cap": 2,
        "q-py2": None, "q-vb2": None, "q-rb2": None, "q-py3": None,
    }
    if extra_caps:
        caps.update(extra_caps)

    defs = [
        ("q-py", "拼音题", "对", ("PY",), 0.1, caps["q-py"], 10),
        ("q-vb", "词汇题", "对", ("VB",), 0.4, caps["q-vb"], 10),
        ("q-rb", "阅读题", "对", ("RB",), 0.7, caps["q-rb"], 10),
        ("q-cap", "限量题", "对", ("PY",), 0.2, caps["q-cap"], 10),
        ("q-py2", "拼音题二", "对", ("PY",), 0.15, caps["q-py2"], 10),
        ("q-vb2", "词汇题二", "对", ("VB",), 0.45, caps["q-vb2"], 10),
        ("q-rb2", "阅读题二", "对", ("RB",), 0.75, caps["q-rb2"], 10),
        ("q-py3", "拼音题三", "对", ("PY",), 0.2, caps["q-py3"], 10),
    ]
    if bank_revision >= 2:
        # 修订版改了题干/答案与难度，用来证明旧结果不被改写。
        defs[0] = ("q-py", "修订后的拼音题", "新答案", ("PY",), 0.9, None, 10)
    bank = build_bank(VersionInfo("BANK-CN", "中文题库", bank_revision), defs)
    catalog.register_bank(bank, "GRAPH-CN:1")
    catalog.register_rules(default_rules(VersionInfo("RULES-CN", "规则", 1)))
    return catalog


def make_service(tmp: str, bank_revision: int = 1) -> ExamService:
    return ExamService(
        catalog=make_catalog(bank_revision),
        sessions=SessionRepository(Path(tmp) / "sessions"),
        exposures=ExposureRepository(Path(tmp) / "exposures.json"),
    )


class ServiceFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.service = make_service(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def run_full_session(self, answers=None):
        session = self.service.start_session(
            "GRAPH-CN:1", "BANK-CN:1", "RULES-CN:1", learner_id="L-1"
        )
        answers = answers or {}
        for step in range(1, 6):
            view = self.service.next_item(session.session_id, f"req-{step}")
            self.assertFalse(view.idempotent_replay)
            submitted = answers.get(view.item_id, "对")
            self.service.submit_answer(
                session.session_id, view.item_id, submitted, f"tok-{step}"
            )
        return session.session_id


class IdempotencyTests(ServiceFixture):
    def test_retrying_next_does_not_double_charge_exposure(self):
        session = self.service.start_session(
            "GRAPH-CN:1", "BANK-CN:1", "RULES-CN:1"
        )
        first = self.service.next_item(session.session_id, "req-1")
        # 同一 request_id 连续重试 3 次：同一题、同一预留。
        for _ in range(3):
            again = self.service.next_item(session.session_id, "req-1")
            self.assertEqual(again.item_id, first.item_id)
            self.assertTrue(again.idempotent_replay)
        ledger = ExposureRepository(Path(self.tmp) / "exposures.json")
        # 只有一个请求预留记录；该题曝光计数最多为 1（限量题恰好被选中时）。
        self.assertEqual(len(ledger.ledger.reservations), 1)
        self.assertLessEqual(ledger.ledger.counts.get(first.item_id, 0), 1)

    def test_capped_item_blocks_after_cap_across_sessions(self):
        s1 = self.service.start_session("GRAPH-CN:1", "BANK-CN:1", "RULES-CN:1")
        s2 = self.service.start_session("GRAPH-CN:1", "BANK-CN:1", "RULES-CN:1")
        # q-cap 上限 2：前两次预留成功。
        self._force_present("q-cap", s1.session_id, "r1", other_items=())
        self._force_present("q-cap", s2.session_id, "r2", other_items=())
        # 第三次必须被告知额度用尽（直接调用账本）。
        with self.assertRaises(ExposureExhausted):
            self.service.exposures.reserve(
                s2.session_id, "r3", "q-cap", 2
            )

    def _force_present(self, item_id, sid, req, other_items):
        # 通过账本预留验证额度逻辑。
        self.service.exposures.reserve(sid, req, item_id, 2)

    def test_answer_token_is_idempotent(self):
        sid = self.run_full_session()
        report = self.service.teacher_report(sid)
        # 用旧令牌重放，不应新增作答记录。
        item = report["responses"][0]["item_id"]
        before = len(report["responses"])
        result = self.service.submit_answer(sid, item, "错", "tok-1")
        self.assertTrue(result["replay"])
        report2 = self.service.teacher_report(sid)
        self.assertEqual(len(report2["responses"]), before)

    def test_request_id_conflict_is_rejected(self):
        s = self.service.start_session("GRAPH-CN:1", "BANK-CN:1", "RULES-CN:1")
        # 同一 request_id 先预留 q-py，再试图改用于 q-vb：必须冲突。
        self.service.exposures.reserve(s.session_id, "req-k", "q-py", None)
        with self.assertRaises(IdempotencyConflict):
            self.service.exposures.reserve(
                s.session_id, "req-k", "q-vb", None
            )


class RestartContinuationTests(ServiceFixture):
    def test_unfinished_session_continues_after_process_restart(self):
        sid = self.run_full_session(answers={})  # 5 题，会话未结束
        # 模拟进程重启：丢弃内存对象，重新构建仓储与服务（同一数据目录）。
        restarted = make_service(self.tmp)
        view = restarted.next_item(sid, "req-6")
        self.assertFalse(view.idempotent_replay)
        self.assertTrue(view.item_id)
        restarted.submit_answer(sid, view.item_id, "对", "tok-6")
        report = restarted.teacher_report(sid)
        self.assertEqual(len(report["selections"]), 6)
        self.assertEqual(report["status"], "in_progress")
        # 曝光账本同样跨重启保留。
        self.assertTrue((Path(self.tmp) / "exposures.json").exists())

    def test_replay_after_crash_restores_reservation_without_recharge(self):
        s = self.service.start_session("GRAPH-CN:1", "BANK-CN:1", "RULES-CN:1")
        view = self.service.next_item(s.session_id, "req-crash")
        ledger = ExposureRepository(Path(self.tmp) / "exposures.json")
        count_after = ledger.ledger.counts.get(view.item_id, 0)
        restarted = make_service(self.tmp)
        again = restarted.next_item(s.session_id, "req-crash")
        self.assertEqual(again.item_id, view.item_id)
        self.assertTrue(again.idempotent_replay)
        ledger2 = ExposureRepository(Path(self.tmp) / "exposures.json")
        self.assertEqual(
            ledger2.ledger.counts.get(view.item_id, 0), count_after
        )


class InvalidationAndRegradeTests(ServiceFixture):
    def test_teacher_invalidates_with_evidence_and_regrade_recomputes(self):
        session = self.service.start_session(
            "GRAPH-CN:1", "BANK-CN:1", "RULES-CN:1"
        )
        sid = session.session_id
        first = self.service.next_item(sid, "req-1")
        self.service.submit_answer(sid, first.item_id, "错", "tok-1")
        for step in range(2, 6):
            view = self.service.next_item(sid, f"req-{step}")
            self.service.submit_answer(sid, view.item_id, "对", f"tok-{step}")
        result = self.service.finish(sid)

        # 教师按证据作废第一题（必须有教师标识与理由）。
        with self.assertRaises(SessionError):
            self.service.invalidate_response(sid, first.item_id, "T-9", "")
        self.service.invalidate_response(
            sid, first.item_id, "T-9", "监控发现该题切换窗口，疑似替考"
        )
        previous, latest = self.service.regrade(
            sid, "T-9", "学员申诉：考试环境断网导致误触提交"
        )
        self.assertEqual(previous.result_index, 0)
        self.assertEqual(latest.result_index, 1)
        self.assertEqual(latest.produced_by, "regrade")
        self.assertEqual(latest.teacher_id, "T-9")
        self.assertGreaterEqual(latest.score, previous.score)
        # 作废题不再计入有效作答数。
        self.assertEqual(latest.invalidated_count, 1)
        self.assertEqual(latest.confirmed_count, previous.confirmed_count - 1)

        # 旧结果仍保留在谱系中，且绑定同一冻结版本。
        report = self.service.teacher_report(sid)
        self.assertEqual(len(report["result_lineage"]), 2)
        self.assertEqual(
            report["result_lineage"][0]["version_refs"],
            report["result_lineage"][1]["version_refs"],
        )
        invalidated = [r for r in report["responses"] if r["status"] == "invalidated"]
        self.assertEqual(invalidated[0]["invalidated_by"], "T-9")
        self.assertIsNone(invalidated[0]["contribution"])

    def test_regrade_refuses_different_rule_versions(self):
        sid = self.run_full_session()
        self.service.finish(sid)
        session = self.service.sessions.load(sid)
        from adaptive_exam.session import FinalResult

        tampered = FinalResult(
            result_index=99,
            score=1.0,
            level="X",
            confirmed_count=1,
            invalidated_count=0,
            skill_mastery={},
            skill_variance={},
            skill_answers={},
            gaps={},
            gap_chains={},
            version_refs={**session.version_refs, "bank_qvid": "BANK-CN:9"},
        )
        with self.assertRaises(SessionError):
            session.regrade(tampered)

    def test_finished_session_cannot_answer_but_can_regrade(self):
        sid = self.run_full_session()
        self.service.finish(sid)
        with self.assertRaises(SessionError):
            self.service.next_item(sid, "req-late")
        previous, latest = self.service.regrade(sid, "T-1", "复核")
        self.assertEqual(latest.score, previous.score)  # 无作废时重算结果一致


class VersionBindingTests(ServiceFixture):
    def test_new_bank_revision_does_not_rewrite_old_result(self):
        sid = self.run_full_session()
        old_result = self.service.finish(sid)
        old_refs = dict(old_result.version_refs)
        self.assertEqual(old_refs["bank_qvid"], "BANK-CN:1")

        # 发布题库修订 rev 2：重新构建目录（模拟升级），旧会话仍解析到 rev1。
        upgraded = ExamService(
            catalog=make_catalog(bank_revision=2),
            sessions=SessionRepository(Path(self.tmp) / "sessions"),
            exposures=ExposureRepository(Path(self.tmp) / "exposures.json"),
        )
        # rev2 目录里没有 rev1 时应明确报错；为了旧结果可读，目录登记 rev1+rev2：
        catalog2 = VersionCatalog()
        from adaptive_exam.graph import build_graph as bg

        catalog2.register_graph(
            bg(VersionInfo("GRAPH-CN", "中文图谱", 1),
               [("PY", "拼音", ()), ("VB", "词汇", ("PY",)), ("RB", "阅读", ("VB",))])
        )
        rev1 = make_catalog(1).banks["BANK-CN:1"]
        rev2 = make_catalog(2).banks["BANK-CN:2"]
        catalog2.register_bank(rev1, "GRAPH-CN:1")
        catalog2.register_bank(rev2, "GRAPH-CN:1")
        catalog2.register_rules(default_rules(VersionInfo("RULES-CN", "规则", 1)))
        upgraded2 = ExamService(
            catalog=catalog2,
            sessions=SessionRepository(Path(self.tmp) / "sessions"),
            exposures=ExposureRepository(Path(self.tmp) / "exposures.json"),
        )
        report_after = upgraded2.teacher_report(sid)
        self.assertEqual(
            report_after["result"]["version_refs"]["bank_qvid"], "BANK-CN:1"
        )
        self.assertEqual(
            report_after["result"]["score"], old_result.score
        )
        self.assertEqual(
            report_after["result"]["level"], old_result.level
        )
        # 用 rev2 开的新会话才引用新版本。
        fresh = upgraded2.start_session("GRAPH-CN:1", "BANK-CN:2", "RULES-CN:1")
        self.assertEqual(fresh.version_refs["bank_qvid"], "BANK-CN:2")
        self.assertNotEqual(
            fresh.version_refs["bank_hash"], old_refs["bank_hash"]
        )


class TeacherReportTests(ServiceFixture):
    def test_report_explains_each_selection_and_contribution(self):
        sid = self.run_full_session()
        self.service.finish(sid)
        report = self.service.teacher_report(sid)
        self.assertEqual(len(report["selections"]), 5)
        first = report["selections"][0]
        self.assertIn("score_breakdown", first)
        self.assertTrue(first["candidates"])
        self.assertEqual(
            first["sort_rule"],
            "按综合分降序；分数并列时依次按 tie_rank 升序、题目ID升序确定",
        )
        for response in report["responses"]:
            self.assertIn("contribution", response)
            self.assertIn("skill_mastery_delta", response["contribution"])

    def test_gap_propagation_appears_in_result(self):
        # 专用小世界：唯一题目考阅读(RB)，链 RB->VB->PY；最少题量 1。
        import dataclasses
        from adaptive_exam.rules import default_rules

        catalog = VersionCatalog()
        catalog.register_graph(
            build_graph(
                VersionInfo("G2", "链状图谱", 1),
                [("PY", "拼音", ()), ("VB", "词汇", ("PY",)), ("RB", "阅读", ("VB",))],
            )
        )
        catalog.register_bank(
            build_bank(
                VersionInfo("B2", "单题题库", 1),
                [("q-rb", "阅读题", "对", ("RB",), 0.7, None, 0)],
            ),
            "G2:1",
        )
        rules = dataclasses.replace(
            default_rules(VersionInfo("R2", "规则", 1)), min_items=1, max_items=3
        )
        catalog.register_rules(rules)
        local = ExamService(
            catalog=catalog,
            sessions=SessionRepository(Path(self.tmp) / "s2"),
            exposures=ExposureRepository(Path(self.tmp) / "exp2.json"),
        )
        s = local.start_session("G2:1", "B2:1", "R2:1")
        view = local.next_item(s.session_id, "r1")
        self.assertEqual(view.item_id, "q-rb")
        local.submit_answer(s.session_id, "q-rb", "错", "t1")
        result = local.finish(s.session_id)
        self.assertEqual(result.gaps["RB"], "direct")
        self.assertEqual(result.gaps["VB"], "inferred")
        self.assertEqual(result.gaps["PY"], "inferred")
        # 解释链从直接缺口指向推断出的先修。
        self.assertEqual(result.gap_chains["PY"], ("RB", "VB", "PY"))

    def test_min_items_gate(self):
        session = self.service.start_session(
            "GRAPH-CN:1", "BANK-CN:1", "RULES-CN:1"
        )
        view = self.service.next_item(session.session_id, "r1")
        self.service.submit_answer(session.session_id, view.item_id, "对", "t1")
        with self.assertRaises(SessionError):
            self.service.finish(session.session_id)
        result = self.service.finish(session.session_id, force=True)
        self.assertEqual(result.confirmed_count, 1)


if __name__ == "__main__":
    unittest.main()
