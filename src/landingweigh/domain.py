import json
from decimal import Decimal
from datetime import datetime
from .intake import ingest

def _time(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timezone_required")
    return parsed

def _money(value):
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise ValueError("invalid_weight")
    return number

def _same(old, fields, values, error):
    if old and tuple(old[x] for x in fields) != tuple(values):
        raise ValueError(error)
    return bool(old)

def trip_import(path,file):
    def handler(db,data,source):
        counts={"voyages":0,"crates":0,"held":0}
        for item in data["voyages"]:
            status = "arrived" if item["permit"] and _time(item["depart"]) < _time(item["arrive"]) else "held"
            values=(item["vessel"],item["permit"],item["depart"],item["arrive"],status)
            old=db.execute("SELECT * FROM voyage WHERE id=?",(item["id"],)).fetchone()
            if _same(old,("vessel","permit","depart","arrive","status"),values,"voyage_identity_conflict"):
                continue
            db.execute("INSERT INTO voyage VALUES (?,?,?,?,?,?,?)",(item["id"],*values,source))
            counts["voyages"]+=1
            counts["held"]+=status=="held"
        for item in data["crates"]:
            voyage=db.execute("SELECT status FROM voyage WHERE id=?",(item["voyage"],)).fetchone()
            status="eligible" if voyage and voyage["status"]=="arrived" and item["seal"] else "held"
            values=(item["voyage"],item["species"],item["seal"],status)
            old=db.execute("SELECT * FROM catch_crate WHERE id=?",(item["id"],)).fetchone()
            if _same(old,("voyage_id","species","seal","status"),values,"crate_identity_conflict"):
                continue
            db.execute("INSERT INTO catch_crate VALUES (?,?,?,?,?,?)",(item["id"],*values,source))
            counts["crates"]+=1
            counts["held"]+=status=="held"
        return counts
    return ingest(path,"trip",file,handler)

def weigh_import(path,file):
    def handler(db,data,source):
        counts={"tickets":0,"trusted":0,"disputed":0}
        for c in data.get("calibrations",[]):
            if _time(c["start"]) >= _time(c["end"]):
                raise ValueError("invalid_calibration_window")
            values=(c["start"],c["end"],str(_money(c["tolerance"])))
            old=db.execute("SELECT * FROM scale_calibration WHERE scale=? AND revision=?",(c["scale"],c["revision"])).fetchone()
            if _same(old,("start_at","end_at","tolerance"),values,"calibration_identity_conflict"):
                continue
            db.execute("INSERT INTO scale_calibration VALUES (?,?,?,?,?,?)",(c["scale"],c["revision"],*values,source))
        for t in data["tickets"]:
            old=db.execute("SELECT * FROM weigh_ticket WHERE id=?",(t["id"],)).fetchone()
            if old:
                if any(str(old[k]) != str(v) for k,v in [("crate_id",t["crate"]),("scale",t["scale"]),("at",t["at"]),("gross",t["gross"]),("tare",t["tare"])]):
                    raise ValueError("ticket_identity_conflict")
                continue
            crate=db.execute("SELECT * FROM catch_crate WHERE id=?",(t["crate"],)).fetchone()
            gross,tare=_money(t["gross"]),_money(t["tare"])
            at=_time(t["at"])
            candidates=[c for c in db.execute("SELECT * FROM scale_calibration WHERE scale=?",(t["scale"],))
                        if _time(c["start_at"])<=at<_time(c["end_at"])]
            cal=max(candidates,key=lambda c:c["revision"]) if candidates else None
            reason=("crate_not_eligible" if not crate or crate["status"]!="eligible" else
                    "calibration_missing" if not cal else "invalid_net" if gross<=tare else "trusted")
            status="trusted" if reason=="trusted" else "disputed"
            net=gross-tare if gross>tare else Decimal(0)
            db.execute("INSERT INTO weigh_ticket VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (t["id"],t["crate"],t["scale"],t["at"],str(gross),str(tare),str(net),cal["revision"] if cal else None,status,reason,source))
            counts["tickets"]+=1
        for crate in db.execute("SELECT id FROM catch_crate"):
            tickets=db.execute("SELECT * FROM weigh_ticket WHERE crate_id=? ORDER BY at,id",(crate["id"],)).fetchall()
            valid=[t for t in tickets if t["reason"]=="trusted"]
            if len(valid)>1:
                tolerance=min(Decimal(db.execute("SELECT tolerance FROM scale_calibration WHERE scale=? AND revision=?",
                                  (t["scale"],t["calibration_revision"])).fetchone()[0]) for t in valid)
                weights=[Decimal(t["net"]) for t in valid]
                if max(weights)-min(weights)>tolerance:
                    db.executemany("UPDATE weigh_ticket SET status='disputed',reason='reweigh_gap' WHERE id=?",[(t["id"],) for t in valid])
        counts["trusted"]=db.execute("SELECT COUNT(*) FROM weigh_ticket WHERE status='trusted'").fetchone()[0]
        counts["disputed"]=db.execute("SELECT COUNT(*) FROM weigh_ticket WHERE status='disputed'").fetchone()[0]
        if db.execute("SELECT 1 FROM quota_account LIMIT 1").fetchone():
            recompute_release(db,source)
        return counts
    return ingest(path,"weigh",file,handler)

def recompute_release(db,source):
    accounts={(r["vessel"],r["species"]):r for r in db.execute("SELECT * FROM quota_account")}
    used={key:Decimal(0) for key in accounts}
    result={"released":0,"held":0}
    for crate in db.execute("SELECT c.*,v.vessel FROM catch_crate c JOIN voyage v ON v.id=c.voyage_id ORDER BY c.id").fetchall():
        tickets=db.execute("SELECT * FROM weigh_ticket WHERE crate_id=? ORDER BY at,id",(crate["id"],)).fetchall()
        trusted=[t for t in tickets if t["status"]=="trusted"]
        key=(crate["vessel"],crate["species"])
        account=accounts.get(key)
        weight=Decimal(trusted[-1]["net"]) if trusted else Decimal(0)
        reason=("crate_held" if crate["status"]!="eligible" else "weigh_dispute" if len(trusted)!=len(tickets) or not trusted else
                "quota_missing" if not account else "quota_short" if used[key]+weight>Decimal(account["capacity"]) else "released")
        status="released" if reason=="released" else "held"
        if status=="released":used[key]+=weight
        old=db.execute("SELECT * FROM release_decision WHERE crate_id=?",(crate["id"],)).fetchone()
        values=(status,str(weight),reason,trusted[-1]["id"] if trusted else None)
        if not old or tuple(old[k] for k in ("status","weight","reason","ticket_id"))!=values:
            db.execute("INSERT INTO release_decision(crate_id,status,weight,reason,ticket_id,source_id) VALUES (?,?,?,?,?,?) "
                       "ON CONFLICT(crate_id) DO UPDATE SET status=excluded.status,weight=excluded.weight,reason=excluded.reason,ticket_id=excluded.ticket_id,source_id=excluded.source_id",
                       (crate["id"],*values,source))
            db.execute("INSERT INTO release_event(crate_id,status,weight,reason,source_id) VALUES (?,?,?,?,?)",
                       (crate["id"],status,str(weight),reason,source))
        result[status]+=1
    for key,amount in used.items():
        db.execute("UPDATE quota_account SET used=? WHERE vessel=? AND species=?",(str(amount),*key))
    return result

def quota_import(path,file):
    def handler(db,data,source):
        for q in data["quotas"]:
            capacity=_money(q["capacity"])
            old=db.execute("SELECT capacity FROM quota_account WHERE vessel=? AND species=?",(q["vessel"],q["species"])).fetchone()
            if old and Decimal(old["capacity"])!=capacity:
                raise ValueError("quota_contract_changed")
            db.execute("INSERT OR IGNORE INTO quota_account VALUES (?,?,?,?,?)",(q["vessel"],q["species"],str(capacity),"0",source))
        return recompute_release(db,source)
    return ingest(path,"quota",file,handler)
