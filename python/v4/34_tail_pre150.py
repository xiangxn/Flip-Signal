#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫尾盘: 把 `价格≥0.80 ∧ dev > 2·sd ∧ sd > 63` 这条检查**提前到 T150 之前**
（2026-09-29 用户提问; 上一轮 = 脚本 33 把它放在 T150→T60 的空窗里）

题面（上一轮结论的直接推论）:

    三段递进判定链**一字不动**, 但在它的**前面**再加一段 `pre`:
    **从窗口开局起逐秒**（rem > 150, 时间顺序）查 价格腿 ≥ 0.80 ∧ dev > 2·sd ∧ sd > 63,
    达标即下单（stage = pre）; 整段不达标则原样走现行三段链（T=150 → T=60 → 监听）。
    已经出信号的窗天然跳过（链在首个信号处终止）。

为什么值得一试（上一轮的发现）: 这条条件在空窗里命中 30 笔, 但其中 18 笔的成交价与基线
在 T=60 段拿到的**完全相同**（都是 0.99）——即「条件成立的时候市场早就定局了」。
把它再往前提（rem 150~295）就是问: **更早的时候, 条件成立而价格还没顶格吗?**

本脚本的关键是**诊断段**（第五节）: 不看价格腿, 直接找「dev 首次 > 2·sd 且 sd > 63」的
那一刻, 看热门侧价格是多少 —— 这才回答得了「提前到底能不能买到便宜」。

符号（全脚本统一, 与 33 同）:
    dev = sgn·(spot − anchor), 单位美元, 正 = 朝押注方向（押 yes +1 / 押 no −1）
    sd  = 该窗 1σ 的美元值 = hist_bps × anchor / 1e4
    sig = dev / sd（位移等于几个 σ）
    EV/注 = 每注（2U）的期望盈亏, 单位 U（1U = 1 美元）

其余口径与 `23_tail_integrated.py` 逐条同源（可判定 tick / σ 未就绪整窗跳过 /
fill = 下单 tick 热门侧有效价 / shares = 2/fill）; pre 段的 tick 宇宙 = 同一套可判定
tick, 只是时间窗取 `rem > 150`（与 23 的 `rem ≤ 150` 不重叠, 两段互斥且合并 = 全窗口）。

