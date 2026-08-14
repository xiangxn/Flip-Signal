# CLAUDE.md — Flip Signal

## 项目概述

**Flip Signal** 是一个针对 **Polymarket BTC 5分钟市场** 的量化交易系统。核心引擎 **Flip Signal Detection** 检测 Polymarket YES/NO 价格穿越 0.7 后的反转信号，基于 Formula B 精简公式（背离硬要求 + 过度自信 + 确认回归）进行方向性下注。

### 核心原则
> **不预测涨跌，只判断"什么时候市场过度自信"。**

### 当前策略版本：Formula B 频率优化版（2026-08-13 标定）
- 完整方案见 `docs/flip_strategy_plan_2026-08-13.md`，分析过程见 `docs/flip_optimization_analysis_2026-08-13.md`，
  频率优化见 `docs/flip_frequency_optimization_2026-08-13.md`
- 三个信号条件：B1 背离硬要求（`divergence_floor=0.0`，BTC 不得与 PM 同向，div<0 直接否决）、
  B2 过度自信（`range_expansion<1.0` → +2）、B3 确认回归（`other_delta` 三档 → +3/+2/+1）
- `score_entry=2`：任一核心信号成立即触发
- 已停用：振荡/低价入场/btc_extreme 加分（权重 0）、path_eff<0.4 与 noise>3 否决（阈值 0）
- **成交口径**：`yes_price/no_price` 存的是各订单簿 **best bid**，回测与实盘的成交价/gate 一律按对侧
  **ask = 1 - 触发侧 bid** 计算（Go 引擎 gate 已按 ask 口径实现，见方案文档 §5）
- **实盘注意**：`max_latency_ms=300` 的 L0 延迟否决只在实盘生效（回测 data_0 无 latency 字段），
  实盘信号率会低于回测基准
- **结算口径（2026-08-14）**：btc-updown-5m 以 **Chainlink TWAP-60** 判定胜负，B1/B2/F0 的
  BTC 侧输入已切换为 TWAP（Binance 仅研究对照）。⚠️ 现有阈值（range 1.0/1.5、gate 0.45 等）
  基于 Binance 口径标定，待 TWAP lab 数据积累后需重标定

---

## 项目结构

```
FlipSignal/
├── cmd/
│   ├── flip/main.go                     # Flip Signal 交易引擎主入口（组件初始化 + 市场循环）
│   ├── lab/main.go                      # 实验室数据采集（ResearchSnapshot）
│   └── test_resolve/main.go             # 结算测试工具
├── internal/
│   ├── config/
│   │   └── config.go                    # AppConfig + Load() + 敏感字段解密
│   ├── lab/
│   │   ├── types.go                     # ResearchSnapshot + Event 结构体
│   │   ├── collector.go                 # 5秒生成 ResearchSnapshot
│   │   └── writer.go                    # JSONL 事件持久化（按日切分）
│   ├── flip/
│   │   ├── types.go                     # FlipConfig + FlipSignal + ScoreParams
│   │   ├── engine.go                    # 状态机: Watching → Confirming → Done
│   │   ├── features.go                  # 特征提取纯函数: PathEff, NoiseRatio, CountFlips…
│   │   ├── scoring.go                   # Formula B 评分（背离硬要求 + 过度自信 + 确认回归）
│   │   ├── hist_range.go               # 历史K线波动范围追踪器
│   │   ├── recorder.go                  # JSONL 信号记录 + P&L 结算
│   │   └── engine_test.go              # 单元测试
│   ├── feed/
│   │   ├── binance_adapter.go           # Binance WS: 价格/量/深度（研究对照）
│   │   ├── orderbook_adapter.go         # Polymarket WS: YES/NO 盘口
│   │   └── twap_adapter.go              # Chainlink TWAP-60 流（结算基准）
│   └── dashboard/
│       ├── server.go                    # HTTP server
│       ├── handlers.go                  # /api/state, /api/signals, /api/snapshots…
│       ├── state.go                     # 运行时组件引用
│       ├── templates.go                 # 嵌入式 HTML 模板
│       └── static/                      # app.js, style.css, index.html
│   └── trading/
│       ├── types.go                     # TradingConfig + OrderRecord + Position
│       ├── risk.go                      # 纯函数风险检查
│       ├── order.go                     # 信号→FAK 订单映射
│       ├── client.go                    # TradeClient 接口 + SDK 适配器
│       ├── recorder.go                  # 交易 JSONL 日志
│       └── trader.go                    # 核心 Trader：开关、下单、结算
├── docs/                                # 策略设计与分析文档
├── config.example.yaml
├── go.mod / go.sum
└── CLAUDE.md                           # 本文件
```

