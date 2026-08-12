# Flip 策略实施方案 — Formula B 精简公式（2026-08-13）

> 状态：**已通过回测验收，待实盘落地**。
> 分析依据：[flip_optimization_analysis_2026-08-13.md](flip_optimization_analysis_2026-08-13.md)
> Python 回测已更新至本方案口径（`python/backtest_flip_scoring.py`）。
> Go 侧改动清单见 §5（待 Python 回测复核后执行）。

---

## 1. 策略哲学

> **不预测涨跌，只判断「什么时候市场过度自信」。**

数据证明（χ²=125，全候选最强特征）：「BTC 与 PM 背离」的穿越才有 flip edge，
「BTC 与 PM 同向」的穿越（占 76%）EV≈0。Formula B 把这条 edge 从
「+1 加分项」升级为「入场前提」，并拆掉所有统计上无效的特征。

三个信号条件与哲学一一对应：

| 条件 | 含义 | 来源 |
|---|---|---|
| **B1 背离硬要求** | 穿越时刻 BTC 必须与 PM 反向 | 「市场过度自信」的必要条件 |
| **B2 过度自信** | BTC 振幅 < 历史平均的一半 | 「BTC 没动但 PM 已 0.7+」 |
| **B3 确认回归** | 确认期对侧 bid 回升 | 「反转正在发生」的执行确认 |

---

## 2. Formula B 完整参数规格

### 2.1 触发与窗口（Layer 0，不变）

| 参数 | 值 | 说明 |
|---|---|---|
| `trigger_threshold` | 0.7 | 一侧 bid > 0.7 触发 |
| `allow_retry_crossings` | true | 每个上升沿都尝试评分，首过者胜，每周期一注 |
| `min_pre_snaps` | 5 | 穿越前至少 5 个 snapshot |
| `max_remaining_sec` | 260 | 太早 = BTC 路径太短 |
| `min_remaining_sec` | 35 | 确认(2 ticks)+FAK 下单余量 |

### 2.2 信号条件（Formula B 核心）

| 参数 | 值 | 说明 |
|---|---|---|
| **`min_divergence`** | **0.05（新增）** | 背离硬要求：YES 侧触发要求 `btc_pos < -0.05`；NO 侧触发要求 `btc_pos > 0.05`。不满足 → 否决 |
| `range_exp_threshold` | 0.5 | `range_exp < 0.5` → +2（B2）|
| `range_exp_max` | 1.5 | `range_exp ≥ 1.5` → 真突破否决（F0）|
| `confirm_delay_ticks` | 2 | 确认等待 10s（B3）|
| `other_delta_weak/strong/vstrong` | 0.01 / 0.02 / 0.05 | 对侧 bid 回升三档 → +1 / +2 / +3 |
| **`score_entry`** | **2** | 任一核心信号成立即触发（od>0.02 或 range<0.5）|
| `score_add` | 99 | 加仓禁用 |

### 2.3 已停用特征（权重/阈值置 0）

| 参数 | 原值 → 新值 | 停用理由 |
|---|---|---|
| `w_oscillating` | 1 → **0** | χ² p=0.32 无统计效力 |
| `w_cheap_entry_strong/weak` | 1/1 → **0/0** | 越便宜 WR 越低，且用不可成交的对侧 bid 判价 |
| `w_btc_extreme` | 1 → **0** | 由 B1 硬过滤取代 |
| `path_eff_veto_min` | 0.4 → **0（禁用）** | 否决滤掉 EV 偏好的候选 |
| `noise_ratio_veto_max` | 3.0 → **0（禁用）** | 中性，纯减信号 |

### 2.4 成交与 gate 口径（实盘对齐，重要）

- `yes_price/no_price` 存的是各订单簿 **best bid**（`bestBid()`）
- 实盘 FAK 买入对侧成交在 **ask**：`对侧 ask = 1 - 触发侧 bid`（双 token 互补）
- **fill 与 gate 均按 ask 口径**：确认时刻 `1 - 触发侧bid > max_entry_price(0.45)` → 信号无效
- 与 Trader 的 FAK 校验（最优卖价 vs max_price，`trader.go:249`）完全一致

---

## 3. 回测基准结果（data_0，1719 事件，ask 口径）

```
总信号数: 126（~20/日）      胜率: 46.0% (Wilson 38-55)
总 P&L:   +26.91            单笔 EV: +0.214
利润因子: 2.60               avg_fill: 0.247
逐日 EV: 6 个完整日全部为正（+0.16 ~ +0.35）
Split A: train +0.226 / test +0.193
Split B: train +0.206 / test +0.263
```

对照当前参数（Formula A）：n=188、WR 41.5%、EV +0.094、P&L +17.70、PF 1.51、
2/6 完整日负 EV。Formula B 信号少 33%，单笔 EV ×2.3，总 P&L +52%。

2 USDC stake 折算（股数 = 2/fill）：**Formula B ≈ +218 USDC / 8 天**（当前 ≈ +120）。

---

## 4. 上线步骤

1. **Python 回测复核**（本轮）：`python backtest_flip_scoring.py --profile btc --data ../data_0/lab/`
   验收线：n=126、WR 46.0%、P&L +26.91、PF 2.60、avg_fill 0.247。
2. **Go 侧落地**（§5 改动清单，需先通过 go build + go test）。
3. **纸面运行 2-3 天**：核对信号频率（预期 ~20/日）、逐笔特征与回测分布一致、
   FAK 前 gate 行为（ask 口径）无异常。
4. **实盘小 stake 起步**：`trading.enabled: true`、`stake_per_signal: 2.0`、
   `max_price: 0.45`，观察 1-2 周实盘成交率与滑点。
