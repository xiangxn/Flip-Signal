#!/usr/bin/env python3
"""
频率导向参数扫描 — 目标：2-4 信号/小时（当前 Formula B ≈ 0.9/小时）。

在 data_0 全量（8/5-8/12, 1719 事件, ~143 小时）上扫描参数组合，
找出信号频率达到 2-4/小时且 EV 保持为正、双 Split 一致性的配置。

方法（沿用 optimize_winrate.py 的防过拟合设计）:
  - 穿越候选一次提取、多次复用（trigger ∈ {0.70, 0.65, 0.62, 0.60} 分别提取）
  - 评分/选择完全复刻 Go engine 语义（窗口→B1→F0→gate(ask)→B2/B3→首过者胜）
  - 排序只看 train EV（Split A: 08-06~08-10 / 08-11；Split B: 08-06~08-09 / 08-10~11）
  - 只用圆整参数值，拒绝样本内精调
  - 输出逐日分解 + Wilson CI

注意: data_0 无 order_book_latency 字段（采集于该字段加入之前），
实盘 max_latency_ms=300 的否决无法在此建模 —— 实盘信号率会略低于
本脚本报告值（26 事件实测：1 个回测信号被延迟否决）。

用法:
  python sweep_frequency.py --stage 1   # 单维扫描
  python sweep_frequency.py --stage 2   # 组合网格
  python sweep_frequency.py --stage 3   # 决赛圈: 逐日分解
"""

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import product
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest_flip_config import FlipBacktestConfig
from backtest_flip_utils import compute_hist_avg_range
from optimize_winrate import load_events, wilson, stats

DATA_DIR = str(Path(__file__).resolve().parent.parent / "data_0" / "lab")


# ═══════════════════════════════════════════════════════════════
# 候选提取（trigger 参数化，其余与 optimize_winrate.extract_candidates 一致）
# ═══════════════════════════════════════════════════════════════

@dataclass
class Cand:
    event_idx: int
    day: str
    side: str
    cross_idx: int
    remaining_sec: int
    range_exp: float | None
    btc_pos: float
    confirm_idx: dict
    other_delta_d: dict
    fill_ask_d: dict
    won: bool
    start_time: int


def extract_candidates(events: list[dict], trigger: float, max_rem: int = 295) -> list[Cand]:
    """提取全部上升沿穿越候选（min_pre_snaps=5，窗口语义与 Go engine 一致）。"""
    cands = []
    for ei, e in enumerate(events):
        snaps = e["snapshots"]
        if e.get("hist_avg_range") is None or len(snaps) < 6:
            continue
        day = datetime.fromtimestamp(e["start_time"], tz=timezone.utc).strftime("%m-%d")
        har = e.get("hist_avg_range")
        open_price = e["open_price"]
        for side in ("yes", "no"):
            other = "no" if side == "yes" else "yes"
            was_above = False
            for i, s in enumerate(snaps):
                is_above = (s[side + "_price"] > trigger
                            and s["remaining_sec"] < max_rem
                            and s["remaining_sec"] > 5)
                if is_above and not was_above and i >= 5:
                    range_exp = abs(s["price"] - open_price) / har if har else None
                    btc_pos = (s["price"] - open_price) / har if har else 0.0
                    confirm_idx, other_delta_d, fill_ask_d = {}, {}, {}
                    for delay in (1, 2, 3):
                        ci = i + delay
                        if ci >= len(snaps):
                            confirm_idx[delay] = -1
                            continue
                        confirm_idx[delay] = ci
                        cs = snaps[ci]
                        other_delta_d[delay] = cs[other + "_price"] - s[other + "_price"]
                        fill_ask_d[delay] = 1.0 - cs[side + "_price"]
                    cands.append(Cand(
                        event_idx=ei, day=day, side=side, cross_idx=i,
                        remaining_sec=s["remaining_sec"],
                        range_exp=range_exp, btc_pos=btc_pos,
                        confirm_idx=confirm_idx, other_delta_d=other_delta_d,
                        fill_ask_d=fill_ask_d,
                        won=(e["outcome"] == 1) if side == "yes" else (e["outcome"] == 0),
                        start_time=e["start_time"],
                    ))
                was_above = is_above
    return cands


