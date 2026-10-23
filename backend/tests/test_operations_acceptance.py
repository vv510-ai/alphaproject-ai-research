"""Independent operational acceptance with synthetic inputs and isolated stores.

This suite never claims a natural trading observation, recipient confirmation or
real backup delivery. Production parsing, cache persistence and collector calls
stay in the path; the quote source and clock are controlled.
"""

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
from urllib.request import OpenerDirector

import httpx
import pytest

from backend.app.quote_collector import QuoteCollector
from backend.app.quote_service import QuoteService
from backend.app.store import WatchlistStore
from backend.app import notification_service as notification_module
from backend.app.anomaly_service import AnomalyService
from backend.app.notification_service import NotificationService
from backend.tests.test_live_acceptance import Clock, RawFeed, SYMBOL, security, CONFIG, FakeFeishuWire
from backend.tests.test_operations_install import setup as installer_setup
from scripts import verify_live_monitor as probe


@pytest.fixture(autouse=True)
def no_external_http(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Real HTTP is forbidden in operations acceptance")

    async def blocked_async(*args, **kwargs):
        raise AssertionError("Real async HTTP is forbidden in operations acceptance")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked_async)
    monkeypatch.setattr(OpenerDirector, "open", blocked)


@pytest.fixture
def sampling(tmp_path):
    clock = Clock()
    store = WatchlistStore(tmp_path / "operations-acceptance.sqlite3")
    store.initialize()
    store.add(security(clock))
    feed = RawFeed(clock)
    quotes = QuoteService(store, feed, clock)
    quotes.initialize()
    collector = QuoteCollector(store, quotes, clock)
    return SimpleNamespace(clock=clock, store=store, feed=feed, quotes=quotes, collector=collector)


def dashboard_proof(sampling):
    dashboard = sampling.quotes.dashboard()
    item = next(item for item in dashboard["items"] if item["symbol"] == SYMBOL)
    assert item["quote"]["eligibility"]["eligible"] is True
    return dashboard, item["quote"]["eligibility"]["proof"]


def assert_acquisition(sample, dashboard, *, origin, cycle_id):
    acquisition = sample["acquisition"]
    assert acquisition["origin"] == origin
    assert acquisition["cycle_id"] == cycle_id
    start = datetime.fromisoformat(acquisition["started_at"])
    committed = datetime.fromisoformat(acquisition["committed_at"])
    observed = datetime.fromisoformat(dashboard["as_of"])
    assert all(value.tzinfo and value.utcoffset() is not None for value in (start, committed, observed))
    assert start <= committed <= observed


def run_cycle(sampling, *, volume):
    sampling.feed.publish(volume=volume)
    sampling.collector.tick()
    state = sampling.collector.state()
    assert state["attempted"] == state["succeeded"] == 1
    assert state["failed"] == 0
    assert isinstance(state["cycle_id"], str) and state["cycle_id"]
    return state["cycle_id"]


def test_manual_activity_is_eligible_without_claiming_background_collection(sampling):
    for volume in ("1000", "1100"):
        sampling.feed.publish(volume=volume)
        sampling.quotes.refresh("sh", SYMBOL)
        if volume == "1000":
            sampling.clock.advance(60)
    dashboard, proof = dashboard_proof(sampling)
    for name in ("previous", "current"):
        assert_acquisition(proof[name], dashboard, origin="manual", cycle_id=None)
    with sampling.store.connect() as db:
        cached = json.loads(db.execute("SELECT payload FROM quote_cache").fetchone()[0])
    assert "acquisition" not in cached  # Existing Quote consumers retain their original payload.


def test_three_actual_collector_cycles_preserve_the_provenance_of_each_sample(sampling):
    first = run_cycle(sampling, volume="1000")
    sampling.clock.advance(60)
    second = run_cycle(sampling, volume="1100")
    dashboard, proof = dashboard_proof(sampling)
    assert_acquisition(proof["previous"], dashboard, origin="collector", cycle_id=first)
    assert_acquisition(proof["current"], dashboard, origin="collector", cycle_id=second)
    previous_current = proof["current"]

    sampling.clock.advance(60)
    third = run_cycle(sampling, volume="1200")
    dashboard, proof = dashboard_proof(sampling)
    assert len({first, second, third}) == 3
    assert proof["previous"] == previous_current
    assert_acquisition(proof["current"], dashboard, origin="collector", cycle_id=third)
    assert sampling.feed.calls == [SYMBOL, SYMBOL, SYMBOL]


