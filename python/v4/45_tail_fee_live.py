#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫尾盘实盘手续费核算（决策 #35 的落地依据 / 复核脚本）。

口径（Polymarket 官方, https://docs.polymarket.com/trading/fees.md, 2026-10-03 核对）:
    fee = C × feeRate × p × (1 − p)
    - 只有 **taker** 付费, maker 恒 0（Crypto 档 taker 0.07 / maker 0 / rebate 20%）;
    - 金额舍到 **5 位小数**, 最小 0.00001 USDC;
    - 对 p ↔ 1−p 对称。
本族只有两类成交计费:
    ① 买入: GTC 挂单里 **POST 响应就带着的那部分成交量**（穿过价差即时撮合 = taker）;
       此后挂在簿上被吃到的部分是 maker、0 费（FillTracker 定稿的累计量里两者混在一起,
       无法拆分——见下面的「老行回填」）。
    ② 止损卖出: 按快照 bid 挂的 FAK 立即卖出 = 恒 taker（exit_shares × exit_price）。

⚠️ 老行回填（与 Go 的 recorder.backfillFee 同一启发式）:
   决策 #35 之前的实盘行没有 taker_shares/fee 键。可恢复的只有「**即时全额成交**」这一种
   形态: `exec_status == filled` ∧ `exec_note` 为空（说明 POST 当场成交、没经过 GTC 挂单）。
   挂单定稿行（note 非空）的即时部分是 taker、余量是 maker, 行内只剩合计 —— 拆不出来,
   一律按 0 计（**系统性低估**, 方向是「少记」不是「多记」）。
   本脚本同时给出「**全部按 taker 计**」的上界, 真值落在两者之间、贴近下界。

用法:
    python/venv/bin/python python/v4/45_tail_fee_live.py [--dir data/tail-live] [--rate 0.07]
"""

import argparse
import glob
import json
import math
import os
import sys
from collections import defaultdict

# 官方对照表（100 股, Crypto rate=0.07, 5 位小数）——自检用, 不许动。
OFFICIAL_TABLE = {  # p → fee（USDC）
    0.01: 0.0693, 0.05: 0.3325, 0.10: 0.63, 0.20: 1.12,
    0.30: 1.47, 0.40: 1.68, 0.50: 1.75, 0.99: 0.0693,
}


def fee(rate: float, shares: float, price: float) -> float:
    """官方公式 + 5 位小数舍入（与 Go 的 tail.Fee 逐位一致, 半值远离零）。"""
    if rate <= 0 or shares <= 0 or price <= 0 or price >= 1:
        return 0.0
    f = shares * rate * price * (1 - price)
    return math.floor(f * 1e5 + 0.5) / 1e5


def selftest() -> None:
    for p, want in OFFICIAL_TABLE.items():
        got = fee(0.07, 100, p)
        if abs(got - want) > 1e-12:
            sys.exit(f"[自检] 官方表失配: p={p} got={got} want={want}")
    for p in (0.01, 0.13, 0.30, 0.47, 0.85, 0.99):
        if abs(fee(0.07, 37.5, p) - fee(0.07, 37.5, 1 - p)) > 1e-12:
            sys.exit(f"[自检] 对称性失配: p={p}")
    if fee(0.07, 0.0001, 0.5) != 0:
        sys.exit("[自检] 量子以下应舍为 0")
    print("[自检] 官方 100 股表 8 格 + 对称性 + 量子舍入 逐位通过\n")


def load(dirpath: str):
    for path in sorted(glob.glob(os.path.join(dirpath, "tail_*.jsonl"))):
        with open(path, encoding="utf-8") as f:
            for ln, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield path, ln, json.loads(line)
                except json.JSONDecodeError as e:
                    sys.exit(f"{path}:{ln} JSON 解析失败: {e}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="data/tail-live")
    ap.add_argument("--rate", type=float, default=0.07)
    args = ap.parse_args()

    selftest()

    per_day = defaultdict(lambda: {"buy": 0.0, "sell": 0.0, "buy_ub": 0.0,
                                   "n_buy": 0, "n_buy_legacy": 0, "n_sell": 0,
                                   "pnl": 0.0})
    unknown_rows = 0
    for _path, _ln, r in load(args.dir):
        day = r.get("date") or "?"
        d = per_day[day]
        if r.get("pnl"):
            d["pnl"] += r["pnl"]

        st = r.get("exec_status") or ""
        sh, px = r.get("shares") or 0, r.get("avg_fill_price") or 0
        if st in ("filled", "partial") and sh > 0 and px > 0:
            # 上界: 全部成交股数按 taker 计。
            d["buy_ub"] += fee(args.rate, sh, px)
            # 回填启发式: 只有「即时全额成交」（filled ∧ note 空）可恢复。
            if st == "filled" and not r.get("exec_note"):
                d["buy"] += fee(args.rate, sh, px)
                d["n_buy"] += 1
            else:
                d["n_buy_legacy"] += 1
        elif st == "resting":
            unknown_rows += 1

        exs, exp = r.get("exit_shares") or 0, r.get("exit_price") or 0
        if exs > 0 and exp > 0:
            d["sell"] += fee(args.rate, exs, exp)
            d["n_sell"] += 1

    tot = defaultdict(float)
    print(f"{'日期':<12}{'全额成交':>6}{'挂单定稿':>7}{'卖出':>5}"
          f"{'买费(可恢复)':>12}{'卖费':>9}{'合计':>9}{'上界(全taker)':>13}")
    print("-" * 76)
    for day in sorted(per_day):
        d = per_day[day]
        total = d["buy"] + d["sell"]
        print(f"{day:<12}{d['n_buy']:>6}{d['n_buy_legacy']:>7}{d['n_sell']:>5}"
              f"{d['buy']:>12.5f}{d['sell']:>9.5f}{total:>9.5f}{d['buy_ub'] + d['sell']:>13.5f}")
        for k in ("buy", "sell", "buy_ub", "n_buy", "n_buy_legacy", "n_sell", "pnl"):
            tot[k] += d[k]
    print("-" * 76)
    grand = tot["buy"] + tot["sell"]
    print(f"{'合计':<12}{int(tot['n_buy']):>6}{int(tot['n_buy_legacy']):>7}"
          f"{int(tot['n_sell']):>5}{tot['buy']:>12.5f}{tot['sell']:>9.5f}"
          f"{grand:>9.5f}{tot['buy_ub'] + tot['sell']:>13.5f}")
    print()
    print(f"落盘 P&L 合计（**未扣费**, 旧口径）: {tot['pnl']:+.4f}U")
    print(f"扣费后（可恢复口径）            : {tot['pnl'] - grand:+.4f}U"
          f"（费 {grand:.5f}U, 占已实现 |P&L| 的 "
          f"{100 * grand / abs(tot['pnl']):.2f}% )")
    print(f"扣费后（全 taker 上界）          : {tot['pnl'] - (tot['buy_ub'] + tot['sell']):+.4f}U")
    print()
    print(f"⚠️ 挂单定稿行（note 非空, 即时部分不可恢复, 按 0 计）: {int(tot['n_buy_legacy'])} 行")
    print(f"⚠️ 仍处 resting 的行（未定稿, 理论上不该出现在收线的日文件里）: {unknown_rows} 行")
    if tot["buy"] > 0:
        print(f"📊 低估幅度: {100 * (1 - tot['buy'] / tot['buy_ub']):.2f}%"
              f"（可恢复 {tot['buy']:.5f}U vs 上界 {tot['buy_ub']:.5f}U）")


if __name__ == "__main__":
    main()
