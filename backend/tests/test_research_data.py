from datetime import datetime

import pytest

from app.integrations.research_data import ResearchData, SHANGHAI, number


NOW = datetime(2026, 10, 14, 9, 15, tzinfo=SHANGHAI)


def test_quote_pages_and_units_and_date():
    calls = []
    def get(url, params):
        calls.append(params['pn'])
        n = params['pn']
        return {'rc': 0, 'data': {'total': 2, 'diff': [{
            'f12': str(n), 'f14': f'行业{n}', 'f2': 123.45, 'f3': -1.25,
            'f124': NOW.timestamp()}]}}
    source = ResearchData(get_json=get, now=lambda: NOW)
    result = source._cached("test-quotes", lambda: source._rows("test"))
    assert calls == [1, 2]
    assert result[0]['change_pct'] == -1.25  # fltt=2 is already percent
    assert result[0]['as_of'].startswith('2026-10-14')
    source._cached("test-quotes", lambda: source._rows("test"))
    assert calls == [1, 2]


def test_quote_repeated_page_rejected():
    source = ResearchData(get_json=lambda *_: {'rc': 0, 'data': {'total': 2, 'diff': [
        {'f12': 'same', 'f14': '银行', 'f3': 1, 'f124': NOW.timestamp()}]}}, now=lambda: NOW)
    with pytest.raises(ValueError, match='Repeated'):
        source._cached("test-quotes", lambda: source._rows("test"))


def test_invalid_or_old_quotes_are_not_fabricated():
    source = ResearchData(get_json=lambda *_: {'rc': 0, 'data': {'total': 1, 'diff': [
        {'f12': 'a', 'f14': '行业', 'f3': '-', 'f124': 1}]}}, now=lambda: NOW)
    with pytest.raises(ValueError, match='recent'):
        source._cached("test-quotes", lambda: source._rows("test"))
    assert number('nan') is None
    assert number('inf') is None
    assert number(0) == 0


def test_economic_calendar_shanghai_bounds_and_unknown_values():
    def get(url, params):
        assert params['start'] == int(NOW.replace(hour=0, minute=0).timestamp())
        return {'code': 20000, 'data': {'items': [
            {'public_date': NOW.timestamp(), 'title': 'CPI', 'country': '中国', 'actual': '', 'forecast': '0'},
            {'public_date': params['end'], 'title': 'tomorrow'},
        ]}}
    rows = ResearchData(get_json=get, now=lambda: NOW).economic_calendar()
    assert len(rows) == 1
    assert rows[0]['actual'] is None
    assert rows[0]['forecast'] == 0


def test_earnings_rescheduled_and_report_period():
    def get(url, params):
        assert params['filter'] == "(APPOINT_PUBLISH_DATE='2026-10-14')"
        return {'success': True, 'result': {'pages': 1, 'data': [
            {'SECURITY_CODE': '000001', 'REPORT_DATE': '2026-09-30', 'FIRST_APPOINT_DATE': '2026-10-14', 'APPOINT_PUBLISH_DATE': '2026-10-15'},
            {'SECURITY_CODE': '000002', 'REPORT_DATE': '2026-09-30', 'APPOINT_PUBLISH_DATE': '2026-10-14'},
        ]}}
    rows = ResearchData(get_json=get, now=lambda: NOW).earnings_calendar()
    assert [r['code'] for r in rows] == ['000002']
    assert '2026-09-30' in rows[0]['event']


def test_calendar_failure_is_not_empty_success():
    source = ResearchData(get_json=lambda *_: {'success': False}, now=lambda: NOW)
    with pytest.raises(ValueError):
        source.earnings_calendar()


def test_industry_survives_market_outage():
    def get(url, params):
        if 'datacenter' in url:
            return {'result': {'data': [{'BOARD_NAME_2LEVEL': '银行Ⅱ'}]}}
        return {'rc': 1}
    info = ResearchData(get_json=get, now=lambda: NOW).sector_info('000001')
    assert info['industry'] == '银行Ⅱ'
    assert info['sector_change_pct'] is None


