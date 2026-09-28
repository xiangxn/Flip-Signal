#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫尾盘：**追加检查点**（不动 T=60 段）的位置曲线 + 用户的 T120/T90 提案（2026-09-28）

背景：`29_tail_t60_rem.py` 的第六节发现「在 T=70 追加一个 ⑤ 检查点」名义 +3.87U
（CI 含 0）——注意那次追加点用的是 **T60 的条件（价格腿非严格 `≥ 0.80`）**。
本轮问题（用户提出）：**从 T150 起每隔 30s 用 T150 的条件再查一次**，即追加 T=120 与 T=90
两个检查点（价格腿**严格 `> 0.80`**，其余腿与 ⑤ 同）。

链形状：`150(严格) → [追加点…(各自算子)] → 60(非严格) → 监听 ②`
每个检查点消费「上一个被消费 tick **之后**」的第一个 `rem ≤ 阈值` 的 tick（互不重叠）;
迟到接入（首个可判定 tick 已在 rem ≤ 60）仍整段跳过前面的检查点（与 oracle 同）。

本脚本同时给出**单点追加的位置曲线**（T=70…135, 两种算子），用来判断提案落在哪个区域 ——
只有一个点好看不能说明问题, 曲线形态（平台 vs 噪声尖峰）才是判据。

用法: python/venv/bin/python python/v4/31_tail_checkpoints.py
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
SINGLES = (70, 75, 80, 90, 105, 120, 135)
COMBOS = (((120, ">"), (90, ">")), ((120, ">"),), ((90, ">"),),
          ((120, ">="), (90, ">=")))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


s13 = load("s13", BASE / "13_tail_sweep.py")
s23 = load("s23", BASE / "23_tail_integrated.py")
s29 = load("s29", BASE / "29_tail_t60_rem.py")     # 复用 pl / stat / line


