#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Polymarket BTC 5m 顺势逻辑回测 v4
- 官方 Crypto Taker 手续费公式
- 滑点
- 入场价格过滤
- 最大回撤 / 连亏统计
- 支持整个目录批量回测
"""

import json
import numpy as np
from pathlib import Path
from typing import List, Dict
import argparse


# ====================== 可调参数 ======================
CHEAP_THRESHOLD = 0.30
MARGIN = 40.0
MIN_REMAINING = 20
MAX_REMAINING = 90
VELOCITY_WINDOW = 8
VELOCITY_HALFLIFE = 15.0
MIN_ABS_VELOCITY = 1.0
ONE_SIGNAL_PER_MARKET = True

MAX_ENTRY_PRICE = 0.92          # 入场价高于此值不交易
SLIPPAGE = 0.005                # 预估滑点（可调整）

UNIT_SIZE = 2.0                 # 每注 2U
# =====================================================


def estimate_velocity(prices: List[float], timestamps: List[int], window: int = 8) -> float:
    if len(prices) < 3:
        return 0.0
    n = min(window, len(prices))
    p = np.array(prices[-n:], dtype=float)
    t = np.array(timestamps[-n:], dtype=float) / 1000.0
    t = t - t[0]
    if t[-1] < 0.5:
        return 0.0
    return float(np.polyfit(t, p, 1)[0])


def predict_twap_decay(R: int, price_history: List[float], current_price: float,
                       v: float, halflife: float = 15.0) -> float:
    if R <= 0:
        return current_price
    tau = max(halflife / np.log(2), 1.0)
    n = len(price_history)
    total = 0.0
    count = 0
    for k in range(R - 59, R + 1):
        if k <= 0:
            idx = n - 1 + k
            total += price_history[idx] if 0 <= idx < n else current_price
        else:
            displacement = v * tau * (1.0 - np.exp(-k / tau))
            total += current_price + displacement
        count += 1
    return total / max(count, 1)


def process_market(market: Dict) -> List[Dict]:
    ticks = market.get("ticks", [])
    if len(ticks) < 40:
        return []

    open_price = market["twap_open_price"]
    actual_close = market["twap_close_price"]
    actual_up = actual_close > open_price

    price_history = []
    ts_history = []
    signals = []

    for tick in ticks:
        rem = tick["rem"]
        if not (MIN_REMAINING <= rem <= MAX_REMAINING):
            continue

        bin_data = tick["bin"]
        pm = tick["pm"]
        price = bin_data["price"]
        ts = tick["ts"]

        price_history.append(price)
        ts_history.append(ts)

        v = estimate_velocity(price_history, ts_history, VELOCITY_WINDOW)
        if abs(v) < MIN_ABS_VELOCITY:
            continue

        twap_pred = predict_twap_decay(rem, price_history, price, v, VELOCITY_HALFLIFE)
        d_pred = twap_pred - open_price

        yes_ask = pm.get("yes_ask", 1.0)
        no_ask = pm.get("no_ask", 1.0)

        side = None
        entry_price = None

        # 顺势逻辑
        if yes_ask <= CHEAP_THRESHOLD and d_pred < -MARGIN:
            side = "NO"
            entry_price = no_ask
        elif no_ask <= CHEAP_THRESHOLD and d_pred > MARGIN:
            side = "YES"
            entry_price = yes_ask

        if side is None:
            continue

        # 入场价格过滤
        if entry_price > MAX_ENTRY_PRICE:
            continue

        correct = (side == "YES" and actual_up) or (side == "NO" and not actual_up)

        # ---------- 官方 Crypto 手续费计算 ----------
        # 实际成交价（含滑点）
        effective_price = min(entry_price + SLIPPAGE, 0.99)

        # 官方公式：fee = C * 0.07 * p * (1-p)
        # 我们按投入 1U 计算：
        # shares = 1 / effective_price
        # fee = shares * 0.07 * effective_price * (1 - effective_price)
        # 简化后：
        fee_1u = 0.07 * (1.0 - effective_price)

        if correct:
            # 赢了：获得 1，成本 = effective_price + fee
            pnl_1u = 1.0 - effective_price - fee_1u
        else:
            # 输了
            pnl_1u = -(effective_price + fee_1u)

        signals.append({
            "rem": rem,
            "side": side,
            "v": round(v, 2),
            "d_pred": round(d_pred, 1),
            "entry_price": round(entry_price, 3),
            "effective_price": round(effective_price, 3),
            "fee_1u": round(fee_1u, 5),
            "correct": correct,
            "abs_d": abs(d_pred),
            "pnl_1u": pnl_1u
        })

    if ONE_SIGNAL_PER_MARKET and signals:
        signals = [max(signals, key=lambda x: x["abs_d"])]

    return signals


def calculate_drawdown(pnl_list: List[float]):
    if not pnl_list:
        return 0.0, 0, 0.0

    cumulative = np.cumsum(pnl_list)
    peak = np.maximum.accumulate(cumulative)
    drawdown = peak - cumulative
    max_dd = float(np.max(drawdown)) if len(drawdown) > 0 else 0.0

    max_consec_losses = 0
    current_consec = 0
    max_loss_amount = 0.0
    current_loss_amount = 0.0

    for pnl in pnl_list:
        if pnl < 0:
            current_consec += 1
            current_loss_amount += pnl
            max_consec_losses = max(max_consec_losses, current_consec)
            max_loss_amount = min(max_loss_amount, current_loss_amount)
        else:
            current_consec = 0
            current_loss_amount = 0.0

    return max_dd, max_consec_losses, max_loss_amount


def main(data_dir: str):
    data_path = Path(data_dir)
    if not data_path.exists():
        print(f"目录不存在: {data_dir}")
        return

    files = sorted(data_path.glob("events_*.jsonl"))
    if not files:
        print("未找到 events_*.jsonl 文件")
        return

    print(f"找到 {len(files)} 个数据文件，开始顺势逻辑回测 v4（官方手续费）...\n")
    print(f"参数: MAX_ENTRY_PRICE={MAX_ENTRY_PRICE}, SLIPPAGE={SLIPPAGE}, "
          f"MAX_REMAINING={MAX_REMAINING}, MARGIN={MARGIN}\n")

    all_signals = []
    total_markets = 0

    for file in files:
        day = file.stem.replace("events_", "")
        day_signals = []
        day_markets = 0

        with open(file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    market = json.loads(line)
                    day_markets += 1
                    total_markets += 1
                    sigs = process_market(market)
                    for s in sigs:
                        s["date"] = day
                    day_signals.extend(sigs)
                    all_signals.extend(sigs)
                except Exception:
                    continue

        if day_signals:
            wr = np.mean([s["correct"] for s in day_signals]) * 100
            day_pnl = sum(s["pnl_1u"] for s in day_signals) * UNIT_SIZE
            print(f"{day}: 市场 {day_markets:3d} | 信号 {len(day_signals):3d} | "
                  f"胜率 {wr:5.1f}% | 收益 {day_pnl:+.2f}U")
        else:
            print(f"{day}: 市场 {day_markets:3d} | 信号   0")

    # ====================== 汇总 ======================
    print("\n" + "="*70)
    print("顺势逻辑回测汇总 v4（已计入官方 Crypto 手续费）")
    print("="*70)
    print(f"总市场数:               {total_markets}")
    print(f"总信号数:               {len(all_signals)}")

    if not all_signals:
        print("没有产生任何信号，请放宽过滤条件。")
        return

    win_rate = np.mean([s["correct"] for s in all_signals]) * 100
    avg_entry = np.mean([s["entry_price"] for s in all_signals])
    avg_effective = np.mean([s["effective_price"] for s in all_signals])
    avg_fee = np.mean([s["fee_1u"] for s in all_signals])

    ev_1u = np.mean([s["pnl_1u"] for s in all_signals])
    total_pnl = sum(s["pnl_1u"] for s in all_signals) * UNIT_SIZE
    total_staked = len(all_signals) * UNIT_SIZE
    roi = (total_pnl / total_staked) * 100 if total_staked > 0 else 0.0

    pnl_sequence = [s["pnl_1u"] * UNIT_SIZE for s in all_signals]
    max_dd, max_consec, max_loss_amt = calculate_drawdown(pnl_sequence)

    print(f"总胜率:                 {win_rate:.2f}%")
    print(f"平均入场价:             {avg_entry:.3f}")
    print(f"平均有效成交价:         {avg_effective:.3f} (含滑点)")
    print(f"平均手续费 (1U):        {avg_fee:.5f}")
    print(f"单笔 EV (1U):           {ev_1u:+.4f}")
    print(f"单笔 EV (2U):           {ev_1u * UNIT_SIZE:+.4f}")
    print(f"总收益 (2U/注):         {total_pnl:+.2f}U")
    print(f"总投入:                 {total_staked:.1f}U")
    print(f"ROI:                    {roi:+.2f}%")
    print(f"最大回撤:               {max_dd:.2f}U")
    print(f"最大连亏次数:           {max_consec}")
    print(f"最大连亏金额:           {max_loss_amt:.2f}U")

    print("\n按剩余时间分桶:")
    for lo, hi in [(20, 40), (40, 60), (60, 90), (90, 120)]:
        sub = [s for s in all_signals if lo <= s["rem"] < hi]
        if sub:
            wr = np.mean([s["correct"] for s in sub]) * 100
            avg_p = np.mean([s["entry_price"] for s in sub])
            ev = np.mean([s["pnl_1u"] for s in sub])
            pnl = sum(s["pnl_1u"] for s in sub) * UNIT_SIZE
            print(f"  {lo:3d}-{hi:3d}s: 胜率 {wr:5.1f}% | 入场 {avg_p:.3f} | "
                  f"EV {ev:+.4f} | 收益 {pnl:+.1f}U | n={len(sub)}")

    print("\n按 |D_pred| 分桶:")
    for thr in [20, 40, 60, 100]:
        sub = [s for s in all_signals if s["abs_d"] >= thr]
        if sub:
            wr = np.mean([s["correct"] for s in sub]) * 100
            ev = np.mean([s["pnl_1u"] for s in sub])
            pnl = sum(s["pnl_1u"] for s in sub) * UNIT_SIZE
            print(f"  |D| ≥ {thr:3d}: 胜率 {wr:5.1f}% | EV {ev:+.4f} | "
                  f"收益 {pnl:+.1f}U | n={len(sub)}")

    # 保存信号
    out_path = data_path / "signals_trend_v4.jsonl"
    with open(out_path, "w", encoding="utf-8") as f:
        for s in all_signals:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    print(f"\n信号已保存至: {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("data_dir", type=str, help="数据目录路径")
    args = parser.parse_args()
    main(args.data_dir)