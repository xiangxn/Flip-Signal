#!/usr/bin/env python3
"""
Binance → Chainlink TWAP-60 影响函数研究（Phase 1，2026-08-16）。

目的：量化 Binance 数据对 btc-updown-5m 结算基准（Chainlink TWAP-60）的传导，
并按 剩余时间 × 波动 regime × gap 符号 × 流方向 条件化，找出影响强度漂移
最大的格子，作为策略假设生成器。

本脚本只做"测量"，不做策略回测：
  - 目标变量 twap_close - twap_t 有意使用未来数据 —— 影响研究就是要测传导；
  - regime 用因果历史振幅（只看已结束窗口），保证 Phase 2/3 可直接沿用。

理论斜率（现货随机游走假设下）：
  - r < 60：结算窗口 [T-60,T] 与当前 TWAP 窗口重叠 (60-r)/60，
    gap 机械拖拽剩余窗口 → 斜率 = r/60
  - r ≥ 60：结算窗口完全在未来，E[未来均价] = spot_t → 斜率 = 1.0
  实测斜率 < 理论 = 现货均值回归强于 RW；> 理论 = 趋势延续强于 RW。
  dev = 实测 - 理论，|dev| 大的格子 = 偏离纯传导结构 = 假设候选。

注：raw gap = spot - twap 被恒定基差（~-68$）主导，符号维度无意义；
条件维度改用去基差后的 dgap = spot - binanceMA60（≈ spot 相对自身
60s 均线的位置，即经典动量/背离量）。回归的预测子仍是 raw gap
（斜率对常数基差不敏感，基差只进截距）。

输出 6 个部分：
  1. 数据概览
  2. 分解：twap = binanceMA60 + venue_comp（基差与残差分布）
  3. tick 级吸收率：corr(ΔBinanceMA60, ΔTwap)
  4. 影响矩阵：(twap_close - twap_t) ~ gap_t，按 r×regime×spot位置(相对自身
     60s均线) 与 r×|dgap|幅度 分格
  5. 流方向矩阵：按 r × 30s 主动流方向 分格
  6. 事件级整体影响 + venue_comp 尖峰检测（按小时/regime/r段 + Top 事件）
  7. 分半稳定性检验：按事件时间中位数对半切，各格子前/后半斜率与 dev 同号性

用法:
    ./venv/bin/python analyze_twap_influence.py --data ../data/btc
"""

import argparse
import json
from pathlib import Path

# ── 常量 ──
# r 分桶：(左开, 右闭]，0 < r <= 240
R_BUCKETS = [(0, 30, "0-30"), (30, 60, "30-60"), (60, 120, "60-120"), (120, 240, "120-240")]
# 各桶理论斜率：r<60 = 机械拖拽 r/60（取桶中点）；r≥60 = RW 纯预测 1.0
THEO = {"0-30": 0.25, "30-60": 0.75, "60-120": 1.0, "120-240": 1.0}
HIST_WINDOW = 18   # 历史振幅回看窗口数
HIST_MIN = 3       # 历史振幅最少需要的窗口数
MA_TICKS = 12      # 60s 均线 = 12 个 5s tick
MA_MIN = 6         # 均线最少样本数（事件首部 tick 不足时跳过）
SPIKE_USD = 15.0   # venue_comp 尖峰阈值（约 3σ）
DEV_MARK = 0.15    # |dev| ≥ 该值时标记为假设候选


def load_events(data_dir):
    events = []
    for p in sorted(Path(data_dir).glob("events_*.jsonl")):
        for line in p.open():
            line = line.strip()
            if line:
                events.append(json.loads(line))
    events.sort(key=lambda e: e["start_time"])
    return events


