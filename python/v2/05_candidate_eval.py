#!/usr/bin/env python3
"""
05 最终候选体检（v2 数据）—— flip: od≤0 & path_eff≤0.30（唯一双半同号正 EV 格）。

体检项: 整体 / 分侧 / 逐日 / 逐小时 / fill 分布 / both 率 / 与 flip 上界差距 /
wait 模式 B（rem≤60 单侧确认 follow）重验（旧结论 EV +0.01~0.02/股）。

用法: ./venv/bin/python v2/05_candidate_eval.py
"""

import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import load_events, extract_cross, cross_window

DATA = "../data/btc"
CONFIRM_S = 10
MAX_FLIP_PRICE = 0.45


def _ci(rate: float, n: int) -> str:
    if n < 5:
        return "—"
    se = (rate * (1 - rate) / n) ** 0.5
    return f"±{1.96 * se * 100:.1f}pp"


def ev_stats(xs, fill_key, flip: bool):
    xs = [x for x in xs if x.get(fill_key) is not None]
    if not xs:
        return None
    if flip:
        pnls = [-x[fill_key] if x["won"] else (1.0 - x[fill_key]) for x in xs]
        wr = sum(1 for x in xs if not x["won"]) / len(xs)
    else:
        pnls = [(1.0 - x[fill_key]) if x["won"] else -x[fill_key] for x in xs]
        wr = sum(1 for x in xs if x["won"]) / len(xs)
    gross_w = sum(p for p in pnls if p > 0)
    gross_l = -sum(p for p in pnls if p < 0)
    return (len(xs), wr, sum(x[fill_key] for x in xs) / len(xs),
            sum(pnls) / len(xs), sum(pnls), gross_w / gross_l if gross_l > 0 else float("inf"))


