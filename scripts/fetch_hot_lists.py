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
import os
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

# 东财名称接口（push2）经常短暂 502：同一次运行内延迟重试，仍失败再用历史名称兜底。
# 延迟重试放在所有榜单抓完之后，等待时间与雪球榜单的抓取重叠。
NAME_RETRY_DELAY = 180      # 距上一次名称请求至少等这么久再试（秒）
NAME_RETRY_TIMES = 2        # 延迟重试最多几次（每次只请求一次，不再内部重试）
NAME_RETRY_DEADLINE = 14 * 60   # 距脚本开始超过这么久就不再等待重试，直接兜底（job 超时 20 分钟）
OK_STATUSES = ("ok", "ok_names_cached")   # 算作成功的状态

EM_RANK_URL = "https://emappdata.eastmoney.com/stockrank/getAllCurrentList"
EM_NAME_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
EM_PAGE = "https://guba.eastmoney.com/rank/"
XQ_URL = "https://xueqiu.com/service/v5/stock/screener/screen"
XQ_PAGE = "https://xueqiu.com/hq"

CSV_FIELDS = ["date", "fetched_at_utc", "fetched_at_sydney", "board", "rank",
              "code", "name", "heat_value", "heat_field", "run_id"]

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


def request_json(method, url, retries=RETRIES, **kwargs):
    """带超时和重试的请求。返回 (json, 尝试次数, 之前失败的原因列表)；全部失败抛 FetchError。"""
    errors, kinds = [], []
    for i in range(retries + 1):
        try:
            r = requests.request(method, url, timeout=TIMEOUT,
                                 headers={"User-Agent": UA, "Accept": "application/json"},
                                 **kwargs)
            if r.status_code != 200:
                kind = "http_4xx" if 400 <= r.status_code < 500 else "http_5xx"
                raise FetchError(kind, f"HTTP {r.status_code}: {r.text[:120]!r}")
            try:
                return r.json(), i + 1, errors
            except ValueError:
                raise FetchError("bad_json", f"非 JSON 响应: {r.text[:120]!r}")
        except (requests.RequestException, FetchError) as e:
            kinds.append(_classify(e))
            errors.append(f"第{i + 1}次 {type(e).__name__}: {e}"[:240])
            if i < retries:
                time.sleep(BACKOFF[i])
    # 以最后一次失败的类型为准
    raise FetchError(kinds[-1], " | ".join(errors), attempts=retries + 1)


def fetch_eastmoney(cfg):
    """返回 (rows, attempts, 名称错误或 None)。名称接口失败时仍返回排名和代码。"""
    data, attempts, retry_errors = request_json("POST", EM_RANK_URL, json={
        "appId": "appId01", "globalId": "786e4c21-70dc-435a-93bb-38",
        "marketType": "", "pageNo": 1, "pageSize": cfg["size"]})
    items = data.get("data") if isinstance(data, dict) else None
    if not items:
        raise FetchError("api_error" if items is None else "empty",
                         f"榜单接口无 data: {str(data)[:160]}", attempts)
    rows = [{"rank": it["rk"], "code": it["sc"], "name": None,
             "heat_value": None, "heat_field": ""} for it in items]
    name_error = None
    try:
        a2, e2 = fill_em_names(rows)
        attempts += a2
        retry_errors += [f"名称接口 {e}" for e in e2]
        missing = sum(not r["name"] for r in rows)
        if missing:
            name_error = FetchError("empty", f"名称接口缺 {missing} 个名称")
    except FetchError as e:
        attempts += e.attempts
        name_error = FetchError(e.kind, f"名称接口失败（排名和代码已保存）: {e}")
    if name_error:
        name_error.names_only = True    # 排名和代码已取到，只缺名称：可延迟重试 / 兜底
    return rows, attempts, name_error, retry_errors


def fill_em_names(rows, retries=RETRIES):
    """用东财行情接口补名称（只取代码 f12 和名称 f14，不取价格），只填还没有名称的行。

    返回 (尝试次数, 之前失败的原因列表)；全部失败抛 FetchError。
    """
    todo = [r for r in rows if not r["name"]]
    secids = ",".join(("0." if r["code"].startswith("SZ") else "1.") + r["code"][2:] for r in todo)
    nd, attempts, errors = request_json("GET", EM_NAME_URL, retries=retries, params={
        "ut": "f057cbcbce2a86e2866ab8877db1d059", "fltt": "2", "invt": "2",
        "fields": "f12,f14", "secids": secids})
    diff = ((nd or {}).get("data") or {}).get("diff") or []
    if isinstance(diff, dict):
        diff = list(diff.values())
    names = {d.get("f12"): d.get("f14") for d in diff}
    for r in todo:
        r["name"] = names.get(r["code"][2:])
    return attempts, errors


