import json
from pathlib import Path
import pytest
from landingweigh.domain import trip_import,weigh_import,quota_import,inspect_import,weigh_adjudicate,quota_transfer
from landingweigh.report import report
from landingweigh.__main__ import demo

F=Path(__file__).parents[1]/"fixtures"
def test_full_landing(tmp_path):
    db=tmp_path/"db.sqlite"
    assert trip_import(db,F/"trip_normal.json")["crates"]==2
    assert weigh_import(db,F/"weigh_normal.json")["trusted"]==2
    assert quota_import(db,F/"quota_normal.json")["released"]==2
    assert {r["crate_id"]:r["status"] for r in report(db)["release_decision"]}=={"C1":"released","C2":"released"}
    assert quota_import(db,F/"quota_normal.json")["released"]==2
    assert len(report(db)["release_decision"])==2

def test_cross_vessel_identity_conflict_preserves_state(tmp_path):
    db=tmp_path/"db.sqlite";trip_import(db,F/"trip_normal.json")
    with pytest.raises(ValueError,match="crate_identity_conflict"):
        trip_import(db,F/"trip_conflict.json")
    assert len(report(db)["voyage"])==1
    assert report(db)["source_failure"][0]["error"]=="crate_identity_conflict"

def test_reweigh_dispute_and_quota_shortage_change_release(tmp_path):
    db=tmp_path/"db.sqlite";trip_import(db,F/"trip_normal.json")
    weigh_import(db,F/"weigh_normal.json");weigh_import(db,F/"weigh_conflict.json")
    quota_import(db,F/"quota_short.json")
    result={r["crate_id"]:r["reason"] for r in report(db)["release_decision"]}
    assert result=={"C1":"weigh_dispute","C2":"released"}

def test_quota_short_without_dispute(tmp_path):
    db=tmp_path/"db.sqlite";trip_import(db,F/"trip_normal.json");weigh_import(db,F/"weigh_normal.json")
    quota_import(db,F/"quota_short.json")
    assert {r["crate_id"]:r["reason"] for r in report(db)["release_decision"]}["C1"]=="quota_short"

def test_demo(tmp_path):
    assert len(demo(tmp_path)["release"])==2

def test_late_reweigh_reverses_release_and_restores_quota(tmp_path):
    db=tmp_path/"db.sqlite";trip_import(db,F/"trip_normal.json")
    weigh_import(db,F/"weigh_normal.json");quota_import(db,F/"quota_normal.json")
    assert {r["crate_id"]:r["status"] for r in report(db)["release_decision"]}["C1"]=="released"
    weigh_import(db,F/"weigh_conflict.json")
    state=report(db)
    assert {r["crate_id"]:r["reason"] for r in state["release_decision"]}["C1"]=="weigh_dispute"
    assert next(r for r in state["quota_account"] if r["species"]=="cod")["used"]=="0"
    assert len([r for r in state["release_event"] if r["crate_id"]=="C1"])==2

def test_calibration_version_and_ticket_identity_are_frozen(tmp_path):
    db=tmp_path/"db.sqlite";trip_import(db,F/"trip_normal.json")
    file=tmp_path/"revised.json"
    payload=json.loads((F/"weigh_normal.json").read_text())
    payload["calibrations"].append({"scale":"W1","revision":2,"start":"2026-05-01T00:00:00+00:00",
                                   "end":"2026-12-01T00:00:00+00:00","tolerance":"0.5"})
    file.write_text(json.dumps(payload),encoding="utf-8")
    weigh_import(db,file)
    assert {r["id"]:r["calibration_revision"] for r in report(db)["weigh_ticket"]}=={"T1":2,"T2":2}
    payload["calibrations"]=[];payload["tickets"][0]["gross"]="103"
    file.write_text(json.dumps(payload),encoding="utf-8")
    with pytest.raises(ValueError,match="ticket_identity_conflict"):
        weigh_import(db,file)

def test_arrival_window_and_bad_net_block_downstream(tmp_path):
    db=tmp_path/"db.sqlite"
    trip_file=tmp_path/"late.json"
    trip_file.write_text(json.dumps({"voyages":[{"id":"V3","vessel":"SEA-3","permit":"P-3",
        "depart":"2026-06-03T00:00:00+00:00","arrive":"2026-06-02T00:00:00+00:00"}],
        "crates":[{"id":"C3","voyage":"V3","species":"cod","seal":"S3"}]}),encoding="utf-8")
    assert trip_import(db,trip_file)["held"]==2
    weigh_file=tmp_path/"bad.json"
    weigh_file.write_text(json.dumps({"calibrations":[],"tickets":[{"id":"T3","crate":"C3","scale":"missing",
        "at":"2026-06-02T01:00:00+00:00","gross":"1","tare":"2"}]}),encoding="utf-8")
    assert weigh_import(db,weigh_file)["disputed"]==1
    quota_file=tmp_path/"quota.json"
    quota_file.write_text(json.dumps({"quotas":[{"vessel":"SEA-3","species":"cod","capacity":"100"}]}),encoding="utf-8")
    assert quota_import(db,quota_file)["held"]==1
    assert report(db)["release_decision"][0]["reason"]=="crate_held"

