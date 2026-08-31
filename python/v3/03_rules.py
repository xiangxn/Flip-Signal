#!/usr/bin/env python3
"""
v3 阶段 3 —— 可解释分桶规则（flip 单线, 目标 WR ≥ 35% 且 EV = WR - fill > 0）。

协议（诚实性关键）:
  1. 规则只在 train 日（≤08-28）上挑选
  2. 在 test 日（08-29/30, 2d）与长测试窗（08-27~08-30, 4d）上验证
  3. 双半同号 + 逐日检查
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from featlib import DATA  # noqa: E402

HALF_EV, HALF_WR = 0.35, 0.35  # 候选门槛


def load():
    df = pd.read_pickle(BASE / "data" / "featmat.pkl")
    return df[df["flip_fill0s"].notna()]


def stats(d, fill_key="flip_fill10s"):
    if len(d) == 0:
        return None
    wr = d["flip_won"].mean()
    f_ = d[fill_key].mean()
    return dict(n=len(d), wr=wr, fill=f_, ev=wr - f_, both=d["cls"].eq("both").mean())


def show(name, d, fill_key="flip_fill10s", tag=""):
    s = stats(d, fill_key)
    if s is None:
        print(f"  [{name}] 空")
        return s
    mark = " ★" if (s["wr"] >= 0.35 and s["ev"] > 0 and s["n"] >= 60) else ""
    print(f"  [{name}] n={s['n']:>4d} WR {s['wr']*100:>5.1f}% fill {s['fill']:.3f} "
          f"EV {s['ev']:+.4f} both {s['both']*100:.1f}%{mark}{tag}")
    return s


def rule_stats(df, mask, fill_key, name):
    """完整体检: train 选 / test 2d / 长窗 4d / 双半 / 逐日。"""
    print(f"\n=== 规则: {name}  (n={mask.sum()}) ===")
    tr = df[mask & df["date"].le("2026-08-28")]
    te = df[mask & df["date"].isin(("2026-08-29", "2026-08-30"))]
    te4 = df[mask & df["date"].ge("2026-08-27") & df["date"].le("2026-08-30")]
    if not mask.any():
        print("  全空")
        return None
    base_te = stats(df[df["date"].isin(("2026-08-29", "2026-08-30"))])
    print(f"  test 基线: WR {base_te['wr']*100:.1f}% fill {base_te['fill']:.3f} EV {base_te['ev']:+.4f}")
    show("train 选样", tr, fill_key)
    show("test 2d 验证", te, fill_key)
    show("长窗 4d 验证", te4, fill_key)
    # 双半（train 期按时间前/后半）
    tdates = sorted(df["date"].unique())
    half_dates = [d for d in tdates if d <= "2026-08-28"]
    m1 = int(len(half_dates) * 0.6)
    h1 = tr[tr["date"].isin(half_dates[:m1])]
    h2 = tr[tr["date"].isin(half_dates[m1:])]
    s1, s2 = stats(h1, fill_key), stats(h2, fill_key)
    ok = s1 and s2 and s1["ev"] > 0 and s2["ev"] > 0
    print(f"  双半: H1 {s1['wr']*100:.1f}%/EV{s1['ev']:+.4f}  H2 {s2['wr']*100:.1f}%/EV{s2['ev']:+.4f}"
          f"  {'✓ 同号' if ok else '✗ 反号'}")
    # 逐日
    day_rows = []
    for d, g in tr.groupby("date"):
        s = stats(g, fill_key)
        day_rows.append((d, s["n"], s["wr"], s["ev"]))
    pos_days = sum(1 for _, _, _, e in day_rows if e > 0)
    print(f"  逐日 EV 正: {pos_days}/{len(day_rows)}  "
          + "  ".join(f"{d[5:]}: {w*100:.0f}%/{e:+.2f}" for d, n, w, e in day_rows))
    return dict(train=stats(tr, fill_key), test2d=stats(te, fill_key),
                test4d=stats(te4, fill_key), half_ok=ok)


def main():
    df = load()
    base = stats(df)
    print(f"数据: {DATA}  |  观测 {len(df)}  |  flip 基线 WR {base['wr']*100:.1f}% "
          f"fill0s {base['fill']:.3f} EV0s {0.249-0.254:+.4f}")

    print("=" * 78)
    print("1D 分桶勘察（train 日 ≤08-28, 目标 WR≥35% 且 EV>0）")
    print("=" * 78)
    tr = df[df["date"].le("2026-08-28")]
    for feat in ["post_end", "other_delta10s", "post_aggr_buy_share", "f_accel",
                 "twap_pos", "dep_imbalance", "basis_slope", "trigger_bid",
                 "path_eff", "spot_pos", "rem", "cross_speed_s"]:
        print(f"\n  ◆ {feat}")
        qs = [df[feat].quantile(q) for q in (0.2, 0.4, 0.6, 0.8)]
        bounds = [-np.inf] + qs + [np.inf]
        prev = None
        for k in range(5):
            lo, hi = bounds[k], bounds[k + 1]
            m = tr[feat].between(lo, hi, inclusive="neither") if k == 0 else \
                tr[feat].gt(lo) & tr[feat].le(hi) if k < 4 else tr[feat].gt(lo)
            s = stats(tr[m])
            if s is None:
                continue
            mark = " ★" if (s["wr"] >= 0.35 and s["ev"] > 0 and s["n"] >= 60) else ""
            print(f"    Q{k+1} ({lo:.3f},{hi:.3f}] n={s['n']:>4d} WR {s['wr']*100:>5.1f}% "
                  f"fill {s['fill']:.3f} EV {s['ev']:+.4f}{mark}")
            prev = s

    print("\n" + "=" * 78)
    print("候选规则体检（train 选 / test 验）")
    print("=" * 78)
    candidates = {
        "post_end ≥ 0.30（穿越侧 +10s 仍高）":
            df["post_end"].ge(0.30),
        "post_end ≥ 0.35":
            df["post_end"].ge(0.35),
        "other_delta10s ≥ 0.02（v2 经典）":
            df["other_delta10s"].ge(0.02),
        "other_delta10s > 0.05":
            df["other_delta10s"].gt(0.05),
        "post_end ≥0.30 & other_delta10s ≥0.02":
            df["post_end"].ge(0.30) & df["other_delta10s"].ge(0.02),
        "post_end ≥0.35 & other_delta10s ≥0.05":
            df["post_end"].ge(0.35) & df["other_delta10s"].ge(0.05),
        "post_aggr_buy_share ≤ 0.4（穿越侧无主动买推动）":
            df["post_aggr_buy_share"].le(0.4),
        "f_accel < 0（现货加速向 flip 有利方向）":
            df["f_accel"].lt(0),
        "dep_imbalance < 0（NO 侧 bid 深度占优）":
            df["dep_imbalance"].lt(0),
        "trigger_bid ≥ 0.85（市场极自信）":
            df["trigger_bid"].ge(0.85),
        "trigger_bid ≥ 0.85 & other_delta10s ≥ 0.02":
            df["trigger_bid"].ge(0.85) & df["other_delta10s"].ge(0.02),
        "rem ≤ 120（窗口尾部）":
            df["rem"].le(120),
        "twap_pos ≥ 0.5（TWAP 已向 flip 有利方向移）":
            df["twap_pos"].ge(0.5),
    }
    results = {}
    for name, mask in candidates.items():
        r = rule_stats(df, mask, "flip_fill10s", name)
        results[name] = r
        # 也要 +0s 入场口径
        tr_m = df[mask & df["date"].le("2026-08-28")]
        te_m = df[mask & df["date"].isin(("2026-08-29", "2026-08-30"))]
        if te_m["flip_fill0s"].notna().any():
            s = stats(te_m, "flip_fill0s")
            print(f"  → 若 +0s 入场: test2d WR {s['wr']*100:.1f}% fill {s['fill']:.3f} "
                  f"EV {s['ev']:+.4f}")
        print()

    # 汇总
    print("=" * 78)
    print("候选汇总（test 2d 验证, 要求 WR≥35% 且 EV>0）")
    print("=" * 78)
    for name, r in results.items():
        if r is None:
            continue
        t = r["test2d"]
        mark = " ★" if (t and t["wr"] >= 0.35 and t["ev"] > 0 and t["n"] >= 60) else ""
        print(f"  {name:<46s} train n={r['train']['n']:>4d} "
              f"test2d n={t['n']:>4d} WR {t['wr']*100:>5.1f}% "
              f"EV {t['ev']:+.4f} 双半{'✓' if r['half_ok'] else '✗'}{mark}")


if __name__ == "__main__":
    main()
