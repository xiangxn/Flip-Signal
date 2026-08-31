# Flip 纸面交易实施计划 —「自信崩溃」策略（2026-08-31, rev3）

> 目标：在 09-15 数据复验前的 2 周内，用 Go 程序**纸面运行**「自信崩溃」策略——
> 实时验证信号频率、时序行为与 P&L。引擎设计为**纸面/实盘同源**，
> 以配置参数区分模式，实盘路径后续直接在纸面程序上加。
> 依据：[策略方案](strategy_plan_2026-08-31.md)（C1/C2 规则）与 [重建模报告](report_2026-08-31.md)。
> 状态：**待确认（rev3）**。确认后按 §9 步骤实施。

---

## 1. 版本管理约定（本次起生效）

- **代码命名不再带版本字眼**（无 v3/paper 前缀）：引擎 `cmd/flip` + `internal/flip` +
  `internal/dashboard`，与旧代码同名同路径——内容差异完全由 **git 分支**承载。
- **分支即版本**：从当前 `eth`（存档 452a79d）切出 **`v3` 分支**开发；当前分支不再改动。
  后续 V4、V5 同样各开分支（`git checkout -b v4` …），查阅历史 = 切分支 + git log。
- **新分支只保留有用文件**（§2.2 清单）；旧文件仅在 git 其他分支可查。
- **文档目录不再按版本分**：`docs/` 直接放当前策略文档（report/strategy_plan/paper_plan），
  历史文档在旧分支；不再出现 `docs/v3` 这类目录。

## 2. 分支内容规划

### 2.1 新引擎结构（通用命名）

```
cmd/flip/main.go              # 主循环（参照 cmd/collect 的市场循环骨架）
internal/flip/                # 策略引擎包（纯逻辑，零外部依赖，可单测）
  types.go                    #   Config + Signal + 状态枚举
  engine.go                   #   状态机: Watching → Confirming → Done
  exec.go                     #   Executor 接口 + PaperExecutor（live 留接口）
  recorder.go                 #   JSONL 记录（按日切分）+ P&L 结算
internal/dashboard/           # Dashboard（参照旧模式重写，见 §7）
  server.go / handlers.go / state.go / templates.go / static/
internal/collect/             # 保留（工具复用 + 采集器备用，见下）
internal/feed/                # 保留（Binance/TWAP 适配器；OrderBook 复用）
internal/trading/             # 精简保留（仅 ResolutionPoller，见 §2.2）
```

### 2.2 新分支保留/删除清单

**保留（工具与数据复用）**

| 文件 | 保留理由 |
|---|---|
| `internal/collect/`（含 cmd/collect） | BestBid/BestAsk/MakePMTick/ParseMarketTokens 复用；采集器代码备用（暂停运行，后续需要大样本时更新再采） |
| `internal/feed/` | OrderBookAdapter（订阅+重启恢复）、TwapAdapter（诊断）、BinanceAdapter（保留研究对照能力） |
| `internal/trading/resolution_poller.go` | 官方结算轮询（gamma umaResolutionStatus） |
| `python/v2/lib.py` | 特征库依赖（extract_cross/cross_window/load_events） |
| `python/v3/`（featlib + 4 个脚本） | 09-15 复验脚本，纸面信号 JSONL 直接复用其分析逻辑 |
| `docs/report_2026-08-31.md`、`docs/strategy_plan_2026-08-31.md`、`docs/paper_plan_2026-08-31.md` | 当前策略三文档（从 docs/v3/ 平移，去版本目录） |
| `go.mod` / `go.sum` / `CLAUDE.md`（更新为新分支内容）/ `.gitignore` | 基础设施 |

**删除（历史版本，git 其他分支可查）**

