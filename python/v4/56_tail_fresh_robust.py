#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫尾盘 · 全新数据面 第 6 步：候选条件的稳健性审判

55 号段内扫描给出的候选（全部落在**同一条**机制上：T=150 检查点的 walk 门槛）：
    walk_min 43 → 80  ⇒  Δ +13.64U  [+0.99, +26.13]（CI 下界 > 0），h1/h2 同号

审判项（对齐仓库既有纪律：doc 29 的搜索校正置换 / doc 40 §9 的价格地板对照）：
  【0】自检：本文件的快速链接线与 51 号 run() 逐位一致
  【1】walk 细网格（40~140 步长 5）+ 逐日 Δ + 分半
  【2】搜索校正置换（层内打乱 walk、每次重扫整格取最大）⇒ 校正 p 值
  【3】价格对照：这道闸是「walk 信息」还是「贵价角落地伪装」？
        (a) T150 评估集内 walk 档 × 价格档 交叉表
        (b) 价格腿单调抬：p > X 在 T150，Δ 与 Δn 对照
        (c) walk≥80 的增益在价格档之间是否均匀
  【4】机制账：被拦的那批在基线里是什么、去哪了（死亡 / 改道 / 改道税）
  【5】交叉条件：walk≥80 之上再加第二条腿（basis / dev_sd / p / sd / rem …）有没有增量
  【6】σ 归一：walk ≥ k·sd 是否等效（80 美元是不是某个 σ 倍数）
