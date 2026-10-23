"""Manage only /opt, /etc and /var/lib/alphaproject-lab. Run through pinned SSH."""
import argparse
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import pwd
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

if __package__:
    from .artifact import sha, valid_id, unpack, verify_tree, validate
else:
    from artifact import sha, valid_id, unpack, verify_tree, validate

BASE=Path('/opt/alphaproject-lab')
DATA=Path('/var/lib/alphaproject-lab')
CONFIG=Path('/etc/alphaproject-lab/credentials')
STATE=Path('/root/alphaproject-lab-deploy')
UNIT=Path('/etc/systemd/system/alphaproject-lab.service')
SITE=Path('/etc/nginx/sites-available/alphaproject-mobile')
SNIPPET=Path('/etc/nginx/snippets/alphaproject-lab-location.conf')
INCLUDE='    include /etc/nginx/snippets/alphaproject-lab-location.conf;\n'
SITE_HASH='caa0b54864b61dd2e9d96576b99053a10c68e85a7b0789b5a7bc789095607fd0'
DB=DATA/'watchlist.sqlite3'
SERVICE='alphaproject-lab'


def stamp():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')


def run(args, *, env=None, cwd=None, timeout=180, log=None):
    result=subprocess.run([str(x) for x in args],cwd=cwd,env=env,capture_output=True,text=True,timeout=timeout)
    if log:
        log.write_text(result.stdout+'\n'+result.stderr,encoding='utf-8')
    if result.returncode:
        raise RuntimeError(f'{args[0]} failed ({result.returncode}): '+result.stderr[-1200:]+result.stdout[-1200:])
    return result.stdout


def save_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    temporary.chmod(0o600)
    os.replace(temporary,path)


def release_path(release_id):
    if not valid_id(release_id):
        raise ValueError('Invalid release id')
    return BASE/'releases'/release_id


def current_id():
    path=BASE/'current'
    if not path.exists() and not path.is_symlink():
        return None
    if not path.is_symlink():
        raise RuntimeError('Current entry is not a managed symlink')
    target=path.resolve()
    if target.parent!=BASE/'releases' or not valid_id(target.name):
        raise RuntimeError('Current entry leaves Lab release directory')
    return target.name


def health():
    try:
        with urllib.request.urlopen('http://127.0.0.1:18010/api/health',timeout=3) as response:
            return json.load(response)
    except Exception:
        return None


def status():
    return {'current_release':current_id(),'health':health(),
            'active':subprocess.run(['systemctl','is-active','--quiet',SERVICE]).returncode==0,
            'enabled':subprocess.run(['systemctl','is-enabled','--quiet',SERVICE]).returncode==0,
            'database_exists':DB.is_file(),'credentials_present':{name:(CONFIG/name).is_file() for name in ('ai.json','feishu.json')},
            'initial_migration':json.loads((STATE/'initial-migration.json').read_text()) if (STATE/'initial-migration.json').is_file() else None}


@contextmanager
def lock():
    STATE.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (STATE/'deployment.lock').open('a+') as handle:
        fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield


def directories():
    try:
        user=pwd.getpwnam(SERVICE)
    except KeyError:
        run(['useradd','--system','--user-group','--home-dir','/nonexistent','--no-create-home','--shell','/usr/sbin/nologin',SERVICE])
        user=pwd.getpwnam(SERVICE)
    if user.pw_dir!='/nonexistent' or user.pw_shell!='/usr/sbin/nologin':
        raise RuntimeError('Unexpected existing service account')
    for path in (BASE/'releases',BASE/'venvs'):
        path.mkdir(parents=True,exist_ok=True,mode=0o755)
    DATA.mkdir(parents=True,exist_ok=True,mode=0o700)
    os.chown(DATA,user.pw_uid,user.pw_gid)
    CONFIG.mkdir(parents=True,exist_ok=True,mode=0o700)
    return user


