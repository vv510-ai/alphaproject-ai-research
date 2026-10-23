"""One natural briefing, existing scheduler/receipts, and zero AI fees."""
from datetime import timedelta
import json

from backend.app.briefing_content import message_body
from backend.app.research_service import ResearchService
from backend.tests.test_briefings import rig, add, enable, queue, state
from backend.tests.test_unified_explanations import Provider


def attach(rig):
    app, _, clock, *_ = rig
    provider = Provider(clock)
    research = ResearchService(app.state.store, provider, clock)
    research.initialize()
    app.state.briefings.research = research
    return research, provider


def test_global_research_daily_and_new_information_form_one_frozen_briefing(rig):
    research, provider = attach(rig)
    add(rig)
    queue(rig)
    assert provider.calls == []
    rig[0].state.briefings.tick()
    report = state(rig)['reports'][0]
    assert report['status'] == 'succeeded' and report['delivery_status'] == 'local_only'
    output = report['output']
    assert len(provider.calls) == 6
    assert output['research_coverage']['missing_sections'] == []
    assert output['research_coverage']['new_count'] == 6
    assert len(output['research_context']['sections']) == 6
    assert all(section['items'] for section in output['research_context']['sections'])
    assert '120' in output['narrative'] and '新增 6 条' in output['narrative']
    assert output['model_cost'] == '0' and output['ai_status'] == 'not_used_by_choice'
    assert output['research_used_count'] == 0
    assert [item['stage'] for item in output['trace']] == ['research_frozen', 'daily_validated', 'model_call']
    assert output['trace'][-1]['input_tokens'] == output['trace'][-1]['output_tokens'] == 0
    assert output['research_cutoff'] == report['snapshot']['research_context']['cutoff']
    body = message_body(report['id'], output)
    assert '研究资料截止' in body and '相关报道不证明涨跌原因' in body
    with rig[0].state.store.connect() as db:
        assert db.execute('SELECT count(*) FROM ai_explanations').fetchone()[0] == 0
    assert rig[4].sent == []
    rig[2].advance(61)
    provider.summary = '公司收入为 12 亿元。'
    research.refresh()
    assert queue(rig).json()['record']['id'] == report['id']
    assert state(rig)['reports'][0]['output'] == output


def test_research_fetch_happens_outside_sqlite_write_transaction(rig):
    _, provider = attach(rig)
    add(rig)
    reached = []
    def assert_unlocked(key, section):
        with rig[0].state.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.rollback()
        reached.append(key)
    provider.mutate = assert_unlocked
    queue(rig)
    rig[0].state.briefings.process_next()
    assert len(reached) == 6 and state(rig)['reports'][0]['status'] == 'succeeded'


def test_research_failure_keeps_daily_facts_and_explicit_six_section_gaps(rig):
    research, _ = attach(rig)
    add(rig)
    def fail():
        raise RuntimeError('private-upstream-details-must-not-escape')
    research.refresh = fail
    queue(rig)
    rig[0].state.briefings.process_next()
    report = state(rig)['reports'][0]
    assert report['status'] == 'succeeded' and report['output']['coverage']['covered'] == 1
    assert len(report['output']['research_coverage']['missing_sections']) == 6
    assert report['output']['trace'][0]['status'] == 'partial'
    assert 'private-upstream-details' not in json.dumps(report)


def test_calendar_unknown_publication_is_visible_schedule_not_ai_evidence(rig):
    _, provider = attach(rig)
    add(rig)
    def change_calendar(key, section):
        if key == 'calendar':
            section['items'][0].update(published_at=None,
                scheduled_at=(rig[2]() + timedelta(hours=12)).isoformat(), summary='计划发布数据，实际值尚未确认。')
    provider.mutate = change_calendar
    queue(rig)
    rig[0].state.briefings.process_next()
    output = state(rig)['reports'][0]['output']
    calendar = next(section for section in output['research_context']['sections'] if section['key'] == 'calendar')
    assert len(calendar['items']) == 1
    assert calendar['items'][0]['published_at'] is None and calendar['items'][0]['ai_eligible'] is False
    assert all(item['section'] != 'calendar' for item in output['research_evidence'])
    assert '发布时间未知' in output['narrative']


def test_existing_schedule_keeps_one_report_one_receipt_and_no_paid_model(rig):
    _, provider = attach(rig)
    add(rig)
    enable(rig)
    rig[2].advance(10)
    for _ in range(3):
        rig[0].state.briefings.tick()
        rig[0].state.notifications.process_pending_events()
    report = state(rig)['reports'][0]
    assert len(state(rig)['reports']) == 1 and len(provider.calls) == 6 and len(rig[4].sent) == 1
    assert report['notification']['status'] == report['delivery_status'] == 'accepted'
    assert report['notification']['confirmed_at'] is None
    with rig[0].state.store.connect() as db:
        assert db.execute('SELECT count(*) FROM briefing_schedule_runs').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM ai_explanations').fetchone()[0] == 0


def test_cached_stock_announcements_are_supplemented_without_more_network(rig):
    research, provider = attach(rig)
    add(rig)
    research.refresh('sh', '600519')
    rig[2].advance(3)
    original = provider.fetch_section
    def market_data(key, market, symbol):
        result = original(key, market, symbol)
        if market is None and key == 'announcements':
            result.update(status='unavailable', items=[], error_message='全局公告源受控缺项')
        return result
    provider.fetch_section = market_data
    queue(rig)
    rig[0].state.briefings.process_next()
    output = state(rig)['reports'][0]['output']
    announcements = next(section for section in output['research_context']['sections'] if section['key'] == 'announcements')
    assert announcements['status'] == 'partial' and len(announcements['items']) == 1
    assert announcements['items'][0]['summary'] == provider.summary
    assert len(provider.calls) == 12 and output['research_context']['stock_snapshot_ids'][0]['snapshot_id']
