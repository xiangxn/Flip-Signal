#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫尾盘：`dev` 的水平差污染——量级 / 阈值穿越归因 / 三种反事实（2026-10-10）

用户命题（另一个策略 b.md 的发现）：`币安现货 − Chainlink 指数` 这个差里混了一个
**日变的水平差 L**（两家数据源的水平偏移，与领先滞后无关）——不是零均值噪声、也不是
常数（14 天逐日中位数 +53.4 → −2.4 美元）；照原式直接实现，信号会带上恒定方向的偏置。

本脚本问同一件事在**扫尾盘**上的形态。tail 的入场腿是

    dev(t) = sgn·(spot − anchor)          sgn: 押 yes +1 / no −1（美元）

`spot` = 币安现货、`anchor` = 边界那一秒的 Chainlink TWAP-60 推送——(spot − anchor) 正是
b.md 的 basis 同族量（币安 − Chainlink）。恒等分解：

    dev(t) = sgn·(spot − binance_open)  +  sgn·(binance_open − anchor)
             └──── 同源位移 dev_bin ────┘     └──── 开盘水平差 offset0（含 b.md 的 L）─┘

`binance_open` = 边界那一分钟的币安 K 线开盘价（采集行顶层字段；决策 #27 红线：≤0 整窗
丢弃）⇒ offset0 **逐窗可见、不需要估计**。⚠️ 但 offset0 ≠ 纯 L：`anchor` 是**滞后 60 秒
的均价**，所以 offset0 里还含「开盘那一刻的即时价 − 该均价」= 开盘动量（真实量）。
这正是「同源重构」在 tail 上**不能照抄**的原因——三个反事实分开测：

    V0  现行            dev = sgn·(spot − anchor)
    V2  全量同源        dev_bin = sgn·(spot − binance_open)      （b.md 的代数抵消，最激进）
    V1  只扣滚动水平差   dev_roll = dev − sgn·L̂(t), L̂ = 前 ≤W 窗 offset0 的中位
                       （只杀「日变的水平」，保留开盘动量与窗内动力学；**因果**, 只用过去窗）

真正的 Chainlink 位移 `walk = sgn·(twap − anchor)` 本来就同源（Chainlink − Chainlink），
不在污染之列——这也是为什么本脚本只动 dev 腿。

⚠️ 本脚本**只做诊断**（不改引擎、不动 parity pin）：
  §0 自检：现行口径逐位复现 oracle 23 的 pin
  §1 水平差的量级：逐日表（对照 b.md 的 L 表）+ 持久性 + 窗内动力学
  §2 决策时刻归因：现行每个信号的 dev 有多少由 offset0 支撑（按段/侧/日）
  §3 反事实 V2：Δ P&L + 配对 bootstrap + 变动窗明细
  §4 敏感性：两套坐标各扫 dev 阈值（诊断用, 不用于挑阈值）
  §5 反事实 V1：滚动去水平（W 扫描）—— 决定「是水平差还是开盘动量在起作用」
  §6 逐日 / 分位诊断：offset0 自身携带结果信息吗
  §7 留一日（LOO）：Δ 的集中度
  §8 侧别偏向：水平差占主导时信号为什么挤向一侧（逐日占比 / 机会-成交转化 / 边际质量）
  §8.5 统计检验：偏向对**胜率**有没有影响（UP/DOWN 分侧 Wilson + Fisher 精确 + 日级配对 bootstrap）
  §8.6 「basis 很高、walk 很小」的单胜率是否相对高一档（2×2 分格 + 同段/同价带控制 + T=150 被闸批照买反事实）

用法: python/venv/bin/python python/v4/46_tail_dev_level_offset.py
"""
import sys
import random
import math
import datetime
import collections
import importlib.util
import statistics
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))
from v2.lib import load_events                                    # noqa: E402

STAKE = 2.0
MAX_LAT = 300
P_FLOOR = 0.80
DEV_USD = 63.0
SD_MIN_USD = 40.0
WALK_MIN_USD = 43.0
FLOOR_MIN_PRICE = 0.83
T150, T60 = 150, 60
STAGES = ("t150", "t60", "listen")

# oracle 23 的 pin（现行口径；本脚本 §0 逐位复现）
PIN_ROWS = 6704
PIN_SIG = 2074
PIN_WR = 97.830280
PIN_PL = 70.314372
PIN_WALK_LOW = 250
PIN_FLOOR_LOW = 24
PIN_WINDOWS = 3640

# b.md 的 L 逐日中位数（用户给的表；口径 = 当日 (币安现货 − Chainlink 指数) 中位数，美元）
BMD_L = {
    "2026-08-18": 53.4, "2026-08-19": 45.7, "2026-08-20": 44.8, "2026-08-21": 31.5,
    "2026-08-22": 13.1, "2026-08-23": 12.3, "2026-08-24": 13.9, "2026-08-25": 16.0,
    "2026-08-26": 11.6, "2026-08-27": 3.6,  "2026-08-28": -2.4, "2026-08-29": 2.5,
    "2026-08-30": 7.2,  "2026-08-31": 15.4,
}


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


s13 = load("s13", BASE / "13_tail_sweep.py")


# ── 数据提取（与 23.win_ticks 逐操作一致, 额外携带 binance_open）──────────────

def win_ticks(e):
    out = []
    anchor = e.get("twap_open_price")
    if not anchor:
        return out
    bopen = e.get("binance_open") or 0.0
    for x in e.get("ticks") or []:
        p = x.get("pm") or {}
        if not p or (p.get("book_latency_ms") or 0) > MAX_LAT:
            continue
        rem = x.get("rem")
        if rem is None or rem <= 0 or rem > T150:
            continue
        # 有效价门（决策 #21/#22, 2026-10-11 起全仓 python 与引擎同口径）
        yb, ya = p.get("yes_bid") or 0, p.get("yes_ask") or 0
        nb, na = p.get("no_bid") or 0, p.get("no_ask") or 0
        eff_yes, eff_no = (ya if ya > 0 else yb), (na if na > 0 else nb)
        if not (eff_yes > 0 or eff_no > 0):
            continue
        spot = (x.get("bin") or {}).get("price")
        if not spot:
            continue
        side = "yes" if eff_yes >= eff_no else "no"
        out.append({"rem": rem, "side": side,
                    "fill": (eff_yes if side == "yes" else eff_no),
                    "ya": eff_yes, "na": eff_no,   # §8 机会口径用（该侧**有效价**）
                    "spot": spot, "twap": (x.get("twap") or {}).get("price"),
                    "bopen": bopen})
    return out


def mkrow(t, anchor, sd, date, outcome, stage, dev_mode, lhat=0.0):
    """一行。dev_mode: 'spot' 现行；'bin' 同源位移（bopen≤0 退化回 spot）；
       'roll' = spot 口径 − sgn·lhat（lhat = 滚动水平差估计, 因果）。"""
    sgn = 1.0 if t["side"] == "yes" else -1.0
    if dev_mode == "bin" and t["bopen"] > 0:
        dev = sgn * (t["spot"] - t["bopen"])
    elif dev_mode == "roll":
        dev = sgn * (t["spot"] - anchor) - sgn * lhat
    else:
        dev = sgn * (t["spot"] - anchor)
    tw = t.get("twap")
    return {
        "date": date, "stage": stage, "rem": t["rem"], "side": t["side"],
        "fill": t["fill"], "buy": t["fill"],
        "dev": dev, "sd": sd, "sig": (dev / sd) if sd else None,
        "walk": (sgn * (tw - anchor)) if tw else None,
        "basis": (sgn * (t["spot"] - tw)) if tw else None,
        "offset": (sgn * (t["bopen"] - anchor)) if t["bopen"] > 0 else None,
        "leg": "dev63" if dev >= DEV_USD else "sigma",
        "settle_won": 1 if ((outcome == 0) if t["side"] == "yes" else (outcome == 1)) else 0,
    }


def dev_pass(r, dev_thr=DEV_USD, sd_thr=SD_MIN_USD):
    """dev 腿达标（现行 r5 的两条连续量腿之一）——返回 (是否过, 实际用的门槛)。"""
    if r["dev"] >= dev_thr:
        return True, dev_thr
    if r["sd"] is not None and r["sd"] >= sd_thr and r["sig"] is not None and r["sig"] >= 1.0:
        return True, r["sd"]
    return False, dev_thr


def r5(r, strict_price=False, dev_thr=DEV_USD, sd_thr=SD_MIN_USD):
    if not (r["fill"] > P_FLOOR if strict_price else r["fill"] >= P_FLOOR):
        return False
    return dev_pass(r, dev_thr, sd_thr)[0]


def r2(r, dev_thr=DEV_USD):
    return r["fill"] >= P_FLOOR and r["dev"] >= dev_thr


def walk_ok(r, x=WALK_MIN_USD):
    w = r.get("walk")
    return True if w is None else w >= x


def chain(ticks, anchor, sd, date, outcome, dev_mode="spot",
          dev_thr=DEV_USD, sd_thr=SD_MIN_USD, lhat=0.0, floor=FLOOR_MIN_PRICE):
    """与 23.chain 逐行同构；差别只有 dev 口径（含 lhat）与 dev 腿阈值。
    floor=0.0 = 关闭 #32/#33 地板（§8.6 复算 #29 当日世界用）。"""
    if not ticks:
        return [], []
    rows, rejects = [], []
    head = ticks[0]
    if head["rem"] > T60:
        r = mkrow(head, anchor, sd, date, outcome, "t150", dev_mode, lhat)
        ok5 = r5(r, strict_price=True, dev_thr=dev_thr, sd_thr=sd_thr)
        if ok5 and walk_ok(r):
            r["ok"] = True
            return [r], rejects
        rejects.append("t150:walk_low" if ok5 else "t150")
        rows.append(r)
        rest = [x for x in ticks if x["rem"] <= T60]
    else:
        rest = ticks
    shadowed = False
    if rest:
        t2 = rest[0]
        r2r = mkrow(t2, anchor, sd, date, outcome, "t60", dev_mode, lhat)
        if r5(r2r, dev_thr=dev_thr, sd_thr=sd_thr):
            if r2r["fill"] <= floor:
                r2r["reject_reason"] = "floor_low"
                rows.append(r2r)
                shadowed = True
            else:
                r2r["ok"] = True
                return rows + [r2r], rejects
        else:
            rejects.append("t60")
            rows.append(r2r)
        for x in rest[1:]:
            rl = mkrow(x, anchor, sd, date, outcome, "listen", dev_mode, lhat)
            if not r2(rl, dev_thr):
                continue
            if rl["fill"] <= floor:
                if not shadowed:
                    rl["reject_reason"] = "floor_low"
                    rows.append(rl)
                    shadowed = True
                continue
            rl["ok"] = True
            rows.append(rl)
            return rows, rejects
    return rows, rejects


