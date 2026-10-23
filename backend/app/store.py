"""One SQLite file, with short transactions and no network calls under a lock."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3
import uuid

from .securities import LookupFailure, Security


class WatchlistStore:
    def __init__(self, path: Path, clock=None):
        self.path = path.resolve()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=3)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection, connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError("数据库版本比本程序新，请使用对应版本，不会清空数据。")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS watchlist (
                    market TEXT NOT NULL CHECK (market IN ('sh', 'sz')),
                    symbol TEXT NOT NULL CHECK (length(symbol) = 6),
                    name TEXT NOT NULL,
                    source TEXT NOT NULL,
                    verified_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (market, symbol)
                )
            """)
            connection.execute("""CREATE TABLE IF NOT EXISTS watchlist_groups (
                id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
            )""")
            columns = {row['name'] for row in connection.execute('PRAGMA table_info(watchlist)')}
            for name, declaration in (
                ('group_id', 'TEXT REFERENCES watchlist_groups(id) ON DELETE SET NULL'),
                ('is_holding', 'INTEGER NOT NULL DEFAULT 0 CHECK(is_holding IN (0,1))'),
                ('display_order', 'INTEGER NOT NULL DEFAULT 0'),
                ('metadata_version', 'INTEGER NOT NULL DEFAULT 0'),
            ):
                if name not in columns:
                    connection.execute(f'ALTER TABLE watchlist ADD COLUMN {name} {declaration}')
            if 'display_order' not in columns:
                rows = connection.execute('SELECT market,symbol FROM watchlist ORDER BY created_at DESC,market,symbol').fetchall()
                connection.executemany('UPDATE watchlist SET display_order=? WHERE market=? AND symbol=?',
                                       [(index, row['market'], row['symbol']) for index, row in enumerate(rows)])
            connection.execute("""CREATE TABLE IF NOT EXISTS watchlist_undo (
                token TEXT PRIMARY KEY, item_json TEXT NOT NULL, rule_json TEXT,
                created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                restored_at TEXT, restored_generation TEXT
            )""")
            if 'extended_rule_json' not in {row['name'] for row in connection.execute('PRAGMA table_info(watchlist_undo)')}:
                connection.execute('ALTER TABLE watchlist_undo ADD COLUMN extended_rule_json TEXT')
            if version == 0:
                connection.execute("PRAGMA user_version = 1")

    def list_items(self) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM watchlist ORDER BY display_order, created_at DESC, market, symbol"
            ).fetchall()
        return [self.public_item(row) for row in rows]

    @staticmethod
    def public_item(row):
        result = dict(row)
        result['is_holding'] = bool(result.get('is_holding', False))
        return result

    def get(self, market: str, symbol: str) -> dict | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM watchlist WHERE market = ? AND symbol = ?", (market, symbol)
            ).fetchone()
        return self.public_item(row) if row else None

    def add(self, security: Security) -> tuple[dict, bool]:
        with self.connect() as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            order = connection.execute('SELECT COALESCE(MIN(display_order),1)-1 FROM watchlist').fetchone()[0]
            cursor = connection.execute("""
                INSERT INTO watchlist (market, symbol, name, source, verified_at, created_at, display_order)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (market, symbol) DO NOTHING
            """, (security.market, security.symbol, security.name, security.source,
                  security.verified_at, self.clock().isoformat(), order))
            created = cursor.rowcount == 1
            row = connection.execute(
                "SELECT * FROM watchlist WHERE market = ? AND symbol = ?",
                (security.market, security.symbol),
            ).fetchone()
        # Returning happens after the transaction has committed successfully.
        return self.public_item(row), created

    def remove(self, market: str, symbol: str) -> bool:
        with self.connect() as connection, connection:
            cursor = connection.execute(
                "DELETE FROM watchlist WHERE market = ? AND symbol = ?", (market, symbol)
            )
            removed = cursor.rowcount == 1
        return removed

    @staticmethod
    def _name(value):
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 20 or any(ord(c) < 32 for c in value):
            raise LookupFailure('invalid_group', '分组名称请填写 1 到 20 个字。', 422)
        return value.strip()

    def groups(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM watchlist_groups ORDER BY created_at,id')]

    def create_group(self, name):
        name = self._name(name)
        with self.connect() as db, db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM watchlist_groups WHERE name=?', (name,)).fetchone()
            if existing:
                return dict(existing)
            if db.execute('SELECT COUNT(*) FROM watchlist_groups').fetchone()[0] >= 50:
                raise LookupFailure('group_limit', '最多保存 50 个分组，请先整理已有分组。', 409)
            item = {'id': uuid.uuid4().hex, 'name': name, 'created_at': self.clock().isoformat()}
            db.execute('INSERT INTO watchlist_groups(id,name,created_at) VALUES(:id,:name,:created_at)', item)
            return item

    def rename_group(self, group_id, name):
        name = self._name(name)
        with self.connect() as db, db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM watchlist_groups WHERE id=?', (group_id,)).fetchone():
                raise LookupFailure('group_missing', '这个分组已不存在，请刷新。', 404)
            duplicate = db.execute('SELECT 1 FROM watchlist_groups WHERE name=? AND id!=?', (name,group_id)).fetchone()
            if duplicate:
                raise LookupFailure('group_exists', '已有同名分组，请换一个名称。', 409)
            db.execute('UPDATE watchlist_groups SET name=? WHERE id=?', (name,group_id))
            return dict(db.execute('SELECT * FROM watchlist_groups WHERE id=?', (group_id,)).fetchone())

    def delete_group(self, group_id):
        with self.connect() as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE watchlist SET group_id=NULL,metadata_version=metadata_version+1 WHERE group_id=?', (group_id,))
            return db.execute('DELETE FROM watchlist_groups WHERE id=?', (group_id,)).rowcount == 1

    def update_metadata(self, market, symbol, generation, expected_version, group_id, is_holding):
        with self.connect() as db, db:
            db.execute('BEGIN IMMEDIATE')
            item = db.execute('SELECT * FROM watchlist WHERE market=? AND symbol=?', (market,symbol)).fetchone()
            if not item or item['created_at'] != generation or item['metadata_version'] != expected_version:
                raise LookupFailure('watchlist_changed', '自选已被另一操作修改，请刷新后再保存。', 409)
            if group_id is not None and not db.execute('SELECT 1 FROM watchlist_groups WHERE id=?', (group_id,)).fetchone():
                raise LookupFailure('group_missing', '选择的分组已不存在，请刷新。', 409)
            db.execute('UPDATE watchlist SET group_id=?,is_holding=?,metadata_version=metadata_version+1 WHERE market=? AND symbol=?',
                       (group_id,int(is_holding),market,symbol))
            return self.public_item(db.execute('SELECT * FROM watchlist WHERE market=? AND symbol=?', (market,symbol)).fetchone())

    def reorder(self, items):
        identities = [(item['market'], item['symbol']) for item in items]
        if len(identities) != len(set(identities)):
            raise LookupFailure('invalid_order', '排序清单含重复股票，请刷新后重试。', 422)
        with self.connect() as db, db:
            db.execute('BEGIN IMMEDIATE')
            current = {(row['market'],row['symbol']): row for row in db.execute('SELECT * FROM watchlist')}
            if set(identities) != set(current) or any(
                current[key]['created_at'] != item['watchlist_created_at'] or current[key]['metadata_version'] != item['metadata_version']
                for key, item in zip(identities,items)
            ):
                raise LookupFailure('watchlist_changed', '自选清单已变化，请刷新后重新排序。', 409)
            for index, key in enumerate(identities):
                db.execute('UPDATE watchlist SET display_order=?,metadata_version=metadata_version+1 WHERE market=? AND symbol=?', (index,*key))
            return [self.public_item(row) for row in db.execute('SELECT * FROM watchlist ORDER BY display_order,market,symbol')]

    def remove_with_undo(self, market, symbol, generation=None):
        with self.connect() as db, db:
            db.execute('BEGIN IMMEDIATE')
            item = db.execute('SELECT * FROM watchlist WHERE market=? AND symbol=?', (market,symbol)).fetchone()
            if not item:
                return {'removed': False}
            if generation is not None and item['created_at'] != generation:
                raise LookupFailure('watchlist_changed', '这只股票已重新加入，请刷新后再决定是否移除。', 409)
            rule = None
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='anomaly_rules'").fetchone():
                rule = db.execute('SELECT * FROM anomaly_rules WHERE market=? AND symbol=?', (market,symbol)).fetchone()
            extended = None
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='extended_anomaly_rules'").fetchone():
                extended = db.execute('SELECT * FROM extended_anomaly_rules WHERE market=? AND symbol=?', (market,symbol)).fetchone()
            token, now = uuid.uuid4().hex, self.clock()
            expires = (now + timedelta(minutes=5)).isoformat()
            db.execute('INSERT INTO watchlist_undo(token,item_json,rule_json,created_at,expires_at,extended_rule_json) VALUES(?,?,?,?,?,?)',
                       (token,json.dumps(dict(item),ensure_ascii=False),json.dumps(dict(rule)) if rule else None,now.isoformat(),expires,json.dumps(dict(extended)) if extended else None))
            db.execute('DELETE FROM watchlist WHERE market=? AND symbol=?', (market,symbol))
            return {'removed': True, 'undo_token': token, 'undo_expires_at': expires}

    def undo_remove(self, token):
        with self.connect() as db, db:
            db.execute('BEGIN IMMEDIATE')
            undo = db.execute('SELECT * FROM watchlist_undo WHERE token=?', (token,)).fetchone()
            if not undo:
                raise LookupFailure('undo_missing', '这次移除记录不存在，请刷新清单。', 404)
            item = json.loads(undo['item_json'])
            current = db.execute('SELECT * FROM watchlist WHERE market=? AND symbol=?', (item['market'],item['symbol'])).fetchone()
            if undo['restored_at']:
                if current and current['created_at'] == undo['restored_generation']:
                    return {'item': self.public_item(current), 'restored': False, 'rule_restored_disabled': bool(undo['rule_json'] or undo['extended_rule_json'])}
                raise LookupFailure('undo_used', '这次撤销已经处理；当前清单又有变化，请刷新。', 409)
            now = self.clock()
            if now >= datetime.fromisoformat(undo['expires_at']):
                raise LookupFailure('undo_expired', '撤销已超过 5 分钟，需要时可重新添加股票。', 409)
            if current:
                raise LookupFailure('already_added', '这只股票已重新加入，不覆盖现在的设置。', 409)
            if item.get('group_id') and not db.execute('SELECT 1 FROM watchlist_groups WHERE id=?', (item['group_id'],)).fetchone():
                item['group_id'] = None
            item['created_at'], item['metadata_version'] = now.isoformat(), 0
            fields = ('market','symbol','name','source','verified_at','created_at','group_id','is_holding','display_order','metadata_version')
            db.execute(f"INSERT INTO watchlist({','.join(fields)}) VALUES({','.join('?' for _ in fields)})", tuple(item.get(key) for key in fields))
            if undo['rule_json']:
                rule = json.loads(undo['rule_json'])
                db.execute('INSERT INTO anomaly_rules(market,symbol,watchlist_created_at,up_threshold,down_threshold,enabled,version,updated_at) VALUES(?,?,?,?,?,0,1,?)',
                           (item['market'],item['symbol'],item['created_at'],rule['up_threshold'],rule['down_threshold'],now.isoformat()))
            if undo['extended_rule_json']:
                extended = json.loads(undo['extended_rule_json'])
                db.execute('INSERT INTO extended_anomaly_rules(market,symbol,watchlist_created_at,version,enabled,config_json,updated_at) VALUES(?,?,?,1,0,?,?)',
                           (item['market'],item['symbol'],item['created_at'],extended['config_json'],now.isoformat()))
            db.execute('UPDATE watchlist_undo SET restored_at=?,restored_generation=? WHERE token=?', (now.isoformat(),item['created_at'],token))
            return {'item': self.public_item(item), 'restored': True, 'rule_restored_disabled': bool(undo['rule_json'] or undo['extended_rule_json'])}
