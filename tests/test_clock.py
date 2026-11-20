import json
from pathlib import Path
import pytest
from landingweigh.domain import trip_import,weigh_import,quota_import
from landingweigh.report import report

F=Path(__file__).parents[1]/"fixtures"

def _input(tmp_path,name,payload):
    file=tmp_path/name;file.write_text(json.dumps(payload),encoding="utf-8");return file

def _clock(state,voyage):
    return next(c for c in state["voyage_clock"] if c["voyage"]==voyage)

def test_shared_offset_cannot_be_spliced_into_valid_trip(tmp_path):
    """启航钟 A 与靠港钟 B 同偏移(快 0-10 分钟):两端各自都能在某个偏移下合格,
    但没有一个共同偏移使两端同时合格,航次必须扣留而不是凭空拼出合格。"""
    db=tmp_path/"db.sqlite"
    result=trip_import(db,F/"trip_clock_detained.json")
    assert result["held"]==2 and result["clock_sheets"]==1
    state=report(db)
    assert state["voyage"][0]["status"]=="held"
    clock=_clock(state,"V-D")
    assert clock["reason"]=="window_unsatisfiable"
    assert clock["permit_events"]=={"start":"depart","end":"arrive"}
    assert clock["offset_ranges"]=={"A":["-600","0"],"B":["-600","0"]}
    conditions={c["name"]:c for c in clock["conditions"]}
    assert conditions["permit_start"]["possible"] and conditions["permit_end"]["possible"]
    assert not conditions["permit_start"]["entailed"] and not conditions["permit_end"]["entailed"]
    assert clock["jointly_satisfiable"] is False
    assert clock["event_utc"]["depart"]==["2026-06-01T11:45:00+00:00","2026-06-01T11:55:00+00:00"]
    assert clock["event_utc"]["arrive"]==["2026-06-01T11:55:00+00:00","2026-06-01T12:05:00+00:00"]
    assert state["catch_crate"][0]["status"]=="held"

def test_departure_end_document_stays_pending_then_late_anchor_resolves(tmp_path):
    """文书把许可结束条件改为按启航核验:同一航次转为待核;待核期间箱组可登记
    实物与原始钟读数但不得可信核秤或放行;后到锚点把待核转成有效且不重写来源。"""
    db=tmp_path/"db.sqlite"
    result=trip_import(db,F/"trip_clock_pending.json")
    assert result["pending"]==1 and result["held"]==1 and result["clock_sheets"]==1
    state=report(db)
    assert state["voyage"][0]["status"]=="pending"
    clock=_clock(state,"V-C")
    assert clock["reason"]=="window_unresolved"
    assert clock["permit_events"]=={"start":"depart","end":"depart"}  # 准入依据取自文书而非一律靠港
    assert clock["jointly_satisfiable"] is True
    assert state["voyage"][0]["depart"]=="2026-06-01T11:55:00+00:00"  # 原始钟读数保留
    assert state["catch_crate"][0]["status"]=="held"  # 实物已登记但不可放行
    assert weigh_import(db,F/"weigh_clock.json")["tickets"]==1
    assert report(db)["weigh_ticket"][0]["reason"]=="crate_not_eligible"  # 待核航次不得可信核秤
    quota_import(db,F/"quota_clock.json")
    state=report(db)
    assert state["release_decision"][0]["reason"]=="crate_held"  # 不得放行
    ticket_source=state["weigh_ticket"][0]["source_id"]
    crate_source=state["catch_crate"][0]["source_id"]
    result=trip_import(db,F/"trip_clock_anchor.json")  # 后到签发锚点 A∈[-240,0]
    assert result["clock_sheets"]==1 and result["quarantined"]==0
    state=report(db)
    assert state["voyage"][0]["status"]=="arrived"
    assert _clock(state,"V-C")["reason"]=="window_satisfied"
    assert _clock(state,"V-C")["offset_ranges"]["A"]==["-240","0"]
    assert state["catch_crate"][0]["status"]=="eligible"
    assert state["weigh_ticket"][0]["status"]=="trusted"
    assert state["release_decision"][0]["status"]=="released"
    assert state["weigh_ticket"][0]["source_id"]==ticket_source  # 秤票来源不得重写
    assert state["catch_crate"][0]["source_id"]==crate_source  # 箱组来源不得重写
    assert [e["status"] for e in state["voyage_status_event"]]==["pending","arrived"]

