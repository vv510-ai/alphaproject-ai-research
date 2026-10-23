"""Durable server observer behavior; all HTTP and Feishu results are synthetic."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
from urllib.request import OpenerDirector

import httpx
import pytest

from backend.app.feishu import FeishuFailure, Target
from backend.app.feishu_config import ConfigFailure, FeishuConfig
from scripts import server_acceptance as runner


NOW = datetime(2026, 10, 22, 7, 36, tzinfo=timezone.utc)
RELEASE = {"release_id": "lab-server-observer-test", "source_sha256": "a" * 64, "app_version": "0.10.2"}
EVENT_ID = "78923b27-d2c5-46e5-971c-367264fbc916"
COLLECTION = {"key": "collection:test-scope", "kind": "collection_passed"}
EVENT = {"key": "event:" + EVENT_ID + ":accepted", "kind": "natural_event_accepted",
         "event": {"id": EVENT_ID, "direction": "up", "change_pct": "3.2"}}
FAULT = {"key": "probe_error:stable", "kind": "probe_error"}
CHANGE = {"key": "configuration:changed", "kind": "rule_changed"}
CALENDARS = ("{ OnCalendar=Mon..Fri *-*-* 09:35:00 Asia/Shanghai ; next_elapse=Fri 2026-10-23 09:35:00 CST } "
             "{ OnCalendar=Mon..Fri *-*-* 15:35:00 Asia/Shanghai ; next_elapse=Fri 2026-10-23 15:35:00 CST }")
# systemd v255 prints one property line per TimersCalendar array entry:
# https://github.com/systemd/systemd/blob/v255/src/systemctl/systemctl-show.c#L1239-L1248
CALENDAR_VALUES = [
    "{ OnCalendar=Mon..Fri *-*-* 15:35:00 Asia/Shanghai ; next_elapse=Thu 2026-10-22 15:35:00 CST }",
    "{ OnCalendar=Mon..Fri *-*-* 09:35:00 Asia/Shanghai ; next_elapse=Thu 2026-10-22 09:35:00 CST }",
]
SYSTEMD_255_CALENDAR_LINES = "\n".join("TimersCalendar=" + value for value in CALENDAR_VALUES)


@pytest.fixture(autouse=True)
def no_external_http(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Observer tests must not contact external services")
    monkeypatch.setattr(OpenerDirector, "open", blocked)
    monkeypatch.setattr(httpx.Client, "send", blocked)


def report(previous, candidates=(), *, passed=False, changed=False):
    state = deepcopy(previous)
    state.setdefault("notified", {})
    state.setdefault("candidates", {})
    for item in state["candidates"].values():
        item["active"] = False
    for item in candidates:
        state["candidates"][item["key"]] = {**deepcopy(item), "active": True}
    return {"continuous_collection": {"status": "passed" if passed else "pending", "cumulative_pass": passed,
                                      "scope": "test-scope"},
            "natural_alert": {"status": "passed" if passed else "pending", "cumulative_pass": passed,
                              "history_complete": True, "accepted_event_ids": [EVENT_ID] if passed else []},
            "rule_configuration": {"status": "changed" if changed else "matched", "changed_during_run": changed},
            "notification_candidates": [deepcopy(value) for key, value in state["candidates"].items()
                                        if value["active"] and key not in state["notified"]], "state": state}


class PublicClient:
    def __init__(self, config):
        self.public = {"configured": True, "enabled": True, "verified_at": NOW.isoformat(),
                       "target_fingerprint_sha256": runner.probe.digest(config.fingerprint)}
        self.requests = []
        self.failure = None

    def get(self, endpoint, params=None):
        self.requests.append((endpoint, params))
        if self.failure:
            raise self.failure
        assert endpoint == "api/notifications"
        return deepcopy(self.public)


class FakeFeishu:
    def __init__(self, config):
        self.config, self.sent, self.verifications = config, [], 0
        self.send_failure = self.verify_failure = None

    def verify_target(self):
        self.verifications += 1
        if self.verify_failure:
            raise self.verify_failure
        return Target("Private test group", "Test bot", self.config.owner_id)

    def send_test(self, request_id, text, detail_url=None):
        self.sent.append((request_id, text, detail_url))
        if self.send_failure:
            raise self.send_failure
        return "om_synthetic" + str(len(self.sent))


class Harness:
    def __init__(self, tmp_path):
        self.directory = tmp_path / "observer"
        self.now = NOW
        self.config = FeishuConfig("cli_TestApp", "PRIVATE_SYNTHETIC_SECRET", "oc_TestGroup", "ou_TestOwner",
                                   "Private test group", "Test bot", NOW.isoformat())
        self.client, self.feishu = PublicClient(self.config), FakeFeishu(self.config)
        self.metadata = {"activation": "manual-start", "planned_at": None, "triggered_at": None,
                         "timer": {"active": True, "enabled": True, "schedule": runner.TIMER_SCHEDULE}}

    def install(self, **updates):
        self.directory.mkdir(mode=0o700, exist_ok=True)
        value = {"schema_version": 1, "release_id": RELEASE["release_id"], "installed_at": self.now.isoformat(),
                 "receipt_key": runner.INSTALLATION_KEY,
                 "timer": {"active": True, "enabled": True, "schedule": runner.TIMER_SCHEDULE}}
        value.update(updates)
        runner.write_json(self.directory / "installation.json", value)

    def run(self, candidates=(), *, passed=False, changed=False, probe_runner=None, **kwargs):
        self.now += timedelta(seconds=1)
        def fake_probe(client, **options):
            assert options["samples"] == 3 and options["interval"] == 65
            assert options["expected_generation"] == runner.probe.DEFAULT_GENERATION
            assert options["expected_rule_version"] == 2 and options["since"] == runner.probe.DEFAULT_SINCE
            return report(options["previous_state"], candidates, passed=passed, changed=changed)
        return runner.run_once(self.directory, probe_client=self.client, feishu_client=self.feishu,
                               config=self.config, release=RELEASE, probe_runner=probe_runner or fake_probe,
                               clock=lambda: self.now, sleeper=lambda _: None, monotonic=lambda: 0,
                               metadata_reader=lambda *_: deepcopy(self.metadata), **kwargs)

    def ledger(self):
        return runner.read_json(self.directory / "notification-ledger.json")


@pytest.fixture
def job(tmp_path):
    return Harness(tmp_path)


def test_pending_runs_remain_quiet_and_keep_real_times(job):
    first, second = job.run(), job.run()
    assert first["status"] == second["status"] == "ok"
    assert not job.feishu.sent and second["terminal"] is None
    assert second["started_at"] == second["completed_at"]
    assert second["schedule"]["triggered_at"] is None
    assert len(list((job.directory / "runs").glob("*.json"))) == 2


def test_real_receipt_required_and_accepted_results_are_not_sent_twice(job):
    first = job.run([COLLECTION])
    second = job.run([COLLECTION])
    assert first["deliveries"][0]["status"] == "accepted"
    assert first["deliveries"][0]["provider_message_id"] == "om_synthetic1"
    assert len(job.feishu.sent) == 1
    assert second["active_candidate_keys"] == [COLLECTION["key"]]
    assert job.ledger()["candidates"][COLLECTION["key"]]["active"] is True
    assert COLLECTION["key"] in runner.read_json(job.directory / "state.json")["probe_state"]["notified"]


def test_setup_receipt_describes_pending_market_goals(job):
    job.install()
    first, second = job.run(), job.run()
    assert first["deliveries"][0]["candidate_keys"] == [runner.INSTALLATION_KEY]
    assert first["deliveries"][0]["installation_release_id"] == RELEASE["release_id"]
    assert len(job.feishu.sent) == 1 and second["terminal"] is None
    assert "待交易时段验证" in job.feishu.sent[0][1] and "待自然事件发生" in job.feishu.sent[0][1]
    assert "自然 3% 提醒验收通过" not in job.feishu.sent[0][1]


@pytest.mark.parametrize("property,value", [("active", False), ("enabled", False), ("schedule", None),
                                          ("schedule", "Mon..Fri 09:35,15:35 UTC")])
def test_setup_requires_live_active_enabled_exact_schedule(job, property, value):
    job.install()
    job.metadata["timer"][property] = value
    result = job.run()
    assert runner.INSTALLATION_KEY not in result["active_candidate_keys"]
    assert result["status"] == "attention_required"
    assert "installation_timer_not_verified" in result["errors"]
    assert not any("迁移完成" in item[1] for item in job.feishu.sent)


@pytest.mark.parametrize("updates", [{"release_id": "lab-other"}, {"installed_at": "2026-10-23T00:00:00Z"},
                                     {"installed_at": "2026-10-21T00:00:00"}, {"receipt_key": "changed"}])
def test_setup_rejects_invalid_or_different_release_record(job, updates):
    job.install(**updates)
    result = job.run()
    assert "installation_record_invalid" in result["errors"]
    assert not any("迁移完成" in item[1] for item in job.feishu.sent)


def test_two_goals_finish_only_after_setup_and_all_corresponding_receipts(job):
    first = job.run([COLLECTION, EVENT], passed=True)
    assert first["terminal_pending"] == "completed" and first["terminal"] is None
    assert not (job.directory / "completed.json").exists()
    job.install()
    second = job.run([COLLECTION, EVENT], passed=True)
    assert second["terminal"] == "completed"
    assert len(job.feishu.sent) == 2
    marker = runner.read_json(job.directory / "completed.json")
    assert marker["target"]["generation"] == runner.probe.DEFAULT_GENERATION
    assert len(marker["accepted_request_ids"]) == 2
    count = len(job.client.requests)
    assert job.run()["already_terminal"] is True
    assert len(job.client.requests) == count and len(job.feishu.sent) == 2


def test_acknowledged_active_rule_change_still_suspends_after_setup(job):
    first = job.run([CHANGE], changed=True)
    assert first["terminal_pending"] == "suspended" and first["terminal"] is None
    job.install()
    second = job.run([CHANGE], changed=True)
    assert second["probe"]["notification_candidates"] == []
    assert second["terminal"] == "suspended"
    assert (job.directory / "suspended.json").exists() and len(job.feishu.sent) == 2


def test_acknowledged_but_active_fault_prevents_later_completion(job):
    first = job.run([COLLECTION, EVENT, FAULT], passed=True)
    assert first["terminal_pending"] is None
    job.install()
    second = job.run([COLLECTION, EVENT, FAULT], passed=True)
    assert second["probe"]["notification_candidates"] == []
    assert second["terminal"] is None and second["terminal_pending"] is None
    assert not (job.directory / "completed.json").exists()
    third = job.run([COLLECTION, EVENT], passed=True)
    assert third["terminal"] == "completed" and len(job.feishu.sent) == 2


@pytest.mark.parametrize("unknown", [True, False])
def test_failed_or_unknown_notification_never_marks_goal_complete(job, unknown):
    job.install()
    job.feishu.send_failure = FeishuFailure("secret provider error", unknown=unknown)
    result = job.run([COLLECTION, EVENT], passed=True)
    assert result["status"] == "attention_required" and result["terminal"] is None
    assert not (job.directory / "completed.json").exists()
    assert not runner.read_json(job.directory / "state.json")["probe_state"]["notified"]


def test_explicit_no_submission_has_bounded_cross_job_attempts(job):
    job.feishu.send_failure = FeishuFailure("Explicit rejection", unknown=False)
    for _ in range(5):
        result = job.run([COLLECTION])
    assert len(job.feishu.sent) == runner.MAX_SEND_ATTEMPTS
    assert len({item[0] for item in job.feishu.sent}) == runner.MAX_SEND_ATTEMPTS
    assert job.ledger()["candidates"][COLLECTION["key"]]["status"] == "exhausted"
    assert result["status"] == "attention_required"


@pytest.mark.parametrize("failure", [FeishuFailure("Unknown", unknown=True), RuntimeError("Unexpected"), None])
def test_unknown_results_never_retry_automatically(job, failure):
    job.feishu.send_failure = failure
    if failure is None:
        job.feishu.send_test = lambda *args: "untrusted receipt"
    first = job.run([COLLECTION])
    job.feishu.send_failure = None
    for _ in range(3):
        result = job.run([COLLECTION])
    assert first["deliveries"][0]["status"] == result["deliveries"][0]["status"] == "unknown"
    assert job.ledger()["candidates"][COLLECTION["key"]]["attempt_count"] == 1
    assert not runner.read_json(job.directory / "state.json")["probe_state"]["notified"]


def test_resolved_unsubmitted_fault_is_retired_instead_of_retried(job):
    job.feishu.send_failure = FeishuFailure("No submission")
    job.run([FAULT])
    job.feishu.send_failure = None
    result = job.run()
    assert len(job.feishu.sent) == 1 and result["active_candidate_keys"] == []
    assert job.ledger()["candidates"][FAULT["key"]]["status"] == "retired"


class SimulatedPowerLoss(BaseException):
    pass


@pytest.mark.parametrize("stage,expected_status,notified", [("after_intent", "unknown", False),
                                                          ("after_provider_acceptance", "unknown", False),
                                                          ("after_receipt", "accepted", True),
                                                          ("after_state", "accepted", True)])
def test_crash_windows_preserve_uncertainty_and_never_duplicate(job, stage, expected_status, notified):
    def crash(actual, record):
        if actual == stage:
            raise SimulatedPowerLoss()
    with pytest.raises(SimulatedPowerLoss):
        job.run([COLLECTION], fault_hook=crash)
    sends_before_restart = len(job.feishu.sent)
    result = job.run([COLLECTION])
    assert len(job.feishu.sent) == sends_before_restart
    assert job.ledger()["candidates"][COLLECTION["key"]]["status"] == expected_status
    assert (COLLECTION["key"] in runner.read_json(job.directory / "state.json")["probe_state"]["notified"]) is notified
    if not notified:
        assert result["status"] == "attention_required"


def test_interrupted_probe_produces_one_stable_fault_once(job):
    def crash_probe(*args, **kwargs):
        raise SimulatedPowerLoss()
    with pytest.raises(SimulatedPowerLoss):
        job.run(probe_runner=crash_probe)
    result = job.run()
    assert result["interrupted_previous_run"]["phase"] == "probing"
    assert result["active_candidate_keys"] == ["server-job:previous_run_interrupted"]
    job.run()
    assert len(job.feishu.sent) == 1


@pytest.mark.parametrize("field,value", [("enabled", False), ("configured", False), ("verified_at", None),
                                       ("target_fingerprint_sha256", "0" * 64)])
def test_explicit_public_disable_or_change_stays_revoked_during_outage(job, field, value):
    job.run()
    job.client.public[field] = value
    closed = job.run([FAULT])
    assert not closed["notification_gate"]["allowed"] and not job.feishu.sent
    job.client.failure = OSError("application unavailable")
    outage = job.run([FAULT])
    assert not outage["notification_gate"]["allowed"] and not job.feishu.sent
    assert job.ledger()["target_authorization"]["status"] == "revoked"


def test_fresh_same_target_revalidation_can_restore_authorized_fault_fallback(job):
    job.run()
    job.client.public["enabled"] = False
    job.run()
    job.client.public["enabled"] = True
    job.run()
    job.client.failure = OSError("application unavailable")
    result = job.run([FAULT, COLLECTION])
    assert result["notification_gate"]["allowed"] and result["notification_gate"]["fault_only"]
    assert result["deliveries"][0]["candidate_keys"] == [FAULT["key"]]
    assert len(job.feishu.sent) == 1


def test_outage_without_prior_same_target_authorization_cannot_send(job):
    job.client.failure = OSError("unavailable")
    result = job.run([FAULT])
    assert not result["notification_gate"]["allowed"] and not job.feishu.sent


def test_changed_private_credential_target_cannot_rebind(job):
    job.run()
    original = job.ledger()["target_fingerprint_sha256"]
    object.__setattr__(job.config, "chat_id", "oc_OtherGroup")
    job.client.public["target_fingerprint_sha256"] = runner.probe.digest(job.config.fingerprint)
    result = job.run([FAULT])
    assert result["notification_gate"]["code"] == "credential_target_changed"
    assert not job.feishu.sent and job.ledger()["target_fingerprint_sha256"] == original


@pytest.mark.parametrize("failure", [FeishuFailure("changed group"), RuntimeError("PRIVATE_INTERNAL_ERROR")])
def test_target_verification_failures_stop_sends_without_leaking_errors(job, failure):
    job.feishu.verify_failure = failure
    result = job.run([FAULT])
    assert result["notification_gate"]["code"] == "private_group_verification_failed"
    assert not job.feishu.sent
    all_evidence = "\n".join(path.read_text(encoding="utf-8") for path in job.directory.rglob("*.json"))
    assert str(failure) not in all_evidence


def test_original_target_digest_and_secret_exclusion(job):
    job.run([EVENT])
    assert job.ledger()["target_fingerprint_sha256"] == runner.probe.digest(job.config.fingerprint)
    content = "\n".join(path.read_text(encoding="utf-8") for path in job.directory.rglob("*.json"))
    for private in (job.config.app_id, job.config.app_secret, job.config.owner_id, job.config.chat_id):
        assert private not in content
    assert "https://8.155.130.86/lab/#event=" + EVENT_ID in job.feishu.sent[0][1]


@pytest.mark.parametrize("event_id", ["not-a-uuid", "https://attacker.example/", None, "../secret"])
def test_event_detail_link_accepts_only_canonical_uuid(event_id):
    candidate = deepcopy(EVENT)
    candidate["event"]["id"] = event_id
    assert "#event=" not in runner.render_message([candidate], None, NOW)


def test_corrupt_or_missing_ledger_fails_closed(job):
    job.run()
    (job.directory / "notification-ledger.json").write_text("{broken", encoding="utf-8")
    result = job.run([EVENT])
    assert result["status"] == "failed" and not job.feishu.sent
    (job.directory / "notification-ledger.json").unlink()
    result = job.run([EVENT])
    assert result["errors"] == ["notification_ledger_missing"] and not job.feishu.sent


def test_forged_receipt_without_matching_delivery_is_rejected(job):
    job.run([COLLECTION])
    ledger = job.ledger()
    ledger["deliveries"].clear()
    runner.write_json(job.directory / "notification-ledger.json", ledger)
    result = job.run([COLLECTION])
    assert result["status"] == "failed" and len(job.feishu.sent) == 1


def test_service_config_has_no_development_fallback(monkeypatch):
    monkeypatch.setattr(runner, "service_credential", lambda name: None)
    with pytest.raises(ConfigFailure, match="service_feishu_credential_required"):
        runner.load_service_config()


def test_no_valid_release_stops_before_probe_or_send(job):
    bad = dict(RELEASE, source_sha256="invalid")
    result = runner.run_once(job.directory, probe_client=job.client, feishu_client=job.feishu,
                             config=job.config, release=bad, clock=lambda: NOW,
                             probe_runner=lambda *a, **kw: pytest.fail("Should not probe"))
    assert result["status"] == "failed" and not job.feishu.sent and not job.client.requests


def test_probe_has_bounded_wall_time_and_fault_is_safe(job):
    def over_budget(client, **kwargs):
        kwargs["sleep"](runner.PROBE_BUDGET_SECONDS)
    result = job.run(probe_runner=over_budget)
    assert "probe_time_budget_exhausted" in result["errors"]
    assert result["active_candidate_keys"] == ["server-job:probe_time_budget_exhausted"]
    assert result["status"] == "attention_required"


def systemd_commands(*, invocation="1" * 32, calendars=CALENDARS, calendar_lines=None,
                     active="active", enabled="enabled", last=None, substate="waiting"):
    calls = []
    last = last or str(int((NOW - timedelta(hours=6)).timestamp() * 1_000_000))
    def execute(arguments):
        calls.append(arguments)
        if arguments[:2] == ["systemctl", "is-active"]:
            return active
        if arguments[:2] == ["systemctl", "is-enabled"]:
            return enabled
        if arguments[0] == "busctl":
            values = {"LastTriggerUSec": last, "LastTriggerUSecMonotonic": "1000000",
                      "ExecMainStartTimestamp": str(int(NOW.timestamp() * 1_000_000)),
                      "ExecMainStartTimestampMonotonic": str(1_000_000 + int(NOW.timestamp() * 1_000_000) - int(last)),
                      "InactiveEnterTimestamp": "0", "InactiveEnterTimestampMonotonic": "0",
                      "InactiveExitTimestamp": str(int((NOW - timedelta(days=1)).timestamp() * 1_000_000))}
            return "\n".join("t " + values.get(name, "0") for name in arguments[6:])
        if runner.SERVICE in arguments:
            return "InvocationID=" + invocation + "\nActiveState=activating\nSubState=start"
        calendar_properties = "TimersCalendar=" + calendars if calendar_lines is None else calendar_lines
        return ("ActiveState=" + active + "\nUnitFileState=" + enabled + "\nLastTriggerUSec=prior display\n"
                + "SubState=" + substate + "\nUnit=" + runner.SERVICE + "\nPersistent=yes\n" + calendar_properties)
    return execute, calls


def test_direct_manual_run_does_not_claim_old_timer_trigger():
    command, _ = systemd_commands()
    result = runner.read_systemd_metadata(NOW, {}, command)
    assert result["activation"] == "manual-start" and result["triggered_at"] is None
    assert result["planned_at"] is None and result["last_trigger_at"] is not None
    assert result["timer"] == {"active": True, "enabled": True, "schedule": runner.TIMER_SCHEDULE}


def test_timer_activation_requires_matching_invocation_and_current_running_timer():
    actual = NOW - timedelta(minutes=1)
    environment = {"INVOCATION_ID": "1" * 32, "TRIGGER_UNIT": runner.TIMER,
                   "TRIGGER_TIMER_REALTIME_USEC": str(int(actual.timestamp() * 1_000_000))}
    command, calls = systemd_commands(last=environment["TRIGGER_TIMER_REALTIME_USEC"], substate="running")
    result = runner.read_systemd_metadata(NOW, environment, command)
    assert result["activation"] == "timer" and result["triggered_at"] == runner.iso(actual)
    assert result["planned_at"] == "2026-10-22T07:35:00+00:00"
    assert result["planned_at_origin"].startswith("inferred-")
    assert ["systemctl", "is-active", runner.TIMER] in calls
    assert ["systemctl", "is-enabled", runner.TIMER] in calls


@pytest.mark.parametrize("change", [{"INVOCATION_ID": "2" * 32}, {"TRIGGER_UNIT": "other.timer"},
                                   {"TRIGGER_TIMER_REALTIME_USEC": "invalid"},
                                   {"TRIGGER_TIMER_REALTIME_USEC": str(int((NOW + timedelta(seconds=1)).timestamp() * 1_000_000))}])
def test_environment_without_current_running_timer_does_not_prove_activation(change):
    environment = {"INVOCATION_ID": "1" * 32, "TRIGGER_UNIT": runner.TIMER,
                   "TRIGGER_TIMER_REALTIME_USEC": str(int(NOW.timestamp() * 1_000_000))}
    environment.update(change)
    command, _ = systemd_commands()
    result = runner.read_systemd_metadata(NOW, environment, command)
    assert result["activation"] == "unknown" and result["triggered_at"] is None and result["planned_at"] is None


@pytest.mark.parametrize("calendars", ["", CALENDARS.replace("Asia/Shanghai", "UTC"),
                                      CALENDARS.replace("Mon..Fri", "Mon..Sun"), CALENDARS + CALENDARS,
                                      CALENDARS.split("} ")[0] + "}", CALENDARS + "unexpected"])
def test_effective_calendar_must_have_only_the_two_authorized_slots(calendars):
    command, _ = systemd_commands(calendars=calendars)
    result = runner.read_systemd_metadata(NOW, {}, command)
    assert result["timer"]["schedule"] is None


@pytest.mark.parametrize("raw", [SYSTEMD_255_CALENDAR_LINES,
                                "TimersCalendar=" + CALENDARS,
                                "\n".join(reversed(SYSTEMD_255_CALENDAR_LINES.splitlines()))])
def test_systemd_calendar_multiline_or_single_line_preserves_every_value(raw):
    command, _ = systemd_commands(calendar_lines=raw)
    result = runner.read_systemd_metadata(NOW, {}, command)
    assert result["timer"]["schedule"] == runner.TIMER_SCHEDULE
    assert result["timer_calendar_values"] == [line.split("=", 1)[1] for line in raw.splitlines()]


@pytest.mark.parametrize("raw", [SYSTEMD_255_CALENDAR_LINES + "\nTimersCalendar=" + CALENDAR_VALUES[0],
                                SYSTEMD_255_CALENDAR_LINES + "\nTimersCalendar=",
                                SYSTEMD_255_CALENDAR_LINES.replace("09:35:00", "10:35:00"),
                                SYSTEMD_255_CALENDAR_LINES.replace("15:35:00 Asia/Shanghai", "15:35:00 UTC"),
                                "TimersCalendar=" + CALENDAR_VALUES[0]])
def test_multiline_calendar_cannot_hide_extra_missing_or_changed_slots(raw):
    command, _ = systemd_commands(calendar_lines=raw)
    result = runner.read_systemd_metadata(NOW, {}, command)
    assert result["timer"]["schedule"] is None


def test_reinstall_after_calendar_parse_failure_keeps_prior_fault_receipt(job):
    job.install()
    job.metadata["timer"]["schedule"] = None
    failed = job.run()
    fault_key = "server-job:installation_timer_not_verified"
    original = failed["deliveries"][0]
    assert original["candidate_keys"] == [fault_key] and original["status"] == "accepted"
    (job.directory / "installation.json").unlink()
    job.run()
    assert len(job.feishu.sent) == 1
    command, _ = systemd_commands(calendar_lines=SYSTEMD_255_CALENDAR_LINES)
    job.metadata = runner.read_systemd_metadata(NOW, {}, command)
    job.install()
    repaired = job.run()
    assert repaired["status"] == "ok" and len(job.feishu.sent) == 2
    assert repaired["deliveries"][0] == original
    assert repaired["deliveries"][1]["candidate_keys"] == [runner.INSTALLATION_KEY]
    assert job.ledger()["candidates"][fault_key]["attempt_count"] == 1
    assert job.ledger()["candidates"][fault_key]["active"] is False


def test_real_os_lock_blocks_a_second_process(tmp_path):
    directory = tmp_path / "locked"
    directory.mkdir(mode=0o700)
    code = ("import sys\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1])\n"
            "from scripts.server_acceptance import StateLock, Busy\n"
            "try:\n with StateLock(Path(sys.argv[2])): print('unexpected')\n"
            "except Busy:\n print('locked')\n")
    with runner.StateLock(directory):
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(runner.ROOT), str(directory)],
                                capture_output=True, text=True, timeout=15, check=True)
        assert result.stdout.strip() == "locked"
    with runner.StateLock(directory):
        pass
    assert (directory / ".job.lock").exists()
