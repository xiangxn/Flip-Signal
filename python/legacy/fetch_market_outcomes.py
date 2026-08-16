#!/usr/bin/env python3
"""
抓取 data_0 旧事件的 PM 市场真实结算结果（by slug），补入 JSONL。

背景: data_0（2026-08-05 ~ 08-12，1719 事件）的 outcome 是 lab 用 Binance
收盘 vs 开盘自算的，不是 PM 市场真实结算（Chainlink TWAP）。本脚本对每个
事件按 slug `btc-updown-5m-{start_time}` 调 gamma API，解析 outcomePrices
（已结算市场为 ["1","0"] 或 ["0","1"]），把真实结果写入新字段
`market_outcome`（0=Up 赢, 1=Down 赢, null=未结算/未找到），
输出到独立目录（不破坏原文件）。

幂等与限速:
  * 抓取结果缓存到 out_dir/market_outcomes_cache.json，重跑自动续传
  * 默认 3 req/s + 抖动；429 指数退避（5s→60s）重试 ≤5 次
  * 网络错误重试 ≤3 次；404 记为 not_found 不重试

代理: 直连不可用，需 https_proxy 环境变量（如 http://127.0.0.1:1087），
与 Go 端 SDK 的代理配置一致。

用法:
    https_proxy=http://127.0.0.1:1087 python fetch_market_outcomes.py \
        --data ../data_0/lab --out-dir ../data_0/lab_resolved [--rate 3]
"""

import argparse
import json
import os
import random
import sys
import time
import urllib.request
from pathlib import Path

GAMMA = "https://gamma-api.polymarket.com"


def make_opener():
    """按 https_proxy 环境变量构建 opener（直连不可用时必须设代理）。"""
    proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"https": proxy, "http": proxy}))
    else:
        print("⚠️ 未设置 https_proxy，将直连（大概率超时）")
    return urllib.request.build_opener(*handlers)


