"""Independent server-observer boundaries with no production data or network.

The real observer, persistence and Feishu client remain in the path. The public
probe result and external HTTP transport are controlled; these tests are not
natural-market, deployment or recipient-confirmation evidence.
"""

from copy import deepcopy
from datetime import timedelta
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

from backend.app.runtime_config import APP_VERSION
from backend.tests.test_acceptance_install import RID, rig as install_rig
from backend.tests.test_live_acceptance import Clock, CONFIG, FakeFeishuWire
from deployment.cloud import acceptance as installer
from scripts import server_acceptance as observer
from scripts import verify_live_monitor as probe


class InterruptedSend(BaseException):
    """Simulate process interruption, which an Exception handler must not absorb."""


@pytest.fixture(autouse=True)
def no_production_io(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Independent observer tests may not use a real HTTP endpoint or SQLite database")

    async def blocked_async(*args, **kwargs):
        raise AssertionError("Independent observer tests may not use real async HTTP")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked_async)
    monkeypatch.setattr(OpenerDirector, "open", blocked)
    monkeypatch.setattr(sqlite3, "connect", blocked)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)


def put_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    path.chmod(0o600)


def get_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def server_case(tmp_path):
    case = SimpleNamespace(
        directory=tmp_path / "isolated-server-state",
        clock=Clock("2026-10-22T03:30:00+08:00"),
        wire=FakeFeishuWire(),
        release={"release_id": "lab-server-acceptance-review", "app_version": APP_VERSION,
                 "source_sha256": "c" * 64},
        public={"configured": True, "enabled": True, "verified_at": "2026-10-21T00:00:00+00:00",
                "target_fingerprint_sha256": probe.digest(CONFIG.fingerprint)},
        unavailable=False, completed_goals=False, changed_rule=False,
        timer_active=False, probe_calls=0, public_reads=0,
    )

    class PublicClient:
        requests = []

        def get(self, endpoint, params=None):
            assert endpoint == "api/notifications" and params is None
            case.public_reads += 1
            if case.unavailable:
                raise ValueError("controlled website unavailable")
            return deepcopy(case.public)

    def controlled_probe(client, **kwargs):
        case.probe_calls += 1
        if case.unavailable:
            raise RuntimeError("controlled probe unavailable")
        state = deepcopy(kwargs["previous_state"])
        state.setdefault("candidates", {})
        state.setdefault("notified", {})
        # The real probe retains candidate history but clears inactive results.
        for value in state["candidates"].values():
            value["active"] = False
        candidates = []
        if case.completed_goals:
            candidates.extend([
                {"key": "collection:review-scope", "kind": "collection_passed", "active": True},
                {"key": "event:review-event:accepted", "kind": "natural_event_accepted", "active": True,
                 "event": {"id": "review-event", "direction": "up", "change_pct": "3.00"}},
            ])
        if case.changed_rule:
            candidates.append({"key": "configuration:review-changed", "kind": "rule_changed", "active": True})
        for item in candidates:
            state["candidates"][item["key"]] = deepcopy(item)
        # Reoffer the controlled current candidates even after acknowledgement:
        # durable delivery evidence must independently prevent a second send.
        return {
            "state": state, "notification_candidates": candidates,
            "continuous_collection": {"status": "passed" if case.completed_goals else "pending",
                                      "cumulative_pass": case.completed_goals, "scope": "review-scope"},
            "natural_alert": {"status": "passed" if case.completed_goals else "pending",
                              "cumulative_pass": case.completed_goals, "history_complete": True,
                              "accepted_event_ids": ["review-event"] if case.completed_goals else []},
            "rule_configuration": {"status": "changed" if case.changed_rule else "matched",
                                   "changed_during_run": case.changed_rule},
            "observations": [{"notifications": deepcopy(case.public)}],
        }

    def metadata(started, environment):
        return {"activation": "manual-start", "invocation_id": None, "planned_at": None,
                "triggered_at": None, "timer": {"active": case.timer_active, "enabled": case.timer_active,
                                                "schedule": observer.TIMER_SCHEDULE}}

    def run(**kwargs):
        case.clock.advance(1)
        return observer.run_once(
            case.directory, probe_client=PublicClient(), feishu_client=case.wire.client,
            config=CONFIG, release=case.release, probe_runner=controlled_probe,
            clock=case.clock, sleeper=lambda _: pytest.fail("Controlled probe must not sleep"),
            environment={}, metadata_reader=metadata, **kwargs,
        )

    case.run, case.controlled_probe, case.metadata = run, controlled_probe, metadata
    return case


