#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v7 统计工具（GBM 专题）—— 与 v4 的判决口径同源。

复用/对齐的算法（v4 现役脚本里的同一套）:
  * 日级 bootstrap   = 14_tail_sweep_sigma.py:103 day_bootstrap / 32_tail_loss_features.py:101 boot_ci
  * 日级配对 Δ       = 38_tail_walk_gate.py:72 boot_delta / 33_tail_dev2sd.py:179 paired_ci
  * Wilson 区间      = 32_tail_loss_features.py:90 wilson

⚠️ 本专题的两条统计纪律（见 docs/gbm_2026-10-01.md §0）:
  1. **一律日级重采样**——同一天的单高度相关（同 regime、同行情），按行 bootstrap 会把
     区间算窄一个量级;
  2. **置换必须按日整块打乱标签**——按行打乱会低估零分布宽度（审计实测：光靠扫阈值就能
     刷出 +3.6U 的"提升"，p95 水平）。
"""
import random

import numpy as np

SEED = 42
REPS = 2000


def by_day(rows):
    """{date: [rows]}（rows 需带 'date' 键）。"""
    d = {}
    for r in rows:
        d.setdefault(r["date"], []).append(r)
    return d


def day_pnl(rows):
    """{date: 当日 P&L 合计}（rows 需带 'date' 与 'pnl'）。"""
    d = {}
    for r in rows:
        d[r["date"]] = d.get(r["date"], 0.0) + r["pnl"]
    return d


def boot_point(vals_by_day, reps=REPS, rng=None):
    """单臂：日级 bootstrap 的 95% 区间（对当日合计做有放回按日重采样）。"""
    rng = rng or random.Random(SEED)
    days = sorted(vals_by_day)
    if not days:
        return 0.0, 0.0
    tot = []
    for _ in range(reps):
        s = 0.0
        for _ in range(len(days)):
            s += vals_by_day[rng.choice(days)]
        tot.append(s)
    tot.sort()
    return tot[int(0.025 * len(tot))], tot[int(0.975 * len(tot))]


def boot_delta(base_day, new_day, reps=REPS, rng=None):
    """配对 Δ：同一天两版策略的 P&L 差，按日重采样 → (obs, lo, hi)。

    base_day / new_day 都是 {date: pnl}; 只在**两版都出现的日**上配对（缺一边的日算 0,
    与 33/42 的 paired 口径一致）。
    """
    rng = rng or random.Random(SEED)
    days = sorted(set(base_day) | set(new_day))
    if not days:
        return 0.0, 0.0, 0.0
    delta = {d: new_day.get(d, 0.0) - base_day.get(d, 0.0) for d in days}
    obs = sum(delta.values())
    tot = []
    for _ in range(reps):
        s = 0.0
        for _ in range(len(days)):
            s += delta[rng.choice(days)]
        tot.append(s)
    tot.sort()
    return obs, tot[int(0.025 * len(tot))], tot[int(0.975 * len(tot))]


def boot_ci_mean(vals_by_day, reps=REPS, rng=None):
    """日级 bootstrap 的均值区间（用于 AUC/Brier 这类**逐笔**指标的按日重采样）。

    vals_by_day = {date: [每笔的值]} → 返回 (obs, lo, hi)；重采样日、对日内全部取均值。
    """
    rng = rng or random.Random(SEED)
    days = sorted(vals_by_day)
    if not days:
        return 0.0, 0.0, 0.0
    flat = [v for d in days for v in vals_by_day[d]]
    obs = float(np.mean(flat)) if flat else 0.0
    tot = []
    for _ in range(reps):
        acc = []
        for _ in range(len(days)):
            acc.extend(vals_by_day[rng.choice(days)])
        tot.append(float(np.mean(acc)) if acc else 0.0)
    tot.sort()
    return obs, tot[int(0.025 * len(tot))], tot[int(0.975 * len(tot))]


def boot_delta_mean(diff_by_day, reps=REPS, rng=None):
    """逐笔配对指标（如 Brier 差）的日级配对 bootstrap → (obs, lo, hi)。

    diff_by_day = {date: [逐笔差值]}（正 = 新版更好/更差, 由调用方定义方向）。
    """
    rng = rng or random.Random(SEED)
    days = sorted(diff_by_day)
    if not days:
        return 0.0, 0.0, 0.0
    flat = [v for d in days for v in diff_by_day[d]]
    obs = float(np.mean(flat)) if flat else 0.0
    tot = []
    for _ in range(reps):
        acc = []
        for _ in range(len(days)):
            acc.extend(diff_by_day[rng.choice(days)])
        tot.append(float(np.mean(acc)) if acc else 0.0)
    tot.sort()
    return obs, tot[int(0.025 * len(tot))], tot[int(0.975 * len(tot))]


def wilson(k, n):
    """Wilson 95% 区间（小 n 不越界）。"""
    if not n:
        return 0.0, 0.0
    z, p = 1.959964, k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return max(0.0, c - h), min(1.0, c + h)


def perm_p(obs, null_vals, greater=True):
    """置换 p 值: (计数+1)/(nperm+1)，与 38/41 同式。"""
    n = len(null_vals)
    if n == 0:
        return float("nan")
    if greater:
        cnt = sum(1 for v in null_vals if v >= obs)
    else:
        cnt = sum(1 for v in null_vals if v <= obs)
    return (cnt + 1) / (n + 1)


def block_shuffle_labels(labels_by_day, rng):
    """按日整块打乱：把「日 → 该日标签列表」的对应关系整体置换（日内顺序保持）。

    返回新的 {date: [labels]}（键不变，值是**别的日**的标签——但天数相同的日之间
    长度可能不同，故按「日序号」重排：第 k 天拿到第 π(k) 天的标签）。
    ⚠️ 长度不等时用「重采样式」：随机抽一个日的标签整体替换（有放回），这是标准的
    block bootstrap/permutation 做法，零分布宽度不受长度不匹配影响。
    """
    days = sorted(labels_by_day)
    return {d: labels_by_day[rng.choice(days)] for d in days}
