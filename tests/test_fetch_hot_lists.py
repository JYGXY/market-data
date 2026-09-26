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


if __name__ == "__main__":
    unittest.main()
