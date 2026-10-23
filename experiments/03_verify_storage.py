"""Teaching probe: real saved quotes, isolated SQLite, no external messages.

This is a storage experiment, not the application's final schema or API.
It creates a new evidence directory on every run and never alters input data.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / 'evidence' / 'architecture-20261017'
DAILY = ROOT / 'evidence/data-source/daily-600519-20261016T211549232586.json'
SPOT = ROOT / 'evidence/data-source/spot-600519-20261016T220152725509.json'
TZ = timezone(timedelta(hours=8))


def inspect_database(path: Path) -> dict:
    if not path.resolve().is_relative_to(EVIDENCE.resolve()):
        raise ValueError('Only this experiment evidence directory is readable')
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as conn:
        quote = conn.execute('SELECT source_time, received_at, price_yuan FROM quotes').fetchone()
        return {
            'watchlist_count': conn.execute('SELECT count(*) FROM watchlist').fetchone()[0],
            'daily_count': conn.execute('SELECT count(*) FROM daily_bars').fetchone()[0],
            'outbox_count': conn.execute('SELECT count(*) FROM notification_outbox').fetchone()[0],
            'outbox_status': conn.execute('SELECT status FROM notification_outbox').fetchone()[0],
            'source_time': quote[0], 'received_at': quote[1], 'price_yuan': quote[2],
            'integrity_check': conn.execute('PRAGMA integrity_check').fetchone()[0],
        }


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == '--inspect':
        print(json.dumps(inspect_database(Path(sys.argv[2])), ensure_ascii=False))
        return 0
    if len(sys.argv) != 1:
        raise ValueError('Run without arguments for the teaching experiment')
    run = EVIDENCE / ('storage-' + datetime.now(TZ).strftime('%Y%m%dT%H%M%S%f'))
    run.mkdir(parents=True, exist_ok=False)
    db_path, backup_path = run / 'experiment.sqlite3', run / 'backup.sqlite3'
    input_hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in (DAILY, SPOT)}
    daily, spot = (json.loads(p.read_text('utf-8-sig')) for p in (DAILY, SPOT))
    print('第1步：读取上一课的真实记录；本次不重新请求行情，也不改变原始文件。')
    conn = sqlite3.connect(db_path, timeout=2)
    conn.executescript('''
        CREATE TABLE watchlist (symbol TEXT PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE daily_bars (
            symbol TEXT NOT NULL, trade_date TEXT NOT NULL, adjustment TEXT NOT NULL,
            source TEXT NOT NULL, close_yuan TEXT NOT NULL, volume_shares INTEGER NOT NULL,
            PRIMARY KEY(symbol, trade_date, adjustment, source));
        CREATE TABLE quotes (
            symbol TEXT PRIMARY KEY, source TEXT NOT NULL, source_time TEXT NOT NULL,
            received_at TEXT NOT NULL, price_yuan TEXT NOT NULL);
        CREATE TABLE notification_outbox (
            event_key TEXT PRIMARY KEY, status TEXT NOT NULL CHECK(status IN ('pending','unknown','confirmed','failed')),
            note TEXT NOT NULL);
    ''')
    journal_mode = conn.execute('PRAGMA journal_mode').fetchone()[0]
    for repetition in range(2):
        with conn:
            conn.execute('INSERT INTO watchlist VALUES (?, ?) ON CONFLICT(symbol) DO NOTHING', ('sh600519', '贵州茅台'))
            for row in daily['rows']:
                conn.execute('INSERT INTO daily_bars VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING',
                             ('sh' + row['股票代码'], row['日期'], 'unadjusted', daily['source'],
                              str(Decimal(str(row['收盘']))), int(row['成交量'])))
            quote = spot['samples'][repetition]['quote']
            conn.execute('''INSERT INTO quotes VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET source=excluded.source, source_time=excluded.source_time,
                    received_at=excluded.received_at, price_yuan=excluded.price_yuan''',
                         (quote['market_symbol'], spot['source'], quote['source_reported_at'],
                          quote['received_at'], str(Decimal(str(quote['price'])))))
            conn.execute('INSERT INTO notification_outbox VALUES (?, ?, ?) ON CONFLICT(event_key) DO NOTHING',
                         ('experiment-only-event', 'pending', '仅验证数据库记录去重；未触发异动、未发消息'))
    print('第2步：同一份自选、6条日线和实验待发送记录各写两次，检查唯一约束是否阻止重复。')
    try:
        with conn:
            conn.execute('INSERT INTO watchlist VALUES (?, ?)', ('rollback-only', '事务回滚实验数据'))
            raise RuntimeError('controlled interruption before transaction commit')
    except RuntimeError:
        pass
    rolled_back = conn.execute('SELECT count(*) FROM watchlist WHERE symbol=?', ('rollback-only',)).fetchone()[0] == 0
    print('第3步：在一笔事务提交前主动中断，检查未完成的写入是否回滚。')
    with conn:
        conn.execute('UPDATE notification_outbox SET status=? WHERE event_key=?', ('unknown', 'experiment-only-event'))
    conn.close()
    child = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(Path(__file__).resolve()), '--inspect', str(db_path)],
                           capture_output=True, text=True, encoding='utf-8', check=True, timeout=15)
    persisted = json.loads(child.stdout)
    print('第4步：关闭数据库连接，启动另一个Python进程读取；这是持久化检查，还不是电脑和手机联调。')
    source_conn, backup_conn = sqlite3.connect(db_path), sqlite3.connect(backup_path)
    try:
        source_conn.backup(backup_conn)
    finally:
        backup_conn.close()
        source_conn.close()
    backup_state = inspect_database(backup_path)
    expected = spot['samples'][1]['quote']
    checks = {
        'duplicate_watchlist_kept_once': persisted['watchlist_count'] == 1,
        'duplicate_daily_kept_six': persisted['daily_count'] == 6,
        'duplicate_local_outbox_kept_once': persisted['outbox_count'] == 1,
        'source_time_preserved': persisted['source_time'] == expected['source_reported_at'],
        'receive_time_separate': persisted['received_at'] == expected['received_at'] and persisted['received_at'] != persisted['source_time'],
        'transaction_rollback': rolled_back,
        'uncertain_delivery_stays_unknown_after_reopen': persisted['outbox_status'] == 'unknown',
        'backup_reopens_with_same_state': backup_state == persisted,
        'database_integrity': persisted['integrity_check'] == 'ok',
        'input_files_unchanged': all(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == h for name, h in input_hashes.items()),
    }
    result = {
        'checked_at': datetime.now(TZ).isoformat(), 'python_version': sys.version.split()[0],
        'sqlite_version': sqlite3.sqlite_version, 'journal_mode': journal_mode,
        'database': str(db_path), 'backup': str(backup_path), 'input_sha256': input_hashes,
        'persisted_state_in_new_process': persisted, 'checks': checks,
        'status': 'passed' if all(checks.values()) else 'failed',
        'scope': '本地SQLite持久化、唯一约束、事务回滚和备份实验；通知状态为人为设置的实验状态。',
        'not_verified': ['真实通知送达与平台去重', '电脑和手机跨设备API流程', '并发写入压力与进程崩溃恢复', '正式业务规则和盘中实时性'],
    }
    (run / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf-8')
    print('结果：' + result['status'])
    print(json.dumps(persisted, ensure_ascii=False, indent=2))
    print('记录：' + str(run / 'result.json'))
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
