#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫尾盘：价格地板**从 T=60 段开始**（2026-10-01 用户决定 = **决策 #33, 已落引擎**）的落地依据。

用户 2026-10-01 决定：「把 listen_min_price 提到从 T=60 开始」——依据是实盘观察：
09-30 那天 t60 段与监听段的 ≤0.83 成交把当日打亏（t60 0.81 一笔 −9.87U、监听
0.81/0.82 两笔 −19.99U），而 >0.83 的成交几乎全赢。同日稍晚实盘数据更新到 10-01
后重算：全样本 34 笔 ≤0.83 成交 22 赢 12 输 **−68.76U**，>0.83 的 516 笔 +98.87U。

本脚本把「地板下延到 t60 段」的代价与收益算清：

  §0 口径自检：C43 / #32（地板只在监听段）/ #33（落地面）三把 pin 逐位对上
  §1 #32 世界里 t60 段的廉价口袋（地板在 t60 会碰到谁）
  §2 变体：地板从 T=60 开始（t60 + 监听）——Δ vs #32、日级配对 95%
  §3 细账：被 t60 地板拦下的笔 → 整窗死亡 vs 改道
  §4 实盘对照（data/tail-live，10U/注）
  §5 前向判据（先写死，再跑）

口径与 23（oracle）/ 38 / 41 逐位一致：宇宙 = `s38.universe()`（可判定 tick ∧ 有 σ）;
σ 按事件索引现算; T=150 价格腿严格大于; walk 闸恒为 C43（决策 #29 只闸段 1）。
本脚本的链 = 41 的 chain + t60 段地板一步（`fill <= X` ⇒ 落影子行且链继续）。
⚠️ 回测上 #33 的 Δ 是**显著为负**的（−2.26U, 日级配对 95% [−4.73,−0.29]）——
落地依据是实盘口径 + 用户决定, 不是回测增益; 详见 docs/tail_floor_from_t60_2026-10-01.md。

