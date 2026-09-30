#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫尾盘：**三段递进判定链**的 oracle（2026-09-24）

对应用户决定 `a.md` 第 1 条（落引擎口径见 docs/tail_integrated_2026-09-24.md）：

    T=150  首个可判定 tick 且 rem ≤ 150 → 判 ⑤，达标即下单
    T=60   前段没出信号, 首个可判定 tick 且 rem ≤ 60 → 再判 ⑤，达标即下单
    监听   前两段都没信号, 此后**每秒**判 ②，达标即下单（直到 rem ≤ 0）

    ⑤ = hot ≥ 0.80 ∧ ( dev ≥ 63 或 ( sd ≥ 40 且 dev ≥ sd ) )
    ② = hot ≥ 0.80 ∧   dev ≥ 63

⚠️ 2026-09-26 起 **T=150 段的价格腿改为严格大于**（`hot > 0.80`）——其余段（T=60 的 ⑤
与监听段 ②）一字不改，仍是非严格 ≥。理由见 `docs/tail_integrated_2026-09-24.md` §6:
报价落在 0.01 网格上, `hot == 0.80` 是常态形态而非浮点边界; 实盘 09-24~25 那 4 笔
0.80 入场 WR 50% / −14.38U, 而同批 > 0.80 的 201 笔 WR 98.5% / +70.15U; 14 天回测里
0.80 桶同样是整条价格梯度上唯一的负 EV 档（n=17 WR 76.5% −1.50U）。见 r5(strict_price)。

⚠️ 2026-09-29 起 **T=150 段加一道入场闸 `walk ≥ 43 美元`**（用户决定, 决策 #29; 分析
`docs/tail_walk_gate_2026-09-29.md`）——`walk = sgn·(twap − anchor)`（本文 `row()` 的
`walk` 键）, 也就是「**已经写进结算线的**那部分位移」（`dev = basis + walk`,
basis = sgn·(spot − twap) 是还没写进去的缺口）。T=150 段上 `⑤ 达标但 walk < 43`
⇒ **本段不出信号, 链继续**走到 T=60/监听（**不是**整窗丢弃: 那一种明显更差, Δ 只有
+14.48U 且区间含零）。其余两段**一字不动**。理由 = 段 1 是位移最没写进去的决策点,
`dev ≥ 63` 由缺口撑起来的单在这里最容易混进来（14 天 2U/注: 合计 +35.09 → **+64.03U**,
只少 53 笔 = 2.5%, 被闸那批输率 49.1% vs 保留 3.98%）。见 walk_ok()。

任一段出信号即**整窗只下一单**。三行的 stage 分别记 t150 / t60 / listen。

本脚本是 `internal/tail/parity_test.go` 的 oracle（Go 引擎逐窗重放的对照真值）,
也是纸面判决的离线口径来源。与旧脚本的关系:

    13/15/16  T=60 快照一次（旧 ⑤）与 T=150/60 监听（A/B/C/D 四口径对照）
    23（本）  三段递进链 —— 旧口径的「A 快照 T=60」是它的特例

