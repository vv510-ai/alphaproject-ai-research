"""Handoff failure/retry checks with temporary SQLite and fake host commands.

No production paths, subprocesses, HTTP, credentials, models or senders run.
The SQLite backup, journal files, retirement guard, and switch logic are real.
"""
from contextlib import closing
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import sys
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def handoff(tmp_path, monkeypatch):
    for name in ('fcntl', 'pwd'):
        if importlib.util.find_spec(name) is None:
            monkeypatch.setitem(sys.modules, name, ModuleType(name))
    from deployment.cloud import remote_release as r, unify as u
    from backend.app.legacy_archive import TABLES

    # The target is Linux. Preserve its newline behavior when testing on Windows.
    write_text = Path.write_text
    monkeypatch.setattr(Path, 'write_text', lambda self, data, encoding=None, errors=None, newline=None:
                        write_text(self, data, encoding=encoding or 'utf-8', errors=errors, newline='\n' if newline is None else newline))
    state, archives, candidate = tmp_path / 'state', tmp_path / 'archives', tmp_path / 'lab-new'
    for path in (state, archives, candidate):
        path.mkdir()
    database, old_database, site = tmp_path / 'lab.sqlite3', tmp_path / 'old.sqlite3', tmp_path / 'site.conf'
    with closing(sqlite3.connect(database)) as db, db:
        db.execute('CREATE TABLE records(id INTEGER PRIMARY KEY)')
        db.execute('INSERT INTO records VALUES(1)')
    with closing(sqlite3.connect(old_database)) as db, db:
        for table in TABLES:
            db.execute(f'CREATE TABLE {table}(code TEXT, deleted_at TEXT)')
    before = 'server {\n    auth_basic off;\n' + r.INCLUDE + '    location / {\n        proxy_pass http://127.0.0.1:18000;\n    }\n}\n'
    site.write_text(before)
    for name, value in {'STATE': state, 'SITE': site, 'DB': database,
                        'SITE_HASH': r.sha(before.replace(r.INCLUDE, '').encode())}.items():
        monkeypatch.setattr(r, name, value)
    for name, value in {'ROOT': candidate, 'OLD_DB': old_database, 'ARCHIVES': archives,
                        'MARKER': state / 'unified-site.json', 'PENDING': state / 'unification-pending.json',
                        'RETIRED': tmp_path / 'old-data/RETIRED-UNIFIED',
                        'RETIRE_UNIT': tmp_path / 'systemd/90-unified-retired.conf'}.items():
        monkeypatch.setattr(u, name, value)
    r.save_json(state / 'latest-unification-rehearsal.json', {'status': 'rehearsal_passed', 'release': 'lab-new'})
    services = {'alphaproject': {'active': True, 'enabled': True}, r.SERVICE: {'active': True, 'enabled': True}}
    fixture = SimpleNamespace(u=u, r=r, calls=[], services=services, fault=None, import_calls=0, commits=0,
                              archive_calls=0, restore_calls=[], restore_error=False, tick=0, before=before,
                              real_restore=u.restore_legacy_rules)
    fixture.settings = {'overrides': {'volume_enabled': True, 'volume_ratio': 4.25}, 'effective': {}}

    def stamp():
        fixture.tick += 1
        return f'tick-{fixture.tick:03}'

    def run(args, **kwargs):
        args = [str(value) for value in args]
        fixture.calls.append(args)
        if args == ['nginx', '-t'] and fixture.fault == 'nginx':
            fixture.fault = None
            raise RuntimeError('injected final nginx failure')
        if args[:2] == ['systemctl', 'daemon-reload']:
            return ''
        if args[0] == 'systemctl':
            action, service = args[1:3]
            if service == 'nginx':
                return ''
            if action == 'show':
                return '123' if services[service]['active'] else '0'
            if action in ('start', 'restart'):
                if service == 'alphaproject' and u.RETIRE_UNIT.exists() and u.RETIRED.exists():
                    assert u.RETIRE_UNIT.read_text() == u.RETIRE_CONTENT
                    services[service]['active'] = False  # Simulate the systemd condition.
                else:
                    services[service]['active'] = True
            elif action == 'stop':
                services[service]['active'] = False
            elif action in ('enable', 'disable'):
                services[service]['enabled'] = action == 'enable'
            else:
                raise AssertionError(args)
        return ''

    def process(args, **kwargs):
        assert args[0] == 'systemctl' and args[1] in ('is-active', 'is-enabled'), args
        field = 'active' if args[1] == 'is-active' else 'enabled'
        return SimpleNamespace(returncode=0 if services[args[-1]][field] else 1)

    def archive(folder, content):
        fixture.archive_calls += 1
        assert not u.RETIRED.exists()  # Archive the old unit before installing its guard.
        manifest = folder / 'archive-hashes.json'
        r.save_json(manifest, {path.name: u.digest(path) for path in folder.iterdir() if path.is_file()})
        return {'manifest': str(manifest), 'manifest_sha256': u.digest(manifest)}

    def backup(source, target):
        # The atomic hard-link publication has separate Linux release tests;
        # these control-flow tests also run in Windows sandboxes without links.
        assert not target.exists()
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src, closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
            assert dst.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'

    def imported(*args):
        fixture.import_calls += 1
        assert not services['alphaproject']['active'] and not services['alphaproject']['enabled']
        assert u.RETIRED.exists() and u.RETIRE_UNIT.exists()
        if fixture.fault == 'before_import':
            fixture.fault = None
            raise RuntimeError('injected failure before import')
        with closing(sqlite3.connect(database)) as db, db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='legacy_migrations'").fetchone()
            if not exists:
                db.execute('CREATE TABLE legacy_migrations(id INTEGER PRIMARY KEY)')
                db.execute('INSERT INTO legacy_migrations VALUES(1)')
                fixture.commits += 1
        if fixture.fault == 'after_commit_crash':
            fixture.fault = None
            raise SystemExit('injected abrupt process exit after commit')
        return {'summary': {'replayed': bool(exists)}}

    def restore(settings):
        fixture.restore_calls.append(settings)
        if fixture.restore_error:
            raise RuntimeError('injected original-rule restoration failure')

    monkeypatch.setattr(r, 'run', run)
    monkeypatch.setattr(r, 'backup', backup)
    monkeypatch.setattr(r, 'stamp', stamp)
    monkeypatch.setattr(r, 'current_id', lambda: 'lab-new')
    monkeypatch.setattr(r, 'release_path', lambda release: candidate)
    monkeypatch.setattr(r, 'await_health', lambda release: {'release_id': release})
    monkeypatch.setattr(u.subprocess, 'run', process)
    monkeypatch.setattr(u, 'validate_nginx_copy', lambda folder, content: None)
    monkeypatch.setattr(u, 'legacy_settings', lambda: fixture.settings)
    monkeypatch.setattr(u, 'identities_for', lambda source: {})
    monkeypatch.setattr(u, 'archive_original', archive)
    monkeypatch.setattr(u, 'import_database', imported)
    monkeypatch.setattr(u, 'restore_legacy_rules', restore)
    monkeypatch.setattr(u.urllib.request, 'urlopen', lambda *args, **kwargs: pytest.fail('External HTTP forbidden'))
    return fixture


