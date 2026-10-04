"""版本标识、内容摘要与规范化序列化。

所有规则实体（能力图谱、题库、评分规则）都以
``实体ID:修订号`` 作为全局限定标识，并附带规范化载荷的 SHA-256 摘要。
会话只保存限定标识与摘要，从而把测评结果永久绑定到当时的规则版本。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .contracts import VersionInfo

GRAPH_KIND = "skill_graph"
BANK_KIND = "item_bank"
RULES_KIND = "grading_rules"


@dataclass(frozen=True)
class QualifiedVersionId:
    entity_id: str
    revision: int

    def __str__(self) -> str:
        return f"{self.entity_id}:{self.revision}"

    @classmethod
    def parse(cls, text: str) -> "QualifiedVersionId":
        if ":" not in text:
            raise ValueError(f"限定版本标识格式不合法: {text!r}")
        entity_id, _, raw_revision = text.rpartition(":")
        revision = int(raw_revision)
        return cls(entity_id, revision)


def qualified_id(version: VersionInfo) -> QualifiedVersionId:
    return QualifiedVersionId(version.entity_id, version.revision)


def qvid(version: VersionInfo) -> str:
    return str(qualified_id(version))


def canonical_json(payload: object) -> str:
    """用于摘要与持久化的规范化 JSON（键排序、无空白、保留中文）。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_digest(payload: object) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
