#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫尾盘三段链 **+ 持仓止损**（2026-09-25）

= `python/v4/23_tail_integrated.py` 的**拷贝**，只多一条「持仓期间止损」腿。

⚠️ 为什么是拷贝而不是直接改 23：23 是 `internal/tail/parity_test.go` 的 oracle，
   动它会毁掉 Go↔py 对账基线（决策 #17/#22 的验收红线）。本脚本**基线部分必须
   逐位复现 23 的四个数字**，末尾有硬断言自检（跑不通就是抄错了）。

## 止损规则（默认值 = 25 号脚本扫描出的平台中心）

    触发 = 持仓侧 bid < STOP_BID(0.30)  ∧  dev < STOP_DEV(−20 美元)

- `dev = sgn·(spot − anchor)`（美元，正 = 朝押注方向），与入场判定同一口径。
- 触发那一秒按**持仓侧 bid 卖出**（taker），`P&L = shares·bid − STAKE`。
- **可评估 tick**（止损只看这些）= 延迟 ≤ 300ms ∧ spot 在场 ∧ **持仓侧 bid > 0** ∧ rem > 0。
  最后一条是硬前提：没有 bid 就卖不掉。⚠️ 这是 23 的 `win_ticks` 之外的第二套门
  （23 只管到 rem ≤ 150 为止的**判定** tick；止损要看**入场之后到闭市**的整条路径）。

## 两种胜率口径（必须分开看，用户前提「不能影响胜率」取决于用哪个）

- **口径 A · 官方 outcome**（引擎现行）：`settle_won` 不动 ⇒ WR 不变 95.97%，
  但被止损**最终官方赢**的那些行会出现「判赢却负 P&L」。
- **口径 B · 落袋**：卖出即定局。`fill ≥ 0.80` 而止损价 < 0.30 ⇒ 被止损行**必然亏损**
  ⇒ WR = (赢家 − 杀赢) / n，一定下降。

## ⚠️ 上界声明（读数前必读）

1. **对手方**：本脚本假设触发那一秒有 bid 可吃。决策 #21 活体取证：尾盘**输家侧 bid 被
   整侧撤空**（探针 rem=12/28/45/47；ETH 首采 rem ∈ [0,34] / [0,79]）——止损要出场那一刻
   正是持仓变输家那一刻。历史 14 天持仓侧 bid==0 出现 **0 次**，是旧采集守卫丢弃空侧
   消息的**构造性产物**（决策 #21/#23）⇒ **所有 Δ 都是上界**。
2. **滑点**：无。按 bid 全额成交，实际 taker 卖出会打折。
3. **判别力**：触发那一刻没有任何可用特征能区分真翻盘/假摔（25 号脚本的 AUC 表：
   BTC 量 0.485~0.499 ≈ 纯噪声）⇒ 这个止损靠的是**后验的残值**，不是先验的判断力。

用法:
    python/venv/bin/python python/v4/26_tail_integrated_stoploss.py
    python/venv/bin/python python/v4/26_tail_integrated_stoploss.py --stop-bid 0.25 --stop-dev -25
    python/venv/bin/python python/v4/26_tail_integrated_stoploss.py --stop-bid 0     # 关掉止损（应逐位回到基线）
