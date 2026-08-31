# CLAUDE.md — Flip Signal

## 项目概述

**Flip Signal** 是一个针对 **Polymarket BTC 5分钟市场**（btc-updown-5m，Chainlink TWAP-60 结算）的量化交易系统。
当前策略 **「自信崩溃」flip**：检测 UP/DOWN 价格穿越 0.7 后 10 秒内的反转信号——一侧 bid 曾 >0.73（市场高度自信）
且 10 秒内崩回 ≤0.66（自信瓦解）时买入对侧。

### 核心原则
> **不预测涨跌，只判断「市场曾经极度自信，却正在 10 秒内自己打脸」。**

### 当前策略状态（2026-08-31 定稿）
- 完整方案见 `docs/strategy_plan_2026-08-31.md`，分析见 `docs/report_2026-08-31.md`，
  实施计划见 `docs/paper_plan_2026-08-31.md`，分析脚本 `python/v3/`。
- **信号条件（全部来自 PM 订单簿，无任何 BTC 价格输入，口径无关）**：
  - 穿越检测：触发侧 best bid 首次 > 0.70（1s tick，每事件**首个**上升沿）
  - C1 高度自信：穿越时刻触发侧 bid > 0.73（trigger_bid）
  - C2 快速崩溃：确认时刻（穿越 +10s）触发侧 bid ≤ 0.66（post_end）
  - 窗口约束：rem ∈ (15, 260)，事件前 10 tick 不检测
- **成交口径**：决策 +10s，买入对侧；`fill` = 对侧真实 ask@+10s（纸面/实盘口径），
  `fill_comp` = 互补价 `1 - 触发侧 bid@+10s`（回测口径，双记录用于对比）
- **P&L**：`shares = stake / fill`；赢 → `shares - stake`，输 → `-stake`（每股兑 1U）
- **结算**：官方 outcome（0=Up 1=Down），gamma `umaResolutionStatus=="resolved"` 后由
  ResolutionPoller 轮询触发
- **回测基准（14 天，2U/笔）**：n=140，WR 50.7%，EV +0.107/股，总 +73.06 USDC，
  Wilson CI [42.5%, 58.9%]，双半同号、test2d/长窗 OOS 为正
- 🔴 **状态：纸面交易实施中**。引擎纸面运行验证信号频率/时序/P&L，待 **09-15 数据复验**
  后评估是否小 stake 实盘。数据采集（cmd/collect）**暂停**（已采 14 天数据足够回测，
  避免 API 压力），代码保留备用。
- 已证伪：+0s 穿越即入（AUC 0.51）、+2s 提前入场、v2 other_delta 族（选择偏差）、
  Binance 盘口/OFI/regime/基差特征（无样本外边缘）、低 fill 策略。

### 版本管理约定（2026-08-31 起生效）
- **分支即版本**：当前分支 `v3`（从 eth=452a79d 切出）。代码命名不带版本字眼；
  后续策略演进（V4、V5…）各开新分支（`git checkout -b v4`…），历史代码/文档
  仅在 git 其他分支可查（eth 分支保留旧 Formula B 引擎与 v1/v2 全部历史）。
- 新分支只保留有用文件；文档直接放 `docs/`，不按版本建子目录。

---

## 项目结构

```
FlipSignal/
├── cmd/
│   ├── flip/main.go                     # 策略引擎主入口（纸面/实盘同源，-mode 切换）
│   ├── collect/main.go                  # 高频数据采集（保留备用，暂停运行）
│   └── compact/main.go                  # 数据压缩工具（collect 配套）
├── internal/
│   ├── flip/
│   │   ├── types.go                     # Config + Signal + 状态枚举
│   │   ├── engine.go                    # 状态机: Watching → Confirming → Done
│   │   ├── exec.go                      # Executor 接口 + PaperExecutor（live 留接口）
│   │   ├── recorder.go                  # JSONL 记录（按日切分）+ P&L 结算
│   │   └── engine_test.go               # 单元测试
│   ├── dashboard/
│   │   ├── server.go                    # HTTP server（go:embed static/）
│   │   ├── handlers.go                  # /api/state, /api/crosses, /api/signals, /api/config
│   │   ├── state.go                     # 运行时组件引用
│   │   └── static/                      # index.html, app.js, style.css（兼容手机浏览器）
│   ├── collect/                         # 数据格式 v2 工具（BestBid/BestAsk/MakePMTick/
│   │                                    #   ParseMarketTokens/SettlementWorker，引擎复用）
│   ├── feed/                            # Binance/TWAP/PM CLOB WS 适配器
│   └── trading/
│       └── resolution_poller.go         # 官方结算轮询（gamma umaResolutionStatus）
├── docs/                                # 当前策略文档（报告/方案/实施计划）
├── python/
│   ├── v2/lib.py                        # 数据加载 + 穿越观测提取（特征库依赖）
│   └── v3/                              # 特征库 + 分析/回测脚本 + reuse_signals.py（复验用）
├── go.mod / go.sum
└── CLAUDE.md                            # 本文件
```

---

## 架构与数据流

```
          Polymarket CLOB             (Binance 仅研究对照, 采集暂停)
        ┌────── WS ──────┐
        │ MarketMonitor  │
        │ (CLOB books)   │
        └───────┬────────┘
                ▼
        UP/DOWN 盘口 (best bid/ask)
                │
                ▼
        Flip Engine (1s tick)
    Watching → Confirming → Done
                │
                ▼
        Executor (PaperExecutor 模拟成交)
                │
                ▼
        Recorder (JSONL 按日切分 + P&L)
                │
                ▼
        ResolutionPoller (gamma 结算轮询)
```

