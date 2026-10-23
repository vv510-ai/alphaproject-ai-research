"""Observe the authorized live rule using bounded GET requests only.

This program never refreshes quotes, runs a demo, changes rules, calls a model,
or sends/acknowledges a Feishu message. The notifications GET may perform the
server's existing timeout housekeeping. Local notification acknowledgements
only mean that the caller has already told the user about a reported result.
"""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.live_policy import proof_eligibility, session_context
from backend.app.quotes import CHINA_TIME

DEFAULT_BASE_URL = "https://8.155.130.86/lab/"
DEFAULT_SINCE = "2026-10-21T16:45:42Z"
DEFAULT_GENERATION = "2026-10-17T15:12:49.230568+00:00"
DEFAULT_RULE_VERSION = 2
TARGET = ("sh", "600519")
MAX_BYTES = 4 * 1024 * 1024
MAX_HISTORY_PAGES = 20
REQUIRED_VALID_SAMPLES = 3
ENDPOINTS = {"release.json", "api/health", "api/dashboard", "api/anomalies",
             "api/notifications", "api/anomalies/events"}


def timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("A bounded timezone-aware ISO timestamp is required")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamp must include its timezone")
    return parsed


def decimal(value):
    if not isinstance(value, str) or not re.fullmatch(r"-?[0-9]{1,20}(?:\.[0-9]{1,28})?", value):
        raise ValueError("Invalid decimal")
    parsed = Decimal(value)
    if not parsed.is_finite():
        raise ValueError("Non-finite decimal")
    return parsed


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def times(value):
    if value is None:
        return None
    parsed = timestamp(value) if isinstance(value, str) else value
    return {"utc": parsed.astimezone(timezone.utc).isoformat(),
            "china": parsed.astimezone(CHINA_TIME).isoformat()}


def fields(value, names):
    """Only named public scalar fields are retained in the evidence file."""
    if not isinstance(value, dict):
        return {}
    return {name: value[name] for name in names.split() if name in value
            and (value[name] is None or isinstance(value[name], (str, int, float, bool)))}


QUOTE_FIELDS = "market symbol name price previous_close change change_pct source_reported_at received_at source source_url price_unit change_pct_unit"
RECORD_FIELDS = "market symbol source source_url price previous_close source_reported_at received_at watchlist_created_at session_id calendar_version cumulative_volume quote_hash raw_sha256"
PROOF_FIELDS = "policy_version market symbol watchlist_created_at session_id session_end_at calendar_version freshness_seconds checked_at"
COLLECTOR_FIELDS = "running heartbeat_at last_cycle_at next_cycle_at cycle_id state interval_seconds freshness_seconds total attempted succeeded failed last_error"
RULE_FIELDS = "market symbol watchlist_created_at up_threshold down_threshold enabled version updated_at last_checked_at last_result"
NOTIFICATION_FIELDS = "id created_at updated_at status error_message confirmed_at kind event_id expires_at"


def public_proof(value):
    if not isinstance(value, dict):
        return None
    result = fields(value, PROOF_FIELDS)
    for key in ("previous", "current"):
        sample = value.get(key)
        result[key] = fields(sample, RECORD_FIELDS)
        if isinstance(sample, dict) and isinstance(sample.get("acquisition"), dict):
            result[key]["acquisition"] = fields(sample["acquisition"], "origin cycle_id started_at committed_at")
    return result


def public_eligibility(value):
    result = fields(value, "eligible code reason checked_at session_id")
    result["proof"] = public_proof(value.get("proof")) if isinstance(value, dict) else None
    return result


def public_quote(value):
    value = value if isinstance(value, dict) else {}
    result = fields(value, "data_state refresh_state last_attempt_at error_message market_state realtime_verified")
    result["data"] = fields(value.get("data"), QUOTE_FIELDS)
    result["eligibility"] = public_eligibility(value.get("eligibility"))
    return result


def target_items(value):
    result = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict) and (item.get("market"), item.get("symbol")) == TARGET:
            row = fields(item, "market symbol created_at name")
            row["quote"] = public_quote(item.get("quote"))
            row["rule"] = fields(item.get("rule"), RULE_FIELDS)
            result.append(row)
    return result


def public_event(value):
    value = value if isinstance(value, dict) else {}
    result = fields(value, "id mode market symbol direction trade_date created_at run_id detail_path")
    snapshot = value.get("snapshot") or {}
    result["snapshot"] = fields(snapshot, "change_pct direction threshold rule_version up_threshold down_threshold watchlist_created_at first_observation detection_label mode market_state freshness_limit_seconds formula repeat_policy session_id session_end_at calendar_version")
    result["snapshot"]["quote"] = fields(snapshot.get("quote"), QUOTE_FIELDS)
    result["snapshot"]["eligibility"] = public_eligibility(snapshot.get("eligibility"))
    result["notification"] = fields(value.get("notification"), NOTIFICATION_FIELDS)
    return result


