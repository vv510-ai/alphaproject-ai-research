"""检查报价身份与时间处理；固定原始响应仅用于回归，不冒充当前行情。"""

from datetime import datetime, timedelta
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import requests


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("spot_experiment", ROOT / "experiments/02_fetch_spot.py")
spot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(spot)
FIXTURE = json.loads((ROOT / "evidence/data-source/spot-probe-600519-20261016T215657.json").read_text(encoding="utf-8"))
BODY = FIXTURE["raw_text"]
RECEIVED = datetime.fromisoformat(FIXTURE["received_at"])


def change_field(index, value):
    fields = BODY.split('"')[1].split("~")
    fields[index] = value
    return 'v_sh600519="' + "~".join(fields) + '";'


def sample(body=BODY, received=RECEIVED):
    return {"status": "ok", "quote": spot.parse_quote(body, "600519", received)}


class SpotExperimentTests(unittest.TestCase):
    def test_observed_response_keeps_identity_price_and_provider_time(self):
        quote = sample()["quote"]
        self.assertEqual(quote["symbol"], "600519")
        self.assertEqual(quote["name"], "贵州茅台")
        self.assertEqual(quote["price"], 1329.00)
        self.assertEqual(quote["source_reported_at"], "2026-10-16T16:14:47+08:00")
        self.assertGreater(quote["source_age_seconds"], 5 * 3600)

    def test_reading_again_does_not_make_old_data_fresh(self):
        a, b = sample(), sample(received=RECEIVED + timedelta(seconds=10))
        result = spot.compare_samples([a, b])
        self.assertEqual(a["quote"]["source_reported_at"], b["quote"]["source_reported_at"])
        self.assertAlmostEqual(b["quote"]["source_age_seconds"] - a["quote"]["source_age_seconds"], 10)
        self.assertFalse(result["price_changed"])
        self.assertFalse(result["source_time_advanced"])
        self.assertFalse(result["realtime_verified"])

    def test_missing_provider_time_remains_unknown(self):
        first, second = sample(change_field(30, "")), sample(change_field(30, "-"))
        self.assertIsNone(first["quote"]["source_reported_at"])
        self.assertIsNone(first["quote"]["source_age_seconds"])
        comparison = spot.compare_samples([first, second])
        self.assertIsNone(comparison["source_time_advanced"])
        self.assertFalse(comparison["realtime_verified"])

    def test_bad_identity_prices_format_or_future_time_are_rejected(self):
        cases = {
            "empty": "", "html": "<html>blocked</html>",
            "wrong_market": BODY.replace("v_sh600519", "v_sz600519"),
            "wrong_code": change_field(2, "000001"),
            "not_finite": change_field(3, "nan"),
            "zero_price": change_field(3, "0"),
            "wrong_change": change_field(32, "99"),
            "bad_time_format": change_field(30, "2026-10-16"),
            "invalid_date": change_field(30, "20261316161447"),
            "future_time": change_field(30, "20261017210000"),
        }
        for name, body in cases.items():
            with self.subTest(name=name), self.assertRaises(spot.QuoteDataError):
                spot.parse_quote(body, "600519", RECEIVED)

    def test_time_progress_can_happen_without_price_change_and_regression_is_visible(self):
        later = sample(change_field(30, "20261016161457"))
        progress = spot.compare_samples([sample(), later])
        self.assertTrue(progress["source_time_advanced"])
        self.assertFalse(progress["price_changed"])
        self.assertFalse(progress["realtime_verified"])
        backwards = spot.compare_samples([later, sample()])
        self.assertTrue(backwards["source_time_regressed"])

    def test_network_failure_cannot_become_zero_price_or_current_time(self):
        with patch.object(spot.requests, "get", side_effect=requests.Timeout("private-request-detail")):
            result = spot.fetch_once("600519")
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("quote", result)
        self.assertNotIn("private-request-detail", json.dumps(result))
        self.assertFalse(spot.compare_samples([result, sample()])["realtime_verified"])


if __name__ == "__main__":
    unittest.main()
