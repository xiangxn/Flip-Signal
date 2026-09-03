#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v4 狗@0.2 R1 家族回测（2 USDC/笔）—— 急跌进 0.2 × 浅洞 × 前 2 分钟（m_45 窗口版）。

规则（发现过程见 /tmp/flipdog/15~18, 2026-09-02 定稿）:
  触发    每事件首个某侧 ask ≤ 0.20 的 tick（1s 分辨率, book_latency≤300ms）
  急跌腿  触发前 45s 窗口（tick 序号 [i-45, i-1], 同侧 ask>0 且有效）内**曾** ≥ 0.40
          （m_45 = 窗口内 max ask; 原「恰 30s 前」点口径 n 太小, 窗口口径信号 ×2~3）
  浅洞腿  dist_s ∈ (-0.5, 0): Binance spot 距锚(twap_open) ≤0.5σ 但未过锚（快变量, 主判定）
  时间腿  rem > 180（窗口前 ~2 分钟）
  双层版  在纯现货版之上加 dist_t ∈ (-0.5, 0): 当前 Chainlink TWAP-60 同样浅
          （慢变量确认「真浅洞」; 单独用 TWAP 无 alpha, 见 16_r1_twap.py）
  dist 定义: dist = sgn·(price − anchor)/anchor·1e4 / hist_bps
             sgn: dog=yes +1 / dog=no −1（>0 = 现货已在狗赢侧）
             hist_bps = 前 ≤18 窗 |tw_close−tw_open| 均值（≥3 窗可用）
收益口径: 2U/注, shares = 2/fill（fill = 触发 ask）; 赢 → shares−2; 输 → −2。
          官方 outcome 结算（0=Up 1=Down）。EV/股 = WR − fill。

本脚本: 自包含提取 + 两个规则回测（纯现货 / 双层）, 输出摘要、h1/h2、
      逐日 P&L、逐日明细 CSV（供 09-15 及未来数据 OOS 复算）。
参考基线（08-18~08-31, 14 天）:
  m_45 纯现货  n=245 WR 29.0% EV +1.078U/注 P&L +264U  日正 12/14
  m_45 双层    n=197 WR 30.5% EV +1.234U/注 P&L +243U