---

## 架构与数据流

```
               Polymarket RTDS          Polymarket CLOB          Binance
             ┌─ WS(Chainlink) ─┐      ┌──── WS ──────┐      ┌── WS ──────┐
             │ TWAP-60 (60s)   │      │ MarketMonitor│      │aggTrade    │
             │ 结算基准价格     │      │ (CLOB books) │      │+depth20    │
             └───────┬─────────┘      └──────┬───────┘      └─────┬──────┘
                     │                       │                    │
                     ▼                       ▼                    ▼
                 TWAP价格              YES/NO价格(best bid)   BTC价格/量/深度
                     │                       │              （研究对照）
                     └───────────┬───────────┘
                                 ▼
                      lab.Collector (5秒定时器)
                                 │
                                 ▼
                         ResearchSnapshot
                                 │
                                 ▼
                          Flip Engine
                   (状态机 + Formula B)
                                 │
                                 ▼
                          FlipRecorder
                       (JSONL + P&L结算)
```

### 数据源分工

| 数据 | 来源 | 方式 |
|------|------|------|
| **TWAP-60 价格（特征基准）** | Polymarket RTDS `crypto_prices_twap_sixty`（Chainlink） | WebSocket（SDK `CryptoPriceMonitor`）|
| **TWAP 官方开/收盘价** | Polymarket REST `api/crypto/crypto-price`（twapEnabled+60s，SDK `FetchOpenPrice`） | HTTP —— 开盘价从 5 分整边界起轮询（接口有数据延迟）；收盘价窗口结束后轮询修正；启动时拉最近 N 个窗口预热历史振幅 |
| BTC 价格/量/深度（研究对照） | Binance `btcusdt@trade` + `@depth20` | WebSocket |
| BTC 5m 开盘价（研究对照） | Binance REST `/api/v3/klines?interval=5m` | HTTP（每周期）|
| YES/NO 盘口 | Polymarket `MarketMonitor` | WebSocket |

### 市场循环流程

```
1. 计算下个 5分钟对齐时间戳
2. 等待窗口开始 + 2秒（确保 Binance kline 已生成）
3. GET /api/v3/klines → Binance K线开盘价（研究对照）；采样 TWAP-60 开盘价（特征基准）
4. GET gamma-api/markets/slug/btc-updown-5m-<ts> → 获取市场信息
5. MarketMonitor.SubscribeTokens() → 订阅新市场的 YES/NO token
6. Collector.StartEvent() → 设置 conditionID/openPrice/endTime
7. 每5秒采集 ResearchSnapshot + Flip Engine 检测（持续 ~298秒）
8. 市场结束 → FinalizeEvent → Resolve → UnsubscribeTokens → goto 1
```

### Flip Engine 状态机

```
Idle ──Reset()──▶ Watching ──price>0.7──▶ Confirming ──score≥5──▶ Done (🎯 Signal)
                       ▲                        │
                       └── fallback ────────────┘（另一侧重试）
```

- **Watching**: 累积 snapshot，追踪 YES/NO 首次穿越 0.7 的位置
- **Confirming**: 等待 T+N ticks 确认，计算 other_delta 和最终评分
- **Done**: 本周期已产生信号，后续 snapshot 被忽略
- **first_crossing_only**: 先试 YES，失败则 fallback 到 NO；两侧都失败则回到 Watching

### Formula B 精简评分（2026-08-13 频率优化版）

三个信号条件，与策略哲学一一对应：

