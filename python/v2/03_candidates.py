#!/usr/bin/env python3
"""
03 组合候选 + 按天 split 验证（v2 数据, 计划 §7.3 纪律: 双半同号才算数）。

决策时刻: 穿越 +10s（特征 ≤ 该时刻）。成交口径: follow=1-对侧bid / flip=1-穿越侧bid。
候选全部为实时可知过滤（无未来信息）。

输出:
  * 每候选: flip/follow 两方向的 n/胜率/均价/EV/P&L/PF
  * 按天 split（前 7 天 vs 后 7 天）双半同号检验
  * 逐日 EV 稳定性

用法: ./venv/bin/python v2/03_candidates.py
"""

import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import load_events, extract_cross, cross_window

DATA = "../data/btc"
CONFIRM_S = 10
MAX_FLIP_PRICE = 0.45   # flip 买入对侧价格 gate（与引擎 trading.max_price 一致口径）


def _ci(rate: float, n: int) -> str:
    if n < 5:
        return "—"
    se = (rate * (1 - rate) / n) ** 0.5
    return f"±{1.96 * se * 100:.1f}pp"


def ev_stats(xs, fill_key, flip: bool):
    """xs: dict 列表 → (n, wr, avg_fill, ev, pnl, pf)。flip=True 买对侧。"""
    xs = [x for x in xs if x.get(fill_key) is not None]
    if not xs:
        return None
    n = len(xs)
    if flip:
        pnls = [-x[fill_key] if x["won"] else (1.0 - x[fill_key]) for x in xs]
    else:
        pnls = [(1.0 - x[fill_key]) if x["won"] else -x[fill_key] for x in xs]
    wr = sum(1 for x in xs if x["won"]) / n if not flip else sum(1 for x in xs if not x["won"]) / n
    avg = sum(x[fill_key] for x in xs) / n
    gross_w = sum(p for p in pnls if p > 0)
    gross_l = -sum(p for p in pnls if p < 0)
    return (n, wr, avg, sum(pnls) / n, sum(pnls), gross_w / gross_l if gross_l > 0 else float("inf"))


def report(xs, title, fill_key, flip: bool) -> None:
    st = ev_stats(xs, fill_key, flip)
    if not st:
        print(f"  {title:<34s} 无数据")
        return
    n, wr, avg, ev, pnl, pf = st
    print(f"  {title:<34s} n={n:>4d} 胜率={wr * 100:>5.1f}% ({_ci(wr, n)}) 均价={avg:.3f} "
          f"EV={ev:+.4f}/股 P&L={pnl:+7.2f} PF={pf:5.2f}")


