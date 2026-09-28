#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫尾盘：T=150 段「单点检查」→「**从 T150 起每秒检查**」的敏感性（2026-09-28）

问题（用户提出）：段 1 只在首个可判定 tick（通常 rem=150）判一次 ⑤，此后到 rem≤60
**整整 90 秒是黑的**（见 29 的诊断）。若把段 1 改成**从 T=150 起每秒判 ⑤**（条件不变：
严格 `> 0.80` 的价格腿）, 到 T=60 再按 T60 的条件, 回测是否改善？

「到 T60 时才按 T60 的条件」有两种读法, 两种都跑（其余口径与 `23_tail_integrated.py` 逐字一致）:

  **读法 A**（最小改动）: 段 1 逐秒判 ⑤（严格 >0.80）→ 段 2/3 **原样不动**
      （首个 rem ≤ 60 的 tick 判 ⑤ 非严格 ≥, 拒绝后监听段每秒判 ②）。
  **读法 B**（逐秒到底）: 逐秒判 ⑤, 只是价格腿的算子按 rem 切换——rem > 60 用严格 `>`,
      rem ≤ 60 用非严格 `≥`。⑤ ⊇ ②（价格腿同算子时 ⑤ 的 dev ∨ σ 覆盖 ② 的 dev）,
      故**监听段被吸收、不再单独产出**。

  两者的差别**只在 rem ≤ 60 那一段**：A 的 t60 判定点是「首个 tick」（拒了就走 ② 监听）,
  B 是「此后每个 tick 都还能用 σ 腿再试一次」。

成交口径 / 统计口径与 23 恒等（2U/注, 赢 shares−STAKE / 输 −STAKE; 日级 bootstrap 2000 次, seed 42）。
⚠️ 行数含义变了：段 1 逐秒 ⇒ 被拒的 tick 也逐秒落行（每窗可达 ~90 行）。**落盘口径必须
按决策 #22 的先例处理**（监听段被拒 tick 不落行）——本脚本只统计信号行, 行数仅作参考。

用法: python/venv/bin/python python/v4/30_tail_t150_persec.py
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

SEED = 42
REPS = 2000
STAGES = ("t150", "t60", "listen")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


s13 = load("s13", BASE / "13_tail_sweep.py")
s23 = load("s23", BASE / "23_tail_integrated.py")
s29 = load("s29", BASE / "29_tail_t60_rem.py")     # 复用 pl / stat / line


# ── 两个候选链 ────────────────────────────────────────────────────────────

def chain_a(ticks, anchor, sd, date, outcome, t150=150, t60=60):
    """读法 A: 段 1 逐秒（T150 条件）; 段 2/3 = oracle 原样。

    返回 (rows, n_rejected_ticks_in_seg1)。
    """
    if not ticks:
        return [], 0
    rows, rej = [], 0
    if ticks[0]["rem"] > t60:
        for x in ticks:
            if x["rem"] <= t60:
                break
            r = s23.row(x, anchor, sd, date, outcome, "t150")
            if s23.r5(r, strict_price=True):
                r["ok"] = True
                return rows + [r], rej
            rows.append(r)
            rej += 1
        rest = [x for x in ticks if x["rem"] <= t60]
    else:
        rest = ticks                                   # 迟到接入（与 oracle 同）
    if rest:
        t2 = rest[0]
        r2r = s23.row(t2, anchor, sd, date, outcome, "t60")
        if s23.r5(r2r):
            r2r["ok"] = True
            return rows + [r2r], rej
        rows.append(r2r)
        for x in rest[1:]:
            rl = s23.row(x, anchor, sd, date, outcome, "listen")
            if s23.r2(rl):
                rl["ok"] = True
                return rows + [rl], rej
    return rows, rej


def chain_b(ticks, anchor, sd, date, outcome, t60=60):
    """读法 B: 逐秒判 ⑤, 价格腿算子按 rem 在 t60 处切换（>60 严格, ≤60 非严格）。"""
    if not ticks:
        return [], 0
    rows, rej = [], 0
    for x in ticks:
        st = "t150" if x["rem"] > t60 else "t60"
        r = s23.row(x, anchor, sd, date, outcome, st)
        if s23.r5(r, strict_price=x["rem"] > t60):
            r["ok"] = True
            return rows + [r], rej
        rows.append(r)
        rej += 1
    return rows, rej


VARIANTS = (("A 段1逐秒(其余原样)", chain_a), ("B 逐秒到底(算子按60切换)", chain_b))

