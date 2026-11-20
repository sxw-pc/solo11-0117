"""Pallet-level transshipment evidence: signed packs, splits, merges and rechecks.

relay_import rejects a mismatched crate handover outright; this module instead
retains every signed pallet record as evidence and lets a solver replay it in
timestamp order (never import order).  For crates on sub-pallets with unclear
labels the solver keeps every physical assignment that is simultaneously
consistent with piece counts, seals, handover times and per-crate rechecks,
then derives for each original crate the vessel it certainly reached, the
vessels it may have reached and the chain breaks it cannot explain.  Original
voyage identity from trip_import is only read, never modified.
"""
import json
from itertools import combinations
from .intake import ingest
from .domain import _time

MAX_CRATES = 6
MAX_PALLETS = 4
MAX_HANDOVERS = 8
MAX_HYPOTHESES = 5000


def _need(item, fields):
    if any(not item.get(field) for field in fields):
        raise ValueError("pallet_record_fields_required")


def pallet_import(path, file):
    def handler(db, data, source):
        packs = data.get("packs") or []
        splits = data.get("splits") or []
        merges = data.get("merges") or []
        handovers = data.get("handovers") or []
        rechecks = data.get("rechecks") or []
        crates = {c for p in packs for c in p.get("crates") or [] if c}
        crates |= {r["crate"] for r in rechecks if r.get("crate")}
        pallets = ({p["pallet"] for p in packs if p.get("pallet")}
                   | {s["from_pallet"] for s in splits if s.get("from_pallet")}
                   | {part["pallet"] for s in splits for part in s.get("into") or [] if part.get("pallet")}
                   | {m["into_pallet"] for m in merges if m.get("into_pallet")}
                   | {pid for m in merges for pid in m.get("from_pallets") or []}
                   | {h["pallet"] for h in handovers if h.get("pallet")}
                   | {r["pallet"] for r in rechecks if r.get("pallet")})
        if len(crates) > MAX_CRATES or len(pallets) > MAX_PALLETS or len(handovers) > MAX_HANDOVERS:
            raise ValueError("verification_limit_exceeded")
        counts = {"packed": 0, "split": 0, "merged": 0, "handovers": 0, "rechecks": 0}
        for p in packs:
            _need(p, ("id", "pallet", "vessel", "seal", "at"))
            members = sorted({c for c in p.get("crates") or [] if c})
            if not members:
                raise ValueError("pallet_record_fields_required")
            old = db.execute("SELECT * FROM pallet_pack WHERE id=?", (p["id"],)).fetchone()
            if old:
                prior = [r[0] for r in db.execute(
                    "SELECT crate_id FROM pallet_pack_crate WHERE pack_id=? ORDER BY crate_id", (p["id"],))]
                if ((old["pallet_id"], old["vessel"], old["seal"], old["at"], old["sign_ref"] or "")
                        != (p["pallet"], p["vessel"], p["seal"], p["at"], p.get("sign_ref") or "")
                        or prior != members):
                    raise ValueError("pack_identity_conflict")
                continue
            db.execute("INSERT INTO pallet_pack VALUES (?,?,?,?,?,?,?)",
                       (p["id"], p["pallet"], p["vessel"], p["seal"], p["at"], p.get("sign_ref"), source))
            db.executemany("INSERT INTO pallet_pack_crate VALUES (?,?)", [(p["id"], c) for c in members])
            counts["packed"] += 1
        for s in splits:
            _need(s, ("id", "from_pallet", "vessel", "old_seal", "at"))
            into = s.get("into") or []
            if (len(into) < 2
                    or any(not part.get("pallet") or not isinstance(part.get("count"), int)
                           or isinstance(part.get("count"), bool) or part["count"] < 1 for part in into)
                    or len({part["pallet"] for part in into}) != len(into)):
                raise ValueError("pallet_record_fields_required")
            parts = sorted((part["pallet"], part["count"], part.get("seal") or "") for part in into)
            old = db.execute("SELECT * FROM pallet_split WHERE id=?", (s["id"],)).fetchone()
            if old:
                prior = sorted((r["pallet_id"], r["crate_count"], r["seal"] or "")
                               for r in db.execute("SELECT * FROM pallet_split_part WHERE split_id=?", (s["id"],)))
                if ((old["from_pallet"], old["vessel"], old["old_seal"], old["at"],
                     old["release_ref"] or "", old["accept_ref"] or "")
                        != (s["from_pallet"], s["vessel"], s["old_seal"], s["at"],
                            s.get("release_ref") or "", s.get("accept_ref") or "")
                        or prior != parts):
                    raise ValueError("split_identity_conflict")
                continue
            db.execute("INSERT INTO pallet_split VALUES (?,?,?,?,?,?,?,?)",
                       (s["id"], s["from_pallet"], s["vessel"], s["old_seal"], s["at"],
                        s.get("release_ref"), s.get("accept_ref"), source))
            db.executemany("INSERT INTO pallet_split_part VALUES (?,?,?,?)",
                           [(s["id"], pallet, count, seal or None) for pallet, count, seal in parts])
            counts["split"] += 1
        for m in merges:
            _need(m, ("id", "into_pallet", "vessel", "seal", "at"))
            srcs = m.get("from_pallets") or []
            old_seals = m.get("old_seals") or {}
            if len(srcs) < 2 or len(set(srcs)) != len(srcs) or m["into_pallet"] in srcs:
                raise ValueError("pallet_record_fields_required")
            parts = sorted((pid, old_seals.get(pid) or "") for pid in srcs)
            old = db.execute("SELECT * FROM pallet_merge WHERE id=?", (m["id"],)).fetchone()
            if old:
                prior = sorted((r["pallet_id"], r["old_seal"] or "")
                               for r in db.execute("SELECT * FROM pallet_merge_part WHERE merge_id=?", (m["id"],)))
                if ((old["into_pallet"], old["vessel"], old["seal"], old["at"],
                     old["release_ref"] or "", old["accept_ref"] or "")
                        != (m["into_pallet"], m["vessel"], m["seal"], m["at"],
                            m.get("release_ref") or "", m.get("accept_ref") or "")
                        or prior != parts):
                    raise ValueError("merge_identity_conflict")
                continue
            db.execute("INSERT INTO pallet_merge VALUES (?,?,?,?,?,?,?,?)",
                       (m["id"], m["into_pallet"], m["vessel"], m["seal"], m["at"],
                        m.get("release_ref"), m.get("accept_ref"), source))
            db.executemany("INSERT INTO pallet_merge_part VALUES (?,?,?)",
                           [(m["id"], pid, seal or None) for pid, seal in parts])
            counts["merged"] += 1
        for h in handovers:
            _need(h, ("id", "pallet", "from", "to", "port", "at", "old_seal", "new_seal"))
            if h["from"] == h["to"]:
                raise ValueError("handover_same_vessel")
            values = (h["pallet"], h["from"], h["to"], h["port"], h["at"], h["old_seal"], h["new_seal"],
                      h.get("release_ref") or "", h.get("accept_ref") or "")
            old = db.execute("SELECT * FROM pallet_handover WHERE id=?", (h["id"],)).fetchone()
            if old:
                keys = ("pallet_id", "from_vessel", "to_vessel", "port", "at",
                        "old_seal", "new_seal", "release_ref", "accept_ref")
                if tuple(old[k] or "" for k in keys) != values:
                    raise ValueError("handover_identity_conflict")
                continue
            db.execute("INSERT INTO pallet_handover VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (h["id"], h["pallet"], h["from"], h["to"], h["port"], h["at"],
                        h["old_seal"], h["new_seal"], h.get("release_ref"), h.get("accept_ref"), source))
            counts["handovers"] += 1
        for r in rechecks:
            _need(r, ("id", "crate", "pallet", "vessel", "seal", "at"))
            values = (r["crate"], r["pallet"], r["vessel"], r["seal"], r["at"], r.get("sign_ref") or "")
            old = db.execute("SELECT * FROM crate_recheck WHERE id=?", (r["id"],)).fetchone()
            if old:
                if tuple(old[k] or "" for k in ("crate_id", "pallet_id", "vessel", "seal", "at", "sign_ref")) != values:
                    raise ValueError("recheck_identity_conflict")
                continue
            db.execute("INSERT INTO crate_recheck VALUES (?,?,?,?,?,?,?,?)",
                       (r["id"], r["crate"], r["pallet"], r["vessel"], r["seal"], r["at"],
                        r.get("sign_ref"), source))
            counts["rechecks"] += 1
        recompute_whereabouts(db)
        conflicts = db.execute("SELECT COUNT(*) FROM evidence_conflict").fetchone()[0]
        return {**counts, "conflicts": conflicts}
    return ingest(path, "pallet", file, handler)


