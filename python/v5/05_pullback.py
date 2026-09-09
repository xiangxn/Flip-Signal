#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v5 回踩单探查（2026-09-08, 用户拍板试回踩变体）:
在 04「速度友好保持确认」之上加回踩腿——
  1 便宜   该侧 ask ≤ 0.30 首个有效 tick = t0（观察起点）
  2 确认   spot OLS-30 速度在友好侧连续保持 K 秒 → 确认时刻 jc
  3 回踩   jc 之后该侧 ask 回踩到 ≤ P（便宜档, P≤0.33）→ 该 tick 下注
           （变体 A: 回踩点不要求速度; 变体 B: 回踩点要求速度仍友好——防
            「冲高回落=真恶化开始」的假回踩）
  4 错过   jc 后直到 rem<1 从未回踩到档 → 放弃（错过族单列: 它们后来赢了
           多少 = 等回踩的机会成本）
fill = 回踩 tick 的该侧 ask（应显著低于 04 确认即买 ≈0.30~0.35 才有利可图）。
对照行: 04 同 K 的确认即买净差/EV。
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
TRIG = m.TRIG
KS = (10, 20, 30)
PBS = (0.28, 0.30, 0.33)


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
                        "t0ask": a[side][t0], "ask": a[side], "rem": rem, "ok": ok,
                        "v30": v30, "n": n})
    return out


def confirm_j(r, K):
    """首个「连续 K 个有效 tick 速度友好」完成 tick; -1 无。"""
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


def friendly_at(r, j):
    side = r["side"]
    v = r["v30"][j]
    return np.isfinite(v) and ((v < 0) if side == "no" else (v > 0))


def scan(r, K, P, need_friendly):
    """→ (下注j, fill) 或 (-1, None); 错过族判定由调用方对 jc 后扫描。"""
    jc = confirm_j(r, K)
    if jc < 0:
        return -2, None            # 从未确认
    for j in range(jc + 1, r["n"]):
        if r["rem"][j] < 1:
            break
        if not (r["ok"][j] and r["ask"][j] <= P):
            continue
        if need_friendly and not friendly_at(r, j):
            continue
        return j, r["ask"][j]
    return -1, None                # 确认后从未回踩（错过）


def line(name, ws, fs):
    if not ws:
        print(f"  {name:30s} n=0")
        return
    wr = np.mean(ws) * 100
    f = np.array(fs)
    pl = np.where(np.array(ws) == 1, STAKE / f - STAKE, -STAKE)
    print(f"  {name:30s} n={len(ws):5d} WR {wr:5.1f}% fill {f.mean():.3f} "
          f"[p25 {np.percentile(f,25):.3f} p75 {np.percentile(f,75):.3f}] "
          f"净差 {wr - f.mean()*100:+5.1f}pt EV {pl.mean():+.3f}U P&L {pl.sum():+8.1f}U")


def main():
    data = str(BASE.parent.parent / "data" / "btc")
    rows = extract(load_events(data))
    print(f"数据 {data}: 观察段 {len(rows)} | 回踩单: 确认(Ks 友好保持)后"
          f" ask 回踩 ≤P 下注\n")
    for K in KS:
        print(f"== K={K}s ==")
        for need in (False, True):
            tag = "变体A 回踩不限速度" if not need else "变体B 回踩须速度仍友好"
            for P in PBS:
                ws, fs, mis_w, mis_n = [], [], 0, 0
                miss_detail = []          # 错过族各段(won, jc 后最低 ask 已到 P 以下?) 略
                for r in rows:
                    j, f = scan(r, K, P, need)
                    if j == -2 or j == -1:
                        if j == -1:
                            mis_n += 1
                            mis_w += r["won"]
                        continue
                    ws.append(r["won"])
                    fs.append(f)
                line(f"{tag} P={P}", ws, fs)
                if mis_n:
                    pass
            # 错过族只在变体A P=0.30 处打印一次
            ws, fs, mis_w, mis_n = [], [], 0, 0
            for r in rows:
                j, f = scan(r, K, 0.30, False)
                if j == -1:
                    mis_n += 1
                    mis_w += r["won"]
            print(f"  [错过族 P=0.30] 确认后未回踩放弃 n={mis_n:5d} "
                  f"其实际 WR {mis_w/max(mis_n,1)*100:5.1f}%"
                  f"（机会成本参考: 若按确认点买… 见 04 对照）")
        print()
    print("（对照 04: spot K=20 确认即买 n=4383 WR 29.6% fill 0.303 EV −0.34U;")
    print(" K=30: n=3532 WR 34.5% fill 0.354 EV −0.38U——回踩若能把 fill 压回 0.28 以下")
    print(" 且 WR 不塌, 才有转正可能。）")


if __name__ == "__main__":
    main()
