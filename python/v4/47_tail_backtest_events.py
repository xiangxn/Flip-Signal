#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫尾盘：**拿现行 oracle 跑 cmd/tail 自己的采集数据（data/events）**。

动机（2026-10-11 用户需求「你用 tail 的回测代码跑一下 data/events 下的数据看看是什么
情况」）：`data/events` 是 cmd/tail 兼作采集器（决策 #27）从 2026-09-27 起落下的
原始采样（数据格式 v2，与 `data/btc` 逐字段同构）——它是**引擎自己的眼睛**，
与新口径 σ / 有效价门 / 最新门槛一起出现。本脚本回答两个问题：

  ① 引擎在这一段真实数据上会下多少单、赢多少、赚多少（**逐位用现行 oracle**，
     即 parity 钉的那条链）；
  ② 这段数据与 14 天 BTC 基线（`data/btc`）有什么不同——σ 就绪率、样本频率、空簿形态。

口径（与 23/38/41/42 同源，**不新造**）：

    加载   = 本文件自带 load_events（与 v2.lib 同构; 同 cmd/collect 落盘格式, 修正行自动合并）
    判定链 = s23.chain（三段递进 + T150 walk 闸 43 + 价格地板 > 0.83, 决策 #29/#32/#33）
    σ      = s13.hist_ranges（前 ≤18 窗 |twap_close − twap_open| 均值, ≥3 窗才就绪）
    门控   = **引擎有效价门**（决策 #21/#22）= 每侧 `ask > 0 ? ask : bid`，四档全 0 才无效
    成交   = fill = 下单 tick 热门侧有效价; shares = STAKE/fill; 赢 +shares−STAKE / 输 −STAKE

⚠️ 2026-10-11 门统一（用户决定「有效价门只是数据有效性的处理…把回测统一到它上面」）：
四档门是旧代码对**旧采集（丢消息留下冻结簿）**的补偿口径，不是策略口径——本脚本与
oracle 23 都已删掉它，全程只有有效价门一条。旧 data/btc 上两门逐位同源（单侧空 ask
的 tick = 0）⇒ parity pin 不动；本数据（正确采集）上两门差 ~43%（见第七节），
那里只有有效价门一套口径。

用法: python/venv/bin/python python/v4/47_tail_backtest_events.py [--dir data/events] [--stoploss] …
"""

import re
import sys
import json
import random
import argparse
import datetime
import collections
import importlib.util
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))

STAKE = 2.0
FEE_RATE = 0.07            # tail.fee_rate（决策 #35, 官方 Crypto 档）——单独一节给扣费口径
SEED = 42
BOOT = 2000


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


s23 = load("s23", BASE / "23_tail_integrated.py")   # oracle：chain / row / r2 / r5
s13 = load("s13", BASE / "13_tail_sweep.py")        # hist_ranges（σ）


def load_events(data_dir):
    """与 v2.lib.load_events 同构（移植一份避免 python 包导入路径依赖）。"""
    events, corrections = [], {}
    for p in sorted(Path(data_dir).glob("events_*.jsonl")):
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get("event_type") == "settlement_correction":
                    corrections[rec["start_time"]] = rec
                else:
                    events.append(rec)
    for e in events:
        corr = corrections.get(e["start_time"])
        if corr:
            e.update({k: v for k, v in corr.items() if k != "event_type"})
    events.sort(key=lambda e: e["start_time"])
    return events


def win_ticks(e):
    """本窗可判定 tick（rem 递减）。门 = **引擎有效价门**（决策 #21/#22, 见文件头）。

    每侧有效价 = ask > 0 ? ask : bid; side = 有效价较大者（平局取 yes）;
    fill = 该侧有效价（bid 兜底时记 src=bid）; 四档全空才判无效。
    """
    out = []
    anchor = e.get("twap_open_price")
    if not anchor:
        return out
    for x in e.get("ticks") or []:
        p = x.get("pm") or {}
        if not p or (p.get("book_latency_ms") or 0) > s23.MAX_LAT:
            continue
        rem = x.get("rem")
        if rem is None or rem <= 0 or rem > s23.T150:
            continue
        eff, src_of = {}, {}
        for side in ("yes", "no"):
            bid, ask = p.get(f"{side}_bid") or 0, p.get(f"{side}_ask") or 0
            eff[side] = ask if ask > 0 else bid
            src_of[side] = "ask" if ask > 0 else "bid"
        if not any(eff.values()):
            continue              # 整簿全空（四档齐 0）⇒ 无有效价
        spot = (x.get("bin") or {}).get("price")
        if not spot:
            continue
        side = "yes" if eff["yes"] >= eff["no"] else "no"
        fill, src = eff[side], src_of[side]
        out.append({"rem": rem, "side": side, "fill": fill, "src": src,
                    "spot": spot, "twap": (x.get("twap") or {}).get("price"),
                    # 止损用: 两侧最优 bid 原样带上（bid == 0 = 整侧撤空, 不是缺数据）
                    "yb": p.get("yes_bid") or 0, "nb": p.get("no_bid") or 0})
    return out


def fee_of(shares, px, fee_rate):
    """官方公式 fee = C×rate×p×(1−p)，5 位小数（决策 #35）。"""
    return round(shares * fee_rate * px * (1 - px), 5) if fee_rate else 0.0


