#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""边界推送↔官方价 秒级相位对齐（2026-09-24）

回答的问题（用户原话）:
  「官方结算的价格是对齐的 5 分钟开始的一条还是前一个窗口的最后一秒(即 59 秒)。
   如果刚才新窗口的 open 等于前一个窗口的 close，那就可以直接用推送的价格数据来结算，
   只有当获取推送价格失败（网络丢了）才去拉取官方的结算价格」

做法: 把「官方在边界 B 时刻的取值」当作待测的**真值**，与推送流里 B 前后各几秒的
推送逐位比较（秒级相位扫描）——若官方取的是「窗口第 0 秒」，则 B+0 那一列全等、
邻近列不等（|差| 随偏移单调放大，呈 V 形）; 若取「前一秒(59s)」，则 B−1 列全等。

数据源:
  A. data/probe/twap_push_*.jsonl   cmd/twapprobe 采的逐秒推送
     {"ts": 评估时刻 ms, "arrived": 本地到达 ms, "price": x}
  B. polymarket crypto-price 接口    官方 open/close（与 19 共用缓存）

为什么用 openPrice 当「边界真值」: 19 号探针在 240 窗样本上已验 **官方
close(N) ≡ 官方 open(N+1) 逐位相等**（同一瞬刻），故「边界 B 的官方取值」只需取
一个数——本脚本取 openPrice(window 起点 = B)。

用法:
  python/venv/bin/python python/v4/20_boundary_push_align.py            # 采满 ≥6 个边界后再跑
  python/venv/bin/python python/v4/20_boundary_push_align.py --min-age 90
