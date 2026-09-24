#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""尾盘活体盘口探针（2026-09-24）：用**真实 CLOB REST 盘口**回答「0.99 到底有没有量可吃」。

背景：17_tail_liquidity_probe.py 用 `data/btc`（08-18~08-31 回测数据）的 `*_top5`
字段测过这件事，结论是「0.99 档中位几百股、数量 <2U 的只占 0.98%」。但那份数据是
**回测期**（样本内），且 `*_top5` 是「前 5 档之和」——快照 tick 上的一秒级读数。
用户从纸面数据（data/v4-tail, 09-23/24）观察到的现象是「60s 检查点上热门侧常常已经
没有 ask 单了」，纸面行只有四个**价格**字段、没有数量，回答不了。

本探针换成**实时**取证：每 2s 拉一次当前 5 分钟窗两侧 token 的 CLOB REST 盘口
（只读公共 API，**不下任何单**），把 rem / 四档价量 / 最优 ask 起的逐档明细落盘。
跑满若干个窗后即可逐窗回答:

  Q1 快照那一刻（`rem ≤ 60` 的首个采样）热门侧最优 ask 是什么价位、那一档有多少股？
  Q2 价位 = 0.99 时，0.99 这一档**本身**的挂单量是多少（不是 top5 之和）？
  Q3 挂单量够不够 2U（= stake/fill 股）？不够的占比？
  Q4 0.99 档是不是**静态占位**（同一个股数长期不动）还是随成交变动？
  Q5 CLOB 给的 `minimum_order_size` 是多少——2U@0.99 = 2.02 股会不会直接被拒？

⚠️ 与引擎读数的已知口径差:
  - 引擎走 WS 订阅（MarketMonitor），本探针走 REST `/book`（同一本账、不同的口）；
  - REST 每秒轮询一次是**采样**，抓不到 1s 内的瞬时形态，故「零挂单」只能证有不能证无
    （真零量必须连续多秒都读到 0 才敢说）。

用法: python/venv/bin/python python/v4/21_tail_live_book_probe.py [--minutes 12] [--interval 2]
      （本机需 https_proxy=http://127.0.0.1:1087）
