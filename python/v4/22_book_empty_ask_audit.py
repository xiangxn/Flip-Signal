#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""空侧盘口审计（2026-09-24）：`cmd/bookprobe` 的 WS 秒级记录 → 逐窗回答三件事。

背景：用户观察「60s 检查点很多其实已经没有 ask 单了，实盘可能不能成交」，并要求
① 核实 SDK 的数据、② Dashboard 别再显示一个假 0.99。本脚本消费 cmd/bookprobe 的
输出（**引擎同一条 SDK WS 通道**，每秒一条 `kind=sec`），逐窗给出：

  Q1 空 ask 从哪一秒开始、持续到收盘多久？（= 实盘挂单还有没有对手方）
  Q2 该秒的**原始**盘口（raw）与**引擎视角**（eng）差多少？差多少秒的簿龄？
     —— eng 是「cmd/{flip,tail} 的 WS 循环丢掉空侧整簿后留下的那份旧簿」，
        页面上的假 0.99 就是它。
  Q3 空 ask 之前的档数/最优档股数轨迹（挂单是慢慢撤走的还是一次撤空）。

⚠️ 口径 3 条（读结论前必须知道）:
  - `kind=sec` 是**每秒一条**的采样（探针主循环 1s 一条），「空侧持续 N 秒」即 N 个采样秒。
  - `eng` 由探针按旧守卫（`len(Bids)==0 || len(Asks)==0 { continue }`）现算，**不是**
    引擎落盘值——引擎落盘的 `tail_*` / `touches_*` 只有价格、没有簿龄，回答不了 Q2。
  - `raw_age_ms` 是最近一条整簿消息的本地接收龄（无偏）。它 > 1.5s 的秒 = WS 断线/
    慢消费者重连期的冻结，与「空侧」是**两个独立**的陈旧来源（Q4 单独统计）。

