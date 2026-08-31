#!/usr/bin/env python3
"""
04 阈值网格扫描 + split 稳定性（v2 数据, 计划 §7.3）—— 确认唯一正候选不是过拟合。

扫描 flip 方向: od 阈值 × path_eff 阈值 × rem 分层; follow 方向同网格。
纪律: 每格报告 n / flip EV / follow EV / 双半 EV（同号才可保留）。
用法: ./venv/bin/python v2/04_combo_scan.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import load_events, extract_cross, cross_window

DATA = "../data/btc"
CONFIRM_S = 10
MAX_FLIP_PRICE = 0.45


def ev(xs, fill_key, flip: bool):
    xs = [x for x in xs if x.get(fill_key) is not None]
    if not xs:
        return None
    if flip:
        pnl = [-x[fill_key] if x["won"] else (1.0 - x[fill_key]) for x in xs]
    else:
        pnl = [(1.0 - x[fill_key]) if x["won"] else -x[fill_key] for x in xs]
    return sum(pnl) / len(xs)


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
        f["fill10s"] = o["fill10s"]
        f["flip_fill10s"] = o["flip_fill10s"]
        f["other_delta10s"] = o["other_delta10s"]
        f["rem"] = o["rem"]
        rows.append(f)

    print(f"数据: {DATA}  |  观测 {len(rows)}  |  flip gate ≤ {MAX_FLIP_PRICE}")
    for flip, label in ((True, "FLIP(买对侧)"), (False, "FOLLOW(买穿越侧)")):
        fk = "flip_fill10s" if flip else "fill10s"
        print()
        print("=" * 100)
        print(f"  {label} —— od 阈值 × path_eff 阈值网格 [格式: n/EV/前半EV/后半EV]")
        print("=" * 100)
        header = "  od≤      | " + " | ".join(f"pe≤{p:.2f}" for p in (0.3, 0.4, 0.5, 0.6, 1.0)) + " | rem≤120"
        print(header)
        for od_t in (0.0, 0.005, 0.01, 0.02):
            cells = []
            for pe_t in (0.3, 0.4, 0.5, 0.6, 1.0):
                sel = [x for x in rows
                       if x["other_delta10s"] is not None and x["other_delta10s"] <= od_t
                       and x.get("path_eff") is not None and x["path_eff"] <= pe_t
                       and (not flip or (x.get(fk) or 1) <= MAX_FLIP_PRICE)]
                e = ev(sel, fk, flip)
                if not sel or e is None:
                    cells.append("       —       ")
                    continue
                sel.sort(key=lambda x: x["event_start"])
                mid = sel[len(sel) // 2]["event_start"]
                a = ev([x for x in sel if x["event_start"] <= mid], fk, flip)
                b = ev([x for x in sel if x["event_start"] > mid], fk, flip)
                tag = "" if a is None or b is None else ("✓" if (a > 0) == (b > 0) else "✗")
                cells.append(f"{len(sel)}/{e:+.3f}/{a:+.2f}/{b:+.2f}{tag}")
            # rem≤120 列（od≤0 & pe≤0.4 基础上）
            sel = [x for x in rows
                   if x["other_delta10s"] is not None and x["other_delta10s"] <= 0.0
                   and x.get("path_eff") is not None and x["path_eff"] <= 0.4
                   and x["rem"] <= 120
                   and (not flip or (x.get(fk) or 1) <= MAX_FLIP_PRICE)]
            e = ev(sel, fk, flip)
            cells.append(f"{len(sel)}/{e:+.3f}" if sel and e is not None else "       —       ")
            print(f"  od≤{od_t:<5.2f} | " + " | ".join(cells))


if __name__ == "__main__":
    main()