def test_mixed_manual_and_background_samples_are_not_relabelled(sampling):
    sampling.feed.publish(volume="1000")
    sampling.quotes.refresh("sh", SYMBOL)
    sampling.clock.advance(60)
    cycle = run_cycle(sampling, volume="1100")
    dashboard, proof = dashboard_proof(sampling)
    assert_acquisition(proof["previous"], dashboard, origin="manual", cycle_id=None)
    assert_acquisition(proof["current"], dashboard, origin="collector", cycle_id=cycle)

    sampling.clock.advance(60)
    sampling.feed.publish(volume="1200")
    sampling.quotes.refresh("sh", SYMBOL)
    dashboard, proof = dashboard_proof(sampling)
    assert_acquisition(proof["previous"], dashboard, origin="collector", cycle_id=cycle)
    assert_acquisition(proof["current"], dashboard, origin="manual", cycle_id=None)


def test_source_wait_is_included_in_collection_start_and_commit_times(sampling):
    first = run_cycle(sampling, volume="1000")
    sampling.clock.advance(60)
    started = sampling.clock()
    sampling.feed.during_fetch = lambda _: sampling.clock.advance(3)
    second = run_cycle(sampling, volume="1100")
    dashboard, proof = dashboard_proof(sampling)
    assert_acquisition(proof["previous"], dashboard, origin="collector", cycle_id=first)
    assert_acquisition(proof["current"], dashboard, origin="collector", cycle_id=second)
    acquisition = proof["current"]["acquisition"]
    assert datetime.fromisoformat(acquisition["started_at"]) == started
    assert datetime.fromisoformat(acquisition["committed_at"]) == sampling.clock()
    assert (sampling.clock() - started).total_seconds() == 3


def test_reads_and_reopened_services_cannot_create_a_new_collection_cycle(sampling):
    first = run_cycle(sampling, volume="1000")
    sampling.clock.advance(60)
    second = run_cycle(sampling, volume="1100")
    _, proof = dashboard_proof(sampling)
    frozen = json.loads(json.dumps(proof))

    sampling.clock.advance(1)
    reopened = QuoteService(sampling.store, sampling.feed, sampling.clock)
    reopened.initialize()
    another_collector = QuoteCollector(sampling.store, reopened, sampling.clock)
    for _ in range(3):
        dashboard = reopened.dashboard()
        assert dashboard["items"][0]["quote"]["eligibility"]["proof"] == frozen
        another_collector.state()
    assert sampling.feed.calls == [SYMBOL, SYMBOL]
    assert_acquisition(frozen["previous"], dashboard, origin="collector", cycle_id=first)
    assert_acquisition(frozen["current"], dashboard, origin="collector", cycle_id=second)


def test_legacy_evidence_is_not_relabelled_when_read_or_followed_by_a_new_cycle(sampling):
    for volume in ("1000", "1100"):
        sampling.feed.publish(volume=volume)
        sampling.quotes.refresh("sh", SYMBOL)
        if volume == "1000":
            sampling.clock.advance(60)
    # Represent an existing v0.10.0 row, which had no provenance field at all.
    with sampling.store.connect() as db, db:
        evidence = json.loads(db.execute("SELECT payload FROM quote_live_evidence").fetchone()[0])
        for sample in (evidence["candidate"], evidence["eligibility"]["proof"]["previous"],
                       evidence["eligibility"]["proof"]["current"]):
            sample.pop("acquisition")
        db.execute("UPDATE quote_live_evidence SET payload=?", (json.dumps(evidence),))
    reopened = QuoteService(sampling.store, sampling.feed, sampling.clock)
    reopened.initialize()
    snapshot = reopened.dashboard()["items"][0]["quote"]["eligibility"]["proof"]
    assert all("acquisition" not in snapshot[key] for key in ("previous", "current"))

    sampling.clock.advance(60)
    first = run_cycle(sampling, volume="1200")
    dashboard, proof = dashboard_proof(sampling)
    assert "acquisition" not in proof["previous"]
    assert_acquisition(proof["current"], dashboard, origin="collector", cycle_id=first)

    sampling.clock.advance(60)
    second = run_cycle(sampling, volume="1300")
    dashboard, proof = dashboard_proof(sampling)
    assert_acquisition(proof["previous"], dashboard, origin="collector", cycle_id=first)
    assert_acquisition(proof["current"], dashboard, origin="collector", cycle_id=second)


