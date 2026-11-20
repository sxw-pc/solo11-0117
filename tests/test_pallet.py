import json
from pathlib import Path
import pytest
from landingweigh.domain import trip_import
from landingweigh.pallet import pallet_import
from landingweigh.report import report

FIX = Path(__file__).resolve().parents[1] / "fixtures"


def _whereabouts(db):
    return {w["crate"]: w for w in report(db)["whereabouts"]}


def test_split_leaves_routes_open_until_late_recheck(tmp_path):
    db = tmp_path / "state.db"
    trip_import(db, FIX / "trip_normal.json")
    result = pallet_import(db, FIX / "pallet_split.json")
    assert result == {"packed": 1, "split": 1, "merged": 0, "handovers": 2, "rechecks": 0, "conflicts": 0}
    wb = _whereabouts(db)
    for crate in ("C1", "C2"):
        # counts alone cannot assert C1 on SEA-2: both routes stay open
        assert wb[crate]["definite_vessel"] is None
        assert wb[crate]["possible_vessels"] == ["SEA-2", "SEA-3"]
        assert not wb[crate]["port_receipt"]["issuable"]
        assert not wb[crate]["custody_certificate"]["issuable"]
        assert "vessel_ambiguous" in wb[crate]["custody_certificate"]["hold_reasons"]
    # late per-crate recheck: SEA-2 sights C1's original seal on PL-X
    assert pallet_import(db, FIX / "pallet_recheck.json")["rechecks"] == 1
    wb = _whereabouts(db)
    assert wb["C1"]["definite_vessel"] == "SEA-2"
    assert wb["C1"]["handover_sequence"] == ["PH1"]
    assert wb["C2"]["definite_vessel"] == "SEA-3"
    assert wb["C2"]["handover_sequence"] == ["PH2"]
    for crate in ("C1", "C2"):
        assert wb[crate]["port_receipt"]["issuable"]
        assert wb[crate]["custody_certificate"]["issuable"]
    rows = report(db)
    # original voyage registration is untouched by pallet evidence
    assert {c["id"]: (c["voyage_id"], c["seal"]) for c in rows["catch_crate"]} == {
        "C1": ("V1", "S1"), "C2": ("V1", "S2")}
    assert rows["voyage"][0]["vessel"] == "SEA-1"
    # re-running the same inputs adds no handovers and changes nothing
    assert pallet_import(db, FIX / "pallet_split.json") == result
    assert pallet_import(db, FIX / "pallet_recheck.json")["rechecks"] == 1
    assert len(report(db)["pallet_handover"]) == 2
    assert report(db)["crate_whereabouts"] == rows["crate_whereabouts"]


def test_remerge_converges_both_crates_to_one_vessel(tmp_path):
    db = tmp_path / "state.db"
    trip_import(db, FIX / "trip_normal.json")
    result = pallet_import(db, FIX / "pallet_merge.json")
    assert result["merged"] == 1 and result["conflicts"] == 0
    wb = _whereabouts(db)
    for crate in ("C1", "C2"):
        assert wb[crate]["definite_vessel"] == "SEA-2"
        assert wb[crate]["possible_pallets"] == ["PL-Z"]
        assert wb[crate]["handover_sequence"] == ["PH1"]
        assert wb[crate]["port_receipt"]["issuable"]
        assert wb[crate]["custody_certificate"]["issuable"]
    pallets = {p["pallet_id"]: p for p in report(db)["pallet_state"]}
    assert json.loads(pallets["PL-Z"]["possible_contents"]) == [["C1", "C2"]]
    assert json.loads(pallets["PL-X"]["possible_contents"]) == [["C1"], ["C2"]]


def test_seal_break_holds_custody_certificate_but_not_port_receipt(tmp_path):
    db = tmp_path / "state.db"
    trip_import(db, FIX / "trip_normal.json")
    assert pallet_import(db, FIX / "pallet_seal_break.json")["conflicts"] == 1
    rows = report(db)
    conflict = rows["evidence_conflict"][0]
    assert conflict["kind"] == "seal_mismatch" and conflict["event_id"] == "PH1"
    # before the recheck either crate may sit on the broken pallet
    assert {r["crate_id"] for r in rows["evidence_conflict_crate"]} == {"C1", "C2"}
    pallet_import(db, FIX / "pallet_recheck.json")
    wb = _whereabouts(db)
    # the port receipt only proves definite arrival: issuable for both crates
    assert wb["C1"]["port_receipt"] == {"issuable": True, "vessel": "SEA-2"}
    assert wb["C2"]["port_receipt"] == {"issuable": True, "vessel": "SEA-3"}
    # the custody certificate also needs an unbroken chain: held for C1 only
    assert not wb["C1"]["custody_certificate"]["issuable"]
    assert "seal_mismatch:PH1" in wb["C1"]["custody_certificate"]["hold_reasons"]
    assert wb["C2"]["custody_certificate"]["issuable"]
    rows = report(db)
    assert {r["crate_id"] for r in rows["evidence_conflict_crate"]} == {"C1"}


