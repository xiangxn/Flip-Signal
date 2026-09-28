#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫尾盘三段链：**第二段判定点 T=60 的敏感性**（2026-09-28）

问题（用户提出）：把三段链的第二段从 `T=60` 改到 `T=90 / 80 / 70`，14 天回测是否有改善？

口径：**逐字复用 `23_tail_integrated.py` 的 `chain()`**——只把模块级常量 `T60` 换成
候选值后再调它（`win_ticks` 仍按 `T150` 过滤, `r5/r2` 的阈值一字不动）。这样保证与
oracle / Go 引擎同源, 不会因为重写一份判定链而引入口径漂移。

⚠️ 改 T60 会**同时**动三件事（读结果时必须分开看, 见输出第二节）:
  1. 第二段判定点提前（rem ≤ T60 的第一个可判定 tick）;
  2. 监听段起点随之提前（被拒后从该 tick 起每秒判 ②）;
  3. **段的归属改变** ⇒ `PriceLeg` 的算子跟着变——`T=150` 段是严格 `> 0.80`（决策 #26），
     `T=60` 段与监听段是非严格 `≥ 0.80`。T60=90 时, 首个可判定 tick 落在 rem ∈ (60, 90]
     的窗会从「t150 段（严格 >）」变成「t60 段（非严格 ≥）」, 这是**规则差异**不是时间差异。

用法: python/venv/bin/python python/v4/29_tail_t60_rem.py
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

CANDIDATES = (60, 70, 80, 90)      # 60 = 现行基线
BASELINE = 60
STAGES = ("t150", "t60", "listen")
SEED = 42
REPS = 2000


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


s13 = load("s13", BASE / "13_tail_sweep.py")
s14 = load("s14", BASE / "14_tail_sweep_sigma.py")
s23 = load("s23", BASE / "23_tail_integrated.py")


# ── 统计 ──────────────────────────────────────────────────────────────────

def pl(rows, stake=2.0):
    s = 0.0
    for r in rows:
        sh = stake / r["fill"]
        s += (sh - stake) if r["settle_won"] else -stake
    return s


def stat(rows):
    n = len(rows)
    if not n:
        return None
    d = collections.defaultdict(list)
    for r in rows:
        d[r["date"]].append(r)
    return {
        "n": n,
        "wr": sum(r["settle_won"] for r in rows) / n,
        "fill": sum(r["fill"] for r in rows) / n,
        "pl": pl(rows),
        "ev": pl(rows) / n,
        "days": len(d),
        "neg_days": sum(1 for v in d.values() if pl(v) < 0),
    }


def line(lab, rows, width=28):
    s = stat(rows)
    if s is None:
        print("  " + lab.ljust(width) + "n=0")
        return
    print("  " + lab.ljust(width) +
          f"n={s['n']:<5} WR {s['wr']*100:6.2f}%  均价 {s['fill']:.4f}  "
          f"EV/注 {s['ev']:+.4f}  P&L {s['pl']:+7.2f}U  亏损日 {s['neg_days']}/{s['days']}")


def run_chain(ticks, anchor, sd, date, outcome, t60):
    """把 oracle 的 chain 用候选 T60 跑一遍（改的是模块级常量, 逻辑零改动）。"""
    old = s23.T60
    s23.T60 = t60
    try:
        return s23.chain(ticks, anchor, sd, date, outcome)
    finally:
        s23.T60 = old


