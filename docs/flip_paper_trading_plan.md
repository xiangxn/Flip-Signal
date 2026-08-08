# Flip 信号纸面交易 — Go 实施计划

> 将 Python `backtest_flip_scoring.py` 的翻转信号检测逻辑，基于 **lab 数据管线**实现为实盘纸面交易。

---

## 1. 核心原则

- **数据源**：使用 `internal/lab` 的 `ResearchSnapshot`（5s 间隔），与 Python 回测完全一致
- **架构**：遵循 MQS 的 Go 框架模式（`go-polymarket-sdk` + `polypilot` + 纯计算层零依赖）
- **频率一致**：lab 的 5s tick 与回测数据频率相同 → **参数 1:1 复用，无需重校准**

---

## 2. 数据流

```
Binance WS ──┐                    Polymarket WS ──┐
(aggTrade,   │                    (CLOB books)    │
 depth20)    │                    (MarketMonitor) │
             ▼                                     ▼
     feed.BinanceAdapter              feed.OrderBookAdapter
             │                                     │
             └──────────────┬──────────────────────┘
                            ▼
                   lab.Collector.Tick()  ← 每 5 秒
                            │
                            ▼
                   lab.ResearchSnapshot  ← 与回测 JSONL 字段一致
                            │
                   ┌───────┴────────┐
                   ▼                ▼
             lab.Event         flip.Engine.ProcessSnapshot()
             (JSONL持久化)         │
                                   ▼
                            flip.FlipSignal
                                   │
                                   ▼
                            flip.Recorder
                            → flip_signals.jsonl
```

**与 Python 回测的一致性**：`ResearchSnapshot` 的字段名与回测 JSONL 中 `Snapshot` 一一对应：

| Python Snapshot | Go ResearchSnapshot | 说明 |
|----------------|---------------------|------|
| `ts` | `Timestamp` | 毫秒时间戳 |
| `remaining_sec` | `RemainingSec` | 剩余秒数 |
| `open` | `OpenPrice` | BTC 开盘价 |
| `price` | `CurrentPrice` | BTC 当前价 |
| `yes_price` | `YesPrice` | PM YES 价 |
| `no_price` | `NoPrice` | PM NO 价 |

---

## 3. 新增/修改文件

```
internal/flip/                     # 新包：翻转信号引擎
├── types.go                       # FlipConfig, FlipSignal, FlipScoreParams
├── engine.go                      # 状态机：WATCHING → CONFIRMING → DONE
├── features.go                    # 特征提取纯函数 (与 Python 1:1 对应)
├── scoring.go                     # 7 维评分 + F0 否决
├── hist_range.go                  # 历史振幅基准追踪器 (跨周期 FIFO)
├── recorder.go                    # JSONL 纸面记录器 (信号+结算回填)
└── engine_test.go                 # 单元测试

cmd/flip/main.go                   # 独立入口 (新建)
```

### 独立 cmd 的理由

1. **关注点分离**：lab 负责数据采集，flip 负责信号检测，互不干扰
2. **独立启停**：可以单独跑 flip 纸面交易，不影响 lab 数据采集
3. **代码清晰**：各自循环逻辑独立，不互相嵌入

---

## 4. 类型定义

### 4.1 FlipConfig — 参数（与 Python `FlipBacktestConfig` 对齐）

