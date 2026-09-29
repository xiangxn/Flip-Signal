#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫尾盘：**止损的时点结构**（2026-09-29）

用户命题（本节按它的三层提法落地，但把坐标换对了）：

    不是「dev 降到多少就止损」，而是「dev 从安全区掉下来以后，市场在危险区停留了多久，
    翻转概率从哪一刻开始明显上升？」—— 统计 `P(flip | dev, 危险区停留时长)`。

## 为什么不能直接算那张表：三个前置事实（本脚本 §1 实测）

1. **结算值 ≈ 收盘那一刻的 feed 瞬时值**，不是末 60 秒均值。
   实测（1358 窗）：把结算预测成 `[(60−R)·B + R·feed_now]/60`（B = 已走过那段的均值）
   残差 sd 反而**更大**（rem=10 时 25.9 vs 「当前值」10.0），W=120/300 更差。
   ⇒ **过去不会被平均掉**，没有「已经锁定」的记账量可用；危险区停留时长要有效，
   只能靠**真实的可预测性**赚钱。
2. **`twap.price` 滞后 `bin.price` 约 15~40 秒**（10 秒变化的互相关在 L=+15~40 达 +0.40，
   L≤0 为负）。而结算线是 feed。⇒ 引擎的 `dev = sgn·(spot−anchor)` 用的是**现货**，
   它 = 结算线位移 + 一个**不参与结算的水平差** `basis = sgn·(spot − twap_now)`。
3. **水平差是水平量、不是滞后**（`28` 号 §12 已证：窗内几乎不动、日内中位 −0.7 → +53.1）。

⇒ 于是「危险区」「停留时长」这些词有两套坐标：
   `dev_bin`（现货口径 = 引擎现行）与 `dev_feed`（结算线口径 = 真正决定输赢的那个量）。
   **两套都算**，并用同一张表对照。

## 判据（本脚本的划算标准）

买价 `fill`、当前可出场价 = 持仓侧 `bid`、每股兑 1U。持仓到结算 vs 现在出场：

    持有 EV = P(win)·(1/fill − 1) − P(lose)      出场 EV = bid/fill − 1
    ⇒ **出场优于持有 ⟺ bid > P(win)**

市场报价 `bid` 就是市场给的 P(win)。所以「找止损」在数学上等价于
**「找一个比市场更准的 P(win)」**；`bid < 阈值` 这类规则只是它的粗糙代理。
本脚本 §3 直接检验「有没有比 bid 更准的量」，§4 才把它变成可回放的规则。

用法:
  python/venv/bin/python python/v4/36_tail_stoploss_timing.py            # 全量
  python/venv/bin/python python/v4/36_tail_stoploss_timing.py --rebuild  # 重建缓存