def load_name_cache(today):
    """最近一份（早于 today）名称完整成功（status=ok）的东财人气榜：返回 ({代码: 名称}, 日期) 或 ({}, None)。"""
    for d in sorted((p for p in HOT_DIR.iterdir() if p.is_dir() and p.name < today), reverse=True):
        try:
            st = json.loads((d / "status.json").read_text(encoding="utf-8"))
            if st["boards"]["em_popularity_a"]["status"] != "ok":
                continue
            with (d / "em_popularity_a.csv").open(encoding="utf-8") as f:
                names = {r["code"]: r["name"] for r in csv.DictReader(f) if r.get("name")}
        except (OSError, ValueError, KeyError):
            continue
        if names:
            return names, d.name
    return {}, None


def fetch_xueqiu(cfg):
    data, attempts, retry_errors = request_json("GET", XQ_URL, params={
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
    return rows, attempts, None, retry_errors


def run_board(name, cfg, day_dir, stamp):
    t0 = time.monotonic()
    entry = {"desc": cfg["desc"], "core": cfg["core"],
             "experimental": cfg.get("experimental", False),
             "source": EM_PAGE if cfg["source"] == "eastmoney" else XQ_PAGE,
             "run_at_utc": stamp["utc"], "run_id": stamp["run_id"]}
    rows = None
    try:
        fn = fetch_eastmoney if cfg["source"] == "eastmoney" else fetch_xueqiu
        rows, attempts, soft_error, retry_errors = fn(cfg)
        path = write_csv(name, rows, day_dir, stamp)
        entry["names_only"] = bool(getattr(soft_error, "names_only", False))
        status = "ok" if not soft_error and len(rows) == cfg["size"] else "partial"
        err = soft_error or (None if len(rows) == cfg["size"]
                             else FetchError("empty", f"只取到 {len(rows)}/{cfg['size']} 行"))
        entry.update(status=status, rows=len(rows), attempts=attempts,
                     file=str(path.relative_to(ROOT)),
                     error=str(err) if err else None, error_kind=err.kind if err else None)
        if retry_errors:
            # 最终成功（或部分成功）前失败过的尝试，保留原因供试跑统计
            entry["retry_errors"] = [e[:240] for e in retry_errors]
    except FetchError as e:
        entry.update(status="failed", rows=0, attempts=e.attempts, file=None,
                     error=str(e), error_kind=e.kind)
    except Exception as e:  # noqa: BLE001  解析等意外错误也只影响本榜
        entry.update(status="failed", rows=0, attempts=None, file=None,
                     error=f"{type(e).__name__}: {e}"[:300], error_kind="other")
    entry["seconds"] = round(time.monotonic() - t0, 2)
    return entry, rows


def write_csv(name, rows, day_dir, stamp):
    path = day_dir / f"{name}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({"date": stamp["date"], "fetched_at_utc": stamp["utc"],
                        "fetched_at_sydney": stamp["sydney"], "board": name,
                        "run_id": stamp["run_id"], **r})
    return path


def repair_em_names(name, entry, rows, day_dir, stamp, t_start, t_last):
    """东财名称缺失时：延迟重试（最多 NAME_RETRY_TIMES 次，每次间隔 NAME_RETRY_DELAY），
    仍缺则用上一份成功文件里同代码的名称补上（状态 ok_names_cached）。排名和代码始终是当天的。"""
    t0 = time.monotonic()
    tries = entry.setdefault("name_retries", [])
    for _ in range(NAME_RETRY_TIMES):
        start_at = t_last + NAME_RETRY_DELAY
        if start_at - t_start > NAME_RETRY_DEADLINE:
            tries.append({"skipped": f"距开始已超过 {NAME_RETRY_DEADLINE // 60} 分钟，不再等待重试"})
            break
        time.sleep(max(0.0, start_at - time.monotonic()))
        at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            fill_em_names(rows, retries=0)
            left = sum(not r["name"] for r in rows)
            tries.append({"at_utc": at, "result": "ok" if not left else f"仍缺 {left} 个名称"})
        except FetchError as e:
            tries.append({"at_utc": at, "result": f"{e.kind}: {e}"[:240]})
        entry["attempts"] = (entry.get("attempts") or 0) + 1
        t_last = time.monotonic()
        if all(r["name"] for r in rows):
            write_csv(name, rows, day_dir, stamp)
            entry.update(status="ok", error=None, error_kind=None,
                         note="名称接口首次失败，延迟重试后取到")
            break
    if entry["status"] != "ok":
        cache, from_day = load_name_cache(stamp["date"])
        if from_day:
            for r in rows:
                if not r["name"] and cache.get(r["code"]):
                    r["name"], r["_cached"] = cache[r["code"]], True
            cached = [r["code"] for r in rows if r.pop("_cached", False)]
            still = [r["code"] for r in rows if not r["name"]]
            write_csv(name, rows, day_dir, stamp)
            entry.update(status="ok_names_cached", names_from=from_day,
                         names_cached=len(cached), names_missing_codes=still,
                         note=(f"名称接口重试后仍失败；排名和代码为当天数据，{len(cached)} 个名称取自 "
                               f"{from_day} 的成功文件" + (f"，{len(still)} 个新代码名称留空"
                                                           if still else "")))
        else:
            entry["note"] = "名称接口重试后仍失败，且没有可用的历史名称"
    entry["seconds"] = round((entry.get("seconds") or 0) + time.monotonic() - t0, 2)
    return entry


