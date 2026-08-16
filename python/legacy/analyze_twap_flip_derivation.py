#!/usr/bin/env python3
"""
TWAP 结算真相下的翻转信号重推导 —— 从基率出发（2026-08-15）。

背景: Formula B（Binance 口径标定）在 TWAP 结算下 WR 17.2%/PF 0.87 被证伪
（见 analyze_twap_calibration.py 与 CLAUDE.md）。本脚本回到 2026-08-05
首次推导翻转策略的路径，在 TWAP 结算口径下重做一遍:

  1. 事件级基率: 多少事件出现过一边 >0.7，其中多少最终翻转
  2. 单特征筛选: 分桶 + χ² + 95%CI + EV（对侧 ask 成交）
  3. 理论检验: 用户假设 —— Binance 数据有前置性（TWAP-60 是 60s 均线，
     结算 = avg(spot[T-60,T])，决策时刻已知其中 max(0, 60-remaining)/60
     的样本；spot 与 twap 的缺口对剩余 TWAP 有机械拖拽），
     用 Binance 特征预测 TWAP 结算应优于用 TWAP 特征
  4. 组合探测: top 特征两两组合 + 缺口×剩余时间专项
  5. Split 验证: 按天拆分样本外

铁律 —— 无未来数据:
  * 特征只用 ≤ 确认 tick（穿越 + confirm_delay_ticks）已知的信息
  * 入场价 = 确认时刻对侧 ask = 1 - 触发侧 bid（实盘成交口径）
  * 结算只用 event.outcome（TWAP 官方口径）
  * twap_close_price / close_price 等窗口结束数据禁止进入特征

符号约定: 所有方向性特征"正 = 对翻转有利"。
基线 EV 结构: 每笔买入对侧 ask≈0.23，翻转即赢 (1-ask)，EV/股 = 翻转率 - 成交价。
策略有正 EV 的充要条件是分桶翻转率 > 分桶成交价。

用法:
    python analyze_twap_flip_derivation.py --data ../data/btc
    # data_0 补丁数据（PM 真实结算）:
    python analyze_twap_flip_derivation.py --data ../data_0/lab_resolved \
        --outcome-field market_outcome
"""

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scipy.stats import chi2_contingency, spearmanr

from backtest_flip_config import FlipBacktestConfig
from backtest_flip_utils import load_events_from_dir, compute_hist_avg_range


# ═══════════════════════════════════════════════════════════════
# 穿越观测收集
# ═══════════════════════════════════════════════════════════════

@dataclass
class Crossing:
    """一次上升沿穿越 0.7 的观测（含 ≤ 确认 tick 的特征与最终结算）。"""
    event_start: int
    side: str               # 穿越侧 "yes" / "no"
    cross_idx: int
    cross_count: int        # 该侧第几次穿越（1 起）
    flip: bool              # 穿越侧最终输掉（TWAP 结算）
    # 穿越时刻特征（单位: hist = TWAP 历史振幅）
    spot_pos: float | None  # Binance 现货 vs TWAP 开盘（正=对翻转有利）
    twap_pos: float | None  # TWAP vs TWAP 开盘（正=对翻转有利）
    twap_gap: float | None  # (twap - spot)/hist 方向校正（正=现货已反向，TWAP 将被拖拽）
    range_exp_spot: float | None
    range_exp_twap: float | None
    remaining_sec: int
    trigger_bid: float
    flow_5s: float | None   # signed_flow_5s 方向校正（正=对翻转有利）
    ret_10s: float | None   # ret_10s 方向校正（正=对翻转有利）
    twap_age_ms: int | None
    # 确认 tick 特征
    other_delta: float | None   # 对侧 bid 变化（正=对翻转有利）
    spot_ret_conf: float | None # 确认期 Binance 现货收益，方向校正
    twap_delta_conf: float | None  # 确认期 TWAP 变化，方向校正
    fill: float             # 确认时刻对侧 ask = 1 - 触发侧 bid（fade 成交价）
    follow_fill: float      # 镜像: 确认时刻触发侧 ask = 1 - 对侧 bid（follow 成交价）


def _side_sign(side: str) -> int:
    """方向校正因子: up-positive 特征对 yes 侧取负（翻转=DOWN 赢）。"""
    return -1 if side == "yes" else 1


