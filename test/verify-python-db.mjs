// Explicit integration check: python3 and the supplied released v3.py are required.
import {mkdtempSync,readFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {execFileSync} from 'node:child_process';
import assert from 'node:assert/strict';
import {executeTool} from '../dist/src/service.js';
import {parseConfig} from '../dist/src/schemas.js';
import {openStore,closeStore,allRows} from '../dist/src/store.js';
import {relevantDigest} from '../dist/src/completion.js';
const root=mkdtempSync(join(tmpdir(),'fact-python-v3-'));
try {
 const db=join(root,'fact.db');
 const python=String.raw`
import ast,sys,json,sqlite3,hashlib,uuid
from datetime import datetime,timezone
from typing import Any
from pathlib import Path
source=Path(sys.argv[1])
for node in ast.parse(source.read_text()).body:
 if isinstance(node,ast.FunctionDef) and node.name in {'now','_json','_hash','ok','_strict','_plan_snapshot','_relevant','_prepare'}:
  exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'))
con=sqlite3.connect(sys.argv[2]); con.row_factory=sqlite3.Row
con.executescript(Path('test/fixtures/schema.sql').read_text())
con.execute('PRAGMA user_version=3')
con.execute("INSERT INTO work VALUES('w','s','名字','purpose','light','active',10,0,'t','t')")
for id in ['g','h']:
 con.execute("INSERT INTO items(id,kind,title,position,created_at,updated_at) VALUES(?,'goal','目标',0,'t','t')",(id,))
changes=[{'op':'update','item_id':'h','title':'新目标'}]
con.execute("INSERT INTO proposals VALUES('python-plan','plan',?,?,NULL,'pending',NULL,NULL,'t',NULL)",(_json(changes),_json(_plan_snapshot(con,changes))))
prepared=_prepare(con,{'target_type':'goal','target_id':'g','request_id':'python-prepare'},None)
con.commit()
print(json.dumps({'completion_id':prepared['data']['proposal_id'],'digest':_relevant(con,'goal','g')},ensure_ascii=False))
con.close()
`;
 const evidence=JSON.parse(execFileSync('python3',['-c',python,process.argv[2],db],{encoding:'utf8'}));
 const config=parseConfig({workRoot:root,indexPath:join(root,'index.db')});
 const inspect=fn=>{const s=openStore(db,{write:false,timeoutMs:100});try{return fn(s);}finally{closeStore(s);}};
 assert.deepEqual(inspect(s=>allRows(s,'SELECT kind,status FROM proposals ORDER BY kind')),[{kind:'completion',status:'pending'},{kind:'plan',status:'pending'}]);
 assert.deepEqual(inspect(s=>relevantDigest(s,'goal','g')),evidence.digest);
 const plan=await executeTool('fact_plan',{action:'apply',work:db,request_id:'ts-apply',proposal_id:'python-plan',confirmation:{summary:'approved'}},config);
 assert.equal(plan.ok,true,JSON.stringify(plan));
 const finish=await executeTool('fact_finish',{action:'confirm',work:db,request_id:'ts-confirm',proposal_id:evidence.completion_id,confirmation:{summary:'approved'}},config);
 assert.equal(finish.ok,true,JSON.stringify(finish));
 assert.deepEqual(inspect(s=>allRows(s,'SELECT id,title,status FROM items ORDER BY id')),[{id:'g',title:'目标',status:'done'},{id:'h',title:'新目标',status:'planned'}]);
 assert.ok(inspect(s=>allRows(s,'SELECT status FROM proposals')).every(p=>p.status==='applied'));
 console.log(JSON.stringify({python:'v3 extracted original helpers; SQLite file committed by Python',pending_before:['plan','completion'],digest_equal:true,plan_ok:plan.ok,finish_ok:finish.ok,proposals_after:'both applied',items_verified:true},null,2));
} finally {rmSync(root,{recursive:true,force:true});}
