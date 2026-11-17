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
        for permit in data.get("permits",[]):
            if _time(permit["start"])>=_time(permit["end"]):raise ValueError("invalid_permit_window")
            old=db.execute("SELECT * FROM fishing_permit WHERE id=?",(permit["id"],)).fetchone()
            values=(permit["vessel"],permit["start"],permit["end"])
            species=sorted(set(permit["species"]))
            if old:
                prior=[r[0] for r in db.execute("SELECT species FROM permit_species WHERE permit_id=? ORDER BY species",(permit["id"],))]
                if tuple(old[k] for k in ("vessel","start_at","end_at"))!=values or prior!=species:
                    raise ValueError("permit_identity_conflict")
            else:
                db.execute("INSERT INTO fishing_permit VALUES (?,?,?,?,?)",(permit["id"],*values,source))
                db.executemany("INSERT INTO permit_species VALUES (?,?)",[(permit["id"],s) for s in species])
        for item in data["voyages"]:
            permit=db.execute("SELECT * FROM fishing_permit WHERE id=?",(item["permit"],)).fetchone()
            status = "arrived" if (permit and permit["vessel"]==item["vessel"] and
                                  _time(permit["start_at"])<=_time(item["arrive"])<_time(permit["end_at"]) and
                                  _time(item["depart"]) < _time(item["arrive"])) else "held"
            values=(item["vessel"],item["permit"],item["depart"],item["arrive"],status)
            old=db.execute("SELECT * FROM voyage WHERE id=?",(item["id"],)).fetchone()
            if _same(old,("vessel","permit","depart","arrive","status"),values,"voyage_identity_conflict"):
                continue
            db.execute("INSERT INTO voyage VALUES (?,?,?,?,?,?,?)",(item["id"],*values,source))
            counts["voyages"]+=1
            counts["held"]+=status=="held"
        for item in data["crates"]:
            voyage=db.execute("SELECT * FROM voyage WHERE id=?",(item["voyage"],)).fetchone()
            authorized=bool(voyage and db.execute("SELECT 1 FROM permit_species WHERE permit_id=? AND species=?",
                                                   (voyage["permit"],item["species"])).fetchone())
            status="eligible" if voyage and voyage["status"]=="arrived" and item["seal"] and authorized else "held"
            values=(item["voyage"],item["species"],item["species"],item["seal"],status)
            old=db.execute("SELECT * FROM catch_crate WHERE id=?",(item["id"],)).fetchone()
            if old and (old["voyage_id"],old["species"],old["seal"])!=(item["voyage"],item["species"],item["seal"]):
                raise ValueError("crate_identity_conflict")
            if old:
                continue
            db.execute("INSERT INTO catch_crate VALUES (?,?,?,?,?,?,?)",(item["id"],*values,source))
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
        key=(crate["vessel"],crate["verified_species"])
        account=accounts.get(key)
        adjudication=db.execute("SELECT * FROM weigh_adjudication WHERE crate_id=? ORDER BY rowid DESC LIMIT 1",(crate["id"],)).fetchone()
        selected=next((t for t in tickets if adjudication and t["id"]==adjudication["selected_ticket"]),None)
        settled=bool(selected and adjudication["ticket_count"]==len(tickets) and selected["calibration_revision"] is not None and Decimal(selected["net"])>0)
        chosen=selected if settled else trusted[-1] if trusted else None
        weight=Decimal(chosen["net"]) if chosen else Decimal(0)
        reason=("crate_held" if crate["status"]!="eligible" else "adjudication_stale" if adjudication and not settled else
                "weigh_dispute" if not settled and (len(trusted)!=len(tickets) or not trusted) else
                "quota_missing" if not account else "quota_short" if used[key]+weight>Decimal(account["capacity"]) else "released")
        status="released" if reason=="released" else "held"
        if status=="released":used[key]+=weight
        old=db.execute("SELECT * FROM release_decision WHERE crate_id=?",(crate["id"],)).fetchone()
        values=(status,str(weight),reason,chosen["id"] if chosen else None)
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
            old=db.execute("SELECT base_capacity FROM quota_account WHERE vessel=? AND species=?",(q["vessel"],q["species"])).fetchone()
            if old and Decimal(old["base_capacity"])!=capacity:
                raise ValueError("quota_contract_changed")
            db.execute("INSERT OR IGNORE INTO quota_account VALUES (?,?,?,?,?,?)",(q["vessel"],q["species"],str(capacity),str(capacity),"0",source))
        for authority in data.get("authorities",[]):
            limit=_money(authority["max_amount"])
            old=db.execute("SELECT max_amount FROM transfer_authority WHERE vessel=? AND species=? AND authorizer=?",
                           (authority["vessel"],authority["species"],authority["authorizer"])).fetchone()
            if old and Decimal(old["max_amount"])!=limit:raise ValueError("transfer_authority_changed")
            db.execute("INSERT OR IGNORE INTO transfer_authority VALUES (?,?,?,?,?)",
                       (authority["vessel"],authority["species"],authority["authorizer"],str(limit),source))
        return recompute_release(db,source)
    return ingest(path,"quota",file,handler)

