#!/usr/bin/env python3
"""
全面特征分析: 对所有通过基础过滤的候选，分析每个特征的预测能力
并找到最优阈值让各个特征都能"说话"。
"""

import argparse
import sys, json, math
from pathlib import Path
from collections import defaultdict
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest_flip_config import FlipBacktestConfig, DEFAULT_CONFIG, ETH_CONFIG
from backtest_flip_utils import (
    load_events_from_dir, compute_hist_avg_range,
    compute_path_efficiency, compute_pre_range, compute_noise_ratio,
    compute_flips, is_oscillating, compute_range_expansion,
    compute_btc_position, compute_other_delta,
)

import os
os.chdir(Path(__file__).resolve().parent)

parser = argparse.ArgumentParser(description="特征分析")
parser.add_argument("--data", default="../data/btc/", help="JSONL 数据目录 (默认: ../data/btc/)")
parser.add_argument("--profile", choices=["btc", "eth"], default="btc", help="参数预设 (默认: btc)")
args = parser.parse_args()

cfg = DEFAULT_CONFIG if args.profile == "btc" else ETH_CONFIG
events = load_events_from_dir(args.data)

# ── Collect ALL candidates that pass basic filters (noise, path_eff, range_exp_max) ──

Candidate = dict  # will have all features + outcome

all_candidates = []

for event in events:
    if event.get("hist_avg_range") is None:
        continue

    for side in ("yes", "no"):
        this_key = "yes_price" if side == "yes" else "no_price"
        other_key = "no_price" if side == "yes" else "yes_price"
        snaps = event["snapshots"]

        cross_idx = None
        for i, s in enumerate(snaps):
            if s[this_key] > cfg.trigger_threshold:
                cross_idx = i
                break

        if cross_idx is None or cross_idx < cfg.min_pre_snaps:
            continue

        cross_snap = snaps[cross_idx]
        pre_prices = [s["price"] for s in snaps[: cross_idx + 1]]
        open_price = event["open_price"]

        net_move = abs(cross_snap["price"] - open_price)
        pre_high, pre_low, pre_range = compute_pre_range(pre_prices)
        if pre_range == 0:
            continue

        path_eff = compute_path_efficiency(pre_prices, open_price)
        noise_ratio_val = compute_noise_ratio(pre_prices, net_move)
        flips_val = compute_flips(pre_prices)

        # Only filter noise and path_eff (keep range_exp for analysis)
        if noise_ratio_val > cfg.noise_ratio_veto_max:
            continue
        if path_eff < cfg.path_eff_veto_min:
            continue

        range_expansion = compute_range_expansion(cross_snap["price"], open_price,
                                                  event.get("hist_avg_range"))
        # Don't filter range_exp >= 2.0 yet — include for analysis
        btc_position = compute_btc_position(cross_snap["price"], open_price,
                                            event.get("hist_avg_range", 0))
        other_delta = compute_other_delta(snaps, cross_idx, other_key, cfg)
        entry_price = cross_snap[other_key]

        # oscillation raw features
        oscillating_current = (
            path_eff <= cfg.path_eff_oscillating
            and noise_ratio_val > cfg.noise_ratio_oscillating
            and flips_val > cfg.flips_oscillating
        )

        # outcome
        if side == "yes":
            won = (event["outcome"] == 1)
            btc_extreme_current = (cfg.btc_pos_min < btc_position < 0)
        else:
            won = (event["outcome"] == 0)
            btc_extreme_current = (0 < btc_position < cfg.btc_pos_max)

        pnl_1share = (1.0 - entry_price) if won else (0.0 - entry_price)

        all_candidates.append({
            "path_eff": path_eff,
            "noise_ratio": noise_ratio_val,
            "flips": flips_val,
            "oscillating": oscillating_current,
            "range_expansion": range_expansion,
            "btc_position": btc_position,
            "btc_extreme": btc_extreme_current,
            "other_delta": other_delta,
            "entry_price": entry_price,
            "won": won,
            "pnl": pnl_1share,
            "side": side,
            "time": event["start_time"],
        })

print(f"基础过滤后候选总数: {len(all_candidates)}")
print(f"  其中 won:  {sum(1 for c in all_candidates if c['won'])} ({sum(1 for c in all_candidates if c['won'])/len(all_candidates)*100:.1f}%)")
print(f"  其中 lost: {sum(1 for c in all_candidates if not c['won'])}")

