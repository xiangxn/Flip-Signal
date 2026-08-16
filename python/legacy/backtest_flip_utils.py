#!/usr/bin/env python3
"""
回测工具函数 — 精简公式版翻转信号回测（Formula B, 2026-08-13 标定）。

特征提取 + 信号检测的纯函数，不依赖 IO。
所有函数均为无副作用纯函数，便于测试和调参。

Formula B 三个信号条件（详见 docs/flip_strategy_plan_2026-08-13.md）:
  B1 背离硬要求: BTC 不得与 PM 同向（divergence_floor=0，div < floor
     直接否决；无历史振幅的事件整体跳过，与实盘引擎行为一致）
  B2 过度自信:   range_expansion < 1.0 → +2
  B3 确认回归:   other_delta 三档 → +3/+2/+1
  score_entry = 2: 任一核心信号成立即触发

成交口径（实盘对齐）:
  yes_price/no_price 存的是各订单簿 BEST BID。实盘 FAK 买对侧成交在
  ASK = 1 - 触发侧 bid（双token互补）。fill_price 与 max_entry_price
  gate 均按 ask 口径计算 — 旧口径按对侧 bid 成交（entry+other_delta）
  每笔系统性低估 spread（实测 ~1 分）。
"""

from __future__ import annotations

from dataclasses import dataclass

from backtest_flip_config import FlipBacktestConfig, DEFAULT_CONFIG


# ═══════════════════════════════════════════════════════════════
# 事件级预处理
# ═══════════════════════════════════════════════════════════════

def _hist_open_close(event: dict) -> tuple[float, float]:
    """返回历史振幅口径的开/收盘价 (open, close)。

    TWAP 官方字段存在时优先 TWAP（btc-updown-5m 结算基准）；
    否则回退 Binance 字段（旧 data_0 兼容）。
    """
    if event.get("twap_open_price") and event.get("twap_close_price"):
        return event["twap_open_price"], event["twap_close_price"]
    return event["open_price"], event["close_price"]


def compute_hist_avg_range(events: list[dict], window_N: int = 18,
                           source: str = "twap") -> None:
    """对每个事件计算前 N 个窗口的平均振幅，原地注入 hist_avg_range。

    events 需按 start_time 升序排列。
    振幅 = close - open 的绝对值。
    source="twap" 时用官方 TWAP 开/收盘（缺失回退 Binance），
    source="binance" 时强制 Binance 口径（研究对照）。
    不足 3 个历史事件时设为 None。
    """
    for i, event in enumerate(events):
        prev_ranges = []
        for j in range(max(0, i - window_N), i):
            if source == "binance":
                o, c = events[j]["open_price"], events[j]["close_price"]
            else:
                o, c = _hist_open_close(events[j])
            prev_ranges.append(abs(c - o))
        if len(prev_ranges) >= 3:
            event["hist_avg_range"] = sum(prev_ranges) / len(prev_ranges)
        else:
            event["hist_avg_range"] = None


def load_events_from_dir(data_dir: str) -> list[dict]:
    """从 JSONL 目录加载所有 event，按 start_time 排序并计算历史振幅。"""
    import json
    from pathlib import Path

    events = []
    for path in sorted(Path(data_dir).glob("events_*.jsonl")):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))

    events.sort(key=lambda e: e["start_time"])
    compute_hist_avg_range(events)
    return events


# ═══════════════════════════════════════════════════════════════
# 特征提取（Formula B）
# ═══════════════════════════════════════════════════════════════

def compute_range_expansion(price_at_cross: float, open_price: float,
                            hist_avg_range: float | None) -> float | None:
    """B2: 振幅扩张 = |price - open| / hist_avg_range。

    Tick-independent — 不依赖采样频率。
    无历史数据时返回 None。
    """
    if hist_avg_range is None or hist_avg_range == 0:
        return None
    return abs(price_at_cross - open_price) / hist_avg_range


def compute_btc_position(price_at_cross: float, open_price: float,
                         hist_avg_range: float) -> float:
    """BTC 位移 vs Open，以历史平均振幅为单位。

    btc_position = (price - open) / hist_avg_range

    Tick-independent — 不依赖采样频率，实盘流式数据同样适用。
    +1.0 = BTC 涨了 1x 历史振幅, -1.0 = BTC 跌了 1x 历史振幅。
    """
    if hist_avg_range == 0:
        return 0.0
    return (price_at_cross - open_price) / hist_avg_range


