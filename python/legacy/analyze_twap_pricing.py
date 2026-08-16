#!/usr/bin/env python3
"""
Phase 2：影响模型定价对照（2026-08-16，v1）。

用 Phase 1 的影响函数（analyze_twap_influence.py）构建逐格定价模型：
  Δtwap = twap_close - twap_t = β_cell · gap_t + ε,  ε ~ N(0, σ_cell²)
  P̂(up) = Φ( (β_cell·gap_t - θ_t) / σ_cell ),   θ_t = twap_open - twap_t

然后把 P̂ 与 PM 订单簿的市场隐含概率对照，计算可成交 EV：
  买 YES: EV = P̂(up) - (1 - no_bid)   （fill = 对侧 ask = 1 - 触发侧 bid）
  买 NO : EV = (1 - P̂(up)) - (1 - yes_bid)
  市场隐含 P(up) 取 mid ≈ (yes_bid + 1 - no_bid) / 2

铁律（无未来数据）：
  - β/σ 只在前半样本（train）拟合，后半样本（test）冻结评估
  - 特征只用 ≤ tick 数据（gap/θ 均为当前时刻已知量）
  - 结算用 event.outcome（TWAP 官方口径，官方收盘价轮询修正后）
  - 每事件最多一注（取最早满足 EV 阈值的 tick），剩余 ≥ min_rem 才可成交

输出：
  1. 数据与模型概览（train/test 切分、各格 β/σ）
  2. 整体评估：不同 EV 阈值下的下注率与 realized EV（per-tick 与 per-event 口径）
  3. 按格子：n 注 / 模型 P̂ / 市场 mid / 残差 / realized EV
  4. 按天：test 期间逐日 realized EV
  5. 校准表：P̂ 十分位 vs 实际 UP 率
  6. 穿越类别交互：下注时刻的单侧/双侧穿越状态 × realized EV

用法:
    ./venv/bin/python analyze_twap_pricing.py --data ../data/btc
"""

import argparse
import math
from datetime import datetime, timezone

from analyze_twap_influence import (
    R_BUCKETS,
    load_events,
    inject_hist_range,
    build_regime_map,
    meanstd,
    slope_corr,
)

MIN_TRAIN_TICKS = 50   # 格子训练样本下限，不足则测试期不下注
MIN_REM = 15           # 可成交剩余秒数下限（与 08-15 重挖口径一致）
EV_GRID = [0.0, 0.03, 0.05]  # EV 阈值敏感性


def phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def collect_pricing_ticks(events, regime_map):
    """逐 tick 收集定价所需观测（gap/θ/盘口/结算/穿越状态）。"""
    ticks = []
    for e in events:
        to = e.get("twap_open_price") or 0.0
        tc = e.get("twap_close_price") or 0.0
        outcome = e.get("outcome")
        if to <= 0 or tc <= 0 or outcome is None:
            continue
        snaps = e["snapshots"]
        reg = regime_map.get(e["start_time"])
        yc = nc = False  # 到当前 tick 为止该侧是否穿越过 0.7
        for i, s in enumerate(snaps):
            if (s.get("yes_price") or 0.0) > 0.7:
                yc = True
            if (s.get("no_price") or 0.0) > 0.7:
                nc = True
            tp = s.get("twap_price") or 0.0
            sp = s.get("price") or 0.0
            if tp <= 0 or sp <= 0:
                continue
            lo = max(0, i - 11)
            ps = [snaps[k]["price"] for k in range(lo, i + 1)
                  if (snaps[k].get("price") or 0.0) > 0]
            if len(ps) < 6:  # MA 预热不足
                continue
            r = s["remaining_sec"]
            if r <= 0 or r > 240:
                continue
            bucket = next((name for lo_r, hi_r, name in R_BUCKETS if lo_r < r <= hi_r), None)
            if bucket is None:
                continue
            category = ("both" if yc and nc else ("yes_only" if yc else
                        ("no_only" if nc else "none")))
            ticks.append({
                "e_start": e["start_time"], "ts": s["ts"], "r": r,
                "bucket": bucket, "regime": reg,
                "gap": sp - tp, "target": tc - tp, "theta": to - tp,
                "yes_bid": s.get("yes_price") or 0.0,
                "no_bid": s.get("no_price") or 0.0,
                "outcome": outcome, "category": category,
            })
    return ticks