def net_won(r):
    """止损失效后的实际**净利润（U, 含双向 fee, stake=STAKE）**。

    无止损行（r 里没有 exit_price）= 持有到期: 赢 shares−STAKE / 输 −STAKE。
    止损行 = 触发 tick 的持仓侧 bid 全额卖出: exit_shares·exit_price − STAKE − 卖费;
    剩余股数若赢按每股 1U 兑付。fee 率从 r["_fee_rate"] 读（0 = 不计费）。
    """
    sh = STAKE / r["fill"]
    fr = r.get("_fee_rate", FEE_RATE)
    if "exit_price" not in r:
        gross = (sh - STAKE) if r["settle_won"] else -STAKE
        return gross - fee_of(sh, r["fill"], fr)
    sold = min(r["exit_shares"], sh)
    rest = sh - sold
    gross = sold * r["exit_price"] + (rest if r["settle_won"] else 0.0) - STAKE
    return gross - fee_of(sh, r["fill"], fr) - fee_of(sold, r["exit_price"], fr)


def pl_rows(rows, fee_rate=0.0):
    """样本 P&L（U）。把 fee_rate 写进每行再走 net_won（0 = 不计费, 未扣费口径）。"""
    for r in rows:
        r["_fee_rate"] = fee_rate
    return sum(net_won(r) for r in rows)


def by_day(rows):
    d = collections.defaultdict(list)
    for r in rows:
        d[r["date"]].append(r)
    return d


def day_bootstrap(rows, ds, rng, reps=BOOT, fee_rate=0.0):
    d = by_day(rows)
    bp = []
    for _ in range(reps):
        pool = [r for k in (rng.choice(ds) for _ in ds) for r in d[k]]
        bp.append(pl_rows(pool, fee_rate))
    bp.sort()
    q = lambda p: bp[int(p * len(bp))]                          # noqa: E731
    return q(0.025), q(0.975)


def fmt_line(lab, rows, width=26, fee_rate=0.0):
    """一行统计。fee_rate > 0 时 P&L/EV **含双向 fee**（止损行按 exit 价再加一笔卖费）。"""
    if not rows:
        print("  " + lab.ljust(width) + "n=0")
        return
    n = len(rows)
    wr = sum(r["settle_won"] for r in rows) / n * 100
    px = sum(r["fill"] for r in rows) / n
    neg = sum(1 for v in by_day(rows).values() if pl_rows(v, fee_rate) < 0)
    tag = "·扣费" if fee_rate else ""
    print("  " + lab.ljust(width) + f"n={n:<5} WR {wr:6.2f}%  均价 {px:.4f}  "
          f"EV/注 {pl_rows(rows, fee_rate)/n:+.4f}{tag}  P&L {pl_rows(rows, fee_rate):+7.2f}U{tag}  "
          f"亏损日 {neg}/{len(by_day(rows))}")