# ═══════════════════════════════════════════════════════════════
# 1. other_delta 分布 & bin 分析
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 80)
print("  1. other_delta 分布分析")
print("=" * 80)

od_bins = [
    ("≤ -0.05", lambda d: d <= -0.05),
    ("-0.05 ~ -0.02", lambda d: -0.05 < d <= -0.02),
    ("-0.02 ~ 0.0", lambda d: -0.02 < d <= 0.0),
    ("0.0 ~ 0.01", lambda d: 0.0 < d <= 0.01),
    ("0.01 ~ 0.02", lambda d: 0.01 < d <= 0.02),
    ("0.02 ~ 0.03", lambda d: 0.02 < d <= 0.03),
    ("0.03 ~ 0.05", lambda d: 0.03 < d <= 0.05),
    ("0.05 ~ 0.10", lambda d: 0.05 < d <= 0.10),
    ("0.10 ~ 0.20", lambda d: 0.10 < d <= 0.20),
    ("> 0.20", lambda d: d > 0.20),
]
for label, pred in od_bins:
    sub = [c for c in all_candidates if pred(c["other_delta"])]
    if sub:
        w = sum(1 for c in sub if c["won"])
        p = sum(c["pnl"] for c in sub)
        print(f"  {label:>16s}: n={len(sub):>4d}, win={w/len(sub)*100:>5.1f}%, P&L={p:>+7.2f}")

# ═══════════════════════════════════════════════════════════════
# 2. path_eff 分析
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 80)
print("  2. path_eff 分布分析 (BTC路径效率, 越低=越震荡)")
print("=" * 80)

pe_bins = [
    ("≤ 0.5", lambda d: d <= 0.5),
    ("0.5 ~ 0.6", lambda d: 0.5 < d <= 0.6),
    ("0.6 ~ 0.7", lambda d: 0.6 < d <= 0.7),
    ("0.7 ~ 0.8", lambda d: 0.7 < d <= 0.8),
    ("0.8 ~ 0.9", lambda d: 0.8 < d <= 0.9),
    ("0.9 ~ 1.1", lambda d: 0.9 < d <= 1.1),
    ("1.1 ~ 1.5", lambda d: 1.1 < d <= 1.5),
    ("> 1.5", lambda d: d > 1.5),
]
for label, pred in pe_bins:
    sub = [c for c in all_candidates if pred(c["path_eff"])]
    if sub:
        w = sum(1 for c in sub if c["won"])
        p = sum(c["pnl"] for c in sub)
        print(f"  {label:>12s}: n={len(sub):>4d}, win={w/len(sub)*100:>5.1f}%, P&L={p:>+7.2f}")

# ═══════════════════════════════════════════════════════════════
# 3. noise_ratio 分析
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 80)
print("  3. noise_ratio 分布分析 (越高=越震荡)")
print("=" * 80)

nr_bins = [
    ("≤ 0.8", lambda d: d <= 0.8),
    ("0.8 ~ 1.0", lambda d: 0.8 < d <= 1.0),
    ("1.0 ~ 1.2", lambda d: 1.0 < d <= 1.2),
    ("1.2 ~ 1.5", lambda d: 1.2 < d <= 1.5),
    ("1.5 ~ 2.0", lambda d: 1.5 < d <= 2.0),
    ("2.0 ~ 2.5", lambda d: 2.0 < d <= 2.5),
    ("2.5 ~ 3.0", lambda d: 2.5 < d <= 3.0),
]
for label, pred in nr_bins:
    sub = [c for c in all_candidates if pred(c["noise_ratio"])]
    if sub:
        w = sum(1 for c in sub if c["won"])
        p = sum(c["pnl"] for c in sub)
        print(f"  {label:>12s}: n={len(sub):>4d}, win={w/len(sub)*100:>5.1f}%, P&L={p:>+7.2f}")

# ═══════════════════════════════════════════════════════════════
# 4. flips 分析
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 80)
print("  4. flips 分布分析 (翻转次数)")
print("=" * 80)

fl_bins = [
    ("0", lambda d: d == 0),
    ("1", lambda d: d == 1),
    ("2", lambda d: d == 2),
    ("3 ~ 4", lambda d: 3 <= d <= 4),
    ("5 ~ 7", lambda d: 5 <= d <= 7),
    ("8 ~ 10", lambda d: 8 <= d <= 10),
    ("> 10", lambda d: d > 10),
]
for label, pred in fl_bins:
    sub = [c for c in all_candidates if pred(c["flips"])]
    if sub:
        w = sum(1 for c in sub if c["won"])
        p = sum(c["pnl"] for c in sub)
        print(f"  {label:>12s}: n={len(sub):>4d}, win={w/len(sub)*100:>5.1f}%, P&L={p:>+7.2f}")

