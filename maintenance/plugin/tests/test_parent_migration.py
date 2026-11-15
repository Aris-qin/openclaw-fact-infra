import json,sqlite3,sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from plugin import v3

def seeded(root):
 source=root/'legacy'/'fact.db';source.parent.mkdir()
 con=sqlite3.connect(source);con.executescript("""
 CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT); INSERT INTO meta VALUES('slug','original-slug');INSERT INTO meta VALUES('purpose','original purpose');
 CREATE TABLE goals(id INTEGER PRIMARY KEY,title TEXT,status TEXT,parent_goal_id INTEGER,position INTEGER,created_at TEXT,completed_at TEXT);
 CREATE TABLE tasks(id INTEGER PRIMARY KEY,title TEXT,status TEXT,goal_id INTEGER,notes TEXT,created_at TEXT,updated_at TEXT);
 CREATE TABLE issues(id INTEGER PRIMARY KEY,issue_key TEXT,title TEXT,status TEXT,goal_id INTEGER,created_at TEXT);
 CREATE TABLE events(id INTEGER PRIMARY KEY,goal_id INTEGER,text TEXT,refs TEXT,created_at TEXT);
 INSERT INTO goals VALUES(1,'G','active',NULL,1,'old',NULL);
 INSERT INTO tasks VALUES(2,'T','done',1,NULL,'old',NULL);
 INSERT INTO issues VALUES(3,'I-3','I','open',1,'old');
 INSERT INTO events VALUES(4,1,'event','[{"table":"tasks","id":2},{"table":"issues","id":3}]','old');PRAGMA user_version=2;
 """);con.commit();con.close();return source

def test_event_refs_become_usable_relations(tmp_path):
 source=seeded(tmp_path);out=Path(v3.migrate_candidate(source,tmp_path/'candidate'));con=sqlite3.connect(out);eid=con.execute("select new_id from legacy_map where old_table='events' and old_id='4'").fetchone()[0];tid=con.execute("select new_id from legacy_map where old_table='tasks' and old_id='2'").fetchone()[0];iid=con.execute("select new_id from legacy_map where old_table='issues' and old_id='3'").fetchone()[0]
 assert con.execute('select count(*) from record_links where record_id=? and item_id=?',(eid,tid)).fetchone()[0]>0
 assert con.execute('select count(*) from record_relations where source_id=? and target_id=?',(eid,iid)).fetchone()[0]>0
 con.close()

def test_migration_init_failure_cleans_only_owned_candidate(tmp_path,monkeypatch):
 source=seeded(tmp_path);before=source.read_bytes();dest=tmp_path/'candidate'
 def fail(*a,**kw):raise sqlite3.OperationalError('database or disk is full')
 monkeypatch.setattr(v3,'_init_db',fail)
 with pytest.raises(sqlite3.OperationalError):v3.migrate_candidate(source,dest)
 assert source.read_bytes()==before
 assert not dest.exists()

def test_migration_manifest_failure_cleans_owned_candidate(tmp_path,monkeypatch):
 source=seeded(tmp_path);before=source.read_bytes();dest=tmp_path/'candidate';original=Path.write_bytes
 def fail(self,*args,**kw):
  if self.name=='manifest.json':raise OSError('disk full')
  return original(self,*args,**kw)
 monkeypatch.setattr(Path,'write_bytes',fail)
 with pytest.raises(OSError):v3.migrate_candidate(source,dest)
 assert source.read_bytes()==before
 assert not dest.exists()

def test_migration_preserves_identity_purpose(tmp_path):
 source=seeded(tmp_path);out=Path(v3.migrate_candidate(source,tmp_path/'candidate'));con=sqlite3.connect(out);row=con.execute('select slug,purpose from work').fetchone();con.close();assert row==('original-slug','original purpose')

def test_handoff_review_keeps_parentless_task_with_colliding_goal_id(tmp_path):
 """Legacy IDs are table-scoped: a nested goal must not hide a root task."""
 from scripts.central_handoff import prepare_handoff, verify_handoff
 source=seeded(tmp_path)
 with sqlite3.connect(source) as con:
  con.execute("INSERT INTO goals VALUES(2,'nested goal','active',1,2,'old',NULL)")
  con.execute("UPDATE tasks SET goal_id=NULL WHERE id=2")
  con.execute("INSERT INTO tasks VALUES(3,'linked task','done',1,NULL,'old',NULL)")
 before=source.read_bytes()
 index=tmp_path/'old-index.db'
 with sqlite3.connect(index) as con:
  con.execute('CREATE TABLE project_registry(slug TEXT PRIMARY KEY,db_path TEXT,name TEXT,status TEXT)')
  con.execute('INSERT INTO project_registry VALUES(?,?,?,?)',('original-slug',str(source),'Original','archived'))
 handoff=tmp_path/'handoff'
 result=prepare_handoff(index,handoff,{'original-slug':str(tmp_path/'final'/'fact.db')})
 assert result['ok']
 candidate=Path(result['entries'][0]['candidate_db'])
 report=json.loads((candidate.parent/'manifest.json').read_text())['migration_review_report']
 assert report['counts']=={'parentless_tasks':1,'unresolved_refs':0}
 assert [task['legacy_id'] for task in report['parentless_tasks']]==['2']
 assert v3.verify_candidate(candidate)['ok']
 assert verify_handoff(handoff)['ok']
 assert source.read_bytes()==before
 with sqlite3.connect(candidate) as con:
  con.execute("UPDATE items SET title='tampered' WHERE kind='task'")
 assert not v3.verify_candidate(candidate)['ok']