def universe(ev):
    """→ (wins, stats)。wins 每项 = {date, outcome, anchor, sd, ticks}。"""
    hr = s13.hist_ranges(ev)
    wins = []
    st = collections.Counter()
    for e in ev:
        outcome, anchor = e.get("outcome"), e.get("twap_open_price")
        if outcome is None or not anchor:
            st["缺 outcome/anchor"] += 1
            continue
        st["有 outcome+anchor"] += 1
        if hr.get(e["start_time"]) is None:
            st["σ 未就绪（no_sigma）"] += 1
            continue
        sd = hr[e["start_time"]] / anchor * 1e4 * anchor / 1e4
        date = datetime.datetime.fromtimestamp(
            e["start_time"], datetime.timezone.utc).strftime("%Y-%m-%d")
        ticks = win_ticks(e)
        if not ticks:
            st["无可判定 tick"] += 1
            continue
        st["参与判定"] += 1
        wins.append({"date": date, "outcome": outcome, "anchor": anchor,
                     "sd": sd, "ticks": ticks, "start": e["start_time"]})
    return wins, st


def chain_floor(ticks, anchor, sd, date, outcome, walk_min, floor, strict=True):
    """`s23.chain` 的参数化拷贝：walk 闸阈值与价格地板阈值可配（其余逐行同源）。

    `walk_min=0` ⇒ 关闸（与引擎 `WalkLeg` 在 `cfg.WalkMinUSD == 0` 时放行同口径）。
    `floor=0` ⇒ 关地板。`strict=False` ⇒ t150 价格腿回落非严格 ≥（只在默认值下与
    oracle 逐位一致的口径，本脚本不用于本次对照，留作回归自检用）。
    """
    if not ticks:
        return [], []
    rows, rejects = [], []
    head = ticks[0]
    if head["rem"] > s23.T60:
        r = s23.row(head, anchor, sd, date, outcome, "t150")
        ok5 = s23.r5(r, strict_price=strict)
        if ok5 and (walk_min <= 0 or s23.walk_ok(r, walk_min)):
            r["ok"] = True
            return [r], rejects
        rejects.append("t150:walk_low" if ok5 else "t150")
        rows.append(r)
        rest = [x for x in ticks if x["rem"] <= s23.T60]
    else:
        rest = ticks
    shadowed = False
    if rest:
        t2 = rest[0]
        r2r = s23.row(t2, anchor, sd, date, outcome, "t60")
        if s23.r5(r2r):
            if floor > 0 and r2r["fill"] <= floor:
                r2r["reject_reason"] = "floor_low"
                rows.append(r2r)
                shadowed = True
            else:
                r2r["ok"] = True
                return rows + [r2r], rejects
        else:
            rejects.append("t60")
            rows.append(r2r)
        for x in rest[1:]:
            rl = s23.row(x, anchor, sd, date, outcome, "listen")
            if not s23.r2(rl):
                continue
            if floor > 0 and rl["fill"] <= floor:
                if not shadowed:
                    rl["reject_reason"] = "floor_low"
                    rows.append(rl)
                    shadowed = True
                continue
            rl["ok"] = True
            rows.append(rl)
            return rows, rejects
    return rows, rejects


def apply_stop(w, sigrow, stop_bid=0.30):
    """止损腿（决策 #34）：成交后逐 tick 看**持仓侧 bid** 严格小于 stop_bid 且 > 0
    ⇒ 按该 tick 快照 bid **全额卖出**（paper 无最小股数约束, live 才套 MinSellShares）。

    触发后不再有后续 taker 检查（引擎同口径: 卖出即终局）。行内落
    `exit_price / exit_shares / exit_rem`（与引擎的 `exit_*` 字段同名）。
    """
    side = sigrow["side"]
    for t in w["ticks"]:
        if t["rem"] >= sigrow["rem"]:
            continue                      # 只看出场时刻**之后**的 tick
        bid = t["yb"] if side == "yes" else t["nb"]
        if 0 < bid < stop_bid:
            sigrow["exit_price"] = bid
            sigrow["exit_shares"] = STAKE / sigrow["fill"]
            sigrow["exit_rem"] = t["rem"]
            return True
    return False


