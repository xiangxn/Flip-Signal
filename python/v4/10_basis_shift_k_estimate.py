#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Basis 平移系数 k 的直接估计（2026-09-17）—— 回答「模型 A（阈值固定）还是模型 B（随 Basis 平移）」。

用户模型（a.md §2-5）：历史带是在某个基准 basis 下标定的，
    ΔDistS = (B − B₀) / Scale,      Scale = Anchor·histBps/10000
量纲核对：dist_s 本身已除以 histBps（σ 的价差尺度）→ Δ ≡ basis_σ（当前 basis, 以 σ 计），
         而 (B₀/Scale) 只是一个常数偏移 → 被带下限 lo 吸收（lo 本来就要拟合）。
于是模型族坍缩成**一个自由参数**：

    z_k ≡ dist_s − k·basis_σ = dist_t + (1−k)·basis_σ        z_k ∈ (lo, 0)
      k=0 → 坐标 dist_s（引擎现行 = 模型 A：阈值固定）
      k=1 → 坐标 dist_t（模型 B 完整平移：阈值按 basis 1:1 移动）

⚠️ 注意两件事（与 08 的关系）：
  ① z_k 带 ≡ 在 dist_s 上把**整条带两条边一起**平移 +k·basis_σ —— 这正是「坐标系平移」，
     不是只挪 lo。08 的坐标替换与它是同一个东西（代数上等价）。
  ② 08 只测了 k 的两个端点（k=0 与 k=1）。k 的**中间段、k̂ 的估计值、以及 k 的可识别性**
     从未测过 —— 本脚本补这一块。

四个判据（判据优先级递增）：
  【2】带内 EV vs basis 分桶 —— 局部斜率，直接给 k 定号（不依赖任何 argmax）
       A → 平坦；B → 倒 U（basis 偏离历史基准越远，带越错位，EV 越低）
  【3】k 全网格扫描（in-sample argmax）—— 看 k̂ 落在哪、曲线是平台还是尖峰
  【4】h1 选 (k, lo) → h2 验证（带同过程置换零分布）—— 诚实的样本外判据
  【5】按 basis 分层各自 refit lo* —— 非参数地读「带随 basis 移动了多少」
       A → 斜率 0；B → 斜率 +1（下界随 basis 上移）