| 文件 | 说明 |
|---|---|
| `cmd/flip` 旧版、`cmd/lab`、`cmd/test_resolve` | 旧 5s Formula B 引擎及配套（已被新实现覆盖/废弃） |
| `internal/flip` 旧实现、`internal/lab`、`internal/config`、旧 dashboard | 旧引擎组件 |
| `internal/trading/` 中 client/order/trader/recorder/risk/prefetch 等 | 实盘 FAK 路径——**live 阶段再恢复**（届时从 eth 分支捡回） |
| `docs/` 中 2026-08-13 及更早的历史文档（flip_strategy_plan_2026-08-13.md 等） | 历史策略文档 |
| `python/` 旧脚本（backtest_*、twap_reanalysis_* 等）及 `python/v2/` 01-05 脚本 | 历史分析（lib.py 保留） |
| `config.example.yaml` | 新引擎配置用 flag+env，无 yaml |

### 2.3 纸面/实盘同源

- 成交执行抽象为 `Executor` 接口（`Execute(sig) (ExecResult, error)`），
  `mode: paper|live` 配置区分。阶段一实现 `PaperExecutor`（按 §3.3 口径模拟成交，
  真实 ask 与互补价双记录）；live（FAK，从 eth 分支恢复实盘路径）留到后续阶段，
  接口与 mode 配置现在预留。

### 2.4 数据采集暂停（用户决策）

- **cmd/collect 暂停运行**：已采 14 天数据（data/btc）足够回测；纸面引擎自身也消费
  gamma REST（市场预取 + 结算轮询），双程序并行会增加 API 压力。
- 代码保留在分支（不删），后续需要更大样本时更新再采。
- **后果**：09-15 复验样本 = 纸面信号记录（每次穿越一行完整字段）+ 现有 14 天数据；
  纸面引擎不落盘 ticks，只记穿越观测（含 book_latency 诊断字段，可事后过滤）。

## 3. 引擎设计（与回测 1:1 映射）

### 3.1 状态机

```
Watching ──首个上升沿(>0.7, 15<rem<260)──▶ Confirming ──+10s──▶ 判定 → Done
  ▲                                                          │
  └──────────── 事件结束(本窗口不再观测) ◀───────────────────┘
```

- **Watching**：每个 1s tick 更新 YES/NO 两侧状态。只在 `15 < rem < 260` 的 tick 上做上升沿
  检测（`bid > 0.7` 且此前同侧最后有效 bid ≤ 0.7）；两侧独立，**事件内时间顺序首个**穿越
  即观测（回测无 fallback 重试——与旧引擎的"多穿越重试"不同）。
- **Confirming**：记录 `trigger_bid`（穿越时刻穿越侧 bid）与 `rem`；等满 10 个 tick。
- **+10s 判定**：
  - `post_end` = 穿越侧 bid@+10s
  - `fill` = 对侧真实 ask@+10s（纸面/实盘成交口径）；同时记录互补价 `1 - 穿越侧bid@+10s`（回测口径）
  - **C1** `trigger_bid > 0.73` 且 **C2** `post_end ≤ 0.66` → 信号：`shares = 2 / fill`，标记 filled
  - 任一不满足 → 失败穿越（记录 `ok=false` + 失败原因，诊断用）
- **Done**：事件内不再检测任何穿越（与回测每事件仅首个观测一致）。

### 3.2 回测口径映射表（逐项核对）

| 回测 `extract_cross`（python/v2/lib.py） | Go 引擎 | 备注 |
|---|---|---|
| `TRIGGER = 0.7`，`bid > 0.7` 且前一有效 tick ≤ 0.7 | 同上（1s 粒度天然对齐） | 缺 tick 时 bid=0 视为 0 |
| 只在 `REMAIN_MIN(15) < rem < REMAIN_MAX(260)` 的 tick 检测 | 同上，rem 由窗口结束时间推算 | 前 40s / 后 15s 的穿越不观测 |
| `len(ticks) < MIN_PRE_TICKS+5` 跳过事件 | 事件前 10 tick（rem>250）不做检测 | 等价 |
| `trigger_bid = 穿越侧 bid@i` | 穿越时刻穿越侧 bid | C1 |
| `j = min(i+10, n-1)` 确认 tick | +10s tick（窗口尾部取末 tick） | 回测 `flip_fill10s` |
| `flip_fill10s = 1 - 穿越侧bid@j` | 互补价（记入 `fill_comp`） | 回测口径，用于频率/EV 对比 |
| （回测无） | 对侧真实 ask@+10s（记入 `fill`） | 纸面/实盘口径，评估差异 |
| `won = outcome 与穿越侧相反` | 同（outcome: 0=Up 1=Down） | 结算 |
| `cls = both/only` | 事件结束时回填（两侧是否都穿越过） | 机制诊断 |

