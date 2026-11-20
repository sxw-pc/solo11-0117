import json
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from .intake import ingest

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
US = 1_000_000  # microseconds per second

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

# ---- clock evidence -------------------------------------------------------
# A device offset o is the signed seconds such that UTC = device_reading + o.
# An anchor bounds one device's offset relative to UTC; a constraint bounds the
# difference o_a - o_b between two devices at a calibration moment. All times
# are converted to integer microseconds so interval arithmetic stays exact.

def _us(value):
    delta = _time(value).astimezone(timezone.utc) - EPOCH
    return (delta.days * 86400 + delta.seconds) * US + delta.microseconds

def _iso(us):
    return (EPOCH + timedelta(microseconds=us)).isoformat()

def _canon_num(value):
    return format(Decimal(str(value)).normalize(), "f")

def _offset_us(text):
    return int((Decimal(text) * US).to_integral_value())

def _fmt_offset(us):
    return None if us is None else format(Decimal(us) / US, "f")

def _fmt_ts(base, offset):
    return None if offset is None else _iso(base + offset)

def _feasible(nodes, edges):
    """True unless the difference constraints o_dst - o_src <= w contain a
    negative cycle (i.e. the clock evidence contradicts itself)."""
    dist = {n: 0 for n in nodes}
    for i in range(len(nodes)):
        relaxed = False
        for s, d, w in edges:
            if dist[s] + w < dist[d]:
                if i == len(nodes) - 1:
                    return False
                dist[d] = dist[s] + w
                relaxed = True
        if not relaxed:
            break
    return True

def _shortest_paths(nodes, edges, source):
    dist = {n: None for n in nodes}
    dist[source] = 0
    for _ in range(len(nodes) - 1):
        updated = False
        for s, d, w in edges:
            if dist[s] is not None and (dist[d] is None or dist[s] + w < dist[d]):
                dist[d] = dist[s] + w
                updated = True
        if not updated:
            break
    return dist

def _parse_sheet(sheet):
    """Validate one clock sheet: credentialed issuer, <=4 devices, <=6
    constraints, at least one signed UTC anchor."""
    try:
        if not isinstance(sheet, dict):
            raise ValueError
        sid, vid = sheet["id"], sheet["voyage"]
        issuer, issued = sheet["issuer"], sheet["issued_at"]
        if not isinstance(issuer, str) or not issuer.strip():
            raise ValueError
        _time(issued)
        anchors = sheet.get("anchors", [])
        constraints = sheet.get("constraints", [])
        if not anchors or len(constraints) > 6:
            raise ValueError
        devices, norm_anchors, seen = set(), [], set()
        for a in anchors:
            dev = a["device"]
            if not isinstance(dev, str) or not dev or dev == "UTC" or dev in seen:
                raise ValueError
            seen.add(dev)
            devices.add(dev)
            lo, hi = _canon_num(a["lower"]), _canon_num(a["upper"])
            if Decimal(lo) > Decimal(hi):
                raise ValueError
            norm_anchors.append((dev, lo, hi))
        norm_constraints = []
        for c in constraints:
            a, b = c["a"], c["b"]
            if not isinstance(a, str) or not isinstance(b, str) or not a or not b \
               or a == b or a == "UTC" or b == "UTC":
                raise ValueError
            _time(c["at"])
            lo, hi = _canon_num(c["lower"]), _canon_num(c["upper"])
            if Decimal(lo) > Decimal(hi):
                raise ValueError
            devices |= {a, b}
            norm_constraints.append((a, b, c["at"], lo, hi))
        if len(devices) > 4:
            raise ValueError
    except (KeyError, TypeError, ValueError, ArithmeticError):
        raise ValueError("invalid_clock_sheet")
    return {"id": sid, "voyage": vid, "issuer": issuer.strip(), "issued_at": issued,
            "anchors": sorted(norm_anchors), "constraints": norm_constraints}

def _sheet_edges(sheet):
    edges, devices = [], set()
    for dev, lo, hi in sheet["anchors"]:
        edges += [("UTC", dev, _offset_us(hi)), (dev, "UTC", -_offset_us(lo))]
        devices.add(dev)
    for a, b, _at, lo, hi in sheet["constraints"]:
        edges += [(a, b, _offset_us(hi)), (b, a, -_offset_us(lo))]
        devices |= {a, b}
    return edges, devices