用法: python 10_basis_shift_k_estimate.py [--data <事件目录>] [--stake 2] [--perm 300]
"""
import argparse
import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent

# 01_backtest_r1.py 是权威提取/口径源（模块级只有定义，main 有守卫）
_spec = importlib.util.spec_from_file_location("r1", BASE / "01_backtest_r1.py")
r1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(r1)

CRASH_MIN = r1.CRASH_MIN
REM_MIN = 180              # 历史基线时间腿（勿随 01 模块默认漂移）
# 01 的 main() 会把 REM_MAX 设为 args（默认 None）; import 拿到的是模块默认 240
# —— 必须显式还原为头条口径（07 脚本 :385 记的同款坑）
r1.REM_MAX = None
r1.REM_MIN = REM_MIN

CUR = {"yes": -0.6, "no": -1.0}        # 引擎现行组合带（in-sample 定带, 09-03）
K_GRID = [round(x, 2) for x in np.arange(-0.5, 1.51, 0.1)]


def pl_of(s, stake):
    """2U/注收益数组（赢 → shares−stake; 输 → −stake）。"""
    if len(s) == 0:
        return np.zeros(0)
    return np.where(s["settle_won"] == 1, stake / s["fill"] - stake, -stake)


def summ(s, stake):
    """(n, WR, EV U/注, P&L)。"""
    if len(s) == 0:
        return (0, 0.0, 0.0, 0.0)
    pl = pl_of(s, stake)
    return (len(s), s["settle_won"].mean(), pl.mean(), pl.sum())


def zk(df, k):
    """z_k = dist_s − k·basis（basis 列由 main 补齐）。"""
    return df["dist_s"] - k * df["basis"]


def band_of(df, k, lo):
    z = zk(df, k)
    return z.notna() & (z > lo) & (z < 0)


def picks_of(df, k, los, stake, min_n=20):
    """给定 k，双侧独立扫 lo → {'yes': lo*, 'no': lo*}（P&L argmax）。"""
    picks = {}
    for side in ("yes", "no"):
        sub = df[df["side"] == side]
        z = zk(sub, k)
        best = None
        for lo in los:
            m = (z.notna() & (z > lo) & (z < 0)).to_numpy()
            nn = int(m.sum())
            if nn < min_n:
                continue
            p = pl_of(sub[m], stake).sum()
            if best is None or p > best[1]:
                best = (lo, p)
        picks[side] = best[0] if best else None
    return picks


def apply_picks(df, k, picks, stake, extra=None):
    m = df["m_45"].ge(CRASH_MIN) & df["rem"].gt(REM_MIN) & df["dist_s"].notna()
    side_ok = np.zeros(len(df), bool)
    for side, lo in picks.items():
        if lo is None:
            continue
        side_ok |= (df["side"] == side).to_numpy() & band_of(df, k, lo).to_numpy()
    m = np.asarray(m & side_ok).copy()      # pandas 的布尔底层数组可能只读 → 必须 copy
    if extra is not None:
        m &= extra
    return df[m]


def paired_bootstrap(day_a, day_b, B=3000, seed=42):
    """按日配对重采样 P&L 差值（a−b）→ 95% CI。"""
    dates = sorted(set(day_a) | set(day_b))
    obs = np.array([day_a.get(d, 0.0) - day_b.get(d, 0.0) for d in dates])
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(dates), size=(B, len(dates)))
    return obs.sum(), np.percentile(obs[idx].sum(axis=1), [2.5, 97.5])


def main():
    ap = argparse.ArgumentParser(description="Basis 平移系数 k 的直接估计")
    ap.add_argument("--data", default=str(BASE.parent.parent / "data" / "btc"))
    ap.add_argument("--stake", type=float, default=r1.STAKE)
    ap.add_argument("--grid-lo", type=float, default=-1.6)
    ap.add_argument("--perm", type=int, default=300)
    ap.add_argument("--seed", type=int, default=23)
    ap.add_argument("--skip-perm", action="store_true")
    args = ap.parse_args()
    stake = args.stake
    los = [round(x, 3) for x in np.arange(args.grid_lo, 0.0, 0.05)]
    rng = np.random.default_rng(args.seed)

    df = r1.extract(args.data)
    df["basis"] = df["dist_s"] - df["dist_t"]
    ndays = df["date"].nunique()
    pop = df[(df["m_45"] >= CRASH_MIN) & (df["rem"] > REM_MIN)
             & df["dist_s"].notna() & df["dist_t"].notna()].copy()

    print(f"数据 {args.data}: {ndays} 天   判定总体 n={len(pop)}"
          f"  side {pop.side.value_counts().to_dict()}\n")

    # ================= 【0】量纲与坐标核对 =================
    print("=" * 78)
    print("【0】量纲核对：ΔDistS = (B−B₀)/Scale ≡ basis_σ")
    print("=" * 78)
    print("  Scale = Anchor·histBps/10000 就是 σ 的价差尺度 → Δ = basis(价差) / Scale = basis_σ")
    print("  B₀ 那份是常数偏移 → 被 lo 吸收 → 全族只剩一个自由参数 k")
    print("  z_k ≡ dist_s − k·basis = dist_t + (1−k)·basis")
    d0 = float((zk(pop, 0.0) - pop["dist_s"]).abs().max())
    d1 = float((zk(pop, 1.0) - pop["dist_t"]).abs().max())
    print(f"  数值核对: max|z_0 − dist_s| = {d0:.2e}   max|z_1 − dist_t| = {d1:.2e}"
          f"   → {'✅ 恒等式成立' if max(d0, d1) < 1e-9 else '❌'}")
    print("  ⚠️ z_k 带 = 在 dist_s 上把**两条边一起**平移 +k·basis（不只是挪 lo）")

    # ================= 【1】可识别性：k 有多少分辨率 =================
    print("\n" + "=" * 78)
    print("【1】可识别性：数据能把 k 分辨到多细？")
    print("=" * 78)
    ds, dt, bz = pop["dist_s"], pop["dist_t"], pop["basis"]
    print(f"  Var(dist_s)={ds.var():.4f}   Var(dist_t)={dt.var():.4f}"
          f"   Var(basis)={bz.var():.4f}")
    print(f"  Var(dist_t)/Var(dist_s) = {dt.var()/ds.var()*100:.1f}%"
          f"   ← k 的**信号空间**（dist_t 占 dist_s 的方差比例）")
    print(f"  ρ(dist_s, basis) = {np.corrcoef(ds, bz)[0,1]:+.3f}"
          f"   ρ(dist_t, basis) = {np.corrcoef(dt, bz)[0,1]:+.3f}")

    # 现行带成员在 k 步进下的换手率 = k 的实际分辨率
    cur_m = (((pop["side"] == "yes") & band_of(pop, 0.0, CUR["yes"]))
             | ((pop["side"] == "no") & band_of(pop, 0.0, CUR["no"]))).to_numpy()
    cur_n = int(cur_m.sum())
    print(f"\n  现行 k=0 组合带 n={cur_n}；带内 basis 分布: "
          f"p25 {bz[cur_m].quantile(.25):+.2f}  中位 {bz[cur_m].median():+.2f}"
          f"  p75 {bz[cur_m].quantile(.75):+.2f}  std {bz[cur_m].std():.2f}")
    print(f"    → 带内 basis 的 std 就是估计 k 的**力臂**（力臂越小 k 越估不准）")
    print(f"\n  k 每步进 0.1σ，现行带成员的换手率（越接近 0 → k 越不可分辨）:")
    line = "   "
    for k in [0.1, 0.2, 0.3, 0.5, 0.7, 1.0]:
        m2 = (((pop["side"] == "yes").to_numpy() & band_of(pop, k, CUR["yes"]).to_numpy())
              | ((pop["side"] == "no").to_numpy() & band_of(pop, k, CUR["no"]).to_numpy()))
        churn = float((m2 != cur_m).sum()) / max(cur_n, 1)
        line += f"  k={k:.1f}: {churn*100:5.1f}%"
    print(line)

    # ================= 【2】判据一：带内 EV vs basis（局部斜率）=================
    print("\n" + "=" * 78)
    print("【2】判据一（最直接）：现行固定带内，EV 随 basis 怎么走")
    print("=" * 78)
    print("  模型 A（阈值固定）→ 带内 EV 对 basis 平坦")
    print("  模型 B（1:1 平移）→ basis 偏离历史基准越远带越错位 → EV 呈**倒 U**")
    cb = pop[cur_m].copy()
    q = pd.qcut(cb["basis"], 5, labels=False, duplicates="drop")
    print(f"\n  {'basis 分位':<22}{'n':>5}{'WR':>8}{'EV':>10}{'P&L':>9}{'basis中位':>11}")
    for g in sorted(pd.Series(q).dropna().unique()):
        sub = cb[q == g]
        n, wr, ev, p = summ(sub, stake)
        lo_b = cb[q == g]["basis"].min()
        hi_b = cb[q == g]["basis"].max()
        print(f"  [{lo_b:+.2f},{hi_b:+.2f}){'':<8}{n:>5}{wr*100:>7.1f}%{ev:>+10.3f}"
              f"{p:>+9.1f}{sub['basis'].median():>+11.2f}")
    # 线性斜率 + 按日 bootstrap CI
    x, y = cb["basis"].to_numpy(), pl_of(cb, stake)
    slope = float(np.polyfit(x, y, 1)[0]) if len(x) > 5 else float("nan")
    boot = []
    for _ in range(3000):
        idx = rng.integers(0, len(x), len(x))
        if x[idx].std() > 1e-9:
            boot.append(np.polyfit(x[idx], y[idx], 1)[0])
    ci = np.percentile(boot, [2.5, 97.5]) if boot else [np.nan, np.nan]
    print(f"\n  带内 OLS 斜率 d(EV)/d(basis) = {slope:+.3f} U/注 per σ"
          f"   95%CI [{ci[0]:+.3f},{ci[1]:+.3f}]"
          f"   {'含 0 → 平坦（支持 A）' if ci[0] <= 0 <= ci[1] else '不含 0'}")

    # ================= 【3】判据二：k 全网格扫描（in-sample）=================
    print("\n" + "=" * 78)
    print("【3】判据二：k 全网格扫描（每侧独立 refit lo, 全样本 argmax）—— in-sample 参照")
    print("=" * 78)
    print(f"  {'k':>6}{'带 yes/no':>18}{'n':>6}{'WR':>8}{'EV':>10}{'P&L':>10}")
    curve = {}
    for k in K_GRID:
        pk = picks_of(pop, k, los, stake)
        s = apply_picks(pop, k, pk, stake)
        n, wr, ev, p = summ(s, stake)
        curve[k] = (pk, n, wr, ev, p)
        mark = "  ← 现行 k=0" if abs(k) < 1e-9 else ("  ← 模型B k=1" if abs(k - 1.0) < 1e-9 else "")
        band_s = f"{pk['yes']}/{pk['no']}"
        print(f"  {k:>+6.2f}{band_s:>18}{n:>6}{wr*100:>7.1f}%{ev:>+10.3f}{p:>+10.1f}{mark}")
    k_best = max(curve, key=lambda kk: curve[kk][4])
    pkb, nb, wb, eb, pb = curve[k_best]
    print(f"\n  in-sample argmax k̂ = {k_best:+.2f}  n={nb} WR {wb*100:.1f}% "
          f"EV {eb:+.3f} P&L {pb:+.1f}U")
    print(f"  k=0 现行 P&L {curve[0.0][4]:+.1f}U   k=1 模型B P&L "
          f"{curve.get(1.0, (None,0,0,0,float('nan')))[4]:+.1f}U"
          f"   → k̂ 增益 {pb - curve[0.0][4]:+.1f}U（in-sample，不可直接信）")

    # ================= 【4】判据三：h1 选 (k,lo) → h2 验证 + 置换零分布 ==========
    print("\n" + "=" * 78)
    print("【4】判据三：h1 选 (k, lo) → h2 验证（反向同做）")
    print("=" * 78)
    if args.skip_perm:
        args.perm = 0
    for sel_h, ver_h in (("h1", "h2"), ("h2", "h1")):
        hsel = pop[pop["h"] == sel_h]
        hver_mask = (pop["h"] == ver_h).to_numpy()
        best = None
        for k in K_GRID:
            pk = picks_of(hsel, k, los, stake)
            s = apply_picks(pop, k, pk, stake, extra=hver_mask)
            n, wr, ev, p = summ(s, stake)
            if best is None or p > best[5]:
                best = (k, pk, n, wr, ev, p)
        kk, pk, n, wr, ev, p = best
        arm0 = apply_picks(pop, 0.0, picks_of(hsel, 0.0, los, stake), stake,
                           extra=hver_mask)
        arm1 = apply_picks(pop, 1.0, picks_of(hsel, 1.0, los, stake), stake,
                           extra=hver_mask)
        print(f"  {sel_h} 选 → {ver_h} 验:")
        print(f"    k̂={kk:+.2f} 带 yes({pk['yes']})/no({pk['no']})  "
              f"n={n} WR {wr*100:.1f}% EV {ev:+.3f} P&L {p:+.1f}U")
        print(f"    k=0 臂 n={len(arm0)} P&L {pl_of(arm0, stake).sum():+.1f}U   |   "
              f"k=1 臂 n={len(arm1)} P&L {pl_of(arm1, stake).sum():+.1f}U")
        a = {d: pl_of(g, stake).sum() for d, g in
             apply_picks(pop, kk, pk, stake, extra=hver_mask).groupby("date")}
        b = {d: pl_of(g, stake).sum() for d, g in arm0.groupby("date")}
        d1, ci1 = paired_bootstrap(a, b)
        print(f"    vs k=0 臂 按日配对 Δ{d1:+.1f}U [{ci1[0]:+.1f},{ci1[1]:+.1f}]"
              f"   {'含 0' if ci1[0] <= 0 <= ci1[1] else '不含0'}")

    if args.perm:
        print(f"\n  置换零分布（打乱 settle_won, 同一整套 (k,lo) 选择过程, "
              f"{args.perm} 次）—— 这是「k 也是拟合出来的」的对口零假设:")
        for sel_h, ver_h in (("h1", "h2"), ("h2", "h1")):
            hsel_idx = (pop["h"] == sel_h).to_numpy()
            hver_idx = (pop["h"] == ver_h).to_numpy()
            base = pop[["side", "settle_won"]].copy()
            null = np.empty(args.perm)
            for t in range(args.perm):
                frame = pop.copy()
                frame["settle_won"] = (base.groupby("side")["settle_won"]
                                       .transform(lambda s: rng.permutation(s.to_numpy()))
                                       ).to_numpy()
                best = -np.inf
                for k in K_GRID:
                    pk = picks_of(frame[hsel_idx], k, los, stake)
                    s = apply_picks(frame, k, pk, stake, extra=hver_idx)
                    if len(s) >= 20:
                        best = max(best, pl_of(s, stake).sum())
                null[t] = best if np.isfinite(best) else 0.0
            obs = -np.inf
            for k in K_GRID:
                pk = picks_of(pop[hsel_idx], k, los, stake)
                s = apply_picks(pop, k, pk, stake, extra=hver_idx)
                if len(s) >= 20:
                    obs = max(obs, pl_of(s, stake).sum())
            pv = float((null >= obs).mean())
            print(f"    {sel_h}→{ver_h}: 观测 {obs:+7.1f}U | 零分布 p50 {np.median(null):+7.1f}U"
                  f"  p95 {np.percentile(null, 95):+7.1f}U"
                  f"  → p={pv:.3f} {'✅ 超噪声' if pv < 0.05 else '❌ 与拟合噪声不可区分'}")

    # ================= 【5】判据四：按 basis 分层各自 refit lo* =================
    print("\n" + "=" * 78)
    print("【5】判据四（非参数）：按 basis 分层，各自 refit lo* —— 带随 basis 移动了多少")
    print("=" * 78)
    print("  模型 A → lo*(basis) 斜率 ≈ 0；模型 B → 斜率 ≈ +1（下界随 basis 1:1 上移）")
    for side in ("yes", "no"):
        sub = pop[pop["side"] == side].copy()
        terc = pd.qcut(sub["basis"], 3, labels=False, duplicates="drop")
        print(f"\n  {side} 侧（n={len(sub)}）:")
        print(f"    {'basis 层':<20}{'n':>5}{'lo*':>8}{'n*':>5}{'EV*':>9}"
              f"{'EV@现行lo':>11}{'basis中位':>11}")
        xs, ys = [], []
        for g in sorted(pd.Series(terc).dropna().unique()):
            s3 = sub[terc == g]
            rows = []
            for lo in los:
                m = band_of(s3, 0.0, lo).to_numpy()
                if m.sum() >= 15:
                    rows.append((lo, int(m.sum()), pl_of(s3[m], stake).sum()))
            if not rows:
                continue
            lo_star = max(rows, key=lambda r: r[2])
            mstar = band_of(s3, 0.0, lo_star[0]).to_numpy()
            n, wr, ev, p = summ(s3[mstar], stake)
            mcur = band_of(s3, 0.0, CUR[side]).to_numpy()
            n2, _w2, ev2, _p2 = summ(s3[mcur], stake)
            bmed = s3["basis"].median()
            lo_b, hi_b = s3["basis"].min(), s3["basis"].max()
            print(f"    [{lo_b:+.2f},{hi_b:+.2f}){'':<5}{len(s3):>5}{lo_star[0]:>+8.2f}"
                  f"{n:>5}{ev:>+9.3f}{ev2:>+11.3f}{bmed:>+11.2f}")
            xs.append(bmed)
            ys.append(lo_star[0])
        if len(xs) >= 3:
            sl = float(np.polyfit(xs, ys, 1)[0])
            print(f"    → lo*(basis) 斜率 = {sl:+.2f}"
                  f"   （A 期望 ≈0，B 期望 ≈+1；3 点估计，仅作方向提示）")

    # ================= 【6】小结 =================
    print("\n" + "=" * 78)
    print("【6】小结")
    print("=" * 78)
    print(f"  k 的力臂（带内 basis std）= {bz[cur_m].std():.2f}σ；"
          f"Var(dist_t)/Var(dist_s) = {dt.var()/ds.var()*100:.1f}%")
    print(f"  in-sample k̂ = {k_best:+.2f}（P&L {pb:+.1f}U vs k=0 的 {curve[0.0][4]:+.1f}U）")
    print("  → 判据 4/5 是否支持 k≠0，见上两节；判据 2 的 CI 是否含 0 决定 k 的符号是否可辨。")


if __name__ == "__main__":
    main()
