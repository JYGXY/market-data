#!/bin/sh
# scripts/schedule_gate.sh 的测试。在临时目录里构造 data/，不动仓库数据。
# 用法: sh scripts/test_schedule_gate.sh   （全部通过退出 0）
set -eu
GATE=$(cd "$(dirname "$0")" && pwd)/schedule_gate.sh
fail=0; n=0

# check <期望 run> <期望 reason 片段> <offset> <weekday> <date> <event> <schedule> [mode]
check() {
  want=$1 frag=$2; shift 2
  n=$((n + 1))
  got=$(NY_OFFSET=$1 NY_WEEKDAY=$2 NY_DATE=$3 sh "$GATE" "$4" "$5" "${6:-full}")
  run=$(echo "$got" | sed -n 's/^run=//p'); reason=$(echo "$got" | sed -n 's/^reason=//p')
  if [ "$run" = "$want" ] && echo "$reason" | grep -qF -- "$frag"; then
    printf 'ok   %-6s %-22s %s\n' "$run" "$5" "$reason"
  else
    printf 'FAIL want run=%s (~%s), got run=%s reason=%s  [%s]\n' "$want" "$frag" "$run" "$reason" "$*"
    fail=$((fail + 1))
  fi
}
# setup <文件名或空> <latest.json 的 generated_at_new_york 或空>
setup() {
  rm -rf data; mkdir data
  [ -z "$1" ] || echo '{}' > "data/$1"
  [ -z "$2" ] || printf '{"generated_at_new_york": "%s"}\n' "$2" > data/latest.json
}

tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT; cd "$tmp"

echo "# 主任务：只在对应的 UTC 偏移、工作日放行"
setup "" ""
check true  "primary run"         -0400 5 2026-09-25 schedule "17 20 * * 1-5"
check false "primary cron for -0500" -0400 5 2026-09-25 schedule "17 21 * * 1-5"
check false "primary cron for -0400" -0500 5 2026-11-27 schedule "17 20 * * 1-5"
check true  "primary run"         -0500 5 2026-11-27 schedule "17 21 * * 1-5"
check false "weekend"             -0400 6 2026-09-26 schedule "17 20 * * 1-5"
check false "weekend"             -0500 7 2026-11-29 schedule "17 21 * * 1-5"

echo "# 备用：当天没数据 -> 补跑"
setup "2026-09-25.json" "2026-09-25 16:18 EDT"
check true  "running catch-up"    -0400 1 2026-09-28 schedule "47 20 * * 1-5"
check false "backup cron for -0500" -0400 1 2026-09-28 schedule "47 21 * * 1-5"
check true  "running catch-up"    -0500 1 2026-11-30 schedule "47 21 * * 1-5"
check false "backup cron for -0400" -0500 1 2026-11-30 schedule "47 20 * * 1-5"
check false "weekend"             -0400 6 2026-09-26 schedule "47 20 * * 1-5"

echo "# 备用：当天文件已存在 -> 跳过"
setup "2026-09-28.json" "2026-09-28 16:18 EDT"
check false "data/2026-09-28.json already exists" -0400 1 2026-09-28 schedule "47 20 * * 1-5"

echo "# 备用：美股假日（文件名是上一交易日，latest 是今天收盘后生成）-> 跳过"
setup "2026-11-25.json" "2026-11-26 16:18 EST"
check false "generated after close on 2026-11-26" -0500 4 2026-11-26 schedule "47 21 * * 1-5"

echo "# 备用：今天只有收盘前的手动运行 -> 仍补跑"
setup "2026-09-25.json" "2026-09-28 10:05 EDT"
check true  "running catch-up"    -0400 1 2026-09-28 schedule "47 20 * * 1-5"

echo "# 旧的 cron 字符串不再放行"
setup "" ""
check false "unknown schedule"    -0400 5 2026-09-25 schedule "20 20 * * 1-5"
check false "unknown schedule"    -0400 5 2026-09-25 schedule "13 21 * * 1-5"

echo "# 手动触发"
setup "2026-09-25.json" "2026-09-25 19:22 EDT"
check true  "full run"            -0400 5 2026-09-25 workflow_dispatch "" full
check false "already exists"      -0400 5 2026-09-25 workflow_dispatch "" backup
check true  "running catch-up"    -0400 1 2026-09-28 workflow_dispatch "" backup

echo "$((n - fail))/$n passed"
[ "$fail" -eq 0 ]
