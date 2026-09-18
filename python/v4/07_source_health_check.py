#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v4「狗@0.2」数据源健康度审计（延迟可见性 + 现货断流 + 阈值依据）。
对应 docs/dog020_risk_latency_plan_2026-09-16.md §2.7。

纯标准库（同 06 风格；部署机上无 numpy/pandas 也能跑）。四段:

  A 本窗 tick 健康度（winstats_*.jsonl, 2026-09-16 起落盘）: 无效 tick 占比、
    丢信号数/原因分解、锚缺失窗、恒等式核对
  B 触底 tick 三源龄（touches_*.jsonl）: book/twap/spot 龄分布 + spot_age 分桶战绩
  C 现货断流专项: 按日/按 UTC 小时缺失率 + 被前序腿遮蔽复算
  D --bt-scan: 读 data/btc 重跑 book 阈值扫描与现货零成交代理（需 venv, 走 01 脚本）
  E 汇总: 延迟导致的信号损失率（09-15 复验那个"查不下去的频率缺口"的可量化部分）

基线（§1.2, 纸面 09-03~09-16 共 14 日 / 回测 08-18~08-31 共 14 日）随行打印
「基线」对照列——**对齐不上说明口径写错了**（§2.7 验收条款）。

阈值默认 = 引擎现行值（book 300 / spot 2000 / twap 10000ms），与 --max-*-ms 一致。

用法:
  python3 07_source_health_check.py                      # A/B/C/E（纯标准库）
  python3 07_source_health_check.py --bt-scan            # 加 D 段
  python/venv/bin/python 07_source_health_check.py --bt-scan   # D 段需 numpy/pandas
