#!/bin/sh
# 决定本次工作流运行是否抓数据。输出 key=value 到 stdout：
#   run=true/false  reason=...  fetch_args=<传给 fetch_market_data.py 的参数>  role=primary|backup|full|force
#
# 用法: schedule_gate.sh <event> <schedule> <mode>
#   event    : github.event_name（schedule / workflow_dispatch ...）
#   schedule : github.event.schedule（触发的 cron 字符串；非定时为空）
#   mode     : 手动触发时的 mode 输入（full / backup / force），定时触发忽略
# 需在仓库根目录运行（要读 data/）。
# 测试用覆盖变量：NY_OFFSET、NY_WEEKDAY、NY_DATE、NY_TIME（HH:MM）。
#
# 定时表（纽约时间 → UTC）：
#   主任务 16:17  → 20:17 (EDT, -0400) / 21:17 (EST, -0500)
#   备用   16:47  → 20:47 (EDT, -0400) / 21:47 (EST, -0500)
# 每个 cron 只在对应的 UTC 偏移下生效，另一个跳过；所以按偏移而非钟点判断。
#
# 规则（force 以外全部适用）：
#   1. 纽约 18:00 以后不抓：期货在 18:00 开下一交易时段、汇率和亚太指数也已进入次日，
#      此时抓到的涨跌和日期不再属于当天。GitHub 定时常迟到数小时，这条挡住迟到的运行。
#   2. 当天已有完整数据就跳过（防止覆盖）：data/<纽约今天>.json 存在且 complete=true，
#      或 latest.json 是纽约今天 16:00 后生成且 complete=true（美股假日时文件名是上一交易日）。
#   force：不做以上检查，并允许覆盖已有的完整文件（fetch_args=--force）。
set -eu
event=$1; schedule=${2:-}; mode=${3:-full}

offset=${NY_OFFSET:-$(TZ=America/New_York date +%z)}
weekday=${NY_WEEKDAY:-$(TZ=America/New_York date +%u)}   # 1=周一 ... 7=周日
today=${NY_DATE:-$(TZ=America/New_York date +%F)}
now=${NY_TIME:-$(TZ=America/New_York date +%H:%M)}

role=
out() { echo "run=$1"; echo "reason=$2"; echo "fetch_args=${3:-}"; echo "role=$role"; exit 0; }

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
  case "$mode" in
    full|backup|force) role=$mode ;;
    *) out false "unknown mode '$mode'" ;;
  esac
fi

[ "$role" = "force" ] && out true "force: checks skipped, may overwrite" "--force"

# 规则 1：18:00 保护
hhmm=$(echo "$now" | tr -d :)
if [ "$hhmm" -ge 1800 ]; then
  out false "$role: after 18:00 New York ($now), futures/FX/Asia have rolled to the next session; use mode=force to override"
fi

# 规则 2：当天已有完整数据
if [ -f "data/$today.json" ] && python3 -c "import json,sys; sys.exit(0 if json.load(open('data/$today.json')).get('complete') else 1)"; then
  out false "$role: data/$today.json already exists and complete=true"
fi
if [ -f data/latest.json ] && python3 - "$today" <<'PY'
import json, sys
d = json.load(open("data/latest.json"))
ts = str(d.get("generated_at_new_york", ""))[:16]   # "YYYY-MM-DD HH:MM"
sys.exit(0 if d.get("complete") and ts[:10] == sys.argv[1] and ts[11:] >= "16:00" else 1)
PY
then
  out false "$role: latest.json already generated after close on $today (New York) and complete=true"
fi
out true "$role: no complete data for $today yet"
