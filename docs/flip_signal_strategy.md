# Flip Signal 策略详解

> 版本: Formula A  
> 回测结果: 34 signals, 52.9% WR, +10.09 P&L, PF=2.7（lab 数据）

---

## 1. 核心哲学

**不预测涨跌，只判断"什么时候市场过度自信"。**

Polymarket BTC 5分钟市场的 YES/NO 价格反映了市场对 BTC 涨跌的即时判断。当某侧价格突破 0.7 时，意味着市场对该方向高度确信。然而统计表明，约 **1/3** 的情况下这份自信是错的 —— 市场最终走向了相反方向。

Flip Signal 的目标不是预测 BTC 涨跌，而是识别"市场过度自信"的时刻，在这些时刻反向押注，赚取概率优势。

### 1.1 策略直觉

| 市场状态 | PM 表现 | 解读 | 策略动作 |
|----------|---------|------|----------|
| YES > 0.7 | 市场确信 BTC 会涨 | "这也太确定了，是不是反应过度了？" | 买入 NO（押跌） |
| NO > 0.7 | 市场确信 BTC 会跌 | "跌这么确定？BTC 可能撑住了" | 买入 YES（押涨） |

### 1.2 统计基础

基于 287 个事件（2天数据）的分析：
- YES > 0.7：176 次，其中 27.3% 翻转（DOWN 赢）
- NO > 0.7：202 次，其中 23.8% 翻转（UP 赢）
- 任意侧 > 0.7：286 次，总翻转率 33.6%

---

## 2. 数据源与架构

### 2.1 数据流

```
Binance WS (btcusdt@trade + @depth20) ──┐
                                         ├──▶ Collector (5s tick) ──▶ ResearchSnapshot
Polymarket WS (MarketMonitor / CLOC) ────┘                              │
                                                                        ▼
Binance REST (GET /api/v3/klines) ──▶ OpenPrice                    Flip Engine
                                                                        │
                                                                        ▼
                                                                   FlipSignal
```

### 2.2 数据来源

| 数据 | 来源 | 方式 | 用途 |
|------|------|------|------|
| BTC 当前价格 | Binance `btcusdt@trade` | WebSocket | 计算 path_eff, noise_ratio, btc_position |
| BTC 5m 开盘价 | Binance REST `/api/v3/klines?interval=5m` | HTTP 每周期 | 对齐 Polymarket 窗口，特征计算基准 |
| BTC 买卖成交量 | Binance `btcusdt@trade` aggressor side | WebSocket | ResearchSnapshot（预留，当前未用于评分） |
| 订单簿深度 | Binance `btcusdt@depth20@100ms` | WebSocket | ResearchSnapshot（预留，当前未用于评分） |
| YES/NO 盘口价 | Polymarket `MarketMonitor` | WebSocket | 穿越检测 + 特征计算 |
| 历史 K 线振幅 | Binance REST `/api/v3/klines` | HTTP 启动时一次性拉取 | hist_avg_range 基准 |

### 2.3 5 秒采样

Collector 以 5 秒间隔生成 ResearchSnapshot，与 Polymarket CLOC 盘口更新频率匹配。每个 5 分钟周期约产生 60 个 snapshot。

### 2.4 市场周期对齐

```
5分钟 window = 每 5 分钟对齐的 UTC 时间边界（如 12:00:00, 12:05:00, 12:10:00...）
OpenPrice = Binance 5m K 线开盘价（与 Polymarket 窗口精确对齐）
窗口长度 ≈ 298 秒（5分钟窗口减去 2 秒延迟）
```

---

## 3. 状态机

### 3.1 状态变迁

```
Idle ──Reset()──▶ Watching ──price > 0.7──▶ Confirming ──score ≥ 5──▶ Done (🎯 Signal)
                       ▲                        │
                       └── returnToWatching ────┘（确认失败，重新等待）
                       ▲                        │
                       └── fallback ────────────┘（YES 失败尝试 NO，或反之）
```