def run_info():
    """版本标识：下游引用时写上 run_id，就能分清是哪一次运行生成的。本地运行时为 null。"""
    rid = os.environ.get("GITHUB_RUN_ID")
    att = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    return {"run_id": int(rid) if rid and rid.isdigit() else None,
            "run_attempt": int(att) if att.isdigit() else None}


def core_ok(boards):
    return all(boards.get(n, {}).get("status") in OK_STATUSES for n, c in BOARDS.items() if c["core"])


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--trigger", default="manual")
    ap.add_argument("--only-missing", action="store_true")
    args = ap.parse_args(argv)

    t_start = time.monotonic()
    now = datetime.now(timezone.utc)
    stamp = {"utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "sydney": now.astimezone(SYD).strftime("%Y-%m-%d %H:%M:%S %Z"),
             "date": now.astimezone(SYD).date().isoformat(), **run_info()}
    day_dir = HOT_DIR / stamp["date"]
    day_dir.mkdir(parents=True, exist_ok=True)
    day_status_path = day_dir / "status.json"

    previous = {}
    if args.only_missing and day_status_path.exists():
        previous = json.loads(day_status_path.read_text(encoding="utf-8")).get("boards", {})

    fetched, boards, name_repairs = {}, {}, []
    for i, (name, cfg) in enumerate(BOARDS.items()):
        if previous.get(name, {}).get("status") in OK_STATUSES:
            boards[name] = previous[name]          # 当天已成功，保留
            continue
        if fetched:
            time.sleep(1)                          # 各榜之间稍作间隔
        entry, rows = run_board(name, cfg, day_dir, stamp)
        fetched[name] = entry
        if entry.pop("names_only", False) and entry["status"] == "partial":
            name_repairs.append((name, rows, time.monotonic()))
        prev = previous.get(name)
        if entry["status"] == "failed" and prev and prev.get("status") == "partial":
            # 重抓失败时保留之前的部分结果（CSV 未被覆盖）
            entry = {**prev, "retry_error": entry["error"], "retry_at_utc": stamp["utc"]}
        boards[name] = entry
    for name, rows, t_last in name_repairs:     # 其余榜单抓完后再处理，等待时间与之重叠
        boards[name] = fetched[name] = repair_em_names(name, fetched[name], rows, day_dir, stamp,
                                                       t_start, t_last)

    status = {
        "date": stamp["date"],
        "last_run_at_utc": stamp["utc"],
        "last_run_at_sydney": stamp["sydney"],
        "last_trigger": args.trigger,
        "run_id": stamp["run_id"],
        "run_attempt": stamp["run_attempt"],
        "core_ok": core_ok(boards),
        "note": ("公开榜单试跑。core=true 的榜单全部 ok（或 ok_names_cached：名称取自历史文件）"
                 "才算当天成功；港股榜为试探。"),
        "boards": boards,
    }
    body = json.dumps(status, ensure_ascii=False, indent=2) + "\n"
    day_status_path.write_text(body, encoding="utf-8")
    (HOT_DIR / "status.json").write_text(body, encoding="utf-8")
    with (HOT_DIR / "runs.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"date": stamp["date"], "run_at_utc": stamp["utc"],
                            "trigger": args.trigger, "only_missing": args.only_missing,
                            "run_id": stamp["run_id"], "run_attempt": stamp["run_attempt"],
                            "boards": fetched}, ensure_ascii=False) + "\n")

    for name, e in boards.items():
        mark = "(保留)" if name not in fetched else f"{e['seconds']}s"
        print(f"{name:18} {e['status']:8} rows={e['rows']:<4} {mark:8} {e.get('error') or ''}"[:300],
              file=sys.stderr)
    print(f"core_ok={status['core_ok']}", file=sys.stderr)


if __name__ == "__main__":
    main()
