import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import assert from 'node:assert/strict';
import {executeTool} from '../src/service.js';
import {parseConfig} from '../src/schemas.js';
import {openStore,closeStore,allRows} from '../src/store.js';
export async function fixture(t:any){const root=fs.mkdtempSync(path.join(os.tmpdir(),'fact-m2-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));const config=parseConfig({workRoot:root,indexPath:path.join(root,'index.db')});let n=0;const created:any=await executeTool('fact_create',{action:'create',slug:'test',purpose:'test',kind:'light',request_id:'create'},config);assert.equal(created.ok,true);const db=created.db_path;const call=async(tool:any,args:any)=>executeTool(tool,{work:db,request_id:'r'+(++n),...args},config) as Promise<any>;const rows=(table:string)=>{const s=openStore(db,{write:false,timeoutMs:100});try{return allRows(s,'SELECT * FROM '+table);}finally{closeStore(s);}};const sql=(q:string)=>{const s=openStore(db,{write:true,timeoutMs:100});try{s.db.exec(q);}finally{closeStore(s);}};const propose=(changes:any[])=>call('fact_plan',{action:'propose',changes});const apply=(id:string)=>call('fact_plan',{action:'apply',proposal_id:id,confirmation:{summary:'approved'}});const plan=async(changes:any[])=>{const p=await propose(changes);assert.equal(p.ok,true,JSON.stringify(p));const a=await apply(p.data.proposal_id);assert.equal(a.ok,true,JSON.stringify(a));return a.data.refs;};return {root,db,config,call,rows,sql,propose,apply,plan};}
export function error(r:any,code:string){assert.equal(r.ok,false,JSON.stringify(r));assert.equal(r.error.code,code);}
