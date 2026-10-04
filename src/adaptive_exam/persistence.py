"""文件持久化：会话仓库与全局曝光账本。

- 会话按 ID 单独 JSON 文件原子写入（tmp + rename），写一半崩溃也不会损坏旧状态。
- 曝光账本记录全系统累计投放次数与“请求ID -> 预留”映射；
  同一请求重试命中既有预留，不再扣减额度。
- 两类数据都落盘，进程重启后未结束会话与曝光计数均可恢复。
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .session import Session


@dataclass(frozen=True)
class Reservation:
    session_id: str
    request_id: str
    item_id: str
    seq: int


class IdempotencyConflict(ValueError):
    """同一 request_id 被用于不同的操作意图。"""


class ExposureExhausted(RuntimeError):
    """题目曝光额度已用尽。"""


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


class SessionRepository:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        return self.directory / f"{session_id}.json"

    def exists(self, session_id: str) -> bool:
        return self._path(session_id).exists()

    def save(self, session: Session) -> None:
        _atomic_write_json(self._path(session.session_id), session.to_dict())

    def load(self, session_id: str) -> Session:
        path = self._path(session_id)
        if not path.exists():
            raise KeyError(f"会话不存在: {session_id}")
        return Session.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list_ids(self) -> tuple[str, ...]:
        return tuple(sorted(p.stem for p in self.glob_sessions()))

    def glob_sessions(self):
        return sorted(self.directory.glob("*.json"))


@dataclass
class ExposureLedger:
    # 题目ID -> 全系统累计投放次数
    counts: dict[str, int] = field(default_factory=dict)
    # "会话ID:请求ID" -> 预留记录
    reservations: dict[str, Reservation] = field(default_factory=dict)

    def remaining(self, item_id: str, cap: int | None) -> int | None:
        if cap is None:
            return None
        return max(0, cap - self.counts.get(item_id, 0))


class ExposureRepository:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.ledger = self._load()

    def _load(self) -> ExposureLedger:
        if not self.path.exists():
            return ExposureLedger()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return ExposureLedger(
            counts=dict(data.get("counts", {})),
            reservations={
                key: Reservation(**row)
                for key, row in data.get("reservations", {}).items()
            },
        )

    def save(self) -> None:
        payload = {
            "counts": dict(sorted(self.ledger.counts.items())),
            "reservations": {
                key: asdict(reservation)
                for key, reservation in sorted(self.ledger.reservations.items())
            },
        }
        _atomic_write_json(self.path, payload)

    @staticmethod
    def _key(session_id: str, request_id: str) -> str:
        return f"{session_id}:{request_id}"

    def lookup(self, session_id: str, request_id: str) -> Reservation | None:
        return self.ledger.reservations.get(self._key(session_id, request_id))

    def reserve(
        self,
        session_id: str,
        request_id: str,
        item_id: str,
        cap: int | None,
    ) -> Reservation:
        """为一次“取下一题”预留一次曝光；同一请求重试幂等。"""
        key = self._key(session_id, request_id)
        existing = self.ledger.reservations.get(key)
        if existing is not None:
            if existing.item_id != item_id:
                raise IdempotencyConflict(
                    f"请求 {request_id} 已预留题目 {existing.item_id}，"
                    f"不能改用于 {item_id}"
                )
            return existing
        used = self.ledger.counts.get(item_id, 0)
        if cap is not None and used >= cap:
            raise ExposureExhausted(f"题目 {item_id} 曝光额度已用尽（上限 {cap}）")
        reservation = Reservation(
            session_id=session_id,
            request_id=request_id,
            item_id=item_id,
            seq=used + 1,
        )
        self.ledger.counts[item_id] = used + 1
        self.ledger.reservations[key] = reservation
        self.save()
        return reservation

    def remaining_map(self, caps: dict[str, int | None]) -> dict[str, int]:
        """供选题引擎过滤：只返回有上限题目的剩余次数。"""
        return {
            item_id: self.ledger.remaining(item_id, cap)
            for item_id, cap in caps.items()
            if cap is not None
        }
