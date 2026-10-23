"""Install the isolated acceptance observer under remote_release's deployment lock.

Like operations.py, changes are limited to this job's two units and installer
records. Runner evidence, delivery intentions, terminal markers and all databases
are retained on every failure. This module never sends a message directly.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
import uuid

if __package__:
    from .artifact import valid_id, verify_tree
else:
    from artifact import valid_id, verify_tree

BASE = Path('/opt/alphaproject-lab')
STATE = Path('/root/alphaproject-lab-deploy')
DATA = Path('/var/lib/alphaproject-lab-acceptance')
SYSTEMD = Path('/etc/systemd/system')
SERVICE = 'alphaproject-lab-acceptance.service'
TIMER = 'alphaproject-lab-acceptance.timer'
ACCOUNT = 'alphaproject-lab'
TOKEN = '__RELEASE_ID__'
OWNER_FILE = '.installer-owner.json'
TOOL = 'alphaproject-lab-server-acceptance'
RECEIPT_KEY = 'server-acceptance-installed-v1'
SCHEDULE = 'Mon..Fri 09:35,15:35 Asia/Shanghai'
SUCCESS = 'installed_and_receipt_verified'


class InstallationFailure(RuntimeError):
    """A transaction failure whose recovery evidence has already been saved."""


def now():
    return datetime.now(timezone.utc)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def run(args, *, check=True, timeout=30):
    result = subprocess.run([str(item) for item in args], capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f'{args[0]} failed ({result.returncode}): {result.stderr[-1000:]} {result.stdout[-1000:]}')
    return result


def properties(name, names):
    output = run(['systemctl', 'show', name, *[item for key in names for item in ('-p', key)]]).stdout
    return dict(line.split('=', 1) for line in output.splitlines() if '=' in line)


def require_administrator():
    if os.name != 'posix' or os.geteuid() != 0:
        raise RuntimeError('Use the authorized administrator SSH channel and deployment lock')


def service_identity():
    import pwd
    user = pwd.getpwnam(ACCOUNT)
    if user.pw_dir != '/nonexistent' or user.pw_shell != '/usr/sbin/nologin':
        raise RuntimeError('Unexpected existing acceptance service account')
    return user.pw_uid, user.pw_gid


def checked_path(path, *, directory=False, uid=None, mode=None):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise RuntimeError('Managed paths must be absolute without parent traversal')
    for part in reversed((path, *path.parents)):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise RuntimeError('Managed paths must not contain links or reparse points')
    info = path.lstat()
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            raise RuntimeError('Expected a managed directory')
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RuntimeError('Expected a regular managed file with one link')
    if os.name == 'posix':
        if uid is not None and info.st_uid != uid:
            raise RuntimeError('Managed path ownership changed')
        if mode is not None and stat.S_IMODE(info.st_mode) != mode:
            raise RuntimeError('Managed path permissions changed')
    return info


def read_bytes(path, *, uid=0, mode=0o600, limit=2 * 1024 * 1024):
    before = checked_path(path, uid=uid, mode=mode)
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        opened = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino) or opened.st_nlink != 1:
            raise RuntimeError('Managed file identity changed while opening')
        with os.fdopen(fd, 'rb', closefd=False) as handle:
            value = handle.read(limit + 1)
        if len(value) > limit:
            raise RuntimeError('Managed record exceeds the expected size')
        return value
    finally:
        os.close(fd)


def read_json(path, *, uid=0, optional=False):
    if optional and not path.exists() and not path.is_symlink():
        return None
    try:
        value = json.loads(read_bytes(path, uid=uid))
    except (ValueError, UnicodeError) as error:
        raise RuntimeError('Managed JSON record is invalid') from error
    if not isinstance(value, dict):
        raise RuntimeError('Managed record must be a JSON object')
    return value


def private_directory(path, *, uid=0, gid=0):
    checked_path(path.parent, directory=True)
    if not path.exists() and not path.is_symlink():
        path.mkdir(mode=0o700)
        if os.name == 'posix':
            os.chown(path, uid, gid)
    checked_path(path, directory=True, uid=uid, mode=0o700)


def atomic_write(path, content, *, mode=0o600, uid=0, gid=0):
    checked_path(path.parent, directory=True)
    if path.exists() or path.is_symlink():
        checked_path(path, uid=uid, mode=mode)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(content)
            handle.flush()
            if os.name == 'posix':
                os.fchmod(handle.fileno(), mode)
                os.fchown(handle.fileno(), uid, gid)
            os.fsync(handle.fileno())
        if os.name != 'posix':
            temporary.chmod(mode)
        os.replace(temporary, path)
        if os.name == 'posix':
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path, value, *, uid=0, gid=0):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode('utf-8'), uid=uid, gid=gid)


def remove_created_installation(value, uid):
    """Undo only this attempt's configuration, never the runner's delivery ledger."""
    path = DATA / 'installation.json'
    if not path.exists() and not path.is_symlink():
        return False
    expected = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode('utf-8')
    if read_bytes(path, uid=uid) != expected:
        raise RuntimeError('Installation configuration changed again; it was preserved for inspection')
    path.unlink()
    if os.name == 'posix':
        fd = os.open(DATA, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return True


@contextmanager
def install_lock():
    """The parent deployment lock remains held; this additionally blocks two installers."""
    private_directory(STATE)
    path = STATE / 'acceptance-install.lock'
    if path.exists() or path.is_symlink():
        checked_path(path, uid=0, mode=0o600)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = checked_path(path, uid=0, mode=0o600)
        opened = os.fstat(fd)
        if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
            raise RuntimeError('Installer lock identity changed')
        if opened.st_size == 0:
            os.write(fd, b'1')
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError('Another acceptance installation is already running') from error
        yield
    finally:
        os.close(fd)


def current_release(expected):
    if not valid_id(expected):
        raise RuntimeError('Invalid expected acceptance release')
    target = BASE / 'releases' / expected
    current = BASE / 'current'
    if not current.is_symlink() or current.resolve() != target:
        raise RuntimeError('Current release changed; acceptance installation refused')
    checked_path(target, directory=True, uid=0)
    manifest = verify_tree(target)
    required = {'scripts/server_acceptance.py', 'scripts/verify_live_monitor.py',
                'deployment/cloud/acceptance.service', 'deployment/cloud/acceptance.timer',
                'backend/requirements.lock.txt'}
    if not required.issubset(manifest['files']):
        raise RuntimeError('Verified release lacks acceptance source or unit templates')
    environment = BASE / 'venvs' / digest((target / 'backend/requirements.lock.txt').read_bytes())[:20]
    if not (target / '.venv').is_symlink() or (target / '.venv').resolve() != environment:
        raise RuntimeError('Acceptance release virtual environment is not the pinned dependency environment')
    checked_path(environment, directory=True, uid=0)
    if not (environment / 'bin/python').is_file():
        raise RuntimeError('Pinned acceptance Python interpreter is missing')
    return target


def render_units(candidate, expected):
    service = (candidate / 'deployment/cloud/acceptance.service').read_text(encoding='utf-8')
    timer = (candidate / 'deployment/cloud/acceptance.timer').read_text(encoding='utf-8')
    if not valid_id(expected) or service.count(TOKEN) != 3 or TOKEN in timer:
        raise RuntimeError('Unexpected acceptance unit template')
    service = service.replace(TOKEN, expected)
    release = '/opt/alphaproject-lab/releases/' + expected
    required = {
        'Type=oneshot', 'User=' + ACCOUNT, 'Group=' + ACCOUNT,
        'WorkingDirectory=' + release, 'StateDirectory=alphaproject-lab-acceptance', 'StateDirectoryMode=0700',
        'ExecStart=' + release + '/.venv/bin/python -I -B ' + release + '/scripts/server_acceptance.py --state-dir /var/lib/alphaproject-lab-acceptance',
        'LoadCredential=feishu.json:/etc/alphaproject-lab/credentials/feishu.json', 'TimeoutStartSec=600',
        'ProtectSystem=strict', 'ConditionPathExists=!/var/lib/alphaproject-lab-acceptance/completed.json',
        'ConditionPathExists=!/var/lib/alphaproject-lab-acceptance/suspended.json',
    }
    lines = service.splitlines()
    if (not required.issubset(lines) or len([line for line in lines if line.startswith('ExecStart=')]) != 1
            or len([line for line in lines if line.startswith('LoadCredential=')]) != 1
            or any(line.startswith(('EnvironmentFile=', 'ExecStartPre=', 'ExecStartPost=')) for line in lines)):
        raise RuntimeError('Acceptance service template changes the approved execution boundary')
    expected_calendar = ['OnCalendar=Mon..Fri *-*-* 09:35:00 Asia/Shanghai',
                         'OnCalendar=Mon..Fri *-*-* 15:35:00 Asia/Shanghai']
    if ([line for line in timer.splitlines() if line.startswith('OnCalendar=')] != expected_calendar
            or not {'Persistent=true', 'AccuracySec=1s', 'Unit=' + SERVICE}.issubset(timer.splitlines())):
        raise RuntimeError('Acceptance timer template has an unexpected schedule')
    return {SERVICE: service.encode('utf-8'), TIMER: timer.encode('utf-8')}


def state_owner(uid, gid):
    private_directory(DATA, uid=uid, gid=gid)
    marker = DATA / OWNER_FILE
    if not marker.exists() and not marker.is_symlink():
        if any(DATA.iterdir()):
            raise RuntimeError('Unmarked nonempty acceptance state must not be adopted')
        write_json(marker, {'schema_version': 1, 'tool': TOOL, 'owner_id': uuid.uuid4().hex,
                            'path': str(DATA), 'created_at': now().isoformat()})
    owner = read_json(marker)
    if (owner.get('schema_version') != 1 or owner.get('tool') != TOOL or owner.get('path') != str(DATA)
            or not re.fullmatch(r'[a-f0-9]{32}', str(owner.get('owner_id', '')))):
        raise RuntimeError('Acceptance state ownership marker is invalid')
    return owner


def parsed_time(value):
    try:
        result = datetime.fromisoformat(value)
        if result.utcoffset() is None:
            raise ValueError('Missing time zone')
        return result
    except (ValueError, TypeError) as error:
        raise RuntimeError('Acceptance timestamp is invalid or has no time zone') from error


def installation_record(expected, uid):
    value = read_json(DATA / 'installation.json', uid=uid, optional=True)
    if value is None:
        return None
    timer = value.get('timer')
    required = {'schema_version', 'release_id', 'installed_at', 'timer', 'receipt_key'}
    if (set(value) not in (required, required | {'runner_release_id'})
            or type(value.get('schema_version')) is not int or value['schema_version'] != 1
            or not valid_id(value.get('release_id')) or value.get('runner_release_id', value['release_id']) != expected
            or value.get('receipt_key') != RECEIPT_KEY
            or not isinstance(timer, dict) or set(timer) != {'active', 'enabled', 'schedule'}
            or timer['active'] is not True or timer['enabled'] is not True or timer['schedule'] != SCHEDULE
            or parsed_time(value.get('installed_at')) > now() + timedelta(seconds=5)):
        raise RuntimeError('Existing installation scope differs; preserve evidence and review before changing it')
    return value


def migration_receipt(report, expected):
    deliveries = report.get('deliveries')
    if not isinstance(deliveries, list) or any(not isinstance(item, dict) for item in deliveries):
        raise RuntimeError('Acceptance run has no valid delivery ledger view')
    matches = [item for item in deliveries if isinstance(item.get('candidate_keys'), list) and RECEIPT_KEY in item['candidate_keys']]
    if (len(matches) != 1 or matches[0].get('status') != 'accepted'
            or matches[0].get('installation_release_id') != expected
            or not isinstance(matches[0].get('provider_message_id'), str) or not matches[0]['provider_message_id'].strip()):
        raise RuntimeError('Exact installation receipt was not accepted for this release; do not resend an uncertain delivery')
    return {key: matches[0][key] for key in ('candidate_keys', 'status', 'provider_message_id', 'installation_release_id')}


def installer_record():
    value = read_json(STATE / 'acceptance.json', optional=True)
    if value is None:
        return None
    hashes = value.get('unit_sha256')
    if (value.get('schema_version') != 1 or value.get('status') != SUCCESS or not valid_id(value.get('release_id'))
            or not re.fullmatch(r'[a-f0-9]{32}', str(value.get('owner_id', '')))
            or not isinstance(hashes, dict) or set(hashes) != {SERVICE, TIMER}
            or any(not isinstance(item, str) or not re.fullmatch(r'[a-f0-9]{64}', item) for item in hashes.values())):
        raise RuntimeError('Root acceptance installation record is invalid')
    installation_release = value.get('installation_release_id', value['release_id'])
    if not valid_id(installation_release):
        raise RuntimeError('Original installation identity is invalid')
    migration_receipt({'deliveries': [value.get('migration_receipt', {})]}, installation_release)
    return value


def check_unsettled(report):
    if report is None:
        return
    deliveries = report.get('deliveries', [])
    if not isinstance(deliveries, list) or any(not isinstance(item, dict) for item in deliveries):
        raise RuntimeError('Existing acceptance delivery evidence is malformed')
    if any(item.get('status') in ('intent', 'unknown', 'sending', 'in_flight', 'reserved') for item in deliveries):
        raise RuntimeError('An acceptance delivery is uncertain; its evidence is preserved without automatic retry')


def verified_upgrade_ledger(uid, installation_release, expected_receipt):
    """Read the durable sending ledger before changing a running installation."""
    ledger = read_json(DATA / 'notification-ledger.json', uid=uid)
    candidates, deliveries = ledger.get('candidates'), ledger.get('deliveries')
    if ledger.get('schema_version') != 1 or not isinstance(candidates, dict) or not isinstance(deliveries, dict):
        raise RuntimeError('Existing acceptance ledger is malformed; preserve it before upgrading')
    check_unsettled({'deliveries': list(deliveries.values())})
    receipt = migration_receipt({'deliveries': list(deliveries.values())}, installation_release)
    candidate = candidates.get(RECEIPT_KEY, {})
    delivery = deliveries.get(candidate.get('delivery_id')) if isinstance(candidate, dict) else None
    if (receipt != expected_receipt or not isinstance(candidate, dict) or candidate.get('status') != 'accepted'
            or candidate.get('kind') != 'installation_completed' or not isinstance(delivery, dict)
            or migration_receipt({'deliveries': [delivery]}, installation_release) != receipt):
        raise RuntimeError('Original installation ledger link differs from the verified receipt')
    return receipt


def loaded_units():
    for name in (SERVICE, TIMER):
        loaded = properties(name, ['FragmentPath', 'DropInPaths', 'LoadState', 'Transient'])
        if (loaded.get('FragmentPath') != str(SYSTEMD / name) or loaded.get('DropInPaths')
                or loaded.get('LoadState') != 'loaded' or loaded.get('Transient') == 'yes'):
            raise RuntimeError('Systemd loaded an unexpected acceptance unit or override')


def timer_state():
    value = properties(TIMER, ['ActiveState', 'UnitFileState', 'NextElapseUSecRealtime', 'LastTriggerUSec'])
    if value.get('ActiveState') != 'active' or value.get('UnitFileState') != 'enabled':
        raise RuntimeError('Acceptance timer is not both active and enabled')
    return value


def wait_for_idle():
    # A persistent timer may start a missed run as soon as it is enabled. Wait for
    # that bounded run before requesting the fresh, explicit installation check.
    deadline = time.monotonic() + 630
    while properties(SERVICE, ['ActiveState']).get('ActiveState') not in ('inactive', 'failed'):
        if time.monotonic() >= deadline:
            raise RuntimeError('Acceptance service did not finish its bounded catch-up run')
        time.sleep(1)


def start_and_verify(expected, uid, *, receipt_required=False, installation_release=None):
    path = DATA / 'last-run.json'
    before = read_bytes(path, uid=uid) if path.exists() or path.is_symlink() else None
    requested_at = now()
    run(['systemctl', 'start', SERVICE], timeout=630)
    service = properties(SERVICE, ['Result', 'ExecMainStatus', 'ExecMainStartTimestamp', 'ExecMainExitTimestamp', 'InvocationID'])
    if service.get('Result') != 'success' or service.get('ExecMainStatus') != '0':
        raise RuntimeError('The actual acceptance service did not finish successfully')
    raw = read_bytes(path, uid=uid)
    try:
        report = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise RuntimeError('Actual acceptance run produced invalid JSON') from error
    if not isinstance(report, dict) or raw == before:
        raise RuntimeError('Acceptance service produced no fresh last-run record')
    started, completed = parsed_time(report.get('started_at')), parsed_time(report.get('completed_at'))
    if started < requested_at or completed < started or completed > now() + timedelta(seconds=5):
        raise RuntimeError('Acceptance last-run timestamps do not prove this requested service execution')
    check_unsettled(report)
    if report.get('status') != 'ok' or report.get('release_id') != expected or not isinstance(report.get('probe'), dict):
        raise RuntimeError('Acceptance run is unsuccessful or belongs to another release')
    if receipt_required:
        migration_receipt(report, installation_release or expected)
    return service, report


def fresh_terminal(expected, uid, since, installation_release):
    """A successful catch-up may finish before the explicit repeat can start."""
    markers = [name for name in ('completed', 'suspended') if (DATA / (name + '.json')).exists()]
    if not markers:
        return None
    if len(markers) != 1:
        raise RuntimeError('Conflicting terminal evidence appeared during upgrade')
    marker = read_json(DATA / (markers[0] + '.json'), uid=uid)
    report = read_json(DATA / 'last-run.json', uid=uid)
    if (marker.get('schema_version') != 1 or report.get('schema_version') != 1
            or marker.get('release_id') != expected or marker.get('terminal') != markers[0]
            or report.get('release_id') != expected or report.get('terminal') != markers[0]
            or report.get('status') != 'ok' or not isinstance(report.get('probe'), dict)
            or marker.get('run_id') != report.get('run_id') or not marker.get('run_id')
            or marker.get('completed_at') != report.get('completed_at')
            or not since <= parsed_time(report.get('started_at')) <= parsed_time(report.get('completed_at')) <= now() + timedelta(seconds=5)):
        raise RuntimeError('Terminal evidence does not belong to this upgraded execution')
    check_unsettled(report)
    migration_receipt(report, installation_release)
    service = properties(SERVICE, ['Result', 'ExecMainStatus', 'InvocationID'])
    if service.get('Result') != 'success' or service.get('ExecMainStatus') != '0':
        raise RuntimeError('Terminal record has no successful service completion')
    return service, report


def summary(report):
    if not isinstance(report, dict):
        return None
    return {key: report.get(key) for key in ('status', 'release_id', 'started_at', 'completed_at', 'terminal', 'terminal_pending', 'deliveries')}


def _install(expected, expected_previous=None):
    candidate = current_release(expected)
    units = render_units(candidate, expected)
    uid, gid = service_identity()
    record_path = STATE / 'acceptance.json'
    previous = installer_record()
    original_installer_record = read_bytes(record_path) if previous else None
    if expected_previous is not None:
        if (not valid_id(expected_previous) or previous is None
                or not (previous['release_id'] == expected_previous
                        or (previous['release_id'] == expected and previous.get('updated_from') == expected_previous))):
            raise RuntimeError('Expected previous observer does not match the managed installation')
    elif previous and previous['release_id'] != expected:
        raise RuntimeError('Existing installation scope differs; explicitly name the previous observer to upgrade')
    old_units = {}
    for name in units:
        path = SYSTEMD / name
        old = read_bytes(path, mode=0o644) if path.exists() or path.is_symlink() else None
        old_units[name] = old
        if old is not None and (not previous or previous.get('unit_sha256', {}).get(name) != digest(old)):
            raise RuntimeError('Existing acceptance unit differs from its managed record')
    for name in (SERVICE, TIMER):
        view = properties(name, ['ActiveState', 'DropInPaths', 'FragmentPath'])
        if view.get('DropInPaths'):
            raise RuntimeError('Acceptance unit has an unreviewed override')
        if view.get('FragmentPath') and view['FragmentPath'] != str(SYSTEMD / name):
            raise RuntimeError('Acceptance unit comes from an unmanaged location')
        if name == SERVICE and view.get('ActiveState') not in ('inactive', 'failed'):
            raise RuntimeError('An acceptance run is active; do not replace its pinned code')
    owner = state_owner(uid, gid)
    if previous and previous.get('owner_id') != owner['owner_id']:
        raise RuntimeError('Installer record and acceptance state have different owners')
    terminal = [name for name in ('completed', 'suspended') if (DATA / (name + '.json')).exists() or (DATA / (name + '.json')).is_symlink()]
    if terminal:
        if len(terminal) != 1:
            raise RuntimeError('Conflicting acceptance terminal markers require inspection')
        read_json(DATA / (terminal[0] + '.json'), uid=uid)
        if not previous or previous.get('status') != SUCCESS:
            raise RuntimeError('Existing terminal state has no verified installation; it was preserved')
        return {**previous, 'status': 'already_terminal', 'terminal': terminal[0], 'requested_release': expected, 'unchanged': True}
    installation = installation_record(previous['release_id'] if previous else expected, uid)
    original_configuration = read_bytes(DATA / 'installation.json', uid=uid) if installation else None
    installation_release = installation['release_id'] if installation else expected
    if previous and installation and installation_release != previous.get('installation_release_id', previous['release_id']):
        raise RuntimeError('Original installation identity conflicts with its verified receipt')
    if installation is not None and previous is None:
        raise RuntimeError('Orphan installation configuration requires inspection; do not announce migration from it')
    last_run = read_json(DATA / 'last-run.json', uid=uid, optional=True)
    check_unsettled(last_run)
    if previous and previous['release_id'] != expected:
        verified_upgrade_ledger(uid, installation_release, previous['migration_receipt'])
    if (previous and previous.get('status') == SUCCESS and previous.get('release_id') == expected
            and all(old_units[name] == content for name, content in units.items())):
        if installation is None:
            raise RuntimeError('Verified installation record is missing; runner evidence was preserved')
        migration_receipt({'deliveries': [previous.get('migration_receipt', {})]}, installation_release)
        loaded_units()
        return {**previous, 'status': 'already_installed', 'timer': timer_state(), 'unchanged': True}
    old_enabled = run(['systemctl', 'is-enabled', '--quiet', TIMER], check=False).returncode == 0
    old_active = run(['systemctl', 'is-active', '--quiet', TIMER], check=False).returncode == 0
    stage = STATE / 'acceptance-unit-check'
    private_directory(stage)
    for name, content in units.items():
        atomic_write(stage / name, content)
    run(['systemd-analyze', 'verify', stage / SERVICE, stage / TIMER])
    current_release(expected)
    mutated = started = enabled = created_installation = updated_installation = record_attempted = False
    transaction_started = now()
    latest = None
    try:
        if old_active:
            run(['systemctl', 'stop', TIMER])
        if properties(SERVICE, ['ActiveState']).get('ActiveState') not in ('inactive', 'failed'):
            raise RuntimeError('An acceptance run started while installing; leave its evidence intact')
        mutated = True
        if installation is not None and installation.get('runner_release_id', installation['release_id']) != expected:
            installation = {**installation, 'runner_release_id': expected}
            updated_installation = True
            write_json(DATA / 'installation.json', installation, uid=uid, gid=gid)
        for name, content in units.items():
            atomic_write(SYSTEMD / name, content, mode=0o644)
        run(['systemctl', 'daemon-reload'])
        loaded_units()
        probe_service = None
        if installation is None:
            started = True
            probe_service, latest = start_and_verify(expected, uid)
            current_release(expected)
        run(['systemctl', 'enable', '--now', TIMER])
        enabled = True
        timer = timer_state()
        if installation is None:
            installation = {'schema_version': 1, 'release_id': expected, 'installed_at': now().isoformat(),
                            'timer': {'active': True, 'enabled': True, 'schedule': SCHEDULE}, 'receipt_key': RECEIPT_KEY}
            created_installation = True
            write_json(DATA / 'installation.json', installation, uid=uid, gid=gid)
        wait_for_idle()
        started = True
        terminal_result = fresh_terminal(expected, uid, transaction_started, installation_release)
        receipt_service, latest = terminal_result or start_and_verify(expected, uid, receipt_required=True, installation_release=installation_release)
        if probe_service is None:
            probe_service = receipt_service
            terminal_result = fresh_terminal(expected, uid, transaction_started, installation_release)
            receipt_service, latest = terminal_result or start_and_verify(expected, uid, receipt_required=True, installation_release=installation_release)
        current_release(expected)
        loaded_units()
        timer = timer_state()
        result = {'schema_version': 1, 'status': SUCCESS, 'release_id': expected,
                  'installation_release_id': installation_release,
                  'owner_id': owner['owner_id'], 'installed_at': installation['installed_at'], 'verified_at': now().isoformat(),
                  'runner_sha256': digest((candidate / 'scripts/server_acceptance.py').read_bytes()),
                  'unit_sha256': {name: digest(content) for name, content in units.items()},
                  'probe_service': probe_service, 'receipt_service': receipt_service, 'timer': timer,
                  'migration_receipt': migration_receipt(latest, installation_release), 'last_run': summary(latest),
                  'state_directory': str(DATA), 'database_changed': False, 'recipient_confirmation_separate': True}
        if previous and previous['release_id'] != expected:
            result['updated_from'] = previous['release_id']
        elif previous and previous.get('updated_from'):
            result['updated_from'] = previous['updated_from']
        record_attempted = True
        write_json(record_path, result)
        return result
    except Exception as error:
        recovery_errors = []

        def recover(args, timeout=30):
            try:
                result = run(args, check=False, timeout=timeout)
                if result.returncode:
                    recovery_errors.append(f'{args}: exit {result.returncode}: {result.stderr[-500:]}')
            except Exception as failure:
                recovery_errors.append(str(failure))

        recover(['systemctl', 'stop', TIMER])
        if started or enabled:
            recover(['systemctl', 'stop', SERVICE], timeout=45)
        recover(['systemctl', 'disable', TIMER])
        installation_removed = False
        if created_installation:
            try:
                installation_removed = remove_created_installation(installation, uid)
            except Exception as failure:
                recovery_errors.append(str(failure))
        installation_restored = False
        if updated_installation:
            try:
                expected_bytes = json.dumps(installation, ensure_ascii=False, indent=2, sort_keys=True).encode('utf-8')
                present = read_bytes(DATA / 'installation.json', uid=uid)
                if present not in (original_configuration, expected_bytes):
                    raise RuntimeError('Installation configuration changed again; refusing a blind rollback')
                if present != original_configuration:
                    atomic_write(DATA / 'installation.json', original_configuration, uid=uid, gid=gid)
                installation_restored = True
            except Exception as failure:
                recovery_errors.append(str(failure))
        if record_attempted:
            try:
                expected_bytes = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True).encode('utf-8')
                present = read_bytes(record_path) if record_path.exists() else None
                if present not in (original_installer_record, expected_bytes):
                    raise RuntimeError('Installer record changed again; refusing a blind rollback')
                if original_installer_record is None:
                    record_path.unlink(missing_ok=True)
                elif present != original_installer_record:
                    atomic_write(record_path, original_installer_record)
            except Exception as failure:
                recovery_errors.append(str(failure))
        if mutated:
            for name, old in old_units.items():
                path = SYSTEMD / name
                try:
                    present = read_bytes(path, mode=0o644) if path.exists() or path.is_symlink() else None
                    if present not in (None, old, units[name]):
                        raise RuntimeError('Acceptance unit changed again; refusing a blind rollback')
                    if old is None:
                        path.unlink(missing_ok=True)
                    else:
                        atomic_write(path, old, mode=0o644)
                except Exception as failure:
                    recovery_errors.append(str(failure))
        recover(['systemctl', 'daemon-reload'])
        if old_enabled:
            recover(['systemctl', 'enable', TIMER])
        if old_active:
            recover(['systemctl', 'start', TIMER])
        try:
            observed = read_json(DATA / 'last-run.json', uid=uid, optional=True)
        except (OSError, RuntimeError):
            observed = None
        failure = {'schema_version': 1, 'status': 'acceptance_install_failed', 'release_id': expected,
                   'failed_at': now().isoformat(), 'error': str(error), 'recovery_errors': recovery_errors,
                   'attempted_unit_sha256': {name: digest(content) for name, content in units.items()},
                   'last_verified_run': summary(latest), 'last_observed_run': summary(observed),
                   'runner_state_and_delivery_evidence_preserved': True,
                   'created_installation_record_removed': installation_removed,
                   'previous_installation_binding_restored': installation_restored,
                   'message_delivery_may_have_occurred': started, 'database_restored': False}
        write_json(STATE / 'acceptance-last-failure.json', failure)
        raise InstallationFailure(json.dumps(failure, ensure_ascii=False)) from error


def install(expected_release, expected_previous=None):
    require_administrator()
    with install_lock():
        try:
            return _install(expected_release, expected_previous)
        except InstallationFailure:
            raise
        except Exception as error:
            # Preflight failures also have a root-only receipt, without touching
            # any existing runner file or stopping an unowned/running unit.
            failure = {'schema_version': 1, 'status': 'acceptance_install_preflight_failed',
                       'release_id': expected_release, 'failed_at': now().isoformat(), 'error': str(error),
                       'units_changed': False, 'runner_evidence_preserved': True, 'database_restored': False}
            write_json(STATE / 'acceptance-last-failure.json', failure)
            raise RuntimeError(json.dumps(failure, ensure_ascii=False)) from error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install'])
    parser.add_argument('--expected-current', required=True)
    parser.add_argument('--expected-previous')
    args = parser.parse_args(argv)
    try:
        result = install(args.expected_current, args.expected_previous)
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        print(json.dumps({'status': 'acceptance_install_failed', 'error': str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