"""
import argparse
import datetime as dt
import glob
import importlib.util
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE.parent.parent / "data" / "v4"
BTDATA = BASE.parent.parent / "data" / "btc"
BT_CSV = BASE / "data" / "trades_r1_combo.csv"   # 组合版头条明细（D 段回测自检用）

STAKE = 2.0
CRASH_MIN = 0.40        # 急跌腿 θ
REM_MIN = 180           # 时间腿（判定 rem > 180）
BAND_YC = (-0.6, 0.0)   # 组合版 yes 带
BAND_NO = (-1.0, 0.0)   # 组合版 no 带

# 三源阈值默认（引擎现行值; 与 cmd/flip 的 --max-book-lat-ms/--max-spot-age-ms/
# --max-twap-age-ms 默认一致——本脚本只审计"现行阈值下的损失", 不改阈值）
LIM_BOOK, LIM_SPOT, LIM_TWAP = 300, 2000, 10000

# §1.2 基线（纸面 09-03~09-16）: 现货缺失 33 行/0.93%, 其中 rem_low 遮蔽 24 /
# missing_spot（真丢）9, 真丢中急跌腿已过 5 行; 回测侧零成交代理 8 笔（1 胜 7 负）
BASE_SPOT = dict(rows=33, pct=0.93, rem_low=24, real=9, crash=5)
BASE_ZERO = dict(n=8)
BASE_CONV = 0.369       # 「rem>180 且急跌过」的条件通过率（§1.2(c) 折算用）


def pct(a, b):
    return (a / b * 100) if b else 0.0


def pctl(vals, p):
    """百分位（线性插值, 与 numpy percentile 默认同口径）。"""
    if not vals:
        return 0.0
    s = sorted(vals)
    if len(s) == 1:
        return float(s[0])
    k = (len(s) - 1) * p / 100.0
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def load_touches():
    """读 touches_*.jsonl（只留 event_type=touch 行, 按 ts 排序）。"""
    rows = []
    for f in sorted(glob.glob(str(DATA / "touches_*.jsonl"))):
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get("event_type") != "touch":
                continue
            rows.append(r)
    rows.sort(key=lambda r: r["ts"])
    return rows


def load_winstats():
    """读 winstats_*.jsonl(每窗一行 tick 健康度)。文件不存在返回 []。"""
    rows = []
    for f in sorted(glob.glob(str(DATA / "winstats_*.jsonl"))):
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get("kind") != "winstats":
                continue
            rows.append(r)
    rows.sort(key=lambda r: r.get("ts", 0))
    return rows


def pnl_of(r):
    """单笔 P&L（引擎已回填; 此处复算作校验）。"""
    if r.get("won"):
        return STAKE / r["fill"] - STAKE
    return -STAKE


def wr_ev(rows):
    """(n, WR, EV, P&L)——仅已结算行参与; rows 应为已结算集合。"""
    if not rows:
        return 0, 0.0, 0.0, 0.0
    k = sum(1 for r in rows if r.get("won"))
    p = sum(r.get("pnl", 0.0) for r in rows)
    return len(rows), k / len(rows), p / len(rows), p


def split_gated(rows, include_gated):
    """被闸行与被闸行数（默认排除, plan §3.7）——被闸行实盘不会开这一笔。

    判据 = gate_reason 非空（daily_loss / first_window 都是"实盘没开"），
    与 §3.7 的 gate_reason=="daily_loss" 纸面等价（paper 只会落 daily_loss）。
    """
    gated = [r for r in rows if r.get("gate_reason")]
    return gated, (rows if include_gated else [r for r in rows if not r.get("gate_reason")])


# ─────────────────────────── A 本窗 tick 健康度 ───────────────────────────

def anchor_missing_at_end(r):
    """本窗**结束时**是否仍无锚（= 本窗不产出观测）。

    2026-09-19 起锚由取锚通道精确命中「评估时刻 == 边界」的推送后回填, 窗口内短暂的
    「锚待命中」（每窗开头必然出现 ~2s, 实测到达 p50 +2.0s）不算缺失——故新行按
    anchor_exact 判定（见 A2）; 09-19 之前的老行没有 anchor_exact 键, 回退
    anchor_missing（当时它恰是「期末仍无锚」语义: 恢复通道成功即清除）。
    """
    if "anchor_exact" in r:
        return not r.get("anchor_exact")
    return bool(r.get("anchor_missing"))


def sec_a():
    print("\n【A】本窗 tick 健康度（winstats_*.jsonl, 2026-09-16 起落盘）")
    rows = load_winstats()
    if not rows:
        print("  无 winstats 文件（该功能 2026-09-16 上线, 需引擎跑过至少 1 个窗口）")
        print("  —— 用途: 无效 tick 占比 + 丢信号明细（延迟挡掉的本会触发 tick, §1.3 盲区）")
        return None
    by_day = defaultdict(list)
    for r in rows:
        by_day[r.get("date", "")].append(r)

    print(f"  {'日期':<12s} {'窗':>4s} {'跳过':>4s} {'无锚':>4s} {'ticks':>6s} "
          f"{'有效':>6s} {'陈旧':>6s} {'缺簿':>5s} {'无效%':>6s} {'丢信号':>6s}")
    tot = Counter()
    bad_ident = 0
    for d in sorted(by_day):
        g = by_day[d]
        t = sum(r.get("ticks", 0) for r in g)
        v = sum(r.get("ticks_valid", 0) for r in g)
        s = sum(r.get("book_stale", 0) for r in g)
        m = sum(r.get("book_missing", 0) for r in g)
        lost = sum(len(r.get("lost_triggers") or []) for r in g)
        skip = sum(1 for r in g if r.get("skip"))
        amiss = sum(1 for r in g if anchor_missing_at_end(r))
        bad_ident += sum(1 for r in g if r.get("ticks", 0) !=
                         r.get("ticks_valid", 0) + r.get("book_stale", 0) + r.get("book_missing", 0)
                         and not r.get("skip"))
        print(f"  {d:<12s} {len(g):4d} {skip:4d} {amiss:4d} {t:6d} {v:6d} {s:6d} {m:5d} "
              f"{pct(s+m, t):5.1f}% {lost:6d}")
        tot.update(dict(win=len(g), skip=skip, amiss=amiss, ticks=t, valid=v,
                        stale=s, missing=m, lost=lost))
    print(f"  {'合计':<12s} {tot['win']:4d} {tot['skip']:4d} {tot['amiss']:4d} "
          f"{tot['ticks']:6d} {tot['valid']:6d} {tot['stale']:6d} {tot['missing']:5d} "
          f"{pct(tot['stale']+tot['missing'], tot['ticks']):5.1f}% {tot['lost']:6d}")

    ident = "✅" if bad_ident == 0 else f"⚠️ {bad_ident} 窗违反"
    print(f"  恒等式 ticks == 有效+陈旧+缺簿: {ident}")
    if tot["win"]:
        print(f"  窗覆盖: {tot['win']} 窗（{tot['win']/288:.2f} 日当量, 288 = 满覆盖）; "
              f"跳过 {tot['skip']} / 期末仍无锚 {tot['amiss']}")

    # 丢信号明细: stale vs missing, 以及其中"本会真成为信号"的部分（rem>180）
    lost = [t for r in rows for t in (r.get("lost_triggers") or [])]
    if lost:
        print("  丢信号原因分解:")
        for reason, n in Counter(t.get("reason", "?") for t in lost).most_common():
            sub = [t for t in lost if t.get("reason") == reason]
            big = [t for t in sub if (t.get("rem") or 0) > REM_MIN]
            lat = pctl([t.get("book_lat_ms", 0) for t in sub], 50)
            print(f"    {reason:<14s} {n:5d} 笔, 其中 rem>{REM_MIN} {len(big):4d} 笔"
                  f"（book_lat 中位 {lat:.0f}ms）")
        print(f"    ⚠️ 上表是**下界**: 只覆盖被闸挡掉的 tick, 亚阈值陈旧（<2s 的 spot 龄）"
              f"仍在判定里, 见 B 段 spot_age 分桶")
    else:
        print("  丢信号 0 笔（本窗 tick 全过数据质量闸）")
    return dict(rows=rows, tot=tot, lost=lost)


# ─────────────────── A2 精确取锚（2026-09-19）───────────────────

def sec_a2(rows):
    """精确取锚健康度: 命中率、发布延迟（到达时刻距边界）、来源分解、未命中窗清单。

    只读 winstats_*.jsonl 的锚诊断字段（anchor_exact/anchor_src/anchor_recovered_ms,
    2026-09-19 起落盘; docs/dog020_anchor_exact_open_2026-09-19.md）——旧行没有
    anchor_exact 这个键, 按「新口径行」之外计数并跳过（不能用 anchor_src 过滤:
    09-18 的老行也带该字段）。

    验收线（09-19 文档）: 命中率 ≥99%、到达 p90 ≤3s。**skip 行必须排除**——
    被跳过的窗口（no_sigma/no_market…）本来就不取锚, 计进去会假性拉低命中率。
    """
    if not rows:
        return
    has = [r for r in rows if "anchor_exact" in r]
    print("\n【A2】精确取锚（winstats_*.jsonl, 2026-09-19 起落盘）")
    if not has:
        print("  无 anchor_exact 字段（该口径 2026-09-19 上线, 需引擎跑过至少 1 个窗口）")
        print("  —— 用途: 命中率、边界那一秒推送的到达延迟分布、未命中窗清单")
        return
    live = [r for r in has if not r.get("skip")]   # 排除跳过窗（本就不取锚）
    hit = [r for r in live if r.get("anchor_exact")]
    print(f"  行数 {len(has)}（新口径 {len(live)} 行实跑 + {len(has)-len(live)} 行跳过;"
          f" 无字段旧行 {len(rows)-len(has)}）")

    if live:
        print(f"  命中率: {len(hit)}/{len(live)} = {pct(len(hit), len(live)):.1f}%"
              f"（验收线 ≥99%; 未命中 {len(live)-len(hit)} 窗——引擎 anchor≤0, 不产出观测）")
    miss = [r for r in live if not r.get("anchor_exact")]
    if miss:
        for r in miss[:10]:
            print(f"    ✗ {r.get('slug','')} event_start={r.get('event_start')}"
                  f" anchor={r.get('anchor')} ticks={r.get('ticks')}")
        if len(miss) > 10:
            print(f"    …（其余 {len(miss)-10} 窗）")

    src = Counter(r.get("anchor_src") or "无锚" for r in has)
    print("  来源占比（含跳过行, 跳过行无锚属正常）: "
          + "  ".join(f"{k}={v}" for k, v in src.most_common()))

    lag = [r.get("anchor_recovered_ms") or 0 for r in hit]
    if lag:
        print(f"  到达延迟（命中窗: 该推送本地到达时刻 − 窗口边界）: "
              f"p10 {pctl(lag, 10):.0f}  p50 {pctl(lag, 50):.0f}  p90 {pctl(lag, 90):.0f}  "
              f"p99 {pctl(lag, 99):.0f}  max {max(lag):.0f} ms（验收线 p90 ≤3000; "
              f"取锚预算 20s）")
        slow = [v for v in lag if v > 5000]
        if slow:
            print(f"  ⚠️ >5s 的迟到窗 {len(slow)} 个（取锚照常捞回, 但窗口开头判定被闸）")


# ─────────────────────────── B 三源龄 + spot_age 分桶 ───────────────────────────

SPOT_BUCKETS = ("无字段", "无推送", "0", "(0,200]", "(200,500]", "(500,1s]",
                "(1s,2s]", ">2s")


def spot_bucket(v):
    """spot_age_ms → 桶名（无字段 = 2026-09-16 前落盘的行; 无推送 = -1）。"""
    if v is None:
        return "无字段"
    if v < 0:
        return "无推送"
    if v == 0:
        return "0"
    if v <= 200:
        return "(0,200]"
    if v <= 500:
        return "(200,500]"
    if v <= 1000:
        return "(500,1s]"
    if v <= 2000:
        return "(1s,2s]"
    return ">2s"


def age_block(name, vals, lim, thr_note=""):
    """一行龄分布（p50/p90/p99/max + 超阈行数）。"""
    if not vals:
        print(f"    {name:<16s} 无数据{thr_note}")
        return
    over = sum(1 for v in vals if v > lim)
    print(f"    {name:<16s} p50 {pctl(vals, 50):7.0f}  p90 {pctl(vals, 90):8.0f}  "
          f"p99 {pctl(vals, 99):9.0f}  max {max(vals):9.0f}  超阈(>{lim}) {over:4d} 行"
          f"{thr_note}")


def sec_b(include_gated):
    print("\n【B】触底 tick 三源龄（touches_*.jsonl）")
    rows = load_touches()
    if not rows:
        print("  无 touches 记录")
        return
    gated, rows = split_gated(rows, include_gated)
    if gated:
        print(f"  注: 被闸行 {len(gated)} 行（{'已计入' if include_gated else '已排除'}）")

    obs, sig = rows, [r for r in rows if r.get("ok")]
    settled = [r for r in sig if r.get("won") is not None]
    print(f"  观测 {len(obs)} 行 / {len({r['date'] for r in rows})} 天; "
          f"信号 {len(sig)} 笔（已结算 {len(settled)}）")

    print("  龄分布:")
    age_block("book_latency_ms", [r.get("book_latency_ms") or 0 for r in obs], LIM_BOOK)
    age_block("twap_age_ms", [r.get("twap_age_ms") or 0 for r in obs], LIM_TWAP)
    spot_vals = [r["spot_age_ms"] for r in obs if "spot_age_ms" in r]
    nopush = sum(1 for v in spot_vals if v < 0)
    age_block("spot_age_ms", [v for v in spot_vals if v >= 0], LIM_SPOT,
              f"; 另有无推送(-1) {nopush} 行")
    if not spot_vals:
        print("    ⚠️ spot_age_ms 全缺: 该字段 2026-09-16 才落盘（commit e494f40）——"
              "此前行无此键, 属预期")
    else:
        print(f"    字段覆盖 {len(spot_vals)}/{len(obs)} 行"
              f"（09-16 前落盘的行无此键, 属预期）")

    # spot_age 分桶战绩: 亚阈值陈旧是否污染 dist_s（§1.2(e) 的盲区, 无基线可对）
    print("  spot_age 分桶战绩（仅已结算信号; 全样本基线 "
          f"n={len(settled)} WR {pct(sum(1 for r in settled if r.get('won')), len(settled)):.1f}% "
          f"EV {sum(r.get('pnl',0) for r in settled)/len(settled) if settled else 0:+.3f}U/注）:")
    buckets = defaultdict(list)
    for r in settled:
        buckets[spot_bucket(r.get("spot_age_ms") if "spot_age_ms" in r else None)].append(r)
    for b in SPOT_BUCKETS:
        g = buckets.get(b)
        if not g:
            continue
        n, wr, ev, p = wr_ev(g)
        print(f"    {b:<10s} n={n:3d}  WR {wr*100:5.1f}%  EV {ev:+.3f}U/注  P&L {p:+6.1f}U")
    print("    （>2s 桶 = 引擎已判现货缺失, 不会成为信号; 该桶存在说明是 --include-gated 带出的）")


# ─────────────────────────── C 现货断流专项 ───────────────────────────

def sec_c(include_gated):
    print("\n【C】现货断流（spot == 0 = 本地接收龄 > 2s）")
    rows = load_touches()
    if not rows:
        return None
    gated, rows = split_gated(rows, include_gated)
    days = sorted({r["date"] for r in rows})
    miss = [r for r in rows if not r.get("spot")]

    print(f"  缺失 {len(miss)} 行 / {len(rows)} = {pct(len(miss), len(rows)):.2f}%"
          f"    「基线 §1.2(c): {BASE_SPOT['rows']} 行 / {BASE_SPOT['pct']}%」")
    print(f"  {'日期':<12s} {'缺失':>4s} {'观测':>5s} {'缺失%':>6s}   基线")
    base_day = {"2026-09-12": "2.1% (6 行)", "2026-09-13": "2.8% (8 行)"}
    for d in days:
        g = [r for r in rows if r["date"] == d]
        m = [r for r in g if not r.get("spot")]
        tag = base_day.get(d, "其余日均 0.64%")
        print(f"  {d:<12s} {len(m):4d} {len(g):5d} {pct(len(m), len(g)):5.1f}%   {tag}")

    # 按 UTC 小时（基线: 20:00~06:00 占 29/33 = 88%）
    hour_miss, hour_all = Counter(), Counter()
    for r in rows:
        h = dt.datetime.fromtimestamp(r["ts"] / 1000, dt.timezone.utc).hour
        hour_all[h] += 1
        if not r.get("spot"):
            hour_miss[h] += 1
    night = sum(v for h, v in hour_miss.items() if h >= 20 or h < 6)
    print(f"  按 UTC 小时（基线: 20:00~06:00 占 29/33 = 88%）: 夜间 {night}/{len(miss)} "
          f"= {pct(night, len(miss)):.0f}%")
    for h in sorted(hour_miss):
        bar = "█" * hour_miss[h]
        print(f"    {h:02d}:00 {hour_miss[h]:3d} 行 / {hour_all[h]:4d} "
              f"({pct(hour_miss[h], hour_all[h]):4.1f}%)  {bar}")

    # 被前序腿遮蔽复算: reject_reason × spot==0
    print("  缺失行的判定分流（区分『真丢』与『被前序腿遮蔽』）:")
    for k, v in Counter(r.get("reject_reason") or "(signal)" for r in miss).most_common():
        note = ""
        if k == "rem_low":
            note = "← 时间腿先否, 本来就不成立（遮蔽无害, 但 reject 统计里不可见）"
        elif k == "missing_spot":
            note = "← 真·丢信号（rem>180 才轮到现货腿）"
        print(f"    {k:<14s} {v:4d} 行  {note}")
    real = [r for r in miss if (r.get("rem") or 0) > REM_MIN]
    crash_ok = [r for r in real if (r.get("m_45") or 0) >= CRASH_MIN]
    est = len(crash_ok) * BASE_CONV
    nd = len(days)
    print(f"  真丢 {len(real)} 行（rem>{REM_MIN}）    「基线: {BASE_SPOT['real']} 行」")
    print(f"    其中急跌腿已过 {len(crash_ok)} 行          「基线: {BASE_SPOT['crash']} 行」")
    print(f"    → 洞腿无法离线复核（spot==0 时 dist_s 不计算）, 按条件通过率 "
          f"{BASE_CONV*100:.1f}% 折算 ≈ **{est:.1f} 笔 / {nd} 天 = {est/nd:.2f} 笔/日**")
    sig_n = len([r for r in rows if r.get("ok")])
    sig_day = sig_n / nd if nd else 0
    print(f"    = 信号量的 {pct(est, sig_n):.2f}%（信号 {sig_day:.1f}/日）")
    return dict(real=len(real), crash=len(crash_ok), est=est, days=nd, sig=sig_n)


# ─────────────────────────── D 回测代理（--bt-scan） ───────────────────────────

def load_backtest_module():
    """importlib 加载 01_backtest_r1.py（单一真相: 阈值扫描必须用同一套提取逻辑）。"""
    p = BASE / "01_backtest_r1.py"
    spec = importlib.util.spec_from_file_location("bt01", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def trigger_rows(max_lat):
    """每事件首个有效触底 tick + 「触发 tick 起往前连续零成交秒数」代理（镜像 01 extract 触发段）。

    zero_sec = 从触发秒**自身**往前数、连续 bin.ticks == 0 的秒数（§1.2(c) 的现货龄
    代理: 成交笔数断流 ≈ 报价也断流）。⚠️ 计数含触发秒本身——不含的话口径会从
    8 笔变成 31 笔（「触发秒无成交」与「前一秒无成交」是两回事, 09-16 对基线时踩到）。
    仅本函数用, 供 D 段与 01 提取结果按 event_start 关联。
    """
    sys.path.insert(0, str(BASE.parent))
    from v2.lib import load_events  # noqa: E402（纯标准库, 无 pandas 依赖）

    out = {}
    for e in load_events(str(BTDATA)):
        anchor = e.get("twap_open_price")
        if e.get("outcome") is None or not anchor:
            continue
        ticks = e.get("ticks") or []
        for i, t in enumerate(ticks):
            pm = t.get("pm") or {}
            if not pm or (pm.get("book_latency_ms") or 0) > max_lat:
                continue
            if not all((pm.get(k) or 0) > 0
                       for k in ("yes_bid", "yes_ask", "no_bid", "no_ask")):
                continue
            if (pm.get("yes_ask") or 1) <= 0.20 or (pm.get("no_ask") or 1) <= 0.20:
                zero = 0
                for j in range(i, max(-1, i - 60), -1):  # 含触发秒自身
                    if ((ticks[j].get("bin") or {}).get("ticks") or 0) > 0:
                        break
                    zero += 1
                out[e["start_time"]] = zero
                break
    return out


def sec_d(thresholds):
    print("\n【D】回测代理（data/btc; 组合纯现货口径, 阈值改动前的依据）")
    try:
        mod = load_backtest_module()
    except ImportError as e:
        print(f"  跳过: 需要 numpy/pandas（用 python/venv/bin/python 跑）: {e}")
        return
    # 01 的 main() 会把 REM_MAX 设为 args（默认 None = 不设上限）; import 拿到的是模块
    # 默认值 240 —— 必须显式还原为头条口径, 否则扫描数字对不上（09-16 踩过的坑）
    zmap = trigger_rows(300)

    print(f"  {'book阈(ms)':>10s} {'n':>5s} {'WR':>7s} {'EV':>9s} {'P&L':>9s} {'日正':>5s}")
    for T in thresholds:
        mod.MAX_LAT = T
        mod.REM_MIN, mod.REM_MAX = REM_MIN, None
        df = mod.extract(str(BTDATA))
        pure, dual, pureC, dualC = mod.rules(df)
        s = df[pureC]
        if len(s) == 0:
            print(f"  {T:>10d} {0:>5d}")
            continue
        shares = STAKE / s["fill"]
        pl = (shares - STAKE).where(s["settle_won"] == 1, -STAKE)
        dp = pl.groupby(s["date"]).sum()
        note = ""
        if T == 300:
            ok = len(s) == 625 and abs(pl.sum() - 395.8) < 0.5
            note = "  ← 自检 ✅（基线 n=625 / +395.8U）" if ok else "  ← ⚠️ 自检失败, 口径漂移"
        print(f"  {T:>10d} {len(s):>5d} {s['settle_won'].mean()*100:6.1f}% "
              f"{pl.mean():+8.3f}U {pl.sum():+8.1f}U {int((dp > 0).sum()):>3d}/{len(dp):<2d}{note}")

    # 零成交代理: 在 T=300 组合子集上看战绩（基线 8 笔, 1 胜 7 负）
    mod.MAX_LAT = 300
    df = mod.extract(str(BTDATA))
    # 口径自检: 本函数的触发扫描 vs 01 的提取（同阈值下事件数必须一致, 否则两条
    # 独立实现的分歧会让 D 段的战绩数字失去意义）
    same = len(zmap) == len(df)
    print(f"  口径自检: 触发扫描 {len(zmap)} 事件 vs 01 提取 {len(df)} 行 "
          f"{'✅' if same else '⚠️ 不一致'}")
    pure, dual, pureC, dualC = mod.rules(df)
    s = df[pureC].copy()
    s["zero"] = [zmap.get(es, -1) for es in s["event_start"]]
    z = s[s["zero"] >= 1]
    print(f"  触发秒无成交（连续零成交 ≥1s, 含触发秒）: {len(z)} 笔"
          f"    「基线 §1.2(c): {BASE_ZERO['n']} 笔（1 胜 7 负）」")
    if len(z):
        shares = STAKE / z["fill"]
        pl = (shares - STAKE).where(z["settle_won"] == 1, -STAKE)
        print(f"    战绩: {int(z['settle_won'].sum())} 胜 {len(z)-int(z['settle_won'].sum())} 负"
              f"  EV {pl.mean():+.3f}U/注  P&L {pl.sum():+.1f}U")
        print("    → n 太小, 只作方向参考（«近 2s 至少 1 笔成交» 过滤后 n 不变, 见 §1.2(c)）")


# ─────────────────────────── E 汇总 ───────────────────────────

def sec_e(c, a):
    print("\n【E】延迟导致的信号损失率（汇总）")
    if c:
        print(f"  1) 现货断流真丢: {c['real']} 行 / {c['days']} 天 "
              f"= {c['real']/c['days']:.2f} 行/日 → 折算少赚 ≈ {c['est']/c['days']:.2f} 笔/日"
              f"（{pct(c['est'], c['sig']):.2f}% 信号量）")
    else:
        print("  1) 现货断流真丢: 无 touches 数据")
    if a and a["tot"].get("ticks"):
        t = a["tot"]
        print(f"  2) 盘口闸丢信号（winstats）: {t['lost']} 笔 / {t['win']} 窗 "
              f"= {t['lost']/t['win']:.3f} 笔/窗 ≈ {t['lost']/t['win']*288:.1f} 笔/日"
              f"（上界: 其中一部分本就会被时间腿等其他腿否）")
        print(f"     无效 tick 占比 {pct(t['stale']+t['missing'], t['ticks']):.2f}%"
              f"（陈旧 {pct(t['stale'], t['ticks']):.2f}% + 缺簿 {pct(t['missing'], t['ticks']):.2f}%）")
    else:
        print("  2) 盘口闸丢信号（winstats）: 无 winstats 文件（09-16 起落盘）")
    print("  3) 亚阈值陈旧（<2s 的 spot 龄污染 dist_s）: 见 B 段 spot_age 分桶——"
          "这是唯一的机制性盲区, 也是本脚本存在的主因")
    print("  判读: 若 1)+2) 合计 ≪ 09-15 复验的频率缺口（36.5/日 vs 44±5）, 则延迟不是主因,"
          "主因仍是触底时点后移（rem_low 占比上升）")


def main():
    ap = argparse.ArgumentParser(description="v4 dog@0.2 数据源健康度审计")
    ap.add_argument("--data", default=str(DATA), help="引擎输出目录（touches_/winstats_）")
    ap.add_argument("--include-gated", action="store_true",
                    help="不排除风控闸拦截行（gate_reason 非空, plan §3.7）")
    ap.add_argument("--bt-scan", action="store_true",
                    help="加 D 段: 读 data/btc 重跑 book 阈值扫描 + 零成交代理（需 venv）")
    ap.add_argument("--bt-thresholds", default="20,50,100,150,200,300",
                    help="D 段扫描的 book 延迟阈值列表（ms）")
    args = ap.parse_args()
    if args.data != str(DATA):
        globals()["DATA"] = Path(args.data)

    print("=" * 78)
    print("狗@0.2 数据源健康度审计（延迟可见性 / 现货断流 / 阈值依据）")
    print(f"数据 {DATA}   阈值 book {LIM_BOOK}ms / spot {LIM_SPOT}ms / twap {LIM_TWAP}ms"
          f"{'   [含被闸行]' if args.include_gated else ''}")
    print("=" * 78)
    a = sec_a()
    sec_a2(a["rows"] if a else [])
    sec_b(args.include_gated)
    c = sec_c(args.include_gated)
    if args.bt_scan:
        sec_d([int(x) for x in args.bt_thresholds.split(",") if x.strip()])
    else:
        print("\n【D】回测代理: 未启用（--bt-scan）")
    sec_e(c, a)
    bt_csv = BT_CSV.exists()
    print(f"\n（回测明细参照 {'存在' if bt_csv else '缺失'}: {BT_CSV.name}；"
          f"任何回测重跑必须用默认参数并核对 n=625 / +395.8U）")


if __name__ == "__main__":
    main()
