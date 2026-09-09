#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""04 的分桶版: spot 速度友好保持的下注族按维度拆净差（WR−fill / EV）。
维度: 侧别 × 触底深度(t0 时 ask) × 保持期强度(均|v|) × rem(t0 时)。
输出每组 n/WR/fill/净差/EV——找 WR 显著高于 fill 的子集（净差>0 才可能有 alpha）。
"""
import sys
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))
sys.path.insert(0, str(BASE))
from v2.lib import load_events  # noqa: E402
from importlib import import_module

m = import_module("04_velocity_hold")   # 复用 pm_ok 常量; extract 独立写以免 import 干扰
STAKE = m.STAKE
LOOK = m.LOOK
TRIG = m.TRIG


def extract(events):
    """同 04: 每事件每侧首个 ask≤TRIG 有效 tick=t0; 数组齐全。"""
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
                        "t0ask": a[side][t0], "rem0": rem[t0], "ask": a[side],
                        "rem": rem, "ok": ok, "v30": v30, "n": n})
    return out


def betj(r, K):
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


def line(name, ws, fs):
    if not ws:
        print(f"  {name:38s} n=0")
        return
    wr = np.mean(ws) * 100
    f = np.array(fs)
    pl = np.where(np.array(ws) == 1, STAKE / f - STAKE, -STAKE)
    print(f"  {name:38s} n={len(ws):5d} WR {wr:5.1f}% fill {f.mean():.3f} "
          f"净差 {wr - f.mean() * 100:+5.1f}pt EV {pl.mean():+.3f}U P&L {pl.sum():+7.1f}U")


def main():
    data = str(BASE.parent.parent / "data" / "btc")
    rows = extract(load_events(data))
    print(f"数据 {data}: 观察段 {len(rows)} | spot OLS-{LOOK} 友好保持 | "
          f"分桶维度: 触底深度 × 保持期强度 × rem\n")
    for K in (10, 20, 30):
        print(f"== K={K}s ==")
        for side in ("yes", "no"):
            ws_all, fs_all = [], []
            for r in rows:
                if r["side"] != side:
                    continue
                j = betj(r, K)
                if j >= 0:
                    ws_all.append(r["won"])
                    fs_all.append(r["ask"][j])
            line(f"{side} 全", ws_all, fs_all)
            # 维度组（顺序切割的层次）
            groups = {}
            for r in rows:
                if r["side"] != side:
                    continue
                j = betj(r, K)
                if j < 0:
                    continue
                dep = "d≤0.20" if r["t0ask"] <= 0.20 else "d∈(0.2,0.3]"
                seg = np.abs(r["v30"][r["t0"] + 1:j + 1])
                seg = seg[np.isfinite(seg)]
                vstr = "v强>0.1" if (len(seg) and seg.mean() > 0.1) else "v弱≤0.1"
                remb = "前段>180s" if r["rem0"] > 180 else "后段≤180s"
                groups.setdefault(dep, ([], []))
                groups.setdefault(f"{dep} × {vstr}", ([], []))
                groups.setdefault(f"{dep} × {vstr} × {remb}", ([], []))
                for g in (dep, f"{dep} × {vstr}", f"{dep} × {vstr} × {remb}"):
                    groups[g][0].append(r["won"])
                    groups[g][1].append(r["ask"][j])
            for g in sorted(groups):
                ws, fs = groups[g]
                line(f"  {side} {g}", ws, fs)
        print()
    print("（注: 三层键是同一批下注的连续切割——第一行含全部, 第二行限强度, ")
    print(" 第三行再限 rem。净差>0 才可能 alpha; n<100 组仅参考。）")


if __name__ == "__main__":
    main()
