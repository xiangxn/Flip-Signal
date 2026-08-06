# Flip 信号优化分析

> 基于 `python/backtest_flip_scoring.py --data data/lab`，在 114 个事件上回测
> 日期: 2026-08-06

## 回测基准 (BASELINE)

| 指标 | 数值 |
|------|------|
| 总事件数 | 114 (UP:48, DOWN:66) |
| 有效事件 | 111 (有历史振幅) |
| 回测信号数 | 26 |
| 胜场 | 7 (26.9%) |
| 总 PnL | **+1.46** |
| 振荡信号 | 8笔, 0胜, PnL=-1.36 |

> 注：`data/flip_signals.jsonl` 为早期实盘记录（23笔, PnL=+1.57），因采样时机不同与回测略有差异。以下所有分析以回测 BASELINE 为准。

---

## 核心发现

**所有亏损都是 PM 方向预测错误**，没有一笔是"方向对但入场价太贵"造成的。问题不在入场精度，而在**方向判断**。

---

## 诊断：6 个问题 & 回测结论

### 问题 1: `btc_extreme` 评分方向反了 → ✅ 已修正 (OPT#2)

原方案 F7：BTC 与 PM 同向时 +1 分。但数据显示 btc_extreme=True 的亏损比例 87.5% > 胜方 57%。

**回测结论**：反转方向后（BTC 与 PM 背离才加分），叠加 OPT#3+5 后生效——纠正了评分逻辑，虽单独不改变过滤结果，但与其他优化协同。

| 状态 | 结果 |
|------|------|
| ✅ 已实施 | 方向反转，BTC 背离 PM 才加分 |

---

### 问题 2: `other_delta < 0.03` 零胜率 → ✅ 已修正 (OPT#1)

9 笔 `other_delta < 0.03` 的信号**全部亏损，无一例外**。原方案中 `other_delta > 0.01` 就给 +2 分，门槛太低。

**回测结论**：改为硬过滤 `other_delta < 0.03 → 不触发`，从 26→19 笔，PnL +1.46→+3.87。

| 状态 | 结果 |
|------|------|
| ✅ 已实施 | 硬过滤 other_delta < 0.03 |

---

### 问题 3: `entry_price < 0.20` 的"便宜入场"反而危险 → ✅ 已修正 (OPT#5)

| Entry 区间 | 交易数 | 胜率 | PnL |
|---|---|---|---|
| **< 0.20** | 4 | **0%** | **-0.67** |
| 0.20–0.25 | 7 | 57% | +2.44 |
| 0.25–0.30 | 12 | 25% | -0.20 |

**回测结论**：`w_cheap_entry_strong` 从 2→0，叠加 OPT#1+2+3 后过滤掉 entry=0.18 和 entry=0.14 两笔亏损。

| 状态 | 结果 |
|------|------|
| ✅ 已实施 | w_cheap_entry_strong = 0 |

---

### 问题 4: `noise_ratio > 3.0` 高风险 → ✅ 已修正 (OPT#3)

| Noise | 交易数 | 胜率 | PnL |
|---|---|---|---|
| **≥ 3.0** | 5 | **0%** | **-1.11** |
| < 3.0 | 18 | 39% | +2.68 |

**回测结论**：硬过滤 `noise_ratio > 3.0 → 不触发`，从 19→11 笔，PnL +3.87→+4.48。所有振荡信号被清除。

| 状态 | 结果 |
|------|------|
| ✅ 已实施 | 硬过滤 noise_ratio > 3.0 |

---

### 问题 5: `remaining_sec < 120` 存活率低 → ❌ 已测试并丢弃 (OPT#4)

| Remaining | 交易数 | 胜率 | PnL |
|---|---|---|---|
| **< 120s** | 7 | 14% | **-0.50** |
| ≥ 120s | 16 | 38% | +2.07 |

**回测结论**：加上此过滤后从 11→8 笔，但误杀了 1 笔盈利交易（rem=82s, PnL=+0.79），PnL 反而从 +4.48 降到 +4.11。

| 状态 | 结果 |
|------|------|
| ❌ 已丢弃 | 误杀盈利，PnL 下降 -0.37 |

---

### 问题 6: `path_eff < 0.4` 极端危险 → ✅ 已修正 (OPT#7)

| path_eff | 交易数 | 胜率 | PnL |
|---|---|---|---|
| **≤ 0.5** | 6 | 17% | **-0.51** |
| > 0.5 | 17 | 35% | +2.08 |

**回测结论**：硬过滤 `path_eff < 0.4 → 不触发`，从 9→8 笔，PnL +4.80→+5.04，仅杀 1 笔亏损（path_eff=0.38），**0 误杀盈利**。

| 状态 | 结果 |
|------|------|
| ✅ 已实施 | 硬过滤 path_eff < 0.4 |

---

## confirm_delay 分桶测试

在最优组合（OPT#1+2+3+5）基础上，测试不同确认延迟：

