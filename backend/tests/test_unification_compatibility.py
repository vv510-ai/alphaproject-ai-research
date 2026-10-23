"""Published-code compatibility, with synthetic SQLite and all external I/O blocked.

The pinned release is exercised as an isolated Python subprocess, not re-created
SQL. Passing the data round trip does not authorize running that release after
unification: its budget reader predates the imported ledger. A separate test
records that limit and verifies the current release gate rejects that binary.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
from types import ModuleType

from fastapi.testclient import TestClient
import httpx
import pytest

from backend.app.ai_config import AIConfig
from backend.app.extended_rules import DEFAULT_CONFIG
from backend.app.feishu_config import FeishuConfig
from backend.app.main import create_app
from backend.app.securities import Security
from backend.tests.test_legacy_archive import source_data

ROOT = Path(__file__).resolve().parents[2]
RELEASE = 'lab-20261022T195932667353Z'
ARCHIVE_SHA256 = '393bed481447124ace54e7ac8fd67887240572d2db2f1fff5f04c39b236499a6'
NOW = datetime(2026, 10, 23, 1, 30, tzinfo=timezone.utc)
HEADERS = {'X-Alpha-Request': 'watchlist-v1', 'Origin': 'http://testserver'}
AI = AIConfig('sk-isolated-never-send', 'https://api.deepseek.com/v1', 'deepseek-flash', '1', '10', NOW.isoformat())
FEISHU = FeishuConfig('cli_isolated', 'NEVER_SEND', 'oc_isolated', 'ou_isolated', '隔离测试群', '隔离测试机器人', NOW.isoformat())
RUNTIME_TABLES = {'anomaly_runtime', 'briefing_runtime'}


# Only the released backend is importable in this process. No production path,
# credential directory or environment setting is passed to it.
OLD_DRIVER = r'''
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import socket
import sys
from unittest.mock import patch
import uuid

root, operation, database, ids_file = sys.argv[1:]
sys.path.insert(0, root)
from fastapi.testclient import TestClient
import httpx
from backend.app import main, notification_service, feishu_config
from backend.app.ai_client import Completion
from backend.app.ai_config import AIConfig
from backend.app.anomaly_rules import Sample
from backend.app.daily import parse_daily
from backend.app.feishu_config import FeishuConfig
from backend.app.quotes import Quote
from backend.app.securities import Security, market_for

assert Path(main.__file__).resolve().is_relative_to(Path(root).resolve())
clock_value = datetime(2026, 10, 23, 1, 30, tzinfo=timezone.utc)
clock = lambda: clock_value
HEADERS = {'X-Alpha-Request': 'watchlist-v1', 'Origin': 'http://testserver'}
AI = AIConfig('sk-isolated-never-send', 'https://api.deepseek.com/v1', 'deepseek-flash', '1', '10', clock().isoformat())
FEISHU = FeishuConfig('cli_isolated', 'NEVER_SEND', 'oc_isolated', 'ou_isolated', '隔离测试群', '隔离测试机器人', clock().isoformat())
blocked_attempts = []
original_connect = socket.socket.connect

def connect(sock, address):
    if isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1'):
        return original_connect(sock, address)  # Python/AnyIO's local socketpair.
    blocked_attempts.append('socket')
    raise AssertionError('External sockets forbidden in compatibility test')

def forbidden_http(*args, **kwargs):
    blocked_attempts.append('http')
    raise AssertionError('External HTTP forbidden in compatibility test')

class Resolver:
    def resolve(self, symbol):
        return Security(symbol, market_for(symbol), '上版合成股票', '隔离身份夹具', clock().isoformat())

class Quotes:
    def fetch(self, symbol):
        return Quote(market_for(symbol), symbol, '上版合成股票', '103', '100', '3', '3',
                     clock().isoformat(), clock().isoformat(), '隔离报价夹具', '', 'synthetic')

class Daily:
    def fetch(self, symbol, start, end):
        key = market_for(symbol) + symbol
        row = [end.isoformat(), '101.00', '102.00', '110.00', '99.00', '100.00', {}, '0.01', '102.00']
        body = json.dumps({'code': 0, 'data': {key: {'day': [row], 'qt': {key: ['1', '隔离股票', symbol]}}}})
        return parse_daily(body, symbol, start, end, clock())

class NoSender:
    def __getattr__(self, name):
        raise AssertionError('Notification delivery forbidden in compatibility test')

class Model:
    def __init__(self): self.calls = 0
    def complete(self, messages):
        self.calls += 1
        data = json.loads(messages[-1]['content'])
        echo = data['factual_echo']
        change = {'kind': 'change', 'text': f'这份记录的现价为 {echo["price"]} 元，昨收为 {echo["previous_close"]} 元，相对昨收上涨，涨跌幅为 {echo["change_pct"]}%。这描述的是事件当时的价格差异。', 'evidence_ids': ['F2', 'F3']}
        body = {'evidence_hash': data['evidence_hash'], 'factual_echo': echo.copy(),
                'cause_status': 'insufficient_evidence', 'missing_evidence_ids': ['news', 'volume', 'sector']}
        if 'snapshot_id' in data:
            body.update(snapshot_id=data['snapshot_id'], statements=[change,
                {'kind': 'context', 'text': '这是用户固定的一份报价，未据此判断是否触发提醒，实时性未验证，不能代替此刻行情。', 'evidence_ids': ['F1', 'F4']},
                {'kind': 'scope', 'text': '解释对应来源时间的当时记录，只描述保存的价格差异，不能判断变化会持续多久。', 'evidence_ids': ['F5', 'F6']}])
        else:
            body.update(event_id=data['event_id'], statements=[change,
                {'kind': 'trigger', 'text': f'当时的变化达到你设置的上涨 {echo["threshold"]}% 提醒线，恰好达到也满足。它说明规则成立，不能据此确定变化会持续。', 'evidence_ids': ['F3', 'F4']},
                {'kind': 'scope', 'text': '本条是合成演练，不是真实行情。解释只针对保存的当时依据。采样仅表明那次检查达到条件，同股票同方向当天最多提醒一次。', 'evidence_ids': ['F1', 'F5', 'F6']}])
        return Completion(json.dumps(body, ensure_ascii=False), 'deepseek-flash', 'isolated', 1200, 500, 'stop', 1)

def read_history(client, ids):
    paths = {
        'event': '/api/events/' + ids['event'],
        'event_ai': '/api/events/' + ids['event'] + '/explanations',
        'quote_ai': '/api/quote-snapshots/' + ids['quote'] + '/explanations',
        'briefings': '/api/briefings', 'notifications': '/api/notifications',
    }
    result = {}
    for name, path in paths.items():
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)
        result[name] = response.json()
    return result

with ExitStack() as stack:
    stack.enter_context(patch.object(socket.socket, 'connect', connect))
    stack.enter_context(patch.object(httpx.HTTPTransport, 'handle_request', forbidden_http))
    stack.enter_context(patch.object(httpx.AsyncHTTPTransport, 'handle_async_request', forbidden_http))
    stack.enter_context(patch.object(feishu_config, 'service_credential', lambda name: None))
    stack.enter_context(patch.object(notification_service, 'load_config', lambda path: FEISHU))
    model = Model()
    app = main.create_app(Path(database), resolver=Resolver(), quote_source=Quotes(), daily_source=Daily(),
                          notification_client=NoSender(), allowed_hosts=['testserver'], clock=clock,
                          run_monitor=False, ai_client=model, ai_config=AI)
    if operation == 'locked':
        try:
            with TestClient(app):
                raise AssertionError('A second application acquired the database')
        except RuntimeError as error:
            assert '已有服务运行' in str(error), str(error)
            print(json.dumps({'lock_blocked': True, 'external_attempts': blocked_attempts}))
        sys.exit(0)
    with TestClient(app) as client:
        if operation == 'seed':
            added = client.post('/api/watchlist', headers=HEADERS, json={'symbol': '600519'})
            assert added.status_code == 201, added.text
            with app.state.store.connect() as db, db:
                db.execute('UPDATE watchlist SET created_at=?', (clock().isoformat(),))
            item = app.state.store.get('sh', '600519')
            rule = app.state.anomalies.save_rule('sh', '600519', item['created_at'], '3', '3', False, 0)
            event = app.state.anomalies.evaluate(rule, Sample(Quotes().fetch('600519'), 'trading', True, True), mode='demo', notify=False)[0]['event_id']
            evidence = client.get('/api/events/' + event + '/explanations').json()['evidence']
            sent = client.post('/api/events/' + event + '/explanations', headers=HEADERS,
                               json={'request_id': str(uuid.uuid4()), 'evidence_hash': evidence['evidence_hash']})
            assert sent.status_code == 202, sent.text
            app.state.quotes.refresh('sh', '600519')
            preview = client.get('/api/stocks/sh/600519/explanations').json()['current']
            saved = client.post('/api/stocks/sh/600519/explanation-snapshots', headers=HEADERS,
                                json={k: preview[k] for k in ('quote_hash', 'watchlist_created_at')})
            assert saved.status_code == 200, saved.text
            quote = saved.json()['snapshot']['id']
            evidence = client.get('/api/quote-snapshots/' + quote + '/explanations').json()['evidence']
            sent = client.post('/api/quote-snapshots/' + quote + '/explanations', headers=HEADERS,
                               json={'request_id': str(uuid.uuid4()), 'evidence_hash': evidence['evidence_hash']})
            assert sent.status_code == 202, sent.text
            # A second unrequested snapshot makes later old/new budget decisions comparable.
            clock_value += timedelta(seconds=90)
            app.state.quotes.refresh('sh', '600519')
            preview = client.get('/api/stocks/sh/600519/explanations').json()['current']
            saved = client.post('/api/stocks/sh/600519/explanation-snapshots', headers=HEADERS,
                                json={k: preview[k] for k in ('quote_hash', 'watchlist_created_at')})
            unrequested = saved.json()['snapshot']['id']
            briefing = str(uuid.uuid4())
            queued = client.post('/api/briefings', headers=HEADERS,
                                 json={'request_id': briefing, 'report_date': '2026-10-23'})
            assert queued.status_code == 202, queued.text
            app.state.briefings.tick()
            notification = str(uuid.uuid4())
            with app.state.store.connect() as db, db:
                db.execute("""INSERT INTO notification_messages(id,created_at,updated_at,target_fingerprint,group_name,bot_name,status,receipt_hash,kind,event_id)
                              VALUES(?,?,?,?,?,?,'delivered',?,'event',?)""",
                           (notification,clock().isoformat(),clock().isoformat(),FEISHU.fingerprint,FEISHU.group_name,FEISHU.bot_name,'0'*64,event))
            ids = {'event': event, 'quote': quote, 'unrequested': unrequested,
                   'briefing': briefing, 'notification': notification}
            Path(ids_file).write_text(json.dumps(ids), encoding='utf-8')
            result = read_history(client, ids)
            assert result['event_ai']['records'][0]['status'] == 'succeeded'
            assert result['quote_ai']['records'][0]['status'] == 'succeeded'
            assert result['briefings']['reports'][0]['status'] == 'succeeded'
        else:
            # Stay after the frozen snapshot; old quote validation rejects future evidence.
            clock_value += timedelta(seconds=90)
            ids = json.loads(Path(ids_file).read_text(encoding='utf-8'))
            result = read_history(client, ids)
            if operation == 'write':
                original = client.get('/api/watchlist').json()['items']
                duplicate = client.post('/api/watchlist', headers=HEADERS, json={'symbol': '600519'})
                assert duplicate.status_code == 200 and duplicate.json()['created'] is False
                added = client.post('/api/watchlist', headers=HEADERS, json={'symbol': '002594'})
                assert added.status_code == 201, added.text
                result['added'] = added.json()['item']
                removed = client.delete('/api/watchlist/sz/002594', headers=HEADERS)
                assert removed.json() == {'removed': True}, removed.text
                assert client.get('/api/watchlist').json()['items'] == original
            elif operation == 'budget':
                path = '/api/quote-snapshots/' + ids['unrequested'] + '/explanations'
                state = client.get(path).json()
                result['budget_before'] = state['budget']
                sent = client.post(path, headers=HEADERS,
                                   json={'request_id': str(uuid.uuid4()), 'evidence_hash': state['evidence']['evidence_hash']})
                result['queue_status'] = sent.status_code
                result['after'] = client.get(path).json()
        result['model_calls'] = model.calls
        result['external_attempts'] = blocked_attempts
        result['module_file'] = main.__file__
        print(json.dumps(result, ensure_ascii=False))
'''


def rows(path):
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
        return {table[0]: [dict(row) for row in db.execute(f'SELECT * FROM "{table[0]}" ORDER BY rowid')]
                for table in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def assert_preserved(before, after, ignored=RUNTIME_TABLES):
    for table, previous in before.items():
        if table in ignored:
            continue
        assert len(after[table]) == len(previous), table
        for old, new in zip(previous, after[table]):
            assert {key: new[key] for key in old} == old, table


def old_run(release, operation, database, ids_file):
    # -I removes PYTHONPATH/user-site; the allowlist excludes all app secrets and
    # the credential/public-URL environment inherited by other processes.
    env = {key: os.environ[key] for key in ('SYSTEMROOT', 'WINDIR', 'COMSPEC') if key in os.environ}
    env.update(TEMP=str(database.parent), TMP=str(database.parent))
    process = subprocess.run([sys.executable, '-I', '-X', 'utf8', str(release / 'compat_driver.py'),
                              str(release), operation, str(database), str(ids_file)],
                             cwd=release, env=env, capture_output=True, text=True, encoding='utf-8', timeout=45)
    assert process.returncode == 0, process.stdout + '\n' + process.stderr
    result = json.loads(process.stdout)
    assert result['external_attempts'] == []
    return result


@pytest.fixture(scope='module')
def published_seed(tmp_path_factory):
    candidates = [ROOT / '.runtime' / 'cloud-packages' / (RELEASE + '.tar.gz')]
    if os.name == 'posix':
        candidates.append(Path('/root/alphaproject-lab-deploy/incoming') / RELEASE / (RELEASE + '.tar.gz'))
    archive = next((path for path in candidates if path.is_file()), None)
    if archive is None:
        pytest.skip('Pinned prior release package is absent from both the local package cache and POSIX incoming archive')
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == ARCHIVE_SHA256
    root = tmp_path_factory.mktemp('published-compatibility')
    release = root / 'previous'
    release.mkdir()
    with tarfile.open(archive, 'r:gz') as package:
        count = 0
        for member in package.getmembers():
            if not member.name.startswith('backend/app/'):
                continue
            path = PurePosixPath(member.name)
            assert member.isfile() and path.suffix == '.py' and '..' not in path.parts
            assert not path.is_absolute() and member.size < 250_000
            target = release.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(package.extractfile(member).read())
            count += 1
    assert count >= 20
    (release / 'compat_driver.py').write_text(OLD_DRIVER, encoding='utf-8')
    database, ids_file = root / 'previous.sqlite3', root / 'ids.json'
    result = old_run(release, 'seed', database, ids_file)
    return {'root': release, 'database': database, 'ids_file': ids_file,
            'ids': json.loads(ids_file.read_text(encoding='utf-8')), 'result': result}


@pytest.fixture
def offline(monkeypatch):
    from backend.app import explanation_service, feishu_config, notification_service
    for key in ('ALPHA_LAB_PUBLIC_URL', 'ALPHA_LAB_DB', 'CREDENTIALS_DIRECTORY'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(notification_service, 'load_config', lambda _: FEISHU)
    monkeypatch.setattr(explanation_service, 'load_config', lambda _: AI)
    monkeypatch.setattr(feishu_config, 'service_credential', lambda _: None)
    attempts = []
    original_connect = socket.socket.connect

    def connect(sock, address):
        if isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1'):
            return original_connect(sock, address)
        attempts.append('socket')
        raise AssertionError('External sockets forbidden in compatibility test')

    def forbidden(*args, **kwargs):
        attempts.append('http')
        raise AssertionError('External HTTP forbidden in compatibility test')

    monkeypatch.setattr(socket.socket, 'connect', connect)
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', forbidden)
    yield
    assert attempts == []


def copy_seed(seed, tmp_path):
    path = tmp_path / 'migrated.sqlite3'
    shutil.copyfile(seed['database'], path)
    return path


@contextmanager
def current(path):
    # All reads stay after the second quote in the old synthetic ledger.
    clock = lambda: datetime(2026, 10, 23, 1, 32, tzinfo=timezone.utc)
    class ForbiddenModel:
        calls = 0
        def complete(self, messages):
            self.calls += 1
            raise AssertionError('The current application must reject before any model call')
    app = create_app(path, allowed_hosts=['testserver'], run_monitor=False, clock=clock,
                     ai_config=AI, ai_client=ForbiddenModel())
    with TestClient(app) as client:
        yield app, client


def import_original(app):
    item = app.state.store.get('sh', '600519')
    group = app.state.store.create_group('新版保留分组')
    app.state.store.update_metadata('sh', '600519', item['created_at'], 0, group['id'], True)
    existing = app.state.extended.save('sh', '600519', item['created_at'], 0, False,
                                     {**DEFAULT_CONFIG, 'volume_enabled': True, 'volume_ratio': '5'})
    data = source_data()
    data['llm_budget'] = [{'on_date': '2026-10-23', 'cost_cny': '0.99'},
                          {'on_date': '2026-10-22', 'cost_cny': '0.10'}]
    effective = {**DEFAULT_CONFIG, 'limit_enabled': True, 'breakout_enabled': True,
                 'volume_enabled': True, 'amplitude_enabled': True, 'event_enabled': True,
                 'volume_ratio': '4.25', 'amplitude_pct': '9.5', 'lookback_days': 20}
    settings = {'overrides': {'volume_ratio': 4.25}, 'effective': effective}
    identity = Security('002119', 'sz', '原版合成股票', '隔离重新核验', NOW.isoformat())
    first = app.state.legacy.import_snapshot(data, {'002119': identity}, settings)
    repeated = app.state.legacy.import_snapshot(data, {}, settings)
    assert repeated['replayed'] and repeated['source_fingerprint'] == first['source_fingerprint']
    assert first['extended_rules_imported'] == 1
    imported = app.state.extended.rule('sz', '002119')
    assert imported['config'] == effective and imported['enabled'] is True
    assert app.state.extended.rule('sh', '600519') == existing
    assert app.state.store.get('sz', '000001') is None
    return first


def test_published_app_reads_and_writes_migrated_database_without_overwriting_history(published_seed, tmp_path, offline):
    database = copy_seed(published_seed, tmp_path)
    initial = rows(database)
    ids = published_seed['ids']
    with current(database) as (app, client):
        assert_preserved(initial, rows(database))
        import_original(app)
        event = client.get('/api/events/' + ids['event'] + '/explanations').json()
        quote = client.get('/api/quote-snapshots/' + ids['quote'] + '/explanations').json()
        assert event['records'][0]['output'] == published_seed['result']['event_ai']['records'][0]['output']
        assert quote['records'][0]['evidence'] == published_seed['result']['quote_ai']['records'][0]['evidence']
        assert client.get('/api/legacy/history').json()['total'] == 10
        migrated = rows(database)
    previous = old_run(published_seed['root'], 'write', database, published_seed['ids_file'])
    assert previous['model_calls'] == 0
    assert previous['added']['is_holding'] == 0 and previous['added']['metadata_version'] == 0
    assert previous['event_ai']['records'][0]['output'] == event['records'][0]['output']
    assert previous['quote_ai']['records'][0]['evidence'] == quote['records'][0]['evidence']
    assert previous['notifications']['records'][0]['confirmed_at'] is None
    assert_preserved(migrated, rows(database))
    with current(database) as (app, client):
        assert app.state.store.get('sh', '600519')['is_holding'] is True
        assert app.state.extended.rule('sz', '002119')['config']['volume_ratio'] == '4.25'
        assert client.get('/api/legacy/history').json()['total'] == 10
        assert client.get('/api/events/' + ids['event']).status_code == 200
        assert client.get('/api/briefings').json()['reports'][0]['id'] == ids['briefing']
        assert_preserved(migrated, rows(database))


def test_old_and_new_binaries_cannot_both_recover_or_write_the_same_database(published_seed, tmp_path, offline):
    database = copy_seed(published_seed, tmp_path)
    with current(database):
        before = rows(database)
        result = old_run(published_seed['root'], 'locked', database, published_seed['ids_file'])
        assert result['lock_blocked']
        assert_preserved(before, rows(database), ignored=set())


def test_old_budget_scope_excludes_imported_usage_so_it_requires_the_release_guard(published_seed, tmp_path, offline):
    database = copy_seed(published_seed, tmp_path)
    path = '/api/quote-snapshots/' + published_seed['ids']['unrequested'] + '/explanations'
    with current(database) as (app, client):
        import_original(app)
        state = client.get(path).json()
        assert state['budget']['day_used'] == '1.002800'
        assert state['budget']['month_used'] == '1.102800'
        assert state['budget']['legacy_day_used'] == '0.990000'
        response = client.post(path, headers=HEADERS, json={
            'request_id': 'e62915cb-0e25-4977-80bf-34598b52ec77', 'evidence_hash': state['evidence']['evidence_hash']})
        assert response.status_code == 429 and response.json()['error']['code'] == 'ai_budget_exhausted'
        before = rows(database)
    previous = old_run(published_seed['root'], 'budget', database, published_seed['ids_file'])
    assert previous['budget_before']['day_used'] == '0.012800'
    assert previous['queue_status'] == 202 and previous['model_calls'] == 1
    assert previous['after']['records'][0]['status'] == 'succeeded'
    # This is an explicit unsupported rollback boundary, not a full-compatibility pass.
    with current(database) as (app, client):
        assert client.get(path).json()['budget']['day_used'] == '1.009200'
        with app.state.store.connect() as db:
            assert db.execute('SELECT COUNT(*) FROM legacy_budget_usage').fetchone()[0] == 2
        after = rows(database)
        for table in ('legacy_records', 'legacy_budget_usage', 'legacy_migrations'):
            assert before[table] == after[table]


def test_real_preflight_preserves_imported_archive_and_published_histories(published_seed, tmp_path, offline, monkeypatch, capsys):
    from deployment.cloud import preflight
    database = copy_seed(published_seed, tmp_path)
    with current(database) as (app, _):
        import_original(app)
    before = rows(database)
    monkeypatch.setattr(sys, 'argv', ['preflight.py', str(database)])
    preflight.main()
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['status'] == 'passed' and receipt['external_http_calls'] == 0
    assert set(receipt['tables_preserved']) == set(before) - RUNTIME_TABLES
    assert {'legacy_records', 'legacy_budget_usage', 'ai_explanations', 'ai_request_aliases'} <= set(receipt['tables_preserved'])
    after = rows(database)
    assert_preserved(before, after, ignored=RUNTIME_TABLES | {'ai_subject_evidence'})
    # Preflight may bind the one old snapshot that had never requested AI.
    assert len(after['ai_subject_evidence']) == len(before['ai_subject_evidence']) + 1
    assert all(row in after['ai_subject_evidence'] for row in before['ai_subject_evidence'])
    assert after['ai_subject_evidence'][-1]['subject_key'] == 'quote:' + published_seed['ids']['unrequested']


@pytest.mark.parametrize('fault', ['missing_table', 'missing_row', 'modified_amount',
                                  'missing_archive', 'modified_archive', 'missing_migration'])
def test_imported_budget_faults_remain_blocked_after_app_restart(published_seed, tmp_path, offline, fault):
    database = copy_seed(published_seed, tmp_path)
    path = '/api/quote-snapshots/' + published_seed['ids']['unrequested'] + '/explanations'
    with current(database) as (app, client):
        import_original(app)
        digest = client.get(path).json()['evidence']['evidence_hash']
        original_ledger = rows(database)['ai_explanations']
    # Faults affect only this disposable synthetic database; they must not be
    # repaired into an apparently unused budget when CREATE IF NOT EXISTS runs.
    with sqlite3.connect(database) as db:
        if fault == 'missing_table':
            db.execute('DROP TABLE legacy_budget_usage')
        elif fault == 'missing_row':
            db.execute("DELETE FROM legacy_budget_usage WHERE budget_day='2026-10-23'")
        elif fault == 'modified_amount':
            db.execute("UPDATE legacy_budget_usage SET charged_micro=0 WHERE budget_day='2026-10-23'")
        elif fault == 'missing_archive':
            db.execute("DELETE FROM legacy_records WHERE kind='budget' AND source_id='2026-10-23'")
        elif fault == 'modified_archive':
            db.execute("UPDATE legacy_records SET content_json=? WHERE kind='budget' AND source_id='2026-10-23'",
                       (json.dumps({'on_date': '2026-10-23', 'cost_cny': '0.01'}),))
        else:
            db.execute('DELETE FROM legacy_migrations')
    with current(database) as (app, client):
        response = client.post(path, headers=HEADERS, json={
            'request_id': 'e62915cb-0e25-4977-80bf-34598b52ec77', 'evidence_hash': digest})
        assert response.status_code == 409, response.text
        assert response.json()['error']['code'] == 'budget_history_unavailable'
        assert app.state.explanations.client.calls == 0
        assert rows(database)['ai_explanations'] == original_ledger
        assert client.get('/api/events/' + published_seed['ids']['event']).status_code == 200


def test_unified_release_gate_rejects_actual_published_code_before_any_system_action(published_seed, tmp_path, monkeypatch):
    # Only fcntl/pwd import shims are needed on Windows. No shell, symlink,
    # credential, backup, publication or service function may run in this check.
    for name in ('fcntl', 'pwd'):
        if importlib.util.find_spec(name) is None:
            monkeypatch.setitem(sys.modules, name, ModuleType(name))
    specification = importlib.util.spec_from_file_location(
        'deployment.cloud._compat_remote_release', ROOT / 'deployment' / 'cloud' / 'remote_release.py')
    remote_release = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(remote_release)
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'unified-site.json').write_text('{}', encoding='utf-8')
    monkeypatch.setattr(remote_release, 'STATE', state)
    monkeypatch.setattr(remote_release, 'current_id', lambda: 'lab-unified')
    monkeypatch.setattr(remote_release, 'release_path', lambda _: published_seed['root'])
    monkeypatch.setattr(remote_release, 'verify_tree', lambda _: {'release': {'release_id': RELEASE}})
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError('System action forbidden in compatibility test')

    for name in ('run', 'preflight', 'backup', 'select', 'install_unit', 'publish'):
        monkeypatch.setattr(remote_release, name, forbidden)
    with pytest.raises(RuntimeError, match='imported budget and archive'):
        remote_release.activate(RELEASE, 'lab-unified')
    assert not calls