def fit_cells(ticks):
    """按 (r段, regime) 格子拟合 Δtwap = α + β·gap + ε 与残差 σ。

    斜率用带截距的 OLS（slope_corr 即去均值协方差/方差），截距 α 吸收
    gap 的恒定基差（~-67$）与格子内漂移。训练样本不足的格子返回 None。
    """
    cells = {}
    for t in ticks:
        if t["regime"] is None:
            continue
        key = (t["bucket"], t["regime"])
        cells.setdefault(key, ([], []))
        cells[key][0].append(t["gap"])
        cells[key][1].append(t["target"])
    models = {}
    for key, (xs, ys) in cells.items():
        if len(xs) < MIN_TRAIN_TICKS:
            models[key] = None
            continue
        beta, corr = slope_corr(xs, ys)
        gbar = sum(xs) / len(xs)
        tbar = sum(ys) / len(ys)
        alpha = tbar - beta * gbar  # 截距：吸收基差与漂移
        resid = [y - (alpha + beta * x) for x, y in zip(xs, ys)]
        _, sigma = meanstd(resid)
        models[key] = (beta, alpha, sigma, corr, len(xs))
    return models


def evaluate(ticks, models, min_ev):
    """冻结模型下注：每 tick 计算 P̂ 与双侧可成交 EV，超过阈值记一注。"""
    bets = []
    for t in ticks:
        m = models.get((t["bucket"], t["regime"]))
        if m is None or m[2] <= 0:
            continue
        beta, alpha, sigma, _, _ = m
        if t["r"] < MIN_REM:
            continue
        if t["yes_bid"] <= 0 or t["no_bid"] <= 0:
            continue
        p_up = phi((alpha + beta * t["gap"] - t["theta"]) / sigma)
        fill_yes = 1.0 - t["no_bid"]  # 买 YES 的对侧 ask
        fill_no = 1.0 - t["yes_bid"]  # 买 NO 的对侧 ask
        ev_yes = p_up - fill_yes
        ev_no = (1.0 - p_up) - fill_no
        if max(ev_yes, ev_no) <= min_ev:
            continue
        side = "yes" if ev_yes >= ev_no else "no"
        fill = fill_yes if side == "yes" else fill_no
        win = (t["outcome"] == 0) if side == "yes" else (t["outcome"] == 1)
        bets.append({
            **t, "side": side, "fill": fill, "p_up": p_up,
            "mkt_mid": (t["yes_bid"] + 1.0 - t["no_bid"]) / 2.0,
            "pred_ev": max(ev_yes, ev_no),
            "realized": (1.0 if win else 0.0) - fill,
        })
    return bets


def first_bet_per_event(bets):
    """每事件保留最早的一注（实盘每周期最多一注）。"""
    first = {}
    for b in sorted(bets, key=lambda b: (b["e_start"], b["ts"])):
        first.setdefault(b["e_start"], b)
    return list(first.values())


def section(title):
    print()
    print("=" * 74)
    print(f"  {title}")
    print("=" * 74)


def summarize(label, bets, ev_grid):
    print(f"  {label}:")
    for tau in ev_grid:
        sel = [b for b in bets if b["pred_ev"] > tau]
        if not sel:
            print(f"    EV>{tau:.2f}: n=0")
            continue
        n_ev = len({b["e_start"] for b in sel})
        m_pred = sum(b["pred_ev"] for b in sel) / len(sel)
        m_real = sum(b["realized"] for b in sel) / len(sel)
        wins = sum(1 for b in sel if b["realized"] > 0)
        print(f"    EV>{tau:.2f}: n={len(sel):>5} (ev={n_ev:>4})  "
              f"预测EV={m_pred:+.4f}  realized={m_real:+.4f}  胜率={wins / len(sel) * 100:5.1f}%")


