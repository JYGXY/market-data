"""scripts/fetch_hot_lists.py 的单元测试：模拟网络响应，不真的联网。

运行: python3 -m unittest discover -s tests
"""
import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import fetch_hot_lists as fh  # noqa: E402


class Resp:
    def __init__(self, status=200, payload=None, text=None):
        self.status_code, self._payload = status, payload
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def em_rank(n=100):
    return {"data": [{"sc": ("SZ" if i % 2 else "SH") + f"{600000 + i}", "rk": i + 1}
                     for i in range(n)]}


def em_names(n=100):
    return {"data": {"diff": [{"f12": f"{600000 + i}", "f14": f"名称{i}"} for i in range(n)]}}


def xq_list(field, n=50):
    return {"data": {"count": 5000, "list": [
        {"symbol": f"SH{600000 + i}", "name": f"股票{i}", field: 10000 - i} for i in range(n)]}}


def router(overrides=None):
    """按 URL / 参数返回模拟响应；overrides: {key: Resp 或 异常}，key 见下。"""
    overrides = overrides or {}
    calls = []

    def fake(method, url, **kw):
        if url == fh.EM_RANK_URL:
            key = "em_rank"
        elif url == fh.EM_NAME_URL:
            key = "em_name"
        else:
            p = kw["params"]
            key = f"xq_{p['order_by']}_{p['category'].lower()}"
        calls.append(key)
        # 请求必须带超时和如实的 User-Agent
        assert kw["timeout"] == fh.TIMEOUT
        assert kw["headers"]["User-Agent"] == fh.UA
        o = overrides.get(key)
        if isinstance(o, Exception):
            raise o
        if o is not None:
            return o
        if key == "em_rank":
            return Resp(payload=em_rank())
        if key == "em_name":
            return Resp(payload=em_names())
        return Resp(payload=xq_list(key.split("_")[1]))
    return fake, calls


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [mock.patch.object(fh, "ROOT", root),
                        mock.patch.object(fh, "HOT_DIR", root / "data" / "hot"),
                        mock.patch.object(fh.time, "sleep", lambda s: None)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def run_main(self, overrides=None, argv=()):
        fake, calls = router(overrides)
        with mock.patch.object(fh.requests, "request", side_effect=fake):
            fh.main(list(argv))
        status = json.loads((fh.HOT_DIR / "status.json").read_text(encoding="utf-8"))
        return status, calls


class TestFetch(Base):
    def test_all_ok(self):
        st, _ = self.run_main()
        self.assertTrue(st["core_ok"])
        for name, b in st["boards"].items():
            self.assertEqual(b["status"], "ok", name)
        self.assertEqual(st["boards"]["em_popularity_a"]["rows"], 100)
        self.assertEqual(st["boards"]["xq_follow_cn"]["rows"], 50)
        day = fh.HOT_DIR / st["date"]
        rows = list(csv.DictReader((day / "xq_deal_cn.csv").open(encoding="utf-8")))
        self.assertEqual(list(rows[0]), fh.CSV_FIELDS)
        self.assertEqual((rows[0]["rank"], rows[0]["heat_field"], rows[0]["heat_value"]),
                         ("1", "deal", "10000"))
        em = list(csv.DictReader((day / "em_popularity_a.csv").open(encoding="utf-8")))
        self.assertEqual(em[0]["name"], "名称0")
        self.assertTrue((day / "status.json").exists())
        runs = (fh.HOT_DIR / "runs.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(runs), 1)

    def test_one_board_failing_does_not_affect_others(self):
        st, calls = self.run_main({"xq_tweet_cn": requests.ConnectionError("reset by peer")})
        b = st["boards"]["xq_tweet_cn"]
        self.assertEqual((b["status"], b["error_kind"], b["attempts"]), ("failed", "connection", 3))
        self.assertEqual(calls.count("xq_tweet_cn"), 3)    # 1 次 + 重试 2 次
        self.assertFalse(st["core_ok"])
        for other in ("em_popularity_a", "xq_follow_cn", "xq_deal_cn"):
            self.assertEqual(st["boards"][other]["status"], "ok")

    def test_error_kinds(self):
        st, _ = self.run_main({
            "xq_follow_cn": requests.Timeout("read timeout"),
            "xq_deal_cn": Resp(403, None, "forbidden"),
            "xq_follow_hk": Resp(200, {"error_code": "400016", "error_description": "请登录"}),
            "xq_tweet_hk": Resp(200, None, "<html>captcha</html>"),
            "xq_deal_hk": Resp(502, None, "bad gateway"),
        })
        kinds = {n: st["boards"][n]["error_kind"] for n in
                 ("xq_follow_cn", "xq_deal_cn", "xq_follow_hk", "xq_tweet_hk", "xq_deal_hk")}
        self.assertEqual(kinds, {"xq_follow_cn": "timeout", "xq_deal_cn": "http_4xx",
                                 "xq_follow_hk": "api_error", "xq_tweet_hk": "bad_json",
                                 "xq_deal_hk": "http_5xx"})
        self.assertIn("请登录", st["boards"]["xq_follow_hk"]["error"])

    def test_em_name_failure_keeps_ranks(self):
        st, _ = self.run_main({"em_name": Resp(502, None, "bad gateway")})
        b = st["boards"]["em_popularity_a"]
        self.assertEqual((b["status"], b["rows"], b["error_kind"]), ("partial", 100, "http_5xx"))
        em = list(csv.DictReader((fh.HOT_DIR / st["date"] / "em_popularity_a.csv")
                                 .open(encoding="utf-8")))
        self.assertEqual((em[0]["rank"], em[0]["code"], em[0]["name"]), ("1", "SH600000", ""))
        self.assertFalse(st["core_ok"])     # partial 不算成功，备用会重抓

    def test_short_list_is_partial(self):
        st, _ = self.run_main({"xq_follow_cn": Resp(payload=xq_list("follow", 20))})
        b = st["boards"]["xq_follow_cn"]
        self.assertEqual((b["status"], b["rows"]), ("partial", 20))

    def test_only_missing_keeps_ok_boards(self):
        self.run_main({"xq_tweet_cn": requests.ConnectionError("reset")})
        st, calls = self.run_main(argv=["--only-missing", "--trigger", "backup"])
        self.assertEqual(calls, ["xq_tweet_cn"])          # 只重抓失败的那个
        self.assertTrue(st["core_ok"])
        self.assertEqual(st["last_trigger"], "backup")
        runs = (fh.HOT_DIR / "runs.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(list(json.loads(runs[1])["boards"]), ["xq_tweet_cn"])

    def test_only_missing_keeps_partial_when_retry_fails(self):
        self.run_main({"em_name": Resp(502, None, "bad gateway")})
        st, _ = self.run_main({"em_rank": requests.ConnectionError("reset")},
                              argv=["--only-missing"])
        b = st["boards"]["em_popularity_a"]
        self.assertEqual((b["status"], b["rows"]), ("partial", 100))
        self.assertIn("reset", b["retry_error"])


class TestRunInfoAndRetries(Base):
    def test_run_id_in_status_csv_and_runs(self):
        with mock.patch.dict(fh.os.environ, {"GITHUB_RUN_ID": "987", "GITHUB_RUN_ATTEMPT": "1"}):
            st, _ = self.run_main()
        self.assertEqual((st["run_id"], st["run_attempt"]), (987, 1))
        self.assertEqual(st["boards"]["xq_follow_cn"]["run_id"], 987)
        rows = list(csv.DictReader((fh.HOT_DIR / st["date"] / "xq_follow_cn.csv").open(encoding="utf-8")))
        self.assertEqual(rows[0]["run_id"], "987")
        run = json.loads((fh.HOT_DIR / "runs.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(run["run_id"], 987)

    def test_local_run_id_is_null(self):
        with mock.patch.dict(fh.os.environ, {}, clear=True):
            st, _ = self.run_main()
        self.assertIsNone(st["run_id"])

    def test_retry_errors_kept_on_success(self):
        fake, calls = router()
        n = {"i": 0}

        def flaky(method, url, **kw):
            if url == fh.EM_NAME_URL and n["i"] < 1:
                n["i"] += 1
                return Resp(502, None, "bad gateway")
            return fake(method, url, **kw)
        with mock.patch.object(fh.requests, "request", side_effect=flaky):
            fh.main([])
        st = json.loads((fh.HOT_DIR / "status.json").read_text(encoding="utf-8"))
        b = st["boards"]["em_popularity_a"]
        self.assertEqual(b["status"], "ok")
        self.assertEqual(len(b["retry_errors"]), 1)
        self.assertIn("502", b["retry_errors"][0])
        self.assertNotIn("retry_errors", st["boards"]["xq_follow_cn"])


class TestEmNameRepair(Base):
    """东财名称接口 502：延迟重试、历史名称兜底。"""

    def setUp(self):
        super().setUp()
        self.sleeps = []
        self.p2 = mock.patch.object(fh.time, "sleep", self.sleeps.append)
        self.p2.start()

    def tearDown(self):
        self.p2.stop()
        super().tearDown()

    def run_with_names(self, name_responses):
        """name_responses: 按次序返回给名称接口的 Resp；用完后一直 502。"""
        fake, calls = router()
        seq = list(name_responses)

        def wrapped(method, url, **kw):
            if url == fh.EM_NAME_URL:
                calls.append("em_name")
                return seq.pop(0) if seq else Resp(502, None, "bad gateway")
            return fake(method, url, **kw)
        with mock.patch.object(fh.requests, "request", side_effect=wrapped):
            fh.main([])
        st = json.loads((fh.HOT_DIR / "status.json").read_text(encoding="utf-8"))
        rows = list(csv.DictReader((fh.HOT_DIR / st["date"] / "em_popularity_a.csv")
                                   .open(encoding="utf-8")))
        return st, rows, calls

    def write_previous_day(self, day="2000-01-01", skip_code=None):
        d = fh.HOT_DIR / day
        d.mkdir(parents=True)
        (d / "status.json").write_text(json.dumps(
            {"boards": {"em_popularity_a": {"status": "ok"}}}), encoding="utf-8")
        with (d / "em_popularity_a.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fh.CSV_FIELDS)
            w.writeheader()
            for i, r in enumerate(reversed(em_rank()["data"])):   # 当年的排名顺序不同
                if r["sc"] != skip_code:
                    w.writerow({"date": day, "board": "em_popularity_a", "rank": i + 1,
                                "code": r["sc"], "name": f"旧名{r['sc'][2:]}"})

    def test_502_then_delayed_retry_succeeds(self):
        bad = [Resp(502, None, "bad gateway")] * 3          # 首次请求连同内部重试共 3 次
        st, rows, calls = self.run_with_names(bad + [Resp(payload=em_names())])
        b = st["boards"]["em_popularity_a"]
        self.assertEqual(b["status"], "ok")
        self.assertTrue(st["core_ok"])
        self.assertEqual(calls.count("em_name"), 4)         # 只延迟重试了 1 次
        self.assertEqual([r["result"] for r in b["name_retries"]], ["ok"])
        self.assertTrue(any(x > 100 for x in self.sleeps))  # 确实等待了（约 3 分钟）
        self.assertEqual((rows[0]["rank"], rows[0]["name"]), ("1", "名称0"))
        self.assertNotIn("names_from", b)

    def test_retries_fail_then_cached_names(self):
        self.write_previous_day(skip_code="SZ600001")       # 上一份文件里没有这个“新代码”
        st, rows, calls = self.run_with_names([])
        b = st["boards"]["em_popularity_a"]
        self.assertEqual(b["status"], "ok_names_cached")
        self.assertTrue(st["core_ok"])                      # 不再占用第二次触发
        self.assertEqual(calls.count("em_name"), 3 + 2)     # 首次 3 次 + 延迟重试 2 次
        self.assertEqual(len(b["name_retries"]), 2)
        self.assertEqual(b["names_from"], "2000-01-01")
        self.assertEqual((b["names_cached"], b["names_missing_codes"]), (99, ["SZ600001"]))
        self.assertIn("2000-01-01", b["note"])
        # 排名和代码是当天的（与旧文件顺序不同），只有名称来自旧文件
        self.assertEqual((rows[0]["rank"], rows[0]["code"], rows[0]["name"]),
                         ("1", "SH600000", "旧名600000"))
        self.assertEqual(rows[1]["name"], "")              # 新代码名称留空
        self.assertEqual(rows[0]["date"], st["date"])

    def test_no_cache_stays_partial(self):
        st, rows, _ = self.run_with_names([])
        b = st["boards"]["em_popularity_a"]
        self.assertEqual(b["status"], "partial")
        self.assertFalse(st["core_ok"])
        self.assertIn("没有可用的历史名称", b["note"])

    def test_deadline_skips_waiting(self):
        self.write_previous_day()
        with mock.patch.object(fh, "NAME_RETRY_DEADLINE", 0):
            st, _, calls = self.run_with_names([])
        b = st["boards"]["em_popularity_a"]
        self.assertEqual(calls.count("em_name"), 3)         # 不再延迟重试
        self.assertIn("skipped", b["name_retries"][0])
        self.assertEqual(b["status"], "ok_names_cached")
        self.assertFalse(any(x > 100 for x in self.sleeps))

    def test_cached_status_kept_by_only_missing(self):
        self.write_previous_day()
        self.run_with_names([])
        fake, calls = router()
        with mock.patch.object(fh.requests, "request", side_effect=fake):
            fh.main(["--only-missing"])
        self.assertNotIn("em_rank", calls)                  # 已算成功，不再重抓


if __name__ == "__main__":
    unittest.main()