# ═══════════════════════════════════════════════════════════════
# 5. btc_position 分析
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 80)
print("  5. btc_position 分布 (vs Open, 以 hist_avg_range 为单位)")
print("=" * 80)

bp_bins = [
    ("< -1.0", lambda d: d < -1.0),
    ("-1.0 ~ -0.5", lambda d: -1.0 <= d < -0.5),
    ("-0.5 ~ -0.3", lambda d: -0.5 <= d < -0.3),
    ("-0.3 ~ -0.1", lambda d: -0.3 <= d < -0.1),
    ("-0.1 ~ 0.0", lambda d: -0.1 <= d < 0.0),
    (" 0.0 ~ 0.1", lambda d: 0.0 <= d < 0.1),
    (" 0.1 ~ 0.3", lambda d: 0.1 <= d < 0.3),
    (" 0.3 ~ 0.5", lambda d: 0.3 <= d < 0.5),
    (" 0.5 ~ 1.0", lambda d: 0.5 <= d < 1.0),
    ("> 1.0", lambda d: d >= 1.0),
]
for label, pred in bp_bins:
    sub = [c for c in all_candidates if pred(c["btc_position"])]
    if sub:
        w = sum(1 for c in sub if c["won"])
        p = sum(c["pnl"] for c in sub)
        print(f"  {label:>16s}: n={len(sub):>4d}, win={w/len(sub)*100:>5.1f}%, P&L={p:>+7.2f}")

# ═══════════════════════════════════════════════════════════════
# 6. range_expansion 分析
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 80)
print("  6. range_expansion 分布 (BTC 实际振幅 / 历史平均振幅)")
print("=" * 80)

re_bins = [
    ("≤ 0.3", lambda d: d is not None and d <= 0.3),
    ("0.3 ~ 0.5", lambda d: d is not None and 0.3 < d <= 0.5),
    ("0.5 ~ 0.7", lambda d: d is not None and 0.5 < d <= 0.7),
    ("0.7 ~ 1.0", lambda d: d is not None and 0.7 < d <= 1.0),
    ("1.0 ~ 1.5", lambda d: d is not None and 1.0 < d <= 1.5),
    ("1.5 ~ 2.0", lambda d: d is not None and 1.5 < d <= 2.0),
    ("> 2.0", lambda d: d is not None and d > 2.0),
    ("None", lambda d: d is None),
]
for label, pred in re_bins:
    sub = [c for c in all_candidates if pred(c["range_expansion"])]
    if sub:
        w = sum(1 for c in sub if c["won"])
        p = sum(c["pnl"] for c in sub)
        print(f"  {label:>12s}: n={len(sub):>4d}, win={w/len(sub)*100:>5.1f}%, P&L={p:>+7.2f}")

# ═══════════════════════════════════════════════════════════════
# 7. entry_price 分析
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 80)
print("  7. entry_price 分布 (对手方价格 = 入场成本)")
print("=" * 80)

ep_bins = [
    ("≤ 0.10", lambda d: d <= 0.10),
    ("0.10 ~ 0.15", lambda d: 0.10 < d <= 0.15),
    ("0.15 ~ 0.20", lambda d: 0.15 < d <= 0.20),
    ("0.20 ~ 0.24", lambda d: 0.20 < d <= 0.24),
    ("0.24 ~ 0.27", lambda d: 0.24 < d <= 0.27),
    ("> 0.27", lambda d: d > 0.27),
]
for label, pred in ep_bins:
    sub = [c for c in all_candidates if pred(c["entry_price"])]
    if sub:
        w = sum(1 for c in sub if c["won"])
        p = sum(c["pnl"] for c in sub)
        print(f"  {label:>12s}: n={len(sub):>4d}, win={w/len(sub)*100:>5.1f}%, P&L={p:>+7.2f}, mean_entry={sum(c['entry_price'] for c in sub)/len(sub):.3f}")

# ═══════════════════════════════════════════════════════════════
# 8. btc_extreme 按 side 分别分析
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 80)
print("  8. btc_extreme 按 side/bucket 分析")
print("=" * 80)

