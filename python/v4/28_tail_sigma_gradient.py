#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫尾盘：σ 梯度阈值（dev_min 随 sd 变化）—— 2026-09-26

起因（用户实盘观察）：低波动时段（周末）σ 塌缩——BTC 5 分钟窗口的 1σ 折美元
（下称 sd）从平日的 100+ 掉到 20~30，信号数与成交量同步下降。现行 ⑤ 的两条腿是

    ⑤ = hot ≥ 0.80 ∧ ( dev ≥ 63 ∨ ( sd ≥ 40 ∧ dev ≥ sd ) )
    ② = hot ≥ 0.80 ∧   dev ≥ 63                   （监听段，无 σ 腿）

把 dev 换算成「位移等于几个 σ」（z = dev / sd，无量纲），现行规则实际要求的门槛是

    sd <  40        z ≥ 63/sd     ← σ 越小要求越苛刻（sd=25 要 2.5σ）★断点在此
    40 ≤ sd < 63    z ≥ 1.0       ← 等价于 dev ≥ sd
    sd ≥ 63         z ≥ 63/sd     ← σ 越大越宽松（sd=120 只要 0.53σ）

即那条「连续段」只覆盖 sd ∈ [40, 63)，两侧都跳回固定 63 美元——低波动期正好落在
最苛刻的一侧。用户要求：**不要固定 dev，改成随 sd 变化的梯度或算法，且检查时点
（T=150 / T=60 / 监听段）保持不变。**

结论（完整报告见 docs/tail_sigma_gradient_2026-09-26.md）：

  ★ 单纯的 σ 梯度（dev ≥ max(1σ, 25) 美元）在两个样本上方向不一致——回测
    +12.3U、实盘 −2.2U，合并 Δ+10.14U 但 95% 区间含 0 ⇒ **不采纳**。
  ★ 把梯度**限定在热门侧已 ≥ 0.95 时启用**（市场自己已给出强确认），是唯一在
    两个样本上都为正、且日级配对区间不含 0 的族：合并 Δ+4.93U，区间
    [+1.00, +10.71]；边际族 196 笔只有 1 笔输（99.5% 胜率 vs 平衡线 98.4%）。
  ★ 该族**不删任何现行信号**（窗集合层面纯增量；另有 22 个共有窗换了入场 tick），
    恢复量集中在低σ日（08-29 34→109、08-30 78→126、09-26 14→23），平日几乎不动。
  ✗ 但**「保证胜率」这道检验整张网格没有一档通过**（§5 的 Wilson 列）：边际族的胜率
    95% 下界始终低于它自己的平衡线（0.95 档 97.17% vs 98.40%；最接近的 0.97 档
    98.00% vs 98.74%）⇒ 就字面意义而言**没找到合格的阈值**。
  ⚠️ `0.95` 与 `1σ∧25` 都**不可辨识**：回测里价格闸越松越好（无闸 +59.85U 高于
     网格上任何一点），argmax 贴网格边缘；选 0.95 的唯一依据是实盘子样本
     （纯梯度实盘 −3.05U → 加闸 +0.41U），代价 ≈ 5.2U 回测 P&L。0.97 变体同理
     （去掉 [0.95,0.97) 那 8 笔 = −1.31U 的负档，但同样不显著）。**禁止再调**。

处置（2026-09-26 用户决定）：**纸面登记，不改引擎**——引擎/oracle/配置/parity 全不动。


方法论红线：所有候选都必须**重跑三段递进链**——放宽阈值会改变「哪个 tick 首次
达标」，在既有信号池上打标签得到的增量族是错的。做法是直接替换 oracle（23 号）
的 r5/r2 两个谓词后调用它自己的 chain（§3 断言基线逐位复现 oracle）。

口径与坐标（与 oracle 23 逐位一致）：
  dev  = sgn·(spot − anchor)，美元；正 = 朝押注方向（sgn: 押 yes +1 / 押 no −1）
  sd   = hist_bps × anchor / 1e4，美元 = 该窗 1σ 折美元
         hist_bps = 前 ≤18 个已完窗口 |close−open|/open 的均值（需 ≥3 窗）
  z    = dev / sd，无量纲（位移 = 几个 σ）
  hot  = 判定 tick 热门侧有效价（ask 优先 / bid 兜底，决策 #22）
  fill = hot（回测口径：触发瞬间按限价成交、无滑点）；shares = 2U / fill
  ★ **盈亏平衡胜率 ≡ 成交价 fill**：赢 = shares − 2U = 2·(1−p)/p、输 = −2U
    ⇒ WR = p 时 EV = 0。故「fill 0.98 ⇒ 必须赢 98% 才不亏」是本表的读法。

两个样本：
  回测 = data/btc（14 天，08-18~08-31，3800+ 窗）
  实盘 = data/tail-live（09-24~09-26，3 天；含 09-24/25 的 stake=10 真实挂单成交）
  ⚠️ 两样本一律按 2U/注 归一（P&L 只反映**每笔形态**，不是真实盈亏额）。
  ⚠️ 实盘监听段**无逐 tick 记录**（引擎不落被拒的监听 tick）⇒ 本次只改 ⑤、②
     一字不动，监听段与基线逐窗相同 ⇒ 实盘子样本仍可完整复核。

用法: python/venv/bin/python python/v4/28_tail_sigma_gradient.py
      python/venv/bin/python python/v4/28_tail_sigma_gradient.py --live-dir data/tail-live
"""
import sys
import json
import glob
import random
import argparse
import datetime
import collections
import importlib.util
from pathlib import Path

BASE = Path(__file__).resolve().parent          # python/v4
sys.path.insert(0, str(BASE.parent))            # python/
from v2.lib import load_events                                    # noqa: E402

DATA = BASE.parent.parent / "data"

STAKE = 2.0
DEV_USD = 63.0            # 现行 ⑤/② 的绝对位移门槛（美元）
SD_MIN_USD = 40.0         # 现行 ⑤ σ 腿的 σ 下限（美元）
P_FLOOR = 0.80            # 价格腿（T=150 段严格大于，其余 ≥，决策 #26）
GRAD_Z = 1.0              # 梯度族的 σ 倍数
GRAD_FLOOR = 25.0         # 梯度族的美元地面
P_HI = 0.95               # 梯度族的价格闸（热门侧 ≥ 此值才启用放宽腿）
LOWSD_SD = 30.0           # 【用户指定候选】只在 sd < 此值的区间启用梯度，其余走现行
LOWSD_Z = 1.25            # 【用户指定候选】该区间的 σ 倍数
LOWSD_FLOOR = 25.0        # 【用户指定候选】该区间的美元地面
LOWSD_LABEL = "低σ梯度 sd<30:1.25σ∧25"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


S13 = load("s13", BASE / "13_tail_sweep.py")       # hist_ranges（σ 定义）
S23 = load("s23", BASE / "23_tail_integrated.py")  # oracle（现行权威链）
R5_CUR, R2_CUR = S23.r5, S23.r2                    # 现行谓词（备份）


# ── 通用统计 ──────────────────────────────────────────────────────────────

def pl(rows):
    """P&L（美元）：赢 = 2/fill − 2；输 = −2"""
    return sum((STAKE / r["fill"] - STAKE) if r["won"] else -STAKE for r in rows)


def wr(rows):
    return sum(r["won"] for r in rows) / len(rows) if rows else 0.0


def avg_fill(rows):
    return sum(r["fill"] for r in rows) / len(rows) if rows else 0.0


def per_day(rows):
    d = collections.defaultdict(float)
    for r in rows:
        d[r["date"]] += (STAKE / r["fill"] - STAKE) if r["won"] else -STAKE
    return d


def line(lab, rows, width=26):
    if not rows:
        print("  " + lab.ljust(width) + "n=0")
        return
    p = avg_fill(rows)
    d = per_day(rows)
    neg = sum(1 for v in d.values() if v < 0)
    print("  " + lab.ljust(width)
          + f"n={len(rows):<5} 胜率 {wr(rows)*100:6.2f}%  均价 {p:.4f}（平衡线 {p*100:.1f}%）  "
          + f"EV/注 {pl(rows)/len(rows):+.4f}  P&L {pl(rows):+7.2f}U  亏损日 {neg}/{len(d)}")


def wilson_lo(k, n, z=1.96):
    """胜率的 Wilson 95% 下界（闭式，无依赖）。

    ⚠️ 这是回答「**在保证胜率的前提下**」那句话的唯一判据：把胜率区间下界与
    平衡线（= 成交价）比。下界 > 平衡线才算「显著地不亏」；n≈190 而超额只有
    ~1 个百分点时，这个不等式**过不去**（见 §5 表）。
    注意它与「日级配对 Δ 区间」回答的是两个不同问题——后者问「加的这些笔是否
    稳定为正」（按 17 天重采样，日间相关性让它更容易显著），前者问「这个族的
    绝对胜率是否高于它的平衡线」。
    """
    if n == 0:
        return 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return max(0.0, (c - h) / d)


def boot_delta(cand, base, reps=2000, seed=42):
    """日级配对 bootstrap：同批重采样日期上算 ΔP&L = cand − base 的 95% 区间。

    ⚠️ 判据必须是**配对 Δ** 的区间：候选与基线在同一批窗上高度相关，各自算绝对
    区间会同时包含对方的点估计（24 号止损评估的同款教训）。
    """
    dc, db = per_day(cand), per_day(base)
    days = sorted(set(dc) | set(db))
    rng = random.Random(seed)
    out = []
    for _ in range(reps):
        pick = [rng.choice(days) for _ in days]
        out.append(sum(dc[d] - db[d] for d in pick))
    out.sort()
    return out[int(0.025 * reps)], out[int(0.975 * reps)]


# ── 三段链：回测判定行（走 oracle 自己的 chain）────────────────────────────

def run_backtest(ev, hr, r5):
    """用 oracle 的 chain 跑 14 天。

    返回 (judgments, signals)：
      judgments = slug → {stage: 行}（只收 t150/t60 判定行，供候选重判——这两段的行
                  身份只由 tick 流决定、与规则无关，所以重判 ≡ 重跑链的第 1/2 段）
      signals   = **完整三段链**的全部 ok 行（含监听段，与实盘同口径）
    """
    S23.r5, S23.r2 = r5, R2_CUR                      # ② 一字不动
    jud, sig = collections.defaultdict(dict), []
    try:
        for e in ev:
            oc, anchor = e.get("outcome"), e.get("twap_open_price")
            h = hr.get(e["start_time"])
            if oc is None or not anchor or h is None:
                continue                              # σ 未就绪 ⇒ 整窗跳过（决策 #13）
            sd = h / anchor * 1e4 * anchor / 1e4      # 保持运算序列，与 Go 逐位一致
            date = datetime.datetime.fromtimestamp(
                e["start_time"], datetime.timezone.utc).strftime("%Y-%m-%d")
            ts = S23.win_ticks(e)
            if not ts:
                continue
            rows, _ = S23.chain(ts, anchor, sd, date, oc)
            for r in rows:
                r["slug"], r["date"], r["src"] = e["slug"], date, "回测"
                r["won"] = bool(r["settle_won"])       # 统一下游读法（回测/实盘同键）
                if r["stage"] in ("t150", "t60"):
                    jud[e["slug"]][r["stage"]] = r
                if r.get("ok"):
                    sig.append(r)
    finally:
        S23.r5, S23.r2 = R5_CUR, R2_CUR
    return jud, sig


# ── 实盘判定行 ────────────────────────────────────────────────────────────

def load_live(dirpath):
    """→ (t150/t60 判定行, 全部行, 已结算窗→won, tailwin 窗→(anchor, close))。

    ⚠️ 已结算行直接取行内 `won`（官方边界推送口径，决策 #19）——引擎当时**拒绝
    过、从未下单**的窗没有 `won`（未注册结算），必须用 tailwin 的 (anchor, close)
    推导方向，否则会把全部新信号误判成「输」。
    """
    jud, allst, settled, win = collections.defaultdict(dict), collections.defaultdict(dict), {}, {}
    for p in sorted(glob.glob(str(Path(dirpath) / "tailwin_*.jsonl"))):
        for L in open(p):
            if L.strip():
                r = json.loads(L)
                win[r["slug"]] = (r["anchor"], r["close"])
    for p in sorted(glob.glob(str(Path(dirpath) / "tail_*.jsonl"))):
        for L in open(p):
            if not L.strip():
                continue
            r = json.loads(L)
            if r.get("kind") != "snap":
                continue                              # frame/scan 是 legacy（决策 #22）
            r["fill"] = r.get("hot_ask") or 0
            r["sig"] = (r["dev"] / r["sd"]) if r.get("sd") else None
            r["src"] = "实盘"
            allst[r["slug"]][r["stage"]] = r
            if r.get("ok") and r.get("won") is not None:
                settled[r["slug"]] = bool(r["won"])
            if r["stage"] in ("t150", "t60"):
                jud[r["slug"]][r["stage"]] = r
    return jud, allst, settled, win


def live_outcome(slug, r, settled, win):
    """(won, 来源)。已结算行用行内官方结果；否则 tailwin 推导；无覆盖返回 None。

    tailwin 推导 = close > anchor ⇒ Up ⇒ 押 yes 者赢。贴线（|close−anchor| 不足
    0.3bp）单独标注——那是决策 #19 记的「σ 的 close 仍是到达口径」造成的，官方
    结果与推导可能相反。
    """
    if slug in settled:
        return settled[slug], "官方"
    a, c = win.get(slug, (None, None))
    if a is None:
        return None, "无覆盖"
    near = abs(c - a) < max(2.0, abs(a) * 3e-5)
    return ((r["side"] == "yes") == (c > a)), ("推导·贴线" if near else "推导")


def judge(jud, listen_map, settled, win, leg, is_live):
    """按 leg 重判 t150/t60 → 本样本的信号行（含胜负）。

    段序与 oracle 的 chain 一致：t150（价格腿严格大于）→ t60（≥）→ 监听段。
    ⚠️ 监听段用 `listen_map`（基线已记录的行，两个样本都传）——② 一字不动 ⇒
    该行在基线与候选里**逐位相同**，对 Δ 贡献恒为 0。**必须传**，否则「原本在监听段
    入场的窗被候选在 t150 提前接走」会被记成整笔新增 P&L（而不是相对监听段的价差），
    使 Δ 系统性偏乐观。
    """
    out = []
    for slug, st in jud.items():
        hit = None
        a, b = st.get("t150"), st.get("t60")
        if a is not None and a["fill"] > P_FLOOR and leg(a):
            hit = a
        elif b is not None and b["fill"] >= P_FLOOR and leg(b):
            hit = b
        if hit is None:
            hit = listen_map.get(slug)                # 监听段信号（两样本同源）
            if hit is None:
                continue
        w, how = (live_outcome(slug, hit, settled, win) if is_live
                  else (bool(hit["settle_won"]), "回测"))
        if w is None:
            continue
        hit = dict(hit)
        hit["won"], hit["how"] = w, how
        out.append(hit)
    return out


# ── 候选谓词 ──────────────────────────────────────────────────────────────

def _price_ok(r, strict_price):
    """价格腿（决策 #26：T=150 段严格大于、其余 ≥）——**所有候选一字不动**。"""
    return r["fill"] > P_FLOOR if strict_price else r["fill"] >= P_FLOOR


