#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
触底(≤0.2)后翻转事件的分布 × 浅洞带 —— 散点图（2026-09-17）。

用户需求：「把回测数据与纸面数据的所有 <0.2 又翻转的数据，和过滤带一起画个散点图，
看它的分布情况。」

口径：
  翻转 = 被砸到 ≤0.2 的那一侧（狗侧）最终结算为赢 → 我们 0.2 买进去会赢的那一笔。
  样本 = 该事件首个触底 tick 的观测（首触不重试，每 event_start 恰一行）。
  横轴 = dist_s = sgn·(现货 − 锚)/锚·1e4/σ   （sgn: 狗侧 yes +1 / no −1）
  纵轴 = dist_t = sgn·(TWAP − 锚)/锚·1e4/σ   （结算线自身的位移 = 真洞深）
         或 基差 = dist_s − dist_t（按侧别翻转后，两侧落同一坐标、可直接比较）
  带   = 现行组合带 dist_s ∈ (yes −0.6, 0) / (no −1.0, 0)；灰色虚竖线 = R1 旧带 −0.5

  ⚠️ 纵轴以 σ 为单位。σ = hist_bps（前 ≤18 个已完窗口 |close−anchor| 均值）。
     换算成美元要乘 锚×σ/1e4；脚本会在图上标注纸面期的中位换算系数。

  ⚠️ 别用「未按侧别翻转的 现货−TWAP」当纵轴：横轴 dist_s 已翻转，混着画两侧必然呈 V 形，
     但那是坐标产物（斜率 ±1），不是「两侧需要不同过滤区间」的证据。该面板已移除。

用法: python 12_flip_scatter.py [--bt data/btc] [--paper data/v4] [--out python/v4/flip_scatter.png]
"""
import argparse
import importlib.util
import sys
import unicodedata
from pathlib import Path

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

BASE = Path(__file__).resolve().parent

# 中文字体（按可用性回退）
plt.rcParams["font.sans-serif"] = ["Arial Unicode MS", "PingFang HK", "PingFang SC",
                                   "Heiti TC", "Hiragino Sans GB", "Songti SC"]
plt.rcParams["axes.unicode_minus"] = False

CUR = {"yes": -0.6, "no": -1.0}       # 现行组合带下限
R1 = -0.5                              # R1 旧带（双侧）参照

C_ALL = "#c8c8c8"    # 全部触底事件（背景）
C_WIN = "#e8743b"    # 翻转（狗侧赢）
C_WIN3 = "#c0392b"   # 翻转 且 过了急跌+时间腿 ← 策略真正会考虑的那批
C_BAND = "#2e86c1"   # 带区间


def dw(s):
    """终端显示宽度（CJK 全角算 2 列）。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def pad(s, n):
    return str(s) + " " * max(0, n - dw(s))


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(name, BASE / fname)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build(args):
    r1 = _load("r1", "01_backtest_r1.py")
    r8 = _load("r8", "08_dist_t_band_refit.py")

    def prep(df, key):
        df = df.copy()
        df["seg"] = key
        df["sgn"] = np.where(df["side"] == "yes", 1.0, -1.0)
        # 纵轴基差口径（2026-09-18）：
        #   basis_flip = 按侧别翻转后的基差 = dist_s − dist_t → 两侧落在同一坐标，可直接比较，
        #                是判断「两侧要不要不同过滤区间」该用的坐标
        #   basis_raw（已弃用，勿再用作看图纵轴）= 现货 − TWAP 原始 σ 值，未按侧别翻转：
        #                与翻转过横轴混画时两侧必然呈 V 形（斜率 ±1），是代数产物不是行情结构
        df["basis_flip"] = df["dist_s"] - df["dist_t"]
        # df["basis_raw"] = (df["dist_s"] - df["dist_t"]) * df["sgn"]
        lo = df["side"].map(CUR)
        df["in_band"] = (df["dist_s"] > lo) & (df["dist_s"] < 0)
        # 另外两条腿（急跌 × 时间）——带只在这条子集上起作用
        df["legs2"] = df["m_45"].ge(0.40) & df["rem"].gt(180)
        df["won"] = df["settle_won"] == 1
        return df[df["settle_won"].notna() & df["dist_s"].notna() & df["dist_t"].notna()]

    bt = prep(r1.extract(args.bt), "bt")
    pp = r8.load_paper(Path(args.paper))
    if "gate_reason" in pp:                       # 熔断/闸门行：默认剔除
        pp = pp[pp["gate_reason"].isna()]
    k = None
    if len(pp) and "hist_bps" in pp and "anchor" in pp:
        k = float((pp["anchor"] * pp["hist_bps"] / 1e4).median())   # σ → 美元
    pp = prep(pp, "pp")
    labels = {k_: f"{nm} {g['date'].min()}~{g['date'].max()}"
              for k_, nm in (("bt", "回测"), ("pp", "纸面"))
              if len(g := (bt if k_ == "bt" else pp))}
    return pd.concat([bt, pp], ignore_index=True), k, labels


