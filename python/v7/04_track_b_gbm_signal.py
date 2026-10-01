#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v7-04 Track B｜GBM 直接下单：tick 级模型自己决定何时入场（丢掉规则链）。

预注册判据见 `docs/gbm_2026-10-01.md` §0.3:
  **主规则**: 逐秒扫描 `rem ≤ 150`, 首个 `p̂ − fill > δ`（δ=0）的 tick 下单, 整窗一单,
  只买该 tick 的热门侧。
  ① 模拟 P&L 的日级配对 Δ（vs 基线 +70.314372U）95% **下界 > 0** 且净 Δ ≥ +10U/14 天@2U;
  ② 入场笔数 ≥ 基线的 **50%**; ③ 窗口级 OOF AUC 日级置换 **p < 0.05**;
  ④ 分半同向 + δ 扫描的搜索校正置换 **p < 0.05**。
  对照三件: ① 现策略; ② **无模型退化版**（同一模拟器、触发 = 固定价格门槛）;
  ③ **规则链 + 模型过滤**（模型只在链的候选 tick 上否决, 不过则按 walk_low 语义继续）。

训练纪律: 标签 = 官方 outcome; 按日 GroupKFold 出 OOF; 每 tick 权重 = 1/窗内 tick 数
（有效样本 = 窗数, 否则 54 万个 tick 会假装成 54 万个独立样本）。