def run_chain(wins, walk_min=43.0, floor=0.83, stop=False, stop_bid=0.30):
    """跑参数化的三段链。返回信号/行/被拦明细。

    ⚠️ oracle 把 T150 段的入场闸编码在**拒绝列表**里（`"t150:walk_low"`），行的
    `reject_reason` 仍是空（与引擎的 `walk_low` 落盘字段不同名）⇒ 这里按拒绝列表
    对应回那一行（每个带该记号的窗，其 t150 行就是被闸行）。
    """
    sig = {s: [] for s in s23.STAGES}
    rows_n = collections.Counter()
    blocked = {"walk_low": [], "floor_low": []}
    n_walk_low = 0
    n_stop = 0
    for w in wins:
        rows, rej = chain_floor(w["ticks"], w["anchor"], w["sd"], w["date"],
                                w["outcome"], walk_min, floor)
        wl = sum(1 for x in rej if x.endswith("walk_low"))
        n_walk_low += wl
        head = next((r for r in rows if r["stage"] == "t150"), None)
        okrow = next((r for r in rows if r.get("ok")), None)
        if stop and okrow is not None:
            n_stop += apply_stop(w, okrow, stop_bid)
        for r in rows:
            r["start"] = w["start"]
            rows_n[r["stage"]] += 1
            if r.get("ok"):
                sig[r["stage"]].append(r)
            elif r.get("reject_reason") in blocked:
                r["block_kind"] = r["reject_reason"]
                blocked[r["reject_reason"]].append(r)
        if wl and head is not None:
            head["block_kind"] = "walk_low"
            blocked["walk_low"].append(head)
    return sig, rows_n, blocked, n_walk_low, n_stop


def audit_book_shape(ev):
    """数据形态审计: 空簿 / 单侧空 ask（有效价门 bid 兜底照收）/ twap 缺失。

    ⚠️ **不是**两套门的差集——python 与引擎现在是同一条有效价门（见文件头）。
    这里数的是数据本身的长相: 「四档部分缺」= 旧四档门会丢掉、而引擎照收的那些 tick。
    """
    st = collections.Counter()
    for e in ev:
        if not e.get("twap_open_price"):
            continue
        for x in e.get("ticks") or []:
            p = x.get("pm") or {}
            if not p or (p.get("book_latency_ms") or 0) > s23.MAX_LAT:
                continue
            rem = x.get("rem")
            if rem is None or rem <= 0 or rem > s23.T150:
                continue
            st["参与审计 tick"] += 1
            yb, ya = p.get("yes_bid") or 0, p.get("yes_ask") or 0
            nb, na = p.get("no_bid") or 0, p.get("no_ask") or 0
            eff_yes, eff_no = (ya if ya > 0 else yb), (na if na > 0 else nb)
            if not (eff_yes > 0 or eff_no > 0):
                st["整簿全空（四档齐 0, 判无效）"] += 1
                continue
            if yb > 0 and ya > 0 and nb > 0 and na > 0:
                if not (x.get("twap") or {}).get("price"):
                    st["四档齐但 twap 缺（walk 闸 fail-open）"] += 1
                continue
            st["四档部分缺（单侧空 ask, bid 兜底照收）"] += 1
            # 谁是被拿来成交的那一侧（有效价高者）
            side = "yes" if eff_yes >= eff_no else "no"
            if (p.get(f"{side}_ask") or 0) > 0:
                st["  └ 被选侧仍是 ask"] += 1
            else:
                st["  └ 被选侧走 bid 兜底"] += 1
    return st