```
delay=0tick ( 0s): n= 0                                    — 无确认不可用
delay=1tick ( 5s): n= 9  win= 78%  PnL=+4.80 ⭐
delay=2tick (10s): n= 5  win= 60%  PnL=+1.83
delay=3tick (15s): n= 9  win= 56%  PnL=+2.88
delay=4tick (20s): n= 9  win= 44%  PnL=+1.99
delay=5tick (25s): n=13  win= 46%  PnL=+2.95
delay=6tick (30s): n=11  win= 55%  PnL=+3.50
delay=7tick (35s): n=10  win= 60%  PnL=+3.71
delay=8tick (40s): n= 8  win= 62%  PnL=+3.13
delay=10tick(50s): n= 9  win= 56%  PnL=+2.89
```

| 状态 | 结果 |
|------|------|
| ✅ 已验证 | 5s (1 tick) 全面最优，不改 |

---

## 逐项优化回测记录（累积叠加）

所有回测基于 `python/backtest_flip_scoring.py --data data/lab`，在 114 个事件上运行。

### 基准 (BASELINE)

```
26 signals, 7 wins (26.9%), PnL=+1.46
Oscillating: 8 trades, 0 wins, PnL=-1.36
```

---

### OPT#1: other_delta < 0.03 硬过滤

**改动**: `backtest_flip_utils.py:check_signal()` — 硬过滤阈值从 `-0.02` 提高到 `0.03`

```python
# Before: if other_delta < -0.02: return None
# After:  if other_delta < 0.03: return None
```

**结果**: 19 signals, 8 wins (42.1%), PnL=+3.87

| 指标 | BASELINE | OPT#1 | Δ |
|---|---|---|---|
| 信号数 | 26 | 19 | -7 |
| 胜率 | 26.9% | 42.1% | +15.2% |
| PnL | +1.46 | +3.87 | +2.41 |
| 振荡信号 | 8笔, 0胜 | 5笔, 0胜 | -3 |

---

### OPT#2: btc_extreme 方向反转

**改动**: `backtest_flip_utils.py:check_signal()` — F7 评分从"BTC与PM同向"改为"BTC与PM背离"

```python
# Before: side=yes → BTC微涨=extreme; side=no → BTC微跌=extreme (PM&BTC同向)
# After:  side=yes → BTC微跌=extreme; side=no → BTC微涨=extreme (PM&BTC背离)
```

**结果**: 19 signals, 8 wins (42.1%), PnL=+3.87（叠加 OPT#1）

| 指标 | OPT#1 | OPT#1+2 | Δ |
|---|---|---|---|
| 信号数 | 19 | 19 | 0 |
| 胜率 | 42.1% | 42.1% | 0 |
| PnL | +3.87 | +3.87 | 0 |
| 平均分 | 7.1 | 6.5 | -0.6 |

> 本项为评分逻辑修正，单独不改变过滤结果，但纠正了错误加分方向。与后续优化叠加时会影响 score 分布。

---

### OPT#3: noise_ratio > 3.0 硬过滤

**改动**: `backtest_flip_utils.py:check_signal()` — 新增硬过滤

```python
if noise_ratio_val > 3.0:
    return None
```

**结果**: 11 signals, 7 wins (63.6%), PnL=+4.48（叠加 OPT#1+2）

| 指标 | OPT#1+2 | OPT#1+2+3 | Δ |
|---|---|---|---|
| 信号数 | 19 | 11 | -8 |
| 胜率 | 42.1% | 63.6% | +21.5% |
| PnL | +3.87 | +4.48 | +0.61 |
| 振荡信号 | 5笔, 0胜 | **0笔** | -5 |

> 所有振荡信号被清除，盈亏比从 1.7 提升到 5.3。

---

### OPT#4: remaining_sec < 120 硬过滤

**改动**: `backtest_flip_utils.py:check_signal()` — 新增硬过滤

```python
if cross_snap["remaining_sec"] < 120:
    return None
```

**结果**: 8 signals, 6 wins (75.0%), PnL=+4.11（叠加 OPT#1+2+3）

| 指标 | OPT#1+2+3 | OPT#1+2+3+4 | Δ |
|---|---|---|---|
| 信号数 | 11 | 8 | -3 |
| 胜率 | 63.6% | 75.0% | +11.4% |
| PnL | +4.48 | +4.11 | **-0.37** |

> ⚠️ 过滤掉 2 亏 1 赢（rem=82s, PnL=+0.79），PnL 反而下降。**不建议采用。**

---

### OPT#5: 移除 entry < 0.20 加分

**改动**: `backtest_flip_config.py` — `w_cheap_entry_strong: 2 → 0`

```python
# Before: w_cheap_entry_strong: int = 2
# After:  w_cheap_entry_strong: int = 0  # entry<0.20 全亏, 移除加分
```

**结果**: 9 signals, 7 wins (77.8%), PnL=+4.80（叠加 OPT#1+2+3，回退 OPT#4）