def make_r5(zmul, floor, p_hi):
    """⑤ 的 dev 腿：hot ≥ p_hi 时用梯度 `dev ≥ min(63, max(z·sd, floor))`，否则用现行。

    p_hi = 0.0 ⇒ 纯梯度（不设价格闸）；p_hi = 9.9 ⇒ 恒走现行（= 基线）。
    ⚠️ 该梯度腿是现行 ⑤ σ 腿的**超集**（现行要求 sd≥40 ∧ dev≥sd，梯度要求
    dev ≥ min(63, max(z·sd, floor))，z=1 且 floor≤40 时逐点更松）⇒ **只增不删**，
    没有「被删掉的基线信号」这一项成本（§6.3 自检）。
    """
    def r5(r, strict_price=False):
        if not _price_ok(r, strict_price):             # 价格腿：与现行逐位相同
            return False
        if r["fill"] < p_hi:                           # 价格闸未过 ⇒ 走现行 dev 腿
            return R5_CUR(r, strict_price)
        bar = min(DEV_USD, max(zmul * r["sd"], floor)) if r["sd"] else DEV_USD
        return r["dev"] >= bar
    return r5


def make_r5_lowsd(zmul, floor, sd_below, p_hi=0.0):
    """**只在 sd < sd_below 时**用梯度 `dev ≥ min(63, max(z·sd, floor))`，其余走现行。

    价格腿一字不动（用户 2026-09-26 要求）。因 sd_below ≤ 40 时现行 σ 腿（要求
    sd ≥ 40）恒不成立 ⇒ 现行在该区间实际就是「dev ≥ 63」，而梯度门槛
    ≤ min(63, max(z·sd_below, floor)) < 63 ⇒ 仍是**超集、只增不删**。
    p_hi > 0 时再叠一道价格闸（仅作附加对照，不属用户要求的那一版）。
    """
    def r5(r, strict_price=False):
        if not _price_ok(r, strict_price):
            return False
        if r["sd"] is not None and r["sd"] < sd_below and r["fill"] >= p_hi:
            return r["dev"] >= min(DEV_USD, max(zmul * r["sd"], floor))
        return R5_CUR(r, strict_price)
    return r5


def make_r5_lowsd_fixed(x, sd_below, p_hi=0.0):
    """sd < sd_below 时用**固定美元门槛** `dev ≥ x`，其余走现行 ⑤；价格腿一字不动。

    与 `make_r5_lowsd`（σ 梯度）的区别：这里 x 是**常数**，不看 sd。
    现行在 sd<40 上实际就是 `dev ≥ 63`（σ 腿要求 sd≥40）⇒ x < 63 时仍是**超集**。
    x = 63 ⇒ 逐位等于基线（对账用）。
    """
    def r5(r, strict_price=False):
        if not _price_ok(r, strict_price):
            return False
        if r["sd"] is not None and r["sd"] < sd_below and r["fill"] >= p_hi:
            return r["dev"] >= x
        return R5_CUR(r, strict_price)
    return r5


def current_r5(r, strict_price=False):
    return R5_CUR(r, strict_price)


def listen_of(rows):
    """slug → 监听段 ok 行（② 一字不动 ⇒ 基线与候选的监听段逐位相同）。

    ⚠️ 用法有前提：候选必须是**超集**（候选通过 ⇒ 现行通过）。这样「候选在 t150/t60
    都拒」⇒「现行也拒」⇒ 现行当初确实走到过监听段 ⇒ 用基线监听行补齐是**精确**的。
    非超集候选会漏掉「现行在 t60 就成交、候选拒判后监听段本可能补上」的窗（Δ 偏保守）。
    """
    return {r["slug"]: r for r in rows if r["stage"] == "listen" and r.get("ok")}


# ── §1 现象 ───────────────────────────────────────────────────────────────

