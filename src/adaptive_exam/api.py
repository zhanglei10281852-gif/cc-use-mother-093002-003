"""基于标准库的 HTTP 适配层，无第三方依赖。

路由::

    POST /admin/graphs|banks|rules      登记版本化内容
    POST /sessions                      开考（钉住修订号）
    POST /sessions/{id}/next            下一题（body 带 request_id）
    POST /sessions/{id}/answer          提交作答（request_id + token）
    POST /sessions/{id}/finish          交卷
    POST /sessions/{id}/void            教师作废（幂等 request_id 可选）
    POST /sessions/{id}/regrade         受控重评
    GET  /sessions/{id}                 会话状态与结果
    GET  /sessions/{id}/explain         逐题依据、贡献与审计日志
    GET  /healthz

所有写操作的 request_id 放在请求体中；同请求重试返回首次结果。
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .codecs import (
    bank_from_dict,
    graph_from_dict,
    rules_from_dict,
)
from .errors import (
    AdaptiveExamError,
    IdempotencyError,
    NotFoundError,
    PendingSelectionError,
    RegradePreconditionError,
    SessionClosedError,
    ValidationError,
    VersionConflictError,
)
from .services import ExamService

_STATUS = {
    ValidationError: 400,
    IdempotencyError: 409,
    VersionConflictError: 409,
    SessionClosedError: 409,
    RegradePreconditionError: 409,
    NotFoundError: 404,
}


class _Handler(BaseHTTPRequestHandler):
    server_version = "AdaptiveExam/1.0"

    # 由 serve() 注入
    service: ExamService = None  # type: ignore[assignment]

    def log_message(self, fmt, *args):  # 静默默认访问日志
        return

    # -- 基础工具 --------------------------------------------------------

    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValidationError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return data

    def _handle(self, fn):
        try:
            fn()
        except PendingSelectionError as exc:
            self._send(409, {
                "error": "pending_selection",
                "message": str(exc),
                "request_id": exc.request_id,
                "token": exc.token,
                "item_id": exc.item_id,
            })
        except AdaptiveExamError as exc:
            status = 422
            for etype, code in _STATUS.items():
                if isinstance(exc, etype):
                    status = code
                    break
            self._send(status, {"error": type(exc).__name__,
                                "message": str(exc)})

    # -- 路由 ------------------------------------------------------------

    def do_GET(self):  # noqa: N802
        self._handle(self._route_get)

    def do_POST(self):  # noqa: N802
        self._handle(self._route_post)

    def _route_get(self) -> None:
        path = self.path.rstrip("/")
        if path == "/healthz":
            self._send(200, {"status": "ok"})
            return
        if path.startswith("/sessions/"):
            rest = path[len("/sessions/"):]
            if rest.endswith("/explain"):
                sid = rest[: -len("/explain")]
                self._send(200, self.service.explain_session(sid))
                return
            self._send(200, self.service.get_session(rest))
            return
        raise NotFoundError(f"未知路径：{self.path}")

    def _route_post(self) -> None:
        path = self.path.rstrip("/")
        body = self._read_json()

        if path == "/admin/graphs":
            result = self.service.register_graph(graph_from_dict(body))
            self._send(201, result)
            return
        if path == "/admin/banks":
            result = self.service.register_bank(bank_from_dict(body))
            self._send(201, result)
            return
        if path == "/admin/rules":
            result = self.service.register_rules(rules_from_dict(body))
            self._send(201, result)
            return
        if path == "/sessions":
            required = ("session_id", "learner_id", "graph_entity_id",
                        "bank_entity_id", "rule_set_id", "rule_revision")
            missing = [k for k in required if k not in body]
            if missing:
                raise ValidationError(f"缺少必填字段：{', '.join(missing)}")
            result = self.service.start_session(
                body["session_id"], body["learner_id"],
                body["graph_entity_id"], body["bank_entity_id"],
                body["rule_set_id"],
                graph_revision=body.get("graph_revision"),
                bank_revision=body.get("bank_revision"),
                rule_revision=body["rule_revision"],
            )
            self._send(201, result)
            return

        if path.startswith("/sessions/"):
            rest = path[len("/sessions/"):]
            sid, _, action = rest.partition("/")
            if not sid or not action:
                raise NotFoundError(f"未知路径：{self.path}")

            if action == "next":
                request_id = body.get("request_id")
                if not request_id:
                    raise ValidationError("缺少 request_id")
                self._send(200,
                           self.service.request_next_item(sid, request_id))
            elif action == "answer":
                self._send(200, self.service.submit_answer(
                    sid, body["request_id"], body["token"], body["answer"]
                ))
            elif action == "finish":
                self._send(200, self.service.finish_session(
                    sid, body["request_id"]
                ))
            elif action == "void":
                self._send(200, self.service.void_evidence(
                    sid, body["item_id"], body["teacher_id"],
                    body["reason"], body.get("request_id"),
                ))
            elif action == "regrade":
                self._send(200, self.service.regrade(
                    sid, body["teacher_id"], body["reason"],
                    body.get("request_id"),
                ))
            else:
                raise NotFoundError(f"未知操作：{action}")
            return
        raise NotFoundError(f"未知路径：{self.path}")


def serve(root: str, host: str = "127.0.0.1", port: int = 8080) -> None:
    """启动 HTTP 服务（阻塞）。"""
    service = ExamService(root)
    handler = type("BoundHandler", (_Handler,), {"service": service})
    httpd = ThreadingHTTPServer((host, port), handler)
    # ExamService 内部已有 RLock；此处仅提示线程模型。
    httpd.daemon_threads = True
    print(f"自适应中文测评后端监听 http://{host}:{port}（数据目录 {root}）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
