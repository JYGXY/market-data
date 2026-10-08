#!/usr/bin/env python3
"""每日公开市场行情抓取。

只抓公开的指数、利率、商品、汇率、按市值排名的美股前 30 公司，
以及 S&P/ASX 300 指数的全部成分股（名单见 data/asx300_constituents.json，每季度更新）。
不接受任何外部清单输入，不抓任何自选个股，也不单独标注或排序任何个股。

输出: data/YYYY-MM-DD.json（纽约日期）和 data/latest.json

用法: python3 scripts/fetch_market_data.py [--force]
  默认不覆盖已存在且 complete=true 的当天文件；--force 强制覆盖。
版本标识从环境变量读取（GitHub Actions 自动提供）：GITHUB_RUN_ID、GITHUB_RUN_ATTEMPT，
以及工作流传入的 RUN_TRIGGER（例如 schedule:primary、workflow_dispatch:full）。
"""
import argparse
import csv
import io
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

NY = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
UA = {"User-Agent": "Mozilla/5.0 (compatible; public-market-data-bot/1.0)"}

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=10d&interval=1d"
YAHOO_CHART_1H = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=5d&interval=1h"
YAHOO_PAGE = "https://finance.yahoo.com/quote/{sym}"
YAHOO_SCREENER = ("https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
                  "?scrIds=largest_market_cap&count=100")
YAHOO_SCREENER_PAGE = "https://finance.yahoo.com/research-hub/screener/largest_market_cap/"
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
FRED_PAGE = "https://fred.stlouisfed.org/series/{sid}"

ASX300_FILE = DATA_DIR / "asx300_constituents.json"
ASX300_WORKERS = 4          # 并发请求数，保持克制
ASX300_MIN_PRICED = 0.95    # 有价格的成分股低于这个比例时，整段记为缺失（complete=false）

# 美国主要交易所代码（排除 OTC/粉单）
US_EXCHANGES = {"NMS", "NGM", "NCM", "NYQ", "ASE", "PCX", "BTS"}

# (键, 名称, Yahoo 代码, 单位)
US_INDICES = [
    ("sp500", "标普 500", "^GSPC", "点"),
    ("dow", "道琼斯工业平均", "^DJI", "点"),
    ("nasdaq", "纳斯达克综合", "^IXIC", "点"),
]
ASIA_INDICES = [
    ("asx200", "ASX 200", "^AXJO", "点"),
    ("hang_seng", "恒生指数", "^HSI", "点"),
    ("csi300", "沪深 300", "000300.SS", "点"),
]
COMMODITIES = [
    ("brent", "布伦特原油（近月期货）", "BZ=F", "美元/桶"),
    ("wti", "WTI 原油（近月期货）", "CL=F", "美元/桶"),
    ("gold", "黄金（COMEX 近月期货）", "GC=F", "美元/盎司"),
]
FX = [
    ("aud_usd", "AUD/USD", "AUDUSD=X", "美元/澳元"),
    ("usd_cny", "USD/CNY", "CNY=X", "人民币/美元"),
    ("usd_hkd", "USD/HKD", "HKD=X", "港元/美元"),
]


def get(url, tries=3, timeout=30):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise last


def null_item(name, unit, source, reason, **extra):
    return {"name": name, "value": None, "change": None, "change_pct": None,
            "unit": unit, "date": None, "source": source, "error": reason, **extra}


