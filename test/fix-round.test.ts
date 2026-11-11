import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {fixture,error} from './m2-helper.js';
import {canonicalJson,validateJsonBounds,FactError} from '../src/schemas.js';
import {mapError,commitMutation,withWrite} from '../src/service.js';
import {openStore} from '../src/store.js';

test('B1: >5000-node internal history and receipt persist while public bounds remain',async t=>{
 const f=await fixture(t),large={rows:Array.from({length:6000},(_,i)=>({id:i}))};
 assert.throws(()=>validateJsonBounds(large),{tag:'VALIDATION_ERROR'});
 const response=withWrite(openStore(f.db,{write:true,timeoutMs:100}),tx=>commitMutation(tx,{rawArgs:{request_id:'large'},requestId:'large',response:{ok:true,data:large},operation:'regression',targetType:'work',targetId:f.rows('work')[0].id,before:large,after:large}));
 assert.equal(response.ok,true);assert.deepEqual(JSON.parse(f.rows('history').at(-1)!.after_json),large);
 assert.deepEqual(JSON.parse(f.rows('request_receipts').at(-1)!.response_json).data,large);
 assert.doesNotThrow(()=>canonicalJson({text:'x'.repeat(200001),deep:Array(6000).fill(null)}));
 const cycle:any={};cycle.self=cycle;assert.throws(()=>canonicalJson(cycle));assert.throws(()=>canonicalJson({bad:NaN}));
});

test('M1/U2: both terminal actions replay latest receipt verbatim before guards; request receipt wins',async t=>{
 const f=await fixture(t),wid=f.rows('work')[0].id;
 for(const terminal of ['confirm','continue']){
 const p=await f.call('fact_finish',{action:'prepare',target_type:'work',target_id:wid,mode:'archive'});
 const pid=p.data.proposal_id;
 const done=await f.call('fact_finish',{action:terminal,proposal_id:pid,...(terminal==='confirm'?{confirmation:{summary:'yes'}}:{})});
 assert.equal(done.ok,true);
 // A persisted rejected response need not use the Node spelling "continued".
 if(terminal==='continue')f.sql(`UPDATE request_receipts SET response_json=json_set(response_json,'$.data.status','rejected') WHERE json_extract(response_json,'$.data.proposal_id')='${pid}' AND committed_revision=${done.revision}`);
 const original=JSON.parse(f.rows('request_receipts').at(-1)!.response_json);
 f.sql("UPDATE work SET status='imported_unreviewed'");
 const before=['work','history','request_receipts','proposals'].map(f.rows);
 for(const action of ['confirm','continue'])assert.deepEqual(await f.call('fact_finish',{action,proposal_id:pid,...(action==='confirm'?{confirmation:{summary:'again'}}:{})}),original);
 assert.deepEqual(['work','history','request_receipts','proposals'].map(f.rows),before);
 const prepareReceipt=f.rows('request_receipts').find(r=>JSON.parse(r.response_json).data?.proposal_id===pid)!;
 error(await f.call('fact_finish',{action:'continue',proposal_id:pid,request_id:prepareReceipt.request_id}),'CONFLICT');
 assert.deepEqual(await f.call('fact_finish',{action:'prepare',target_type:'work',target_id:wid,mode:'archive',request_id:prepareReceipt.request_id}),p);
 f.sql("UPDATE work SET status='active'");
 }
});

test('M2/U3: finish history targets and null/response.data protocol',async t=>{
 const f=await fixture(t),refs=await f.plan([{op:'add',kind:'task',title:'task',ref:'task'}]);
 for(const [type,id] of [['work',f.rows('work')[0].id],['task',refs.task]]){
 const p=await f.call('fact_finish',{action:'prepare',target_type:type,target_id:id});assert.equal(p.ok,true);
 let h=f.rows('history').at(-1)!;assert.equal(h.target_type,type);assert.equal(h.target_id,id);assert.equal(h.before_json,null);assert.deepEqual(JSON.parse(h.after_json),p.data);
 const c=await f.call('fact_finish',{action:type==='work'?'continue':'confirm',proposal_id:p.data.proposal_id,...(type==='task'?{confirmation:{summary:'yes'}}:{})});assert.equal(c.ok,true);
 h=f.rows('history').at(-1)!;assert.equal(h.target_type,'proposal');assert.equal(h.target_id,p.data.proposal_id);assert.equal(h.before_json,null);assert.deepEqual(JSON.parse(h.after_json),c.data);
 }
});

test('U4: default details object and explicit details preserved',()=>{
 assert.deepEqual(mapError(new Error('plain'),'fact_finish').error.details,{});
 assert.deepEqual(mapError(new FactError('NOT_FOUND'),'fact_finish').error.details,{});
 for(const details of [null,{},[],{id:'x'},false])assert.deepEqual(mapError(new FactError('NOT_FOUND','message',details as any),'fact_finish').error.details,details);
});

test('gate: read-only preflight, second same-connection gate, closure and new-store exemption',async()=>{
 // Instrument only the SQLite boundary of the actual compiled store module.
 const source=readFileSync(new URL('../src/store.js',import.meta.url),'utf8')
 .replace(/import \{ DatabaseSync \} from 'node:sqlite';/, 'const DatabaseSync=globalThis.__gateDB;')
 .replace("'./schemas.js'",JSON.stringify(new URL('../src/schemas.js',import.meta.url).href));
 const events:any[]=[];let versions:number[]=[];
 class FakeDB{v:number;closed=false;constructor(_p:string,options:any){this.v=versions.shift()??0;events.push(['open',options.readOnly]);}prepare(){return {get:()=>({user_version:this.v})};}exec(sql:string){events.push(['exec',sql]);}close(){assert.equal(this.closed,false);this.closed=true;events.push(['close']);}}
 (globalThis as any).__gateDB=FakeDB;
 try{
 const store=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
 for(const v of [0,2,4]){events.length=0;versions=[v];assert.throws(()=>store.openStore(':memory:',{write:true,timeoutMs:100}),{tag:v<3?'MIGRATION_REQUIRED':'UNSUPPORTED_SCHEMA'});assert.deepEqual(events,[['open',true],['close']]);}
 events.length=0;versions=[3,4];assert.throws(()=>store.openStore(':memory:',{write:true,timeoutMs:100}),{tag:'UNSUPPORTED_SCHEMA'});assert.deepEqual(events.filter(e=>e[0]!=='exec'),[['open',true],['close'],['open',false],['close']]);assert.ok(!events.some(e=>e[1]==='PRAGMA journal_mode=WAL'));
 events.length=0;versions=[3,3];store.closeStore(store.openStore(':memory:',{write:true,timeoutMs:100}));assert.deepEqual(events.filter(e=>e[0]==='open'),[['open',true],['open',false]]);assert.equal(events.filter(e=>e[0]==='close').length,2);assert.ok(events.some(e=>e[1]==='PRAGMA journal_mode=WAL'));
 events.length=0;versions=[0];store.closeStore(store.openNewStore(':memory:',100));assert.deepEqual(events.filter(e=>e[0]==='open'),[['open',false]]);
 }finally{delete (globalThis as any).__gateDB;}
});