@pytest.fixture
def backup_source(tmp_path):
    store = WatchlistStore(tmp_path / "watchlist.sqlite3")
    store.initialize()
    store.add(security(Clock()))
    release = tmp_path / "release.json"
    release.write_text(json.dumps({"release_id": "lab-independent-acceptance", "app_version": "0.10.1",
                                   "source_sha256": "0" * 64, "built_at": "2026-10-22T00:00:00+00:00"}), encoding="utf-8")
    return SimpleNamespace(store=store, release=release, root=tmp_path / "managed-backups")


def test_failed_new_backup_cannot_apply_a_smaller_retention_to_existing_good_backups(backup_source):
    from deployment.cloud import backup

    source = backup_source
    receipts = [backup.create_backup(source.store.path, release_file=source.release, keep=2) for _ in range(2)]
    originals = {receipt["backup_id"]: hashlib.sha256(
        (Path(receipt["directory"]) / "watchlist.sqlite3").read_bytes()).hexdigest() for receipt in receipts}

    # Damage only this test's isolated source. A failing replacement backup must
    # never treat --keep 1 as permission to discard either previous good copy.
    source.store.path.write_bytes(b"synthetic broken SQLite source")
    with pytest.raises((backup.BackupError, sqlite3.DatabaseError)):
        backup.create_backup(source.store.path, release_file=source.release, keep=1)
    listed = backup.list_backups(source.root)
    assert {item["backup_id"] for item in listed["backups"]} == set(originals)
    for backup_id, digest in originals.items():
        assert hashlib.sha256((source.root / backup_id / "watchlist.sqlite3").read_bytes()).hexdigest() == digest
    assert not list(source.root.glob(".pending-*"))


def test_new_source_commits_do_not_change_or_invalidate_the_completed_backup(backup_source):
    from deployment.cloud import backup

    source = backup_source
    receipt = backup.create_backup(source.store.path, release_file=source.release)
    snapshot = Path(receipt["directory"]) / "watchlist.sqlite3"
    before = snapshot.read_bytes()
    source.store.add(security(Clock(), "000001"))
    assert {item["symbol"] for item in source.store.list_items()} == {SYMBOL, "000001"}

    verified = backup.verify_backup(source.root, receipt["backup_id"])
    assert verified["verified"] is True
    assert snapshot.read_bytes() == before
    with sqlite3.connect(snapshot.as_uri() + "?mode=ro&immutable=1", uri=True) as db:
        assert db.execute("SELECT symbol FROM watchlist").fetchall() == [(SYMBOL,)]


def test_installer_rejects_a_successful_job_that_only_has_an_old_snapshot(installer_setup, monkeypatch):
    from deployment.cloud import operations as ops

    _, commands, _ = installer_setup
    managed = ops.DATA / "managed-backups"
    managed.mkdir()
    (managed / "previous-complete").mkdir()
    monkeypatch.setattr(ops, "list_backups", lambda _: {"backups": [{
        "backup_id": "previous-complete", "manifest": {
            "source": {"release": {"release_id": "lab-test"}},
            "created_at_utc": "2026-10-21T00:00:00+00:00",
        }}], "ignored": []})
    with pytest.raises(RuntimeError, match="fresh verified snapshot"):
        ops.install("lab-test")
    assert ["systemctl", "enable", "--now", ops.TIMER] not in commands
    assert (managed / "previous-complete").is_dir()