def read_ids(path):
    with closing(sqlite3.connect(path)) as db:
        return [row[0] for row in db.execute('SELECT id FROM records ORDER BY id')]


def test_committed_nginx_failure_keeps_one_owner_and_resumes_without_reimport_or_data_loss(handoff):
    f, u, r = handoff, handoff.u, handoff.r
    f.fault = 'nginx'
    with pytest.raises(RuntimeError, match='final nginx failure'):
        u.switch('lab-new')
    pending = json.loads(u.PENDING.read_text())
    assert pending['phase'] == 'imported_needs_repair' and not u.MARKER.exists()
    assert f.services['alphaproject'] == {'active': False, 'enabled': False}
    assert f.services[r.SERVICE]['active'] and f.commits == 1
    assert u.RETIRED.exists() and u.RETIRE_UNIT.exists()
    r.run(['systemctl', 'start', 'alphaproject'])  # Legacy deploy tools explicitly start it.
    assert not f.services['alphaproject']['active']
    with closing(sqlite3.connect(r.DB)) as db, db:
        db.execute('INSERT INTO records VALUES(2)')
    result = u.switch('lab-new')
    assert result['status'] == 'unified' and not u.PENDING.exists() and u.MARKER.exists()
    assert f.import_calls == 2 and f.commits == 1 and f.archive_calls == 1
    assert read_ids(r.DB) == [1, 2] and not f.restore_calls
    before_calls = list(f.calls)
    assert u.switch('lab-new')['replayed']
    assert f.calls == before_calls


def test_abrupt_exit_after_commit_recovers_from_durable_archive_phase(handoff):
    f, u = handoff, handoff.u
    f.fault = 'after_commit_crash'
    with pytest.raises(SystemExit):
        u.switch('lab-new')
    assert json.loads(u.PENDING.read_text())['phase'] == 'archived'
    assert not f.services['alphaproject']['active'] and not f.services['alphaproject']['enabled']
    assert u.RETIRED.exists() and f.commits == 1
    assert u.switch('lab-new')['status'] == 'unified'
    assert f.commits == 1 and f.archive_calls == 1


