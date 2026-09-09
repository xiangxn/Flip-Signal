#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v5 TWAP-pred 主线回测（v1: 线性速度 v + 结算窗历史滚出）—— data/btc 全量 14 天。

规则出处: docs/twap-pred.md（§三/§四/§八 离散统一公式; §十 数据可用性与"用实际
ts 差估计速度"）; 回测计划落盘: docs/twap_pred_plan_2026-09-07.md。

入场（doc §二/§四; 2026-09-07 并入方向腿为第 3 核心腿）:
  观察     某侧 pm ask ≤ trigger（默认 0.3）开始密切观察
  速度腿   v(L) = 入场 tick 前 L 秒 bin.price 对连续时间 ts 的 OLS 斜率（$/s）
           —— doc §十: 实际 ts 差, 勿假设严格 1s
  方向腿   v 带方向: buy yes 需 v ≥ 0 / buy no 需 v ≤ 0——速度若朝该侧恶化方向
           （背向下注侧）禁止入场, 观察延续至 v 转好或窗口结束; v==0 中性放行
           （用户 2026-09-07 确认: "动态计算速度, 方向向恶化方向不能下注"）
  穿越腿   D_pred = TWAP_pred − twap_open_price:
           TWAP_pred = (结算窗内 ts≤t_i 真实 bin.price 之和
                        + 结算窗内 ts>t_i 的 (P0 + v·Δt) 之和) / 60
           （R≥60 时已知部分为空 → 自动退化纯外推, 等价 doc P0+v(R−29.5)）
  信号     yes_ask≤trigger 且 D_pred >  +M → 买 yes
           no_ask ≤trigger 且 D_pred <  −M → 买 no
  M        安全边际（$，doc §四"后续优化重点" → 全网格扫描, 不做样本内选点结论）
语义约定（相对 doc 的显式化）:
  * 逐 tick 连续观察：首个「ask≤trigger 且 D_pred 越 ±M 且方向腿过」tick 即入场
    （doc 字面; ≠ dog@0.2 的"首触即判"——狗家族首触不重试口径不迁移到本策略;
    方向腿拒绝只是跳过该 tick, 不入场即停——观察动态延续）。
  * 每窗每侧最多一单、每窗最多一单（先命中侧先到先得, 入场即停——
    为将来落 Go 引擎 Watching→Done 单信号惯例铺路）。
  * 数据门控（镜像 python/v4/01_backtest_r1.py）: pm 四字段 >0、book_latency
    ≤300ms、rem ≥ 1（剔除结算边界 tick）。
  * 结算窗 = 60 个 tick（rem ≤ 59 且 ts < END; 末 tick ts==END 为 rem=0 重复
    采样须排除——实测 twap.price 末 tick 与官方 twap_close_price 逐位相等,
    证实结算窗即终点 END 最后 60 秒）。
收益口径（与 v4 全同）: stake/注, shares=stake/fill（fill=入场 tick 侧 ask）;
赢 → shares−stake, 输 → −stake。输赢按官方 outcome（0=Up 1=Down, lib 已
settlement_correction 合并覆盖）。预测全程 Binance 口径 vs 官方 Chainlink 结算
（与 dog@0.2「现货预测腿 vs 官方结算」同构; Binance↔Chainlink 系统基差影响
留后续变体分析——回测只对"预测符号/幅度"与 actual_d 的一致性做校准审计, 见 02）。

输出:
  A 数据概览（窗口/事件/≤trigger 观察 episode 频率——窗口末段输家 ask 趋 0 为
    常态, 信号频率须以 rem≥60 段与网格命中量为准）
  B 网格主线决策表（现行三腿口径: 方向腿并入; look × M 每格:
    n/WR/均fill/EV/P&L/日正/h1:h2/R<60 滚出族）
  B′ 方向腿对照（同格无腿 vs 有腿: M=0 各 look + argmax 行 + L=15/M=10,
    量化第 3 腿剔除的恶化方向单贡献）
  C 分桶统计（argmax 行 + 自然对照 L=15/M=10 行 × rem 桶 × |v| 桶 + 逐日 P&L）
  CSV 逐笔明细（python/v5/data/trades_twap_pred.csv, 现行三腿口径行, 供 02
  增强评估与未来 OOS）

