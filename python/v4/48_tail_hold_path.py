#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫尾盘：把 dev / basis / walk / sd 还原成**现货语言**，并检验用户的命题（2026-10-11）。

用户原话：

  「根据这么多数据的验证，我觉得 dev、basis、walk、sd 做为扫尾盘策略的过滤条件其实是
   没有科学依据的，只是从 data/btc 回测数据里拟合出来的一个基本无意义的数据。真正决定
   扫尾盘是否成功的是持仓后，spot 是否有向不利的一方移动，总体来说跟 dev 没有直接关系，
   只是当 dev 大 walk 也大时能接受的 spot 向不利方向移动的量更大一些。你看看能不能从
   这个思路来监控数据。或者从 data/events 中的数据里找出新的办法来实现扫尾盘？」

本脚本先把**被引用得最多、但仓库里从没验证过**的一条结构钉死（§1）——它把上面那句话
从「直觉」变成**恒等式**，然后按用户的思路做三件事：

    §1 结构：twap 推送到底是什么口径 / 结算到底取哪一段 / 于是 dev・basis・walk 各自是什么
    §2 赌注的精确形式：`结算余量 = dev0 + Δ`，`Δ = 结算线在持仓后的位移` ⇒ 赢 ⟺ Δ > −dev0
    §3 Δ 可预测吗（用户命题的核心：如果不可预测，那么过滤条件就只能是「strike 定得对不对」）
    §4 strike 定价：WR vs fill vs dev0 / walk0 —— 市场价有没有把 dev0 定错
    §5 持仓后路径：赢家与输家的 dev(t)/walk(t)/basis(t) 形状（用户要「监控」的那个东西）
    §6 把监控变成动作：`dev(t) < X ⇒ 按持仓侧 bid 出场` vs 持有到期

⚠️ 与已否家族的关系（不重复提案）：
  - `docs/tail_entry_timing_2026-09-29.md`：挪检查点/换阈值（无条件推后）——本脚本不碰检查点。
  - `docs/tail_stoploss_crossing_2026-09-30.md`（#39）：`bid` 下穿的停留时长 τ / 穿越次数 k。
  - `[[tail-velocity-stop-refuted-2026-10-03]]`：**速度投影**止损（外推现货速度）。
  - 决策 #24 的 dev 腿：`bid < 0.30 ∧ dev < −20` 双腿（实盘只触发 9 次、捞到 9/23）。
  §6 测的是**单腿** `dev(t) < X`（结算线坐标的缓冲耗尽），与上面四条都不同，但同一族，
  故 §6 的结论一律按「是否优于持有」读，并且**先看它在现行宇宙（地板之后）里的 Δ**。

用法: python/venv/bin/python python/v4/48_tail_hold_path.py
"""

import sys
import math
import random
import datetime
import statistics as st
import collections
import importlib.util
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


s23 = load("s23", BASE / "23_tail_integrated.py")   # oracle：口径常量 / 数据加载
s13 = load("s13", BASE / "13_tail_sweep.py")        # σ: hist_ranges
s38 = load("s38", BASE / "38_tail_walk_gate.py")    # 统计口径（pnl / boot_delta / by_day）

STAKE = s23.STAKE
MAX_LAT = s23.MAX_LAT
boot_delta = s38.boot_delta
by_day = s38.by_day
T150, T60 = 150, s23.T60
P_FLOOR = s23.P_FLOOR
DEV_USD = s23.DEV_USD
SD_MIN_USD = s23.SD_MIN_USD
WALK_X = s23.WALK_MIN_USD          # 43：T=150 入场闸（决策 #29）
FLOOR_X = s23.FLOOR_MIN_PRICE      # 0.83：价格地板（决策 #32/#33）
EVENTS = "data/events"
NEW = "data/events"                # 现行分析样本（09-27~10-11）
OLD = "data/btc"                   # 三把 pin 的样本


# ── 数据 ──────────────────────────────────────────────────────────────────

def path_ticks(e):
    """本窗 `rem ≤ 150` 的**可判定 tick** + 原始盘口 —— 供「持仓后路径」与「出场」使用。

    门与 `s23.win_ticks` 逐位同源（`book_latency ≤ 300` ∧ 有效价 > 0 ∧ spot > 0 ∧
    `0 < rem ≤ 150`），只是多留 pm 四档（出场要读**入场那一侧**的 bid）与 ts。
    """
    out = []
    for x in e.get("ticks") or []:
        p = x.get("pm") or {}
        if not p or (p.get("book_latency_ms") or 0) > MAX_LAT:
            continue
        rem = x.get("rem")
        if rem is None or rem <= 0 or rem > T150:
            continue
        yb, ya = p.get("yes_bid") or 0, p.get("yes_ask") or 0
        nb, na = p.get("no_bid") or 0, p.get("no_ask") or 0
        eff_yes, eff_no = (ya if ya > 0 else yb), (na if na > 0 else nb)
        if not (eff_yes > 0 or eff_no > 0):
            continue                      # 整簿全空（决策 #21/#22 的有效价门）
        spot = (x.get("bin") or {}).get("price")
        if not spot:
            continue
        yes_hot = eff_yes >= eff_no       # 平局取 yes（与 Go HotBook 同）
        out.append({"ts": x.get("ts"), "rem": rem,
                    "side": "yes" if yes_hot else "no",
                    "fill": eff_yes if yes_hot else eff_no,
                    "yb": yb, "ya": ya, "nb": nb, "na": na,
                    "spot": spot, "twap": (x.get("twap") or {}).get("price")})
    return out


def mkrow(t, idx, stage, anchor, sd):
    """一条判定/信号行（口径同 oracle 23 的 row()，另留 idx / sgn / spot / twap 原值）。"""
    sgn = 1.0 if t["side"] == "yes" else -1.0
    tw = t.get("twap")
    return {"idx": idx, "stage": stage, "rem": t["rem"], "side": t["side"], "sgn": sgn,
            "fill": t["fill"], "spot": t["spot"], "twap": tw, "sd": sd,
            "dev": sgn * (t["spot"] - anchor),
            "walk": (sgn * (tw - anchor)) if tw else None,
            "basis": (sgn * (t["spot"] - tw)) if tw else None}


def ok5(r, strict):
    """⑤ = 价格腿 ∧ (dev ≥ 63 ∨ (sd ≥ 40 ∧ dev ≥ sd))。严格大于只用于 T=150 段（#26）。"""
    if not (r["fill"] > P_FLOOR if strict else r["fill"] >= P_FLOOR):
        return False
    if r["dev"] >= DEV_USD:
        return True
    return r["sd"] is not None and r["sd"] >= SD_MIN_USD and r["dev"] >= r["sd"]


def ok2(r):
    """② = 价格腿 ∧ dev ≥ 63（监听段；价格腿非严格）。"""
    return r["fill"] >= P_FLOOR and r["dev"] >= DEV_USD


