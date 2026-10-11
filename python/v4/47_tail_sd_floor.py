#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫尾盘：**低 σ（sd 极小）时不入场**能不能过滤掉亏损（2026-10-11 用户提出）。

用户原话：「今天凌晨出现了密集的亏损窗，数据我已经更新到 data/events 了，我观察到的是
sd 极低，你分析一下看，在 sd 极低时不入场，是否可以过滤掉一些。**主要是添加到只有 dev
门槛的时候**。」

先看清问题落在哪条腿上（现行三段链, 决策 #26/#29/#32/#33）:

    T=150  ⑤（价格腿严格 >0.80）∧ walk ≥ 43
    ⑤ = dev ≥ 63  ∨  (sd ≥ 40 ∧ dev ≥ sd)
    T=60   ⑤（价格腿非严格 ≥0.80）∧ 价格地板 > 0.83
    监听   ② = dev ≥ 63（**不含 σ 腿**）∧ 价格地板 > 0.83

⇒ `sd` 只在 ⑤ 的**第二项**（σ 腿）里出现过，而且那一项是**放宽**（允许 dev < 63）。
`dev ≥ 63` 这一项在 sd 上是**无条件**的。所以「只有 dev 门槛的时候」= 信号是**靠
`dev ≥ 63` 进来的**（σ 腿要么不适用〔监听段〕、要么没起作用〔sd < 40 或 dev 已 ≥ 63〕）。
用户要加的东西 = 在这条路上再加一条 **sd 下限**（`sd < X ⇒ 不入场`）。

⚠️ 方向与 09-26 那次相反: 那次是「低σ时**放宽** dev」（`docs/tail_sigma_gradient_2026-09-26.md`
全否）; 这次是「低σ时**拒单**」。但两者共用同一条机制假设——
`docs/tail_sigma_gradient_2026-09-26.md` §12.1: **sd 越小, 同一个 spot−TWAP 水平差在 σ 尺子
上越大**（sd<20 时中位 2.80σ, sd≥100 时 0.23σ）⇒ dev 证的不是「市场走了多远」而是
「两个数据源差多少」。本脚本 §3 就是在新样本上重测这条机制。

本脚本做四件事:

    §0 口径自检（data/btc 三把 pin: C43 / #32 / #33 逐位）——本脚本的链必须与 oracle 23 同源
    §1 新样本（data/events）概况: 窗数 / σ 分布 / 逐日 / 「今天凌晨」逐窗
    §2 现象: 信号按 sd 分档的胜率、平衡线、余量、P&L（分阶段）
    §3 机制: 低 sd 档里 dev 是不是被水平差主导（ratio = |offset|/sd）
    §4 变体: sd 地板（**影子行 + 链继续**, 与价格地板 #32/#33 同一形态）× 适用范围 × 阈值扫描
    §5 实盘对照（data/tail-live, 若在）

