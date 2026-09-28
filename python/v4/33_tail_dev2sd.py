#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫尾盘: 在 **T150→T60 的空窗**里逐秒查 `dev > 2·sd ∧ sd > 63` —— 有没有正 EV
（2026-09-29 用户提问, 2026-09-29 澄清后重做）

**题面**（用户两次表述合并后的准确读法）:

    三段递进判定链**一字不动**（T=150 首个可判定 tick 判 ⑤ → T=60 再判 ⑤ → 此后每秒判 ②）,
    只在「T=150 段被拒之后、T=60 段之前」这段**现在什么都不判的空档**里, **逐秒**追加
    一条检查: 价格腿 ≥ 0.80 ∧ dev > 2·sd ∧ sd > 63 → 达标即下单。
    已经出信号的窗跳过（链本身在首个信号处终止, 天然满足）。

    即: 它**不是**替换判定腿, 而是**新增检查点**——「提前入场」家族
    （脚本 29/31 用 ⑤ 本体试过 T70~135 的固定检查点, 本脚本把判据换成题面那条）。

符号（全脚本统一）:
    dev = sgn·(spot − anchor), 单位美元, 正 = 朝押注方向（押 yes +1 / 押 no −1）
    sd  = 该窗 1σ 的美元值 = hist_bps × anchor / 1e4
    sig = dev / sd（位移等于几个 σ）
    EV/注 = 每注（2U）的期望盈亏, 单位 U（1U = 1 美元）

其余口径（宇宙 / σ / 成交 / P&L）与 `23_tail_integrated.py` 逐条同源:
    可判定 tick = 延迟 ≤ 300ms ∧ 热门侧有效价 > 0 ∧ spot > 0 ∧ rem > 0;
    σ 未就绪（< 3 窗）整窗跳过; 迟到的窗（首个可判定 tick 已在 rem ≤ 60）没有空窗 ⇒ 不查;
    fill = 下单 tick 热门侧有效价, shares = 2/fill; 赢 +（shares−2）, 输 −2。

用法: python/venv/bin/python python/v4/33_tail_dev2sd.py
"""
import sys
import glob
import json
import random
import datetime
import collections
import importlib.util
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))
from v2.lib import load_events                                    # noqa: E402

STAKE = 2.0
BOOT = 2000
SEED = 42
K_NEW = 2.0            # 题面: dev > 2·sd
SD_NEW = 63.0          # 题面: sd > 63 美元
T150, T60 = 150, 60
STAGES = ("t150", "gap", "t60", "listen")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


s13 = load("s13", BASE / "13_tail_sweep.py")
s14 = load("s14", BASE / "14_tail_sweep_sigma.py")
s23 = load("s23", BASE / "23_tail_integrated.py")


# ── 三段链 + 空窗检查（判定腿以参数注入, 其余照抄 23）────────────────────

def chain(ticks, anchor, sd, date, outcome, rules, gap_rule=None, gap_max=T150):
    """三段递进判定链, 可选在空窗插一条逐秒检查。

    rules    = (rule150, rule60, rulelisten), 各自是**含价格腿在内的完整谓词**
               （基线 = s23.r5(strict) / s23.r5 / s23.r2）。
    gap_rule = 空窗谓词（None = 不检查）; gap_max = 空窗里只查 rem ≤ gap_max 的 tick。
    返回 (信号行 或 None, 空窗里实际判定过的 tick 数)。
    """
    if not ticks:
        return None, 0
    head = ticks[0]
    n_gap = 0
    if head["rem"] > T60:
        r = s23.row(head, anchor, sd, date, outcome, "t150")
        if rules[0](r):
            r["ok"] = True
            return r, 0
        if gap_rule is not None:
            # 空窗 = rem ∈ (60, 本窗首个可判定 tick 的 rem], 时间顺序、逐秒
            for x in ticks[1:]:
                if x["rem"] <= T60:
                    break
                if x["rem"] > gap_max:
                    continue
                rg = s23.row(x, anchor, sd, date, outcome, "gap")
                n_gap += 1
                if gap_rule(rg):
                    rg["ok"] = True
                    return rg, n_gap
        rest = [x for x in ticks if x["rem"] <= T60]
    else:
        rest = ticks                      # 迟到接入: 无 t150 段、无空窗, 不伪造行
    if not rest:
        return None, n_gap
    r = s23.row(rest[0], anchor, sd, date, outcome, "t60")
    if rules[1](r):
        r["ok"] = True
        return r, n_gap
    for x in rest[1:]:
        rl = s23.row(x, anchor, sd, date, outcome, "listen")
        if rules[2](rl):
            rl["ok"] = True
            return rl, n_gap
    return None, n_gap


def price_leg(r, strict):
    """价格腿（与 23 同源）: T=150 段严格 > 0.80, 其余段 ≥ 0.80（决策 #26）。"""
    return r["fill"] > s23.P_FLOOR if strict else r["fill"] >= s23.P_FLOOR