def day_split(rows, cand, fill_key, flip: bool):
    """前 7 天 vs 后 7 天（按事件 start_time 排序, 中位切分）。"""
    xs = [x for x in rows if cand(x)]
    if not xs:
        return None
    xs.sort(key=lambda x: x["event_start"])
    mid = xs[len(xs) // 2]["event_start"]
    a = [x for x in xs if x["event_start"] <= mid]
    b = [x for x in xs if x["event_start"] > mid]
    sa, sb = ev_stats(a, fill_key, flip), ev_stats(b, fill_key, flip)
    if not sa or not sb:
        return None
    return sa, sb, mid


def main():
    events = load_events(DATA)
    by_start = {e["start_time"]: e for e in events}
    obs = extract_cross(events, "outcome")
    rows = []
    for o in obs:
        ev = by_start[o["event_start"]]
        f = cross_window(ev, o["i"], o["side"], o["other"],
                         open_price=ev.get("twap_open_price"))
        f["event_start"] = o["event_start"]
        f["cls"] = o["cls"]
        f["won"] = o["won"]
        f[f"fill{CONFIRM_S}s"] = o[f"fill{CONFIRM_S}s"]
        f[f"flip_fill{CONFIRM_S}s"] = o[f"flip_fill{CONFIRM_S}s"]
        f[f"other_delta{CONFIRM_S}s"] = o[f"other_delta{CONFIRM_S}s"]
        rows.append(f)

    CAND = [
        ("flip: od≤0", "翻转（买对侧, gate≤0.45）", "flip_fill10s", True,
         lambda r: (r["other_delta10s"] is not None and r["other_delta10s"] <= 0
                    and (r.get("flip_fill10s") or 1) <= MAX_FLIP_PRICE)),
        ("flip: od≤0 & path_eff≤0.4", "翻转（旧 od0_pe4 形态）", "flip_fill10s", True,
         lambda r: (r["other_delta10s"] is not None and r["other_delta10s"] <= 0
                    and r.get("path_eff") is not None and r["path_eff"] <= 0.4
                    and (r.get("flip_fill10s") or 1) <= MAX_FLIP_PRICE)),
        ("flip: od≤0 & twap_pos>0.25", "翻转（TWAP 已动）", "flip_fill10s", True,
         lambda r: (r["other_delta10s"] is not None and r["other_delta10s"] <= 0
                    and r.get("twap_pos") is not None and r["twap_pos"] > 0.25
                    and (r.get("flip_fill10s") or 1) <= MAX_FLIP_PRICE)),
        ("flip: od≤0 & noise≥4", "翻转（噪声高→both 率高）", "flip_fill10s", True,
         lambda r: (r["other_delta10s"] is not None and r["other_delta10s"] <= 0
                    and r.get("noise") is not None and r["noise"] >= 4
                    and (r.get("flip_fill10s") or 1) <= MAX_FLIP_PRICE)),
        ("follow: od0_pe4", "follow（旧过滤重验）", "fill10s", False,
         lambda r: (r["other_delta10s"] is not None and r["other_delta10s"] <= 0
                    and r.get("path_eff") is not None and r["path_eff"] <= 0.4)),
        ("follow: tape_imb>T2", "follow（PM 强势净买）", "fill10s", False,
         lambda r: (r.get("tape_imb") is not None and r["tape_imb"] > 0)),
        ("follow: twap_gap≤-0.5", "follow（现货远离 TWAP）", "fill10s", False,
         lambda r: (r.get("twap_gap") is not None and r["twap_gap"] <= -0.5)),
    ]

    print(f"数据: {DATA}  |  穿越观测 {len(rows)}  |  决策 +{CONFIRM_S}s  |  "
          f"flip gate ≤ {MAX_FLIP_PRICE}")
    print("=" * 88)
    print("  候选组合（全部 ≤ 决策时刻可知）")
    print("=" * 88)
    for name, desc, fk, flip, cand in CAND:
        print(f"\n  ◆ {name} —— {desc}")
        report(rows, "全样本", fk, flip)
        sel = [x for x in rows if cand(x)]
        both = sum(1 for x in sel if x["cls"] == "both")
        if sel:
            print(f"  {'':<34s} both 率 {both / len(sel) * 100:.1f}%  "
                  f"({both}/{len(sel)})  [基线 {sum(1 for r in rows if r['cls']=='both') / len(rows) * 100:.1f}%]")
            days = sorted({datetime.fromtimestamp(x["event_start"], tz=timezone.utc).date()
                           for x in sel})
            print(f"  {'':<34s} 覆盖天数 {len(days)}  |  日均 {len(sel) / len(days):.1f} 笔")
            sa, sb, mid = day_split(rows, cand, fk, flip)
            if sa and sb:
                sign = "✓ 双半同号" if (sa[3] > 0) == (sb[3] > 0) else "✗ 异号"
                print(f"  {'':<34s} split: 前半 n={sa[0]} EV={sa[3]:+.4f} | "
                      f"后半 n={sb[0]} EV={sb[3]:+.4f}  {sign}")
        # 逐日
        xs = [x for x in rows if cand(x)]
        if xs:
            by_day = {}
            for x in xs:
                d = datetime.fromtimestamp(x["event_start"], tz=timezone.utc).date()
                by_day.setdefault(d, []).append(x)
            parts = []
            for d in sorted(by_day):
                st = ev_stats(by_day[d], fk, flip)
                if st:
                    parts.append(f"{str(d)[5:]}:{st[3]:+.3f}")
            print(f"  {'':<34s} 逐日EV: {' '.join(parts)}")


if __name__ == "__main__":
    main()
