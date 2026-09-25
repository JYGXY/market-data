#!/bin/sh
# 决定本次工作流运行是否抓数据。输出一行 key=value 到 stdout（run=true/false, reason=...）。
#
# 用法: schedule_gate.sh <event> <schedule> <mode>
#   event    : github.event_name（schedule / workflow_dispatch ...）
#   schedule : github.event.schedule（触发的 cron 字符串；非定时为空）
#   mode     : 手动触发时的 mode 输入（full / backup），定时触发忽略
# 需在仓库根目录运行（备用检查要读 data/）。
# 测试用覆盖变量：NY_OFFSET、NY_WEEKDAY、NY_DATE。
#
# 定时表（纽约时间 → UTC）：
#   主任务 16:17  → 20:17 (EDT, -0400) / 21:17 (EST, -0500)
#   备用   16:47  → 20:47 (EDT, -0400) / 21:47 (EST, -0500)
# 每个 cron 只在对应的 UTC 偏移下生效，另一个跳过；所以按偏移而非钟点判断，
# GitHub 延迟触发也不会重复或错位。
set -eu
event=$1; schedule=${2:-}; mode=${3:-full}

offset=${NY_OFFSET:-$(TZ=America/New_York date +%z)}
weekday=${NY_WEEKDAY:-$(TZ=America/New_York date +%u)}   # 1=周一 ... 7=周日
today=${NY_DATE:-$(TZ=America/New_York date +%F)}

out() { echo "run=$1"; echo "reason=$2"; exit 0; }

if [ "$event" = "schedule" ]; then
  case "$schedule" in
    "17 20 * * 1-5") role=primary; want=-0400 ;;
    "17 21 * * 1-5") role=primary; want=-0500 ;;
    "47 20 * * 1-5") role=backup;  want=-0400 ;;
    "47 21 * * 1-5") role=backup;  want=-0500 ;;
    *) out false "unknown schedule '$schedule'" ;;
  esac
  [ "$weekday" -le 5 ] || out false "weekend in New York (weekday=$weekday)"
  [ "$offset" = "$want" ] || out false "$role cron for $want, New York is $offset"
else
  role=$mode
fi

if [ "$role" = "backup" ]; then
  # 当天已生成：data/<纽约今天>.json 存在，或 latest.json 是纽约今天 16:00 收盘后生成的
  # （美股假日时文件名是上一交易日）。收盘前的手动运行不算，否则会挡住补跑。
  if [ -f "data/$today.json" ]; then
    out false "backup: data/$today.json already exists"
  fi
  if [ -f data/latest.json ] && python3 - "$today" <<'PY'
import json, sys
d = json.load(open("data/latest.json"))
ts = str(d.get("generated_at_new_york", ""))[:16]   # "YYYY-MM-DD HH:MM"
sys.exit(0 if ts[:10] == sys.argv[1] and ts[11:] >= "16:00" else 1)
PY
  then
    out false "backup: latest.json already generated after close on $today (New York)"
  fi
  out true "backup: no data for $today yet, running catch-up"
fi
out true "$role run"