def compute_btc_divergence(side: str, btc_position: float) -> float:
    """B1: 背离度（方向校正后），正 = BTC 与 PM 反向。

    YES 侧触发（PM 看涨）取 -btc_position（BTC 跌 = 背离）；
    NO 侧触发（PM 看跌）取 +btc_position（BTC 涨 = 背离）。
    """
    return -btc_position if side == "yes" else btc_position


def compute_other_delta(snapshots: list[dict], cross_idx: int, other_key: str,
                        cfg: FlipBacktestConfig) -> float | None:
    """B3: 对面价格 N 秒变化。

    other_delta = other_price[t + confirm_delay_ticks] - other_price[t]。

    与 Go 引擎一致：确认 tick 必须真实存在（实盘市场已结束时无法获得
    确认数据），数据不足时返回 None 表示无法确认。
    """
    conf_idx = cross_idx + cfg.confirm_delay_ticks
    if conf_idx >= len(snapshots):
        return None  # 窗口末尾：确认数据不存在，实盘同样无法确认
    return snapshots[conf_idx][other_key] - snapshots[cross_idx][other_key]


# ═══════════════════════════════════════════════════════════════
# 信号检测 & 评分
# ═══════════════════════════════════════════════════════════════

@dataclass
class FlipSignal:
    """单笔翻转信号。"""
    event_time: int         # unix 秒
    condition_id: str
    side: str               # "yes" 或 "no"
    score: int              # 复合评分（Formula B: 最高 5）
    entry_price: float      # 穿越时刻对侧 bid (特征/纸面结算用)
    fill_price: float       # 确认时刻对侧 ASK = 1 - 触发侧 bid（实盘真实成交价）
    won: bool               # 是否赢
    pnl: float              # 盈亏 (按 fill_price 成交计算)
    shares: int             # 下单量
    remaining_sec: int      # 入场时剩余秒数

    # 特征详情 (debug / 分析用)
    range_expansion: float | None
    btc_position: float
    btc_divergence: float   # 背离度：正=BTC 与 PM 反向
    other_delta: float