⚠️ 与 Go 引擎逐条对齐的口径（parity 断言的就是这些）:

  1. **可判定 tick** = 延迟 ≤ 300ms ∧ 热门侧**有效价** > 0 ∧ spot > 0 ∧ rem > 0。
     有效价 = `ask` 优先、`bid` 兜底（2026-09-24 起, a.md 第 2 条）——**不再是四档齐全**。
     历史 14 天里没有一次单侧空簿（本脚本第五节实测）, 故这里仍用四档门 +
     第 5 节的差集审计：一旦审计非 0, 两套宇宙就分家, parity 会立刻报出来。
  2. **σ 未就绪（sd is None）整窗跳过**——与 cmd/tail 的前置闸同源（决策 #13:
     `hist.Count() < HistMin` ⇒ 整窗不产出观测）。引擎内另有 no_hist 分支,
     那是纯函数防线（现网到不了）, 故 oracle 不做「σ 缺失仍判 dev 腿」的放行。
  3. **迟到接入**（首个可判定 tick 已 rem ≤ 60）跳过 T=150 段, **不伪造 t150 行**。
  4. **缺 spot 的 tick 只跳过、不推进任何段**（等下一个可判定 tick）——不是整窗丢弃。
  5. **T=150 段的入场闸**（2026-09-29 决策 #29）: ⑤ 达标 ∧ `walk ≥ 43 美元` 才算信号,
     walk 不够 ⇒ 落一行判定行（拒绝原因 walk_low）、**链继续**。⚠️ twap 缺失（walk 无值）
     ⇒ 闸**放行**（fail-open, 与 Go 的 WalkLeg 同口径）——14 天里 T=150 判定 tick 的
     twap 从不缺失, 故这一支在回测里零命中。

成交口径与 13/16 恒等: fill = 下单 tick 热门侧有效价; shares = STAKE/fill;
赢 → shares−STAKE, 输 → −STAKE。判决区间复用 14 的日级 bootstrap（2000 次, seed 42）。

