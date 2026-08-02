# Market Quality Specification v1.0

版本：v1.0
目标：

> 判断 BTC 5分钟 Polymarket 市场在当前时刻是否具有尾盘下注价值。

核心原则：

**MQS 不预测价格方向。**

它只回答：

> 当前市场状态是否足够健康，使得尾盘方向下注具有正期望。

---

# 1. 系统输入定义

## 1.1 数据周期

所有指标基于：

* 1秒 snapshot

原因：

Polymarket BTC 5分钟市场：

* 开盘后持续5分钟
* 尾盘决策窗口：

  * 剩余60秒
  * 剩余30秒
  * 剩余15秒

因此需要秒级粒度。

---

## 1.2 Snapshot基础字段

```go
type Snapshot struct {

    Timestamp int64

    // market
    MarketID string
    RemainingSec int


    // BTC
    OpenPrice float64
    Price float64


    // price path
    Return1s float64
    Return5s float64
    Return10s float64
    Return30s float64
    ReturnFromOpen float64


    // OHLC
    HighFromOpen float64
    LowFromOpen float64


    // volume
    BuyVolume1s float64
    SellVolume1s float64

    BuyVolume10s float64
    SellVolume10s float64


    // order flow
    SignedFlow10s float64


    // volatility
    Volatility10s float64
    Volatility30s float64


    // depth
    BidDepth5 float64
    AskDepth5 float64

    BidDepth10 float64
    AskDepth10 float64


    // polymarket
    YesPrice float64
    NoPrice float64
}
```

---

# 2. MQS整体结构

最终输出：

```go
type MarketQuality struct {

    TrendScore float64

    NoiseScore float64

    HealthScore float64

    FlowScore float64

    LiquidityScore float64


    Total float64

}
```

范围：

```
0-100
```

---

# 3. Trend Score（趋势质量）

权重：

```
30%
```

目的：

判断：

> 当前5分钟是否存在清晰方向。

---

## 3.1 Efficiency Ratio

窗口：

30秒

公式：

```
ER = |Price_now - Price_30s_ago|
     /
     Σ|每秒价格变化|
```

解释：

接近：

1

代表：

直线移动。

接近：

0

代表：

来回震荡。

评分：

```
ER >=0.7     100分

0.5-0.7      70分

0.3-0.5      40分

<0.3         0分
```

---

## 3.2 Trend R²

窗口：

30秒

线性回归：

```
price = a*time+b
```

得到：

R²

评分：

```
R² >0.8      100

0.6-0.8      70

0.4-0.6      40

<0.4         0
```

---

## 3.3 Direction Consistency

统计：

过去30秒：

每秒return方向。

例如：

```
+ + + + - + + +
```

正方向比例：

```
positive_count / total
```

评分：

```
>75%     100

60-75    70

50-60    40

<50      0
```

---

## TrendScore

```text
TrendScore =
ER*0.4
+
R2*0.35
+
Direction*0.25
```

---

# 4. Noise Score（市场噪声）

权重：

25%

注意：

这个分越高代表：

越乱。

最终计算时：

使用：

```
100 - NoiseScore
```

---

## 4.1 Direction Flip

窗口：

30秒

统计：

方向改变次数。

例如：

```
+ + - + - -
```

变化：

3次

评分：

```
0-2次     100

3-5       50

>5        0
```

---

## 4.2 Return Noise Ratio

计算：

```
Noise =
实际路径长度
/
净移动距离
```

例如：

价格：

100

→

101

但是中间：

99.5

100.8

99.7

101

路径：

4

净：

1

Noise=4

评分：

```
Noise <1.5       100

1.5-3            60

>3               0
```

---

## 4.3 Volatility Stability

判断：

是不是突然爆波。

比较：

```
Vol10s / Vol60s
```

评分：

```
0.8-1.5    100

1.5-2      50

>2         0
```

---

# NoiseScore

```
Flip*0.4
+
PathNoise*0.4
+
VolStable*0.2
```

---

# 5. Health Score（趋势健康）

权重：

20%

目的：

判断：

趋势有没有衰竭。

---

## 5.1 Momentum Decay

比较：

最近：

10秒动量

vs

之前：

10秒动量

例如：

```
前10秒:
+0.2%

后10秒:
+0.05%
```

衰减：

75%

评分：

```
衰减<30%    100

30-60       60

>60         0
```

---

## 5.2 Volume Support

价格上涨：

必须伴随：

成交增加。

计算：

```
Volume_last10
/
Volume_prev10
```

评分：

```
>1.2     100

0.8-1.2  60

<0.8     0
```

---

## 5.3 Retracement

趋势上涨：

计算：

```
(high-price)/(high-open)
```

评分：

```
<30%      100

30-60%    50

>60%      0
```

---

# HealthScore

```
Momentum*0.4
+
Volume*0.3
+
Retracement*0.3
```

---

# 6. Flow Score（订单流）

权重：

15%

---

## 6.1 Buy Ratio

```
BuyVolume /
(BuyVolume+SellVolume)
```

评分：

```
>65%    100

55-65   70

50-55   40

<50     0
```

---

## 6.2 Signed Flow Trend

过去：

30秒 SignedFlow

方向是否持续。

---

## FlowScore

```
BuyRatio*0.5
+
FlowTrend*0.5
```

---

# 7. Liquidity Score（流动性）

权重：

10%

---

## 7.1 Depth Imbalance

公式：

```
Bid/(Bid+Ask)
```

---

## 7.2 Depth Stability

过去10秒：

Depth变化。

如果：

突然减少：

扣分。

---

## 7.3 Spread

Polymarket:

YES/NO盘口。

---

# LiquidityScore

```
Imbalance*0.4
+
Stability*0.4
+
Spread*0.2
```

---

# 8. MQS最终公式

```
MQS =

TrendScore
*0.30

+

(100-NoiseScore)
*0.25

+

HealthScore
*0.20

+

FlowScore
*0.15

+

LiquidityScore
*0.10

```

---

# 9. 交易准入规则 v1.0

## 禁止交易

任何：

```
RemainingSec <10

OR

LiquidityScore <40

OR

NoiseScore >70
```

直接拒绝。

---

## 标准交易

满足：

```
RemainingSec:

10-60


MQS:

>=75
```

允许。

---

## 强信号

```
MQS >=85

TrendScore>=80

NoiseScore<=30

HealthScore>=70
```

增加仓位。

---

# 10. 记录格式

每次决策必须保存：

```json
{
"time":"",

"remaining":32,


"MQS":86,


"trend":91,

"noise":18,

"health":79,

"flow":82,

"liquidity":75,


"btc_return":"0.18%",


"decision":"BUY_YES",

"result":"WIN"
}
```

---

# 11. 后续优化路线

v1.0 不使用机器学习。

数据量达到：

```
10000+ events
```

以后：

训练：

LightGBM

输入：

全部feature。

目标：

不是：

涨跌。

目标：

```
P(profit > 0)
```

即：

> 当前状态是否值得下注。