def chain_ck(ticks, anchor, sd, date, outcome, cks, t60=60):
    """150 段 + 若干附加检查点 + 60 段 + 监听段。

    cks: ((阈值, 算子), …) —— 算子是 ">" 或 ">=", 施加在价格腿上（其余腿 = ⑤ 本体）。
    返回 (rows, 被拒检查点数)。
    """
    if not ticks:
        return [], 0
    rows, rej = [], 0
    head = ticks[0]
    if head["rem"] <= t60:
        cur, rest = -1, ticks                       # 迟到接入（与 oracle 同）
    else:
        cur = -1
        for i, (thr, op) in enumerate(cks):
            if i > 0 and ticks[cur]["rem"] <= thr:
                continue          # 该检查点时刻已过去（迟到接入）⇒ 跳过, 不追溯判定
            j = cur + 1
            while j < len(ticks) and ticks[j]["rem"] > thr:
                j += 1
            if j >= len(ticks):
                break
            cur = j
            x = ticks[cur]
            r = s23.row(x, anchor, sd, date, outcome, f"ck{thr}{op}")
            if s23.r5(r, strict_price=(op == ">")):
                r["ok"] = True
                return rows + [r], rej
            rows.append(r)
            rej += 1
        rest = [x for x in ticks[cur + 1:] if x["rem"] <= t60]
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

    s23.T60 = 60
    base = {}
    for key, date, ticks, anchor, sd, outcome in windows:
        base[key] = [r for r in s23.chain(ticks, anchor, sd, date, outcome)[0] if r.get("ok")]

    dates = sorted(set(d for _, d, _, _, _, _ in windows))
    half = len(dates) // 2
    h1, h2 = set(dates[:half]), set(dates[half:])
    rng = random.Random(SEED)

    def run(cks):
        pw = {}
        for key, date, ticks, anchor, sd, outcome in windows:
            pw[key] = [r for r in chain_ck(ticks, anchor, sd, date, outcome, cks)[0]
                       if r.get("ok")]
        return pw

    def report(label, pw):
        delta = collections.defaultdict(float)
        better = worse = new = lost = mov_n = 0
        add_p, mov_d = [], 0.0         # 新增（基线无信号）的 P&L / 改道（入场点变了）的**差额**
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
                if v["rem"] != b["rem"]:
                    mov_n += 1
                    mov_d += dv - db
            elif v and not b:
                new += 1
                add_p.append(v)
            elif b and not v:
                lost += 1
        ds = sorted(delta)
        boot = sorted(sum(delta[rng.choice(ds)] for _ in ds) for _ in range(REPS))
        q = lambda p: boot[int(p * len(boot))]                            # noqa: E731
        a, c = sum(delta[d] for d in h1), sum(delta[d] for d in h2)
        return dict(d=sum(delta.values()), lo=q(0.025), hi=q(0.975),
                    h1=a, h2=c, new=new, lost=lost, better=better, worse=worse, pw=pw,
                    add_p=add_p, mov_n=mov_n, mov_d=mov_d)

    def rowfmt(label, r, n_all):
        sign = " " if r["lo"] <= 0 <= r["hi"] else "*"
        print(f"  {label:<14}{r['d']:>+9.2f}{r['h1']:>+9.2f}{r['h2']:>+9.2f}"
              f"{r['new']:>6}{s29.pl(r['add_p']):>+10.2f}"
              f"{r['mov_n']:>6}{r['mov_d']:>+10.2f}"
              f"   [{r['lo']:+.2f}, {r['hi']:+.2f}]{sign}")

    hdr = (f"  {'变体':<14}{'ΔP&L':>9}{'前半':>9}{'后半':>9}"
           f"{'新增n':>6}{'新增P&L':>10}{'改道n':>6}{'Δ改道':>10}{'95% CI':>24}")

    # ── 一、单点追加的位置曲线 ───────────────────────────────────────────
    print("\n" + "=" * 104)
    print("一、单点追加的位置曲线（在 150 与 60 之间**只加一个**检查点; 基线 = 现行三段链）")
    print("=" * 104)
    print(f"  基线 n=2133 WR 96.06% +35.09U;  前半 {min(h1)}~{max(h1)} / 后半 {min(h2)}~{max(h2)}")
    print("  `*` = 95% 区间不含 0\n")
    print(hdr)
    curve = {}
    for op in (">=", ">"):
        print(f"\n  算子 价格腿 {op} 0.80（{'T60 的条件' if op == '>=' else 'T150 的条件'}）")
        for R in SINGLES:
            r = report(f"追加@{R}", run(((150, ">"), (R, op))))
            curve[(R, op)] = r
            rowfmt(f"  +T={R}", r, 2133)
    print("\n  读法: 曲线应看**形态**——若只在某一点冒尖、左右都差, 是噪声尖峰;")
    print("        若一整段同号且量级相近, 才是可讨论的区域。")

    # ── 二、用户提案：T120 + T90（用 T150 的条件）────────────────────────
    print("\n" + "=" * 104)
    print("二、用户提案：从 T150 起每隔 30s 用 T150 的条件再查（追加 T=120 与 T=90）")
    print("=" * 104)
    print(hdr)
    combo = None
    for cks in COMBOS:
        lab = "+".join(f"T{t}{('>' if op == '>' else '>=')}" for t, op in cks)
        r = report(lab, run(((150, ">"),) + cks))
        if cks == COMBOS[0]:
            combo = r
        rowfmt("  " + lab, r, 2133)
    print("\n  注: `T120>T90>` = 用户提案本体（两点都是严格 >）; 下面三行是**分解**——")
    print("      单独加 T=120 / 单独加 T=90 / 两点都用非严格（对照算子这一维）。")

    # ── 三、提案命中的窗：入场 rem 与 EV ─────────────────────────────────
    print("\n" + "=" * 104)
    print("三、用户提案：新增 / 改道信号的去向（基线无信号的窗 = 新增; 其余 = 改道）")
    print("=" * 104)
    pw = combo["pw"]
    add, moved = [], []
    for key, date, _t, _a, _sd, _o in windows:
        v = pw[key][0] if pw[key] else None
        b = base[key][0] if base[key] else None
        if v and not b:
            add.append(v)
        elif v and b and v is not b and v["rem"] != b["rem"]:
            moved.append((b, v))
    for lab, g, dlt in (("新增（基线一行都没有）", add, None),
                        ("改道（基线有信号但入场点变了）", [v for _b, v in moved],
                         sum(s29.pl([v]) - s29.pl([b]) for b, v in moved))):
        if not g:
            print(f"\n  {lab}: n=0")
            continue
        p = s29.pl(g)
        n = len(g)
        extra = f"  Δ vs 基线 {dlt:+.2f}U" if dlt is not None else ""
        print(f"\n  {lab}: n={n}  WR {sum(r['settle_won'] for r in g)/n*100:.2f}%  "
              f"均价 {sum(r['fill'] for r in g)/n:.4f}  盈亏平衡 WR = 均价 "
              f"{sum(r['fill'] for r in g)/n*100:.2f}%  EV/注 {p/n:+.4f}U  P&L {p:+.2f}U{extra}")
        if g is add:
            days = collections.defaultdict(list)
            for key, date, _t, _a, _sd, _o in windows:
                v = pw[key][0] if pw[key] else None
                if v and not base[key]:
                    days[date].append(v)
            ds = sorted(days)
            boot = []
            for _ in range(REPS):
                pool = [r for k in (rng.choice(ds) for _ in ds) for r in days[k]]
                boot.append(s29.pl(pool))
            boot.sort()
            q = lambda p2: boot[int(p2 * len(boot))]                      # noqa: E731
            print(f"      日级 bootstrap 95%: EV/注 [{q(0.025)/n:+.4f}, {q(0.975)/n:+.4f}]U")
        bb = collections.defaultdict(list)
        for r in g:
            bb[(r["rem"] // 15) * 15].append(r)
        for k in sorted(bb, reverse=True):
            s = bb[k]
            print(f"      rem {k:>3}~{k+14:<3} n={len(s):<4} "
                  f"WR {sum(x['settle_won'] for x in s)/len(s)*100:6.2f}%  "
                  f"均价 {sum(x['fill'] for x in s)/len(s):.4f}  P&L {s29.pl(s):+7.2f}U")

    # ── 五、逐窗输赢翻转账（新亏损从哪来）────────────────────────────────
    print("\n" + "=" * 104)
    print("五、逐窗输赢翻转账：追加检查点到底多下输了几笔")
    print("=" * 104)
    bw = sum(1 for k in base if base[k] and base[k][0]["settle_won"])
    bl = sum(1 for k in base if base[k] and not base[k][0]["settle_won"])
    print(f"  基线: 赢 {bw} / 输 {bl}（共 {bw+bl} 注）\n")
    print(f"  {'变体':<14}{'赢→输':>8}{'输→赢':>8}{'新增赢':>8}{'新增输':>8}"
          f"{'换边':>7}{'总赢':>8}{'总输':>8}{'净增输':>9}")

    def flips(pw):
        w2l = l2w = nw = nl = side = tw = tl = 0
        detail = []
        for key, date, _t, _a, _sd, _o in windows:
            b = base[key][0] if base[key] else None
            v = pw[key][0] if pw[key] else None
            if v:
                tw += v["settle_won"]
                tl += not v["settle_won"]
            if b and v:
                if b["settle_won"] and not v["settle_won"]:
                    w2l += 1
                    detail.append(("赢→输", date, b, v))
                elif v["settle_won"] and not b["settle_won"]:
                    l2w += 1
                if b["side"] != v["side"]:
                    side += 1
            elif v and not b:
                if v["settle_won"]:
                    nw += 1
                else:
                    nl += 1
                    detail.append(("新增输", date, None, v))
        return w2l, l2w, nw, nl, side, tw, tl, detail

    for op in (">=", ">"):
        print(f"\n  算子 {op} 0.80")
        for R in SINGLES:
            w2l, l2w, nw, nl, side, tw, tl, _ = flips(curve[(R, op)]["pw"])
            print(f"    {'+T=' + str(R):<12}{w2l:>8}{l2w:>8}{nw:>8}{nl:>8}"
                  f"{side:>7}{tw:>8}{tl:>8}{tl-bl:>+9}")

    print("\n  用户提案（多点）同款对照:")
    for cks in COMBOS:
        lab = "+".join(f"T{t}{op}" for t, op in cks)
        pw_c = combo["pw"] if cks == COMBOS[0] else run(((150, ">"),) + cks)
        w2l, l2w, nw, nl, side, tw, tl, _ = flips(pw_c)
        print(f"    {lab:<12}{w2l:>8}{l2w:>8}{nw:>8}{nl:>8}"
              f"{side:>7}{tw:>8}{tl:>8}{tl-bl:>+9}")

    # T=70 的逐笔明细
    detail = flips(curve[(70, ">=")]["pw"])[7]
    print(f"\n  T=70 明细（价格腿 ≥；与 > 逐位相同）——新增/翻转出来的**输**共 {len(detail)} 笔:")
    print(f"    {'类型':<8}{'日期':<12}{'入场rem':>8}{'成交价':>8}{'side':>6}"
          f"{'dev(美元)':>11}{'基线入场rem':>12}{'基线成交价':>12}")
    for kind, date, b, v in sorted(detail, key=lambda x: (x[0], x[1])):
        print(f"    {kind:<8}{date:<12}{v['rem']:>8}{v['fill']:>8.2f}{v['side']:>6}"
              f"{v['dev']:>+11.1f}"
              f"{(b['rem'] if b else 0):>12}{(b['fill'] if b else 0):>12.2f}")

    print("\n" + "=" * 104)
    print("四、不变式核对：所有变体的「丢失信号」")
    print("=" * 104)
    lost = [r["lost"] for r in curve.values()] + [combo["lost"]]
    print(f"  全部变体合计 丢失信号 = {sum(lost)}（应为 0 —— 追加点只抢答, 不改原链任何路径）")
    print(f"  参考窗数: 参与判定 {len(windows)} 窗; σ 未就绪跳过 {skipped_no_sigma} 窗")


if __name__ == "__main__":
    main()
