"""Run the existing read-only acceptance probe and notify its verified results.

The service owns only its state directory. It never initializes the application,
NotificationService, a database or a model. Feishu credentials are read only from
the service credential mount; no development vault or environment secret fallback
is supported. The installer is responsible for the fixed release and timer.
"""

import argparse
from copy import deepcopy
from datetime import datetime, time as day_time, timedelta, timezone
import errno
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.feishu import FeishuClient, FeishuFailure
from backend.app.feishu_config import ConfigFailure, FeishuConfig, service_credential
from backend.app.runtime_config import release_info
from scripts import verify_live_monitor as probe

SERVICE = "alphaproject-lab-acceptance.service"
TIMER = "alphaproject-lab-acceptance.timer"
TIMER_SCHEDULE = "Mon..Fri 09:35,15:35 Asia/Shanghai"
INSTALLATION_KEY = "server-acceptance-installed-v1"
MAX_SEND_ATTEMPTS = 3
MAX_BATCH_KEYS = 12
MAX_FILE_BYTES = 64 * 1024 * 1024
PROBE_BUDGET_SECONDS = 480
SCHEMA_VERSION = 1
FAULT_KINDS = {"probe_error", "server_job_error", "collection_needs_attention",
               "event_delivery_needs_attention", "event_evidence_needs_attention"}
KNOWN_KINDS = FAULT_KINDS | {"collection_passed", "natural_event_accepted", "rule_changed",
                            "notification_configuration_changed", "installation_completed"}
STATUS_VALUES = {"intent", "accepted", "not_submitted", "unknown"}


class StateFailure(Exception):
    """A safe, fixed diagnostic code; never include file contents or credentials."""


class Busy(StateFailure):
    pass


class BudgetExceeded(RuntimeError):
    pass


def utcnow():
    return datetime.now(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat()


def _private_directory(path):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        raise StateFailure("state_directory_unsafe")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise StateFailure("state_directory_unsafe")
    if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise StateFailure("state_directory_permissions")
    return path


def read_json(path, *, optional=False):
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        if optional:
            return None
        raise StateFailure("state_file_missing")
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES
                or (os.name == "posix" and (info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o077))):
            raise StateFailure("state_file_unsafe")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except StateFailure:
        raise
    except (OSError, ValueError, UnicodeError):
        raise StateFailure("state_file_unreadable") from None


def write_json(path, value):
    """Atomic, flushed writes in the already private state directory."""
    path = Path(path)
    if path.is_symlink():
        raise StateFailure("state_file_unsafe")
    content = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(content) > MAX_FILE_BYTES:
        raise StateFailure("state_file_too_large")
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    descriptor = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except (OSError, ValueError, TypeError):
        raise StateFailure("state_write_failed") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