"""
import argparse
import datetime
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent.parent
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
WINDOW = 300
SLUG_PREFIX = "btc-updown-5m"
LEVELS = 6          # 从最优 ask 起记多少档（价位 + 单档股数）


def get_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "tail-book-probe/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def window_start(ts=None):
    ts = int(ts if ts is not None else time.time())
    return ts // WINDOW * WINDOW


def fetch_market(wstart):
    """按 slug 取 gamma 市场 → (condition_id, up_token, down_token)。"""
    data = get_json(f"{GAMMA}/markets?slug={SLUG_PREFIX}-{wstart}")
    if not data:
        return None
    m = data[0]
    toks = json.loads(m.get("clobTokenIds") or "[]")
    outcomes = [str(o) for o in json.loads(m.get("outcomes") or "[]")]
    up = down = ""
    for i, oc in enumerate(outcomes):
        if i >= len(toks):
            break
        if oc in ("Up", "Yes"):
            up = toks[i]
        elif oc in ("Down", "No"):
            down = toks[i]
    if not up and len(toks) == 2:      # outcome 命名不认识时按 [Up, Down] 顺序兜底
        up, down = toks[0], toks[1]
    return {"condition_id": m.get("conditionId", ""), "up": up, "down": down,
            "slug": m.get("slug", "")}


def fetch_book(token_id):
    """REST 盘口 → (best_bid, best_ask, 最优 ask 起的逐档 [(价, 股)]，买盘最优档股数)。"""
    b = get_json(f"{CLOB}/book?token_id={token_id}")
    bids = [(float(x["price"]), float(x["size"])) for x in (b.get("bids") or [])]
    asks = [(float(x["price"]), float(x["size"])) for x in (b.get("asks") or [])]
    # REST 返回的档位顺序不保证；自行排序: 最优卖 = 最低价, 最优买 = 最高价。
    asks.sort(key=lambda x: x[0])
    bids.sort(key=lambda x: -x[0])
    best_bid = bids[0][0] if bids else 0.0
    bid_sz = bids[0][1] if bids else 0.0
    # 合并同价位（REST 理论上已合并, 防御性再合一次）
    lv = []
    for p, s in asks:
        if lv and abs(lv[-1][0] - p) < 1e-9:
            lv[-1][1] += s
        else:
            lv.append([p, s])
    best_ask = lv[0][0] if lv else 0.0
    return best_bid, best_ask, lv[:LEVELS], bid_sz


def fetch_min_order(condition_id):
    """CLOB 市场元数据 → 最小下单股数（拿不到返回 0）。"""
    try:
        m = get_json(f"{CLOB}/markets/{condition_id}")
        return float(m.get("minimum_order_size") or 0)
    except Exception:
        return 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=12.0)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--out", default=str(ROOT / "data" / "probe"))
    args = ap.parse_args()

    if not (os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")):
        print("⚠️ 未设 https_proxy —— Polymarket 直连会超时（本机需 http://127.0.0.1:1087）")

    Path(args.out).mkdir(parents=True, exist_ok=True)
    out = Path(args.out) / f"tail_book_{datetime.datetime.now().strftime('%Y-%m-%d')}.jsonl"
    t_end = time.time() + args.minutes * 60
    mkt = None
    cur = None
    n = 0
    print(f"尾盘盘口探针启动：每 {args.interval}s 采样一次，跑 {args.minutes} 分钟 → {out}")
    print(f"{'UTC':<9}{'rem':>5}  {'up ask(量)':>18}{'up bid':>8}  "
          f"{'down ask(量)':>18}{'down bid':>8}  热门侧最优 ask 逐档(价×股)")
    while time.time() < t_end:
        try:
            ws = window_start()
            if ws != cur:
                cur, mkt = ws, fetch_market(ws)
                if mkt:
                    mo = fetch_min_order(mkt["condition_id"])
                    print(f"── 窗口 {ws} {mkt['slug']}  minimum_order_size="
                          f"{mo or '未知'} 股 ──")
                else:
                    print(f"⚠️ 窗口 {ws} 取不到市场（slug {SLUG_PREFIX}-{ws}）")
            if not mkt:
                time.sleep(args.interval)
                continue
            ub, ua, ulv, _ = fetch_book(mkt["up"])
            db, da, dlv, _ = fetch_book(mkt["down"])
            rem = cur + WINDOW - int(time.time())
            side = "yes" if ua >= da else "no"
            hot = ulv if side == "yes" else dlv
            hot_px = ua if side == "yes" else da
            hot_sz = hot[0][1] if hot else 0.0
            row = {
                "ts": int(time.time() * 1000), "window": cur, "rem": rem,
                "yes_ask": ua, "yes_bid": ub, "no_ask": da, "no_bid": db,
                "hot_side": side, "hot_ask": hot_px, "hot_ask_size": hot_sz,
                "hot_ask_levels": hot,          # 最优起逐档 [(价, 单档股)]
                "up_ask_levels": ulv, "down_ask_levels": dlv,
                "slug": mkt["slug"], "condition_id": mkt["condition_id"],
            }
            with out.open("a") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            n += 1
            if rem <= 65:
                lv = " ".join(f"{p:.2f}×{s:.0f}" for p, s in hot[:5])
                print(f"{datetime.datetime.utcnow().strftime('%H:%M:%S'):<9}{rem:>5}  "
                      f"{ua:>8.2f}({ulv[0][1] if ulv else 0:>7.0f}){ub:>8.2f}  "
                      f"{da:>8.2f}({dlv[0][1] if dlv else 0:>7.0f}){db:>8.2f}  "
                      f"[{side}] {hot_px:.2f}×{hot_sz:.0f}  {lv}")
        except Exception as e:
            print(f"⚠️ 采样失败: {e}")
        time.sleep(args.interval)
    print(f"完成: {n} 条采样 → {out}")


if __name__ == "__main__":
    main()