### 市场循环流程

```
1. 计算下个 5分钟对齐时间戳；预取 gamma 市场信息（边界前 20s）
2. 窗口起点 → 订阅 UP/DOWN token（MarketMonitor），引擎 Reset
3. 每秒 1s tick：读 UP/DOWN 盘口 → ProcessTick(引擎状态机)
4. 穿越检测 → 确认（+10s）→ C1/C2 判定 → 信号/失败穿越 → Recorder
5. 窗口结束（rem=0）→ 事件封存 → 注册结算轮询 → 下一窗口
```

### 引擎状态机

```
Watching ──首个上升沿(>0.7, 15<rem<260)──▶ Confirming ──+10s──▶ 判定 → Done
  ▲                                                          │
  └──────────── 事件结束(本窗口不再观测) ◀───────────────────┘
```

- **Watching**: 1s tick 更新 UP/DOWN 两侧状态，只在 15 < rem < 260 的 tick 做上升沿检测
- **Confirming**: 记录 trigger_bid，等 10 个 tick 取 post_end 判定
- **Done**: 事件内不再检测（与回测每事件仅首个观测一致，无 fallback 重试）
- 数据质量：盘口 bid/ask 为 0（缺数据）时**直接跳过信号检查**，记录 book_latency 供事后过滤

---

## 依赖库

| 库 | 用途 |
|----|------|
| `github.com/xiangxn/go-polymarket-sdk` | Polymarket REST/WS 客户端 |
| `github.com/gorilla/websocket` | Binance WebSocket 连接 |
| `github.com/tidwall/gjson` | JSON 解析（SDK 依赖）|

### 本地开发 replace 指令

```
replace (
    github.com/xiangxn/go-polymarket-sdk => /tmp/go-polymarket-sdk
)
```

---

## Go 代码规范与最佳实践

（沿用既有规范，本分支全量适用）

- **注释语言一律使用中文**，技术专有名词保留英文（如 BTC、PM、UP/DOWN 等）
- **标准库优先**；`internal/flip/` 核心计算层零外部依赖，可独立测试
- **纯函数优先**：特征提取/判定逻辑为纯函数，无副作用，table-driven 测试
- 命名：文件 `snake_case.go`、导出 `PascalCase`、私有 `camelCase`、常量 `stateXxx`
- 配置用专用 struct + `DefaultConfig()` 工厂；可调参数集中在 config struct，
  通过注释标注出处（如 `strategy_plan_2026-08-31.md §2`）
- 状态机用 `type xxxState int` + iota + `switch e.state`
- 并发：只在必要边界加锁（`sync.Mutex` 写、`sync.RWMutex` 读多写少），
  简单标志用 `atomic`；Context 优雅关闭
- 错误处理：返回 `(result, error)` 不 panic；`fmt.Errorf("context: %w", err)`
- 日志：`log.Printf("[Component] message")`，关键状态用 emoji ⚠️🔒🟢🔴🟡🔥🎯
- I/O：`bufio.Writer` 包装，JSONL 每行一个 object，追加模式支持重启不丢数据

### 测试
```bash
go test ./internal/flip/ -v           # 引擎单元测试
go build ./...                         # 全量编译检查
go run ./cmd/flip -dashboard :8090     # 运行引擎 + Dashboard
```

---

## 关键设计决策

1. **纸面/实盘同源**：成交执行抽象为 `Executor` 接口，`mode: paper|live` 配置区分。
   阶段一仅 `PaperExecutor`（ask@+10s 模拟成交）；live（FAK）接口与配置已预留，
   纸面验证通过后从 eth 分支恢复实盘路径接入。
2. **口径与回测 1:1**：穿越检测/确认/C1/C2/P&L 全部映射回测 `extract_cross`
   口径（见 paper_plan §3.2 映射表），信号 JSONL 字段对齐回测 trades_v3.csv，
   09-15 用 `python/v3/reuse_signals.py` 映射复核（side up/down→yes/no、
   fill←fill_comp、won→flip_won）。
3. **首穿越不重试**：事件内首个上升沿即观测，不满足 C1/C2 即放弃本窗口——
   与回测刻意一致，非引擎缺陷。
4. **fill 双记录**：真实对侧 ask（纸面/实盘口径）与互补价（回测口径）同时落盘，
   用于评估互补假设的有效性。
5. **数据采集暂停**：cmd/collect 保留不运行（API 压力），09-15 复验样本 =
   纸面信号记录 + 现有 14 天数据。
6. **即时落盘 + 重启恢复**：观测窗口结束立即落盘（行级 flush，崩溃不丢）；
   结算回填 temp+rename 原子重写当日文件；重启扫描 JSONL 恢复内存态并
   自动重新注册未结算信号的结算轮询。
7. **单 WS 订阅复用**：引擎与采集共用 `feed.OrderBookAdapter` 的重启恢复模式。

---

## 运行方式

```bash
# 纸面运行（默认）
go run ./cmd/flip -output data/v3 -dashboard :8090

# 参数（与回测脚本同名）
go run ./cmd/flip --trigger-threshold 0.7 --trigger-bid-min 0.73 \
                  --post-end-max 0.66 --stake 2 --mode paper
```

环境变量（参照 cmd/collect 约定，无配置即只读运行）：
| 变量 | 说明 | 必填 |
|------|------|------|
| `POLYMARKET_OWNER_KEY` | 钱包私钥 | live 模式 |
| `POLYMARKET_CLOB_KEY/SECRET/PASSPHRASE` | CLOB 凭证 | live 模式 |
| `POLYMARKET_PROXY` | SOCKS5 代理 | 可选 |