"""
import argparse
import datetime
import importlib.util
import json
import statistics
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent.parent
PUSH_DIR = ROOT / "data" / "probe"

# 复用 19 号探针的官方接口取数（含缓存）——口径只有一处实现
_spec = importlib.util.spec_from_file_location("probe19", BASE / "19_boundary_price_probe.py")
p19 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(p19)

SPAN = p19.SPAN
OFFSETS = [-3, -2, -1, 0, 1, 2, 3]     # 秒级相位偏移（相对边界）


def _iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc
                                           ).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def load_pushes():
    """读全部推送文件 → {评估时刻ms: (price, arrived_ms)}（同 ts 多条取后到者, 同引擎口径）。"""
    out = {}
    files = sorted(PUSH_DIR.glob("twap_push_*.jsonl"))
    bad = 0
    for f in files:
        with f.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                if d["ts"] <= 0:                     # 缺时间戳的条目（引擎会丢弃, 这里也丢）
                    bad += 1
                    continue
                out[d["ts"]] = (d["price"], d["arrived"])
    return out, files, bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-age", type=int, default=90,
                    help="只分析「闭市后已过 N 秒」的边界（官方值需 ~40s 收敛, 默认 90）")
    ap.add_argument("--sleep", type=float, default=1.0, help="官方接口请求间隔秒")
    args = ap.parse_args()

    pushes, files, bad = load_pushes()
    if not pushes:
        print("❌ 没找到推送数据 —— 先跑采集: "
              "https_proxy=http://127.0.0.1:1087 go run ./cmd/twapprobe -out data/probe")
        return 1
    ts_list = sorted(pushes)
    lo, hi = ts_list[0], ts_list[-1]
    print(f"采集文件 {len(files)} 个, {len(pushes)} 条推送（丢弃缺时间戳 {bad} 条）")
    print(f"覆盖 {_iso(lo/1000)[:16]} → {_iso(hi/1000)[:16]}（UTC）")
    gaps = [t for i, t in enumerate(ts_list[1:], 1) if t - ts_list[i - 1] != 1000]
    cov = 100.0 * len(pushes) / max(1, (hi - lo) // 1000 + 1)
    print(f"逐秒覆盖率 {cov:.1f}%（缺口 {len(gaps)} 处, 最长 {max([ts_list[i]-ts_list[i-1] for i,t in enumerate(ts_list[1:],1)]+[0])/1000:.1f}s）")
    arr = sorted(a - t for t, (p, a) in pushes.items())
    print(f"到达延迟（本地到达 − 评估时刻, 含时钟偏差）p50={arr[len(arr)//2]/1000:.2f}s "
          f"p90={arr[int(len(arr)*.9)]/1000:.2f}s\n")

    now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    # 边界 = 5 分钟整点（unix 秒），且窗口已闭市 min_age 秒以上
    b = lo // 300000 * 300000
    bounds = [t for t in range(b, hi + 1, 300000) if t + SPAN * 1000 + args.min_age * 1000 <= now * 1000]
    if not bounds:
        print(f"⚠️ 尚无满足「闭市后 ≥{args.min_age}s」的边界（采集刚起步？）")
        return 1

    cache = p19.load_cache()
    added, failed = p19.fetch_many([t // 1000 for t in bounds], cache, args.sleep)
    print(f"官方接口: 本次新取 {added} 条（失败 {failed}）\n")

    # 逐边界取官方 openPrice（= 边界 B 时刻的官方取值）
    rows = []
    for B in bounds:
        c = cache.get(B // 1000)
        if not c or c["open"] <= 0:
            continue
        rows.append((B, c["open"], c["close"]))
    if not rows:
        print("❌ 没有同时具备推送与官方值的边界")
        return 1

    print(f"可用边界 {len(rows)} 个（{_iso(rows[0][0]/1000)[:16]} → {_iso(rows[-1][0]/1000)[:16]}）")
    ref = statistics.median([r[1] for r in rows])

    # ── 相位扫描: 官方 open(B) vs 推送@(B + k 秒) ──
    print("\n【相位扫描】官方 open(B)  vs  推送@(B+k 秒)   ← 哪一列全等, 就是对齐哪一秒")
    print(f"{'k(秒)':>6} {'n':>4} {'逐位相等':>9} {'|差|中位':>12} {'|差|p90':>12}   美元（BTC≈{ref:,.0f}）")
    best = None
    for k in OFFSETS:
        ds, eq = [], 0
        for B, op, cp in rows:
            p = pushes.get(B + k * 1000)
            if not p:
                continue
            d = p[0] - op
            ds.append(abs(d))
            eq += (d == 0)
        if not ds:
            print(f"{k:>6} {0:>4}        —            —            —")
            continue
        ds.sort()
        q = lambda f: ds[min(len(ds) - 1, int(len(ds) * f))]
        print(f"{k:>6} {len(ds):>4} {eq:>9} {q(.5):>12.3f} {q(.9):>12.3f}   "
              f"({q(.5)/ref*1e4:.4f} / {q(.9)/ref*1e4:.4f} bps)")
        if best is None or q(.5) < best[1]:
            best = (k, q(.5))
    print(f"→ 中位差最小: k={best[0]} 秒（|差| 中位 {best[1]:.3f} 美元）")

    # ── 官方前后窗共用边界值? （19 号在 240 窗上已验, 这里在**同一批边界**上复核）──
    print("\n【复核】官方 close(窗结束于 B) ≡ 官方 open(窗起于 B) ?")
    pairs = []
    for B in bounds:
        w0 = cache.get((B - SPAN * 1000) // 1000)     # 窗 [B−300, B]
        w1 = cache.get(B // 1000)                     # 窗 [B, B+300]
        if w0 and w1 and w0["close"] > 0 and w1["open"] > 0:
            pairs.append((w0["close"], w1["open"]))
    if pairs:
        eq = sum(1 for a, b_ in pairs if a == b_)
        ds = sorted(abs(a - b_) for a, b_ in pairs)
        print(f"  n={len(pairs)}  逐位相等 {eq} ({100*eq/len(pairs):.1f}%)  "
              f"|差| 中位 {ds[len(ds)//2]:.4f} 美元  p90 {ds[min(len(ds)-1,int(len(ds)*.9))]:.4f} 美元")

    # ── 自算结算 vs 官方方向（本次采集窗内振幅是否远大于我们对齐误差）──
    print("\n【自算结算风险】若用推送值自算 win 方向, 与官方方向不一致的窗口数:")
    risky = 0
    vals = []
    for B, op, cp in rows:
        p0 = pushes.get(B)
        p1 = pushes.get(B + SPAN * 1000)
        if not p0 or not p1:
            continue
        official = 0 if cp > op else 1                 # 0=Up(涨) 1=Down(跌)
        ours = 0 if p1[0] > p0[0] else 1
        vals.append((abs(cp - op), official == ours))
        risky += (official != ours)
    if vals:
        amps = sorted(v[0] for v in vals)
        print(f"  可自算边界 {len(vals)} 个, 方向不一致 {risky} 个; "
              f"窗口振幅中位 {amps[len(amps)//2]:.2f} 美元（振幅越小越危险）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
