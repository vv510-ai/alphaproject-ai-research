"""Install a pinned, private backup job after a verified Lab release is active."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

if __package__:
    from .artifact import valid_id, verify_tree
    from .backup import list_backups
else:
    from artifact import valid_id, verify_tree
    from backup import list_backups

BASE = Path('/opt/alphaproject-lab')
DATA = Path('/var/lib/alphaproject-lab')
STATE = Path('/root/alphaproject-lab-deploy')
SYSTEMD = Path('/etc/systemd/system')
SERVICE = 'alphaproject-lab-backup.service'
TIMER = 'alphaproject-lab-backup.timer'
TOKEN = '__BACKUP_SOURCE_SHA256__'


def digest(value):
    return hashlib.sha256(value).hexdigest()


def run(args, *, check=True, timeout=30):
    result = subprocess.run([str(x) for x in args], capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f'{args[0]} failed ({result.returncode}): {result.stderr[-1200:]} {result.stdout[-1200:]}')
    return result


def atomic_write(path, data, mode=0o600):
    descriptor, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def private_directory(path):
    if path.is_symlink():
        raise RuntimeError('A managed directory must not be a symlink')
    path.mkdir(mode=0o700, parents=False, exist_ok=True)
    record = path.stat()
    if not path.is_dir() or record.st_uid != 0 or record.st_mode & 0o077:
        raise RuntimeError('A managed directory must be private and root owned')


def current_release(expected):
    if not valid_id(expected):
        raise RuntimeError('Invalid expected release')
    current = BASE / 'current'
    target = BASE / 'releases' / expected
    if not current.is_symlink() or current.resolve() != target or target.is_symlink():
        raise RuntimeError('Current release changed; operations install refused')
    verify_tree(target)
    return target


def render_units(candidate, script_hash):
    service = (candidate / 'deployment/cloud/backup.service').read_text(encoding='utf-8')
    if service.count(TOKEN) != 1 or len(script_hash) != 64:
        raise RuntimeError('Unexpected backup service template')
    return {
        SERVICE: service.replace(TOKEN, script_hash).encode(),
        TIMER: (candidate / 'deployment/cloud/backup.timer').read_bytes(),
    }


def properties(name, names):
    output = run(['systemctl', 'show', name, *[x for key in names for x in ('-p', key)]]).stdout
    return dict(line.split('=', 1) for line in output.splitlines() if '=' in line)


def install(expected):
    candidate = current_release(expected)
    script = (candidate / 'deployment/cloud/backup.py').read_bytes()
    script_hash = digest(script)
    units = render_units(candidate, script_hash)
    state_path = STATE / 'operations.json'
    if state_path.is_symlink():
        raise RuntimeError('Operations state must be a regular file')
    previous = json.loads(state_path.read_text()) if state_path.is_file() else None
    old_units = {}
    for name, content in units.items():
        path = SYSTEMD / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise RuntimeError('Unmanaged backup unit needs inspection')
        old_units[name] = path.read_bytes() if path.exists() else None
        if old_units[name] is not None and old_units[name] != content:
            if not previous or digest(old_units[name]) != previous.get('unit_sha256', {}).get(name):
                raise RuntimeError('Existing backup unit differs from the managed record')
    # Pin a standalone standard-library script so app rollbacks cannot remove it.
    private_directory(BASE / 'operations')
    pinned = BASE / 'operations' / script_hash
    private_directory(pinned)
    script_path = pinned / 'backup.py'
    if script_path.is_symlink() or (script_path.exists() and script_path.read_bytes() != script):
        raise RuntimeError('Pinned backup tool changed')
    if not script_path.exists():
        atomic_write(script_path, script, 0o400)
    private_directory(DATA / 'managed-backups')
    stage = STATE / 'operations-unit-check'
    private_directory(stage)
    for name, content in units.items():
        atomic_write(stage / name, content, 0o600)
    run(['systemd-analyze', 'verify', stage / SERVICE, stage / TIMER])
    old_enabled = run(['systemctl', 'is-enabled', '--quiet', TIMER], check=False).returncode == 0
    old_active = run(['systemctl', 'is-active', '--quiet', TIMER], check=False).returncode == 0
    existing_service = properties(SERVICE, ['ActiveState', 'DropInPaths'])
    if existing_service.get('ActiveState') not in ('inactive', 'failed') or existing_service.get('DropInPaths'):
        raise RuntimeError('Backup service is active or has an unreviewed override')
    if properties(TIMER, ['DropInPaths']).get('DropInPaths'):
        raise RuntimeError('Backup timer has an unreviewed override')
    current_release(expected)
    try:
        if old_active:
            run(['systemctl', 'stop', TIMER])
        for name, content in units.items():
            atomic_write(SYSTEMD / name, content, 0o644)
        run(['systemctl', 'daemon-reload'])
        for name in units:
            loaded = properties(name, ['FragmentPath', 'DropInPaths'])
            if loaded.get('FragmentPath') != str(SYSTEMD / name) or loaded.get('DropInPaths'):
                raise RuntimeError('Systemd loaded an unexpected backup unit or override')
        # Exercise the real sandboxed job before enabling a persistent schedule.
        previous_ids = {path.name for path in (DATA / 'managed-backups').iterdir()}
        requested_at = datetime.now(timezone.utc)
        run(['systemctl', 'start', SERVICE], timeout=150)
        service_state = properties(SERVICE, ['Result', 'ExecMainStatus', 'ExecMainStartTimestamp', 'ExecMainExitTimestamp'])
        if service_state.get('Result') != 'success' or service_state.get('ExecMainStatus') != '0':
            raise RuntimeError('The actual backup service did not succeed')
        backups = list_backups(DATA / 'managed-backups')
        if not backups['backups']:
            raise RuntimeError('The backup service produced no verified snapshot')
        latest = backups['backups'][0]
        if latest['manifest']['source']['release']['release_id'] != expected:
            raise RuntimeError('The actual snapshot is from another release')
        if latest['backup_id'] in previous_ids or datetime.fromisoformat(latest['manifest']['created_at_utc']) < requested_at:
            raise RuntimeError('The actual backup job did not produce a fresh verified snapshot')
        current_release(expected)
        run(['systemctl', 'enable', '--now', TIMER])
        timer_state = properties(TIMER, ['ActiveState', 'UnitFileState', 'NextElapseUSecRealtime', 'LastTriggerUSec'])
        if timer_state.get('ActiveState') != 'active' or timer_state.get('UnitFileState') != 'enabled':
            raise RuntimeError('The backup timer did not become enabled and active')
        result = {
            'status': 'installed_and_backup_verified', 'at': datetime.now(timezone.utc).isoformat(),
            'release_id': expected, 'script_sha256': script_hash, 'pinned_script': str(script_path),
            'unit_sha256': {name: digest(content) for name, content in units.items()},
            'service': service_state, 'timer': timer_state, 'backup': latest,
            'retention_ignored': backups['ignored'], 'offsite_backup': False,
        }
        atomic_write(state_path, json.dumps(result, ensure_ascii=False, indent=2).encode())
        return result
    except Exception as error:
        # Restore only these two units. Never undo or replace any application data.
        recovery_errors = []
        for args in (['systemctl', 'stop', TIMER], ['systemctl', 'stop', SERVICE], ['systemctl', 'disable', TIMER]):
            try:
                recovery = run(args, check=False, timeout=150)
                if recovery.returncode:
                    recovery_errors.append(f'{args}: exit {recovery.returncode}: {recovery.stderr[-500:]}')
            except Exception as recovery:
                recovery_errors.append(str(recovery))
        for name, old in old_units.items():
            try:
                if old is None:
                    (SYSTEMD / name).unlink(missing_ok=True)
                else:
                    atomic_write(SYSTEMD / name, old, 0o644)
            except Exception as recovery:
                recovery_errors.append(str(recovery))
        for args in (['systemctl', 'daemon-reload'], *([['systemctl', 'enable', TIMER]] if old_enabled else []), *([['systemctl', 'start', TIMER]] if old_active else [])):
            try:
                run(args)
            except Exception as recovery:
                recovery_errors.append(str(recovery))
        failure = {'status': 'operations_install_failed', 'error': str(error), 'recovery_errors': recovery_errors,
                   'database_restored': False, 'pinned_tool_and_backups_retained': True}
        atomic_write(STATE / 'operations-last-failure.json', json.dumps(failure, ensure_ascii=False, indent=2).encode())
        raise RuntimeError(json.dumps(failure, ensure_ascii=False)) from error


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['install'])
    parser.add_argument('--expected-current', required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError('Use the authorized administrator SSH channel')
    print(json.dumps(install(args.expected_current), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

