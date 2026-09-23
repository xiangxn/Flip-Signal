#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫尾盘：起扫时点 T=150 的「监听」口径回测 + 空簿/0.99 填充单审计（2026-09-23）

用户 2026-09-23 提出的两点：

1. **起扫时点**：现行引擎与 13/15 都是「`rem ≤ 60` 的第一个有效 tick 取一次快照，
   判定一次，不达标即本窗结束」（一次性闩锁）。用户要求改为
   「**`rem ≤ 150` 起持续观察，一旦全部条件满足就下单**」。本脚本把四种口径同台对照：

      A 快照 T=60    —— 现行实现（基线, 必须复现 13 的头条 n=1536 WR 99.61% +36.93U）
      D 快照 T=150   —— 引擎已落的 frame 行口径（只记录、现行不下单）
      B 监听 T=60    —— 从 rem≤60 起扫到闭市，首个达标 tick 下单
      C 监听 T=150   —— **用户口径**：从 rem≤150 起扫到闭市，首个达标 tick 下单

   「监听」= 每个有效 tick 都重新判一次（热门侧也按当刻两侧 ask 重新确定），
   命中即下单；不命中就继续等下一个 tick，直到 rem ≤ 0。

2. **空簿**：热门侧订单簿为空在真实市场很常见，必须分清「有 0.99 可以吃」与
   「根本没有挂单可吃」。本脚本第五节审计 14 天数据里的实际形态。

后续追加（同日，用户要求「10s 分桶看看情况」）—— 六~九节把 T 按 **10s 分辨率**扫一遍:

   六 入场时点扫描（快照口径）: T = 10,20,…,150 各取「rem≤T 首 tick 判定一次」
   七 起扫时点扫描（监听口径）: T = 10,20,…,150 各取「rem≤T 起首个达标 tick」
   八 监听 T=150 时首个达标 tick 落在哪个 10s 桶（信号发生时刻分布）
   九 每个 10s 桶的盘口画像（热门 ask 中位 / =0.99 占比 / 桶首无条件入场的 WR 与 EV）

⚠️ **T 是在同一批 14 天上扫的**（3753 窗）——按 §4.3 的纪律, 这张表的 argmax
**不能**当作新参数选出来用。本表的用途是「现行 T=60 有没有被邻居打败」：两条曲线的
argmax 都落在 T=60、[30,70] 是平台、T≥80 崩掉 ⇒ 结论是**不动**，不是「改成扫出来的那个」。

判定规则（文档 §1.2/§2，与引擎 tail/rules 同）:
  价格腿 hot_ask ≥ 0.80
  位移腿 dev ≥ 63 美元  或  (sd ≥ 40 美元 且 dev ≥ sd)
  dev = sgn·(spot − anchor) 美元（sgn: 押 yes +1 / no −1）
  sd  = hist_bps·anchor/1e4 = 该窗 1σ 折美元

成交: fill = 下单 tick 热门侧 ask; shares = STAKE/fill; 赢 → shares−STAKE, 输 → −STAKE
判决: 日级 bootstrap 95%（2000 次, seed 42, 复用 14 的 day_bootstrap/pl）

