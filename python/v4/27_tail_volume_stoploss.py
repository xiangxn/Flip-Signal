#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**窗口 BTC 总成交量**对「持仓止损」有没有帮助（2026-09-25）

用户提问：「data/btc 里的 tick 数据能不能聚合出整个 5 分钟的 BTC 总交易量，看看这个
数据对止损是否有帮助」。

## 这个量是什么

tick 行里有 `bin.buy_vol` / `bin.sell_vol`（Binance BTCUSDT 本秒主动买/卖量，单位 BTC，
增量）。窗口内逐秒求和 = **本窗 BTC 总成交量**（14 天实测 p5 10.7 / p50 45.0 / p95
230.4 BTC，极差 1237 倍 —— 动态范围足够大，不是个常数）。

## 为什么它可能有帮助

25 号脚本已证：**触发止损那一秒**没有任何先验特征能区分「真翻盘」与「假摔」
（BTC 秒量 AUC 0.485~0.499 ≈ 纯噪声，见 25 号第三节）。那是**单秒**的量。
本脚本问的是另一个量：**整窗的活动水平**。

机制假设：尾盘买热门侧赌的是「现货已经走出来」。$63 的位移发生在放量窗 vs 缩量窗，
含义完全不同 —— 放量窗 = 真有大资金推动（位移更可能守住）；缩量窗 = 薄盘漂移
（更容易被打回去）。若成立，则止损的性价比应当**随成交量分层**。

## 三个必须先说清的坑

1. **整窗总量是滞后量**（闭市才知道）。止损要在 `rem≈60` 做决定，那时整窗只走了 80%。
   故本脚本一律**两套并排**：`V_win`（整窗，后验诊断）与 `V_now`（到止损时刻的累计，
   因果可用），并在因果那套上单独跑一遍，避免把 look-ahead 的漂亮数字当结论。
2. **与 σ 腿可能冗余**。`sd`（hist_bps 折美元）是**历史**波动代理，`V_win` 是**本窗实现**
   活动量。两者若高度相关，则本量不提供新信息 —— 第一节直接量相关系数。
3. **样本量与上界**。止损只触发 68 次（其中杀赢 5 次）⇒ 分档后每档十几笔，**任何**
   分层结论都在噪声里；且 26 号文件头的两条上界（历史数据持仓侧 bid 恒 >0 = 构造性、
   无滑点）原样适用。本脚本只做**机制筛查**，不做判决。

用法:
    python/venv/bin/python python/v4/27_tail_volume_stoploss.py
    python/venv/bin/python python/v4/27_tail_volume_stoploss.py --dump
