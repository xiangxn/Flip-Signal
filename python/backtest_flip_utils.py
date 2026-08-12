#!/usr/bin/env python3
"""
回测工具函数 — 精简公式版翻转信号回测（Formula B, 2026-08-13 标定）。

特征提取 + 信号检测的纯函数，不依赖 IO。
所有函数均为无副作用纯函数，便于测试和调参。

Formula B 三个信号条件（详见 docs/flip_strategy_plan_2026-08-13.md）:
  B1 背离硬要求: 穿越时刻 BTC 必须与 PM 反向（min_divergence=0.05）
  B2 过度自信:   range_expansion < 0.5 → +2
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

def compute_hist_avg_range(events: list[dict], window_N: int = 18) -> None:
    """对每个事件计算前 N 根 K 线的平均振幅，原地注入 hist_avg_range。

    events 需按 start_time 升序排列。
    振幅 = close_price - open_price 的绝对值。
    不足 3 个历史事件时设为 None。
    """
    for i, event in enumerate(events):
        prev_ranges = []
        for j in range(max(0, i - window_N), i):
            r = abs(events[j]["close_price"] - events[j]["open_price"])
            prev_ranges.append(r)
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
# 特征提取 (T=0 实时特征)
# ═══════════════════════════════════════════════════════════════

def extract_pre_prices(snapshots: list[dict], cross_idx: int) -> list[float]:
    """提取穿越时刻之前 (含穿越时刻) 的所有 BTC 价格。"""
    return [s["price"] for s in snapshots[: cross_idx + 1]]


def compute_path_efficiency(pre_prices: list[float], open_price: float) -> float:
    """§2.2 路径效率 = |穿越时BTC价 - Open| / pre_range。

    ~1.0 = 单边趋势, ~0.0 = 来回振荡。
    """
    net_move = abs(pre_prices[-1] - open_price)
    pre_high = max(pre_prices)
    pre_low = min(pre_prices)
    pre_range = pre_high - pre_low
    if pre_range == 0:
        return 0.0
    return net_move / pre_range


def compute_pre_range(pre_prices: list[float]) -> tuple[float, float, float]:
    """返回 (pre_high, pre_low, pre_range)。"""
    pre_high = max(pre_prices)
    pre_low = min(pre_prices)
    return pre_high, pre_low, pre_high - pre_low


def compute_noise_ratio(pre_prices: list[float], net_move: float) -> float:
    """§2.3 噪声比 = total_path / net_move。

    total_path = Σ|p[i] - p[i-1]| (tick 级别累计路径长度)。
    """
    total_path = sum(
        abs(pre_prices[i] - pre_prices[i - 1])
        for i in range(1, len(pre_prices))
    )
    if net_move == 0:
        return total_path  # 纯震荡，净位移为 0
    return total_path / net_move


def compute_flips(pre_prices: list[float]) -> int:
    """§2.4 方向翻转次数。

    统计价格方向变化的次数 (忽略平盘 tick)。
    """
    flips = 0
    for i in range(2, len(pre_prices)):
        d1 = pre_prices[i - 1] - pre_prices[i - 2]
        d2 = pre_prices[i] - pre_prices[i - 1]
        if d1 != 0 and d2 != 0 and (d1 > 0) != (d2 > 0):
            flips += 1
    return flips


def is_oscillating(pre_prices: list[float], open_price: float,
                 cfg: FlipBacktestConfig,
                 path_eff: float | None = None,
                 noise_ratio: float | None = None,
                 flips: int | None = None) -> bool:
    """§2.5 来回振荡综合判定: 三个条件需同时满足。

    可传入预计算值避免重复遍历 pre_prices。
    """
    if path_eff is None:
        path_eff = compute_path_efficiency(pre_prices, open_price)
    if noise_ratio is None:
        net_move = abs(pre_prices[-1] - open_price)
        noise_ratio = compute_noise_ratio(pre_prices, net_move)
    if flips is None:
        flips = compute_flips(pre_prices)

    return (
        path_eff <= cfg.path_eff_oscillating
        and noise_ratio > cfg.noise_ratio_oscillating
        and flips > cfg.flips_oscillating
    )


def compute_range_expansion(price_at_cross: float, open_price: float,
                            hist_avg_range: float | None) -> float | None:
    """§2.6 振幅扩张 = abs(price - open) / hist_avg_range。

    Tick-independent — 不依赖采样频率。
    无历史数据时返回 None。
    """
    if hist_avg_range is None or hist_avg_range == 0:
        return None
    return abs(price_at_cross - open_price) / hist_avg_range


def compute_btc_position(price_at_cross: float, open_price: float,
                         hist_avg_range: float) -> float:
    """§2.8 BTC 位移 vs Open，以历史平均振幅为单位。

    btc_position = (price - open) / hist_avg_range

    Tick-independent — 不依赖采样频率，实盘流式数据同样适用。
    +1.0 = BTC 涨了 1x 历史振幅, -1.0 = BTC 跌了 1x 历史振幅。
    """
    if hist_avg_range == 0:
        return 0.0
    return (price_at_cross - open_price) / hist_avg_range


def compute_other_delta(snapshots: list[dict], cross_idx: int, other_key: str,
                        cfg: FlipBacktestConfig) -> float | None:
    """§2.7 对面价格 N 秒变化。

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
    score: int              # 复合评分
    entry_price: float      # 穿越时刻对侧 bid (评分特征用)
    fill_price: float       # 确认时刻对侧 ASK = 1 - 触发侧 bid（实盘真实成交价）
    won: bool               # 是否赢
    pnl: float              # 盈亏 (按 fill_price 成交计算)
    shares: int             # 下单量
    remaining_sec: int      # 入场时剩余秒数

    # 特征详情 (debug / 分析用)
    path_eff: float
    noise_ratio: float
    flips: int
    is_oscillating: bool
    range_expansion: float | None
    btc_position: float
    btc_extreme: bool
    btc_divergence: float   # 背离度：正=BTC 与 PM 反向（YES侧取 -btc_pos）
    other_delta: float