for side_label, side_filter in [("YES >0.7 (赌DOWN)", lambda c: c["side"] == "yes"),
                                  ("NO >0.7  (赌UP)", lambda c: c["side"] == "no")]:
    sub = [c for c in all_candidates if side_filter(c)]
    if not sub:
        continue
    print(f"\n  [{side_label}] n={len(sub)}")

    if side_filter == (lambda c: c["side"] == "yes"):
        bp_bins_side = [
            ("btc_pos < -0.5 (BTC大跌)", lambda d: d < -0.5),
            ("-0.5 ~ -0.3", lambda d: -0.5 <= d < -0.3),
            ("-0.3 ~ -0.1", lambda d: -0.3 <= d < -0.1),
            ("-0.1 ~ 0.0 (微跌)", lambda d: -0.1 <= d < 0.0),  # this IS flippedge for YES
            ("0.0 ~ 0.1  (微涨)", lambda d: 0.0 <= d < 0.1),
            ("0.1 ~ 0.3", lambda d: 0.1 <= d < 0.3),
            ("0.3 ~ 0.5", lambda d: 0.3 <= d < 0.5),
            ("btc_pos > 0.5 (BTC大涨)", lambda d: d >= 0.5),
        ]
        for bl, bp in bp_bins_side:
            s2 = [c for c in sub if bp(c["btc_position"])]
            if s2:
                w = sum(1 for c in s2 if c["won"])
                p = sum(c["pnl"] for c in s2)
                print(f"    {bl:28s}: n={len(s2):>3d}, win={w/len(s2)*100:>5.1f}%, P&L={p:>+7.2f}")
    else:
        bp_bins_side = [
            ("btc_pos < -0.5 (BTC大跌)", lambda d: d < -0.5),
            ("-0.5 ~ -0.3", lambda d: -0.5 <= d < -0.3),
            ("-0.3 ~ -0.1", lambda d: -0.3 <= d < -0.1),
            ("-0.1 ~ 0.0 (微跌)", lambda d: -0.1 <= d < 0.0),
            ("0.0 ~ 0.1  (微涨)", lambda d: 0.0 <= d < 0.1),  # this IS flippedge for NO
            ("0.1 ~ 0.3", lambda d: 0.1 <= d < 0.3),
            ("0.3 ~ 0.5", lambda d: 0.3 <= d < 0.5),
            ("btc_pos > 0.5 (BTC大涨)", lambda d: d >= 0.5),
        ]
        for bl, bp in bp_bins_side:
            s2 = [c for c in sub if bp(c["btc_position"])]
            if s2:
                w = sum(1 for c in s2 if c["won"])
                p = sum(c["pnl"] for c in s2)
                print(f"    {bl:28s}: n={len(s2):>3d}, win={w/len(s2)*100:>5.1f}%, P&L={p:>+7.2f}")

# ═══════════════════════════════════════════════════════════════
# 9. 振荡检测的 3 条件各自独立分析
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 80)
print("  9. 振荡三条件各自独立预测能力")
print("=" * 80)

for label, pred, cond_desc in [
    ("path_eff ≤ 0.5 (当前)", lambda c: c["path_eff"] <= 0.5, "path_eff ≤ 0.5"),
    ("path_eff ≤ 0.6", lambda c: c["path_eff"] <= 0.6, "path_eff ≤ 0.6"),
    ("path_eff ≤ 0.7", lambda c: c["path_eff"] <= 0.7, "path_eff ≤ 0.7"),
    ("path_eff ≤ 0.8", lambda c: c["path_eff"] <= 0.8, "path_eff ≤ 0.8"),
    ("noise_ratio > 5.0 (当前)", lambda c: c["noise_ratio"] > 5.0, "noise > 5"),
    ("noise_ratio > 2.5", lambda c: c["noise_ratio"] > 2.5, "noise > 2.5"),
    ("noise_ratio > 2.0", lambda c: c["noise_ratio"] > 2.0, "noise > 2.0"),
    ("noise_ratio > 1.5", lambda c: c["noise_ratio"] > 1.5, "noise > 1.5"),
    ("flips > 2 (当前)", lambda c: c["flips"] > 2, "flips > 2"),
    ("flips > 1", lambda c: c["flips"] > 1, "flips > 1"),
    ("flips > 0", lambda c: c["flips"] > 0, "flips > 0"),
]:
    sub = [c for c in all_candidates if pred(c)]
    if sub:
        w = sum(1 for c in sub if c["won"])
        p = sum(c["pnl"] for c in sub)
        print(f"  {label:30s}: n={len(sub):>4d}, win={w/len(sub)*100:>5.1f}%, P&L={p:>+7.2f}")
    else:
        print(f"  {label:30s}: n=   0 (从未触发!)")

