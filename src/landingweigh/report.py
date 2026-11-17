import json
from .store import connect

def report(path):
    db=connect(path)
    try:
        names=["source_blob","voyage","catch_crate","scale_calibration","weigh_ticket","quota_account","release_decision","release_event"]
        return {name:[dict(row) for row in db.execute("SELECT * FROM "+name)] for name in names}
    finally:db.close()

def report_json(path):
    return json.dumps(report(path),ensure_ascii=False,indent=2,default=lambda x:x.hex() if isinstance(x,bytes) else str(x))