class StateLock:
    """The OS releases this lock after a crash; the lock inode is never deleted."""
    def __init__(self, directory):
        self.path = Path(directory) / ".job.lock"
        self.descriptor = None

    def __enter__(self):
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            if self.path.is_symlink():
                raise StateFailure("lock_file_unsafe")
            self.descriptor = os.open(self.path, flags, 0o600)
            info = os.fstat(self.descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise StateFailure("lock_file_unsafe")
            if os.name == "nt":
                import msvcrt
                if info.st_size == 0:
                    os.write(self.descriptor, b"0")
                os.lseek(self.descriptor, 0, os.SEEK_SET)
                msvcrt.locking(self.descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return self
        except OSError as error:
            if self.descriptor is not None:
                os.close(self.descriptor)
                self.descriptor = None
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise Busy("another_observer_is_running") from None
            raise StateFailure("lock_failed") from None
        except BaseException:
            if self.descriptor is not None:
                os.close(self.descriptor)
                self.descriptor = None
            raise

    def __exit__(self, *exc):
        if self.descriptor is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(self.descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(self.descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.descriptor, fcntl.LOCK_UN)
            finally:
                os.close(self.descriptor)
                self.descriptor = None


def load_state(directory):
    state = read_json(directory / "state.json", optional=True)
    ledger = read_json(directory / "notification-ledger.json", optional=True)
    if state is not None and ledger is None:
        raise StateFailure("notification_ledger_missing")
    if state is None:
        state = {"schema_version": SCHEMA_VERSION, "probe_state": {}, "job": {}}
    if ledger is None:
        ledger = {"schema_version": SCHEMA_VERSION, "target_fingerprint_sha256": None,
                  "candidates": {}, "deliveries": {}}
    if (state.get("schema_version") != SCHEMA_VERSION or not isinstance(state.get("probe_state"), dict)
            or not isinstance(state.get("job"), dict) or ledger.get("schema_version") != SCHEMA_VERSION
            or not isinstance(ledger.get("candidates"), dict) or not isinstance(ledger.get("deliveries"), dict)):
        raise StateFailure("state_schema_invalid")
    target = ledger.get("target_fingerprint_sha256")
    if target is not None and not re.fullmatch(r"[a-f0-9]{64}", str(target)):
        raise StateFailure("ledger_target_invalid")
    for key, record in ledger["candidates"].items():
        if (not isinstance(key, str) or not key or len(key) > 512 or not isinstance(record, dict)
                or record.get("kind") not in KNOWN_KINDS
                or record.get("status") not in {"pending", "accepted", "not_submitted", "unknown", "exhausted", "retired", "intent"}
                or type(record.get("attempt_count")) is not int or not 0 <= record["attempt_count"] <= MAX_SEND_ATTEMPTS):
            raise StateFailure("ledger_candidate_invalid")
    for key, delivery in ledger["deliveries"].items():
        if (not isinstance(delivery, dict) or delivery.get("request_id") != key
                or delivery.get("status") not in STATUS_VALUES or not isinstance(delivery.get("candidate_keys"), list)
                or not delivery["candidate_keys"] or any(item not in ledger["candidates"] for item in delivery["candidate_keys"])):
            raise StateFailure("ledger_delivery_invalid")
        try:
            if str(uuid.UUID(key)) != key:
                raise ValueError()
            probe.timestamp(delivery["started_at"])
        except (ValueError, KeyError, TypeError):
            raise StateFailure("ledger_delivery_invalid") from None
        if delivery["status"] == "accepted" and not re.fullmatch(r"om_[A-Za-z0-9]+", str(delivery.get("provider_message_id", ""))):
            raise StateFailure("ledger_receipt_invalid")
    for key, record in ledger["candidates"].items():
        if record["status"] in ("accepted", "unknown", "intent", "not_submitted"):
            delivery = ledger["deliveries"].get(record.get("delivery_id"))
            if not delivery or key not in delivery["candidate_keys"] or delivery["status"] != record["status"]:
                raise StateFailure("ledger_link_invalid")
    return state, ledger


def load_service_config():
    value = service_credential("feishu.json")
    if value is None:
        raise ConfigFailure("service_feishu_credential_required")
    try:
        config = FeishuConfig(**value)
        if not all(isinstance(item, str) and item for item in value.values()):
            raise ValueError()
        return config
    except (TypeError, ValueError):
        raise ConfigFailure("service_feishu_credential_invalid") from None


def validate_release(value):
    if (not isinstance(value, dict) or not re.fullmatch(r"lab-[A-Za-z0-9._-]{1,80}", str(value.get("release_id", "")))
            or not re.fullmatch(r"[a-f0-9]{64}", str(value.get("source_sha256", "")))
            or not isinstance(value.get("app_version"), str) or not value["app_version"]):
        raise StateFailure("fixed_release_identity_required")
    return value


def _systemd_command(arguments):
    try:
        result = subprocess.run(arguments, check=False, capture_output=True, text=True, timeout=3,
                                env={**os.environ, "LC_ALL": "C", "TZ": "UTC"})
        if result.returncode or len(result.stdout) > 4096:
            return None
        return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return None


def _properties(text):
    return dict(line.split("=", 1) for line in (text or "").splitlines() if "=" in line)


def _property_values(text, name):
    # Array properties may repeat the key. A scalar dict would silently drop
    # all but the final TimersCalendar entry on the deployed systemd 255.
    return [value for key, value in (line.split("=", 1) for line in (text or "").splitlines() if "=" in line)
            if key == name]


def _usec_time(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,20}", value) or int(value) <= 0:
        return None
    try:
        return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=int(value))
    except (ValueError, OverflowError, OSError):
        return None


def _bus_timestamps(command, unit, interface, names):
    # v255 busctl returns one typed line per requested property. Do not parse
    # systemctl's human-readable timestamps, which lose microsecond precision.
    path = "/org/freedesktop/systemd1/unit/" + unit.replace("-", "_2d").replace(".", "_2e")
    raw = command(["busctl", "--system", "get-property", "org.freedesktop.systemd1", path,
                   "org.freedesktop.systemd1." + interface, *names])
    raw = raw if isinstance(raw, str) and len(raw) <= 4096 else None
    lines = raw.splitlines() if raw else []
    values = {name: None for name in names}
    if len(lines) == len(names):
        for name, line in zip(names, lines):
            match = re.fullmatch(r"t ([0-9]{1,20})", line)
            if match and int(match[1]) < 2**64 - 1:
                values[name] = match[1]
    return values, raw


def _invocation_id(value):
    return value if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{32}", value) else None


def _trigger_environment_status(environment, trigger, monotonic):
    if not any(environment.values()):
        return "missing"
    if environment.get("TRIGGER_UNIT") != TIMER:
        return "different-unit"
    actual = _usec_time(environment.get("TRIGGER_TIMER_REALTIME_USEC"))
    raw_monotonic = environment.get("TRIGGER_TIMER_MONOTONIC_USEC")
    if actual is None or not isinstance(raw_monotonic, str) or not re.fullmatch(r"[0-9]{1,20}", raw_monotonic):
        return "invalid"
    if trigger is None or monotonic is None:
        return "not-correlated"
    if actual > trigger or int(raw_monotonic) > int(monotonic):
        return "future"
    if actual < trigger or int(raw_monotonic) < int(monotonic):
        return "stale"
    return "matches-manager"


def _inferred_slot(triggered):
    local = triggered.astimezone(probe.CHINA_TIME)
    for days_back in range(8):
        day = local.date() - timedelta(days=days_back)
        if day.weekday() >= 5:
            continue
        for hour in (15, 9):
            slot = datetime.combine(day, day_time(hour, 35), probe.CHINA_TIME)
            if slot <= local:
                return slot
    return None


def _verified_timer_schedule(raw):
    # systemctl's TimersCalendar property contains the effective calendar list,
    # including drop-ins. Reject additions, omissions and a different timezone.
    # v255 prints one name=value line per (sst) entry, not one joined array:
    # https://github.com/systemd/systemd/blob/v255/src/systemctl/systemctl-show.c#L1239-L1248
    # https://github.com/systemd/systemd/blob/v255/src/shared/bus-print-properties.c#L14-L25
    # Also accept an explicitly joined value, without discarding any entries.
    values = [raw] if isinstance(raw, str) else raw
    if (not isinstance(values, list) or not values or any(not isinstance(value, str) or not value for value in values)
            or sum(len(value) for value in values) > 4096):
        return None
    calendars = []
    pattern = r"\{\s*OnCalendar=([^;{}]+)\s*;\s*next_elapse=[^{}]*\}"
    for value in values:
        entries = re.findall(pattern, value)
        if not entries or re.sub(pattern, "", value).strip():
            return None
        calendars.extend(entries)
    expected = {"Mon..Fri *-*-* 09:35:00 Asia/Shanghai", "Mon..Fri *-*-* 15:35:00 Asia/Shanghai"}
    if len(calendars) != 2 or {value.strip() for value in calendars} != expected:
        return None
    return TIMER_SCHEDULE


def read_systemd_metadata(started, environment=None, command=_systemd_command):
    """Correlate the current invocation with an actually running timer.

    v255 unit_start() retains the older activation details object, so a new
    INVOCATION_ID does not make TRIGGER_* current. timer_enter_running() updates
    LastTrigger before starting the service; timer_trigger_notify() returns the
    timer to waiting when that service ends. Require that live state, target and
    invocation plus both clock orderings. A retained LastTrigger or environment
    alone is never proof. Sources (also documented in the acceptance evidence):
    https://github.com/systemd/systemd/blob/v255/src/core/unit.c#L1820-L1823
    https://github.com/systemd/systemd/blob/v255/src/core/timer.c#L538-L575
    https://github.com/systemd/systemd/blob/v255/src/core/timer.c#L708-L735
    """
    environment = os.environ if environment is None else environment
    service = _properties(command(["systemctl", "show", SERVICE, "--no-pager", "--property=InvocationID",
                                   "--property=ActiveState", "--property=SubState"]))
    timer_output = command(["systemctl", "show", TIMER, "--no-pager", "--property=ActiveState",
                            "--property=SubState", "--property=UnitFileState", "--property=Unit",
                            "--property=Persistent", "--property=LastTriggerUSec", "--property=TimersCalendar"])
    timer = _properties(timer_output)
    calendar_values = _property_values(timer_output, "TimersCalendar")
    active = command(["systemctl", "is-active", TIMER])
    enabled = command(["systemctl", "is-enabled", TIMER])
    timer_times, timer_raw = _bus_timestamps(command, TIMER, "Timer", ("LastTriggerUSec", "LastTriggerUSecMonotonic"))
    exec_times, exec_raw = _bus_timestamps(command, SERVICE, "Service",
                                          ("ExecMainStartTimestamp", "ExecMainStartTimestampMonotonic"))
    previous_times, previous_raw = _bus_timestamps(command, SERVICE, "Unit",
                                                  ("InactiveEnterTimestamp", "InactiveEnterTimestampMonotonic"))
    activation_times, activation_raw = _bus_timestamps(command, TIMER, "Unit", ("InactiveExitTimestamp",))
    after = _properties(command(["systemctl", "show", SERVICE, "--no-pager", "--property=InvocationID"]))
    invocation = _invocation_id(environment.get("INVOCATION_ID"))
    manager_invocation = _invocation_id(service.get("InvocationID"))
    after_invocation = _invocation_id(after.get("InvocationID"))
    raw_environment = {name: value if isinstance(value := environment.get(name), str) and len(value) <= 128 else None
                       for name in ("TRIGGER_UNIT", "TRIGGER_TIMER_REALTIME_USEC", "TRIGGER_TIMER_MONOTONIC_USEC")}
    last_trigger = _usec_time(timer_times["LastTriggerUSec"])
    exec_started = _usec_time(exec_times["ExecMainStartTimestamp"])
    timer_activated = _usec_time(activation_times["InactiveExitTimestamp"])
    trigger_mono = timer_times["LastTriggerUSecMonotonic"]
    exec_mono = exec_times["ExecMainStartTimestampMonotonic"]
    previous_mono = previous_times["InactiveEnterTimestampMonotonic"]
    previous_real = previous_times["InactiveEnterTimestamp"]
    consistent = bool(invocation and invocation == manager_invocation == after_invocation)
    # The lower bound excludes retained triggers from an earlier run; the upper
    # bound excludes a timer that elapsed after a manual run had already begun.
    clock_ordered = bool(last_trigger and exec_started and last_trigger <= exec_started <= started
                         and all(value is not None for value in (trigger_mono, exec_mono, previous_mono, previous_real))
                         and 0 < int(trigger_mono) <= int(exec_mono)
                         and int(previous_mono) < int(trigger_mono)
                         and int(previous_real) < int(timer_times["LastTriggerUSec"])
                         and abs((exec_started - last_trigger).total_seconds()
                                 - (int(exec_mono) - int(trigger_mono)) / 1_000_000) <= 1)
    correlated = (consistent and clock_ordered and timer.get("ActiveState") == "active"
                  and timer.get("SubState") == "running" and timer.get("Unit") == SERVICE
                  and service.get("ActiveState") in {"activating", "active"})
    mode = "manual-start" if not environment.get("INVOCATION_ID") and not any(raw_environment.values()) else "unknown"
    actual = last_trigger if correlated else None
    if actual is not None:
        mode = "timer"
    schedule = _verified_timer_schedule(calendar_values)
    planned = _inferred_slot(actual) if actual is not None and schedule else None
    timing = "unknown"
    if planned is not None:
        # Persistent can coalesce multiple missed occurrences. This is only the
        # most recent scheduled slot, not a reconstruction of every missed run.
        if timer.get("Persistent") == "yes" and timer_activated and planned < timer_activated <= actual:
            timing = "persistent-catch-up-inferred"
        elif (actual - planned).total_seconds() < 60:
            # A minute relation, not an assertion that startup was on time.
            timing = "within-scheduled-minute"
        else:
            timing = "delayed-or-catch-up-unresolved"
    return {"activation": mode, "activation_evidence": "systemd-manager-current-timer-invocation" if mode == "timer" else
            ("direct-process-without-systemd-invocation" if mode == "manual-start" else "trigger-cause-unavailable"),
            "invocation_id": invocation, "manager_invocation_id": manager_invocation,
            "trigger_unit": TIMER if actual else None, "triggered_at": iso(actual) if actual else None,
            "last_trigger_usec": timer_times["LastTriggerUSec"],
            "last_trigger_at": iso(last_trigger) if last_trigger else None,
            "last_trigger_text": timer.get("LastTriggerUSec") if len(timer.get("LastTriggerUSec", "")) < 120 else None,
            "planned_at": iso(planned) if planned else None,
            "planned_at_origin": "inferred-most-recent-weekday-scheduled-slot" if planned else None,
            "trigger_metadata_is_best_effort": True,
            "trigger_environment": raw_environment,
            "trigger_environment_status": _trigger_environment_status(raw_environment, last_trigger, trigger_mono),
            "timer_run_timing": timing, "missed_occurrences_reconstructed": False,
            "manager_evidence": {"service": service, "timer": timer, "invocation_id_after_read": after_invocation,
                                 "invocation_matches": consistent, "clock_ordered": clock_ordered,
                                 "exec_started_at": iso(exec_started) if exec_started else None,
                                 "timer_activated_at": iso(timer_activated) if timer_activated else None,
                                 "numeric_outputs": {"timer_trigger": timer_raw, "service_exec": exec_raw,
                                                     "service_previous_inactive": previous_raw, "timer_activation": activation_raw}},
            "timer_calendar_values": calendar_values,
            "timer": {"active": active == "active" and timer.get("ActiveState") == "active",
                      "enabled": enabled == "enabled" and timer.get("UnitFileState") == "enabled",
                      "schedule": schedule}}


class BudgetedProbeClient:
    def __init__(self, client, deadline, monotonic):
        self.client, self.deadline, self.monotonic = client, deadline, monotonic

    @property
    def requests(self):
        return getattr(self.client, "requests", [])

    def check(self, seconds=12):
        if self.monotonic() + seconds >= self.deadline:
            raise BudgetExceeded("probe_time_budget_exhausted")

    def get(self, endpoint, params=None):
        self.check()
        return self.client.get(endpoint, params)


def recover_intents(ledger, now):
    recovered = []
    for delivery in ledger["deliveries"].values():
        if delivery["status"] == "intent":
            delivery.update(status="unknown", completed_at=iso(now), error_code="interrupted_during_send")
            for key in delivery["candidate_keys"]:
                ledger["candidates"][key].update(status="unknown", updated_at=iso(now))
            recovered.append(delivery["request_id"])
    return recovered


def reconcile_accepted(state, ledger, now):
    current = state["probe_state"]
    for key, candidate in ledger["candidates"].items():
        if candidate["status"] == "accepted":
            delivery = ledger["deliveries"][candidate["delivery_id"]]
            if delivery["status"] != "accepted" or key not in delivery["candidate_keys"]:
                raise StateFailure("accepted_receipt_link_invalid")
            current.setdefault("candidates", {}).setdefault(key, {"key": key, "kind": candidate["kind"], "active": False})
            if key not in current.get("notified", {}):
                current = probe.acknowledge(current, [key], probe.timestamp(delivery["completed_at"]))
    state["probe_state"] = current


def fault_candidate(code):
    return {"key": "server-job:" + code, "kind": "server_job_error", "active": True, "code": code}


def installation_candidate(directory, release, metadata, now):
    value = read_json(directory / "installation.json", optional=True)
    if value is None:
        return None
    try:
        required = {"schema_version", "release_id", "installed_at", "timer", "receipt_key"}
        if (set(value) not in (required, required | {"runner_release_id"})
                or value["schema_version"] != SCHEMA_VERSION
                or not isinstance(value["release_id"], str) or not re.fullmatch(r"lab-[A-Za-z0-9_-]+", value["release_id"])
                or value.get("runner_release_id", value["release_id"]) != release["release_id"]
                or value["receipt_key"] != INSTALLATION_KEY
                or value["timer"] != {"active": True, "enabled": True, "schedule": TIMER_SCHEDULE}
                or probe.timestamp(value["installed_at"]) > now):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise StateFailure("installation_record_invalid") from None
    if metadata.get("timer") != {"active": True, "enabled": True, "schedule": TIMER_SCHEDULE}:
        raise StateFailure("installation_timer_not_verified")
    return {"key": INSTALLATION_KEY, "kind": "installation_completed", "active": True,
            "installation_release_id": value["release_id"], "installed_at": value["installed_at"], "schedule": TIMER_SCHEDULE}


def target_gate(client, config, ledger, feishu, now):
    local_fingerprint = probe.digest(config.fingerprint)
    bound = ledger.get("target_fingerprint_sha256")
    def revoked(code):
        ledger["target_authorization"] = {"status": "revoked", "code": code, "observed_at": iso(now)}
        return {"allowed": False, "fault_only": False, "code": code}
    if bound is not None and bound != local_fingerprint:
        return revoked("credential_target_changed")
    try:
        public = client.get("api/notifications")
    except Exception:
        # A previously verified original target can receive an application-down
        # diagnostic. This never bypasses a returned disabled/changed setting.
        if not bound or bound != local_fingerprint:
            return {"allowed": False, "fault_only": False, "code": "public_target_unavailable_unbound"}
        if ledger.get("target_authorization", {}).get("status") != "verified":
            return {"allowed": False, "fault_only": False, "code": "public_target_unavailable_authorization_revoked"}
        public, fault_only = None, True
    else:
        fault_only = False
        if (not isinstance(public, dict) or public.get("configured") is not True or public.get("enabled") is not True
                or not public.get("verified_at") or public.get("config_error") or public.get("check_error")
                or public.get("target_fingerprint_sha256") != local_fingerprint):
            return revoked("public_target_disabled_changed_or_unverified")
    try:
        target = feishu.verify_target()
        if target.owner_id != config.owner_id:
            raise FeishuFailure("Target owner differs")
    except Exception:
        return revoked("private_group_verification_failed")
    if bound is None:
        ledger["target_fingerprint_sha256"] = local_fingerprint
        ledger["target_bound_at"] = iso(now)
    ledger["target_last_verified_at"] = iso(now)
    if not fault_only:
        ledger["target_authorization"] = {"status": "verified", "code": "original_target_verified", "observed_at": iso(now)}
    return {"allowed": True, "fault_only": fault_only,
            "code": "original_target_verified_application_unavailable" if fault_only else "original_target_verified"}


def _safe_number(value):
    return value if isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,20}(?:\.[0-9]{1,28})?", value) else "待核对"


def render_message(candidates, report, now):
    lines = ["AlphaProject Lab · 服务器自动验收", now.astimezone(probe.CHINA_TIME).strftime("北京时间 %Y-%m-%d %H:%M:%S")]
    for candidate in candidates:
        kind = candidate["kind"]
        if kind == "installation_completed":
            lines.append("服务器自动验收迁移完成：工作日 09:35、15:35 自动检查，电脑和 Codex 关闭后仍可运行。")
            collection = (report or {}).get("continuous_collection", {}).get("cumulative_pass") is True
            natural = (report or {}).get("natural_alert", {}).get("cumulative_pass") is True
            lines.append("实际验收进度：连续采集" + ("已验证" if collection else "待交易时段验证") + "；茅台涨跌 3% 自然提醒" + ("已验证" if natural else "待自然事件发生") + "。")
        elif kind == "collection_passed":
            lines.append("连续采集验收通过：贵州茅台（600519）已有同一交易时段、同一服务进程的 3 次不同有效后台采样，来源时间、接收时间和累计成交量均前进。")
        elif kind == "natural_event_accepted":
            event = candidate.get("event") or {}
            direction = "上涨" if event.get("direction") == "up" else "下跌"
            lines.append("自然 3% 提醒验收通过：贵州茅台（600519）" + direction + " " + _safe_number(event.get("change_pct")) + "%，触发计算、事件快照、同日同方向去重及原提醒的飞书平台接收记录均已核对。")
            identity = event.get("id")
            try:
                if str(uuid.UUID(identity)) == identity:
                    lines.append("本次事件详情：https://8.155.130.86/lab/#event=" + identity)
            except (ValueError, TypeError, AttributeError):
                pass
        elif kind == "rule_changed":
            lines.append("验收规则发生变化：贵州茅台 3% 规则或自选记录已改变。本次验收将暂停，请核对当前规则；程序没有恢复旧配置。")
        elif kind == "collection_needs_attention":
            lines.append("后台取数需要检查：交易时段连续 3 个不同后台采集轮次均记录了这只股票的获取失败。验收保留为待通过。")
        elif kind == "event_delivery_needs_attention":
            lines.append("自然事件已有记录，但对应原提醒的发送结果需要核对，尚不能把提醒送达判为通过。")
        elif kind == "event_evidence_needs_attention":
            lines.append("历史自然事件的依据出现不一致，自动验收保留为待核对，请检查事件详情。")
        elif kind == "notification_configuration_changed":
            lines.append("通知配置发生变化，自动验收没有改回旧配置，请核对原有私人群与通知开关。")
        elif kind in ("probe_error", "server_job_error"):
            lines.append("服务器验收任务发现运行故障或上次执行中断，已有状态与通知账本保留；本次没有把未完成检查判为通过。")
    lines.append("平台接收记录与本人实际查看分别记录。学习版入口：https://8.155.130.86/lab/")
    return "\n".join(lines)


def _candidate_priority(candidate):
    return (0 if candidate["kind"] == "installation_completed" else 1 if candidate["kind"] == "rule_changed"
            else 2 if candidate["kind"] in FAULT_KINDS else 3, candidate["key"])


def _public_deliveries(ledger):
    names = ("candidate_keys", "status", "provider_message_id", "request_id", "run_id", "started_at", "completed_at",
             "error_code", "candidate_attempt_counts", "installation_release_id")
    return [{key: deepcopy(item.get(key)) for key in names} for item in ledger["deliveries"].values()]


def _accepted(ledger, key, *, installation_release_id=None):
    candidate = ledger["candidates"].get(key, {})
    if candidate.get("status") != "accepted":
        return False
    delivery = ledger["deliveries"].get(candidate.get("delivery_id"), {})
    return bool(delivery.get("status") == "accepted" and key in delivery.get("candidate_keys", [])
                and re.fullmatch(r"om_[A-Za-z0-9]+", str(delivery.get("provider_message_id", "")))
                and (installation_release_id is None or delivery.get("installation_release_id") == installation_release_id))


def terminal_candidate(report, candidates, ledger, installation, gate):
    if not report:
        return None
    current = {item["key"]: item for item in candidates}
    current.update({key: item for key, item in report.get("state", {}).get("candidates", {}).items()
                    if isinstance(item, dict) and item.get("active") is True})
    candidates = list(current.values())
    rule = report.get("rule_configuration", {})
    changed = [item["key"] for item in candidates if item["kind"] == "rule_changed"]
    if rule.get("status") == "changed" and changed and all(_accepted(ledger, key) for key in changed):
        return "suspended"
    collection = report.get("continuous_collection", {})
    natural = report.get("natural_alert", {})
    collection_key = "collection:" + str(collection.get("scope"))
    natural_keys = ["event:" + str(identity) + ":accepted" for identity in natural.get("accepted_event_ids", [])]
    if (rule.get("status") == "matched" and not rule.get("changed_during_run")
            and collection.get("cumulative_pass") is True and natural.get("status") == "passed"
            and natural.get("history_complete") is True and _accepted(ledger, collection_key)
            and any(_accepted(ledger, key) for key in natural_keys)
            and gate.get("allowed") and not gate.get("fault_only")
            and not any(item["kind"] in FAULT_KINDS | {"notification_configuration_changed", "rule_changed"} for item in candidates)):
        return "completed"
    return None


def _finish(directory, state, ledger, result, clock, marker=None):
    completed = clock()
    result["completed_at"] = iso(completed)
    result["deliveries"] = _public_deliveries(ledger)
    result["terminal"] = marker
    state["job"].update(status="completed", phase="finished", completed_at=result["completed_at"], result_status=result["status"])
    write_json(directory / "notification-ledger.json", ledger)
    write_json(directory / "state.json", state)
    report_path = directory / "runs" / (result["run_id"] + ".json")
    result["evidence_file"] = "runs/" + report_path.name
    write_json(report_path, result)
    write_json(directory / "last-run.json", result)
    if marker:
        write_json(directory / (marker + ".json"), {"schema_version": SCHEMA_VERSION, "terminal": marker,
                   "release_id": result["release_id"], "completed_at": result["completed_at"],
                   "run_id": result["run_id"], "evidence_file": result["evidence_file"],
                   "target": {"market": probe.TARGET[0], "symbol": probe.TARGET[1], "generation": probe.DEFAULT_GENERATION,
                              "rule_version": probe.DEFAULT_RULE_VERSION, "since": probe.DEFAULT_SINCE},
                   "accepted_request_ids": [item["request_id"] for item in result["deliveries"] if item["status"] == "accepted"]})
    return result


def run_once(state_dir, *, probe_client=None, feishu_client=None, config=None, release=None,
             probe_runner=probe.run_probe, clock=utcnow, sleeper=time.sleep, monotonic=time.monotonic,
             environment=None, metadata_reader=read_systemd_metadata, fault_hook=None):
    """One bounded invocation; injected I/O is intended for isolated tests only."""
    started, start_tick = clock(), monotonic()
    directory = _private_directory(Path(state_dir))
    _private_directory(directory / "runs")
    hook = fault_hook or (lambda stage, record: None)
    with StateLock(directory):
        result = {"schema_version": SCHEMA_VERSION, "run_id": uuid.uuid4().hex,
                  "release_id": None, "status": "ok", "started_at": iso(started), "completed_at": None,
                  "probe": None, "deliveries": [], "terminal": None, "terminal_pending": None, "errors": []}
        try:
            state, ledger = load_state(directory)
        except StateFailure as error:
            result.update(status="failed", completed_at=iso(clock()), errors=[str(error)])
            write_json(directory / "last-run.json", result)
            return result
        recovery = recover_intents(ledger, started)
        result["recovered_unknown_request_ids"] = recovery
        reconcile_accepted(state, ledger, started)
        for marker in ("completed", "suspended"):
            prior_terminal = read_json(directory / (marker + ".json"), optional=True)
            if prior_terminal is not None:
                if prior_terminal.get("schema_version") != SCHEMA_VERSION or prior_terminal.get("terminal") != marker:
                    raise StateFailure("terminal_marker_invalid")
                result.update(terminal=marker, status="ok", release_id=prior_terminal.get("release_id"), completed_at=iso(clock()),
                              deliveries=_public_deliveries(ledger), already_terminal=True)
                return result
        old_job = deepcopy(state["job"])
        interrupted = bool(old_job.get("status") == "running" and old_job.get("phase") in ("preflight", "probing"))
        state["job"] = {"run_id": result["run_id"], "started_at": result["started_at"], "status": "running", "phase": "preflight"}
        # Ledger exists before state, so a killed first run cannot leave an empty
        # replacement ledger to be silently recreated on the next invocation.
        write_json(directory / "notification-ledger.json", ledger)
        write_json(directory / "state.json", state)
        try:
            executing_release = validate_release(release if release is not None else release_info(ROOT))
            result["release_id"] = executing_release["release_id"]
            config = config if config is not None else load_service_config()
            feishu = feishu_client if feishu_client is not None else FeishuClient(config)
            client = probe_client if probe_client is not None else probe.ReadOnlyClient()
            metadata = metadata_reader(started, environment)
            if not isinstance(metadata, dict):
                raise StateFailure("schedule_metadata_invalid")
            result["schedule"] = metadata
            state["job"]["invocation_id"] = metadata.get("invocation_id")
        except (StateFailure, ConfigFailure, ValueError, RuntimeError, OSError):
            result.update(status="failed", errors=["preflight_configuration_or_identity_failed"])
            return _finish(directory, state, ledger, result, clock)

        candidates = []
        guarded = BudgetedProbeClient(client, start_tick + PROBE_BUDGET_SECONDS, monotonic)
        def short_sleep(seconds):
            guarded.check(seconds + 12)
            sleeper(seconds)
        state["job"]["phase"] = "probing"
        write_json(directory / "state.json", state)
        try:
            report = probe_runner(guarded, samples=3, max_observations=6, interval=65, since=probe.DEFAULT_SINCE,
                                  planned_at=metadata.get("planned_at"), triggered_at=metadata.get("triggered_at"),
                                  previous_state=state["probe_state"], expected_generation=probe.DEFAULT_GENERATION,
                                  expected_rule_version=probe.DEFAULT_RULE_VERSION, clock=clock, sleep=short_sleep,
                                  progress=lambda *args, **kwargs: None)
            if not isinstance(report, dict) or not isinstance(report.get("state"), dict) or not isinstance(report.get("notification_candidates"), list):
                raise ValueError()
            result["probe"] = report
            state["probe_state"] = deepcopy(report["state"])
            candidates.extend(deepcopy(report["notification_candidates"]))
        except Exception as error:
            code = "probe_time_budget_exhausted" if isinstance(error, BudgetExceeded) else "probe_execution_failed"
            result["errors"].append(code)
            candidates.append(fault_candidate(code))
            result["status"] = "attention_required"
        if interrupted:
            candidates.append(fault_candidate("previous_run_interrupted"))
            result["interrupted_previous_run"] = {key: old_job.get(key) for key in ("run_id", "started_at", "invocation_id", "phase")}
        state["job"]["phase"] = "evaluated"
        write_json(directory / "state.json", state)

        installation = None
        try:
            installation = installation_candidate(directory, executing_release, metadata, clock())
            if installation:
                candidates.append(installation)
        except StateFailure as error:
            result["errors"].append(str(error))
            candidates.append(fault_candidate(str(error)))
            result["status"] = "attention_required"

        # Runtime-only candidates from a previous run are not automatically
        # resurrected. A retired error cannot be retried after the error resolves.
        active = {}
        probe_active = [value for value in state["probe_state"].get("candidates", {}).values()
                        if isinstance(value, dict) and value.get("active") is True] if result["probe"] else []
        for candidate in probe_active + candidates:
            if (not isinstance(candidate, dict) or candidate.get("kind") not in KNOWN_KINDS
                    or not isinstance(candidate.get("key"), str) or not candidate["key"] or len(candidate["key"]) > 512):
                result["errors"].append("candidate_schema_invalid")
                result["status"] = "attention_required"
                continue
            if candidate.get("active") is False:
                continue
            active[candidate["key"]] = candidate
        for key, value in state["probe_state"].setdefault("candidates", {}).items():
            if value.get("kind") in ("server_job_error", "installation_completed"):
                value["active"] = key in active
        for key, candidate in active.items():
            state["probe_state"]["candidates"][key] = {**deepcopy(candidate), "active": True}
            prior = ledger["candidates"].get(key)
            if prior is None:
                ledger["candidates"][key] = {"kind": candidate["kind"], "status": "pending", "attempt_count": 0, "active": True, "created_at": iso(clock())}
            elif prior["kind"] != candidate["kind"]:
                raise StateFailure("candidate_identity_changed")
            elif prior["status"] == "retired":
                prior["status"] = "pending" if prior["attempt_count"] < MAX_SEND_ATTEMPTS else "exhausted"
        for key, value in ledger["candidates"].items():
            value["active"] = key in active
            if not value["active"] and value["status"] in ("pending", "not_submitted", "exhausted"):
                value.update(status="retired", updated_at=iso(clock()))

        gate = target_gate(client, config, ledger, feishu, clock())
        result["notification_gate"] = gate
        if not gate["allowed"] or gate["fault_only"]:
            result["status"] = "attention_required"
            result["errors"].append(gate["code"])
        eligible = []
        for key, candidate in sorted(active.items(), key=lambda pair: _candidate_priority(pair[1])):
            record = ledger["candidates"][key]
            if candidate["kind"] == "installation_completed" and record.get("delivery_id"):
                previous_delivery = ledger["deliveries"][record["delivery_id"]]
                if previous_delivery.get("installation_release_id") != candidate["installation_release_id"]:
                    result["status"] = "attention_required"
                    result["errors"].append("installation_scope_conflict")
                    continue
            if record["status"] == "accepted":
                continue
            if record["status"] in ("unknown", "exhausted"):
                result["status"] = "attention_required"
                continue
            if record["attempt_count"] >= MAX_SEND_ATTEMPTS:
                record["status"] = "exhausted"
                result["status"] = "attention_required"
                continue
            if gate["allowed"] and (not gate["fault_only"] or candidate["kind"] in FAULT_KINDS):
                eligible.append(candidate)
        write_json(directory / "notification-ledger.json", ledger)
        write_json(directory / "state.json", state)

        if eligible:
            selected = eligible[:MAX_BATCH_KEYS]
            request_id, send_started = str(uuid.uuid4()), clock()
            message = render_message(selected, result["probe"], send_started)
            delivery = {"request_id": request_id, "run_id": result["run_id"], "candidate_keys": [item["key"] for item in selected],
                        "status": "intent", "provider_message_id": None, "started_at": iso(send_started), "completed_at": None,
                        "message_text": message, "message_sha256": probe.digest(message), "candidate_attempt_counts": {}}
            installation_items = [item for item in selected if item["kind"] == "installation_completed"]
            if installation_items:
                delivery["installation_release_id"] = installation_items[0]["installation_release_id"]
            for candidate in selected:
                record = ledger["candidates"][candidate["key"]]
                record.update(status="intent", delivery_id=request_id, attempt_count=record["attempt_count"] + 1, updated_at=iso(send_started))
                delivery["candidate_attempt_counts"][candidate["key"]] = record["attempt_count"]
            state["job"]["phase"] = "sending"
            write_json(directory / "state.json", state)
            ledger["deliveries"][request_id] = delivery
            write_json(directory / "notification-ledger.json", ledger)
            hook("after_intent", deepcopy(delivery))
            try:
                message_id = feishu.send_test(request_id, message)
                hook("after_provider_acceptance", deepcopy(delivery))
                if not isinstance(message_id, str) or not re.fullmatch(r"om_[A-Za-z0-9]+", message_id):
                    raise FeishuFailure("Provider receipt is incomplete", unknown=True)
                delivery.update(status="accepted", provider_message_id=message_id, completed_at=iso(clock()))
            except FeishuFailure as error:
                delivery.update(status="unknown" if error.unknown else "not_submitted", completed_at=iso(clock()),
                                error_code="provider_result_unknown" if error.unknown else "provider_explicitly_not_submitted")
                result["status"] = "attention_required"
            except Exception:
                delivery.update(status="unknown", completed_at=iso(clock()), error_code="send_exception_result_unknown")
                result["status"] = "attention_required"
            for key in delivery["candidate_keys"]:
                ledger["candidates"][key].update(status=delivery["status"], updated_at=delivery["completed_at"])
            write_json(directory / "notification-ledger.json", ledger)
            hook("after_receipt", deepcopy(delivery))
            reconcile_accepted(state, ledger, clock())
            write_json(directory / "state.json", state)
            hook("after_state", deepcopy(state))
        else:
            reconcile_accepted(state, ledger, clock())

        pending_terminal = terminal_candidate(result["probe"], list(active.values()), ledger, installation, gate)
        result["terminal_pending"] = pending_terminal
        installation_accepted = bool(installation and _accepted(ledger, INSTALLATION_KEY, installation_release_id=installation["installation_release_id"]))
        marker = pending_terminal if installation_accepted else None
        if recovery:
            result["status"] = "attention_required"
        result["active_candidate_keys"] = sorted(active)
        result["deferred_candidate_keys"] = [item["key"] for item in eligible[MAX_BATCH_KEYS:]]
        if result["deferred_candidate_keys"]:
            result["status"] = "attention_required"
        return _finish(directory, state, ledger, result, clock, marker)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        result = run_once(arguments.state_dir)
        print(json.dumps({key: result.get(key) for key in ("status", "release_id", "started_at", "completed_at", "terminal")}, ensure_ascii=False), flush=True)
        return 0 if result["status"] in ("ok", "attention_required") else 1
    except Busy:
        print('{"status":"locked","reason":"another_observer_is_running"}', flush=True)
        return 0
    except (StateFailure, ConfigFailure, ValueError, TypeError, OSError):
        print('{"status":"failed","reason":"observer_state_or_configuration_failed"}', file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
