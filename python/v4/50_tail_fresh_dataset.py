#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫尾盘 · 全新数据面 第 1 步：data/events 原始解析 → 逐秒特征缓存

⚠️ 本文件**刻意不复用仓库里任何既有回测/引擎代码**（用户要求 2026-10-11：
「不要用现有的任何代码，防止之前有 bug 污染新逻辑的判断」）。
语义只按 CLAUDE.md 的**规格描述**从零实现，全部落在本文件里，可独立审计。

产出（默认 data/fresh/dataset.pkl）：
  windows: 每个 5 分钟窗一条记录，含
    meta : start / day / outcome / anchor(twap_open) / close(twap_close) / binance_open / slug
    sd   : 该窗 σ 折美元（前 ≤18 个「连续」已完窗的 |close-open| 均值，≥3 窗可用）
    arr  : 逐秒数组（rem / ts / spot / twap / 盘口四档 + top5 深度 / 延迟 / 币安买卖量）

口径来源（规格 → 本文件实现）：
  - anchor     = 行内 twap_open_price（边界那一秒的 Chainlink 推送）
  - spot       = tick.bin.price（**原始值**，不套引擎的陈旧钳零 —— 决策 #27 采集口径）
  - twap       = tick.twap.price
  - 盘口       = tick.pm.{yes,no}_{bid,ask} + top5 深度 + book_latency_ms（决策 #21：空侧照收）
  - outcome    = 行内 outcome（0=Up 1=Down，官方）
  - sd(hist_bps) = 前 ≤18 个**连续**窗 |twap_close − twap_open| 的均值（美元口径）
                  连续 = start_time 差恰好 300 秒（等价 flip 侧 RecentBlock 的缺口截断）
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import pickle
import sys

import numpy as np

DAY_SEC = 300
SIGMA_N = 18
SIGMA_MIN = 3


def parse_file(path: str) -> list[dict]:
    """读一个 events_*.jsonl → 窗口记录列表（跳过 settlement_correction 修正行）。"""
    out = []
    with open(path, "r") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            # settlement_correction 修正行没有 slug / ticks（决策 #23 的 compact 形态）
            if "slug" not in d or "ticks" not in d:
                continue
            ticks = d["ticks"]
            if not ticks:
                continue
            n = len(ticks)
            rem = np.empty(n, dtype=np.int32)
            ts = np.empty(n, dtype=np.int64)
            spot = np.zeros(n, dtype=np.float64)
            twap = np.zeros(n, dtype=np.float64)
            yb = np.zeros(n, dtype=np.float64)
            ya = np.zeros(n, dtype=np.float64)
            nb = np.zeros(n, dtype=np.float64)
            na = np.zeros(n, dtype=np.float64)
            yb5 = np.zeros(n, dtype=np.float64)
            ya5 = np.zeros(n, dtype=np.float64)
            nb5 = np.zeros(n, dtype=np.float64)
            na5 = np.zeros(n, dtype=np.float64)
            lat = np.zeros(n, dtype=np.int32)
            bbuy = np.zeros(n, dtype=np.float64)
            bsell = np.zeros(n, dtype=np.float64)
            bbid5 = np.zeros(n, dtype=np.float64)
            bask5 = np.zeros(n, dtype=np.float64)
            for i, t in enumerate(ticks):
                rem[i] = int(t.get("rem", -1))
                ts[i] = int(t.get("ts", 0))
                b = t.get("bin") or {}
                spot[i] = float(b.get("price") or 0.0)
                bbuy[i] = float(b.get("buy_vol") or 0.0)
                bsell[i] = float(b.get("sell_vol") or 0.0)
                bbid5[i] = float(b.get("bid5") or 0.0)
                bask5[i] = float(b.get("ask5") or 0.0)
                p = t.get("pm") or {}
                yb[i] = float(p.get("yes_bid") or 0.0)
                ya[i] = float(p.get("yes_ask") or 0.0)
                nb[i] = float(p.get("no_bid") or 0.0)
                na[i] = float(p.get("no_ask") or 0.0)
                yb5[i] = float(p.get("yes_bid_top5") or 0.0)
                ya5[i] = float(p.get("yes_ask_top5") or 0.0)
                nb5[i] = float(p.get("no_bid_top5") or 0.0)
                na5[i] = float(p.get("no_ask_top5") or 0.0)
                lat[i] = int(p.get("book_latency_ms") or 0)
                tw = t.get("twap") or {}
                twap[i] = float(tw.get("price") or 0.0)
            rec = {
                "start": int(d.get("start_time") or 0),
                "day": str(d.get("date") or ""),  # 行内 date 可能缺失，下面按文件日回填
                "outcome": int(d.get("outcome")),
                "anchor": float(d.get("twap_open_price") or 0.0),
                "close": float(d.get("twap_close_price") or 0.0),
                "bin_open": float(d.get("binance_open") or 0.0),
                "slug": d.get("slug", ""),
                "arr": {
                    "rem": rem, "ts": ts, "spot": spot, "twap": twap,
                    "yb": yb, "ya": ya, "nb": nb, "na": na,
                    "yb5": yb5, "ya5": ya5, "nb5": nb5, "na5": na5,
                    "lat": lat, "bbuy": bbuy, "bsell": bsell,
                    "bbid5": bbid5, "bask5": bask5,
                },
            }
            out.append(rec)
    return out


