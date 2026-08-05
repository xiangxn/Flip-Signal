# 复合评分版回测实施计划

> 基于 `docs/data_flip_analysis.md` 第 10 节复合评分系统
> 目标: 实现纯实时（无未来信息）的翻转信号回测

---

## 1. 数据模型

### 1.1 输入: 事件数据 (JSONL)

每个事件代表一个 5 分钟 Polymarket BTC 市场周期:

```python
Event = {
    "start_time":    int,        # 市场开始时间 (unix)
    "open_price":    float,      # BTC 5m K线开盘价
    "close_price":   float,      # BTC 5m K线收盘价
    "outcome":       int,        # 0=UP赢, 1=DOWN赢
    "snapshots":     [Snapshot], # 每秒数据
}
```

### 1.2 Snapshot 结构

```python
Snapshot = {
    "ts":              int,      # 时间戳 (ms)
    "remaining_sec":   int,      # 距离市场结束剩余秒数
    "open":            float,    # BTC 开盘价 (同事件)
    "price":           float,    # BTC 当前价格
    "ret_10s":         float,    # 10秒收益率 (2 ticks × 5s)
    "buy_vol_5s":      float,    # 5秒主动买入量
    "sell_vol_5s":     float,    # 5秒主动卖出量
    "signed_flow_5s":  float,    # 5秒净订单流 = buy_vol - sell_vol
    "vol_10s":         float,    # 10秒累计成交量
    "vol_30s":         float,    # 30秒累计成交量
    "bid_depth":       float,    # BTC 买盘深度
    "ask_depth":       float,    # BTC 卖盘深度
    "yes_price":       float,    # Polymarket YES token 价格 [0, 1]
    "no_price":        float,    # Polymarket NO token 价格 [0, 1]
}
```

### 1.3 额外计算: 历史振幅基准

对每个事件，计算前 N 根 K 线的平均振幅:

```python
N = 18  # 前18根 ≈ 1.5小时

# 事件需按 start_time 排序
for i, event in enumerate(events_sorted):
    prev_ranges = [
        events[j].btc_range()
        for j in range(max(0, i-N), i)
    ]
    event.hist_avg_range = mean(prev_ranges) if len(prev_ranges) >= 3 else None
```

---

## 2. 特征定义 & 初始参数

所有特征只在 **>0.7 第一次穿越时** 计算。特征分为两类:
- **T=0 特征**: 穿越时刻立即可知
- **T=+5s 特征**: 需等 5 秒后确认

---

### 2.1 前置条件 (Layer 0)

| 参数 | 值 | 说明 |
|------|-----|------|
| `trigger_threshold` | **0.7** | PM 一侧价格超过此值触发 |
| `first_crossing_only` | **True** | 仅在首次穿越时触发，后续穿越忽略 |
| `min_pre_snaps` | **5** | 穿越前至少需要 5 个 snapshot |

---

### 2.2 路径效率 — path_eff

```
path_eff = abs(穿越时BTC价 - Open) / pre_range
```

| 子特征 | 公式 | 说明 |
|--------|------|------|
| `net_move` | abs(price_at_cross - open_price) | BTC 从开盘到穿越的净位移 |
| `pre_range` | max(pre_prices) - min(pre_prices) | 穿越前的价格振幅 |
| `pre_high` | max(pre_prices) | 穿越前最高价 |
| `pre_low` | min(pre_prices) | 穿越前最低价 |

**解读**: 
- ~1.0 = 单边趋势 (价格直达方向，没回头)
- ~0.0 = 来回振荡 (价格来回走，净位移小)

**初始阈值**: `path_eff ≤ 0.5` → 判定为来回振荡 (5s数据校准)

---

### 2.3 噪声比 — noise_ratio

```
total_path = sum(abs(pre_prices[i] - pre_prices[i-1]) for i in 1..n)
noise_ratio = total_path / net_move
```

| 子特征 | 公式 | 说明 |
|--------|------|------|
| `total_path` | Σ\|p[i] - p[i-1]\| | 累计 tick 级别路径长度 |

**解读**: 噪声比越高 = 越震荡。来回振荡通常 >5，单边趋势通常 <3。

**初始阈值**: 辅助判断，`noise_ratio > 5` 加强振荡判定

