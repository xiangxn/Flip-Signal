#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v4 扫尾盘策略回测（2026-09-22 用户提出）—— 尾盘快照口径。

策略（用户原话）:
  1. 当一边 ≥ 0.8 的价格时
  2. 观察 spot 与 TWAP-60 是否都在该侧一边（都 > twap_open；镜像: no 侧都 < twap_open）
  3. 都在就下注该侧

口径（"扫尾盘"的正确采样方式 —— 尾盘是**状态**，不是价格穿越事件）:
  每窗在尾盘起点取一次快照 = 「rem ≤ T 的第一个有效 tick」（T = 尾盘起点秒数）
  该 tick 上 ask 较大的一侧 = 热门侧（买入对象），price = 该侧 ask（吃单成本）
  → 每窗每 T 恰好一条观测; 价格分桶就是快照上的**实际价位**（不是穿越那一跳）
  对照: 见 §6「首次触达档位」口径（价格穿越 0.8 的瞬间买入, 发生在窗口中段）

提取门控（同 01_backtest_r1.py / Go 引擎）:
  pm 存在, book_latency_ms ≤ 300, 四字段报价齐全, rem > 0

收益口径: STAKE U/注; 成交价 = 触发侧 ask; shares = STAKE/fill
  赢 → shares − STAKE, 输 → −STAKE; 盈亏平衡胜率 = fill; EV/股 = WR − fill
  EV@中价 = WR − (bid+ask)/2 —— "不付买卖价差"的理论上界, 衡量信号本身有没有 edge

单位: 偏离量 d = sgn·(X − anchor)/anchor·1e4 bps（sgn = +1 yes / −1 no, 正 = 站在热门侧一边）
  σ = 前 ≤18 窗 |tw_close − tw_open| 均值 / anchor·1e4（同 01）

