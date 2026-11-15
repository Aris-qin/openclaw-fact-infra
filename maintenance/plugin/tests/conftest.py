import os
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def fact_index_db(monkeypatch, tmp_path):
    path = tmp_path / "index" / "fact-index.db"
    monkeypatch.setenv("FACT_INDEX_DB", str(path))
    monkeypatch.setenv("FACT_PROJECTS_ROOT", str(tmp_path))
    return path


@pytest.fixture
def project_factory(tmp_path):
    def make(slug="test-project", *, with_db=True):
        project_dir = tmp_path / slug
        project_dir.mkdir()
        if with_db:
            db_path = project_dir / "fact.db"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE marker (id INTEGER)")
            conn.commit()
            conn.close()
        return project_dir
    return make


@pytest.fixture(scope="session", autouse=True)
def session_fact_index_db(tmp_path_factory):
    """Keep all tests' central index isolated from the user's real home."""
    mp = pytest.MonkeyPatch()
    index_path = tmp_path_factory.mktemp("fact-index") / "fact-index.db"
    mp.setenv("FACT_INDEX_DB", str(index_path))
    yield index_path
    mp.undo()


@pytest.fixture
def make_project(tmp_path):
    """Create a project/index database in one of the migration test shapes."""
    def make(slug, shape):
        project_dir = tmp_path / slug
        project_dir.mkdir()
        db_path = project_dir / "fact.db"

        if shape == "empty":
            return project_dir
        if shape == "zero-bytes":
            db_path.touch()
            return project_dir
        if shape == "legacy-central":
            index_path = Path(os.environ["FACT_INDEX_DB"])
            index_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(index_path)
            conn.execute("CREATE TABLE journals (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL)")
            conn.execute("""CREATE TABLE project_registry (
                slug TEXT PRIMARY KEY, db_path TEXT NOT NULL, name TEXT,
                status TEXT DEFAULT 'active', summary TEXT, target_journal TEXT,
                last_activity_at TEXT, updated_at TEXT
            )""")
            for i in range(5):
                conn.execute("INSERT INTO project_registry(slug, db_path, name) VALUES(?,?,?)",
                             (f"legacy-{i}", str(project_dir / f"p{i}" / "fact.db"), f"Legacy {i}"))
            conn.execute("INSERT INTO journals(name) VALUES('Duplicate Journal')")
            conn.execute("INSERT INTO journals(name) VALUES('Duplicate Journal')")
            conn.commit()
            conn.close()
            return project_dir

        conn = sqlite3.connect(db_path)
        # Historical project schema (v1), with optional old experiments shape.
        conn.executescript("""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
                type TEXT DEFAULT 'task', status TEXT DEFAULT 'todo', priority TEXT DEFAULT 'medium',
                notes TEXT, completed_at TEXT, created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')));
            CREATE TABLE decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, decision_key TEXT UNIQUE,
                title TEXT NOT NULL, status TEXT DEFAULT 'active', summary TEXT, rationale TEXT,
                source_file TEXT, created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now')));
            CREATE TABLE issues (id INTEGER PRIMARY KEY AUTOINCREMENT, issue_key TEXT UNIQUE,
                title TEXT NOT NULL, status TEXT DEFAULT 'open', severity TEXT DEFAULT 'medium',
                hypothesis TEXT, workaround TEXT, resolved_at TEXT, created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')));
            CREATE TABLE experiments (id INTEGER PRIMARY KEY AUTOINCREMENT, issue_key TEXT,
                name TEXT NOT NULL, input_config TEXT, expected_result TEXT, actual_result TEXT,
                verdict TEXT DEFAULT 'pending', run_at TEXT, notes TEXT, created_at TEXT DEFAULT (datetime('now')));
            INSERT INTO meta(key,value) VALUES ('schema_version','1');
        """)
        if shape == "v2":
            conn.execute("ALTER TABLE experiments ADD COLUMN updated_at TEXT DEFAULT (datetime('now'))")
            conn.executescript("""
                CREATE TABLE goals (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
                    done_criteria TEXT, status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','done','abandoned')),
                    parent_goal_id INTEGER, phase TEXT, origin TEXT NOT NULL DEFAULT 'normal' CHECK(origin IN ('normal','backfilled')),
                    position INTEGER, created_at TEXT DEFAULT (datetime('now')), completed_at TEXT);
                CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, goal_id INTEGER, phase TEXT,
                    text TEXT NOT NULL, refs TEXT, created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now')));
            """)
            conn.execute("PRAGMA user_version=2")
        elif shape not in ("v1", "legacy-exp"):
            conn.close()
            raise ValueError(f"unknown project shape: {shape}")
        if shape in ("v1", "v2", "legacy-exp"):
            for table, column in (("tasks", "title"), ("decisions", "title"), ("issues", "title"), ("experiments", "name")):
                for i in range(2):
                    conn.execute(f"INSERT INTO {table}({column}) VALUES(?)", (f"{table}-{i}",))
        conn.commit()
        conn.close()
        return project_dir

    return make
