"""Installer transaction tests: synthetic systemd/receipts, no network or live state."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest

from deployment.cloud import acceptance as install

RID = 'lab-acceptance-fixture'


def accepted(release=RID):
    return {'candidate_keys': [install.RECEIPT_KEY], 'status': 'accepted',
            'provider_message_id': 'om-synthetic-fixture', 'installation_release_id': release}


class SystemdFixture:
    def __init__(self):
        self.commands = []
        self.runs = 0
        self.modes = []
        self.active = self.enabled = False
        self.override = ''
        self.fragment_override = None
        self.result = 'success'
        self.exit_status = '0'
        self.catch_up_polls = 0
        self.catch_up_on_enable = False
        self.active_at_setup = []

    def execute(self, args, *, check=True, timeout=30):
        args = [str(item) for item in args]
        self.commands.append(args)
        output, code = '', 0
        if args[:2] == ['systemctl', 'show']:
            name = args[2]
            path = install.SYSTEMD / name
            fragment = self.fragment_override or (str(path) if path.exists() else '')
            if name == install.SERVICE:
                active = 'activating' if self.catch_up_polls else 'inactive'
                if self.catch_up_polls:
                    self.catch_up_polls -= 1
                output = f'ActiveState={active}\nResult={self.result}\nExecMainStatus={self.exit_status}\nInvocationID=synthetic-{self.runs}\n'
            else:
                output = f'ActiveState={"active" if self.active else "inactive"}\nUnitFileState={"enabled" if self.enabled else "disabled"}\nNextElapseUSecRealtime=next-fixture-session\n'
            output += f'DropInPaths={self.override}\nFragmentPath={fragment}\nLoadState={"loaded" if path.exists() else "not-found"}\nTransient=no\n'
        elif args[:2] == ['systemctl', 'is-enabled']:
            code = 0 if self.enabled else 1
        elif args[:2] == ['systemctl', 'is-active']:
            code = 0 if self.active else 1
        elif args == ['systemctl', 'start', install.SERVICE]:
            assert not self.catch_up_polls, 'explicit service start must wait for persistent catch-up'
            self.runs += 1
            mode = self.modes[self.runs - 1] if self.runs <= len(self.modes) else 'ok'
            if mode == 'exception':
                raise RuntimeError('synthetic service start failure')
            if mode != 'unchanged':
                started = datetime.now(timezone.utc)
                if mode == 'stale':
                    started -= timedelta(days=1)
                if mode == 'future':
                    started += timedelta(days=1)
                setup = (install.DATA / 'installation.json').exists()
                delivery = accepted()
                if setup:
                    self.active_at_setup.append(self.active and self.enabled)
                deliveries = [delivery] if setup else []
                if mode in ('intent', 'unknown'):
                    delivery['status'] = mode
                    delivery['provider_message_id'] = None
                elif mode == 'wrong_key':
                    delivery['candidate_keys'] = ['some-other-natural-result']
                elif mode == 'old_receipt':
                    delivery['installation_release_id'] = 'lab-old-fixture'
                elif mode == 'empty_receipt':
                    delivery['provider_message_id'] = ''
                elif mode == 'duplicate_receipt':
                    deliveries.append(dict(delivery))
                report = {'status': 'attention_required' if mode == 'attention' else 'ok',
                          'release_id': 'lab-wrong-fixture' if mode == 'wrong_release' else RID,
                          'started_at': started.isoformat(), 'completed_at': started.isoformat(),
                          'probe': {'synthetic_fixture': True, 'state': 'pending'},
                          'deliveries': deliveries, 'terminal': None}
                install.write_json(install.DATA / 'last-run.json', report)
                install.write_json(install.DATA / 'notification-ledger.json', {'deliveries': deliveries})
            self.result, self.exit_status = ('timeout', '9') if mode == 'service_failed' else ('success', '0')
        elif args == ['systemctl', 'enable', '--now', install.TIMER]:
            self.enabled = self.active = True
            if self.catch_up_on_enable:
                self.catch_up_polls = 2
        elif args == ['systemctl', 'enable', install.TIMER]:
            self.enabled = True
        elif args == ['systemctl', 'start', install.TIMER]:
            self.active = True
        elif args == ['systemctl', 'disable', install.TIMER]:
            self.enabled = False
        elif args == ['systemctl', 'stop', install.TIMER]:
            self.active = False
        return CompletedProcess(args, code, output, '')


@pytest.fixture
def rig(tmp_path, monkeypatch):
    for name in ('BASE', 'STATE', 'SYSTEMD'):
        path = tmp_path / name
        path.mkdir(mode=0o700)
        monkeypatch.setattr(install, name, path)
    monkeypatch.setattr(install, 'DATA', tmp_path / 'acceptance-state')
    # Tests model privileged installation; real non-root execution is refused by
    # require_administrator and is separately exercised by the Linux deployment.
    if os.name == 'posix' and os.geteuid() != 0:
        pytest.skip('Privileged ownership tests run as root in the isolated Linux deployment checks')
    candidate = tmp_path / 'candidate'
    templates = candidate / 'deployment/cloud'
    templates.mkdir(parents=True)
    original = Path(__file__).resolve().parents[2] / 'deployment/cloud'
    for name in ('acceptance.service', 'acceptance.timer'):
        (templates / name).write_bytes((original / name).read_bytes())
    (candidate / 'scripts').mkdir()
    (candidate / 'scripts/server_acceptance.py').write_text('# synthetic runner fixture\n', encoding='utf-8')
    monkeypatch.setattr(install, 'require_administrator', lambda: None)
    monkeypatch.setattr(install, 'service_identity', lambda: (0, 0))
    monkeypatch.setattr(install, 'current_release', lambda expected: candidate)
    fake = SystemdFixture()
    monkeypatch.setattr(install, 'run', fake.execute)
    monkeypatch.setattr(install.time, 'sleep', lambda seconds: None)
    return SimpleNamespace(candidate=candidate, systemd=fake)


def test_installs_only_after_two_fresh_runs_and_exact_platform_receipt(rig):
    result = install.install(RID)
    assert result['status'] == install.SUCCESS
    assert result['migration_receipt'] == accepted()
    assert result['database_changed'] is False and result['recipient_confirmation_separate'] is True
    assert rig.systemd.runs == 2 and rig.systemd.active_at_setup == [True]
    starts = [index for index, args in enumerate(rig.systemd.commands) if args == ['systemctl', 'start', install.SERVICE]]
    enabled = rig.systemd.commands.index(['systemctl', 'enable', '--now', install.TIMER])
    assert starts[0] < enabled < starts[1]
    record = install.read_json(install.STATE / 'acceptance.json')
    assert record['last_run']['release_id'] == RID
    assert record['unit_sha256'] == {name: install.digest((install.SYSTEMD / name).read_bytes()) for name in (install.SERVICE, install.TIMER)}
    configuration = install.read_json(install.DATA / 'installation.json')
    assert configuration['receipt_key'] == install.RECEIPT_KEY
    assert configuration['timer'] == {'active': True, 'enabled': True, 'schedule': install.SCHEDULE}


def test_units_pin_code_and_only_load_feishu_without_database_access(rig):
    units = install.render_units(rig.candidate, RID)
    service, timer = units[install.SERVICE].decode(), units[install.TIMER].decode()
    assert '/current/' not in service and install.TOKEN not in service
    assert service.count('/releases/' + RID) == 3
    assert 'User=alphaproject-lab\n' in service and 'Group=alphaproject-lab\n' in service
    assert [line for line in service.splitlines() if line.startswith('LoadCredential=')] == [
        'LoadCredential=feishu.json:/etc/alphaproject-lab/credentials/feishu.json']
    assert 'StateDirectoryMode=0700' in service and 'TimeoutStartSec=600' in service
    assert 'InaccessiblePaths=-/var/lib/alphaproject-lab -/var/lib/alphaproject ' in service
    assert 'ConditionPathExists=!/var/lib/alphaproject-lab-acceptance/completed.json' in service
    assert 'ConditionPathExists=!/var/lib/alphaproject-lab-acceptance/suspended.json' in service
    assert 'Persistent=true' in timer and 'AccuracySec=1s' in timer


@pytest.mark.parametrize('mode', ['stale', 'future', 'wrong_release', 'attention', 'service_failed'])
def test_systemctl_success_or_history_cannot_replace_a_fresh_successful_run(rig, mode):
    rig.systemd.modes = [mode]
    with pytest.raises(RuntimeError):
        install.install(RID)
    assert not (install.STATE / 'acceptance.json').exists()
    assert not (install.SYSTEMD / install.SERVICE).exists()
    assert not (install.SYSTEMD / install.TIMER).exists()
    assert not (install.DATA / 'installation.json').exists()
    assert ['systemctl', 'enable', '--now', install.TIMER] not in rig.systemd.commands
    failure = install.read_json(install.STATE / 'acceptance-last-failure.json')
    assert failure['database_restored'] is False and failure['runner_state_and_delivery_evidence_preserved'] is True
    assert (install.DATA / 'last-run.json').exists()


@pytest.mark.parametrize('mode', ['wrong_key', 'old_receipt', 'empty_receipt', 'duplicate_receipt', 'intent', 'unknown', 'unchanged'])
def test_only_exact_accepted_setup_receipt_completes_install_and_failed_config_is_removed(rig, mode):
    rig.systemd.modes = ['ok', mode]
    with pytest.raises(RuntimeError):
        install.install(RID)
    assert not (install.STATE / 'acceptance.json').exists()
    assert not (install.DATA / 'installation.json').exists()
    assert not rig.systemd.active and not rig.systemd.enabled
    assert (install.DATA / 'notification-ledger.json').exists()
    failure = install.read_json(install.STATE / 'acceptance-last-failure.json')
    assert failure['created_installation_record_removed'] is True
    assert failure['recovery_errors'] == []
    if mode in ('intent', 'unknown'):
        assert failure['last_observed_run']['deliveries'][0]['status'] == mode
        preserved = (install.DATA / 'notification-ledger.json').read_bytes()
        runs_before = rig.systemd.runs
        with pytest.raises(RuntimeError, match='uncertain'):
            install.install(RID)
        assert rig.systemd.runs == runs_before
        assert (install.DATA / 'notification-ledger.json').read_bytes() == preserved


def test_failure_preserves_installation_changed_after_this_attempt_wrote_it(rig, monkeypatch):
    execute = rig.systemd.execute
    preserved = {}

    def change_configuration_then_fail(args, **kwargs):
        result = execute(args, **kwargs)
        if args == ['systemctl', 'start', install.SERVICE] and rig.systemd.runs == 2:
            configuration = install.read_json(install.DATA / 'installation.json')
            configuration['operator_note'] = 'independent later change'
            install.write_json(install.DATA / 'installation.json', configuration)
            for name in ('installation.json', 'notification-ledger.json'):
                preserved[name] = (install.DATA / name).read_bytes()
            raise RuntimeError('synthetic failure after independent configuration change')
        return result

    monkeypatch.setattr(install, 'run', change_configuration_then_fail)
    with pytest.raises(RuntimeError, match='independent configuration change'):
        install.install(RID)
    assert {name: (install.DATA / name).read_bytes() for name in preserved} == preserved
    assert install.read_json(install.DATA / 'notification-ledger.json')['deliveries'] == [accepted()]
    failure = install.read_json(install.STATE / 'acceptance-last-failure.json')
    assert failure['created_installation_record_removed'] is False
    assert any('configuration changed again' in item for item in failure['recovery_errors'])
    assert not rig.systemd.active and not rig.systemd.enabled
    assert not (install.STATE / 'acceptance.json').exists()


def test_retry_after_failed_second_run_announces_only_after_timer_is_enabled(rig):
    rig.systemd.modes = ['ok', 'exception', 'ok', 'ok']
    with pytest.raises(RuntimeError):
        install.install(RID)
    assert not (install.DATA / 'installation.json').exists()
    result = install.install(RID)
    assert result['status'] == install.SUCCESS
    assert rig.systemd.runs == 4
    assert rig.systemd.active_at_setup == [True]


def test_repeated_successful_install_does_not_run_or_notify_again(rig):
    first = install.install(RID)
    ledger = (install.DATA / 'notification-ledger.json').read_bytes()
    result = install.install(RID)
    assert result['status'] == 'already_installed' and result['unchanged'] is True
    assert result['migration_receipt'] == first['migration_receipt']
    assert rig.systemd.runs == 2
    assert (install.DATA / 'notification-ledger.json').read_bytes() == ledger


@pytest.mark.parametrize('terminal', ['completed', 'suspended'])
def test_terminal_state_is_preserved_without_restarting_observation(rig, terminal):
    install.install(RID)
    marker = install.DATA / (terminal + '.json')
    install.write_json(marker, {'schema_version': 1, 'terminal': terminal, 'synthetic_fixture': True})
    before = marker.read_bytes()
    result = install.install(RID)
    assert result['status'] == 'already_terminal' and result['terminal'] == terminal
    assert marker.read_bytes() == before and rig.systemd.runs == 2


def test_orphan_installation_cannot_announce_a_disabled_timer(rig):
    install.state_owner(0, 0)
    value = {'schema_version': 1, 'release_id': RID, 'installed_at': datetime.now(timezone.utc).isoformat(),
             'timer': {'active': True, 'enabled': True, 'schedule': install.SCHEDULE}, 'receipt_key': install.RECEIPT_KEY}
    install.write_json(install.DATA / 'installation.json', value)
    before = (install.DATA / 'installation.json').read_bytes()
    with pytest.raises(RuntimeError, match='Orphan'):
        install.install(RID)
    assert rig.systemd.runs == 0 and not rig.systemd.active
    assert (install.DATA / 'installation.json').read_bytes() == before


def test_existing_unmanaged_unit_and_unmarked_state_are_not_adopted(rig):
    unit = install.SYSTEMD / install.SERVICE
    unit.write_bytes(b'unmanaged operator unit')
    with pytest.raises(RuntimeError, match='managed record'):
        install.install(RID)
    assert unit.read_bytes() == b'unmanaged operator unit'
    assert rig.systemd.runs == 0
    unit.unlink()
    install.DATA.mkdir(mode=0o700)
    sentinel = install.DATA / 'existing-ledger.json'
    sentinel.write_bytes(b'operator-owned evidence')
    with pytest.raises(RuntimeError, match='Unmarked nonempty'):
        install.install(RID)
    assert sentinel.read_bytes() == b'operator-owned evidence'
    assert not (install.DATA / install.OWNER_FILE).exists()


@pytest.mark.parametrize('kind', ['override', 'fragment'])
def test_runtime_unit_overrides_are_rejected_before_installation(rig, kind):
    if kind == 'override':
        rig.systemd.override = '/run/systemd/system/acceptance.service.d/operator.conf'
    else:
        rig.systemd.fragment_override = '/run/systemd/transient/other.service'
    with pytest.raises(RuntimeError):
        install.install(RID)
    assert not list(install.SYSTEMD.iterdir()) and rig.systemd.runs == 0


def test_concurrent_installer_fails_before_mutating_units(rig):
    with install.install_lock():
        with pytest.raises(RuntimeError, match='already running'):
            install.install(RID)
    assert rig.systemd.runs == 0 and not list(install.SYSTEMD.iterdir())


def test_persistent_catch_up_is_waited_out_before_fresh_receipt_check(rig):
    rig.systemd.catch_up_on_enable = True
    assert install.install(RID)['status'] == install.SUCCESS
    assert rig.systemd.runs == 2 and rig.systemd.catch_up_polls == 0


def test_existing_managed_units_and_installation_restore_without_rewriting_ledger(rig):
    owner = install.state_owner(0, 0)
    old = {install.SERVICE: b'previous managed service\n', install.TIMER: b'previous managed timer\n'}
    for name, content in old.items():
        install.atomic_write(install.SYSTEMD / name, content, mode=0o644)
    record = {'schema_version': 1, 'status': install.SUCCESS, 'release_id': RID, 'owner_id': owner['owner_id'],
              'unit_sha256': {name: install.digest(content) for name, content in old.items()}, 'migration_receipt': accepted()}
    install.write_json(install.STATE / 'acceptance.json', record)
    configuration = {'schema_version': 1, 'release_id': RID, 'installed_at': datetime.now(timezone.utc).isoformat(),
                     'timer': {'active': True, 'enabled': True, 'schedule': install.SCHEDULE}, 'receipt_key': install.RECEIPT_KEY}
    install.write_json(install.DATA / 'installation.json', configuration)
    install.write_json(install.DATA / 'notification-ledger.json', {'deliveries': [accepted()]})
    before = {name: (install.DATA / name).read_bytes() for name in ('installation.json', 'notification-ledger.json')}
    rig.systemd.active = rig.systemd.enabled = True
    rig.systemd.modes = ['exception']
    with pytest.raises(RuntimeError):
        install.install(RID)
    assert {name: (install.SYSTEMD / name).read_bytes() for name in old} == old
    assert {name: (install.DATA / name).read_bytes() for name in before} == before
    assert rig.systemd.active and rig.systemd.enabled


def test_current_release_drift_after_receipt_preserves_accepted_evidence(rig, monkeypatch):
    checks = 0

    def current(expected):
        nonlocal checks
        checks += 1
        if checks == 4:
            raise RuntimeError('synthetic release changed after receipt')
        return rig.candidate

    monkeypatch.setattr(install, 'current_release', current)
    with pytest.raises(RuntimeError, match='release changed'):
        install.install(RID)
    assert install.read_json(install.DATA / 'notification-ledger.json')['deliveries'] == [accepted()]
    assert not (install.DATA / 'installation.json').exists()
    assert not (install.STATE / 'acceptance.json').exists()
    assert not (install.SYSTEMD / install.SERVICE).exists()


def test_different_release_install_does_not_reuse_old_receipt_or_reset_evidence(rig):
    install.install(RID)
    before = {name: (install.DATA / name).read_bytes() for name in ('installation.json', 'notification-ledger.json')}
    with pytest.raises(RuntimeError, match='scope differs'):
        install.install('lab-another-release')
    assert rig.systemd.runs == 2
    assert {name: (install.DATA / name).read_bytes() for name in before} == before


def test_template_cannot_add_model_credentials(rig):
    path = rig.candidate / 'deployment/cloud/acceptance.service'
    with path.open('a', encoding='utf-8') as handle:
        handle.write('\nLoadCredential=ai.json:/etc/alphaproject-lab/credentials/ai.json\n')
    with pytest.raises(RuntimeError, match='execution boundary'):
        install.install(RID)
    assert rig.systemd.runs == 0 and not list(install.SYSTEMD.iterdir())
