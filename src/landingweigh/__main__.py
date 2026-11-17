import argparse
import json
import tempfile
from pathlib import Path
from .domain import trip_import,weigh_import,quota_import
from .report import report,report_json

def demo(workdir=None):
    root=Path(workdir or tempfile.mkdtemp(prefix="landingweigh-"))
    root.mkdir(parents=True,exist_ok=True)
    fixtures=Path.cwd()/"fixtures"
    db=root/"landing.db"
    trip_import(db,fixtures/"trip_normal.json")
    weigh_import(db,fixtures/"weigh_normal.json")
    quota_import(db,fixtures/"quota_normal.json")
    return {"db":str(db),"release":[{"crate":r["crate_id"],"status":r["status"]} for r in report(db)["release_decision"]]}

def main():
    parser=argparse.ArgumentParser()
    subs=parser.add_subparsers(dest="command",required=True)
    for name in ("trip-import","weigh-import","quota-import"):
        sub=subs.add_parser(name);sub.add_argument("--db",required=True);sub.add_argument("--file",required=True)
    d=subs.add_parser("demo");d.add_argument("--workdir")
    r=subs.add_parser("report");r.add_argument("--db",required=True)
    args=parser.parse_args()
    if args.command=="demo":print(json.dumps(demo(args.workdir),ensure_ascii=False))
    elif args.command=="report":print(report_json(args.db))
    else:print(json.dumps({"trip-import":trip_import,"weigh-import":weigh_import,"quota-import":quota_import}[args.command](args.db,args.file),ensure_ascii=False))
if __name__=="__main__":main()
