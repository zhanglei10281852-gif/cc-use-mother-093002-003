"""命令行冒烟：跑一次完整自适应会话（含重试、作废、重评），打印关键结果。"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from adaptive_exam.catalog import load_catalog
from adaptive_exam.persistence import ExposureRepository, SessionRepository
from adaptive_exam.service import ExamService

catalog = load_catalog(Path(__file__).parent / "examples" / "catalog_seed.json")

with tempfile.TemporaryDirectory() as tmp:
    service = ExamService(
        catalog=catalog,
        sessions=SessionRepository(Path(tmp) / "sessions"),
        exposures=ExposureRepository(Path(tmp) / "exposures.json"),
    )
    session = service.start_session("GRAPH-CN:1", "BANK-CN:1", "RULES-CN:1")

    for step in range(1, 7):
        view = service.next_item(session.session_id, f"req-{step}")
        # 模拟同一请求重试：不应重复扣减曝光。
        replay = service.next_item(session.session_id, f"req-{step}")
        assert replay.item_id == view.item_id and replay.idempotent_replay
        answer = "x"  # 冒烟脚本全部答错，观察降难度与缺口传播
        service.submit_answer(session.session_id, view.item_id, answer, f"tok-{step}")

    result = service.finish(session.session_id, force=True)
    report = service.teacher_report(session.session_id)

print(
    json.dumps(
        {
            "首题": report["selections"][0]["item_id"],
            "分数": result.score,
            "等级": result.level,
            "直接缺口": [s for s, kind in result.gaps.items() if kind == "direct"],
            "推断缺口": [s for s, kind in result.gaps.items() if kind == "inferred"],
            "版本绑定": result.version_refs,
        },
        ensure_ascii=False,
        indent=2,
    )
)
