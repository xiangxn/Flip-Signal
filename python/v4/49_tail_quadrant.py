#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""49_tail_quadrant.py — 「以 anchor 为中心：三条线分治」逻辑 v2 回测 + 阈值扫描（2026-10-11）。

用户提案（原话要点）：
  ① 按 spot / anchor / twap 三条线的情况分别处理；
  ② 以 anchor 为中心：spot 与 twap **同侧**一套逻辑，**异侧**另一套；
  ③ 同侧 ⇒ 方向唯一；异侧 ⇒ 同时存在「现货腿」与「线腿」两个候选，
     现行代码只走现货腿（dev），**异侧里线腿那一支（DOWN / UP）被整个放弃**；
  ④ 对称性：`spot>twap>anchor` 与 `anchor>twap>spot` 同为同侧；
     `spot>anchor>twap` 与 `twap>anchor>spot` 同为异侧。

── 三线四象限（对**下注侧**的两条腿取号；dev = sgn·(spot−anchor)，walk = sgn·(twap−anchor)）

  排列（anchor 居中）       dev walk  市场热门侧   现行代码                       v2 新增
  Q1 spot>twap>anchor（上方同侧） +   +    YES       出 YES 信号（走 dev）          不变
  Q2 anchor>twap>spot（下方同侧） +   +    NO        出 NO 信号（走 dev）           不变
  Q3 spot>anchor>twap（异侧）   −   +    市场说 DOWN  现货腿 dev<0 ⇒ leg_out；      线腿：
     spot 在上、结算线在下              (hot=NO)    若 hot=YES 则 T150 恒被 #29    dev<0 ∧
                                                       walk 闸拦                     walk ≥ W
  Q4 twap>anchor>spot（异侧）   +   −    市场说 UP    镜像；hot=YES 时 dev<0         ⇒ 买该侧
     结算线在上、spot 在下              (hot=YES)   ⇒ leg_out

  判定腿（每段同形）：
    arm_spot = dev ≥ 63 ∨ (sd ≥ 40 ∧ dev ≥ sd)      ← 现行，原样
    arm_line = dev < 0 ∧ walk ≥ W                   ← 新增（W 扫描；可选附加 dev ≤ −G）
    其余腿一字不动：价格腿算子（#26，T150 严格 >）、三段链与检查点、一窗一单、
    #29 的 T150 walk 闸**只作用在 arm_spot**（arm_line 本身就是线条件）、
    地板（#32/#33）照旧拦 T60 / 监听段且被拦即链继续。
  结构性质（已断言）：v2 ⊇ 现行 ⇒ **不会有窗被 v2 丢单**，只有「保持 / 改写 / 新增」；
  且**改写/新增必然由 arm_line 触发**（arm_spot 能先触发的窗两边同点）。

pin（§0）：W 关闭（None）时逐窗与 48 号脚本的 signal() 全等（idx），两样本全部窗。