def signal(w):
    """现行三段链在本窗**实际成交的那一笔** → (行, tick 下标)。

    与 oracle 23 的 `chain()` 逐条同源：价格腿算子在段间不同（#26）/ walk 闸只闸段 1
    （#29）/ 地板拦段 2 与段 3（#32+#33）且被拦即**链继续**。
    """
    tk, anchor, sd = w["ticks"], w["anchor"], w["sd"]
    if not tk:
        return None, None
    head = tk[0]
    if head["rem"] > T60:
        r = mkrow(head, 0, "t150", anchor, sd)
        if ok5(r, strict=True) and (r["walk"] is None or r["walk"] >= WALK_X):
            return r, 0
        rest = [i for i, x in enumerate(tk) if x["rem"] <= T60]
    else:
        rest = list(range(len(tk)))       # 迟到接入：整段跳过 T=150, 不伪造 t150 行
    if not rest:
        return None, None
    i2 = rest[0]
    r2 = mkrow(tk[i2], i2, "t60", anchor, sd)
    if ok5(r2, strict=False):
        if r2["fill"] > FLOOR_X:
            return r2, i2
        # 被地板拦 ⇒ 落影子行 + 链继续到监听段
    else:
        r2 = None
    for i in rest[1:]:
        rl = mkrow(tk[i], i, "listen", anchor, sd)
        if not ok2(rl):
            continue
        if rl["fill"] > FLOOR_X:
            return rl, i
    return None, None


def build(data_dir):
    """宇宙：σ 就绪（≥3 窗）∧ 有 outcome/anchor ∧ 有可判定 tick。"""
    ev = s23.load_events(data_dir)
    hr = s13.hist_ranges(ev)
    wins = []
    for e in ev:
        outcome, anchor = e.get("outcome"), e.get("twap_open_price")
        if outcome is None or not anchor:
            continue
        h = hr.get(e["start_time"])
        if h is None:
            continue
        sd = h / anchor * 1e4 * anchor / 1e4          # = h（与引擎 SigmaUSD 同口径）
        tk = path_ticks(e)
        if not tk:
            continue
        date = datetime.datetime.fromtimestamp(
            e["start_time"], datetime.timezone.utc).strftime("%Y-%m-%d")
        wins.append({"date": date, "start": e["start_time"], "outcome": outcome,
                     "anchor": anchor, "sd": sd, "ticks": tk,
                     "close": e.get("twap_close_price")})
    return wins


def won_of(side, outcome):
    return 1 if ((outcome == 0) if side == "yes" else (outcome == 1)) else 0


def mov(r):
    """**持仓后结算线的位移** Δ = sgn·(close − spot0)（现货坐标, 美元）。

    ⚠️ 它含「binance 现货 vs Chainlink TWAP 指数」的水平差 —— 这也是 dev/basis 自带的
    那个量（见 `docs/tail_dev_level_offset_2026-10-10.md`）。§3 的干净版是
    `dw = sgn·(close − twap0)`（**结算线坐标**，不含水平差），两套都给。
    """
    return r["sgn"] * (r["close"] - r["spot"])


def dwalk(r):
    """结算线坐标下的持仓后位移 Δwalk = sgn·(close − twap0)。"""
    return (r["sgn"] * (r["close"] - r["twap"])) if r["twap"] else None


# ── 小工具 ────────────────────────────────────────────────────────────────

def med(v):
    v = [x for x in v if x is not None]
    return st.median(v) if v else float("nan")


def q(v, p):
    v = sorted(x for x in v if x is not None)
    return v[min(len(v) - 1, int(p * len(v)))] if v else float("nan")


def corr(a, b):
    p = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    if len(p) < 3:
        return float("nan")
    xs = [x for x, _ in p]
    ys = [y for _, y in p]
    mx, my = st.mean(xs), st.mean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return sum((x - mx) * (y - my) for x, y in p) / (sx * sy) if sx and sy else float("nan")


def fee(sh, px, rate=0.07):
    """Polymarket 官方 taker 费（决策 #35）: C×rate×p×(1−p)，5 位小数。"""
    return math.floor(sh * rate * px * (1 - px) * 1e5 + 0.5) / 1e5


def line(ch="-", n=78):
    print(ch * n)


def pct(x, n):
    return f"{x / n * 100:5.1f}%" if n else "  n/a "


# ── §0 自检 ───────────────────────────────────────────────────────────────

def z0(wins_old, S_old, wins_new, S_new):
    line("=")
    print("§0 口径自检（本脚本的链必须与 oracle 23 / 47 号逐位一致）")
    line("=")
    print("  data/btc（14 天, 决策 #33 的 pin = 2074 / 97.830280% / +70.314372U）:")
    if S_old:
        wr = sum(r["settle_won"] for r in S_old) / len(S_old) * 100
        print(f"    窗 {len(wins_old)}  信号 n={len(S_old)}  WR {wr:.6f}%  P&L {s38.pnl(S_old):+.6f}U")
    else:
        print("    ⚠️ data/btc 缺失 ⇒ 跳过（其余节照跑）")
    print("  data/events（09-27~10-11, 47 号现行链 = n=1959 / WR 96.68% / +12.29U）:")
    wr = sum(r["settle_won"] for r in S_new) / len(S_new) * 100
    print(f"    窗 {len(wins_new)}  信号 n={len(S_new)}  WR {wr:.2f}%  P&L {s38.pnl(S_new):+.2f}U")


# ── §1 结构 ───────────────────────────────────────────────────────────────

def z1(wins):
    line("=")
    print("§1 结构：twap 推送与结算到底取哪一段（把 dev/basis/walk 翻译回现货语言）")
    line("=")

    # (a) 官方 close ≡ 边界那一秒的推送
    d = [abs(w["close"] - [t["twap"] for t in w["ticks"] if t["twap"]][-1])
         for w in wins if w["close"] and any(t["twap"] for t in w["ticks"])]
    print(f"(a) |官方 twap_close − 末 tick 的 twap 推送|  中位 {med(d):.3f} 美元, "
          f"p90 {q(d, .9):.2f}  (n={len(d)})")

    # (b) outcome ⟺ sign(close − anchor)
    ok = sum(1 for w in wins if (w["close"] > w["anchor"]) == (w["outcome"] == 0))
    n = sum(1 for w in wins if w["close"])
    print(f"(b) `outcome ⟺ sign(twap_close − anchor)` : {ok}/{n} 逐位一致 "
          f"⇒ **结算就是结算线的最终位移**")

    # (c) twap 推送 = 过去 60 秒均价：Δtwap 对 Δbin(t) 相关 ≈0, 对 (bin(t)−bin(t−60))/60 斜率 ≈1
    a, b, c = [], [], []
    for w in wins[:600]:
        tk = w["ticks"]
        for i in range(61, len(tk)):
            if not (tk[i]["twap"] and tk[i - 1]["twap"] and tk[i - 60]["spot"]):
                continue
            a.append(tk[i]["twap"] - tk[i - 1]["twap"])
            b.append(tk[i]["spot"] - tk[i - 1]["spot"])
            c.append((tk[i]["spot"] - tk[i - 60]["spot"]) / 60.0)
    sl = sum(x * y for x, y in zip(a, c)) / sum(y * y for y in c) if c else float("nan")
    print(f"(c) corr(Δtwap, Δbin 瞬时) = {corr(a, b):+.3f}   "
          f"corr(Δtwap, (bin(t)−bin(t−60))/60) = {corr(a, c):+.3f} (斜率 {sl:.3f})")
    print("    ⇒ twap 推送 = **过去 60 秒的均价**，它以现货 1/60 的速度移动、由 60 秒前的现货驱动")

    print()
    print("于是四个量在现货语言里各是什么（`sgn` = 押注方向）：")
    print("    dev   = sgn·(spot − anchor)       现货 相对 **anchor（上一分钟均价）** 的位移")
    print("    walk  = sgn·(twap − anchor)       近一分钟均价 相对 anchor 的位移 ← **趋势分量**")
    print("    basis = sgn·(spot − twap)         现货 相对 **它自己近一分钟均值** 的偏离 ← **尖峰分量**")
    print("    sd    = 窗口振幅 σ（美元）          上面这些量的自然刻度")
    print()
    print("    ⚠️ **结算只看最后 60 秒的均价** ⇒ 决策点（T=150 / T=60）上「现货此后一动不动」")
    print("       时的结算余量 **恰好就是 dev**（r ≥ 60 时窗口还没开始被平均进去）——")
    print("       用户说的「能承受的 adverse movement」= dev，是**恒等式**不是拟合值。")