### 3.2 状态详解

| 状态 | 说明 | 触发条件 |
|------|------|----------|
| **Idle** | 未启动，等待 Reset() | 引擎初始化后 / 上一周期结束 |
| **Watching** | 累积 snapshot，监测价格穿越 | Reset() 进入 |
| **Confirming** | 检测到穿越，等待 T+N ticks 后确认 | YES 或 NO 价格突破 0.7 |
| **Done** | 本周期已产生信号，后续 snapshot 忽略 | 评分 ≥ ScoreEntry (5) |

### 3.3 多穿越模式（AllowRetryCrossings: true，默认）

与传统的"首次穿越仅一试"不同，多穿越模式允许：

1. **上升沿检测**：每次价格从 ≤0.7 到 >0.7 的跳变都视为一个新的穿越机会
2. **YES 优先**：同一 tick 内同时发生两侧穿越时，优先尝试 YES
3. **失败即回退**：若当前穿越确认失败（评分不足），回到 Watching 等待下一个上升沿
4. **每周期最多一注**：一旦产生有效信号（score ≥ 5），本周期不再观察后续穿越

### 3.4 关键时序约束

- **穿越必须在窗口后段发生**：`remaining_sec < MaxRemainingSec`（默认 260 秒），即穿越最早仅允许在窗口开始后约 40 秒出现。过早的穿越 BTC 路径太短，特征不够可靠。
- **最小前置 snapshot**：穿越前至少需要 MinPreSnaps（默认 5）个 snapshot，确保有足够的 BTC 价格历史计算特征。

---

## 4. 7 特征详解

### 4.1 F1v / F1 / F2: 对面价格变化（OtherDelta）⭐⭐⭐

**最重要的信号特征，贡献理论最高分 3+2+1=6 分。**

当一侧价格突破 0.7 后，观察对面价格在确认期内的变化：

```
other_delta = 对面价格(确认时刻) - 对面价格(穿越时刻)
```

| 场景 | 含义 | 评分 |
|------|------|------|
| YES > 0.7（市场看涨）→ 看 NO 价格 | NO 价格上涨 = 有人不认同涨价，在买入 NO | 对面动得越多，翻转概率越高 |
| NO > 0.7（市场看跌）→ 看 YES 价格 | YES 价格上涨 = 有人不认同跌价，在买入 YES | 同上 |

**Formula A 三级评分**：

| 条件 | 分值 | 权重 | 解读 |
|------|------|------|------|
| other_delta > 0.05 | +3 | WOtherD5VStrong=3 | 对面巨大移动，市场分歧强烈 |
| other_delta > 0.02 | +2 | WOtherD5Strong=2 | 对面明显移动 |
| other_delta > 0.01 | +1 | WOtherD5Weak=1 | 对面有轻微移动（保底加分） |

注意三者是 **elif 互斥**的，不会叠加。最高取 +3。

### 4.2 F3: 振荡形态（IsOscillating）⭐⭐

**三条件必须同时满足**，暗示趋势衰竭，市场在犹豫：

| 条件 | 公式 | 阈值 | 含义 |
|------|------|------|------|
| path_eff ≤ | `|lastPrice - openPrice| / (max - min)` | 0.8 | 价格走的不是"直线"，中途有回调 |
| noise_ratio > | `Σ|Δprice| / |net move|` | 1.5 | 价格在同一位附近反复拉扯 |
| flips > | 方向切换次数 | 1 | 价格方向频繁切换 |

三个指标联合判断：价格看似走了一段净位移，但实际上经历了大量往返 —— 这是趋势衰竭的典型微观结构。

**权重**: WOscillating = 2

### 4.3 F4 / F5: 低价入场（CheapEntry）⭐

**赔率特征**，elif 互斥不叠加：

```
entry_price = 对面价格（穿越时刻）
```