def inspect_import(path,file):
    """A physical species check can redirect the quota account or hold the crate."""
    def handler(db,data,source):
        result={"accepted":0,"held":0}
        for entry in data["inspections"]:
            crate=db.execute("SELECT c.*,v.permit,v.arrive FROM catch_crate c JOIN voyage v ON v.id=c.voyage_id WHERE c.id=?",
                             (entry["crate"],)).fetchone()
            if not crate or _time(entry["at"])<_time(crate["arrive"]):raise ValueError("inspection_before_landing")
            if entry["action"] not in {"accept_observed","hold_dispute"} or not entry["reason"].strip():
                raise ValueError("invalid_inspection_action")
            old=db.execute("SELECT * FROM species_inspection WHERE id=?",(entry["id"],)).fetchone()
            values=(entry["crate"],entry["observed_species"],entry["action"],entry["at"],entry["reason"])
            if old:
                if tuple(old[k] for k in ("crate_id","observed_species","action","inspected_at","reason"))!=values:
                    raise ValueError("inspection_identity_conflict")
                continue
            authorized=bool(db.execute("SELECT 1 FROM permit_species WHERE permit_id=? AND species=?",
                                       (crate["permit"],entry["observed_species"])).fetchone())
            status="eligible" if entry["action"]=="accept_observed" and authorized else "held"
            db.execute("INSERT INTO species_inspection VALUES (?,?,?,?,?,?,?)",(entry["id"],*values,source))
            db.execute("UPDATE catch_crate SET verified_species=?,status=? WHERE id=?",
                       (entry["observed_species"],status,entry["crate"]))
            result["accepted" if status=="eligible" else "held"]+=1
        if db.execute("SELECT 1 FROM quota_account LIMIT 1").fetchone():recompute_release(db,source)
        return result
    return ingest(path,"inspection",file,handler)

def weigh_adjudicate(path,file):
    """Select an evidence-backed ticket after conflicting physical reweighs."""
    def handler(db,data,source):
        result={"settled":0}
        for item in data["decisions"]:
            tickets=db.execute("SELECT * FROM weigh_ticket WHERE crate_id=? ORDER BY at,id",(item["crate"],)).fetchall()
            ticket=next((t for t in tickets if t["id"]==item["ticket"]),None)
            if len(tickets)<2 or not ticket or ticket["calibration_revision"] is None or Decimal(ticket["net"])<=0:
                raise ValueError("unverifiable_selected_ticket")
            if not item["reason"].strip():raise ValueError("adjudication_reason_required")
            old=db.execute("SELECT * FROM weigh_adjudication WHERE id=?",(item["id"],)).fetchone()
            values=(item["crate"],item["ticket"],len(tickets),item["reason"])
            if old and tuple(old[k] for k in ("crate_id","selected_ticket","ticket_count","reason"))!=values:
                raise ValueError("adjudication_identity_conflict")
            if not old:
                db.execute("INSERT INTO weigh_adjudication VALUES (?,?,?,?,?,?)",(item["id"],*values,source))
                result["settled"]+=1
        if db.execute("SELECT 1 FROM quota_account LIMIT 1").fetchone():recompute_release(db,source)
        return result
    return ingest(path,"adjudication",file,handler)

def quota_transfer(path,file):
    """Reassign unused species allowance between licensed vessels."""
    def handler(db,data,source):
        result={"transferred":0}
        for item in data["transfers"]:
            amount=_money(item["amount"])
            if amount<=0 or item["from"]==item["to"] or not item["authorizer"].strip():
                raise ValueError("invalid_transfer")
            old=db.execute("SELECT * FROM quota_transfer WHERE id=?",(item["id"],)).fetchone()
            values=(item["from"],item["to"],item["species"],str(amount),item["authorizer"])
            if old:
                if tuple(old[k] for k in ("from_vessel","to_vessel","species","amount","authorizer"))!=values:
                    raise ValueError("transfer_identity_conflict")
                continue
            donor=db.execute("SELECT * FROM quota_account WHERE vessel=? AND species=?",(item["from"],item["species"])).fetchone()
            receiver=db.execute("SELECT * FROM quota_account WHERE vessel=? AND species=?",(item["to"],item["species"])).fetchone()
            authority=db.execute("SELECT max_amount FROM transfer_authority WHERE vessel=? AND species=? AND authorizer=?",
                                 (item["from"],item["species"],item["authorizer"])).fetchone()
            if not authority or amount>Decimal(authority["max_amount"]):raise ValueError("transfer_not_authorized")
            if not donor or not receiver or Decimal(donor["capacity"])-amount<Decimal(donor["used"]):
                raise ValueError("transfer_exceeds_unused_allowance")
            db.execute("UPDATE quota_account SET capacity=? WHERE vessel=? AND species=?",
                       (str(Decimal(donor["capacity"])-amount),item["from"],item["species"]))
            db.execute("UPDATE quota_account SET capacity=? WHERE vessel=? AND species=?",
                       (str(Decimal(receiver["capacity"])+amount),item["to"],item["species"]))
            db.execute("INSERT INTO quota_transfer VALUES (?,?,?,?,?,?,?)",(item["id"],*values,source))
            result["transferred"]+=1
        recompute_release(db,source)
        return result
    return ingest(path,"transfer",file,handler)