用法: python/venv/bin/python python/v4/42_tail_floor_from_t60.py
"""

import sys
import json
import glob
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
WALK_X = s23.WALK_MIN_USD          # 43：T150 入场闸（决策 #29）
FLOOR_X = s23.FLOOR_MIN_PRICE      # 0.83：价格地板阈值（oracle 常量同日随决策 #33 改名）

# 三把 pin（oracle 23 / 41 打印的值; #33 那把 = 23 的现行 pin）
PIN_C43_SIG, PIN_C43_PNL, PIN_C43_ROWS = 2080, 64.026354, 6686
PIN_32_SIG, PIN_32_PNL, PIN_32_ROWS, PIN_32_SHADOW = 2076, 72.578473, 6697, 15
PIN_33_SIG, PIN_33_PNL, PIN_33_ROWS, PIN_33_SHADOW = 2074, 70.314372, 6704, 24


# ── 链：三段递进 + T150 walk 闸 + 价格地板（floor_from 决定从哪段开始）──────

def chain(w, floor_from=None):
    """返回 (rows, info)。floor_from ∈ {None, "listen", "t60"}。

    None      = 无地板（复现 C43）
    "listen"  = 现行落地面（决策 #32: 只拦监听段）
    "t60"     = 本脚本要论证的形态（t60 段 + 监听段都拦; t150 一字不动）

    ⚠️ 影子行闩锁**跨段共享**（与引擎的 floorShadowed 同口径）: 影子行的用途是
    「无地板世界里成交的那一笔」的离线反事实——t60 被拦后链继续到监听段, 若监听段
    再落一条, 反事实就有两条、读不出来。
    """
    ticks, anchor, sd, date, outcome = (w["ticks"], w["anchor"], w["sd"],
                                        w["date"], w["outcome"])
    if not ticks:
        return [], {"shadow": [], "sig": None}
    rows, info = [], {"shadow": [], "sig": None}
    shadowed = False

    head = ticks[0]
    if head["rem"] > T60:
        r = s23.row(head, anchor, sd, date, outcome, "t150")
        ok5 = s23.r5(r, strict_price=True)          # 段 1 价格腿严格大于（#26）
        if ok5 and s23.walk_ok(r, WALK_X):
            r["ok"] = True
            rows.append(r)
            info["sig"] = ("t150", id(head))
            return rows, info
        rows.append(r)
        rest = [x for x in ticks if x["rem"] <= T60]
    else:
        rest = ticks                                # 迟到接入: 不伪造 t150 行

    if rest:
        r2 = s23.row(rest[0], anchor, sd, date, outcome, "t60")
        if s23.r5(r2):
            # 地板从 T=60 开始: ⑤ 达标但 fill ≤ X ⇒ 本 tick 本会是信号, 被拦下。
            # 落一行影子行（stage=t60, reject_reason=floor_low）且**链继续**到监听段。
            if floor_from == "t60" and r2["fill"] <= FLOOR_X:
                r2["reject_reason"] = "floor_low"
                info["shadow"].append(r2)
                rows.append(r2)
                shadowed = True
            else:
                r2["ok"] = True
                rows.append(r2)
                info["sig"] = ("t60", id(rest[0]))
                return rows, info
        else:
            rows.append(r2)
        for x in rest[1:]:
            rl = s23.row(x, anchor, sd, date, outcome, "listen")
            if not s23.r2(rl):
                continue
            cut = floor_from is not None and rl["fill"] <= FLOOR_X
            if cut:
                if not shadowed:                    # 影子行每窗至多一条
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


def run(wins, floor_from=None):
    sigs, shadows, nrows = [], [], 0
    wid = 0
    for w in wins:
        wid += 1
        rows, info = chain(w, floor_from)
        nrows += len(rows)
        for r in rows:
            r["wid"] = wid
            r["walk0"] = s38.walk_of(w["ticks"][0], w["anchor"])
            if r.get("ok"):
                sigs.append(r)
        shadows.extend(info["shadow"])
    return sigs, shadows, nrows


def desc(sigs):
    n = len(sigs)
    wr = sum(r["settle_won"] for r in sigs) / n * 100
    return (f"n={n:<5} WR {wr:6.2f}%  输 {sum(1 for r in sigs if not r['settle_won']):<3} "
            f"P&L {s38.pnl(sigs):+8.2f}U")


def main():
    wins = s38.universe()
    print(f"宇宙：{len(wins)} 窗（pin 3640 有可判定 tick ∧ 有 σ 的窗）")

    # ── §0 口径自检 ──────────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("§0 口径自检（三把 pin 都要逐位对上，否则后面数字不可信）")
    c43_s, c43_sh, c43_rows = run(wins, None)
    now32_s, now32_sh, now32_rows = run(wins, "listen")
    now33_s, now33_sh, now33_rows = run(wins, "t60")
    ok_c43 = len(c43_s) == PIN_C43_SIG and abs(s38.pnl(c43_s) - PIN_C43_PNL) < 1e-4 \
        and c43_rows == PIN_C43_ROWS
    ok_32 = len(now32_s) == PIN_32_SIG and abs(s38.pnl(now32_s) - PIN_32_PNL) < 1e-4 \
        and now32_rows == PIN_32_ROWS and len(now32_sh) == PIN_32_SHADOW
    ok_33 = len(now33_s) == PIN_33_SIG and abs(s38.pnl(now33_s) - PIN_33_PNL) < 1e-4 \
        and now33_rows == PIN_33_ROWS and len(now33_sh) == PIN_33_SHADOW
    print(f"  C43（只闸 T150）            {desc(c43_s)}  行 {c43_rows}   "
          f"pin {PIN_C43_SIG} / +{PIN_C43_PNL:.6f}U / 行 {PIN_C43_ROWS} {'✅' if ok_c43 else '❌'}")
    print(f"  #32（地板只拦监听段）       {desc(now32_s)}  行 {now32_rows}  影子 {len(now32_sh)}   "
          f"pin {PIN_32_SIG} / +{PIN_32_PNL:.6f}U / 行 {PIN_32_ROWS} / 影子 {PIN_32_SHADOW} "
          f"{'✅' if ok_32 else '❌'}")
    print(f"  #33（地板拦 t60+监听, 落地）{desc(now33_s)}  行 {now33_rows}  影子 {len(now33_sh)}   "
          f"pin {PIN_33_SIG} / +{PIN_33_PNL:.6f}U / 行 {PIN_33_ROWS} / 影子 {PIN_33_SHADOW} "
          f"{'✅' if ok_33 else '❌'}")

    BD_now = s38.by_day(now32_s)     # 参照系 = #32 世界（地板下延要跟它比）

    # ── §1 t60 段的廉价口袋（#32 世界里）──────────────────────────────────
    print("\n" + "=" * 100)
    print(f"§1 地板下延到 t60 会碰到谁：#32 世界里 **t60 段信号**的 fill ≤ {FLOOR_X} 明细")
    t60_all = [r for r in now32_s if r["stage"] == "t60"]
    cheap_t60 = sorted([r for r in t60_all if r["fill"] <= FLOOR_X], key=lambda r: r["date"])
    print(f"  t60 段信号合计 n={len(t60_all)}，其中 fill ≤ {FLOOR_X} 的 n={len(cheap_t60)}"
          f"（{len(cheap_t60)/len(t60_all)*100:.1f}%）")
    print("     date        rem  side  fill    walk     结果")
    for r in cheap_t60:
        pnl_ = (STAKE / r["fill"] - STAKE) if r["settle_won"] else -STAKE
        print(f"     {r['date']}  {r['rem']:>3}  {r['side']:<5} {r['fill']:.2f}  "
              f"{r['walk']:7.1f}   {'赢' if r['settle_won'] else '输'}（{pnl_:+.2f}U）")
    if cheap_t60:
        print(f"     ⇒ 这一格: 输 {sum(1 for r in cheap_t60 if not r['settle_won'])} 笔, "
              f"P&L {s38.pnl(cheap_t60):+.2f}U（对照 t60 全段 {s38.pnl(t60_all):+.2f}U）")

    # ── §2 变体：地板从 T=60 开始（= 决策 #33, 已落地）────────────────────
    print("\n" + "=" * 100)
    print("§2 变体：地板从 T=60 开始（t60 + 监听都拦; t150 一字不动）——2026-10-01 已落引擎 = 决策 #33")
    new_s, new_sh, new_rows = run(wins, "t60")
    lo, hi = s38.boot_delta(BD_now, s38.by_day(new_s))
    print(f"  新形态（落地面）        {desc(new_s)}  行 {new_rows}  影子 {len(new_sh)}")
    print(f"  Δ vs #32               {s38.pnl(new_s)-PIN_32_PNL:+8.2f}U   日级配对 95% "
          f"[{lo:+7.2f},{hi:+7.2f}]")
    print(f"  Δ vs C43               {s38.pnl(new_s)-PIN_C43_PNL:+8.2f}U")
    print("  ⚠️ 区间**整体为负**（不含 0）⇒ 回测上这是**显著变差**的一改;")
    print("     落地依据是实盘口径 + 用户决定（前向判据见 §5 与 docs/tail_floor_from_t60_2026-10-01.md）。")

    # ── §3 细账 ─────────────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print(f"§3 细账：t60 地板拦下的 {len([r for r in new_sh if r['stage'] == 't60'])} 笔"
          "（无地板世界里的 t60 信号）")
    died, reroute_pairs = [], []
    for w in wins:
        rows_n, info_n = chain(w, "listen")     # #32 世界（地板只在监听段）
        rows_f, info_f = chain(w, "t60")        # 新世界（#33）
        if not any(r["stage"] == "t60" for r in info_f["shadow"]):
            continue                            # 本窗没被 t60 地板碰
        sig_n = [r for r in rows_n if r.get("ok")]
        sig_f = [r for r in rows_f if r.get("ok")]
        if not sig_f:
            died.append(sig_n[0])
        elif sig_n and sig_f[0]["fill"] != sig_n[0]["fill"]:
            reroute_pairs.append((sig_n[0], sig_f[0]))
    if died:
        print(f"  整窗死亡 {len(died)}：那批在 #32 世界里的结果 — 输 "
              f"{sum(1 for r in died if not r['settle_won'])}，P&L {s38.pnl(died):+.2f}U")
    else:
        print("  整窗死亡 0")
    if reroute_pairs:
        old_p = s38.pnl([p[0] for p in reroute_pairs])
        new_p = s38.pnl([p[1] for p in reroute_pairs])
        print(f"  同段改道 {len(reroute_pairs)}：新 fill 中位 "
              f"{sorted(p[1]['fill'] for p in reroute_pairs)[len(reroute_pairs)//2]:.4f}"
              f"（旧 fill 中位 "
              f"{sorted(p[0]['fill'] for p in reroute_pairs)[len(reroute_pairs)//2]:.4f}）")
        print(f"     这批窗 P&L: {old_p:+.2f}U → {new_p:+.2f}U（改道税 {new_p-old_p:+.2f}U）")
        dead_dates = collections.Counter(r["date"] for r in died)
        rer_dates = collections.Counter(p[0]["date"] for p in reroute_pairs)
        print(f"     死亡日: {dict(sorted(dead_dates.items()))}")
        print(f"     改道日: {dict(sorted(rer_dates.items()))}")

    # ── §4 实盘对照 ─────────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("§4 实盘对照（data/tail-live，09-24~10-01，10U/注；只算实际成交 filled/partial）")
    lv = collections.defaultdict(lambda: {"n": 0, "w": 0, "l": 0, "p": 0, "pnl": 0.0, "rows": []})
    for f in sorted(glob.glob("data/tail-live/tail_2026-*.jsonl")):
        day = f[-15:-6]
        for ln in open(f):
            ln = ln.strip()
            if not ln:
                continue
            try:
                d = json.loads(ln)
            except Exception:
                continue
            if d.get("kind") != "snap" or not d.get("ok"):
                continue
            if (d.get("exec_status") or "") not in ("filled", "partial"):
                continue
            k = (d.get("stage"), "≤0.83" if (d.get("hot_ask") or 0) <= FLOOR_X else ">0.83")
            e = lv[k]
            e["n"] += 1
            # ⚠️ won 三态: True/False/None（None = 尚未定案, 别并进「输」——它会把
            # 尚未结算的那一笔读成亏损）。已定案的行才进 W/L, 未定案的单独计数。
            if d.get("won") is True:
                e["w"] += 1
            elif d.get("won") is False:
                e["l"] += 1
            else:
                e["p"] += 1
            e["pnl"] += d.get("pnl") or 0
            if k[1] == "≤0.83":
                e["rows"].append((day, d.get("rem"), d.get("fill") or d.get("hot_ask"),
                                  d.get("won"), d.get("pnl")))
    for stage in ("t150", "t60", "listen"):
        for bucket in ("≤0.83", ">0.83"):
            e = lv.get((stage, bucket))
            if not e:
                continue
            pend = f"  待定 {e['p']}" if e["p"] else ""
            print(f"  {stage:<7} {bucket:<6} n={e['n']:<3} W/L={e['w']}/{e['l']:<3} "
                  f"P&L {e['pnl']:+8.4f}U{pend}")
        for row in sorted(lv.get((stage, "≤0.83"), {"rows": []})["rows"]):
            print(f"        {row[0]} rem={row[1]:>3} fill={row[2]:.2f} "
                  f"{'赢' if row[3] else ('输' if row[3] is False else '待定')} {row[4]:+.2f}U")
    print("  ⚠️ 实盘 t60 的 ≤0.83 成交 6 笔（09-29 起）: 4 赢 2 输 **−11.38U**"
          "（数据更新到 10-01）——样本仍薄, 不能单独定案;")
    print("     但把三段合起来看, 廉价口袋（≤0.83）是实盘里唯一系统性输钱的那一格:"
          " 34 笔 22 赢 12 输 −68.76U, >0.83 的 516 笔 +98.87U。")

    # ── §5 前向判据 ─────────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("§5 前向判据（先写死, 再跑——三条同时满足才继续留着; 判据原文见")
    print("   docs/tail_floor_from_t60_2026-10-01.md §5）")
    print("     ① 被拦组（t60 段 floor_low 影子行那批）的输率 ≥ t60 保留组 + 20pp")
    print("     ② 用影子行现算的日级配对 Δ 的 95% 下界 > 0")
    print("     ③ 净 Δ ≥ +10U / 14 天 @2U（⚠️ 与前两条一起, 要的是「实盘口径下地板确实省钱」）")
    print("     复验触发: ≥14 个完整日（自 2026-10-01 起）或 ≥10 条 t60 段 floor_low 影子行（先到为准）")


if __name__ == "__main__":
    main()