### 3.3 P&L 口径（与回测 2U 完全一致）

- `shares = 2 / fill`；赢 → `pnl = shares - 2`（每股兑 1U）；输 → `pnl = -2`。
- 仅通过判定（ok=true）的信号参与 P&L；失败穿越无盈亏。

## 4. 记录设计（JSONL，按日切分 `data/v3/`）

每窗口一行（事件类型字段区分），字段与回测 trades_v3.csv 对齐以便直接复用 python 分析：

```jsonc
// 穿越记录（成功与失败都记 —— 校准信号频率必需）
{"event_type":"cross", "ts":..., "date":"2026-08-31", "condition_id":"...", "slug":"...",
 "event_start":..., "side":"up", "rem":120, "trigger_bid":0.74,   // side: up/down（回测为 yes/no）
 "post_end":0.62, "fill":0.40, "fill_comp":0.38, "shares":5.0, "stake":2,
 "ok":true, "reject_reason":"", "book_latency_ms":120, "twap_age_ms":0,
 "cls":"both",                          // 窗口结束时回填（整窗类别: both/only）
 "won":true, "pnl":+3.0, "resolved_at":"..."}  // 结算时回填（未结算行无此字段）
```

- **即时落盘**：全部观测窗口结束时立即写盘（行级 flush），ok=true 行先写
  won/pnl 空缺、结算后整体重写当日文件（temp+rename 原子替换）—— 崩溃/断电不丢已记录事件。
- **重启恢复**：启动时扫描 `crosses_*.jsonl` 恢复内存态，未结算信号的 conditionID
  自动重新注册结算轮询（`recorder.PendingSignals()`）。
- **UTC 日**：`date` 与文件名切分均为 UTC 日（与回测 date 口径一致，避免本地时区跨日错位）。
- **09-15 复验**：纸面 JSONL → 回测口径（up/down→yes/no、fill←fill_comp、won→flip_won）
  由 `python/v3/reuse_signals.py` 完成，输出 `trades_paper_v3.csv` 与基准对比摘要。

> 说明：`data/v3/` 是**数据输出目录**名（非代码版本字眼），如不合意可改
> `data/paper/` 或 `data/signals/`。

## 5. 结算

- 复用 `internal/trading/resolution_poller.go`：窗口结束注册 `(conditionID, slug)`，
  10s 轮询 gamma，`umaResolutionStatus=="resolved"` 且 outcomePrices 胜方 > 0.5（归一化容忍
  "1"/"1.0"/"0.999…"）→ 回调 `Resolve(conditionID, outcome)`。
- 回调失败（如落盘失败）保持 pending 下次轮询重试；超过 24h 未结算（争议/无效市场）
  放弃轮询（记录保持未结算状态）。
- 与回测的"官方 TWAP outcome"一致；collect 的流采样+修正机制不引入（直接等官方，更准）。

## 6. 配置（flag + 代码默认值，参照 cmd/collect 风格）

- 阈值集中在 `flip.DefaultConfig()`，flag 覆盖，参数与回测脚本同名：
  `--trigger-threshold` / `--trigger-bid-min` / `--post-end-max` / `--stake` / `--mode` /
  `--dashboard :8090` / `--output data/v3`。
- 不引入 yaml/viper（旧 internal/config 已从分支删除）。

## 7. Dashboard 重写方案（internal/dashboard/）

参照旧 dashboard 三件套重写（server.go / handlers.go / state.go + `go:embed static/`，无 templates.go）：

