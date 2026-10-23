"""Loopback browser fixture with actual API/SQLite and controlled upstream inputs.

Only stdin controls the clock and research source. No testing route is added to
the application. Each invocation uses a fresh private directory, or explicitly
resumes its own marked database. No production data or credentials are opened.
"""
import argparse
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    directory = args.directory.resolve()
    allowed = (ROOT / '.runtime' / 'unified-flow').resolve()
    if not directory.is_relative_to(allowed) or directory == allowed:
        raise RuntimeError('Use a new directory under .runtime/unified-flow')
    marker = directory / 'fixture.json'
    if args.resume:
        if not marker.is_file():
            raise RuntimeError('Only this marked fixture can be resumed')
        metadata = json.loads(marker.read_text(encoding='utf-8'))
    else:
        directory.mkdir(parents=True, exist_ok=False)
        metadata = {'started_at': datetime.now(timezone.utc).isoformat(), 'offset': 0}
        marker.write_text(json.dumps(metadata), encoding='utf-8')
    database = directory / 'fixture.sqlite3'
    for name in ('CREDENTIALS_DIRECTORY', 'ALPHA_LAB_PUBLIC_URL', 'ALPHA_LAB_DB'):
        os.environ.pop(name, None)
    os.environ['ALPHA_LAB_DB'] = str(database)

    import httpx
    import uvicorn
    from backend.app import notification_service
    from backend.app.main import create_app
    from backend.app.securities import Security, market_for
    from backend.tests.test_explanations import CONFIG as MODEL_CONFIG
    from backend.tests.test_briefings import DailySource

    blocked_network, forbidden_calls = [], []

    def blocked(*_args, **_kwargs):
        blocked_network.append('HTTP transport')
        raise AssertionError('External HTTP is forbidden in this fixture')

    async def blocked_async(*_args, **_kwargs):
        return blocked()

    httpx.HTTPTransport.handle_request = blocked
    httpx.AsyncHTTPTransport.handle_async_request = blocked_async
    original_connect = socket.socket.connect

    def local_connect(sock, address):
        if isinstance(address, tuple) and address[0] not in ('127.0.0.1', '::1'):
            blocked_network.append('non-loopback socket')
            raise AssertionError('External sockets are forbidden in this fixture')
        return original_connect(sock, address)

    socket.socket.connect = local_connect
    notification_service.load_config = lambda _path: None

    class Clock:
        def __call__(self):
            return datetime.fromisoformat(metadata['started_at']) + timedelta(seconds=metadata['offset'])

    clock = Clock()

    class Resolver:
        def resolve(self, symbol):
            return Security(symbol, market_for(symbol), '集成股票甲' if symbol == '600519' else '集成股票乙',
                            '受控身份输入，非证券建议', clock().isoformat())

    class Forbidden:
        def fetch(self, *_args, **_kwargs):
            forbidden_calls.append('market or daily')
            raise AssertionError('This management/research check must not acquire stock prices')

        def complete(self, *_args, **_kwargs):
            forbidden_calls.append('model')
            raise AssertionError('This check must not call a model')

    class Research:
        def __init__(self):
            self.calls, self.revision, self.fail = [], 1, False
            self.published = (clock() - timedelta(minutes=1)).isoformat()

        def fetch_section(self, key, market=None, symbol=None):
            self.calls.append([key, market, symbol])
            time.sleep(.12)
            if self.fail:
                raise RuntimeError('受控来源失败')
            if key in ('indices', 'global', 'sectors'):
                items = [{'name': f'受控{key}指标', 'code': f'{key}-1', 'price': 100,
                          'change_pct': 1.25, 'as_of': self.published}]
            elif key == 'calendar':
                items = [{'upstream_id': 'schedule-1', 'title': '受控未来日程',
                          'url': 'https://example.org/calendar',
                          'scheduled_at': (clock() + timedelta(days=1)).isoformat()}]
            else:
                items = [{'upstream_id': f'{key}-1', 'title': f'受控{key}资料',
                          'summary': f'合成数值 {self.revision * 100} 万元，不代表真实资讯。',
                          'url': f'https://example.org/{key}-1', 'published_at': self.published}]
            return {'source': '合成研究输入', 'source_url': 'https://example.org/feed', 'items': items}

    research = Research()
    daily = DailySource(clock)
    daily.fail_for.add('000001')
    forbidden = Forbidden()
    app = create_app(database, Resolver(), ['127.0.0.1'], quote_source=forbidden, clock=clock,
                     daily_source=daily, run_monitor=False, ai_client=forbidden,
                     ai_config=MODEL_CONFIG, research_source=research)
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(instance):
        async with original_lifespan(instance):
            if not args.resume:
                data = {'watchlist_group': [], 'watchlist_item': [], 'llm_budget': [],
                        'briefing_record': [{'on_date': clock().date().isoformat(),
                                             'content_json': json.dumps({'body_text': '合成旧简报'}),
                                             'version': 'full', 'push_status': 'delivered',
                                             'created_at': clock().isoformat()}],
                        'push_log': [{'id': index, 'ts': (clock() - timedelta(minutes=index)).isoformat(),
                                      'status': 'success' if index % 2 else 'delivered', 'retries': 0,
                                      'target': 'synthetic-private-target', 'code': '600519',
                                      'signal': 'volume', 'uuid': f'archive-{index}'} for index in range(1, 33)],
                        'undelivered': [{'id': 1, 'uuid': 'old-unsent', 'request_json': json.dumps({
                            'code': '600519', 'content': {'text': '合成未发送旧记录'},
                            'receive_id': 'synthetic-private-target'}), 'fail_count': 1,
                            'queued_at': clock().isoformat(), 'is_holding': 0}]}
                app.state.legacy.import_snapshot(data, {})
            yield

    app.router.lifespan_context = lifespan
    listener = socket.socket()
    listener.bind(('127.0.0.1', args.port))
    listener.listen(128)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                                         log_level='error', access_log=False))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.03)
        if not server.started:
            raise RuntimeError('Isolated application did not start')
        emit({'ready': True, 'pid': os.getpid(), 'port': port})
        for line in sys.stdin:
            command = json.loads(line)
            if command['action'] == 'stop':
                break
            if command['action'] == 'source':
                research.fail = command.get('fail', False)
                research.revision = command.get('revision', research.revision)
                metadata['offset'] += command.get('advance', 0)
                marker.write_text(json.dumps(metadata), encoding='utf-8')
                emit({'source_updated': True})
                continue
            if command['action'] == 'briefing':
                app.state.briefings.process_next()
                emit({'briefing_processed': True})
                continue
            if command['action'] != 'stats':
                raise RuntimeError('Unknown fixture control')
            with app.state.store.connect() as db:
                tables = ('watchlist', 'watchlist_groups', 'research_snapshots',
                          'legacy_records', 'anomaly_events', 'notification_messages', 'ai_explanations', 'briefing_reports')
                counts = {table: db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in tables}
                integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
                foreign_keys = [list(row) for row in db.execute('PRAGMA foreign_key_check')]
            emit({'counts': counts, 'research_calls': research.calls, 'forbidden_calls': forbidden_calls,
                  'daily_calls': daily.calls,
                  'blocked_network': blocked_network, 'integrity': integrity, 'foreign_keys': foreign_keys,
                  'pid': os.getpid()})
    finally:
        server.should_exit = True
        thread.join(10)
        listener.close()
        if thread.is_alive():
            raise RuntimeError('Isolated application did not stop cleanly')


if __name__ == '__main__':
    main()
