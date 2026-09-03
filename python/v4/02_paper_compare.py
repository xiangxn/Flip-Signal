#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v4 纸面记录对账与口径复核（dog@0.2 组合版侧别带——2026-09-03 起引擎现行口径;
早前 R1 (−0.5,0) 双侧已退居回测对照, 见 01 脚本 rules 前两条）。

读引擎落盘的 touches_*.jsonl（触发 tick 即记，成功/失败都记），输出:

  1) 观测量/信号量逐日统计（信号频率 ≈ 回测组合单层 44.6/日, 观测 ~260/日）
  2) 信号口径复核: 用记录里的 side/m_45/dist_s/rem 重判四腿（per-side 带
     yes(−0.6,0)/no(−1,0), 镜像 01 rules 组合版 pureC）, 与 ok 列比对
     （不一致告警——ok 与重判应恒等; dist_s=0=输入缺失不参与重判）
  3) 结算口径: 已结算信号的 WR/EV/P&L（won+pnl 回填行）, 待结算计数
  4) reject_reason 分布（对照实测: rem_low ~40%、no_crash 等）
  5) 与回测 trades_r1_combo.csv 的字段口径对照（可选项 --btcsv）: 同名键
     date/event_start/side/rem/fill/m_20/m_30/m_45/dist_s/dist_t 语义一致
     确认; 纸面与回测日期不重叠时不做逐笔对账, 只对照分布形状

对账键: |ts − event_start·1000| ≤ 2s（ts 为触底 unix 毫秒, event_start 为窗口
起点 unix 秒）——不用 rem（live 与数据有 ±1-2 tick 相位差, 见口径文档 §3）。

用法: python 02_paper_compare.py [--data <dir>] [--btcsv trades_r1_combo.csv]
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent

STAKE = 2.0
CRASH_MIN = 0.40
# 组合版侧别带（01 rules pureC 同源）: yes 破位中继 (−0.6,0) / no 顶部恐慌 (−1,0)
BAND_YC = (-0.6, 0.0)
BAND_NO = (-1.0, 0.0)
REM_MIN = 180
ALIGN_MS = 2000  # 对账对齐容差（ts 与 event_start 双重对齐）

# 观测记录 → 回测 CSV 的字段映射（口径文档 §2; key 全同名, 仅 won/settle_won 不同）
FIELD_MAP = {
    "date": "date", "event_start": "event_start", "side": "side", "rem": "rem",
    "fill": "fill", "m_20": "m_20", "m_30": "m_30", "m_45": "m_45",
    "dist_s": "dist_s", "dist_t": "dist_t",
    "ok": "in_pure", "won": "settle_won",
}


def load_records(data_dir):
    """读 touches_*.jsonl（按日 glob 排序, 保持时间序）。"""
    rows = []
    for f in sorted(Path(data_dir).glob("touches_*.jsonl")):
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    print(f"⚠️  {f.name}: 行解析失败跳过: {line[:80]}")
                    continue
                if r.get("event_type") != "touch":
                    print(f"⚠️  {f.name}: 跳过非 touch 行: {line[:80]}")
                    continue
                rows.append(r)
    if not rows:
        print(f"{data_dir}: 无记录（空跑/未部署？）")
        sys.exit(0)
    return rows


def rejudge(r):
    """用记录特征重判四腿（镜像 01 rules 组合版 pureC）; 输入缺失返回 None。"""
    if (r.get("m_45") or 0) < CRASH_MIN:
        return False
    ds = r.get("dist_s")
    lo = BAND_NO[0] if r.get("side") == "no" else BAND_YC[0]
    if not ds or not (lo < ds < 0.0):  # dist_s=0/缺 = 未计算
        return None
    if not (r.get("rem") or 0) > REM_MIN:
        return False
    return True