def prepare(archive,expected_hash):
    archive=archive.resolve()
    manifest,_=validate(archive,expected_hash)
    release_id=manifest['release']['release_id']
    if archive.parent!=STATE/'incoming'/release_id:
        raise RuntimeError('Archive outside this release incoming directory')
    directories()
    destination=release_path(release_id)
    unpack(archive,expected_hash,destination)
    dependency_hash=sha((destination/'backend/requirements.lock.txt').read_bytes())[:20]
    environment=BASE/'venvs'/dependency_hash
    if not (environment/'bin/python').is_file():
        run(['python3.12','-m','venv',environment])
    output=run([environment/'bin/python','-m','pip','install','--disable-pip-version-check','-r',destination/'backend/requirements.lock.txt'],timeout=300)
    (STATE/(release_id+'-dependencies.log')).write_text(output,encoding='utf-8')
    run([environment/'bin/python','-m','pip','check'])
    link=destination/'.venv'
    if link.is_symlink():
        if link.resolve()!=environment:
            raise RuntimeError('Unexpected dependency environment')
    elif link.exists():
        raise RuntimeError('Release venv entry is not a symlink')
    else:
        link.symlink_to(environment,target_is_directory=True)
    test_env={k:v for k,v in os.environ.items() if k not in ('CREDENTIALS_DIRECTORY','ALPHA_LAB_PUBLIC_URL','ALPHA_LAB_DB')}
    (STATE/'tests').mkdir(mode=0o700,parents=True,exist_ok=True)
    test_env.update(PYTHONDONTWRITEBYTECODE='1',ALPHA_LAB_DB=str(STATE/'tests'/release_id/'unused.sqlite3'))
    output=run([environment/'bin/python','-m','pytest','-q','-p','no:cacheprovider','--basetemp',STATE/'tests'/release_id],env=test_env,cwd=destination,timeout=240,log=STATE/(release_id+'-tests.log'))
    verify_tree(destination)
    result={'status':'prepared','release':manifest['release'],'dependency_environment':str(environment),'tests':output[-1600:]}
    save_json(STATE/(release_id+'-prepared.json'),result)
    return result


def backup(source,target):
    target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    if target.exists():
        raise RuntimeError('Backup target already exists')
    descriptor,name=tempfile.mkstemp(prefix='.backup-',suffix='.sqlite3',dir=target.parent)
    os.close(descriptor)
    temporary=Path(name)
    try:
        with closing(sqlite3.connect(f'file:{source.as_posix()}?mode=ro',uri=True)) as src,closing(sqlite3.connect(temporary)) as dst:
            src.backup(dst)
            assert dst.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            assert not dst.execute('PRAGMA foreign_key_check').fetchall()
        temporary.chmod(0o600)
        os.link(temporary,target)  # Atomic publication, refusing an existing target.
    finally:
        temporary.unlink(missing_ok=True)


def seed(release_id,initial,expected_hash):
    candidate=release_path(release_id)
    verify_tree(candidate)
    if current_id() or DB.exists():
        raise RuntimeError('Initial migration refuses an existing cloud installation or database')
    user=directories()
    payload=json.load(sys.stdin)
    if set(payload)!= {'ai','feishu'}:
        raise RuntimeError('Unexpected credential fields')
    sys.path.insert(0,str(candidate))
    from backend.app.ai_config import AIConfig
    from backend.app.feishu_config import FeishuConfig
    AIConfig(**payload['ai']).validate()
    FeishuConfig(**payload['feishu'])
    if not all(isinstance(v,str) and v for v in payload['feishu'].values()):
        raise RuntimeError('Invalid Feishu credential')
    initial=initial.resolve()
    if initial.parent!=STATE/'incoming'/release_id or sha(initial.read_bytes())!=expected_hash:
        raise RuntimeError('Initial database path or hash mismatch')
    # Separate root-only files. Never print the payload or put it into argv/env.
    for name,value in [('ai.json',payload['ai']),('feishu.json',payload['feishu'])]:
        path=CONFIG/name
        if path.exists():
            if path.is_symlink() or json.loads(path.read_text())!=value:
                raise RuntimeError('Existing credential differs; refusing replacement')
        else:
            descriptor,temporary_name=tempfile.mkstemp(prefix='.credential-',dir=CONFIG)
            temporary=Path(temporary_name)
            try:
                with os.fdopen(descriptor,'w',encoding='utf-8') as handle:
                    json.dump(value,handle,ensure_ascii=False)
                    handle.flush();os.fsync(handle.fileno())
                os.link(temporary,path)
            finally:
                temporary.unlink(missing_ok=True)
    backup(initial,DB)
    os.chown(DB,user.pw_uid,user.pw_gid)
    result={'status':'seeded','release_id':release_id,'initial_database_sha256':expected_hash,'secrets_in_code_archive':False}
    save_json(STATE/'initial-migration.json',result)
    return result


