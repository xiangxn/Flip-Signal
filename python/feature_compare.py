#!/usr/bin/env python3
"""
特征组合对比 v2 — 使用 AND 逻辑组合特征。

每个特征独立使用自己的最佳阈值，全部通过（AND）才入场。
对比各种特征组合的叠加效果。
"""

import argparse, sys, json
from dataclasses import dataclass, field
from pathlib import Path
from itertools import combinations
import numpy as np
from scipy import stats

CAPITAL, SPREAD, LOOKBACK, WARMUP_SEC, TOLERANCE = 5.0, 0.10, 10, 60, 10
TRAIN_SPLIT = 0.7
ENTRY_SEC = 220


def load_events(data_dir: str) -> list[dict]:
    events = []
    for f in sorted(Path(data_dir).glob("events_*.jsonl")):
        for line in open(f):
            events.append(json.loads(line))
    return events


def compute_hist_vols(events, lookback=LOOKBACK):
    vols = []
    for i in range(len(events)):
        if i < lookback:
            vols.append(np.nan); continue
        past = [abs((events[j]["close_price"] - events[j]["open_price"]) / events[j]["open_price"])
                for j in range(i - lookback, i)]
        vols.append(np.median(past))
    return vols


def compute_hist_yes_ranges(events, lookback=LOOKBACK):
    ranges = []
    for i in range(len(events)):
        if i < lookback:
            ranges.append(np.nan); continue
        past = [max(s["yes_price"] for s in events[j]["snapshots"]) -
                min(s["yes_price"] for s in events[j]["snapshots"])
                for j in range(i - lookback, i)]
        ranges.append(np.median(past))
    return ranges


def find_snapshot(snapshots, target_sec, tolerance=TOLERANCE):
    best, best_dist = None, float("inf")
    for s in snapshots:
        dist = abs(s["remaining_sec"] - target_sec)
        if dist < best_dist:
            best_dist = dist; best = s
    return best if (best is not None and best_dist <= tolerance) else None


def extract_features(snapshots, entry_snap, hist_vol, hist_yes_range, open_price):
    feats = {}
    remaining = entry_snap["remaining_sec"]
    early = [s for s in snapshots if s["remaining_sec"] >= remaining]
    yes_p = [s["yes_price"] for s in early]
    no_p = [s["no_price"] for s in early]
    n = len(early)

    # F1: 当前波动率 / 历史波动率
    partial_ret = abs((entry_snap["price"] - open_price) / open_price)
    feats["partial_ratio"] = partial_ret / hist_vol if hist_vol > 0 else 1.0

    # F2: PM 盘口波动 (yes/no std 之和)
    feats["pm_vol"] = float(np.std(yes_p) + np.std(no_p)) if n >= 5 else 0.0

    # F3: 早期价格范围比
    if n >= 5 and hist_yes_range > 0:
        feats["early_range_ratio"] = ((max(yes_p)-min(yes_p)) + (max(no_p)-min(no_p))) / 2 / hist_yes_range
    else:
        feats["early_range_ratio"] = 1.0

    # F4: 买卖比
    buy = sum(s.get("buy_vol_5s", 0) for s in early)
    sell = sum(s.get("sell_vol_5s", 0) for s in early)
    feats["buy_ratio"] = buy / (buy + sell) if (buy + sell) > 0 else 0.5

    # F5: 盘口深度平衡
    bids = [s.get("bid_depth", 0) for s in early]
    asks = [s.get("ask_depth", 0) for s in early]
    avg_b, avg_a = np.mean(bids) if bids else 0, np.mean(asks) if asks else 0
    feats["depth_balance"] = avg_b / (avg_b + avg_a) if (avg_b + avg_a) > 0 else 0.5

    # F6: PM 价格锚定（接近 0.5 的程度）
    if n >= 5:
        extreme = sum(1 for p in yes_p if p < 0.35 or p > 0.65)
        feats["pm_anchor"] = 1.0 - extreme / n
    else:
        feats["pm_anchor"] = 0.5

    # F7: 签名流一致性 (|mean|/std)
    if n >= 5:
        flows = [s.get("signed_flow_5s", 0) for s in early]
        fstd = np.std(flows)
        feats["flow_consistency"] = abs(np.mean(flows)) / fstd if fstd > 0 else 0.0
    else:
        feats["flow_consistency"] = 0.0

    # F8: 综合 — yes/no 价格稳定性（早期价格变化率，越低越好）
    if n >= 5:
        yes_change = abs(yes_p[-1] - yes_p[0])
        no_change = abs(no_p[-1] - no_p[0])
        feats["price_stability"] = 1.0 - min((yes_change + no_change), 1.0)
    else:
        feats["price_stability"] = 0.5

    return feats


