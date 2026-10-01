#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v7-02: 建**实盘**数据集（`data/tail-live/tail_*.jsonl` → core 特征 + 现行规则重放）。

为什么只能测 core（见 `docs/gbm_2026-10-01.md` §0.4）:
  * 实盘行是**决策点快照**（引擎每秒不落盘, 只在三段判定点落行）⇒ 没有逐秒盘口序列,
    `spot_ret_10s_sigma` / `giveback_sigma` / `vol_*` 这类**窗口内动量/量比**算不出来;
  * core 8 个（fill / sd_bps / dev_sigma / walk_sigma / basis_sigma / rem /
    book_latency_ms / twap_age_ms）**每个都直接落在行里**, 无需其他行。

两份产物:
  * `live_rows.json` —— 逐行（含未达标的判定行）, 带 `ok_now`（**现行规则**下的判定）
  * `live.npz`      —— 模型输入（core 矩阵 + 元数据）

⚠️ 口径两条:
  1. **重放不是重算**: 只在**已落盘的那些行**上跑现行规则（老版本只在监听段达标时才落行
     ⇒ 监听段的重放是「best effort」, 可能漏掉现行规则会下单的那些 tick）。文档 §5 已写明。
  2. `fill` 一律取行内 `hot_ask`（= 引擎的有效价, `hot_src` 说明它来自 ask 还是 bid）——
     与 `internal/tail.HotBook` 同口径; ⚠️ 实盘 41% 来自 bid 兜底, 回测里是 0。