| 条件 | 分值 | 权重 | 解读 |
|------|------|------|------|
| entry_price < 0.20 | +1 | WCheapEntryStr=1 | 极低价入场，赔率极高 |
| entry_price < 0.25 | +1 | WCheapEntryWeak=1 | 低价入场，赔率较好 |

为什么低价入场是优势？因为如果判断正确，损失的只是对面那一侧的少量份额；
但 PM 在价格 >0.7 的一侧置信极高，对面价格往往被打压到很低的水平 ——
这恰好是高赔率的入场点。

### 4.4 F6: 振幅萎缩（RangeExpansion）⭐⭐

基于历史 K 线振幅来衡量当前 BTC 移动的幅度：

```
hist_avg_range = 最近 N 根 5m K 线 |close - open| 的均值
range_expansion = |price - openPrice| / hist_avg_range
```

| 条件 | 分值 | 权重 | 解读 |
|------|------|------|------|
| range_expansion < 0.5 | +2 | WRangeExpansion=2 | BTC 几乎没动，PM 却极度自信 → 过度反应概率高 |

BTC 的价格波动有惯性 —— 通常 5 分钟内振幅在某个"正常范围"内。如果 BTC 几乎没动（振幅不到历史均值的一半），PM 却已经把单侧价格推到 0.7 以上，这暗示 PM 的定价脱离了 BTC 的实际波动基础。

### 4.5 F7: BTC 方向背离（BTCExtreme）⭐

```
btc_position = (price - openPrice) / hist_avg_range
```

| PM 状态 | BTC 条件 | 分值 | 权重 | 解读 |
|---------|----------|------|------|------|
| YES > 0.7（PM 看涨） | btc_position < -0.1 | +1 | WBtcExtreme=1 | PM 看涨但 BTC 在跌 → PM 过度反应 |
| NO > 0.7（PM 看跌） | btc_position > 0.1 | +1 | WBtcExtreme=1 | PM 看跌但 BTC 在涨 → PM 过度反应 |

**这是回测中效应量最大（1.25）的单一特征**：在 YES > 0.7 时，BTC 下跌的幅度是区分 Flip vs Strong 的最强信号。

### 4.6 F0: 真突破否决（Veto）

| 条件 | 动作 | 解读 |
|------|------|------|
| range_expansion ≥ 2.0 | 一票否决，不下注 | BTC 正在经历真突破（振幅是正常的 2 倍+），PM 判断很可能是对的 |

这是硬止损失 —— 与其在真突破中逆势下注，不如放弃这次信号。统计显示振幅扩张 ≥2x 时，翻转概率大幅下降。

### 4.7 特征总结

| 特征 | 简称 | 类型 | 理论最高分 | 依赖 |
|------|------|------|-----------|------|
| F1v | OtherDelta > 0.05 | 市场微观结构 | +3 | 确认期数据 |
| F1 | OtherDelta > 0.02 | 市场微观结构 | +2 | 确认期数据 |
| F2 | OtherDelta > 0.01 | 市场微观结构 | +1 | 确认期数据 |
| F3 | 振荡形态 | 价格微观结构 | +2 | BTC 价格序列 |
| F4 | EntryPrice < 0.20 | 赔率 | +1 | 穿越时刻 |
| F5 | EntryPrice < 0.25 | 赔率 | +1 | 穿越时刻 |
| F6 | RangeExpansion < 0.5 | BTC 波动率 | +2 | hist_avg_range |
| F7 | BTC 背离 | BTC 方向 | +1 | hist_avg_range |
| **理论最高** | | | **12** | |

---

## 5. 确认机制

### 5.1 确认延迟

穿越后不立即下注，而是等待 ConfirmDelayTicks（默认 1 tick = 5 秒）：
- 在确认期内观察 other_delta（对面价格变化）
- 这是唯一需要"等待未来数据"的点，但等待窗口极短（5秒），实时交易中可行

### 5.2 T=0 前置否决

