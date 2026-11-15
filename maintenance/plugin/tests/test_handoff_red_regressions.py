import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from plugin import v3
from plugin.tests.test_central_handoff import legacy_work, old_index
from scripts.central_handoff import prepare_handoff, register_handoff, verify_handoff


@pytest.fixture
def handoff(tmp_path, monkeypatch):
    sources=[tmp_path/"a.db",tmp_path/"b.db"]
    legacy_work(sources[0],"internal-one",parentless=True,unresolved=True)
    legacy_work(sources[1],"internal-two")
    with sqlite3.connect(sources[0]) as con:
        con.execute("INSERT INTO meta VALUES('nullable',NULL)")
    old=tmp_path/"old.db"; old_index(old,*sources)
    finals={"legacy-one":str(tmp_path/"published-a"/"fact.db"),"legacy-two":str(tmp_path/"published-b"/"fact.db")}
    root=tmp_path/"handoff"; prepared=prepare_handoff(old,root,finals)
    index=tmp_path/"new-index.db"
    monkeypatch.setenv("FACT_INDEX_DB",str(index))
    monkeypatch.setenv("FACT_WORKSPACE_ROOT",str(tmp_path/"workspace"))
    return {"root":root,"old":old,"sources":sources,"finals":finals,"prepared":prepared,"index":index,"tmp":tmp_path}


def publish(handoff):
    for entry in handoff["prepared"]["entries"]:
        target=Path(entry["final_db"]); target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(entry["candidate_db"],target)


def call(name,args):
    return json.loads(v3.HANDLERS[name](args,session_id="isolated-regression"))


def regenerate(candidate):
    manifest_path=candidate.parent/"manifest.json"
    manifest=json.loads(manifest_path.read_text())
    with sqlite3.connect(candidate) as con:
        manifest["logical"]=v3._logical_manifest(con)
        for key in ("registry_snapshot","migration_review_report"):
            row=con.execute("SELECT value FROM legacy_meta WHERE key=?",(key,)).fetchone()
            if row: manifest[key]=json.loads(row[0])
    raw=(json.dumps(manifest,ensure_ascii=False,sort_keys=True,indent=2)+"\n").encode()
    manifest_path.write_bytes(raw)
    (candidate.parent/"manifest.sha256").write_text(hashlib.sha256(raw).hexdigest()+"\n")


@pytest.mark.parametrize("source",["work","central"])
@pytest.mark.parametrize("wal",[False,True])
def test_changed_sources_reject_prepare_retry_and_registration(handoff,source,wal):
    publish(handoff)
    path=handoff["sources"][0] if source=="work" else handoff["old"]
    con=sqlite3.connect(path)
    if wal:
        con.execute("PRAGMA journal_mode=WAL"); con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute("UPDATE tasks SET title='changed'" if source=="work" else "UPDATE project_registry SET name='changed'")
    con.commit()
    try:
        with pytest.raises(ValueError,match="changed"):
            prepare_handoff(handoff["old"],handoff["root"],handoff["finals"])
        assert not register_handoff(handoff["root"],handoff["index"],"stale")["ok"]
    finally: con.close()


def test_checkpoint_and_readonly_clients_do_not_change_snapshot(handoff):
    path=handoff["sources"][0]
    with sqlite3.connect(path) as con:
        con.execute("PRAGMA journal_mode=WAL")
        with sqlite3.connect(f"file:{path}?mode=ro",uri=True) as reader:
            reader.execute("SELECT * FROM tasks").fetchall()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert prepare_handoff(handoff["old"],handoff["root"],handoff["finals"])==handoff["prepared"]