用法: python/venv/bin/python python/v4/47_tail_sd_floor.py
"""

import sys
import random
import datetime
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


s23 = load("s23", BASE / "23_tail_integrated.py")   # oracle：row / r2 / r5 / walk_ok / win_ticks
s38 = load("s38", BASE / "38_tail_walk_gate.py")    # 统计口径（pnl / by_day / boot_delta）
s13 = load("s13", BASE / "13_tail_sweep.py")        # σ: hist_ranges

STAKE = s23.STAKE
T60 = s23.T60
WALK_X = s23.WALK_MIN_USD        # 43：T=150 入场闸（决策 #29）
FLOOR_X = s23.FLOOR_MIN_PRICE    # 0.83：价格地板（决策 #32/#33）

# 三把 pin（oracle 23 / 42 打印的值）
PIN = {
    None:      (2080, 64.026354, 6686, 0),    # C43：只闸 T150（地板前）
    "listen":  (2076, 72.578473, 6697, 15),   # #32：地板只拦监听段
    "t60":     (2074, 70.314372, 6704, 24),   # #33：地板拦 t60+监听（**现行**）
}


# ── 宇宙 ──────────────────────────────────────────────────────────────────

def universe(data_dir):
    """与 38.universe 逐位同源，只是数据目录参数化。"""
    ev = s23.load_events(data_dir)
    hr = s13.hist_ranges(ev)
    wins = []
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
        ticks = s23.win_ticks(e)
        if not ticks:
            continue
        wins.append({"date": date, "start": e["start_time"], "outcome": outcome,
                     "anchor": anchor, "sd": sd, "ticks": ticks})
    return wins


# ── 链：三段递进 + walk 闸 + 价格地板 + **sd 地板** ─────────────────────────

def chain(w, floor_from="t60", sd_from=None, X=0.0):
    """返回 (rows, info)。info["shadow"] = 被拦下的影子行, info["sig"] = 信号。

    floor_from: None / "listen" / "t60"（现行 = "t60", 决策 #32+#33）
    sd_from:    None / "listen" / "t60" / "all" —— **sd 地板**的适用范围
                （sd < X ⇒ 本 tick 本会是信号, 被拦下 ⇒ 落影子行 + **链继续**）
    """
    ticks, anchor, sd, date, outcome = (w["ticks"], w["anchor"], w["sd"],
                                        w["date"], w["outcome"])
    rows, info = [], {"shadow": [], "sig": None, "sigrow": None}
    if not ticks:
        return rows, info
    shadowed = False

    def sd_cut(r, stages):
        return sd_from in stages and r["sd"] is not None and r["sd"] < X

    head = ticks[0]
    if head["rem"] > T60:
        r = s23.row(head, anchor, sd, date, outcome, "t150")
        if s23.r5(r, strict_price=True) and s23.walk_ok(r, WALK_X):
            if sd_cut(r, ("all",)):
                r["reject_reason"] = "sd_low"
                info["shadow"].append(r)
                rows.append(r)
                shadowed = True
            else:
                r["ok"] = True
                rows.append(r)
                info["sig"] = ("t150", id(head))
                info["sigrow"] = r
                return rows, info
        else:
            rows.append(r)
        rest = [x for x in ticks if x["rem"] <= T60]
    else:
        rest = ticks                                # 迟到接入: 不伪造 t150 行

    if rest:
        r2 = s23.row(rest[0], anchor, sd, date, outcome, "t60")
        if s23.r5(r2):
            cut_sd = sd_cut(r2, ("t60", "all"))
            cut_floor = floor_from == "t60" and r2["fill"] <= FLOOR_X
            if cut_sd or cut_floor:
                r2["reject_reason"] = "sd_low" if cut_sd else "floor_low"
                info["shadow"].append(r2)
                rows.append(r2)
                shadowed = True
            else:
                r2["ok"] = True
                rows.append(r2)
                info["sig"] = ("t60", id(rest[0]))
                info["sigrow"] = r2
                return rows, info
        else:
            rows.append(r2)
        for x in rest[1:]:
            rl = s23.row(x, anchor, sd, date, outcome, "listen")
            if not s23.r2(rl):
                continue
            cut_sd = sd_cut(rl, ("listen", "t60", "all"))
            cut_floor = floor_from is not None and rl["fill"] <= FLOOR_X
            if cut_sd or cut_floor:
                if not shadowed:                    # 影子行每窗至多一条
                    rl["reject_reason"] = "sd_low" if cut_sd else "floor_low"
                    info["shadow"].append(rl)
                    rows.append(rl)
                    shadowed = True
                continue
            rl["ok"] = True
            rows.append(rl)
            info["sig"] = ("listen", id(x))
            info["sigrow"] = rl
            return rows, info
    return rows, info


def run(wins, floor_from="t60", sd_from=None, X=0.0):
    sigs, shadows, nrows = [], [], 0
    for w in wins:
        rows, info = chain(w, floor_from, sd_from, X)
        nrows += len(rows)
        for r in rows:
            r["date"] = w["date"]
            r["start"] = w["start"]
            if r.get("ok"):
                sigs.append(r)
        shadows.extend(info["shadow"])
    return sigs, shadows, nrows


def blocked_batch(ev, scope, X):
    """被 sd 地板拦掉的窗里, **基线世界实际会成交的那一笔**（= 反事实要扣掉的那笔）。

    ⚠️ 不能用影子行代替：影子行落在「第一个被拦的 tick」上, 而 sd 检查在 chain 里排在
    地板检查**前面** ⇒ 当一个 tick 上 sd 与地板**同时**命中时, 影子行落在那个更早的
    便宜 tick（fill ≤ 0.83, 本来就被地板拦着）, 而基线的真信号在更晚的贵 tick 上。
    实测 113 条里有 13 条这样（全部落在 2026-10-10）, 影子行口径少算 1.59U。
    另注：sd 是**窗级常量** ⇒ sd<20 的窗里每个 tick 都被拦, 故改道（reroute）结构上为空。
    """
    died, reroute = [], []
    for w in ev:
        _, i_n = chain(w, "t60", None, 0)
        _, i_f = chain(w, "t60", scope, X)
        if not any(r.get("reject_reason") == "sd_low" for r in i_f["shadow"]):
            continue
        sn, sf = i_n["sigrow"], i_f["sigrow"]
        if sn is None:
            continue
        if sf is None:
            died.append(sn)
        elif sf["stage"] != sn["stage"] or sf["rem"] != sn["rem"]:
            reroute.append((sn, sf))
    return died, reroute


# ── 统计 ─────────────────────────────────────────────────────────────────

def pnl(rows):
    return sum((STAKE / r["fill"] - STAKE) if r["settle_won"] else -STAKE for r in rows)


def by_day(rows):
    d = collections.defaultdict(float)
    for r in rows:
        d[r["date"]] += (STAKE / r["fill"] - STAKE) if r["settle_won"] else -STAKE
    return d


def boot(base, new, n=2000, seed=47):
    days = sorted(set(list(base) + list(new)))
    d = [new.get(k, 0.0) - base.get(k, 0.0) for k in days]
    rnd = random.Random(seed)
    out = sorted(sum(d[rnd.randrange(len(d))] for _ in d) for _ in range(n))
    return out[int(0.025 * n)], out[int(0.975 * n) - 1]


def med(xs):
    xs = sorted(xs)
    if not xs:
        return 0.0
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def desc(rows):
    if not rows:
        return "n=0"
    n = len(rows)
    wr = sum(r["settle_won"] for r in rows) / n * 100
    fill = sum(r["fill"] for r in rows) / n
    return (f"n={n:<5} WR {wr:6.2f}%  均价 {fill:.4f}  余量 {wr/100-fill:+6.2%}  "
            f"输 {sum(1 for r in rows if not r['settle_won']):<3} P&L {pnl(rows):+8.2f}U")


def dline(lab, rows, width=34):
    print("  " + lab.ljust(width) + desc(rows))


# ── 主 ────────────────────────────────────────────────────────────────────

def main():
    print("=" * 108)
    print("§0 口径自检（data/btc 三把 pin 必须逐位对上, 否则后面数字不可信）")
    print("=" * 108)
    bt = universe("data/btc")
    ok_all = True
    for ff, pin in PIN.items():
        s, sh, nr = run(bt, ff)
        ok = (len(s) == pin[0] and abs(pnl(s) - pin[1]) < 1e-4
              and nr == pin[2] and len(sh) == pin[3])
        ok_all &= ok
        name = {None: "C43（无地板）", "listen": "#32（地板只拦监听）",
                "t60": "#33（地板拦 t60+监听, 现行）"}[ff]
        print(f"  {name:<28} {desc(s)}  行 {nr}  影子 {len(sh)}  {'✅' if ok else '❌'}")
    print(f"  宇宙 {len(bt)} 窗（pin 3640）")
    print(f"  {'全对 ✅' if ok_all else '❌ 自检失败——后面不要读'}")

    ev = universe("data/events")
    base, base_sh, base_rows = run(ev, "t60")            # 现行落地面
    print(f"\n  新样本 data/events: {len(ev)} 窗（{ev[0]['date']} ~ {ev[-1]['date']}）")
    print(f"  现行链（决策 #33）   {desc(base)}  行 {base_rows}  影子 {len(base_sh)}")

    BD = by_day(base)

    # ── §1 新样本概况 + 「今天凌晨」───────────────────────────────────────
    print("\n" + "=" * 108)
    print("§1 新样本逐日（现行链; σ 分布 + 信号 + 结果）")
    print("=" * 108)
    print(f"  {'日期':<12}{'窗数':>6}{'σ中位':>9}{'t150':>6}{'t60':>6}{'监听':>6}{'合计':>6}"
          f"{'WR':>8}{'输':>4}{'P&L':>10}")
    days = sorted(set(w["date"] for w in ev))
    for d in days:
        ws = [w for w in ev if w["date"] == d]
        per = {s: [r for r in base if r["date"] == d and r["stage"] == s]
               for s in ("t150", "t60", "listen")}
        day = [r for r in base if r["date"] == d]
        wr = (sum(r["settle_won"] for r in day) / len(day) * 100) if day else 0.0
        print(f"  {d:<12}{len(ws):>6}{med([w['sd'] for w in ws]):>9.1f}"
              f"{len(per['t150']):>6}{len(per['t60']):>6}{len(per['listen']):>6}{len(day):>6}"
              f"{wr:>7.1f}%{sum(1 for r in day if not r['settle_won']):>4}{pnl(day):>+10.2f}")
    print(f"  {'合计':<12}{len(ev):>6}{med([w['sd'] for w in ev]):>9.1f}"
          f"{sum(1 for r in base if r['stage']=='t150'):>6}"
          f"{sum(1 for r in base if r['stage']=='t60'):>6}"
          f"{sum(1 for r in base if r['stage']=='listen'):>6}{len(base):>6}"
          f"{sum(r['settle_won'] for r in base)/len(base)*100:>7.1f}%"
          f"{sum(1 for r in base if not r['settle_won']):>4}{pnl(base):>+10.2f}")

    # 亏损窗逐条（近 3 天）
    print("\n  最近三天的**输单**逐笔（含 sd / dev / walk / offset 解剖）:")
    cut = sorted(set(w["date"] for w in ev))[-3:]
    los = [r for r in base if not r["settle_won"] and r["date"] in cut]
    print(f"    {'日期':<12}{'UTC时刻':<7}{'段':<8}{'rem':>4}{'side':<5}{'fill':>6}"
          f"{'sd':>7}{'dev':>8}{'walk':>8}{'basis':>8}{'ratio':>7}")
    for r in sorted(los, key=lambda r: r["start"]):
        hhmm = datetime.datetime.fromtimestamp(
            r["start"], datetime.timezone.utc).strftime("%H:%M")
        # basis = dev − walk（twap 缺失时 walk 为 None ⇒ basis 也算不出）
        basis = None if r["walk"] is None else r["dev"] - r["walk"]
        ratio = None if (basis is None or r["sd"] == 0) else abs(basis) / r["sd"]

        def fm(v):
            return "       —" if v is None else f"{v:8.1f}"

        print(f"    {r['date']:<12}{hhmm:<7}{r['stage']:<8}{r['rem']:>4}{r['side']:<5}"
              f"{r['fill']:>6.2f}{r['sd']:>7.1f}{r['dev']:>8.1f}"
              f"{fm(r['walk'])}{fm(basis)}"
              f"{('       —' if ratio is None else f'{ratio:8.2f}')}")

    # ── §2 按 sd 分档 ────────────────────────────────────────────────────
    print("\n" + "=" * 108)
    print("§2 信号按 sd 分档（现行链全部信号 = 全部靠 dev 腿或 σ 腿进来的）")
    print("=" * 108)
    BUCKETS = [(0, 10), (10, 15), (15, 20), (20, 25), (25, 30), (30, 40),
               (40, 63), (63, 100), (100, 1e9)]

    def bucket_of(sd):
        for lo, hi in BUCKETS:
            if lo <= sd < hi:
                return f"[{lo:.0f},{hi:.0f})" if hi < 1e8 else f"[{lo:.0f}, +∞)"
        return "?"

    print(f"  {'sd 档':<14}{'n':>6}{'均价':>9}{'WR':>9}{'平衡线':>9}{'余量':>9}"
          f"{'输':>4}{'P&L':>10}{'EV/注':>9}")
    for lo, hi in BUCKETS:
        g = [r for r in base if lo <= r["sd"] < hi]
        if not g:
            continue
        n = len(g)
        wr = sum(r["settle_won"] for r in g) / n
        fill = sum(r["fill"] for r in g) / n
        print(f"  {bucket_of(g[0]['sd']):<14}{n:>6}{fill:>9.4f}{wr*100:>8.2f}%{fill*100:>8.2f}%"
              f"{wr-fill:>+8.2%}{sum(1 for r in g if not r['settle_won']):>4}"
              f"{pnl(g):>+10.2f}{pnl(g)/n:>+9.4f}")

    print("\n  分段 × sd 分档（P&L / n）:")
    for st in ("t150", "t60", "listen"):
        parts = []
        for lo, hi in BUCKETS:
            g = [r for r in base if r["stage"] == st and lo <= r["sd"] < hi]
            if g:
                parts.append(f"{bucket_of(lo)}: {pnl(g):+.1f}U/{len(g)}")
        print(f"    {st:<8} " + "  |  ".join(parts))

    print("\n  「靠哪条腿进来的」× sd 分档（dev 腿 = dev ≥ 63; σ 腿 = dev < 63 ∧ sd ≥ 40 ∧ dev ≥ sd）:")
    for leg in ("dev 腿", "σ 腿"):
        parts = []
        for lo, hi in BUCKETS:
            g = [r for r in base if lo <= r["sd"] < hi
                 and (("dev 腿" if r["dev"] >= s23.DEV_USD else "σ 腿") == leg)]
            if g:
                parts.append(f"{bucket_of(lo)}: {pnl(g):+.1f}U/{len(g)}"
                             f"({sum(1 for r in g if not r['settle_won'])}输)")
        print(f"    {leg:<8} " + "  |  ".join(parts))

    # ── §3 机制 ─────────────────────────────────────────────────────────
    print("\n" + "=" * 108)
    print("§3 机制：低 sd 档的 dev 是不是被 spot−TWAP 水平差主导（09-26 §12 在新样本上重测）")
    print("=" * 108)
    print(f"  {'sd 档':<14}{'n':>6}{'|offset|中位':>13}{'ratio中位':>11}{'ratio≥1占比':>12}"
          f"{'|walk|中位':>12}{'输率':>8}")
    for lo, hi in BUCKETS:
        g = [r for r in base if lo <= r["sd"] < hi and r["walk"] is not None]
        if not g:
            continue
        offs = [abs(r["dev"] - r["walk"]) for r in g]
        ratios = [abs(r["dev"] - r["walk"]) / r["sd"] for r in g if r["sd"] > 0]
        print(f"  {bucket_of(lo):<14}{len(g):>6}{med(offs):>13.1f}{med(ratios):>11.2f}"
              f"{sum(1 for x in ratios if x >= 1)/len(ratios)*100:>11.1f}%"
              f"{med([abs(r['walk']) for r in g]):>12.1f}"
              f"{sum(1 for r in g if not r['settle_won'])/len(g)*100:>7.1f}%")

    # ── §4 sd 地板变体 ──────────────────────────────────────────────────
    print("\n" + "=" * 108)
    print("§4 变体：sd 地板（sd < X ⇒ 落影子行 + 链继续, 与价格地板同一形态）")
    print("=" * 108)
    print(f"  基线（决策 #33）  {desc(base)}")

    SCAN = [5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 63]
    for scope, lab in (("listen", "只拦监听段"), ("t60", "拦 t60+监听"),
                       ("all", "拦三段（含 T=150）")):
        print(f"\n  ── 适用范围: {lab} ──")
        print(f"  {'X(美元)':>8}{'n':>7}{'WR':>9}{'输':>5}{'P&L':>10}{'Δ基线':>10}"
              f"{'日级配对 95%':>22}")
        for X in SCAN:
            s, sh, nr = run(ev, "t60", scope, X)
            lo, hi = boot(BD, by_day(s))
            d = pnl(s) - pnl(base)
            star = "*" if (lo > 0 or hi < 0) else " "
            print(f"  {X:>8}{len(s):>7}{sum(r['settle_won'] for r in s)/len(s)*100:>8.2f}%"
                  f"{sum(1 for r in s if not r['settle_won']):>5}{pnl(s):>+10.2f}{d:>+10.2f}"
                  f"   [{lo:+7.2f},{hi:+7.2f}]{star}")

    print("\n  ⚠️ 全表为**扫描**：argmax 的 Δ 是 11 档 × 3 范围里的最大值, 读它必须扣搜索代价")
    print("     （见 §5 的置换安慰剂）。区间含 0 = 与基线不可区分。")

    # ── §5 细账 + 安慰剂 ────────────────────────────────────────────────
    print("\n" + "=" * 108)
    print("§5 细账：被 sd 地板拦下的那批「在基线世界里成交了哪一笔」")
    print("=" * 108)
    for scope, X in (("listen", 20), ("t60", 20), ("all", 20)):
        sigs, shadows, _ = run(ev, "t60", scope, X)
        sd_sh = [r for r in shadows if r.get("reject_reason") == "sd_low"]
        if not sd_sh:
            print(f"  scope={scope} X={X}: 影子行 0")
            continue
        died, reroute = blocked_batch(ev, scope, X)
        print(f"\n  ── scope={scope}, X={X}：影子行 {len(sd_sh)} 条"
              f"（被拦的那批在基线世界里的信号）──")
        print(f"     整窗死亡 {len(died)}" +
              (f"：这批里输 {sum(1 for r in died if not r['settle_won'])} 笔, "
               f"基线 P&L {pnl(died):+.2f}U ⇒ **不买它们省下 {-pnl(died):+.2f}U**"
               if died else ""))
        if died:
            n = len(died)
            wr = sum(r["settle_won"] for r in died) / n
            fill = sum(r["fill"] for r in died) / n
            blocked = {(r["start"], r["stage"]) for r in sd_sh}
            keep = [r for r in base if (r["start"], r["stage"]) not in blocked]
            print(f"     被拦批次画像: n={n} 均价 {fill:.4f} WR {wr*100:.2f}% "
                  f"余量 {wr-fill:+.2%} 找平把数 {fill/(1-fill):.1f}"
                  f"  |  保留批次 n={len(keep)} 均价 "
                  f"{sum(r['fill'] for r in keep)/len(keep):.4f} "
                  f"WR {sum(r['settle_won'] for r in keep)/len(keep)*100:.2f}%")
        if reroute:
            old = [p[0] for p in reroute]
            new = [p[1] for p in reroute]
            print(f"     同段改道 {len(reroute)}：新 fill 中位 {med([x[1]['fill'] for x in reroute]):.4f}"
                  f"（旧 {med([x[0]['fill'] for x in reroute]):.4f}）; "
                  f"这批窗 P&L {pnl(old):+.2f}U → {pnl(new):+.2f}U"
                  f"（改道税 {pnl(new)-pnl(old):+.2f}U）")

    print("\n  被拦那批的逐笔（scope=t60, X=20）:")
    sigs, shadows, _ = run(ev, "t60", "t60", 20)
    sd_sh = sorted([r for r in shadows if r.get("reject_reason") == "sd_low"],
                   key=lambda r: r["start"])
    for r in sd_sh:
        hhmm = datetime.datetime.fromtimestamp(
            r["start"], datetime.timezone.utc).strftime("%m-%d %H:%M")
        w = "      —" if r["walk"] is None else f"{r['walk']:7.1f}"
        print(f"     {hhmm}  {r['stage']:<7}{r['rem']:>4}  {r['side']:<5}{r['fill']:.2f}  "
              f"sd {r['sd']:>6.1f}  dev {r['dev']:>7.1f}  walk {w}  "
              f"{'赢' if r['settle_won'] else '输'}")
    died, _ = blocked_batch(ev, "t60", 20)
    print(f"     ⇒ 反事实（**基线世界那批真信号**）: {len(died)} 笔里输 "
          f"{sum(1 for r in died if not r['settle_won'])} 笔, P&L {pnl(died):+.2f}U "
          f"⇒ **不买它们省下 {-pnl(died):+.2f}U**")
    print(f"        （影子行自身 P&L {pnl(sd_sh):+.2f}U —— 13 条影子行落在更早的便宜 tick 上, "
          f"不能用它当反事实, 见 blocked_batch 注释）")

    # ── §6 安慰剂：按 (段, 价带) 分层随机拦同样笔数 ───────────────────────
    print("\n" + "=" * 108)
    print("§6 安慰剂：按 **(段, 价带) 分层**随机挑同样笔数（4000 次）——控制「这只是高价单」")
    print("=" * 108)
    print("  被 sd 挑出来的那批均价 0.9565、保留批 0.9636 —— **并不更贵**（与回测样本相反）。")
    print("  所以问题的形式是：「同段同价带里随机挑同样笔数，能不能也省下这么多？」")

    def band(f):
        """价带（0.05 一档, 顶端 1.00 单列）——安慰剂按它配平。"""
        return next((l for l in (0.80, 0.85, 0.90, 0.95, 0.98) if l <= f < l + 0.05), 1.0)

    for scope, X in (("listen", 20), ("t60", 20), ("all", 20)):
        died, _ = blocked_batch(ev, scope, X)
        if not died:
            print(f"  scope={scope:<8} X={X:<4} 拦 0 笔")
            continue
        real = -pnl(died)                        # 省下的钱（正 = 省了）——反事实口径
        key = lambda r: (r["stage"], band(r["fill"]))
        need = collections.Counter(key(r) for r in died)
        pool = collections.defaultdict(list)
        for r in base:
            pool[key(r)].append(r)
        rnd = random.Random(47)
        outs = []
        for _ in range(4000):
            tot = 0.0
            for k, kk in need.items():
                g = pool.get(k, [])
                for r in rnd.sample(g, min(kk, len(g))):
                    tot -= (STAKE / r["fill"] - STAKE) if r["settle_won"] else -STAKE
            outs.append(tot)
        outs.sort()
        p = sum(1 for o in outs if o >= real) / len(outs)
        p95 = outs[int(0.95 * len(outs))]
        print(f"  scope={scope:<8} X={X:<4} 拦 {len(died):<4} 笔: 实际省 {real:+6.2f}U; "
              f"分层随机 中位 {outs[len(outs)//2]:+6.2f}U, p95 {p95:+6.2f}U, "
              f"最大 {outs[-1]:+6.2f}U ⇒ **p = {p:.4f}**  {'（过 p95）' if real > p95 else ''}")

    # ── §7 分半 + 逐日归因（Δ 稳不稳）────────────────────────────────────
    print("\n" + "=" * 108)
    print("§7 argmax 那一格的 Δ 稳不稳：分半 + 逐日归因（scope=t60, X=20）")
    print("=" * 108)
    s_new, sh_new, _ = run(ev, "t60", "t60", 20)
    delta_day = {}
    for d in sorted(set(r["date"] for r in base)):
        delta_day[d] = (by_day(s_new).get(d, 0.0) - BD.get(d, 0.0))
    days_sorted = sorted(delta_day)
    half = len(days_sorted) // 2
    h1 = sum(delta_day[d] for d in days_sorted[:half])
    h2 = sum(delta_day[d] for d in days_sorted[half:])
    print(f"  总 Δ {sum(delta_day.values()):+.2f}U; 前半（{days_sorted[0]}~{days_sorted[half-1]}）"
          f"{h1:+.2f}U | 后半（{days_sorted[half]}~{days_sorted[-1]}）{h2:+.2f}U")
    print("  逐日 Δ（正 = 该日 sd 地板省钱）:")
    for d in days_sorted:
        if abs(delta_day[d]) > 1e-9:
            print(f"    {d}  {delta_day[d]:+7.2f}U")

    died, _ = blocked_batch(ev, "t60", 20)
    agg = collections.defaultdict(lambda: [0, 0, 0])
    for r in base:
        agg[r["date"]][0] += 1
        if not r["settle_won"]:
            agg[r["date"]][1] += 1
            if r["sd"] < 20:
                agg[r["date"]][2] += 1
    blk = collections.Counter(r["date"] for r in died)
    blkloss = collections.Counter(r["date"] for r in died if not r["settle_won"])
    print(f"\n  {'日期':<12}{'信号':>6}{'输':>5}{'输单sd<20':>10}{'地板拦窗':>9}{'其中输':>7}"
          f"{'基线P&L':>10}{'地板后':>9}{'Δ':>8}")
    for d in days_sorted:
        n, l, l20 = agg[d]
        print(f"  {d:<12}{n:>6}{l:>5}{l20:>10}{blk[d]:>9}{blkloss[d]:>7}"
              f"{BD.get(d, 0.0):>+10.2f}{BD.get(d, 0.0)+delta_day[d]:>+9.2f}{delta_day[d]:>+8.2f}")
    print(f"  合计        {len(base):>6}{sum(1 for r in base if not r['settle_won']):>5}"
          f"{sum(1 for r in base if not r['settle_won'] and r['sd'] < 20):>10}"
          f"{len(died):>9}{sum(1 for r in died if not r['settle_won']):>7}"
          f"{pnl(base):>+10.2f}{pnl(s_new):>+9.2f}{pnl(s_new)-pnl(base):>+8.2f}")
    print("  ⚠️ 15 天里 12 天开火次数为 0；正 Δ 全在 10-10（+4.70U）+ 10-11（+0.42U）——")
    print("     而 10-10 正是「用户发现这个现象」的那一天（用同一天既发现又验证 = 循环）。")

    # ── §8 同价带内 sd 还有没有信息 ──────────────────────────────────────
    print("\n" + "=" * 108)
    print("§8 关键控制：**同价带内** sd 还携带信息吗（否则这只是价格地板的伪装）")
    print("=" * 108)
    print(f"  {'价带':<14}{'sd<20':>26}{'sd≥20':>26}   余量差")
    for lo, hi in ((0.80, 0.85), (0.85, 0.90), (0.90, 0.95), (0.95, 1.01)):
        g = [r for r in base if lo <= r["fill"] < hi]
        a = [r for r in g if r["sd"] < 20]
        b = [r for r in g if r["sd"] >= 20]
        def cell(x):
            if not x:
                return f"{'—':>26}"
            n = len(x)
            wr = sum(r["settle_won"] for r in x) / n
            fill = sum(r["fill"] for r in x) / n
            return f"n={n:<4} 输{sum(1 for r in x if not r['settle_won']):<3} " \
                   f"余量{wr-fill:+.2%} {pnl(x):+6.1f}U"
        dd = ""
        if a and b:
            wa = sum(r["settle_won"] for r in a) / len(a) - sum(r["fill"] for r in a) / len(a)
            wb = sum(r["settle_won"] for r in b) / len(b) - sum(r["fill"] for r in b) / len(b)
            dd = f"{wa-wb:+.2%}"
        print(f"  [{lo:.2f},{hi:.2f})   {cell(a):>26}{cell(b):>26}   {dd}")

    # ── §9 换把更贴机制的刀：ratio = |dev − walk| / sd ────────────────────
    print("\n" + "=" * 108)
    print("§9 换刀对照：拦的是「dev 由水平差撑起来」而不是「sd 小」")
    print("=" * 108)
    print("  机制里的量是 ratio = |offset| / sd（09-26 §12.1）。这里改拦 ratio ≥ K，")
    print("  看它是否比 sd 刀更干净（⚠️ 只作对照，ratio 不在引擎的行里, 要落得先落 basis 列）。")
    print(f"  {'K':>6}{'被拦n':>8}{'被拦P&L':>10}{'省下':>9}")
    for K in (0.5, 1, 1.5, 2, 3, 4, 6):
        g = [r for r in base if r["walk"] is not None
             and abs(r["dev"] - r["walk"]) / r["sd"] >= K]
        print(f"  {K:>6}{len(g):>8}{pnl(g):>+10.2f}{-pnl(g):>+9.2f}U")
    keep = [r for r in base if r["walk"] is not None
            and abs(r["dev"] - r["walk"]) / r["sd"] >= 2]
    print(f"  ⚠️ 这些数只是**事后切一刀的省下额**, 不含改道/重入 → 与 §5 的口径不同, 只比排序。")
    print(f"     ratio≥2 的 n={len(keep)}（占 {len(keep)/len(base)*100:.1f}%）, "
          f"其中 sd<20 的只有 {sum(1 for r in keep if r['sd'] < 20)} 笔 ⇒ 两把刀砍的不是同一批。")


if __name__ == "__main__":
    main()
