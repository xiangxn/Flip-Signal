#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v5×v4 杂交探查（2026-09-08, 用户拍板）: dog@0.2 三腿 × fill≤0.25 入场封顶。
dog 语义（python/v4/01_backtest_r1.py, 组合版侧别带）:
  触发   每事件首个某侧 ask≤0.20 tick = t0（交叉态同 dog: sgn·(spot−anchor)<0 侧）
  急跌腿 m_45: t0 前 45s 同侧 ask max ≥ 0.40（固定于 t0）
  浅洞腿 dist_s(j) = sgn·(spot_j−anchor)/anchor·1e4/hist_bps ∈ 带
          （yes (−0.6,0) / no (−1,0); 组合版现行口径）
  时间腿 rem_j > 180
本次变体（入场放宽）: 不做「首触即判」——在 t0 后的 tick 序列里（仍要求
ask ≤ P 才可入场, P 主档 0.25）找第一个同时满足 m_45 + dist_s(入场 tick) +
rem(入场 tick) 的 tick 入场; 找不到 → 放弃。对照行 = dog 原样首触即判
（组合版）。报告入场延迟、腿丢失分布、EV 对照。
"""
import sys
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))
sys.path.insert(0, str(BASE))
from v2.lib import load_events  # noqa: E402

STAKE = 2.0
MAX_LAT = 300
CRASH_WIN = 45
CRASH_MIN = 0.40
BAND_YC = (-0.6, 0.0)
BAND_NO = (-1.0, 0.0)
REM_MIN = 180
PBS = (0.20, 0.22, 0.25)      # 入场 ask 封顶
TRIG = 0.20


def pm_ok(t):
    pm = t.get("pm") or {}
    return (pm.get("book_latency_ms") or 0) <= MAX_LAT and all(
        (pm.get(k) or 0) > 0 for k in ("yes_bid", "yes_ask", "no_bid", "no_ask"))


def hist_sigma(events):
    """前 ≤18 窗 |close−open| 均值（需 ≥3 窗）→ {start_time: bpsσ}。"""
    out = {}
    for i, e in enumerate(events):
        prev = []
        for j in range(max(0, i - 18), i):
            o, c = events[j].get("twap_open_price"), events[j].get("twap_close_price")
            if o and c:
                prev.append(abs(c - o))
        out[e["start_time"]] = (sum(prev) / len(prev) / e["twap_open_price"] * 1e4
                                if len(prev) >= 3 and e.get("twap_open_price") else None)
    return out


def dist_at(e, j, side, anchor, sig):
    spot = (e["ticks"][j].get("bin") or {}).get("price")
    if not spot or not sig:
        return None
    sgn = 1.0 if side == "yes" else -1.0
    return sgn * (spot - anchor) / anchor * 1e4 / sig


def in_band(d, side):
    lo = BAND_YC[0] if side == "yes" else BAND_NO[0]
    return d is not None and lo < d < 0.0


def m45_ok(e, t0i, side):
    """t0 前 45 tick 内同侧有效 ask max ≥ CRASH_MIN。"""
    mx = 0.0
    for j in range(max(0, t0i - CRASH_WIN), t0i):
        pm = (e["ticks"][j].get("pm") or {})
        if not pm or (pm.get("book_latency_ms") or 0) > MAX_LAT:
            continue
        a = pm.get(f"{side}_ask") or 0
        if a > 0 and a > mx:
            mx = a
    return mx >= CRASH_MIN


def main():
    data = str(BASE.parent.parent / "data" / "btc")
    events = [e for e in load_events(data)
              if e.get("outcome") is not None and e.get("twap_open_price")]
    sig = hist_sigma(events)
    # 触发扫描: 每事件首个 ≤0.20
    trigs = []
    for e in events:
        anchor = e["twap_open_price"]
        s = sig.get(e["start_time"])
        t0i = None
        for i, t in enumerate(e["ticks"]):
            if t.get("rem") is None or t["rem"] < 1:
                continue
            if not pm_ok(t):
                continue
            pm = t.get("pm") or {}
            ya, na = pm.get("yes_ask") or 1, pm.get("no_ask") or 1
            if ya <= TRIG or na <= TRIG:
                t0i = i
                side = "no" if (ya > TRIG) else "yes"
                if ya <= TRIG and na <= TRIG:      # 交叉态: sgn·(spot−anchor)<0 侧
                    spot = (t.get("bin") or {}).get("price")
                    side = "no" if (spot or anchor) > anchor else "yes"
                break
        if t0i is None:
            continue
        trigs.append({"e": e, "i": t0i, "side": side, "anchor": anchor, "sig": s})
    print(f"数据 {data}: {len(events)} 事件 → 0.2 首触 {len(trigs)} 段"
          f"（yes {sum(1 for x in trigs if x['side']=='yes')} / "
          f"no {sum(1 for x in trigs if x['side']=='no')}）\n")

    # dog 原样对照（首触即判, 组合版）
    ws, fs = [], []
    for x in trigs:
        e, i0, side = x["e"], x["i"], x["side"]
        if not (m45_ok(e, i0, side) and in_band(dist_at(e, i0, side, x["anchor"], x["sig"]), side)
                and (e["ticks"][i0].get("rem") or 0) > REM_MIN):
            continue
        fill = (e["ticks"][i0].get("pm") or {}).get(f"{side}_ask")
        won = int((e["outcome"] == 0) if side == "yes" else (e["outcome"] == 1))
        ws.append(won); fs.append(fill)
    f = np.array(fs)
    pl = np.where(np.array(ws) == 1, STAKE / f - STAKE, -STAKE)
    print(f"dog 原样(首触即判, 组合带): n={len(ws):5d} WR {np.mean(ws)*100:5.1f}% "
          f"fill {f.mean():.3f} EV {pl.mean():+.3f}U P&L {pl.sum():+8.1f}U\n")

    # 变体: 入场放宽到首个 (ask≤P & m45 & 浅洞(入场tick) & rem>180) 的 tick
    for P in PBS:
        ws, fs, dlays = [], [], []
        miss = {"m45": 0, "浅洞": 0, "时间": 0, "ask>P无入场": 0}
        for x in trigs:
            e, i0, side = x["e"], x["i"], x["side"]
            anchor, s = x["anchor"], x["sig"]
            crash = m45_ok(e, i0, side)          # 恒定于 t0
            j = None
            if crash:
                for k in range(i0, len(e["ticks"])):
                    tk = e["ticks"][k]
                    if (tk.get("rem") or -1) < 1:
                        break
                    pm = tk.get("pm") or {}
                    ask = pm.get(f"{side}_ask") or 1
                    if ask > P:                    # 出封顶窗口: 之后不再回判
                        # 注: 与 05 不同, 这里是单次出窗放弃（浅洞腿机制上不会回来）
                        break
                    if (tk.get("rem") or 0) <= REM_MIN:
                        continue
                    if not in_band(dist_at(e, k, side, anchor, s), side):
                        continue
                    j = k
                    break
            if j is None:
                if not crash:
                    miss["m45"] += 1
                else:
                    # 分辨丢失原因
                    reason = "ask>P无入场"
                    for k in range(i0, len(e["ticks"])):
                        tk = e["ticks"][k]
                        if (tk.get("rem") or -1) < 1:
                            reason = "时间(rem 耗尽)"; break
                        pm = tk.get("pm") or {}
                        ask = pm.get(f"{side}_ask") or 1
                        if ask > P:
                            reason = "ask>P无入场"; break
                        if (tk.get("rem") or 0) <= REM_MIN:
                            reason = "时间(判定窗内 rem≤180 且 ask 仍≤P)"
                        d = dist_at(e, k, side, anchor, s)
                        if d is not None and d <= (BAND_YC[0] if side == "yes" else BAND_NO[0]):
                            reason = "浅洞(spot 走深出带)"
                    miss[reason] = miss.get(reason, 0) + 1
                continue
            fill = (e["ticks"][j].get("pm") or {}).get(f"{side}_ask")
            won = int((e["outcome"] == 0) if side == "yes" else (e["outcome"] == 1))
            ws.append(won); fs.append(fill); dlays.append(j - i0)
        f = np.array(fs)
        pl = np.where(np.array(ws) == 1, STAKE / f - STAKE, -STAKE)
        print(f"变体 ask≤{P:.2f} 入场(三腿在入场 tick 判): n={len(ws):5d} "
              f"WR {np.mean(ws)*100:5.1f}% fill {f.mean():.3f} "
              f"[p50 {np.median(f):.3f}] EV {pl.mean():+.3f}U P&L {pl.sum():+8.1f}U"
              f" | 延迟中位 {np.median(dlays) if dlays else 0:.0f}s")
        tot = sum(miss.values()) + len(ws)
        print(f"   放弃 {tot-len(ws)}（m45 {miss['m45']}, 浅洞 {miss['浅洞']}, "
              f"时间 {miss.get('时间(判定窗内 rem≤180 且 ask 仍≤P)',0)}, "
              f"ask>P {miss['ask>P无入场']}, 其余 {miss.get('时间(rem 耗尽)',0)}）")
    print("\n（对照: dog 组合版 14 天 n=625 WR 24.6% EV +0.633U/注 +396U;")
    print(" 变体放宽容许首触时浅洞未就位的段在 ask≤P 内等 spot 进带后入场。）")


if __name__ == "__main__":
    main()
