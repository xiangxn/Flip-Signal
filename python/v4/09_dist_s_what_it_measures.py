#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dist_s 究竟在量什么？（2026-09-16）—— 用户提问「dist_s 是不是回测里拟合出来的
无意义标量？」

策略本意：某侧 ask 被砸到 0.2（盘口崩了），但**现货相对历史波动还没真正走出来**
（浅坑），且剩余时间够 → 赌反转。本意要的是「位移 / 历史波动」这个比。

已证：dist_s ≡ dist_t + 基差项，Var(基差)/Var(dist_s) ≈ 83~90%。
本脚本回到 tick 级价路，把三个量翻译成价格路径语言并做**可分离的分解**：

  基差项 = 现货ₜ − TWAP₆₀ₜ
         = [现货ₜ − 现货₆₀均值ₜ]  +  [现货₆₀均值ₜ − TWAP₆₀ₜ]
           └─ 速度项(与当前价有关)┘   └─ 馈送偏移项(两个60s均值之差, 与当前价无关)┘

回答两件事：
  Q1 dist_s 反应了什么？—— 各项的方差占比 + 双方差项的持续性（慢偏移 vs 快噪声）
  Q2 它是不是拟合出来的空标量？—— 置换零分布下的「同网格 refit」红利
     （真正对口的零假设：把结果标签打乱, 同一个 argmax 网格搜索能刷出多少 U）

