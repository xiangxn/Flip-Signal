#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
浅洞腿「口径重标定」对照（2026-09-16）——回答 09-03 没做完的那个实验。

背景（2026-09-16 纸面诊断）：dist_s ≡ dist_t + 基差项（三个量同带 sgn/anchor/σ）。
全纸面 n=3639 实测 Var(基差)/Var(dist_s)=83%、ρ(dist_s,dist_t)=+0.25、带内信号
dist_t 中位仅 −0.14σ → 引擎现行「浅洞腿」主要量的是「现货相对 TWAP-60 的瞬时偏离」，
不是「结算依据（TWAP）相对锚走了多远」。09-03 的结论「去基差后 alpha 消失
（EV +1.08 → +0.06）」是**直接套用同一组带宽**得到的，带内 dist_t 中位只 −0.14σ
意味着那个带对 dist_t 坐标根本没标定过 → 该结论未被干净检验。

本脚本：在「急跌(m_45≥0.40) × 时间(rem>180) 已过、只剩浅洞腿定生死」的判定总体上，
把三条坐标各自**重新标定**同形状带 (−x, 0)，同台对比：
  A. dist_s   （引擎现行口径：现货 vs 锚）
  B. dist_t   （结算线口径 = 「洞深」：TWAP vs 锚）
  C. 基差项    （= dist_s − dist_t：现货相对 TWAP-60 的领先量）
判据优先级：**h1 选阈 → h2 验证（及反向）**——单看 14 天全样本 argmax 是 in-sample
红利（见 dog020-band-threshold-robustness：1% 网格达 90% 最优、分半 argmax 跳动）。

