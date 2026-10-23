"""Cloud entry and systemd-credential contract; only fake identities and isolated DBs."""
from dataclasses import asdict
import json
import os
import sqlite3
from pathlib import Path
import uuid

from fastapi.testclient import TestClient
import pytest

from backend.app.ai_config import load_config as load_ai
from backend.app.anomaly_rules import Sample
from backend.app.feishu_config import ConfigFailure, load_config as load_feishu, service_credential, credential_permissions
from backend.app.main import create_app
from backend.app.quotes import Quote
from backend.app.runtime_config import APP_VERSION, public_location, release_info
from backend.app.securities import Security
from backend.tests.test_evidence_entry import Model
from backend.tests.test_explanations import Clock, CONFIG as AI
from backend.tests.test_notifications import CONFIG as FEISHU


@pytest.fixture
def credentials(tmp_path, monkeypatch):
    directory=tmp_path/'service-credentials'
    directory.mkdir()
    for name,value in [('feishu.json',FEISHU),('ai.json',AI)]:
        target=directory/name
        target.write_text(json.dumps(asdict(value)),encoding='utf-8')
        target.chmod(0o600)
    monkeypatch.setenv('CREDENTIALS_DIRECTORY',str(directory))
    yield directory


def test_protected_service_credentials_load_and_do_not_need_windows_vault(credentials,tmp_path):
    assert load_ai(tmp_path/'absent.dpapi')==AI
    assert load_feishu(tmp_path/'absent.dpapi')==FEISHU
    (credentials/'ai.json').unlink()
    with pytest.raises(ConfigFailure,match='服务凭据'):
        load_ai(tmp_path/'absent.dpapi')


@pytest.mark.skipif(os.name!='posix',reason='POSIX file permission enforcement')
def test_shared_credential_file_rejected(credentials):
    (credentials/'ai.json').chmod(0o644)
    with pytest.raises(ConfigFailure):
        service_credential('ai.json')


def test_credential_name_and_json_boundaries(credentials):
    with pytest.raises(ConfigFailure):
        service_credential('../anything')
    (credentials/'feishu.json').write_text('[]',encoding='utf-8')
    with pytest.raises(ConfigFailure):
        load_feishu(Path('unused'))


def test_systemd_readonly_credential_permissions_remain_private():
    protected='/run/credentials/alphaproject-lab.service'
    assert credential_permissions(0o440,0,0,996,protected)
    assert credential_permissions(0o600,0,0,996,'/etc/alphaproject-lab/credentials')
    assert credential_permissions(0o400,996,996,996,protected)
    assert not credential_permissions(0o440,0,0,996,'/tmp/untrusted')
    assert not credential_permissions(0o440,0,100,996,protected)
    assert not credential_permissions(0o644,0,0,996,protected)
    assert not credential_permissions(0o460,0,0,996,protected)


def test_acceptance_service_can_read_only_its_protected_feishu_credential():
    protected='/run/credentials/alphaproject-lab-acceptance.service'
    assert credential_permissions(0o440,0,0,996,protected,'feishu.json')
    assert credential_permissions(0o400,996,996,996,protected,'feishu.json')
    for name in ('ai.json', None):
        assert not credential_permissions(0o440,0,0,996,protected,name)
        assert not credential_permissions(0o600,0,0,996,protected,name)
    assert not credential_permissions(0o440,0,100,996,protected,'feishu.json')
    assert not credential_permissions(0o644,0,0,996,protected,'feishu.json')
    assert not credential_permissions(0o440,0,0,996,protected+'-other','feishu.json')


