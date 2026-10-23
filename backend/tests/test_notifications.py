"""All sends in this suite use synthetic transports/clients, never the real vault."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
import re
import uuid
from threading import Event

from fastapi.testclient import TestClient
import httpx
import pytest

from backend.app import notification_service as module
from backend.app.feishu import FeishuClient, FeishuFailure, Target
from backend.app.feishu_config import ConfigFailure, FeishuConfig, load_config, read_source_fields, save_config
from backend.app.main import create_app
from backend.app.notification_service import NotificationService
from backend.app.securities import LookupFailure, Security
from backend.app.store import WatchlistStore

NOW = datetime(2026, 10, 17, 19, 0, tzinfo=timezone.utc)
CONFIG = FeishuConfig("cli_fixture", "FAKE_SECRET_FOR_OFFLINE_TEST", "oc_fixture", "ou_fixture", "合成单人群", "合成机器人", NOW.isoformat())
HEADERS = {"X-Alpha-Request": "watchlist-v1", "Origin": "http://testserver"}


class Clock:
    now = NOW

    def __call__(self):
        return self.now

    def advance(self, seconds=31):
        self.now += timedelta(seconds=seconds)


class ClientFixture:
    def __init__(self):
        self.sent = []
        self.checks = 0
        self.failure = self.target_failure = self.during_check = self.during_send = None

    def verify_target(self):
        self.checks += 1
        if self.during_check:
            self.during_check()
        if self.target_failure:
            raise self.target_failure
        return Target(CONFIG.group_name, CONFIG.bot_name, CONFIG.owner_id)

    def send_test(self, request_id, text, detail_url=None):
        self.sent.append((request_id, text))
        if self.during_send:
            self.during_send()
        if self.failure:
            raise self.failure
        return "om_fixture"


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "load_config", lambda _: CONFIG)
    store = WatchlistStore(tmp_path / "test.sqlite3")
    store.initialize()
    clock, client = Clock(), ClientFixture()
    result = NotificationService(store, tmp_path / "not-a-real-vault", client, clock)
    result.initialize()
    return result, client, clock


def queue(service, request_id=None):
    return service.queue_test(request_id or str(uuid.uuid4()), CONFIG.fingerprint)


def test_vault_roundtrip_is_encrypted_and_corruption_is_rejected(tmp_path):
    path = tmp_path / "test.dpapi"
    if os.name != 'nt':
        with pytest.raises(ConfigFailure, match='Windows'):
            save_config(path, CONFIG)
        return
    save_config(path, CONFIG)
    assert CONFIG.app_secret.encode() not in path.read_bytes()
    assert CONFIG.chat_id.encode() not in path.read_bytes()
    assert load_config(path) == CONFIG
    path.write_bytes(b"not valid encrypted data")
    with pytest.raises(ConfigFailure):
        load_config(path)
    assert CONFIG.app_secret not in repr(CONFIG)


def test_config_is_only_loaded_on_server_start(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(module, "load_config", lambda path: calls.append(path))
    store = WatchlistStore(tmp_path / "startup.sqlite3")
    notifications = NotificationService(store, tmp_path / "missing-vault")
    assert calls == []
    store.initialize()
    notifications.initialize()
    assert len(calls) == 1 and notifications.state()["configured"] is False


def test_import_reads_only_allowed_fields_and_rejects_ambiguous_config(tmp_path):
    path = tmp_path / "source.env"
    path.write_text('LARK_APP_ID="cli_test"\nLARK_APP_SECRET=secret\nLARK_RECEIVE_ID=oc_test\nLARK_RECEIVE_ID_TYPE=chat_id\nUNRELATED_KEY=do_not_copy\n', encoding="utf-8")
    result = read_source_fields(path)
    assert set(result) == {"LARK_APP_ID", "LARK_APP_SECRET", "LARK_RECEIVE_ID", "LARK_RECEIVE_ID_TYPE"}
    path.write_text(path.read_text() + "LARK_RECEIVE_ID=oc_other\n")
    with pytest.raises(ConfigFailure):
        read_source_fields(path)


def transport_fixture(change=None, send_result=None):
    calls = []
    def handle(request):
        path = request.url.path
        calls.append((path, request.content))
        if "tenant_access_token" in path:
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "fixture_token", "expire": 7200})
        if path.endswith("/members"):
            data = {"items": [{"member_id": CONFIG.owner_id}], "has_more": False, "member_total": 1}
            body = {"code": 0, "data": data}
        elif path.endswith("/info"):
            body = {"code": 0, "bot": {"activate_status": 2, "app_name": CONFIG.bot_name}}
        elif path.endswith("/messages"):
            if isinstance(send_result, Exception):
                raise send_result
            return httpx.Response(200, json=send_result if send_result is not None else {"code": 0, "data": {"message_id": "om_test"}})
        else:
            body = {"code": 0, "data": {"chat_type": "private", "chat_mode": "group", "user_count": "1", "bot_count": "1", "owner_id": CONFIG.owner_id, "name": CONFIG.group_name}}
        if change:
            change(path, body)
        return httpx.Response(200, json=body)
    return httpx.MockTransport(handle), calls


@pytest.mark.parametrize("kind", ["public", "second_person", "second_bot", "different_owner", "more_members", "wrong_member", "disabled_bot", "missing_data"])
def test_target_validation_fails_closed(kind):
    def change(path, body):
        if path.endswith("/members"):
            if kind == "more_members": body["data"]["has_more"] = True
            if kind == "wrong_member": body["data"]["items"][0]["member_id"] = "ou_other"
        elif path.endswith("/info"):
            if kind == "disabled_bot": body["bot"]["activate_status"] = 1
        else:
            if kind == "public": body["data"]["chat_type"] = "public"
            if kind == "second_person": body["data"]["user_count"] = "2"
            if kind == "second_bot": body["data"]["bot_count"] = "2"
            if kind == "different_owner": body["data"]["owner_id"] = "ou_other"
            if kind == "missing_data": body["data"] = None
    transport, calls = transport_fixture(change)
    with pytest.raises(FeishuFailure):
        FeishuClient(CONFIG, transport).verify_target()
    assert not any(path.endswith("/messages") for path, _ in calls)


def test_fixed_recipient_content_and_uuid_and_cached_token():
    transport, calls = transport_fixture()
    client = FeishuClient(CONFIG, transport)
    target = client.verify_target()
    assert target.owner_id == CONFIG.owner_id
    request_id = str(uuid.uuid4())
    assert client.send_test(request_id, "合成测试内容") == "om_test"
    sent = json.loads(calls[-1][1])
    assert sent == {"receive_id": CONFIG.chat_id, "msg_type": "text", "content": json.dumps({"text": "合成测试内容"}, ensure_ascii=False), "uuid": request_id}
    assert sum("tenant_access_token" in path for path, _ in calls) == 1


@pytest.mark.parametrize("response,unknown", [
    ({"code": 230001, "msg": "SECRET_VALUE_MUST_NOT_SURFACE"}, False),
    ({"code": 0, "data": {}}, True),
    ({"code": False, "data": {}}, True),
    (httpx.ReadTimeout("SECRET_VALUE_MUST_NOT_SURFACE"), True),
    (httpx.WriteError("SECRET_VALUE_MUST_NOT_SURFACE"), True),
    (httpx.ConnectError("SECRET_VALUE_MUST_NOT_SURFACE"), False),
])
def test_send_failure_classification_is_conservative_and_redacted(response, unknown):
    transport, calls = transport_fixture(send_result=response)
    with pytest.raises(FeishuFailure) as error:
        FeishuClient(CONFIG, transport).send_test(str(uuid.uuid4()), "fixture")
    assert error.value.unknown is unknown
    assert "SECRET_VALUE" not in str(error.value)
    assert sum(path.endswith("/messages") for path, _ in calls) == 1


def test_off_is_default_and_state_reads_never_send(service):
    notifications, client, _ = service
    assert not notifications.state()["enabled"]
    assert client.checks == 0 and client.sent == []
    with pytest.raises(LookupFailure):
        queue(notifications)
    assert notifications.state()["records"] == []


def test_same_request_concurrent_replay_is_sent_once(service):
    notifications, client, _ = service
    notifications.set_enabled(True)
    request_id = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(lambda _: queue(notifications, request_id), range(12)))
    bodies = [body for _, body in results if body]
    assert len(bodies) == 1
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _: notifications.process(request_id, bodies[0]), range(4)))
    assert len(client.sent) == 1
    assert notifications.state()["records"][0]["status"] == "accepted"
    assert queue(notifications, request_id)[1] is None


def test_platform_acceptance_does_not_confirm_human_receipt(service):
    notifications, client, _ = service
    notifications.set_enabled(True)
    record, body = queue(notifications)
    notifications.process(record["id"], body)
    state = notifications.state()
    assert state["records"][0]["status"] == "accepted"
    assert state["records"][0]["confirmed_at"] is None
    code = re.search(r"收件核验码：(\d{6})", client.sent[0][1])[1]
    serialized = json.dumps(state)
    assert code not in serialized and "receipt_hash" not in serialized and "provider_message_id" not in serialized
    with notifications.store.connect() as db:
        assert code not in str([tuple(row) for row in db.execute("SELECT * FROM notification_messages")])
    wrong = "000000" if code != "000000" else "111111"
    with pytest.raises(LookupFailure):
        notifications.confirm(record["id"], wrong)
    assert notifications.confirm(record["id"], code)["confirmed_at"] == NOW.isoformat()
    assert notifications.confirm(record["id"], code)["confirmed_at"] == NOW.isoformat()
    assert len(client.sent) == 1


def test_confirmation_attempts_are_bounded(service):
    notifications, client, _ = service
    notifications.set_enabled(True)
    record, body = queue(notifications)
    notifications.process(record["id"], body)
    code = re.search(r"收件核验码：(\d{6})", body)[1]
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(10):
        with pytest.raises(LookupFailure) as error:
            notifications.confirm(record["id"], wrong)
        assert error.value.status == 400
    with pytest.raises(LookupFailure) as error:
        notifications.confirm(record["id"], code)
    assert error.value.status == 429


@pytest.mark.parametrize("stage", ["pending", "checking"])
def test_switch_off_cancels_before_submission_and_reenable_does_not_replay(service, stage):
    notifications, client, _ = service
    notifications.set_enabled(True)
    record, body = queue(notifications)
    if stage == "checking":
        client.during_check = lambda: notifications.set_enabled(False)
    else:
        notifications.set_enabled(False)
    notifications.process(record["id"], body)
    assert notifications.state()["records"][0]["status"] == "cancelled"
    client.during_check = None
    notifications.set_enabled(True)
    notifications.process(record["id"], body)
    assert client.sent == []


def test_switch_off_during_handoff_keeps_real_receipt(service):
    notifications, client, _ = service
    notifications.set_enabled(True)
    record, body = queue(notifications)
    client.during_send = lambda: notifications.set_enabled(False)
    notifications.process(record["id"], body)
    assert not notifications.state()["enabled"]
    assert notifications.state()["records"][0]["status"] == "accepted"
    assert len(client.sent) == 1


def test_late_enable_cannot_override_a_newer_disable(service):
    notifications, client, _ = service
    checking, release = Event(), Event()
    def pause():
        checking.set()
        assert release.wait(5)
    client.during_check = pause
    with ThreadPoolExecutor(max_workers=1) as executor:
        old_enable = executor.submit(notifications.set_enabled, True)
        assert checking.wait(5)
        assert notifications.set_enabled(False)["enabled"] is False
        release.set()
        with pytest.raises(LookupFailure) as error:
            old_enable.result()
    assert error.value.code == "setting_superseded"
    assert notifications.state()["enabled"] is False
    assert client.sent == []


def test_changed_group_stops_send_and_keeps_record(service):
    notifications, client, _ = service
    notifications.set_enabled(True)
    record, body = queue(notifications)
    client.target_failure = FeishuFailure("群成员发生变化")
    notifications.process(record["id"], body)
    assert notifications.state()["records"][0]["status"] == "failed"
    assert client.sent == []


@pytest.mark.parametrize("state", ["pending", "checking", "sending"])
def test_restart_recovers_without_replay(service, state):
    notifications, client, _ = service
    notifications.set_enabled(True)
    record, body = queue(notifications)
    with notifications.store.connect() as db, db:
        db.execute("UPDATE notification_messages SET status = ? WHERE id = ?", (state, record["id"]))
    notifications.initialize()
    expected = "unknown" if state == "sending" else "cancelled"
    assert notifications.state()["records"][0]["status"] == expected
    notifications.process(record["id"], body)
    assert client.sent == []


def test_unknown_result_is_not_retried_and_can_still_be_confirmed(service):
    notifications, client, _ = service
    notifications.set_enabled(True)
    record, body = queue(notifications)
    client.failure = FeishuFailure("回执丢失", True)
    notifications.process(record["id"], body)
    assert notifications.state()["records"][0]["status"] == "unknown"
    notifications.process(record["id"], body)
    assert queue(notifications, record["id"])[1] is None
    code = re.search(r"收件核验码：(\d{6})", body)[1]
    confirmed = notifications.confirm(record["id"], code)
    assert confirmed["status"] == "unknown" and confirmed["confirmed_at"]
    assert len(client.sent) == 1


def test_cooldown_active_guard_and_stale_background_task(service):
    notifications, client, clock = service
    notifications.set_enabled(True)
    record, body = queue(notifications)
    with pytest.raises(LookupFailure) as error:
        queue(notifications)
    assert error.value.code == "notification_busy"
    clock.advance(121)
    assert notifications.state()["records"][0]["status"] == "cancelled"
    notifications.process(record["id"], body)
    assert client.sent == []
    record, body = queue(notifications)
    notifications.process(record["id"], body)
    with pytest.raises(LookupFailure) as error:
        queue(notifications)
    assert error.value.code == "notification_cooldown"


def test_api_boundaries_privacy_and_legacy_records(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "load_config", lambda _: CONFIG)
    client = ClientFixture()
    path = tmp_path / "api.sqlite3"
    old = WatchlistStore(path)
    old.initialize()
    original, _ = old.add(Security("600519", "sh", "合成股票", "fixture", NOW.isoformat()))
    with TestClient(create_app(path, allowed_hosts=["testserver"], notification_client=client, run_monitor=False)) as browser:
        assert browser.get("/api/notifications").json()["configured"]
        assert browser.post("/api/notifications/settings", json={"enabled": True}).status_code == 403
        assert browser.post("/api/notifications/settings", headers=HEADERS, json={"enabled": "true"}).status_code == 422
        assert browser.post("/api/notifications/settings", headers=HEADERS, json={"enabled": True}).status_code == 200
        payload = {"request_id": str(uuid.uuid4()), "target_fingerprint": CONFIG.fingerprint}
        assert browser.post("/api/notifications/test", headers=HEADERS, json={**payload, "receive_id": "oc_other"}).status_code == 422
        assert browser.post("/api/notifications/test", headers=HEADERS, json=payload).status_code == 202
        assert browser.post("/api/notifications/test", headers=HEADERS, json=payload).status_code == 202
        result = browser.get("/api/notifications")
        assert len(client.sent) == 1 and result.json()["records"][0]["status"] == "accepted"
        assert result.json()["records"][0]["confirmed_at"] is None
        for hidden in (CONFIG.app_secret, CONFIG.app_id, CONFIG.owner_id, CONFIG.chat_id, "receipt_hash", "tenant_access_token"):
            assert hidden not in result.text
        assert browser.get("/api/watchlist").json()["items"] == [original]
    reopened = WatchlistStore(path)
    reopened.initialize()
    assert reopened.list_items() == [original]
