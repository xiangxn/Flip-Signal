#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫尾盘：在 rem=100 追加一个判定点（条件 = T150 的条件）——2026-10-02 用户提案。

用户原话：「尝试在 rem=100 时添加一个判定点，条件用 t150 一样的，先在回测中看，
根据新的胜率、EV、收益，我们再考虑是否落到 go 引擎中」。

链形状（变体）: 150(严格 ⑤ ∧ walk≥43) → **100(同 T150)** → 60(⑤ ∧ 地板>0.83) → 监听(② ∧ 地板>0.83)
基线（现行 #33）: 150(严格 ⑤ ∧ walk≥43) →                     60(⑤ ∧ 地板>0.83) → 监听(② ∧ 地板>0.83)

语义（与 31_tail_checkpoints.py 同族的既有先例, 保证与旧曲线可比）:

  - 每个检查点消费「上一个被消费 tick **之后**」的第一个 `rem ≤ 阈值` 的 tick;
  - 若前一个检查点消费时本检查点的阈值已经过去（迟到接入）⇒ **跳过**, 不追溯判定;
  - 首个可判定 tick 已 `rem ≤ 60` ⇒ 跳过全部前置检查点（与 oracle 同, 不伪造 t150 行）;
  - 追加点**只抢答**: 不改变原链任何路径 ⇒ 变体信号 ⊇ 基线信号, **丢失信号恒 0**
    （T100 消费的 tick 在 rem ∈ (60,100], 不在 T60/监听段所用的 `rem ≤ 60` 子集里,
     故段 2/3 逐位不受影响; 唯一例外是跨段数据洞, 由 §0c 审计）。

⚠️ 这是「追加检查点」族在**新基线**上的复测。旧基线（#26 之后, +35.09U）上 T70~135
十四变体全不显著、T90/T105 是整条曲线最差的一带（`docs/tail_entry_timing_2026-09-29.md`、
31 号脚本）; 但现行链已加 #29 闸 + #32/#33 地板 ⇒ 宇宙与口径都变了, 故在新 pin
（2074 / +70.314372U）上按用户要求重跑。**新旧曲线不可直接比**, 只看新曲线形态。

口径与 23（oracle）/ 38 / 42 逐位一致: 宇宙 = `s38.universe()`; σ 按事件索引现算;
T150 价格腿严格大于; walk 闸恒为 C43; 地板恒为现行落地面（t60 + 监听）。

