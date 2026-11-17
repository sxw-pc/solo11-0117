import json
from pathlib import Path
import pytest
from landingweigh.domain import trip_import,weigh_import,quota_import
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