def test_late_anchor_can_turn_pending_into_detained(tmp_path):
    """后到锚点也可能把待核航次坐实为扣留,箱组与秤票维持不可信。"""
    db=tmp_path/"db.sqlite"
    trip_import(db,F/"trip_clock_pending.json")
    weigh_import(db,F/"weigh_clock.json");quota_import(db,F/"quota_clock.json")
    anchor=_input(tmp_path,"anchor.json",{"clock_sheets":[{"id":"CS-9","voyage":"V-C",
        "issuer":"harbor-clock-officer","issued_at":"2026-06-01T09:30:00+00:00",
        "anchors":[{"device":"A","lower":"-600","upper":"-360"}],"constraints":[]}]})
    assert trip_import(db,anchor)["clock_sheets"]==1
    state=report(db)
    assert state["voyage"][0]["status"]=="held"
    assert _clock(state,"V-C")["reason"]=="window_unsatisfiable"
    assert state["catch_crate"][0]["status"]=="held"
    assert state["weigh_ticket"][0]["status"]=="disputed"
    assert state["release_decision"][0]["reason"]=="crate_held"
    assert [e["status"] for e in state["voyage_status_event"]]==["pending","held"]

def test_related_clock_constraints_propagate_offsets(tmp_path):
    """相关钟差沿约束链传播:锚点定 A,A-B 同步,B-C 差 ±30 秒,推出 C 的可行区间。"""
    db=tmp_path/"db.sqlite"
    result=trip_import(db,F/"trip_clock_related.json")
    assert result["voyages"]==1 and result["held"]==0 and result["pending"]==0
    state=report(db)
    assert state["voyage"][0]["status"]=="arrived"
    clock=_clock(state,"V-R")
    assert clock["offset_ranges"]=={"A":["-60","0"],"B":["-60","0"],"C":["-90","30"]}
    assert clock["event_utc"]["arrive"]==["2026-06-01T11:56:30+00:00","2026-06-01T11:58:30+00:00"]
    assert all(c["entailed"] for c in clock["conditions"])
    assert state["catch_crate"][0]["status"]=="eligible"

def test_contradictory_constraints_quarantine_the_batch(tmp_path):
    """约束互相矛盾时隔离该批校对证据,航次保持待核;后续一致证据仍可定夺,
    与已接受证据矛盾的后到批次同样被隔离且不改变已定状态。"""
    db=tmp_path/"db.sqlite"
    result=trip_import(db,F/"trip_clock_contradiction.json")
    assert result["quarantined"]==1 and result["clock_sheets"]==0
    state=report(db)
    sheet=state["clock_sheet"][0]
    assert sheet["status"]=="quarantined" and sheet["reason"]=="constraint_contradiction"
    assert state["voyage"][0]["status"]=="pending"
    assert _clock(state,"V-X")["offset_ranges"]["A"]==[None,None]  # 被隔离的证据不参与解释
    good=_input(tmp_path,"good.json",{"clock_sheets":[{"id":"CS-X2","voyage":"V-X",
        "issuer":"harbor-clock-officer","issued_at":"2026-06-01T09:00:00+00:00",
        "anchors":[{"device":"A","lower":"-240","upper":"0"}],
        "constraints":[{"a":"A","b":"B","at":"2026-06-01T09:00:00+00:00","lower":"0","upper":"0"}]}]})
    assert trip_import(db,good)["clock_sheets"]==1
    state=report(db)
    assert state["voyage"][0]["status"]=="arrived"
    bad=_input(tmp_path,"bad.json",{"clock_sheets":[{"id":"CS-X3","voyage":"V-X",
        "issuer":"harbor-clock-officer","issued_at":"2026-06-01T10:00:00+00:00",
        "anchors":[{"device":"A","lower":"300","upper":"600"}],"constraints":[]}]})
    result=trip_import(db,bad)
    assert result["quarantined"]==1 and result["clock_sheets"]==0
    state=report(db)
    assert next(s for s in state["clock_sheet"] if s["id"]=="CS-X3")["reason"]=="evidence_contradiction"
    assert state["voyage"][0]["status"]=="arrived"  # 矛盾批次被隔离而非凭顺序覆盖
    by_status={}
    for s in state["clock_sheet"]:by_status.setdefault(s["status"],set()).add(s["id"])
    assert by_status=={"accepted":{"CS-X2"},"quarantined":{"CS-X1","CS-X3"}}  # 隔离证据留存但不参与解释

