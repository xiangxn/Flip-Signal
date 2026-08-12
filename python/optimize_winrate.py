#!/usr/bin/env python3
"""
胜率/EV 优化分析 — data_0 全量（Aug 5-12, 1719 事件）。

核心修正（实盘口径）:
  1. yes_price/no_price 存储的是各自订单簿的 BEST BID（见 cmd/flip/main.go bestBid()）。
     实盘 FAK 买入对侧 token 时成交在 ASK = 1 - 触发侧 bid（双token互补，无手续费套利恒等式）。
     旧回测按对侧 bid 成交（entry + other_delta），每笔系统性低估 spread（数据实测 ~0.9 分）。
  2. gate（max_entry_price）实盘是 Trader 用最优卖价校验（trader.go），必须按 ASK 口径应用：
     ask = 1 - 触发侧bid[confirm] ≤ gate ⇔ 触发侧bid[confirm] ≥ 1 - gate。
     引擎当前用对侧 bid 做 gate（engine.go:737），与 Trader 的 ask 校验差一个 spread。

防过拟合设计:
  - 按时间切 train/test（Split A: 8/5-8/10 训练, 8/11-8/12 验证; Split B: 8/5-8/8 训练, 8/9-8/12 验证）。
  - 特征分析 + 参数搜索只在 train 上进行，test 只用于验证，不参与选择。
  - 网格只用"圆整"参数值，拒绝在样本内精调阈值。
  - 输出 Wilson 置信区间 + 逐日分解，检查稳定性。

用法:
  python optimize_winrate.py [--section all|features|search]
"""

import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest_flip_config import FlipBacktestConfig
from backtest_flip_utils import compute_hist_avg_range

DATA_DIR = str(Path(__file__).resolve().parent.parent / "data_0" / "lab")


# ═══════════════════════════════════════════════════════════════
# 数据加载 & 候选穿越点提取（一次提取，多次复用）
# ═══════════════════════════════════════════════════════════════

def load_events(data_dir: str) -> list[dict]:
    events = []
    for path in sorted(Path(data_dir).glob("events_*.jsonl")):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
    events.sort(key=lambda e: e["start_time"])
    compute_hist_avg_range(events, window_N=18)
    return events


@dataclass
class Cand:
    """一次上升沿穿越 0.7 的候选，含全部特征（T=0 与 T+delay 确认）。"""
    event_idx: int
    day: str                 # "MM-DD" (UTC)，用于时间切分
    side: str                # 触发侧 "yes"/"no"
    cross_idx: int
    remaining_sec: int
    # T=0 特征
    path_eff: float
    noise_ratio: float
    flips: int
    range_exp: float | None
    btc_pos: float
    other_bid_cross: float   # 对侧 bid @cross（旧口径 entry）
    entry_ask: float         # 对侧 ASK @cross = 1 - 触发侧 bid @cross（真实可成交价）
    # T+delay 确认（按 delay 分别存储，搜索时由 config 选择口径）
    confirm_idx: dict[int, int]          # delay -> confirm idx（不存在则为 -1）
    other_delta_d: dict[int, float]      # delay -> 对侧 bid 变化（引擎评分特征，bid 口径）
    other_bid_confirm_d: dict[int, float]# delay -> 对侧 bid @confirm（旧口径 fill）
    fill_ask_d: dict[int, float]         # delay -> 对侧 ASK @confirm（实盘成交价）
    spread_confirm_d: dict[int, float]   # delay -> confirm 时刻 spread
    # 结果
    won: bool                # 买对侧是否赢
    # 事件信息（复盘用）
    start_time: int


