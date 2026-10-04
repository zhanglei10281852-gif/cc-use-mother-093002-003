"""能力图谱与诊断题目的基础契约。

历史模块，类型定义已迁移至 :mod:`adaptive_exam.models`；此处保留再导出
以兼容旧导入路径，并登记题目与题库版本之间的关联记录。
"""
from dataclasses import dataclass

from .models import SkillGraphVersion  # noqa: F401  (再导出)


@dataclass(frozen=True)
class ExamItemRecord:
    """题目与题库实体之间的关联登记记录。"""

    record_id: str
    entity_id: str
    category: str

    def __post_init__(self) -> None:
        if not self.record_id or not self.entity_id or not self.category:
            raise ValueError("关联记录信息不完整")