@pytest.mark.parametrize("revocation", ["disabled", "different_target"])
def test_explicit_revocation_survives_a_later_outage_until_fresh_authorization(server_case, revocation):
    case = server_case
    initial = case.run()
    assert initial["status"] == "ok" and not case.wire.messages
    original = deepcopy(case.public)
    if revocation == "disabled":
        case.public["enabled"] = False
    else:
        case.public["target_fingerprint_sha256"] = "e" * 64
    denied = case.run()
    assert denied["notification_gate"]["allowed"] is False
    assert not case.wire.messages

    case.unavailable = True
    outage = case.run()
    assert outage["notification_gate"]["allowed"] is False
    assert not case.wire.messages, "An unavailable website must not revive a previously revoked notification authorization"
    assert not (case.directory / "completed.json").exists()

    # Only a fresh positive response for the original target may restore it.
    case.unavailable = False
    case.public = original
    assert case.run()["notification_gate"]["allowed"] is True
    assert not case.wire.messages
    case.unavailable = True
    first_fault = case.run()
    assert first_fault["notification_gate"]["fault_only"] is True
    assert len(case.wire.messages) == 1
    again = case.run()
    assert len(case.wire.messages) == 1
    assert first_fault["deliveries"] == again["deliveries"]


def test_accepted_but_unpersisted_send_is_not_retried_after_reopening(server_case):
    case = server_case
    case.completed_goals = True

    def crash(stage, record):
        if stage == "after_provider_acceptance":
            assert len(case.wire.messages) == 1
            saved = get_json(case.directory / "notification-ledger.json")
            assert saved["deliveries"][record["request_id"]]["status"] == "intent"
            raise InterruptedSend()

    with pytest.raises(InterruptedSend):
        case.run(fault_hook=crash)
    first_request = case.wire.messages[0]["uuid"]
    reopened = case.run()
    assert len(case.wire.messages) == 1
    assert reopened["terminal"] is None
    assert not (case.directory / "completed.json").exists()
    assert len(reopened["deliveries"]) == 1
    assert reopened["deliveries"][0]["request_id"] == first_request
    assert reopened["deliveries"][0]["status"] == "unknown"
    assert reopened["deliveries"][0]["provider_message_id"] is None


def test_durable_receipt_recovers_after_crash_without_another_message(server_case):
    case = server_case
    case.completed_goals = True
    case.directory.mkdir(mode=0o700)
    case.timer_active = True
    put_json(case.directory / "installation.json", {
        "schema_version": 1, "release_id": case.release["release_id"],
        "installed_at": case.clock().isoformat(), "receipt_key": observer.INSTALLATION_KEY,
        "timer": {"active": True, "enabled": True, "schedule": observer.TIMER_SCHEDULE},
    })

    def crash(stage, record):
        if stage == "after_receipt":
            assert record["status"] == "accepted"
            assert not (case.directory / "completed.json").exists()
            raise InterruptedSend()

    with pytest.raises(InterruptedSend):
        case.run(fault_hook=crash)
    assert len(case.wire.messages) == 1
    before = get_json(case.directory / "notification-ledger.json")["deliveries"]
    reopened = case.run()
    assert len(case.wire.messages) == 1
    assert reopened["terminal"] == "completed"
    assert get_json(case.directory / "completed.json")["terminal"] == "completed"
    assert get_json(case.directory / "notification-ledger.json")["deliveries"] == before


def test_another_process_holds_the_full_observer_lock_before_any_probe_or_send(server_case):
    case = server_case
    case.directory.mkdir(mode=0o700)
    ready = case.directory.parent / "independent-lock-ready"
    root = Path(__file__).resolve().parents[2]
    child_code = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from scripts.server_acceptance import StateLock
with StateLock(Path(sys.argv[2])):
    Path(sys.argv[3]).write_text('locked', encoding='utf-8')
    sys.stdin.read(1)
