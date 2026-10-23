"""Save an explicit quote version without refreshing data or calling a model."""
import hashlib
import json
import uuid

from .explanation_evidence import canonical
from .quote_evidence import freeze_quote, validate_quote
from .securities import LookupFailure, market_for


class QuoteSnapshots:
    def __init__(self, store, clock, detail_prefix=''):
        self.store, self.clock = store, clock
        self.detail_prefix = detail_prefix

    @staticmethod
    def initialize(db):
        db.execute('''CREATE TABLE IF NOT EXISTS ai_quote_snapshots (
            id TEXT PRIMARY KEY, market TEXT NOT NULL, symbol TEXT NOT NULL,
            watchlist_created_at TEXT NOT NULL, payload TEXT NOT NULL,
            fingerprint TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
        )''')

    def _current(self, db, market, symbol):
        if market_for(symbol) != market:
            raise LookupFailure('invalid_symbol', '市场与股票代码不一致。', 400)
        item = db.execute('SELECT * FROM watchlist WHERE market=? AND symbol=?', (market, symbol)).fetchone()
        if not item:
            raise LookupFailure('not_in_watchlist', '这只股票已不在自选中；旧依据仍可通过保存的链接查看。', 404)
        row = db.execute('SELECT * FROM quote_cache WHERE market=? AND symbol=?', (market, symbol)).fetchone()
        if not row or row['watchlist_created_at'] != item['created_at'] or not row['payload']:
            raise LookupFailure('quote_missing', '尚无可用报价，请先在自选卡片获取报价，再回来固定依据。', 409)
        try:
            payload = json.loads(row['payload'])
            validate_quote(payload, market, symbol, self.clock().isoformat())
        except (ValueError, TypeError):
            raise LookupFailure('invalid_quote_evidence', '保存的报价无法读取，请重新获取并核对。', 422) from None
        payload.pop('raw_text', None)
        digest = hashlib.sha256(canonical({'generation': item['created_at'], 'quote': payload}).encode()).hexdigest()
        return dict(item), payload, digest

    @staticmethod
    def public(row, detail_prefix=''):
        return {'id': row['id'], 'created_at': row['created_at'], 'market': row['market'], 'symbol': row['symbol'],
                'quote': json.loads(row['payload']), 'detail_path': detail_prefix + '/#quote=' + row['id']}

    def state(self, market, symbol):
        with self.store.connect() as db:
            rows = db.execute('SELECT * FROM ai_quote_snapshots WHERE market=? AND symbol=? ORDER BY created_at DESC,id DESC LIMIT 20', (market, symbol)).fetchall()
            try:
                item, quote, digest = self._current(db, market, symbol)
                current = {'quote': quote, 'quote_hash': digest, 'watchlist_created_at': item['created_at']}
                error = None
            except LookupFailure as failure:
                if failure.code == 'invalid_symbol':
                    raise
                current, error = None, failure.message
        return {'current': current, 'error_message': error, 'snapshots': [self.public(row, self.detail_prefix) for row in rows]}

    def save(self, market, symbol, generation, expected_hash):
        with self.store.connect() as db, db:
            db.execute('BEGIN IMMEDIATE')
            item, quote, digest = self._current(db, market, symbol)
            if item['created_at'] != generation or digest != expected_hash:
                raise LookupFailure('quote_changed', '报价或自选已变化，请重新读取并核对，再固定这份报价。', 409)
            row = db.execute('SELECT * FROM ai_quote_snapshots WHERE fingerprint=?', (digest,)).fetchone()
            if row:
                return self.public(row, self.detail_prefix), False
            snapshot_id = str(uuid.uuid4())
            db.execute('INSERT INTO ai_quote_snapshots VALUES (?,?,?,?,?,?,?)',
                       (snapshot_id, market, symbol, generation, canonical(quote), digest, self.clock().isoformat()))
            row = db.execute('SELECT * FROM ai_quote_snapshots WHERE id=?', (snapshot_id,)).fetchone()
            freeze_quote(row)
            return self.public(row, self.detail_prefix), True

    @staticmethod
    def evidence(db, snapshot_id):
        row = db.execute('SELECT * FROM ai_quote_snapshots WHERE id=?', (snapshot_id,)).fetchone()
        if not row:
            raise LookupFailure('snapshot_not_found', '这份报价依据不存在，请核对链接或重新选择已保存依据。', 404)
        return freeze_quote(row)
