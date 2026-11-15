"""fact-infra v3 service: five strict tools over one SQLite database per work."""
from __future__ import annotations

import hashlib
import json
import math
import os
import base64
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 3
MAX_BATCH = 100
MAX_TEXT = 200_000
MAX_STRING = 10_000
MAX_ID = 256
MAX_JSON_BYTES = 1_000_000
MAX_JSON_DEPTH = 12
MAX_JSON_NODES = 5_000
ITEM_STATUSES = {"planned", "active", "blocked", "awaiting_confirmation", "done", "cancelled"}
RECORD_FIELDS = {
    "issue": {"severity", "hypothesis", "workaround", "resolution", "issue_key"},
    "decision": {"summary", "rationale", "decision_key"},
    "experiment": {"input_config", "expected_result", "actual_result", "verdict", "run_at"},
    "result": {"result", "evidence"},
    "note": set(),
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def ok(data=None, **extra):
    return {"ok": True, "data": data or {}, **extra}


def err(code, message, details=None, retryable=False):
    return {"ok": False, "error": {"code": code, "message": message, "details": details or {}, "retryable": retryable}}


def dumps(value):
    return json.dumps(value, ensure_ascii=False, indent=1)


def _strict(args, allowed, required=()):
    unknown = set(args) - set(allowed)
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")
    missing = [k for k in required if k not in args or args[k] in (None, "")]
    if missing:
        raise ValueError(f"missing fields: {missing}")


def _bounded_json(value):
    nodes = 0
    def walk(node, depth):
        nonlocal nodes
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise ValueError("payload nesting/node limit exceeded")
        if isinstance(node, float) and not math.isfinite(node):
            raise ValueError("non-finite numbers are forbidden")
        if isinstance(node, str) and len(node) > MAX_TEXT:
            raise ValueError("string too long")
        if isinstance(node, dict):
            if any(not isinstance(k, str) for k in node):
                raise ValueError("object keys must be strings")
            for child in node.values(): walk(child, depth + 1)
        elif isinstance(node, list):
            for child in node: walk(child, depth + 1)
        elif node is not None and not isinstance(node, (str, int, float, bool)):
            raise ValueError("value is not JSON-compatible")
    walk(value, 0)
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > MAX_JSON_BYTES:
        raise ValueError("serialized payload too large")


def _validate_public(name, args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    _bounded_json(args)
    schema = next((tool["parameters"] for tool in schemas() if tool["name"] == name), None)
    if schema is None: raise ValueError("unknown tool")
    _validate_schema_value(args, schema, name)
    for key, value in args.items():
        if key in {"request_id", "work", "proposal_id", "record_id", "item_id", "target_id", "id", "cursor"}:
            if value is not None and (not isinstance(value, str) or not value or len(value) > MAX_ID):
                raise ValueError(f"{key} must be a non-empty bounded string")
    if name == "fact_create":
        _strict(args, {"action","slug","name","purpose","kind","root_path","request_id"},
                {"action","slug","purpose","kind","request_id"})
        if args.get("action") != "create" or args.get("kind") not in {"project", "light"}:
            raise ValueError("invalid create action/kind")
        for key, limit in (("slug", 100), ("name", 500), ("purpose", 10_000), ("root_path", 4096)):
            if key in args and (not isinstance(args[key], str) or not args[key] or len(args[key]) > limit):
                raise ValueError(f"{key} must be a non-empty bounded string")
    elif name == "fact_read":
        action = args.get("action")
        allowed = {
            "overview": {"action"},
            "current": {"action", "work", "limit"},
            "detail": {"action", "work", "id", "collection", "limit", "cursor"},
            "history": {"action", "work", "limit", "cursor"},
        }
        if action not in allowed:
            raise ValueError("invalid read action")
        _strict(args, allowed[action], () if action == "overview" else ("work",))
        if action == "detail" and bool(args.get("id")) == bool(args.get("collection")):
            raise ValueError("detail requires exactly one of id or collection")
        if action == "detail" and args.get("id") and ({"limit", "cursor"} & set(args)):
            raise ValueError("limit/cursor are only valid for detail collections")
    elif name == "fact_record":
        for key in ("title", "body", "resolution", "reason"):
            if key in args and not isinstance(args[key], str):
                raise ValueError(f"{key} must be a string")
        if "expected_version" in args and (isinstance(args["expected_version"], bool) or not isinstance(args["expected_version"], int)):
            raise ValueError("expected_version must be an integer")


def _validate_schema_value(value, schema, path):
    types=schema.get("type")
    if types:
        types=[types] if isinstance(types,str) else types
        matches=("object" in types and isinstance(value,dict)) or ("array" in types and isinstance(value,list)) or ("string" in types and isinstance(value,str)) or ("integer" in types and isinstance(value,int) and not isinstance(value,bool)) or ("number" in types and isinstance(value,(int,float)) and not isinstance(value,bool)) or ("boolean" in types and isinstance(value,bool)) or ("null" in types and value is None)
        if not matches: raise ValueError(f"{path} has invalid type")
    if "const" in schema and value != schema["const"]: raise ValueError(f"{path} has invalid value")
    if "enum" in schema and value not in schema["enum"]: raise ValueError(f"{path} has invalid value")
    if isinstance(value,str):
        if len(value)<schema.get("minLength",0) or len(value)>schema.get("maxLength",len(value)): raise ValueError(f"{path} has invalid length")
    if isinstance(value,int) and not isinstance(value,bool):
        if "minimum" in schema and value<schema["minimum"]: raise ValueError(f"{path} is below minimum")
        if "maximum" in schema and value>schema["maximum"]: raise ValueError(f"{path} is above maximum")
    if isinstance(value,list):
        if len(value)<schema.get("minItems",0) or len(value)>schema.get("maxItems",len(value)): raise ValueError(f"{path} has invalid item count")
        for i,item in enumerate(value): _validate_schema_value(item,schema.get("items",{}),f"{path}[{i}]")
    if isinstance(value,dict):
        required=schema.get("required",[]); missing=[key for key in required if key not in value]
        if missing: raise ValueError(f"{path} missing fields: {missing}")
        props=schema.get("properties",{})
        if schema.get("additionalProperties") is False:
            unknown=set(value)-set(props)
            if unknown: raise ValueError(f"{path} unknown fields: {sorted(unknown)}")
        if len(value)>schema.get("maxProperties",len(value)): raise ValueError(f"{path} has too many properties")
        for key,item in value.items():
            if key in props: _validate_schema_value(item,props[key],f"{path}.{key}")
    for clause in schema.get("allOf",[]):
        condition=clause.get("if")
        applies=True
        if condition:
            try: _validate_schema_value(value,condition,path)
            except ValueError: applies=False
        if applies and "then" in clause: _validate_schema_value(value,clause["then"],path)
    if "not" in schema:
        try: _validate_schema_value(value,schema["not"],path)
        except ValueError: pass
        else: raise ValueError(f"{path} contains fields forbidden for this action")
    if "anyOf" in schema:
        for option in schema["anyOf"]:
            try: _validate_schema_value(value,option,path); break
            except ValueError: continue
        else: raise ValueError(f"{path} does not match any allowed shape")
    if "oneOf" in schema:
        matches=0
        for option in schema["oneOf"]:
            try: _validate_schema_value(value,option,path); matches+=1
            except ValueError: pass
        if matches != 1: raise ValueError(f"{path} must match exactly one allowed shape")


def _connect(path, write=False):
    timeout = float(os.environ.get("FACT_SQLITE_TIMEOUT", "30"))
    con = sqlite3.connect(path, timeout=timeout)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    if write:
        con.execute("PRAGMA journal_mode=WAL")
    version = con.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        con.close()
        raise RuntimeError("UNSUPPORTED_SCHEMA")
    if version < SCHEMA_VERSION:
        con.close()
        raise RuntimeError("MIGRATION_REQUIRED")
    return con


SCHEMA = """
CREATE TABLE work(id TEXT PRIMARY KEY,slug TEXT UNIQUE NOT NULL,name TEXT NOT NULL,purpose TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN('project','light')),status TEXT NOT NULL DEFAULT 'active',revision INTEGER NOT NULL DEFAULT 0,
 root_items_version INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE items(id TEXT PRIMARY KEY,kind TEXT NOT NULL CHECK(kind IN('goal','task')),parent_id TEXT REFERENCES items(id),
 title TEXT NOT NULL,description TEXT,status TEXT NOT NULL DEFAULT 'planned',position INTEGER NOT NULL,version INTEGER NOT NULL DEFAULT 1,
 children_version INTEGER NOT NULL DEFAULT 0,completed_at TEXT,removed_at TEXT,legacy_payload TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE dependencies(item_id TEXT REFERENCES items(id),requires_id TEXT REFERENCES items(id),PRIMARY KEY(item_id,requires_id));
CREATE TABLE records(id TEXT PRIMARY KEY,kind TEXT NOT NULL,title TEXT NOT NULL,body TEXT NOT NULL,payload_json TEXT NOT NULL,status TEXT NOT NULL,
 issue_key TEXT UNIQUE,decision_key TEXT UNIQUE,supersedes_id TEXT REFERENCES records(id),version INTEGER NOT NULL DEFAULT 1,
 legacy_payload TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE record_links(record_id TEXT REFERENCES records(id),item_id TEXT REFERENCES items(id),relation TEXT NOT NULL,
 UNIQUE(record_id,item_id,relation));
CREATE UNIQUE INDEX record_work_link ON record_links(record_id,relation) WHERE item_id IS NULL;
CREATE TABLE record_relations(source_id TEXT REFERENCES records(id),target_id TEXT REFERENCES records(id),relation TEXT NOT NULL,
 PRIMARY KEY(source_id,target_id,relation));
CREATE TABLE file_refs(id TEXT PRIMARY KEY,record_id TEXT REFERENCES records(id),item_id TEXT REFERENCES items(id),locator TEXT NOT NULL,
 storage TEXT NOT NULL,role TEXT NOT NULL,purpose TEXT NOT NULL,handoff_status TEXT NOT NULL DEFAULT 'not_applicable',corpus_id TEXT,
 source_locator TEXT,created_at TEXT NOT NULL);
CREATE TABLE proposals(id TEXT PRIMARY KEY,kind TEXT NOT NULL,payload_json TEXT NOT NULL,base_versions_json TEXT NOT NULL,
 review_summary TEXT,status TEXT NOT NULL,source_session TEXT,confirmation_json TEXT,created_at TEXT NOT NULL,applied_at TEXT);
CREATE TABLE history(seq INTEGER PRIMARY KEY AUTOINCREMENT,operation TEXT NOT NULL,target_type TEXT,target_id TEXT,before_json TEXT,
 after_json TEXT,request_id TEXT,proposal_id TEXT,source_session TEXT,occurred_at TEXT NOT NULL);
CREATE TRIGGER history_no_update BEFORE UPDATE ON history BEGIN SELECT RAISE(ABORT,'history is append-only'); END;
CREATE TRIGGER history_no_delete BEFORE DELETE ON history BEGIN SELECT RAISE(ABORT,'history is append-only'); END;
CREATE TABLE request_receipts(request_id TEXT PRIMARY KEY,payload_hash TEXT NOT NULL,response_json TEXT NOT NULL,committed_revision INTEGER NOT NULL);
CREATE TABLE legacy_map(old_table TEXT,old_id TEXT,old_key TEXT,new_type TEXT,new_id TEXT,PRIMARY KEY(old_table,old_id));
CREATE TABLE legacy_meta(key TEXT PRIMARY KEY,value TEXT);
"""


def _init_db(path, row):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    con.execute("INSERT INTO work VALUES(?,?,?,?,?,'active',0,0,?,?)",
                (row["id"], row["slug"], row["name"], row["purpose"], row["kind"], row["created_at"], row["created_at"]))
    con.commit(); con.close()


def _resolve(work):
    if not isinstance(work, str) or not work:
        raise ValueError("work required")
    p = Path(str(work)).expanduser()
    if p.is_dir(): p = p / "fact.db"
    if not p.is_file():
        index = _index_path()
        if index.is_file():
            con = sqlite3.connect(f"file:{index}?mode=ro", uri=True)
            row = con.execute("SELECT db_path FROM work_registry WHERE slug=? OR work_id=?", (work, work)).fetchone()
            if not row and "work_aliases" in {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
                row = con.execute("SELECT r.db_path FROM work_aliases a JOIN work_registry r ON r.work_id=a.work_id WHERE a.alias=?", (work,)).fetchone()
            con.close()
            if row: p = Path(row[0])
    if not p.is_file(): raise FileNotFoundError(str(p))
    return str(p.resolve())


def _receipt(con, request_id, payload):
    if not request_id: raise ValueError("request_id required")
    row = con.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
    if not row: return None
    if row["payload_hash"] != _hash(payload): raise RuntimeError("REQUEST_CONFLICT")
    return json.loads(row["response_json"])


def _commit(con, request_id, payload, response, operation, target_type, target_id, proposal_id=None, session=None, before=None, after=None):
    con.execute("UPDATE work SET revision=revision+1,updated_at=?", (now(),))
    revision = con.execute("SELECT revision FROM work").fetchone()[0]
    response["revision"] = revision
    con.execute("INSERT INTO history(operation,target_type,target_id,before_json,after_json,request_id,proposal_id,source_session,occurred_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (operation,target_type,target_id,_json(before) if before is not None else None,_json(after) if after is not None else None,request_id,proposal_id,session,now()))
    if os.environ.get("FACT_FAULT_POINT")=="history": raise RuntimeError("INJECTED_HISTORY_FAILURE")
    con.execute("INSERT INTO request_receipts VALUES(?,?,?,?)", (request_id,_hash(payload),_json(response),revision))
    if os.environ.get("FACT_FAULT_POINT")=="receipt": raise RuntimeError("INJECTED_RECEIPT_FAILURE")
    return response


def _sqlite_error(exc):
    message = str(exc)
    if "locked" in message.lower() or "busy" in message.lower():
        return err("DB_BUSY", message, retryable=True)
    return err("VALIDATION_ERROR", message)


def _entity_state(con):
    """Small-database entity state used to retain factual, reviewable history."""
    result = {}
    for table, order in (("work", "id"), ("items", "id"), ("dependencies", "item_id,requires_id"),
                         ("records", "id"), ("record_links", "record_id,item_id,relation"),
                         ("record_relations", "source_id,target_id,relation"), ("file_refs", "id"),
                         ("proposals", "id")):
        rows = [dict(r) for r in con.execute(f"SELECT * FROM {table} ORDER BY {order}")]
        result[table] = {str(r.get("id") or "|".join(str(r.get(k)) for k in r)): r for r in rows}
    return result


def _entity_diff(before, after):
    old, new = {}, {}
    for table in before:
        keys = set(before[table]) | set(after[table])
        changed = [key for key in keys if before[table].get(key) != after[table].get(key)]
        if changed:
            old[table] = {key: before[table].get(key) for key in sorted(changed)}
            new[table] = {key: after[table].get(key) for key in sorted(changed)}
    return old, new


def _index_path():
    """Keep the central guide shared across standard Hermes profiles.

    Explicit FACT_INDEX_DB always wins. Standalone/custom homes remain isolated;
    only the conventional <root>/profiles/<name> layout maps to its parent root.
    Resolve the SDK's context-local home in multiplexed gateway processes.
    """
    override = os.environ.get("FACT_INDEX_DB")
    if override:
        return Path(override)
    try:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home())
    except ImportError:
        home = Path(os.environ.get("HERMES_HOME", str(Path.home()/".hermes")))
    if home.parent.name == "profiles":
        home = home.parent.parent
    return home / "fact-index.db"


def rebuild_catalog(index=None):
    index=Path(index) if index else _index_path()
    if not index.is_file(): return None
    con=sqlite3.connect(index); con.row_factory=sqlite3.Row
    rows=[dict(r) for r in con.execute("SELECT * FROM work_registry ORDER BY slug")]
    aliases=[dict(r) for r in con.execute("SELECT * FROM work_aliases ORDER BY alias")] if "work_aliases" in _legacy_tables(con) else []
    con.close()
    target=index.parent/"catalog.json"; tmp=index.parent/(".catalog-"+uuid.uuid4().hex+".tmp")
    tmp.write_text(json.dumps({"version":1,"works":rows,"aliases":aliases},ensure_ascii=False,indent=2)+"\n")
    os.replace(tmp,target)
    return str(target)


@contextmanager
def _write(path):
    con = _connect(path, True)
    try:
        con.execute("BEGIN IMMEDIATE")
        yield con
        con.commit()
    except Exception:
        con.rollback(); raise
    finally: con.close()


def create(args, **ctx):
    path=None; folder=None; stage=None; published=False; catalog=None
    try:
        _strict(args, {"action","slug","name","purpose","kind","root_path","request_id"}, {"action","slug","purpose","kind","request_id"})
        if args["action"] != "create" or args["kind"] not in {"project","light"}: raise ValueError("invalid create action/kind")
        slug = args["slug"]
        if not slug or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in slug): raise ValueError("invalid slug")
        root = Path(args.get("root_path") or os.environ.get("FACT_WORKSPACE_ROOT", "/workspace"))
        folder = root / ("projects" if args["kind"] == "project" else "work") / slug
        path = folder / "fact.db"
        payload = dict(args)
        if path.is_file():
            con = _connect(path); prior = _receipt(con,args["request_id"],payload); con.close()
            if prior:
                _update_catalog(path)
                return dumps(prior)
            raise FileExistsError(str(path))
        t = now(); wid = str(uuid.uuid4())
        index=_index_path(); index.parent.mkdir(parents=True,exist_ok=True)
        catalog=sqlite3.connect(index); catalog.execute("BEGIN IMMEDIATE"); _catalog_schema(catalog)
        if path.is_file():
            con=_connect(path); prior=_receipt(con,args["request_id"],payload); con.close()
            if prior:
                catalog.rollback(); catalog.close(); catalog=None
                _update_catalog(path)
                return dumps(prior)
            raise FileExistsError(str(path))
        _check_namespace(catalog,wid,slug,path)
        folder.parent.mkdir(parents=True, exist_ok=True)
        stage = folder.parent / (".fact-create-" + slug + "-" + uuid.uuid4().hex)
        stage.mkdir()
        staged_path = stage / "fact.db"
        _init_db(staged_path,{"id":wid,"slug":slug,"name":args.get("name") or slug,"purpose":args["purpose"],"kind":args["kind"],"created_at":t})
        response = ok({"created":True},work_id=wid,db_path=str(path.resolve()),revision=1,
                      warnings=[{"code":"catalog_refresh_best_effort","message":"The catalog is derived and may be rebuilt independently from the published local database."}],
                      follow_up=[])
        with _write(str(staged_path)) as con:
            con.execute("UPDATE work SET revision=0")
            _commit(con,args["request_id"],payload,response,"create","work",wid,session=ctx.get("session_id"),after=payload)
        checkpoint=sqlite3.connect(staged_path); checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)"); checkpoint.close()
        fd=os.open(staged_path,os.O_RDONLY); os.fsync(fd); os.close(fd)
        dfd=os.open(stage,os.O_RDONLY); os.fsync(dfd); os.close(dfd)
        if os.environ.get("FACT_FAULT_POINT")=="create_before_publish": os._exit(90)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.link(staged_path, path)
            published=True
            dfd=os.open(folder,os.O_RDONLY); os.fsync(dfd); os.close(dfd)
            shutil.rmtree(stage,ignore_errors=True); stage=None
            pfd=os.open(folder.parent,os.O_RDONLY); os.fsync(pfd); os.close(pfd)
        except OSError:
            if path.is_file():
                catalog.rollback(); catalog.close(); catalog=None
                con=_connect(path); prior=_receipt(con,args["request_id"],payload); con.close()
                if prior:
                    _update_catalog(path)
                    shutil.rmtree(stage,ignore_errors=True); stage=None
                    return dumps(prior)
            raise
        if os.environ.get("FACT_FAULT_POINT")=="create_after_publish": os._exit(91)
        _catalog_upsert(catalog,path); catalog.commit(); catalog.close(); catalog=None
        _update_catalog(path)
        # Catalog refresh is derived, best-effort work. Never mutate the published receipt:
        # the first response and every concurrent/lost-response replay must be byte-equivalent.
        con=_connect(path); persisted=_receipt(con,args["request_id"],payload); con.close()
        return dumps(persisted)
    except RuntimeError as e:
        return dumps(err("CONFLICT" if str(e)=="REQUEST_CONFLICT" else str(e),str(e)))
    except Exception as e:
        if stage and stage.exists() and not published:
            shutil.rmtree(stage,ignore_errors=True)
        return dumps(err("VALIDATION_ERROR",str(e)))
    finally:
        if catalog is not None: catalog.rollback(); catalog.close()


def _catalog_schema(con):
    con.execute("CREATE TABLE IF NOT EXISTS work_registry(work_id TEXT PRIMARY KEY,slug TEXT,name TEXT,kind TEXT,root_path TEXT,db_path TEXT,purpose_excerpt TEXT,status TEXT,last_activity_at TEXT,observed_revision INTEGER,indexed_at TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS work_aliases(alias TEXT PRIMARY KEY,work_id TEXT NOT NULL REFERENCES work_registry(work_id))")


def _check_namespace(con, work_id, slug, path, aliases=()):
    claims={work_id,slug,*aliases}
    for existing_id,existing_slug,existing_path in con.execute("SELECT work_id,slug,db_path FROM work_registry"):
        if existing_id==work_id and str(Path(existing_path).resolve())!=str(Path(path).resolve()):
            raise RuntimeError("IDENTITY_CONFLICT")
        if claims.intersection({existing_id,existing_slug}) and existing_id!=work_id:
            raise RuntimeError("ALIAS_CONFLICT")
    for alias,existing_id in con.execute("SELECT alias,work_id FROM work_aliases"):
        if alias in claims and existing_id!=work_id: raise RuntimeError("ALIAS_CONFLICT")


def _catalog_upsert(con, path):
    with sqlite3.connect(path) as src: row=src.execute("SELECT * FROM work").fetchone()
    _check_namespace(con,row[0],row[1],path)
    previous=con.execute("SELECT slug FROM work_registry WHERE work_id=?",(row[0],)).fetchone()
    if previous and previous[0]!=row[1]: con.execute("INSERT OR IGNORE INTO work_aliases VALUES(?,?)",(previous[0],row[0]))
    con.execute("INSERT INTO work_registry VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(work_id) DO UPDATE SET slug=excluded.slug,name=excluded.name,kind=excluded.kind,root_path=excluded.root_path,db_path=excluded.db_path,purpose_excerpt=excluded.purpose_excerpt,status=excluded.status,last_activity_at=excluded.last_activity_at,observed_revision=excluded.observed_revision,indexed_at=excluded.indexed_at",(row[0],row[1],row[2],row[4],str(Path(path).parent),str(Path(path).resolve()),row[3][:200],row[5],row[9],row[6],now()))


def _update_catalog(path):
    con=None
    try:
        index = _index_path()
        index.parent.mkdir(parents=True,exist_ok=True)
        con = sqlite3.connect(index); con.execute("BEGIN IMMEDIATE"); _catalog_schema(con)
        _catalog_upsert(con,path); con.commit(); con.close(); con=None
        rebuild_catalog(index); return None
    except Exception as exc: return str(exc)
    finally:
        if con is not None: con.rollback(); con.close()


def _validate_change(con, changes):
    if not isinstance(changes,list) or not changes or len(changes)>MAX_BATCH: raise ValueError("changes must be a non-empty bounded array")
    refs={}
    allowed={"add":{"op","ref","kind","parent_id","parent_ref","title","description","position"},"update":{"op","item_id","title","description","position","parent_id"},"remove":{"op","item_id"},"reorder":{"op","item_id","position"},"depend":{"op","item_id","item_ref","requires_id","requires_ref"},"undepend":{"op","item_id","requires_id"},"reopen":{"op","item_id"},"cancel":{"op","item_id"},"work_update":{"op","name","purpose","kind"},"adopt":{"op","acknowledge_migration_review"}}
    execution_changes=sorted(enumerate(changes), key=lambda pair: (
        0 if pair[1]["op"]=="add" and pair[1].get("kind")=="goal" else
        1 if pair[1]["op"]=="add" else 2, pair[0]))
    for _,c in execution_changes:
        if not isinstance(c,dict) or c.get("op") not in allowed: raise ValueError("invalid plan op")
        unknown=set(c)-allowed[c["op"]]
        if unknown: raise ValueError(f"unknown change fields: {sorted(unknown)}")
        if "position" in c and (isinstance(c["position"],bool) or not isinstance(c["position"],int) or c["position"] < 0):
            raise ValueError("position must be a non-negative integer")
        if c.get("ref"):
            if c["ref"] in refs: raise ValueError("duplicate ref")
            refs[c["ref"]]=(c.get("kind"),c)
    graph={}
    for r in con.execute("SELECT item_id,requires_id FROM dependencies"): graph.setdefault(r[0],set()).add(r[1])
    def identity(c,key):
        ref_key = key.replace("_id", "_ref")
        return c.get(key) or ("@" + c[ref_key] if c.get(ref_key) else None)
    for c in changes:
        if c["op"]=="update" and "parent_id" in c:
            item=con.execute("SELECT kind,removed_at FROM items WHERE id=?",(c.get("item_id"),)).fetchone()
            parent=con.execute("SELECT kind,removed_at FROM items WHERE id=?",(c["parent_id"],)).fetchone()
            if not item or item[0]!="task" or item[1] is not None: raise ValueError("parent assignment requires an existing task")
            if not parent or parent[0]!="goal" or parent[1] is not None: raise ValueError("parent target must be an existing live goal")
        if c["op"]=="add":
            if c.get("kind") not in {"goal","task"} or not c.get("title"): raise ValueError("add requires kind/title")
            if c["kind"]=="task" and not (c.get("parent_id") or c.get("parent_ref")):
                kind=con.execute("SELECT kind FROM work").fetchone()[0]
                if kind=="project": raise ValueError("project tasks require a goal")
            parent=c.get("parent_id") or c.get("parent_ref")
            if c["kind"]=="goal" and parent: raise ValueError("goals cannot be nested")
            if c.get("parent_ref") and (c["parent_ref"] not in refs or refs[c["parent_ref"]][0]!="goal"): raise ValueError("invalid parent ref")
        if c["op"]=="depend":
            a=identity(c,"item_id"); b=identity(c,"requires_id")
            if not a or not b or a==b: raise ValueError("dependency self/missing")
            def dep_kind(value):
                if value.startswith("@"):
                    ref=value[1:]
                    if ref not in refs: raise ValueError("missing dependency ref")
                    return refs[ref][0]
                row=con.execute("SELECT kind,removed_at FROM items WHERE id=?",(value,)).fetchone()
                if not row or row[1] is not None: raise ValueError("missing/removed dependency item")
                return row[0]
            if dep_kind(a) != dep_kind(b): raise ValueError("mixed-kind dependency")
            graph.setdefault(a,set()).add(b)
        if c["op"]=="undepend": graph.get(c["item_id"],set()).discard(c["requires_id"])
    visiting=set(); done=set()
    def visit(n):
        if n in visiting: raise ValueError("dependency cycle")
        if n in done:return
        visiting.add(n)
        for q in graph.get(n,()): visit(q)
        visiting.remove(n);done.add(n)
    for n in graph: visit(n)


def _plan_snapshot(con, changes):
    """Capture only entities and membership sets that a batch can observe/change."""
    item_ids=set(); parent_ids=set(); root=False
    for c in changes:
        op=c["op"]
        for key in ("item_id","requires_id","parent_id"):
            if c.get(key): item_ids.add(c[key])
        if op=="add":
            if c.get("parent_id"): parent_ids.add(c["parent_id"])
            elif not c.get("parent_ref"): root=True
        elif (op in {"remove","reorder"} or op=="update" and "parent_id" in c) and c.get("item_id"):
            row=con.execute("SELECT parent_id FROM items WHERE id=?",(c["item_id"],)).fetchone()
            if row and row[0]: parent_ids.add(row[0])
            elif row: root=True
            if op=="update": parent_ids.add(c["parent_id"])
    items={}
    for iid in sorted(item_ids | parent_ids):
        row=con.execute("SELECT version,children_version,removed_at FROM items WHERE id=?",(iid,)).fetchone()
        if not row: raise ValueError(f"missing item: {iid}")
        items[iid]=[row[0],row[1],row[2]]
    return {"root":con.execute("SELECT root_items_version FROM work").fetchone()[0] if root else None,
            "items":items,"parents":sorted(parent_ids)}


def plan(args, **ctx):
    try:
        action=args.get("action"); path=_resolve(args.get("work")); session=ctx.get("session_id")
        if action=="propose":
            _strict(args,{"action","work","changes","review_summary","request_id"},{"action","work","changes","request_id"})
            with _write(path) as con:
                prior=_receipt(con,args["request_id"],args)
                if prior:return dumps(prior)
                status=con.execute("SELECT status FROM work").fetchone()[0]
                if status=="imported_unreviewed":
                    if len(args["changes"]) != 1 or args["changes"][0].get("op") != "adopt": raise RuntimeError("ADOPTION_REQUIRED")
                else: _guard_work(con)
                _validate_change(con,args["changes"])
                versions=_plan_snapshot(con,args["changes"])
                pid=str(uuid.uuid4()); con.execute("INSERT INTO proposals VALUES(?,?,?,?,?,'pending',?,NULL,?,NULL)",(pid,"plan",_json(args["changes"]),_json(versions),args.get("review_summary"),session,now()))
                response=ok({"proposal_id":pid,"changes":args["changes"],"needs_user_confirmation":True},warnings=[],follow_up=["Show the exact diff and ask the user to confirm."])
                _commit(con,args["request_id"],args,response,"plan_propose","proposal",pid,pid,session,after=args["changes"])
            return dumps(response)
        if action in {"apply","reject"}:
            _strict(args,{"action","work","proposal_id","confirmation","request_id"},{"action","work","proposal_id","request_id"})
            with _write(path) as con:
                prior=_receipt(con,args["request_id"],args)
                if prior:return dumps(prior)
                p=con.execute("SELECT * FROM proposals WHERE id=?",(args["proposal_id"],)).fetchone()
                if not p: raise LookupError("proposal")
                if p["kind"]!="plan": raise ValueError("proposal is not a plan")
                if p["status"] in {"applied","rejected"}:
                    saved=con.execute("SELECT response_json FROM request_receipts WHERE json_extract(response_json,'$.data.proposal_id')=? AND request_id!=? ORDER BY rowid DESC LIMIT 1",(p["id"],args["request_id"])).fetchone()
                    if saved:return saved[0]
                    raise RuntimeError("STALE_PROPOSAL")
                if p["status"]!="pending": raise RuntimeError("STALE_PROPOSAL")
                work_status=con.execute("SELECT status FROM work").fetchone()[0]
                adoption_changes=json.loads(p["payload_json"])
                is_adoption=len(adoption_changes)==1 and adoption_changes[0].get("op")=="adopt"
                if work_status=="imported_unreviewed" and not is_adoption: raise RuntimeError("ADOPTION_REQUIRED")
                if work_status!="imported_unreviewed": _guard_work(con)
                before_state=_entity_state(con)
                if action=="reject":
                    con.execute("UPDATE proposals SET status='rejected',confirmation_json=?,applied_at=? WHERE id=?",(_json(args.get("confirmation")),now(),p["id"])); response=ok({"proposal_id":p["id"],"status":"rejected"})
                else:
                    if not isinstance(args.get("confirmation"),dict) or not args["confirmation"].get("summary"): raise RuntimeError("CONFIRMATION_REQUIRED")
                    response=_apply_plan(con,p,args,session)
                before,after=_entity_diff(before_state,_entity_state(con))
                _commit(con,args["request_id"],args,response,"plan_"+action,"proposal",p["id"],p["id"],session,before,after)
            _update_catalog(path); return dumps(response)
        raise ValueError("invalid action")
    except RuntimeError as e: return dumps(err("CONFLICT" if str(e)=="REQUEST_CONFLICT" else str(e),str(e)))
    except sqlite3.OperationalError as e:return dumps(_sqlite_error(e))
    except LookupError as e:return dumps(err("NOT_FOUND",str(e)))
    except Exception as e:return dumps(err("VALIDATION_ERROR",str(e)))


def _apply_plan(con,p,args,session):
    base=json.loads(p["base_versions_json"])
    if base.get("root") is not None and con.execute("SELECT root_items_version FROM work").fetchone()[0]!=base["root"]:
        raise RuntimeError("STALE_PROPOSAL")
    for iid, expected in base.get("items",{}).items():
        row=con.execute("SELECT version,children_version,removed_at FROM items WHERE id=?",(iid,)).fetchone()
        if not row or list(row)!=expected: raise RuntimeError("STALE_PROPOSAL")
    changes=json.loads(p["payload_json"])
    _validate_change(con,changes)
    refs={c["ref"]:str(uuid.uuid4()) for c in changes if c["op"]=="add" and c.get("ref")}
    t=now()
    execution_changes=sorted(enumerate(changes), key=lambda pair: (
        0 if pair[1]["op"]=="add" and pair[1].get("kind")=="goal" else
        1 if pair[1]["op"]=="add" else 2, pair[0]))
    for _,c in execution_changes:
        op=c["op"]
        if op=="add":
            iid=refs.get(c.get("ref")) or str(uuid.uuid4()); parent=c.get("parent_id") or refs.get(c.get("parent_ref")); pos=c.get("position")
            if pos is None: pos=con.execute("SELECT COALESCE(MAX(position),0)+1 FROM items WHERE parent_id IS ? AND removed_at IS NULL",(parent,)).fetchone()[0]
            con.execute("INSERT INTO items(id,kind,parent_id,title,description,status,position,created_at,updated_at) VALUES(?,?,?,?,?,'planned',?,?,?)",(iid,c["kind"],parent,c["title"],c.get("description"),pos,t,t))
            if parent: con.execute("UPDATE items SET children_version=children_version+1 WHERE id=?",(parent,))
            else: con.execute("UPDATE work SET root_items_version=root_items_version+1")
        elif op=="depend": con.execute("INSERT INTO dependencies VALUES(?,?)",(c.get("item_id") or refs[c["item_ref"]],c.get("requires_id") or refs[c["requires_ref"]]))
        elif op=="undepend": con.execute("DELETE FROM dependencies WHERE item_id=? AND requires_id=?",(c["item_id"],c["requires_id"]))
        elif op in {"update","reorder","reopen","remove","cancel"}:
            iid=c["item_id"]; row=con.execute("SELECT * FROM items WHERE id=?",(iid,)).fetchone()
            if not row: raise LookupError(iid)
            if op=="remove":
                con.execute("UPDATE items SET removed_at=?,version=version+1,updated_at=? WHERE id=?",(t,t,iid))
                if row["parent_id"]: con.execute("UPDATE items SET children_version=children_version+1 WHERE id=?",(row["parent_id"],))
                else: con.execute("UPDATE work SET root_items_version=root_items_version+1")
            elif op=="cancel": con.execute("UPDATE items SET status='cancelled',version=version+1,updated_at=? WHERE id=?",(t,t,iid))
            elif op=="reopen": con.execute("UPDATE items SET status='planned',completed_at=NULL,version=version+1,updated_at=? WHERE id=?",(t,iid))
            else:
                fields={k:c[k] for k in ("title","description","position","parent_id") if k in c}
                if not fields: raise ValueError("empty update")
                if "parent_id" in fields and fields["parent_id"]!=row["parent_id"]:
                    for parent_id in {row["parent_id"],fields["parent_id"]}:
                        if parent_id: con.execute("UPDATE items SET children_version=children_version+1 WHERE id=?",(parent_id,))
                        else: con.execute("UPDATE work SET root_items_version=root_items_version+1")
                con.execute("UPDATE items SET "+",".join(f"{k}=?" for k in fields)+",version=version+1,updated_at=? WHERE id=?",(*fields.values(),t,iid))
        elif op=="work_update":
            fields={k:c[k] for k in ("name","purpose","kind") if k in c}
            if c.get("kind") not in (None,"project"): raise ValueError("only light to project upgrade is allowed")
            if not fields: raise ValueError("empty work update")
            con.execute("UPDATE work SET "+",".join(f"{k}=?" for k in fields)+",updated_at=?",(*fields.values(),t))
        elif op=="adopt":
            if con.execute("SELECT status FROM work").fetchone()[0] != "imported_unreviewed": raise RuntimeError("INVALID_TRANSITION")
            review=con.execute("SELECT value FROM legacy_meta WHERE key='migration_review_report'").fetchone()
            if review:
                report=json.loads(review[0]); count=sum(report.get("counts",{}).values())
                if count and c.get("acknowledge_migration_review") is not True: raise RuntimeError("MIGRATION_REVIEW_ACK_REQUIRED")
                if count:
                    report["acknowledged"]=True
                    con.execute("UPDATE legacy_meta SET value=? WHERE key='migration_review_report'",(_json(report),))
            snapshot=con.execute("SELECT value FROM legacy_meta WHERE key='registry_snapshot'").fetchone()
            destination_status="active"
            if snapshot and json.loads(snapshot[0]).get("raw",{}).get("status")=="archived": destination_status="archived"
            con.execute("UPDATE work SET status=?,updated_at=?",(destination_status,t))
    con.execute("UPDATE proposals SET status='applied',confirmation_json=?,applied_at=? WHERE id=?",(_json(args["confirmation"]),t,p["id"]))
    return ok({"proposal_id":p["id"],"status":"applied","refs":{k:v for k,v in refs.items() if k}})


def _guard_work(con, allow_completed=False):
    status=con.execute("SELECT status FROM work").fetchone()[0]
    if status=="archived": raise RuntimeError("WORK_ARCHIVED")
    if status=="completed" and not allow_completed: raise RuntimeError("WORK_COMPLETED")
    if status=="imported_unreviewed": raise RuntimeError("ADOPTION_REQUIRED")


def record(args, **ctx):
    try:
        action=args.get("action"); path=_resolve(args.get("work")); session=ctx.get("session_id")
        with _write(path) as con:
            prior=_receipt(con,args.get("request_id"),args)
            if prior:return dumps(prior)
            _guard_work(con)
            before_state=_entity_state(con)
            if action=="append": response=_append(con,args)
            elif action=="update": response=_update_record(con,args)
            elif action in {"start","block"}: response=_item_state(con,args,action)
            else: raise ValueError("invalid record action")
            before,after=_entity_diff(before_state,_entity_state(con))
            _commit(con,args["request_id"],args,response,"record_"+action,"work",con.execute("SELECT id FROM work").fetchone()[0],session=session,before=before,after=after)
        _update_catalog(path); return dumps(response)
    except RuntimeError as e:return dumps(err("CONFLICT" if str(e)=="REQUEST_CONFLICT" else str(e),str(e)))
    except sqlite3.OperationalError as e:return dumps(_sqlite_error(e))
    except LookupError as e:return dumps(err("NOT_FOUND",str(e)))
    except Exception as e:return dumps(err("VALIDATION_ERROR",str(e)))


def _append(con,args):
    _strict(args,{"action","work","records","request_id"},{"records","request_id"})
    rows=args["records"]
    if not isinstance(rows,list) or not rows or len(rows)>MAX_BATCH: raise ValueError("invalid records batch")
    refs={}; pending=[]; t=now()
    common={"ref","kind","title","body","status","links","relations","files","supersedes_id","supersedes_ref"}
    for r in rows:
        kind=r.get("kind")
        if kind not in RECORD_FIELDS: raise ValueError("invalid record kind")
        unknown=set(r)-common-RECORD_FIELDS[kind]
        if unknown: raise ValueError(f"fields invalid for {kind}: {sorted(unknown)}")
        if not r.get("title") or not r.get("body") or len(r["body"])>MAX_TEXT: raise ValueError("title/body required or too large")
        if kind=="issue":
            if r.get("severity") not in {None,"low","medium","high","critical"}: raise ValueError("invalid issue severity")
            if r.get("status","open") not in {"open","resolved"}: raise ValueError("invalid issue status")
            if r.get("status","open")=="resolved" and not r.get("resolution"): raise ValueError("resolution required")
        elif kind=="decision" and r.get("status","active") not in {"active","superseded"}: raise ValueError("invalid decision status")
        elif kind=="experiment" and r.get("verdict") not in {None,"pass","fail","inconclusive"}: raise ValueError("invalid experiment verdict")
        rid=str(uuid.uuid4())
        if r.get("ref"): refs[r["ref"]]=rid
        pending.append((rid,r))
    for rid,r in pending:
        payload={k:r[k] for k in RECORD_FIELDS[r["kind"]] if k in r and k not in {"issue_key","decision_key"}}
        status=r.get("status") or ("open" if r["kind"]=="issue" else "active")
        supersedes=r.get("supersedes_id") or refs.get(r.get("supersedes_ref"))
        if supersedes:
            old=con.execute("SELECT kind,status FROM records WHERE id=?",(supersedes,)).fetchone()
            if not old and supersedes not in [x[0] for x in pending]: raise ValueError("invalid supersedes")
            if r["kind"]!="decision" or (old and (old[0]!="decision" or old[1]=="superseded")): raise ValueError("invalid decision supersession")
            con.execute("UPDATE records SET status='superseded',version=version+1,updated_at=? WHERE id=?",(t,supersedes))
        con.execute("INSERT INTO records(id,kind,title,body,payload_json,status,issue_key,decision_key,supersedes_id,version,legacy_payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,1,NULL,?,?)",(rid,r["kind"],r["title"],r["body"],_json(payload),status,r.get("issue_key"),r.get("decision_key"),supersedes,t,t))
        for link in r.get("links") or [{"relation":"context"}]:
            if set(link)-{"item_id","item_ref","relation"}: raise ValueError("invalid link fields")
            item=link.get("item_id")
            if item and not con.execute("SELECT 1 FROM items WHERE id=?",(item,)).fetchone(): raise ValueError("invalid item link")
            relation=link.get("relation","context")
            if relation not in {"context","affects","evidence"}: raise ValueError("invalid link relation")
            con.execute("INSERT INTO record_links VALUES(?,?,?)",(rid,item,relation))
        for relation in r.get("relations") or []:
            if set(relation)-{"target_id","target_ref","relation"}: raise ValueError("invalid relation fields")
            target=relation.get("target_id") or refs.get(relation.get("target_ref"))
            if not target or target==rid: raise ValueError("invalid record relation")
            rel=relation.get("relation")
            if rel not in {"supports","contradicts","evidence_for","follows"}: raise ValueError("invalid record relation")
            con.execute("INSERT INTO record_relations VALUES(?,?,?)",(rid,target,rel))
        for f in r.get("files") or []: _file(con,rid,f,t)
    return ok({"refs":{k:v for k,v in refs.items() if k},"count":len(rows)})


def _file(con,rid,f,t):
    allowed={"locator","storage","role","purpose","handoff_status","corpus_id","source_locator","item_id"}
    if set(f)-allowed: raise ValueError("invalid file fields")
    if f.get("storage") not in {"local","corpus","external"} or f.get("role") not in {"temp","artifact","reference"}: raise ValueError("invalid storage/role")
    locator=f.get("locator")
    if not isinstance(locator,str) or not locator: raise ValueError("file locator required")
    if f["storage"]=="local":
        if Path(locator).is_absolute() or ".." in Path(locator).parts: raise ValueError("local file locator escapes work root")
        db_path=Path(con.execute("PRAGMA database_list").fetchone()[2])
        root=db_path.parent.resolve(); resolved=(root/locator).resolve(strict=False)
        if resolved!=root and root not in resolved.parents: raise ValueError("local file locator escapes work root")
    hs=f.get("handoff_status","not_applicable")
    if hs not in {"not_applicable","pending_handoff","accepted","failed"}: raise ValueError("invalid handoff status")
    if f["storage"]=="corpus" and (hs!="accepted" or not f.get("corpus_id")): raise ValueError("corpus requires accepted handoff and real corpus_id")
    con.execute("INSERT INTO file_refs VALUES(?,?,?,?,?,?,?,?,?,?,?)",(str(uuid.uuid4()),rid,f.get("item_id"),f["locator"],f["storage"],f["role"],f["purpose"],hs,f.get("corpus_id"),f.get("source_locator"),t))


def _update_record(con,args):
    _strict(args,{"action","work","record_id","title","body","payload","status","resolution","links","relations","files","expected_version","request_id"},{"record_id","expected_version","request_id"})
    row=con.execute("SELECT * FROM records WHERE id=?",(args["record_id"],)).fetchone()
    if not row: raise LookupError(args["record_id"])
    if isinstance(args["expected_version"],bool) or args["expected_version"]!=row["version"]: raise RuntimeError("VERSION_CONFLICT")
    status=args.get("status",row["status"]); payload=json.loads(row["payload_json"])
    if "payload" in args:
        if not isinstance(args["payload"],dict): raise ValueError("payload must be an object")
        payload=dict(args["payload"])
    if "resolution" in args: payload["resolution"]=args["resolution"]
    if row["kind"]=="issue":
        if status not in {"open","resolved"}: raise ValueError("invalid issue status")
        if status=="resolved" and not payload.get("resolution"): raise ValueError("resolution required")
    elif "resolution" in args or "status" in args: raise ValueError("status/resolution only valid for issues")
    con.execute("UPDATE records SET title=?,body=?,status=?,payload_json=?,version=version+1,updated_at=? WHERE id=?",(args.get("title",row["title"]),args.get("body",row["body"]),status,_json(payload),now(),row["id"]))
    if "links" in args:
        if not isinstance(args["links"],list): raise ValueError("links must be an array")
        con.execute("DELETE FROM record_links WHERE record_id=?",(row["id"],))
        for link in args["links"]:
            if set(link)-{"item_id","relation"}: raise ValueError("invalid link fields")
            item=link.get("item_id"); relation=link.get("relation","context")
            if item and not con.execute("SELECT 1 FROM items WHERE id=?",(item,)).fetchone(): raise ValueError("invalid item link")
            if relation not in {"context","affects","evidence"}: raise ValueError("invalid link relation")
            con.execute("INSERT INTO record_links VALUES(?,?,?)",(row["id"],item,relation))
    if "relations" in args:
        if not isinstance(args["relations"],list): raise ValueError("relations must be an array")
        con.execute("DELETE FROM record_relations WHERE source_id=?",(row["id"],))
        for rel in args["relations"]:
            if set(rel)-{"target_id","relation"}: raise ValueError("invalid relation fields")
            if rel.get("target_id")==row["id"] or not con.execute("SELECT 1 FROM records WHERE id=?",(rel.get("target_id"),)).fetchone(): raise ValueError("invalid record relation")
            if rel.get("relation") not in {"supports","contradicts","evidence_for","follows"}: raise ValueError("invalid record relation")
            con.execute("INSERT INTO record_relations VALUES(?,?,?)",(row["id"],rel["target_id"],rel["relation"]))
    if "files" in args:
        if not isinstance(args["files"],list): raise ValueError("files must be an array")
        con.execute("DELETE FROM file_refs WHERE record_id=?",(row["id"],))
        for f in args["files"]: _file(con,row["id"],f,now())
    return ok({"record_id":row["id"],"status":status})


def _item_state(con,args,action):
    _strict(args,{"action","work","item_id","reason","expected_version","request_id"},{"item_id","expected_version","request_id"})
    row=con.execute("SELECT * FROM items WHERE id=? AND removed_at IS NULL",(args["item_id"],)).fetchone()
    if not row: raise LookupError(args["item_id"])
    if isinstance(args["expected_version"],bool) or args["expected_version"]!=row["version"]: raise RuntimeError("VERSION_CONFLICT")
    transitions={"start":{"planned","blocked"},"block":{"planned","active"}}
    if row["status"] not in transitions[action]: raise RuntimeError("INVALID_TRANSITION")
    if action=="start":
        if not _can_start(con,dict(row)): raise RuntimeError("DEPENDENCY_BLOCKED")
        status="active"
    else: status="blocked"
    con.execute("UPDATE items SET status=?,version=version+1,updated_at=? WHERE id=?",(status,now(),row["id"]))
    return ok({"item_id":row["id"],"status":status})


def finish(args, **ctx):
    try:
        action=args.get("action"); path=_resolve(args.get("work")); session=ctx.get("session_id")
        with _write(path) as con:
            prior=_receipt(con,args.get("request_id"),args)
            if prior:return dumps(prior)
            if action in {"confirm","continue"} and args.get("proposal_id"):
                terminal=con.execute("SELECT status FROM proposals WHERE id=?",(args["proposal_id"],)).fetchone()
                if terminal and terminal[0] in {"applied","rejected"}:
                    saved=con.execute("SELECT response_json FROM request_receipts WHERE json_extract(response_json,'$.data.proposal_id')=? ORDER BY rowid DESC LIMIT 1",(args["proposal_id"],)).fetchone()
                    if saved:return saved[0]
            if action=="prepare": response=_prepare(con,args,session)
            elif action=="confirm": response=_confirm(con,args,session)
            elif action=="continue": response=_continue(con,args,session)
            else: raise ValueError("invalid finish action")
            _commit(con,args["request_id"],args,response,"finish_"+action,args.get("target_type","proposal"),args.get("target_id") or args.get("proposal_id"),response["data"].get("proposal_id"),session,after=response["data"])
        _update_catalog(path); return dumps(response)
    except RuntimeError as e:return dumps(err("CONFLICT" if str(e)=="REQUEST_CONFLICT" else str(e),str(e)))
    except sqlite3.OperationalError as e:return dumps(_sqlite_error(e))
    except LookupError as e:return dumps(err("NOT_FOUND",str(e)))
    except Exception as e:return dumps(err("VALIDATION_ERROR",str(e)))


def _relevant(con,target_type,target_id):
    if target_type=="work":
        item_ids=[r[0] for r in con.execute("SELECT id FROM items WHERE removed_at IS NULL ORDER BY id")]
        records=[dict(r) for r in con.execute("SELECT * FROM records ORDER BY id")]
    else:
        item_ids=[r[0] for r in con.execute("WITH RECURSIVE tree(id) AS (SELECT ? UNION ALL SELECT i.id FROM items i JOIN tree t ON i.parent_id=t.id WHERE i.removed_at IS NULL) SELECT id FROM tree ORDER BY id",(target_id,))]
        marks=",".join("?" for _ in item_ids)
        records=[dict(r) for r in con.execute(f"SELECT DISTINCT r.* FROM records r JOIN record_links l ON l.record_id=r.id WHERE l.item_id IN ({marks}) ORDER BY r.id",item_ids)] if item_ids else []
    items=[dict(r) for r in con.execute("SELECT * FROM items WHERE id IN (%s) ORDER BY id"%(",".join("?" for _ in item_ids)),item_ids)] if item_ids else []
    dependencies=[dict(r) for r in con.execute("SELECT * FROM dependencies WHERE item_id IN (%s) OR requires_id IN (%s) ORDER BY item_id,requires_id"%(",".join("?" for _ in item_ids),",".join("?" for _ in item_ids)),item_ids+item_ids)] if item_ids else []
    record_ids=[r["id"] for r in records]
    links=[dict(r) for r in con.execute("SELECT * FROM record_links WHERE record_id IN (%s) ORDER BY record_id,item_id,relation"%(",".join("?" for _ in record_ids)),record_ids)] if record_ids else []
    relations=[dict(r) for r in con.execute("SELECT * FROM record_relations WHERE source_id IN (%s) OR target_id IN (%s) ORDER BY source_id,target_id,relation"%(",".join("?" for _ in record_ids),",".join("?" for _ in record_ids)),record_ids+record_ids)] if record_ids else []
    files=[dict(r) for r in con.execute("SELECT * FROM file_refs WHERE record_id IN (%s) OR item_id IN (%s) ORDER BY id"%(",".join("?" for _ in record_ids) or "NULL",",".join("?" for _ in item_ids) or "NULL"),record_ids+item_ids)] if record_ids or item_ids else []
    work=dict(con.execute("SELECT id,status,kind FROM work").fetchone())
    return {"digest":_hash({"work":work,"items":items,"dependencies":dependencies,"records":records,"links":links,"relations":relations,"files":files})}


def _prepare(con,args,session):
    _strict(args,{"action","mode","work","target_type","target_id","request_id"},{"target_type","target_id","request_id"})
    target=args["target_type"]
    if target not in {"work","goal","task"}: raise ValueError("invalid target")
    mode=args.get("mode","complete")
    if mode not in {"complete","archive","restore"}: raise ValueError("invalid finish mode")
    work=con.execute("SELECT id,status FROM work").fetchone()
    if target=="work" and args["target_id"]!=work["id"]: raise ValueError("target work mismatch")
    if mode in {"archive","restore"} and target!="work": raise ValueError("archive/restore are work-only")
    if work["status"]=="archived" and mode!="restore": raise RuntimeError("WORK_ARCHIVED")
    if mode=="restore" and work["status"]!="archived": raise RuntimeError("INVALID_TRANSITION")
    if target!="work":
        row=con.execute("SELECT * FROM items WHERE id=? AND kind=?",(args["target_id"],target)).fetchone()
        if not row: raise LookupError(args["target_id"])
        prior_status=row["status"]
        con.execute("UPDATE items SET status='awaiting_confirmation',version=version+1,updated_at=? WHERE id=?",(now(),row["id"]))
    else: prior_status=con.execute("SELECT status FROM work").fetchone()[0]
    base=_relevant(con,target,args["target_id"]); pid=str(uuid.uuid4())
    open_items=[dict(r) for r in con.execute("SELECT id,kind,title,status FROM items WHERE status NOT IN('done','cancelled') AND removed_at IS NULL")]
    open_issues=[dict(r) for r in con.execute("SELECT id,title,status FROM records WHERE kind='issue' AND status='open'")]
    payload={"mode":mode,"target_type":target,"target_id":args["target_id"],"prior_status":prior_status,"open_items":open_items,"open_issues":open_issues}
    con.execute("INSERT INTO proposals VALUES(?,?,?,?,?,'pending',?,NULL,?,NULL)",(pid,"completion",_json(payload),_json(base),"completion summary",session,now()))
    return ok({"proposal_id":pid,"summary":payload,"needs_user_confirmation":True},follow_up=["Ask whether to finish now or add method, result, or notes."])


def _confirm(con,args,session):
    _strict(args,{"action","work","proposal_id","confirmation","request_id"},{"proposal_id","confirmation","request_id"})
    if not isinstance(args["confirmation"],dict) or not args["confirmation"].get("summary"): raise RuntimeError("CONFIRMATION_REQUIRED")
    p=con.execute("SELECT * FROM proposals WHERE id=? AND kind='completion'",(args["proposal_id"],)).fetchone()
    if not p: raise LookupError(args["proposal_id"])
    if p["status"]!="pending": raise RuntimeError("STALE_PROPOSAL")
    payload=json.loads(p["payload_json"]); current=_relevant(con,payload["target_type"],payload["target_id"])
    if _json(current)!=p["base_versions_json"]: con.execute("UPDATE proposals SET status='stale' WHERE id=?",(p["id"],)); raise RuntimeError("STALE_PROPOSAL")
    t=now(); mode=payload["mode"]
    if payload["target_type"]=="work":
        status={"complete":"completed","archive":"archived","restore":"active"}[mode]
        con.execute("UPDATE work SET status=?,updated_at=?",(status,t))
    else: con.execute("UPDATE items SET status='done',completed_at=?,version=version+1,updated_at=? WHERE id=?",(t,t,payload["target_id"]))
    con.execute("UPDATE proposals SET status='applied',confirmation_json=?,applied_at=? WHERE id=?",(_json(args["confirmation"]),t,p["id"]))
    return ok({"proposal_id":p["id"],"status":"applied","mode":mode})


def _continue(con,args,session):
    _strict(args,{"action","work","proposal_id","request_id"},{"proposal_id","request_id"})
    p=con.execute("SELECT * FROM proposals WHERE id=? AND kind='completion'",(args["proposal_id"],)).fetchone()
    if not p: raise LookupError(args["proposal_id"])
    if p["status"] in {"rejected","applied"}:
        return ok({"proposal_id":p["id"],"status":"continued" if p["status"]=="rejected" else "applied"})
    payload=json.loads(p["payload_json"])
    if payload["target_type"]!="work": con.execute("UPDATE items SET status=?,version=version+1,updated_at=? WHERE id=? AND status='awaiting_confirmation'",(payload.get("prior_status","planned"),now(),payload["target_id"]))
    con.execute("UPDATE proposals SET status='rejected',applied_at=? WHERE id=?",(now(),p["id"]))
    return ok({"proposal_id":p["id"],"status":"continued"})


def read(args, **ctx):
    try:
        action=args.get("action")
        if action=="overview": return dumps(_overview(args))
        _strict(args,{"action","work","id","collection","limit","cursor"},{"action","work"})
        if "limit" in args and (isinstance(args["limit"],bool) or not isinstance(args["limit"],int)): raise ValueError("limit must be an integer")
        if "limit" in args and not 1 <= args["limit"] <= 200: raise ValueError("limit out of range")
        path=_resolve(args.get("work")); con=_connect(path)
        try:
            con.execute("BEGIN")
            if action=="current": response=_current(con,args.get("limit",20))
            elif action=="detail": response=_detail(con,args)
            elif action=="history": response=_history(con,args)
            else: raise ValueError("invalid read action")
        finally: con.close()
        return dumps(response)
    except RuntimeError as e:return dumps(err(str(e),str(e)))
    except Exception as e:return dumps(err("VALIDATION_ERROR",str(e)))


def _page(rows,total,limit,offset):
    return {"items":[dict(r) for r in rows],"page":{"total":total,"returned":len(rows),"has_more":offset+len(rows)<total,"cursor":str(offset+len(rows)) if offset+len(rows)<total else None}}


def _history(con,args):
    limit=args.get("limit",50)
    cursor=args.get("cursor")
    if cursor:
        state=_decode_cursor(cursor)
        anchor,last=state.get("anchor"),state.get("last")
        if not isinstance(anchor,int) or not isinstance(last,int) or anchor < 0 or last < 0: raise ValueError("invalid cursor")
    else:
        anchor=con.execute("SELECT COALESCE(MAX(seq),0) FROM history").fetchone()[0]; last=0
    total=con.execute("SELECT COUNT(*) FROM history WHERE seq<=?",(anchor,)).fetchone()[0]
    rows=con.execute("SELECT * FROM history WHERE seq>? AND seq<=? ORDER BY seq LIMIT ?",(last,anchor,limit+1)).fetchall()
    more=len(rows)>limit; rows=rows[:limit]
    next_cursor=_encode_cursor({"anchor":anchor,"last":rows[-1]["seq"]}) if more else None
    return ok({"items":[dict(r) for r in rows],"page":{"total":total,"returned":len(rows),"has_more":more,"cursor":next_cursor}})


def _encode_cursor(value):
    return base64.urlsafe_b64encode(_json(value).encode()).decode().rstrip("=")


def _decode_cursor(value):
    if not isinstance(value,str) or not value: raise ValueError("invalid cursor")
    try:
        return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
    except Exception as exc:
        raise ValueError("invalid cursor") from exc


def _detail(con,args):
    if args.get("collection"):
        if args.get("id"): raise ValueError("id and collection are mutually exclusive")
        return _collection(con,args)
    oid=args.get("id"); row=con.execute("SELECT * FROM items WHERE id=?",(oid,)).fetchone() or con.execute("SELECT * FROM records WHERE id=?",(oid,)).fetchone()
    if not row:return err("NOT_FOUND",str(oid))
    data=dict(row)
    if "payload_json" in data:
        data["payload"]=json.loads(data["payload_json"])
        data["links"]=[dict(r) for r in con.execute("SELECT item_id,relation FROM record_links WHERE record_id=? ORDER BY relation,item_id",(oid,))]
        data["relations"]=[dict(r) for r in con.execute("SELECT target_id,relation FROM record_relations WHERE source_id=? ORDER BY relation,target_id",(oid,))]
        data["files"]=[dict(r) for r in con.execute("SELECT * FROM file_refs WHERE record_id=? ORDER BY id",(oid,))]
        root=Path(con.execute("PRAGMA database_list").fetchone()[2]).parent
        for f in data["files"]:
            f["broken_reference"]=f["storage"]=="local" and not (root/f["locator"]).exists()
    return ok(data)


def _collection(con,args):
    collection=args["collection"]
    specs={"items":("items","id"),"records":("records","id"),"files":("file_refs","id"),"proposals":("proposals","id")}
    if collection not in specs: raise ValueError("invalid collection")
    table,key=specs[collection]; limit=args.get("limit",20)
    revision=con.execute("SELECT revision FROM work").fetchone()[0]; last=""
    if args.get("cursor"):
        state=_decode_cursor(args["cursor"])
        if state.get("revision") != revision: raise RuntimeError("CURSOR_STALE")
        if state.get("collection") != collection or not isinstance(state.get("last"),str): raise ValueError("invalid cursor")
        last=state["last"]
    total=con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    rows=con.execute(f"SELECT * FROM {table} WHERE {key}>? ORDER BY {key} LIMIT ?",(last,limit+1)).fetchall()
    more=len(rows)>limit; rows=rows[:limit]
    cursor=_encode_cursor({"revision":revision,"collection":collection,"last":rows[-1][key]}) if more else None
    return ok({"items":[dict(r) for r in rows],"page":{"total":total,"returned":len(rows),"has_more":more,"cursor":cursor},"revision":revision})


def _bounded(rows,total,limit):
    rows=rows[:limit]
    return {"items":rows,"page":{"total":total,"returned":len(rows),"has_more":len(rows)<total}}


def _can_start(con,row):
    if row["status"]!="planned" or row.get("removed_at"): return False
    blocked=con.execute("SELECT 1 FROM dependencies d LEFT JOIN items i ON i.id=d.requires_id WHERE d.item_id=? AND (i.status!='done' OR i.removed_at IS NOT NULL OR i.id IS NULL)",(row["id"],)).fetchone()
    if blocked:return False
    if row.get("parent_id"):
        parent=con.execute("SELECT id,status,removed_at FROM items WHERE id=?",(row["parent_id"],)).fetchone()
        if not parent or parent["removed_at"] or parent["status"] in {"blocked","cancelled"}: return False
        if con.execute("SELECT 1 FROM dependencies d LEFT JOIN items i ON i.id=d.requires_id WHERE d.item_id=? AND (i.status!='done' OR i.removed_at IS NOT NULL OR i.id IS NULL)",(parent["id"],)).fetchone(): return False
    return True


def _current(con,limit=20):
    work=dict(con.execute("SELECT * FROM work").fetchone())
    items=[dict(r) for r in con.execute("SELECT * FROM items WHERE removed_at IS NULL ORDER BY parent_id,position,id")]
    records=[dict(r) for r in con.execute("SELECT * FROM records ORDER BY created_at DESC,id DESC")]
    issues=[r for r in records if r["kind"]=="issue" and r["status"]=="open"]
    decisions=[r for r in records if r["kind"]=="decision" and r["status"]=="active"]
    proposals=[dict(r) for r in con.execute("SELECT id,kind,review_summary,created_at FROM proposals WHERE status='pending' ORDER BY created_at DESC,id DESC")]
    goals=[i for i in items if i["kind"]=="goal"]; tasks=[i for i in items if i["kind"]=="task"]
    goal_counts={"total":len(goals),"done":sum(i["status"]=="done" for i in goals),"cancelled":sum(i["status"]=="cancelled" for i in goals)}
    task_counts={"total":len(tasks),"done":sum(i["status"]=="done" for i in tasks),"cancelled":sum(i["status"]=="cancelled" for i in tasks)}
    counts={"goals":len(goals),"tasks":len(tasks),"done":goal_counts["done"]+task_counts["done"],"cancelled":goal_counts["cancelled"]+task_counts["cancelled"],"open_issues":len(issues)}
    files=[dict(r) for r in con.execute("SELECT * FROM file_refs ORDER BY created_at DESC,id DESC")]
    outcomes=[r for r in records if r["kind"] in {"result","experiment"}]
    legacy=[r for r in records if r.get("legacy_payload") and not con.execute("SELECT 1 FROM record_links WHERE record_id=? AND item_id IS NOT NULL",(r["id"],)).fetchone()]
    startable=[i for i in items if _can_start(con,i)]
    registry_row=con.execute("SELECT value FROM legacy_meta WHERE key='registry_snapshot'").fetchone()
    review_row=con.execute("SELECT value FROM legacy_meta WHERE key='migration_review_report'").fetchone()
    return ok({"work":work,"counts":counts,"goal_counts":goal_counts,"task_counts":task_counts,
        "items":items[:limit],"issues":issues[:limit],"decisions":decisions[:limit],"pending_proposals":proposals[:limit],"suggested_startable":startable[:limit],
        "goals":_bounded(goals,len(goals),limit),"tasks":_bounded(tasks,len(tasks),limit),"records":_bounded(records,len(records),limit),
        "outcomes":_bounded(outcomes,len(outcomes),limit),"files":_bounded(files,len(files),limit),"unassigned_legacy":_bounded(legacy,len(legacy),limit),
        "legacy_registry":json.loads(registry_row[0]) if registry_row else None,
        "migration_review":json.loads(review_row[0]) if review_row else None,
        "generated_at":now(),"revision":work["revision"]})


def _overview(args):
    index=_index_path(); freshness="unavailable"; rows=[]
    if index.is_file():
        try:
            con=sqlite3.connect(index); con.row_factory=sqlite3.Row; rows=[dict(r) for r in con.execute("SELECT * FROM work_registry ORDER BY slug")]; con.close()
            if not rows:freshness="unverified"
            else:
                stale=False
                for r in rows:
                    try:
                        db=sqlite3.connect(f"file:{r['db_path']}?mode=ro",uri=True)
                        actual=db.execute("SELECT id,revision FROM work").fetchone(); db.close()
                        if not actual or actual[0]!=r["work_id"] or actual[1]!=r["observed_revision"]: stale=True
                    except Exception: stale=True
                freshness="stale" if stale else "fresh"
        except Exception:freshness="unavailable"
    return ok({"works":rows,"catalog_freshness":freshness,"generated_at":now()})


def schemas():
    s={"type":"string","minLength":1,"maxLength":MAX_STRING}
    confirmation={"type":"object","additionalProperties":False,"properties":{
        "summary":{"type":"string","minLength":1,"maxLength":2000}},"required":["summary"]}
    change={"type":"object","additionalProperties":False,"properties":{"op":{"type":"string","enum":["add","update","remove","reorder","depend","undepend","reopen","cancel","work_update","adopt"]},"ref":s,"kind":{"type":"string","enum":["goal","task","project"]},"name":s,"purpose":s,"parent_id":s,"parent_ref":s,"item_id":s,"item_ref":s,"requires_id":s,"requires_ref":s,"title":s,"description":s,"position":{"type":"integer","minimum":0},"acknowledge_migration_review":{"type":"boolean"}},"required":["op"]}
    link={"type":"object","additionalProperties":False,"properties":{"item_id":s,"item_ref":s,"relation":{"type":"string","enum":["context","affects","evidence"]}}}
    relation={"type":"object","additionalProperties":False,"properties":{"target_id":s,"target_ref":s,"relation":{"type":"string","enum":["supports","contradicts","evidence_for","follows"]}}}
    file_ref={"type":"object","additionalProperties":False,"properties":{"locator":s,"storage":{"type":"string","enum":["local","corpus","external"]},"role":{"type":"string","enum":["temp","artifact","reference"]},"purpose":s,"handoff_status":{"type":"string","enum":["not_applicable","pending_handoff","accepted","failed"]},"corpus_id":s,"source_locator":s,"item_id":s},"required":["locator","storage","role","purpose"]}
    record={"type":"object","additionalProperties":False,"properties":{"ref":s,"kind":{"type":"string","enum":sorted(RECORD_FIELDS)},"title":s,"body":s,"status":s,"severity":{"type":"string","enum":["low","medium","high","critical"]},"hypothesis":s,"workaround":s,"resolution":s,"issue_key":s,"summary":s,"rationale":s,"decision_key":s,"supersedes_id":s,"supersedes_ref":s,"input_config":{"type":["object","string"]},"expected_result":s,"actual_result":s,"verdict":{"type":"string","enum":["pass","fail","inconclusive"]},"run_at":s,"result":s,"evidence":s,"links":{"type":"array","maxItems":100,"items":link},"relations":{"type":"array","maxItems":100,"items":relation},"files":{"type":"array","maxItems":100,"items":file_ref}},"required":["kind","title","body"]}
    changes={"type":"array","minItems":1,"maxItems":MAX_BATCH,"items":change}; records={"type":"array","minItems":1,"maxItems":MAX_BATCH,"items":record}
    def one(name,desc,actions,props,contracts=None):
        parameters={"type":"object","additionalProperties":False,
            "properties":{"action":{"type":"string","enum":actions},**props},"required":["action"]}
        if contracts:
            clauses=[]
            for action,required,forbidden in contracts:
                then={"required":["action",*required]}
                if forbidden:
                    then["not"]={"anyOf":[{"required":[field]} for field in forbidden]}
                clauses.append({"if":{"properties":{"action":{"const":action}},"required":["action"]},"then":then})
            parameters["allOf"]=clauses
        return {"name":name,"description":desc,"parameters":parameters}
    common={"work":s,"request_id":s}
    tools = [
      one("fact_create","Create project/light work; purpose is mandatory.",["create"],{"slug":s,"name":s,"purpose":s,"kind":{"type":"string","enum":["project","light"]},"root_path":s,"request_id":s},[
          ("create",["slug","purpose","kind","request_id"],[])]),
      one("fact_read","Read overview/current/detail/history with stable cursors.",["overview","current","detail","history"],{"work":s,"id":s,"collection":{"type":"string","enum":["items","records","files","proposals"]},"limit":{"type":"integer","minimum":1,"maximum":200},"cursor":s},[
          ("overview",[],["work","id","collection","limit","cursor"]),
          ("current",["work"],["id","collection","cursor"]),
          ("detail",["work"],[]),
          ("history",["work"],["id","collection"])]),
      one("fact_plan","Propose/apply/reject an exact version-bound batch. Include only fields valid for the selected action; never placeholders.",["propose","apply","reject"],{**common,"changes":changes,"proposal_id":s,"review_summary":s,"confirmation":confirmation},[
          ("propose",["work","request_id","changes"],["proposal_id","confirmation"]),
          ("apply",["work","request_id","proposal_id","confirmation"],["changes","review_summary"]),
          ("reject",["work","request_id","proposal_id"],["changes","review_summary"])]),
      one("fact_record","Append/update facts or start/block an item atomically.",["append","update","start","block"],{**common,"records":records,"record_id":s,"item_id":s,"title":s,"body":{"type":"string","maxLength":MAX_TEXT},"payload":{"type":"object","maxProperties":100},"links":{"type":"array","maxItems":100,"items":link},"relations":{"type":"array","maxItems":100,"items":relation},"files":{"type":"array","maxItems":100,"items":file_ref},"status":s,"resolution":s,"reason":s,"expected_version":{"type":"integer","minimum":1}},[
          ("append",["work","request_id","records"],["record_id","item_id","title","body","payload","links","relations","files","status","resolution","reason","expected_version"]),
          ("update",["work","request_id","record_id","expected_version"],["records","item_id","reason"]),
          ("start",["work","request_id","item_id","expected_version"],["records","record_id","title","body","payload","links","relations","files","status","resolution"]),
          ("block",["work","request_id","item_id","expected_version"],["records","record_id","title","body","payload","links","relations","files","status","resolution"])]),
      one("fact_finish","Prepare/confirm/continue completion or lifecycle. Include only fields valid for the selected action; never placeholders.",["prepare","confirm","continue"],{**common,"mode":{"type":"string","enum":["complete","archive","restore"]},"target_type":{"type":"string","enum":["work","goal","task"]},"target_id":s,"proposal_id":s,"confirmation":confirmation},[
          ("prepare",["work","request_id","target_type","target_id"],["proposal_id","confirmation"]),
          ("confirm",["work","request_id","proposal_id","confirmation"],["mode","target_type","target_id"]),
          ("continue",["work","request_id","proposal_id"],["mode","target_type","target_id","confirmation"])]),
    ]
    read_schema=next(tool["parameters"] for tool in tools if tool["name"]=="fact_read")
    read_schema["allOf"].append({"if":{"properties":{"action":{"const":"detail"}},"required":["action"]},
        "then":{"oneOf":[{"required":["id"],"not":{"required":["collection"]}},
                          {"required":["collection"],"not":{"required":["id"]}}]}})
    return tools


def _public_handler(name, handler):
    def invoke(args, **ctx):
        try:
            _validate_public(name, args)
        except Exception as exc:
            return dumps(err("VALIDATION_ERROR", str(exc)))
        return handler(args, **ctx)
    return invoke


HANDLERS={name:_public_handler(name, handler) for name,handler in {
    "fact_create":create,"fact_read":read,"fact_plan":plan,"fact_record":record,"fact_finish":finish}.items()}


def _logical_manifest(con):
    tables = ("work","items","dependencies","records","record_links","record_relations",
              "file_refs","legacy_map","legacy_meta")
    result = {}
    for table in tables:
        columns = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
        order = ",".join(f'"{c}"' for c in columns)
        rows = [dict(zip(columns, row)) for row in con.execute(f'SELECT * FROM "{table}" ORDER BY {order}')]
        result[table] = {"rows": len(rows), "sha256": _hash(rows)}
    return result


def _legacy_tables(con):
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _legacy_identity(con, source):
    meta = {}
    if "meta" in _legacy_tables(con):
        meta = {str(r[0]): r[1] for r in con.execute("SELECT key,value FROM meta")}
    slug = meta.get("slug") or source.parent.name or "legacy"
    purpose = meta.get("purpose") or meta.get("description") or "migrated legacy work"
    name = meta.get("name") or meta.get("title") or slug
    return {"slug": slug, "name": name, "purpose": purpose}


def _legacy_refs(value):
    if value in (None, ""): return []
    try: refs = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError): return []
    if not isinstance(refs, list): return []
    return [r for r in refs if isinstance(r, dict) and isinstance(r.get("table"), str) and r.get("id") is not None]