def yahoo_daily(key, name, sym, unit, closes_only=False):
    """取最近两个有效日收盘，算涨跌。日期按交易所本地时区。

    closes_only=True（指数）：若最新一根日线所在交易时段尚未收盘，丢弃它，
    只用已完成的收盘价。期货/汇率近乎 24 小时交易，取抓取时的最新价。
    """
    api = YAHOO_CHART.format(sym=quote(sym))
    page = YAHOO_PAGE.format(sym=quote(sym))
    try:
        res = get(api).json()["chart"]["result"][0]
        tz = ZoneInfo(res["meta"]["exchangeTimezoneName"])
        ts = res.get("timestamp") or []
        closes = res["indicators"]["quote"][0]["close"]
        bars = [(t, c) for t, c in zip(ts, closes) if c is not None]
        meta = res["meta"]
        note = None
        reg = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
        now = time.time()
        in_session = bool(reg) and reg["start"] <= now < reg["end"] + 900
        # Yahoo 有时最新一根日线的 close 为空，但报价里已有收盘价；
        # 不补的话会悄悄退回上一交易日。盘中（指数）不补。
        rmp, rmt = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
        if (ts and closes and closes[-1] is None and rmp is not None and rmt
                and rmt >= ts[-1] and not (closes_only and in_session)):
            bars.append((rmt, rmp))
            note = "最新日线收盘缺失，取数据源报价中的收盘价"
        if closes_only and bars and in_session and bars[-1][0] >= reg["start"]:
            bars = bars[:-1]
            note = "当日尚未收盘，取上一完整交易日收盘"
        # 最近一个已结束的交易时段（盘中那根不算）。若取到的收盘比它旧，说明中间有一天
        # 数据源只给了空收盘——不处理的话会悄悄退回更早的日期（2026-09-28 ASX 退回 9/25）。
        done_ts = [t for t in ts if not (in_session and t >= reg["start"])]
        stale = False
        if bars and done_ts and max(done_ts) > bars[-1][0]:
            gap_t = max(done_ts)
            gap_day = datetime.fromtimestamp(gap_t, tz).date()
            pc = yahoo_session_close(sym, gap_day) if closes_only else None
            if pc is not None:
                bars.append((gap_t, pc))
                note = f"{gap_day.isoformat()} 日线收盘缺失，取当日最后一根小时线收盘"
            else:
                stale = True
                note = ("数据源缺少 " + datetime.fromtimestamp(gap_t, tz).date().isoformat()
                        + " 的收盘，数值为更早日期，已标记 stale")
        if (meta.get("instrumentType") == "FUTURE" and rmp is not None and rmt
                and meta.get("fulldayChange") is not None):
            # 期货：用报价自带的当日涨跌。近月换月时，日线会把新旧两个合约接在一起，
            # 直接相减会得出虚假的大涨大跌；报价的涨跌是同一合约的。
            last, t_last = rmp, rmt
            prev = rmp - meta["fulldayChange"]
        elif len(bars) >= 2:
            (_, prev), (t_last, last) = bars[-2], bars[-1]
        elif note is None and meta.get("regularMarketPrice") is not None \
                and meta.get("fulldayChange") is not None:
            # 部分代码（如沪深 300）日线历史不全，改用数据源自带的最新价与当日涨跌
            last, t_last = meta["regularMarketPrice"], meta["regularMarketTime"]
            prev = last - meta["fulldayChange"]
            note = "日线历史不全，改用数据源报价中的最新收盘与涨跌"
        else:
            return null_item(name, unit, page, "数据源返回的有效收盘不足两天", symbol=sym)
        item = {
            "name": name, "symbol": sym,
            "value": round(last, 4),
            "change": round(last - prev, 4),
            "change_pct": round((last / prev - 1) * 100, 3),
            "prev_close": round(prev, 4),
            "unit": unit,
            "date": datetime.fromtimestamp(t_last, tz).date().isoformat(),
            "source": page,
        }
        if unit == "%":
            item["change_bp"] = round((last - prev) * 100, 1)
        if note:
            item["note"] = note
        if stale:
            item["stale"] = True
        return item
    except Exception as e:  # noqa: BLE001
        return null_item(name, unit, page, f"抓取失败: {type(e).__name__}: {e}"[:300], symbol=sym)


def yahoo_session_close(sym, day):
    """某个已结束交易日的收盘：取当天（交易所时区）最后一根小时线的收盘。

    不用 chartPreviousClose：实测它的含义随 range 变化（2026-09-30 ^AXJO 的 1d/2d/5d
    分别给出 8665.0 / 8679.7 / 8765.3），不可靠。取不到返回 None。
    """
    try:
        res = get(YAHOO_CHART_1H.format(sym=quote(sym))).json()["chart"]["result"][0]
        tz = ZoneInfo(res["meta"]["exchangeTimezoneName"])
        pts = [c for t, c in zip(res.get("timestamp") or [], res["indicators"]["quote"][0]["close"])
               if c is not None and datetime.fromtimestamp(t, tz).date() == day]
        return round(pts[-1], 4) if pts else None
    except Exception:  # noqa: BLE001
        return None