用法: python/venv/bin/python python/v4/34_tail_pre150.py
"""
import sys
import random
import datetime
import collections
import importlib.util
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))
from v2.lib import load_events                                    # noqa: E402

MAX_LAT = 300
T150 = 150
FIELDS = ("yes_bid", "yes_ask", "no_bid", "no_ask")

K_NEW = 2.0            # 题面: dev > 2·sd
SD_NEW = 63.0          # 题面: sd > 63 美元
PRE_MAX = 295          # pre 区的时间上界（295 ≈ 窗口开局那一秒; 见第三节扫描）
STAGES = ("pre", "t150", "gap", "t60", "listen")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


s13 = load("s13", BASE / "13_tail_sweep.py")
s23 = load("s23", BASE / "23_tail_integrated.py")
s33 = load("s33", BASE / "33_tail_dev2sd.py")     # 复用三段链 / pl / 配对 bootstrap

STAKE = s33.STAKE
pl = s33.pl
line = s33.line
paired_ci = s33.paired_ci
leg_new = s33.leg_new
price_leg = s33.price_leg

BASE_RULES = s33.BASE_RULES                        # 现行 ⑤(严格价) / ⑤ / ②


# ── tick 提取: rem > 150 的那一半（宇宙与 23.win_ticks 逐条同源）──────────

def pre_ticks(e):
    """本窗 `rem > 150` 的可判定 tick（时间顺序 = rem 递减）。

    与 23.win_ticks 唯一的差别是时间窗: 那边收 rem ≤ 150, 这边收 rem > 150。
    两段互斥、并集 = 窗口里全部可判定 tick（parity 审计据此对账, 见第七节）。
    """
    out = []
    if not e.get("twap_open_price"):
        return out
    for x in e.get("ticks") or []:
        p = x.get("pm") or {}
        if not p or (p.get("book_latency_ms") or 0) > MAX_LAT:
            continue
        rem = x.get("rem")
        if rem is None or rem <= T150:
            continue
        if not all((p.get(k) or 0) > 0 for k in FIELDS):
            continue
        spot = (x.get("bin") or {}).get("price")
        if not spot:
            continue
        ya, na = p.get("yes_ask") or 0, p.get("no_ask") or 0
        side = "yes" if ya >= na else "no"        # 平局取 yes（与 Go HotBook 同）
        out.append({"rem": rem, "side": side,
                    "fill": (ya if side == "yes" else na),
                    "spot": spot, "twap": (x.get("twap") or {}).get("price")})
    return out


# ── 链: pre 段 + 现行三段链 ───────────────────────────────────────────────

def rule_pre(k=K_NEW, sd_min=SD_NEW, strict=False):
    """pre 段判据 = 价格腿 ∧ dev > k·sd ∧ sd > sd_min。"""
    return lambda r: price_leg(r, strict) and leg_new(r, k, sd_min)


def chain_pre(pre, ticks, anchor, sd, date, outcome, pre_rule,
              pre_max=PRE_MAX, gap_rule=None, gap_max=T150):
    """pre 段（rem ∈ (150, pre_max], 时间顺序逐秒）+ 现行三段链。

    返回 (信号行 或 None, {'pre': n, 'gap': n}) —— 各新增段实际判定过的 tick 数。
    """
    n = {"pre": 0, "gap": 0}
    if pre_rule is not None:
        for x in pre:                              # 时间顺序: rem 从大到小
            if x["rem"] > pre_max:
                continue                           # 比 pre_max 更早的 tick 不查
            rp = s23.row(x, anchor, sd, date, outcome, "pre")
            n["pre"] += 1
            if pre_rule(rp):
                rp["ok"] = True
                return rp, n
    r, ng = s33.chain(ticks, anchor, sd, date, outcome, BASE_RULES, gap_rule, gap_max)
    n["gap"] = ng
    return r, n


def run(ev, hr, pre_rule=None, pre_max=PRE_MAX, gap_rule=None, gap_max=T150):
    """返回 (信号行列表, 逐窗映射, pre 段判定 tick 数, 空窗判定 tick 数)。"""
    sigs, by_win, npre, ngap = [], {}, 0, 0
    for e in ev:
        outcome, anchor = e.get("outcome"), e.get("twap_open_price")
        if outcome is None or not anchor:
            continue
        h = hr.get(e["start_time"])
        if h is None:
            continue                               # σ 未就绪整窗跳过（决策 #13）
        sd = h / anchor * 1e4 * anchor / 1e4
        date = datetime.datetime.fromtimestamp(
            e["start_time"], datetime.timezone.utc).strftime("%Y-%m-%d")
        ticks = s23.win_ticks(e)
        pre = pre_ticks(e)
        if not ticks and not pre:
            continue
        r, n = chain_pre(pre, ticks, anchor, sd, date, outcome, pre_rule,
                         pre_max, gap_rule, gap_max)
        npre += n["pre"]
        ngap += n["gap"]
        if r is not None:
            r["win"] = e["start_time"]
            sigs.append(r)
            by_win[e["start_time"]] = r
    return sigs, by_win, npre, ngap


def main():
    ev = load_events("data/btc")
    hr = s13.hist_ranges(ev)
    rng = random.Random(s33.SEED)

    base_sigs, base_map, _, _ = run(ev, hr)
    PRE = rule_pre()
    pre_sigs, pre_map, npre, _ = run(ev, hr, pre_rule=PRE)
    # 上一轮的题面（空窗检查）也一起算, 用于第八节对照
    gap_sigs, gap_map, _, ngap = run(ev, hr, gap_rule=s33.rule_gap())
    both_sigs, both_map, _, _ = run(ev, hr, pre_rule=PRE, gap_rule=s33.rule_gap())

    print("=" * 108)
    print("一、题面：三段链**之前**逐秒查（rem > 150）价格≥0.80 ∧ dev>2·sd ∧ sd>63")
    print("=" * 108)
    line("基线（现行三段链）", base_sigs, rng)
    line("题面（pre 段 + 三段链）", pre_sigs, rng)
    p = paired_ci(base_map, pre_map, rng)
    print(f"\n  配对 Δ（题面 − 基线）P&L: 点估计 {pl(pre_sigs)-pl(base_sigs):+.2f}U, "
          f"日级 bootstrap 95%CI [{p[0]:+.1f}, {p[1]:+.1f}]U")
    print(f"  pre 段里实际判定过的 tick 总数为 {npre}"
          f"（每窗最多 ~148 个 = rem 295→150 的秒数）")
    print("\n  按段分解（题面这一列的信号落在哪一段）:")
    for s in STAGES:
        rowsl = [r for r in pre_sigs if r["stage"] == s]
        if rowsl or s == "pre":
            line(f"    {s}", rowsl, rng, width=22)

    # ── 二、新增 / 改道 ─────────────────────────────────────────────────
    print("\n" + "=" * 108)
    print("二、分解：pre 段抢跑入场的（改道）vs 基线整窗本来不下注的（新增）")
    print("=" * 108)
    moved, added = [], []
    for w, v in pre_map.items():
        if v["stage"] != "pre":
            continue
        b = base_map.get(w)
        (moved.append((b, v)) if b else added.append(v))
    print(f"  改道 {len(moved)} 笔（基线也有信号, 只是入场点被提前）; "
          f"新增 {len(added)} 笔（基线整窗无信号）")
    print(f"  {'组':<12}{'n':>6}{'基线P&L':>10}{'变体P&L':>10}{'ΔP&L':>10}"
          f"{'基线均价':>10}{'变体均价':>10}{'基线WR':>9}{'变体WR':>9}")
    for lab, rows, has_base in (("改道", moved, True), ("新增", added, False)):
        v = [x[1] for x in rows] if has_base else rows
        if not v:
            continue
        b = [x[0] for x in rows] if has_base else []
        bl = pl(b) if has_base else 0.0
        bwr = (sum(r["settle_won"] for r in b) / len(b) * 100) if has_base else 0.0
        bpx = (sum(r["fill"] for r in b) / len(b)) if has_base else 0.0
        vwr = sum(r["settle_won"] for r in v) / len(v) * 100
        print(f"  {lab:<12}{len(v):>6}{bl:>+10.2f}{pl(v):>+10.2f}{pl(v)-bl:>+10.2f}"
              f"{bpx:>10.4f}{sum(r['fill'] for r in v)/len(v):>10.4f}"
              f"{bwr:>8.1f}%{vwr:>8.1f}%")
    win2loss = sum(1 for b, v in moved if b["settle_won"] and not v["settle_won"])
    loss2win = sum(1 for b, v in moved if not b["settle_won"] and v["settle_won"])
    newloss = sum(1 for r in added if not r["settle_won"])
    print(f"  改道里 赢→输 {win2loss} 笔 / 输→赢 {loss2win} 笔; 新增里输 {newloss} 笔"
          f"（新增赢 {len(added)-newloss} 笔）")

    # ── 三、时间范围扫描 ────────────────────────────────────────────────
    print("\n" + "=" * 108)
    print("三、稳健性 A: **允许入场的最早时刻**（题面 K=2 / sd>63; 段下界固定 rem=150, 只改上界)")
    print("=" * 108)
    print(f"  {'pre 区上界':<26}{'总n':>7}{'总P&L':>10}{'pre段n':>9}{'pre段P&L':>11}"
          f"{'pre段均价':>11}{'pre段EV/注':>12}{'配对Δ 95%CI':>20}")
    for lab, pmax, rule in (("rem ≤ 295（窗口开局）", 295, PRE),
                            ("rem ≤ 240", 240, PRE),
                            ("rem ≤ 200", 200, PRE),
                            ("rem ≤ 175", 175, PRE),
                            ("rem ≤ 160", 160, PRE),
                            ("rem ≤ 295, 价格 > 0.80", 295, rule_pre(strict=True))):
        sigs, mp, _, _ = run(ev, hr, pre_rule=rule, pre_max=pmax)
        ps = [r for r in sigs if r["stage"] == "pre"]
        pc = paired_ci(base_map, mp, rng)
        pct = f"[{pc[0]:+.1f},{pc[1]:+.1f}]U" if pc else "—"
        if not ps:
            print(f"  {lab:<26}{len(sigs):>7}{pl(sigs):>+10.2f}{0:>9}{'—':>11}"
                  f"{'—':>11}{'—':>12}{pct:>20}")
            continue
        print(f"  {lab:<26}{len(sigs):>7}{pl(sigs):>+10.2f}{len(ps):>9}{pl(ps):>+11.2f}"
              f"{sum(r['fill'] for r in ps)/len(ps):>11.4f}{pl(ps)/len(ps):>+12.4f}"
              f"{pct:>20}")
    print("  （`上界` = 允许入场的最早时刻: 295 = 窗口开局起就允许按条件提前入场（= 题面）;")
    print("    175 = 只有条件撑到 T150 前最后 25 秒才入场 ⇒ 上界越小 = 越不提前, 也就越像基线）")

    # ── 四、阈值网格 ────────────────────────────────────────────────────
    print("\n" + "=" * 108)
    print("四、稳健性 B: 判据阈值网格（单元格 = 总 P&L U / pre 段 n / pre 段 EV每注U）")
    print("=" * 108)
    print("  " + "sd下限".ljust(10) + "".join(f"K={k:g}".ljust(24)
                                           for k in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0)))
    for sd_min in (0.0, 40.0, 50.0, 63.0, 75.0, 90.0):
        cells = []
        for k in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0):
            sigs, _, _, _ = run(ev, hr, pre_rule=rule_pre(k, sd_min))
            ps = [r for r in sigs if r["stage"] == "pre"]
            tot = pl(sigs)
            if not ps:
                cells.append(f"{tot:+7.1f}/0/—".ljust(24))
                continue
            cells.append(f"{tot:+7.1f}/{len(ps):>4}/{pl(ps)/len(ps):+.3f}".ljust(24))
        tag = "  ←题面" if sd_min == SD_NEW else ""
        print("  " + f"{sd_min:g}".ljust(10) + "".join(cells) + tag)
    _sds = sorted(r["sd"] for r in base_sigs)
    print(f"  （基线信号 sd 中位 = {_sds[len(_sds)//2]:.1f} 美元 ⇒ 63 这一档 ≈ 波动高于中位;"
          f" 基线总 P&L = {pl(base_sigs):+.2f}U）")

    # ── 五、诊断: 条件成立时价格是多少（不看价格腿）────────────────────────
    print("\n" + "=" * 108)
    print("五、诊断：pre 区里「dev 首次 > 2·sd ∧ sd > 63」那一刻, 热门侧价格是多少？")
    print("=" * 108)
    first_hit, blocked = [], []        # (行, 该窗首个价格腿达标的 tick) / 一直没到价
    hk = {}                            # start_time → {cand, ok, persist}
    n_legonly = n_legonly_hit = 0
    win_all = 0
    for e in ev:
        outcome, anchor = e.get("outcome"), e.get("twap_open_price")
        if outcome is None or not anchor:
            continue
        h = hr.get(e["start_time"])
        if h is None:
            continue
        sd = h / anchor * 1e4 * anchor / 1e4
        date = datetime.datetime.fromtimestamp(
            e["start_time"], datetime.timezone.utc).strftime("%Y-%m-%d")
        pre = pre_ticks(e)
        if not pre:
            continue
        win_all += 1
        cand, ci_ = None, -1
        for i, x in enumerate(pre):                    # 时间顺序
            r = s23.row(x, anchor, sd, date, outcome, "pre")
            if leg_new(r):
                cand, ci_ = r, i
                break
        if cand is None:
            continue
        n_legonly += 1
        # 条件成立**那一点及之后**（时间顺序）第一个价格腿达标的 tick
        ok_tick = next((s23.row(x, anchor, sd, date, outcome, "pre")
                        for x in pre[ci_:] if x["fill"] >= 0.80), None)
        # 位移是否**延续**到 T150 前最后 25 秒（rem ∈ (150,175] 仍满足 dev > 2·sd）
        # ⚠️ 这是「后来才知道」的信息, 只用于描述, 不是可交易判据
        hk[e["start_time"]] = {
            "cand": cand, "persist": any(leg_new(s23.row(x, anchor, sd, date, outcome, "pre"))
                                         for x in pre if 150 < x["rem"] <= 175)}
        if ok_tick is not None:
            n_legonly_hit += 1
            first_hit.append((cand, ok_tick))
        else:
            blocked.append(cand)
    print(f"  有 pre 段可判定 tick 的窗: {win_all}")
    print(f"  其中「dev > 2·sd ∧ sd > 63」在 pre 区成立过的窗: {n_legonly}"
          f"（占 {n_legonly/win_all*100:.1f}%）")
    if n_legonly:
        fl = sorted(x["fill"] for x, _ in first_hit) + sorted(x["fill"] for x in blocked)
        print(f"  条件成立的**那一刻**价格: 中位 {fl[len(fl)//2]:.3f}"
              f"（p10 {fl[len(fl)//10]:.3f} / p90 {fl[9*len(fl)//10]:.3f}）;"
              f"  ≥ 0.80 的 {n_legonly_hit} 窗, < 0.80 的 {len(blocked)} 窗")
        dr = sorted(k["rem"] - x["rem"] for x, k in first_hit)
        if dr:
            print(f"  价格腿达标（≥0.80）距条件成立: 中位 {dr[len(dr)//2]:.0f} 秒"
                  f"（p10 {dr[len(dr)//10]:.0f} / p90 {dr[9*len(dr)//10]:.0f}）")
            diff = sorted(x["fill"] - k["fill"] for x, k in first_hit)
            print(f"  那段等待里价格又涨了中位 {-(diff[len(diff)//2]):.4f}"
                  f"（条件成立 {sum(x['fill'] for x, _ in first_hit)/len(first_hit):.4f} → "
                  f"达标时 {sum(k['fill'] for _, k in first_hit)/len(first_hit):.4f}）")
        if blocked:
            rb = sorted(x["rem"] for x in blocked)
            print(f"  「条件成立但价格始终 < 0.80」的 {len(blocked)} 窗: rem 中位 "
                  f"{rb[len(rb)//2]:.0f} ⇒ 价格腿没放行, 这 {len(blocked)} 窗一律落回原三段链")

    # ── 六、机制: pre 抢跑把入场提前了多少 ───────────────────────────────
    print("\n" + "=" * 108)
    print("六、机制: pre 抢跑把入场提前了多少（改道那批 vs 基线同窗）")
    print("=" * 108)
    if moved:
        dr = [v["rem"] - b_["rem"] for b_, v in moved]      # 正 = pre 更早（rem 更大）
        dp = [b_["fill"] - v["fill"] for b_, v in moved]
        print(f"  改道 {len(moved)} 笔: 入场提前中位 {sorted(dr)[len(dr)//2]:.0f} 秒"
              f"（p10 {sorted(dr)[len(dr)//10]:.0f} / p90 {sorted(dr)[9*len(dr)//10]:.0f}）;"
              f" 价格便宜中位 {sorted(dp)[len(dp)//2]:+.4f}"
              f"（均价 {sum(x[0]['fill'] for x in moved)/len(moved):.4f} → "
              f"{sum(x[1]['fill'] for x in moved)/len(moved):.4f}）")
        print("  改道里基线原先落在: " + ", ".join(
            f"{s} {sum(1 for x in moved if x[0]['stage']==s)} 笔" for s in STAGES
            if any(x[0]["stage"] == s for x in moved)))
        print(f"  逐笔 Δ（pre − 基线）: 正 "
              f"{sum(1 for b_, v in moved if pl([v]) - pl([b_]) > 1e-9)} 笔, "
              f"负 {sum(1 for b_, v in moved if pl([v]) - pl([b_]) < -1e-9)} 笔, "
              f"平 {sum(1 for b_, v in moved if abs(pl([v]) - pl([b_])) <= 1e-9)} 笔")
        # 改道子集自身的配对 CI（只拿这 208 个窗的基线/variant 两边）
        mw = set(v["win"] for _b, v in moved)
        pm = paired_ci({w: r for w, r in base_map.items() if w in mw},
                       {w: r for w, r in pre_map.items() if w in mw}, rng)
        if pm:
            print(f"  改道子集自身的配对 Δ 95%CI: [{pm[0]:+.1f}, {pm[1]:+.1f}]U"
                  f"（点估计 {sum(pl([v])-pl([b_]) for b_, v in moved):+.2f}U）")
        flips = [(b_, v) for b_, v in moved if b_["settle_won"] and not v["settle_won"]]
        print(f"\n  赢→输 的 {len(flips)} 笔（pre 提前入场把赢单变输单的那些）:")
        print(f"    {'日期':<12}{'pre rem':>8}{'pre价':>8}{'基线段':>8}{'基线rem':>8}{'基线价':>8}")
        for b_, v in sorted(flips, key=lambda x: (x[1]["date"], -x[1]["rem"])):
            print(f"    {v['date']:<12}{v['rem']:>8}{v['fill']:>8.4f}"
                  f"{b_['stage']:>8}{b_['rem']:>8}{b_['fill']:>8.4f}")
    if added:
        print(f"  新增 {len(added)} 笔: 均价 "
              f"{sum(r['fill'] for r in added)/len(added):.4f}, "
              f"WR {sum(r['settle_won'] for r in added)/len(added)*100:.1f}%, "
              f"P&L {pl(added):+.2f}U, sig 中位 "
              f"{sorted(r['sig'] for r in added)[len(added)//2]:.2f}, sd 中位 "
              f"{sorted(r['sd'] for r in added)[len(added)//2]:.1f} 美元")
    ph = [v for _b, v in moved] + added
    if ph:
        print(f"\n  pre 段命中 {len(ph)} 笔逐笔明细：")
        print(f"    {'日期':<12}{'rem':>5}{'侧':>5}{'成交价':>8}{'dev(美元)':>10}"
              f"{'sd(美元)':>9}{'sig':>6}{'结果':>6}   基线原先")
        for r in sorted(ph, key=lambda x: (x["date"], -x["rem"])):
            b_ = next((b for b, v in moved if v is r), None)
            tail = (f"{b_['stage']} 段 rem={b_['rem']} 价 {b_['fill']:.4f} "
                    f"({'赢' if b_['settle_won'] else '输'})") if b_ else "（新增：基线整窗无信号）"
            print(f"    {r['date']:<12}{r['rem']:>5}{r['side']:>5}{r['fill']:>8.4f}"
                  f"{r['dev']:>10.1f}{r['sd']:>9.1f}{r['sig']:>6.2f}"
                  f"{('赢' if r['settle_won'] else '输'):>6}   {tail}")
        print(f"\n  pre 段命中按**入场 rem** 分桶（第三节那个「rem ≤ 175 过线格」是平台还是尖峰）:")
        bk = collections.defaultdict(list)
        for r in ph:
            bk[min(275, (r["rem"] // 25) * 25)].append(r)
        print(f"    {'rem 桶':<14}{'n':>5}{'WR':>9}{'均价':>9}{'EV/注':>10}{'P&L':>9}")
        for k in sorted(bk, reverse=True):
            s = bk[k]
            wr = sum(x["settle_won"] for x in s) / len(s) * 100
            print(f"    {f'{k}~{k+24}':<14}{len(s):>5}{wr:>8.2f}%"
                  f"{sum(x['fill'] for x in s)/len(s):>9.4f}"
                  f"{pl(s)/len(s):>+10.4f}{pl(s):>+9.2f}")
        print("    （读法: 若「越早越差」是真的, 该列应自下而上单调走低; 只冒一格 = 噪声）")

    # ── 六之二、提前入场那批一分为二（描述性：用了「后来才知道」的信息）────
    print("\n" + "=" * 108)
    print("六之二、提前入场的一分为二：位移**延续**的窗 vs 位移**回落**的窗")
    print("=" * 108)
    grp = collections.defaultdict(lambda: {"d": 0.0, "bp": 0.0, "v": [], "b": []})
    for w, info in hk.items():
        v = pre_map.get(w)
        if v is None or v["stage"] != "pre":
            continue
        b = base_map.get(w)
        early = info["cand"]["rem"] > 175
        g = ("A 提前入场 ∧ 位移延续" if (early and info["persist"]) else
             "B 提前入场 ∧ 位移回落" if early else "C 入场已在 rem ≤ 175")
        grp[g]["d"] += pl([v]) - (pl([b]) if b else 0.0)
        grp[g]["bp"] += (pl([b]) if b else 0.0)
        grp[g]["v"].append(v)
        if b:
            grp[g]["b"].append(b)
    print(f"  {'组':<26}{'n':>5}{'输':>5}{'pre段P&L':>10}{'基线P&L':>10}{'ΔP&L':>9}"
          f"{'ΔEV/注':>10}{'pre均价':>9}{'基线均价':>9}")
    for g in sorted(grp):
        r = grp[g]
        vs, bs = r["v"], r["b"]
        nl = sum(1 for x in vs if not x["settle_won"])
        print(f"  {g:<26}{len(vs):>5}{nl:>5}{pl(vs):>+10.2f}{r['bp']:>+10.2f}{r['d']:>+9.2f}"
              f"{r['d']/len(vs):>+10.4f}{sum(x['fill'] for x in vs)/len(vs):>9.4f}"
              f"{(sum(x['fill'] for x in bs)/len(bs) if bs else 0):>9.4f}")
    print(f"  ⇒ 三组 Δ 相加 = {sum(r['d'] for r in grp.values()):+.2f}U（= 第一节的总 Δ）")
    print("  ⚠️ A/B 的分界线（位移撑到 rem ≤ 175）在入场的当下**不可知**——它是事后切分,")
    print("     只说明「便宜」与「买错」是同一件事的两面, 不是说存在一条能挑出 A 组的判据。")

    # ── 七、对照: pre 段 + 空窗段（上一轮题面）叠加 ──────────────────────
    print("\n" + "=" * 108)
    print("七、对照: 把上一轮的空窗检查也一起加上（pre + gap + 三段链）")
    print("=" * 108)
    line("基线（现行三段链）", base_sigs, rng)
    line("上一轮：空窗检查", gap_sigs, rng)
    line("本轮：pre 段", pre_sigs, rng)
    line("两段都加：pre + 空窗", both_sigs, rng)
    for lab, sg, mp in (("空窗", gap_sigs, gap_map), ("pre", pre_sigs, pre_map),
                        ("pre + 空窗", both_sigs, both_map)):
        pc = paired_ci(base_map, mp, rng)
        print(f"  {lab} 的配对 Δ: {pl(sg)-pl(base_sigs):+.2f}U, "
              f"95%CI [{pc[0]:+.1f}, {pc[1]:+.1f}]U")
    # 两段命中集的重叠
    g_win = set(w for w, r in gap_map.items() if r["stage"] == "gap")
    p_win = set(w for w, r in pre_map.items() if r["stage"] == "pre")
    print(f"\n  空窗段命中窗 {len(g_win)} 个, pre 段命中窗 {len(p_win)} 个, "
          f"重叠 {len(g_win & p_win)} 个")
    print(f"  （空窗段那 {len(g_win)} 个窗里, 有 {sum(1 for w in g_win if not any(x['win']==w and x['stage']=='pre' for x in pre_sigs))}"
          f" 个在 pre 区从没满足过题面条件 ⇒ 「提前」在那些窗里压根没发生）")

    print("\n" + "=" * 108)
    print("八、宇宙对账: pre 段（rem>150）与 23 的 rem≤150 是否互补")
    print("=" * 108)
    n_pre_ticks = n_ticks150 = 0
    for e in ev:
        if not e.get("twap_open_price"):
            continue
        n_pre_ticks += len(pre_ticks(e))
        n_ticks150 += len(s23.win_ticks(e))
    print(f"  rem > 150 的可判定 tick: {n_pre_ticks};  rem ≤ 150 的: {n_ticks150}")
    print("  （两段都在各自的提取器里做了同一套门: 延迟 ≤ 300ms ∧ 四档齐 ∧ spot > 0 ∧ rem > 0）")


if __name__ == "__main__":
    main()