def extract_candidates(events: list[dict], max_rem: int = 260) -> list[Cand]:
    """提取全部上升沿穿越候选（trigger=0.7, min_pre_snaps=5）。

    windowed 语义与 Go engine / backtest_flip_utils 完全一致：
      is_above = price>0.7 且 min_remaining<rem<max_rem，
      was_above 只跟踪窗口内状态 → 价格全程>0.7 时，进入窗口的首个
      snapshot 也被计为上升沿（这是当前引擎的真实行为，回测必须对齐）。
    max_rem=295 时窗口覆盖首个 snapshot → 退化为原始价格穿越语义。
    """
    import datetime
    from datetime import timezone
    cands = []
    for ei, e in enumerate(events):
        snaps = e["snapshots"]
        if e.get("hist_avg_range") is None or len(snaps) < 6:
            continue
        day = datetime.datetime.fromtimestamp(e["start_time"], tz=timezone.utc).strftime("%m-%d")
        for side in ("yes", "no"):
            other = "no" if side == "yes" else "yes"
            was_above = False
            for i, s in enumerate(snaps):
                is_above = (s[side + "_price"] > 0.7
                            and s["remaining_sec"] < max_rem
                            and s["remaining_sec"] > 5)
                if is_above and not was_above and i >= 5:
                    # ── T=0 特征（与 _score_crossing 一致）──
                    pre = [x["price"] for x in snaps[: i + 1]]
                    open_price = e["open_price"]
                    net_move = abs(s["price"] - open_price)
                    pre_hi, pre_lo = max(pre), min(pre)
                    pre_range = pre_hi - pre_lo
                    if pre_range == 0:
                        was_above = is_above
                        continue
                    path_eff = net_move / pre_range
                    total_path = sum(abs(pre[j] - pre[j - 1]) for j in range(1, len(pre)))
                    noise = total_path / net_move if net_move > 0 else total_path
                    flips = 0
                    for j in range(2, len(pre)):
                        d1 = pre[j - 1] - pre[j - 2]
                        d2 = pre[j] - pre[j - 1]
                        if d1 != 0 and d2 != 0 and (d1 > 0) != (d2 > 0):
                            flips += 1
                    har = e.get("hist_avg_range")
                    range_exp = abs(s["price"] - open_price) / har if har else None
                    btc_pos = (s["price"] - open_price) / har if har else 0.0
                    other_bid_cross = s[other + "_price"]
                    entry_ask = 1.0 - s[side + "_price"]

                    # ── T+delay 确认（delay ∈ {1,2,3,5}，搜索时由 config 选择）──
                    confirm_idx, other_delta_d, other_bid_confirm_d = {}, {}, {}
                    fill_ask_d, spread_confirm_d = {}, {}
                    for delay in (1, 2, 3, 5):
                        ci = i + delay
                        if ci >= len(snaps):
                            confirm_idx[delay] = -1
                            continue
                        confirm_idx[delay] = ci
                        cs = snaps[ci]
                        obc = cs[other + "_price"]
                        od = obc - other_bid_cross
                        fa = 1.0 - cs[side + "_price"]
                        other_delta_d[delay] = od
                        other_bid_confirm_d[delay] = obc
                        fill_ask_d[delay] = fa
                        spread_confirm_d[delay] = fa - obc

                    cands.append(Cand(
                        event_idx=ei, day=day, side=side, cross_idx=i,
                        remaining_sec=s["remaining_sec"],
                        path_eff=path_eff, noise_ratio=noise, flips=flips,
                        range_exp=range_exp, btc_pos=btc_pos,
                        other_bid_cross=other_bid_cross, entry_ask=entry_ask,
                        confirm_idx=confirm_idx, other_delta_d=other_delta_d,
                        other_bid_confirm_d=other_bid_confirm_d,
                        fill_ask_d=fill_ask_d, spread_confirm_d=spread_confirm_d,
                        won=(e["outcome"] == 1) if side == "yes" else (e["outcome"] == 0),
                        start_time=e["start_time"],
                    ))
                was_above = is_above
    return cands


# ═══════════════════════════════════════════════════════════════
# 信号选择（复刻 engine 顺序语义：按 snapshot 顺序，同 tick YES 优先，首过者胜）
# ═══════════════════════════════════════════════════════════════

def score_candidate(c: Cand, cfg: FlipBacktestConfig,
                    gate_mode: str = "ask") -> tuple[int, float, float, float] | None:
    """对候选打分，返回 (score, fill_ask, other_delta, other_bid_confirm) 或 None。

    gate_mode: "ask" 实盘口径（Trader 用最优卖价 vs max_price, trader.go:249）
               "bid" 旧回测口径（引擎用对侧 bid vs max_price, engine.go:737）
    """
    # 窗口过滤（Layer 0）
    if not (cfg.min_remaining_sec < c.remaining_sec < cfg.max_remaining_sec):
        return None
    # 确认 tick 存在（实盘窗口末尾无法确认 → 丢弃）
    delay = cfg.confirm_delay_ticks
    if c.confirm_idx.get(delay, -1) < 0:
        return None
    # 结构性过滤（搜索用，通过 _ 前缀动态属性挂载）
    if getattr(cfg, "_side_only", "both") == "yes" and c.side != "yes":
        return None
    veto_thr = getattr(cfg, "_veto_align", 0.0)  # >0 启用：同向超阈值否决
    min_div = getattr(cfg, "_min_div", -9.0)     # >-9 启用：背离必须超过此值
    # 背离度：正 = BTC 与 PM 反向（YES侧=btc跌, NO侧=btc涨）
    div = -c.btc_pos if c.side == "yes" else c.btc_pos
    if veto_thr > 0 and div < -veto_thr:  # 同向（BTC 与 PM 方向一致）→ 否决
        return None
    if min_div > -9 and div < min_div:    # 背离不足 → 否决
        return None
    # T=0 硬过滤（与 backtest_flip_utils 一致：noise>3.0 否决、path_eff<0.4 否决；
    # 减法实验可通过 _noise_veto/_path_eff_veto 关闭）
    if getattr(cfg, "_noise_veto", True) and c.noise_ratio > 3.0:
        return None
    if getattr(cfg, "_path_eff_veto", True) and c.path_eff < 0.4:
        return None
    if c.range_exp is not None and c.range_exp >= cfg.range_exp_max:
        return None
    # gate
    fill_ask = c.fill_ask_d[delay]
    other_bid_confirm = c.other_bid_confirm_d[delay]
    gate_price = fill_ask if gate_mode == "ask" else other_bid_confirm
    if cfg.max_entry_price > 0 and gate_price > cfg.max_entry_price:
        return None
    # 评分
    other_delta = c.other_delta_d[delay]
    osc = (c.path_eff <= cfg.path_eff_oscillating
           and c.noise_ratio > cfg.noise_ratio_oscillating
           and c.flips > cfg.flips_oscillating)
    score = 0
    if other_delta > cfg.other_delta_vstrong:
        score += cfg.w_other_d5_vstrong
    elif other_delta > cfg.other_delta_strong:
        score += cfg.w_other_d5_strong
    elif other_delta > cfg.other_delta_weak:
        score += cfg.w_other_d5_weak
    if osc:
        score += cfg.w_oscillating
    # F4/F5 低价入场 — 与当前引擎一致：用对侧 bid（entry_cheap_strong/weak 口径）
    if c.other_bid_cross < cfg.entry_cheap_strong:
        score += cfg.w_cheap_entry_strong
    elif c.other_bid_cross < cfg.entry_cheap_weak:
        score += cfg.w_cheap_entry_weak
    if c.range_exp is not None and c.range_exp < cfg.range_exp_threshold:
        score += cfg.w_range_expansion
    btc_extreme = (c.btc_pos < cfg.btc_pos_min) if c.side == "yes" else (c.btc_pos > cfg.btc_pos_max)
    if btc_extreme:
        score += cfg.w_btc_extreme
    if score < cfg.score_entry:
        return None
    return score, fill_ask, other_delta, other_bid_confirm