def fred_latest(sid, name, unit):
    page = FRED_PAGE.format(sid=sid)
    try:
        text = get(FRED_CSV.format(sid=sid), tries=4, timeout=60).text
        rows = [r for r in csv.reader(io.StringIO(text))][1:]
        rows = [(d, v) for d, v in rows if v not in ("", ".")]
        if len(rows) < 2:
            return null_item(name, unit, page, "FRED 返回的有效观测不足两期", series=sid)
        (_, prev), (d_last, last) = rows[-2], rows[-1]
        prev, last = float(prev), float(last)
        return {
            "name": name, "series": sid,
            "value": last,
            "change": round(last - prev, 4),
            "change_pct": None,
            "unit": unit,
            "date": d_last,
            "source": page,
            "note": "FRED 发布通常滞后 1 个交易日；change 为相邻两期差值",
        }
    except Exception as e:  # noqa: BLE001
        return null_item(name, unit, page, f"抓取失败: {type(e).__name__}: {e}"[:300], series=sid)


def us_top30():
    try:
        quotes = get(YAHOO_SCREENER).json()["finance"]["result"][0]["quotes"]
    except Exception as e:  # noqa: BLE001
        return {"source": YAHOO_SCREENER_PAGE, "items": None,
                "error": f"抓取失败: {type(e).__name__}: {e}"[:300]}
    out, seen = [], set()
    for q in sorted(quotes, key=lambda q: q.get("marketCap") or 0, reverse=True):
        if q.get("exchange") not in US_EXCHANGES or q.get("quoteType") != "EQUITY":
            continue
        company = (q.get("longName") or q.get("shortName") or q["symbol"]).strip()
        if company in seen:  # 同一公司多类股（如 GOOGL/GOOG），只保留市值靠前的一类
            continue
        seen.add(company)
        t = q.get("regularMarketTime")
        price, chg, pct = (q.get("regularMarketPrice"), q.get("regularMarketChange"),
                           q.get("regularMarketChangePercent"))
        out.append({
            "rank": len(out) + 1,
            "symbol": q["symbol"],
            "name": company,
            "value": price,
            "change": round(chg, 4) if chg is not None else None,
            "change_pct": round(pct, 3) if pct is not None else None,
            "market_cap_usd": q.get("marketCap"),
            "date": datetime.fromtimestamp(t, NY).date().isoformat() if t else None,
            "source": YAHOO_PAGE.format(sym=quote(q["symbol"])),
            **({} if price is not None else {"error": "数据源未返回价格"}),
        })
        if len(out) == 30:
            break
    return {"source": YAHOO_SCREENER_PAGE, "items": out,
            "note": "按 Yahoo 实时市值排序，仅限美国主要交易所上市股票（含 ADR），同一公司多类股只计一次"}


def load_asx300_constituents(path=None):
    """读取成分股名单文件；返回 (doc, None) 或 (None, 原因)。"""
    path = path or ASX300_FILE
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        codes = [c["code"] for c in doc["constituents"]]
        if not codes:
            return None, "成分股名单为空"
        return doc, None
    except FileNotFoundError:
        return None, f"缺少成分股名单文件 {Path(path).name}"
    except (ValueError, KeyError, TypeError) as e:
        return None, f"成分股名单文件格式错误: {type(e).__name__}: {e}"[:300]


def asx_daily_bars(code):
    """一只 ASX 股票最近的已完成日线：返回 [(本地日期, 收盘, 成交量)]，按日期升序。

    盘中那根不算；同一天多根（盘中开盘那根 + 实时那根）取最后一个非空收盘、最大成交量。
    成交量为 None（数据源未给）时视为有成交。
    """
    res = get(YAHOO_CHART.format(sym=quote(code + ".AX"))).json()["chart"]["result"][0]
    meta = res["meta"]
    tz = ZoneInfo(meta["exchangeTimezoneName"])
    q = res["indicators"]["quote"][0]
    reg = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    now = time.time()
    in_session = bool(reg) and reg["start"] <= now < reg["end"] + 900
    days = {}
    for t, c, v in zip(res.get("timestamp") or [], q.get("close") or [], q.get("volume") or []):
        if in_session and t >= reg["start"]:
            continue
        d = datetime.fromtimestamp(t, tz).date().isoformat()
        pc, pv = days.get(d, (None, None))
        days[d] = (c if c is not None else pc,
                   v if pv is None else (pv if v is None else max(pv, v)))
    return [(d, c, v) for d, (c, v) in sorted(days.items())]