用法:
  python 01_backtest_twap_pred.py [--data data/btc] [--trigger-ask 0.3]
      [--stake 2] [--looks 5,10,15,20,30] [--ms 0,5,10,15,20,30,50]
"""
import argparse
import math
import sys
from math import sqrt
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))
from v2.lib import load_events  # noqa: E402

# ---- 常量（出处标注） ------------------------------------------------------
STAKE_DEF = 2.0        # 每注 USDC（与 v4 回测同口径）
TRIGGER_DEF = 0.30     # 观察/入场阈值（twap-pred.md §二: 0.3 更宽松, 0.25 亦可）
LOOKS_DEF = (5, 10, 15, 20, 30)      # v 估计回看秒数（doc §十未定 → 网格扫描）
MARGINS_DEF = (0.0, 5.0, 10.0, 15.0, 20.0, 30.0, 50.0)  # M 安全边际档（$）
LAT_MAX = 300          # pm book_latency_ms 上限（镜像 v4 01_backtest_r1.py）
END_SEC = 300.0        # 5 分钟窗口总时长（秒）
WIN_REM_MAX = 59       # 结算窗 rem 上限（终点 END 前 60 个 tick, 60s 窗）
SLOPE_MIN_FRAC = 0.8   # v OLS 最少样本 = max(3, ceil(0.8×L))
SLOPE_MIN_N = 3
CSV_DEF = BASE / "data" / "trades_twap_pred.csv"


# ---- 事件级向量化预计算 -----------------------------------------------------

def prep_event(e):
    """事件 → 预测所需向量数组。仅用 ts/rem/pm/bin 等入场时刻已知信息;
    outcome/twap_close 绝不进入本函数（防回看泄漏的结构性保证）。"""
    ticks = e.get("ticks") or []
    n = len(ticks)
    # ts 一律平移到窗口起点（小尺度秒 0~300）: OLS 需要 x 的去中心化差,
    # 绝对 epoch 秒 (~1.8e9) 会造成灾难性浮点抵消（sxx−sx²/c 误差淹掉真值）。
    # 下游只使用 ts 差分, 平移不改变任何结果。
    ts = np.array([t["ts"] for t in ticks], dtype=float) / 1000.0 - e["start_time"]
    rem = np.array([t.get("rem", -1) for t in ticks], dtype=float)
    # pm 四字段报价 + 门控（缺字段按 0 → 门控排除）
    pm = [t.get("pm") or {} for t in ticks]
    yb = np.array([p.get("yes_bid") or 0.0 for p in pm])
    ya = np.array([p.get("yes_ask") or 0.0 for p in pm])
    nb = np.array([p.get("no_bid") or 0.0 for p in pm])
    na = np.array([p.get("no_ask") or 0.0 for p in pm])
    lat = np.array([p.get("book_latency_ms") or 0 for p in pm])
    pm_ok = ((yb > 0) & (ya > 0) & (nb > 0) & (na > 0)
             & (lat <= LAT_MAX)).astype(bool)
    # Binance spot / 成交流 / 盘口深度
    pr = np.array([(t.get("bin") or {}).get("price") or np.nan
                   for t in ticks], dtype=float)
    bv = np.array([(t.get("bin") or {}).get("buy_vol") or 0.0 for t in ticks])
    sv = np.array([(t.get("bin") or {}).get("sell_vol") or 0.0 for t in ticks])
    d5 = np.array([(t.get("bin") or {}).get("bid5") or 0.0 for t in ticks])
    a5 = np.array([(t.get("bin") or {}).get("ask5") or 0.0 for t in ticks])
    # 结算窗（60 tick: rem≤59 且 ts<END; 排除末 tick ts==END 的 rem=0 重复采样）
    win = (rem >= 0) & (rem <= WIN_REM_MAX) & (ts < END_SEC)
    nwin = int(win.sum())
    if nwin != 60:  # 防御: 数据审计下应恒 60
        print(f"⚠️ [prep] {e['start_time']} 结算窗 tick 数 {nwin}≠60, "
              f"按实际 {nwin} 归一", file=sys.stderr)
    # 前缀和: 已知部分 = 结算窗内 ts≤t_i 的真实 tick
    wp = np.where(win, np.where(np.isfinite(pr), pr, 0.0), 0.0)
    wt = np.where(win, ts, 0.0)
    pc_p = np.concatenate(([0.0], np.cumsum(wp)))   # 窗口价前缀（pc_p[i+1]=Σj≤i）
    pc_n = np.concatenate(([0], np.cumsum(win)))    # 窗口 tick 计数前缀
    pc_t = np.concatenate(([0.0], np.cumsum(wt)))   # 窗口 ts 前缀
    cb = np.concatenate(([0.0], np.cumsum(bv)))     # 成交流前缀（10s/120s 特征）
    cs = np.concatenate(([0.0], np.cumsum(sv)))
    open_ = e.get("twap_open_price") or np.nan
    return {"start": e["start_time"], "n": n, "ts": ts, "rem": rem,
            "pr": pr, "pm_ok": pm_ok, "ask_y": np.where(pm_ok, ya, np.inf),
            "ask_n": np.where(pm_ok, na, np.inf),
            "win": win, "nwin": nwin, "pc_p": pc_p, "pc_n": pc_n, "pc_t": pc_t,
            "cb": cb, "cs": cs, "d5": d5, "a5": a5, "open": open_}


def slopes(pe, looks):
    """v(L): 每 tick 前 L 秒 bin.price 对连续 ts 的 OLS 斜率（两指针 + 运行和,
    真实 ts 差——doc §十）。样本数 < max(3, ceil(0.8L)) → NaN（该 (tick,L) 无效）。"""
    ts, pr, n = pe["ts"], pe["pr"], pe["n"]
    out = {}
    for L in looks:
        v = np.full(n, np.nan)
        req = max(SLOPE_MIN_N, math.ceil(SLOPE_MIN_FRAC * L))
        lo, cc = 0, 0
        sx = sy = sxx = sxy = 0.0
        for i in range(n):
            x, y = ts[i], pr[i]
            if math.isfinite(y):
                cc += 1
                sx += x
                sy += y
                sxx += x * x
                sxy += x * y
            while ts[i] - ts[lo] > L:   # 弹出窗口外 tick（含无效价占位）
                xl, yl = ts[lo], pr[lo]
                if math.isfinite(yl):
                    cc -= 1
                    sx -= xl
                    sy -= yl
                    sxx -= xl * xl
                    sxy -= xl * yl
                lo += 1
            if cc >= req:
                den = cc * sxx - sx * sx
                if den > 1e-12:
                    v[i] = (cc * sxy - sx * sy) / den
        out[L] = v
    return out


def d_pred_vec(pe, vL):
    """全 tick 的 D_pred（doc §八 连续时间化）:
    (已知真实和 + Σ未来 (P0+v·Δt)) / nwin − open。v/P0 无效处 → NaN。"""
    ts, pr = pe["ts"], pe["pr"]
    known = pe["pc_p"][1:pe["n"] + 1]        # 已知部分和（含 t_i 自身, k=0 真实价）
    known_n = pe["pc_n"][1:pe["n"] + 1]
    fut_n = pe["nwin"] - known_n
    fut_ts = pe["pc_t"][-1] - pe["pc_t"][1:pe["n"] + 1]  # Σ未来窗口 ts
    fut_dt = fut_ts - fut_n * ts             # Σ未来 (ts_j − t_i)
    d = (known + fut_n * pr + vL * fut_dt) / pe["nwin"] - pe["open"]
    d[~np.isfinite(vL) | ~np.isfinite(pr) | ~np.isfinite(d)] = np.nan
    return d


def first_hit(cond):
    """布尔向量首 True 索引; 无则 -1。"""
    if not cond.any():
        return -1
    return int(np.argmax(cond))


def scan_event(pe, vL, look, margin, trigger, vdir=False):
    """单事件单格（look, margin）: 首命中 tick → 行 dict 或 None。

    每窗最多一单: yes/no 各自首命中 tick 取更早者; 同 tick 双侧同时命中（数学上
    不可能——yes_ask≤0.3 ⟺ no_bid≳0.7）按更小 fill 侧兜底。

    vdir=True（现行口径）: 方向腿并入——buy yes 需 v≥0 / buy no 需 v≤0, 即速度
    若向该侧恶化方向（背向下注侧）则该 tick 不入场、观察延续至 v 转好或窗口结束;
    v==0 中性放行。vdir=False 仅供 B′ 无腿对照（历史口径）。"""
    rem = pe["rem"]
    valid = pe["pm_ok"] & (rem >= 1) & np.isfinite(vL) & np.isfinite(pe["pr"])
    d = d_pred_vec(pe, vL)
    cy = valid & (pe["ask_y"] <= trigger) & (d > margin)
    cn = valid & (pe["ask_n"] <= trigger) & (d < -margin)
    if vdir:                       # 方向腿: 恶化方向禁入（v==0 双侧均过 = 中性放行）
        cy = cy & (vL >= 0.0)      # buy yes: spot 须上行/持平
        cn = cn & (vL <= 0.0)      # buy no:  spot 须下行/持平
    iy, inn = first_hit(cy), first_hit(cn)
    if iy < 0 and inn < 0:
        return None
    if iy >= 0 and inn >= 0:
        if iy != inn:
            i, side = (min(iy, inn), "yes" if iy < inn else "no")
        else:  # 同 tick 双侧命中兜底（理论上不发生）
            i, side = iy, "yes" if pe["ask_y"][iy] <= pe["ask_n"][iy] else "no"
    elif iy >= 0:
        i, side = iy, "yes"
    else:
        i, side = inn, "no"
    t = int(i)
    fill = pe["ask_y"][t] if side == "yes" else pe["ask_n"][t]
    ts_i = pe["ts"][t]

    def win_sum(cum, secs):  # [ts−secs, ts] 成交流前缀差
        lo = int(np.searchsorted(pe["ts"], ts_i - secs, side="left"))
        return float(cum[t + 1] - cum[lo])

    return {"tick_i": t, "side": side, "look": look, "margin": margin,
            "rem": float(rem[t]), "fill": float(fill),
            "v": float(vL[t]), "d_pred": float(d[t]),
            "known_n": int(pe["pc_n"][t + 1]),
            "buy10": win_sum(pe["cb"], 10.0), "sell10": win_sum(pe["cs"], 10.0),
            "buy120": win_sum(pe["cb"], 120.0), "sell120": win_sum(pe["cs"], 120.0),
            "depth_bid5": float(pe["d5"][t]), "depth_ask5": float(pe["a5"][t])}


# ---- B′ 无腿对照补扫（历史口径） -------------------------------------------

def scan_legless(events, trigger, cells):
    """B′ 对照: 对指定 (look, margin) 格集合做**无方向腿**（并入前历史口径）扫描,
    行结构与 scan_event 全同。只用于量化方向腿剔除的恶化方向单, 不进主表/CSV。"""
    rows = []
    for e in events:
        if e.get("outcome") is None or not (e.get("twap_open_price")):
            continue
        pe = prep_event(e)
        if not ((pe["ask_y"] <= trigger).any() or (pe["ask_n"] <= trigger).any()):
            continue
        vs = slopes(pe, {L for L, _ in cells})
        start, outcome = e["start_time"], e["outcome"]
        actual_d = (e.get("twap_close_price") or 0.0) - (e.get("twap_open_price") or 0.0)
        for L, m in cells:
            r = scan_event(pe, vs[L], L, m, trigger, vdir=False)
            if r is None:
                continue
            r.update({"event_start": start, "actual_d": float(actual_d),
                      "settle_won": int((outcome == 0) if r["side"] == "yes"
                                        else (outcome == 1))})
            rows.append(r)
    return rows


# ---- 观测 episode 概览（A 节） ---------------------------------------------

def episodes(mask, early):
    """连续 True 段计数; early 为 True 的 tick 所在段另计（early 段 = 含 rem≥60
    tick, 即"前 4 分钟内即可观察/入场"的信号段）。"""
    ep_all = ep_early = 0
    run = False
    has_early = False
    for f, em in zip(mask, early):
        if f:
            run = True
            has_early = has_early or bool(em)
        else:
            if run:
                ep_all += 1
                if has_early:
                    ep_early += 1
            run = False
            has_early = False
    if run:
        ep_all += 1
        if has_early:
            ep_early += 1
    return ep_all, ep_early


# ---- 报告（仿 v4 中文格式） ---------------------------------------------------

def wilson(k, n, z=1.96):
    """Wilson 置信区间（v4 同款）。"""
    if n == 0:
        return (0, 0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    w = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - w) / d, (c + w) / d)


def pl_of(s):
    """收益向量（U/注）: 赢 → stake/fill−stake, 输 → −stake。"""
    return np.where(s["settle_won"] == 1, STAKE / s["fill"] - STAKE, -STAKE)


def stat_line(name, s, ndays=None):
    """单行摘要; ndays 给定时附 日正/h1:h2。"""
    if len(s) == 0:
        print(f"{name}: n=0")
        return
    pl = pl_of(s)
    k = int(s["settle_won"].sum())
    lo, hi = wilson(k, len(s))
    line = (f"{name}: n={len(s):4d}  WR {k/len(s)*100:5.1f}% "
            f"[{lo*100:.1f},{hi*100:.1f}]  fill {s['fill'].mean():.3f}  "
            f"EV {pl.mean():+.3f}U/注  P&L {pl.sum():+7.1f}U")
    if ndays:
        dg = s.groupby("date")["settle_won"].sum()
        line += (f"  日正 {int((dg > 0).sum())}/{ndays}  "
                 f"h1 {len(s[s['h']=='h1'])} / h2 {len(s[s['h']=='h2'])}")
    print(line)


# ---- 主流程 -------------------------------------------------------------------

def main():
    global STAKE
    ap = argparse.ArgumentParser(description="v5 TWAP-pred 主线回测（线性 v + 滚出）")
    ap.add_argument("--data", default=str(BASE.parent.parent / "data" / "btc"))
    ap.add_argument("--trigger-ask", type=float, default=TRIGGER_DEF)
    ap.add_argument("--stake", type=float, default=STAKE_DEF)
    ap.add_argument("--looks", type=str, default=",".join(map(str, LOOKS_DEF)))
    ap.add_argument("--ms", type=str, default=",".join(map(str, MARGINS_DEF)))
    ap.add_argument("--csv", default=str(CSV_DEF))
    args = ap.parse_args()
    STAKE = args.stake
    trigger = args.trigger_ask
    looks = tuple(int(x) for x in args.looks.split(","))
    margins = tuple(float(x) for x in args.ms.split(","))

    events = load_events(args.data)
    if not events:
        print("无事件数据")
        return
    d0 = pd.to_datetime(events[0]["start_time"], unit="s").date()
    d1 = pd.to_datetime(events[-1]["start_time"], unit="s").date()
    print(f"数据 {args.data}: 事件窗口 {len(events)} 个（{d0} ~ {d1}）"
          f" | trigger≤{trigger} | stake={STAKE}U | looks={looks} | M档={margins}\n")

    # 单遍扫描（现行三腿口径 = vdir=True 并入方向腿）: A 概览计数 + 网格逐格首命中
    n_win_y = n_win_n = n_win_any = n_win_early = 0
    ep_y = ep_ey = ep_n = ep_en = 0
    rows = []
    for k, e in enumerate(events):
        if e.get("outcome") is None or not (e.get("twap_open_price")):
            continue
        pe = prep_event(e)
        my = pe["ask_y"] <= trigger          # ask=inf 已天然排除无效 tick
        mn = pe["ask_n"] <= trigger
        if my.any():
            n_win_y += 1
        if mn.any():
            n_win_n += 1
        if (my | mn).any():
            n_win_any += 1
        if (my & (pe["rem"] >= 60)).any() or (mn & (pe["rem"] >= 60)).any():
            n_win_early += 1
        a, b = episodes(my, my & (pe["rem"] >= 60))
        ep_y += a
        ep_ey += b
        a, b = episodes(mn, mn & (pe["rem"] >= 60))
        ep_n += a
        ep_en += b
        if not (my.any() or mn.any()):
            continue                       # 无 ≤trigger tick → 全格必不命中
        vs = slopes(pe, looks)
        start, outcome = e["start_time"], e["outcome"]
        actual_d = (e.get("twap_close_price") or 0.0) - (e.get("twap_open_price") or 0.0)
        for L in looks:
            for m in margins:
                r = scan_event(pe, vs[L], L, m, trigger, vdir=True)
                if r is not None:
                    r.update({"event_start": start, "actual_d": float(actual_d),
                              "settle_won": int((outcome == 0) if r["side"] == "yes"
                                                else (outcome == 1))})
                    rows.append(r)
        if (k + 1) % 1000 == 0:
            print(f"  扫描 {k+1}/{len(events)} 事件 ...", file=sys.stderr)

    # ---------- A: 概览 ----------
    print("A 数据概览")
    print(f"  窗口总数 {len(events)}; 含 ask≤trigger tick 的窗口: yes侧 {n_win_y}"
          f" / no侧 {n_win_n} / 任一 {n_win_any}（含 rem≥60 段: {n_win_early}）")
    print(f"  ≤trigger 观察 episode: yes {ep_y}（含前 4 分钟段 {ep_ey}） / "
          f"no {ep_n}（含前 4 分钟段 {ep_en}）")
    print("  （窗口末段输家 ask 趋 0 属常态, 信号频率须以 rem≥60 段与网格命中量"
          "为准, 勿把 episode 总数当信号量）\n")

    if not rows:
        print("B/C: 网格零命中（trigger 过高或数据问题）")
        return
    df = pd.DataFrame(rows)
    assert not df.duplicated(["event_start", "look", "margin"]).any(), \
        "每窗每格应至多一单"
    assert ((df["fill"] > 0) & (df["fill"] <= trigger)).all(), "fill 越界"
    assert df["rem"].min() >= 1, "入场 rem 须 ≥1"
    assert np.isfinite(df[["v", "d_pred", "fill", "known_n"]].to_numpy()).all()
    df["date"] = pd.to_datetime(df["event_start"], unit="s").dt.date.astype(str)
    dpos = pd.factorize(df["date"])[0]
    half = len(np.unique(df["date"])) // 2
    df["h"] = np.where(dpos < half, "h1", "h2")

    # ---------- B: 网格决策表（现行三腿口径） ----------
    print("B 网格主线决策表（现行口径 = 三腿: ask≤trigger × D_pred 越 ±M × 方向腿"
          "v 与下注方向一致; 每格 = 该配置下全量窗口命中, 单窗一单）")
    print(f"  {'look':>4} {'M($)':>5} {'n':>5} {'WR%':>6} {'fill':>6} "
          f"{'EV/注':>8} {'P&L(U)':>9} {'日正':>5} {'h1/h2':>8} {'R<60':>5}")
    best = None
    for (L, m), s in df.groupby(["look", "margin"], sort=True):
        pl = pl_of(s)
        k = int(s["settle_won"].sum())
        dg = s.groupby("date")["settle_won"].sum()
        if best is None or pl.sum() > best[1]:
            best = ((L, m), pl.sum())
        n60 = int((s["rem"] < 60).sum())
        print(f"  {L:>4} {m:>5.0f} {len(s):>5} {k/len(s)*100:>6.1f} "
              f"{s['fill'].mean():>6.3f} {pl.mean():>+8.3f} {pl.sum():>+9.1f} "
              f"{int((dg > 0).sum()):>5} {len(s[s['h']=='h1']):>3}"
              f"/{len(s[s['h']=='h2']):<3} {n60:>5}")
    print(f"  ※ in-sample P&L argmax: look={best[0][0]} M=${best[0][1]:.0f}, "
          f"+{best[1]:.0f}U——仅样本内参考, 复验口径同 dog@0.2 09-15\n")

    # ---------- B′: 方向腿对照 ----------
    # 对 M=0 各 look + argmax 行 + 自然对照格补扫**无腿**（并入前历史口径）行,
    # 同格对照量化第 3 腿剔除的「速度向恶化方向」单贡献。
    grid_cells = {(L, m) for L in looks for m in margins}
    comp = sorted(({(L, 0.0) for L in looks} | {best[0], (15.0, 10.0)})
                  & grid_cells)
    r0 = scan_legless(events, trigger, comp)
    d0 = pd.DataFrame(r0) if r0 else pd.DataFrame(
        columns=["event_start", "look", "margin", "fill", "settle_won"])
    print("B′ 方向腿对照（同一格两行: 无腿=并入前历史口径 → 现行含方向腿;")
    print("  差异即第 3 腿剔除的恶化方向单——buy yes 需 v≥0 / buy no 需 v≤0, ")
    print("  恶化方向禁入、观察延续至 v 转好或窗口结束, v==0 中性放行）")
    for L, m in comp:
        tag = f"  look={L:>2} M=${m:>3.0f}"
        stat_line(f"{tag}  无腿(对照)  ", d0[(d0["look"] == L) & (d0["margin"] == m)])
        stat_line(f"{tag}  现行(含方向腿)", df[(df["look"] == L) & (df["margin"] == m)])
    print()

    # ---------- C: 分桶统计 ----------
    for tag, (L, m) in (("C-1 P&L argmax 行", best[0]),
                        ("C-2 自然对照 L=15 M=10", (15, 10.0))):
        s = df[(df["look"] == L) & (df["margin"] == m)].copy()
        if not len(s):
            print(f"{tag} (look={L} M=${m:.0f}): n=0\n")
            continue
        s["rem_b"] = np.where(s["rem"] >= 180, "rem≥180",
                              np.where(s["rem"] >= 60, "rem60..179", "rem<60"))
        av = s["v"].abs()
        s["v_b"] = np.where(av == 0, "|v|=0", np.where(av <= 0.05, "(0,0.05]",
                            np.where(av <= 0.2, "(0.05,0.2]", ">0.2")))
        ndays = s["date"].nunique()
        print(f"{tag}: look={L} M=${m:.0f} trigger={trigger}  n={len(s)}")
        stat_line("  总", s, ndays)
        for rb in ("rem≥180", "rem60..179", "rem<60"):
            gb = s[s["rem_b"] == rb]
            stat_line(f"  [{rb}]", gb, ndays)
        for vb in ("|v|=0", "(0,0.05]", "(0.05,0.2]", ">0.2"):
            gb = s[s["v_b"] == vb]
            stat_line(f"  |v| {vb:>9}", gb, ndays)
        print("  逐日:")
        for d, gb in sorted(s.groupby("date")):
            stat_line(f"    {d}", gb)
        print()

    # ---------- CSV 明细 ----------
    cols = ["date", "event_start", "tick_i", "side", "rem", "look", "margin",
            "d_pred", "fill", "v", "known_n", "buy10", "sell10", "buy120",
            "sell120", "depth_bid5", "depth_ask5", "settle_won", "actual_d"]
    out = df[cols].sort_values(["event_start", "look", "margin"])
    Path(args.csv).parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(args.csv, index=False)
    print(f"已导出逐笔明细 {args.csv}（{len(out)} 行; 预测侧列均不含结算信息, "
          f"供 02 增强评估/未来 OOS 免重扫）")


if __name__ == "__main__":
    main()
