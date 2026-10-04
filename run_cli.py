"""命令行入口：端到端演示、内容播种与 HTTP 服务。

用法：

    python3 run_cli.py demo               # 临时目录跑完整场景
    python3 run_cli.py seed --root data   # 向数据目录登记演示内容
    python3 run_cli.py serve --root data  # 启动 HTTP 后端
"""
import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from adaptive_exam.fixtures import (  # noqa: E402
    BANK_ID,
    GRAPH_ID,
    RULES_ID,
    DeterministicClock,
    build_bank_v1,
    build_graph_v1,
    build_rules_v1,
)
from adaptive_exam.services import ExamService  # noqa: E402


def seed(service: ExamService) -> None:
    service.register_graph(build_graph_v1())
    service.register_bank(build_bank_v1())
    service.register_rules(build_rules_v1())


def _emit(title: str, payload) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def run_demo(root: str | None = None) -> None:
    tmp = root or tempfile.mkdtemp(prefix="adaptive-exam-demo-")
    service = ExamService(tmp, clock=DeterministicClock())
    seed(service)
    print(f"数据目录：{tmp}")

    service.start_session(
        "DEMO-1", "学员-甲", GRAPH_ID, BANK_ID, RULES_ID,
        graph_revision=1, bank_revision=1, rule_revision=1,
    )

    first = service.request_next_item("DEMO-1", "req-sel-1")
    _emit("首次选题（冷启动，难度最接近入门档）", {
        "item_id": first["item_id"], "token": first["token"],
        "reason": first["selection_reason"],
    })

    retry = service.request_next_item("DEMO-1", "req-sel-1")
    _emit("同一请求重试：同一题、曝光不重复扣减", {
        "item_id": retry["item_id"], "retried": retry["retried"],
    })

    bank = build_bank_v1()
    answer = service.submit_answer(
        "DEMO-1", "req-ans-1", first["token"],
        bank.get(first["item_id"]).answer,
    )
    _emit("判分与单题贡献", {
        "item_id": answer["item_id"], "is_correct": answer["is_correct"],
        "contribution": answer["contribution"],
    })

    # 模拟网络中断：下一题选出后未作答，新请求被要求凭原标识续考。
    picked2 = service.request_next_item("DEMO-1", "req-sel-2")
    try:
        service.request_next_item("DEMO-1", "req-sel-2-new-connection")
    except Exception as exc:  # noqa: BLE001 - 演示需要打印
        _emit("断网后换新请求标识被拒", {"error": type(exc).__name__,
                                      "message": str(exc),
                                      "resume_request_id": "req-sel-2"})
    resumed = service.request_next_item("DEMO-1", "req-sel-2")
    _emit("凭原请求标识续考成功", {"item_id": resumed["item_id"],
                                 "same_token": resumed["token"]
                                 == picked2["token"]})

    # 连续作答直到自动/主动结束。
    n = 2
    while True:
        item = bank.get(resumed["item_id"])
        scored = service.submit_answer(
            "DEMO-1", f"req-ans-{n}", resumed["token"], item.answer
        )
        if scored.get("finished"):
            break
        nxt = service.request_next_item("DEMO-1", f"req-sel-{n + 1}")
        n += 1
        resumed = nxt
        if n > 12:
            service.finish_session("DEMO-1", "req-finish-1")
            break

    session = service.get_session("DEMO-1")
    _emit("结束结果（等级绑定当时规则版本）", session["result"])

    first_item = service.explain_session("DEMO-1")["items"][0]["item_id"]
    voided = service.void_evidence(
        "DEMO-1", first_item, "教师-林老师",
        "演示：开场设备故障，按证据作废首题", request_id="req-void-1"
    )
    _emit("教师作废异常作答并触发受控重评", voided)

    explanation = service.explain_session("DEMO-1")
    _emit("教师视角解释（节选：前 2 题依据 + 审计尾部）", {
        "pinned_versions": explanation["pinned_versions"],
        "items": [
            {k: it[k] for k in ("order", "item_id", "status",
                                "selection_reason", "contribution",
                                "voided_by")}
            for it in explanation["items"][:2]
        ],
        "audit_tail": explanation["audit_log"][-3:],
        "result_history_len": len(explanation["result_history"]),
    })


def main() -> None:
    parser = argparse.ArgumentParser(description="自适应中文测评后端")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo")
    seed_p = sub.add_parser("seed")
    seed_p.add_argument("--root", required=True)
    serve_p = sub.add_parser("serve")
    serve_p.add_argument("--root", required=True)
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    if args.cmd == "demo":
        run_demo()
    elif args.cmd == "seed":
        service = ExamService(args.root)
        seed(service)
        print(f"演示内容已登记到 {args.root}")
    elif args.cmd == "serve":
        from adaptive_exam.api import serve
        serve(args.root, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
