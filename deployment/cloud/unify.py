"""One-time, locked handoff from the original service to the verified Lab release.

Run on the existing server as root. Rehearsal uses private SQLite backups only.
After the final import commits, failures never restart the retired sender or
restore an old database over new data. All private archives remain on server.
"""
import argparse
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
import urllib.request
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from deployment.cloud import remote_release as r
from backend.app.extended_rules import DEFAULT_CONFIG, validate_config
from backend.app.legacy_archive import read_legacy_copy
from backend.app.main import create_app
from backend.app.securities import TencentResolver, Security
from fastapi.testclient import TestClient
import httpx

OLD_DB=Path('/var/lib/alphaproject/alpha.db')
ARCHIVES=Path('/root/alphaproject-unification')
MARKER=r.STATE/'unified-site.json'
PENDING=r.STATE/'unification-pending.json'
RETIRED=Path('/var/lib/alphaproject/RETIRED-UNIFIED')
RETIRE_UNIT=Path('/etc/systemd/system/alphaproject.service.d/90-unified-retired.conf')
RETIRE_CONTENT='[Unit]\n# Managed unification: old update tools cannot restart the retired sender.\nConditionPathExists=!/var/lib/alphaproject/RETIRED-UNIFIED\n'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fetch(url):
    with urllib.request.urlopen(url,timeout=10) as response:
        return json.load(response)


def legacy_settings():
    overrides=fetch('http://127.0.0.1:18000/anomaly/rules')
    values={'ANOMALY_VOLUME_RATIO':'3','ANOMALY_AMPLITUDE_PCT':'8','ANOMALY_LOOKBACK_DAYS':'60'}
    for line in Path('/etc/alphaproject/alpha.env').read_text().splitlines():
        key,sep,value=line.partition('=')
        if sep and key.strip() in values:
            tokens=shlex.split(value,comments=True)
            if len(tokens)!=1:raise RuntimeError('Cannot parse an original rule default')
            values[key.strip()]=tokens[0]
    effective=dict(DEFAULT_CONFIG)
    for key in effective:
        if key.endswith('_enabled'):
            effective[key]=overrides[key]
        else:
            value=overrides.get(key)
            if value is None:value=values['ANOMALY_'+key.upper()]
            effective[key]=int(value) if key=='lookback_days' else str(value)
    return {'overrides':overrides,'effective':validate_config(effective),
            'mapping_note':'仅为原版新导入股票承接原开关和阈值；现有学习版股票设置优先。来源涨跌停价代替按名称猜比例，突破使用不复权收盘价。'}


def restore_legacy_rules(settings):
    # The original service stores overrides only in memory, so a restart alone
    # cannot restore the user's runtime configuration.
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        try:
            request=urllib.request.Request('http://127.0.0.1:18000/anomaly/rules',method='PATCH',
                data=json.dumps(settings['overrides']).encode(),
                headers={'Content-Type':'application/json','Origin':'https://8.155.130.86'})
            with urllib.request.urlopen(request,timeout=3) as response:json.load(response)
            if fetch('http://127.0.0.1:18000/anomaly/rules')!=settings['overrides']:
                raise RuntimeError('Original rule overrides do not match their saved values')
            return
        except (OSError,ValueError,RuntimeError):
            time.sleep(.25)
    raise RuntimeError('Original service restarted but saved runtime rule overrides were not restored; keep the pending handoff record')


