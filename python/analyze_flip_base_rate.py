#!/usr/bin/env python3
"""
0.7 穿越的翻转基率分析（TWAP 结算真相）。

策略核心问题：一方 bid 穿越 0.7 后，最终 TWAP 结算有多少比例翻向了对面？
（翻转 = 穿越侧最终输掉）。基率 vs 买入对侧的成交价决定策略是否天然有 EV：
    EV/股 = 翻转率 - 成交价（买入价即盈亏平衡点）

对每个事件的每侧首个上升沿穿越（有效窗口 + min snaps，与引擎同口径），
记录穿越/确认特征并按维度分桶统计翻转率、成交价、EV：
  成交价分桶 / 剩余时间（30s 细粒度 + 尾部向内累计，双口径）/
  TWAP 背离度 / Binance 背离度 / 触发深度 / 对侧确认 other_delta /
  TWAP 振幅扩张 / Binance 现货 2-tick 反转

用法:
    python analyze_flip_base_rate.py --data ../data/btc
"""

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest_flip_config import FlipBacktestConfig
from backtest_flip_utils import load_events_from_dir, compute_hist_avg_range


@dataclass
class Crossing:
    event_start: int
    side: str            # "yes" 或 "no"（穿越侧）
    cross_idx: int
    remaining_sec: int
    trigger_bid: float   # 穿越侧 bid（穿越时刻）
    fill: float          # 对侧 ask = 1 - 穿越侧 bid（确认时刻，缺则穿越时刻）
    flip: bool           # 最终结算翻向对面 = 穿越侧输
    # 特征
    twap_div: float | None       # TWAP 背离度（正=TWAP 与 PM 反向）
    binance_div: float | None    # Binance 背离度
    other_delta: float | None    # 确认期对侧 bid 变化
    range_exp: float | None      # TWAP 振幅扩张
    binance_ret_2tick: float | None  # 确认期 Binance 现货收益（正=朝穿越侧方向续动）
    outcome: int         # 最终结算（0=UP 1=DOWN）


def collect_crossings(events, cfg: FlipBacktestConfig) -> list[Crossing]:
    """收集每事件每侧首个上升沿穿越（有效窗口 + min snaps）。"""
    crossings = []
    for event in events:
        if event.get("hist_avg_range") is None:
            continue
        snaps = event["snapshots"]
        outcome = event["outcome"]
        hist = event["hist_avg_range"]
        twap_open = event.get("twap_open_price") or 0

        for side in ("yes", "no"):
            this_key = "yes_price" if side == "yes" else "no_price"
            other_key = "no_price" if side == "yes" else "yes_price"
            was_above = False
            for i, s in enumerate(snaps):
                is_above = (s[this_key] > cfg.trigger_threshold
                            and s["remaining_sec"] < cfg.max_remaining_sec
                            and s["remaining_sec"] > cfg.min_remaining_sec)
                if is_above and not was_above and i >= cfg.min_pre_snaps:
                    # 穿越特征
                    twap_price = s.get("twap_price") or 0
                    twap_div = None
                    if twap_price and twap_open:
                        btc_pos = (twap_price - twap_open) / hist if hist else 0.0
                        twap_div = -btc_pos if side == "yes" else btc_pos
                    binance_div = None
                    if s.get("price") and event.get("open_price"):
                        bpos = (s["price"] - event["open_price"]) / hist if hist else 0.0
                        binance_div = -bpos if side == "yes" else bpos
                    range_exp = None
                    if twap_price and twap_open:
                        range_exp = abs(twap_price - twap_open) / hist if hist else None

                    # 确认特征（确认 tick 不足则用穿越时刻值）
                    conf = snaps[i + cfg.confirm_delay_ticks] if i + cfg.confirm_delay_ticks < len(snaps) else None
                    if conf is not None:
                        fill = 1.0 - conf[this_key]  # 对侧 ask = 1 - 触发侧 bid
                        other_delta = conf[other_key] - s[other_key]
                        if s.get("price") and conf.get("price") and s["price"] > 0:
                            binance_ret_2tick = (conf["price"] - s["price"]) / s["price"]
                            if side == "no":
                                binance_ret_2tick = -binance_ret_2tick  # 朝穿越侧方向为正
                        else:
                            binance_ret_2tick = None
                    else:
                        fill = 1.0 - s[this_key]
                        other_delta = None
                        binance_ret_2tick = None

                    crossings.append(Crossing(
                        event_start=event["start_time"],
                        side=side,
                        cross_idx=i,
                        remaining_sec=s["remaining_sec"],
                        trigger_bid=s[this_key],
                        fill=fill,
                        flip=(outcome == 1) if side == "yes" else (outcome == 0),
                        twap_div=twap_div,
                        binance_div=binance_div,
                        other_delta=other_delta,
                        range_exp=range_exp,
                        binance_ret_2tick=binance_ret_2tick,
                        outcome=outcome,
                    ))
                    break
                was_above = is_above
    return crossings


