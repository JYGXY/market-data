"""scripts/update_asx300_constituents.py 的单元测试：模拟接口响应，不真的联网。

运行: python3 -m unittest discover -s tests
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import update_asx300_constituents as up  # noqa: E402


def payload(codes, total=None):
    return {"totalCount": len(codes) if total is None else total,
            "data": [{"s": f"ASX:{c}", "d": [c, f"{c} Ltd", "ASX"]} for c in codes]}


CODES = [f"A{i:02d}" for i in range(299)]


class TestParse(unittest.TestCase):
    def test_sorted_and_complete(self):
        items = up.parse(payload(list(reversed(CODES))))
        self.assertEqual([i["code"] for i in items], sorted(CODES))
        self.assertEqual(items[0], {"code": "A00", "name": "A00 Ltd"})

    def test_rejects_wrong_count(self):
        with self.assertRaises(ValueError):
            up.parse(payload(CODES[:200]))

    def test_rejects_truncated_response(self):
        with self.assertRaises(ValueError):
            up.parse(payload(CODES, total=320))

    def test_rejects_non_asx_symbol(self):
        p = payload(CODES)
        p["data"][0]["s"] = "NASDAQ:AAPL"
        with self.assertRaises(ValueError):
            up.parse(p)


class TestMain(unittest.TestCase):
    def test_bad_response_keeps_old_file(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "asx300_constituents.json"
            out.write_text('{"constituents": [{"code": "OLD", "name": "Old"}]}')
            with mock.patch.object(up, "OUT", out), \
                    mock.patch.object(up, "fetch", return_value=payload(CODES[:10])):
                self.assertEqual(up.main([]), 1)
            self.assertIn("OLD", out.read_text())

    def test_writes_file(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "data" / "asx300_constituents.json"
            with mock.patch.object(up, "OUT", out), mock.patch.object(up, "ROOT", Path(d)), \
                    mock.patch.object(up, "fetch", return_value=payload(CODES)):
                self.assertEqual(up.main([]), 0)
            doc = json.loads(out.read_text())
            self.assertEqual((doc["index"], doc["count"]), ("S&P/ASX 300", 299))


if __name__ == "__main__":
    unittest.main()
