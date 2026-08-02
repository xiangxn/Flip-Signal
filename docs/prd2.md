# Polyman Feature Research Lab v1.0

## BTC 5分钟 Polymarket 扫尾盘特征研究系统设计文档

版本：v1.0
目标：验证 BTC 5分钟二元事件中，哪些实时市场状态特征能够提高尾盘下注胜率和期望收益。

---

# 1. 项目定位

## 1.1 不做什么

第一阶段**不做**：

* BTC价格预测模型
* 趋势预测
* MQS市场质量评分
* 自动交易机器人
* LightGBM模型

原因：

目前最大的问题不是预测能力，而是：

> 不知道哪些信息真正具有交易价值。

所以第一目标：

**建立一个特征验证实验室。**

---

## 1.2 做什么

系统回答：

> 在一个5分钟BTC事件中，当剩余X秒时，当前市场状态是否具有扫尾优势？

即：

输入：

```
当前状态
```

输出：

```
未来结算结果概率 / EV
```

---

# 2. 核心研究假设

我们的核心假设：

> BTC 5分钟K线内部存在生命周期规律。

每个5分钟事件：

```
开盘
 |
 |
中间变化
 |
 |
尾盘
 |
 |
结算
```

不同时间点：

市场状态不同。

因此研究单位：

不是：

* 日期
* 小时
* 月份

而是：

## Event Lifecycle

即：

每一个5分钟事件内部的状态变化。

---

# 3. 总体架构

```
              Binance
        aggTrade + depth20
                  |
                  |
                  v
          Go Data Collector
                  |
                  |
                  v
          Snapshot Generator
                  |
                  |
                  v
             Parquet Data
                  |
                  |
                  v
        Python Feature Lab
                  |
        +---------+---------+
        |                   |
        v                   v
 Feature Calculator     Analyzer
        |                   |
        +---------+---------+
                  |
                  v
            Research Report
```

---

# 4. 技术方案

## 4.1 Go负责

职责：

* 实时采集
* 数据清洗
* 时间同步
* Snapshot生成

原因：

* Binance websocket高性能
* 长时间运行稳定
* 数据采集不应该频繁修改

---

## 4.2 Python负责

职责：

* Feature计算
* 统计分析
* 可视化
* 报告生成

原因：

研究阶段迭代速度最快。

---

# 5. 数据层设计

---

# 5.1 原始数据

## Binance aggTrade

字段：

```go
type Trade struct {

    Timestamp int64

    Price float64

    Quantity float64

    IsBuyerMaker bool

}
```

用于：

* 主动买卖
* 成交量
* Flow

---

## Binance depth20

字段：

```go
type Depth struct {

    Timestamp int64


    BidPrice []float64

    BidSize []float64


    AskPrice []float64

    AskSize []float64

}
```

用于：

* 流动性
* 深度变化

---

## Polymarket

字段：

```go
type MarketPrice struct {

    Timestamp int64

    YesPrice float64

    NoPrice float64

}
```

---

# 5.2 Snapshot设计

每秒一条。

Snapshot只保存事实。

禁止加入任何Feature。

```go
type Snapshot struct {


    Timestamp int64


    // event

    EventID string

    RemainingSec int



    // BTC

    OpenPrice float64

    CurrentPrice float64



    // return

    Return1s float64



    // volume

    BuyVolume1s float64

    SellVolume1s float64



    // order flow

    SignedFlow1s float64



    // volatility

    Volatility10s float64

    Volatility30s float64



    // depth

    BidDepth float64

    AskDepth float64



    // polymarket

    YesPrice float64

    NoPrice float64

}
```

---

# 6. Event设计

研究单位：

一个5分钟BTC周期。

```go
type Event struct {

    EventID string


    StartTime int64


    OpenPrice float64


    ClosePrice float64


    Outcome int


    Snapshots []Snapshot

}
```

Outcome:

```
YES = 1

NO = 0
```

---

# 7. 第一批核心Feature设计

只研究7个。

不扩展。

---

# Feature 1

# DistanceToStrike

## 意义

当前价格距离开盘价多少。

公式：

```
Distance

=

(CurrentPrice - OpenPrice)
/
OpenPrice
```

方向统一：

下注YES：

正值。

下注NO：

负值取反。

得到：

```
DistanceInFavor
```

---

# Feature 2

# SafetyRatio

## 意义

