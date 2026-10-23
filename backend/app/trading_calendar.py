"""Finite, source-pinned regular SSE/SZSE calendar; unknown years fail closed."""

from datetime import date, timedelta

VERSION = 'sse-szse-2026-verified-20261019'
SOURCES = [
    {'name': '上海证券交易所：2026 年休市安排', 'url': 'https://www.sse.com.cn/disclosure/dealinstruc/closed/'},
    {'name': '深圳证券交易所：深证会〔2025〕481号', 'url': 'https://investor.szse.cn/disclosure/notice/general/t20251222_618087.html'},
]
HOLIDAYS = (
    ('2026-01-01', '2026-01-03', '元旦'),
    ('2026-02-15', '2026-02-23', '春节'),
    ('2026-04-04', '2026-04-06', '清明节'),
    ('2026-05-01', '2026-05-05', '劳动节'),
    ('2026-06-19', '2026-06-21', '端午节'),
    ('2026-09-25', '2026-09-27', '中秋节'),
    ('2026-10-01', '2026-10-07', '国庆节'),
)


class TradingCalendar:
    def day(self, value: date):
        if value.year != 2026:
            return 'unknown', '这个年份的交易日历尚未核验，暂停生成，不按星期数猜测。'
        for start, end, label in HOLIDAYS:
            if start <= value.isoformat() <= end:
                return 'closed', f'{label}休市'
        if value.weekday() >= 5:
            return 'closed', '周末休市；调休工作日也不作为交易日'
        return 'open', '按已核验年度公告，为常规交易日'

    def context(self, value: date):
        state, reason = self.day(value)
        previous = None
        if state == 'open':
            cursor = value - timedelta(days=1)
            for _ in range(30):
                prior, _ = self.day(cursor)
                if prior == 'unknown':
                    state, reason = 'unknown', '前一交易日落在未核验的年份，暂停生成。'
                    break
                if prior == 'open':
                    previous = cursor.isoformat()
                    break
                cursor -= timedelta(days=1)
            if not previous and state == 'open':
                state, reason = 'unknown', '无法确定前一交易日，暂停生成。'
        return {'date': value.isoformat(), 'state': state, 'reason': reason,
                'previous_trade_date': previous, 'version': VERSION,
                'verified_on': '2026-10-19', 'covered_years': [2026], 'sources': SOURCES,
                'note': '常规沪深 A 股日历；未实时订阅临时休市公告，不代表个股未停牌。'}