def _insert_sheet(db, sheet, status, reason, source):
    db.execute("INSERT INTO clock_sheet VALUES (?,?,?,?,?,?,?)",
               (sheet["id"], sheet["voyage"], sheet["issuer"], sheet["issued_at"], status, reason, source))
    db.executemany("INSERT INTO clock_anchor VALUES (?,?,?,?)",
                   [(sheet["id"], d, lo, hi) for d, lo, hi in sheet["anchors"]])
    db.executemany("INSERT INTO clock_constraint VALUES (?,?,?,?,?,?,?)",
                   [(sheet["id"], i, a, b, at, lo, hi)
                    for i, (a, b, at, lo, hi) in enumerate(sheet["constraints"])])

def _accepted_edges(db, vid):
    edges, devices = [], set()
    for sheet in db.execute("SELECT * FROM clock_sheet WHERE voyage=? AND status='accepted'", (vid,)).fetchall():
        for a in db.execute("SELECT * FROM clock_anchor WHERE sheet_id=?", (sheet["id"],)).fetchall():
            edges += [("UTC", a["device"], _offset_us(a["offset_upper"])),
                      (a["device"], "UTC", -_offset_us(a["offset_lower"]))]
            devices.add(a["device"])
        for c in db.execute("SELECT * FROM clock_constraint WHERE sheet_id=?", (sheet["id"],)).fetchall():
            edges += [(c["device_a"], c["device_b"], _offset_us(c["diff_upper"])),
                      (c["device_b"], c["device_a"], -_offset_us(c["diff_lower"]))]
            devices |= {c["device_a"], c["device_b"]}
    return edges, devices

def _evaluate_voyage(db, voyage, hint=False):
    """Three-way verdict under one shared clock interpretation per device:
    'arrived' when every feasible offset assignment satisfies the permit
    window, 'held' when none does, otherwise 'pending'."""
    permit = db.execute("SELECT * FROM fishing_permit WHERE id=?", (voyage["permit"],)).fetchone()
    sheet_mode = hint or db.execute("SELECT 1 FROM clock_sheet WHERE voyage=? LIMIT 1", (voyage["id"],)).fetchone()
    if not permit or permit["vessel"] != voyage["vessel"]:
        return "held", ({"reason": "permit_mismatch"} if sheet_mode else None)
    if not sheet_mode:
        ok = (_time(permit["start_at"]) <= _time(voyage["arrive"]) < _time(permit["end_at"])
              and _time(voyage["depart"]) < _time(voyage["arrive"]))
        return ("arrived" if ok else "held"), None
    depart_device, arrive_device = voyage["depart_device"], voyage["arrive_device"]
    if not depart_device or not arrive_device:
        raise ValueError("voyage_device_required")
    edges, devices = _accepted_edges(db, voyage["id"])
    devices |= {depart_device, arrive_device}
    nodes = ["UTC"] + sorted(devices)
    if not _feasible(nodes, edges):
        return "held", {"reason": "clock_evidence_infeasible"}
    dist = {n: _shortest_paths(nodes, edges, n) for n in nodes}
    def hi(d):
        return dist["UTC"][d]
    def lo(d):
        v = dist[d]["UTC"]
        return None if v is None else -v
    dep_us, arr_us = _us(voyage["depart"]), _us(voyage["arrive"])
    events = {"depart": (depart_device, dep_us), "arrive": (arrive_device, arr_us)}
    start_dev, start_reading = events[permit["start_event"]]
    end_dev, end_reading = events[permit["end_event"]]
    # condition 0: depart_utc < arrive_utc  <=>  o_dep - o_arr <= order_bound
    order_bound = arr_us - dep_us - 1
    forward, backward = dist[arrive_device][depart_device], dist[depart_device][arrive_device]
    order_entailed = forward is not None and forward <= order_bound
    order_possible = backward is None or -backward <= order_bound
    # condition 1: event_utc >= permit start  <=>  o_start_dev >= start_bound
    start_bound = _us(permit["start_at"]) - start_reading
    start_entailed = lo(start_dev) is not None and lo(start_dev) >= start_bound
    start_possible = hi(start_dev) is None or hi(start_dev) >= start_bound
    # condition 2: event_utc < permit end  <=>  o_end_dev <= end_bound
    end_bound = _us(permit["end_at"]) - end_reading - 1
    end_entailed = hi(end_dev) is not None and hi(end_dev) <= end_bound
    end_possible = lo(end_dev) is None or lo(end_dev) <= end_bound
    joint = _feasible(nodes, edges + [(arrive_device, depart_device, order_bound),
                                      (start_dev, "UTC", -start_bound),
                                      ("UTC", end_dev, end_bound)])
    entailed = order_entailed and start_entailed and end_entailed
    status = "arrived" if entailed else "pending" if joint else "held"
    reason = {"arrived": "window_satisfied", "pending": "window_unresolved",
              "held": "window_unsatisfiable"}[status]
    detail = {"reason": reason,
              "permit_events": {"start": permit["start_event"], "end": permit["end_event"]},
              "offset_ranges": {d: [_fmt_offset(lo(d)), _fmt_offset(hi(d))] for d in sorted(devices)},
              "event_utc": {"depart": [_fmt_ts(dep_us, lo(depart_device)), _fmt_ts(dep_us, hi(depart_device))],
                            "arrive": [_fmt_ts(arr_us, lo(arrive_device)), _fmt_ts(arr_us, hi(arrive_device))]},
              "conditions": [{"name": "depart_before_arrive", "entailed": order_entailed, "possible": order_possible},
                             {"name": "permit_start", "entailed": start_entailed, "possible": start_possible},
                             {"name": "permit_end", "entailed": end_entailed, "possible": end_possible}],
              "jointly_satisfiable": joint}
    return status, detail