def draw_panel(ax, s, ycol, side, ylab, dollar_k=None):
    """一个面板：x = dist_s, y = ycol。side=None 表示两侧合并。"""
    sub = s if side is None else s[s["side"] == side]
    # 带区间
    if side is None:
        ax.axvspan(CUR["no"], CUR["yes"], color=C_BAND, alpha=0.16, zorder=0)
        ax.axvspan(CUR["yes"], 0, color=C_BAND, alpha=0.26, zorder=0)
        ax.axvline(R1, color="k", ls=":", lw=1.0, zorder=1)
        n_in = int(sub["in_band"].sum())
        inb = sub["in_band"]
    else:
        ax.axvspan(CUR[side], 0, color=C_BAND, alpha=0.26, zorder=0)
        ax.axvline(R1, color="k", ls=":", lw=1.0, zorder=1)
        n_in = int(sub["in_band"].sum())
        inb = sub["in_band"]
    # 分层：灰色底 = 全部触底；橙 = 翻转；空心红圈 = 翻转且过急跌+时间腿。
    # 合并面板（side=None）用 marker 区分 yes(○)/no(△)——两侧的纵轴含义不同，
    # 不区分会把两条支线看成一条。
    groups = [(None, sub)] if side is not None else [
        ("yes", sub[sub["side"] == "yes"]), ("no", sub[sub["side"] == "no"])]
    mk = {None: "o", "yes": "o", "no": "^"}
    for sk, gg in groups:
        m = mk[sk]
        tag = "" if sk is None else ("yes " if sk == "yes" else "no ")
        ax.scatter(gg["dist_s"], gg[ycol], s=5 if sk is None else 7, c=C_ALL,
                   alpha=0.45, linewidths=0, marker=m, zorder=2,
                   label=f"全部触底 {tag}({len(gg)})")
        w = gg[gg["won"]]
        ax.scatter(w["dist_s"], w[ycol], s=16, c=C_WIN, alpha=0.9, linewidths=0,
                   marker=m, zorder=3, label=f"翻转 {tag}({len(w)})")
        w3 = w[w["legs2"]]
        ax.scatter(w3["dist_s"], w3[ycol], s=46, facecolors="none", edgecolors=C_WIN3,
                   linewidths=1.4, marker=m, zorder=4,
                   label=f"翻转·过急跌+时间腿 {tag}({len(w3)})")
    # y 轴按分位裁剪：no 侧 dist_t 有 −8σ 级离群，全量显示会把主云压成一条线
    yv = sub[ycol].dropna()
    if len(yv) > 20:
        q0, q1 = yv.quantile(0.005), yv.quantile(0.995)
        pad_ = 0.08 * (q1 - q0)
        ax.set_ylim(q0 - pad_, q1 + pad_)
    # 带参考线
    ax.axhline(0, color="k", lw=0.6, alpha=0.35, zorder=1)
    if ycol == "dist_t":
        # 对角虚线 y = x（现货＝TWAP ⇒ dist_s = dist_t），按可见范围截断
        x0, x1 = ax.get_xlim()
        y0, y1 = ax.get_ylim()
        a, b_ = max(x0, y0), min(x1, y1)
        if a < b_:
            ax.plot([a, b_], [a, b_], color="k", lw=0.8, ls="--", alpha=0.45, zorder=1)
        ax.annotate("对角虚线: dist_s = dist_t（现货＝TWAP，即基差为 0）",
                    xy=(0.03, 0.03), xycoords="axes fraction",
                    fontsize=7.5, color="#444")
    # 统计标注
    b = sub[sub["legs2"]]
    txt = (f"触底事件 {len(sub)}，翻转 {int(sub['won'].sum())}\n"
           f"{'带内合计' if side is None else '该侧带内'} {n_in} 条\n"
           f"「急跌+时间」两腿 {len(b)} 条：\n"
           f"　带内 {int(b['in_band'].sum())} → 翻转 {int((b['in_band'] & b['won']).sum())}\n"
           f"　带外 {int((~b['in_band']).sum())} → 翻转 {int((~b['in_band'] & b['won']).sum())}")
    # 统计框放到点最少的角落（按数据坐标在四个象限里的落点比例选）
    fx = (sub["dist_s"] - ax.get_xlim()[0]) / np.diff(ax.get_xlim())[0]
    fy = (sub[ycol] - ax.get_ylim()[0]) / np.diff(ax.get_ylim())[0]
    quad = ((fx > 0.45).astype(int) + 2 * (fy > 0.45).astype(int))
    corner = int(quad.value_counts().reindex([0, 1, 2, 3], fill_value=0).idxmin())
    cx = 0.02 if corner in (0, 2) else 0.98
    cy = 0.97 if corner in (2, 3) else 0.03
    ax.text(cx, cy, txt, transform=ax.transAxes,
            va="top" if corner in (2, 3) else "bottom",
            ha="left" if corner in (0, 2) else "right",
            fontsize=8, linespacing=1.5,
            bbox=dict(fc="white", ec="#bbb", alpha=0.92, pad=3.5))
    if side is None:
        ylab += "\n（圆点=yes 侧，三角=no 侧）"
    ax.set_xlabel("dist_s  （负 = 现货在狗败侧的坑里；0 = 现货回到锚）", fontsize=9)
    ax.set_ylabel(ylab, fontsize=9)
    ax.tick_params(labelsize=8)
    ax.grid(alpha=0.15, lw=0.5)
    if ycol == "basis_flip" and dollar_k:
        ax.annotate(f"1σ ≈ {dollar_k:.0f} 美元（纸面期中位）", xy=(0.98, 0.03),
                    xycoords="axes fraction", ha="right", fontsize=7.5, color="#444")