def simulate_trade(snapshots, entry_snap, outcome, spread=SPREAD, capital=CAPITAL):
    yes_e, no_e = entry_snap["yes_price"], entry_snap["no_price"]
    rem = entry_snap["remaining_sec"]
    yes_t = min(yes_e + spread, 0.99)
    no_t = min(no_e + spread, 0.99)
    y_f, n_f = False, False
    y_fp, n_fp = 0.0, 0.0
    for s in snapshots:
        if s["remaining_sec"] >= rem: continue
        if not y_f and s["yes_price"] >= yes_t: y_f, y_fp = True, s["yes_price"]
        if not n_f and s["no_price"] >= no_t: n_f, n_fp = True, s["no_price"]
        if y_f and n_f: break
    qty = int(capital / 0.5) // 2
    rev = qty * (y_fp if y_f else (1.0 if outcome == 0 else 0.0))
    rev += qty * (n_fp if n_f else (1.0 if outcome == 1 else 0.0))
    return {"pnl": rev - capital, "both": y_f and n_f,
            "one": (y_f or n_f) and not (y_f and n_f), "none": not y_f and not n_f}


# ── 特征元信息 ──
FEATURE_META = [
    ("partial_ratio",      "F1.当前/历史波动率",   "lt"),
    ("pm_vol",             "F2.PM盘口波动(std)",   "lt"),
    ("early_range_ratio",  "F3.早期价格范围比",    "lt"),
    ("buy_ratio",          "F4.买卖比",            "gt"),
    ("depth_balance",      "F5.盘口深度平衡",      "gt"),
    ("pm_anchor",          "F6.PM价格锚定(近0.5)",  "gt"),
    ("flow_consistency",   "F7.签名流一致性",      "lt"),
    ("price_stability",    "F8.价格稳定性",        "gt"),
]


@dataclass
class ComboResult:
    name: str
    features: list[str]
    train_thresholds: dict
    test_trades: int = 0
    test_total_pnl: float = 0.0
    test_mean_pnl: float = 0.0
    test_both_pct: float = 0.0
    test_win_rate: float = 0.0
    pred_precision: float = 0.0
    pred_recall: float = 0.0
    pred_f1: float = 0.0
    corr: float = 0.0


def find_best_threshold(values, targets, direction, min_trades=5):
    """在训练数据上搜索最佳单个特征阈值。"""
    best_thresh, best_pnl = None, -999
    search_range = np.arange(0.02, 0.98, 0.02) if direction == "gt" else np.arange(0.02, 0.98, 0.02)

    for t in search_range:
        if direction == "lt":
            mask = values < t
        else:
            mask = values > t
        if mask.sum() < min_trades:
            continue
        total = sum(targets[mask])
        if total > best_pnl:
            best_pnl = total
            best_thresh = t
    return best_thresh