| 条件 | 说明 | 权重 |
|------|------|------|
| **B1 背离硬要求** | TWAP 不得与 PM 同向：`divergence_floor=0.0`（div<0 直接否决）。`min_divergence`（默认 0）可额外要求背离强度。⚠️ χ²=125 证据基于 Binance 口径，待 TWAP 数据重标定 | 硬过滤 |
| B3 确认回归 OtherDelta | other_delta > 0.05 / 0.02 / 0.01 | +3 / +2 / +1 |
| **B2 过度自信 RangeExpansion** | range_expansion < 1.0（TWAP 移动不足 1 个历史振幅但 PM 已 0.7+）⚠️ 阈值待 TWAP 重标定 | +2 |
| F0 Veto | range_expansion ≥ 1.5（真突破，PM 是对的）⚠️ 阈值待 TWAP 重标定 | 否决 |
| **L0 TWAP 新鲜度** | twap_age > `max_twap_age_ms`（默认 5000ms，TWAP-60 推送间隔约 2s）→ 特征基于过期价格 | 否决 |
| **Price Gate（ask 口径）** | 确认时刻对侧 **ask = 1 - 触发侧 bid** > max_entry_price（0.45，与 trading.max_price 一致） | 信号无效 |

- `score_entry = 2`：任一核心信号成立即触发（od>0.02 或 range<1.0），无需特征堆叠
- 窗口：`min_remaining_sec=15`（穿越 rem≥20 触发，确认 2 ticks 后 FAK 仍有 ~10s 缓冲；
  窗口尾部信号质量最高，见频率优化文档 §2.1）
- **已删除**（2026-08-13 Formula B 落地时从配置与代码中移除）：F3 振荡、F4/F5 低价入场、
  F7 btc_extreme 加分、path_eff<0.4 与 noise>3 否决、`allow_retry_crossings` 兼容模式
  （引擎始终多穿越重试）。详见方案文档 §2
- **成交口径**：`yes_price/no_price` 存的是各订单簿 **best bid**。实盘 FAK 买入对侧
  成交在 **ask = 1 - 触发侧 bid**（双 token 互补）。回测 fill 与 gate 均按 ask 口径
  （Go 引擎已实现，与 trader.go 最优卖价校验一致），见方案文档 §5.2
- 基准表现（data_0, 1719 事件）：n=364、2.54 信号/小时、WR 45.6%、EV +0.234/笔、
  P&L +85.10、PF 2.89、avg_fill 0.222；6 个完整日全部正 EV（频率优化详情见
  `docs/flip_frequency_optimization_2026-08-13.md`）

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

### 包设计

- **标准库优先**: 能用标准库就不引入第三方依赖
- **零外部依赖核心**: `internal/flip/` 和 `internal/lab/` 不依赖任何外部包，可独立测试
- **接口隔离**: SDK 集成层（`internal/feed/`）与核心计算层分离
- **纯函数优先**: 特征提取（`features.go`）和评分（`scoring.go`）为纯函数，无副作用，易于测试

### 注释规范

- **注释语言一律使用中文**，技术专有名词保留英文（如 BTC、PM、path_eff、noise_ratio、YES/NO 等）
- **数学公式、符号映射保留原样**（如 `§2.2: path_eff = |lastPrice - openPrice| / pre_range`、`POLYMARKET_* → sdk.polymarket.*`）
- 注释应描述逻辑意图而非复述代码，做到专业、精准、不直译

### 命名约定

- **文件名**: `snake_case.go`
- **包名**: 小写单词，与目录名一致
- **导出类型**: `PascalCase`（如 `FlipConfig`, `ResearchSnapshot`）
- **导出函数/方法**: `PascalCase`（如 `NewEngine`, `ProcessSnapshot`）
- **私有函数/方法**: `camelCase`（如 `enterConfirming`, `onConfirmed`）
- **私有常量**: `camelCase`（如 `stateWatching`）
- **包级文档**: 每个包首行描述包职责

### 结构体与配置

- 配置使用专用 struct + `DefaultConfig()` 工厂函数返回默认值
- 所有可调参数集中在 config struct 中，通过注释标注出处（如 `§2.1`, `Formula B`）
- 字段对齐用 tab 对齐到列，提高可读性
- 对外暴露的 struct 字段加 JSON tag

