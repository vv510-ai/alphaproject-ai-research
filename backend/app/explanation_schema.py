"""Keep the existing AI ledger and aliases while adding a second evidence subject."""
from .quote_snapshots import QuoteSnapshots

FIELDS = '''id TEXT PRIMARY KEY, event_id TEXT, cache_key TEXT NOT NULL,
    evidence_hash TEXT NOT NULL, prompt_version TEXT NOT NULL, requested_model TEXT NOT NULL,
    evidence_json TEXT NOT NULL, messages_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','running','succeeded','failed','rejected','interrupted')),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, retry_of TEXT,
    budget_day TEXT NOT NULL, budget_month TEXT NOT NULL,
    reserved_micro INTEGER NOT NULL, charged_micro INTEGER NOT NULL,
    cost_basis TEXT NOT NULL, price_profile TEXT NOT NULL,
    input_tokens INTEGER, output_tokens INTEGER, actual_model TEXT, model_fingerprint TEXT,
    elapsed_ms INTEGER, raw_output TEXT, output_json TEXT,
    error_code TEXT, error_message TEXT, reviewed_at TEXT,
    quote_snapshot_id TEXT,
    trace_json TEXT,
    CHECK((event_id IS NOT NULL) != (quote_snapshot_id IS NOT NULL)),
    FOREIGN KEY(event_id) REFERENCES anomaly_events(id),
    FOREIGN KEY(quote_snapshot_id) REFERENCES ai_quote_snapshots(id)'''


def initialize_schema(store):
    with store.connect() as db:
        # Must be outside a transaction; do not rename the old table, which would
        # rewrite the foreign-key target of the unchanged request-alias table.
        db.execute('PRAGMA foreign_keys=OFF')
        try:
            db.execute('BEGIN IMMEDIATE')
            QuoteSnapshots.initialize(db)
            columns = [row['name'] for row in db.execute('PRAGMA table_info(ai_explanations)')]
            if not columns:
                db.execute('CREATE TABLE ai_explanations (' + FIELDS + ')')
            elif 'quote_snapshot_id' not in columns:
                db.execute('CREATE TABLE new_ai_explanations (' + FIELDS + ')')
                names = ','.join('"' + name.replace('"', '""') + '"' for name in columns)
                db.execute(f'INSERT INTO new_ai_explanations ({names}) SELECT {names} FROM ai_explanations')
                db.execute('DROP TABLE ai_explanations')
                db.execute('ALTER TABLE new_ai_explanations RENAME TO ai_explanations')
            elif 'trace_json' not in columns:
                db.execute('ALTER TABLE ai_explanations ADD COLUMN trace_json TEXT')
            db.execute('''CREATE TABLE IF NOT EXISTS ai_subject_evidence (
                subject_key TEXT PRIMARY KEY, base_hash TEXT NOT NULL,
                evidence_json TEXT NOT NULL, frozen_at TEXT NOT NULL)''')
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ai_success ON ai_explanations(cache_key) WHERE status='succeeded'")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ai_active ON ai_explanations(cache_key) WHERE status IN ('pending','running')")
            db.execute('''CREATE TABLE IF NOT EXISTS ai_request_aliases (
                request_id TEXT PRIMARY KEY, event_id TEXT NOT NULL,
                evidence_hash TEXT NOT NULL, retry_of TEXT, record_id TEXT NOT NULL,
                FOREIGN KEY(record_id) REFERENCES ai_explanations(id))''')
            if db.execute('PRAGMA foreign_key_check').fetchone():
                raise RuntimeError('AI schema migration failed foreign-key validation; original data retained.')
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.execute('PRAGMA foreign_keys=ON')