| API | 内容 |
|---|---|
| `/api/state` | 当前窗口（conditionID/rem/引擎状态/穿越侧与倒计时/yes-no bid+ask）+ 汇总（信号数/胜率/累计 P&L/逐日正/最大回撤） |
| `/api/crosses` | 最近穿越列表（含失败，`ok` 标色）—— 校准信号频率（预期 ~10 信号/天，穿越总数另算） |
| `/api/signals` | 已结算信号历史（WR/fill/P&L） |
| `/api/config` | 阈值族参数 + mode |

前端单页（index.html + app.js + style.css，全量重写“需要兼容手机浏览器显示”）：顶部状态卡（1s 轮询）→
中部当前窗口微视图（盘口价格/状态机进度条）→ 穿越历史表（成功/失败）→ 信号表 + P&L 汇总。

## 8. 文件清单（新分支最终形态）

```
cmd/flip/main.go                  # 主循环：配置 + 组件装配 + 市场循环
cmd/collect/                      # 保留（暂停运行）
internal/flip/{types,engine,exec,recorder,engine_test,recorder_test}.go
internal/dashboard/{server,handlers,state}.go + static/
internal/collect/  internal/feed/  internal/trading/resolution_poller.go
python/v2/lib.py   python/v3/{featlib,02_model,03_rules,04_anatomy,06_backtest,reuse_signals}.py
docs/{paper_plan,strategy_plan,report}_2026-08-31.md
CLAUDE.md（更新） go.mod  go.sum  .gitignore
```

## 9. 实施步骤

0. `git checkout -b v3`（从 eth=452a79d 切出）；随后清理分支：删除 §2.2 清单中的历史文件，
   平移 docs/v3/ → docs/，更新 CLAUDE.md → 提交一次「分支初始化」
1. `internal/flip` types + engine + 单元测试（纯函数先行，覆盖：上升沿检测、窗口边界 rem、
   两侧独立、+10s 确认、C1/C2 判定、窗口尾部 j 越界）
2. `internal/flip/exec.go`（Executor 接口 + PaperExecutor）
3. `internal/flip/recorder.go`（按日切分 + 结算回填 + 崩溃恢复）
4. `cmd/flip/main.go` 市场循环（预取/订阅/1s tick/窗口结束/结算注册）
5. `internal/dashboard/`（server + handlers + 前端）
6. `go build ./...` + `go vet ./...` + `go test ./internal/flip/`
7. 试运行 30-60 分钟（独立跑，不依赖 collect），核对日志 + dashboard + JSONL
8. 每完成阶段提交一次 git

## 10. 验证标准（试运行期）

| 指标 | 预期 | 异常信号 |
|---|---|---|
| 信号频率 | ~10 信号/天（回测基准） | 差异 >50% 需查口径偏差 |
| 穿越-信号比 | 回测同规则下 ≈140/3641 | 过高/过低提示盘口读取偏差 |
| fill vs fill_comp | 相差 ≤0.01（档位差） | 持续 >0.01 说明互补假设失效 |
| 结算 | 窗口结束后数分钟 resolved | 长期 pending 查 ResolutionPoller |

## 11. 风险与说明

1. **数据缺失口径**：WS 断流时 bid 缺省为 0 → 可能漏穿越或误判（回测同口径，仅影响
   记录完整性，不影响规则偏差，如果bid为0直接跳过信号检查）；记录 book_latency_ms 供事后过滤。
2. **互补假设 vs 真实 ask**：回测无对侧真实 ask 快照，纸面记录两者差异，09-15 复验时
   评估是否需调整 fill 口径。
3. **首穿越不重试**：事件内首个穿越不满足 C1/C2 即放弃本窗口（回测刻意如此）。
4. **数据采集暂停**：09-15 复验样本 = 纸面信号记录 + 现有 14 天数据；如需更大样本
   届时恢复 cmd/collect（代码保留在分支）。
5. **live 模式为后续阶段**：Executor 接口与 mode 配置预留，实盘实现（FAK + 凭证，
   从 eth 分支恢复实盘路径）在纸面验证通过后再接。
