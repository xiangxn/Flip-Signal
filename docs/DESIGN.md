# MQS（市场质量评分系统）— Go 实现方案

## 背景

PRD 定义了一套量化评分系统，用于判断 **BTC 5分钟 Polymarket 市场在尾盘是否具有下注价值**。核心原则：**不预测价格方向，只评估市场状态是否足够健康**，使得尾盘方向性下注具有正期望。

系统输出 0-100 的综合评分，由 5 个维度加权合成：趋势质量(Trend)、市场噪声(Noise)、趋势健康(Health)、订单流(Flow)、流动性(Liquidity)。交易决策完全基于规则引擎的阈值判断。

已有两个可复用的 Go 包：
- **`go-polymarket-sdk`** — Polymarket REST/WS 客户端：订单簿实时推送(`MarketMonitor`)、BTC 价格推送(`CryptoPriceMonitor`)、下单/签名
- **`polypilot`** — 完整的交易机器人框架：`Engine` 驱动 `Feed` → `Probability` → `Strategy` → `Executor` 流水线，附带状态管理、风控、对账

本方案将 MQS 设计为**独立核心库** + **polypilot 集成层**，既能在 polypilot 内运行，也能作为独立服务部署。

---

## 项目结构

```
LastTrading/
├── cmd/
│   └── mqs/
│       └── main.go                       # 独立运行入口
├── internal/
│   ├── snapshot/
│   │   ├── types.go                      # Snapshot 结构体 (PRD §1.2)
│   │   ├── ring_buffer.go                # 线程安全环形缓冲区 (容量300 = 5分钟×60秒)
│   │   └── collector.go                  # 合并 BTC价格 + 订单簿 → 每秒生成Snapshot
│   ├── mqs/
│   │   ├── types.go                      # MarketQuality 输出结构体 (PRD §2)
│   │   ├── trend.go                      # 趋势评分 (§3): 效率比、R²、方向一致性
│   │   ├── noise.go                      # 噪声评分 (§4): 方向翻转、路径噪声比、波动率稳定性
│   │   ├── health.go                     # 健康评分 (§5): 动量衰减、成交量支撑、回撤
│   │   ├── flow.go                       # 订单流评分 (§6): 买卖比、签名流趋势
│   │   ├── liquidity.go                  # 流动性评分 (§7): 深度失衡、深度稳定性、点差
│   │   ├── engine.go                     # 编排器: 接收 []*Snapshot → 输出 MarketQuality
│   │   └── engine_test.go
│   ├── decision/
│   │   ├── rules.go                      # 交易准入规则 (§9): 禁止/标准/强信号
│   │   ├── recorder.go                   # JSON 决策日志 (§10)
│   │   └── rules_test.go
│   ├── feed/
│   │   ├── binance_adapter.go            # Binance WS: BTC价格/成交量/深度/订单流
│   │   ├── orderbook_adapter.go          # 封装 MarketMonitor → Polymarket 订单簿 channel
│   │   └── snapshot_feed.go              # 合并 Binance BTC + Polymarket → EventBus
│   └── strategy/
│       └── mqs_strategy.go               # polypilot Strategy 实现: OnUpdate → 计算MQS → 返回OrderIntent
├── config.example.yaml
├── go.mod
├── prd.md
└── DESIGN.md
```

---

## 数据流

> **关键架构决策**：Snapshot 中的成交量、深度、订单流等字段来自 **BTC 现货市场数据（Binance）**，而非 Polymarket。Polymarket 只提供 YES/NO 盘口价格。

```
Binance WS (btcusdt@trade + @depth20)  ──→ BTC价格/成交量/深度/订单流 ──┐
                                                                       ├──→ SnapshotCollector (1秒) ──→ RingBuffer (300)
Polymarket MarketMonitor (WS)          ──→ YES/NO 盘口价格 ────────────┘          │
                                                                                  ▼
                                                                   MQS Engine (计算5个维度的评分)
                                                                                  │
                                                                                  ▼
                                                                   Decision Engine (应用§9规则)
                                                                                  │
                                                                  ┌───────────────┼───────────────┐
                                                                  ▼               ▼               ▼
                                                             禁止交易        标准交易         强信号加仓
```

