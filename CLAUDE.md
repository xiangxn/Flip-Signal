# CLAUDE.md — Dog@0.2 Flip Signal

## 项目概述

**Flip Signal** 是一个针对 **Polymarket BTC 5分钟市场**（btc-updown-5m，Chainlink TWAP-60 结算）的量化交易系统。
当前策略 **「狗@0.2」**：检测 UP/DOWN 盘口某侧 ask 被砸到 ≤0.20 的 tick（下狗机会），
若该侧刚经历急跌（45s 内 ask 曾 ≥0.40）且 Binance spot 相对锚（TWAP-60 开盘）处于
**浅洞**（spot 在狗败侧、向狗败方向偏锚 ≤0.5σ 的浅坑，dist_s ∈ (−0.5, 0)）、窗口尚余 >180s，则买入该下狗。

### 核心原则
> **不预测涨跌，只判断「市场刚把某个 outcome 砸到 0.2 的极端折价——砸出急跌坑，
> 现货却没真正走出来」，买折价本身。**

### 当前策略状态（2026-09-02 定稿，v4 分支）
- 完整方案见 `docs/engine_plan_dog020_2026-09-02.md`，口径映射/运行说明见
  `docs/dog020_mapping_2026-09-02.md`，分析/回测脚本 `python/v4/`（权威 =
  `python/v4/01_backtest_r1.py`）。
- **信号条件（触发与急跌腿来自 PM 订单簿；浅洞腿输入 Binance spot）**：
  - 触发：每事件首个有效 tick 上某侧 ask 满足 `0 < ask ≤ 0.20`（唯一触底侧即狗侧；
    两侧都 ≤0.2 的交叉态取 sgn·(spot−anchor)<0 一侧，14 天历史 0 次）
  - 急跌：m_45 = 触发前 45 个 tick 槽位内同侧 ask max ≥ 0.40
  - 浅洞：dist_s = sgn·(spot−anchor)/anchor·1e4/hist_bps ∈ (−0.5, 0) 开区间
  - 时间：rem > 180（窗口前 ~2 分钟）
  - σ（hist_bps）= 前 ≤18 个已完窗口 |tw_close−tw_open| 均值（≥3 窗可用）
- **成交口径**：fill = 触发 tick 狗侧 ask（≤0.20，无滑点）；`shares = stake/fill`；
  赢 → `shares − stake`，输 → `−stake`（每股兑 1U）
- **结算**：官方 outcome（0=Up 1=Down），gamma `umaResolutionStatus=="resolved"` 后由
  ResolutionPoller 轮询触发
- **回测基准（14 天，2U/笔，2026-08-18~31）**：R1 m_45 纯现货 n=245，WR 29.0%，
  EV +1.078U/注，+264U/14 天；日正 12/14；双层（+dist_t）n=197，WR 30.5%，EV +1.234U/注
- 观察变体（2026-09-03 起 01 脚本四规则报告，未落引擎，09-15 后定）：no 侧浅洞带放宽到
  (−1,0)（yes 不变）纯现货 n=546 WR 24.4% EV +0.606U/注 +331U/14 天；双层 n=461
  EV +0.622U/注 +287U——新增 no(−1,−0.5] 段 301 笔 WR 20.6% EV +0.22U/注（+67U）
- 🔴 **状态：纸面交易实施中**。引擎纸面运行验证信号频率/时序/P&L，待 **09-15 双样本
  复验**（现网记录 + 已有 14 天数据）后评估是否小 stake 实盘。
- 已证伪：flip「自信崩溃」家族（v3，分支 v3 保留）、v1/v2 follow/wait 族、0.2 深度
  全市场扫、双层版单独 TWAP 腿等——历史分析/代码在 git 其他分支可查。

### 版本管理约定（2026-08-31 起生效，v4 分支沿用）
- **分支即版本**：当前分支 `v4`（从 v3 切出，dog@0.2 全新独立实现）。
  代码命名不带版本字眼；后续策略演进各开新分支（`git checkout -b v5`…），
  旧分支（v3/eth）保留全部旧代码/文档，可 `git checkout v3 -- <路径>` 复活。
- 新分支只保留有用文件；文档直接放 `docs/`，不按版本建子目录。
- **提交规范**：提交信息用中文、组件前缀（`flip:` / `dashboard:` / `python:` /
  `cleanup:` / `docs:`），**不带 `Co-Authored-By` 等任何署名行**（2026-08-31 用户要求）。

---

## 项目结构

