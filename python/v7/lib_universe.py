#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v7 宇宙层：从原始 events 产出 ①可判定 tick 网格 ②现行规则链的信号行。

⚠️ **本文件是 oracle（`python/v4/23_tail_integrated.py`）的逐位镜像**——判定链、σ 口径、
可判定 tick 口径全部照抄，只在外面加了两件 oracle 不需要的东西:
  * 每个 tick 保留**网格索引**（`gi`）与逐 tick 的原始量（spot/twap/成交量），供特征用;
  * 规则参数化（`Rules`）——默认值 = 现行规则 ⇒ 跑出来必须逐位等于 oracle 的 pin
    （6704 行 / 2074 信号 / 97.830280% / +70.314372U）。

**内存策略（data/btc 共 744MB, 机器 17GB）**: 两遍扫描。
  第一遍（`scan_scalars`）：全量 parse, 但只留窗口级标量（σ 输入 + 成交量）——几十 KB;
  第二遍（`iter_windows`）：**逐文件**parse、抽 tick 网格、用完即丢（一次只有一天在内存里）。
"""
import datetime
import json
from pathlib import Path

# ── 与 oracle 23 逐位一致的口径常量 ──────────────────────────────────────
STAKE = 2.0
MAX_LAT = 300            # 盘口延迟闸（ms）
P_FLOOR = 0.80           # 价格腿门槛
DEV_USD = 63.0           # dev 腿（美元）
SD_MIN_USD = 40.0        # σ 腿（美元）
WALK_MIN_USD = 43.0      # T=150 段入场闸（决策 #29）
FLOOR_MIN_PRICE = 0.83   # 价格地板（决策 #32/#33, 段 2/3）
T150, T60 = 150, 60
GRID_REM_MAX = 300       # 特征网格上限（整窗）; 判定候选 = 其中 rem ≤ T150 的子序列
FIELDS = ("yes_bid", "yes_ask", "no_bid", "no_ask")
STAGES = ("t150", "t60", "listen")

# 网格列（`iter_windows` 产出的每 tick 元组顺序; `01_build_dataset.py` 按它建 numpy 列）
GRID_COLS = (
    "gi",          # 网格内序号（0 = 本窗首个可判定 tick）
    "ts",          # tick 时间戳（ms）
    "rem",         # 距闭市秒数
    "sgn",         # +1 热门侧 = yes（Up）/ −1 = no（Down）
    "fill",        # 热门侧有效价（ask 优先、bid 兜底）
    "spot",        # Binance 现货
    "twap",        # Chainlink TWAP-60（缺失 = nan）
    "dev",         # sgn·(spot − anchor)  美元
    "walk",        # sgn·(twap − anchor)  美元（缺失 = nan）
    "basis",       # sgn·(spot − twap)    美元（缺失 = nan）
    "lat",         # book_latency_ms
    "twap_age",    # twap.age_ms（缺失 = nan）
    "buy_vol",     # 本 tick Binance 主动买量
    "sell_vol",    # 本 tick Binance 主动卖量
    "n_trd",       # 本 tick Binance 成交笔数
    "vol_cum",     # 本窗**到本 tick 为止**的累计成交量（buy+sell）
    # ── 以下两列**追加**在末尾（B2 全窗模型用; 索引 16/17, 上面的索引一字不动）──
    "yes_ask",     # Up 侧要价（买 Up 要付的价; = fill 当且仅当热门侧是 yes）
    "no_ask",      # Down 侧要价（买 Down 要付的价）
)


# ── 第一遍: 窗口级标量（σ 与成交量基线） ─────────────────────────────────

def _slim(rec):
    """窗口级标量: σ 的输入 + 成交量统计（不含 ticks）。"""
    ticks = rec.get("ticks") or []
    buy = sell = 0.0
    n = 0
    for x in ticks:
        # ⚠️ 域必须是 `rem ∈ (0, 300]`（= 整窗, 与特征网格 GRID_REM_MAX **同域**）：
        # 基线按整窗算、分子按 rem ≤ 150 算, 两者会因「尾盘放量」系统性错开。
        rem = x.get("rem")
        if rem is None or rem <= 0 or rem > GRID_REM_MAX:
            continue
        b = x.get("bin") or {}
        buy += b.get("buy_vol") or 0.0
        sell += b.get("sell_vol") or 0.0
        n += 1
    return {
        "start_time": rec.get("start_time"),
        "condition_id": rec.get("condition_id"),
        "outcome": rec.get("outcome"),
        "open": rec.get("twap_open_price"),      # = anchor（决策 #15）
        "close": rec.get("twap_close_price"),
        "n_ticks_raw": n,
        "vol_rate": ((buy + sell) / n) if n else None,   # 本窗平均每秒成交量（BTC）
    }


def scan_scalars(paths):
    """第一遍: 全量 parse 但只留标量 → 按 start_time 排序的 list。

    settlement_correction 行的合并与 `v2.lib.load_events` 同口径（data/btc 里实际为 0 行,
    保留逻辑以防新采集目录里出现）。
    """
    recs, corr = {}, {}
    for path in sorted(paths):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get("event_type") == "settlement_correction":
                    corr[rec["start_time"]] = rec
                else:
                    recs[rec["start_time"]] = _slim(rec)
    for st, c in corr.items():
        if st in recs:
            r = recs[st]
            if c.get("outcome") is not None:
                r["outcome"] = c["outcome"]
            for k, kk in (("twap_open_price", "open"), ("twap_close_price", "close")):
                if c.get(k):
                    r[kk] = c[k]
    return sorted(recs.values(), key=lambda r: r["start_time"])


def hist_context(slims, sig_n=18, base_n=48, sig_min=3, base_min=8):
    """逐窗 σ（决策 #13/#15 口径）+ 两个「过去 N 窗」基线。

    σ = **前 ≤18 个已完窗口** |tw_close − tw_open| 的均值（≥3 个可用）——`13_tail_sweep.py`
    的 `hist_ranges` 逐位镜像（注意: 取的是**全部**前序窗, 不管它有没有可判定 tick）。
    ⚠️ 基线（volume/σ 的「正常水平」）**只用已完窗**——当前窗自己不参与（防泄漏）。
    """
    out = {}
    for i, e in enumerate(slims):
        prev = []
        for j in range(max(0, i - sig_n), i):
            o, c = slims[j]["open"], slims[j]["close"]
            if o and c:
                prev.append(abs(c - o))
        sd = (sum(prev) / len(prev)) if len(prev) >= sig_min else None

        wr = [slims[j]["vol_rate"] for j in range(max(0, i - base_n), i)
              if slims[j]["vol_rate"]]
        vol_base = (sorted(wr)[len(wr) // 2] if len(wr) >= base_min else None)

        sds = []
        for j in range(max(0, i - base_n), i):
            o, c = slims[j]["open"], slims[j]["close"]
            if o and c:
                sds.append(abs(c - o))
        sd_base = (sorted(sds)[len(sds) // 2] if len(sds) >= base_min else None)

        out[e["start_time"]] = {"sd": sd, "vol_base": vol_base, "sd_base": sd_base,
                                "slim": e}
    return out


# ── 第二遍: 逐窗 tick 网格 ───────────────────────────────────────────────

def grid_of(rec, anchor, rem_max=None):
    """本窗可判定 tick 网格——`23_tail_integrated.py:win_ticks` 的逐位镜像 + 附加列。

    可判定 = 延迟 ≤ 300ms ∧ 四档 > 0 ∧ rem ∈ (0, rem_max] ∧ spot > 0（口径见 oracle 文件头 1）。
    `anchor ≤ 0` ⇒ 空网格（本窗不产出任何行）。

    ⚠️ **默认 `rem_max = GRID_REM_MAX`(300) 覆盖整窗, 而判定候选只有 `rem ≤ 150` 那一段**
    （`decision_rows()`）——两者刻意分开: T=150 判定 tick 往往就是整窗第一个可判定 tick,
    若网格从 rem ≤ 150 起步, 它的 10s 动量/30s 量比/回吐**全部无历史可看**（46% 的信号
    落在这一段）。判定链本身仍只看 `rem ≤ 150` 的子序列, 与 oracle 逐位一致。
    """
    out = []
    if not anchor:
        return out
    rem_max = GRID_REM_MAX if rem_max is None else rem_max
    cum = 0.0
    gi = 0
    for x in rec.get("ticks") or []:
        p = x.get("pm") or {}
        if not p or (p.get("book_latency_ms") or 0) > MAX_LAT:
            continue
        rem = x.get("rem")
        if rem is None or rem <= 0 or rem > rem_max:
            continue
        if not all((p.get(k) or 0) > 0 for k in FIELDS):
            continue
        b = x.get("bin") or {}
        spot = b.get("price")
        if not spot:
            continue
        ya, na = p.get("yes_ask") or 0, p.get("no_ask") or 0
        side_yes = ya >= na                       # 平局取 yes（与 Go HotBook 同）
        sgn = 1.0 if side_yes else -1.0
        fill = ya if side_yes else na
        tw = x.get("twap") or {}
        twap = tw.get("price") or None
        buy = b.get("buy_vol") or 0.0
        sell = b.get("sell_vol") or 0.0
        cum += buy + sell
        out.append((
            gi, float(x.get("ts") or 0), float(rem), sgn, float(fill), float(spot),
            float(twap) if twap else float("nan"),
            sgn * (spot - anchor),
            (sgn * (twap - anchor)) if twap else float("nan"),
            (sgn * (spot - twap)) if twap else float("nan"),
            float(p.get("book_latency_ms") or 0),
            float(tw.get("age_ms")) if tw.get("age_ms") is not None else float("nan"),
            float(buy), float(sell), float(b.get("ticks") or 0), cum,
            float(ya), float(na),
        ))
        gi += 1
    return out


def iter_windows(paths, ctx, with_grid=True):
    """第二遍: 逐文件产出窗口 dict（用完的文件内存即刻释放）。

    yields: {"start_time","condition_id","outcome","anchor","sd","vol_base","sd_base",
             "date","rows"(网格元组 list),"vol_rate"}
    """
    paths = sorted(paths)
    for path in paths:
        with open(path) as f:
            events = []
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get("event_type") == "settlement_correction":
                    continue
                events.append(rec)
        for rec in events:
            st = rec.get("start_time")
            c = ctx.get(st)
            if c is None:
                continue
            anchor = rec.get("twap_open_price")
            rows = grid_of(rec, anchor) if with_grid else []
            yield {
                "start_time": st,
                "condition_id": rec.get("condition_id"),
                "outcome": rec.get("outcome"),
                "anchor": anchor,
                "sd": c["sd"],
                "vol_base": c["vol_base"],
                "sd_base": c["sd_base"],
                "vol_rate": c["slim"]["vol_rate"],
                "date": datetime.datetime.fromtimestamp(
                    st, datetime.timezone.utc).strftime("%Y-%m-%d") if st else None,
                "rows": rows,
            }
        del events


# ── 规则（现行 = 决策 #26/#29/#32/#33; 参数化只为 Track A 的网格） ─────────

class Rules:
    """三段链的参数集。默认值 = 现行规则 ⇒ 逐位复现 oracle。

    Track A 的「动态阈值」在这里注入: 四个 `*_fn` 把常数换成状态的函数
    （签名 `f(r, sd) -> bool`, r 是判定行 dict）。默认全 None = 用常数阈值。
    """

    def __init__(self, walk_usd=WALK_MIN_USD, dev_usd=DEV_USD, sd_min=SD_MIN_USD,
                 p_floor=P_FLOOR, floor_price=FLOOR_MIN_PRICE, strict_t150=True,
                 walk_fn=None, dev_fn=None, sd_fn=None, floor_fn=None,
                 extra_ok=None):
        self.walk_usd = walk_usd
        self.dev_usd = dev_usd
        self.sd_min = sd_min
        self.p_floor = p_floor
        self.floor_price = floor_price
        self.strict_t150 = strict_t150
        self.walk_fn = walk_fn
        self.dev_fn = dev_fn
        self.sd_fn = sd_fn
        self.floor_fn = floor_fn        # 价格地板（段 2/3）的动态形式
        # 附加腿（Track B 的「规则 + 模型过滤」形态）: `extra_ok(r, base) -> bool`。
        # 不过 ⇒ 同 walk_low 的语义: **链继续**（不是整窗丢弃）, 判定行照落。
        self.extra_ok = extra_ok

    def extra(self, r, base):
        return True if self.extra_ok is None else self.extra_ok(r, base)

    # ── 四条腿 ──
    def price_t150(self, r):
        return r["fill"] > self.p_floor if self.strict_t150 else r["fill"] >= self.p_floor

    def price_other(self, r):
        return r["fill"] >= self.p_floor

    def dev_ok(self, r):
        return self.dev_fn(r) if self.dev_fn else (r["dev"] >= self.dev_usd)

    def sd_ok(self, r):
        if self.sd_fn:
            return self.sd_fn(r)
        return r["sd"] is not None and r["sd"] >= self.sd_min

    def walk_ok(self, r):
        """T=150 段入场闸: walk ≥ 阈值。walk 无值 ⇒ **放行**（fail-open, 与 Go 同口径）。"""
        if self.walk_fn:
            return self.walk_fn(r)
        w = r["walk"]
        return True if (w is None or w != w) else w >= self.walk_usd   # None/nan ⇒ 放行

    def floor_ok(self, r):
        """价格地板（段 2/3）: 有效价 > 阈值（**严格**）。"""
        if self.floor_fn:
            return self.floor_fn(r)
        return r["fill"] > self.floor_price

    def r5(self, r, t150):
        ok_price = self.price_t150(r) if t150 else self.price_other(r)
        if not ok_price:
            return False
        if self.dev_ok(r):
            return True
        return self.sd_ok(r) and r["sig"] is not None and r["sig"] >= 1.0

    def r2(self, r):
        return self.price_other(r) and self.dev_ok(r)


def decision_rows(rows):
    """判定候选 = 网格里 `rem ≤ T150` 的子序列（= oracle 的 `win_ticks` 全集, 顺序一致）。

    ⚠️ 链的每一步都必须吃这个子序列——直接吃整窗网格会让「首个可判定 tick」变成
    rem≈298 的那一个, 与引擎/oracle 的语义完全不同。
    """
    return [x for x in rows if x[2] <= T150]


def row_of(t, anchor, sd, date, outcome, stage, gi, base):
    """判定行 dict —— `23_tail_integrated.py:row` 的镜像 + 网格索引/窗口标识。

    `t` 是网格元组（GRID_COLS 顺序）。数值一律与 oracle 同式同序（浮点逐位一致）。
    """
    sgn = t[3]
    dev = t[7]
    twap = t[6]
    gi = int(gi)          # 从 numpy 矩阵重建的行里 gi 是 float64, 统一成 int
    return {
        "date": date, "stage": stage, "rem": t[2], "side": ("yes" if sgn > 0 else "no"),
        "fill": t[4], "buy": t[4],                 # buy 别名: 复用 14 的 pl/day_bootstrap
        "dev": dev, "sd": sd, "sig": (dev / sd) if sd else None,
        "walk": (None if twap != twap else t[8]),
        "settle_won": 1 if ((outcome == 0) if sgn > 0 else (outcome == 1)) else 0,
        "gi": gi, "event_start": base["start_time"], "condition_id": base["condition_id"],
        "outcome": outcome,
        # 窗口级基线（Track A 的动态阈值要用; 都是**已完窗**的统计量, 无泄漏）
        "sd_base": base.get("sd_base"), "vol_base": base.get("vol_base"),
    }


def chain(rows, sd, date, outcome, rules, base):
    """三段递进判定链 —— `23_tail_integrated.py:chain` 的逐位镜像（+ gi/base 透传）。

    返回 (rows, rejects): 判定行成功与否都产出; 信号行只在达标时产出（ok=True）。
    """
    if not rows:
        return [], []
    res, rejects = [], []
    head = rows[0]
    if head[2] > T60:
        # 段 1: 首个可判定 tick 且 rem ≤ 150（价格腿**严格**大于, 决策 #26）
        r = row_of(head, base["anchor"], sd, date, outcome, "t150", head[0], base)
        ok5 = rules.r5(r, t150=True)
        # 入场闸（决策 #29）: ⑤ 达标 ∧ walk ≥ 阈值才算信号; 闸掉 ⇒ 链继续
        if ok5 and rules.walk_ok(r) and rules.extra(r, base):
            r["ok"] = True
            return [r], rejects
        if ok5:
            rejects.append("t150:walk_low" if not rules.walk_ok(r) else "t150:extra")
        else:
            rejects.append("t150")
        res.append(r)
        rest = [x for x in rows if x[2] <= T60]
    else:
        # 迟到接入: 首个可判定 tick 已在 T=60 段 → 跳过段 1, 不伪造 t150 行
        rest = rows
    shadowed = False        # 影子行闩锁（段 2/3 共用 ⇒ 每窗至多一条）
    if rest:
        t2 = rest[0]
        r2r = row_of(t2, base["anchor"], sd, date, outcome, "t60", t2[0], base)
        if rules.r5(r2r, t150=False):
            # 价格地板（决策 #32/#33）: ⑤ 达标但价 ≤ 地板 ⇒ 影子行 + 链继续
            if not rules.floor_ok(r2r):
                r2r["reject_reason"] = "floor_low"
                res.append(r2r)
                shadowed = True
            elif not rules.extra(r2r, base):
                rejects.append("t60:extra")
                res.append(r2r)
            else:
                r2r["ok"] = True
                return res + [r2r], rejects
        else:
            rejects.append("t60")
            res.append(r2r)
        # 段 3: 监听 —— 此后每秒判 ②, 首个达标 tick 出信号（未达标的 tick 不落行）
        for x in rest[1:]:
            rl = row_of(x, base["anchor"], sd, date, outcome, "listen", x[0], base)
            if not rules.r2(rl):
                continue
            if not rules.floor_ok(rl):
                if not shadowed:
                    rl["reject_reason"] = "floor_low"
                    res.append(rl)
                    shadowed = True
                continue
            if not rules.extra(rl, base):
                continue                       # 监听段的被拒 tick 不落行（与规则腿一致）
            rl["ok"] = True
            res.append(rl)
            return res, rejects
    return res, rejects


# ── P&L 与 pin ───────────────────────────────────────────────────────────

def pnl_of(r, stake=STAKE):
    """成交口径: shares = stake/fill; 赢 → shares−stake, 输 → −stake（与 oracle 同）。"""
    return (stake / r["fill"] - stake) if r["settle_won"] else -stake


def pl(rows, stake=STAKE):
    return sum(pnl_of(r, stake) for r in rows)


PIN = {                      # oracle 23 的现行 pin（决策 #33 之后）
    "rows": 6704, "signals": 2074, "wr": 97.830280, "pnl": 70.314372,
    "walk_low": 250, "floor_low": 24, "windows": 3640, "no_sigma": 3,
    "stages": {"t150": (3638, 958), "t60": (2676, 741), "listen": (390, 375)},
}


def summarize(signals, rows_n, walk_low, floor_low, windows, no_sigma):
    """把一次全量重放压成与 PIN 可比的一行。"""
    allsig = [r for s in STAGES for r in signals[s]]
    n = len(allsig)
    wr = (sum(r["settle_won"] for r in allsig) / n * 100) if n else 0.0
    return {
        "rows": sum(rows_n.values()), "signals": n, "wr": wr, "pnl": pl(allsig),
        "walk_low": walk_low, "floor_low": floor_low, "windows": windows,
        "no_sigma": no_sigma,
        "stages": {s: (rows_n[s], len(signals[s])) for s in STAGES},
    }


def check_pin(got, want=None, tol=1e-6, quiet=False):
    """§0 自检: 逐位对 pin。不一致 ⇒ 打印差异并 return False。"""
    want = want or PIN
    ok = True
    msgs = []
    for k in ("rows", "signals", "walk_low", "floor_low", "windows", "no_sigma"):
        if got[k] != want[k]:
            ok = False
            msgs.append(f"  ✗ {k}: got {got[k]} want {want[k]}")
    for k in ("wr", "pnl"):
        if abs(got[k] - want[k]) > tol:
            ok = False
            msgs.append(f"  ✗ {k}: got {got[k]:.6f} want {want[k]:.6f}")
    for s, (r0, s0) in want["stages"].items():
        if got["stages"][s] != (r0, s0):
            ok = False
            msgs.append(f"  ✗ stage {s}: got {got['stages'][s]} want {(r0, s0)}")
    if not quiet:
        print(("  §0 pin " + ("✅ 逐位一致" if ok else "❌ 不一致"))
              + f"  rows={got['rows']} signals={got['signals']} "
                f"WR={got['wr']:.6f}% P&L={got['pnl']:+.6f}U")
        for m in msgs:
            print(m)
    return ok


def replay(paths, ctx=None, rules=None, quiet=False):
    """全量重放一遍（信号行 + 计数）——供 §0 pin 与 Track A 复用。"""
    rules = rules or Rules()
    ctx = ctx or hist_context(scan_scalars(paths))
    signals = {s: [] for s in STAGES}
    rows_n = {s: 0 for s in STAGES}
    walk_low = floor_low = windows = no_sigma = 0
    for w in iter_windows(paths, ctx):
        if w["outcome"] is None or not w["anchor"]:
            continue
        if w["sd"] is None:
            no_sigma += 1
            continue
        sub = decision_rows(w["rows"])
        if not sub:
            continue
        windows += 1
        rs, rej = chain(sub, w["sd"], w["date"], w["outcome"], rules, w)
        walk_low += sum(1 for x in rej if x.endswith("walk_low"))
        floor_low += sum(1 for r in rs if r.get("reject_reason") == "floor_low")
        for r in rs:
            rows_n[r["stage"]] += 1
            if r.get("ok"):
                signals[r["stage"]].append(r)
    return summarize(signals, rows_n, walk_low, floor_low, windows, no_sigma)


def paths_of(dirs):
    """`--dirs a b c` → 全部 events_*.jsonl 路径。"""
    ps = []
    for d in dirs:
        ps.extend(sorted(Path(d).glob("events_*.jsonl")))
    return ps