def _score_crossing(event: dict, side: str, cross_idx: int,
                    cfg: FlipBacktestConfig) -> FlipSignal | None:
    """对指定穿越点评分，返回 FlipSignal 或 None（评分不足/被否决）。

    从 check_signal 中抽取的纯评分逻辑 — 给定穿越点 cross_idx，
    提取特征、应用硬过滤、计算复合评分、判定输赢。

    Formula B 流程（2026-08-13）:
      窗口 → 确认tick存在 → B1 背离硬要求 → T=0 质量否决(可停用)
      → F0 真突破否决 → gate(ask口径) → 评分(B2+B3) → 判定输赢(ask成交)
    """
    this_key = "yes_price" if side == "yes" else "no_price"
    other_key = "no_price" if side == "yes" else "yes_price"
    snaps = event["snapshots"]
    cross_snap = snaps[cross_idx]
    pre_prices = extract_pre_prices(snaps, cross_idx)
    open_price = event["open_price"]

    # ── T=0 实时特征 ──

    net_move = abs(cross_snap["price"] - open_price)
    pre_high = max(pre_prices)
    pre_low = min(pre_prices)
    pre_range = pre_high - pre_low
    if pre_range == 0:
        return None

    path_eff = net_move / pre_range
    noise_ratio_val = compute_noise_ratio(pre_prices, net_move)
    flips_val = compute_flips(pre_prices)
    oscillating = is_oscillating(pre_prices, open_price, cfg,
                                 path_eff=path_eff,
                                 noise_ratio=noise_ratio_val,
                                 flips=flips_val)

    # ── T=0 质量否决（0 = 禁用；2026-08-13 减法后默认停用）──
    if cfg.noise_ratio_veto_max > 0 and noise_ratio_val > cfg.noise_ratio_veto_max:
        return None
    if cfg.path_eff_veto_min > 0 and path_eff < cfg.path_eff_veto_min:
        return None

    # 振幅扩张 (tick-independent: |price-open|/hist_avg_range)
    range_expansion = compute_range_expansion(cross_snap["price"], open_price,
                                              event.get("hist_avg_range"))

    # F0: 振幅过大 — 一票否决 (BTC 大幅移动 = PM 是对的)
    if range_expansion is not None and range_expansion >= cfg.range_exp_max:
        return None

    # BTC 位置 (tick-independent: vs Open, 以历史振幅为单位)
    btc_position = compute_btc_position(cross_snap["price"], open_price,
                                        event.get("hist_avg_range", 0))
    # 背离度：正 = BTC 与 PM 反向（YES侧触发取 -btc_pos，NO侧取 +btc_pos）
    btc_divergence = -btc_position if side == "yes" else btc_position

    # ── B1 背离硬要求（Formula B 核心，2026-08-13）──
    # 同向穿越 EV≈0（χ²=125 分桶），BTC 与 PM 反向才有 flip edge。
    # 阈值 0.03~0.10 为平台，0.05 为自然边界。
    if cfg.min_divergence > 0 and btc_divergence < cfg.min_divergence:
        return None

    # 兼容字段：btc_extreme（权重已停用，保留输出）
    if side == "yes":
        btc_extreme = (btc_position < cfg.btc_pos_min)
    else:
        btc_extreme = (btc_position > cfg.btc_pos_max)

    # 入场价（对侧 bid，评分特征用；真实成交价见 fill_price）
    entry_price = cross_snap[other_key]

    # ── T+delay 确认特征 ──
    other_delta = compute_other_delta(snaps, cross_idx, other_key, cfg)

    # 确认数据不存在 (窗口末尾, remaining_sec 不足) → 与 Go 引擎一致，丢弃
    if other_delta is None:
        return None

    # Hard filter: other_delta 下限 (默认禁用, 由评分权重处理)
    if other_delta < cfg.od_hard_filter:
        return None

    # ── 入场价上限 gate（ASK 口径，实盘对齐）──
    # 实盘 Trader 用最优卖价(=1-触发侧bid)校验 FAK (trader.go)，
    # 回测必须同口径。对侧 ask 超出 max_price → 盈亏比恶化 + FAK 被拒。
    fill_price = 1.0 - snaps[cross_idx + cfg.confirm_delay_ticks][this_key]
    if cfg.max_entry_price > 0 and fill_price > cfg.max_entry_price:
        return None

    # ── Step 4: 计算评分（Formula B：B2 过度自信 + B3 确认回归）──

    score = 0

    # B3: 对面价格变化 — 三档评分
    if other_delta > cfg.other_delta_vstrong:
        score += cfg.w_other_d5_vstrong
    elif other_delta > cfg.other_delta_strong:
        score += cfg.w_other_d5_strong
    elif other_delta > cfg.other_delta_weak:
        score += cfg.w_other_d5_weak

    # F3: 来回振荡（2026-08-13 起权重 0，字段保留）
    if oscillating:
        score += cfg.w_oscillating

    # F4/F5: 低价入场（2026-08-13 起权重 0，字段保留）
    if entry_price < cfg.entry_cheap_strong:
        score += cfg.w_cheap_entry_strong
    elif entry_price < cfg.entry_cheap_weak:
        score += cfg.w_cheap_entry_weak

    # B2: BTC 还没怎么动 (range_expansion < threshold) → PM 过度自信 → +2
    if range_expansion is not None and range_expansion < cfg.range_exp_threshold:
        score += cfg.w_range_expansion

    # F7: BTC 极端位置（2026-08-13 起权重 0，由 B1 硬过滤取代）
    if btc_extreme:
        score += cfg.w_btc_extreme

    # ── Step 5: 判断入场 ──
    if score < cfg.score_entry:
        return None

    shares = 2 if score >= cfg.score_add else 1

    # ── Step 6: 判定输赢 ──
    # outcome=0 → UP 赢, outcome=1 → DOWN 赢
    if side == "yes":
        # YES>0.7, 我们买 NO (赌 DOWN)
        won = (event["outcome"] == 1)
    else:
        # NO>0.7, 我们买 YES (赌 UP)
        won = (event["outcome"] == 0)

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
        path_eff=path_eff,
        noise_ratio=noise_ratio_val,
        flips=flips_val,
        is_oscillating=oscillating,
        range_expansion=range_expansion,
        btc_position=btc_position,
        btc_extreme=btc_extreme,
        btc_divergence=btc_divergence,
        other_delta=other_delta,
    )


