#!/usr/bin/env python3
"""Offline v3 inventory/candidate migration/verification; never switches live paths."""
import argparse, json, sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from plugin.v3 import migrate_candidate, verify_candidate, rebuild_catalog
from scripts.central_handoff import prepare_handoff, register_handoff, verify_handoff

def inventory(root):
    result=[]
    for path in sorted(Path(root).rglob("fact.db")):
        row={"path":str(path),"size":path.stat().st_size,"schema":None,"integrity":"unavailable","wal":Path(str(path)+"-wal").exists()}
        if row["size"]:
            try:
                con=sqlite3.connect(f"file:{path}?mode=ro",uri=True); row["schema"]=con.execute("PRAGMA user_version").fetchone()[0]; row["integrity"]=con.execute("PRAGMA integrity_check").fetchone()[0]; con.close()
            except Exception as exc: row["error"]=str(exc)
        else: row["error"]="zero-byte database"
        result.append(row)
    return result

def main():
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest="command",required=True)
    x=sub.add_parser("inventory"); x.add_argument("--workspace",required=True)
    x=sub.add_parser("migrate"); x.add_argument("--source",required=True); x.add_argument("--candidate",required=True)
    x=sub.add_parser("verify"); x.add_argument("--work",required=True)
    x=sub.add_parser("reindex"); x.add_argument("--index",required=True)
    x=sub.add_parser("handoff-prepare"); x.add_argument("--old-index",required=True); x.add_argument("--output",required=True); x.add_argument("--destinations",required=True)
    x=sub.add_parser("handoff-verify"); x.add_argument("--handoff",required=True); x.add_argument("--final",action="store_true")
    x=sub.add_parser("handoff-register"); x.add_argument("--handoff",required=True); x.add_argument("--index",required=True); x.add_argument("--request-id",required=True)
    a=p.parse_args()
    try:
        if a.command=="inventory": out={"ok":True,"works":inventory(a.workspace)}
        elif a.command=="migrate": out={"ok":True,"candidate":migrate_candidate(a.source,a.candidate),"switched":False}
        elif a.command=="verify": out=verify_candidate(a.work)
        elif a.command=="reindex": out={"ok":True,"catalog":rebuild_catalog(a.index)}
        elif a.command=="handoff-prepare": out=prepare_handoff(a.old_index,a.output,json.loads(Path(a.destinations).read_text()))
        elif a.command=="handoff-verify": out=verify_handoff(a.handoff,a.final)
        else: out=register_handoff(a.handoff,a.index,a.request_id)
    except Exception as exc:
        out={"ok":False,"error":{"code":"MAINTENANCE_FAILED","message":str(exc)}}
    print(json.dumps(out,ensure_ascii=False,indent=2))
    if not out.get("ok",False): raise SystemExit(1)
if __name__=="__main__": main()