def test_global_quotes_timezone_and_percent():
    lines = []
    for symbol in ['usDJI', 'usIXIC', 'usINX', 'hkHSI']:
        fields = [''] * 33
        fields[1], fields[3], fields[32] = symbol, '1234', '-0.14'
        fields[30] = '2026-10-13 16:00:00' if symbol.startswith('us') else '2026/10/14 09:10:00'
        lines.append(f'v_{symbol}="' + '~'.join(fields) + '";')
    source = ResearchData(get_text=lambda _: '\n'.join(lines), now=lambda: NOW)
    rows = source.global_indices()
    assert len(rows) == 4
    assert rows[0]['change_pct'] == -0.14
    assert rows[0]['as_of'] == '2026-10-14T04:00:00+08:00'


def cls_feed(url, params):
    if url.endswith("plate_list"):
        page = params['page']
        assert params['type'] == 'industry'
        start, end = (0, 6) if page == 1 else (0, 12)
        return {'code': 200, 'data': {'is_all': int(page == 2), 'plate_data': [
            {'secu_code': f'cls{i}', 'secu_name': f'行业{i}', 'change': (i - 6) / 100}
            for i in range(start, end)]}}
    if url.endswith('/stock/detail'):
        return {'code': 200, 'data': {'plate': [
            {'secu_code': 'concept', 'secu_name': '热门概念'},
            {'secu_code': 'cls6', 'secu_name': '行业6'}]}}
    if url.endswith('/kline'):
        code = params['secu_code']
        return {'code': 200, 'data': [{'secu_code': code, 'date': 20261013,
            'close_px': 100, 'change': (int(code[3:]) - 6) / 100}]}
    raise AssertionError(url)


def test_cls_ranking_complete_pages_units_date_and_cache():
    calls = []
    def get(url, params):
        calls.append(url)
        return cls_feed(url, params)
    source = ResearchData(get_json=get, now=lambda: NOW)
    rows = source.sector_ranking()
    assert len(rows) == 10
    assert rows[0]['change_pct'] == 5
    assert rows[-1]['change_pct'] == -6
    assert {r['as_of'] for r in rows} == {'2026-10-13'}
    assert {r['source'] for r in rows} == {'财联社（日线）'}
    assert len(calls) == 12  # two universe pages + ten bounded bar requests
    assert source.sector_ranking() == rows
    assert len(calls) == 12


def test_cls_industry_excludes_concepts_and_preserves_zero():
    info = ResearchData(get_json=cls_feed, now=lambda: NOW).sector_info('002119')
    assert info['industry'] == '行业6'
    assert info['sector_change_pct'] == 0
    assert info['sector_as_of'] == '2026-10-13'


@pytest.mark.parametrize('change,date,code', [
    ('nan', 20261013, 'cls0'), (0.01, 20260901, 'cls0'),
    (0.01, 20261015, 'cls0'), (0.01, 20261013, 'wrong'),
])
def test_cls_invalid_bar_rejected(change, date, code):
    def get(url, params):
        return {'code': 200, 'data': [{'secu_code': code, 'date': date, 'change': change, 'close_px': 100}]}
    with pytest.raises(ValueError):
        ResearchData(get_json=get, now=lambda: NOW)._sector_bar({'code': 'cls0', 'name': '行业'})


def test_cls_repeated_universe_rejected():
    def get(url, params):
        return {'code': 200, 'data': {'is_all': 0, 'plate_data': [
            {'secu_code': 'same', 'secu_name': '行业', 'change': 0}]}}
    with pytest.raises(ValueError, match='Repeated'):
        ResearchData(get_json=get, now=lambda: NOW).sectors()


def test_cls_bar_failure_preserves_verified_industry():
    def get(url, params):
        if url.endswith('/kline'):
            return {'code': 500}
        return cls_feed(url, params)
    info = ResearchData(get_json=get, now=lambda: NOW).sector_info('002119')
    assert info['industry'] == '行业6'
    assert info['sector_change_pct'] is None
    assert info['sector_as_of'] is None


def test_cls_outage_uses_dated_eastmoney_fallback():
    def get(url, params):
        if 'cls.cn' in url:
            return {'code': 500}
        return {'rc': 0, 'data': {'total': 1, 'diff': [
            {'f12': 'bk1', 'f14': '银行', 'f3': 1.2, 'f124': NOW.timestamp()}]}}
    rows = ResearchData(get_json=get, now=lambda: NOW).sector_ranking()
    assert rows[0]['source'] == '东方财富'
    assert rows[0]['change_pct'] == 1.2
