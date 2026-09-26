#!/usr/bin/env python3
"""每日热榜抓取（试跑）：东方财富人气榜、雪球热股榜。

只抓公开榜单：排名、代码、名称、接口给的热度数值。不抓价格，不接受任何外部股票清单。

接口地址与 akshare 的 stock_hot_rank_em / stock_hot_{follow,tweet,deal}_xq 相同，
但不调用 akshare 函数本身，原因：
  - akshare 的雪球请求头伪装成 Chrome 浏览器；本仓库规定不伪装请求头，
    这里用如实标明身份的 User-Agent，被拒绝就如实记录；
  - akshare 的请求不设超时，也没有重试；
  - akshare 的雪球函数翻页拉取全部 A 股（约 5000 只），这里只取前 50，一次请求。

输出（日期为悉尼日期）：
  data/hot/YYYY-MM-DD/<榜单>.csv
  data/hot/YYYY-MM-DD/status.json   当天各榜最终状态
  data/hot/status.json              同上，最近一天
  data/hot/runs.jsonl               每次运行追加一行（试跑报告用）

用法:
  python3 scripts/fetch_hot_lists.py [--trigger primary|backup|manual] [--only-missing]
  --only-missing：当天已成功的榜单保留不动，只重抓未成功的（备用运行用）。
"""
import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

SYD = ZoneInfo("Australia/Sydney")
ROOT = Path(__file__).resolve().parent.parent
HOT_DIR = ROOT / "data" / "hot"

# 如实标明身份，不冒充浏览器
UA = "market-data-bot/1.0 (+https://github.com/JYGXY/market-data; public hot-list trial)"
TIMEOUT = 30          # 每次请求超时（秒）
RETRIES = 2           # 失败后重试次数（共 3 次尝试）
BACKOFF = [3, 6]      # 重试前等待（秒）

EM_RANK_URL = "https://emappdata.eastmoney.com/stockrank/getAllCurrentList"
EM_NAME_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
EM_PAGE = "https://guba.eastmoney.com/rank/"
XQ_URL = "https://xueqiu.com/service/v5/stock/screener/screen"
XQ_PAGE = "https://xueqiu.com/hq"

CSV_FIELDS = ["date", "fetched_at_utc", "fetched_at_sydney", "board", "rank",
              "code", "name", "heat_value", "heat_field"]

# 榜单定义。core=True 的榜单决定“当天是否成功”（备用运行据此判断）；
# 港股榜 akshare 不支持，这里试探雪球同一接口的 category=HK，标记为 experimental。
BOARDS = {
    "em_popularity_a": {"source": "eastmoney", "size": 100, "core": True,
                        "desc": "东方财富人气榜（A 股）"},
    "xq_follow_cn": {"source": "xueqiu", "category": "CN", "order_by": "follow", "size": 50,
                     "core": True, "desc": "雪球热股榜-关注（沪深）"},
    "xq_tweet_cn": {"source": "xueqiu", "category": "CN", "order_by": "tweet", "size": 50,
                    "core": True, "desc": "雪球热股榜-讨论（沪深）"},
    "xq_deal_cn": {"source": "xueqiu", "category": "CN", "order_by": "deal", "size": 50,
                   "core": True, "desc": "雪球热股榜-交易（沪深）"},
    "xq_follow_hk": {"source": "xueqiu", "category": "HK", "order_by": "follow", "size": 50,
                     "core": False, "experimental": True, "desc": "雪球热股榜-关注（港股，试探）"},
    "xq_tweet_hk": {"source": "xueqiu", "category": "HK", "order_by": "tweet", "size": 50,
                    "core": False, "experimental": True, "desc": "雪球热股榜-讨论（港股，试探）"},
    "xq_deal_hk": {"source": "xueqiu", "category": "HK", "order_by": "deal", "size": 50,
                   "core": False, "experimental": True, "desc": "雪球热股榜-交易（港股，试探）"},
}


class FetchError(Exception):
    """kind: timeout / connection / http_4xx / http_5xx / bad_json / api_error / empty"""

    def __init__(self, kind, message, attempts=0):
        super().__init__(message)
        self.kind, self.attempts = kind, attempts


