"""Optional-rule arithmetic and the real event/outbox lifecycle; all feeds synthetic."""
from datetime import datetime, timedelta
import json
from types import SimpleNamespace

import httpx
import pytest

from backend.app import notification_service as notification_module
from backend.app.daily_service import DailyService
from backend.app.extended_anomalies import ExtendedAnomalies
from backend.app.extended_rules import DEFAULT_CONFIG, evaluate_rules, metrics, history_evidence, validate_extended_event
from backend.app.quotes import parse_quote
from backend.app.securities import LookupFailure
from backend.app.store import WatchlistStore
from backend.tests.test_live_acceptance import Clock, RawFeed, FakeFeishuWire, CONFIG, security, reopen, raw_quote


def optional_raw(clock, *, price='103.00', volume='1000', high='110', low='90', ratio='4', upper='110', lower='90'):
    fields=raw_quote('600519',clock(),price=price,volume=volume).split('"')[1].split('~')
    fields+=['']*(50-len(fields))
    for index,value in ((33,high),(34,low),(47,upper),(48,lower),(49,ratio)):
        fields[index]=value
    return 'v_sh600519="'+'~'.join(fields)+'";'


def quote(clock, **kwargs):
    return parse_quote(optional_raw(clock,**kwargs),'600519',clock())


def config(**changes):
    return {**DEFAULT_CONFIG,**changes}


@pytest.fixture
def rig(tmp_path,monkeypatch):
    clock=Clock()
    monkeypatch.delenv('CREDENTIALS_DIRECTORY',raising=False)
    monkeypatch.setattr(notification_module,'load_config',lambda _:CONFIG)
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',lambda *_:pytest.fail('Unexpected external HTTP'))
    store=WatchlistStore(tmp_path/'extended.sqlite3',clock)
    store.initialize();store.add(security(clock))
    result=reopen(store,clock,RawFeed(clock),FakeFeishuWire())
    DailyService(store,clock=clock).initialize()
    result.extended=ExtendedAnomalies(store,result.quotes,result.notifications,clock=clock)
    result.extended.initialize();result.anomalies.extended=result.extended
    result.notifications.set_enabled(True)
    return result


def save(rig, **changes):
    old=rig.extended.rule('sh','600519')
    return rig.extended.save('sh','600519',old['watchlist_created_at'],old['version'],True,config(**changes))


def refresh(rig, **kwargs):
    rig.feed.raw['600519']=optional_raw(rig.clock,**kwargs)
    return rig.quotes.refresh('sh','600519')


def qualify(rig, **kwargs):
    refresh(rig,volume='1000',**kwargs)
    rig.clock.advance()
    return refresh(rig,volume='1100',**kwargs)


def results(rig):
    return rig.extended.evaluate(rig.extended.rule('sh','600519'))


@pytest.mark.parametrize(('price','kind'), [('110','limit_up'),('90','limit_down')])
def test_limits_use_source_prices_and_touch_not_fixed_board_percent(price,kind):
    clock=Clock()
    values=evaluate_rules(quote(clock,price=price),config(limit_enabled=True),clock())
    assert next(row for row in values if row['rule_type']==kind)['status']=='triggered'
    invalid=evaluate_rules(quote(clock,price=price,upper=''),config(limit_enabled=True),clock())
    assert all(row['status']=='blocked' for row in invalid)


@pytest.mark.parametrize(('ratio','status'), [('0','below'),('3','below'),('3.01','triggered'),('','blocked'),('-1','blocked'),('NaN','blocked')])
def test_volume_missing_and_exact_boundary_are_distinct(ratio,status):
    clock=Clock()
    assert evaluate_rules(quote(clock,ratio=ratio),config(volume_enabled=True),clock())[0]['status']==status


def test_amplitude_uses_range_over_previous_close_and_rejects_inconsistent_range():
    clock=Clock()
    value=evaluate_rules(quote(clock,high='108',low='100'),config(amplitude_enabled=True),clock())[0]
    assert value['metric_value']=='8.00' and value['status']=='below'
    value=evaluate_rules(quote(clock,high='108.01',low='100'),config(amplitude_enabled=True),clock())[0]
    assert value['status']=='triggered'
    assert evaluate_rules(quote(clock,high='102',low='101'),config(amplitude_enabled=True),clock())[0]['status']=='blocked'


def daily():
    return {'market':'sh','symbol':'600519','adjustment':'none','price_unit':'元',
            'source':'合成日线','source_url':'https://example.invalid/daily','received_at':'2026-10-21T16:00:00+08:00',
            'bars':[{'date':day,'close':str(98+index)} for index,day in enumerate(['2026-10-15','2026-10-16','2026-10-19','2026-10-20','2026-10-21'])]}


@pytest.mark.parametrize('fault',['gap','prior_close_not_finished','future','adjusted','old_last_date'])
def test_history_must_cover_finished_contiguous_sessions(fault):
    clock=Clock();history=daily()
    if fault=='gap':history['bars'][0]['date']='2026-10-14'
    elif fault=='prior_close_not_finished':history['received_at']='2026-10-21T14:59:00+08:00'
    elif fault=='future':history['received_at']='2026-10-23T16:00:00+08:00'
    elif fault=='adjusted':history['adjustment']='qfq'
    else:history['bars'].pop()
    rows=evaluate_rules(quote(clock),config(breakout_enabled=True,lookback_days=5),clock(),daily=history)
    assert len(rows)==2 and all(row['status']=='blocked' for row in rows)


def test_complete_history_fixes_its_original_window():
    clock=Clock()
    rows=evaluate_rules(quote(clock),config(breakout_enabled=True,lookback_days=5),clock(),daily=daily())
    assert [row['status'] for row in rows]==['triggered','below']
    assert rows[0]['rule_evidence']['history']['high_close']=='102'