```go
type FlipConfig struct {
    TriggerThreshold float64 // PM price > this triggers detection (0.7)
    MinPreSnaps      int     // Minimum snapshots before crossing (5)
    // ...
}

func DefaultConfig() FlipConfig {
    return FlipConfig{
        TriggerThreshold: 0.7,
        MinPreSnaps:      5,
    }
}
```

### 状态机

- 用 `type xxxState int` + `const` iota 定义状态枚举
- 每个状态用 `switch e.state { case stateXxx: ... }` 分发
- 状态命名: `state` 前缀 + 形容词（`stateWatching`, `stateConfirming`, `stateDone`）

### 并发安全

- **原则**: 只在必要的边界加锁，避免过度同步
- `sync.Mutex` 保护写操作（文件写入、连接切换）
- `sync.RWMutex` 保护读多写少的数据
- `atomic.Bool` / `atomic.Int64` 用于简单标志位
- Dashboard getter 与主循环 goroutine 之间通过注释说明内存可见性假设
- Context 用于优雅关闭：`signal.NotifyContext(ctx, os.Interrupt, syscall.SIGTERM)`

```go
type Writer struct {
    mu         sync.Mutex
    dir        string
    file       *os.File
    buf        *bufio.Writer
    currentDay string
}

func (w *Writer) Write(event *Event) error {
    w.mu.Lock()
    defer w.mu.Unlock()
    // ...
}
```

### 错误处理

- 函数返回 `(result, error)`，不 panic
- 用 `fmt.Errorf("context: %w", err)` 包装错误，保留调用链
- 初始化阶段无法继续的错误用 `log.Fatalf`，运行中错误用 `log.Printf` + continue
- 对异步操作（goroutine + channel）设超时或 select on ctx.Done()

### 日志规范

- `log.Printf("[ComponentName] message")` — 统一前缀格式
- 关键状态用 emoji: ⚠️🔒🟢🔴🟡🔥🎯
- 禁止 `fmt.Println` 用于运行日志
- 日志级别用前缀区分：`[Flip]`, `[Cycle]`, `[Event]`

### I/O 规范

- 文件写入用 `bufio.Writer` 包装，减少系统调用
- JSONL 格式：每行一个 JSON object，适合流式处理
- 文件追加模式（`O_APPEND`）支持重启不丢数据
- `defer Flush() + Close()` 确保数据落盘

### 测试规范

- 测试文件命名：`*_test.go`
- 纯函数用 table-driven test（多组输入/输出）
- 状态机测试模拟完整的 Snapshot 序列
- 每个测试函数覆盖一个具体场景，命名：`Test<Component>_<Scenario>`

```go
func TestPathEfficiency_Trending(t *testing.T) { ... }
func TestPathEfficiency_Oscillating(t *testing.T) { ... }
func TestEngine_CrossingWithConfirm(t *testing.T) { ... }
```

### 方法接收者

- 需要修改接收者的方法用指针接收者 `(e *Engine)`
- 纯读取且接收者较小（< 64 bytes）也用指针接收者保持一致性
- 返回拷贝的方法用值接收者（如 `(e *Engine) Config() FlipConfig`）

### import 分组

```go
import (
    // 标准库
    "fmt"
    "log"
    "time"

    // 第三方库
    "github.com/tidwall/gjson"

    // 项目内部
    "github.com/necklace/flip-signal/internal/flip"
)
```

---

## 如何修改

### 添加新的评分特征
1. 在 `internal/flip/types.go` 的 `FlipConfig` 和 `ScoreParams` 中添加新字段
2. 在 `internal/flip/scoring.go` 的 `ComputeFlipScore()` 中加入新特征
3. 在 `internal/flip/engine.go` 的 `enterConfirming()` 或 `onConfirmed()` 中计算特征值
4. **先在 Python 回测中验证**：新特征必须有候选层统计效力（χ²）+ 双 Split 样本外
   证据才可加入，避免重回特征堆叠老路（参见 Formula B 减法教训，2026-08-13）

### 添加新的数据源
1. 在 `internal/feed/` 新建 adapter
2. 在 `internal/lab/collector.go` 的 `Tick()` 中调用新数据源
3. 在 `ResearchSnapshot` 结构体中添加新字段