def public_response(endpoint, value):
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    if endpoint == "release.json":
        return fields(value, "release_id app_version built_at source_sha256")
    if endpoint == "api/health":
        return fields(value, "status service version release_id instance_id")
    if endpoint in ("api/dashboard", "api/anomalies"):
        result = fields(value, "as_of total live_ready live_pause_reason check_interval_seconds")
        result["items"] = target_items(value.get("items"))
        result["collector"] = fields(value.get("collector"), COLLECTOR_FIELDS)
        if endpoint == "api/anomalies":
            result["runtime"] = fields(value.get("runtime"), "heartbeat_at checks last_error running")
        return result
    if endpoint == "api/notifications":
        result = fields(value, "configured enabled verified_at config_error check_error")
        fingerprint = value.get("target_fingerprint")
        result["target_fingerprint_sha256"] = digest(fingerprint) if isinstance(fingerprint, str) and fingerprint else None
        return result
    if endpoint == "api/anomalies/events":
        return {"as_of": value.get("as_of"),
                "event_history": fields(value.get("event_history"), "since through next_cursor complete"),
                "events": [public_event(item) for item in value.get("events", [])
                           if isinstance(item, dict) and (item.get("market"), item.get("symbol")) == TARGET]}
    raise ValueError("Endpoint is not allowed")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


class ReadOnlyClient:
    def __init__(self, base_url=DEFAULT_BASE_URL, timeout=10):
        parsed = urlsplit(base_url)
        production = parsed.scheme == "https" and parsed.hostname == "8.155.130.86" and parsed.port in (None, 443) and parsed.path.rstrip("/") == "/lab"
        local = parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")
        if not (production or local) or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Only the authorized Lab HTTPS URL or a loopback test server is allowed")
        if not 0 < timeout <= 30:
            raise ValueError("Request timeout must be between 0 and 30 seconds")
        self.base_url, self.timeout = base_url.rstrip("/") + "/", timeout
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.requests = []

    def get(self, endpoint, params=None):
        if endpoint not in ENDPOINTS or (params and endpoint != "api/anomalies/events"):
            raise ValueError("GET endpoint is not allowed")
        if params and set(params) - {"since", "cursor", "limit"}:
            raise ValueError("Query parameter is not allowed")
        url = self.base_url + endpoint + ("?" + urlencode(params) if params else "")
        entry = {"method": "GET", "endpoint": endpoint, "started_at": times(datetime.now(timezone.utc))}
        self.requests.append(entry)
        try:
            request = Request(url, method="GET", headers={"Accept": "application/json", "Cache-Control": "no-cache"})
            with self.opener.open(request, timeout=self.timeout) as response:
                entry["http_status"] = response.status
                if response.status != 200:
                    raise ValueError("HTTP response was not 200")
                body = response.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    raise ValueError("JSON response exceeds the bounded size")
                return public_response(endpoint, json.loads(body))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError) as error:
            entry["error_type"] = type(error).__name__
            if isinstance(error, HTTPError):
                entry["http_status"] = error.code
            # Do not copy exception bodies, redirect URLs, headers or credentials.
            raise ValueError("GET failed for " + endpoint + " (" + type(error).__name__ + ")") from None
        finally:
            entry["completed_at"] = times(datetime.now(timezone.utc))


