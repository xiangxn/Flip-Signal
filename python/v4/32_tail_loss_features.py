#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫尾盘：**输的特征挖掘**（2026-09-29）

动机（用户）：实盘 10U/注下，一笔输 = −10U，而一笔赢在 0.94 只值 +0.6U ⇒
「每天少亏一笔就是多赚 10U」。若输能**事前识别**，剔除它是廉价增益。

判据（本脚本用的唯一划算标准）：把某个子集整个剔掉, P&L 变化 = −(该子集的 P&L)。
等价的解析式：子集**盈亏平衡输率 λ* = 1 − 该子集平均成交价**（因为二元市场
`shares = stake/fill` ⇒ 赢 stake(1/fill − 1)、输 stake；令期望为 0 即 λ = 1 − fill）。
10U 本金、均价 0.9446 ⇒ λ* = 5.54%, 而基线输率 3.94% —— 门槛不高。

⚠️ **多重比较是这个分析的头号敌人**: 16 个特征 × ~5 桶 = ~80 格, 每格 n 只有几十,
光靠「某格看起来最差」必然能挑出一个假阳性。故：

  1. 每格给 Wilson 95% 区间（小 n 也不越界）与**剔除该格的实测 P&L + 日级 bootstrap 区间**；
  2. 全局置换检验：把输赢标签整体打乱 REPS 次, 每次重算**全部特征的最大 χ²** ⇒
     得到「最好看的那一格能好看到什么程度」的零分布, 观察值与之比才是 p 值；
  3. 头号候选做**分半稳定性**（前半/后半各自方向与量级）。

与 oracle 的一致性：本脚本自建 tick 列（多留原始盘口字段供抽特征）, 但**逐窗断言**
它与 `23_tail_integrated.py` 的 `win_ticks` 产出完全相同的信号行（stage/rem/side/fill），
不一致即报错退出 —— 特征是从与 oracle 同一批 tick 上抽的。

