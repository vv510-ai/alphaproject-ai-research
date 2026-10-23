"""Explicit observer upgrades preserve installation identity and accepted history."""

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from backend.tests.test_acceptance_install import RID, accepted, install, rig
from backend.tests.test_server_acceptance import Harness, RELEASE, report, runner, COLLECTION, EVENT


NEXT = 'lab-acceptance-upgrade-fixture'


def seed_ledger():
    value = {'schema_version': 1,
             'candidates': {install.RECEIPT_KEY: {'kind': 'installation_completed', 'status': 'accepted', 'delivery_id': 'original'}},
             'deliveries': {'original': accepted()}}
    install.write_json(install.DATA / 'notification-ledger.json', value)
    return value


def upgrade_execution(rig, monkeypatch, *, fail_on=None):
    original = rig.systemd.execute
    calls = []

    def execute(args, **kwargs):
        ledger = (install.DATA / 'notification-ledger.json').read_bytes()
        if args == ['systemctl', 'start', install.SERVICE]:
            calls.append(len(calls) + 1)
            value = install.read_json(install.DATA / 'installation.json')
            assert value['release_id'] == RID
            assert value['runner_release_id'] == NEXT
            assert rig.systemd.active and rig.systemd.enabled
            if len(calls) == fail_on:
                raise RuntimeError('synthetic upgrade execution failed')
        result = original(args, **kwargs)
        if args == ['systemctl', 'start', install.SERVICE]:
            value = install.read_json(install.DATA / 'last-run.json')
            value['release_id'] = NEXT
            install.write_json(install.DATA / 'last-run.json', value)
            install.atomic_write(install.DATA / 'notification-ledger.json', ledger)
        return result

    monkeypatch.setattr(install, 'run', execute)
    return calls


def test_explicit_upgrade_keeps_original_receipt_and_is_idempotent(rig, monkeypatch):
    first = install.install(RID)
    seed_ledger()
    original = install.read_json(install.DATA / 'installation.json')
    ledger = (install.DATA / 'notification-ledger.json').read_bytes()
    historical = install.DATA / 'historical-run.json'
    historical.write_bytes(b'{"preserved": true}')
    calls = upgrade_execution(rig, monkeypatch)
    result = install.install(NEXT, expected_previous=RID)
    assert result['release_id'] == NEXT
    assert result['installation_release_id'] == RID
    assert result['updated_from'] == RID
    assert result['migration_receipt'] == first['migration_receipt'] == accepted()
    assert install.read_json(install.DATA / 'installation.json') == {**original, 'runner_release_id': NEXT}
    assert (install.DATA / 'notification-ledger.json').read_bytes() == ledger
    assert historical.read_bytes() == b'{"preserved": true}'
    assert len(calls) == 2
    assert install.install(NEXT, expected_previous=RID)['status'] == 'already_installed'
    assert len(calls) == 2


def test_explicit_upgrade_rejects_stale_expected_observer_before_changes(rig):
    install.install(RID)
    old = {name: (install.SYSTEMD / name).read_bytes() for name in (install.SERVICE, install.TIMER)}
    with pytest.raises(RuntimeError, match='previous|scope'):
        install.install(NEXT, expected_previous='lab-unrelated-observer')
    assert {name: (install.SYSTEMD / name).read_bytes() for name in old} == old
    assert rig.systemd.runs == 2 and rig.systemd.active