def preflight(release_id):
    candidate=release_path(release_id)
    verify_tree(candidate)
    copy=STATE/'preflight'/(release_id+'-'+stamp()+'.sqlite3')
    backup(DB,copy)
    env={**os.environ,'CREDENTIALS_DIRECTORY':str(CONFIG),'ALPHA_LAB_PUBLIC_URL':'https://8.155.130.86/lab',
         'PYTHONDONTWRITEBYTECODE':'1','ALPHA_LAB_DB':str(copy)}
    output=run([candidate/'.venv/bin/python',candidate/'deployment/cloud/preflight.py',copy],env=env,cwd=candidate,timeout=60)
    result=json.loads(output)
    result['database_copy']=str(copy)
    save_json(STATE/(release_id+'-preflight.json'),result)
    return result


def install_unit(candidate):
    content=(candidate/'deployment/cloud/service.conf').read_bytes()
    if UNIT.exists() and UNIT.read_bytes()!=content:
        raise RuntimeError('Existing Lab unit differs; review it before replacement')
    if not UNIT.exists():
        UNIT.write_bytes(content); UNIT.chmod(0o644)
        run(['systemctl','daemon-reload'])
    run(['systemctl','enable',SERVICE])


def publish(candidate):
    before=SITE.read_text()
    unified=STATE/'unified-site.json'
    if unified.is_file():
        marker=json.loads(unified.read_text())
        if sha(before.encode())!=marker['site_sha256'] or before.count(INCLUDE)!=1:
            raise RuntimeError('Unified site changed outside the managed handoff; refusing a blind edit')
        snippet_before=SNIPPET.read_bytes()
        SNIPPET.write_bytes((candidate/'deployment/cloud/nginx.location.conf').read_bytes())
        try:
            run(['nginx','-t']);run(['systemctl','reload','nginx'])
        except Exception:
            SNIPPET.write_bytes(snippet_before)
            run(['nginx','-t'])
            raise
        return
    original=before.replace(INCLUDE,'')
    if sha(original.encode())!=SITE_HASH or before.count(INCLUDE)>1:
        raise RuntimeError('Existing shared Nginx site changed; refusing a blind edit')
    backup_path=STATE/'nginx-before-lab.conf'
    if not backup_path.exists():
        backup_path.write_text(original); backup_path.chmod(0o600)
    snippet_before=SNIPPET.read_bytes() if SNIPPET.exists() else None
    SNIPPET.write_bytes((candidate/'deployment/cloud/nginx.location.conf').read_bytes())
    SNIPPET.chmod(0o644)
    if INCLUDE not in before:
        if before.count('    auth_basic off;\n')!=1:
            raise RuntimeError('Nginx insertion anchor changed')
        SITE.write_text(before.replace('    auth_basic off;\n','    auth_basic off;\n'+INCLUDE))
    try:
        run(['nginx','-t'])
        run(['systemctl','reload','nginx'])
    except Exception:
        SITE.write_text(before)
        if snippet_before is not None:
            SNIPPET.write_bytes(snippet_before)
        else:
            SNIPPET.unlink(missing_ok=True)
        run(['nginx','-t'])
        raise


def select(release_id):
    target=release_path(release_id)
    current_id()  # Validate that an existing link is managed.
    temporary=BASE/'current.next'
    if temporary.exists() or temporary.is_symlink():
        raise RuntimeError('A pending current link needs inspection')
    temporary.symlink_to(target,target_is_directory=True)
    os.replace(temporary,BASE/'current')


def await_health(release_id):
    deadline=time.monotonic()+25
    while time.monotonic()<deadline:
        value=health()
        if value and value.get('release_id')==release_id:
            if value.get('configuration_ready')!={'ai':True,'notifications':True}:
                raise RuntimeError('Actual service did not load its model/notification credentials')
            actual=Path(os.readlink(f"/proc/{value['pid']}/cwd"))
            if actual!=release_path(release_id):
                raise RuntimeError('Process directory differs from release')
            return value
        time.sleep(.25)
    raise RuntimeError('Candidate did not return the expected release')


