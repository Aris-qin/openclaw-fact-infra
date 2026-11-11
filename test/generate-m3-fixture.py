"""Extract only pure helpers and completion preparation; use an in-memory database."""
import ast
import sys
import json
import sqlite3
import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any
from pathlib import Path
source = ast.parse(Path(sys.argv[1]).read_text())
wanted = {'now', '_json', '_hash', 'ok', '_strict', '_relevant', '_prepare', '_plan_snapshot'}
for node in source.body:
    if isinstance(node, ast.FunctionDef) and node.name in wanted:
        exec(compile(ast.Module(body=[node], type_ignores=[]), sys.argv[1], 'exec'))
con = sqlite3.connect(':memory:')
con.row_factory = sqlite3.Row
con.executescript(Path('test/fixtures/schema.sql').read_text())
con.execute("INSERT INTO work VALUES('w','s','名字','purpose','light','active',10,0,'t','t')")
con.execute("INSERT INTO items(id,kind,title,position,created_at,updated_at) VALUES('g','goal','目标',0,'t','t')")
changes = [{'op':'update','item_id':'g','title':'新目标'}]
base = _plan_snapshot(con, changes)
con.execute("INSERT INTO proposals VALUES('python-plan','plan',?,?,NULL,'pending',NULL,NULL,'t',NULL)", (_json(changes),_json(base)))
plan_ready = {table:[dict(r) for r in con.execute('SELECT * FROM '+table)] for table in ['work','items','proposals']}
prepared = _prepare(con, {'target_type':'goal','target_id':'g','request_id':'python-prepare'}, None)
data = {table:[dict(r) for r in con.execute('SELECT * FROM '+table)] for table in ['work','items','proposals']}
data['plan_ready'] = plan_ready
data['completion_id'] = prepared['data']['proposal_id']
data['relevant'] = _relevant(con,'goal','g')
Path('test/fixtures/m3-python.json').write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')