def main():
    events = load_events(DATA)
    by_start = {e["start_time"]: e for e in events}
    obs = extract_cross(events, "outcome")
    rows = []
    for o in obs:
        ev0 = by_start[o["event_start"]]
        f = cross_window(ev0, o["i"], o["side"], o["other"],
                         open_price=ev0.get("twap_open_price"))
        f["event_start"] = o["event_start"]
        f["cls"] = o["cls"]
        f["won"] = o["won"]
        f["side"] = o["side"]
        f["fill10s"] = o["fill10s"]
        f["flip_fill10s"] = o["flip_fill10s"]
        f["other_delta10s"] = o["other_delta10s"]
        f["rem"] = o["rem"]
        rows.append(f)

    def cand(r):
        return (r["other_delta10s"] is not None and r["other_delta10s"] <= 0
                and r.get("path_eff") is not None and r["path_eff"] <= 0.30
                and (r.get("flip_fill10s") or 1) <= MAX_FLIP_PRICE)

    sel = [r for r in rows if cand(r)]
    fk = "flip_fill10s"

    print(f"数据: {DATA}  |  观测 {len(rows)}  |  候选 flip: od≤0 & path_eff≤0.30 (gate≤{MAX_FLIP_PRICE})")
    print("=" * 84)

    st = ev_stats(sel, fk, True)
    n, wr, avg, ev_, pnl, pf = st
    both = sum(1 for r in sel if r["cls"] == "both")
    print(f"  ◆ 整体: n={n}  胜率={wr * 100:.1f}% ({_ci(wr, n)})  flip均价={avg:.3f}  "
          f"EV={ev_:+.4f}/股  P&L={pnl:+.2f}  PF={pf:.2f}")
    print(f"    both 率 {both / n * 100:.1f}%  [全样本基线 {sum(1 for r in rows if r['cls']=='both') / len(rows) * 100:.1f}%]")

    # 分侧
    print("  ── 分侧 ──")
    for side in ("yes", "no"):
        xs = [r for r in sel if r["side"] == side]
        s = ev_stats(xs, fk, True)
        if s:
            print(f"    {side.upper()}: n={s[0]} 胜率={s[1] * 100:.1f}% EV={s[3]:+.4f} P&L={s[4]:+.2f}")

    # 逐日
    print("  ── 逐日 ──")
    by_day = {}
    for r in sel:
        d = datetime.fromtimestamp(r["event_start"], tz=timezone.utc).date()
        by_day.setdefault(d, []).append(r)
    n_pos = 0
    print(f"    {'日':<12s} {'n':>4s} {'胜率':>7s} {'EV':>9s} {'日P&L':>8s}")
    for d in sorted(by_day):
        s = ev_stats(by_day[d], fk, True)
        if not s:
            continue
        if s[3] > 0:
            n_pos += 1
        print(f"    {str(d):<12s} {s[0]:>4d} {s[1] * 100:>6.1f}% {s[3]:>+9.4f} {s[4]:>+8.2f}")
    print(f"    正 EV 天数 {n_pos}/{len(by_day)}")

    # 逐小时（UTC）
    print("  ── 逐小时 (UTC) ──")
    by_hour = Counter()
    for r in sel:
        h = datetime.fromtimestamp(r["event_start"], tz=timezone.utc).hour
        by_hour[h] += 1
    print("    " + " ".join(f"{h:02d}:{by_hour[h]}" for h in range(24) if by_hour[h]))

    # fill 分布
    print("  ── fill 分布（flip 买入价）──")
    for lo, hi, lab in [(0.0, 0.20, "≤0.20"), (0.20, 0.30, "0.20-0.30"),
                        (0.30, 0.45, "0.30-0.45"), (0.45, 1.01, ">0.45(gate外)")]:
        xs = [r for r in sel if lo <= r[fk] < hi]
        s = ev_stats(xs, fk, True) if xs else None
        if s:
            print(f"    fill {lab:<12s} n={s[0]:>4d} 胜率={s[1] * 100:>5.1f}% EV={s[3]:+.4f}")

    # rem 分层
    print("  ── rem 分层 ──")
    for lo, hi, lab in [(15, 60, "15-60s"), (60, 120, "60-120s"), (120, 200, "120-200s"), (200, 260, "200-260s")]:
        xs = [r for r in sel if lo <= r["rem"] < hi]
        s = ev_stats(xs, fk, True) if xs else None
        if s:
            print(f"    rem {lab:<10s} n={s[0]:>4d} 胜率={s[1] * 100:>5.1f}% EV={s[3]:+.4f}")

    # 与上界差距
    print()
    print(f"  ◆ 与 flip 上界对比: 候选 EV {ev_:+.4f} vs 类别上界 +0.51/股（差 {ev_ - 0.51:+.4f}）")

    # wait 模式 B 重验（旧结论 follow rem≤60 单侧确认 EV +0.01~0.02/股）
    print()
    print("=" * 84)
    print("  wait 模式 B 重验: 截止 rem≤60 仅一侧穿越 → follow 该侧（决策时刻实时可知）")
    print("=" * 84)
    for wait_rem in (60, 90, 120):
        sigs = []
        for event in events:
            outcome = event.get("outcome")
            if outcome is None:
                continue
            ticks = event.get("ticks") or []
            crossed = set()
            state = {"yes": False, "no": False}
            for t in ticks:
                rem = t.get("rem")
                if rem is None or not (15 < rem < 260):
                    continue
                bids = {}
                for side in ("yes", "no"):
                    b = (t.get("pm") or {}).get(f"{side}_bid") or 0
                    bids[side] = b
                    if b > 0.7 and not state[side]:
                        crossed.add(side)
                    state[side] = b > 0.7
                if rem <= wait_rem and len(crossed) == 1:
                    side = list(crossed)[0]
                    other = "no" if side == "yes" else "yes"
                    fill = 1.0 - bids[other]
                    if fill > 0.95:
                        break
                    won = (outcome == 0) if side == "yes" else (outcome == 1)
                    sigs.append((fill, won))
                    break
        n = len(sigs)
        if not n:
            continue
        wr = sum(1 for _, w in sigs if w) / n
        mf = sum(f for f, _ in sigs) / n
        ev_w = sum((1.0 - f) if w else -f for f, w in sigs) / n
        pnl = sum((1.0 - f) if w else -f for f, w in sigs)
        print(f"  rem≤{wait_rem}: n={n:>4d}  胜率={wr * 100:>5.1f}%  均价={mf:.3f}  "
              f"EV={ev_w:+.4f}/股  P&L={pnl:+.2f}")


if __name__ == "__main__":
    main()
