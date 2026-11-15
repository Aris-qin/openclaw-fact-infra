import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from plugin.v3 import plan, read
from scripts.central_handoff import prepare_handoff, register_handoff


def legacy_work(path: Path, slug: str, *, parentless=False, unresolved=False):
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
    CREATE TABLE goals(id INTEGER PRIMARY KEY,title TEXT,status TEXT);
    CREATE TABLE tasks(id INTEGER PRIMARY KEY,goal_id INTEGER,title TEXT,status TEXT);
    CREATE TABLE events(id INTEGER PRIMARY KEY,text TEXT,refs TEXT);
    """)
    con.executemany("INSERT INTO meta VALUES(?,?)", [("slug", slug), ("name", slug), ("purpose", "migrated legacy work")])
    con.execute("INSERT INTO goals VALUES(1,'goal','todo')")
    con.execute("INSERT INTO tasks VALUES(1,?,?,?)", (None if parentless else 1, "task", "todo"))
    if unresolved:
        con.execute("INSERT INTO events VALUES(1,'old ref',?)", (json.dumps([{"table": "goals", "id": 99}]),))
    con.commit(); con.close()


def old_index(path: Path, one: Path, two: Path):
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE project_registry(slug TEXT PRIMARY KEY,db_path TEXT,name TEXT,status TEXT,category TEXT,last_activity_at TEXT,extra TEXT);
    CREATE TABLE journals(id INTEGER PRIMARY KEY,content TEXT);
    """)
    con.execute("INSERT INTO project_registry VALUES(?,?,?,?,?,?,?)", ("legacy-one", str(one), "中文项目一", "active", "research", None, "raw-a"))
    con.execute("INSERT INTO project_registry VALUES(?,?,?,?,?,?,?)", ("legacy-two", str(two), "中文项目二", "archived", "ops", "2026-01-01T00:00:00Z", "raw-b"))
    con.execute("INSERT INTO journals VALUES(1,'corpus journal')")
    con.commit(); con.close()


def test_prepare_preserves_registry_journals_aliases_and_review(tmp_path):
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    legacy_work(a, "internal-one", parentless=True, unresolved=True)
    legacy_work(b, "internal-two")
    index = tmp_path / "old-index.db"; old_index(index, a, b)
    finals = {"legacy-one": str(tmp_path / "final-a" / "fact.db"), "legacy-two": str(tmp_path / "final-b" / "fact.db")}
    result = prepare_handoff(index, tmp_path / "handoff", finals)
    assert result["ok"] and result["entries"][0]["canonical_slug"] == "internal-one"
    assert (tmp_path / "handoff" / "central-backup.db").is_file()
    backup = sqlite3.connect(tmp_path / "handoff" / "central-backup.db")
    assert backup.execute("SELECT content FROM journals").fetchone()[0] == "corpus journal"
    backup.close()
    candidate = Path(result["entries"][0]["candidate_db"])
    con = sqlite3.connect(candidate)
    assert con.execute("SELECT name FROM work").fetchone()[0] == "中文项目一"
    report = json.loads(con.execute("SELECT value FROM legacy_meta WHERE key='migration_review_report'").fetchone()[0])
    assert report["counts"] == {"parentless_tasks": 1, "unresolved_refs": 1}
    assert con.execute("SELECT parent_id FROM items WHERE kind='task'").fetchone()[0] is None
    con.close()
    assert prepare_handoff(index, tmp_path / "handoff", finals) == result


def test_prepare_rejects_duplicate_alias_before_candidates(tmp_path):
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    legacy_work(a, "same"); legacy_work(b, "same")
    index = tmp_path / "old-index.db"; old_index(index, a, b)
    with pytest.raises(ValueError, match="alias"):
        prepare_handoff(index, tmp_path / "handoff", {"legacy-one": str(tmp_path/"x.db"), "legacy-two": str(tmp_path/"y.db")})
    assert not (tmp_path / "handoff").exists()


def test_register_checks_final_identity_atomic_rollback_and_exact_retry(tmp_path, monkeypatch):
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    legacy_work(a, "internal-one", unresolved=True); legacy_work(b, "internal-two")
    old = tmp_path / "old.db"; old_index(old, a, b)
    finals = {"legacy-one": str(tmp_path/"published-a.db"), "legacy-two": str(tmp_path/"published-b.db")}
    prepared = prepare_handoff(old, tmp_path/"handoff", finals)
    for entry in prepared["entries"]:
        Path(entry["final_db"]).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(entry["candidate_db"], entry["final_db"])
    central = tmp_path / "new-index.db"
    monkeypatch.setenv("FACT_HANDOFF_FAULT", "before_commit")
    failed = register_handoff(tmp_path/"handoff", central, "register-1")
    assert not failed["ok"]
    with sqlite3.connect(central) as con:
        assert con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    monkeypatch.delenv("FACT_HANDOFF_FAULT")
    done = register_handoff(tmp_path/"handoff", central, "register-1")
    assert done["ok"] and register_handoff(tmp_path/"handoff", central, "register-1") == done
    con = sqlite3.connect(central)
    assert con.execute("SELECT COUNT(*) FROM work_registry").fetchone()[0] == 2
    assert con.execute("SELECT work_id FROM work_aliases WHERE alias='legacy-one'").fetchone() == con.execute("SELECT work_id FROM work_registry WHERE slug='internal-one'").fetchone()
    con.close()
    sqlite3.connect(finals["legacy-two"]).execute("UPDATE work SET name='tampered'").connection.commit()
    conflict = register_handoff(tmp_path/"handoff", central, "register-2")
    assert not conflict["ok"] and conflict["error"]["code"] == "FINAL_MISMATCH"


def test_adoption_requires_review_ack_and_archived_stays_archived(tmp_path):
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    legacy_work(a, "internal-one", parentless=True, unresolved=True); legacy_work(b, "internal-two")
    old = tmp_path / "old.db"; old_index(old, a, b)
    prepared = prepare_handoff(old, tmp_path/"handoff", {"legacy-one":str(tmp_path/"fa.db"),"legacy-two":str(tmp_path/"fb.db")})
    active, archived = [entry["candidate_db"] for entry in prepared["entries"]]
    proposed=json.loads(plan({"action":"propose","work":active,"request_id":"p1","changes":[{"op":"adopt"}]}))
    denied=json.loads(plan({"action":"apply","work":active,"request_id":"a1","proposal_id":proposed["data"]["proposal_id"],"confirmation":{"summary":"reviewed"}}))
    assert denied["error"]["code"] == "MIGRATION_REVIEW_ACK_REQUIRED"
    proposed=json.loads(plan({"action":"propose","work":active,"request_id":"p2","changes":[{"op":"adopt","acknowledge_migration_review":True}]}))
    assert json.loads(plan({"action":"apply","work":active,"request_id":"a2","proposal_id":proposed["data"]["proposal_id"],"confirmation":{"summary":"reviewed unresolved items"}}))["ok"]
    current=json.loads(read({"action":"current","work":active}))
    assert current["data"]["migration_review"]["acknowledged"] is True
    assert current["data"]["migration_review"]["resolved"] is False
    proposed=json.loads(plan({"action":"propose","work":archived,"request_id":"p3","changes":[{"op":"adopt"}]}))
    assert json.loads(plan({"action":"apply","work":archived,"request_id":"a3","proposal_id":proposed["data"]["proposal_id"],"confirmation":{"summary":"archive reviewed"}}))["ok"]
    assert sqlite3.connect(archived).execute("SELECT status FROM work").fetchone()[0] == "archived"
