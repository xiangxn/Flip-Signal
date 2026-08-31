#!/usr/bin/env python3
"""
v2 数据公共库 —— 1s 微观数据（data/btc, 格式见 docs/recollection_plan_2026-08-16.md §3.2）。

提供:
  load_events()     加载 JSONL + settlement_correction 合并（官方口径覆盖流值口径）
  extract_cross()   事件内首个 0.7 上升沿穿越观测（1s ticks, bid 口径）
  cross_window()    穿越窗口内的微观特征提取（PM tape / Binance OFI / 深度 / reprice）

口径与 v1 保持一致:
  * 穿越判定: pm.yes_bid / pm.no_bid > 0.7（best bid, 与旧 lab 同口径）
  * 成交:     确认时刻对侧 ask = 1 - 对侧 bid（双 token 互补, 与实盘 FAK 一致）
  * 窗口:     rem ∈ (15, 260), 穿越前至少 5 个 tick
  * 类别标签: only/both 为整窗（未来信息）信息, 仅作训练目标, 绝不进特征
"""

import glob
import json
from pathlib import Path

TRIGGER = 0.7
MIN_PRE_TICKS = 5
REMAIN_MAX = 260
REMAIN_MIN = 15


def load_events(data_dir: str) -> list[dict]:
    """加载 JSONL 事件，按 start_time 排序；修正行覆盖合并（同 flip/follow 的 load_events）。"""
    events = []
    corrections = {}
    for path in sorted(Path(data_dir).glob("events_*.jsonl")):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get("event_type") == "settlement_correction":
                    corrections[rec["start_time"]] = rec
                else:
                    events.append(rec)
    for e in events:
        corr = corrections.get(e["start_time"])
        if corr:
            e.update({k: v for k, v in corr.items() if k != "event_type"})
    events.sort(key=lambda e: e["start_time"])
    return events


def _tick_bid(t, side: str) -> float:
    """tick 上 side 侧 best bid（缺字段返回 0）。"""
    pm = t.get("pm") or {}
    return pm.get(f"{side}_bid") or 0.0


def extract_cross(events: list[dict], outcome_field: str = "outcome",
                  confirm_s: tuple = (2, 10)):
    """事件内时间顺序首个 0.7 上升沿穿越 → 观测 dict。

    返回每条观测:
      event_start / side / cls(only|both) / rem / i (tick 索引)
      trigger_bid           穿越时刻穿越侧 bid
      fill{2,10}s           follow 口径成交价 = 1 - 对侧 bid（确认 +2s/+10s）
      flip_fill{2,10}s      flip 口径成交价 = 1 - 穿越侧 bid（确认 +2s/+10s）
      other_delta{2,10}s    确认期对侧 bid 变化
      won                   follow（买穿越侧）是否赢
      ticks                 (i, 确认后索引) 供下游窗口特征
      trades                事件 trades 数组（按 token 分桶）
    """
    obs = []
    for event in events:
        outcome = event.get(outcome_field)
        if outcome is None:
            continue
        ticks = event.get("ticks") or []
        if len(ticks) < MIN_PRE_TICKS + 5:
            continue
        state = {"yes": False, "no": False}
        crossed = set()
        first = None
        for i, t in enumerate(ticks):
            rem = t.get("rem")
            if rem is None:
                continue
            if not (REMAIN_MIN < rem < REMAIN_MAX):
                continue
            for side in ("yes", "no"):
                above = _tick_bid(t, side) > TRIGGER
                if above and not state[side]:
                    crossed.add(side)
                    if first is None:
                        first = (i, side)
                state[side] = above

        if first is None:
            continue
        i, side = first
        other = "no" if side == "yes" else "yes"
        cls = "both" if len(crossed) == 2 else "only"
        won = (outcome == 0) if side == "yes" else (outcome == 1)

        o = {
            "event_start": event["start_time"],
            "side": side,
            "other": other,
            "cls": cls,
            "rem": ticks[i]["rem"],
            "i": i,
            "trigger_bid": _tick_bid(ticks[i], side),
            "won": won,
            "trades": event.get("trades") or [],
        }
        for ds in confirm_s:
            j = min(i + ds, len(ticks) - 1)
            t = ticks[j]
            this_bid = _tick_bid(t, side)
            oth_bid = _tick_bid(t, other)
            o[f"fill{ds}s"] = 1.0 - oth_bid if oth_bid else None
            o[f"flip_fill{ds}s"] = 1.0 - this_bid if this_bid else None
            o[f"other_delta{ds}s"] = (oth_bid - _tick_bid(ticks[i], other)) if oth_bid else None
        obs.append(o)
    return obs