穿越时刻（T=0）即执行的前置检查，不通过则直接放弃本次穿越：

| 否决条件 | 阈值 | 解读 |
|----------|------|------|
| path_eff < PathEffVetoMin | 0.4 | 趋势太不明确，价格几乎在原地徘徊 |
| noise_ratio > NoiseRatioVetoMax | 3.0 | PM 价格过于不稳定，信号噪声太大 |
| range_expansion ≥ RangeExpMax | 2.0 | BTC 真突破，F0 否决 |
| order_book_latency > MaxLatencyMs | 0（默认禁用） | 订单簿延迟过大，数据不可信 |

### 5.3 入场价确定

```
if side == "yes"（YES > 0.7，做空）:
    entry_price = cross_snap.NoPrice   // 买入 NO 的价格
if side == "no"（NO > 0.7，做多）:
    entry_price = cross_snap.YesPrice  // 买入 YES 的价格
```

入场价固定为穿越时刻的**对面价格**，不随确认期变化。

---

## 6. 评分公式（Formula A）

### 6.1 计算流程

```
1. 检查 L0: 订单簿延迟是否超标 → 是则 veto
2. 检查 F0: range_expansion ≥ 2.0 → 是则 veto
3. F1v: other_delta > 0.05 → +3
4. F1:  other_delta > 0.02 → +2 (elif)
5. F2:  other_delta > 0.01 → +1 (elif)
6. F3:  IsOscillating → +2
7. F4:  entry_price < 0.20 → +1 (elif)
8. F5:  entry_price < 0.25 → +1 (elif)
9. F6:  range_expansion < 0.5 → +2 (需要 hist ready)
10. F7: BTC diverges from PM → +1 (需要 hist ready)
11. 总分 ≥ 5 → 产生信号 🎯
```

### 6.2 评分阈值

| 阈值 | 默认值 | 含义 |
|------|--------|------|
| ScoreEntry | 5 | 总分 ≥ 5 → 开仓 1 份 |
| ScoreAdd | 99（禁用）| 总分 ≥ 99 → 加仓 → 实际永不触发 |

### 6.3 典型得分场景

**最强信号（score = 8-9）**：
- other_delta > 0.05 (+3): 对面猛动，市场严重分歧
- 振荡形态 (+2): 趋势衰竭
- range_expansion < 0.5 (+2): BTC 没怎么动
- entry_price < 0.20 (+1): 超低价入场
- **总计 8 分** → 强烈信号

**边缘通过（score = 5-6）**：
- other_delta > 0.02 (+2)
- 振荡形态 (+2)
- entry_price < 0.25 (+1)
- **总计 5 分** → 刚好过线

**失败案例（score = 3-4）**：
- other_delta > 0.01 (+1)
- entry_price < 0.25 (+1)
- BTC 背离 (+1)
- **总计 3 分** → 信号太弱，不下注

---

## 7. 信号输出

### 7.1 FlipSignal 结构

```go
type FlipSignal struct {
    Time         time.Time  // 信号产生时间 (UTC)
    ConditionID  string     // Polymarket condition ID
    Side         string     // "yes" 或 "no"
    Score        int        // 复合评分 (0-12)
    EntryPrice   float64    // 入场价（对面价格）
    Shares       float64    // 目标股数（由调用方填充）
    RemainingSec int        // 窗口剩余秒数

    // 特征详情（分析/调试用）
    PathEff        float64  // 路径效率
    NoiseRatio     float64  // 噪声比率
    Flips          int      // 方向切换次数
    IsOscillating  bool     // 是否振荡形态
    RangeExpansion float64  // 振幅扩张倍数
    BTCPosition    float64  // BTC 归一化位置
    BTCExtreme     bool     // BTC 是否与 PM 背离
    OtherDelta     float64  // 对面价格变化

    // 执行与结算
    ExecStatus   string  // "pending" | "filled" | "failed"
    FilledShares float64 // 实际成交股数
    AvgFillPrice float64 // 实际成交均价
    Won          bool    // 是否获胜
    PnL          float64 // 盈亏
}
```