"""
import sys
import random
import datetime
import argparse
import collections
import importlib.util
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))
from v2.lib import load_events                                    # noqa: E402

STAKE = 2.0
MAX_LAT = 300
P_FLOOR = 0.80
DEV_USD = 63.0
SD_MIN_USD = 40.0
T150, T60 = 150, 60
FIELDS = ("yes_bid", "yes_ask", "no_bid", "no_ask")

STAGES = ("t150", "t60", "listen")

# 止损腿默认值（25 号脚本的扫描平台中心：bid<0.25~0.30 ∧ dev<−20 整片同号）
STOP_BID = 0.30
STOP_DEV = -20.0

# 23 号脚本的 oracle 数字（自检用；改动即须同步 internal/tail/parity_test.go）
ORACLE = {
    "t150":   (3638, 1220, 93.934426, 16.022637),
    "t60":    (2414,  568, 99.119718, 15.756180),
    "listen": ( 347,  347, 97.982709,  3.895932),
    "合计":   (6399, 2135, 95.971897, 35.674749),
}
ORACLE_WINDOWS, ORACLE_NOSIGMA = 3640, 3


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


s13 = load("s13", BASE / "13_tail_sweep.py")
s14 = load("s14", BASE / "14_tail_sweep_sigma.py")


# ── 数据提取（与 23 逐字一致，只多带 tick 下标 i 供持仓路径回放） ──────────

def win_ticks(e):
    """本窗**可判定 tick**（时间顺序, rem 递减）——口径见 23 号文件头 1。

    ⚠️ 只收 `rem ≤ T150` 的 tick。与 23 的唯一差异 = 每条多一个 `i`（在全量 ticks
    里的下标），供止损回放用；**不参与任何判定**。
    """
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
            continue                       # 四档门（差集审计见第五节）
        spot = (x.get("bin") or {}).get("price")
        if not spot:
            continue                       # 缺 spot 只跳过, 不推进段
        ya, na = p.get("yes_ask") or 0, p.get("no_ask") or 0
        side = "yes" if ya >= na else "no"  # 平局取 yes（与 Go HotBook 同）
        out.append({"rem": rem, "side": side,
                    "fill": (ya if side == "yes" else na),
                    "spot": spot, "twap": (x.get("twap") or {}).get("price"),
                    "i": i})
    return out


def row(t, anchor, sd, date, outcome, stage):
    sgn = 1.0 if t["side"] == "yes" else -1.0
    dev = sgn * (t["spot"] - anchor)
    return {
        "date": date, "stage": stage, "rem": t["rem"], "side": t["side"],
        "fill": t["fill"], "buy": t["fill"],      # buy 别名: 复用 14 的 pl/day_bootstrap
        "dev": dev, "sd": sd, "sig": (dev / sd) if sd else None,
        "settle_won": 1 if ((outcome == 0) if t["side"] == "yes" else (outcome == 1)) else 0,
        "i": t.get("i"),                          # ← 唯一新增字段（止损回放用）
    }


def r5(r):
    """⑤ = 价格腿 ∧ (dev ≥ 63 ∨ (sd ≥ 40 ∧ dev ≥ sd))。"""
    if not (r["fill"] >= P_FLOOR):
        return False
    if r["dev"] >= DEV_USD:
        return True
    return r["sd"] is not None and r["sd"] >= SD_MIN_USD and r["sig"] is not None and r["sig"] >= 1.0


def r2(r):
    """② = 价格腿 ∧ dev ≥ 63（监听段的规则, 不含 σ 腿）。"""
    return r["fill"] >= P_FLOOR and r["dev"] >= DEV_USD


def chain(ticks, anchor, sd, date, outcome):
    """三段递进判定链 → 本窗产出的行（至多 3 条）。**与 23 逐字一致。**"""
    if not ticks:
        return [], []
    rows, rejects = [], []
    head = ticks[0]
    if head["rem"] > T60:
        r = row(head, anchor, sd, date, outcome, "t150")
        if r5(r):
            r["ok"] = True
            return [r], rejects
        rejects.append("t150")
        rows.append(r)
        rest = [x for x in ticks if x["rem"] <= T60]
    else:
        rest = ticks
    if rest:
        t2 = rest[0]
        r2r = row(t2, anchor, sd, date, outcome, "t60")
        if r5(r2r):
            r2r["ok"] = True
            return rows + [r2r], rejects
        rejects.append("t60")
        rows.append(r2r)
        for x in rest[1:]:
            rl = row(x, anchor, sd, date, outcome, "listen")
            if r2(rl):
                rl["ok"] = True
                rows.append(rl)
                return rows, rejects
    return rows, rejects


# ── 止损腿（新增） ────────────────────────────────────────────────────────

def post(e, r):
    """持仓后的可评估路径 `[(rem, dev, 持仓侧 bid, bid_top5 深度)]`。

    与 23 的 `win_ticks` 不同：这是**第二套门**，只管止损看什么——
    延迟 ≤ 300ms ∧ spot 在场 ∧ 持仓侧 bid > 0 ∧ rem > 0。
    **不要求 rem ≤ 150**（持仓后一路看到闭市）、**不要求四档齐全**（只看持仓侧）。
    """
    anchor = e.get("twap_open_price")
    if not anchor or r.get("i") is None:
        return []
    sgn = 1.0 if r["side"] == "yes" else -1.0
    bidk = r["side"] + "_bid"
    szk = r["side"] + "_bid_top5"
    out = []
    for x in e["ticks"][r["i"] + 1:]:
        rem = x.get("rem")
        if rem is None or rem <= 0:
            continue
        p = x.get("pm") or {}
        if not p or (p.get("book_latency_ms") or 0) > MAX_LAT:
            continue
        spot = (x.get("bin") or {}).get("price")
        if not spot:
            continue
        b = p.get(bidk) or 0.0
        if b <= 0:
            continue                       # 没有 bid ⇒ 卖不掉（实盘红线）
        out.append((rem, sgn * (spot - anchor), b, p.get(szk) or 0.0))
    return out


def find_stop(path, stop_bid, stop_dev):
    """首个满足 [持仓侧 bid < stop_bid] ∧ [dev < stop_dev] 的可评估 tick → (rem,dev,bid,sz)。"""
    if stop_bid <= 0 or stop_dev is None:
        return None
    for rem, dev, b, sz in path:
        if b < stop_bid and dev < stop_dev:
            return (rem, dev, b, sz)
    return None


def attach_stop(e, r, path, stop_bid, stop_dev):
    """给一行挂上止损结果：`shares` / `pnl_hold` / `stopped` / `pnl_stop`。"""
    sh = STAKE / r["fill"]
    hold = (sh - STAKE) if r["settle_won"] else -STAKE
    r["shares"] = sh
    r["pnl_hold"] = hold
    r["stopped"] = find_stop(path, stop_bid, stop_dev)
    r["pnl_stop"] = (sh * r["stopped"][2] - STAKE) if r["stopped"] else hold


# ── 统计输出 ──────────────────────────────────────────────────────────────

def pl(rows):
    """基线 P&L（= 23 的 pl，逐字一致）。"""
    s = 0.0
    for r in rows:
        sh = STAKE / r["fill"]
        s += (sh - STAKE) if r["settle_won"] else -STAKE
    return s


def pl_stop(rows):
    """止损后 P&L。"""
    return sum(r["pnl_stop"] for r in rows)


def by_day(rows):
    d = collections.defaultdict(list)
    for r in rows:
        d[r["date"]].append(r)
    return d


def boot(rows, reps=2000, seed=42):
    """日级 bootstrap 的 P&L 区间（口径与 14.day_bootstrap 同, 但 P&L 走止损）。

    ⚠️ 与 23 的 `ci()` **不逐位可比**——23 复用同一个 `random.Random(42)` 流,
    每次调用都消耗它, 有顺序依赖；这里独立起流, 只比区间量级。
    """
    ds = sorted(set(r["date"] for r in rows if r["date"] != "2026-08-31"))
    if len(rows) < 5 or not ds:
        return None
    d, rng, n = by_day(rows), random.Random(seed), len(rows)
    bp = []
    for _ in range(reps):
        pool = [r for k in (rng.choice(ds) for _ in ds) for r in d[k]]
        bp.append(sum(x["pnl_stop"] for x in pool))
    bp.sort()
    # CPython 3.12 的 quantile 口径与 14 的 a[int(p*len)] 同形
    return bp[int(0.025 * len(bp))], bp[int(0.975 * len(bp))]


def boot_delta(rows, reps=2000, seed=42):
    """**配对** bootstrap 的 Δ 区间（同一批重采样日期上算 止损后 − 基线）。

    这才是「止损有没有用」的判据——上面 `boot()` 给的是止损后 P&L 的区间，
    基线点估计落在里面很正常（两者高度相关），不构成显著性判断。
    """
    ds = sorted(set(r["date"] for r in rows if r["date"] != "2026-08-31"))
    if len(rows) < 5 or not ds:
        return None
    d, rng = by_day(rows), random.Random(seed)
    out = []
    for _ in range(reps):
        pool = [r for k in (rng.choice(ds) for _ in ds) for r in d[k]]
        out.append(sum(x["pnl_stop"] - x["pnl_hold"] for x in pool))
    out.sort()
    return out[int(0.025 * len(out))], out[int(0.975 * len(out))]


def line(lab, rows, width=30):
    if not rows:
        print("  " + lab.ljust(width) + "n=0")
        return
    n = len(rows)
    wr = sum(r["settle_won"] for r in rows) / n
    px = sum(r["fill"] for r in rows) / n
    d = by_day(rows)
    neg = sum(1 for v in d.values() if pl(v) < 0)
    print("  " + lab.ljust(width) + f"n={n:<5} WR {wr*100:6.2f}%  均价 {px:.4f}  "
          f"EV/注 {pl(rows)/n:+.4f}  P&L {pl(rows):+7.2f}U  亏损日 {neg}/{len(d)}")


def line_stop(lab, rows, width=30):
    """止损后同一行（口径 B 落袋 WR + 止损 P&L + Δ）。"""
    if not rows:
        print("  " + lab.ljust(width) + "n=0")
        return
    n = len(rows)
    fire = [r for r in rows if r["stopped"]]
    killed = [r for r in fire if r["settle_won"]]
    saved = [r for r in fire if not r["settle_won"]]
    wr_a = sum(r["settle_won"] for r in rows) / n            # 官方 outcome 口径
    wr_b = (sum(r["settle_won"] for r in rows) - len(killed)) / n   # 落袋口径
    d = by_day(rows)
    neg = sum(1 for v in d.values() if pl_stop(v) < 0)
    print("  " + lab.ljust(width) + f"n={n:<5} WR {wr_a*100:6.2f}%→{wr_b*100:6.2f}%  "
          f"触发 {len(fire):<4}(杀赢 {len(killed)} 救输 {len(saved)})  "
          f"P&L {pl_stop(rows):+7.2f}U  Δ {pl_stop(rows)-pl(rows):+6.2f}U  亏损日 {neg}/{len(d)}")


# ── 主流程 ────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="扫尾盘三段链 + 持仓止损（23 的拷贝 + 一条腿）")
    ap.add_argument("--data", default="data/btc")
    ap.add_argument("--stop-bid", type=float, default=STOP_BID,
                    help="止损价格腿：持仓侧 bid 低于此即候选（0 = 关掉止损）")
    ap.add_argument("--stop-dev", type=float, default=STOP_DEV,
                    help="止损位移腿：dev 低于此（美元）")
    ap.add_argument("--dump", action="store_true", help="逐笔打印被止损的行")
    ap.add_argument("--grid", action="store_true", help="(bid × dev) 阈值敏感性网格")
    args = ap.parse_args()

    ev = load_events(args.data)
    hr = s13.hist_ranges(ev)
    stop_bid, stop_dev = args.stop_bid, args.stop_dev

    signals = {s: [] for s in STAGES}
    rows_n = collections.Counter()
    skipped_no_sigma = windows = no_sigma_windows = 0
    audit = collections.Counter()
    margin = float("inf")
    npath = 0                       # 持仓后可评估 tick 总数（口径审计用）
    npath_nobid = 0                 # 其中「持仓侧 bid == 0」被丢掉的

    def edges(r):
        m = abs(r["dev"] - DEV_USD)
        if r["sd"] is not None:
            m = min(m, abs(r["sd"] - SD_MIN_USD), abs(r["dev"] - r["sd"]))
        return m

    for e in ev:
        outcome, anchor = e.get("outcome"), e.get("twap_open_price")
        if outcome is None or not anchor:
            continue
        h = hr.get(e["start_time"])
        if h is None:
            skipped_no_sigma += 1
            no_sigma_windows += 1
            continue                           # σ 未就绪 ⇒ 整窗跳过（决策 #13）
        sd = h / anchor * 1e4 * anchor / 1e4
        date = datetime.datetime.fromtimestamp(
            e["start_time"], datetime.timezone.utc).strftime("%Y-%m-%d")

        ticks = win_ticks(e)
        if not ticks:
            continue
        windows += 1
        rows, _ = chain(ticks, anchor, sd, date, outcome)
        for r in rows:
            rows_n[r["stage"]] += 1
            margin = min(margin, edges(r))
            if r.get("ok"):
                p = post(e, r)
                attach_stop(e, r, p, stop_bid, stop_dev)
                r["path"] = p                    # 网格扫描要重挂不同阈值
                npath += len(p)
                signals[r["stage"]].append(r)

        # 持仓侧 bid==0 的丢 tick 计数（实盘红线量化：本样本应恒 0）
        for r in rows:
            if not r.get("ok") or r.get("i") is None:
                continue
            for x in e["ticks"][r["i"] + 1:]:
                rem = x.get("rem")
                if rem is None or rem <= 0:
                    continue
                p = x.get("pm") or {}
                if not p or (p.get("book_latency_ms") or 0) > MAX_LAT:
                    continue
                if not (x.get("bin") or {}).get("price"):
                    continue
                if not (p.get(r["side"] + "_bid") or 0):
                    npath_nobid += 1

        for x in e.get("ticks") or []:
            p = x.get("pm") or {}
            if not p or (p.get("book_latency_ms") or 0) > MAX_LAT:
                continue
            rem = x.get("rem")
            if rem is None or rem <= 0 or rem > T150:
                continue
            audit["参与审计的 tick 总数"] += 1
            quad = all((p.get(k) or 0) > 0 for k in FIELDS)
            eff = {}
            for side, (bid_k, ask_k) in (("yes", ("yes_bid", "yes_ask")),
                                         ("no", ("no_bid", "no_ask"))):
                bid, ask = p.get(bid_k) or 0, p.get(ask_k) or 0
                eff[side] = ask if ask > 0 else bid
            if quad:
                if not (x.get("twap") or {}).get("price"):
                    audit["四档齐但 twap 缺（引擎不用 twap 判, 不影响宇宙）"] += 1
                continue
            if not any(eff.values()):
                audit["整簿全空（四档齐 0 = 该窗无 PM 簿, 两套门一致判无效）"] += 1
                continue
            audit["四档部分缺（单侧空 ask 等）"] += 1
            if eff["yes"] > 0 or eff["no"] > 0:
                audit["  └ 有效价门放行而四档门拒绝（★ 宇宙分家）"] += 1

    allsig = [r for s in STAGES for r in signals[s]]

    print("=" * 100)
    print("一、三段递进判定链（14 天, 2U/注, σ 未就绪窗跳过; 任一段出信号即整窗只下一单）")
    print("=" * 100)
    for s in STAGES:
        line(f"{s} 段信号", signals[s])
    line("合计（三段全部）", allsig)
    print(f"\n  参考窗数: 参与判定 {windows} 窗; σ 未就绪跳过 {skipped_no_sigma} 窗")

    print("\n" + "=" * 100)
    print("二、行计数（判定行成功与否都落盘 + 信号行; parity 断言的就是这三组）")
    print("=" * 100)
    print(f"  {'段':<8}{'全部行':>8}{'信号行':>8}{'被拒判定行':>12}")
    for s in STAGES:
        print(f"  {s:<8}{rows_n[s]:>8}{len(signals[s]):>8}{rows_n[s] - len(signals[s]):>12}")
    print(f"  {'合计':<8}{sum(rows_n.values()):>8}{len(allsig):>8}"
          f"{sum(rows_n.values()) - len(allsig):>12}")
    print(f"  判定边界的最小余量: {margin:.3e}")

    print("\n" + "=" * 100)
    print(f"三、★ 止损结果（触发 = 持仓侧 bid < {stop_bid:g} ∧ dev < {stop_dev:+g} 美元; "
          f"按触发秒 bid 卖出）")
    print("=" * 100)
    if stop_bid <= 0:
        print("  止损已关闭（--stop-bid 0）——下面每一行应逐位等于基线。")
    for s in STAGES:
        line_stop(f"{s} 段止损后", signals[s])
    line_stop("合计止损后", allsig)
    b_all = pl(allsig)
    s_all = pl_stop(allsig)
    print(f"\n  基线 P&L {b_all:+.2f}U → 止损后 {s_all:+.2f}U   （Δ {s_all-b_all:+.2f}U, "
          f"相对基线 {(s_all-b_all)/abs(b_all)*100:+.1f}%）")
    dci = boot_delta(allsig)
    print(f"  **Δ 的配对 bootstrap 95%（日级, 2000 次, seed 42）: "
          f"[{dci[0]:+.2f}, {dci[1]:+.2f}]**  "
          f"{'⇒ 下界 > 0, 显著' if dci[0] > 0 else '⇒ 区间含 0, 不显著'}")
    print(f"  （止损后 P&L 的绝对区间 {tuple(round(v,1) for v in boot(allsig))} "
          f"仅供参考——基线与止损后高度相关, 拿它判显著性会误判）")

    fire = [r for r in allsig if r["stopped"]]
    killed = [r for r in fire if r["settle_won"]]
    saved = [r for r in fire if not r["settle_won"]]
    if fire:
        gain = sum(r["shares"] * r["stopped"][2] for r in saved)               # 救输: 捞回残值
        loss = sum(r["shares"] * (r["stopped"][2] - 1) for r in killed)        # 杀赢: 亏掉拿满的 1
        print(f"\n  Δ 分解（结构恒等式, 25 号脚本）: 救输捞回 {gain:+.2f}U "
              f"+ 杀赢损失 {loss:+.2f}U = {gain+loss:+.2f}U")
        pbar = sum(r["stopped"][2] for r in fire) / len(fire)
        print(f"  触发均出场价 {pbar:.3f} ⇒ 盈亏平衡所需精度 {1-pbar:.1%}, "
              f"实际精度 {len(saved)/len(fire):.1%}"
              f"  (差 {(len(saved)/len(fire))-(1-pbar):+.1%})")
        # ★ 用户判据：杀赢会不会把收益拖到比不止损还差
        crit = (1 - pbar) / pbar
        ratio = len(saved) / len(killed) if killed else float("inf")
        print(f"\n  ★ 杀赢/救输 临界比 = (1−均出场价)/均出场价 = {crit:.1f} : 1")
        print(f"     ⇒ 每杀 1 个赢家, 至少要救回 {crit:.1f} 个输家才不亏。实际 {ratio:.1f} : 1"
              f"（安全边际 {ratio/crit:.1f}×）")
        print(f"     ⇒ 单笔账: 救输平均 {gain/len(saved):+.3f}U/笔, "
              f"杀赢平均 {loss/len(killed) if killed else 0:+.3f}U/笔"
              f"（{'净正' if gain+loss > 0 else '★ 净负——按你的判据应放弃'}）")
        rms = sorted(r["stopped"][0] for r in fire)
        pxs = sorted(r["stopped"][2] for r in fire)
        q = lambda a, p: a[min(len(a) - 1, int(p * len(a)))]                  # noqa: E731
        print(f"  触发时点 rem: p10 {q(rms,.1):.0f} p25 {q(rms,.25):.0f} p50 {q(rms,.5):.0f} "
              f"p75 {q(rms,.75):.0f}  出场价: p10 {q(pxs,.1):.3f} p50 {q(pxs,.5):.3f} "
              f"p90 {q(pxs,.9):.3f}")
        print(f"  触发占全部信号 {len(fire)/len(allsig)*100:.2f}%")

    if args.grid:
        GBID = (0.40, 0.35, 0.30, 0.25, 0.20, 0.15)
        GDEV = (-35, -30, -25, -20, -15, -10)
        print("\n" + "=" * 100)
        print("三之二、阈值敏感性网格（Δ U/14 天; 行 = 价格腿, 列 = dev 腿美元）")
        print("=" * 100)
        print(f"  {'bid<':>7}" + "".join(f"{d:>10}" for d in GDEV))
        for sb in GBID:
            cells = []
            for sdev in GDEV:
                tot = 0.0
                for r in allsig:
                    st = find_stop(r["path"], sb, sdev)
                    tot += (r["shares"] * st[2] - STAKE) if st else r["pnl_hold"]
                cells.append(tot - b_all)
            print(f"  {sb:>7.2f}" + "".join(f"{v:>+10.2f}" for v in cells))
        print(f"\n  触发数/杀赢数（同一网格）")
        print(f"  {'bid<':>7}" + "".join(f"{d:>10}" for d in GDEV))
        for sb in GBID:
            cells = []
            for sdev in GDEV:
                f2 = [r for r in allsig if find_stop(r["path"], sb, sdev)]
                cells.append(f"{len(f2)}/{sum(1 for r in f2 if r['settle_won'])}")
            print(f"  {sb:>7.2f}" + "".join(f"{v:>10}" for v in cells))
        print("  （整片同号 = 平台; 单点凸起 = 噪声尖峰; 杀赢数随 bid 放宽而上升）")

    print("\n" + "=" * 100)
    print("四、逐日明细（基线 P&L / 止损后 P&L / Δ）")
    print("=" * 100)
    days = sorted(set(r["date"] for r in allsig))
    print(f"  {'日期':<12}{'t150':>7}{'t60':>7}{'listen':>8}{'合计':>7}{'WR':>8}"
          f"{'基线':>10}{'止损后':>10}{'Δ':>9}{'触发':>6}")
    for d in days:
        day = [r for r in allsig if r["date"] == d]
        per = {s: [r for r in signals[s] if r["date"] == d] for s in STAGES}
        wr = sum(r["settle_won"] for r in day) / len(day) * 100
        print(f"  {d:<12}{len(per['t150']):>7}{len(per['t60']):>7}{len(per['listen']):>8}"
              f"{len(day):>7}{wr:>7.1f}%{pl(day):>+10.2f}{pl_stop(day):>+10.2f}"
              f"{pl_stop(day)-pl(day):>+9.2f}{sum(1 for r in day if r['stopped']):>6}")
    print(f"  {'合计':<12}{len(signals['t150']):>7}{len(signals['t60']):>7}"
          f"{len(signals['listen']):>8}{len(allsig):>7}"
          f"{sum(r['settle_won'] for r in allsig)/len(allsig)*100:>7.1f}%"
          f"{pl(allsig):>+10.2f}{pl_stop(allsig):>+10.2f}"
          f"{pl_stop(allsig)-pl(allsig):>+9.2f}{len(fire):>6}")

    print("\n" + "=" * 100)
    print("五、parity 断言表（= 23 号脚本的 oracle, 逐位自检）")
    print("=" * 100)
    print(f"  {'段':<8}{'行数':>8}{'信号':>8}{'WR(%)':>12}{'P&L(U)':>14}{'自检':>6}")
    ok_all = True
    for s in STAGES + ("合计",):
        if s == "合计":
            rr, sig = sum(rows_n.values()), allsig
        else:
            rr, sig = rows_n[s], signals[s]
        wr = (sum(r["settle_won"] for r in sig) / len(sig) * 100) if sig else 0.0
        p = pl(sig)
        o = ORACLE[s]
        good = (rr == o[0] and len(sig) == o[1]
                and abs(wr - o[2]) < 1e-4 and abs(p - o[3]) < 1e-4)
        ok_all &= good
        print(f"  {s:<8}{rr:>8}{len(sig):>8}{wr:>12.6f}{p:>14.6f}{'✅' if good else '❌':>6}")
    ok_all &= (windows == ORACLE_WINDOWS and skipped_no_sigma == ORACLE_NOSIGMA)
    print(f"  // 参与判定的窗数 = {windows}（oracle {ORACLE_WINDOWS}）; "
          f"σ 未就绪跳过 = {skipped_no_sigma} 窗（oracle {ORACLE_NOSIGMA}）"
          f"  {'✅' if windows==ORACLE_WINDOWS and skipped_no_sigma==ORACLE_NOSIGMA else '❌'}")
    print(f"  // 基线自检: {'✅ 与 23_tail_integrated.py 逐位一致（拷贝未引入偏差）' if ok_all else '❌ 与 23 不一致——拷贝过程出错, 上面的止损 Δ 不可信'}")

    print("\n" + "=" * 100)
    print("六、审计：空簿 / twap 缺失 / 门控差集 + **持仓侧 bid 缺失**（止损可执行性）")
    print("=" * 100)
    for k, v in audit.most_common():
        print(f"  {k:<30} {v}")
    print(f"\n  持仓后可评估 tick（止损看得见的）: {npath}")
    print(f"  └ 其中持仓侧 bid == 0 被丢弃的 tick: {npath_nobid}"
          f"  {'（与旧数据一致 = 构造性 0, 决策 #21）' if npath_nobid == 0 else '（★ 非 0, 与决策 #21 的构造性 0 不符）'}")

    if args.dump and fire:
        print("\n" + "=" * 100)
        print("七、被止损的行逐笔（时间 / 段 / 侧 / 入场 fill / 触发 rem / dev / bid / 官方结果）")
        print("=" * 100)
        for r in sorted(fire, key=lambda x: (x["date"], -x["stopped"][0])):
            rem, dev, b, sz = r["stopped"]
            print(f"  {r['date']} {r['stage']:<7}{r['side']:<4}fill {r['fill']:.2f}  "
                  f"rem {rem:>3.0f}  dev {dev:+8.1f}  bid {b:.3f}  "
                  f"shares {r['shares']:.2f}  {'官方赢' if r['settle_won'] else '官方输'}"
                  f"  pnl {r['pnl_stop']:+.3f}")


if __name__ == "__main__":
    main()
