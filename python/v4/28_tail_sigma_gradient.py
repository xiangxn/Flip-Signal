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

def run_backtest(ev, hr, r5, want_full_chain=False):
    """用 oracle 的 chain 跑 14 天。

    返回 (judgments, signals)：judgments 是 slug → {stage: 行}（只收 t150/t60 判定行，
    供候选重判），signals 是全部 ok 行（只有 want_full_chain 时才含监听段）。
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
                if r.get("ok") and (want_full_chain or r["stage"] in ("t150", "t60")):
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


def judge(jud, allst, settled, win, leg, is_live):
    """按 leg 重判 t150/t60 → 本样本的信号行（含胜负）。

    段序与 oracle 的 chain 一致：t150（价格腿严格大于）→ t60（≥）→ 监听段。
    监听段沿用基线已记录的行（本次 ② 一字不动 ⇒ 逐窗相同）。
    """
    out = []
    for slug, st in jud.items():
        hit = None
        a, b = st.get("t150"), st.get("t60")
        if a is not None and a["fill"] > P_FLOOR and leg(a):
            hit = a
        elif b is not None and b["fill"] >= P_FLOOR and leg(b):
            hit = b
        if hit is None and is_live:
            l = allst.get(slug, {}).get("listen")
            if l and l.get("ok"):
                hit = l
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

def make_r5(zmul, floor, p_hi):
    """⑤ 的 dev 腿：hot ≥ p_hi 时用梯度 `dev ≥ min(63, max(z·sd, floor))`，否则用现行。

    p_hi = 0.0 ⇒ 纯梯度（不设价格闸）；p_hi = 9.9 ⇒ 恒走现行（= 基线）。
    ⚠️ 该梯度腿是现行 ⑤ σ 腿的**超集**（现行要求 sd≥40 ∧ dev≥sd，梯度要求
    dev ≥ min(63, max(z·sd, floor))，z=1 且 floor≤40 时逐点更松）⇒ **只增不删**，
    没有「被删掉的基线信号」这一项成本（§6.3 自检）。
    """
    def r5(r):
        if r["fill"] < p_hi:                          # 价格闸未过 ⇒ 现行
            return R5_CUR(r)
        bar = min(DEV_USD, max(zmul * r["sd"], floor)) if r["sd"] else DEV_USD
        return r["dev"] >= bar
    return r5


def current_r5(r):
    return R5_CUR(r)


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

def sec3(ev, hr, lvjud, lvall, settled, win):
    print("\n" + "=" * 100)
    print("§3  链级重跑（必须重跑三段链：放宽阈值会改变「哪个 tick 首次达标」）")
    print("=" * 100)
    bt, full = run_backtest(ev, hr, R5_CUR, want_full_chain=True)
    line("现行（完整三段链）", full)
    assert len(full) == 2133, f"基线对账失败：n={len(full)}（应为 2133）"
    assert abs(pl(full) - 35.092747766846315) < 1e-9, f"基线对账失败：P&L={pl(full)!r}"
    print("  ✓ 基线逐位对账 oracle 23：n=2133  P&L=+35.092747766846315U")

    print("\n  [3.2 t150/t60 两段宇宙 —— 实盘可复核的同口径]")
    CAND = [("现行", current_r5),
            (f"纯 {GRAD_Z}σ∧{GRAD_FLOOR:.0f}", make_r5(GRAD_Z, GRAD_FLOOR, 0.0)),
            (f"{GRAD_Z}σ∧{GRAD_FLOOR:.0f} ∧ hot≥0.90", make_r5(GRAD_Z, GRAD_FLOOR, 0.90)),
            (f"{GRAD_Z}σ∧{GRAD_FLOOR:.0f} ∧ hot≥0.95", make_r5(GRAD_Z, GRAD_FLOOR, P_HI)),
            (f"{GRAD_Z}σ∧{GRAD_FLOOR:.0f} ∧ hot≥0.97", make_r5(GRAD_Z, GRAD_FLOOR, 0.97)),
            ("1.25σ∧25 ∧ hot≥0.97", make_r5(1.25, 25.0, 0.97)),
            ("1.5σ∧25 ∧ hot≥0.97", make_r5(1.5, 25.0, 0.97))]
    res = {}
    print(f"\n  {'候选':26s} {'样本':4s} {'n':>6s} {'胜率':>8s} {'均价':>8s} {'EV/注':>9s} {'P&L':>9s}")
    for lab, leg in CAND:
        b = judge(bt, {}, {}, {}, leg, False)
        l = judge(lvjud, lvall, settled, win, leg, True)
        res[lab] = b + l
        for nm, v in (("回测", b), ("实盘", l), ("合并", b + l)):
            print(f"  {lab:26s} {nm:4s} {len(v):6d} {wr(v)*100:7.2f}% {avg_fill(v):8.4f} "
                  f"{pl(v)/len(v):+9.4f} {pl(v):+9.2f}")
        print()
    base = res["现行"]
    print("  日级配对 bootstrap（2000 次, seed 42）—— ΔP&L = 候选 − 现行")
    for lab, _ in CAND[1:]:
        lo, hi = boot_delta(res[lab], base)
        flag = "  含 0" if lo <= 0 <= hi else "  ★不含 0"
        print(f"    {lab:26s} Δ{pl(res[lab])-pl(base):+7.2f}U   95% 区间 [{lo:+.2f}, {hi:+.2f}]{flag}")
    return bt, res, base


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

def sec5(bt, lvjud, lvall, settled, win, base):
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
        rs = judge(bt, {}, {}, {}, leg, False) + judge(lvjud, lvall, settled, win, leg, True)
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

def sec6(bt, lvjud, lvall, settled, win, base, cache):
    print("\n" + "=" * 100)
    print("§6  稳健性")
    print("=" * 100)
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

    if args.skip_backtest:
        sec1([], {}, live_rows)
        sec2()
        return
    ev = load_events(args.bt_dir)
    hr = S13.hist_ranges(ev)
    sec1(ev, hr, live_rows)
    sec2()
    bt, res, base = sec3(ev, hr, lvjud, lvall, settled, win)
    sec4(res, base)
    cache = sec5(bt, lvjud, lvall, settled, win, base)
    sec6(bt, lvjud, lvall, settled, win, base, cache)
    print("\n" + "=" * 100)
    print(f"结论：`{GRAD_Z}σ∧{GRAD_FLOOR:.0f} ∧ hot≥{P_HI}` 是两个样本上都为正的形态")
    print("      （② 一字不动、两个判定时点不变、不删任何现行信号），但**没通过"
          "「保证胜率」检验**：")
    print("      边际族胜率的 Wilson 95% 下界低于它自己的平衡线（§5 表），最接近的"
          " 0.97 档也差 0.74pp。")
    print("      处置（用户 2026-09-26 决定）：**纸面登记，不改引擎**。")
    print("      复查触发：① 再来一次低σ日（σ 中位 < 35）的实盘数据；② 实盘边际族"
          " ≥ 50 笔（现 13 笔）。")
    print("=" * 100)


if __name__ == "__main__":
    main()
