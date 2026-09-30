#!/usr/bin/env python3
"""扫尾盘 · 止损第四问: 仓位报价的**下穿 / 上穿**与停留时长（2026-09-30）

用户问题（原文）:「你看一下回测中那些亏掉的, **仓位下穿上/上穿上的情况, 以及持续时间**,
看能不能找到好的止损办法」。

与前三轮的区别（为什么这不是重复）:

| 轮次 | 观测对象 | 形态 |
|---|---|---|
| 25/26/决策 #24 | 瞬时**阈值**（`bid < 0.30 ∧ dev < −20` 那一秒卖） | 无记忆 |
| 36 §2/§4 | **dev 区**的停留时长（位移口径, 不是报价） | 有记忆, 但量的是现货位移 |
| **本脚本** | **持仓侧报价 bid 本身**的下穿 / 上穿 | 有记忆, 量的是报价路径形状 |

用户观察到的现象（「下穿 → 市场改价 → 几秒后又上穿 → 改回来」）本来就是**报价路径**的
性质; 36 号把停留时长做在 `dev` 上（位移坐标）, 本脚本把它做在 **bid** 上（报价坐标）。
两者的判别目标相同: **出场优于持有 ⟺ 出场价 > P(赢)**（36 §3）——市场报价是良标的,
所以任何止损规则都必须靠「在某个特定状态下比市场更准」赚钱, 本脚本就去找这个状态。

三条口径（与 oracle / 引擎对齐）:
1. **宇宙** = `23_tail_integrated.py` 现行口径（含决策 #29 的 T=150 入场闸 `walk ≥ 43`）。
   36 号脚本钉的是闸前宇宙（n=2133）, 本脚本钉闸后（n=2080）——§0 自检逐位比对。
2. **仓位价格 = 持仓侧 bid**（你能卖出去的那个价）。不用 mid / 有效价: 出场语义就是卖出,
   而 §7 的可成交性折价也是在 bid 上标定的。⚠️ 老数据的 bid 在崩盘段**系统性偏高**
   （旧采集把空侧盘口整条丢掉, 决策 #21）——这一点在 §5 折价里量化。
3. **下穿的定义**: `bid < L`（严格小于）, 按真实经过时间算停留（rem 差, 缺 tick 的秒数照算）。

用法:
  python/venv/bin/python python/v4/39_tail_stoploss_crossing.py            # 用缓存
  python/venv/bin/python python/v4/39_tail_stoploss_crossing.py --rebuild  # 重建缓存
"""
import sys
import json
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
CACHE = BASE / "data" / "tail39_paths.pkl"
CACHE36 = BASE / "data" / "tail36_paths.pkl"     # 36 号的缓存 = **闸前**宇宙（决策 #29 之前）

STAKE = 2.0
MAX_LAT = 300
T150, T60 = 150, 60
FIELDS = ("yes_bid", "yes_ask", "no_bid", "no_ask")

# oracle 23 现行 pin（决策 #29 之后）: 信号 2080 / P&L +64.026354U / 判定行 6686
PIN_N, PIN_PNL, PIN_ROWS, PIN_WALK_LOW = 2080, 64.026354, 6686, 250


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


s36 = load("s36", BASE / "36_tail_stoploss_timing.py")
s23, s13 = s36.s23, s36.s13
win_ticks_idx, path_arrays = s36.win_ticks_idx, s36.path_arrays
boot_day, wilson = s36.boot_day, s36.wilson


# ── 信号重建（oracle 23 现行口径 + tick 下标）──────────────────────────────

def chain_idx(ticks, anchor, sd, date, outcome):
    """镜像 s23.chain（含决策 #29 的 T=150 入场闸）, 每行带 tick 下标 i。"""
    if not ticks:
        return []
    rows = []
    head = ticks[0]
    if head["rem"] > T60:
        r = s23.row(head, anchor, sd, date, outcome, "t150")
        r["i"] = head["i"]
        # ⚠️ 与 36 号唯一的差别: 段 1 加 walk 入场闸（决策 #29）
        ok5 = s23.r5(r, strict_price=True)
        r["walk_low"] = bool(ok5 and not s23.walk_ok(r))   # ⑤ 达标但被闸（§0 对账用）
        if ok5 and s23.walk_ok(r):
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
                "walk_low": bool(r.get("walk_low")),
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