def collect_crossings(events: list[dict], cfg: FlipBacktestConfig,
                      outcome_field: str = "outcome") -> list[Crossing]:
    """收集每个事件两侧的全部上升沿穿越（有效窗口 + min snaps，与引擎同口径）。

    outcome_field: 结算字段。data/btc 用 "outcome"（TWAP 官方口径）；
    data_0/lab_resolved 用 "market_outcome"（PM 市场真实结算补丁）。
    """
    crossings: list[Crossing] = []
    for event in events:
        if event.get("hist_avg_range") is None:
            continue
        outcome = event.get(outcome_field)
        if outcome is None:
            continue
        hist = event["hist_avg_range"]
        # 结算锚定开盘价: TWAP 官方缺失时回退 Binance 开盘（data_0 补丁数据）
        twap_open = event.get("twap_open_price") or event.get("open_price") or None
        snaps = event["snapshots"]

        for side in ("yes", "no"):
            sgn = _side_sign(side)
            this_key = "yes_price" if side == "yes" else "no_price"
            other_key = "no_price" if side == "yes" else "yes_price"
            was_above = False
            count = 0
            for i, s in enumerate(snaps):
                is_above = (s[this_key] > cfg.trigger_threshold
                            and s["remaining_sec"] < cfg.max_remaining_sec
                            and s["remaining_sec"] > cfg.min_remaining_sec)
                if is_above and not was_above and i >= cfg.min_pre_snaps:
                    count += 1
                    # ── 穿越时刻特征（全部 ≤ 穿越 tick 已知）──
                    spot = s.get("price") or 0
                    twap = s.get("twap_price") or None
                    spot_pos = twap_pos = gap = re_spot = re_twap = None
                    if spot and twap_open and hist:
                        spot_pos = sgn * (spot - twap_open) / hist
                        re_spot = abs(spot - twap_open) / hist
                    if twap and twap_open and hist:
                        twap_pos = sgn * (twap - twap_open) / hist
                        re_twap = abs(twap - twap_open) / hist
                        if spot:
                            # gap 方向: yes 侧 twap>spot 有利(拖拽向下)，no 侧相反
                            gap = (twap - spot) / hist if side == "yes" else (spot - twap) / hist
                    flow = sgn * s.get("signed_flow_5s", 0.0) if s.get("signed_flow_5s") is not None else None
                    ret10 = sgn * s.get("ret_10s", 0.0) if s.get("ret_10s") is not None else None

                    # ── 确认 tick 特征（≤ 确认 tick 已知）──
                    conf = snaps[i + cfg.confirm_delay_ticks] if i + cfg.confirm_delay_ticks < len(snaps) else None
                    if conf is not None:
                        fill = 1.0 - conf[this_key]
                        other_delta = conf[other_key] - s[other_key]
                        spot_ret_conf = None
                        if conf.get("price") and s.get("price") and s["price"] > 0:
                            spot_ret_conf = sgn * (conf["price"] - s["price"]) / s["price"]
                        twap_delta_conf = None
                        if conf.get("twap_price") and s.get("twap_price") and hist:
                            twap_delta_conf = sgn * (conf["twap_price"] - s["twap_price"]) / hist
                    else:
                        fill = 1.0 - s[this_key]
                        other_delta = spot_ret_conf = twap_delta_conf = None

                    # 镜像 follow 成交: 触发侧 ask = 1 - 对侧 bid（同互补口径）
                    follow_fill = 1.0 - (s[other_key] + (other_delta or 0.0))

                    crossings.append(Crossing(
                        event_start=event["start_time"],
                        side=side,
                        cross_idx=i,
                        cross_count=count,
                        flip=(outcome == 1) if side == "yes" else (outcome == 0),
                        spot_pos=spot_pos, twap_pos=twap_pos, twap_gap=gap,
                        range_exp_spot=re_spot, range_exp_twap=re_twap,
                        remaining_sec=s["remaining_sec"], trigger_bid=s[this_key],
                        flow_5s=flow, ret_10s=ret10,
                        twap_age_ms=s.get("twap_age_ms"),
                        other_delta=other_delta, spot_ret_conf=spot_ret_conf,
                        twap_delta_conf=twap_delta_conf, fill=fill,
                        follow_fill=follow_fill,
                    ))
                was_above = is_above
    return crossings


# ═══════════════════════════════════════════════════════════════
# 通用报告
# ═══════════════════════════════════════════════════════════════

