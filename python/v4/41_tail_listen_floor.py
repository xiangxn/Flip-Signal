#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫尾盘：监听段**价格地板** `有效价 > X`（2026-10-01 决策 #32）的落地依据。

用户 2026-10-01 的问题：「今天的实盘数据，短时间里就出现了两次输的，都是监听区间，
<=0.83 而且 walk 很小」。前面 40 号脚本 §9 已顺带比过「walk 闸下延到监听段」与
「换成一根价格地板」——本脚本把后者的细节补齐，作为**落地依据**：

  §0 口径自检：闸前 / 现行 C43 / 落地形态（C43 + 地板 0.83）三把 pin 逐位对上
  §1 廉价口袋长什么样（监听段信号的 fill 分布 = 地板会碰到谁）
  §2 阈值扫描（参照系 = C43；区间 = **日级配对** bootstrap 95%）
  §3 细账：X=0.83 拦下的 15 笔 → 整窗死亡 vs 同段改道（钱从哪来）
  §4 安慰剂：随机拦同样笔数（4000 次）——「按价格挑」比「瞎挑」强多少
  §5 前向判据（先写死，再跑）

口径与 23（oracle）/ 38 / 40 逐位一致：宇宙 = `s38.universe()`（可判定 tick ∧ 有 σ）;
σ 按事件索引现算; T=150 价格腿严格大于; walk 闸恒为 C43（决策 #29 只闸段 1）。
本脚本的链 = 23 的 chain() + 监听段地板一步（`fill <= X` ⇒ 落影子行且链继续）。