用法: python/venv/bin/python python/v7/04_track_b_gbm_signal.py [nperm=200]
"""
import json
import sys
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import lib_sim as SIM             # noqa: E402
import lib_stats as S             # noqa: E402
import lib_universe as U          # noqa: E402

CUT = "2026-08-25"
DELTA_MAIN = 0.0
DELTA_GRID = (0.0, 0.01, 0.02, 0.03, 0.05, 0.08)
P_GRID = (0.80, 0.85, 0.88, 0.90, 0.92, 0.94, 0.96)      # 退化对照的价格门槛

try:
    import lightgbm as lgb
except ImportError:                                            # pragma: no cover
    print("需要 lightgbm: python/venv/bin/pip install lightgbm")
    sys.exit(1)


# ── 数据装配 ──────────────────────────────────────────────────────────────

def build(data):
    """tick 级设计矩阵: X（14 特征）+ 标签 + 权重 + 窗号 + 日号。"""
    X, y, wgt, wid, day, pos = [], [], [], [], [], []
    for i in range(len(data)):
        w = data.window(i)
        A, Xw = w["A"], w["X"]
        n = len(A)
        if n == 0 or w["outcome"] is None:
            continue
        sgn = A[:, 0 * 0 + 3]                       # C_SGN
        yes = sgn > 0
        won = np.where(yes, w["outcome"] == 0, w["outcome"] == 1).astype(np.int8)
        X.append(Xw)
        y.append(won)
        wgt.append(np.full(n, 1.0 / n))
        wid.append(np.full(n, i, dtype=np.int32))
        day.append(np.full(n, w["day"], dtype=np.int32))
        pos.append(np.arange(n, dtype=np.int32))
    return (np.vstack(X).astype(np.float32),
            np.concatenate(y).astype(np.int8),
            np.concatenate(wgt).astype(np.float64),
            np.concatenate(wid), np.concatenate(day), np.concatenate(pos))


def fit_predict(Xtr, ytr, wtr, Xte, seed=42, n_estimators=400, params=None):
    """单个折的模型（默认参数 = 预注册那套; 敏感性检查会换 params）。"""
    import lightgbm as lgb
    p = dict(objective="binary", learning_rate=0.05, num_leaves=8,
             min_data_in_leaf=500, feature_fraction=0.9, bagging_fraction=0.9,
             bagging_freq=1, lambda_l2=1.0, verbose=-1, seed=seed, num_threads=4)
    p.update(params or {})
    ds = lgb.Dataset(Xtr, label=ytr, weight=wtr)
    m = lgb.train(p, ds, num_boost_round=n_estimators)
    return m.predict(Xte), m


def oof_predict(data, seed=42, n_folds=5, n_estimators=400, params=None, cols=None):
    """按日 GroupKFold 出全网格 OOF p̂（返回: 每窗的 p̂ 数组 dict + 特征重要度）。

    `cols` = 列索引子集（05 号用它做 core-8 模型的参照; 行序与标签不受影响）。
    """
    X, y, wgt, wid, day, pos = build(data)
    if cols is not None:
        X = X[:, cols]
    days = np.unique(day)
    fold_of = {d: k % n_folds for k, d in enumerate(days)}
    p_hat = np.full(len(y), np.nan)
    imp = None
    models = []
    for kf in range(n_folds):
        te = np.isin(day, [d for d in days if fold_of[d] == kf])
        tr = ~te
        p_te, mdl = fit_predict(X[tr], y[tr], wgt[tr], X[te],
                                seed=seed + kf, n_estimators=n_estimators, params=params)
        p_hat[te] = p_te
        models.append(mdl)
        imp = mdl.feature_importance("gain") if imp is None else imp + mdl.feature_importance("gain")
    pcol = {}
    for i in range(len(data)):
        sl = data.slice(i)
        if sl.stop > sl.start:
            pcol[i] = p_hat[sl]
    return pcol, imp / max(1, len(models)), y, wid, day, X


# ── 决策与模拟 ────────────────────────────────────────────────────────────

def stage_marks(A):
    """逐 tick 的段标签（与链的切段同口径）: 首个 rem≤60 的 tick = t60, 其后 = listen。"""
    rem = A[:, 2]
    out = np.array(["t150"] * len(A), dtype=object)
    first60 = np.argmax(rem <= 60) if (rem <= 60).any() else None
    if first60 is not None:
        out[first60] = "t60"
        out[first60 + 1:] = "listen"
    return out


def decide_model(pcol, delta):
    """首个 `p̂ − fill > δ` 的 tick 入场（整窗一单）。"""
    def decide(w):
        i = w["i"]
        p = pcol.get(i)
        if p is None:
            return None
        A = w["A"]
        st = stage_marks(A)
        for j in range(len(A)):
            if p[j] - A[j, 4] > delta:
                return _row(w, A[j], int(j), st[j], float(p[j]))
        return None
    return decide


def decide_price(pcol_unused, thr):
    """退化对照: 首个 `fill > 门槛` 的 tick 入场（完全没有模型）。"""
    def decide(w):
        A = w["A"]
        st = stage_marks(A)
        for j in range(len(A)):
            if A[j, 4] > thr:
                return _row(w, A[j], int(j), st[j], np.nan)
        return None
    return decide


def _row(w, t, pos, stage, p):
    sgn = t[3]
    return {"date": w["date"], "day": w["day"], "event_start": w["start_time"],
            "condition_id": w["condition_id"], "stage": stage, "rem": float(t[2]),
            "side": ("yes" if sgn > 0 else "no"), "fill": float(t[4]),
            "gi": int(t[0]), "sd": w["sd"], "dev": float(t[7]),
            "walk": (None if t[6] != t[6] else float(t[8])), "pos": pos, "p_hat": p}


def sim(data, decide):
    rows = []
    for i in range(len(data)):
        w = data.window(i)
        if w["outcome"] is None:
            continue
        r = decide(w)
        if r is None:
            continue
        won = 1 if ((w["outcome"] == 0) if r["side"] == "yes" else (w["outcome"] == 1)) else 0
        r["won"] = won
        r["win_i"] = i
        r["outcome"] = w["outcome"]
        r["pnl"] = (U.STAKE / r["fill"] - U.STAKE) if won else -U.STAKE
        rows.append(r)
    return rows


def auc(scores, labels, w=None):
    """ROC-AUC（tie 取平均秩）。"""
    s = np.asarray(scores, dtype=np.float64)
    lab = np.asarray(labels, dtype=np.float64)
    ok = ~np.isnan(s)
    s, lab = s[ok], lab[ok]
    w = None if w is None else np.asarray(w, dtype=np.float64)[ok]
    if len(s) == 0 or lab.min() == lab.max():
        return float("nan")
    order = np.argsort(s, kind="mergesort")
    ranks = np.empty(len(s))
    ranks[order] = np.arange(1, len(s) + 1)
    # 并列取平均秩
    s_sorted = s[order]
    i = 0
    while i < len(s_sorted):
        j = i
        while j + 1 < len(s_sorted) and s_sorted[j + 1] == s_sorted[i]:
            j += 1
        if j > i:
            ranks[order[i:j + 1]] = (i + 1 + j + 1) / 2.0
        i = j + 1
    if w is None:
        n1, n0 = lab.sum(), (1 - lab).sum()
        return (ranks[lab == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0) if n1 and n0 else float("nan")
    return float("nan")


def brier(p, y, w):
    m = ~np.isnan(p)
    return float(np.average((p[m] - y[m]) ** 2, weights=w[m]))


def logloss(p, y, w):
    m = ~np.isnan(p)
    eps = 1e-9
    pp = np.clip(p[m], eps, 1 - eps)
    return float(-np.average(y[m] * np.log(pp) + (1 - y[m]) * np.log(1 - pp), weights=w[m]))


def summarize(rows):
    return SIM.summarize(rows)


# ── 主流程 ───────────────────────────────────────────────────────────────

def main():
    nperm = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    data = SIM.Data()
    base_rows = SIM.simulate(data, SIM.chain_decide(U.Rules()))
    base_day = S.day_pnl(base_rows)
    b = summarize(base_rows)
    print("=" * 96)
    print("v7-04 Track B｜GBM 直接下单（tick 级模型 + 首个 p̂−fill>δ 入场）")
    print("=" * 96)
    print(f"  基线（现规则链）: n={b['n']} WR {b['wr']:.4f}% P&L {b['pnl']:+.4f}U  "
          f"§0 {'✅' if abs(b['pnl']-U.PIN['pnl'])<1e-6 and b['n']==U.PIN['signals'] else '❌'}")

    # ── §1 训练 + OOF ──
    print("\n" + "=" * 96)
    print("§1 tick 级模型（按日 GroupKFold 5 折 OOF; 权重 = 1/窗内 tick 数）")
    print("=" * 96)
    pcol, imp, y, wid, day, X = oof_predict(data)
    cols = list(data.cols)
    print("  特征重要度（gain, 归一化）:")
    for k in np.argsort(-imp)[:14]:
        print(f"       {cols[k]:<22} {imp[k]/imp.sum()*100:6.2f}%")
    fill = X[:, cols.index("fill")].astype(np.float64)
    # 市场价（fill）当基准预测器
    wgt = np.concatenate([np.full(len(data.window(i)["A"]), 1.0 / len(data.window(i)["A"]))
                          for i in range(len(data)) if len(data.window(i)["A"])])
    print(f"\n  全网格 n={len(y)}: 基础胜率 {np.average(y, weights=wgt)*100:.2f}%")
    print(f"     模型  Brier {brier(np.concatenate([pcol[i] for i in sorted(pcol)]), y, wgt):.6f}"
          f"  logloss {logloss(np.concatenate([pcol[i] for i in sorted(pcol)]), y, wgt):.6f}")
    print(f"     市价  Brier {brier(fill, y, wgt):.6f}  logloss {logloss(fill, y, wgt):.6f}"
          f"   （市价 = 热门侧有效价, 直接当 P(赢) 的零模型）")

    # ── §2 主规则（δ=0） ──
    print("\n" + "=" * 96)
    print(f"§2 主规则: 首个 p̂ − fill > {DELTA_MAIN} 的 tick 入场（OOF p̂）")
    print("=" * 96)
    rows = sim(data, decide_model(pcol, DELTA_MAIN))
    s = summarize(rows)
    arm_day = S.day_pnl(rows)
    dlt, lo, hi = S.boot_delta(base_day, arm_day)
    print(f"  n={s['n']} WR {s['wr']:.4f}% P&L {s['pnl']:+.4f}U  "
          f"Δ {dlt:+.2f}U 95%[{lo:+.2f},{hi:+.2f}]  （基线 n={b['n']} / {b['pnl']:+.2f}U）")
    h1 = [r for r in rows if r["date"] < CUT]
    h2 = [r for r in rows if r["date"] >= CUT]
    d1 = sum(r["pnl"] for r in h1) - sum(base_day.get(d, 0) for d in set(r["date"] for r in h1))
    d2 = sum(r["pnl"] for r in h2) - sum(base_day.get(d, 0) for d in set(r["date"] for r in h2))
    print(f"  分半 Δ: h1 {d1:+.2f}U / h2 {d2:+.2f}U")

    # 入场时点/价格分布 vs 基线
    def dist(rs, key):
        v = np.array([r[key] for r in rs if r[key] is not None], dtype=float)
        return (np.percentile(v, [10, 50, 90]) if len(v) else [np.nan] * 3)

    print(f"  入场 rem 分位[10,50,90]: 模型 {np.round(dist(rows,'rem'),1)}  "
          f"基线 {np.round(dist(base_rows,'rem'),1)}")
    print(f"  入场 fill 分位[10,50,90]: 模型 {np.round(dist(rows,'fill'),3)}  "
          f"基线 {np.round(dist(base_rows,'fill'),3)}")
    st_cnt = {}
    for r in rows:
        st_cnt[r["stage"]] = st_cnt.get(r["stage"], 0) + 1
    print(f"  段分布: {st_cnt}（模型自己走到的段; 基线 = 958/741/375）")
    gap = np.mean([r["p_hat"] - r["fill"] for r in rows])
    real_wr = sum(r["won"] for r in rows) / len(rows)
    print(f"  ⚠️ 校准缺口: 成交时平均 (p̂ − fill) = {gap:+.4f}, 而**实现**胜率 − 平均成交价 = "
          f"{real_wr - np.mean([r['fill'] for r in rows]):+.4f} ⇒ "
          f"{'模型系统性高估' if gap > 0 > real_wr - np.mean([r['fill'] for r in rows]) else '方向一致'}")

    # ── §2b 敏感性: 换模型容量（防「只是这一套超参的伪影」） ──
    print("\n  §2b 主规则对模型容量的敏感性（同一纪律, 只换 LightGBM 容量）:")
    for nm, pp in (("小 (8 叶/500)", None),
                   ("中 (31 叶/100)", {"num_leaves": 31, "min_data_in_leaf": 100}),
                   ("大 (127 叶/20)", {"num_leaves": 127, "min_data_in_leaf": 20})):
        pc2 = oof_predict(data, params=pp)[0]
        rs2 = sim(data, decide_model(pc2, DELTA_MAIN))
        s2 = summarize(rs2)
        g2 = np.mean([r["p_hat"] - r["fill"] for r in rs2])
        print(f"      {nm:<16} n={s2['n']:<5} WR {s2['wr']:6.2f}%  P&L {s2['pnl']:+7.2f}U"
              f"  平均(p̂−fill) {g2:+.4f}")

    # ── §3 判别力（判据 ③） ──
    print("\n" + "=" * 96)
    print("§3 判别力: tick 级 AUC + 窗口级（决策 tick）AUC, 均做**按日整块**标签置换")
    print("=" * 96)
    p_all = np.concatenate([pcol[i] for i in sorted(pcol)])
    A_tick = auc(p_all, y)
    A_tick_mkt = auc(fill, y)                 # 市价（fill）自己的判别力 = 模型的参照杆
    # 窗口级: 每窗取它的决策 tick（主规则选中的那个）
    win_list = sorted({r["win_i"] for r in rows})
    ws_p = np.array([{r["win_i"]: r for r in rows}[i]["p_hat"] for i in win_list])
    ws_y = np.array([{r["win_i"]: r for r in rows}[i]["won"] for i in win_list], dtype=float)
    A_win = auc(ws_p, ws_y)
    # 日级置换: 打乱日的 outcome → 重算标签
    out_by_day, win_by_day = {}, {}
    for i in range(len(data)):
        w = data.win[i]
        if w["outcome"] is None:
            continue
        out_by_day.setdefault(w["date"], []).append(w["outcome"])
        win_by_day.setdefault(w["date"], []).append(i)
    days_have = list(out_by_day)
    import random
    rng = random.Random(S.SEED)
    null_tick, null_win = [], []
    win_rows_by_i = {r["win_i"]: r for r in rows}
    for _ in range(nperm):
        src = {d: rng.choice(days_have) for d in days_have}
        wout = {}
        for d in days_have:
            outs = out_by_day[src[d]]
            for k, wi in enumerate(win_by_day[d]):
                wout[wi] = outs[k % len(outs)]
        # tick 标签重建
        yy = np.full(len(y), -1, dtype=np.int8)
        off = 0
        for i in range(len(data)):
            w = data.win[i]
            sl = data.slice(i)
            n = sl.stop - sl.start
            if n == 0 or w["outcome"] is None:
                continue
            A = data.window(i)["A"]
            sgn = A[:, 3]
            yy[sl] = np.where(sgn > 0, wout[i] == 0, wout[i] == 1).astype(np.int8)
        m = yy >= 0
        null_tick.append(auc(p_all[m], yy[m]))
        wy = np.array([1 if ((wout[i] == 0) if win_rows_by_i[i]["side"] == "yes"
                             else (wout[i] == 1)) else 0 for i in win_list], dtype=float)
        null_win.append(auc(ws_p, wy))
    p_tick = S.perm_p(A_tick, [v for v in null_tick if v == v])
    p_win = S.perm_p(A_win, [v for v in null_win if v == v])
    print(f"  tick 级 AUC 模型 {A_tick:.4f} / **市价 {A_tick_mkt:.4f}**  "
          f"置换零分布中位 {np.nanmedian(null_tick):.4f} ⇒ p={p_tick:.4f}")
    print(f"  窗口级 AUC {A_win:.4f}（n={len(ws_p)} 个决策 tick）"
          f"  置换零分布中位 {np.nanmedian(null_win):.4f} ⇒ p={p_win:.4f}")

    # ── §4 对照 ②: 无模型退化版 ──
    print("\n" + "=" * 96)
    print("§4 对照② 无模型退化版: 触发 = 固定价格门槛 `fill > x`（同一模拟器, 全样本扫描）")
    print("=" * 96)
    price_tab = {}
    for thr in P_GRID:
        rs = sim(data, decide_price(None, thr))
        sm = summarize(rs)
        dd, l2, h2b = S.boot_delta(base_day, S.day_pnl(rs))
        price_tab[thr] = sm
        print(f"    fill > {thr:.2f}   n={sm['n']:<5} WR {sm['wr']:6.2f}%  P&L {sm['pnl']:+7.2f}U"
              f"  Δ {sm['pnl']-b['pnl']:+7.2f}U  95%[{l2:+7.2f},{h2b:+7.2f}]")

    # ── §5 对照 ③: 规则链 + 模型过滤 ──
    print("\n" + "=" * 96)
    print("§5 对照③ 规则链 + 模型过滤（只在链的候选 tick 上否决; 不过 ⇒ 同 walk_low 语义继续走链）")
    print("=" * 96)
    filt_tab = {}
    for df in (0.0, 0.01, 0.02, 0.03):
        def make_extra(df_):
            def extra(r, base):
                i = base["i"]
                p = pcol.get(i)
                if p is None:
                    return True
                pos = base["gi2pos"].get(int(r["gi"]))
                if pos is None or np.isnan(p[pos]):
                    return True
                return p[pos] - r["fill"] > df_
            return extra
        rs = SIM.simulate(data, SIM.chain_decide(U.Rules(extra_ok=make_extra(df))))
        sm = summarize(rs)
        dd, l2, h2b = S.boot_delta(base_day, S.day_pnl(rs))
        filt_tab[df] = (sm, dd, l2, h2b, rs)
        print(f"    p̂ − fill > {df:.2f}   n={sm['n']:<5} WR {sm['wr']:6.2f}%  P&L {sm['pnl']:+7.2f}U"
              f"  Δ {dd:+7.2f}U  95%[{l2:+7.2f},{h2b:+7.2f}]")

    # ── §6 δ 扫描 + 搜索校正置换（折内选择纪律） ──
    print("\n" + "=" * 96)
    print("§6 δ 扫描（描述性）+ 折内选择 OOF + 搜索校正置换")
    print("=" * 96)
    grid_rows = {}
    for d_ in DELTA_GRID:
        rs = sim(data, decide_model(pcol, d_))
        sm = summarize(rs)
        grid_rows[d_] = rs
        print(f"    δ={d_:.2f}   n={sm['n']:<5} WR {sm['wr']:6.2f}%  P&L {sm['pnl']:+7.2f}U"
              f"  Δ {sm['pnl']-b['pnl']:+7.2f}U")

    days = sorted(set(w["date"] for w in data.win))
    fold = {d: k % 5 for k, d in enumerate(days)}
    day_of_v = {d_: S.day_pnl(rs) for d_, rs in grid_rows.items()}
    win_of_v = {d_: {r["win_i"]: r for r in rs} for d_, rs in grid_rows.items()}

    def oof_pick(fold_of_day):
        oof = []
        best = {}
        for kf in range(5):
            tr = [d for d in days if fold_of_day[d] != kf]
            te = set(d for d in days if fold_of_day[d] == kf)
            best_kf = max(DELTA_GRID, key=lambda v: (sum(day_of_v[v].get(d, 0.0) for d in tr), -v))
            best[kf] = best_kf
            for wi, r in win_of_v[best_kf].items():
                if data.win[wi]["date"] in te:
                    oof.append(r)
        return oof, best

    oof, best = oof_pick(fold)
    sm = summarize(oof)
    dlt, lo, hi = S.boot_delta(base_day, S.day_pnl(oof))
    h1o = sum(r["pnl"] for r in oof if r["date"] < CUT)
    h2o = sum(r["pnl"] for r in oof if r["date"] >= CUT)
    d1o = h1o - sum(base_day.get(d, 0) for d in set(r["date"] for r in oof if r["date"] < CUT))
    d2o = h2o - sum(base_day.get(d, 0) for d in set(r["date"] for r in oof if r["date"] >= CUT))
    print(f"  折内最优 δ: " + ", ".join(f"折{k}→{v}" for k, v in sorted(best.items())))
    print(f"  OOF: n={sm['n']} WR {sm['wr']:.2f}% P&L {sm['pnl']:+.2f}U Δ={dlt:+.2f}U "
          f"95%[{lo:+.2f},{hi:+.2f}]  分半 Δ {d1o:+.2f}/{d2o:+.2f}")

    def score_rows(rows_, wout, ref_up):
        s = 0.0
        for r in rows_:
            p_up = ref_up[data.win[r["win_i"]]["date"]]
            yes = (r["side"] == "yes")
            wref = p_up if yes else (1.0 - p_up)
            o = wout[r["win_i"]]
            won = (o == 0) if yes else (o == 1)
            s += (2.0 * wref / r["fill"]) if won else (-2.0 * (1.0 - wref) / r["fill"])
        return (s / len(rows_)) if rows_ else 0.0

    def money_rows(rows_, wout):
        s = 0.0
        for r in rows_:
            won = 1 if ((wout[r["win_i"]] == 0) if r["side"] == "yes"
                        else (wout[r["win_i"]] == 1)) else 0
            s += ((U.STAKE / r["fill"] - U.STAKE) if won else -U.STAKE)
        return (s / len(rows_)) if rows_ else 0.0

    real_out = {i: w["outcome"] for i, w in enumerate(data.win) if w["outcome"] is not None}
    real_up = {d: sum(1 for wi in win_by_day[d] if real_out[wi] == 0) / len(win_by_day[d])
               for d in win_by_day}
    obs_sc = score_rows(oof, real_out, real_up) - score_rows(base_rows, real_out, real_up)
    obs_mn = money_rows(oof, real_out) - money_rows(base_rows, real_out)
    null_sc, null_mn = [], []
    for _ in range(nperm):
        src = {d: rng.choice(days_have) for d in days_have}
        wout, ref_up = {}, {}
        for d in days_have:
            outs = out_by_day[src[d]]
            for k, wi in enumerate(win_by_day[d]):
                wout[wi] = outs[k % len(outs)]
            ref_up[d] = sum(1 for o in outs if o == 0) / len(outs)
        dpv = {}
        for v, rs in grid_rows.items():
            dd = {}
            for r in rs:
                won = 1 if ((wout[r["win_i"]] == 0) if r["side"] == "yes"
                            else (wout[r["win_i"]] == 1)) else 0
                dd[r["date"]] = dd.get(r["date"], 0.0) + \
                    ((U.STAKE / r["fill"] - U.STAKE) if won else -U.STAKE)
            dpv[v] = dd
        sel = []
        for kf in range(5):
            tr = [d for d in days if fold[d] != kf]
            te = set(d for d in days if fold[d] == kf)
            best_kf = max(DELTA_GRID, key=lambda v: (sum(dpv[v].get(d, 0.0) for d in tr), -v))
            for wi, r in win_of_v[best_kf].items():
                if data.win[wi]["date"] in te:
                    sel.append(r)
        null_sc.append(score_rows(sel, wout, ref_up) - score_rows(base_rows, wout, ref_up))
        null_mn.append(money_rows(sel, wout) - money_rows(base_rows, wout))
    p_sc = S.perm_p(obs_sc, null_sc)
    p_mn = S.perm_p(obs_mn, null_mn)
    p_search = max(p_sc, p_mn)
    print(f"  置换统计量（折外 OOF 臂 − 基线）: 每笔得分 {obs_sc:+.4f} vs p95 "
          f"{np.percentile(null_sc,95):+.4f} ⇒ p={p_sc:.4f}; 每笔 P&L {obs_mn:+.4f}U vs p95 "
          f"{np.percentile(null_mn,95):+.4f}U ⇒ p={p_mn:.4f}  ⇒ 取保守 p={p_search:.4f}")

    # ── §7 诊断 ──
    print("\n" + "=" * 96)
    print("§7 诊断: (p̂ − fill) 分桶 EV / fill 分桶 / 校准 / 模型隐含 walk 阈值")
    print("=" * 96)
    print("  (a) 全网格 (p̂ − fill) 十分位 × **实现校准缺口**（每行 = 一个 tick; 缺 >0 ⇒ 真有钱）:")
    rows_all = []
    for i in range(len(data)):
        w = data.window(i)
        if w["outcome"] is None or i not in pcol:
            continue
        A, p = w["A"], pcol[i]
        yes = A[:, 3] > 0
        won = np.where(yes, w["outcome"] == 0, w["outcome"] == 1).astype(float)
        for j in range(len(A)):
            rows_all.append((p[j] - A[j, 4], A[j, 4], p[j], won[j]))
    arr = np.array(rows_all)
    qs = np.quantile(arr[:, 0], np.linspace(0, 1, 11))
    print("      桶        p̂−fill 区间         n      平均 fill  实现胜率   缺口(胜率−fill)")
    for k in range(10):
        m = (arr[:, 0] >= qs[k]) & (arr[:, 0] <= qs[k + 1])
        gap_k = arr[m, 3].mean() - arr[m, 1].mean()
        print(f"      D{k+1:<2} [{qs[k]:+.3f},{qs[k+1]:+.3f}]  {m.sum():>7}   "
              f"{arr[m,1].mean():.4f}   {arr[m,3].mean()*100:6.2f}%   {gap_k:+.4f}")
    print("  (b) 模型入场价分桶 EV（模型实际下的单）:")
    fills = np.array([r["fill"] for r in rows])
    for lo_, hi_ in ((0.8, 0.9), (0.9, 0.95), (0.95, 0.99), (0.99, 1.01)):
        m = (fills >= lo_) & (fills < hi_)
        if m.sum() == 0:
            continue
        sub = [r for r, mm in zip(rows, m) if mm]
        print(f"      fill ∈ [{lo_},{hi_})  n={m.sum():<5} WR "
              f"{sum(r['won'] for r in sub)/len(sub)*100:6.2f}%  "
              f"P&L {sum(r['pnl'] for r in sub):+7.2f}U")
    print("  (c) 校准（tick 级, 权重 1/窗内 tick 数; 模型 vs 市价）:")
    for lo_, hi_ in ((0.0, 0.8), (0.8, 0.9), (0.9, 0.95), (0.95, 0.99), (0.99, 1.01)):
        m = (fill >= lo_) & (fill < hi_)
        if m.sum() == 0:
            continue
        print(f"      市价 ∈ [{lo_},{hi_})  n={m.sum():<7} 实际胜率 {y[m].mean()*100:6.2f}%  "
              f"市价均值 {fill[m].mean():.3f}  模型均值 {np.nanmean(p_all[m]):.3f}")
    # (d) 隐含 walk 阈值 vs σ（t150 判定 tick = 每窗 rem≤150 的首个 tick）
    print("  (d) 模型隐含的 walk 阈值: t150 判定 tick 上按 walk/σ 分桶看 (p̂ − fill) 在哪过 0")
    t150 = []
    for i in range(len(data)):
        w = data.window(i)
        if w["outcome"] is None or i not in pcol or len(w["A"]) == 0:
            continue
        A, p = w["A"], pcol[i]
        if A[0, 2] > 60:                                    # 该窗的 t150 判定 tick
            A0 = A[0]
            wr = float(p[0] - A0[4])
            if A0[8] == A0[8]:                              # walk 有值
                t150.append((w["sd"], float(A0[8]), wr, float(A0[4])))
    t150 = np.array(t150)
    imp_thr = {}
    if len(t150):
        q1, q2 = np.quantile(t150[:, 0], [1/3, 2/3])
        for name, m in (("低σ", t150[:, 0] < q1), ("中σ", (t150[:, 0] >= q1) & (t150[:, 0] < q2)),
                        ("高σ", t150[:, 0] >= q2)):
            sub = t150[m]
            xs = sub[:, 1] / sub[:, 0]                      # walk/σ
            ys = sub[:, 2]
            edges = np.quantile(xs, np.linspace(0, 1, 9))
            pts = []
            for k in range(8):
                mm = (xs >= edges[k]) & (xs <= edges[k + 1])
                if mm.sum() >= 5:
                    pts.append(((edges[k] + edges[k + 1]) / 2, ys[mm].mean()))
            cross = float("nan")
            for k in range(len(pts) - 1):
                if pts[k][1] < 0 <= pts[k + 1][1]:
                    x0, y0 = pts[k]
                    x1, y1 = pts[k + 1]
                    cross = x0 + (0 - y0) * (x1 - x0) / (y1 - y0)
                    break
            imp_thr[name] = cross
            med_sd = float(np.median(sub[:, 0]))
            print(f"      {name}（n={m.sum()}，σ 中位 {med_sd:.1f}$）: (p̂−fill) 过 0 的 walk/σ "
                  f"{'—（区间内不穿零）' if cross != cross else f'{cross:+.3f}'}"
                  f"  ⇒ 折美元 "
                  f"{'—' if cross != cross else f'{cross*med_sd:+.1f}$'}")
    print(f"      ⚠️ 现行闸 = 43$（在 σ 中位 {np.median(t150[:,0]) if len(t150) else float('nan'):.1f}$ 上 "
          f"≈ 0.68σ）——模型隐含阈值的**σ 梯度**是 Track A 动态化的直接依据")

    # ── §8 判决 ──
    print("\n" + "=" * 96)
    print("§8 判决（四条全过才立项）")
    print("=" * 96)
    c1 = (lo > 0) and (dlt >= 10.0)
    c2 = sm["n"] >= 0.5 * b["n"]
    c3 = (p_tick < 0.05) and (p_win < 0.05)
    c4 = (d1o > 0 and d2o > 0) or (d1o < 0 and d2o < 0)
    print(f"  ① 日级配对 Δ 下界>0 且 Δ≥+10U: {'✅' if c1 else '❌'}（Δ{dlt:+.2f}U, "
          f"95%[{lo:+.2f},{hi:+.2f}]）")
    print(f"  ② 笔数 ≥ 基线 50%（{0.5*b['n']:.0f}）: {'✅' if c2 else '❌'}（n={sm['n']}）")
    print(f"  ③ tick 级 AUC p<0.05 且 窗口级 AUC p<0.05: {'✅' if c3 else '❌'}"
          f"（p={p_tick:.4f} / {p_win:.4f}）")
    print(f"  ④ 分半同向（{d1o:+.2f}/{d2o:+.2f}）且搜校 p<0.05（{p_search:.4f}）: "
          f"{'✅' if (c4 and p_search < 0.05) else '❌'}")
    print(f"  ⇒ {'✅ 立项' if all([c1, c2, c3, c4 and p_search < 0.05]) else '❌ 否'}")

    out = {
        "baseline": b,
        "main": {"n": sm["n"], "wr": sm["wr"], "pnl": sm["pnl"], "delta": dlt,
                 "lo": lo, "hi": hi, "h1": d1o, "h2": d2o,
                 "stage": {k: int(v) for k, v in st_cnt.items()},
                 "fill_p": [float(x) for x in dist(rows, "fill")],
                 "rem_p": [float(x) for x in dist(rows, "rem")]},
        "auc": {"tick": float(A_tick), "tick_p": float(p_tick),
                "win": float(A_win), "win_p": float(p_win)},
        "price_control": {str(k): v for k, v in price_tab.items()},
        "filter_control": {str(k): {"n": v[0]["n"], "wr": v[0]["wr"], "pnl": v[0]["pnl"],
                                    "delta": v[1], "lo": v[2], "hi": v[3]}
                           for k, v in filt_tab.items()},
        "delta_grid": {str(k): summarize(v) for k, v in grid_rows.items()},
        "search_p": {"sc": p_sc, "mn": p_mn, "conservative": p_search},
        "imp": {cols[k]: float(imp[k] / imp.sum()) for k in range(len(cols))},
    }
    (BASE / "data/track_b.json").write_text(json.dumps(out, indent=1, ensure_ascii=False),
                                            encoding="utf-8")
    print("\n[04] 明细已存 python/v7/data/track_b.json")


if __name__ == "__main__":
    main()