def fetch_market(opener, slug: str, timeout: int = 15) -> tuple[int | None, dict]:
    """抓取市场并解析真实结算。

    返回 (market_outcome, meta)：
      market_outcome: 0=Up 赢, 1=Down 赢, None=不可得
      meta: {"status": "ok"|"not_found"|"unresolved"|"error", "closed": bool, ...}
    """
    url = f"{GAMMA}/markets/slug/{slug}"
    req = urllib.request.Request(url, headers={"User-Agent": "flip-lab/1.0"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, {"status": "not_found"}
        raise  # 429/5xx 交给上层退避重试
    except Exception as e:  # 网络错误
        raise RuntimeError(str(e)) from e

    meta = {"status": "ok", "closed": data.get("closed", False),
            "uma": data.get("umaResolutionStatus", "")}
    prices_raw = data.get("outcomePrices", "[]")
    try:
        prices = [float(x) for x in json.loads(prices_raw)]
    except (json.JSONDecodeError, TypeError, ValueError):
        prices = []

    # 已结算: 赢家价格=1。outcomes 约定 [0]=Up, [1]=Down
    if data.get("closed") and len(prices) == 2:
        winner = int(prices.index(max(prices)))
        meta["prices"] = prices
        return winner, meta
    meta["status"] = "unresolved"
    meta["prices"] = prices
    return None, meta


def fetch_with_retry(opener, slug: str, rate: float) -> tuple[int | None, dict]:
    """限速 + 429 指数退避 + 网络错误重试。"""
    time.sleep(1.0 / rate * random.uniform(0.8, 1.2))  # 限速抖动
    backoff = 5.0
    for attempt in range(5):
        try:
            return fetch_market(opener, slug)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print(f"  ⚠️ 429 rate limited, 退避 {backoff:.0f}s...", flush=True)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
                continue
            raise
        except Exception as e:
            if attempt < 2:
                time.sleep(2.0 * (attempt + 1))
                continue
            return None, {"status": "error", "err": str(e)[:80]}
    return None, {"status": "error", "err": "429 重试耗尽"}


def main():
    parser = argparse.ArgumentParser(description="补抓 PM 市场真实结算到 JSONL")
    parser.add_argument("--data", default="../data_0/lab/", help="原始 JSONL 目录")
    parser.add_argument("--out-dir", default="../data_0/lab_resolved/",
                        help="补丁后输出目录（不破坏原文件）")
    parser.add_argument("--rate", type=float, default=3.0, help="抓取速率 req/s（默认 3，防 429）")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不抓取不写出")
    args = parser.parse_args()

    data_dir = Path(args.data)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_path = out_dir / "market_outcomes_cache.json"

    # ── 加载事件 ──
    events = []
    files = sorted(data_dir.glob("events_*.jsonl"))
    for path in files:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append((path.name, json.loads(line)))
    print(f"事件总数: {len(events)}，文件: {[p.name for p in files]}")

    # ── 加载缓存（幂等续传）──
    cache = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text())
        print(f"缓存已有: {len(cache)} 条")

    # ── 抓取 ──
    opener = make_opener()
    todo = [(name, e) for name, e in events
            if str(e["start_time"]) not in cache]
    print(f"待抓取: {len(todo)} 个市场（{len(events) - len(todo)} 已缓存）")

    if args.dry_run:
        print("dry-run 结束。")
        return

    status_count = {}
    t0 = time.time()
    for i, (name, e) in enumerate(todo):
        slug = f"btc-updown-5m-{e['start_time']}"
        outcome, meta = fetch_with_retry(opener, slug, args.rate)
        cache[str(e["start_time"])] = {"outcome": outcome, **meta}
        status_count[meta["status"]] = status_count.get(meta["status"], 0) + 1

        if (i + 1) % 25 == 0:
            cache_path.write_text(json.dumps(cache))
            el = time.time() - t0
            print(f"  进度 {i + 1}/{len(todo)} ({el:.0f}s, "
                  f"{status_count.get('ok', 0)} ok / "
                  f"{status_count.get('not_found', 0)} 404 / "
                  f"{status_count.get('unresolved', 0)} 未结算 / "
                  f"{status_count.get('error', 0)} 错误)", flush=True)
    cache_path.write_text(json.dumps(cache))
    print(f"抓取完成: {status_count}")

    # ── 写出补丁文件 ──
    n_patched = 0
    for name, e in events:
        rec = cache.get(str(e["start_time"]))
        if rec and rec["status"] == "ok" and rec["outcome"] is not None:
            e["market_outcome"] = rec["outcome"]
            n_patched += 1
    for path in files:
        with open(path) as f:
            lines = f.readlines()
        out_path = out_dir / path.name
        with open(out_path, "w") as f:
            for line in lines:
                e = json.loads(line)
                rec = cache.get(str(e["start_time"]))
                if rec:
                    e["market_outcome"] = rec["outcome"] if rec["status"] == "ok" else None
                f.write(json.dumps(e) + "\n")
        print(f"  写出 {out_path.name}")

    # ── 汇总: Binance 自算 outcome vs 市场真实结算 分歧 ──
    agree = disagree = unresolved = 0
    amp_buckets = {"Q1 最小振幅": [0, 0, 0], "Q2": [0, 0, 0],
                   "Q3": [0, 0, 0], "Q4 最大振幅": [0, 0, 0]}
    valid = [e for _, e in events if e.get("market_outcome") is not None]
    if valid:
        amps = sorted(abs(e["close_price"] - e["open_price"]) for e in valid)
        qs = [amps[len(amps) * k // 4] for k in (1, 2, 3)]
        for e in valid:
            d = 1 if e["outcome"] != e["market_outcome"] else 0
            if d:
                disagree += 1
            else:
                agree += 1
            amp = abs(e["close_price"] - e["open_price"])
            idx = 0 if amp < qs[0] else (1 if amp < qs[1] else (2 if amp < qs[2] else 3))
            b = amp_buckets[list(amp_buckets)[idx]]
            b[0] += 1
            b[1] += d
        unresolved = len(events) - len(valid)
        print(f"\n【Binance 自算 vs 市场真实结算 分歧】(n={len(valid)}, 未结算/未找到 {unresolved})")
        print(f"  总体分歧率: {disagree}/{len(valid)} ({disagree / len(valid) * 100:.1f}%)")
        for label, (n, d, _) in amp_buckets.items():
            if n:
                print(f"    {label}: {d}/{n} ({d / n * 100:.1f}%)")


if __name__ == "__main__":
    main()