---

### 2.4 方向翻转次数 — flips

```python
flips = 0
for i in range(2, len(pre_prices)):
    d1 = sign(pre_prices[i-1] - pre_prices[i-2])
    d2 = sign(pre_prices[i] - pre_prices[i-1])
    if d1 != 0 and d2 != 0 and d1 != d2:
        flips += 1
```

**解读**: 穿越前价格方向变化的总次数。

**初始阈值**: `flips > 2` 加强振荡判定 (5s数据校准)

---

### 2.5 来回振荡综合判定

三个子特征组合判断:

```python
is_oscillating = (
    path_eff <= 0.5 and      # 5s数据校准 (原 0.4)
    noise_ratio > 5 and
    flips > 2                # 5s数据校准 (原 10)
)
```

> **说明**: 三个条件需同时满足。阈值已根据 5s 数据重新校准，路径效率是核心，噪声比和翻转次数为辅助确认。

---

### 2.6 振幅扩张 — range_expansion

```
range_expansion = abs(price_at_cross - open_price) / hist_avg_range
```

| 参数 | 值 | 说明 |
|------|-----|------|
| `hist_window_N` | **18** | 前 18 根 K 线 (~1.5小时, sweep最优) |
| `hist_avg_range` | avg(\|close-open\|) | 历史平均振幅 (USDT) |

**解读**: BTC 从 Open 到穿越的绝对位移，以历史平均振幅为单位。
- < 0.5x: BTC 还没怎么动，PM 已经 >0.7 → 过度自信 → +2
- 0.5x ~ 1.5x: 正常范围
- ≥ 2.0x: BTC 大幅移动，PM 是对的 → 真突破，一票否决

与 tick 频率无关，实盘流式数据同样适用。

**初始阈值**: `range_expansion < 0.5` → +2 分; `range_expansion ≥ 2.0` → 不交易 (F0 否决)

---

### 2.7 对面价格 5 秒变化 — other_delta_5s ★★★ (最强特征)

```
other_delta_5s = other_price[t+5s] - other_price[t]
```

| 参数 | 值 | 说明 |
|------|-----|------|
| `confirm_delay` | **5 秒** | 等待时间 |
| `side` | yes / no | 当前 >0.7 的方向 |
| `this_price` | yes_price 或 no_price | >0.7 那一边的价格 |
| `other_price` | no_price 或 yes_price | 对面的价格 (入场价) |

**评分规则**:

| 条件 | 加分 | 独立翻转率 |
|------|------|-----------|
| `other_delta_5s > 0.03` | **+4** | ~50% |
| `other_delta_5s > 0.01` | **+2** | ~43% |
| $-0.02$ ~ $0.02$ | 0 | ~24% |
| `other_delta_5s < -0.02` | 不触发 | ~13% |

---

### 2.8 BTC 位置 (穿越前) — btc_position

```
btc_position = (price_at_cross - open_price) / hist_avg_range
```

| 参数 | 值 | 说明 |
|------|-----|------|
| `hist_avg_range` | 前 18 根 K 线 \|close-open\| 均值 | 历史平均振幅 (USDT) |

**解读**: BTC 从 Open 到穿越时刻的位移，以历史平均振幅为单位。
- +1.0 = BTC 涨了 1 倍历史振幅
- -1.0 = BTC 跌了 1 倍历史振幅
- 与 tick 频率无关，实盘流式数据同样适用。

**评分规则**: 

| 场景 | 条件 | 加分 |
|------|------|------|
| YES>0.7 (PM看涨) | 0 < btc_position < 0.5 | +1 |
| NO>0.7 (PM看跌) | -0.5 < btc_position < 0 | +1 |

> **逻辑**: BTC 在 PM 方向上只走了不到 0.5x 历史振幅 → PM 的强信念缺乏 BTC 走势支撑 → 更可能翻转。
> BTC 在 PM 方向上走了 >1x 历史振幅 → PM 是对的，不下注。

---

### 2.9 入场价格 — entry_price

```
IF side == 'yes':  entry_price = no_price  (买NO, 赌DOWN赢)
IF side == 'no':   entry_price = yes_price (买YES, 赌UP赢)
```

**评分规则**:

| 条件 | 加分 | 说明 |
|------|------|------|
| `entry_price < 0.20` | **+2** | 极低价入场，赔率 >5:1 |
| `entry_price < 0.25` | **+1** | 低价入场，赔率 >4:1 |

---

### 2.10 复合评分汇总

| # | 特征 | 条件 | 加分 | 类型 |
|---|------|------|------|------|
| F1 | 对面5s变化 | > 0.03 | **+4** | 确认信号 (T+5s) |
| F2 | 对面5s变化 | > 0.01 | **+2** | 确认信号 (T+5s) |
| F3 | 来回振荡 | eff≤0.5 & noise>5 & flips>2 | **+1** | 实时 (T=0) |
| F4 | 低价入场 | < 0.20 | **+2** | 实时 (T=0) |
| F5 | 低价入场 | < 0.25 | **+1** | 实时 (T=0) |
| F6 | 振幅过小 (BTC没动) | < 0.5x hist | **+2** | 实时 (T=0) |
| F7 | BTC微动 (PM方向) | YES: 0<pos<0.5, NO: -0.5<pos<0 | **+1** | 实时 (T=0) |
| **F0** | **振幅过大 (一票否决)** | **≥ 2.0x hist** | **信号无效** | 实时 (T=0) |

> **F0 振幅过大否决**: 当 `range_expansion ≥ 2.0` 时，BTC 已从 Open 移动超过 2 倍历史平均振幅。这是真正的突破行情，PM 方向大概率正确。**直接跳过，不参与评分。**

**满分**: 13 分

**入场阈值**:

| 分数 | 动作 | 
|------|------|
| `range_expansion ≥ 2.0` | **不交易** (真突破，一票否决) |
| `score < 5` | 不交易 |
| `score ≥ 5` | **开仓** (买对面 1 share) |
| `score ≥ 7` | **加仓** (买对面 2 shares) |

---

## 3. 回测流程

### 3.1 主循环

```python
results = []

for event in events:
    # Step 0: 跳过历史振幅不足的事件
    if event.hist_avg_range is None:
        continue
    
    # Step 1: 寻找第一次 >0.7 穿越
    for side in ['yes', 'no']:
        signal = check_signal(event, side)
        if signal:
            results.append(signal)
            break  # 每个事件最多触发一次
```

### 3.2 信号检测 (check_signal)