def fetch_history(client, since):
    events, seen, cursors = [], set(), set()
    ceiling = None
    result = {"events": events, "event_history": {"since": since, "through": None, "complete": False, "next_cursor": None}, "errors": []}
    cursor = None
    for _ in range(MAX_HISTORY_PAGES):
        params = {"since": since, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        try:
            page = client.get("api/anomalies/events", params)
            meta = page["event_history"]
            if timestamp(meta["since"]) != timestamp(since):
                raise ValueError("History range changed")
            if ceiling is None:
                ceiling = meta["through"]
            if meta["through"] != ceiling or timestamp(ceiling) > timestamp(page["as_of"]):
                raise ValueError("History ceiling is inconsistent")
            for event in page["events"]:
                if not event.get("id") or event["id"] in seen:
                    raise ValueError("History contains a repeated event id")
                seen.add(event["id"])
                events.append(event)
            cursor = meta.get("next_cursor")
            result.update(as_of=page["as_of"], event_history=dict(meta))
            if meta.get("complete") is True and cursor is None:
                return result
            result["event_history"]["complete"] = False
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise ValueError("History cursor is missing or repeated")
            cursors.add(cursor)
        except (KeyError, TypeError, ValueError) as error:
            result["errors"].append(str(error))
            result["event_history"]["complete"] = False
            return result
    result["errors"].append("History page limit reached; completeness is unverified")
    result["event_history"]["complete"] = False
    return result


def observe(client, since, clock):
    observation = {"observed_at": times(clock()), "errors": []}
    for key, endpoint in (("release", "release.json"), ("health", "api/health"),
                          ("dashboard", "api/dashboard"), ("anomalies", "api/anomalies"),
                          ("notifications", "api/notifications")):
        try:
            observation[key] = client.get(endpoint)
        except ValueError as error:
            observation["errors"].append(str(error))
    observation["history"] = fetch_history(client, since)
    # Bind the batch to a release and process, including a restart during GETs.
    for key, endpoint in (("health_after", "api/health"), ("release_after", "release.json")):
        try:
            observation[key] = client.get(endpoint)
        except ValueError as error:
            observation["errors"].append(str(error))
    observation["completed_at"] = times(clock())
    return observation


def release_binding(observation):
    release, health = observation.get("release", {}), observation.get("health", {})
    if (health.get("status") != "ok" or health.get("service") != "AlphaProject-Lab"
            or not isinstance(health.get("instance_id"), str) or not health["instance_id"]
            or not release.get("release_id") or release.get("release_id") != health.get("release_id")
            or not release.get("app_version") or release.get("app_version") != health.get("version")
            or not re.fullmatch(r"[a-f0-9]{64}", release.get("source_sha256", ""))):
        raise ValueError("Release identity or process identity is missing/inconsistent")
    if observation.get("release_after", release) != release or observation.get("health_after", health) != health:
        raise ValueError("The release or process changed while the observation was read")
    return {"release_id": release["release_id"], "source_sha256": release["source_sha256"],
            "app_version": release["app_version"], "instance_id": health["instance_id"]}


def target_item(container):
    items = [item for item in container.get("items", [])
             if (item.get("market"), item.get("symbol")) == TARGET]
    if len(items) != 1:
        raise ValueError("The target watchlist item is missing or ambiguous")
    return items[0]


def collector_provenance(proof, observed_at, collector=None):
    cycles = []
    for key in ("previous", "current"):
        sample = proof[key]
        acquisition = sample["acquisition"]
        cycle = acquisition.get("cycle_id")
        if acquisition.get("origin") != "collector" or not isinstance(cycle, str) or not cycle:
            raise ValueError("Both samples must carry original background-collector provenance")
        started, received, committed = (timestamp(acquisition["started_at"]), timestamp(sample["received_at"]), timestamp(acquisition["committed_at"]))
        if not started <= received <= committed <= observed_at:
            raise ValueError("Sample acquisition/commit times are not coherent with server observation time")
        cycles.append(cycle)
    if cycles[0] == cycles[1]:
        raise ValueError("A proof pair must come from two different collector cycles")
    if collector is not None:
        if collector.get("running") is not True or collector.get("state") not in ("waiting", "collecting"):
            raise ValueError("The background collector is not currently running")
        if collector.get("cycle_id") != cycles[1]:
            raise ValueError("The persisted current sample does not belong to the reported collector cycle")
        heartbeat, last_cycle = timestamp(collector["heartbeat_at"]), timestamp(collector["last_cycle_at"])
        if not 0 <= (observed_at - heartbeat).total_seconds() < 10:
            raise ValueError("Collector heartbeat is missing, old or later than the server observation")
        if not last_cycle <= timestamp(proof["current"]["acquisition"]["started_at"]) <= observed_at:
            raise ValueError("Collector cycle and acquisition times do not agree")
    return cycles


def current_record_matches_quote(quote, proof):
    for name in ("market", "symbol", "price", "previous_close", "source", "source_url", "source_reported_at", "received_at"):
        if quote.get(name) != proof["current"].get(name):
            raise ValueError("Current sample metadata does not match its persisted quote: " + name)


def collection_point(observation, generation):
    result = {"valid": False, "reasons": []}
    try:
        if observation.get("errors"):
            raise ValueError("One or more required GET observations failed")
        binding = release_binding(observation)
        dashboard = observation["dashboard"]
        observed_at = timestamp(dashboard["as_of"])
        result["market_state"] = session_context(observed_at)["state"]
        result["server_as_of"] = dashboard["as_of"]
        item = target_item(dashboard)
        quote = item["quote"]
        if item["created_at"] != generation:
            raise ValueError("The target watchlist generation changed")
        if quote.get("refresh_state") != "succeeded" or quote.get("eligibility", {}).get("eligible") is not True:
            raise ValueError("The current persisted quote is not eligible for live detection")
        proof = quote["eligibility"]["proof"]
        current_record_matches_quote(quote["data"], proof)
        checked = proof_eligibility(quote["data"], proof, observed_at, generation)
        if not checked["eligible"]:
            raise ValueError("Independent proof check failed: " + checked["code"])
        cycles = collector_provenance(proof, observed_at, dashboard["collector"])
        current = proof["current"]
        result.update(valid=True, binding=binding, session_id=proof["session_id"],
                      generation=generation, cycle_id=cycles[1], previous_cycle_id=cycles[0],
                      source_reported_at=current["source_reported_at"], received_at=current["received_at"],
                      committed_at=current["acquisition"]["committed_at"], quote_hash=current["quote_hash"],
                      cumulative_volume=current["cumulative_volume"], source=current["source"],
                      source_url=current["source_url"], previous_close=current["previous_close"])
    except (KeyError, TypeError, ValueError, InvalidOperation, AttributeError) as error:
        result["reasons"].append(str(error))
    return result


def rule_configuration(observation, generation, version):
    if not isinstance(observation.get("anomalies"), dict) or "items" not in observation["anomalies"]:
        return {"status": "unknown", "actual": {}, "reason": "Rule response is unavailable"}
    try:
        item = target_item(observation["anomalies"])
        rule = item["rule"]
        actual = fields(rule, RULE_FIELDS)
        matched = (item.get("created_at") == generation and rule.get("watchlist_created_at") == generation
                   and rule.get("version") == version and rule.get("enabled") in (True, 1)
                   and decimal(rule.get("up_threshold")) == Decimal(3)
                   and decimal(rule.get("down_threshold")) == Decimal(3))
        return {"status": "matched" if matched else "changed", "actual": actual,
                "watchlist_created_at": item.get("created_at"),
                "reason": "Authorized 3% up/down rule is enabled" if matched else "Rule or watchlist changed; it was not restored"}
    except (KeyError, TypeError, ValueError, InvalidOperation) as error:
        return {"status": "changed", "actual": {}, "reason": "Target rule/watchlist is missing or malformed; not restored: " + str(error)}


def repeated_source_failure(observations, generation):
    """Require evidence of target quote failures in three background cycles."""
    failed = []
    for observation in observations[-3:]:
        try:
            binding = release_binding(observation)
            dashboard = observation["dashboard"]
            observed_at = timestamp(dashboard["as_of"])
            if session_context(observed_at)["state"] != "trading":
                return None
            item = target_item(dashboard)
            quote, collector = item["quote"], dashboard["collector"]
            if (item["created_at"] != generation or quote.get("refresh_state") != "failed"
                    or collector.get("running") is not True or not collector.get("cycle_id")
                    or not isinstance(collector.get("failed"), int) or collector["failed"] < 1
                    or not collector.get("last_error") or not quote.get("error_message")):
                return None
            last_cycle = timestamp(collector["last_cycle_at"])
            attempted = timestamp(quote["last_attempt_at"])
            heartbeat = timestamp(collector["heartbeat_at"])
            if not last_cycle <= attempted <= observed_at or not 0 <= (observed_at - heartbeat).total_seconds() < 10:
                return None
            failed.append({"binding": binding, "cycle_id": collector["cycle_id"], "last_attempt_at": quote["last_attempt_at"],
                           "server_as_of": dashboard["as_of"], "quote_error": quote["error_message"],
                           "collector_error": collector["last_error"]})
        except (KeyError, TypeError, ValueError):
            return None
    if (len(failed) != 3 or len({item["cycle_id"] for item in failed}) != 3
            or any(item["binding"] != failed[0]["binding"] for item in failed)
            or not all(timestamp(right["last_attempt_at"]) > timestamp(left["last_attempt_at"])
                       for left, right in zip(failed, failed[1:]))):
        return None
    return failed


def event_identity(event):
    return {key: event.get(key) for key in ("id", "mode", "market", "symbol", "direction", "trade_date", "created_at", "run_id", "snapshot")}


def validate_event(event, since, through, generation, version, receipt_as_of=None):
    result = {"id": event.get("id"), "valid": False, "reasons": [],
              "notification_status": "missing", "platform_accepted": False, "user_confirmed": False}
    try:
        snapshot = event["snapshot"]
        quote = snapshot["quote"]
        created = timestamp(event["created_at"])
        if not timestamp(since) <= created <= timestamp(through):
            raise ValueError("Event falls outside the complete history range")
        if event.get("mode") != "live" or snapshot.get("mode") != "live" or event.get("run_id") is not None:
            raise ValueError("A demo or synthetic run cannot satisfy natural live acceptance")
        if (event.get("market"), event.get("symbol")) != TARGET or (quote.get("market"), quote.get("symbol")) != TARGET:
            raise ValueError("Event stock identity is inconsistent")
        direction = event["direction"]
        if direction not in ("up", "down") or snapshot.get("direction") != direction:
            raise ValueError("Event direction is inconsistent")
        if (snapshot.get("watchlist_created_at") != generation or snapshot.get("rule_version") != version
                or any(decimal(snapshot[key]) != Decimal(3) for key in ("threshold", "up_threshold", "down_threshold"))):
            raise ValueError("Event does not belong to the authorized rule generation/version/threshold")
        price, previous = decimal(quote["price"]), decimal(quote["previous_close"])
        if price <= 0 or previous <= 0:
            raise ValueError("Event price or previous close is invalid")
        change = (price - previous) / previous * 100
        if change != decimal(snapshot["change_pct"]) or not (change >= 3 if direction == "up" else change <= -3):
            raise ValueError("Unrounded price calculation does not reach the recorded direction/3% threshold")
        trade_date = timestamp(quote["source_reported_at"]).astimezone(CHINA_TIME).date().isoformat()
        if trade_date != event["trade_date"]:
            raise ValueError("Event trading date does not match its source quote")
        eligibility = snapshot["eligibility"]
        proof = eligibility["proof"]
        current_record_matches_quote(quote, proof)
        if eligibility.get("eligible") is not True:
            raise ValueError("Event snapshot did not have live eligibility")
        checked = proof_eligibility(quote, proof, created, generation)
        if not checked["eligible"]:
            raise ValueError("Historical proof check failed at event time: " + checked["code"])
        collector_provenance(proof, created)
        for name in ("session_id", "session_end_at", "calendar_version"):
            if snapshot.get(name) != proof.get(name):
                raise ValueError("Snapshot session/calendar metadata does not match its proof")
        result.update(valid=True, created_at=event["created_at"], trade_date=trade_date,
                      direction=direction, change_pct=format(change, "f"),
                      immutable_sha256=digest(event_identity(event)),
                      dedup_key="|".join(("live", *TARGET, direction, trade_date)),
                      first_observation=snapshot.get("first_observation"),
                      detail_path=event.get("detail_path"))
        receipt = event.get("notification") or {}
        if receipt.get("event_id") != event["id"] or receipt.get("kind") != "anomaly" or not receipt.get("id"):
            result["reasons"].append("No matching event-linked anomaly notification record")
            return result
        receipt_limit = timestamp(receipt_as_of or through)
        if not created <= timestamp(receipt["created_at"]) <= timestamp(receipt["updated_at"]) <= receipt_limit:
            result["reasons"].append("Notification receipt timestamps are incoherent")
            return result
        status = receipt.get("status")
        result.update(notification_status=status, notification_id=receipt["id"],
                      notification_updated_at=receipt["updated_at"], platform_accepted=status == "accepted")
        if receipt.get("confirmed_at"):
            confirmed = timestamp(receipt["confirmed_at"])
            result["user_confirmed"] = bool(status == "accepted" and created <= confirmed <= receipt_limit)
            result["confirmed_at"] = receipt["confirmed_at"]
        if status != "accepted":
            result["reasons"].append("The event-linked notification is " + str(status) + "; platform acceptance is unproved")
    except (KeyError, TypeError, ValueError, InvalidOperation, AttributeError) as error:
        result["valid"] = False
        result["platform_accepted"] = False
        result["reasons"].append(str(error))
    return result


def collection_streak(points):
    """Keep the existing consecutive/distinct sample contract for every window."""
    streak, achieved = [], None
    for point in points:
        if not point["valid"]:
            streak = []
            continue
        if streak:
            old = streak[-1]
            comparable = (point["binding"] == old["binding"] and point["session_id"] == old["session_id"]
                          and timestamp(point["source_reported_at"]) > timestamp(old["source_reported_at"])
                          and timestamp(point["received_at"]) > timestamp(old["received_at"])
                          and decimal(point["cumulative_volume"]) > decimal(old["cumulative_volume"])
                          and all(point[key] == old[key] for key in ("source", "source_url", "previous_close"))
                          and point["cycle_id"] not in {row["cycle_id"] for row in streak})
            if not comparable:
                streak = []
        streak.append(point)
        if len(streak) >= REQUIRED_VALID_SAMPLES:
            achieved = deepcopy(streak[-REQUIRED_VALID_SAMPLES:])
    return streak, achieved


def analyze_observations(observations, previous_state=None, *, since=DEFAULT_SINCE,
                         expected_generation=DEFAULT_GENERATION, expected_rule_version=DEFAULT_RULE_VERSION):
    """Pure evaluator. HTTP 200, heartbeat and fixture data alone cannot pass it."""
    timestamp(since)
    state = deepcopy(previous_state or {})
    for key in ("collection_passes", "events", "event_acceptances", "notified", "candidates"):
        state.setdefault(key, {})
    state["schema_version"] = 1
    latest = observations[-1] if observations else {}
    points = [collection_point(item, expected_generation) for item in observations]
    streak, achieved = collection_streak(points)
    try:
        binding = release_binding(latest)
        scope = digest({**{key: binding[key] for key in ("release_id", "source_sha256", "app_version")}, "generation": expected_generation})
    except (KeyError, TypeError, ValueError):
        binding, scope = None, None
    # An earlier release passing in a mixed batch does not pass the latest release.
    if achieved and (binding is None or achieved[-1]["binding"] != binding):
        achieved = None
    if achieved and scope:
        state["collection_passes"][scope] = {"binding": binding, "points": achieved}
    prior_pass = state["collection_passes"].get(scope) if scope else None
    collection = {"status": "passed" if achieved else "pending", "observed_in_run": bool(achieved),
                  "cumulative_pass": bool(prior_pass), "scope": scope, "points": points,
                  "consecutive_valid_samples": len(streak),
                  "reason": "Three distinct current persisted background samples were independently verified" if achieved
                  else "Waiting for three distinct valid background observations in one release/process/session"}
    if prior_pass:
        collection["verified_evidence"] = prior_pass

    configurations = [rule_configuration(item, expected_generation, expected_rule_version) for item in observations]
    rule = configurations[-1] if configurations else {"status": "unknown", "reason": "No observations"}
    # last_checked_at/last_result are operational fields, not a configuration change.
    # An unavailable GET is unknown; it is not evidence that a rule was changed.
    config_keys = "market symbol watchlist_created_at up_threshold down_threshold enabled version updated_at"
    rule["changed_during_run"] = len({digest(fields(item.get("actual"), config_keys)) for item in configurations
                                      if item["status"] != "unknown"}) > 1

    history = latest.get("history", {})
    acceptance_scope = digest({"since": timestamp(since).astimezone(timezone.utc).isoformat(),
                               "generation": expected_generation, "rule_version": expected_rule_version})
    scoped_acceptances = state["event_acceptances"].setdefault(acceptance_scope, {})
    meta = history.get("event_history", {})
    complete = meta.get("complete") is True and meta.get("next_cursor") is None and not history.get("errors")
    try:
        complete = complete and timestamp(meta["since"]) == timestamp(since) and timestamp(since) <= timestamp(meta["through"]) <= timestamp(history["as_of"])
    except (KeyError, TypeError, ValueError):
        complete = False
    events, event_ids, keys = [], {}, {}
    for raw in history.get("events", []):
        event = validate_event(raw, since, meta.get("through"), expected_generation, expected_rule_version, history.get("as_of"))
        events.append(event)
        identity = event.get("id")
        if identity in event_ids:
            complete = False
            event["valid"] = False
            event["reasons"].append("Duplicate event id in history")
        event_ids[identity] = event
        prior = state["events"].get(identity)
        if prior and prior.get("immutable_sha256") != digest(event_identity(raw)):
            event["valid"] = False
            event["reasons"].append("An immutable event snapshot changed since the previous run")
            for scoped in state["event_acceptances"].values():
                if identity in scoped:
                    scoped[identity]["valid"] = False
                    scoped[identity]["platform_accepted"] = False
        if raw.get("mode") == "live" and (raw.get("market"), raw.get("symbol")) == TARGET:
            raw_key = "|".join(str(raw.get(key)) for key in ("mode", "market", "symbol", "direction", "trade_date"))
            keys.setdefault(raw_key, []).append(event)
    for rows in keys.values():
        if len(rows) > 1:
            for event in rows:
                event["valid"] = False
                event["reasons"].append("Same-day same-direction deduplication violated")
    accepted = [event for event in events if event["valid"] and event["platform_accepted"]]
    for event in events:
        if event["valid"] and complete:
            state["events"][event["id"]] = deepcopy(event)
            scoped_acceptances[event["id"]] = deepcopy(event)
        elif event.get("id") in state["events"] and not event["valid"]:
            if event["id"] in scoped_acceptances:
                scoped_acceptances[event["id"]]["valid"] = False
                scoped_acceptances[event["id"]]["platform_accepted"] = False
    cumulative_accepted = [item for item in scoped_acceptances.values() if item.get("valid") and item.get("platform_accepted")]
    natural = {"status": "passed" if accepted and complete else "pending", "history_complete": complete,
               "since": since, "through": meta.get("through"), "accepted_event_ids": [event["id"] for event in accepted] if complete else [],
               "cumulative_pass": bool(cumulative_accepted), "acceptance_scope": acceptance_scope, "user_confirmation_separate": True,
               "reason": "Natural live event, exact 3% rule, same-day deduplication and matching platform acceptance verified" if accepted and complete
               else ("History is incomplete; missed events cannot be ruled out" if not complete else "No qualifying natural event with a matching accepted notification yet")}

    candidates = []
    if prior_pass and scope:
        candidates.append({"key": "collection:" + scope, "kind": "collection_passed", "release_id": prior_pass["binding"]["release_id"]})
    failures = repeated_source_failure(observations, expected_generation)
    collection["failure_observations"] = failures or []
    if failures:
        signature = {"scope": scope, "generation": expected_generation,
                     "errors": sorted({(item["quote_error"], item["collector_error"]) for item in failures})}
        candidates.append({"key": "collection_failure:" + digest(signature), "kind": "collection_needs_attention",
                           "reason": "The target quote failed in three distinct background cycles during trading", "observations": failures})
    if rule["status"] == "changed" or rule["changed_during_run"]:
        candidates.append({"key": "configuration:" + digest(fields(rule.get("actual"), config_keys)), "kind": "rule_changed", "reason": rule["reason"]})
    notification_config = latest.get("notifications", {})
    config_ready = (notification_config.get("configured") is True and notification_config.get("enabled") is True
                    and bool(notification_config.get("verified_at")) and bool(notification_config.get("target_fingerprint_sha256"))
                    and not notification_config.get("config_error") and not notification_config.get("check_error"))
    fingerprint = notification_config.get("target_fingerprint_sha256")
    fingerprints = {item["notifications"]["target_fingerprint_sha256"] for item in observations
                    if isinstance(item.get("notifications"), dict)
                    and item["notifications"].get("target_fingerprint_sha256")}
    notification_changed = (len(fingerprints) > 1
                            or bool(state.get("notification_target") and fingerprint != state["notification_target"]))
    if not config_ready or notification_changed:
        candidates.append({"key": "notification_configuration:" + digest(notification_config), "kind": "notification_configuration_changed", "reason": "Notification configuration is incomplete, disabled or changed"})
    if fingerprint:
        state["notification_target"] = fingerprint
    for event in events:
        if event["valid"] and event["platform_accepted"] and complete:
            candidates.append({"key": "event:" + str(event["id"]) + ":accepted", "kind": "natural_event_accepted", "event": event})
        elif event["valid"] and event["notification_status"] in ("failed", "cancelled", "expired", "unknown"):
            candidates.append({"key": "event:" + str(event["id"]) + ":" + event["notification_status"], "kind": "event_delivery_needs_attention", "event": event})
        elif not event["valid"]:
            candidates.append({"key": "event:" + str(event["id"]) + ":invalid:" + digest(event["reasons"]), "kind": "event_evidence_needs_attention", "event": event})
    if latest.get("errors") or history.get("errors"):
        reasons = latest.get("errors", []) + history.get("errors", [])
        candidates.append({"key": "probe_error:" + digest(reasons), "kind": "probe_error", "reasons": reasons})
    # Keep candidate audit history, but do not keep reporting a resolved fault.
    # Achievement candidates are recomputed from still-valid cumulative evidence.
    for candidate in state["candidates"].values():
        candidate["active"] = False
    for candidate in candidates:
        candidate["active"] = True
        state["candidates"][candidate["key"]] = candidate
    pending = [candidate for key, candidate in state["candidates"].items() if candidate.get("active") and key not in state["notified"]]
    return {"continuous_collection": collection, "rule_configuration": rule, "natural_alert": natural,
            "notification_configuration": {"ready": config_ready, "changed": notification_changed},
            "events": events, "notification_candidates": pending, "state": state}


def acknowledge(state, keys, at):
    result = deepcopy(state)
    for key in keys:
        if key not in result.get("candidates", {}) and key not in result.get("notified", {}):
            raise ValueError("Cannot acknowledge an unknown notification candidate")
        result.setdefault("notified", {})[key] = times(at)
    return result


def run_probe(client, *, samples=3, max_observations=None, interval=65, since=DEFAULT_SINCE, planned_at=None,
              triggered_at=None, previous_state=None, expected_generation=DEFAULT_GENERATION,
              expected_rule_version=DEFAULT_RULE_VERSION, clock=None, sleep=None, progress=print):
    # samples retains its original requested count; callers opt into extra reads.
    # Neither count can lower the three-valid-sample acceptance requirement.
    max_observations = samples if max_observations is None else max_observations
    if (type(samples) is not int or type(max_observations) is not int
            or not 1 <= samples <= max_observations <= 10 or not 1 <= interval <= 300):
        raise ValueError("Use 1-10 requested observations, a maximum between that count and 10, and a 1-300 second interval")
    clock = clock or (lambda: datetime.now(timezone.utc))
    sleep = sleep or time.sleep
    started = clock()
    report = {"schema_version": 1, "mode": "GET-only-live-observation", "target": {"market": TARGET[0], "symbol": TARGET[1], "generation": expected_generation, "rule_version": expected_rule_version, "up_threshold": "3", "down_threshold": "3"},
              "schedule": {"planned_at": times(planned_at), "triggered_at": times(triggered_at), "started_at": times(started)},
              "requested_samples": samples, "interval_seconds": interval, "observations": [],
              "sampling": {"required_valid_samples": REQUIRED_VALID_SAMPLES, "max_observations": max_observations}}
    initial_binding, initial_session = None, None
    stop_code = "observation_limit"
    for index in range(max_observations):
        progress(f"Observing {index + 1}/{max_observations}: release, persisted quotes, rule and historical live events", flush=True)
        observation = observe(client, since, clock)
        report["observations"].append(observation)
        # Re-evaluate from the saved prior state. Intermediate attempts do not
        # acknowledge candidates, persist success, or cause notification writes.
        analysis = analyze_observations(report["observations"], previous_state, since=since,
                                        expected_generation=expected_generation, expected_rule_version=expected_rule_version)
        try:
            binding = release_binding(observation)
        except (KeyError, TypeError, ValueError):
            binding = None
            if any(key in observation and after in observation and observation[key] != observation[after]
                   for key, after in (("release", "release_after"), ("health", "health_after"))):
                stop_code = "identity_changed"
                break
        if binding:
            if initial_binding is not None and binding != initial_binding:
                stop_code = "identity_changed"
                break
            initial_binding = binding
        try:
            context = session_context(timestamp(observation["dashboard"]["as_of"]))
        except (KeyError, TypeError, ValueError):
            # A transient dashboard GET failure can consume one bounded retry
            # during local trading hours. Local time never validates a sample.
            context = session_context(clock())
        if context["state"] != "trading":
            stop_code = "outside_session"
            break
        if initial_session is not None and context["session_id"] != initial_session:
            stop_code = "session_changed"
            break
        initial_session = context["session_id"]
        rule = analysis["rule_configuration"]
        if rule["status"] == "changed" or rule["changed_during_run"]:
            stop_code = "rule_changed"
            break
        notification = analysis["notification_configuration"]
        if "notifications" in observation and (not notification["ready"] or notification["changed"]):
            stop_code = "notification_configuration_changed"
            break
        if index + 1 >= samples and analysis["continuous_collection"]["observed_in_run"]:
            stop_code = "collection_passed"
            break
        if index < max_observations - 1:
            remaining = interval
            while remaining > 0:
                delay = min(10, remaining)
                sleep(delay)
                remaining -= delay
    report.update(analysis)
    report["sampling"].update(actual_observations=len(report["observations"]), stop_code=stop_code,
                              consecutive_valid_samples=analysis["continuous_collection"]["consecutive_valid_samples"])
    report["sampling_stop_reason"] = {
        "collection_passed": "Three consecutive distinct valid background observations were verified in this run",
        "observation_limit": "The bounded observation window ended; no extra GET or notification was attempted",
        "outside_session": "Outside a verified continuous-auction session; historical events were still inspected",
        "session_changed": "The continuous-auction session changed; samples were not combined across sessions",
        "identity_changed": "The release or process identity changed; this observation window was stopped",
        "rule_changed": "The authorized rule or watchlist changed; no settings were restored",
        "notification_configuration_changed": "Notification configuration was unavailable for use, disabled or changed; no settings were restored",
    }[stop_code]
    progress(report["sampling_stop_reason"], flush=True)
    completed = clock()
    report["schedule"]["completed_at"] = times(completed)
    report["schedule"]["start_delay_seconds"] = max(0, (started - timestamp(planned_at)).total_seconds()) if planned_at else None
    report["schedule"]["trigger_delay_seconds"] = max(0, (timestamp(triggered_at) - timestamp(planned_at)).total_seconds()) if planned_at and triggered_at else None
    report["schedule"]["started_late"] = bool(planned_at and started > timestamp(planned_at))
    report["requests"] = deepcopy(getattr(client, "requests", []))
    report["state"]["last_run"] = {"schedule": report["schedule"], "collection_status": report["continuous_collection"]["status"], "natural_alert_status": report["natural_alert"]["status"]}
    return report


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--max-observations", type=int,
                        help="Maximum GET observation batches, including bounded retries; defaults to --samples")
    parser.add_argument("--interval", type=float, default=65)
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--since", default=DEFAULT_SINCE)
    parser.add_argument("--planned-at")
    parser.add_argument("--triggered-at")
    parser.add_argument("--expected-generation", default=DEFAULT_GENERATION)
    parser.add_argument("--expected-rule-version", type=int, default=DEFAULT_RULE_VERSION)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--ack-notified", action="append", default=[], metavar="KEY")
    parser.add_argument("--ack-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        for value in (args.since, args.planned_at, args.triggered_at, args.expected_generation):
            if value:
                timestamp(value)
        if args.state and args.output.resolve() == args.state.resolve():
            raise ValueError("Output and cumulative state must use different files")
        state = json.loads(args.state.read_text(encoding="utf-8")) if args.state and args.state.exists() else {}
        if not isinstance(state, dict):
            raise ValueError("Cumulative state must be a JSON object")
        if args.ack_only:
            if not args.state or not args.ack_notified:
                raise ValueError("Acknowledgement-only mode needs --state and --ack-notified")
            state = acknowledge(state, args.ack_notified, datetime.now(timezone.utc))
            write_json(args.state, state)
            write_json(args.output, {"mode": "local-notification-acknowledgement", "acknowledged_keys": args.ack_notified, "notified": state["notified"]})
            print("Local user-notification acknowledgement saved; no HTTP request was made", flush=True)
            return 0
        report = run_probe(ReadOnlyClient(args.base_url, args.timeout), samples=args.samples,
                           max_observations=args.max_observations, interval=args.interval,
                           since=args.since, planned_at=args.planned_at, triggered_at=args.triggered_at,
                           previous_state=state, expected_generation=args.expected_generation,
                           expected_rule_version=args.expected_rule_version)
        if args.ack_notified:
            report["state"] = acknowledge(report["state"], args.ack_notified, datetime.now(timezone.utc))
            report["notification_candidates"] = [item for item in report["notification_candidates"] if item["key"] not in report["state"]["notified"]]
        if args.state:
            write_json(args.state, report["state"])
        write_json(args.output, report)
        print(json.dumps({"output": str(args.output.resolve()), "collection": report["continuous_collection"]["status"],
                          "collection_previously_verified": report["continuous_collection"]["cumulative_pass"],
                          "natural_alert": report["natural_alert"]["status"], "history_complete": report["natural_alert"]["history_complete"],
                          "notification_candidate_keys": [item["key"] for item in report["notification_candidates"]]}, ensure_ascii=False), flush=True)
        return 0
    except (ValueError, OSError, TypeError) as error:
        print("Live observation did not complete: " + str(error), file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