用法: python 01_backtest_r1.py [--data <事件目录>] [--stake 2]
"""
import argparse
import sys
from math import sqrt
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))
from v2.lib import load_events  # noqa: E402

STAKE = 2.0
MAX_LAT = 300          # pm book_latency_ms 上限
CRASH_WINDOW = 45      # 急跌窗口（tick）
CRASH_MIN = 0.40       # 窗口内曾 ≥ θ
BAND = (-0.5, 0.0)     # 浅洞带
REM_MIN = 180          # 时间腿
LOOKS = (20, 30, 45, 60)  # 顺带记录的窗口, 供子版本复算


def hist_ranges(events):
    """前 ≤18 窗 |tw_close−tw_open| 均值（σ, 需 ≥3 窗）; 每事件 start_time → σ。"""
    hr = {}
    for i, e in enumerate(events):
        prev = []
        for j in range(max(0, i - 18), i):
            o, c = events[j].get("twap_open_price"), events[j].get("twap_close_price")
            if o and c:
                prev.append(abs(c - o))
        hr[e["start_time"]] = sum(prev) / len(prev) if len(prev) >= 3 else None
    return hr


def extract(data_dir):
    """每事件首个某侧 ask≤0.20 的观测 → DataFrame（决策时刻信息 + 结算标签）。"""
    events = load_events(data_dir)
    hr = hist_ranges(events)
    rows = []
    for e in events:
        outcome = e.get("outcome")
        anchor = e.get("twap_open_price")
        if outcome is None or not anchor:
            continue
        ticks = e.get("ticks") or []
        h = hr.get(e["start_time"])
        hist_bps = (h / anchor * 1e4) if h else None
        trig = None
        for i, t in enumerate(ticks):
            pm = t.get("pm") or {}
            if not pm or (pm.get("book_latency_ms") or 0) > MAX_LAT:
                continue
            # 整簿快照门控（镜像 Go 引擎，2026-09-03 由 UP-only 扩为对称四字段）：
            # UP/DOWN 两侧 bid/ask 报价齐全才有效。实测报价缺失为整行全空（两侧
            # 同秒为 0，14 天 3.04%），无单侧缺失态 → 该门控与旧版历史结果全等。
            if not all((pm.get(k) or 0) > 0
                       for k in ("yes_bid", "yes_ask", "no_bid", "no_ask")):
                continue
            if (pm.get("yes_ask") or 1) <= 0.20 or (pm.get("no_ask") or 1) <= 0.20:
                trig = (i, t, pm)
                break
        if trig is None:
            continue
        i, t, pm = trig
        rem = t.get("rem")
        if rem is None:
            continue
        ya = (pm.get("yes_ask") or 1)
        na = (pm.get("no_ask") or 1)
        if ya <= 0.20 and na <= 0.20:
            # 交叉态（14 天历史 0 次）：狗侧 = sgn·(spot−anchor)<0 一侧（浅洞带
            # 可能成立侧），镜像 Go 引擎；spot/锚不可判时退回 yes。
            spot = (t.get("bin") or {}).get("price")
            dog = "no" if (spot or 0) > anchor else "yes"
        elif ya <= 0.20:
            dog = "yes"
        else:
            dog = "no"
        sgn = 1.0 if dog == "yes" else -1.0
        won = 1 if ((outcome == 0) if dog == "yes" else (outcome == 1)) else 0
        r = {"event_start": e["start_time"], "side": dog, "rem": rem,
             "fill": (pm.get(f"{dog}_ask") or 0), "settle_won": won}
        if hist_bps:
            spot = (t.get("bin") or {}).get("price")
            tw = t.get("twap") or {}
            twp = tw.get("price")
            if spot:
                r["dist_s"] = sgn * (spot - anchor) / anchor * 1e4 / hist_bps
            if twp:
                r["dist_t"] = sgn * (twp - anchor) / anchor * 1e4 / hist_bps
        # 回看窗口: [i−τ, i−1] 内同侧有效 ask 的 max（tick 序号近似秒）
        pre = []
        for j in range(max(0, i - LOOKS[-1]), i):
            pj = ticks[j].get("pm") or {}
            if not pj or (pj.get("book_latency_ms") or 0) > MAX_LAT:
                continue
            a = pj.get(f"{dog}_ask") or 0
            if a > 0:
                pre.append(i - j)   # dt
                pre.append(a)
        if pre:
            dt = np.array(pre[0::2])
            ak = np.array(pre[1::2])
            for tau in LOOKS:
                sel = ak[dt <= tau]
                if len(sel):
                    r[f"m_{tau}"] = sel.max()
        rows.append(r)
    df = pd.DataFrame(rows)
    if len(df) == 0:
        return df
    df["date"] = pd.to_datetime(df["event_start"], unit="s").dt.date.astype(str)
    # 半样本: 按时序把日期切成两半（14 天窗口下 = 08-18~24 / 08-25~31）
    dpos = pd.factorize(df["date"])[0]
    half = len(np.unique(df["date"])) // 2
    df["h"] = np.where(dpos < half, "h1", "h2")
    return df


def shallow(v):
    return (v > BAND[0]) & (v < BAND[1])


def rules(df):
    """两个规则 → 布尔掩码。m_45 缺失(NaN)/dist 缺失 → 不入选。"""
    crash = df["m_45"] >= CRASH_MIN
    pure = crash & shallow(df["dist_s"]) & (df["rem"] > REM_MIN)
    dual = pure & shallow(df["dist_t"])
    return pure, dual


def wilson(k, n, z=1.96):
    if n == 0:
        return (0, 0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    w = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - w) / d, (c + w) / d)


def report(name, s, ndays):
    """规则摘要: 信号量/胜率/收益/两半/日正 + 逐日 P&L。"""
    if len(s) == 0:
        print(f"{name}: n=0\n")
        return
    k = int(s.settle_won.sum())
    lo, hi = wilson(k, len(s))
    f = s.fill.mean()
    shares = STAKE / s["fill"]
    pl = np.where(s["settle_won"] == 1, shares - STAKE, -STAKE)
    day_pl = lambda g: np.where(  # noqa: E731
        g["settle_won"] == 1, STAKE / g["fill"] - STAKE, -STAKE).sum()
    nday_pos = int(sum(day_pl(g) > 0 for _, g in s.groupby("date")))
    print(f"{name}")
    print(f"  信号量: n={len(s)}（{len(s)/ndays:.1f}/日）  side {s.side.value_counts().to_dict()}")
    print(f"  胜率:   {k/len(s)*100:.1f}% [{lo*100:.1f},{hi*100:.1f}]  fill {f:.3f}  "
          f"(盈亏平衡 {f*100:.1f}%)")
    print(f"  收益:   EV {k/len(s)-f:+.4f}/股 ≈ {pl.mean():+.3f}U/注  P&L {pl.sum():+.0f}U/"
          f"{ndays}天 (≈ {pl.sum()/ndays:+.1f}U/日)  日正 {nday_pos}/{ndays}")
    for h, nm in (("h1", "前半"), ("h2", "后半")):
        g = s[s["h"] == h]
        if len(g):
            plg = np.where(g["settle_won"] == 1, STAKE / g["fill"] - STAKE, -STAKE)
            print(f"    {nm} {h}: n={len(g):3d}  WR {g.settle_won.mean()*100:5.1f}%  "
                  f"EV {plg.mean():+.3f}U/注  P&L {plg.sum():+6.1f}U")
    print("  逐日:")
    for d, g in sorted(s.groupby("date")):
        plg = np.where(g["settle_won"] == 1, STAKE / g["fill"] - STAKE, -STAKE)
        print(f"    {d}: n={len(g):3d}  WR {g.settle_won.mean()*100:5.1f}%  "
              f"P&L {plg.sum():+6.1f}U")
    print()


def main():
    global STAKE
    ap = argparse.ArgumentParser(description="v4 狗@0.2 R1(m_45) 回测")
    ap.add_argument("--data", default=str(
        BASE.parent.parent / "data" / "btc"), help="data/btc 事件目录")
    ap.add_argument("--stake", type=float, default=STAKE)
    ap.add_argument("--csv", default=str(BASE / "data" / "trades_r1.csv"),
                    help="明细 CSV 输出路径")
    args = ap.parse_args()
    STAKE = args.stake

    df = extract(args.data)
    print(f"数据 {args.data}: 事件 → 0.2 首触 {len(df)}\n")
    pure, dual = rules(df)
    ndays = df["date"].nunique()
    for name, mask in (("R1 m_45 纯现货", pure), ("R1 m_45 双层", dual)):
        report(name, df[mask], ndays)

    # 导出逐笔明细（双规则用 in_pure/in_dual 标记, 供 OOS/子版本复算）
    out = df[pure | dual].copy()
    out["in_pure"] = pure[pure | dual]
    out["in_dual"] = dual[pure | dual]
    cols = ["date", "event_start", "side", "rem", "fill", "m_20", "m_30", "m_45",
            "dist_s", "dist_t", "in_pure", "in_dual", "settle_won"]
    out[cols].to_csv(args.csv, index=False)
    print(f"已导出逐笔明细 {args.csv}（{len(out)} 行）")


if __name__ == "__main__":
    main()