def requires_unified_release():
    if any((STATE/name).is_file() for name in ('unified-site.json','unification-pending.json')):
        return True
    if DB.is_file():
        with closing(sqlite3.connect(DB.as_uri()+'?mode=ro',uri=True)) as db:
            return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='legacy_migrations'").fetchone()
                        and db.execute('SELECT 1 FROM legacy_migrations').fetchone())
    return False


def activate(release_id,expected_current):
    previous=current_id()
    if (previous or 'none')!=expected_current:
        raise RuntimeError('Current release changed; refusing stale switch')
    candidate=release_path(release_id)
    manifest=verify_tree(candidate)
    if requires_unified_release():
        if not (candidate/'backend/app/legacy_archive.py').is_file() or 'legacy_budget_usage=legacy_budget_usage' not in (candidate/'backend/app/main.py').read_text():
            raise RuntimeError('Unified data requires a release that carries the imported budget and archive; old binaries are read-only recovery tools')
    if not DB.is_file() or not (STATE/(release_id+'-prepared.json')).is_file():
        raise RuntimeError('Prepare and initial data migration must finish first')
    check=preflight(release_id)
    install_unit(candidate)
    snapshot=DATA/'backups'/(stamp()+'-before-'+release_id+'.sqlite3')
    try:
        run(['systemctl','stop',SERVICE],timeout=100)
        backup(DB,snapshot)
        # Repeat against the final stopped DB, not only a possibly stale live copy.
        check=preflight(release_id)
        select(release_id)
        run(['systemctl','start',SERVICE])
        active=await_health(release_id)
        publish(candidate)
        result={'status':'active','release':manifest['release'],'previous_release':previous,
                'health':active,'database_backup':str(snapshot),'preflight':check,
                'code_only_switch':True,'checked_at':datetime.now(timezone.utc).isoformat()}
        save_json(STATE/(stamp()+'-activation.json'),result)
        save_json(STATE/'latest.json',result)
        return result
    except Exception as error:
        run(['systemctl','stop',SERVICE],timeout=100)
        recovered=False
        if previous:
            select(previous)
            run(['systemctl','start',SERVICE])
            await_health(previous)
            recovered=True
        save_json(STATE/(stamp()+'-failed.json'),{'attempted':release_id,'previous':previous,
                  'code_recovered':recovered,'database_restored':False,'error':str(error)})
        raise


def install_operations(release_id):
    if current_id() != release_id:
        raise RuntimeError('Operations must target the current verified release')
    candidate = release_path(release_id)
    verify_tree(candidate)
    return json.loads(run([candidate/'.venv/bin/python', candidate/'deployment/cloud/operations.py',
                           'install', '--expected-current', release_id], timeout=210))


def install_acceptance(release_id, expected_observer=None):
    if current_id() != release_id:
        raise RuntimeError('Acceptance install must target the current verified release')
    candidate = release_path(release_id)
    verify_tree(candidate)
    arguments=[candidate/'.venv/bin/python', candidate/'deployment/cloud/acceptance.py',
               'install', '--expected-current', release_id]
    if expected_observer is not None:
        arguments.extend(['--expected-previous', expected_observer])
    return json.loads(run(arguments, timeout=2040))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['prepare','seed','activate','status','preflight','operations','acceptance'])
    parser.add_argument('--release')
    parser.add_argument('--archive',type=Path)
    parser.add_argument('--sha256')
    parser.add_argument('--database',type=Path)
    parser.add_argument('--expected-current')
    parser.add_argument('--expected-observer')
    args=parser.parse_args()
    if os.geteuid()!=0:
        raise RuntimeError('Use the authorized administrator SSH channel')
    with lock():
        if args.action=='prepare': result=prepare(args.archive,args.sha256)
        elif args.action=='seed': result=seed(args.release,args.database,args.sha256)
        elif args.action=='activate': result=activate(args.release,args.expected_current)
        elif args.action=='preflight': result=preflight(args.release)
        elif args.action=='operations': result=install_operations(args.release)
        elif args.action=='acceptance': result=install_acceptance(args.release,args.expected_observer)
        else: result=status()
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