def sec1(ev, hr, live_rows):
    print("\n" + "=" * 100)
    print("§1  现象：σ（1σ 折美元）的周内结构")
    print("=" * 100)
    byday = collections.defaultdict(list)
    for e in ev:
        h = hr.get(e["start_time"])
        if h is None:
            continue
        d = datetime.datetime.fromtimestamp(
            e["start_time"], datetime.timezone.utc).strftime("%Y-%m-%d")
        byday[d].append(h)
    print("\n  [14 天回测] 逐日 σ 中位（08-22/23 与 08-29/30 是周末）")
    for d in sorted(byday):
        v = sorted(byday[d])[len(byday[d]) // 2]
        wd = datetime.date.fromisoformat(d).strftime("%a")
        print(f"    {d} {wd}  σ中位 {v:7.2f} 美元   窗数 {len(byday[d]):4d}   "
              + "█" * int(v / 4))
    print("\n  [实盘 tail-live] 逐日 σ 与信号数")
    for d in sorted({r["date"] for r in live_rows}):
        rs = [r for r in live_rows if r["date"] == d]
        sig = [r for r in rs if r.get("ok")]
        sds = sorted(r["sd"] for r in sig if r.get("sd"))
        med = sds[len(sds) // 2] if sds else 0
        print(f"    {d}  σ中位 {med:7.2f} 美元   判定行 {len(rs):4d}   信号 {len(sig):4d}")


# ── §2 现行 dev_min(sd) ───────────────────────────────────────────────────

def sec2():
    print("\n" + "=" * 100)
    print("§2  现行规则实际要求的位移门槛 dev_min(sd) 与它的断点")
    print("=" * 100)
    print("\n    sd(美元)   现行 dev_min   需要的 z     梯度族 dev_min   需要的 z")
    for sd in (15, 20, 25, 30, 35, 39.9, 40, 45, 50, 60, 62.9, 63, 80, 120):
        cur = DEV_USD if sd < SD_MIN_USD else (sd if sd < DEV_USD else DEV_USD)
        grad = min(DEV_USD, max(GRAD_Z * sd, GRAD_FLOOR))
        tail = "   ← 现行断点：sd<40 跳回固定 63" if abs(sd - 39.9) < 0.2 else ""
        print(f"    {sd:8.1f}   {cur:10.1f}   {cur/sd:8.2f}σ    {grad:12.1f}   {grad/sd:8.2f}σ{tail}")


# ── §3 链级重跑 ───────────────────────────────────────────────────────────

def sec3(ev, hr, lvjud, listen_lv, settled, win):
    print("\n" + "=" * 100)
    print("§3  链级重跑（必须重跑三段链：放宽阈值会改变「哪个 tick 首次达标」）")
    print("=" * 100)
    bt, full = run_backtest(ev, hr, R5_CUR)
    listen_bt = listen_of(full)
    line("现行（完整三段链）", full)
    assert len(full) == 2133, f"基线对账失败：n={len(full)}（应为 2133）"
    assert abs(pl(full) - 35.092747766846315) < 1e-9, f"基线对账失败：P&L={pl(full)!r}"
    print("  ✓ 基线逐位对账 oracle 23：n=2133  P&L=+35.092747766846315U")

    print("\n  [3.2 全链宇宙 —— 两样本同口径（都含监听段）]")
    CAND = [("现行", current_r5),
            (LOWSD_LABEL, make_r5_lowsd(LOWSD_Z, LOWSD_FLOOR, LOWSD_SD)),
            (LOWSD_LABEL + " ∧hot≥0.95", make_r5_lowsd(LOWSD_Z, LOWSD_FLOOR, LOWSD_SD, P_HI)),
            (f"纯 {GRAD_Z}σ∧{GRAD_FLOOR:.0f}", make_r5(GRAD_Z, GRAD_FLOOR, 0.0)),
            (f"{GRAD_Z}σ∧{GRAD_FLOOR:.0f} ∧ hot≥0.90", make_r5(GRAD_Z, GRAD_FLOOR, 0.90)),
            (f"{GRAD_Z}σ∧{GRAD_FLOOR:.0f} ∧ hot≥0.95", make_r5(GRAD_Z, GRAD_FLOOR, P_HI)),
            (f"{GRAD_Z}σ∧{GRAD_FLOOR:.0f} ∧ hot≥0.97", make_r5(GRAD_Z, GRAD_FLOOR, 0.97)),
            ("1.25σ∧25 ∧ hot≥0.97", make_r5(1.25, 25.0, 0.97)),
            ("1.5σ∧25 ∧ hot≥0.97", make_r5(1.5, 25.0, 0.97))]
    res = {}
    print(f"\n  {'候选':26s} {'样本':4s} {'n':>6s} {'胜率':>8s} {'均价':>8s} {'EV/注':>9s} {'P&L':>9s}")
    for lab, leg in CAND:
        b = judge(bt, listen_bt, {}, {}, leg, False)
        l = judge(lvjud, listen_lv, settled, win, leg, True)
        res[lab] = b + l
        for nm, v in (("回测", b), ("实盘", l), ("合并", b + l)):
            print(f"  {lab:26s} {nm:4s} {len(v):6d} {wr(v)*100:7.2f}% {avg_fill(v):8.4f} "
                  f"{pl(v)/len(v):+9.4f} {pl(v):+9.2f}")
        print()
    base = res["现行"]
    base_bt = judge(bt, listen_bt, {}, {}, current_r5, False)
    base_lv = judge(lvjud, listen_lv, settled, win, current_r5, True)
    assert (len(base_bt), round(pl(base_bt), 6)) == (len(full), round(pl(full), 6)), (
        f"judge 路径自检失败：n={len(base_bt)} vs {len(full)}、"
        f"P&L={pl(base_bt)!r} vs {pl(full)!r}（监听段补齐或段序与 chain 不一致）")
    print(f"  ✓ judge 路径自检：重判判定行 + 基线监听行 ⇒ 逐位复现完整链"
          f"（n={len(base_bt)}  P&L={pl(base_bt):+.6f}U）")
    print("  日级配对 bootstrap（2000 次, seed 42）—— ΔP&L = 候选 − 现行")
    for lab, _ in CAND[1:]:
        lo, hi = boot_delta(res[lab], base)
        flag = "  含 0" if lo <= 0 <= hi else "  ★不含 0"
        lost = ({r["slug"] for r in base_bt} - {r["slug"] for r in res[lab] if r["src"] == "回测"}) | \
               ({r["slug"] for r in base_lv} - {r["slug"] for r in res[lab] if r["src"] == "实盘"})
        print(f"    {lab:26s} Δ{pl(res[lab])-pl(base):+7.2f}U   95% 区间 [{lo:+.2f}, {hi:+.2f}]{flag}"
              + (f"   ⚠️ 丢 {len(lost)} 个现行窗（非超集 ⇒ Δ 偏乐观）" if lost else ""))
    sec3b(res, base, base_bt, base_lv)
    return {"bt": bt, "listen_bt": listen_bt, "base": base,
            "base_bt": base_bt, "base_lv": base_lv, "res": res}


def sec3b(res, base, base_bt, base_lv):
    """用户指定候选（sd<30 用梯度、其余现行、价格腿不动）的专项体检。"""
    print("\n" + "-" * 100)
    print(f"[3.3] 专项：{LOWSD_LABEL}（价格腿一字不动，T=150 仍严格 > 0.80）")
    print("-" * 100)
    cand = res[LOWSD_LABEL]
    bkey = {r["slug"] + "|" + r["src"] for r in base}
    ckey = {r["slug"] + "|" + r["src"]: r for r in cand}
    inc = [r for r in cand if r["slug"] + "|" + r["src"] not in bkey]
    lost_bt = {r["slug"] for r in base_bt} - {r["slug"] for r in cand if r["src"] == "回测"}
    lost_lv = {r["slug"] for r in base_lv} - {r["slug"] for r in cand if r["src"] == "实盘"}

    print(f"\n  ① 只增不删自检：现行 n={len(base)} → 候选 n={len(cand)}（新增 {len(inc)} 笔）；"
          + (f"丢失 回测 {len(lost_bt)} / 实盘 {len(lost_lv)} 个现行窗  ⚠️ 非超集"
             if (lost_bt or lost_lv) else
             f"丢失 0 笔  ✓ 严格超集（梯度门槛在 sd<{LOWSD_SD:.0f} 上恒 < 63）"))
    print("\n  ② 分样本对照（亏损日 = 当日 P&L < 0 的天数）：")
    for src in ("回测", "实盘"):
        a = [r for r in base if r["src"] == src]
        c = [r for r in cand if r["src"] == src]
        if not a and not c:
            continue
        na = sum(1 for v in per_day(a).values() if v < 0)
        nc = sum(1 for v in per_day(c).values() if v < 0)
        print(f"     {src}：现行 n={len(a):5d} 胜率 {wr(a)*100:6.2f}% 均价 {avg_fill(a):.4f} "
              f"{pl(a):+7.2f}U（亏损日 {na}）  →  候选 n={len(c):5d} 胜率 {wr(c)*100:6.2f}% "
              f"均价 {avg_fill(c):.4f} {pl(c):+7.2f}U（亏损日 {nc}）  Δ{pl(c)-pl(a):+.2f}U")

    print(f"\n  ③ 边际族（现行没有的 {len(inc)} 笔）—— 全部落在 sd<{LOWSD_SD:.0f}，这是候选唯一的改动面：")
    for src in ("回测", "实盘", "合并"):
        s = [r for r in inc if src == "合并" or r["src"] == src]
        if not s:
            print(f"     {src:4s} 0 笔")
            continue
        k, n = sum(r["won"] for r in s), len(s)
        lo95 = wilson_lo(k, n)
        print(f"     {src:4s} n={n:4d} 赢 {k:3d} 胜率 {k/n*100:6.2f}% 均价 {avg_fill(s):.4f}"
              f"（平衡线 {avg_fill(s)*100:5.2f}%） P&L {pl(s):+6.2f}U   Wilson95%下界 {lo95*100:6.2f}%"
              + ("  ✓ 下界 > 平衡线" if lo95 > avg_fill(s) else "  ✗ 下界 < 平衡线（不显著）"))
    print("\n  ④ 边际族的 z / 成交价结构：")
    for l, f in (("z<1.25", lambda r: r["sig"] is not None and r["sig"] < 1.25),
                 ("1.25≤z<2", lambda r: r["sig"] is not None and 1.25 <= r["sig"] < 2),
                 ("z≥2", lambda r: r["sig"] is not None and r["sig"] >= 2)):
        s = [r for r in inc if f(r)]
        if s:
            print(f"     {l:10s} n={len(s):3d} 胜率 {wr(s)*100:6.2f}% P&L {pl(s):+6.2f}U")
    for l, f in (("fill<0.90", lambda r: r["fill"] < 0.90),
                 ("0.90≤fill<0.97", lambda r: 0.90 <= r["fill"] < 0.97),
                 ("fill≥0.97", lambda r: r["fill"] >= 0.97)):
        s = [r for r in inc if f(r)]
        if s:
            print(f"     {l:14s} n={len(s):3d} 胜率 {wr(s)*100:6.2f}% P&L {pl(s):+6.2f}U")
    print("\n  ⑤ 边际族逐条（实盘行的胜负来自 tailwin 推导 —— 引擎当时拒判、未下单）：")
    if not inc:
        print("     （无）")
    for r in sorted(inc, key=lambda x: (x["src"], x["date"], x["stage"])):
        zs = f"{r['sig']:.2f}" if r["sig"] is not None else " -- "
        print(f"     {r['src']} {r['date']} {r['stage']:6s} {r['side']:3s} z={zs} "
              f"dev{r['dev']:+7.1f} σ{r['sd']:5.1f} @{r['fill']:.2f}  "
              f"{'赢' if r['won'] else '输'}（{r['how']}）  {pl([r]):+.4f}U")
    print("\n  ⑥ 逐日信号数与当日 P&L（现行 → 候选）：")
    d0 = collections.Counter(r["date"] for r in base)
    d1 = collections.Counter(r["date"] for r in cand)
    p0, p1 = per_day(base), per_day(cand)
    for d in sorted(set(d0) | set(d1)):
        print(f"     {d}  {d0[d]:4d} → {d1[d]:4d}   ({p0[d]:+7.1f}U → {p1[d]:+7.1f}U)"
              + ("  ◀ 新增" if d1[d] > d0[d] else ""))
    print("\n  ⑦ 共有窗的入场位移（放宽阈值让某些窗在更早的段入场，成交价随之变）：")
    moved = []
    for r in base:
        c = ckey.get(r["slug"] + "|" + r["src"])
        if c is not None and (r["stage"], round(r["fill"], 4)) != (c["stage"], round(c["fill"], 4)):
            moved.append((r, c))
    if not moved:
        print("     0 个（所有共有窗的段与成交价都不变）")
    else:
        print(f"     {len(moved)} 个窗换段/换价，合计 {sum(pl([c]) - pl([r]) for r, c in moved):+.2f}U"
              "（已含在总 Δ 里，故 ③ 边际族之和 ≠ 总 Δ）")
        for r, c in sorted(moved, key=lambda x: (x[0]["src"], x[0]["date"]))[:40]:
            zs = f"{r['sig']:.2f}" if r["sig"] is not None else " -- "
            print(f"       {r['src']} {r['date']} {r['stage']:6s}@{r['fill']:.2f} → "
                  f"{c['stage']:6s}@{c['fill']:.2f}  z={zs} dev{r['dev']:+7.1f} σ{r['sd']:5.1f}  "
                  f"{pl([c])-pl([r]):+.4f}U")
        if len(moved) > 40:
            print(f"       …（另 {len(moved)-40} 条略）")
    alt = LOWSD_LABEL + " ∧hot≥0.95"
    if alt in res:
        c2 = res[alt]
        inc2 = [r for r in c2 if r["slug"] + "|" + r["src"] not in bkey]
        print(f"\n  ⑧ 附加对照（**非**用户要求的那一版，只作参照）：{alt}")
        for src in ("回测", "实盘", "合并"):
            s = [r for r in inc2 if src == "合并" or r["src"] == src]
            if not s:
                print(f"     {src:4s} 0 笔")
                continue
            k, n = sum(r["won"] for r in s), len(s)
            lo95 = wilson_lo(k, n)
            print(f"     {src:4s} n={n:4d} 赢 {k:3d} 胜率 {k/n*100:6.2f}% 均价 {avg_fill(s):.4f}"
                  f"（平衡线 {avg_fill(s)*100:5.2f}%） P&L {pl(s):+6.2f}U   Wilson95%下界 {lo95*100:6.2f}%"
                  + ("  ✓ 下界 > 平衡线" if lo95 > avg_fill(s) else "  ✗ 下界 < 平衡线（不显著）"))
        print(f"     ⇒ 叠价格闸后的合并 Δ{pl(c2)-pl(base):+.2f}U（边际族缩到 {len(inc2)} 笔）"
              "——『sd<30 放宽』与『hot≥0.95 闸』是两件事，后者已在 §5 单独登记。")


# ── §4 边际族解剖 ─────────────────────────────────────────────────────────

def sec4(res, base):
    print("\n" + "=" * 100)
    print("§4  边际族解剖：放宽到底加进来一批什么样的信号")
    print("=" * 100)
    bslug = {r["slug"] + "|" + r["src"] for r in base}
    for lab in (f"纯 {GRAD_Z}σ∧{GRAD_FLOOR:.0f}", f"{GRAD_Z}σ∧{GRAD_FLOOR:.0f} ∧ hot≥{P_HI}"):
        inc = [r for r in res[lab] if r["slug"] + "|" + r["src"] not in bslug]
        print(f"\n  [{lab}] 边际族 n={len(inc)}  胜率 {wr(inc)*100:.2f}%  "
              f"均价 {avg_fill(inc):.4f}（平衡线 {avg_fill(inc)*100:.1f}%）  {pl(inc):+.2f}U")
        print("    分样本: " + "   ".join(
            f"{s} n={sum(1 for r in inc if r['src'] == s)}"
            f"/{sum(r['won'] for r in inc if r['src'] == s)}胜"
            f"/均价 {avg_fill([r for r in inc if r['src'] == s]):.4f}"
            f"（平衡线 {avg_fill([r for r in inc if r['src'] == s])*100:.1f}%）"
            f"/{pl([r for r in inc if r['src'] == s]):+.2f}U" for s in ("回测", "实盘")))
        print("    z 结构（亏损是否在低 z？）: " + "   ".join(
            f"{l} n={sum(1 for r in inc if f(r))}"
            f"/{sum(r['won'] for r in inc if f(r))}胜"
            f"/{pl([r for r in inc if f(r)]):+.2f}U"
            for l, f in (("z<1.25", lambda r: r["sig"] < 1.25),
                         ("1.25≤z<2", lambda r: 1.25 <= r["sig"] < 2),
                         ("z≥2", lambda r: r["sig"] >= 2))))
        print("    成交价结构: " + "   ".join(
            f"{l} n={sum(1 for r in inc if f(r))} 胜率"
            f"{sum(r['won'] for r in inc if f(r))/max(1, sum(1 for r in inc if f(r)))*100:.1f}%"
            f"/{pl([r for r in inc if f(r)]):+.2f}U"
            for l, f in (("fill<0.90", lambda r: r["fill"] < 0.90),
                         ("0.90≤fill<0.97", lambda r: 0.90 <= r["fill"] < 0.97),
                         ("fill≥0.97", lambda r: r["fill"] >= 0.97))))
        bad = sorted([x for x in inc if not x["won"]], key=lambda x: x["date"])
        print(f"    边际族里的亏损行（{len(bad)} 条）:")
        for r in bad:
            print(f"      {r['src']} {r['date']} {r['stage']:6s} {r['side']:3s} "
                  f"z={r['sig']:.2f} dev{r['dev']:+7.1f} σ{r['sd']:5.1f} @{r['fill']:.2f}")
    print("\n  ⚠️ 对照组：**基线族自身**按成交价分桶（同样结构 ⇒ 成交价是策略的普遍风险轴，")
    print("     不是放宽腿特有的）")
    for l, f in (("fill<0.90", lambda r: r["fill"] < 0.90),
                 ("0.90≤fill<0.97", lambda r: 0.90 <= r["fill"] < 0.97),
                 ("fill≥0.97", lambda r: r["fill"] >= 0.97)):
        s = [r for r in base if f(r)]
        print(f"    {l:14s} n={len(s):4d} 胜率 {wr(s)*100:6.2f}% 均价 {avg_fill(s):.4f}"
              f"（平衡线 {avg_fill(s)*100:.1f}%） {pl(s):+7.2f}U")


# ── §5 价格条件的梯度（推荐族）────────────────────────────────────────────

def sec5(ctx, lvjud, listen_lv, settled, win):
    bt, listen_bt, base = ctx["bt"], ctx["listen_bt"], ctx["base"]
    print("\n" + "=" * 100)
    print(f"§5  推荐族：梯度只在热门侧已 ≥ {P_HI} 时启用")
    print("=" * 100)
    bslug = {r["slug"] + "|" + r["src"] for r in base}
    grid = [0.90, 0.91, 0.92, 0.93, 0.94, 0.95, 0.96, 0.97, 0.98, 0.99]
    cache = {}
    print(f"\n  {'价格闸 P_hi':14s} {'合并 n':>7s} {'胜率':>8s} {'均价':>8s} {'P&L':>9s}"
          f"  {'边际 n':>7s} {'边际胜率':>9s} {'边际P&L':>9s}  {'ΔP&L':>8s}  95%区间")
    print(f"  {'':14s} {'':>7s} {'':>8s} {'':>8s} {'':>9s}"
          f"  {'':>7s} {'胜率下界':>9s} {'平衡线':>9s}  {'':>8s}")
    for ph in grid:
        leg = make_r5(GRAD_Z, GRAD_FLOOR, ph)
        rs = judge(bt, listen_bt, {}, {}, leg, False) + judge(lvjud, listen_lv, settled, win, leg, True)
        cache[ph] = rs
        inc = [r for r in rs if r["slug"] + "|" + r["src"] not in bslug]
        lo, hi = boot_delta(rs, base)
        k, n = sum(r["won"] for r in inc), len(inc)
        lo95 = wilson_lo(k, n)
        print(f"  hot≥{ph:<10.2f} {len(rs):7d} {wr(rs)*100:7.2f}% {avg_fill(rs):8.4f} {pl(rs):+9.2f}"
              f"  {n:7d} {wr(inc)*100:8.2f}% {pl(inc):+9.2f}  {pl(rs)-pl(base):+8.2f}"
              f"  [{lo:+.2f}, {hi:+.2f}]" + ("" if lo > 0 or hi < 0 else " 含0")
              + f"\n  {'':14s} {'':>7s} {'':>8s} {'':>8s} {'':>9s}  {'':>7s} "
              + f"{lo95*100:8.2f}% {avg_fill(inc)*100:8.2f}%  "
              + ("下界 > 平衡线 ✓" if lo95 > avg_fill(inc) else "下界 < 平衡线 ✗（不显著）"))
    print("  ⚠️ 「胜率下界」= 边际族胜率的 Wilson 95% 下界；「平衡线」= 该族均价。"
          "**下界必须大于平衡线**才算")
    print("     「在保证胜率的前提下」成立。n≈190 而超额只有 ~1 个百分点 ⇒ 这个不等式过不去，")
    print("     无论闸取 0.95 还是 0.97。见报告 §7.1。")
    best = max(grid, key=lambda ph: pl(cache[ph]))
    rank = sorted((pl(cache[ph]) for ph in grid), reverse=True).index(pl(cache[P_HI])) + 1
    print(f"\n  ⚠️ 全样本 argmax = {best:.2f}（{pl(cache[best]):+.2f}U），"
          f"推荐值 {P_HI:.2f} 排第 {rank}/{len(grid)}；曲线在 0.90~0.99 上近乎平坦")
    print("     ⇒ **该阈值不可辨识，禁止据回测调它**（与 σ 腿的 `40` 同款处境：")
    print("       真 argmax 落在网格边缘、重采样里挑不出来，见 §6.2 留一天检验）")
    rec = cache[P_HI]
    inc = [r for r in rec if r["slug"] + "|" + r["src"] not in bslug]
    print("\n  低σ恢复量（推荐族边际族按 σ 分桶）—— 恢复应集中在低σ日")
    for l, f in (("σ<25", lambda r: r["sd"] < 25), ("25≤σ<35", lambda r: 25 <= r["sd"] < 35),
                 ("35≤σ<45", lambda r: 35 <= r["sd"] < 45), ("σ≥45", lambda r: r["sd"] >= 45)):
        s = [r for r in inc if f(r)]
        if s:
            print(f"    {l:10s} n={len(s):3d} 胜率 {wr(s)*100:6.2f}% 均价 {avg_fill(s):.4f}"
                  f"（平衡线 {avg_fill(s)*100:.1f}%） {pl(s):+6.2f}U")
    print("\n  逐日信号数（现行 → 推荐）：")
    d0 = collections.Counter(r["date"] for r in base)
    d1 = collections.Counter(r["date"] for r in rec)
    p0, p1 = per_day(base), per_day(rec)
    for d in sorted(set(d0) | set(d1)):
        mark = "  ◀ 低σ日" if d1[d] - d0[d] >= 10 else ""
        print(f"    {d}  {d0[d]:4d} → {d1[d]:4d}   ({p0[d]:+7.1f}U → {p1[d]:+7.1f}U){mark}")
    print(f"\n  ⚠️ 边际族的超额有多薄（平衡线 = 成交价：赢一笔只赚 2·(1−p)/p，输一笔亏 2U）：")
    for src in ("回测", "实盘", "合并"):
        s = [r for r in inc if src == "合并" or r["src"] == src]
        if not s:
            continue
        k = sum(r["won"] for r in s)
        beh = len(s) * avg_fill(s)
        print(f"    {src}: n={len(s)} 赢 {k} 笔，平衡线要求 {beh:.1f} 笔 ⇒ 超额 {k-beh:+.1f} 笔"
              f"（每输一笔要 {2/max(1e-9, 2/avg_fill(s)-2):.0f} 个赢家才补得回来）")
    hows = collections.Counter(r["how"] for r in inc)
    print(f"    胜负来源: {dict(hows)}"
          + ("   ⚠️ 实盘部分全是 tailwin 推导（引擎当时未下单、无官方 won）"
             if any(k != "回测" for k in hows) else ""))
    print("    敏感性: 实盘边际族若**翻转任意一笔**为输 ⇒ "
          f"该族 P&L 从 {pl([r for r in inc if r['src'] == '实盘']):+.2f}U 掉到 "
          f"{pl([r for r in inc if r['src'] == '实盘'])-2-STAKE/0.984+STAKE:+.2f}U 量级")
    print("\n  ── 价格闸细分：0.95 → 0.97 去掉的是哪一段 ──")
    inc95 = [r for r in cache[0.95] if r["slug"] + "|" + r["src"] not in bslug]
    inc97 = [r for r in cache[0.97] if r["slug"] + "|" + r["src"] not in bslug]
    k95 = {r["slug"] + "|" + r["src"] for r in inc95}
    k97 = {r["slug"] + "|" + r["src"] for r in inc97}
    dropped = [r for r in inc95 if r["slug"] + "|" + r["src"] not in k97]
    added = [r for r in inc97 if r["slug"] + "|" + r["src"] not in k95]
    print(f"    边际族: hot≥0.95 n={len(inc95)} WR {wr(inc95)*100:.2f}% "
          f"均价 {avg_fill(inc95):.4f}（平衡线 {avg_fill(inc95)*100:.1f}%） {pl(inc95):+.2f}U"
          f"   →   hot≥0.97 n={len(inc97)} WR {wr(inc97)*100:.2f}% "
          f"均价 {avg_fill(inc97):.4f}（平衡线 {avg_fill(inc97)*100:.1f}%） {pl(inc97):+.2f}U")
    print(f"    0.97 相对 0.95 少掉的 {len(dropped)} 笔（fill ∈ [0.95,0.97)）："
          f"赢 {sum(r['won'] for r in dropped)} P&L {pl(dropped):+.2f}U；"
          f"多出的 {len(added)} 笔 P&L {pl(added):+.2f}U")
    for r in sorted(dropped, key=lambda x: (x["src"], x["date"])):
        print(f"      {r['src']} {r['date']} {r['stage']:6s} {r['side']:3s} z={r['sig']:.2f} "
              f"@{r['fill']:.2f}  {'赢' if r['won'] else '输'}（{r['how']}）")
    print("    ⇒ 与决策 #26（0.80 是价格梯度上唯一负 EV 档）同一套逻辑：如果这一段在"
          "两样本里都是负贡献，")
    print("      那么『把闸抬到 0.97』与该决策同源——但同样**不可辨识**（§6.2 显示 argmax "
          "贴网格下边缘），")
    print("      且边际族会从 196 笔缩到 188 笔 ⇒ 更薄。**纸面登记、不改引擎。**")

    print("\n  边际族逐条（实盘部分必看：引擎当时从未下单的窗，胜负来自 tailwin 推导）：")
    for r in sorted([x for x in inc if x["src"] == "实盘"], key=lambda x: x["date"]):
        print(f"    {r['date']} {r['stage']:6s} {r['side']:3s} z={r['sig']:.2f} "
              f"dev{r['dev']:+7.1f} σ{r['sd']:5.1f} @{r['fill']:.2f}  "
              f"{'赢' if r['won'] else '输'}（{r['how']}）")
    return cache


# ── §6 稳健性 ─────────────────────────────────────────────────────────────

def sec6(ctx, cache):
    print("\n" + "=" * 100)
    print("§6  稳健性")
    print("=" * 100)
    base = ctx["base"]
    rec = cache[P_HI]
    print("\n  [6.1 多种子] 日级配对区间不随种子漂移")
    for seed in (1, 7, 42, 99, 2026):
        lo, hi = boot_delta(rec, base, seed=seed)
        print(f"    seed={seed:5d}  Δ{pl(rec)-pl(base):+6.2f}U   95% 区间 [{lo:+.2f}, {hi:+.2f}]")
    print("\n  [6.2 留一天 argmax] 逐日剔除后重挑价格闸，看推荐值是否被某一天绑架")
    days = sorted({r["date"] for r in base})
    cnt = collections.Counter()
    for d in days:
        cnt[max(cache, key=lambda ph: pl([r for r in cache[ph] if r["date"] != d]))] += 1
    print(f"    argmax 分布（{len(days)} 天）: {dict(sorted(cnt.items()))}")
    print("    ⚠️ 读法：argmax 稳定落在 **0.90~0.91 = 网格最低端**，而价格闸越松整个族越"
          "接近纯梯度")
    print("       （P_hi→0 无闸 = +59.85U，比网格上任一点都高）⇒ **闸在回测里没有内部最优**，")
    print("       曲线随闸收紧单调下滑（±1U 噪声），argmax 贴边不是「找到了最优阈值」。")
    print(f"       ⇒ 选 {P_HI} 的**唯一依据是实盘子样本**（纯梯度实盘 −3.05U → 加闸后 "
          f"+0.41U，见 §4）；")
    print("         代价是明确放弃约 5.2U 回测 P&L（+59.85 → +54.64）。这是取舍，不是调参。")
    print("\n  [6.3 纯增量自检] 推荐族应不删任何现行信号（梯度腿是现行 ⑤ 的超集）")
    bs = {r["slug"] + "|" + r["src"]: r for r in base}
    rs = {r["slug"] + "|" + r["src"]: r for r in rec}
    print(f"    现行有、推荐没有的窗: {len(set(bs) - set(rs))}（应为 0）；"
          f"净增 {len(set(rs) - set(bs))} 笔 = {len(rec)} − {len(base)}")
    moved = [(bs[k], rs[k]) for k in set(bs) & set(rs)
             if (bs[k]["stage"], round(bs[k]["fill"], 4)) != (rs[k]["stage"], round(rs[k]["fill"], 4))]
    print(f"    ⚠️ 「纯增量」只对**窗集合**成立：另有 {len(moved)} 个共有的窗换了入场 tick"
          "（t60→t150 提前，成交价随之变）")
    if moved:
        d = sum(pl([b]) - pl([a]) for a, b in moved)
        print(f"       这 {len(moved)} 个窗的 P&L 变化合计 {d:+.2f}U"
              f"（影响已含在总 Δ 里，故 §4 的「边际族」之和 ≠ 总 Δ）")
    print("\n  [6.4 分半] 两个样本各自前后半（回测按日期中点、实盘按日）")
    for src in ("回测", "实盘"):
        sub = sorted([r for r in rec if r["src"] == src], key=lambda r: r["date"])
        bsub = sorted([r for r in base if r["src"] == src], key=lambda r: r["date"])
        if not sub:
            continue
        half = len(sub) // 2
        dmid = sub[half]["date"]
        for nm, f in (("前半", lambda r: r["date"] < dmid), ("后半", lambda r: r["date"] >= dmid)):
            line(f"    {src} {nm} 现行", [r for r in bsub if f(r)], width=18)
            line(f"    {src} {nm} 推荐", [r for r in sub if f(r)], width=18)


# ── §9 sd<30 的固定阈值扫描 ───────────────────────────────────────────────
# （报告 §7/§8 是散文「风险/处置」，故脚本编号直接跳到 §9 与报告对齐）

FIX_GRID = [60.0, 55.0, 50.0, 45.0, 42.5, 40.0, 37.5, 35.0, 32.5, 30.0,
            27.5, 25.0, 22.5, 20.0, 17.5, 15.0]     # 降序：每个 X 的「宽带」= 与下一个更严值之差

FIX_SD_BELOW = 30.0        # 【用户要求】固定阈值只用于 sd < 30
FIX_LABEL = "sd<30 固定阈值"


def _fix_scan(bt, listen_bt, base, base_bt, base_lv, lvjud, listen_lv, settled, win,
              sd_below, p_hi, grid):
    """对每个 X 重跑三段链 → {X: (合并信号行, 边际族)}。"""
    bkey = {r["slug"] + "|" + r["src"] for r in base}
    cache, inc = {}, {}
    for x in grid:
        leg = make_r5_lowsd_fixed(x, sd_below, p_hi)
        v = judge(bt, listen_bt, {}, {}, leg, False) + \
            judge(lvjud, listen_lv, settled, win, leg, True)
        cache[x] = v
        inc[x] = [r for r in v if r["slug"] + "|" + r["src"] not in bkey]
    lost = {}
    for x in grid:
        lost[x] = (len({r["slug"] for r in base_bt} - {r["slug"] for r in cache[x] if r["src"] == "回测"}),
                   len({r["slug"] for r in base_lv} - {r["slug"] for r in cache[x] if r["src"] == "实盘"}))
    return cache, inc, lost


def _sub(rows, src):
    return rows if src == "合并" else [r for r in rows if r["src"] == src]


def sec9(ev, hr, ctx, lvjud, listen_lv, settled, win):
    bt, listen_bt, base = ctx["bt"], ctx["listen_bt"], ctx["base"]
    base_bt, base_lv = ctx["base_bt"], ctx["base_lv"]
    print("\n" + "=" * 100)
    print(f"§9  {FIX_LABEL}扫描：sd < {FIX_SD_BELOW:.0f} 时 `dev ≥ X`（常数，美元），其余走现行")
    print("=" * 100)
    print("    动机：σ 梯度（§3.3/§5.4）两样本方向相反、且 z 结构显示亏损不在低 z ⇒")
    print("          换一个问题——**在 sd<30 这一档里，有没有一个像 63/40 那样的固定数**。")
    print("    价格腿一字不动（T=150 仍严格 > 0.80）。X=63 即基线（现行在 sd<40 上就是 dev≥63）。")

    bkey = {r["slug"] + "|" + r["src"] for r in base}
    out = {}
    for tag, p_hi in (("A 无价格闸", 0.0), (f"B 叠已登记的价格闸 hot≥{P_HI}", P_HI)):
        cache, inc, lost = _fix_scan(bt, listen_bt, base, base_bt, base_lv,
                                     lvjud, listen_lv, settled, win,
                                     FIX_SD_BELOW, p_hi, FIX_GRID)
        out[tag] = (cache, inc, lost)
        print("\n" + "-" * 100)
        print(f"[9.x {tag}]  sd < {FIX_SD_BELOW:.0f} ⇒ dev ≥ X")
        print("-" * 100)
        print(f"\n  ① 总览（现行 = 基线：n={len(base)}  {pl(base):+.2f}U）")
        print(f"  {'X(美元)':>8s} | {'回测 n':>6s} {'P&L':>9s} {'亏日':>5s} | "
              f"{'实盘 n':>6s} {'P&L':>9s} {'亏日':>5s} | {'合并 n':>6s} {'胜率':>7s} "
              f"{'均价':>7s} {'P&L':>8s} | {'ΔP&L':>8s}  {'配对 95% 区间':>20s}")
        for x in FIX_GRID:
            v = cache[x]
            bb, ll = _sub(v, "回测"), _sub(v, "实盘")
            db, dl = per_day(bb), per_day(ll)
            lo, hi = boot_delta(v, base)
            flag = ("  含0" if lo <= 0 <= hi else "  ★")
            lw = f"  ⚠️丢{lost[x][0]}/{lost[x][1]}" if any(lost[x]) else ""
            print(f"  {x:8.1f} | {len(bb):6d} {pl(bb):+9.2f} {sum(1 for z in db.values() if z<0):3d}/"
                  f"{len(db):<2d} | {len(ll):6d} {pl(ll):+9.2f} "
                  f"{sum(1 for z in dl.values() if z<0):3d}/{len(dl):<2d} | "
                  f"{len(v):6d} {wr(v)*100:6.2f}% {avg_fill(v):7.4f} {pl(v):+8.2f} | "
                  f"{pl(v)-pl(base):+8.2f}  [{lo:+7.2f}, {hi:+7.2f}]{flag}{lw}")
        print(f"\n  ② 边际族（新增的那些笔）——「保证胜率」检验 = 胜率 Wilson 95% 下界 > 平衡线")
        print(f"  {'X(美元)':>8s} {'边际 n':>7s} {'胜率':>8s} {'均价(平衡线)':>14s} "
              f"{'P&L':>9s} {'EV/注':>8s} | {'回测 n/P&L':>16s} | {'实盘 n/P&L':>16s} | "
              f"{'胜率下界':>9s} {'判定':>6s}")
        for x in FIX_GRID:
            s = inc[x]
            if not s:
                print(f"  {x:8.1f} {'—':>7s}   （X=63 与现行逐位相同）")
                continue
            k, n = sum(r["won"] for r in s), len(s)
            lo95 = wilson_lo(k, n)
            ok = lo95 > avg_fill(s)
            sb, sl = _sub(s, "回测"), _sub(s, "实盘")
            print(f"  {x:8.1f} {n:7d} {wr(s)*100:7.2f}% {avg_fill(s):8.4f}({avg_fill(s)*100:5.1f}%) "
                  f"{pl(s):+9.2f} {pl(s)/n:+8.4f} | {len(sb):5d} {pl(sb):+9.2f} | "
                  f"{len(sl):5d} {pl(sl):+9.2f} | {lo95*100:8.2f}% "
                  + ("  ✓" if ok else "  ✗"))
        print("    ⚠️ 平衡线 = 该族均价（买在 p：赢赚 2(1−p)/p、输亏 2U ⇒ 胜率=p 才不亏）。")
        print("       「✗」= 这个族的胜率在 n 的精度下还不高于它自己的平衡线 —— 即前一版"
              "（§5.4/§6.1）\n       失败的正是这道检验，它不会因为把 σ 换成常数而自动通过。")

        # ③ 逐档增量：从 X 放宽到「下一档更严」时买进来的那一批（集合嵌套 ⇒ 可精确分解）
        print(f"\n  ③ 逐档增量（**从 X 放宽到下一档更严值**时买进来的那一批；集合嵌套 ⇒ 可精确分解）")
        print(f"  {'dev 区间(美元)':>16s} {'n':>5s} {'胜率':>8s} {'均价':>7s} {'P&L':>9s} "
              f"{'EV/注':>8s} | {'回测 n/P&L':>16s} | {'实盘 n/P&L':>16s}")
        prev = None
        for x in FIX_GRID:                       # 降序：X 越小越宽
            s = inc[x] if prev is None else [r for r in inc[x]
                                             if r["slug"] + "|" + r["src"] not in prev]
            prev = {r["slug"] + "|" + r["src"] for r in inc[x]}
            if not s:
                continue
            nxt = [g for g in FIX_GRID if g > x]
            hi_ = min(nxt) if nxt else DEV_USD
            sb, sl = _sub(s, "回测"), _sub(s, "实盘")
            print(f"  [{x:5.1f}, {hi_:5.1f}) {len(s):5d} {wr(s)*100:7.2f}% {avg_fill(s):7.4f} "
                  f"{pl(s):+9.2f} {pl(s)/len(s):+8.4f} | {len(sb):5d} {pl(sb):+9.2f} | "
                  f"{len(sl):5d} {pl(sl):+9.2f}")

        # ④ 可辨识性：这个 X 是不是噪声尖峰（本项目的老毛病：阈值贴噪声峰）
        days = sorted({r["date"] for r in base})
        dmat = {}
        for x in FIX_GRID:
            dv, db = per_day(cache[x]), per_day(base)
            dmat[x] = {d: dv.get(d, 0.0) - db.get(d, 0.0) for d in days}
        rng = random.Random(42)
        cnt = collections.Counter()
        for _ in range(2000):
            pick = [rng.choice(days) for _ in days]
            cnt[max(FIX_GRID, key=lambda x: sum(dmat[x][d] for d in pick))] += 1
        loo = collections.Counter()
        for d in days:
            loo[max(FIX_GRID, key=lambda x: sum(v for k, v in dmat[x].items() if k != d))] += 1
        vals = {x: sum(dmat[x].values()) for x in FIX_GRID}
        print(f"\n  ④ 可辨识性：X 是「平台」还是「噪声尖峰」？")
        print(f"    全样本 argmax = {max(FIX_GRID, key=lambda x: vals[x]):.1f}"
              f"（{vals[max(FIX_GRID, key=lambda x: vals[x])]:+.2f}U）；"
              f"Δ(X) 曲线: " + "  ".join(f"{x:.0f}:{vals[x]:+.1f}" for x in sorted(FIX_GRID)))
        print(f"    留一天重挑 argmax（{len(days)} 天）: {dict(sorted(loo.items()))}")
        print(f"    日级 bootstrap 2000 次 argmax 分布: {dict(sorted(cnt.items()))}")
        top = cnt.most_common(1)[0]
        print(f"    ⇒ argmax 命中率最高 {top[1]/2000*100:.1f}%"
              + ("（**没有任何一刀占优** ⇒ 与 σ 腿的 `40` 同款：该阈值不可辨识）"
                 if top[1] / 2000 < 0.5 else ""))

        # ⑤ 最好那一档的亏损行逐条
        best = max(FIX_GRID, key=lambda x: vals[x])
        bad = sorted([r for r in inc[best] if not r["won"]], key=lambda r: (r["src"], r["date"]))
        print(f"\n  ⑤ X={best:.1f}（本面板 argmax）边际族的亏损行（{len(bad)} 条）")
        for r in bad:
            print(f"      {r['src']} {r['date']} {r['stage']:6s} {r['side']:3s} dev{r['dev']:+7.1f} "
                  f"σ{r['sd']:5.1f} z={r['sig']:.2f} @{r['fill']:.2f}（{r['how']}）")
        loos = min(FIX_GRID)
        print(f"    对照：最宽那一档 X={loos:.1f}（边际族 n={len(inc[loos])} "
              f"胜率 {wr(inc[loos])*100:.2f}%）的全部亏损行 ——")
        print("      「argmax 那一刀好在哪」= 它把下面哪几条排除了：")
        for r in sorted([x for x in inc[loos] if not x["won"]], key=lambda x: (x["src"], x["date"])):
            inbest = r["slug"] + "|" + r["src"] in {y["slug"] + "|" + y["src"] for y in inc[best]}
            print(f"      {'[在]' if inbest else '[排除]'} {r['src']} {r['date']} {r['stage']:6s} "
                  f"{r['side']:3s} dev{r['dev']:+7.1f} σ{r['sd']:5.1f} z={r['sig']:.2f} "
                  f"@{r['fill']:.2f}（{r['how']}）")

    # ⑥ 边界题：断点在 sd=40，只修 sd<30 会留下 [30,40) 这段更严的夹缝
    print("\n" + "-" * 100)
    print("[9.y 边界对照] 同样的固定阈值，推广到整条断点区间 sd < 40（无价格闸）")
    print("-" * 100)
    g2 = [50.0, 45.0, 40.0, 35.0, 30.0, 25.0, 20.0]
    cache40, inc40, lost40 = _fix_scan(bt, listen_bt, base, base_bt, base_lv,
                                       lvjud, listen_lv, settled, win, 40.0, 0.0, g2)
    print(f"  {'X(美元)':>8s} {'合并 n':>7s} {'P&L':>9s} {'ΔP&L':>9s}  {'配对 95% 区间':>20s}"
          f" {'回测Δ':>9s} {'实盘Δ':>9s} | {'边际 n':>7s} {'边际P&L':>9s} {'胜率下界':>9s}")
    for x in g2:
        v = cache40[x]
        lo, hi = boot_delta(v, base)
        s = inc40[x]
        k, n = sum(r["won"] for r in s), len(s)
        db = pl(_sub(v, "回测")) - pl(_sub(base, "回测"))
        dl = pl(_sub(v, "实盘")) - pl(_sub(base, "实盘"))
        print(f"  {x:8.1f} {len(v):7d} {pl(v):+9.2f} {pl(v)-pl(base):+9.2f}  "
              f"[{lo:+7.2f}, {hi:+7.2f}] " + ("含0" if lo <= 0 <= hi else " ★ ")
              + f" {db:+9.2f} {dl:+9.2f} | {n:7d} {pl(s):+9.2f} {wilson_lo(k,n)*100:8.2f}%")
    print("  ⚠️ 读法：只修 sd<30 时，sd∈[30,40) 仍要求 dev ≥ 63 ⇒ 门槛在 sd=30 处从 X 跳回 63。")
    print("     本表把同一个 X 用到整段 [0,40)：全部 7 档两样本方向都相反（回测正、实盘负）⇒ 不可用。")
    print("     原因与 §9 A 面板同源：去掉价格闸之后，sd<40 ∧ dev<63 这一段本身没有正 EV。")

    print("\n" + "-" * 100)
    print("[9.z 阶梯对照] sd < 40 ⇒ dev ≥ X，**叠已登记的价格闸 hot≥0.95**（无闸版见 9.y）")
    print("-" * 100)
    print("    目的：X=40 时门槛曲线变成单调阶梯 —— 40（sd<40）→ sd（40≤sd<63）→ 63（sd≥63），")
    print("          且 40/63 都是现行规则里已有的数，只把断点那一格从 63 换成 40。")
    g3 = [50.0, 45.0, 42.5, 40.0, 37.5, 35.0, 32.5, 30.0, 27.5, 25.0, 22.5, 20.0]
    cache40g, inc40g, lost40g = _fix_scan(bt, listen_bt, base, base_bt, base_lv,
                                          lvjud, listen_lv, settled, win, 40.0, P_HI, g3)
    print(f"  {'X(美元)':>8s} {'合并 n':>7s} {'P&L':>9s} {'ΔP&L':>9s}  {'配对 95% 区间':>20s}"
          f" {'回测Δ':>9s} {'实盘Δ':>9s} | {'边际 n':>7s} {'边际P&L':>9s} {'胜率下界':>9s} {'平衡线':>8s}")
    for x in g3:
        v = cache40g[x]
        lo, hi = boot_delta(v, base)
        s = inc40g[x]
        k, n = sum(r["won"] for r in s), len(s)
        db = pl(_sub(v, "回测")) - pl(_sub(base, "回测"))
        dl = pl(_sub(v, "实盘")) - pl(_sub(base, "实盘"))
        print(f"  {x:8.1f} {len(v):7d} {pl(v):+9.2f} {pl(v)-pl(base):+9.2f}  "
              f"[{lo:+7.2f}, {hi:+7.2f}] " + ("含0" if lo <= 0 <= hi else " ★ ")
              + f" {db:+9.2f} {dl:+9.2f} | {n:7d} {pl(s):+9.2f} {wilson_lo(k,n)*100:8.2f}% "
              f"{avg_fill(s)*100:7.2f}%")
    print("\n  逐档增量（从 X 放宽到下一档更严值时买进来的一批；EV/注 是否稳定决定"
          "「有没有平台」）")
    print(f"  {'dev 区间(美元)':>16s} {'n':>5s} {'胜率':>8s} {'均价':>7s} {'P&L':>8s} {'EV/注':>8s}"
          f" | {'回测 n/P&L':>16s} | {'实盘 n/P&L':>16s}")
    prev = None
    for x in g3:
        s = inc40g[x] if prev is None else [r for r in inc40g[x]
                                            if r["slug"] + "|" + r["src"] not in prev]
        prev = {r["slug"] + "|" + r["src"] for r in inc40g[x]}
        if not s:
            continue
        nxt = [g for g in g3 if g > x]
        hi_ = min(nxt) if nxt else DEV_USD
        sb, sl = _sub(s, "回测"), _sub(s, "实盘")
        print(f"  [{x:5.1f}, {hi_:5.1f}) {len(s):5d} {wr(s)*100:7.2f}% {avg_fill(s):7.4f} "
              f"{pl(s):+8.2f} {pl(s)/len(s):+8.4f} | {len(sb):5d} {pl(sb):+9.2f} | "
              f"{len(sl):5d} {pl(sl):+9.2f}")
    days = sorted({r["date"] for r in base})
    dmat = {x: {d: per_day(cache40g[x]).get(d, 0.0) - per_day(base).get(d, 0.0) for d in days}
            for x in g3}
    loo = collections.Counter()
    for d in days:
        loo[max(g3, key=lambda x: sum(v for k, v in dmat[x].items() if k != d))] += 1
    rng = random.Random(42)
    cnt = collections.Counter()
    for _ in range(2000):
        pick = [rng.choice(days) for _ in days]
        cnt[max(g3, key=lambda x: sum(dmat[x][d] for d in pick))] += 1
    print(f"\n  超集自检（现行有、候选没有的窗数，应为 0/0）：{dict(sorted(lost40g.items()))}")
    print(f"  可辨识性：留一天重挑 argmax（{len(days)} 天）= {dict(sorted(loo.items()))}")
    print(f"            日级 bootstrap 2000 次 argmax 分布 = {dict(sorted(cnt.items()))}")
    print("  ⚠️ 若 argmax 贴在网格**最宽**那一端（X 越小 Δ 越大、且逐档 EV/注 稳定同号），")
    print("     含义是「**门槛越松越好、没有内部最优**」——即这一段的收益来自价格闸本身，")
    print("     dev 门槛只是在做减法；那么就不存在「理想的那个数」，只存在「能放松到哪」。")
    out["y"] = (cache40, inc40, lost40, g2, 40.0, 0.0)
    out["z"] = (cache40g, inc40g, lost40g, g3, 40.0, P_HI)
    return out


# ── §10 回测单样本判定（把实盘从判据里拿掉）───────────────────────────────
# 用户 2026-09-26：「先不以实盘数据作为依据，因为太少了。先在回测数据上看。」
# ⇒ 实盘只作为附注列（样本 4~25 笔，方向翻转由单笔决定），判据全部只吃 14 天回测。

BT_SPLIT = "2026-08-25"     # 14 天分半：前半 08-18~08-24 / 后半 08-25~08-31


def _bt(rows):
    return [r for r in rows if r["src"] == "回测"]


def _epoch(slug):
    """slug（btc-updown-5m-<epoch>）尾部的边界时间戳，用于同日排序。"""
    try:
        return int(slug.rsplit("-", 1)[-1])
    except (ValueError, AttributeError):
        return 0


def _loss_runs(rows):
    """同一 UTC 日内的**连续亏损段长** → {日期: [段长…]}（按时间序，跨日断开）。

    用户 2026-09-26 的验收口径：「只要单日不出现连续亏，就可以接受」——薄赔率策略
    单日连亏两把就是 −4U（stake 2U）/ −20U（stake 10U），远大于单笔赢的 +0.035U。
    """
    per = collections.defaultdict(list)
    for r in sorted(rows, key=lambda r: (r["date"], _epoch(r["slug"]))):
        per[r["date"]].append(not r["won"])
    out = {}
    for d, seq in per.items():
        rs, cur = [], 0
        for lost in seq:
            if lost:
                cur += 1
            elif cur:
                rs.append(cur)
                cur = 0
        if cur:
            rs.append(cur)
        out[d] = rs
    return out


def _runs_line(rows):
    """连亏摘要：最大连亏 / 有 ≥2 连亏的天数 / 明细。"""
    rr = _loss_runs(rows)
    mx = max((max(v) if v else 0) for v in rr.values()) if rr else 0
    bad = {d: v for d, v in rr.items() if v and max(v) >= 2}
    return mx, bad


def _bt_ident(grid, cache, base_bt, reps=2000, seed=42):
    """回测单样本的可辨识性：Δ曲线 / 留一天重挑 / 日级 bootstrap / 前后半对照。

    返回 (Δ曲线, LOO 计数, bootstrap argmax 计数, {半:Δ曲线}, 天数列表)。
    ⚠️ 全部只在**回测日**上重采样——实盘那 3 天不进 days。
    """
    days = sorted({r["date"] for r in base_bt})
    db = per_day(base_bt)
    dmat = {x: {d: per_day(_bt(cache[x])).get(d, 0.0) - db.get(d, 0.0) for d in days}
            for x in grid}
    curve = {x: sum(dmat[x].values()) for x in grid}
    loo = collections.Counter()
    for d in days:
        loo[max(grid, key=lambda x: sum(v for k, v in dmat[x].items() if k != d))] += 1
    rng = random.Random(seed)
    cnt = collections.Counter()
    for _ in range(reps):
        pick = [rng.choice(days) for _ in days]
        cnt[max(grid, key=lambda x: sum(dmat[x][d] for d in pick))] += 1
    halves = {}
    for nm, sel in (("前半", lambda d: d < BT_SPLIT), ("后半", lambda d: d >= BT_SPLIT)):
        ds = [d for d in days if sel(d)]
        halves[nm] = {x: sum(dmat[x][d] for d in ds) for x in grid}
    return curve, loo, cnt, halves, days


def _bt_panel(title, note, grid, cache, inc, base_bt):
    """一个面板的回测单样本判定：总览 / 逐档增量 / 可辨识性 / 胜率检验。"""
    print("\n" + "-" * 100)
    print(f"[{title}]  {note}")
    print("-" * 100)
    n0, p0 = len(base_bt), pl(base_bt)
    dv0 = per_day(base_bt)
    print(f"\n  ① 回测总览（基线 = 现行：n={n0}  {p0:+.2f}U  "
          f"亏损日 {sum(1 for z in dv0.values() if z < 0)}/{len(dv0)}）")
    print(f"  {'X(美元)':>8s} {'n':>6s} {'胜率':>7s} {'均价':>7s} {'P&L':>9s} {'亏损日':>8s} "
          f"{'ΔP&L':>8s}  {'配对 95% 区间（14 天）':>24s}")
    for x in grid:
        v = _bt(cache[x])
        dv = per_day(v)
        lo, hi = boot_delta(v, base_bt)
        print(f"  {x:8.1f} {len(v):6d} {wr(v)*100:6.2f}% {avg_fill(v):7.4f} {pl(v):+9.2f} "
              f"{sum(1 for z in dv.values() if z < 0):4d}/{len(dv):<3d} {pl(v)-p0:+8.2f}  "
              f"[{lo:+8.2f}, {hi:+8.2f}] " + ("含0" if lo <= 0 <= hi else "★"))

    print(f"\n  ② 边际族（只回测）——「保证胜率」检验 = 胜率 Wilson 95% 下界 > 平衡线")
    print(f"  {'X(美元)':>8s} {'边际 n':>7s} {'胜率':>8s} {'均价(平衡线)':>14s} {'P&L':>8s} "
          f"{'EV/注':>8s} {'负笔':>5s} | {'下界':>8s} {'判定':>5s} {'容亏':>5s} {'找平':>5s}")
    for x in grid:
        s = _bt(inc[x])
        if not s:
            print(f"  {x:8.1f} {'—':>7s}   （与现行逐位相同，无新增）")
            continue
        k, n = sum(r["won"] for r in s), len(s)
        lo95 = wilson_lo(k, n)
        pf = avg_fill(s)
        print(f"  {x:8.1f} {n:7d} {wr(s)*100:7.2f}% {pf:8.4f}({pf*100:5.1f}%) "
              f"{pl(s):+8.2f} {pl(s)/n:+8.4f} {n-k:5d} | {lo95*100:7.2f}% "
              + ("  ✓" if lo95 > pf else "  ✗")
              + f" {pl(s)/STAKE:5.1f} {pf/(1-pf):5.1f}")
    print("    ⚠️ 「容亏」= 这个族的累计 P&L 除以单笔亏损 2U = **它还能吃下几笔亏损才归零**。")
    print("       「找平」= 均价 p 时要赢 p/(1−p) 把才抵得上一把输（用户实盘口径 20~40 把"
          "⇒ 均价 0.95~0.975）。")
    print("       扫尾盘是薄赔率策略 ⇒ 边际族的 P&L 从来不是「几笔赢出来的」，而是"
          "「一笔输都会毁掉一大块」。")

    print(f"\n  ③ 逐档增量（把**最宽那一档**买进来的行按 dev 分桶；集合精确划分 ⇒ 合计 = 该档总增量）")
    print("     ⚠️ 不用「更严档 ∖ 更宽档」：同一个 slug 在不同门槛下可能落在**不同 tick**"
          "（门槛一松，t150 段就提前达标）\n        ⇒ 那份差集的 P&L 之和 ≠ 总增量（本例差 10U）。"
          "改按最宽档自己的行分桶，可精确相加。")
    edges = sorted(grid)
    tot = _bt(inc[grid[-1]])
    print(f"  {'dev 区间(美元)':>16s} {'n':>5s} {'胜率':>8s} {'均价':>7s} {'P&L':>8s} "
          f"{'EV/注':>8s} {'负笔':>5s}")
    acc = 0.0
    for i, lo_ in enumerate(edges):
        hi_ = edges[i + 1] if i + 1 < len(edges) else DEV_USD
        s = [r for r in tot if lo_ <= r["dev"] < hi_]
        if not s:
            continue
        acc += pl(s)
        k, n = sum(r["won"] for r in s), len(s)
        print(f"  [{lo_:5.1f}, {hi_:5.1f}) {n:5d} {wr(s)*100:7.2f}% {avg_fill(s):7.4f} "
              f"{pl(s):+8.2f} {pl(s)/n:+8.4f} {n-k:5d}")
    print(f"  {'合计':>16s} {len(tot):5d} {wr(tot)*100:7.2f}% {avg_fill(tot):7.4f} "
          f"{acc:+8.2f} {acc/len(tot):+8.4f}")
    assert abs(acc - pl(tot)) < 1e-9, f"分桶不平：{acc} vs {pl(tot)}"

    curve, loo, cnt, halves, days = _bt_ident(grid, cache, base_bt)
    best = max(grid, key=lambda x: curve[x])
    print(f"\n  ④ 可辨识性（只在 {len(days)} 个回测日上）")
    print("    Δ(X) 曲线: " + "  ".join(f"{x:.0f}:{curve[x]:+.1f}" for x in sorted(grid)))
    print(f"    全样本 argmax = {best:.1f}（{curve[best]:+.2f}U）")
    print(f"    留一天重挑 argmax = {dict(sorted(loo.items()))}")
    print(f"    日级 bootstrap 2000 次 argmax = {dict(sorted(cnt.items()))}"
          f"   ← 最高一档占比 {cnt.most_common(1)[0][1]/2000*100:.1f}%")
    for nm in ("前半", "后半"):
        hc = halves[nm]
        hb = max(grid, key=lambda x: hc[x])
        pos = sum(1 for x in grid if hc[x] > 0)
        print(f"    {nm} argmax = {hb:.1f}（{hc[hb]:+.2f}U）；同号档 {pos}/{len(grid)} 为正"
              + ("   ⚠️ 两半符号不一致" if (hb != best) else ""))
    print("    ⚠️ 读法：argmax 命中率 < 50% 或两半 argmax 不同 ⇒ 该 X 是噪声峰不是平台；")
    print("       逐档 EV/注 若稳定同号且 Δ(X) 单调 ⇒ 没有内部最优，只有「能放松到哪」。")

    # ④b 增量行的逐日 P&L（用户口径：加进来的行会不会把某一天拖亏）
    print(f"\n  ④b 增量行的逐日 P&L（负值 = 这一天**新加的行**是亏的 ⇒ 把该天拖向负）")
    print(f"  {'X(美元)':>8s} {'有增量天':>8s} {'负增量天':>8s} {'最差一天':>16s}  明细（负值日: 美元）")
    for x in grid:
        s = _bt(inc[x])
        if not s:
            continue
        dd = per_day(s)
        neg = {d: v for d, v in dd.items() if v < 0}
        wd, wv = min(dd.items(), key=lambda kv: kv[1])
        print(f"  {x:8.1f} {len(dd):8d} {len(neg):8d} {wd[5:]:>10s}{wv:+7.2f}  "
              + (str(dict(sorted(neg.items()))) if neg else "（无）"))

    # ⑤ 用户验收口径：单日不出现连续亏损（薄赔率策略唯一能接受的失败模式）
    bmx, bbad = _runs_line(base_bt)
    print(f"\n  ⑤ 同日连亏（用户验收口径：单日不出现连续亏即可接受）")
    print(f"    基线（现行）：最大连亏 {bmx} 把，有连亏的天数 {len(bbad)}/{len(_loss_runs(base_bt))}"
          + (f"  {dict(sorted((d, max(v)) for d, v in bbad.items()))}" if bbad else ""))
    print(f"  {'X(美元)':>8s} {'最大连亏':>8s} {'连亏天数':>8s}  明细（日期:段长）")
    for x in grid:
        mx, bad = _runs_line(_bt(cache[x]))
        flag = "" if len(bad) <= len(bbad) else "  ⚠️ 比基线多"
        print(f"  {x:8.1f} {mx:8d} {len(bad):6d}/{len(_loss_runs(_bt(cache[x]))):<3d} "
              + (str(dict(sorted((d, max(v)) for d, v in bad.items()))) if bad else "（无）")
              + flag)
    return grid, best, curve, cnt, halves


def sec10(fix, base_bt):
    """回测单样本判定：三个面板重判，判据完全不看实盘。"""
    print("\n" + "=" * 100)
    print("§10  回测单样本判定（用户 2026-09-26：实盘样本太少，不作依据）")
    print("=" * 100)
    print("    判据三条，全部只吃 14 天回测：")
    print("      ① ΔP&L 的**日级配对区间**（14 个回测日重采样 2000 次，seed 42）不含 0；")
    print("      ② 逐档 EV/注 稳定同号（平台）而非正负交替（噪声）；")
    print("      ③ argmax 可辨识：留一天重挑 & bootstrap 命中率 ≥ 50%、前后半 argmax 一致。")
    print("    ★ 但用户 2026-09-26 提醒：扫尾盘「盈亏比本来就差，唯一就是胜率高，靠高胜率")
    print("      覆盖少量输局」⇒ 真正的判据是**边际族胜率本身**（② 表右列的 Wilson 判定），")
    print("      ΔP&L 只是它的副产品：均价 ~0.98 时赢一笔 +0.035U、输一笔 −2U（约 58:1）。")
    print("    ⚠️ 实盘那 3 天（253 行、边际族仅 4~25 笔）在本节**不参与任何判据**。")
    cacheA, incA, _ = fix["A 无价格闸"]                    # 面板 A/B 里存的是 3 元组
    cacheB, incB, _ = fix[f"B 叠已登记的价格闸 hot≥{P_HI}"]
    gA = gB = FIX_GRID
    sdA = sdB = FIX_SD_BELOW
    phA, phB = 0.0, P_HI
    cacheY, incY, _, gy, sdy, phy = fix["y"]
    cacheZ, incZ, _, gz, sdz, phz = fix["z"]
    return [
        _bt_panel("10.A  sd<30 ⇒ dev ≥ X（无价格闸）",
                  f"sd_below={sdA:.0f}", gA, cacheA, incA, base_bt),
        _bt_panel("10.B  sd<30 ⇒ dev ≥ X ∧ hot≥0.95",
                  f"sd_below={sdB:.0f} 价格闸 hot≥{phB}", gB, cacheB, incB, base_bt),
        _bt_panel("10.C  sd<40 ⇒ dev ≥ X（无价格闸）",
                  f"sd_below={sdy:.0f}", gy, cacheY, incY, base_bt),
        _bt_panel("10.D  sd<40 ⇒ dev ≥ X ∧ hot≥0.95（阶梯 40→sd→63 那一版）",
                  f"sd_below={sdz:.0f} 价格闸 hot≥{phz}", gz, cacheZ, incZ, base_bt),
    ]


# ── §11 回测单样本：低σ档的质量、收益归因与「池化格」口径 ──────────────────
# 承接用户 2026-09-26 的三条口径：① 只看回测（实盘太少）；② 判据 = 单日不出现
# 连亏（赔率差、靠胜率覆盖）；③ 不许为追胜率抬价格闸。本节回答一个问题：
# **sd<30 这一档到底该不该补信号**——答案是不该，理由是这一档本身没有 alpha。

POOL_X = 40.0            # 池化对照的代表阈值（10.A~10.D 各取这一档）
PRICE_BANDS = ((0.80, 0.85), (0.85, 0.90), (0.90, 0.95), (0.95, 0.98), (0.98, 1.01))


def _pool(rows):
    """一格的合并读数：n / 胜率 / 均价（≡平衡线）/ EV / 容亏笔数 / 找平把数 / Wilson 下界。

    「平衡线」= 该格均价（赢 2(1−p)/p、输 2U ⇒ EV=0 ⇔ 胜率 = 均价）；
    「容亏」= 该格 P&L ÷ 2U（再输几笔就把这格的收益抹平）；「找平」= p/(1−p)。
    """
    n = len(rows)
    if not n:
        return None
    k = sum(r["won"] for r in rows)
    pf = sum(r["fill"] for r in rows) / n
    p = pl(rows)
    # 边际 / 其标准误（H0: 真胜率 = 平衡线）：所有分格的 n 都小，这个 z 通常 |z|<1.5，
    # 写出来是为了防止把「14 天里为负」直接读成「这一段真的在亏」。
    z = (k / n - pf) / (pf * (1 - pf) / n) ** 0.5 if 0 < pf < 1 else 0.0
    return dict(n=n, k=k, wr=k / n, pf=pf, p=p, ev=p / n, tol=p / 2.0, z=z,
                need=pf / (1 - pf) if pf < 1 else float("inf"), wlo=wilson_lo(k, n))


def _pool_line(lab, c, width=32):
    if c is None:
        print(f"  {lab:{width}s} n=0")
        return
    print(f"  {lab:{width}s} n={c['n']:4d} 胜率 {c['wr']*100:6.2f}% 均价 {c['pf']:.4f}"
          f" 边际 {(c['wr']-c['pf'])*100:+5.2f}pp(z{c['z']:+4.1f}) EV/注 {c['ev']:+.4f}U"
          f" 合 {c['p']:+7.2f}U 容亏 {c['tol']:5.1f}笔 找平 {c['need']:3.0f}把"
          f" | Wilson下界 {c['wlo']*100:5.2f}% vs 平衡线 {c['pf']*100:5.2f}%"
          + (" ✓" if c['wlo'] > c['pf'] else " ✗"))


def sec11(fix, ctx):
    print("\n" + "=" * 100)
    print("§11  回测单样本：低σ档的质量 / 收益归因 / 「池化格」口径")
    print("=" * 100)
    print("    三条用户口径（2026-09-26）：① 只看回测（实盘 3 天不作依据）；")
    print("      ② 判据 = 单日不出现连亏（本策略赔率差，靠胜率覆盖少量输局）；")
    print("      ③ 不许为追胜率抬价格闸（抬了之后赢一笔只 +0.03U、输一笔 −2U）。")
    print("    「平衡线」= 该格均价：赢 2(1−p)/p、输 2U ⇒ 胜率必须 > 均价才不亏。")
    base = ctx["base_bt"]
    bkey = {r["slug"] + "|" + r["src"] for r in ctx["base"]}

    # ── 11.1 σ 档 × 价格档：现行已买行的边际 ──
    print("\n" + "-" * 100)
    print("[11.1 σ 档 × 价格档：现行已买行（14 天回测）的质量]")
    print("-" * 100)
    for lo, hi in ((0, 30), (30, 40), (40, 63), (63, 1e9)):
        tag = f"sd∈[{lo:.0f},{hi:.0f})" if hi < 1e9 else f"sd≥{lo:.0f}"
        rs = [r for r in base if lo <= r["sd"] < hi]
        _pool_line(f"{tag} 全部", _pool(rs))
        for plo in (0.95, 0.98):
            _pool_line(f"{tag} ∧ 价≥{plo:.2f}", _pool([r for r in rs if r["fill"] >= plo]))
    print("    ⇒ 低σ两档（sd<40）边际为负、高σ两档为正；且**价格越高边际越薄**")

    # ── 11.2 收益归因：+35.09U 来自哪个价格段 / 哪个段 ──
    print("\n" + "-" * 100)
    print("[11.2 P&L 归因：基线的收益来自哪个价格段 / 哪个触发段]")
    print("-" * 100)
    tot = pl(base)
    print(f"  {'价格段':>11s} {'n':>5s} {'胜率':>7s} {'均价':>7s} {'边际':>8s} {'z':>5s} "
          f"{'EV/注':>9s} {'合计':>8s} {'占P&L':>7s} {'找平':>5s}")
    for lo, hi in PRICE_BANDS:
        rs = [r for r in base if lo <= r["fill"] < hi]
        c = _pool(rs)
        if c is None:
            continue
        print(f"  {lo:.2f}~{hi:.2f} {c['n']:5d} {c['wr']*100:6.2f}% {c['pf']:7.4f} "
              f"{(c['wr']-c['pf'])*100:+7.2f}pp {c['z']:+5.1f} {c['ev']:+9.4f} {c['p']:+8.2f} "
              f"{c['p']/tot*100:6.1f}% {c['need']:4.0f}把")
    cb = _pool(base)
    print(f"  {'合计':>11s} {len(base):5d} {wr(base)*100:6.2f}% {avg_fill(base):7.4f} "
          f"{(wr(base)-avg_fill(base))*100:+7.2f}pp {cb['z']:+5.1f} {tot/len(base):+9.4f} "
          f"{tot:+8.2f} {cb['need']:4.0f}把")
    print("    ⚠️ 每一段的 |z| 都 < 1.5 ⇒ **没有哪一段能单独定罪**；这张表只说明")
    print("       「收益集中在 0.85~0.98、且价格越高找平要求越高」，不是分段的显著性结论。")
    for st in ("t150", "t60", "listen"):
        c = _pool([r for r in base if r.get("stage") == st])
        if c:
            _pool_line(f"  触发段 {st}", c)

    # ── 11.3 池化格口径：放松后「该格全部行」的期望（边际族太小，不可作准） ──
    print("\n" + "-" * 100)
    print(f"[11.3 池化格口径：放松到 dev ≥ {POOL_X:.0f} 后该「价格×σ」格里的**全部**行]")
    print("-" * 100)
    print("    理由：边际族只有 7~130 笔且常常 100% 全赢（样本太小、构造上是最靠近边界的行），")
    print("    该格的期望应把「同格里现行已买的行」一起算——它们与新增行同源、可交换。")
    CA, IA, _ = fix["A 无价格闸"]
    CB, IB, _ = fix[f"B 叠已登记的价格闸 hot≥{P_HI}"]
    CY, IY, _, _, _, _ = fix["y"]
    CZ, IZ, _, _, _, _ = fix["z"]
    for lab, C, sd_hi, gate in (("10.A sd<30（无价格闸）", CA, 30.0, 0.0),
                                (f"10.B sd<30 ∧ hot≥{P_HI}", CB, 30.0, P_HI),
                                ("10.C sd<40（无价格闸）", CY, 40.0, 0.0),
                                (f"10.D sd<40 ∧ hot≥{P_HI}", CZ, 40.0, P_HI)):
        print(f"\n  ▸ {lab}")
        for X in sorted(C):
            if X not in (POOL_X, 32.5):
                continue
            bt = [r for r in C[X] if r["src"] == "回测"]
            cell = [r for r in bt if r["sd"] < sd_hi and r["dev"] >= X and r["fill"] >= gate]
            _pool_line(f"dev≥{X:.0f}：全格", _pool(cell))
            _pool_line("       └ 其中新增行",
                       _pool([r for r in cell if r["slug"] + "|" + r["src"] not in bkey]))
    _pool_line("【对照】全基线（不限格）", _pool(base))

    # ── 11.4 反向线索：低σ日该「抬」dev 门槛而不是降（只登记，n 极小） ──
    print("\n" + "-" * 100)
    print("[11.4 反向线索：低σ档抬 dev 门槛（只在 sd<30 内筛，不改任何配置）]")
    print("-" * 100)
    for lo in (30.0, 40.0):
        rs = [r for r in base if r["sd"] < lo]
        print(f"\n  sd < {lo:.0f}（现行 {len(rs)} 笔 {pl(rs):+.2f}U）")
        for X in (63.0, 80.0, 100.0, 120.0):
            _pool_line(f"dev ≥ {X:.0f}", _pool([r for r in rs if r["dev"] >= X]))
    print("    ⚠️ dev≥100 那两行只有 19/33 笔且 100% 全赢 ⇒ **线索不是结论**，")
    print("       但方向明确：低σ档的钱在「位移特别大」那一侧，不在「门槛更低」那一侧。")

    # ── 11.5 用户判据：逐日增量 + 同日连亏（10.C/10.D 的 X=40 档） ──
    print("\n" + "-" * 100)
    print(f"[11.5 用户判据（单日不连亏）：新增行逐日 P&L + 同日连亏段（X={POOL_X:.0f}）]")
    print("-" * 100)
    bmx, bbad = _runs_line(_bt(base))
    print(f"  基线：最长连亏 {bmx} 笔；出现连亏（≥2 笔）的日子 {sorted(bbad)}"
          f"，逐日段长 {dict(sorted(bbad.items()))}")
    for lab, C in (("10.A", CA), ("10.B", CB), ("10.C", CY), ("10.D", CZ)):
        if POOL_X not in C:
            continue
        new = [r for r in C[POOL_X]
               if r["slug"] + "|" + r["src"] not in bkey and r["src"] == "回测"]
        d = per_day(new)
        neg = {k: v for k, v in sorted(d.items()) if v < 0}
        mx, bad = _runs_line(_bt(C[POOL_X]))
        same = (mx, sorted(bad)) == (bmx, sorted(bbad))
        print(f"  {lab} X={POOL_X:.0f}：新增回测行 {len(new):4d} 笔 {pl(new):+6.2f}U"
              f"（均价 {avg_fill(new):.4f}，预期亏损 ≈ {len(new)*(1-avg_fill(new)):.1f} 笔）"
              f"｜负增量日 {len(neg)}"
              + ("：" + ", ".join(f"{k} {v:+.2f}U" for k, v in neg.items()) if neg else "")
              + f"｜全候选最长连亏 {mx} 笔、连亏日 {len(bad)} 天"
              + ("  ✓ 连亏结构不变" if same
                 else f"  ⚠️ 段长变了：{dict(sorted(bad.items()))}"))
    print("    ⚠️ ① 基线自己就有 4 天出现连亏（最长 3 笔）——用户的「单日不连亏」在现行规则上")
    print("       本来就不成立，改动只是「没有变更差」；②「负增量日 0 天」在期望 ≈0 的行上是")
    print("       样本波动（预期亏损笔数见上），不能读成「这些行不亏」。")


# ── §12 机制：σ 小时 dev 的过滤意义为什么会失效 ───────────────────────────

_MAX_LAT = getattr(S23, "MAX_LAT", 300)     # 与 oracle 同源，不另写一份
_FOUR = ("yes_bid", "yes_ask", "no_bid", "no_ask")
MECH_SD_BANDS = ((0.0, 20.0), (20.0, 30.0), (30.0, 40.0),
                 (40.0, 63.0), (63.0, 100.0), (100.0, 1e9))
MECH_R_BANDS = ((0.0, 0.25), (0.25, 0.5), (0.5, 1.0), (1.0, 2.0), (2.0, 1e9))


def _q(xs, f):
    """分位数（无 numpy；f=0.5 即中位）。"""
    s = sorted(xs)
    return s[min(len(s) - 1, int(f * len(s)))] if s else float("nan")


def _mech_rows(ev, hr):
    """rem=60 的判定行（四档齐全 ∧ 延迟合规）——机制检验用的**全宇宙**，不是成交行。"""
    out = []
    for e in ev:
        oc, anchor, h = e.get("outcome"), e.get("twap_open_price"), hr.get(e["start_time"])
        if oc is None or not anchor or not h:
            continue
        x = next((y for y in (e.get("ticks") or []) if y.get("rem") == 60), None)
        p = (x or {}).get("pm") or {}
        if (p.get("book_latency_ms") or 0) > _MAX_LAT:
            continue
        if not all((p.get(k) or 0) > 0 for k in _FOUR):
            continue
        spot, tw = (x.get("bin") or {}).get("price"), (x.get("twap") or {}).get("price")
        if not spot or not tw:
            continue
        side = "yes" if p["yes_ask"] >= p["no_ask"] else "no"
        fill = p["yes_ask"] if side == "yes" else p["no_ask"]
        sgn = 1.0 if side == "yes" else -1.0
        close = e.get("twap_close_price")
        out.append(dict(date=datetime.datetime.fromtimestamp(
            e["start_time"], datetime.timezone.utc).strftime("%Y-%m-%d"),
            slug=e["slug"], ev=e, sd=h, side=side, fill=fill, hot=(fill >= P_FLOOR),
            dev=sgn * (spot - anchor), off=spot - tw, ratio=abs(spot - tw) / h if h else None,
            dS=spot - anchor, dT=tw - anchor,
            flipS=((spot - anchor) > 0) != ((close - anchor) > 0) if close else None,
            flipT=((tw - anchor) > 0) != ((close - anchor) > 0) if close else None,
            lost=not ((oc == 0) if side == "yes" else (oc == 1))))
    return out


def sec12(ev, hr):
    print("\n" + "=" * 100)
    print("§12  机制：σ 小时 `dev` 的过滤意义为什么会失效（offset = 现货 − TWAP）")
    print("=" * 100)
    print("    用户 2026-09-26 的判断：「当 sd 过小时，它的过滤意义就失效了」——本节量化它。")
    print("    坐标：sd = 1σ 折美元；offset = spot − twap（**同一 tick** 上 Binance 现货与")
    print("    Chainlink 聚合值的**水平差**，美元）；ratio = |offset| / sd（这个水平差是几个 σ）；")
    print("    fill = 热门侧有效价；翻车率 = 最终输的比例；余量 = (1 − fill) − 翻车率（pp，")
    print("    >0 才赚钱——赢 2(1−p)/p、输 2U ⇒ 临界翻车率 ≡ 1−fill）。")
    rows = _mech_rows(ev, hr)
    hot = [r for r in rows if r["hot"]]
    print(f"    rem=60 判定行 {len(rows)} 条（其中热 ≥{P_FLOOR:.2f} 的 {len(hot)} 条）")

    # ── 12.1 偏移在 σ 尺子上有多大 ──
    print("\n" + "-" * 100)
    print("[12.1 offset 在 σ 尺子上有多大：sd 越小，同一个水平差占 dev 的份额越大]")
    print("-" * 100)
    print(f"  {'sd 档':>12s} {'n':>5s} {'|offset|中位$':>13s} {'ratio 中位':>10s} "
          f"{'ratio≥1':>8s} {'|dev|中位$':>11s}")
    for lo, hi in MECH_SD_BANDS:
        g = [r for r in rows if lo <= r["sd"] < hi]
        if len(g) < 10:
            continue
        tag = f"[{lo:.0f},{hi:.0f})" if hi < 1e9 else f"≥{lo:.0f}"
        rr = [r["ratio"] for r in g if r["ratio"] is not None]
        print(f"  {tag:>12s} {len(g):5d} {_q([abs(r['off']) for r in g], .5):13,.1f} "
              f"{_q(rr, .5):10.2f} {sum(1 for v in rr if v >= 1) / len(rr) * 100:7.1f}% "
              f"{_q([abs(r['dev']) for r in g], .5):11,.1f}")
    print("    ⇒ sd<20 时中位偏移 = 2.80σ、sd≥100 时 0.23σ：**同一个水平差，σ 越小被放大得越厉害**。")

    # ── 12.2 位移是谁在动：现货 vs TWAP（结算看 TWAP） ──
    print("\n" + "-" * 100)
    print("[12.2 位移层的方向可靠性：现货位移 vs TWAP 位移（结算看 TWAP，不看现货）]")
    print("-" * 100)
    print(f"  {'ratio 档':>12s} {'n':>5s} {'现货翻车':>9s} {'TWAP翻车':>9s} "
          f"{'|现货位移|$':>12s} {'|TWAP位移|$':>12s} {'offset/位移':>11s}")
    for lo, hi in MECH_R_BANDS:
        g = [r for r in rows if r["flipS"] is not None
             and lo <= (r["ratio"] or 0) < hi and abs(r["dS"]) >= 20]
        if len(g) < 10:
            continue
        tag = f"[{lo:.2f},{hi:.2f})" if hi < 1e9 else f"≥{lo:.1f}"
        dd = _q([abs(r["dS"]) for r in g], .5)
        print(f"  {tag:>12s} {len(g):5d} "
              f"{sum(r['flipS'] for r in g) / len(g) * 100:8.1f}% "
              f"{sum(r['flipT'] for r in g) / len(g) * 100:8.1f}% "
              f"{dd:12,.1f} {_q([abs(r['dT']) for r in g], .5):12,.1f} "
              f"{_q([abs(r['off']) for r in g], .5) / dd:11.2f}")
    print("    ⇒ 偏移大时「现货位移」的方向到闭市翻车近四成，而结算线的位移几乎不翻")
    print("      —— 那个位移不是市场走的，是数据源的水平差。")

    # ── 12.3 价格层：余量按 ratio 分档（“过滤失效”的算术形式） ──
    print("\n" + "-" * 100)
    print(f"[12.3 价格层余量按 ratio 分档（热 ≥{P_FLOOR:.2f} 的全部行）——过滤失效的算术形式]")
    print("-" * 100)
    print(f"  {'ratio 档':>12s} {'n':>5s} {'fill':>8s} {'平衡线':>8s} {'翻车率':>8s} "
          f"{'余量pp':>8s} {'找平把':>7s}")
    for lo, hi in MECH_R_BANDS:
        g = [r for r in hot if lo <= (r["ratio"] or 0) < hi]
        if len(g) < 10:
            continue
        tag = f"[{lo:.2f},{hi:.2f})" if hi < 1e9 else f"≥{lo:.1f}"
        f = sum(r["fill"] for r in g) / len(g)
        lr = sum(r["lost"] for r in g) / len(g)
        print(f"  {tag:>12s} {len(g):5d} {f:8.4f} {(1 - f) * 100:7.2f}% {lr * 100:7.2f}% "
              f"{(1 - f - lr) * 100:+8.2f} {f / (1 - f):6.1f}把")
    fb = sum(r["fill"] for r in hot) / len(hot)
    lb = sum(r["lost"] for r in hot) / len(hot)
    print(f"  {'合计':>12s} {len(hot):5d} {fb:8.4f} {(1 - fb) * 100:7.2f}% {lb * 100:7.2f}% "
          f"{(1 - fb - lb) * 100:+8.2f} {fb / (1 - fb):6.1f}把")
    print("    ⇒ 只有 ratio ≥ 2 那一档余量塌成 ≈ 0：σ 小到让水平差独大时，dev 这条闸门")
    print("      不是在挑信号，是在放行（赚不赚钱已经与 dev 无关）。")

    # ── 12.4 sd<30 内部：是谁在亏 ──
    print("\n" + "-" * 100)
    print("[12.4 sd<30 的行按 ratio 拆开：偏移独大的那批来自哪几天]")
    print("-" * 100)
    low = [r for r in rows if r["sd"] < 30.0]
    for lo, hi in MECH_R_BANDS:
        g = [r for r in low if lo <= (r["ratio"] or 0) < hi]
        if len(g) < 10:
            continue
        f = sum(r["fill"] for r in g) / len(g)
        lr = sum(r["lost"] for r in g) / len(g)
        days = collections.Counter(r["date"][5:] for r in g)
        tag = f"[{lo:.2f},{hi:.2f})" if hi < 1e9 else f"≥{lo:.1f}"
        print(f"  ratio {tag:>12s} n={len(g):4d} fill {f:.4f} 翻车 {lr * 100:5.2f}% "
              f"余量 {(1 - f - lr) * 100:+6.2f}pp  日子 "
              + " ".join(f"{d}:{c}" for d, c in days.most_common(4)))
    print("    ⇒ 负余量的两格是「偏移独大」（ratio≥2，93% 来自 08-18/19）与「价太低」")
    print("      （ratio<0.25，fill 仅 0.90）——两者都不是「阈值没找对」。")

    # ── 12.5 这个水平差不是滞后（为什么不能用「等一等」修） ──
    print("\n" + "-" * 100)
    print("[12.5 offset 是水平差不是滞后：日内稳定、窗内稳定]")
    print("-" * 100)
    print(f"  {'日期':>8s} {'窗数':>5s} {'窗内偏移中位的 p25/p50/p75':>28s} {'日IQR':>7s}")
    daily = collections.defaultdict(list)
    for e in ev:
        if hr.get(e["start_time"]) is None:
            continue
        tk = [(x["rem"], (x.get("bin") or {}).get("price"), (x.get("twap") or {}).get("price"))
              for x in (e.get("ticks") or [])]
        o = [b - t for _, b, t in tk if b and t]
        if not o:
            continue
        daily[datetime.datetime.fromtimestamp(
            e["start_time"], datetime.timezone.utc).strftime("%Y-%m-%d")].append(_q(o, .5))
    for d in sorted(daily):
        v = daily[d]
        print(f"  {d[5:]:>8s} {len(v):5d} {_q(v, .25):9,.1f} / {_q(v, .5):9,.1f} / "
              f"{_q(v, .75):9,.1f} {_q(v, .75) - _q(v, .25):7,.1f}")
    print("    窗内演化（偏移最大的那一天的前三窗，逐 rem）：")
    dmax = max(daily, key=lambda d: abs(_q(daily[d], .5)))
    n = 0
    for e in ev:
        if datetime.datetime.fromtimestamp(
                e["start_time"], datetime.timezone.utc).strftime("%Y-%m-%d") != dmax:
            continue
        tk = {x["rem"]: ((x.get("bin") or {}).get("price") or 0)
              - ((x.get("twap") or {}).get("price") or 0) for x in (e.get("ticks") or [])}
        if min(tk or {0: 0}) != 0:
            continue
        print("      " + datetime.datetime.fromtimestamp(
            e["start_time"], datetime.timezone.utc).strftime("%H:%M") + "  "
            + "  ".join(f"rem{r}={tk[r]:+.1f}" for r in (298, 200, 150, 60, 0) if r in tk))
        n += 1
        if n >= 3:
            break
    print("    ⇒ 偏移在整窗内几乎不动、日内也只有 ±10 的抖动 ⇒ 它是**水平差**，不是延迟；")
    print("      所以「等一等/取平均」修不掉它——只能把它**测出来**（落 basis = spot − twap）。")

    # ── 12.6 反向检验：用户的「被一笔大单带翻转」──
    print("\n" + "-" * 100)
    print("[12.6 反面检验：低σ日「被一笔大单带翻转」——价格层/砸穿/PM 卖压三个读数]")
    print("-" * 100)
    print("    分组按**日子**（不按 sd），因为「偏移大」与「σ 小」是两件事（见 12.5）。")
    print(f"  {'日期组':>12s} {'n':>5s} {'翻车率':>7s} {'fill中位':>8s} {'砸穿0.5':>8s} "
          f"{'PM卖股数中位':>12s} {'PM单笔最大中位':>14s}")
    for lab, days in (("08-18/19", ("08-18", "08-19")),
                      ("08-29/30", ("08-29", "08-30")),
                      ("其它 10 天", None)):
        if days is None:
            g = [r for r in hot
                 if r["date"][5:] not in ("08-18", "08-19", "08-29", "08-30")]
        else:
            g = [r for r in hot if r["date"][5:] in days]
        if not g:
            continue
        broke, sells, mx = 0, [], []
        for r in g:
            hit = None
            for x in r["ev"].get("ticks") or []:
                if (x.get("rem") or 999) >= 60:
                    continue
                b = (x.get("pm") or {}).get(r["side"] + "_bid") or 0
                if 0 < b < 0.5:
                    hit = x.get("rem")
                    break
            if hit is not None:
                broke += 1
            tr = [t for t in (r["ev"].get("trades") or [])
                  if t.get("token") == r["side"].upper() and (t.get("rem") or 0) <= 60]
            if tr:
                sells.append(sum(t.get("sell_size") or 0 for t in tr))
                mx.append(max(t.get("max_size") or 0 for t in tr))
        print(f"  {lab:>12s} {len(g):5d} {sum(r['lost'] for r in g) / len(g) * 100:6.2f}% "
              f"{_q([r['fill'] for r in g], .5):8.4f} "
              f"{broke / len(g) * 100:7.1f}% {_q(sells, .5):12,.0f} {_q(mx, .5):14,.0f}")
    print("    ⇒ 三组的翻车率/砸穿比例/卖压**无 σ 或偏移趋势** ⇒ 「一笔大单带翻盘口」")
    print("      在 PM 市场层读不出来（翻车本来就少：热 ≥0.80 的行只有 2~3%）。")
    print("      ⚠️ 「砸穿 0.5」与卖压两列受旧采集守卫影响（空侧消息被整条丢弃，决策 #21）")
    print("      ⇒ 只作「有没有趋势」的对照，不能当水平量用。")


# ── main ─────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="扫尾盘 σ 梯度阈值探索")
    ap.add_argument("--live-dir", default=str(DATA / "tail-live"))
    ap.add_argument("--bt-dir", default=str(DATA / "btc"))
    ap.add_argument("--skip-backtest", action="store_true")
    args = ap.parse_args()

    live_rows = []
    for p in sorted(glob.glob(str(Path(args.live_dir) / "tail_*.jsonl"))):
        for L in open(p):
            if L.strip():
                live_rows.append(json.loads(L))
    lvjud, lvall, settled, win = load_live(Path(args.live_dir))
    listen_lv = listen_of([st[k] for st in lvall.values() for k in st])

    if args.skip_backtest:
        sec1([], {}, live_rows)
        sec2()
        return
    ev = load_events(args.bt_dir)
    hr = S13.hist_ranges(ev)
    sec1(ev, hr, live_rows)
    sec2()
    ctx = sec3(ev, hr, lvjud, listen_lv, settled, win)
    sec4(ctx["res"], ctx["base"])
    cache = sec5(ctx, lvjud, listen_lv, settled, win)
    sec6(ctx, cache)
    fix = sec9(ev, hr, ctx, lvjud, listen_lv, settled, win)
    base, res = ctx["base"], ctx["res"]
    bkey = {r["slug"] + "|" + r["src"] for r in base}
    print("\n" + "=" * 100)
    print("结论（两个候选都按同一套判据：分样本方向 + 配对 Δ 区间 + 边际族胜率 Wilson 下界）")
    print("=" * 100)
    for lab, note in ((f"{GRAD_Z}σ∧{GRAD_FLOOR:.0f} ∧ hot≥{P_HI}", "§5 价格闸版"),
                      (LOWSD_LABEL, "用户 09-26 指定：sd<30 段用 1.25σ∧25，其余现行")):
        v = res[lab]
        inc = [r for r in v if r["slug"] + "|" + r["src"] not in bkey]
        k, n = sum(r["won"] for r in inc), len(inc)
        lo, hi = boot_delta(v, base)
        lo95 = wilson_lo(k, n)
        db = pl([r for r in v if r["src"] == "回测"]) - pl([r for r in base if r["src"] == "回测"])
        dl = pl([r for r in v if r["src"] == "实盘"]) - pl([r for r in base if r["src"] == "实盘"])
        print(f"\n  ▸ {lab}（{note}）")
        print(f"    合并：现行 n={len(base)} {pl(base):+.2f}U  →  候选 n={len(v)} "
              f"胜率 {wr(v)*100:.2f}% 均价 {avg_fill(v):.4f}（平衡线 {avg_fill(v)*100:.2f}%）"
              f" {pl(v):+.2f}U")
        print(f"    分样本 Δ：回测 {db:+.2f}U / 实盘 {dl:+.2f}U"
              + ("   ⚠️ 方向不一致" if db * dl < 0 else ""))
        print(f"    边际族 n={n} 胜率 {k/n*100:.2f}% 均价 {avg_fill(inc):.4f}"
              f"（平衡线 {avg_fill(inc)*100:.2f}%） P&L {pl(inc):+.2f}U   "
              f"Wilson95% 下界 {lo95*100:.2f}%"
              + ("  ✓ 下界 > 平衡线" if lo95 > avg_fill(inc)
                 else "  ✗ 下界 < 平衡线 ⇒ 不过「保证胜率」检验"))
        print(f"    日级配对 Δ 95% 区间 [{lo:+.2f}, {hi:+.2f}]"
              + ("  含 0" if lo <= 0 <= hi else "  ★不含 0"))
    print("\n  ── §9 固定阈值扫描（sd<30 ⇒ dev ≥ X 常数）──")
    for tag in ("A 无价格闸", f"B 叠已登记的价格闸 hot≥{P_HI}"):
        cache, inc, lost = fix[tag]
        vals = {x: pl(cache[x]) - pl(base) for x in FIX_GRID}
        best = max(FIX_GRID, key=lambda x: vals[x])
        v, s = cache[best], inc[best]
        k, n = sum(r["won"] for r in s), len(s)
        lo, hi = boot_delta(v, base)
        db = pl(_sub(v, "回测")) - pl(_sub(base, "回测"))
        dl = pl(_sub(v, "实盘")) - pl(_sub(base, "实盘"))
        print(f"\n  ▸ {tag}：全样本 argmax X = {best:.1f}")
        print(f"    合并 Δ{vals[best]:+.2f}U 区间 [{lo:+.2f}, {hi:+.2f}]"
              + ("  含 0" if lo <= 0 <= hi else "  ★不含 0")
              + f"；分样本 Δ 回测 {db:+.2f}U / 实盘 {dl:+.2f}U"
              + ("   ⚠️ 方向不一致" if db * dl < 0 else ""))
        print(f"    边际族 n={n} 胜率 {k/n*100:.2f}% 均价 {avg_fill(s):.4f}"
              f"（平衡线 {avg_fill(s)*100:.2f}%） P&L {pl(s):+.2f}U  "
              f"Wilson95% 下界 {wilson_lo(k, n)*100:.2f}%"
              + ("  ✓ 下界 > 平衡线" if wilson_lo(k, n) > avg_fill(s)
                 else "  ✗ 下界 < 平衡线 ⇒ 不过「保证胜率」检验"))
    print("\n  处置：三者（梯度 / 用户指定变体 / 固定阈值）都**纸面登记，不改引擎**"
          "（用户 2026-09-26 决定）。")
    print("        复查触发：① 再来一次低σ日（σ 中位 < 35）的实盘数据；② 实盘边际族 ≥ 50 笔。")

    # ── §10 回测单样本判定 + 小结 ──
    bests = sec10(fix, ctx["base_bt"])
    print("\n" + "-" * 100)
    print("[10.E 回测单样本小结]")
    print("-" * 100)
    for title, (grid, best, curve, cnt, halves) in zip(
            ("10.A sd<30 无闸", "10.B sd<30 ∧hot≥0.95",
             "10.C sd<40 无闸", "10.D sd<40 ∧hot≥0.95"), bests):
        top = cnt.most_common(1)[0]
        pos = sum(1 for x in grid if curve[x] > 0)
        hb = {nm: max(grid, key=lambda x: halves[nm][x]) for nm in ("前半", "后半")}
        print(f"\n  ▸ {title}")
        print(f"    回测 argmax = {best:.1f}（{curve[best]:+.2f}U）；同号档 {pos}/{len(grid)} 为正；"
              f"Δ 曲线 [{min(curve.values()):+.1f}, {max(curve.values()):+.1f}]U")
        print(f"    bootstrap argmax 最高档 = {top[0]:.1f}（{top[1]/2000*100:.1f}%）"
              f"；前后半 argmax {hb['前半']:.1f} / {hb['后半']:.1f}"
              + ("   ✓ 一致" if hb['前半'] == hb['后半'] == best else "   ⚠️ 不一致"))
    sec11(fix, ctx)
    print("\n  处置：**不改引擎**（本节结论比 §9/§10 更进一步——低σ档不是「阈值没找对」，")
    print("        而是**这一档本身没有 alpha**：放松 dev 后的全格 EV 在 0 附近、均价 0.983")
    print("        要 58~62 把找平；现行 sd<30 的行本来就是负 EV 档 ⇒ 少交易是正确行为）。")
    sec12(ev, hr)
    print("\n  处置（§12）：机制已定位——`dev = spot − anchor` 混进了**数据源水平差**")
    print("        （Binance 现货 vs Chainlink 聚合），σ 一小它就被放大成 ~2.8σ ⇒ 闸门失效。")
    print("        结论不变：**不改引擎**；可见化（落 basis = spot − twap）另立项。")
    print("=" * 100)


if __name__ == "__main__":
    main()