```go
type FlipConfig struct {
    // Layer 0: 前置条件
    TriggerThreshold  float64 // 0.7
    FirstCrossingOnly bool    // true
    MinPreSnaps       int     // 5

    // 振荡判定 (三条件需同时满足)
    PathEffOscillating    float64 // ≤0.5
    NoiseRatioOscillating float64 // >5
    FlipsOscillating      int     // >2  (5s数据, 与回测一致)

    // 振幅扩张 (tick-independent)
    HistWindowN        int     // 18 根K线
    RangeExpThreshold  float64 // <0.5 → +2分
    RangeExpMax        float64 // ≥2.0 → F0否决

    // 对面确认
    ConfirmDelayTicks int     // 1 tick = 5秒
    OtherDeltaStrong  float64 // >0.03 → +4分
    OtherDeltaWeak    float64 // >0.01 → +2分

    // BTC 位置
    BTCPosMax float64 // 0.5
    BTCPosMin float64 // -0.5

    // 入场价
    EntryCheapStrong float64 // <0.20 → +2分
    EntryCheapWeak   float64 // <0.25 → +1分

    // 评分权重 (与回测完全一致)
    WOtherD5Strong  int // 4
    WOtherD5Weak    int // 2
    WOscillating    int // 1
    WCheapEntryStr  int // 2
    WCheapEntryWeak int // 1
    WRangeExpansion int // 2
    WBtcExtreme     int // 1

    // 入场阈值
    ScoreEntry int // ≥5 → 开仓 1 share
    ScoreAdd   int // ≥此值 → 加仓 2 shares (99 = disabled, 5s数据评分不够精细)
}

func DefaultConfig() FlipConfig {
    return FlipConfig{
        TriggerThreshold:  0.7,
        FirstCrossingOnly: true,
        MinPreSnaps:       5,
        // ... 全部与 backtest_flip_config.py 对齐
        ConfirmDelayTicks: 1,   // ← 5s数据: 1 tick = 5s
        FlipsOscillating:  2,   // ← 5s数据: 与回测一致
        ScoreAdd:          99,  // ← disabled (5s数据评分不够精细, 与Python一致)
    }
}
```

### 4.2 FlipSignal — 信号输出

```go
type FlipSignal struct {
    Time         time.Time `json:"time"`
    ConditionID  string    `json:"condition_id"`
    MarketID     string    `json:"market_id"`
    Side         string    `json:"side"`        // "yes" / "no"
    Score        int       `json:"score"`
    EntryPrice   float64   `json:"entry_price"`
    Shares       int       `json:"shares"`
    RemainingSec int       `json:"remaining_sec"`

    // 特征详情
    PathEff        float64 `json:"path_eff"`
    NoiseRatio     float64 `json:"noise_ratio"`
    Flips          int     `json:"flips"`
    IsOscillating  bool    `json:"is_oscillating"`
    RangeExpansion float64 `json:"range_expansion"`
    BTCPosition    float64 `json:"btc_position"`
    BTCExtreme     bool    `json:"btc_extreme"`
    OtherDelta     float64 `json:"other_delta"`

    // 结算回填
    Won bool    `json:"won"`
    PnL float64 `json:"pnl"`
}
```

### 4.3 Engine 状态机

```go
type flipState int
const (
    stateIdle       flipState = iota
    stateWatching              // 累积 snapshot, 等 >0.7 穿越
    stateConfirming            // 已穿越, 等 T+5s 确认
    stateDone                  // 本周期完成/否决
)

type Engine struct {
    cfg          FlipConfig
    histRange    *HistRangeTracker
    state        flipState
    snapBuffer   []*lab.ResearchSnapshot  // 穿越前+穿越时刻的 snapshots
    crossSnap    *lab.ResearchSnapshot    // 穿越时刻的快照 (用于 other_delta)
    crossSide    string                   // "yes" / "no"
    confirmCount int                      // CONFIRMING 状态已等待 tick 数
    generation   int64
    doneThisGen  bool

    // T=0 特征 (onCrossing 计算, onConfirmed 读取用于 FlipSignal)
    pathEff        float64
    noiseRatio     float64
    flips          int
    oscillating    bool
    rangeExpansion float64  // 振幅扩张 (hist就绪时有效, 否则0)
    btcPosition    float64
    btcExtreme     bool
    // entryPrice 在 onConfirmed 中从 crossSnap 直接取值, 不单独存储
}
```

---

## 5. 模块详细设计

### 5.1 hist_range.go — 历史振幅追踪器

跨周期维护前 N 个事件的 `|close - open|` 均值，与 Python `compute_hist_avg_range()` 一致。

```go
type HistRangeTracker struct {
    mu       sync.RWMutex
    windowN  int
    ranges   []float64   // FIFO, 最近 N 个振幅
    avgRange float64
    ready    bool        // len(ranges) >= 3
}

// Warmup 启动时从 Binance REST 拉取最近 N 根 5m K 线预热。
func (t *HistRangeTracker) Warmup(binance *feed.BinanceAdapter, symbol string) error

// AddRange 追加一个周期的振幅 (每个周期结束调用)。
func (t *HistRangeTracker) AddRange(openPrice, closePrice float64)

// AvgRange 返回当前平均振幅。
func (t *HistRangeTracker) AvgRange() float64

// IsReady 是否有足够数据 (≥3 个历史周期)。
func (t *HistRangeTracker) IsReady() bool
```