def _score_crossing(event: dict, side: str, cross_idx: int,
                    cfg: FlipBacktestConfig) -> FlipSignal | None:
    """对指定穿越点评分，返回 FlipSignal 或 None（评分不足/被否决）。

    Formula B 流程（2026-08-13）:
      窗口 → 确认tick存在 → B1 背离硬要求 → F0 真突破否决
      → gate(ask口径) → 评分(B2+B3) → 判定输赢(ask成交)
    """
    this_key = "yes_price" if side == "yes" else "no_price"
    other_key = "no_price" if side == "yes" else "yes_price"
    snaps = event["snapshots"]
    cross_snap = snaps[cross_idx]

    # ── BTC 侧价格口径（2026-08-14: TWAP-60 为市场结算基准）──
    if cfg.price_source == "twap":
        # 快照 TWAP 值缺失 → 否决（与 Go 引擎一致，不回退 Binance）
        price_at_cross = cross_snap.get("twap_price")
        open_price = cross_snap.get("twap_open") or event.get("twap_open_price")
        if not price_at_cross or not open_price:
            return None
    else:
        price_at_cross = cross_snap["price"]
        open_price = event["open_price"]

    hist_avg_range = event.get("hist_avg_range")

    # ── B1 背离硬要求（Formula B 核心）──
    # 双向过滤：divergence_floor 否决同向（div < floor）+ min_divergence 可选强度要求。
    # 历史振幅未就绪 → 无法计算背离度 → 丢弃（与 Go 引擎/实盘行为一致）
    if cfg.min_divergence > 0 or cfg.divergence_floor > -999:
        if hist_avg_range is None:
            return None
        btc_position = compute_btc_position(price_at_cross, open_price, hist_avg_range)
        btc_divergence = compute_btc_divergence(side, btc_position)
        if btc_divergence < cfg.divergence_floor:
            return None
        if cfg.min_divergence > 0 and btc_divergence < cfg.min_divergence:
            return None
    else:
        btc_position = compute_btc_position(price_at_cross, open_price,
                                            hist_avg_range or 0)
        btc_divergence = compute_btc_divergence(side, btc_position)

    # ── B2 振幅扩张 ──
    range_expansion = compute_range_expansion(price_at_cross, open_price,
                                              hist_avg_range)

    # F0: 振幅过大 — 一票否决 (BTC 大幅移动 = PM 是对的)
    if range_expansion is not None and range_expansion >= cfg.range_exp_max:
        return None

    # 入场价（对侧 bid，特征/纸面结算用；真实成交价见 fill_price）
    entry_price = cross_snap[other_key]

    # ── B3 确认（T+delay）──
    other_delta = compute_other_delta(snaps, cross_idx, other_key, cfg)

    # 确认数据不存在 (窗口末尾, remaining_sec 不足) → 与 Go 引擎一致，丢弃
    if other_delta is None:
        return None

    # other_delta 硬过滤下限 (默认禁用, 由评分权重处理)
    if other_delta < cfg.od_hard_filter:
        return None

    # ── 入场价上限 gate（ASK 口径，实盘对齐）──
    # 实盘 Trader 用最优卖价(=1-触发侧bid)校验 FAK (trader.go)，
    # 回测必须同口径。对侧 ask 超出 max_price → 盈亏比恶化 + FAK 被拒。
    fill_price = 1.0 - snaps[cross_idx + cfg.confirm_delay_ticks][this_key]
    if cfg.max_entry_price > 0 and fill_price > cfg.max_entry_price:
        return None

    # ── 评分（Formula B：B2 过度自信 + B3 确认回归）──
    score = 0

    # B3: 对面价格变化 — 三档评分
    if other_delta > cfg.other_delta_vstrong:
        score += cfg.w_other_d5_vstrong
    elif other_delta > cfg.other_delta_strong:
        score += cfg.w_other_d5_strong
    elif other_delta > cfg.other_delta_weak:
        score += cfg.w_other_d5_weak

    # B2: BTC 还没怎么动 (range_expansion < threshold) → PM 过度自信 → +2
    if range_expansion is not None and range_expansion < cfg.range_exp_threshold:
        score += cfg.w_range_expansion

    # ── 判断入场 ──
    if score < cfg.score_entry:
        return None

    shares = 2 if score >= cfg.score_add else 1

    # ── 判定输赢 ──
    # outcome_source="binance" 时用 binance_outcome（研究对照）；
    # 默认 "twap" 用 event.outcome（新数据为 TWAP 口径 = 市场真实结算）。
    # outcome=0 → UP 赢, outcome=1 → DOWN 赢
    if cfg.outcome_source == "binance" and "binance_outcome" in event:
        outcome = event["binance_outcome"]
    else:
        outcome = event["outcome"]
    if side == "yes":
        # YES>0.7, 我们买 NO (赌 DOWN)
        won = (outcome == 1)
    else:
        # NO>0.7, 我们买 YES (赌 UP)
        won = (outcome == 0)

    # 实盘对齐: 按确认时刻对侧 ASK 成交。
    # yes_price/no_price 存的是各订单簿 best bid，买对侧必须跨 spread 到
    # ask（双token互补: 对侧ask = 1 - 触发侧bid）。旧口径按对侧 bid 成交
    # 每笔系统性低估 ~1 分 spread。
    pnl = (1.0 - fill_price) * shares if won else -fill_price * shares

    return FlipSignal(
        event_time=event["start_time"],
        condition_id=event["condition_id"],
        side=side,
        score=score,
        entry_price=entry_price,
        fill_price=fill_price,
        won=won,
        pnl=pnl,
        shares=shares,
        remaining_sec=cross_snap["remaining_sec"],
        range_expansion=range_expansion,
        btc_position=btc_position,
        btc_divergence=btc_divergence,
        other_delta=other_delta,
    )


def check_signal(event: dict, side: str,
                 cfg: FlipBacktestConfig) -> FlipSignal | None:
    """对给定 side (yes/no) 检测 >0.7 穿越并评估信号。

    多穿越重试：遍历所有向上穿越 0.7 的上升沿，
    返回第一个评分通过的信号。一次事件最多产生一个信号（已下注不再观察）。
    """
    this_key = "yes_price" if side == "yes" else "no_price"
    snaps = event["snapshots"]

    was_above = False
    for i, s in enumerate(snaps):
        is_above = (s[this_key] > cfg.trigger_threshold
                    and s["remaining_sec"] < cfg.max_remaining_sec
                    and s["remaining_sec"] > cfg.min_remaining_sec)
        if is_above and not was_above and i >= cfg.min_pre_snaps:
            signal = _score_crossing(event, side, i, cfg)
            if signal is not None:
                return signal
        was_above = is_above
    return None