# ── 路径原语 ──────────────────────────────────────────────────────────────

def mask_of(r):
    return r["path"]["bm"] < 0


def dwell_of(bid, rem, L):
    """逐 tick「当前这一段 bid < L 已持续多少秒」（不在下方 = −1）。

    ⚠️ 这一段的定义 = **连续**在下方（回到 L 之上即清零重来）, 与 36 号 §2 的
    用户口径一致; 时长用 rem 差算真实经过时间（缺 tick 的秒数照算）。
    """
    out = np.full(len(bid), -1.0)
    t0 = None
    for i in range(len(bid)):
        if bid[i] < L:
            if t0 is None:
                t0 = rem[i]
            out[i] = t0 - rem[i]
        else:
            t0 = None
    return out


def episodes_of(bid, rem, L):
    """所有「在 L 下方」的连续段: [(起始下标, 持续秒数, 最低价)]。"""
    eps, t0, lo = [], None, None
    for i in range(len(bid)):
        if bid[i] < L:
            if t0 is None:
                t0, lo = i, bid[i]
            lo = min(lo, bid[i])
        elif t0 is not None:
            eps.append((t0, rem[t0] - rem[i - 1], lo))
            t0 = None
    if t0 is not None:
        eps.append((t0, rem[t0] - rem[len(bid) - 1], lo))
    return eps


# ── §0 口径自检 ───────────────────────────────────────────────────────────

def sec0(rows):
    print("=" * 100)
    print("§0 口径自检（必须与 oracle 23 现行 pin 逐位一致: 决策 #29 之后的宇宙）")
    sig = [r for r in rows if r["ok"]]
    pnl = sum((STAKE / r["fill"] - STAKE) if r["won"] else -STAKE for r in sig)
    wr = sum(1 for r in sig if r["won"]) / len(sig)
    wl = sum(1 for r in rows if r.get("walk_low"))
    ok1 = len(sig) == PIN_N and abs(pnl - PIN_PNL) < 1e-6
    ok2 = len(rows) == PIN_ROWS and wl == PIN_WALK_LOW
    tag = "✅" if (ok1 and ok2) else "❌"
    print(f"  信号 n={len(sig)}（pin {PIN_N}）  WR {wr*100:.6f}%  P&L {pnl:+.6f}U（pin {PIN_PNL:+.6f}）")
    print(f"  判定行 {len(rows)}（pin {PIN_ROWS}）  walk_low 行 {wl}（pin {PIN_WALK_LOW}）  {tag}")
    if tag == "❌":
        print("  ⚠️ 与 oracle 不一致 —— 后续所有结论无效, 先修口径")
    return tag == "✅"


# ── §1 亏单的路径形状（用户在问的那张表）─────────────────────────────────

LEVELS = (0.95, 0.90, 0.85, 0.80, 0.70, 0.60, 0.50, 0.40, 0.30, 0.20)
TAUS = (0, 1, 2, 3, 5, 8, 10, 15, 20, 30)


def fire_ref(sig):
    """36 号的现行候选（瞬时）: `bid < 0.30 ∧ dev_bin < −20` 首次满足即卖。"""
    out = {}
    for r in sig:
        m = mask_of(r)
        hit = np.flatnonzero((r["path"]["bid"][m] < 0.30) & (r["path"]["dev"][m] < -20.0))
        if len(hit):
            i = int(hit[0])
            out[r["cid"]] = (i, r["path"]["bid"][m][i])
    return out


def sec0b(rows):
    """闸前 / 闸后对照: 决策 #29 的入场闸把这个止损的收益吃掉了多少。

    两个宇宙用的是**同一条规则、同一套出场口径**, 唯一差别是段 1 有没有 walk 闸。
    """
    print("\n" + "=" * 100)
    print("§0b 闸前 / 闸后: 现行止损候选 `bid<.30 ∧ dev<−20` 在两个宇宙里的表现")
    print("    （闸前 = 决策 #29 之前, 直接用 36 号的缓存; 闸后 = 本脚本的宇宙）")
    print("   规则                          触发     出场价  触发组  出场价− 杀赢/救输    ΔP&L    日级配对95%CI")
    print("                                           均值   胜率    胜率")
    print("   " + "·" * 100)
    sig_new = [r for r in rows if r["ok"]]
    keep = None
    if CACHE36.exists():
        with open(CACHE36, "rb") as f:
            old = pickle.load(f)
        sig_old = [r for r in old if r["ok"]]
        replay_fire(sig_old, fire_ref(sig_old), f"闸前（n={len(sig_old)}）")
    else:
        print("   （36 号缓存不在, 跳过闸前对照——先跑一次 36_tail_stoploss_timing.py）")
    keep = replay_fire(sig_new, fire_ref(sig_new), f"闸后（n={len(sig_new)}）")
    print("   ⚠️ 闸把「结算线还没走」的那批入场整个删掉了 —— 而止损赚的恰好是同一批窗，")
    print("      两个机制是**替代品**（都吃「报价先塌、位移后到」这一个结构），不是叠加的。")
    return keep


