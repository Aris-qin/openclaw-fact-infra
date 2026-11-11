import {randomUUID} from 'node:crypto';
import {FactError,canonicalJson,type Success} from './schemas.js';
import {getRow,allRows,run,type Tx} from './store.js';
import {payloadHash,readReceipt,findTerminalReceipt,commitMutation} from './service.js';
export function relevantData(tx:Tx,type:string,id:string){
 const ids=allRows(tx,type==='work'?'SELECT id FROM items WHERE removed_at IS NULL ORDER BY id':'WITH RECURSIVE tree(id) AS (SELECT ? UNION ALL SELECT i.id FROM items i JOIN tree t ON i.parent_id=t.id WHERE i.removed_at IS NULL) SELECT id FROM tree ORDER BY id',type==='work'?[]:[id]).map(r=>r.id);
 const marks=(xs:any[])=>xs.length?xs.map(()=>'?').join(','):'NULL';
 const records=type==='work'?allRows(tx,'SELECT * FROM records ORDER BY id'):allRows(tx,`SELECT DISTINCT r.* FROM records r JOIN record_links l ON l.record_id=r.id WHERE l.item_id IN (${marks(ids)}) ORDER BY r.id`,ids);
 const rs=records.map(r=>r.id);
 return {work:getRow(tx,'SELECT id,status,kind FROM work')!,items:allRows(tx,`SELECT * FROM items WHERE id IN (${marks(ids)}) ORDER BY id`,ids),dependencies:allRows(tx,`SELECT * FROM dependencies WHERE item_id IN (${marks(ids)}) OR requires_id IN (${marks(ids)}) ORDER BY item_id,requires_id`,[...ids,...ids]),records,links:allRows(tx,`SELECT * FROM record_links WHERE record_id IN (${marks(rs)}) ORDER BY record_id,item_id,relation`,rs),relations:allRows(tx,`SELECT * FROM record_relations WHERE source_id IN (${marks(rs)}) OR target_id IN (${marks(rs)}) ORDER BY source_id,target_id,relation`,[...rs,...rs]),files:allRows(tx,`SELECT * FROM file_refs WHERE record_id IN (${marks(rs)}) OR item_id IN (${marks(ids)}) ORDER BY id`,[...rs,...ids])};
}
export function relevantDigest(tx:Tx,type:string,id:string){return {digest:payloadHash(relevantData(tx,type,id))};}
export function handleFinish(tx:Tx,a:any):Success{
 const prior=readReceipt(tx,a.request_id,a);if(prior)return prior;
 if(['confirm','continue'].includes(a.action)&&a.proposal_id){
 const p=getRow(tx,'SELECT status FROM proposals WHERE id=?',[a.proposal_id]);
 if(p&&['applied','rejected'].includes(p.status)){const receipt=findTerminalReceipt(tx,a.proposal_id);if(receipt)return receipt;}
 }
 if(getRow(tx,'SELECT status FROM work')!.status==='imported_unreviewed')throw new FactError('ADOPTION_REQUIRED');
 const t=new Date().toISOString();let response:Success,pid:string;
 if(a.action==='prepare'){
 const w=getRow(tx,'SELECT * FROM work')!,mode=a.mode??'complete';
 if(a.target_type==='work'&&a.target_id!==w.id)throw new FactError('VALIDATION_ERROR','target work mismatch');
 if(mode!=='complete'&&a.target_type!=='work')throw new FactError('VALIDATION_ERROR','archive/restore are work-only');
 if(w.status==='archived'&&mode!=='restore')throw new FactError('WORK_ARCHIVED');
 if(mode==='restore'&&w.status!=='archived')throw new FactError('INVALID_TRANSITION');
 let status=w.status;if(a.target_type!=='work'){const i=getRow(tx,'SELECT * FROM items WHERE id=? AND kind=?',[a.target_id,a.target_type]);if(!i)throw new FactError('NOT_FOUND');status=i.status;run(tx,"UPDATE items SET status='awaiting_confirmation',version=version+1,updated_at=? WHERE id=?",[t,i.id]);}
 const base=relevantDigest(tx,a.target_type,a.target_id);pid=randomUUID();const payload={mode,target_type:a.target_type,target_id:a.target_id,prior_status:status,open_items:allRows(tx,"SELECT id,kind,title,status FROM items WHERE status NOT IN('done','cancelled') AND removed_at IS NULL"),open_issues:allRows(tx,"SELECT id,title,status FROM records WHERE kind='issue' AND status='open'")};
 run(tx,"INSERT INTO proposals VALUES(?,?,?,?,?,'pending',NULL,NULL,?,NULL)",[pid,'completion',canonicalJson(payload),canonicalJson(base),'completion summary',t]);response={ok:true,data:{proposal_id:pid,summary:payload,needs_user_confirmation:true},follow_up:['Ask whether to finish now or add method, result, or notes.']};
 }else{
 const p=getRow(tx,"SELECT * FROM proposals WHERE id=? AND kind='completion'",[a.proposal_id]);if(!p)throw new FactError('NOT_FOUND');pid=p.id;const v=JSON.parse(p.payload_json);
 if(a.action==='confirm'){
 if(!a.confirmation?.summary)throw new FactError('CONFIRMATION_REQUIRED');if(p.status!=='pending'||canonicalJson(relevantDigest(tx,v.target_type,v.target_id))!==p.base_versions_json)throw new FactError('STALE_PROPOSAL');
 if(v.target_type==='work')run(tx,'UPDATE work SET status=?,updated_at=?',[({complete:'completed',archive:'archived',restore:'active'} as any)[v.mode],t]);else run(tx,"UPDATE items SET status='done',completed_at=?,version=version+1,updated_at=? WHERE id=?",[t,t,v.target_id]);
 run(tx,"UPDATE proposals SET status='applied',confirmation_json=?,applied_at=? WHERE id=?",[canonicalJson(a.confirmation),t,pid]);response={ok:true,data:{proposal_id:pid,status:'applied',mode:v.mode}};
 }else{
 if(['rejected','applied'].includes(p.status))throw new FactError('STALE_PROPOSAL');
 if(v.target_type!=='work')run(tx,"UPDATE items SET status=?,version=version+1,updated_at=? WHERE id=? AND status='awaiting_confirmation'",[v.prior_status??'planned',t,v.target_id]);run(tx,"UPDATE proposals SET status='rejected',applied_at=? WHERE id=?",[t,pid]);response={ok:true,data:{proposal_id:pid,status:'continued'}};
 }
 }
 return commitMutation(tx,{rawArgs:a,requestId:a.request_id,response,operation:'finish_'+a.action,targetType:a.target_type??'proposal',targetId:a.target_id??a.proposal_id,proposalId:pid,before:null,after:response.data});
}