def _expected_migration(old):
    """Derive migration coverage and relationships solely from the legacy backup."""
    tables = _legacy_tables(old); rows = {}; expected_links = []; expected_relations = []; unresolved = []
    by_key = {}
    for table in ("goals","tasks","decisions","issues","experiments","events"):
        if table not in tables: continue
        rows[table] = {str(r["id"]): dict(r) for r in old.execute(f'SELECT * FROM "{table}"')}
        for oid, raw in rows[table].items():
            key = raw.get("issue_key") or raw.get("decision_key")
            if key: by_key[(table, str(key))] = oid
    for table, table_rows in rows.items():
        for oid, raw in table_rows.items():
            goal_id = raw.get("parent_goal_id") if table == "goals" else (raw.get("goal_id") or raw.get("parent_goal_id") if table == "tasks" else raw.get("goal_id"))
            if goal_id is not None:
                target=("goals",str(goal_id))
                if target[1] in rows.get("goals",{}):
                    (expected_links if table not in {"goals","tasks"} else expected_relations).append(
                        (table,oid,*target,"context" if table not in {"goals","tasks"} else "parent"))
                else: unresolved.append({"source_table":table,"source_id":oid,"table":"goals","id":str(goal_id),"reason":"missing"})
            if table == "experiments" and raw.get("issue_key"):
                target=by_key.get(("issues",str(raw["issue_key"])))
                if target: expected_relations.append((table,oid,"issues",target,"evidence_for"))
                else: unresolved.append({"source_table":table,"source_id":oid,"table":"issues","key":str(raw["issue_key"]),"reason":"missing"})
            if table == "events":
                for ref in _legacy_refs(raw.get("refs")):
                    rt, ri = ref["table"], str(ref["id"])
                    if ri not in rows.get(rt,{}):
                        unresolved.append({"source_table":table,"source_id":oid,"table":rt,"id":ri,"reason":"missing_or_unsupported"}); continue
                    relation = str(ref.get("relation") or "context")
                    if rt in {"goals","tasks"}: expected_links.append((table,oid,rt,ri,relation if relation in {"context","affects","evidence"} else "context"))
                    else: expected_relations.append((table,oid,rt,ri,relation if relation in {"supports","contradicts","evidence_for","follows"} else "follows"))
    return rows, expected_links, expected_relations, unresolved


