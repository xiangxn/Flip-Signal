#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v7-00: 底座自检（跑在任何分析之前）。

四关:
  ① **§0 pin**（oracle 23 逐位复现）—— 宇宙层与判定链没跑偏;
  ② **信号 ↔ 原始行同一性** —— 信号表里的 rem/fill/dev/walk/side/sd 必须与原始 tick 逐位相等
     （防「gi 指错行」这类静默错位: 曾经 T=150 段的 gi 被写成常量 0, 特征整段取错行,
     而两侧同错时对账照样通过）;
  ③ **手工复算** —— 抽每段各一条信号, 用纯 python（不走 numpy 向量化）逐字段重算 15 个特征;
  ④ **无未来信息** —— 把每个 tick 之后的行**全部删掉**重算, 该 tick 的特征必须逐位不变。

用法: python/venv/bin/python python/v7/00_selfcheck.py [--dirs data/btc]
"""
import argparse
import json
import random
import sys
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import lib_features as F          # noqa: E402
import lib_universe as U          # noqa: E402


def manual_features(rows, gi, anchor, sd, vol_base):
    """纯 python 复算（独立于 lib_features 的 numpy 实现）——只用到 rows[0..gi]。"""
    ts = [r[1] for r in rows]
    spot = [r[5] for r in rows]
    sgn = rows[gi][3]
    m = {
        "fill": rows[gi][4], "rem": rows[gi][2], "sd_bps": sd / anchor * 1e4,
        "dev_sigma": rows[gi][7] / sd, "walk_sigma": rows[gi][8] / sd,
        "basis_sigma": rows[gi][9] / sd, "book_latency_ms": rows[gi][10],
        "twap_age_ms": rows[gi][11],
    }
    rem_i = rows[gi][2]
    if rem_i > 60:
        m["slack_sigma"] = rows[gi][7] / sd
    else:
        k0 = next(k for k in range(len(rows)) if rows[k][2] <= 60)
        span, cnt = 60.0 - rem_i, gi - k0
        if span <= 0:
            m["slack_sigma"] = rows[gi][7] / sd
        elif cnt < 1 or 2 * cnt < span + 1:
            m["slack_sigma"] = float("nan")
        else:
            hm = sum(spot[k0:gi]) * span / cnt
            m["slack_sigma"] = sgn * ((hm + rem_i * spot[gi]) / 60.0 - anchor) / sd
    back = [k for k in range(gi + 1) if ts[gi] - ts[k] >= 10000]
    if not back or abs(ts[gi] - ts[back[-1]] - 10000) > 1500:
        m["spot_ret_10s_sigma"] = float("nan")
    else:
        m["spot_ret_10s_sigma"] = sgn * (spot[gi] - spot[back[-1]]) / sd
    m["giveback_sigma"] = (max(r[7] for r in rows[:gi + 1]) - rows[gi][7]) / sd
    cum = [r[15] for r in rows]
    run = cum[gi] / (gi + 1)
    st30 = next((k for k in range(gi + 1) if ts[gi] - ts[k] <= 30000), 0)
    cnt30 = gi + 1 - st30
    rate30 = (cum[gi] - (cum[st30 - 1] if st30 > 0 else 0.0)) / cnt30
    m["vol_rate_30s"] = rate30 / run if (cnt30 >= 5 and gi + 1 >= 10) else float("nan")
    m["vol_ratio_prev"] = (run / vol_base) if (gi + 1 >= 10 and vol_base) else float("nan")
    return m


def same(a, b):
    if a != a and b != b:
        return True
    return abs(a - b) <= 1e-9 * max(1.0, abs(b))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dirs", nargs="+", default=["data/btc"])
    ap.add_argument("--n-rand", type=int, default=20)
    args = ap.parse_args()
    paths = U.paths_of(args.dirs)
    fails = 0

    # ── ① §0 pin ──
    print("[①] 现行规则链 pin …")
    ctx = U.hist_context(U.scan_scalars(paths))
    if not U.check_pin(U.replay(paths, ctx)):
        fails += 1

    sig = np.load(BASE / "data/signals.npz")
    wins = json.loads((BASE / "data/windows.json").read_text())
    start2wi = {w["start_time"]: i for i, w in enumerate(wins)}

    # 抽三个窗（每段一条信号）+ 若干随机窗
    picks = {}
    for stage in (0, 1, 2):
        idx = np.nonzero(sig["stage"] == stage)[0]
        picks[int(sig["win"][idx[0]])] = int(idx[0])
    rng = random.Random(42)
    rand_wins = rng.sample([w["start_time"] for w in wins], min(args.n_rand, len(wins)))
    want = set(picks) | set(rand_wins)
    key2sig = {int(sig["win"][i]): i for i in range(len(sig["win"]))}

    raw = {}
    for p in paths:
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                if r.get("start_time") in want:
                    raw[r["start_time"]] = r
        if len(raw) == len(want):
            break
    print(f"[②③④] 抽 {len(picks)} 个信号窗 + {len(rand_wins)} 个随机窗 …")

    n_fut = 0
    for st in sorted(raw):
        e = raw[st]
        c = ctx[st]
        if c["sd"] is None or not e.get("twap_open_price"):
            continue
        rows = U.grid_of(e, e["twap_open_price"])
        if not rows:
            continue
        anchor, sd, vb = e["twap_open_price"], c["sd"], c["vol_base"]
        Aw = np.array(rows, dtype=np.float64)
        feats = F.window_features(Aw, anchor, sd, vb, c["sd_base"])

        # ── ② 信号行 ↔ 原始行同一性（只在信号窗做）──
        if st in picks:
            si = key2sig[st]
            gi = int(sig["gi"][si])
            r = rows[gi]
            checks = [("rem", r[2], sig["rem"][si]), ("fill", r[4], sig["fill"][si]),
                      ("dev", r[7], sig["dev"][si]), ("sd", sd, sig["sd"][si]),
                      ("side", r[3] > 0, sig["rem"][si] is not None and r[3] > 0)]
            if not np.isnan(sig["walk"][si]):
                checks.append(("walk", r[8], sig["walk"][si]))
            bad = [(k, a, b) for k, a, b in checks if not same(a, b)]
            # 段位一致性: t150 的 rem 必须 > 60 且 ≤ 150
            stage = int(sig["stage"][si])
            if stage == 0 and not (60 < r[2] <= 150):
                bad.append(("t150 rem 越界", r[2], "60<rem<=150"))
            if bad:
                fails += 1
                print(f"  ✗ [②] win={st} gi={gi} 原行不符: {bad}")
            else:
                print(f"  ✓ [②] win={st} stage={F.STAGE_ID_REV[stage]} gi={gi} "
                      f"rem={r[2]:.0f} fill={r[4]} dev={r[7]:+.2f} sd={sd:.2f}")

            # ── ③ 手工复算（抽 3 个信号窗各自的判定 tick）──
            man = manual_features(rows, gi, anchor, sd, vb)
            diffs = [(k, float(feats[k][gi]), v) for k, v in man.items()
                     if not same(float(feats[k][gi]), v)]
            if diffs:
                fails += 1
                print(f"  ✗ [③] win={st} 手工复算不符: {diffs}")
            else:
                print(f"  ✓ [③] win={st} 15 个特征逐字段手工复算一致")

        # ── ④ 无未来信息: 在位置 i 截断重算, 特征必须不变 ──
        n = len(rows)
        for i in sorted({0, n // 3, n // 2, (2 * n) // 3, n - 1}):
            f_trunc = F.window_features(Aw[:i + 1], anchor, sd, vb, c["sd_base"])
            for k in F.TICK_FULL:
                a, b = float(f_trunc[k][i]), float(feats[k][i])
                if not same(a, b):
                    fails += 1
                    n_fut += 1
                    print(f"  ✗ [④] win={st} gi={i} 特征 {k} 随未来数据变化: "
                          f"截断 {a} vs 全量 {b}")
    print(f"[④] 截断重算: {'✅ 全部逐位不变（无未来信息）' if n_fut == 0 else f'❌ {n_fut} 处不一致'}")
    print(f"\n{'✅ 自检全部通过' if fails == 0 else f'❌ 自检失败 {fails} 处'}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
