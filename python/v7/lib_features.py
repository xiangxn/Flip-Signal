#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v7 特征层：把 tick 网格（lib_universe.GRID_COLS）变成模型输入矩阵。

三条设计红线（见 docs/gbm_2026-10-01.md §2 与数据审计）:

  1. **一律 σ 归一 / 无量纲**。`dev ≥ 63 美元` 在回测期 BTC 64k~81k 上是 9.8~7.7bps、
     实盘 84k 上是 7.5bps——同一常数在不同价位是**不同的真值**。故美元量一律除 σ（或
     取比值），跨标的/跨 regime 可迁移。
  2. **只用 `ts ≤ 当前 tick` 的数据**。所有动量腿是「回看」;「过去 N 窗」类基线只看
     **已完窗**（`lib_universe.hist_context`）。标签（最终 outcome）绝不进特征。
  3. **不做单调变换的重复**。`dev_abs` / `dev − 63` / `price − 0.83` 与 `dev` / `fill`
     对树模型是同一条分割轴的重复（gbm.md 里的 `*_minus_*`、`price_vs_*` 全属此类）。

砍掉 gbm.md 的字段及理由（逐条对账见报告 §2）:
  * `dev_abs` / `dev_minus_63` / `walk_minus_43` / `sigma_minus_40` / `price_vs_080/083`
    —— 常数平移 / 取绝对值的重复轴, 树学不到新东西;
  * 全部 PM 盘口类（`bid_ask_spread` / `book_depth` / `opposite_depth` / `hot_src` /
    `pm_price_change_*`）—— **回测里零方差**（14 天无一次单侧空簿、spread 恒 0.01）,
    实盘 41% 走 bid 兜底 ⇒ 两套数据不同分布, 训练即错; 只作实盘分组维度;
  * `volume_5m/1m/30s/10s` 四个绝对量 —— 换成两个**无量纲比率**（同窗内相对活跃度 +
    对历史基线比率）;
  * `dev_above_63_duration` / `dev_above_x_duration` —— 是**规则门槛的选择效应**
    （过了门槛才有这一行）, 不是市场量; `dev_shrink_duration` 同理（依赖阈值）;
  * 组合特征（`dev / volume_ratio` 等）—— 树自己会组合, 人工给只是加倍过拟合面。
