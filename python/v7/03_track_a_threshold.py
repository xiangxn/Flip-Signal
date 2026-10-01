#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v7-03 Track A｜阈值动态化：把四个常数阈值换成**状态的函数**，链结构一字不动。

判据见 `docs/gbm_2026-10-01.md` §0.2（预注册）:
  ① OOF 日级配对 Δ 95% 下界 > 0; ② 净 Δ ≥ +10U/14 天 @2U;
  ③ 搜索校正置换 p < 0.05; ④ 分半（08-25）两半同向。**四条全过才立项**。

做法:
  * 单轴网格（不允许事后拼组合）: walk χ∈{0.5..1.0} / dev ψ∈{0.8..2.0} /
    σ ς∈{0.5..1.25} / 地板 3 个单调阶梯;
  * **折内拟合**: 按日 5 折, 每折在训练日上挑 P&L 最大的那一档, 应用到该折的测试日
    ⇒ 折外行拼成 OOF 宇宙; 再与基线做**日级配对 Δ**;
  * 搜索校正置换: 把日标签整块打乱（日内顺序保持）, 重跑**整个选择流程** —— 这样
    「扫了 6 档挑最好」的代价被算进零分布。

用法: python/venv/bin/python python/v7/03_track_a_threshold.py
"""
import json
import random
import sys
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import lib_sim as SIM             # noqa: E402
import lib_stats as S             # noqa: E402
import lib_universe as U          # noqa: E402

CUT = "2026-08-25"                # 分半切点（与 38/41/42 的历史切点一致）


# ── 四个轴（预注册网格） ──────────────────────────────────────────────────

def axis_generators():
    """{轴名: (档位标签 list, 构造 Rules 的函数 list)}。

    ⚠️ 每个 `*_pred(x)` 返回的是**判定函数** `f(r) -> bool`, 不是构造器;
    `mk_rules(**kw)` 才是构造器 —— 两者混用会让 `walk_fn` 拿到一个恒真的对象
    （曾经踩过: 六档 χ 全落回 n=2128 的无闸值, 而单看代码完全看不出来）。
    """
    ax = {}

    def mk_rules(**kw):
        return lambda: U.Rules(**kw)

    def walk_pred(chi):
        def f(r):
            w = r["walk"]
            if w is None or w != w:
                return True                     # fail-open（与引擎同口径）
            return w >= chi * r["sd"]
        return f

    chi_grid = (0.5, 0.6, 0.7, 0.8, 0.9, 1.0)
    ax["walk≥χσ"] = ([f"χ={c}" for c in chi_grid],
                     [mk_rules(walk_fn=walk_pred(c)) for c in chi_grid])

    def dev_pred(psi):
        def f(r):
            return r["dev"] >= psi * r["sd"]
        return f

    psi_grid = (0.8, 1.0, 1.2, 1.5, 2.0)
    ax["dev≥ψσ"] = ([f"ψ={p}" for p in psi_grid],
                    [mk_rules(dev_fn=dev_pred(p)) for p in psi_grid])

    def sd_pred(z):
        def f(r):
            b = r.get("sd_base")
            if not b:
                return r["sd"] is not None and r["sd"] >= U.SD_MIN_USD    # 无基线 ⇒ 回退常数
            return r["sd"] >= z * b
        return f

    z_grid = (0.5, 0.75, 1.0, 1.25)
    ax["σ≥ς·σprev"] = ([f"ς={z}" for z in z_grid],
                       [mk_rules(sd_fn=sd_pred(z)) for z in z_grid])

    # 地板三档: 等值 / σ 越大越严 / σ 越小越严（阶梯在折内按训练日 σ 三分位定）
    ax["地板阶梯"] = (["全 0.83", "σ 大⇒严", "σ 小⇒严"],
                     [("flat",), ("up",), ("down",)])
    return ax


def ladder_floor_fn(kind, t1, t2):
    """按 σ 三分位决定地板价（t1/t2 = 下/上三分位切点）。"""
    def f(r):
        if kind == "flat":
            return r["fill"] > 0.83
        s = r["sd"]
        if kind == "up":        # σ 越大越严
            return r["fill"] > (0.80 if s < t1 else (0.83 if s < t2 else 0.86))
        return r["fill"] > (0.86 if s < t1 else (0.83 if s < t2 else 0.80))
    return f


# ── 运行 ──────────────────────────────────────────────────────────────────

def main():
    data = SIM.Data()
    rules_of = axis_generators()
    base_rules = U.Rules()
    base_rows = SIM.simulate(data, SIM.chain_decide(base_rules))
    base_day = S.day_pnl(base_rows)
    b = SIM.summarize(base_rows)
    print("=" * 96)
    print("v7-03 Track A｜阈值动态化（基线 = 现行规则链）")
    print("=" * 96)
    print(f"  基线: n={b['n']} WR {b['wr']:.4f}% P&L {b['pnl']:+.4f}U")
    print(f"  §0 自检 {'✅' if abs(b['pnl']-U.PIN['pnl'])<1e-6 and b['n']==U.PIN['signals'] else '❌'}"
          f"  （oracle {U.PIN['signals']} / {U.PIN['pnl']:+.6f}U）")

    # 每档的 σ 三分位切点（全样本; 仅用于地板阶梯这种**需要全局刻度**的形式）
    sds = np.array([w["sd"] for w in data.win])
    t1, t2 = np.quantile(sds, [1/3, 2/3])

    days = sorted(set(w["date"] for w in data.win))

    def run(rules):
        return SIM.simulate(data, SIM.chain_decide(rules))

    def fold_of_set(day_set):
        """按日 5 折（GroupKFold, 只用出现过信号的日）。"""
        k = 5
        return {d: i % k for i, d in enumerate(sorted(day_set))}

    # ── §2 单轴扫描（全样本, **仅描述**） ──
    print("\n" + "=" * 96)
    print("§2 单轴全样本扫描（描述性; 判决看 §3 的折外 OOF）")
    print("=" * 96)
    table = {}
    for axis, (labels, makers) in rules_of.items():
        print(f"\n  [{axis}]")
        rows_by_v = []
        for lab, mk in zip(labels, makers):
            if isinstance(mk, tuple):       # 地板阶梯
                rl = U.Rules(floor_fn=ladder_floor_fn(mk[0], t1, t2))
            else:
                rl = mk()
            rs = run(rl)
            sm = SIM.summarize(rs)
            _, lo, hi = S.boot_delta(base_day, S.day_pnl(rs))
            d = sm["pnl"] - b["pnl"]
            print(f"    {lab:<10} n={sm['n']:<5} WR {sm['wr']:6.2f}%  P&L {sm['pnl']:+7.2f}U"
                  f"  Δ {d:+7.2f}U  95%[{lo:+7.2f},{hi:+7.2f}]")
            rows_by_v.append(rs)
        table[axis] = (labels, rows_by_v)

    # ── §3 折内拟合 → OOF → 配对 Δ ──
    print("\n" + "=" * 96)
    print("§3 折内拟合（按日 5 折, 训练日挑 P&L 最大档）⇒ 折外 OOF 宇宙 vs 基线")
    print("=" * 96)
    oof_report = {}
    oof_by_axis = {}
    for axis, (labels, rows_by_v) in table.items():
        day_of_v = [S.day_pnl(rs) for rs in rows_by_v]
        # 每档的逐窗记录（用于置换时快速重算 P&L）
        win_of_v = [{r["win_i"]: r for r in rs} for rs in rows_by_v]
        fold = fold_of_set(days)

        def oof_rows(shuffle=None):
            """shuffle = {win_i: won'} 覆盖标签时用于置换。返回 OOF 行 list。"""
            oof = []
            best = {}
            for kf in range(5):
                tr = [d for d in days if fold[d] != kf]
                te = [d for d in days if fold[d] == kf]
                # 训练折上挑 P&L 最大档（并列取最接近现行常数的一档 = 索引最小者用
                # 「与基线同样本 P&L 差」排序, 全并列时取第一个）
                best_kf = max(range(len(labels)),
                              key=lambda vi: (sum(day_of_v[vi].get(d, 0.0) for d in tr), -vi))
                best[kf] = labels[best_kf]
                for wi, r in win_of_v[best_kf].items():
                    d = data.win[r["win_i"]]["date"]
                    if d not in te:
                        continue
                    won = r["won"] if shuffle is None else shuffle[r["win_i"]]
                    fill = r["fill"]
                    oof.append({"date": d, "win_i": r["win_i"], "won": won, "fill": fill,
                                "side": r["side"],
                                "pnl": (U.STAKE / fill - U.STAKE) if won else -U.STAKE})
            return oof, best

        oof, best = oof_rows()
        oof_by_axis[axis] = oof
        od, lo, hi = S.boot_delta(base_day, S.day_pnl(oof))
        n = len(oof)
        wr = sum(r["won"] for r in oof) / n * 100 if n else 0.0
        pnl = sum(r["pnl"] for r in oof)
        half1 = [r for r in oof if r["date"] < CUT]
        half2 = [r for r in oof if r["date"] >= CUT]
        d1 = sum(r["pnl"] for r in half1) - sum(base_day.get(d, 0) for d in
                                               set(r["date"] for r in half1))
        d2 = sum(r["pnl"] for r in half2) - sum(base_day.get(d, 0) for d in
                                                set(r["date"] for r in half2))
        print(f"\n  [{axis}]  折内最优档: " + ", ".join(f"折{k}→{v}" for k, v in sorted(best.items())))
        print(f"    OOF: n={n} WR {wr:.2f}% P&L {pnl:+.2f}U  Δ={od:+.2f}U "
              f"95%[{lo:+.2f},{hi:+.2f}]  分半 Δ h1 {d1:+.2f} / h2 {d2:+.2f}")
        oof_report[axis] = {"n": n, "wr": wr, "pnl": pnl, "delta": od, "lo": lo, "hi": hi,
                            "h1": d1, "h2": d2, "best": best}

    # ── §4 搜索校正置换（整块打乱日标签, 重跑整个选择流程） ──
    print("\n" + "=" * 96)
    print("§4 搜索校正置换（按日整块打乱标签, 重跑「折内挑档 + 折外评估」全流程）")
    print("=" * 96)
    nperm = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    rng = random.Random(S.SEED)
    # 逐日窗口列表（置换时按「日 → 该日窗口的标签列表」整块搬运, 日内顺序保持）
    out_by_day, win_order_by_day = {}, {}
    for i, w in enumerate(data.win):
        if w["outcome"] is None:
            continue
        out_by_day.setdefault(w["date"], []).append(w["outcome"])
        win_order_by_day.setdefault(w["date"], []).append(i)
    days_have = list(win_order_by_day)
    fold = fold_of_set(days)
    tr_of, te_of = {}, {}
    for kf in range(5):
        tr_of[kf] = [d for d in days if fold[d] != kf]
        te_of[kf] = set(d for d in days if fold[d] == kf)

    def day_pnl_under(rows, wout):
        d = {}
        for r in rows:
            o = wout[r["win_i"]]
            won = 1 if ((o == 0) if r["side"] == "yes" else (o == 1)) else 0
            dd = data.win[r["win_i"]]["date"]
            d[dd] = d.get(dd, 0.0) + ((U.STAKE / r["fill"] - U.STAKE) if won else -U.STAKE)
        return d

    def score_rows(rows, wout, ref_up):
        """**相对参考胜率**的得分**均值**（每笔）: 赢 +2·wref/f, 输 −2·(1−wref)/f, wref = 当日参考胜率。

        ⚠️ 为什么不用总 P&L: 标签打乱后 E[每笔 P&L | 成交价 f] = 2p/f − 2 随 f 单调
        （f=0.20 ⇒ +3U, f=0.94 ⇒ −0.94U）⇒ 「两臂总 P&L 之差」此时量的是**两臂的成交价
        组成差异**而非判别力（实测零分布中位被推到 +477U）。
        ⚠️ 为什么除以笔数: 本式打乱后的期望 ≈ 2(2·wref−1)/f ≈ 0, 但只要按**总**分比,
        「多下 200 笔」就白拿 200×2.3 分（实测 obs +201 而零分布 ≤ +14, 全被笔数主导）。
        取**每笔均值**后两臂同尺度、零分布中心在 0。**判决 p 出自它; 效果量仍报钱的 Δ（§3）。**
        """
        s = 0.0
        for r in rows:
            p_up = ref_up[data.win[r["win_i"]]["date"]]
            yes = (r["side"] == "yes")
            wref = p_up if yes else (1.0 - p_up)
            o = wout[r["win_i"]]
            won = (o == 0) if yes else (o == 1)
            s += (2.0 * wref / r["fill"]) if won else (-2.0 * (1.0 - wref) / r["fill"])
        return (s / len(rows)) if rows else 0.0

    def money_rows(rows, wout):
        """**每笔平均 P&L**（钱, 除以笔数）。"""
        s = 0.0
        for r in rows:
            won = 1 if ((wout[r["win_i"]] == 0) if r["side"] == "yes"
                        else (wout[r["win_i"]] == 1)) else 0
            s += ((U.STAKE / r["fill"] - U.STAKE) if won else -U.STAKE)
        return (s / len(rows)) if rows else 0.0

    real_out = {i: w["outcome"] for i, w in enumerate(data.win) if w["outcome"] is not None}
    real_up = {d: sum(1 for wi in wl if real_out[wi] == 0) / len(wl)
               for d, wl in win_order_by_day.items()}

    for axis, (labels, rows_by_v) in table.items():
        win_of_v = [{r["win_i"]: r for r in rs} for rs in rows_by_v]
        arm_oof = oof_by_axis[axis]
        obs_sc = score_rows(arm_oof, real_out, real_up) - score_rows(base_rows, real_out, real_up)
        obs_mn = money_rows(arm_oof, real_out) - money_rows(base_rows, real_out)
        null_sc, null_mn = [], []
        for _ in range(nperm):
            src = {d: rng.choice(days_have) for d in days_have}
            wout, ref_up = {}, {}
            for d in days_have:
                outs = out_by_day[src[d]]
                for k, wi in enumerate(win_order_by_day[d]):
                    wout[wi] = outs[k % len(outs)]
                ref_up[d] = sum(1 for o in outs if o == 0) / len(outs)
            dpv = [day_pnl_under(rs, wout) for rs in rows_by_v]     # 选择流程照旧用钱
            sel = []
            for kf in range(5):
                best_vi = max(range(len(labels)), key=lambda vi: (
                    sum(dpv[vi].get(d, 0.0) for d in tr_of[kf]), -vi))
                for wi, r in win_of_v[best_vi].items():
                    if data.win[wi]["date"] in te_of[kf]:
                        sel.append(r)
            null_sc.append(score_rows(sel, wout, ref_up) - score_rows(base_rows, wout, ref_up))
            null_mn.append(money_rows(sel, wout) - money_rows(base_rows, wout))
        # 两条零中心统计量各出一个 p, **取保守者**（任一不过即不过）——避免挑统计量
        p_sc = S.perm_p(obs_sc, null_sc)
        p_mn = S.perm_p(obs_mn, null_mn)
        p = max(p_sc, p_mn)
        print(f"  [{axis}]  OOF Δ={oof_report[axis]['delta']:+.2f}U（钱, 总）;  置换统计量 obs/零分布p95:")
        print(f"      每笔得分 {obs_sc:+.4f} / {np.percentile(null_sc, 95):+.4f} ⇒ p={p_sc:.4f}"
              f"   |  每笔 P&L {obs_mn:+.4f}U / {np.percentile(null_mn, 95):+.4f}U ⇒ p={p_mn:.4f}"
              f"   ⇒ 取保守 p={p:.4f}")
        oof_report[axis]["p_boot"] = p
        oof_report[axis]["p_sc"], oof_report[axis]["p_mn"] = p_sc, p_mn
        oof_report[axis]["obs_sc"], oof_report[axis]["obs_mn"] = obs_sc, obs_mn
        oof_report[axis]["null_sc_med"] = float(np.median(null_sc))
        oof_report[axis]["null_mn_med"] = float(np.median(null_mn))

    # ── §5 汇总判决 ──
    print("\n" + "=" * 96)
    print("§5 判决（四条全过才立项）")
    print("=" * 96)
    for axis, r in oof_report.items():
        c1 = r["lo"] > 0
        c2 = r["delta"] >= 10.0
        c3 = r.get("p_boot", 1.0) < 0.05
        c4 = (r["h1"] > 0 and r["h2"] > 0) or (r["h1"] < 0 and r["h2"] < 0)
        print(f"  [{axis}] ①区间下界>0 {'✅' if c1 else '❌'}({r['lo']:+.2f})"
              f"  ②Δ≥+10U {'✅' if c2 else '❌'}({r['delta']:+.2f})"
              f"  ③搜校 p<0.05 {'✅' if c3 else '❌'}({r.get('p_boot', float('nan')):.4f})"
              f"  ④分半同向 {'✅' if c4 else '❌'}({r['h1']:+.2f}/{r['h2']:+.2f})"
              f"  ⇒ {'立项' if all([c1,c2,c3,c4]) else '否'}")

    (BASE / "data/track_a.json").write_text(
        json.dumps({"baseline": b, "oof": oof_report}, indent=1, ensure_ascii=False),
        encoding="utf-8")
    print("\n[03] 明细已存 python/v7/data/track_a.json")


if __name__ == "__main__":
    main()