# ────────────────────────────────────────────────────────────────
# 穿越窗口微观特征（全部 ≤ 确认时刻已知）
# ────────────────────────────────────────────────────────────────

def cross_window(event: dict, i: int, side: str, other: str,
                 pre_sec: int = 10, post_sec: int = 10,
                 open_price: float | None = None, hist: float | None = None) -> dict:
    """穿越前后窗口特征（tick 索引 i 为穿越 tick）。

    pre_sec/post_sec 单位为秒（1s ticks 直接按索引换算）。
    open_price/hist 用于 v1 对照特征（spot_pos/path_eff/twap_pos）。
    """
    ticks = event.get("ticks") or []
    n = len(ticks)
    i0 = max(0, i - pre_sec)
    i1 = min(n, i + post_sec + 1)

    feats: dict = {}

    # ── Binance 1s 成交流（穿越前窗口）──
    bin_pre = [t.get("bin") or {} for t in ticks[i0:i]]
    bv = sum(x.get("buy_vol") or 0 for x in bin_pre)
    sv = sum(x.get("sell_vol") or 0 for x in bin_pre)
    feats["of_pre"] = (bv - sv) if (bv + sv) > 1e-12 else 0.0          # 主动净买(手)
    feats["flow_pre"] = (bv - sv) / (bv + sv) if (bv + sv) > 1e-12 else 0.0
    feats["vol_pre"] = bv + sv
    ticks_pre = sum(x.get("ticks") or 0 for x in bin_pre)
    feats["n_ticks_pre"] = ticks_pre

    # ── PM 盘口（穿越 tick）──
    pm = ticks[i].get("pm") or {}
    yes_dep = (pm.get("yes_bid_top5") or 0) + (pm.get("yes_ask_top5") or 0)
    no_dep = (pm.get("no_bid_top5") or 0) + (pm.get("no_ask_top5") or 0)
    if side == "yes":
        feats["cross_bid_dep"] = (pm.get("yes_bid_top5") or 0) / yes_dep if yes_dep else None
        feats["cross_depth"] = yes_dep
    else:
        feats["cross_bid_dep"] = (pm.get("no_bid_top5") or 0) / no_dep if no_dep else None
        feats["cross_depth"] = no_dep
    feats["book_age"] = (ticks[i]["ts"] - (pm.get("book_ts") or ticks[i]["ts"])) if pm else 0
    feats["latency"] = pm.get("book_latency_ms") or 0

    # ── 盘口 reprice 速度（穿越后窗口内 distinct book_ts 更新数）──
    book_ts_set = set()
    for t in ticks[i:i1]:
        p = t.get("pm") or {}
        if p.get("book_ts"):
            book_ts_set.add(p["book_ts"])
    feats["reprice_n"] = len(book_ts_set)
    gap = 0.0
    if i + 1 < len(ticks):
        p0, p1 = ticks[i].get("pm") or {}, ticks[i + 1].get("pm") or {}
        if p0.get("book_ts") and p1.get("book_ts"):
            gap = p1["book_ts"] - p0["book_ts"]
    feats["reprice_gap"] = gap

    # ── PM tape（trades 聚合, 穿越后窗口; trades[].ts 为毫秒桶起始）──
    trades = event.get("trades") or []
    tb = [x for x in trades if x.get("token") == side.upper()]
    to = [x for x in trades if x.get("token") == other.upper()]
    t0 = ticks[i]["ts"] // 1000
    t1 = (ticks[min(i + post_sec, n - 1)]["ts"] // 1000) + 1
    s_buy = sum(x.get("buy_size") or 0 for x in tb if t0 <= (x.get("ts") or 0) // 1000 < t1)
    s_sell = sum(x.get("sell_size") or 0 for x in tb if t0 <= (x.get("ts") or 0) // 1000 < t1)
    o_buy = sum(x.get("buy_size") or 0 for x in to if t0 <= (x.get("ts") or 0) // 1000 < t1)
    o_sell = sum(x.get("sell_size") or 0 for x in to if t0 <= (x.get("ts") or 0) // 1000 < t1)
    tot = s_buy + s_sell + o_buy + o_sell
    feats["tape_net"] = (s_buy - s_sell) if (s_buy + s_sell) > 0 else None
    feats["tape_imb"] = (s_buy - s_sell) / (s_buy + s_sell) if (s_buy + s_sell) > 0 else None
    feats["tape_vol"] = s_buy + s_sell
    feats["tape_other_imb"] = (o_buy - o_sell) / (o_buy + o_sell) if (o_buy + o_sell) > 0 else None
    feats["tape_share"] = (s_buy + s_sell) / tot if tot > 0 else None
    max_sz = max((x.get("max_size") or 0) for x in tb if t0 <= (x.get("ts") or 0) // 1000 < t1) if tb else 0
    feats["tape_max"] = max_sz / (s_buy + s_sell) if (s_buy + s_sell) > 0 else None
    feats["tape_net_other"] = (o_buy - o_sell) if (o_buy + o_sell) > 0 else None

    # ── 穿越后 10s 的价格轨迹（微观 zigzag: 穿越后是否立即回撤）──
    post_bids = [_tick_bid(t, side) for t in ticks[i:i + post_sec + 1]]
    feats["post_dip"] = (max(post_bids) - min(post_bids)) if post_bids else None
    feats["post_end"] = post_bids[-1] if post_bids else None

    # ── TWAP 新鲜度 ──
    tw = ticks[i].get("twap") or {}
    feats["twap_age"] = tw.get("age_ms")

    # ── v1 对照特征（穿越前现货路径 + TWAP 位置）──
    sgn = 1 if side == "yes" else -1
    prices = [((t.get("bin") or {}).get("price") or 0.0) for t in ticks[:i + 1]]
    spot = prices[-1] if prices else 0.0
    amp = max(prices) - min(prices) if prices else 0.0
    net = abs(spot - open_price) if open_price else 0.0
    path = sum(abs(prices[k] - prices[k - 1]) for k in range(1, len(prices)))
    flips = sum(1 for k in range(2, len(prices))
                if (prices[k] - prices[k - 1]) * (prices[k - 1] - prices[k - 2]) < 0)
    feats["path_eff"] = net / amp if amp > 1e-9 else None
    feats["noise"] = path / net if net > 1e-9 else None
    feats["flips"] = flips
    if open_price and hist:
        feats["spot_pos"] = sgn * (spot - open_price) / hist
    tw = ticks[i].get("twap") or {}
    twap = tw.get("price")
    twap_open = event.get("twap_open_price")
    if twap and twap_open and hist:
        feats["twap_pos"] = sgn * (twap - twap_open) / hist
        if spot:
            feats["twap_gap"] = sgn * (spot - twap) / hist
    if twap and hist:
        conf_tw = (ticks[min(i + post_sec, n - 1)].get("twap") or {}).get("price")
        if conf_tw:
            feats["twap_delta"] = sgn * (conf_tw - twap) / hist
    return feats
