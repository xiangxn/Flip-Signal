#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""双边 0.40 挂单探查（用户 2026-10-08 提问的分析脚本）。

两个问题:
  ① data/btc 与 data/events（cmd/tail 采集）里, UP(Yes)/DOWN(No) 两侧 ask
     **都到过 ≤0.40** 的窗口占比是多少?（阈值网格 0.30~0.50 一并给）
  ② 如果窗口开局在两侧**同时挂 0.40 的买单**、不撤单挂到闭市, 会是个什么情况?

口径:
  - "到过 X" = 该侧 ask ∈ (0, X] 出现在某个 1s 采样上。
    ⚠️ ask == 0 是**整侧撤空**（决策 #21）——没有卖单, 挂着的买单吃不到, 不算。
  - 成交语义: 挂在 0.40 的买单被卖方砸中 ⇒ **成交价 = 0.40**（maker 价; 本市场
    takerOnly 计费、maker 费 0, 见决策 #35）。1s 采样看不到队列位置 ⇒ 这是**上界**;
    另用 trades 里「主动卖成交 ≤0.40」的打印做**下界**交叉验证（§5）。
  - 结算 = 行内官方 outcome（0=Up 1=Down）; P&L 按 **1 股/边** 计（×5 = 2U/边,
    ×25 = 10U/边）。两边的挂单都是被动成交（maker）, 不计 taker 费。
  - 整窗空簿（两侧 ask 全程为 0, 订阅缺失类）不计入任何分母, §0 单独报数。

用法:
    python/venv/bin/python python/v4/46_dual_side_04.py [--dirs data/btc data/events]
