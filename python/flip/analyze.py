#!/usr/bin/env python3
"""
翻转（Flip）线特征筛选 —— 决策时刻预判 both 类别（独立模块，只服务翻转）。

观测单位: 事件内时间顺序首个 0.7 穿越（确认 T+2 ticks 的成交特征）。
标签:    both = 对侧后来也穿越过 0.7（flip 的领域, 首个穿越侧只赢 ~20%）
         only = 整窗内仅穿越侧出现过 >0.7（flip 必亏, follow 的领域）
         ⚠️ 标签是整窗未来信息，仅作训练目标，绝不进特征。
特征:    全部 ≤ 决策时刻已知（穿越 tick 或确认 tick）。
输出:    每特征分桶 × both 率 × flip EV × χ²。

用法:
    python analyze.py --data ../data_0/lab_resolved --outcome-field market_outcome
    python analyze.py --data ../data/btc --outcome-field outcome
"""

import argparse
import json
import sys
from pathlib import Path

from scipy.stats import chi2_contingency


def load_events(data_dir: str) -> list[dict]:
    events = []
    corrections = {}
    for path in sorted(Path(data_dir).glob("events_*.jsonl")):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get("event_type") == "settlement_correction":
                    corrections[rec["start_time"]] = rec
                else:
                    events.append(rec)
    # 结算修正行按 start_time 覆盖合并（官方 open/close/outcome 延迟到达后追加，
    # 见 internal/collect/settle.go；文件内行序与事件序无关，按 start_time 对齐）
    if corrections:
        for e in events:
            corr = corrections.get(e["start_time"])
            if corr:
                e.update({k: v for k, v in corr.items() if k != "event_type"})
    events.sort(key=lambda e: e["start_time"])
    for i, e in enumerate(events):
        prev = []
        for j in range(max(0, i - 18), i):
            o = events[j].get("twap_open_price") or events[j].get("open_price")
            c = events[j].get("twap_close_price") or events[j].get("close_price")
            if o and c:
                prev.append(abs(c - o))
        e["hist_avg_range"] = sum(prev) / len(prev) if len(prev) >= 3 else None
    return events


def extract_obs(events: list[dict], outcome_field: str) -> list[dict]:
    """每个事件首个穿越 → 观测 dict（特征 ≤ 决策时刻，标签为整窗类别）。"""
    obs = []
    for event in events:
        if event.get("hist_avg_range") is None:
            continue
        outcome = event.get(outcome_field)
        if outcome is None:
            continue
        hist = event["hist_avg_range"]
        open_price = event.get("open_price")
        snaps = event["snapshots"]
        state = {"yes": False, "no": False}
        crossed = set()
        first = None

        for i, s in enumerate(snaps):
            if i < 5:
                continue
            if not (s["remaining_sec"] < 260 and s["remaining_sec"] > 15):
                continue
            for side in ("yes", "no"):
                key = "yes_price" if side == "yes" else "no_price"
                above = s[key] > 0.7
                if above and not state[side]:
                    crossed.add(side)
                    if first is None:
                        first = (i, side)
                state[side] = above

        if first is None:
            continue
        i, side = first
        sgn = 1 if side == "yes" else -1   # 正 = 现货/流动朝穿越侧方向
        s = snaps[i]
        conf = snaps[i + 2] if i + 2 < len(snaps) else None
        this_key = "yes_price" if side == "yes" else "no_price"
        other_key = "no_price" if side == "yes" else "yes_price"

        spot = s.get("price") or 0
        prices = [x.get("price") or 0 for x in snaps[:i + 1]]
        net = abs(spot - open_price) if open_price else 0
        amp = max(prices) - min(prices) if prices else 0
        path = sum(abs(prices[k] - prices[k - 1]) for k in range(1, len(prices)))
        flips = sum(1 for k in range(2, len(prices))
                    if (prices[k] - prices[k - 1]) * (prices[k - 1] - prices[k - 2]) < 0)

        o = {
            "event_start": event["start_time"],
            "side": side,
            "cls": "both" if len(crossed) == 2 else "only",
            "rem": s["remaining_sec"],
            "elapsed": 300 - s["remaining_sec"],
            "trigger_bid": s[this_key],
            "flip_fill": (1.0 - conf[this_key]) if conf else None,
            "follow_fill": (1.0 - conf[other_key]) if conf else None,
            "other_delta": (conf[other_key] - s[other_key]) if conf else None,
            "spot_ret_conf": (sgn * (conf["price"] - spot) / spot)
                             if conf and conf.get("price") and spot else None,
            "ret_10s": sgn * (s.get("ret_10s") or 0),
            "flow_5s": sgn * (s.get("signed_flow_5s") or 0),
            "vol_10s": s.get("vol_10s"),
            "spot_pos": (sgn * (spot - open_price) / hist) if open_price else None,
            "range_spot": (abs(spot - open_price) / hist) if open_price else None,
            "path_eff": net / amp if amp > 1e-9 else None,
            "noise": path / net if net > 1e-9 else None,
            "flips": flips,
            "twap_pos": None, "twap_gap": None, "twap_delta": None,
        }
        twap = s.get("twap_price")
        twap_open = event.get("twap_open_price")
        if twap and twap_open and hist:
            o["twap_pos"] = sgn * (twap - twap_open) / hist
            if spot:
                o["twap_gap"] = sgn * (spot - twap) / hist
        if twap and conf and conf.get("twap_price") and hist:
            o["twap_delta"] = sgn * (conf["twap_price"] - twap) / hist

        won = (outcome == 1) if side == "yes" else (outcome == 0)
        o["won"] = won
        obs.append(o)
    return obs


