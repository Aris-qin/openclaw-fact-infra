"""Offline, checkable legacy-central handoff. Never publishes or switches work files."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from pathlib import Path

from plugin.v3 import _check_namespace, _connect, _legacy_identity, _logical_manifest, migrate_candidate, now, verify_candidate


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _source_fingerprint(path):
    con=sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
    try:
        con.execute("BEGIN")
        version=con.execute("PRAGMA user_version").fetchone()[0]
        return {"logical_sha256":hashlib.sha256(_json([version,list(con.iterdump())]).encode()).hexdigest()}
    finally: con.close()


def _fresh(plan):
    if _source_fingerprint(plan["input_index"]) != plan["central_fingerprint"]:
        raise ValueError("old central index changed")
    for entry in plan["entries"]:
        if _source_fingerprint(entry["source_path"]) != entry["source_fingerprint"]:
            raise ValueError("legacy source changed")


def _prepared_result(output, plan):
    return {"ok":True,"handoff_dir":str(output.resolve()),"entries":plan["entries"],"registered":False,"switched":False}


def _backup(source, target):
    src = sqlite3.connect(f"file:{Path(source).resolve()}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close(); src.close()


def _rows(con, table):
    con.row_factory = sqlite3.Row
    return [dict(row) for row in con.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]


def _manifest_refresh(candidate, extra):
    path = Path(candidate); manifest_path = path.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    con = sqlite3.connect(path); con.execute("PRAGMA wal_checkpoint(TRUNCATE)"); con.close()
    con = _connect(path)
    manifest["logical"] = _logical_manifest(con); con.close()
    manifest.update(extra)
    manifest["candidate_sha256"] = _sha(path)
    raw = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    manifest_path.write_bytes(raw)
    (path.parent / "manifest.sha256").write_text(hashlib.sha256(raw).hexdigest() + "\n")
    return manifest


def _load_existing(output, old_index, destinations):
    plan_path = output / "handoff-plan.json"
    result_path = output / "prepare-result.json"
    if not plan_path.is_file() or not result_path.is_file():
        raise ValueError("handoff output exists but is incomplete")
    plan_raw = plan_path.read_bytes(); plan = json.loads(plan_raw)
    if (output / "handoff-plan.sha256").read_text().strip() != hashlib.sha256(plan_raw).hexdigest():
        raise ValueError("corrupted handoff plan")
    if plan["input_index"] != str(Path(old_index).resolve()) or plan["destinations"] != destinations:
        raise ValueError("conflicting handoff retry")
    if _sha(output / "central-backup.db") != plan["central_backup_sha256"]:
        raise ValueError("corrupted central backup")
    _fresh(plan)
    result=_prepared_result(output,plan)
    if json.loads(result_path.read_text()) != result:
        raise ValueError("corrupted prepare result")
    verified=verify_handoff(output)
    if not verified["ok"]: raise ValueError(str(verified))
    return result


def prepare_handoff(old_index, output_dir, destinations):
    old_index, output = Path(old_index), Path(output_dir)
    destinations = {str(k): str(Path(v).resolve()) for k, v in destinations.items()}
    if output.exists():
        return _load_existing(output, old_index, destinations)
    if not old_index.is_file(): raise ValueError("missing old central index")
    central_before=_source_fingerprint(old_index)
    stage = output.parent / (".handoff-" + uuid.uuid4().hex)
    stage.mkdir(parents=True)
    try:
        central_backup = stage / "central-backup.db"; _backup(old_index, central_backup)
        central = sqlite3.connect(f"file:{central_backup}?mode=ro", uri=True); central.row_factory = sqlite3.Row
        tables = {r[0] for r in central.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "project_registry" not in tables: raise ValueError("old central index has no project_registry")
        registry = _rows(central, "project_registry")
        if set(destinations) != {str(r.get("slug")) for r in registry}:
            raise ValueError("destinations must explicitly cover every registry slug")
        preflight = {}
        finals=[Path(value) for value in destinations.values()]
        if len(set(finals))!=len(finals): raise ValueError("duplicate final destination")
        sources=[old_index.resolve(),*[Path(row.get("db_path") or "").resolve() for row in registry]]
        for final in finals:
            if final == output.resolve() or output.resolve() in final.parents or final in output.resolve().parents:
                raise ValueError("destination overlaps handoff artifacts")
            for source in sources:
                if final == source or final in source.parents or source in final.parents or str(final) in {str(source)+"-wal",str(source)+"-shm"}:
                    raise ValueError("destination overlaps source/index")
            if any(final in other.parents for other in finals if other!=final): raise ValueError("overlapping destinations")
        for row in registry:
            slug=str(row["slug"])
            if not slug or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for char in slug):
                raise ValueError("unsafe registry slug")
            source = Path(row.get("db_path") or "")
            if not source.is_file(): raise ValueError(f"missing source for {row.get('slug')}")
            src = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True); src.row_factory=sqlite3.Row
            identity = _legacy_identity(src, source); src.close()
            for alias in {str(row["slug"]), identity["slug"]}:
                if alias in preflight and preflight[alias] != str(row["slug"]):
                    raise ValueError(f"alias collision: {alias}")
            preflight[alias] = str(row["slug"])
        associations={str(row["slug"]):{"source_path":str(Path(row["db_path"]).resolve()),"central_backup_sha256":_sha(central_backup)} for row in registry}
        (stage/"central-associations.json").write_text(_json(associations))
        entries = []; identities = []
        for row in registry:
            source = Path(row.get("db_path") or "")
            if not source.is_file(): raise ValueError(f"missing source for {row.get('slug')}")
            source_before=_source_fingerprint(source)
            entry_dir = stage / "candidates" / str(row["slug"])
            candidate = Path(migrate_candidate(source, entry_dir))
            if _source_fingerprint(source)!=source_before: raise ValueError(f"source changed during handoff: {row.get('slug')}")
            con = _connect(candidate, True); con.execute("BEGIN IMMEDIATE")
            work = con.execute("SELECT id,slug,name FROM work").fetchone()
            unresolved = json.loads((entry_dir / "manifest.json").read_text()).get("unresolved_refs", [])
            parentless = [dict(r) for r in con.execute("SELECT lm.old_id, i.id AS item_id, i.legacy_payload FROM items i JOIN legacy_map lm ON lm.new_id=i.id WHERE i.kind='task' AND i.parent_id IS NULL")]
            report = {"counts":{"parentless_tasks":len(parentless),"unresolved_refs":len(unresolved)},
                      "parentless_tasks":[{"legacy_id":r["old_id"],"item_id":r["item_id"],"raw":json.loads(r["legacy_payload"])} for r in parentless],
                      "unresolved_refs":unresolved,"acknowledged":False,"resolved":False}
            snapshot = {"raw":row,"registry_slug":str(row["slug"]),"internal_slug":work[1],"source_path":str(source.resolve()),"central_backup_sha256":_sha(central_backup)}
            if con.execute("SELECT 1 FROM legacy_meta WHERE key IN ('registry_snapshot','migration_review_report')").fetchone():
                raise ValueError("reserved legacy metadata collision")
            con.execute("UPDATE work SET name=?", (row.get("name") or work[2],))
            con.execute("INSERT OR REPLACE INTO legacy_meta VALUES('registry_snapshot',?)", (_json(snapshot),))
            con.execute("INSERT OR REPLACE INTO legacy_meta VALUES('migration_review_report',?)", (_json(report),))
            con.commit(); con.close()
            manifest = _manifest_refresh(candidate, {"registry_snapshot":snapshot,"migration_review_report":report})
            verified = verify_candidate(candidate)
            if not verified.get("ok"): raise ValueError(f"candidate verification failed for {row['slug']}: {verified}")
            entry = {"registry_slug":str(row["slug"]),"canonical_slug":work[1],"work_id":work[0],
                     "candidate_db":str((output / "candidates" / str(row["slug"]) / "fact.db").resolve()),
                     "final_db":destinations[str(row["slug"])],"candidate_sha256":manifest["candidate_sha256"],
                     "source_path":str(source),"source_backup_sha256":manifest["source_sha256"],
                     "source_fingerprint":source_before,
                     "registry_status":row.get("status"),"aliases":sorted({str(row["slug"]),work[1]})}
            entries.append(entry); identities.append(entry)
        central.close()
        claimed = {}
        for entry in identities:
            for value in {entry["work_id"], *entry["aliases"]}:
                if value in claimed and claimed[value] != entry["work_id"]:
                    raise ValueError(f"alias/identity collision: {value}")
                claimed[value] = entry["work_id"]
        plan = {"version":1,"input_index":str(old_index.resolve()),"central_backup_sha256":_sha(central_backup),
                "central_fingerprint":central_before,"destinations":destinations,"entries":entries,"publication":"operator copies verified candidates to explicit final_db paths; this command never switches files"}
        _fresh(plan)
        raw = (json.dumps(plan,ensure_ascii=False,sort_keys=True,indent=2)+"\n").encode()
        (stage/"handoff-plan.json").write_bytes(raw); (stage/"handoff-plan.sha256").write_text(hashlib.sha256(raw).hexdigest()+"\n")
        result = _prepared_result(output,plan)
        (stage/"prepare-result.json").write_text(json.dumps(result,ensure_ascii=False,sort_keys=True,indent=2)+"\n")
        os.replace(stage, output)
        return result
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _registry_schema(con):
    con.execute("CREATE TABLE IF NOT EXISTS work_registry(work_id TEXT PRIMARY KEY,slug TEXT UNIQUE,name TEXT,kind TEXT,root_path TEXT,db_path TEXT,purpose_excerpt TEXT,status TEXT,last_activity_at TEXT,observed_revision INTEGER,indexed_at TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS work_aliases(alias TEXT PRIMARY KEY,work_id TEXT NOT NULL REFERENCES work_registry(work_id))")
    con.execute("CREATE TABLE IF NOT EXISTS handoff_receipts(request_id TEXT PRIMARY KEY,payload_hash TEXT NOT NULL,response_json TEXT NOT NULL)")


def register_handoff(handoff_dir, target_index, request_id):
    try:
        root, target = Path(handoff_dir), Path(target_index)
        raw = (root/"handoff-plan.json").read_bytes()
        if (root/"handoff-plan.sha256").read_text().strip()!=hashlib.sha256(raw).hexdigest():
            raise RuntimeError("CORRUPT_PLAN")
        plan=json.loads(raw); payload_hash=hashlib.sha256(raw).hexdigest()
        if _sha(root/"central-backup.db") != plan["central_backup_sha256"]: raise RuntimeError("CORRUPT_BACKUP")
        protected={Path(plan["input_index"]).resolve(),*[Path(entry[key]).resolve() for entry in plan["entries"] for key in ("source_path","candidate_db","final_db")]}
        protected_artifacts={Path(str(path)+suffix) for path in protected for suffix in ("","-wal","-shm")}
        if target.resolve() in protected_artifacts or target.resolve()==root.resolve() or root.resolve() in target.resolve().parents:
            raise ValueError("target index overlaps handoff/source/final artifacts")
        target.parent.mkdir(parents=True,exist_ok=True); con=sqlite3.connect(target); con.execute("PRAGMA foreign_keys=ON")
        con.execute("BEGIN IMMEDIATE"); _registry_schema(con)
        prior=con.execute("SELECT payload_hash,response_json FROM handoff_receipts WHERE request_id=?",(request_id,)).fetchone()
        if prior:
            if prior[0]!=payload_hash: raise RuntimeError("REQUEST_CONFLICT")
            con.rollback(); con.close()
            return json.loads(prior[1])
        _fresh(plan)
        verified=verify_handoff(root)
        if not verified["ok"]: raise RuntimeError("FINAL_MISMATCH")
        verified=verify_handoff(root,final=True)
        if not verified["ok"]: raise RuntimeError("FINAL_MISMATCH")
        checked=[]
        central=sqlite3.connect(f"file:{(root/'central-backup.db').resolve()}?mode=ro",uri=True); central.row_factory=sqlite3.Row
        registry={str(r["slug"]):dict(r) for r in central.execute("SELECT * FROM project_registry")}
        for entry in plan["entries"]:
            final=Path(entry["final_db"])
            if not final.is_file() or _sha(final)!=entry["candidate_sha256"]: raise RuntimeError("FINAL_MISMATCH")
            db=_connect(final); work=db.execute("SELECT id,slug,name,purpose,kind,status,revision,updated_at FROM work").fetchone()
            snapshot=json.loads(db.execute("SELECT value FROM legacy_meta WHERE key='registry_snapshot'").fetchone()[0])
            if (work[0]!=entry["work_id"] or work[1]!=entry["canonical_slug"]
                    or work[2]!=(registry[entry["registry_slug"]].get("name") or work[2])
                    or snapshot["raw"]!=registry[entry["registry_slug"]]):
                db.close(); raise RuntimeError("FINAL_MISMATCH")
            integrity=db.execute("PRAGMA integrity_check").fetchone()[0]; fk=db.execute("PRAGMA foreign_key_check").fetchall(); db.close()
            if integrity!="ok" or fk: raise RuntimeError("FINAL_MISMATCH")
            checked.append((entry,work))
        central.close()
        for entry,work in checked:
            _check_namespace(con,work[0],work[1],entry["final_db"],entry["aliases"])
            if con.execute("SELECT 1 FROM work_registry WHERE work_id=?",(work[0],)).fetchone(): raise RuntimeError("IDENTITY_CONFLICT")
            rawrow=registry[entry["registry_slug"]]
            con.execute("INSERT INTO work_registry VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(work_id) DO UPDATE SET slug=excluded.slug,name=excluded.name,kind=excluded.kind,root_path=excluded.root_path,db_path=excluded.db_path,purpose_excerpt=excluded.purpose_excerpt,status=excluded.status,last_activity_at=excluded.last_activity_at,observed_revision=excluded.observed_revision,indexed_at=excluded.indexed_at",
                        (work[0],work[1],work[2],work[4],str(Path(entry["final_db"]).parent),str(Path(entry["final_db"]).resolve()),work[3][:200],work[5],rawrow.get("last_activity_at"),work[6],now()))
            for alias in entry["aliases"]: con.execute("INSERT OR REPLACE INTO work_aliases VALUES(?,?)",(alias,work[0]))
        if os.environ.get("FACT_HANDOFF_FAULT")=="before_commit": raise RuntimeError("INJECTED_FAILURE")
        response={"ok":True,"registered":len(checked),"request_id":request_id,"derived_catalog":"rebuild with fact_maintenance.py reindex; registration is already committed"}
        con.execute("INSERT INTO handoff_receipts VALUES(?,?,?)",(request_id,payload_hash,_json(response))); con.commit(); con.close()
        return response
    except Exception as exc:
        try: con.rollback(); con.close()
        except Exception: pass
        code=str(exc) if str(exc) in {"FINAL_MISMATCH","ALIAS_CONFLICT","IDENTITY_CONFLICT","REQUEST_CONFLICT","CORRUPT_PLAN","CORRUPT_BACKUP"} else "REGISTRATION_FAILED"
        return {"ok":False,"error":{"code":code,"message":str(exc)}}


def verify_handoff(handoff_dir, final=False):
    root=Path(handoff_dir)
    try:
        raw=(root/"handoff-plan.json").read_bytes(); plan=json.loads(raw)
        if (root/"handoff-plan.sha256").read_text().strip()!=hashlib.sha256(raw).hexdigest(): raise ValueError("corrupted handoff plan")
        if _sha(root/"central-backup.db")!=plan["central_backup_sha256"]: raise ValueError("corrupted central backup")
        results=[]
        for entry in plan["entries"]:
            path=Path(entry["final_db"] if final else entry["candidate_db"])
            if not path.is_file(): raise ValueError(f"missing {'final' if final else 'candidate'} DB: {path}")
            if _sha(path)!=entry["candidate_sha256"]: raise ValueError(f"changed DB: {path}")
            result=verify_candidate(path,Path(entry["candidate_db"]).parent)
            if not result.get("ok"): raise ValueError(f"candidate independent verification failed: {result}")
            results.append({"registry_slug":entry["registry_slug"],"path":str(path),"verified":True})
        return {"ok":True,"mode":"final" if final else "candidate","entries":results}
    except Exception as exc:
        return {"ok":False,"error":{"code":"HANDOFF_VERIFY_FAILED","message":str(exc)}}
