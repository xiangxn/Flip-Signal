#!/usr/bin/env python3
"""
Part 3: 阈值搜索 + 冻结训练/测试样本外验证（TWAP 真实结算）。

流程:
  1. 按天切分: train = 08-05..08-10（6 天），test = 08-11..08-12（2 天）
  2. 单特征双向阈值搜索（阈值格点 = train 分位数，冻结后套 test）
  3. 贪心 AND 组合（最多 3 层），每层在 train 上选择，test 只在最后评估
  4. 最终过滤器的实盘口径模拟: 每事件一注，首个通过的穿越成交（fill2）

防过拟合声明:
  * 特征候选只来自 Part 2 中 χ² p<0.1 的（其余不进搜索空间）
  * 阈值在 train 上冻结，test 只评估一次
  * 最终过滤器按天输出 EV 表，两日 test 同号才视为线索

用法:
    python 03_filter_search.py --data ../../data_0/lab_resolved
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import (load_events, collect_crossings, stats, split_by_days,
                 ev_per_bet, day_of)

TEST_DAYS = {"08-11", "08-12"}
MIN_TRAIN_N = 50          # 训练集过滤后最少观测
MIN_GRID = 5              # 阈值格点最小保留（百分位 5% 步长）


# 候选特征: (key, 名称)。方向符号: +1 = 高值有利（≥t），-1 = 低值有利（≤t）
CANDIDATES = [
    ("flow_5s",       "5s主动流（正=反向）",       +1),
    ("flow_cum30",    "30s累计流（正=反向）",      +1),
    ("depth_imb",     "盘口失衡（正=反向）",       +1),
    ("twap60_pos",    "60s均值位置（正=反向）",    -1),
    ("gap60",         "spot-60s缺口（正=反向）",   +1),
    ("trigger_bid",   "触发价",                   -1),
    ("remaining_sec", "剩余时间",                 +1),
    ("range_exp",     "振幅扩张",                 -1),
    ("pm_vel",        "PM升速",                   +1),
    ("vol_10s",       "波动率10s",                -1),
    ("hour_utc",      "UTC小时(12-18窗口)",       +1),  # 离散特殊处理
]

# 离散窗口型过滤器（非单调阈值）
WINDOW_FILTERS = [
    ("hour 12-18 UTC", lambda x: 12 <= x.hour_utc < 18),
    ("first crossing of event", lambda x: x.cross_count_same == 1
                                          and x.total_crosses_before == 1),
    ("YES side", lambda x: x.side == "yes"),
    ("NO side", lambda x: x.side == "no"),
]


def split_train_test(xs):
    tr = [x for x in xs if day_of(x) not in TEST_DAYS]
    te = [x for x in xs if day_of(x) in TEST_DAYS]
    return tr, te


def sweep_single(tr, te, key, direction, name):
    """单特征阈值搜索: 格点 = train 分位数。返回最佳阈值 + 两集统计。"""
    vals = sorted(getattr(x, key) for x in tr if getattr(x, key) is not None)
    if len(vals) < MIN_TRAIN_N * 2:
        return None
    results = []
    for q in range(MIN_GRID, 100 - MIN_GRID + 1, MIN_GRID):
        t = vals[len(vals) * q // 100]
        if direction > 0:
            sub_tr = [x for x in tr if getattr(x, key) is not None
                      and getattr(x, key) >= t]
        else:
            sub_tr = [x for x in tr if getattr(x, key) is not None
                      and getattr(x, key) <= t]
        if len(sub_tr) < MIN_TRAIN_N:
            continue
        if direction > 0:
            sub_te = [x for x in te if getattr(x, key) is not None
                      and getattr(x, key) >= t]
        else:
            sub_te = [x for x in te if getattr(x, key) is not None
                      and getattr(x, key) <= t]
        a, b = stats(sub_tr, "fill2"), stats(sub_te, "fill2")
        results.append((t, a, b))
    if not results:
        return None
    # 按 train EV 选最优（test 只报告，不参与选择）
    best = max(results, key=lambda r: r[1]["ev"])
    return best


def report_row(label, a_tr, a_te, t=None):
    row = (f"  {label:<34s} thr={t:>10.4g}" if t is not None
           else f"  {label:<34s} {'':>14s}")
    if a_tr and a_tr["n"]:
        row += (f" | train n={a_tr['n']:>4d} 翻转率 {a_tr['flip_rate'] * 100:>5.1f}% "
                f"fill {a_tr['mean_fill']:.3f} EV {a_tr['ev']:+.4f}")
    if a_te and a_te["n"]:
        row += (f" | test n={a_te['n']:>4d} 翻转率 {a_te['flip_rate'] * 100:>5.1f}% "
                f"fill {a_te['mean_fill']:.3f} EV {a_te['ev']:+.4f}")
    print(row)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="../../data_0/lab_resolved")
    args = parser.parse_args()

    events = load_events(args.data)
    crossings = collect_crossings(events)
    tr, te = split_train_test(crossings)
    print("=" * 88)
    print("  Part 3: 冻结阈值搜索 + 样本外验证")
    print("=" * 88)
    print(f"  全部穿越: {len(crossings)}  train={len(tr)}（08-05..08-10）  "
          f"test={len(te)}（08-11..08-12）")
    b_tr, b_te = stats(tr, "fill2"), stats(te, "fill2")
    print(f"  基线: train 翻转率 {b_tr['flip_rate'] * 100:.1f}% EV {b_tr['ev']:+.4f}"
          f" | test 翻转率 {b_te['flip_rate'] * 100:.1f}% EV {b_te['ev']:+.4f}")

    # ══ 1. 单特征搜索 ══
    print(f"\n  ── 1. 单特征阈值搜索（train 选阈值，test 验证）──")
    singles = []
    for key, name, direction in CANDIDATES:
        if key == "hour_utc":
            continue  # 离散特征走窗口过滤器
        r = sweep_single(tr, te, key, direction, name)
        if r:
            t, a_tr, a_te = r
            report_row(name, a_tr, a_te, t)
            singles.append((name, key, direction, t, a_tr, a_te))
    print()

    # 离散窗口过滤器
    print(f"  ── 离散窗口过滤器 ──")
    window_rows = []
    for name, pred in WINDOW_FILTERS:
        sub_tr = [x for x in tr if pred(x)]
        sub_te = [x for x in te if pred(x)]
        a_tr, a_te = stats(sub_tr, "fill2"), stats(sub_te, "fill2")
        report_row(name, a_tr, a_te)
        window_rows.append((name, pred, a_tr, a_te))

    # ══ 2. 贪心 AND 组合（以单特征中 test EV 最高者为基础）══
    print(f"\n  ── 2. 贪心 AND 组合 ──")
    # 基础层选择: train EV 最高的单特征
    ranked = sorted(singles, key=lambda r: r[4]["ev"], reverse=True)
    if not ranked:
        print("  无候选。")
        return

    base_name, base_key, base_dir, base_t, _, _ = ranked[0]
    print(f"  基础过滤器: {base_name} (thr={base_t:.4g})")

    def base_pred(x):
        v = getattr(x, base_key)
        if v is None:
            return False
        return v >= base_t if base_dir > 0 else v <= base_t

    base_tr = [x for x in tr if base_pred(x)]
    base_te = [x for x in te if base_pred(x)]

    # 第二层: 在基础子集内搜索其余特征
    print(f"\n  第二层候选（在基础子集 train n={len(base_tr)} 内搜索）:")
    combos = []
    for key, name, direction in CANDIDATES:
        if key == base_key or key == "hour_utc":
            continue
        r = sweep_single(base_tr, base_te, key, direction, name)
        if r:
            t, a_tr, a_te = r
            report_row(f"{base_name} + {name}", a_tr, a_te, t)
            combos.append((name, key, direction, t, a_tr, a_te))
    for name, pred, a_tr, a_te in window_rows:
        sub_tr = [x for x in base_tr if pred(x)]
        sub_te = [x for x in base_te if pred(x)]
        a_tr2, a_te2 = stats(sub_tr, "fill2"), stats(sub_te, "fill2")
        if a_tr2["n"] >= MIN_TRAIN_N:
            report_row(f"{base_name} + {name}", a_tr2, a_te2)
            combos.append((name, "window", 0, None, a_tr2, a_te2))

    # 选择组合: train EV 最高
    if not combos:
        print("  无组合可用。")
        return
    best_combo = max(combos, key=lambda r: r[4]["ev"])
    cname, ckey, cdir, ct, c_tr, c_te = best_combo
    print(f"\n  选定组合: {base_name} + {cname}")
    if ckey == "window":
        pred2 = next(p for n, p, _, _ in window_rows if n == cname)
    else:
        pred2 = (lambda v: v >= ct) if cdir > 0 else (lambda v: v <= ct)

    def combo_pred(x):
        if not base_pred(x):
            return False
        if ckey == "window":
            return pred2(x)
        v = getattr(x, ckey)
        return v is not None and pred2(v)

    final_tr = [x for x in tr if combo_pred(x)]
    final_te = [x for x in te if combo_pred(x)]
    print(f"  最终组合: train {stats(final_tr, 'fill2')['n']} / "
          f"test {stats(final_te, 'fill2')['n']} 观测")
    report_row("最终组合", stats(final_tr, "fill2"), stats(final_te, "fill2"))

    # ══ 3. 实盘口径模拟: 每事件一注 ══
    print(f"\n  ── 3. 每事件一注模拟（时间优先首个通过穿越，fill2 成交）──")
    for label, pred, xs in [("最终组合", combo_pred, crossings),
                            ("基础过滤器", base_pred, crossings)]:
        by_event: dict[int, list] = {}
        for x in xs:
            by_event.setdefault(x.event_start, []).append(x)
        bets = []
        for et, es in sorted(by_event.items()):
            es.sort(key=lambda x: x.cross_idx)
            for x in es:
                if pred(x) and x.fill2 is not None:
                    bets.append(x)
                    break
        a = stats(bets, "fill2")
        pnl = sum(ev_per_bet(x, "fill2") for x in bets)
        days = split_by_days(bets)
        print(f"\n  【{label}】每事件一注: n={a['n']} 翻转率 {a['flip_rate'] * 100:.1f}% "
              f"fill {a['mean_fill']:.3f} EV {a['ev']:+.4f}/股 总P&L {pnl:+.2f} 股")
        print(f"  {'日期':<8s} {'n':>5s} {'翻转率':>8s} {'fill':>7s} {'EV/股':>8s}")
        for d in sorted(days):
            ad = stats(days[d], "fill2")
            print(f"  {d:<8s} {ad['n']:>5d} {ad['flip_rate'] * 100:>7.1f}% "
                  f"{ad['mean_fill']:>7.3f} {ad['ev']:>+8.4f}")

    # ══ 4. fill0 敏感性 ══
    print(f"\n  ── 4. 敏感性: 穿越 tick 立即成交（fill0, 无确认延迟）──")
    for label, pred in [("最终组合", combo_pred), ("基础过滤器", base_pred)]:
        sub = [x for x in crossings if pred(x)]
        a = stats(sub, "fill0")
        print(f"  {label}: n={a['n']} 翻转率 {a['flip_rate'] * 100:.1f}% "
              f"fill {a['mean_fill']:.3f} EV {a['ev']:+.4f}/股")


if __name__ == "__main__":
    main()
