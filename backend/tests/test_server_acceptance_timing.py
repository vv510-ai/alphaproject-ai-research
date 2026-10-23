"""Timer evidence regressions; command results are controlled, no remote writes."""

from datetime import datetime, timedelta, timezone

import pytest

from scripts import server_acceptance as runner


STARTED = datetime.fromisoformat("2026-10-22T07:35:01.340547+00:00")
TRIGGERED = datetime.fromisoformat("2026-10-22T07:35:00.853992+00:00")
EXEC_STARTED = datetime.fromisoformat("2026-10-22T07:35:00.861373+00:00")
STALE_TRIGGER = datetime.fromisoformat("2026-10-22T01:35:00.853974+00:00")
INVOCATION = "18106f1a825f406594b8f5aa44402bd2"
TRIGGER_MONOTONIC = 1781674916951
EXEC_MONOTONIC = 1781674924330


def usec(value):
    return str(int((value - datetime(1970, 1, 1, tzinfo=timezone.utc)) / timedelta(microseconds=1)))


class Manager:
    def __init__(self, *, trigger=TRIGGERED, exec_started=EXEC_STARTED):
        self.service = {"InvocationID": INVOCATION, "ActiveState": "activating", "SubState": "start"}
        self.timer = {"ActiveState": "active", "UnitFileState": "enabled", "SubState": "running",
                      "Unit": runner.SERVICE, "Persistent": "yes", "LastTriggerUSec": "display only"}
        self.calendars = [
            "{ OnCalendar=Mon..Fri *-*-* 15:35:00 Asia/Shanghai ; next_elapse=Thu 2026-10-22 07:35:00 UTC }",
            "{ OnCalendar=Mon..Fri *-*-* 09:35:00 Asia/Shanghai ; next_elapse=Fri 2026-10-23 01:35:00 UTC }",
        ]
        self.numeric = {
            ("timer", "LastTriggerUSec"): usec(trigger),
            ("timer", "LastTriggerUSecMonotonic"): str(TRIGGER_MONOTONIC),
            ("timer", "InactiveExitTimestamp"): usec(trigger - timedelta(days=1)),
            ("service", "ExecMainStartTimestamp"): usec(exec_started),
            ("service", "ExecMainStartTimestampMonotonic"): str(EXEC_MONOTONIC),
            ("service", "InactiveEnterTimestamp"): usec(trigger - timedelta(hours=6) + timedelta(seconds=3)),
            ("service", "InactiveEnterTimestampMonotonic"): str(TRIGGER_MONOTONIC - 6 * 3600_000_000 + 3_000_000),
        }
        self.after_invocation = INVOCATION
        self.service_reads = 0
        self.calls = []

    def __call__(self, arguments):
        self.calls.append(arguments)
        if arguments[:2] == ["systemctl", "is-active"]:
            return "active"
        if arguments[:2] == ["systemctl", "is-enabled"]:
            return "enabled"
        if arguments[0] == "busctl":
            unit = "timer" if arguments[4].endswith("2etimer") else "service"
            return "\n".join("t " + self.numeric.get((unit, prop), "unavailable") for prop in arguments[6:])
        if runner.SERVICE in arguments:
            self.service_reads += 1
            if self.service_reads > 1:
                return "InvocationID=" + self.after_invocation
            return "\n".join(f"{key}={value}" for key, value in self.service.items())
        return "\n".join(f"{key}={value}" for key, value in self.timer.items()) + "\n" + "\n".join(
            "TimersCalendar=" + value for value in self.calendars)


def environment(trigger=TRIGGERED, **changes):
    result = {"INVOCATION_ID": INVOCATION, "TRIGGER_UNIT": runner.TIMER,
              "TRIGGER_TIMER_REALTIME_USEC": usec(trigger),
              "TRIGGER_TIMER_MONOTONIC_USEC": str(TRIGGER_MONOTONIC)}
    result.update(changes)
    return result


def test_afternoon_uses_current_manager_trigger_while_preserving_stale_environment():
    """Real Oct 22 wall-clock/Invocation values; current manager states model the active run."""
    manager = Manager()
    env = environment(STALE_TRIGGER, TRIGGER_TIMER_MONOTONIC_USEC=str(TRIGGER_MONOTONIC - 6 * 3600_000_000))
    result = runner.read_systemd_metadata(STARTED, env, manager)
    assert result["activation"] == "timer"
    assert result["triggered_at"] == runner.iso(TRIGGERED)
    assert result["planned_at"] == "2026-10-22T07:35:00+00:00"
    assert result["trigger_environment"]["TRIGGER_TIMER_REALTIME_USEC"] == usec(STALE_TRIGGER)
    assert result["trigger_environment_status"] == "stale"
    assert result["activation_evidence"] == "systemd-manager-current-timer-invocation"


def test_manual_service_start_does_not_reuse_retained_timer_environment():
    manager = Manager(trigger=STALE_TRIGGER)
    manager.timer["SubState"] = "waiting"
    result = runner.read_systemd_metadata(STARTED, environment(STALE_TRIGGER), manager)
    assert result["activation"] == "unknown"
    assert result["triggered_at"] is None and result["planned_at"] is None
    assert result["last_trigger_at"] == runner.iso(STALE_TRIGGER)


def test_manual_direct_process_does_not_reuse_last_manager_trigger():
    result = runner.read_systemd_metadata(STARTED, {}, Manager())
    assert result["activation"] == "manual-start"
    assert result["triggered_at"] is None and result["planned_at"] is None


