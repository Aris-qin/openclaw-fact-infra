import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,readFileSync,chmodSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {DatabaseSync} from 'node:sqlite';
import {openStore} from '../src/store.js';
test('legacy read and write gates preserve database bytes before WAL setup',()=>{
 const p=join(mkdtempSync(join(tmpdir(),'fact-legacy-gate-')),'v2.db');
 const db=new DatabaseSync(p);db.exec('PRAGMA user_version=2; CREATE TABLE projects(id TEXT)');db.close();
 chmodSync(p,0o444);const before=readFileSync(p);
 for(const write of [false,true]){assert.throws(()=>openStore(p,{write,timeoutMs:100}),{tag:'MIGRATION_REQUIRED'});assert.deepEqual(readFileSync(p),before);}
});