### 数据源分工

| 数据字段 | 来源 | 协议 |
|----------|------|------|
| Price | Binance `btcusdt@trade` | WebSocket |
| OpenPrice | Binance REST `/api/v3/klines?interval=5m` | HTTP |
| Return1s/5s/10s/30s | Collector 从 RingBuffer 历史计算 | 本地计算 |
| HighFromOpen, LowFromOpen | Collector 扫描 RingBuffer | 本地计算 |
| BuyVolume, SellVolume | Binance `btcusdt@trade`（根据 aggressor side 区分买卖） | WebSocket |
| SignedFlow | 本地计算: BuyVolume - SellVolume | 本地计算 |
| Volatility | Collector 计算收益率标准差 | 本地计算 |
| BidDepth/AskDepth | Binance `btcusdt@depth20@100ms` | WebSocket |
| YesPrice, NoPrice | Polymarket `MarketMonitor` (CLOB order book) | WebSocket |

---

## 各模块详细设计

### 1. Snapshot 层 (`internal/snapshot/`)

**`types.go`** — 严格对应 PRD §1.2 的字段定义：
```go
type Snapshot struct {
    Timestamp    int64
    MarketID     string
    RemainingSec int

    // BTC 价格
    OpenPrice float64
    Price     float64

    // 价格路径（通过与环形缓冲区历史数据对比计算）
    Return1s       float64
    Return5s       float64
    Return10s      float64
    Return30s      float64
    ReturnFromOpen float64

    // OHLC (窗口内)
    HighFromOpen float64
    LowFromOpen  float64

    // 成交量
    BuyVolume1s  float64
    SellVolume1s float64
    BuyVolume10s  float64
    SellVolume10s float64

    // 订单流
    SignedFlow10s float64

    // 波动率
    Volatility10s float64
    Volatility30s float64

    // 深度 (前N档)
    BidDepth5  float64
    AskDepth5  float64
    BidDepth10 float64
    AskDepth10 float64

    // Polymarket 盘口
    YesPrice float64
    NoPrice  float64
}
```

**`ring_buffer.go`** — 线程安全环形缓冲区，容量 300（5分钟 × 60秒）。
- `Push(s *Snapshot)` — O(1)
- `Window(n int) []*Snapshot` — 返回最近 n 个样本
- `Last() *Snapshot` — O(1) 最新样本
- `SnapshotAt(ago int) *Snapshot` — 返回 N 秒前的样本

**`collector.go`** — 核心逻辑：1 秒定时器驱动：
- 从 `CryptoPriceMonitor` 读取最新 BTC 价格
- 从 `MarketMonitor` 读取最新订单簿
- 与 RingBuffer 中历史数据对比，计算 `Return1s/5s/10s/30s`
- 扫描窗口计算 OHLC (HighFromOpen, LowFromOpen)
- 通过累计成交量差值计算各时段成交量
- 从订单簿前 N 档计算深度
- 波动率 = 窗口内收益率的标准差

---

### 2. MQS 引擎 (`internal/mqs/`)

**`types.go`** — 输出结构体：
```go
type MarketQuality struct {
    TrendScore     float64 // 0-100  趋势质量
    NoiseScore     float64 // 0-100  噪声（越高越乱）
    HealthScore    float64 // 0-100  趋势健康度
    FlowScore      float64 // 0-100  订单流
    LiquidityScore float64 // 0-100  流动性
    Total          float64 // 0-100  加权综合评分
}
```

**`engine.go`** — `func ComputeMQS(window []*Snapshot) MarketQuality`：
- 调用各子评分器
- 应用最终加权公式（§8）：
  ```
  Total = Trend×0.30 + (100-Noise)×0.25 + Health×0.20 + Flow×0.15 + Liquidity×0.10
  ```

**各子评分器**（每个文件计算一个维度）：