def test_prepare_snapshots_committed_source_and_central_wal(handoff):
    source=sqlite3.connect(handoff["sources"][0]); central=sqlite3.connect(handoff["old"])
    for con in (source,central):
        con.execute("PRAGMA journal_mode=WAL"); con.execute("PRAGMA wal_autocheckpoint=0")
    source.execute("UPDATE tasks SET title='already committed in WAL'"); source.commit()
    central.execute("UPDATE project_registry SET name='中央 WAL 中文' WHERE slug='legacy-one'"); central.commit()
    output=handoff["tmp"]/"wal-handoff"
    try:
        result=prepare_handoff(handoff["old"],output,handoff["finals"])
        with sqlite3.connect(result["entries"][0]["candidate_db"]) as candidate:
            assert candidate.execute("SELECT name FROM work").fetchone()[0]=="中央 WAL 中文"
            assert candidate.execute("SELECT title FROM items WHERE kind='task'").fetchone()[0]=="already committed in WAL"
        for con in (source,central): con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert prepare_handoff(handoff["old"],output,handoff["finals"])==result
    finally: source.close(); central.close()


@pytest.mark.parametrize("destination",["duplicate","source","source-wal","index","artifact","ancestor"])
def test_destination_conflicts_preflight(handoff,destination):
    finals=dict(handoff["finals"])
    output=handoff["tmp"]/"other-handoff"
    invalid={"duplicate":finals["legacy-two"],"source":str(handoff["sources"][0]),
             "source-wal":str(handoff["sources"][0])+"-wal","index":str(handoff["old"]),
             "artifact":str(output/"candidates"/"fact.db"),"ancestor":str(handoff["tmp"])}
    finals["legacy-one"]=invalid[destination]
    with pytest.raises(ValueError): prepare_handoff(handoff["old"],output,finals)
    assert not output.exists()


def test_registry_slug_cannot_escape_staging(handoff):
    with sqlite3.connect(handoff["old"]) as con:
        con.execute("UPDATE project_registry SET slug='../escape' WHERE slug='legacy-one'")
    finals={"../escape":handoff["finals"]["legacy-one"],"legacy-two":handoff["finals"]["legacy-two"]}
    with pytest.raises(ValueError,match="unsafe"):
        prepare_handoff(handoff["old"],handoff["tmp"]/"other",finals)
    assert not (handoff["tmp"]/"escape").exists()


def test_prepare_retry_rejects_response_and_candidate_tampering(handoff):
    response=handoff["root"]/"prepare-result.json"
    response.write_text('{"ok":true,"entries":[]}')
    with pytest.raises(ValueError,match="result"):
        prepare_handoff(handoff["old"],handoff["root"],handoff["finals"])
    response.write_text(json.dumps(handoff["prepared"]))
    candidate=handoff["prepared"]["entries"][0]["candidate_db"]
    with sqlite3.connect(candidate) as con: con.execute("UPDATE work SET name='forged'")
    with pytest.raises(ValueError): prepare_handoff(handoff["old"],handoff["root"],handoff["finals"])


@pytest.mark.parametrize("corruption",["registry-name","registry-raw","missing-null","extra-meta","review-count","review-row","review-unresolved","work-name"])
def test_independent_backup_verification_rejects_regenerated_manifests(handoff,corruption):
    candidate=Path(handoff["prepared"]["entries"][0]["candidate_db"])
    with sqlite3.connect(candidate) as con:
        if corruption=="missing-null": con.execute("DELETE FROM legacy_meta WHERE key='nullable'")
        elif corruption=="extra-meta": con.execute("INSERT INTO legacy_meta VALUES('forged','value')")
        elif corruption=="work-name": con.execute("UPDATE work SET name='forged'")
        else:
            key="registry_snapshot" if corruption.startswith("registry") else "migration_review_report"
            value=json.loads(con.execute("SELECT value FROM legacy_meta WHERE key=?",(key,)).fetchone()[0])
            if corruption=="registry-name":
                value["raw"]["name"]="FORGED"; con.execute("UPDATE work SET name='FORGED'")
            elif corruption=="registry-raw": value["raw"]["status"]="forged"
            elif corruption=="review-count": value["counts"]["parentless_tasks"]=0
            elif corruption=="review-row": value["parentless_tasks"][0]["raw"]["title"]="forged"
            else: value["unresolved_refs"]=[]
            con.execute("UPDATE legacy_meta SET value=? WHERE key=?",(json.dumps(value),key))
    regenerate(candidate)
    assert not v3.verify_candidate(candidate)["ok"]
    publish(handoff)
    assert not register_handoff(handoff["root"],handoff["index"],"forged")["ok"]