**Warmup 实现**：调用 Binance REST `GET /api/v3/klines?symbol=BTCUSDT&interval=5m&limit=18`，解析每根 K 线的 `|close - open|`，填入 FIFO 队列。

### 5.2 features.go — 特征提取

5 个纯函数，与 Python `backtest_flip_utils.py` 完全对应：

```go
// PathEfficiency §§2.2
// path_eff = abs(lastPrice - openPrice) / (max(prePrices) - min(prePrices))
func PathEfficiency(prePrices []float64, openPrice float64) float64

// TotalPath §§2.3 helper — Σ|p[i] - p[i-1]| 累计tick级路径长度
func TotalPath(prePrices []float64) float64

// NoiseRatio §§2.3
// noise_ratio = total_path / net_move (net_move==0 → 返回 total_path)
func NoiseRatio(prePrices []float64, netMove float64) float64

// CountFlips §§2.4
// 相邻价格方向变化次数 (忽略平盘 tick)
func CountFlips(prePrices []float64) int

// IsOscillating §§2.5
// path_eff <= 0.5 && noise_ratio > 5 && flips > 2
func IsOscillating(pathEff, noiseRatio float64, flips int, cfg FlipConfig) bool

// RangeExpansion §§2.6
// = abs(price - openPrice) / histAvgRange  (tick-independent)
func RangeExpansion(price, openPrice, histAvgRange float64) float64

// BTCPosition §§2.8
// = (price - openPrice) / histAvgRange  (tick-independent)
func BTCPosition(price, openPrice, histAvgRange float64) float64
```

### 5.3 scoring.go — 复合评分

```go
type ScoreParams struct {
    Side           string
    OtherDelta     float64
    IsOscillating  bool
    EntryPrice     float64
    RangeExpansion float64
    BTCPosition    float64
    HistReady      bool
    Cfg            FlipConfig
}

// ComputeFlipScore 返回 (score, vetoed)。
// vetoed=true 表示 F0 否决 (RangeExpansion >= 2.0)。
func ComputeFlipScore(params ScoreParams) (score int, vetoed bool) {
    // F0: 振幅过大一票否决 (仅 hist ready 时)
    if params.HistReady && params.RangeExpansion >= params.Cfg.RangeExpMax {
        return 0, true
    }

    score := 0

    // F1/F2: 对面价格变化
    if params.OtherDelta > params.Cfg.OtherDeltaStrong {
        score += params.Cfg.WOtherD5Strong
    } else if params.OtherDelta > params.Cfg.OtherDeltaWeak {
        score += params.Cfg.WOtherD5Weak
    }

    // F3: 来回振荡
    if params.IsOscillating {
        score += params.Cfg.WOscillating
    }

    // F4/F5: 低价入场
    if params.EntryPrice < params.Cfg.EntryCheapStrong {
        score += params.Cfg.WCheapEntryStr
    } else if params.EntryPrice < params.Cfg.EntryCheapWeak {
        score += params.Cfg.WCheapEntryWeak
    }

    // F6: 振幅过小 — PM 过度自信
    if params.HistReady && params.RangeExpansion < params.Cfg.RangeExpThreshold {
        score += params.Cfg.WRangeExpansion
    }

    // F7: BTC 微动 (PM 方向上)
    if params.HistReady {
        var extreme bool
        if params.Side == "yes" {
            extreme = params.BTCPosition > 0 && params.BTCPosition < params.Cfg.BTCPosMax
        } else {
            extreme = params.BTCPosition > params.Cfg.BTCPosMin && params.BTCPosition < 0
        }
        if extreme {
            score += params.Cfg.WBtcExtreme
        }
    }

    return score, false
}
```

### 5.4 engine.go — Flip Engine