```
FlipSignal/
├── cmd/flip/                         # 策略引擎主入口（纸面/实盘同源，-mode 切换）
│   ├── main.go                       # 窗口循环/数据源接线/anchor σ/触发即执行记录
│   ├── pmtick.go                     # PM 盘口采样 + token 解析（main 私有 helper）
│   └── pmtick_test.go
├── internal/
│   ├── flip/
│   │   ├── types.go                  # Config + Tick/Observation/Record + 状态枚举
│   │   ├── engine.go                 # 状态机: Watching → Done（触底观测/四腿判定）
│   │   ├── exec.go                   # Executor 接口 + PaperExecutor（live 留接口）
│   │   ├── recorder.go               # JSONL 记录（按日切分）+ P&L 结算回填
│   │   └── engine_test.go / recorder_test.go
│   ├── dashboard/
│   │   ├── server.go                 # HTTP server（go:embed static/）
│   │   ├── handlers.go               # /api/state, /api/observations, /api/signals, /api/config
│   │   ├── state.go                  # 运行时组件引用
│   │   └── static/                   # index.html, app.js, style.css（兼容手机浏览器）
│   ├── feed/
│   │   ├── binance_adapter.go        # Binance BTCUSDT WS（spot 浅洞输入, 本地接收龄）
│   │   └── twap_adapter.go           # Chainlink TWAP-60（anchor/σ）+ FetchTwapRanges 预热
│   └── trading/
│       └── resolution_poller.go      # 官方结算轮询（gamma umaResolutionStatus）
├── docs/                             # 策略文档（v4 方案/口径映射）
├── python/
│   ├── v2/lib.py                     # 数据加载器（v4 回测脚本依赖，保留）
│   └── v4/                           # 回测权威脚本 + 纸面对账脚本
├── go.mod / go.sum
└── CLAUDE.md                         # 本文件
```

**已删除（v4 清理，v3 分支保留可复活）**：cmd/collect、cmd/compact、internal/collect
（数据采集管线——引擎所需盘口工具已内置 pmtick.go）；internal/feed/orderbook_adapter.go；
twap_adapter 的 PollOfficialOpen/ClosePrice；python/v3、docs 三份 2026-08-31 flip 文档。

---

## 架构与数据流

```
   Polymarket CLOB          Chainlink TWAP-60         Binance BTCUSDT spot
   ┌─────────────┐          ┌──────────────┐          ┌──────────────────┐
   │ MarketMonitor│         │ TwapAdapter  │          │ BinanceAdapter   │
   │ (UP/DOWN books)│       │ (anchor/σ)   │          │ (浅洞腿输入)      │
   └──────┬──────┘          └──────┬───────┘          └───────┬──────────┘
          ▼                        ▼                          ▼
    UP/DOWN 盘口 1s        边界 TWAP 值/龄           最后价 + 本地接收龄(>2s 判 stale)
          │                        │                          │
          └──────────────┬─────────┴──────────┬───────────────┘
                         ▼
               Flip Engine (1s tick)
            Watching → (触底) → Done
                         │
                         ▼
             Executor (PaperExecutor 模拟成交)
                         │
                         ▼
             Recorder (touches_*.jsonl 按日切分 + P&L)
                         │
                         ▼
             ResolutionPoller (gamma 结算轮询)
```

### 市场循环流程

```
1. 计算下个 5分钟对齐时间戳；预取 gamma 市场信息（边界前 20s）
2. 边界对齐 → 注入窗口上下文（anchor=TWAP 流值、σ）→ 订阅 UP/DOWN token
3. 每秒 1s tick：读 UP/DOWN 盘口 + Binance spot + TWAP → ProcessTick(状态机)
4. 首个触底 tick（ask≤0.20）→ 四腿判定 → 观测落盘（ok 与失败都记，即时落盘）
5. ok 信号 → PaperExecutor 执行 → Register 结算轮询（窗口内完成，无窗末补判）
6. 窗口结束（rem=0）→ |close−anchor| 追加进 σ 滚动窗 → 下一窗口
```

### 引擎状态机

```
Watching ──首个触底观测(ask≤0.20, 四腿判定)──▶ Done
   ▲                                        │
   └────────── 窗口结束(rem==0) ◀───────────┘
```

- **Watching**: 1s tick 更新两侧状态；锚缺失窗口（anchor≤0，窗口级）整窗不观测
  （镜像回测 :69 锚缺失事件跳过）；有效 tick（latency≤300 且 UP/DOWN 双侧报价齐全
  ——整簿快照门控，实测缺失为整行全空）上检查 up/down ask 是否 ≤0.20
