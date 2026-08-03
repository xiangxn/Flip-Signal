"""
Feature: imbalance_trend — 订单簿失衡趋势（方向调整后）

衡量买卖盘口深度的相对变化趋势，判断支撑是否在增强或瓦解。

  raw = BidDepth / AskDepth  — 当前买卖深度比（>1 = 买方深度更厚）
  trend = raw_now / raw_30s_ago - 1  — 过去 30 秒的变化率

方向调整：
  - price > open（赌涨）：bid 相对 ask 在增厚 = 买盘支撑增强 → 正值
  - price < open（赌跌）：ask 相对 bid 在增厚 = 卖盘压力增强 → 正值

  adjusted = sign(price - open) × trend

正值 = 订单簿支撑当前方向，负值 = 订单簿背离（支撑在瓦解）。
"""

import numpy as np
import pandas as pd

from features.base import Feature

LOOKBACK = 6  # ticks (~30s at 5s tick interval)


class ImbalanceTrend(Feature):
    name = "imbalance_trend"
    description = (
        "订单簿失衡趋势(30s)：买卖深度比的方向调整变化率——"
        "正值 = 盘口支撑当前方向，负值 = 支撑在瓦解"
    )

    def compute(self, df: pd.DataFrame) -> pd.Series:
        results = []
        for _condition_id, group in df.groupby("condition_id"):
            bid = group["bid_depth"]
            ask = group["ask_depth"]

            # Raw imbalance ratio
            imbalance = bid / ask.replace(0, np.nan)

            # 30s change rate
            imbalance_prev = imbalance.shift(LOOKBACK)
            trend = imbalance / imbalance_prev.replace(0, np.nan) - 1.0

            # Direction-adjust
            distance = (group["price"] - group["open"]) / group["open"]
            direction = np.sign(distance).replace(0, 1)

            adjusted = trend * direction
            results.append(adjusted.fillna(0))

        if results:
            return pd.concat(results).reindex(df.index)
        return pd.Series(0.0, index=df.index)