用法: python/venv/bin/python python/v7/02_build_live_dataset.py [--dir data/tail-live]
"""
import argparse
import glob
import json
import sys
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import lib_universe as U          # noqa: E402

CORE_TICK = ["fill", "sd_bps", "dev_sigma", "walk_sigma", "basis_sigma", "rem",
             "book_latency_ms", "twap_age_ms"]
STAGE_ID = {"t150": 0, "t60": 1, "listen": 2}


def feats_of(r):
    """一行 → core 8 特征（口径与 lib_features.window_features 的 core 子集逐位一致）。"""
    sd = r.get("sd")
    anchor = r.get("anchor")
    twap = r.get("twap")
    sgn = 1.0 if r.get("side") == "yes" else -1.0
    fill = r.get("hot_ask")
    if not fill or not sd or not anchor:
        return None
    walk = sgn * (twap - anchor) if (twap and twap > 0) else float("nan")
    dev = r.get("dev")
    return {
        "fill": float(fill),
        "sd_bps": float(sd) / float(anchor) * 1e4,
        "dev_sigma": float(dev) / float(sd),
        "walk_sigma": walk / float(sd),
        "basis_sigma": (float(dev) - walk) / float(sd),
        "rem": float(r.get("rem") or 0),
        "book_latency_ms": float(r.get("book_latency_ms") or 0),
        "twap_age_ms": float(r.get("twap_age_ms") or 0),
    }


def judge_now(r, f):
    """**现行规则**下这一行该不该成交（返回 (ok, 原因)）——链在窗口内的推进在 `replay` 里。"""
    stage = r.get("stage")
    price_ok = (f["fill"] > U.P_FLOOR) if stage == "t150" else (f["fill"] >= U.P_FLOOR)
    if not price_ok:
        return False, "price_low"
    dev = r.get("dev")
    sig = dev / r["sd"] if r.get("sd") else None
    leg = (dev >= U.DEV_USD) or (sig is not None and r["sd"] >= U.SD_MIN_USD and sig >= 1.0)
    if stage == "t150":
        if not leg:
            return False, "leg_out"
        w = f["walk_sigma"] * r["sd"]
        if w == w and w < U.WALK_MIN_USD:
            return False, "walk_low"
        return True, None
    if not leg:
        return False, "leg_out"
    if not (f["fill"] > U.FLOOR_MIN_PRICE):
        return False, "floor_low"
    return True, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="data/tail-live")
    args = ap.parse_args()
    paths = sorted(glob.glob(str(Path(args.dir) / "tail_2*.jsonl")))
    rows = []
    for p in paths:
        for line in open(p):
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get("kind") != "snap" or r.get("event_type") not in (None, "tail"):
                continue
            f = feats_of(r)
            if f is None:
                continue
            rows.append((r, f))
    print(f"[02] 读入 {len(paths)} 个文件, 快照行 {len(rows)}")

    # ── 字段语义自检: 用行内 rules 字典反查我的读法 ──
    #   `dev63`      = dev ≥ 63
    #   `sigma_usd40`= **合取** `sd ≥ 40 ∧ dev ≥ sd`（不是单独的 sd 腿——自检时踩过）
    #   `price`      = fill vs 0.80（T=150 段 09-29 起是严格大于 ⇒ 有刻意的少数不一致）
    mismatch = {"dev63": 0, "sigma_usd40": 0, "price": 0}
    for r, f in rows:
        ru = r.get("rules") or {}
        if "dev63" in ru and (f["dev_sigma"] * r["sd"] >= U.DEV_USD) != bool(ru["dev63"]):
            mismatch["dev63"] += 1
        if "sigma_usd40" in ru and (r["sd"] >= U.SD_MIN_USD and r["dev"] >= r["sd"]) \
                != bool(ru["sigma_usd40"]):
            mismatch["sigma_usd40"] += 1
        if "price" in ru and (f["fill"] >= U.P_FLOOR) != bool(ru["price"]):
            mismatch["price"] += 1
    print(f"[02] 字段语义自检（与行内 rules 字典对照, 不同版本有刻意的算子差异）: {mismatch}")

    # ── 现行规则重放（按窗推进三段的链） ──
    by_win = {}
    for r, f in rows:
        by_win.setdefault((r.get("date"), r.get("event_start"), r.get("condition_id")),
                          []).append((r, f))
    n_sig_now, n_sig_old, rec, meta = 0, 0, [], []
    for key, items in by_win.items():
        items.sort(key=lambda t: t[0].get("ts") or 0)
        decided = False
        for r, f in items:
            if r.get("ok"):
                n_sig_old += 1
            ok_now, why = judge_now(r, f)
            chain_ok = ok_now and not decided          # 该窗已出过信号 ⇒ 后面的行不再成交
            if chain_ok:
                decided = True
                n_sig_now += 1
            rec.append({
                "date": r.get("date"), "event_start": r.get("event_start"),
                "condition_id": r.get("condition_id"), "ts": r.get("ts"),
                "stage": r.get("stage"), "rem": r.get("rem"), "side": r.get("side"),
                "hot_src": r.get("hot_src"), "fill": f["fill"],
                "sd": r.get("sd"), "dev": r.get("dev"),
                "walk": f["walk_sigma"] * r["sd"], "ok_old": bool(r.get("ok")),
                "ok_now": bool(chain_ok), "why": why,
                "exec_status": r.get("exec_status"), "stake": r.get("stake"),
                "shares": r.get("shares"), "won": r.get("won"),
                "settle_src": r.get("settle_src"),
            })
            meta.append((r, f, bool(chain_ok)))
    print(f"[02] 窗口 {len(by_win)}; 信号行: 记录时 {n_sig_old} ⇒ **现行规则重放 {n_sig_now}**")

    from collections import Counter
    st = Counter(m[0].get("stage") for m in meta if m[2])
    print(f"[02] 现行规则下的段分布: {dict(st)}")
    print(f"[02] hot_src 分布（现行规则下成交的行）: "
          f"{dict(Counter(m[0].get('hot_src') for m in meta if m[2]))}")
    won_known = [m for m in meta if m[2] and m[0].get("won") is not None]
    if won_known:
        print(f"[02] 有结算的现行信号 {len(won_known)} 条, WR "
              f"{sum(m[0]['won'] for m in won_known)/len(won_known)*100:.2f}%")
    ex = Counter(m[0].get("exec_status") for m in meta if m[2])
    print(f"[02] 执行状态（现行信号; 09-26 起才有此字段）: {dict(ex)}")

    # ── 存盘 ──
    out = BASE / "data"
    out.mkdir(exist_ok=True)
    (out / "live_rows.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    days = sorted({r.get("date") for r, _ in rows if r.get("date")})
    day_ix = {d: i for i, d in enumerate(days)}
    X = np.array([[f[k] for k in CORE_TICK] for _, f, _ in meta], dtype=np.float64)
    won = np.array([np.nan if r.get("won") is None else float(r["won"]) for r, _, _ in meta])
    np.savez_compressed(
        out / "live.npz", X=X, won=won,
        day=np.array([day_ix.get(r.get("date"), -1) for r, _, _ in meta], dtype=np.int32),
        ok_now=np.array([k for _, _, k in meta], dtype=np.int8),
        ok_old=np.array([1 if r.get("ok") else 0 for r, _, _ in meta], dtype=np.int8),
        stage=np.array([STAGE_ID.get(r.get("stage"), -1) for r, _, _ in meta], dtype=np.int8),
        fill=X[:, 0], rem=X[:, 5],
        event_start=np.array([r.get("event_start") or 0 for r, _, _ in meta], dtype=np.int64),
        src_bid=np.array([1 if r.get("hot_src") == "bid" else 0 for r, _, _ in meta], dtype=np.int8),
        filled=np.array([1 if r.get("exec_status") in ("filled", "partial") else 0
                         for r, _, _ in meta], dtype=np.int8),
        core=CORE_TICK, days=days)
    print(f"[02] ✅ live.npz（{X.shape}）与 live_rows.json 已存 python/v7/data/")


if __name__ == "__main__":
    main()
