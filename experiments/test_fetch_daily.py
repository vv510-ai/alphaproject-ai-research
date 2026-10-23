"""离线回归检查：这里的数字是测试样本，不是行情，不写入实验记录。"""

from contextlib import redirect_stdout
from datetime import date
import importlib.util
from io import StringIO
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import pandas as pd
import requests


spec = importlib.util.spec_from_file_location("daily_experiment", Path(__file__).with_name("01_fetch_daily.py"))
daily = importlib.util.module_from_spec(spec)
spec.loader.exec_module(daily)
START, END = date(2026, 10, 1), date(2026, 10, 15)


def eastmoney_row():
    return pd.DataFrame([{
        "日期": "2026-10-15", "股票代码": "600519",
        "开盘": 10, "收盘": 11, "最高": 12, "最低": 9, "成交量": 23,
    }])


def tencent_row():
    return pd.DataFrame([{
        "date": "2026-10-15", "open": 10, "close": 11,
        "high": 12, "low": 9, "volume": 2300,
    }])


class DailyExperimentTests(unittest.TestCase):
    def test_proxy_failure_falls_back_with_same_query_and_correct_source(self):
        with patch.object(daily, "fetch_raw", side_effect=[requests.exceptions.ProxyError(), tencent_row()]) as fetch:
            with redirect_stdout(StringIO()):
                result = daily.fetch_daily("600519", START, END)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["source"], "AKShare / 腾讯证券")
        self.assertEqual(result["rows"][0]["成交量"], 2300)
        self.assertEqual(result["volume_unit"], "股")
        self.assertNotIn("response_symbol_matches", result["checks"])
        self.assertEqual([item["status"] for item in result["attempts"]], ["failed", "ok"])
        self.assertEqual([call.args for call in fetch.call_args_list], [
            ("eastmoney", "600519", START, END), ("tencent", "600519", START, END),
        ])

    def test_successful_primary_does_not_request_backup_and_converts_hands(self):
        with patch.object(daily, "fetch_raw", return_value=eastmoney_row()) as fetch:
            with redirect_stdout(StringIO()):
                result = daily.fetch_daily("600519", START, END)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(result["source"], "AKShare / 东方财富")
        self.assertEqual(result["rows"][0]["成交量"], 2300)
        self.assertTrue(result["checks"]["response_symbol_matches"])

    def test_bad_data_is_rejected(self):
        scenarios = {
            "empty": pd.DataFrame(),
            "missing_column": eastmoney_row().drop(columns=["收盘"]),
            "wrong_symbol": eastmoney_row().assign(股票代码="000001"),
            "outside_dates": eastmoney_row().assign(日期="2026-10-16"),
            "duplicate_dates": pd.concat([eastmoney_row(), eastmoney_row()]),
            "missing_value": eastmoney_row().assign(收盘=None),
            "impossible_price": eastmoney_row().assign(收盘=99),
            "negative_volume": eastmoney_row().assign(成交量=-1),
            "infinite_price": eastmoney_row().assign(最高=float("inf")),
        }
        for name, frame in scenarios.items():
            with self.subTest(name=name), self.assertRaises(daily.DataValidationError):
                daily.normalize_and_check(frame, "eastmoney", "600519", START, END)

    def test_both_fail_without_fabricating_prices_or_leaking_request_text(self):
        errors = [
            requests.exceptions.ProxyError("http://name:private-password@proxy.example/?token=private-token"),
            requests.exceptions.ReadTimeout("private-token"),
        ]
        with patch.object(daily, "fetch_raw", side_effect=errors):
            with redirect_stdout(StringIO()):
                result = daily.fetch_daily("600519", START, END)
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["source"])
        self.assertNotIn("rows", result)
        self.assertEqual(len(result["attempts"]), 2)
        self.assertNotIn("private-", json.dumps(result))

    def test_installed_tencent_adapter_bounds_all_requests_and_keeps_volume_units(self):
        # 使用本机真实 AKShare 解析逻辑，只替换 HTTP 返回；核对两次请求的超时。
        for symbol, tx_symbol, expected_volume in [
            ("600519", "sh600519", 2300),
            ("000001", "sz000001", 2300),
            ("688001", "sh688001", 23),
        ]:
            with self.subTest(symbol=symbol), patch.object(daily.requests, "Session") as session_class:
                session = session_class.return_value.__enter__.return_value

                def response(url, **kwargs):
                    if "weekTrends" in url:
                        payload = {"data": [["2000-01-01"]]}
                    else:
                        payload = {"data": {tx_symbol: {"day": [
                            ["2026-10-15", "10", "11", "12", "9", "23", "0", "0.2", "100"]
                        ]}}}
                    return daily.SimpleNamespace(text="fixture=" + json.dumps(payload))

                session.get.side_effect = response
                data = daily.fetch_raw("tencent", symbol, START, END)
                self.assertEqual(data.iloc[0]["volume"], expected_volume)
                self.assertEqual(session.get.call_count, 2)
                self.assertTrue(all(call.kwargs["timeout"] == (5, 10) for call in session.get.call_args_list))


if __name__ == "__main__":
    unittest.main()