def test_final_verification_reads_actual_committed_wal(handoff):
    publish(handoff)
    final=handoff["prepared"]["entries"][0]["final_db"]
    con=sqlite3.connect(final); con.execute("PRAGMA journal_mode=WAL"); con.execute("PRAGMA wal_autocheckpoint=0")
    before=Path(final).read_bytes()
    con.execute("UPDATE items SET title='committed WAL corruption'"); con.commit()
    try:
        assert Path(final).read_bytes()==before
        assert not verify_handoff(handoff["root"],final=True)["ok"]
        assert not register_handoff(handoff["root"],handoff["index"],"wal")["ok"]
    finally: con.close()


def test_registration_independently_verifies_candidate_wal(handoff):
    publish(handoff)
    candidate=Path(handoff["prepared"]["entries"][0]["candidate_db"])
    con=sqlite3.connect(candidate); con.execute("PRAGMA journal_mode=WAL"); con.execute("PRAGMA wal_autocheckpoint=0")
    before=candidate.read_bytes()
    con.execute("UPDATE items SET title='candidate WAL corruption'"); con.commit()
    try:
        assert candidate.read_bytes()==before
        assert not register_handoff(handoff["root"],handoff["index"],"candidate-wal")["ok"]
    finally: con.close()


def test_new_registration_request_cannot_reapply_existing_identity(handoff):
    publish(handoff)
    assert register_handoff(handoff["root"],handoff["index"],"first")["ok"]
    with sqlite3.connect(handoff["index"]) as con: before=list(con.iterdump())
    result=register_handoff(handoff["root"],handoff["index"],"second")
    assert not result["ok"] and result["error"]["code"]=="IDENTITY_CONFLICT"
    with sqlite3.connect(handoff["index"]) as con: assert list(con.iterdump())==before


@pytest.mark.parametrize("corruption",["version","trigger","extra-table"])
def test_final_verification_validates_current_schema_in_wal(handoff,corruption):
    publish(handoff)
    final=handoff["prepared"]["entries"][0]["final_db"]
    con=sqlite3.connect(final); con.execute("PRAGMA journal_mode=WAL"); con.execute("PRAGMA wal_autocheckpoint=0")
    before=Path(final).read_bytes()
    statements={"version":"PRAGMA user_version=99","trigger":"DROP TRIGGER history_no_update","extra-table":"CREATE TABLE forged(value TEXT)"}
    con.execute(statements[corruption]); con.commit()
    try:
        assert Path(final).read_bytes()==before
        assert not verify_handoff(handoff["root"],final=True)["ok"]
        assert not register_handoff(handoff["root"],handoff["index"],"schema")["ok"]
    finally: con.close()


def test_same_request_race_returns_same_immutable_receipt(handoff):
    publish(handoff)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda unused:register_handoff(handoff["root"],handoff["index"],"race"),range(2)))
    assert responses[0]["ok"] and responses[0]==responses[1]
    with sqlite3.connect(handoff["index"]) as con:
        assert con.execute("SELECT COUNT(*) FROM handoff_receipts").fetchone()[0]==1
    with sqlite3.connect(handoff["sources"][0]) as con: con.execute("UPDATE tasks SET title='new source'")
    with sqlite3.connect(handoff["prepared"]["entries"][0]["final_db"]) as con:
        con.execute("UPDATE work SET revision=revision+1")
    assert register_handoff(handoff["root"],handoff["index"],"race")==responses[0]
    assert not register_handoff(handoff["root"],handoff["index"],"new-request")["ok"]