def test_contradictory_sheets_in_one_file_are_quarantined_together(tmp_path):
    """同一批内两张校对单互相矛盾时整批隔离,不按输入顺序选一条。"""
    db=tmp_path/"db.sqlite"
    payload={"permits":[{"id":"P-Y","vessel":"SEA-Y","start":"2026-06-01T11:50:00+00:00",
        "end":"2026-06-01T12:00:00+00:00","start_event":"depart","end_event":"depart","species":["cod"]}],
        "voyages":[{"id":"V-Y","vessel":"SEA-Y","permit":"P-Y","depart":"2026-06-01T11:55:00+00:00",
        "arrive":"2026-06-01T12:05:00+00:00","depart_device":"A","arrive_device":"B"}],
        "clock_sheets":[
            {"id":"CS-Y1","voyage":"V-Y","issuer":"harbor-clock-officer","issued_at":"2026-06-01T08:00:00+00:00",
             "anchors":[{"device":"A","lower":"-600","upper":"0"}],
             "constraints":[{"a":"A","b":"B","at":"2026-06-01T08:00:00+00:00","lower":"0","upper":"0"}]},
            {"id":"CS-Y2","voyage":"V-Y","issuer":"harbor-clock-officer","issued_at":"2026-06-01T08:20:00+00:00",
             "anchors":[{"device":"A","lower":"-600","upper":"0"}],
             "constraints":[{"a":"A","b":"B","at":"2026-06-01T08:20:00+00:00","lower":"300","upper":"600"}]}],
        "crates":[]}
    result=trip_import(db,_input(tmp_path,"pair.json",payload))
    assert result["quarantined"]==2 and result["clock_sheets"]==0
    state=report(db)
    assert {s["id"] for s in state["clock_sheet"]}=={"CS-Y1","CS-Y2"}
    assert all(s["status"]=="quarantined" for s in state["clock_sheet"])
    assert state["voyage"][0]["status"]=="pending"

def test_sheet_structure_and_device_attribution_are_validated(tmp_path):
    db=tmp_path/"db.sqlite"
    trip_import(db,F/"trip_clock_pending.json")
    no_anchor=_input(tmp_path,"no-anchor.json",{"clock_sheets":[{"id":"CS-B1","voyage":"V-C",
        "issuer":"harbor-clock-officer","issued_at":"2026-06-01T09:00:00+00:00","anchors":[],
        "constraints":[{"a":"A","b":"B","at":"2026-06-01T09:00:00+00:00","lower":"0","upper":"0"}]}]})
    with pytest.raises(ValueError,match="invalid_clock_sheet"):
        trip_import(db,no_anchor)
    too_many=_input(tmp_path,"too-many.json",{"clock_sheets":[{"id":"CS-B2","voyage":"V-C",
        "issuer":"harbor-clock-officer","issued_at":"2026-06-01T09:00:00+00:00",
        "anchors":[{"device":"A","lower":"0","upper":"0"}],
        "constraints":[{"a":"A","b":"B","at":"2026-06-01T09:00:00+00:00","lower":"0","upper":"0"},
                       {"a":"A","b":"C","at":"2026-06-01T09:00:00+00:00","lower":"0","upper":"0"},
                       {"a":"A","b":"D","at":"2026-06-01T09:00:00+00:00","lower":"0","upper":"0"},
                       {"a":"A","b":"E","at":"2026-06-01T09:00:00+00:00","lower":"0","upper":"0"}]}]})
    with pytest.raises(ValueError,match="invalid_clock_sheet"):
        trip_import(db,too_many)
    unknown=_input(tmp_path,"unknown.json",{"clock_sheets":[{"id":"CS-B3","voyage":"V-ZZ",
        "issuer":"harbor-clock-officer","issued_at":"2026-06-01T09:00:00+00:00",
        "anchors":[{"device":"A","lower":"0","upper":"0"}],"constraints":[]}]})
    with pytest.raises(ValueError,match="clock_sheet_voyage_unknown"):
        trip_import(db,unknown)
    assert report(db)["voyage"][0]["status"]=="pending"  # 失败的导入不改变既有状态

def test_duplicate_imports_are_idempotent_and_sheet_identity_is_frozen(tmp_path):
    db=tmp_path/"db.sqlite"
    first=trip_import(db,F/"trip_clock_pending.json")
    assert trip_import(db,F/"trip_clock_pending.json")==first  # 同字节直接命中缓存
    state=report(db)
    assert len(state["clock_sheet"])==1 and len(state["voyage"])==1 and len(state["catch_crate"])==1
    payload=json.loads((F/"trip_clock_pending.json").read_text())
    repacked=_input(tmp_path,"repacked.json",payload)  # 内容相同、字节不同
    result=trip_import(db,repacked)
    assert result["voyages"]==0 and result["crates"]==0 and result["clock_sheets"]==0
    state=report(db)
    assert len(state["clock_sheet"])==1 and len(state["voyage_status_event"])==1
    payload["clock_sheets"][0]["constraints"][0]["upper"]="5"  # 同 id 不同内容
    with pytest.raises(ValueError,match="clock_sheet_identity_conflict"):
        trip_import(db,_input(tmp_path,"changed.json",payload))
    payload=json.loads((F/"trip_clock_pending.json").read_text())
    payload["voyages"][0]["arrive_device"]="A"  # 航次设备归属同样冻结
    with pytest.raises(ValueError,match="voyage_identity_conflict"):
        trip_import(db,_input(tmp_path,"changed-voyage.json",payload))
    assert report(db)["voyage"][0]["arrive_device"]=="B"
