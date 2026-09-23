#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""边界价探针：官方结算价对齐「窗口第 0 秒」还是「前一秒（59s）」？（2026-09-24）

用户提出的问题（原话）:
  「看看 twap_adapter 的推送价格，官方结算的价格是对齐的 5 分钟开始的一条还是前一个
   窗口的最后一秒(即 59 秒)。如果刚才新窗口的 open 等于前一个窗口的 close，
   那就可以直接用推送的价格数据来结算，只有当获取推送价格失败（网络丢了）
   才去拉取官方的结算价格」

为什么值钱: 引擎的锚就是「边界那一秒的推送」（决策 #15 精确取锚）。若官方结算的
open/close 用的是同一个边界值，则结算判定可以离线自算（推送即真值），只在丢推送时
回退官方 HTTP —— 少一条外部依赖（gamma 轮询 / crypto-price 接口）+ 秒级出结果。

三个数据源:
  A. data/v4/windows_*.jsonl   每窗一行, 含 anchor = 边界那一秒的推送（决策 #15 精确
     匹配）、close = 到达口径流值（引擎现口径）、event_start = 窗口起点（对齐 300s）
  B. polymarket crypto-price API   official_open / official_close（官方口径,
     https://polymarket.com/api/crypto/crypto-price）
  C. data/btc/events_*.jsonl   逐窗 outcome（官方结算方向, 0=Up 1=Down）

四问（N 为任一窗口, 边界 = N 的起点 = N−1 的终点）:
  Q1  official_open(N)  == anchor(N)            推送@边界秒 是否就是官方 open（#15 的 8/8 检验扩样）
  Q2  official_close(N) == anchor(N+1)          ★核心: 官方 close 是否 = 下一个边界那一秒的推送
  Q3  official_close(N) == official_open(N+1)   用户假设：官方口径下前后窗是否共用一个边界值
  Q4  official_close(N) == close_stream(N)      引擎现 close 口径（到达口径）偏差有多大

口径说明: 本脚本所有价格比较都是**逐位浮点相等**（官方返回 15~16 位有效数字,
不做任何容差），差异同时给美元与 bps 两个单位; bps 以该窗口的
(open+close)/2 为分母。σ 折算用本批窗口振幅 |close−open| 的中位数（该窗
hist_bps 的可用代理），只为把「差多少 bps」翻译成「差几分之一个 σ」。

用法:
  python/venv/bin/python python/v4/19_boundary_price_probe.py --sample 240   # 抽样快跑（~4 分钟）
  python/venv/bin/python python/v4/19_boundary_price_probe.py --all          # 全量（~1 小时, 落缓存）
  python/venv/bin/python python/v4/19_boundary_price_probe.py --all --offsets # 附加: 官方值的秒级相位扫描
