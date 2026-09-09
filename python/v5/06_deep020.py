#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v5 深坑入口探查（2026-09-08, 用户设计 2026-09-08: 回踩证伪后降触发）:
  1 便宜    该侧 ask ≤ 0.20 首个有效 tick = t0（深坑才启动观察/确认）
  2 确认    spot OLS-30 友好连续保持 K 秒 → jc（K=0 = 无确认腿对照）
  3 入场    jc 之后首个 ask ≤ P 的 tick（P ≤ 0.25 封顶 fill / 盈亏平衡线;
            确认完成瞬间 ask 仍在 P 内 → 立即买; 涨过 P 等回落, 直到 rem<1）
  4 错过    jc 后从未 ≤P → 放弃（错过族单列实际 WR = 机会成本）
fill 封顶 0.20/0.22/0.25（+0.30 参照）= 平衡线 20~30%; 对照 K=0 无确认族。
分侧输出（04b 显示 no 深坑族明显优于 yes, 单列）。参考线: dog@0.2 组合带
n=625 WR 24.6% fill≈0.19 EV +0.633U/注。
"""
import sys
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))
sys.path.insert(0, str(BASE))
from v2.lib import load_events  # noqa: E402
from importlib import import_module

m = import_module("04_velocity_hold")
STAKE = m.STAKE
LOOK = m.LOOK
TRIG = 0.20
KS = (0, 10, 20, 30)
PBS = (0.20, 0.22, 0.25, 0.30)


def extract(events):
    """每事件每侧首个 ask≤TRIG 有效 tick=t0; 数组齐全（同 04/05）。"""
    out = []
    for e in events:
        if e.get("outcome") is None:
            continue
        ticks = e.get("ticks") or []
        n = len(ticks)
        ts = np.array([t["ts"] for t in ticks], dtype=float) / 1000.0 - e["start_time"]
        rem = np.array([t.get("rem", -1) for t in ticks], dtype=float)
        ok = np.zeros(n, bool)
        a = {s: np.full(n, np.nan) for s in ("yes", "no")}
        pr = np.full(n, np.nan)
        for i, t in enumerate(ticks):
            pm = t.get("pm") or {}
            if m.pm_ok(t):
                ok[i] = True
                for s in ("yes", "no"):
                    a[s][i] = pm.get(f"{s}_ask") or 0.0
            b = t.get("bin") or {}
            if b.get("price"):
                pr[i] = b["price"]
        v30 = np.full(n, np.nan)
        lo = cc = 0
        sx = sy = sxx = sxy = 0.0
        for i in range(n):
            x, y = ts[i], pr[i]
            if np.isfinite(y):
                cc += 1; sx += x; sy += y; sxx += x * x; sxy += x * y
            while ts[i] - ts[lo] > LOOK:
                xl, yl = ts[lo], pr[lo]
                if np.isfinite(yl):
                    cc -= 1; sx -= xl; sy -= yl; sxx -= xl * xl; sxy -= xl * yl
                lo += 1
            if cc >= 24:
                den = cc * sxx - sx * sx
                if den > 1e-12:
                    v30[i] = (cc * sxy - sx * sy) / den
        for side in ("yes", "no"):
            hit = np.where(ok & (a[side] <= TRIG) & (rem >= 1))[0]
            if len(hit) == 0:
                continue
            t0 = int(hit[0])
            won = int((e["outcome"] == 0) if side == "yes" else (e["outcome"] == 1))
            out.append({"ev": e["start_time"], "side": side, "t0": t0, "won": won,
                        "t0ask": a[side][t0], "ask": a[side], "rem": rem, "ok": ok,
                        "v30": v30, "n": n})
    return out


def confirm_j(r, K):
    if K == 0:
        return r["t0"]                # 无确认: jc=t0（触底即入场扫描起点）
    side = r["side"]
    fr = (r["v30"] < 0) if side == "no" else (r["v30"] > 0)
    fr = fr & np.isfinite(r["v30"]) & r["ok"]
    run = 0
    for j in range(r["t0"] + 1, r["n"]):
        if fr[j] and r["rem"][j] >= 1:
            run += 1
            if run >= K:
                return j
        else:
            run = 0
    return -1


def scan(r, K, P):
    jc = confirm_j(r, K)
    if jc < 0:
        return -2, None                # 从未确认
    for j in range(jc, r["n"]):        # jc 本身若 ask≤P 即立即买
        if r["rem"][j] < 1:
            break
        if r["ok"][j] and r["ask"][j] <= P:
            return j, r["ask"][j]
    return -1, None                    # 错过


def line(name, ws, fs):
    if not ws:
        print(f"  {name:36s} n=0")
        return
    wr = np.mean(ws) * 100
    f = np.array(fs)
    pl = np.where(np.array(ws) == 1, STAKE / f - STAKE, -STAKE)
    print(f"  {name:36s} n={len(ws):5d} WR {wr:5.1f}% fill {f.mean():.3f} "
          f"[p25 {np.percentile(f,25):.3f} p75 {np.percentile(f,75):.3f}] "
          f"净差 {wr - f.mean()*100:+5.1f}pt EV {pl.mean():+.3f}U "
          f"P&L {pl.sum():+8.1f}U")


def main():
    data = str(BASE.parent.parent / "data" / "btc")
    rows = extract(load_events(data))
    ys = sum(1 for r in rows if r["side"] == "yes")
    ns = len(rows) - ys
    print(f"数据 {data}: 触发≤{TRIG} 观察段 {len(rows)}（yes {ys} / no {ns}）\n")
    for K in KS:
        tag = "K= 0 无确认(对照)" if K == 0 else f"K={K:>2}s 确认"
        print(f"== {tag} ==")
        for side in ("合计", "yes", "no"):
            for P in PBS:
                ws, fs = [], []
                mis = [(0, 0), (0, 0), (0, 0)]   # 错过族 (n, win) 按 side 槽位
                slot = {"合计": 0, "yes": 1, "no": 2}[side]
                for r in rows:
                    if side != "合计" and r["side"] != side:
                        continue
                    j, f = scan(r, K, P)
                    if j == -1:
                        if slot == 0:
                            pass
                        continue
                    if j == -2:
                        continue
                    ws.append(r["won"])
                    fs.append(f)
                line(f"{side} P≤{P:.2f}", ws, fs)
        print()


if __name__ == "__main__":
    main()