# ═══════════════════════════════════════════════════════════════
# 10. 振荡三条件 vs other_delta 各区间组合分析
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 80)
print("  10. 放宽后振荡 vs other_delta 交叉分析 (path_eff≤0.8, noise>1.5, flips>1)")
print("=" * 80)

# Relaxed oscillation
def relaxed_osc(c):
    return c["path_eff"] <= 0.8 and c["noise_ratio"] > 1.5 and c["flips"] > 1

osc_relaxed = [c for c in all_candidates if relaxed_osc(c)]
osc_not = [c for c in all_candidates if not relaxed_osc(c)]
print(f"  放宽振荡: n={len(osc_relaxed)}, win={sum(1 for c in osc_relaxed if c['won'])/len(osc_relaxed)*100:.1f}%, P&L={sum(c['pnl'] for c in osc_relaxed):+.2f}" if osc_relaxed else "  放宽振荡: 0个")
print(f"  非振荡:   n={len(osc_not)}, win={sum(1 for c in osc_not if c['won'])/len(osc_not)*100:.1f}%, P&L={sum(c['pnl'] for c in osc_not):+.2f}" if osc_not else "  非振荡: 0个")

# Cross with other_delta
for od_label, od_pred in [
    ("od < 0.01", lambda c: c["other_delta"] < 0.01),
    ("0.01 ≤ od < 0.03", lambda c: 0.01 <= c["other_delta"] < 0.03),
    ("od ≥ 0.03", lambda c: c["other_delta"] >= 0.03),
]:
    osc_sub = [c for c in osc_relaxed if od_pred(c)]
    not_sub = [c for c in osc_not if od_pred(c)]
    if osc_sub:
        w = sum(1 for c in osc_sub if c['won'])
        p = sum(c['pnl'] for c in osc_sub)
        print(f"  [{od_label}] 振荡: n={len(osc_sub):>3d}, win={w/len(osc_sub)*100:>5.1f}%, P&L={p:>+7.2f}")
    if not_sub:
        w = sum(1 for c in not_sub if c['won'])
        p = sum(c['pnl'] for c in not_sub)
        print(f"  [{od_label}] 非振荡: n={len(not_sub):>3d}, win={w/len(not_sub)*100:>5.1f}%, P&L={p:>+7.2f}")

# ═══════════════════════════════════════════════════════════════
# 11. btc_extreme 放宽分析
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 80)
print("  11. btc_extreme 放宽分析")
print("=" * 80)

# For YES side (赌DOWN): btc_pos < 0 means BTC跌, PM看涨 → 背离
# For NO side (赌UP): btc_pos > 0 means BTC涨, PM看跌 → 背离
for label, pred in [
    ("YES: btc_pos < -0.3", lambda c: c["side"] == "yes" and c["btc_position"] < -0.3),
    ("YES: btc_pos < -0.1", lambda c: c["side"] == "yes" and c["btc_position"] < -0.1),
    ("YES: btc_pos < 0", lambda c: c["side"] == "yes" and c["btc_position"] < 0),
    ("YES: btc_pos -0.5~0", lambda c: c["side"] == "yes" and -0.5 < c["btc_position"] < 0),
    ("NO: btc_pos > 0.3", lambda c: c["side"] == "no" and c["btc_position"] > 0.3),
    ("NO: btc_pos > 0.1", lambda c: c["side"] == "no" and c["btc_position"] > 0.1),
    ("NO: btc_pos > 0", lambda c: c["side"] == "no" and c["btc_position"] > 0),
    ("NO: btc_pos 0~0.5", lambda c: c["side"] == "no" and 0 < c["btc_position"] < 0.5),
]:
    sub = [c for c in all_candidates if pred(c)]
    if sub:
        w = sum(1 for c in sub if c['won'])
        p = sum(c['pnl'] for c in sub)
        print(f"  {label:30s}: n={len(sub):>4d}, win={w/len(sub)*100:>5.1f}%, P&L={p:>+7.2f}")

# ═══════════════════════════════════════════════════════════════
# 12. 多特征组合搜索 — 找到最佳评分组合
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 80)
print("  12. 特征组合穷举 — 寻找最优评分公式")
print("=" * 80)

# Define feature detectors with different thresholds
features = []