"""
import sys
import math
import random
import datetime
import argparse
import collections
import importlib.util
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))
from v2.lib import load_events                                    # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


s13 = load("s13", BASE / "13_tail_sweep.py")
s26 = load("s26", BASE / "26_tail_integrated_stoploss.py")

STAKE = s26.STAKE
STAGES = s26.STAGES
STOP_BID, STOP_DEV = s26.STOP_BID, s26.STOP_DEV
MAX_LAT = s26.MAX_LAT

WINDOW = 300.0          # 窗口长度（秒）


# ── 成交量提取 ────────────────────────────────────────────────────────────

def vol_prof(e):
    """逐 tick 的 BTC 成交量累计。

    返回 `(rows, total)`：`rows` = [{rem, v, cum}]（时间顺序，rem 递减），
    `total` = 整窗总成交量（BTC）。

    `v` = `bin.buy_vol + bin.sell_vol` = 本秒**主动买 + 主动卖**（单位 BTC）。
    两个字段都是 Binance aggTrade 的增量，求和即该秒总成交量（无需去重）。
    """
    rows, cum = [], 0.0
    for x in e.get("ticks") or []:
        b = x.get("bin") or {}
        v = (b.get("buy_vol") or 0.0) + (b.get("sell_vol") or 0.0)
        cum += v
        rows.append({"rem": x.get("rem"), "v": v, "cum": cum})
    return rows, cum


def vol_cum(rows, rem):
    """到 `rem` 那一刻（含）的累计成交量 + 已过秒数。

    ⚠️ 因果量：只用 `rem` 之前的信息（tick 按 rem 递减排列）⇒ live 可得。
    """
    c = 0.0
    for r in rows:
        if r["rem"] is None:
            continue
        c = r["cum"]
        if r["rem"] <= rem:
            break
    return c, max(1.0, WINDOW - rem)


def vol_burst(rows, rem, span=30):
    """`rem` 之前 `span` 秒的成交量（放量检测用）。"""
    s = 0.0
    for r in rows:
        if r["rem"] is None:
            continue
        if rem < r["rem"] <= rem + span:
            s += r["v"]
        if r["rem"] <= rem:
            break
    return s


# ── 统计小工具 ────────────────────────────────────────────────────────────

def q(xs, p):
    if not xs:
        return float("nan")
    a = sorted(xs)
    i = min(len(a) - 1, max(0, int(p * (len(a) - 1))))
    return a[i]


def auc(pos, neg):
    """AUC = P(score_pos > score_neg)（并列计 0.5）。None = 某一侧为空。"""
    if not pos or not neg:
        return None
    w = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return w / (len(pos) * len(neg))


def spearman(xs, ys):
    """秩相关（并列取平均秩）。"""
    n = len(xs)
    if n < 3:
        return None

    def rank(v):
        idx = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[idx[j + 1]] == v[idx[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                r[idx[k]] = avg
            i = j + 1
        return r

    rx, ry = rank(xs), rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return num / (dx * dy) if dx and dy else None


def line(lab, rows, width=24):
    """一行的 注数 / 胜率 / P&L（基线 hold） / 止损 P&L / Δ。"""
    n = len(rows)
    if not n:
        print(f"  {lab:<{width}} n=0")
        return
    won = sum(r["settle_won"] for r in rows)
    hold = sum(r["pnl_hold"] for r in rows)
    stop = sum(r["pnl_stop"] for r in rows)
    print(f"  {lab:<{width}} n={n:<5} WR {won / n * 100:5.2f}%  "
          f"P&L {hold:+8.2f} → {stop:+8.2f}  Δ {stop - hold:+7.2f}U")


def boot_gated(rows, reps=2000, seed=42):
    """配对 bootstrap 的 Δ 区间（口径同 26 的 `boot_delta`，但读 `_pnl_gated`）。

    第四节要重挂「开关」后的 P&L，不能借用 `pnl_stop` 字段（它是无开关的版本）。
    """
    ds = sorted(set(r["date"] for r in rows if r["date"] != "2026-08-31"))
    if len(rows) < 5 or not ds:
        return None
    d = collections.defaultdict(list)
    for r in rows:
        d[r["date"]].append(r)
    rng = random.Random(seed)
    out = []
    for _ in range(reps):
        pool = [r for k in (rng.choice(ds) for _ in ds) for r in d[k]]
        out.append(sum(x["_pnl_gated"] - x["pnl_hold"] for x in pool))
    out.sort()
    return out[int(0.025 * len(out))], out[int(0.975 * len(out))]


# ── 主流程 ────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="窗口 BTC 总成交量 × 持仓止损")
    ap.add_argument("--data", default="data/btc")
    ap.add_argument("--stop-bid", type=float, default=STOP_BID)
    ap.add_argument("--stop-dev", type=float, default=STOP_DEV)
    ap.add_argument("--dump", action="store_true", help="打印被止损行的成交量明细")
    args = ap.parse_args()

    ev = load_events(args.data)
    hr = s13.hist_ranges(ev)

    sigs = []
    nobs = skipped = 0
    for e in ev:
        outcome, anchor = e.get("outcome"), e.get("twap_open_price")
        if outcome is None or not anchor:
            continue
        h = hr.get(e["start_time"])
        if h is None:
            skipped += 1
            continue                                    # σ 未就绪整窗跳过（决策 #13）
        sd = h / anchor * 1e4 * anchor / 1e4
        date = datetime.datetime.fromtimestamp(
            e["start_time"], datetime.timezone.utc).strftime("%Y-%m-%d")
        ticks = s26.win_ticks(e)
        if not ticks:
            continue
        nobs += 1
        rows, _ = s26.chain(ticks, anchor, sd, date, outcome)
        for r in rows:
            if not r.get("ok"):
                continue
            p = s26.post(e, r)
            s26.attach_stop(e, r, p, args.stop_bid, args.stop_dev)
            vrows, vtot = vol_prof(e)
            e["_vrows"] = vrows     # 第六节要回放整条 tick 序列（挂在事件上, 多行共用）
            r["_e"] = e
            r["_Vwin"] = vtot
            r["_sd"] = sd
            # 因果量：到**止损那一刻**（未触发则用入场时刻）的累计成交量与速率
            t = r["stopped"]
            rem_ref = t[0] if t else r["rem"]
            c, el = vol_cum(vrows, rem_ref)
            r["_Vnow"] = c
            r["_Vrate"] = c / el                            # BTC/秒
            r["_Vburst"] = vol_burst(vrows, rem_ref, 30) / max(1e-9, r["_Vrate"]) / 30.0
            sigs.append(r)

    print("=" * 100)
    print("零、基线自检（必须逐位复现 26/23 号脚本）")
    print("=" * 100)
    base_hold = sum(r["pnl_hold"] for r in sigs)
    base_stop = sum(r["pnl_stop"] for r in sigs)
    nwin = sum(r["settle_won"] for r in sigs)
    print(f"  信号 n={len(sigs)}  WR {nwin / len(sigs) * 100:.4f}%  "
          f"基线 P&L {base_hold:+.4f}U → 止损后 {base_stop:+.4f}U  Δ {base_stop - base_hold:+.4f}U")
    fire = [r for r in sigs if r["stopped"]]
    killed = [r for r in fire if r["settle_won"]]
    saved = [r for r in fire if not r["settle_won"]]
    print(f"  止损触发 {len(fire)} 次（{len(fire) / len(sigs) * 100:.2f}%）  "
          f"杀赢 {len(killed)} / 救输 {len(saved)}   参与判定窗口 {nobs}（σ 跳过 {skipped}）")
    ok = (len(sigs) == 2135 and abs(base_hold - 35.674749) < 1e-6)
    print(f"  {'✅ 与 oracle 一致' if ok else '❌ 与 oracle 不符（先修脚本再读下文）'}")
    if not ok:
        return

    # ── 一、分布与基本关系 ────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("一、窗口总成交量长什么样（`V_win` = 300 个 tick 的 buy_vol+sell_vol 求和）")
    print("=" * 100)
    V = [r["_Vwin"] for r in sigs]
    for p in (.10, .25, .50, .75, .90):
        print(f"  p{int(p * 100):<3} {q(V, p):9.1f} BTC")
    print(f"  min {min(V):.1f}  max {max(V):.1f}  极差 {max(V) / max(min(V), 1e-9):.0f}×")

    print("\n  与既有量的关系（Spearman 秩相关，n=%d）:" % len(sigs))
    for lab, ys in (("σ 折美元 sd", [r["_sd"] for r in sigs]),
                    ("入场 dev（美元）", [r["dev"] for r in sigs]),
                    ("入场 rem", [r["rem"] for r in sigs]),
                    ("结算结果（赢=1）", [r["settle_won"] for r in sigs])):
        rho = spearman(V, ys)
        print(f"    ρ(V_win, {lab:<16}) = {rho:+.3f}")
    print("  ⚠️ 若 ρ(V_win, sd) 高 ⇒ 本量与 σ 腿冗余；若 ρ(V_win, 结算) ≈ 0")
    print("     ⇒ 它对**入场胜率**没有预测力（那它唯一可能的用处就是止损侧）")

    # ── 二、判别力：真翻盘 vs 假摔 ────────────────────────────────────────
    print("\n" + "=" * 100)
    print("二、判别力：触发止损的行里，成交量能否区分「救对了」与「杀错了」")
    print("=" * 100)
    print(f"  阳性（真输, 救对了）= {len(saved)} 行；阴性（最终赢, 杀错了）= {len(killed)} 行")
    for lab, key in (("V_win 整窗总量（后验）", "_Vwin"),
                     ("V_now 到止损时累计（因果）", "_Vnow"),
                     ("V_rate 到止损时 BTC/秒（因果）", "_Vrate"),
                     ("V_burst 近 30s 放量倍数（因果）", "_Vburst")):
        a = auc([r[key] for r in saved], [r[key] for r in killed])
        print(f"    {lab:<32} AUC {a:.3f}" if a is not None else f"    {lab:<32} AUC n/a")
    print("  ⚠️ 5 个阴性样本的 AUC 标准误 ≈ 0.13 —— 任何 |AUC−0.5| < 0.26 都不显著。")

    # ── 三、按成交量分层看止损 Δ ──────────────────────────────────────────
    print("\n" + "=" * 100)
    print("三、分层：按窗口成交量切三档，各档止损 Δ（**机制筛查**，非判决）")
    print("=" * 100)
    for lab, key in (("V_win 整窗总量（后验）", "_Vwin"),
                     ("V_rate 止损时速率（因果）", "_Vrate")):
        vals = sorted(r[key] for r in sigs)
        lo, hi = q(vals, 1 / 3), q(vals, 2 / 3)
        print(f"\n  ── 按 {lab} 三分（切点 {lo:.1f} / {hi:.1f}）──")
        for nm, sel in (("低量窗", lambda v: v <= lo),
                        ("中量窗", lambda v: lo < v <= hi),
                        ("高量窗", lambda v: v > hi)):
            line(nm, [r for r in sigs if sel(r[key])], width=10)
        print(f"  {'':<10} —— 其中触发止损的笔数 / 杀赢数 ——")
        for nm, sel in (("低量窗", lambda v: v <= lo),
                        ("中量窗", lambda v: lo < v <= hi),
                        ("高量窗", lambda v: v > hi)):
            g = [r for r in sigs if sel(r[key])]
            f = [r for r in g if r["stopped"]]
            k = [r for r in f if r["settle_won"]]
            print(f"  {nm:<10} 触发 {len(f):>3}  杀赢 {len(k):>2}  "
                  f"救输 {len(f) - len(k):>2}  杀赢率 {len(k) / len(f) * 100 if f else 0:5.1f}%")

    # ── 三b、按用户判据：杀赢/救输比 vs 临界比 ────────────────────────────
    print("\n" + "=" * 100)
    print("三b、用**用户判据**重述（临界比 = 杀赢一次亏的 ÷ 救输一次省的）")
    print("=" * 100)
    print("  出场价 p̄ ⇒ 救输省下 `shares·p̄`、杀赢多亏 `shares·(1−p̄)`")
    print("  ⇒ 打平所需 救输:杀赢 = (1−p̄) : p̄。实际比值高于临界 = 止损划算。")
    allp = [r["stopped"][2] for r in fire]
    pbar = sum(allp) / len(allp)
    print(f"\n  全样本：平均出场价 p̄ = {pbar:.3f} ⇒ 临界比 {1 - pbar:.2f} : {pbar:.2f} "
          f"= {(1 - pbar) / pbar:.1f} : 1；实际 {len(saved)} : {len(killed)} "
          f"= {len(saved) / max(1, len(killed)):.1f} : 1")
    print(f"\n  {'档位':<12}{'触发':>5}{'杀/救':>9}{'p̄':>8}{'临界比':>9}{'实际比':>9}"
          f"{'每笔救输':>10}{'每笔杀赢':>10}{'Δ':>8}")
    vals = sorted(r["_Vrate"] for r in sigs)
    lo, hi = q(vals, 1 / 3), q(vals, 2 / 3)
    for nm, sel in (("低量窗", lambda v: v <= lo),
                    ("中量窗", lambda v: lo < v <= hi),
                    ("高量窗", lambda v: v > hi)):
        g = [r for r in sigs if sel(r["_Vrate"])]
        f = [r for r in g if r["stopped"]]
        k = [r for r in f if r["settle_won"]]
        s = [r for r in f if not r["settle_won"]]
        if not f:
            continue
        pb = sum(r["stopped"][2] for r in f) / len(f)
        crit = (1 - pb) / pb
        act = f"{len(s) / len(k):.1f}:1" if k else "∞ (0 杀赢)"
        per_s = (sum(r["shares"] * r["stopped"][2] for r in s) / len(s)) if s else 0
        per_k = (sum(r["shares"] * (1 - r["stopped"][2]) for r in k) / len(k)) if k else 0
        dl = sum(r["pnl_stop"] - r["pnl_hold"] for r in g)
        print(f"  {nm:<12}{len(f):>5}{len(k):>4}/{len(s):<4}{pb:>8.3f}{crit:>8.1f}:1"
              f"{act:>12}{per_s:>10.3f}{per_k:>10.3f}{dl:>+8.2f}")
    print("\n  ⚠️ 两个陷阱（这档的 Δ 都不能当结论）:")
    print("    ① 低量窗 8 笔 0 杀赢是**样本量**不是优势——按全局 7.4% 的杀赢率，")
    print("       8 笔里出现 0 杀赢的概率 ≈ 54%, 完全在噪声内;")
    print("    ② 更要命的是它的 p̄ = 0.044 ⇒ 临界比 **21.8:1**——止损在低量窗触发时")
    print("       价格已经塌到 0.04, 每笔救输只省 0.098U, 而 1 笔杀赢要亏 ~2.1U。")
    print("       ⇒ 8 笔救输全对才 +0.78U; 只要出现 **1 笔杀赢** 就翻负。")
    print("    ③ 真正稳定的是中/高量窗: p̄≈0.15、临界比 5.5:1、实际比 8.7~14.5:1,")
    print("       留出 1.6~2.6 倍余量 —— 这才是那 +10.44U 的来源。")

    # ── 四、作为开关：只在某类窗里止损 ────────────────────────────────────
    print("\n" + "=" * 100)
    print("四、作为开关：只在成交量满足条件的窗里止损（因果量，逐档扫描）")
    print("=" * 100)
    print("  开关 = 「成交量满足条件时止损照常，否则不止损」——未触发止损的行 Δ 恒 0，")
    print("  故 P&L 列 = 该开关下的实际总收益，Δ = 它减基线 +35.67U。")
    for lab, key in (("V_win 整窗总量（后验, 仅作上界参考）", "_Vwin"),
                     ("V_rate 止损时速率（因果）", "_Vrate")):
        vals = sorted(r[key] for r in sigs)
        for op, cmp_, init in ((">", lambda v, t: v > t, lambda p: q(vals, p) if p else -1.0),
                               ("<", lambda v, t: v < t, lambda p: q(vals, p) if p else 1e18)):
            print(f"\n  ── {lab}　开关 = 成交量 {op} 阈值 ──")
            print(f"  {'阈值(分位)':<16}{'触发':>5}{'杀赢':>6}{'救输':>6}"
                  f"{'P&L':>11}{'Δ':>9}{'Δ的95%CI':>20}")
            for p in (.0, .25, .50, .75, .90):
                th = init(p)
                sel = [r for r in sigs if cmp_(r[key], th)]
                for r in sel:
                    st = r["stopped"]
                    r["_pnl_gated"] = (r["shares"] * st[2] - STAKE) if st else r["pnl_hold"]
                pnl = sum(r["_pnl_gated"] for r in sel)
                delta = pnl - sum(r["pnl_hold"] for r in sel)
                f = [r for r in sel if r["stopped"]]
                k = [r for r in f if r["settle_won"]]
                ci = boot_gated(sel) or (0.0, 0.0)
                tag = "无开关" if p == 0 else f"{op}{round(th, 1)} (p{int(p * 100)})"
                print(f"  {tag:<16}{len(f):>5}{len(k):>6}{len(f) - len(k):>6}"
                      f"{pnl:>11.2f}{delta:>+9.2f}   [{ci[0]:+.2f}, {ci[1]:+.2f}]")

    # ── 六、砸盘量比（用户假设：被砸那一刻的连续放量）─────────────────────
    print("\n" + "=" * 100)
    print("六、砸盘量比：**被砸那一刻**最近 k 秒的量 vs 砸盘之前的基准速率")
    print("=" * 100)
    print("  用户假设：被砸前 20 BTC、然后连着砸 40 BTC ⇒ 可能真有事件 ⇒ 止损更该做。")
    print("  前几节测的是整窗总量（一个静态的窗口属性）, 这里测的是**砸盘那一刻的动态**。")
    print("  ⚠️ 基准速率**不含**被砸的那 k 秒（否则放量会把自己归一化掉, 正是第一节")
    print("     那个 30s 版本失效的原因）——基准 = 窗口开头到 `rem+k` 为止的平均每秒量。")

    def burst_at(e, rem, k, bid_now, bid_key):
        """(放量倍数, 这 k 秒的 bid 跌幅)。基准 = 被砸之前那段的平均每秒量。"""
        vr = e["_vrows"]
        v_now, _ = vol_cum(vr, rem)
        v_prev, el_prev = vol_cum(vr, rem + k)
        recent = v_now - v_prev
        base_rate = v_prev / el_prev                      # BTC/秒（砸盘前）
        ratio = (recent / k) / base_rate if base_rate > 0 else None
        # bid 跌幅：本 tick 的 bid 减 k 秒前那一 tick 的 bid（时间上更早 = rem 更大）
        prev_bid = None
        for x in e["ticks"]:
            if x.get("rem") is None:
                continue
            if x["rem"] <= rem + k:
                break
            pm = x.get("pm") or {}
            if (pm.get("book_latency_ms") or 0) <= MAX_LAT:
                prev_bid = pm.get(bid_key)
        drop = (prev_bid - bid_now) if prev_bid else None
        return ratio, drop

    def attach_burst(e, r, k):
        t = r["stopped"]
        if not t:
            r["_burst"], r["_drop"] = None, None
            return
        r["_burst"], r["_drop"] = burst_at(
            e, t[0], k, t[2], r["side"] + "_bid")

    for k in (10, 15, 20, 30, 60):
        for r in sigs:
            attach_burst(r["_e"], r, k)
        a_r = auc([r["_burst"] for r in saved if r["_burst"] is not None],
                  [r["_burst"] for r in killed if r["_burst"] is not None])
        a_d = auc([r["_drop"] for r in saved if r["_drop"] is not None],
                  [r["_drop"] for r in killed if r["_drop"] is not None])
        med_k = q([r["_burst"] for r in killed if r["_burst"] is not None], .5)
        med_s = q([r["_burst"] for r in saved if r["_burst"] is not None], .5)
        print(f"  k={k:>3}s  AUC(量比) {a_r:.3f}   AUC(bid跌幅) {a_d:.3f}   "
              f"杀赢中位量比 {med_k:.2f}×  救输中位 {med_s:.2f}×"
              if a_r is not None else f"  k={k:>3}s  n/a")

    # ── 六b、同样的量比，换一个**有统计力**的靶子 ────────────────────────
    print("\n" + "=" * 100)
    print("六b、量比有没有判别力？——换成样本量足够的靶子重测")
    print("=" * 100)
    print("  ⚠️ 上面那张表只有 5 个杀赢 ⇒ AUC 标准误 0.13, 什么都测不出来。")
    print("  换个问法：**持仓期间任何一次「跌破 B」的时刻**, 量比能否预测这一笔最终输?")
    print("  样本量上去了（跌破 0.60 的有几百笔）, 且这正是止损要做的那个判断。")

    def first_break(e, r, level):
        """入场后首个 bid < level 的 tick → (rem, bid, 该 tick 在 ticks 里的下标)。"""
        for i in range(r["i"] + 1, len(e["ticks"])):
            x = e["ticks"][i]
            rem = x.get("rem")
            if rem is None or rem <= 0:
                continue
            pm = x.get("pm") or {}
            if not pm or (pm.get("book_latency_ms") or 0) > MAX_LAT:
                continue
            if not (x.get("bin") or {}).get("price"):
                continue
            b = pm.get(r["side"] + "_bid") or 0.0
            if 0 < b < level:
                return rem, b
        return None

    for lvl in (0.60, 0.50, 0.40, 0.30):
        bk = [r for r in sigs if first_break(r["_e"], r, lvl)]
        for k in (20,):
            pos = [r for r in bk if not r["settle_won"]]
            neg = [r for r in bk if r["settle_won"]]
            for lab, key in (("量比", "_burst"), ("bid跌幅", "_drop"), ("V_win", "_Vwin")):
                if key in ("_burst", "_drop"):
                    for r in bk:
                        rem, b = first_break(r["_e"], r, lvl)
                        r["_b2"], r["_d2"] = burst_at(
                            r["_e"], rem, k, b, r["side"] + "_bid")
                    vals_p = [r["_b2"] if key == "_burst" else r["_d2"] for r in pos]
                    vals_n = [r["_b2"] if key == "_burst" else r["_d2"] for r in neg]
                else:
                    vals_p, vals_n = [r[key] for r in pos], [r[key] for r in neg]
                a = auc([v for v in vals_p if v is not None],
                        [v for v in vals_n if v is not None])
                print(f"  跌破 {lvl:.2f} 的 {len(bk):>3} 笔（真输 {len(pos):>3} / 最终赢 {len(neg):>3}）"
                      f"  {lab:<8} AUC {a:.3f}")
        print()

    # ── 六c、效应量：把 0.60 那一档的判别力摊平成胜率 ────────────────────
    print("=" * 100)
    print("六c、效应量（跌破 0.60 的 179 笔，按砸盘量比分三档看最终胜率）")
    print("=" * 100)
    lvl, k = 0.60, 20
    bk = []
    for r in sigs:
        fb = first_break(r["_e"], r, lvl)
        if not fb:
            continue
        rem, b = fb
        ratio, drop = burst_at(r["_e"], rem, k, b, r["side"] + "_bid")
        if ratio is None:
            continue
        bk.append((ratio, drop, r))
    rs = sorted(x[0] for x in bk)
    lo, hi = q(rs, 1 / 3), q(rs, 2 / 3)
    print(f"  量比切点 {lo:.2f}× / {hi:.2f}×（k={k}s）；样本 {len(bk)} 笔\n")
    print(f"  {'档位':<14}{'n':>5}{'最终输':>8}{'最终赢':>8}{'输率':>9}{'该档均量比':>12}")
    for nm, sel in (("低量比", lambda v: v <= lo), ("中量比", lambda v: lo < v <= hi),
                    ("高量比", lambda v: v > hi)):
        g = [x for x in bk if sel(x[0])]
        lose = sum(1 for x in g if not x[2]["settle_won"])
        win = len(g) - lose
        print(f"  {nm:<14}{len(g):>5}{lose:>8}{win:>8}{lose / len(g) * 100:>8.1f}%"
              f"{sum(x[0] for x in g) / len(g):>11.2f}×")
    print("\n  ⚠️ 读法：这是「跌破 0.60 之后最终输」的比例。若高量比档显著更高，")
    print("     说明放量砸盘确实带着信息——但那也只是 **1.6σ** 的边缘信号（AUC 0.574），")
    print("     且它在降到 0.50/0.40/0.30 时**逐级衰减到噪声**（AUC 0.532/0.493/0.482），")
    print("     而止损恰恰是在 0.30 那一刻做的决定。")

    # ── 五、逐笔明细 ──────────────────────────────────────────────────────
    if args.dump:
        print("\n" + "=" * 100)
        print("五、被止损的 68 行逐笔（按 V_win 排序；`官方赢` = 杀错）")
        print("=" * 100)
        print("  date       stage  side fill  rem   dev   bid    V_win    V_rate  结果")
        for r in sorted(fire, key=lambda x: x["_Vwin"]):
            rem, dev, b, _ = r["stopped"]
            print(f"  {r['date']} {r['stage']:<6} {r['side']:<4} {r['fill']:.2f} "
                  f"{rem:>4.0f} {dev:>+7.1f} {b:>5.3f} {r['_Vwin']:>8.1f} "
                  f"{r['_Vrate']:>7.2f}  {'★官方赢' if r['settle_won'] else '输'}")

    # ── 结论 ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("结论")
    print("=" * 100)
    print("""
  ── 问题 1：能不能从 tick 聚合出整窗 BTC 总成交量？──
  ✅ 能，且是现成的：`V_win = Σ(bin.buy_vol + bin.sell_vol)`（单位 BTC）。
     信号窗 p10/p50/p90 = 23.8 / 66.0 / 204.9，极差 598× —— 不是常数，有信息量。

  ── 问题 2：对止损有帮助吗？──
  ❌ 没有可用的帮助。三条独立证据一致：

  (1) **对入场没有预测力**：ρ(V_win, 结算结果) = −0.023。
      它不是信号（这也解释了为什么它当不了止损的判别特征）。
  (2) **与 σ 腿半冗余**：ρ(V_win, sd) = +0.579。本窗实现的活动量有近 6 成
      被「前 18 窗的历史振幅」解释了；剩下那部分没带来判别力。
  (3) **判别力极弱**：触发止损的 68 笔里，杀赢 5 笔的 V_win 中位 118.3 vs
      救输 63 笔中位 86.7（AUC 0.384）——方向是**高量窗更容易杀错**，
      与「放量窗位移更真」的直觉相反；但 5 个事件撑不起这个方向。

  ── 问题 3：用户假设「砸盘量比」（被砸前 20 BTC、然后连砸 40 BTC）──
  ⚠️ 方向**对**，但强度**不够、且正好在止损那一刻消失**。这是本脚本最值得看的结果：

      跌破价位    样本      量比 AUC     bid 跌幅 AUC
      0.60       179      0.574         0.583     ← 高量比 ⇒ 更容易是真输（用户说对了）
      0.50       148      0.532         0.557
      0.40       128      0.493         0.522
      0.30       112      0.482        0.484     ← 止损就在这里做决定 ⇒ **纯噪声**

     · 效应量（六c，跌破 0.60 的 179 笔按量比分三档）: 高量比档最终输 **60.0%**、
       低量比档 **43.3%** —— 差 16.7pp，1.8σ，p ≈ 0.06，**不显著**；
     · 更要命的是它**逐级衰减**：0.574 → 0.532 → 0.493 → 0.482。被砸得越深，
       量比的解释力越弱；而止损的触发点恰好在最深的 0.30 那一档。
     · 道理也说得通: 砸到 0.60 时「是不是真有事」还有悬念, 放量确实是信息;
       砸到 0.30 时**事情已经发生完了**, 量比早被价格吸收干净。

     ⇒ 用「砸盘量比」提高止损精度: **做不到**。它测得出「这次砸盘凶不凶」,
       但测不出「砸完之后会不会弹回来」—— 而后者才是止损要判的东西。

  ── 问题 4：那能不能当开关（只在某类窗里止损）？──
  ❌ 每一种切法都比不切差或持平（第四节全表）：
     · 只在**高**量窗止损：Δ 从 +10.44 掉到 +1.67(p75) / −0.35(p90)，CI 全部含 0；
     · 只在**低**量窗止损：Δ 只剩 +0.76(p25)，因为 68 笔里 61 笔被关掉了；
     · 最好的一个（`<p90`，+10.78）只比不切好 0.34U —— 挪掉 11 笔触发换来的，
       在 5 个杀赢的样本上等于零。

  ── 顺带浮现的一条线索（是**另一条策略**, 不在本次范围）──
  六b 顺带量到: 持仓期间跌破 0.60 之后, 最终输的概率是 **48%（86/179）**。
  在 0.60 出场 vs 死扛的赔率结构完全不同 —— 出场价 p 处 临界比 = (1−p):p,
  0.60 处只有 **0.67:1**（vs 止损在 0.30 的 5.5:1）: 杀错一次只亏 0.40/股,
  救对一次省 0.60/股, 几乎对半开就够本。这与「止损在 0.30」是两个数量级上
  不同的赌注。⚠️ 但它是**早退策略**、不是止损腿, 且同样吃决策 #21 的对手方上界
  （0.60 处还有没有买盘, 同样未经验证）⇒ 须单独立项, 不要顺手加。

  ── 真正学到的一件事（值得记，但不是这个量）──
  第四节那张临界比表暴露了一个**先前没注意到**的结构：止损的性价比由**出场价 p̄**
  决定，而 p̄ 随窗口活动水平变化——低量窗 p̄=0.044（临界比 21.8:1），中/高量窗
  p̄≈0.15（临界比 5.5:1）。低量窗里止损触发时价格已经塌到 0.04，**救也救不回多少**
  （每笔 +0.098U），却仍要承担杀赢的风险（每笔 −2.1U）。全样本 6.2:1 的余量
  （12.6:1 实际）主要由中/高量窗贡献。

  ── 该做什么 ──
  什么都不做。成交量维度上**没有可落地的改动**：它不改善止损的选择性，当开关也没用。
  零假设（成交量与止损无关）没有被数据推翻 —— 但同样地，也没有任何证据支持用它。
  ⚠️ 样本上界：止损 68 次 / 杀赢 5 次，且 26 号文件头的两条上界（历史数据持仓侧
  bid 恒 >0 = 构造性、无滑点）原样适用 —— 本脚本是**机制筛查**，不是判决。
""")


if __name__ == "__main__":
    main()
