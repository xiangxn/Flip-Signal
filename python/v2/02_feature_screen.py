#!/usr/bin/env python3
"""
02 因子重挖（v2 数据, 计划 §7.2）—— 决策时刻预判 both/only 类别。

观测单位: 事件内首个 0.7 穿越（1s 分辨率）。
决策时刻: 穿越 +10s（确认窗口, 特征全部 ≤ 该时刻）。
标签:    both = 整窗对侧也穿越过 0.7（flip 领域, 首个穿越侧只赢 22%）
         only = 整窗仅穿越侧 >0.7（follow 领域）—— 整窗未来信息, 仅作训练目标。

输出:    每特征分桶 × both 率 × flip EV(+10s) × follow EV(+10s) × χ²。

特征族（按计划优先级）:
  P0-3 PM tape:      tape_imb/tape_share/tape_max/tape_other_imb（穿越后 10s 成交流向）
  P0-1 Binance OFI:  flow_pre/vol_pre/n_ticks_pre/of_pre（穿越前 10s 主动流）
  P0-2 深度/reprice: cross_bid_dep/cross_depth/book_age/reprice_n/reprice_gap
  微观轨迹:          post_dip（穿越后穿越侧 bid 回撤幅度）
  v1 对照:           other_delta10s/spot_ret/path_eff/noise/flips/twap_*
"""

import sys
from pathlib import Path

from scipy.stats import chi2_contingency

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import load_events, extract_cross, cross_window

DATA = "../data/btc"
CONFIRM_S = 10   # 决策时刻 = 穿越 +10s


def hist_ranges(events):
    """每事件历史振幅基准: 前 ≤18 个窗口 |twap_close-twap_open| 均值（≥3 才可用）。"""
    hr = {}
    for i, e in enumerate(events):
        prev = []
        for j in range(max(0, i - 18), i):
            o, c = events[j].get("twap_open_price"), events[j].get("twap_close_price")
            if o and c:
                prev.append(abs(c - o))
        hr[e["start_time"]] = sum(prev) / len(prev) if len(prev) >= 3 else None
    return hr