```go
func NewEngine(cfg FlipConfig, histRange *HistRangeTracker) *Engine
func (e *Engine) Reset(generation int64)  // 新周期, 回到 WATCHING
func (e *Engine) ProcessSnapshot(snap *lab.ResearchSnapshot, generation int64) *FlipSignal
```

**ProcessSnapshot 实现逻辑**：

```go
func (e *Engine) ProcessSnapshot(snap *lab.ResearchSnapshot, gen int64) *FlipSignal {
    if gen != e.generation || e.doneThisGen {
        return nil
    }

    switch e.state {
    case stateIdle:
        return nil  // 等 Reset()

    case stateWatching:
        // ⚠️ 先追加到 buffer, 再检查穿越
        // 这样 snapBuffer 的最后一条就是穿越快照
        e.snapBuffer = append(e.snapBuffer, snap)

        if snap.YesPrice > e.cfg.TriggerThreshold {
            return e.onCrossing(snap, "yes")
        }
        if snap.NoPrice > e.cfg.TriggerThreshold {
            return e.onCrossing(snap, "no")
        }
        return nil

    case stateConfirming:
        // ⚠️ 确认阶段不追加到 snapBuffer
        e.confirmCount++
        if e.confirmCount >= e.cfg.ConfirmDelayTicks {
            return e.onConfirmed(snap)
        }
        return nil

    case stateDone:
        return nil
    }
    return nil
}
```

**onCrossing(snap, side)** — 穿越时调用：

```go
func (e *Engine) onCrossing(snap *lab.ResearchSnapshot, side string) *FlipSignal {
    // 检查穿越前 snapshot 数量
    if len(e.snapBuffer) < e.cfg.MinPreSnaps {
        e.state = stateDone
        return nil
    }

    // 提取 prePrices (包含穿越时刻, 即 snapBuffer 最后一条)
    prePrices := make([]float64, len(e.snapBuffer))
    for i, s := range e.snapBuffer {
        prePrices[i] = s.CurrentPrice
    }

    openPrice := snap.OpenPrice

    // T=0 特征计算
    netMove := math.Abs(prePrices[len(prePrices)-1] - openPrice)
    preHigh, preLow := maxSlice(prePrices), minSlice(prePrices)
    preRange := preHigh - preLow
    if preRange == 0 {
        e.state = stateDone
        return nil
    }

    e.pathEff = netMove / preRange

    // noise_ratio: netMove==0 → 纯震荡, 返回 total_path (与Python一致)
    totalPathVal := TotalPath(prePrices)
    if netMove > 0 {
        e.noiseRatio = totalPathVal / netMove
    } else {
        e.noiseRatio = totalPathVal
    }

    e.flips = CountFlips(prePrices)
    e.oscillating = e.pathEff <= e.cfg.PathEffOscillating &&
        e.noiseRatio > e.cfg.NoiseRatioOscillating &&
        e.flips > e.cfg.FlipsOscillating

    // 振幅扩张 (仅 hist 就绪时计算, 否则为0)
    if e.histRange.IsReady() {
        e.rangeExpansion = math.Abs(snap.CurrentPrice-openPrice) / e.histRange.AvgRange()
    }

    // F0: 振幅过大一票否决 (hist就绪 且 range_exp >= 2.0)
    if e.histRange.IsReady() && e.rangeExpansion >= e.cfg.RangeExpMax {
        e.state = stateDone  // 真突破，不下注
        return nil
    }

    // 保存穿越状态，进入确认阶段
    e.crossSnap = snap
    e.crossSide = side
    e.confirmCount = 0  // 下一个 tick 开始计数
    e.state = stateConfirming
    return nil
}
```

**onConfirmed(snap)** — 确认 tick 到达时调用：

