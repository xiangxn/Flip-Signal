#!/usr/bin/env python3
"""
follow（顺势）专项分析 —— 穿越 0.7 后买入触发侧，能否赚钱？

背景: flip 信号少（C1&C2 双过滤后仅 140/3753），而 follow 每个穿越都是事件。
用户假设: "0.7 左右入场 + 胜率 > 80% 应该能赚"。

口径（与 flip 完全对齐）:
  * 观测 = 事件内首个 0.7 上升沿（extract_cross 同款）
  * follow = 买触发侧（顺势）
  * fill_real{0,2,10}s = 决策时刻触发侧真实 ask（纸面/实盘口径）
  * fill_comp{0,2,10}s = 1 - 对侧 bid（回测互补口径, fill{2,10}s 即此）
  * EV = WR - fill（每股兑 1U）
  * 标签: won = 触发侧赢（官方 outcome 结算）

关注问题:
  1. 全量 follow 的 WR / fill / EV 到底如何
  2. 按 trigger_bid / fill 分层, 是否存在 WR ≥ 80% 且 EV > 0 的子集
  3. 入场时点 +0s / +2s / +10s 哪个口径更优
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from v2.lib import load_events, extract_cross

REPO = Path(__file__).resolve().parent.parent.parent
DATA = str(REPO / "data" / "btc")


def enrich_obs(events, obs):
    """补真实 ask 口径 fill（穿越/确认时刻触发侧 ask）与决策时刻 bid。"""
    by_start = {e["start_time"]: e for e in events}
    rows = []
    for o in obs:
        ev = by_start[o["event_start"]]
        ticks = ev.get("ticks") or []
        side, other, i = o["side"], o["other"], o["i"]
        r = dict(o)

        def snap(j):
            pm = ticks[min(j, len(ticks) - 1)].get("pm") or {}
            return {
                "bid": pm.get(f"{side}_bid") or 0.0,
                "ask": pm.get(f"{side}_ask") or 0.0,
                "oth_bid": pm.get(f"{other}_bid") or 0.0,
            }

        for ds in (0, 2, 10):
            s = snap(i + ds)
            r[f"fill_real{ds}s"] = s["ask"] or None          # 真实吃单价
            r[f"fill_comp{ds}s"] = 1.0 - s["oth_bid"] if s["oth_bid"] else None
        rows.append(r)
    return rows


def summ(df, fill_key, label=""):
    """按 fill 口径汇总 EV = WR - fill。"""
    d = df.dropna(subset=[fill_key]).copy()
    n = len(d)
    if n == 0:
        return None
    wr = d["won"].mean()
    fill = d[fill_key].mean()
    ev = wr - fill
    # Wilson CI for WR
    z = 1.96
    denom = 1 + z**2 / n
    p_hat = (wr + z**2 / (2 * n)) / denom
    half = z * np.sqrt(wr * (1 - wr) / n + z**2 / (4 * n**2)) / denom
    return {
        "n": n, "WR": wr, "fill": fill, "EV": ev,
        "WR_lo": p_hat - half, "WR_hi": p_hat + half,
    }


def report(df, fill_key, title):
    s = summ(df, fill_key)
    print(f"\n===== {title} （fill = {fill_key}） =====")
    if not s:
        print("  无有效样本")
        return
    print(f"  n={s['n']}  WR={s['WR']*100:.1f}% [{(s['WR_lo'])*100:.1f}, {(s['WR_hi'])*100:.1f}]  "
          f"fill={s['fill']:.3f}  EV={s['EV']*100:+.2f}/股")


def main():
    events = load_events(DATA)
    obs = extract_cross(events, "outcome")
    df = pd.DataFrame(enrich_obs(events, obs))
    df["date"] = pd.to_datetime(df["event_start"], unit="s").dt.date.astype(str)
    print(f"数据: {DATA} | 事件 {len(events)} | follow 观测 {len(df)}")

    # ── 1. 全量基线 ──
    for ds in (0, 2, 10):
        report(df, f"fill_real{ds}s", f"全量 follow 基线（+{ds}s 真实 ask 入场）")
        report(df, f"fill_comp{ds}s", f"全量 follow 基线（+{ds}s 互补口径入场）")

    # ── 2. trigger_bid 分层 ──
    bins = [0.7, 0.72, 0.74, 0.76, 0.80, 0.85, 1.01]
    labels = ["0.70-0.72", "0.72-0.74", "0.74-0.76", "0.76-0.80", "0.80-0.85", "0.85+"]
    df["tb_bin"] = pd.cut(df["trigger_bid"], bins=bins, labels=labels, right=False)
    print("\n===== 按 trigger_bid 分层（+10s 真实 ask / +10s 互补） =====")
    for b, g in df.groupby("tb_bin", observed=True):
        r_real = summ(g, "fill_real10s")
        r_comp = summ(g, "fill_comp10s")
        if r_real:
            print(f"  [{b}] n={r_real['n']:4d}  WR={r_real['WR']*100:5.1f}%  "
                  f"real fill={r_real['fill']:.3f} EV={r_real['EV']*100:+6.2f}  |  "
                  f"comp fill={r_comp['fill']:.3f} EV={r_comp['EV']*100:+6.2f}")

    # ── 3. 按 fill（入场价）分层: 用户假设 "0.7 左右入场" ──
    f_bins = [0, 0.71, 0.73, 0.75, 0.80, 1.01]
    f_labels = ["<=0.71", "0.71-0.73", "0.73-0.75", "0.75-0.80", "0.80+"]
    df["fb_bin"] = pd.cut(df["fill_real10s"], bins=f_bins, labels=f_labels, right=False)
    print("\n===== 按 +10s 真实入场价分层（用户假设: 0.7 左右入场） =====")
    for b, g in df.groupby("fb_bin", observed=True):
        r = summ(g, "fill_real10s")
        if r:
            print(f"  [{b}] n={r['n']:4d}  WR={r['WR']*100:5.1f}% [{(r['WR_lo'])*100:.1f},{(r['WR_hi'])*100:.1f}]  "
                  f"fill={r['fill']:.3f}  EV={r['EV']*100:+6.2f}/股")

    # ── 4. 深度: 触发侧 bid 能到 0.7+ 但 ask 仍低（入场价接近 0.7 的子集） ──
    cheap = df[df["fill_real10s"] <= 0.73]
    print(f"\n===== fill<=0.73 的子集（\"0.7 左右入场\"）=====")
    print(f"  n={len(cheap)} / {len(df)}（{len(cheap)/len(df)*100:.0f}%）")
    report(cheap, "fill_real10s", "  fill<=0.73 全量")
    for b, g in cheap.groupby("tb_bin", observed=True):
        r = summ(g, "fill_real10s")
        if r:
            print(f"  [{b}] n={r['n']:4d}  WR={r['WR']*100:5.1f}%  fill={r['fill']:.3f}  EV={r['EV']*100:+6.2f}")

    # ── 5. 分日稳定性（全量 follow +10s real） ──
    print("\n===== 分日（+10s 真实 ask） =====")
    for d, g in df.groupby("date"):
        r = summ(g, "fill_real10s")
        if r:
            print(f"  {d}  n={r['n']:4d}  WR={r['WR']*100:5.1f}%  fill={r['fill']:.3f}  EV={r['EV']*100:+6.2f}")

    # ── 6. 穿越后 10s 触发侧 bid 轨迹（触发侧 bid@10s = 1 - 对侧 ask@10s） ──
    print("\n===== 穿越后 10s 触发侧 bid 轨迹 =====")
    d10 = df.dropna(subset=["fill_real10s"]).copy()
    # 触发侧 bid@10s: 互补恒等数据下 = 1 - 对侧 ask@10s; 直接比对侧 bid 更准:
    # fill_comp10s = 1 - 对侧 bid@10s → 对侧 bid@10s = 1 - fill_comp10s
    # 触发侧 bid 涨跌 = 穿越时触发侧 bid - 对侧 bid@10s 的互补方向, 用原始字段:
    d10["bid_chg10s"] = d10["trigger_bid"] - (1 - d10["fill_comp10s"])
    d10 = d10.dropna(subset=["bid_chg10s"])
    up = d10[d10["bid_chg10s"] > 0]
    dn = d10[d10["bid_chg10s"] <= 0]
    for nm, g in (("触发侧 bid 继续走强", up), ("触发侧 bid 回落", dn)):
        r = summ(g, "fill_real10s")
        if r:
            print(f"  {nm}: n={r['n']:4d}  WR={r['WR']*100:5.1f}%  fill={r['fill']:.3f}  EV={r['EV']*100:+6.2f}")

    # ── 7. cls 拆分（only/both 为未来信息, 仅作解剖） ──
    print("\n===== only/both 解剖（+10s real） =====")
    for c, g in df.groupby("cls"):
        r = summ(g, "fill_real10s")
        if r:
            print(f"  {c}: n={r['n']:4d}  WR={r['WR']*100:5.1f}%  fill={r['fill']:.3f}  EV={r['EV']*100:+6.2f}")

    df.to_pickle("data/follow.pkl")


if __name__ == "__main__":
    main()
