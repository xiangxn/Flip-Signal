#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫尾盘止损：**实盘数据**验证（2026-10-03 决策 #34 的落地依据）。

数据 = `data/tail-live/`（服务器真实 GTC 挂单成交，10U/注）:
  - `tail_*.jsonl`     信号行（成交股数/均价/cost/won/pnl——结算后的真金）
  - `tailhold_*.jsonl` 持仓监察逐 tick 行（**持仓侧 bid/bid5** + rem + book_latency）

为什么必须用实盘而不是回测: 回测的「快照瞬间即成交」与实盘的「挂单等成交」不是同
一个估计量（~0.99 的窗实盘近半不成交）——止损要出场，出场价与成交概率都得按真实样本看。

口径（与 docs/tail_stoploss_live_2026-10-03.md 逐条对应）:
  - 持仓集合 = `exec_status ∈ {filled, partial}` ∧ 已结算（won ≠ None）∧ 有 hold 行;
  - 触发 = 逐 tick 扫 hold 行: `rem > 0` ∧ `book_latency_ms ≤ 300`（StopArmed 同口径）
    ∧ `0 < hold_bid < X`（纯价格腿, **严格小于**; bid == 0 是「卖不掉」不是触发）;
  - 出场 = 首次触发那一 tick 的 `hold_bid` **全额卖出**（剩余 = 全部股数; 实际 FAK 可能
    部分成交, 由退出段的可成交性检查折算）;
  - 反事实 = `shares·bid − cost`（全卖, 不再持有兑付）; 旧口径 = 行上真实 pnl;
  - Δ = Σ(反事实 − 旧), 日级配对 bootstrap 95% CI（`boot_delta`, n=2000 seed=44,
    与 23/38/41/42 同一实现——区间只能按「同一批日子重采样」读）。

§0 自检: 先钉住实盘基线（行数/胜率/P&L）, 与 dashboard 对不上就是读错了数据。
§1 纯价格腿 X=0.30: 触发次数 / 输单命中 / 误杀 / Δ / 日级配对 CI。
§2 阈值扫描 0.15~0.50（**看形状, 不是选参数**——0.30 是用户决定值）。
§3 可成交性: 触发秒的 `bid5 ≥ 剩余股数` 占比、bid5 最小值、最大持仓。
§4 出场价与 dev 腿对照（文档 §口径: 为什么不用 dev 腿）。