def test_installer_records_nonzero_recovery_commands_as_incomplete_recovery(installer_setup, monkeypatch):
    from deployment.cloud import operations as ops

    _, _, original = installer_setup

    def failing_run(args, **kwargs):
        if args == ["systemctl", "start", ops.SERVICE]:
            raise RuntimeError("controlled backup job failure")
        if args[:2] in (["systemctl", "stop"], ["systemctl", "disable"]):
            return subprocess.CompletedProcess(args, 1, "", "controlled recovery refusal")
        return original(args, **kwargs)

    monkeypatch.setattr(ops, "run", failing_run)
    with pytest.raises(RuntimeError, match="controlled backup job failure"):
        ops.install("lab-test")
    result = json.loads((ops.STATE / "operations-last-failure.json").read_text())
    assert len(result["recovery_errors"]) == 3
    assert all("controlled recovery refusal" in error for error in result["recovery_errors"])
    assert result["database_restored"] is False


def test_pinned_backup_cli_still_executes_when_the_application_copy_disappears(installer_setup):
    from deployment.cloud import operations as ops

    candidate, _, _ = installer_setup
    result = ops.install("lab-test")
    # Only the install fixture's copied candidate is removed, never workspace code.
    source_copy = candidate / "deployment" / "cloud" / "backup.py"
    assert source_copy.is_relative_to(candidate) and candidate.name == "candidate"
    source_copy.unlink()
    process = subprocess.run([sys.executable, "-I", "-B", result["pinned_script"], "--help"],
                             capture_output=True, text=True, timeout=5)
    assert process.returncode == 0, process.stderr
    assert "create" in process.stdout and "verify" in process.stdout


@pytest.fixture
def threaded_probe(tmp_path, monkeypatch):
    """Real worker thread and service transactions, with a fake quote/provider wire.

    Unlike a fixture that overwrites is_alive, collection provenance below is
    obtained from the running production collector. This remains synthetic
    acceptance and is never presented as an actual market or recipient result.
    """
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.delenv("ALPHA_LAB_PUBLIC_URL", raising=False)
    monkeypatch.setattr(notification_module, "load_config", lambda _: CONFIG)
    clock = Clock()
    store = WatchlistStore(tmp_path / "threaded-probe.sqlite3")
    store.initialize()
    store.add(security(clock))
    # The observed public generation is stored in UTC, although its equivalent
    # local wall time is 2026-10-17 23:12:49.230568 +08:00.
    generation = "2026-10-17T15:12:49.230568+00:00"
    with store.connect() as db, db:
        db.execute("UPDATE watchlist SET created_at=?", (generation,))
    feed = RawFeed(clock)
    quotes = QuoteService(store, feed, clock)
    quotes.initialize()
    wire = FakeFeishuWire()
    notifications = NotificationService(store, tmp_path / "absent-test-vault", wire.client,
                                        clock, "https://example.invalid/lab")
    notifications.initialize()
    notifications.set_enabled(True)
    anomalies = AnomalyService(store, quotes, notifications, clock, "/lab")
    anomalies.initialize()
    for previous_version in (0, 1):
        anomalies.save_rule("sh", SYMBOL, generation, "3", "3", True, previous_version)
    collector = QuoteCollector(store, quotes, clock)
    quotes.collector = collector
    rows, previous_cycle = [], None
    try:
        for index in range(4):
            # Publish the future raw sample before advancing the controlled
            # clock, so a concurrent production tick cannot consume stale data.
            next_time = clock() + timedelta(seconds=60 if index else 0)
            feed.publish(volume=str(1000 + index * 100), price="103.00", at=next_time)
            if index:
                clock.advance(60)
            else:
                collector.start()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                current = collector.state()
                if (current["cycle_id"] and current["cycle_id"] != previous_cycle
                        and current["state"] == "waiting" and current["succeeded"] == 1):
                    break
                time.sleep(.01)
            else:
                pytest.fail("The isolated real background collector did not finish its bounded cycle")
            assert collector.is_alive() and current["running"]
            previous_cycle = current["cycle_id"]
            anomalies.check_live()
            notifications.process_pending_events()
            if index:
                rows.append({
                    "release": {"release_id": "independent-threaded-probe", "app_version": "0.11.0",
                                "source_sha256": "c" * 64},
                    "health": {"status": "ok", "service": "AlphaProject-Lab", "version": "0.11.0",
                               "release_id": "independent-threaded-probe", "instance_id": "real-worker-test"},
                    "dashboard": probe.public_response("api/dashboard", quotes.dashboard()),
                    "anomalies": probe.public_response("api/anomalies", anomalies.state()),
                    "notifications": probe.public_response("api/notifications", notifications.state()),
                    "history": probe.public_response("api/anomalies/events", anomalies.event_history(probe.DEFAULT_SINCE)),
                    "errors": [],
                })
    finally:
        collector.stop()
        if collector.thread:
            collector.thread.join(timeout=2)
        assert not collector.is_alive()
    return SimpleNamespace(rows=rows, clock=clock, store=store, generation=generation,
                           feed=feed, wire=wire, notifications=notifications, anomalies=anomalies)


