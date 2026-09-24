#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v4 扫尾盘「快照口径 A vs 监听口径 B」的**纸面配对判定**（2026-09-23）。

🔴 **2026-09-24 作废（保留供历史对账，勿据此判策略）**：`a.md` 的整合改造把监听段
从「只记录的对账行」变成**策略本体的一部分**（rem ≤ t60_rem 之后每秒判 ② 并真下单，
见 `docs/tail_integrated_2026-09-24.md` §1）——`kind=scan` 行引擎**不再产出**，
「监听口径 B」不再是对照物。本脚本因此失去输入（scan 行恒为空），A/B 配对问题自然消解。
现行口径的权威数字 = `python/v4/23_tail_integrated.py`（三段链 oracle）。

背景：引擎原口径 A = 「rem ≤ 60 的第一个有效 tick」取快照、当场判定 ⑤ 并下单。
用户的假设口径 B = 「从 rem ≤ 60 起持续监听，第一个 ⑤ 达标的 tick 才下单」。
14 天回测里 B 比 A 只多 +7.00U、日级 bootstrap 95% 区间跨 0（[−9.1, +22.5]）⇒ 按
项目自己的 §5.2 判据**不显著**、不改引擎，改为在纸面上**同窗配对**攒样本再判。

为此引擎新增了 `kind=scan` 的**只记录对账行**（internal/tail/engine.go 的第三闩锁）：
快照行未达标时，监听段继续跑，第一个 ⑤ 达标的 tick 落一行 scan（不下单、不进风控
闸、但**照常注册结算**回填 won/pnl——否则「快照没成交」的窗口就永远没有窗口结果）。
结构性保证：快照达标时不再产监听行 ⇒

    scan 行的窗口集合 ≡ B ∖ A（监听段新增的那些窗口）
    B = A 行 ∪ scan 行（同窗不会重复计数）

本脚本读纸面 tail_*.jsonl，输出 A / B / 增量三组，以及**日级配对 bootstrap**的
Δ P&L 区间（配对 = 同一批重采样日期上算 B−A，不是两条独立区间相减）。

口径（与回测/Go judge 1:1）:
  - 每笔 2U：赢 → stake/hot_ask − stake，输 → −stake（hot_ask = 那一 tick 热门侧 ask，
    即成交价；scan 行的股数/价都按同一公式，只是没真下单）
  - 只统计已结算行（won 非空）：未结算（挂单中/等待 gamma）与无锚整窗跳过的行排除
  - 风控闸行（gate_reason 非空）默认排除（同 02/06/07）；--include-gated 恢复旧口径。
    ⚠️ scan 行**从不**进闸（引擎里 HandleScan 不调 gate）——它是对账行，不是订单。
  - **配对要求（红线）**：同一窗口不得既有 ok 的快照行又有 scan 行；A∖B 恒为空
    （监听段只会新增窗口，不会丢掉快照已抓到的窗口）。
  - 日级 bootstrap：每轮有放回抽「日期」（不是抽注），把被抽中日期的全部注合起来算
    P&L；与 13_tail_sweep.py:424 `boot_days` 同形制、同 seed=42、同 2000 次
    ⇒ 与 Dashboard 判决卡（Go `tail.BootstrapCI`）同一个数。
  - **判定用的日期集合排除当天**（UTC 日未走完 = 只有半天样本，同 01 排除 08-31 的
    道理）；`--keep-last-day` 恢复。完整日不足 14 天 / 注数不足 800 时只登记不出判词
    （§5.2 的辅助闸门，主判据是区间）。

用法: python 18_tail_scan_register.py [--data data/v4] [--stake 2] [--boot 2000]
                                      [--seed 42] [--include-gated] [--keep-last-day]
