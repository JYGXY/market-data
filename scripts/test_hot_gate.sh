#!/bin/sh
# scripts/hot_gate.sh 的测试。在临时目录里构造 data/hot/，不动仓库数据。
set -eu
GATE=$(cd "$(dirname "$0")" && pwd)/hot_gate.sh
fail=0; n=0

# check <期望 run> <期望 args> <reason 片段> <offset> <date> <event> <schedule> [mode]
check() {
  want=$1 wargs=$2 frag=$3; shift 3
  n=$((n + 1))
  got=$(SYD_OFFSET=$1 SYD_DATE=$2 sh "$GATE" "$3" "$4" "${5:-full}")
  run=$(echo "$got" | sed -n 's/^run=//p'); args=$(echo "$got" | sed -n 's/^args=//p')
  reason=$(echo "$got" | sed -n 's/^reason=//p')
  if [ "$run" = "$want" ] && [ "$args" = "$wargs" ] && echo "$reason" | grep -qF -- "$frag"; then
    printf 'ok   %-6s %-14s %-16s %s\n' "$run" "$4" "$args" "$reason"
  else
    printf 'FAIL want run=%s args=%s (~%s), got run=%s args=%s reason=%s [%s]\n' \
      "$want" "$wargs" "$frag" "$run" "$args" "$reason" "$*"
    fail=$((fail + 1))
  fi
}
# setup <date 或空> <core_ok: true/false>
setup() {
  rm -rf data; mkdir -p data/hot
  [ -z "$1" ] || { mkdir -p "data/hot/$1"; printf '{"core_ok": %s}\n' "$2" > "data/hot/$1/status.json"; }
}

tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT; cd "$tmp"

echo "# 主运行：只在对应偏移放行（AEST +1000 / AEDT +1100）"
setup "" ""
check true  ""  "primary: no run"        +1000 2026-09-28 schedule "37 21 * * *"
check false ""  "primary cron for +1100" +1000 2026-09-28 schedule "37 20 * * *"
check true  ""  "primary: no run"        +1100 2026-10-05 schedule "37 20 * * *"
check false ""  "primary cron for +1000" +1100 2026-10-05 schedule "37 21 * * *"

echo "# 主运行也防覆盖：当天已成功 -> 跳过；已有但未全部成功 -> 只补抓"
setup 2026-09-28 true
check false ""  "already succeeded today" +1000 2026-09-28 schedule "37 21 * * *"
setup 2026-09-28 false
check true  "--only-missing" "core_ok=false" +1000 2026-09-28 schedule "37 21 * * *"

echo "# 备用：当天没有运行 -> 全部抓；有但未成功 -> 只补抓；已成功 -> 跳过"
setup "" ""
check true  ""  "backup: no run"         +1000 2026-09-28 schedule "22 22 * * *"
check true  ""  "backup: no run"         +1100 2026-10-05 schedule "22 21 * * *"
check false ""  "backup cron for +1100"  +1000 2026-09-28 schedule "22 21 * * *"
check false ""  "backup cron for +1000"  +1100 2026-10-05 schedule "22 22 * * *"
setup 2026-09-28 false
check true  "--only-missing" "core_ok=false" +1000 2026-09-28 schedule "22 22 * * *"
setup 2026-09-28 true
check false ""  "already succeeded today" +1000 2026-09-28 schedule "22 22 * * *"
echo "# 只有前一天成功 -> 当天照抓"
setup 2026-09-27 true
check true  ""  "backup: no run"         +1000 2026-09-28 schedule "22 22 * * *"

echo "# 未知 cron（美股任务的定时、旧的 08:37 / 08:52 定时）"
setup "" ""
check false ""  "unknown schedule"       +1000 2026-09-28 schedule "17 20 * * 1-5"
check false ""  "unknown schedule"       +1000 2026-09-28 schedule "37 22 * * *"
check false ""  "unknown schedule"       +1100 2026-10-05 schedule "52 21 * * *"

echo "# 手动触发（外部触发用 full）"
setup "" ""
check true  ""  "manual: no run"         +1000 2026-09-30 workflow_dispatch "" full
setup 2026-09-30 false
check true  "--only-missing" "core_ok=false" +1000 2026-09-30 workflow_dispatch "" full
setup 2026-09-30 true
check false ""  "already succeeded today" +1000 2026-09-30 workflow_dispatch "" full
check false ""  "already succeeded today" +1000 2026-09-30 workflow_dispatch "" backup
check true  ""  "force: checks skipped"  +1000 2026-09-30 workflow_dispatch "" force
check false ""  "unknown mode"           +1000 2026-09-30 workflow_dispatch "" bogus

echo "$((n - fail))/$n passed"
[ "$fail" -eq 0 ]
