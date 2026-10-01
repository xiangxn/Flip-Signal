#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v7-06 B2｜全窗模型：**从 0s 起喂**、label = 官方 outcome、**双侧可下单**。

预注册判据见 `docs/gbm_2026-10-01.md` §0.3B（写于 B1 出结果后、本脚本任何一次运行前）:

  **主规则（V0）**: tick 级 GBM 预测 `P(Up)`；逐秒扫**整窗**（`rem ∈ (0,300]`, 首个可判定
  tick 通常 rem≈298 = 窗口一开就喂），首个 `max(p̂ − yes_ask, (1−p̂) − no_ask) > δ`
  （δ=0）的 tick 下单（买 Up 付 `yes_ask`、买 Down 付 `no_ask`），整窗一单, 持到结算。
  训练按 1/窗内 tick 数加权（有效样本 = 窗数）；**不加价格地板 / rem 限制 / 规则腿**。

  分解变体（归因用）: V1 整窗·只买热门侧 / V2 `rem ≤ 150`·双侧 / V3 `rem ≤ 150`·只买热门侧。

  判据（四条全过才立项）: ① 日级配对 Δ 95% 下界 > 0 且 Δ ≥ +10U/14 天@2U;
  ② 笔数 ≥ 基线 50%; ③ tick 级 **与** 窗口级 OOF AUC 的日级整块置换 p < 0.05;
  ④ 分半同向 + δ 扫描的搜索校正置换 p < 0.05。

与 B1（04 号）的三处差别（也是本轮的全部变量）:
  1. 训练/扫描域 `rem ≤ 150` → **整窗 `rem ≤ 300`**（数据侧 `ticks_wide.npz`）;
  2. label `热门侧赢` → **官方 outcome**（`y = 1 ⟺ Up 赢`），特征换 **Up 朝向**（`UP_TICK`）;
  3. 入场侧 热门侧（固定） → **双侧择一**（模型价与两侧要价各自比）。

