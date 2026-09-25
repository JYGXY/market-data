# market-data

每日公开市场行情快照。**本仓库只存放公开市场数据**，不含任何个人账户、交易或关注列表信息，
也不抓取任何自选个股的价格。

## 运行方式

- GitHub Actions 工作流 `.github/workflows/daily-market-data.yml`，纽约时间周一至周五运行，周末不运行：
  - **16:17 主任务**（美股收盘后 17 分钟）
  - **16:47 备用任务**：当天数据已生成就直接退出，只在主任务漏跑时补跑一次（下游 17:10 读文件，留出余量）
- 分钟数刻意避开整点和半点。GitHub 文档说明，整点前后定时任务负载最高，可能延迟甚至被丢弃。
- GitHub 定时只认 UTC，所以每个任务放两个定时，分别对应夏令时和冬令时：

  | 任务 | EDT（UTC-4） | EST（UTC-5） |
  |---|---|---|
  | 主任务 | `17 20 * * 1-5` | `17 21 * * 1-5` |
  | 备用 | `47 20 * * 1-5` | `47 21 * * 1-5` |

  `scripts/schedule_gate.sh` 按纽约当前 UTC 偏移只放行对应的那个定时，并核对纽约星期几。判断看偏移而不看钟点，所以 GitHub 延迟触发也不会重复或错位。
- 备用任务判断“当天已生成”的依据：`data/<纽约今天>.json` 已存在，或 `latest.json` 是纽约今天 16:00 之后生成的（美股假日时文件名是上一交易日；收盘前的手动运行不算）。
- 美股休市的工作日照常运行，但美股数据仍是上一交易日，`trading_date` 不变，会覆盖同名文件。
- 主任务和备用任务都依赖 GitHub 定时；若 GitHub 两次都没触发，当天仍会缺数据，下游须按“下游读取须知”核对 `trading_date`。
- 也可以在 Actions 页面手动触发（`workflow_dispatch`）：`mode=full` 直接抓取，`mode=backup` 走备用逻辑。也可以本地运行：

  ```sh
  pip install -r requirements.txt
  python3 scripts/fetch_market_data.py
  ```

## 输出文件

| 文件 | 说明 |
|---|---|
| `data/YYYY-MM-DD.json` | 当天快照。文件名即 `trading_date`（美股交易日）；休市日或手动运行时覆盖上一交易日文件，不会产生重复日期。 |
| `data/latest.json` | 最近一次快照，内容与当天文件相同。 |

## 下游读取须知

读取 `data/latest.json` 时，**先核对 `trading_date`**：

- `trading_date` 不是你预期的当天美股交易日 → **视为未更新**（任务没跑、跑失败或数据源未刷新），不要当作当天行情使用。
- `complete` 为 `false` → 有部分项缺失，缺哪些见 `missing`，对应项的 `error` 写明原因。

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
sh scripts/test_schedule_gate.sh            # 测试定时门控逻辑
```
