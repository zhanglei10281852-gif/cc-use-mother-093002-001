import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
from joint_program.contracts import AgreementVersion, ObligationRecord

entity = AgreementVersion("E-DEMO", "国际中文联合培养履约协同", 1)
record = ObligationRecord("R-DEMO", entity.entity_id, "已登记")
print(json.dumps({"entity": entity.display_name, "revision": entity.revision, "record_state": record.category}, ensure_ascii=False))