@pytest.mark.parametrize('fault',[None,'old','future','received_before_published','no_url','null_url'])
def test_news_rule_uses_publication_and_receipt_after_enable(fault):
    clock=Clock()
    item={'id':'N1','title':'合成公告','summary':'仅用于验证时间边界','url':'https://example.invalid/news',
          'published_at':'2026-10-22T09:55:00+08:00','received_at':'2026-10-22T09:56:00+08:00','source':'合成资讯'}
    if fault=='old':item['published_at']='2026-10-22T09:49:00+08:00'
    elif fault=='future':item['published_at']='2026-10-22T10:01:00+08:00'
    elif fault=='received_before_published':item['received_at']='2026-10-22T09:54:00+08:00'
    elif fault=='no_url':item['url']=''
    elif fault=='null_url':item['url']=None
    research={'snapshot_id':'S1','sections':[{'key':'announcements','status':'available','items':[item]}]}
    row=evaluate_rules(quote(clock),config(event_enabled=True),clock(),research=research,enabled_at='2026-10-22T09:50:00+08:00')[0]
    assert row['status']==('triggered' if fault is None else 'below')


def test_news_latest_selection_compares_instants_in_mixed_timezones():
    clock=Clock('2026-10-22T14:00:00+08:00')
    rows=[{'id':key,'title':'合成公告','summary':'','url':'https://example.invalid/'+key,
           'published_at':at,'received_at':'2026-10-22T14:00:00+08:00','source':'合成'}
          for key,at in [('older','2026-10-22T13:30:00+08:00'),('newer','2026-10-22T05:50:00+00:00')]]
    research={'snapshot_id':'S1','sections':[{'key':'news','status':'available','items':rows}]}
    row=evaluate_rules(quote(clock),config(event_enabled=True),clock(),research=research,enabled_at='2026-10-22T13:00:00+08:00')[0]
    assert row['rule_evidence']['news']['id']=='newer'


def test_live_rule_requires_qualified_pair_and_queues_exactly_once_across_restart(rig):
    rule=save(rig,volume_enabled=True)
    refresh(rig)
    assert results(rig)[0]['status']=='blocked'
    rig.clock.advance();refresh(rig,volume='1100')
    result=results(rig)[0]
    assert result['status']=='triggered'
    event_id=result['event_id']
    with rig.store.connect() as db:
        event=dict(db.execute('SELECT * FROM anomaly_events WHERE id=?',(event_id,)).fetchone())
        assert validate_extended_event(event)[1]['metric_value']=='4'
        assert db.execute('SELECT count(*) FROM notification_messages WHERE event_id=?',(event_id,)).fetchone()[0]==1
    rig.extended=ExtendedAnomalies(rig.store,rig.quotes,rig.notifications,clock=rig.clock)
    rig.extended.initialize()
    assert results(rig)[0]['status']=='duplicate'
    rig.notifications.process_pending_events();rig.notifications.process_pending_events()
    assert len(rig.wire.messages)==1
    card=json.loads(rig.wire.messages[0]['content'])
    assert '量比提醒' in json.dumps(card,ensure_ascii=False)
    assert '下跌达到 3%' not in json.dumps(card,ensure_ascii=False)


@pytest.mark.parametrize('action',['disable','replace','remove','stale_quote'])
def test_pending_rule_is_rechecked_before_external_send(rig,action):
    save(rig,volume_enabled=True);qualify(rig);event=results(rig)[0]
    if action=='disable':
        old=rig.extended.rule('sh','600519')
        rig.extended.save('sh','600519',old['watchlist_created_at'],old['version'],False,old['config'])
    elif action=='replace':save(rig,volume_enabled=True,volume_ratio='5')
    elif action=='remove':rig.store.remove('sh','600519')
    else:rig.clock.advance(120)
    rig.notifications.process_pending_events()
    assert rig.wire.messages==[]
    with rig.store.connect() as db:
        status=db.execute('SELECT status FROM notification_messages WHERE event_id=?',(event['event_id'],)).fetchone()[0]
        assert status in ('cancelled','expired')


def test_below_then_trigger_records_observation_and_rule_change_cas(rig):
    old=save(rig,volume_enabled=True)
    qualify(rig,ratio='2');assert results(rig)[0]['status']=='below'
    rig.clock.advance();refresh(rig,ratio='4',volume='1200')
    event=results(rig)[0]
    with rig.store.connect() as db:
        snapshot=json.loads(db.execute('SELECT snapshot FROM anomaly_events WHERE id=?',(event['event_id'],)).fetchone()[0])
        assert snapshot['first_observation'] is False
    save(rig,volume_enabled=True,volume_ratio='6')
    assert rig.extended.evaluate(old)[0]['status']=='cancelled'
    with pytest.raises(LookupFailure):
        rig.extended.save('sh','600519',old['watchlist_created_at'],old['version'],True,old['config'])


def test_undo_preserves_thresholds_but_does_not_reenable_rules_or_resend(rig):
    old=save(rig,volume_enabled=True,volume_ratio='4.5')
    removed=rig.store.remove_with_undo('sh','600519',old['watchlist_created_at'])
    rig.clock.advance(1)
    restored=rig.store.undo_remove(removed['undo_token'])
    rule=rig.extended.rule('sh','600519')
    assert restored['rule_restored_disabled'] is True
    assert rule['enabled'] is False and rule['config']['volume_ratio']=='4.5'
    assert rule['watchlist_created_at']!=old['watchlist_created_at']
    assert rig.extended.evaluate(old)[0]['status']=='cancelled'
    assert rig.wire.messages==[]