用法: python/venv/bin/python python/v4/22_book_empty_ask_audit.py [--file data/probe/book_2026-09-24.jsonl]
"""
import argparse
import collections
import glob
import json
import os
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def load(path):
    rows = [json.loads(l) for l in open(path) if l.strip()]
    sec = [r for r in rows if r.get("kind") == "sec"]
    evt = [r for r in rows if r.get("kind") == "evt"]
    return sec, evt


def side_series(sec, tok):
    """某 token 的逐秒序列: [(rem, n_asks, n_bids, ask, ask_sz, raw_age_ms, eng_ask, eng_age_ms)]。"""
    out = []
    for r in sec:
        v = r.get(tok) or {}
        out.append((r["rem"], v.get("n_asks", 0), v.get("n_bids", 0), v.get("ask", 0.0),
                    v.get("ask_sz", 0.0), v.get("raw_age_ms", 0),
                    v.get("eng_ask", 0.0), v.get("eng_age_ms", 0)))
    return out


def empty_run(series, idx):
    """从 series[idx] 起（含）连续 n_asks == 0 的长度。"""
    n = 0
    for s in series[idx:]:
        if s[1] == 0:
            n += 1
        else:
            break
    return n


def audit_window(win, sec, evt):
    subs = [r for r in sec if r["window"] == win]
    subs.sort(key=lambda r: -r["rem"])          # rem 降序 = 时间顺序
    slug = subs[0].get("slug") or str(win)
    print(f"── 窗口 {win} {slug}（采样 {len(subs)} 秒, rem {subs[0]['rem']}→{subs[-1]['rem']}）")
    summary = {"win": win, "empty": {}, "freeze_max": 0}
    for tok, name in (("up", "UP(yes)"), ("down", "DOWN(no)")):
        ser = side_series(subs, tok)
        # 首个「asks 为空 且 此后一直为空」的秒（排除起始尚未收到消息的那几秒）
        start = None
        for i, s in enumerate(ser):
            if s[1] == 0 and i > 0 and empty_run(ser, i) == len(ser) - i:
                start = i
                break
        # 空 bids（镜像同一事实: 空 asks ⇔ 对手侧空 bids）
        bstart = None
        for i, s in enumerate(ser):
            if s[2] == 0 and i > 0 and all(x[2] == 0 for x in ser[i:]):
                bstart = i
                break
        if start is None and bstart is None:
            print(f"   {name}: 全程有 ask 与 bid（未出现空侧）")
            continue
        parts = []
        if start is not None:
            rem0 = ser[start][0]
            parts.append(f"asks 空 起始 rem={rem0}（持续到收盘 {len(ser) - start} 秒）")
            trail = " ".join(str(s[1]) for s in ser[max(0, start - 12):start + 1])
            parts.append(f"空侧前档数轨迹 {trail}")
            before = ser[start - 1] if start > 0 else None
            if before:
                parts.append(f"空侧前一秒最优 ask {before[3]:.2f}×{before[4]:.0f} 股")
        if bstart is not None:
            parts.append(f"bids 空 起始 rem={ser[bstart][0]}（持续 {len(ser) - bstart} 秒）")
        print(f"   {name}: " + "; ".join(parts))
        if start is not None:
            summary["empty"][tok] = (ser[start][0], len(ser) - start)

    # Q2: 引擎视角 vs 原始（空侧起始之后逐秒对照, 只打印前 3 秒与最后 1 秒）
    for tok, name in (("up", "UP(yes)"), ("down", "DOWN(no)")):
        ser = side_series(subs, tok)
        start = next((i for i, s in enumerate(ser)
                      if s[1] == 0 and i > 0 and empty_run(ser, i) == len(ser) - i), None)
        if start is None:
            continue
        seg = ser[start:]
        print(f"   引擎视角 {name}: 原始 ask 自 rem={seg[0][0]} 起为 0（空）; "
              f"引擎仍报 {seg[0][6]:.2f} → 末秒 {seg[-1][6]:.2f}（簿龄 "
              f"{seg[-1][7] / 1000:.1f}s, 即陈旧 {len(seg)} 秒）")
        for i in (0, 1, 2):
            if i < len(seg):
                s = seg[i]
                print(f"      rem={s[0]:>3} raw n_asks={s[1]} raw ask={s[3]:.2f} → "
                      f"eng ask={s[6]:.2f} eng 簿龄={s[7] / 1000:.1f}s")
        s = seg[-1]
        print(f"      rem={s[0]:>3}（末秒） raw n_asks={s[1]} raw ask={s[3]:.2f} → "
              f"eng ask={s[6]:.2f} eng 簿龄={s[7] / 1000:.1f}s")

    # Q4: 断线冻结（raw_age_ms 尖峰）
    ages = [max(s[5], d[5]) for s, d in zip(side_series(subs, "up"), side_series(subs, "down"))]
    if ages:
        summary["freeze_max"] = max(ages)
        n_freeze = sum(1 for a in ages if a > 1500)
        if n_freeze:
            print(f"   ⚠️ WS 冻结（raw_age_ms > 1500）: {n_freeze} 秒, 最长 "
                  f"{max(ages) / 1000:.1f}s（慢消费者断线重连, 与空侧无关的第二个陈旧来源）")
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="", help="bookprobe 输出（空 = 取 data/probe 最新一个）")
    args = ap.parse_args()
    path = args.file or sorted(glob.glob(str(ROOT / "data" / "probe" / "book_*.jsonl")))[-1]
    sec, evt = load(path)
    windows = sorted({r["window"] for r in sec})
    print("=" * 96)
    print(f"空侧盘口审计（cmd/bookprobe 的 SDK WS 秒级记录）")
    print(f"文件: {os.path.relpath(path, ROOT)}   窗口 {len(windows)} 个   秒行 {len(sec)}   "
          f"事件行 {len(evt)}")
    print("=" * 96)
    sums = [audit_window(w, sec, evt) for w in windows]

    print()
    print("=" * 96)
    print("汇总")
    print("=" * 96)
    any_empty = [s for s in sums if s["empty"]]
    print(f"  {len(any_empty)}/{len(sums)} 个窗出现过「整侧 asks 空」")
    for tok, name in (("up", "UP(yes)"), ("down", "DOWN(no)")):
        vals = [s["empty"][tok] for s in any_empty if tok in s["empty"]]
        if vals:
            rems = [v[0] for v in vals]
            durs = [v[1] for v in vals]
            print(f"  {name}: 空侧起始 rem 中位 {statistics.median(rems):.0f}"
                  f"（逐个 {sorted(rems, reverse=True)}）, 持续秒数中位 "
                  f"{statistics.median(durs):.0f}（逐个 {durs}）")
    fz = [s["freeze_max"] for s in sums if s["freeze_max"]]
    if fz:
        print(f"  WS 冻结最长 {max(fz) / 1000:.1f}s（各窗 {[round(x / 1000, 1) for x in fz]}）")
    print()
    print("  读法: 引擎视角（eng）那一列才是 Dashboard 当时显示的读数——旧守卫丢掉空侧整簿后")
    print("  内存里留的是撤单前那份旧簿（0.99 由此而来）。修复后空侧照存 ⇒ bestAsk=0 ⇒")
    print("  该 tick 被四档门控判无效、页面显示「—」、本窗不再产出基于假卖价的信号。")


if __name__ == "__main__":
    main()