用法: python 13_tail_sweep.py [--data DIR] [--stake 2]
"""
import argparse
import datetime
import sys
from math import sqrt
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))
from v2.lib import load_events  # noqa: E402

STAKE = 2.0
MAX_LAT = 300                 # pm book_latency_ms 上限（同 01）
TS = (240, 180, 150, 120, 90, 60, 30)   # 尾盘起点候选（rem ≤ T 的第一个 tick）
MAIN_T = 120                  # 头条口径: 最后 2 分钟
P_FLOOR = 0.80                # 策略的价格门槛
PB = [(0.00, 0.70, "<0.70"), (0.70, 0.75, "[0.70,0.75)"), (0.75, 0.80, "[0.75,0.80)"),
      (0.80, 0.85, "[0.80,0.85)"), (0.85, 0.90, "[0.85,0.90)"),
      (0.90, 0.95, "[0.90,0.95)"), (0.95, 1.01, "[0.95,1.00]")]


def hist_ranges(events):
    """前 ≤18 窗 |tw_close−tw_open| 均值（σ, 需 ≥3 窗）; 每事件 start_time → σ。"""
    hr = {}
    for i, e in enumerate(events):
        prev = []
        for j in range(max(0, i - 18), i):
            o, c = events[j].get("twap_open_price"), events[j].get("twap_close_price")
            if o and c:
                prev.append(abs(c - o))
        hr[e["start_time"]] = sum(prev) / len(prev) if len(prev) >= 3 else None
    return hr


def snapshots(events, hr, T, field="ask"):
    """每窗取「rem ≤ T 的第一个有效 tick」→ 热门侧（ask 较大者）一条观测。"""
    rows = []
    for e in events:
        outcome = e.get("outcome")
        anchor = e.get("twap_open_price")
        if outcome is None or not anchor:
            continue
        t = pm = None
        for x in e.get("ticks") or []:
            p = x.get("pm") or {}
            if not p or (p.get("book_latency_ms") or 0) > MAX_LAT:
                continue
            if not all((p.get(k) or 0) > 0
                       for k in ("yes_bid", "yes_ask", "no_bid", "no_ask")):
                continue
            rem = x.get("rem")
            if rem is None or rem <= 0:
                continue
            if rem <= T:
                t, pm = x, p
                break
        if t is None:
            continue
        ya, na = pm["yes_ask"], pm["no_ask"]
        side = "yes" if ya >= na else "no"
        spot = (t.get("bin") or {}).get("price")
        twap = (t.get("twap") or {}).get("price")
        if not spot or not twap:
            continue
        sgn = 1.0 if side == "yes" else -1.0
        h = hr.get(e["start_time"])
        r = {
            "date": datetime.datetime.fromtimestamp(
                e["start_time"], datetime.timezone.utc).strftime("%Y-%m-%d"),
            "side": side,
            "fill": pm[f"{side}_{field}"],      # 触发（判定）字段
            "buy": pm[f"{side}_ask"],           # 实际成交价（吃单）
            "mid": (pm[f"{side}_bid"] + pm[f"{side}_ask"]) / 2,
            "rem": t["rem"],
            "T": T,
            "anchor": anchor,
            "hist_bps": (h / anchor * 1e4) if h else None,
            "d_spot": sgn * (spot - anchor) / anchor * 1e4,   # 同向归一（正 = 站在热门侧）
            "d_twap": sgn * (twap - anchor) / anchor * 1e4,
            "raw_spot": spot - anchor,                        # 原始美元: spot − twap_open
            "raw_twap": twap - anchor,                        # 原始美元: twap − twap_open
            "up_won": 1 if outcome == 0 else 0,               # UP 侧结算（与下注方向无关）
            "settle_won": 1 if ((outcome == 0) if side == "yes" else (outcome == 1)) else 0,
        }
        if h:
            r["s_spot"] = r["d_spot"] / r["hist_bps"]
            r["s_twap"] = r["d_twap"] / r["hist_bps"]
        # 策略三条件: 价格达标 + 两腿同向
        r["hot"] = r["buy"] >= P_FLOOR
        r["confirm"] = (r["d_spot"] > 0) and (r["d_twap"] > 0)
        r["sig"] = r["hot"] and r["confirm"]
        rows.append(r)
    return rows


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    w = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - w) / d, (c + w) / d)


def stat(rows):
    n = len(rows)
    if n == 0:
        return None
    k = sum(r["settle_won"] for r in rows)
    wr = k / n
    f = sum(r["buy"] for r in rows) / n
    mid = sum(r["mid"] for r in rows) / n
    pl = sum((STAKE / r["buy"] - STAKE) if r["settle_won"] else -STAKE for r in rows)
    lo, hi = wilson(k, n)
    return {"n": n, "k": k, "wr": wr, "fill": f, "mid": mid, "ev": wr - f,
            "evmid": wr - mid, "evm": pl / n, "pl": pl, "lo": lo, "hi": hi}


HEAD = ("  分桶            n    胜率   95%CI Wilson    成交价   中间价  平衡线"
        "   EV/股  EV@中价    EV/注      P&L")
SUB = "  " + "-" * 98


def prow(label, s, w=14):
    if s is None:
        print(f"  {label:<{w}s} {0:5d}      —")
        return
    print(f"  {label:<{w}s} {s['n']:5d} {s['wr']*100:5.1f}%  "
          f"[{s['lo']*100:4.1f},{s['hi']*100:4.1f}]     "
          f"{s['fill']:6.3f}  {s['mid']:6.3f}  {s['fill']*100:5.1f}%  "
          f"{s['ev']:+7.4f}  {s['evmid']:+7.4f}  {s['evm']:+7.3f}  {s['pl']:+8.1f}")


def in_bucket(v, lo, hi):
    return lo <= v < hi


def main():
    global STAKE
    ap = argparse.ArgumentParser(description="v4 扫尾盘策略回测（尾盘快照口径）")
    ap.add_argument("--data", default=str(BASE.parent.parent / "data" / "btc"))
    ap.add_argument("--stake", type=float, default=STAKE)
    args = ap.parse_args()
    STAKE = args.stake

    events = load_events(args.data)
    hr = hist_ranges(events)
    S = {T: snapshots(events, hr, T) for T in TS}
    main_rows = S[MAIN_T]
    ndays = len({r["date"] for r in main_rows})
    a = sorted(r["anchor"] for r in main_rows)[len(main_rows) // 2]
    sig = [r for r in main_rows if r["sig"]]
    sig_all = [r for r in main_rows if r["hot"] and r["confirm"]]

    print("=" * 102)
    print("v4 扫尾盘策略 —— 尾盘快照口径（不是价格穿越那一下）")
    print(f"数据 {args.data}   事件 {len(events)} 窗 / {ndays} 天")
    print(f"采样: 每窗取「rem ≤ T 的第一个有效 tick」= 尾盘起点快照; "
          f"该 tick 上 ask 较大的一侧 = 热门侧（买入对象）")
    print(f"条件: ① 热门侧 ask ≥ {P_FLOOR:.2f}  ② spot 与 TWAP 都在热门侧一边")
    print(f"口径: {STAKE:g}U/注, 成交价 = 热门侧 ask, EV/股 = WR − 成交价, "
          f"EV@中价 = WR − 中间价")
    print(f"锚中位 {a:.0f} → 1 bps ≈ ${a/1e4:.2f}; "
          f"σ 中位 {sorted(r['hist_bps'] for r in main_rows if r['hist_bps'])[len(main_rows)//2]:.2f} bps"
          f" ≈ ${sorted(r['hist_bps'] for r in main_rows if r['hist_bps'])[len(main_rows)//2]*a/1e4:.0f}")
    print("=" * 102)

    # ── §1 尾盘起点 T 的对照 ────────────────────────────────────
    print("\n§1 尾盘起点 T 对照 —— 同一策略在不同「尾盘起点」下的表现")
    print("  (a) 三条件全满足（热门侧 ask ≥ 0.80 + 两腿同向）:")
    print(HEAD)
    for T in TS:
        prow(f"rem≤{T}", stat([r for r in S[T] if r["sig"]]))
    print("\n  (b) 只要求热门侧 ask ≥ 0.80（不看 spot/TWAP）:")
    print(HEAD)
    for T in TS:
        prow(f"rem≤{T}", stat([r for r in S[T] if r["hot"]]))
    print("\n  (c) 快照时热门侧 ask 的分布（尾盘越晚, 价格越高 → 说明为什么必须看价分桶）:")
    for T in TS:
        rows = S[T]
        fs = sorted(r["buy"] for r in rows)
        n = len(fs)
        print(f"    rem≤{T:3d}: n={n:4d}  p10 {fs[n//10]:.3f}  中位 {fs[n//2]:.3f}  "
              f"p90 {fs[9*n//10]:.3f}   ≥0.80 占 {sum(1 for x in fs if x >= 0.8)/n*100:.0f}%"
              f"   ≥0.90 占 {sum(1 for x in fs if x >= 0.9)/n*100:.0f}%")

    # ── §2 价格分桶（核心: 0.75~0.95）──────────────────────────
    print(f"\n§2 价格分桶 —— 尾盘快照上热门侧的实际价位（样本 = 首触 rem≤T 的全部窗口）\n")
    print("  (a) 快照价格 × 尾盘起点 T —— 单元格 = 胜率%（n）:")
    hdr = "  " + " " * 13 + "".join(f"{('rem≤'+str(T)):>13s}" for T in TS)
    print(hdr)
    for lo, hi, lab in PB:
        line = f"  {lab:<13s}"
        for T in TS:
            g = [r for r in S[T] if in_bucket(r["buy"], lo, hi)]
            line += (f"{stat(g)['wr']*100:9.1f}%({len(g):3d})" if g else f"{'—':>13s}")
        print(line)
    print("\n  (b) 同表, 单元格 = EV/股（= WR − 成交价；>0 = 吃单也有正期望）:")
    print(hdr)
    for lo, hi, lab in PB:
        line = f"  {lab:<13s}"
        for T in TS:
            g = [r for r in S[T] if in_bucket(r["buy"], lo, hi)]
            line += (f"{stat(g)['ev']:+12.4f} " if g else f"{'—':>13s}")
        print(line)
    print("\n  (c) 明细（T=%.0fs 尾盘, 按快照价格）:" % MAIN_T)
    print(HEAD)
    for lo, hi, lab in PB:
        prow(lab, stat([r for r in main_rows if in_bucket(r["buy"], lo, hi)]))
    print("\n  (d) 同上但只看「两腿同向」的快照:")
    print(HEAD)
    for lo, hi, lab in PB:
        prow(lab, stat([r for r in main_rows
                        if in_bucket(r["buy"], lo, hi) and r["confirm"]]))
    print("\n  (e) 0.80 附近加密分桶（T=%.0fs, 只看 ask ∈ [0.75,0.92)）:" % MAIN_T)
    print(HEAD)
    for lo in (0.75, 0.78, 0.80, 0.82, 0.84, 0.86, 0.88, 0.90):
        prow(f"[{lo:.2f},{lo+0.02:.2f})",
             stat([r for r in main_rows if in_bucket(r["buy"], lo, lo + 0.02)]), w=12)

    # ── §3 条件 ② 的边际价值 ──────────────────────────────────
    print(f"\n§3 条件②（spot 与 TWAP 同向）的边际价值 —— T={MAIN_T}s, 热门侧 ask ≥ 0.80\n")
    print(HEAD)
    hot = [r for r in main_rows if r["hot"]]
    prow("≥0.80 全部", stat(hot))
    prow("  +两腿同向", stat([r for r in hot if r["confirm"]]))
    prow("  未同向", stat([r for r in hot if not r["confirm"]]))
    print(SUB)
    print(f"  条件②的命中率: spot 同向 {sum(1 for r in hot if r['d_spot'] > 0)/len(hot)*100:.1f}%, "
          f"twap 同向 {sum(1 for r in hot if r['d_twap'] > 0)/len(hot)*100:.1f}%, "
          f"两腿都同向 {sum(1 for r in hot if r['confirm'])/len(hot)*100:.1f}%"
          f"  → 热门侧 ≥0.8 时现货本来就在该侧, 条件②几乎恒定成立")
    for sd in ("yes", "no"):
        prow(f"  {sd.upper()} 同向", stat([r for r in hot
                                          if r["confirm"] and r["side"] == sd]))
        prow(f"  {sd.upper()} 未同向", stat([r for r in hot
                                            if not r["confirm"] and r["side"] == sd]))
        print(SUB)
    print("\n  四象限（spot 同向? × twap 同向?）:")
    print(HEAD)
    for lab, fn in (("两腿都同向", lambda r: r["d_spot"] > 0 and r["d_twap"] > 0),
                    ("仅 spot 同向", lambda r: r["d_spot"] > 0 and r["d_twap"] <= 0),
                    ("仅 twap 同向", lambda r: r["d_spot"] <= 0 and r["d_twap"] > 0),
                    ("两腿都反向", lambda r: r["d_spot"] <= 0 and r["d_twap"] <= 0)):
        prow(lab, stat([r for r in hot if fn(r)]))
    print("\n  确认门槛扫描（两腿同向偏离都 > x bps）:")
    print("  门槛       n     胜率    成交价  EV/股   EV@中价    P&L")
    for x in (-20, -5, 0, 1, 2, 5, 10, 20):
        s = stat([r for r in hot if r["d_spot"] > x and r["d_twap"] > x])
        if s:
            print(f"  >{x:3d}bps  {s['n']:5d}  {s['wr']*100:5.1f}%  {s['fill']:6.3f}  "
                  f"{s['ev']:+7.4f}  {s['evmid']:+7.4f}  {s['pl']:+8.1f}")

    # ── §4 spot−twap_open / twap−twap_open 的**值**分桶 ────────
    print(f"\n§4 spot−twap_open / twap−twap_open 的值 → 胜率"
          f"（T={MAIN_T}s 尾盘快照, 热门侧 ask ≥ 0.80, n={len(hot)}）")
    print(f"  两个量都换算成**美元原值**（= sgn·(X − anchor)/anchor·1e4 × anchor/1e4）; "
          f"σ 中位 {sorted(r['hist_bps'] for r in hot if r['hist_bps'])[len(hot)//2]:.1f} bps"
          f" ≈ ${sorted(r['hist_bps'] for r in hot if r['hist_bps'])[len(hot)//2]*a/1e4:.0f}"
          f" = 1σ 参考\n")
    DOL = lambda r, k: r[k] / 1e4 * r["anchor"]  # noqa: E731  bps → 美元
    for nm, key, edges in (("spot − twap_open", "d_spot", [0, 20, 50, 100, 200]),
                           ("twap − twap_open", "d_twap", [0, 10, 25, 50, 100])):
        print(f"  (a) {nm}（美元; 正 = 站在下注侧一边; 负 = 反对下注侧）:")
        print(HEAD)
        for lo, hi, lab in ([(-1e9, edges[0], f"<{edges[0]}")]
                            + [(edges[i], edges[i+1], f"[{edges[i]},{edges[i+1]})")
                               for i in range(len(edges) - 1)]
                            + [(edges[-1], 1e9, f"≥{edges[-1]}")]):
            prow(lab, stat([r for r in hot if in_bucket(DOL(r, key), lo, hi)]))
        print(f"    按侧拆开（同向偏离 > 0 的占比 / 两侧胜率）:")
        for sd in ("yes", "no"):
            pos = [r for r in hot if r["side"] == sd and DOL(r, key) > 0]
            neg = [r for r in hot if r["side"] == sd and DOL(r, key) <= 0]
            sp, sn = stat(pos), stat(neg)
            print(f"      {sd.upper()}: {nm} > 0 占 {len(pos)/(len(pos)+len(neg))*100:.1f}%  "
                  + (f"正侧 n={sp['n']} WR {sp['wr']*100:.1f}% EV {sp['ev']:+.4f}/股" if sp else "")
                  + (f"   负侧 n={sn['n']} WR {sn['wr']*100:.1f}% EV {sn['ev']:+.4f}/股" if sn else ""))
        print()
    print("  (b) 二维: spot−twap_open × twap−twap_open（美元, 都是同向归一后）→ 热门侧胜率%(n):")
    se, te = [0, 50, 150], [0, 40, 100]
    print("  " + " " * 18 + "".join(f"{f'twap∈[{te[i]},{te[i+1]})':>18s}"
                                    for i in range(len(te) - 1)) + f"{'≥'+str(te[-1]):>18s}")
    for i in range(len(se) + 1):
        sl = f"<{se[0]}" if i == 0 else (f"≥{se[-1]}" if i == len(se)
                                         else f"[{se[i-1]},{se[i]})")
        line = f"  spot {sl:<12s}"
        for j in range(len(te) + 1):
            lo_t = -1e9 if j == 0 else te[j - 1]
            hi_t = 1e9 if j == len(te) else te[j]
            lo_s = -1e9 if i == 0 else se[i - 1]
            hi_s = 1e9 if i == len(se) else se[i]
            g = [r for r in hot if in_bucket(DOL(r, "d_spot"), lo_s, hi_s)
                 and in_bucket(DOL(r, "d_twap"), lo_t, hi_t)]
            line += (f"{stat(g)['wr']*100:11.0f}%({len(g):4d})" if g else f"{'—':>18s}")
        print(line)
    print("\n  (c) 不限价格门 —— 全部尾盘快照（n=%d, 不筛 0.80），看两个量本身的预测力。"
          % len(main_rows))
    print("      押注方向恒为「下注侧」（快照上 ask 较大的一侧）; 下方 = 两个量同向归一后的美元值:")
    print(HEAD)
    for nm, key, edges in (("spot−twap_open", "d_spot", [0, 20, 50, 100, 200]),
                           ("twap−twap_open", "d_twap", [0, 10, 25, 50, 100])):
        for lo, hi, lab in ([(-1e9, edges[0], f"<{edges[0]}")]
                            + [(edges[i], edges[i + 1], f"[{edges[i]},{edges[i+1]})")
                               for i in range(len(edges) - 1)]
                            + [(edges[-1], 1e9, f"≥{edges[-1]}")]):
            prow(f"{nm.split('−')[0]} {lab}", stat(
                [r for r in main_rows if in_bucket(DOL(r, key), lo, hi)]))
        print(SUB)
    print("  (d) 无条件口径 —— 恒看 UP 侧结算（与下注方向无关）→ P(UP 赢), 原始值分桶:")
    print("  分桶（美元）      n    P(UP赢)   [95%CI]      （≈50% = 该量无预测力）")
    for nm, key, edges in (("spot − twap_open", "raw_spot", [0, 20, 50, 100, 200]),
                           ("twap − twap_open", "raw_twap", [0, 10, 25, 50, 100])):
        print(f"  ── {nm} ──")
        for lo, hi, lab in ([(-1e9, edges[0], f"<{edges[0]}")]
                            + [(edges[i], edges[i + 1], f"[{edges[i]},{edges[i+1]})")
                               for i in range(len(edges) - 1)]
                            + [(edges[-1], 1e9, f"≥{edges[-1]}")]):
            sel = [r for r in main_rows if in_bucket(r[key], lo, hi)]
            if not sel:
                print(f"  {lab:<16s} {0:5d}      —")
                continue
            k = sum(r["up_won"] for r in sel)
            l2, h2 = wilson(k, len(sel))
            print(f"  {lab:<16s} {len(sel):5d}   {k/len(sel)*100:5.1f}%   "
                  f"[{l2*100:4.1f},{h2*100:4.1f}]")

    # ── §5 交叉表: 价格 × 尾盘起点 ────────────────────────────
    print(f"\n§5 交叉表: 快照价格 × 尾盘起点 —— 单元格 = 胜率%（n），只看 {P_FLOOR:.2f} 以上\n")
    print(hdr)
    for lo, hi, lab in PB[3:]:
        line = f"  {lab:<13s}"
        for T in TS:
            g = [r for r in S[T] if in_bucket(r["buy"], lo, hi) and r["confirm"]]
            line += (f"{stat(g)['wr']*100:9.1f}%({len(g):3d})" if g else f"{'—':>13s}")
        print(line)

    # ── §6 对照: 首次触达口径（价格穿越 0.80 的瞬间买入）──────
    print("\n§6 对照 —— 「首次触达」口径（价格穿越 0.80 那一跳就买, 不看 rem）")
    print("  （用户在 2026-09-22 指出这不是扫尾盘: 穿越发生在窗口中段, 中位 rem 211s）\n")
    ft = []
    for e in events:
        outcome, anchor = e.get("outcome"), e.get("twap_open_price")
        if outcome is None or not anchor:
            continue
        for x in e.get("ticks") or []:
            p = x.get("pm") or {}
            if not p or (p.get("book_latency_ms") or 0) > MAX_LAT:
                continue
            if not all((p.get(k) or 0) > 0
                       for k in ("yes_bid", "yes_ask", "no_bid", "no_ask")):
                continue
            rem = x.get("rem")
            if rem is None or rem <= 0:
                continue
            if p["yes_ask"] >= P_FLOOR or p["no_ask"] >= P_FLOOR:
                side = "yes" if p["yes_ask"] >= p["no_ask"] else "no"
                spot = (x.get("bin") or {}).get("price")
                twap = (x.get("twap") or {}).get("price")
                if spot and twap:
                    sgn = 1.0 if side == "yes" else -1.0
                    ft.append({
                        "side": side, "buy": p[f"{side}_ask"],
                        "mid": (p[f"{side}_bid"] + p[f"{side}_ask"]) / 2,
                        "rem": rem,
                        "d_spot": sgn * (spot - anchor) / anchor * 1e4,
                        "d_twap": sgn * (twap - anchor) / anchor * 1e4,
                        "settle_won": 1 if ((outcome == 0) if side == "yes"
                                            else (outcome == 1)) else 0,
                        "confirm": (sgn * (spot - anchor) > 0) and (sgn * (twap - anchor) > 0),
                    })
                break
    print(HEAD)
    prow("首次触达≥0.80", stat(ft))
    prow("  +两腿同向", stat([r for r in ft if r["confirm"]]))
    print(f"  触发时刻 rem: 中位 {sorted(r['rem'] for r in ft)[len(ft)//2]}s "
          f"（尾盘 ≤120s 占 {sum(1 for r in ft if r['rem'] <= 120)/len(ft)*100:.0f}%）")

    # ────────────────────────────────────────────────────────────
    # §7 用 spot−twap_open / twap−twap_open 的**美元值**当过滤器
    #    用户口径: 不是一到 ≥0.80 就下注, 要看这两个差值到哪一档胜率最高
    # ────────────────────────────────────────────────────────────
    import random

    def pl_of(rows):
        return sum((STAKE / r["buy"] - STAKE) if r["settle_won"] else -STAKE
                   for r in rows)

    def nday(rows):
        d: dict = {}
        for r in rows:
            d[r["date"]] = d.get(r["date"], 0.0) + (
                (STAKE / r["buy"] - STAKE) if r["settle_won"] else -STAKE)
        return sum(1 for v in d.values() if v > 0), len(d)

    def boot_days(rows, B=2000, seed=42):
        """日级 bootstrap（按日期重采样）→ P&L 的 95% 区间。"""
        d: dict = {}
        for r in rows:
            d.setdefault(r["date"], []).append(r)
        keys = list(d)
        rnd = random.Random(seed)
        v = sorted(sum(pl_of(d[keys[rnd.randrange(len(keys))]])
                       for _ in keys) for _ in range(B))
        return v[int(0.025 * B)], v[int(0.975 * B)]

    Q = (("raw_spot", "spot−twap_open"), ("raw_twap", "twap−twap_open"),
         ("gap", "spot−twap（两者之差/基差）"))

    def qv(r, name):
        """按下注侧符号归一的美元值（no 侧取负; 正 = 站在下注侧一边）。"""
        sg = 1.0 if r["side"] == "yes" else -1.0
        if name == "gap":
            return sg * (r["raw_spot"] - r["raw_twap"])
        return sg * r[name]

    for T in (120, 60):
        hot = [r for r in S[T] if r["hot"]]
        hb = sorted(x["hist_bps"] for x in hot if x["hist_bps"])
        anc = sorted(x["anchor"] for x in hot)[len(hot) // 2]
        SIG_REF = hb[len(hb) // 2] * anc / 1e4   # σ 中位 → 美元
        print(f"\n§7 过滤器 = 这两个量的美元值（T={T}s 尾盘, 热门侧 ask ≥ 0.80, "
              f"n={len(hot)}）")
        print(f"  样本内 σ 中位 {hb[len(hb)//2]:.1f} bps ≈ {SIG_REF:.0f} 美元"
              f" → 下面每档以 σ 为单位标注")
        for key, nm in Q:
            print(f"\n  (a) 只要求 {nm} ≥ 阈值（美元）:")
            for th in (0, 20, 40, 60, 80, 100, 150, 200, 300):
                g = [r for r in hot if qv(r, key) >= th]
                s = stat(g)
                if s is None or s["n"] < 10:
                    continue
                nd, tot = nday(g)
                bl, bh = boot_days(g)
                print(f"  ≥{th:>3d}美元({th/SIG_REF:4.2f}σ) n={s['n']:4d} "
                      f"WR {s['wr']*100:5.1f}% [{s['lo']*100:4.1f},{s['hi']*100:4.1f}] "
                      f"成交价 {s['fill']:.3f}  EV {s['ev']:+.4f}/股  "
                      f"EV@中价 {s['evmid']:+.4f}  EV/注 {s['evm']:+.3f}  "
                      f"P&L {s['pl']:+7.1f}U  日正 {nd}/{tot} "
                      f"日级95%CI[{bl:+.0f},{bh:+.0f}]U")
        # 二维阈值网格: 两腿各自 ≥ 阈值
        print("\n  (b) 二维网格: spot−twap_open × twap−twap_open 双阈值"
              "（单元格 = 胜率% / EV/股, n<30 标 *）")
        sths = (0, 50, 100, 150, 200, 300)
        tths = (0, 25, 50, 75, 100, 150)
        print("    " + "spot\\twap".ljust(12) +
              "".join(f"≥{t:>4d}美元".rjust(15) for t in tths))
        for x in sths:
            row = f"    ≥{x:>4d}美元".ljust(12)
            for y in tths:
                g = [r for r in hot
                     if qv(r, "raw_spot") >= x and qv(r, "raw_twap") >= y]
                s = stat(g)
                if s is None or s["n"] < 10:
                    row += f"{'—':>15s}"
                    continue
                mk = "*" if s["n"] < 30 else " "
                row += f"{s['wr']*100:5.1f}%/{s['ev']:+.3f}{mk}".rjust(15)
            print(row)
        # 控价检验: 固定价格带内过滤, 看过滤器是否有独立信息
        print("\n  (c) 控价检验 —— 同一价格带内按 spot−twap_open 分档"
              "（若 EV 仍随档上升, 过滤器才有独立 alpha）:")
        for plo, phi in ((0.80, 0.90), (0.90, 0.95), (0.95, 1.01)):
            band = [r for r in hot if plo <= r["buy"] < phi]
            if len(band) < 30:
                continue
            s0 = stat(band)
            print(f"    价格 [{plo:.2f},{phi:.2f}) 全部: n={s0['n']:4d} "
                  f"WR {s0['wr']*100:.1f}% 成交价 {s0['fill']:.3f} "
                  f"EV {s0['ev']:+.4f}/股  P&L {s0['pl']:+.1f}U")
            for th in (0, 50, 100, 200):
                g = [r for r in band if qv(r, "raw_spot") >= th]
                s = stat(g)
                if s is None or s["n"] < 20:
                    continue
                print(f"      + spot≥{th:>3d}美元(≥{th/SIG_REF:.1f}σ): "
                      f"n={s['n']:4d} WR {s['wr']*100:.1f}% 成交价 {s['fill']:.3f} "
                      f"EV {s['ev']:+.4f}/股  EV@中价 {s['evmid']:+.4f}  "
                      f"P&L {s['pl']:+.1f}U")
            for th in (0, 25, 50, 100):
                g = [r for r in band if qv(r, "raw_twap") >= th]
                s = stat(g)
                if s is None or s["n"] < 20:
                    continue
                print(f"      + twap≥{th:>3d}美元(≥{th/SIG_REF:.1f}σ): "
                      f"n={s['n']:4d} WR {s['wr']*100:.1f}% 成交价 {s['fill']:.3f} "
                      f"EV {s['ev']:+.4f}/股  EV@中价 {s['evmid']:+.4f}  "
                      f"P&L {s['pl']:+.1f}U")


if __name__ == "__main__":
    main()
