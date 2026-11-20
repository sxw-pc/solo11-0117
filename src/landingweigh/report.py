import json
from .store import connect

def report(path):
    db=connect(path)
    try:
        names=["source_blob","source_failure","voyage","fishing_permit","permit_species","catch_crate","species_inspection","scale_calibration","weigh_ticket","weigh_adjudication","quota_account","transfer_authority","quota_transfer","release_decision","release_event","voyage_status_event","clock_sheet","clock_anchor","clock_constraint"]
        data={name:[dict(row) for row in db.execute("SELECT * FROM "+name)] for name in names}
        data["voyage_clock"]=[{"voyage":v["id"],"status":v["status"],**json.loads(v["clock_detail"])}
                              for v in data["voyage"] if v.get("clock_detail")]
        data["chains"]=[dict(row) for row in db.execute("""
            SELECT c.id AS crate,c.species AS declared_species,c.verified_species,c.status AS crate_status,v.id AS voyage,v.vessel,v.status AS voyage_status,
                   t.id AS ticket,t.net,t.calibration_revision,t.status AS ticket_status,
                   q.capacity,q.used,d.status AS release_status,d.reason AS release_reason,
                   src.sha AS crate_source_sha
            FROM catch_crate c JOIN voyage v ON v.id=c.voyage_id
            JOIN source_blob src ON src.id=c.source_id
            LEFT JOIN weigh_ticket t ON t.id=(SELECT id FROM weigh_ticket WHERE crate_id=c.id ORDER BY at DESC,id DESC LIMIT 1)
            LEFT JOIN quota_account q ON q.vessel=v.vessel AND q.species=c.verified_species
            LEFT JOIN release_decision d ON d.crate_id=c.id ORDER BY c.id
        """)]
        return data
    finally:db.close()

def report_json(path):
    return json.dumps(report(path),ensure_ascii=False,indent=2,default=lambda x:x.hex() if isinstance(x,bytes) else str(x))