| 文件 | 维度(权重) | 子指标 | 窗口 |
|------|-----------|--------|------|
| `trend.go` | 趋势(30%) | 效率比 ER、趋势 R²、方向一致性 | 30秒 |
| `noise.go` | 噪声(25%) | 方向翻转次数、路径噪声比、波动率稳定性(Vol10s/Vol60s) | 30秒 |
| `health.go` | 健康(20%) | 动量衰减(后10s vs 前10s)、成交量支撑比、回撤比例 | 20秒 |
| `flow.go` | 订单流(15%) | 买入占比、签名流趋势持续性 | 10秒/30秒 |
| `liquidity.go` | 流动性(10%) | 深度失衡(Bid/(Bid+Ask))、深度稳定性、点差 | 10秒 |

每个子指标根据 PRD 阈值返回 0/40/50/60/70/100 分。每个评分器按 PRD 指定的权重合成子指标。

#### 各维度详细算法

##### Trend Score — 趋势质量（权重 30%）

**子指标 1: Efficiency Ratio（效率比，权重 0.4）**
```
ER = |Price_now - Price_30s_ago| / Σ|每秒价格变化|
```
- ER ≥ 0.7 → 100分
- 0.5-0.7 → 70分
- 0.3-0.5 → 40分
- <0.3 → 0分

**子指标 2: Trend R²（趋势拟合度，权重 0.35）**
- 对过去30秒的价格做线性回归 `price = a*time + b`
- R² > 0.8 → 100分
- 0.6-0.8 → 70分
- 0.4-0.6 → 40分
- <0.4 → 0分

**子指标 3: Direction Consistency（方向一致性，权重 0.25）**
- 统计过去30秒每秒 return 的方向
- positive_count / total > 75% → 100分
- 60-75% → 70分
- 50-60% → 40分
- <50% → 0分

**合成**：`TrendScore = ER×0.4 + R²×0.35 + Direction×0.25`

---

##### Noise Score — 市场噪声（权重 25%）

> 注意：这个分越高代表越乱。最终计算时使用 `100 - NoiseScore`。

**子指标 1: Direction Flip（方向翻转次数，权重 0.4）**
- 统计30秒内价格方向改变的次数
- 0-2次 → 100分
- 3-5次 → 50分
- >5次 → 0分

**子指标 2: Return Noise Ratio（路径噪声比，权重 0.4）**
```
Noise = 实际路径长度 / 净移动距离
```
- Noise < 1.5 → 100分
- 1.5-3 → 60分
- >3 → 0分

**子指标 3: Volatility Stability（波动率稳定性，权重 0.2）**
```
Ratio = Vol10s / Vol60s
```
- 0.8-1.5 → 100分
- 1.5-2 → 50分
- >2 → 0分

**合成**：`NoiseScore = Flip×0.4 + PathNoise×0.4 + VolStable×0.2`

---

##### Health Score — 趋势健康（权重 20%）

**子指标 1: Momentum Decay（动量衰减，权重 0.4）**
- 比较最近10秒动量 vs 之前10秒动量
- 衰减 < 30% → 100分
- 30-60% → 60分
- >60% → 0分

**子指标 2: Volume Support（成交量支撑，权重 0.3）**
```
Ratio = Volume_last10 / Volume_prev10
```
- > 1.2 → 100分
- 0.8-1.2 → 60分
- < 0.8 → 0分

**子指标 3: Retracement（回撤，权重 0.3）**
```
Retracement = (high - price) / (high - open)
```
- < 30% → 100分
- 30-60% → 50分
- > 60% → 0分

**合成**：`HealthScore = Momentum×0.4 + Volume×0.3 + Retracement×0.3`

---

##### Flow Score — 订单流（权重 15%）

**子指标 1: Buy Ratio（买入占比，权重 0.5）**
```
Ratio = BuyVolume / (BuyVolume + SellVolume)
```
- > 65% → 100分
- 55-65% → 70分
- 50-55% → 40分
- < 50% → 0分