def score_candidate(c: Cand, cfg: FlipBacktestConfig):
    """Formula B 评分（与 Go engine / optimize_winrate 一致，ask 口径）。

    扩展: _div_floor 动态属性 —— div < _div_floor 直接否决（研究用）。
    默认 None=仅按 min_divergence 过滤（引擎当前行为）。
    """
    if not (cfg.min_remaining_sec < c.remaining_sec < cfg.max_remaining_sec):
        return None
    delay = cfg.confirm_delay_ticks
    if c.confirm_idx.get(delay, -1) < 0:
        return None
    div = -c.btc_pos if c.side == "yes" else c.btc_pos
    if cfg.min_divergence > 0 and div < cfg.min_divergence:
        return None
    div_floor = getattr(cfg, "_div_floor", None)
    if div_floor is not None and div < div_floor:
        return None
    if c.range_exp is not None and c.range_exp >= cfg.range_exp_max:
        return None
    fill_ask = c.fill_ask_d[delay]
    if cfg.max_entry_price > 0 and fill_ask > cfg.max_entry_price:
        return None
    other_delta = c.other_delta_d[delay]
    score = 0
    if other_delta > cfg.other_delta_vstrong:
        score += cfg.w_other_d5_vstrong
    elif other_delta > cfg.other_delta_strong:
        score += cfg.w_other_d5_strong
    elif other_delta > cfg.other_delta_weak:
        score += cfg.w_other_d5_weak
    if c.range_exp is not None and c.range_exp < cfg.range_exp_threshold:
        score += cfg.w_range_expansion
    if score < cfg.score_entry:
        return None
    return score, fill_ask, other_delta


def select_signals(cands: list[Cand], cfg: FlipBacktestConfig) -> list[dict]:
    """复刻 engine 顺序语义：按 (cross_idx, YES 优先) 扫描，每事件首过者胜。"""
    per_event = defaultdict(list)
    for c in cands:
        per_event[c.event_idx].append(c)
    signals = []
    for ei in sorted(per_event):
        ordered = sorted(per_event[ei], key=lambda c: (c.cross_idx, 0 if c.side == "yes" else 1))
        for c in ordered:
            res = score_candidate(c, cfg)
            if res is None:
                continue
            score, fill_ask, other_delta = res
            signals.append({
                "event_idx": ei, "day": c.day, "side": c.side, "score": score,
                "fill": fill_ask, "won": c.won,
                "pnl": (1.0 - fill_ask) if c.won else -fill_ask,
                "remaining_sec": c.remaining_sec, "other_delta": other_delta,
                "range_exp": c.range_exp, "btc_pos": c.btc_pos,
            })
            break
    return signals


# ═══════════════════════════════════════════════════════════════
# 评估工具
# ═══════════════════════════════════════════════════════════════

TOTAL_HOURS = None  # 由事件数 × 5min 计算

def eval_config(cands: list[Cand], cfg: FlipBacktestConfig,
                days: list[str], label: str) -> dict:
    global TOTAL_HOURS
    sigs = select_signals(cands, cfg)
    s = stats(sigs)
    per_hour = s.get("n", 0) / TOTAL_HOURS if TOTAL_HOURS else 0.0
    bd = day_breakdown(sigs)
    neg_days = sum(1 for v in bd.values() if v["n"] >= 3 and v["ev"] < 0)
    # Split 一致性（完整日）
    full_days = [d for d in days if d not in ("08-05", "08-12")]
    splitA_tr = set(full_days[:-1]); splitA_te = set(full_days[-1:])
    splitB_tr = set(full_days[:4]);  splitB_te = set(full_days[4:])
    def ev_on(dayset):
        ss = stats([x for x in sigs if x["day"] in dayset])
        return (ss["ev"], ss["n"])
    evA_tr, nA_tr = ev_on(splitA_tr); evA_te, _ = ev_on(splitA_te)
    evB_tr, nB_tr = ev_on(splitB_tr); evB_te, _ = ev_on(splitB_te)
    return {
        "label": label, "n": s.get("n", 0), "per_hour": per_hour,
        "wr": s.get("wr", 0), "wr_lo": s.get("wr_lo", 0), "wr_hi": s.get("wr_hi", 0),
        "ev": s.get("ev", 0), "pnl": s.get("pnl", 0), "pf": s.get("pf", 0),
        "avg_fill": s.get("avg_fill", 0),
        "neg_days": neg_days, "bd": bd,
        "evA_tr": evA_tr, "nA_tr": nA_tr, "evA_te": evA_te,
        "evB_tr": evB_tr, "nB_tr": nB_tr, "evB_te": evB_te,
    }


def day_breakdown(signals):
    by_day = defaultdict(list)
    for s in signals:
        by_day[s["day"]].append(s)
    return {d: stats(v) for d, v in sorted(by_day.items())}