def sec1(rows):
    sig = [r for r in rows if r["ok"]]
    nw = sum(1 for r in sig if r["won"])
    print("\n" + "=" * 100)
    print(f"§1 持仓侧报价的下穿结构（n={len(sig)} 笔信号: {nw} 赢 / {len(sig)-nw} 输）")
    print("   口径: 下穿 = 入场后持仓侧 bid **首次**严格跌破 L; 停留 = 该段连续在 L 下方的秒数")
    print("   已在下 = 入场那一 tick 的 bid 就已经 < L（没得跨, 本来就是下方）\n")
    print("     L     已在下   下穿n  其中赢/输   首次下穿   停留中位   回来过    P(输|下穿)   P(输|没下穿)")
    print("                                     中位rem     /最长          赢/输")
    print("   " + "·" * 96)
    for L in LEVELS:
        already = crossed = 0
        cw = cl = 0
        rems, durs = [], []
        rec_w = rec_l = 0
        nw_never = nl_never = 0
        for r in sig:
            m = mask_of(r)
            bid, rem = r["path"]["bid"][m], r["path"]["rem"][m]
            if len(bid) == 0:
                continue
            if bid[0] < L:
                already += 1
            eps = episodes_of(bid, rem, L)
            first = next((e for e in eps if e[0] > 0 or bid[0] >= L), None)
            if first is None:
                if r["won"]:
                    nw_never += 1
                else:
                    nl_never += 1
                continue
            crossed += 1
            cw, cl = cw + r["won"], cl + (not r["won"])
            rems.append(rem[first[0]])
            durs.append(max((e[1] for e in eps), default=0.0))
            # 首次下穿之后, 有没有再回到 L 之上过
            back = bool(np.any(bid[first[0]:] >= L))
            if back:
                rec_w, rec_l = rec_w + r["won"], rec_l + (not r["won"])
        pw = cl / (cw + cl) * 100 if cw + cl else float("nan")
        nnever = nw_never + nl_never
        pn = nl_never / nnever * 100 if nnever else float("nan")
        print(f"   {L:<6.2f} {already:>6} {crossed:>8} {cw:>6}/{cl:<5} "
              f"{np.median(rems) if rems else 0:>7.0f}s {np.median(durs) if durs else 0:>6.0f}s"
              f"/{max(durs) if durs else 0:>4.0f}s {rec_w:>5}/{rec_l:<4} "
              f"{pw:>9.1f}% {('%.1f%%(%d)' % (pn, nnever)) if nnever else '—':>13}")
    print("\n   读法: 「回来过」= 首次下穿之后报价曾回到 L 之上（假摔）; 它若是赢单 ⇒ 止损会杀赢。")
    print("   「已在下」多的是高价位档（入场 fill 是 ask, bid 天然低 1~2 分钱）。")

    # 亏单逐档的细节: 它们是在什么时候跌破各档的
    print("\n§1b 亏单的下穿时点（n=输单）: 跌破各档时 rem 还有多少、有没有假摔")
    print("     L    下穿过的亏单   首次下穿rem(p10/中位/p90)   下穿后回到L之上   最长停留中位")
    print("   " + "·" * 92)
    losers = [r for r in sig if not r["won"]]
    for L in LEVELS:
        rr, back, dur = [], 0, []
        for r in losers:
            m = mask_of(r)
            bid, rem = r["path"]["bid"][m], r["path"]["rem"][m]
            eps = episodes_of(bid, rem, L)
            first = next((e for e in eps if e[0] > 0 or (len(bid) and bid[0] >= L)), None)
            if first is None:
                continue
            rr.append(rem[first[0]])
            if np.any(bid[first[0]:] >= L):
                back += 1
            dur.append(max((e[1] for e in eps), default=0.0))
        if not rr:
            print(f"   {L:<6.2f} {'0':>10}")
            continue
        print(f"   {L:<6.2f} {len(rr):>10} ({len(rr)/len(losers)*100:4.0f}%) "
              f"{np.percentile(rr,10):>7.0f} / {np.median(rr):>5.0f} / {np.percentile(rr,90):<6.0f}"
              f"   {back:>5}/{len(rr):<5} {np.median(dur):>10.0f}s")


