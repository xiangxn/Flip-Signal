# 实盘交易（Live Trading）模块实现方案

> 创建日期：2026-08-08 | 版本：v3 | 状态：待审阅

---

## 1. 背景

Flip Signal 纸面交易阶段已完成。新增实盘交易模块，在信号触发时向 Polymarket CLOB 提交 FAK 订单，依据实际成交与市场结算计算 realized P&L。

**核心原则**：简单优先、不破坏纸面路径、默认安全。

---

## 2. 设计决策

| 决策 | 结论 | 理由 |
|------|------|------|
| 订单类型 | **FAK**，直接提交，不做预校验 | 简单；FAK 按可用流动性立即成交，剩余自动取消 |
| 实盘开关 | `Enabled: bool`（true=实盘，false=纸面）| 最简单 |
| 日单量上限 | 不做 | 信号天然低频（每 5min 最多 1 个）|
| Dashboard | 不新增 API 或前端，只修改 State.Mode | 够用 |
| 环境变量 | 不新增绑定 | 配置文件 + CLI flags |
| 最高允许价格 | **0.35**（`max_price: 0.35`）| 直接设置绝对限价，非滑点百分比 |

---

## 3. 新包结构

```
internal/trading/
├── types.go      # TradingConfig + OrderRecord + Position + ExecutionState
├── risk.go       # 纯函数风险检查（零外部依赖）
├── order.go      # 信号→FAK 订单映射
├── client.go     # TradeClient 窄接口 + SdkClient 适配器（SDK 依赖仅此文件）
├── recorder.go   # 交易 JSONL 日志
├── trader.go     # 核心 Trader：开关、下单、结算
├── risk_test.go
├── order_test.go
└── trader_test.go
```

分层：SDK 依赖层(client.go) → 纯逻辑层(risk.go, order.go) → 执行层(trader.go)

---

## 4. 核心数据结构

### 4.1 TradingConfig

```go
type TradingConfig struct {
    Enabled              bool    // 启动时是否启用实盘（true=实盘，false=纸面）
    OutputPath           string  // 交易记录 JSONL 路径（默认 data/trades.jsonl）
    StakePerSignal       float64 // 每信号投入 USDC（默认 5.0）
    MaxPrice             float64 // 最高允许价格（默认 0.35）
    MaxDailyLoss         float64 // 日亏上限 USDC（默认 10.0）
    CooldownAfterLossSec int     // 亏损后冷却秒数（默认 300）
}
```

### 4.2 OrderRecord

```go
type OrderRecord struct {
    ID           string          // CLOB orderID
    ConditionID  string
    TokenID      string
    Side         string          // BUY
    TokenSide    string          // "yes" / "no"
    Price        float64         // 价格上限（maxPrice）
    Shares       float64         // 请求股数
    FilledShares float64         // 实际成交股数（从 GetOpenOrders 获取）
    AvgFillPrice float64         // 成交均价
    Success      bool
    ErrorMsg     string
    Signal       *flip.FlipSignal
    CreatedAt    time.Time
}
```

订单生命周期极简：构造 → FAK 提交 → 成功/失败。无挂单状态。

### 4.3 Position / ExecutionState

```go
type Position struct {
    ConditionID      string
    TokenID          string
    TokenSide        string    // "yes" / "no"
    Shares           float64
    AvgPrice         float64   // 实际加权成交价
    CostUSDC         float64   // 投入成本
    OrderID          string
    OpenedAt         time.Time
    SettledAt        time.Time
    Won              bool
    PnL              float64
    Outcome          int       // 0=Up 1=Down
    ResolutionSource string    // "ws" | "simulated_fallback"
}

type ExecutionState struct {
    DailyPnl         float64
    DailyLimitHit    bool
    CooldownUntil    time.Time
    CurrentCondition string
    Position         *Position
    LastSkipReason   string
}
```

---

## 5. 信号 → FAK 订单映射

### 5.1 方向映射

| FlipSignal.Side | 含义 | CLOB Side | 买入 Token | 持仓方向 |
|-----------------|------|-----------|------------|----------|
| `"yes"` | YES > 0.7，过度看涨 | BUY | NO token | 赌 DOWN |
| `"no"` | NO > 0.7，过度看跌 | BUY | YES token | 赌 UP |

### 5.2 FAK 订单构造流程

```
信号触发 (sig.EntryPrice, sig.Side)
  │
  ├── 1. 确定目标 token：side=="yes" → NO token；side=="no" → YES token
  │
  ├── 2. maxPrice = cfg.MaxPrice                          // 默认 0.35（直接限价）
  │
  ├── 3. SDK 创建 FAK 市价单（处理 tick size、price 校验、EIP-712 签名）：
  │      signedOrder, err = client.CreateMarketOrder(&UserMarketOrder{
  │          TokenID:   tokenID,
  │          Price:     &maxPrice,
  │          Amount:    StakePerSignal,    // BUY 的 Amount = USDC
  │          Side:      BUY,
  │          OrderType: MARKET_FAK,
  │      }, CreateOrderOptions{})
  │
  ├── 4. 提交（OrderType 是 POST 时的字段，不参与签名）：
  │      result, err = client.PostOrder(signedOrder, FAK, false)
  │      解析：result.Get("success").Bool()、result.Get("orderID").String()
  │
  └── 5. 成交确认：
         GetOpenOrders({Id: &orderID}) → SizeMatched, Status
```