def asx_item(code, name, bars, session):
    """把日线整理成一项。session 是本次的参考交易日（大多数成分股的最新成交日）。"""
    page = YAHOO_PAGE.format(sym=quote(code + ".AX"))
    traded = [(d, c) for d, c, v in bars if c is not None and (v is None or v > 0)]
    item = {"symbol": code, "name": name, "value": None, "prev_close": None,
            "change": None, "change_pct": None, "date": session, "source": page}
    today = [c for d, c in traded if d == session]
    before = [(d, c) for d, c in traded if d < session]
    if today and before:
        last, prev = today[-1], before[-1][1]
        item.update(value=round(last, 4), prev_close=round(prev, 4),
                    change=round(last - prev, 4), change_pct=round((last / prev - 1) * 100, 3))
        return item
    if today:  # 有成交，只是缺前一成交日（如新上市）：给收盘价，不算 stale
        item["value"] = round(today[-1], 4)
        item["note"] = f"数据源缺少 {session} 之前的成交日，无法计算涨跌"
        return item
    # 停牌或当日无成交：不填 0，value 为 null，标 stale 并写原因；附上最后成交日供参考
    item["stale"] = True
    bar = next(((c, v) for d, c, v in bars if d == session), None)
    last = before[-1] if before else None
    why = (f"{session} 无成交（成交量为 0）" if bar and bar[1] == 0
           else f"{session} 无交易数据（可能停牌）")
    item["note"] = why + (f"；最后成交日 {last[0]}，收盘 {round(last[1], 4)}" if last else "")
    if last:
        item["last_trade_date"], item["last_close"] = last[0], round(last[1], 4)
    return item


def asx300():
    doc, err = load_asx300_constituents()
    base = {"index": "S&P/ASX 300", "items": None,
            "note": "指数全部成分股，按代码字母顺序排列；价格为最近一个已收盘的 ASX 交易日（date）"}
    if err:
        return {**base, "error": err}
    base.update(constituents_as_of=doc.get("as_of"), constituents_source=doc.get("source"))
    names = {c["code"]: c.get("name") or c["code"] for c in doc["constituents"]}

    def one(code):
        try:
            return code, asx_daily_bars(code), None
        except Exception as e:  # noqa: BLE001
            return code, None, f"抓取失败: {type(e).__name__}: {e}"[:300]

    with ThreadPoolExecutor(ASX300_WORKERS) as ex:
        results = list(ex.map(one, sorted(names)))
    # 参考交易日：各成分股最新成交日的众数（个别停牌股不影响）
    latest = [next((d for d, c, v in reversed(b) if c is not None and (v is None or v > 0)), None)
              for _, b, _ in results if b]
    latest = [d for d in latest if d]
    session = Counter(latest).most_common(1)[0][0] if latest else None
    items = []
    for code, bars, ferr in results:
        if ferr or session is None:
            items.append({"symbol": code, "name": names[code], "value": None, "prev_close": None,
                          "change": None, "change_pct": None, "date": session,
                          "source": YAHOO_PAGE.format(sym=quote(code + ".AX")), "stale": True,
                          "note": ferr or "数据源未返回有效日线", "error": ferr or "数据源未返回有效日线"})
        else:
            items.append(asx_item(code, names[code], bars, session))
    return {**base, "session_date": session, "items": items}


def build():
    now = datetime.now(timezone.utc)
    return {
        "generated_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_at_new_york": now.astimezone(NY).strftime("%Y-%m-%d %H:%M %Z"),
        "disclaimer": "仅含公开市场数据。取不到的值为 null，并在 error 字段注明原因。",
        "us_indices": {k: yahoo_daily(k, n, s, u, True) for k, n, s, u in US_INDICES},
        "us_top30_by_market_cap": us_top30(),
        "rates": {
            "us_10y_yield": yahoo_daily("us_10y_yield", "美国 10 年期国债收益率", "^TNX", "%"),
            "ig_credit_spread": fred_latest(
                "BAMLC0A0CM", "ICE BofA 美国投资级公司债期权调整利差 (OAS)", "百分点"),
        },
        "commodities": {k: yahoo_daily(k, n, s, u) for k, n, s, u in COMMODITIES},
        "fx": {k: yahoo_daily(k, n, s, u) for k, n, s, u in FX},
        "asia_pacific_indices": {k: yahoo_daily(k, n, s, u, True) for k, n, s, u in ASIA_INDICES},
        "asx300": asx300(),
    }


