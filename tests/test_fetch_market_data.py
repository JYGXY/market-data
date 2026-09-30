"""scripts/fetch_market_data.py 的单元测试：模拟 Yahoo 响应，不真的联网。

运行: python3 -m unittest discover -s tests
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import fetch_market_data as fm  # noqa: E402

DAY = 86400
# 2026-09-24 .. 2026-09-29 00:00 UTC（ASX 日线时间戳就是悉尼开盘 = UTC 00:00 左右）
T = {d: 1790208000 + (d - 24) * DAY for d in range(22, 31)}


class Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


def chart(ts, closes, meta_extra=None):
    meta = {"exchangeTimezoneName": "Australia/Sydney", "instrumentType": "INDEX"}
    meta.update(meta_extra or {})
    return {"chart": {"result": [{"meta": meta, "timestamp": ts,
                                  "indicators": {"quote": [{"close": closes}]}}]}}


class TestStaleClose(unittest.TestCase):
    def run_daily(self, ten_day, one_day=None, now=None):
        def fake_get(url, **kw):
            if "interval=1h" in url:
                if one_day is None:
                    raise RuntimeError("1d unavailable")
                return Resp(one_day)
            return Resp(ten_day)
        with mock.patch.object(fm, "get", side_effect=fake_get), \
                mock.patch.object(fm.time, "time", return_value=now or T[29] + 3600):
            return fm.yahoo_daily("asx200", "ASX 200", "^AXJO", "点", closes_only=True)

    def asx_0929_payload(self):
        # 复现 2026-09-29 00:21 UTC：9/29 盘中；9/28 那根收盘为空
        return chart([T[24], T[25], T[28], T[29]], [8702.0, 8665.0, None, 8690.0],
                     {"currentTradingPeriod": {"regular": {"start": T[29], "end": T[29] + 6 * 3600}},
                      "regularMarketPrice": 8690.0, "regularMarketTime": T[29] + 3000})

    def test_gap_filled_from_previous_close(self):
        # 小时线：9/28 当天几根，最后一根 8679.7；9/29 盘中的不应被取用
        hourly = chart([T[28] + 3600, T[28] + 6 * 3600, T[29] + 3600], [8676.5, 8679.7, 8690.0],
                       {"chartPreviousClose": 8765.3})
        item = self.run_daily(self.asx_0929_payload(), one_day=hourly)
        self.assertEqual((item["value"], item["prev_close"], item["date"]), (8679.7, 8665.0, "2026-09-28"))
        self.assertNotIn("stale", item)
        self.assertIn("2026-09-28 日线收盘缺失，取当日最后一根小时线收盘", item["note"])

    def test_gap_unfillable_marked_stale(self):
        item = self.run_daily(self.asx_0929_payload(), one_day=None)
        self.assertEqual((item["value"], item["date"]), (8665.0, "2026-09-25"))
        self.assertTrue(item["stale"])
        self.assertIn("2026-09-28", item["note"])

    def test_normal_in_session_not_stale(self):
        payload = chart([T[25], T[28], T[29]], [8665.0, 8679.7, 8690.0],
                        {"currentTradingPeriod": {"regular": {"start": T[29], "end": T[29] + 6 * 3600}}})
        item = self.run_daily(payload)
        self.assertEqual((item["value"], item["date"]), (8679.7, "2026-09-28"))
        self.assertNotIn("stale", item)


class TestMain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = mock.patch.object(fm, "DATA_DIR", Path(self.tmp.name))
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def fake_build(self, sp_date="2026-09-29", stale=False):
        item = {"value": 1.0, "date": sp_date}
        asia = {"asx200": {**item, **({"stale": True} if stale else {})}}
        return lambda: {"generated_at_utc": "x", "us_indices": {"sp500": item},
                        "us_top30_by_market_cap": {"items": [{"symbol": f"S{i}", "value": 1} for i in range(30)]},
                        "rates": {}, "commodities": {}, "fx": {}, "asia_pacific_indices": asia}

    def run_main(self, argv=(), env=None, **kw):
        with mock.patch.object(fm, "build", self.fake_build(**kw)), \
                mock.patch.dict(fm.os.environ, env or {}, clear=True):
            fm.main(list(argv))
        return json.loads((Path(self.tmp.name) / "latest.json").read_text(encoding="utf-8"))

    def test_run_metadata(self):
        d = self.run_main(env={"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2",
                               "RUN_TRIGGER": "workflow_dispatch:full"})
        self.assertEqual((d["run_id"], d["run_attempt"], d["run_trigger"]),
                         (123, 2, "workflow_dispatch:full"))
        self.assertEqual(list(d)[:4], ["trading_date", "complete", "missing", "stale"])
        d = self.run_main(argv=["--force"])
        self.assertEqual((d["run_id"], d["run_trigger"]), (None, "local"))

    def test_stale_list(self):
        d = self.run_main(stale=True)
        self.assertEqual(d["stale"], ["asia_pacific_indices.asx200"])
        self.assertTrue(d["complete"])      # stale 不影响 complete

    def test_no_overwrite_of_complete_file(self):
        self.run_main(env={"GITHUB_RUN_ID": "1"})
        d = self.run_main(env={"GITHUB_RUN_ID": "2"})           # 第二次不覆盖
        self.assertEqual(d["run_id"], 1)
        day = json.loads((Path(self.tmp.name) / "2026-09-29.json").read_text(encoding="utf-8"))
        self.assertEqual(day["run_id"], 1)
        d = self.run_main(argv=["--force"], env={"GITHUB_RUN_ID": "3"})   # force 覆盖
        self.assertEqual(d["run_id"], 3)


if __name__ == "__main__":
    unittest.main()
