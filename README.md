# market-data

每日公开市场行情快照。**本仓库只存放公开市场数据**，不含任何个人账户、交易或关注列表信息，
也不抓取任何自选个股的价格。

## 运行方式

**主力是外部触发，GitHub 定时是备份。** 2026-09 实测：GitHub 定时经常迟到 2–4 小时，9/29 起一度约 20 小时完全不触发（社区里自 2026-08-26 起有同类报告）；而 `workflow_dispatch` 几秒内就会启动。

- **外部触发**：仓库外的定时任务在纽约 16:25（交易日）检查 main 上是否已有当天完整文件，没有就触发 `workflow_dispatch`（`mode=full`），约 15 分钟后复查，仍没有再触发一次。每天最多触发 2 次。
- **GitHub 定时（备份）**：`.github/workflows/daily-market-data.yml`，纽约时间周一至周五 16:17 主任务、16:47 备用。GitHub 定时只认 UTC，所以每个任务放两个定时：

  | 任务 | EDT（UTC-4） | EST（UTC-5） |
  |---|---|---|
  | 主任务 | `17 20 * * 1-5` | `17 21 * * 1-5` |
  | 备用 | `47 20 * * 1-5` | `47 21 * * 1-5` |

  `scripts/schedule_gate.sh` 按纽约当前 UTC 偏移只放行对应的那个定时，并核对纽约星期几。

**无论由谁触发，都遵守两条规则**（`scripts/schedule_gate.sh`，`mode=force` 除外）：

1. **不覆盖**：`data/<纽约今天>.json` 已存在且 `complete=true`，或 `latest.json` 是纽约今天 16:00 后生成且 `complete=true`（美股假日时文件名是上一交易日）→ 跳过。抓取脚本本身也不会覆盖已完整的同名文件。
2. **18:00 保护**：纽约 18:00 以后不抓。18:00 起期货进入下一交易时段（涨跌改为相对新结算价）、汇率和亚太指数进入次日，抓到的数值不再属于当天。迟到的 GitHub 定时会被这条挡住。

手动触发（Actions 页面或 API）的 `mode`：

| mode | 行为 |
|---|---|
| `full` / `backup` | 遵守以上两条规则 |
| `force` | 不检查，允许覆盖已完整的文件，18:00 后也会抓。只在人工确认后使用 |

本地运行（默认同样不覆盖已完整的文件，`--force` 强制覆盖）：

```sh
pip install -r requirements.txt
python3 scripts/fetch_market_data.py
```

## 输出文件

| 文件 | 说明 |
|---|---|
| `data/YYYY-MM-DD.json` | 当天快照。文件名即 `trading_date`（美股交易日）。已完整的文件不会被自动覆盖。 |
| `data/latest.json` | 最近一次快照，内容与当天文件相同。 |

## 下游读取须知

读取 `data/latest.json` 时，**先核对 `trading_date`**：

- `trading_date` 不是你预期的当天美股交易日 → **视为未更新**（任务没跑、跑失败或数据源未刷新），不要当作当天行情使用。
- `complete` 为 `false` → 有部分项缺失，缺哪些见 `missing`，对应项的 `error` 写明原因。
- `stale` 不为空 → 这些项的数值来自更早的交易日（数据源缺某天收盘），`note` 写明缺的是哪天。
- 引用数据时写上 `run_id`（例如“行情 2026-09-28，run 36482936264”），就能分清是哪一次运行生成的版本。

```python
import json, datetime, zoneinfo
d = json.load(open("data/latest.json"))
today_ny = datetime.datetime.now(zoneinfo.ZoneInfo("America/New_York")).date().isoformat()
if d["trading_date"] != today_ny:
    raise SystemExit(f"latest.json 未更新：trading_date={d['trading_date']}，今天={today_ny}")
if not d["complete"]:
    print("部分缺失：", d["missing"])
```

（上例按“纽约当天”判断，适用于收盘后读取；周末、美股假日读取时，预期的交易日应是上一交易日。）

## 字段说明

顶层字段：

| 字段 | 含义 |
|---|---|
| `trading_date` | 美股交易日（`YYYY-MM-DD`），取美股指数最新收盘的日期，与文件名相同 |
| `complete` | `true` = 所有项都取到数值（含前 30 家公司）；有任何一项为 `null` 即为 `false` |
| `missing` | 取不到的项列表（如 `rates.ig_credit_spread`），`complete` 为 `true` 时为空 |
| `stale` | 数值不是最近一个已结束交易日的项（数据源缺收盘、补不上时）。不影响 `complete` |
| `run_id` / `run_attempt` | 生成该文件的 GitHub Actions 运行编号和重试次数；本地运行为 `null` |
| `run_trigger` | 触发方式，如 `workflow_dispatch:full`、`schedule:primary`；本地运行为 `local` |
| `generated_at_utc` / `generated_at_new_york` | 抓取时间 |
| `disclaimer` | 说明 |

每个数据项的通用字段：