# ═══════════════════════════════════════════════════════════════
# 回测运行
# ═══════════════════════════════════════════════════════════════

def run_backtest(events: list[dict],
                 cfg: FlipBacktestConfig = None) -> tuple[list[FlipSignal], int, int]:
    """对全部事件运行回测，返回 (信号列表, 总事件数, 有效事件数)。

    每个事件最多产生一个信号（每事件一注）。
    按 snapshot 时间顺序扫描 YES/NO 两侧的穿越，先到先评，同 snapshot 内 YES 优先。
    这直接对应 Go Flip Engine 的实时行为（Watching 状态下同时检测两侧上升沿）。
    """
    if cfg is None:
        cfg = DEFAULT_CONFIG

    signals: list[FlipSignal] = []
    total = len(events)

    # Ensure hist_avg_range uses the configured window size & price source
    compute_hist_avg_range(events, window_N=cfg.hist_window_N,
                           source=cfg.price_source)
    active = sum(1 for e in events if e.get("hist_avg_range") is not None)

    for event in events:
        # Step 0: 跳过历史振幅不足的事件（B1 无法计算，与 Go 引擎一致）
        if event.get("hist_avg_range") is None:
            continue

        # Step 1: 按时间顺序扫描 — YES/NO 两侧同时检测，先到先评
        snaps = event["snapshots"]
        yes_was_above = False
        no_was_above = False

        signal = None
        for i, s in enumerate(snaps):
            # 两侧上升沿检测（与 Go engine ProcessSnapshot 完全一致）
            yes_is_above = (s["yes_price"] > cfg.trigger_threshold
                          and s["remaining_sec"] < cfg.max_remaining_sec
                          and s["remaining_sec"] > cfg.min_remaining_sec)
            no_is_above = (s["no_price"] > cfg.trigger_threshold
                         and s["remaining_sec"] < cfg.max_remaining_sec
                         and s["remaining_sec"] > cfg.min_remaining_sec)

            yes_rising = yes_is_above and not yes_was_above and i >= cfg.min_pre_snaps
            no_rising = no_is_above and not no_was_above and i >= cfg.min_pre_snaps

            yes_was_above = yes_is_above
            no_was_above = no_is_above

            # 同 snapshot 内 YES 优先（tie-breaking，匹配 Go engine）
            if yes_rising:
                signal = _score_crossing(event, "yes", i, cfg)
                if signal is not None:
                    break
            if no_rising:
                signal = _score_crossing(event, "no", i, cfg)
                if signal is not None:
                    break

        if signal is not None:
            signals.append(signal)

    return signals, total, active