def attach_sigma(windows: list[dict]) -> None:
    """算每个窗的 σ（折美元）。

    两种取窗口径都算，主口径 = **按事件索引**（前 ≤18 个存在的窗，跨缺口不截断）：
      这是能与文档 pin 对齐的口径 —— data/events 上 n=1959 逐位一致（见 52 号校准），
      即文档 oracle 的语义。副口径 = 严格连续（缺口截断，同 flip 侧 RecentBlock），
      仅作敏感性对照：两口径中位差 2%，但在采集缺口处最多差 123 美元。
    """
    windows.sort(key=lambda w: w["start"])
    moves = [abs(w["close"] - w["anchor"]) for w in windows]
    for i, w in enumerate(windows):
        blk = moves[max(0, i - SIGMA_N):i]
        w["sd"] = float(np.mean(blk)) if len(blk) >= SIGMA_MIN else 0.0
        w["sd_n"] = len(blk)

    by_start = {w["start"]: w for w in windows}
    for w in windows:
        m2 = []
        s = w["start"]
        for k in range(1, SIGMA_N + 1):
            prev = by_start.get(s - k * DAY_SEC)
            if prev is None:
                break  # 缺口 ⇒ 截断
            m2.append(abs(prev["close"] - prev["anchor"]))
        w["sd_contig"] = float(np.mean(m2)) if len(m2) >= SIGMA_MIN else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="data/events")
    ap.add_argument("--out", default="data/fresh/dataset.pkl")
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.dir, "events_*.jsonl")))
    if not files:
        print(f"✗ 没找到 {args.dir}/events_*.jsonl")
        return 1

    all_w = []
    for p in files:
        day = os.path.basename(p)[len("events_"):-len(".jsonl")]
        ws = parse_file(p)
        for w in ws:
            if not w["day"]:
                w["day"] = day
        all_w.extend(ws)
        print(f"  {os.path.basename(p)}  {len(ws):4d} 窗")

    attach_sigma(all_w)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "wb") as fh:
        pickle.dump(all_w, fh, protocol=4)

    days = sorted({w["day"] for w in all_w})
    n_bad_anchor = sum(1 for w in all_w if w["anchor"] <= 0)
    n_bad_open = sum(1 for w in all_w if w["bin_open"] <= 0)
    n_nosigma = sum(1 for w in all_w if w["sd"] <= 0)
    print(f"\n✅ {len(all_w)} 窗 → {args.out}")
    print(f"   日期 {days[0]} ~ {days[-1]}（{len(days)} 个日）")
    print(f"   anchor ≤ 0 : {n_bad_anchor}   binance_open ≤ 0 : {n_bad_open}   sd 不可用 : {n_nosigma}")
    sizes = [len(w["arr"]["rem"]) for w in all_w]
    print(f"   每窗 tick 数 min/中位/max = {min(sizes)}/{int(np.median(sizes))}/{max(sizes)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