def _ci(rate: float, n: int) -> str:
    """二项 95% CI（Wald），n<5 时返回 '—'。"""
    if n < 5:
        return "—"
    se = (rate * (1 - rate) / n) ** 0.5
    return f"±{1.96 * se * 100:.1f}pp"


def bucket_report(title: str, xs: list[Crossing], key: str,
                  buckets: list[tuple[str, float | None, float | None]],
                  baseline: float) -> dict:
    """按特征分桶打印 n/翻转率/95%CI/均价/EV + χ² 检验。

    buckets: [(标签, lo, hi)]，hi=None 表示开区间上界。
    返回每桶信息 dict，供组合探测复用。
    """
    rows = []
    for label, lo, hi in buckets:
        b = [x for x in xs if (v := getattr(x, key)) is not None and lo <= v and (hi is None or v < hi)]
        rows.append((label, lo, hi, b))

    n_total = sum(len(b) for _, _, _, b in rows)
    print(f"\n  【{title}】 (n={n_total}, 基线翻转率 {baseline * 100:.1f}%)")
    print(f"  {'分桶':<18s} {'n':>4s} {'翻转率':>8s} {'95%CI':>12s} "
          f"{'Δ基率':>8s} {'均价':>7s} {'EV/股':>8s}")
    for label, _, _, b in rows:
        if not b:
            print(f"  {label:<18s} {0:>4d}        -")
            continue
        n = len(b)
        fr = sum(1 for x in b if x.flip) / n
        mf = sum(x.fill for x in b) / n
        print(f"  {label:<18s} {n:>4d} {fr * 100:>7.1f}% {_ci(fr, n):>12s} "
              f"{(fr - baseline) * 100:>+7.1f}pp {mf:>7.3f} {fr - mf:>+8.3f}")

    # χ² 独立性检验（分桶 × 翻转）
    table = [[sum(1 for x in b if x.flip), sum(1 for x in b if not x.flip)]
             for _, _, _, b in rows if b]
    if len(table) >= 2 and all(sum(r) > 0 for r in table):
        chi2, p, _, _ = chi2_contingency(table, correction=False)
        print(f"  χ²={chi2:.1f}  p={p:.3f}  {'★' if p < 0.05 else ('△' if p < 0.1 else '·')}")

    # 有序桶的 Spearman 趋势检验
    ordered = [(idx, sum(1 for x in b if x.flip) / len(b))
               for idx, (_, _, _, b) in enumerate(rows) if len(b) >= 5]
    if len(ordered) >= 3:
        rho, p = spearmanr([o[0] for o in ordered], [o[1] for o in ordered])
        print(f"  Spearman ρ={rho:+.2f}  p={p:.3f}（有序桶单调性）")

    return {label: b for label, _, _, b in rows}


# ═══════════════════════════════════════════════════════════════
# 分桶定义
# ═══════════════════════════════════════════════════════════════

FEATURE_BUCKETS = {
    # 注意: spot 类特征以 TWAP 历史振幅为单位，量级是 TWAP 特征的 ~8 倍
    # （spot 5min 位移 ±3~4 hist，TWAP 位移 ±0.5 hist），分桶需按各自尺度
    "spot_pos": [("≤-3 同向", -1e9, -3.0), ("-3~0", -3.0, 0.0),
                 ("0~3", 0.0, 3.0), (">3 强背离", 3.0, None)],
    "twap_pos": [("≤0 同向", -1e9, 0.0), ("0~0.25 弱背离", 0.0, 0.25),
                 ("0.25~0.5 背离", 0.25, 0.5), (">0.5 强背离", 0.5, None)],
    "twap_gap": [("≤-3 同向", -1e9, -3.0), ("-3~0", -3.0, 0.0),
                 ("0~3", 0.0, 3.0), (">3 强缺口", 3.0, None)],
    "range_exp_spot": [("<1.5", -1e9, 1.5), ("1.5-3", 1.5, 3.0),
                       ("3-5", 3.0, 5.0), ("≥5", 5.0, None)],
    "range_exp_twap": [("<0.5", -1e9, 0.5), ("0.5-1.0", 0.5, 1.0),
                       ("1.0-1.5", 1.0, 1.5), ("≥1.5", 1.5, None)],
    "remaining_sec": [("16-60s", 16, 60), ("61-120s", 60, 120),
                      ("121-180s", 120, 180), ("181-240s", 180, 240), ("241-260s", 240, 260)],
    "trigger_bid": [("0.70-0.75", 0.70, 0.75), ("0.75-0.80", 0.75, 0.80),
                    ("≥0.80", 0.80, None)],
    "other_delta": [("≤0", -1e9, 0.0), ("0~0.01", 0.0, 0.01),
                    ("0.01~0.02", 0.01, 0.02), ("0.02~0.05", 0.02, 0.05), (">0.05", 0.05, None)],
    # 正=对翻转有利: 负值 = 现货/TWAP 朝 PM 方向续动（不利）
    "spot_ret_conf": [("<-3bp 现货续动", -1e9, -3e-4), ("±3bp 走平", -3e-4, 3e-4),
                      (">3bp 现货反向", 3e-4, None)],
    "twap_delta_conf": [("≤-0.05 续动", -1e9, -0.05), ("±0.05", -0.05, 0.05),
                        (">0.05 反向", 0.05, None)],
    "flow_5s": [("≤-1 不利", -1e9, -1.0), ("±1", -1.0, 1.0), (">1 有利", 1.0, None)],
    "ret_10s": [("≤-1e-4 不利", -1e9, -1e-4), ("±1e-4", -1e-4, 1e-4), (">1e-4 有利", 1e-4, None)],
    "cross_count": [("第1次", 1, 1.5), ("第2次", 2, 2.5), ("≥第3次", 3, None)],
    "twap_age_ms": [("≤2000 新鲜", -1e9, 2000), ("2000-5000", 2000, 5000),
                    (">5000 过期", 5000, None)],
}


