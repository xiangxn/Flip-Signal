#!/usr/bin/env python3
"""
v3 follow 线 —— 可解释分桶规则（镜像 03_rules 的 flip 协议）。

目标: 找 follow（买穿越侧）中 EV = WR - fill > 0 的可解释子集。
协议（诚实性关键）:
  1. 规则只在 train 日（≤08-28）上挑选
  2. 在 test 日（08-29/30, 2d）与长测试窗（08-27~08-30, 4d）上验证
  3. 双半同号 + 逐日检查
注意: 特征方向校正（f_ 前缀）是为 flip 定的, follow 有利方向 = f_ 负值。
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from featlib import DATA  # noqa: E402

FILL = "follow_fill10s"
WON = "follow_won"


def load():
    df = pd.read_pickle(BASE / "data" / "featmat.pkl")
    return df[df["follow_fill0s"].notna()]


def stats(d):
    if len(d) == 0:
        return None
    wr = d[WON].mean()
    f_ = d[FILL].mean()
    return dict(n=len(d), wr=wr, fill=f_, ev=wr - f_, both=d["cls"].eq("both").mean())


def show(name, d, tag=""):
    s = stats(d)
    if s is None:
        print(f"  [{name}] 空")
        return s
    mark = " ★" if (s["ev"] > 0 and s["n"] >= 60) else ""
    print(f"  [{name}] n={s['n']:>4d} WR {s['wr']*100:>5.1f}% fill {s['fill']:.3f} "
          f"EV {s['ev']:+.4f} both {s['both']*100:.1f}%{mark}{tag}")
    return s


def rule_stats(df, mask, name):
    print(f"\n=== 规则: {name}  (n={mask.sum()}) ===")
    tr = df[mask & df["date"].le("2026-08-28")]
    te = df[mask & df["date"].isin(("2026-08-29", "2026-08-30"))]
    te4 = df[mask & df["date"].ge("2026-08-27") & df["date"].le("2026-08-30")]
    if not mask.any():
        print("  全空")
        return None
    base_te = stats(df[df["date"].isin(("2026-08-29", "2026-08-30"))])
    print(f"  test 基线: WR {base_te['wr']*100:.1f}% fill {base_te['fill']:.3f} "
          f"EV {base_te['ev']:+.4f}")
    show("train 选样", tr)
    show("test 2d 验证", te)
    show("长窗 4d 验证", te4)
    tdates = sorted(df["date"].unique())
    half_dates = [d for d in tdates if d <= "2026-08-28"]
    m1 = int(len(half_dates) * 0.6)
    h1 = tr[tr["date"].isin(half_dates[:m1])]
    h2 = tr[tr["date"].isin(half_dates[m1:])]
    s1, s2 = stats(h1), stats(h2)
    ok = s1 and s2 and s1["ev"] > 0 and s2["ev"] > 0
    print(f"  双半: H1 {s1['wr']*100:.1f}%/EV{s1['ev']:+.4f}  "
          f"H2 {s2['wr']*100:.1f}%/EV{s2['ev']:+.4f}  {'✓ 同号' if ok else '✗ 反号'}")
    day_rows = []
    for d, g in tr.groupby("date"):
        s = stats(g)
        day_rows.append((d, s["n"], s["wr"], s["ev"]))
    pos_days = sum(1 for _, _, _, e in day_rows if e > 0)
    print(f"  逐日 EV 正: {pos_days}/{len(day_rows)}  "
          + "  ".join(f"{d[5:]}: {w*100:.0f}%/{e:+.2f}" for d, n, w, e in day_rows))
    return dict(train=stats(tr), test2d=stats(te), test4d=stats(te4), half_ok=ok)


def main():
    df = load()
    base = stats(df)
    print(f"数据: {DATA}  |  观测 {len(df)}  |  follow 基线 WR {base['wr']*100:.1f}% "
          f"fill {base['fill']:.3f} EV {base['ev']:+.4f}")

    print("=" * 78)
    print("1D 分桶勘察（train 日 ≤08-28, 目标 EV>0）")
    print("=" * 78)
    tr = df[df["date"].le("2026-08-28")]
    for feat in ["post_end", "trigger_bid", "other_delta10s", "post_aggr_buy_share",
                 "f_accel", "f_slope", "twap_pos", "dep_imbalance", "basis_slope",
                 "path_eff", "spot_pos", "rem", "tape_share", "post_pp_mean",
                 "tape_imb", "basis_cross0", "post_dip"]:
        print(f"\n  ◆ {feat}")
        qs = [tr[feat].quantile(q) for q in (0.2, 0.4, 0.6, 0.8)]
        bounds = [-np.inf] + qs + [np.inf]
        for k in range(5):
            lo, hi = bounds[k], bounds[k + 1]
            if k == 0:
                m = tr[feat].between(lo, hi, inclusive="neither")
            elif k < 4:
                m = tr[feat].gt(lo) & tr[feat].le(hi)
            else:
                m = tr[feat].gt(lo)
            s = stats(tr[m])
            if s is None:
                continue
            mark = " ★" if (s["ev"] > 0 and s["n"] >= 60) else ""
            print(f"    Q{k+1} ({lo:.3f},{hi:.3f}] n={s['n']:>4d} WR {s['wr']*100:>5.1f}% "
                  f"fill {s['fill']:.3f} EV {s['ev']:+.4f}{mark}")

    print("\n" + "=" * 78)
    print("候选规则体检（train 选 / test 验）")
    print("=" * 78)
    candidates = {
        "trigger_bid ≥ 0.80（深触发）":
            df["trigger_bid"].ge(0.80),
        "trigger_bid ≥ 0.85":
            df["trigger_bid"].ge(0.85),
        "post_end ≥ 0.80（+10s 仍强）":
            df["post_end"].ge(0.80),
        "post_end ≥ 0.85":
            df["post_end"].ge(0.85),
        "post_end ≥0.80 & trigger_bid ≥0.75":
            df["post_end"].ge(0.80) & df["trigger_bid"].ge(0.75),
        "f_accel < 0（现货沿触发侧加速）":
            df["f_accel"].lt(0),
        "f_slope < 0（现货趋势沿触发侧）":
            df["f_slope"].lt(0),
        "post_aggr_buy_share ≥ 0.5（触发侧被主动买）":
            df["post_aggr_buy_share"].ge(0.5),
        "other_delta10s ≥ 0（对侧未走强）":
            df["other_delta10s"].ge(0),
        "trigger_bid ≥0.80 & post_aggr_buy_share ≥0.5":
            df["trigger_bid"].ge(0.80) & df["post_aggr_buy_share"].ge(0.5),
        "twap_pos > 0（TWAP 沿触发侧）":
            df["twap_pos"].gt(0),
        "rem ≤ 120（晚窗口）":
            df["rem"].le(120),
        "post_end ≥0.80 & f_accel <0":
            df["post_end"].ge(0.80) & df["f_accel"].lt(0),
        "post_end ≥0.80 & other_delta10s ≥0":
            df["post_end"].ge(0.80) & df["other_delta10s"].ge(0),
    }
    results = {}
    for name, mask in candidates.items():
        results[name] = rule_stats(df, mask, name)
        print()

    print("=" * 78)
    print("候选汇总（test 2d 验证, 要求 EV>0）")
    print("=" * 78)
    for name, r in results.items():
        if r is None:
            continue
        t = r["test2d"]
        mark = " ★" if (t and t["ev"] > 0 and t["n"] >= 60) else ""
        print(f"  {name:<42s} train n={r['train']['n']:>4d} "
              f"test2d n={t['n']:>4d} WR {t['wr']*100:>5.1f}% "
              f"EV {t['ev']:+.4f} 双半{'✓' if r['half_ok'] else '✗'}{mark}")


if __name__ == "__main__":
    main()