def check_signal(event: dict, side: str,
                 cfg: FlipBacktestConfig) -> FlipSignal | None:
    """对给定 side (yes/no) 检测 >0.7 穿越并评估信号。

    这是核心信号检测函数，对应 §3.2 check_signal。

    当 allow_retry_crossings=True（默认）：遍历所有向上穿越 0.7 的上升沿，
    返回第一个评分通过的信号。一次事件最多产生一个信号（已下注不再观察）。

    当 allow_retry_crossings=False：仅检测第一次穿越（旧行为）。
    """
    this_key = "yes_price" if side == "yes" else "no_price"
    snaps = event["snapshots"]

    if cfg.allow_retry_crossings:
        # ── 多穿越模式: 遍历所有上升沿（从 ≤0.7 → >0.7），第一个评分通过的获胜 ──
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
    else:
        # ── 单穿越模式 (旧行为): 仅检测第一次 >0.7 ──
        cross_idx = None
        for i, s in enumerate(snaps):
            if s[this_key] > cfg.trigger_threshold and s["remaining_sec"] < cfg.max_remaining_sec and s["remaining_sec"] > cfg.min_remaining_sec:
                cross_idx = i
                break

        if cross_idx is None or cross_idx < cfg.min_pre_snaps:
            return None

        return _score_crossing(event, side, cross_idx, cfg)


