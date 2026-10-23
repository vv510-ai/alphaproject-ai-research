"""Bounded observer retry regressions; all market input and HTTP are isolated."""

from copy import deepcopy

import pytest

from backend.tests.test_live_monitor_probe import (
    FakeClient, live_event, market, no_external_http, observation, samples,
)
from scripts import verify_live_monitor as probe


class SequenceClient(FakeClient):
    """Serve complete observation batches, without replacing their evaluator."""

    def __init__(self, rows, *, failures=None):
        super().__init__(rows[0])
        self.rows = rows
        self.index = 0
        self.releases = 0
        self.failures = failures or {}

    def get(self, endpoint, params=None):
        if endpoint == "release.json":
            self.releases += 1
            if self.releases % 2:
                self.row = self.rows[min(self.index, len(self.rows) - 1)]
        failure = endpoint in self.failures.get(self.index, ())
        if failure:
            self.requests.append({"method": "GET", "endpoint": endpoint})
            result = None
        else:
            result = super().get(endpoint, params)
        if endpoint == "release.json" and self.releases % 2 == 0:
            self.index += 1
        if failure:
            raise ValueError("Synthetic GET unavailable: " + endpoint)
        return result


def more_samples(market, count=6):
    rows = samples(market)
    while len(rows) < count:
        market.clock.advance(60)
        market.feed.publish(price="103.00", volume=str(1000 + (len(rows) + 1) * 100))
        market.collector.tick()
        rows.append(observation(market))
    return rows


def stale(row):
    changed = deepcopy(row)
    changed["dashboard"]["items"][0]["quote"]["eligibility"] = {
        "eligible": False, "code": "stale", "proof": None,
    }
    return changed


def run(market, rows, *, max_observations=6, failures=None, previous_state=None):
    client = SequenceClient(rows, failures=failures)
    sleeps = []
    report = probe.run_probe(
        client, samples=3, max_observations=max_observations, interval=65,
        expected_generation=market.generation, clock=market.clock,
        sleep=sleeps.append, progress=lambda *args, **kwargs: None,
        previous_state=previous_state,
    )
    return report, client, sleeps


def test_bounded_window_recovers_stale_first_observation_without_weakening_three_point_rule(market):
    rows = more_samples(market)
    rows[0] = stale(rows[0])
    legacy = probe.run_probe(
        SequenceClient(rows), samples=3, expected_generation=market.generation,
        clock=market.clock, sleep=lambda seconds: None, progress=lambda *args, **kwargs: None,
    )
    assert len(legacy["observations"]) == 3
    assert legacy["continuous_collection"]["status"] == "pending"
    assert [point["valid"] for point in legacy["continuous_collection"]["points"]] == [False, True, True]

    report, client, sleeps = run(market, rows)
    assert len(report["observations"]) == 4
    assert report["continuous_collection"]["status"] == "passed"
    assert report["sampling"]["required_valid_samples"] == 3
    assert report["sampling"]["max_observations"] == 6
    assert report["sampling"]["consecutive_valid_samples"] == 3
    assert report["sampling"]["stop_code"] == "collection_passed"
    assert [point["valid"] for point in report["continuous_collection"]["points"]] == [False, True, True, True]
    verified = report["continuous_collection"]["verified_evidence"]["points"]
    assert len(verified) == 3 and len({point["cycle_id"] for point in verified}) == 3
    assert [item["kind"] for item in report["notification_candidates"]] == ["collection_passed"]
    assert sum(sleeps) == 195 and max(sleeps) <= 10
    assert len(client.requests) == 32
    assert all(request["method"] == "GET" for request in client.requests)
    assert {request["endpoint"] for request in client.requests} == probe.ENDPOINTS


@pytest.mark.parametrize("invalid_indexes, expected_count, expected_status", [
    ({1}, 5, "passed"),
    ({1, 3}, 6, "pending"),
])
def test_invalid_observations_reset_the_streak_instead_of_counting_scattered_valid_points(
    market, invalid_indexes, expected_count, expected_status,
):
    rows = more_samples(market)
    rows = [stale(row) if index in invalid_indexes else row for index, row in enumerate(rows)]
    report, _, _ = run(market, rows)
    assert len(report["observations"]) == expected_count
    assert report["continuous_collection"]["status"] == expected_status
    if expected_status == "pending":
        assert report["sampling"]["stop_code"] == "observation_limit"
        assert report["sampling"]["consecutive_valid_samples"] == 2


def test_repeated_cache_exhausts_only_the_bounded_window_without_passing(market):
    row = samples(market)[-1]
    report, _, sleeps = run(market, [row] * 6)
    assert len(report["observations"]) == 6
    assert report["continuous_collection"]["status"] == "pending"
    assert report["sampling"]["stop_code"] == "observation_limit"
    assert report["sampling"]["consecutive_valid_samples"] == 1
    assert sum(sleeps) == 325


@pytest.mark.parametrize("changed", ["release", "process"])
def test_identity_change_stops_retry_without_combining_different_runtime_samples(market, changed):
    rows = more_samples(market)
    for row in rows[1:]:
        if changed == "release":
            row["release"]["release_id"] = row["health"]["release_id"] = "replacement-release"
        else:
            row["health"]["instance_id"] = "replacement-process"
    report, _, sleeps = run(market, rows)
    assert len(report["observations"]) == 2
    assert report["continuous_collection"]["status"] == "pending"
    assert report["sampling"]["stop_code"] == "identity_changed"
    assert sum(sleeps) == 65


