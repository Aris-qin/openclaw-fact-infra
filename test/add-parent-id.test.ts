import test from 'node:test';
import assert from 'node:assert/strict';
import {fixture,error} from './m2-helper.js';

const tables=['items','work','history','proposals','request_receipts'];
for(const kind of ['project','light']) {
  for(const parent of ['task','removed goal','missing']) test(`${kind}: add rejects explicit ${parent} parent without writes`,async t=>{
    const f=await fixture(t);
    const r=await f.plan([{op:'add',kind:'goal',title:'G',ref:'g'},{op:'add',kind:'task',title:'T',parent_ref:'g',ref:'t'}]);
    if(parent==='removed goal')await f.plan([{op:'remove',item_id:r.g}]);
    if(kind==='project')await f.plan([{op:'work_update',kind:'project'}]);
    const before=tables.map(f.rows);
    error(await f.propose([{op:'add',kind:'task',title:'Invalid',parent_id:parent==='task'?r.t:parent==='missing'?'missing-id':r.g}]),'VALIDATION_ERROR');
    assert.deepEqual(tables.map(f.rows),before);
  });
  test(`${kind}: live goal parent is persisted`,async t=>{
    const f=await fixture(t);
    const r=await f.plan([{op:'add',kind:'goal',title:'G',ref:'g'}]);
    if(kind==='project')await f.plan([{op:'work_update',kind:'project'}]);
    const added=await f.plan([{op:'add',kind:'task',title:'T',parent_id:r.g,ref:'t'}]);
    assert.equal(f.rows('items').find(i=>i.id===added.t)!.parent_id,r.g);
  });
}
test('project missing parent rejected without writes; light root remains available',async t=>{
  const f=await fixture(t),r=await f.plan([{op:'add',kind:'task',title:'Root',ref:'t'}]);
  assert.equal(f.rows('items').find(i=>i.id===r.t)!.parent_id,null);
  await f.plan([{op:'work_update',kind:'project'}]);
  const before=tables.map(f.rows);
  error(await f.propose([{op:'add',kind:'task',title:'Missing parent'}]),'VALIDATION_ERROR');
  assert.deepEqual(tables.map(f.rows),before);
});
test('project forward parent_ref remains valid',async t=>{
  const f=await fixture(t);await f.plan([{op:'work_update',kind:'project'}]);
  const r=await f.plan([{op:'add',kind:'task',title:'T',parent_ref:'g',ref:'t'},{op:'add',kind:'goal',title:'G',ref:'g'}]);
  assert.equal(f.rows('items').find(i=>i.id===r.t)!.parent_id,r.g);
});
test('apply rejects parent removed after propose as stale without writes',async t=>{
  const f=await fixture(t),r=await f.plan([{op:'add',kind:'goal',title:'G',ref:'g'}]);
  const p=await f.propose([{op:'add',kind:'task',title:'T',parent_id:r.g}]);assert.equal(p.ok,true);
  await f.plan([{op:'remove',item_id:r.g}]);
  const before=tables.map(f.rows);
  error(await f.apply(p.data.proposal_id),'STALE_PROPOSAL');
  assert.deepEqual(tables.map(f.rows),before);
});
test('apply revalidates explicit parent kind even when snapshot versions match',async t=>{
  const f=await fixture(t),r=await f.plan([{op:'add',kind:'goal',title:'G',ref:'g'}]);
  const p=await f.propose([{op:'add',kind:'task',title:'T',parent_id:r.g}]);assert.equal(p.ok,true);
  // Deliberately corrupt only this temporary SQLite fixture, preserving snapshot fields.
  f.sql(`UPDATE items SET kind='task' WHERE id='${r.g}'`);
  const before=tables.map(f.rows);
  error(await f.apply(p.data.proposal_id),'VALIDATION_ERROR');
  assert.deepEqual(tables.map(f.rows),before);
});