def _refresh_voyage(db, vid, source):
    row = db.execute("SELECT * FROM voyage WHERE id=?", (vid,)).fetchone()
    status, detail = _evaluate_voyage(db, row)
    detail_json = json.dumps(detail, ensure_ascii=False) if detail else None
    if status == row["status"] and detail_json == row["clock_detail"]:
        return False
    db.execute("UPDATE voyage SET status=?,clock_detail=? WHERE id=?", (status, detail_json, vid))
    if status != row["status"]:
        db.execute("INSERT INTO voyage_status_event(voyage_id,status,reason,source_id) VALUES (?,?,?,?)",
                   (vid, status, (detail or {}).get("reason"), source))
        return True
    return False

def _refresh_crates(db, vid):
    voyage = db.execute("SELECT * FROM voyage WHERE id=?", (vid,)).fetchone()
    for crate in db.execute("SELECT * FROM catch_crate WHERE voyage_id=?", (vid,)).fetchall():
        inspection = db.execute("SELECT action FROM species_inspection WHERE crate_id=? "
                                "ORDER BY inspected_at DESC,rowid DESC LIMIT 1", (crate["id"],)).fetchone()
        blocked = bool(inspection and inspection["action"] == "hold_dispute")
        authorized = bool(db.execute("SELECT 1 FROM permit_species WHERE permit_id=? AND species=?",
                                     (voyage["permit"], crate["verified_species"])).fetchone())
        status = "eligible" if (voyage["status"] == "arrived" and crate["seal"]
                                and authorized and not blocked) else "held"
        if status != crate["status"]:
            db.execute("UPDATE catch_crate SET status=? WHERE id=?", (status, crate["id"]))