def main():
    global STAKE
    ap = argparse.ArgumentParser(description="扫尾盘回测：data/events（cmd/tail 自采）")
    ap.add_argument("--dir", default="data/events", help="数据目录（默认 data/events）")
    ap.add_argument("--stake", type=float, default=2.0)
    ap.add_argument("--fee-rate", type=float, default=0.07)
    ap.add_argument("--walk-min", type=float, default=s23.WALK_MIN_USD,
                    help="T=150 段入场闸阈值；0 = 关闭（与引擎 WalkLeg 同口径）")
    ap.add_argument("--floor", type=float, default=s23.FLOOR_MIN_PRICE,
                    help="T=60/监听段价格地板；0 = 关闭")
    ap.add_argument("--stoploss", action="store_true",
                    help="开启持仓止损（决策 #34）：持仓侧 bid 严格 < --stop-bid 且 > 0 ⇒ 按快照 bid 全额卖出")
    ap.add_argument("--stop-bid", type=float, default=0.30, help="止损触发价（默认 0.30）")
    args = ap.parse_args()

    STAKE = args.stake

    walk_lab = "关闭" if args.walk_min <= 0 else f"≥ {args.walk_min:g} 美元"
    floor_lab = "关闭" if args.floor <= 0 else f"> {args.floor:g}"
    stop_lab = f"开（bid < {args.stop_bid:g} ⇒ 快照 bid 全额卖）" if args.stoploss else "关"
    print("=" * 104)
    print(f"扫尾盘回测 · 数据 = {args.dir} · 门控 = 引擎有效价门（决策 #21/#22） · stake = {STAKE}U/注")
    print("=" * 104)
    print(f"  链 = oracle 23 参数化（三段递进 + T150 walk 闸 {walk_lab} + "
          f"T60/监听价格地板 {floor_lab}）")
    print(f"  止损 = {stop_lab}")
    print(f"  σ  = hist_ranges（前 ≤18 窗 |close−open| 均值, ≥3 窗就绪）；"
          f"缺 σ 整窗跳过（决策 #13）")

    ev = load_events(args.dir)
    print(f"  载入 {len(ev)} 个窗口"
          f"（{ev[0]['start_time']} ~ {ev[-1]['start_time']}）"
          if ev else "  ⚠️ 没有数据")

    wins, st = universe(ev)

    print("\n" + "=" * 104)
    print("一、宇宙（参与判定 = 有 outcome ∧ 有锚 ∧ σ 就绪 ∧ 有可判定 tick）")
    print("=" * 104)
    for k, v in st.items():
        print(f"  {k:<28} {v}")
    if not wins:
        return

    sig, rows_n, blocked, n_walk_low, n_stop = run_chain(
        wins, walk_min=args.walk_min, floor=args.floor,
        stop=args.stoploss, stop_bid=args.stop_bid)
    allsig = [r for s in s23.STAGES for r in sig[s]]
    days = sorted(set(r["date"] for r in allsig))
    rng = random.Random(SEED)

    print("\n" + "=" * 104)
    print(f"二、三段递进判定链（stake {STAKE:g}U/注; 表内为**未扣费** P&L, 末日行给扣费口径）")
    print("=" * 104)
    for s in s23.STAGES:
        fmt_line(f"{s} 段信号", sig[s])
    fmt_line("合计（三段全部）", allsig)
    fmt_line("合计·扣费后", allsig, fee_rate=args.fee_rate)
    print("\n  日级 bootstrap 95%（2000 次, seed 42）:")
    for s in s23.STAGES:
        r = sig[s]
        if len(r) < 5:
            print(f"    {s:<8} [样本薄]")
            continue
        lo, hi = day_bootstrap(r, sorted(set(x['date'] for x in r)), rng)
        print(f"    {s:<8} [{lo:+.1f}, {hi:+.1f}]")
    lo, hi = day_bootstrap(allsig, days, rng)
    print(f"    {'合计':<8} [{lo:+.1f}, {hi:+.1f}]")

    print("\n" + "=" * 104)
    print("三、行计数（判定行 + 信号行 + 影子行; 与 oracle 的计数器同口径）")
    print("=" * 104)
    print(f"  {'段':<8}{'全部行':>8}{'信号行':>8}{'其余':>8}")
    for s in s23.STAGES:
        print(f"  {s:<8}{rows_n[s]:>8}{len(sig[s]):>8}{rows_n[s]-len(sig[s]):>8}")
    print(f"  {'合计':<8}{sum(rows_n.values()):>8}{len(allsig):>8}"
          f"{sum(rows_n.values())-len(allsig):>8}")
    wl, fl = blocked["walk_low"], blocked["floor_low"]
    print(f"  T=150 入场闸拦下（walk_low）= {n_walk_low} 行"
          f"（照买会产生的 P&L: {pl_rows(wl, 0.0):+.2f}U, "
          f"WR {sum(r['settle_won'] for r in wl)/max(1,len(wl))*100:.1f}%）")
    print(f"  价格地板拦下（floor_low）= {len(fl)} 行"
          f"（照买会产生的 P&L: {pl_rows(fl, 0.0):+.2f}U, "
          f"WR {sum(r['settle_won'] for r in fl)/max(1,len(fl))*100:.1f}%）")
    # 被闸那批的分流：整窗死亡（链上再没出过 OK 行）vs 改道（后面的段接了单）
    def divert(g):
        with_sig = {r["start"] for r in allsig}
        die = [r for r in g if r["start"] not in with_sig]
        same = [r for r in g if r["start"] in with_sig]
        return die, same
    for lab, g in (("walk_low", wl), ("floor_low", fl)):
        if not g:
            continue
        die, same = divert(g)
        print(f"    └ {lab}: 整窗死亡 {len(die)} 笔（照买 {pl_rows(die, 0.0):+.2f}U）"
              f" + 改道重入 {len(same)} 笔（同窗另有一段接了单）")

    if args.stoploss:
        # 止损细账（决策 #34 的形态）: 触发数 / 救输杀赢 / 净 Δ（同一批信号, 含双向费）
        trig = [r for r in allsig if "exit_price" in r]
        saved = [r for r in trig if not r["settle_won"]]
        killed = [r for r in trig if r["settle_won"]]
        hold_rows = [{k: v for k, v in r.items() if k != "exit_price"} for r in trig]
        pl_rows(trig, args.fee_rate)
        pl_rows(hold_rows, args.fee_rate)
        delta = sum(net_won(r) for r in trig) - sum(net_won(r) for r in hold_rows)
        print(f"\n  止损（bid < {args.stop_bid:g}）: 触发 {len(trig)} 次"
              f"（占信号 {len(trig)/max(1,len(allsig))*100:.1f}%）")
        print(f"    └ 救输 {len(saved)} 笔（无止损会亏 {sum(-STAKE for _ in saved):+.2f}U, "
              f"止损袋回 {sum(r['exit_shares']*r['exit_price'] for r in saved):+.2f}U）"
              f" / 杀赢 {len(killed)} 笔（袋回 {sum(r['exit_shares']*r['exit_price'] for r in killed):+.2f}U）")
        print(f"    └ 净 Δ（含买卖双向 fee）= {delta:+.2f}U")
        if trig:
            er = sorted(r["exit_price"] for r in trig)
            erm = sorted(r["exit_rem"] for r in trig)
            print(f"    └ 出场价中位 {er[len(er)//2]:.3f}（min {er[0]:.2f} max {er[-1]:.2f}）  "
                  f"触发时 rem 中位 {erm[len(erm)//2]}")

    print("\n" + "=" * 104)
    print("四、逐日明细（P&L = 未扣费；P&L·扣费 = 含双向 fee）")
    print("=" * 104)
    print(f"  {'日期':<12}{'t150':>7}{'t60':>7}{'listen':>8}{'合计':>7}{'WR':>8}"
          f"{'P&L':>10}{'P&L·扣费':>11}{'EV/注·扣费':>11}")
    for d in days:
        per = {s: [r for r in sig[s] if r["date"] == d] for s in s23.STAGES}
        day = [r for r in allsig if r["date"] == d]
        wr = sum(r["settle_won"] for r in day) / len(day) * 100
        ng = pl_rows(day, 0.0); nn = pl_rows(day, args.fee_rate)
        print(f"  {d:<12}{len(per['t150']):>7}{len(per['t60']):>7}{len(per['listen']):>8}"
              f"{len(day):>7}{wr:>7.1f}%{ng:>+10.2f}{nn:>+11.2f}{nn/len(day):>+11.3f}")
    ng = pl_rows(allsig, 0.0); nn = pl_rows(allsig, args.fee_rate)
    print(f"  {'合计':<12}{len(sig['t150']):>7}{len(sig['t60']):>7}{len(sig['listen']):>8}"
          f"{len(allsig):>7}{sum(r['settle_won'] for r in allsig)/len(allsig)*100:>7.1f}%"
          f"{ng:>+10.2f}{nn:>+11.2f}{nn/len(allsig):>+11.3f}")

    print("\n" + "=" * 104)
    print("五、信号特征（均价 / 侧别 / 段）")
    print("=" * 104)
    print(f"  均价 {sum(r['fill'] for r in allsig)/len(allsig):.4f}  "
          f"fill ≤0.83: {sum(1 for r in allsig if r['fill']<=0.83)} 笔 "
          f"({pl_rows([r for r in allsig if r['fill']<=0.83], 0.0):+.2f}U) / "
          f">0.83: {sum(1 for r in allsig if r['fill']>0.83)} 笔 "
          f"({pl_rows([r for r in allsig if r['fill']>0.83], 0.0):+.2f}U)")
    for side in ("yes", "no"):
        g = [r for r in allsig if r["side"] == side]
        fmt_line(f"侧 {side}", g, width=12)
    for s in s23.STAGES:
        g = sig[s]
        if g:
            print(f"  {s:<8} 均价 {sum(r['fill'] for r in g)/len(g):.4f}  "
                  f"rem 中位 {sorted(r['rem'] for r in g)[len(g)//2]}")

    print("\n" + "=" * 104)
    print("六、扣费口径（决策 #35: fee = C×{rate}×p×(1−p), 5 位小数, 只有 taker 付费）".format(rate=args.fee_rate))
    print("=" * 104)
    print(f"  ⚠️ 回测没有「挂单成交」概念 ⇒ 全部按 taker 计费 = **上界口径**"
          f"（实盘里挂到簿上被吃掉的 maker 部分 0 费）")
    gross = pl_rows(allsig, 0.0)
    net = pl_rows(allsig, args.fee_rate)
    print(f"  未扣费 {gross:+.4f}U → 扣费 {net:+.4f}U（费 {gross-net:.4f}U, "
          f"吃掉 {abs(gross-net)/max(abs(gross),1e-9)*100:.1f}%）")
    lo_g, hi_g = day_bootstrap(allsig, days, rng)
    lo_n, hi_n = day_bootstrap(allsig, days, rng, fee_rate=args.fee_rate)
    print(f"  日级 95%: 未扣费 [{lo_g:+.1f}, {hi_g:+.1f}] → 扣费 [{lo_n:+.1f}, {hi_n:+.1f}]")

    # 止损 A/B（同批信号, 只差止损腿; 含双向 fee）
    on_n = pl_rows(allsig, 0.0)
    on_g = pl_rows(allsig, args.fee_rate)
    hold = [{k: v for k, v in r.items() if k != "exit_price"} for r in allsig]
    off_n = pl_rows(hold, 0.0)
    off_g = pl_rows(hold, args.fee_rate)
    print(f"\n  止损 A/B（同 {len(allsig)} 笔信号, 扣费口径）: "
          f"持有到期 {off_g:+.2f}U → 带止损 {on_g:+.2f}U  ⇒ Δ {on_g-off_g:+.2f}U"
          f"（未扣费 Δ {on_n-off_n:+.2f}U）")

    print("\n" + "=" * 104)
    print("七、空簿形态审计（单侧空 ask 的 tick = 有效价门 bid 兜底照收的那些）")
    print("=" * 104)
    aud = audit_book_shape(ev)
    if not aud:
        print("  零命中。")
    else:
        # 固定顺序打印（most_common 会按计数把「└ 子项」插到父项前面, 看起来像串了行）
        order = ["参与审计 tick", "整簿全空（四档齐 0, 判无效）",
                 "四档部分缺（单侧空 ask, bid 兜底照收）",
                 "  └ 被选侧走 bid 兜底", "  └ 被选侧仍是 ask",
                 "四档齐但 twap 缺（walk 闸 fail-open）"]
        for k in order:
            if aud.get(k):
                print(f"  {k:<36} {aud[k]}")


if __name__ == "__main__":
    main()