def test_restart_inside_one_get_batch_ends_the_window_without_retrying_old_identity(market):
    class RestartingClient(SequenceClient):
        health_reads = 0

        def get(self, endpoint, params=None):
            result = super().get(endpoint, params)
            if endpoint == "api/health":
                self.health_reads += 1
                if self.health_reads == 2:
                    result["instance_id"] = "restarted-during-reads"
            return result

    client = RestartingClient(more_samples(market))
    sleeps = []
    report = probe.run_probe(
        client, samples=3, max_observations=6, expected_generation=market.generation,
        clock=market.clock, sleep=sleeps.append, progress=lambda *args, **kwargs: None,
    )
    assert len(report["observations"]) == 1 and sleeps == []
    assert report["continuous_collection"]["status"] == "pending"
    assert report["sampling"]["stop_code"] == "identity_changed"


def test_session_change_stops_the_window_without_carrying_morning_points_into_afternoon(market):
    morning = samples(market)[:1]
    market.clock.set("2026-10-22T13:00:00+08:00")
    afternoon = samples(market)
    report, _, _ = run(market, morning + afternoon)
    assert len(report["observations"]) == 2
    assert report["continuous_collection"]["status"] == "pending"
    assert report["sampling"]["stop_code"] == "session_changed"


@pytest.mark.parametrize("changed", ["rule", "notification_disabled", "notification_target"])
def test_configuration_change_ends_sampling_and_is_reported_without_restoring_settings(market, changed):
    rows = more_samples(market)
    for row in rows[1:]:
        if changed == "rule":
            row["anomalies"]["items"][0]["rule"]["up_threshold"] = "4"
        elif changed == "notification_disabled":
            row["notifications"]["enabled"] = False
        else:
            row["notifications"]["target_fingerprint_sha256"] = "c" * 64
    report, client, _ = run(market, rows)
    assert len(report["observations"]) == 2
    assert report["continuous_collection"]["status"] == "pending"
    assert report["sampling"]["stop_code"] == (
        "rule_changed" if changed == "rule" else "notification_configuration_changed"
    )
    kind = "rule_changed" if changed == "rule" else "notification_configuration_changed"
    assert any(candidate["kind"] == kind for candidate in report["notification_candidates"])
    assert all(request["method"] == "GET" for request in client.requests)


@pytest.mark.parametrize("endpoint", ["release.json", "api/dashboard", "api/anomalies", "api/notifications"])
def test_first_network_failure_is_preserved_then_recovered_within_the_same_window(market, endpoint):
    rows = more_samples(market)
    report, _, _ = run(market, rows, failures={0: [endpoint]})
    assert len(report["observations"]) == 4
    assert report["observations"][0]["errors"]
    assert report["continuous_collection"]["status"] == "passed"
    assert report["rule_configuration"]["changed_during_run"] is False
    assert [item["kind"] for item in report["notification_candidates"]] == ["collection_passed"]


def test_persistent_network_failure_exhausts_limit_without_false_configuration_change(market):
    rows = more_samples(market)
    report, _, sleeps = run(market, rows, failures={index: ["api/dashboard", "api/anomalies"] for index in range(6)})
    assert len(report["observations"]) == 6
    assert report["continuous_collection"]["status"] == "pending"
    assert report["sampling"]["stop_code"] == "observation_limit"
    assert report["rule_configuration"]["changed_during_run"] is False
    assert [item["kind"] for item in report["notification_candidates"]] == ["probe_error"]
    assert sum(sleeps) == 325


def test_offhours_stops_after_one_history_check_even_with_extra_observation_budget(market):
    event = live_event(samples(market)[-1])
    market.clock.set("2026-10-22T15:35:00+08:00")
    row = observation(market)
    row["history"]["events"] = [event]
    report, _, sleeps = run(market, [row])
    assert len(report["observations"]) == 1 and sleeps == []
    assert report["continuous_collection"]["status"] == "pending"
    assert report["natural_alert"]["status"] == "passed"
    assert report["sampling"]["stop_code"] == "outside_session"


def test_window_ends_when_market_closes_before_three_valid_points(market):
    rows = samples(market)[:2]
    market.clock.set("2026-10-22T15:00:00+08:00")
    rows.append(observation(market))
    report, _, sleeps = run(market, rows)
    assert len(report["observations"]) == 3
    assert report["continuous_collection"]["status"] == "pending"
    assert report["sampling"]["stop_code"] == "outside_session"
    assert sum(sleeps) == 130


def test_existing_acknowledgement_is_not_repeated_when_new_window_recovers(market):
    rows = more_samples(market)
    rows[0] = stale(rows[0])
    first, _, _ = run(market, rows)
    keys = [candidate["key"] for candidate in first["notification_candidates"]]
    state = probe.acknowledge(first["state"], keys, market.clock())
    next_report, _, _ = run(market, rows, previous_state=state)
    assert next_report["continuous_collection"]["status"] == "passed"
    assert next_report["notification_candidates"] == []


@pytest.mark.parametrize("maximum", [0, 2, 11, 3.5, True])
def test_invalid_maximum_is_rejected_before_any_get(market, maximum):
    client = SequenceClient(samples(market))
    with pytest.raises(ValueError):
        probe.run_probe(client, samples=3, max_observations=maximum)
    assert client.requests == []
