"""Explicit Lab deployment commands. No original AlphaProject updater is invoked."""
import argparse
from dataclasses import asdict
from contextlib import closing
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
import tempfile

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from deployment.cloud.artifact import build as pack, source_digest, validate, sha
import httpx

HOST='root@8.155.130.86'
URL='https://8.155.130.86/lab/'
ACCESS=ROOT.parent/'.runtime/aliyun-access'
SSH=Path('D:/Git/usr/bin/ssh.exe')
SCP=Path('D:/Git/usr/bin/scp.exe')
PACKAGES=ROOT/'.runtime/cloud-packages'
RUNS=ROOT/'.runtime/cloud-runs'
PYTHON=ROOT/'.venv/Scripts/python.exe'
OPTIONS=['-i',str(ACCESS/'id_ed25519'),'-o','UserKnownHostsFile='+str(ACCESS/'known_hosts'),
         '-o','StrictHostKeyChecking=yes','-o','BatchMode=yes','-o','ConnectTimeout=15']


def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')


def command(args,*,cwd=ROOT,input_data=None,timeout=360,log=None):
    result=subprocess.run([str(x) for x in args],cwd=cwd,input=input_data,capture_output=True,timeout=timeout)
    output=result.stdout.decode('utf-8',errors='replace')
    error=result.stderr.decode('utf-8',errors='replace')
    if log:
        log.parent.mkdir(parents=True,exist_ok=True)
        log.write_text(output+'\n'+error,encoding='utf-8')
    if result.returncode:
        raise RuntimeError(f'Command failed ({result.returncode}): {Path(args[0]).name}\n'+output[-1600:]+error[-1600:])
    return output


def ssh(arguments,*,input_data=None,timeout=360):
    return command([SSH,*OPTIONS,HOST,' '.join(shlex.quote(str(x)) for x in arguments)],input_data=input_data,timeout=timeout)


def package_record(path):
    record=json.loads(path.read_text(encoding='utf-8'))
    manifest,files=validate(Path(record['archive']),record['sha256'])
    if manifest['release']!=record['release']:
        raise RuntimeError('Package record mismatch')
    return record,files


def local_tools(record,files):
    directory=RUNS/record['release']['release_id']/'tools'
    directory.mkdir(parents=True,exist_ok=True)
    for name in ('artifact.py','remote_release.py'):
        (directory/name).write_bytes(files['deployment/cloud/'+name])
    return directory


def remote(record,files,action,arguments=(),*,payload=None):
    release_id=record['release']['release_id']
    directory='/root/alphaproject-lab-deploy/incoming/'+release_id
    hashes={directory+'/'+name:sha(files['deployment/cloud/'+name]) for name in ('artifact.py','remote_release.py')}
    script=directory+'/remote_release.py'
    code=("import hashlib,pathlib,runpy,sys; "
          f"expected={hashes!r}; "
          "assert all(hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()==v for p,v in expected.items()),'Deployment tool hash mismatch'; "
          f"sys.path.insert(0,{directory!r}); sys.argv={[script,action,*arguments]!r}; "
          f"runpy.run_path({script!r},run_name='__main__')")
    result=json.loads(ssh(['python3','-c',code],input_data=payload,timeout=2070 if action=='acceptance' else 360))
    save(RUNS/release_id/(action+'-'+datetime.now(timezone.utc).strftime('%H%M%S%f')+'.json'),result)
    return result