@pytest.mark.parametrize('missing',['ai.json','feishu.json'])
def test_public_startup_rejects_missing_credentials_before_changing_existing_data(credentials,tmp_path,monkeypatch,missing):
    monkeypatch.setenv('ALPHA_LAB_PUBLIC_URL','https://example.com/lab')
    database=tmp_path/'existing.sqlite3'
    with TestClient(create_app(database,run_monitor=False),base_url='https://example.com'):
        pass
    with sqlite3.connect(database) as db:
        db.execute('UPDATE notification_settings SET enabled=1,verified_at=?',('previous-verification',))
    def settings():
        with sqlite3.connect(database) as db:
            return db.execute('SELECT * FROM notification_settings').fetchall()
    before=settings()
    (credentials/missing).unlink()
    with pytest.raises(ConfigFailure):
        with TestClient(create_app(database,run_monitor=False),base_url='https://example.com'):
            pass
    assert settings()==before


@pytest.mark.parametrize('value',['http://example.com/lab','https://name:secret@example.com/lab','https://example.com/lab?x=1','https://example.com:8000/lab','https://example.com/lab/../','https://example.com/%2flab'])
def test_invalid_public_entry_is_rejected(value,monkeypatch):
    monkeypatch.setenv('ALPHA_LAB_PUBLIC_URL',value)
    with pytest.raises((RuntimeError,ValueError)):
        public_location()


def test_release_identity_refuses_mixed_code(tmp_path):
    (tmp_path/'release.json').write_text(json.dumps({'app_version':'old','release_id':'lab-fixture'}),encoding='utf-8')
    with pytest.raises(RuntimeError):
        release_info(tmp_path)


def test_public_entry_uses_configured_host_prefix_origin_and_same_saved_evidence(credentials,tmp_path,monkeypatch):
    monkeypatch.setenv('ALPHA_LAB_PUBLIC_URL','https://example.com/lab/')
    clock,model=Clock(),Model()
    app=create_app(tmp_path/'cloud.sqlite3',clock=clock,ai_client=model,run_monitor=False)
    with TestClient(app,base_url='https://example.com') as client:
        access=client.get('/api/access').json()
        assert access['mode']=='public' and access['public_url']=='https://example.com/lab/'
        assert access['lan_urls']==[] and access['release']['app_version']==APP_VERSION
        assert client.get('/api/health').json()['configuration_ready']=={'ai':True,'notifications':True}
        assert app.state.notifications.config==FEISHU and app.state.explanations.config==AI
        assert app.state.notifications.detail_base_url=='https://example.com/lab'
        assert client.get('/api/health',headers={'Host':'untrusted.example'}).status_code==400
        assert client.post('/api/notifications/settings',headers={'X-Alpha-Request':'watchlist-v1','Origin':'https://attacker.invalid'},json={'enabled':False}).status_code==403
        headers={'X-Alpha-Request':'watchlist-v1','Origin':'https://example.com'}
        assert client.post('/api/notifications/settings',headers=headers,json={'enabled':False}).status_code==200
        item,_=app.state.store.add(Security('600519','sh','合成股票','fixture',clock().isoformat()))
        quote=Quote('sh','600519','合成股票','103','100','3','3',clock().isoformat(),clock().isoformat(),'fixture','','')
        rule=app.state.anomalies.save_rule('sh','600519',item['created_at'],'3','3',False,0)
        event_id=app.state.anomalies.evaluate(rule,Sample(quote,'trading',True,True),mode='demo',directions=('up',),notify=False)[0]['event_id']
        assert client.get('/api/events/'+event_id).json()['detail_path']=='/lab/#event='+event_id
        snapshot_id=str(uuid.uuid4())
        with app.state.store.connect() as db,db:
            db.execute('INSERT INTO ai_quote_snapshots VALUES (?,?,?,?,?,?,?)',(snapshot_id,'sh','600519',item['created_at'],json.dumps(quote.to_dict()),'fixture-fingerprint',clock().isoformat()))
        snapshots=client.get('/api/stocks/sh/600519/explanations').json()['snapshots']
        assert snapshots[0]['detail_path']=='/lab/#quote='+snapshot_id
        public=json.dumps([client.get('/api/notifications').json(),client.get('/api/events/'+event_id+'/explanations').json()])
        assert AI.api_key not in public and FEISHU.app_secret not in public
        assert not model.calls
