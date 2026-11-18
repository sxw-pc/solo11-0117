from pathlib import Path
import pytest
from landingweigh.domain import trip_import
from landingweigh.relay import relay_import
from landingweigh.report import report

FIX = Path(__file__).resolve().parents[1] / "fixtures"


def test_direct_handover_preserves_original_crate_and_current_holder(tmp_path):
    db = tmp_path / "state.db"
    trip_import(db, FIX / "trip_normal.json")
    trip_import(db, FIX / "relay_permit.json")
    assert relay_import(db, FIX / "relay_normal.json") == {"recorded": 1}
    assert relay_import(db, FIX / "relay_normal.json") == {"recorded": 1}
    rows = report(db)
    assert rows["catch_crate"][0]["voyage_id"] == "V1"
    assert rows["crate_custody"][0]["holder_vessel"] == "SEA-2"
    assert rows["crate_custody"][0]["seal"] == "S1-B"
    assert len(rows["crate_handover"]) == 1


def test_handover_cannot_depart_from_previous_holder(tmp_path):
    db = tmp_path / "state.db"
    trip_import(db, FIX / "trip_normal.json")
    trip_import(db, FIX / "relay_permit.json")
    relay_import(db, FIX / "relay_normal.json")
    with pytest.raises(ValueError, match="handover_custody_mismatch"):
        relay_import(db, FIX / "relay_conflict.json")
    rows = report(db)
    assert len(rows["crate_handover"]) == 1
    assert rows["source_failure"][-1]["error"] == "handover_custody_mismatch"