```go
func (e *Engine) onConfirmed(snap *lab.ResearchSnapshot) *FlipSignal {
    e.state = stateDone
    e.doneThisGen = true

    // 计算 other_delta (对面价格变化)
    var otherDelta float64
    if e.crossSide == "yes" {
        otherDelta = snap.NoPrice - e.crossSnap.NoPrice   // YES>0.7, 对面是 NO
    } else {
        otherDelta = snap.YesPrice - e.crossSnap.YesPrice // NO>0.7, 对面是 YES
    }

    // Hard filter: 对面下跌 >0.02 → 不触发 (flip rate 仅 13%)
    if otherDelta < -0.02 {
        return nil
    }

    // 入场价 = 穿越时刻的对面价
    var entryPrice float64
    if e.crossSide == "yes" {
        entryPrice = e.crossSnap.NoPrice   // 买 NO, 赌 DOWN
    } else {
        entryPrice = e.crossSnap.YesPrice  // 买 YES, 赌 UP
    }

    // BTC 位置 (仅 hist 就绪时)
    e.btcPosition = 0.0
    e.btcExtreme = false
    if e.histRange.IsReady() {
        e.btcPosition = (e.crossSnap.CurrentPrice - e.crossSnap.OpenPrice) / e.histRange.AvgRange()
        if e.crossSide == "yes" {
            e.btcExtreme = e.btcPosition > 0 && e.btcPosition < e.cfg.BTCPosMax
        } else {
            e.btcExtreme = e.btcPosition > e.cfg.BTCPosMin && e.btcPosition < 0
        }
    }

    // 计算评分 — 使用 engine 存储的 T=0 特征
    params := ScoreParams{
        Side:           e.crossSide,
        OtherDelta:     otherDelta,
        IsOscillating:  e.oscillating,
        EntryPrice:     entryPrice,
        RangeExpansion: e.rangeExpansion,
        BTCPosition:    e.btcPosition,
        HistReady:      e.histRange.IsReady(),
        Cfg:            e.cfg,
    }

    score, vetoed := ComputeFlipScore(params)
    if vetoed || score < e.cfg.ScoreEntry {
        return nil
    }

    shares := 1
    if score >= e.cfg.ScoreAdd {
        shares = 2
    }

    return &FlipSignal{
        Time:           time.Now(),
        Side:           e.crossSide,
        Score:          score,
        EntryPrice:     entryPrice,
        Shares:         shares,
        RemainingSec:   e.crossSnap.RemainingSec,
        PathEff:        e.pathEff,
        NoiseRatio:     e.noiseRatio,
        Flips:          e.flips,
        IsOscillating:  e.oscillating,
        RangeExpansion: e.rangeExpansion,
        BTCPosition:    e.btcPosition,
        BTCExtreme:     e.btcExtreme,
        OtherDelta:     otherDelta,
    }
}
```

**关键设计点**：
- `snapBuffer` 在 WATCHING 中追加，穿越时刻的 snap 也在 buffer 末尾 → prePrices 包含穿越价，与 Python `snaps[:cross_idx+1]` 一致
- `crossSnap` 单独保存指针，用于 CONFIRMING 阶段计算 `other_delta` 和 `entry_price`
- **CONFIRMING 状态不追加 snapBuffer**，确认用的 snap 只用于 other_delta 计算
- `other_delta < -0.02` 硬过滤（与 Python 一致），flip rate 仅 13%
- **F0 否决有 HistReady 守卫**：hist 未就绪时不否决，降级跳过

### 5.5 recorder.go — 纸面记录器

```go
type FlipRecorder struct {
    mu      sync.Mutex
    file    *os.File
    pending map[string]*FlipSignal // conditionID → signal
}

func NewFlipRecorder(path string) (*FlipRecorder, error)

// RecordSignal 写入信号记录（won/pnl 为空，等结算回填）。
// 同时存入 pending map。
func (r *FlipRecorder) RecordSignal(sig *FlipSignal) error

// Resolve 结算: 回填 won/pnl，更新 JSONL 记录。
// outcome: lab.Event.Outcome (0=Up, 1=Down)
func (r *FlipRecorder) Resolve(conditionID string, outcome int) error

func (r *FlipRecorder) Close() error
```

**JSONL 输出格式**（与 Python 回测导出格式一致）：

```json
{"time":"2026-08-06T14:35:22Z","condition_id":"0xabc...","side":"yes","score":7,"entry_price":0.183,"shares":1,"remaining_sec":245,"path_eff":0.32,"noise_ratio":8.5,"flips":4,"is_oscillating":true,"range_expansion":0.3,"btc_position":0.25,"btc_extreme":true,"other_delta":0.042}
```

**结算行**（同一个 condition_id 的 Resolve 后追加）：