def trip_import(path,file):
    def handler(db,data,source):
        counts={"voyages":0,"crates":0,"held":0,"pending":0,"clock_sheets":0,"quarantined":0}
        for permit in data.get("permits",[]):
            if _time(permit["start"])>=_time(permit["end"]):raise ValueError("invalid_permit_window")
            start_event=permit.get("start_event","arrive");end_event=permit.get("end_event","arrive")
            if start_event not in ("depart","arrive") or end_event not in ("depart","arrive"):
                raise ValueError("invalid_permit_event")
            old=db.execute("SELECT * FROM fishing_permit WHERE id=?",(permit["id"],)).fetchone()
            values=(permit["vessel"],permit["start"],permit["end"],start_event,end_event)
            species=sorted(set(permit["species"]))
            if old:
                prior=[r[0] for r in db.execute("SELECT species FROM permit_species WHERE permit_id=? ORDER BY species",(permit["id"],))]
                if tuple(old[k] for k in ("vessel","start_at","end_at","start_event","end_event"))!=values or prior!=species:
                    raise ValueError("permit_identity_conflict")
            else:
                db.execute("INSERT INTO fishing_permit(id,vessel,start_at,end_at,start_event,end_event,source_id) VALUES (?,?,?,?,?,?,?)",
                           (permit["id"],*values,source))
                db.executemany("INSERT INTO permit_species VALUES (?,?)",[(permit["id"],s) for s in species])
        sheet_hint={s.get("voyage") for s in data.get("clock_sheets",[]) if isinstance(s,dict)}
        sheet_hint.discard(None)
        new_voyages=[]
        for item in data.get("voyages",[]):
            record={"id":item["id"],"vessel":item["vessel"],"permit":item["permit"],"depart":item["depart"],
                    "arrive":item["arrive"],"depart_device":item.get("depart_device"),
                    "arrive_device":item.get("arrive_device")}
            old=db.execute("SELECT * FROM voyage WHERE id=?",(item["id"],)).fetchone()
            if old:
                values=(item["vessel"],item["permit"],item["depart"],item["arrive"],
                        item.get("depart_device"),item.get("arrive_device"))
                if tuple(old[k] for k in ("vessel","permit","depart","arrive","depart_device","arrive_device"))!=values:
                    raise ValueError("voyage_identity_conflict")
                continue
            status,detail=_evaluate_voyage(db,record,hint=item["id"] in sheet_hint)
            detail_json=json.dumps(detail,ensure_ascii=False) if detail else None
            db.execute("INSERT INTO voyage(id,vessel,permit,depart,arrive,status,depart_device,arrive_device,clock_detail,source_id) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (item["id"],item["vessel"],item["permit"],item["depart"],item["arrive"],status,
                        item.get("depart_device"),item.get("arrive_device"),detail_json,source))
            db.execute("INSERT INTO voyage_status_event(voyage_id,status,reason,source_id) VALUES (?,?,?,?)",
                       (item["id"],status,(detail or {}).get("reason"),source))
            new_voyages.append(item["id"])
            counts["voyages"]+=1
        touched=set(sheet_hint)
        batches={}
        for raw in data.get("clock_sheets",[]):
            sheet=_parse_sheet(raw)
            voyage=db.execute("SELECT * FROM voyage WHERE id=?",(sheet["voyage"],)).fetchone()
            if not voyage:raise ValueError("clock_sheet_voyage_unknown")
            if not voyage["depart_device"] or not voyage["arrive_device"]:raise ValueError("voyage_device_required")
            old=db.execute("SELECT * FROM clock_sheet WHERE id=?",(sheet["id"],)).fetchone()
            if old:
                anchors=sorted((r["device"],r["offset_lower"],r["offset_upper"]) for r in
                               db.execute("SELECT * FROM clock_anchor WHERE sheet_id=?",(sheet["id"],)))
                constraints=sorted((r["device_a"],r["device_b"],r["checked_at"],r["diff_lower"],r["diff_upper"]) for r in
                                   db.execute("SELECT * FROM clock_constraint WHERE sheet_id=?",(sheet["id"],)))
                same=(old["voyage"],old["issuer"],old["issued_at"])==(sheet["voyage"],sheet["issuer"],sheet["issued_at"]) \
                     and anchors==sheet["anchors"] and constraints==sorted(sheet["constraints"])
                if not same:raise ValueError("clock_sheet_identity_conflict")
                continue
            batches.setdefault(sheet["voyage"],[]).append(sheet)
        for vid,batch in batches.items():
            viable=[]
            for sheet in batch:
                edges,devices=_sheet_edges(sheet)
                if _feasible(["UTC"]+sorted(devices),edges):
                    viable.append(sheet)
                else:
                    _insert_sheet(db,sheet,"quarantined","constraint_contradiction",source)
                    counts["quarantined"]+=1
            if viable:
                prior_edges,prior_devices=_accepted_edges(db,vid)
                batch_edges=[];batch_devices=set()
                for sheet in viable:
                    edges,devices=_sheet_edges(sheet)
                    batch_edges+=edges;batch_devices|=devices
                if not _feasible(["UTC"]+sorted(batch_devices),batch_edges):
                    for sheet in viable:_insert_sheet(db,sheet,"quarantined","constraint_contradiction",source)
                    counts["quarantined"]+=len(viable)
                elif not _feasible(["UTC"]+sorted(batch_devices|prior_devices),prior_edges+batch_edges):
                    for sheet in viable:_insert_sheet(db,sheet,"quarantined","evidence_contradiction",source)
                    counts["quarantined"]+=len(viable)
                else:
                    for sheet in viable:_insert_sheet(db,sheet,"accepted",None,source)
                    counts["clock_sheets"]+=len(viable)
        changed=False
        for vid in touched:
            changed=_refresh_voyage(db,vid,source) or changed
        for vid in new_voyages:
            final=db.execute("SELECT status FROM voyage WHERE id=?",(vid,)).fetchone()[0]
            counts["held"]+=final=="held"
            counts["pending"]+=final=="pending"
        for item in data.get("crates",[]):
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
        if changed:
            for vid in touched:_refresh_crates(db,vid)
            if db.execute("SELECT 1 FROM weigh_ticket LIMIT 1").fetchone():_refresh_weigh(db)
            if db.execute("SELECT 1 FROM quota_account LIMIT 1").fetchone():recompute_release(db,source)
        return counts
    return ingest(path,"trip",file,handler)