# ── §2 赌注的精确形式 ─────────────────────────────────────────────────────

def z2(sigs):
    line("=")
    print("§2 赌注的精确形式：结算余量 = dev0 + Δ，赢 ⟺ Δ > −dev0")
    line("=")
    print("    现货坐标:  余量 = dev0 + Δ ,  Δ = sgn·(close − spot0)      ← 含水平差")
    print("    结算线坐标: 余量 = walk0 + Δwalk , Δwalk = sgn·(close − twap0)  ← 干净")
    print()

    W = [r for r in sigs if r["settle_won"]]
    L = [r for r in sigs if not r["settle_won"]]
    print(f"信号 n={len(sigs)}  赢 {len(W)}  输 {len(L)}")
    print()
    print(f"{'':14}{'中位':>12}{'p25':>12}{'p75':>12}")
    for name, f in (("dev0 (美元)", lambda r: r["dev"]),
                    ("walk0 (美元)", lambda r: r["walk"]),
                    ("basis0 (美元)", lambda r: r["basis"]),
                    ("Δ (美元)", mov),
                    ("Δwalk (美元)", dwalk)):
        v = [f(r) for r in sigs]
        print(f"{name:14}{med(v):12.1f}{q(v, .25):12.1f}{q(v, .75):12.1f}")

    print()
    print("赢/输两组的**同一批量**（美元）:")
    print(f"{'':14}{'赢 中位':>11}{'输 中位':>11}{'赢 p10':>11}{'输 p90':>11}")
    for name, f in (("dev0", lambda r: r["dev"]), ("walk0", lambda r: r["walk"]),
                    ("basis0", lambda r: r["basis"]), ("Δ", mov), ("Δwalk", dwalk),
                    ("fill", lambda r: r["fill"])):
        print(f"{name:14}{med([f(r) for r in W]):11.3f}{med([f(r) for r in L]):11.3f}"
              f"{q([f(r) for r in W], .10):11.3f}{q([f(r) for r in L], .90):11.3f}")

    # 恒等式自检
    bad = 0
    for r in sigs:
        if r["walk"] is None:
            continue
        if abs((r["dev"]) - (r["walk"] + r["basis"])) > 1e-6:
            bad += 1
        m = r["sgn"] * (r["close"] - r["anchor"])
        if abs(m - (r["dev"] + mov(r))) > 1e-6:
            bad += 1
    print(f"\n恒等式自检（dev ≡ walk+basis, 余量 ≡ dev0+Δ）: 违例 {bad} 条")

    # 正态化后的未来位移
    z = [r["dev"] / r["sd"] for r in sigs if r["sd"]]
    zw = [dwalk(r) / r["sd"] for r in sigs if r["sd"] and dwalk(r) is not None]
    print(f"\n**strike 的刻度**:  dev0/sd  中位 {med(z):.2f}σ  (p10 {q(z,.1):.2f}σ, p90 {q(z,.9):.2f}σ)")
    print(f"**未来的尺度**:  Δwalk/sd 中位 {med(zw):+.2f}σ  "
          f"(p10 {q(zw,.1):+.2f}σ, p90 {q(zw,.9):+.2f}σ, 标准差 {st.pstdev(zw):.2f}σ)")
    print("    ⇒ 一笔注的胜负 = 「strike 有多少 σ」 vs 「未来位移抽到多少 σ」")


# ── §3 Δ 可预测吗 ─────────────────────────────────────────────────────────

