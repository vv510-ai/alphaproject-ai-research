"""Archive rejection and recovery behaviour, without real hosts or production data."""
import io
import json
from pathlib import Path
import sqlite3
import tarfile

import pytest

from deployment.cloud.artifact import allowed, sha, unpack, validate


def package(tmp_path,extra=None,wrong_hash=False):
    release={'release_id':'lab-test','app_version':'0.9.0','source_sha256':'0'*64,'built_at':'fixture'}
    files={'release.json':json.dumps(release).encode(),'backend/app/example.py':b'print("fixture")\n'}
    manifest={'format':1,'release':release,'files':{k:{'sha256':sha(v),'size':len(v)} for k,v in files.items()}}
    if wrong_hash:
        manifest['files']['backend/app/example.py']['sha256']='f'*64
    files['manifest.json']=json.dumps(manifest).encode()
    if extra:
        files[extra]=b'not a valid release asset'
    path=tmp_path/'fixture.tar.gz'
    with tarfile.open(path,'w:gz') as tar:
        for name,data in files.items():
            info=tarfile.TarInfo(name);info.size=len(data)
            tar.addfile(info,io.BytesIO(data))
    return path


@pytest.mark.parametrize('name',['../escape.py','/absolute.py','backend/app/../../.env','.env','.runtime/watchlist.sqlite3','backend/app/key.pem','frontend/dist/database.db','backend\\app\\x.py'])
def test_release_rejects_data_secrets_and_path_escape(name,tmp_path):
    assert not allowed(name)
    path=package(tmp_path,name)
    with pytest.raises(ValueError):
        unpack(path,sha(path.read_bytes()),tmp_path/'lab-test')
    assert not (tmp_path/'lab-test').exists()


def test_code_hash_verification_and_idempotent_extraction(tmp_path):
    path=package(tmp_path)
    destination=tmp_path/'lab-test'
    unpack(path,sha(path.read_bytes()),destination)
    unpack(path,sha(path.read_bytes()),destination)
    (destination/'backend/app/example.py').write_bytes(b'changed')
    with pytest.raises(ValueError,match='changed'):
        unpack(path,sha(path.read_bytes()),destination)


def test_archive_and_member_hash_must_both_match(tmp_path):
    path=package(tmp_path,wrong_hash=True)
    with pytest.raises(ValueError,match='Archive hash'):
        validate(path,'0'*64)
    with pytest.raises(ValueError,match='File hash'):
        validate(path,sha(path.read_bytes()))


def test_interrupted_extraction_can_retry_without_a_partial_final_release(tmp_path,monkeypatch):
    path=package(tmp_path)
    destination=tmp_path/'lab-test'
    original=Path.open
    def interrupted(self,mode='r',*args,**kwargs):
        if self.name=='example.py' and mode=='xb':
            raise OSError('simulated disk failure on second file')
        return original(self,mode,*args,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path,'open',interrupted)
        with pytest.raises(OSError):
            unpack(path,sha(path.read_bytes()),destination)
    assert not destination.exists() and not list(tmp_path.glob('.unpack-*'))
    unpack(path,sha(path.read_bytes()),destination)
    assert (destination/'backend/app/example.py').is_file()


def test_failed_backup_does_not_publish_empty_database_and_can_retry(tmp_path):
    pytest.importorskip('fcntl',reason='Linux backup implementation')
    from deployment.cloud.remote_release import backup
    source=tmp_path/'missing.sqlite3';target=tmp_path/'final.sqlite3'
    with pytest.raises(sqlite3.OperationalError):
        backup(source,target)
    assert not target.exists() and not list(tmp_path.glob('.backup-*'))
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE records(value TEXT)')
        db.execute("INSERT INTO records VALUES ('preserved')")
    backup(source,target)
    with sqlite3.connect(target) as db:
        assert db.execute('SELECT * FROM records').fetchall()==[('preserved',)]
    with pytest.raises(RuntimeError,match='already exists'):
        backup(source,target)


def test_failed_activation_restores_code_and_keeps_post_start_data(tmp_path,monkeypatch):
    pytest.importorskip('fcntl',reason='POSIX release-switch recovery runs on the target Linux environment')
    from deployment.cloud import remote_release as module
    base=tmp_path/'app';data=tmp_path/'data';state=tmp_path/'state'
    data.mkdir();state.mkdir()
    for name in ('lab-a','lab-b'):
        (base/'releases'/name).mkdir(parents=True)
        (state/(name+'-prepared.json')).write_text('{}')
    (base/'current').symlink_to(base/'releases/lab-a',target_is_directory=True)
    db=data/'watchlist.sqlite3'
    with sqlite3.connect(db) as connection:
        connection.execute('CREATE TABLE items(id INTEGER PRIMARY KEY)')
        connection.execute('INSERT INTO items VALUES (1)')
    for name,value in [('BASE',base),('DATA',data),('STATE',state),('DB',db)]:
        monkeypatch.setattr(module,name,value)
    monkeypatch.setattr(module,'verify_tree',lambda path:{'release':{'release_id':path.name}})
    monkeypatch.setattr(module,'preflight',lambda rid:{'status':'passed'})
    monkeypatch.setattr(module,'install_unit',lambda candidate:None)
    monkeypatch.setattr(module,'publish',lambda candidate:None)
    calls=[]
    monkeypatch.setattr(module,'run',lambda args,**kwargs:calls.append(args) or '')
    def started(rid):
        if rid=='lab-b':
            with sqlite3.connect(db) as connection:
                connection.execute('INSERT INTO items VALUES (2)')
            raise RuntimeError('controlled failure after start')
        return {'release_id':rid}
    monkeypatch.setattr(module,'await_health',started)
    with pytest.raises(RuntimeError,match='controlled failure'):
        module.activate('lab-b','lab-a')
    assert module.current_id()=='lab-a'
    with sqlite3.connect(db) as connection:
        assert connection.execute('SELECT id FROM items ORDER BY id').fetchall()==[(1,),(2,)]
    report=json.loads(next(state.glob('*-failed.json')).read_text())
    assert report['code_recovered'] and not report['database_restored']
    calls.clear()
    with pytest.raises(RuntimeError,match='Current release changed'):
        module.activate('lab-b','lab-other')
    assert not calls
