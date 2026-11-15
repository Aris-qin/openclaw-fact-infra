import json, sqlite3, shutil, sys
from pathlib import Path
import pytest
REPO=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(REPO))
from plugin import v3
from scripts.central_handoff import prepare_handoff, register_handoff, verify_handoff
from plugin.tests.test_central_handoff import legacy_work, old_index

@pytest.fixture
def data(tmp_path,monkeypatch):
 a,b=tmp_path/'a.db',tmp_path/'b.db'
 legacy_work(a,'internal-one',parentless=True,unresolved=True);legacy_work(b,'internal-two')
 old=tmp_path/'old.db';old_index(old,a,b)
 finals={'legacy-one':str(tmp_path/'published-a/fact.db'),'legacy-two':str(tmp_path/'published-b/fact.db')}
 result=prepare_handoff(old,tmp_path/'handoff',finals)
 monkeypatch.setenv('FACT_INDEX_DB',str(tmp_path/'new-index.db'));monkeypatch.setenv('FACT_WORKSPACE_ROOT',str(tmp_path/'workspace'))
 return tmp_path,a,b,old,finals,result

def publish(d):
 for e in d[-1]['entries']:
  p=Path(e['final_db']);p.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(e['candidate_db'],p)

def call(name,args):return json.loads(v3.HANDLERS[name](args,session_id='parent-test'))

def test_prepare_retry_rejects_stale_source(data):
 root,a,b,old,finals,result=data
 with sqlite3.connect(a) as c:c.execute("update tasks set title='new task content'")
 with pytest.raises(Exception):prepare_handoff(old,root/'handoff',finals)

def test_register_rejects_stale_source(data):
 root,a,b,old,finals,result=data;publish(data)
 with sqlite3.connect(a) as c:c.execute("update tasks set title='new task content'")
 r=register_handoff(root/'handoff',root/'new-index.db','reg');assert not r['ok'],r

def test_prepare_rejects_same_final_path(data):
 root,a,b,old,finals,result=data
 with pytest.raises(Exception):prepare_handoff(old,root/'other-handoff',dict.fromkeys(finals,str(root/'same/fact.db')))

def test_prepare_retry_checks_corrupted_result(data):
 root,*_=data
 (root/'handoff/prepare-result.json').write_text('{"ok":true,"entries":[]}')
 with pytest.raises(Exception):prepare_handoff(data[3],root/'handoff',data[4])

def test_imported_parentless_task_can_assign_after_confirmed_adoption(data):
 root,a,b,old,finals,result=data;path=result['entries'][0]['candidate_db']
 p=call('fact_plan',{'action':'propose','work':path,'request_id':'p','changes':[{'op':'adopt','acknowledge_migration_review':True}]});assert p['ok'],p
 r=call('fact_plan',{'action':'apply','work':path,'request_id':'a','proposal_id':p['data']['proposal_id'],'confirmation':{'summary':'test simulation'}});assert r['ok'],r
 with sqlite3.connect(path) as c:
  goal=c.execute("select id from items where kind='goal'").fetchone()[0];task=c.execute("select id from items where kind='task'").fetchone()[0]
 p=call('fact_plan',{'action':'propose','work':path,'request_id':'assign','changes':[{'op':'update','item_id':task,'parent_id':goal}]});assert p['ok'],p
 r=call('fact_plan',{'action':'apply','work':path,'request_id':'assign-a','proposal_id':p['data']['proposal_id'],'confirmation':{'summary':'test simulation'}});assert r['ok'],r
 with sqlite3.connect(path) as c:assert c.execute('select parent_id from items where id=?',(task,)).fetchone()[0]==goal

def test_new_create_cannot_shadow_legacy_alias(data):
 root,a,b,old,finals,result=data;publish(data)
 assert register_handoff(root/'handoff',root/'new-index.db','reg')['ok']
 r=call('fact_create',{'action':'create','slug':'legacy-one','kind':'light','purpose':'not allowed alias shadow','request_id':'new'})
 assert not r['ok'],r
 assert not (root/'workspace/work/legacy-one/fact.db').exists()

def test_independent_verify_rejects_forged_registry_name_and_meta(data):
 root,a,b,old,finals,result=data;path=Path(result['entries'][0]['candidate_db'])
 with sqlite3.connect(path) as c:
  c.execute("update work set name='FORGED'")
  snap=json.loads(c.execute("select value from legacy_meta where key='registry_snapshot'").fetchone()[0]);snap['raw']['name']='FORGED'
  c.execute("update legacy_meta set value=? where key='registry_snapshot'",(json.dumps(snap),))
 with sqlite3.connect(path) as c:logical=v3._logical_manifest(c)
 m=json.loads((path.parent/'manifest.json').read_text());m['registry_snapshot']=snap;m['logical']=logical
 raw=(json.dumps(m,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode();(path.parent/'manifest.json').write_bytes(raw)
 import hashlib
 (path.parent/'manifest.sha256').write_text(hashlib.sha256(raw).hexdigest()+'\n')
 r=v3.verify_candidate(path);assert not r['ok'],r