def leg_new(r, k=K_NEW, sd_min=SD_NEW):
    """题面腿: dev > k·sd 且 sd > sd_min（美元, 两处都是严格大于）。"""
    return (r["sd"] is not None and r["sd"] > sd_min
            and r["sig"] is not None and r["sig"] > k)


def rule_gap(k=K_NEW, sd_min=SD_NEW, strict=False):
    """空窗检查 = 价格腿 ∧ dev > k·sd ∧ sd > sd_min。"""
    return lambda r: price_leg(r, strict) and leg_new(r, k, sd_min)


# 基线三段: 现行 ⑤（t150 严格价）/ ⑤ / ②; 空窗用 ⑤ 本体（= 脚本 29/31 的追加检查点）
BASE_RULES = (lambda r: s23.r5(r, strict_price=True), s23.r5, s23.r2)
R5_GAP = lambda r: s23.r5(r)                                     # noqa: E731


def run(ev, hr, rules, gap_rule=None, gap_max=T150):
    """返回 (信号行列表, 逐窗映射 start_time → 行, 空窗判定 tick 总数)。"""
    sigs, by_win, ngap = [], {}, 0
    for e in ev:
        outcome, anchor = e.get("outcome"), e.get("twap_open_price")
        if outcome is None or not anchor:
            continue
        h = hr.get(e["start_time"])
        if h is None:
            continue                      # σ 未就绪整窗跳过（决策 #13）
        sd = h / anchor * 1e4 * anchor / 1e4
        date = datetime.datetime.fromtimestamp(
            e["start_time"], datetime.timezone.utc).strftime("%Y-%m-%d")
        ticks = s23.win_ticks(e)
        if not ticks:
            continue
        r, n = chain(ticks, anchor, sd, date, outcome, rules, gap_rule, gap_max)
        ngap += n
        if r is not None:
            r["win"] = e["start_time"]
            sigs.append(r)
            by_win[e["start_time"]] = r
    return sigs, by_win, ngap


# ── 统计工具 ──────────────────────────────────────────────────────────────

def pl(rows):
    return sum((STAKE / r["fill"] - STAKE) if r["settle_won"] else -STAKE for r in rows)


def by_day(rows):
    d = {}
    for r in rows:
        d.setdefault(r["date"], []).append(r)
    return d


def ci(rows, rng):
    """日级 bootstrap 95%（复用 14 的口径: 抽日期、保留日内全部注）。"""
    if len(rows) < 5:
        return None
    ds = sorted(set(r["date"] for r in rows))
    (lo, hi), _ = s14.day_bootstrap(rows, ds, BOOT, rng)
    return lo, hi


def paired_ci(base_map, var_map, rng):
    """配对 Δ = 变体 P&L − 基线 P&L 的日级 bootstrap 95%（判决口径, 决策 #24）。

    逐窗配对: 同一窗两边各算一笔（一边没有就是 0）, 按日汇总后重采样日期。
    """
    days = sorted(set(r["date"] for r in base_map.values())
                  | set(r["date"] for r in var_map.values()))
    pb, pv = {}, {}
    for w, r in base_map.items():
        pb[w] = (STAKE / r["fill"] - STAKE) if r["settle_won"] else -STAKE
    for w, r in var_map.items():
        pv[w] = (STAKE / r["fill"] - STAKE) if r["settle_won"] else -STAKE
    d = collections.defaultdict(float)
    for w, v in pb.items():
        d[base_map[w]["date"]] += pv.get(w, 0.0) - v
    for w, v in pv.items():
        if w not in pb:
            d[var_map[w]["date"]] += v
    vec = [d[x] for x in days]
    if len(days) < 5:
        return None
    out = []
    for _ in range(BOOT):
        out.append(sum(vec[rng.randrange(len(vec))] for _ in days))
    out.sort()
    q = lambda a, p: a[int(p * len(a))]                          # noqa: E731
    return q(out, 0.025), q(out, 0.975)