# ── §2 停留时长 vs 结局 ───────────────────────────────────────────────────

def sec2(rows):
    """P(输 | 在 L 之下已连续停留 ≥ τ 秒) —— 表格口径与 36 §2 一致, 只是量换成报价。"""
    sig = [r for r in rows if r["ok"]]
    print("\n" + "=" * 100)
    print("§2 下穿 L 后停留越久, 越危险吗（P(输 | 在 L 下方已连续 ≥ τ 秒), 窗级）")
    print("     ⚠️ 每行 = 一个价格档; 列 = 确认时长 τ; 格子 = 该窗最终输的概率(n)")
    print("     与 36 §2 的区别: 36 量的是 dev 区停留, 这里量的是**报价**在 L 下方的停留")
    head = "     " + "L".ljust(7) + "".join(f"{('τ≥%ds' % t):>12}" for t in TAUS)
    print("\n" + head)
    for L in LEVELS:
        cells = {t: [0, 0] for t in TAUS}
        for r in sig:
            m = mask_of(r)
            bid, rem = r["path"]["bid"][m], r["path"]["rem"][m]
            dw = dwell_of(bid, rem, L)
            for t in TAUS:
                if np.any(dw >= t):
                    cells[t][0 if r["won"] else 1] += 1
        line = f"     {L:<7.2f}"
        for t in TAUS:
            w, l = cells[t]
            line += f"{('%5.1f%%(%4d)' % (l/(w+l)*100, w+l)) if w+l else '—':>12}"
        print(line)

    # 关键对照: 输率随 τ 上行**不等于**停留携带报价之外的信息。真正的判据是 36 §3 那条
    # 「出场优于持有 ⟺ bid > P(win)」: 把每个格子的实际输率与那一格的**市场隐含输率**
    # (1 − 出场中位 bid) 并排 —— 两者相等就说明市场已经把这件情报掉了。
    print("\n     上表只说明「待得越久越危险」; 那是不是**报价之外的**信息, 要看实际输率与")
    print("     市场隐含输率（1 − 出场中位 bid）差多少 —— 判据同 36 §3: 出场优于持有 ⟺ bid > P(win)。")
    head2 = "     实际输率 − (1−中位bid)  (pp)"
    print("\n" + head2 + "; 括号 = 出场中位 bid")
    print("     " + "L".ljust(7) + "".join(f"{('τ≥%ds' % t):>14}" for t in TAUS))
    allgap = []
    for L in LEVELS:
        acc = {t: [] for t in TAUS}
        for r in sig:
            m = mask_of(r)
            bid, rem = r["path"]["bid"][m], r["path"]["rem"][m]
            dw = dwell_of(bid, rem, L)
            for t in TAUS:
                hit = np.nonzero(dw >= t)[0]
                if len(hit):
                    acc[t].append((1.0 if not r["won"] else 0.0, bid[hit[0]]))
        line = f"     {L:<7.2f}"
        for t in TAUS:
            v = acc[t]
            if not v:
                line += f"{'—':>14}"
                continue
            loss = sum(a for a, _ in v) / len(v)
            mb = float(np.median([b for _, b in v]))
            gap = (loss - (1 - mb)) * 100
            allgap.append(gap)
            line += f"{('%+.1f (%.3f)' % (gap, mb)):>14}"
        print(line)
    g = np.array(allgap)
    print(f"\n     全表 {len(g)} 格: 中位 {np.median(g):+.1f}pp, 正值 {int((g > 0).sum())} 格, "
          f"最大 {g.max():+.1f}pp ⇒ 市场隐含输率普遍 ≥ 实际输率（卖价还不如持有）。")


# ── §3 规则回放: bid < L 持续 ≥ τ 秒 ⇒ 出场 ──────────────────────────────

