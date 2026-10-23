"""Real observer -> proof evaluator -> durable ledger with controlled raw inputs."""

from copy import deepcopy

from backend.tests.test_live_monitor_probe import market, no_external_http
from backend.tests.test_live_monitor_sampling import SequenceClient, more_samples, stale
from backend.tests.test_server_acceptance import Harness, RELEASE
from scripts import server_acceptance as runner


def test_upgraded_service_recovers_first_invalid_sample_and_notifies_once(market, tmp_path, monkeypatch):
    h = Harness(tmp_path)
    h.now = market.clock()
    h.install()
    h.run()
    before = deepcopy(h.ledger()['deliveries'])
    path = h.directory / 'installation.json'
    configuration = runner.read_json(path)
    new_release = {**RELEASE, 'release_id': 'lab-window-flow'}
    runner.write_json(path, {**configuration, 'runner_release_id': new_release['release_id']})
    monkeypatch.setattr(runner.probe, 'DEFAULT_GENERATION', market.generation)
    rows = more_samples(market)
    rows[0] = stale(rows[0])
    for row in rows:
        row['notifications']['target_fingerprint_sha256'] = runner.probe.digest(h.config.fingerprint)
    waited = []

    def execute():
        return runner.run_once(h.directory, probe_client=SequenceClient(rows),
                               feishu_client=h.feishu, config=h.config, release=new_release,
                               clock=market.clock, sleeper=waited.append,
                               monotonic=lambda: sum(waited),
                               metadata_reader=lambda *_: deepcopy(h.metadata))

    first = execute()
    assert first['status'] == 'ok', first['errors']
    assert first['probe']['sampling']['actual_observations'] == 4
    assert first['probe']['sampling']['stop_code'] == 'collection_passed'
    assert first['probe']['continuous_collection']['cumulative_pass'] is True
    assert sum(waited) == 195
    assert len(h.feishu.sent) == 2
    assert all(h.ledger()['deliveries'][key] == value for key, value in before.items())
    stored = runner.read_json(h.directory / 'last-run.json')
    assert stored['probe']['sampling']['required_valid_samples'] == 3
    # A second invocation reloads the same private state and recomputes proof;
    # accepted notification candidates must not cause another delivery.
    second = execute()
    assert second['status'] == 'ok'
    assert len(h.feishu.sent) == 2
    assert {x['request_id']: x for x in second['deliveries']} == {x['request_id']: x for x in first['deliveries']}
    assert second['terminal'] is None  # No natural 3% event was provided.