def main():
    ap = argparse.ArgumentParser(description="触底后翻转事件的分布 × 浅洞带")
    ap.add_argument("--bt", default=str(BASE.parent.parent / "data" / "btc"))
    ap.add_argument("--paper", default=str(BASE.parent.parent / "data" / "v4"))
    ap.add_argument("--out", default=str(BASE / "flip_scatter.png"))
    ap.add_argument("--xlim", default="-3.0,2.5", help="dist_s 显示范围（排除极端离群）")
    args = ap.parse_args()
    xlo, xhi = (float(v) for v in args.xlim.split(","))

    alld, k, labels = build(args)
    print(f"样本合计 {len(alld)} 行（每 event_start 一行，首触不重试）")
    for key, g in alld.groupby("seg"):
        print(f"  {labels[key]}: n={len(g)}  翻转={int(g['won'].sum())}"
              f" ({g['won'].mean()*100:.1f}%)  yes/no="
              f"{int((g['side']=='yes').sum())}/{int((g['side']=='no').sum())}")
    if k:
        print(f"  偏移 σ→美元 换算系数（锚×σ/1e4 中位）: {k:.1f}")

    # ============ 图 1：散点 2 行 × 3 列 ============
    segs = [s for s in ("bt", "pp") if s in labels]
    nm = lambda s: labels[s]  # noqa: E731
    fig, axes = plt.subplots(len(segs), 3, figsize=(19, 5.3 * len(segs)), squeeze=False)
    for i, seg in enumerate(segs):
        s = alld[alld["seg"] == seg]
        draw_panel(axes[i][0], s, "dist_t", "yes",
                   "dist_t  （TWAP 自身位移 = 真洞深）")
        draw_panel(axes[i][1], s, "dist_t", "no",
                   "dist_t  （TWAP 自身位移 = 真洞深）")
        draw_panel(axes[i][2], s, "basis_flip", None,
                   "基差（已按侧别翻转）dist_s − dist_t，σ\n>0 = 现货已朝狗赢方向领先 TWAP", k)
        axes[i][0].set_title(f"{nm(seg)} · yes 侧（UP 被砸）", fontsize=11)
        axes[i][1].set_title(f"{nm(seg)} · no 侧（DOWN 被砸）", fontsize=11)
        axes[i][2].set_title(f"{nm(seg)} · 两侧合并·共同坐标（两侧塌成一条线）", fontsize=11)
        # 已删除「现货−TWAP 原始偏移（未翻转）」面板（2026-09-18 用户要求）：
        # 它把翻转过横轴与未翻转纵轴混画，两侧会呈 V 形——纯坐标产物，
        # 曾被误读成「两侧不是同一个过滤区间」。要看两侧差异请用第 3 列
        # （共同坐标）+ 12 脚本里的分段/斜率检验，不要用未翻转纵轴。
        # draw_panel(axes[i][3], s, "basis_raw", None,
        #            "现货 − TWAP 原始偏移（σ，未翻转）\n>0 = binPrice 高于 TWAP", k)
        for j in range(3):
            axes[i][j].set_xlim(xlo, xhi)
    handles = [
        Line2D([], [], ls="", marker="o", ms=4, mfc=C_ALL, mec="none",
               label="全部触底事件（○ yes / △ no）"),
        Line2D([], [], ls="", marker="o", ms=5, mfc=C_WIN, mec="none",
               label="翻转 = 被砸那侧最终赢"),
        Line2D([], [], ls="", marker="o", ms=8, mfc="none", mec=C_WIN3, mew=1.4,
               label="翻转 且 过了急跌+时间腿（带只在这批上有作用）"),
        Patch(fc=C_BAND, alpha=0.28, label="现行浅洞带 dist_s ∈ (yes −0.6 / no −1.0, 0)"),
        Line2D([], [], color="k", ls=":", label="R1 旧带下限 −0.5"),
        Line2D([], [], color="k", ls="--", lw=0.8, label="现货＝TWAP（基差 0）"),
        Line2D([], [], color="w", ls="", label=""),
        Line2D([], [], color="w", ls="",
               label="第 3 列纵轴已按侧别翻转，两侧可直接比较（未翻转纵轴会画出 V 形假象，已移除）"),
    ]
    fig.legend(handles=handles, loc="lower center", ncol=3, fontsize=9.5,
               bbox_to_anchor=(0.5, 0.002), frameon=False)
    fig.suptitle("触底(ask≤0.2)后翻转事件的分布 × 浅洞带   "
                 "（翻转 = 被砸那一侧最终赢 = 0.2 买入会赢的那笔）", fontsize=14)
    fig.tight_layout(rect=[0, 0.075, 1, 0.965])
    fig.savefig(args.out, dpi=140, facecolor="white")
    print(f"\n图 1 已写出: {args.out}")

    # ============ 图 2：dist_s 边沿分布 + 分桶翻转率 ============
    cols = [(nm(s), alld[alld["seg"] == s]) for s in segs] + [("全部合并", alld)]
    fig2, ax2r = plt.subplots(2, len(cols), figsize=(6 * len(cols), 9.5), squeeze=False)
    bins = np.arange(xlo, xhi + 0.1, 0.15)
    ctr = (bins[:-1] + bins[1:]) / 2

    def decorate(ax, show_counts):
        ax.axvspan(CUR["no"], CUR["yes"], color=C_BAND, alpha=0.16, zorder=0)
        ax.axvspan(CUR["yes"], 0, color=C_BAND, alpha=0.26, zorder=0)
        ax.axvline(CUR["yes"], color=C_BAND, lw=1.2, alpha=0.8, zorder=1)
        ax.axvline(CUR["no"], color=C_BAND, lw=1.2, alpha=0.8, zorder=1)
        ax.axvline(R1, color="k", ls=":", lw=1.0, zorder=1)
        ax.set_xlim(xlo, xhi)
        ax.tick_params(labelsize=8)
        ax.grid(alpha=0.15, lw=0.5)
        if show_counts:
            ax.legend(fontsize=8, framealpha=0.95)

    for j, (title, s) in enumerate(cols):
        # 上：事件数（全部触底 vs 翻转 vs 翻转且过两腿）
        ax = ax2r[0][j]
        ax.hist(s["dist_s"], bins=bins, color=C_ALL, alpha=0.85,
                label=f"全部触底 n={len(s)}", zorder=2)
        ax.hist(s.loc[s["won"], "dist_s"], bins=bins, color=C_WIN, alpha=0.9,
                label=f"翻转 n={int(s['won'].sum())}", zorder=3)
        b = s[s["legs2"]]
        ax.hist(b.loc[b["won"], "dist_s"], bins=bins, color=C_WIN3, alpha=0.55,
                label=f"翻转·过两腿 n={int((b['legs2'] & b['won']).sum())}", zorder=4)
        ax.set_title(f"{title} · 数量", fontsize=11)
        ax.set_ylabel("事件数", fontsize=9)
        decorate(ax, True)

        # 下：分桶翻转率 + 二项误差棒；横线 = 该子集整体翻转率
        ax = ax2r[1][j]
        sub = s[s["legs2"]]                    # 只看过了急跌+时间腿的（带的适用域）
        idx = np.digitize(sub["dist_s"], bins) - 1
        keep = (idx >= 0) & (idx < len(ctr))
        idx, won = idx[keep], sub["won"].to_numpy()[keep]
        n_b = np.bincount(idx, minlength=len(ctr))
        k_b = np.bincount(idx, weights=won, minlength=len(ctr))
        ok = n_b >= 15                          # 样本太少的桶不画（噪声）
        rate = np.where(ok, k_b / np.maximum(n_b, 1) * 100, np.nan)
        se = np.where(ok, np.sqrt(rate / 100 * (1 - rate / 100) / np.maximum(n_b, 1)) * 100,
                      np.nan)
        ax.errorbar(ctr[ok], rate[ok], yerr=se[ok], fmt="o-", ms=6, lw=1.6,
                    color=C_WIN3, ecolor=C_WIN3, elinewidth=1.2, capsize=3, zorder=4,
                    label="分桶翻转率 ±1 标准误")
        base = sub["won"].mean() * 100
        ax.axhline(base, color="k", lw=1.2, alpha=0.7, zorder=3,
                   label=f"该子集整体翻转率 {base:.1f}%（n={len(sub)}）")
        ax.bar(ctr, n_b, width=0.13, color=C_ALL, alpha=0.35, zorder=1,
               label="桶内样本数（右轴）")
        ax.set_ylim(0, max(45, np.nanmax(rate) * 1.35 if ok.any() else 45))
        ax2_ = ax.twinx()      # 柱子的独立刻度；只在最右列显示数字，免得和邻panel 的
        ax2_.set_ylim(0, n_b.max() * 3.2)   # 左轴刻度挤在一起
        ax2_.set_ylabel("桶内样本数", fontsize=8, color="#888")
        ax2_.tick_params(labelsize=7, colors="#888", labelright=(j == len(cols) - 1))
        ax2_.set_zorder(0)
        ax.set_zorder(1)
        ax.patch.set_visible(False)
        ax.set_title(f"{title} · 翻转率（仅过急跌+时间腿）", fontsize=11)
        ax.set_ylabel("翻转率 %", fontsize=9)
        ax.set_xlabel("dist_s  （负 = 现货在狗败侧的坑里）", fontsize=9)
        decorate(ax, True)

    fig2.suptitle("dist_s 分布与分桶翻转率   "
                  "（蓝区 = 现行浅洞带，两侧下限不同：yes −0.6 / no −1.0；灰点线 = R1 旧带 −0.5）",
                  fontsize=13)
    fig2.tight_layout(rect=[0, 0, 1, 0.955])
    p2 = str(Path(args.out).with_name(Path(args.out).stem + "_hist.png"))
    fig2.savefig(p2, dpi=140, facecolor="white")
    print(f"图 2 已写出: {p2}")

    # ============ 文字摘要 ============
    print("\n" + "=" * 84)
    print("翻转事件落在哪里（仅「过了急跌+时间腿」的子集 —— 带只在这批上有作用）")
    print("=" * 84)
    heads = [(labels[k] if k in labels else "全部合并") for k in segs + ["全部合并"]]
    w = max(dw(h) for h in heads) + 2
    print(f"  {pad('期间', w)}{pad('两侧', 6)}{'过两腿n':>8}{'带内n':>7}{'带内翻转':>9}"
          f"{'带外n':>7}{'带外翻转':>9}{'带内翻率':>9}{'带外翻率':>9}")
    for key in segs + ["全部合并"]:
        s = alld if key == "全部合并" else alld[alld["seg"] == key]
        for side in ("yes", "no", "两侧"):
            g = s[s["legs2"]] if side == "两侧" else s[s["legs2"] & (s["side"] == side)]
            if not len(g):
                continue
            i, o = g[g["in_band"]], g[~g["in_band"]]
            f = lambda d: (d["won"].sum() / len(d) * 100) if len(d) else float("nan")  # noqa: E731
            print(f"  {pad(labels[key] if key in labels else key, w)}{pad(side, 6)}"
                  f"{len(g):>8}{len(i):>7}{int(i['won'].sum()):>9}"
                  f"{len(o):>7}{int(o['won'].sum()):>9}{f(i):>8.1f}%{f(o):>8.1f}%")


if __name__ == "__main__":
    main()
