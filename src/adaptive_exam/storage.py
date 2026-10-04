"""基于 JSON 文件的持久化。

目录布局::

    root/
      graphs/<entity_id>.json        # 同一实体的全部图谱修订
      banks/<entity_id>.json         # 同一实体的全部题库修订
      rules/<rule_set_id>.json       # 同一规则集的全部修订
      exposures.json                 # 全局题目曝光台账（跨会话）
      sessions/<session_id>.json
      idem/<session_id>/<hash>.json  # 请求级幂等记录

所有写入均走“临时文件 + 原子替换”，进程在任意时刻崩溃都不会留下
半截 JSON；服务层另外以进程锁保证多线程下的临界区一致性。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from . import codecs
from .errors import NotFoundError, VersionConflictError
from .models import ItemBank, ScoringRuleSet, Session, SkillGraph


def _atomic_write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_json(path: Path):
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


class ContentRegistry:
    """版本化能力图谱、题库与规则集的登记处。"""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        (self.root / "graphs").mkdir(parents=True, exist_ok=True)
        (self.root / "banks").mkdir(parents=True, exist_ok=True)
        (self.root / "rules").mkdir(parents=True, exist_ok=True)

    # -- 能力图谱 ---------------------------------------------------------

    def add_graph(self, graph: SkillGraph) -> None:
        path = self.root / "graphs" / f"{graph.version.entity_id}.json"
        doc = _read_json(path) if path.exists() else {"versions": []}
        revisions = [v["version"]["revision"] for v in doc["versions"]]
        if graph.version.revision in revisions:
            raise VersionConflictError(
                f"图谱 {graph.version.entity_id} 修订 "
                f"{graph.version.revision} 已存在"
            )
        doc["versions"].append(codecs.graph_to_dict(graph))
        doc["versions"].sort(key=lambda d: d["version"]["revision"])
        _atomic_write_json(path, doc)

    def get_graph(self, entity_id: str, revision: int) -> SkillGraph:
        path = self.root / "graphs" / f"{entity_id}.json"
        if not path.exists():
            raise NotFoundError(f"图谱实体 {entity_id} 不存在")
        for d in _read_json(path)["versions"]:
            if d["version"]["revision"] == revision:
                return codecs.graph_from_dict(d)
        raise NotFoundError(f"图谱 {entity_id}:{revision} 不存在")

    def latest_graph(self, entity_id: str) -> SkillGraph:
        path = self.root / "graphs" / f"{entity_id}.json"
        if not path.exists() or not _read_json(path)["versions"]:
            raise NotFoundError(f"图谱实体 {entity_id} 没有任何版本")
        latest = max(_read_json(path)["versions"],
                     key=lambda d: d["version"]["revision"])
        return codecs.graph_from_dict(latest)

    # -- 题库 -------------------------------------------------------------

    def add_bank(self, bank: ItemBank) -> None:
        path = self.root / "banks" / f"{bank.version.entity_id}.json"
        doc = _read_json(path) if path.exists() else {"versions": []}
        revisions = [v["version"]["revision"] for v in doc["versions"]]
        if bank.version.revision in revisions:
            raise VersionConflictError(
                f"题库 {bank.version.entity_id} 修订 "
                f"{bank.version.revision} 已存在"
            )
        doc["versions"].append(codecs.bank_to_dict(bank))
        doc["versions"].sort(key=lambda d: d["version"]["revision"])
        _atomic_write_json(path, doc)

    def get_bank(self, entity_id: str, revision: int) -> ItemBank:
        path = self.root / "banks" / f"{entity_id}.json"
        if not path.exists():
            raise NotFoundError(f"题库实体 {entity_id} 不存在")
        for d in _read_json(path)["versions"]:
            if d["version"]["revision"] == revision:
                return codecs.bank_from_dict(d)
        raise NotFoundError(f"题库 {entity_id}:{revision} 不存在")

    def latest_bank(self, entity_id: str) -> ItemBank:
        path = self.root / "banks" / f"{entity_id}.json"
        if not path.exists() or not _read_json(path)["versions"]:
            raise NotFoundError(f"题库实体 {entity_id} 没有任何版本")
        latest = max(_read_json(path)["versions"],
                     key=lambda d: d["version"]["revision"])
        return codecs.bank_from_dict(latest)

    # -- 评分规则 ---------------------------------------------------------

    def add_rules(self, rules: ScoringRuleSet) -> None:
        path = self.root / "rules" / f"{rules.rule_set_id}.json"
        doc = _read_json(path) if path.exists() else {"revisions": []}
        revisions = [d["revision"] for d in doc["revisions"]]
        if rules.revision in revisions:
            raise VersionConflictError(
                f"规则集 {rules.rule_set_id} 修订 {rules.revision} 已存在"
            )
        doc["revisions"].append(codecs.rules_to_dict(rules))
        doc["revisions"].sort(key=lambda d: d["revision"])
        _atomic_write_json(path, doc)

    def get_rules(self, rule_set_id: str, revision: int) -> ScoringRuleSet:
        path = self.root / "rules" / f"{rule_set_id}.json"
        if not path.exists():
            raise NotFoundError(f"规则集 {rule_set_id} 不存在")
        for d in _read_json(path)["revisions"]:
            if d["revision"] == revision:
                return codecs.rules_from_dict(d)
        raise NotFoundError(f"规则集 {rule_set_id}:{revision} 不存在")


class ExposureLedger:
    """跨会话的题目曝光台账。

    落盘结构为 ``{"counts": {题: 次数}, "reservations": {请求: 题}}``。
    占用按请求标识幂等：同一请求崩溃后重试，绝不产生第二次扣减；
    这以“宁可少记一次、不可重复扣减”的方向覆盖崩溃窗口。
    """

    def __init__(self, root: str | Path):
        self.path = Path(root) / "exposures.json"

    def _load(self) -> dict:
        if not self.path.exists():
            return {"counts": {}, "reservations": {}}
        doc = _read_json(self.path)
        doc.setdefault("counts", {})
        doc.setdefault("reservations", {})
        doc["counts"] = {k: int(v) for k, v in doc["counts"].items()}
        return doc

    def counts(self) -> dict[str, int]:
        return dict(self._load()["counts"])

    def reserved_item_for(
        self, session_id: str, request_id: str
    ) -> str | None:
        return self._load()["reservations"].get(
            f"{session_id}|{request_id}"
        )

    def reserve(
        self, session_id: str, request_id: str, item_id: str, limit: int
    ) -> tuple[bool, str]:
        """幂等占用一次曝光。

        返回 ``(是否成功, 实际记账的题目)``：

        * 首次占用且未达上限：扣减并登记占用请求；
        * 同一会话的同一 ``request_id`` 重试：不重复扣减，返回首次题目；
        * 已达上限：返回 ``(False, item_id)``，不写任何记录。
        """
        doc = self._load()
        counts = doc["counts"]
        reservations = doc["reservations"]
        key = f"{session_id}|{request_id}"
        if key in reservations:
            return True, reservations[key]
        if counts.get(item_id, 0) >= limit:
            return False, item_id
        counts[item_id] = counts.get(item_id, 0) + 1
        reservations[key] = item_id
        _atomic_write_json(self.path, doc)
        return True, item_id


class SessionRepository:
    """会话与请求级幂等记录的存储。"""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        (self.root / "sessions").mkdir(parents=True, exist_ok=True)
        (self.root / "idem").mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        return self.root / "sessions" / f"{session_id}.json"

    def save(self, session: Session) -> None:
        _atomic_write_json(self._path(session.session_id),
                           codecs.session_to_dict(session))

    def get(self, session_id: str) -> Session:
        path = self._path(session_id)
        if not path.exists():
            raise NotFoundError(f"会话 {session_id} 不存在")
        return codecs.session_from_dict(_read_json(path))

    def exists(self, session_id: str) -> bool:
        return self._path(session_id).exists()

    def all_session_ids(self) -> list[str]:
        return sorted(p.stem for p in (self.root / "sessions").glob("*.json"))

    # -- 幂等记录 ---------------------------------------------------------

    def _idem_path(self, session_id: str, request_id: str) -> Path:
        digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        return self.root / "idem" / session_id / f"{digest}.json"

    def load_idempotency(
        self, session_id: str, request_id: str
    ) -> dict | None:
        path = self._idem_path(session_id, request_id)
        if not path.exists():
            return None
        return _read_json(path)

    def save_idempotency(
        self, session_id: str, request_id: str, op: str, response: dict
    ) -> None:
        _atomic_write_json(
            self._idem_path(session_id, request_id),
            {"request_id": request_id, "op": op, "response": response},
        )
