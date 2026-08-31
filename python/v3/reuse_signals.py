#!/usr/bin/env python3
"""
纸面信号 → 回测口径映射与复验（09-15 数据复验用）。

将 Go 引擎纸面信号 JSONL（-output 目录下的 crosses_*.jsonl）映射为与
回测 trades_v3.csv 同口径的明细，输出 data/v3/trades_paper_v3.csv，
并打印纸面样本 vs 回测基准（14 天）的对比摘要。

口径映射（与 CLAUDE.md / paper_plan §3.2 一致）:
  side      up/down           → yes/no（回测 csv 口径）
  fill      fill_comp（互补价 1-触发侧bid@+10s）→ fill（回测口径 flip_fill10s）
  fill_ask  对侧真实 ask@+10s  → fill_ask（纸面/实盘口径，对照保留）
  won       *bool             → flip_won（1/0；待结算留空不参与统计）
  date      UTC 日             → date（与回测一致）
  pnl       USDC              → pnl（结算回填后才有值）

用法:
  python3 reuse_signals.py [--dir ../data/v3] [--stake 2]
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
CSV_COLS = ["date", "event_start", "side", "trigger_bid", "post_end",
            "fill", "shares", "flip_won", "pnl", "cls"]
EXTRA_COLS = ["ts", "rem", "fill_ask", "condition_id"]


def wilson(n, k, z=1.96):
    """Wilson 95% CI（与 06_backtest.wilson 同公式）。"""
    if n == 0:
        return 0.0, 0.0
    p = k / n
    denom = 1 + z * z / n
    c = (p + z * z / (2 * n)) / denom
    m = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return c - m, c + m


def load_paper(dirpath):
    """读取纸面 JSONL（ok=true 全部行，含待结算），映射为回测口径 df。"""
    rows = []
    for p in sorted(Path(dirpath).glob("crosses_*.jsonl")):
        with open(p) as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                except json.JSONDecodeError:
                    continue  # 损坏行跳过（recorder 恢复时同样跳过）
                if not r.get("ok"):
                    continue
                rows.append({
                    "date": r.get("date"),
                    "event_start": r.get("event_start"),
                    "side": "yes" if r.get("side") == "up" else "no",
                    "trigger_bid": r.get("trigger_bid"),
                    "post_end": r.get("post_end"),
                    "fill": r.get("fill_comp"),   # 回测口径（flip_fill10s）
                    "shares": r.get("shares"),
                    "flip_won": r.get("won"),     # None = 待结算
                    "pnl": r.get("pnl"),          # None = 待结算
                    "cls": r.get("cls"),
                    "ts": r.get("ts"),
                    "rem": r.get("rem"),
                    "fill_ask": r.get("fill"),    # 纸面/实盘口径（对照）
                    "condition_id": r.get("condition_id"),
                })
    df = pd.DataFrame(rows, columns=CSV_COLS + EXTRA_COLS)
    df["flip_won"] = df["flip_won"].astype("object")  # bool/None 混合列
    return df


def show(title, df, stake):
    """与 06_backtest.show 同格式的摘要（仅统计已结算样本）。"""
    done = df[df["flip_won"].notna()]
    pending = len(df) - len(done)
    if done.empty:
        print(f"{title}: 无已结算信号（待结算 {pending}）")
        return
    n = len(done)
    wr = done["flip_won"].astype(float).mean()
    fill = done["fill"].mean()
    ev_share = wr - fill
    total = done["pnl"].sum()
    cum = done["pnl"].cumsum()
    max_dd = float((cum.cummax() - cum).max())
    day_ev_pos = int(done.groupby("date")["pnl"].sum().gt(0).sum())
    day_n = done["date"].nunique()
    lo, hi = wilson(n, int(round(n * wr)))
    print(f"\n{'=' * 66}")
    print(f"  {title}")
    print(f"{'=' * 66}")
    print(f"  信号数        {n:>4d}  （待结算 {pending}）")
    print(f"  胜率          {wr * 100:>5.1f}%   Wilson 95% CI [{lo * 100:.1f}%, {hi * 100:.1f}%]")
    if "fill_ask" in done.columns:
        print(f"  平均 fill     {fill:.3f}   (fill_ask {done['fill_ask'].mean():.3f})")
    else:
        print(f"  平均 fill     {fill:.3f}")
    print(f"  EV/股         {ev_share:+.4f}")
    print(f"  EV/笔（{stake:.0f}U）   {done['pnl'].mean():+.4f} USDC")
    print(f"  总 P&L        {total:+.2f} USDC")
    print(f"  逐日盈利      {day_ev_pos}/{day_n} 天")
    print(f"  最大回撤      {max_dd:.2f} USDC")


def main():
    ap = argparse.ArgumentParser(description="纸面信号 → 回测口径映射与复验")
    ap.add_argument("--dir", default=str(BASE / "data"),
                    help="纸面 JSONL 目录（引擎 -output 参数，默认 python/v3/data）")
    ap.add_argument("--stake", type=float, default=2.0, help="每笔投入 USDC（默认 2）")
    args = ap.parse_args()

    paper = load_paper(args.dir)
    if paper.empty:
        print(f"⚠️  目录 {args.dir} 无 ok=true 信号行（引擎尚未产出或目录不对）")
        sys.exit(1)

    # 明细落盘（与 trades_v3.csv 同列，可直接 diff）
    out = BASE / "data" / "trades_paper_v3.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    paper[CSV_COLS + EXTRA_COLS].to_csv(out, index=False)
    print(f"✅ 明细已写 {out}（{len(paper)} 条，含待结算）")
    print(f"数据: {args.dir}  |  纸面信号 {len(paper)}  |  "
          f"{paper['date'].min()} ~ {paper['date'].max()}")

    # 纸面 vs 回测基准对比
    bt = BASE / "data" / "trades_v3.csv"
    if bt.exists():
        bench = pd.read_csv(bt, dtype={"side": str})
        print(f"回测基准: {bt}（{len(bench)} 笔，08-18~08-30 样本内 OOS 复核详见 06_backtest）")
        bench_done = bench[bench["flip_won"].notna()]
        if not bench_done.empty:
            show(f"回测基准（{len(bench_done)} 天样本）", bench_done, args.stake)
    show(f"纸面样本（复验用）", paper, args.stake)


if __name__ == "__main__":
    main()