"""
import numpy as np

# 列索引（与 lib_universe.GRID_COLS 对齐）
C_GI, C_TS, C_REM, C_SGN, C_FILL, C_SPOT = 0, 1, 2, 3, 4, 5
C_TWAP, C_DEV, C_WALK, C_BASIS, C_LAT, C_TWAGE = 6, 7, 8, 9, 10, 11
C_BUYV, C_SELLV, C_NTRD, C_VOLCUM = 12, 13, 14, 15
C_YESA, C_NOA = 16, 17                   # 追加列（B2 全窗模型用）

# ── 特征集（预注册; 见报告 §2） ───────────────────────────────────────────
FULL = [
    "fill",                # 热门侧有效价（= 盈亏平衡胜率本身）
    "sd_bps",              # σ / anchor × 1e4（当前波动 regime）
    "dev_sigma",           # sgn·(spot − anchor) / σ
    "walk_sigma",          # sgn·(twap − anchor) / σ —— 已写进结算线的位移（决策 #29 的闸）
    "basis_sigma",         # sgn·(spot − twap) / σ —— 还没写进去的缺口
    "slack_sigma",         # 冻结假设下**预计结算 dev** / σ（rem > 60 时 = dev_sigma）
    "rem",                 # 距闭市秒数
    "stage",               # 0/1/2 = t150/t60/listen（仅信号级模型; tick 级用 rem 取代）
    "spot_ret_10s_sigma",  # 近 10s 现货位移 / σ
    "dev_chg_10s_sigma",   # 近 10s dev 变化 / σ
    "walk_chg_10s_sigma",  # 近 10s walk 变化 / σ（慢线还在不在走）
    "giveback_sigma",      # 本窗截至此刻 max(dev) − dev（峰值回吐）/ σ
    "vol_rate_30s",        # 近 30s 成交量 / 本窗截至此刻均速（无量纲）
    "vol_ratio_prev",      # 本窗截至此刻均速 / 前 ≤48 窗中位（无量纲）
    "book_latency_ms",     # 盘口延迟（数据质量 + 行情状态代理）
]
CORE = ["fill", "sd_bps", "dev_sigma", "walk_sigma", "basis_sigma", "rem", "stage",
        "book_latency_ms", "twap_age_ms"]   # 实盘 snapshot 行可算的那 9 个
TICK_FULL = [f for f in FULL if f != "stage"]      # tick 级: 连续 rem 取代 stage

# ── B2 全窗模型的**同套特征、换朝向**（`orient="up"`）──
# ⚠️ 与 `TICK_FULL` 一一对应, 只把 `fill`（热门侧有效价）换成 `up_px`（Up 侧要价 yes_ask），
# 其余腿的方向全部改成「以 Up 为正」——**不加任何新特征**（要加就该另立一节预注册）。
UP_TICK = ["up_px" if k == "fill" else k for k in TICK_FULL]

STAGE_ID = {"t150": 0, "t60": 1, "listen": 2}


def _nan(n):
    return np.full(n, np.nan)


def _lag(ts, vals, lag_ms, tol_ms=1500):
    """`lag_ms` 毫秒前的值（按**时间戳**对齐, 不按索引——索引假设 1Hz 连续）。

    找不到落在容差内的那一 tick ⇒ NaN（宁缺勿错）。
    """
    n = len(ts)
    out = _nan(n)
    if n == 0:
        return out
    j = np.searchsorted(ts, ts - lag_ms, side="left")
    ok = (j < np.arange(n)) & (j >= 0)
    jj = np.clip(j, 0, n - 1)
    d = ts - ts[jj]
    good = ok & (np.abs(d - lag_ms) <= tol_ms)
    out[good] = vals[jj[good]]
    return out


def frozen_dev(rem, sgn, spot, anchor, sd):
    """冻结假设下的**预计结算 dev**（美元）——`internal/tail/curve.go:RequiredPrice` 的对应量。

    结算窗 = 闭市前 60 秒（TWAP-60）。`rem ≤ 60` 时, 已定局的那 `60 − rem` 秒的价格和为
    `H`（用逐秒 spot 近似, 见报告 §2 口径说明）; 假设从现在起现货**冻在当前价**, 则
    闭市 TWAP = `(H + rem·spot)/60`, 于是预计结算 dev = `sgn·((H + rem·spot)/60 − anchor)`。

    `rem > 60` ⇒ 结算窗一秒都还没发生 ⇒ 「冻住」外推就是当前 dev（连续衔接: rem = 60 处
    两者恒等）。缺样本的秒按 `H = 均值 × (60 − rem)` 补（与曲线图同口径）; 样本不足期望
    一半 ⇒ NaN（宁可不给）。
    """
    n = len(rem)
    out = np.array(sgn * (spot - anchor), dtype=float)   # rem > 60 段 = 当前 dev
    m = rem <= 60.0
    if not m.any():
        return out
    k0 = int(np.argmax(m))                  # 首个进入结算窗的 tick（rem 单调递减）
    csum = np.concatenate([[0.0], np.cumsum(spot)])
    for i in np.nonzero(m)[0]:
        span = 60.0 - rem[i]
        if span <= 0.0:                     # rem == 60: 结算窗一秒都没发生 ⇒ 就是 dev
            continue
        cnt = i - k0                        # 已定局的样本秒数
        if cnt < 1 or 2 * cnt < span + 1:   # 样本不足期望一半 ⇒ 不给（宁缺勿错）
            out[i] = np.nan
            continue
        hm = (csum[i] - csum[k0]) * span / cnt      # 缺样本的秒按均值补
        out[i] = sgn[i] * ((hm + rem[i] * spot[i]) / 60.0 - anchor)
    return out


def window_features(A, anchor, sd, vol_base, sd_base=None, orient="hot"):
    """一个窗口的网格 → {特征名: np 数组}（行长 = 网格 tick 数）。

    输入 `A` 是 (n, ≥16) 的网格矩阵（GRID_COLS）; `anchor`/`sd` = 本窗锚与 σ（美元）。

    `orient`:
      * `"hot"`（默认）—— 全部腿以**热门侧**为正（`sgn = A[:,C_SGN]`），价格 = `fill`。
        这是 B1/04 号的口径, 输出与历史**逐位一致**（网格列只追加在末尾, 索引未动）。
      * `"up"` —— 全部腿以 **Up** 为正（`sgn ≡ +1`），价格 = `up_px`（= `yes_ask`,
        买 Up 要付的价）。B2 全窗模型的输入（`UP_TICK`）。
        ⚠️ 网格里存的 dev/walk/basis 是**热门侧朝向**的 ⇒ 乘回 `A[:,C_SGN]` 翻到 Up 朝向
        （±1 相乘, 逐位精确）。
    """
    n = len(A)
    f = {}
    if n == 0:
        return f
    ts, rem = A[:, C_TS], A[:, C_REM]
    spot = A[:, C_SPOT]
    if orient == "hot":
        sgn = A[:, C_SGN]
        px, px_name = A[:, C_FILL], "fill"
        dev, walk, basis = A[:, C_DEV], A[:, C_WALK], A[:, C_BASIS]
    elif orient == "up":
        sgn = np.ones(n)
        px, px_name = A[:, C_YESA], "up_px"
        dev = A[:, C_DEV] * A[:, C_SGN]
        walk = A[:, C_WALK] * A[:, C_SGN]
        basis = A[:, C_BASIS] * A[:, C_SGN]
    else:
        raise ValueError(f"unknown orient: {orient}")

    f[px_name] = px
    f["rem"] = rem
    f["sd_bps"] = np.full(n, sd / anchor * 1e4) if anchor else _nan(n)
    f["dev_sigma"] = dev / sd
    f["walk_sigma"] = walk / sd
    f["basis_sigma"] = basis / sd
    f["slack_sigma"] = frozen_dev(rem, sgn, spot, anchor, sd) / sd
    f["book_latency_ms"] = A[:, C_LAT]
    f["twap_age_ms"] = A[:, C_TWAGE]

    s10 = _lag(ts, spot, 10000.0)
    f["spot_ret_10s_sigma"] = sgn * (spot - s10) / sd
    d10 = _lag(ts, dev, 10000.0)
    f["dev_chg_10s_sigma"] = (dev - d10) / sd
    w10 = _lag(ts, walk, 10000.0)
    f["walk_chg_10s_sigma"] = (walk - w10) / sd

    peak = np.maximum.accumulate(np.where(np.isnan(dev), -np.inf, dev))
    f["giveback_sigma"] = (peak - dev) / sd

    # ── 成交量（两个无量纲比率; 全部只回看）──
    cum = A[:, C_VOLCUM]
    idx = np.arange(1, n + 1, dtype=float)
    run_rate = cum / idx                                    # 本窗截至此刻均速
    st = np.searchsorted(ts, ts - 30000.0, side="left")
    cnt30 = idx - st                                        # 近 30s 的 tick 数
    prev_cum = np.where(st > 0, cum[np.clip(st - 1, 0, n - 1)], 0.0)
    rate30 = (cum - prev_cum) / np.maximum(cnt30, 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        f["vol_rate_30s"] = np.where((cnt30 >= 5) & (idx >= 10), rate30 / run_rate, np.nan)
        f["vol_ratio_prev"] = (np.where(idx >= 10, run_rate / vol_base, np.nan)
                               if vol_base else _nan(n))
    if sd_base:
        f["sd_ratio_prev"] = np.full(n, sd / sd_base)       # 备用（不进预注册集）
    return f


STAGE_ID_REV = {v: k for k, v in STAGE_ID.items()}