| 字段 | 含义 |
|---|---|
| `name` | 中文名称 |
| `symbol` / `series` | 数据源代码（Yahoo 代码 / FRED 序列号） |
| `value` | 收盘值（或最新值，见下表） |
| `change` | 与上一交易日相比的变动（绝对值） |
| `change_pct` | 与上一交易日相比的涨跌幅（%） |
| `change_bp` | 仅利率类：变动的基点数 |
| `prev_close` | 上一交易日收盘 |
| `unit` | 单位 |
| `date` | 该数值对应的日期（交易所本地时区） |
| `source` | 来源网址（可在浏览器打开核对） |
| `note` | 补充说明（可选） |
| `stale` | 仅在为 `true` 时出现：数值来自更早的交易日 |
| `error` | 取不到时的原因；此时 `value` 等数值字段为 `null` |

### 各板块与来源

| 板块 / 键 | 内容 | 代码 | 来源 |
|---|---|---|---|
| `us_indices.sp500` | 标普 500 收盘与涨跌 | `^GSPC` | Yahoo Finance |
| `us_indices.dow` | 道琼斯工业平均 | `^DJI` | Yahoo Finance |
| `us_indices.nasdaq` | 纳斯达克综合 | `^IXIC` | Yahoo Finance |
| `us_top30_by_market_cap.items[]` | 美股市值前 30 公司：`rank`、`symbol`、`name`、收盘 `value`、`change`、`change_pct`、`market_cap_usd`、`date` | Yahoo “Largest Market Cap” 筛选器 | Yahoo Finance |
| `rates.us_10y_yield` | 美国 10 年期国债收益率（%） | `^TNX` | Yahoo Finance（CBOE） |
| `rates.ig_credit_spread` | ICE BofA 美国投资级公司债期权调整利差 OAS（百分点） | `BAMLC0A0CM` | FRED（圣路易斯联储） |
| `commodities.brent` | 布伦特原油近月期货（美元/桶） | `BZ=F` | Yahoo Finance |
| `commodities.wti` | WTI 原油近月期货（美元/桶） | `CL=F` | Yahoo Finance |
| `commodities.gold` | COMEX 黄金近月期货（美元/盎司） | `GC=F` | Yahoo Finance |
| `fx.aud_usd` | AUD/USD | `AUDUSD=X` | Yahoo Finance |
| `fx.usd_cny` | USD/CNY（在岸人民币） | `CNY=X` | Yahoo Finance |
| `fx.usd_hkd` | USD/HKD | `HKD=X` | Yahoo Finance |
| `asia_pacific_indices.asx200` | 标普/澳交所 200 | `^AXJO` | Yahoo Finance |
| `asia_pacific_indices.hang_seng` | 恒生指数 | `^HSI` | Yahoo Finance |
| `asia_pacific_indices.csi300` | 沪深 300 | `000300.SS` | Yahoo Finance |

口径说明：

- **指数**只取已完成交易日的收盘；若抓取时某市场仍在交易，自动退回上一完整交易日，并在 `note` 注明。
  纽约 16:20 时亚太市场均已收盘，因此 ASX 200 / 恒指 / 沪深 300 通常是当天（亚太日期）的收盘，日期可能比美股晚一天或因假期更早。
- **期货和汇率**近乎 24 小时交易，`value` 是抓取时的最新价，`change` 相对上一交易日收盘。
  期货的涨跌取数据源报价中**同一合约**的涨跌：近月换月当天，`prev_close` 是新合约的前收，不会把新旧两个合约的价差算成涨跌。
- 数据源偶尔会漏掉最新一根日线的收盘值；此时改用报价中的收盘价，并在 `note` 注明，不会悄悄退回上一交易日。
- **市值前 30**：按 Yahoo 实时市值降序，只计美国主要交易所（NYSE / Nasdaq 等，含 ADR，不含 OTC 粉单）；
  同一公司多类股（如 GOOGL / GOOG、BRK-A / BRK-B）只保留市值靠前的一类。名单每天按市值重新排，只记数字，不记涨跌原因。
- **信用利差**：FRED 通常滞后 1 个交易日发布，`date` 是实际观测日。`change` 为相邻两期差值（百分点）。
- 数据源为公开免费接口，可能延迟或临时不可用；取不到的项写 `null` 并在 `error` 注明原因，不做估算填补。

## 每日热榜（试跑）

工作流 `.github/workflows/hot-lists.yml`，脚本 `scripts/fetch_hot_lists.py`。只抓公开榜单：排名、代码、名称、接口给的热度数值，不抓价格。

### 时间（悉尼）

**主力是外部触发**：仓库外的定时任务在悉尼 07:45 检查当天 `status.json`，`core_ok` 不为 `true` 就触发 `workflow_dispatch`（`mode=full`），约 15 分钟后复查、仍未成功再触发一次，每天最多 2 次。下面的 GitHub 定时是备份。

| GitHub 定时 | 悉尼时间 | AEST（+1000） | AEDT（+1100，2026-10-04 起） |
|---|---|---|---|
| 主运行 | 07:37 | `37 21 * * *` | `37 20 * * *` |
| 备用 | 08:22 | `22 22 * * *` | `22 21 * * *` |