def chain_add(ticks, anchor, sd, date, outcome, t_extra):
    """**追加**一个 ⑤ 判定点（不撤掉 T=60 的那个）：150 → t_extra → 60 → listen。

    与 `chain()` 的唯一差别是中间多一段：被 t150 段拒掉后, 先看首个 `rem ≤ t_extra`
    的 tick 判 ⑤（非严格 ≥, 与 t60 段同算子）, 拒绝才继续走原来的 t60 段。
    该段的 tick 落在 `rem ∈ (60, t_extra]` —— 这段区间在原三段链里是**完全黑的**
    （t150 段只看首个 tick, 之后直接跳到 rem ≤ 60）。

    ⚠️ 结构性不变式（第五节断言）: 附加段只可能在**原信号之前**抢答, 不可能让任何窗
    丢掉信号（原 t150/t60/listen 三段的代码路径逐行未变）。
    """
    if not ticks:
        return []
    rows = []
    head = ticks[0]
    skips = 0
    if head["rem"] > s23.T60:
        r = s23.row(head, anchor, sd, date, outcome, "t150")
        if s23.r5(r, strict_price=True):
            r["ok"] = True
            return [r], 0
        rows.append(r)
        if head["rem"] > t_extra:
            nxt = next((x for x in ticks if x["rem"] <= t_extra), None)
            if nxt is not None:
                rx = s23.row(nxt, anchor, sd, date, outcome, "extra")
                if s23.r5(rx):
                    rx["ok"] = True
                    return rows + [rx], 0
                rows.append(rx)
        else:
            skips = 1          # 首个 tick 已落在附加段内（数据里应为 0, 见输出）
        rest = [x for x in ticks if x["rem"] <= s23.T60]
    else:
        rest = ticks           # 迟到接入（与 oracle 同）
    if rest:
        t2 = rest[0]
        r2r = s23.row(t2, anchor, sd, date, outcome, "t60")
        if s23.r5(r2r):
            r2r["ok"] = True
            return rows + [r2r], skips
        rows.append(r2r)
        for x in rest[1:]:
            rl = s23.row(x, anchor, sd, date, outcome, "listen")
            if s23.r2(rl):
                rl["ok"] = True
                return rows + [rl], skips
    return rows, skips