用法: python/venv/bin/python python/v4/32_tail_loss_features.py
"""
import sys
import json
import glob
import random
import datetime
import collections
import importlib.util
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))
from v2.lib import load_events                                    # noqa: E402

import numpy as np                                                # noqa: E402

SEED = 42
REPS = 2000
STAKE = 2.0                       # 回测口径（λ* 与 stake 无关, 只与成交价有关）
LIVE_STAKE = 10.0


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


s13 = load("s13", BASE / "13_tail_sweep.py")
s23 = load("s23", BASE / "23_tail_integrated.py")


# ── tick 列（与 s23.win_ticks 同判据, 额外保留原始盘口供抽特征）──────────────
def ticks_ex(e):
    out = []
    anchor = e.get("twap_open_price")
    if not anchor:
        return out
    for x in e.get("ticks") or []:
        p = x.get("pm") or {}
        if not p or (p.get("book_latency_ms") or 0) > s23.MAX_LAT:
            continue
        rem = x.get("rem")
        if rem is None or rem <= 0 or rem > s23.T150:
            continue
        if not all((p.get(k) or 0) > 0 for k in s23.FIELDS):
            continue
        spot = (x.get("bin") or {}).get("price")
        if not spot:
            continue
        ya, na = p.get("yes_ask") or 0, p.get("no_ask") or 0
        side = "yes" if ya >= na else "no"
        out.append({"rem": rem, "side": side, "fill": (ya if side == "yes" else na),
                    "spot": spot, "twap": (x.get("twap") or {}).get("price"),
                    "pm": p, "bin": x.get("bin") or {}, "raw": x})
    return out


def pl(r, stake=STAKE):
    """单笔 P&L（赢 shares−stake / 输 −stake）。"""
    return (stake / r["fill"] - stake) if r["settle_won"] else -stake


def wilson(k, n):
    """Wilson 95% 区间（小 n 不越界）。"""
    if not n:
        return 0.0, 0.0
    z, p = 1.959964, k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return max(0.0, c - h), min(1.0, c + h)


def boot_ci(vals_by_day, rng, reps=REPS):
    """日级 bootstrap 区间（按 UTC 日整块重采样, 与 23/14 同法）。"""
    ds = sorted(vals_by_day)
    if not ds:
        return 0.0, 0.0
    outs = []
    for _ in range(reps):
        s = 0.0
        for _ in ds:
            s += sum(vals_by_day[rng.choice(ds)])
        outs.append(s)
    outs.sort()
    return outs[int(0.025 * len(outs))], outs[int(0.975 * len(outs))]


# ── 币安 1m K 线（外部行情特征；首次运行自动拉取并缓存到 /tmp）────────────
KL_CACHE = Path("/tmp/kline_btc_1m_flip.jsonl")


def fetch_klines(t0_ms, t1_ms):
    import urllib.request
    import os
    if KL_CACHE.exists():
        rows = [json.loads(l) for l in open(KL_CACHE)]
        if rows and rows[0][0] <= t0_ms and rows[-1][0] >= t1_ms - 60_000:
            return rows
    rows, cur = [], t0_ms
    while cur < t1_ms:
        url = ("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m"
               f"&startTime={cur}&limit=1000")
        proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
        op = urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": proxy}) if proxy
            else urllib.request.ProxyHandler({}))
        with op.open(url, timeout=30) as fh:
            chunk = json.loads(fh.read().decode())
        if not chunk:
            break
        rows += chunk
        cur = chunk[-1][0] + 60_000
    KL_CACHE.write_text("\n".join(json.dumps(r) for r in rows))
    print(f"  （已拉取币安 1m K 线 {len(rows)} 根 → {KL_CACHE}）")
    return rows


def kline_features(rows, start_s, anchor, sd):
    """窗**开始前**的 1m K 线特征（严格不看未来）。返回 dict（缺数据给 None）。"""
    import bisect
    ot = [r[0] for r in rows]
    i = bisect.bisect_left(ot, int(start_s) * 1000)     # 第一根 >= 窗开始的 K 线
    if i < 61:
        return {}
    c = [float(r[4]) for r in rows]
    px = c[i - 1]
    rets = [np.log(c[j] / c[j - 1]) for j in range(max(1, i - 61), i)]
    r15 = np.array(rets[-15:]) if len(rets) >= 15 else np.array(rets)
    r60 = np.array(rets)
    hi = max(float(r[2]) for r in rows[i - 15:i])
    lo = min(float(r[3]) for r in rows[i - 15:i])
    # 当日（UTC 00:00 起）振幅
    d0 = int(start_s) // 86400 * 86400
    j = bisect.bisect_left(ot, d0 * 1000)
    dhi = max(float(r[2]) for r in rows[j:i]) if i > j else px
    dlo = min(float(r[3]) for r in rows[j:i]) if i > j else px
    vol15 = float(r15.std(ddof=1)) * np.sqrt(5) * px if len(r15) > 2 else 0.0
    vol60 = float(r60.std(ddof=1)) * np.sqrt(5) * px if len(r60) > 2 else 0.0
    return dict(mom15=c[i - 1] - c[i - 16], mom60=c[i - 1] - c[i - 61],
                mom15_sig=(c[i - 1] - c[i - 16]) / sd if sd else 0,
                vol15=vol15, vol15_ratio=(vol15 / sd) if sd else 0,
                accel=(vol15 / vol60) if vol60 else 0,
                amp15=(hi - lo), day_amp=(dhi - dlo))


# ── 主流程 ───────────────────────────────────────────────────────────────
def main():
    ev = load_events("data/btc")
    hr = s13.hist_ranges(ev)

    # 第一遍：窗级信息（当日 σ 中位、当日首末开盘价、窗内序号）
    wins = []
    skipped = 0
    for e in ev:
        outcome, anchor = e.get("outcome"), e.get("twap_open_price")
        if outcome is None or not anchor:
            continue
        h = hr.get(e["start_time"])
        if h is None:
            skipped += 1
            continue
        sd = h / anchor * 1e4 * anchor / 1e4
        date = datetime.datetime.fromtimestamp(
            e["start_time"], datetime.timezone.utc).strftime("%Y-%m-%d")
        tx = ticks_ex(e)
        if not tx:
            continue
        s23.T60 = 60
        ref = s23.chain(s23.win_ticks(e), anchor, sd, date, outcome)[0]
        mine = s23.chain(tx, anchor, sd, date, outcome)[0]
        a = [(r["stage"], r["rem"], r["side"], round(r["fill"], 6)) for r in ref if r.get("ok")]
        b = [(r["stage"], r["rem"], r["side"], round(r["fill"], 6)) for r in mine if r.get("ok")]
        if a != b:                                   # 宇宙必须与 oracle 逐位一致
            raise SystemExit(f"⚠️ tick 列与 oracle 不一致: {e['slug']} {a} != {b}")
        ok = [r for r in mine if r.get("ok")][0] if any(r.get("ok") for r in mine) else None
        wins.append(dict(start=e["start_time"], date=date, anchor=anchor, sd=sd,
                         outcome=outcome, ticks=tx, ok=ok,
                         bin_open=e.get("binance_open") or 0,
                         first_half=[(x.get("bin") or {}).get("price") for x in (e["ticks"] or [])
                                     if 150 < (x.get("rem") or 0) <= 300
                                     and (x.get("bin") or {}).get("price")]))

    wins.sort(key=lambda w: w["start"])
    day_sd = collections.defaultdict(list)
    day_open = {}
    idx_in_day = collections.Counter()
    for w in wins:
        day_sd[w["date"]].append(w["sd"])
        day_open.setdefault(w["date"], w["bin_open"])
        w["idx_day"] = idx_in_day[w["date"]]
        idx_in_day[w["date"]] += 1
    day_med = {d: sorted(v)[len(v) // 2] for d, v in day_sd.items()}

    # 第二遍：抽特征
    kl = fetch_klines(int(wins[0]["start"] - 3600 * 24) * 1000,
                      int(wins[-1]["start"] + 600) * 1000)
    sig = []
    for w in wins:
        if not w["ok"]:
            continue
        r = w["ok"]
        # 定位到出信号的那个 tick（stage/rem/side/fill 唯一确定）
        t = next(t for t in w["ticks"]
                 if t["rem"] == r["rem"] and t["side"] == r["side"]
                 and abs(t["fill"] - r["fill"]) < 1e-9)
        p, bn = t["pm"], t["bin"]
        hot5 = (p.get("yes_ask_top5") if t["side"] == "yes" else p.get("no_ask_top5")) or 0
        other = "no" if t["side"] == "yes" else "yes"
        oth5 = (p.get("yes_ask_top5") if other == "yes" else p.get("no_ask_top5")) or 0
        tw = t["twap"] or 0
        sgn = 1.0 if t["side"] == "yes" else -1.0
        kf = kline_features(kl, w["start"], w["anchor"], w["sd"])
        fh = w["first_half"]
        wvol = 0.0
        if len(fh) > 30:                      # 窗内前 150s 的实现波动 → 折算成 |5min 波动| 口径
            rr = np.diff(np.log(np.array(fh, dtype=float)))
            wvol = float(rr.std(ddof=1)) * np.sqrt(150) * 0.7979 * fh[-1]
        # σ 腿放行 = (sd ≥ 40 且 dev ≥ sd)；否则必是 dev 腿单独放行
        sig.append(dict(
            date=w["date"], start=w["start"], stage=r["stage"], rem=r["rem"], side=r["side"],
            fill=r["fill"], dev=r["dev"], sd=r["sd"],
            sig=(r["dev"] / r["sd"]) if r["sd"] else 0,
            won=r["settle_won"], pnl=pl(r), pl1=pl(r, 1.0),
            leg=("σ腿" if (r["sd"] >= s23.SD_MIN_USD and r["dev"] >= r["sd"]) else "dev腿"),
            hour=datetime.datetime.fromtimestamp(
                w["start"], datetime.timezone.utc).hour,
            idx_day=w["idx_day"],
            sd_rel=(w["sd"] / day_med[w["date"]]) if day_med[w["date"]] else 0,
            day_med_sd=day_med[w["date"]],
            day_open=w["bin_open"], anchor=w["anchor"],
            basis=sgn * (t["spot"] - tw) if tw else 0,
            lat=(p.get("book_latency_ms") or 0),
            hot5=hot5, oth5=oth5, bin_ticks=(bn.get("ticks") or 0),
            bin_vol=(bn.get("buy_vol") or 0) - (bn.get("sell_vol") or 0),
            # ── 外部行情（币安 1m K 线, 只看窗开始之前）+ 窗内前 150s ──
            mom15_sig=sgn * kf.get("mom15_sig", 0.0),
            vol15_ratio=kf.get("vol15_ratio", 0.0),
            accel=kf.get("accel", 0.0),
            amp15=kf.get("amp15", 0.0),
            day_amp=kf.get("day_amp", 0.0),
            wvol_ratio=(wvol / r["sd"]) if r["sd"] else 0.0,
        ))
    n = len(sig)
    nl = sum(1 for r in sig if not r["won"])
    print("=" * 110)
    print(f"一、样本：14 天 {n} 笔（输 {nl} = {nl/n*100:.2f}%）；σ 未就绪跳过 {skipped} 窗")
    avg_fill = sum(r["fill"] for r in sig) / n
    print(f"    平均成交价 {avg_fill:.4f} ⇒ 整体盈亏平衡输率 λ* = 1 − 均价 = {1-avg_fill:.4f}"
          f"（实测 {nl/n:.4f}）")
    print(f"    ⚠️ 剔除任一子集的划算判据：该子集**自身**输率 > 1 − 该子集平均成交价")

    # ── 特征定义（桶 = 标签函数）────────────────────────────────────────
    F = collections.OrderedDict()
    F["段 stage"] = ("stage", None)
    F["成交价"] = ("fill", [(0.80, 0.85), (0.85, 0.90), (0.90, 0.95),
                          (0.95, 0.99), (0.99, 1.01)])
    F["入场 rem"] = ("rem", [(0, 30), (30, 60), (60, 90), (90, 120),
                           (120, 150.5)])
    F["侧别"] = ("side", None)
    F["dev(美元)"] = ("dev", [(63, 70), (70, 80), (80, 100), (100, 150), (150, 1e9)])
    F["σ=sd(美元)"] = ("sd", [(0, 50), (50, 65), (65, 90), (90, 130), (130, 1e9)])
    F["dev/σ 倍数"] = ("sig", [(1, 1.5), (1.5, 2), (2, 3), (3, 1e9)])
    F["放行腿"] = ("leg", None)
    F["成交价−0.80 余量"] = ("marg", [(0, 0.02), (0.02, 0.05), (0.05, 0.10),
                                 (0.10, 0.20), (0.20, 1.01)])
    F["UTC 小时"] = ("hour", [(0, 6), (6, 12), (12, 18), (18, 24)])
    F["当天第几窗"] = ("idx_day", [(0, 60), (60, 120), (120, 180), (180, 288)])
    F["σ / 当日σ中位"] = ("sd_rel", [(0, 0.7), (0.7, 1.0), (1.0, 1.5), (1.5, 1e9)])
    F["基差 basis(美元)"] = ("basis", [(-1e9, -30), (-30, 0), (0, 30), (30, 1e9)])
    F["盘口延迟(ms)"] = ("lat", [(0, 50), (50, 150), (150, 301)])
    F["热门侧5档深度"] = ("hot5", [(0, 200), (200, 800), (800, 3000), (3000, 1e9)])
    F["对手侧5档深度"] = ("oth5", [(0, 200), (200, 800), (800, 3000), (3000, 1e9)])
    F["Binance 成交笔数/s"] = ("bin_ticks", [(0, 1), (1, 3), (3, 8), (8, 1e9)])
    F["Binance 净主买量"] = ("bin_vol", [(-1e9, -0.5), (-0.5, 0), (0, 0.5), (0.5, 1e9)])
    F["锚位(千美元)"] = ("anchor", [(0, 60), (60, 90), (90, 110), (110, 1e9)])
    # ── 外部行情特征（币安 K 线；除最后一项外全部只用窗开始之前的数据）──
    F["前15m动量÷σ(顺向)"] = ("mom15_sig", [(-1e9, -1), (-1, -0.3), (-0.3, 0.3),
                                       (0.3, 1), (1, 1e9)])
    F["前15m波动÷σ"] = ("vol15_ratio", [(0, 0.8), (0.8, 1.2), (1.2, 2), (2, 1e9)])
    F["波动加速15m/60m"] = ("accel", [(0, 0.8), (0.8, 1.2), (1.2, 2), (2, 1e9)])
    F["窗内前150s波动÷σ"] = ("wvol_ratio", [(0, 0.5), (0.5, 0.8), (0.8, 1.2), (1.2, 1e9)])
    F["前15m振幅(美元)"] = ("amp15", [(0, 100), (100, 250), (250, 500), (500, 1e9)])
    F["当日至今日振幅"] = ("day_amp", [(0, 200), (200, 500), (500, 1000), (1000, 1e9)])

    def bucketize(key, edges):
        """返回 (标签列表, 每笔的桶名)。"""
        if edges is None:
            labs = sorted(set(r[key] for r in sig))
            return labs, [r[key] for r in sig]
        labs = [f"{lo:g}~{hi:g}" for lo, hi in edges]
        out = []
        for r in sig:
            if key == "marg":
                v = r["fill"] - 0.80
            elif key == "anchor":
                v = r["anchor"] / 1000.0
            else:
                v = r[key]
            out.append(next((labs[i] for i, (lo, hi) in enumerate(edges) if lo <= v < hi),
                            labs[-1]))
        return labs, out

    rng = random.Random(SEED)
    print("\n" + "=" * 110)
    print("二、逐特征分桶（🍎 = 该格自身 P&L 为负且日级 bootstrap 区间不含 0 ⇒ 剔除它划算）")
    print("=" * 110)
    tables = {}
    for name, (key, edges) in F.items():
        labs, asg = bucketize(key, edges)
        print(f"\n  ── {name} " + "─" * (60 - len(name)))
        print(f"    {'桶':<14}{'n':>5}{'输':>5}{'输率':>8}{'Wilson 95%':>16}"
              f"{'均价':>8}{'λ*=1−均价':>11}{'该格P&L':>10}{'日级95%区间':>18}")
        rows_by = collections.defaultdict(list)
        for r, b in zip(sig, asg):
            rows_by[b].append(r)
        for b in labs:
            g = rows_by.get(b)
            if not g:
                continue
            k, m = sum(1 for r in g if not r["won"]), len(g)
            lo, hi = wilson(k, m)
            p = sum(r["pnl"] for r in g)
            bd = collections.defaultdict(list)
            for r in g:
                bd[r["date"]].append(r["pnl"])
            ci = boot_ci(bd, rng, reps=1000)
            af = sum(r["fill"] for r in g) / m
            star = " 🍎" if (p < 0 and ci[1] < 0) else ""
            print(f"    {b:<14}{m:>5}{k:>5}{k/m*100:>7.2f}%"
                  f"{f'[{lo*100:.1f}, {hi*100:.1f}]':>16}{af:>8.4f}{1-af:>11.4f}"
                  f"{p:>+10.2f}{f'[{ci[0]:+.2f}, {ci[1]:+.2f}]':>18}{star}")
        tables[name] = (labs, asg)

    # ── 三、全局置换检验 ────────────────────────────────────────────────
    print("\n" + "=" * 110)
    print("三、全局置换检验：把输赢标签打乱 %d 次，看「全部特征里最好的那一格」能好看到什么程度" % REPS)
    print("=" * 110)
    won = np.array([1 if r["won"] else 0 for r in sig])
    feats = []
    for name, (labs, asg) in tables.items():
        idx = np.array([labs.index(b) for b in asg])
        feats.append((name, idx, len(labs)))
    obs = []
    for name, idx, nb in feats:
        o = np.bincount(idx[won == 0], minlength=nb).astype(float)
        e = np.bincount(idx, minlength=nb).astype(float) * (nl / n)
        ok = e > 0
        obs.append((float(((o[ok] - e[ok]) ** 2 / e[ok]).sum()), name))
    obs.sort(reverse=True)
    rng2 = np.random.default_rng(SEED)
    null = np.empty(REPS)
    for i in range(REPS):
        perm = rng2.permutation(won)
        m = 0.0
        for name, idx, nb in feats:
            o = np.bincount(idx[perm == 0], minlength=nb).astype(float)
            e = np.bincount(idx, minlength=nb).astype(float) * (nl / n)
            ok = e > 0
            m = max(m, float(((o[ok] - e[ok]) ** 2 / e[ok]).sum()))
        null[i] = m
    print(f"  观察值（最强的特征）: {obs[0][1]} χ² = {obs[0][0]:.2f}")
    print(f"  次强: {obs[1][1]} χ² = {obs[1][0]:.2f}   第三: {obs[2][1]} χ² = {obs[2][0]:.2f}")
    print(f"  零分布（打乱后「最大 χ²」的分位）: 50% {np.percentile(null,50):.2f} / "
          f"90% {np.percentile(null,90):.2f} / 95% {np.percentile(null,95):.2f} / "
          f"99% {np.percentile(null,99):.2f}   最大 {null.max():.2f}")
    pval = (null >= obs[0][0]).mean()
    print(f"  ⇒ 全局 p = {pval:.4f}"
          f"{'（不显著：最好的那格落在打乱后的正常范围内）' if pval > 0.05 else '（显著）'}")

    # ── 四、头号候选的分半稳定性 ────────────────────────────────────────
    top = obs[0][1]
    labs, asg = tables[top]
    dates = sorted(set(r["date"] for r in sig))
    h1 = set(dates[:len(dates) // 2])
    print("\n" + "=" * 110)
    print(f"四、头号候选「{top}」的分半稳定性（前半 {min(h1)}~{max(h1)}）")
    print("=" * 110)
    print(f"  {'桶':<14}{'前半 n/输/输率':>22}{'后半 n/输/输率':>22}{'前半P&L':>11}{'后半P&L':>11}")
    for b in labs:
        g = [r for r, x in zip(sig, asg) if x == b]
        if not g:
            continue
        a = [r for r in g if r["date"] in h1]
        c = [r for r in g if r["date"] not in h1]
        f = lambda g2: (f"{len(g2)}/{sum(1 for r in g2 if not r['won'])}/"
                        f"{(sum(1 for r in g2 if not r['won'])/len(g2)*100 if g2 else 0):.1f}%")
        print(f"  {b:<14}{f(a):>22}{f(c):>22}"
              f"{sum(r['pnl'] for r in a):>+11.2f}{sum(r['pnl'] for r in c):>+11.2f}")

    # ── 五、输的聚集性 ─────────────────────────────────────────────────
    print("\n" + "=" * 110)
    print("五、输的聚集性：输是散落的还是成簇的？")
    print("=" * 110)
    seq = sorted(sig, key=lambda r: (r["date"], r["idx_day"]))
    byday = collections.defaultdict(list)
    for r in seq:
        byday[r["date"]].append(r)
    # 相邻两笔（同日）的输赢组合: key = (前一笔是否输, 后一笔是否输)
    adj = collections.Counter()
    for d, g in byday.items():
        for a, b in zip(g, g[1:]):
            adj[(not a["won"], not b["won"])] += 1
    bw, bl = adj[(False, False)], adj[(False, True)]     # 前赢 → 后赢/后输
    lw, ll = adj[(True, False)], adj[(True, True)]       # 前输 → 后赢/后输
    print(f"  同日相邻两笔: 双双赢 {bw} / 先赢后输 {bl} / 先输后赢 {lw} / 双双输 {ll}")
    kw = ll / max(1, ll + lw)
    kb = bl / max(1, bl + bw)
    print(f"  「前一笔输」⇒ 下一笔输率 {ll}/{ll+lw} = {kw*100:.2f}%"
          f"    「前一笔赢」⇒ {bl}/{bl+bw} = {kb*100:.2f}%    基线 {nl/n*100:.2f}%")
    # 置换检验: 在每个交易日内打乱顺序（保留当天的输赢构成）, 看 5/84 有多极端
    rng3 = random.Random(SEED)
    null = []
    for _ in range(REPS):
        pn = pl2 = 0
        for d, g in byday.items():
            o = [r["won"] for r in g]
            rng3.shuffle(o)
            for a, b in zip(o, o[1:]):
                if not a:
                    if b:
                        pn += 0
                    else:
                        pn += 1
                if not a:
                    pl2 += 1
        null.append(pn / pl2 if pl2 else 0)
    pv = sum(1 for x in null if x >= kw) / len(null)
    print(f"  置换 p（打乱日内顺序, 「前一笔输」的输率 ≥ 观察值）= {pv:.3f}"
          f" ⇒ {'不显著' if pv > 0.05 else '显著'}")

    # ── 「当天输满 k 笔就停手」这一族规则 ──────────────────────────────
    print("\n  「当天累计输满 k 笔后停手」——直接对应「每天少亏一笔」的想法:")
    print(f"    {'规则':<16}{'跳过笔数':>9}{'跳过的赢/输':>13}{'跳过的P&L':>11}"
          f"{'净效果Δ':>10}{'日级95%区间':>20}")
    for k in range(1, 9):
        skip_n = skip_k = skip_p = 0
        byd = collections.defaultdict(float)
        for d, g in sorted(byday.items()):
            losses = 0
            for r in g:
                if losses >= k:
                    skip_n += 1
                    skip_k += 0 if r["won"] else 1
                    skip_p += r["pnl"]
                    byd[d] += r["pnl"]
                if not r["won"]:
                    losses += 1
        lo, hi = boot_ci({d: [v] for d, v in byd.items()}, rng, reps=1000)
        print(f"    输满 {k} 笔停手{'':<5}{skip_n:>9}{'赢'+str(skip_n-skip_k)+'/输'+str(skip_k):>13}"
              f"{skip_p:>+11.2f}{-skip_p:>+10.2f}"
              f"{f'[{lo:+.2f}, {hi:+.2f}]':>20}")
    print("    （净效果Δ = −跳过的P&L；正 = 规则赚钱。区间含 0 = 与「什么都不做」不可区分）")

    # ── 五之二、日输次数的平衡点（用户 2026-09-29 提出）─────────────────
    # 状态 = 该笔落单时「当天已结算的输笔数」。无前视: 一笔在闭市 +10s 定案（决策 #19），
    # 而当天后续窗口最早的下单点是 rem≤150（= 闭市后 +150s）⇒ 状态量实盘可得。
    print("\n" + "=" * 110)
    print("五之二、日输次数的平衡点：当天已输 k 笔之后，剩下的注还值不值得下？")
    print("=" * 110)
    st = collections.defaultdict(list)
    for d, g in sorted(byday.items()):
        k = 0
        for r in g:
            st[k].append(r)
            if not r["won"]:
                k += 1
    print(f"  {'已输k笔':>7}{'n':>6}{'输':>5}{'输率':>8}{'Wilson 95%':>18}{'均价':>8}"
          f"{'λ*=1−均价':>10}{'P&L':>10}{'EV/注':>8}{'日级95%区间':>20}")
    for k in sorted(st):
        g = st[k]
        if len(g) < 15:
            continue
        kk = sum(1 for r in g if not r["won"])
        af = sum(r["fill"] for r in g) / len(g)
        p = sum(r["pnl"] for r in g)
        byd2 = collections.defaultdict(list)
        for r in g:
            byd2[r["date"]].append(r["pnl"])
        lo, hi = boot_ci(byd2, rng, reps=2000)
        wl, wh = wilson(kk, len(g))
        flag = " 🍎" if (p < 0 and hi < 0) else ""
        print(f"  {k:>7}{len(g):>6}{kk:>5}{kk/len(g)*100:>7.2f}%"
              f"{f'[{wl*100:.1f}, {wh*100:.1f}]':>18}"
              f"{af:>8.4f}{1-af:>10.4f}{p:>+10.2f}{p/len(g):>+8.4f}"
              f"{f'[{lo:+.2f}, {hi:+.2f}]':>20}{flag}")
    print("  读法: 「已输 k 笔」这一格的 EV/注 若转负且区间不含 0, 该 k 就是停手点；")
    print("        若各格都还是正的 ⇒ 当天的坏行情并没有让**后续**注变差，停手只是少赚。")

    # 日级异质性: 各日输率是否同质（同质 = 坏日子不可提前识别, 只是运气）
    obs = [sum(1 for r in byday[d] if not r["won"]) for d in dates]
    exp = [len(byday[d]) * nl / n for d in dates]
    chi2 = sum((o - e) ** 2 / e for o, e in zip(obs, exp) if e > 0)
    dfree = len(dates) - 1
    print(f"\n  日级异质性 χ² = {chi2:.2f} (df={dfree}；同质期望 ≈ {dfree}±{np.sqrt(2*dfree):.1f})")
    print(f"  ⇒ {'各日输率同质（坏日子不可区分）' if chi2 < dfree + 2*np.sqrt(2*dfree) else '各日输率不同质（存在真·坏日子）'}")

    # ── 五之三、连胜 / 当日累计胜率 → 下一笔会不会输（用户 2026-09-29）──
    print("\n" + "=" * 110)
    print("五之三、连胜之后是不是就该输了？（用户观察: 胜率到 98% 以上, 没多久输单就来了）")
    print("=" * 110)

    def streak_buckets(rows):
        """{该笔之前当天连胜笔数: [行]}——**每笔都入桶**（含输的那笔本身）。
        桶 j≥1 里必然只有输了才可能结束长连胜, 故这些桶的输率就是条件输率。"""
        b = collections.defaultdict(list)
        for d, g in sorted(byday.items()):
            s = 0
            for r in g:
                b[min(s, 6)].append(r)
                s = s + 1 if r["won"] else 0
        return b

    sb = streak_buckets(sig)
    print(f"  {'此前连胜':>9}{'n':>6}{'输':>5}{'输率':>8}{'Wilson 95%':>18}{'均价':>8}{'λ*':>8}{'P&L':>10}")
    for k in sorted(sb):
        g = sb[k]
        kk = sum(1 for r in g if not r["won"])
        af = sum(r["fill"] for r in g) / len(g)
        wl, wh = wilson(kk, len(g))
        lab = f"{k}" if k < 6 else "6+"
        print(f"  {lab:>9}{len(g):>6}{kk:>5}{kk/len(g)*100:>7.2f}%"
              f"{f'[{wl*100:.1f}, {wh*100:.1f}]':>18}"
              f"{af:>8.4f}{1-af:>8.4f}{sum(r['pnl'] for r in g):>+10.2f}")
    print("  注: 「输」这一列在各桶里就是「连胜被终结」的次数；桶越大 = 当天已经越热。")
    # 置换检验: 统计量 = 连胜 ≥5 档的输率（日内打乱顺序, 保留当天输赢构成）
    def hi5_rate(seqs):
        nh = nlh = 0
        for seq in seqs:
            s = 0
            for w in seq:
                if s >= 5:
                    nh += 1
                    nlh += 0 if w else 1
                s = s + 1 if w else 0
        return nlh / nh if nh else 0.0

    obs_hi = hi5_rate([[r["won"] for r in byday[d]] for d in dates])
    n_hi5 = sum(1 for k, g in sb.items() if k >= 5 for _ in g)
    rng4 = random.Random(SEED)
    null4 = []
    for _ in range(REPS):
        seqs = []
        for d in dates:
            o = [r["won"] for r in byday[d]]
            rng4.shuffle(o)
            seqs.append(o)
        null4.append(hi5_rate(seqs))
    pv4 = sum(1 for x in null4 if x >= obs_hi) / len(null4)
    print(f"\n  连胜 ≥5 之后下一笔: 输率 {obs_hi*100:.2f}%（n={n_hi5}）  "
          f"置换 p = {pv4:.3f} ⇒ {'不显著' if pv4 > 0.05 else '显著'}")

    # 当日累计胜率（至少已下 20 笔）分桶 → 当前笔输率
    rb = collections.defaultdict(list)
    for d, g in sorted(byday.items()):
        w = 0
        for i, r in enumerate(g):
            if i >= 20:
                wr_now = w / i
                key = ("<97%" if wr_now < 0.97 else "97~98%" if wr_now < 0.98 else
                       "98~99%" if wr_now < 0.99 else "≥99%")
                rb[key].append(r)
            w += r["won"]
    print(f"\n  当日**此前**累计胜率（至少已下 20 笔）→ 当前这笔:")
    print(f"  {'此前胜率':>9}{'n':>6}{'输':>5}{'输率':>8}{'Wilson 95%':>18}{'均价':>8}")
    for k in ("<97%", "97~98%", "98~99%", "≥99%"):
        g = rb.get(k) or []
        if not g:
            continue
        kk = sum(1 for r in g if not r["won"])
        af = sum(r["fill"] for r in g) / len(g)
        wl, wh = wilson(kk, len(g))
        print(f"  {k:>9}{len(g):>6}{kk:>5}{kk/len(g)*100:>7.2f}%"
              f"{f'[{wl*100:.1f}, {wh*100:.1f}]':>18}{af:>8.4f}")

    # 「累计胜率首次达 98% 之后, 还要下多少笔才出现当天第一笔输」
    def gap_after_98(sequences):
        gaps = []
        for seq in sequences:                      # seq = [(won, …)] 按时间
            w = 0
            hit = None
            for i, won in enumerate(seq):
                if i >= 20 and (w / i) >= 0.98:
                    hit = i
                    break
                w += won
            if hit is None:
                continue
            for j in range(hit + 1, len(seq)):
                if not seq[j]:
                    gaps.append(j - hit)
                    break
            else:
                gaps.append(None)                  # 当天没再输（右删失）
        return gaps

    obs_seq = [[r["won"] for r in byday[d]] for d in dates]
    obs_gaps = gap_after_98(obs_seq)
    oc = [g for g in obs_gaps if g is not None]
    print(f"\n  当天累计胜率**首次** ≥98% 的窗口有 {len(obs_gaps)} 天；其中 {len(oc)} 天之后再出现输、"
          f"{len(obs_gaps)-len(oc)} 天到收盘再没输")
    if oc:
        oc_s = sorted(oc)
        print(f"  到下一笔输的间隔笔数: 中位 {oc_s[len(oc_s)//2]}  均值 {sum(oc)/len(oc):.1f}  "
              f"范围 [{oc_s[0]}, {oc_s[-1]}]"
              f"   （零假设下几何分布均值 1/{(nl/n):.4f} ≈ {1/(nl/n):.0f} 笔）")
    rng5 = random.Random(SEED)
    null5 = []
    for _ in range(REPS):
        seqs = []
        for d in dates:
            o = [r["won"] for r in byday[d]]
            rng5.shuffle(o)
            seqs.append(o)
        gg = [g for g in gap_after_98(seqs) if g is not None]
        null5.append(sum(gg) / len(gg) if gg else 0)
    if oc:
        null5.sort()
        pv5 = sum(1 for x in null5 if x <= sum(oc) / len(oc)) / len(null5)
        print(f"  置换 p（间隔 ≤ 观察值）= {pv5:.3f} ⇒ "
              f"{'不显著' if pv5 > 0.05 else '显著（达到 98% 后确实更快出事）'}")

    # 规则族: 连胜 ≥ k 之后停手 / 停 m 笔
    print(f"\n  「当天连胜 ≥ k 之后停手」与「连胜 ≥ k 后跳过 m 笔」:")
    print(f"    {'规则':<18}{'跳过笔数':>9}{'跳过的赢/输':>13}{'ΔP&L':>10}{'日级95%区间':>20}")
    for k in (3, 5, 6, 8):
        for m in (5, 30, 10 ** 9):
            skip_n = skip_k = skip_p = 0
            byd = collections.defaultdict(float)
            for d, g in sorted(byday.items()):
                s, pause = 0, 0
                for r in g:
                    if s >= k and pause < m:
                        pause += 1
                        skip_n += 1
                        skip_k += 0 if r["won"] else 1
                        skip_p += r["pnl"]
                        byd[d] += r["pnl"]
                    if not r["won"]:
                        s = 0
                    else:
                        s += 1
            lo, hi = boot_ci({d: [v] for d, v in byd.items()}, rng, reps=1000)
            lab = f"连胜≥{k} 停手" if m > 10 ** 8 else f"连胜≥{k} 停 {m} 笔"
            print(f"    {lab:<18}{skip_n:>9}{'赢'+str(skip_n-skip_k)+'/输'+str(skip_k):>13}"
                  f"{-skip_p:>+10.2f}{f'[{lo:+.2f}, {hi:+.2f}]':>20}")

    # ── 七、坏日特征：输 ≥6 次的那一天，BTC 数据里有没有可认的记号 ───────
    # （用户 2026-09-29: 「一天输 6 次以上对应的特征，出现几次就当天停手」）
    print("\n" + "=" * 110)
    print("六、坏日（当天输 ≥6 笔）在 BTC K 线上有没有记号")
    print("=" * 110)

    def dayfeat(t0_s, t1_s):
        """[t0, t1) 内**已收完**的 1m K 线特征（美元口径）。只用完整落在区间内的 K 线。"""
        import bisect
        ot = [r[0] for r in kl]
        i0 = bisect.bisect_left(ot, int(t0_s) * 1000)
        i1 = bisect.bisect_right(ot, int(t1_s) * 1000 - 60_000)
        seg = kl[i0:i1]
        if len(seg) < 20:
            return None
        hi = max(float(r[2]) for r in seg)
        lo = min(float(r[3]) for r in seg)
        cl = np.array([float(r[4]) for r in seg])
        rr = np.diff(np.log(cl))
        return dict(rng=hi - lo,
                    eff=abs(cl[-1] - cl[0]) / (hi - lo) if hi > lo else 0.0,
                    flip=float(np.sum(np.diff(np.sign(rr)) != 0)) / max(1, len(rr) - 1),
                    net=float(cl[-1] - cl[0]),
                    vol5=float(rr.std(ddof=1)) * np.sqrt(5) * float(cl[-1]))

    day_last = {d: max(w["start"] for w in wins if w["date"] == d) for d in dates}
    di = []
    for d in dates:
        g = byday[d]
        y, m_, dd = (int(x) for x in d.split("-"))
        d0 = int(datetime.datetime(y, m_, dd, tzinfo=datetime.timezone.utc).timestamp())
        f = dayfeat(d0, day_last[d] + 300)
        kk = sum(1 for r in g if not r["won"])
        di.append(dict(date=d, n=len(g), loss=kk, bad=kk >= 6, f=f))
    nb = sum(1 for x in di if x["bad"])
    print(f"  14 天里输 ≥6 笔的有 {nb} 天（阈值 6 = 实盘 stake 10U 时约 −60U 的那一档）")
    print(f"\n  {'日期':<12}{'注数':>6}{'输':>5}{'坏日':>6}{'全天振幅$':>11}{'趋势效率':>9}"
          f"{'反转率':>8}{'净涨跌$':>10}{'实现波动$':>11}{'当日σ中位$':>11}")
    for x in di:
        f = x["f"] or {}
        print(f"  {x['date']:<12}{x['n']:>6}{x['loss']:>5}{'✔' if x['bad'] else '':>6}"
              f"{f.get('rng', 0):>11.0f}{f.get('eff', 0):>9.2f}{f.get('flip', 0):>8.2f}"
              f"{f.get('net', 0):>+10.0f}{f.get('vol5', 0):>11.1f}{day_med[x['date']]:>11.1f}")

    # 精确置换（C(14,7)=3432 种分组全枚举）: 坏日 vs 好日 的秩和
    import itertools
    keys = ("rng", "eff", "flip", "vol5", "net")
    names = dict(rng="全天振幅", eff="趋势效率", flip="反转率", vol5="实现波动", net="净涨跌")
    print(f"\n  坏日 vs 好日的秩和检验（14 天, C(14,7)=3432 种分组**全枚举**, 双侧）:")
    for key in keys:
        vals = [abs((x["f"] or {}).get(key, 0.0)) if key == "net" else (x["f"] or {}).get(key, 0.0)
                for x in di]
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        rank = [0.0] * len(vals)
        for pos, i in enumerate(order):
            rank[i] = pos + 1
        obs = sum(rank[i] for i, x in enumerate(di) if x["bad"])
        null = []
        for comb in itertools.combinations(range(len(vals)), nb):
            null.append(sum(rank[i] for i in comb))
        mu = sum(null) / len(null)
        sd = (sum((u - mu) ** 2 for u in null) / len(null)) ** 0.5
        pv = sum(1 for u in null if abs(u - mu) >= abs(obs - mu)) / len(null)
        mb = np.median([vals[i] for i, x in enumerate(di) if x["bad"]])
        mg = np.median([vals[i] for i, x in enumerate(di) if not x["bad"]])
        print(f"    {names[key]:<10} 坏日中位 {mb:>10.2f}   好日中位 {mg:>10.2f}"
              f"   秩和 {obs:.0f} (期望 {mu:.0f}±{sd:.0f})   p = {pv:.3f}"
              f"{'  ⚠️' if pv < 0.05 else ''}")

    # 当天至今（可执行版）: 每笔只看到「本窗前」的 K 线
    for r in sig:
        y, m_, dd = (int(x) for x in r["date"].split("-"))
        d0 = int(datetime.datetime(y, m_, dd, tzinfo=datetime.timezone.utc).timestamp())
        f = dayfeat(d0, r["start"]) or {}
        r["d_rng"] = f.get("rng", 0.0)
        r["d_flip"] = f.get("flip", 0.0)
        r["d_eff"] = f.get("eff", 0.0)
    print("\n  当天**至今**（只用本窗之前的 K 线, 实盘可得）→ 当前这笔的输率:")
    for key, lab, edges in (("d_flip", "当天至今 1m 反转率", (0.0, 0.42, 0.52, 9.9)),
                            ("d_rng", "当天至今振幅（美元）", (0.0, 600, 1500, 9e9))):
        print(f"    ── {lab}")
        for a, b in zip(edges, edges[1:]):
            g = [r for r in sig if a <= r[key] < b]
            if not g:
                continue
            kk = sum(1 for r in g if not r["won"])
            wl, wh = wilson(kk, len(g))
            af = sum(r["fill"] for r in g) / len(g)
            byd3 = collections.defaultdict(list)
            for r in g:
                byd3[r["date"]].append(r["pnl"])
            lo, hi = boot_ci(byd3, rng, reps=1000)
            print(f"       {a:>7.2f}~{b if b < 9e8 else 9e8:>7.2f}  n={len(g):>4} 输 {kk:>2} "
                  f"({kk/len(g)*100:>5.2f}%) [{wl*100:.1f}, {wh*100:.1f}]  均价 {af:.4f} "
                  f"EV/注 {sum(r['pnl'] for r in g)/len(g):>+7.4f}U  [{lo:+.1f}, {hi:+.1f}]")
    print("\n  规则族（当天至今特征越界 ⇒ 当天余下全部停手; 状态只用本窗前的 K 线）:")
    print(f"    {'规则':<26}{'停手笔数':>9}{'跳过的赢/输':>13}{'ΔP&L':>10}{'日级95%区间':>20}")
    for key, thr, lab in (("d_flip", 0.45, "当天至今反转率 > 0.45"),
                          ("d_flip", 0.55, "当天至今反转率 > 0.55"),
                          ("d_rng", 1500, "当天至今振幅 > 1500$"),
                          ("d_rng", 2500, "当天至今振幅 > 2500$"),
                          ("d_eff", 0.35, "当天趋势效率 < 0.35"),
                          ("d_eff", 0.15, "当天趋势效率 < 0.15")):
        op = (lambda v, t=thr: v > t) if key != "d_eff" else (lambda v, t=thr: v < t)
        skip_n = skip_k = skip_p = 0
        byd4 = collections.defaultdict(float)
        for d in dates:
            off = False
            for r in byday[d]:
                if not off and r[key] and op(r[key]):
                    off = True                       # 越界后当天不再恢复（当日熔断语义）
                if off:
                    skip_n += 1
                    skip_k += 0 if r["won"] else 1
                    skip_p += r["pnl"]
                    byd4[d] += r["pnl"]
        lo, hi = boot_ci({d: [v] for d, v in byd4.items()}, rng, reps=1000)
        print(f"    {lab:<26}{skip_n:>9}{'赢'+str(skip_n-skip_k)+'/输'+str(skip_k):>13}"
              f"{-skip_p:>+10.2f}{f'[{lo:+.2f}, {hi:+.2f}]':>20}")
    print("\n  逐日输率（同日注数与输率一起看：输率高的日子是真高还是小样本）:")
    print(f"    {'日期':<12}{'注数':>6}{'输':>5}{'输率':>8}{'当日σ中位':>11}{'当日P&L':>10}")
    for d in dates:
        g = byday[d]
        k = sum(1 for r in g if not r["won"])
        print(f"    {d:<12}{len(g):>6}{k:>5}{k/len(g)*100:>7.2f}%"
              f"{day_med[d]:>11.2f}{sum(r['pnl'] for r in g):>+10.2f}")

    # ── 六、实盘 5 天交叉验证 ───────────────────────────────────────────
    print("\n" + "=" * 110)
    print("七、实盘 5 天（data/tail-live, 10U/注）—— 同一批特征的输率")
    print("=" * 110)
    live = []
    for f in sorted(glob.glob("data/tail-live/tail_2026-09-2*.jsonl")):
        for line in open(f):
            r = json.loads(line)
            if r.get("kind") != "snap" or not r.get("ok"):
                continue
            if r.get("won") is None or r.get("exec_status") not in ("filled", "partial"):
                continue                    # 只看真有仓位的（未成交/被闸不进胜率）
            live.append(r)
    ln = len(live)
    lk = sum(1 for r in live if not r["won"])
    lf = sum((r.get("fill_price") or r.get("hot_ask") or 0) for r in live) / ln
    print(f"  样本 {ln} 笔（输 {lk} = {lk/ln*100:.2f}%）；均价 {lf:.4f} ⇒ λ* = {1-lf:.4f}")
    for lab, fn in (("段 stage", lambda r: r.get("stage")),
                    ("侧别", lambda r: r.get("side")),
                    ("成交价", lambda r: next((b for lo, hi, b in
                                             ((0.80, 0.85, "0.80~0.85"), (0.85, 0.90, "0.85~0.90"),
                                              (0.90, 0.95, "0.90~0.95"), (0.95, 0.99, "0.95~0.99"),
                                              (0.99, 1.01, "0.99+"))
                                             if lo <= (r.get("fill_price") or r.get("hot_ask") or 0) < hi),
                                             "0.99+")),
                    ("入场 rem", lambda r: next((b for lo, hi, b in
                                               ((0, 30, "0~30"), (30, 60, "30~60"), (60, 90, "60~90"),
                                                (90, 120, "90~120"), (120, 151, "120~150"))
                                               if lo <= (r.get("rem") or 0) < hi), "120~150")),
                    ("dev(美元)", lambda r: next((b for lo, hi, b in
                                                ((63, 70, "63~70"), (70, 80, "70~80"),
                                                 (80, 100, "80~100"), (100, 150, "100~150"),
                                                 (150, 1e9, "150+"))
                                                if lo <= (r.get("dev") or 0) < hi), "150+")),
                    ("σ=hist_bps", lambda r: next((b for lo, hi, b in
                                                 ((0, 5, "<5"), (5, 8, "5~8"), (8, 12, "8~12"),
                                                  (12, 1e9, "12+"))
                                                 if lo <= (r.get("hist_bps") or 0) < hi), "12+"))):
        g = collections.defaultdict(list)
        for r in live:
            g[fn(r)].append(r)
        print(f"\n    ── {lab}")
        for b in sorted(g, key=lambda x: str(x)):
            s = g[b]
            k = sum(1 for r in s if not r["won"])
            lo, hi = wilson(k, len(s))
            print(f"      {str(b):<12}{len(s):>5}{k:>5}{k/len(s)*100:>7.2f}%"
                  f"{f'[{lo*100:.1f}, {hi*100:.1f}]':>16}")


if __name__ == "__main__":
    main()