def missing_items(data):
    """列出所有取不到数值的项；空列表表示全部取到。"""
    missing = []
    for sec in ("us_indices", "rates", "commodities", "fx", "asia_pacific_indices"):
        missing += [f"{sec}.{k}" for k, v in data[sec].items() if v.get("value") is None]
    top = data["us_top30_by_market_cap"]["items"]
    if top is None:
        missing.append("us_top30_by_market_cap")
    else:
        if len(top) < 30:
            missing.append(f"us_top30_by_market_cap (只取到 {len(top)} 家)")
        missing += [f"us_top30_by_market_cap.{i['symbol']}" for i in top if i["value"] is None]
    # ASX 300：个股停牌/无成交/取不到记在 stale；整段取不到或有价格的不足 95% 才算缺失
    asx = data.get("asx300")
    if asx is not None:
        items = asx.get("items")
        if not items:
            missing.append("asx300")
        else:
            priced = sum(1 for i in items if i["value"] is not None)
            if priced < ASX300_MIN_PRICED * len(items):
                missing.append(f"asx300 (只取到 {priced}/{len(items)} 只的价格)")
    return missing


def stale_entries(data):
    """数值不是最近一个已结束交易时段的项，附原因。不影响 complete。

    返回 [(路径, 原因)]：指数/商品/汇率是数据源缺收盘；ASX 300 个股是停牌、当日无成交或取不到。
    """
    out = [(f"{sec}.{k}", v.get("note") or "")
           for sec in ("us_indices", "rates", "commodities", "fx", "asia_pacific_indices")
           for k, v in data[sec].items() if v.get("stale")]
    out += [(f"asx300.{i['symbol']}", i.get("note") or "")
            for i in (data.get("asx300") or {}).get("items") or [] if i.get("stale")]
    return out


def stale_items(data):
    return [p for p, _ in stale_entries(data)]


def run_info():
    """版本标识：下游引用时写上 run_id，就能分清是哪一次运行生成的。本地运行时为 null。"""
    rid = os.environ.get("GITHUB_RUN_ID")
    return {"run_id": int(rid) if rid and rid.isdigit() else None,
            "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"])
            if os.environ.get("GITHUB_RUN_ATTEMPT", "").isdigit() else None,
            "run_trigger": os.environ.get("RUN_TRIGGER") or ("github-actions" if rid else "local")}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="覆盖已存在且 complete=true 的当天文件")
    args = ap.parse_args(argv)

    data = build()
    DATA_DIR.mkdir(exist_ok=True)
    # 交易日取美股指数的最新收盘日期；三个都取不到时退回纽约当天日期（此时 complete 必为 false）。
    day = next((v["date"] for v in data["us_indices"].values() if v["date"]),
               datetime.now(NY).date().isoformat())
    # 不覆盖已有的完整文件（美股假日时 day 是上一交易日，同样受保护），除非 --force
    target = DATA_DIR / f"{day}.json"
    if target.exists() and not args.force:
        try:
            old = json.loads(target.read_text(encoding="utf-8"))
        except ValueError:
            old = {}
        if old.get("complete"):
            print(f"data/{day}.json already complete (run_id={old.get('run_id')}); not overwriting. "
                  "Use --force to override.", file=sys.stderr)
            return
    missing = missing_items(data)
    stale = stale_entries(data)
    data = {"trading_date": day, "complete": not missing, "missing": missing,
            "stale": [p for p, _ in stale], "stale_reasons": dict(stale), **run_info(), **data}
    body = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    target.write_text(body, encoding="utf-8")
    (DATA_DIR / "latest.json").write_text(body, encoding="utf-8")
    print(f"wrote data/{day}.json and data/latest.json (complete={not missing}, "
          f"stale={len(data['stale'])}, run_id={data['run_id']})", file=sys.stderr)

if __name__ == "__main__":
    main()
