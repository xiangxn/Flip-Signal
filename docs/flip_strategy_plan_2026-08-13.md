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

> ✅ **已落地（2026-08-13）**：Go（flip 包/配置/测试）+ Python 回测均已按本规格实现并验收。
> 停用特征与兼容模式的配置项/代码已**全部删除**（非置零保留）。

### 2.1 触发与窗口（Layer 0）

| 参数 | 值 | 说明 |
|---|---|---|
| `trigger_threshold` | 0.7 | 一侧 bid > 0.7 触发 |
| `min_pre_snaps` | 5 | 穿越前至少 5 个 snapshot |
| `max_remaining_sec` | 260 | 太早 = BTC 路径太短 |
| `min_remaining_sec` | 35 | 确认(2 ticks)+FAK 下单余量 |

多穿越重试为引擎固定行为（每个上升沿都尝试评分，首过者胜，每周期一注），
不再作为配置项（原 `allow_retry_crossings` 兼容模式已删除）。

### 2.2 信号条件（Formula B 核心）

| 参数 | 值 | 说明 |
|---|---|---|
| **`min_divergence`** | **0.05（新增）** | 背离硬要求：YES 侧触发要求 `btc_pos < -0.05`；NO 侧触发要求 `btc_pos > 0.05`。不满足 → 否决；历史振幅未就绪的事件整体跳过（与回测一致） |
| `range_exp_threshold` | 0.5 | `range_exp < 0.5` → +2（B2）|
| `range_exp_max` | 1.5 | `range_exp ≥ 1.5` → 真突破否决（F0）|
| `confirm_delay_ticks` | 2 | 确认等待 10s（B3）|
| `other_delta_weak/strong/vstrong` | 0.01 / 0.02 / 0.05 | 对侧 bid 回升三档 → +1 / +2 / +3 |
| **`score_entry`** | **2** | 任一核心信号成立即触发（od>0.02 或 range<0.5）|
| `score_add` | 99 | 加仓禁用 |

### 2.3 已删除的特征与配置项

| 删除项 | 删除理由 |
|---|---|
| `w_oscillating` + `path_eff_oscillating` + `noise_ratio_oscillating` + `flips_oscillating`（F3 振荡） | χ² p=0.32 无统计效力 |
| `w_cheap_entry_strong/weak` + `entry_cheap_strong/weak`（F4/F5 低价入场） | 越便宜 WR 越低，且用不可成交的对侧 bid 判价 |
| `w_btc_extreme` + `btc_pos_max/min`（F7） | 由 B1 硬过滤取代 |
| `path_eff_veto_min`（0.4） | 否决滤掉 EV 偏好的候选 |
| `noise_ratio_veto_max`（3.0） | 中性，纯减信号 |
| `allow_retry_crossings`（兼容模式） | 引擎固定多穿越重试，兼容分支已删 |

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

1. ~~**Python 回测复核**~~ ✅ 已完成：n=126、WR 46.0%、P&L +26.91、PF 2.60、avg_fill 0.247，验收通过。
2. ~~**Go 侧落地**~~ ✅ 已完成（2026-08-13）：见 §5 执行记录，`go build` + `go test ./...` 全绿。
3. **纸面运行 2-3 天**：核对信号频率（预期 ~20/日）、逐笔特征与回测分布一致、
   FAK 前 gate 行为（ask 口径）无异常。
4. **实盘小 stake 起步**：`trading.enabled: true`、`stake_per_signal: 2.0`、
   `max_price: 0.45`，观察 1-2 周实盘成交率与滑点。
5. **复盘**：对比实盘 fill 与回测 ask 口径的偏差，确认 spread 模型（~1 分）仍成立。

---

## 5. Go 侧执行记录（✅ 已完成 2026-08-13）

停用特征与兼容模式的**配置项与代码全部删除**（非置零保留）：

| 文件 | 改动 |
|---|---|
| `internal/flip/types.go` | FlipConfig：新增 `MinDivergence=0.05`、`ScoreEntry=2`；删除振荡三条件/低价入场两档/btc_pos 阈值/两个 T=0 否决/对应权重/`AllowRetryCrossings` 全部字段。FlipSignal/ScoreParams：删除 path_eff/noise/flips/osc/btc_extreme，新增 `BtcDivergence` |
| `internal/flip/features.go` | 删除 PathEfficiency/TotalPath/NoiseRatio/CountFlips/IsOscillating；保留 RangeExpansion/BTCPosition；新增 `Divergence(side, btcPos)` |
| `internal/flip/scoring.go` | `ComputeFlipScore` 改为 Formula B：L0 延迟否决 + F0 真突破否决 + B3 三档 + B2 过度自信 |
| `internal/flip/engine.go` | 删除兼容模式分支与旧特征计算；`enterConfirming`/`evaluateCrossingAt` 新增 B1 背离硬过滤（hist 未就绪即否决，与回测一致）；gate 已为 ask 口径；信号字段对齐 |
| `internal/dashboard/` | handlers.go/app.js/index.html：PathEff/Noise/Osc/BTCExtreme 列与面板替换为 BtcDivergence；删除 Multi-Crossing 开关显示 |
| `cmd/flip/main.go` | 启动日志与 SIGNAL 日志改为 Formula B 字段（div/range/other_d） |
| `config.yaml` / `config.example.yaml` | flip 段重写：删除全部停用项与 `allow_retry_crossings`，新增 `min_divergence: 0.05`、`score_entry: 2` |
| `internal/flip/engine_test.go` | 重写：删除振荡/veto/兼容模式用例；新增 B1 背离否决、hist 未就绪否决、pending 穿越评估用例；gate 用例按 ask 口径 |
| `python/` | backtest_flip_config/utils/scoring 同步删除停用项与兼容分支；验收 n=126/WR 46.0%/+26.91 与分析一致 |

`min_divergence` 语义：YES 侧触发要求 `btc_pos < -0.05`，NO 侧触发要求 `btc_pos > 0.05`；
历史振幅未就绪的事件整体跳过（与 Python 回测行为一致）。

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