FEATURES = [
    ("rem", "剩余秒数", [("16-60s", 16, 60), ("61-120s", 60, 120),
                         ("121-180s", 120, 180), ("181-240s", 180, 240), ("241-260s", 240, 260)]),
    ("elapsed", "窗口已过秒数", [("<60s", -1e9, 60), ("60-120s", 60, 120), (">120s", 120, None)]),
    ("trigger_bid", "触发深度", [("0.70-0.75", 0.70, 0.75), ("0.75-0.80", 0.75, 0.80), ("≥0.80", 0.80, None)]),
    ("flip_fill", "flip 成交价", [("<0.20", -1e9, 0.20), ("0.20-0.25", 0.20, 0.25),
                                  ("0.25-0.30", 0.25, 0.30), (">0.30", 0.30, None)]),
    ("follow_fill", "follow 成交价(对照)", [("<0.75", -1e9, 0.75), ("0.75-0.80", 0.75, 0.80),
                                            ("0.80-0.85", 0.80, 0.85), (">0.85", 0.85, None)]),
    ("other_delta", "确认期对侧变化", [("≤0", -1e9, 0.0), ("0-0.01", 0.0, 0.01),
                                       ("0.01-0.02", 0.01, 0.02), ("0.02-0.05", 0.02, 0.05), (">0.05", 0.05, None)]),
    ("spot_ret_conf", "确认期现货方向", [("<-3bp 反向", -1e9, -3e-4), ("±3bp", -3e-4, 3e-4), (">3bp 续动", 3e-4, None)]),
    ("ret_10s", "穿越前10s现货", [("<-1e-4 反向", -1e9, -1e-4), ("±1e-4", -1e-4, 1e-4), (">1e-4 续动", 1e-4, None)]),
    ("flow_5s", "5s净流", [("≤-1 反向", -1e9, -1.0), ("±1", -1.0, 1.0), (">1 续动", 1.0, None)]),
    ("spot_pos", "现货位置(hist)", [("≤-3", -1e9, -3.0), ("-3~0", -3.0, 0.0), ("0~3", 0.0, 3.0), (">3", 3.0, None)]),
    ("range_spot", "振幅扩张(spot)", [("<1.5", -1e9, 1.5), ("1.5-3", 1.5, 3.0), ("3-5", 3.0, 5.0), ("≥5", 5.0, None)]),
    ("path_eff", "路径效率", [("≤0.4 双边", -1e9, 0.4), ("0.4-0.6", 0.4, 0.6), ("0.6-0.8", 0.6, 0.8), (">0.8 单边", 0.8, None)]),
    ("noise", "噪声比", [("<2", -1e9, 2.0), ("2-4", 2.0, 4.0), ("4-8", 4.0, 8.0), ("≥8", 8.0, None)]),
    ("flips", "方向翻转次数", [("<3", -1e9, 3.0), ("3-6", 3.0, 6.0), ("≥6", 6.0, None)]),
    ("vol_10s", "波动率10s", None),
    ("twap_pos", "TWAP位置(hist)", [("≤0", -1e9, 0.0), ("0-0.25", 0.0, 0.25), (">0.25", 0.25, None)]),
    ("twap_gap", "spot-TWAP缺口", [("≤-0.5", -1e9, -0.5), ("±0.5", -0.5, 0.5), (">0.5", 0.5, None)]),
    ("twap_delta", "确认期TWAP变化", [("≤-0.05", -1e9, -0.05), ("±0.05", -0.05, 0.05), (">0.05", 0.05, None)]),
]