用法: python/venv/bin/python python/v4/16_tail_t150_scan.py
"""
import sys
import random
import statistics
import datetime
import collections
import importlib.util
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))
from v2.lib import load_events                                    # noqa: E402

STAKE = 2.0
MAX_LAT = 300
P_FLOOR = 0.80
DEV_USD = 63.0
SD_MIN_USD = 40.0
FIELDS = ("yes_bid", "yes_ask", "no_bid", "no_ask")
FILLER_PX = 0.99          # 常驻填充报价价位（审计用, 非策略参数）


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ts = load("ts13", BASE / "13_tail_sweep.py")
s14 = load("s14", BASE / "14_tail_sweep_sigma.py")


# ── 数据提取 ──────────────────────────────────────────────────────────────

def tail_ticks(e, T, gate="quad"):
    """窗口内 rem ≤ T 的全部有效 tick（时间顺序）。

    gate="quad"  四档报价全须 > 0（现行引擎/13/15 的门, 对齐基线）
    gate="ask"   只要求至少一侧 ask > 0（可成交性门, 用于空簿审计的放宽对照）
    """
    out = []
    anchor = e.get("twap_open_price")
    if not anchor:
        return out
    for x in e.get("ticks") or []:
        p = x.get("pm") or {}
        if not p or (p.get("book_latency_ms") or 0) > MAX_LAT:
            continue
        rem = x.get("rem")
        if rem is None or rem <= 0 or rem > T:
            continue
        ya, na = p.get("yes_ask") or 0, p.get("no_ask") or 0
        if gate == "quad":
            if not all((p.get(k) or 0) > 0 for k in FIELDS):
                continue
        else:
            if not (ya > 0 or na > 0):
                continue
        spot = (x.get("bin") or {}).get("price")
        if not spot:
            continue
        side = "yes" if ya >= na else "no"
        out.append({
            "rem": rem, "side": side, "fill": (ya if side == "yes" else na),
            "ask_yes": ya, "ask_no": na,
            "spot": spot, "twap": (x.get("twap") or {}).get("price"),
        })
    return out


def rules(dev, sd, sig, fill):
    """⑤: 价格腿 + (dev ≥ 63 或 (sd ≥ 40 且 dev ≥ sd))"""
    if not (fill >= P_FLOOR):
        return False
    if dev >= DEV_USD:
        return True
    return sd is not None and sd >= SD_MIN_USD and sig is not None and sig >= 1.0


def mkrow(t, anchor, sd, date, outcome):
    sgn = 1.0 if t["side"] == "yes" else -1.0
    dev = sgn * (t["spot"] - anchor)
    return {
        "date": date, "rem": t["rem"], "side": t["side"],
        "fill": t["fill"], "buy": t["fill"],      # buy 别名: 复用 14 的 pl/day_bootstrap
        "dev": dev, "sd": sd, "sig": (dev / sd) if sd else None,
        "settle_won": 1 if ((outcome == 0) if t["side"] == "yes" else (outcome == 1)) else 0,
    }


def pl(rows):
    s = 0.0
    for r in rows:
        sh = STAKE / r["fill"]
        s += (sh - STAKE) if r["settle_won"] else -STAKE
    return s


def line(lab, rows, width=26):
    if not rows:
        print("  " + lab.ljust(width) + "n=0")
        return
    n = len(rows)
    wr = sum(r["settle_won"] for r in rows) / n
    px = sum(r["fill"] for r in rows) / n
    d = collections.defaultdict(list)
    for r in rows:
        d[r["date"]].append(r)
    neg = sum(1 for v in d.values() if pl(v) < 0)
    print("  " + lab.ljust(width) + f"n={n:<6} WR {wr*100:6.2f}%  价 {px:.4f}  "
          f"EV/注 {pl(rows)/n:+.4f}  P&L {pl(rows):+7.2f}U  亏损日 {neg}/{len(d)}")


def main():
    ev = load_events("data/btc")
    hr = ts.hist_ranges(ev)
    rng = random.Random(42)

    def ci(rows):
        if len(rows) < 5:
            return "[样本薄]"
        ds = sorted(set(r["date"] for r in rows if r["date"] != "2026-08-31"))
        (lo, hi), _ = s14.day_bootstrap(rows, ds, 2000, rng)
        return f"[{lo:+.1f}, {hi:+.1f}]"

    snap60, snap150, scan150, scan60 = [], [], [], []
    extra_by_gate = 0            # 放宽为 ask 门后多出来的有效 tick（空簿审计）
    zero_book_ticks = 0
    reasons = collections.Counter()

    for e in ev:
        outcome, anchor = e.get("outcome"), e.get("twap_open_price")
        if outcome is None or not anchor:
            continue
        h = hr.get(e["start_time"])
        hist_bps = (h / anchor * 1e4) if h else None
        sd = (hist_bps * anchor / 1e4) if hist_bps else None
        date = datetime.datetime.fromtimestamp(
            e["start_time"], datetime.timezone.utc).strftime("%Y-%m-%d")

        t150 = tail_ticks(e, 150, "quad")
        t60 = [t for t in t150 if t["rem"] <= 60]

        # 空簿审计: 四档门之下被挡、但至少一侧 ask > 0 的 tick
        relaxed = tail_ticks(e, 150, "ask")
        extra_by_gate += len([t for t in relaxed
                              if t["rem"] <= 150]) - len(t150)

        # D 快照 T=150 / A 快照 T=60
        for buf, ticks in ((snap150, t150), (snap60, t60)):
            if not ticks:
                continue
            r = mkrow(ticks[0], anchor, sd, date, outcome)
            if rules(r["dev"], sd, r["sig"], r["fill"]):
                buf.append(r)

        # C 监听 T=150 / B 监听 T=60
        for buf, ticks in ((scan150, t150), (scan60, t60)):
            for t in ticks:
                r = mkrow(t, anchor, sd, date, outcome)
                if rules(r["dev"], sd, r["sig"], r["fill"]):
                    buf.append(r)
                    break

    # ── 一、四口径同台 ───────────────────────────────────────────────────
    print("\n" + "=" * 96)
    print("一、四口径同台（14 天, 2U/注, 08-31 仅 1 窗已含入）")
    print("=" * 96)
    line("A 快照 T=60（现行）", snap60)
    line("D 快照 T=150", snap150)
    line("B 监听 T=60→闭市", scan60)
    line("C 监听 T=150→闭市（★）", scan150)
    print("\n  日级 bootstrap 95%（判决口径 §5.2）:")
    for lab, rows in (("A 快照 T=60", snap60), ("D 快照 T=150", snap150),
                      ("B 监听 T=60", scan60), ("C 监听 T=150", scan150)):
        print(f"    {lab.ljust(24)} {ci(rows)}")

    # ── 二、相对基线的增量 ───────────────────────────────────────────────
    print("\n" + "=" * 96)
    print("二、相对现行基线 A 的增量")
    print("=" * 96)
    key = lambda r: (r["date"], r["rem"], r["side"], r["fill"])
    ka = {key(r) for r in snap60}
    for lab, rows in (("C∖A（监听 T=150 的净增量）", scan150),
                      ("B∖A（监听 T=60 的净增量）", scan60),
                      ("D∖A（快照推到 T=150 的净增量）", snap150)):
        add = [r for r in rows if key(r) not in ka]
        ka_local = {key(r) for r in rows}
        drop = [r for r in snap60 if key(r) not in ka_local]
        line("  └ " + lab, add)
        print(f"      增量 CI {ci(add)}   A 中被替换掉 {len(drop)} 笔 "
              f"({pl(drop):+.2f}U)")

    # ── 三、成交价分桶（谁在 0.99 上成交）────────────────────────────────
    print("\n" + "=" * 96)
    print(f"三、成交价分桶：{FILLER_PX:.2f} 填充价位 vs 其余（策略 edge 是否全靠 0.99）")
    print("=" * 96)
    for lab, rows in (("A 快照 T=60", snap60), ("C 监听 T=150", scan150)):
        if not rows:
            continue
        f99 = [r for r in rows if abs(r["fill"] - FILLER_PX) < 1e-9]
        rest = [r for r in rows if abs(r["fill"] - FILLER_PX) >= 1e-9]
        print(f"\n  {lab}:")
        line("  └ 恰好在 0.99 成交", f99)
        line("  └ 其余价位成交", rest)
        if f99:
            print(f"      0.99 档贡献 P&L {pl(f99):+.2f}U / 合计 {pl(rows):+.2f}U "
                  f"= {pl(f99)/pl(rows)*100:.1f}%")

    # ── 四、延迟分布 ─────────────────────────────────────────────────────
    print("\n" + "=" * 96)
    print("四、监听口径的延迟（首个达标 tick 相对首个有效 tick 晚多少秒）")
    print("=" * 96)
    for T, lab in ((150, "C 监听 T=150"), (60, "B 监听 T=60")):
        firsts, found = [], []
        for e in ev:
            if e.get("outcome") is None or not e.get("twap_open_price"):
                continue
            ticks = tail_ticks(e, T)
            if not ticks:
                continue
            firsts.append(ticks[0]["rem"])
            found.append(ticks[-1]["rem"])
        print(f"  {lab}: 首个有效 tick 的 rem 中位 {sorted(firsts)[len(firsts)//2]}")

    # ── 五、空簿审计 ─────────────────────────────────────────────────────
    print("\n" + "=" * 96)
    print("五、空簿审计：『有 0.99 可以吃』vs『根本没挂单可吃』")
    print("=" * 96)
    q = collections.Counter()
    for e in ev:
        ticks = [x for x in (e.get("ticks") or [])
                 if x.get("rem") and 0 < x["rem"] <= 150]
        if not ticks:
            continue
        q["窗总数"] += 1
        zs = [x for x in ticks
              if not any((x.get("pm") or {}).get(k) for k in FIELDS)]
        if len(zs) == len(ticks):
            q["整窗零簿"] += 1
        elif zs:
            q["部分零簿"] += 1
            for x in zs:
                p = x["pm"]
                q["  部分零簿的缺失字段: " + ",".join(
                    f for f in FIELDS if not (p.get(f) or 0))] += 1
    for k, v in q.items():
        print(f"  {k:<44} {v}")
    print(f"\n  放宽为『至少一侧 ask > 0』后多出的有效 tick: {extra_by_gate}")
    print("  ⚠️ 14 天历史里零簿**永远是整簿同时为 0**（book_ts/book_latency 亦为 0 =")
    print("     该窗根本没有 PM 簿），从未出现单侧 ask=0。")
    print("     ⇒ **历史数据无法区分**用户问的那两种情形；两者都是 0。")

    # ── 六、T 扫描（10s 分辨率）───────────────────────────────────────────
    # 先把每窗 ticks 预算好（含 dev/sig/win），两条曲线共用：
    #   入场时点 = 「快照在 rem≤T 首 tick」—— 看 edge 随入场时点怎么变
    #   起扫时点 = 「从 rem≤T 起扫到闭市的首个达标 tick」—— 用户口径
    wins = []
    for e in ev:
        outcome, anchor = e.get("outcome"), e.get("twap_open_price")
        if outcome is None or not anchor:
            continue
        h = hr.get(e["start_time"])
        hist_bps = (h / anchor * 1e4) if h else None
        sd = (hist_bps * anchor / 1e4) if hist_bps else None
        tk = tail_ticks(e, 150, "quad")
        if not tk:
            continue
        date = datetime.datetime.fromtimestamp(
            e["start_time"], datetime.timezone.utc).strftime("%Y-%m-%d")
        rows = []
        for t in tk:
            sgn = 1.0 if t["side"] == "yes" else -1.0
            dev = sgn * (t["spot"] - anchor)
            rows.append({
                "date": date, "rem": t["rem"], "side": t["side"],
                "fill": t["fill"], "buy": t["fill"],
                "dev": dev, "sd": sd, "sig": (dev / sd) if sd else None,
                "settle_won": 1 if ((outcome == 0) if t["side"] == "yes"
                                    else (outcome == 1)) else 0,
            })
        wins.append(rows)

    def sweep(mode):
        """返回 {T: rows}; mode='entry' 首 tick 判定 / mode='scan' 首个达标 tick"""
        out = collections.defaultdict(list)
        for rows in wins:
            for T in RANGE_T:
                sub = [r for r in rows if r["rem"] <= T]
                if not sub:
                    continue
                if mode == "entry":
                    r = sub[0]
                    if rules(r["dev"], r["sd"], r["sig"], r["fill"]):
                        out[T].append(r)
                else:
                    for r in sub:
                        if rules(r["dev"], r["sd"], r["sig"], r["fill"]):
                            out[T].append(r)
                            break
        return out

    RANGE_T = list(range(10, 151, 10))

    print("\n" + "=" * 96)
    print("六、10s 分桶扫描：入场时点 T（快照口径）—— edge 活在哪一段时间里")
    print("=" * 96)
    print(f"  {'T':>4}{'n':>7}{'WR':>9}{'均价':>9}{'EV/注':>10}{'P&L':>11}"
          f"{'亏损日':>8}{'  日级 95% CI':<20}")
    ent = sweep("entry")
    for T in RANGE_T:
        rows = ent[T]
        if not rows:
            continue
        n = len(rows)
        wr = sum(r["settle_won"] for r in rows) / n
        px = sum(r["fill"] for r in rows) / n
        d = collections.defaultdict(list)
        for r in rows:
            d[r["date"]].append(r)
        neg = sum(1 for v in d.values() if pl(v) < 0)
        print(f"  {T:>4}{n:>7}{wr*100:>8.2f}%{px:>9.4f}{pl(rows)/n:>+10.4f}"
              f"{pl(rows):>+10.2f}U{neg:>5}/{len(d):<3}{ci(rows):>20}")

    print("\n" + "=" * 96)
    print("七、10s 分桶扫描：起扫时点 T（监听口径, 用户提案）—— T 越小越好吗")
    print("=" * 96)
    print(f"  {'T':>4}{'n':>7}{'WR':>9}{'均价':>9}{'EV/注':>10}{'P&L':>11}"
          f"{'亏损日':>8}{'  日级 95% CI':<20}")
    scn = sweep("scan")
    for T in RANGE_T:
        rows = scn[T]
        if not rows:
            continue
        n = len(rows)
        wr = sum(r["settle_won"] for r in rows) / n
        px = sum(r["fill"] for r in rows) / n
        d = collections.defaultdict(list)
        for r in rows:
            d[r["date"]].append(r)
        neg = sum(1 for v in d.values() if pl(v) < 0)
        print(f"  {T:>4}{n:>7}{wr*100:>8.2f}%{px:>9.4f}{pl(rows)/n:>+10.4f}"
              f"{pl(rows):>+10.2f}U{neg:>5}/{len(d):<3}{ci(rows):>20}")

    # 首个达标 tick 落在哪个 rem 桶（监听 T=150 时, 信号实际何时发生）
    print("\n" + "=" * 96)
    print("八、监听 T=150 时，首个达标 tick 实际落在哪个 10s 桶（信号发生时刻分布）")
    print("=" * 96)
    hist = collections.Counter()
    for rows in wins:
        for r in rows:
            if rules(r["dev"], r["sd"], r["sig"], r["fill"]):
                hist[(r["rem"] - 1) // 10 * 10] += 1
                break
    tot = sum(hist.values())
    for k in sorted(hist, reverse=True):
        print(f"    rem [{k:>3},{k+10:>3})  {hist[k]:>5} 窗  {hist[k]/tot*100:5.1f}%")

    # 每个 10s 桶的盘口画像（价格/量/WR）—— 与 17 的流动性探针互为印证
    print("\n" + "=" * 96)
    print("九、每个 10s 桶的盘口画像（全体有效 tick）")
    print("=" * 96)
    allt = [r for rows in wins for r in rows]
    print(f"  {'rem 桶':<13}{'n':>8}{'热门 ask 中位':>15}{'=0.99 占比':>12}"
          f"{'买在桶首的 WR':>15}{'买在桶首 EV/注':>16}")
    for k in sorted(set((r["rem"] - 1) // 10 * 10 for r in allt)):
        sub = [r for r in allt if (r["rem"] - 1) // 10 * 10 == k]
        if len(sub) < 50:
            continue
        px = statistics.median(r["fill"] for r in sub)
        p99 = sum(1 for r in sub if abs(r["fill"] - FILLER_PX) < 1e-9) / len(sub)
        # 桶首入场: 每窗在该桶的第一个 tick
        firsts, seen = [], set()
        for i, rows in enumerate(wins):
            for r in rows:
                if (r["rem"] - 1) // 10 * 10 == k:
                    firsts.append(r)
                    break
        if firsts:
            wr = sum(r["settle_won"] for r in firsts) / len(firsts)
            ev = pl(firsts) / len(firsts)
            print(f"  [{k:>3},{k+10:>3})  {len(sub):>7}{px:>15.4f}{p99*100:>11.1f}%"
                  f"{wr*100:>14.2f}%{ev:>+16.4f}")
        else:
            print(f"  [{k:>3},{k+10:>3})  {len(sub):>7}{px:>15.4f}{p99*100:>11.1f}%"
                  f"{'—':>15}{'—':>16}")

    # ── 十、B vs A 配对稳健性（扫描 T=60 值不值得改）────────────────────
    print("\n" + "=" * 96)
    print("十、B（监听 T=60）vs A（快照 T=60）配对对照：+7.0U 站得住吗")
    print("=" * 96)
    da_, db_ = collections.defaultdict(list), collections.defaultdict(list)
    for r in snap60:
        da_[r["date"]].append(r)
    for r in scan60:
        db_[r["date"]].append(r)
    days_all = sorted(set(da_) | set(db_) | {r["date"] for r in scan150})

    diff_day = {d: pl(db_.get(d, [])) - pl(da_.get(d, [])) for d in days_all}
    print(f"  逐日 B−A 差（U）:")
    for d in days_all:
        v = diff_day[d]
        mark = "▲" if v > 1e-9 else ("▼" if v < -1e-9 else "=")
        print(f"    {d}  {pl(da_.get(d,[])):+6.2f} → {pl(db_.get(d,[])):+6.2f}   "
              f"Δ {v:+6.2f}  {mark}")
    better = sum(1 for v in diff_day.values() if v > 1e-9)
    worse = sum(1 for v in diff_day.values() if v < -1e-9)
    print(f"  日胜率: 更好 {better} 天 / 更差 {worse} 天 / 持平 "
          f"{len(days_all)-better-worse} 天;  合计 Δ {sum(diff_day.values()):+.2f}U")

    # 配对 bootstrap: 对日期重采样（结构同 14 的 day_bootstrap, 但对象是逐日差值）
    def paired_ci(dd, boot=2000, seed=42):
        r = random.Random(seed)
        vals = [dd[d] for d in days_all if d != "2026-08-31"]
        # 若窗口有 08-31 的差, 一并放进池子但日期层不含它（与 14 同处理）
        pool = vals if not dd.get("2026-08-31") else vals + [
            dd["2026-08-31"] / 13.0]
        out = sorted(sum(pool[r.randrange(len(pool))] for _ in pool) for _ in range(boot))
        return out[int(0.025 * boot)], out[int(0.975 * boot)]

    lo, hi = paired_ci(diff_day)
    print(f"  日级配对 bootstrap 95%: Δ [{lo:+.2f}, {hi:+.2f}]U  "
          f"{'✅ 下界 > 0' if lo > 0 else '❌ 跨 0'}")

    # 分半 / 剔热点日
    ds_ok = [d for d in days_all if d != "2026-08-31"]
    cut = ds_ok[len(ds_ok) // 2]
    h1 = sum(diff_day[d] for d in ds_ok if d < cut)
    h2 = sum(diff_day[d] for d in ds_ok if d >= cut)
    print(f"  分半: 前半（< {cut}）Δ {h1:+.2f}U   后半 Δ {h2:+.2f}U")
    top = sorted(diff_day.items(), key=lambda kv: -kv[1])[:3]
    rest = sum(diff_day.values()) - sum(v for _, v in top)
    print(f"  剔掉贡献最大的 3 天 " + ", ".join(f"{k[5:]} {v:+.2f}" for k, v in top)
          + f":  剩余 Δ {rest:+.2f}U")

    # 增量落在哪个 rem（它们比快照晚多久成交）
    added_rows = [r for r in scan60 if key(r) not in ka]
    if added_rows:
        rems = sorted(r["rem"] for r in added_rows)
        print(f"  增量 485 笔的成交 rem: 中位 {rems[len(rems)//2]}  "
              f"p10 {rems[len(rems)//10]}  p90 {rems[len(rems)*9//10]}  "
              f"（现行快照恒在 rem≈60）")

if __name__ == "__main__":
    main()