用法: python/venv/bin/python python/v7/06_full_window_gbm.py [nperm=200]
"""
import json
import sys
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import lib_features as F          # noqa: E402
import lib_sim as SIM             # noqa: E402
import lib_stats as S             # noqa: E402
import lib_universe as U          # noqa: E402

CUT = "2026-08-25"
DELTA_MAIN = 0.0
DELTA_GRID = (0.0, 0.01, 0.02, 0.03, 0.05, 0.08)

try:
    import lightgbm as lgb                                        # noqa: F401
except ImportError:                                               # pragma: no cover
    print("需要 lightgbm: python/venv/bin/pip install lightgbm")
    sys.exit(1)


# ── 数据装配（整窗网格 + Up 标签） ────────────────────────────────────────

def build_wide(data):
    """整窗设计矩阵: X（Up 朝向 14 特征）+ 标签 `y = 1 ⟺ Up 赢` + 权重 + 窗号 + 日号。"""
    X, y, wgt, wid, day = [], [], [], [], []
    for i in range(len(data)):
        w = data.window_wide(i)
        Aw, Xw = w["Aw"], w["Xw"]
        n = len(Aw)
        if n == 0 or w["outcome"] is None:
            continue
        X.append(Xw)
        y.append(np.full(n, w["outcome"] == 0, dtype=np.int8))
        wgt.append(np.full(n, 1.0 / n))
        wid.append(np.full(n, i, dtype=np.int32))
        day.append(np.full(n, w["day"], dtype=np.int32))
    return (np.vstack(X).astype(np.float32), np.concatenate(y).astype(np.int8),
            np.concatenate(wgt).astype(np.float64), np.concatenate(wid),
            np.concatenate(day))


def fit_predict(Xtr, ytr, wtr, Xte, seed=42, n_estimators=400, params=None):
    """单折模型（默认参数与 04 号逐字相同 —— 只换了数据域与标签）。"""
    p = dict(objective="binary", learning_rate=0.05, num_leaves=8,
             min_data_in_leaf=500, feature_fraction=0.9, bagging_fraction=0.9,
             bagging_freq=1, lambda_l2=1.0, verbose=-1, seed=seed, num_threads=4)
    p.update(params or {})
    ds = lgb.Dataset(Xtr, label=ytr, weight=wtr)
    m = lgb.train(p, ds, num_boost_round=n_estimators)
    return m.predict(Xte), m


def oof_wide(data, seed=42, n_folds=5, n_estimators=400, params=None):
    """按日 GroupKFold 出整窗 OOF p̂（= P(Up)）+ 特征重要度。"""
    X, y, wgt, wid, day = build_wide(data)
    days = np.unique(day)
    fold_of = {d: k % n_folds for k, d in enumerate(days)}
    p_hat = np.full(len(y), np.nan)
    imp = None
    n_models = 0
    for kf in range(n_folds):
        te = np.isin(day, [d for d in days if fold_of[d] == kf])
        tr = ~te
        p_te, mdl = fit_predict(X[tr], y[tr], wgt[tr], X[te],
                                seed=seed + kf, n_estimators=n_estimators, params=params)
        p_hat[te] = p_te
        imp = mdl.feature_importance("gain") if imp is None \
            else imp + mdl.feature_importance("gain")
        n_models += 1
    pcol = {}
    for i in range(len(data)):
        sl = data.wide_slice(i)
        if sl.stop > sl.start:
            pcol[i] = p_hat[sl]
    return pcol, imp / max(1, n_models), y, wid, day, X


# ── 决策（双侧）与模拟 ───────────────────────────────────────────────────

def decide_two_sided(pcol, delta, rem_max=None, hot_only=False):
    """首个「模型比要价高出 δ」的 tick 入场（整窗一单）。

    * `hot_only=True`  ⇒ 只买该 tick 的**热门侧**（V1/V3; 与 B1 同族）;
      否则**双侧择一**：买 `max(p̂ − yes_ask, (1−p̂) − no_ask)` 那一侧（V0/V2）。
    * `rem_max`  ⇒ 只在这个 rem 以内扫（V2/V3 = 150; 默认整窗）。
    比较符是**严格大于**（与 B1 的 `p̂ − fill > δ` 同式）。
    """
    def decide(w):
        i = w["i"]
        p = pcol.get(i)
        Aw = w["Aw"]
        if p is None or len(Aw) == 0:
            return None
        ya, na = Aw[:, F.C_YESA], Aw[:, F.C_NOA]
        m_yes, m_no = p - ya, (1.0 - p) - na
        if hot_only:
            buy_yes = Aw[:, F.C_SGN] > 0
            m = np.where(buy_yes, m_yes, m_no)
        else:
            buy_yes = m_yes >= m_no
            m = np.where(buy_yes, m_yes, m_no)
        ok = m > delta
        if rem_max is not None:
            ok = ok & (Aw[:, F.C_REM] <= rem_max)
        idx = np.nonzero(ok)[0]
        if len(idx) == 0:
            return None
        j = int(idx[0])
        t = Aw[j]
        px = float(ya[j] if buy_yes[j] else na[j])
        return _entry(w, t, j, "yes" if buy_yes[j] else "no", px, float(p[j]),
                      float(m[j]), float(ya[j]), float(na[j]))
    return decide


def decide_thr(kind, thr, rem_max=None):
    """无模型对照（判据要求「模型不是被一个常数门槛复现」）:

    * `kind="hot"`   —— 首个热门侧有效价 `> thr` 的 tick，买热门侧;
    * `kind="cheap"` —— 首个 `min(yes_ask, no_ask) < thr` 的 tick，买便宜那一侧。
    """
    def decide(w):
        Aw = w["Aw"]
        if len(Aw) == 0:
            return None
        ya, na = Aw[:, F.C_YESA], Aw[:, F.C_NOA]
        if kind == "hot":
            buy_yes = Aw[:, F.C_SGN] > 0
            px = np.where(buy_yes, ya, na)
            ok = px > thr
        else:
            buy_yes = ya < na
            px = np.where(buy_yes, ya, na)
            ok = px < thr
        if rem_max is not None:
            ok = ok & (Aw[:, F.C_REM] <= rem_max)
        idx = np.nonzero(ok)[0]
        if len(idx) == 0:
            return None
        j = int(idx[0])
        return _entry(w, Aw[j], j, "yes" if buy_yes[j] else "no", float(px[j]),
                      np.nan, np.nan, float(ya[j]), float(na[j]))
    return decide


def decide_first(first_side):
    """绝对基线对照: 首个可判定 tick（rem≈298）无条件买 `first_side` 一侧。"""
    def decide(w):
        Aw = w["Aw"]
        if len(Aw) == 0:
            return None
        t = Aw[0]
        ya, na = float(t[F.C_YESA]), float(t[F.C_NOA])
        if first_side == "hot":
            side = "yes" if t[F.C_SGN] > 0 else "no"
        else:
            side = "yes" if ya < na else "no"
        px = ya if side == "yes" else na
        return _entry(w, t, 0, side, px, np.nan, np.nan, ya, na)
    return decide


def decide_wr(pcol, theta, require_edge=True, rem_max=None):
    """「胜率最高」口径（§8 探索）: 每 tick 取模型认为**胜率更高**的那一侧, 首个
    「该侧胜率 ≥ θ（`require_edge` 时还要 > 该侧要价）」的 tick 入场, 整窗一单。

    ⚠️ 与 V0 的区别是**挑的是胜率不是边际**: θ 越高 ⇒ 入场越晚、价越贵。
    `require_edge=False` 就是纯胜率口径（`win rate ≥ θ` 即买, 不看价）——用来单独
    展示「只追胜率」会走到哪里。
    """
    th = float(theta)

    def decide(w):
        i = w["i"]
        p = pcol.get(i)
        Aw = w["Aw"]
        if p is None or len(Aw) == 0:
            return None
        p = np.asarray(p, dtype=np.float64)
        buy_yes = p >= 0.5
        wr = np.where(buy_yes, p, 1.0 - p)
        px = np.where(buy_yes, Aw[:, F.C_YESA], Aw[:, F.C_NOA])
        ok = wr >= th
        if require_edge:
            ok = ok & (wr > px)
        if rem_max is not None:
            ok = ok & (Aw[:, F.C_REM] <= rem_max)
        idx = np.nonzero(ok)[0]
        if len(idx) == 0:
            return None
        j = int(idx[0])
        t = Aw[j]
        return _entry(w, t, j, "yes" if buy_yes[j] else "no", float(px[j]),
                      float(p[j]), float(wr[j] - px[j]), float(t[F.C_YESA]),
                      float(t[F.C_NOA]))
    return decide


def mkt_pcol(data):
    """「零模型」版 p̂ = 去水市价 `yes_ask/(yes_ask+no_ask)`（逐窗逐 tick）。

    用途：把 `decide_two_sided` 的**同一条规则**跑在「市场自己」身上——模型若没有超过
    市价的选边能力，两者应当给出同一个结果（这是 V2 那种大数的第一道体检）。
    """
    out = {}
    for i in range(len(data)):
        sl = data.wide_slice(i)
        ya = data.Aw[sl, F.C_YESA].astype(np.float64)
        na = data.Aw[sl, F.C_NOA].astype(np.float64)
        with np.errstate(invalid="ignore", divide="ignore"):
            out[i] = ya / (ya + na)
    return out


def decide_last(pcol):
    """对照「最后一秒买模型看好的一侧」: 末个有效 tick 入场（把胜率推到极限）。"""
    def decide(w):
        i = w["i"]
        p = pcol.get(i)
        Aw = w["Aw"]
        if p is None or len(Aw) == 0:
            return None
        ya, na = Aw[:, F.C_YESA], Aw[:, F.C_NOA]
        ok = np.nonzero((ya > 0) & (na > 0))[0]
        if len(ok) == 0:
            return None
        j = int(ok[-1])
        t = Aw[j]
        buy_yes = float(p[j]) >= 0.5
        px = float(ya[j] if buy_yes else na[j])
        return _entry(w, t, j, "yes" if buy_yes else "no", px, float(p[j]),
                      float(max(p[j], 1 - p[j]) - px), float(ya[j]), float(na[j]))
    return decide


def _entry(w, t, j, side, px, p, margin, ya, na):
    sgn = t[F.C_SGN]
    return {"date": w["date"], "day": w["day"], "event_start": w["start_time"],
            "condition_id": w["condition_id"], "rem": float(t[F.C_REM]),
            "side": side, "fill": float(px), "gi": int(t[F.C_GI]),
            "sd": w["sd"], "dev": float(t[F.C_DEV]) * sgn,   # Up 朝向（诊断用）
            "p_hat": p, "margin": margin, "yes_ask": ya, "no_ask": na,
            "hot_side": "yes" if sgn > 0 else "no", "pos": j,
            "hot_px": float(t[F.C_FILL])}


def sim_full(data, decide, stake=U.STAKE):
    """逐窗跑 decide → 整窗一单的 P&L 行（成交价 = **实际买的那一侧的要价**）。"""
    rows = []
    for i in range(len(data)):
        w = data.window_wide(i)
        if w["outcome"] is None:
            continue
        r = decide(w)
        if r is None:
            continue
        won = 1 if ((w["outcome"] == 0) if r["side"] == "yes" else (w["outcome"] == 1)) else 0
        r["won"] = won
        r["win_i"] = i
        r["outcome"] = w["outcome"]
        r["pnl"] = (stake / r["fill"] - stake) if won else -stake
        rows.append(r)
    return rows


def alt_rows(rows, which):
    """反事实：**同一个决策 tick、换一侧买**（拆「模型选边」与「入场时点」的贡献）。

    `which`: `"hot"` = 买该 tick 的热门侧; `"opp"` = 买模型所选的反面; `"cheap"` =
    买要价更低的一侧。决策 tick 一字不动 ⇒ 差别只剩「那一秒买哪边」。
    """
    out = []
    for r in rows:
        if which == "hot":
            side = r["hot_side"]
        elif which == "opp":
            side = "no" if r["side"] == "yes" else "yes"
        else:
            side = "yes" if r["yes_ask"] < r["no_ask"] else "no"
        rr = dict(r)
        rr["side"] = side
        rr["fill"] = float(r["yes_ask"] if side == "yes" else r["no_ask"])
        rr["won"] = 1 if ((r["outcome"] == 0) if side == "yes" else (r["outcome"] == 1)) else 0
        rr["pnl"] = (U.STAKE / rr["fill"] - U.STAKE) if rr["won"] else -U.STAKE
        out.append(rr)
    return out


# ── 指标 ────────────────────────────────────────────────────────────────

def rank_cache(scores):
    """把「打分 → 平均秩」预计算好（并列取平均秩）——置换测试里 p̂ 不变、只有标签在变
    ⇒ 秩可复用, 省掉 200 次 argsort（整窗网格 107 万行, 一次约 1s）。"""
    s = np.asarray(scores, dtype=np.float64)
    ok = ~np.isnan(s)
    ss = s[ok]
    r = np.empty(len(ss), dtype=np.float64)
    if len(ss) == 0:
        return r, ok
    order = np.argsort(ss, kind="mergesort")
    ss_sorted = ss[order]
    newgrp = np.empty(len(ss_sorted), dtype=bool)
    newgrp[0] = True
    if len(ss_sorted) > 1:
        newgrp[1:] = ss_sorted[1:] != ss_sorted[:-1]
    gid = np.cumsum(newgrp) - 1
    cnt = np.bincount(gid)
    end_rank = np.cumsum(cnt)[gid]                  # 组内最大秩
    start_rank = end_rank - cnt[gid] + 1
    r[order] = (start_rank + end_rank) / 2.0
    return r, ok


def auc_cached(cache, labels):
    r, ok = cache
    lab = np.asarray(labels, dtype=np.float64)[ok]
    n1, n0 = lab.sum(), (1 - lab).sum()
    if n1 == 0 or n0 == 0:
        return float("nan")
    return (r[lab == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)


def auc(scores, labels):
    """ROC-AUC（并列取平均秩）——与 04 号同实现（04 用逐组循环, 这里向量化, 结果相同）。"""
    return auc_cached(rank_cache(scores), labels)


def brier(p, y, w):
    m = ~np.isnan(p)
    return float(np.average((p[m] - y[m]) ** 2, weights=w[m]))


def logloss(p, y, w):
    m = ~np.isnan(p)
    eps = 1e-9
    pp = np.clip(p[m], eps, 1 - eps)
    return float(-np.average(y[m] * np.log(pp) + (1 - y[m]) * np.log(1 - pp), weights=w[m]))


def summarize(rows):
    s = SIM.summarize(rows)
    if rows:
        s["side_yes"] = sum(1 for r in rows if r["side"] == "yes") / len(rows)
        s["rem_p"] = [float(x) for x in np.percentile([r["rem"] for r in rows], [10, 50, 90])]
        s["fill_p"] = [float(x) for x in np.percentile([r["fill"] for r in rows], [10, 50, 90])]
    return s


def delta_report(name, rows, base_day):
    """一行变体报告（n / WR / P&L / 日级配对 Δ + 区间 / 分半）。"""
    s = summarize(rows)
    dlt, lo, hi = S.boot_delta(base_day, S.day_pnl(rows))
    h1 = sum(r["pnl"] for r in rows if r["date"] < CUT)
    h2 = sum(r["pnl"] for r in rows if r["date"] >= CUT)
    d1 = h1 - sum(base_day.get(d, 0) for d in {r["date"] for r in rows if r["date"] < CUT})
    d2 = h2 - sum(base_day.get(d, 0) for d in {r["date"] for r in rows if r["date"] >= CUT})
    print(f"    {name:<26} n={s['n']:<5} WR {s['wr']:6.2f}%  P&L {s['pnl']:+8.2f}U  "
          f"Δ {dlt:+7.2f}U 95%[{lo:+8.2f},{hi:+8.2f}]  分半 {d1:+.2f}/{d2:+.2f}"
          + (f"  YES占比 {s['side_yes']*100:.1f}%" if s["n"] else ""))
    return s, dlt, lo, hi, d1, d2


# ── 主流程 ───────────────────────────────────────────────────────────────

def main():
    nperm = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    data = SIM.Data()
    if data.Aw is None:
        print("缺 ticks_wide.npz —— 先跑 01_build_dataset.py 重建数据集")
        sys.exit(1)
    base_rows = SIM.simulate(data, SIM.chain_decide(U.Rules()))
    base_day = S.day_pnl(base_rows)
    b = SIM.summarize(base_rows)
    pin_ok = abs(b["pnl"] - U.PIN["pnl"]) < 1e-6 and b["n"] == U.PIN["signals"]
    print("=" * 96)
    print("v7-06 B2｜全窗模型（从 0s 起扫整窗 + label = 官方 outcome + 双侧择一）")
    print("=" * 96)
    print(f"  基线（现规则链）: n={b['n']} WR {b['wr']:.4f}% P&L {b['pnl']:+.4f}U  "
          f"§0 {'✅' if pin_ok else '❌'}")
    if not pin_ok:
        sys.exit(1)

    # ── §1 模型 ──
    print("\n" + "=" * 96)
    print("§1 整窗模型（按日 GroupKFold 5 折 OOF; 权重 = 1/窗内 tick 数; label = Up 赢）")
    print("=" * 96)
    pcol, imp, y, wid, day, X = oof_wide(data)
    wcols = list(data.cols_w)
    print("  特征重要度（gain, 归一化）:")
    for k in np.argsort(-imp)[:len(wcols)]:
        print(f"       {wcols[k]:<22} {imp[k]/imp.sum()*100:6.2f}%")
    wgt = np.concatenate([np.full(len(data.window_wide(i)["Aw"]),
                                  1.0 / len(data.window_wide(i)["Aw"]))
                          for i in range(len(data)) if len(data.window_wide(i)["Aw"])])
    up_px = X[:, wcols.index("up_px")].astype(np.float64)
    dn_px = data.Aw[:, F.C_NOA].astype(np.float64)
    de_mid = up_px / (up_px + dn_px)          # 去水市价（yes_ask/(yes_ask+no_ask)）
    p_all = np.concatenate([pcol[i] for i in sorted(pcol)])
    print(f"\n  整窗网格 n={len(y)}（窗 {len(pcol)}）: 基础 Up 胜率 "
          f"{np.average(y, weights=wgt)*100:.2f}%")
    print(f"     模型          Brier {brier(p_all, y, wgt):.6f}  logloss {logloss(p_all, y, wgt):.6f}")
    print(f"     市价(要价)    Brier {brier(up_px, y, wgt):.6f}  logloss {logloss(up_px, y, wgt):.6f}")
    print(f"     市价(去水)    Brier {brier(de_mid, y, wgt):.6f}  logloss {logloss(de_mid, y, wgt):.6f}"
          f"   ← 零模型基准（去水 = yes_ask/(yes_ask+no_ask)）")
    print(f"     ⚠️ 模型 vs 市价(去水) 的 Brier 差 = "
          f"{brier(p_all, y, wgt) - brier(de_mid, y, wgt):+.6f}"
          f"（<0 才是跑赢）")

    # ── §8（计算）胜率口径的候选臂先算好（打印在 §8, 诊断在 §6 就要用） ──
    WR_THETAS = (0.80, 0.85, 0.90, 0.95, 0.97, 0.99)
    wr_tab = {th: sim_full(data, decide_wr(pcol, th)) for th in WR_THETAS}
    rs_pure = sim_full(data, decide_wr(pcol, 0.97, require_edge=False))
    rs_last = sim_full(data, decide_last(pcol))

    # ── §2 主规则 V0 ──
    print("\n" + "=" * 96)
    print(f"§2 主规则 V0（预注册）: 整窗双侧, 首个 max(p̂−yes_ask, (1−p̂)−no_ask) > {DELTA_MAIN}")
    print("=" * 96)
    rows = sim_full(data, decide_two_sided(pcol, DELTA_MAIN))
    s0, dlt, lo, hi, d1o, d2o = delta_report("V0 整窗·双侧", rows, base_day)
    n_win = len(pcol)
    print(f"    入场窗 {s0['n']}/{n_win}（{s0['n']/n_win*100:.1f}%）; "
          f"rem 分位[10,50,90] {np.round(s0['rem_p'],1)}; fill 分位 {np.round(s0['fill_p'],3)}")
    print(f"    入场侧分布: YES {sum(1 for r in rows if r['side']=='yes')} / "
          f"NO {sum(1 for r in rows if r['side']=='no')}; "
          f"与热门侧一致 {sum(1 for r in rows if r['side']==r['hot_side'])/max(1,len(rows))*100:.1f}%")

    # ── §3 变体 + 无模型对照 ──
    print("\n" + "=" * 96)
    print("§3 分解变体（预注册）与无模型对照")
    print("=" * 96)
    variants = {}
    variants["V1"] = sim_full(data, decide_two_sided(pcol, DELTA_MAIN, hot_only=True))
    variants["V2"] = sim_full(data, decide_two_sided(pcol, DELTA_MAIN, rem_max=U.T150))
    variants["V3"] = sim_full(data, decide_two_sided(pcol, DELTA_MAIN, rem_max=U.T150,
                                                     hot_only=True))
    for k, rs in variants.items():
        delta_report(k + (" 整窗·热门侧" if k == "V1" else
                          " rem≤150·双侧" if k == "V2" else " rem≤150·热门侧"), rs, base_day)
    # ── 零模型对照: **同一条规则换 p̂ = 去水市价**（V2 大数的第一道体检） ──
    pcol_mkt = mkt_pcol(data)
    print("    ── 零模型对照（同规则、p̂ = 去水市价） ──")
    zc = {}
    zc["Z0 整窗·双侧(市价)"] = sim_full(data, decide_two_sided(pcol_mkt, DELTA_MAIN))
    zc["Z2 rem≤150·双侧(市价)"] = sim_full(data,
                                            decide_two_sided(pcol_mkt, DELTA_MAIN,
                                                             rem_max=U.T150))
    for k, rs in zc.items():
        s, dlt_z, lo_z, hi_z, d1z, d2z = delta_report(k, rs, base_day)
        if s["n"]:
            print(f"          平均成交价 {np.mean([r['fill'] for r in rs]):.3f}  "
                  f"买热门侧占比 "
                  f"{sum(1 for r in rs if r['side']==r['hot_side'])/len(rs)*100:.1f}%")
    print("    ── 无模型对照 ──")
    ctrl = {}
    for thr in (0.55, 0.60, 0.70, 0.80):
        ctrl[("hot", thr)] = sim_full(data, decide_thr("hot", thr))
        delta_report(f"买热门侧 fill>{thr:.2f}", ctrl[("hot", thr)], base_day)
    for thr in (0.45, 0.40, 0.30):
        ctrl[("cheap", thr)] = sim_full(data, decide_thr("cheap", thr))
        delta_report(f"买便宜侧 px<{thr:.2f}", ctrl[("cheap", thr)], base_day)
    for name, fs in (("首 tick 买热门侧", "hot"), ("首 tick 买便宜侧", "cheap")):
        ctrl[("first", fs)] = sim_full(data, decide_first(fs))
        delta_report(name, ctrl[("first", fs)], base_day)

    # ── §4 判别力（判据 ③） ──
    print("\n" + "=" * 96)
    print("§4 判别力: tick 级 AUC（全整窗网格）与窗口级 AUC（V0 决策 tick）; 按日整块置换")
    print("=" * 96)
    A_tick, A_tick_mkt, A_tick_mkt2 = auc(p_all, y), auc(up_px, y), auc(de_mid, y)
    win_rows = {r["win_i"]: r for r in rows}
    win_list = sorted(win_rows)
    ws_p = np.array([win_rows[i]["p_hat"] for i in win_list])
    ws_y = np.array([win_rows[i]["won"] for i in win_list], dtype=float)
    A_win = auc(ws_p, ws_y)
    # 日级整块置换: 打乱「日 → 该日 outcome 列表」的对应
    out_by_day, win_by_day = {}, {}
    for i in range(len(data)):
        w = data.win[i]
        if w["outcome"] is None:
            continue
        out_by_day.setdefault(w["date"], []).append(w["outcome"])
        win_by_day.setdefault(w["date"], []).append(i)
    days_have = list(out_by_day)
    n_win = len(data)
    cache_tick = rank_cache(p_all)                # p̂ 不变 ⇒ 秩只算一次
    import random
    rng = random.Random(S.SEED)
    null_tick, null_win = [], []
    for _ in range(nperm):
        src = {d: rng.choice(days_have) for d in days_have}
        wout = {}
        for d in days_have:
            outs = out_by_day[src[d]]
            for k, wi in enumerate(win_by_day[d]):
                wout[wi] = outs[k % len(outs)]
        yp = (perm_out(wout, n_win)[wid] == 0).astype(np.int8)
        null_tick.append(auc_cached(cache_tick, yp))
        wy = np.array([1 if ((wout[i] == 0) if win_rows[i]["side"] == "yes"
                             else (wout[i] == 1)) else 0 for i in win_list], dtype=float)
        null_win.append(auc(ws_p, wy))
    p_tick = S.perm_p(A_tick, [v for v in null_tick if v == v])
    p_win = S.perm_p(A_win, [v for v in null_win if v == v])
    print(f"  tick 级 AUC 模型 {A_tick:.4f} / 市价(要价) {A_tick_mkt:.4f} / "
          f"市价(去水) {A_tick_mkt2:.4f}   置换零分布中位 {np.nanmedian(null_tick):.4f} ⇒ p={p_tick:.4f}")
    print(f"  窗口级 AUC {A_win:.4f}（n={len(ws_p)} 个决策 tick）"
          f"  置换零分布中位 {np.nanmedian(null_win):.4f} ⇒ p={p_win:.4f}")

    # ── §5 δ 扫描 + 折内选择 OOF + 搜索校正置换（判据 ④） ──
    print("\n" + "=" * 96)
    print("§5 δ 扫描（描述性）+ 折内选择 OOF + 搜索校正置换")
    print("=" * 96)
    grid_rows = {}
    for d_ in DELTA_GRID:
        rs = sim_full(data, decide_two_sided(pcol, d_))
        grid_rows[d_] = rs
        sm = summarize(rs)
        print(f"    δ={d_:.2f}   n={sm['n']:<5} WR {sm['wr']:6.2f}%  P&L {sm['pnl']:+8.2f}U"
              f"  Δ {sm['pnl']-b['pnl']:+7.2f}U")
    days = sorted({w["date"] for w in data.win})
    fold = {d: k % 5 for k, d in enumerate(days)}
    day_of_v = {d_: S.day_pnl(rs) for d_, rs in grid_rows.items()}
    win_of_v = {d_: {r["win_i"]: r for r in rs} for d_, rs in grid_rows.items()}

    def oof_pick(fold_of_day):
        oof, best = [], {}
        for kf in range(5):
            tr = [d for d in days if fold_of_day[d] != kf]
            te = {d for d in days if fold_of_day[d] == kf}
            bk = max(DELTA_GRID, key=lambda v: (sum(day_of_v[v].get(d, 0.0) for d in tr), -v))
            best[kf] = bk
            for wi, r in win_of_v[bk].items():
                if data.win[wi]["date"] in te:
                    oof.append(r)
        return oof, best

    oof, best = oof_pick(fold)
    so = summarize(oof)
    dlt_o, lo_o, hi_o = S.boot_delta(base_day, S.day_pnl(oof))
    d1 = sum(r["pnl"] for r in oof if r["date"] < CUT) - \
        sum(base_day.get(d, 0) for d in {r["date"] for r in oof if r["date"] < CUT})
    d2 = sum(r["pnl"] for r in oof if r["date"] >= CUT) - \
        sum(base_day.get(d, 0) for d in {r["date"] for r in oof if r["date"] >= CUT})
    print(f"  折内最优 δ: " + ", ".join(f"折{k}→{v}" for k, v in sorted(best.items())))
    print(f"  OOF: n={so['n']} WR {so['wr']:.2f}% P&L {so['pnl']:+.2f}U Δ={dlt_o:+.2f}U "
          f"95%[{lo_o:+.2f},{hi_o:+.2f}]  分半 Δ {d1:+.2f}/{d2:+.2f}")

    real_out = {i: w["outcome"] for i, w in enumerate(data.win) if w["outcome"] is not None}

    def money_rows(rows_, wout):
        if not rows_:
            return 0.0
        s = 0.0
        for r in rows_:
            won = 1 if ((wout[r["win_i"]] == 0) if r["side"] == "yes"
                        else (wout[r["win_i"]] == 1)) else 0
            s += ((U.STAKE / r["fill"] - U.STAKE) if won else -U.STAKE)
        return s / len(rows_)

    obs_mn = money_rows(oof, real_out) - money_rows(base_rows, real_out)
    null_mn = []
    for _ in range(nperm):
        src = {d: rng.choice(days_have) for d in days_have}
        wout = {}
        for d in days_have:
            outs = out_by_day[src[d]]
            for k, wi in enumerate(win_by_day[d]):
                wout[wi] = outs[k % len(outs)]
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
            te = {d for d in days if fold[d] == kf}
            bk = max(DELTA_GRID, key=lambda v: (sum(dpv[v].get(d, 0.0) for d in tr), -v))
            for wi, r in win_of_v[bk].items():
                if data.win[wi]["date"] in te:
                    sel.append(r)
        null_mn.append(money_rows(sel, wout) - money_rows(base_rows, wout))
    p_search = S.perm_p(obs_mn, null_mn)
    print(f"  置换统计量（折外 OOF 臂 − 基线, 每笔 P&L）: {obs_mn:+.4f}U vs p95 "
          f"{np.percentile(null_mn,95):+.4f}U ⇒ 搜校 p={p_search:.4f}")
    print(f"  （另: 真实 out 下每笔 P&L 差 = {obs_mn:+.4f}U 对应整族 Δ "
          f"{obs_mn*so['n']:+.2f}U）")

    # ── §6 诊断 ──
    print("\n" + "=" * 96)
    print("§6 诊断: 入场时点/价格/侧别 · (模型价−要价) 分桶 · rem 分桶 · 校准 · 可挂单性")
    print("=" * 96)
    print("  (a) 全整窗网格「双侧择一」的（模型价 − 要价）十分位 × 实现校准缺口:")
    m_yes = p_all - up_px
    m_no = (1.0 - p_all) - dn_px
    buy_yes = m_yes >= m_no
    margin = np.where(buy_yes, m_yes, m_no)
    px_buy = np.where(buy_yes, up_px, dn_px)
    won_buy = np.where(buy_yes, y == 1, y == 0).astype(float)
    qs = np.quantile(margin, np.linspace(0, 1, 11))
    print("      桶        (模型价−要价) 区间       n      平均价   实现胜率   缺口(胜率−价)")
    for k in range(10):
        m = (margin >= qs[k]) & (margin <= qs[k + 1])
        print(f"      D{k+1:<2} [{qs[k]:+.3f},{qs[k+1]:+.3f}]  {m.sum():>7}   "
              f"{px_buy[m].mean():.4f}   {won_buy[m].mean()*100:6.2f}%   "
              f"{won_buy[m].mean()-px_buy[m].mean():+.4f}")
    print("  (b) V0 入场价分桶 EV + 入场 rem 分桶 EV:")
    fills = np.array([r["fill"] for r in rows])
    for lo_, hi_ in ((0.30, 0.50), (0.50, 0.70), (0.70, 0.85), (0.85, 0.95), (0.95, 1.01)):
        m = (fills >= lo_) & (fills < hi_)
        if m.sum() == 0:
            continue
        sub = [r for r, mm in zip(rows, m) if mm]
        print(f"      fill ∈ [{lo_:.2f},{hi_:.2f})  n={m.sum():<5} WR "
              f"{sum(r['won'] for r in sub)/len(sub)*100:6.2f}%  "
              f"P&L {sum(r['pnl'] for r in sub):+7.2f}U")
    rems = np.array([r["rem"] for r in rows])
    for lo_, hi_ in ((0, 60), (60, 150), (150, 240), (240, 301)):
        m = (rems >= lo_) & (rems < hi_)
        if m.sum() == 0:
            continue
        sub = [r for r, mm in zip(rows, m) if mm]
        print(f"      rem ∈ [{lo_:>3},{hi_:>3})  n={m.sum():<5} WR "
              f"{sum(r['won'] for r in sub)/len(sub)*100:6.2f}%  "
              f"P&L {sum(r['pnl'] for r in sub):+7.2f}U")
    print("  (c) 校准（整窗 tick 级, 权重 1/窗内 tick 数; 模型 vs 市价去水）:")
    for lo_, hi_ in ((0.0, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.8), (0.8, 1.01)):
        m = (de_mid >= lo_) & (de_mid < hi_)
        if m.sum() == 0:
            continue
        print(f"      去水市价 ∈ [{lo_:.2f},{hi_:.2f})  n={m.sum():<7} 实际 Up 胜率 "
              f"{y[m].mean()*100:6.2f}%  市价均值 {de_mid[m].mean():.3f}  "
              f"模型均值 {np.nanmean(p_all[m]):.3f}")
    print("  (b2) 入场画像（fill / rem 分位; 侧别构成）:")
    prof = [("V0 整窗·双侧", rows), ("V1 整窗·热门", variants["V1"]),
            ("V2 rem≤150·双侧", variants["V2"]), ("V3 rem≤150·热门", variants["V3"])]
    prof += [(f"θ={k:.2f}", wr_tab[k]) for k in WR_THETAS]
    prof += [("纯胜率 θ=0.97", rs_pure), ("最后一秒", rs_last)]
    for nm, rs in prof:
        if not rs:
            continue
        sm = summarize(rs)
        fq = np.percentile([r["fill"] for r in rs], [10, 50, 90])
        rq = np.percentile([r["rem"] for r in rs], [10, 50, 90])
        hot = sum(1 for r in rs if r["side"] == r["hot_side"]) / len(rs) * 100
        print(f"      {nm:<16} n={sm['n']:<5} WR {sm['wr']:6.2f}%  成交价 p10/50/90 "
              f"{fq[0]:.3f}/{fq[1]:.3f}/{fq[2]:.3f}  rem p10/50/90 "
              f"{rq[0]:.0f}/{rq[1]:.0f}/{rq[2]:.0f}s  买热门侧占比 {hot:5.1f}%")
    print("  (b2b) 成交价分桶账（**大数从哪来**; 平价 = WR 恰好等于成交价的档）:")
    for nm, rs in (("V2 rem≤150·双侧", variants["V2"]), ("θ=0.90", wr_tab[0.90]),
                   ("θ=0.99", wr_tab[0.99]), ("最后一秒", rs_last)):
        if not rs:
            continue
        print(f"      ── {nm} ──")
        for lo_, hi_ in ((0.0, 0.15), (0.15, 0.30), (0.30, 0.50), (0.50, 0.70),
                         (0.70, 0.85), (0.85, 0.95), (0.95, 1.01)):
            sub = [r for r in rs if lo_ <= r["fill"] < hi_]
            if not sub:
                continue
            w = sum(r["won"] for r in sub)
            print(f"         fill ∈ [{lo_:.2f},{hi_:.2f})  n={len(sub):<5} WR "
                  f"{w/len(sub)*100:6.2f}%  P&L {sum(r['pnl'] for r in sub):+8.2f}U  "
                  f"赢单 +{np.mean([r['pnl'] for r in sub if r['won']]) if w else 0:.2f}U/ "
                  f"输单 −{U.STAKE:.0f}U")
    print("  (b3) 反事实分解（**同一决策 tick 换一侧**, 拆「选边」与「时点」的贡献）:")
    for nm, rs in (("V0", rows), ("V2", variants["V2"]), ("θ=0.90", wr_tab[0.90]),
                   ("θ=0.99", wr_tab[0.99])):
        if not rs:
            continue
        sm0 = summarize(rs)
        print(f"      {nm:<8} 模型选边 n={sm0['n']:<5} WR {sm0['wr']:6.2f}% "
              f"P&L {sm0['pnl']:+8.2f}U   （同 tick 换边 ↓）")
        for which, lab in (("hot", "买热门侧"), ("opp", "买反面"), ("cheap", "买便宜侧")):
            ra = alt_rows(rs, which)
            sa = summarize(ra)
            print(f"               {lab:<8} n={sa['n']:<5} WR {sa['wr']:6.2f}% "
                  f"P&L {sa['pnl']:+8.2f}U  平均价 "
                  f"{np.mean([r['fill'] for r in ra]):.3f}")
    print("  (d) 可挂单性（CLOB `minimum_order_size = 5 股`）: stake=2U 时 fill < 0.40 买不到 5 股")
    for nm, rs in (("V0", rows),) + tuple((k, v) for k, v in variants.items()) \
            + tuple((f"θ={k:.2f}", wr_tab[k]) for k in WR_THETAS) \
            + (("纯胜率 θ=0.97", rs_pure), ("最后一秒", rs_last)):
        if not rs:
            continue
        bad = sum(1 for r in rs if U.STAKE / r["fill"] < 5.0)
        print(f"      {nm}: {bad}/{len(rs)} 笔（{bad/len(rs)*100:.1f}%）低于交易所下限")

    # ── §7 判决 ──
    print("\n" + "=" * 96)
    print("§7 判决（四条全过才立项; V0 = 预注册主规则）")
    print("=" * 96)
    c1 = (lo > 0) and (dlt >= 10.0)
    c2 = s0["n"] >= 0.5 * b["n"]
    c3 = (p_tick < 0.05) and (p_win < 0.05)
    c4 = ((d1o > 0 and d2o > 0) or (d1o < 0 and d2o < 0)) and (p_search < 0.05)
    print(f"  ① 日级配对 Δ 下界>0 且 Δ≥+10U: {'✅' if c1 else '❌'}（Δ{dlt:+.2f}U, "
          f"95%[{lo:+.2f},{hi:+.2f}]）")
    print(f"  ② 笔数 ≥ 基线 50%（{0.5*b['n']:.0f}）: {'✅' if c2 else '❌'}（n={s0['n']}）")
    print(f"  ③ 分级 AUC p<0.05（tick 与窗口都要）: {'✅' if c3 else '❌'}"
          f"（p={p_tick:.4f} / {p_win:.4f}）")
    print(f"  ④ 分半同向（{d1o:+.2f}/{d2o:+.2f}）且搜校 p<0.05（{p_search:.4f}）: "
          f"{'✅' if c4 else '❌'}")
    print(f"  ⇒ {'✅ 立项' if all([c1, c2, c3, c4]) else '❌ 否'}")

    # ── §8 「扫尾盘胜率最高」口径（用户口径的直接实现; **探索, 非 §0.3B 判据**） ──
    print("\n" + "=" * 96)
    print("§8 胜率口径（探索, 非判据）: 整窗扫描, 取模型认为胜率更高的一侧, 首个「胜率 ≥ θ 且 > 要价」")
    print("=" * 96)
    wr_stat = {}
    for th in WR_THETAS:
        rs = wr_tab[th]
        s, dlt_w, lo_w, hi_w, h1_w, h2_w = delta_report(f"θ={th:.2f} 胜率档", rs, base_day)
        wr_stat[th] = {"s": s, "d": (dlt_w, lo_w, hi_w, h1_w, h2_w)}
        if s["n"]:
            print(f"          平均成交价 {np.mean([r['fill'] for r in rs]):.3f}  "
                  f"rem 中位 {np.median([r['rem'] for r in rs]):.0f}s")
    # 纯胜率口径（不看价）——单独一行, 展示「只追胜率」会退化成什么
    s_pure, dlt_p, lo_p, hi_p, h1_p, h2_p = delta_report("θ=0.97 纯胜率(不看价)",
                                                         rs_pure, base_day)
    print(f"          平均成交价 {np.mean([r['fill'] for r in rs_pure]):.3f}")
    s_l, dlt_l, lo_l, hi_l, h1_l, h2_l = delta_report("最后一秒·模型看好侧", rs_last,
                                                      base_day)
    print(f"          平均成交价 {np.mean([r['fill'] for r in rs_last]):.3f}"
          f"  ← 胜率可以刷到很高, 但赢的钱只有 (1/价 − 1)")
    print("    胜率面（逐 rem 桶; 「更高侧」= 模型认为胜率高的那一侧）:")
    print(f"      {'rem 桶':<14}{'窗数':>6}{'模型给胜率':>12}{'该侧实战胜率':>14}"
          f"{'Δ(实战−模型)':>14}{'该侧要价均值':>14}")
    for lo_, hi_ in ((0, 30), (30, 60), (60, 90), (90, 120), (120, 180), (180, 240),
                     (240, 301)):
        pv, rv, xv = [], [], []
        for i in range(len(data)):
            w = data.window_wide(i)
            p = pcol.get(i)
            if p is None:
                continue
            Aw = w["Aw"]
            m = (Aw[:, F.C_REM] >= lo_) & (Aw[:, F.C_REM] < hi_)
            m = m & (Aw[:, F.C_YESA] > 0) & (Aw[:, F.C_NOA] > 0)
            if not m.any():
                continue
            pm = p[m]
            by = pm >= 0.5                       # 模型看好的那一侧是不是 Up
            wr_t = np.where(by, pm, 1.0 - pm)    # 模型自报胜率
            win_up = bool(w["outcome"] == 0)
            # ⚠️ 必须用 `not`：Python 的 `~True == -2`（按位取反的是底层 int）
            real = np.where(by, win_up, not win_up)   # 该侧实际赢没赢（同窗同 outcome）
            pv.append(wr_t.mean())
            rv.append(real.mean())
            xv.append(np.where(by, Aw[m, F.C_YESA], Aw[m, F.C_NOA]).mean())
        if pv:
            print(f"      [{lo_:>3},{hi_:>3})   {len(pv):>5}{np.mean(pv)*100:>11.2f}%"
                  f"{np.mean(rv)*100:>13.2f}%{(np.mean(rv)-np.mean(pv))*100:>+13.2f}pp"
                  f"{np.mean(xv):>14.3f}")

    out = {
        "baseline": b,
        "wr_rule": {str(k): v["s"] for k, v in wr_stat.items()},
        "wr_rule_delta": {str(k): {"d": v["d"][0], "lo": v["d"][1], "hi": v["d"][2],
                                   "h1": v["d"][3], "h2": v["d"][4]}
                          for k, v in wr_stat.items()},
        "wr_pure": {"s": s_pure, "d": dlt_p, "lo": lo_p, "hi": hi_p},
        "last_tick": {"s": s_l, "d": dlt_l, "lo": lo_l, "hi": hi_l},
        "main": {"n": s0["n"], "wr": s0["wr"], "pnl": s0["pnl"], "delta": dlt,
                 "lo": lo, "hi": hi, "h1": d1o, "h2": d2o,
                 "side_yes": s0.get("side_yes"), "rem_p": s0.get("rem_p"),
                 "fill_p": s0.get("fill_p")},
        "variants": {k: summarize(v) for k, v in variants.items()},
        "auc": {"tick": float(A_tick), "tick_p": float(p_tick), "win": float(A_win),
                "win_p": float(p_win), "tick_mkt": float(A_tick_mkt),
                "tick_mkt_devig": float(A_tick_mkt2)},
        "calib": {"brier_model": brier(p_all, y, wgt),
                  "brier_ask": brier(up_px, y, wgt),
                  "brier_devig": brier(de_mid, y, wgt),
                  "logloss_model": logloss(p_all, y, wgt),
                  "logloss_devig": logloss(de_mid, y, wgt)},
        "delta_grid": {str(k): summarize(v) for k, v in grid_rows.items()},
        "search_p": p_search,
        "imp": {wcols[k]: float(imp[k] / imp.sum()) for k in range(len(wcols))},
    }
    (BASE / "data/full_window.json").write_text(
        json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    print("\n[06] 明细已存 python/v7/data/full_window.json")


def perm_out(wout, n_win):
    """{窗号: 置换后的 outcome} → 按窗号索引的 numpy 数组（再用行→窗号 `wid` 取用）。"""
    return np.array([wout.get(i, -1) for i in range(n_win)], dtype=np.int64)


if __name__ == "__main__":
    main()