"""
import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent

STAKE = 2.0
BOOT = 2000
SEED = 42          # 与 13_tail_sweep.py:430 / Go judge.BootstrapCI 同种子
PRICE_MIN = 0.80   # 价格腿门槛（仅用于给增量按成因分桶, 不参与判定）
DAY_MIN = 14       # §5.2 主判据的前置: 完整 UTC 日数
N_MIN = 800        # §5.2 主判据的前置: 注数

KIND_SNAP = "snap"
KIND_SCAN = "scan"


def pad(s, width):
    """按显示宽度左对齐（CJK 与 σ/ρ 等算 2 列）。"""
    w = sum(2 if ord(c) > 0x2E7F else 1 for c in s)
    return s + " " * max(0, width - w)


def load_rows(data_dir):
    """读 tail_*.jsonl（按日 glob 排序）→ 归一化行字典列表。

    只收 `event_type == "tail"` 的行（tailwin_/tailstats_ 是别的文件, 不会被
    `tail_*.jsonl` 匹配到——前缀后紧跟的字符不同）。
    """
    rows, bad = [], Counter()
    for f in sorted(Path(data_dir).glob("tail_*.jsonl")):
        with open(f, encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                except json.JSONDecodeError:
                    bad["json"] += 1
                    continue
                if r.get("event_type") != "tail":
                    bad["event_type"] += 1
                    continue
                rows.append(r)
    return rows, bad


def pl_of(rows, stake):
    """样本 P&L（U）：赢 → stake/hot_ask − stake，输 → −stake。"""
    return sum((stake / r["hot_ask"] - stake) if r["won"] else -stake for r in rows)


def by_day(rows):
    d = defaultdict(list)
    for r in rows:
        d[r["date"]].append(r)
    return d


def boot_days(rows, ds, reps, rng):
    """日级 bootstrap 的 P&L 区间（lo, hi）—— 13_tail_sweep.py:424 `boot_days` 同形制:
    每轮有放回抽 len(ds) 个日期、把抽中日的**日内全部注**合成一次 P&L, 2000 次取
    2.5%/97.5% 分位（索引取整, 与 Go 侧一致）。"""
    d = by_day(rows)
    v = sorted(sum(pl_of(d[rng.choice(ds)], STAKE) for _ in ds) for _ in range(reps))
    return v[int(0.025 * reps)], v[int(0.975 * reps)]


def boot_days_delta(a_rows, b_rows, ds, reps, rng, stake):
    """**配对**的日级 bootstrap: 同一批重采样日期上算 (B − A) 的 P&L 差。

    与「两条独立区间相减」不同——配对把日效应消掉, 是这条增量的正确检验。
    """
    da, db = by_day(a_rows), by_day(b_rows)
    v = sorted(sum(pl_of(db[k], stake) - pl_of(da[k], stake) for k in (rng.choice(ds) for _ in ds))
               for _ in range(reps))
    return v[int(0.025 * reps)], v[int(0.975 * reps)]


def stat_line(lab, rows, stake, extra=""):
    """一行统计（同 14_tail_sweep_sigma.line 的形状: n / 胜率 / 均价 / EV每注 / P&L）。"""
    if not rows:
        print(f"  {pad(lab, 26)} n=0")
        return
    n = len(rows)
    k = sum(1 for r in rows if r["won"])
    price = sum(r["hot_ask"] for r in rows) / n
    pl = pl_of(rows, stake)
    d = by_day(rows)
    print(f"  {pad(lab, 26)} n={n:5d}  胜率 {k / n * 100:5.1f}%  均价 {price:.3f}  "
          f"EV/注 {pl / n:+.4f}U  P&L {pl:+7.2f}U  "
          f"日正 {sum(1 for v in d.values() if pl_of(v, stake) > 0):2d}/{len(d)}{extra}")


def median(v):
    v = sorted(v)
    if not v:
        return 0.0
    m = len(v) // 2
    return v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2


def main():
    global STAKE
    ap = argparse.ArgumentParser(description="扫尾盘 快照口径 A vs 监听口径 B 的纸面配对判定")
    ap.add_argument("--data", default=str(BASE.parent.parent / "data" / "v4"),
                    help="纸面输出目录（含 tail_*.jsonl）")
    ap.add_argument("--stake", type=float, default=STAKE, help="每笔投入 USDC（默认 2）")
    ap.add_argument("--boot", type=int, default=BOOT, help="日级 bootstrap 次数（默认 2000）")
    ap.add_argument("--seed", type=int, default=SEED, help="bootstrap 种子（默认 42, 与 Go judge 同）")
    ap.add_argument("--include-gated", action="store_true",
                    help="不过滤 gate_reason 行（恢复旧口径; 默认排除）")
    ap.add_argument("--keep-last-day", action="store_true",
                    help="把最后一个（未走完的）UTC 日也算进判定日期集合（默认排除）")
    args = ap.parse_args()
    STAKE = args.stake

    rows, bad = load_rows(args.data)
    if not rows:
        print(f"⚠️  {args.data} 下没有 tail_*.jsonl 行——引擎还没跑过/输出目录不对。")
        print("    判定的前置: 完整 UTC 日 ≥ %d 且 A 注数 ≥ %d（§5.2）。" % (DAY_MIN, N_MIN))
        return 1
    if bad:
        print(f"⚠️  跳过异常行: {dict(bad)}")

    kinds = Counter(r.get("kind") for r in rows)
    print(f"读入 {len(rows)} 行来自 {args.data}："
          f"frame {kinds.get('frame', 0)} / snap {kinds.get('snap', 0)} / scan {kinds.get('scan', 0)}")
    unknown = [k for k in kinds if k not in ("frame", KIND_SNAP, KIND_SCAN)]
    if unknown:
        print(f"⚠️  未知 kind {unknown} —— 本脚本只认 frame/snap/scan")

    if args.include_gated:
        print("⚠️  --include-gated: 风控闸行也计入（旧口径, 与 Dashboard 判决卡不一致）")
        kept = [r for r in rows if r.get("kind") in (KIND_SNAP, KIND_SCAN) and r.get("ok")]
    else:
        kept = [r for r in rows
                if r.get("kind") in (KIND_SNAP, KIND_SCAN) and r.get("ok")
                and not r.get("gate_reason")]

    # ── 结算状态分解（未结算行不进统计: 挂单中/等待 gamma 结算）──
    settled = [r for r in kept if r.get("won") is not None]
    pend = [r for r in kept if r.get("won") is None]
    if pend:
        pk = Counter(r["kind"] for r in pend)
        print(f"   未结算 {len(pend)} 行（snap {pk.get(KIND_SNAP, 0)} / scan {pk.get(KIND_SCAN, 0)}）"
              f"—— 不进本次统计, 等 gamma 回填")

    A = [r for r in settled if r["kind"] == KIND_SNAP]
    S = [r for r in settled if r["kind"] == KIND_SCAN]
    B = A + S                       # 结构性: 同窗互斥 ⇒ 直接并集
    if not A and not S:
        print("⚠️  没有任何已结算的 snap/scan 行——先让纸面跑起来（结算在闭市后数分钟）。")
        return 1

    # ── 红线自检: 同窗不得既有达标快照又有监听行（引擎的单向闩锁保证）──
    ok_snap_win = {r["condition_id"] for r in A}
    scan_win = {r["condition_id"] for r in S}
    both = ok_snap_win & scan_win
    if both:
        print(f"🔴 红线违规: {len(both)} 个窗口同时有达标 snap 行与 scan 行"
              f"（例: {sorted(both)[:3]}）—— 引擎闩锁或记录有 bug, 下面的配对不成立")

    # ── 日期集合（判定用）: 排除未走完的当天 ──
    days_all = sorted({r["date"] for r in settled})
    days = list(days_all)
    last_dropped = None
    if not args.keep_last_day and len(days) > 1:
        last_dropped = days[-1]
        days = days[:-1]

    print(f"\n§1 三组统计（每笔 {STAKE:g}U；日期集合 {len(days)} 天"
          f"{f', 已排除未走完的 {last_dropped}' if last_dropped else ''}）")
    stat_line("A 快照 rem≤60 首 tick", A, STAKE)
    stat_line("B 监听 首个达标 tick", B, STAKE)
    stat_line("增量 B∖A", S, STAKE, extra=f"  亏损日 {sum(1 for v in by_day(S).values() if pl_of(v, STAKE) < 0)}"
              if S else "")
    # A∖B 结构性为空: scan 只在快照未达标时产出, 监听段不会让已抓到的窗口消失
    print(f"  {pad('A∖B 丢掉信号', 26)} n=0（结构性: 监听段只增不减）")

    # ── 增量按成因分桶: 快照那一 tick 为什么没达标（读该窗 snap 行的 reject_reason）──
    if S:
        rej = {}
        for r in rows:
            if r.get("kind") == KIND_SNAP and r.get("condition_id") in scan_win:
                rej[r["condition_id"]] = (r.get("reject_reason") or "ok(不该出现)")
        groups = defaultdict(list)
        for r in S:
            groups[rej.get(r["condition_id"], "无 snap 行")].append(r)
        print(f"\n§2 增量按「快照那一 tick 为何不达标」分桶（门槛 hot_ask ≥ {PRICE_MIN:g}）")
        for name, g in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            stat_line(f"└ {name}", g, STAKE,
                      extra=f"  中位 rem {median([r['rem'] for r in g]):.0f}s  "
                            f"中位 hot_ask {median([r['hot_ask'] for r in g]):.3f}")
        print(f"  （注: price_low = 快照时热门侧 ask 还没到 {PRICE_MIN:g}；"
              f"leg_out = 位移腿 dev/σ 没达标；missing_spot/missing_twap = 输入缺失；"
              f"ok(不该出现) 说明该窗快照其实达标——那是红线违规）")

    # ── 日级 bootstrap（判定主判据）──
    print(f"\n§3 日级 bootstrap 95% 区间（重采样日期 {args.boot} 次, seed {args.seed}"
          f"；与 13_tail_sweep.py:424 / Dashboard 判决卡同形制）")
    rng = random.Random(args.seed)
    a_ci = boot_days(A, days, args.boot, rng)
    rng = random.Random(args.seed)
    b_ci = boot_days(B, days, args.boot, rng)
    rng = random.Random(args.seed)
    d_ci = boot_days_delta(A, B, days, args.boot, rng, STAKE)
    print(f"  {pad('A 快照口径', 26)} P&L {pl_of(A, STAKE):+7.2f}U   区间 [{a_ci[0]:+7.2f}, {a_ci[1]:+7.2f}]U")
    print(f"  {pad('B 监听口径', 26)} P&L {pl_of(B, STAKE):+7.2f}U   区间 [{b_ci[0]:+7.2f}, {b_ci[1]:+7.2f}]U")
    print(f"  {pad('增量 B−A（配对）', 26)} P&L {pl_of(S, STAKE):+7.2f}U   区间 [{d_ci[0]:+7.2f}, {d_ci[1]:+7.2f}]U")

    print(f"\n§4 逐日明细（UTC；Δ = B − A）")
    print(f"  {pad('日期', 12)} {pad('A 注数', 7)} {pad('A P&L', 10)} {pad('B 注数', 7)} "
          f"{pad('B P&L', 10)} {pad('Δ注数', 6)} Δ P&L")
    for d in days_all:
        da, db = [r for r in A if r["date"] == d], [r for r in B if r["date"] == d]
        pa, pb = pl_of(da, STAKE), pl_of(db, STAKE)
        mark = "  ← 未计入判定" if d == last_dropped else ("  ← 计入判定" if d in days else "")
        print(f"  {pad(d, 12)} {len(da):<7d} {pa:+9.2f}U {len(db):<7d} {pb:+9.2f}U "
              f"{len(db) - len(da):<6d} {pb - pa:+8.2f}U{mark}")

    # ── 判词 ──
    n_days = len(days)
    n_a = len(A)
    ready = n_days >= DAY_MIN and n_a >= N_MIN
    print(f"\n§5 判词")
    print(f"  前置闸门: 完整 UTC 日 {n_days}/{DAY_MIN}，A 注数 {n_a}/{N_MIN}"
          f" → {'已过' if ready else '未过（只登记，不判决）'}")
    if d_ci[0] > 0:
        v = "增量下界 > 0 → 监听口径 B 优于快照口径 A"
    elif d_ci[1] < 0:
        v = "增量上界 < 0 → 快照口径 A 优于监听口径 B"
    else:
        v = "区间跨 0 → 不显著（维持 A；样本继续攒，或按 §5.2 延到 28 日）"
    print(f"  配对 Δ 区间 [{d_ci[0]:+.2f}, {d_ci[1]:+.2f}]U → {v}")
    if not ready:
        print(f"  ⚠️  前置闸门未过，上面的判词**只作登记**，不构成口径切换依据。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
