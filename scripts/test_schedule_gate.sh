#!/bin/sh
# scripts/schedule_gate.sh 的测试。在临时目录里构造 data/，不动仓库数据。
# 用法: sh scripts/test_schedule_gate.sh   （全部通过退出 0）
set -eu
GATE=$(cd "$(dirname "$0")" && pwd)/schedule_gate.sh
fail=0; n=0

# check <期望 run> <期望 fetch_args> <reason 片段> <offset> <weekday> <date> <time> <event> <schedule> [mode]
check() {
  want=$1 wargs=$2 frag=$3; shift 3
  n=$((n + 1))
  got=$(NY_OFFSET=$1 NY_WEEKDAY=$2 NY_DATE=$3 NY_TIME=$4 sh "$GATE" "$5" "$6" "${7:-full}")
  run=$(echo "$got" | sed -n 's/^run=//p'); reason=$(echo "$got" | sed -n 's/^reason=//p')
  args=$(echo "$got" | sed -n 's/^fetch_args=//p')
  if [ "$run" = "$want" ] && [ "$args" = "$wargs" ] && echo "$reason" | grep -qF -- "$frag"; then
    printf 'ok   %-6s %s %-16s %-8s %s\n' "$run" "$4" "$6" "$args" "$reason"
  else
    printf 'FAIL want run=%s args=%s (~%s), got run=%s args=%s reason=%s  [%s]\n' \
      "$want" "$wargs" "$frag" "$run" "$args" "$reason" "$*"
    fail=$((fail + 1))
  fi
}
# setup <当天文件名或空> <该文件 complete: true/false> <latest.json 的 generated_at_new_york 或空> <latest complete>
setup() {
  rm -rf data; mkdir data
  [ -z "$1" ] || printf '{"complete": %s}\n' "$2" > "data/$1"
  [ -z "$3" ] || printf '{"generated_at_new_york": "%s", "complete": %s}\n' "$3" "${4:-true}" > data/latest.json
}

tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT; cd "$tmp"

echo "# 主任务：只在对应的 UTC 偏移、工作日放行"
setup "" "" "" ""
check true  "" "primary: no complete data"  -0400 5 2026-09-25 16:20 schedule "17 20 * * 1-5"
check false "" "primary cron for -0500"     -0400 5 2026-09-25 16:20 schedule "17 21 * * 1-5"
check false "" "primary cron for -0400"     -0500 5 2026-11-27 16:20 schedule "17 20 * * 1-5"
check true  "" "primary: no complete data"  -0500 5 2026-11-27 16:20 schedule "17 21 * * 1-5"
check false "" "weekend"                    -0400 6 2026-09-26 16:20 schedule "17 20 * * 1-5"
check false "" "weekend"                    -0500 7 2026-11-29 16:20 schedule "17 21 * * 1-5"

echo "# 主任务也防覆盖：当天文件已完整 -> 跳过；不完整 -> 重抓"
setup "2026-09-28.json" true "2026-09-28 16:58 EDT" true
check false "" "complete=true"              -0400 1 2026-09-28 16:20 schedule "17 20 * * 1-5"
setup "2026-09-28.json" false "2026-09-28 16:58 EDT" false
check true  "" "primary: no complete data"  -0400 1 2026-09-28 16:20 schedule "17 20 * * 1-5"

echo "# 18:00 保护：迟到的定时（2026-09-28 20:21、2026-09-29 19:43 的真实情况）不抓"
setup "" "" "2026-09-25 19:22 EDT" true
check false "" "after 18:00"                -0400 1 2026-09-28 20:21 schedule "17 20 * * 1-5"
check false "" "after 18:00"                -0400 2 2026-09-29 19:43 schedule "17 20 * * 1-5"
check false "" "after 18:00"                -0400 2 2026-09-29 18:00 schedule "47 20 * * 1-5"
check true  "" "primary: no complete data"  -0400 2 2026-09-29 17:59 schedule "17 20 * * 1-5"
check false "" "after 18:00"                -0400 2 2026-09-29 19:43 workflow_dispatch "" full
check false "" "after 18:00"                -0400 2 2026-09-29 19:43 workflow_dispatch "" backup

echo "# 备用：当天没数据 -> 补跑"
setup "2026-09-25.json" true "2026-09-25 16:18 EDT" true
check true  "" "backup: no complete data"   -0400 1 2026-09-28 16:47 schedule "47 20 * * 1-5"
check false "" "backup cron for -0500"      -0400 1 2026-09-28 16:47 schedule "47 21 * * 1-5"
check true  "" "backup: no complete data"   -0500 1 2026-11-30 16:47 schedule "47 21 * * 1-5"
check false "" "backup cron for -0400"      -0500 1 2026-11-30 16:47 schedule "47 20 * * 1-5"
check false "" "weekend"                    -0400 6 2026-09-26 16:47 schedule "47 20 * * 1-5"

echo "# 备用：当天文件已完整 -> 跳过"
setup "2026-09-28.json" true "2026-09-28 16:18 EDT" true
check false "" "complete=true"              -0400 1 2026-09-28 16:47 schedule "47 20 * * 1-5"

echo "# 美股假日（文件名是上一交易日，latest 今天收盘后生成且完整）-> 跳过"
setup "2026-11-25.json" true "2026-11-26 16:18 EST" true
check false "" "generated after close on 2026-11-26" -0500 4 2026-11-26 16:47 schedule "47 21 * * 1-5"

echo "# 今天只有收盘前的手动运行 -> 仍抓"
setup "2026-09-25.json" true "2026-09-28 10:05 EDT" true
check true  "" "backup: no complete data"   -0400 1 2026-09-28 16:47 schedule "47 20 * * 1-5"

echo "# 旧的 cron 字符串不再放行"
setup "" "" "" ""
check false "" "unknown schedule"           -0400 5 2026-09-25 16:20 schedule "20 20 * * 1-5"
check false "" "unknown schedule"           -0400 5 2026-09-25 16:20 schedule "13 21 * * 1-5"

echo "# 手动触发"
setup "2026-09-25.json" true "2026-09-25 19:22 EDT" true
check false "" "complete=true"              -0400 5 2026-09-25 16:30 workflow_dispatch "" full
check false "" "complete=true"              -0400 5 2026-09-25 16:30 workflow_dispatch "" backup
check true  "--force" "force"               -0400 5 2026-09-25 16:30 workflow_dispatch "" force
check true  "--force" "force"               -0400 5 2026-09-25 21:00 workflow_dispatch "" force
check true  "" "full: no complete data"     -0400 1 2026-09-28 16:25 workflow_dispatch "" full
check false "" "unknown mode"               -0400 1 2026-09-28 16:25 workflow_dispatch "" bogus

echo "$((n - fail))/$n passed"
[ "$fail" -eq 0 ]
