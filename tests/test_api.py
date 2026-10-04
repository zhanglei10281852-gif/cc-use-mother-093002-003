import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from adaptive_exam.api import serve
from adaptive_exam.catalog import load_catalog

SEED = Path(__file__).parents[1] / "examples" / "catalog_seed.json"


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        catalog = load_catalog(SEED)
        cls.server = serve(
            catalog=catalog,
            session_dir=str(tmp / "sessions"),
            exposure_path=str(tmp / "exposures.json"),
            host="127.0.0.1",
            port=0,
        )
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._tmp.cleanup()

    def request(self, method: str, path: str, payload=None):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_flow_with_retries_invalidation_and_regrade(self):
        status, session = self.request(
            "POST",
            "/sessions",
            {
                "graph_qvid": "GRAPH-CN:1",
                "bank_qvid": "BANK-CN:1",
                "rules_qvid": "RULES-CN:1",
                "learner_id": "ASEAN-7",
                "session_id": "S-E2E-1",
            },
        )
        self.assertEqual(status, 201)

        item_ids = []
        for step in range(1, 6):
            status, view = self.request(
                "POST", "/sessions/S-E2E-1/next", {"request_id": f"req-{step}"}
            )
            self.assertEqual(status, 200)
            self.assertFalse(view["idempotent_replay"])
            item_ids.append(view["item_id"])
            # 网络中断重试：同请求必须返回同题且标记 replay，不重复扣曝光。
            status, retry = self.request(
                "POST", "/sessions/S-E2E-1/next", {"request_id": f"req-{step}"}
            )
            self.assertEqual(retry["item_id"], view["item_id"])
            self.assertTrue(retry["idempotent_replay"])

            answer = "肯定不对的答案" if step == 1 else None
            body = {"item_id": view["item_id"], "answer_token": f"tok-{step}"}
            body["answer"] = answer if answer is not None else self._correct_answer(view["item_id"])
            status, submitted = self.request(
                "POST", "/sessions/S-E2E-1/answers", body
            )
            self.assertEqual(status, 200)
            # 作答令牌重放。
            status, replay_answer = self.request(
                "POST", "/sessions/S-E2E-1/answers", dict(body)
            )
            self.assertTrue(replay_answer["replay"])

        # 无理由作废被拒。
        status, bad = self.request(
            "POST",
            "/sessions/S-E2E-1/invalidate",
            {"item_id": item_ids[0], "teacher_id": "T-1", "reason": ""},
        )
        self.assertEqual(status, 400)
        self.assertEqual(bad["error"]["code"], "invalid_request")

        # 教师按证据作废。
        status, _ = self.request(
            "POST",
            "/sessions/S-E2E-1/invalidate",
            {
                "item_id": item_ids[0],
                "teacher_id": "T-1",
                "reason": "巡考记录：学员第一题期间长时间离屏",
            },
        )
        self.assertEqual(status, 200)

        # 作废后继续补答一题（第 6 题），保证有效作答达到最少题量。
        status, extra = self.request(
            "POST", "/sessions/S-E2E-1/next", {"request_id": "req-6"}
        )
        self.assertEqual(status, 200)
        status, _ = self.request(
            "POST",
            "/sessions/S-E2E-1/answers",
            {
                "item_id": extra["item_id"],
                "answer": self._correct_answer(extra["item_id"]),
                "answer_token": "tok-6",
            },
        )
        self.assertEqual(status, 200)

        status, result = self.request("POST", "/sessions/S-E2E-1/finish", {})
        self.assertEqual(status, 200)
        self.assertEqual(result["version_refs"]["bank_qvid"], "BANK-CN:1")
        self.assertEqual(result["confirmed_count"], 5)
        self.assertEqual(result["invalidated_count"], 1)

        # 结测后继续取题被拒。
        status, late = self.request(
            "POST", "/sessions/S-E2E-1/next", {"request_id": "late"}
        )
        self.assertEqual(status, 400)

        # 申诉受控重评。
        status, regraded = self.request(
            "POST",
            "/sessions/S-E2E-1/regrade",
            {"teacher_id": "T-1", "reason": "学员申诉首题设备误触"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(regraded["latest"]["result_index"], 1)
        self.assertEqual(regraded["latest"]["produced_by"], "regrade")

        # 教师报告：逐题依据 + 贡献 + 谱系。
        status, report = self.request("GET", "/sessions/S-E2E-1/report")
        self.assertEqual(status, 200)
        self.assertEqual(len(report["selections"]), 6)
        self.assertTrue(report["selections"][0]["candidates"])
        self.assertEqual(len(report["result_lineage"]), 2)
        invalidated = [r for r in report["responses"] if r["status"] == "invalidated"]
        self.assertEqual(invalidated[0]["invalidated_by"], "T-1")

    def test_healthz_lists_versions(self):
        status, payload = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertIn("BANK-CN:1", payload["bank_versions"])

    def test_unknown_session_404(self):
        status, payload = self.request("GET", "/sessions/NOPE/report")
        self.assertEqual(status, 404)

    def test_next_requires_request_id(self):
        self.request(
            "POST",
            "/sessions",
            {
                "graph_qvid": "GRAPH-CN:1",
                "bank_qvid": "BANK-CN:1",
                "rules_qvid": "RULES-CN:1",
                "session_id": "S-E2E-2",
            },
        )
        status, payload = self.request(
            "POST", "/sessions/S-E2E-2/next", {}
        )
        self.assertEqual(status, 400)

    @staticmethod
    def _correct_answer(item_id: str) -> str:
        # 与种子题库标准答案一致；通过报告接口反查更稳，这里直接映射常用题。
        mapping = {}
        catalog = load_catalog(SEED)
        return mapping.get(item_id, catalog.banks["BANK-CN:1"].items[item_id].answer)


if __name__ == "__main__":
    unittest.main()
