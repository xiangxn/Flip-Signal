#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""尾盘流动性探针：热门侧 ask 的**可成交数量**随 rem / 价位 / 确定性如何变化（2026-09-23）

用户提出的问题（原话）:
  「如果热门侧的订单簿已经为空了，这种在真实市场很常见，所以一定要分清是有 0.99
   可以吃，还是根本没有挂单可吃了」
  「不是你看到的回测数据那样，一旦确定性高，如 0.99 后，一会儿就不会有人提供流动性了」

背景：13/15/16 的回测**只用价格、不看数量**——快照成交价 0.9846、其中 82% 是恰好
0.99，但这些「0.99 的单子」在真实市场到底有量可吃、还是只是一个没人接的挂价，
回测无法回答（36.93U 里 63.6% 的 P&L 来自 0.99 档）。本探针用数据里**已有但从未用过的**
数量字段回答它。

字段语义（本探针 §A 实证，来自 v3 分支 internal/collect/book_utils.go:43「前 5 档数量之和」）:
  `*_top5` = 该侧盘口**前 5 档数量之和（股）**, 不是价格。
  PM 是**单一镜像簿**, 只有两个独立报价: 卖 YES@p ≡ 买 NO@(1−p)。故恒有
    yes_ask_top5 ≡ no_bid_top5   (同一个报价的两面)
    yes_bid_top5 ≡ no_ask_top5
  ⇒ 热门侧 ask 的可用量 = 该侧 `ask_top5`。
  ⚠️ 关键不对称: **价位 0.99 时 top5 就是「0.99 这唯一价位的全部深度」**
  （没有更低价的档位可加），而 0.85 的 top5 含 5 档、是「以 0.85 成交的可用量」的**上界**。
  故 0.99 档的测量是精确的，其它价位偏乐观。