def fmt_row(r: dict, show_splits: bool = True) -> str:
    if r["n"] == 0:
        return f"{r['label']:<44} n=0"
    base = (f"{r['label']:<44} n={r['n']:>4} {r['per_hour']:>5.2f}/h "
            f"WR={r['wr']:>5.1f}% ({r['wr_lo']:.0f}-{r['wr_hi']:.0f}) "
            f"EV={r['ev']:>+6.3f} P&L={r['pnl']:>+7.2f} PF={r['pf']:>5.2f} "
            f"fill={r['avg_fill']:.3f} 负日={r['neg_days']}")
    if show_splits:
        base += (f" | A:{r['evA_tr']:+.3f}/{r['evA_te']:+.3f}"
                 f" B:{r['evB_tr']:+.3f}/{r['evB_te']:+.3f}")
    return base


# ═══════════════════════════════════════════════════════════════
# Stage 1: 单维扫描
# ═══════════════════════════════════════════════════════════════

def stage1(cands_by_trigger: dict[float, list[Cand]], days: list[str]):
    base = FlipBacktestConfig()
    print("=" * 130)
    print("  Stage 1: 单维扫描（每次只改一处，其余=当前参数；trigger 0.7 候选池）")
    print("=" * 130)
    cands = cands_by_trigger[0.70]
    results = []

    def one_dim(name, attr, values):
        for v in values:
            cfg = FlipBacktestConfig()
            setattr(cfg, attr, v)
            results.append(eval_config(cands, cfg, days, f"{name}={v}"))

    results.append(eval_config(cands, FlipBacktestConfig(), days, "基线 当前参数"))
    one_dim("min_divergence", "min_divergence", [0.04, 0.03, 0.02, 0.0])
    one_dim("range_exp_thr", "range_exp_threshold", [0.6, 0.75, 1.0])
    for name, weak, strong, vstrong in (
        ("od tiers (0.005,0.01,0.05)", 0.005, 0.01, 0.05),
        ("od tiers (0.005,0.01,0.025)", 0.005, 0.01, 0.025),
    ):
        cfg = FlipBacktestConfig()
        cfg.other_delta_weak, cfg.other_delta_strong, cfg.other_delta_vstrong = weak, strong, vstrong
        results.append(eval_config(cands, cfg, days, name))
    one_dim("score_entry", "score_entry", [1])
    one_dim("confirm_delay", "confirm_delay_ticks", [1, 3])
    one_dim("max_remaining_sec", "max_remaining_sec", [280, 295])
    one_dim("min_remaining_sec", "min_remaining_sec", [25, 15])
    one_dim("max_entry_price", "max_entry_price", [0.48, 0.50])
    # trigger 单独候选池
    for tr in (0.65, 0.62, 0.60):
        cfg = FlipBacktestConfig()
        cfg.trigger_threshold = tr
        results.append(eval_config(cands_by_trigger[tr], cfg, days, f"trigger_threshold={tr}"))

    for r in results:
        print("  " + fmt_row(r, show_splits=False))


# ═══════════════════════════════════════════════════════════════
# Stage 2: 组合网格（目标 2-4/小时）
# ═══════════════════════════════════════════════════════════════

def stage2(cands_by_trigger: dict[float, list[Cand]], days: list[str]):
    print()
    print("=" * 130)
    print("  Stage 2: 组合网格（圆整值；排序看 train EV 双 Split 一致性）")
    print("=" * 130)
    results = []
    # div 策略: (min_divergence, div_floor)
    #   (0.05, None) = 当前参数
    #   (0.03, None) = 平台下界
    #   (0.0,  0.0)  = 允许中性/背离，拒绝同向（div<0）
    #   (0.0, -0.05) = §6.4 变体（拒绝强同向）
    for trigger, (min_div, div_floor), range_thr, min_rem, delay in product(
            [0.70, 0.65], [(0.05, None), (0.03, None), (0.0, 0.0), (0.0, -0.05)],
            [0.5, 1.0], [35, 15], [2],
    ):
        cfg = FlipBacktestConfig()
        cfg.trigger_threshold = trigger
        cfg.min_divergence = min_div
        cfg._div_floor = div_floor
        cfg.range_exp_threshold = range_thr
        cfg.min_remaining_sec = min_rem
        cfg.confirm_delay_ticks = delay
        dv = "d≥0" if div_floor == 0.0 else ("d≥-0.05" if div_floor == -0.05 else f"div={min_div}")
        label = (f"trg={trigger} {dv:>7} range<{range_thr} minrem={min_rem}")
        results.append(eval_config(cands_by_trigger[trigger], cfg, days, label))

    # 参考: §6.4 激进变体（原始语义, div 全放开）
    cfg = FlipBacktestConfig()
    cfg.min_divergence = 0.0
    results.append(eval_config(cands_by_trigger[0.70], cfg, days, "参考 §6.4 div=0 全放开"))

    def sort_key(r):
        ok = (r["evA_tr"] > 0 and r["nA_tr"] >= 25 and r["evB_tr"] > 0 and r["nB_tr"] >= 25)
        return (0 if ok else 1, -min(r["evA_tr"], r["evB_tr"]))

    results.sort(key=sort_key)
    print(f"  {'':44} |  n    /h    WR%   (CI)      EV     P&L     PF   fill  负日 | A/B split EV")
    print("  " + "-" * 126)
    for r in results:
        print("  " + fmt_row(r))


