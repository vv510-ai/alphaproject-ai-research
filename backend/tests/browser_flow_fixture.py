"""Loopback-only browser fixture; actual app/SQLite, controlled external inputs.

Driven by frontend/tests/persistent-flow.mjs. Controls use the parent's stdin,
never an application route. No production data or credential vault is opened.
"""
import argparse
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import re
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
    allowed = (ROOT / '.runtime' / 'persistent-flow').resolve()
    if not directory.is_relative_to(allowed) or directory == allowed:
        raise RuntimeError('Use a new fixture directory under .runtime/persistent-flow')
    if args.resume:
        if not (directory / 'fixture.json').is_file():
            raise RuntimeError('Only this fixture can be resumed')
    else:
        directory.mkdir(parents=True, exist_ok=False)
    database = directory / 'fixture.sqlite3'
    for name in ('CREDENTIALS_DIRECTORY', 'ALPHA_LAB_PUBLIC_URL', 'ALPHA_LAB_DB'):
        os.environ.pop(name, None)
    os.environ['ALPHA_LAB_DB'] = str(database)

    import httpx
    import uvicorn
    from backend.app import notification_service
    from backend.app.ai_client import AITransportFailure
    from backend.app.main import create_app
    from backend.tests.test_explanations import FakeModel, CONFIG as MODEL_CONFIG
    from backend.tests.test_live_acceptance import Clock, RawFeed, FakeFeishuWire, CONFIG, SYMBOL, security

    blocked_network = []

    def blocked(*_args, **_kwargs):
        blocked_network.append('HTTP transport')
        raise AssertionError('Real external HTTP is forbidden in the browser fixture')

    async def blocked_async(*_args, **_kwargs):
        return blocked()

    httpx.HTTPTransport.handle_request = blocked
    httpx.AsyncHTTPTransport.handle_async_request = blocked_async
    original_connect = socket.socket.connect

    def local_connect(sock, address):
        if isinstance(address, tuple) and address[0] not in ('127.0.0.1', '::1'):
            blocked_network.append('non-loopback socket')
            raise AssertionError('External sockets are forbidden in the browser fixture')
        return original_connect(sock, address)

    socket.socket.connect = local_connect
    notification_service.load_config = lambda _path: CONFIG
    clock = Clock('2026-10-23T10:00:00+08:00' if args.resume else '2026-10-22T10:00:00+08:00')
    feed, wire = RawFeed(clock), FakeFeishuWire()

    class Model(FakeModel):
        def complete(self, messages):
            clock.advance(1)  # Preserve ordering between explicitly retried requests.
            if not self.calls:
                self.calls.append(messages)
                raise AITransportFailure('fixture_timeout', '隔离模型首次超时，请手动重试。', uncertain=True)
            return super().complete(messages)

    class Resolver:
        def resolve(self, symbol):
            return security(clock, symbol)

    class Daily:
        def fetch(self, *_args, **_kwargs):
            raise AssertionError('Daily acquisition is outside this browser regression')

    model = Model()
    app = create_app(database, Resolver(), ['127.0.0.1'], quote_source=feed, clock=clock,
                     daily_source=Daily(), notification_client=wire.client, run_monitor=False,
                     ai_client=model, ai_config=MODEL_CONFIG)
    app_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(instance):
        async with app_lifespan(instance):
            if not args.resume:
                item, _ = app.state.store.add(security(clock))
                app.state.anomalies.save_rule('sh', SYMBOL, item['created_at'], '3', '3', True, 0)
                app.state.notifications.set_enabled(True)
                feed.publish(price='102.00', volume='1000')
                app.state.quotes.refresh('sh', SYMBOL)
                clock.advance()
                feed.publish(price='103.00', volume='1100')
                app.state.quotes.refresh('sh', SYMBOL)
                app.state.anomalies.check_live()
                app.state.notifications.process_pending_events()
                event = app.state.anomalies.state()['events'][0]
                assert event['mode'] == 'live' and event['snapshot']['eligibility']['eligible']
                assert event['notification']['status'] == 'accepted' and len(wire.messages) == 1
                content = json.loads(wire.messages[0]['content'])
                text = content['text'] if 'text' in content else content['zh_cn']['content'][0][0]['text']
                fixture = {'event_id': event['id'], 'notification_id': event['notification']['id'],
                           'receipt_code': re.search(r'收件核验码：(\d{6})', text).group(1)}
                (directory / 'fixture.json').write_text(json.dumps(fixture), encoding='utf-8')
            yield

    app.router.lifespan_context = lifespan
    listener = socket.socket()
    listener.bind(('127.0.0.1', args.port))
    listener.listen(128)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error', access_log=False))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.03)
        if not server.started:
            raise RuntimeError('The isolated application did not start')
        fixture = json.loads((directory / 'fixture.json').read_text(encoding='utf-8'))
        emit({'ready': True, 'pid': os.getpid(), 'port': port, **fixture})
        for line in sys.stdin:
            command = json.loads(line)
            if command['action'] == 'stop':
                break
            if command['action'] != 'stats':
                raise RuntimeError('Unknown fixture control')
            with app.state.store.connect() as db:
                records = [dict(row) for row in db.execute(
                    'SELECT id,status,reviewed_at,charged_micro FROM ai_explanations ORDER BY rowid')]
                notification = dict(db.execute(
                    'SELECT id,status,confirmed_at,confirmation_attempts FROM notification_messages WHERE id=?',
                    (fixture['notification_id'],)).fetchone())
                integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
                count = db.execute('SELECT count(*) FROM watchlist').fetchone()[0]
            emit({'model_calls': len(model.calls), 'simulated_messages': len(wire.messages),
                  'quote_fetches': len(feed.calls), 'blocked_network': blocked_network,
                  'records': records, 'notification': notification, 'watchlist_count': count,
                  'integrity': integrity, 'pid': os.getpid()})
    finally:
        server.should_exit = True
        thread.join(10)
        listener.close()
        if thread.is_alive():
            raise RuntimeError('The fixture did not stop cleanly')


if __name__ == '__main__':
    main()