```json
{"type":"resolution","condition_id":"0xabc...","side":"yes","entry_price":0.183,"shares":1,"won":true,"pnl":0.817}
```

**PnL 公式（与 Python 一致）**：

```go
func computePnL(won bool, entryPrice float64, shares int) float64 {
    if won {
        return (1.0 - entryPrice) * float64(shares)
    }
    return (0.0 - entryPrice) * float64(shares)
}
```

**won 判定**：

```go
// outcome: 0=Up赢, 1=Down赢 (lab.Event.Outcome)
if side == "yes" {
    // YES>0.7, 买NO赌DOWN → DOWN赢才算赢
    won = (outcome == 1)
} else {
    // NO>0.7, 买YES赌UP → UP赢才算赢
    won = (outcome == 0)
}
```

**结算回填方案**：`Resolve()` 收到 outcome 后：
1. 从 `pending[conditionID]` 取出信号
2. 计算 won + pnl
3. 追加一行 `{"type":"resolution",...}` 写入 JSONL

由于 JSONL 是追加写的，已写入的行不能原地修改。采用方式：
1. `RecordSignal` 写入一行信号详情（不含 won/pnl）
2. `Resolve` 计算 won/pnl 后，写入第二条记录包含 `type:"resolution"` + 结果

这样后续分析时按 condition_id 关联即可。

---

## 6. cmd/flip/main.go — 独立入口

与 `cmd/lab/main.go` 同样的架构模式：`go-polymarket-sdk` + `feed.BinanceAdapter` + `feed.OrderBookAdapter` + `lab.Collector`。

### 6.1 整体结构

```go
package main

import (
    "context"
    "crypto/rand"
    "encoding/hex"
    "flag"
    "fmt"
    "log"
    "os"
    "os/signal"
    "sync"
    "syscall"
    "time"

    "github.com/tidwall/gjson"
    "github.com/xiangxn/go-polymarket-sdk/model"
    sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"

    "github.com/necklace/flip-signal/internal/feed"
    "github.com/necklace/flip-signal/internal/flip"
    "github.com/necklace/flip-signal/internal/lab"
)

func main() {
    // 1. 初始化 Polymarket client (只读)
    // 2. 初始化 BinanceAdapter (BTC WS)
    // 3. 初始化 OrderBookAdapter (Polymarket CLOB WS)
    // 4. 初始化 lab.Collector (5s ResearchSnapshot)
    // 5. 初始化 flip.HistRangeTracker + Warmup (Binance REST)
    // 6. 初始化 flip.Engine + flip.Recorder
    // 7. 市场循环 (同 lab 模式)
}
```

### 6.2 市场循环

```
loop:
  1. 计算下一个 5min 对齐时间戳
  2. sleep 到窗口开始 + 2s
  3. FetchKlineOpenPrice → 拿到 BTC 开盘价
  4. FetchMarketBySlug → 拿到 conditionID / token IDs
  5. SubscribeTokens → 订阅 YES/NO order books
  6. StartEvent → lab.Collector 开始新周期
  7. 5s ticker 循环:
       - UpdatePolymarket (从 order book channel 读取最新价)
       - collector.Tick() → ResearchSnapshot
       - flipEngine.ProcessSnapshot(snap, generation) → FlipSignal?
       - 如果有信号 → flipRecorder.RecordSignal()
       - remaining_sec <= 0 → break
  8. FinalizeEvent → event (含 outcome)
  9. histTracker.AddRange(event.OpenPrice, event.ClosePrice)
  10. generation++; flipEngine.Reset(generation)
  11. flipRecorder.Resolve(event.ConditionID, event.Outcome)
  12. UnsubscribeTokens → goto 1
```

### 6.3 关键初始化代码