def database_rows(path):
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        db.execute('BEGIN')
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
        return {row[0]:[dict(item) for item in db.execute('SELECT * FROM "'+row[0]+'" ORDER BY rowid')]
                for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def check_preserved(before,after):
    skipped={'anomaly_runtime','briefing_runtime','research_collector_runtime'}
    checked=[]
    for table,old in before.items():
        if table in skipped:continue
        # Additive columns and new imported rows are allowed; existing values are not.
        remaining=list(after[table])
        for previous in old:
            match=next((i for i,new in enumerate(remaining) if all(new.get(k)==v for k,v in previous.items())),None)
            if match is None:raise RuntimeError('Existing data changed during import: '+table)
            remaining.pop(match)
        checked.append(table)
    return checked


def import_database(target,legacy,identities,settings):
    before=database_rows(target)
    os.environ['CREDENTIALS_DIRECTORY']=str(r.CONFIG)
    os.environ['ALPHA_LAB_PUBLIC_URL']='https://8.155.130.86/lab'
    app=create_app(target,allowed_hosts=['testserver'],run_monitor=False)
    with patch.object(httpx.HTTPTransport,'handle_request',side_effect=RuntimeError('Network forbidden inside migration')):
        with TestClient(app) as client:
            summary=app.state.legacy.import_copy(legacy,identities,settings)
            after=database_rows(target)
            replay=app.state.legacy.import_copy(legacy,identities,settings)
            if not replay['replayed'] or after!=database_rows(target):
                raise RuntimeError('Migration replay changed data')
            assert client.get('/api/legacy/migration').json()['source_fingerprint']==summary['source_fingerprint']
            assert client.get('/api/watchlist').status_code==200
            assert client.get('/api/anomalies').status_code==200
            assert client.get('/api/briefings').status_code==200
            for row in before.get('anomaly_events',[]):
                assert client.get('/api/events/'+row['id']).status_code==200
            for row in before.get('ai_quote_snapshots',[]):
                assert client.get('/api/quote-snapshots/'+row['id']+'/explanations').status_code==200
    after=database_rows(target)
    return {'summary':summary,'preserved_tables':check_preserved(before,after),'idempotent_replay':True,
            'counts_before':{k:len(v) for k,v in before.items()},'counts_after':{k:len(v) for k,v in after.items()},
            'external_http_calls_during_import':0,'old_notifications_requeued':0,
            'budget_estimate_micro':sum(row['charged_micro'] for row in after['legacy_budget_usage'])}


def old_binary_check(previous,database,folder):
    candidate=r.release_path(previous)
    compatibility=folder/'previous-binary.sqlite3'
    r.backup(database,compatibility)
    env={**os.environ,'CREDENTIALS_DIRECTORY':str(r.CONFIG),'ALPHA_LAB_PUBLIC_URL':'https://8.155.130.86/lab',
         'PYTHONDONTWRITEBYTECODE':'1','ALPHA_LAB_DB':str(compatibility)}
    # No external HTTP and no scheduler/model call. This is structural recovery,
    # not permission to run the retired binary with its older budget accounting.
    result=json.loads(r.run([candidate/'.venv/bin/python',candidate/'deployment/cloud/preflight.py',compatibility],
                            env=env,cwd=candidate,timeout=90))
    return {'read_integrity':result,'older_binary_may_run_paid_ai':False,
            'reason':'旧版本不认识导入预算和扩展事件，只用于私有只读恢复；正式版本回退必须保留统一预算适配。'}


def new_folder(kind):
    ARCHIVES.mkdir(mode=0o700,exist_ok=True)
    folder=ARCHIVES/(r.stamp()+'-'+kind)
    folder.mkdir(mode=0o700)
    return folder


def identities_for(data):
    resolver=TencentResolver()
    return {row['code']:resolver.resolve(row['code']) for row in data['watchlist_item'] if not row.get('deleted_at')}


def rehearse(expected_current,previous):
    if r.current_id()!=expected_current:raise RuntimeError('Current Lab release changed')
    r.verify_tree(ROOT)
    folder=new_folder('rehearsal')
    r.backup(OLD_DB,folder/'original.sqlite3');r.backup(r.DB,folder/'lab.sqlite3')
    source=read_legacy_copy(folder/'original.sqlite3')
    identities=identities_for(source)
    settings=legacy_settings()
    r.save_json(folder/'identities.json',{key:asdict(value) for key,value in identities.items()})
    r.save_json(folder/'rules.json',settings)
    before_hash=digest(folder/'original.sqlite3')
    migration=import_database(folder/'lab.sqlite3',folder/'original.sqlite3',identities,settings)
    if digest(folder/'original.sqlite3')!=before_hash:raise RuntimeError('Original copy was modified')
    compatibility=old_binary_check(previous,folder/'lab.sqlite3',folder)
    result={'status':'rehearsal_passed','release':expected_current,'previous_release':previous,'directory':str(folder),
            'original_copy_sha256':before_hash,'candidate_copy_sha256':digest(folder/'lab.sqlite3'),
            'migration':migration,'previous_binary':compatibility,'live_databases_modified':False}
    r.save_json(folder/'result.json',result);r.save_json(r.STATE/'latest-unification-rehearsal.json',result)
    return result


def unified_site(before):
    # Retain the exact existing TLS, ACME, public access, origin guard and limits.
    if before.count(r.INCLUDE)!=1 or r.sha(before.replace(r.INCLUDE,'').encode())!=r.SITE_HASH:
        raise RuntimeError('Shared Nginx site no longer matches the reviewed base')
    anchor='    location / {\n        proxy_pass http://127.0.0.1:18000;'
    start=before.index(anchor)
    end=before.index('    }\n',start)+len('    }\n')
    replacement='''    location = / { return 308 /lab/$is_args$args; }
    location = /release.json {
        proxy_pass http://127.0.0.1:18010/release.json;
        proxy_set_header Host 8.155.130.86;
    }
    location / {
        default_type application/json;
        return 410 '{"error":"original_api_retired","message":"Use the unified application at /lab/; original records are available in its history archive."}';
    }
'''
    return before[:start]+replacement+before[end:]


def validate_nginx_copy(folder,content):
    sites=list(Path('/etc/nginx/sites-enabled').iterdir())
    if len(sites)!=1 or sites[0].resolve()!=r.SITE:
        raise RuntimeError('Additional enabled sites need explicit routing review')
    candidate=folder/'nginx-candidate.conf'
    candidate.write_text(content);candidate.chmod(0o600)
    main=Path('/etc/nginx/nginx.conf').read_text()
    anchor='include /etc/nginx/sites-enabled/*;'
    if main.count(anchor)!=1:raise RuntimeError('Nginx enabled-site include changed')
    private=folder/'nginx-validation.conf'
    private.write_text(main.replace(anchor,'include '+str(candidate)+';'));private.chmod(0o600)
    r.run(['nginx','-t','-c',private])


def assert_stopped(service):
    if r.run(['systemctl','show',service,'-p','MainPID','--value']).strip()!='0':
        raise RuntimeError('Service still has a main process: '+service)
    if subprocess.run(['systemctl','is-active','--quiet',service]).returncode==0:
        raise RuntimeError('Service is still active: '+service)


def retirement_guard(release,folder,install=True):
    identity={'retired_by':'alphaproject-lab','release':release,'archive':str(folder)}
    if RETIRE_UNIT.exists() and RETIRE_UNIT.read_text()!=RETIRE_CONTENT:
        raise RuntimeError('Unknown original-service retirement override')
    if RETIRED.exists() and json.loads(RETIRED.read_text())!=identity:
        raise RuntimeError('Original service has a different retirement record')
    if install:
        RETIRE_UNIT.parent.mkdir(parents=True,exist_ok=True)
        RETIRE_UNIT.write_text(RETIRE_CONTENT);RETIRE_UNIT.chmod(0o644)
        r.save_json(RETIRED,identity)
    else:
        RETIRED.unlink(missing_ok=True);RETIRE_UNIT.unlink(missing_ok=True)
    r.run(['systemctl','daemon-reload'])


def archive_original(folder,before):
    release=Path('/opt/alphaproject/current').resolve()
    if not release.is_relative_to('/opt/alphaproject/releases'):
        raise RuntimeError('Original current release is not a managed release')
    archive=folder/'original-code.tar.gz'
    with tarfile.open(archive,'w:gz') as tar:
        for path in sorted(release.rglob('*')):
            relative=path.relative_to(release)
            if any(part in {'.venv','venv','node_modules','__pycache__','.git','.runtime'} for part in relative.parts):continue
            if path.is_file() and not path.is_symlink():tar.add(path,arcname=str(relative),recursive=False)
    archive.chmod(0o600)
    shutil.copytree('/etc/alphaproject',folder/'original-config')
    units=folder/'original-systemd';units.mkdir(mode=0o700)
    for path in [Path('/etc/systemd/system/alphaproject.service'),*Path('/etc/systemd/system/alphaproject.service.d').glob('*.conf')]:
        if path.is_file():shutil.copy2(path,units/path.name)
    (folder/'nginx-before.conf').write_text(before)
    hashes={str(p.relative_to(folder)):digest(p) for p in folder.rglob('*') if p.is_file()}
    for path in folder.rglob('*'):path.chmod(0o700 if path.is_dir() else 0o600)
    r.save_json(folder/'archive-hashes.json',hashes)
    return {'original_release':str(release),'files':len(hashes),'manifest':str(folder/'archive-hashes.json'),
            'manifest_sha256':digest(folder/'archive-hashes.json')}


def switch(expected_current):
    if MARKER.exists():
        result=json.loads(MARKER.read_text())
        if (r.sha(r.SITE.read_bytes())!=result['site_sha256']
                or subprocess.run(['systemctl','is-active','--quiet','alphaproject']).returncode==0
                or subprocess.run(['systemctl','is-enabled','--quiet','alphaproject']).returncode==0):
            raise RuntimeError('Existing handoff marker does not match live state')
        return {**result,'replayed':True}
    if r.current_id()!=expected_current or ROOT!=r.release_path(expected_current):raise RuntimeError('Run the current verified release only')
    rehearsal=json.loads((r.STATE/'latest-unification-rehearsal.json').read_text())
    if rehearsal['status']!='rehearsal_passed' or rehearsal['release']!=expected_current:
        raise RuntimeError('Successful rehearsal for this release is required')
    if PENDING.is_file():
        pending=json.loads(PENDING.read_text())
        if pending['release']!=expected_current:raise RuntimeError('Pending handoff belongs to another release')
        folder=Path(pending['directory']).resolve()
        if folder.parent!=ARCHIVES or not folder.is_dir():raise RuntimeError('Invalid pending handoff directory')
        before=(folder/'nginx-before.conf').read_text();after=unified_site(before)
        if r.sha(r.SITE.read_bytes()) not in (r.sha(before.encode()),r.sha(after.encode())):
            raise RuntimeError('Nginx changed outside the interrupted handoff')
        settings=json.loads((folder/'rules.json').read_text())
        identities={key:Security(**value) for key,value in json.loads((folder/'identities.json').read_text()).items()}
    else:
        before=r.SITE.read_text();after=unified_site(before)
        folder=new_folder('handoff')
        validate_nginx_copy(folder,after)
        settings=legacy_settings()
        source=read_legacy_copy(OLD_DB);identities=identities_for(source)
        (folder/'nginx-before.conf').write_text(before);(folder/'nginx-before.conf').chmod(0o600)
        r.save_json(folder/'rules.json',settings)
        r.save_json(folder/'identities.json',{key:asdict(value) for key,value in identities.items()})
        pending={'release':expected_current,'directory':str(folder),'phase':'prepared',
                 'original_enabled':subprocess.run(['systemctl','is-enabled','--quiet','alphaproject']).returncode==0}
        r.save_json(PENDING,pending)
    committed=False
    try:
        r.run(['systemctl','stop','alphaproject'],timeout=100)
        r.run(['systemctl','stop',r.SERVICE],timeout=100)
        assert_stopped('alphaproject');assert_stopped(r.SERVICE)
        r.run(['systemctl','disable','alphaproject'])
        if not (folder/'original.sqlite3').exists():r.backup(OLD_DB,folder/'original.sqlite3')
        r.backup(r.DB,folder/('lab-before-'+r.stamp()+'.sqlite3'))
        final_source=read_legacy_copy(folder/'original.sqlite3')
        if final_source!=read_legacy_copy(OLD_DB):raise RuntimeError('Original database changed after the frozen handoff copy')
        if any(row['code'] not in identities for row in final_source['watchlist_item'] if not row.get('deleted_at')):
            raise RuntimeError('Original watchlist changed during identity verification')
        if pending.get('archive'):
            archive=pending['archive']
            if digest(Path(archive['manifest']))!=archive['manifest_sha256']:raise RuntimeError('Private archive manifest changed')
            for name,expected in json.loads(Path(archive['manifest']).read_text()).items():
                if digest(folder/name)!=expected:raise RuntimeError('Private archive content changed: '+name)
        else:
            archive=archive_original(folder,before)
            pending.update(phase='archived',archive=archive)
            r.save_json(PENDING,pending)
        retirement_guard(expected_current,folder)
        migration=import_database(r.DB,folder/'original.sqlite3',identities,settings)
        committed=True
        pending['phase']='imported';r.save_json(PENDING,pending)
        r.run(['systemctl','start',r.SERVICE]);health=r.await_health(expected_current)
        r.SITE.write_text(after);r.run(['nginx','-t']);r.run(['systemctl','reload','nginx'])
        assert_stopped('alphaproject')
        result={'status':'unified','release':expected_current,'directory':str(folder),'archive':archive,'migration':migration,
                'health':health,'site_sha256':r.sha(after.encode()),'public_url':'https://8.155.130.86/',
                'application_url':'https://8.155.130.86/lab/','retired_service':'alphaproject','retired_database_deleted':False,
                'retirement_guard':str(RETIRE_UNIT),'retirement_marker':str(RETIRED),
                'single_business_owner':r.SERVICE,'completed_at':datetime.now(timezone.utc).isoformat()}
        r.save_json(folder/'result.json',result);r.save_json(MARKER,result)
        PENDING.unlink(missing_ok=True)
        return result
    except Exception as error:
        # Never put the original scheduler back after the data becomes unified.
        if not committed:
            with closing(sqlite3.connect(r.DB)) as db:
                committed=bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='legacy_migrations'").fetchone() and db.execute('SELECT 1 FROM legacy_migrations').fetchone())
        if committed:
            r.run(['systemctl','disable','alphaproject'])
            r.run(['systemctl','start',r.SERVICE])
            # The known-good candidate stays owner; leave route repair reviewable.
            pending['phase']='imported_needs_repair';r.save_json(PENDING,pending)
        else:
            r.SITE.write_text(before)
            retirement_guard(expected_current,folder,install=False)
            if pending['original_enabled']:r.run(['systemctl','enable','alphaproject'])
            r.run(['systemctl','start','alphaproject']);r.run(['systemctl','start',r.SERVICE])
            restore_legacy_rules(settings)
            PENDING.unlink(missing_ok=True)
        r.save_json(folder/'failure.json',{'error':str(error),'import_committed':committed,'database_restored':False,
                                         'original_restarted':not committed,'at':datetime.now(timezone.utc).isoformat()})
        raise


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['rehearse','switch'])
    parser.add_argument('--expected-current',required=True)
    parser.add_argument('--previous-release')
    args=parser.parse_args()
    if os.geteuid()!=0:raise RuntimeError('Use the existing authorized pinned SSH administrator channel')
    with r.lock():
        result=rehearse(args.expected_current,args.previous_release) if args.action=='rehearse' else switch(args.expected_current)
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