# ═══════════════════════════════════════════════════════════════
# Stage 3: 决赛圈逐日分解
# ═══════════════════════════════════════════════════════════════

def stage3(cands_by_trigger: dict[float, list[Cand]], days: list[str]):
    print()
    print("=" * 130)
    print("  Stage 3: 决赛圈逐日分解（2-4/小时带宽内、train EV 双正者）")
    print("=" * 130)
    finalists = []
    for label, trigger, min_div, div_floor, range_thr, min_rem in (
        ("当前 Formula B (基线)",           0.70, 0.05, None, 0.5, 35),
        ("★推荐 d≥0 range<1.0 minrem=15",  0.70, 0.0,  0.0,  1.0, 15),
        ("★推荐 d≥0 range<1.0 minrem=25",  0.70, 0.0,  0.0,  1.0, 25),
        ("d≥0 range<0.5 minrem=15",        0.70, 0.0,  0.0,  0.5, 15),
        ("d≥0 range<1.0 minrem=35 (保守)", 0.70, 0.0,  0.0,  1.0, 35),
        ("trg=0.65 d≥0 range<1.0 minrem=15", 0.65, 0.0, 0.0, 1.0, 15),
        ("div=0.03 range<1.0 minrem=15 (高质量低频)", 0.70, 0.03, None, 1.0, 15),
        ("div=0.03 range<1.0 minrem=25",    0.70, 0.03, None, 1.0, 25),
    ):
        cfg = FlipBacktestConfig()
        cfg.trigger_threshold = trigger
        cfg.min_divergence = min_div
        cfg._div_floor = div_floor
        cfg.range_exp_threshold = range_thr
        cfg.min_remaining_sec = min_rem
        finalists.append((label, cfg, trigger))

    for label, cfg, trigger in finalists:
        sigs = select_signals(cands_by_trigger[trigger], cfg)
        r = eval_config(cands_by_trigger[trigger], cfg, days, label)
        usd = sum(s["pnl"] * 2.0 / s["fill"] for s in sigs)  # $2 stake/信号
        print(f"\n【{label}】 {fmt_row(r)} | $2注≈{usd:+.0f} USDC/8天")
        print("    逐日: " + " | ".join(
            f"{d} n={v['n']} WR={v['wr']:.0f}% EV={v['ev']:+.2f}" for d, v in r["bd"].items()))


def main():
    global TOTAL_HOURS
    parser = argparse.ArgumentParser(description="频率导向参数扫描")
    parser.add_argument("--stage", type=int, default=1, choices=[1, 2, 3])
    args = parser.parse_args()

    events = load_events(DATA_DIR)
    TOTAL_HOURS = len(events) * 5 / 60
    print(f"事件: {len(events)} 个 ≈ {TOTAL_HOURS:.1f} 小时")

    cands_by_trigger = {tr: extract_candidates(events, tr) for tr in (0.70, 0.65, 0.62, 0.60)}
    for tr, cs in cands_by_trigger.items():
        print(f"  trigger={tr}: {len(cs)} 个穿越候选")
    days = sorted({c.day for cs in cands_by_trigger.values() for c in cs})
    print(f"日期: {days}")
    print(f"目标: 2-4 信号/小时 → 全样本需 n={TOTAL_HOURS*2:.0f}~{TOTAL_HOURS*4:.0f}\n")

    if args.stage == 1:
        stage1(cands_by_trigger, days)
    elif args.stage == 2:
        stage2(cands_by_trigger, days)
    else:
        stage3(cands_by_trigger, days)


if __name__ == "__main__":
    main()