def _classify(exc):
    if isinstance(exc, FetchError):
        return exc.kind
    if isinstance(exc, requests.Timeout):
        return "timeout"
    if isinstance(exc, requests.ConnectionError):
        return "connection"
    return "other"


def request_json(method, url, **kwargs):
    """带超时和重试的请求。返回 (json, 尝试次数)；全部失败抛 FetchError。"""
    errors, kinds = [], []
    for i in range(RETRIES + 1):
        try:
            r = requests.request(method, url, timeout=TIMEOUT,
                                 headers={"User-Agent": UA, "Accept": "application/json"},
                                 **kwargs)
            if r.status_code != 200:
                kind = "http_4xx" if 400 <= r.status_code < 500 else "http_5xx"
                raise FetchError(kind, f"HTTP {r.status_code}: {r.text[:120]!r}")
            try:
                return r.json(), i + 1
            except ValueError:
                raise FetchError("bad_json", f"非 JSON 响应: {r.text[:120]!r}")
        except (requests.RequestException, FetchError) as e:
            kinds.append(_classify(e))
            errors.append(f"第{i + 1}次 {type(e).__name__}: {e}"[:240])
            if i < RETRIES:
                time.sleep(BACKOFF[i])
    # 以最后一次失败的类型为准
    raise FetchError(kinds[-1], " | ".join(errors), attempts=RETRIES + 1)


def fetch_eastmoney(cfg):
    """返回 (rows, attempts, 名称错误或 None)。名称接口失败时仍返回排名和代码。"""
    data, attempts = request_json("POST", EM_RANK_URL, json={
        "appId": "appId01", "globalId": "786e4c21-70dc-435a-93bb-38",
        "marketType": "", "pageNo": 1, "pageSize": cfg["size"]})
    items = data.get("data") if isinstance(data, dict) else None
    if not items:
        raise FetchError("api_error" if items is None else "empty",
                         f"榜单接口无 data: {str(data)[:160]}", attempts)
    rows = [{"rank": it["rk"], "code": it["sc"], "name": None,
             "heat_value": None, "heat_field": ""} for it in items]
    # 名称：东财行情接口，只取代码(f12)和名称(f14)，不取价格
    secids = ",".join(("0." if r["code"].startswith("SZ") else "1.") + r["code"][2:] for r in rows)
    name_error = None
    try:
        nd, a2 = request_json("GET", EM_NAME_URL, params={
            "ut": "f057cbcbce2a86e2866ab8877db1d059", "fltt": "2", "invt": "2",
            "fields": "f12,f14", "secids": secids})
        attempts += a2
        diff = ((nd or {}).get("data") or {}).get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        names = {d.get("f12"): d.get("f14") for d in diff}
        for r in rows:
            r["name"] = names.get(r["code"][2:])
        missing = sum(r["name"] is None for r in rows)
        if missing:
            name_error = FetchError("empty", f"名称接口缺 {missing} 个名称")
    except FetchError as e:
        attempts += e.attempts
        name_error = FetchError(e.kind, f"名称接口失败（排名和代码已保存）: {e}")
    return rows, attempts, name_error


def fetch_xueqiu(cfg):
    data, attempts = request_json("GET", XQ_URL, params={
        "category": cfg["category"], "size": cfg["size"], "order": "desc",
        "order_by": cfg["order_by"], "only_count": "0", "page": "1"})
    if not isinstance(data, dict) or "data" not in data:
        d = data if isinstance(data, dict) else {}
        raise FetchError("api_error", f"接口返回 {d.get('error_code')}: "
                         f"{d.get('error_description', str(data)[:160])}", attempts)
    items = (data["data"] or {}).get("list") or []
    if not items:
        raise FetchError("empty", "接口返回空列表", attempts)
    rows = [{"rank": i + 1, "code": it.get("symbol"), "name": it.get("name"),
             "heat_value": it.get(cfg["order_by"]), "heat_field": cfg["order_by"]}
            for i, it in enumerate(items[:cfg["size"]])]
    return rows, attempts, None