当前优势相对于近期市场波动是否足够大。

公式：

```
SafetyRatio

=

DistanceInFavor

/

RecentVolatility
```

其中：

RecentVolatility：

推荐：

过去30秒：

```
std(return_1s)
```

或者：

```
sum(abs(return_1s))
```

解释：

如果：

```
Distance = 0.15%

Volatility = 0.03%
```

那么：

```
SafetyRatio = 5
```

---

# Feature 3

# ReversalCapacity

## 意义

剩余时间内，市场还有多少能力反转。

公式：

```
ReversalCapacity

=

RecentMaxMove

/

DistanceInFavor
```

例如：

当前优势：

0.15%

过去30秒最大移动：

0.05%

结果：

0.33

越低越安全。

---

# Feature 4

# VolatilityExpansion

## 意义

当前是否进入异常波动状态。

公式：

```
VolatilityExpansion

=

Volatility10s

/

Volatility60s
```

解释：

> 越大，尾盘风险越高。

---

# Feature 5

# DirectionPersistence

## 意义

当前方向是否持续。

过去N秒：

统计：

上涨/下跌比例。

例如：

过去20秒：

18秒上涨。

比例：

90%

---

# Feature 6

# SignedFlow

## 意义

成交是否支持当前方向。

公式：

```
SignedFlow

=

BuyVolume-SellVolume
```

标准化：

```
/

(BuyVolume+SellVolume)
```

---

# Feature 7

# VolumeAcceleration

## 意义

成交是否正在增强。

公式：

```
VolumeAcceleration

=

Volume(last10s)

/

Volume(previous10s)
```

---

# 8. Feature分析模块

每个Feature自动生成分析报告。

---

## 8.1 Bucket Analysis

例如：

SafetyRatio：

分10档。

输出：

| Bucket | 样本 | 胜率 | EV |
| ------ | -- | -- | -- |
| 0-0.5  |    |    |    |
| 0.5-1  |    |    |    |
| 1-2    |    |    |    |
| 2-3    |    |    |    |
| >3     |    |    |    |

---

## 8.2 Remaining Time Conditioning

必须按剩余时间分析。

例如：

SafetyRatio：

分别：

```
60秒

30秒

15秒

5秒
```

因为：

同一个指标：

不同剩余时间意义不同。

---

## 8.3 Monotonicity

计算：

Spearman Rank。

回答：

Feature增加：

胜率是否增加。

---

## 8.4 Feature Combination

支持二维分析。

例如：

```
SafetyRatio

+

VolatilityExpansion
```

输出：

二维胜率矩阵。

目的：

发现组合优势。

---

# 9. 输出报告

自动生成：

Markdown。

例如：

```
Feature:

SafetyRatio


Samples:

120000


Spearman:

0.73


Best Region:

Remaining 20-40s

SafetyRatio > 3


WinRate:

92%


EV:

+0.08
```

---

# 10. 开发顺序

## Phase 1：数据

时间：

1周以内

完成：

* Binance采集
* Snapshot生成
* Event切分

---

## Phase 2：Feature Lab

时间：

3-5天

完成：

* Feature接口
* Bucket分析
* 报告生成

---

## Phase 3：核心Feature验证

优先：

1. DistanceToStrike
2. SafetyRatio
3. ReversalCapacity
4. VolatilityExpansion
5. SignedFlow
6. VolumeAcceleration
7. DirectionPersistence

---

# 11. 后续路线

只有当：

这些Feature被证明有效。

才进入：

## 第二阶段

Feature组合。

例如：

```
SafetyRatio > 3

+

VolatilityExpansion < 1

+

Flow > 0
```

---

## 第三阶段

模型。

例如：

LightGBM。

目标：

不是预测BTC。

而是：

```
P(after fee profit)
```

输入：

7~20个经过验证的Feature。

---

# 最终原则

整个项目遵循：

```
数据
 ↓
事实Snapshot
 ↓
Feature
 ↓
统计验证
 ↓
组合
 ↓
模型
 ↓
交易
```

不要：

```
数据
 ↓
模型
 ↓
希望赚钱
```

---

这个版本我认为是目前讨论后最合理的收敛版：

* 足够简单，可以快速实现；
* 保留了未来扩展空间；
* 针对 Polymarket 5分钟二元事件，而不是普通BTC交易；
* 核心验证目标明确：**找到尾盘状态优势。**