def select_signals(cands: list[Cand], cfg: FlipBacktestConfig,
                   mode: str = "ask") -> list[dict]:
    """复刻 engine 顺序语义选信号。

    mode: "ask" 实盘口径（成交+gate 均按对侧 ask）
          "bid" 旧回测口径（成交+gate 均按对侧 bid，复刻 backtest_flip_utils）
    """
    per_event: dict[int, list[Cand]] = defaultdict(list)
    for c in cands:
        per_event[c.event_idx].append(c)
    signals = []
    for ei in sorted(per_event):
        # 按 (cross_idx, side YES 优先) 排序 = engine 逐 snapshot 扫描顺序
        ordered = sorted(per_event[ei], key=lambda c: (c.cross_idx, 0 if c.side == "yes" else 1))
        for c in ordered:
            res = score_candidate(c, cfg, gate_mode=mode)
            if res is None:
                continue
            score, fill_ask, other_delta, other_bid_confirm = res
            fill = fill_ask if mode == "ask" else other_bid_confirm
            pnl = (1.0 - fill) if c.won else -fill
            signals.append({
                "event_idx": ei, "day": c.day, "side": c.side,
                "score": score, "fill": fill, "won": c.won, "pnl": pnl,
                "remaining_sec": c.remaining_sec, "other_delta": other_delta,
                "fill_ask": fill_ask, "path_eff": c.path_eff,
                "noise_ratio": c.noise_ratio, "flips": c.flips,
                "range_exp": c.range_exp, "btc_pos": c.btc_pos,
                "entry_ask": c.entry_ask,
                "other_bid_confirm": other_bid_confirm,
                "spread_confirm": c.spread_confirm_d[cfg.confirm_delay_ticks],
            })
            break  # 每事件一注
    return signals


# ═══════════════════════════════════════════════════════════════
# 统计工具
# ═══════════════════════════════════════════════════════════════