@pytest.mark.parametrize('original_enabled', [True, False])
def test_failure_before_commit_restores_original_enable_state_and_runtime_rules(handoff, original_enabled):
    f, u = handoff, handoff.u
    f.services['alphaproject']['enabled'] = original_enabled
    f.fault = 'before_import'
    with pytest.raises(RuntimeError, match='before import'):
        u.switch('lab-new')
    assert f.services['alphaproject'] == {'active': True, 'enabled': original_enabled}
    assert f.restore_calls == [f.settings] and not u.PENDING.exists()
    assert not u.RETIRED.exists() and not u.RETIRE_UNIT.exists() and not u.MARKER.exists()
    assert read_ids(f.r.DB) == [1] and f.commits == 0


def test_changed_private_archive_blocks_retry_while_retired_sender_stays_stopped(handoff):
    f, u = handoff, handoff.u
    f.fault = 'nginx'
    with pytest.raises(RuntimeError):
        u.switch('lab-new')
    folder = Path(json.loads(u.PENDING.read_text())['directory'])
    (folder / 'original.sqlite3').write_bytes(b'changed archive')
    with pytest.raises((RuntimeError, sqlite3.DatabaseError)):
        u.switch('lab-new')
    assert not f.services['alphaproject']['active'] and not f.services['alphaproject']['enabled']
    assert u.PENDING.exists() and u.RETIRED.exists() and f.commits == 1


def test_failed_runtime_rule_restore_retains_pending_record(handoff):
    f, u = handoff, handoff.u
    f.fault, f.restore_error = 'before_import', True
    with pytest.raises(RuntimeError, match='original-rule restoration failure'):
        u.switch('lab-new')
    assert u.PENDING.exists() and not u.MARKER.exists() and f.commits == 0
    assert f.restore_calls == [f.settings]


@pytest.mark.parametrize('proof', ['pending', 'complete', 'database'])
def test_old_candidate_rejected_by_each_independent_unification_evidence(handoff, monkeypatch, proof):
    f, u, r = handoff, handoff.u, handoff.r
    if proof == 'pending':
        r.save_json(u.PENDING, {})
    elif proof == 'complete':
        r.save_json(u.MARKER, {})
    else:
        with closing(sqlite3.connect(r.DB)) as db, db:
            db.execute('CREATE TABLE legacy_migrations(id INTEGER PRIMARY KEY)')
            db.execute('INSERT INTO legacy_migrations VALUES(1)')
    monkeypatch.setattr(r, 'verify_tree', lambda path: {'release': {'release_id': 'lab-old'}})
    monkeypatch.setattr(r, 'preflight', lambda *args: pytest.fail('Unsafe old candidate reached preflight'))
    with pytest.raises(RuntimeError, match='imported budget and archive'):
        r.activate('lab-old', 'lab-new')
    assert f.calls == []


def test_empty_migration_schema_does_not_falsely_mark_data_imported(handoff):
    r = handoff.r
    with closing(sqlite3.connect(r.DB)) as db, db:
        db.execute('CREATE TABLE legacy_migrations(id INTEGER PRIMARY KEY)')
    assert r.requires_unified_release() is False


def test_unknown_retirement_guard_is_never_overwritten_or_removed(handoff):
    f, u = handoff, handoff.u
    u.RETIRE_UNIT.parent.mkdir()
    u.RETIRE_UNIT.write_text('operator-owned override')
    folder = u.ARCHIVES / 'fixture'
    folder.mkdir()
    for install in (True, False):
        with pytest.raises(RuntimeError, match='Unknown original-service retirement override'):
            u.retirement_guard('lab-new', folder, install=install)
    assert u.RETIRE_UNIT.read_text() == 'operator-owned override' and f.calls == []


def test_original_memory_rules_restore_requires_readback_match(handoff, monkeypatch):
    # Call the real helper, not the switch fixture's restoration spy.
    import time
    import urllib.request
    requests, readbacks = [], []
    expected = handoff.settings['overrides']

    def request(value, **kwargs):
        requests.append(value)
        assert value.get_method() == 'PATCH' and json.loads(value.data) == expected
        return io.BytesIO(json.dumps(expected).encode())

    def fetch(url):
        readbacks.append(url)
        return expected if len(readbacks) > 1 else {'volume_ratio': 3}

    monkeypatch.setattr(urllib.request, 'urlopen', request)
    monkeypatch.setattr(time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(handoff.u, 'fetch', fetch)
    handoff.real_restore(handoff.settings)
    assert len(requests) == len(readbacks) == 2