def test_voyage_identity_cannot_be_rewritten_by_new_source(tmp_path):
    db=tmp_path/"db.sqlite";trip_import(db,F/"trip_normal.json")
    changed=json.loads((F/"trip_normal.json").read_text())
    changed["voyages"][0]["vessel"]="SEA-OTHER"
    file=tmp_path/"changed.json";file.write_text(json.dumps(changed),encoding="utf-8")
    with pytest.raises(ValueError,match="voyage_identity_conflict"):
        trip_import(db,file)
    assert report(db)["voyage"][0]["vessel"]=="SEA-1"

def _input(tmp_path,name,payload):
    file=tmp_path/name;file.write_text(json.dumps(payload),encoding="utf-8");return file

def test_species_inspection_moves_weight_between_quota_accounts(tmp_path):
    db=tmp_path/"db.sqlite";trip_import(db,F/"trip_normal.json")
    weigh_import(db,F/"weigh_normal.json");quota_import(db,F/"quota_normal.json")
    inspection=_input(tmp_path,"inspect.json",{"inspections":[{"id":"I1","crate":"C1","observed_species":"hake",
        "action":"accept_observed","at":"2026-06-02T02:00:00+00:00","reason":"physical gill count"}]})
    assert inspect_import(db,inspection)["accepted"]==1
    state=report(db)
    assert next(c for c in state["catch_crate"] if c["id"]=="C1")["verified_species"]=="hake"
    assert next(q for q in state["quota_account"] if q["vessel"]=="SEA-1" and q["species"]=="cod")["used"]=="0"
    assert next(d for d in state["release_decision"] if d["crate_id"]=="C1")["reason"]=="quota_short"
    transfer=_input(tmp_path,"transfer.json",{"transfers":[{"id":"X1","from":"SEA-2","to":"SEA-1",
        "species":"hake","amount":"100","authorizer":"quota-officer"}]})
    assert quota_transfer(db,transfer)["transferred"]==1
    assert next(d for d in report(db)["release_decision"] if d["crate_id"]=="C1")["status"]=="released"
    assert len([e for e in report(db)["release_event"] if e["crate_id"]=="C1"])==3

def test_unlicensed_observed_species_holds_crate_and_reverses_release(tmp_path):
    db=tmp_path/"db.sqlite";trip_import(db,F/"trip_normal.json")
    weigh_import(db,F/"weigh_normal.json");quota_import(db,F/"quota_normal.json")
    assert inspect_import(db,F/"inspect_conflict.json")["held"]==1
    state=report(db)
    assert next(c for c in state["catch_crate"] if c["id"]=="C1")["status"]=="held"
    assert next(d for d in state["release_decision"] if d["crate_id"]=="C1")["reason"]=="crate_held"

def test_quota_transfer_authority_and_unused_balance_are_enforced(tmp_path):
    db=tmp_path/"db.sqlite";trip_import(db,F/"trip_normal.json");weigh_import(db,F/"weigh_normal.json")
    quota_import(db,F/"quota_normal.json")
    bad=_input(tmp_path,"bad-transfer.json",{"transfers":[{"id":"X2","from":"SEA-2","to":"SEA-1",
        "species":"hake","amount":"20","authorizer":"unknown"}]})
    with pytest.raises(ValueError,match="transfer_not_authorized"):quota_transfer(db,bad)
    assert report(db)["quota_transfer"]==[]
    assert report(db)["source_failure"][-1]["error"]=="transfer_not_authorized"

def test_adjudicated_ticket_releases_dispute_but_later_ticket_stales_decision(tmp_path):
    db=tmp_path/"db.sqlite";trip_import(db,F/"trip_normal.json");weigh_import(db,F/"weigh_normal.json")
    quota_import(db,F/"quota_normal.json");weigh_import(db,F/"weigh_conflict.json")
    decision=_input(tmp_path,"adjudicate.json",{"decisions":[{"id":"A1","crate":"C1","ticket":"T1",
        "reason":"sealed crate photo matches first weighment"}]})
    assert weigh_adjudicate(db,decision)["settled"]==1
    assert next(d for d in report(db)["release_decision"] if d["crate_id"]=="C1")["status"]=="released"
    late=_input(tmp_path,"late.json",{"calibrations":[],"tickets":[{"id":"T4","crate":"C1","scale":"W1",
        "at":"2026-06-02T03:00:00+00:00","gross":"102","tare":"2"}]})
    weigh_import(db,late)
    assert next(d for d in report(db)["release_decision"] if d["crate_id"]=="C1")["reason"]=="adjudication_stale"
