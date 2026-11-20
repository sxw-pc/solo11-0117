import json
from .store import connect

def report(path):
    db=connect(path)
    try:
        names=["source_blob","source_failure","voyage","fishing_permit","permit_species","catch_crate","species_inspection","scale_calibration","weigh_ticket","weigh_adjudication","quota_account","transfer_authority","quota_transfer","release_decision","release_event","crate_custody","crate_handover","pallet_pack","pallet_pack_crate","pallet_split","pallet_split_part","pallet_merge","pallet_merge_part","pallet_handover","crate_recheck","pallet_state","crate_whereabouts","evidence_conflict","evidence_conflict_crate"]
        data={name:[dict(row) for row in db.execute("SELECT * FROM "+name)] for name in names}
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
        conflicts={row["id"]:row for row in data["evidence_conflict"]}
        affected={}
        for link in data["evidence_conflict_crate"]:
            affected.setdefault(link["crate_id"],[]).append(link["conflict_id"])
        data["whereabouts"]=[{
            "crate":w["crate_id"],
            "definite_vessel":w["definite_vessel"],
            "possible_vessels":json.loads(w["possible_vessels"]),
            "possible_pallets":json.loads(w["possible_pallets"]),
            "handover_sequence":json.loads(w["handover_sequence"]),
            "port_receipt":{"issuable":bool(w["receipt_ok"]),
                            "vessel":w["definite_vessel"] if w["receipt_ok"] else None},
            "custody_certificate":{"issuable":bool(w["custody_ok"]),
                                   "hold_reasons":json.loads(w["hold_reasons"])},
            "conflicts":[{"id":c["id"],"kind":c["kind"],"event_id":c["event_id"],"detail":c["detail"]}
                         for c in (conflicts[i] for i in affected.get(w["crate_id"],[]))],
        } for w in data["crate_whereabouts"]]
        return data
    finally:db.close()

def report_json(path):
    return json.dumps(report(path),ensure_ascii=False,indent=2,default=lambda x:x.hex() if isinstance(x,bytes) else str(x))