@pytest.mark.parametrize('fail_on', [1, 2])
def test_failed_upgrade_restores_binding_and_units_but_retains_new_evidence(rig, monkeypatch, fail_on):
    first = install.install(RID)
    seed_ledger()
    old = {name: (install.SYSTEMD / name).read_bytes() for name in (install.SERVICE, install.TIMER)}
    configuration = (install.DATA / 'installation.json').read_bytes()
    ledger = (install.DATA / 'notification-ledger.json').read_bytes()
    calls = upgrade_execution(rig, monkeypatch, fail_on=fail_on)
    with pytest.raises(RuntimeError, match='synthetic upgrade'):
        install.install(NEXT, expected_previous=RID)
    assert len(calls) == fail_on
    assert {name: (install.SYSTEMD / name).read_bytes() for name in old} == old
    assert (install.DATA / 'installation.json').read_bytes() == configuration
    assert (install.DATA / 'notification-ledger.json').read_bytes() == ledger
    assert install.read_json(install.STATE / 'acceptance.json') == first
    assert rig.systemd.active and rig.systemd.enabled
    if fail_on == 2:
        assert install.read_json(install.DATA / 'last-run.json')['release_id'] == NEXT


def test_upgraded_runner_uses_original_installation_identity_without_resending(tmp_path):
    h = Harness(tmp_path)
    h.install()
    initial = h.run()
    assert len(h.feishu.sent) == 1
    old_deliveries = deepcopy(h.ledger()['deliveries'])
    path = h.directory / 'installation.json'
    configuration = runner.read_json(path)
    configuration['runner_release_id'] = NEXT
    runner.write_json(path, configuration)
    current_release = {**RELEASE, 'release_id': NEXT}
    value = runner.run_once(h.directory, probe_client=h.client, feishu_client=h.feishu,
                            config=h.config, release=current_release,
                            probe_runner=lambda client, **opts: report(opts['previous_state']),
                            clock=lambda: h.now, sleeper=lambda _: None, monotonic=lambda: 0,
                            metadata_reader=lambda *_: deepcopy(h.metadata))
    assert value['status'] == 'ok' and value['errors'] == []
    assert value['release_id'] == NEXT
    assert value['deliveries'] == initial['deliveries']
    assert h.ledger()['deliveries'] == old_deliveries
    assert len(h.feishu.sent) == 1


@pytest.mark.parametrize('damage', ['missing', 'malformed', 'unknown', 'unlinked', 'changed_receipt'])
def test_upgrade_rejects_bad_durable_ledger_before_changing_units(rig, damage):
    install.install(RID)
    value = seed_ledger()
    path = install.DATA / 'notification-ledger.json'
    if damage == 'missing':
        path.unlink()
    elif damage == 'malformed':
        path.write_text('broken JSON', encoding='utf-8')
    else:
        if damage == 'unknown':
            value['deliveries']['pending'] = {'status': 'unknown', 'candidate_keys': ['event:new']}
        elif damage == 'unlinked':
            value['candidates'][install.RECEIPT_KEY]['delivery_id'] = 'missing'
        else:
            value['deliveries']['original']['provider_message_id'] = 'om_changed'
        install.write_json(path, value)
    before = {name: (install.SYSTEMD / name).read_bytes() for name in (install.SERVICE, install.TIMER)}
    configuration = (install.DATA / 'installation.json').read_bytes()
    with pytest.raises(RuntimeError):
        install.install(NEXT, expected_previous=RID)
    assert rig.systemd.runs == 2 and rig.systemd.active and rig.systemd.enabled
    assert {name: (install.SYSTEMD / name).read_bytes() for name in before} == before
    assert (install.DATA / 'installation.json').read_bytes() == configuration


@pytest.mark.parametrize('target', ['binding', 'root_record'])
def test_upgrade_write_then_fsync_failure_restores_both_managed_records(rig, monkeypatch, target):
    first = install.install(RID)
    seed_ledger()
    original = install.read_json(install.DATA / 'installation.json')
    units = {name: (install.SYSTEMD / name).read_bytes() for name in (install.SERVICE, install.TIMER)}
    upgrade_execution(rig, monkeypatch)
    write = install.write_json
    thrown = []
    failure_path = install.DATA / 'installation.json' if target == 'binding' else install.STATE / 'acceptance.json'

    def write_then_fail(path, value, **kwargs):
        write(path, value, **kwargs)
        if path == failure_path and not thrown:
            thrown.append(True)
            raise OSError('synthetic directory fsync failed after replace')

    monkeypatch.setattr(install, 'write_json', write_then_fail)
    with pytest.raises(RuntimeError, match='fsync'):
        install.install(NEXT, expected_previous=RID)
    assert install.read_json(install.DATA / 'installation.json') == original
    assert install.read_json(install.STATE / 'acceptance.json') == first
    assert {name: (install.SYSTEMD / name).read_bytes() for name in units} == units
    assert rig.systemd.active and rig.systemd.enabled
    assert install.read_json(install.STATE / 'acceptance-last-failure.json')['recovery_errors'] == []