def _distributions(pool, counts):
    """Every way to deal a sorted pool into ordered parts of the given sizes."""
    if not counts:
        yield ()
        return
    first, rest = counts[0], counts[1:]
    for combo in combinations(pool, first):
        remaining = [c for c in pool if c not in combo]
        for tail in _distributions(remaining, rest):
            yield (combo,) + tail


def _handover_path(hyp, cid, live, pallets):
    """Pallet lineage of one crate in one hypothesis, as an ordered handover-id list."""
    pid = next((p for p in live if cid in hyp[p]), None)
    if pid is None:
        return []
    chain = []
    while pid is not None:
        chain.append(pid)
        parents = pallets[pid]["parents"]
        if not parents:
            pid = None
        elif len(parents) == 1:
            pid = parents[0]
        else:
            pid = next(p for p in parents if cid in hyp[p])
    ids = []
    for pallet in reversed(chain):
        ids.extend(pallets[pallet]["handovers"])
    return ids


def recompute_whereabouts(db):
    packs = [dict(r) for r in db.execute("SELECT * FROM pallet_pack")]
    pack_crates = {}
    for r in db.execute("SELECT * FROM pallet_pack_crate ORDER BY crate_id"):
        pack_crates.setdefault(r["pack_id"], []).append(r["crate_id"])
    splits = [dict(r) for r in db.execute("SELECT * FROM pallet_split")]
    split_parts = {}
    for r in db.execute("SELECT * FROM pallet_split_part ORDER BY pallet_id"):
        split_parts.setdefault(r["split_id"], []).append(
            {"pallet": r["pallet_id"], "count": r["crate_count"], "seal": r["seal"]})
    merges = [dict(r) for r in db.execute("SELECT * FROM pallet_merge")]
    merge_parts = {}
    for r in db.execute("SELECT * FROM pallet_merge_part ORDER BY pallet_id"):
        merge_parts.setdefault(r["merge_id"], []).append(
            {"pallet": r["pallet_id"], "old_seal": r["old_seal"]})
    handovers = [dict(r) for r in db.execute("SELECT * FROM pallet_handover")]
    handover_by_id = {h["id"]: h for h in handovers}
    rechecks = [dict(r) for r in db.execute("SELECT * FROM crate_recheck")]
    registry = {r["id"]: r["seal"] for r in db.execute("SELECT id,seal FROM catch_crate")}

    for table in ("evidence_conflict_crate", "evidence_conflict", "pallet_state", "crate_whereabouts"):
        db.execute("DELETE FROM " + table)

    # Connected components keep one verification bounded: a component may hold
    # at most MAX_CRATES crates, MAX_PALLETS pallets and MAX_HANDOVERS handovers.
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    for p in packs:
        find("P:" + p["pallet_id"])
        for c in pack_crates.get(p["id"], []):
            union("P:" + p["pallet_id"], "C:" + c)
    for s in splits:
        find("P:" + s["from_pallet"])
        for part in split_parts.get(s["id"], []):
            union("P:" + s["from_pallet"], "P:" + part["pallet"])
    for m in merges:
        find("P:" + m["into_pallet"])
        for part in merge_parts.get(m["id"], []):
            union("P:" + m["into_pallet"], "P:" + part["pallet"])
    for h in handovers:
        find("P:" + h["pallet_id"])
    for r in rechecks:
        union("P:" + r["pallet_id"], "C:" + r["crate_id"])

    members = {}
    for node in list(parent):
        root = find(node)
        members.setdefault(root, {"pallets": set(), "crates": set()})
        members[root]["pallets" if node.startswith("P:") else "crates"].add(node[2:])
    comp_handovers = {root: 0 for root in members}
    for h in handovers:
        comp_handovers[find("P:" + h["pallet_id"])] += 1

    conflicts = []  # [kind, event_id, detail, pallets:set, crates:set]

    def flag(kind, event_id, detail, pallets=(), crates=()):
        conflicts.append([kind, event_id, detail, set(pallets), set(crates)])

    skipped = set()
    for root, mem in members.items():
        if (len(mem["crates"]) > MAX_CRATES or len(mem["pallets"]) > MAX_PALLETS
                or comp_handovers[root] > MAX_HANDOVERS):
            flag("verification_limit_exceeded", None,
                 "component holds %d crates, %d pallets, %d handovers"
                 % (len(mem["crates"]), len(mem["pallets"]), comp_handovers[root]),
                 pallets=mem["pallets"], crates=mem["crates"])
            skipped.add(root)

    # Replay records in timestamp order so conclusions never depend on import order.
    events = []
    for p in packs:
        events.append((_time(p["at"]), 0, p["id"], "pack", p))
    for h in handovers:
        events.append((_time(h["at"]), 1, h["id"], "handover", h))
    for s in splits:
        events.append((_time(s["at"]), 2, s["id"], "split", s))
    for m in merges:
        events.append((_time(m["at"]), 3, m["id"], "merge", m))
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    pallets = {}     # pallet_id -> runtime custody state
    structures = {}  # component root -> [(born_at, rank, event_id, kind, payload)]
    packed_by = {}   # crate -> first valid pack id
    for at, rank, eid, kind, ev in events:
        if kind == "pack":
            pid = ev["pallet_id"]
            root = find("P:" + pid)
            if root in skipped:
                continue
            if pid in pallets:
                flag("pallet_id_reused", eid,
                     "pallet %s already created by %s" % (pid, pallets[pid]["born_id"]), pallets=[pid])
                continue
            members_ok = []
            for c in pack_crates.get(eid, []):
                if c not in registry:
                    flag("crate_unknown", eid, "crate %s has no landed registry entry" % c, crates=[c])
                elif c in packed_by:
                    flag("crate_double_packed", eid,
                         "crate %s already packed by %s" % (c, packed_by[c]), crates=[c])
                else:
                    members_ok.append(c)
            for c in members_ok:
                packed_by[c] = eid
            if not ev["sign_ref"]:
                flag("credentials_missing", eid, "pack %s lacks a signature reference" % eid,
                     pallets=[pid], crates=members_ok)
            pallets[pid] = {"root": root, "born_id": eid, "kind": "pack", "born_at": at, "last_at": at,
                            "born_vessel": ev["vessel"], "vessel": ev["vessel"], "seal": ev["seal"],
                            "count": len(members_ok), "parents": [], "live": True, "gone_at": None,
                            "gone_id": None, "handovers": []}
            structures.setdefault(root, []).append((at, rank, eid, "pack", (pid, frozenset(members_ok))))
        elif kind == "split":
            src = ev["from_pallet"]
            root = find("P:" + src)
            if root in skipped:
                continue
            parts = split_parts.get(eid, [])
            if src not in pallets:
                flag("split_parent_unknown", eid, "parent pallet %s is not on record" % src)
                continue
            if not pallets[src]["live"]:
                flag("split_parent_consumed", eid,
                     "parent pallet %s was consumed by %s" % (src, pallets[src]["gone_id"]), pallets=[src])
                continue
            reused = [pt["pallet"] for pt in parts if pt["pallet"] in pallets]
            if reused:
                flag("pallet_id_reused", eid,
                     "pallet ids already exist: %s" % ",".join(sorted(reused)), pallets=reused)
                continue
            total = sum(pt["count"] for pt in parts)
            if total != pallets[src]["count"]:
                # A split must conserve crates; totals alone may not send one
                # crate down two routes, so the split is set aside as evidence.
                flag("split_count_violation", eid,
                     "parts hold %d crates but parent %s holds %d" % (total, src, pallets[src]["count"]),
                     pallets=[src])
                continue
            breaks = []
            if at < pallets[src]["last_at"]:
                breaks.append("time_reversal")
            if ev["vessel"] != pallets[src]["vessel"]:
                breaks.append("holder_mismatch")
            if ev["old_seal"] != pallets[src]["seal"]:
                breaks.append("seal_mismatch")
            if not ev["release_ref"] or not ev["accept_ref"]:
                breaks.append("credentials_missing")
            if any(not pt["seal"] for pt in parts):
                breaks.append("seal_missing")
            if breaks:
                flag("+".join(breaks), eid, "split of pallet %s: %s" % (src, ",".join(breaks)), pallets=[src])
            pallets[src]["live"] = False
            pallets[src]["gone_at"] = at
            pallets[src]["gone_id"] = eid
            for pt in parts:
                pallets[pt["pallet"]] = {"root": root, "born_id": eid, "kind": "split", "born_at": at,
                                         "last_at": at, "born_vessel": pallets[src]["vessel"],
                                         "vessel": pallets[src]["vessel"], "seal": pt["seal"],
                                         "count": pt["count"], "parents": [src], "live": True,
                                         "gone_at": None, "gone_id": None, "handovers": []}
            structures.setdefault(root, []).append((at, rank, eid, "split", (src, parts)))
        elif kind == "merge":
            dest = ev["into_pallet"]
            root = find("P:" + dest)
            if root in skipped:
                continue
            parts = merge_parts.get(eid, [])
            srcs = [pt["pallet"] for pt in parts]
            unknown = [s for s in srcs if s not in pallets]
            if unknown:
                flag("merge_parent_unknown", eid,
                     "parent pallets not on record: %s" % ",".join(sorted(unknown)))
                continue
            consumed = [s for s in srcs if not pallets[s]["live"]]
            if consumed:
                flag("merge_parent_consumed", eid,
                     "parent pallets already consumed: %s" % ",".join(sorted(consumed)), pallets=consumed)
                continue
            if dest in pallets:
                flag("pallet_id_reused", eid,
                     "pallet %s already created by %s" % (dest, pallets[dest]["born_id"]), pallets=[dest])
                continue
            breaks = []
            if at < max(pallets[s]["last_at"] for s in srcs):
                breaks.append("time_reversal")
            if any(pallets[s]["vessel"] != ev["vessel"] for s in srcs):
                breaks.append("holder_mismatch")
            for pt in parts:
                if not pt["old_seal"]:
                    breaks.append("seal_unverified")
                    break
                if pt["old_seal"] != pallets[pt["pallet"]]["seal"]:
                    breaks.append("seal_mismatch")
                    break
            if not ev["release_ref"] or not ev["accept_ref"]:
                breaks.append("credentials_missing")
            breaks = list(dict.fromkeys(breaks))
            if breaks:
                flag("+".join(breaks), eid, "merge into pallet %s: %s" % (dest, ",".join(breaks)), pallets=srcs)
            for s in srcs:
                pallets[s]["live"] = False
                pallets[s]["gone_at"] = at
                pallets[s]["gone_id"] = eid
            pallets[dest] = {"root": root, "born_id": eid, "kind": "merge", "born_at": at, "last_at": at,
                             "born_vessel": ev["vessel"], "vessel": ev["vessel"], "seal": ev["seal"],
                             "count": sum(pallets[s]["count"] for s in srcs), "parents": srcs,
                             "live": True, "gone_at": None, "gone_id": None, "handovers": []}
            structures.setdefault(root, []).append((at, rank, eid, "merge", (dest, srcs)))
        else:  # handover
            pid = ev["pallet_id"]
            root = find("P:" + pid)
            if root in skipped:
                continue
            if pid not in pallets:
                flag("handover_pallet_unknown", eid, "pallet %s is not on record" % pid)
                continue
            if not pallets[pid]["live"]:
                flag("handover_pallet_consumed", eid,
                     "pallet %s was consumed by %s" % (pid, pallets[pid]["gone_id"]), pallets=[pid])
                continue
            breaks = []
            if at < pallets[pid]["last_at"]:
                breaks.append("time_reversal")
            if ev["from_vessel"] != pallets[pid]["vessel"]:
                breaks.append("holder_mismatch")
            if ev["old_seal"] != pallets[pid]["seal"]:
                breaks.append("seal_mismatch")
            if not ev["release_ref"] or not ev["accept_ref"]:
                breaks.append("credentials_missing")
            if not ev["new_seal"]:
                breaks.append("seal_missing")
            if breaks:
                flag("+".join(breaks), eid, "handover on pallet %s: %s" % (pid, ",".join(breaks)), pallets=[pid])
            pallets[pid]["vessel"] = ev["to_vessel"]
            pallets[pid]["seal"] = ev["new_seal"]
            pallets[pid]["last_at"] = at
            pallets[pid]["handovers"].append(eid)

    # Per-crate rechecks: only statements consistent with custody, lifetime and
    # the registered original seal may constrain assignments; the rest are kept
    # as conflict evidence.
    valid_rechecks = []
    for rc in sorted(rechecks, key=lambda r: (_time(r["at"]), r["id"])):
        eid, cid, pid = rc["id"], rc["crate_id"], rc["pallet_id"]
        root = find("P:" + pid)
        if root in skipped:
            continue
        if cid not in registry:
            flag("recheck_crate_unknown", eid, "crate %s has no landed registry entry" % cid, crates=[cid])
            continue
        if pid not in pallets:
            flag("recheck_pallet_unknown", eid, "pallet %s is not on record" % pid, crates=[cid])
            continue
        pal = pallets[pid]
        at = _time(rc["at"])
        if at < pal["born_at"] or (pal["gone_at"] is not None and at > pal["gone_at"]):
            flag("recheck_outside_lifetime", eid, "pallet %s was not active at %s" % (pid, rc["at"]),
                 pallets=[pid], crates=[cid])
            continue
        holder = pal["born_vessel"]
        for hid in pal["handovers"]:
            h = handover_by_id[hid]
            if _time(h["at"]) <= at:
                holder = h["to_vessel"]
        if rc["vessel"] != holder:
            flag("recheck_holder_mismatch", eid,
                 "pallet %s was held by %s, not %s, at %s" % (pid, holder, rc["vessel"], rc["at"]),
                 pallets=[pid], crates=[cid])
            continue
        if rc["seal"] != registry[cid]:
            flag("recheck_seal_mismatch", eid,
                 "crate %s seal %s does not match the registered original seal" % (cid, rc["seal"]),
                 pallets=[pid], crates=[cid])
            continue
        if not rc["sign_ref"]:
            flag("credentials_missing", eid, "recheck %s lacks a signature reference" % eid,
                 pallets=[pid], crates=[cid])
            continue
        valid_rechecks.append(rc)

    # Enumerate every count-conserving assignment per component, then keep only
    # those simultaneously consistent with all valid rechecks.
    survivors_by_root = {}
    for root, structs in structures.items():
        structs.sort(key=lambda e: (e[0], e[1], e[2]))
        hyps = [{}]
        too_complex = False
        for at, rank, eid, kind, payload in structs:
            if kind == "pack":
                pid, exact = payload
                for h in hyps:
                    h[pid] = exact
            elif kind == "merge":
                dest, srcs = payload
                for h in hyps:
                    h[dest] = frozenset().union(*(h[s] for s in srcs))
            else:
                src, parts = payload
                counts = [pt["count"] for pt in parts]
                new_hyps = []
                for h in hyps:
                    pool = sorted(h[src])
                    for dist in _distributions(pool, counts):
                        nh = dict(h)
                        for pt, chosen in zip(parts, dist):
                            nh[pt["pallet"]] = frozenset(chosen)
                        new_hyps.append(nh)
                hyps = new_hyps
            if len(hyps) > MAX_HYPOTHESES:
                too_complex = True
                break
        if too_complex:
            flag("verification_too_complex", None,
                 "component assignments exceed %d hypotheses" % MAX_HYPOTHESES,
                 pallets=members[root]["pallets"], crates=members[root]["crates"])
            survivors_by_root[root] = []
            continue
        comp_rechecks = [rc for rc in valid_rechecks if find("P:" + rc["pallet_id"]) == root]
        survivors = [h for h in hyps
                     if all(rc["crate_id"] in h[rc["pallet_id"]] for rc in comp_rechecks)]
        if comp_rechecks and not survivors:
            ids = sorted(rc["id"] for rc in comp_rechecks)
            flag("recheck_conflict", "+".join(ids),
                 "per-crate rechecks are mutually inconsistent: %s" % ",".join(ids),
                 crates={rc["crate_id"] for rc in comp_rechecks})
            survivors = hyps
        survivors_by_root[root] = survivors

    base = {}          # crate -> possible pallets/vessels and handover sequences
    pallet_rows = []   # pallet_state rows
    for root, survivors in survivors_by_root.items():
        if not survivors:
            continue
        comp_pids = [pid for pid, pal in pallets.items() if pal["root"] == root]
        live = sorted(pid for pid in comp_pids if pallets[pid]["live"])
        for pid in sorted(comp_pids):
            pal = pallets[pid]
            poss = sorted({tuple(sorted(h[pid])) for h in survivors})
            pallet_rows.append((pid, pal["vessel"], pal["seal"], 1 if pal["live"] else 0,
                                pal["born_at"].isoformat(),
                                pal["gone_at"].isoformat() if pal["gone_at"] else None,
                                json.dumps([list(p) for p in poss])))
        for cid in sorted(members[root]["crates"]):
            poss_pallets = sorted({pid for pid in live for h in survivors if cid in h[pid]})
            poss_vessels = sorted({pallets[pid]["vessel"] for pid in poss_pallets})
            sequences = {tuple(_handover_path(h, cid, live, pallets)) for h in survivors}
            base[cid] = {"possible_pallets": poss_pallets, "possible_vessels": poss_vessels,
                         "sequences": sequences}

    # A conflict affects every crate that may sit on the involved pallets in any
    # surviving assignment, plus crates named directly by the evidence.
    final_conflicts = []
    for kind, eid, detail, cpallets, ccrates in conflicts:
        affected = set(ccrates)
        for pid in cpallets:
            if pid in pallets:
                for h in survivors_by_root.get(pallets[pid]["root"], []):
                    affected |= set(h.get(pid, ()))
        final_conflicts.append((kind, eid, detail, affected))
    final_conflicts.sort(key=lambda c: (c[0], c[1] or "", c[2]))
    affected_by = {}
    for kind, eid, detail, affected in final_conflicts:
        for c in affected:
            affected_by.setdefault(c, []).append((kind, eid))
    for kind, eid, detail, affected in final_conflicts:
        cur = db.execute("INSERT INTO evidence_conflict(kind,event_id,detail) VALUES (?,?,?)",
                         (kind, eid, detail))
        db.executemany("INSERT INTO evidence_conflict_crate VALUES (?,?)",
                       [(cur.lastrowid, c) for c in sorted(affected)])

    rows = []
    for cid in sorted(base):
        info = base[cid]
        poss_vessels = info["possible_vessels"]
        definite = poss_vessels[0] if len(poss_vessels) == 1 else None
        route_unique = len(info["sequences"]) == 1
        sequence = sorted(info["sequences"])[0] if route_unique else ()
        breaks = affected_by.get(cid, [])
        reasons = sorted({"%s:%s" % (k, e) for k, e in breaks})
        if not route_unique:
            reasons.append("route_ambiguous")
        if len(poss_vessels) > 1:
            reasons.append("vessel_ambiguous")
        if not poss_vessels:
            reasons.append("whereabouts_unknown")
        # The port receipt only proves definite arrival; the custody certificate
        # additionally requires an explained, uniquely attributable chain.
        receipt = 1 if definite else 0
        custody = 1 if receipt and not breaks and route_unique else 0
        rows.append((cid, definite, json.dumps(poss_vessels), json.dumps(info["possible_pallets"]),
                     json.dumps(list(sequence)), 1 if route_unique else 0, 1 if breaks else 0,
                     json.dumps(reasons), receipt, custody))
    for root, mem in members.items():
        for cid in sorted(mem["crates"]):
            if cid not in base:
                reasons = sorted({"%s:%s" % (k, e) for k, e in affected_by.get(cid, [])}
                                 | {"whereabouts_unknown"})
                rows.append((cid, None, "[]", "[]", "[]", 0, 1, json.dumps(reasons), 0, 0))
    rows.sort(key=lambda r: r[0])
    db.executemany("INSERT INTO crate_whereabouts VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    pallet_rows.sort(key=lambda r: r[0])
    db.executemany("INSERT INTO pallet_state VALUES (?,?,?,?,?,?,?)", pallet_rows)