def replay_fire(sig, fires, tag, pr=1.0, quiet=False):
    """按**预计算的出场下标**回放: 出场 = 该 tick 的持仓侧 bid 卖出, 否则持有到结算。

    fires: {cid: (下标, 出场bid)}。返回 (触发数, Δ, CI下界, CI上界, 日级Δ, 触发价均值, 触发组胜率)。
    """
    base = new = 0.0
    by_day = collections.defaultdict(float)
    nfire = kill = save = 0
    pb = []
    g_save = c_kill = 0.0        # 救一个输单挽回的 U / 杀一个赢单损失的 U
    for r in sig:
        sh = STAKE / r["fill"]
        hold = (sh - STAKE) if r["won"] else -STAKE
        hit = fires.get(r["cid"])
        if hit is None:
            pnl = hold
        else:
            i, b = hit
            nfire += 1
            pnl = sh * b * pr - STAKE
            if r["won"]:
                kill += 1
                c_kill += sh * (1 - b)          # 本可拿到 1, 只卖了 b
            else:
                save += 1
                g_save += sh * b                # 本可拿到 0, 拿回 b
            pb.append(b)
        base += hold
        new += pnl
        by_day[r["date"]] += pnl - hold
    lo, hi = boot_day(by_day)
    pbar = np.mean(pb) if pb else float("nan")
    wrf = kill / nfire * 100 if nfire else float("nan")
    if not quiet:
        print(f"   {tag:<30} {nfire:>5} ({nfire/len(sig)*100:5.1f}%) {pbar:>7.3f} {wrf:>7.1f}% "
              f"{pbar*100-wrf:>+7.1f}pp {kill:>5}/{save:<5} {new-base:>+8.2f}U [{lo:>+7.2f},{hi:>+7.2f}]")
    return dict(n=nfire, d=new - base, lo=lo, hi=hi, kill=kill, save=save, pbar=pbar, wrf=wrf,
                gsave=g_save / save if save else float("nan"),
                ckill=c_kill / kill if kill else float("nan"))


def fold_for(sig, L, px_key="bid", own_bid_exit=True):
    """一次性算出所有 τ 的出场下标: {tau: {cid: (i, 出场价)}}。

    出场价 = 那一 tick 的**持仓侧 bid**（不管触发用哪个价格口径判的）。
    """
    per = {t: {} for t in TAUS}
    for r in sig:
        m = mask_of(r)
        rem = r["path"]["rem"][m]
        px = r["path"][px_key][m]
        bid = r["path"]["bid"][m]
        dw = dwell_of(px, rem, L)
        for t in TAUS:
            idx = np.flatnonzero(dw >= t)
            if len(idx):
                i = int(idx[0])
                per[t][r["cid"]] = (i, bid[i])
    return per


def sec3(rows):
    sig = [r for r in rows if r["ok"]]
    print("\n" + "=" * 100)
    print(f"§3 规则回放: 「持仓侧报价跌破 L 且连续停留 ≥ τ 秒 ⇒ 在第 τ 秒按 bid 出场」（n={len(sig)}）")
    print("    τ=0 就是 25/26 号的瞬时阈值规则（一旦跌破立刻卖）; τ 变大 = 等确认")
    print("   规则                          触发     出场价  触发组  出场价− 杀赢/救输    ΔP&L    日级配对95%CI")
    print("                                           均值   胜率    胜率")
    print("   " + "·" * 100)
    ref = []
    # 参照: 立即出场 / 持有到末刻
    ref.append(replay_fire(sig, {r["cid"]: (0, r["path"]["bid"][mask_of(r)][0])
                                 for r in sig if mask_of(r).sum()}, "立即出场（入场即卖）"))
    # 36 号的现行候选: bid<0.30 ∧ dev_bin<−20（瞬时）
    cur = {}
    for r in sig:
        m = mask_of(r)
        hit = np.flatnonzero((r["path"]["bid"][m] < 0.30) & (r["path"]["dev"][m] < -20.0))
        if len(hit):
            i = int(hit[0])
            cur[r["cid"]] = (i, r["path"]["bid"][m][i])
    ref.append(replay_fire(sig, cur, "参照: bid<.30 ∧ dev<−20"))
    print("   " + "·" * 100)
    table = {}
    for L in LEVELS:
        per = fold_for(sig, L)
        for t in TAUS:
            tag = f"L={L:.2f} τ={t:>2}s"
            res = replay_fire(sig, per[t], tag)
            table[(L, t)] = res
        print("   " + "·" * 100)
    return table, ref