"""
    child = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", child_code, str(root), str(case.directory), str(ready)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists(), "The isolated child process did not acquire the real OS lock"
        with pytest.raises(observer.Busy):
            case.run()
        assert case.probe_calls == case.public_reads == 0
        assert case.wire.requests == []
        assert not (case.directory / "state.json").exists()
        assert not (case.directory / "notification-ledger.json").exists()
    finally:
        try:
            _, error = child.communicate(input="x", timeout=5)
        except subprocess.TimeoutExpired:
            # This exact process was created above solely for the test lock.
            child.kill()
            _, error = child.communicate(timeout=5)
        assert child.returncode == 0, error
    assert case.run()["status"] == "ok"
    assert not case.wire.messages


def test_damaged_existing_ledger_is_retained_and_never_treated_as_a_new_job(server_case):
    case = server_case
    assert case.run()["status"] == "ok"
    ledger = case.directory / "notification-ledger.json"
    damaged = b'{"schema_version":'
    ledger.write_bytes(damaged)
    previous_calls = case.probe_calls, case.public_reads, len(case.wire.requests)
    result = case.run()
    assert result["status"] == "failed"
    assert ledger.read_bytes() == damaged
    assert (case.probe_calls, case.public_reads, len(case.wire.requests)) == previous_calls
    assert not case.wire.messages and not (case.directory / "completed.json").exists()


def test_installation_claim_alone_cannot_send_migration_success_when_timer_is_inactive(server_case):
    case = server_case
    case.directory.mkdir(mode=0o700)
    put_json(case.directory / "installation.json", {
        "schema_version": 1, "release_id": case.release["release_id"],
        "installed_at": case.clock().isoformat(), "receipt_key": observer.INSTALLATION_KEY,
        "timer": {"active": True, "enabled": True, "schedule": observer.TIMER_SCHEDULE},
    })
    result = case.run()
    assert result["status"] == "attention_required"
    assert observer.INSTALLATION_KEY not in result["active_candidate_keys"]
    assert not any(observer.INSTALLATION_KEY in item["candidate_keys"] for item in result["deliveries"])
    for message in case.wire.messages:
        assert "迁移完成" not in message["content"]
    assert result["terminal"] is None


@pytest.fixture
def connected_installation(install_rig, server_case, monkeypatch):
    """Use real runner/ledger/client, with only systemd and external I/O replaced."""
    case, machine = server_case, install_rig.systemd
    case.directory = installer.DATA
    case.release["release_id"] = RID
    monkeypatch.setattr(installer, "now", case.clock)
    bridge = SimpleNamespace(case=case, machine=machine, reports=[], crash_on_setup=False)

    def real_service(args, **kwargs):
        if args != ["systemctl", "start", installer.SERVICE]:
            return machine.execute(args, **kwargs)
        machine.commands.append(args)
        machine.runs += 1
        case.timer_active = machine.active and machine.enabled
        setup = (case.directory / "installation.json").exists()
        if setup:
            machine.active_at_setup.append(case.timer_active)

        def crash(stage, record):
            if setup and bridge.crash_on_setup and stage == "after_provider_acceptance":
                raise InterruptedSend()

        try:
            report = case.run(fault_hook=crash)
        except InterruptedSend as error:
            machine.result, machine.exit_status = "exit-code", "1"
            # systemctl represents a failed child as an ordinary command error.
            raise RuntimeError("controlled service crash after provider acceptance") from error
        bridge.reports.append(report)
        machine.result, machine.exit_status = "success", "0"
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(installer, "run", real_service)
    return bridge


def test_installer_and_real_runner_create_one_receipt_after_timer_enable(connected_installation):
    bridge = connected_installation
    case, machine = bridge.case, bridge.machine
    installed = installer.install(RID)
    assert installed["status"] == installer.SUCCESS
    assert machine.runs == 2 and machine.active_at_setup == [True]
    assert bridge.reports[0]["deliveries"] == []
    assert observer.INSTALLATION_KEY not in bridge.reports[0]["active_candidate_keys"]
    assert bridge.reports[1]["deliveries"][0]["status"] == "accepted"
    assert bridge.reports[1]["deliveries"][0]["installation_release_id"] == RID
    assert len(case.wire.messages) == 1
    starts = [i for i, args in enumerate(machine.commands) if args == ["systemctl", "start", installer.SERVICE]]
    enabled = machine.commands.index(["systemctl", "enable", "--now", installer.TIMER])
    assert starts[0] < enabled < starts[1]
    before = (case.directory / "notification-ledger.json").read_bytes()
    repeated = installer.install(RID)
    assert repeated["status"] == "already_installed"
    assert machine.runs == 2 and len(case.wire.messages) == 1
    assert (case.directory / "notification-ledger.json").read_bytes() == before


def test_setup_crash_with_stale_last_run_cannot_resend_through_install_retry(connected_installation):
    bridge = connected_installation
    case, machine = bridge.case, bridge.machine
    bridge.crash_on_setup = True
    with pytest.raises(RuntimeError, match="controlled service crash"):
        installer.install(RID)
    assert len(case.wire.messages) == 1
    assert get_json(case.directory / "last-run.json")["deliveries"] == []
    ledger = get_json(case.directory / "notification-ledger.json")
    assert [item["status"] for item in ledger["deliveries"].values()] == ["intent"]
    assert not (case.directory / "installation.json").exists()
    assert not machine.active and not machine.enabled

    # The last report predates the failed send. Recovering the real ledger must
    # expose the uncertainty before a replacement setup can send anything.
    bridge.crash_on_setup = False
    with pytest.raises(RuntimeError, match="uncertain"):
        installer.install(RID)
    assert machine.runs == 3 and len(case.wire.messages) == 1
    assert [item["status"] for item in get_json(case.directory / "last-run.json")["deliveries"]] == ["unknown"]
    ledger_before = (case.directory / "notification-ledger.json").read_bytes()
    with pytest.raises(RuntimeError, match="uncertain"):
        installer.install(RID)
    assert machine.runs == 3 and len(case.wire.messages) == 1
    assert (case.directory / "notification-ledger.json").read_bytes() == ledger_before
    assert not (installer.STATE / "acceptance.json").exists()
    assert not (case.directory / "completed.json").exists()