# other_delta variants
for thresh, weight, name in [(0.05, 3, "od>0.05"), (0.04, 3, "od>0.04"), (0.03, 3, "od>0.03"),
                               (0.02, 2, "od>0.02"), (0.015, 2, "od>0.015"),
                               (0.01, 1, "od>0.01")]:
    features.append((name, lambda c, t=thresh: c["other_delta"] > t, weight))

# path_eff variants (lower = more oscillating = good for flip)
for thresh, weight, name in [(0.5, 2, "pe≤0.5"), (0.6, 2, "pe≤0.6"), (0.7, 2, "pe≤0.7"), (0.8, 2, "pe≤0.8")]:
    features.append((name, lambda c, t=thresh: c["path_eff"] <= t, weight))

# noise_ratio variants
for thresh, weight, name in [(2.5, 2, "nr>2.5"), (2.0, 2, "nr>2.0"), (1.5, 2, "nr>1.5"), (1.2, 1, "nr>1.2")]:
    features.append((name, lambda c, t=thresh: c["noise_ratio"] > t, weight))

# flips
for thresh, weight, name in [(3, 1, "flips>3"), (2, 1, "flips>2"), (1, 1, "flips>1")]:
    features.append((name, lambda c, t=thresh: c["flips"] > t, weight))

# btc_extreme
features.append(("btc_ext_yes", lambda c: c["side"] == "yes" and c["btc_position"] < 0, 1))
features.append(("btc_ext_no", lambda c: c["side"] == "no" and c["btc_position"] > 0, 1))
features.append(("btc_ext_yes_tight", lambda c: c["side"] == "yes" and -0.5 < c["btc_position"] < 0, 1))
features.append(("btc_ext_no_tight", lambda c: c["side"] == "no" and 0 < c["btc_position"] < 0.5, 1))

# range_expansion (BTC barely moved → PM overconfident)
features.append(("re<0.5", lambda c: c["range_expansion"] is not None and c["range_expansion"] < 0.5, 2))
features.append(("re<0.3", lambda c: c["range_expansion"] is not None and c["range_expansion"] < 0.3, 2))
features.append(("re<0.7", lambda c: c["range_expansion"] is not None and c["range_expansion"] < 0.7, 1))

# entry_price
features.append(("entry<0.20", lambda c: c["entry_price"] < 0.20, 2))
features.append(("entry<0.25", lambda c: c["entry_price"] < 0.25, 1))
features.append(("entry<0.15", lambda c: c["entry_price"] < 0.15, 2))

# Now test: for each candidate, score with ALL features, then find best threshold
# Instead of full combinatorial search, let's test a few sensible formula candidates

