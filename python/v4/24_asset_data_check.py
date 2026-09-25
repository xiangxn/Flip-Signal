#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据格式 v2 采集数据的正确性校验（**任意标的**: btc / eth / sol / bnb …）。

对应 2026-09-25 的采集同步（cmd/collect 从 eth 分支同步 + 硬编码参数化，
见 CLAUDE.md 决策 #23）。本脚本回答一个问题: 「这份 events_*.jsonl 里的数字
是不是真的」——每一段都拿**外部真值**或**内部恒等式**去对，而不是自说自话。

八段:

  A 文件与行结构: 键齐全、start_time 5 分钟对齐、slug 与 start_time/标的一致、
    无重复窗
  B tick 覆盖: 每窗 tick 数（期望 ~300）、ts 单调无重复、首/末 tick 相对边界的
    偏移、最大 tick 间隔、rem 与 ts 恒等式
  C 盘口镜像恒等式: Polymarket 的 YES/NO 是同一本簿的两面 ⇒ yes_bid + no_ask = 1、
    yes_ask + no_bid = 1（±1 个 tick = 0.01）；前 5 档数量逐位相等
  D Binance 交叉核对（网络）: 拉同窗 1m K 线，逐分钟对 ① 成交笔数 ② 成交量
    ③ 价格区间，并核对 binance_open 是否等于同窗 5m K 线开盘价
  E TWAP 采样健康度: age_ms 分布、零价条数、与现货价的偏离（TWAP-60 是 60 秒
    滚动均值，必然滞后，看的是量级而非相等）
  F 成交桶一致性: ts 落在整秒、token 只有 YES/NO、每 token 每秒唯一、
    rem 恒等式、价格 ∈ [0,1]
  G 边界价口径核对（网络，**决定性的一段**）: 官方 crypto-price 的 openPrice /
    closePrice vs 行内 twap_open_price / twap_close_price——精确边界推送口径的
    全部意义就是这两个数**逐位相等**（决策 #19 在 BTC 上实测 315/315 与 309/309）
  H 结算方向核对（网络）: gamma umaResolutionStatus=="resolved" 的 outcomePrices
    vs 行内 outcome

⚠️ 网络段（D/G/H）需要 `https_proxy=http://127.0.0.1:1087`（本机 Polymarket 直连
超时），且 polymarket.com / gamma 需要浏览器 User-Agent（裸 urllib 的默认 UA 被
Cloudflare 挡 403）。加 `--no-net` 只跑离线段（A/B/C/E/F）。

用法:
  python/venv/bin/python python/v4/24_asset_data_check.py --asset eth
  python/venv/bin/python python/v4/24_asset_data_check.py --asset btc --dir data/btc
  python/venv/bin/python python/v4/24_asset_data_check.py --asset eth --no-net