def bucket_report(title, xs, pred, key=None):
    """按 pred 分桶打印 n/翻转率/均价/EV。key 为 Crossing 属性名。"""
    buckets = {}
    for x in xs:
        label = pred(x) if key is None else pred(getattr(x, key))
        if label is None:
            continue
        buckets.setdefault(label, []).append(x)

    print(f"\n  【{title}】")
    print(f"  {'分桶':<22s} {'n':>4s} {'翻转率':>8s} {'均价':>7s} {'EV/股':>8s}")
    for label in sorted(buckets, key=str):
        b = buckets[label]
        n = len(b)
        fr = sum(1 for x in b if x.flip) / n
        mf = sum(x.fill for x in b) / n
        ev = fr - mf
        print(f"  {str(label):<22s} {n:>4d} {fr * 100:>7.1f}% {mf:>7.3f} {ev:>+8.3f}")


def remaining_time_report(xs, title):
    """30s 细粒度剩余时间分桶 + 尾部向内累计视图（翻转率/均价/EV）。"""
    print(f"\n  【{title}】 n={len(xs)}")
    print(f"  {'剩余时间':>12s} {'n':>4s} {'翻转率':>9s} {'95%CI':>12s} {'均价':>7s} {'EV/股':>8s}")
    edges = [(240, 260), (210, 240), (180, 210), (150, 180), (120, 150),
             (90, 120), (60, 90), (30, 60), (15, 30)]
    for lo, hi in edges:
        b = [x for x in xs if lo < x.remaining_sec <= hi]
        if not b:
            print(f"  {f'{lo + 1}-{hi}s':>12s} {0:>4d}        -")
            continue
        n = len(b)
        fr = sum(1 for x in b if x.flip) / n
        mf = sum(x.fill for x in b) / n
        se = (fr * (1 - fr) / n) ** 0.5
        print(f"  {f'{lo + 1}-{hi}s':>12s} {n:>4d} {fr * 100:>8.1f}% "
              f"{f'±{1.96 * se * 100:.1f}pp':>12s} {mf:>7.3f} {fr - mf:>+8.3f}")
    # 累计（窗口尾部向内）：限定"只做尾部穿越"时的整体表现
    print("  --- 累计（窗口尾部向内）---")
    for cutoff in (60, 90, 120, 150, 180, 210):
        b = [x for x in xs if x.remaining_sec <= cutoff]
        if len(b) < 5:
            continue
        n = len(b)
        fr = sum(1 for x in b if x.flip) / n
        mf = sum(x.fill for x in b) / n
        se = (fr * (1 - fr) / n) ** 0.5
        print(f"  {f'≤{cutoff}s':>12s} {n:>4d} {fr * 100:>8.1f}% "
              f"{f'±{1.96 * se * 100:.1f}pp':>12s} {mf:>7.3f} {fr - mf:>+8.3f}")