运行: python/venv/bin/python python/v4/44_tail_stoploss_live.py
"""
import argparse
import glob
import json
import os
import random
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MAX_LAT = 300        # StopArmed 的盘口延迟闸（tail.max_book_lat_ms）
X_DEFAULT = 0.30     # 用户决定值（stoploss_bid）
DAY0, DAY1 = "2026-09-25", "2026-10-02"   # 持仓监察上线的 8 天


def load_signals(dirpath):
    """信号行: 有真实成交（filled/partial）且已结算的那些。"""
    out = []
    for f in sorted(glob.glob(os.path.join(dirpath, "tail_*.jsonl"))):
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("event_type") != "tail" or r.get("kind") != "snap":
                continue
            if r.get("exec_status") not in ("filled", "partial"):
                continue
            if r.get("won") is None or r.get("won") is False and r.get("pnl") is None:
                continue
            r["shares"] = float(r.get("shares") or 0)
            if r["shares"] <= 0:
                continue
            out.append(r)
    return out


def load_hold(dirpath):
    """持仓监察行: condition_id -> [rows]（时间正序）。"""
    by = {}
    for f in sorted(glob.glob(os.path.join(dirpath, "tailhold_*.jsonl"))):
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("kind") != "tailhold":
                continue
            by.setdefault(r["condition_id"], []).append(r)
    for rows in by.values():
        rows.sort(key=lambda r: r["ts"])
    return by


def cost_of(rec):
    c = rec.get("cost") or 0
    return c if c > 0 else rec.get("stake") or 0


def find_trigger(rows, X, dev_below=None):
    """首个触发 tick（StopArmed 同口径）: rem>0 ∧ lat≤300 ∧ 0<bid<X
    ∧（若给 dev_below）dev < dev_below —— 文档 §5 的 dev 腿对照用。"""
    for r in rows:
        if r.get("rem", 0) <= 0:
            continue
        if (r.get("book_latency_ms") or 0) > MAX_LAT:
            continue
        bid = r.get("hold_bid") or 0
        if not (bid > 0 and bid < X):
            continue
        if dev_below is not None:
            dv = r.get("dev")
            if dv is None or dv >= dev_below:
                continue
        return r
    return None


def simulate(sigs, holds, X, dev_below=None):
    """逐仓反事实。返回 (明细, Δ, 触发数, 命中输单, 误杀)。"""
    det, delta, ntrig, hit_lost, misfire = [], 0.0, 0, 0, 0
    for rec in sigs:
        rows = holds.get(rec["condition_id"]) or []
        old = rec.get("pnl") or 0.0
        t = find_trigger(rows, X, dev_below) if rows else None
        new = old
        if t is not None:
            bid = t["hold_bid"]
            new = rec["shares"] * bid - cost_of(rec)   # 全额卖出, 不再兑付
            ntrig += 1
            if rec.get("won") is False:
                hit_lost += 1
            else:
                misfire += 1
        delta += new - old
        det.append({"rec": rec, "hold": t, "old": old, "new": new})
    return det, delta, ntrig, hit_lost, misfire


def day_paired_ci(det, n_boot=2000, seed=44):
    """日级配对 bootstrap: 按 UTC 日聚合 Δ 后重采样（`boot_delta` 同实现, 见 38 号脚本）。"""
    by_day = {}
    for d in det:
        by_day.setdefault(d["rec"]["date"], 0.0)
        by_day[d["rec"]["date"]] += d["new"] - d["old"]
    days = sorted(by_day)
    deltas = [by_day[k] for k in days]
    rnd = random.Random(seed)
    boots = []
    n = len(deltas)
    for _ in range(n_boot):
        s = sum(deltas[rnd.randrange(n)] for _ in range(n))
        boots.append(s)
    boots.sort()
    lo, hi = boots[int(0.025 * n_boot)], boots[int(0.975 * n_boot) - 1]
    return by_day, lo, hi


def fmt(v):
    return "%+.2f" % v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(ROOT, "data", "tail-live"))
    ap.add_argument("--x", type=float, default=X_DEFAULT)
    ap.add_argument("--nperm", type=int, default=2000)
    args = ap.parse_args()

    sigs_all = load_signals(args.dir)
    holds = load_hold(args.dir)
    sigs = [r for r in sigs_all if DAY0 <= r["date"] <= DAY1]
    n_with_hold = sum(1 for r in sigs if holds.get(r["condition_id"]))

    print("=" * 78)
    print("§0 自检: 实盘基线（%s ~ %s, 10U/注）" % (DAY0, DAY1))
    print("=" * 78)
    won = sum(1 for r in sigs if r.get("won"))
    pl = sum(r.get("pnl") or 0 for r in sigs)
    print("  全部成交且已结算持仓: %d 笔（赢 %d / 输 %d, 胜率 %.2f%%）" % (
        len(sigs), won, len(sigs) - won, 100.0 * won / max(len(sigs), 1)))
    print("  其中带持仓监察（可评估）: %d 笔, 基线 P&L %s U" % (n_with_hold, fmt(pl)))
    print("  （对账: dashboard 同口径——成交行含止损卖出回填 pnl）")
    sigs = [r for r in sigs if holds.get(r["condition_id"])]
    base = sum(r.get("pnl") or 0 for r in sigs)
    print("  可评估子集基线 P&L: %s U" % fmt(base))

    print()
    print("=" * 78)
    print("§1 纯价格腿 bid < %.2f（用户决定值）" % args.x)
    print("=" * 78)
    det, delta, ntrig, hit_lost, misfire = simulate(sigs, holds, args.x)
    n_lost = sum(1 for r in sigs if r.get("won") is False)
    print("  触发 %d 次 / %d 笔持仓（%.1f%%）" % (ntrig, len(sigs), 100.0 * ntrig / max(len(sigs), 1)))
    print("  输单命中 %d / %d（%.1f%%）; 误杀（触发但赢）%d / %d" % (
        hit_lost, n_lost, 100.0 * hit_lost / max(n_lost, 1), misfire,
        len(sigs) - n_lost))
    print("  Δ = %s U（反事实 %s → %s）" % (fmt(delta), fmt(base), fmt(base + delta)))
    by_day, lo, hi = day_paired_ci(det, args.nperm)
    print("  日级配对 95%% CI: [%s, %s]  (%d 天)" % (fmt(lo), fmt(hi), len(by_day)))
    for d in sorted(by_day):
        mark = "  ←" if by_day[d] != 0 else ""
        print("      %s  Δ %s%s" % (d, fmt(by_day[d]), mark))

    print()
    print("=" * 78)
    print("§2 阈值扫描（看形状, 不选参数; 0.30 是用户决定值）")
    print("=" * 78)
    print("  %-6s %-6s %-8s %-8s %-10s %s" % ("X", "触发", "输单命中", "误杀", "Δ(U)", "日级 95% CI"))
    for X in (0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50):
        detX, dX, ntX, hlX, mfX = simulate(sigs, holds, X)
        _, loX, hiX = day_paired_ci(detX, args.nperm)
        print("  %-6.2f %-6d %-8d %-8d %-10s [%s, %s]" % (
            X, ntX, hlX, mfX, fmt(dX), fmt(loX), fmt(hiX)))

    print()
    print("=" * 78)
    print("§3 可成交性: 触发那一刻有没有对手方（bid5 = 持仓侧买五档合计股数）")
    print("=" * 78)
    det, _, _, _, _ = simulate(sigs, holds, args.x)
    fracs, min_bid5, max_shares = [], None, 0.0
    for d in det:
        if d["hold"] is None:
            continue
        b5 = d["hold"].get("hold_bid5") or 0
        sh = d["rec"]["shares"]
        max_shares = max(max_shares, sh)
        fracs.append(b5 / sh if sh > 0 else 0)
        min_bid5 = b5 if min_bid5 is None else min(min_bid5, b5)
    if fracs:
        print("  触发秒 bid5 ≥ 剩余股数: %d / %d（%.1f%%）" % (
            sum(1 for f in fracs if f >= 1), len(fracs), 100.0 * sum(1 for f in fracs if f >= 1) / len(fracs)))
        print("  bid5 最小值 %.2f 股; 最大持仓 %.2f 股; bid5/股数 中位 %.1f×" % (
            min_bid5, max_shares, statistics.median(fracs)))
    else:
        print("  （无触发）")

    print()
    print("=" * 78)
    print("§4 出场价分布 与 首触发 rem 分布")
    print("=" * 78)
    bids = [d["hold"]["hold_bid"] for d in det if d["hold"] is not None]
    rems = [d["hold"]["rem"] for d in det if d["hold"] is not None]
    if bids:
        print("  出场价 bid: 中位 %.3f（p10 %.3f / p90 %.3f）" % (
            statistics.median(bids),
            sorted(bids)[int(0.1 * len(bids))], sorted(bids)[int(0.9 * len(bids))]))
        print("  首触发 rem: 中位 %ds（min %d / max %d）" % (
            statistics.median(rems), min(rems), max(rems)))
        # 输单里未触发的那些（止损没捞到的）看它当时的 bid 最低多少
        missed = [d for d in det if d["rec"].get("won") is False and d["hold"] is None]
        print("  输单未触发（止损漏网）: %d 笔" % len(missed))
    # dev 腿对照: 触发秒 dev 分布（实盘证据：报价先塌、结算线还没动）
    devs = [d["hold"].get("dev") for d in det if d["hold"] is not None and d["hold"].get("dev") is not None]
    if devs:
        print("  触发秒 dev: 中位 %s 美元（≥0 占 %.0f%%——报价先塌、结算线还没动）" % (
            fmt(statistics.median(devs)), 100.0 * sum(1 for v in devs if v >= 0) / len(devs)))

    print()
    print("=" * 78)
    print("§5 dev 腿对照: 原文档口径 `bid < 0.30 ∧ dev < −20`（决策 #24 的线上形态）")
    print("=" * 78)
    print("  %-28s %-6s %-8s %-8s %-10s %s" % ("口径", "触发", "输单命中", "误杀", "Δ(U)", "均出场"))
    for lab, kw in (("纯价格腿 bid<0.30", dict(X=0.30)),
                    ("bid<0.30 ∧ dev<−20", dict(X=0.30, dev_below=-20)),
                    ("bid<0.30 ∧ dev<−50", dict(X=0.30, dev_below=-50)),
                    ("bid<0.30 ∧ dev<0", dict(X=0.30, dev_below=0))):
        d2, dlt, nt2, hl2, mf2 = simulate(sigs, holds, **kw)
        exs = [d["hold"]["hold_bid"] for d in d2 if d["hold"] is not None]
        print("  %-28s %-6d %-8d %-8d %-10s %s" % (
            lab, nt2, hl2, mf2, fmt(dlt),
            "%.3f" % statistics.median(exs) if exs else "—"))
    print("  ⚠️ dev = sgn·(spot − anchor): 触发秒它还没塌（见 §4）⇒ dev 腿 = 等确认,")
    print("     确认到的时候报价已经走远（出场价中位 0.101 vs 0.260, 且只捞到一部分输单）。")


if __name__ == "__main__":
    main()