def evaluate_combo(feature_names, all_data, train_idx, test_idx, mode="and", min_votes=None):
    """
    特征组合评估。

    mode="and": 所有特征必须同时通过（AND 逻辑）
    mode="vote": N 个特征中至少 min_votes 个通过（投票制），在训练集上搜索最佳 min_votes
    """
    feat_meta = {n: (n, l, d) for n, l, d in FEATURE_META}

    train_data = [all_data[i] for i in train_idx]
    test_data = [all_data[i] for i in test_idx]
    combo_name = " + ".join(feature_names)

    # ── 对每个特征，在训练集上独立找到最佳阈值 ──
    thresholds = {}
    for fn in feature_names:
        _, _, direction = feat_meta[fn]
        vals = np.array([d["features"][fn] for d in train_data])
        targets = np.array([d["pnl"] for d in train_data])
        thresh = find_best_threshold(vals, targets, direction, min_trades=3)
        if thresh is None:
            thresh = np.median(vals)
        thresholds[fn] = thresh

    # ── 对每个数据点，计算每个特征是否通过 ──
    def compute_pass_matrix(data):
        n = len(data)
        m = len(feature_names)
        passed = np.zeros((n, m), dtype=bool)
        for j, fn in enumerate(feature_names):
            _, _, direction = feat_meta[fn]
            vals = np.array([d["features"][fn] for d in data])
            t = thresholds[fn]
            if direction == "lt":
                passed[:, j] = vals < t
            else:
                passed[:, j] = vals > t
        return passed

    train_pass = compute_pass_matrix(train_data)
    test_pass = compute_pass_matrix(test_data)

    if mode == "and":
        min_votes_list = [len(feature_names)]
        combo_name += " [AND]"
    else:
        # 搜索最佳 min_votes
        min_votes_list = list(range(1, len(feature_names) + 1))
        combo_name += " [VOTE]"

    best_min_votes = len(feature_names)
    best_train_pnl = -999

    for mv in min_votes_list:
        train_passing = train_pass.sum(axis=1) >= mv
        train_pnls = [train_data[i]["pnl"] for i in range(len(train_data)) if train_passing[i]]
        total = sum(train_pnls)
        if total > best_train_pnl and len(train_pnls) >= 3:
            best_train_pnl = total
            best_min_votes = mv

    # ── 测试集上用最佳 min_votes ──
    test_passing = test_pass.sum(axis=1) >= best_min_votes
    test_trades = [test_data[i] for i in range(len(test_data)) if test_passing[i]]
    test_pnls = [d["pnl"] for d in test_trades]

    # ── 预测 low_vol 评估 ──
    all_pass = compute_pass_matrix(all_data)
    all_passing = all_pass.sum(axis=1) >= best_min_votes
    actual_low = np.array([d["is_low_vol"] for d in all_data])
    tp = np.sum(all_passing & actual_low)
    fp = np.sum(all_passing & ~actual_low)
    fn = np.sum(~all_passing & actual_low)
    prec = tp / max(tp + fp, 1)
    rec = tp / max(tp + fn, 1)
    f1 = 2 * prec * rec / max(prec + rec, 0.001)

    # 相关性
    combo_scores = np.zeros(len(all_data))
    for fn in feature_names:
        _, _, direction = feat_meta[fn]
        raw = np.array([d["features"][fn] for d in all_data])
        if raw.std() > 0:
            z = (raw - raw.mean()) / raw.std()
            if direction == "lt": z = -z
            combo_scores += z
    if len(feature_names) > 0:
        combo_scores /= len(feature_names)
        corr, _ = stats.pearsonr(combo_scores, np.array([d["full_vol_ratio"] for d in all_data]))
    else:
        corr = 0

    return ComboResult(
        name=f"{combo_name} {best_min_votes}/{len(feature_names)}",
        features=list(feature_names),
        train_thresholds=thresholds,
        test_trades=len(test_trades),
        test_total_pnl=sum(test_pnls) if test_pnls else 0.0,
        test_mean_pnl=np.mean(test_pnls) if test_pnls else 0.0,
        test_both_pct=float(np.mean([d["both"] for d in test_trades])) if test_trades else 0.0,
        test_win_rate=float(np.mean([p > 0 for p in test_pnls])) if test_pnls else 0.0,
        pred_precision=prec, pred_recall=rec, pred_f1=f1, corr=float(corr),
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="../data/")
    parser.add_argument("--entry", type=int, default=ENTRY_SEC)
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    data_dir = (script_dir / args.data).resolve()

    print("加载数据...")
    events = load_events(str(data_dir))
    print(f"  Event 数: {len(events)}")

    hist_vols = compute_hist_vols(events)
    hist_yes_ranges = compute_hist_yes_ranges(events)

    # ── 收集所有数据点 ──
    all_data = []
    for i, e in enumerate(events):
        hvol = hist_vols[i]; hyr = hist_yes_ranges[i]
        if np.isnan(hvol) or np.isnan(hyr): continue
        snap = find_snapshot(e["snapshots"], args.entry)
        if snap is None: continue
        if snap["remaining_sec"] > 300 - WARMUP_SEC: continue

        feats = extract_features(e["snapshots"], snap, hvol, hyr, e["open_price"])
        trade = simulate_trade(e["snapshots"], snap, e["outcome"])
        full_abs = abs((e["close_price"] - e["open_price"]) / e["open_price"])
        all_data.append({
            "features": feats,
            "pnl": trade["pnl"], "both": trade["both"],
            "full_vol_ratio": full_abs / hvol if hvol > 0 else 1.0,
            "is_low_vol": (full_abs / hvol if hvol > 0 else 1.0) < 0.5,
        })

    # 时间序列切分
    split = int(len(all_data) * TRAIN_SPLIT)
    train_idx = list(range(split))
    test_idx = list(range(split, len(all_data)))
    print(f"  训练集: {len(train_idx)} events, 测试集: {len(test_idx)} events")
    print(f"  入场时间: {args.entry}s, 热身期: {WARMUP_SEC}s")

    # ── Baseline ──
    test_pnls_all = [all_data[i]["pnl"] for i in test_idx]
    nofil = ComboResult("NoFilter", [], {},
                        test_trades=len(test_idx),
                        test_total_pnl=sum(test_pnls_all),
                        test_mean_pnl=np.mean(test_pnls_all) if test_pnls_all else 0,
                        test_both_pct=float(np.mean([all_data[i]["both"] for i in test_idx])))

    # ── Oracle ──
    oracle_data = [all_data[i] for i in test_idx if all_data[i]["is_low_vol"]]
    oracle_pnls = [d["pnl"] for d in oracle_data]
    oracle = ComboResult("Oracle", [], {},
                         test_trades=len(oracle_data),
                         test_total_pnl=sum(oracle_pnls) if oracle_pnls else 0,
                         test_mean_pnl=np.mean(oracle_pnls) if oracle_pnls else 0,
                         test_both_pct=float(np.mean([d["both"] for d in oracle_data])) if oracle_data else 0)

    print(f"\n  NoFilter: {nofil.test_trades} trades, both={nofil.test_both_pct:.1%}, "
          f"mean=${nofil.test_mean_pnl:+.3f}, total=${nofil.test_total_pnl:+.2f}")
    print(f"  Oracle:   {oracle.test_trades} trades, both={oracle.test_both_pct:.1%}, "
          f"mean=${oracle.test_mean_pnl:+.3f}, total=${oracle.test_total_pnl:+.2f}")

    # ── 单特征评估 ──
    print(f"\n{'='*100}")
    print("单特征评估")
    print(f"{'='*100}")
    singles = {}
    for fn, flabel, _ in FEATURE_META:
        res = evaluate_combo([fn], all_data, train_idx, test_idx, mode="and")
        singles[fn] = res
        print(f"  {flabel:<35} trades={res.test_trades:>3}, both={res.test_both_pct:.1%}, "
              f"win={res.test_win_rate:.1%}, mean=${res.test_mean_pnl:+.3f}, "
              f"total=${res.test_total_pnl:+.2f}, F1={res.pred_f1:.2f}")

    # ── 排名 ──
    ranked = sorted(singles.items(), key=lambda x: -x[1].test_total_pnl)
    print(f"\n  单特征排名: " + " > ".join(f"{fn}(${singles[fn].test_total_pnl:+.2f})" for fn, _ in ranked))

    # ── 累加组合：AND vs VOTE ──
    print(f"\n{'='*100}")
    print("累加组合对比：AND（全部通过） vs VOTE（多数通过）")
    print(f"{'='*100}")

    base_fn = ranked[0][0]
    current = [base_fn]

    for mode in ["and", "vote"]:
        print(f"\n  --- {mode.upper()} 模式 ---")
        current = [base_fn]
        prev_total = singles[base_fn].test_total_pnl
        base_label = FEATURE_META[[m[0] for m in FEATURE_META].index(base_fn)][1]
        print(f"  {base_label:<35} trades={singles[base_fn].test_trades:>3}, "
              f"total=${singles[base_fn].test_total_pnl:+.2f}")

        for fn, _ in ranked[1:]:
            current.append(fn)
            res = evaluate_combo(current, all_data, train_idx, test_idx, mode=mode)
            delta = res.test_total_pnl - prev_total
            feat_label = FEATURE_META[[m[0] for m in FEATURE_META].index(fn)][1]
            print(f"  +{feat_label:<33} trades={res.test_trades:>3}, "
                  f"both={res.test_both_pct:.1%}, total=${res.test_total_pnl:+.2f} "
                  f"(Δ{delta:+.2f}), F1={res.pred_f1:.2f}")
            prev_total = res.test_total_pnl

    # ── 最终汇总 ──
    # 重新计算所有组合用 VOTE 模式
    print(f"\n{'='*100}")
    print("最佳 VOTE 组合 vs 单特征 vs Oracle")
    print(f"{'='*100}")

    # 用 VOTE 模式测试 top 特征的累加
    vote_results = []
    current_vote = [base_fn]
    for fn, _ in ranked[1:]:
        current_vote.append(fn)
        res = evaluate_combo(current_vote, all_data, train_idx, test_idx, mode="vote")
        vote_results.append(res)

    # 测试 ALL features
    all_vote = evaluate_combo([fn for fn, _, _ in FEATURE_META], all_data, train_idx, test_idx, mode="vote")
    vote_results.append(all_vote)

    print(f"{'策略':<55} {'笔数':>4} {'两侧成交':>8} {'胜率':>6} {'总P&L':>9} {'占Oracle':>8}")
    print("-" * 95)
    print(f"{'NoFilter':<55} {nofil.test_trades:>4} {nofil.test_both_pct:>7.1%} "
          f"{nofil.test_win_rate:>5.1%} ${nofil.test_total_pnl:>+8.2f} {'—':>8}")
    print(f"{'Oracle (full_vol<0.5)':<55} {oracle.test_trades:>4} {oracle.test_both_pct:>7.1%} "
          f"{oracle.test_win_rate:>5.1%} ${oracle.test_total_pnl:>+8.2f} {'100%':>8}")
    print("-" * 95)

    # 最佳单特征
    for fn, _ in ranked[:3]:
        s = singles[fn]
        label = FEATURE_META[[m[0] for m in FEATURE_META].index(fn)][1]
        pct = s.test_total_pnl / oracle.test_total_pnl * 100 if oracle.test_total_pnl else 0
        print(f"  {label:<53} {s.test_trades:>4} {s.test_both_pct:>7.1%} "
              f"{s.test_win_rate:>5.1%} ${s.test_total_pnl:>+8.2f} {pct:>7.0f}%")

    print("-" * 95)
    for res in vote_results:
        pct = res.test_total_pnl / oracle.test_total_pnl * 100 if oracle.test_total_pnl else 0
        pnl_str = f"${res.test_total_pnl:+.2f}" if res.test_total_pnl else "$0.00"
        print(f"  {res.name:<53} {res.test_trades:>4} {res.test_both_pct:>7.1%} "
              f"{res.test_win_rate:>5.1%} {pnl_str:>9} {pct:>7.0f}%")
    print()


if __name__ == "__main__":
    main()