def line(lab, rows, rng, width=26):
    if not rows:
        print("  " + lab.ljust(width) + "n=0")
        return
    n = len(rows)
    wr = sum(r["settle_won"] for r in rows) / n
    px = sum(r["fill"] for r in rows) / n
    d = by_day(rows)
    neg = sum(1 for v in d.values() if pl(v) < 0)
    c = ci(rows, rng)
    ctxt = f"  P&L 95%CI [{c[0]:+.1f},{c[1]:+.1f}]" if c else ""
    print("  " + lab.ljust(width) + f"n={n:<5} WR {wr*100:6.2f}%  均价 {px:.4f}  "
          f"EV/注 {pl(rows)/n:+.4f}U  P&L {pl(rows):+7.2f}U  亏损日 {neg}/{len(d)}{ctxt}")


# ── 主流程 ────────────────────────────────────────────────────────────────

def main():
    ev = load_events("data/btc")
    hr = s13.hist_ranges(ev)
    rng = random.Random(SEED)

    base_sigs, base_map, _ = run(ev, hr, BASE_RULES)
    GAP = rule_gap()
    gap_sigs, gap_map, ngap = run(ev, hr, BASE_RULES, gap_rule=GAP)

    print("=" * 108)
    print("一、题面：T150 段被拒后、T60 段之前，**逐秒**查 价格≥0.80 ∧ dev>2·sd ∧ sd>63")
    print("=" * 108)
    line("基线（现行三段链）", base_sigs, rng)
    line("题面（三段链 + 空窗检查）", gap_sigs, rng)
    p = paired_ci(base_map, gap_map, rng)
    print(f"\n  配对 Δ（题面 − 基线）P&L: 点估计 {pl(gap_sigs)-pl(base_sigs):+.2f}U, "
          f"日级 bootstrap 95%CI [{p[0]:+.1f}, {p[1]:+.1f}]U")
    print(f"  空窗里实际判定过的 tick 总数为 {ngap}（每窗最多 ~90 个 = rem 150→60 的秒数）")
    print("\n  按段分解（题面这一列的信号落在哪一段）:")
    for s in STAGES:
        line(f"    {s}", [r for r in gap_sigs if r["stage"] == s], rng, width=22)

    # ── 二、新增 / 改道 ──────────────────────────────────────────────────
    print("\n" + "=" * 108)
    print("二、分解：空窗抢在 T=60 之前入场的（改道）vs 基线整窗本来不下注的（新增）")
    print("=" * 108)
    moved, added = [], []          # (基线那一笔, 空窗那一笔) / 空窗那一笔
    for w, v in gap_map.items():
        if v["stage"] != "gap":
            continue               # 入场点没变（仍在 t60 / 监听段）⇒ 不参与分解
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

    # ── 三、稳健性 ───────────────────────────────────────────────────────
    print("\n" + "=" * 108)
    print("三、稳健性 A: 空窗判据的阈值网格（单元格 = 总 P&L U / 空窗段 n / 空窗段 EV每注U）")
    print("=" * 108)
    print("  " + "sd下限".ljust(10) + "".join(f"K={k:g}".ljust(24)
                                           for k in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0)))
    for sd_min in (0.0, 40.0, 50.0, 63.0, 75.0, 90.0):
        cells = []
        for k in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0):
            sigs, _, _ = run(ev, hr, BASE_RULES, gap_rule=rule_gap(k, sd_min))
            gs = [r for r in sigs if r["stage"] == "gap"]
            tot = pl(sigs)
            if not gs:
                cells.append(f"{tot:+7.1f}/0/—".ljust(24))
                continue
            cells.append(f"{tot:+7.1f}/{len(gs):>4}/{pl(gs)/len(gs):+.3f}".ljust(24))
        tag = "  ←题面" if sd_min == SD_NEW else ""
        print("  " + f"{sd_min:g}".ljust(10) + "".join(cells) + tag)
    _sds = sorted(r["sd"] for r in base_sigs)
    print(f"  （基线信号 sd 中位 = {_sds[len(_sds)//2]:.1f} 美元 ⇒ 63 这一档 ≈ 波动高于中位;")
    print("    基线总 P&L = +%.2fU, 空窗段 = 抢在 T=60 前入场的那些笔）" % pl(base_sigs))

    print("\n" + "=" * 108)
    print("四、稳健性 B: 空窗检查的**时间范围**（题面 K=2 / sd>63; 与 29/31 的固定检查点同族）")
    print("=" * 108)
    print(f"  {'空窗范围':<24}{'总n':>7}{'总P&L':>10}{'空窗段n':>9}{'空窗段P&L':>11}"
          f"{'空窗段均价':>11}{'配对Δ 95%CI':>20}")
    for lab, rule, gmax in (("rem ≤ 150（题面）", GAP, T150),
                            ("rem ≤ 120", GAP, 120),
                            ("rem ≤ 90", GAP, 90),
                            ("rem ≤ 70", GAP, 70),
                            ("rem ≤ 61", GAP, 61),
                            ("rem ≤ 150, 价格 > 0.80", rule_gap(strict=True), T150)):
        sigs, mp, _ = run(ev, hr, BASE_RULES, gap_rule=rule, gap_max=gmax)
        gs = [r for r in sigs if r["stage"] == "gap"]
        pc = paired_ci(base_map, mp, rng)
        pct = f"[{pc[0]:+.1f},{pc[1]:+.1f}]U" if pc else "—"
        px = sum(r["fill"] for r in gs) / len(gs) if gs else 0.0
        print(f"  {lab:<24}{len(sigs):>7}{pl(sigs):>+10.2f}"
              f"{len(gs):>9}{pl(gs):>+11.2f}{px:>11.4f}{pct:>20}")
    print("  （rem ≤ 61 = 空窗几乎全程; rem ≤ 70 = 只在快进 T=60 的那几秒查;"
          " 最后一行把价格腿改成严格大于, 检验决策 #26 的比较符在空窗段要不要跟随）")

    print("\n" + "=" * 108)
    print("五、对照: 空窗判据换成**基线 ⑤ 本体**（= 29/31 已测的「提前入场」家族）")
    print("=" * 108)
    sigs5, mp5, _ = run(ev, hr, BASE_RULES, gap_rule=R5_GAP)
    gs5 = [r for r in sigs5 if r["stage"] == "gap"]
    line("基线", base_sigs, rng)
    line("空窗查 ⑤（非严格价）", sigs5, rng)
    line("  └ 空窗段", gs5, rng, width=22)
    p5 = paired_ci(base_map, mp5, rng)
    print(f"  配对 Δ: 点估计 {pl(sigs5)-pl(base_sigs):+.2f}U, "
          f"95%CI [{p5[0]:+.1f}, {p5[1]:+.1f}]U")

    # ── 六、机制 ─────────────────────────────────────────────────────────
    print("\n" + "=" * 108)
    print("六、机制: 空窗抢跑把入场提前了多少")
    print("=" * 108)
    if moved:
        dr = [b_["rem"] - v["rem"] for b_, v in moved]
        dp = [b_["fill"] - v["fill"] for b_, v in moved]
        print(f"  改道 {len(moved)} 笔: rem 提前中位 {sorted(dr)[len(dr)//2]:.0f} 秒"
              f"（p10 {sorted(dr)[len(dr)//10]:.0f} / p90 {sorted(dr)[9*len(dr)//10]:.0f}）;"
              f" 价格便宜中位 {sorted(dp)[len(dp)//2]:+.4f}"
              f"（均价 {sum(x[0]['fill'] for x in moved)/len(moved):.4f} → "
              f"{sum(x[1]['fill'] for x in moved)/len(moved):.4f}）")
        print("  改道里基线原先落在: " + ", ".join(
            f"{s} {sum(1 for x in moved if x[0]['stage']==s)} 笔" for s in STAGES
            if any(x[0]["stage"] == s for x in moved)))
        print(f"  逐笔 Δ（空窗 − 基线）: 正 "
              f"{sum(1 for b_, v in moved if pl([v]) - pl([b_]) > 1e-9)} 笔, "
              f"负 {sum(1 for b_, v in moved if pl([v]) - pl([b_]) < -1e-9)} 笔, "
              f"平 {sum(1 for b_, v in moved if abs(pl([v]) - pl([b_])) <= 1e-9)} 笔")
        print("\n  空窗抢跑 30 笔明细：" if len(moved) == 30 else "\n  空窗抢跑明细：")
        print(f"    {'日期':<12}{'rem':>5}{'侧':>5}{'成交价':>8}{'dev(美元)':>10}"
              f"{'sd(美元)':>9}{'sig':>6}   基线原先")
        for b_, v in sorted(moved, key=lambda x: (x[1]["date"], -x[1]["rem"])):
            print(f"    {v['date']:<12}{v['rem']:>5}{v['side']:>5}{v['fill']:>8.4f}"
                  f"{v['dev']:>10.1f}{v['sd']:>9.1f}{v['sig']:>6.2f}   "
                  f"{b_['stage']} 段 rem={b_['rem']} 价 {b_['fill']:.4f} "
                  f"({'赢' if b_['settle_won'] else '输'})")
    if added:
        print(f"  新增 {len(added)} 笔的画像: 均价 "
              f"{sum(r['fill'] for r in added)/len(added):.4f}, sig 中位 "
              f"{sorted(r['sig'] for r in added)[len(added)//2]:.2f}, sd 中位 "
              f"{sorted(r['sd'] for r in added)[len(added)//2]:.1f} 美元")

    # ── 七、实盘/纸面交叉核对 ─────────────────────────────────────────────
    print("\n" + "=" * 108)
    print("七、实盘/纸面样本交叉核对（data/tail-live, 09-24~28; 10U/注 的 GTC 挂单成交样本）")
    print("=" * 108)
    live = []
    for f in sorted(glob.glob("data/tail-live/tail_2026-*.jsonl")):
        for ln in open(f):
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if r.get("kind") != "snap" or not r.get("ok"):
                continue
            live.append(r)
    filled = [r for r in live if r.get("exec_status") in ("filled", "partial")]
    st = live[0].get("stage")
    print(f"  09-24~28: 信号 {len(live)} 笔, 其中 GTC 真成交 {len(filled)} 笔"
          f"（exec_status=filled/partial, 10U/笔）")
    print("  实盘按段分布: " + ", ".join(
        f"{s} {sum(1 for r in live if r.get('stage')==s)} 笔" for s in ("t150", "t60", "listen")))
    print(f"  {'过滤条件':<28}{'信号n':>7}{'成交n':>7}{'成交WR':>9}{'成交P&L':>10}{'EV/成交笔':>11}")
    for k, sd_min in ((K_NEW, SD_NEW), (K_NEW, 0.0), (1.0, 0.0), (1.0, SD_NEW)):
        def hit(r):
            return (r.get("sd") and r.get("dev") is not None
                    and r["sd"] > sd_min and r["dev"] / r["sd"] > k)
        g = [r for r in live if hit(r)]
        gf = [r for r in filled if hit(r)]
        if not gf:
            print(f"  dev > {k:g}·sd ∧ sd > {sd_min:g}".ljust(28)
                  + f"{len(g):>7}{len(gf):>7}{'—':>9}{'—':>10}{'—':>11}")
            continue
        wr = sum(1 for r in gf if r.get("won")) / len(gf)
        pnl = sum(r.get("pnl") or 0 for r in gf)
        print(f"  dev > {k:g}·sd ∧ sd > {sd_min:g}".ljust(28)
              + f"{len(g):>7}{len(gf):>7}{wr*100:>8.1f}%{pnl:>+10.2f}{pnl/len(gf):>+11.4f}")
    print("  ⚠️ 实盘/纸面是**挂单等成交**样本, 与回测「快照瞬间即成交」不是同一个估计量"
          "（成交样本天然偏向热门侧走弱）, 只作方向性核对; 成交笔数少时噪声极大。")

    # ── 附: 上一轮的读法（全链替换）─────────────────────────────────────
    print("\n" + "=" * 108)
    print("附、对照: 把三段链的判定腿**整体换成**题面条件（上一轮的读法, 非本次题面）")
    print("=" * 108)
    nr = (lambda r: price_leg(r, True) and leg_new(r),
          lambda r: price_leg(r, False) and leg_new(r),
          lambda r: price_leg(r, False) and leg_new(r))
    sigs_r, mp_r, _ = run(ev, hr, nr)
    line("全链替换", sigs_r, rng)
    pr = paired_ci(base_map, mp_r, rng)
    print(f"  配对 Δ: 点估计 {pl(sigs_r)-pl(base_sigs):+.2f}U, "
          f"95%CI [{pr[0]:+.1f}, {pr[1]:+.1f}]U")


if __name__ == "__main__":
    main()