### 修改 Flip 检测参数
- 编辑 `internal/flip/types.go` 的 `DefaultConfig()` 函数
- 或启动时通过 flag 覆盖

### 测试
```bash
go test ./internal/flip/ -v           # Flip Engine 单元测试
go build ./...                         # 全量编译检查
go run ./cmd/flip -dashboard :8090     # 运行 Flip 检测 + Dashboard
```

---

## 关键设计决策

1. **Binance 直连而非通过 Polymarket 转发**: Polymarket 的 `CryptoPriceMonitor` 只提供价格，没有成交量、深度、订单流。

2. **TWAP-60 为唯一 BTC 特征口径（2026-08-14）**: btc-updown-5m 以 Chainlink TWAP-60
   判定胜负，B1/B2/F0 与历史振幅全部基于 TWAP（SDK `CryptoPriceMonitor` 订阅
   `crypto_prices_twap_sixty`）。Binance 数据保留仅作基差研究对照，禁止双口径特征叠加
   （Formula B 减法哲学）。官方开/收盘价经 `FetchOpenPrice`（crypto-price 接口）获取：
   开盘价从窗口边界起轮询（接口有数据延迟，最长 15s，超时回退流采样），收盘价窗口
   结束后轮询修正（最长 9s）。历史振幅基准默认**冷启动**（`hist_warmup_enabled=false`，
   主循环积累 ≥3 个窗口后 B1/B2 可用）—— 标定期保证基准口径来自单一采集管线；
   预热开关（拉官方历史振幅）在口径验证稳定后启用。

2. **单 WS 连接多流复用**: `btcusdt@trade` + `btcusdt@depth20` 合并为一个连接。

3. **OpenPrice 来自 5m K线而非首笔成交**: 与 Polymarket 5分钟窗口精确对齐。

4. **核心计算层零外部依赖**: `flip` 和 `lab` 包可独立编译、离线测试。

5. **无密钥只读模式**: 未配置私钥时自动生成临时密钥，仅用于数据读取。

6. **5秒采样**: 与 Polymarket CLOC 盘口更新频率匹配，减少噪声。

7. **多穿越重试**: 每个向上穿越 0.7 的上升沿都触发检测，首个评分通过者下注。每周期最多一注，YES 优先。已下注后不再观察后续穿越。

8. **纸面交易 + 实盘可选**: 默认纸面交易，配置 `trading.enabled: true` 或 `-trading` flag 启用 FAK 市价单实盘执行。需要有 CLOB 凭证。

---

## 环境变量

所有环境变量使用 `PM_` 前缀，点号替换为下划线，由 viper 自动映射到配置路径。
例如 `PM_SDK_POLYMARKET_OWNER_KEY` → `sdk.polymarket.owner_key`。

| 变量 | 说明 | 必填 |
|------|------|------|
| `PM_SDK_POLYMARKET_OWNER_KEY` | 钱包私钥（hex，支持加密存储）| 仅交易模式 |
| `PM_SDK_POLYMARKET_CLOB_CREDS_KEY` | CLOB API Key | 仅交易模式 |
| `PM_SDK_POLYMARKET_CLOB_CREDS_SECRET` | CLOB API Secret | 仅交易模式 |
| `PM_SDK_POLYMARKET_CLOB_CREDS_PASSPHRASE` | CLOB API Passphrase | 仅交易模式 |
| `PM_SDK_POLYMARKET_FUNDER_ADDRESS` | 代理钱包地址 | 可选 |
| `PM_SDK_SOCKS_PROXY` | SOCKS5 代理 | 可选 |
| `PM_CONFIG_DECRYPT_PASSWORD` | 解密密码（替代交互式输入）| 可选 |
| `PM_RUNTIME_SYMBOL` | Binance 交易对 | 可选 |
| `PM_FLIP_TRIGGER_THRESHOLD` 等 | Flip 引擎参数 | 可选 |

敏感字段（`owner_key`、`clob_creds.*`）支持 AES-256-CBC 加密存储。
用 `pmutils.NewEncryptor(password).Encrypt(plaintext)` 生成密文写入 config.yaml，
启动时通过 `PM_CONFIG_DECRYPT_PASSWORD` 或交互式终端输入密码解密。