def main():
    parser = argparse.ArgumentParser(description="影响模型定价对照 (Phase 2 v1)")
    parser.add_argument("--data", default="../data/btc/", help="JSONL 数据目录")
    parser.add_argument("--min-rem", type=int, default=MIN_REM, help="可成交剩余秒数下限")
    args = parser.parse_args()

    events = load_events(args.data)
    if not events:
        print("无事件数据，退出。")
        return
    inject_hist_range(events)
    regime_map = build_regime_map(events)
    ticks = collect_pricing_ticks(events, regime_map)

    # train / test 按事件时间中位数对半切（与 Phase 1 第 7 节一致）
    starts = sorted({t["e_start"] for t in ticks})
    split_st = starts[len(starts) // 2]
    tr = [t for t in ticks if t["e_start"] < split_st]
    te = [t for t in ticks if t["e_start"] >= split_st]

    models = fit_cells(tr)

    # ── 1. 数据与模型概览 ──
    section("1. 数据与模型概览")
    print(f"  事件: {len(events)}  |  tick: {len(ticks)}  |  "
          f"train/test 切分点: {split_st} ({len(tr)} / {len(te)} ticks)")
    print("  格子模型 (train 拟合，Δtwap = α + β·gap):")
    print(f"  {'bucket':>8} {'regime':>7} {'n':>6} {'α':>8} {'β':>8} {'σ':>8} {'corr':>6}")
    for key in sorted(k for k, m in models.items() if m):
        beta, alpha, sigma, corr, n = models[key]
        print(f"  {key[0]:>8} {key[1]:>7} {n:>6} {alpha:>+8.2f} {beta:>+8.3f} "
              f"{sigma:>8.2f} {corr:>6.3f}")

    # ── 2. 整体评估 ──
    section("2. 整体评估（test 冻结）")
    all_bets = evaluate(te, models, 0.0)
    first_bets = first_bet_per_event(all_bets)
    summarize("per-tick 口径", all_bets, EV_GRID)
    summarize("per-event 口径（每事件首注）", first_bets, EV_GRID)

    # ── 3. 按格子 ──
    section("3. 按格子（test，EV>0 的 per-event 首注）")
    print(f"  {'bucket':>8} {'regime':>7} {'n注':>5} {'模型P̂':>7} {'市场mid':>7} "
          f"{'残差':>7} {'realized':>9}")
    for key in sorted({(b["bucket"], b["regime"]) for b in first_bets}):
        sel = [b for b in first_bets if (b["bucket"], b["regime"]) == key]
        mp = sum(b["p_up"] for b in sel) / len(sel)
        mm = sum(b["mkt_mid"] for b in sel) / len(sel)
        mr = sum(b["realized"] for b in sel) / len(sel)
        print(f"  {key[0]:>8} {key[1]:>7} {len(sel):>5} {mp:>7.3f} {mm:>7.3f} "
              f"{mp - mm:>+7.3f} {mr:>+9.4f}")

    # ── 4. 按天 ──
    section("4. 按天（test，EV>0 的 per-event 首注）")
    days = {}
    for b in first_bets:
        d = datetime.fromtimestamp(b["e_start"], tz=timezone.utc).strftime("%m-%d")
        days.setdefault(d, []).append(b)
    for d in sorted(days):
        sel = days[d]
        mr = sum(b["realized"] for b in sel) / len(sel)
        wins = sum(1 for b in sel if b["realized"] > 0)
        print(f"    {d}: n={len(sel):>4}  realized={mr:+.4f}  胜率={wins / len(sel) * 100:5.1f}%")

    # ── 5. 校准表 ──
    section("5. 校准表（test 全部可定价 tick，P̂ 十分位 vs 实际 UP 率）")
    eligible = []
    for t in te:
        m = models.get((t["bucket"], t["regime"]))
        if m is None or m[2] <= 0 or t["yes_bid"] <= 0 or t["no_bid"] <= 0:
            continue
        beta, alpha, sigma, _, _ = m
        p = phi((alpha + beta * t["gap"] - t["theta"]) / sigma)
        eligible.append((p, t["outcome"] == 0))
    if eligible:
        eligible.sort()
        n = len(eligible)
        print(f"  {'十分位':>6} {'n':>6} {'P̂均值':>7} {'实际UP率':>8} {'偏差':>7}")
        for k in range(10):
            lo, hi = n * k // 10, n * (k + 1) // 10
            seg = eligible[lo:hi]
            mp = sum(p for p, _ in seg) / len(seg)
            mu = sum(1 for _, u in seg if u) / len(seg)
            print(f"  D{k + 1:>5} {len(seg):>6} {mp:>7.3f} {mu:>8.3f} {mu - mp:>+7.3f}")

    # ── 6. 穿越类别交互 ──
    section("6. 穿越类别交互（test，EV>0 的 per-event 首注，按注单时刻类别）")
    cats = {}
    for b in first_bets:
        cats.setdefault(b["category"], []).append(b)
    for c in sorted(cats):
        sel = cats[c]
        mr = sum(b["realized"] for b in sel) / len(sel)
        wins = sum(1 for b in sel if b["realized"] > 0)
        mf = sum(b["fill"] for b in sel) / len(sel)
        print(f"    {c:>9}: n={len(sel):>4}  fill={mf:.3f}  realized={mr:+.4f}  "
              f"胜率={wins / len(sel) * 100:5.1f}%")


if __name__ == "__main__":
    main()