```go
// ── Binance ──
binanceCfg := feed.BinanceConfig{
    Symbol:        *symbol,
    StreamBaseURL: "wss://data-stream.binance.vision",
    RestBaseURL:   "https://data-api.binance.vision",
}
binance := feed.NewBinanceAdapterWithConfig(binanceCfg)
go binance.Start(ctx)

// ── Polymarket books ──
bookAdapter := feed.NewOrderBookAdapterWithResolve(clobWSURL, client, false)
bookAdapter.Start(ctx)

// ── Lab Collector (5s snapshots) ──
collector := lab.NewCollector(binance)

// ── Flip Engine ──
flipCfg := flip.DefaultConfig()
histTracker := flip.NewHistRangeTracker(flipCfg.HistWindowN) // 18
if err := histTracker.Warmup(binance, *symbol); err != nil {
    log.Printf("[Flip] hist warmup failed: %v", err)
}
flipEngine := flip.NewEngine(flipCfg, histTracker)

// ── Flip Recorder ──
flipRecorder, err := flip.NewFlipRecorder(*outputPath)
if err != nil {
    log.Fatalf("[Flip] recorder: %v", err)
}
defer flipRecorder.Close()

var generation int64
```

### 6.4 Snapshot 消费（collectLoop 内）

```go
case tickTime := <-ticker.C:
    // 读取 Polymarket book 价格
    bookMu.RLock()
    yb, nb := yesBook, noBook
    bookMu.RUnlock()
    collector.UpdatePolymarket(bestBid(yb), bestBid(nb))

    snap := collector.Tick(tickTime)
    if snap == nil {
        continue
    }

    // ── Flip 信号检测 ──
    if sig := flipEngine.ProcessSnapshot(snap, generation); sig != nil {
        sig.ConditionID = conditionID
        flipRecorder.RecordSignal(sig)
        log.Printf("[Flip] 🎯 SIGNAL: %s>0.7 score=%d entry=%.3f shares=%d | "+
            "osc=%v path_eff=%.2f noise=%.1f flips=%d range_exp=%.1f btc_pos=%.2f other_d=%+.3f rem=%ds",
            sig.Side, sig.Score, sig.EntryPrice, sig.Shares,
            sig.IsOscillating, sig.PathEff, sig.NoiseRatio, sig.Flips,
            sig.RangeExpansion, sig.BTCPosition, sig.OtherDelta,
            sig.RemainingSec)
    }

    if snap.RemainingSec <= 0 {
        ticker.Stop()
        break collectLoop
    }
```

### 6.5 周期结束

```go
// 结算
event := collector.FinalizeEvent()
histTracker.AddRange(event.OpenPrice, event.ClosePrice)
generation++
flipEngine.Reset(generation)

// outcome: 0=Up, 1=Down. 与 Python 回测一致.
flipRecorder.Resolve(event.ConditionID, event.Outcome)

log.Printf("[Flip] cycle done — %s outcome=%d open=%.2f close=%.2f",
    event.ConditionID, event.Outcome, event.OpenPrice, event.ClosePrice)
```

### 6.6 CLI 参数