"""
import argparse
import collections
import datetime
import json
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent          # python/v4
ROOT = BASE.parent.parent                        # 仓库根
WIN_DIR = ROOT / "data" / "v4"
CACHE = BASE / "data" / "official_open_close.jsonl"

API = "https://polymarket.com/api/crypto/crypto-price"
SPAN = 300          # btc-updown-5m 窗口长度（秒）
LOOKBACK = 60       # TWAP-60


# ───────────────────────── 数据源 A: 引擎纸面逐窗 ─────────────────────────

def load_windows(win_dir=WIN_DIR):
    """读 windows_*.jsonl → {event_start: row}（同窗跨日文件只留一条）。

    row: event_start, anchor, close, amp, slug, date, anchor_src?
    ⚠️ anchor 的**口径分朝代**: 09-19 之前 = 到达口径（Latest(), 有的还被决策 #14 的
    官方 +40s 覆盖过）; 09-19 起（决策 #15）= 精确匹配边界那一秒的推送, 且行里带
    anchor_src 字段。故只有带 anchor_src 的行（data/v4live）才是在测 #15。
    """
    out = {}
    for f in sorted(win_dir.glob("windows_*.jsonl")):
        with f.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                out[d["event_start"]] = d
    return out


def load_settlements(win_dir):
    """读 touches_*.jsonl 的已结算 ok 行 → {event_start: 市场实际方向}（0=Up 1=Down）。

    方向从 `won` 反推: 押 yes 且 won ⇒ Up; 押 no 且 won ⇒ Down（反之取补）。
    只用 ok=true 且带 won 字段的行（未结算的 ok 行没有 won）。
    """
    out = {}
    for f in sorted(win_dir.glob("touches_*.jsonl")):
        with f.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                if not d.get("ok") or "won" not in d:
                    continue
                up = (d["side"] == "yes") == bool(d["won"])
                out[d["event_start"]] = 0 if up else 1
    return out


# ───────────────────────── 数据源 B: 官方 crypto-price ─────────────────────────

def _iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc
                                           ).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def fetch_official(start_ts, timeout=20):
    """拉一个窗口的官方 open/close。返回 (open, close, raw_dict)；失败返回 (0, 0, {...})。"""
    q = urllib.parse.urlencode({
        "symbol": "BTC",
        "eventStartTime": _iso(start_ts),
        "endDate": _iso(start_ts + SPAN),
        "variant": "fiveminute",
        "twapEnabled": "true",
        "twapLookbackSeconds": str(LOOKBACK),
    })
    req = urllib.request.Request(API + "?" + q, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:160]
        except Exception:
            pass
        return 0.0, 0.0, {"error": f"HTTP {e.code}", "body": body}
    except Exception as e:                                    # 网络/超时/JSON
        return 0.0, 0.0, {"error": type(e).__name__}
    # 接口口径: 数据未就绪时 openPrice/closePrice 为 null（SDK 注释）
    op = raw.get("openPrice")
    cp = raw.get("closePrice")
    return (float(op) if isinstance(op, (int, float)) else 0.0,
            float(cp) if isinstance(cp, (int, float)) else 0.0,
            raw)


def load_cache():
    out = {}
    if CACHE.exists():
        with CACHE.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                prev = out.get(d["ts"])
                # 后写覆盖先写, 但**失败记录（open=0）不覆盖成功记录**: 否则一次限流
                # 就把已取到的真值抹掉, 后续分析会把它误算成「官方缺失」。
                if prev is not None and prev.get("open", 0) > 0 and d.get("open", 0) <= 0:
                    continue
                out[d["ts"]] = d
    return out


def fetch_many(starts, cache, sleep_s):
    """串行拉取（带缓存 + 指数退避重试），返回 (新增条数, 失败条数)。

    限流两条链路都有可能触发: ① polymarket 边缘节点直接回 429（body 是 "Too Many
    Requests", 与业务无关）; ② 上游 Chainlink 429 被包成 HTTP 400（body 里带
    "Chainlink API error 429"）。两者都用同一套退避: 2s → 5s → 10s → 20s, 最多
    4 次; 退避期间**整个队列停等**（惩罚期内继续发只会加重限流）。
    """
    todo = [t for t in starts if t not in cache or cache[t].get("open", 0) <= 0]
    if not todo:
        return 0, 0
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    added = failed = 0
    backoff = [2.0, 5.0, 10.0, 20.0]
    with CACHE.open("a") as fh:
        for i, ts in enumerate(todo):
            if i:
                time.sleep(sleep_s)
            op = cp = 0.0
            raw = {}
            for attempt in range(len(backoff) + 1):
                op, cp, raw = fetch_official(ts)
                if op > 0 and cp > 0:
                    break
                if attempt < len(backoff):
                    wait = backoff[attempt]
                    print(f"  ⏳ {_iso(ts)[:16]} 失败（{raw.get('error')} "
                          f"{str(raw.get('body',''))[:60].strip()}），{wait:.0f}s 后重试",
                          file=sys.stderr)
                    time.sleep(wait)
            rec = {"ts": ts, "open": op, "close": cp, "fetched_at": int(time.time()),
                   "raw": raw}
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            cache[ts] = rec
            added += 1
            if op <= 0 or cp <= 0:
                failed += 1
                print(f"  ⚠️ {_iso(ts)} 官方价格缺失（重试耗尽）: "
                      f"{raw.get('error')} {raw.get('body','')}", file=sys.stderr)
            if added % 50 == 0:
                print(f"  … 已取 {added}/{len(todo)}", file=sys.stderr)
    return added, failed


# ───────────────────────── 统计工具 ─────────────────────────

def cmp_stats(pairs, ref_price):
    """pairs: [(a, b)]。返回相等率与 |a−b| 分位（美元 / bps）。

    bps 分母 = ref_price（该批窗口价格中位数），单位为 0.0001%。
    """
    n = len(pairs)
    if not n:
        return None
    eq = sum(1 for a, b in pairs if a == b)
    ds = sorted(abs(a - b) for a, b in pairs)
    q = lambda arr, p: arr[min(len(arr) - 1, int(len(arr) * p))]
    bps = [d / ref_price * 1e4 for d in ds]
    return {"n": n, "eq": eq, "eq_pct": 100.0 * eq / n,
            "usd_p50": q(ds, .5), "usd_p90": q(ds, .9), "usd_max": ds[-1],
            "bps_p50": q(bps, .5), "bps_p90": q(bps, .9), "bps_max": bps[-1]}


def show(label, st, sigma_bps, sigma_usd):
    if st is None:
        print(f"{label}: 无样本")
        return
    if st["eq_pct"] >= 99.999:
        print(f"{label}: n={st['n']}  ✅ 全部逐位相等（{st['eq']}/{st['n']}）")
        return
    print(f"{label}: n={st['n']}  逐位相等 {st['eq']} ({st['eq_pct']:.1f}%)  "
          f"|差| p50={st['usd_p50']:.3f} 美元({st['bps_p50']:.4f}bps={st['bps_p50']/sigma_bps:.2f}σ) "
          f"p90={st['usd_p90']:.3f} 美元({st['bps_p90']:.4f}bps={st['bps_p90']/sigma_bps:.2f}σ) "
          f"max={st['usd_max']:.2f} 美元")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0,
                    help="均匀抽样窗口数（0 = 不抽样, 配合 --all 用全量）")
    ap.add_argument("--all", action="store_true", help="（已废弃, 取数现在默认自动补缺; 保留兼容）")
    ap.add_argument("--no-fetch", action="store_true", help="只用缓存, 不联网")
    ap.add_argument("--sleep", type=float, default=1.0, help="请求间隔秒（默认 1.0）")
    ap.add_argument("--limit", type=int, default=0, help="只分析前 N 个窗口（调试用）")
    ap.add_argument("--win-dir", default=str(WIN_DIR),
                    help="windows_*.jsonl 所在目录（默认 data/v4; 实盘= data/v4live）")
    args = ap.parse_args()

    wins = load_windows(Path(args.win_dir))
    starts = sorted(wins)
    if args.sample:
        # **连续块**抽样（不是逐窗等距抽）: Q3/Q5 是跨窗配对统计（窗 N 的 close 对
        # 窗 N+1 的 open），逐窗等距抽会把配对全部打散（实测 n=235 → Q3 只剩 1 对）。
        block = 8
        nblk = max(1, args.sample // block)
        step = max(1, len(starts) // nblk)
        sel = []
        for i in range(0, len(starts), step):
            sel.extend(starts[i:i + block])
        starts = sel[:args.sample]
    if args.limit:
        starts = starts[:args.limit]

    print(f"数据源 A: {len(wins)} 个窗口 "
          f"({_iso(min(wins))[:16]} → {_iso(max(wins))[:16]}), 本次分析 {len(starts)} 个")
    print(f"数据源 B: 官方 crypto-price 接口（缓存 {CACHE.relative_to(ROOT)}）")

    cache = load_cache()
    # 已结算窗口（Q6 的外部校验样本）全部取官方值, 与抽样集合并
    settled = load_settlements(Path(args.win_dir))
    fetch_list = sorted(set(starts) | set(settled))
    if not args.no_fetch:
        print("拉取官方价格…", file=sys.stderr)
        added, failed = fetch_many(fetch_list, cache, args.sleep)
        print(f"  本次新取 {added} 条, 其中失败 {failed} 条", file=sys.stderr)

    got = [t for t in starts if cache.get(t, {}).get("open", 0) > 0]
    print(f"数据源 B: 本次可用 {len(got)}/{len(starts)} 个窗口"
          f"（另含已结算窗口 {len(settled)} 个）\n")
    if not got:
        print("❌ 一个窗口都没取到官方价（检查代理 https_proxy / 是否被限流）")
        return 1

    # σ 尺子: 该批窗口振幅 |close−open| 的中位数（hist_bps 的可用代理）
    amps = [abs(cache[t]["close"] - cache[t]["open"]) for t in got]
    ref_price = statistics.median([cache[t]["open"] for t in got])
    # 振幅折算成 bps 再取中位：避免价格水平漂移影响
    amps_bps = sorted(abs(cache[t]["close"] - cache[t]["open"]) / cache[t]["open"] * 1e4
                      for t in got)
    sig_bps = amps_bps[len(amps_bps) // 2]
    sig_usd = statistics.median(amps)
    print(f"σ 尺子（本批窗口中位振幅）: {sig_bps:.3f} bps = {sig_usd:.2f} 美元/窗"
          f"（n={len(got)}）\n")

    # ── Q1: official_open(N) vs anchor(N) ──
    q1 = [(cache[t]["open"], wins[t]["anchor"]) for t in got
          if wins[t].get("anchor", 0) > 0]
    # ── Q2: official_close(N) vs anchor(N+1) ──
    q2 = [(cache[t]["close"], wins[t + SPAN]["anchor"]) for t in got
          if (t + SPAN) in wins and wins[t + SPAN].get("anchor", 0) > 0]
    # ── Q3: official_close(N) vs official_open(N+1) ──
    q3 = [(cache[t]["close"], cache[t + SPAN]["open"]) for t in got
          if (t + SPAN) in cache and cache[t + SPAN]["open"] > 0]
    # ── Q4: official_close(N) vs close_stream(N) ──
    q4 = [(cache[t]["close"], wins[t]["close"]) for t in got
          if wins[t].get("close", 0) > 0]
    # ── Q0: 反向对照: official_close(N) vs anchor(N)（若官方 close 取的是「本窗起点那一秒」）
    q0 = [(cache[t]["close"], wins[t]["anchor"]) for t in got
          if wins[t].get("anchor", 0) > 0]

    print("═" * 78)
    print("【Q1】官方 open(N)  vs  边界那一秒的推送 anchor(N)")
    show("  ", cmp_stats(q1, ref_price), sig_bps, sig_usd)
    print("【Q2】官方 close(N) vs  下一个边界那一秒的推送 anchor(N+1)   ★核心")
    show("  ", cmp_stats(q2, ref_price), sig_bps, sig_usd)
    print("【Q3】官方 close(N) vs  官方 open(N+1)（用户假设, 官方口径）")
    show("  ", cmp_stats(q3, ref_price), sig_bps, sig_usd)
    print("【Q4】官方 close(N) vs  引擎现口径 close_stream(N)（到达口径）")
    show("  ", cmp_stats(q4, ref_price), sig_bps, sig_usd)
    print("【Q0】对照: 官方 close(N) vs 本窗起点的推送 anchor(N)（错位窗, 必不等的验证）")
    show("  ", cmp_stats(q0, ref_price), sig_bps, sig_usd)
    print("═" * 78)

    # ── Q5: 自算结算方向 vs 官方方向 ──
    # 用本表自带的 anchor/close 当「自算值」（v4live = 引擎现行口径: anchor 精确边界
    # 推送 + close 到达口径流值），与官方 (open, close) 的方向逐窗对赌。
    # 这是「不用官方接口、自己结算」的错误率**直接估计**（比任何误差传播更硬）。
    q5 = []
    for t in got:
        w = wins[t]
        if w.get("anchor", 0) <= 0 or w.get("close", 0) <= 0:
            continue
        off_dir = 0 if cache[t]["close"] > cache[t]["open"] else 1
        our_dir = 0 if w["close"] > w["anchor"] else 1
        q5.append((t, abs(cache[t]["close"] - cache[t]["open"]), off_dir == our_dir,
                   abs(w["close"] - w["anchor"])))
    if q5:
        mism = [x for x in q5 if not x[2]]
        amps = sorted(x[1] for x in q5)
        print(f"【Q5】自算结算方向 vs 官方方向: n={len(q5)}  不一致 {len(mism)} 个 "
              f"({100*len(mism)/len(q5):.2f}%)")
        print(f"  官方振幅中位 {amps[len(amps)//2]:.2f} 美元; "
              f"自算振幅中位 {sorted(x[3] for x in q5)[len(q5)//2]:.2f} 美元")
        for t, a, _, oa in mism[:8]:
            print(f"  ✗ {_iso(t)[5:16]} 官方 Δ={cache[t]['close']-cache[t]['open']:+.4f} 美元 "
                  f"(振幅 {a:.3f})  自算 Δ={wins[t]['close']-wins[t]['anchor']:+.4f} 美元")

    # ── Q6: 官方 API 方向 vs 市场实际结算方向 ──
    # 「官方 (open, close) 的方向」是否就等于市场真正结算的那个方向（gamma outcome）。
    # 这是对 19/20 号探针全部结论的**外部校验**: 若 API 方向与市场结算不符, 那么
    # 「API 值 = 结算口径」这个前提本身就不成立, 后面所有对齐都无意义。
    if settled:
        common = [t for t in sorted(settled) if cache.get(t, {}).get("open", 0) > 0]
        mism = [(t, settled[t], 0 if cache[t]["close"] > cache[t]["open"] else 1)
                for t in common
                if settled[t] != (0 if cache[t]["close"] > cache[t]["open"] else 1)]
        print(f"【Q6】官方 API 方向 vs 市场实际结算方向: n={len(common)}  不一致 {len(mism)} 个 "
              f"({100*len(mism)/max(1,len(common)):.2f}%)")
        for t, mkt, off in mism[:8]:
            print(f"  ✗ {_iso(t)[5:16]} 市场={'Up' if mkt==0 else 'Down'} "
                  f"API={'Up' if off==0 else 'Down'}  Δ={cache[t]['close']-cache[t]['open']:+.4f} 美元")

    # 方向一致性: sign(official_close − official_open) 与引擎视角一致率
    # （仅统计本批内前后窗都取到的; 真正的官方 outcome 对照在 data/btc 段）
    print("\n明细样例（Q3 不等的前 10 对）:")
    shown = 0
    for t in got:
        if (t + SPAN) not in cache or cache[t + SPAN]["open"] <= 0:
            continue
        a, b = cache[t]["close"], cache[t + SPAN]["open"]
        if a != b:
            print(f"  {_iso(t)[5:16]}  close={a:.6f}  open(N+1)={b:.6f}  "
                  f"Δ={b-a:+.4f} 美元 ({(b-a)/a*1e4:+.4f} bps)")
            shown += 1
            if shown >= 10:
                break
    if not shown:
        print("  （无: 全部逐位相等）")


if __name__ == "__main__":
    main()