- 下游“甄选”09:05 读取；提前运行，给 GitHub 定时延迟留余量。
- cron 是 UTC 前一天。`scripts/hot_gate.sh` 按悉尼当前 UTC 偏移，只放行对应的那个定时。
- 主运行、备用、手动 `full`/`backup` 规则相同（防覆盖）：当天 `core_ok` 为 `true` 就跳过；已有但未全部成功，只补抓未成功的榜单，已成功的保留不动；当天还没有就全部抓。
- 手动 `mode=force`：不检查，全部重抓。`commit=false` 时只抓取，并在运行摘要里显示结果，不写入仓库。

### 榜单

| 文件名 | 内容 | 行数 | 热度字段 | 核心 |
|---|---|---|---|---|
| `em_popularity_a.csv` | 东方财富人气榜（A 股） | 100 | 接口不提供，留空 | 是 |
| `xq_follow_cn.csv` | 雪球热股榜-关注（沪深） | 50 | `follow` 关注人数 | 是 |
| `xq_tweet_cn.csv` | 雪球热股榜-讨论（沪深） | 50 | `tweet` 讨论数 | 是 |
| `xq_deal_cn.csv` | 雪球热股榜-交易（沪深） | 50 | `deal` 分享交易数 | 是 |
| `xq_{follow,tweet,deal}_hk.csv` | 同上（港股，试探） | 50 | 同上 | 否 |

- CSV 列：`date`（悉尼日期）、`fetched_at_utc`、`fetched_at_sydney`、`board`、`rank`、`code`、`name`、`heat_value`、`heat_field`、`run_id`（生成该文件的运行编号，下游引用时写上）。
- 东财榜单接口只给排名和代码。名称来自东财行情接口（只取代码和名称字段），该接口失败时，仍保存排名和代码，`name` 留空，状态记为 `partial`。
- akshare 的雪球函数只支持沪深（`category=CN`）。港股榜试探的是同一接口的 `category=HK`，不计入“当天成功”。

### 为什么不直接调用 akshare 函数

接口地址与 akshare 的 `stock_hot_rank_em`、`stock_hot_{follow,tweet,deal}_xq` 相同，但请求由本仓库自己发：

- akshare 的雪球请求头伪装成 Chrome 浏览器。本仓库不伪装请求头，请求如实标明身份（`market-data-bot/1.0`），被网站拒绝就如实记录。
- akshare 的请求不设超时、不重试。这里每次请求超时 30 秒，失败后重试 2 次，分别间隔 3 秒和 6 秒。
- akshare 的雪球函数会翻页拉取全部 A 股（约 5000 只、二十多次请求），这里只取前 50 名，一次请求。

### 输出

| 路径 | 内容 |
|---|---|
| `data/hot/YYYY-MM-DD/<榜单>.csv` | 当天榜单（悉尼日期） |
| `data/hot/YYYY-MM-DD/status.json` | 当天各榜最终状态 |
| `data/hot/status.json` | 最近一天的状态（同上） |
| `data/hot/runs.jsonl` | 每次运行追加一行，含每个榜的状态、行数、耗时、尝试次数、失败原因 |

`status.json` 顶层有 `run_id`、`run_attempt`（最近一次运行），每个榜也有自己的 `run_id`（备用运行保留下来的榜，是之前那次运行的编号）。重试后才成功的榜，`retry_errors` 保留之前几次失败的原因。

`status` 取值：`ok`（行数足额）、`partial`（有数据但不全，例如缺名称）、`failed`（无数据）。

`error_kind` 取值：

| 取值 | 含义 |
|---|---|
| `timeout` | 超时 |
| `connection` | 连接被拒或被重置（常见于海外 IP 被拦截） |
| `http_4xx` | 访问被拒绝 |
| `http_5xx` | 服务端错误 |
| `api_error` | 接口返回了错误码 |
| `bad_json` | 返回的不是数据，例如验证页 |
| `empty` | 数据为空或不全 |

试跑报告：`python3 scripts/hot_trial_report.py --from YYYY-MM-DD --to YYYY-MM-DD`

## 提交前检查

`scripts/check_forbidden.py` 扫描文件内容和文件名，发现与个人账户、交易价格、关注列表相关的敏感字样即返回非零、拒绝提交
（词表见脚本内 `FORBIDDEN`，以转义形式存放，脚本本身也能通过扫描）。三道关：

1. 本地 git hook：`git config core.hooksPath .githooks`，之后每次 `git commit` 自动扫描暂存区。
2. 每日工作流在 `git commit` 前运行检查，失败则不提交。
3. `.github/workflows/forbidden-check.yml` 在每次 push / PR 时扫描全仓库。

手动运行：

```sh
python3 scripts/check_forbidden.py          # 扫描暂存区
python3 scripts/check_forbidden.py --all    # 扫描全仓库
sh scripts/test_schedule_gate.sh            # 测试定时门控逻辑（美股）
sh scripts/test_hot_gate.sh                 # 测试定时门控逻辑（热榜）
python3 -m unittest discover -s tests       # 热榜抓取单元测试
```