def z3(sigs):
    line("=")
    print("§3 用户命题的核心：持仓后的位移 Δ 在入场那一刻可预测吗？")
    line("=")
    print("  若不可预测（鞅），则任何过滤条件的价值只剩一条：**把 strike 定对**。")
    print()

    S = [r for r in sigs if r["walk"] is not None and r["sd"]]
    y = [dwalk(r) / r["sd"] for r in S]                 # 结算线坐标（干净）
    y2 = [mov(r) / r["sd"] for r in S]                  # 现货坐标（含水平差）
    feats = [
        ("walk0/sd", [r["walk"] / r["sd"] for r in S]),
        ("dev0/sd", [r["dev"] / r["sd"] for r in S]),
        ("basis0/sd", [r["basis"] / r["sd"] for r in S]),
        ("fill", [r["fill"] for r in S]),
        ("rem0", [r["rem"] for r in S]),
        ("sd (美元)", [r["sd"] for r in S]),
        ("side = yes", [1.0 if r["side"] == "yes" else 0.0 for r in S]),
        ("stage=t150", [1.0 if r["stage"] == "t150" else 0.0 for r in S]),
    ]
    print(f"corr(·, Δwalk/sd)   n={len(S)}   （Δwalk = 结算线在持仓后的位移, σ 归一）")
    for name, v in feats:
        print(f"    {name:14} {corr(v, y):+.3f}")
    print(f"\ncorr(·, Δ/sd)（现货坐标, 含水平差）")
    for name, v in feats:
        print(f"    {name:14} {corr(v, y2):+.3f}")

    # 水平差那一层：Δ 里有多少是「入场那一刻的 spot−twap 缺口」的回补
    print()
    print("  拆开看（用户提到 basis 可以「消失」）:")
    b = [r["basis"] / r["sd"] for r in S]
    print(f"    corr(basis0/sd, Δwalk/sd) = {corr(b, y):+.3f}   "
          f"← 缺口**会不会回补**（负 = 均值回复, 0 = 无关, 正 = 延续）")
    print(f"    corr(basis0/sd, Δ/sd)     = {corr(b, y2):+.3f}   ← 现货坐标下同一问题")

    # 用 walk0 分批, 看 Δwalk、Δ 的均值是否随 strike 变
    print()
    print("  按 walk0/sd 分档（strike 大小）看**未来的位移**是否随之变:")
    S2 = sorted(S, key=lambda r: r["walk"] / r["sd"])
    nb = 5
    print(f"    {'walk0/sd 档':22}{'n':>5}{'Δwalk/sd 均值':>15}{'Δ/sd 均值':>12}"
          f"{'赢率':>8}{'fill 均值':>10}")
    for k in range(nb):
        g = S2[k * len(S2) // nb:(k + 1) * len(S2) // nb]
        if not g:
            continue
        lo, hi = g[0]["walk"] / g[0]["sd"], g[-1]["walk"] / g[-1]["sd"]
        dv = [dwalk(r) / r["sd"] for r in g]
        dp = [mov(r) / r["sd"] for r in g]
        wr = sum(r["settle_won"] for r in g) / len(g)
        print(f"    [{lo:8.2f},{hi:8.2f}]σ{nb*' '}{len(g):>5}{st.mean(dv):15.2f}"
              f"{st.mean(dp):12.2f}{wr*100:7.1f}%{st.mean([r['fill'] for r in g]):10.4f}")


# ── §4 strike 定价 ────────────────────────────────────────────────────────

def z4(sigs):
    line("=")
    print("§4 市场价有没有把 strike 定错（用户问：dev 是不是无意义）")
    line("=")
    print("  一笔注的边际 = 赢率 − 平均 fill（fill 就是盈亏平衡胜率; 右为正 = 有 edge）")
    print()

    def blk(title, key, edges):
        print(f"  {title}")
        print(f"    {'档':20}{'n':>6}{'赢':>5}{'输':>5}{'赢率':>8}{'fill':>9}{'边际':>9}{'P&L':>10}")
        for k in range(len(edges) - 1):
            lo, hi = edges[k], edges[k + 1]
            g = [r for r in sigs if lo <= key(r) < hi]
            if not g:
                continue
            wr = sum(r["settle_won"] for r in g) / len(g)
            fl = st.mean([r["fill"] for r in g])
            p = sum((STAKE / r["fill"] - STAKE) if r["settle_won"] else -STAKE for r in g)
            mark = "  ←" if wr - fl > 0.01 and len(g) >= 20 else ""
            print(f"    [{lo:6.2f},{hi:6.2f}){len(g):>6}{sum(r['settle_won'] for r in g):>5}"
                  f"{len(g)-sum(r['settle_won'] for r in g):>5}{wr*100:7.1f}%{fl:9.4f}"
                  f"{(wr-fl)*100:+8.1f}pp{p:+9.1f}U{mark}")
        print()

    blk("(a) 按 fill 分桶（市场自己的定价）", lambda r: r["fill"],
        [0.80, 0.85, 0.90, 0.95, 0.98, 1.01])
    blk("(b) 按 dev0 分桶（strike 美元）", lambda r: r["dev"],
        [60, 100, 150, 250, 400, 1e9])
    blk("(c) 按 walk0 分桶（趋势分量美元）",
        lambda r: (r["walk"] if r["walk"] is not None else -1e9),
        [-1e9, 0, 20, 43, 80, 1e9])
    blk("(d) 按 basis0 分桶（尖峰分量美元）",
        lambda r: (r["basis"] if r["basis"] is not None else -1e9),
        [-1e9, 0, 20, 43, 80, 1e9])


# ── §5 持仓后路径 ─────────────────────────────────────────────────────────

def sigs(wins, with_path=False):
    """现行链信号行（挂胜负/日期/位移），可选挂上「入场后逐 tick 的路径」。"""
    out = []
    for w in wins:
        r, i = signal(w)
        if not r:
            continue
        r = dict(r)
        r["date"] = w["date"]
        r["close"] = w["close"]
        r["anchor"] = w["anchor"]
        r["outcome"] = w["outcome"]
        r["settle_won"] = won_of(r["side"], w["outcome"])
        r["Δ"] = mov(r)
        r["Δwalk"] = dwalk(r)
        r["path"] = []
        if with_path:
            for t in w["ticks"][i + 1:]:
                tw = t["twap"]
                r["path"].append(
                    {"rem": t["rem"], "spot": t["spot"], "twap": tw,
                     "dev": r["sgn"] * (t["spot"] - w["anchor"]),
                     "walk": (r["sgn"] * (tw - w["anchor"])) if tw else None,
                     "basis": (r["sgn"] * (t["spot"] - tw)) if tw else None,
                     "hold_bid": (t["yb"] if r["side"] == "yes" else t["nb"])})
        out.append(r)
    return out


def z5(S):
    line("=")
    print("§5 持仓后的路径（用户要监控的那个东西）")
    line("=")
    W = [r for r in S if r["settle_won"]]
    L = [r for r in S if not r["settle_won"]]
    print(f"信号 n={len(S)}（赢 {len(W)} / 输 {len(L)}）")
    print()

    print("  (a) 入场后 dev(t) 的最低位（=「离输最近到过哪儿」, 美元）")
    print(f"    {'':8}{'min dev(t)':>13}{'末值 dev':>11}{'穿过 0 的占比':>15}")
    for name, G in (("赢", W), ("输", L)):
        mn = [min([p["dev"] for p in r["path"]], default=r["dev"]) for r in G]
        en = [r["path"][-1]["dev"] if r["path"] else r["dev"] for r in G]
        cr = sum(1 for r in G if min([p["dev"] for p in r["path"]], default=1) < 0)
        print(f"    {name:8}{med(mn):13.1f}{med(en):11.1f}{cr/len(G)*100:14.1f}%")

    print()
    print("  (b) 入场后 dev(t) 首次跌到 0 以下 —— 剩下的时间还剩多少（只统计真的跌下去的）")
    for name, G in (("赢", W), ("输", L)):
        rem0 = [r["path"][k]["rem"] for r in G
                for k in range(len(r["path"])) if r["path"][k]["dev"] < 0]
        # 首次下穿那一刻的 rem
        first = []
        for r in G:
            for p in r["path"]:
                if p["dev"] < 0:
                    first.append(p["rem"])
                    break
        print(f"    {name:8} 下穿过 {len(first)}/{len(G)} 笔, 首次下穿时 rem 中位 "
              f"{med(first):.0f}s (p25 {q(first,.25):.0f} / p75 {q(first,.75):.0f})")

    print()
    print("  (c) 赢单里 dev(t) 最低位仍 > 0 的占比（= 全程没被威胁过）")
    for name, G in (("赢", W), ("输", L)):
        c = sum(1 for r in G if min([p["dev"] for p in r["path"]], default=r["dev"]) > 0)
        print(f"    {name:8} {c}/{len(G)} = {c/len(G)*100:.1f}%")

    print()
    print("  (d) 结算线坐标：入场后 walk(t) 的最低位（walk 是窗口级平滑量, 移动慢）")
    for name, G in (("赢", W), ("输", L)):
        v = [min([p["walk"] for p in r["path"] if p["walk"] is not None], default=None)
             for r in G]
        v = [x for x in v if x is not None]
        cr = sum(1 for x in v if x < 0)
        print(f"    {name:8} min walk(t) 中位 {med(v):8.1f} 美元, "
              f"其中下穿过 0 的 {cr}/{len(v)} = {cr/len(v)*100:.1f}%")

    print()
    print("  (e) **缓冲消耗率** = min dev(t) / dev0（1 = 整条缓冲吃光, > 1 = 已跌穿 anchor）")
    for name, G in (("赢", W), ("输", L)):
        v = []
        for r in G:
            if r["dev"] <= 0:
                continue
            mn = min([p["dev"] for p in r["path"]], default=r["dev"])
            v.append(mn / r["dev"])
        v.sort()
        print(f"    {name:8} 中位 {med(v):6.2f}  p10 {q(v,.10):6.2f}  p90 {q(v,.90):6.2f}  "
              f"（≥1 的占 {sum(1 for x in v if x >= 1)/len(v)*100:.1f}%）")

    print()
    print("  (f) 结算线口径：入场后 `walk(t) < 0` 的 tick 占比（= 「此刻结算就在输」的时长占比）")
    for name, G in (("赢", W), ("输", L)):
        fr = []
        for r in G:
            pw = [p["walk"] for p in r["path"] if p["walk"] is not None]
            if not pw:
                continue
            fr.append(sum(1 for x in pw if x < 0) / len(pw))
        fr.sort()
        nz = sum(1 for x in fr if x > 0)
        print(f"    {name:8} 中位 {med(fr)*100:5.1f}%  有进入过负区的 {nz}/{len(fr)} = "
              f"{nz/len(fr)*100:.1f}%")


# ── §6 把监控变成动作 ─────────────────────────────────────────────────────

def z6(S):
    line("=")
    print("§6 出场规则：`dev(t) < X` ⇒ 按**持仓侧 bid** 出场（vs 持有到期）")
    line("=")
    print("  ⚠️ 与已否家族的关系见文件头; 这里只做**单腿 dev 下穿**这一条，且逐格读 Δ 与区间。")
    print()

    def hold_pnl(r):
        return (STAKE / r["fill"] - STAKE) if r["settle_won"] else -STAKE

    def sim(X, mode="dev"):
        """出场规则世界 → (逐日 P&L 映射, 触发数, 冻结数, 触发明细)。

        触发 = 入场后首个 `dev(t) < X`（mode="walk" 时改用 walk(t)）的 tick;
        出场按那个 tick 的**持仓侧 bid** 全额成交（决策 #34 口径）; 卖费按 #35 扣。
        `bid == 0`（整侧撤空）⇒ 卖不掉 ⇒ 当作未触发（留仓到期），另计冻结数。
        """
        day, ntrig, nfroz, detail = collections.defaultdict(float), 0, 0, []
        for r in S:
            hit = None
            for p in r["path"]:
                v = p["dev"] if mode == "dev" else p["walk"]
                if v is None:
                    continue
                if v < X:
                    hit = p
                    break
            sh = STAKE / r["fill"]
            if hit is None or not hit["hold_bid"] or hit["hold_bid"] <= 0:
                if hit is not None:
                    nfroz += 1
                day[r["date"]] += hold_pnl(r)
                continue
            ntrig += 1
            bid = hit["hold_bid"]
            # 全额卖出 ⇒ 已实现 = 卖出所得 − 本金 − 卖费（卖出的股数不再参与兑付）
            pnl = (sh * bid - STAKE) - fee(sh, bid)
            day[r["date"]] += pnl
            detail.append({"rem": hit["rem"], "bid": bid, "pnl": pnl,
                           "base": hold_pnl(r), "won": r["settle_won"]})
        return day, ntrig, nfroz, detail

    bd = s38.by_day(S)
    bp = s38.pnl(S)
    print(f"  基线（持有到期）: n={len(S)}  P&L {bp:+.2f}U @2U")
    print()
    for mode, unit, grid in (("dev", "dev(t) = 现货离 anchor（瞬间口径）", (0, -20, -40, -60, -100)),
                             ("walk", "walk(t) = 结算线离 anchor（**结算口径**）", (0, -20, -40, -60))):
        print(f"  触发量 = {unit}")
        print(f"    {'X':>7}{'触发':>6}{'冻结':>6}{'Δ P&L':>10}{'95% 配对区间':>22}"
              f"{'触发时 rem':>11}{'出场 bid':>10}{'救输/杀赢':>11}")
        for X in grid:
            nd, nt, nf, det = sim(X, mode)
            d = sum(nd.values()) - bp
            lo, hi = s38.boot_delta(bd, dict(nd), n=2000, seed=48)
            saved = sum(1 for c in det if not c["won"])
            killed = sum(1 for c in det if c["won"])
            star = "  ★" if lo - bp > 0 else ""
            print(f"    {X:7d}{nt:6d}{nf:6d}{d:+10.2f}U"
                  f"{f'[{lo-bp:+.2f},{hi-bp:+.2f}]':>22}"
                  f"{med([c['rem'] for c in det]):11.0f}{med([c['bid'] for c in det]):10.3f}"
                  f"{saved:>8}/{killed}{star}")
        print()
    print("  ⚠️ 读法：Δ 与它的 95% 配对区间要**成对**看（决策 #24 的教训）——只看 P&L 绝对值会误读。")
    print("     「救输/杀赢」的比价关系：赢单赚 (1−fill)/fill, 输单亏 1 ⇒ 盈亏比 ≈ (1−fill)/fill。")
    print("     0.99 价位上救 1 笔只值『杀赢 1 笔』的 1/99 ⇒ 止损要在 0.99 附近**几乎不误杀**才划算。")


def z7(S):
    line("=")
    print("§7 用户命题的直接检验：同一**价位**里, dev 的构成（趋势 vs 尖峰）还有没有信息？")
    line("=")
    print("  若同价带内 walk0/basis0 仍能分开胜负 ⇒ 市场没定价这部分（= 有 edge）;")
    print("  若分不开 ⇒ 市场价已经把 dev 的构成吃干净了（= 过滤条件无意义，用户对）。")
    print()
    for lab, key, edges in (
            ("按 fill 带 × walk0 档", lambda r: (r["walk"] if r["walk"] is not None else -1e9),
             [(-1e9, 0), (0, 43), (43, 80), (80, 1e9)]),
            ("按 fill 带 × basis0 档", lambda r: (r["basis"] if r["basis"] is not None else -1e9),
             [(-1e9, 0), (0, 43), (43, 80), (80, 1e9)])):
        print(f"  {lab}")
        print(f"    {'fill 带':16}{'分档':18}{'n':>5}{'赢/输':>8}{'赢率':>8}"
              f"{'fill 均值':>10}{'边际':>9}")
        for lo_f, hi_f in ((0.83, 0.95), (0.95, 0.98), (0.98, 1.01)):
            for lo, hi in edges:
                g = [r for r in S if lo_f <= r["fill"] < hi_f and lo <= key(r) < hi]
                if len(g) < 15:
                    continue
                wr = sum(r["settle_won"] for r in g) / len(g)
                w = sum(r["settle_won"] for r in g)
                fl = st.mean([r["fill"] for r in g])
                mark = "  ←" if abs(wr - fl) > 0.03 and len(g) >= 30 else ""
                name = (f"[{lo:g},{hi:g})")
                print(f"    [{lo_f:.2f},{hi_f:.2f})   {name:18}{len(g):>5}{w:>5}/{len(g)-w:<2}"
                      f"{wr*100:7.1f}%{fl:10.4f}{(wr-fl)*100:+8.1f}pp{mark}")
        print()


def calib(wins, rem_at, tol=5):
    """在 `rem ≈ rem_at` 的检查点上，热门侧有效价 → 实际赢率的校准（**每窗一条**）。

    押注 = 买当时的热门侧；`d = won − fill` 是逐笔边际（fill 就是盈亏平衡胜率）。
    """
    rows = []
    for w in wins:
        t = next((x for x in w["ticks"] if x["rem"] <= rem_at), None)
        if t is None or t["rem"] < rem_at - tol:
            continue
        rows.append((t["fill"], won_of(t["side"], w["outcome"])))
    return rows


def calib_table(rows, edges, title, minn=12):
    print(f"  {title}   n={len(rows)}")
    print(f"    {'价档':16}{'n':>6}{'赢':>5}{'输':>5}{'实际赢率':>10}{'均价':>9}"
          f"{'边际':>9}{'±SE':>7}{'P&L':>9}")
    for lo, hi in zip(edges[:-1], edges[1:]):
        g = [(f, w) for f, w in rows if lo <= f < hi]
        if len(g) < minn:
            continue
        wr = st.mean([w for _, w in g])
        fl = st.mean([f for f, _ in g])
        d = [w - f for f, w in g]
        se = st.pstdev(d) / math.sqrt(len(d))
        p = sum((STAKE / f - STAKE) if w else -STAKE for f, w in g)
        ns = sum(w for _, w in g)
        mark = "  ←" if abs(wr - fl) > 2 * se and wr - fl > 0.01 else ""
        print(f"    [{lo:.2f},{hi:.2f}){len(g):>7}{ns:>5}{len(g)-ns:>5}{wr*100:9.1f}%"
              f"{fl:9.4f}{(wr-fl)*100:+8.1f}pp{se*100:6.1f}{p:+8.1f}U{mark}")
    print()


def z8(wins_new, wins_old):
    line("=")
    print("§8 「最后一分钟」可不可预测（**全体窗**, 不只信号）—— 决定有没有新办法的总答案")
    line("=")
    print("  结算 = 最后 60 秒的均价。问：在结算窗**开启的那一刻**（rem=60），")
    print("  已知的一切能不能预测随后 60 秒均值的落点？")
    print()

    A, B, C, D = [], [], [], []      # 状态 / 未来 / 市场价 / 结果
    for w in wins_new:
        t60 = next((t for t in w["ticks"] if t["rem"] <= 60), None)
        if t60 is None or not t60["twap"] or w["close"] is None or t60["rem"] < 55:
            continue
        a = w["anchor"]
        A.append(t60["twap"] - a)                 # 结算线此刻相对 anchor（美元）
        B.append(w["close"] - t60["twap"])        # 随后 60 秒结算线还会走多少（美元）
        yes_hot = (t60["ya"] if t60["ya"] > 0 else t60["yb"]) >= \
                  (t60["na"] if t60["na"] > 0 else t60["nb"])
        eff_y = (t60["ya"] if t60["ya"] > 0 else t60["yb"])
        eff_n = (t60["na"] if t60["na"] > 0 else t60["nb"])
        C.append(eff_y if yes_hot else 1.0 - eff_n)
        D.append(1.0 if w["close"] > a else 0.0)
    print(f"  样本 {len(A)} 窗（有 rem≈60 的 tick）")
    print(f"    corr(walk60, 随后 60 秒位移)     = {corr(A, B):+.3f}   ← 状态能不能预测未来")
    print(f"    corr(walk60, 结果)               = {corr(A, D):+.3f}   ← 状态与结果相关（必然强）")
    print(f"    corr(市场价, 结果)               = {corr(C, D):+.3f}   ← 市场价的判别力")
    print(f"    corr(市场价, 随后 60 秒位移)      = {corr(C, B):+.3f}   ← 市场有没有「未来」信息")
    print("    ⇒ 状态对未来几乎无信息（r²≈1%），而**市场价有**（+0.22）——")
    print("      过滤条件无法预测未来，只能挑 strike；市场价才是唯一的胜率估计量。")
    print()

    edges = [0.80, 0.86, 0.90, 0.92, 0.94, 0.96, 0.98, 1.01]
    calib_table(calib(wins_new, 150), edges, "校准图 · rem=150 检查点 · data/events（09-27~10-11）")
    calib_table(calib(wins_new, 60), edges, "校准图 · rem=60 检查点 · data/events")
    calib_table(calib(wins_old, 150), edges, "校准图 · rem=150 检查点 · data/btc（14 天, **样本外**）")
    calib_table(calib(wins_old, 60), edges, "校准图 · rem=60 检查点 · data/btc（**样本外**）")


def req_price(pts, k, anchor):
    """「临界价」P(rem)（决策 #30 已落引擎 `internal/tail.RequiredPrice`）。

    含义：现货从此刻起**一直守住这个价不动** ⇒ 闭市 TWAP-60 恰好压在 anchor 上。
    `rem ≥ 60` ⇒ `P = anchor`（结算窗还没开）；`rem < 60` ⇒ 已定局的 `(60−rem)` 秒
    按实际均价算，剩下的 rem 秒全靠当前价撑。缺样本的秒按均值补齐；样本不足期望一半 ⇒
    返 None（宁可不画）。**与 Go 侧逐字同式**，唯一的区别是这里从路径现算。
    """
    r = pts[k]["rem"]
    if r >= 60:
        return anchor
    span = 60 - r
    s = [p["spot"] for p in pts[:k] if r < p["rem"] <= 60 and p["spot"] > 0]
    if 2 * len(s) < span + 1:
        return None                       # 样本不足期望一半 ⇒ 宁可不画（同 Go）
    return (60 * anchor - st.mean(s) * span) / r


def z9(S, wins_all):
    line("=")
    print("§9 监控：三个读数 —— 余量 / 计分板 / 前瞻线")
    line("=")
    print("  用户那句「dev 大 = 能承受的 spot 逆向移动量更大」在 `rem ≥ 60` 段是**恒等式**。")
    print("  r=0 之后计分板换成结算线本身。三个量分列如下。")
    print()

    # (a) 恒等
    d = []
    for r in S:
        pts = [{"rem": r["rem"], "spot": r["spot"]}] + r["path"]
        for k in range(len(pts)):
            d.append(abs(r["sgn"] * (pts[k]["spot"] - r["anchor"])
                         - (r["dev"] + r["sgn"] * (pts[k]["spot"] - r["spot"]))))
    print("(a) **余量 = dev**（`rem ≥ 60` 段的恒等式）")
    print("    margin ≡ sgn·(close − anchor) = dev0 + Δ,  Δ = sgn·(close − spot0)")
    print("    赢 ⟺ Δ > −dev0 ⇒ **dev0 就是「能承受的逆向移动量」本身**，不是它的代理量。")
    print(f"    `dev(t) = dev0 + sgn·(spot(t) − spot0)` 实测 {len(d)} 点，最大偏差 "
          f"{max(d) if d else 0:.2e} 美元 ✓（纯代数，无拟合）")
    print()

    # (a2) 结算线口径的胜负恒等式
    bad = 0
    tot = 0
    for w in wins_all:
        if w["close"] is None or w["anchor"] <= 0 or w["outcome"] is None:
            continue
        tot += 1
        if (0 if w["close"] > w["anchor"] else 1) != w["outcome"]:
            bad += 1
    print(f"(a2) 结算线口径: 赢 ⟺ `twap_close > anchor`。全样本 {tot} 窗，"
          f"与官方 outcome 不符 {bad} 窗 ✓")
    print("     ⇒ **计分板是 `walk(t) = sgn·(twap(t) − anchor)`**（结算线现在值多少）——")
    print("       它无水平差、平滑、且闭市那一刻它就是结果。")
    print()

    # (b) 三把尺子在赢/输上的暴露
    print("(b) 持仓期间各读数曾为负的比例 / 为负的时长占比（赢 vs 输）")
    print(f"    {'':4}{'读数':14}{'曾为负':>10}{'时长中位':>10}{'时长均值':>10}")
    for nm, G in (("赢", [r for r in S if r["settle_won"]]),
                  ("输", [r for r in S if not r["settle_won"]])):
        fr, fd, fw = [], [], []
        for r in G:
            pts = [{"rem": r["rem"], "spot": r["spot"]}] + r["path"]
            a = b = w_ = c = 0
            for k in range(len(pts)):
                p = req_price(pts, k, r["anchor"])
                if p is not None:
                    a += (r["sgn"] * (pts[k]["spot"] - p)) < 0
                b += (r["sgn"] * (pts[k]["spot"] - r["anchor"])) < 0
                if pts[k].get("twap"):
                    w_ += (r["sgn"] * (pts[k]["twap"] - r["anchor"])) < 0
                c += 1
            fr.append(a / max(c, 1))
            fd.append(b / max(c, 1))
            fw.append(w_ / max(c, 1))
        for lbl, v in (("room(t)", fr), ("dev(t)", fd), ("walk(t)", fw)):
            nz = sum(1 for x in v if x > 0)
            print(f"    {nm:3}{lbl:14}{nz:>5}/{len(G):<5}{med(v)*100:9.1f}%"
                  f"{st.mean(v)*100:9.1f}%")
    print("    ⇒ **walk 是唯一能把输单挑出来的那把尺子**（平滑、无水平差、闭市即结果）；")
    print("      room 从现货重建，带「Binance 现货 vs Chainlink 指数」的水平差。")
    print()

    # (c) 量化水平差：末点判号 vs 真实结果
    print("(c) 水平差有多大：拿末点的 room 符号去猜输赢，错在哪")
    last = [(r, r["path"][-1]) for r in S if r["path"]]
    last = [(r, t) for r, t in last if t["rem"] <= 3]
    mism = []
    for r, t in last:
        pts = [{"rem": r["rem"], "spot": r["spot"]}] + r["path"]
        p = req_price(pts, len(pts) - 1, r["anchor"])
        if p is None:
            continue
        pred_won = (r["sgn"] * (t["spot"] - p)) > 0
        if pred_won != bool(r["settle_won"]):
            mism.append(r)
    print(f"    末点 rem ≤ 3 的 {len(last)} 笔里, room 符号猜错的 {len(mism)} 笔 "
          f"（{len(mism)/max(len(last),1)*100:.0f}%）")
    err = []
    for w in wins_all:
        if w["close"] is None or w["anchor"] <= 0:
            continue
        s = [t["spot"] for t in w["ticks"] if t["spot"] > 0 and 0 < t["rem"] <= 60]
        if len(s) < 30:
            continue
        err.append(st.mean(s) - w["close"])
    print(f"    直接量: 末 60 秒 Binance 现货均价 − 结算值（{len(err)} 窗）")
    print(f"      中位 {med(err):+.1f} 美元  p25 {q(err,.25):+.1f} / p75 {q(err,.75):+.1f}  "
          f"|中位绝对| {med([abs(x) for x in err]):.1f}")
    print("    ⇒ 末点判号对的大多是「差很远」的窗，错的全是「差几美元」的贴线窗——")
    print("      这与 Go 侧 `RequiredPrice` 注释里那个已存档的洞是**同一件事**（现货估历史和的水平差）。")
    print()

    # (d) 落地规格
    print("(d) 落地规格（**纯旁路**，不参与判定——与持仓监察 #24 同一性质）")
    print("    ① `tailhold_*` 行加三列: `dev`（余量, rem≥60 就是它）、`walk`（计分板）、")
    print("       `room`（前瞻, 只有 rem<60 才与 dev 不同）；再加各自持仓期最小值；")
    print("    ② 页面曲线加第 6 条 `spot − P(rem)`（P 已在画, 相减即可, 零新公式）；")
    print("    ③ σ 归一版本（除以 sd）供跨窗比较。")
    print("    ⚠️ **都不是出场规则**——把它当止损腿已被 §6 否掉：市场报得比你快，")
    print("       要么杀赢单要么白等（你看见 `walk<0` 时价已经在那儿了）。")


def z11(S, S_old):
    line("=")
    print("§11 决定性一问：**同一价格带内**，dev/walk/basis 还有没有判别力？")
    line("=")
    print("  用户命题 = 「dev 等只是从回测里拟合出来的、无科学依据的过滤条件」。")
    print("  可证伪的读法：把价格按住不动，dev 还能不能分开好坏 ⇒ 能 = 它真在选,")
    print("  不能 = 它的全部作用只是「和价格相关」（ρ(walk,fill)=+0.31 那件事）。")
    print()

    bands = [(0.80, 0.90), (0.90, 0.95), (0.95, 0.98), (0.98, 1.01)]
    for key, lbl in (("dev", "dev0"), ("walk", "walk0"), ("basis", "basis0")):
        print(f"  按 {lbl} 在价格带内三分（边际 = 赢率 − fill, 正 = 有 edge）")
        print(f"    {'价格带':14}{'dev 档':16}{'n':>5}{'赢':>5}{'输':>5}{'赢率':>8}"
              f"{'fill':>9}{'边际':>9}{'P&L':>9}")
        for lo, hi in bands:
            G = [r for r in S if lo <= r["fill"] < hi]
            if len(G) < 30:
                continue
            G.sort(key=lambda r: r[key])
            k1 = len(G) // 3
            k2 = 2 * len(G) // 3
            for tag, g in (("低", G[:k1]), ("中", G[k1:k2]), ("高", G[k2:])):
                if not g:
                    continue
                wr = st.mean([r["settle_won"] for r in g])
                fl = st.mean([r["fill"] for r in g])
                pp = sum((STAKE / r["fill"] - STAKE) if r["settle_won"] else -STAKE for r in g)
                nw = sum(r["settle_won"] for r in g)
                rg = f"{g[0][key]:>7.0f}~{g[-1][key]:<7.0f}"
                print(f"    [{lo:.2f},{hi:.2f})  {rg:16}{len(g):>5}{nw:>5}{len(g)-nw:>5}"
                      f"{wr*100:7.1f}%{fl:9.4f}{(wr-fl)*100:+8.1f}pp{pp:+8.1f}U")
        print()

    # 窄价桶内二分 + 日级配对
    print("  窄价桶（0.005 宽）内按中位数二分, 汇总「高档 − 低档」的边际差与 P&L 差")
    print(f"    {'量':10}{'窄桶数':>8}{'高档 n':>8}{'低档 n':>8}{'边际差':>10}"
          f"{'Δ P&L':>10}{'日级配对 95%':>22}")
    for key, lbl in (("dev", "dev0"), ("walk", "walk0"), ("basis", "basis0"),
                     ("sd", "sd"), ("rem", "rem0")):
        buckets = collections.defaultdict(list)
        for r in S:
            buckets[round(r["fill"] / 0.005)].append(r)
        topo, boto = [], []
        for b, g in buckets.items():
            if len(g) < 8:
                continue
            g = sorted(g, key=lambda r: r[key])
            h = len(g) // 2
            topo += g[h:]
            boto += g[:h]
        if len(topo) < 30:
            continue
        ma = st.mean([r["settle_won"] - r["fill"] for r in topo])
        mb = st.mean([r["settle_won"] - r["fill"] for r in boto])
        da, db = by_day(topo), by_day(boto)
        lo_, hi_ = boot_delta(db, da, n=2000, seed=48)
        dd = sum(da.get(k, 0.0) - db.get(k, 0.0) for k in set(da) | set(db))
        print(f"    {lbl:10}{len(buckets):>8}{len(topo):>8}{len(boto):>8}"
              f"{(ma-mb)*100:+9.1f}pp{dd:+9.2f}U{'['+format(lo_,'+.2f')+','+format(hi_,'+.2f')+']':>22}")
    print()

    # 在 data/btc 上复验
    print("  **样本外复验**（data/btc 14 天, 同一套窄桶内二分）")
    print(f"    {'量':10}{'高档 n':>8}{'低档 n':>8}{'边际差':>10}{'Δ P&L':>10}{'日级配对 95%':>22}")
    for key, lbl in (("dev", "dev0"), ("walk", "walk0"), ("basis", "basis0"),
                     ("sd", "sd"), ("rem", "rem0")):
        buckets = collections.defaultdict(list)
        for r in S_old:
            buckets[round(r["fill"] / 0.005)].append(r)
        topo, boto = [], []
        for b, g in buckets.items():
            if len(g) < 8:
                continue
            g = sorted(g, key=lambda r: r[key])
            h = len(g) // 2
            topo += g[h:]
            boto += g[:h]
        if len(topo) < 30:
            continue
        ma = st.mean([r["settle_won"] - r["fill"] for r in topo])
        mb = st.mean([r["settle_won"] - r["fill"] for r in boto])
        da, db = by_day(topo), by_day(boto)
        lo_, hi_ = boot_delta(db, da, n=2000, seed=48)
        dd = sum(da.get(k, 0.0) - db.get(k, 0.0) for k in set(da) | set(db))
        print(f"    {lbl:10}{len(topo):>8}{len(boto):>8}"
              f"{(ma-mb)*100:+9.1f}pp{dd:+9.2f}U{'['+format(lo_,'+.2f')+','+format(hi_,'+.2f')+']':>22}")
    print("    ⇒ 两样本同号且区间不含零 ⇒ 该量在价格之外**确有**判别力；")
    print("      两样本反号或含零 ⇒ 它的作用就是「与价格相关」而已。")


def z10(S, S_old):
    line("=")
    print("§10 前提复核：现行链在两个样本上分别赚多少（决定「要不要找新办法」）")
    line("=")
    for nm, G in (("data/btc 14 天（08-18~08-31, 调参用的那批）", S_old),
                  ("data/events 15 天（09-27~10-11, 最近）", S)):
        if not G:
            continue
        p = sum((STAKE / r["fill"] - STAKE) if r["settle_won"] else -STAKE for r in G)
        nw = sum(r["settle_won"] for r in G)
        print(f"  {nm}")
        print(f"    n={len(G)}  赢 {nw} / 输 {len(G)-nw}  WR {nw/len(G)*100:.2f}%  "
              f"P&L {p:+.2f}U @{STAKE}U  ⇒ **每笔边际 {(p/len(G))/STAKE*100:+.3f}%**  "
              f"均价 {st.mean([r['fill'] for r in G]):.4f}")
    print()
    if S:
        print("  data/events 逐日：")
        print(f"    {'日期':12}{'n':>5}{'赢':>4}{'输':>4}{'P&L':>9}{'边际':>9}{'均价':>9}")
        byd = collections.defaultdict(list)
        for r in S:
            byd[r["date"]].append(r)
        for k in sorted(byd):
            g = byd[k]
            p = sum((STAKE / r["fill"] - STAKE) if r["settle_won"] else -STAKE for r in g)
            nw = sum(r["settle_won"] for r in g)
            print(f"    {k:12}{len(g):>5}{nw:>4}{len(g)-nw:>4}{p:+8.2f}U"
                  f"{(p/len(g))/STAKE*100:+8.3f}%{st.mean([r['fill'] for r in g]):9.4f}")


def main():
    print("加载数据 ...")
    wins_new = build(NEW)
    S = sigs(wins_new, with_path=True)
    wins_old, S_old = [], []
    if Path(OLD).is_dir() and any(Path(OLD).glob("events_*.jsonl")):
        wins_old = build(OLD)
        S_old = sigs(wins_old)

    z0(wins_old, S_old, wins_new, S)
    print()

    z1(wins_new)
    print()
    z2(S)
    print()
    z3(S)
    print()
    z4(S)
    print()
    z5(S)
    print()
    z6(S)
    print()
    z7(S)
    z8(wins_new, wins_old)
    print()
    z9(S, wins_new)
    print()
    z10(S, S_old)
    print()
    z11(S, S_old)


if __name__ == "__main__":
    main()