用法: python/venv/bin/python python/v4/43_tail_checkpoint_t100.py
"""

import sys
import random
import collections
import importlib.util
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


s23 = load("s23", BASE / "23_tail_integrated.py")   # oracle：row / r2 / r5 / walk_ok
s38 = load("s38", BASE / "38_tail_walk_gate.py")    # 宇宙/统计口径（复用即同源）
s42 = load("s42", BASE / "42_tail_floor_from_t60.py")  # 基线链本体（#33 落地面）

STAKE = s23.STAKE
T150, T60 = s23.T150, s23.T60
WALK_X = s23.WALK_MIN_USD          # 43：T150 入场闸（决策 #29）
FLOOR_X = s23.FLOOR_MIN_PRICE      # 0.83：价格地板阈值（决策 #32/#33）

# 三把 pin（oracle 23 / 42 打印的现行值）
PIN_C43 = (2080, 64.026354, 6686)
PIN_32 = (2076, 72.578473, 6697, 15)
PIN_33 = (2074, 70.314372, 6704, 24)

CURVE = (70, 80, 90, 100, 105, 110, 120, 135)     # §6 位置曲线（同一算子/闸, 只挪位置）


# ── 链：三段递进 + 追加检查点（用户提案 = cks=((100, True),)）────────────────

def r5_at(r, thr, strict):
    """⑤ 本体, 但**价格腿阈值换成 thr**（用户的「T100 价格腿提高」提案用）。

    ⚠️ 这是 35 号脚本那一族的「换阈值」动作——回测上必须按搜索代价读（见 §6）。
    """
    if not (r["fill"] > thr if strict else r["fill"] >= thr):
        return False
    if r["dev"] >= s23.DEV_USD:
        return True
    return (r["sd"] is not None and r["sd"] >= s23.SD_MIN_USD
            and r["sig"] is not None and r["sig"] >= 1.0)


def chain(w, cks=(), use_walk=True, walk_x=None, ck_floor=None, ck_price=None):
    """返回 (rows, info)。

    cks: ((阈值, 价格腿严格?), …) —— 追加检查点（在 T150 之后、T60 之前, 互不重叠）。
         ⚠️ 检查点的其余腿 = **⑤ 本体**（dev/σ 与 T150 同）, walk 闸按 use_walk 施加
         （用户口径「条件用 t150 一样的」⇒ 默认 True）。
    info: {"sig": 段名|None, "ck": [(阈值, "walk_low"|"leg_out"), …], "gaps": 跨段洞数,
           "shadow": [floor_low 影子行]}
    """
    ticks, anchor, sd, date, outcome = (w["ticks"], w["anchor"], w["sd"],
                                        w["date"], w["outcome"])
    rows, info = [], {"sig": None, "ck": [], "gaps": 0, "shadow": []}
    if not ticks:
        return rows, info
    wx = WALK_X if walk_x is None else walk_x
    shadowed = False
    ck_tick = None
    head = ticks[0]
    if head["rem"] > T60:
        # 段 1: T=150（价格腿严格大于 #26 + 入场闸 #29）
        r = s23.row(head, anchor, sd, date, outcome, "t150")
        ok5 = s23.r5(r, strict_price=True)
        if ok5 and (not use_walk or s23.walk_ok(r, wx)):
            r["ok"] = True
            rows.append(r)
            info["sig"] = "t150"
            return rows, info
        info["ck"].append((T150, "walk_low" if ok5 else "leg_out"))
        rows.append(r)
        cur = 0
        # 追加检查点（用户提案那一族）
        for (thr, strict) in cks:
            if ticks[cur]["rem"] <= thr:
                continue                      # 迟到接入: 该检查点时刻已过 ⇒ 跳过
            j = cur + 1
            while j < len(ticks) and ticks[j]["rem"] > thr:
                j += 1
            if j >= len(ticks):
                info["gaps"] += 1             # 本窗没有 rem ≤ thr 的 tick（跨段数据洞）
                break
            cur = j
            ck_tick = ticks[cur]
            rc = s23.row(ck_tick, anchor, sd, date, outcome, f"t{thr}")
            okc = (r5_at(rc, ck_price, strict) if ck_price is not None
                   else s23.r5(rc, strict_price=strict))
            okw = okc and (not use_walk or s23.walk_ok(rc, wx))
            if okw and ck_floor is not None and rc["fill"] <= ck_floor:
                # 敏感性用: 检查点上**也**套价格地板（用户口径「同 T150」本身不含地板）
                info["ck"].append((thr, "floor_low"))
                rows.append(rc)
                continue
            if okw:
                rc["ok"] = True
                rows.append(rc)
                info["sig"] = f"t{thr}"
                return rows, info
            info["ck"].append((thr, "walk_low" if okc else "leg_out"))
            rows.append(rc)
        rest = [x for x in ticks if x["rem"] <= T60]
        if ck_tick is not None and ck_tick["rem"] <= T60:
            # 跨段洞: 检查点消费到的 tick 本身已在 T60 区间 ⇒ 同一个 tick 不判两次
            rest = [x for x in rest if x is not ck_tick]
    else:
        rest = ticks                            # 迟到接入: 跳过全部前置检查点

    if rest:
        # 段 2: T=60（⑤ 非严格 + 地板 >0.83, 决策 #33）
        r2 = s23.row(rest[0], anchor, sd, date, outcome, "t60")
        if s23.r5(r2):
            if r2["fill"] <= FLOOR_X:
                r2["reject_reason"] = "floor_low"
                info["shadow"].append(r2)
                rows.append(r2)
                shadowed = True
            else:
                r2["ok"] = True
                rows.append(r2)
                info["sig"] = "t60"
                return rows, info
        else:
            rows.append(r2)
        # 段 3: 监听（② + 地板, 决策 #32/#33; 影子行跨段共用闩锁）
        for x in rest[1:]:
            rl = s23.row(x, anchor, sd, date, outcome, "listen")
            if not s23.r2(rl):
                continue
            if rl["fill"] <= FLOOR_X:
                if not shadowed:
                    rl["reject_reason"] = "floor_low"
                    info["shadow"].append(rl)
                    rows.append(rl)
                    shadowed = True
                continue
            rl["ok"] = True
            rows.append(rl)
            info["sig"] = "listen"
            return rows, info
    return rows, info


def run_chain(wins, cks=(), use_walk=True, walk_x=None, ck_floor=None, ck_price=None):
    """跑全宇宙 → (信号行列表, 逐窗 info 列表, 全部行数, 影子行数)。"""
    sigs, infos, nrows, nshadow = [], [], 0, 0
    for i, w in enumerate(wins):
        rows, info = chain(w, cks, use_walk, walk_x, ck_floor, ck_price)
        nrows += len(rows)
        nshadow += len(info["shadow"])
        info["wid"] = i
        infos.append(info)
        for r in rows:
            r["wid"] = i
            r["walk0"] = s38.walk_of(w["ticks"][0], w["anchor"])
            if r.get("ok"):
                sigs.append(r)
    return sigs, infos, nrows, nshadow


def sigkey(r):
    """信号的同一性: 段 + 入场秒 + 成交价 + 侧别（用于分辨「同一笔」与「改道」）。"""
    return (r["stage"], r["rem"], r["fill"], r["side"])


def desc(sigs):
    n = len(sigs)
    wr = sum(r["settle_won"] for r in sigs) / n * 100
    return (f"n={n:<5} WR {wr:6.2f}%  输 {sum(1 for r in sigs if not r['settle_won']):<3} "
            f"均价 {sum(r['fill'] for r in sigs)/n:.4f}  EV/注 {s38.pnl(sigs)/n:+.4f}U  "
            f"P&L {s38.pnl(sigs):+8.2f}U")


def main():
    wins = s38.universe()
    print(f"宇宙：{len(wins)} 窗（pin 3640 有可判定 tick ∧ 有 σ 的窗）")

    # ── §0 口径自检：三把 pin + 本脚本链在新链参数下的逐位一致性 ────────────
    print("\n" + "=" * 100)
    print("§0 口径自检（不通过则后面数字不可信）")
    c43_s, _, c43_rows = s42.run(wins, None)
    p32_s, p32_sh, p32_rows = s42.run(wins, "listen")
    p33_s, p33_sh, p33_rows = s42.run(wins, "t60")
    for lab, sigs, rows, sh, pin in (
            ("C43（只闸 T150）", c43_s, c43_rows, None, PIN_C43),
            ("#32（地板只拦监听段）", p32_s, p32_rows, p32_sh, PIN_32),
            ("#33（地板拦 t60+监听, 现行基线）", p33_s, p33_rows, p33_sh, PIN_33)):
        ok = len(sigs) == pin[0] and abs(s38.pnl(sigs) - pin[1]) < 1e-4 and rows == pin[2]
        if len(pin) == 4:
            ok = ok and len(sh) == pin[3]
        extra = f"  影子 {len(sh)}   pin {pin[3]}" if len(pin) == 4 else ""
        print(f"  {lab:<34}{desc(sigs)}  行 {rows}{extra}  {'✅' if ok else '❌'}")

    # 本脚本 chain(cks=()) 必须与 42 的 #33 链逐位同解（加检查点前的忠实性自检）
    my_s, my_infos, my_rows, my_sh = run_chain(wins, cks=())
    same = (len(my_s) == len(p33_s) and abs(s38.pnl(my_s) - s38.pnl(p33_s)) < 1e-9
            and my_rows == p33_rows and my_sh == len(p33_sh)
            and collections.Counter(sigkey(r) for r in my_s)
            == collections.Counter(sigkey(r) for r in p33_s))
    print(f"  本脚本 chain(cks=()) vs #33 链逐位一致：{'✅' if same else '❌ 实现有差, 停'}")
    if not same:
        sys.exit(1)
    # ⚠️ 逐窗对照一律用 my_s 当基线（上面刚验过它与 #33 逐位一致）——s42.run 的 wid 是
    # 1-based、本脚本是 0-based, 混用会让 {wid: row} 整体错位（曾把丢失信号算成 628）。
    base_sigs = my_s
    BD_base = s38.by_day(base_sigs)             # Δ 的参照系 = 现行基线
    base_by_wid = {r["wid"]: r for r in base_sigs}
    dates = sorted(set(w["date"] for w in wins))
    half = len(dates) // 2
    h1, h2 = set(dates[:half]), set(dates[half:])

    # ── §1 用户提案：rem=100 追加点（条件 = T150 的）───────────────────────
    print("\n" + "=" * 100)
    print("§1 用户提案：rem=100 追加一个判定点（价格腿严格 > 0.80 ∧ ⑤ 的 dev/σ 腿 ∧ walk ≥ 43）")
    var_s, var_infos, var_rows, var_sh = run_chain(wins, cks=((100, True),))
    lo, hi = s38.boot_delta(BD_base, s38.by_day(var_s))
    print(f"  基线 #33   {desc(p33_s)}")
    print(f"  变体（+T100）{desc(var_s)}")
    print(f"  ΔP&L vs #33 = {s38.pnl(var_s) - PIN_33[1]:+.2f}U   日级配对 95% [{lo:+.2f}, {hi:+.2f}]"
          f"   {'⚠️ 含 0（不显著）' if lo <= 0 <= hi else '★ 不含 0'}")
    print(f"  全部行 {my_rows} → {var_rows}；影子行 {my_sh} → {var_sh}")
    print("\n  按段（信号行, 变体的 t100 是新段）:")
    stages = ["t150", "t100", "t60", "listen"]
    for s in stages:
        g = [r for r in var_s if r["stage"] == s]
        if g:
            print(f"    {s:<8}{desc(g)}")
    print(f"    {'合计':<8}{desc(var_s)}")

    # 逐日
    print("\n  逐日明细（P&L 为当日合计）:")
    d_base, d_var = s38.by_day(base_sigs), s38.by_day(var_s)
    allsig_days = sorted(set(r["date"] for r in var_s))
    print(f"  {'日期':<12}{'基线n':>7}{'变体n':>7}{'基线P&L':>10}{'变体P&L':>10}{'Δ':>9}")
    for d in allsig_days:
        gb = [r for r in base_sigs if r["date"] == d]
        gv = [r for r in var_s if r["date"] == d]
        dd = d_var.get(d, 0) - d_base.get(d, 0)
        print(f"  {d:<12}{len(gb):>7}{len(gv):>7}{d_base.get(d,0):>+10.2f}"
              f"{d_var.get(d,0):>+10.2f}{dd:>+9.2f}")
    print(f"  {'合计':<12}{len(base_sigs):>7}{len(var_s):>7}{s38.pnl(base_sigs):>+10.2f}"
          f"{s38.pnl(var_s):>+10.2f}{s38.pnl(var_s)-s38.pnl(base_sigs):>+9.2f}")
    print(f"  前半（{min(h1)}~{max(h1)}）Δ = "
          f"{sum(d_var.get(d,0)-d_base.get(d,0) for d in h1):+.2f}U   "
          f"后半（{min(h2)}~{max(h2)}）Δ = "
          f"{sum(d_var.get(d,0)-d_base.get(d,0) for d in h2):+.2f}U")

    # ── §2 分解：新增 vs 改道（T100 只抢答, 丢失信号应恒 0）────────────────
    print("\n" + "=" * 100)
    print("§2 分解：新增（基线不下单的窗）vs 改道（基线有信号但入场点/价变了）")
    var_by_wid = {r["wid"]: r for r in var_s}
    new, moved, lost, unchanged = [], [], [], 0
    for i, w in enumerate(wins):
        b, v = base_by_wid.get(i), var_by_wid.get(i)
        if b and v:
            if sigkey(b) == sigkey(v):
                unchanged += 1
            else:
                moved.append((b, v))
        elif v and not b:
            new.append(v)
        elif b and not v:
            lost.append(b)
    print(f"  丢失信号 = {len(lost)}（应恒 0 —— 追加点只抢答, 不改原链）")
    print(f"  原样（信号与基线同一笔） = {unchanged}")
    if new:
        nw = sum(r["settle_won"] for r in new)
        px = sum(r["fill"] for r in new) / len(new)
        print(f"\n  新增 n={len(new)}  WR {nw/len(new)*100:.2f}%  均价 {px:.4f}"
              f"（盈亏平衡 WR = {px*100:.2f}%）  EV/注 {s38.pnl(new)/len(new):+.4f}U  "
              f"P&L {s38.pnl(new):+.2f}U")
        byd = collections.defaultdict(list)
        for r in new:
            byd[r["date"]].append(r)
        ds = sorted(byd)
        rng = random.Random(43)
        boot = sorted(s38.pnl([r for k in (rng.choice(ds) for _ in ds) for r in byd[k]])
                      for _ in range(2000))
        q = lambda p: boot[int(p * len(boot))]                            # noqa: E731
        print(f"      日级 bootstrap 95%: EV/注 [{q(0.025)/len(new):+.4f}, "
              f"{q(0.975)/len(new):+.4f}]U")
    else:
        print("\n  新增 n=0")
    if moved:
        old_p = s38.pnl([b for b, _ in moved])
        new_p = s38.pnl([v for _, v in moved])
        nf = sorted(v["fill"] for _, v in moved)
        bf = sorted(b["fill"] for b, _ in moved)
        flips = sum(1 for b, v in moved if b["side"] != v["side"])
        print(f"\n  改道 n={len(moved)}  新 fill 中位 {nf[len(nf)//2]:.4f}（旧 {bf[len(bf)//2]:.4f}）"
              f"  换边 {flips}")
        print(f"      这批窗 P&L: {old_p:+.2f}U → {new_p:+.2f}U（改道税/收益 {new_p-old_p:+.2f}U）")
        print(f"      新入场 rem 分布: {dict(sorted(collections.Counter((v['rem']//10)*10 for _, v in moved).items()))}")
    if lost:
        print(f"  ⚠️ 丢失明细: {[(b['date'], b['stage'], b['rem']) for b in lost[:10]]}")

    # ── §3 T100 的信号从哪来：基线里这窗的 T150 是被谁拒的 ────────────────
    print("\n" + "=" * 100)
    print("§3 T100 信号的来路（对每个 T100 信号, 看基线世界里同一窗发生了什么）")
    prov = collections.Counter()
    walklow_recovered = []
    for r in var_s:
        if r["stage"] != "t100":
            continue
        i = r["wid"]
        w = wins[i]
        b = base_by_wid.get(i)
        head = w["ticks"][0]
        r0 = s23.row(head, w["anchor"], w["sd"], w["date"], w["outcome"], "t150")
        # 基线的 T150 拒绝原因（与 oracle 同口径: 先看 ⑤, 再看 walk 闸）
        if head["rem"] <= T60:
            rej = "迟到接入（无 t150 段）"
        elif not s23.r5(r0, strict_price=True):
            rej = "T150 ⑤ 不达标"
        elif not s23.walk_ok(r0, WALK_X):
            rej = "T150 walk_low（被闸）"
        else:
            rej = "T150 本应出信号（异常）"
        if b is None:
            prov[rej + " → 基线整窗无信号"] += 1
        else:
            prov[rej + f" → 基线改道至 {b['stage']}"] += 1
        if rej.startswith("T150 walk_low") and b is not None:
            walklow_recovered.append((b, r))
    for k, v in prov.most_common():
        print(f"  {v:>5}  {k}")
    if walklow_recovered:
        op = s38.pnl([b for b, _ in walklow_recovered])
        np_ = s38.pnl([r for _, r in walklow_recovered])
        f1 = sorted(b["fill"] for b, _ in walklow_recovered)
        f2 = sorted(r["fill"] for _, r in walklow_recovered)
        print(f"\n  其中「基线被 walk 闸拒 → 改道 t60/监听」的有 {len(walklow_recovered)} 窗, T100 把它们提前接住:")
        print(f"      fill 中位 {f2[len(f2)//2]:.4f}（基线改道后 {f1[len(f1)//2]:.4f}）"
              f"  P&L {op:+.2f}U → {np_:+.2f}U（改道回收 {np_-op:+.2f}U）")
    t100_rows = [r for r in var_s if r["stage"] == "t100"]
    if t100_rows:
        wrl = [r for r in t100_rows if not r["settle_won"]]
        print(f"\n  T100 段信号 n={len(t100_rows)} 输 {len(wrl)} 笔, P&L {s38.pnl(t100_rows):+.2f}U;")
        print("     输单明细（赢单不逐笔打印）:")
        for r in sorted(wrl, key=lambda r: r["date"]):
            print(f"     {r['date']}  rem={r['rem']:>3}  {r['side']:<5} fill={r['fill']:.2f}  "
                  f"walk={r['walk']:7.1f}  （-2.00U）")
        wins_ = [r for r in t100_rows if r["settle_won"]]
        if wins_:
            print(f"     赢单合计 +{s38.pnl(wins_):.2f}U（均价 "
                  f"{sum(r['fill'] for r in wins_)/len(wins_):.4f}）")

    # ── §4 逐窗输赢翻转 ─────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("§4 逐窗输赢翻转账（新增的那部分信号把赢输结构改成了什么样）")
    w2l = l2w = nwin = nlos = sidef = 0
    bw = sum(1 for r in base_sigs if r["settle_won"])
    bl = len(base_sigs) - bw
    for b, v in moved:
        if b["settle_won"] and not v["settle_won"]:
            w2l += 1
        elif v["settle_won"] and not b["settle_won"]:
            l2w += 1
        if b["side"] != v["side"]:
            sidef += 1
    aw = sum(1 for r in new if r["settle_won"])
    al = len(new) - aw
    tw = sum(1 for r in var_s if r["settle_won"])
    tl = len(var_s) - tw
    print(f"  基线: 赢 {bw} / 输 {bl}      变体: 赢 {tw} / 输 {tl}（净增输 {tl-bl:+d}）")
    print(f"  改道部分: 赢→输 {w2l}  输→赢 {l2w}  换边 {sidef}")
    print(f"  新增部分: 新增赢 {aw}  新增输 {al}")

    # ── §5 敏感性：算子 / walk 闸 / 位置曲线 ─────────────────────────────
    print("\n" + "=" * 100)
    print("§5 敏感性（同一维只动一处; ⚠️ 多格搜索 ⇒ 单点正值要按搜索代价压缩）")

    def brief(lab, sigs):
        lo_, hi_ = s38.boot_delta(BD_base, s38.by_day(sigs))
        d = s38.pnl(sigs) - PIN_33[1]
        star = " " if lo_ <= 0 <= hi_ else "*"
        h1d = sum(s38.by_day(sigs).get(x, 0) - d_base.get(x, 0) for x in h1)
        h2d = sum(s38.by_day(sigs).get(x, 0) - d_base.get(x, 0) for x in h2)
        print(f"  {lab:<40}{len(sigs):>6}{d:>+9.2f}{h1d:>+9.2f}{h2d:>+9.2f}"
              f"   [{lo_:+.2f}, {hi_:+.2f}]{star}")
        return d

    print(f"  {'变体':<40}{'n':>6}{'ΔP&L':>9}{'前半':>9}{'后半':>9}{'95% CI':>20}")
    for lab, kw in (
            ("T100 严格 >0.80 + walk 闸（用户口径）", dict(cks=((100, True),))),
            ("T100 非严格 ≥0.80 + walk 闸", dict(cks=((100, False),))),
            ("T100 严格 >0.80, **无** walk 闸", dict(cks=((100, True),), use_walk=False)),
            ("T100 严格 >0.80 + walk 闸 + 地板 >0.83", dict(cks=((100, True),), ck_floor=FLOOR_X))):
        s_, _, _, _ = run_chain(wins, **kw)
        brief(lab, s_)
    print("\n  位置曲线（同算子/闸, 只挪检查点位置; 旧基线曲线见 31 号脚本, 形态不可直接比）:")
    for T in CURVE:
        s_, infos_, _, _ = run_chain(wins, cks=((T, True),))
        brief(f"追加 @T={T}", s_)
    print("  读法: 看**形态**（整段同号 = 可讨论; 单点冒尖 = 噪声尖峰）, 不看单点。")

    # ── §6 用户提案：抬高 T100 的价格腿（0.85~0.95, 2026-10-02）────────────────
    print("\n" + "=" * 100)
    print("§6 价格腿抬高扫描（T100 段: 价格腿阈值 X, 其余腿/闸/地板一字不动）")
    print("   用户假设: T100 入场容易落在价格**波峰**上 ⇒ 要求更高的价才算数。")
    # 6a 波峰检验: T100 成交价 vs 同一窗「rem ≤ 60 首个可判定 tick」的热门侧价
    n_cmp = n_cheap = n_eq = n_high = 0
    for r in t100_rows:
        later = [x for x in wins[r["wid"]]["ticks"] if x["rem"] <= T60]
        if not later:
            continue
        f2 = later[0]["fill"]
        n_cmp += 1
        if r["fill"] < f2 - 1e-12:
            n_cheap += 1
        elif r["fill"] > f2 + 1e-12:
            n_high += 1
        else:
            n_eq += 1
    if n_cmp:
        print(f"  6a 波峰检验: T100 成交价 vs 同窗稍后（rem≤60 首个判定 tick）价 —— "
              f"T100 更便宜 {n_cheap} / 相同 {n_eq} / **T100 更贵（= 随后回落, 买在当场高点）** "
              f"{n_high}（共 {n_cmp}）")
    # 6b T100 候选按成交价分桶（损失集中在便宜桶还是各桶都负）
    print("\n  6b T100 段（X=0.80）按 fill 分桶:")
    print(f"     {'桶':<12}{'n':>5}{'WR':>8}{'盈亏平衡WR':>11}{'EV/注':>10}{'P&L':>9}")
    for a, b in ((0.80, 0.85), (0.85, 0.90), (0.90, 0.95), (0.95, 0.99), (0.99, 1.01)):
        g = [r for r in t100_rows if a - 1e-9 <= r["fill"] < b - 1e-9]
        if not g:
            continue
        px = sum(r["fill"] for r in g) / len(g)
        wr = sum(r["settle_won"] for r in g) / len(g)
        print(f"     {f'{a:.2f}~{b:.2f}':<12}{len(g):>5}{wr*100:>7.2f}%{px*100:>10.2f}%"
              f"{s38.pnl(g)/len(g):>+10.4f}{s38.pnl(g):>+9.2f}U")
    # 6c 扫描: 两种算子 × X ∈ 0.85~0.95
    print("\n  6c 扫描（X=0.80 那行 = 用户口径原值）:")
    print(f"     {'X':>5}{'算子':>5}{'n':>6}{'ΔP&L':>9}{'前半':>8}{'后半':>8}{'95% CI':>20}"
          f"{'t100n':>7}{'t100P&L':>9}{'t100 EV/注':>11}")
    n_pos = 0
    grid = [round(0.80 + 0.01 * i, 2) for i in range(20)]      # 0.80~0.99（含用户区间 0.85~0.95）
    for strict in (True, False):
        for X in grid:
            kw = dict(cks=((100, strict),), ck_price=X)
            s_, _, _, _ = run_chain(wins, **kw)
            lo_, hi_ = s38.boot_delta(BD_base, s38.by_day(s_))
            d = s38.pnl(s_) - PIN_33[1]
            h1d = sum(s38.by_day(s_).get(x, 0) - d_base.get(x, 0) for x in h1)
            h2d = sum(s38.by_day(s_).get(x, 0) - d_base.get(x, 0) for x in h2)
            t = [r for r in s_ if r["stage"] == "t100"]
            tev = (s38.pnl(t) / len(t)) if t else 0.0
            star = "*" if lo_ > 0 else " "
            if lo_ > 0:
                n_pos += 1
            print(f"     {X:>5.2f}{'>' if strict else '>=':>5}{len(s_):>6}{d:>+9.2f}{h1d:>+8.2f}"
                  f"{h2d:>+8.2f}   [{lo_:+7.2f},{hi_:+7.2f}]{star}{len(t):>7}"
                  f"{s38.pnl(t):>+9.2f}U{tev:>+11.4f}")
    print(f"\n  搜索提示: {len(grid)*2} 格全在同一份 14 天样本内; 「CI 下界 > 0」的格子数 = {n_pos}。")
    print("  ⚠️ 抬价格腿的**方向性预测**（无 X 也会说的那种）: X 越高, T100 抢答越少 ⇒ 链条越")
    print("     退回基线（X→1 时 Δ→0 而**非**变正）——所以只有曲线出现**峰**且峰在区间内、")
    print("     前后半同号、且不是单格冒尖, 才值得继续; 单调爬向 0 不算证据。")

    # 跨段数据洞审计（§0c）
    gaps = sum(inf["gaps"] for inf in run_chain(wins, cks=((100, True),))[1])
    print(f"\n  §0c 审计: 跨段数据洞（检查点区间内无可判定 tick）= {gaps} 窗"
          f"（0 ⇒ 上面所有语义分支都不触发）")


if __name__ == "__main__":
    main()
