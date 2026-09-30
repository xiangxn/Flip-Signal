把特征分成 **核心必选、市场状态、盘口质量、派生特征** 四层。

其中我会特别保留你现在已经验证过的几个阈值对应的原始量，**不要让 GBM 直接替代它们**，而是先让 GBM 学习“这些条件同时出现时，成功概率到底是多少”。

| 类别        | 特征                         | 含义 / 计算                      | 优先级 | 我为什么会放进去                       |
| --------- | -------------------------- | ---------------------------- | --- | ------------------------------ |
| 时间        | `remaining_sec`            | 距 5m 结算剩余秒数                  | ⭐⭐⭐ | 所有阶段的基础状态                      |
| 时间        | `stage`                    | `T150 / T60 / listen`        | ⭐⭐⭐ | 三段的条件分布明显不同                    |
| PM价格      | `price`                    | 热门侧有效价，ask 优先、bid 兜底         | ⭐⭐⭐ | 当前入场价格直接决定盈亏平衡胜率               |
| PM价格      | `price_vs_083`             | `price - 0.83`               | ⭐⭐  | 对监听段尤其重要                       |
| PM价格      | `price_vs_080`             | `price - 0.80`               | ⭐⭐  | 对应现有第一道价格门槛                    |
| Spot/TWAP | `dev`                      | `sign × (spot - anchor)`     | ⭐⭐⭐ | 当前策略最核心的方向性变量                  |
| Spot/TWAP | `walk`                     | `sign × (twap - anchor)`     | ⭐⭐⭐ | 已经写入 TWAP 的方向性位移               |
| Spot/TWAP | `basis`                    | `spot - twap`                | ⭐⭐⭐ | `dev = walk + basis`，区分快速/慢速部分 |
| Spot/TWAP | `dev_abs`                  | `abs(dev)`                   | ⭐⭐  | 观察距离大小本身                       |
| 波动        | `sigma_usd`                | `hist_bps × anchor / 1e4`    | ⭐⭐⭐ | 你的 1σ 美元尺度                     |
| 波动        | `dev_sigma`                | `dev / sigma_usd`            | ⭐⭐⭐ | 把绝对价格距离转换成波动尺度                 |
| 波动        | `walk_sigma`               | `walk / sigma_usd`           | ⭐⭐⭐ | 判断 TWAP 已经走了多少个 σ              |
| 波动        | `basis_sigma`              | `basis / sigma_usd`          | ⭐⭐  | 判断 spot 相对 TWAP 的偏离程度          |
| 现有规则      | `dev_minus_63`             | `dev - 63`                   | ⭐   | 保留与现有门槛的关系                     |
| 现有规则      | `walk_minus_43`            | `walk - 43`                  | ⭐   | 保留 T150 特有门槛                   |
| 现有规则      | `sigma_minus_40`           | `sigma_usd - 40`             | ⭐   | 保留 σ 最低有效尺度                    |
| 成交量       | `volume_5m`                | 当前事件累计 BTC 成交量               | ⭐⭐⭐ | 你提出的核心新增变量                     |
| 成交量       | `volume_1m`                | 最近 60s BTC 成交量               | ⭐⭐⭐ | 比整个 5m 累计量更能描述当前状态             |
| 成交量       | `volume_30s`               | 最近 30s BTC 成交量               | ⭐⭐⭐ | 捕捉临近入场时的活跃程度                   |
| 成交量       | `volume_10s`               | 最近 10s BTC 成交量               | ⭐⭐  | 捕捉瞬时爆量                         |
| 成交量       | `volume_ratio_1m`          | `volume_1m / 历史同期正常量`        | ⭐⭐⭐ | 我认为比绝对 volume 更重要              |
| 成交量       | `volume_ratio_30s`         | `volume_30s / 历史同期正常量`       | ⭐⭐⭐ | 判断当前是否突然放量                     |
| 成交量       | `volume_accel`             | 短周期 volume 相对前一周期的变化         | ⭐⭐  | 判断放量是在增强还是衰减                   |
| 价格动量      | `spot_return_5s`           | 最近 5s spot 变化                | ⭐⭐⭐ | 当前方向是否仍在持续                     |
| 价格动量      | `spot_return_10s`          | 最近 10s spot 变化               | ⭐⭐⭐ | 判断趋势持续性                        |
| 价格动量      | `spot_return_30s`          | 最近 30s spot 变化               | ⭐⭐  | 判断这次移动的持续时间                    |
| 价格动量      | `dev_change_5s`            | 最近 5s dev 变化                 | ⭐⭐⭐ | 你之前特别关注的 dev 是否开始收缩            |
| 价格动量      | `dev_change_10s`           | 最近 10s dev 变化                | ⭐⭐⭐ | 判断反转是否已经开始                     |
| TWAP形态    | `walk_change_5s`           | 最近 5s walk 变化                | ⭐⭐  | 判断慢线是否还在向目标方向移动                |
| TWAP形态    | `basis_change_5s`          | 最近 5s basis 变化               | ⭐⭐⭐ | 判断 spot 与 TWAP 的距离变化           |
| 持续性       | `dev_above_63_duration`    | dev 连续 ≥63 USD 的持续时间         | ⭐⭐⭐ | 你之前已经发现 duration 很可能有信息        |
| 持续性       | `dev_above_x_duration`     | dev 连续超过动态阈值的时间              | ⭐⭐  | 比单点 dev 更稳定                    |
| 持续性       | `dev_shrink_duration`      | dev 开始下降后的持续时间               | ⭐⭐⭐ | 对判断“假突破/开始反转”很有价值              |
| 持续性       | `basis_sign_duration`      | basis 当前方向持续时间               | ⭐⭐  | 区分快速价偏离还是稳定状态                  |
| 盘口        | `book_latency_ms`          | 当前 tick 的盘口延迟                | ⭐⭐⭐ | 数据质量控制，同时可能反映行情状态              |
| 盘口        | `bid_ask_spread`           | 热门侧 bid/ask spread           | ⭐⭐  | 判断当前价格质量                       |
| 盘口        | `book_depth`               | 热门侧附近深度                      | ⭐⭐  | 判断价格是否容易被推动                    |
| 盘口        | `opposite_depth`           | 反方向盘口深度                      | ⭐   | 辅助判断价格稳定性                      |
| PM变化      | `pm_price_change_5s`       | PM 热门侧最近 5s 价格变化             | ⭐⭐  | PM 是否已经开始反映 spot               |
| PM变化      | `pm_price_change_10s`      | PM 最近 10s 价格变化               | ⭐⭐  | 与 spot 动量结合                    |
| PM变化      | `pm_spot_lag`              | PM 价格变化相对 spot 变化的滞后程度       | ⭐⭐⭐ | 你的策略本质上就是在捕捉这种错位               |
| 状态组合      | `dev / volume_ratio`       | dev 相对于当前成交活跃度               | ⭐⭐  | GBM 可自行学习，但显式提供有帮助             |
| 状态组合      | `walk / volume_ratio`      | walk 与市场活跃度关系                | ⭐⭐  | 判断 TWAP 位移是否异常                 |
| 状态组合      | `dev_sigma / volume_ratio` | 波动标准化后的 dev 与 volume 状态      | ⭐⭐  | 捕捉 regime                      |
| 状态组合      | `basis / sigma_usd`        | basis 的波动标准化                 | ⭐⭐⭐ | 统一不同波动环境                       |
| 事件历史      | `event_volume_percentile`  | 当前 volume 在历史事件中的 percentile | ⭐⭐  | 比绝对 BTC 数量更稳定                  |
| 事件历史      | `event_vol_percentile`     | 当前波动在历史事件中的位置                | ⭐⭐  | 判断当前属于低/正常/高波动 regime          |