@pytest.mark.parametrize('terminal', ['completed', 'suspended'])
def test_upgrade_finishing_on_first_run_preserves_terminal_without_second_start(rig, monkeypatch, terminal):
    install.install(RID)
    seed_ledger()
    calls = upgrade_execution(rig, monkeypatch)
    execute = install.run

    def finish(args, **kwargs):
        value = execute(args, **kwargs)
        if args == ['systemctl', 'start', install.SERVICE]:
            data = install.read_json(install.DATA / 'last-run.json')
            data.update(schema_version=1, terminal=terminal, run_id='test-terminal-run')
            install.write_json(install.DATA / 'last-run.json', data)
            install.write_json(install.DATA / (terminal + '.json'), {
                'schema_version': 1, 'terminal': terminal, 'release_id': NEXT,
                'run_id': data['run_id'], 'completed_at': data['completed_at']})
        return value

    monkeypatch.setattr(install, 'run', finish)
    result = install.install(NEXT, expected_previous=RID)
    assert result['last_run']['terminal'] == terminal
    assert len(calls) == 1
    assert install.read_json(install.DATA / (terminal + '.json'))['release_id'] == NEXT


def test_upgraded_runner_can_finish_using_original_installation_receipt(tmp_path):
    h = Harness(tmp_path)
    h.install()
    h.run()
    path = h.directory / 'installation.json'
    configuration = runner.read_json(path)
    runner.write_json(path, {**configuration, 'runner_release_id': NEXT})
    value = runner.run_once(h.directory, probe_client=h.client, feishu_client=h.feishu,
                            config=h.config, release={**RELEASE, 'release_id': NEXT},
                            probe_runner=lambda client, **opts: report(opts['previous_state'], [COLLECTION, EVENT], passed=True),
                            clock=lambda: h.now, sleeper=lambda _: None, monotonic=lambda: 0,
                            metadata_reader=lambda *_: deepcopy(h.metadata))
    assert value['terminal'] == 'completed'
    assert len(h.feishu.sent) == 2
    assert len([x for x in value['deliveries'] if runner.INSTALLATION_KEY in x['candidate_keys']]) == 1


@pytest.mark.parametrize('damage', ['failed-service', 'bad-schema', 'unlinked-run'])
def test_catch_up_terminal_requires_successful_service_and_linked_current_report(rig, damage):
    install.install(RID)
    stamp = datetime.now(timezone.utc)
    value = {'schema_version': 1, 'release_id': NEXT, 'status': 'ok', 'terminal': 'completed',
             'run_id': 'new-terminal', 'started_at': stamp.isoformat(), 'completed_at': stamp.isoformat(),
             'probe': {}, 'deliveries': [accepted()]}
    marker = {key: value[key] for key in ('schema_version', 'release_id', 'terminal', 'run_id', 'completed_at')}
    if damage == 'failed-service':
        rig.systemd.result, rig.systemd.exit_status = 'exit-code', '1'
    elif damage == 'bad-schema':
        marker['schema_version'] = 2
    else:
        marker['run_id'] = 'older-run'
    install.write_json(install.DATA / 'last-run.json', value)
    install.write_json(install.DATA / 'completed.json', marker)
    with pytest.raises(RuntimeError, match='Terminal'):
        install.fresh_terminal(NEXT, 0, stamp, RID)
