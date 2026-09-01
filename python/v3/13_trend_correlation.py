#!/usr/bin/env python3
"""
v3 flip/follow × BTC 趋势相关性分析 —— 2026-09-02 探索结论落盘。

数据: data/btc 事件 ticks 内 bin.price（1s Binance BTC 价, 决策时刻可知）。
趋势度量（对齐: 触发侧为 yes 取原值 / no 取负, aligned>0 = BTC 与触发侧同向）:
  ret_pre{10,30,60,120} = 穿越前 N 秒 BTC 收益
  ret_win              = 窗口起点 → 穿越时刻收益（决策时刻已知）
  ret_post10           = 穿越 → +10s（确认时刻）
  ret_full             = 整窗收益（未来, 仅解剖用）

核心结论（14 天全量, 与 06/07/10/11/12 同数据同口径）:
  1. 平凡层: 穿越几乎总在 BTC 顺触发侧方向运动后发生（92.9%, r=+0.865）
     —— bid 价格即方向定价, 无交易价值, 是理解背景。
  2. flip × 趋势 = 中性: 信号发生分布与无信号穿越一致（C1/C2 纯市场情绪条件）;
     胜负与穿越前趋势无稳定分层（flip 的 alpha 与 BTC 路径无关）。
     flip 赢=整窗 BTC 转对侧方向是平凡恒等式（flip 买对侧）, 非发现。
  3. follow 全量 × 趋势: 无相关（|r|<0.03）。
  4. 【新候选】逆势穿越 follow: 窗口内 BTC 与触发侧反向（6% 罕见事件）时
     买触发侧: n=220, WR 80.0% @ fill 0.747, EV +0.0535/股（2U/注约 +31U/14 天）。
     OOS 全绿: test2d +0.066 / 长窗 +0.078 / 双半同号 / 逐日 10/13。
     机制: 逆势抢跑事件对侧整窗再穿越概率低（only 率 72%）; 决策时刻完全可知。
     与 flip 独立（事件重叠 8/220）。
  5. 候选限定: fill<0.73 区 EV +0.116（n=79）; 触发侧 bid@10s 崩溃越深 EV 越高;
     对侧 bid@10s 过滤无增益（反而降 EV）; 3/13 天负; n 仍小, 纸面验证 + 09-15 复验。
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from v2.lib import load_events, extract_cross

REPO = Path(__file__).resolve().parent.parent.parent
DATA = str(REPO / "data" / "btc")
STAKE = 2.0
FLIP_C1, FLIP_C2 = 0.73, 0.66


def bin_price(ticks, j):
    if j < 0:
        return None
    j = min(j, len(ticks) - 1)
    p = (ticks[j].get("bin") or {}).get("price") or 0.0
    return p if p > 0 else None


def r(a, b):
    return None if (not a or not b) else (b - a) / a


def load():
    events = load_events(DATA)
    obs = extract_cross(events, "outcome", confirm_s=(10,))
    by_start = {e["start_time"]: e for e in events}
    rows = []
    for o in obs:
        ev = by_start[o["event_start"]]
        ticks = ev.get("ticks") or []
        i = o["i"]
        pi = bin_price(ticks, i)
        d = dict(o)
        d["ret_pre10"] = r(bin_price(ticks, i - 10), pi)
        d["ret_pre30"] = r(bin_price(ticks, i - 30), pi)
        d["ret_pre60"] = r(bin_price(ticks, i - 60), pi)
        d["ret_pre120"] = r(bin_price(ticks, i - 120), pi)
        d["ret_win"] = r(bin_price(ticks, 0), pi)
        d["ret_post10"] = r(pi, bin_price(ticks, i + 10))
        d["ret_full"] = r(bin_price(ticks, 0), bin_price(ticks, len(ticks) - 1))
        # 各决策时刻盘口（实时代理检查用）
        def pm_field(j, f):
            v = (ticks[min(j, len(ticks) - 1)].get("pm") or {}).get(f) or 0.0
            return v if v > 0 else None
        d["oth_bid_i"] = pm_field(i, f"{o['other']}_bid")
        d["oth_bid_10"] = pm_field(i + 10, f"{o['other']}_bid")
        d["side_bid_10"] = pm_field(i + 10, f"{o['side']}_bid")
        d["flip_won"] = 1 - d["won"]
        rows.append(d)
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["event_start"], unit="s").dt.date.astype(str)
    is_yes = df["side"].eq("yes")
    for c in ("ret_pre10", "ret_pre30", "ret_pre60", "ret_pre120",
              "ret_win", "ret_post10", "ret_full"):
        df["aligned_" + c] = np.where(is_yes, df[c], -df[c])
    return df


def stats(d, fill_key="fill10s", won_key="won", min_n=10):
    d = d[d[fill_key].gt(0)].dropna(subset=[fill_key])
    n = len(d)
    if n < min_n:
        return None
    wr = d[won_key].mean()
    fill = d[fill_key].mean()
    return n, wr, fill, wr - fill


def show(name, s):
    if s is None:
        print(f"  {name}: n<10 跳过")
        return
    n, wr, fill, ev = s
    print(f"  {name}: n={n:4d} WR {wr*100:5.1f}% fill {fill:.3f} EV {ev:+.4f}")


def bucket(df, col, name, fill_key="fill10s", won_key="won", sign=True):
    d = df.dropna(subset=[col, fill_key])
    if sign:
        pos, neg = d[d[col] > 0], d[d[col] < 0]
        print(f"\n◆ {name}（{col}, 正=顺势/负=逆势, n={len(d)}）")
        show("顺势 (aligned>0)", stats(pos, fill_key, won_key))
        show("逆势 (aligned<0)", stats(neg, fill_key, won_key))
    else:
        print(f"\n◆ {name}（{col} 分位数, n={len(d)}）")
        qs = [d[col].quantile(q) for q in (0.2, 0.4, 0.6, 0.8)]
        bounds = [-np.inf] + qs + [np.inf]
        for k in range(5):
            lo, hi = bounds[k], bounds[k + 1]
            if k == 0:
                m = d[col].between(lo, hi, inclusive="neither")
            elif k < 4:
                m = d[col].gt(lo) & d[col].le(hi)
            else:
                m = d[col].gt(lo)
            show(f"Q{k+1} ({lo:+.4f},{hi:+.4f}]", stats(d[m], fill_key, won_key))


def oos_full(df, name):
    """逆势 follow 候选完整 OOS 体检。"""
    print(f"\n=== {name} 体检（fill=fill10s follow 口径） ===")
    show("全量", stats(df))
    show("train ≤08-28", stats(df[df["date"].le("2026-08-28")]))
    show("test2d 08-29/30", stats(df[df["date"].isin(("2026-08-29", "2026-08-30"))]))
    show("长窗 08-27~30", stats(df[df["date"].ge("2026-08-27")]))
    hd = sorted(df["date"].unique())
    m1 = int(len(hd) * 0.6)
    s1, s2 = stats(df[df["date"].isin(hd[:m1])]), stats(df[df["date"].isin(hd[m1:])])
    if s1 and s2:
        print(f"  双半: H1 {s1[1]*100:.1f}%/EV{s1[3]:+.4f} | H2 {s2[1]*100:.1f}%/EV{s2[3]:+.4f} "
              f"{'✓ 同号' if s1[3] > 0 and s2[3] > 0 else '✗ 反号'}")
    day_rows = []
    for d, g in df.groupby("date"):
        s = stats(g)
        if s:
            day_rows.append((d, s[0], s[1], s[3]))
    pos = sum(1 for _, _, _, e in day_rows if e > 0)
    print(f"  逐日 EV 正: {pos}/{len(day_rows)}")
    print("  " + "  ".join(f"{d[5:]}: {w*100:.0f}%/{e:+.2f}" for d, n, w, e in day_rows))


def main():
    df = load()
    n = len(df)
    print(f"数据: {DATA} | 穿越观测 {n}")

    # ── 1. 平凡层 ──
    d0 = df.dropna(subset=["ret_full", "aligned_ret_win"])
    print("\n[1. 平凡层] 穿越时刻对齐趋势为正: "
          f"{(d0['aligned_ret_win'] > 0).mean()*100:.1f}% | "
          f"触发侧=yes × BTC上涨 r={d0['side'].eq('yes').corr(d0['ret_win'].gt(0)):+.3f}")

    # ── 2. flip × 趋势 ──
    fd = df[df["trigger_bid"].gt(FLIP_C1) & df["flip_fill10s"].notna() &
            (1 - df["flip_fill10s"]).le(FLIP_C2)].copy()
    print(f"\n[2. flip × 趋势] 定稿信号 n={len(fd)}")
    for col in ("aligned_ret_pre10", "aligned_ret_pre30", "aligned_ret_win"):
        bucket(fd, col, "flip", fill_key="flip_fill10s", won_key="flip_won")
    bucket(fd, "aligned_ret_win", "flip 窗口趋势分桶", fill_key="flip_fill10s",
           won_key="flip_won", sign=False)
    bucket(fd, "aligned_ret_full", "flip 整窗趋势（解剖, 未来信息）",
           fill_key="flip_fill10s", won_key="flip_won", sign=False)
    for col in ("aligned_ret_pre30", "aligned_ret_win"):
        g = df.dropna(subset=[col])
        s = g[g["trigger_bid"].gt(FLIP_C1) & (1 - g["flip_fill10s"]).le(FLIP_C2)][col]
        ns = g[~g["trigger_bid"].gt(FLIP_C1)][col]
        print(f"  信号发生分布 {col}: flip med={s.median():+.4f} (n={len(s)}) | "
              f"无信号 med={ns.median():+.4f} (n={len(ns)})")

    # ── 3. follow 全量相关性 ──
    print("\n[3. follow × 趋势] 胜负相关系数（全量, |r|>0.03 才 95% 显著）")
    for col in ("ret_pre10", "ret_pre30", "ret_pre60", "ret_pre120", "ret_win"):
        c = df.dropna(subset=[col, "won"])[[col, "won"]].corr().iloc[0, 1]
        print(f"  won × {col}: {c:+.4f}")

    # ── 4. 逆势穿越 follow 候选 ──
    rev = df[df["aligned_ret_win"].lt(0)].copy()
    print(f"\n[4. 逆势穿越 follow] n={len(rev)}（顺逆对比如下）")
    for nm, m in (("逆势 (<0)", df["aligned_ret_win"].lt(0)),
                  ("顺势 (>0)", df["aligned_ret_win"].gt(0))):
        show(nm, stats(df[m]))
    oos_full(rev, "逆势穿越 follow 候选")
    print("\n  -- only/both 解剖（未来信息, 仅解剖） --")
    for c, g in rev.groupby("cls"):
        show(c, stats(g))
    print("  -- +10s 确认时刻分层（实时可交易） --")
    for lo, hi in ((0.0, 0.25), (0.25, 0.28), (0.28, 0.30), (0.30, 0.35), (0.35, 1.0)):
        show(f"对侧bid@10s∈[{lo:.2f},{hi:.2f})", stats(rev[rev["oth_bid_10"].between(lo, hi, inclusive="left")]))
    for lo, hi in ((0.0, 0.62), (0.62, 0.66), (0.66, 0.70), (0.70, 0.73), (0.73, 1.0)):
        show(f"触发侧bid@10s∈[{lo:.2f},{hi:.2f})", stats(rev[rev["side_bid_10"].between(lo, hi, inclusive="left")]))
    for lo, hi in ((0.0, 0.73), (0.73, 0.76), (0.76, 1.0)):
        show(f"fill∈[{lo:.2f},{hi:.2f})", stats(rev[rev["fill10s"].between(lo, hi, inclusive="left")]))

    # ── 5. 与 flip 独立 ──
    both_ev = set(fd["event_start"]) & set(rev["event_start"])
    print(f"\n[5. flip ∩ 逆势 follow] 事件重叠 {len(both_ev)} "
          f"（flip 内逆势占比 {fd['aligned_ret_win'].lt(0).mean()*100:.0f}%）")
    fpos = fd.copy()
    for nm, m in (("flip 顺势", fd["aligned_ret_win"].gt(0)),
                  ("flip 逆势", fd["aligned_ret_win"].lt(0))):
        g = fpos[m]
        if len(g) >= 10:
            wr = g["flip_won"].mean()
            fill = g["flip_fill10s"].mean()
            print(f"  {nm}: n={len(g)} WR {wr*100:.1f}% EV {wr-fill:+.4f}")


if __name__ == "__main__":
    main()