def _evaluate_ticket(db,crate,scale,at,gross,tare):
    candidates=[c for c in db.execute("SELECT * FROM scale_calibration WHERE scale=?",(scale,))
                if _time(c["start_at"])<=at<_time(c["end_at"])]
    cal=max(candidates,key=lambda c:c["revision"]) if candidates else None
    reason=("crate_not_eligible" if not crate or crate["status"]!="eligible" else
            "calibration_missing" if not cal else "invalid_net" if gross<=tare else "trusted")
    return cal,reason

def _apply_reweigh_gap(db):
    for crate in db.execute("SELECT id FROM catch_crate"):
        tickets=db.execute("SELECT * FROM weigh_ticket WHERE crate_id=? ORDER BY at,id",(crate["id"],)).fetchall()
        valid=[t for t in tickets if t["reason"]=="trusted"]
        if len(valid)>1:
            tolerance=min(Decimal(db.execute("SELECT tolerance FROM scale_calibration WHERE scale=? AND revision=?",
                              (t["scale"],t["calibration_revision"])).fetchone()[0]) for t in valid)
            weights=[Decimal(t["net"]) for t in valid]
            if max(weights)-min(weights)>tolerance:
                db.executemany("UPDATE weigh_ticket SET status='disputed',reason='reweigh_gap' WHERE id=?",[(t["id"],) for t in valid])

def _refresh_weigh(db):
    """Re-derive ticket status/reason from current crate and calibration state.
    Ticket identity and source provenance are never rewritten."""
    for t in db.execute("SELECT * FROM weigh_ticket").fetchall():
        crate=db.execute("SELECT * FROM catch_crate WHERE id=?",(t["crate_id"],)).fetchone()
        gross,tare=Decimal(t["gross"]),Decimal(t["tare"])
        cal,reason=_evaluate_ticket(db,crate,t["scale"],_time(t["at"]),gross,tare)
        status="trusted" if reason=="trusted" else "disputed"
        net=gross-tare if gross>tare else Decimal(0)
        revision=cal["revision"] if cal else None
        if (status,reason,revision,str(net))!=(t["status"],t["reason"],t["calibration_revision"],t["net"]):
            db.execute("UPDATE weigh_ticket SET status=?,reason=?,calibration_revision=?,net=? WHERE id=?",
                       (status,reason,revision,str(net),t["id"]))
    _apply_reweigh_gap(db)

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
            cal,reason=_evaluate_ticket(db,crate,t["scale"],at,gross,tare)
            status="trusted" if reason=="trusted" else "disputed"
            net=gross-tare if gross>tare else Decimal(0)
            db.execute("INSERT INTO weigh_ticket VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (t["id"],t["crate"],t["scale"],t["at"],str(gross),str(tare),str(net),cal["revision"] if cal else None,status,reason,source))
            counts["tickets"]+=1
        _apply_reweigh_gap(db)
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
            crate=db.execute("SELECT c.*,v.permit,v.arrive,v.status AS voyage_status FROM catch_crate c JOIN voyage v ON v.id=c.voyage_id WHERE c.id=?",
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
            status="eligible" if entry["action"]=="accept_observed" and authorized and crate["voyage_status"]=="arrived" else "held"
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