def screen(rows, key, label, buckets, base_both, base_fev, base_fev_n):
    vals = [r[key] for r in rows if r.get(key) is not None]
    if len(vals) < 30:
        print(f"\n  【{key}: {label}】 有效样本不足 ({len(vals)})")
        return
    if buckets is None:
        srt = sorted(vals)
        q1, q2 = srt[len(srt) // 3], srt[2 * len(srt) // 3]
        if q1 == q2:
            print(f"\n  【{key}: {label}】 分位数重合, 跳过")
            return
        buckets = [("低 T1", -1e9, q1), ("中 T2", q1, q2), ("高 T3", q2, None)]

    print(f"\n  【{key}: {label}】 (基线 both 率 {base_both * 100:.1f}%, flip EV {base_fev:+.4f}/n={base_fev_n})")
    print(f"  {'分桶':<14s} {'n':>5s} {'both率':>8s} {'Δ':>7s} {'flipEV':>9s} {'followEV':>10s}")
    table = []
    for bname, lo, hi in buckets:
        b = [r for r in rows if (v := r.get(key)) is not None and lo <= v and (hi is None or v < hi)]
        if not b:
            continue
        both = sum(1 for r in b if r["cls"] == "both") / len(b)
        fev = _ev(b, f"flip_fill{CONFIRM_S}s", flip=True)
        fo = _ev(b, f"fill{CONFIRM_S}s", flip=False)
        print(f"  {bname:<14s} {len(b):>5d} {both * 100:>7.1f}% {(both - base_both) * 100:>+6.1f}pp "
              f"{fev:>+9.4f} {fo:>+10.4f}")
        table.append([sum(1 for r in b if r["cls"] == "both"),
                      sum(1 for r in b if r["cls"] == "only")])
    if len(table) >= 2 and all(sum(r) > 0 for r in table):
        chi2, p, _, _ = chi2_contingency(table, correction=False)
        print(f"  χ²={chi2:.1f}  p={p:.3f}  {'★' if p < 0.05 else ('△' if p < 0.1 else '·')}")


def _ev(rows, fill_key, flip: bool) -> float:
    """分桶 EV: follow = 买穿越侧(won 时赚 1-fill, !won 亏 fill);
    flip = 买对侧(won 时亏 fill, !won 赚 1-fill)。"""
    xs = [r for r in rows if r.get(fill_key) is not None]
    if not xs:
        return float("nan")
    if not flip:
        return sum((1.0 - r[fill_key]) if r["won"] else -r[fill_key] for r in xs) / len(xs)
    return sum(-r[fill_key] if r["won"] else (1.0 - r[fill_key]) for r in xs) / len(xs)


def main():
    events = load_events(DATA)
    by_start = {e["start_time"]: e for e in events}
    hr = hist_ranges(events)
    obs = extract_cross(events, "outcome")
    rows = []
    for o in obs:
        ev = by_start[o["event_start"]]
        f = cross_window(
            ev, o["i"], o["side"], o["other"],
            open_price=ev.get("twap_open_price"),
            hist=hr[o["event_start"]],
        )
        f.update({k: v for k, v in o.items() if k in ("cls", "won")})
        for ds in (CONFIRM_S,):
            f[f"fill{ds}s"] = o[f"fill{ds}s"]
            f[f"flip_fill{ds}s"] = o[f"flip_fill{ds}s"]
            f[f"other_delta{ds}s"] = o[f"other_delta{ds}s"]
        rows.append(f)

    # trades 覆盖（tape 特征可用性）
    tb_rows = [r for r in rows if r.get("tape_imb") is not None]
    print(f"数据: {DATA}  |  穿越观测 {len(rows)}  |  tape 可用 {len(tb_rows)} "
          f"({len(tb_rows) / len(rows) * 100:.0f}%)  |  决策时刻 +{CONFIRM_S}s")

    both_n = sum(1 for r in rows if r["cls"] == "both")
    base_both = both_n / len(rows)
    base_fev = _ev(rows, f"flip_fill{CONFIRM_S}s", flip=True)
    base_fev_n = sum(1 for r in rows if r.get(f"flip_fill{CONFIRM_S}s") is not None)

    print("=" * 70)
    print(f"  翻转线: 预判 both 类（flip EV +{base_fev:.2f}/股 的领域）特征筛选")
    print("=" * 70)

    # (key, 中文标签, 分桶)
    FEATURES = [
        # P0-3 PM tape（穿越后 10s）
        ("tape_imb", "穿越侧 tape 主动买卖不平衡", None),
        ("tape_net", "穿越侧 tape 净买量(股)", None),
        ("tape_share", "穿越侧 tape 占总成交量份额", None),
        ("tape_max", "最大单笔占比", None),
        ("tape_other_imb", "对侧 tape 不平衡", None),
        # P0-1 Binance 1s 流（穿越前 10s）
        ("flow_pre", "Binance 主动净买占比(前10s)", None),
        ("of_pre", "Binance 净买量(手, 前10s)", None),
        ("vol_pre", "Binance 总成交量(手, 前10s)", None),
        ("n_ticks_pre", "成交笔数(前10s)", None),
        # P0-2 深度 / reprice
        ("cross_bid_dep", "穿越侧 top5 bid 深度占比", None),
        ("cross_depth", "穿越侧 top5 总深度", None),
        ("book_age", "穿越时盘口新鲜度(ms)", None),
        ("reprice_n", "穿越后10s 盘口更新次数", None),
        ("reprice_gap", "穿越后首间隔 book_ts 差(ms)", None),
        # 微观轨迹
        ("post_dip", "穿越后10s 穿越侧 bid 回撤幅度", None),
        # v1 对照
        ("other_delta10s", "确认期对侧 bid 变化", [("≤0", -1e9, 0.0), ("0-0.01", 0.0, 0.01),
                                                   ("0.01-0.02", 0.01, 0.02), ("0.02-0.05", 0.02, 0.05),
                                                   (">0.05", 0.05, None)]),
        ("path_eff", "穿越前路径效率", [("≤0.4", -1e9, 0.4), ("0.4-0.6", 0.4, 0.6),
                                        ("0.6-0.8", 0.6, 0.8), (">0.8", 0.8, None)]),
        ("noise", "噪声比", [("<2", -1e9, 2.0), ("2-4", 2.0, 4.0), ("4-8", 4.0, 8.0), ("≥8", 8.0, None)]),
        ("flips", "方向翻转次数(前)", [("<3", -1e9, 3.0), ("3-6", 3.0, 6.0), ("≥6", 6.0, None)]),
        ("twap_pos", "TWAP 位置(hist)", [("≤0", -1e9, 0.0), ("0-0.25", 0.0, 0.25), (">0.25", 0.25, None)]),
        ("twap_gap", "spot-TWAP 缺口", [("≤-0.5", -1e9, -0.5), ("±0.5", -0.5, 0.5), (">0.5", 0.5, None)]),
        ("twap_delta", "确认期 TWAP 变化", [("≤-0.05", -1e9, -0.05), ("±0.05", -0.05, 0.05),
                                             (">0.05", 0.05, None)]),
    ]
    for key, label, buckets in FEATURES:
        screen(rows, key, label, buckets, base_both, base_fev, base_fev_n)


if __name__ == "__main__":
    main()