用法: python/venv/bin/python python/v4/17_tail_liquidity_probe.py
"""
import sys
import statistics
import collections
import datetime
import importlib.util
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))
from v2.lib import load_events                                    # noqa: E402

MAX_LAT = 300
FIELDS = ("yes_bid", "yes_ask", "no_bid", "no_ask")
TAIL = 150
FILLER_PX = 0.99
STAKE = 2.0


def md(v, p):
    """分位（粗, 不插值）。"""
    if not v:
        return 0.0
    v = sorted(v)
    return v[min(len(v) - 1, int(p * len(v)))]


def main():
    ev = load_events("data/btc")

    # ── 采集：尾盘每一个有效 tick（不是只取首帧）────────────────────────
    ticks = []          # {rem, side, ask, sz, cold_ask, win, date}
    mirror_bad = 0
    mirror_n = 0
    for e in ev:
        outcome = e.get("outcome")
        if outcome is None or not e.get("twap_open_price"):
            continue
        date = datetime.datetime.fromtimestamp(
            e["start_time"], datetime.timezone.utc).strftime("%Y-%m-%d")
        for x in e.get("ticks") or []:
            p = x.get("pm") or {}
            if not p or (p.get("book_latency_ms") or 0) > MAX_LAT:
                continue
            rem = x.get("rem")
            if rem is None or rem <= 0 or rem > TAIL:
                continue
            if not all((p.get(k) or 0) > 0 for k in FIELDS):
                continue
            # 镜像恒等式核验（只核验, 不做任何过滤）
            mirror_n += 1
            if abs((p.get("yes_ask_top5") or 0) - (p.get("no_bid_top5") or 0)) > 1e-6:
                mirror_bad += 1
            ya, na = p["yes_ask"], p["no_ask"]
            side = "yes" if ya >= na else "no"
            ticks.append({
                "date": date, "rem": rem, "side": side,
                "ask": p[f"{side}_ask"], "sz": p.get(f"{side}_ask_top5") or 0,
                "cold_ask": p[("no" if side == "yes" else "yes") + "_ask"],
                "win": 1 if ((outcome == 0) if side == "yes" else (outcome == 1)) else 0,
            })

    print("=" * 92)
    print("A. 字段语义核验：PM 是单一镜像簿（卖 YES@p ≡ 买 NO@(1−p)）")
    print("=" * 92)
    print(f"  尾盘有效 tick {mirror_n} 条; yes_ask_top5 ≠ no_bid_top5 的 "
          f"{mirror_bad} 条 ({mirror_bad/mirror_n*100:.3f}%)")
    print("  ⇒ `*_top5` 是数量字段（价格量级不符: 中位千位股数），且两侧互为镜像")

    # ── B. 数量分布：热门侧 ask 到底有没有量可吃 ──────────────────────────
    print("\n" + "=" * 92)
    print("B. 热门侧 ask 的可成交数量（股）：能不能吃下 2U")
    print("=" * 92)
    print(f"  {'价位档':<16}{'n':>7}{'中位':>10}{'p10':>9}{'p90':>10}"
          f"{'>0 股':>9}{'够2U':>9}{'<1 股':>8}")
    buckets = [("全部", 0.0, 1.01), ("ask == 0.99", 0.985, 0.995),
               ("[0.95,0.99)", 0.95, 0.985), ("[0.80,0.95)", 0.80, 0.95),
               ("< 0.80", 0.0, 0.80)]
    for lab, lo, hi in buckets:
        sub = [t for t in ticks if lo <= t["ask"] < hi]
        if not sub:
            continue
        sz = [t["sz"] for t in sub]
        need = STAKE / md(sz, 0.5) if md(sz, 0.5) > 0 else 0
        need = STAKE / max(md(sz, 0.5), 0.001)
        need = STAKE / statistics.median(sz) if statistics.median(sz) > 0 else 1e9
        print(f"  {lab:<16}{len(sub):>7}{statistics.median(sz):>10.1f}"
              f"{md(sz,0.1):>9.1f}{md(sz,0.9):>10.1f}"
              f"{sum(1 for s in sz if s > 0)/len(sz)*100:>8.2f}%"
              f"{sum(1 for s in sz if s >= need)/len(sz)*100:>8.1f}%"
              f"{sum(1 for s in sz if s < 1):>8}")

    # ── C. 你说的时间效应：rem → 可用量 ─────────────────────────────────
    print("\n" + "=" * 92)
    print("C. 时间效应：越接近闭市，热门侧 ask 的量是否越薄？（全价位 / 仅 0.99 档）")
    print("=" * 92)
    print(f"  {'rem 桶':<12}{'全价位 中位':>14}{'p10':>9}{'n':>9}"
          f"   |{'0.99 档 中位':>15}{'p10':>9}{'n':>9}{'<1股占比':>11}")
    for lo, hi in ((140, 151), (100, 140), (60, 100), (30, 60), (10, 30), (1, 10)):
        a = [t for t in ticks if lo <= t["rem"] < hi]
        b = [t for t in a if abs(t["ask"] - FILLER_PX) < 1e-9]
        if not a:
            continue
        sa, sb = [t["sz"] for t in a], [t["sz"] for t in b]
        s = f"  {f'[{lo},{hi})':<12}{statistics.median(sa):>14.1f}{md(sa,0.1):>9.1f}{len(sa):>9}"
        if sb:
            s += (f"   |{statistics.median(sb):>15.1f}{md(sb,0.1):>9.1f}{len(sb):>9}"
                  f"{sum(1 for x in sb if x < 1)/len(sb)*100:>10.2f}%")
        else:
            s += f"   |{'—':>15}"
        print(s)

    # ── D. 确定性效应（本探针的正题）────────────────────────────────────
    # 「确定性高」的操作化: 收盘那一侧 (win=1) 的 ask ≥ 0.95。看它的量在尾盘怎么走。
    print("\n" + "=" * 92)
    print("D. 确定性效应：把「最终会赢的那一侧 ask ≥0.95」定义为高确定性，看其可用量的")
    print("   尾盘轨迹 —— 若你说得对，越接近闭市量应越薄、直到没有挂单")
    print("=" * 92)
    print(f"  {'rem 桶':<12}{'赢侧≥0.95 中位':>18}{'p10':>9}{'=0 占比':>11}{'<1股':>9}"
          f"   |{'对照: 赢侧<0.95':>18}{'中位':>10}")
    for lo, hi in ((140, 151), (100, 140), (60, 100), (30, 60), (10, 30), (1, 10)):
        hc, lc = [], []
        for t in ticks:
            if not (lo <= t["rem"] < hi):
                continue
            if t["win"]:
                (hc if t["ask"] >= 0.95 else lc).append(t["sz"])
        if not hc and not lc:
            continue
        s = f"  {f'[{lo},{hi})':<12}{statistics.median(hc) if hc else 0:>18.1f}"
        if hc:
            s += (f"{md(hc,0.1):>9.1f}{sum(1 for x in hc if x == 0)/len(hc)*100:>10.2f}%"
                  f"{sum(1 for x in hc if x < 1):>9}")
        else:
            s += f"{'—':>9}"
        s += f"   |{statistics.median(lc) if lc else 0:>18.1f}"
        print(s)

    # ── E. 占位单检验：0.99 档的量是否是一个固定常数 ─────────────────────
    print("\n" + "=" * 92)
    print("E. 占位单检验：0.99 档的量若为「系统填充」，应出现同一个值的高频重复")
    print("=" * 92)
    s99 = [round(t["sz"], 2) for t in ticks if abs(t["ask"] - FILLER_PX) < 1e-9]
    cnt = collections.Counter(s99)
    print(f"  0.99 档 tick {len(s99)} 条, 去重后 {len(cnt)} 个不同取值"
          f" (重复率 {(1-len(cnt)/max(len(s99),1))*100:.2f}%)")
    print("  最高频取值 top8:")
    for v, c in cnt.most_common(8):
        print(f"    {v:>12.2f} 股  出现 {c:>6} 次 ({c/len(s99)*100:5.3f}%)")
    print(f"  整数取值的占比: {sum(1 for v in s99 if abs(v-round(v))<1e-9)/len(s99)*100:.2f}%"
          f"  （真挂单应为小数, 系统填充常为整数）")

    # ── F. 零量统计 ─────────────────────────────────────────────────────
    print("\n" + "=" * 92)
    print("F. 「根本没有挂单可吃」的实测频次")
    print("=" * 92)
    z = [t for t in ticks if t["sz"] == 0]
    tiny = [t for t in ticks if 0 < t["sz"] < 1]
    print(f"  数量 == 0            {len(z):>7} 条 ({len(z)/len(ticks)*100:.3f}%)")
    print(f"  数量 ∈ (0,1) 股       {len(tiny):>7} 条 ({len(tiny)/len(ticks)*100:.3f}%)")
    print(f"  数量 < 2.02 股(2U)   "
          f"{sum(1 for t in ticks if t['sz'] < 2.02):>7} 条 "
          f"({sum(1 for t in ticks if t['sz'] < 2.02)/len(ticks)*100:.3f}%)")
    if z:
        c = collections.Counter(t["ask"] for t in z)
        print(f"  零量 tick 的价位分布: {c.most_common(6)}")

    # ── G. 数量门对头条回测的影响（paper 的「即时全额成交」假设有多贵）────
    print("\n" + "=" * 92)
    print("G. 若 paper 改判「数量够才成交」，现行头条 A（快照 T=60 + ⑤ 规则）会变成什么")
    print("=" * 92)
    spec = importlib.util.spec_from_file_location("ts13g", BASE / "13_tail_sweep.py")
    tsg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tsg)
    hr = tsg.hist_ranges(ev)

    snaps = []
    for e in ev:
        outcome = e.get("outcome")
        anchor = e.get("twap_open_price")
        if outcome is None or not anchor:
            continue
        h = hr.get(e["start_time"])
        hist_bps = (h / anchor * 1e4) if h else None
        sd = (hist_bps * anchor / 1e4) if hist_bps else None
        for x in e.get("ticks") or []:
            p = x.get("pm") or {}
            if not p or (p.get("book_latency_ms") or 0) > MAX_LAT:
                continue
            rem = x.get("rem")
            if rem is None or rem <= 0 or rem > 60:
                continue
            if not all((p.get(k) or 0) > 0 for k in FIELDS):
                continue
            ya, na = p["yes_ask"], p["no_ask"]
            side = "yes" if ya >= na else "no"
            sgn = 1.0 if side == "yes" else -1.0
            spot = (x.get("bin") or {}).get("price")
            if not spot:
                break
            dev = sgn * (spot - anchor)
            sig = (dev / sd) if sd else None
            fills, sz = p[f"{side}_ask"], p.get(f"{side}_ask_top5") or 0
            snapline = None
            if fills >= 0.80 and (dev >= 63.0 or (sd is not None and sd >= 40.0
                                                  and sig is not None and sig >= 1.0)):
                snapline = {"fill": fills, "sz": sz,
                            "won": 1 if ((outcome == 0) if side == "yes"
                                         else (outcome == 1)) else 0}
            if snapline:
                snaps.append(snapline)
            break

    def pl_ungated(rows):
        s = 0.0
        for r in rows:
            sh = STAKE / r["fill"]
            s += (sh - STAKE) if r["won"] else -STAKE
        return s

    base_n, base_pl = len(snaps), pl_ungated(snaps)
    print(f"  A 基线（paper 即时全额成交, 不看数量）  n={base_n}  P&L {base_pl:+.2f}U")
    dropped = [r for r in snaps if r["sz"] < STAKE / r["fill"]]
    kept = [r for r in snaps if r["sz"] >= STAKE / r["fill"]]
    print(f"  └ 其中数量不够 2U 的                    n={len(dropped)}  "
          f"（paper 记账 {pl_ungated(dropped):+.2f}U, live 下这些根本不会成交）")
    print(f"  A' 数量门版（size ≥ Stake/fill 才成交） n={len(kept)}  "
          f"P&L {pl_ungated(kept):+.2f}U  "
          f"WR {sum(r['won'] for r in kept)/max(len(kept),1)*100:.2f}%")
    if kept and dropped:
        print(f"  ⇒ 数量门的净影响 {pl_ungated(kept)-base_pl:+.2f}U "
              f"（={base_pl:.2f} − {pl_ungated(dropped):+.2f}）")


if __name__ == "__main__":
    main()