# ═══════════════════════════════════════════════════════════════
# 回测运行
# ═══════════════════════════════════════════════════════════════

def run_backtest(events: list[dict],
                 cfg: FlipBacktestConfig = None) -> tuple[list[FlipSignal], int, int]:
    """对全部事件运行回测，返回 (信号列表, 总事件数, 有效事件数)。

    每个事件最多产生一个信号（每事件一注）。
    按 snapshot 时间顺序扫描 YES/NO 两侧的穿越，先到先评，同 snapshot 内 YES 优先。
    这直接对应 Go Flip Engine 的实时行为（Watching 状态下同时检测两侧上升沿）。

    多穿越重试由 cfg.allow_retry_crossings 控制（默认 True）。
    """
    if cfg is None:
        cfg = DEFAULT_CONFIG

    signals: list[FlipSignal] = []
    total = len(events)

    # Ensure hist_avg_range uses the configured window size
    compute_hist_avg_range(events, window_N=cfg.hist_window_N)
    active = sum(1 for e in events if e.get("hist_avg_range") is not None)

    for event in events:
        # Step 0: 跳过历史振幅不足的事件
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
            if cfg.allow_retry_crossings:
                if yes_rising:
                    signal = _score_crossing(event, "yes", i, cfg)
                    if signal is not None:
                        break
                if no_rising:
                    signal = _score_crossing(event, "no", i, cfg)
                    if signal is not None:
                        break
            else:
                # 兼容模式：仅处理两侧各自首次穿越（旧行为，与 Go compat mode 一致）
                if yes_rising:
                    signal = _score_crossing(event, "yes", i, cfg)
                    if signal is not None:
                        break
                    # 兼容模式下 YES 首次已尝试，后续 YES 不再触发
                elif no_rising:
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
    print(f"  平均穿越价:    {avg_entry:>7.3f}  (对侧 bid, 评分特征用)")
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
        ("来回振荡 (oscillating)      ", lambda s: s.is_oscillating),
        ("单边趋势 (trending)       ", lambda s: not s.is_oscillating),
        ("对面上升 (other_delta>0)  ", lambda s: s.other_delta > 0),
        ("对面下跌 (other_delta<=0) ", lambda s: s.other_delta <= 0),
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
