#!/usr/bin/env python3
"""热榜试跑报告：统计 data/hot/runs.jsonl 和每天的 status.json。

用法: python3 scripts/hot_trial_report.py [--from YYYY-MM-DD] [--to YYYY-MM-DD]
输出 Markdown 到 stdout。日期为悉尼日期。
"""
import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

HOT_DIR = Path(__file__).resolve().parent.parent / "data" / "hot"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to", dest="end")
    args = ap.parse_args()

    runs = []
    runs_path = HOT_DIR / "runs.jsonl"
    if runs_path.exists():
        for line in runs_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                runs.append(json.loads(line))
    in_range = lambda d: (not args.start or d >= args.start) and (not args.end or d <= args.end)
    runs = [r for r in runs if in_range(r["date"])]
    days = sorted({r["date"] for r in runs})

    # 每天最终状态（当天 status.json）
    final = {}
    for d in days:
        p = HOT_DIR / d / "status.json"
        if p.exists():
            final[d] = json.loads(p.read_text(encoding="utf-8"))

    per = defaultdict(lambda: {"fetches": 0, "ok": 0, "secs": [], "ok_secs": [],
                               "kinds": Counter(), "errors": [], "primary": 0, "primary_ok": 0})
    triggers = Counter()
    for r in runs:
        triggers[r["trigger"]] += 1
        for name, b in r["boards"].items():
            s = per[name]
            s["fetches"] += 1
            s["secs"].append(b["seconds"])
            if b["status"] == "ok":
                s["ok"] += 1
                s["ok_secs"].append(b["seconds"])
            else:
                s["kinds"][b.get("error_kind") or "?"] += 1
                s["errors"].append(f"{r['date']} {r['trigger']}: {(b.get('error') or '')[:160]}")
            if r["trigger"] == "primary":
                s["primary"] += 1
                s["primary_ok"] += b["status"] == "ok"

    avg = lambda xs: f"{sum(xs) / len(xs):.1f}s" if xs else "-"
    print(f"# 热榜试跑报告（{days[0] if days else '-'} ~ {days[-1] if days else '-'}，悉尼日期）\n")
    print(f"- 有运行记录的天数：{len(days)}；运行次数：" +
          "，".join(f"{k} {v}" for k, v in sorted(triggers.items())))
    core_days = sum(1 for d in days if final.get(d, {}).get("core_ok"))
    print(f"- 核心榜单当天全部成功的天数：{core_days}/{len(days)}\n")
    print("| 榜单 | 当天最终成功 | 主运行成功率 | 单次抓取成功率 | 平均耗时（全部/成功） | 失败类型 |")
    print("|---|---|---|---|---|---|")
    names = sorted(per, key=lambda n: (not n.startswith("em"), n.endswith("_hk"), n))
    for n in names:
        s = per[n]
        day_ok = sum(1 for d in days if final.get(d, {}).get("boards", {}).get(n, {}).get("status") == "ok")
        prim = f"{s['primary_ok']}/{s['primary']}" if s["primary"] else "-"
        kinds = "，".join(f"{k}×{v}" for k, v in s["kinds"].most_common()) or "无"
        print(f"| {n} | {day_ok}/{len(days)} | {prim} | {s['ok']}/{s['fetches']} | "
              f"{avg(s['secs'])} / {avg(s['ok_secs'])} | {kinds} |")
    print("\n失败类型：timeout 超时；connection 连接被拒/重置（常见于海外 IP 被拦截）；"
          "http_4xx 被拒绝访问；http_5xx 服务端错误；api_error 接口返回错误码；"
          "bad_json 返回的不是数据（如验证页）；empty 数据为空或不全。\n")
    for n in names:
        if per[n]["errors"]:
            print(f"## {n} 失败明细\n")
            for e in per[n]["errors"]:
                print(f"- {e}")
            print()


if __name__ == "__main__":
    main()
