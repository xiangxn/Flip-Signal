"""
Feature: cumulative_buy_pct — 累积买方占比（方向调整后）

从开盘到当前时刻，累积主动买入量占总成交量的比例，按价格方向调整。

  - price > open:  值 = 累积买方占比（越高 = 买盘主导，支持涨）
  - price < open:  值 = 累积卖方占比（越高 = 卖盘主导，支持跌）

范围 [0, 1]，0.5 = 买卖平衡。
> 0.5 表示累积订单流支持当前价格方向。
< 0.5 表示累积订单流与价格方向背离。

相比 signed_flow 的 1 秒粒度，这个累积值非常稳定——
不会因为单笔大单剧烈跳动，反映的是整个事件窗口的订单流倾向。
"""

import numpy as np
import pandas as pd

from features.base import Feature


class CumulativeBuyPct(Feature):
    name = "cumulative_buy_pct"
    description = (
        "从开盘累积的买卖占比（方向调整）：>0.5 = 累计订单流支持当前方向，"
        "<0.5 = 累计订单流与价格方向背离"
    )

    def compute(self, df: pd.DataFrame) -> pd.Series:
        results = []
        for _condition_id, group in df.groupby("condition_id"):
            buy_vol = group["buy_vol_5s"]
            sell_vol = group["sell_vol_5s"]

            cum_buy = buy_vol.cumsum()
            cum_sell = sell_vol.cumsum()
            total = cum_buy + cum_sell

            # Raw buy percentage
            buy_pct = cum_buy / total.replace(0, np.nan)

            # Direction-adjust: if price > open, keep buy_pct; if < open, use sell_pct
            distance = (group["price"] - group["open"]) / group["open"]
            direction = np.where(distance > 0, 1, -1)
            direction[distance == 0] = 0

            # When direction=1 (up): keep buy_pct
            # When direction=-1 (down): use 1-buy_pct = sell_pct
            adjusted = np.where(
                direction > 0,
                buy_pct,
                np.where(direction < 0, 1.0 - buy_pct, 0.5)
            )
            adjusted = pd.Series(adjusted, index=group.index).fillna(0.5)
            results.append(adjusted)

        if results:
            return pd.concat(results).reindex(df.index)
        return pd.Series(0.5, index=df.index)