def print_summary(signals: list[FlipSignal],
                  total_events: int = 0,
                  active_events: int = 0,
                  events: list[dict] | None = None) -> None:
    """打印回测汇总报告 (§3.3, §5)。"""
    if not signals:
        print("无信号。")
        return

    n = len(signals)
    wins = sum(1 for s in signals if s.won)
    total_pnl = sum(s.pnl for s in signals)
    avg_entry = sum(s.entry_price for s in signals) / n
    avg_fill = sum(s.fill_price for s in signals) / n
    avg_score = sum(s.score for s in signals) / n
    avg_shares = sum(s.shares for s in signals) / n
    avg_div = sum(s.btc_divergence for s in signals) / n

    # 从事件时间跨度估算天数
    if signals:
        t_min = min(s.event_time for s in signals)
        t_max = max(s.event_time for s in signals)
        days = max((t_max - t_min) / 86400, 1)
        daily_rate = n / days
    else:
        daily_rate = 0

    print("=" * 58)
    print("  Formula B 翻转信号回测 — 结果汇总")
    print("=" * 58)

    # 数据日期跨度
    from datetime import datetime, timezone
    if events:
        all_ts = [e["start_time"] for e in events if e.get("start_time")]
        if all_ts:
            ts_min, ts_max = min(all_ts), max(all_ts)
            date_min = datetime.fromtimestamp(ts_min, tz=timezone.utc).strftime("%m-%d")
            date_max = datetime.fromtimestamp(ts_max, tz=timezone.utc).strftime("%m-%d")
            data_days = max((ts_max - ts_min) / 86400, 1)
            print(f"  数据跨度:      {date_min} → {date_max} ({data_days:.0f} 天)")
    elif signals:
        t_min = min(s.event_time for s in signals)
        t_max = max(s.event_time for s in signals)
        date_min = datetime.fromtimestamp(t_min, tz=timezone.utc).strftime("%m-%d")
        date_max = datetime.fromtimestamp(t_max, tz=timezone.utc).strftime("%m-%d")
        print(f"  信号跨度:      {date_min} → {date_max}")

    if total_events:
        print(f"  总事件数:      {total_events:>5d}")
    if active_events:
        print(f"  有效事件:      {active_events:>5d}  (有历史振幅)")
    print(f"  总信号数:      {n:>5d}")
    print(f"  日均信号:      {daily_rate:>5.0f}")
    print(f"  胜率:          {wins / n * 100:>5.1f}%  ({wins}/{n})")
    print(f"  总 P&L:        {total_pnl:>+7.2f}  (按确认时刻对侧 ASK 成交)")
    print(f"  平均成交价:    {avg_fill:>7.3f}  (对侧 ASK = 1 - 触发侧 bid)")
    print(f"  平均穿越价:    {avg_entry:>7.3f}  (对侧 bid, 特征/纸面结算用)")
    print(f"  平均背离度:    {avg_div:>7.3f}  (正=BTC与PM反向)")
    print(f"  平均分数:      {avg_score:>7.1f}")
    print(f"  平均下单量:    {avg_shares:>7.1f} shares")
    losers_pnl = sum(s.pnl for s in signals if s.pnl < 0)
    gross_win = sum(s.pnl for s in signals if s.pnl > 0)
    n_win = sum(1 for s in signals if s.pnl > 0)
    n_loss = sum(1 for s in signals if s.pnl < 0)
    if losers_pnl < 0 and total_pnl > 0:
        print(f"  净盈亏比:      {total_pnl / abs(losers_pnl):>7.2f}  (净P&L/总亏损, 依赖胜率)")
    else:
        print(f"  净盈亏比:      N/A")
    if n_win > 0 and n_loss > 0 and gross_win > 0:
        avg_win = gross_win / n_win
        avg_loss = abs(losers_pnl) / n_loss
        print(f"  单笔盈亏比:    {avg_win / avg_loss:>7.2f}  (平均盈利/平均亏损)")
        print(f"  利润因子:      {gross_win / abs(losers_pnl):>7.2f}  (总盈利/总亏损)")
    print("-" * 58)

    # 按分数分桶（Formula B: score>=2 开仓，最高 5）
    for lo, hi, label in [(0, 2, "Score 0-1 (未触发)"),
                            (2, 4, "Score 2-3 (开仓)"),
                            (4, 99, "Score 4+  (叠加)")]:
        subset = [s for s in signals if lo <= s.score < hi]
        if subset:
            w = sum(1 for s in subset if s.won)
            p = sum(s.pnl for s in subset)
            avg_f = sum(s.fill_price for s in subset) / len(subset)
            print(f"  {label:22s}: n={len(subset):>3d}, "
                  f"win={w / len(subset) * 100:>5.1f}%, "
                  f"P&L={p:>+7.2f}, "
                  f"avg_fill={avg_f:.3f}")
    print("-" * 58)

    # 按特征分桶
    print("  特征分桶:")
    for label, pred in [
        ("对侧确认 (other_delta>0.01) ", lambda s: s.other_delta > 0.01),
        ("对侧未确认 (other_delta<=0) ", lambda s: s.other_delta <= 0),
        ("强背离 (div>=0.15)          ", lambda s: s.btc_divergence >= 0.15),
        ("弱背离 (0.05<=div<0.15)     ", lambda s: 0.05 <= s.btc_divergence < 0.15),
        ("过度自信 (range_exp<0.5)    ", lambda s: s.range_expansion is not None
                                                and s.range_expansion < 0.5),
    ]:
        subset = [s for s in signals if pred(s)]
        if subset:
            w = sum(1 for s in subset if s.won)
            p = sum(s.pnl for s in subset)
            print(f"  {label}: n={len(subset):>3d}, "
                  f"win={w / len(subset) * 100:>5.1f}%, "
                  f"P&L={p:>+7.2f}")
    print("=" * 58)


def export_signals_jsonl(signals: list[FlipSignal], path: str) -> None:
    """将信号导出为 JSONL 文件 (§5.3)。"""
    import json
    from dataclasses import asdict

    with open(path, "w") as f:
        for s in signals:
            d = asdict(s)
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
