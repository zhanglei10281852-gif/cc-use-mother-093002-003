import json
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from adaptive_exam.api import _Handler
from adaptive_exam.codecs import bank_to_dict, graph_to_dict, rules_to_dict
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


class HttpServerHarness:
    def __init__(self, root: str):
        service = ExamService(root, clock=DeterministicClock())
        service.register_graph(build_graph_v1())
        service.register_bank(build_bank_v1())
        service.register_rules(build_rules_v1())
        handler = type("BoundHandler", (_Handler,), {"service": service})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def _request(port: int, method: str, path: str, payload=None):
    url = f"http://127.0.0.1:{port}{path}"
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class ApiFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.harness = HttpServerHarness(self.tmp).__enter__()
        self.port = self.harness.port

    def tearDown(self):
        self.harness.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_health(self):
        status, body = _request(self.port, "GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})

    def test_full_http_flow_with_retries(self):
        # 开考
        status, body = _request(self.port, "POST", "/sessions", {
            "session_id": "HTTP-1", "learner_id": "L-9",
            "graph_entity_id": GRAPH_ID, "bank_entity_id": BANK_ID,
            "rule_set_id": RULES_ID,
            "graph_revision": 1, "bank_revision": 1, "rule_revision": 1,
        })
        self.assertEqual(status, 201)

        # 选题 + 完全相同的重试请求
        status, first = _request(self.port, "POST",
                                 "/sessions/HTTP-1/next",
                                 {"request_id": "n1"})
        self.assertEqual(status, 200)
        self.assertFalse(first["retried"])
        status, retry = _request(self.port, "POST",
                                 "/sessions/HTTP-1/next",
                                 {"request_id": "n1"})
        self.assertEqual(status, 200)
        self.assertTrue(retry["retried"])
        self.assertEqual(retry["item_id"], first["item_id"])

        # 断网换新请求标识 -> 409 且带续考信息
        status, conflict = _request(self.port, "POST",
                                    "/sessions/HTTP-1/next",
                                    {"request_id": "n1-other-connection"})
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"], "pending_selection")
        self.assertEqual(conflict["request_id"], "n1")
        self.assertEqual(conflict["item_id"], first["item_id"])

        # 错误令牌作答 -> 400
        status, bad = _request(self.port, "POST",
                               "/sessions/HTTP-1/answer",
                               {"request_id": "a1", "token": "wrong",
                                "answer": "ba"})
        self.assertEqual(status, 400)

        # 正确作答，重试一次
        bank = build_bank_v1()
        answer = bank.get(first["item_id"]).answer
        payload = {"request_id": "a1", "token": first["token"],
                   "answer": answer}
        status, scored = _request(self.port, "POST",
                                  "/sessions/HTTP-1/answer", payload)
        self.assertEqual(status, 200)
        self.assertTrue(scored["is_correct"])
        status, scored_retry = _request(
            self.port, "POST", "/sessions/HTTP-1/answer", payload
        )
        self.assertEqual(status, 200)
        self.assertEqual(scored_retry, scored)

        # 跨操作复用 request_id -> 409
        status, reused = _request(self.port, "POST",
                                  "/sessions/HTTP-1/next",
                                  {"request_id": "a1"})
        self.assertEqual(status, 409)

    def test_void_and_explain(self):
        _request(self.port, "POST", "/sessions", {
            "session_id": "HTTP-2", "learner_id": "L-9",
            "graph_entity_id": GRAPH_ID, "bank_entity_id": BANK_ID,
            "rule_set_id": RULES_ID,
            "graph_revision": 1, "bank_revision": 1, "rule_revision": 1,
        })
        picked = {}
        for n in range(1, 13):
            _, picked = _request(self.port, "POST",
                                 "/sessions/HTTP-2/next",
                                 {"request_id": f"n{n}"})
            ans = build_bank_v1().get(picked["item_id"]).answer
            _, scored = _request(self.port, "POST",
                                 "/sessions/HTTP-2/answer",
                                 {"request_id": f"a{n}",
                                  "token": picked["token"], "answer": ans})
            if scored.get("finished"):
                break
        self.assertTrue(scored["finished"])

        first_item = "I-002"
        status, voided = _request(self.port, "POST",
                                  "/sessions/HTTP-2/void", {
                                      "request_id": "v1",
                                      "item_id": first_item,
                                      "teacher_id": "T-WANG",
                                      "reason": "考场录音异常",
                                  })
        self.assertEqual(status, 200)
        self.assertEqual(voided["regrade"]["item_count"],
                         scored["result"]["item_count"] - 1)

        # 作废请求重试不产生第二次重评
        status, voided_retry = _request(
            self.port, "POST", "/sessions/HTTP-2/void", {
                "request_id": "v1", "item_id": first_item,
                "teacher_id": "T-WANG", "reason": "考场录音异常",
            }
        )
        self.assertEqual(status, 200)
        _, explained = _request(self.port, "GET",
                                "/sessions/HTTP-2/explain")
        self.assertEqual(len(explained["result_history"]), 1)
        self.assertEqual(explained["items"][0]["voided_by"], "T-WANG")

    def test_unknown_session_404(self):
        status, body = _request(self.port, "GET", "/sessions/NOPE")
        self.assertEqual(status, 404)
        self.assertIn("NotFoundError", body["error"])


class AdminRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.harness = HttpServerHarness(self.tmp).__enter__()
        self.port = self.harness.port

    def tearDown(self):
        self.harness.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_duplicate_revision_conflicts(self):
        status, _ = _request(
            self.port, "POST", "/admin/graphs",
            graph_to_dict(build_graph_v1()),
        )
        # 夹具已登记 rev1，再登记同修订号 -> 409
        self.assertEqual(status, 409)


if __name__ == "__main__":
    unittest.main()