### 如果让我明天直接开始做，我会分三批

**第一批：必须有**

```text
remaining_sec
stage
price
dev
walk
basis
sigma_usd
dev_sigma
walk_sigma

volume_5m
volume_1m
volume_30s
volume_10s
volume_ratio_1m
volume_ratio_30s

spot_return_5s
spot_return_10s
spot_return_30s
dev_change_5s
dev_change_10s
basis_change_5s

dev_above_63_duration
dev_shrink_duration

book_latency_ms
```

这批已经足够做第一版 GBM。

---

### 第二批：我认为很可能有价值

```text
pm_price_change_5s
pm_price_change_10s
pm_spot_lag

walk_change_5s
basis_change_5s

volume_accel
event_volume_percentile
event_vol_percentile
bid_ask_spread
```

---

### 第三批：让 GBM 自己探索

```text
dev / volume_ratio
walk / volume_ratio
dev_sigma / volume_ratio
basis / sigma_usd
```

这些可以作为 engineered features [人工派生特征]，然后看 feature importance / SHAP 是否真的有贡献。

---

## 还有一个我会特别坚持的原则

你的：

```text
walk_min_usd = 43
dev_min_usd = 63
sigma_min_usd = 40
listen_min_price = 0.83
```

**第一版不要删除。**

我会把它们看成：

> **Rule Gate [规则闸门] + GBM Probability Filter [概率过滤器]**

也就是：

```text
原始市场
   ↓
现有规则
   ↓
候选信号
   ↓
GBM
   ↓
P(win) ≥ 某阈值
   ↓
下单
```

这样你明天研究 GBM 时，实际上是在回答一个非常明确的问题：

> **“在我已经知道这个事件满足扫尾盘条件的情况下，还有哪些变量可以把 90% 左右的候选信号进一步分成高胜率和低胜率两类？”**

而不是让 GBM 从零重新发现你已经花大量时间验证过的 `43 / 63 / 40 / 0.83`。

另外，**label 仍然建议严格用最终 5 分钟 UP/DOWN 结果**，不要用中途价格变化当 label。这样才能和你现在真正想优化的“扫尾盘最终胜率”保持一致。