def sec3b(table):
    """把 §3 的结果压成两张小表: Δ 随 τ 的曲线 + 杀赢/救输临界比。"""
    print("\n§3b 两条可读的曲线（ΔP&L 单位 U, 14 天）")
    print("     ΔP&L  vs τ:")
    print("     " + "L".ljust(8) + "".join(f"{('τ=%d' % t):>8}" for t in TAUS))
    for L in LEVELS:
        print(f"     {L:<8.2f}" + "".join(f"{table[(L,t)]['d']:>+8.2f}" for t in TAUS))
    print("\n     杀赢/救输（每救一个输单要杀掉的赢单数）:")
    print("     " + "L".ljust(8) + "".join(f"{('τ=%d' % t):>8}" for t in TAUS))
    for L in LEVELS:
        line = f"     {L:<8.2f}"
        for t in TAUS:
            v = table[(L, t)]
            line += f"{(v['kill']/v['save'] if v['save'] else float('inf')):>8.2f}"
        print(line)
    print("\n     ⚠️ 但「杀几个」不是判据 —— **赔率结构**才是。两个单位代价:")
    print("       救一个输单挽回 `股数×出场价`（本可全亏 −2U）;")
    print("       杀一个赢单损失 `股数×(1−出场价)`（本可全赢 +0.1~0.2U）。")
    print("       ⇒ **临界比 = 平均挽回 / 平均损失** = 每救 1 个输单允许杀几个赢单。")
    print("       它随 L 反向变化: 高价档救得多(+1.9U)、杀得少(−0.3U) ⇒ 临界比宽松(~6:1);")
    print("       低价档救得少(+0.2U)、杀得多(−1.9U) ⇒ 临界比苛刻(~0.1:1)。所以只能两个一起看:")
    for (L, t) in ((0.90, 0), (0.60, 0), (0.40, 8), (0.20, 3)):
        v = table[(L, t)]
        if v["kill"] and v["save"]:
            print(f"         L={L:.2f} τ={t:>2}s  救 {v['gsave']:+.2f}U/个  杀 {v['ckill']:.2f}U/个  "
                  f"临界 {v['gsave']/v['ckill']:.1f}:1  实际 {v['kill']/v['save']:.1f}:1  "
                  f"⇒ {'划算' if v['gsave']/v['ckill'] > v['kill']/v['save'] else '亏'}"
                  f"（Δ{v['d']:+.2f}U）")
    print("\n     下表 = 临界/实际（> 1 = 划算; < 1 的格子即使 Δ 看着不为负也站不住）:")
    print("     " + "L".ljust(8) + "".join(f"{('τ=%d' % t):>8}" for t in TAUS))
    for L in LEVELS:
        line = f"     {L:<8.2f}"
        for t in TAUS:
            v = table[(L, t)]
            if v["kill"] == 0 or v["save"] == 0:
                line += f"{'—':>8}"
            else:
                actual = v["kill"] / v["save"]
                crit = v["gsave"] / v["ckill"]
                line += f"{crit/actual:>8.2f}"
        print(line)
    print("\n     CI 下界 > 0 的格子（唯一值得进一步看的）:")
    hits = [(L, t, v) for (L, t), v in table.items() if v["lo"] > 0]
    hits.sort(key=lambda x: -x[2]["d"])
    for L, t, v in hits[:12]:
        print(f"       L={L:.2f} τ={t:>2}s  Δ{v['d']:+7.2f}U  [{v['lo']:+.2f},{v['hi']:+.2f}]  "
              f"触发{v['n']}  出场价均值{v['pbar']:.3f} 触发组胜率{v['wrf']:.1f}%")
    if not hits:
        print("       （无 —— 100 个格子里没有一个的 95% 区间下界站上 0）")
    return hits


# ── §4 反复下穿: 第几次才是真的 ──────────────────────────────────────────