```python
def check_signal(event, side):
    """对给定 side (yes/no) 检测 >0.7 穿越并评估信号"""
    
    this_key = 'yes_price' if side == 'yes' else 'no_price'
    other_key = 'no_price' if side == 'yes' else 'yes_price'
    snaps = event.snapshots
    
    # Step 1: 找第一次穿越
    cross_idx = None
    for i, s in enumerate(snaps):
        if s[this_key] > 0.7:
            cross_idx = i
            break
    
    if cross_idx is None or cross_idx < 5:
        return None  # 穿越太早，数据不够
    
    # Step 2: 提取 T=0 实时特征
    cross_snap = snaps[cross_idx]
    pre_snaps = snaps[:cross_idx + 1]
    pre_prices = [s.price for s in pre_snaps]
    
    pre_high = max(pre_prices)
    pre_low = min(pre_prices)
    pre_range = pre_high - pre_low
    
    if pre_range == 0:
        return None
    
    net_move = abs(cross_snap.price - event.open_price)
    
    # F3: 路径效率
    path_eff = net_move / pre_range
    
    # 噪声比
    total_path = sum(abs(pre_prices[i] - pre_prices[i-1]) 
                     for i in range(1, len(pre_prices)))
    noise_ratio = total_path / net_move if net_move > 0 else total_path
    
    # 翻转次数
    flips = 0
    for i in range(2, len(pre_prices)):
        d1 = sign(pre_prices[i-1] - pre_prices[i-2])
        d2 = sign(pre_prices[i] - pre_prices[i-1])
        if d1 != 0 and d2 != 0 and d1 != d2:
            flips += 1
    
    is_oscillating = (path_eff <= 0.5 and noise_ratio > 5 and flips > 2)
    
    # F6: 振幅扩张 (tick-independent)
    range_expansion = abs(cross_snap.price - event.open_price) / event.hist_avg_range
    
    # F0: 振幅过大 — 一票否决 (真突破, 不交易)
    if range_expansion >= 2.0:
        return None
    
    # F7: BTC 位置 (tick-independent)
    btc_position = (cross_snap.price - event.open_price) / event.hist_avg_range
    if side == 'yes':
        btc_extreme = (0 < btc_position < 0.5)
    else:
        btc_extreme = (-0.5 < btc_position < 0)
    
    # F4/F5: 入场价
    entry_price = cross_snap[other_key]
    
    # Step 3: 等 5 秒 → T+5s 确认特征 (5s数据: 1 tick = 5s)
    conf_idx = min(cross_idx + 1, len(snaps) - 1)
    other_delta_5s = snaps[conf_idx][other_key] - cross_snap[other_key]
    
    # Hard filter: 对面下跌 >0.02 → 不触发
    if other_delta_5s < -0.02:
        return None
    
    # Step 4: 计算评分
    score = 0
    if other_delta_5s > 0.03:
        score += 4
    elif other_delta_5s > 0.01:
        score += 2
    
    if is_oscillating:
        score += 1
    
    if entry_price < 0.20:
        score += 2
    elif entry_price < 0.25:
        score += 1
    
    if range_expansion < 0.5:
        score += 2
    
    if btc_extreme:
        score += 1
    
    # Step 5: 判断入场
    if score < 5:
        return None
    
    # Step 6: 判定输赢
    # outcome=0 → UP赢, outcome=1 → DOWN赢
    if side == 'yes':
        # YES>0.7, 我们买 NO (赌 DOWN)
        won = (event.outcome == 1)  # DOWN 赢了
    else:
        # NO>0.7, 我们买 YES (赌 UP)
        won = (event.outcome == 0)  # UP 赢了
    
    pnl = (1.0 - entry_price) if won else (0.0 - entry_price)
    
    return {
        'event_time': event.start_time,
        'side': side,
        'score': score,
        'entry_price': entry_price,
        'won': won,
        'pnl': pnl,
        'remaining_sec': cross_snap.remaining_sec,
        # 特征详情 (debug用)
        'path_eff': path_eff,
        'noise_ratio': noise_ratio,
        'flips': flips,
        'is_oscillating': is_oscillating,
        'range_expansion': range_expansion,
        'btc_position': btc_position,
        'btc_extreme': btc_extreme,
        'other_delta_5s': other_delta_5s,
    }
```

### 3.3 回测汇总

```python
signals = [r for r in results if r is not None]

wins = sum(1 for s in signals if s['won'])
total_pnl = sum(s['pnl'] for s in signals)

print(f"总信号数: {len(signals)}")
print(f"胜率: {wins/len(signals)*100:.1f}%")
print(f"总P&L: {total_pnl:+.2f}")
print(f"平均入场: {mean(s['entry_price'] for s in signals):.3f}")
print(f"平均分数: {mean(s['score'] for s in signals):.1f}")

# 按分数分组
for score_range in [(0,5), (5,7), (7,99)]:
    subset = [s for s in signals if score_range[0] <= s['score'] < score_range[1]]
    if subset:
        w = sum(1 for s in subset if s['won'])
        p = sum(s['pnl'] for s in subset)
        print(f"  Score {score_range[0]}-{score_range[1]}: n={len(subset)}, "
              f"win={w/len(subset)*100:.1f}%, P&L={p:+.2f}")
```

---

## 4. 参数汇总表

