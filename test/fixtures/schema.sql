
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