def screen(obs: list[dict], key: str, buckets, base_both: float, base_fev: float) -> None:
    vals = [o[key] for o in obs if o.get(key) is not None]
    if len(vals) < 30:
        print(f"\n  【{key}】 有效样本不足 ({len(vals)})")
        return
    if buckets is None:
        srt = sorted(vals)
        q1, q2 = srt[len(srt) // 3], srt[2 * len(srt) // 3]
        buckets = [("低T1", -1e9, q1), ("中T2", q1, q2), ("高T3", q2, None)]

    print(f"\n  【{key}】 (基线 both 率 {base_both * 100:.1f}%, flip EV {base_fev:+.4f})")
    print(f"  {'分桶':<16s} {'n':>5s} {'both率':>8s} {'Δ':>7s} {'flipEV':>10s}")
    table = []
    for label, lo, hi in buckets:
        b = [o for o in obs if (v := o.get(key)) is not None and lo <= v and (hi is None or v < hi)]
        if not b:
            continue
        both = sum(1 for o in b if o["cls"] == "both") / len(b)
        fev = sum(((1.0 - o["flip_fill"]) if o["won"] else -o["flip_fill"])
                  for o in b if o["flip_fill"] is not None) / max(1, len([o for o in b if o["flip_fill"] is not None]))
        n_f = len([o for o in b if o["flip_fill"] is not None])
        print(f"  {label:<16s} {len(b):>5d} {both * 100:>7.1f}% {(both - base_both) * 100:>+6.1f}pp "
              f"{fev:>+9.4f} (n={n_f})")
        table.append([sum(1 for o in b if o["cls"] == "both"),
                      sum(1 for o in b if o["cls"] == "only")])
    if len(table) >= 2 and all(sum(r) > 0 for r in table):
        chi2, p, _, _ = chi2_contingency(table, correction=False)
        print(f"  χ²={chi2:.1f}  p={p:.3f}  {'★' if p < 0.05 else ('△' if p < 0.1 else '·')}")


def main():
    parser = argparse.ArgumentParser(description="翻转（Flip）线特征筛选（独立）")
    parser.add_argument("--data", default="../data_0/lab_resolved/", help="JSONL 数据目录")
    parser.add_argument("--outcome-field", default="market_outcome",
                        help="结算字段: market_outcome(data_0) / outcome(data/btc)")
    args = parser.parse_args()

    events = load_events(args.data)
    obs = extract_obs(events, args.outcome_field)
    if not obs:
        print("无观测。")
        sys.exit(1)
    both_n = sum(1 for o in obs if o["cls"] == "both")
    print(f"数据: {args.data}  |  事件 {len(events)}  |  首个穿越观测 {len(obs)}  |  "
          f"both {both_n} ({both_n / len(obs) * 100:.1f}%) / only {len(obs) - both_n}")
    base_both = both_n / len(obs)
    base_fev = sum(((1.0 - o["flip_fill"]) if o["won"] else -o["flip_fill"])
                   for o in obs if o["flip_fill"] is not None)
    n_f = len([o for o in obs if o["flip_fill"] is not None])
    base_fev /= max(1, n_f)

    print("=" * 62)
    print("  翻转线: 预判 both 类（flip 赢 ~80% 的窗口）的特征筛选")
    print("=" * 62)
    for key, label, buckets in FEATURES:
        screen(obs, key, buckets, base_both, base_fev)


if __name__ == "__main__":
    main()