用法: python 08_dist_t_band_refit.py [--data <事件目录>] [--stake 2] [--grid-lo -1.6]
"""
import argparse
import importlib.util
import json
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent

# 01_backtest_r1.py 是权威提取/口径源（模块级只有定义，main 有守卫）
_spec = importlib.util.spec_from_file_location("r1", BASE / "01_backtest_r1.py")
r1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(r1)

CRASH_MIN = r1.CRASH_MIN
REM_MIN = 180            # 历史基线时间腿（勿随 01 模块默认漂移）
# 01 的 main() 会把 REM_MAX 设为 args（默认 None）; import 拿到的是模块默认 240
# —— 必须显式还原为头条口径（07_source_health_check.py:385 记的同款坑）
r1.REM_MAX = None
r1.REM_MIN = REM_MIN

COORDS = ("dist_s", "dist_t", "basis")


def pl_of(s, stake):
    """2U/注收益数组（赢 → shares−stake; 输 → −stake）。"""
    return np.where(s["settle_won"] == 1, stake / s["fill"] - stake, -stake)


def summ(s, stake):
    """(n, WR, EV U/注, P&L)。"""
    if len(s) == 0:
        return (0, 0.0, 0.0, 0.0)
    pl = pl_of(s, stake)
    return (len(s), s["settle_won"].mean(), pl.mean(), pl.sum())


def band_mask(df, coord, lo, hi=0.0):
    v = df[coord]
    return v.notna() & (v > lo) & (v < hi)


def scan(df, coord, los, stake, restrict=None):
    """带 (−x, 0) 的 lo 网格扫描 → [(lo, n, WR, EV, P&L)]（可选限定样本）。"""
    out = []
    for lo in los:
        m = band_mask(df, coord, lo)
        if restrict is not None:
            m &= restrict
        out.append((lo,) + summ(df[m], stake))
    return out


def argmax_pl(rows, min_n=20):
    """按 P&L 选 lo（样本量下限防单点）。"""
    cand = [r for r in rows if r[1] >= min_n]
    return max(cand, key=lambda r: r[4])[0] if cand else None


def paired_bootstrap(day_a, day_b, B=3000, seed=42):
    """按日配对重采样 P&L 差值（a−b）→ 95% CI。day_* 为 {date: pl}。"""
    dates = sorted(set(day_a) | set(day_b))
    obs = np.array([day_a.get(d, 0.0) - day_b.get(d, 0.0) for d in dates])
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(dates), size=(B, len(dates)))
    boots = obs[idx].sum(axis=1)
    return obs.sum(), np.percentile(boots, [2.5, 97.5])


def side_split(df, coord, los, stake, halves=None):
    """每侧独立扫 lo（组合带 = yes 用 yes 的 lo / no 用 no 的 lo）。
    halves 非空时只在该半样本上选阈（供 h1 选阈 → h2 验证）。"""
    picks = {}
    for side in ("yes", "no"):
        sub = df[df["side"] == side]
        if halves is not None:
            sub = sub[sub["h"] == halves]
        rows = scan(sub, coord, los, stake,
                    restrict=(sub["m_45"] >= CRASH_MIN) & (sub["rem"] > REM_MIN))
        picks[side] = argmax_pl(rows)
    return picks


def apply_bands(df, coord, picks, stake, extra=None):
    """组合带选取 → 子样本。"""
    m = (df["m_45"] >= CRASH_MIN) & (df["rem"] > REM_MIN) & df[coord].notna()
    side_ok = np.zeros(len(df), bool)
    for side, lo in picks.items():
        if lo is None:
            continue
        side_ok |= (df["side"] == side) & band_mask(df, coord, lo)
    m &= side_ok
    if extra is not None:
        m &= extra
    return df[m]


def main():
    ap = argparse.ArgumentParser(description="浅洞腿口径重标定对照")
    ap.add_argument("--data", default=str(BASE.parent.parent / "data" / "btc"))
    ap.add_argument("--stake", type=float, default=r1.STAKE)
    ap.add_argument("--grid-lo", type=float, default=-1.6, help="lo 扫描下界")
    ap.add_argument("--grid-step", type=float, default=0.05)
    ap.add_argument("--detail", action="store_true",
                    help="逐 lo 打印扫描曲线（判「平台 vs 噪声尖峰」）")
    ap.add_argument("--paper-dir", default=str(BASE.parent.parent / "data" / "v4"),
                    help="纸面数据目录（默认 data/v4; 置空跳过【F】样本外应用）")
    args = ap.parse_args()

    stake = args.stake
    los = [round(x, 3) for x in
           np.arange(args.grid_lo, 0.0, args.grid_step)]

    df = r1.extract(args.data)
    df["basis"] = df["dist_s"] - df["dist_t"]
    ndays = df["date"].nunique()
    print(f"数据 {args.data}: 事件 → 0.2 首触 {len(df)}（{ndays} 天）")
    print(f"时间腿 rem > {REM_MIN}（REM_MAX 显式还原 None）  带形状 (−x, 0)")

    # 判定总体：急跌 × 时间已过（只剩浅洞腿定生死）
    pop = df[(df["m_45"] >= CRASH_MIN) & (df["rem"] > REM_MIN)].copy()
    popd = pop[pop["dist_s"].notna() & pop["dist_t"].notna()]
    print(f"\n判定总体: {len(pop)}  其中 dist_s/dist_t 均有值 {len(popd)}"
          f"（缺 {(len(pop)-len(popd))}）  side {popd.side.value_counts().to_dict()}")
    print(f"  |dist_s| 中位 {popd.dist_s.abs().median():.2f}σ   "
          f"|dist_t| 中位 {popd.dist_t.abs().median():.2f}σ   "
          f"|basis| 中位 {popd.basis.abs().median():.2f}σ")

    # 【A】基线：无浅洞腿（只做急跌×时间）→ 任何带的增量意义都相对它
    print("\n【A0】不设浅洞腿基准（急跌 m_45≥0.40 × 时间 rem>180 全放行）")
    for name, s in (("全部", popd),
                    ("yes", popd[popd.side == "yes"]),
                    ("no", popd[popd.side == "no"])):
        n, wr, ev, p = summ(s, stake)
        print(f"  {name:<6} n={n:3d}  WR {wr*100:5.1f}%  EV {ev:+.3f}U/注  P&L {p:+7.1f}U"
              f"  (≈{p / ndays:+.1f}U/日)")

    # 【A】基线：引擎现行组合带（in-sample 定带, 09-03）
    print("\n【A】引擎现行组合带（dist_s yes(−0.6,0) / no(−1.0,0)，in-sample 定带）")
    cur = popd[((popd.side == "yes") & band_mask(popd, "dist_s", -0.6)) |
               ((popd.side == "no") & band_mask(popd, "dist_s", -1.0))]
    for name, s in (("全样本", cur), ("h1", cur[cur.h == "h1"]), ("h2", cur[cur.h == "h2"])):
        n, wr, ev, p = summ(s, stake)
        print(f"  {name:<6} n={n:3d}  WR {wr*100:5.1f}%  EV {ev:+.3f}U/注  P&L {p:+7.1f}U")

    # 【B】三条坐标的全样本 lo 扫描（每侧独立）
    print("\n【B】全样本 lo 网格扫描（P&L argmax; n≥20）——in-sample 参照, 非判据")
    for coord in COORDS:
        picks = side_split(popd, coord, los, stake)
        s = apply_bands(popd, coord, picks, stake)
        n, wr, ev, p = summ(s, stake)
        print(f"  {coord:<7} 带 yes({picks['yes']})/no({picks['no']})  "
              f"n={n:3d}  WR {wr*100:5.1f}%  EV {ev:+.3f}U/注  P&L {p:+7.1f}U")
        if args.detail:
            for side in ("yes", "no"):
                sub = popd[popd["side"] == side]
                rows = scan(sub, coord, los, stake,
                            restrict=(sub["m_45"] >= CRASH_MIN) & (sub["rem"] > REM_MIN))
                print(f"      {side} 曲线 (lo → n / EV / P&L):")
                line = "        "
                for lo, nn, ww, ee, pp in rows:
                    line += f"{lo:+.2f}:{nn}/{ee:+.2f}/{pp:+.0f}  "
                    if len(line) > 150:
                        print(line)
                        line = "        "
                if line.strip():
                    print(line)

    # 【C】h1 选阈 → h2 验证（及反向）—— 诚实的判据
    print("\n【C】h1 选阈 → h2 验证（反向同做）: 每条坐标的带在「没见过的」半样本上")
    print(f"  {'坐标':<8}{'选阈半样本':<12}{'带(yes/no)':<20}{'验证半样本':<12}"
          f"{'n':>4}{'WR':>8}{'EV':>10}{'P&L':>9}")
    oos = {}
    for coord in COORDS:
        for sel_h, ver_h in (("h1", "h2"), ("h2", "h1")):
            picks = side_split(popd, coord, los, stake, halves=sel_h)
            ver = apply_bands(popd, coord, picks, stake, extra=(popd["h"] == ver_h))
            n, wr, ev, p = summ(ver, stake)
            oos.setdefault(coord, []).append((ver_h, picks, n, wr, ev, p, ver))
            band_s = "{}/{}".format(picks["yes"], picks["no"])
            print(f"  {coord:<8}{sel_h + ' 选阈':<12}{band_s:<20}{ver_h:<12}"
                  f"{n:>4}{wr*100:>7.1f}%{ev:>+10.3f}{p:>+9.1f}")

    # 【D】配对 bootstrap: 同半样本上，各坐标 refit 带 vs (a) 基线固定带 (b) dist_s refit 带
    print("\n【D】按日配对 bootstrap（3000 次, a−b 的 95%CI）")
    print("     参照①= 引擎现行固定带（in-sample 定带）; 参照②= dist_s 同法 refit（同待遇对照）")
    base_by_h = {h: cur[cur.h == h] for h in ("h1", "h2")}
    ref_by_h = {h: {} for h in ("h1", "h2")}   # (ver_h, sel_h) → dist_s refit 臂
    for ver_h, picks, _n, _w, _e, _p, ver in oos["dist_s"]:
        sel_h = "h2" if ver_h == "h1" else "h1"
        ref_by_h[ver_h][sel_h] = ver
    for coord in COORDS:
        for ver_h, picks, n, wr, ev, p, ver in oos[coord]:
            a = {d: pl_of(g, stake).sum() for d, g in ver.groupby("date")}
            b = {d: pl_of(g, stake).sum() for d, g in base_by_h[ver_h].groupby("date")}
            d1, ci1 = paired_bootstrap(a, b)
            sel_h = "h2" if ver_h == "h1" else "h1"
            ref = ref_by_h[ver_h][sel_h]
            c = {d: pl_of(g, stake).sum() for d, g in ref.groupby("date")}
            d2, ci2 = paired_bootstrap(a, c)
            flag1 = "含 0" if ci1[0] <= 0 <= ci1[1] else "不含0"
            flag2 = "含 0" if ci2[0] <= 0 <= ci2[1] else "不含0"
            print(f"  {coord:<7} 验证 {ver_h}: vs基线 Δ{d1:+7.1f}U [{ci1[0]:+6.1f},{ci1[1]:+6.1f}] {flag1}"
                  f"   |  vs dist_s-refit Δ{d2:+7.1f}U [{ci2[0]:+6.1f},{ci2[1]:+6.1f}] {flag2}")

    # 【E】拆开 vs 相加：dist_s 单腿带 与 (dist_t 腿 ∧ basis 腿) 对比
    print("\n【E】拆腿对照: 现行 dist_s 单腿带 vs dist_t ∧ basis 双腿（各自在选阈半样本上定带）")
    for sel_h, ver_h in (("h1", "h2"), ("h2", "h1")):
        ps = side_split(popd, "dist_s", los, stake, halves=sel_h)
        pt = side_split(popd, "dist_t", los, stake, halves=sel_h)
        pb = side_split(popd, "basis", los, stake, halves=sel_h)
        one = apply_bands(popd, "dist_s", ps, stake, extra=(popd["h"] == ver_h))
        mt = apply_bands(popd, "dist_t", pt, stake)
        mb = apply_bands(popd, "basis", pb, stake)
        two = popd.loc[sorted(set(mt.index) & set(mb.index))]
        two = two[two["h"] == ver_h]
        n1, w1, e1, p1 = summ(one, stake)
        n2, w2, e2, p2 = summ(two, stake)
        print(f"  {sel_h} 选阈 → {ver_h}: dist_s 单腿 n={n1:3d} EV {e1:+.3f} P&L {p1:+7.1f}U  |  "
              f"dist_t∧basis 双腿 n={n2:3d} EV {e2:+.3f} P&L {p2:+7.1f}U")

    # 【F】纸面期样本外应用（回测期定的带 → 09-06 起纸面数据; 结算标签由 windows_* 推导）
    if args.paper_dir:
        paper_oos(args.paper_dir, stake)


#: 回测期（08-18~08-31）标定的带 → 纸面期前向应用（不再重标定）
FROZEN_RULES = (
    ("现行 dist_s 组合带", "dist_s", {"yes": -0.6, "no": -1.0}),
    ("dist_t refit（14天 argmax）", "dist_t", {"yes": -0.4, "no": -0.15}),
    ("basis refit（14天 argmax）", "basis", {"yes": -0.35, "no": -0.65}),
    ("不设浅洞腿（基准）", None, None),
)


def load_paper(paper_dir):
    """纸面 touches + windows_* → 每行补齐推导结算标签。

    结算线 = Chainlink TWAP-60，windows_* 已存 anchor/close → outcome 可本地推导
    （up = close > anchor）。用已结算的 ok 行核对：09-06 起一致率 99%+。
    09-03~09-05 无 windows_* 文件（σ 本地预热 09-06 才落盘）→ 该段不可推导。
    """
    import glob
    import pandas as pd
    win = {}
    for f in glob.glob(str(Path(paper_dir) / "windows_*.jsonl")):
        for l in open(f):
            w = json.loads(l)
            if w.get("amp") is not None:
                win[w["event_start"]] = (w["anchor"], w["close"])
    rows = []
    for f in sorted(glob.glob(str(Path(paper_dir) / "touches_*.jsonl"))):
        for l in open(f):
            r = json.loads(l)
            if r.get("event_type") != "touch":
                continue
            w = win.get(r["event_start"])
            if not w:
                continue
            # 0=Up 1=Down。平盘（close == anchor）归 **Up**（PM 口径 >= 算 UP，
            # 2026-09-17 用户指正）；此前是 `w[1] > w[0]` 且平盘整行丢弃——
            # 实测纸面 3400 个已完窗里平盘 0 个，故此改动对现有数据为零影响，
            # 仅为口径正确性（新数据出现平盘时不再静默丢行）。
            der = 0 if w[1] >= w[0] else 1
            r["settle_won"] = int((der == 0) if r["side"] == "yes" else (der == 1))
            # dist_s/dist_t 为 omitempty（缺失时不落键）→ 缺键按 NaN（不参与带判定）
            ds, dt = r.get("dist_s"), r.get("dist_t")
            r["dist_s"] = ds if ds else float("nan")
            r["dist_t"] = dt if dt else float("nan")
            r["basis"] = r["dist_s"] - r["dist_t"]
            r["m_45"] = r.get("m_45") or float("nan")
            rows.append(r)
    df = pd.DataFrame(rows)
    if len(df):
        df["pl"] = np.where(df["settle_won"] == 1, STAKE_REF / df["fill"] - STAKE_REF,
                            -STAKE_REF)
    return df


STAKE_REF = 2.0


def paper_oos(paper_dir, stake):
    global STAKE_REF
    STAKE_REF = stake
    df = load_paper(paper_dir)
    if not len(df):
        print("\n【F】纸面期: 无可推导标签的行（缺 windows_*）")
        return
    df["pl"] = np.where(df["settle_won"] == 1, stake / df["fill"] - stake, -stake)
    # 口径核对: 引擎已结算的 ok 行 vs 推导标签
    ok = df[(df["ok"] == True) & df["won"].notna()]  # noqa: E712
    agree = int((ok["settle_won"].astype(bool) == ok["won"]).sum())
    bad = ok[ok["settle_won"].astype(bool) != ok["won"]]
    bad_win = set(bad["event_start"])
    print(f"\n【F】纸面期样本外应用（带回测期标定, 不再重标定）")
    print(f"  标签推导核对: 引擎已结算 {len(ok)} 行, 本地推导一致 {agree}"
          f"（{agree/max(1,len(ok))*100:.1f}%）  区间 {df['date'].min()} ~ {df['date'].max()}")
    print(f"  ⚠️ 结算标签是本地推导（windows_* 的 anchor/close）不是官方结算：残差 ~0.8%，"
          f"且缺窗行全被排除 → 绝对水平不可信，只读规则间的相对排序。")
    print(f"  敏感性: 推导与官方不符的 {len(bad_win)} 个窗口整窗剔除后重算（标 *）")
    gated = df[df.get("gate_reason").notna()] if "gate_reason" in df else df.iloc[:0]
    if len(gated):
        df = df[df["gate_reason"].isna()]
        print(f"  已剔除 gate_reason 非空行 {len(gated)}（熔断/闸门; --include-gated 口径未开）")
    df["bad_win"] = df["event_start"].isin(bad_win)
    for lo_d, hi_d in [(None, None), ("2026-09-07", None)]:
        sub = df if lo_d is None else df[df["date"] >= lo_d]
        nd = sub["date"].nunique()
        print(f"\n  ── 区间 {sub['date'].min()} ~ {sub['date'].max()}（{nd} 天, "
              f"{len(sub)} 行有标签）")
        print(f"  {'规则':<24}{'n':>5}{'WR':>8}{'EV':>10}{'P&L':>10}{'U/日':>8}")
        for name, coord, bands in FROZEN_RULES:
            m = ((sub["m_45"] >= CRASH_MIN) & (sub["rem"] > REM_MIN)).to_numpy()
            if coord is not None:
                side_ok = np.zeros(len(sub), bool)
                for side, lo in bands.items():
                    side_ok |= ((sub["side"] == side).to_numpy()
                                & band_mask(sub, coord, lo).to_numpy())
                m = m & side_ok
            s = sub[m]
            if not len(s):
                print(f"  {name:<24}{0:>5}")
                continue
            ev = s["pl"].mean()
            clean = s[~s["bad_win"]]
            tail = (f"   *剔除坏窗 n={len(clean)} EV {clean['pl'].mean():+.3f} "
                    f"P&L {clean['pl'].sum():+.1f}U" if len(clean) else "")
            print(f"  {name:<24}{len(s):>5}{s.settle_won.mean()*100:>7.1f}%"
                  f"{ev:>+10.3f}{s.pl.sum():>+10.1f}{s.pl.sum()/nd:>+8.1f}{tail}")
            y = s[s["side"] == "yes"]
            nn = s[s["side"] == "no"]
            print(f"  {'':<24}   side: yes n={len(y):3d} EV {y['pl'].mean():+.3f} P&L "
                  f"{y.pl.sum():+7.1f}U  |  no n={len(nn):3d} EV {nn['pl'].mean():+.3f} "
                  f"P&L {nn.pl.sum():+7.1f}U" if len(y) and len(nn)
                  else f"  {'':<24}   side: yes {len(y)} / no {len(nn)}")
        print(f"  {'（引擎实际成交 ok 行）':<24}"
              f"{int(sub['ok'].sum()):>5}")


if __name__ == "__main__":
    main()
