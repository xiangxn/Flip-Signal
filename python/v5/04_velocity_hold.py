#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v5 速度保持探查（用户逻辑捋顺版, 2026-09-08）:
下注三条件:
  1 便宜   该侧 ask ≤ 0.30（观察起点, 只负责筛选"折价"）
  2 方向   速度站在下注方向一侧（友好）: buy yes → 速度>0; buy no → 速度<0
  3 保持   友好状态**连续保持 K 秒**后才在首个完成 tick 下注
            （持续确认过滤单 tick 噪声; fill = 该 tick 该侧 ask, 可已回升>0.3）
速度度量两候选（分别出表）:
  A 盘口侧速度: 该侧 ask 短窗净变化（PM 重新定价方向）
  B spot 速度:  Binance price OLS 斜率 v（doc §十 口径, 预测腿输入）
统计: 各 (度量 × K) 桶的 n/WR/均fill/EV + 下注距 t0 延迟 + 不下注族 WR 对照。
"""
import argparse
import sys
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))
from v2.lib import load_events  # noqa: E402

TRIG = 0.30
LAT_MAX = 300
KS = (5, 10, 20, 30)     # 友好保持秒数（分桶）
SLOPE_WIN = 10           # 盘口侧速度净变化窗（秒）
LOOK = 30                # spot OLS 回看秒
STAKE = 2.0


def pm_ok(t):
    pm = t.get("pm") or {}
    return (pm.get("book_latency_ms") or 0) <= LAT_MAX and all(
        (pm.get(k) or 0) > 0 for k in ("yes_bid", "yes_ask", "no_bid", "no_ask"))


def bet(r, key, K, t0, n):
    """首个「连续 K 个有效 tick 全友好」的完成 tick; 返回 j* 或 -1。
    有效 tick = pm 报价齐全（含 rem≥1）。"""
    side = r["side"]
    fr = (r[key] < 0) if side == "no" else (r[key] > 0)
    fr = fr & np.isfinite(r[key]) & r["ok"]
    run = 0
    for j in range(t0 + 1, n):
        if fr[j] and r["rem"][j] >= 1:
            run += 1
            if run >= K:
                return j
        else:
            run = 0
    return -1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(BASE.parent.parent / "data" / "btc"))
    args = ap.parse_args()
    events = load_events(args.data)
    print(f"数据 {args.data}: {len(events)} 事件")
    print(f"口径: 该侧 ask≤{TRIG} 首个有效 tick=t0（观察起点）; 速度友好连续保持 K 秒")
    print("的首个完成 tick = 下注点（fill=当时该侧 ask, 可>0.3）; 无 K 秒保持→不下注\n")

    rows = []
    for e in events:
        outcome = e.get("outcome")
        if outcome is None:
            continue
        ticks = e.get("ticks") or []
        n = len(ticks)
        ts = np.array([t["ts"] for t in ticks], dtype=float) / 1000.0 - e["start_time"]
        rem = np.array([t.get("rem", -1) for t in ticks], dtype=float)
        ok = np.zeros(n, bool)
        a = {s: np.full(n, np.nan) for s in ("yes", "no")}
        pr = np.full(n, np.nan)
        for i, t in enumerate(ticks):
            pm = t.get("pm") or {}
            if pm_ok(t):
                ok[i] = True
                for s in ("yes", "no"):
                    a[s][i] = pm.get(f"{s}_ask") or 0.0
            b = t.get("bin") or {}
            if b.get("price"):
                pr[i] = b["price"]
        # spot OLS-30（滑动, 两指针运行和）
        v30 = np.full(n, np.nan)
        lo = cc = 0
        sx = sy = sxx = sxy = 0.0
        for i in range(n):
            x, y = ts[i], pr[i]
            if np.isfinite(y):
                cc += 1; sx += x; sy += y; sxx += x * x; sxy += x * y
            while ts[i] - ts[lo] > LOOK:
                xl, yl = ts[lo], pr[lo]
                if np.isfinite(yl):
                    cc -= 1; sx -= xl; sy -= yl; sxx -= xl * xl; sxy -= xl * yl
                lo += 1
            if cc >= 24:
                den = cc * sxx - sx * sx
                if den > 1e-12:
                    v30[i] = (cc * sxy - sx * sy) / den
        # 盘口侧 ask 净变化（前 SLOPE_WIN 秒, 只算有效 tick）
        dask = {s: np.full(n, np.nan) for s in ("yes", "no")}
        for s in ("yes", "no"):
            for j in range(n):
                if not ok[j]:
                    continue
                lo2 = np.searchsorted(ts, ts[j] - SLOPE_WIN, side="left")
                seg = a[s][lo2:j + 1]
                f = seg[np.isfinite(seg)]
                if len(f) >= 5:
                    dask[s][j] = f[-1] - f[0]
        for side in ("yes", "no"):
            hit = np.where(ok & (a[side] <= TRIG) & (rem >= 1))[0]
            if len(hit) == 0:
                continue
            t0 = int(hit[0])
            won = int((outcome == 0) if side == "yes" else (outcome == 1))
            rows.append({"event_start": e["start_time"], "side": side, "t0": t0,
                         "won": won, "ask": a[side], "rem": rem, "ok": ok,
                         "dask": dask[side], "v30": v30, "n": n})

    print(f"观察段 {len(rows)}\n")
    for metric, key in (("盘口侧速度(ask 净变化)", "dask"),
                        ("spot 速度(OLS-30)", "v30")):
        for K in KS:
            wins, fills, dlays = [], [], []
            sk_wins, sk_n = 0, 0
            for r in rows:
                j = bet(r, key, K, r["t0"], r["n"])
                if j < 0:
                    sk_n += 1
                    sk_wins += r["won"]
                    continue
                wins.append(r["won"])
                fills.append(r["ask"][j])
                dlays.append(j - r["t0"])
            n_bet = len(wins)
            if not n_bet:
                print(f"{metric} K={K:>2}s: 下注 0")
                continue
            wr = np.mean(wins) * 100
            f = np.array(fills)
            pl = np.where(np.array(wins) == 1, STAKE / f - STAKE, -STAKE)
            print(f"{metric} | K={K:>2}s | 下注 n={n_bet:5d} "
                  f"({n_bet/(n_bet+sk_n)*100:5.1f}%) WR {wr:5.1f}% | "
                  f"fill 均 {f.mean():.3f} p50 {np.median(f):.3f} p75 "
                  f"{np.percentile(f,75):.3f} | 延迟中位 {np.median(dlays):.0f}s | "
                  f"EV {pl.mean():+.3f}U/注 P&L {pl.sum():+8.1f}U | "
                  f"不下注族 n={sk_n:4d} WR {sk_wins/max(sk_n,1)*100:4.1f}%")
        print()

    print("（口径注: fill 可>0.3=确认后价格已回升; 不下注族=速度从未在友好侧保持 K 秒"
          "\n = 恶化/稳住族, 预期 WR 低。下注族 WR 需 > fill(盈亏平衡) 才有 EV。）")


if __name__ == "__main__":
    main()