def test_split_must_conserve_crate_count(tmp_path):
    db = tmp_path / "state.db"
    trip_import(db, FIX / "trip_normal.json")
    assert pallet_import(db, FIX / "pallet_count_violation.json")["conflicts"] == 1
    rows = report(db)
    conflict = rows["evidence_conflict"][0]
    assert conflict["kind"] == "split_count_violation"
    assert {r["crate_id"] for r in rows["evidence_conflict_crate"]} == {"C1", "C2"}
    wb = _whereabouts(db)
    for crate in ("C1", "C2"):
        # totals alone must not put one crate on two routes: both stay on PL-1
        assert wb[crate]["possible_vessels"] == ["SEA-1"]
        assert wb[crate]["possible_pallets"] == ["PL-1"]
        assert wb[crate]["port_receipt"]["issuable"]
        assert not wb[crate]["custody_certificate"]["issuable"]
    assert {p["pallet_id"] for p in rows["pallet_state"]} == {"PL-1"}


def test_out_of_order_import_converges_without_new_handovers(tmp_path):
    scrambled = tmp_path / "scrambled.db"
    trip_import(scrambled, FIX / "trip_normal.json")
    first = pallet_import(scrambled, FIX / "pallet_oo_handovers.json")
    assert first["handovers"] == 2 and first["conflicts"] == 2  # pallets not on record yet
    assert report(scrambled)["crate_whereabouts"] == []
    pallet_import(scrambled, FIX / "pallet_oo_pack.json")
    ordered = tmp_path / "ordered.db"
    trip_import(ordered, FIX / "trip_normal.json")
    pallet_import(ordered, FIX / "pallet_split.json")
    # conclusions depend on timestamps and evidence, never on import order
    assert report(scrambled)["crate_whereabouts"] == report(ordered)["crate_whereabouts"]
    assert report(scrambled)["pallet_state"] == report(ordered)["pallet_state"]
    assert report(scrambled)["evidence_conflict"] == []
    # re-running the same inputs adds no handovers
    pallet_import(scrambled, FIX / "pallet_oo_handovers.json")
    pallet_import(scrambled, FIX / "pallet_oo_pack.json")
    assert len(report(scrambled)["pallet_handover"]) == 2


def test_conflicting_rechecks_are_retained_with_affected_crates(tmp_path):
    db = tmp_path / "state.db"
    trip_import(db, FIX / "trip_normal.json")
    pallet_import(db, FIX / "pallet_split.json")
    assert pallet_import(db, FIX / "pallet_recheck_conflict.json")["conflicts"] == 1
    rows = report(db)
    assert len(rows["crate_recheck"]) == 2  # both statements kept as evidence
    conflict = rows["evidence_conflict"][0]
    assert conflict["kind"] == "recheck_conflict"
    assert {r["crate_id"] for r in rows["evidence_conflict_crate"]} == {"C1"}
    wb = _whereabouts(db)
    assert wb["C1"]["definite_vessel"] is None
    assert not wb["C1"]["custody_certificate"]["issuable"]
    assert any(r.startswith("recheck_conflict")
               for r in wb["C1"]["custody_certificate"]["hold_reasons"])
    assert not wb["C2"]["port_receipt"]["issuable"]


def test_verification_batch_is_bounded(tmp_path):
    db = tmp_path / "state.db"
    trip_import(db, FIX / "trip_normal.json")
    too_many_crates = tmp_path / "crates.json"
    too_many_crates.write_text(json.dumps({"packs": [{"id": "PK9", "pallet": "PL-9",
        "crates": ["C%d" % i for i in range(7)], "vessel": "SEA-1", "seal": "SP-9",
        "at": "2026-06-02T01:00:00+00:00", "sign_ref": "s"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="verification_limit_exceeded"):
        pallet_import(db, too_many_crates)
    too_many_pallets = tmp_path / "pallets.json"
    too_many_pallets.write_text(json.dumps({
        "packs": [{"id": "PK1", "pallet": "PL-1", "crates": ["C1", "C2", "C3", "C4"],
                   "vessel": "SEA-1", "seal": "SP-1", "at": "2026-06-02T01:00:00+00:00",
                   "sign_ref": "s"}],
        "splits": [{"id": "SP1", "from_pallet": "PL-1", "vessel": "SEA-1", "old_seal": "SP-1",
                    "into": [{"pallet": p, "count": 1, "seal": "S"}
                             for p in ("PL-A", "PL-B", "PL-C", "PL-D")],
                    "at": "2026-06-02T02:00:00+00:00", "release_ref": "r", "accept_ref": "a"}]}),
        encoding="utf-8")
    with pytest.raises(ValueError, match="verification_limit_exceeded"):
        pallet_import(db, too_many_pallets)
    too_many_handovers = tmp_path / "handovers.json"
    too_many_handovers.write_text(json.dumps({"handovers": [
        {"id": "H%d" % i, "pallet": "PL-1", "from": "SEA-1", "to": "SEA-2", "port": "PORT-B",
         "at": "2026-06-02T01:00:00+00:00", "old_seal": "S", "new_seal": "S2",
         "release_ref": "r", "accept_ref": "a"} for i in range(9)]}), encoding="utf-8")
    with pytest.raises(ValueError, match="verification_limit_exceeded"):
        pallet_import(db, too_many_handovers)
    assert report(db)["source_failure"][-1]["error"] == "verification_limit_exceeded"
    assert report(db)["pallet_pack"] == []
