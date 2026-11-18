"""Direct sealed crate handovers between licensed vessels at a port."""
from .intake import ingest
from .domain import _time


def relay_import(path, file):
    def handler(db, data, source):
        recorded = 0
        for item in data["handovers"]:
            fields = ("id", "crate", "from", "to", "port", "at", "old_seal", "new_seal", "release_ref", "accept_ref")
            if any(not item.get(field) for field in fields):
                raise ValueError("handover_fields_required")
            old = db.execute("SELECT * FROM crate_handover WHERE id=?", (item["id"],)).fetchone()
            values = (item["crate"], item["from"], item["to"], item["port"], item["at"],
                      item["old_seal"], item["new_seal"], item["release_ref"], item["accept_ref"])
            if old:
                keys = ("crate_id", "from_vessel", "to_vessel", "port", "at", "old_seal", "new_seal", "release_ref", "accept_ref")
                if tuple(old[key] for key in keys) != values:
                    raise ValueError("handover_identity_conflict")
                continue
            if item["from"] == item["to"]:
                raise ValueError("handover_same_vessel")
            custody = db.execute("SELECT * FROM crate_custody WHERE crate_id=?", (item["crate"],)).fetchone()
            crate = db.execute("SELECT * FROM catch_crate WHERE id=?", (item["crate"],)).fetchone()
            if not custody or not crate or custody["holder_vessel"] != item["from"] or custody["seal"] != item["old_seal"]:
                raise ValueError("handover_custody_mismatch")
            instant = _time(item["at"])
            if instant < _time(custody["last_at"]):
                raise ValueError("handover_before_custody")
            permits = db.execute("""SELECT p.id,p.start_at,p.end_at FROM fishing_permit p
                JOIN permit_species s ON s.permit_id=p.id
                WHERE p.vessel=? AND s.species=?""", (item["to"], crate["verified_species"])).fetchall()
            if not any(_time(p["start_at"]) <= instant < _time(p["end_at"]) for p in permits):
                raise ValueError("recipient_permit_missing")
            db.execute("INSERT INTO crate_handover VALUES (?,?,?,?,?,?,?,?,?,?,?)", (item["id"], *values, source))
            db.execute("UPDATE crate_custody SET holder_vessel=?,seal=?,last_at=? WHERE crate_id=?",
                       (item["to"], item["new_seal"], item["at"], item["crate"]))
            recorded += 1
        return {"recorded": recorded}
    return ingest(path, "relay", file, handler)