- **判定顺序**（一次完成）：rem_low → no_hist → missing_spot → no_crash → dist_out
  （missing_anchor 现网不可达，仅 decide 纯函数防线）；全过 → ok（shares = stake/fill）
- **Done**: 事件内不再检测（与回测每事件仅首个观测一致，无 fallback 重试）
- 数据质量：无效 tick 压 0 占槽（不进触发检查，不贡献急跌窗 max）

---

## 依赖库

| 库 | 用途 |
|----|------|
| `github.com/xiangxn/go-polymarket-sdk` | Polymarket REST/WS 客户端 |
| `github.com/gorilla/websocket` | Binance WebSocket 连接（feed adapter） |
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
  通过注释标注出处（如 `01_backtest_r1.py` 常量）
- 状态机用 `type xxxState int` + iota + `switch e.state`
- 并发：只在必要边界加锁（`sync.Mutex` 写、`sync.RWMutex` 读多写少），
  简单标志用 `atomic`；Context 优雅关闭
- 错误处理：返回 `(result, error)` 不 panic；`fmt.Errorf("context: %w", err)`
- 日志：`log.Printf("[Component] message")`，关键状态用 emoji ⚠️🔒🟢🔴🟡🔥🎯
- I/O：`bufio.Writer` 包装，JSONL 每行一个 object，追加模式支持重启不丢数据

### 测试
```bash
go test ./internal/flip/ ./internal/trading/ ./cmd/flip/ -v   # 引擎/记录器/盘口工具
go build ./...                         # 全量编译检查
go run ./cmd/flip -dashboard :8090     # 运行引擎 + Dashboard
```

---

## 关键设计决策

1. **纸面/实盘同源**：成交执行抽象为 `Executor` 接口，`mode: paper|live` 配置区分。
   阶段一仅 `PaperExecutor`（校验 fill>0，无其它边界）；live（FAK）接口与配置已预留，
   纸面验证通过后从 eth 分支历史恢复实盘路径接入。
2. **口径与回测 1:1**：触发/急跌窗/浅洞/时间腿/σ/P&L 全部映射回测 `01_backtest_r1.py`
   口径（详见 `docs/dog020_mapping_2026-09-02.md`），观测 JSONL 字段对齐回测 CSV，
   09-15 用 `python/v4/02_paper_compare.py` 映射复核（ok ⇔ in_pure、won ⇔ settle_won）。
3. **首触不重试**：事件内首个触底 tick 即观测，判定失败即 Done（本窗不再检）——
   与回测刻意一致，非引擎缺陷。
4. **观测全落盘**：失败观测同样落盘（reject_reason 分解），供信号频率校准与
   09-15 原因分布对比；触发即落盘 + 结算仅 ok 行注册轮询。
5. **数据采集已删除**：cmd/collect 整体移除（API 压力考量，保留备用已无意义）；
   未来需增采按 `docs/dog020_mapping_2026-09-02.md §5` 复活（注意键名炸弹）。
6. **即时落盘 + 重启恢复**：观测在触发 tick 立即落盘（行级 flush，崩溃不丢）；
   结算回填 temp+rename 原子重写当日文件；重启扫描 JSONL 恢复内存态并自动重新
   注册未结算信号的结算轮询。
7. **单 WS 订阅复用**：MarketMonitor 重启恢复模式沿用；TwapAdapter 内建新鲜度
   看门狗（推送停更超 2min 自动重建订阅）；BinanceAdapter 首拨失败由 main 侧
   指数退避重试、断线后 runReadLoop 自愈。

---

## 运行方式

```bash
# 纸面运行（默认）
go run ./cmd/flip -output data/v4 -dashboard :8090

# 参数（与回测脚本同名）
go run ./cmd/flip --trigger-ask-max 0.2 --crash-min-ask 0.4 \
                  --crash-window 45 --dist-lo -0.5 --dist-hi 0 \
                  --rem-min 180 --stake 2 --mode paper
```

环境变量（无配置即只读运行）：
| 变量 | 说明 | 必填 |
|------|------|------|
| `POLYMARKET_OWNER_KEY` | 钱包私钥 | live 模式 |
| `POLYMARKET_CLOB_KEY/SECRET/PASSPHRASE` | CLOB 凭证 | live 模式 |
| `POLYMARKET_PROXY` | SOCKS5 代理（本地运行 Polymarket 必需） | 可选 |

> 本地运行记得 `export https_proxy=http://127.0.0.1:1087`（Polymarket 直连超时）；
> 部署机勿设指向不通代理的 HTTP(S)_PROXY（Binance 拨号走环境代理）。
