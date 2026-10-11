#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫尾盘 · 全新数据面 第 2 步：从零实现的三段链模拟器（可批量换条件）

⚠️ 同样**不复用仓库任何既有代码**（用户要求：避免历史 bug 污染）。
本文件只依赖 50 号脚本产出的 data/fresh/dataset.pkl。

规格（照 CLAUDE.md 文字逐条实现，不参照任何 .go/.py 源码）：
  有效 tick = book_latency_ms ≤ 300 ∧ 热门侧有效价 > 0 ∧ spot > 0 ∧ rem > 0
  热门侧   = 有效价高的一侧（有效价 = ask>0?ask:bid），平局取 yes
  sgn      = +1 热门侧 yes / −1 热门侧 no
  dev      = sgn·(spot − anchor)      basis = sgn·(spot − twap)     walk = sgn·(twap − anchor)
  sd       = 该窗 σ 折美元（50 号脚本按连续前窗算好）
  ⑤(T150/T60) = 价格腿 ∧ (dev ≥ dev_min ∨ (sd ≥ sd_min ∧ dev ≥ sd))
  ②(监听)     = 价格腿 ∧ dev ≥ dev_min
  价格腿：T150 严格 > price_min；T60 与监听 ≥ price_min
  闸/地板：T150 还要 walk ≥ walk_min（0 关）；T60 与监听还要 有效价 > floor_min_price（0 关）
  三段链：T150 首个 rem≤150 的有效 tick → 不过则 T60 首个 rem≤60 的有效 tick
          → 不过则监听段此后逐秒。任一段成信号即整窗结束。
          ⚠️ 首个 rem≤150 的有效 tick 若已 rem≤60 ⇒ 跳过 T150 段（不伪造 t150 行），
             同一 tick 直接按 T60 规则判。
          ⚠️ 监听段从 T60 判定 tick **之后**开始；没有 rem≤60 的有效 tick ⇒ 不进监听段。

成交口径：fill = 该 tick 有效价；shares = stake/fill；赢 shares−stake / 输 −stake（每股兑 1U）
         净口径另扣 taker 费 fee = shares·rate·p·(1−p) = stake·rate·(1−p)