def pl(rows):
    s = 0.0
    for r in rows:
        sh = STAKE / r["fill"]
        s += (sh - STAKE) if r["settle_won"] else -STAKE
    return s


def pl1(r):
    sh = STAKE / r["fill"]
    return (sh - STAKE) if r["settle_won"] else -STAKE


def boot_delta(base_by_day, new_by_day, n=2000, seed=46):
    """日级**配对** Δ 的 bootstrap 95% 区间（同 38 号口径；08-31 半天剔除同 23 的 ci）。"""
    days = sorted(set(list(base_by_day) + list(new_by_day)) - {"2026-08-31"})
    d = [new_by_day.get(k, 0.0) - base_by_day.get(k, 0.0) for k in days]
    rnd = random.Random(seed)
    out = []
    for _ in range(n):
        out.append(sum(d[rnd.randrange(len(d))] for _ in d))
    out.sort()
    return out[int(0.025 * n)], out[int(0.975 * n) - 1]


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def fm(x, nd=1):
    return "—" if x is None else f"{x:+.{nd}f}"


def run_roll(cached, W, dev_mode="roll", dev_thr=DEV_USD, sd_thr=SD_MIN_USD):
    """滚动去水平世界：按时间顺序扫一遍, L̂ = 前 ≤W 窗 offset0 的中位（因果, ≥12 才用）。"""
    sigs, by_day = [], collections.defaultdict(float)
    hist = collections.deque(maxlen=W)
    for ticks, anchor, sd, date, outcome, offset0, start in cached:
        lhat = statistics.median(hist) if (offset0 is not None and len(hist) >= 12) else 0.0
        rows, _ = chain(ticks, anchor, sd, date, outcome, dev_mode, dev_thr, sd_thr, lhat)
        for r in rows:
            if r.get("ok"):
                sigs.append(r)
                by_day[date] += pl1(r)
                break
        if offset0 is not None:
            hist.append(offset0)
    return sigs, by_day


