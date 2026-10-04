"""标准库 HTTP 适配层。

路由：
- POST /sessions                       开始会话
- POST /sessions/{id}/next             取下一题（body: {"request_id": ...}，重试幂等）
- POST /sessions/{id}/answers          提交作答（answer_token 幂等）
- POST /sessions/{id}/invalidate       教师按证据作废异常作答
- POST /sessions/{id}/finish           结测（结果绑定冻结版本）
- POST /sessions/{id}/regrade          受控重评
- GET  /sessions/{id}/report           逐题选择依据与作答贡献
- GET  /healthz                        存活与版本目录概览

服务每次操作都从磁盘加载会话，因此多个进程/重启共享同一数据目录时行为一致。
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

from .catalog import VersionCatalog
from .persistence import (
    ExposureRepository,
    IdempotencyConflict,
    SessionRepository,
)
from .service import ExamService, NextItemView
from .session import Session, SessionError


class ApiContext:
    def __init__(
        self,
        catalog: VersionCatalog,
        session_dir: str,
        exposure_path: str,
    ):
        self.lock = threading.Lock()
        self.catalog = catalog

        def build_service() -> ExamService:
            return ExamService(
                catalog=catalog,
                sessions=SessionRepository(session_dir),
                exposures=ExposureRepository(exposure_path),
            )

        self.build_service = build_service


def _next_view_dict(view: NextItemView) -> dict[str, Any]:
    data = asdict(view)
    data["skill_ids"] = list(view.skill_ids)
    if view.stop_reason is not None:
        data["stopped"] = True
    return data


def _session_summary(session: Session) -> dict[str, Any]:
    result = session.current_result()
    return {
        "session_id": session.session_id,
        "learner_id": session.learner_id,
        "status": session.status,
        "version_refs": session.version_refs,
        "presented": len(session.selections),
        "confirmed": len(session.confirmed_responses),
        "current_level": result.level if result else None,
        "current_score": result.score if result else None,
    }


def create_handler(context: ApiContext) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "AdaptiveExam/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # 安静日志
            return

        # ---- 基础收发 ----

        def _send_json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise SessionError(f"请求体不是合法 JSON: {exc}") from exc
            if not isinstance(data, dict):
                raise SessionError("请求体必须是 JSON 对象")
            return data

        def _error(self, exc: Exception) -> None:
            if isinstance(exc, KeyError):
                status, code = 404, "not_found"
            elif isinstance(exc, IdempotencyConflict):
                status, code = 409, "idempotency_conflict"
            elif isinstance(exc, SessionError | ValueError):
                status, code = 400, "invalid_request"
            else:  # pragma: no cover - 兜底
                status, code = 500, "internal_error"
            self._send_json(
                status,
                {"error": {"code": code, "message": str(exc)}},
            )

        # ---- 路由 ----

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            try:
                if path == "/healthz":
                    with context.lock:
                        service = context.build_service()
                        payload = {
                            "status": "ok",
                            "graph_versions": sorted(service.catalog.graphs),
                            "bank_versions": sorted(service.catalog.banks),
                            "rules_versions": sorted(service.catalog.rules),
                        }
                    self._send_json(200, payload)
                    return
                if path.startswith("/sessions/") and path.endswith("/report"):
                    session_id = path.split("/")[2]
                    with context.lock:
                        report = context.build_service().teacher_report(session_id)
                    self._send_json(200, report)
                    return
                self._send_json(404, {"error": {"code": "not_found", "message": path}})
            except Exception as exc:  # noqa: BLE001
                self._error(exc)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            try:
                body = self._read_json()
                with context.lock:
                    self._route_post(path, body)
            except Exception as exc:  # noqa: BLE001
                self._error(exc)

        def _route_post(self, path: str, body: dict[str, Any]) -> None:
            service = context.build_service()
            parts = [p for p in path.split("/") if p]

            if parts == ["sessions"]:
                session = service.start_session(
                    graph_qvid=body["graph_qvid"],
                    bank_qvid=body["bank_qvid"],
                    rules_qvid=body["rules_qvid"],
                    learner_id=body.get("learner_id"),
                    session_id=body.get("session_id"),
                )
                self._send_json(201, _session_summary(session))
                return

            if len(parts) == 3 and parts[0] == "sessions":
                session_id, action = parts[1], parts[2]
                if action == "next":
                    request_id = body.get("request_id")
                    if not request_id:
                        raise SessionError("next 必须携带 request_id 以支持重试幂等")
                    view = service.next_item(session_id, request_id)
                    self._send_json(200, _next_view_dict(view))
                    return
                if action == "answers":
                    result = service.submit_answer(
                        session_id=session_id,
                        item_id=body["item_id"],
                        answer=body["answer"],
                        answer_token=body.get("answer_token"),
                    )
                    self._send_json(200, result)
                    return
                if action == "invalidate":
                    session = service.invalidate_response(
                        session_id=session_id,
                        item_id=body["item_id"],
                        teacher_id=body["teacher_id"],
                        reason=body["reason"],
                    )
                    self._send_json(200, _session_summary(session))
                    return
                if action == "finish":
                    result = service.finish(session_id, force=bool(body.get("force")))
                    self._send_json(200, _result_payload(result))
                    return
                if action == "regrade":
                    previous, latest = service.regrade(
                        session_id=session_id,
                        teacher_id=body["teacher_id"],
                        reason=body["reason"],
                    )
                    self._send_json(
                        200,
                        {
                            "previous": _result_payload(previous),
                            "latest": _result_payload(latest),
                        },
                    )
                    return
            self._send_json(404, {"error": {"code": "not_found", "message": path}})

    return Handler


def _result_payload(result: Any) -> dict[str, Any]:
    payload = asdict(result)
    payload["gap_chains"] = {k: list(v) for k, v in result.gap_chains.items()}
    return payload


def serve(
    catalog: VersionCatalog,
    session_dir: str,
    exposure_path: str,
    host: str = "127.0.0.1",
    port: int = 8080,
) -> ThreadingHTTPServer:
    context = ApiContext(catalog, session_dir, exposure_path)
    server = ThreadingHTTPServer((host, port), create_handler(context))
    return server