用法: python/venv/bin/python python/v4/41_tail_listen_floor.py
"""

import sys
import random
import argparse
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

STAKE = s23.STAKE
T60 = s23.T60
WALK_X = s23.WALK_MIN_USD          # 43：现行落地形态的 T150 入场闸（决策 #29）
FLOOR_X = s23.LISTEN_MIN_PRICE     # 0.83：本脚本要论证的那个 X

# 三把 pin（oracle 23 打印的值）
PIN_PRE_SIG, PIN_PRE_PNL = 2133, 35.092748          # 闸前（无 walk 闸、无地板）
PIN_C43_SIG, PIN_C43_PNL = 2080, 64.026354          # C43（只闸 T150）＝决策 #29 落地后
PIN_C43_ROWS = 6686
PIN_NEW_SIG, PIN_NEW_PNL = 2076, 72.578473          # C43 + 监听段地板 0.83（本脚本要落的形态）
PIN_NEW_ROWS, PIN_NEW_SHADOW = 6697, 15


# ── 链：三段递进（23 的 chain）+ T150 walk 闸 + 监听段价格地板 ─────────────

def chain(w, floor_x, walk_x=WALK_X, blocked=frozenset()):
    """返回 (rows, info)。

    floor_x=None ⇒ 无地板（复现 C43）; blocked = 被安慰剂随机拦掉的 tick id 集
    （语义与地板完全一样: 本 tick 本会是信号, 拦下后**链继续**）。
    info: shadow = 影子行; died = 本窗最终没有信号。
    """
    ticks, anchor, sd, date, outcome = (w["ticks"], w["anchor"], w["sd"],
                                        w["date"], w["outcome"])
    if not ticks:
        return [], {"shadow": [], "sig": None, "tickids": []}
    rows, info = [], {"shadow": [], "sig": None, "tickids": []}
    gated_t150 = False

    head = ticks[0]
    if head["rem"] > T60:
        r = s23.row(head, anchor, sd, date, outcome, "t150")
        ok5 = s23.r5(r, strict_price=True)          # 段 1 价格腿严格大于（#26）
        if ok5 and (walk_x is None or s23.walk_ok(r, walk_x)):
            r["ok"] = True
            rows.append(r)
            info["sig"] = ("t150", id(head))
            return rows, info
        if ok5:
            gated_t150 = True
        rows.append(r)
        rest = [x for x in ticks if x["rem"] <= T60]
    else:
        rest = ticks                                # 迟到接入: 不伪造 t150 行

    if rest:
        r2 = s23.row(rest[0], anchor, sd, date, outcome, "t60")
        if s23.r5(r2):
            r2["ok"] = True
            rows.append(r2)
            info["sig"] = ("t60", id(rest[0]))
            return rows, info
        rows.append(r2)
        shadowed = False
        for x in rest[1:]:
            rl = s23.row(x, anchor, sd, date, outcome, "listen")
            if not s23.r2(rl):
                continue
            cut = (floor_x is not None and rl["fill"] <= floor_x) or (id(x) in blocked)
            if cut:
                if not shadowed:                    # 影子行每窗至多一条（= 无地板世界的那一笔）
                    rl["reject_reason"] = "floor_low"
                    info["shadow"].append(rl)
                    rows.append(rl)
                    shadowed = True
                continue
            rl["ok"] = True
            rows.append(rl)
            info["sig"] = ("listen", id(x))
            return rows, info
    return rows, info


def run(wins, floor_x, walk_x=WALK_X, blocked=frozenset(), collect_ticks=False):
    """跑一个 config。返回 (信号行, 影子行, 行数, 被拦笔数, tick→信号 的映射)。

    ⚠️ 「被拦笔数」= 影子行数（无地板世界里本该成交的那些）；它与信号数的变化不同
    （被拦的窗可能改道重入 ⇒ 净信号 −4 而影子行 15）。
    """
    sigs, shadows, tickmap = [], [], {}
    nrows = 0
    wid = 0
    for w in wins:
        wid += 1
        rows, info = chain(w, floor_x, walk_x, blocked)
        nrows += len(rows)
        for r in rows:
            r["wid"] = wid
            r["walk0"] = s38.walk_of(w["ticks"][0], w["anchor"])
        for r in rows:
            if r.get("ok"):
                sigs.append(r)
        for r in info["shadow"]:
            shadows.append(r)
        if collect_ticks and info["sig"] and info["sig"][0] == "listen":
            tickmap[info["sig"][1]] = (w["date"], wid)
    return sigs, shadows, nrows, tickmap


def desc(sigs):
    n = len(sigs)
    wr = sum(r["settle_won"] for r in sigs) / n * 100
    return (f"n={n:<5} WR {wr:6.2f}%  输 {sum(1 for r in sigs if not r['settle_won']):<3} "
            f"P&L {s38.pnl(sigs):+8.2f}U")


def main():
    ap = argparse.ArgumentParser(description="监听段价格地板的落地依据")
    ap.add_argument("--nperm", type=int, default=400,
                    help="§4 安慰剂次数（默认 400 = 秒级; 写文档/复验用 4000, 约 9 分钟）")
    args = ap.parse_args()
    wins = s38.universe()
    print(f"宇宙：{len(wins)} 窗（pin 3640 有可判定 tick ∧ 有 σ 的窗）")

    # ── §0 口径自检 ──────────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("§0 口径自检（三把 pin 都要逐位对上，否则后面数字不可信）")
    pre_s, pre_sh, pre_rows, _ = run(wins, None, walk_x=None)
    c43_s, c43_sh, c43_rows, _ = run(wins, None)
    new_s, new_sh, new_rows, _ = run(wins, FLOOR_X)
    ok_pre = len(pre_s) == PIN_PRE_SIG and abs(s38.pnl(pre_s) - PIN_PRE_PNL) < 1e-4
    ok_c43 = len(c43_s) == PIN_C43_SIG and abs(s38.pnl(c43_s) - PIN_C43_PNL) < 1e-4 \
        and c43_rows == PIN_C43_ROWS
    ok_new = len(new_s) == PIN_NEW_SIG and abs(s38.pnl(new_s) - PIN_NEW_PNL) < 1e-4 \
        and new_rows == PIN_NEW_ROWS and len(new_sh) == PIN_NEW_SHADOW
    print(f"  闸前基线（无闸）        {desc(pre_s)}  行 {pre_rows}   "
          f"pin {PIN_PRE_SIG} / +{PIN_PRE_PNL:.6f}U {'✅' if ok_pre else '❌'}")
    print(f"  现行 C43（只闸 T150）   {desc(c43_s)}  行 {c43_rows}   "
          f"pin {PIN_C43_SIG} / +{PIN_C43_PNL:.6f}U / 行 {PIN_C43_ROWS} {'✅' if ok_c43 else '❌'}")
    print(f"  落地形态（+地板 0.83）  {desc(new_s)}  行 {new_rows}  影子 {len(new_sh)}   "
          f"pin {PIN_NEW_SIG} / +{PIN_NEW_PNL:.6f}U / 行 {PIN_NEW_ROWS} / 影子 {PIN_NEW_SHADOW} "
          f"{'✅' if ok_new else '❌'}")

    BD_c43 = s38.by_day(c43_s)

    # ── §1 廉价口袋 ─────────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("§1 地板会碰到谁：现行 C43 下**监听段信号**的 fill 分布（n=372）")
    li = [r for r in c43_s if r["stage"] == "listen"]
    lit = sorted(li, key=lambda r: r["fill"])
    for lo, hi in ((0.80, 0.82), (0.83, 0.90), (0.91, 0.97), (0.98, 1.00)):
        g = [r for r in li if lo - 1e-9 <= r["fill"] <= hi + 1e-9]
        if not g:
            continue
        wl = sorted(r["walk"] for r in g if r["walk"] is not None)
        print(f"   fill [{lo:.2f},{hi:.2f}]  n={len(g):<4} 输率 "
              f"{sum(1 for r in g if not r['settle_won'])/len(g)*100:5.2f}%  "
              f"P&L {s38.pnl(g):+7.2f}U   walk 中位 {wl[len(wl)//2]:7.1f}")
    cheap = [r for r in li if r["fill"] <= FLOOR_X]
    print(f"   ⇒ 地板 >{FLOOR_X} 拦的是最左那格: n={len(cheap)}（占监听段 "
          f"{len(cheap)/len(li)*100:.1f}%）, 输 "
          f"{sum(1 for r in cheap if not r['settle_won'])} 笔, P&L {s38.pnl(cheap):+.2f}U")
    print(f"   ⚠️ 与 walk 的关系: 这 {len(cheap)} 笔里 walk 中位 "
          f"{sorted(r['walk'] for r in cheap)[len(cheap)//2]:.1f} 美元（对照全段 34.4）"
          f" ⇒ 廉价角与低 walk 同向但不是同一件事（ρ=+0.308, 见 40 §9）")

    print("\n   §1b 对照：**其它段**的廉价口袋（同样 `fill ≤ 0.83`）——为什么本决策只管监听段")
    for st in ("t150", "t60"):
        g = [r for r in c43_s if r["stage"] == st and r["fill"] <= FLOOR_X]
        if not g:
            continue
        wl = sorted(r["walk"] for r in g if r["walk"] is not None)
        print(f"     {st:<7} n={len(g):<4} 输率 "
              f"{sum(1 for r in g if not r['settle_won'])/len(g)*100:5.2f}%  "
              f"P&L {s38.pnl(g):+7.2f}U   walk 中位 {wl[len(wl)//2]:7.1f}")
    print("     ⚠️ 段 1 的廉价角大多已被 walk 闸（C43）先拦过一道; 段 2 是另一族决策"
          "（动它就是改检查点族, 见 docs/tail_entry_timing_2026-09-29.md）, "
          "本脚本只做登记、不提议。")

    # ── §2 阈值扫描 ─────────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("§2 阈值扫描（参照系 = 现行 C43；Δ = 日级配对 bootstrap 95%）")
    print("     X(严格大于)    n      Δ vs C43        95% 区间        影子行  净信号变化")
    for X in (0.80, 0.81, 0.82, 0.83, 0.85, 0.90):
        s_, sh_, r_, _ = run(wins, X)
        lo, hi = s38.boot_delta(BD_c43, s38.by_day(s_))
        star = "  ← 落地" if abs(X - FLOOR_X) < 1e-9 else ""
        print(f"     >{X:.2f}      {len(s_):<5} {s38.pnl(s_)-PIN_C43_PNL:+8.2f}U   "
              f"[{lo:+7.2f},{hi:+7.2f}]   {len(sh_):>4}   {len(s_)-PIN_C43_SIG:+4d}{star}")

    # ── §3 细账 ─────────────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print(f"§3 细账：地板 >{FLOOR_X} 拦下的 {len(new_sh)} 笔（无地板世界里的那些信号）")
    print("     date        rem  side  fill    walk     结果")
    for r in sorted(new_sh, key=lambda r: (r["date"], -r["rem"])):
        pnl_ = (s38.STAKE / r["fill"] - s38.STAKE) if r["settle_won"] else -s38.STAKE
        print(f"     {r['date']}  {r['rem']:>3}  {r['side']:<5} {r['fill']:.2f}  "
              f"{r['walk']:7.1f}   {'赢' if r['settle_won'] else '输'}（{pnl_:+.2f}U）")
    # 有影子行 ⇒ 该窗被拦: 地板世界里没信号 = 整窗死亡; 换了价 = 同段改道
    died = []
    reroute_pairs = []
    for w in wins:
        rows_n, info_n = chain(w, None)      # C43 世界
        rows_f, info_f = chain(w, FLOOR_X)   # 地板世界
        if not info_f["shadow"]:
            continue
        sig_n = [r for r in rows_n if r.get("ok")]
        sig_f = [r for r in rows_f if r.get("ok")]
        if not sig_f:
            died.append(sig_n[0])            # 整窗死亡（本窗再没有任何信号）
        elif sig_n and sig_f[0]["fill"] != sig_n[0]["fill"]:
            reroute_pairs.append((sig_n[0], sig_f[0]))
    print(f"\n     整窗死亡 {len(died)}：那批在 C43 世界里的结果 "
          f"— 输 {sum(1 for r in died if not r['settle_won'])}，"
          f"P&L {s38.pnl(died):+.2f}U（地板把它们省了）")
    print(f"     同段改道 {len(reroute_pairs)}：新 fill 中位 "
          f"{sorted(p[1]['fill'] for p in reroute_pairs)[len(reroute_pairs)//2]:.4f}"
          f"（旧 fill 中位 {sorted(p[0]['fill'] for p in reroute_pairs)[len(reroute_pairs)//2]:.4f}）")
    old_p = s38.pnl([p[0] for p in reroute_pairs])
    new_p = s38.pnl([p[1] for p in reroute_pairs])
    print(f"       这批窗的 P&L: {old_p:+.2f}U → {new_p:+.2f}U（改道税 {new_p-old_p:+.2f}U）")
    print(f"     ⇒ 净 Δ = 省下的 {abs(s38.pnl(died)):.2f}U − 改道税 "
          f"{abs(new_p-old_p):.2f}U = {s38.pnl(new_s)-PIN_C43_PNL:+.2f}U")
    # 与 walk 闸同款的分半检查（08-25 切点）
    h1 = [r for r in died if r["date"] < "2026-08-25"]
    h2 = [r for r in died if r["date"] >= "2026-08-25"]
    print(f"     分半（切点 08-25）: 整窗死亡 h1 {len(h1)} 窗（{s38.pnl(h1):+.2f}U）/ "
          f"h2 {len(h2)} 窗（{s38.pnl(h2):+.2f}U）"
          f" ⇒ 省下的钱 {'两半同号' if (not h1 or not h2 or (s38.pnl(h1) < 0) == (s38.pnl(h2) < 0)) else '**两半异号**'}")

    # ── §4 安慰剂 ───────────────────────────────────────────────────────
    nperm = args.nperm
    print("\n" + "=" * 100)
    print(f"§4 安慰剂：随机拦**同样笔数**的监听段信号（{nperm} 次）——按价格挑强不强于瞎挑")
    print("     ⚠️ 这是本脚本最重的一段（每次重跑整条链）: nperm=400 秒级, 4000 约 9 分钟")
    cand = []                                # C43 世界里监听段信号对应的 tick id
    for w in wins:
        _, info = chain(w, None)
        if info["sig"] and info["sig"][0] == "listen":
            cand.append((w, info["sig"][1]))
    print(f"     候选 = 现行 C43 的监听段信号 {len(cand)} 笔；每次随机取 "
          f"{len(new_sh)} 笔按下（拦下即链继续, 与地板语义一致）")
    rnd = random.Random(41)
    outs = []
    obs = s38.pnl(new_s) - PIN_C43_PNL
    for _ in range(nperm):
        pick = rnd.sample(cand, len(new_sh))
        blk = set(t[1] for t in pick)
        s_, _, _, _ = run(wins, None, blocked=blk)
        outs.append(s38.pnl(s_) - PIN_C43_PNL)
    outs.sort()
    pv = (sum(1 for v in outs if v >= obs) + 1) / (nperm + 1)
    print(f"     安慰剂 Δ 分布: 中位 {outs[nperm//2]:+.2f}U  p95 {outs[int(0.95*nperm)]:+.2f}U  "
          f"最大 {outs[-1]:+.2f}U")
    print(f"     实际 Δ = {obs:+.2f}U（>0 的安慰剂样本占 "
          f"{sum(1 for v in outs if v > 0)/nperm*100:.1f}%）")
    print(f"     ⇒ **按价格挑的 p = {pv:.4f}**"
          f"（{'过 0.05' if pv < 0.05 else '不过 0.05——样本太薄, 这正是要有前向复验的原因'}）"
          f" ［nperm={nperm}: p 的分辨率 ≈ {1/nperm:.4f}］")

    # ── §5 前向判据 ─────────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("§5 前向判据（先写死, 再跑——三条同时满足才扩实盘）")
    kept_li = [r for r in new_s if r["stage"] == "listen"]
    print(f"     ① 被拦组（floor_low 影子行那批）的输率 ≥ **监听段保留组** + 20pp"
          f"（回测: {sum(1 for r in new_sh if not r['settle_won'])/len(new_sh)*100:.1f}% vs "
          f"{sum(1 for r in kept_li if not r['settle_won'])/len(kept_li)*100:.2f}%"
          f"——⚠️ 被拦组那 15 笔的输率是在 **C43 世界**里读的（地板世界的监听段已不含它们））")
    print("     ② 用影子行现算的日级配对 Δ 的 95% **下界 > 0**")
    print(f"     ③ 净 Δ ≥ +10U / 14 天 @2U（回测现值 {obs:+.2f}U / 14 天）")
    print(f"     复验触发: ≥14 个完整日 或 ≥10 条 floor_low 影子行（先到为准）")
    print("     ⚠️ 影子行只落**首个**被拦 tick ⇒ 反事实恰好是「无地板世界成交的那一笔」。")
    print("     ⚠️ 影子行**行内没有 outcome**: Recorder.pending 按 conditionID 唯一（一窗")
    print("        挂不下两行）+ isSettlable 只认 ok 行 ⇒ 复验要**离线 join** outcome:")
    print("        同窗有信号行（15 笔里 11 笔）⇒ 由它的 won 反推; 整窗死亡（4 笔）⇒ 读")
    print("        runtime.events_dir 的采集行（condition_id 对齐, 行内有官方 outcome）。")


if __name__ == "__main__":
    main()