**子指标 2: Signed Flow Trend（签名流趋势，权重 0.5）**
- 统计过去30秒 SignedFlow10s 的方向一致性
- > 80% 同号 → 100分
- 60-80% → 60分
- 否则 → 0分

**合成**：`FlowScore = BuyRatio×0.5 + FlowTrend×0.5`

---

##### Liquidity Score — 流动性（权重 10%）

**子指标 1: Depth Imbalance（深度失衡，权重 0.4）**
```
Imbalance = Bid / (Bid + Ask)
```
- 接近 0.5 最好，偏离越大越差
- 0.4-0.6 → 100分
- 0.3-0.4 或 0.6-0.7 → 50分
- 否则 → 0分

**子指标 2: Depth Stability（深度稳定性，权重 0.4）**
- 比较当前深度与10秒前深度的变化
- 变化 < 20% → 100分
- 20-50% → 50分
- > 50% → 0分

**子指标 3: Spread（点差，权重 0.2）**
```
Spread = YesPrice + NoPrice - 1
```
- < 0.02 → 100分
- 0.02-0.05 → 50分
- > 0.05 → 0分

**合成**：`LiquidityScore = Imbalance×0.4 + Stability×0.4 + Spread×0.2`

---

### 3. 决策引擎 (`internal/decision/`)

**`rules.go`** — 实现 PRD §9 的交易准入规则：

```go
type Decision int
const (
    DecisionForbidden Decision = iota  // 禁止: RemainingSec<10 | Liquidity<40 | Noise>70
    DecisionNoTrade                     // 不交易: MQS < 75，但未被禁止
    DecisionStandard                    // 标准交易: MQS >= 75, 剩余10-60秒
    DecisionStrong                      // 强信号: MQS>=85 & Trend>=80 & Noise<=30 & Health>=70
)
```

`func Decide(mq MarketQuality, remainingSec int) (Decision, string)`
- 首先检查禁止条件（硬拒绝）
- 然后检查强信号条件
- 再检查标准交易条件
- 都不满足则为 NoTrade

**`recorder.go`** — 写入 JSON 格式决策日志（§10）：
```go
type DecisionRecord struct {
    Time      string  `json:"time"`
    Remaining int     `json:"remaining"`
    MQS       float64 `json:"MQS"`
    Trend     float64 `json:"trend"`
    Noise     float64 `json:"noise"`
    Health    float64 `json:"health"`
    Flow      float64 `json:"flow"`
    Liquidity float64 `json:"liquidity"`
    BTCReturn string  `json:"btc_return"`
    Decision  string  `json:"decision"`
    Result    string  `json:"result"`
}
```
- 每行一个 JSON 对象，追加写入 `mqs_decisions.jsonl`
- `Result` 字段在市场结算后回填

---

### 4. Feed 适配层 (`internal/feed/`)

**`binance_adapter.go`** — 直接连接 Binance WebSocket，单连接多流复用：
- 流 1: `btcusdt@trade` → 实时成交价 + 根据 aggressor side 区分买卖量
- 流 2: `btcusdt@depth20@100ms` → 订单簿深度（前 5/10 档买卖盘）
- REST: `GET /api/v3/klines?symbol=BTCUSDT&interval=5m&limit=1` → 当前5分钟K线开盘价
- 对外暴露 `BinanceBTCMarketData`（价格、深度、成交量）
- 10 秒滚动窗口成交量统计

**`orderbook_adapter.go`**：
- 封装 `MarketMonitor`，订阅指定市场的 token ID
- 对外暴露 `<-chan *OrderBook` 最新 Polymarket 订单簿
- 每次 WS book 更新 → channel

**`snapshot_feed.go`** — 合并两个数据源，1秒生成 Snapshot：
- BTC 数据来自 `BinanceAdapter`（价格、成交量、深度）
- Polymarket 数据来自 `OrderBookAdapter`（YES/NO 盘口价）
- 调用 `mqs.ComputeMQS(ringBuffer.Window(300))` 计算评分
- 发布 Snapshot + MQS 到输出 channel

---

### 5. Polypilot 策略 (`internal/strategy/`)