5. **复盘**：对比实盘 fill 与回测 ask 口径的偏差，确认 spread 模型（~1 分）仍成立。

---

## 5. Go 侧改动清单（待执行）

### 5.1 `internal/flip/types.go`

1. `FlipConfig` 新增字段：
   ```go
   // B1 背离硬要求：穿越时刻 BTC 必须与 PM 反向（背离度 = ±btc_pos，YES侧取负）。
   // 背离度 < 此值 → 否决。0 = 禁用。2026-08-13 Formula B: 0.05
   MinDivergence float64 `mapstructure:"min_divergence"`
   ```
2. `DefaultConfig()` 变更：
   - `MinDivergence: 0.05`（新增）
   - `ScoreEntry: 2`（原 5）
   - `WOscillating: 0`、`WCheapEntryStr/Weak: 0`、`WBtcExtreme: 0`
   - `PathEffVetoMin: 0`（禁用，原 0.4）、`NoiseRatioVetoMax: 0`（禁用，原 3.0）
   - 其余（TriggerThreshold/AllowRetryCrossings/MinPreSnaps/MaxRemainingSec/MinRemainingSec/
     HistWindowN/RangeExpThreshold/RangeExpMax/ConfirmDelayTicks/OD 三档/MaxEntryPrice 0.45/
     MaxLatencyMs）不变
   - 更新注释：引用 `docs/flip_strategy_plan_2026-08-13.md`，删除「2026-08-12 网格搜索」旧注释

### 5.2 `internal/flip/scoring.go` / `engine.go`

1. **新增 B1 背离硬过滤**：在 `evaluateCrossingAt`/`enterConfirming` 的 T=0 过滤处：
   ```go
   divergence := btcPos
   if side == "yes" { divergence = -btcPos }
   if cfg.MinDivergence > 0 && divergence < cfg.MinDivergence {
       return nil // 同向/中性穿越：EV≈0，否决
   }
   ```
2. **gate 口径统一为 ask**（`engine.go:737` 与 `:603`）：
   ```go
   confirmPrice := 1.0 - confSnap.YesPrice // side=yes（买NO: 对侧ask = 1 - YES bid）
   confirmPrice := 1.0 - confSnap.NoPrice  // side=no（买YES）
   ```
   替代现在的 `confSnap.NoPrice / confSnap.YesPrice`（对侧 bid），与 trader.go 的
   最优卖价校验口径一致。
3. 评分函数 `ComputeFlipScore`：振荡/低价入场/btc_extreme 分支保留代码但权重由
   config 控制（已置 0），无需删除逻辑。
4. FlipSignal 输出新增 `BtcDivergence` 字段（JSON: `btc_divergence`），与 Python 对齐。

### 5.3 `config.yaml` / `config.example.yaml`

```yaml
flip:
  trigger_threshold: 0.7
  allow_retry_crossings: true
  min_pre_snaps: 5
  max_remaining_sec: 260
  min_remaining_sec: 35
  min_divergence: 0.05        # 新增：B1 背离硬要求
  path_eff_oscillating: 0.7   # 停用特征，字段保留
  noise_ratio_oscillating: 1.5
  flips_oscillating: 1
  hist_window_n: 18
  range_exp_threshold: 0.5
  range_exp_max: 1.5
  confirm_delay_ticks: 2
  od_hard_filter: -999.0
  other_delta_vstrong: 0.05
  other_delta_strong: 0.02
  other_delta_weak: 0.01
  btc_pos_max: 0.1            # 停用特征，字段保留
  btc_pos_min: -0.1
  path_eff_veto_min: 0        # 停用（原 0.4）
  noise_ratio_veto_max: 0     # 停用（原 3.0）
  entry_cheap_strong: 0.20    # 停用特征，字段保留
  entry_cheap_weak: 0.25
  max_entry_price: 0.45       # ASK 口径 gate，与 trading.max_price 一致
  w_other_delta_vstrong: 3
  w_other_delta_strong: 2
  w_other_delta_weak: 1
  w_oscillating: 0            # 停用（原 1）
  w_cheap_entry_strong: 0     # 停用（原 1）
  w_cheap_entry_weak: 0       # 停用（原 1）
  w_range_expansion: 2
  w_btc_extreme: 0            # 停用（原 1），由 min_divergence 取代
  score_entry: 2              # 原 5
  score_add: 99
  max_latency_ms: 0
```

### 5.4 单元测试

- `engine_test.go` 需同步：现有用例按 Formula A 参数断言，需改为 Formula B 参数
  或显式构造旧参数跑兼容用例。
- 新增用例：B1 背离否决（同向穿越不触发）、ask 口径 gate（触发侧 bid 回落 → gate 放行）。

---

## 6. 风险与局限

1. **样本仍是 8 天（完整日 6 天）**。n=126 的 Wilson CI 38-55%，结构稳健性
   （双 Split、阈值平台、语义双验证）比单点胜率更可信，但不足以排除 regime 漂移。
2. **信号频率波动**：完整日 n 在 4~44 之间（08-08 单日 44 笔），日频依赖行情。
3. **spread 模型假设**：fill 按「对侧 ask = 1 - 触发侧 bid」估算（实测 spread ~1 分），
   实盘 FAK 吃多层流动性时可能略差于该估算。
4. **侧别不对称在 div 池内反转**（NO 侧触发更好），Formula B 保持两侧都做，
   不套用「只做 YES 侧」的旧结论。
5. **参数变更需 Go 单测全绿后再上线**；实盘小 stake 观察期不可跳过。