@pytest.mark.parametrize("env", [
    {},
    environment(TRIGGER_TIMER_REALTIME_USEC="invalid"),
    environment(TRIGGER_TIMER_REALTIME_USEC=""),
    environment(TRIGGER_TIMER_REALTIME_USEC=usec(STARTED + timedelta(days=1))),
    environment(TRIGGER_UNIT="other.timer"),
])
def test_bad_environment_never_overrides_correlated_manager_facts(env):
    # A systemd invocation is still mandatory; absent trigger environment is allowed.
    env["INVOCATION_ID"] = INVOCATION
    result = runner.read_systemd_metadata(STARTED, env, Manager())
    assert result["activation"] == "timer"
    assert result["triggered_at"] == runner.iso(TRIGGERED)
    assert result["trigger_environment_status"] in {"missing", "invalid", "future", "different-unit"}


@pytest.mark.parametrize("update", [
    {"InvocationID": "2" * 32}, {"InvocationID": ""},
    {"ActiveState": "inactive", "SubState": "dead"},
])
def test_missing_mismatched_or_inactive_service_cannot_be_correlated(update):
    manager = Manager()
    manager.service.update(update)
    result = runner.read_systemd_metadata(STARTED, environment(), manager)
    assert result["activation"] == "unknown"
    assert result["triggered_at"] is None and result["planned_at"] is None


def test_invocation_change_during_metadata_collection_is_unknown():
    manager = Manager()
    manager.after_invocation = "2" * 32
    result = runner.read_systemd_metadata(STARTED, environment(), manager)
    assert result["activation"] == "unknown"
    assert result["triggered_at"] is None


@pytest.mark.parametrize("field,value", [
    ("SubState", "waiting"), ("SubState", "elapsed"), ("SubState", ""),
    ("Unit", "other.service"), ("ActiveState", "inactive"),
])
def test_timer_state_and_target_must_describe_this_running_service(field, value):
    manager = Manager()
    manager.timer[field] = value
    result = runner.read_systemd_metadata(STARTED, environment(), manager)
    assert result["activation"] == "unknown"
    assert result["triggered_at"] is None and result["planned_at"] is None


@pytest.mark.parametrize("key,value", [
    (("timer", "LastTriggerUSec"), "invalid"),
    (("timer", "LastTriggerUSec"), "0"),
    (("timer", "LastTriggerUSec"), "99999999999999999999"),
    (("timer", "LastTriggerUSec"), usec(STARTED + timedelta(seconds=1))),
    (("timer", "LastTriggerUSecMonotonic"), "invalid"),
    (("timer", "LastTriggerUSecMonotonic"), str(EXEC_MONOTONIC + 1)),
    (("timer", "LastTriggerUSecMonotonic"), str(TRIGGER_MONOTONIC - 5_000_000)),
    (("service", "ExecMainStartTimestamp"), usec(TRIGGERED - timedelta(seconds=1))),
    (("service", "ExecMainStartTimestamp"), usec(STARTED + timedelta(seconds=1))),
    (("service", "ExecMainStartTimestampMonotonic"), "0"),
    (("service", "InactiveEnterTimestamp"), usec(TRIGGERED + timedelta(microseconds=1))),
    (("service", "InactiveEnterTimestampMonotonic"), str(TRIGGER_MONOTONIC + 1)),
])
def test_unordered_future_or_malformed_manager_times_remain_unknown(key, value):
    manager = Manager()
    manager.numeric[key] = value
    result = runner.read_systemd_metadata(STARTED, environment(), manager)
    assert result["activation"] == "unknown"
    assert result["triggered_at"] is None and result["planned_at"] is None


@pytest.mark.parametrize("trigger,expected_slot", [
    (datetime.fromisoformat("2026-10-23T04:00:00+00:00"), "2026-10-23T01:35:00+00:00"),
    (datetime.fromisoformat("2026-10-24T04:00:00+00:00"), "2026-10-23T07:35:00+00:00"),
    (datetime.fromisoformat("2026-10-26T00:00:00+00:00"), "2026-10-23T07:35:00+00:00"),
])
def test_persistent_catch_up_keeps_actual_trigger_and_inferred_previous_weekday_slot(trigger, expected_slot):
    manager = Manager(trigger=trigger, exec_started=trigger + timedelta(milliseconds=10))
    manager.numeric[("timer", "InactiveExitTimestamp")] = usec(trigger - timedelta(seconds=1))
    result = runner.read_systemd_metadata(trigger + timedelta(seconds=1), environment(STALE_TRIGGER), manager)
    assert result["activation"] == "timer"
    assert result["triggered_at"] == runner.iso(trigger)
    assert result["planned_at"] == expected_slot
    assert result["timer_run_timing"] == "persistent-catch-up-inferred"
    assert result["planned_at_origin"] == "inferred-most-recent-weekday-scheduled-slot"
    assert result["missed_occurrences_reconstructed"] is False


def test_first_invocation_can_have_no_previous_inactive_timestamp():
    manager = Manager()
    manager.numeric[("service", "InactiveEnterTimestamp")] = "0"
    manager.numeric[("service", "InactiveEnterTimestampMonotonic")] = "0"
    result = runner.read_systemd_metadata(STARTED, environment(), manager)
    assert result["activation"] == "timer"


def test_changed_calendar_keeps_trigger_fact_but_does_not_invent_planned_slot():
    manager = Manager()
    manager.calendars = [value.replace("Mon..Fri", "Mon..Sun") for value in manager.calendars]
    result = runner.read_systemd_metadata(STARTED, environment(), manager)
    assert result["activation"] == "timer"
    assert result["triggered_at"] == runner.iso(TRIGGERED)
    assert result["planned_at"] is None and result["timer_run_timing"] == "unknown"


def test_unavailable_manager_never_accepts_trigger_environment_alone():
    result = runner.read_systemd_metadata(STARTED, environment(), lambda args: None)
    assert result["activation"] == "unknown"
    assert result["triggered_at"] is None and result["planned_at"] is None