def main():
    parser = argparse.ArgumentParser(description="0.7 穿越翻转基率分析")
    parser.add_argument("--data", default="../data/btc/", help="JSONL 数据目录")
    args = parser.parse_args()

    events = load_events_from_dir(args.data)
    cfg = FlipBacktestConfig()
    compute_hist_avg_range(events, cfg.hist_window_N, "twap")

    crossings = collect_crossings(events, cfg)
    # 每事件只保留最早一次穿越（一注/事件的口径）
    first = {}
    for x in crossings:
        first.setdefault(x.event_start, x)
    earliest = list(first.values())

    print(f"事件: {len(events)} | 穿越观测: {len(crossings)} (每事件最早: {len(earliest)})")

    # ── 总体基率 ──
    for label, xs in [("全部穿越", crossings), ("每事件最早穿越", earliest)]:
        n = len(xs)
        fr = sum(1 for x in xs if x.flip) / n
        mf = sum(x.fill for x in xs) / n
        se = (fr * (1 - fr) / n) ** 0.5
        print(f"\n【{label}】 n={n}  翻转率={fr * 100:.1f}% (±{1.96 * se * 100:.1f}pp 95%CI)  "
              f"均价={mf:.3f}  EV={fr - mf:+.3f}/股")

    # ── 成交价分桶 ──
    def fill_bucket(x):
        f = x.fill
        if f < 0.15: return "<0.15"
        if f < 0.20: return "0.15-0.20"
        if f < 0.25: return "0.20-0.25"
        if f < 0.30: return "0.25-0.30"
        if f <= 0.45: return "0.30-0.45"
        return None  # 不可成交
    bucket_report("成交价分桶（对侧 ask）", earliest, fill_bucket)

    # ── 剩余时间分桶（30s 细粒度 + 累计，两个口径）──
    remaining_time_report(crossings, "剩余时间翻转率 — 全部穿越观测")
    remaining_time_report(earliest, "剩余时间翻转率 — 每事件最早穿越")

    # ── TWAP 背离度分桶 ──
    def twap_div_bucket(x):
        d = x.twap_div
        if d is None: return None
        if d < -0.5: return "TWAP同向<-0.5"
        if d < 0: return "TWAP同向 -0.5~0"
        if d < 0.5: return "TWAP背离 0~0.5"
        return "TWAP背离 >0.5"
    bucket_report("TWAP 背离度（正=与 PM 反向）", earliest, twap_div_bucket)

    # ── Binance 背离度分桶 ──
    def binance_div_bucket(x):
        d = x.binance_div
        if d is None: return None
        if d < -0.5: return "Bin同向<-0.5"
        if d < 0: return "Bin同向 -0.5~0"
        if d < 0.5: return "Bin背离 0~0.5"
        return "Bin背离 >0.5"
    bucket_report("Binance 背离度（正=与 PM 反向）", earliest, binance_div_bucket)

    # ── 触发深度分桶 ──
    def depth_bucket(x):
        t = x.trigger_bid
        if t < 0.75: return "0.70-0.75"
        if t < 0.80: return "0.75-0.80"
        return ">=0.80"
    bucket_report("触发侧深度", earliest, depth_bucket)

    # ── other_delta 分桶 ──
    def od_bucket(x):
        d = x.other_delta
        if d is None: return None
        if d <= 0: return "对侧回落 <=0"
        if d <= 0.01: return "0~0.01"
        if d <= 0.02: return "0.01~0.02"
        if d <= 0.05: return "0.02~0.05"
        return ">0.05"
    bucket_report("确认期对侧变化 other_delta", earliest, od_bucket)

    # ── TWAP 振幅扩张分桶 ──
    def re_bucket(x):
        r = x.range_exp
        if r is None: return None
        if r < 0.5: return "<0.5"
        if r < 1.0: return "0.5-1.0"
        if r < 1.5: return "1.0-1.5"
        return ">=1.5"
    bucket_report("TWAP 振幅扩张 range_exp", earliest, re_bucket)

    # ── Binance 现货确认期续动方向 ──
    def spot_bucket(x):
        r = x.binance_ret_2tick
        if r is None: return None
        if r < -0.0003: return "现货已反转 (<-3bp)"
        if r < 0.0003: return "现货走平"
        return "现货续动 (>+3bp)"
    bucket_report("确认期 Binance 现货方向（正=朝穿越侧）", earliest, spot_bucket)

    # ── 组合维度：TWAP 背离 × 现货确认方向 ──
    print("\n  【TWAP背离 × 现货确认方向 组合】")
    print(f"  {'组合':<32s} {'n':>4s} {'翻转率':>8s} {'均价':>7s} {'EV/股':>8s}")
    for tw in [("背离", lambda d: d is not None and d > 0),
               ("同向", lambda d: d is not None and d <= 0)]:
        for sp in [("现货反转", lambda r: r is not None and r < -0.0003),
                   ("现货未反转", lambda r: r is not None and r >= -0.0003)]:
            b = [x for x in earliest
                 if tw[1](x.twap_div) and sp[1](x.binance_ret_2tick)]
            if len(b) < 5:
                continue
            fr = sum(1 for x in b if x.flip) / len(b)
            mf = sum(x.fill for x in b) / len(b)
            print(f"  {'TWAP' + tw[0] + ' × ' + sp[0]:<32s} {len(b):>4d} "
                  f"{fr * 100:>7.1f}% {mf:>7.3f} {fr - mf:>+8.3f}")


if __name__ == "__main__":
    main()