def wilson(n: int, k: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 区间（二项比例 95% CI）。"""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def stats(signals: list[dict]) -> dict:
    if not signals:
        return {"n": 0}
    n = len(signals)
    wins = sum(1 for s in signals if s["won"])
    pnl = sum(s["pnl"] for s in signals)
    gross_w = sum(s["pnl"] for s in signals if s["pnl"] > 0)
    gross_l = abs(sum(s["pnl"] for s in signals if s["pnl"] < 0))
    lo, hi = wilson(n, wins)
    fills = [s["fill"] for s in signals]
    return {
        "n": n, "wr": wins / n * 100, "wr_lo": lo * 100, "wr_hi": hi * 100,
        "pnl": pnl, "ev": pnl / n,
        "pf": gross_w / gross_l if gross_l > 0 else float("inf"),
        "avg_fill": sum(fills) / n,
    }


def fmt(s: dict) -> str:
    if s.get("n", 0) == 0:
        return "n=0"
    return (f"n={s['n']:>4} WR={s['wr']:>5.1f}% ({s['wr_lo']:.0f}-{s['wr_hi']:.0f}) "
            f"EV={s['ev']:>+6.3f} P&L={s['pnl']:>+7.2f} PF={s['pf']:>5.2f} "
            f"avg_fill={s['avg_fill']:.3f}")


def day_breakdown(signals: list[dict]) -> dict[str, dict]:
    by_day = defaultdict(list)
    for s in signals:
        by_day[s["day"]].append(s)
    return {d: stats(v) for d, v in sorted(by_day.items())}


# ═══════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════

def section_baseline(cands: list[Cand], days: list[str]):
    print("=" * 100)
    print("  一、基线对比：当前参数在两种成交口径下的表现（data_0 全量）")
    print("=" * 100)
    cfg = FlipBacktestConfig()
    for mode, label in (("bid", "旧口径：按对侧 bid 成交（entry+other_delta，复刻 backtest_flip_utils）"),
                        ("ask", "实盘口径：按对侧 ask 成交（1 - 触发侧 bid）+ ask gate")):
        sigs = select_signals(cands, cfg, mode=mode)
        s = stats(sigs)
        print(f"\n[{label}]")
        print(f"  {fmt(s)}")
        bd = day_breakdown(sigs)
        print("  逐日: " + " | ".join(
            f"{d} n={v['n']} WR={v['wr']:.0f}% P&L={v['pnl']:+.1f}" for d, v in bd.items()))
    # spread 拖累量化
    sigs_ask = select_signals(cands, cfg, mode="ask")
    sp = [s["spread_confirm"] for s in sigs_ask if s["spread_confirm"] > 0]
    if sp:
        import statistics
        print(f"\n  实盘口径信号 confirm 时刻 spread: mean={statistics.mean(sp):.4f} "
              f"median={statistics.median(sp):.4f} (每笔成本差异)")


def section_features(cands: list[Cand], days: list[str]):
    print("\n" + "=" * 100)
    print("  二、特征统计效力（全候选，ask 口径成交假设；train 集）")
    print("=" * 100)
    train_days = days[:-2]
    pool = [c for c in cands if c.day in train_days]
    print(f"  train 集 ({train_days[0]}~{train_days[-1]}): {len(pool)} 个穿越候选\n")

    def bucket_report(label, buckets):
        """buckets: list[(name, predicate)]，打印每桶 WR/EV + chi2。"""
        from scipy import stats as st
        rows = []
        for name, pred in buckets:
            sub = [c for c in pool if pred(c)]
            if not sub:
                rows.append((name, 0, None))
                continue
            w = sum(1 for c in sub if c.won)
            ev = sum((1 - c.fill_ask_d[2]) if c.won else -c.fill_ask_d[2] for c in sub) / len(sub)
            rows.append((name, len(sub), (w, ev)))
        obs = np.array([[tup[0], nn - tup[0]] for name, nn, tup in rows if tup], dtype=float)
        if len(obs) >= 2 and all(obs.sum(axis=1) > 0) and obs.sum() > 0:
            chi2, p, _, _ = st.chi2_contingency(obs)
        else:
            chi2, p = 0.0, 1.0
        print(f"  ── {label}  (χ²={chi2:.1f}, p={p:.3f}) ──")
        for name, n, tup in rows:
            if tup is None:
                print(f"    {name:>28}: n=0")
                continue
            w, ev = tup
            lo, hi = wilson(n, w)
            print(f"    {name:>28}: n={n:>5} WR={w/n*100:>5.1f}% ({lo*100:.0f}-{hi*100:.0f}) "
                  f"EV={ev:>+6.3f}")

    # 1. fill_ask 分桶（实盘成交价, delay=2）
    bucket_report("成交价 fill_ask 分桶 (买对侧 ask, delay=2)", [
        ("fill < 0.30", lambda c: c.fill_ask_d[2] < 0.30),
        ("fill 0.30-0.35", lambda c: 0.30 <= c.fill_ask_d[2] < 0.35),
        ("fill 0.35-0.40", lambda c: 0.35 <= c.fill_ask_d[2] < 0.40),
        ("fill 0.40-0.45", lambda c: 0.40 <= c.fill_ask_d[2] < 0.45),
        ("fill 0.45-0.50", lambda c: 0.45 <= c.fill_ask_d[2] < 0.50),
        ("fill >= 0.50", lambda c: c.fill_ask_d[2] >= 0.50),
    ])

    # 2. other_delta 分桶 (delay=2)
    bucket_report("确认期对侧 bid 变化 other_delta (delay=2)", [
        ("od <= 0", lambda c: c.other_delta_d[2] <= 0),
        ("od 0.00-0.01", lambda c: 0 < c.other_delta_d[2] <= 0.01),
        ("od 0.01-0.02", lambda c: 0.01 < c.other_delta_d[2] <= 0.02),
        ("od 0.02-0.05", lambda c: 0.02 < c.other_delta_d[2] <= 0.05),
        ("od > 0.05", lambda c: c.other_delta_d[2] > 0.05),
    ])

    # 3. BTC 位置（vs Open，历史振幅单位，按触发侧方向校正：正=背离, 负=同向）
    directed = lambda c: (-c.btc_pos) if c.side == "yes" else c.btc_pos
    bucket_report("BTC 位置（方向校正: 正=背离, 负=同向）", [
        ("背离强 (dir > 0.3)", lambda c: directed(c) > 0.3),
        ("背离中 (0.15~0.3)", lambda c: 0.15 < directed(c) <= 0.3),
        ("背离弱 (0.05~0.15)", lambda c: 0.05 < directed(c) <= 0.15),
        ("中性 (-0.05~0.05)", lambda c: abs(directed(c)) <= 0.05),
        ("同向弱 (-0.15~-0.05)", lambda c: -0.15 < directed(c) <= -0.05),
        ("同向强 (<= -0.15)", lambda c: directed(c) <= -0.15),
    ])

    # 4. 振荡
    bucket_report("路径形态", [
        ("振荡 (path_eff<=0.7 & noise>1.5 & flips>1)", lambda c: c.path_eff <= 0.7 and c.noise_ratio > 1.5 and c.flips > 1),
        ("趋势 (path_eff > 0.7)", lambda c: c.path_eff > 0.7),
        ("噪声否决 (noise > 3)", lambda c: c.noise_ratio > 3.0),
        ("低路径效率 (path_eff < 0.4)", lambda c: c.path_eff < 0.4),
    ])

    # 5. range_expansion
    bucket_report("振幅扩张 range_exp（|BTC位移|/历史振幅）", [
        ("< 0.25 (BTC几乎没动)", lambda c: c.range_exp is not None and c.range_exp < 0.25),
        ("0.25-0.5", lambda c: c.range_exp is not None and 0.25 <= c.range_exp < 0.5),
        ("0.5-1.0", lambda c: c.range_exp is not None and 0.5 <= c.range_exp < 1.0),
        ("1.0-1.5", lambda c: c.range_exp is not None and 1.0 <= c.range_exp < 1.5),
        (">= 1.5 (真突破否决)", lambda c: c.range_exp is not None and c.range_exp >= 1.5),
    ])

    # 6. 剩余时间
    bucket_report("剩余时间 remaining_sec @cross", [
        ("> 200s", lambda c: c.remaining_sec > 200),
        ("120-200s", lambda c: 120 < c.remaining_sec <= 200),
        ("60-120s", lambda c: 60 < c.remaining_sec <= 120),
        ("35-60s", lambda c: 35 < c.remaining_sec <= 60),
    ])

    # 7. entry_ask（触发时刻可成交价）
    bucket_report("触发时刻对侧 ask entry_ask", [
        ("< 0.20", lambda c: c.entry_ask < 0.20),
        ("0.20-0.25", lambda c: 0.20 <= c.entry_ask < 0.25),
        ("0.25-0.30", lambda c: 0.25 <= c.entry_ask < 0.30),
        (">= 0.30", lambda c: c.entry_ask >= 0.30),
    ])

    # 8. 触发侧
    bucket_report("触发侧", [
        ("YES>0.7 买 NO", lambda c: c.side == "yes"),
        ("NO>0.7 买 YES", lambda c: c.side == "no"),
    ])


def _select(cands_sub: list[Cand], cfg: FlipBacktestConfig) -> list[dict]:
    return select_signals(cands_sub, cfg, "ask")


def section_search(cands: list[Cand], days: list[str]):
    print("\n" + "=" * 100)
    print("  三、参数搜索（train 上调，test 验证；ask 成交口径）")
    print("=" * 100)
    # 08-05 与 08-12 是半日数据，Split 只用完整日：
    #   Split A: train 08-06~08-10 (5 个完整日), test 08-11 (1 个完整日)
    #   Split B: train 08-06~08-09 (4 日), test 08-10~08-11 (2 日)
    full_days = [d for d in days if d not in ("08-05", "08-12")]
    splitA_train = set(full_days[:-1])
    splitA_test = set(full_days[-1:])
    splitB_train = set(full_days[:4])
    splitB_test = set(full_days[4:])
    print(f"  Split A: train={sorted(splitA_train)} test={sorted(splitA_test)}"
          f" | Split B: train={sorted(splitB_train)} test={sorted(splitB_test)}")

    base = FlipBacktestConfig()
    results = []

    def run(cfg, name):
        st = {}
        for split, (tr, te) in (("A", (splitA_train, splitA_test)),
                                ("B", (splitB_train, splitB_test))):
            st[split] = (
                stats(_select([c for c in cands if c.day in tr], cfg)),
                stats(_select([c for c in cands if c.day in te], cfg)),
            )
        st["full"] = stats(_select(cands, cfg))
        results.append((name, cfg, st))

    def cstr(s):
        if s["n"] == 0:
            return "n=0"
        return f"n={s['n']:>3} WR={s['wr']:>4.0f}% EV={s['ev']:>+.3f} P&L={s['pnl']:>+6.2f}"

    # 当前参数基线
    run(base, "当前参数")

    # ── Stage 1: 单变量消融（一次只改一处，其余保持当前参数）──
    ablations = []
    v = FlipBacktestConfig()
    v._veto_align = 0.05
    ablations.append(("+同向否决 (veto@0.05)", v))
    v = FlipBacktestConfig()
    v._veto_align = 0.1
    ablations.append(("+同向否决 (veto@0.10)", v))
    v = FlipBacktestConfig()
    v._side_only = "yes"
    ablations.append(("+只做YES侧", v))
    v = FlipBacktestConfig()
    v.w_oscillating = 0
    ablations.append(("+振荡权重归零 (w_osc=0)", v))
    v = FlipBacktestConfig()
    v._veto_align = 0.05
    v.w_oscillating = 0
    ablations.append(("+veto@0.05 + w_osc=0", v))
    v = FlipBacktestConfig()
    v._veto_align = 0.05
    v._side_only = "yes"
    v.w_oscillating = 0
    ablations.append(("+veto@0.05 + 只YES侧 + w_osc=0", v))
    for name, cfg in ablations:
        run(cfg, name)

    # ── Stage 2: 粗网格（圆整值）：veto 阈值 × delay × gate × score × od ──
    from itertools import product
    for veto, delay, gate, sc_entry, od_w in product(
            [0.0, 0.05, 0.1], [1, 2, 3], [0.40, 0.45], [5, 6],
            [0.005, 0.01, 0.02],
    ):
        cfg = FlipBacktestConfig()
        cfg.confirm_delay_ticks = delay
        cfg.max_entry_price = gate
        cfg.score_entry = sc_entry
        cfg.other_delta_weak = od_w
        cfg.other_delta_strong = od_w * 2
        cfg.other_delta_vstrong = od_w * 5
        cfg._veto_align = veto
        name = f"S2 veto={veto} delay={delay} gate={gate} sc>={sc_entry} od={od_w}"
        run(cfg, name)

    # ── Stage 3: 侧过滤 × veto 阈值（在 delay=2, gate=0.45, od=0.01 固定）──
    for side_only, veto, sc_entry, delay in product(
            ["both", "yes"], [0.0, 0.05], [5, 6], [1, 2],
    ):
        cfg = FlipBacktestConfig()
        cfg.confirm_delay_ticks = delay
        cfg.score_entry = sc_entry
        cfg._side_only = side_only
        cfg._veto_align = veto
        name = f"S3 side={side_only} veto={veto} sc>={sc_entry} delay={delay}"
        run(cfg, name)

    # ── Stage 4: 背离硬要求（BTC 与 PM 背离 > 阈值才允许入场，策略核心升级）──
    for min_div, delay, sc_entry, side_only, od_w in product(
            [0.0, 0.05, 0.1], [1, 2], [3, 5], ["both", "yes"], [0.005, 0.01],
    ):
        cfg = FlipBacktestConfig()
        cfg.confirm_delay_ticks = delay
        cfg.score_entry = sc_entry
        cfg.other_delta_weak = od_w
        cfg.other_delta_strong = od_w * 2
        cfg.other_delta_vstrong = od_w * 5
        cfg._side_only = side_only
        cfg._min_div = min_div
        name = f"S4 min_div={min_div} delay={delay} sc>={sc_entry} side={side_only} od={od_w}"
        run(cfg, name)

    # ── 排序：一致性优先 —— train EV 在两个 split 均 >0 且 n_train>=25，
    #    再按 (min(train EV), 更大 n) 排；test 仅展示不参与排序 ──
    def sort_key(r):
        name, cfg, st = r
        evs = [st[s][0]["ev"] if st[s][0]["n"] >= 25 else -9 for s in ("A", "B")]
        ns = [st[s][0]["n"] for s in ("A", "B")]
        ok = all(ev > 0 for ev in evs)
        return (0 if ok else 1, -min(evs), -sum(ns))

    results.sort(key=sort_key)
    hdr = f"  {'配置':<40} | {'trainA':>24} | {'testA':>24} | {'trainB':>24} | {'testB':>24} | {'full':>24}"
    print("\n" + hdr)
    print("  " + "-" * len(hdr))
    shown = 0
    for name, cfg, st in results:
        if shown >= 32 and not name.startswith("当前参数") and not name.startswith("+"):
            continue
        shown += 1
        print(f"  {name:<40} | {cstr(st['A'][0]):>24} | {cstr(st['A'][1]):>24} "
              f"| {cstr(st['B'][0]):>24} | {cstr(st['B'][1]):>24} | {cstr(st['full']):>24}")
    print()

    # ── 决赛圈报告：冠军配置的逐日分解 + 置信区间 ──
    finalists = [
        (name, cfg) for name, cfg, st in results
        if name in ("当前参数", "+同向否决 (veto@0.05)", "+同向否决 (veto@0.10)",
                    "+只做YES侧", "S2 veto=0.05 delay=2 gate=0.45 sc>=5 od=0.01",
                    "S2 veto=0.0 delay=1 gate=0.45 sc>=6 od=0.01")
    ]
    for name, cfg in finalists:
        sigs = _select(cands, cfg)
        s = stats(sigs)
        print(f"  【{name}】全量: {fmt(s)}")
        bd = day_breakdown(sigs)
        print("    逐日: " + " | ".join(
            f"{d} n={v['n']} WR={v['wr']:.0f}% EV={v['ev']:+.2f}" for d, v in bd.items()))
        print()


def section_subtract(cands: list[Cand], days: list[str]):
    """特征减法分析：拆特征、拆否决、重标定分数门槛，观察信号量与质量。

    核心问题：当前公式 7 特征叠加 + 3 个硬否决 → 信号稀缺。哪些可以拆？
      特征层证据：振荡 χ²=3.5(p=0.32) 无效；低价入场方向相反（越便宜 WR 越低）；
                 path_eff<0.4 否决滤掉的反而是 EV 偏好的候选；noise>3 否决中性。
    """
    print("\n" + "=" * 100)
    print("  五、特征减法分析（ask 口径；目标：少特征、多信号、质量不降）")
    print("=" * 100)
    full_days = [d for d in days if d not in ("08-05", "08-12")]
    splitA_train = set(full_days[:-1])
    splitA_test = set(full_days[-1:])
    splitB_train = set(full_days[:4])
    splitB_test = set(full_days[4:])

    results = []

    def run(cfg, name, cat):
        st = {}
        for split, (tr, te) in (("A", (splitA_train, splitA_test)),
                                ("B", (splitB_train, splitB_test))):
            st[split] = (
                stats(_select([c for c in cands if c.day in tr], cfg)),
                stats(_select([c for c in cands if c.day in te], cfg)),
            )
        st["full"] = stats(_select(cands, cfg))
        results.append((cat, name, st))

    def cstr(s):
        if s["n"] == 0:
            return "n=0"
        return f"n={s['n']:>3} WR={s['wr']:>4.0f}% EV={s['ev']:>+.3f} P&L={s['pnl']:>+6.2f}"

    base = FlipBacktestConfig()
    run(base, "当前参数 (7特征+3否决, sc>=5)", "基线")

    # ── 1. 单特征删除（其余不动，sc>=5 固定）──
    v = FlipBacktestConfig(); v.w_oscillating = 0
    run(v, "-F3 振荡 (w_osc=0)", "单删")
    v = FlipBacktestConfig(); v.w_cheap_entry_strong = 0; v.w_cheap_entry_weak = 0
    run(v, "-F4/F5 低价入场 (w_cheap=0)", "单删")
    v = FlipBacktestConfig(); v.w_btc_extreme = 0
    run(v, "-F7 BTC背离加分 (w_btc=0)", "单删")
    v = FlipBacktestConfig(); v._path_eff_veto = False
    run(v, "-否决 path_eff<0.4", "单删")
    v = FlipBacktestConfig(); v._noise_veto = False
    run(v, "-否决 noise>3.0", "单删")
    v = FlipBacktestConfig(); v._path_eff_veto = False; v._noise_veto = False
    run(v, "-两个否决 (path_eff+noise)", "单删")
    v = FlipBacktestConfig(); v._path_eff_veto = False; v._noise_veto = False; v.w_oscillating = 0
    run(v, "-两否决 -振荡", "单删")

    # ── 2. 精简公式（od + range + btc 三特征，无 osc/cheap，重标定 sc）──
    def lean(sc, use_btc=True, veto=True, md=None, od_w=0.01):
        v = FlipBacktestConfig()
        v.w_oscillating = 0
        v.w_cheap_entry_strong = 0
        v.w_cheap_entry_weak = 0
        v.w_btc_extreme = 1 if use_btc else 0
        v._path_eff_veto = veto
        v._noise_veto = veto
        v.score_entry = sc
        if md is not None:
            v._min_div = md
        v.other_delta_weak = od_w
        v.other_delta_strong = od_w * 2
        v.other_delta_vstrong = od_w * 5
        return v

    for sc in (3, 4):
        run(lean(sc, use_btc=True, veto=True), f"精简 od+range+btc, sc>={sc}", "精简")
    for sc in (2, 3):
        run(lean(sc, use_btc=False, veto=True), f"精简 od+range, sc>={sc}", "精简")
    for sc in (3, 4):
        run(lean(sc, use_btc=True, veto=False), f"精简 od+range+btc, 无否决, sc>={sc}", "精简")
    for sc in (2, 3):
        run(lean(sc, use_btc=False, veto=False), f"精简 od+range, 无否决, sc>={sc}", "精简")

    # ── 3. 精简 + min_div（唯一硬过滤，其他否决全拆）──
    for md, sc in ((0.05, 3), (0.05, 4), (0.05, 5), (0.0, 3), (0.0, 4)):
        run(lean(sc, use_btc=True, veto=False, md=md), f"精简+min_div={md}, sc>={sc}", "min_div")
    for md, sc in ((0.05, 2), (0.05, 3)):
        run(lean(sc, use_btc=False, veto=False, md=md), f"精简(无btc)+min_div={md}, sc>={sc}", "min_div")

    # ── 输出：按类别，train EV 一致为正且 n 大者优先 ──
    def sort_key(r):
        cat, name, st = r
        evs = [st[s][0]["ev"] if st[s][0]["n"] >= 20 else -9 for s in ("A", "B")]
        ns = [st[s][0]["n"] for s in ("A", "B")]
        ok = all(ev > 0 for ev in evs)
        return (0 if ok else 1, -min(evs), -sum(ns))

    results.sort(key=sort_key)
    hdr = f"  {'配置':<40} | {'trainA':>24} | {'testA':>24} | {'trainB':>24} | {'testB':>24} | {'full':>24}"
    for cat in ("基线", "单删", "精简", "min_div"):
        print(f"\n  ── {cat} ──")
        print(hdr)
        print("  " + "-" * len(hdr))
        for rcat, name, st in results:
            if rcat != cat:
                continue
            print(f"  {name:<40} | {cstr(st['A'][0]):>24} | {cstr(st['A'][1]):>24} "
                  f"| {cstr(st['B'][0]):>24} | {cstr(st['B'][1]):>24} | {cstr(st['full']):>24}")
    print()
    """决赛圈验证：逐日分解、原始穿越语义、div×fill 交叉、实盘换算。"""
    print("\n" + "=" * 100)
    print("  四、决赛圈验证（ask 口径）")
    print("=" * 100)

    def mk(name, **kw):
        cfg = FlipBacktestConfig()
        for k, v in kw.items():
            setattr(cfg, k, v)
        return name, cfg

    finalists = [
        mk("当前参数"),
        mk("冠军A: min_div=0.05 sc>=5", _min_div=0.05),
        mk("冠军B: min_div=0.05 sc>=3", _min_div=0.05, score_entry=3),
        mk("冠军C: min_div=0.0 sc>=5", _min_div=0.0),
        mk("备选: veto@0.05 (保留中性)", _veto_align=0.05),
    ]

    for name, cfg in finalists:
        sigs = _select(cands, cfg)
        s = stats(sigs)
        print(f"\n【{name}】全量: {fmt(s)}")
        bd = day_breakdown(sigs)
        print("  逐日: " + " | ".join(
            f"{d} n={v['n']} WR={v['wr']:.0f}% EV={v['ev']:+.2f}" for d, v in bd.items()))
        # 实盘换算（2 USDC stake，FAK）
        if sigs:
            import numpy as _np
            live_pnl = sum((s["pnl"] * 2.0 / s["fill"]) for s in sigs)
            n_days = max((max(s["event_idx"] for s in sigs) - min(s["event_idx"] for s in sigs)) / 288, 0)
            days_span = (max(x["day"] for x in [{"day": s["day"]} for s in sigs]),
                         min(x["day"] for x in [{"day": s["day"]} for s in sigs]))
            print(f"  实盘换算 (stake=2 USDC/signal, fill 时股数=2/fill): "
                  f"总 P&L ≈ {live_pnl:+.1f} USDC / 8 天")

    # ── 原始穿越语义 vs 窗口语义（引擎当前行为）──
    print("\n  ── 穿越语义稳健性：原始价格穿越 (max_rem=295) vs 引擎窗口语义 (max_rem=260) ──")
    raw_cands = extract_candidates(load_events(DATA_DIR), max_rem=295)
    for name, cfg in finalists:
        s_win = stats(_select(cands, cfg))
        s_raw = stats(_select(raw_cands, cfg))
        print(f"  {name:<36} | 窗口语义: {fmt(s_win):<52} | 原始语义: {fmt(s_raw)}")

    # ── 冠军 div×fill 交叉表 ──
    name, cfg = finalists[1]
    sigs = _select(cands, cfg)
    print(f"\n  ── {name} 的 fill×won 交叉 ──")
    buckets = defaultdict(list)
    for s in sigs:
        f = s["fill"]
        key = ("<0.30" if f < 0.30 else "0.30-0.35" if f < 0.35 else
               "0.35-0.40" if f < 0.40 else "0.40-0.45" if f <= 0.45 else ">0.45")
        buckets[key].append(s)
    for k in ("<0.30", "0.30-0.35", "0.35-0.40", "0.40-0.45", ">0.45"):
        sub = buckets.get(k, [])
        if sub:
            w = sum(1 for s in sub if s["won"])
            ev = sum(s["pnl"] for s in sub) / len(sub)
            print(f"    fill {k:>8}: n={len(sub):>3} WR={w/len(sub)*100:>5.1f}% EV={ev:>+6.3f}")


def main():
    events = load_events(DATA_DIR)
    print(f"事件: {len(events)}")
    cands = extract_candidates(events)
    print(f"穿越候选: {len(cands)}")
    days = sorted(set(c.day for c in cands))
    print(f"日期: {days}\n")

    section = sys.argv[1] if len(sys.argv) > 1 else "all"
    if section in ("all", "baseline"):
        section_baseline(cands, days)
    if section in ("all", "features"):
        section_features(cands, days)
    if section in ("all", "search"):
        section_search(cands, days)
    if section in ("all", "validate"):
        section_validate(cands, days)
    if section in ("all", "subtract"):
        section_subtract(cands, days)


if __name__ == "__main__":
    main()