def main():
    ev = load_events("data/btc")
    hr = s13.hist_ranges(ev)

    sig = {m: {s: [] for s in STAGES} for m in ("spot", "bin")}
    rows_n = {m: collections.Counter() for m in ("spot", "bin")}
    stat = {m: collections.Counter() for m in ("spot", "bin")}
    win_sig = {m: {} for m in ("spot", "bin")}
    win_day = {}
    wstat = []
    cached = []            # (ticks, anchor, sd, date, outcome, offset0, start)
    no_bopen = 0
    walklow_rows = []      # §8.6: T=150 段被 #29 闸拦下的判定行（「照买」反事实用）

    for e in ev:
        outcome, anchor = e.get("outcome"), e.get("twap_open_price")
        if outcome is None or not anchor:
            continue
        h = hr.get(e["start_time"])
        if h is None:
            stat["spot"]["no_sigma"] += 1
            continue
        sd = h / anchor * 1e4 * anchor / 1e4
        date = datetime.datetime.fromtimestamp(
            e["start_time"], datetime.timezone.utc).strftime("%Y-%m-%d")
        ticks = win_ticks(e)
        if not ticks:
            continue
        for m in ("spot", "bin"):
            stat[m]["windows"] += 1
        bopen = ticks[0]["bopen"]
        if bopen <= 0:
            no_bopen += 1
        diffs = [t["spot"] - t["twap"] for t in ticks if t["twap"]]
        offset0 = (bopen - anchor) if bopen > 0 else None
        wstat.append({
            "date": date, "start": e["start_time"], "offset0": offset0,
            "d_iqr": (statistics.quantiles(diffs, n=4)[2] -
                      statistics.quantiles(diffs, n=4)[0]) if len(diffs) >= 4 else None,
            "n": len(ticks), "up": 1 if outcome == 0 else 0,
            "hot": ticks[0]["side"],
            "hot_won": 1 if ((ticks[0]["side"] == "yes") == (outcome == 0)) else 0,
        })
        win_day[e["start_time"]] = date
        cached.append((ticks, anchor, sd, date, outcome, offset0, e["start_time"]))

        for m in ("spot", "bin"):
            rows, rej = chain(ticks, anchor, sd, date, outcome, m)
            stat[m]["walk_low"] += sum(1 for x in rej if x.endswith("walk_low"))
            if m == "spot":
                for r in rows:
                    if (r["stage"] == "t150" and not r.get("ok")
                            and r5(r, strict_price=True) and not walk_ok(r)):
                        r["_start"] = e["start_time"]
                        walklow_rows.append(r)
            stat[m]["floor_low"] += sum(1 for r in rows if r.get("reject_reason") == "floor_low")
            fin = None
            for r in rows:
                rows_n[m][r["stage"]] += 1
                if r.get("ok"):
                    sig[m][r["stage"]].append(r)
                    fin = r
            win_sig[m][e["start_time"]] = fin

    allsig = {m: [r for s in STAGES for r in sig[m][s]] for m in ("spot", "bin")}
    a = allsig["spot"]

    # ── §0 自检 ──────────────────────────────────────────────────────────────
    print("=" * 100)
    print("§0 自检：现行口径（dev = spot 口径）逐位复现 oracle 23 的 pin")
    print("=" * 100)
    wr = sum(r["settle_won"] for r in a) / len(a) * 100
    p = pl(a)
    checks = [
        ("全部行", sum(rows_n["spot"].values()), PIN_ROWS),
        ("信号数", len(a), PIN_SIG),
        ("WR(%)", round(wr, 6), PIN_WR),
        ("P&L(U)", round(p, 6), PIN_PL),
        ("walk_low 行", stat["spot"]["walk_low"], PIN_WALK_LOW),
        ("floor_low 行", stat["spot"]["floor_low"], PIN_FLOOR_LOW),
        ("参与判定窗数", stat["spot"]["windows"], PIN_WINDOWS),
    ]
    ok_all = True
    for lab, got, want in checks:
        tag = "✅" if got == want else "❌"
        if got != want:
            ok_all = False
        print(f"  {tag} {lab:<14} 现算 = {got:<12} pin = {want}")
    print(f"  {'✅ 口径一致，可继续' if ok_all else '❌ 口径漂移——后续数字不可信'}")
    if not ok_all:
        return
    print(f"  附带：binance_open ≤ 0 的窗 = {no_bopen}"
          f"（0 = 采集红线生效, 同源位移逐窗可算）")

    # ── §1 水平差的量级 ──────────────────────────────────────────────────────
    print()
    print("=" * 100)
    print("§1 水平差 offset0 = binance_open − anchor（美元）—— b.md 的 L 在本数据上的形态")
    print("=" * 100)
    offs = [w["offset0"] for w in wstat if w["offset0"] is not None]
    q1, q3 = statistics.quantiles(offs, n=4)[0], statistics.quantiles(offs, n=4)[2]
    print(f"  全样本 {len(offs)} 窗：中位 {fm(med(offs))}  均值 {fm(statistics.mean(offs))}"
          f"  p25 {fm(q1)}  p75 {fm(q3)}  >0 占比 {sum(1 for x in offs if x > 0)/len(offs)*100:.1f}%")
    dgrp = collections.defaultdict(list)
    for w in wstat:
        if w["offset0"] is not None:
            dgrp[w["date"]].append(w["offset0"])
    print()
    print(f"  {'日期':<12}{'窗数':>5}{'offset0中位':>12}{'offset0均值':>12}"
          f"{'b.md L 中位':>12}{'窗内(spot−twap)IQR中位':>22}")
    for d in sorted(dgrp):
        g = dgrp[d]
        giqr = med([w["d_iqr"] for w in wstat if w["date"] == d])
        print(f"  {d:<12}{len(g):>5}{med(g):>+12.1f}{statistics.mean(g):>+12.1f}"
              f"{BMD_L.get(d, float('nan')):>+12.1f}{giqr:>22.1f}")
    days_med = {d: med(g) for d, g in dgrp.items()}
    between = statistics.pvariance(list(days_med.values()))
    within = statistics.mean([statistics.pvariance(g) for g in dgrp.values()])
    print(f"\n  日均值方差(跨日) / 窗间方差(日内) = {between:.1f} / {within:.1f}"
          f"（√ 后 ≈ {between**0.5:.0f} / {within**0.5:.0f} 美元：水平差是日级量, "
          f"但窗间起伏也不小——它同时是「锚滞后 + 趋势」的即时读数）")
    xs, ys, prev = [], [], None
    for w in wstat:
        if w["offset0"] is None:
            prev = None
            continue
        if prev is not None and prev["date"] == w["date"]:
            xs.append(prev["offset0"])
            ys.append(w["offset0"])
        prev = w
    if len(xs) > 10:
        mx, my = statistics.mean(xs), statistics.mean(ys)
        cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        sx = sum((x - mx) ** 2 for x in xs) ** 0.5
        sy = sum((y - my) ** 2 for y in ys) ** 0.5
        print(f"  同日相邻窗 lag-1 相关（offset0）= {cov/(sx*sy):+.3f}（n={len(xs)}）")
    iqrs = [w["d_iqr"] for w in wstat if w["d_iqr"] is not None]
    print(f"  窗内 (spot−twap) 的 IQR 中位 = {med(iqrs):.1f} 美元（判定区 rem≤150 同宇宙 tick）"
          f"  vs  offset0 的跨窗 IQR = {q3-q1:.1f} 美元")

    # ── §2 决策时刻归因 ──────────────────────────────────────────────────────
    print()
    print("=" * 100)
    print(f"§2 现行信号（n={len(a)}）在决策 tick 上的分解：dev = 同源位移 dev_bin + 水平差 offset")
    print("=" * 100)
    print("  单位美元（已按持仓方向带符号）；「越线靠水平差」= dev 过腿的余量 < offset 的贡献")
    print()
    print(f"  {'分组':<14}{'n':>5}{'dev中位':>9}{'walk中位':>9}{'basis中位':>10}"
          f"{'offset中位':>11}{'dev_bin中位':>11}{'越线靠水平差':>14}")
    groups = [
        ("合计", a),
        ("yes 侧", [r for r in a if r["side"] == "yes"]),
        ("no 侧", [r for r in a if r["side"] == "no"]),
        ("t150 段", sig["spot"]["t150"]),
        ("t60 段", sig["spot"]["t60"]),
        ("listen 段", sig["spot"]["listen"]),
        ("dev63 腿", [r for r in a if r["leg"] == "dev63"]),
        ("sigma 腿", [r for r in a if r["leg"] == "sigma"]),
    ]
    for lab, g in groups:
        if not g:
            print(f"  {lab:<14}{0:>5}")
            continue
        n_l = sum(1 for r in g if r["offset"] is not None
                  and r["offset"] > r["dev"] - dev_pass(r)[1])
        dbin = med([r["dev"] - r["offset"] if r["offset"] is not None else None for r in g])
        print(f"  {lab:<14}{len(g):>5}{med([r['dev'] for r in g]):>+9.1f}"
              f"{med([r['walk'] for r in g]):>+9.1f}{med([r['basis'] for r in g]):>+10.1f}"
              f"{med([r['offset'] for r in g]):>+11.1f}{dbin:>+11.1f}"
              f"{n_l:>8} ({n_l/len(g)*100:4.1f}%)")
    print()
    print(f"  {'日期':<12}{'n信号':>6}{'当日offset0中位':>15}{'yes占比':>8}"
          f"{'越线靠水平差':>14}{'yes侧等效门槛':>13}{'fill中位':>9}")
    print("  （等效门槛 = 63 − 当日 offset0 中位：同源位移坐标下, 该日 yes 侧实际要移动多少美元）")
    dg = collections.defaultdict(list)
    for r in a:
        dg[r["date"]].append(r)
    for d in sorted(dg):
        g = dg[d]
        n_l = sum(1 for r in g if r["offset"] is not None
                  and r["offset"] > r["dev"] - dev_pass(r)[1])
        yshare = sum(1 for r in g if r["side"] == "yes") / len(g) * 100
        eq = DEV_USD - days_med[d]
        print(f"  {d:<12}{len(g):>6}{days_med[d]:>+15.1f}{yshare:>7.0f}%"
              f"{f'{n_l} ({n_l/len(g)*100:.0f}%)':>14}{eq:>+11.0f} 美元"
              f"{med([r['fill'] for r in g]):>9.3f}")

    # ── §3 反事实 V2（全量同源）─────────────────────────────────────────────
    print()
    print("=" * 100)
    print("§3 反事实 V2：dev → dev_bin = sgn·(spot − binance_open)，其余规则/阈值一字不动")
    print("=" * 100)
    b = allsig["bin"]
    wrb = sum(r["settle_won"] for r in b) / len(b) * 100
    pb = pl(b)
    print(f"  {'段':<10}{'现行 n':>8}{'同源 n':>8}{'现行 P&L':>11}{'同源 P&L':>11}"
          f"{'Δn':>6}{'ΔU':>9}")
    for s in STAGES:
        n0, n1 = len(sig["spot"][s]), len(sig["bin"][s])
        p0, p1 = pl(sig["spot"][s]), pl(sig["bin"][s])
        print(f"  {s:<10}{n0:>8}{n1:>8}{p0:>+11.2f}{p1:>+11.2f}{n1-n0:>+6d}{p1-p0:>+9.2f}")
    print(f"  {'合计':<10}{len(a):>8}{len(b):>8}{p:>+11.2f}{pb:>+11.2f}"
          f"{len(b)-len(a):>+6d}{pb-p:>+9.2f}")
    print(f"  WR：现行 {wr:.6f}% → 同源 {wrb:.6f}%")
    base_day = collections.defaultdict(float)
    new_day = collections.defaultdict(float)
    for st, r in win_sig["spot"].items():
        if r is not None:
            base_day[win_day[st]] += pl1(r)
    for st, r in win_sig["bin"].items():
        if r is not None:
            new_day[win_day[st]] += pl1(r)
    lo, hi = boot_delta(base_day, new_day)
    print(f"  日级配对 Δ 的 95% bootstrap 区间：[{lo:+.2f}, {hi:+.2f}]"
          f"  （{'含零——与现行不可区分' if lo <= 0 <= hi else '不含零'}）")
    print()
    ch = collections.Counter()
    drop_pl, add_pl, reroute_pl = 0.0, 0.0, 0.0
    drop_row, add_row, reroute_row = [], [], []
    for st in win_sig["spot"]:
        r0, r1 = win_sig["spot"][st], win_sig["bin"][st]
        if r0 is None and r1 is None:
            continue
        if r0 is not None and r1 is None:
            ch["丢单（现行有, 同源无）"] += 1
            drop_pl += pl1(r0)
            drop_row.append(r0)
        elif r0 is None and r1 is not None:
            ch["新增（同源有, 现行无）"] += 1
            add_pl += pl1(r1)
            add_row.append(r1)
        elif r0["stage"] != r1["stage"] or abs(r0["fill"] - r1["fill"]) > 1e-12:
            ch["同窗但段/价变（重入路径不同）"] += 1
            reroute_pl += pl1(r1) - pl1(r0)
            reroute_row.append((r0, r1))
        else:
            ch["逐位相同"] += 1
    for k, v in ch.most_common():
        print(f"  {k:<26}{v:>6}")
    print(f"  └ 丢单那批（按现行世界计）P&L = {drop_pl:+.2f}U；新增那批（按同源世界计）"
          f"P&L = {add_pl:+.2f}U；同窗重入差 = {reroute_pl:+.2f}U")
    if drop_row:
        dy = [r for r in drop_row if r["side"] == "yes"]
        dn = [r for r in drop_row if r["side"] == "no"]
        print(f"    丢单 n={len(drop_row)}：yes {len(dy)} / no {len(dn)}；"
              f"输 {sum(1 for r in drop_row if not r['settle_won'])}；"
              f"fill 中位 {med([r['fill'] for r in drop_row]):.3f}；"
              "段 " + "/".join(f"{s}:{sum(1 for r in drop_row if r['stage']==s)}" for s in STAGES))
        print(f"      分侧：yes P&L {pl(dy):+.2f}U（fill 中位 {med([r['fill'] for r in dy]):.3f}）"
              f" / no P&L {pl(dn):+.2f}U（fill 中位 {med([r['fill'] for r in dn]):.3f}）")
    if add_row:
        ay = [r for r in add_row if r["side"] == "yes"]
        an = [r for r in add_row if r["side"] == "no"]
        print(f"    新增 n={len(add_row)}：yes {len(ay)} / no {len(an)}；"
              f"输 {sum(1 for r in add_row if not r['settle_won'])}；"
              f"fill 中位 {med([r['fill'] for r in add_row]):.3f}；"
              "段 " + "/".join(f"{s}:{sum(1 for r in add_row if r['stage']==s)}" for s in STAGES))
        print(f"      分侧：yes P&L {pl(ay):+.2f}U（fill 中位 {med([r['fill'] for r in ay]):.3f}）"
              f" / no P&L {pl(an):+.2f}U（fill 中位 {med([r['fill'] for r in an]):.3f}）")
    if reroute_row:
        f0 = med([a0["fill"] for a0, _ in reroute_row])
        f1 = med([a1["fill"] for _, a1 in reroute_row])
        m0 = statistics.mean([a0["fill"] for a0, _ in reroute_row])
        m1 = statistics.mean([a1["fill"] for _, a1 in reroute_row])
        costlier = sum(1 for a0, a1 in reroute_row if a1["fill"] > a0["fill"] + 1e-12)
        later = sum(1 for a0, a1 in reroute_row
                    if STAGES.index(a1["stage"]) > STAGES.index(a0["stage"]))
        flip = sum(1 for a0, a1 in reroute_row if a0["settle_won"] != a1["settle_won"])
        print(f"    改道 n={len(reroute_row)}：fill 中位 {f0:.3f} → {f1:.3f}、"
              f"均值 {m0:.3f} → {m1:.3f}；同源更贵 {costlier} 笔、更晚段 {later} 笔、"
              f"胜负翻转 {flip} 笔")

    # ── §4 敏感性（诊断用）──────────────────────────────────────────────────
    print()
    print("=" * 100)
    print("§4 敏感性：两套坐标各扫一遍 dev 腿阈值（诊断用, 不用于挑阈值）")
    print("=" * 100)
    print("  说明：现行 63 是在**被污染的 dev** 上标定的；换同源位移后同一个 63 的含义变了"
          "（yes 侧更严、no 侧更松）。下表看 Δ 是否对阈值敏感。")
    print()
    print(f"  {'口径':<24}{'dev阈值':>8}{'σ阈值':>7}{'n':>7}{'P&L':>10}{'ΔU vs 现行':>12}")
    for mode, lab in (("spot", "现行 dev（被污染）"), ("bin", "同源 dev_bin")):
        for dt in (50.0, 56.0, 63.0, 70.0):
            n_ = 0
            p_ = 0.0
            for ticks, anchor, sd, date, outcome, offset0, start in cached:
                rows, _ = chain(ticks, anchor, sd, date, outcome, mode, dt, SD_MIN_USD)
                for r in rows:
                    if r.get("ok"):
                        n_ += 1
                        p_ += pl1(r)
            print(f"  {lab:<24}{dt:>8.0f}{SD_MIN_USD:>7.0f}{n_:>7}{p_:>+10.2f}{p_-p:>+12.2f}")

    # ── §5 反事实 V1（滚动去水平——只杀「日变水平」）────────────────────────
    print()
    print("=" * 100)
    print("§5 反事实 V1：dev_roll = dev − sgn·L̂(t)，L̂ = 前 ≤W 窗 offset0 的中位（因果, ≥12 窗）")
    print("=" * 100)
    print("  V2 把 offset0（水平差 + 开盘动量）整个扣掉；V1 只扣它的**慢变分量**。")
    print("  若 V1 ≈ 现行 ⇒ 被扣的水平差不承重（真正承重的是开盘动量, 那是真信息）;")
    print("  若 V1 ≈ V2 ⇒ 「日变水平」本身在承重（b.md 式担忧成立）。")
    print()
    print(f"  {'口径':<28}{'W(窗)':>7}{'n':>7}{'P&L':>10}{'ΔU vs 现行':>12}{'日级配对 95% CI':>24}")
    roll_by_day = {}
    roll_sigs = {}
    for W in (12, 36, 72):
        rs, rb = run_roll(cached, W)
        lo2, hi2 = boot_delta(base_day, rb)
        print(f"  {'V1 滚动去水平（dev_roll）':<28}{W:>7}{len(rs):>7}{pl(rs):>+10.2f}"
              f"{pl(rs)-p:>+12.2f}{f'[{lo2:+.2f}, {hi2:+.2f}]':>24}")
        roll_by_day[W] = rb
        roll_sigs[W] = rs
    print(f"  {'V2 全量同源（dev_bin）':<28}{'—':>7}{len(b):>7}{pb:>+10.2f}{pb-p:>+12.2f}"
          f"{f'[{lo:+.2f}, {hi:+.2f}]':>24}")
    print(f"  {'V0 现行':<28}{'—':>7}{len(a):>7}{p:>+10.2f}{0.0:>+12.2f}")
    W0 = 36          # 主用窗口 = 前 3 小时（§6/§7 用这一档）
    print()
    print("  按侧分解（ΔU = 新世界 − 现行；两个世界的方向由市场价定, 差别只在 dev 腿的放行）")
    print(f"  {'口径':<22}{'yes n':>7}{'yes P&L':>10}{'no n':>7}{'no P&L':>10}"
          f"{'ΔU yes':>9}{'ΔU no':>9}")
    for lab, g in (("V0 现行", a), ("V2 全量同源", b), ("V1(36) 滚动", roll_sigs[W0])):
        gy = [r for r in g if r["side"] == "yes"]
        gn = [r for r in g if r["side"] == "no"]
        ay = [r for r in a if r["side"] == "yes"]
        an = [r for r in a if r["side"] == "no"]
        print(f"  {lab:<22}{len(gy):>7}{pl(gy):>+10.2f}{len(gn):>7}{pl(gn):>+10.2f}"
              f"{pl(gy)-pl(ay):>+9.2f}{pl(gn)-pl(an):>+9.2f}")

    # ── §6 逐日 / 分位诊断 ──────────────────────────────────────────────────
    print()
    print("=" * 100)
    print("§6 逐日诊断：offset0 自身携带结果信息吗（窗口级）")
    print("=" * 100)
    print(f"  {'日期':<12}{'窗数':>5}{'Up占比':>8}{'offset0中位':>12}{'现行P&L':>10}"
          f"{'V2 ΔU':>9}{'V1(36) ΔU':>11}")
    base_d = collections.defaultdict(float)
    new_d = collections.defaultdict(float)
    roll_d = collections.defaultdict(float)
    for st, r in win_sig["spot"].items():
        if r is not None:
            base_d[win_day[st]] += pl1(r)
    for st, r in win_sig["bin"].items():
        if r is not None:
            new_d[win_day[st]] += pl1(r)
    roll_d = roll_by_day[W0]
    for d in sorted(dgrp):
        g = dgrp[d]
        upd = sum(1 for w in wstat if w["date"] == d and w["up"]) / len(g) * 100
        print(f"  {d:<12}{len(g):>5}{upd:>7.0f}%{med(g):>+12.1f}{base_d.get(d, 0.0):>+10.2f}"
              f"{new_d.get(d, 0.0)-base_d.get(d, 0.0):>+9.2f}"
              f"{roll_d.get(d, 0.0)-base_d.get(d, 0.0):>+11.2f}")
    print()
    # offset0 五分位（窗口级）
    srt = sorted([w for w in wstat if w["offset0"] is not None], key=lambda w: w["offset0"])
    print("  offset0 五分位（全部参与判定窗；现行信号与 P&L 落到各行）")
    print("  「热侧胜率」= 市场挑的热侧与结果同向的比例——它若随 offset0 上行, 说明 offset0")
    print("  携带**市场没吃进价格**的信息；若基本持平, 则 offset0 只是与「哪侧是热侧」同步。")
    print(f"  {'档':<8}{'n窗':>6}{'offset0范围':>18}{'Up占比':>8}{'yes热占比':>10}"
          f"{'热侧胜率':>9}{'现行信号':>9}{'现行P&L':>10}")
    k = len(srt) // 5
    sig_by_start = {st: r for st, r in win_sig["spot"].items()}
    for i in range(5):
        part = srt[i * k:(i + 1) * k] if i < 4 else srt[4 * k:]
        starts = set(w["start"] for w in part)
        ss = [r for st, r in sig_by_start.items() if st in starts and r is not None]
        upd = sum(w["up"] for w in part) / len(part) * 100
        yhot = sum(1 for w in part if w["hot"] == "yes") / len(part) * 100
        hwr = sum(w["hot_won"] for w in part) / len(part) * 100
        rng = f"[{part[0]['offset0']:+.0f}, {part[-1]['offset0']:+.0f}]"
        print(f"  Q{i+1:<7}{len(part):>6}{rng:>18}{upd:>7.0f}%{yhot:>9.0f}%{hwr:>8.0f}%"
              f"{len(ss):>9}{pl(ss):>+10.2f}")

    # ── §7 留一日（LOO）─────────────────────────────────────────────────────
    print()
    print("=" * 100)
    print("§7 留一日（leave-one-day-out）：Δ 的集中度")
    print("=" * 100)
    for lab, nd in (("V2 同源", new_d), (f"V1(36) 滚动", roll_d)):
        tot = sum(nd.values()) - sum(base_d.values())
        excs = {d: tot - (nd.get(d, 0.0) - base_d.get(d, 0.0)) for d in base_d}
        flip = [d for d, x in excs.items() if (x > 0) != (tot > 0)]
        print(f"  {lab:<12} 总 Δ {tot:+.2f}U；剔任一日后 Δ 范围 "
              f"[{min(excs.values()):+.2f}, {max(excs.values()):+.2f}]；"
              f"剔某日后变号的天数 = {len(flip)}/{len(excs)}"
              f"{('（' + ','.join(sorted(flip)) + '）') if flip else ''}")

    # ── §8 侧别偏向（用户 2026-10-10 追问）────────────────────────────────────
    print()
    print("=" * 100)
    print("§8 侧别偏向：水平差/basis 占主导时, 信号为什么挤向一侧——算术必然吗、携带结果信息吗")
    print("=" * 100)
    print("  恒等式：dev ≥ 63 ⇔ dev_bin ≥ 63 − offset（逐信号精确；offset = sgn·(binance_open − anchor)）。")
    print("  ⇒ offset>0 的一侧门槛被水平差压低、另一侧被抬高；「另一侧几乎没信号」= 那侧要凑出")
    print("  63+|offset| 的同源位移才算数。另：任何信号的侧别恒等于 sign(spot − anchor)（dev>0 恒真）。")
    print()

    # §8.1 逐日：侧别占比随当日水平差打摆 + 两侧等效门槛
    print("  §8.1 逐日：信号侧别占比 vs 当日 offset0 中位（门槛 = 63 ∓ 当日 offset0 中位, 美元）")
    print(f"  {'日期':<12}{'n信号':>6}{'yes占比':>8}{'offset0中位':>12}{'yes门槛':>9}"
          f"{'no门槛':>8}{'当日P&L':>9}{'V2 n(yes%)':>12}")
    yes_share_d, L_d = {}, {}
    for d in sorted(dg):
        g = dg[d]
        L = days_med[d]
        ny = sum(1 for r in g if r["side"] == "yes")
        gb = [r for st, r in win_sig["bin"].items() if r is not None and win_day[st] == d]
        yb = sum(1 for r in gb if r["side"] == "yes")
        yes_share_d[d], L_d[d] = ny / len(g) * 100, L
        v2s = f"{len(gb)} ({yb/len(gb)*100:.0f}%)" if gb else "0"
        print(f"  {d:<12}{len(g):>6}{ny/len(g)*100:>7.0f}%{L:>+12.1f}{DEV_USD-L:>+9.0f}"
              f"{DEV_USD+L:>+8.0f}{base_d.get(d, 0.0):>+9.2f}{v2s:>12}")
    xs = [L_d[d] for d in sorted(dg)]
    ys = [yes_share_d[d] for d in sorted(dg)]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = sum((x - mx) ** 2 for x in xs) ** 0.5
    sy2 = sum((y - my) ** 2 for y in ys) ** 0.5
    print(f"  逐日相关：offset0 中位 vs yes 信号占比 → r = {cov/(sx*sy2):+.3f}（n={len(xs)} 天）")

    # §8.2 窗口级：机会（价格世界）vs 成交——偏向是市场给的还是条子给的
    print()
    print("  §8.2 窗口级：机会（价格世界, 忽略 dev/walk 腿）vs 成交——转化率 = 该侧信号数 / 该侧机会窗数")
    print("  （机会 = 该侧有效价在 rem>60 时 >0.80、rem≤60 时 >0.83 的 tick 存在；有效价门宇宙）")
    opp = {}
    for ticks_, anchor_, sd_, date_, outcome_, offset0_, start_ in cached:
        oy = on = False
        for t in ticks_:
            bar = P_FLOOR if t["rem"] > T60 else FLOOR_MIN_PRICE
            if t["ya"] > bar:
                oy = True
            if t["na"] > bar:
                on = True
        opp[start_] = (oy, on)
    print(f"  {'档':<8}{'n窗':>6}{'yes热占比':>10}{'机会yes':>8}{'机会no':>8}{'现yes%':>8}"
          f"{'V2yes%':>8}{'转化yes':>9}{'转化no':>9}{'热侧胜率':>9}")
    for i in range(5):
        part = srt[i * k:(i + 1) * k] if i < 4 else srt[4 * k:]
        starts = set(w["start"] for w in part)
        ss = [r for st, r in win_sig["spot"].items() if st in starts and r is not None]
        sb = [r for st, r in win_sig["bin"].items() if st in starts and r is not None]
        noy = sum(1 for w in part if opp.get(w["start"], (False, False))[0])
        non = sum(1 for w in part if opp.get(w["start"], (False, False))[1])
        sy_ = sum(1 for r in ss if r["side"] == "yes")
        by_ = sum(1 for r in sb if r["side"] == "yes")
        yhot = sum(1 for w in part if w["hot"] == "yes") / len(part) * 100
        hwr = sum(w["hot_won"] for w in part) / len(part) * 100
        cy = f"{sy_/noy*100:.1f}%" if noy else "—"
        cn = f"{(len(ss)-sy_)/non*100:.1f}%" if non else "—"
        py = f"{sy_/len(ss)*100:.0f}%" if ss else "—"
        pb_ = f"{by_/len(sb)*100:.0f}%" if sb else "—"
        print(f"  Q{i+1:<7}{len(part):>6}{yhot:>9.0f}%{noy:>8}{non:>8}{py:>8}"
              f"{pb_:>8}{cy:>9}{cn:>9}{hwr:>8.0f}%")
    oppc = collections.Counter()
    for w in wstat:
        oy, on = opp.get(w["start"], (False, False))
        oppc["y"] += oy
        oppc["n"] += on
        oppc["both"] += (oy and on)
        oppc["neither"] += (not oy and not on)
    n_yes_a = sum(1 for r in a if r["side"] == "yes")
    by_all = sum(1 for r in b if r["side"] == "yes")
    print(f"  全样本：机会 yes {oppc['y']} 窗 / no {oppc['n']} 窗（两侧都有 {oppc['both']}、"
          f"都没有 {oppc['neither']}）；现行 yes {n_yes_a} / no {len(a)-n_yes_a}"
          f"（转化 {n_yes_a/oppc['y']*100:.1f}% / {(len(a)-n_yes_a)/oppc['n']*100:.1f}%）；"
          f"V2 yes {by_all} / no {len(b)-by_all}")

    # §8.3 边际交易质量
    print()
    print("  §8.3 边际交易质量：同侧内【顺风 offset>0（水平差帮它过线）】vs【逆风 offset≤0（顶着过线）】")
    print(f"  {'格':<14}{'n':>5}{'WR%':>7}{'U/笔':>8}{'P&L':>8}{'fill中位':>9}{'fill均值':>9}{'dev_bin中位':>12}")
    for side in ("yes", "no"):
        for lab, wind in (("顺风", True), ("逆风", False)):
            g = [r for r in a if r["side"] == side and ((r["offset"] or 0.0) > 0) == wind]
            if not g:
                print(f"  {side + ' ' + lab:<14}{0:>5}")
                continue
            w_ = sum(r["settle_won"] for r in g) / len(g) * 100
            db = med([r["dev"] - r["offset"] for r in g if r["offset"] is not None])
            print(f"  {side + ' ' + lab:<14}{len(g):>5}{w_:>7.2f}{pl(g)/len(g):>+8.3f}"
                  f"{pl(g):>+8.2f}{med([r['fill'] for r in g]):>9.3f}"
                  f"{statistics.mean([r['fill'] for r in g]):>9.3f}{db:>+12.1f}")
    print()
    print("  越线靠水平差（dev 过腿余量 < offset 贡献）的两批 + 同侧其余对照")
    print(f"  {'批':<14}{'n':>5}{'WR%':>7}{'U/笔':>8}{'P&L':>8}{'fill中位':>9}{'fill均值':>9}")
    for side in ("yes", "no"):
        g = [r for r in a if r["side"] == side]
        gl = [r for r in g if r["offset"] is not None and r["offset"] > r["dev"] - dev_pass(r)[1]]
        g0 = [r for r in g if not (r["offset"] is not None and r["offset"] > r["dev"] - dev_pass(r)[1])]
        for lab, gg in ((f"{side} L-driven", gl), (f"{side} 其余", g0)):
            if not gg:
                continue
            w_ = sum(r["settle_won"] for r in gg) / len(gg) * 100
            print(f"  {lab:<14}{len(gg):>5}{w_:>7.2f}{pl(gg)/len(gg):>+8.3f}{pl(gg):>+8.2f}"
                  f"{med([r['fill'] for r in gg]):>9.3f}"
                  f"{statistics.mean([r['fill'] for r in gg]):>9.3f}")
    dy_ = med([r["dev"] - r["offset"] for r in a if r["side"] == "yes" and r["offset"] is not None])
    dn_ = med([r["dev"] - r["offset"] for r in a if r["side"] == "no" and r["offset"] is not None])
    print(f"\n  两侧同源位移中位差 = {dn_ - dy_:+.1f} 美元（no 侧为过同一条 63 线, 要比 yes 侧多走的"
          f"同源位移）；≈ 2 × offset0 全样本中位 {2*med(offs):+.1f}（门槛差恒等为 2×offset, 同量级即核对成立）")

    # §8.5 统计推断（用户 2026-10-10 追问：「这个偏差对胜率是否有影响？分别统计 UP、DOWN 的胜率」）
    print()
    print("  §8.5 统计检验：偏向对胜率有没有影响（Wilson 95% 区间 / 双侧 Fisher 精确 / 日级配对 bootstrap）")
    print("  （口径：p = 双侧 Fisher 精确；bootstrap = 日级配对重采样 2000 次、种子 46、剔 08-31 半天；胜率单位 %）")

    def wilson(w, n, z=1.96):
        """Wilson 95% 区间（w 胜 / n 总），返回百分比 (lo, hi)。"""
        if n == 0:
            return 0.0, 0.0
        p = w / n
        d = 1 + z * z / n
        c = p + z * z / (2 * n)
        h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
        return (c - h) / d * 100, (c + h) / d * 100

    def fisher(w1, n1, w2, n2):
        """两样本 2×2 双侧 Fisher 精确 p 值（超几何求和，含 prob ≤ 观测×1.0000001 的全部表）。"""
        tot, wins = n1 + n2, w1 + w2

        def lc(n, kk):
            if kk < 0 or kk > n:
                return float("-inf")
            return math.lgamma(n + 1) - math.lgamma(kk + 1) - math.lgamma(n - kk + 1)

        def pr(x):
            return math.exp(lc(wins, x) + lc(tot - wins, n1 - x) - lc(tot, n1))

        p_obs = pr(w1)
        s = 0.0
        for x in range(max(0, n1 - (tot - wins)), min(n1, wins) + 1):
            p = pr(x)
            if p <= p_obs * (1 + 1e-7):
                s += p
        return min(1.0, s)

    def wr_of(rows):
        return sum(r["settle_won"] for r in rows), len(rows)

    def show(lab, rows, ind=2):
        """一行：n / 胜 / WR% / Wilson 95%。"""
        w, n = wr_of(rows)
        if n == 0:
            print(f"  {' ' * ind}{lab:<18}{0:>5}")
            return
        lo, hi = wilson(w, n)
        print(f"  {' ' * ind}{lab:<18}{n:>5}{w:>4}{w / n * 100:>8.2f}{f'[{lo:.2f}, {hi:.2f}]':>18}")

    def side_rows(rows, side, stage=None):
        return [r for r in rows if r["side"] == side and (stage is None or r["stage"] == stage)]

    def wind_rows(rows, side, wind):
        """侧别 × 风向（True = 顺风 offset>0；offset 缺省按 0 归逆风, 与 §8.3 同口径）。"""
        return [r for r in rows if r["side"] == side and ((r["offset"] or 0.0) > 0) == wind]

    def ldr(r):
        return r["offset"] is not None and r["offset"] > r["dev"] - dev_pass(r)[1]

    def boot_wr_delta(rows1, rows2, n=2000, seed=46):
        """日级配对 bootstrap：两臂共享日期重采样 → ΔWR = WR(臂1)−WR(臂2)（pp）的 95% 区间。"""
        d1, d2 = {}, {}
        for r in rows1:
            x = d1.setdefault(r["date"], [0, 0])
            x[0] += r["settle_won"]
            x[1] += 1
        for r in rows2:
            x = d2.setdefault(r["date"], [0, 0])
            x[0] += r["settle_won"]
            x[1] += 1
        days = sorted((set(d1) | set(d2)) - {"2026-08-31"})
        rnd = random.Random(seed)
        out = []
        for _ in range(n):
            wa = na = wb = nb = 0
            for _i in range(len(days)):
                dd = days[rnd.randrange(len(days))]
                wa += d1.get(dd, [0, 0])[0]
                na += d1.get(dd, [0, 0])[1]
                wb += d2.get(dd, [0, 0])[0]
                nb += d2.get(dd, [0, 0])[1]
            if na and nb:
                out.append((wa / na - wb / nb) * 100)
        out.sort()
        return out[int(0.025 * len(out))], out[int(0.975 * len(out)) - 1]

    print()
    print("  (a) UP（押 yes）vs DOWN（押 no）胜率：全样本 / 分段, Wilson 95% 区间")
    print(f"  {'组':<20}{'n':>5}{'胜':>4}{'WR%':>8}{'Wilson 95%':>18}")
    show("全样本", a)
    show("UP (yes)", side_rows(a, "yes"))
    show("DOWN (no)", side_rows(a, "no"))
    for st in STAGES:
        show(f"  {st}: UP", side_rows(a, "yes", st), ind=4)
        show(f"  {st}: DOWN", side_rows(a, "no", st), ind=4)
    wy, ny = wr_of(side_rows(a, "yes"))
    wn, nn = wr_of(side_rows(a, "no"))
    print(f"  Fisher UP vs DOWN（全样本）：输 {ny - wy}/{ny} vs {nn - wn}/{nn} → "
          f"p = {fisher(wy, ny, wn, nn):.3f}（UP {wy/ny*100:.2f}% vs DOWN {wn/nn*100:.2f}%）")
    for st in STAGES:
        wy_, ny_ = wr_of(side_rows(a, "yes", st))
        wn_, nn_ = wr_of(side_rows(a, "no", st))
        if not ny_ or not nn_:
            print(f"  Fisher {st} 段内 UP vs DOWN：一侧无样本（UP {ny_} / DOWN {nn_}），跳过")
            continue
        print(f"  Fisher {st} 段内 UP vs DOWN：输 {ny_ - wy_}/{ny_} vs {nn_ - wn_}/{nn_} → "
              f"p = {fisher(wy_, ny_, wn_, nn_):.3f}（UP {wy_/ny_*100:.2f}% vs DOWN {wn_/nn_*100:.2f}%）")

    print()
    print("  (b) 四格（侧别 × 风向）：同侧 顺风 vs 逆风 的胜率差")
    for side in ("yes", "no"):
        w1, n1 = wr_of(wind_rows(a, side, True))
        w2, n2 = wr_of(wind_rows(a, side, False))
        print(f"  {side}: 顺风 {w1}/{n1} 胜 ({w1/n1*100:.2f}%) vs 逆风 {w2}/{n2} 胜 "
              f"({w2/n2*100:.2f}%) → Fisher p = {fisher(w1, n1, w2, n2):.3f}")

    print()
    print("  (c) L-driven（过线余量 < 水平差贡献，§8.3 口径）vs 同侧其余：胜率有无差")
    for side in ("yes", "no"):
        gl = [r for r in a if r["side"] == side and ldr(r)]
        g0 = [r for r in a if r["side"] == side and not ldr(r)]
        w1, n1 = wr_of(gl)
        w2, n2 = wr_of(g0)
        print(f"  {side}: L-driven {w1}/{n1} ({w1/n1*100:.2f}%) vs 其余 {w2}/{n2} "
              f"({w2/n2*100:.2f}%) → Fisher p = {fisher(w1, n1, w2, n2):.3f}")
    ga = [r for r in a if ldr(r)]
    gb = [r for r in a if not ldr(r)]
    w1, n1 = wr_of(ga)
    w2, n2 = wr_of(gb)
    lo, hi = boot_wr_delta(ga, gb)
    print(f"  池化: L-driven {w1}/{n1} ({w1/n1*100:.2f}%) vs 其余 {w2}/{n2} ({w2/n2*100:.2f}%) → "
          f"Fisher p = {fisher(w1, n1, w2, n2):.3f}；日级配对 ΔWR = {(w1/n1-w2/n2)*100:+.2f}pp, "
          f"95% [{lo:+.2f}, {hi:+.2f}]")

    print()
    print("  (d) 组成差：现行多收的（丢单批, §3）vs 同源多收的（新增批, §3）——偏向把什么样的单放进来了")
    w1, n1 = wr_of(drop_row)
    w2, n2 = wr_of(add_row)
    lo1, hi1 = wilson(w1, n1)
    lo2, hi2 = wilson(w2, n2)
    print(f"  丢单批 {w1}/{n1} 胜 ({w1/n1*100:.2f}%, Wilson [{lo1:.2f}, {hi1:.2f}]) vs "
          f"新增批 {w2}/{n2} 胜 ({w2/n2*100:.2f}%, Wilson [{lo2:.2f}, {hi2:.2f}]) → "
          f"Fisher p = {fisher(w1, n1, w2, n2):.3f}")

    print()
    print("  (e) 世界级：现行（V0）vs 全量同源（V2）总胜率；逐日水平差 vs 逐日胜率")
    wa, na_ = wr_of(a)
    wb, nb_ = wr_of(b)
    lo, hi = boot_wr_delta(a, b)
    print(f"  V0 {wa}/{na_} = {wa/na_*100:.4f}% vs V2 {wb}/{nb_} = {wb/nb_*100:.4f}% → "
          f"ΔWR = {(wa/na_-wb/nb_)*100:+.2f}pp（Fisher p = {fisher(wa, na_, wb, nb_):.3f}）；"
          f"日级配对 95% [{lo:+.2f}, {hi:+.2f}]")
    wr_d = {d: sum(r["settle_won"] for r in dg[d]) / len(dg[d]) * 100 for d in dg}
    ud = [base_d.get(d, 0.0) / len(dg[d]) for d in sorted(dg)]
    ysd = [wr_d[d] for d in sorted(dg)]
    mu = statistics.mean(ud)
    mw = statistics.mean(ysd)
    su = sum((y - mu) ** 2 for y in ud) ** 0.5
    sw = sum((y - mw) ** 2 for y in ysd) ** 0.5
    cu = sum((x - mx) * (y - mu) for x, y in zip(xs, ud))
    cw = sum((x - mx) * (y - mw) for x, y in zip(xs, ysd))
    print(f"  逐日（n={len(xs)} 天, L = 当日 offset0 中位）：L vs 当日胜率 r = {cw/sx/sw:+.3f}；"
          f"L vs 当日每笔 U r = {cu/sx/su:+.3f}（对照 §8.1: L vs yes 占比 r = {cov/sx/sy2:+.3f}）")
    print("  判读：UP/DOWN 与四格、L-driven 全平（p 大）⇒ 偏向筛的是**哪些单进场**（(d) 组成差），"
          "不是同批单的条件胜率；且 (d) 那点胜率优势改不成钱——§3 已证同源重构净 −38.75U（改道税吃掉）。")

    # §8.6 「basis 很高、walk 很小」的胜率现象（用户 2026-10-11 追问）
    print()
    print("=" * 100)
    print("§8.6 「basis 很高、walk 很小」的单，胜率是否相对高一档？（用户 2026-10-11 追问）")
    print("=" * 100)
    print("  口径: basis = sgn·(spot − twap)（领先缺口, 美元）、walk = sgn·(twap − anchor)（结算线已走的位移,"
          " 美元）, dev = basis + walk；")
    print("  分格线用现成的两条: walk < 43（入场闸阈值）、basis ≥ 63（dev 腿阈值）——不新扫参数。")
    print("  ⚠️ 现行世界 T=150 段被 #29 闸挡掉 walk<43 ⇒ 「walk 小」的信号几乎全在 t60/监听段（见 (a) 段构成列）。")

    def tag86(r):
        if r["walk"] is None or r["basis"] is None:
            return None
        if r["walk"] < WALK_MIN_USD:
            return "W小B高" if r["basis"] >= DEV_USD else "W小B低"
        return "W大B高" if r["basis"] >= DEV_USD else "W大B低"

    print()
    print(f"  (a) 现行信号（n={len(a)}）分格（行 = walk 档, 列 = basis 档）")
    print(f"  {'格':<8}{'n':>5}{'WR%':>7}{'Wilson95':>16}{'U/笔':>8}{'fill中位':>9}"
          f"{'basis中位':>10}{'walk中位':>9}{'dev中位':>9}{'段构成':>28}")
    cells86 = collections.OrderedDict((k, []) for k in ("W小B高", "W小B低", "W大B高", "W大B低"))
    miss86 = 0
    for r in a:
        t86 = tag86(r)
        if t86 is None:
            miss86 += 1
        else:
            cells86[t86].append(r)
    for lab, g in cells86.items():
        if not g:
            print(f"  {lab:<8}{0:>5}")
            continue
        w_, n_ = wr_of(g)
        lo_, hi_ = wilson(w_, n_)
        stg = "+".join(f"{s}:{sum(1 for r in g if r['stage'] == s)}" for s in STAGES
                       if any(r["stage"] == s for r in g))
        print(f"  {lab:<8}{n_:>5}{w_/n_*100:>7.2f}{f'[{lo_:.2f},{hi_:.2f}]':>16}"
              f"{pl(g)/n_:>+8.3f}{med([r['fill'] for r in g]):>9.3f}"
              f"{med([r['basis'] for r in g]):>+10.1f}{med([r['walk'] for r in g]):>+9.1f}"
              f"{med([r['dev'] for r in g]):>+9.1f}{stg:>28}")
    if miss86:
        print(f"  （twap 缺失、basis/walk 不可算: {miss86} 条）")

    A86 = cells86["W小B高"]
    rest86 = [r for r in a if tag86(r) not in (None, "W小B高")]
    print()
    print("  (b) 对照检验（双侧 Fisher；主格 = W小B高 = walk<43 ∧ basis≥63）")
    for lab, g1, g2 in (
            ("主格 vs 同 basis 档的 walk≥43", A86, cells86["W大B高"]),
            ("主格 vs 同 walk 档的 basis<63", A86, cells86["W小B低"]),
            ("主格 vs 其余全部", A86, rest86),
            ("basis≥63（两 walk 档合并）vs basis<63", A86 + cells86["W大B高"],
             cells86["W小B低"] + cells86["W大B低"])):
        w1, n1 = wr_of(g1)
        w2, n2 = wr_of(g2)
        if not (n1 and n2):
            continue
        print(f"  {lab}: {w1}/{n1} = {w1/n1*100:.2f}% vs {w2}/{n2} = {w2/n2*100:.2f}% → "
              f"p = {fisher(w1, n1, w2, n2):.3f}")
    if A86 and rest86:
        w1, n1 = wr_of(A86)
        w2, n2 = wr_of(rest86)
        lo_, hi_ = boot_wr_delta(A86, rest86)
        print(f"  日级配对 ΔWR（主格 − 其余）= {(w1/n1 - w2/n2)*100:+.2f}pp, "
              f"95% [{lo_:+.2f}, {hi_:+.2f}]")
    ld86 = [r for r in A86 if ldr(r)]
    if ld86:
        w1, n1 = wr_of(ld86)
        llo, lhi = wilson(w1, n1)
        print(f"  主格里 L-driven（§8.5 口径, 按 offset0）占 {len(ld86)}/{len(A86)}；"
              f"其胜率 {w1}/{n1} = {w1/n1*100:.2f}% Wilson [{llo:.2f},{lhi:.2f}]"
              f"（≈ 主格整体, 两个口径高度重合但都不是「更高胜率」）")
    print()
    print("  极端带（「walk 尤其小 / basis 尤其高」）:")
    for lab, pred in (
            ("walk<43 ∧ basis≥100", lambda r: r["walk"] is not None and r["walk"] < WALK_MIN_USD
             and r["basis"] is not None and r["basis"] >= 100),
            ("walk<0 ∧ basis≥63", lambda r: r["walk"] is not None and r["walk"] < 0
             and r["basis"] is not None and r["basis"] >= DEV_USD),
            ("walk<0（不限 basis）", lambda r: r["walk"] is not None and r["walk"] < 0)):
        g = [r for r in a if pred(r)]
        if not g:
            print(f"  {lab}: 0 条")
            continue
        w_, n_ = wr_of(g)
        lo_, hi_ = wilson(w_, n_)
        stg = "+".join(f"{s}:{sum(1 for r in g if r['stage'] == s)}" for s in STAGES
                       if any(r["stage"] == s for r in g))
        print(f"  {lab}: {w_}/{n_} = {w_/n_*100:.2f}% Wilson [{lo_:.2f},{hi_:.2f}] "
              f"fill中位 {med([r['fill'] for r in g]):.3f} 段 {stg}")

    print()
    print("  (c) 混淆控制：主格的高胜率是不是「后段 + 高 fill」构成的？同段内 / 同价带内再看一眼")
    for st in ("t60", "listen"):
        g1 = [r for r in A86 if r["stage"] == st]
        g2 = [r for r in a if r["stage"] == st and tag86(r) not in (None, "W小B高")]
        w1, n1 = wr_of(g1)
        w2, n2 = wr_of(g2)
        if not (n1 and n2):
            print(f"  {st} 段内: 主格 {n1} 条 / 同段其余 {n2} 条，样本不足跳过")
            continue
        print(f"  {st} 段内: 主格 {w1}/{n1} ({w1/n1*100:.2f}%) vs 同段其余 {w2}/{n2} "
              f"({w2/n2*100:.2f}%) → p = {fisher(w1, n1, w2, n2):.3f}")
    print(f"  {'fill 带':<14}{'主格 n':>7}{'WR%':>8}{'带内其余 n':>11}{'WR%':>8}{'p':>8}")
    for lo_, hi_ in ((0.80, 0.90), (0.90, 0.95), (0.95, 0.99), (0.99, 1.01)):
        g = [r for r in a if lo_ <= r["fill"] < hi_ and tag86(r) is not None]
        g1 = [r for r in g if tag86(r) == "W小B高"]
        g2 = [r for r in g if tag86(r) != "W小B高"]
        w1, n1 = wr_of(g1)
        w2, n2 = wr_of(g2)
        s1 = f"{w1/n1*100:.2f}" if n1 else "—"
        s2 = f"{w2/n2*100:.2f}" if n2 else "—"
        pp = f"{fisher(w1, n1, w2, n2):.3f}" if n1 and n2 else "—"
        print(f"  [{lo_:.2f},{hi_:.2f}){'':<4}{n1:>7}{s1:>8}{n2:>11}{s2:>8}{pp:>8}")

    print()
    print("  (d) T=150 反事实：「dev 过腿但 walk<43」被 #29 闸拦下的那批若照买"
          "（本族唯一有这类单的段——闸没拦的话它们就是 T150 信号）")
    dead86 = [r for r in walklow_rows if win_sig["spot"].get(r["_start"]) is None]
    rer86 = [r for r in walklow_rows if win_sig["spot"].get(r["_start"]) is not None]
    keep150 = [r for r in a if r["stage"] == "t150"]
    wk, nk = wr_of(keep150)
    print(f"  walk_low 行 {len(walklow_rows)}（§0 计数 {stat['spot']['walk_low']}）"
          f"，其中整窗死亡 {len(dead86)} / 改道重入 {len(rer86)}")
    print(f"  对照 T150 保留段（现行信号）: {wk}/{nk} = {wk/nk*100:.2f}%")
    for lab, g in (("全批照买", walklow_rows), ("其中整窗死亡那批", dead86), ("其中改道那批", rer86)):
        if not g:
            print(f"  {lab}: 0 条")
            continue
        w_, n_ = wr_of(g)
        lo_, hi_ = wilson(w_, n_)
        print(f"  {lab}: {w_}/{n_} = {w_/n_*100:.2f}% Wilson [{lo_:.2f},{hi_:.2f}] "
              f"fill中位 {med([r['fill'] for r in g]):.3f} U/笔 {pl(g)/n_:+.3f}")
    for lab, pred in (("basis≥63（缺口撑过线）", lambda r: r["basis"] is not None and r["basis"] >= DEV_USD),
                      ("basis<63（σ 腿那部分）", lambda r: r["basis"] is None or r["basis"] < DEV_USD),
                      ("basis≥100", lambda r: r["basis"] is not None and r["basis"] >= 100),
                      ("walk<0（结算线反向）", lambda r: r["walk"] is not None and r["walk"] < 0)):
        g = [r for r in walklow_rows if pred(r)]
        if not g:
            print(f"  {lab}: 0 条")
            continue
        w1, n1 = wr_of(g)
        lo1, hi1 = wilson(w1, n1)
        print(f"  {lab}: {w1}/{n1} = {w1/n1*100:.2f}% Wilson [{lo1:.2f},{hi1:.2f}] "
              f"U/笔 {pl(g)/n1:+.3f} fill中位 {med([r['fill'] for r in g]):.3f}")
    # 死亡/改道拆分在 #29 当日（无 #32/#33 地板）是什么样——复算对账文档里的 53/197
    wl_starts = set(r["_start"] for r in walklow_rows)
    pre_dead, pre_rer = [], []
    for ticks, anchor, sd, date, outcome, offset0, start in cached:
        if start not in wl_starts:
            continue
        rows, _ = chain(ticks, anchor, sd, date, outcome, "spot", floor=0.0)
        fin = next((x for x in rows if x.get("ok")), None)
        (pre_rer if fin else pre_dead).append(fin or next(x for x in rows if x["stage"] == "t150"))
    print(f"  同批窗在 #32/#33 地板之前（floor=0）复算: 整窗死亡 {len(pre_dead)}"
          f"（输 {sum(1 for r in pre_dead if not r['settle_won'])}） / 改道 {len(pre_rer)}"
          f" —— 与 #29 文档的 53/197 对账")
    w1, n1 = wr_of([r for r in walklow_rows if r["basis"] >= DEV_USD])
    w2, n2 = wr_of([r for r in walklow_rows if r["basis"] < DEV_USD])
    print(f"  被闸批内部按 basis 切: ≥63 的 {w1}/{n1} = {w1/n1*100:.2f}% vs <63 的 {w2}/{n2} = "
          f"{w2/n2*100:.2f}% → Fisher p = {fisher(w1, n1, w2, n2):.3f}（方向顺着用户假设, "
          f"但两边都远低于保留段 96.24%）")
    print("  判读：没有「basis 高 + walk 小 ⇒ 胜率相对高」这一现象。三层证据方向一致：① 现行信号里主格 "
          "96.89% 比其余 97.91% 略低（p=0.39, 不显著）；细分到段内, listen 段 3 个输单**全部**落在主格"
          "（p=0.013, 名义且方向相反）；极端带（basis≥100 / walk<0）更低（93~95%）。② T=150 段——"
          "这类单真正会被拦下的地方——照买 85.60%, 远低于保留段 96.24%：它不是好口袋, 是全族最差的之一"
          "（#29 闸的价值正来自避开它）。③ 唯一顺向痕迹 = 被闸批内部 basis 越高越好（83.3%→89.0%→15/15）, "
          "但整批仍在保留段之下、n=15 是事后切点 ⇒ 不构成可用口袋, 不改引擎、不据此调闸。")


if __name__ == "__main__":
    main()