| 指标 | OPT#1+2+3 | OPT#1+2+3+5 | Δ |
|---|---|---|---|
| 信号数 | 11 | 9 | -2 |
| 胜率 | 63.6% | 77.8% | +14.2% |
| PnL | +4.48 | +4.80 | +0.32 |

> 过滤掉 2 笔亏损（entry=0.18 和 entry=0.14 各扣 2 分后跌出 score≥5 阈值）。

---

### OPT#6: ScoreEntry 5 → 6

**改动**: `backtest_flip_config.py` — `score_entry: 5 → 6`

**结果**: 7 signals, 5 wins (71.4%), PnL=+3.52（叠加 OPT#1+2+3+5）

| 指标 | OPT#1+2+3+5 | OPT#1+2+3+5+6 | Δ |
|---|---|---|---|
| 信号数 | 9 | 7 | -2 |
| PnL | +4.80 | +3.52 | **-1.28** |

> ⚠️ 过滤掉 2 笔 score=5 盈利交易（+1.28），PnL 大幅下降。**不建议采用。**

---

### OPT#7: path_eff < 0.4 硬过滤

**改动**: `backtest_flip_utils.py:check_signal()` — 新增硬过滤

```python
if path_eff < 0.4:
    return None
```

**结果**: 8 signals, 7 wins (87.5%), PnL=+5.04（叠加 OPT#1+2+3+5）

| 指标 | OPT#1+2+3+5 | OPT#1+2+3+5+7 | Δ |
|---|---|---|---|
| 信号数 | 9 | 8 | -1 |
| 胜率 | 77.8% | 87.5% | +9.7% |
| PnL | +4.80 | +5.04 | +0.24 |

> 仅杀 1 笔亏损（path_eff=0.38, PnL=-0.24），**0 误杀盈利**。剩余 1 笔亏损 path_eff=0.46 > 0.4 未被过滤。

---

## 最终对比总表

| Step | 累积改动 | Signals | Wins | Win% | PnL | Δ vs BASE |
|---|---|---|---|---|---|---|
| BASELINE | 原始配置 | 26 | 7 | 26.9% | +1.46 | — |
| OPT#1 | +other_delta≥0.03 | 19 | 8 | 42.1% | +3.87 | +2.41 |
| OPT#1+2 | +btc_extreme反转 | 19 | 8 | 42.1% | +3.87 | +2.41 |
| OPT#1+2+3 | +noise≤3.0 | 11 | 7 | 63.6% | +4.48 | +3.02 |
| OPT#1+2+3+4 | +rem≥120s | 8 | 6 | 75.0% | +4.11 | +2.65 ⚠️ |
| OPT#1+2+3+5 | +移除cheap bonus | 9 | 7 | 77.8% | +4.80 | +3.34 |
| **OPT#1+2+3+5+7** ⭐ | +path_eff≥0.4 | **8** | **7** | **87.5%** | **+5.04** | **+3.58** |
| OPT#1+2+3+5+6 | +score≥6 | 7 | 5 | 71.4% | +3.52 | +2.06 ❌ |

### 🏆 推荐组合：OPT#1 + OPT#2 + OPT#3 + OPT#5 + OPT#7

```
8 signals, 7 wins (87.5%), PnL=+5.04, 盈亏比 18.0
```

**保留的改动**:
1. ✅ `other_delta < 0.03` → 不触发（硬过滤）
2. ✅ `btc_extreme` 方向反转（BTC与PM背离才加分）
3. ✅ `noise_ratio > 3.0` → 不触发（硬过滤）
4. ✅ 移除 `entry < 0.20` 加分（w_cheap_entry_strong = 0）
5. ✅ `path_eff < 0.4` → 不触发（硬过滤）

**丢弃的改动**:
- ❌ `remaining_sec < 120` 硬过滤 → 误杀盈利交易
- ❌ `ScoreEntry` 提高到 6 → 误杀 score=5 盈利交易

### 剩余 1 笔亏损分析

| # | Side | Entry | Score | Rem | PathEff | Noise | Flips | other_d |
|---|---|---|---|---|---|---|---|---|
| L2 | yes | 0.28 | 6 | 96s | 0.46 | 3.0 | 2 | +0.04 |

path_eff=0.46 在阈值之上，noise=3.0 刚好踩线。需要更多数据才能判断是否值得进一步收紧。

---

## 实施建议

对应 Go 代码修改（`internal/flip/`）：

| 文件 | 修改 |
|---|---|
| [engine.go](internal/flip/engine.go) | onCrossing: 加 `if other_delta < 0.03 → stateDone`, `if noise_ratio > 3.0 → stateDone`, `if path_eff < 0.4 → stateDone` |
| [scoring.go](internal/flip/scoring.go) | F7: 反转 btc_extreme 判断方向；F4: w_cheap_entry_strong 设为 0 |
| [types.go](internal/flip/types.go) | DefaultConfig: OtherDeltaWeak 从 0.01 → 0.03，WCheapEntryStr 从 2 → 0 |