def main():
    parser = argparse.ArgumentParser(description="TWAP 结算真相下的翻转信号重推导")
    parser.add_argument("--data", default="../data/btc/", help="JSONL 数据目录")
    parser.add_argument("--outcome-field", default="outcome",
                        help="结算字段: outcome(TWAP口径, data/btc) 或 "
                             "market_outcome(PM真实结算, data_0/lab_resolved)")
    args = parser.parse_args()
    outcome_field = args.outcome_field

    events = load_events_from_dir(args.data)
    cfg = FlipBacktestConfig()
    compute_hist_avg_range(events, cfg.hist_window_N, "twap")

    # ═══════════════════════════════════════════════════════════
    # Part 1: 事件级基率 —— 全事件中有多少出现过 >0.7，其中多少翻转
    # ═══════════════════════════════════════════════════════════
    valid = [e for e in events if e.get(outcome_field) is not None]
    crossings = collect_crossings(events, cfg, outcome_field)
    first_by_event = {}
    for x in crossings:
        first_by_event.setdefault(x.event_start, x)
    earliest = list(first_by_event.values())

    n_flip_event = sum(1 for x in earliest if x.flip)
    n_cross_event = len(first_by_event)
    yes_first = [x for x in earliest if x.side == "yes"]
    no_first = [x for x in earliest if x.side == "no"]

    print("=" * 62)
    print("  Part 1: 事件级基率（TWAP 结算真相）")
    print("=" * 62)
    print(f"  总事件: {len(events)}  |  有效结算事件: {len(valid)}")
    print(f"  有穿越事件（一边>0.7, 有效窗口）: {n_cross_event}/{len(valid)} "
          f"({n_cross_event / max(len(valid), 1) * 100:.1f}%)")
    print(f"  无穿越事件: {len(valid) - n_cross_event} 个")
    print(f"  事件级翻转（最早穿越侧输掉）: {n_flip_event}/{n_cross_event} "
          f"({n_flip_event / max(n_cross_event, 1) * 100:.1f}%)")
    print(f"  全部穿越观测: {len(crossings)} 个（多穿越会重复计）")
    all_fr = sum(1 for x in crossings if x.flip) / len(crossings)
    all_fill = sum(x.fill for x in crossings) / len(crossings)
    print(f"  全部穿越基线: 翻转率 {all_fr * 100:.1f}%  均价 {all_fill:.3f}  "
          f"EV {all_fr - all_fill:+.3f}/股")
    for label, xs in [("YES 侧最早触发", yes_first), ("NO 侧最早触发", no_first)]:
        if not xs:
            continue
        fr = sum(1 for x in xs if x.flip) / len(xs)
        mf = sum(x.fill for x in xs) / len(xs)
        print(f"    {label}: n={len(xs):>3d}  翻转率 {fr * 100:>5.1f}%  "
              f"均价 {mf:.3f}  EV {fr - mf:+.3f}/股")
    for label, xs in [("YES 侧（全部穿越）", [x for x in crossings if x.side == "yes"]),
                      ("NO 侧（全部穿越）", [x for x in crossings if x.side == "no"])]:
        if not xs:
            continue
        fr = sum(1 for x in xs if x.flip) / len(xs)
        mf = sum(x.fill for x in xs) / len(xs)
        print(f"    {label}: n={len(xs):>3d}  翻转率 {fr * 100:>5.1f}%  "
              f"均价 {mf:.3f}  EV {fr - mf:+.3f}/股")
    print()

    # ═══════════════════════════════════════════════════════════
    # Part 2: 单特征筛选
    # ═══════════════════════════════════════════════════════════
    print("=" * 62)
    print("  Part 2: 单特征筛选（正=对翻转有利；样本小，只看方向性线索）")
    print("=" * 62)
    results = {}
    for key, buckets in FEATURE_BUCKETS.items():
        results[key] = bucket_report(key, crossings, key, buckets, all_fr)

    # ═══════════════════════════════════════════════════════════
    # Part 3: 理论检验 —— Binance 前置性 vs TWAP 特征
    # ═══════════════════════════════════════════════════════════
    print()
    print("=" * 62)
    print("  Part 3: 理论检验 —— Binance 特征（前置）vs TWAP 特征（滞后）")
    print("=" * 62)
    print("""
  TWAP-60 = 60s 均线: 结算 TWAP(T) = avg(spot[T-60,T])，决策时刻
  已知其中 max(0, 60-remaining)/60 的样本。若现货已反向而 TWAP 未及，
  (spot-twap) 缺口对剩余 TWAP 有机械拖拽 —— 这就是 Binance 数据的
  前置性。两种口径的特征量级差 ~8 倍（spot 位移 ±3~4 hist vs TWAP ±0.5 hist），
  直接比原始分桶不可比 —— 这里按各自分布的三分位（T1/T2/T3）比较
  翻转率分离度：分离度越大，该口径特征越能预判 TWAP 结算翻转。""")

    def tercile_flip(xs, key):
        """按特征三分位返回 [(分位, n, 翻转率)]，量级无关的分离度比较。"""
        vals = sorted(getattr(x, key) for x in xs if getattr(x, key) is not None)
        if len(vals) < 30:
            return None
        q1, q2 = vals[len(vals) // 3], vals[2 * len(vals) // 3]
        out = []
        for label, lo, hi in [("低 T1", None, q1), ("中 T2", q1, q2), ("高 T3", q2, None)]:
            b = [x for x in xs if (v := getattr(x, key)) is not None
                 and (lo is None or v >= lo) and (hi is None or v < hi)]
            fr = sum(1 for x in b if x.flip) / len(b) if b else None
            out.append((label, len(b), fr))
        return out

    for tw_key, sp_key, name in [("twap_pos", "spot_pos", "位置背离（vs TWAP开盘）"),
                                 ("twap_delta_conf", "spot_ret_conf", "确认期回归")]:
        print(f"\n  ── {name} ──")
        print(f"  {'三分位':<8s} {'TWAP口径':>20s} {'Binance口径':>22s}")
        tw = tercile_flip(crossings, tw_key)
        sp = tercile_flip(crossings, sp_key)
        if not tw or not sp:
            print("  样本不足")
            continue
        for (l1, n1, f1), (l2, n2, f2) in zip(tw, sp):
            t = f"{f1 * 100:>5.1f}% (n={n1})" if f1 is not None else "—"
            s = f"{f2 * 100:>5.1f}% (n={n2})" if f2 is not None else "—"
            print(f"  {l1:<8s} {t:>20s} {s:>22s}")
        if f1 is not None and f2 is not None:
            sep_t = abs(tw[2][2] - tw[0][2])
            sep_s = abs(sp[2][2] - sp[0][2])
            print(f"  T3-T1 分离度: TWAP {sep_t * 100:+.1f}pp  |  "
                  f"Binance {sep_s * 100:+.1f}pp  （大者更能预判翻转）")

    # ═══════════════════════════════════════════════════════════
    # Part 4: 组合探测
    # ═══════════════════════════════════════════════════════════
    print()
    print("=" * 62)
    print("  Part 4: 组合探测（两两 2×2，中位数切分）")
    print("=" * 62)
    combo_keys = ["spot_pos", "twap_pos", "twap_gap", "range_exp_spot",
                  "range_exp_twap", "remaining_sec", "trigger_bid",
                  "other_delta", "spot_ret_conf", "twap_delta_conf", "cross_count"]
    # 依据 Part 2 的 χ² 显著性排序，取 top 5 做组合
    ranked = []
    for key in combo_keys:
        table = [[sum(1 for x in b if x.flip), sum(1 for x in b if not x.flip)]
                 for b in results[key].values() if len(b) >= 5]
        if len(table) >= 2:
            chi2, p, _, _ = chi2_contingency(table, correction=False)
            ranked.append((p, key))
    ranked.sort()
    top5 = [k for _, k in ranked[:5]]
    print(f"  top-5（χ² 最显著）: {top5}")

    def pair_grid(k1, k2):
        xs = [x for x in crossings
              if getattr(x, k1) is not None and getattr(x, k2) is not None]
        if len(xs) < 20:
            return
        m1 = sorted(getattr(x, k1) for x in xs)[len(xs) // 2]
        m2 = sorted(getattr(x, k2) for x in xs)[len(xs) // 2]
        print(f"\n  ── {k1} × {k2} (切分: {m1:.3f} / {m2:.3f}) ──")
        print(f"  {'组合':<34s} {'n':>4s} {'翻转率':>8s} {'95%CI':>12s} "
              f"{'均价':>7s} {'EV/股':>8s}")
        for l1, p1 in [("高", lambda v: v >= m1), ("低", lambda v: v < m1)]:
            for l2, p2 in [("高", lambda v: v >= m2), ("低", lambda v: v < m2)]:
                b = [x for x in xs if p1(getattr(x, k1)) and p2(getattr(x, k2))]
                if len(b) < 5:
                    continue
                fr = sum(1 for x in b if x.flip) / len(b)
                mf = sum(x.fill for x in b) / len(b)
                print(f"  {k1}{l1} × {k2}{l2:<18s} {len(b):>4d} {fr * 100:>7.1f}% "
                      f"{_ci(fr, len(b)):>12s} {mf:>7.3f} {fr - mf:>+8.3f}")

    for i in range(len(top5)):
        for j in range(i + 1, len(top5)):
            pair_grid(top5[i], top5[j])

    # 专项: 缺口 × 剩余时间（机械拖拽需要时间生效，remaining ≤ 90s 最强）
    print("\n  ── 专项: twap_gap × remaining_sec（机械拖拽窗口）──")
    print(f"  {'组合':<34s} {'n':>4s} {'翻转率':>8s} {'95%CI':>12s} "
          f"{'均价':>7s} {'EV/股':>8s}")
    for g_lo, g_hi, gl in [(0.0, 0.25, "gap 0~0.25"), (0.25, None, "gap >0.25")]:
        for r_lo, r_hi, rl in [(16, 60, "rem≤60s"), (60, 120, "rem 61-120s"),
                               (120, 180, "rem 121-180s"), (180, 260, "rem 181-260s")]:
            b = [x for x in crossings
                 if x.twap_gap is not None and g_lo <= x.twap_gap
                 and (g_hi is None or x.twap_gap < g_hi)
                 and r_lo < x.remaining_sec <= r_hi]
            if len(b) < 5:
                continue
            fr = sum(1 for x in b if x.flip) / len(b)
            mf = sum(x.fill for x in b) / len(b)
            print(f"  {gl:<14s} × {rl:<12s} {len(b):>4d} {fr * 100:>7.1f}% "
                  f"{_ci(fr, len(b)):>12s} {mf:>7.3f} {fr - mf:>+8.3f}")

    # ═══════════════════════════════════════════════════════════
    # Part 5: Split 验证（按天拆分，交叉训练/测试）
    # ═══════════════════════════════════════════════════════════
    print()
    print("=" * 62)
    print("  Part 5: Split 验证（按天拆分，交叉训练/测试）")
    print("=" * 62)
    from datetime import datetime, timezone
    days = sorted({datetime.fromtimestamp(x.event_start, tz=timezone.utc).date()
                   for x in crossings})
    if len(days) < 2:
        print("  数据不足 2 天，跳过。")
    else:
        print(f"  天数: {[str(d) for d in days]}")
        # 候选过滤器: Part 2 中 |Δ基率| ≥ 3pp 且 n ≥ 10 的桶
        candidates = []
        for key, buckets in FEATURE_BUCKETS.items():
            for label, lo, hi in buckets:
                b = [x for x in crossings
                     if (v := getattr(x, key)) is not None and lo <= v and (hi is None or v < hi)]
                if len(b) >= 10 and abs(sum(1 for x in b if x.flip) / len(b) - all_fr) >= 0.03:
                    candidates.append((key, label, lo, hi, b))

        print(f"  候选过滤器（|Δ基率|≥3pp, n≥10）: {len(candidates)} 个")
        print(f"  {'过滤器':<34s} {'全日n':>5s} {'D1翻转率':>8s} "
              f"{'D2翻转率':>8s} {'D1 EV':>8s} {'D2 EV':>8s}")
        for key, label, lo, hi, full in candidates:
            row = f"  {key}.{label:<26s} {len(full):>5d}"
            for d in days:
                b = [x for x in full
                     if datetime.fromtimestamp(x.event_start, tz=timezone.utc).date() == d]
                if len(b) >= 5:
                    fr = sum(1 for x in b if x.flip) / len(b)
                    mf = sum(x.fill for x in b) / len(b)
                    row += f" {fr * 100:>7.1f}% {fr - mf:>+8.3f}"
                else:
                    row += f" {'—':>7s} {'—':>8s}"
            print(row)
        print("  （两日同号（翻转率同侧偏离基线且 EV 同号）才视为可复现线索）")

    # ═══════════════════════════════════════════════════════════
    # Part 6: 镜像检验 —— 跟随穿越侧（fade 的镜子）
    # ═══════════════════════════════════════════════════════════
    print()
    print("=" * 62)
    print("  Part 6: 镜像检验 —— 跟随穿越侧（买入触发侧）")
    print("=" * 62)
    print("""
  fade 基线 EV≈0 意味着镜子另一侧: 77% 时间穿越侧赢。跟随交易在
  穿越+确认后买入触发侧，成交 = 触发侧 ask = 1 - 对侧 bid（同互补
  口径）。fade EV + follow EV = -(价差)，镜子两侧共享同一信息。""")

    def follow_stats(xs):
        n = len(xs)
        w = sum(1 for x in xs if not x.flip)
        mf = sum(x.follow_fill for x in xs) / n
        ev = sum(((1.0 - x.follow_fill) if not x.flip else -x.follow_fill)
                 for x in xs) / n
        return n, w / n, mf, ev

    for label, xs in [("全部穿越", crossings), ("每事件最早穿越", earliest)]:
        n, wr, mf, ev = follow_stats(xs)
        print(f"  跟随-{label}: n={n}  胜率={wr * 100:.1f}%  均价={mf:.3f}  "
              f"EV={ev:+.4f}/股")
    for side in ("yes", "no"):
        xs = [x for x in crossings if x.side == side]
        n, wr, mf, ev = follow_stats(xs)
        print(f"    跟随 {side.upper()} 全部穿越: n={n}  胜率={wr * 100:.1f}%  "
              f"均价={mf:.3f}  EV={ev:+.4f}/股")
        xs = [x for x in earliest if x.side == side]
        n, wr, mf, ev = follow_stats(xs)
        print(f"    跟随 {side.upper()} 每事件最早: n={n}  胜率={wr * 100:.1f}%  "
              f"均价={mf:.3f}  EV={ev:+.4f}/股")

    # 按天稳定性（每事件最早）
    days_f = sorted({datetime.fromtimestamp(x.event_start, tz=timezone.utc).date()
                     for x in earliest})
    if len(days_f) >= 3:
        print(f"\n  按天（跟随-每事件最早，分侧，格式: 胜率/EV）:")
        print(f"  {'侧':<6s}" + "".join(f" {d.strftime('%m-%d'):>13s}" for d in days_f))
        for side in ("yes", "no"):
            row = f"  {side.upper():<6s}"
            for d in days_f:
                xs = [x for x in earliest if x.side == side
                      and datetime.fromtimestamp(x.event_start, tz=timezone.utc).date() == d]
                if len(xs) >= 5:
                    wr = sum(1 for x in xs if not x.flip) / len(xs)
                    ev = sum(((1.0 - x.follow_fill) if not x.flip else -x.follow_fill)
                             for x in xs) / len(xs)
                    row += f" {wr * 100:>4.0f}%/{ev:+.3f}"
                else:
                    row += f" {'—':>13s}"
            print(row)

    print()
    print("=" * 62)
    print("  说明: 所有显著性仅作方向性线索，新数据积累后重跑本脚本。")
    print("=" * 62)


if __name__ == "__main__":
    main()
