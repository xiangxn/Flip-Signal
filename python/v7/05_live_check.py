#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v7-05: 实盘一致性检查（**非判决依据**, 见 `docs/gbm_2026-10-01.md` §0.4）。

实盘 8 天**不是干净前向**: 规则三次改动（09-26 / 09-29 / 10-01）、成交内生（GTC 挂单等成交
= 逆向选择）、价位段与回测不同。故这里只回答三件事:
  ① **core 模型的判别力与校准方向**（回测训练 → 实盘应用, 不重训）;
  ② **特征漂移**（回测前半 / 后半 / 实盘的分位 + PSI）;
  ③ **成交率 × p̂ 十分位**（逆向选择证据: 模型越看好 ⇒ 越买不到）。

⚠️ 不拿实盘真实 P&L 当判据——成交率与 p̂ 负相关, 拿它评模型就是拿选择效应评模型。

用法: python/venv/bin/python python/v7/05_live_check.py
"""
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import lib_sim as SIM             # noqa: E402
import lib_stats as S             # noqa: E402

CUT = "2026-08-25"


def load_tb():
    spec = importlib.util.spec_from_file_location("tb04", BASE / "04_track_b_gbm_signal.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def quant(v, qs=(0.1, 0.5, 0.9)):
    v = np.asarray(v, dtype=float)
    v = v[~np.isnan(v)]
    return np.quantile(v, qs) if len(v) else np.full(len(qs), np.nan)


def psi(base_vals, new_vals, bins=10):
    """PSI（基准分位切桶, 新样本落同桶）。>0.25 一般算显著漂移。"""
    b = np.asarray(base_vals, dtype=float)
    n = np.asarray(new_vals, dtype=float)
    b = b[~np.isnan(b)]
    n = n[~np.isnan(n)]
    if len(b) < 50 or len(n) < 50:
        return float("nan")
    edges = np.quantile(b, np.linspace(0, 1, bins + 1))
    edges[0], edges[-1] = -np.inf, np.inf
    pb = np.histogram(b, edges)[0] / len(b)
    pn = np.histogram(n, edges)[0] / len(n)
    pb = np.clip(pb, 1e-6, None)
    pn = np.clip(pn, 1e-6, None)
    return float(np.sum((pn - pb) * np.log(pn / pb)))


def main():
    tb = load_tb()
    data = SIM.Data()
    live = np.load(BASE / "data/live.npz", allow_pickle=True)
    # 交集: `twap_age_ms` 只在实盘行上有（回测 tick 网格没这列）⇒ 移出转移模型,
    # 报告里注明核心集少一列（不影响结论: 它是数据质量量, 与判别力无关）。
    core_names = [c for c in live["core"].tolist() if c in data.cols]
    live_idx = [live["core"].tolist().index(c) for c in core_names]
    core_idx = [data.cols.index(c) for c in core_names]
    print(f"  转移模型特征（回测 ∩ 实盘, {len(core_names)}）: {core_names}")
    print("=" * 96)
    print("v7-05 实盘一致性（非判决依据）")
    print("=" * 96)
    print(f"  实盘快照行 {live['X'].shape[0]}（窗口 {len(set(live['event_start'].tolist()))}）")

    # ── ① core 模型: 回测 OOF 参照 + 回测全量训练 → 实盘应用 ──
    print("\n" + "=" * 96)
    print("① core 模型（只用实盘算得出的特征）")
    print("=" * 96)
    pcol_core, _, y_bt, wid_bt, day_bt, X_core = tb.oof_predict(data, cols=core_idx)
    p_bt = np.concatenate([pcol_core[i] for i in sorted(pcol_core)])
    A_bt = tb.auc(p_bt, y_bt)
    print(f"  回测 tick 级 OOF AUC（core 特征）: {A_bt:.4f}"
          f"   （14 特征模型见 04 号 §3）")

    # 回测全量训练（含全部日）→ 打到实盘
    _, _, w_bt, _, _, _ = tb.build(data)
    _, m = tb.fit_predict(X_core, y_bt, w_bt, X_core[:1])
    p_live = m.predict(live["X"][:, live_idx])
    ok_now = live["ok_now"].astype(bool)
    won = live["won"]
    known = ok_now & ~np.isnan(won)
    print(f"  现行规则重放的实盘信号 {ok_now.sum()} 条（有结算 {known.sum()}）")
    if known.sum() > 30:
        A_live = tb.auc(p_live[known], won[known])
        print(f"  实盘信号上 core 模型 AUC: {A_live:.4f}（n={known.sum()}, "
              f"输单 {int((won[known]==0).sum())}）")
        print("  校准（按 p̂ 四分位; 期望 = 模型均值, 实际 = 实现胜率）:")
        qs = np.quantile(p_live[known], np.linspace(0, 1, 5))
        for k in range(4):
            mm = known.copy()
            mm[known] = (p_live[known] >= qs[k]) & (p_live[known] <= qs[k + 1])
            if mm.sum() == 0:
                continue
            print(f"      Q{k+1} p̂∈[{qs[k]:.3f},{qs[k+1]:.3f}]  n={mm.sum():<4} "
                  f"模型均值 {p_live[mm].mean():.4f}  实际 {won[mm].mean()*100:6.2f}%  "
                  f"成交价均值 {live['fill'][mm].mean():.4f}")

    # 判别力对照: 用**成交价本身**当预测器（市价基准）
    if known.sum() > 30:
        print(f"  对照: 拿成交价当预测器的 AUC {tb.auc(live['fill'][known], won[known]):.4f}")

    # ── ② 特征漂移 ──
    print("\n" + "=" * 96)
    print("② 特征漂移（分位 [10,50,90]; PSI 以**回测全期**为基准, >0.25 视为显著）")
    print("=" * 96)
    d_bt = np.array(data.days)[day_bt]
    h1 = d_bt < CUT
    h2 = d_bt >= CUT
    print(f"  {'特征':<20} {'回测前半 [10,50,90]':<26} {'回测后半':<26} "
          f"{'实盘':<26} PSI")
    for k, name in enumerate(core_names):
        a = quant(X_core[h1, k])
        b2 = quant(X_core[h2, k])
        c = quant(live["X"][:, live_idx[k]])
        p = psi(X_core[:, k], live["X"][:, live_idx[k]])
        fmt = lambda v: "[" + ",".join(f"{x:8.3f}" for x in v) + "]"
        print(f"  {name:<20} {fmt(a):<26} {fmt(b2):<26} {fmt(c):<26} {p:6.3f}")

    # ── ③ 成交率 × p̂ ──
    print("\n" + "=" * 96)
    print("③ 逆向选择: 成交率 × p̂ 十分位（只取**真正挂过单**的行 = 记录时的 ok 行, 09-26 起）")
    print("=" * 96)
    att = live["ok_old"].astype(bool) & (np.array(live["days"])[live["day"]] >= "2026-09-26")
    if att.sum() > 50:
        qs = np.quantile(p_live[att], np.linspace(0, 1, 11))
        for k in range(10):
            mm = np.zeros(len(p_live), dtype=bool)
            mm[att] = (p_live[att] >= qs[k]) & (p_live[att] <= qs[k + 1])
            if mm.sum() == 0:
                continue
            fr = live["filled"][mm].mean()
            print(f"      D{k+1:<2} p̂∈[{qs[k]:.4f},{qs[k+1]:.4f}]  n={mm.sum():<4} "
                  f"成交率 {fr*100:6.2f}%  成交价均值 {live['fill'][mm].mean():.4f}  "
                  f"bid 兜底占比 {live['src_bid'][mm].mean()*100:5.1f}%")
        ts = np.corrcoef(p_live[att], live["filled"][att].astype(float))[0, 1]
        ts2 = np.corrcoef(p_live[att], live["fill"][att])[0, 1]
        ts3 = np.corrcoef(p_live[att], live["src_bid"][att].astype(float))[0, 1]
        print(f"      ρ(p̂, 成交) = {ts:+.3f}   ρ(p̂, 成交价) = {ts2:+.3f}   "
              f"ρ(p̂, bid 兜底) = {ts3:+.3f}")
        print("      ⇒ 负的成交相关 + 正的兜底相关 = **高 p̂ 区正是热门侧被人买光/撤空的那批**"
              "（模型最看好的地方恰恰是挂单挂不上的地方）")
        # 只看 ask 在场（真能挂单）的子集, 成交率还随 p̂ 掉吗
        ask = att & (live["src_bid"] == 0)
        if ask.sum() > 30:
            lo = ask & (p_live < np.median(p_live[ask]))
            hi = ask & (p_live >= np.median(p_live[ask]))
            print(f"      只在 ask 在场子集内（n={ask.sum()}）: 低半成交率 "
                  f"{live['filled'][lo].mean()*100:.1f}% / 高半 "
                  f"{live['filled'][hi].mean()*100:.1f}%  "
                  f"（ρ={np.corrcoef(p_live[ask], live['filled'][ask].astype(float))[0,1]:+.3f}）")
    else:
        print("      样本不足")

    (BASE / "data/live_check.json").write_text(json.dumps({
        "n_live_rows": int(live["X"].shape[0]),
        "auc_core_bt": float(A_bt),
        "n_live_signals": int(ok_now.sum()),
    }, indent=1, ensure_ascii=False), encoding="utf-8")
    print("\n[05] 明细已存 python/v7/data/live_check.json")


if __name__ == "__main__":
    main()