formulas = [
    # Current formula (baseline)
    {
        "name": "当前公式",
        "features": [
            ("od>0.03", lambda c: c["other_delta"] > 0.03, 4),
            ("od>0.01", lambda c: c["other_delta"] > 0.01, 2),
            ("osc", lambda c: c["oscillating"], 1),
            ("entry<0.20", lambda c: c["entry_price"] < 0.20, 0),  # weight=0
            ("entry<0.25", lambda c: c["entry_price"] < 0.25, 1),
            ("re<0.5", lambda c: c["range_expansion"] is not None and c["range_expansion"] < 0.5, 2),
            ("btc_ext", lambda c: c["btc_extreme"], 1),
        ],
        "entry_score": 5,
    },
    # Formula A: 降低 other_delta 权重, 激活振荡和 btc_extreme
    {
        "name": "A: 降低od主导+放宽振荡+btc",
        "features": [
            ("od>0.05", lambda c: c["other_delta"] > 0.05, 3),
            ("od>0.02", lambda c: c["other_delta"] > 0.02, 2),
            ("od>0.01", lambda c: c["other_delta"] > 0.01, 1),
            ("osc_relaxed", lambda c: c["path_eff"] <= 0.8 and c["noise_ratio"] > 1.5 and c["flips"] > 1, 2),
            ("entry<0.25", lambda c: c["entry_price"] < 0.25, 1),
            ("entry<0.20", lambda c: c["entry_price"] < 0.20, 1),
            ("re<0.5", lambda c: c["range_expansion"] is not None and c["range_expansion"] < 0.5, 2),
            ("btc_ext_wide", lambda c: (c["side"] == "yes" and c["btc_position"] < -0.1) or (c["side"] == "no" and c["btc_position"] > 0.1), 1),
        ],
        "entry_score": 5,
    },
    # Formula B: more balanced
    {
        "name": "B: 平衡公式(od降权+振荡+btc+entry+range)",
        "features": [
            ("od>0.04", lambda c: c["other_delta"] > 0.04, 3),
            ("od>0.02", lambda c: c["other_delta"] > 0.02, 2),
            ("od>0.01", lambda c: c["other_delta"] > 0.01, 1),
            ("pe≤0.7", lambda c: c["path_eff"] <= 0.7, 1),
            ("nr>2.0", lambda c: c["noise_ratio"] > 2.0, 1),
            ("flips>2", lambda c: c["flips"] > 2, 1),
            ("entry<0.25", lambda c: c["entry_price"] < 0.25, 1),
            ("re<0.5", lambda c: c["range_expansion"] is not None and c["range_expansion"] < 0.5, 2),
            ("btc_ext", lambda c: (c["side"] == "yes" and c["btc_position"] < -0.1) or (c["side"] == "no" and c["btc_position"] > 0.1), 1),
        ],
        "entry_score": 5,
    },
    # Formula C: 纯成交量（high specificity, low recall）
    {
        "name": "C: 高质量(高门槛少信号)",
        "features": [
            ("od>0.03", lambda c: c["other_delta"] > 0.03, 3),
            ("od>0.01", lambda c: c["other_delta"] > 0.01, 1),
            ("pe≤0.6", lambda c: c["path_eff"] <= 0.6, 2),
            ("nr>2.5", lambda c: c["noise_ratio"] > 2.5, 1),
            ("entry<0.20", lambda c: c["entry_price"] < 0.20, 1),
            ("re<0.3", lambda c: c["range_expansion"] is not None and c["range_expansion"] < 0.3, 2),
        ],
        "entry_score": 6,
    },
    # Formula D: 保留 od 硬过滤但降低到 0.01，只过滤对面下跌的情况
    {
        "name": "D: od硬过滤降到0.01+放宽振荡+range",
        "features": [
            ("od>0.03", lambda c: c["other_delta"] > 0.03, 4),
            ("od>0.01", lambda c: c["other_delta"] > 0.01, 2),
            ("osc_relaxed", lambda c: c["path_eff"] <= 0.8 and c["noise_ratio"] > 1.5 and c["flips"] > 1, 2),
            ("entry<0.25", lambda c: c["entry_price"] < 0.25, 1),
            ("re<0.5", lambda c: c["range_expansion"] is not None and c["range_expansion"] < 0.5, 2),
            ("btc_ext", lambda c: (c["side"] == "yes" and c["btc_position"] < -0.1) or (c["side"] == "no" and c["btc_position"] > 0.1), 1),
        ],
        "entry_score": 5,
        "od_hard_filter": 0.01,
    },
]

print(f"\n{'Formula':<30s} {'Signals':>7s} {'WinRate':>8s} {'P&L':>9s} {'AvgSc':>6s} {'PF':>6s} {'Sc5WR':>7s} {'Sc6WR':>7s} {'Sc7WR':>7s}")
print("-" * 105)

for formula in formulas:
    scores = []
    for c in all_candidates:
        # Optional hard filter
        if "od_hard_filter" in formula and c["other_delta"] < formula["od_hard_filter"]:
            continue
        if c["range_expansion"] is not None and c["range_expansion"] >= 2.0:
            continue  # F0 veto always applies

        score = 0
        for name, pred, weight in formula["features"]:
            if pred(c):
                score += weight
        c_with_score = dict(c)
        c_with_score["score"] = score
        scores.append(c_with_score)

    # Filter by entry score
    passed = [c for c in scores if c["score"] >= formula["entry_score"]]
    if passed:
        n = len(passed)
        wins = sum(1 for c in passed if c["won"])
        total_pnl = sum(c["pnl"] for c in passed)
        avg_score = sum(c["score"] for c in passed) / n
        losers_pnl = abs(sum(c["pnl"] for c in passed if c["pnl"] < 0))
        pf = total_pnl / losers_pnl if losers_pnl > 0 else float('inf')

        # By score bucket
        sc5 = [c for c in passed if c["score"] == 5]
        sc6 = [c for c in passed if c["score"] == 6]
        sc7p = [c for c in passed if c["score"] >= 7]
        wr5 = f"{sum(1 for c in sc5 if c['won'])/len(sc5)*100:.0f}%" if sc5 else "-"
        wr6 = f"{sum(1 for c in sc6 if c['won'])/len(sc6)*100:.0f}%" if sc6 else "-"
        wr7 = f"{sum(1 for c in sc7p if c['won'])/len(sc7p)*100:.0f}%" if sc7p else "-"
        print(f"{formula['name']:<30s} {n:>7d} {wins/n*100:>7.1f}% {total_pnl:>+9.2f} {avg_score:>6.1f} {pf:>6.1f} {wr5:>7s} {wr6:>7s} {wr7:>7s}")
    else:
        print(f"{formula['name']:<30s} {0:>7d} {'-':>8s} {'-':>9s} {'-':>6s} {'-':>6s}")