### 7.2 仓位计算

Engine 只负责信号检测，仓位由 Trader 层计算：

```
shares = stake_per_signal / entry_price
```

例如：stake = 100 USDC, entry_price = 0.18 → shares = 555.56

---

## 8. 历史振幅追踪器（HistRangeTracker）

### 8.1 作用

为 F6（RangeExpansion）和 F7（BTCExtreme）提供基准。维护最近 N 根 5m K 线 `|close - open|` 的滑动窗口。

### 8.2 工作方式

| 阶段 | 操作 |
|------|------|
| 启动时 | `Warmup()`：从 Binance REST 拉取最近 18 根 K 线，批量计算振幅 |
| 每周期结束时 | `AddRange(openPrice, closePrice)`：追加最新完成的周期，FIFO 淘汰最旧的 |
| 特征计算时 | `AvgRange()` / `IsReady()`：读取当前均值（需要 ≥3 根才就绪） |

### 8.3 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| HistWindowN | 18 | 窗口大小 ≈ 1.5 小时的历史数据 |
| Ready 条件 | len(ranges) ≥ 3 | 至少有 3 根 K 线（15 分钟）才能计算均值 |

### 8.4 设计考量

为什么用 `|close - open|` 而非 `|high - low|`？

- 5m 开盘价与 Polymarket 窗口精确对齐
- `|close - open|` 代表"5 分钟内的净变化"，不含影线噪声
- 与 btc_position 的定义自然衔接（position 也用 openPrice 做基准）

---

## 9. 风控机制

### 9.1 硬风控

| 机制 | 说明 | 默认值 |
|------|------|--------|
| F0 否决 | 真突破不逆势 | range_expansion ≥ 2.0 → veto |
| T=0 路径否决 | 趋势太模糊不下注 | path_eff < 0.4 → veto |
| T=0 噪声否决 | PM 价格太不稳定不下注 | noise_ratio > 3.0 → veto |
| 延迟风控 | 订单簿延迟过大不下注 | latency > MaxLatencyMs（默认禁用） |

### 9.2 软约束

| 机制 | 说明 |
|------|------|
| 窗口时序 | 穿越太早（remaining_sec ≥ 260）不触发 |
| 最小数据量 | 穿越前不足 5 个 snapshot 不触发 |
| 单周期单注 | doneThisGen 后不再产生信号 |
| 评分门槛 | score ≥ 5 才开单 |

---

## 10. 配置参数速查

### 10.1 Layer 0: 前置条件

| 参数 | 默认值 | 说明 |
|------|--------|------|
| TriggerThreshold | 0.7 | PM 价格突破此值触发检测 |
| AllowRetryCrossings | true | 多穿越模式 |
| MinPreSnaps | 5 | 穿越前最少 snapshot 数 |
| MaxRemainingSec | 260 | 穿越最早允许的窗口剩余秒数 |

### 10.2 振荡检测阈值

| 参数 | 默认值 | 说明 |
|------|--------|------|
| PathEffOscillating | 0.8 | path_eff ≤ 此值视为振荡候选 |
| NoiseRatioOscillating | 1.5 | noise_ratio > 此值视为振荡候选 |
| FlipsOscillating | 1 | flips > 此值视为振荡候选 |

### 10.3 振幅扩张（Hist Range）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| HistWindowN | 18 | 历史 K 线窗口大小 |
| RangeExpThreshold | 0.5 | < 此值 → +2 (F6) |
| RangeExpMax | 2.0 | ≥ 此值 → Veto (F0) |

### 10.4 确认与评分