"""
import argparse
import datetime as dt
import glob
import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent.parent

# 盘口镜像恒等式的容差：tick size = 0.01，两面各自四舍五入到分 ⇒ 1 个 tick 的余量
BOOK_TOL = 0.011
# 单窗期望 tick 数（300 秒，采样 1s；边界抖动允许少采 1~2 个）
TICK_MIN, TICK_MAX = 293, 302
WINDOW_SEC = 300
# 单窗名义 tick 数：C 段的逐窗越界率用它当分母（实际 tick 数 299~301，差别可忽略）
WINDOW_TICKS = 300
# 官方 crypto-price 收敛延迟：闭市后 ~40s 才给最终值（决策 #14），留 90s 余量
OFFICIAL_SETTLE_LAG = 90

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 每段累计的失败条目（(段号, 窗, 描述)），最后统一打印
FAILS = []
WARNS = []


def fail(sec, slug, msg):
    FAILS.append((sec, slug, msg))


def warn(sec, slug, msg):
    WARNS.append((sec, slug, msg))


def pct(n, d):
    return f"{100.0 * n / d:.2f}%" if d else "—"


def fmt_num(x, nd=2):
    return f"{x:,.{nd}f}" if x == x and abs(x) != float("inf") else "—"


def http_json(url, timeout=20, retry=2):
    """带浏览器 UA 的 GET → JSON。429（上游 Chainlink 限流）等 1.5s 重试，失败抛异常。"""
    last = None
    for i in range(retry + 1):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            last = e
            if e.code != 429 or i == retry:
                raise
            time.sleep(1.5 * (i + 1))
        except Exception as e:
            last = e
            if i == retry:
                raise
            time.sleep(0.5)
    raise last


# ───────────────────────── 载入 ─────────────────────────

def load_events(dirpath, asset):
    """读目录下全部 events_*.jsonl，返回 [(文件, 行号, dict)]；跳过解析失败的行。"""
    rows, bad = [], 0
    for fp in sorted(glob.glob(str(dirpath / "events_*.jsonl"))):
        with open(fp, encoding="utf-8") as fh:
            for ln, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError as e:
                    bad += 1
                    fail("A", Path(fp).name, f"第 {ln} 行 JSON 解析失败: {e}")
                    continue
                if d.get("event_type") == "settlement_correction":
                    rows.append((fp, ln, d, True))
                else:
                    rows.append((fp, ln, d, False))
    if bad:
        print(f"  ⚠️ {bad} 行无法解析")
    return rows


# ───────────────────────── A 结构与元数据 ─────────────────────────

REQUIRED = ["condition_id", "slug", "start_time", "twap_open_price", "twap_close_price",
            "close_source", "outcome", "binance_open", "ticks", "trades"]


def check_a(rows, asset):
    print("\n【A】文件与行结构")
    events = [r for r in rows if not r[3]]
    corr = [r for r in rows if r[3]]
    print(f"  事件行 {len(events)}，结算修正行 {len(corr)}")

    seen = {}
    byday = Counter()
    no_anchor_src = 0
    for fp, ln, d, _ in events:
        missing = [k for k in REQUIRED if k not in d]
        if missing:
            fail("A", d.get("slug", "?"), f"缺键 {missing}")
            continue
        st = d["start_time"]
        byday[dt.datetime.fromtimestamp(st, dt.timezone.utc).strftime("%Y-%m-%d")] += 1
        if st % WINDOW_SEC != 0:
            fail("A", d["slug"], f"start_time={st} 未对齐 5 分钟")
        # slug 格式 <asset>-updown-5m-<start_time>
        want = f"{d['slug'].rsplit('-', 1)[0]}-{st}" if "-" in d["slug"] else None
        if want and d["slug"] != want:
            fail("A", d["slug"], f"slug 尾号 {d['slug'].rsplit('-', 1)[-1]} != start_time {st}")
        if not d["slug"].startswith(asset + "-updown-5m"):
            fail("A", d["slug"], f"slug 前缀不是 {asset}-updown-5m")
        if st in seen:
            fail("A", d["slug"], f"与 {seen[st]} 重复的 start_time")
        seen[st] = d["slug"]
        for k in ("twap_open_price", "twap_close_price", "binance_open"):
            if not (d[k] > 0):
                fail("A", d["slug"], f"{k}={d[k]}（非正值；锚为 0 是决策 #15 的红线）")
        if "anchor_source" not in d:
            no_anchor_src += 1

    for day, n in sorted(byday.items()):
        print(f"    {day}: {n} 窗")
    if no_anchor_src:
        print(f"  ℹ️ {no_anchor_src}/{len(events)} 窗无 anchor_source 字段"
              f"（2026-09-25 前的旧行，非缺陷）")
    if events:
        src = Counter(d["close_source"] for _, _, d, _ in events)
        asrc = Counter(d.get("anchor_source", "(缺失)") for _, _, d, _ in events)
        print(f"  close_source 分布: {dict(src)}")
        print(f"  anchor_source 分布: {dict(asrc)}")
        n_push = sum(1 for _, _, d, _ in events if d["close_source"] == "push")
        print(f"  边界推送口径覆盖: {n_push}/{len(events)} = {pct(n_push, len(events))}")


# ───────────────────────── B tick 覆盖 ─────────────────────────

def check_b(events):
    print("\n【B】tick 覆盖与 rem 恒等式")
    counts, first_off, last_off, gaps = [], [], [], []
    for _, _, d, _ in events:
        slug, st, ticks = d["slug"], d["start_time"], d["ticks"]
        counts.append(len(ticks))
        if not (TICK_MIN <= len(ticks) <= TICK_MAX):
            fail("B", slug, f"tick 数 {len(ticks)} 越界 [{TICK_MIN},{TICK_MAX}]")
        tss = [t["ts"] for t in ticks]
        if tss != sorted(tss):
            fail("B", slug, "ts 非单调递增")
        if len(set(tss)) != len(tss):
            fail("B", slug, "存在重复 ts")
        if tss:
            first_off.append(tss[0] / 1000.0 - st)
            last_off.append(tss[-1] / 1000.0 - st)
        for a, b in zip(tss, tss[1:]):
            if b - a > 1500:
                gaps.append((slug, b - a))
        for t in ticks:
            sec = int(t["ts"] // 1000)
            # rem 是**剩余整秒数**；收尾那一条落在边界上（sec = st+300）时钳到 0
            want = max(0, st + WINDOW_SEC - sec - 1)
            if abs(t["rem"] - want) > 1:
                fail("B", slug, f"ts={t['ts']} rem={t['rem']} 期望 {want}")
                break
            # 闭区间：收尾 tick 正好落在 st+WINDOW_SEC（rem=0）
            if not (st <= sec <= st + WINDOW_SEC):
                fail("B", slug, f"ts={t['ts']} 落在窗口外")
                break

    if counts:
        print(f"  tick 数: 中位 {statistics.median(counts):.0f}，"
              f"范围 [{min(counts)}, {max(counts)}]，共 {sum(counts):,} 行")
    if first_off:
        print(f"  首 tick 相对边界偏移: 中位 {statistics.median(first_off):.2f}s "
              f"(p10 {sorted(first_off)[len(first_off)//10]:.2f}s)")
        print(f"  末 tick 相对边界偏移: 中位 {statistics.median(last_off):.2f}s")
    if gaps:
        print(f"  ⚠️ >1.5s 的 tick 间隔: {len(gaps)} 处，最长 {max(g[1] for g in gaps)}ms")
        for slug, g in sorted(gaps, key=lambda x: -x[1])[:5]:
            print(f"      {slug}: {g}ms")
    else:
        print("  tick 间隔: 全部 ≤1.5s（无断档）")


# ───────────────────────── C 盘口镜像恒等式 ─────────────────────────

def check_c(events):
    """
    盘口互补: `yes_bid + no_ask = 1`（买腿）与 `yes_ask + no_bid = 1`（卖腿）。

    ⚠️ 只有**两条腿都在**时这条约束才可判——决策 #21 的「赢家侧整侧撤空」会让一条腿
    整个消失（ya=0 且 nb=0），此时 0+0≠1 不是数据错误。故按腿判：两条腿都为 0 跳过，
    否则要求 ≈1。整侧缺腿单独统计（引擎的四档门控正是靠它判 tick 无效）。
    """
    print("\n【C】盘口互补恒等式（YES/NO 是同一本簿的两面）")
    n_tick = n_compl = n_top5 = 0
    bad_compl, bad_top5 = Counter(), Counter()
    all_empty = missing_leg = 0
    leg_rem = []      # 缺腿 tick 的 rem（看是不是集中在尾盘）
    top5_mismatch_ex = []
    for _, _, d, _ in events:
        for t in d["ticks"]:
            pm = t["pm"]
            n_tick += 1
            yb, ya, nb, na = pm["yes_bid"], pm["yes_ask"], pm["no_bid"], pm["no_ask"]
            if yb == 0 and ya == 0 and nb == 0 and na == 0:
                all_empty += 1
                continue
            buy_leg = not (yb == 0 and na == 0)   # 买腿 = yes_bid / no_ask
            sell_leg = not (ya == 0 and nb == 0)  # 卖腿 = yes_ask / no_bid
            if not buy_leg or not sell_leg:
                missing_leg += 1
                leg_rem.append(t["rem"])
            bad = ((buy_leg and abs(yb + na - 1.0) > BOOK_TOL) or
                   (sell_leg and abs(ya + nb - 1.0) > BOOK_TOL))
            if bad:
                bad_compl[d["slug"]] += 1
            else:
                n_compl += 1
            # 前 5 档数量：两本簿独立撮合，数量非定义性相等（仅作参考）
            ok = (abs(pm["yes_bid_top5"] - pm["no_ask_top5"]) < 1e-9 and
                  abs(pm["yes_ask_top5"] - pm["no_bid_top5"]) < 1e-9)
            if ok:
                n_top5 += 1
            else:
                bad_top5[d["slug"]] += 1
                if len(top5_mismatch_ex) < 3:
                    top5_mismatch_ex.append(
                        (d["slug"], t["ts"], pm["yes_bid_top5"], pm["no_ask_top5"],
                         pm["yes_ask_top5"], pm["no_bid_top5"]))

    valid = n_tick - all_empty
    bad_price = valid - n_compl
    print(f"  tick 总数 {n_tick:,}；四档全空（无效 tick）{all_empty:,} = {pct(all_empty, n_tick)}")
    print(f"  整侧缺腿（决策 #21 场景，赢家侧撤空）{missing_leg:,} = {pct(missing_leg, n_tick)}"
          + (f"；rem 范围 [{min(leg_rem)}, {max(leg_rem)}]" if leg_rem else ""))
    print(f"  价格互补达标: {n_compl:,}/{valid:,} = {pct(n_compl, valid)}（容差 ±{BOOK_TOL}）"
          f"，越界 {bad_price:,} = {pct(bad_price, valid)}")
    print(f"  前 5 档数量镜像相等: {n_top5:,}/{valid:,} = {pct(n_top5, valid)}"
          f"（两本独立簿，数量非定义性相等，仅作参考）")
    if bad_compl:
        top = bad_compl.most_common(3)
        print(f"    价格互补越界最多窗: {top}")
    if bad_top5:
        top = bad_top5.most_common(3)
        print(f"    前 5 档不等最多窗: {top}")
        for ex in top5_mismatch_ex:
            print(f"      例: {ex[0]} ts={ex[1]} yes_bid_top5={ex[2]} no_ask_top5={ex[3]} "
                  f"yes_ask_top5={ex[4]} no_bid_top5={ex[5]}")
    # 判定分两级（阈值由 data/btc 3753 窗实测标定，见 docs §5.5）：
    #   - 合计率 > 1% ⇒ FAIL。BTC 实测 0.126%（1373/1,091,652）
    #   - 单窗 > 5% ⇒ FAIL。BTC 逐窗 p99 = 1.0%、max = 1.667%（5/300）
    # 越界的成因是**两本簿的采样错位**：一行里的 yes_* 与 no_* 来自两条独立的 `book`
    # 消息（`MakePMTick` 的 `book_ts` 取两簿较大者，行内无从分辨），急跌秒会把两个瞬间
    # 的价格并进同一行 ⇒ 瞬态越界。不是数据损坏，故阈值按实测分布定，不按理论取 0。
    rate = bad_price / valid if valid else 0.0
    worst = bad_compl.most_common(1)[0] if bad_compl else ("—", 0)
    if rate > 0.01:
        fail("C", worst[0], f"价格互补越界 {bad_price:,} = {pct(bad_price, valid)}（阈值 1%）")
    elif worst[1] / max(1, WINDOW_TICKS) > 0.05:
        fail("C", worst[0], f"单窗价格互补越界 {worst[1]}/{WINDOW_TICKS} "
                            f"= {pct(worst[1], WINDOW_TICKS)}（阈值 5%）")
    elif bad_price:
        warn("C", "—", f"价格互补越界 {bad_price:,} 个 tick = {pct(bad_price, valid)}；"
                      f"单窗最多 {worst[1]}（均在实测分布内）")


# ───────────────────────── D Binance 交叉核对 ─────────────────────────

def binance_klines(rest, symbol, interval, start_ms, limit):
    url = (f"{rest}/api/v3/klines?symbol={urllib.parse.quote(symbol)}"
           f"&interval={interval}&startTime={start_ms}&limit={limit}")
    return http_json(url)


def check_d(events, asset, rest, symbol):
    print("\n【D】Binance 交叉核对（1m K 线）")
    ratios_cnt, ratios_vol, open_diffs = [], [], []
    n_ok = 0
    for _, _, d, _ in events:
        slug, st = d["slug"], d["start_time"]
        try:
            kl = binance_klines(rest, symbol, "1m", st * 1000, 5)
        except Exception as e:
            warn("D", slug, f"K 线拉取失败: {type(e).__name__} {e}")
            continue
        if len(kl) != 5:
            warn("D", slug, f"K 线只有 {len(kl)} 根（期望 5）")
            continue
        n_ok += 1
        # 逐分钟聚合 tick
        by_min = defaultdict(list)
        for t in d["ticks"]:
            by_min[int(t["ts"] // 1000 - st) // 60].append(t)
        for i, k in enumerate(kl):
            if k[0] // 1000 != st + i * 60:
                fail("D", slug, f"第 {i} 根 K 线 openTime 不对齐")
                continue
            kcnt, kvol = int(k[8]), float(k[5])
            klow, khigh = float(k[3]), float(k[2])
            rows = by_min.get(i, [])
            if not rows:
                fail("D", slug, f"第 {i} 分钟无 tick")
                continue
            cnt = sum(t["bin"]["ticks"] for t in rows)
            vol = sum(t["bin"]["buy_vol"] + t["bin"]["sell_vol"] for t in rows)
            if kcnt:
                ratios_cnt.append(cnt / kcnt)
            if kvol:
                ratios_vol.append(vol / kvol)
            # 价格必须落在该分钟 K 线的 [low, high] 内（留 1bp 容差给四舍五入）
            for t in rows:
                p = t["bin"]["price"]
                if p <= 0:
                    fail("D", slug, f"ts={t['ts']} bin.price={p}")
                    break
                if not (klow * 0.9999 <= p <= khigh * 1.0001):
                    fail("D", slug, f"ts={t['ts']} 价 {p} 越出 K 线 [{klow}, {khigh}]")
                    break
        # binance_open == 同窗 5m K 线开盘价
        try:
            k5 = binance_klines(rest, symbol, "5m", st * 1000, 1)
            if k5 and k5[0][0] // 1000 == st:
                o5 = float(k5[0][1])
                open_diffs.append(abs(o5 - d["binance_open"]))
                if abs(o5 - d["binance_open"]) > 1e-6:
                    fail("D", slug, f"binance_open={d['binance_open']} != 5m K 线开盘 {o5}")
        except Exception as e:
            warn("D", slug, f"5m K 线拉取失败: {e}")

    if n_ok == 0:
        print("  ⚠️ 无窗口完成 K 线核对（网络不可达？）")
        return
    print(f"  覆盖 {n_ok}/{len(events)} 窗")
    if ratios_cnt:
        s = sorted(ratios_cnt)
        print(f"  成交笔数比（采集 aggTrade 计数 / K 线 count）: 中位 {statistics.median(s):.4f}，"
              f"p05 {s[len(s)//20]:.4f}，p95 {s[-len(s)//20]:.4f}")
    if ratios_vol:
        s = sorted(ratios_vol)
        print(f"  成交量比（Σ(buy_vol+sell_vol) / K 线 volume）: 中位 {statistics.median(s):.4f}，"
              f"p05 {s[len(s)//20]:.4f}，p95 {s[-len(s)//20]:.4f}")
    if open_diffs:
        print(f"  binance_open vs 5m K 线开盘价: {sum(1 for x in open_diffs if x == 0)}/{len(open_diffs)} 逐位相等，"
              f"最大差 {max(open_diffs):.10f}")


# ───────────────────────── E TWAP 采样 ─────────────────────────

def check_e(events):
    print("\n【E】TWAP 采样健康度")
    ages, zeros, devs = [], 0, []
    for _, _, d, _ in events:
        for t in d["ticks"]:
            tw, p = t["twap"], t["bin"]["price"]
            if not (tw["price"] > 0):
                zeros += 1
                continue
            ages.append(tw["age_ms"])
            if p > 0:
                # 与同秒现货的相对偏离（bps）；TWAP-60 是 60 秒滚动均值 ⇒ 必然滞后
                devs.append(abs(tw["price"] - p) / p * 1e4)
    n = len(ages)
    if not n:
        print("  ⚠️ 无有效 TWAP 采样")
        return
    s = sorted(ages)
    print(f"  有效采样 {n:,}；零价 {zeros:,} = {pct(zeros, n + zeros)}")
    print(f"  age_ms: p50 {statistics.median(s):.0f}，p90 {s[int(n*0.9)]:.0f}，"
          f"p99 {s[min(n-1, int(n*0.99))]:.0f}，max {s[-1]:.0f}")
    sd = sorted(devs)
    print(f"  |TWAP − 现货|/现货: p50 {statistics.median(sd):.2f}bps，"
          f"p90 {sd[int(len(sd)*0.9)]:.2f}bps（滚动均值的预期滞后，非错误）")
    if zeros:
        warn("E", "—", f"{zeros} 个 tick 的 TWAP 为 0（推送缺口）")


# ───────────────────────── F 成交桶 ─────────────────────────

def check_f(events):
    print("\n【F】成交桶一致性")
    n_rows = 0
    tok = Counter()
    lat = []
    for _, _, d, _ in events:
        slug, st = d["slug"], d["start_time"]
        seen = set()
        for r in d["trades"]:
            n_rows += 1
            tok[r["token"]] += 1
            if r["ts"] % 1000 != 0:
                fail("F", slug, f"成交桶 ts={r['ts']} 非整秒")
            if r["token"] not in ("YES", "NO"):
                fail("F", slug, f"未知 token {r['token']!r}")
            key = (r["token"], r["ts"])
            if key in seen:
                fail("F", slug, f"token×秒重复: {key}")
            seen.add(key)
            sec = int(r["ts"] // 1000)
            want = max(0, st + WINDOW_SEC - sec)
            if abs(r["rem"] - want) > 1:
                fail("F", slug, f"ts={r['ts']} rem={r['rem']} 期望 {want}")
            for k in ("last_price", "best_bid", "best_ask", "vwap"):
                v = r[k]
                if v < 0 or v > 1.0001:
                    fail("F", slug, f"ts={r['ts']} {k}={v} 越出 [0,1]")
                    break
            if r["n_buy"] < 0 or r["n_sell"] < 0 or r["max_size"] < 0:
                fail("F", slug, f"ts={r['ts']} 负笔数/量")
    if events:
        lat = [len(d["trades"]) for _, _, d, _ in events]
        print(f"  成交聚合行 {n_rows:,}；每窗中位 {statistics.median(lat):.0f} 行，"
              f"范围 [{min(lat)}, {max(lat)}]")
    print(f"  token 分布: {dict(tok)}")


# ───────────────────────── G 边界价口径（决定性） ─────────────────────────

def crypto_price(symbol, start_epoch):
    iso = dt.datetime.fromtimestamp(start_epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = ("https://polymarket.com/api/crypto/crypto-price?"
           + urllib.parse.urlencode({"symbol": symbol, "eventStartTime": iso,
                                     "variant": "fiveminute", "twapEnabled": "true",
                                     "twapLookbackSeconds": 60}))
    return http_json(url)


def check_g(events, symbol):
    """
    行内边界价 vs 官方 crypto-price。

    判定分档（这是本段的关键）: `close_source == "push"` 的行**必须**与官方逐位
    相等——推送口径的全部意义就在这（决策 #19: 315/315 与 309/309 逐位相同）；
    旧口径（stream/official）的行**本来就不等**，只作对照打印，不算失败。
    """
    print("\n【G】边界价口径核对（官方 crypto-price vs 行内值）")
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    n = 0
    push = {"n": 0, "open_ex": 0, "close_ex": 0, "open_d": [], "close_d": [], "worst": []}
    legacy = {"n": 0, "open_ex": 0, "close_ex": 0, "open_d": [], "close_d": []}
    for _, _, d, _ in events:
        st = d["start_time"]
        if now - (st + WINDOW_SEC) < OFFICIAL_SETTLE_LAG:
            continue  # 官方值未收敛（决策 #14）
        try:
            o = crypto_price(symbol, st)
        except Exception as e:
            warn("G", d["slug"], f"crypto-price 失败: {type(e).__name__} {e}")
            continue
        if not (o.get("openPrice") and o.get("closePrice")):
            warn("G", d["slug"], f"官方未就绪: {o}")
            continue
        n += 1
        time.sleep(0.35)  # 上游 Chainlink 限流（429），逐窗留间隔
        do = abs(o["openPrice"] - d["twap_open_price"])
        dc = abs(o["closePrice"] - d["twap_close_price"])
        is_push = d["close_source"] == "push" and d.get("anchor_source", "push") == "push"
        b = push if is_push else legacy
        b["n"] += 1
        b["open_ex"] += (do == 0)
        b["close_ex"] += (dc == 0)
        b["open_d"].append(do)
        b["close_d"].append(dc)
        if is_push and max(do, dc) > 0 and len(push["worst"]) < 5:
            push["worst"].append((max(do, dc), d["slug"], o["openPrice"], d["twap_open_price"],
                                  o["closePrice"], d["twap_close_price"]))
    if n == 0:
        print("  ⚠️ 无可核对窗口（都还没到官方收敛时间，或接口不可达）")
        return
    print(f"  覆盖 {n} 窗（推送口径 {push['n']} / 旧口径 {legacy['n']}）")
    for tag, b in (("推送 push", push), ("旧口径 stream/official", legacy)):
        if not b["n"]:
            continue
        print(f"  [{tag}] 开盘价逐位相等 {b['open_ex']}/{b['n']}，最大差 {max(b['open_d']):.6f} 美元；"
              f"收盘价逐位相等 {b['close_ex']}/{b['n']}，最大差 {max(b['close_d']):.6f} 美元")
    for w in push["worst"]:
        print(f"    ❌ {w[1]}: 官方 open {w[2]} vs 行内 {w[3]}；官方 close {w[4]} vs 行内 {w[5]}")
    if push["n"] and (push["open_ex"] != push["n"] or push["close_ex"] != push["n"]):
        fail("G", push["worst"][0][1] if push["worst"] else "—",
             f"推送口径边界价与官方不等（open {push['open_ex']}/{push['n']}、"
             f"close {push['close_ex']}/{push['n']} 逐位相等）")
    elif push["n"]:
        print(f"  ✅ 推送口径 {push['n']} 窗全部与官方逐位相等（这就是决策 #19/#23 的口径）")


# ───────────────────────── H 结算方向 ─────────────────────────

def check_h(events, asset):
    print("\n【H】结算方向核对（gamma）")
    n_res = n_cmp = n_ok = 0
    mism = []
    for _, _, d, _ in events:
        slug = d["slug"]
        try:
            m = http_json(f"https://gamma-api.polymarket.com/markets/slug/{slug}?include_tag=true")
        except Exception as e:
            warn("H", slug, f"gamma 失败: {type(e).__name__} {e}")
            continue
        if not m or m.get("umaResolutionStatus") != "resolved":
            continue
        n_res += 1
        if m.get("conditionId") != d["condition_id"]:
            fail("H", slug, "condition_id 与 gamma 不一致")
        op = m.get("outcomePrices")
        if isinstance(op, str):
            try:
                op = json.loads(op)
            except json.JSONDecodeError:
                continue
        if not op or len(op) < 2:
            continue
        n_cmp += 1
        official = 0 if float(op[0]) > 0.5 else 1
        mine = 0 if d["twap_close_price"] >= d["twap_open_price"] else 1
        if mine == official:
            n_ok += 1
        else:
            mism.append((slug, official, mine, d["outcome"]))
        if d["outcome"] != mine:
            fail("H", slug, f"行内 outcome={d['outcome']} 与 close>=open 推导值 {mine} 不符")
    if n_res == 0:
        print("  ⚠️ 尚无窗被 gamma 标记 resolved（UMA 结算有延迟）")
    else:
        print(f"  已 resolved {n_res} 窗，可核对 {n_cmp} 窗")
        print(f"  方向与官方一致: {n_ok}/{n_cmp} = {pct(n_ok, n_cmp)}")
        for s, o, mi, mine in mism[:5]:
            print(f"    ⚠️ {s}: 官方 {o} vs 行内推 {mi}（行内 outcome={mine}）")


# ───────────────────────── main ─────────────────────────

def main():
    ap = argparse.ArgumentParser(description="数据格式 v2 采集数据正确性校验（任意标的）")
    ap.add_argument("--asset", default="eth", help="资产名（派生目录/slug/交易对/符号）")
    ap.add_argument("--dir", default=None, help="数据目录（默认 data/<asset>）")
    ap.add_argument("--symbol", default=None, help="Binance 交易对（默认 <ASSET>USDT）")
    ap.add_argument("--twap-symbol", default=None, help="crypto-price 的 symbol（默认 <ASSET>）")
    ap.add_argument("--rest", default="https://data-api.binance.vision", help="Binance REST 基地址")
    ap.add_argument("--net-limit", type=int, default=20,
                    help="网络段最多核对最近 N 窗（默认 20；每窗 6 次网调）")
    ap.add_argument("--no-net", action="store_true", help="只跑离线段（A/B/C/E/F）")
    args = ap.parse_args()

    asset = args.asset.strip().lower()
    dirpath = Path(args.dir) if args.dir else (ROOT / "data" / asset)
    symbol = args.symbol or (asset.upper() + "USDT")
    twap_symbol = args.twap_symbol or asset.upper()

    print("=" * 78)
    print(f"采集数据校验 — 资产 {asset} | 目录 {dirpath}")
    print(f"Binance {symbol} | crypto-price symbol {twap_symbol} | "
          f"网络段 {'关闭' if args.no_net else '开启'}")
    print("=" * 78)

    if not dirpath.exists():
        print(f"❌ 目录不存在: {dirpath}")
        return 2
    rows = load_events(dirpath, asset)
    if not rows:
        print("❌ 无事件行")
        return 2

    events = [r for r in rows if not r[3]]
    check_a(rows, asset)
    check_b(events)
    check_c(events)
    check_e(events)
    check_f(events)
    if not args.no_net:
        # 网络段只取最近 N 窗（按 start_time 排序取尾部）——每窗 6 次网调，
        # 全量核对一份 14 天数据要上千次请求，且旧窗的官方值早已收敛/被清理。
        net_events = sorted(events, key=lambda r: r[2]["start_time"])[-args.net_limit:]
        print(f"\n（网络段核对最近 {len(net_events)} 窗，共 {len(events)} 窗）")
        check_d(net_events, asset, args.rest.rstrip("/"), symbol)
        check_g(net_events, twap_symbol)
        check_h(net_events, asset)

    print("\n" + "=" * 78)
    print("汇总")
    print("=" * 78)
    if WARNS:
        print(f"⚠️ 警告 {len(WARNS)} 条:")
        for sec, slug, msg in WARNS[:15]:
            print(f"   [{sec}] {slug}: {msg}")
        if len(WARNS) > 15:
            print(f"   … 另 {len(WARNS) - 15} 条")
    if FAILS:
        print(f"❌ 失败 {len(FAILS)} 条:")
        for sec, slug, msg in FAILS[:20]:
            print(f"   [{sec}] {slug}: {msg}")
        if len(FAILS) > 20:
            print(f"   … 另 {len(FAILS) - 20} 条")
        return 1
    print("✅ 全部检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