用法: python 09_dist_s_what_it_measures.py [--data <事件目录>] [--stake 2] [--perm 500]
"""
import argparse
import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("r1", BASE / "01_backtest_r1.py")
r1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(r1)
r1.REM_MAX = None          # 头条口径无上限（01 模块默认 240 是陷阱, 见 07 脚本注释）
r1.REM_MIN = 180

KS = (10, 30, 60, 120, 240)
SPAN = 60                  # TWAP-60 的回看长度
FIXED = {"yes": -0.6, "no": -1.0}     # 引擎现行组合带


def build(data_dir):
    """触发时刻的价路特征（与 01_backtest_r1.extract 同门控 + 同口径）。"""
    events = r1.load_events(data_dir)
    hr = r1.hist_ranges(events)
    rows = []
    for e in events:
        outcome, anchor = e.get("outcome"), e.get("twap_open_price")
        if outcome is None or not anchor:
            continue
        ticks = e.get("ticks") or []
        h = hr.get(e["start_time"])
        if not h:
            continue
        hist_bps = h / anchor * 1e4
        i = None
        for j, t in enumerate(ticks):
            pm = t.get("pm") or {}
            if not pm or (pm.get("book_latency_ms") or 0) > r1.MAX_LAT:
                continue
            if not all((pm.get(k) or 0) > 0
                       for k in ("yes_bid", "yes_ask", "no_bid", "no_ask")):
                continue
            if (pm.get("yes_ask") or 1) <= 0.20 or (pm.get("no_ask") or 1) <= 0.20:
                i = j
                break
        if i is None:
            continue
        t, pm = ticks[i], ticks[i]["pm"]
        rem = t.get("rem")
        spot = (t.get("bin") or {}).get("price")
        if rem is None or not spot:
            continue
        ya, na = (pm.get("yes_ask") or 1), (pm.get("no_ask") or 1)
        if ya <= 0.20 and na <= 0.20:
            side = "no" if spot > anchor else "yes"
        elif ya <= 0.20:
            side = "yes"
        else:
            side = "no"
        sgn = 1.0 if side == "yes" else -1.0
        r = {"event_start": e["start_time"], "side": side, "rem": rem,
             "fill": pm.get(f"{side}_ask") or 0, "anchor": anchor, "spot": spot}
        r["settle_won"] = int((outcome == 0) if side == "yes" else (outcome == 1))
        r["dist_s"] = sgn * (spot - anchor) / anchor * 1e4 / hist_bps
        twp = (t.get("twap") or {}).get("price")
        if twp:
            r["dist_t"] = sgn * (twp - anchor) / anchor * 1e4 / hist_bps
            r["basis"] = r["dist_s"] - r["dist_t"]
        sp = [(ticks[j].get("bin") or {}).get("price") or 0.0 for j in range(i + 1)]
        if sp[0] > 0:
            # 窗口开盘瞬间的锚缺口: 与触发无关, 开盘即已知
            r["gap0"] = sgn * (sp[0] - anchor) / anchor * 1e4 / hist_bps
        for k in KS:
            r[f"v{k}"] = (sgn * (sp[i] - sp[i - k]) / anchor * 1e4 / hist_bps
                          if i - k >= 0 and sp[i - k] > 0 else np.nan)
        # 速度项 = 现货相对自身 60s 均值；馈送偏移项 = 两个 60s 均值之差（与当前价无关）
        if twp and i >= SPAN:
            win = [x for x in sp[i - SPAN:i + 1] if x > 0]
            if len(win) >= SPAN // 2:
                m60 = float(np.mean(win))
                r["vel"] = sgn * (sp[i] - m60) / anchor * 1e4 / hist_bps
                r["foff"] = sgn * (m60 - twp) / anchor * 1e4 / hist_bps
        rows.append(r)
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["event_start"], unit="s").dt.date.astype(str)
    return df


def residual_structure(data_dir, stride=3):
    """逐 tick 检验「馈送偏移项」是慢偏移还是快噪声：
    窗口内均值/窗口内 std 的方差分解 + 相邻 tick 自相关。"""
    events = r1.load_events(data_dir)
    hr = r1.hist_ranges(events)
    allv, wmean, within = [], [], []
    for e in events:
        anchor = e.get("twap_open_price")
        h = hr.get(e["start_time"])
        if not anchor or not h:
            continue
        hist_bps = h / anchor * 1e4
        ticks = e.get("ticks") or []
        ser = []
        for j in range(0, len(ticks), stride):
            t = ticks[j]
            pm = t.get("pm") or {}
            rem = t.get("rem")
            if not pm or rem is None or not (30 <= rem <= 270):
                continue
            if (pm.get("book_latency_ms") or 0) > r1.MAX_LAT:
                continue
            sp = [(ticks[q].get("bin") or {}).get("price") or 0.0
                  for q in range(max(0, j - SPAN), j + 1)]
            win = [x for x in sp if x > 0]
            twp = (t.get("twap") or {}).get("price")
            if not twp or len(win) < SPAN // 2:
                continue
            ser.append((np.mean(win) - twp) / anchor * 1e4 / hist_bps)
        if len(ser) >= 5:
            a = np.array(ser)
            allv.append(a)
            wmean.append(a.mean())
            within.append(a.std())
    flat = np.concatenate(allv)
    wmean, within = np.array(wmean), np.array(within)
    ac = float(np.corrcoef(flat[:-1], flat[1:])[0, 1])
    return {"n_tick": len(flat), "n_win": len(allv), "std": flat.std(),
            "ac1": ac, "var_between": wmean.var(), "var_within": (within ** 2).mean()}


def cross_window(data_dir, stride=5):
    """馈送偏移项跨窗口是否持续：按时间排序取每窗中段值（未归一 bps），
    比较相邻窗、同日前半/后半窗的相关与均值漂移。"""
    events = sorted(r1.load_events(data_dir), key=lambda e: e["start_time"])
    ser = []
    for e in events:
        anchor = e.get("twap_open_price")
        ticks = e.get("ticks") or []
        if not anchor or len(ticks) < 120:
            continue
        vals = []
        for j in range(0, len(ticks), stride):
            t = ticks[j]
            pm = t.get("pm") or {}
            rem = t.get("rem")
            if not pm or rem is None or rem > 240:
                continue
            sp = [(ticks[q].get("bin") or {}).get("price") or 0.0
                  for q in range(max(0, j - SPAN), j + 1)]
            win = [x for x in sp if x > 0]
            twp = (t.get("twap") or {}).get("price")
            if twp and len(win) >= SPAN // 2:
                vals.append((np.mean(win) - twp) / anchor * 1e4)
        if len(vals) >= 10:
            ser.append((e["start_time"], float(np.mean(vals)), float(np.std(vals))))
    a = np.array([s[1] for s in ser])
    nxt = np.array([s[1] for s in ser[1:]])
    cur = np.array([s[1] for s in ser[:-1]])
    # 同日内: 前半窗均值 vs 后半窗均值
    day = np.array([pd.to_datetime(s[0], unit="s").date() for s in ser])
    half = []
    for d in np.unique(day):
        v = a[day == d]
        if len(v) >= 8:
            half.append((v[:len(v) // 2].mean(), v[len(v) // 2:].mean()))
    return {"n_win": len(a), "std": a.std(),
            "ac1": float(np.corrcoef(cur, nxt)[0, 1]) if len(a) > 3 else np.nan,
            "half": np.array(half) if half else np.zeros((0, 2))}


def corr(a, b):
    m = a.notna() & b.notna()
    return float(np.corrcoef(a[m], b[m])[0, 1]) if m.sum() >= 3 else np.nan


def pl_of(s, stake):
    return np.where(s["settle_won"] == 1, stake / s["fill"] - stake, -stake)


def band_mask(frame, coord, los):
    """按侧别下限 lo(side) 生成 (lo, 0) 开区间掩码。"""
    m = np.zeros(len(frame), bool)
    for side, lo in los.items():
        m |= ((frame["side"] == side).to_numpy() & frame[coord].notna().to_numpy()
              & (frame[coord] > lo).to_numpy() & (frame[coord] < 0).to_numpy())
    return m


def refit(frame, coord, los_grid, min_n=20):
    """双侧独立 argmax(总 P&L) —— 与 08 脚本同法。"""
    picks = {}
    for side in ("yes", "no"):
        sub = frame[frame["side"] == side]
        best = None
        for lo in los_grid:
            m = (sub[coord].notna() & (sub[coord] > lo) & (sub[coord] < 0)).to_numpy()
            if m.sum() < min_n:
                continue
            p = pl_of(sub[m], 1.0).sum()      # 置换零分布只需排序, 单位任意
            if best is None or p > best[1]:
                best = (lo, p)
        if best:
            picks[side] = best[0]
    return picks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(BASE.parent.parent / "data" / "btc"))
    ap.add_argument("--stake", type=float, default=2.0)
    ap.add_argument("--perm", type=int, default=500)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--skip-perm", action="store_true")
    args = ap.parse_args()
    stake = args.stake
    rng = np.random.default_rng(args.seed)

    df = build(args.data)
    ref = r1.extract(args.data)
    df = df.merge(ref[["event_start", "m_45", "h"]], on="event_start", how="left")
    print(f"数据: 触发 {len(df)}（{df['date'].nunique()} 天）\n")

    # ================= Q1 =================
    print("=" * 78)
    print("【Q1】dist_s 在价格路径语言里是什么")
    print("=" * 78)
    print("  dist_s   = sgn·(现货ₜ − 锚)/锚/σ      现货自窗口开盘的累计位移")
    print("  dist_t   = sgn·(TWAPₜ − 锚)/锚/σ      结算线自开盘的累计位移 ← 真正的输赢变量")
    print("  基差项   = sgn·(现货ₜ − TWAPₜ)/锚/σ")
    print("           = 速度项 + 馈送偏移项")
    print("  速度项   = sgn·(现货ₜ − 现货的60s均值)/锚/σ     现货相对自身均线的偏离")
    print("  馈送偏移 = sgn·(现货的60s均值 − TWAP₆₀)/锚/σ   两个60s均值之差, 与当前价无关\n")

    sub = df.dropna(subset=["dist_s", "dist_t", "basis", "vel", "foff"])
    print(f"  可分解样本 n={len(sub)}（需 ≥60 tick 回看）")
    print(f"\n  {'量':<10}{'中位':>8}{'std':>9}{'与dist_s相关':>13}{'与胜负点二列':>14}")
    for c in ("dist_t", "basis", "vel", "foff"):
        print(f"  {c:<10}{sub[c].median():>+8.2f}{sub[c].std():>9.2f}"
              f"{corr(sub['dist_s'], sub[c]):>+13.3f}"
              f"{corr(sub[c], sub['settle_won']):>+14.3f}")

    b, v, f = sub["basis"], sub["vel"], sub["foff"]
    tot = b.var()
    print(f"\n  Var(基差)={tot:.4f} = Var(速度项)={v.var():.4f}"
          f" + Var(馈送偏移)={f.var():.4f} + 2Cov={2*np.cov(v,f)[0,1]:+.4f}")
    print(f"    占比: 速度项 {v.var()/tot*100:5.1f}%   馈送偏移 {f.var()/tot*100:5.1f}%"
          f"   交叉 {2*np.cov(v,f)[0,1]/tot*100:+5.1f}%")
    print(f"    ρ(速度项, 馈送偏移) = {corr(v, f):+.3f}"
          f"   → 两项{'几乎正交, 可当独立坐标用' if abs(corr(v,f)) < 0.2 else '有耦合'}")
    print(f"\n  dist_s 的可解释度: Var(dist_t+速度项+馈送偏移) vs Var(dist_s)"
          f"  →  R²={1-((sub['dist_s']-sub['dist_t']-b)**2).sum()/((sub['dist_s']-sub['dist_s'].mean())**2).sum():.6f}")
    print(f"  ρ(dist_s, 现货自开盘累计位移) = "
          f"{corr(sub['dist_s'], sub['dist_s']-sub['dist_t']+sub['dist_t']):+.3f} (恒等式)")
    print(f"  ρ(dist_s, v240) = {corr(sub['dist_s'], sub['v240']):+.3f}"
          f"   ρ(dist_s, v60) = {corr(sub['dist_s'], sub['v60']):+.3f}")

    print("\n  馈送偏移项的持续性（逐 tick, rem 30~270, 步长 3）:")
    rs = residual_structure(args.data)
    print(f"    tick 样本 {rs['n_tick']}  涉及窗口 {rs['n_win']}  总 std {rs['std']:.4f}")
    print(f"    窗口内 std² {rs['var_within']:.4f} vs 窗口间均值方差 {rs['var_between']:.4f}"
          f"  → 窗口间占比 {rs['var_between']/(rs['var_between']+rs['var_within'])*100:.1f}%")
    print(f"    相邻 tick 自相关 (步长3) = {rs['ac1']:+.3f}"
          f"  → {'慢变量（馈送级偏移）' if rs['ac1'] > 0.5 else '快噪声' if rs['ac1'] < 0.2 else '中等持续性'}")

    print("\n【Q1b】既然馈送偏移在窗内近乎常数，dist_s 在开盘时就定型了多少？")
    g = df.dropna(subset=["gap0", "dist_s"])
    print(f"  gap0 = sgn·(开盘瞬间现货 − 锚)/锚/σ（触发前就已知, 不含任何砸盘信息）")
    print(f"    ρ(gap0, dist_s) = {corr(g['gap0'], g['dist_s']):+.3f}"
          f"   Var(gap0)/Var(dist_s) = {g['gap0'].var()/g['dist_s'].var()*100:.1f}%")
    pop_g = g[(g["rem"] > r1.REM_MIN) & (g["m_45"] >= r1.CRASH_MIN)]
    los_grid = np.round(np.arange(-1.6, 0.0, 0.05), 2)
    pk = refit(pop_g, "gap0", los_grid)
    m = band_mask(pop_g, "gap0", pk)
    s = pop_g[m]
    print(f"    gap0 refit 带 yes({pk.get('yes')})/no({pk.get('no')}): n={len(s)}"
          f"  WR {s.settle_won.mean()*100:.1f}%  EV {pl_of(s, stake).mean():+.3f}U/注"
          f"  P&L {pl_of(s, stake).sum():+.1f}U")
    mfix = band_mask(pop_g, "dist_s", FIXED)
    agree = float((band_mask(pop_g, "gap0", FIXED) & mfix).sum()) / max(mfix.sum(), 1)
    print(f"    现行固定带命中的 {mfix.sum()} 笔里, 有 {agree*100:.1f}% 在开盘瞬间就已在同一带内")
    cw = cross_window(args.data)
    print(f"\n  馈送偏移跨窗口: 涉及 {cw['n_win']} 窗  窗间 std {cw['std']:.3f}bps"
          f"  相邻窗相关 {cw['ac1']:+.3f}")
    if len(cw["half"]):
        d = cw["half"][:, 1] - cw["half"][:, 0]
        print(f"    同日内前半窗→后半窗均值漂移: 中位 {np.median(d):+.3f}bps"
              f"  |漂移|/窗间std = {np.median(abs(d))/cw['std']:.2f}"
              f"  → {'慢 regime（跨窗持续）' if np.median(abs(d)) < 0.5*cw['std'] else '每窗重新抽取'}")

    # ================= Q2 =================
    print("\n" + "=" * 78)
    print("【Q2】它是不是回测拟合出来的空标量？")
    print("=" * 78)
    pop = df[(df["rem"] > r1.REM_MIN) & (df["m_45"] >= r1.CRASH_MIN)].copy()
    print(f"  口径 = 头条（rem>180, m_45≥{r1.CRASH_MIN}）: n={len(pop)}")
    fixm = band_mask(pop, "dist_s", FIXED)
    print(f"  现行固定带 yes(−0.6)/no(−1.0): n={fixm.sum()}  "
          f"WR {pop[fixm].settle_won.mean()*100:.1f}%  "
          f"EV {pl_of(pop[fixm], stake).mean():+.3f}U/注  "
          f"P&L {pl_of(pop[fixm], stake).sum():+.1f}U  ← 应与头条 625/+396U 吻合\n")

    los_grid = np.round(np.arange(-1.6, 0.0, 0.05), 2)
    print("  Q2a 各坐标同网格 refit（双侧独立 argmax）:")
    print(f"    {'坐标':<10}{'带 yes/no':>18}{'n':>6}{'WR':>8}{'EV':>9}{'P&L':>10}")
    for coord in ("dist_s", "dist_t", "vel", "foff", "basis", "v60"):
        pk = refit(pop, coord, los_grid)
        if not pk:
            continue
        m = band_mask(pop, coord, pk)
        s = pop[m]
        tag = f"{pk.get('yes')}/{pk.get('no')}"
        print(f"    {coord:<10}{tag:>18}{len(s):>6}{s.settle_won.mean()*100:>7.1f}%"
              f"{pl_of(s, stake).mean():>+9.3f}{pl_of(s, stake).sum():>+10.1f}U")

    if args.skip_perm:
        return
    # ---- Q2b 置换零分布：同网格 refit 在打乱标签上能刷出多少
    print(f"\n  Q2b 置换零分布（打乱 settle_won, 同网格 argmax, {args.perm} 次）——")
    print("      这是「拟合空标量」的对口零假设: 若坐标无线索, 同网格照样能刷出正 U")
    spl = pop[["side", "settle_won", "h"]].copy()
    for coord in ("dist_s", "dist_t", "basis"):
        obs_picks = refit(pop, coord, los_grid)
        obs = pl_of(pop[band_mask(pop, coord, obs_picks)], stake).sum()
        null = np.empty(args.perm)
        for t in range(args.perm):
            sh = spl.copy()
            sh["settle_won"] = (spl.groupby("side")["settle_won"]
                                .transform(lambda s: rng.permutation(s.to_numpy())))
            frame = pop.copy()
            frame["settle_won"] = sh["settle_won"].to_numpy()
            null[t] = pl_of(frame[band_mask(frame, coord, refit(frame, coord, los_grid))],
                            stake).sum()
        p = float((null >= obs).mean())
        print(f"    {coord:<10} 观测 refit {obs:+7.1f}U | 零分布 p50 {np.median(null):+7.1f}U"
              f"  p95 {np.percentile(null,95):+7.1f}U  p99 {np.percentile(null,99):+7.1f}U"
              f"  → p={p:.3f} {'✅ 超噪声' if p < 0.05 else '❌ 与拟合噪声不可区分'}")

    # ---- Q2c 半样本：h1 选带 → h2 验证（带自己的零分布）
    print(f"\n  Q2c 半样本 h1选→h2验（带自己的置换零分布）")
    h1 = pop[pop["h"] == pop["h"].min()]
    h2 = pop[pop["h"] == pop["h"].max()]
    print(f"      h1 n={len(h1)}  h2 n={len(h2)}   （h 值 {sorted(pop['h'].unique())}）")
    for coord in ("dist_s", "dist_t", "basis"):
        pk = refit(h1, coord, los_grid)
        if not pk:
            continue
        oos = pl_of(h2[band_mask(h2, coord, pk)], stake).sum()
        null_h = np.empty(max(50, args.perm // 4))
        s1 = h1[["side", "settle_won"]].copy()
        for t in range(len(null_h)):
            f1 = h1.copy()
            f1["settle_won"] = (s1.groupby("side")["settle_won"]
                                .transform(lambda s: rng.permutation(s.to_numpy()))
                                ).to_numpy()
            pk_n = refit(f1, coord, los_grid)
            null_h[t] = pl_of(h2[band_mask(h2, coord, pk_n)], stake).sum() if pk_n else 0.0
        p = float((null_h >= oos).mean())
        print(f"      {coord:<8} h1带 yes({pk.get('yes')})/no({pk.get('no')})"
              f"  → h2 {oos:+7.1f}U | 零分布 p50 {np.median(null_h):+7.1f}U"
              f" p95 {np.percentile(null_h,95):+7.1f}U  → p={p:.3f}"
              f" {'✅' if p < 0.05 else '❌'}")


if __name__ == "__main__":
    main()