def test_probe_defaults_accept_real_background_cycles_and_service_event_without_claiming_user_confirmation(threaded_probe):
    rig = threaded_probe
    original = deepcopy(rig.rows)
    result = probe.analyze_observations(rig.rows)
    assert rig.rows == original
    assert result["continuous_collection"]["status"] == "passed"
    assert result["rule_configuration"]["status"] == "matched"
    assert result["natural_alert"]["status"] == "passed"
    assert result["natural_alert"]["history_complete"] is True
    assert len(result["events"]) == len(rig.wire.messages) == 1
    assert result["events"][0]["platform_accepted"] is True
    assert result["events"][0]["user_confirmed"] is False
    assert result["events"][0]["id"] == rig.rows[-1]["history"]["events"][0]["id"]
    assert len(set(point["cycle_id"] for point in result["continuous_collection"]["points"])) == 3
    assert rig.feed.calls == [SYMBOL] * 4
    assert {item["kind"] for item in result["notification_candidates"]} == {
        "collection_passed", "natural_event_accepted"}
    with rig.store.connect() as db:
        assert db.execute("SELECT confirmed_at FROM notification_messages").fetchall()[0][0] is None

    # Identical repeated observations are reads, not three distinct cycles.
    repeated = probe.analyze_observations([deepcopy(rig.rows[-1])] * 3)
    assert repeated["continuous_collection"]["status"] == "pending"
    assert repeated["natural_alert"]["status"] == "passed"


def test_natural_event_cumulative_acceptance_does_not_leak_across_expected_ranges(threaded_probe):
    first = probe.analyze_observations(threaded_probe.rows)
    assert first["natural_alert"]["cumulative_pass"] is True
    original_state = deepcopy(first["state"])
    # A different release may keep the same historic natural event acceptance.
    current = deepcopy(threaded_probe.rows[-1])
    current["release"]["release_id"] = current["health"]["release_id"] = "next-release"
    current["history"]["events"] = []
    assert probe.analyze_observations([current], original_state)["natural_alert"]["cumulative_pass"] is True

    new_since = (threaded_probe.clock() + timedelta(seconds=1)).isoformat()
    for changed in ({"expected_generation": "2026-10-22T02:05:00+00:00"},
                    {"expected_rule_version": 3}, {"since": new_since}):
        row = deepcopy(current)
        if "since" in changed:
            row["history"]["event_history"].update(since=new_since, through=new_since)
            row["history"]["as_of"] = new_since
        observed = probe.analyze_observations([row], original_state, **changed)
        assert observed["natural_alert"]["status"] == "pending"
        assert observed["natural_alert"]["cumulative_pass"] is False, changed
        assert not any(item["kind"] == "natural_event_accepted" for item in observed["notification_candidates"])
    assert first["state"] == original_state
