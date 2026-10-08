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
        self.assertIn("asia_pacific_indices.asx200", d["stale_reasons"])
        self.assertTrue(d["complete"])      # stale 不影响 complete

    def test_no_overwrite_of_complete_file(self):
        self.run_main(env={"GITHUB_RUN_ID": "1"})
        d = self.run_main(env={"GITHUB_RUN_ID": "2"})           # 第二次不覆盖
        self.assertEqual(d["run_id"], 1)
        day = json.loads((Path(self.tmp.name) / "2026-09-29.json").read_text(encoding="utf-8"))
        self.assertEqual(day["run_id"], 1)
        d = self.run_main(argv=["--force"], env={"GITHUB_RUN_ID": "3"})   # force 覆盖
        self.assertEqual(d["run_id"], 3)


def asx_chart(ts, closes, volumes, reg=None):
    meta = {"exchangeTimezoneName": "Australia/Sydney", "instrumentType": "EQUITY"}
    if reg:
        meta["currentTradingPeriod"] = {"regular": {"start": reg[0], "end": reg[1]}}
    return {"chart": {"result": [{"meta": meta, "timestamp": ts,
                                  "indicators": {"quote": [{"close": closes, "volume": volumes}]}}]}}


class TestAsx300Item(unittest.TestCase):
    BARS = [("2026-10-05", 10.0, 100), ("2026-10-06", 10.5, 200)]

    def test_normal(self):
        i = fm.asx_item("AAA", "Alpha", self.BARS, "2026-10-06")
        self.assertEqual((i["value"], i["prev_close"], i["change"], i["change_pct"], i["date"]),
                         (10.5, 10.0, 0.5, 5.0, "2026-10-06"))
        self.assertEqual(list(i)[:8], ["symbol", "name", "value", "prev_close", "change",
                                       "change_pct", "date", "source"])
        self.assertNotIn("stale", i)

    def test_zero_volume_is_stale_not_zero(self):
        bars = self.BARS + [("2026-10-07", 10.5, 0)]
        i = fm.asx_item("AAA", "Alpha", bars, "2026-10-07")
        self.assertIsNone(i["value"])
        self.assertTrue(i["stale"])
        self.assertIn("2026-10-07 无成交", i["note"])
        self.assertEqual((i["last_trade_date"], i["last_close"]), ("2026-10-06", 10.5))

    def test_missing_session_bar_is_suspension(self):
        i = fm.asx_item("AAA", "Alpha", self.BARS, "2026-10-07")
        self.assertIsNone(i["value"])
        self.assertIn("可能停牌", i["note"])

    def test_new_listing_has_price_but_no_change(self):
        i = fm.asx_item("NEW", "New Co", [("2026-10-07", 3.0, 50)], "2026-10-07")
        self.assertEqual((i["value"], i["change"]), (3.0, None))
        self.assertNotIn("stale", i)


class TestAsx300Section(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name) / "asx300_constituents.json"
        self.file.write_text(json.dumps({"as_of": "2026-10-08", "source": "x", "constituents": [
            {"code": "ZZZ", "name": "Zed"}, {"code": "BBB", "name": "Bee"},
            {"code": "SUS", "name": "Suspended"}, {"code": "ERR", "name": "Broken"}]}))
        self.p = mock.patch.object(fm, "ASX300_FILE", self.file)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def run_section(self, now=T[29] + 3600 * 12):
        ts = [T[25], T[28], T[29]]

        def fake_get(url, **kw):
            if "ERR.AX" in url:
                raise RuntimeError("404 Not Found")
            if "SUS.AX" in url:
                return Resp(asx_chart(ts[:2], [5.0, 5.0], [10, 20]))
            return Resp(asx_chart(ts, [1.0, 2.0, 3.0], [10, 20, 30]))
        with mock.patch.object(fm, "get", side_effect=fake_get), \
                mock.patch.object(fm.time, "time", return_value=now):
            return fm.asx300()

    def test_only_constituents_sorted_by_code(self):
        sec = self.run_section()
        self.assertEqual([i["symbol"] for i in sec["items"]], ["BBB", "ERR", "SUS", "ZZZ"])
        self.assertEqual(sec["session_date"], "2026-09-29")
        self.assertNotIn("rank", sec["items"][0])

    def test_suspension_and_errors_go_to_stale(self):
        data = {k: {} for k in ("us_indices", "rates", "commodities", "fx", "asia_pacific_indices")}
        data["us_top30_by_market_cap"] = {"items": [{"symbol": f"S{i}", "value": 1} for i in range(30)]}
        data["asx300"] = self.run_section()
        stale = dict(fm.stale_entries(data))
        self.assertEqual(sorted(stale), ["asx300.ERR", "asx300.SUS"])
        self.assertIn("可能停牌", stale["asx300.SUS"])
        self.assertIn("404", stale["asx300.ERR"])
        # 4 只里 2 只没价格，低于 95% -> 整段记缺失
        self.assertEqual(fm.missing_items(data), ["asx300 (只取到 2/4 只的价格)"])

    def test_in_session_bar_dropped(self):
        # 9/29 盘中：那根不算，参考日退回 9/28
        reg = (T[29], T[29] + 6 * 3600)
        with mock.patch.object(fm, "get", return_value=Resp(asx_chart(
                [T[25], T[28], T[29], T[29] + 3000], [1.0, 2.0, None, 2.5], [10, 20, 5, 5], reg))), \
                mock.patch.object(fm.time, "time", return_value=T[29] + 3600):
            bars = fm.asx_daily_bars("BBB")
        self.assertEqual([b[0] for b in bars], ["2026-09-25", "2026-09-28"])

    def test_missing_file(self):
        self.file.unlink()
        sec = fm.asx300()
        self.assertIsNone(sec["items"])
        data = {k: {} for k in ("us_indices", "rates", "commodities", "fx", "asia_pacific_indices")}
        data["us_top30_by_market_cap"] = {"items": [{"symbol": f"S{i}", "value": 1} for i in range(30)]}
        data["asx300"] = sec
        self.assertEqual(fm.missing_items(data), ["asx300"])


if __name__ == "__main__":
    unittest.main()