def build():
    before=source_digest(ROOT)
    logs=RUNS/('checks-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    command([PYTHON,'-m','pip','check'],log=logs/'python-dependencies.txt')
    command([PYTHON,'-X','utf8','-m','pytest','-q'],log=logs/'tests.txt')
    npm=shutil.which('npm.cmd') or shutil.which('npm')
    if not npm: raise RuntimeError('npm is unavailable')
    command([npm,'ci'],cwd=ROOT/'frontend',log=logs/'npm-ci.txt')
    command([npm,'run','check'],cwd=ROOT/'frontend',log=logs/'typecheck.txt')
    command([npm,'exec','vite','build','--','--base=/lab/','--outDir=dist-cloud'],cwd=ROOT/'frontend',log=logs/'build.txt')
    if source_digest(ROOT)!=before:
        raise RuntimeError('Source changed during validation; rerun checks')
    result=pack(ROOT,PACKAGES)
    result['local_checks']=str(logs)
    result['package_record']=str(PACKAGES/(result['release']['release_id']+'.json'))
    save(Path(result['package_record']),result)
    save(PACKAGES/'latest.json',result)
    return result


def prepare(record,files):
    release_id=record['release']['release_id']
    target='/root/alphaproject-lab-deploy/incoming/'+release_id
    tooling=local_tools(record,files)
    ssh(['install','-d','-m','700',target])
    command([SCP,*OPTIONS,tooling/'artifact.py',tooling/'remote_release.py',Path(record['archive']),HOST+':'+target+'/'])
    return remote(record,files,'prepare',['--archive',target+'/'+Path(record['archive']).name,'--sha256',record['sha256']])


def seed(record,files):
    # Recover an uncertain earlier outcome before creating another migration.
    current=remote(record,files,'status')
    release_id=record['release']['release_id']
    marker=ROOT/'.runtime/cloud-primary.json'
    saved=json.loads(marker.read_text(encoding='utf-8')) if marker.is_file() else None
    if saved and (saved.get('release_id')!=release_id or saved.get('url')!=URL):
        raise RuntimeError('A different migration is recorded; inspect it before continuing')
    if current['database_exists'] or current['current_release']:
        previous=current.get('initial_migration') or {}
        if (saved and previous.get('release_id')==release_id and saved.get('initial_database_sha256')
                and previous.get('initial_database_sha256')==saved['initial_database_sha256']):
            return {'status':'seeded_existing','initial_migration':previous,'cloud_data_overwritten':False}
        raise RuntimeError('Cloud data already exists; this command will not overwrite it')
    if os.environ.get('CREDENTIALS_DIRECTORY'):
        raise RuntimeError('Initial migration must use the current Windows user vault')
    from backend.app.ai_config import load_config as load_ai
    from backend.app.feishu_config import load_config as load_feishu
    ai=load_ai(ROOT/'.runtime/ai-config.dpapi')
    feishu=load_feishu(ROOT/'.runtime/feishu-config.dpapi')
    if not ai or not feishu:
        raise RuntimeError('Lab model or notification configuration is missing')
    database=ROOT/'.runtime/watchlist.sqlite3'
    if not saved:
        with httpx.Client(base_url='http://127.0.0.1:18765',trust_env=False,timeout=5) as client:
            health=client.get('/api/health').json()
            if health.get('service')!='AlphaProject-Lab':
                raise RuntimeError('Local process identity changed')
            if client.get('/api/notifications').json()['active'] or client.get('/api/briefings').json()['active']:
                raise RuntimeError('Finish the active notification/briefing before migration')
        check_idle(database)
        command([PYTHON,'-X','utf8','scripts/lab.py','stop'])
        saved={'url':URL,'state':'migrating','release_id':release_id,'local_process_before_stop':health,
               'at':datetime.now(timezone.utc).isoformat()}
    from backend.app.runtime_lock import RuntimeLock
    ownership=RuntimeLock(database.with_suffix('.service.lock'))
    ownership.acquire()
    try:
        # Holding this DB's process lock proves no app instance owns it. Also reject a live local entry.
        with httpx.Client(base_url='http://127.0.0.1:18765',trust_env=False,timeout=5) as client:
            try:
                client.get('/api/health')
            except httpx.ConnectError:
                pass
            else:
                raise RuntimeError('Local entry still responds after stop; migration refused')
        check_idle(database)
        save(marker,saved)
        copy=RUNS/release_id/'initial.sqlite3'
        copy.parent.mkdir(parents=True,exist_ok=True)
        if not copy.exists():
            descriptor,name=tempfile.mkstemp(prefix='.initial-',suffix='.sqlite3',dir=copy.parent)
            os.close(descriptor)
            temporary=Path(name)
            try:
                with closing(sqlite3.connect(f'file:{database.as_posix()}?mode=ro',uri=True)) as src,closing(sqlite3.connect(temporary)) as dst:
                    src.backup(dst)
                    assert dst.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
                    assert not dst.execute('PRAGMA foreign_key_check').fetchall()
                os.link(temporary,copy)
            finally:
                temporary.unlink(missing_ok=True)
        copy_hash=sha(copy.read_bytes())
        if saved.get('initial_database_sha256') not in (None,copy_hash):
            raise RuntimeError('Saved migration backup changed')
        if database_rows(database)!=database_rows(copy):
            raise RuntimeError('Local data changed since the migration backup; refusing stale data')
        saved['initial_database_sha256']=copy_hash
        save(marker,saved)
        remote_path='/root/alphaproject-lab-deploy/incoming/'+release_id+'/'+copy.name
        command([SCP,*OPTIONS,copy,HOST+':'+remote_path])
        payload=json.dumps({'ai':asdict(ai),'feishu':asdict(feishu)},ensure_ascii=False).encode('utf-8')
        return remote(record,files,'seed',['--release',release_id,'--database',remote_path,'--sha256',copy_hash],payload=payload)
    finally:
        ownership.release()


def check_idle(database):
    with closing(sqlite3.connect(f'file:{database.as_posix()}?mode=ro',uri=True)) as db:
        for table,statuses in [('ai_explanations',('pending','running')),('anomaly_runs',('pending','running')),
                               ('briefing_reports',('pending','running')),('notification_messages',('pending','checking','sending'))]:
            placeholders=','.join('?' for _ in statuses)
            if db.execute(f'SELECT count(*) FROM {table} WHERE status IN ({placeholders})',statuses).fetchone()[0]:
                raise RuntimeError('Finish active work before migration: '+table)


def database_rows(path):
    with closing(sqlite3.connect(f'file:{path.as_posix()}?mode=ro',uri=True)) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
        return {r[0]:db.execute('SELECT * FROM "'+r[0].replace('"','""')+'" ORDER BY rowid').fetchall()
                for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def verify_public_owner(client, release):
    """Verify the public owner before and after the explicit one-time handoff."""
    migration = client.get('api/legacy/migration')
    if migration.status_code == 404:
        imported = False  # Historical releases before the migration API.
    else:
        migration.raise_for_status()
        state = migration.json()
        if state.get('status') not in ('imported', 'not_imported'):
            raise RuntimeError('Unknown public migration state')
        imported = state['status'] == 'imported'
    root_release = client.get('https://8.155.130.86/release.json')
    root_release.raise_for_status()
    if imported:
        if root_release.json() != release:
            raise RuntimeError('Unified root release differs from the active application')
        root = client.get('https://8.155.130.86/')
        if root.status_code != 308 or root.headers.get('location') not in ('/lab/', URL):
            raise RuntimeError('Unified root does not redirect to the application')
        for method in ('GET', 'POST'):
            retired = client.request(method, 'https://8.155.130.86/watchlist',
                                     headers={'Origin': 'https://8.155.130.86'})
            if retired.status_code != 410 or retired.json().get('error') != 'original_api_retired':
                raise RuntimeError('Original API has not been retired')
        return {'site_mode': 'unified', 'root_release_matches': True, 'original_api_retired': True}
    if root_release.json().get('release_id') != 'alphaproject-20261015T211612763703Z':
        raise RuntimeError('Original public release changed before migration')
    return {'site_mode': 'separate', 'original_release_unchanged': True}


def verify(record):
    manifest,_=validate(Path(record['archive']),record['sha256'])
    with httpx.Client(base_url=URL,timeout=20,trust_env=False,follow_redirects=False) as client:
        def get(path):
            response=client.get(path)
            response.raise_for_status()
            return response
        release=get('release.json').json()
        if release!=record['release']:
            raise RuntimeError('Public release differs from the package')
        health=get('api/health').json()
        if health['release_id']!=release['release_id']:
            raise RuntimeError('Public backend release differs')
        assert health.get('configuration_ready')=={'ai':True,'notifications':True},'Actual service credentials are not ready'
        access=get('api/access').json()
        assert access['mode']=='public' and access['public_url']==URL
        watchlist=get('api/watchlist').json()
        assets={}
        for name,meta in manifest['files'].items():
            if name.startswith('frontend/dist/'):
                relative=name.removeprefix('frontend/dist/')
                response=get('' if relative=='index.html' else relative)
                assert sha(response.content)==meta['sha256'],relative
                assets[relative]=meta['sha256']
        ownership = verify_public_owner(client, release)
    result={'status':'public_artifacts_verified','url':URL,'https_certificate_verified':True,
            'release':release,'health':health,'watchlist':watchlist,'served_assets':assets,
            **ownership,'business_flow':'requires separate browser acceptance'}
    save(RUNS/release['release_id']/'public-verification.json',result)
    marker=ROOT/'.runtime/cloud-primary.json'
    if marker.exists():
        value=json.loads(marker.read_text(encoding='utf-8'))
        value.update(state='cloud-active',release_id=release['release_id'])
        save(marker,value)
    return result


def main():
    parser=argparse.ArgumentParser(description='Separate AlphaProject Lab cloud release')
    parser.add_argument('action',choices=['build','prepare','seed','activate','status','verify','update','operations','acceptance'])
    parser.add_argument('--package',type=Path)
    parser.add_argument('--expected-current')
    parser.add_argument('--expected-observer')
    args=parser.parse_args()
    if args.action=='build':
        result=build()
    elif args.action=='update':
        built=build(); record,files=package_record(Path(built['package_record']))
        prepare(record,files)
        before=remote(record,files,'status')
        if not before['current_release'] or not before['database_exists']:
            raise RuntimeError('Use the first migration procedure before routine updates')
        remote(record,files,'activate',['--release',record['release']['release_id'],'--expected-current',before['current_release']])
        result=verify(record)
    else:
        if not args.package: parser.error('--package is required')
        record,files=package_record(args.package)
        if args.action=='prepare': result=prepare(record,files)
        elif args.action=='seed': result=seed(record,files)
        elif args.action=='activate':
            if not args.expected_current: parser.error('--expected-current is required')
            result=remote(record,files,'activate',['--release',record['release']['release_id'],'--expected-current',args.expected_current])
        elif args.action=='verify': result=verify(record)
        elif args.action in ('operations','acceptance'):
            verify(record)
            arguments=['--release',record['release']['release_id']]
            if args.action=='acceptance' and args.expected_observer:
                arguments.extend(['--expected-observer',args.expected_observer])
            result=remote(record,files,args.action,arguments)
        else: result=remote(record,files,'status')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    try:
        main()
    except Exception as error:
        print('Deployment did not complete: '+str(error),file=sys.stderr)
        raise SystemExit(1)
