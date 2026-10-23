"""Start the candidate only against a DB copy, with all external HTTP blocked."""
import json
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from fastapi.testclient import TestClient
import httpx
from backend.app.main import create_app


def rows(path):
    with sqlite3.connect(f'file:{path.as_posix()}?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
        return {r[0]:[dict(x) for x in db.execute(f'SELECT * FROM "{r[0]}" ORDER BY rowid')]
                for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def main():
    database=Path(sys.argv[1]).resolve()
    if database==Path('/var/lib/alphaproject-lab/watchlist.sqlite3'):
        raise RuntimeError('Preflight refuses the live database')
    before=rows(database)
    app=create_app(database,allowed_hosts=['testserver'],run_monitor=False)
    with patch.object(httpx.HTTPTransport,'handle_request',side_effect=RuntimeError('External HTTP disabled in preflight')):
        with TestClient(app) as client:
            assert app.state.notifications.config and not app.state.notifications.config_error
            assert app.state.explanations.config and not app.state.explanations.config_error
            assert client.get('/api/watchlist').status_code==200
            for row in before.get('anomaly_events',[]):
                response=client.get('/api/events/'+row['id'])
                assert response.status_code==200
            for row in before.get('ai_quote_snapshots',[]):
                assert client.get('/api/quote-snapshots/'+row['id']+'/explanations').status_code==200
    after=rows(database)
    preserved=[]
    for table,old in before.items():
        if table in ('anomaly_runtime','briefing_runtime'):
            continue
        if table=='ai_subject_evidence':
            # Reading a previously unrequested subject may bind its fixed evidence.
            # Existing bindings must remain byte-for-byte unchanged; new ones are additive.
            assert len(after[table])>=len(old),table
            assert all(any(all(new.get(k)==v for k,v in previous.items()) for new in after[table]) for previous in old),table
            preserved.append(table)
            continue
        assert len(old)==len(after[table]),table
        assert all(all(new.get(k)==v for k,v in previous.items()) for previous,new in zip(old,after[table])),table
        preserved.append(table)
    print(json.dumps({'status':'passed','external_http_calls':0,'tables_preserved':preserved,
                      'row_counts':{name:len(value) for name,value in before.items()},
                      'public_url':app.state.notifications.detail_base_url,'actual_credentials_loaded':True}))


if __name__=='__main__':
    main()