| 参数 | 初始值 | 类型 | 可调范围 | 说明 |
|------|--------|------|---------|------|
| `trigger_threshold` | 0.7 | 硬阈值 | 0.65-0.80 | PM 穿越触发价 |
| `confirm_delay` | 5 | 秒 | 3-10 | 对面确认等待时间 |
| `hist_window_N` | 18 | 根 | 12-36 | 历史振幅基准窗口 (sweep最优) |
| **评分权重** | | | | |
| `w_other_d5_strong` | +4 | 分 | 3-5 | 对面大涨加分 |
| `w_other_d5_weak` | +2 | 分 | 1-3 | 对面小涨加分 |
| `w_oscillating` | +1 | 分 | 0-2 | 来回振荡加分 |
| `w_cheap_entry_strong` | +2 | 分 | 1-3 | 极低价加分 |
| `w_cheap_entry_weak` | +1 | 分 | 0-2 | 低价加分 |
| `w_range_expansion` | +2 | 分 | 1-3 | 振幅扩张加分 |
| `w_btc_extreme` | +1 | 分 | 0-2 | BTC极端位置加分 |
| **阈值** | | | | |
| `path_eff_oscillating` | ≤0.5 | 硬阈值 | 0.3-0.6 | 振荡判定 (5s数据校准) |
| `noise_ratio_oscillating` | >5 | 硬阈值 | 3-8 | 噪声比判定 |
| `flips_oscillating` | >2 | 硬阈值 | 2-8 | 翻转次数判定 (5s数据校准: 5s粒度下方向变化远少于1s) |
| `range_exp_threshold` | <0.5 | 硬阈值 | 0.3-0.8 | 振幅过小=BTC没动, PM过度自信 (加分) |
| `range_exp_max` | ≥2.0 | 一票否决 | 1.5-2.5 | 振幅过大，真突破，不交易 |
| `btc_extreme_high` | >0.8 | 硬阈值 | 0.7-0.9 | BTC高位 |
| `btc_extreme_low` | <0.2 | 硬阈值 | 0.1-0.3 | BTC低位 |
| `entry_cheap_strong` | <0.20 | 硬阈值 | 0.15-0.22 | 极低价 |
| `entry_cheap_weak` | <0.25 | 硬阈值 | 0.20-0.28 | 低价 |
| **入场** | | | | |
| `score_entry` | ≥5 | 硬阈值 | 4-6 | 开仓线 |
| `score_add` | ≥7 | 硬阈值 | 6-8 | 加仓线 |

---

## 5. 回测输出

### 5.1 核心指标

| 指标 | 预期值 (2天, 287事件) |
|------|----------------------|
| 总信号数 | ~100 |
| 日信号数 | ~50 |
| 胜率 | ~43.0% |
| 总P&L (1 share) | ~+19.09 |
| 平均入场价 | ~0.248 |
| 盈亏比 | >2.0 |

### 5.2 分组输出

按分数分桶:
- Score 0-4: 未触发 (用于对比基准翻转率)
- Score 5-6: 入场 
- Score 7+: 加仓

按特征分桶:
- 来回振荡 vs 单边趋势
- 振幅扩张 vs 正常 vs 收缩
- 对面上升 vs 持平 vs 下跌

### 5.3 逐笔记录

每笔信号输出为 JSONL，包含所有特征值和 P&L，用于后续分析。

---

## 6. 已知问题 & 注意事项

1. **buy_vol_5s 全为 0 (已修复)**: 采集代码存在 bug（Go JSON 大小写不敏感回退匹配导致 IsBuyerMM 被 M 字段覆盖），已修复。重新采集后的数据 buy_vol_5s 将恢复正常。历史数据仍然全为 0。

2. **signed_flow_5s 降级 (历史数据)**: 已采集的历史数据中 `signed_flow_5s = -sell_vol_5s`（因 buy_vol_5s 全为 0），仅反映卖出压力。Bug 已修复，新数据将正常。

3. **振幅过大 (≥2.0x) 一票否决**: BTC 从 Open 移动超过 2 倍历史平均振幅时，是真突破行情，PM 方向大概率正确。已在 F0 中硬过滤，直接返回 None 不交易。

4. **2 天数据量有限**: 287 事件中 score≥5 的信号仅 ~100 个。需要更多数据验证稳定性。

5. **滑点未考虑**: 理论计算按穿越+5s 时的对面价入场，实盘中可能有滑点。

6. **PM 盘口深度**: 小额 (1 share) 通常无问题，大额需要检查 orderbook 深度。

---

## 7. 实现文件建议

```
python/
├── backtest_flip_scoring.py    # 主回测脚本 (本文档实现)
├── backtest_flip_utils.py      # 特征提取函数库
└── backtest_flip_config.py     # 参数配置 (方便调参)

output/
└── flip_signals_2026-08-03_04.jsonl  # 回测信号输出
```

---

*初始参数版本: v1.0*
*待积累更多数据后调优*
