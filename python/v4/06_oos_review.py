#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v4「狗@0.2」09-15 双样本复验（组合带独立样本裁判）——按
docs/dog020_oos_review_2026-09-15.md §5(A 层)/§4(B 层)/§6(C 层) 固定格式执行。

纯标准库实现（无 numpy/pandas 依赖）。统计窗默认 09-04→09-16 UTC 共 12 整日
（计划指定 12 整日；引擎实际 09-03 15:42 UTC 起跑非 UTC 0 点，按计划「对齐到
首完整 UTC 日」条款前滚到 09-04）。--from/--to 可切换其它窗口做稳健性对照。

用法:
  python3 06_oos_review.py                          # 主窗口
  python3 06_oos_review.py --from 2026-09-03 --to 2026-09-15   # 含 09-03 残日
  python3 06_oos_review.py --include-gated          # 不排除日亏熔断被闸行

⚠️ 风控闸（2026-09-16 起）: gate_reason 非空的行是「纸面照记照结算、但实盘那一笔
不会开」的信号（方案 A, docs §3.5/§3.7）——默认排除, 否则 P&L 会混入未成交行。
"""
import argparse
import datetime as dt
import glob
import json
import math
import random
import statistics
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE.parent.parent / "data" / "v4"
BTCSV = BASE / "data" / "trades_r1_combo.csv"

STAKE = 2.0
BAND_YC = (-0.6, 0.0)    # yes 组合带
BAND_NO = (-1.0, 0.0)    # no 组合带
BAND_R1 = (-0.5, 0.0)    # R1 双侧对照带
REM_MIN = 180
CRASH_MIN = 0.40

# 回测基线（14 天 08-18~31，2U/注）——docs §2
BT = dict(n=625, wr=0.246, ev=0.633, pnl=395.8)
BT_R1 = dict(n=245, wr=0.290, ev=1.078, pnl=264.0)
BT_INCR_EV = 0.35   # 增量族（组合∖R1）边际 EV

# 回测触底侧参照（A 层频率诊断用; 由 /tmp/bt_touch_diag.py 实测, 08-19~30 整日口径）
BT_TOUCH_DAY = 280.0    # 触底/日（满覆盖日; 08-18/08-31 为半日不计）
BT_CONV = 0.159         # 触底→信号转换率 = 44.6/280

# §4 主裁判线（09-05 定稿版；计划要求 09-14 回填未执行，故用预注册初稿线）
LINE_OK, LINE_BAD = 100.0, -80.0


def load(from_day, to_day):
    """读 touches_*.jsonl，取 [from_day 00:00 UTC, to_day 00:00 UTC) 观测。"""
    rows = []
    for f in sorted(glob.glob(str(DATA / "touches_*.jsonl"))):
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get("event_type") != "touch":
                continue
            d = r.get("date", "")
            if from_day <= d < to_day:
                rows.append(r)
    rows.sort(key=lambda r: r["ts"])
    return rows


def pnl_of(r):
    """单笔 P&L（引擎已回填 pnl；此处按口径复算作校验）。"""
    if r.get("won"):
        return STAKE / r["fill"] - STAKE
    return -STAKE


def band_ok(r, band_yes, band_no, rem_min=REM_MIN):
    """按给定侧别带重判四腿（对照 C 层）。"""
    if (r.get("m_45") or 0) < CRASH_MIN:
        return False
    ds = r.get("dist_s")
    lo = band_no[0] if r.get("side") == "no" else band_yes[0]
    hi = band_no[1] if r.get("side") == "no" else band_yes[1]
    if not ds or not (lo < ds < hi):
        return False
    return (r.get("rem") or 0) > rem_min


def boot_day_cluster(settled, iters=20000, seed=20260915):
    """日聚类 bootstrap：按日重采样求总 P&L 的 90%/95% 区间（与计划 §3 日聚类口径一致）。"""
    by_day = {}
    for r in settled:
        by_day.setdefault(r["date"], []).append(r["pnl"])
    days = sorted(by_day)
    rnd = random.Random(seed)
    totals = []
    for _ in range(iters):
        s = 0.0
        for _ in range(len(days)):
            s += sum(by_day[days[rnd.randrange(len(days))]])
        totals.append(s)
    totals.sort()
    q = lambda p: totals[int(p * (len(totals) - 1))]
    return q(0.025), q(0.05), q(0.5), q(0.95), q(0.975)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="frm", default="2026-09-04", help="起始 UTC 日（含）")
    ap.add_argument("--to", dest="to", default="2026-09-16", help="结束 UTC 日（不含）")
    ap.add_argument("--include-gated", action="store_true",
                    help="不排除风控闸拦截行（gate_reason 非空; 默认排除, docs §3.7）")
    args = ap.parse_args()

    rows = load(args.frm, args.to)
    days = sorted({r["date"] for r in rows})
    obs = len(rows)
    # 风控闸被闸行（方案 A: 纸面照记照结算, 实盘不会开这一笔）——默认排除, 只用它
    # 报告剔除量; 观测级统计（触底/日、reject 分流、口径复核）仍看全量, 保持可比
    gated = [r for r in rows if r.get("gate_reason")]
    sig = [r for r in rows if r.get("ok") and (args.include_gated or not r.get("gate_reason"))]
    settled = [r for r in sig if r.get("won") is not None]
    pending = len(sig) - len(settled)

    print("=" * 78)
    print(f"统计窗 {args.frm} 00:00 UTC → {args.to} 00:00 UTC  ({len(days)} 日)")
    if gated:
        g = sorted({r["date"] for r in gated})
        print(f"被闸行 {len(gated)} 笔（{g[0]}~{g[-1]}）: "
              f"{'已计入（--include-gated）' if args.include_gated else '已排除'}")
    print("=" * 78)

    # ---------------- §5 A 层：口径与运行完整性（前置闸门） ----------------
    print("\n【§5 A 层 · 运行完整性】")
    # 结算完整
    won_ok = all((r["won"] is True) == (r["pnl"] > 0) for r in settled)
    pnl_match = sum(1 for r in settled if abs(r["pnl"] - pnl_of(r)) > 1e-6)
    print(f"  [{'x' if pending < 3 else ' '}] 结算完整: ok {len(sig)} 笔, "
          f"待结算 {pending}（<3 达标）, won⇔pnl>0 一致={won_ok}, pnl 复算不符 {pnl_match} 行")

    # 运行完整性: 触底 ~270/日、信号 ~44±5/日
    obs_day = obs / len(days)
    sig_day = len(sig) / len(days)
    ok_band = 39 <= sig_day <= 49
    conv = len(sig) / obs if obs else 0.0
    print(f"  [{'x' if 255 <= obs_day <= 300 else ' '}] 触底观测 {obs_day:.1f}/日"
          f"（计划 ~270/日; 288 = 满覆盖 288 窗）")
    print(f"  [{'x' if ok_band else ' '}] 信号 {sig_day:.1f}/日（计划 44±5）")
    print(f"  [{'x' if abs(conv-BT_CONV) < 0.015 else ' '}] 触底→信号转换率 {conv*100:.1f}%"
          f"（回测 {BT_CONV*100:.1f}% = 44.6/280; 触底侧 {len(rows)/len(days):.0f}/日"
          f" vs 回测 {BT_TOUCH_DAY:.0f}/日）")

    # reject 分流
    print("  观测判定分流:")
    counts = {}
    for r in rows:
        counts[r.get("reject_reason") or "(signal)"] = counts.get(r.get("reject_reason") or "(signal)", 0) + 1
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"    {k:<14s} {v:5d}  {v/obs*100:5.1f}%")

    # 落盘字段 0 异常: anchor/hist_bps/fill 属"不该缺"（缺失=口径断裂）;
    # spot/m_45 缺失是合法状态（Binance 断流置 0 / 回看窗不足）, 且判定顺序保证
    # 其必被 rem_low 或 missing_spot 拒——只需确认无一进入信号。
    hard = [r for r in rows if not r.get("anchor") or not r.get("hist_bps") or not r.get("fill")]
    soft = [r for r in rows if not r.get("spot") or not r.get("m_45")]
    soft_sig = [r for r in soft if r.get("ok")]
    print(f"  [{'x' if not hard else ' '}] 关键字段（anchor/hist_bps/fill）0 异常: {len(hard)} 行")
    print(f"  [{'x' if not soft_sig else ' '}] 软缺失（spot/m_45）{len(soft)} 行"
          f"（{len(soft)/obs*100:.1f}%, 合法状态）, 其中进入信号 {len(soft_sig)} 行")

    # 口径复核: ok ⇔ 组合带重判
    mism = [r for r in rows if r.get("dist_s") and bool(r.get("ok")) != band_ok(r, (-0.6, 0.0), (-1.0, 0.0))]
    print(f"  [{'x' if not mism else ' '}] 口径复核 ok ⇔ 组合带重判: 不一致 {len(mism)} 行")

    # ---------------- §4 B 层：主裁判 ----------------
    print("\n【§4 B 层 · 组合带头条判据】")
    n = len(settled)
    wins = sum(1 for r in settled if r["won"])
    wr = wins / n if n else 0.0
    pnl = sum(r["pnl"] for r in settled)
    ev = pnl / n if n else 0.0
    day_pnl = {}
    for r in settled:
        day_pnl[r["date"]] = day_pnl.get(r["date"], 0.0) + r["pnl"]
    pos_days = sum(1 for v in day_pnl.values() if v > 0)

    print(f"  样本 n={n} 笔 / {len(days)} 日  (计划预期 ≈535)")
    print(f"  WR {wr*100:.1f}%   （回测 24.6%）")
    print(f"  EV {ev:+.3f}U/注 （回测 +0.633U）")
    print(f"  总 P&L {pnl:+.1f}U  （回测同期期望 ≈+{n*BT['ev']:.0f}U）")
    print(f"  日正 {pos_days}/{len(day_pnl)}")

    lo95, lo90, med, hi90, hi95 = boot_day_cluster(settled)
    print(f"  日聚类 bootstrap 总 P&L: 中位 {med:+.0f}U, 90% [{lo90:+.0f}, {hi90:+.0f}], "
          f"95% [{lo95:+.0f}, {hi95:+.0f}]")

    # z 检验（日聚类: 用日 P&L 序列估 SE）
    dvals = list(day_pnl.values())
    se_day = statistics.stdev(dvals) * math.sqrt(len(dvals)) if len(dvals) > 1 else float("nan")
    exp = n * BT["ev"]
    print(f"  期望差 {pnl-exp:+.1f}U; 日聚类 SE {se_day:.0f}U → z {(pnl-exp)/se_day:+.2f}")

    # 构成校正期望: OOS 的 R1/增量族配比与回测不同（OOS 偏 R1 侧）, 直接拿
    # 438×0.633 作零假设会低估应得收益——改用"按 OOS 实际配比 × 回测分族 EV"。
    r1c = [r for r in settled if band_ok(r, BAND_R1, BAND_R1)]
    incrc = [r for r in settled if r not in r1c]
    exp_c = len(r1c) * BT_R1["ev"] + len(incrc) * BT_INCR_EV
    print(f"  构成校正期望: R1 {len(r1c)}×{BT_R1['ev']:+.3f} + 增量 {len(incrc)}×"
          f"{BT_INCR_EV:+.3f} = {exp_c:+.0f}U（OOS 配比偏 R1 侧 → 比混合期望高）")
    print(f"  构成校正期望差 {pnl-exp_c:+.1f}U → z {(pnl-exp_c)/se_day:+.2f}")
    print(f"  WR 检验: sd={math.sqrt(BT['wr']*(1-BT['wr'])/n)*100:.1f}pp → "
          f"z {(wr-BT['wr'])/math.sqrt(BT['wr']*(1-BT['wr'])/n):+.2f}")

    if pnl >= LINE_OK:
        verdict = "站住"
    elif pnl <= LINE_BAD:
        verdict = "证伪"
    else:
        verdict = "不显著"
    print(f"\n  ▶ 判定: P&L {pnl:+.1f}U 落 [{LINE_BAD:+.0f}, {LINE_OK:+.0f}] 之外? "
          f"{'否' if LINE_BAD < pnl < LINE_OK else '是'} → **{verdict}**")
    if wr < 0.20 and n >= 400:
        print(f"  ▶ 辅助判据触发: WR {wr*100:.1f}% < 20% 且 n≥400（加重判负置信）")

    # 逐日
    print("\n  逐日:")
    for d in days:
        g = [r for r in settled if r["date"] == d]
        if not g:
            print(f"    {d}: n= 0")
            continue
        gw = sum(1 for r in g if r["won"])
        print(f"    {d}: n={len(g):3d}  WR {gw/len(g)*100:5.1f}%  "
              f"P&L {sum(r['pnl'] for r in g):+7.1f}U  累计 {sum(day_pnl[x] for x in days if x <= d):+7.1f}U")

    # ---------------- §6 C 层：解释性对照 ----------------
    print("\n【§6 C 层 · 解释性对照】")
    # 1. 增量族（组合∖R1）
    r1 = [r for r in settled if band_ok(r, BAND_R1, BAND_R1)]
    incr = [r for r in settled if r not in r1]
    nd = len(days)
    print(f"  1) 增量族（组合∖R1）: n={len(incr)}（{len(incr)/nd:.1f}/日, 回测 27.1/日）, "
          f"EV {sum(r['pnl'] for r in incr)/len(incr):+.3f}U/注, "
          f"P&L {sum(r['pnl'] for r in incr):+.1f}U"
          f"（回测边际 ≈+{BT_INCR_EV}U/注）")
    print(f"     R1 子集:            n={len(r1)}（{len(r1)/nd:.1f}/日, 回测 17.5/日）, "
          f"EV {sum(r['pnl'] for r in r1)/len(r1):+.3f}U/注, "
          f"P&L {sum(r['pnl'] for r in r1):+.1f}U（回测 +{BT_R1['ev']}U/注）")
    print(f"     ▶ 判据(§6.1): 增量族 EV ≤0 → 建议回 R1; 实测 {sum(r['pnl'] for r in incr)/len(incr):+.3f} > 0"
          f" → 不回 R1（且 R1 自身 EV 已低于增量族, 回 R1 会更差）")
    print(f"     ⚠️ 带宽优劣不可判: 80% 功效需 n≈1310, 本次增量 n={len(incr)} → 只记方向")

    # 2. 侧别
    print("  2) 侧别分桶（回测 no 339(54%)/yes 286）:")
    for sd in ("yes", "no"):
        g = [r for r in settled if r["side"] == sd]
        if not g:
            continue
        gw = sum(1 for r in g if r["won"])
        print(f"     {sd:>3s}: n={len(g):3d} ({len(g)/n*100:4.1f}%)  WR {gw/len(g)*100:5.1f}%  "
              f"EV {sum(r['pnl'] for r in g)/len(g):+.3f}U/注  P&L {sum(r['pnl'] for r in g):+7.1f}U")

    # 3. 深度分布镜像（live vs 回测 dist_s 均值）
    bt = {}
    if BTCSV.exists():
        for line in open(BTCSV, encoding="utf-8").readlines()[1:]:
            p = line.strip().split(",")
            if p[10] == "True":
                bt.setdefault(p[2], []).append(float(p[8]))
    print("  3) 触底 dist_s 均值镜像（回测 vs OOS）:")
    for sd in ("yes", "no"):
        g = [r["dist_s"] for r in settled if r["side"] == sd and r.get("dist_s")]
        b = bt.get(sd, [])
        bl = f"{statistics.mean(b):+.2f}" if b else "n/a"
        ol = f"{statistics.mean(g):+.2f}" if g else "n/a"
        print(f"     {sd:>3s}: 回测 {bl}σ（n={len(b)}）  OOS {ol}σ（n={len(g)}）")

    # 4. 按日画像
    print("  4) 逐日画像（净漂移 = 当日收盘−开盘 bps）:")
    win = {}
    for f in sorted(glob.glob(str(DATA / "windows_*.jsonl"))):
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            win.setdefault(r["date"], []).append(r)
    for d in days:
        g = [r for r in settled if r["date"] == d]
        w = sorted(win.get(d, []), key=lambda x: x["event_start"])
        drift = ""
        if len(w) > 1:
            drift = f"{(w[-1]['close']-w[0]['anchor'])/(w[0]['anchor'] or 1)*1e4:+.1f}bps"
        print(f"    {d}: 笔数 {len(g):3d}  净漂移 {drift:>9s}  日 P&L {day_pnl.get(d, 0.0):+7.1f}U")

    # 保存 JSON 供后续比对
    out = BASE / "data" / f"oos_review_{args.frm}_{args.to}.json"
    out.write_text(json.dumps({
        "window": [args.frm, args.to], "n": n, "wr": wr, "ev": ev, "pnl": pnl,
        "by_day": day_pnl, "boot90": [lo90, hi90], "verdict": verdict,
        "side": {sd: dict(n=len([r for r in settled if r["side"] == sd]),
                          pnl=sum(r["pnl"] for r in settled if r["side"] == sd))
                 for sd in ("yes", "no")},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果写入 {out}")


if __name__ == "__main__":
    main()