**`mqs_strategy.go`** — 实现 `runtime.Strategy` 接口：

```go
func (s *MQSStrategy) OnUpdate(e core.Event, o runtime.Observation, snap state.Snapshot) []runtime.OrderIntent {
    // 1. 从 event/observation features 中提取 MQS
    // 2. 根据市场结束时间计算剩余秒数
    // 3. 调用 decision.Decide(mq, remainingSec)
    // 4. 如果是 Standard 或 Strong → 返回 BUY OrderIntent
    // 5. 如果是 Forbidden → 取消所有挂单
}
```

策略决策逻辑：
- **方向选择**：对比当前 BTC 价格与开盘价 — 涨则 BUY YES，跌则 BUY NO
- **仓位管理**：标准信号 = 基础仓位，强信号 = 2× 基础仓位
- **去重**：通过 state snapshot 追踪已下单，避免重复下单

---

### 6. 独立入口 (`cmd/mqs/main.go`)

独立运行 MQS 服务：
1. 加载配置（Polymarket API 密钥、市场 slug、策略参数）
2. 初始化 `PolymarketClient`
3. 启动 `CryptoPriceMonitor`（BTC）+ `MarketMonitor`（目标市场）
4. 启动 1 秒 Snapshot 采集器
5. 每次 Snapshot → 计算 MQS → 评估决策 → 记录日志
6. 收到 SIGTERM/SIGINT 时优雅关闭

---

## 复用已有包

| 功能 | 来源 | 使用方式 |
|------|------|----------|
| Polymarket REST/WS 客户端 | `go-polymarket-sdk/polymarket.PolymarketClient` | 直接使用 `NewClient(cfg)` |
| Polymarket 订单簿推送 | `go-polymarket-sdk/polymarket.MarketMonitor` | 订阅市场 token，获取 YES/NO 盘口 |
| Polymarket 订单类型 | `go-polymarket-sdk/orders` | `Book`, `Side`, `OrderType` 等 |
| Polymarket 配置模式 | `go-polymarket-sdk/polymarket.Config` | 复用 viper/mapstructure 模式 |
| Binance WebSocket | `github.com/gorilla/websocket` | 直连 `wss://stream.binance.com:9443` |
| 策略接口 | `polypilot/runtime.Strategy` | 实现该接口以集成到 polypilot |
| 事件总线 | `polypilot/core.EventBus` | 发布 MQS 事件 |
| 引擎编排 | `polypilot/runtime.Engine` | Feed → Probability → Strategy 流水线 |

---

## 实现顺序

1. **`internal/snapshot/types.go` + `ring_buffer.go`** — 基础数据结构，不依赖网络
2. **`internal/mqs/`** 全部 5 个评分器 + engine — 纯计算逻辑，可离线单元测试
3. **`internal/decision/`** — 交易规则 + 日志记录器
4. **`internal/feed/binance_adapter.go`** — Binance WS: BTC 价格/成交量/深度（单连接多流）
5. **`internal/feed/orderbook_adapter.go`** — Polymarket 订单簿适配器
6. **`internal/feed/snapshot_feed.go`** — 合并 Binance BTC + Polymarket YES/NO → Snapshot
7. **`internal/strategy/mqs_strategy.go`** — polypilot 集成层
8. **`cmd/mqs/main.go`** — 独立运行二进制
9. **`config.example.yaml`** — 配置模板

---

## 验证方案

1. **单元测试**：每个 MQS 评分器用人工构造的 Snapshot 窗口测试，验证评分结果符合 PRD 阈值表
2. **集成测试**：`engine_test.go` 喂入 300 样本的合成窗口，验证 5 个评分均在 [0,100] 范围内，Total 符合加权公式
3. **实盘观察**：运行 `cmd/mqs` 连接真实 Polymarket BTC 5分钟市场，观察 MQS 评分随时间变化，验证日志正确写入
4. **polypilot 集成**：将 `MQSStrategy` 注册到 polypilot engine 配置中，验证能正常接收事件并产生 OrderIntent
