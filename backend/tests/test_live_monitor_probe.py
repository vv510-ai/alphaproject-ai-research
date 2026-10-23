"""Adversarial tests for a GET-only observer; all market input is synthetic."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
from types import SimpleNamespace
from urllib.request import OpenerDirector

import pytest

from backend.app.quote_collector import QuoteCollector
from backend.app.quote_service import QuoteService
from backend.app.store import WatchlistStore
from backend.app.securities import LookupFailure
from backend.tests.test_live_acceptance import Clock, RawFeed, SYMBOL, security
from scripts import verify_live_monitor as probe


@pytest.fixture(autouse=True)
def no_external_http(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Probe tests must not contact a live HTTP endpoint")
    monkeypatch.setattr(OpenerDirector, "open", blocked)


@pytest.fixture
def market(tmp_path):
    clock = Clock()
    store = WatchlistStore(tmp_path / "probe.sqlite3")
    store.initialize()
    store.add(security(clock))
    feed = RawFeed(clock)
    quotes = QuoteService(store, feed, clock)
    quotes.initialize()
    collector = QuoteCollector(store, quotes, clock)
    collector.is_alive = lambda: True
    quotes.collector = collector
    generation = store.list_items()[0]["created_at"]
    return SimpleNamespace(clock=clock, store=store, feed=feed, quotes=quotes,
                           collector=collector, generation=generation)


def observation(market):
    now = market.clock().isoformat()
    dashboard = probe.public_response("api/dashboard", market.quotes.dashboard())
    item = deepcopy(dashboard["items"][0])
    item["rule"] = {"market": "sh", "symbol": SYMBOL, "watchlist_created_at": market.generation,
                    "up_threshold": "3", "down_threshold": "3", "enabled": 1,
                    "version": 2, "updated_at": "2026-10-21T16:45:42+00:00"}
    return {"release": {"release_id": "lab-probe-test", "app_version": "0.11.0", "source_sha256": "a" * 64},
            "health": {"status": "ok", "service": "AlphaProject-Lab", "version": "0.11.0",
                       "release_id": "lab-probe-test", "instance_id": "probe-process"},
            "dashboard": dashboard, "anomalies": {"items": [item]},
            "notifications": {"configured": True, "enabled": True, "verified_at": now,
                              "target_fingerprint_sha256": "b" * 64},
            "history": {"events": [], "event_history": {"since": probe.DEFAULT_SINCE,
                        "through": now, "next_cursor": None, "complete": True}, "as_of": now, "errors": []},
            "errors": []}


def samples(market, *, price="103.00"):
    result = []
    for index in range(4):
        if index:
            market.clock.advance(60)
        market.feed.publish(price=price, volume=str(1000 + index * 100))
        market.collector.tick()
        if index:
            result.append(observation(market))
    return result


def evaluate(rows, market, state=None):
    return probe.analyze_observations(rows, state, expected_generation=market.generation)


def live_event(row, *, identity="event-one", direction="up", status="accepted"):
    item = row["dashboard"]["items"][0]
    quote = deepcopy(item["quote"]["data"])
    proof = deepcopy(item["quote"]["eligibility"]["proof"])
    now = row["dashboard"]["as_of"]
    snapshot = {"quote": quote, "change_pct": "3.00" if direction == "up" else "-3.00",
                "direction": direction, "threshold": "3", "rule_version": 2,
                "up_threshold": "3", "down_threshold": "3", "watchlist_created_at": item["created_at"],
                "first_observation": True, "mode": "live", "market_state": "trading",
                "eligibility": {"eligible": True, "proof": proof},
                **{key: proof[key] for key in ("session_id", "session_end_at", "calendar_version")}}
    return {"id": identity, "mode": "live", "market": "sh", "symbol": SYMBOL,
            "direction": direction, "trade_date": quote["source_reported_at"][:10], "created_at": now,
            "run_id": None, "snapshot": snapshot, "detail_path": "/lab/#event=" + identity,
            "notification": {"id": "notification-" + identity, "event_id": identity, "kind": "anomaly",
                             "status": status, "created_at": now, "updated_at": now, "confirmed_at": None}}


def test_three_distinct_current_background_observations_are_required(market):
    rows = samples(market)
    assert evaluate(rows[:2], market)["continuous_collection"]["status"] == "pending"
    assert evaluate([rows[0]] * 3, market)["continuous_collection"]["status"] == "pending"
    result = evaluate(rows, market)
    assert result["continuous_collection"]["status"] == "passed"
    assert result["continuous_collection"]["cumulative_pass"] is True
    assert result["natural_alert"]["status"] == "pending"
    assert result["natural_alert"]["history_complete"] is True
    assert len(result["notification_candidates"]) == 1
    assert result["notification_candidates"][0]["kind"] == "collection_passed"


@pytest.mark.parametrize("corruption", ["manual_current", "manual_previous", "missing_acquisition", "commit_future", "receipt_future", "wrong_cycle", "same_cycle", "stopped", "old_heartbeat", "last_cycle_future", "stale", "no_volume_growth"])
def test_heartbeat_or_manual_or_invalid_proof_cannot_pass(market, corruption):
    rows = samples(market)
    for row in rows:
        view = row["dashboard"]["items"][0]["quote"]
        proof = view["eligibility"]["proof"]
        current, previous = proof["current"], proof["previous"]
        collector = row["dashboard"]["collector"]
        later = (probe.timestamp(row["dashboard"]["as_of"]) + timedelta(seconds=1)).isoformat()
        if corruption == "manual_current":
            current["acquisition"]["origin"] = "manual"
        elif corruption == "manual_previous":
            previous["acquisition"]["origin"] = "manual"
        elif corruption == "missing_acquisition":
            previous.pop("acquisition")
        elif corruption == "commit_future":
            current["acquisition"]["committed_at"] = later
        elif corruption == "receipt_future":
            current["received_at"] = later
        elif corruption == "wrong_cycle":
            collector["cycle_id"] = "another-cycle"
        elif corruption == "same_cycle":
            current["acquisition"]["cycle_id"] = previous["acquisition"]["cycle_id"]
        elif corruption == "stopped":
            collector["running"] = False
        elif corruption == "old_heartbeat":
            collector["heartbeat_at"] = (probe.timestamp(row["dashboard"]["as_of"]) - timedelta(seconds=10)).isoformat()
        elif corruption == "last_cycle_future":
            collector["last_cycle_at"] = later
        elif corruption == "stale":
            row["dashboard"]["as_of"] = (probe.timestamp(row["dashboard"]["as_of"]) + timedelta(seconds=121)).isoformat()
        elif corruption == "no_volume_growth":
            current["cumulative_volume"] = previous["cumulative_volume"]
    result = evaluate(rows, market)
    assert result["continuous_collection"]["status"] == "pending"
    assert result["continuous_collection"]["cumulative_pass"] is False


@pytest.mark.parametrize("changed", ["release", "process", "within_get_batch"])
def test_collection_is_bound_to_one_release_and_process(market, changed):
    rows = samples(market)
    if changed == "release":
        rows[-1]["release"]["release_id"] = rows[-1]["health"]["release_id"] = "new-release"
    elif changed == "process":
        rows[-1]["health"]["instance_id"] = "restarted-process"
    else:
        rows[-1]["health_after"] = {**rows[-1]["health"], "instance_id": "restarted-process"}
    assert evaluate(rows, market)["continuous_collection"]["status"] == "pending"


def test_previous_pass_is_retained_without_pretending_the_current_run_passed(market):
    rows = samples(market)
    first = evaluate(rows, market)
    market.clock.set("2026-10-22T15:35:00+08:00")
    later = observation(market)
    result = evaluate([later], market, first["state"])
    assert result["continuous_collection"]["status"] == "pending"
    assert result["continuous_collection"]["cumulative_pass"] is True
    assert len(result["notification_candidates"]) == 1
    acknowledged = probe.acknowledge(result["state"], [result["notification_candidates"][0]["key"]], market.clock())
    assert evaluate([later], market, acknowledged)["notification_candidates"] == []
    later["release"]["release_id"] = later["health"]["release_id"] = "later-release"
    assert evaluate([later], market, acknowledged)["continuous_collection"]["cumulative_pass"] is False


def test_local_acknowledgement_requires_an_existing_candidate(market):
    with pytest.raises(ValueError, match="unknown notification"):
        probe.acknowledge({}, ["invented-key"], market.clock())


def test_three_real_source_failures_are_actionable_deduplicated_and_resolve(market):
    market.feed.failure = LookupFailure("source_unavailable", "合成来源失败", 502)
    rows = []
    for index in range(3):
        if index:
            market.clock.advance(60)
        market.feed.publish(volume=str(1000 + index * 100))
        market.collector.tick()
        rows.append(observation(market))
    first = evaluate(rows[:2], market)
    assert not any(item["kind"] == "collection_needs_attention" for item in first["notification_candidates"])
    assert not any(item["kind"] == "collection_needs_attention" for item in evaluate([rows[0]] * 3, market)["notification_candidates"])
    failed = evaluate(rows, market)
    candidates = [item for item in failed["notification_candidates"] if item["kind"] == "collection_needs_attention"]
    assert len(candidates) == 1
    assert failed["continuous_collection"]["status"] == "pending"
    acknowledged = probe.acknowledge(failed["state"], [candidates[0]["key"]], market.clock())
    repeated = evaluate(rows, market, acknowledged)
    assert not any(item["kind"] == "collection_needs_attention" for item in repeated["notification_candidates"])
    market.feed.failure = None
    market.clock.advance(60)
    market.feed.publish(volume="1500")
    market.collector.tick()
    # One successful cycle is still warming up, but the source failure has resolved.
    recovered = evaluate([observation(market)], market, failed["state"])
    assert recovered["continuous_collection"]["status"] == "pending"
    assert not any(item["kind"] == "collection_needs_attention" for item in recovered["notification_candidates"])
    assert recovered["state"]["candidates"][candidates[0]["key"]]["active"] is False


def test_missing_target_and_restored_notification_configuration_are_reported_correctly(market):
    row = samples(market)[-1]
    row["notifications"]["enabled"] = False
    first = evaluate([row], market)
    assert any(item["kind"] == "notification_configuration_changed" for item in first["notification_candidates"])
    row["notifications"]["enabled"] = True
    row["anomalies"]["items"] = []
    second = evaluate([row], market, first["state"])
    assert second["rule_configuration"]["status"] == "changed"
    assert any(item["kind"] == "rule_changed" for item in second["notification_candidates"])
    assert not any(item["kind"] == "notification_configuration_changed" for item in second["notification_candidates"])


def test_current_sample_metadata_must_match_the_persisted_quote(market):
    rows = samples(market)
    for row in rows:
        row["dashboard"]["items"][0]["quote"]["eligibility"]["proof"]["current"]["price"] = "104.00"
    assert evaluate(rows, market)["continuous_collection"]["status"] == "pending"


def test_rule_changes_are_reported_without_restoring_configuration(market):
    rows = samples(market)
    rows[-1]["anomalies"]["items"][0]["rule"].update(enabled=0, version=3)
    before = deepcopy(rows)
    result = evaluate(rows, market)
    assert rows == before
    assert result["rule_configuration"]["status"] == "changed"
    assert result["rule_configuration"]["changed_during_run"] is True
    assert any(item["kind"] == "rule_changed" for item in result["notification_candidates"])


@pytest.mark.parametrize("direction,price", [("up", "103.00"), ("down", "97.00")])
def test_historical_natural_event_has_exact_math_linked_receipt_and_separate_confirmation(market, direction, price):
    row = samples(market, price=price)[-1]
    event = live_event(row, direction=direction)
    market.clock.set("2026-10-23T15:35:00+08:00")
    later = observation(market)
    later["history"]["events"] = [event]
    result = evaluate([later], market)
    assert result["natural_alert"]["status"] == "passed"
    assert result["events"][0]["platform_accepted"] is True
    assert result["events"][0]["user_confirmed"] is False
    assert result["events"][0]["first_observation"] is True
    assert result["events"][0]["trade_date"] == "2026-10-22"
    assert result["continuous_collection"]["status"] == "pending"


@pytest.mark.parametrize("corruption", ["demo", "rounded", "direction", "trade_date", "threshold", "generation", "version", "quote_hash", "wrong_receipt", "wrong_kind", "missing_receipt", "failed_receipt", "future_receipt", "incomplete_history"])
def test_event_and_delivery_false_positives_are_rejected(market, corruption):
    row = samples(market)[-1]
    event = live_event(row)
    row["history"]["events"] = [event]
    if corruption == "demo":
        event["mode"] = "demo"
    elif corruption == "rounded":
        event["snapshot"]["change_pct"] = "2.999999999"
    elif corruption == "direction":
        event["direction"] = "down"
    elif corruption == "trade_date":
        event["trade_date"] = "2026-10-21"
    elif corruption == "threshold":
        event["snapshot"]["threshold"] = "0.01"
    elif corruption == "generation":
        event["snapshot"]["watchlist_created_at"] = "changed"
    elif corruption == "version":
        event["snapshot"]["rule_version"] = 3
    elif corruption == "quote_hash":
        event["snapshot"]["eligibility"]["proof"]["current"]["quote_hash"] = "b" * 64
    elif corruption == "wrong_receipt":
        event["notification"]["event_id"] = "another-event"
    elif corruption == "wrong_kind":
        event["notification"]["kind"] = "test"
    elif corruption == "missing_receipt":
        event["notification"] = None
    elif corruption == "failed_receipt":
        event["notification"]["status"] = "failed"
    elif corruption == "future_receipt":
        event["notification"]["updated_at"] = (market.clock() + timedelta(seconds=1)).isoformat()
    elif corruption == "incomplete_history":
        row["history"]["event_history"].update(complete=False, next_cursor="more")
    result = evaluate([row], market)
    assert result["natural_alert"]["status"] == "pending"
    assert result["natural_alert"]["cumulative_pass"] is False


def test_same_day_direction_duplicate_events_fail_even_across_rule_versions(market):
    row = samples(market)[-1]
    first = live_event(row)
    second = live_event(row, identity="event-two")
    second["snapshot"]["rule_version"] = 3
    row["history"]["events"] = [first, second]
    result = evaluate([row], market)
    assert result["natural_alert"]["status"] == "pending"
    assert all(not event["valid"] for event in result["events"])


def test_changed_immutable_event_invalidates_prior_acceptance(market):
    row = samples(market)[-1]
    row["history"]["events"] = [live_event(row)]
    first = evaluate([row], market)
    assert first["natural_alert"]["cumulative_pass"]
    row["history"]["events"][0]["snapshot"]["first_observation"] = False
    result = evaluate([row], market, first["state"])
    assert result["natural_alert"]["status"] == "pending"
    assert result["natural_alert"]["cumulative_pass"] is False


@pytest.mark.parametrize("scope_change", ["since", "generation", "version"])
def test_cumulative_natural_acceptance_is_bound_to_its_expected_scope(market, scope_change):
    row = samples(market)[-1]
    row["history"]["events"] = [live_event(row)]
    first = evaluate([row], market)
    assert first["natural_alert"]["cumulative_pass"] is True
    row["history"]["events"] = []
    options = {"expected_generation": market.generation}
    if scope_change == "since":
        options["since"] = "2026-10-22T00:00:00+00:00"
        row["history"]["event_history"]["since"] = options["since"]
    elif scope_change == "generation":
        options["expected_generation"] = "2026-10-22T02:00:01+00:00"
    else:
        options["expected_rule_version"] = 3
    result = probe.analyze_observations([row], first["state"], **options)
    assert result["natural_alert"]["cumulative_pass"] is False
    assert result["natural_alert"]["acceptance_scope"] != first["natural_alert"]["acceptance_scope"]


def test_receipt_update_after_history_ceiling_uses_server_response_time(market):
    row = samples(market)[-1]
    event = live_event(row)
    event["notification"]["updated_at"] = (market.clock() + timedelta(seconds=1)).isoformat()
    row["history"]["as_of"] = (market.clock() + timedelta(seconds=2)).isoformat()
    row["history"]["events"] = [event]
    assert evaluate([row], market)["natural_alert"]["status"] == "passed"


def test_public_projection_drops_raw_payloads_secrets_and_provider_identifiers(market):
    row = samples(market)[-1]
    event = live_event(row)
    event["snapshot"]["quote"].update(raw_text="DO_NOT_KEEP_RAW", secret="DO_NOT_KEEP_SECRET")
    event["notification"].update(receipt_hash="DO_NOT_KEEP_RECEIPT", provider_message_id="DO_NOT_KEEP_PROVIDER")
    result = probe.public_event(event)
    assert "DO_NOT_KEEP" not in json.dumps(result)
    projected = probe.public_response("api/notifications", {"configured": True, "target_fingerprint": "private-target", "secret": "secret"})
    assert "private-target" not in json.dumps(projected)
    assert projected["target_fingerprint_sha256"]


class FakeClient:
    def __init__(self, row):
        self.row = row
        self.requests = []

    def get(self, endpoint, params=None):
        self.requests.append({"method": "GET", "endpoint": endpoint})
        key = {"release.json": "release", "api/health": "health", "api/dashboard": "dashboard",
               "api/anomalies": "anomalies", "api/notifications": "notifications", "api/anomalies/events": "history"}[endpoint]
        return deepcopy(self.row[key])


def test_offhours_still_checks_history_without_sleeping_and_records_schedule_delay(market):
    original = samples(market)[-1]
    event = live_event(original)
    market.clock.set("2026-10-23T15:35:10+08:00")
    row = observation(market)
    row["history"]["events"] = [event]
    client = FakeClient(row)
    slept = []
    report = probe.run_probe(client, samples=3, interval=65, expected_generation=market.generation,
                             planned_at="2026-10-23T15:35:00+08:00", triggered_at="2026-10-23T15:35:05+08:00",
                             clock=market.clock, sleep=slept.append, progress=lambda *args, **kwargs: None)
    assert len(report["observations"]) == 1 and slept == []
    assert report["natural_alert"]["status"] == "passed"
    assert report["schedule"]["start_delay_seconds"] == 10
    assert report["schedule"]["trigger_delay_seconds"] == 5
    assert report["schedule"]["started_late"] is True
    assert report["schedule"]["completed_at"]["china"].startswith("2026-10-23T15:35:10")
    assert all(item["method"] == "GET" for item in client.requests)
    assert {item["endpoint"] for item in client.requests} == probe.ENDPOINTS


def test_during_trading_waits_in_short_chunks_and_never_invents_pass_from_fixed_cache(market):
    row = samples(market)[-1]
    slept = []
    report = probe.run_probe(FakeClient(row), samples=3, interval=65, expected_generation=market.generation,
                             clock=market.clock, sleep=slept.append, progress=lambda *args, **kwargs: None)
    assert len(report["observations"]) == 3
    assert max(slept) <= 10 and sum(slept) == 130
    assert report["continuous_collection"]["status"] == "pending"
    assert report["schedule"]["planned_at"] is None


def test_history_uses_opaque_cursor_and_a_frozen_ceiling(market):
    row = samples(market)[-1]
    first, second = live_event(row), live_event(row, identity="event-two")
    history = row["history"]
    calls = []

    def get(endpoint, params):
        calls.append((endpoint, deepcopy(params)))
        page = deepcopy(history)
        if len(calls) == 1:
            page["events"] = [first]
            page["event_history"].update(complete=False, next_cursor="opaque-server-cursor")
        else:
            page["events"] = [second]
        return page

    result = probe.fetch_history(SimpleNamespace(get=get), probe.DEFAULT_SINCE)
    assert result["event_history"]["complete"] is True
    assert len(result["events"]) == 2
    assert "cursor" not in calls[0][1]
    assert calls[1][1]["cursor"] == "opaque-server-cursor"


@pytest.mark.parametrize("failure", ["repeated_cursor", "changed_ceiling", "error", "page_cap"])
def test_incomplete_history_never_silently_passes(market, monkeypatch, failure):
    row = samples(market)[-1]
    count = 0
    monkeypatch.setattr(probe, "MAX_HISTORY_PAGES", 2)

    def get(endpoint, params):
        nonlocal count
        count += 1
        if failure == "error":
            raise ValueError("GET unavailable")
        page = deepcopy(row["history"])
        cursor = "same" if failure == "repeated_cursor" else "cursor-" + str(count)
        page["event_history"].update(complete=False, next_cursor=cursor)
        if failure == "changed_ceiling" and count == 2:
            page["event_history"]["through"] = (market.clock() - timedelta(seconds=1)).isoformat()
        return page

    result = probe.fetch_history(SimpleNamespace(get=get), probe.DEFAULT_SINCE)
    assert result["event_history"]["complete"] is False
    assert result["errors"]
    assert count <= 2


def test_http_client_enforces_get_allowlist_no_redirects_and_verified_production_target():
    for base in ("http://8.155.130.86/lab/", "https://example.invalid/", "https://user:secret@8.155.130.86/lab/", "https://8.155.130.86/", "https://8.155.130.86/lab/?token=x"):
        with pytest.raises(ValueError):
            probe.ReadOnlyClient(base)
    client = probe.ReadOnlyClient()
    for endpoint in ("api/quotes/refresh", "api/anomalies/demo", "api/notifications/test", "https://elsewhere.invalid"):
        with pytest.raises(ValueError, match="not allowed"):
            client.get(endpoint)
    sent = []

    class Response(io.BytesIO):
        status = 200

    def open_request(request, timeout):
        sent.append(request)
        assert timeout == 10
        return Response(b'{"release_id":"safe","secret":"DO_NOT_KEEP_SECRET"}')

    client.opener = SimpleNamespace(open=open_request)
    assert client.get("release.json") == {"release_id": "safe"}
    assert sent[0].get_method() == "GET"
    assert not any(key.lower() in ("authorization", "cookie") for key in sent[0].headers)
    assert probe.NoRedirect().redirect_request(None, None, 302, None, None, "https://example.invalid") is None


def test_ack_only_cli_does_no_http_and_keeps_output_separate(tmp_path):
    state_path, output = tmp_path / "state.json", tmp_path / "ack.json"
    probe.write_json(state_path, {"candidates": {"event:x:accepted": {"kind": "natural_event_accepted"}}, "notified": {}})
    assert probe.main(["--state", str(state_path), "--output", str(output), "--ack-only", "--ack-notified", "event:x:accepted"]) == 0
    assert "event:x:accepted" in json.loads(state_path.read_text(encoding="utf-8"))["notified"]
    assert json.loads(output.read_text(encoding="utf-8"))["mode"] == "local-notification-acknowledgement"
    assert probe.main(["--state", str(state_path), "--output", str(state_path), "--ack-only", "--ack-notified", "event:x:accepted"]) == 2
