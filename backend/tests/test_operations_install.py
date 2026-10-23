"""Installer boundaries: unknown units, failed real job, and persistent tool pinning."""
import json
from datetime import datetime, timezone
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from deployment.cloud import operations as ops


@pytest.fixture
def setup(tmp_path, monkeypatch):
    for name in ('BASE', 'DATA', 'STATE', 'SYSTEMD'):
        target = tmp_path / name
        target.mkdir()
        monkeypatch.setattr(ops, name, target)
    candidate = tmp_path / 'candidate'
    source = Path(__file__).resolve().parents[2] / 'deployment/cloud'
    target = candidate / 'deployment/cloud'
    target.mkdir(parents=True)
    for name in ('backup.py', 'backup.service', 'backup.timer'):
        (target / name).write_bytes((source / name).read_bytes())
    monkeypatch.setattr(ops, 'current_release', lambda expected: candidate)
    monkeypatch.setattr(ops, 'private_directory', lambda path: path.mkdir(mode=0o700, exist_ok=True))
    monkeypatch.setattr(ops, 'list_backups', lambda root: {
        'backups': [{'manifest': {'source': {'release': {'release_id': 'lab-test'}}, 'created_at_utc': datetime.now(timezone.utc).isoformat()}, 'backup_id': 'complete'}], 'ignored': []})
    commands = []
    def run(args, *, check=True, timeout=30):
        commands.append([str(x) for x in args])
        code, output = 0, ''
        if 'is-enabled' in args or 'is-active' in args:
            code = 1
        if 'show' in args:
            output = ('Result=success\nExecMainStatus=0\nActiveState=inactive\n' if ops.SERVICE in args
                      else 'ActiveState=active\nUnitFileState=enabled\nNextElapseUSecRealtime=tomorrow\n')
            name = ops.SERVICE if ops.SERVICE in args else ops.TIMER
            output += 'DropInPaths=\nFragmentPath=' + str(ops.SYSTEMD / name) + '\n'
        return CompletedProcess(args, code, output, '')
    monkeypatch.setattr(ops, 'run', run)
    return candidate, commands, run


def test_installs_pinned_tool_then_verifies_job_before_enabling_timer(setup):
    _, commands, _ = setup
    result = ops.install('lab-test')
    assert result['status'] == 'installed_and_backup_verified'
    assert Path(result['pinned_script']).is_file()
    unit = (ops.SYSTEMD / ops.SERVICE).read_text()
    assert '/current/deployment/cloud/backup.py' not in unit
    assert ops.TOKEN not in unit
    assert result['script_sha256'] in unit
    assert commands.index(['systemctl', 'start', ops.SERVICE]) < commands.index(['systemctl', 'enable', '--now', ops.TIMER])
    assert json.loads((ops.STATE / 'operations.json').read_text())['backup']['backup_id'] == 'complete'


def test_existing_unknown_unit_is_never_overwritten(setup):
    _, commands, _ = setup
    path = ops.SYSTEMD / ops.SERVICE
    path.write_text('somebody else owns this')
    with pytest.raises(RuntimeError, match='differs'):
        ops.install('lab-test')
    assert path.read_text() == 'somebody else owns this'
    assert commands == []


def test_failed_job_removes_only_new_units_and_retains_data(setup, monkeypatch):
    _, commands, original = setup
    sentinel = ops.DATA / 'watchlist.sqlite3'
    sentinel.write_bytes(b'original database sentinel')
    def fail(args, **kwargs):
        if args == ['systemctl', 'start', ops.SERVICE]:
            raise RuntimeError('real job failed')
        return original(args, **kwargs)
    monkeypatch.setattr(ops, 'run', fail)
    with pytest.raises(RuntimeError, match='real job failed'):
        ops.install('lab-test')
    assert not (ops.SYSTEMD / ops.SERVICE).exists()
    assert not (ops.SYSTEMD / ops.TIMER).exists()
    assert sentinel.read_bytes() == b'original database sentinel'
    assert ['systemctl', 'enable', '--now', ops.TIMER] not in commands
    failure = json.loads((ops.STATE / 'operations-last-failure.json').read_text())
    assert failure['recovery_errors'] == [] and failure['database_restored'] is False


def test_unverified_or_different_release_snapshot_prevents_timer(setup, monkeypatch):
    _, commands, _ = setup
    monkeypatch.setattr(ops, 'list_backups', lambda root: {
        'backups': [{'manifest': {'source': {'release': {'release_id': 'lab-other'}}}}], 'ignored': []})
    with pytest.raises(RuntimeError, match='another release'):
        ops.install('lab-test')
    assert ['systemctl', 'enable', '--now', ops.TIMER] not in commands


def test_bad_template_is_rejected_before_writing_units(setup):
    candidate, commands, _ = setup
    (candidate / 'deployment/cloud/backup.service').write_text('ExecStart=unknown')
    with pytest.raises(RuntimeError, match='template'):
        ops.install('lab-test')
    assert list(ops.SYSTEMD.iterdir()) == [] and commands == []


def test_previous_managed_unit_is_restored_if_replacement_fails(setup, monkeypatch):
    _, _, original = setup
    old = {ops.SERVICE: b'previous known service', ops.TIMER: b'previous known timer'}
    for name, content in old.items():
        (ops.SYSTEMD / name).write_bytes(content)
    (ops.STATE / 'operations.json').write_text(json.dumps({'unit_sha256': {name: ops.digest(content) for name, content in old.items()}}))
    def fail(args, **kwargs):
        if args == ['systemctl', 'start', ops.SERVICE]:
            raise RuntimeError('failed upgrade')
        return original(args, **kwargs)
    monkeypatch.setattr(ops, 'run', fail)
    with pytest.raises(RuntimeError, match='failed upgrade'):
        ops.install('lab-test')
    for name, content in old.items():
        assert (ops.SYSTEMD / name).read_bytes() == content