def fold_kth(sig, L, k):
    """第 k 次下穿的出场下标: 数**穿越次数**（不等待时长）, 第 k 次跌破那一 tick 即卖。

    「入场即在下」（bid 开仓就 < L）算作第 1 次——没得跨, 本来就是下方。
    """
    per = {}
    for r in sig:
        m = mask_of(r)
        bid, rem = r["path"]["bid"][m], r["path"]["rem"][m]
        eps = episodes_of(bid, rem, L)
        if len(eps) >= k:
            i = eps[k - 1][0]
            per[r["cid"]] = (i, bid[i])
    return per


def sec4(rows):
    sig = [r for r in rows if r["ok"]]
    print("\n" + "=" * 100)
    print("§4 换个确认方式: **第 k 次下穿才卖**（数穿越次数, 不等待时长）")
    print("    与 §3 的 τ 腿不同: τ 量的是「连续在下方多久」, 这里量的是「跌了几回」")
    print("    L        k=1（首次即卖）        k=2（第二次才卖）      k=3（第三次才卖）")
    print("   " + "·" * 96)
    out = {}
    for L in LEVELS:
        line = f"   {L:<6.2f}"
        for k in (1, 2, 3):
            res = replay_fire(sig, fold_kth(sig, L, k), f"  k={k}", quiet=True)
            out[(L, k)] = res
            line += f"   {res['d']:>+7.2f}U [{res['lo']:>+6.2f},{res['hi']:>+6.2f}]"
        print(line)
    print("\n   读法: **k 越大越好是普遍的**（尤其 0.95/0.90/0.85 三档: −70 → −28 → −10）——")
    print("   与 §3 的「τ 越大越好」同向, 两个量说的是同一件事: 第一次跌破绝大多数是假摔,")
    print("   要它再跌一次才说明是真破位。但代价是出场价被让掉, 所以最好的一格也只有 +4.9U,")
    print("   与 §3 的最优格（+5.3U）在同一水平 —— 这就是这台机器能给的全部上限。")
    return out


def sec6(rows):
    """口径坑: 「下穿之后有没有回来」不能直接当信号用（这次实测发现的一个陷阱）。"""
    sig = [r for r in rows if r["ok"]]
    print("\n" + "=" * 100)
    print("§6 口径坑存档: 「回来过」这个量为什么不能直接用")
    print("   L     首次下穿n   其中最终赢    假摔率      回来过又输的   其中再次跌破占比")
    print("                              (下穿里最终赢)  (下穿后回到L之上)")
    print("   " + "·" * 96)
    for L in LEVELS:
        tot = w = back = 0
        backl = relost = 0
        for r in sig:
            m = mask_of(r)
            bid, rem = r["path"]["bid"][m], r["path"]["rem"][m]
            eps = episodes_of(bid, rem, L)
            if not eps:
                continue
            tot += 1
            w += r["won"]
            tail = bid[eps[0][0]:]
            if not np.any(tail >= L):
                continue
            back += 1
            j = eps[0][0] + int(np.argmax(tail >= L))
            if not r["won"]:
                backl += 1
                if any(e[0] > j for e in eps):
                    relost += 1
        if tot == 0:
            continue
        print(f"   {L:<6.2f} {tot:>10} {w:>9}({w/tot*100:4.1f}%) {back/tot*100:>9.1f}% "
              f"{backl:>14} {relost/backl*100 if backl else 0:>18.1f}%")
    print("\n   ⚠️ 最后一列恒为 100% —— 这是**构造性**的, 不是发现: 亏损窗的价格最终必然跌回任何 L 之下,")
    print("      所以「回来过又输掉」的窗 100% 都会「再次跌破」。同理 §1 的「没回来」那一列在低价档")
    print("      也等价于「输了」。⇒ 「下穿有没有回来」这个问题**必须**用 §3 的固定时间窗 τ 来问,")
    print("      或者用 §4 的穿越次数 k 来问; 直接问「最终回没回来」等于把结果提前读了一遍。")


# ── §5 可成交性折算 + 与现行候选对照 ─────────────────────────────────────