def test_alias_identity_conflicts_and_refresh_preserve_aliases(handoff):
    publish(handoff)
    assert register_handoff(handoff["root"],handoff["index"],"register")["ok"]
    entry=handoff["prepared"]["entries"][0]
    for slug in ("legacy-one","internal-one",entry["work_id"]):
        result=call("fact_create",{"action":"create","slug":slug,"kind":"light","purpose":"no shadow","request_id":slug})
        assert not result["ok"]
        assert not (handoff["tmp"]/"workspace"/"work"/slug/"fact.db").exists()
    assert v3._update_catalog(entry["final_db"]) is None
    catalog=json.loads((handoff["index"].parent/"catalog.json").read_text())
    assert {"alias":"legacy-one","work_id":entry["work_id"]} in catalog["aliases"]
    copied=handoff["tmp"]/"copied.db"; shutil.copy2(entry["final_db"],copied)
    assert v3._update_catalog(copied)=="IDENTITY_CONFLICT"
    with sqlite3.connect(handoff["index"]) as con:
        assert con.execute("SELECT db_path FROM work_registry WHERE work_id=?",(entry["work_id"],)).fetchone()[0]==entry["final_db"]


@pytest.mark.parametrize("conflict",["id","slug","alias","same-id-new-path"])
def test_registration_conflicts_roll_back_existing_registry(handoff,conflict):
    publish(handoff)
    entry=handoff["prepared"]["entries"][0]
    from scripts.central_handoff import _registry_schema
    with sqlite3.connect(handoff["index"]) as con:
        _registry_schema(con)
        identity=entry["work_id"] if conflict=="same-id-new-path" else "other-id"
        slug=entry["work_id"] if conflict=="id" else entry["canonical_slug"] if conflict=="slug" else "other-slug"
        con.execute("INSERT INTO work_registry(work_id,slug,db_path) VALUES(?,?,?)",(identity,slug,str(handoff["tmp"]/"elsewhere.db")))
        if conflict=="alias": con.execute("INSERT INTO work_aliases VALUES(?,?)",(entry["work_id"],identity))
        before=list(con.iterdump())
    assert not register_handoff(handoff["root"],handoff["index"],"conflict")["ok"]
    with sqlite3.connect(handoff["index"]) as con: assert list(con.iterdump())==before


def test_registration_checks_conflict_committed_while_waiting_for_lock(handoff,monkeypatch):
    from scripts.central_handoff import _registry_schema
    publish(handoff)
    lock=sqlite3.connect(handoff["index"]); _registry_schema(lock); lock.commit(); lock.execute("BEGIN IMMEDIATE")
    connected=Event(); original_connect=sqlite3.connect
    def connect(database,*args,**kwargs):
        con=original_connect(database,*args,**kwargs)
        if str(database)==str(handoff["index"]): connected.set()
        return con
    monkeypatch.setattr(sqlite3,"connect",connect)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(register_handoff,handoff["root"],handoff["index"],"waited")
        assert connected.wait(5)
        lock.execute("INSERT INTO work_registry(work_id,slug,db_path) VALUES('other','other','/isolated/other.db')")
        lock.execute("INSERT INTO work_aliases VALUES(?, 'other')",(handoff["prepared"]["entries"][0]["work_id"],))
        lock.commit()
        result=pending.result(timeout=10)
    assert not result["ok"] and result["error"]["code"]=="ALIAS_CONFLICT"
    assert lock.execute("SELECT work_id FROM work_registry").fetchall()==[("other",)]
    assert lock.execute("SELECT COUNT(*) FROM handoff_receipts").fetchone()[0]==0
    lock.close()


def test_cli_failure_exits_nonzero(handoff):
    completed=subprocess.run([sys.executable,"scripts/fact_maintenance.py","handoff-verify","--handoff",str(handoff["tmp"]/"missing")],capture_output=True,text=True)
    assert completed.returncode!=0 and not json.loads(completed.stdout)["ok"]


def adopt(handoff):
    path=handoff["prepared"]["entries"][0]["candidate_db"]
    proposed=call("fact_plan",{"action":"propose","work":path,"request_id":"adopt-p","changes":[{"op":"adopt","acknowledge_migration_review":True}]})
    assert apply(path,proposed,"adopt-a")["ok"]
    return path