```
go run ./cmd/flip -symbol BTCUSDT -output data/flip_signals.jsonl
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `-symbol` | BTCUSDT | Binance 交易对 |
| `-slug` | btc-updown-5m | Polymarket slug 前缀 |
| `-output` | data/flip_signals.jsonl | 信号 JSONL 输出路径 |
| `-config` | config.yaml | SDK 配置文件（可选） |

---

## 7. 依赖关系

```
internal/flip/
├── 依赖 internal/lab       (ResearchSnapshot 类型)
├── 依赖 internal/feed      (BinanceAdapter, 仅 hist_range 预热用)
├── 不依赖 internal/mqs     (独立评分体系)
├── 不依赖 internal/snapshot
├── 不依赖 internal/decision
│
└── 被 cmd/flip/main.go 引用
```

| 依赖 | 用途 |
|------|------|
| `github.com/xiangxn/go-polymarket-sdk` | SDK 类型（在 cmd/flip 层使用，flip 包不直接依赖） |
| `github.com/xiangxn/polypilot` | 策略框架（当前纸面交易阶段不使用，未来实盘下单时引入） |
| 标准库 `encoding/json`, `sync`, `os` | JSONL 写入、并发安全、文件操作 |

---

## 8. 实施步骤

### Step 1: `internal/flip/types.go`
- FlipConfig + DefaultConfig()
- FlipSignal 结构体
- flipState 枚举
- ScoreParams 结构体

### Step 2: `internal/flip/features.go`
- PathEfficiency, NoiseRatio, CountFlips, IsOscillating
- RangeExpansion, BTCPosition
- 纯函数，无外部依赖，可独立单测

### Step 3: `internal/flip/scoring.go`
- ComputeFlipScore: 7 维 + F0 否决

### Step 4: `internal/flip/hist_range.go`
- HistRangeTracker + Warmup (调用 Binance REST) + AddRange

### Step 5: `internal/flip/engine.go`
- Engine + ProcessSnapshot 状态机

### Step 6: `internal/flip/recorder.go`
- FlipRecorder: RecordSignal + Resolve

### Step 7: `internal/flip/engine_test.go`
- 用构造数据验证: 状态机流转、特征计算正确性、评分与 Python 一致

### Step 8: 创建 `cmd/flip/main.go`
- 独立入口，复用 lab 架构模式
- 初始化 BinanceAdapter + OrderBookAdapter + lab.Collector
- 初始化 flip.HistRangeTracker (Warmup) + Engine + Recorder
- 市场循环：collectLoop 内调 ProcessSnapshot
- 周期结束时 AddRange + Reset + Resolve

---

## 9. 参数表（与 Python 回测一致）

所有参数与 `backtest_flip_config.py` 完全相同，无需重校准：

| 参数 | 值 | 说明 |
|------|-----|------|
| `trigger_threshold` | 0.7 | PM > 0.7 触发 |
| `first_crossing_only` | true | 仅首次穿越 |
| `min_pre_snaps` | 5 | 穿越前至少 5 个 snapshot |
| `path_eff_oscillating` | ≤0.5 | 振荡判定 |
| `noise_ratio_oscillating` | >5 | 噪声比判定 |
| `flips_oscillating` | >2 | 翻转次数判定 |
| `hist_window_N` | 18 | 历史振幅窗口 |
| `range_exp_threshold` | <0.5 | 振幅过小加分 |
| `range_exp_max` | ≥2.0 | F0 否决 |
| `confirm_delay_ticks` | 1 | T+5s 确认 |
| `other_delta_strong` | >0.03 | +4 分 |
| `other_delta_weak` | >0.01 | +2 分 |
| `entry_cheap_strong` | <0.20 | +2 分 |
| `entry_cheap_weak` | <0.25 | +1 分 |
| `score_entry` | ≥5 | 开仓 (1 share) |
| `score_add` | ≥**99** | 加仓 (disabled, 5s评分不够精细, 与Python一致) |

---

## 10. 风险 & 注意事项

1. **hist_range 预热**：启动时通过 Binance REST `GET /api/v3/klines?interval=5m&limit=18` 一次拉取历史 K 线，直接算好 `hist_avg_range`，**无预热等待期**。首周期即可使用全部 7 维特征。
2. **对面价硬过滤**：`other_delta < -0.02` 直接否决，回测中此条件 flip rate 仅 13%，安全有效。
3. **结算源**：lab 用 BTC 价格判定 outcome，不等 Polymarket 链上结算。对 BTC updown 市场来说两个结果一致（平盘极少见）。
4. **与 MQS 并行**：Flip 和 MQS 是独立信号，如果同时运行 lab + mqs，注意 WS 连接复用（两个 cmd 各自连一套 WS）。
5. **不修改 lab 数据结构**：Flip 包只读取 ResearchSnapshot，不改变 lab 的任何类型定义。

---

## 11. 文件清单

| 文件 | 估算行数 | 说明 |
|------|---------|------|
| `internal/flip/types.go` | ~90 | 配置 + 信号 + 状态枚举 |
| `internal/flip/features.go` | ~80 | 5 个特征纯函数 |
| `internal/flip/scoring.go` | ~60 | 7 维评分 + F0 |
| `internal/flip/hist_range.go` | ~90 | 历史振幅追踪 + REST 预热 |
| `internal/flip/engine.go` | ~160 | 状态机引擎 (含 onCrossing/onConfirmed) |
| `internal/flip/recorder.go` | ~80 | JSONL 记录器 |
| `internal/flip/engine_test.go` | ~120 | 单元测试 |
| `cmd/flip/main.go` | ~250 | 独立入口 (市场循环 + book追踪 + config) |
| **总计** | **~930** | |