def inject_hist_range(events):
    """因果历史振幅（TWAP 口径，只看已结束窗口，无未来数据）。"""
    for i, e in enumerate(events):
        prev = []
        for j in range(max(0, i - HIST_WINDOW), i):
            o = events[j].get("twap_open_price") or 0.0
            c = events[j].get("twap_close_price") or 0.0
            if o > 0 and c > 0:
                prev.append(abs(c - o))
        e["_hist_range"] = (sum(prev) / len(prev)) if len(prev) >= HIST_MIN else None


def build_regime_map(events):
    """按因果历史振幅中位数把事件分为 low/high 波动 regime。"""
    valid = [e for e in events if e["_hist_range"] is not None]
    if not valid:
        return {}
    rngs = sorted(e["_hist_range"] for e in valid)
    med = rngs[len(rngs) // 2]
    return {e["start_time"]: ("high" if e["_hist_range"] > med else "low") for e in valid}


def meanstd(xs):
    n = len(xs)
    m = sum(xs) / n
    v = sum((x - m) ** 2 for x in xs) / (n - 1) if n > 1 else 0.0
    return m, v ** 0.5


def slope_corr(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    c = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return 0.0, 0.0
    return c / vx, c / (vx * vy) ** 0.5


def collect_ticks(events, regime_map):
    """逐 tick 收集观测：gap、未来 TWAP 目标、条件标签、venue_comp、Δ 对。"""
    ticks = []
    absorb_x, absorb_y = [], []
    for e in events:
        tc = e.get("twap_close_price") or 0.0
        if tc <= 0:
            continue
        snaps = e["snapshots"]
        reg = regime_map.get(e["start_time"])
        for i, s in enumerate(snaps):
            tp = s.get("twap_price") or 0.0
            sp = s.get("price") or 0.0
            if tp <= 0 or sp <= 0:
                continue
            lo = max(0, i - MA_TICKS + 1)
            ps = [snaps[k]["price"] for k in range(lo, i + 1)
                  if (snaps[k].get("price") or 0.0) > 0]
            if len(ps) < MA_MIN:
                continue
            ma = sum(ps) / len(ps)
            r = s["remaining_sec"]
            if r <= 0 or r > 240:
                continue
            bucket = next((name for lo_r, hi_r, name in R_BUCKETS if lo_r < r <= hi_r), None)
            if bucket is None:
                continue
            flows = [snaps[k].get("signed_flow_5s") for k in range(max(0, i - 5), i + 1)]
            flows = [f for f in flows if f is not None]
            flow30 = sum(flows) if len(flows) >= 3 else None

            dgap = sp - ma  # 去基差 gap ≈ spot 相对自身 60s 均线
            mag = abs(dgap)
            dgap_mag = "<15" if mag < 15 else ("15-40" if mag < 40 else "≥40")
            ticks.append({
                "e_start": e["start_time"], "r": r, "bucket": bucket,
                "regime": reg, "gap": sp - tp, "target": tc - tp, "dgap": dgap,
                "dgap_sign": "spot>MA60" if dgap > 0 else "spot≤MA60",
                "dgap_mag": dgap_mag,
                "flow_sign": ("buy" if flow30 > 0 else "sell") if flow30 is not None else None,
                "vc": tp - ma,
                "hour": (e["start_time"] // 3600) % 24,
            })
            # tick 级吸收：ΔBinanceMA60 vs ΔTwap（相邻 tick）
            if i > 0:
                prev = snaps[i - 1]
                p_tp = prev.get("twap_price") or 0.0
                lo2 = max(0, i - MA_TICKS)
                ps2 = [snaps[k]["price"] for k in range(lo2, i)
                       if (snaps[k].get("price") or 0.0) > 0]
                if p_tp > 0 and len(ps2) >= MA_MIN:
                    absorb_x.append(ma - sum(ps2) / len(ps2))
                    absorb_y.append(tp - p_tp)
    return ticks, absorb_x, absorb_y


def pct(vals, p):
    xs = sorted(vals)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


def fmt_key(kv, width=10):
    return " ".join(f"{v:<{width}}" if isinstance(v, str) else f"{v:>{width}}"
                    for v in kv if v is not None)


def print_matrix(title, ticks, dims, with_dev=True):
    print(title)
    groups = {}
    for t in ticks:
        key = tuple(t[d] for d in dims)
        if any(k is None for k in key):
            continue
        groups.setdefault(key, []).append(t)

    header = (fmt_key(dims, 10) + " " +
              f"{'n_ticks':>8} {'n_ev':>5} {'slope':>7} {'corr':>6} " +
              (f"{'dev':>7} " if with_dev else "") + f"{'mean|dgap|':>10}")
    print(header)
    print("-" * len(header))
    for key in sorted(groups):
        g = groups[key]
        xs = [t["gap"] for t in g]
        ys = [t["target"] for t in g]
        sl, cr = slope_corr(xs, ys)
        n_ev = len({t["e_start"] for t in g})
        mg = sum(abs(t["dgap"]) for t in g) / len(g)
        dev = sl - THEO[key[0]] if with_dev else 0.0
        mark = " ◀" if with_dev and abs(dev) >= DEV_MARK else ""
        print(f"{fmt_key(key, 10)} {len(g):>8} {n_ev:>5} {sl:>+7.3f} {cr:>6.3f} "
              + (f"{dev:>+7.3f} " if with_dev else "")
              + f"{mg:>10.1f}{mark}")
    print()


def print_matrix_split(title, ticks, dims, split_st):
    """按事件时间对半切：每个格子输出前/后半样本的斜率与 dev 同号性。

    同号 = 前/后半样本的 dev（实测-理论）同向，即偏离传导结构的方向
    在时间上稳定；✗ = 单半样本现象。格子需两半各覆盖 ≥15 个事件才输出。
    """
    print(title)
    groups = {}
    for t in ticks:
        key = tuple(t[d] for d in dims)
        if any(k is None for k in key):
            continue
        groups.setdefault(key, ([], []))
        groups[key][0 if t["e_start"] < split_st else 1].append(t)

    header = (fmt_key(dims, 10) + " " +
              f"{'nA':>6} {'nB':>6} {'slopeA':>8} {'slopeB':>8} "
              f"{'devA':>7} {'devB':>7} {'同号':>4}")
    print(header)
    print("-" * len(header))
    for key in sorted(groups):
        ga, gb = groups[key]
        nea = len({t["e_start"] for t in ga})
        neb = len({t["e_start"] for t in gb})
        if nea < 15 or neb < 15:
            continue
        sla, _ = slope_corr([t["gap"] for t in ga], [t["target"] for t in ga])
        slb, _ = slope_corr([t["gap"] for t in gb], [t["target"] for t in gb])
        da, db = sla - THEO[key[0]], slb - THEO[key[0]]
        same = "✓" if da * db > 0 else "✗"
        print(f"{fmt_key(key, 10)} {nea:>6} {neb:>6} {sla:>+8.3f} {slb:>+8.3f} "
              f"{da:>+7.3f} {db:>+7.3f} {same:>4}")
    print()


def section(title):
    print()
    print("=" * 74)
    print(f"  {title}")
    print("=" * 74)


def main():
    parser = argparse.ArgumentParser(description="Binance→TWAP 影响函数研究 (Phase 1)")
    parser.add_argument("--data", default="../data/btc/", help="JSONL 数据目录")
    parser.add_argument("--spike-usd", type=float, default=SPIKE_USD, help="venue_comp 尖峰阈值(USD)")
    args = parser.parse_args()

    events = load_events(args.data)
    if not events:
        print("无事件数据，退出。")
        return
    inject_hist_range(events)
    regime_map = build_regime_map(events)
    ticks, absorb_x, absorb_y = collect_ticks(events, regime_map)

    # ── 1. 数据概览 ──
    section("1. 数据概览")
    n_ev_ok = len({t["e_start"] for t in ticks})
    t0, t1 = events[0]["start_time"], events[-1]["start_time"]
    print(f"  事件数: {len(events)}  |  跨度: {(t1 - t0) / 3600:.1f} 小时")
    print(f"  有效 tick: {len(ticks)}  |  覆盖事件: {n_ev_ok}  |  "
          f"regime 可用事件: {sum(1 for v in regime_map.values())}")
    print(f"  regime 分界(中位历史振幅): 见第 4 节 'regime' 列  low/high")

    if not ticks:
        print("  无有效 tick（数据可能缺 twap_price/twap_close_price 字段）。")
        return

    # ── 2. venue_comp 分解 ──
    section("2. 分解: twap = binanceMA60 + venue_comp  (USD)")
    vcs = [t["vc"] for t in ticks]
    m, sd = meanstd(vcs)
    vc_med = pct(vcs, 0.5)  # 只算一次，后续尖峰过滤共用（避免 O(n²) 排序）
    spike = [v for v in vcs if abs(v - vc_med) > args.spike_usd]
    print(f"  n={len(vcs)}  median={vc_med:+.1f}  mean={m:+.1f}  std={sd:.1f}")
    print(f"  P5={pct(vcs, 0.05):+.1f}  P95={pct(vcs, 0.95):+.1f}  "
          f"|vc-median|>{args.spike_usd:.0f}$: {len(spike)} ({len(spike) / len(vcs) * 100:.1f}%)")
    print("  按剩余时间（基差应恒定；随 r 漂移 = 结算窗口内 Binance 权重变化）:")
    for _, _, name in R_BUCKETS:
        xs = [t["vc"] for t in ticks if t["bucket"] == name]
        if xs:
            bm, bsd = meanstd(xs)
            print(f"    r={name:>7}: n={len(xs):>5}  mean={bm:+.1f}  std={bsd:.1f}")

    # ── 3. tick 级吸收率 ──
    section("3. tick 级吸收率: corr(ΔBinanceMA60, ΔTwap)")
    if absorb_x:
        sl, cr = slope_corr(absorb_x, absorb_y)
        print(f"  n={len(absorb_x)}  slope={sl:.3f}  corr={cr:.3f}")
        print(f"  → Chainlink 聚合价对 Binance 的 5s 增量敏感度 ≈ {sl * 100:.0f}%")
    else:
        print("  无有效 Δ 对。")

    # ── 4. 影响矩阵 ──
    section("4. 影响矩阵: (twap_close - twap_t) ~ gap_t = spot_t - twap_t")
    print("  按剩余时间（无条件）:")
    for _, _, name in R_BUCKETS:
        g = [t for t in ticks if t["bucket"] == name]
        xs = [t["gap"] for t in g]
        ys = [t["target"] for t in g]
        sl, cr = slope_corr(xs, ys)
        print(f"    r={name:>7}: n={len(g):>5}  slope={sl:+.3f}  corr={cr:.3f}  "
              f"理论={THEO[name]:.2f}  dev={sl - THEO[name]:+.3f}")
    print()
    print_matrix("  按 r × regime × spot位置(相对自身60s均线) 分格（◀ = |dev|≥0.15 假设候选）:",
                 ticks, ["bucket", "regime", "dgap_sign"])
    print_matrix("  按 r × |dgap| 幅度 分格（USD）:",
                 ticks, ["bucket", "dgap_mag"])

    # ── 5. 流方向矩阵 ──
    section("5. 流方向矩阵: 按 r × 30s 主动流方向 分格")
    print_matrix("  (twap_close - twap_t) ~ gap_t，流方向 = 穿越前 30s 累计主动流符号:",
                 ticks, ["bucket", "flow_sign"])

    # ── 6. 事件级影响 + 尖峰检测 ──
    section("6. 事件级整体影响: dTwap ~ dBinance")
    xs, ys = [], []
    for e in events:
        to, tc = e.get("twap_open_price") or 0.0, e.get("twap_close_price") or 0.0
        bo, bc = e.get("open_price") or 0.0, e.get("close_price") or 0.0
        if to > 0 and tc > 0 and bo > 0 and bc > 0:
            xs.append(bc - bo)
            ys.append(tc - to)
    if xs:
        sl, cr = slope_corr(xs, ys)
        bm, bsd = meanstd(xs)
        tm, tsd = meanstd(ys)
        print(f"  n={len(xs)}  slope={sl:.3f}  corr={cr:.3f}  "
              f"(binance std={bsd:.1f}, twap std={tsd:.1f})")
        print("  按 regime:")
        for reg in ("low", "high"):
            rx = [x for x, e in zip(xs, events)
                  if regime_map.get(e["start_time"]) == reg and x is not None]
            ry = [y for y, e in zip(ys, events)
                  if regime_map.get(e["start_time"]) == reg and y is not None]
            if len(rx) > 1:
                rsl, rcr = slope_corr(rx, ry)
                print(f"    {reg:>5}: n={len(rx):>4}  slope={rsl:.3f}  corr={rcr:.3f}")

    print()
    print(f"  venue_comp 尖峰率 (|vc-median|>{args.spike_usd:.0f}$) 按小时 UTC:")
    hours = {}
    for t in ticks:
        hours.setdefault(t["hour"], []).append(t["vc"])
    for h in sorted(hours):
        xs = hours[h]
        sp = sum(1 for v in xs if abs(v - vc_med) > args.spike_usd)
        print(f"    {h:02d} 时: n={len(xs):>5}  尖峰 {sp / len(xs) * 100:>5.1f}%  "
              f"median={pct(xs, 0.5):+.1f}")

    print()
    print(f"  venue_comp 尖峰率 按 regime:")
    for reg in ("low", "high"):
        xs = [t["vc"] for t in ticks if t["regime"] == reg]
        if xs:
            reg_med = pct(xs, 0.5)
            sp = sum(1 for v in xs if abs(v - vc_med) > args.spike_usd)
            print(f"    {reg:>5}: n={len(xs):>5}  尖峰 {sp / len(xs) * 100:>5.1f}%  "
                  f"median={reg_med:+.1f}")

    print()
    print("  venue_comp 尖峰率 Top 5 事件:")
    ev_rows = []
    for e in events:
        ets = [t for t in ticks if t["e_start"] == e["start_time"]]
        if len(ets) < 30:
            continue
        sp = sum(1 for t in ets if abs(t["vc"] - vc_med) > args.spike_usd)
        ev_rows.append((sp / len(ets), e["start_time"], regime_map.get(e["start_time"]), len(ets)))
    ev_rows.sort(reverse=True)
    for rate, st, reg, n in ev_rows[:5]:
        from datetime import datetime, timezone
        dt = datetime.fromtimestamp(st, tz=timezone.utc)
        print(f"    {dt:%m-%d %H:%M}  regime={reg}  尖峰 {rate * 100:.1f}%  (n={n})")

    # ── 7. 分半稳定性检验 ──
    section("7. 分半稳定性检验（按事件时间中位数对半切）")
    starts = sorted({t["e_start"] for t in ticks})
    split_st = starts[len(starts) // 2]
    print(f"  切分点: {split_st}  前一半 {sum(1 for s in starts if s < split_st)} 事件 / "
          f"后一半 {sum(1 for s in starts if s >= split_st)} 事件")
    print("  同号 = 前/后半样本的 dev 相对理论同向；nA/nB = 各半覆盖事件数；✗ = 单半样本现象")
    print()
    print_matrix_split("  无条件按剩余时间:", ticks, ["bucket"], split_st)
    print_matrix_split("  r × regime × spot位置:", ticks, ["bucket", "regime", "dgap_sign"], split_st)
    print_matrix_split("  r × 流方向:", ticks, ["bucket", "flow_sign"], split_st)


if __name__ == "__main__":
    main()