"""
from __future__ import annotations

import importlib.util
import os
import sys

import numpy as np

_here = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("fresh51", os.path.join(_here, "51_tail_fresh_chain.py"))
C = importlib.util.module_from_spec(_spec)
sys.modules["fresh51"] = C
_spec.loader.exec_module(C)

_first = C._first
EPS = 1e-9
STAKE = C.STAKE


# ------------------------------------------------------------------ 快速链（从 ok150 起）
def chain(prep, ok150):
    """与 51 号 run() 的 T60/监听段逐位一致，只把 T150 段换成外部传入的 ok150。"""
    W, T = prep.V.shape
    wrow = np.arange(W)
    fired = np.zeros(W, dtype=bool)
    stage = np.zeros(W, dtype=np.int32)
    idx = np.full(W, -1, dtype=np.int32)
    fire150 = ok150 & ~prep.skip
    fired |= fire150
    stage[fire150] = 1
    idx[fire150] = prep.i150[fire150]

    t60_eval = prep.has60 & ~fired
    ok60 = np.zeros(W, dtype=bool)
    ok60[t60_eval] = prep.r5_t60[wrow[t60_eval], prep.i60[t60_eval]]
    fire60 = ok60 & ~fired
    fired |= fire60
    stage[fire60] = 2
    idx[fire60] = prep.i60[fire60]

    fallback = np.where(prep.has150, prep.i150, -1)
    bound = np.where(prep.has60, prep.i60, fallback)
    mL = prep.V & (prep.ar > bound[:, None]) & (prep.rem > 0)
    iL = _first(mL & prep.r2_lis)
    fireL = (iL >= 0) & ~fired
    fired |= fireL
    stage[fireL] = 3
    idx[fireL] = iL[fireL]

    won = np.zeros(W, dtype=bool)
    fi = np.nonzero(fired)[0]
    hot_yes = prep.hot_yes[fi, idx[fi]]
    oc = prep.outcome[fi]
    won[fi] = (hot_yes & (oc == 0)) | (~hot_yes & (oc == 1))
    fill = prep.p[fi, idx[fi]]
    shares = STAKE / fill
    pnl = np.where(won[fi], shares - STAKE, -STAKE)
    fee = shares * prep.rules.fee_rate * fill * (1 - fill)
    pw = np.zeros(W); pn = np.zeros(W)
    pw[fi] = pnl; pn[fi] = pnl - fee
    return C.Entries(w=W, fired=fired, stage=stage, idx=idx,
                     reason=np.zeros(W, dtype=np.int32), won=won, pnl=pw, pnl_net=pn)


def boot(e, base, panel, n=4000, seed=56):
    return C.boot_delta(panel, e, base, n=n, seed=seed)


def main() -> int:
    ap = __import__("argparse").ArgumentParser()
    ap.add_argument("--sec", default="all")
    ap.add_argument("--nperm", type=int, default=500)
    args = ap.parse_args()

    ws = C.load()
    panel, skip = C.build_panel(ws)
    W = panel.W
    wrow = np.arange(W)

    base = C.simulate(panel, C.Rules(), skip)          # 现行链 = walk 43
    B = float(base.pnl.sum()); N = int(base.fired.sum())

    prep_off = C.prepare(panel, C.Rules(walk_min=0.0), skip)   # 闸关：r5_t150 = ⑤ 本体
    ev = prep_off.t150_eval
    i150 = prep_off.i150
    ok_base_150 = np.zeros(W, dtype=bool)
    ok_base_150[ev] = prep_off.r5_t150[wrow[ev], i150[ev]]      # ⑤（无闸）在评估 tick
    walk_ev = np.full(W, np.nan)
    walk_ev[ev] = panel.walk[wrow[ev], i150[ev]]
    p150 = panel.p[wrow, np.maximum(i150, 0)]           # 评估 tick 上的有效价（无 eval 的窗为垃圾值）
    f150 = base.fired & (base.stage == 1)               # 基线在 T150 成交的窗
    won150 = np.zeros(W, dtype=bool)
    _ix = base.idx[f150]; _r = np.nonzero(f150)[0]
    won150[_r] = (panel.hot_yes[_r, _ix] & (panel.outcome[_r] == 0)) | \
                 (~panel.hot_yes[_r, _ix] & (panel.outcome[_r] == 1))

    # ---- 自检：快速链 + walk≥43 必须逐位复现基线
    chk = chain(prep_off, ok_base_150 & (walk_ev >= 43.0))
    assert int(chk.fired.sum()) == N and abs(float(chk.pnl.sum()) - B) < 1e-9, \
        f"自检失败 chk={int(chk.fired.sum())}/{float(chk.pnl.sum()):.4f} vs {N}/{B:.4f}"
    print(f"【0】自检 ✅ 快速链 walk≥43 与 51 号 run() 逐位一致（n={N} 毛 {B:+.4f}U）")

    days = sorted(set(panel.day))
    mid = days[len(days) // 2]
    dh1 = np.array([d < mid for d in panel.day]); dh2 = ~dh1
    t = C.entry_table(panel, base)

    def run_walk(X, extra=None, require_ev=True):
        ok = ok_base_150 & (walk_ev >= X)
        if extra is not None:
            ok = ok & extra
        return chain(prep_off, ok)

    def line(tag, e, n_boot=3000, seed=56):
        d = float(e.pnl.sum()) - B
        _, lo, hi = boot(e, base, panel, n=n_boot, seed=seed)
        st = [int(((e.stage == s) & e.fired).sum()) for s in (1, 2, 3)]
        a = float(e.pnl[dh1].sum() - base.pnl[dh1].sum())
        b = float(e.pnl[dh2].sum() - base.pnl[dh2].sum())
        print(f"  {tag:<26}{int(e.fired.sum()):>6}{int(e.fired.sum())-N:>+6}"
              f"{e.pnl.sum():>+10.2f}{d:>+9.2f}{lo:>+10.2f}{hi:>+10.2f}{a:>+9.2f}{b:>+9.2f}   {st}")
        return d

    # ---------------------------------------------------------------- 【1】细网格
    if args.sec in ("all", "1"):
        print("\n" + "=" * 108)
        print("【1】walk_min 细网格（T150 检查点；0 = 关闸）—— Δ 相对现行 43")
        print(f"  {'walk_min':<26}{'n':>6}{'Δn':>6}{'毛P&L':>10}{'ΔP&L':>9}"
              f"{'95%下界':>10}{'95%上界':>10}{'h1':>9}{'h2':>9}   段分布")
        grid = [0.0, 30.0, 40.0] + [43.0] + list(range(45, 141, 5))
        deltas = {}
        for X in grid:
            e = chain(prep_off, ok_base_150 & (walk_ev >= X)) if X > 0 else chain(prep_off, ok_base_150)
            deltas[X] = line(f"{X:.0f}", e, n_boot=2000)
        best = max((k for k in deltas if k > 0), key=lambda k: deltas[k])
        print(f"  ⇒ 名义最优 walk_min={best:.0f}（Δ {deltas[best]:+.2f}U）")

    # ----------------------------------------------------- 【2】搜索校正置换（max-stat）
    # ------------------------------------- 【2b】联合校正：打乱 outcome，整个 T150 条件族取最大
    if args.sec in ("all", "2b"):
        print("\n" + "=" * 108)
        print("【2b】族级搜索校正：把**整个扫过的条件空间**一起重扫取最大 Δ")
        print("      族 = T150 掩码族（12 特征 × 10 阈值 × 2 方向）+ 参数族（单参数 + walk×其他双参数）")
        SDc = np.broadcast_to(panel.sd[:, None], panel.p.shape)
        wc = panel.walk[wrow, np.maximum(i150, 0)]
        dc = panel.dev[wrow, np.maximum(i150, 0)]
        bc = panel.basis[wrow, np.maximum(i150, 0)]
        rc = panel.rem[wrow, np.maximum(i150, 0)].astype(float)
        feats = {
            "walk": wc, "dev": dc, "basis": bc, "p": p150,
            "walk/sd": wc / np.maximum(panel.sd, 1e-9),
            "dev/sd": dc / np.maximum(panel.sd, 1e-9),
            "basis/sd": bc / np.maximum(panel.sd, 1e-9),
            "sd": panel.sd, "rem": rc,
            "raw_move": panel.spot[wrow, np.maximum(i150, 0)] - panel.anchor,
            "raw_tw": panel.twap[wrow, np.maximum(i150, 0)] - panel.anchor,
            "spot": panel.spot[wrow, np.maximum(i150, 0)],
        }
        ev_rows = np.nonzero(ev)[0]
        # 把「我们真正提的那几个条件」原样塞进族里（否则校正会低估搜索宽度）
        CAND = {"walk": (43.0, 80.0), "dev": (63.0,), "sd": (40.0,), "p": (0.95, 0.99)}
        conds = []
        for name, v in feats.items():
            vv = v[ev_rows]
            cuts = sorted(set([float(np.percentile(vv, q)) for q in range(5, 96, 10)]
                              + list(CAND.get(name, ()))))
            for op in (">=", "<="):
                for X in cuts:
                    extra = (v >= X) if op == ">=" else (v <= X)
                    ok = ok_base_150 & extra
                    ok[~ev] = False
                    e = chain(prep_off, ok)
                    conds.append((f"{name} {op} {X:.2f}", e))
        # 参数族：真正的 Rules 改动（对三段都生效的旋钮 + walk 与它们的双参数组合）
        def _rl(tag, **kw):
            conds.append((tag, C.run(C.prepare(panel, C.Rules(**kw), skip))))
        for w_ in (0.0, 60.0, 70.0, 80.0, 100.0, 130.0):
            _rl(f"walk_min={w_:.0f}", walk_min=w_)
        for dv in (70.0, 75.0, 80.0, 90.0):
            _rl(f"dev_min={dv:.0f}", dev_min=dv)
        for fl in (0.0, 0.90, 0.95, 0.97):
            _rl(f"floor_min={fl}", floor_min=fl)
        for pm in (0.85, 0.90):
            _rl(f"price_min={pm}", price_min=pm)
        for sm in (0.0, 30.0, 50.0, 60.0):
            _rl(f"sd_min={sm:.0f}", sd_min=sm)
        for dv in (70.0, 75.0, 80.0):
            for w_ in (60.0, 70.0, 80.0, 100.0):
                _rl(f"walk{w_:.0f}+dev{dv:.0f}", walk_min=w_, dev_min=dv)
        for fl in (0.90, 0.95, 0.97):
            for w_ in (60.0, 70.0, 80.0):
                _rl(f"walk{w_:.0f}+floor{fl}", walk_min=w_, floor_min=fl)
        for pm in (0.85, 0.90):
            for w_ in (70.0, 80.0):
                _rl(f"walk{w_:.0f}+price{pm}", walk_min=w_, price_min=pm)
        for sm in (0.0, 50.0):
            for w_ in (70.0, 80.0):
                _rl(f"walk{w_:.0f}+sd{sm:.0f}", walk_min=w_, sd_min=sm)
        # 基线世界
        eb = base
        rows_b = np.nonzero(eb.fired)[0]
        # 每个条件：fired 行集合（路由固定 ⇒ 置换下不用重跑链）
        NC = len(conds)
        fired_m = np.zeros((NC, W), dtype=bool)
        hot_m = np.zeros((NC, W), dtype=bool)
        invf = np.zeros((NC, W))
        nobs = np.zeros(NC)
        obs = np.zeros(NC)
        for k, (tag, e) in enumerate(conds):
            r = np.nonzero(e.fired)[0]
            fired_m[k, r] = True
            hot_m[k, r] = panel.hot_yes[r, e.idx[r]]
            invf[k, r] = 1.0 / panel.p[r, e.idx[r]]
            nobs[k] = len(r)
            obs[k] = float(e.pnl.sum())
        # 基线也做成一行
        r = rows_b
        fired_m = np.vstack([fired_m, np.zeros((1, W), dtype=bool)])
        hot_m = np.vstack([hot_m, np.zeros((1, W), dtype=bool)])
        invf = np.vstack([invf, np.zeros((1, W))])
        fired_m[-1, r] = True
        hot_m[-1, r] = panel.hot_yes[r, eb.idx[r]]
        invf[-1, r] = 1.0 / panel.p[r, eb.idx[r]]
        nobs = np.append(nobs, len(r))
        obs = np.append(obs, B)
        obs_delta = obs[:NC] - B
        best_k = int(np.argmax(obs_delta))
        is_walk = np.array(["walk" in c[0] for c in conds])
        kw_ = int(np.argmax(np.where(is_walk, obs_delta, -1e9)))
        print(f"      族 = {NC} 个条件（{int(is_walk.sum())} 个含 walk）")
        print(f"      观测：族内最大 Δ = {obs_delta[best_k]:+.2f}U（{conds[best_k][0]}）"
              f"；walk 子族最大 = {obs_delta[kw_]:+.2f}U（{conds[kw_][0]}）")
        top = np.argsort(-obs_delta)[:6]
        print("      观测 Δ 前 6：" + " | ".join(
            f"{conds[i][0]} {obs_delta[i]:+.2f}" for i in top))

        # 两个互补零模型（都对**整个族**取最大 Δ ⇒ family-wise）：
        #  ① 有效价格零：每窗一枚 U(0,1) 硬币 u_i（跨条件、跨两世界共享），win = u_i < 该世界成交价。
        #     每扇窗的期望 P&L 恒为 0 ⇒ 任何条件的 E[Δ] ≡ 0（无方向性偏差）；代价 = 假设窗间独立
        #     （忽略「同日同 regime」的聚集）⇒ 偏乐观。
        #  ② 结局置换零：层 =（基线成交价 2 位小数 × 侧别 × 日）内打乱 outcome ⇒ 胜率与日结构
        #     逐格精确保住；但改道窗按**新价**成交、胜率仍按原层 ⇒ 对本候选**保守**。
        fill_m = np.where(fired_m, np.where(invf > 0, 1.0 / np.maximum(invf, 1e-12), 1.0), 1.0)
        dmap = {d: i for i, d in enumerate(sorted(set(panel.day)))}
        dcode = np.array([dmap[d] for d in panel.day])
        strat = np.full(W, -1)
        strat[r] = (dcode[r] * 1000 + np.round(panel.p[r, eb.idx[r]] * 100).astype(int) * 2
                    + panel.hot_yes[r, eb.idx[r]].astype(int))
        uniq_s = np.unique(strat)
        print(f"      ② 层 = 成交价 2 位小数 × 侧别 × 日：{len(uniq_s)} 层、"
              f"单层最大 {max(int((strat == s).sum()) for s in uniq_s)} 窗")
        rng = np.random.default_rng(20261011)
        mx1 = np.empty(args.nperm); mxw1 = np.empty(args.nperm)
        mx2 = np.empty(args.nperm); mxw2 = np.empty(args.nperm)
        WATCH = ("walk >= 43.00", "walk >= 80.00", "p >= 0.99", "dev >= 63.00",
                 "walk_min=80", "walk80+dev70", "walk80+floor0.97", "dev_min=75",
                 "floor_min=0.97")
        sel = [i for i, c in enumerate(conds) if c[0] in WATCH]
        dw1 = np.empty((args.nperm, len(sel))); dw2 = np.empty((args.nperm, len(sel)))
        for p_i in range(args.nperm):
            u = rng.random(W)[None, :]
            WIN1 = fired_m & (u < fill_m)
            p1 = (WIN1 * invf).sum(axis=1) * STAKE - nobs * STAKE
            d1 = p1[:NC] - p1[NC]
            mx1[p_i] = float(d1.max()); mxw1[p_i] = float(np.where(is_walk, d1, -1e9).max())
            dw1[p_i] = d1[sel]

            po = panel.outcome.copy()
            for s in uniq_s:
                m = strat == s
                if m.sum() > 1:
                    po[m] = rng.permutation(po[m])
            A = (po == 0)[None, :]; Bb = (po == 1)[None, :]
            WIN2 = fired_m & ((A & hot_m) | (Bb & ~hot_m))
            p2 = (WIN2 * invf).sum(axis=1) * STAKE - nobs * STAKE
            d2 = p2[:NC] - p2[NC]
            mx2[p_i] = float(d2.max()); mxw2[p_i] = float(np.where(is_walk, d2, -1e9).max())
            dw2[p_i] = d2[sel]
            if (p_i + 1) % 200 == 0:
                print(f"        … {p_i+1}/{args.nperm}", flush=True)
        print(f"\n      逐条件零分布（观测 Δ | ① 中位[下2.5,上97.5] | ② 中位[下2.5,上97.5]）：")
        for j, i in enumerate(sel):
            print(f"        {conds[i][0]:<16} 观测 {obs_delta[i]:+7.2f}U | "
                  f"① {np.median(dw1[:,j]):+7.2f} [{np.percentile(dw1[:,j],2.5):+7.2f},"
                  f"{np.percentile(dw1[:,j],97.5):+7.2f}] | "
                  f"② {np.median(dw2[:,j]):+7.2f} [{np.percentile(dw2[:,j],2.5):+7.2f},"
                  f"{np.percentile(dw2[:,j],97.5):+7.2f}]")
        print(f"      ① 有效价格零：family p = {float(np.mean(mx1 >= obs_delta[best_k])):.4f}"
              f"（max_Δ 中位 {np.median(mx1):+.2f}U / p95 {np.percentile(mx1,95):+.2f}U）"
              f"；walk 子族 p = {float(np.mean(mxw1 >= obs_delta[kw_])):.4f}"
              f"（{int(is_walk.sum())} 格，中位 {np.median(mxw1):+.2f}U）")
        print(f"      ② 结局置换零：family p = {float(np.mean(mx2 >= obs_delta[best_k])):.4f}"
              f"（max_Δ 中位 {np.median(mx2):+.2f}U / p95 {np.percentile(mx2,95):+.2f}U）"
              f"；walk 子族 p = {float(np.mean(mxw2 >= obs_delta[kw_])):.4f}"
              f"（{int(is_walk.sum())} 格，中位 {np.median(mxw2):+.2f}U / "
              f"p95 {np.percentile(mxw2,95):+.2f}U）")
        print("      读法：① 说「零边际市场里靠运气能刷到多高」；② 说「保住价/日校准、只打断")
        print("            特征↔胜负配对能刷到多高」。两个 p 都看，结论以更差的那个为准。")

    # ❌ 【2a】默认不跑：与 ② 同类的「层内置换特征/结局」零在实践中**有系统偏移**
    #    （改道窗按更贵的价成交、胜率却仍按原层抽 ⇒ 改道税被当成证据；实测这个零把
    #     `dev>=63`（观测 −12.03U）也判成「显著」）。要复现请显式 `--sec 2a`。
    if args.sec == "2a":
        print("\n" + "=" * 108)
        print(f"【2a｜已作废】walk 专属置换：层内打乱 walk 在 T150 评估集上的取值，每次重扫整格取最大 Δ")
        print(f"      层 = 成交价(2位小数) × sd 五分位 × 侧别（保住「价格/波动/方向」，只破坏 walk 的配对）")
        # 评估集各行（有 eval 的窗）的 分层键
        rows = np.nonzero(ev)[0]
        fill_ev = panel.p[rows, i150[rows]]
        sd_bin = np.digitize(panel.sd[rows], np.percentile(panel.sd[rows], [20, 40, 60, 80]))
        side = panel.hot_yes[rows, i150[rows]].astype(int)
        day_id = np.array([days.index(d) for d in panel.day])
        base_strata = (np.round(fill_ev * 100).astype(int) * 1000 + sd_bin * 10 + side)
        XG = [43.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0, 110.0, 120.0, 130.0]
        obs_max = max(deltas[X] for X in XG if X in deltas)
        print(f"      观测：max_Δ over {len(XG)} 档 = {obs_max:+.2f}U")
        for tag, day_scale in (("不跨日（层含日）", 100000), ("可跨日（层不含日）", 0)):
            strata = base_strata + day_scale * day_id[rows] if day_scale else base_strata
            uniq, inv = np.unique(strata, return_inverse=True)
            sizes = np.bincount(inv)
            print(f"      ── 变体「{tag}」：层数 {len(uniq)}，单元素层 {int((sizes == 1).sum())}"
                  f"（占 {100 * sizes[sizes == 1].sum() / len(rows):.1f}% 的窗）")
            rng = np.random.default_rng(20261011)
            ge = np.zeros(args.nperm)
            for p_i in range(args.nperm):
                key = inv * 1000.0 + rng.random(len(rows))
                order = np.argsort(key, kind="stable")
                tmp = walk_ev.copy()
                tmp[rows] = walk_ev[rows][order]
                mx = -1e9
                for X in XG:
                    e = chain(prep_off, ok_base_150 & (tmp >= X))
                    mx = max(mx, float(e.pnl.sum()) - B)
                ge[p_i] = mx
                if (p_i + 1) % 200 == 0:
                    print(f"        … {p_i+1}/{args.nperm}  p̂ = {np.mean(ge[:p_i+1] >= obs_max):.4f}",
                          flush=True)
            print(f"        ⇒ 校正 p = {float(np.mean(ge >= obs_max)):.4f}   "
                  f"（置换 max_Δ 中位 {np.median(ge):+.2f}U，p95 {np.percentile(ge,95):+.2f}U，"
                  f"最大 {ge.max():+.2f}U）")

    # ------------------------------------------------------------ 【3】价格对照
    if args.sec in ("all", "3"):
        print("\n" + "=" * 108)
        print("【3】价格对照 —— 「walk 的门槛」是不是「贵价角落地的伪装」？")
        m = ev & ~skip
        wv = panel.walk[wrow, np.maximum(i150, 0)]; pv = p150
        print("  (a) T150 评估集：walk 三档 × 价格三档 的「基线成交之后」胜负（只含基线真成交的窗）")
        pb = [(0.80, 0.95), (0.95, 0.99), (0.99, 1.01)]
        hdr = "      " + "".join(f"{f'p∈[{lo:.2f},{hi:.2f})':>18}" for lo, hi in pb)
        print(hdr)
        for wlo, whi in [(0, 60), (60, 80), (80, 1e9)]:
            cells = []
            for lo, hi in pb:
                mm = f150 & (wv >= wlo) & (wv < whi) & (pv >= lo) & (pv < hi)
                nn = int(mm.sum()); ll = int((~won150 & mm).sum())
                pnl = float(base.pnl[mm].sum())
                cells.append(f"n={nn:<4d}输{ll:<3d}{pnl:+7.2f}U")
            print(f"      walk∈[{wlo:.0f},{whi if whi < 1e8 else 999:.0f})" + "".join(f"{c:>18}" for c in cells))
        print("  (b) 价格腿单调抬（只拦 T150，两个方向；对照 walk≥80 的 Δn = "
              f"{int(chain(prep_off, ok_base_150 & (walk_ev >= 80)).fired.sum()) - N}）")
        print(f"      {'条件':<22}{'Δn':>6}{'ΔP&L':>10}{'95%下界':>10}{'95%上界':>10}{'h1':>9}{'h2':>9}")
        for op, X in ([(">", x) for x in (0.90, 0.93, 0.95, 0.96, 0.97, 0.98)]
                      + [("<=", x) for x in (0.85, 0.90, 0.95, 0.97)]):
            extra = (p150 > X) if op == ">" else (p150 <= X)
            ok = ok_base_150 & extra
            ok[~ev] = False
            e = chain(prep_off, ok)
            d = float(e.pnl.sum()) - B
            _, lo, hi = boot(e, base, panel, n=2000)
            a = float(e.pnl[dh1].sum() - base.pnl[dh1].sum())
            b = float(e.pnl[dh2].sum() - base.pnl[dh2].sum())
            print(f"      p {op} {X:<18.2f}{int(e.fired.sum())-N:>+6}{d:>+10.2f}{lo:>+10.2f}{hi:>+10.2f}"
                  f"{a:>+9.2f}{b:>+9.2f}")
        print("  (c) walk≥80 的增益按价格档拆分（基线 T150 成交集内）")
        for lo, hi in [(0.80, 0.90), (0.90, 0.95), (0.95, 0.99), (0.99, 1.01)]:
            mm = f150 & (pv >= lo) & (pv < hi)
            keep = mm & (wv >= 80)
            print(f"      p∈[{lo:.2f},{hi:.2f})  基线 n={int(mm.sum()):4d} 毛 {base.pnl[mm].sum():+7.2f}U"
                  f" → walk≥80 n={int(keep.sum()):4d} 毛 {base.pnl[keep].sum():+7.2f}U"
                  f"   （差 {base.pnl[mm].sum()-base.pnl[keep].sum():+.2f}U 的 T150 直接腾挪）")

    # --------------------------------------------------------------- 【4】机制账
    if args.sec in ("all", "4"):
        print("\n" + "=" * 108)
        print("【4a】参照线：T150 段的可选项对照（都相对现行 43 基线）")
        refs = [
            ("T150 整段关闭", np.zeros(W, dtype=bool)),
            ("T150 只留 walk≥80", ok_base_150 & (walk_ev >= 80.0)),
            ("T150 只留 p≥0.99", ok_base_150 & (p150 >= 0.99)),
            ("T150 只留 p≤0.95", ok_base_150 & (p150 <= 0.95)),
            ("T150 只留 p∈[0.95,0.99)", ok_base_150 & (p150 >= 0.95) & (p150 < 0.99)),
        ]
        print(f"  {'选项':<26}{'n':>6}{'Δn':>6}{'毛P&L':>10}{'ΔP&L':>9}{'95%下界':>10}{'95%上界':>10}"
              f"{'h1':>9}{'h2':>9}   T150剩")
        for tag, ok in refs:
            if tag.startswith("T150 整段"):
                ok = np.zeros(W, dtype=bool)
            ok = ok.copy()
            ok[~ev] = False
            e = chain(prep_off, ok)
            d = float(e.pnl.sum()) - B
            _, lo, hi = boot(e, base, panel, n=3000)
            a = float(e.pnl[dh1].sum() - base.pnl[dh1].sum())
            b = float(e.pnl[dh2].sum() - base.pnl[dh2].sum())
            print(f"  {tag:<26}{int(e.fired.sum()):>6}{int(e.fired.sum())-N:>+6}{e.pnl.sum():>+10.2f}"
                  f"{d:>+9.2f}{lo:>+10.2f}{hi:>+10.2f}{a:>+9.2f}{b:>+9.2f}"
                  f"{int(((e.stage==1)&e.fired).sum()):>6}")

        print("\n【4b】机制账：walk_min 43 → 80 的 +Δ 从哪来（对齐 doc 29 §机制 的记账法）")
        e80 = run_walk(80.0)
        blocked = base.fired & (base.stage == 1) & (walk_ev < 80.0)     # 基线成交、被新阈值拦
        nb = int(blocked.sum())
        lost_b = int((~won150 & blocked).sum())
        pnl_b = float(base.pnl[blocked].sum())
        rerouted = blocked & e80.fired
        died = blocked & ~e80.fired
        print(f"  被拦（基线在 T150 成交、eval-walk<80）：n={nb}"
              f"  其中输 {lost_b}（输率 {100*lost_b/max(nb,1):.1f}%）  基线 P&L {pnl_b:+.2f}U")
        keep_m = f150 & (walk_ev >= 80)
        print(f"     保留组对照：n={int(keep_m.sum())} 输率 "
              f"{100*int((~won150 & keep_m).sum())/max(int(keep_m.sum()),1):.1f}%"
              f"  基线 P&L {float(base.pnl[keep_m].sum()):+.2f}U")
        rrow = np.nonzero(rerouted)[0]
        print(f"     被拦批的入场价：基线（T150）中位 {np.median(p150[blocked]):.3f} vs "
              f"保留组中位 {np.median(p150[keep_m]):.3f}；被拦批改道后入场价中位 "
              f"{np.median(panel.p[rrow, e80.idx[rrow]]):.3f}（改道 {int(rerouted.sum())} 窗）")
        print(f"     整窗死亡 {int(died.sum())} 窗（基线里这些 {pnl_b - float(base.pnl[rerouted].sum()):+.2f}U）")
        r_s = e80.stage[rerouted]
        print(f"     改道 {int(rerouted.sum())} 窗 → t60 {int((r_s==2).sum())} / listen {int((r_s==3).sum())}；"
              f"改道后 P&L {float(e80.pnl[rerouted].sum()):+.2f}U（基线同批 {float(base.pnl[rerouted].sum()):+.2f}U，"
              f"改道税 {float(e80.pnl[rerouted].sum()) - float(base.pnl[rerouted].sum()):+.2f}U）")
        print(f"     总账：省下 {-pnl_b:+.2f}（被拦批不再亏/赚） + 改道后所得 {float(e80.pnl[rerouted].sum()):+.2f}"
              f" = Δ {float(e80.pnl.sum()) - B:+.2f}U ✅")
        print("  walk_min=80 之后三段分布：" + C.stage_table(panel, e80).replace("\n", "\n   "))

    # ------------------------------------------------------------ 【5】交叉条件
    if args.sec in ("all", "5"):
        print("\n" + "=" * 108)
        print("【5】交叉条件：T150 的入场判据同时用 walk 与价格 —— AND（都要）与 OR（任一）")
        print("     动机 = §3(a) 的二维表：输单集中在 walk∈[60,80) ∧ p<0.95 这一格")

        def sim(extra, tag, n_boot=2000):
            ok = ok_base_150 & extra
            ok[~ev] = False
            e = chain(prep_off, ok)
            d = float(e.pnl.sum()) - B
            _, lo, hi = boot(e, base, panel, n=n_boot)
            a = float(e.pnl[dh1].sum() - base.pnl[dh1].sum())
            b = float(e.pnl[dh2].sum() - base.pnl[dh2].sum())
            print(f"  {tag:<34}{int(e.fired.sum()):>6}{int(e.fired.sum())-N:>+6}{e.pnl.sum():>+10.2f}"
                  f"{d:>+9.2f}{lo:>+10.2f}{hi:>+10.2f}{a:>+9.2f}{b:>+9.2f}")
            return e, d

        print(f"  {'条件（T150 之外三段不变）':<34}{'n':>6}{'Δn':>6}{'毛P&L':>10}{'ΔP&L':>9}"
              f"{'95%下界':>10}{'95%上界':>10}{'h1':>9}{'h2':>9}")
        wv, pv_ = walk_ev, p150
        A = (43.0, 60.0, 70.0, 80.0)
        Bb = (0.95, 0.97, 0.99, 1.01)
        print("  ── OR（walk 够 或 价格够）")
        for a in A:
            for b in Bb:
                sim((wv >= a) | (pv_ >= b), f"walk≥{a:.0f} ∨ p≥{b:.2f}")
        print("  ── AND（walk 够 且 价格够）")
        for a in A:
            for b in Bb:
                sim((wv >= a) & (pv_ >= b), f"walk≥{a:.0f} ∧ p≥{b:.2f}")
        print("  ── 只价格")
        for b in Bb:
            sim(pv_ >= b, f"p≥{b:.2f}（无 walk）")
        print("  ── 只 walk")
        for a in A:
            sim(wv >= a, f"walk≥{a:.0f}（无价格）")

        print("\n  ── 最优格的细账（并列对照）")
        for tag, extra in (("walk≥80", wv >= 80.0), ("walk≥80 ∨ p≥0.95", (wv >= 80.0) | (pv_ >= 0.95)),
                           ("walk≥80 ∨ p≥0.97", (wv >= 80.0) | (pv_ >= 0.97)),
                           ("walk≥70 ∨ p≥0.97", (wv >= 70.0) | (pv_ >= 0.97))):
            ok = ok_base_150 & extra
            ok[~ev] = False
            e = chain(prep_off, ok)
            f1 = e.fired & (e.stage == 1)
            ix1 = e.idx[f1]; r1 = np.nonzero(f1)[0]
            w1 = (panel.hot_yes[r1, ix1] & (panel.outcome[r1] == 0)) | \
                 (~panel.hot_yes[r1, ix1] & (panel.outcome[r1] == 1))
            print(f"     {tag:<20} T150剩 {int(f1.sum()):4d} 输 {int((~w1).sum()):2d}"
                  f"  所有段 n={int(e.fired.sum())} 毛 {e.pnl.sum():+.2f}U"
                  f" | 段分布 [{int(((e.stage==1)&e.fired).sum())},"
                  f"{int(((e.stage==2)&e.fired).sum())},{int(((e.stage==3)&e.fired).sum())}]")

        print("\n  ── 参数级组合：walk_min=80 再叠第二个旋钮（是否比单腿更好）")
        print(f"    {'组合':<30}{'n':>6}{'Δn':>6}{'毛P&L':>10}{'ΔP&L':>9}{'95%下界':>10}{'95%上界':>10}")
        for tag, r2 in (("walk80 单腿", C.Rules(walk_min=80)),
                        ("walk80 + dev75", C.Rules(walk_min=80, dev_min=75)),
                        ("walk80 + dev70", C.Rules(walk_min=80, dev_min=70)),
                        ("walk80 + floor0.95", C.Rules(walk_min=80, floor_min=0.95)),
                        ("walk80 + floor0.97", C.Rules(walk_min=80, floor_min=0.97)),
                        ("walk80 + price0.85", C.Rules(walk_min=80, price_min=0.85)),
                        ("walk80 + sd_min50", C.Rules(walk_min=80, sd_min=50))):
            p2 = C.prepare(panel, r2, skip)
            e2 = C.run(p2)
            d2 = float(e2.pnl.sum()) - B
            _, lo2, hi2 = boot(e2, base, panel, n=2000)
            print(f"    {tag:<30}{int(e2.fired.sum()):>6}{int(e2.fired.sum())-N:>+6}"
                  f"{e2.pnl.sum():>+10.2f}{d2:>+9.2f}{lo2:>+10.2f}{hi2:>+10.2f}")

    # ------------------------------------------------------------- 【6】σ 归一
    if args.sec in ("all", "6"):
        print("\n" + "=" * 108)
        print("【6】σ 归一：walk ≥ k·sd 是否与 walk ≥ 80 美元等效（80/σ 中位是多少）")
        sdm = float(np.median(panel.sd[ev]))
        print(f"      T150 评估集 sd 中位 {sdm:.2f} 美元 ⇒ 80 美元 = {80/sdm:.2f}σ；"
              f"43 美元 = {43/sdm:.2f}σ")
        print(f"  {'条件':<26}{'n':>6}{'Δn':>6}{'毛P&L':>10}{'ΔP&L':>9}{'95%下界':>10}{'95%上界':>10}")
        for k in (0.6, 0.8, 1.0, 1.2, 1.3, 1.4):
            ok = ok_base_150 & (walk_ev >= k * panel.sd)
            ok[~ev] = False
            e = chain(prep_off, ok)
            d = float(e.pnl.sum()) - B
            _, lo, hi = boot(e, base, panel, n=2000)
            print(f"      walk ≥ {k:.1f}·sd{'':<13}{int(e.fired.sum()):>6}{int(e.fired.sum())-N:>+6}"
                  f"{e.pnl.sum():>+10.2f}{d:>+9.2f}{lo:>+10.2f}{hi:>+10.2f}")

    # --------------------------------------------------- 【7】43→80 增量的逐日 / LOO 归因
    if args.sec in ("all", "7"):
        print("\n" + "=" * 108)
        print("【7】43 → 80 这个**增量**由哪些天撑起（逐日 + 留一日）")
        e80 = chain(prep_off, ok_base_150 & (walk_ev >= 80))
        days = sorted(set(panel.day))
        dmask = {d: np.array([x == d for x in panel.day]) for d in days}
        dsum = np.array([float((e80.pnl - base.pnl)[dmask[d]].sum()) for d in days])
        tot = float(dsum.sum())
        order = np.argsort(-np.abs(dsum))
        print(f"      逐日 Δ（80 减 43）：合计 {tot:+.2f}U；绝对值前 5：")
        for i in order[:5]:
            print(f"        {days[i]}  {dsum[i]:+7.2f}U  （占 |合计| 的 {abs(dsum[i])/max(abs(tot),1e-9)*100:.0f}%）")
        pos = (dsum > 0).sum(); neg = (dsum < 0).sum()
        print(f"      正日 {pos} / 负日 {neg}；剔除最大单日 {days[order[0]]} 后 Δ = {tot-dsum[order[0]]:+.2f}U")
        loo = np.array([tot - dsum[i] for i in range(len(days))])
        print(f"      LOO 增量区间 [{loo.min():+.2f}, {loo.max():+.2f}]U，"
              f"符号不变 {int((np.sign(loo)==np.sign(tot)).sum())}/{len(days)} 天")
        # 被闸批的输单落在哪几天
        blk = base.fired & ~e80.fired
        rb = np.nonzero(blk)[0]
        wb = np.where(panel.hot_yes[rb, base.idx[rb]], panel.outcome[rb] == 0,
                      panel.outcome[rb] == 1)
        dl = {}
        for r_i, w_i in zip(rb, wb):
            dl.setdefault(panel.day[r_i], [0, 0])
            dl[panel.day[r_i]][0] += 1
            dl[panel.day[r_i]][1] += int(not w_i)
        print("      被拦批的输单逐日（只列有输单的天）：")
        for d in sorted(dl):
            n_d, l_d = dl[d]
            if l_d:
                print(f"        {d}  被拦 {n_d:3d} 笔 / 输 {l_d:2d} 笔"
                      f"  （这批窗在基线里的盈亏 {(base.pnl - e80.pnl)[dmask[d]].sum():+.2f}U）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