def main():
    ev = load_events("data/btc")
    hr = s13.hist_ranges(ev)

    # 一次遍历：每窗只算一次 ticks/信号行, 各候选 T60 复用（算得便宜, 但省 IO/σ）
    windows = []          # (key, date, ticks, anchor, sd, outcome)
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

    # ── 逐候选跑链 ────────────────────────────────────────────────────────
    sigs = {}         # t60 → {stage: [rows]}
    allsig = {}       # t60 → [rows]
    perwin = {}       # t60 → {start_time: [rows]}
    for t in CANDIDATES:
        per_stage = {s: [] for s in STAGES}
        pw = {}
        for key, date, ticks, anchor, sd, outcome in windows:
            rows, _ = run_chain(ticks, anchor, sd, date, outcome, t)
            ok = [r for r in rows if r.get("ok")]
            pw[key] = ok
            for r in ok:
                per_stage[r["stage"]].append(r)
        sigs[t] = per_stage
        allsig[t] = [r for s in STAGES for r in per_stage[s]]
        perwin[t] = pw

    # ── 一、总览 ─────────────────────────────────────────────────────────
    print("\n" + "=" * 104)
    print("一、第二段判定点 T=60 → {70,80,90}：三段链合计（14 天, 2U/注, σ 未就绪窗跳过）")
    print("=" * 104)
    for t in CANDIDATES:
        tag = "  ← 现行基线" if t == BASELINE else ""
        line(f"T60={t}", allsig[t], width=12)
        print(" " * 14 + tag)
    print(f"\n  参考窗数: 参与判定 {len(windows)} 窗; σ 未就绪跳过 {skipped_no_sigma} 窗")

    # ── 二、分段明细（看信号从哪里来）────────────────────────────────────
    print("\n" + "=" * 104)
    print("二、分段明细（段归属会随 T60 变——注意 t150 段是严格 >0.80, 另两段是 ≥0.80）")
    print("=" * 104)
    for t in CANDIDATES:
        print(f"\n  T60 = {t}")
        for s in STAGES:
            line(f"    {s} 段信号", sigs[t][s])

    # ── 三、逐窗配对 Δ vs 基线 + 日级 bootstrap ─────────────────────────
    print("\n" + "=" * 104)
    print(f"三、配对 Δ（逐窗 variant − baseline(T60={BASELINE})）与日级 bootstrap 95% 区间")
    print("=" * 104)
    base_pw = perwin[BASELINE]
    base_row = {k: (v[0] if v else None) for k, v in base_pw.items()}
    rng = random.Random(SEED)
    dates = sorted(set(d for _, d, _, _, _, _ in windows))
    print(f"  {'T60':<6}{'ΔP&L(U)':>12}{'ΔEV/注':>12}{'改善窗':>8}{'恶化窗':>8}"
          f"{'新增信号':>10}{'消失信号':>10}{'95% CI (ΔP&L)':>26}")
    for t in CANDIDATES:
        if t == BASELINE:
            continue
        delta = collections.defaultdict(float)
        better = worse = new = gone = 0
        for key, date, _ticks, _a, _sd, _o in windows:
            b = base_row[key]
            v = perwin[t][key][0] if perwin[t][key] else None
            db = pl([b]) if b else 0.0
            dv = pl([v]) if v else 0.0
            delta[date] += dv - db
            if not b and not v:
                continue
            d = dv - db
            if d > 1e-9:
                better += 1
            elif d < -1e-9:
                worse += 1
            if not b and v:
                new += 1
            elif b and not v:
                gone += 1
        ds = sorted(delta)
        obs = sum(delta.values())
        n_win = sum(1 for k in base_row if base_row[k] or perwin[t][k])
        boot = []
        for _ in range(REPS):
            boot.append(sum(delta[rng.choice(ds)] for _ in ds))
        boot.sort()
        q = lambda p: boot[int(p * len(boot))]                            # noqa: E731
        print(f"  {t:<6}{obs:>+12.2f}{obs/n_win:>+12.4f}{better:>8}{worse:>8}"
              f"{new:>10}{gone:>10}   [{q(0.025):+.2f}, {q(0.975):+.2f}]")

    # ── 四、迁移矩阵：信号段的变化 ───────────────────────────────────────
    print("\n" + "=" * 104)
    print("四、逐窗「信号段」迁移矩阵（行 = 基线 T60=60, 列 = 变体; `-` = 无信号）")
    print("=" * 104)
    for t in CANDIDATES:
        if t == BASELINE:
            continue
        mat = collections.Counter()
        for key, date, _ticks, _a, _sd, _o in windows:
            b = base_row[key]
            v = perwin[t][key][0] if perwin[t][key] else None
            mat[(b["stage"] if b else "-", v["stage"] if v else "-")] += 1
        print(f"\n  T60 = {t}")
        cols = STAGES + ("-",)
        print("    " + "基线\\变体".ljust(12) + "".join(c.rjust(9) for c in cols))
        for bs in STAGES + ("-",):
            rowc = [mat[(bs, cs)] for cs in cols]
            if not any(rowc):
                continue
            print("    " + bs.ljust(12) + "".join(str(c).rjust(9) for c in rowc))

    # ── 五、新增/消失信号的构成（赢输与成交价）─────────────────────────
    print("\n" + "=" * 104)
    print("五、新增 / 消失信号的构成（新信号按 `成交价` 分桶; 消失信号在基线里的赢输）")
    print("=" * 104)
    for t in CANDIDATES:
        if t == BASELINE:
            continue
        add, drop = [], []
        for key, date, _ticks, _a, _sd, _o in windows:
            b = base_row[key]
            v = perwin[t][key][0] if perwin[t][key] else None
            if not b and v:
                add.append(v)
            elif b and not v:
                drop.append(b)
        print(f"\n  T60 = {t}")
        if add:
            print(f"    新增 n={len(add)}  WR {sum(r['settle_won'] for r in add)/len(add)*100:.2f}%"
                  f"  均价 {sum(r['fill'] for r in add)/len(add):.4f}  P&L {pl(add):+.2f}U")
            buckets = collections.defaultdict(list)
            for r in add:
                buckets[round(r["fill"], 2)].append(r)
            for px in sorted(buckets):
                g = buckets[px]
                print(f"      fill≈{px:.2f}  n={len(g):<4} "
                      f"WR {sum(x['settle_won'] for x in g)/len(g)*100:6.2f}%  "
                      f"P&L {pl(g):+7.2f}U")
        else:
            print("    新增 n=0")
        if drop:
            print(f"    消失 n={len(drop)}  WR {sum(r['settle_won'] for r in drop)/len(drop)*100:.2f}%"
                  f"  均价 {sum(r['fill'] for r in drop)/len(drop):.4f}  P&L {pl(drop):+.2f}U")
        else:
            print("    消失 n=0")

    # ── 六、补充变体：**追加**检查点（不撤掉 T=60 那个）──────────────────
    print("\n" + "=" * 104)
    print("六、补充变体：在 rem≤90/80/70 **追加**一个 ⑤ 判定点（T=60 段保留）——"
          "原链 rem∈(60,T] 是黑的")
    print("=" * 104)
    print("  口径：150(严格>) → T(非严格≥) → 60(非严格≥) → listen; 附加段拒绝才继续走原链")
    lost_total = 0
    for t in (70, 80, 90):
        per_stage = collections.defaultdict(list)
        pw, skips = {}, 0
        for key, date, ticks, anchor, sd, outcome in windows:
            rows, sk = chain_add(ticks, anchor, sd, date, outcome, t)
            skips += sk
            ok = [r for r in rows if r.get("ok")]
            pw[key] = ok
            for r in ok:
                per_stage[r["stage"]].append(r)
        agg = [r for s in ("t150", "extra", "t60", "listen") for r in per_stage[s]]
        print(f"\n  T_extra = {t}   （附加段首 tick 已落在附加段内的窗: {skips}）")
        for s in ("t150", "extra", "t60", "listen"):
            if per_stage[s]:
                line(f"    {s} 段信号", per_stage[s])
        line("    合计", agg, width=28)
        delta = collections.defaultdict(float)
        lost = gained = 0
        for key, date, _ticks, _a, _sd, _o in windows:
            b = base_row[key]
            v = pw[key][0] if pw[key] else None
            db = pl([b]) if b else 0.0
            dv = pl([v]) if v else 0.0
            delta[date] += dv - db
            if b and not v:
                lost += 1
            elif v and not b:
                gained += 1
        lost_total += lost
        ds = sorted(delta)
        obs = sum(delta.values())
        boot = sorted(sum(delta[rng.choice(ds)] for _ in ds) for _ in range(REPS))
        q = lambda p: boot[int(p * len(boot))]                            # noqa: E731
        print(f"    ΔP&L {obs:+.2f}U   ΔEV/注 {obs/len(base_row):+.4f}   新增信号 {gained}   "
              f"丢失信号 {lost}（不变式应为 0）   95% CI [{q(0.025):+.2f}, {q(0.975):+.2f}]")
    print(f"\n  不变式核对: 三个附加变体的「丢失信号」合计 = {lost_total} "
          f"（应为 0 —— 附加段只抢答, 不改变原链任何路径）")

    # ── 七、分半稳健性（全部变体一起看）──────────────────────────────────
    print("\n" + "=" * 104)
    print("七、分半稳健性：ΔP&L 按日期前半 / 后半拆（14 天对半, 每半 7 天）")
    print("=" * 104)
    half = len(dates) // 2
    h1, h2 = set(dates[:half]), set(dates[half:])
    print(f"  前半 {min(h1)} ~ {max(h1)}   后半 {min(h2)} ~ {max(h2)}")
    print(f"  {'变体':<20}{'ΔP&L':>10}{'前半':>10}{'后半':>10}{'同号':>8}")
    for t in (70, 80, 90):
        for kind in ("替换", "追加"):
            delta = collections.defaultdict(float)
            for key, date, ticks, anchor, sd, outcome in windows:
                b = base_row[key]
                if kind == "替换":
                    ok = perwin[t][key]
                else:
                    ok = [r for r in chain_add(ticks, anchor, sd, date, outcome, t)[0]
                          if r.get("ok")]
                dv = pl(ok[:1]) if ok else 0.0
                db = pl([b]) if b else 0.0
                delta[date] += dv - db
            a = sum(delta[d] for d in h1)
            c = sum(delta[d] for d in h2)
            same = "是" if (a > 0) == (c > 0) else "**否**"
            print(f"  {kind + '@' + str(t):<20}{a+c:>+10.2f}{a:>+10.2f}{c:>+10.2f}{same:>8}")
    print("\n  注: 6 个变体都跑在同 14 天（样本内）, 且都不是预注册假设 —— 单个变体的正 Δ")
    print("      在没有独立样本前不足以支撑改参数（本项目既有立场: 小样本 A/B 排序不可作证据）。")


if __name__ == "__main__":
    main()