| 参数 | 默认值 | 说明 |
|------|--------|------|
| ConfirmDelayTicks | 1 | 确认等待 tick 数 (1 tick = 5s) |
| OtherDeltaVStrong | 0.05 | > 此值 → +3 |
| OtherDeltaStrong | 0.02 | > 此值 → +2 |
| OtherDeltaWeak | 0.01 | > 此值 → +1 |
| ScoreEntry | 5 | 开仓阈值 |
| ScoreAdd | 99 | 加仓阈值（禁用） |

### 10.5 T=0 否决

| 参数 | 默认值 | 说明 |
|------|--------|------|
| PathEffVetoMin | 0.4 | path_eff < 此值 → veto |
| NoiseRatioVetoMax | 3.0 | noise_ratio > 此值 → veto |

### 10.6 入场价格

| 参数 | 默认值 | 说明 |
|------|--------|------|
| EntryCheapStrong | 0.20 | < 此值 → +1 |
| EntryCheapWeak | 0.25 | < 此值 → +1 |

### 10.7 BTC 背离

| 参数 | 默认值 | 说明 |
|------|--------|------|
| BTCPosMax | 0.1 | NO>0.7: btc_position > 此值 → BTC 背离 |
| BTCPosMin | -0.1 | YES>0.7: btc_position < 此值 → BTC 背离 |

### 10.8 评分权重

| 参数 | 默认值 | 对应特征 |
|------|--------|----------|
| WOtherD5VStrong | 3 | other_delta > 0.05 |
| WOtherD5Strong | 2 | other_delta > 0.02 |
| WOtherD5Weak | 1 | other_delta > 0.01 |
| WOscillating | 2 | IsOscillating |
| WCheapEntryStr | 1 | entry_price < 0.20 |
| WCheapEntryWeak | 1 | entry_price < 0.25 |
| WRangeExpansion | 2 | range_expansion < 0.5 |
| WBtcExtreme | 1 | BTC diverges from PM |

### 10.9 延迟风控

| 参数 | 默认值 | 说明 |
|------|--------|------|
| MaxLatencyMs | 0 | 0 = 禁用，建议值 300ms |

---

## 11. 相关文档

| 文档 | 内容 |
|------|------|
| [data_flip_analysis.md](data_flip_analysis.md) | 原始数据分析：翻转率统计、特征效应量、复合评分可行性 |
| [flip_backtest_plan.md](flip_backtest_plan.md) | 回测实施计划：数据模型、特征计算、Python 回测框架 |
| [flip_signal_evaluation_report.md](flip_signal_evaluation_report.md) | 信号评估报告：Formula A 34 信号详细分析 |
| [flip_optimization_analysis.md](flip_optimization_analysis.md) | 参数优化分析：阈值搜索、权重调优 |
| [flip_paper_trading_plan.md](flip_paper_trading_plan.md) | 纸面交易计划：部署方案、监控指标 |
| [live_trading_plan.md](live_trading_plan.md) | 实盘交易计划（进行中） |

---

## 12. 关键设计决策

1. **Binance 直连而非通过 Polymarket 转发**：Polymarket 的 CryptoPriceMonitor 只提供价格，没有成交量、深度数据。

2. **OpenPrice 使用 5m K 线开盘价而非首笔成交价**：与 Polymarket 5 分钟窗口精确对齐，确保特征计算的一致性。

3. **核心计算层零外部依赖**：`flip` 和 `lab` 包可独立编译、离线测试，特征提取和评分为纯函数。

4. **确认延迟而非即时下注**：等待 5 秒让 other_delta 得以形成 —— 这是信号质量的关键提升点，代价是 5 秒的滑点风险。

5. **多穿越重试优于单次尝试**：允许每周期多次穿越检测，首个通过评分的获胜。这避免了早期穿越因 BTC 路径不够长而被浪费。

6. **纸面交易 + 实盘可选**：默认纸面交易，配置 `trading.enabled: true` 启用 FAK 市价单实盘执行。

7. **5 秒采样频率**：与 Polymarket CLOC 盘口更新频率匹配，减少噪声的同时保留足够的时序信息用于特征计算。