BINS = ((0, 60, "≤60"), (61, 70, "61~70"), (71, 80, "71~80"), (81, 90, "81~90"),
        (91, 105, "91~105"), (106, 120, "106~120"), (121, 135, "121~135"),
        (136, 150, "136~150"))


def bin_of(rem):
    for lo, hi, lab in BINS:
        if lo <= rem <= hi:
            return lab
    return "?"


def main():
    ev = load_events("data/btc")
    hr = s13.hist_ranges(ev)

    windows = []
    skipped_no_sigma = 0
    for e in ev:
        outcome, anchor = e.get("outcome"), e.get("twap_open_price")
        if outcome is None or not anchor:
            continue
        h = hr.get(e["start_time"])
        if h is None:
            skipped_no_sigma += 1
            continue
        sd = h / anchor * 1e4 * anchor / 1e4
        date = datetime.datetime.fromtimestamp(
            e["start_time"], datetime.timezone.utc).strftime("%Y-%m-%d")
        ticks = s23.win_ticks(e)
        if not ticks:
            continue
        windows.append((e["start_time"], date, ticks, anchor, sd, outcome))

    # 基线（T60=60 的三段链）+ 两个候选
    base = {}
    for key, date, ticks, anchor, sd, outcome in windows:
        s23.T60 = 60
        base[key] = [r for r in s23.chain(ticks, anchor, sd, date, outcome)[0] if r.get("ok")]
    s23.T60 = 60
    res = {}
    for name, fn in VARIANTS:
        pw, rej_tot = {}, 0
        for key, date, ticks, anchor, sd, outcome in windows:
            rows, rej = fn(ticks, anchor, sd, date, outcome)
            rej_tot += rej
            pw[key] = [r for r in rows if r.get("ok")]
        res[name] = (pw, rej_tot)

    rng = random.Random(SEED)
    dates = sorted(set(d for _, d, _, _, _, _ in windows))

    def ci(delta):
        ds = sorted(delta)
        boot = sorted(sum(delta[rng.choice(ds)] for _ in ds) for _ in range(REPS))
        q = lambda p: boot[int(p * len(boot))]                            # noqa: E731
        return q(0.025), q(0.975)

    # ── 一、总览 ─────────────────────────────────────────────────────────
    print("\n" + "=" * 104)
    print("一、总览（14 天, 2U/注, σ 未就绪窗跳过）")
    print("=" * 104)
    line = s29.line
    line("基线: T150 单点（现行）", [r for v in base.values() for r in v], width=26)
    for name, (pw, rej) in res.items():
        line(name, [r for v in pw.values() for r in v], width=26)
    print(f"\n  参考窗数: 参与判定 {len(windows)} 窗; σ 未就绪跳过 {skipped_no_sigma} 窗")

    # ── 二、分段明细 ─────────────────────────────────────────────────────
    print("\n" + "=" * 104)
    print("二、分段明细（段 1 逐秒 ⇒ t150 段信号量暴增; 段 2/3 被抽走）")
    print("=" * 104)
    for name, (pw, rej) in res.items():
        print(f"\n  {name}    （段1 被拒 tick 合计 {rej} 个 = 若不落盘可省的行数）")
        for s in STAGES:
            line(f"    {s} 段信号", [r for v in pw.values() for r in v if r["stage"] == s])

    # ── 三、配对 Δ + bootstrap + 分半 + 不变式 ───────────────────────────
    print("\n" + "=" * 104)
    print("三、配对 Δ（逐窗 variant − 基线）与日级 bootstrap 95% 区间")
    print("=" * 104)
    half = len(dates) // 2
    h1, h2 = set(dates[:half]), set(dates[half:])
    print(f"  前半 {min(h1)} ~ {max(h1)}   后半 {min(h2)} ~ {max(h2)}")
    print(f"  {'变体':<26}{'ΔP&L':>10}{'ΔEV/注':>10}{'改善':>7}{'恶化':>7}"
          f"{'新增':>7}{'丢失':>7}{'前半':>9}{'后半':>9}{'95% CI':>22}")
    for name, (pw, _rej) in res.items():
        delta = collections.defaultdict(float)
        better = worse = new = lost = 0
        for key, date, _t, _a, _sd, _o in windows:
            b = base[key][0] if base[key] else None
            v = pw[key][0] if pw[key] else None
            db, dv = (s29.pl([b]) if b else 0.0), (s29.pl([v]) if v else 0.0)
            delta[date] += dv - db
            if b and v:
                if dv - db > 1e-9:
                    better += 1
                elif dv - db < -1e-9:
                    worse += 1
            elif v and not b:
                new += 1
            elif b and not v:
                lost += 1
        a, c = sum(delta[d] for d in h1), sum(delta[d] for d in h2)
        lo, hi = ci(delta)
        n_all = sum(1 for k in base if base[k] or pw[k])
        print(f"  {name:<26}{a+c:>+10.2f}{(a+c)/n_all:>+10.4f}{better:>7}{worse:>7}"
              f"{new:>7}{lost:>7}{a:>+9.2f}{c:>+9.2f}   [{lo:+.2f}, {hi:+.2f}]")
    print("\n  不变式: 「丢失」应为 0 —— 逐秒只会**更早**出信号, 不会让任何窗丢掉信号")

    # ── 四、按**入场 rem** 分桶（关键诊断）────────────────────────────────
    print("\n" + "=" * 104)
    print("四、按入场 tick 的 rem 分桶：变体信号 vs 同窗基线（Δ 就是这行的账）")
    print("=" * 104)
    for name, (pw, _rej) in res.items():
        print(f"\n  {name}")
        print(f"    {'入场 rem':<12}{'n':>6}{'WR':>9}{'均价':>9}{'EV/注':>10}"
              f"{'P&L(变体)':>12}{'P&L(基线同窗)':>15}{'Δ':>10}")
        buckets = collections.defaultdict(list)
        for key, date, _t, _a, _sd, _o in windows:
            v = pw[key][0] if pw[key] else None
            if v:
                buckets[bin_of(v["rem"])].append((key, v))
        for lo, hi, lab in BINS:
            g = buckets.get(lab)
            if not g:
                continue
            vs = [v for _k, v in g]
            bs = [base[k][0] for k, _v in g if base[k]]
            pv, pb = s29.pl(vs), s29.pl(bs)
            wr = sum(r["settle_won"] for r in vs) / len(vs) * 100
            px = sum(r["fill"] for r in vs) / len(vs)
            print(f"    {lab:<12}{len(vs):>6}{wr:>8.1f}%{px:>9.4f}{pv/len(vs):>+10.4f}"
                  f"{pv:>+12.2f}{pb:>+15.2f}{pv-pb:>+10.2f}")
        v_all = [pw[k][0] for k, *_ in windows if pw[k]]
        b_all = [base[k][0] for k, *_ in windows if base[k]]
        print(f"    {'合计':<12}{len(v_all):>6}"
              f"{sum(r['settle_won'] for r in v_all)/len(v_all)*100:>8.1f}%"
              f"{sum(r['fill'] for r in v_all)/len(v_all):>9.4f}"
              f"{s29.pl(v_all)/len(v_all):>+10.4f}{s29.pl(v_all):>+12.2f}"
              f"{s29.pl(b_all):>+15.2f}{s29.pl(v_all)-s29.pl(b_all):>+10.2f}")

    # ── 五、新增信号的构成（基线完全没有信号的窗）────────────────────────
    print("\n" + "=" * 104)
    print("五、新增信号（基线一行都没有的窗）按入场 rem 分桶")
    print("=" * 104)
    for name, (pw, _rej) in res.items():
        add = []
        for key, _date, _t, _a, _sd, _o in windows:
            v = pw[key][0] if pw[key] else None
            if v and not base[key]:
                add.append(v)
        if not add:
            print(f"\n  {name}: 新增 n=0")
            continue
        print(f"\n  {name}: 新增 n={len(add)}  WR "
              f"{sum(r['settle_won'] for r in add)/len(add)*100:.2f}%  "
              f"均价 {sum(r['fill'] for r in add)/len(add):.4f}  P&L {s29.pl(add):+.2f}U")
        bb = collections.defaultdict(list)
        for r in add:
            bb[bin_of(r["rem"])].append(r)
        for lo, hi, lab in BINS:
            g = bb.get(lab)
            if not g:
                continue
            print(f"      rem {lab:<10} n={len(g):<4} "
                  f"WR {sum(x['settle_won'] for x in g)/len(g)*100:6.2f}%  "
                  f"均价 {sum(x['fill'] for x in g)/len(g):.4f}  P&L {s29.pl(g):+7.2f}U")


if __name__ == "__main__":
    main()
