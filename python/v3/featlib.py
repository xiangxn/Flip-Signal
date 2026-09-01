#!/usr/bin/env python3
"""
v3 特征库 —— 从零重建模（单一目标: flip WR ≥ 35% 且 EV = WR - fill > 0）。

数据: data/btc v2 格式（1s ticks + trades 聚合）, 3753 事件。
观测: 事件内首个 0.7 上升沿穿越（1s 分辨率）。
决策时刻: A = 穿越 tick（仅允许穿越前信息, fill = 对侧 ask@穿越）;
          B = 穿越 +10s（v2 口径, fill = 1 - 触发侧 bid@+10s）。
标签:   flip_won = 穿越侧输掉（买对侧赢）。

铁律: 特征 ≤ 决策时刻; outcome/twap_close/类别(only/both) 只作标签/解剖, 绝不进特征。

特征族（v2 未覆盖的新字段已标 ★）:
  ★ Binance 盘口失衡   bin.bid5/ask5/bid10/ask10 → obi5/obi10/变化/均值（方向校正）
  ★ 成交价质量         trades.vwap/last_price/best_bid/best_ask → 成交在盘口中的位置
  ★ 长程 regime       穿越前全历史 spot 斜率/R²/加速度/波动率/回撤/位置
  ★ 基差轨迹          spot-twap 最近 60s 轨迹形状（均值/斜率/极值/符号变化）
  ★ PM 双侧深度        yes/no top5 深度不对称 + 对侧挂单结构
  v2 旧族              tape/OFI/深度/reprice/path_eff/noise/flips/twap_*/other_delta
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from v2.lib import load_events, extract_cross, cross_window

REPO = Path(__file__).resolve().parent.parent.parent  # python/ 的上层 = 仓库根
DATA = str(REPO / "data" / "btc")
TRIGGER = 0.7


# ────────────────────────────────────────────────────────────────
# 新增特征族
# ────────────────────────────────────────────────────────────────

def bin_book_feats(pm_bin: dict, sgn: int) -> dict:
    """Binance 盘口失衡（★ bid5/ask5/bid10/ask10 首次建模）。
    sgn = +1 表示"该方向对 flip 有利"（NO 穿越 +1, YES 穿越 -1, 因 YES 穿越 flip=买 Down）。"""
    b5, a5 = pm_bin.get("bid5") or 0, pm_bin.get("ask5") or 0
    b10, a10 = pm_bin.get("bid10") or 0, pm_bin.get("ask10") or 0
    out = {}
    out["obi5"] = (b5 - a5) / (b5 + a5) if (b5 + a5) > 1e-9 else None
    out["obi10"] = (b10 - a10) / (b10 + a10) if (b10 + a10) > 1e-9 else None
    out["dratio5"] = b5 / a5 if a5 > 1e-9 else None
    # 方向校正: 正值 = 盘口偏 flip 有利方向
    for k in ("obi5", "obi10", "dratio5"):
        if out[k] is not None:
            out[f"f_{k}"] = sgn * out[k]
    return out


def trades_quality(trades: list, token: str, t0_ms: int, t1_ms: int) -> dict:
    """成交价质量（★ vwap/last_price/best_bid/best_ask 首次建模）。
    price_pos ∈ [0,1]: 0=成交在 bid(被动卖/接单), 1=成交在 ask(主动买/吃单)。
    对穿越侧: price_pos 高 = 主动买推动 → flip 不利方向。"""
    rows = [x for x in trades if x.get("token") == token and t0_ms <= (x.get("ts") or 0) < t1_ms]
    if not rows:
        return {}
    pos = []
    aggr_buy, aggr_sell = 0.0, 0.0
    for r in rows:
        b, a = r.get("best_bid") or 0, r.get("best_ask") or 0
        v = r.get("vwap") or 0
        sp = a - b
        if sp > 1e-6:
            pos.append(np.clip((v - b) / sp, 0.0, 1.0))
        if r.get("buy_size") or 0:
            # 以 best_ask 成交才算主动买（size 分布未知, 用 vwap 位置近似）
            if sp > 1e-6 and (v - b) / sp > 0.5:
                aggr_buy += r.get("buy_size") or 0
            else:
                aggr_sell += r.get("buy_size") or 0
        if r.get("sell_size") or 0:
            if sp > 1e-6 and (v - b) / sp < 0.5:
                aggr_sell += r.get("sell_size") or 0
            else:
                aggr_buy += r.get("sell_size") or 0
    out = {}
    if pos:
        out["pp_mean"] = float(np.mean(pos))
        out["pp_std"] = float(np.std(pos))
    tot = aggr_buy + aggr_sell
    if tot > 0:
        out["aggr_buy_share"] = aggr_buy / tot  # 主动买占比（含大小单近似）
    return out


def regime_feats(ticks: list, i: int, open_price: float, sgn: int) -> dict:
    """长程 regime（★ 穿越前全历史, ≤ 穿越时刻已知）。
    方向校正: 值越大越偏 flip 有利方向。"""
    prices = [((t.get("bin") or {}).get("price") or 0.0) for t in ticks[:i + 1]]
    prices = [p for p in prices if p > 0]
    out = {}
    n = len(prices)
    if n < 10:
        return out
    p = np.asarray(prices, dtype=float)
    x = np.arange(n)
    slope, intercept = np.polyfit(x, p, 1)
    resid = p - (slope * x + intercept)
    ss_tot = float(np.sum((p - p.mean()) ** 2))
    r2 = 1.0 - float(np.sum(resid ** 2)) / ss_tot if ss_tot > 1e-12 else 0.0
    out["slope_bps100"] = slope / p[-1] * 1e4 * 100.0          # 每100s 的 bps
    out["trend_r2"] = r2
    out["f_slope"] = sgn * out["slope_bps100"]
    half = n // 2
    s1, _ = np.polyfit(x[:half], p[:half], 1)
    s2, _ = np.polyfit(x[half:], p[half:], 1)
    out["accel"] = (s2 - s1) / p[-1] * 1e4 * 100.0
    out["f_accel"] = sgn * out["accel"]
    rets = np.diff(p) / p[:-1]
    out["vol_1s"] = float(np.std(rets)) * 1e4                      # 1s 收益 std（bps）
    out["range_bps"] = (float(p.max()) - float(p.min())) / p[-1] * 1e4
    out["drawdown"] = float((p.max() - p[-1]) / p.max()) * 1e4      # 距高点回撤 bps
    out["runup"] = float((p[-1] - p.min()) / p.min()) * 1e4
    out["ret_open_bps"] = (p[-1] - open_price) / open_price * 1e4 if open_price else None
    out["f_ret_open"] = sgn * out["ret_open_bps"] if out["ret_open_bps"] is not None else None
    return out


def basis_feats(ticks: list, i: int, sgn: int) -> dict:
    """spot-twap 基差轨迹（★ 最近 60s 形状, ≤ 穿越时刻已知）。
    基差正 = spot 高于 twap（spot 已领先）。"""
    vals = []
    for t in ticks[max(0, i - 59):i + 1]:
        b = t.get("bin") or {}
        tw = t.get("twap") or {}
        if b.get("price") and tw.get("price"):
            vals.append((b["price"] - tw["price"]) / tw["price"] * 1e4)
    out = {}
    if len(vals) >= 5:
        v = np.asarray(vals, dtype=float)
        out["basis_mean"] = float(v.mean())
        out["basis_last"] = float(v[-1])
        out["basis_max_abs"] = float(np.abs(v).max())
        out["basis_slope"] = float(np.polyfit(np.arange(len(v)), v, 1)[0])  # bps/10s
        cross0 = int(np.any(v[:-1] * v[1:] < 0))
        out["basis_cross0"] = cross0
        for k in ("basis_mean", "basis_last", "basis_max_abs", "basis_slope"):
            out[f"f_{k}"] = sgn * out[k]
    return out


def pm_depth_feats(pm: dict, side: str, other: str) -> dict:
    """PM 双侧深度结构（★ 首次建模双侧不对称）。"""
    yb, ya = pm.get("yes_bid_top5") or 0, pm.get("yes_ask_top5") or 0
    nb, na = pm.get("no_bid_top5") or 0, pm.get("no_ask_top5") or 0
    out = {}
    tot = yb + ya + nb + na
    if tot > 0:
        out["dep_imbalance"] = (yb - nb) / (yb + nb) if (yb + nb) > 0 else None  # YES 侧 bid 占比
        out["dep_share_yes"] = (yb + ya) / tot
    b = pm.get(f"{side}_bid_top5") or 0
    a = pm.get(f"{side}_ask_top5") or 0
    ob = pm.get(f"{other}_bid_top5") or 0
    oa = pm.get(f"{other}_ask_top5") or 0
    if (b + a) > 0:
        out["cross_bid_share5"] = b / (b + a)
    if (ob + oa) > 0:
        out["other_bid_share5"] = ob / (ob + oa)
        out["other_ask_top5"] = oa  # 对侧 ask 深度 = flip 买入可用流动性
    out["cross_top5"] = b + a
    out["other_top5"] = ob + oa
    return out


# ────────────────────────────────────────────────────────────────
# 观测级全特征
# ────────────────────────────────────────────────────────────────

def build_obs_features(events, obs, hist_ranges, by_start):
    """对 extract_cross 的观测扩展为全特征 dict（决策时刻 B 特征全集 + 标签/填充）。"""
    rows = []
    for o in obs:
        ev = by_start[o["event_start"]]
        ticks = ev.get("ticks") or []
        i, side, other = o["i"], o["side"], o["other"]
        sgn = 1.0 if side == "no" else -1.0   # flip 有利方向校正
        hist = hist_ranges.get(o["event_start"])

        # v2 旧特征（穿越 ±10s 窗口）
        f = cross_window(ev, i, side, other, open_price=ev.get("twap_open_price"), hist=hist)
        # 穿越 tick 的原始快照
        t0 = ticks[i]
        bin0 = t0.get("bin") or {}
        pm0 = t0.get("pm") or {}

        # ★ 新特征
        f.update(bin_book_feats(bin0, sgn))
        t_now = t0["ts"]
        t_end = ticks[min(i + 10, len(ticks) - 1)]["ts"]
        f.update({f"pre_{k}": v for k, v in trades_quality(ev.get("trades") or [], side.upper(),
                                                            t_now - 10_000, t_now).items()})
        f.update({f"post_{k}": v for k, v in trades_quality(ev.get("trades") or [], side.upper(),
                                                             t_now, t_end).items()})
        f.update(regime_feats(ticks, i, ev.get("binance_open") or 0, sgn))
        f.update(basis_feats(ticks, i, sgn))
        f.update(pm_depth_feats(pm0, side, other))

        # ★ 穿越动力学（PM 速度）
        bid_now = pm0.get(f"{side}_bid") or 0
        cross_idx = None
        for k in range(i, -1, -1):
            if (ticks[k].get("pm") or {}).get(f"{side}_bid") and (ticks[k]["pm"][f"{side}_bid"]) > 0.5:
                cross_idx = k
                break
        f["cross_speed_s"] = float(i - cross_idx) if cross_idx is not None else None  # 0.5→0.7 用时
        b5 = (ticks[i - 5].get("pm") or {}).get(f"{side}_bid") if i >= 5 else None
        f["pm_vel5s"] = (bid_now - b5) * 100.0 if b5 else None    # 穿越侧 bid 5s 动量（分）
        f["i_pos"] = i / max(len(ticks), 1)
        f["trigger_bid"] = o["trigger_bid"]

        # 标签与填充（不参与特征）
        f["flip_won"] = 1 - int(o["won"])                          # 穿越侧输 = flip 赢
        f["flip_fill0s"] = pm0.get(f"{other}_ask")                 # 穿越即入 fill = 对侧 ask
        f["flip_fill2s"] = o.get("flip_fill2s")
        f["flip_fill10s"] = o.get("flip_fill10s")
        # follow 镜像口径（买穿越侧）: follow_won = 穿越侧赢
        f["follow_won"] = int(o["won"])
        f["follow_fill0s"] = pm0.get(f"{side}_ask")                # 穿越即入 fill = 触发侧 ask
        f["follow_fill2s"] = o.get("fill2s")                       # 1 - 对侧 bid@+2s
        f["follow_fill10s"] = o.get("fill10s")                     # 1 - 对侧 bid@+10s
        f["other_delta2s"] = o.get("other_delta2s")                # v2 经典对照
        f["other_delta10s"] = o.get("other_delta10s")
        f["cls"] = o["cls"]
        f["side"] = side
        f["rem"] = o["rem"]
        f["event_start"] = o["event_start"]
        rows.append(f)
    return rows


def hist_ranges(events):
    """每事件历史振幅基准（前 ≤18 窗口 |twap_close-twap_open| 均值, ≥3 才可用）。"""
    hr = {}
    for i, e in enumerate(events):
        prev = []
        for j in range(max(0, i - 18), i):
            o, c = events[j].get("twap_open_price"), events[j].get("twap_close_price")
            if o and c:
                prev.append(abs(c - o))
        hr[e["start_time"]] = sum(prev) / len(prev) if len(prev) >= 3 else None
    return hr


def main():
    events = load_events(DATA)
    by_start = {e["start_time"]: e for e in events}
    hr = hist_ranges(events)
    obs = extract_cross(events, "outcome")
    rows = build_obs_features(events, obs, hr, by_start)
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["event_start"], unit="s").dt.date.astype(str)
    df.to_pickle("data/featmat.pkl")
    # 摘要
    print(f"数据: {DATA}  |  事件 {len(events)}  |  穿越观测 {len(df)}")
    print(f"flip 基线: WR {df['flip_won'].mean() * 100:.1f}%  "
          f"fill0s {df['flip_fill0s'].mean():.3f}  fill10s {df['flip_fill10s'].mean():.3f}")
    print(f"follow 基线: WR {df['follow_won'].mean() * 100:.1f}%  "
          f"fill0s {df['follow_fill0s'].mean():.3f}  fill10s {df['follow_fill10s'].mean():.3f}")
    print(f"按日分布: {df['date'].value_counts().sort_index().to_dict()}")
    feat_cols = [c for c in df.columns if c not in
                 ("flip_won", "flip_fill0s", "flip_fill2s", "flip_fill10s",
                  "follow_won", "follow_fill0s", "follow_fill2s", "follow_fill10s",
                  "cls", "side", "rem", "event_start", "date")]
    print(f"特征数: {len(feat_cols)}（含 None 列: "
          f"{[c for c in feat_cols if df[c].isna().all()]}）")


if __name__ == "__main__":
    main()