# ═══════════════════════════════════════════════════════════════
# 13. 最终推荐: 详细展示最佳公式
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 80)
print("  13. 推荐公式详细回测")
print("=" * 80)

# Let's try one more: the best balanced approach
# Goal: other_delta no longer dominates, other features contribute meaningfully

best_formula = {
    "name": "推荐: 多维度平衡评分",
    "features": [
        # other_delta: still important but less dominant
        ("od>0.05", lambda c: c["other_delta"] > 0.05, 3),
        ("od>0.02", lambda c: c["other_delta"] > 0.02, 2),
        ("od>0.01", lambda c: c["other_delta"] > 0.01, 1),
        # Oscillation (relaxed): path_eff low + noise high + flips
        ("pe≤0.8", lambda c: c["path_eff"] <= 0.8, 1),
        ("nr>2.0", lambda c: c["noise_ratio"] > 2.0, 1),
        ("flips>1", lambda c: c["flips"] > 1, 1),
        # BTC extreme: BTC direction contradicts PM
        ("btc_ext", lambda c: (c["side"] == "yes" and c["btc_position"] < -0.1) or (c["side"] == "no" and c["btc_position"] > 0.1), 1),
        # PM overconfidence: BTC barely moved
        ("re<0.5", lambda c: c["range_expansion"] is not None and c["range_expansion"] < 0.5, 2),
        # Entry price: cheaper entry = higher reward
        ("entry<0.20", lambda c: c["entry_price"] < 0.20, 1),
        ("entry<0.25", lambda c: c["entry_price"] < 0.25, 1),
    ],
    "entry_score": 5,
    "od_hard_filter": 0.005,  # minimal: only filter negative/flat other_delta
}

scores = []
for c in all_candidates:
    if "od_hard_filter" in best_formula and c["other_delta"] < best_formula["od_hard_filter"]:
        continue
    if c["range_expansion"] is not None and c["range_expansion"] >= 2.0:
        continue

    score = 0
    detail = {}
    for name, pred, weight in best_formula["features"]:
        if pred(c):
            score += weight
            detail[name] = weight
    c_with_score = dict(c)
    c_with_score["score"] = score
    c_with_score["detail"] = detail
    scores.append(c_with_score)

passed = [c for c in scores if c["score"] >= best_formula["entry_score"]]
n = len(passed)
wins = sum(1 for c in passed if c["won"])
total_pnl = sum(c["pnl"] for c in passed)
avg_score = sum(c["score"] for c in passed) / n if n else 0

print(f"\n  信号数: {n}, 胜率: {wins/n*100:.1f}%, P&L: {total_pnl:+.2f}" if n else "\n  无信号")
print(f"  平均分数: {avg_score:.1f}")

if passed:
    # Score distribution
    for s in range(5, 13):
        sub = [c for c in passed if c["score"] == s]
        if sub:
            w = sum(1 for c in sub if c["won"])
            p = sum(c["pnl"] for c in sub)
            print(f"  Score={s}: n={len(sub):>3d}, win={w/len(sub)*100:>5.1f}%, P&L={p:>+7.2f}")

    # Feature contribution analysis
    print(f"\n  各项特征触发频率:")
    for name, pred, weight in best_formula["features"]:
        triggered = sum(1 for c in passed if pred(c))
        print(f"    {name:12s} (w={weight}): {triggered}/{n} ({triggered/n*100:.0f}%)")

    # Show sample signals with detail
    print(f"\n  逐笔信号:")
    print(f"  {'time':>12s} {'side':>4s} {'score':>5s} {'od':>7s} {'pnl':>+7s}  detail")
    print(f"  {'-'*70}")
    for c in sorted(passed, key=lambda x: x["score"], reverse=True):
        detail_str = " + ".join(f"{k}({v})" for k, v in sorted(c["detail"].items()))
        print(f"  {c['time']:>12d} {c['side']:>4s} {c['score']:>5d} {c['other_delta']:>+7.4f} {c['pnl']:>+7.2f}  {detail_str}")