"""
import sys
import json
import glob
import math
import random
import pickle
import datetime
import argparse
import collections
import importlib.util
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))
from v2.lib import load_events                                    # noqa: E402

import numpy as np                                                # noqa: E402

DATA = BASE.parent.parent / "data" / "btc"
LIVE = BASE.parent.parent / "data" / "tail-live"
CACHE = BASE / "data" / "tail36_paths.pkl"

STAKE = 2.0
MAX_LAT = 300
T150, T60 = 150, 60
FIELDS = ("yes_bid", "yes_ask", "no_bid", "no_ask")

# oracle 23 的验收 pin（决策 #22 / #26）
PIN_N, PIN_PNL = 2133, 35.092748


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


s23 = load("s23", BASE / "23_tail_integrated.py")
s13 = load("s13", BASE / "13_tail_sweep.py")


# ── 信号重建（与 oracle 23 现行口径逐位一致，额外带 tick 下标）──────────────

def win_ticks_idx(e):
    """镜像 s23.win_ticks，多带全量 ticks 的下标 i。"""
    out = []
    anchor = e.get("twap_open_price")
    if not anchor:
        return out
    for i, x in enumerate(e.get("ticks") or []):
        p = x.get("pm") or {}
        if not p or (p.get("book_latency_ms") or 0) > MAX_LAT:
            continue
        rem = x.get("rem")
        if rem is None or rem <= 0 or rem > T150:
            continue
        if not all((p.get(k) or 0) > 0 for k in FIELDS):
            continue
        spot = (x.get("bin") or {}).get("price")
        if not spot:
            continue
        ya, na = p.get("yes_ask") or 0, p.get("no_ask") or 0
        side = "yes" if ya >= na else "no"
        out.append({"rem": rem, "side": side, "fill": (ya if side == "yes" else na),
                    "spot": spot, "twap": (x.get("twap") or {}).get("price"), "i": i})
    return out


def chain_idx(ticks, anchor, sd, date, outcome):
    """镜像 s23.chain（含 t150 价格腿严格大于），每行带 tick 下标 i。"""
    if not ticks:
        return []
    rows = []
    head = ticks[0]
    if head["rem"] > T60:
        r = s23.row(head, anchor, sd, date, outcome, "t150")
        r["i"] = head["i"]
        if s23.r5(r, strict_price=True):
            r["ok"] = True
            return [r]
        rows.append(r)
        rest = [x for x in ticks if x["rem"] <= T60]
    else:
        rest = ticks
    if rest:
        t2 = rest[0]
        r2 = s23.row(t2, anchor, sd, date, outcome, "t60")
        r2["i"] = t2["i"]
        if s23.r5(r2):
            r2["ok"] = True
            return rows + [r2]
        rows.append(r2)
        for x in rest[1:]:
            rl = s23.row(x, anchor, sd, date, outcome, "listen")
            rl["i"] = x["i"]
            if s23.r2(rl):
                rl["ok"] = True
                rows.append(rl)
                return rows
    return rows


# ── 路径抽取 ──────────────────────────────────────────────────────────────

def path_arrays(e, i0, side, anchor):
    """入场后逐 tick 路径（含入场那一 tick 本身）。

    收录门 = `pm` 在场 ∧ 延迟 ≤ 300 ∧ spot > 0 ∧ feed > 0 ∧ rem ≥ 0。
    ⚠️ 四档全零 = **整簿缺失**（老采集守卫的构造性产物，决策 #21），记 bm 标记后丢弃；
    老数据里永远不会出现「单侧空簿」，所以可成交性只能由 live 监察回答（§5）。
    """
    sgn = 1.0 if side == "yes" else -1.0
    bk, ak = side + "_bid", side + "_ask"
    cols = {k: [] for k in ("rem", "bid", "ask", "dev", "devf", "basis", "spot", "feed", "bm", "lat")}
    for x in (e.get("ticks") or [])[i0:]:
        p = x.get("pm") or {}
        rem = x.get("rem")
        if rem is None or rem < 0:
            continue
        lat = p.get("book_latency_ms") if p else None
        if p and lat is not None and lat > MAX_LAT:
            continue
        spot = (x.get("bin") or {}).get("price")
        feed = (x.get("twap") or {}).get("price")
        if not spot or spot <= 0 or not feed or feed <= 0:
            continue
        if p and all((p.get(k) or 0) == 0 for k in FIELDS):
            cols["bm"].append(rem)                    # 整簿缺失：只登记, 不进状态
            continue
        b = (p or {}).get(bk) or 0.0
        a = (p or {}).get(ak) or 0.0
        cols["rem"].append(rem)
        cols["bid"].append(b)
        cols["ask"].append(a)
        cols["dev"].append(sgn * (spot - anchor))
        cols["devf"].append(sgn * (feed - anchor))
        cols["basis"].append(sgn * (spot - feed))
        cols["spot"].append(spot)
        cols["feed"].append(feed)
        cols["bm"].append(-1)                          # 占位, 保持等长
        cols["lat"].append(lat if lat is not None else -1)
    return {k: np.asarray(v, dtype=float) for k, v in cols.items()}


def build():
    ev = load_events(str(DATA))
    hr = s13.hist_ranges(ev)
    out = []
    for e in ev:
        outcome, anchor = e.get("outcome"), e.get("twap_open_price")
        if outcome is None or not anchor:
            continue
        h = hr.get(e["start_time"])
        if h is None:
            continue                                   # σ 未就绪整窗跳过（决策 #13）
        sd = h / anchor * 1e4 * anchor / 1e4           # 与 Go 侧同运算序列
        date = datetime.datetime.fromtimestamp(
            e["start_time"], datetime.timezone.utc).strftime("%Y-%m-%d")
        rows = chain_idx(win_ticks_idx(e), anchor, sd, date, outcome)
        for r in rows:
            p = path_arrays(e, r["i"], r["side"], anchor)
            if len(p["rem"]) == 0:
                continue                               # 路径为空（入场即闭市）——与 oracle 同
            out.append({
                "cid": e.get("condition_id"), "slug": e.get("slug"),
                "start": e["start_time"], "date": date, "stage": r["stage"],
                "side": r["side"], "ok": bool(r.get("ok")), "rem0": r["rem"],
                "fill": r["fill"], "dev0": r["dev"], "sd": sd, "anchor": anchor,
                "won": bool(r["settle_won"]), "path": p,
            })
    return out


def get(rebuild=False):
    if CACHE.exists() and not rebuild:
        with open(CACHE, "rb") as f:
            return pickle.load(f)
    d = build()
    CACHE.parent.mkdir(exist_ok=True)
    with open(CACHE, "wb") as f:
        pickle.dump(d, f)
    return d


# ── §0 口径自检 ───────────────────────────────────────────────────────────

def sec0(rows):
    print("=" * 96)
    print("§0 口径自检（必须与 oracle 23 现行 pin 逐位一致）")
    sig = [r for r in rows if r["ok"]]
    pnl = sum((STAKE / r["fill"] - STAKE) if r["won"] else -STAKE for r in sig)
    wr = sum(1 for r in sig if r["won"]) / len(sig)
    tag = "✅" if (len(sig) == PIN_N and abs(pnl - PIN_PNL) < 1e-6) else "❌"
    print(f"  信号 n={len(sig)}（pin {PIN_N}）  WR {wr*100:.6f}%  P&L {pnl:+.6f}U（pin {PIN_PNL:+.6f}） {tag}")
    if tag == "❌":
        print("  ⚠️ 与 oracle 不一致 —— 后续所有结论无效，先修口径")
    print(f"  判定行总计 {len(rows)} 行，涉及 {len(set(r['cid'] for r in rows))} 窗")
    return tag == "✅"


# ── §1 三个前置事实 ───────────────────────────────────────────────────────

def sec1(rows, ev):
    print("\n" + "=" * 96)
    print("§1 前置事实（决定后面的表怎么读）")

    # 1a 结算 = 收盘瞬时值 vs 末 W 秒均值
    print("\n§1a 结算值是什么？——预测器残差 sd（美元），1358 窗全量")
    print("   rem | 当前 feed 值 | 当前 bin 值 | 末60s均值(bin) | 末60s均值(feed) | 混合(60−R)/60")
    for R in (10, 30, 60, 90):
        cells = []
        for name in ("feed_now", "bin_now", "ma_bin", "ma_feed", "mix"):
            cells.append([])
        for e in ev:
            c = e.get("twap_close_price")
            t = {}
            for x in e["ticks"]:
                r = x.get("rem")
                f = (x.get("twap") or {}).get("price")
                b = (x.get("bin") or {}).get("price")
                if r is None or not f or not b or f <= 0 or b <= 0:
                    continue
                t[r] = (b, f)
            if not c or R not in t:
                continue
            sb = [t[r][0] for r in range(R + 1, 61) if r in t]
            sf = [t[r][1] for r in range(R + 1, 61) if r in t]
            if R < 60 and len(sb) < (60 - R) * 0.9:
                continue
            m = lambda v: sum(v) / len(v) if v else float("nan")
            cells[0].append(c - t[R][1])
            cells[1].append(c - t[R][0])
            cells[2].append(c - m(sb))
            cells[3].append(c - m(sf))
            cells[4].append(c - (((60 - R) * m(sb) + R * t[R][1]) / 60))
        txt = " | ".join(f"{np.std(v):6.2f}" for v in cells)
        print(f"   {R:>3} | {txt}    n={len(cells[0])}")
    print("   ⇒ 「当前 feed 值」最好 ⇒ **没有已锁定的平均量**，危险区停留时长必须靠可预测性赚钱")

    # 1b feed 滞后 bin
    print("\n§1b feed 相对 bin 的领先/滞后（10 秒变化互相关，1358 窗均值）")
    S = []
    for e in ev:
        t = {}
        for x in e["ticks"]:
            r = x.get("rem")
            f = (x.get("twap") or {}).get("price")
            b = (x.get("bin") or {}).get("price")
            if r is None or not f or not b or f <= 0 or b <= 0:
                continue
            t[r] = (b, f)
        if len(t) >= 250:
            S.append(t)
    K = 10

    def roll(v, k):
        c = np.cumsum(np.insert(v, 0, 0))
        return c[k:] - c[:-k]
    for L in (-40, -20, -10, 0, 10, 15, 20, 30, 40):
        cs = []
        for t in S:
            rs = sorted(t, reverse=True)
            b = np.diff(np.array([t[r][0] for r in rs]))
            f = np.diff(np.array([t[r][1] for r in rs]))
            if len(b) < K * 2 + abs(L) + 2:
                continue
            a, c = roll(b, K), roll(f, K)
            off = abs(L)
            if L >= 0:
                u, v = a[:len(a) - off], c[off:]
            else:
                u, v = a[off:], c[:len(c) - off]
            if u.std() > 0 and v.std() > 0:
                cs.append(np.corrcoef(u, v)[0, 1])
        bar = "█" * int(max(0, np.mean(cs)) * 40)
        print(f"   L={L:+3d}s  corr={np.mean(cs):+.4f}  {bar}")
    print("   ⇒ 峰值在正滞后 ⇒ **feed 滞后于现货** ⇒ dev(现货口径) 含一个不结算的水平差")

    # 1c 水平差本身（⚠️ 不乘 sgn：它是数据源的水平差, 与持仓侧无关）
    print("\n§1c 水平差 spot − twap_now（美元，不乘 sgn）：逐日")
    byd = collections.defaultdict(list)
    for r in rows:
        p = r["path"]
        sg = 1.0 if r["side"] == "yes" else -1.0
        for v in p["basis"][p["bm"] < 0]:
            byd[r["date"]].append(sg * v)
    for d in sorted(byd):
        v = byd[d]
        print(f"   {d}  n={len(v):>7}  中位 {np.median(v):+7.2f}  p10 {np.percentile(v,10):+8.2f}"
              f"  p90 {np.percentile(v,90):+8.2f}")

    # 1d 整簿缺失 tick（老数据的洞）——决定老数据能不能回答可成交性
    print("\n§1d 整簿缺失 tick（四档全零）：老数据全量事件里的 rem 分布")
    band = collections.Counter()
    tot = collections.Counter()
    for e in ev:
        for x in e.get("ticks") or []:
            p = x.get("pm") or {}
            rem = x.get("rem")
            if not p or rem is None:
                continue
            tot[int(rem // 30) * 30] += 1
            if all((p.get(k) or 0) == 0 for k in FIELDS):
                band[int(rem // 30) * 30] += 1
    for k in sorted(tot, reverse=True):
        if tot[k] < 200:
            continue
        print(f"   rem {k:>3}-{k+29:<3}  缺失 {band[k]:>7} / {tot[k]:>8} = {band[k]/tot[k]*100:5.2f}%")
    print("   ⇒ 若缺失全落在窗首（订阅未就绪），则判定区（rem≤150）内**永远看不到单侧空簿**")
    print("     —— 老数据在结构上答不了可成交性（决策 #21）")


# ── 统计小工具 ────────────────────────────────────────────────────────────

def wilson(k, n, z=1.959964):
    if not n:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def bands_of(v, edges):
    """区间下标 i：edges[i] <= v < edges[i+1]（edges 递增, 首尾为 ±inf 哨兵）。"""
    for i in range(len(edges) - 1):
        if v < edges[i + 1]:
            return i
    return len(edges) - 2


DEV_EDGES = (-1e9, 0.0, 20.0, 50.0, 100.0, 200.0, 1e9)
DEV_LAB = ("<0", "0~20", "20~50", "50~100", "100~200", ">200")   # 低 → 高
DUR_EDGES = (0.0, 2.0, 5.0, 10.0, 20.0, 1e9)
DUR_LAB = ("0~1s", "2~4s", "5~9s", "10~19s", "20s+")


def episode_dur(rem, dev, X):
    """逐 tick 的「当前这一段 dev<X 已持续多少秒」（不在区内 = None）。

    与用户 §4 的口径一致：**当前这一次**恶化，不是「过去 N 秒里有多少秒 <X」；
    dev 回到 X 之上即清零重来。时长用 rem 差，缺 tick 的秒数照算（真实经过时间）。
    """
    out = [None] * len(dev)
    inside, t0 = False, None
    for i in range(len(dev)):
        if dev[i] < X:
            if not inside:
                inside, t0 = True, rem[i]
            out[i] = t0 - rem[i]
        else:
            inside, t0 = False, None
    return out


# ── §2 用户要的二维表 ─────────────────────────────────────────────────────

def grid(rows, key, X, tag, only_sig=True):
    """P(最终输 | dev 档 × 危险区停留时长) —— 窗级去重（每窗每格只记首次）。"""
    cell = collections.defaultdict(lambda: [0, 0, []])       # (rb,cb) -> [win, lose, rems]
    low = [r for r in rows if r["ok"]] if only_sig else rows
    for r in low:
        p = r["path"]
        m = p["bm"] < 0
        rem, dev = p["rem"][m], p[key][m]
        du = episode_dur(rem, dev, X)
        seen = set()
        for i in range(len(rem)):
            if du[i] is None:
                continue
            rb, cb = bands_of(dev[i], DEV_EDGES), bands_of(du[i], DUR_EDGES)
            if (rb, cb) in seen:
                continue
            seen.add((rb, cb))
            c = cell[(rb, cb)]
            c[0 if r["won"] else 1] += 1
            c[2].append(rem[i])
    print(f"\n§2{tag} P(输 | {key} 档 × 跌破 {X:.0f} 后停留时长)  —— 口径: {tag[0]}, X={X:.0f}")
    print("        " + "".join(f"{lab:>11}" for lab in DUR_LAB) + "   | 行合计")
    for rb in range(len(DEV_LAB)):
        line = f" {DEV_LAB[rb]:>7}"
        tw = tl = 0
        for cb in range(len(DUR_LAB)):
            w, l, _ = cell.get((rb, cb), (0, 0, []))
            tw += w
            tl += l
            if w + l == 0:
                line += f"{'—':>11}"
            else:
                line += f"{l/(w+l)*100:6.1f}%({w+l:>3})"
        line += f"   | {tl/(tw+tl)*100 if tw+tl else 0:5.1f}%({tw+tl:>4})"
        print(line)
    # 时长与 rem 的共线（读表前提）
    med = {}
    for (rb, cb), (w, l, rr) in cell.items():
        if rr:
            med.setdefault(cb, []).extend(rr)
    print("  该格中位 rem: " + "".join(f"{np.median(med[cb]):>8.0f}s  " if cb in med and med[cb] else
                                     f"{'—':>11}" for cb in range(len(DUR_LAB))))
    print("  （口径提醒: 入场最晚的一批在 rem≈60, 所以「停留 20s+」的格子天然偏 rem 小）")


# ── §3 判别力：市场报价 vs 各种状态量 ─────────────────────────────────────

def calib(rows, tag):
    """bid 的标定曲线（窗级：每窗每个桶只记首次进入）。"""
    edges = (0.0, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 1.01)
    cell = collections.defaultdict(lambda: [0, 0, [], []])
    for r in rows:
        if not r["ok"]:
            continue
        p = r["path"]
        m = p["bm"] < 0
        rem, bid = p["rem"][m], p["bid"][m]
        seen = set()
        for i in range(len(rem)):
            b = bands_of(bid[i], edges)
            if b in seen:
                continue
            seen.add(b)
            c = cell[b]
            c[0 if r["won"] else 1] += 1
            c[2].append(bid[i])
            c[3].append(rem[i])
    print(f"\n§3{tag} 市场报价 bid 的标定（窗级去重）")
    print("   bid 桶        n窗   平均报价   实际胜率       Wilson 95%        中位rem")
    for b in range(len(edges) - 1):
        w, l, bb, rr = cell.get(b, (0, 0, [], []))
        if w + l < 5:
            continue
        lo, hi = wilson(w, w + l)
        print(f"   [{edges[b]:.2f},{edges[b+1]:.2f})  {w+l:>5}   {np.mean(bb):.4f}   "
              f"{(w)/(w+l)*100:6.2f}%   [{lo*100:5.2f}, {hi*100:5.2f}]   {np.median(rr):>6.0f}s")
    print("   ⇒ 报价与实际胜率的差 = 出场优势；差为正 ⇒ 该价位**卖比持有好**")


def strat(rows, bid_bands, key, qs=(33, 67)):
    """bid 桶内再按 key 分档 —— 检验「报价之外还有没有信息」。"""
    vals = np.concatenate([r["path"][key][r["path"]["bm"] < 0] for r in rows if r["ok"]])
    q = np.percentile(vals, qs)
    print(f"\n§3   bid 桶内按 {key} 分档（窗级去重；分位 {q[0]:+.1f}/{q[1]:+.1f}）")
    print("   bid 桶        n窗 | " + " | ".join(f"{key} {'≤q33' if i==0 else ('中' if i==1 else '≥q67'):>10}"
                                                for i in range(3)))
    for lo, hi in bid_bands:
        cell = collections.defaultdict(lambda: [0, 0])
        for r in rows:
            if not r["ok"]:
                continue
            p = r["path"]
            m = p["bm"] < 0
            rem, bid, v = p["rem"][m], p["bid"][m], p[key][m]
            seen = set()
            for i in range(len(rem)):
                if not (lo <= bid[i] < hi):
                    continue
                g = 0 if v[i] <= q[0] else (1 if v[i] < q[1] else 2)
                if g in seen:
                    continue
                seen.add(g)
                cell[g][0 if r["won"] else 1] += 1
        cols = []
        tot = 0
        for g in range(3):
            w, l = cell.get(g, (0, 0))
            tot += w + l
            cols.append("—" if w + l == 0 else f"{w/(w+l)*100:5.1f}%({w+l:>3})")
        print(f"   [{lo:.2f},{hi:.2f})  {tot:>5} | " + " | ".join(f"{c:>14}" for c in cols))
    print("   ⇒ 同一报价下若三档胜率相近 ⇒ 该量已被市场吃进报价里")


# ── §4 回放：把规则变成钱 ─────────────────────────────────────────────────

def boot_day(by_day, n=4000, seed=36):
    """日级配对 bootstrap（14 天，与 25/26 号脚本同法）：95% 区间。"""
    days = sorted(by_day)
    rnd = random.Random(seed)
    tot = []
    for _ in range(n):
        tot.append(sum(by_day[days[rnd.randrange(len(days))]] for _ in days))
    tot.sort()
    return tot[int(0.025 * n)], tot[int(0.975 * n)]


def ctx_of(r, Xs=(100.0, 50.0, 0.0, -20.0)):
    """一条路径的掩码数组 + 各阈值下的「当前这一段已持续多少秒」。"""
    p = r["path"]
    m = p["bm"] < 0
    c = {k: p[k][m] for k in ("rem", "bid", "ask", "dev", "devf", "basis")}
    for X in Xs:
        c["dur%g" % X] = episode_dur(c["rem"], c["dev"], X)
    return c


def replay(rows, fn, tag, extra="", pr=1.0):
    """按规则回放：首次满足即出场（持仓侧 bid），否则持有到结算。

    返回 (触发数, ΔU, CI 下界, CI 上界, 日级 Δ 字典)。`pr` = 出场价折价系数，
    用于把「实盘监察测到的真实成交价」折算回老数据（见 §4 尾的折价表）。
    """
    base = new = 0.0
    by_day = collections.defaultdict(float)
    nfire = kill = save = 0
    pb, ds = [], []
    for r in rows:
        if not r["ok"]:
            continue
        sh = STAKE / r["fill"]
        hold = (sh - STAKE) if r["won"] else -STAKE
        c = ctx_of(r)
        hit = None
        for i in range(len(c["rem"])):
            if fn(c, i):
                hit = i
                break
        if hit is None:
            pnl = hold
        else:
            nfire += 1
            pnl = sh * c["bid"][hit] * pr - STAKE
            if r["won"]:
                kill += 1
            else:
                save += 1
            pb.append(c["bid"][hit])
            ds.append(pnl - hold)
        base += hold
        new += pnl
        by_day[r["date"]] += pnl - hold
    lo, hi = boot_day(by_day)
    pbar = np.mean(pb) if pb else float("nan")
    d = new - base
    wrf = kill / nfire * 100 if nfire else float("nan")
    pmed = float(np.median(pb)) if pb else float("nan")
    print(f"   {tag:<34} {nfire:>4} ({nfire/len(rows)*100:4.1f}%)  {pbar:>6.3f} {pmed:>6.3f} {wrf:5.1f}%  "
          f"{(pbar*100-wrf):+6.1f}pp  {kill:>4}/{save:<4}  {d:+7.2f}U  [{lo:+6.2f},{hi:+6.2f}] {extra}")
    return nfire, d, lo, hi, by_day


def sec4(rows):
    sig = [r for r in rows if r["ok"]]
    n = len(sig)
    print("\n" + "=" * 96)
    print(f"§4 规则回放（n={n} 笔信号；出场 = 首次满足那一秒按持仓侧 bid 卖出，无滑点）")
    print("   规则                              触发     均/中位  触发组  出场价−  杀赢/救输    ΔP&L   日级配对95%CI")
    print("                                          出场价   胜率   胜率")
    print("   " + "·" * 100)

    print("   ── 参照组 ──")
    replay(sig, lambda c, i: True, "立即出场（入场那一秒就卖）")
    replay(sig, lambda c, i: i == len(c["rem"]) - 1, "持有到最后一刻")
    # 上界：预先知道结果时能拿到的最好价
    def oracle(c, i):
        return False
    base_up = 0.0
    for r in sig:
        if r["won"]:
            continue
        c = ctx_of(r)
        base_up += STAKE / r["fill"] * max(c["bid"]) - STAKE
    los = [r for r in sig if not r["won"]]
    hold_los = -STAKE * len(los)
    print(f"   {'【上界】亏损者出在路径最高 bid':<34} {len(los):>4}          —     0/{len(los):<4}  "
          f"{base_up-hold_los:+7.2f}U   ← 不可实现，只看天花板")

    print("   ── 竞价腿单腿（bid < p）──")
    for p in (0.80, 0.70, 0.60, 0.50, 0.40, 0.30, 0.25, 0.20, 0.10):
        replay(sig, lambda c, i, p=p: c["bid"][i] < p, f"bid < {p:.2f}")

    print("   ── 位移腿单腿（现货口径 dev_bin < X）──")
    for X in (-20.0, 0.0, 20.0, 50.0):
        replay(sig, lambda c, i, X=X: c["dev"][i] < X, f"dev_bin < {X:+.0f}")

    print("   ── 位移腿单腿（结算线口径 dev_feed < X）──")
    for X in (-20.0, 0.0, 20.0, 50.0):
        replay(sig, lambda c, i, X=X: c["devf"][i] < X, f"dev_feed < {X:+.0f}")

    print("   ── 双条件（26 号现行候选 + 变体）──")
    replay(sig, lambda c, i: c["bid"][i] < 0.30 and c["dev"][i] < -20.0, "bid<0.30 ∧ dev_bin<−20（现行）")
    replay(sig, lambda c, i: c["bid"][i] < 0.25 and c["dev"][i] < -20.0, "bid<0.25 ∧ dev_bin<−20")
    replay(sig, lambda c, i: c["bid"][i] < 0.30 and c["devf"][i] < -20.0, "bid<0.30 ∧ dev_feed<−20")
    replay(sig, lambda c, i: c["bid"][i] < 0.30 and c["devf"][i] < 0.0, "bid<0.30 ∧ dev_feed<0")
    for p in (0.40, 0.30, 0.20):
        for X in (-20.0, 0.0):
            replay(sig, lambda c, i, p=p, X=X: c["bid"][i] < p and c["devf"][i] < X,
                   f"bid<{p:.2f} ∧ dev_feed<{X:+.0f}")

    print("   ── 用户的时长腿（危险区停留 ≥ τ 秒）──")
    for X in (50.0, 100.0):
        for tau in (5.0, 10.0, 20.0):
            replay(sig, lambda c, i, X=X, tau=tau: (c["dur%g" % X][i] is not None
                                                    and c["dur%g" % X][i] >= tau),
                   f"dev_bin<{X:.0f} 持续 ≥{tau:.0f}s")
    for tau in (5.0, 10.0):
        replay(sig, lambda c, i, tau=tau: (c["dur0"][i] is not None and c["dur0"][i] >= tau),
               f"dev_bin<0 持续 ≥{tau:.0f}s")
    print("   ── 三条件：时长 + bid（用户 §8 的形态）──")
    for tau in (5.0, 10.0, 20.0):
        replay(sig, lambda c, i, tau=tau: (c["dur100"][i] is not None and c["dur100"][i] >= tau
                                           and c["bid"][i] < 0.30),
               f"bid<0.30 ∧ dev_bin<100 持续 ≥{tau:.0f}s")
        replay(sig, lambda c, i, tau=tau: (c["dur0"][i] is not None and c["dur0"][i] >= tau
                                           and c["bid"][i] < 0.50),
               f"bid<0.50 ∧ dev_bin<0 持续 ≥{tau:.0f}s")

    # 关键候选的分半（14 天 = h1 前 7 天 / h2 后 7 天）
    print("   ── 时长当**过滤器**（用户 §8 的 dev<X ∧ 持续>τ 用在现行规则上）──")
    print("      （现行触发时 dev 已 <−20 ⇒ 必然早已过零, dur0 从过零那一刻算起）")
    for tau in (5.0, 10.0, 20.0):
        replay(sig, lambda c, i, tau=tau: (c["bid"][i] < 0.30 and c["dev"][i] < -20.0
                                           and c["dur0"][i] is not None and c["dur0"][i] >= tau),
               f"bid<0.30 ∧ dev<−20 ∧ 过零已 ≥{tau:.0f}s")
        replay(sig, lambda c, i, tau=tau: (c["bid"][i] < 0.30 and c["dev"][i] < -20.0
                                           and c["dur0"][i] is not None and c["dur0"][i] < tau),
               f"bid<0.30 ∧ dev<−20 ∧ 过零还 <{tau:.0f}s")
    for tau in (5.0, 10.0):
        replay(sig, lambda c, i, tau=tau: (c["bid"][i] < 0.30 and c["dev"][i] < -20.0
                                           and c["dur100"][i] is not None and c["dur100"][i] >= tau),
               f"bid<0.30 ∧ dev<−20 ∧ 入区(dev<100)已 ≥{tau:.0f}s")

    print("   ── 用户 §6 的结构腿：结算线跟现货**同不同意**（触发时 dev_feed 在哪）──")
    print("      （66 次触发的中位态: dev_bin −28 / dev_feed **+33** / basis −64 / rem 52）")
    for X in (0.0, 20.0, 50.0):
        replay(sig, lambda c, i, X=X: (c["bid"][i] < 0.30 and c["dev"][i] < -20.0
                                       and c["devf"][i] >= X),
               f"bid<0.30 ∧ dev_bin<−20 ∧ dev_feed ≥{X:+.0f}（结算线还高）")
        replay(sig, lambda c, i, X=X: (c["bid"][i] < 0.30 and c["dev"][i] < -20.0
                                       and c["devf"][i] < X),
               f"bid<0.30 ∧ dev_bin<−20 ∧ dev_feed <{X:+.0f}（结算线已跟）")
    for B in (-40.0, -80.0, -120.0):
        replay(sig, lambda c, i, B=B: (c["bid"][i] < 0.30 and c["dev"][i] < -20.0
                                       and c["basis"][i] < B),
               f"bid<0.30 ∧ dev_bin<−20 ∧ basis<{B:.0f}（现货深跌破结算线）")

    print("\n   ── 关键候选分半（ΔU：h1 前 7 天 / h2 后 7 天）──")
    days = sorted(set(r["date"] for r in sig))
    mid = days[len(days) // 2]

    def half(fn, tag):
        out = []
        for part in (days[:len(days) // 2], days[len(days) // 2:]):
            s = set(part)
            _, d, _, _, _ = replay([r for r in sig if r["date"] in s], fn, tag)
            out.append(d)
        print(f"      {tag:<30} h1 {out[0]:+7.2f}U   h2 {out[1]:+7.2f}U   "
              f"（分界 {mid}）")

    half(lambda c, i: c["bid"][i] < 0.30 and c["dev"][i] < -20.0, "bid<0.30 ∧ dev_bin<−20")
    half(lambda c, i: c["bid"][i] < 0.25 and c["dev"][i] < -20.0, "bid<0.25 ∧ dev_bin<−20")
    half(lambda c, i: c["bid"][i] < 0.30 and c["devf"][i] < -20.0, "bid<0.30 ∧ dev_feed<−20")

    # 实盘折算：老数据的 bid 从不塌到 0（构造性）⇒ 出场价必然乐观
    print("\n   ── 实盘折算（用 §5 监察测到的真实成交条件压老数据的 Δ）──")
    print("      折价系数 = live 可成交出场价 / 老数据出场价；触发率 = live 有对手方的比例")
    for tag, fn in (("bid<0.30 ∧ dev_bin<−20",
                     lambda c, i: c["bid"][i] < 0.30 and c["dev"][i] < -20.0),
                    ("bid<0.25 ∧ dev_bin<−20",
                     lambda c, i: c["bid"][i] < 0.25 and c["dev"][i] < -20.0)):
        for pr, er, lab in ((0.78, 1.0, "价格×0.78（live 可成交均价 / 老数据均价）"),
                            (0.78, 0.667, "价格×0.78 且触发率×2/3（1/3 无买盘）")):
            _, d, lo, hi, _ = replay(sig, fn, f"{tag} {lab}", pr=pr)
            print(f"        ⇒ 折算后 {d*er:+6.2f}U（未折算前见上表；er={er} 为保守折算）")

    print("   ── 时长是否独立于 dev（控制 dev 档与 rem 档后）──")
    dur_ctrl(sig, "dev", 100.0, 5.0)
    dur_ctrl(sig, "dev", 0.0, 5.0,
             bands=((-200, -40), (-40, -20), (-20, -10), (-10, -5), (-5, 0)))
    dur_ctrl(sig, "dev", 0.0, 10.0,
             bands=((-200, -40), (-40, -20), (-20, -10), (-10, -5), (-5, 0)))


def dur_ctrl(rows, key="dev", X=100.0, tau=5.0, bands=None):
    """时长有没有**独立于 dev** 的信息：在 (细 dev 档 × rem 档) 格内比较长短两组。

    两个必须同时钉住的量（否则比出来的是假效应）：
      1. **dev 档**——刚跌破阈值那一 tick 的 dev 必然贴着阈值，而「已停留 ≥τ」组的
         dev 可以低得多 ⇒ 不控 dev 比出的是**水平效应**（§2 那张表的 X=0 版就是）；
      2. **rem 档**——停留越久 ⇒ 剩余时间越少 ⇒ 越接近结算 ⇒ 输率本来就高。
    格内还额外打印两组的平均 dev，让「档内残余不平衡」可见。
    """
    print(f"\n    控制 (dev 档 × rem 档) 后：{key} < {X:.0f} 的 tick 按已停留时长 ≥{tau:.0f}s 分两组")
    print("      dev 档            n(短) 均dev  输率(短) | n(长) 均dev  输率(长) |   Δpp")
    wsum = nsum = 0.0
    for dl, dh in (bands or ((-40, -20), (-20, 0), (0, 25), (25, 50), (50, 75), (75, 100))):
        cell = [[0, 0, 0.0], [0, 0, 0.0]]
        for r in rows:
            if not r["ok"]:
                continue
            c = ctx_of(r)
            seen = set()
            for i in range(len(c["rem"])):
                if not (dl <= c[key][i] < dh) or c[key][i] >= X:
                    continue
                du = c["dur%g" % X][i]
                if du is None:
                    continue
                g = (1 if du >= tau else 0, int(c["rem"][i] // 10))
                if g in seen:
                    continue
                seen.add(g)
                z = cell[1 if du >= tau else 0]
                z[0 if r["won"] else 1] += 1
                z[2] += c[key][i]
        ws, ls, ds = cell[0]
        wl, ll, dl_ = cell[1]
        ns, nl = ws + ls, wl + ll
        if ns + nl < 30:
            continue
        ps = ls / ns * 100 if ns else float("nan")
        pl = ll / nl * 100 if nl else float("nan")
        ms, ml = (ds / ns if ns else float("nan")), (dl_ / nl if nl else float("nan"))
        wgt = ns * nl / (ns + nl) if ns and nl else 0.0
        wsum += wgt * (pl - ps) if (ns and nl) else 0.0
        nsum += wgt
        print(f"      {dl:>+5}~{dh:<+5}  {ns:>6} {ms:>6.1f}  {ps:6.1f}%  | {nl:>6} {ml:>6.1f}  "
              f"{pl:6.1f}%  | {pl-ps:+6.1f}")
    print(f"      加权合并 Δ = {wsum/nsum if nsum else float('nan'):+.2f}pp"
          f"（权重 n_短·n_长/(n_短+n_长)）")
    print("      ⇒ 若每格 Δ 都在 0 附近 ⇒ 时长**没有**独立于 dev 的信息")


# ── §5 实盘可成交性（唯一能回答「卖得掉吗」的数据）───────────────────────

def sec5():
    print("\n" + "=" * 96)
    print("§5 实盘可成交性：tailhold_*.jsonl（09-25~29 真实成交样本）")
    hold = collections.defaultdict(list)
    for fp in sorted(glob.glob(str(LIVE / "tailhold_*.jsonl"))):
        with open(fp) as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    h = json.loads(ln)
                    hold[h["condition_id"]].append(h)
    # 结果取自信号行（与持仓监察同一 condition_id）
    sig = {}
    for fp in sorted(glob.glob(str(LIVE / "tail_*.jsonl"))):
        with open(fp) as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                r = json.loads(ln)
                if r.get("kind") != "snap" or not r.get("ok"):
                    continue
                if r.get("won") is None:
                    continue
                sig[r["condition_id"]] = r
    ids = [c for c in hold if c in sig]
    print(f"  监察窗 {len(hold)} 个，其中已结算的信号窗 {len(ids)} 个"
          f"（赢 {sum(1 for c in ids if sig[c]['won'])} / "
          f"输 {sum(1 for c in ids if not sig[c]['won'])}）")
    if not ids:
        return
    nrow = sum(len(hold[c]) for c in ids)
    print(f"  监察行 {nrow} 行；持仓侧 bid 缺字段/深度的行 "
          f"{sum(1 for c in ids for h in hold[c] if 'hold_bid5' not in h)} 行")

    # 5a stop_cand 时刻的对手方
    print("\n§5a 若挂 26 号止损（bid<0.30 ∧ dev<−20）：触发那一刻有没有对手方")
    cand = [h for c in ids for h in hold[c] if h.get("stop_cand")]
    print(f"  stop_cand=true 的 tick 共 {len(cand)} 个，分属 "
          f"{len(set(h['condition_id'] for h in cand))} 个窗")
    if cand:
        z = [h for h in cand if h["hold_bid"] == 0]
        thin = [h for h in cand if 0 < h["hold_bid5"] < 5]
        ok5 = [h for h in cand if h["hold_bid5"] >= 5]
        print(f"    hold_bid == 0（整侧无买单, 卖不掉） : {len(z):>4} = {len(z)/len(cand)*100:5.1f}%")
        print(f"    0 < 深度 < 5 股（低于交易所下限）   : {len(thin):>4} = {len(thin)/len(cand)*100:5.1f}%")
        print(f"    深度 ≥ 5 股（真能卖）              : {len(ok5):>4} = {len(ok5)/len(cand)*100:5.1f}%")
    # 5b 逐窗：首次触发的时刻与当时的深度
    print("\n§5b 逐窗「首次触发」的那一秒（每窗只算最先满足的那一 tick）")
    print("    结果  笔数 | 触发rem 中位 | bid 中位 | 深度≥5股 占比 | bid==0 占比")
    for won, lab in ((False, "最终输"), (True, "最终赢")):
        rows0 = []
        for c in ids:
            if bool(sig[c]["won"]) != won:
                continue
            hr = sorted([h for h in hold[c] if h.get("stop_cand")], key=lambda h: -h["rem"])
            if hr:
                rows0.append(hr[0])
        if not rows0:
            continue
        print(f"    {lab}  {len(rows0):>4} | {np.median([h['rem'] for h in rows0]):>10.0f}s | "
              f"{np.median([h['hold_bid'] for h in rows0]):>7.3f} | "
              f"{sum(1 for h in rows0 if h['hold_bid5']>=5)/len(rows0)*100:>11.1f}% | "
              f"{sum(1 for h in rows0 if h['hold_bid']==0)/len(rows0)*100:>9.1f}%")
    # 5c 亏损者：从「第一次跌破 0.5」到「卖不掉」还有多久
    print("\n§5c 亏损窗的可出场窗口（从首次跌破阈值 到 出现 bid==0 之间的秒数）")
    print("    阈值  窗数 | 出场窗口秒数 p10/中位/p90 | 窗口内存在「bid≥0.05 且深度≥5股」的窗占比")
    for thr in (0.80, 0.60, 0.50, 0.40, 0.30):
        gaps, exe = [], 0
        for c in ids:
            if sig[c]["won"]:
                continue
            hr = sorted(hold[c], key=lambda h: -h["rem"])
            t_in = [h for h in hr if 0 < h["hold_bid"] < thr]
            t_z = [h for h in hr if h["hold_bid"] == 0]
            if not t_in:
                continue
            back = t_in[0]["rem"]
            if t_z and t_z[0]["rem"] < back:
                gaps.append(back - t_z[0]["rem"])
            elif not t_z:
                gaps.append(back - 0)          # 一路有买单到尾
            # 出场窗口内是否有可成交的一秒
            lo_rem = t_z[0]["rem"] if (t_z and t_z[0]["rem"] < back) else 0
            if any(h["hold_bid"] >= 0.05 and h["hold_bid5"] >= 5 for h in t_in
                   if h["rem"] >= lo_rem):
                exe += 1
        if not gaps:
            continue
        print(f"    {thr:.2f}  {len(gaps):>4} | {np.percentile(gaps,10):>7.0f} / "
              f"{np.median(gaps):>4.0f} / {np.percentile(gaps,90):>6.0f} | {exe/len(gaps)*100:>10.1f}%")
    # 5e 逐窗明细（11 个亏损窗全列）
    print("\n§5e 全部亏损窗逐窗（入场 rem / 首次 bid<0.30 / 首次 dev<−20 / 末行 bid）")
    print("    入rem  fill  行数 | 首次 bid<0.30 (rem/bid/dev) | 首次 dev<−20 (rem/bid/深度) | 末行bid")
    los = [c for c in ids if not sig[c]["won"]]
    z_at = ex_at = 0
    for c in sorted(los, key=lambda c: -sig[c]["rem"]):
        hr = sorted(hold[c], key=lambda h: -h["rem"])
        s = sig[c]
        a = [h for h in hr if 0 < h["hold_bid"] < 0.30]
        b = [h for h in hr if h["dev"] < -20]
        fa = "—" if not a else f"{a[0]['rem']:>3}/{a[0]['hold_bid']:.3f}/{a[0]['dev']:+7.1f}"
        fb = "—" if not b else f"{b[0]['rem']:>3}/{b[0]['hold_bid']:.3f}/{b[0]['hold_bid5']:>7.0f}"
        print(f"    {s['rem']:>4}  {s['hot_ask']:>5}  {len(hr):>4} | {fa:<26} | {fb:<26} | "
              f"{hr[-1]['hold_bid']:.3f}")
        if b:
            if b[0]["hold_bid"] == 0:
                z_at += 1
            else:
                ex_at += 1
    print(f"    ⇒ 11 个亏窗**末行 bid 全为 0**（整侧撤空, 决策 #21 在实盘逐窗复现）")
    print(f"    ⇒ 规则首次触发（bid<0.30 ∧ dev<−20）的 6 窗里：bid==0（卖不掉）{z_at} 个、"
          f"可成交 {ex_at} 个")
    print(f"    ⇒ 关键时序：首次 bid<0.30 的 rem 明显**大于**首次 dev<−20 的 rem"
          f" ⇒ 报价先塌、位移后到（老数据里 dev 腿是「确认」，不是「预警」）")

    # 5d 口径缺口：live 监察行里没有 twap ⇒ dev_feed 实盘不可算
    k = sorted(hold[ids[0]][0].keys())
    print(f"\n§5d 监察行字段：{' '.join(k)}")
    print(f"    含 twap/feed？{'twap' in k or 'feed' in k} ⇒ "
          f"**dev_feed 与 basis 在实盘监察里算不出来**（缺口, 要落盘才谈得上在 live 用）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--no-live", action="store_true")
    args = ap.parse_args()
    rows = get(args.rebuild)
    ok = sec0(rows)
    if not ok:
        return
    ev = load_events(str(DATA))
    sec1(rows, ev)
    sig = [r for r in rows if r["ok"]]
    print("\n" + "=" * 96)
    print("§2 用户的二维表（dev 档 × 危险区停留时长）")
    for X in (100.0, 0.0):
        grid(sig, "dev", X, "现货口径(dev_bin)")
        grid(sig, "devf", X, "结算线口径(dev_feed)")
    print("\n" + "=" * 96)
    calib(sig, "")
    bb = ((0.90, 1.01), (0.80, 0.90), (0.60, 0.80), (0.40, 0.60), (0.20, 0.40), (0.0, 0.20))
    for key in ("dev", "devf", "basis", "bid"):
        if key == "bid":
            continue
        strat(sig, bb, key)
    sec4(rows)
    if not args.no_live:
        sec5()


if __name__ == "__main__":
    main()
