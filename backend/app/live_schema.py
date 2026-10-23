"""Additive monitoring state, independent of the stable quote/event payloads."""


def initialize_live_schema(store):
    with store.connect() as db, db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS quote_live_evidence (
                market TEXT NOT NULL,
                symbol TEXT NOT NULL,
                watchlist_created_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (market, symbol),
                FOREIGN KEY (market, symbol) REFERENCES watchlist(market, symbol) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS quote_collector_runtime (
                id INTEGER PRIMARY KEY CHECK (id=1),
                payload TEXT NOT NULL DEFAULT '{}'
            );
            INSERT OR IGNORE INTO quote_collector_runtime (id) VALUES (1);
        """)
