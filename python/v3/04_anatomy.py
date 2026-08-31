#!/usr/bin/env python3
"""
v3 阶段 4 —— 机制解剖（flip 单线, 目标 WR ≥ 35% 且 EV > 0）。

聚焦阶段 3 唯一在 train 上达标且机制可解释的候选: post_end（穿越侧 +10s bid）低桶
——"穿越在确认时刻已失败, 买 flip"。做:
  1. post_end 阈值扫描（train 选 / test 2d / 长窗 4d 验证）
  2. 与其它 top 特征的 2D 组合
  3. 最终候选机制解剖: both 率 / outcome 分布 / fill 分解 vs 基线 → 真预测 or fill 残渣
  4. 低 fill 角度（D1 方向: 便宜 flip, WR 低但 EV 可能正）
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from featlib import DATA  # noqa: E402


def load():
    df = pd.read_pickle(BASE / "data" / "featmat.pkl")
    return df[df["flip_fill0s"].notna()]


def stats(d, fill_key="flip_fill10s"):
    if len(d) == 0:
        return None
    wr = d["flip_won"].mean()
    f_ = d[fill_key].mean()
    return dict(n=len(d), wr=wr, fill=f_, ev=wr - f_,
                both=d["cls"].eq("both").mean(),
                up=d["side"].eq("yes").eq(d["cls"]).astype(int).mean() if False else None)


def main():
    df = load()
    tr = df[df["date"].le("2026-08-28")]
    te = df[df["date"].isin(("2026-08-29", "2026-08-30"))]
    te4 = df[df["date"].ge("2026-08-27") & df["date"].le("2026-08-30")]

    print(f"数据: {DATA}  |  观测 {len(df)}  |  基线 WR {df['flip_won'].mean()*100:.1f}%")
    print("=" * 78)
    print("1) post_end 阈值扫描（决策 +10s, fill = flip_fill10s）")
    print("=" * 78)
    print(f"  {'阈值':<10s} {'train':>26s} | {'test2d':>26s} | {'长窗4d':>26s}")
    for thr in (0.76, 0.72, 0.70, 0.68, 0.65, 0.62, 0.60, 0.55, 0.50):
        row = []
        for d in (tr, te, te4):
            m = d[d["post_end"].le(thr)]
            s = stats(m)
            row.append(f"n={s['n']:>4d} WR {s['wr']*100:>5.1f}% f {s['fill']:.3f} EV {s['ev']:+.4f}"
                       if s else "空")
        print(f"  ≤{thr:<8.2f} {row[0]:<30s} | {row[1]:<30s} | {row[2]}")

    print()
    print("=" * 78)
    print("2) post_end ≤ 0.68 × 其它 top 特征（train 选样）")
    print("=" * 78)
    base_df = tr[tr["post_end"].le(0.68)]
    bs = stats(base_df)
    print(f"  post_end≤0.68 基线: n={bs['n']} WR {bs['wr']*100:.1f}% fill {bs['fill']:.3f} "
          f"EV {bs['ev']:+.4f} both {bs['both']*100:.1f}%")
    top2d = []
    for feat in ["other_delta10s", "f_accel", "f_dratio5", "flow_pre", "path_eff",
                 "twap_pos", "dep_imbalance", "trigger_bid", "basis_slope", "rem",
                 "post_aggr_buy_share", "pre_aggr_buy_share", "obi5", "cross_speed_s"]:
        base_f = base_df[base_df[feat].notna()]
        if len(base_f) < len(base_df) * 0.9 or base_f[feat].nunique() < 5:
            print(f"\n  ◆ post_end≤0.68 & {feat}  （有效样本不足, 跳过）")
            continue
        qs = [base_f[feat].quantile(q) for q in (0.33, 0.66)]
        if qs[0] == qs[1]:
            print(f"\n  ◆ post_end≤0.68 & {feat}  （分位数重合, 跳过）")
            continue
        print(f"\n  ◆ post_end≤0.68 & {feat}")
        for name, lo, hi in (("低", -np.inf, qs[0]), ("中", qs[0], qs[1]), ("高", qs[1], np.inf)):
            m = base_f[base_f[feat].gt(lo) & base_f[feat].le(hi)] if name == "中" else \
                base_f[base_f[feat].le(hi)] if name == "低" else base_f[base_f[feat].gt(lo)]
            s = stats(m)
            if s and s["ev"] > 0 and s["wr"] >= 0.35 and s["n"] >= 60:
                top2d.append((f"{feat}={name}", lo, hi, s))
            if s is None:
                continue
            mark = " ★" if (s and s["wr"] >= 0.40 and s["ev"] > 0 and s["n"] >= 60) else ""
            print(f"    {name:<3s} n={s['n']:>4d} WR {s['wr']*100:>5.1f}% fill {s['fill']:.3f} "
                  f"EV {s['ev']:+.4f}{mark}")

    print()
    print("=" * 78)
    print("2b) train 上 WR≥35% & EV>0 的 2D 组合 → test 验证")
    print("=" * 78)
    for fname, lo, hi, s in sorted(top2d, key=lambda x: -x[3]["ev"])[:12]:
        t_mask = df["post_end"].le(0.68) & df[fname.split("=")[0]].gt(lo) & \
                 df[fname.split("=")[0]].le(hi)
        # 处理"低/高"开区间
        if fname.endswith("=低"):
            t_mask = df["post_end"].le(0.68) & df[fname.split("=")[0]].le(hi)
        elif fname.endswith("=高"):
            t_mask = df["post_end"].le(0.68) & df[fname.split("=")[0]].gt(lo)
        st, ste, ste4 = stats(df[t_mask & df["date"].le("2026-08-28")]), \
                        stats(df[t_mask & df["date"].isin(("2026-08-29", "2026-08-30"))]), \
                        stats(df[t_mask & df["date"].ge("2026-08-27") & df["date"].le("2026-08-30")])
        ok = ste and ste["wr"] >= 0.35 and ste["ev"] > 0 and ste["n"] >= 60
        print(f"  post_end≤0.68 & {fname}: "
              f"train n={st['n']} WR {st['wr']*100:.1f}% EV {st['ev']:+.4f} | "
              f"test2d n={ste['n']} WR {ste['wr']*100:.1f}% EV {ste['ev']:+.4f} | "
              f"长窗 n={ste4['n']} WR {ste4['wr']*100:.1f}% EV {ste4['ev']:+.4f}"
              + ("  ★" if ok else ""))

    print()
    print("=" * 78)
    print("3) 最终候选机制解剖: post_end ≤ 0.68（决策 +10s, fill = flip_fill10s）")
    print("=" * 78)
    mask = df["post_end"].le(0.68)
    cand = df[mask]
    print(f"  全量: n={len(cand)}  WR {cand['flip_won'].mean()*100:.1f}%  "
          f"fill {cand['flip_fill10s'].mean():.3f}  EV {cand['flip_won'].mean()-cand['flip_fill10s'].mean():+.4f}")
    print(f"  基线: n={len(df)}    WR {df['flip_won'].mean()*100:.1f}%  "
          f"fill {df['flip_fill10s'].mean():.3f}")
    # 分层: both 率 / outcome
    for d, tag in ((tr, "train"), (te, "test2d"), (te4, "长窗4d")):
        m = d[mask]
        s = stats(m)
        b = stats(d)
        print(f"\n  [{tag}] 候选 vs 基线:")
        print(f"    both 率: 候选 {s['both']*100:.1f}%  vs  基线 {b['both']*100:.1f}%  "
              f"Δ{(s['both']-b['both'])*100:+.1f}pp")
        # outcome 分布（Up 占比）
        up_c = m["side"].eq("yes").eq(m["flip_won"].eq(0)).mean()  # 穿越侧=YES 且赢(=Up)
        up_b = d["side"].eq("yes").eq(d["flip_won"].eq(0)).mean()
        print(f"    Up 占比: 候选 {up_c*100:.1f}%  vs  基线 {up_b*100:.1f}%")
        # fill 分解
        print(f"    fill:    候选 {s['fill']:.3f}  vs  基线 {b['fill']:.3f}  "
              f"Δ{s['fill']-b['fill']:+.3f}")
        # 逐日
        rows = []
        for dt, g in m.groupby("date"):
            s2 = stats(g)
            rows.append(f"{dt[5:]}:{s2['wr']*100:.0f}%/{s2['ev']:+.2f}")
        pos = sum(1 for r in rows if float(r.split('/')[1].split('+')[1] if '+' in r.split('/')[1] else r.split('/')[1].replace('-','-0').replace('0.','0.')) > 0)
        print(f"    逐日: {' '.join(rows)}")
    # 双半（train 前 60% vs 后 40%）
    half = [d for d in sorted(tr["date"].unique())]
    h1 = tr[tr["date"].isin(half[:int(len(half)*0.6)]) & mask]
    h2 = tr[tr["date"].isin(half[int(len(half)*0.6):]) & mask]
    s1, s2 = stats(h1), stats(h2)
    print(f"\n  双半: H1 n={s1['n']} WR {s1['wr']*100:.1f}% EV {s1['ev']:+.4f}  |  "
          f"H2 n={s2['n']} WR {s2['wr']*100:.1f}% EV {s2['ev']:+.4f}  "
          f"{'✓ 同号' if s1['ev'] > 0 and s2['ev'] > 0 else '✗ 反号'}")

    print()
    print("=" * 78)
    print("4) 低 fill 角度: flip 买入价 ≤ 阈值（决策 +10s）")
    print("=" * 78)
    for thr in (0.15, 0.18, 0.20, 0.25):
        row = []
        for d in (tr, te, te4):
            m = d[d["flip_fill10s"].le(thr)]
            s = stats(m)
            row.append(f"n={s['n']:>4d} WR {s['wr']*100:>5.1f}% EV {s['ev']:+.4f}" if s else "空")
        print(f"  fill≤{thr:<6.2f} {'  '.join(row)}")


if __name__ == "__main__":
    main()
