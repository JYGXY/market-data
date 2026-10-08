#!/usr/bin/env python3
"""更新 S&P/ASX 300 成分股名单（每季度一次）。

来源：TradingView 公开的 S&P/ASX 300（XKO）成分股页面；页面本身通过公开的筛选接口
（scanner.tradingview.com，无需登录）加载完整名单，这里直接调用同一接口，请求头如实标明身份。
成分股是指数的全部成员，不做任何挑选、排序或标注；输出按代码字母顺序排列。

输出: data/asx300_constituents.json
用法: python3 scripts/update_asx300_constituents.py [--dry-run]
  名单数量不在 280–320 之间、或代码格式异常时拒绝写入，保留旧名单（退出码 1）。
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "asx300_constituents.json"
UA = {"User-Agent": "Mozilla/5.0 (compatible; public-market-data-bot/1.0)"}
SOURCE_PAGE = "https://www.tradingview.com/symbols/ASX-XKO/components/"
SCAN_URL = "https://scanner.tradingview.com/australia/scan"
SCAN_BODY = {"columns": ["name", "description", "exchange"],
             "symbols": {"symbolset": ["SYML:ASX;XKO"]},
             "range": [0, 400]}
MIN_COUNT, MAX_COUNT = 280, 320
CODE_RE = re.compile(r"^[A-Z0-9]{3,6}$")


def fetch(timeout=30, tries=3):
    last = None
    for _ in range(tries):
        try:
            r = requests.post(SCAN_URL, json=SCAN_BODY, headers=UA, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
    raise last


def parse(payload):
    """返回按代码排序的 [{"code", "name"}]；格式不对抛 ValueError。"""
    rows = payload.get("data") or []
    total = payload.get("totalCount")
    out = {}
    for row in rows:
        exch, _, code = (row.get("s") or "").partition(":")
        d = row.get("d") or []
        name = (d[1] if len(d) > 1 else "") or ""
        if exch != "ASX" or not CODE_RE.match(code):
            raise ValueError(f"unexpected symbol {row.get('s')!r}")
        out[code] = name.strip()
    if total is not None and total != len(rows):
        raise ValueError(f"source reports {total} constituents but returned {len(rows)}")
    if not MIN_COUNT <= len(out) <= MAX_COUNT:
        raise ValueError(f"{len(out)} constituents, expected {MIN_COUNT}-{MAX_COUNT}")
    return [{"code": c, "name": out[c]} for c in sorted(out)]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    args = ap.parse_args(argv)
    try:
        items = parse(fetch())
    except Exception as e:  # noqa: BLE001
        print(f"refusing to update constituents: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    now = datetime.now(timezone.utc)
    doc = {
        "index": "S&P/ASX 300",
        "as_of": now.astimezone(ZoneInfo("Australia/Sydney")).date().isoformat(),
        "fetched_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": SOURCE_PAGE,
        "note": "指数全部成分股，按代码字母顺序排列；每季度（3/6/9/12 月调整生效后）更新一次",
        "count": len(items),
        "constituents": items,
    }
    old = []
    if OUT.exists():
        try:
            old = [c["code"] for c in json.loads(OUT.read_text(encoding="utf-8"))["constituents"]]
        except (ValueError, KeyError):
            old = []
    new = [c["code"] for c in items]
    added, removed = sorted(set(new) - set(old)), sorted(set(old) - set(new))
    print(f"{len(items)} constituents; +{len(added)} -{len(removed)} vs previous file", file=sys.stderr)
    if args.dry_run:
        print(json.dumps(doc, ensure_ascii=False))
        return 0
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