def sec5(rows, table, cands):
    """cands: [(标签, {cid: (下标, 出场bid)}, 原始CI文本), …]。"""
    sig = [r for r in rows if r["ok"]]
    print("\n" + "=" * 100)
    print("§5 可成交性折算（与 36 §7 同法）——只对**原始 Δ 为正**的候选做")
    print("   折扣一: 触发时**没有对手方**（整侧撤空, bid 冻结在旧值）——实盘监察 334 个候选 tick 里 66.8%")
    print("   折扣二: 可成交价 vs 老数据价 ×0.78（09-25~29 六个可成交样本）")
    print("   ⚠️ 老数据的 bid 在崩盘段系统性偏高是**构造性**的（旧采集丢空侧消息, 决策 #21）\n")
    print("   规则                        原始Δ      价格×0.78   再×2/3触发率   触发时bid中位   CI(原始)")
    for tag, fires, ci in cands:
        d0 = replay_fire_silent(sig, fires, 1.0)
        d1 = replay_fire_silent(sig, fires, 0.78)
        pb = np.array([b for _, b in fires.values()])
        med = np.median(pb) if len(pb) else float("nan")
        print(f"   {tag:<28} {d0:>+7.2f}U {d1:>+11.2f}U {d1*2/3:>+13.2f}U {med:>13.3f}   {ci}")
    print("\n   ⚠️ 折算本身是保守近似（把折扣均匀施加）, 不是精确重估; 方向是确定的: 老数据偏高。")

    # 触发用哪一侧的价格看: 卖价（ask）比买价（bid）高一个价差, 用 ask 判会**更早**触发,
    # 但出场仍然是按 bid 卖 —— 这是本族唯一一个「不改判据、只改观测口径」的变体。
    print("\n§5b 触发口径的敏感度: 用**卖价 ask** 判跌破（更早）, 用 **bid** 卖出（不变）")
    print("   规则                        按bid判Δ    按ask判Δ     出场价均值(bid/ask判)   触发数(bid/ask)")
    for (L, t), v in sorted(((k, v) for k, v in table.items() if v["d"] > 0 and v["kill"] > 0),
                            key=lambda kv: -kv[1]["d"])[:4]:
        f_bid = fold_for(sig, L, "bid")[t]
        f_ask = fold_for(sig, L, "ask")[t]
        d_bid = replay_fire_silent(sig, f_bid, 1.0)
        d_ask = replay_fire_silent(sig, f_ask, 1.0)
        mb = np.mean([b for _, b in f_bid.values()]) if f_bid else float("nan")
        ma = np.mean([b for _, b in f_ask.values()]) if f_ask else float("nan")
        print(f"   L={L:.2f} τ={t:>2}s               {d_bid:>+7.2f}U  {d_ask:>+8.2f}U    "
              f"{mb:.3f} / {ma:.3f}          {len(f_bid):>4}/{len(f_ask):<4}")


def replay_fire_silent(sig, fires, pr):
    base = new = 0.0
    for r in sig:
        hold = (STAKE / r["fill"] - STAKE) if r["won"] else -STAKE
        hit = fires.get(r["cid"])
        new += hold if hit is None else STAKE / r["fill"] * hit[1] * pr - STAKE
        base += hold
    return new - base


# ── §6 上穿回来之后呢（假摔的代价 / 收益）────────────────────────────────

# ── main ──────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()
    rows = get(args.rebuild)
    if not sec0(rows):
        return
    sec0b(rows)
    sec1(rows)
    sec2(rows)
    sig = [r for r in rows if r["ok"]]
    table, _ = sec3(rows)
    hits = sec3b(table)
    ktab = sec4(rows)
    sec5(rows, table, cands_of(sig, table, hits, ktab))
    sec6(rows)


def cands_of(sig, table, hits, ktab):
    """§5 折价表的候选: 参照 + §3 最优 + §4 最优（都要求 原始Δ > 0 且杀赢 > 0）。"""
    out = [("参照: bid<.30 ∧ dev<−20", fire_ref(sig), "")]
    top3 = sorted(((k, v) for k, v in table.items() if v["d"] > 0 and v["kill"] > 0),
                  key=lambda kv: -kv[1]["d"])[:3]
    for (L, t), v in top3:
        out.append((f"§3 L={L:.2f} τ={t}s", fold_for(sig, L)[t], f"[{v['lo']:+.2f},{v['hi']:+.2f}]"))
    topk = sorted(((k, v) for k, v in ktab.items() if v["d"] > 0 and v["kill"] > 0),
                  key=lambda kv: -kv[1]["d"])[:2]
    for (L, k), v in topk:
        out.append((f"§4 L={L:.2f} k={k}", fold_kth(sig, L, k), f"[{v['lo']:+.2f},{v['hi']:+.2f}]"))
    return out


if __name__ == "__main__":
    main()