"""
from __future__ import annotations

import pickle
from dataclasses import dataclass, field

import numpy as np

T_MAX = 300
STAKE = 2.0
FEE_RATE = 0.07


# ----------------------------------------------------------------------------- 载入
def load(path: str = "data/fresh/dataset.pkl") -> list[dict]:
    with open(path, "rb") as fh:
        return pickle.load(fh)


# ------------------------------------------------------------------- 逐 tick 特征矩阵
@dataclass
class Panel:
    """把 3806 个窗摊平成 W×T 矩阵。"""
    W: int
    rem: np.ndarray      # (W,T) int
    valid: np.ndarray    # (W,T) bool
    p: np.ndarray        # (W,T) 热门侧有效价
    dev: np.ndarray
    basis: np.ndarray
    walk: np.ndarray
    hot_yes: np.ndarray
    spot: np.ndarray
    twap: np.ndarray
    anchor: np.ndarray   # (W,)
    sd: np.ndarray       # (W,)
    outcome: np.ndarray  # (W,) 0=Up 1=Down
    start: np.ndarray
    day: list
    slug: list
    hot_has_ask: np.ndarray = None   # 热门侧有 ask（有效价来自 ask）
    hot_ask5: np.ndarray = None      # 热门侧 ask 前 5 档深度（股）
    hot_bid5: np.ndarray = None
    sd_contig: np.ndarray = None
    lat: np.ndarray = None           # (W,T) 盘口延迟

    def won_of(self, wi: np.ndarray) -> np.ndarray:  # noqa: 占位，见 won_at
        raise NotImplementedError


def build_panel(windows: list[dict], max_lat_ms: int = 300) -> tuple[Panel, np.ndarray]:
    """返回 (Panel, 需跳过的窗下标: anchor≤0 或 sd 不可用)。"""
    W = len(windows)
    rem = np.full((W, T_MAX), -999, dtype=np.int32)
    ts = np.zeros((W, T_MAX), dtype=np.int64)
    spot = np.zeros((W, T_MAX))
    twap = np.zeros((W, T_MAX))
    yb = np.zeros((W, T_MAX)); ya = np.zeros((W, T_MAX))
    nb = np.zeros((W, T_MAX)); na = np.zeros((W, T_MAX))
    ya5 = np.zeros((W, T_MAX)); na5 = np.zeros((W, T_MAX))
    yb5 = np.zeros((W, T_MAX)); nb5 = np.zeros((W, T_MAX))
    lat = np.zeros((W, T_MAX), dtype=np.int32)
    for i, w in enumerate(windows):
        a = w["arr"]
        n = min(len(a["rem"]), T_MAX)
        rem[i, :n] = a["rem"][:n]
        ts[i, :n] = a["ts"][:n]
        spot[i, :n] = a["spot"][:n]
        twap[i, :n] = a["twap"][:n]
        yb[i, :n] = a["yb"][:n]; ya[i, :n] = a["ya"][:n]
        nb[i, :n] = a["nb"][:n]; na[i, :n] = a["na"][:n]
        ya5[i, :n] = a["ya5"][:n]; na5[i, :n] = a["na5"][:n]
        yb5[i, :n] = a["yb5"][:n]; nb5[i, :n] = a["nb5"][:n]
        lat[i, :n] = a["lat"][:n]

    anchor = np.array([w["anchor"] for w in windows])
    sd = np.array([w["sd"] for w in windows])
    sd_contig = np.array([w.get("sd_contig", 0.0) for w in windows])
    outcome = np.array([w["outcome"] for w in windows], dtype=np.int32)

    eff_up = np.where(ya > 0, ya, yb)
    eff_dn = np.where(na > 0, na, nb)
    hot_yes = eff_up >= eff_dn                      # 平局取 yes
    p = np.maximum(eff_up, eff_dn)
    sgn = np.where(hot_yes, 1.0, -1.0)

    # 热门侧盘口细节（成交性 / 深度）：ask 缺时有效价走 bid（决策 #21）
    hot_has_ask = np.where(hot_yes, ya > 0, na > 0)
    hot_ask5 = np.where(hot_yes, ya5, na5)
    hot_bid5 = np.where(hot_yes, yb5, nb5)

    in_win = rem > -900
    valid = in_win & (lat <= max_lat_ms) & (p > 0) & (spot > 0) & (rem > 0)

    dev = sgn * (spot - anchor[:, None])
    walk = sgn * (twap - anchor[:, None])
    basis = sgn * (spot - twap)

    panel = Panel(W=W, rem=rem, valid=valid, p=p, dev=dev, basis=basis, walk=walk,
                  hot_yes=hot_yes, spot=spot, twap=twap, anchor=anchor, sd=sd,
                  outcome=outcome,
                  start=np.array([w["start"] for w in windows]),
                  day=[w["day"] for w in windows],
                  slug=[w["slug"] for w in windows],
                  hot_has_ask=hot_has_ask, hot_ask5=hot_ask5, hot_bid5=hot_bid5,
                  sd_contig=sd_contig, lat=lat)
    skip = (anchor <= 0) | (sd <= 0)
    return panel, skip


# --------------------------------------------------------------------------- 阈值配置
@dataclass
class Rules:
    price_min: float = 0.80
    dev_min: float = 63.0
    sd_min: float = 40.0
    walk_min: float = 43.0      # 0 = 关闭 T150 入场闸
    floor_min: float = 0.83     # 0 = 关闭 T60/监听价格地板
    stake: float = STAKE
    fee_rate: float = FEE_RATE
    use_sigma_leg: bool = True  # ⑤ 的 σ 腿（题设默认开）

    def rule5(self, stage_is_t150: bool, p, dev, sd, walk) -> np.ndarray:
        price = (p > self.price_min) if stage_is_t150 else (p >= self.price_min)
        body = (dev >= self.dev_min)
        if self.use_sigma_leg:
            body = body | ((sd >= self.sd_min) & (dev >= sd))
        return price & body

    def rule2(self, p, dev) -> np.ndarray:
        return (p >= self.price_min) & (dev >= self.dev_min)

    def walk_gate(self, walk) -> np.ndarray:
        if self.walk_min == 0:
            return np.ones_like(walk, dtype=bool)
        return walk >= self.walk_min

    def floor(self, p) -> np.ndarray:
        if self.floor_min == 0:
            return np.ones_like(p, dtype=bool)
        return p > self.floor_min


@dataclass
class Entries:
    """每窗一条：是否成交 + 成交时的各项读数。"""
    w: int
    fired: np.ndarray          # (W,) bool
    stage: np.ndarray          # (W,) 0=未成交 1=t150 2=t60 3=listen
    idx: np.ndarray            # (W,) 成交 tick 在窗内下标（未成交 = -1）
    reason: np.ndarray         # (W,) 1=price_low 2=leg_out 3=walk_low 4=floor_low 5=no_tick
    won: np.ndarray            # (W,) bool
    pnl: np.ndarray            # (W,) 毛盈亏
    pnl_net: np.ndarray        # (W,) 扣 taker 费

    def at(self, panel: Panel, name: str) -> np.ndarray:
        """取成交 tick 上的特征值（未成交 = nan）。"""
        src = {"p": panel.p, "dev": panel.dev, "basis": panel.basis, "walk": panel.walk,
               "rem": panel.rem, "spot": panel.spot, "twap": panel.twap}[name]
        out = np.full(panel.W, np.nan)
        m = self.fired
        out[m] = src[np.nonzero(m)[0], self.idx[m]]
        return out

    def side_hot_yes(self, panel: Panel) -> np.ndarray:
        out = np.zeros(panel.W, dtype=bool)
        m = self.fired
        out[m] = panel.hot_yes[np.nonzero(m)[0], self.idx[m]]
        return out


def _first(mask: np.ndarray) -> np.ndarray:
    """(W,T) bool → 每行首个 True 的下标，无则 -1。"""
    any_ = mask.any(axis=1)
    idx = mask.argmax(axis=1)
    idx[~any_] = -1
    return idx


@dataclass
class Prep:
    """链的「不随条件变」的部分，供扫描时反复复用。"""
    W: int
    ar: np.ndarray
    V: np.ndarray
    rem: np.ndarray
    p: np.ndarray
    dev: np.ndarray
    walk: np.ndarray
    basis: np.ndarray
    sd: np.ndarray
    hot_yes: np.ndarray
    outcome: np.ndarray
    r5_t150: np.ndarray
    r5_t60: np.ndarray
    r2_lis: np.ndarray
    m150: np.ndarray
    m60: np.ndarray
    i150: np.ndarray
    i60: np.ndarray
    has150: np.ndarray
    has60: np.ndarray
    t150_eval: np.ndarray
    skip: np.ndarray
    rules: "Rules"


def prepare(panel: Panel, rules: Rules, skip: np.ndarray,
            t150_strict: bool = True) -> Prep:
    W, T = panel.valid.shape
    ar = np.arange(T)[None, :]
    V = panel.valid & ~skip[:, None]
    rem, p, dev, walk = panel.rem, panel.p, panel.dev, panel.walk
    sd = panel.sd[:, None]
    r5_t150 = rules.rule5(t150_strict, p, dev, sd, walk) & rules.walk_gate(walk)
    r5_t60 = rules.rule5(False, p, dev, sd, walk) & rules.floor(p)
    r2_lis = rules.rule2(p, dev) & rules.floor(p)
    m150 = V & (rem <= 150)
    m60 = V & (rem <= 60)
    i150 = _first(m150)
    i60 = _first(m60)
    wrow = np.arange(W)
    has150 = i150 >= 0
    has60 = i60 >= 0
    skip_t150 = has150 & (rem[wrow, np.maximum(i150, 0)] <= 60)
    return Prep(W=W, ar=ar, V=V, rem=rem, p=p, dev=dev, walk=walk, basis=panel.basis,
                sd=panel.sd, hot_yes=panel.hot_yes, outcome=panel.outcome,
                r5_t150=r5_t150, r5_t60=r5_t60, r2_lis=r2_lis, m150=m150, m60=m60,
                i150=i150, i60=i60, has150=has150, has60=has60,
                t150_eval=has150 & ~skip_t150, skip=skip, rules=rules)


def run(prep: Prep, extra150: np.ndarray | None = None,
        extra60: np.ndarray | None = None,
        extralisten: np.ndarray | None = None,
        listen_needs_t60: bool = True,
        listen_includes_t60: bool = False) -> "Entries":
    W, T = prep.V.shape
    ar = prep.ar
    wrow = np.arange(W)
    rem, p = prep.rem, prep.p
    i150, i60 = prep.i150, prep.i60
    has150, has60 = prep.has150, prep.has60

    r5_t150, r5_t60, r2_lis = prep.r5_t150, prep.r5_t60, prep.r2_lis
    if extra150 is not None:
        r5_t150 = r5_t150 & extra150
    if extra60 is not None:
        r5_t60 = r5_t60 & extra60
    if extralisten is not None:
        r2_lis = r2_lis & extralisten

    fired = np.zeros(W, dtype=bool)
    stage = np.zeros(W, dtype=np.int32)
    idx = np.full(W, -1, dtype=np.int32)
    reason = np.zeros(W, dtype=np.int32)

    t150_eval = prep.t150_eval
    ok150 = np.zeros(W, dtype=bool)
    ok150[t150_eval] = r5_t150[wrow[t150_eval], i150[t150_eval]]
    fire150 = ok150 & ~prep.skip
    fired |= fire150
    stage[fire150] = 1
    idx[fire150] = i150[fire150]

    t60_eval = has60 & ~fired
    ok60 = np.zeros(W, dtype=bool)
    ok60[t60_eval] = r5_t60[wrow[t60_eval], i60[t60_eval]]
    fire60 = ok60 & ~fired
    fired |= fire60
    stage[fire60] = 2
    idx[fire60] = i60[fire60]

    fallback = np.where(has150, i150, -1)
    if listen_includes_t60:
        bound = np.where(has60, i60 - 1, fallback)
        gate60 = np.ones(W, dtype=bool)
    else:
        bound = np.where(has60, i60, fallback)
        gate60 = np.ones(W, dtype=bool) if listen_needs_t60 else (has150 | has60)
    after = (ar > bound[:, None]) & gate60[:, None]
    mL = prep.V & after & (rem > 0)
    iL = _first(mL & r2_lis)
    fireL = (iL >= 0) & ~fired
    fired |= fireL
    stage[fireL] = 3
    idx[fireL] = iL[fireL]

    nofire = ~fired & ~prep.skip
    reason[nofire & ~has150] = 5
    rules = prep.rules
    m = nofire & t150_eval
    if m.any():
        rr = wrow[m]; i = i150[m]
        r = np.full(int(m.sum()), 2, dtype=np.int32)
        price_bad = ~(p[rr, i] > rules.price_min)
        walk_bad = ~rules.walk_gate(prep.walk[rr, i])
        r[price_bad] = 1
        r[~price_bad & walk_bad] = 3
        reason[rr] = r
    m = nofire & ~t150_eval & has60
    if m.any():
        rr = wrow[m]; i = i60[m]
        r = np.full(int(m.sum()), 2, dtype=np.int32)
        price_bad = ~(p[rr, i] >= rules.price_min)
        floor_bad = ~rules.floor(p[rr, i])
        r[price_bad] = 1
        r[~price_bad & floor_bad] = 4
        reason[rr] = r

    won = np.zeros(W, dtype=bool)
    fi = np.nonzero(fired)[0]
    hot_yes = prep.hot_yes[fi, idx[fi]]
    oc = prep.outcome[fi]
    won[fi] = (hot_yes & (oc == 0)) | (~hot_yes & (oc == 1))
    fill = p[fi, idx[fi]]
    shares = rules.stake / fill
    pnl = np.where(won[fi], shares - rules.stake, -rules.stake)
    fee = shares * rules.fee_rate * fill * (1 - fill)
    pnl_full = np.zeros(W); pnl_net_full = np.zeros(W)
    pnl_full[fi] = pnl; pnl_net_full[fi] = pnl - fee
    return Entries(w=W, fired=fired, stage=stage, idx=idx, reason=reason,
                   won=won, pnl=pnl_full, pnl_net=pnl_net_full)


def simulate(panel: Panel, rules: Rules, skip: np.ndarray,
             extra150: np.ndarray | None = None,
             extra60: np.ndarray | None = None,
             extralisten: np.ndarray | None = None,
             listen_needs_t60: bool = True,
             listen_includes_t60: bool = False,
             t150_strict: bool = True) -> Entries:
    """三段链。extra* = 每段额外的 (W,T) bool 条件（None = 无额外条件）。

    末尾三个开关只服务于口径归因诊断（default = CLAUDE.md 规格）：
      listen_needs_t60   : 监听段是否必须先经过 rem≤60 的有效 tick（规格=是）
      listen_includes_t60: 监听段能否在 T60 判定那一 tick 上判定（规格=否，从下一 tick 起）
      t150_strict        : T150 价格腿严格大于（规格=是，决策 #26）
    """
    prep = prepare(panel, rules, skip, t150_strict=t150_strict)
    return run(prep, extra150=extra150, extra60=extra60, extralisten=extralisten,
               listen_needs_t60=listen_needs_t60, listen_includes_t60=listen_includes_t60)


# ------------------------------------------------------------------------- 成交行提取
def entry_table(panel: Panel, e: Entries) -> dict:
    """把已成交行摊平成「一行一笔」的特征表（分析用）。"""
    fi = np.nonzero(e.fired)[0]
    ix = e.idx[fi]
    return {
        "w": fi, "stage": e.stage[fi].copy(), "won": e.won[fi].copy(),
        "pnl": e.pnl[fi].copy(), "pnl_net": e.pnl_net[fi].copy(),
        "p": panel.p[fi, ix], "dev": panel.dev[fi, ix], "basis": panel.basis[fi, ix],
        "walk": panel.walk[fi, ix], "rem": panel.rem[fi, ix].astype(float),
        "spot": panel.spot[fi, ix], "twap": panel.twap[fi, ix],
        "anchor": panel.anchor[fi], "sd": panel.sd[fi],
        "hot_yes": panel.hot_yes[fi, ix].copy(),
        "ask5": panel.hot_ask5[fi, ix], "has_ask": panel.hot_has_ask[fi, ix].copy(),
        "lat": panel.lat[fi, ix].astype(float),
        "day": np.array([panel.day[i] for i in fi]),
        "start": panel.start[fi],
        "slug": np.array([panel.slug[i] for i in fi]),
    }


def excess(t: dict) -> np.ndarray:
    """每笔的「超额定价」收益 = P&L / stake：赢 (1−p)/p，输 −1。"""
    return np.where(t["won"], (1.0 - t["p"]) / t["p"], -1.0)


# ------------------------------------------------------------------------- 统计工具
def boot_delta(panel: Panel, a: Entries, b: Entries, n: int = 4000, seed: int = 51
               ) -> tuple[float, float, float]:
    """日级配对 bootstrap：Δ = ΣP&L(a) − ΣP&L(b)，按日重采样。返回 (Δ, lo, hi)。"""
    days = np.array(sorted(set(panel.day)))
    didx = {d: i for i, d in enumerate(days)}
    di = np.array([didx[d] for d in panel.day])
    da = np.bincount(di, weights=a.pnl, minlength=len(days))
    db = np.bincount(di, weights=b.pnl, minlength=len(days))
    diff = da - db
    rng = np.random.default_rng(seed)
    tot = np.empty(n)
    for k in range(n):
        s = rng.integers(0, len(days), len(days))
        tot[k] = diff[s].sum()
    return diff.sum(), float(np.percentile(tot, 2.5)), float(np.percentile(tot, 97.5))


def stage_table(panel: Panel, e: Entries) -> str:
    lines = []
    tw = {}
    for s, name in ((1, "t150"), (2, "t60"), (3, "listen")):
        m = e.fired & (e.stage == s)
        n = int(m.sum())
        w = int(e.won[m].sum())
        pnl = float(e.pnl[m].sum())
        pnl_n = float(e.pnl_net[m].sum())
        tw[name] = (n, w, pnl)
        lines.append(f"  {name:6s} n={n:5d}  WR={100.0*w/max(n,1):7.3f}%  "
                     f"毛 {pnl:+8.3f}U  净 {pnl_n:+8.3f}U")
    n = int(e.fired.sum()); w = int(e.won.sum())
    lines.append(f"  {'合计':6s} n={n:5d}  WR={100.0*w/max(n,1):7.3f}%  "
                 f"毛 {e.pnl.sum():+8.3f}U  净 {e.pnl_net.sum():+8.3f}U")
    return "\n".join(lines)


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/fresh/dataset.pkl")
    args = ap.parse_args()

    ws = load(args.data)
    panel, skip = build_panel(ws)
    print(f"窗数 {panel.W}  sd 不可用/无锚 跳过 {int(skip.sum())}  参与判定 {panel.W-int(skip.sum())}")

    rules = Rules()
    e = simulate(panel, rules, skip)
    print("\n【基线 = 现行引擎口径（walk≥43 / floor>0.83 / ⑤ 含 σ 腿）】")
    print(stage_table(panel, e))
    print(f"  文档 pin（data/events, 现行链）≈ 1959 笔 / 毛 +12.29U @2U  ← 用于校准本实现")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
