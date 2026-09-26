#!/bin/sh
# 热榜抓取的定时门控（写法同 schedule_gate.sh）。输出 key=value 到 stdout：
#   run=true/false  reason=...  trigger=primary|backup|manual  args=<传给 fetch_hot_lists.py 的参数>
#
# 用法: hot_gate.sh <event> <schedule> <mode>
#   event    : github.event_name
#   schedule : github.event.schedule（触发的 cron 字符串；非定时为空）
#   mode     : 手动触发时的 mode 输入（full / backup），定时触发忽略
# 需在仓库根目录运行（备用检查要读 data/hot/）。测试用覆盖变量：SYD_OFFSET、SYD_DATE。
#
# 定时表（悉尼时间，每天 → UTC 前一天）：
#   主运行 08:37 → 22:37 (AEST, +1000) / 21:37 (AEDT, +1100)
#   备用   08:52 → 22:52 (AEST, +1000) / 21:52 (AEDT, +1100)
# 悉尼 2026-10-04 起进入夏令时。每个 cron 只在对应的 UTC 偏移下生效，另一个跳过。
set -eu
event=$1; schedule=${2:-}; mode=${3:-full}

offset=${SYD_OFFSET:-$(TZ=Australia/Sydney date +%z)}
today=${SYD_DATE:-$(TZ=Australia/Sydney date +%F)}

out() { echo "run=$1"; echo "reason=$2"; echo "trigger=$3"; echo "args=$4"; exit 0; }

if [ "$event" = "schedule" ]; then
  case "$schedule" in
    "37 22 * * *") role=primary; want=+1000 ;;
    "37 21 * * *") role=primary; want=+1100 ;;
    "52 22 * * *") role=backup;  want=+1000 ;;
    "52 21 * * *") role=backup;  want=+1100 ;;
    *) out false "unknown schedule '$schedule'" none "" ;;
  esac
  [ "$offset" = "$want" ] || out false "$role cron for $want, Sydney is $offset" "$role" ""
else
  role=$mode
  [ "$role" = "full" ] && role=manual
fi

if [ "$role" = "backup" ]; then
  # 主运行成功（当天 status.json 的 core_ok 为 true）就跳过；否则只重抓未成功的榜单
  st="data/hot/$today/status.json"
  if [ -f "$st" ] && python3 -c "import json,sys; sys.exit(0 if json.load(open('$st')).get('core_ok') else 1)"; then
    out false "backup: $st core_ok=true, primary succeeded" backup ""
  fi
  out true "backup: no successful run for $today yet" backup "--only-missing"
fi
out true "$role run" "$role" ""
