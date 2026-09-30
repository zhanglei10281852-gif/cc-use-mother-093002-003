import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
from adaptive_exam.contracts import SkillGraphVersion, ExamItemRecord

entity = SkillGraphVersion("E-DEMO", "自适应中文能力诊断编排", 1)
record = ExamItemRecord("R-DEMO", entity.entity_id, "已登记")
print(json.dumps({"entity": entity.display_name, "revision": entity.revision, "record_state": record.category}, ensure_ascii=False))