def run_board(name, cfg, day_dir, stamp):
    t0 = time.monotonic()
    entry = {"desc": cfg["desc"], "core": cfg["core"],
             "experimental": cfg.get("experimental", False),
             "source": EM_PAGE if cfg["source"] == "eastmoney" else XQ_PAGE,
             "run_at_utc": stamp["utc"]}
    try:
        fn = fetch_eastmoney if cfg["source"] == "eastmoney" else fetch_xueqiu
        rows, attempts, soft_error = fn(cfg)
        path = day_dir / f"{name}.csv"
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            w.writeheader()
            for r in rows:
                w.writerow({"date": stamp["date"], "fetched_at_utc": stamp["utc"],
                            "fetched_at_sydney": stamp["sydney"], "board": name, **r})
        status = "ok" if not soft_error and len(rows) == cfg["size"] else "partial"
        err = soft_error or (None if len(rows) == cfg["size"]
                             else FetchError("empty", f"只取到 {len(rows)}/{cfg['size']} 行"))
        entry.update(status=status, rows=len(rows), attempts=attempts,
                     file=str(path.relative_to(ROOT)),
                     error=str(err) if err else None, error_kind=err.kind if err else None)
    except FetchError as e:
        entry.update(status="failed", rows=0, attempts=e.attempts, file=None,
                     error=str(e), error_kind=e.kind)
    except Exception as e:  # noqa: BLE001  解析等意外错误也只影响本榜
        entry.update(status="failed", rows=0, attempts=None, file=None,
                     error=f"{type(e).__name__}: {e}"[:300], error_kind="other")
    entry["seconds"] = round(time.monotonic() - t0, 2)
    return entry


def core_ok(boards):
    return all(boards.get(n, {}).get("status") == "ok" for n, c in BOARDS.items() if c["core"])


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--trigger", default="manual")
    ap.add_argument("--only-missing", action="store_true")
    args = ap.parse_args(argv)

    now = datetime.now(timezone.utc)
    stamp = {"utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "sydney": now.astimezone(SYD).strftime("%Y-%m-%d %H:%M:%S %Z"),
             "date": now.astimezone(SYD).date().isoformat()}
    day_dir = HOT_DIR / stamp["date"]
    day_dir.mkdir(parents=True, exist_ok=True)
    day_status_path = day_dir / "status.json"

    previous = {}
    if args.only_missing and day_status_path.exists():
        previous = json.loads(day_status_path.read_text(encoding="utf-8")).get("boards", {})

    fetched, boards = {}, {}
    for i, (name, cfg) in enumerate(BOARDS.items()):
        if previous.get(name, {}).get("status") == "ok":
            boards[name] = previous[name]          # 当天已成功，保留
            continue
        if fetched:
            time.sleep(1)                          # 各榜之间稍作间隔
        entry = fetched[name] = run_board(name, cfg, day_dir, stamp)
        prev = previous.get(name)
        if entry["status"] == "failed" and prev and prev.get("status") == "partial":
            # 重抓失败时保留之前的部分结果（CSV 未被覆盖）
            entry = {**prev, "retry_error": entry["error"], "retry_at_utc": stamp["utc"]}
        boards[name] = entry

    status = {
        "date": stamp["date"],
        "last_run_at_utc": stamp["utc"],
        "last_run_at_sydney": stamp["sydney"],
        "last_trigger": args.trigger,
        "core_ok": core_ok(boards),
        "note": "公开榜单试跑。core=true 的榜单全部 ok 才算当天成功；港股榜为试探。",
        "boards": boards,
    }
    body = json.dumps(status, ensure_ascii=False, indent=2) + "\n"
    day_status_path.write_text(body, encoding="utf-8")
    (HOT_DIR / "status.json").write_text(body, encoding="utf-8")
    with (HOT_DIR / "runs.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"date": stamp["date"], "run_at_utc": stamp["utc"],
                            "trigger": args.trigger, "only_missing": args.only_missing,
                            "boards": fetched}, ensure_ascii=False) + "\n")

    for name, e in boards.items():
        mark = "(保留)" if name not in fetched else f"{e['seconds']}s"
        print(f"{name:18} {e['status']:8} rows={e['rows']:<4} {mark:8} {e.get('error') or ''}"[:300],
              file=sys.stderr)
    print(f"core_ok={status['core_ok']}", file=sys.stderr)


if __name__ == "__main__":
    main()