def apply(path,proposed,request_id):
    assert proposed["ok"],proposed
    return call("fact_plan",{"action":"apply","work":path,"request_id":request_id,"proposal_id":proposed["data"]["proposal_id"],"confirmation":{"summary":"test simulation only"}})


def propose(path,changes,request_id):
    return call("fact_plan",{"action":"propose","work":path,"changes":changes,"request_id":request_id})


def test_parentless_task_assignment_reparent_versions_history_and_receipt(handoff):
    path=adopt(handoff)
    with sqlite3.connect(path) as con:
        task=con.execute("SELECT id FROM items WHERE kind='task'").fetchone()[0]
        goal=con.execute("SELECT id FROM items WHERE kind='goal'").fetchone()[0]
    assignment=propose(path,[{"op":"update","item_id":task,"parent_id":goal}],"assign-p")
    first=apply(path,assignment,"assign-a"); assert first["ok"]
    assert apply(path,assignment,"assign-a")==first
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT parent_id,version FROM items WHERE id=?",(task,)).fetchone()==(goal,2)
        assert con.execute("SELECT children_version FROM items WHERE id=?",(goal,)).fetchone()[0]==1
        assert con.execute("SELECT COUNT(*) FROM history WHERE operation='plan_apply'").fetchone()[0]==2
    assert apply(path,propose(path,[{"op":"add","kind":"goal","title":"new goal"}],"new-p"),"new-a")["ok"]
    with sqlite3.connect(path) as con: new_goal=con.execute("SELECT id FROM items WHERE title='new goal'").fetchone()[0]
    assert apply(path,propose(path,[{"op":"update","item_id":task,"parent_id":new_goal}],"move-p"),"move-a")["ok"]
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT children_version FROM items WHERE id=?",(goal,)).fetchone()[0]==2
        assert con.execute("SELECT children_version FROM items WHERE id=?",(new_goal,)).fetchone()[0]==1


@pytest.mark.parametrize("changed",["task","old-parent","new-parent"])
def test_parent_assignment_stale_guards_are_atomic(handoff,changed):
    path=adopt(handoff)
    assert apply(path,propose(path,[{"op":"add","kind":"goal","title":"new goal"}],"new-p"),"new-a")["ok"]
    with sqlite3.connect(path) as con:
        task=con.execute("SELECT id FROM items WHERE kind='task'").fetchone()[0]
        goals=[row[0] for row in con.execute("SELECT id FROM items WHERE kind='goal' ORDER BY title")]
        con.execute("UPDATE items SET parent_id=? WHERE id=?",(goals[0],task))
    proposed=propose(path,[{"op":"update","item_id":task,"parent_id":goals[1]}],"move-p")
    target={"task":task,"old-parent":goals[0],"new-parent":goals[1]}[changed]
    with sqlite3.connect(path) as con:
        con.execute("UPDATE items SET children_version=children_version+1 WHERE id=?",(target,))
        before=list(con.iterdump())
    denied=apply(path,proposed,"move-a")
    assert denied["error"]["code"]=="STALE_PROPOSAL"
    with sqlite3.connect(path) as con: assert list(con.iterdump())==before


@pytest.mark.parametrize("invalid",["task-target","goal-source","removed-target","missing-target"])
def test_parent_assignment_validates_existing_task_and_live_goal(handoff,invalid):
    path=adopt(handoff)
    with sqlite3.connect(path) as con:
        task=con.execute("SELECT id FROM items WHERE kind='task'").fetchone()[0]
        goal=con.execute("SELECT id FROM items WHERE kind='goal'").fetchone()[0]
        if invalid=="removed-target": con.execute("UPDATE items SET removed_at='removed' WHERE id=?",(goal,))
    item=goal if invalid=="goal-source" else task
    parent=task if invalid=="task-target" else "missing" if invalid=="missing-target" else goal
    assert not propose(path,[{"op":"update","item_id":item,"parent_id":parent}],"invalid")["ok"]