def migrate_candidate(source, candidate_dir):
    source=Path(source); candidate_dir=Path(candidate_dir)
    if not source.is_file() or source.stat().st_size==0: raise ValueError("missing or zero-byte source")
    candidate_dir.mkdir(parents=True,exist_ok=False); backup=candidate_dir/"legacy.db"
    src=dst=old=new=logical=None
    try:
        src=sqlite3.connect(f"file:{source}?mode=ro",uri=True); src.row_factory=sqlite3.Row
        tables=_legacy_tables(src); version=src.execute("PRAGMA user_version").fetchone()[0]
        integrity=src.execute("PRAGMA integrity_check").fetchone()[0]
        if version>2 or integrity!="ok" or not tables.intersection({"meta","goals","tasks","decisions","issues","experiments","events"}):
            raise ValueError("unsupported or corrupt source")
        dst=sqlite3.connect(backup); src.backup(dst); dst.close(); dst=None; src.close(); src=None
        old=sqlite3.connect(backup); old.row_factory=sqlite3.Row
        identity=_legacy_identity(old,source); expected_rows, expected_links, expected_relations, unresolved=_expected_migration(old)
        out=candidate_dir/"fact.db"; t=now(); wid=str(uuid.uuid4())
        _init_db(out,{"id":wid,**identity,"kind":"project","created_at":t})
        new=_connect(out,True); new.execute("BEGIN IMMEDIATE")
        raw_rows={}
        for table in ("meta","goals","tasks","decisions","issues","experiments","events"):
            exists=old.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()
            if not exists: continue
            rows=old.execute(f'SELECT * FROM "{table}"').fetchall()
            if table=="meta":
                for r in rows:
                    new.execute("INSERT OR REPLACE INTO legacy_meta VALUES(?,?)",(r["key"],r["value"]))
                continue
            for r in rows:
                raw=dict(r); oid=str(raw.get("id")); nid=str(uuid.uuid4()); key=raw.get("issue_key") or raw.get("decision_key"); raw_rows[(table,oid)]=raw
                if table in {"goals","tasks"}:
                    kind="goal" if table=="goals" else "task"; status={"todo":"planned","doing":"active","done":"done","blocked":"blocked","cancelled":"cancelled","active":"active"}.get(raw.get("status"),"planned")
                    new.execute("INSERT INTO items(id,kind,title,description,status,position,completed_at,legacy_payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(nid,kind,raw.get("title") or "legacy",raw.get("notes") or raw.get("done_criteria"),status,raw.get("position") if raw.get("position") is not None else int(oid),raw.get("completed_at"),_json(raw),raw.get("created_at") or t,raw.get("updated_at") or raw.get("created_at") or t))
                    ntype="item"
                else:
                    kind={"decisions":"decision","issues":"issue","experiments":"experiment","events":"note"}[table]; title=raw.get("title") or raw.get("name") or raw.get("text") or "legacy"
                    body=raw.get("summary") or raw.get("actual_result") or raw.get("text") or raw.get("notes") or title
                    status=raw.get("status") or ("active" if kind=="decision" else "open" if kind=="issue" else "active")
                    payload={k:v for k,v in raw.items() if k not in {"id","title","name","text","status","created_at","updated_at","issue_key","decision_key"}}
                    new.execute("INSERT INTO records(id,kind,title,body,payload_json,status,issue_key,decision_key,legacy_payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(nid,kind,title,body,_json(payload),status,raw.get("issue_key") if kind=="issue" else None,raw.get("decision_key") if kind=="decision" else None,_json(raw),raw.get("created_at") or t,raw.get("updated_at") or raw.get("created_at") or t)); ntype="record"
                new.execute("INSERT INTO legacy_map VALUES(?,?,?,?,?)",(table,oid,key,ntype,nid))
        for (table,oid),raw in raw_rows.items():
            nid=new.execute("SELECT new_id FROM legacy_map WHERE old_table=? AND old_id=?",(table,oid)).fetchone()[0]
            goal_id=raw.get("parent_goal_id") if table=="goals" else raw.get("goal_id") or raw.get("parent_goal_id") if table=="tasks" else raw.get("goal_id")
            if goal_id is not None:
                goal=new.execute("SELECT new_id FROM legacy_map WHERE old_table='goals' AND old_id=?",(str(goal_id),)).fetchone()
                if goal and table in {"goals","tasks"}: new.execute("UPDATE items SET parent_id=? WHERE id=?",(goal[0],nid))
                elif goal: new.execute("INSERT INTO record_links VALUES(?,?,?)",(nid,goal[0],"context"))
            if table=="experiments" and raw.get("issue_key"):
                issue=new.execute("SELECT new_id FROM legacy_map WHERE old_table='issues' AND old_key=?",(raw["issue_key"],)).fetchone()
                if issue: new.execute("INSERT INTO record_relations VALUES(?,?,?)",(nid,issue[0],"evidence_for"))
            if table=="events":
                for ref in _legacy_refs(raw.get("refs")):
                    target=new.execute("SELECT new_type,new_id FROM legacy_map WHERE old_table=? AND old_id=?",(ref["table"],str(ref["id"]))).fetchone()
                    if not target: continue
                    relation=str(ref.get("relation") or "context")
                    if target[0]=="item": new.execute("INSERT OR IGNORE INTO record_links VALUES(?,?,?)",(nid,target[1],relation if relation in {"context","affects","evidence"} else "context"))
                    else: new.execute("INSERT OR IGNORE INTO record_relations VALUES(?,?,?)",(nid,target[1],relation if relation in {"supports","contradicts","evidence_for","follows"} else "follows"))
        new.execute("UPDATE work SET status='imported_unreviewed'")
        new.execute("INSERT INTO history(operation,target_type,target_id,after_json,occurred_at) VALUES('migration','work',?,?,?)",(wid,_json({"source":str(source),"schema":version}),t)); new.commit()
        new.close();new=None; old.close();old=None
        logical=sqlite3.connect(f"file:{out}?mode=ro",uri=True)
        manifest={"source":str(source),"source_sha256":hashlib.sha256(backup.read_bytes()).hexdigest(),
              "candidate_sha256":hashlib.sha256(out.read_bytes()).hexdigest(),"schema":version,
              "mapped_rows":len(raw_rows),"unresolved_refs":unresolved,"logical":_logical_manifest(logical)}
        logical.close();logical=None
        manifest_path=candidate_dir/"manifest.json"
        manifest_bytes=(json.dumps(manifest,ensure_ascii=False,sort_keys=True,indent=2)+"\n").encode()
        manifest_path.write_bytes(manifest_bytes)
        (candidate_dir/"manifest.sha256").write_text(hashlib.sha256(manifest_bytes).hexdigest()+"\n")
        return str(out)
    except Exception:
        if new is not None:
            try: new.rollback()
            except Exception: pass
        for con in (logical,new,old,dst,src):
            if con is not None:
                try: con.close()
                except Exception: pass
        shutil.rmtree(candidate_dir,ignore_errors=True)
        raise


def verify_candidate(path, evidence_dir=None):
    try:
        path=Path(path); evidence=Path(evidence_dir) if evidence_dir else path.parent
        manifest_path=evidence/"manifest.json"; checksum_path=evidence/"manifest.sha256"
        manifest_bytes=manifest_path.read_bytes(); manifest=json.loads(manifest_bytes)
        manifest_ok=checksum_path.read_text().strip()==hashlib.sha256(manifest_bytes).hexdigest()
        con=_connect(path); integrity=con.execute("PRAGMA integrity_check").fetchone()[0]; foreign_keys=con.execute("PRAGMA foreign_key_check").fetchall(); mapped=con.execute("SELECT COUNT(*) FROM legacy_map").fetchone()[0]
        schema_query="SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' ORDER BY type,name"
        with sqlite3.connect(":memory:") as expected_schema:
            expected_schema.executescript(SCHEMA)
            schema_ok=[tuple(row) for row in con.execute(schema_query)]==expected_schema.execute(schema_query).fetchall()
        missing=con.execute("SELECT COUNT(*) FROM legacy_map m WHERE (m.new_type='item' AND NOT EXISTS(SELECT 1 FROM items i WHERE i.id=m.new_id)) OR (m.new_type='record' AND NOT EXISTS(SELECT 1 FROM records r WHERE r.id=m.new_id))").fetchone()[0]; mismatches=[]
        backup=evidence/"legacy.db"
        backup_ok=backup.is_file() and hashlib.sha256(backup.read_bytes()).hexdigest()==manifest.get("source_sha256")
        coverage_ok=False; relationships_ok=False; normalized_ok=False; unresolved_ok=False
        mapping_ok=False; meta_ok=False; identity_ok=False
        if backup_ok:
            old=sqlite3.connect(backup); old.row_factory=sqlite3.Row
            expected_rows, expected_links, expected_relations, unresolved = _expected_migration(old)
            expected=sum(len(rows) for rows in expected_rows.values())
            normalized_ok=True
            expected_maps={}
            for table, rows in expected_rows.items():
                for oid, raw in rows.items():
                    expected_maps[(table,oid)]=(raw.get("issue_key") or raw.get("decision_key"),
                                                "item" if table in {"goals","tasks"} else "record")
            actual_maps={(m[0],m[1]):(m[2],m[3],m[4]) for m in con.execute("SELECT * FROM legacy_map")}
            mapping_ok=(set(actual_maps)==set(expected_maps) and all(
                actual_maps[key][:2] == value for key,value in expected_maps.items()))
            old_meta={str(r[0]):r[1] for r in old.execute("SELECT key,value FROM meta")} if "meta" in _legacy_tables(old) else {}
            new_meta={str(r[0]):r[1] for r in con.execute("SELECT key,value FROM legacy_meta")}
            expected_meta=dict(old_meta)
            expected_identity=_legacy_identity(old,Path(manifest.get("source") or backup))
            association_path=evidence.parent.parent/"central-associations.json"
            enriched=association_path.is_file() or "registry_snapshot" in new_meta or "registry_snapshot" in manifest
            if enriched:
                association=json.loads(association_path.read_text())[evidence.name]
                central_path=association_path.parent/"central-backup.db"
                central_hash=hashlib.sha256(central_path.read_bytes()).hexdigest()
                if central_hash!=association["central_backup_sha256"]: raise ValueError("central backup changed")
                with sqlite3.connect(f"file:{central_path.resolve()}?mode=ro",uri=True) as central:
                    central.row_factory=sqlite3.Row
                    registry=dict(central.execute("SELECT * FROM project_registry WHERE slug=?",(evidence.name,)).fetchone())
                if str(Path(registry["db_path"]).resolve())!=association["source_path"]:
                    raise ValueError("central association mismatch")
                expected_identity=_legacy_identity(old,Path(association["source_path"]))
                snapshot={"raw":registry,"registry_slug":evidence.name,"internal_slug":expected_identity["slug"],
                          "source_path":association["source_path"],"central_backup_sha256":central_hash}
                parentless=[]
                # Legacy numeric IDs are scoped to their source table; goal IDs may collide.
                parent_ids={relation[1] for relation in expected_relations if relation[0]=="tasks" and relation[4]=="parent"}
                for old_id, raw in expected_rows.get("tasks",{}).items():
                    if old_id not in parent_ids:
                        mapped_row=con.execute("SELECT new_id FROM legacy_map WHERE old_table='tasks' AND old_id=?",(old_id,)).fetchone()
                        parentless.append({"legacy_id":old_id,"item_id":mapped_row[0],"raw":raw})
                report={"counts":{"parentless_tasks":len(parentless),"unresolved_refs":len(unresolved)},
                        "parentless_tasks":parentless,"unresolved_refs":unresolved,"acknowledged":False,"resolved":False}
                for key,value in {"registry_snapshot":snapshot,"migration_review_report":report}.items():
                    if key in old_meta: raise ValueError("reserved legacy metadata collision")
                    if manifest.get(key)!=value: raise ValueError("registry/review manifest mismatch")
                    if key not in new_meta or json.loads(new_meta[key])!=value: raise ValueError("registry/review metadata mismatch")
                    expected_meta[key]=new_meta[key]
                expected_identity["name"]=registry.get("name") or expected_identity["name"]
            meta_ok=new_meta==expected_meta
            work_identity=con.execute("SELECT slug,name,purpose FROM work").fetchone()
            identity_ok=tuple(work_identity)==(expected_identity["slug"],expected_identity["name"],expected_identity["purpose"])
            for m in con.execute("SELECT * FROM legacy_map"):
                raw=old.execute(f'SELECT * FROM "{m[0]}" WHERE CAST(id AS TEXT)=?',(m[1],)).fetchone()
                target=con.execute("SELECT legacy_payload FROM items WHERE id=?",(m[4],)).fetchone() if m[3]=="item" else con.execute("SELECT legacy_payload FROM records WHERE id=?",(m[4],)).fetchone()
                if not raw or not target or json.loads(target[0])!=dict(raw): mismatches.append({"table":m[0],"id":m[1]})
                if raw and target:
                    raw=dict(raw)
                    if m[3]=="item":
                        actual=con.execute("SELECT kind,title,description,status,position,completed_at,created_at,updated_at FROM items WHERE id=?",(m[4],)).fetchone()
                        kind="goal" if m[0]=="goals" else "task"; status={"todo":"planned","doing":"active","done":"done","blocked":"blocked","cancelled":"cancelled","active":"active"}.get(raw.get("status"),"planned")
                        wanted=(kind,raw.get("title") or "legacy",raw.get("notes") or raw.get("done_criteria"),status,raw.get("position") if raw.get("position") is not None else int(m[1]),raw.get("completed_at"),raw.get("created_at") or actual[6],raw.get("updated_at") or raw.get("created_at") or actual[7])
                    else:
                        kind={"decisions":"decision","issues":"issue","experiments":"experiment","events":"note"}[m[0]]; title=raw.get("title") or raw.get("name") or raw.get("text") or "legacy"; body=raw.get("summary") or raw.get("actual_result") or raw.get("text") or raw.get("notes") or title; status=raw.get("status") or ("active" if kind=="decision" else "open" if kind=="issue" else "active")
                        actual=con.execute("SELECT kind,title,body,status,issue_key,decision_key,created_at,updated_at FROM records WHERE id=?",(m[4],)).fetchone()
                        wanted=(kind,title,body,status,raw.get("issue_key") if kind=="issue" else None,raw.get("decision_key") if kind=="decision" else None,raw.get("created_at") or actual[6],raw.get("updated_at") or raw.get("created_at") or actual[7])
                        expected_payload={k:v for k,v in raw.items() if k not in {"id","title","name","text","status","created_at","updated_at","issue_key","decision_key"}}
                        payload_row=con.execute("SELECT payload_json FROM records WHERE id=?",(m[4],)).fetchone()
                        if not payload_row or json.loads(payload_row[0]) != expected_payload:
                            normalized_ok=False; mismatches.append({"table":m[0],"id":m[1],"kind":"payload_json"})
                    if tuple(actual)!=wanted: normalized_ok=False; mismatches.append({"table":m[0],"id":m[1],"kind":"normalized"})
            coverage_ok = expected == mapped == manifest.get("mapped_rows")
            def mapped_id(table, oid):
                row=con.execute("SELECT new_id FROM legacy_map WHERE old_table=? AND old_id=?",(table,oid)).fetchone()
                return row[0] if row else None
            link_set={(r[0],r[1],r[2]) for r in con.execute("SELECT record_id,item_id,relation FROM record_links")}
            relation_set={(r[0],r[1],r[2]) for r in con.execute("SELECT source_id,target_id,relation FROM record_relations")}
            parent_set={(r[0],r[1]) for r in con.execute("SELECT id,parent_id FROM items WHERE parent_id IS NOT NULL")}
            wanted_links={(mapped_id(a,b),mapped_id(c,d),e) for a,b,c,d,e in expected_links}
            wanted_record_rel={(mapped_id(a,b),mapped_id(c,d),e) for a,b,c,d,e in expected_relations if e!="parent"}
            wanted_parents={(mapped_id(a,b),mapped_id(c,d)) for a,b,c,d,e in expected_relations if e=="parent"}
            relationships_ok=(link_set==wanted_links and relation_set==wanted_record_rel and parent_set==wanted_parents)
            unresolved_ok=manifest.get("unresolved_refs",[])==unresolved
            old.close()
        logical=_logical_manifest(con); logical_ok=logical==manifest.get("logical")
        con.close()
        return {"ok":integrity=="ok" and schema_ok and not foreign_keys and missing==0 and not mismatches and manifest_ok and logical_ok and backup_ok and coverage_ok and normalized_ok and relationships_ok and unresolved_ok and mapping_ok and meta_ok and identity_ok,
                "integrity":integrity,"foreign_key_errors":len(foreign_keys),"mapped":mapped,"missing":missing,
                "mismatches":mismatches,"manifest_ok":manifest_ok,"logical_ok":logical_ok,
                "backup_ok":backup_ok,"coverage_ok":coverage_ok,"normalized_ok":normalized_ok,
                "relationships_ok":relationships_ok,"unresolved_ok":unresolved_ok,
                "mapping_ok":mapping_ok,"meta_ok":meta_ok,"identity_ok":identity_ok,"schema_ok":schema_ok}
    except Exception as e:return {"ok":False,"error":str(e)}
