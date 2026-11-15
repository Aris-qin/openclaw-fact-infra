import json
import sqlite3
from pathlib import Path

import pytest

from plugin import v3


def legacy(path, version=0):
    path.parent.mkdir(parents=True,exist_ok=True)
    con=sqlite3.connect(path)
    con.executescript("""
      CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
      INSERT INTO meta VALUES('slug','kept-slug'); INSERT INTO meta VALUES('purpose','kept purpose');
      CREATE TABLE goals(id INTEGER PRIMARY KEY,title TEXT,status TEXT,parent_goal_id INTEGER,position INTEGER,created_at TEXT,completed_at TEXT);
      CREATE TABLE tasks(id INTEGER PRIMARY KEY,title TEXT,status TEXT,goal_id INTEGER,notes TEXT,created_at TEXT,updated_at TEXT);
      CREATE TABLE events(id INTEGER PRIMARY KEY,goal_id INTEGER,text TEXT,refs TEXT,created_at TEXT);
      INSERT INTO goals VALUES(1,'G','active',NULL,1,'old',NULL);
      INSERT INTO tasks VALUES(2,'T','done',1,NULL,'old',NULL);
      INSERT INTO events VALUES(3,1,'E','[{"table":"tasks","id":2},{"table":"issues","id":99}]','old');
    """)
    con.execute(f"PRAGMA user_version={version}"); con.commit(); return con


def test_zero_version_known_schema_and_unresolved_report(tmp_path):
    source=tmp_path/"legacy"/"fact.db"; legacy(source).close()
    out=Path(v3.migrate_candidate(source,tmp_path/"candidate"))
    report=v3.verify_candidate(out)
    assert report["ok"] and report["relationships_ok"] and report["normalized_ok"]
    manifest=json.loads((out.parent/"manifest.json").read_text())
    assert manifest["unresolved_refs"] == [{"id":"99","reason":"missing_or_unsupported","source_id":"3","source_table":"events","table":"issues"}]


def test_wal_backup_includes_committed_uncheckpointed_rows(tmp_path):
    source=tmp_path/"legacy"/"fact.db"; con=legacy(source)
    con.execute("PRAGMA journal_mode=WAL"); con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute("INSERT INTO tasks VALUES(4,'WAL','done',1,NULL,'new',NULL)"); con.commit()
    out=Path(v3.migrate_candidate(source,tmp_path/"candidate"))
    assert sqlite3.connect(out).execute("SELECT COUNT(*) FROM legacy_map WHERE old_table='tasks'").fetchone()[0] == 2
    assert v3.verify_candidate(out)["ok"]
    con.close()


@pytest.mark.parametrize("kind",["unknown","future","corrupt","zero_byte"])
def test_bad_sources_rejected_without_candidate(tmp_path, kind):
    source=tmp_path/"legacy"/"fact.db"; source.parent.mkdir()
    if kind=="unknown":
        con=sqlite3.connect(source); con.execute("CREATE TABLE unrelated(x)"); con.commit(); con.close()
    elif kind=="future":
        con=legacy(source,99); con.close()
    elif kind=="corrupt": source.write_bytes(b"not sqlite")
    else: source.touch()
    with pytest.raises((ValueError,sqlite3.DatabaseError)): v3.migrate_candidate(source,tmp_path/"candidate")
    assert not (tmp_path/"candidate").exists()


def test_candidate_collision_preserves_existing_directory(tmp_path):
    source=tmp_path/"legacy"/"fact.db"; legacy(source).close()
    candidate=tmp_path/"candidate"; candidate.mkdir(); marker=candidate/"keep"; marker.write_text("mine")
    with pytest.raises(FileExistsError): v3.migrate_candidate(source,candidate)
    assert marker.read_text()=="mine"


def test_all_tool_schemas_and_runtime_reject_irrelevant_placeholders(tmp_path, monkeypatch):
    schemas={tool["name"]:tool["parameters"] for tool in v3.schemas()}
    invalid={
      "fact_create":{"action":"create","slug":"x","purpose":"p","kind":"light","request_id":"r","name":False},
      "fact_read":{"action":"overview","work":"placeholder"},
      "fact_plan":{"action":"apply","work":"x","request_id":"r","proposal_id":"p","confirmation":{"summary":"ok"},"changes":[]},
      "fact_record":{"action":"append","work":"x","request_id":"r","records":[],"record_id":"placeholder"},
      "fact_finish":{"action":"continue","work":"x","request_id":"r","proposal_id":"p","target_id":"placeholder"},
    }
    for name,args in invalid.items():
        with pytest.raises(ValueError): v3._validate_schema_value(args,schemas[name],name)
        result=json.loads(v3.HANDLERS[name](args))
        assert result["error"]["code"]=="VALIDATION_ERROR"
    valid={
      "fact_create":{"action":"create","slug":"x","purpose":"p","kind":"light","request_id":"r"},
      "fact_read":{"action":"detail","work":"x","collection":"records","limit":2},
      "fact_plan":{"action":"reject","work":"x","request_id":"r","proposal_id":"p"},
      "fact_record":{"action":"block","work":"x","request_id":"r","item_id":"i","expected_version":1,"reason":"why"},
      "fact_finish":{"action":"prepare","work":"x","request_id":"r","target_type":"work","target_id":"i"},
    }
    for name,args in valid.items(): v3._validate_schema_value(args,schemas[name],name)
