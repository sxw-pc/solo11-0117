import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS source_blob(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL,result TEXT,UNIQUE(kind,sha));
CREATE TABLE IF NOT EXISTS source_failure(id INTEGER PRIMARY KEY,kind TEXT,sha TEXT,raw BLOB,error TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS voyage(id TEXT PRIMARY KEY,vessel TEXT NOT NULL,permit TEXT NOT NULL,depart TEXT NOT NULL,arrive TEXT NOT NULL,status TEXT NOT NULL,source_id INTEGER REFERENCES source_blob(id));
CREATE TABLE IF NOT EXISTS catch_crate(id TEXT PRIMARY KEY,voyage_id TEXT REFERENCES voyage(id),species TEXT NOT NULL,seal TEXT NOT NULL,status TEXT NOT NULL,source_id INTEGER REFERENCES source_blob(id));
CREATE TABLE IF NOT EXISTS scale_calibration(scale TEXT,revision INTEGER,start_at TEXT,end_at TEXT,tolerance TEXT,source_id INTEGER REFERENCES source_blob(id),PRIMARY KEY(scale,revision));
CREATE TABLE IF NOT EXISTS weigh_ticket(id TEXT PRIMARY KEY,crate_id TEXT REFERENCES catch_crate(id),scale TEXT,at TEXT,gross TEXT,tare TEXT,net TEXT,calibration_revision INTEGER,status TEXT,reason TEXT,source_id INTEGER REFERENCES source_blob(id));
CREATE TABLE IF NOT EXISTS quota_account(vessel TEXT,species TEXT,capacity TEXT NOT NULL,used TEXT NOT NULL,source_id INTEGER REFERENCES source_blob(id),PRIMARY KEY(vessel,species));
CREATE TABLE IF NOT EXISTS release_decision(crate_id TEXT PRIMARY KEY,status TEXT NOT NULL,weight TEXT,reason TEXT NOT NULL,ticket_id TEXT,source_id INTEGER REFERENCES source_blob(id));
CREATE TABLE IF NOT EXISTS release_event(id INTEGER PRIMARY KEY,crate_id TEXT,status TEXT,weight TEXT,reason TEXT,source_id INTEGER REFERENCES source_blob(id));
"""

def connect(path):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(p)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db