### 5.3 关键点

- **不做预校验**：直接 FAK 提交，SDK 内部处理 tick size、price 校验、NegRisk、EIP-712 签名
- **FAK 语义**：按当前 orderbook ≤ maxPrice 的流动性立即成交，未成交部分自动取消
- **Size 由配置决定**：`Amount = StakePerSignal` USDC（BUY 的 Amount 语义是 USDC 金额）
- **无需超时/取消/TradeMonitor**：FAK 立即执行，无挂单状态
- **成交后确认**：PostOrder 只返回 orderID + success，用 GetOpenOrders 查看 SizeMatched

---

## 6. 风险管理

### 6.1 门控

```
OnSignal
  ├── enabled == false → skip
  ├── DailyLimitHit || DailyPnl ≤ -MaxDailyLoss → skip
  ├── now < CooldownUntil → skip
  └── OpenPosition → skip
```

### 6.2 参数

| 风险 | 配置 | 默认值 | 行为 |
|------|------|--------|------|
| 单信号仓位 | StakePerSignal | 5 USDC | USDC 预算 |
| 价格上限 | MaxPrice | 0.35 | 最高允许价格（绝对限价）|
| 日亏 | MaxDailyLoss | 10 USDC | 触发当日停止，UTC 零点重置 |
| 连续亏损 | CooldownAfterLossSec | 300s | 亏损结算后冷却 |
| 并发持仓 | — | 固定 1 | 已有持仓拒单 |

---

## 7. 结算

```
OnCycleEnd(conditionID)
  │
  ├── 无持仓 → 返回
  └── 有持仓：
       ├── GTC 挂单对账（TradeMonitor 已实时追踪）
       ├── 创建 Position 并注册到 ResolutionPoller
       └── ResolutionPoller 异步轮询 gamma API 等待结算
          ├── umaResolutionStatus=="resolved" → settleWithOutcome("poller")
          └── 不设超时：持续轮询直到 Polymarket 完成结算
```

纸面路径 flipRecorder.Resolve() 保持不变，两条 P&L 各自独立。

---

## 8. main.go 集成

### 8.1 构造点

```go
var trader *trading.Trader
if !readOnly {
    tradeClient := &trading.SdkClient{Client: client}
    trader = trading.NewTrader(cfg.Trading, tradeClient, bookAdapter.SubscribeResolved())
    trader.Start(ctx) // 后台：结算监听 + 日志重放
    if cfg.Trading.Enabled {
        trader.Enable()
    }
}
```

### 8.2 周期钩子（3 处追加，不修改现有代码）

**A. 周期开始**（订阅 token 后）：
```go
if trader != nil { trader.NewCycle(conditionID, yesTokenID, noTokenID) }
```

**B. 信号发射点**（flipRecorder.RecordSignal 之后）：
```go
if trader != nil { trader.OnSignal(sig) }
```

**C. 周期结束**（flipRecorder.Resolve 之后）：
```go
if trader != nil { trader.OnCycleEnd(event.ConditionID, event.Outcome) }
```

### 8.3 AppConfig

```go
type AppConfig struct {
    Runtime RuntimeConfig           // 不变
    SDK     sdk.Config              // 不变
    Binance feed.BinanceConfig      // 不变
    Flip    flip.FlipConfig         // 不变
    Trading trading.TradingConfig   // 新增
}
```

---

## 9. Dashboard

不新增 API 或前端。仅修改 `State.Mode`：
- 纸面 `Mode = "paper"`
- 实盘 `Mode = "live"`

`State` 新增可选 `Trader *trading.Trader` 字段用于 Mode 判断。

---

## 10. 配置

### 10.1 config.example.yaml

```yaml
trading:
  enabled: false               # true=实盘，false=纸面
  output_path: "data/trades.jsonl"
  stake_per_signal: 5.0
  max_price: 0.35              # 最高允许价格
  max_daily_loss: 10.0
  cooldown_after_loss_sec: 300
```

### 10.2 CLI flags

| Flag | 说明 |
|------|------|
| `-trading` | 启动时启用实盘 |
| `-stake` | 每信号投入 USDC |
| `-max-loss` | 日亏上限 USDC |

### 10.3 环境变量

不新增。

---

## 11. 测试

- **risk_test.go** — CheckRisk 各路径、ComputeShares、CalcPnL、IsNewDay
- **order_test.go** — 方向映射、FAK 参数构造
- **trader_test.go** — mockClient：Enable/Disable、OnSignal（正常+风控拦截）、结算（WS+回退）

---

## 12. 实施顺序

| 步骤 | 内容 | 依赖 |
|------|------|------|
| 1 | `types.go` + `risk.go` + `risk_test.go` | 无 |
| 2 | `order.go` + `order_test.go` | 1 |
| 3 | `client.go` + `recorder.go` | 1 |
| 4 | `trader.go` + `trader_test.go` | 1-3 |
| 5 | `main.go` 集成 | 4 |
| 6 | `config.example.yaml` + `CLAUDE.md` 更新 | 5 |
| 7 | `dashboard/state.go` Mode 更新 | 4 |
| 8 | 小额实盘冒烟测试 | 5-7 |

步骤 1-4 不触碰任何现有文件。
