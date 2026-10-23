"""First migration control flow with fake network, commands, credentials and tiny DBs."""
import json
import sqlite3

import httpx
import pytest

from backend.app.runtime_lock import RuntimeLock
from backend.tests.test_explanations import CONFIG as AI
from backend.tests.test_notifications import CONFIG as FEISHU
from deployment.cloud import deploy


@pytest.fixture
def migration(tmp_path,monkeypatch):
    root=tmp_path/'project';runtime=root/'.runtime';runtime.mkdir(parents=True)
    database=runtime/'watchlist.sqlite3'
    with sqlite3.connect(database) as db:
        for name in ('ai_explanations','anomaly_runs','briefing_reports','notification_messages'):
            db.execute(f'CREATE TABLE {name}(status TEXT)')
            db.execute(f"INSERT INTO {name} VALUES ('succeeded')")
    monkeypatch.setattr(deploy,'ROOT',root)
    monkeypatch.setattr(deploy,'RUNS',runtime/'runs')
    monkeypatch.delenv('CREDENTIALS_DIRECTORY',raising=False)
    monkeypatch.setattr('backend.app.ai_config.load_config',lambda path:AI)
    monkeypatch.setattr('backend.app.feishu_config.load_config',lambda path:FEISHU)
    state={'stopped':False,'stop_is_noop':False,'fail_upload':False,'health_reads':0,'uploads':0,'seed_calls':0,'cloud':None}
    original_client=httpx.Client
    def respond(request):
        if request.url.path=='/api/health':
            state['health_reads']+=1
            if state['stopped']:
                raise httpx.ConnectError('test process stopped',request=request)
            return httpx.Response(200,json={'service':'AlphaProject-Lab','pid':123,'instance_id':'fixture'})
        return httpx.Response(200,json={'active':False})
    monkeypatch.setattr(deploy.httpx,'Client',lambda **kwargs:original_client(transport=httpx.MockTransport(respond),**kwargs))
    def command(args,**kwargs):
        if 'stop' in args:
            state['stopped']=not state['stop_is_noop']
        else:
            state['uploads']+=1
            if state['fail_upload']:
                state['fail_upload']=False
                raise RuntimeError('simulated upload interruption')
        return ''
    def remote(record,files,action,arguments=(),**kwargs):
        if action=='status':
            return {'database_exists':bool(state['cloud']),'current_release':None,'initial_migration':state['cloud']}
        assert action=='seed'
        state['seed_calls']+=1
        state['cloud']={'release_id':arguments[1],'initial_database_sha256':arguments[-1]}
        return {'status':'seeded'}
    monkeypatch.setattr(deploy,'command',command)
    monkeypatch.setattr(deploy,'remote',remote)
    return state,{'release':{'release_id':'lab-fixture'}},database


def test_successful_stop_command_is_not_accepted_while_instance_responds(migration):
    state,record,database=migration
    state['stop_is_noop']=True
    with pytest.raises(RuntimeError,match='still responds'):
        deploy.seed(record,{})
    assert state['health_reads']==2 and state['uploads']==state['seed_calls']==0
    assert not (deploy.ROOT/'.runtime/cloud-primary.json').exists()


def test_migration_refuses_a_database_still_owned_by_another_instance(migration):
    state,record,database=migration
    owner=RuntimeLock(database.with_suffix('.service.lock'));owner.acquire()
    try:
        with pytest.raises(RuntimeError,match='已有服务运行'):
            deploy.seed(record,{})
        assert state['seed_calls']==state['uploads']==0
    finally:
        owner.release()


def test_upload_interruption_resumes_same_verified_backup_and_seed_receipt(migration):
    state,record,database=migration
    state['fail_upload']=True
    with pytest.raises(RuntimeError,match='upload interruption'):
        deploy.seed(record,{})
    initial=deploy.RUNS/'lab-fixture/initial.sqlite3'
    saved_bytes=initial.read_bytes()
    assert state['stopped'] and state['seed_calls']==0
    assert deploy.seed(record,{})['status']=='seeded'
    assert initial.read_bytes()==saved_bytes and state['uploads']==2 and state['seed_calls']==1
    assert deploy.seed(record,{})['status']=='seeded_existing'
    assert state['seed_calls']==1 and state['uploads']==2


def test_resume_refuses_local_data_changed_after_backup(migration):
    state,record,database=migration
    state['fail_upload']=True
    with pytest.raises(RuntimeError):
        deploy.seed(record,{})
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO ai_explanations VALUES ('succeeded')")
    with pytest.raises(RuntimeError,match='Local data changed'):
        deploy.seed(record,{})
    assert state['seed_calls']==0 and state['uploads']==1