用法: python/venv/bin/python python/v4/23_tail_integrated.py
"""
import sys
import random
import datetime
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
WALK_MIN_USD = 43.0        # T=150 段入场闸（决策 #29）。⚠️ 引擎侧已改成配置键 tail.walk_min_usd
                           # （默认 43, 为 ETH 等标的各带标定值）——本脚本**不读配置**, 恒定 43:
                           # 它是 parity 的对账口径, 与引擎 DefaultConfig() 逐位一致
LISTEN_MIN_PRICE = 0.83    # 监听段**价格地板**（2026-10-01 决策 #32）: ② 达标之外还要热门侧
                           # 有效价**严格大于**此值才出信号; 被拦的 tick 落一行 floor_low
                           # **影子行**（每窗至多一条, 供离线反事实）且链继续。
                           # ⚠️ 引擎侧 = 配置键 tail.listen_min_price（默认 0.83）——同 walk,
                           # 本脚本恒定默认值, 是 parity 的对账口径。
T150, T60 = 150, 60
FIELDS = ("yes_bid", "yes_ask", "no_bid", "no_ask")

STAGES = ("t150", "t60", "listen")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


s13 = load("s13", BASE / "13_tail_sweep.py")
s14 = load("s14", BASE / "14_tail_sweep_sigma.py")


# ── 数据提取 ──────────────────────────────────────────────────────────────

def win_ticks(e):
    """本窗**可判定 tick**（时间顺序, rem 递减）——口径见文件头 1。

    ⚠️ 只收 `rem ≤ T150` 的 tick: 更早的 tick 在引擎里连判定分支都进不去
    （`rem > T150Rem` 直接 return）, 「首个可判定 tick」指的是**首个 rem ≤ 150 的**。
    """
    out = []
    anchor = e.get("twap_open_price")
    if not anchor:
        return out
    for x in e.get("ticks") or []:
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
                    "spot": spot, "twap": (x.get("twap") or {}).get("price")})
    return out


def row(t, anchor, sd, date, outcome, stage):
    sgn = 1.0 if t["side"] == "yes" else -1.0
    dev = sgn * (t["spot"] - anchor)
    tw = t.get("twap")
    return {
        "date": date, "stage": stage, "rem": t["rem"], "side": t["side"],
        "fill": t["fill"], "buy": t["fill"],      # buy 别名: 复用 14 的 pl/day_bootstrap
        "dev": dev, "sd": sd, "sig": (dev / sd) if sd else None,
        # walk = sgn·(twap − anchor) = **已写进结算线**的那部分位移（美元）;
        # dev = basis + walk（basis = sgn·(spot−twap) 是缺口/领先量）。None = twap 缺失。
        "walk": (sgn * (tw - anchor)) if tw else None,
        "settle_won": 1 if ((outcome == 0) if t["side"] == "yes" else (outcome == 1)) else 0,
    }


def r5(r, strict_price=False):
    """⑤ = 价格腿 ∧ (dev ≥ 63 ∨ (sd ≥ 40 ∧ dev ≥ sd))。

    strict_price=True 时价格腿用**严格大于**（`hot > P_FLOOR`）——**只用于 T=150 段**
    （2026-09-26 用户决定, 见文件头）。T=60 段与 r2 保持非严格 ≥。
    """
    if not (r["fill"] > P_FLOOR if strict_price else r["fill"] >= P_FLOOR):
        return False
    if r["dev"] >= DEV_USD:
        return True
    return r["sd"] is not None and r["sd"] >= SD_MIN_USD and r["sig"] is not None and r["sig"] >= 1.0


def r2(r):
    """② = 价格腿 ∧ dev ≥ 63（监听段的规则, 不含 σ 腿）。价格腿**非严格** ≥（不变）。"""
    return r["fill"] >= P_FLOOR and r["dev"] >= DEV_USD


def walk_ok(r, x=WALK_MIN_USD):
    """T=150 段的**入场闸**（2026-09-29 决策 #29）: walk ≥ x 美元。

    ⚠️ 只在段 1 用（见 chain 的 T=150 分支）——段 2/3 一字不动。
    walk 无值（twap 缺失）⇒ **放行**（fail-open）: 数据洞不该变成「静默不下单」
    （与 Go 的 decide.WalkLeg 同口径; 14 天里这一支零命中）。
    """
    w = r.get("walk")
    return True if w is None else w >= x


def chain(ticks, anchor, sd, date, outcome):
    """三段递进判定链 → 本窗产出的行（至多 3 条: t150 判定 / t60 判定 / listen 信号）。

    判定行**成功与否都产出**（ok=False 带 reject）；信号行只在达标时产出。
    返回 (rows, rejects) —— rejects 只统计被拒的判定行原因。
    """
    if not ticks:
        return [], []
    rows, rejects = [], []
    head = ticks[0]
    if head["rem"] > T60:
        # 段 1: 首个可判定 tick 且 rem ≤ 150（价格腿**严格**大于, 2026-09-26）
        r = row(head, anchor, sd, date, outcome, "t150")
        ok5 = r5(r, strict_price=True)
        # 段 1 的入场闸（2026-09-29 决策 #29）: ⑤ 达标 ∧ walk ≥ 43 美元才算信号。
        # 闸掉 ⇒ 落一行判定行, **链继续**（T=60/监听段照常判, 不是整窗丢弃）。
        if ok5 and walk_ok(r):
            r["ok"] = True
            return [r], rejects
        rejects.append("t150:walk_low" if ok5 else "t150")
        rows.append(r)
        rest = [x for x in ticks if x["rem"] <= T60]
    else:
        # 迟到接入: 首个可判定 tick 已在 T=60 段 → 整段跳过, 不伪造 t150 行
        rest = ticks
    if rest:
        # 段 2: 首个可判定 tick 且 rem ≤ 60
        t2 = rest[0]
        r2r = row(t2, anchor, sd, date, outcome, "t60")
        if r5(r2r):
            r2r["ok"] = True
            return rows + [r2r], rejects
        rejects.append("t60")
        rows.append(r2r)
        # 段 3: 监听 —— 此后每秒判 ②, 首个达标 tick 出信号（② 未达标的 tick 不落行）。
        # 2026-10-01 决策 #32: ② 达标但 `fill ≤ LISTEN_MIN_PRICE` 的 tick 被**价格
        # 地板**拦下——落一行 floor_low **影子行**（每窗至多一条: 无地板时链在首个
        # ② 达标 tick 就结束, 之后的廉价 tick 根本不会成为信号）且**链继续**。
        shadowed = False
        for x in rest[1:]:
            rl = row(x, anchor, sd, date, outcome, "listen")
            if not r2(rl):
                continue
            if rl["fill"] <= LISTEN_MIN_PRICE:
                if not shadowed:
                    rl["reject_reason"] = "floor_low"
                    rows.append(rl)
                    shadowed = True
                continue
            rl["ok"] = True
            rows.append(rl)
            return rows, rejects
    return rows, rejects


# ── 统计输出 ──────────────────────────────────────────────────────────────

def pl(rows):
    s = 0.0
    for r in rows:
        sh = STAKE / r["fill"]
        s += (sh - STAKE) if r["settle_won"] else -STAKE
    return s


def line(lab, rows, width=30):
    if not rows:
        print("  " + lab.ljust(width) + "n=0")
        return
    n = len(rows)
    wr = sum(r["settle_won"] for r in rows) / n
    px = sum(r["fill"] for r in rows) / n
    d = collections.defaultdict(list)
    for r in rows:
        d[r["date"]].append(r)
    neg = sum(1 for v in d.values() if pl(v) < 0)
    print("  " + lab.ljust(width) + f"n={n:<5} WR {wr*100:6.2f}%  均价 {px:.4f}  "
          f"EV/注 {pl(rows)/n:+.4f}  P&L {pl(rows):+7.2f}U  亏损日 {neg}/{len(d)}")


def main():
    ev = load_events("data/btc")
    hr = s13.hist_ranges(ev)
    rng = random.Random(42)

    def ci(rows):
        if len(rows) < 5:
            return "[样本薄]"
        ds = sorted(set(r["date"] for r in rows if r["date"] != "2026-08-31"))
        (lo, hi), _ = s14.day_bootstrap(rows, ds, 2000, rng)
        return f"[{lo:+.1f}, {hi:+.1f}]"

    signals = {s: [] for s in STAGES}      # **信号行**（ok=true, 按段）
    rows_n = collections.Counter()         # 全部行（判定行 + 信号行, 按段）
    walk_low = 0                           # T=150 段被入场闸拦下的行数（决策 #29）
    floor_low = 0                          # 监听段被价格地板拦下的行数（决策 #32; 影子行）
    skipped_no_sigma = 0                   # σ 未就绪整窗跳过（与 cmd/tail 前置闸同源）
    windows = 0                            # 参与统计的窗数（有可判定 tick 的）
    audit = collections.Counter()          # 空簿/twap/门控差集审计
    no_sigma_windows = 0
    margin = float("inf")                  # 判定边界的最小余量（浮点对账的安全垫）

    def edges(r):
        """一条行距离 dev/σ 三条**连续量**边界的最小余量（|dev−63| / |sd−40| / |dev−sd|）。

        ⚠️ 不含价格腿: 报价落在 0.01 的 tick 网格上, `fill == 0.80` 是常态,
        而它是**恰好在边界上放行/拦下**——两边读同一个 double, 行为确定（T=150 段
        严格大于后, 0.80 那一格从「恰好放行」变成「恰好拦下」, 同样是确定的）。
        dev/sd 是浮点连续量, 它们的余量才是「1ulp 差异会不会让 n 差 1」的安全垫。
        """
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
        # 与 Go 侧逐操作对齐: hist_bps = h/anchor*1e4 → sd = hist_bps*anchor/1e4。
        # 数学上 sd ≡ h, 但保持同一运算序列可让两边浮点结果逐位一致
        # （差 1ulp 就可能在 dev≥sd 的边界上让 n 差 1）。
        sd = h / anchor * 1e4 * anchor / 1e4
        date = datetime.datetime.fromtimestamp(
            e["start_time"], datetime.timezone.utc).strftime("%Y-%m-%d")

        ticks = win_ticks(e)
        if not ticks:
            continue
        windows += 1
        rows, rej = chain(ticks, anchor, sd, date, outcome)
        walk_low += sum(1 for x in rej if x.endswith("walk_low"))
        floor_low += sum(1 for r in rows if r.get("reject_reason") == "floor_low")
        for r in rows:
            rows_n[r["stage"]] += 1
            margin = min(margin, edges(r))
            if r.get("ok"):
                signals[r["stage"]].append(r)

        # 审计: 空簿形态 / twap 缺失 / 「四档门 vs 有效价门」差集
        # （差集非 0 ⇒ 两套宇宙分家, parity 的三段 n 会立刻对不上）
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
                    # twap 缺的 tick 上 walk 无值 ⇒ 入场闸 fail-open（两边同口径, 不改变宇宙）
                    audit["四档齐但 twap 缺（walk 闸 fail-open, 不影响宇宙）"] += 1
                continue
            if not any(eff.values()):
                audit["整簿全空（四档齐 0 = 该窗无 PM 簿, 两套门一致判无效）"] += 1
                continue
            # 单侧/部分缺 —— Go 的有效价门会放行, python 的四档门不会 ⇒ 差集
            audit["四档部分缺（单侧空 ask 等）"] += 1
            if eff["yes"] > 0 or eff["no"] > 0:
                audit["  └ 有效价门放行而四档门拒绝（★ 宇宙分家）"] += 1

    allsig = [r for s in STAGES for r in signals[s]]

    print("\n" + "=" * 100)
    print("一、三段递进判定链（14 天, 2U/注, σ 未就绪窗跳过; 任一段出信号即整窗只下一单）")
    print("=" * 100)
    for s in STAGES:
        line(f"{s} 段信号", signals[s])
    line("合计（三段全部）", allsig)
    print("\n  日级 bootstrap 95%（判决口径）:")
    for s in STAGES:
        print(f"    {s:<8} {ci(signals[s])}")
    print(f"    {'合计':<8} {ci(allsig)}")
    print(f"\n  参考窗数: 参与判定 {windows} 窗; σ 未就绪跳过 {skipped_no_sigma} 窗")

    print("\n" + "=" * 100)
    print("二、行计数（判定行成功与否都落盘 + 信号行; parity 断言的就是这三组）")
    print("=" * 100)
    print(f"  {'段':<8}{'全部行':>8}{'信号行':>8}{'被拒判定行':>12}")
    for s in STAGES:
        print(f"  {s:<8}{rows_n[s]:>8}{len(signals[s]):>8}{rows_n[s] - len(signals[s]):>12}")
    print(f"  {'合计':<8}{sum(rows_n.values()):>8}{len(allsig):>8}"
          f"{sum(rows_n.values()) - len(allsig):>12}")
    print("  注: t150 判定行 = 「首个可判定 tick 且 rem ≤ 150」的窗数（迟到接入的窗没有它）;")
    print("      t60 判定行 = 走到第二段的窗数（迟到接入的窗只有 t60 一行）;")
    print("      listen 段只在 ② 达标时落行 + 每窗至多一条 floor_low 影子行（决策 #32）, "
          "故它没有被拒判定行——上面 listen 的「被拒」列数出来就是影子行。")
    print(f"  T=150 入场闸拦下（reject = walk_low, 决策 #29）= {walk_low} 行"
          f"（= ⑤ 达标但 walk < {WALK_MIN_USD:g} 美元的那批; 链继续 ⇒ 其中多数改道到 t60/listen）")
    print(f"  监听段价格地板拦下（reject = floor_low, 决策 #32）= {floor_low} 行"
          f"（= ② 达标但有效价 ≤ {LISTEN_MIN_PRICE:g} 的那批, 每窗至多一条影子行; 链继续）")
    print(f"  判定边界的最小余量（dev/σ 三条连续量腿的浮点安全垫, 应远大于 1e-9）: {margin:.3e}")
    print("      （价格腿不在内: 报价在 0.01 网格上, `fill == 0.80` 恰好边界放行是常态形态）")

    print("\n" + "=" * 100)
    print("三、逐日明细（信号按段分桶; P&L 为当日合计）")
    print("=" * 100)
    days = sorted(set(r["date"] for r in allsig))
    print(f"  {'日期':<12}{'t150':>7}{'t60':>7}{'listen':>8}{'合计':>7}"
          f"{'WR':>8}{'P&L':>10}")
    for d in days:
        per = {s: [r for r in signals[s] if r["date"] == d] for s in STAGES}
        day = [r for r in allsig if r["date"] == d]
        wr = sum(r["settle_won"] for r in day) / len(day) * 100
        print(f"  {d:<12}{len(per['t150']):>7}{len(per['t60']):>7}{len(per['listen']):>8}"
              f"{len(day):>7}{wr:>7.1f}%{pl(day):>+10.2f}")
    print(f"  {'合计':<12}{len(signals['t150']):>7}{len(signals['t60']):>7}"
          f"{len(signals['listen']):>8}{len(allsig):>7}"
          f"{sum(r['settle_won'] for r in allsig)/len(allsig)*100:>7.1f}%{pl(allsig):>+10.2f}")

    print("\n" + "=" * 100)
    print("四、与旧口径对照（旧 = 只在 rem ≤ 60 判一次 ⑤, 即三段的特例）")
    print("=" * 100)
    old = [r for r in signals["t60"]]                 # 近似: 第二段信号
    line("旧口径近似（t60 段信号）", old)
    print("  ⚠️ 精确的旧口径见 13_tail_sweep.py（n=1536 WR 99.61% +36.93U）——两者差异 =")
    print("     旧口径在 σ 未就绪窗上也判（本脚本跳过）, 且它的宇宙与判定点口径有自己的历史。")

    print("\n" + "=" * 100)
    print("六、parity 断言表（internal/tail/parity_test.go 逐位照抄的 oracle）")
    print("=" * 100)
    print("  // 行数 = 该段全部行（判定行 + 信号行）; 信号数 / WR / P&L 只算 ok 行")
    print(f"  {'段':<8}{'行数':>8}{'信号':>8}{'WR(%)':>12}{'P&L(U)':>14}")
    for s in STAGES:
        sig = signals[s]
        wr = (sum(r["settle_won"] for r in sig) / len(sig) * 100) if sig else 0.0
        print(f"  {s:<8}{rows_n[s]:>8}{len(sig):>8}{wr:>12.6f}{pl(sig):>14.6f}")
    tot_wr = sum(r["settle_won"] for r in allsig) / len(allsig) * 100
    print(f"  {'合计':<8}{sum(rows_n.values()):>8}{len(allsig):>8}{tot_wr:>12.6f}{pl(allsig):>14.6f}")
    print(f"  // 参与判定的窗数 = {windows}（= 有 ≥1 个 rem ≤ 150 可判定 tick 且有 σ 的窗）")
    print(f"  // σ 未就绪整窗跳过 = {skipped_no_sigma} 窗（引擎前置闸同源, 决策 #13）")
    print(f"  // T=150 入场闸拦下 = {walk_low} 行（reject_reason = walk_low, 决策 #29）")
    print(f"  // 监听段价格地板拦下 = {floor_low} 行（reject_reason = floor_low, 决策 #32; 影子行）")
    print("  // 兜底零命中: 全部行 HotSrc 恒 ask（14 天里没有一次单侧空簿, 见第五节）")

    print("\n" + "=" * 100)
    print("五、审计：空簿 / twap 缺失 / 门控差集（parity 宇宙是否与四档门同源）")
    print("=" * 100)
    if not audit:
        print("  零命中（rem ≤ 150 的 tick 全部四档齐备、twap 在场）——两套宇宙同源。")
    else:
        for k, v in audit.most_common():
            print(f"  {k:<30} {v}")
    print(f"\n  σ 未就绪跳过的窗: {no_sigma_windows}")


if __name__ == "__main__":
    main()