用法: python/venv/bin/python python/v4/49_tail_quadrant.py
"""

import collections
import importlib.util
import statistics as st
from pathlib import Path

BASE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


m48 = load("m48", BASE / "48_tail_hold_path.py")
s38 = m48.s38
boot_delta = s38.boot_delta
STAKE, FEE_RATE = m48.STAKE, 0.07
T150, T60 = 150, 60
P_FLOOR = m48.P_FLOOR            # 0.80
FLOOR_X = m48.FLOOR_X            # 0.83（地板）
DEV_USD = m48.DEV_USD            # 63
SD_MIN = m48.SD_MIN_USD          # 40
WALK_X = m48.WALK_X              # 43（#29 闸，仅 arm_spot × T150）

BASE_CFG = dict(mode="v2", W=None, G=0.0, p150=P_FLOOR, floor=FLOOR_X,
                dev=DEV_USD, sd_min=SD_MIN, walk_x=WALK_X, sigma=True)


# ── 口径 ──────────────────────────────────────────────────────────────────

def rows_of(w):
    """本窗每 tick 的判定量（不含阈值）。tick 流 = 48.path_ticks（rem≤150 / 延迟闸 / 有效价门）。"""
    a, sd = w["anchor"], w["sd"]
    out = []
    for i, t in enumerate(w["ticks"]):
        sgn = 1.0 if t["side"] == "yes" else -1.0
        tw = t.get("twap")
        out.append({"i": i, "rem": t["rem"], "side": t["side"], "fill": t["fill"], "sd": sd,
                    "dev": sgn * (t["spot"] - a),
                    "walk": (sgn * (tw - a)) if tw else None})
    return out


def arm(r, cfg, stage):
    """三段共用的判定：返回 'sig' / 'floor'（过腿被地板拦 ⇒ 链继续）/ None。

    ⚠️ 段间判定腿不同（现行口径）：T=150 与 T=60 段是 ⑤（有 σ 路线、T150 另有 #29 闸），
    监听段是 ②（**只有 dev ≥ 63，无 σ 路线**）。arm_line 三段同形挂在其上。
    """
    if stage == "t150":
        if not (r["fill"] > cfg["p150"]):
            return None
    else:
        if not (r["fill"] >= P_FLOOR):
            return None
    dev, walk = r["dev"], r["walk"]
    ok_spot = dev >= cfg["dev"]
    if stage != "listen":                      # ② 没有 σ 路线
        if (not ok_spot and cfg["sigma"] and r["sd"] is not None
                and r["sd"] >= cfg["sd_min"] and dev >= r["sd"]):
            ok_spot = True
        if stage == "t150" and ok_spot and not (walk is None or walk >= cfg["walk_x"]):
            ok_spot = False
    if cfg["mode"] == "v2":
        ok_line = (cfg["W"] is not None and dev < 0 and walk is not None
                   and walk >= cfg["W"] and dev <= -cfg["G"])
    else:  # mode="both_neg"：参考格 —— 下注侧与**两条腿**都反（市场与两线读反时的热门侧）
        ok_line = dev < 0 and walk is not None and walk < 0
    if not (ok_spot or ok_line):
        return None
    if stage != "t150" and not (r["fill"] > cfg["floor"]):
        return "floor"
    return "sig"


def chain(rows, cfg):
    """三段链（与 48.signal 同构，只换判定腿）。返回成交 tick 下标或 None。"""
    if not rows:
        return None
    if rows[0]["rem"] > T60:
        if arm(rows[0], cfg, "t150") == "sig":
            return 0
        rest = [i for i, r in enumerate(rows) if r["rem"] <= T60]
    else:
        rest = list(range(len(rows)))
    if not rest:
        return None
    if arm(rows[rest[0]], cfg, "t60") == "sig":
        return rest[0]
    for i in rest[1:]:
        if arm(rows[i], cfg, "listen") == "sig":
            return i
    return None


def stage_of(rows, idx):
    j60 = next(i for i, r in enumerate(rows) if r["rem"] <= T60)
    if idx < j60:
        return "t150"
    return "t60" if idx == j60 else "listen"


def net_of(fill, won):
    """2U/注、按 fill 即时成交（taker）、扣官方费。"""
    sh = STAKE / fill
    gross = (sh - STAKE) if won else -STAKE
    return gross - round(sh * FEE_RATE * fill * (1 - fill), 5)


def prep(wins):
    """预计算每窗 rows + 现行链下标（§0 pin 一次，全部配置复用）。"""
    for w in wins:
        w["rows"] = rows_of(w)
        _, w["base_idx"] = m48.signal(w)
    return wins


def quad(r):
    """下注侧两条腿的符号命名（侧别无关）。"""
    d, wl = r["dev"], r["walk"]
    if wl is None:
        return "walk缺"
    if d >= 0 and wl >= 0:
        return "两腿同正"
    if d < 0 and wl < 0:
        return "两腿同负"
    if d >= 0 and wl < 0:
        return "现货正/线负"
    return "现货负/线正"


def one(w, idx):
    r = w["rows"][idx]
    won = m48.won_of(r["side"], w["outcome"])
    return r, won, net_of(r["fill"], won)


def run(wins, cfg, strict=True):
    """跑一个配置：base 对照 + 三类分解 + 日级配对 Δ CI。

    ⚠️ base = **同参数但线腿关闭**（bcfg = cfg 去掉 W / 模式归 v2）——Δ 只度量线腿（arm_line）
    自身的增量。默认参数下 bcfg == BASE_CFG == 现行链（§0 pin 已钉）。改其它参数的变体
    也因此在同口径下比较（否则「改了 dev 的 v2」会去对「dev=63 的现行」，两处变化混一起）。
    strict=True 断言 v2 ⊇ base（不许丢单）——只对真超集形态成立；§6 参考格传 False。
    """
    bcfg = dict(cfg, W=None, mode="v2")
    out = {"n_base": 0, "n_v2": 0, "base_net": 0.0, "v2_net": 0.0, "lost": 0, "kept": 0,
           "added": [], "sub": []}
    base_day, v2_day = collections.defaultdict(float), collections.defaultdict(float)
    add_day = collections.defaultdict(float)
    sub_base_day, sub_v2_day = collections.defaultdict(float), collections.defaultdict(float)
    for w in wins:
        rows = w["rows"]
        be = chain(rows, bcfg)
        ve = chain(rows, cfg)
        if be is None and ve is None:
            continue
        if be is not None:
            _, _, p = one(w, be)
            out["n_base"] += 1
            out["base_net"] += p
            base_day[w["date"]] += p
        if ve is not None:
            r, won, p = one(w, ve)
            out["n_v2"] += 1
            out["v2_net"] += p
            v2_day[w["date"]] += p
        if be is None:
            out["added"].append((w["date"], ve, stage_of(rows, ve), r["side"], r["fill"], won, p))
            add_day[w["date"]] += p
        elif ve is None:
            out["lost"] += 1
        elif ve == be:
            out["kept"] += 1
        else:
            if strict:
                assert ve < be, "v2 ⊇ 现行被打破"
            rv, wonv, pv = one(w, ve)
            rb, wonb, pb = one(w, be)
            out["sub"].append((w["date"], be, ve, stage_of(rows, ve), rv["side"], rv["fill"], wonv, pv,
                               rb["side"], rb["fill"], wonb, pb))
            sub_base_day[w["date"]] += pb
            sub_v2_day[w["date"]] += pv
    if strict:
        assert out["lost"] == 0, "v2 丢单（超集形态被打破）"
    out["delta"] = out["v2_net"] - out["base_net"]
    out["delta_ci"] = boot_delta(base_day, v2_day)
    out["add_net"] = sum(add_day.values())
    out["add_ci"] = boot_delta({}, add_day)
    if sub_base_day:
        out["sub_delta"] = sum(sub_v2_day.values()) - sum(sub_base_day.values())
        out["sub_ci"] = boot_delta(sub_base_day, sub_v2_day)
    else:
        out["sub_delta"], out["sub_ci"] = 0.0, (0.0, 0.0)
    out["add_day"] = dict(add_day)
    return out


def batch(batch_recs):
    """批次画像：(n, 赢, WR%, fill均, 净U, 侧别计数, 段计数)。"""
    if not batch_recs:
        return 0, 0, 0.0, 0.0, 0.0, {}, {}
    n = len(batch_recs)
    wl = sum(1 for t in batch_recs if t[5])
    fl = st.mean([t[4] for t in batch_recs])
    net = sum(t[6] for t in batch_recs)
    sd = collections.Counter(t[3] for t in batch_recs)
    sg = collections.Counter(t[2] for t in batch_recs)
    return n, wl, wl / n * 100, fl, net, dict(sd), dict(sg)


def sub_profile(sub):
    """改写批画像：同侧（同 outcome，比价差）vs 异侧（一边必输）。"""
    if not sub:
        return "改写 0"
    same = [t for t in sub if t[4] == t[8]]
    diff = [t for t in sub if t[4] != t[8]]

    def f(g):
        if not g:
            return "n=0"
        vw = sum(1 for t in g if t[6])
        bw = sum(1 for t in g if t[10])
        return f"n={len(g):>3} v2赢{vw:>3}/基赢{bw:>3} Δ{sum(t[7] - t[11] for t in g):+7.2f}U"

    return f"同侧 {f(same)} ‖ 异侧 {f(diff)}"


def fmt_ci(ci):
    return f"[{ci[0]:+8.2f},{ci[1]:+8.2f}]"


def line(n=104):
    print("─" * n)


def main():
    print("加载样本 …")
    wins_ev = prep(m48.build(m48.NEW))
    wins_bt = prep(m48.build(m48.OLD))

    # ── §0 pin：v2 关闭 == 现行链 ────────────────────────────────────────
    bad = sum(1 for w in wins_ev + wins_bt if chain(w["rows"], BASE_CFG) != w["base_idx"])
    print(f"§0 pin: events 窗 {len(wins_ev)} / btc 窗 {len(wins_bt)}；不对齐 {bad}（必须 0）")
    for nm, ws in (("events", wins_ev), ("btc", wins_bt)):
        nb = [w for w in ws if w["base_idx"] is not None]
        gross = sum(((STAKE / one(w, w["base_idx"])[0]["fill"] - STAKE)
                     if one(w, w["base_idx"])[1] else -STAKE) for w in nb)
        net = sum(one(w, w["base_idx"])[2] for w in nb)
        print(f"     {nm} 现行链基线: n={len(nb)} 毛 {gross:+.2f}U 净(费) {net:+.2f}U")

    # ── §1 象限画像 ──────────────────────────────────────────────────────
    QLAB = {
        ("yes", 1, 1): "Q1 同侧·上    spot,twap 均在锚上 → 热门 YES（dev+ walk+）",
        ("no", 1, 1): "Q2 同侧·下    均在锚下 → 热门 NO（dev+ walk+）",
        ("no", -1, 1): "Q3 异侧      spot>anchor>twap → 热门 NO（dev− walk+）← 现行放弃",
        ("yes", -1, 1): "Q4 异侧      twap>anchor>spot → 热门 YES（dev− walk+）← 现行放弃",
        ("yes", 1, -1): "Q5 现货正/线负（热门 YES, dev+ walk−）",
        ("no", 1, -1): "Q5 现货正/线负（热门 NO, dev+ walk−）",
        ("yes", -1, -1): "Q6 两腿同负（热门 YES, dev− walk−）",
        ("no", -1, -1): "Q6 两腿同负（热门 NO, dev− walk−）",
    }
    for nm, ws in (("events", wins_ev), ("btc", wins_bt)):
        c = collections.Counter(quad(w["rows"][w["base_idx"]]) for w in ws if w["base_idx"] is not None)
        cnt, cnt_base = collections.Counter(), collections.Counter()
        for w in ws:
            seen = set()
            for r in w["rows"]:
                if r["walk"] is None:
                    continue
                seen.add((r["side"], 1 if r["dev"] >= 0 else -1, 1 if r["walk"] >= 0 else -1))
            for k in seen:
                cnt[k] += 1
            if w["base_idx"] is not None:
                r = w["rows"][w["base_idx"]]
                if r["walk"] is not None:
                    cnt_base[(r["side"], 1 if r["dev"] >= 0 else -1, 1 if r["walk"] >= 0 else -1)] += 1
        print(f"§1 {nm} 三线象限（窗口计数 = 任一 tick 属该象限；括号 = 现行成交 tick 所在象限）:")
        for k in sorted(cnt, key=lambda k: -cnt[k]):
            print(f"     {QLAB[k]}: {cnt[k]} 窗（现行成交 {cnt_base.get(k, 0)}）")
        pool = collections.Counter()
        for w in ws:
            seen = set()
            for r in w["rows"]:
                if r["dev"] < 0 and r["walk"] is not None and r["walk"] >= 0:
                    seen.add(r["side"])
            for s in seen:
                pool[s] += 1
        print(f"     现行成交 tick 的两腿符号: " + "、".join(f"{k} {v}" for k, v in c.most_common()))
        print(f"     被放弃支路机会池（任一处 tick 满足 dev<0 ∧ walk≥0）: NO 侧 {pool['no']} 窗 / YES 侧 {pool['yes']} 窗")

    # ── §2 events 主扫描 W × G ───────────────────────────────────────────
    print()
    line()
    print("§2 events 扫描：W（线腿阈值, 美元） × G（|dev| 下限, 美元）；其余阈值 = 现行（dev 63 / σ腿 开 / #29 闸 43）")
    line()
    print(f"{'W':>4} {'G':>4} | {'新增':>4} {'改写':>4} {'保持':>4} | {'v2净':>8} {'Δ':>7} {'Δ 95%(日配对)':>19} | "
          f"{'新增批净':>8} {'新增批 95%':>19} {'改写Δ':>8}")
    results = []
    for W in (0, 10, 20, 30, 43, 63):
        for G in (0, 20, 40, 63):
            r = run(wins_ev, dict(BASE_CFG, W=float(W), G=float(G)))
            print(f"{W:>4} {G:>4} | {len(r['added']):>4} {len(r['sub']):>4} {r['kept']:>4} | "
                  f"{r['v2_net']:>+8.2f} {r['delta']:>+7.2f} {fmt_ci(r['delta_ci']):>19} | "
                  f"{r['add_net']:>+8.2f} {fmt_ci(r['add_ci']):>19} {r['sub_delta']:>+8.2f}")
            results.append((W, G, r))

    print("\n（主扫描里非 G=0 行若与 G=0 同数 ⇒ |dev| 下限不起作用；新增批=改写外多出来的窗）")
    print("（新增批画像：W×G=0 各行）")
    for W, G, r in results:
        if G:
            continue
        n, wl, wr, fl, net, sd, sg = batch(r["added"])
        print(f"   W={W:>2}: 新增 n={n:>3} 赢{wl:>3} WR {wr:>6.2f}% fill均 {fl:.4f} 余量 {wr - fl*100:+.2f}pp "
              f"净 {net:+.2f}U {fmt_ci(r['add_ci'])} | 侧 {sd} 段 {sg} | 改写 n={len(r['sub'])} Δ {r['sub_delta']:+.2f}U {fmt_ci(r['sub_ci'])}")
        print(f"         {sub_profile(r['sub'])}")

    # 选定配置：(W,G) 对 —— 主扫描里 Δ 下界最高者 + 四个代表格
    bw, bg, _ = max(results, key=lambda t: t[2]["delta_ci"][0])
    chosen = [(0, 0), (20, 40), (20, 63), (43, 0), (63, 0)]
    if (bw, bg) not in chosen:
        chosen.append((bw, bg))
    chosen = sorted(set(chosen))
    print(f"\n选定配置（W,G）: {chosen}（含 Δ 下界最高者 ({bw},{bg})）")

    # ── §3 events 参数邻域重扫（围绕选定 W）─────────────────────────────
    print()
    line()
    print(f"§3 events 参数邻域重扫（基准行 = W={bw}, G={bg}；每行只动一个键，基线同参数关线腿）")
    line()
    variants = [("基准", {})]
    for dv in (40, 50, 80):
        variants.append((f"dev {DEV_USD:.0f}→{dv}", dict(dev=float(dv))))
    for wx in (0, 100):
        variants.append((f"#29 闸 walk_x {WALK_X:.0f}→{wx}", dict(walk_x=float(wx))))
    for p15 in (0.83, 0.85):
        variants.append((f"T150 价格腿 >{p15}", dict(p150=p15)))
    for fx in (0.85, 0.0):
        variants.append((f"地板 >{fx}" if fx else "地板关", dict(floor=fx)))
    variants.append(("σ 路线关", dict(sigma=False)))
    print(f"{'变体':>18} | {'v2净':>8} {'Δ':>7} {'Δ 95%(日配对)':>19} | {'新增':>4} {'新增批净':>8}")
    for lbl, over in variants:
        cfg = dict(BASE_CFG, W=float(bw), G=float(bg))
        cfg.update(over)
        r = run(wins_ev, cfg)
        print(f"{lbl:>18} | {r['v2_net']:>+8.2f} {r['delta']:>+7.2f} {fmt_ci(r['delta_ci']):>19} | "
              f"{len(r['added']):>4} {r['add_net']:>+8.2f}")
    print("（注：Δ = v2 − 同参数基线（基线 = 本行参数但关掉线腿）；『基准』行与 §2 对应格逐位一致）")

    # ── §4 选定配置细账（events）────────────────────────────────────────
    print()
    line()
    print("§4 events 细账（选定配置）")
    line()
    rs = {}
    for W, G in chosen:
        r = run(wins_ev, dict(BASE_CFG, W=float(W), G=float(G)))
        rs[(W, G)] = r
        n, wl, wr, fl, net, sd, sg = batch(r["added"])
        print(f"W={W:>2} G={G:>2}: Δ {r['delta']:>+7.2f}U {fmt_ci(r['delta_ci'])} | "
              f"新增 n={n} 赢{wl} WR {wr:.2f}% 余量 {wr - fl*100:+.2f}pp 净 {net:+.2f}U | "
              f"改写 n={len(r['sub'])} Δ {r['sub_delta']:+.2f}U {fmt_ci(r['sub_ci'])} | "
              f"分解核对 Δ≈新增+改写: {r['delta']:+.2f} vs {r['add_net'] + r['sub_delta']:+.2f}")
        if r["sub"]:
            print(f"            {sub_profile(r['sub'])}")
    # 逐日（用 Δ 最大者）
    best_pair = max(chosen, key=lambda k: rs[k]["delta"])
    r = rs[best_pair]
    print(f"\n逐日（W={best_pair[0]}, G={best_pair[1]}）：")
    base_day, v2_day = collections.defaultdict(float), collections.defaultdict(float)
    cfg_best = dict(BASE_CFG, W=float(best_pair[0]), G=float(best_pair[1]))
    for w in wins_ev:
        if w["base_idx"] is not None:
            base_day[w["date"]] += one(w, w["base_idx"])[2]
        ve = chain(w["rows"], cfg_best)
        if ve is not None:
            v2_day[w["date"]] += one(w, ve)[2]
    add_day = r["add_day"]
    print(f"   {'日期':>10} {'现行净':>8} {'v2净':>8} {'Δ':>8} {'其中新增批':>10}")
    for d in sorted(set(base_day) | set(v2_day)):
        print(f"   {d:>10} {base_day.get(d, 0):>+8.2f} {v2_day.get(d, 0):>+8.2f} "
              f"{v2_day.get(d, 0) - base_day.get(d, 0):>+8.2f} {add_day.get(d, 0):>+10.2f}")
    # 集中度：剔除新增批最大的 1/2/3 天
    top = sorted(add_day.items(), key=lambda kv: -kv[1])
    print(f"   新增批最大 3 天: " + "、".join(f"{d}({v:+.2f})" for d, v in top[:3]))
    for k in (1, 2, 3):
        d2 = {d: v for d, v in add_day.items() if d not in dict(top[:k])}
        lo, hi = boot_delta({}, d2)
        print(f"   剔新增批 top{k} 天: 新增批净 {sum(d2.values()):+.2f}U 95% [{lo:+.2f},{hi:+.2f}]")

    # ── §5 btc 验证 ─────────────────────────────────────────────────────
    print()
    line()
    print("§5 btc 验证（同配置；先行权数字应与 14 天基线对账）")
    line()
    print(f"{'配置':>10}: {'v2净':>8} {'Δ':>7} {'Δ 95%(日配对)':>19} | {'新增':>4} {'新增净':>7} {'新增批 95%':>19} | {'改写':>4} {'改写Δ':>7}")
    for W, G in [(None, 0)] + chosen:
        cfg = dict(BASE_CFG, W=None if W is None else float(W), G=float(G))
        r = run(wins_bt, cfg)
        n, wl, wr, fl, net, sd, sg = batch(r["added"])
        tag = "现行(无 v2)" if W is None else f"W={W},G={G}"
        print(f"{tag:>10}: {r['v2_net']:>+8.2f} {r['delta']:>+7.2f} {fmt_ci(r['delta_ci']):>19} | "
              f"{n:>4} {net:>+7.2f} {fmt_ci(r['add_ci']):>19} | {len(r['sub']):>4} {r['sub_delta']:>+7.2f}"
              + (f" | 侧 {sd} 段 {sg}" if n else ""))
        if W is not None and r["sub"]:
            print(f"            {sub_profile(r['sub'])}")
    r = run(wins_bt, dict(BASE_CFG, W=float(best_pair[0]), G=float(best_pair[1])))
    add_day = r["add_day"]
    top = sorted(add_day.items(), key=lambda kv: -kv[1])
    print(f"   btc {best_pair} 新增批最大 3 天: " + "、".join(f"{d}({v:+.2f})" for d, v in top[:3]))
    for k in (1, 2, 3):
        d2 = {d: v for d, v in add_day.items() if d not in dict(top[:k])}
        lo, hi = boot_delta({}, d2)
        print(f"   剔新增批 top{k} 天: 新增批净 {sum(d2.values()):+.2f}U 95% [{lo:+.2f},{hi:+.2f}]")

    # ── §5b btc 同网格全扫（防「只验了 events 选出来的格子」）──────────────
    print()
    print("§5b btc 同网格全扫（与 §2 同参数；看有没有任何一格在 btc 上也成立）")
    print(f"{'W':>4} {'G':>4} | {'新增':>4} {'改写':>4} {'保持':>4} | {'v2净':>8} {'Δ':>7} {'Δ 95%(日配对)':>19} | "
          f"{'新增批净':>8} {'新增批 95%':>19} {'改写Δ':>8}")
    npos = 0
    for W in (0, 10, 20, 30, 43, 63):
        for G in (0, 20, 40, 63):
            r = run(wins_bt, dict(BASE_CFG, W=float(W), G=float(G)))
            if r["delta_ci"][0] > 0:
                npos += 1
            print(f"{W:>4} {G:>4} | {len(r['added']):>4} {len(r['sub']):>4} {r['kept']:>4} | "
                  f"{r['v2_net']:>+8.2f} {r['delta']:>+7.2f} {fmt_ci(r['delta_ci']):>19} | "
                  f"{r['add_net']:>+8.2f} {fmt_ci(r['add_ci']):>19} {r['sub_delta']:>+8.2f}")
    print(f"（btc 全网格 Δ 下界 > 0 的格数: {npos}）")

    # ── §6 参考格：下注侧两腿都反（both_neg）────────────────────────────
    print()
    line()
    print("§6 参考格（不属 v2）：市场热门侧与两条腿都反（dev<0 ∧ walk<0，价格腿同现行）")
    for nm, ws in (("events", wins_ev), ("btc", wins_bt)):
        r = run(ws, dict(BASE_CFG, mode="both_neg", W=None, G=0.0), strict=False)
        n, wl, wr, fl, net, sd, sg = batch(r["added"])
        print(f"   {nm}: 无信号窗内首触发 n={n} 赢{wl} WR {wr:.2f}% fill均 {fl:.4f} 净 {net:+.2f}U "
              f"{fmt_ci(r['add_ci'])} | 侧 {sd} 段 {sg} | （丢单 {r['lost']} / 被顶替 {len(r['sub'])}）")
        if r["sub"]:
            print(f"        全窗合计: v2净 {r['v2_net']:+.2f} Δ {r['delta']:+.2f} {fmt_ci(r['delta_ci'])} | "
                  f"被顶替 Δ {r['sub_delta']:+.2f}U | {sub_profile(r['sub'])}")


if __name__ == "__main__":
    main()