def main():
    ap = argparse.ArgumentParser(description="v4 纸面记录对账（dog@0.2 组合版侧别带）")
    ap.add_argument("--data", default=str(BASE.parent.parent / "data" / "v4"),
                    help="引擎 touches_*.jsonl 目录")
    ap.add_argument("--btcsv", default=str(BASE / "data" / "trades_r1_combo.csv"),
                    help="回测逐笔 CSV（对照口径用 = 组合版明细, 可不存在）")
    args = ap.parse_args()

    rows = load_records(args.data)
    df = pd.DataFrame(rows)
    df = df.sort_values("ts").reset_index(drop=True)
    df["date"] = df["date"].astype(str)
    df["event_ts"] = df["event_start"].astype(int) * 1000  # 对账键（毫秒）

    print(f"纸面记录 {args.data}: 观测 {len(df)} 行 / {df['date'].nunique()} 天")

    # 1) 信号口径复核: 重判 vs ok 列（应恒等; 输入缺失时重判为 None 不比对）
    j = df["ok"].map({True: 1, False: 0}).astype(float)
    rj = df.apply(lambda r: None if rejudge(r) is None else float(rejudge(r)), axis=1)
    cmp = df[["ok"]].copy()
    cmp["rejudge"] = rj
    both = cmp.dropna()
    if len(both):
        mismatch = int((both["ok"].astype(int) != both["rejudge"].astype(int)).sum())
        # ok=true 但重判 False 的行（真正值得查的口径漂移）
        drift = both[(both["ok"]) & (~both["rejudge"].astype(bool))]
        note = " — 一致 ✅"
        if len(drift):
            note = f"（⚠️ ok=true 但重判不过 {len(drift)} 行: {drift['ts'].tolist()[:5]}）"
        print(f"  口径复核: 可比 {len(both)} 行, 不一致 {mismatch}{note}")
    else:
        print("  口径复核: 全部观测输入缺失（dist_s=0），无法重判——σ/spot 持续缺失？")

    # 2) 观测与信号逐日统计
    print("\n逐日（观测 | 信号 ok | 信号/日 对照回测组合单层 ≈44.6）:")
    day_g = df.groupby("date")
    for d, g in sorted(day_g):
        print(f"  {d}: 观测 {len(g):3d} | 信号 {int(g['ok'].sum()):3d} | "
              f"待结算 {int(((g['ok']) & (g['won'].isna())).sum()):3d}")
    obs_day = len(df) / len(day_g)
    sig = df[df["ok"]]
    sig_day = len(sig) / len(day_g) if len(day_g) else 0
    print(f"  日均: 观测 {obs_day:.0f} | 信号 {sig_day:.1f}（回测组合单层 44.6/日）")

    # 3) reject 分布（全部观测）
    if "reject_reason" in df:
        print("\nreject_reason 分布（含待结算信号）:")
        vc = df["reject_reason"].fillna("(signal)").value_counts()
        total = len(df)
        for k, v in vc.items():
            print(f"  {k:<14s} {v:5d}  {v/total*100:5.1f}%")
        print(f"  {'合计':<14s} {total:5d}")

    # 4) 信号特征与结算统计
    if len(sig):
        print(f"\n信号 {len(sig)} 笔: side {sig['side'].value_counts().to_dict()}")
        print(f"  fill: mean {sig['fill'].mean():.3f}  min {sig['fill'].min():.3f}  "
              f"max {sig['fill'].max():.3f}（应 ≤0.20）")
        for col in ("m_20", "m_30", "m_45", "dist_s", "dist_t"):
            if col in sig:
                s = pd.to_numeric(sig[col], errors="coerce")
                print(f"  {col}: mean {s.mean():.2f}  [p5 {s.quantile(.05):.2f}, "
                      f"p95 {s.quantile(.95):.2f}]  n_calc {s.notna().sum()}")
        settled = sig[sig["won"].notna()]
        if len(settled):
            k = int(settled["won"].sum())
            pnl = settled["pnl"].sum()
            nd = settled["date"].nunique()
            print(f"  已结算 {len(settled)}/{len(sig)}: WR {k/len(settled)*100:.1f}%  "
                  f"P&L {pnl:+.1f}U/{nd}天  EV {pnl/len(settled):+.3f}U/注  "
                  f"（对照基准组合 WR 24.6% / EV +0.633U/注, 样本小别过早下结论）")
            print("  逐日:")
            for d, g in sorted(settled.groupby("date")):
                gk = int(g["won"].sum())
                print(f"    {d}: n={len(g):2d}  WR {gk/len(g)*100:5.1f}%  "
                      f"P&L {g['pnl'].sum():+6.1f}U")
        else:
            print(f"  已结算 0/{len(sig)}——全部待结算（正常: 每窗结算延迟 ≤5 分钟）")

    # 5) 与回测 CSV 口径对照（可选）: 字段同名映射 + 日期不重叠时的形状对照
    btcsv = Path(args.btcsv)
    if btcsv.exists():
        bt = pd.read_csv(btcsv)
        print(f"\n回测 CSV {btcsv.name}: {len(bt)} 行, 日期 {bt['date'].min()}~{bt['date'].max()}")
        print("  字段映射核对:")
        missing = [k for k, v in FIELD_MAP.items() if v not in bt.columns]
        print(f"    {btcsv.name}: 缺 {missing if missing else '无'}")
        have = [k for k in df.columns if k in FIELD_MAP and k in ("dist_s", "ok", "won")]
        print(f"    touches 记录: 对齐键 event_start 存在={ 'event_start' in df.columns }, "
              f"won 回填={int(df['won'].notna().sum())} 行")
        # 日期重叠部分（理论上无: 纸面从 09-02 起）才做逐笔对账
        common = set(bt["date"]) & set(df["date"])
        if common:
            print(f"  ⚠️ 日期重叠 {sorted(common)[:5]}——做逐笔对齐: ")
            bt_es = set(bt["event_start"])
            ok_rows = df[df["ok"]]
            aligned = ok_rows[ok_rows["event_start"].isin(bt_es)]
            hit = 0
            for _, r in aligned.iterrows():
                cand = bt[(bt["event_start"] == r["event_start"]) &
                          (abs(bt["event_start"] * 1000 - r["event_ts"]) <= ALIGN_MS)]
                if len(cand):
                    hit += 1
            print(f"    对齐命中 {hit}/{len(aligned)}（|ts−event_start·1000| ≤ {ALIGN_MS}ms）")
        else:
            print("  日期无重叠（纸面 09-02 起 vs 回测 08-18~31）——按分布对照即可,"
                  " 不做逐笔对账")
    else:
        print(f"\n（--btcsv 不存在, 跳过回测对照: {args.btcsv}）")

    # 落盘轻量摘要（供 09-15 快照比较）
    out = BASE / "data" / "paper_summary_tsv.txt"
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(f"asof  {df['ts'].max()}\n")
        fh.write(f"days  {df['date'].nunique()}\n")
        fh.write(f"obs   {len(df)}\n")
        fh.write(f"sig   {len(sig)}\n")
        fh.write(f"settled {int(sig['won'].notna().sum()) if len(sig) else 0}\n")
        fh.write(f"wr    {(sig['won'].sum()/sig['won'].notna().sum()) if len(sig) and sig['won'].notna().any() else float('nan')}\n")
        fh.write(f"pnl   {sig['pnl'].sum() if len(sig) else 0.0}\n")
    print(f"\n摘要已写入 {out}")


if __name__ == "__main__":
    main()