"""

import argparse
import glob
import json
import os
import re
import statistics
import sys
from array import array
from collections import defaultdict

GRID = [0.30, 0.35, 0.40, 0.45, 0.50]
INF = float("inf")
REM_BUCKETS = [(0, 60), (60, 120), (120, 180), (180, 240), (240, 301)]
DATE_RE = re.compile(r"events_(\d{4}-\d{2}-\d{2})\.jsonl$")


def load_windows(dirs):
    """逐窗抽取轻量记录（逐秒 ask 序列用 array('f') 存, 供挂单时点变体重扫）。"""
    recs, bad, empty = [], 0, 0
    for d in dirs:
        src = os.path.basename(d.rstrip("/"))
        for path in sorted(glob.glob(os.path.join(d, "events_*.jsonl"))):
            m = DATE_RE.search(os.path.basename(path))
            date = m.group(1) if m else os.path.basename(path)
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        bad += 1  # 活文件尾行的半截写
                        continue
                    ticks = r.get("ticks") or []
                    if not ticks:
                        continue
                    rems = array("h")
                    # ⚠️ 必须 'd'（float64）: float32 存不下 0.40（最近的是
                    # 0.4000000059604645）, `a <= 0.40` 会把「恰好 0.40」漏判掉。
                    asks = {"yes": array("d"), "no": array("d")}
                    min_ask = {"yes": INF, "no": INF}
                    min_asksum, max_bidsum = INF, -INF
                    both_le04_ticks = 0
                    for t in ticks:
                        pm = t.get("pm") or {}
                        ya, na = pm.get("yes_ask", 0.0), pm.get("no_ask", 0.0)
                        yb, nb = pm.get("yes_bid", 0.0), pm.get("no_bid", 0.0)
                        rems.append(t["rem"])
                        asks["yes"].append(ya)
                        asks["no"].append(na)
                        if ya > 0 and ya < min_ask["yes"]:
                            min_ask["yes"] = ya
                        if na > 0 and na < min_ask["no"]:
                            min_ask["no"] = na
                        if ya > 0 and na > 0:
                            min_asksum = min(min_asksum, ya + na)
                        if yb > 0 and nb > 0:
                            max_bidsum = max(max_bidsum, yb + nb)
                        if 0 < ya <= 0.40 and 0 < na <= 0.40:
                            both_le04_ticks += 1
                    if min_ask["yes"] == INF and min_ask["no"] == INF:
                        empty += 1  # 整窗无任何卖单（订阅缺失类）—— 不进分母
                        continue
                    # 成交打印低水位（主动卖的那个秒桶里 last_price/vwap 的较小者）
                    min_sell_print = {"yes": INF, "no": INF}
                    for tr in r.get("trades") or []:
                        if (tr.get("n_sell") or 0) <= 0:
                            continue
                        tok = (tr.get("token") or "").upper()
                        side = "yes" if tok == "YES" else "no" if tok == "NO" else None
                        if side is None:
                            continue
                        low = min(tr.get("last_price") or INF, tr.get("vwap") or INF)
                        if low < min_sell_print[side]:
                            min_sell_print[side] = low
                    recs.append({
                        "src": src, "date": date,
                        "start": r["start_time"], "outcome": r["outcome"],
                        "yes_win": r["outcome"] == 0,
                        "n_ticks": len(ticks),
                        "rems": rems, "asks": asks,
                        "min_ask": min_ask, "min_sell_print": min_sell_print,
                        "min_asksum": min_asksum, "max_bidsum": max_bidsum,
                        "both_le04_ticks": both_le04_ticks,
                    })
    return recs, bad, empty


def pct(a, b):
    return f"{100.0 * a / b:.1f}%" if b else "—"


def fill_rem(rec, side, X, r_lo=0, r_hi=10**9):
    """挂单在 rem∈(r_lo, r_hi] 有效时, 该侧买 X 的成交时点（首个有效 tick 且
    ask∈(0,X]）。返回该 tick 的 rem, 无成交返 None。"""
    rems, asks = rec["rems"], rec["asks"][side]
    for i, rem in enumerate(rems):
        if rem > r_hi:
            continue
        if rem <= r_lo:
            break  # rem 单调递减（极少数乱序由排序兜底见下）
        a = asks[i]
        if 0 < a <= X:
            return rem
    return None


def sim_window(rec, X, r_lo=0, r_hi=10**9):
    """同时挂 X 买单: (成交边集合, 每股每边 P&L)。"""
    filled = set()
    for side in ("yes", "no"):
        if fill_rem(rec, side, X, r_lo, r_hi) is not None:
            filled.add(side)
    cost = X * len(filled)
    payout = sum(1.0 for s in filled if (s == "yes") == rec["yes_win"])
    return filled, payout - cost


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dirs", nargs="+", default=["data/btc", "data/events"])
    args = ap.parse_args()

    recs, bad, empty = load_windows(args.dirs)
    n = len(recs)

    # ---------- §0 自检 ----------
    seen, dup = set(), 0
    for r in recs:
        if r["start"] in seen:
            dup += 1
        seen.add(r["start"])
    win_days = sorted({r["date"] for r in recs})
    n_yes = sum(1 for r in recs if r["yes_win"])
    print("=" * 84)
    print("§0 自检")
    print(f"  有效窗口 = {n}（重复 {dup}；整窗空簿剔除 {empty}）  坏行 = {bad}")
    print(f"  覆盖 {len(win_days)} 天: {win_days[0]} ~ {win_days[-1]}")
    print(f"  outcome: Yes 赢 {n_yes} ({pct(n_yes, n)}) / No 赢 {n - n_yes}")
    print(f"  tick 数中位 = {statistics.median(r['n_ticks'] for r in recs)}")
    print("  两源: " + "  ".join(
        f"{s}={sum(1 for r in recs if r['src'] == s)}" for s in sorted({r['src'] for r in recs})))

    # ---------- §1 到过阈值的占比 ----------
    print("\n" + "=" * 84)
    print("§1 「某侧 ask 到过阈值」的窗口占比（ask == 0 整侧撤空不算）")
    print(f"{'阈值X':>6} | {'至少一边到过':>13} | {'Yes 到过':>12} | {'No 到过':>12} | "
          f"{'两边都到过':>11} | {'两边都过(同时tick)':>16}")
    for X in GRID:
        y = sum(1 for r in recs if r["min_ask"]["yes"] <= X)
        nn = sum(1 for r in recs if r["min_ask"]["no"] <= X)
        any1 = sum(1 for r in recs if min(r["min_ask"]["yes"], r["min_ask"]["no"]) <= X)
        both = sum(1 for r in recs if r["min_ask"]["yes"] <= X and r["min_ask"]["no"] <= X)
        tb = (f"{sum(r['both_le04_ticks'] for r in recs)} 次" if X == 0.40 else "—")
        print(f"{X:>6.2f} | {any1:>5} ({pct(any1, n):>5}) | {y:>4} ({pct(y, n):>5}) | "
              f"{nn:>4} ({pct(nn, n):>5}) | {both:>4} ({pct(both, n):>5}) | {tb:>16}")

    print("\n  0.40 阈值分源:")
    for s in sorted({r["src"] for r in recs}):
        sub = [r for r in recs if r["src"] == s]
        y = sum(1 for r in sub if r["min_ask"]["yes"] <= 0.40)
        nn = sum(1 for r in sub if r["min_ask"]["no"] <= 0.40)
        both = sum(1 for r in sub if r["min_ask"]["yes"] <= 0.40 and r["min_ask"]["no"] <= 0.40)
        one = sum(1 for r in sub if min(r["min_ask"]["yes"], r["min_ask"]["no"]) <= 0.40)
        print(f"    {s:>7}: n={len(sub):>4}  至少一边 {one} ({pct(one, len(sub))})  "
              f"Yes {pct(y, len(sub))}  No {pct(nn, len(sub))}  两边都到过 {both} ({pct(both, len(sub))})")

    sums = [r["min_asksum"] for r in recs if r["min_asksum"] < INF]
    below1 = sum(1 for s in sums if s < 1.0)
    below08 = sum(1 for s in sums if s < 0.8)
    print(f"\n  两侧 ask 之和（逐 tick 最小）: 中位 {statistics.median(sums):.3f} / "
          f"p01 {sorted(sums)[len(sums)//100]:.3f} / 最小 {min(sums):.3f}")
    print(f"  ask 和 < 1.0 的窗口 {below1} ({pct(below1, len(sums))})；"
          f"< 0.8（同一 tick 两边都 ≤0.4 = 瞬时套利）的窗口 {below08}")
    for lvl in (0.99, 0.97, 0.95, 0.90):
        c = sum(1 for s in sums if s <= lvl)
        print(f"    ask 和 ≤ {lvl:.2f}: {c:>4} 窗 ({pct(c, len(sums))})", end="")
        if c:
            gain = sum(1.0 - s for s in sums if s <= lvl)
            print(f"  （若能在该 tick 吃下两边, 合计锁定 {gain:.2f}/股）")
        else:
            print()

    # ---------- §2 首次触底时点 ----------
    print("\n" + "=" * 84)
    print("§2 0.40 首次触底的时点分布（rem 秒）")
    for side in ("yes", "no"):
        rems = [r0 for r in recs
                if (r0 := fill_rem(r, side, 0.40)) is not None]
        hist = "  ".join(f"{lo}~{hi}:{sum(1 for x in rems if lo < x <= hi)}" for lo, hi in REM_BUCKETS)
        print(f"  {side:>3} 触底 {len(rems):>4} 窗, 首触 rem 中位 {statistics.median(rems):.0f} | {hist}")
    both = [(r, fill_rem(r, "yes", 0.40), fill_rem(r, "no", 0.40)) for r in recs]
    both = [(r, ry, rn) for r, ry, rn in both if ry is not None and rn is not None]
    gaps = [abs(ry - rn) for _, ry, rn in both]
    order = defaultdict(int)
    for _, ry, rn in both:
        order["同 tick" if ry == rn else ("yes 先" if ry > rn else "no 先")] += 1
    print(f"  两边都触底 {len(both)} 窗: " + " / ".join(f"{k} {v}" for k, v in order.items())
          + f"  |  两次触底时间差: 中位 {statistics.median(gaps):.0f}s, "
          f"p25 {sorted(gaps)[len(gaps)//4]:.0f}s, p75 {sorted(gaps)[3*len(gaps)//4]:.0f}s")

    # 开盘状态（解释为什么触底来得这么早）
    ft = [r["asks"]["yes"][0] for r in recs if r["asks"]["yes"][0] > 0]
    ft_skew = sum(1 for r in recs
                  if min([a for s in ("yes", "no") if (a := r["asks"][s][0]) > 0] or [9]) <= 0.40)
    fts = sorted(ft)
    print(f"  首 tick 的 yes_ask: 中位 {statistics.median(ft):.3f} "
          f"(p25 {fts[len(fts)//4]:.3f} / p75 {fts[3*len(fts)//4]:.3f})"
          f" —— 开盘即一侧 ≤0.40 的窗口 {ft_skew} ({pct(ft_skew, n)})")

    # ---------- §3 双边 0.40 挂单反事实 ----------
    print("\n" + "=" * 84)
    print("§3 开局两侧同时挂 0.40 买单（不撤，1 股/边）→ 结局分类")
    cats = defaultdict(lambda: [0, 0.0])
    day_pnl, day_cnt = defaultdict(float), defaultdict(int)
    for r in recs:
        filled, pnl = sim_window(r, 0.40)
        if len(filled) == 2:
            k = "两边都成交（锁定 +0.20）"
        elif len(filled) == 1:
            s = next(iter(filled))
            won = (s == "yes") == r["yes_win"]
            k = f"只{'Yes' if s == 'yes' else 'No'}成交·{'赢' if won else '输'}"
        else:
            k = "无成交"
        cats[k][0] += 1
        cats[k][1] += pnl
        if filled:
            day_pnl[r["date"]] += pnl
            day_cnt[r["date"]] += 1
    total = 0.0
    print(f"  {'分类':<26} {'窗数':>5} {'占比':>7} {'每窗P&L':>9} {'合计P&L':>10}")
    for k in sorted(cats):
        c, s = cats[k]
        total += s
        print(f"  {k:<26} {c:>5} {pct(c, n):>7} {s / c:>+9.3f} {s:>+10.2f}")
    print(f"  {'合计':<26} {n:>5} {'100%':>7} {total / n:>+9.4f} {total:>+10.2f}")
    print(f"     1 股/边 → ×5 = 2U/边（{total / n * 5:+.3f} U/窗）; ×25 = 10U/边（{total / n * 25:+.3f} U/窗）")
    print(f"  ⚠️ 无成交 {cats['无成交'][0]} 窗不出手（不占资金）；"
          f"「只一边成交」那批里最终赢的只有 "
          f"{cats.get('只Yes成交·赢', [0])[0] + cats.get('只No成交·赢', [0])[0]} 窗")

    print("\n  单侧触底窗口里，触底边最终赢的次数（追的是不是输家）:")
    for X in GRID:
        single_won = single_cnt = 0
        for r in recs:
            y, nn = r["min_ask"]["yes"] <= X, r["min_ask"]["no"] <= X
            if y != nn:  # 只有一边触底
                single_cnt += 1
                s = "yes" if y else "no"
                single_won += int((s == "yes") == r["yes_win"])
        if single_cnt:
            print(f"    X={X:.2f}: 单侧触底 {single_cnt:>4} 窗, 触底边最终赢 {single_won:>3} "
                  f"({pct(single_won, single_cnt)})")

    print("\n  对照: 只挂一边（无条件买被砸到 ≤X 的那侧, 每股, 全程）:")
    print(f"  {'X':>5} {'触底边次':>8} {'该边最终赢':>9} {'WR':>7} {'单边EV/股':>10}")
    for X in GRID:
        cnt = won = 0
        for r in recs:
            for s in ("yes", "no"):
                if r["min_ask"][s] <= X:
                    cnt += 1
                    won += int((s == "yes") == r["yes_win"])
        ev = (won / cnt) * (1 - X) - (1 - won / cnt) * X
        print(f"  {X:>5.2f} {cnt:>8} {won:>9} {pct(won, cnt):>7} {ev:>+10.4f}")

    print("\n  阈值网格反事实（开局挂、全程有效，1 股/边）:")
    print(f"  {'X':>5} {'两边都成交':>10} {'只一边成交':>10} {'无成交':>7} {'EV/窗':>9} "
          f"{'2U/边·U/窗':>11}")
    for X in GRID:
        c2 = c1 = c0 = 0
        tot = 0.0
        for r in recs:
            filled, pnl = sim_window(r, X)
            tot += pnl
            if len(filled) == 2:
                c2 += 1
            elif len(filled) == 1:
                c1 += 1
            else:
                c0 += 1
        note = "  ← +0.2 锁定" if X == 0.40 else ""
        print(f"  {X:>5.2f} {c2:>5} ({pct(c2, n):>5}) {c1:>5} ({pct(c1, n):>5}) {c0:>7} "
              f"{tot / n:>+9.4f} {tot / n * 5:>+11.3f}{note}")

    print("\n  逐日（0.40，1 股/边；只列有成交窗的天）:")
    for d in sorted(day_pnl):
        print(f"    {d}  窗 {day_cnt[d]:>3}  P&L {day_pnl[d]:>+8.2f}")

    # ---------- §4 挂单起止时点变体 ----------
    print("\n" + "=" * 84)
    print("§4 挂单时点变体（0.40，1 股/边）: 「开盘后 T 秒才挂」/「到点撤单」")
    variants = [
        ("全程（不撤）", 0, 10**9),
        ("开盘后 10s 才挂", 0, 290),
        ("开盘后 30s 才挂", 0, 270),
        ("开盘后 60s 才挂", 0, 240),
        ("只 rem>180（flip 时间腿口径，到点撤）", 180, 10**9),
        ("只 rem≤180 持有（T=180 才挂）", 0, 180),
        ("只 rem≤150 持有（T=150 才挂）", 0, 150),
        ("只 rem≤60 持有（T=60 才挂）", 0, 60),
    ]
    print(f"  {'变体':<34} {'有成交窗':>7} {'两边都成交':>8} {'EV/窗':>9} {'合计P&L':>10}")
    for name, lo, hi in variants:
        tot = 0.0
        nfill = nboth = 0
        for r in recs:
            filled, pnl = sim_window(r, 0.40, lo, hi)
            tot += pnl
            nfill += int(len(filled) > 0)
            nboth += int(len(filled) == 2)
        print(f"  {name:<34} {nfill:>7} {nboth:>8} {tot / n:>+9.4f} {tot:>+10.2f}")

    # ---------- §5 trades 交叉验证 ----------
    print("\n" + "=" * 84)
    print("§5 交叉验证: ask 触底 ≤0.40 vs 真实成交打印（n_sell>0 的秒桶, last/vwap 低水位）")
    for s in ("yes", "no"):
        touch = [r for r in recs if r["min_ask"][s] <= 0.40]
        sell = [r for r in touch if r["min_sell_print"][s] <= 0.405]
        print(f"  {s:>3}: ask 触底 {len(touch):>4} 窗 → 其中主动卖打印 ≤0.40 的 {len(sell):>4} "
              f"({pct(len(sell), len(touch))})  ← 触底有真实卖流的比例")
    both_sell = sum(1 for r in recs
                    if r["min_sell_print"]["yes"] <= 0.405 and r["min_sell_print"]["no"] <= 0.405)
    both_touch = sum(1 for r in recs
                     if r["min_ask"]["yes"] <= 0.40 and r["min_ask"]["no"] <= 0.40)
    print(f"  两侧都有主动卖打印 ≤0.40: {both_sell} 窗 ({pct(both_sell, n)})"
          f"  vs 两侧 ask 都触底 {both_touch} 窗 ({pct(both_touch, n)})")
    print("  ⚠️ 打印是**下界**（1s 秒桶看不到队列位置与瞬时成交）; ask 触底是**上界**。")


if __name__ == "__main__":
    main()
