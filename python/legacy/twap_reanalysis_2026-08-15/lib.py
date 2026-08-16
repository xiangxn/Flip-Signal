#!/usr/bin/env python3
"""
TWAP 真实结算下的翻转特征重新挖掘 —— 数据层与特征提取纯函数（2026-08-15）。

背景:
  data_0/lab_resolved（2026-08-05 ~ 08-12，1719 事件）的 snapshots 全部是
  Binance 现货/量/深度 + PM 订单簿数据；market_outcome 是 PM 市场真实结算
  （btc-updown-5m 以 Chainlink TWAP-60 判定胜负）。旧 Formula B 特征在
  TWAP 结算下被证伪（WR 17.2%），本模块从零开始重新挖掘。

铁律 —— 无未来数据:
  1. 特征只允许使用 ≤ 穿越 tick 的快照数据（含滚动历史，只向后看）
  2. 历史振幅基准只使用**之前已结束窗口**的事件（expanding window）
  3. 结算只使用 market_outcome（事件级，TWAP 官方口径）
  4. close_price / outcome / 当前事件后续 snapshots 禁止进入特征

符号约定:
  所有方向性特征"正 = 对翻转有利"（翻转 = 穿越侧最终输掉）。
  YES 穿越时翻转 = DOWN 赢，sgn = -1；NO 穿越时翻转 = UP 赢，sgn = +1。
  EV 结构: 买入对侧（穿越侧的互补 token），成交价 fill = 对侧 ask
  = 1 - 触发侧 bid。EV/股 = 翻转率 - fill。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

TRIGGER = 0.7            # 穿越阈值
MAX_REMAINING = 260      # 有效窗口上界（秒，与旧引擎一致）
MIN_REMAINING = 15       # 有效窗口下界（秒，尾部无法成交）
MIN_PRE_SNAPS = 5        # 穿越前最少快照数（预热）
HIST_WINDOW = 18         # 历史振幅回看窗口数
HIST_MIN = 3             # 历史振幅最少需要的窗口数
CONFIRM_TICKS = 2        # 确认延迟 tick 数（5s/tick，模拟实盘确认+下单延迟）


# ═══════════════════════════════════════════════════════════════
# 数据加载
# ═══════════════════════════════════════════════════════════════

def load_events(data_dir: str) -> list[dict]:
    """加载 lab_resolved 全部事件，按 start_time 排序，注入因果历史振幅。"""
    events = []
    for path in sorted(Path(data_dir).glob("events_*.jsonl")):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
    events.sort(key=lambda e: e["start_time"])
    _inject_hist_range(events)
    return events


def _inject_hist_range(events: list[dict]) -> None:
    """因果历史振幅: 每个事件的 hist_range = 前 N 个**已完成窗口**的平均
    |close-open|（Binance 口径，与快照 price 同源）。

    事件按时间排序后向前看，绝对不使用当前或未来事件的数据。
    """
    for i, e in enumerate(events):
        prev = []
        for j in range(max(0, i - HIST_WINDOW), i):
            o, c = events[j]["open_price"], events[j]["close_price"]
            prev.append(abs(c - o))
        e["hist_range"] = (sum(prev) / len(prev)) if len(prev) >= HIST_MIN else None


# ═══════════════════════════════════════════════════════════════
# 穿越观测
# ═══════════════════════════════════════════════════════════════

@dataclass
class Crossing:
    """一次上升沿穿越 0.7 的观测：全部因果特征 + TWAP 结算标签。"""
    # 标识
    event_start: int
    condition_id: str
    side: str                 # "yes" / "no"（穿越侧）
    cross_idx: int            # 快照索引
    flip: bool                # 穿越侧在 TWAP 结算下输掉（= 最终翻转）

    # ── PM 订单簿特征（≤ 穿越 tick）──
    trigger_bid: float        # 触发侧 best bid（穿越价）
    other_bid: float          # 对侧 best bid
    spread: float             # 隐含价差 = trigger + other - 1
    pm_vel: float             # 触发侧 3 tick 升速（穿越前抬升速度）
    cross_count_same: int     # 该侧第几次穿越
    other_crossed_before: bool  # 对侧此前是否已穿越过 0.7
    total_crosses_before: int # 两侧历史穿越总数
    ob_latency_ms: float | None  # PM 订单簿延迟（部分天缺失）

    # ── Binance 现货特征（≤ 穿越 tick，正=对翻转有利）──
    spot_pos: float | None    # 方向校正的现货位置 (spot-open)/hist
    range_exp: float | None   # |spot-open|/hist 振幅扩张
    twap60_pos: float | None  # 方向校正的 60s 滚动均值位置（≈已实现的 TWAP 偏置）
    gap60: float | None       # 方向校正的 spot 与其 60s 均线的缺口
    ret_60s: float | None     # 方向校正的 60s 动量
    ret_30s: float | None     # 方向校正的 30s 动量
    ret_10s: float | None     # 方向校正的 10s 动量
    flow_5s: float | None     # 方向校正的 5s 主动流
    flow_cum30: float | None  # 方向校正的 30s 累计主动流
    flow_cum60: float | None  # 方向校正的 60s 累计主动流
    depth_imb: float | None   # 方向校正的盘口失衡 (bid-ask)/(bid+ask)
    vol_10s: float | None     # 10s 波动率（方向无关）
    vol_30s: float | None     # 30s 波动率（方向无关）

    # ── 时间 ──
    remaining_sec: int        # 穿越时窗口剩余秒数
    hour_utc: int

    # ── 成交价（对侧 ask = 1 - 触发侧 bid）──
    fill0: float              # 穿越 tick 立即成交
    fill2: float | None       # +2 ticks 确认后成交（None=窗口末尾无法确认）


def _side_sign(side: str) -> int:
    """方向校正因子: YES 穿越时翻转=DOWN 赢（sgn=-1），NO 反之。"""
    return -1 if side == "yes" else 1


def _div(x: float | None, hist: float | None) -> float | None:
    if x is None or not hist:
        return None
    return x / hist


def collect_crossings(events: list[dict]) -> list[Crossing]:
    """收集全部事件的因果穿越观测。

    与实盘可执行口径对齐:
      * 仅有效窗口（15 < remaining ≤ 260）且预热足够（i ≥ MIN_PRE_SNAPS）
      * 穿越定义: 该侧 bid 从 ≤0.7 变为 >0.7 的上升沿
      * 特征只用 ≤ i 的快照；fill2 需要 i+2 快照真实存在
    """
    out: list[Crossing] = []
    for e in events:
        if e.get("market_outcome") is None:
            continue
        hist = e.get("hist_range")
        snaps = e["snapshots"]
        n = len(snaps)

        # 预计算两侧的穿越历史（用于 cross_count / other_crossed_before）
        yes_crossed_until = [0] * n
        no_crossed_until = [0] * n
        yc = nc = 0
        for i in range(1, n):
            if (snaps[i]["yes_price"] > TRIGGER and snaps[i - 1]["yes_price"] <= TRIGGER):
                yc += 1
            if (snaps[i]["no_price"] > TRIGGER and snaps[i - 1]["no_price"] <= TRIGGER):
                nc += 1
            yes_crossed_until[i] = yc
            no_crossed_until[i] = nc

        for side in ("yes", "no"):
            sgn = _side_sign(side)
            this_key = "yes_price" if side == "yes" else "no_price"
            other_key = "no_price" if side == "yes" else "yes_price"

            for i in range(1, n):
                s = snaps[i]
                # 上升沿 + 有效窗口 + 预热
                if not (snaps[i - 1][this_key] <= TRIGGER < s[this_key]):
                    continue
                if not (MIN_REMAINING < s["remaining_sec"] <= MAX_REMAINING):
                    continue
                if i < MIN_PRE_SNAPS:
                    continue

                spot = s.get("price") or 0.0
                o = e["open_price"]

                # ── PM 特征 ──
                prev3 = snaps[max(0, i - 3)][this_key]
                pm_vel = s[this_key] - prev3
                ob_lat = s.get("order_book_latency")

                # ── BTC 特征（全部 ≤ i 已知）──
                spot_pos = _div(sgn * (spot - o), hist) if spot and o else None
                range_exp = _div(abs(spot - o), hist) if spot and o else None

                twap60_pos = gap60 = ret_60s = flow_cum60 = None
                if i >= 11 and spot:
                    roll60 = sum(snaps[k]["price"] for k in range(i - 11, i + 1)) / 12
                    twap60_pos = _div(sgn * (roll60 - o), hist) if o else None
                    gap60 = _div(sgn * (spot - roll60), hist)
                    p_prev = snaps[i - 12]["price"]
                    if p_prev:
                        ret_60s = sgn * (spot / p_prev - 1.0)
                    flow_cum60 = sgn * sum(
                        snaps[k].get("signed_flow_5s") or 0.0
                        for k in range(i - 11, i + 1))

                ret_30s = flow_cum30 = None
                if i >= 5 and spot:
                    p_prev = snaps[i - 6]["price"]
                    if p_prev:
                        ret_30s = sgn * (spot / p_prev - 1.0)
                    flow_cum30 = sgn * sum(
                        snaps[k].get("signed_flow_5s") or 0.0
                        for k in range(i - 5, i + 1))

                ret_10s = None
                if s.get("ret_10s") is not None:
                    ret_10s = sgn * s["ret_10s"]

                flow_5s = None
                if s.get("signed_flow_5s") is not None:
                    flow_5s = sgn * s["signed_flow_5s"]

                depth_imb = None
                bd, ad = s.get("bid_depth"), s.get("ask_depth")
                if bd is not None and ad is not None and (bd + ad) > 0:
                    depth_imb = sgn * (bd - ad) / (bd + ad)

                vol_10s, vol_30s = s.get("vol_10s"), s.get("vol_30s")

                # ── 成交价 ──
                fill0 = 1.0 - s[this_key]
                fill2 = None
                if i + CONFIRM_TICKS < n:
                    conf = snaps[i + CONFIRM_TICKS]
                    if conf["remaining_sec"] > 0:  # 市场未结束才可成交
                        fill2 = 1.0 - conf[this_key]

                # ── 结算标签 ──
                outcome = e["market_outcome"]  # 0=Up 赢, 1=Down 赢
                flip = (outcome == 1) if side == "yes" else (outcome == 0)

                out.append(Crossing(
                    event_start=e["start_time"],
                    condition_id=e["condition_id"],
                    side=side, cross_idx=i, flip=flip,
                    trigger_bid=s[this_key], other_bid=s[other_key],
                    spread=s[this_key] + s[other_key] - 1.0,
                    pm_vel=pm_vel,
                    cross_count_same=(yes_crossed_until[i] if side == "yes"
                                      else no_crossed_until[i]),
                    other_crossed_before=(no_crossed_until[i] > 0 if side == "yes"
                                          else yes_crossed_until[i] > 0),
                    total_crosses_before=yes_crossed_until[i] + no_crossed_until[i],
                    ob_latency_ms=ob_lat,
                    spot_pos=spot_pos, range_exp=range_exp,
                    twap60_pos=twap60_pos, gap60=gap60,
                    ret_60s=ret_60s, ret_30s=ret_30s, ret_10s=ret_10s,
                    flow_5s=flow_5s, flow_cum30=flow_cum30, flow_cum60=flow_cum60,
                    depth_imb=depth_imb, vol_10s=vol_10s, vol_30s=vol_30s,
                    remaining_sec=s["remaining_sec"], hour_utc=(e["start_time"] // 3600) % 24,
                    fill0=fill0, fill2=fill2,
                ))
    return out


def first_crossing_per_event(xs: list[Crossing]) -> list[Crossing]:
    """每个事件保留最早的一个穿越（实盘每事件最多一注）。"""
    first: dict[int, Crossing] = {}
    for x in xs:
        if x.event_start not in first or x.cross_idx < first[x.event_start].cross_idx:
            first[x.event_start] = x
    return list(first.values())


# ═══════════════════════════════════════════════════════════════
# 统计工具
# ═══════════════════════════════════════════════════════════════

def stats(xs: list[Crossing], fill_attr: str = "fill0") -> dict:
    """一组观测的翻转率 / 成交价 / EV 汇总。"""
    n = len(xs)
    if n == 0:
        return {"n": 0}
    flips = sum(1 for x in xs if x.flip)
    fills = [getattr(x, fill_attr) for x in xs if getattr(x, fill_attr) is not None]
    mf = sum(fills) / len(fills) if fills else None
    ev = (flips - sum(fills)) / n if fills else None
    return {"n": n, "flip_rate": flips / n, "mean_fill": mf, "ev": ev, "flips": flips}


def ev_per_bet(x: Crossing, fill_attr: str = "fill0") -> float | None:
    """单笔 EV = 翻转率贡献 (1-fill) - 未翻转损失 fill。"""
    f = getattr(x, fill_attr)
    if f is None:
        return None
    return (1.0 - f) if x.flip else -f


def day_of(x: Crossing) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(x.event_start, tz=timezone.utc).strftime("%m-%d")


def split_by_days(xs: list[Crossing]) -> dict[str, list[Crossing]]:
    days: dict[str, list[Crossing]] = {}
    for x in xs:
        days.setdefault(day_of(x), []).append(x)
    return days
