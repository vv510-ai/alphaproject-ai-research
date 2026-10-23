"""Small code-only release format shared by packager and server verifier."""
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import os
import shutil
import tarfile
import tempfile

MAX_BYTES = 64 * 1024 * 1024
EXACT = {'pyproject.toml', 'backend/requirements.lock.txt', 'frontend/package.json',
         'frontend/package-lock.json', 'frontend/tsconfig.json', 'frontend/vite.config.ts',
         'deployment/cloud/backup.service', 'deployment/cloud/backup.timer',
         'deployment/cloud/acceptance.service', 'deployment/cloud/acceptance.timer',
         'scripts/verify_live_monitor.py', 'scripts/server_acceptance.py'}
EXACT.update('backend/tests/fixtures/'+code+'-newfqkline-probe.json' for code in ('sh600519','sz000001','sz300750','sh688981'))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def valid_id(value):
    return isinstance(value,str) and bool(re.fullmatch(r'lab-[A-Za-z0-9._-]{1,80}',value))


def allowed(name):
    p=PurePosixPath(name)
    if p.is_absolute() or '..' in p.parts or '\\' in name or str(p)!=name:
        return False
    if name in EXACT or name in ('manifest.json','release.json'):
        return True
    if p.suffix=='.py' and any(name.startswith(prefix) for prefix in ('backend/app/','backend/tests/','deployment/cloud/')):
        return '__pycache__' not in p.parts
    if name.startswith('frontend/src/'):
        return p.suffix in ('.ts','.tsx','.css')
    if name.startswith('frontend/dist/'):
        return p.suffix in ('.html','.js','.css','.json','.txt','.svg')
    return name in ('deployment/cloud/service.conf','deployment/cloud/nginx.location.conf')


def source_files(root):
    names=set(EXACT)
    for pattern in ('backend/app/**/*.py','backend/tests/**/*.py','frontend/src/**/*','deployment/cloud/*.py','deployment/cloud/*.conf'):
        for path in root.glob(pattern):
            if path.is_file():
                names.add(path.relative_to(root).as_posix())
    return sorted(name for name in names if allowed(name))


def source_digest(root):
    return sha(''.join(name+'\0'+sha((root/name).read_bytes())+'\n' for name in source_files(root)).encode())


def build(root, out):
    now=datetime.now(timezone.utc)
    release_id='lab-'+now.strftime('%Y%m%dT%H%M%S%fZ')
    files={}
    for name in source_files(root):
        path=root/name
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('Source must be a regular file inside this project')
        files[name]=path.read_bytes()
    for path in (root/'frontend/dist-cloud').rglob('*'):
        if path.is_file():
            if path.is_symlink():
                raise ValueError('Build symlinks are not accepted')
            name='frontend/dist/'+path.relative_to(root/'frontend/dist-cloud').as_posix()
            if not allowed(name):
                raise ValueError('Unexpected build output: '+name)
            files[name]=path.read_bytes()
    if b'/lab/assets/' not in files.get('frontend/dist/index.html',b''):
        raise ValueError('Build must target /lab/')
    version=re.search(r"APP_VERSION = '([^']+)'",(root/'backend/app/runtime_config.py').read_text(encoding='utf-8'))[1]
    release={'release_id':release_id,'app_version':version,'source_sha256':source_digest(root),'built_at':now.isoformat()}
    files['release.json']=json.dumps(release,sort_keys=True).encode()
    manifest={'format':1,'release':release,'files':{name:{'sha256':sha(data),'size':len(data)} for name,data in sorted(files.items())}}
    files['manifest.json']=json.dumps(manifest,sort_keys=True).encode()
    out.mkdir(parents=True,exist_ok=True)
    archive=out/(release_id+'.tar.gz')
    with tarfile.open(archive,'w:gz') as tar:
        for name,data in sorted(files.items()):
            info=tarfile.TarInfo(name); info.size=len(data); info.mode=0o644; info.mtime=0
            tar.addfile(info,io.BytesIO(data))
    validate(archive,sha(archive.read_bytes()))
    return {'archive':str(archive),'sha256':sha(archive.read_bytes()),'release':release,'files':len(files),'bytes':archive.stat().st_size}


def validate(archive,expected_hash):
    if sha(archive.read_bytes())!=expected_hash:
        raise ValueError('Archive hash mismatch')
    files={}; size=0
    with tarfile.open(archive,'r:gz') as tar:
        for member in tar:
            size+=member.size
            if not member.isfile() or member.size<0 or not allowed(member.name) or member.name in files or size>MAX_BYTES:
                raise ValueError('Unsafe or duplicate archive member')
            files[member.name]=tar.extractfile(member).read()
    manifest=json.loads(files['manifest.json'])
    if manifest.get('format')!=1 or not valid_id(manifest['release']['release_id']):
        raise ValueError('Invalid release manifest')
    if set(files)!=(set(manifest['files'])|{'manifest.json'}):
        raise ValueError('Unexpected or missing file')
    if json.loads(files['release.json'])!=manifest['release']:
        raise ValueError('Release identity mismatch')
    for name,record in manifest['files'].items():
        if len(files[name])!=record['size'] or sha(files[name])!=record['sha256']:
            raise ValueError('File hash mismatch: '+name)
    return manifest,files


def unpack(archive,expected_hash,destination):
    manifest,files=validate(archive,expected_hash)
    if destination.exists():
        verify_tree(destination,manifest)
        return manifest
    destination.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix='.unpack-',dir=destination.parent))
    candidate=stage/destination.name
    try:
        candidate.mkdir()
        for name,data in files.items():
            target=candidate/name
            target.parent.mkdir(parents=True,exist_ok=True)
            with target.open('xb') as handle:
                handle.write(data)
            target.chmod(0o644)
        verify_tree(candidate,manifest)
        # The remote caller holds the deployment lock. Existing final trees are immutable.
        if destination.exists():
            verify_tree(destination,manifest)
        else:
            os.rename(candidate,destination)
    finally:
        if stage.resolve().parent!=destination.parent.resolve():
            raise RuntimeError('Unexpected temporary directory')
        shutil.rmtree(stage)
    return manifest


def verify_tree(root,manifest=None):
    manifest=manifest or json.loads((root/'manifest.json').read_text())
    if not valid_id(manifest['release']['release_id']) or root.name!=manifest['release']['release_id']:
        raise ValueError('Release directory identity mismatch')
    for name,record in manifest['files'].items():
        path=root/name
        if not allowed(name) or path.is_symlink() or not path.is_file() or sha(path.read_bytes())!=record['sha256']:
            raise ValueError('Release file changed: '+name)
    if json.loads((root/'release.json').read_text())!=manifest['release']:
        raise ValueError('Release file identity mismatch')
    return manifest
