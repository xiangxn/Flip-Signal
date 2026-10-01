#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v7-01: 建 GBM 专用数据集（信号级 + tick 级）, 带 §0 pin 自检。

产物（默认 `python/v7/data/`）:
  * `signals.npz`     —— **信号行**（现行规则链 ok 行, 14 天 2074 条）: FULL 特征 + 原始量 + 元数据
  * `ticks.npz`       —— **窄 tick 网格**（可判定 tick, rem ≤ 150）: 原始 18 列 + FULL−stage 特征
  * `ticks_wide.npz`  —— **整窗网格**（rem ≤ 300, B2/06 号用）: 同一批窗口 + **Up 朝向**特征
  * `windows.json`    —— 逐窗元数据（start_time / condition_id / 日期 / 锚 / σ / 成交量基线）
  * `meta.json`       —— 列名、日期索引、NaN 覆盖率、pin 结果

用法:
  python/venv/bin/python python/v7/01_build_dataset.py --dirs data/btc
  python/venv/bin/python python/v7/01_build_dataset.py --dirs data/btc data/events   # 新采集并入
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import lib_features as F          # noqa: E402
import lib_universe as U          # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dirs", nargs="+", default=["data/btc"],
                    help="事件目录（可多个; 新采集的数据直接并进来）")
    ap.add_argument("--out", default=str(BASE / "data"))
    ap.add_argument("--no-pin", action="store_true", help="跳过 §0 pin（不推荐）")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    paths = U.paths_of(args.dirs)
    print(f"[01] 目录 {args.dirs} → {len(paths)} 个文件")

    print("[01] 第一遍: 窗口级标量（σ 与成交量基线）…")
    ctx = U.hist_context(U.scan_scalars(paths))

    # ── §0 pin: 现行规则链必须逐位复现 oracle ──
    if not args.no_pin:
        print("[01] §0 现行规则链 pin（oracle = python/v4/23_tail_integrated.py）…")
        got = U.replay(paths, ctx)
        if not U.check_pin(got):
            print("[01] ❌ pin 不过 —— 宇宙层与 oracle 分家了, 停止建库。")
            sys.exit(1)

    print("[01] 第二遍: 逐窗抽网格 + 算特征…")
    A_list, X_list, off, wrows = [], [], [0], []
    Aw_list, Xw_list, offw = [], [], [0]          # B2: 整窗网格（rem ≤ 300, Up 朝向特征）
    sig_feats, sig_meta = [], []
    nan_cnt, nan_cnt_w = {}, {}
    n_win = n_skip_sigma = n_skip_empty = 0
    days = []

    def day_idx(d):
        if d not in days:
            days.append(d)
        return days.index(d)

    for w in U.iter_windows(paths, ctx):
        if w["outcome"] is None or not w["anchor"]:
            continue
        if w["sd"] is None:
            n_skip_sigma += 1
            continue
        if not w["rows"]:
            n_skip_empty += 1
            continue
        n_win += 1
        # ⚠️ 两套行: **特征看整窗**（Aw, rem ≤ 300）而**判定只看 rem ≤ 150 的子序列**
        # （sel）——判定 tick 的动量/量比/回吐才有历史可看（见 lib_universe.grid_of 注释）。
        Aw = np.array(w["rows"], dtype=np.float64)
        sel = np.nonzero(Aw[:, F.C_REM] <= U.T150)[0]
        if len(sel) == 0:
            n_skip_empty += 1
            n_win -= 1
            continue
        A = Aw[sel]
        feats = F.window_features(Aw, w["anchor"], w["sd"], w["vol_base"], w["sd_base"])
        di = day_idx(w["date"])

        # 信号行（现行规则; 与 §0 pin 同一函数）
        rs, _ = U.chain([tuple(x) for x in A], w["sd"], w["date"], w["outcome"], U.Rules(), w)
        for r in rs:
            if not r.get("ok"):
                continue
            gi = r["gi"]                     # = 宽网格行号（Aw 与特征数组同序）
            # `stage` 是窗口级量（不属于逐 tick 数组）, 单独填
            sig_feats.append([float(F.STAGE_ID[r["stage"]]) if k == "stage"
                              else feats[k][gi] for k in F.FULL])
            sig_meta.append({
                "date": r["date"], "day": di, "event_start": r["event_start"],
                "condition_id": r["condition_id"], "outcome": r["outcome"],
                "stage": F.STAGE_ID[r["stage"]], "rem": r["rem"], "side": r["side"],
                "fill": r["fill"], "dev": r["dev"], "walk": r["walk"],
                "basis": (r["dev"] - r["walk"]) if r["walk"] is not None else None,
                "sd": r["sd"], "sig": r["sig"], "gi": gi,
                "won": r["settle_won"], "pnl": U.pnl_of(r), "n_rows": len(A),
            })

        A_list.append(A)
        # 特征矩阵同样**取宽网格那一次计算**再按 sel 切片（否则动量腿又丢了前段历史）
        X_list.append(np.column_stack([feats[k][sel] for k in F.TICK_FULL]))
        off.append(off[-1] + len(A))
        # ── B2: 同一窗的**整窗**网格 + Up 朝向特征（B1 的窄网格一行不动）──
        feats_up = F.window_features(Aw, w["anchor"], w["sd"], w["vol_base"], w["sd_base"],
                                     orient="up")
        Aw_list.append(Aw)
        Xw_list.append(np.column_stack([feats_up[k] for k in F.UP_TICK]))
        offw.append(offw[-1] + len(Aw))
        for k in F.UP_TICK:
            nan_cnt_w[k] = nan_cnt_w.get(k, 0) + int(np.isnan(feats_up[k]).sum())
        wrows.append({"start_time": w["start_time"], "condition_id": w["condition_id"],
                      "date": w["date"], "day": di, "outcome": w["outcome"],
                      "anchor": w["anchor"], "sd": w["sd"], "vol_base": w["vol_base"],
                      "sd_base": w["sd_base"],
                      "vol_rate": w["vol_rate"], "n_rows": len(A), "n_rows_wide": len(Aw)})
        for k in F.TICK_FULL:
            v = feats[k][sel]
            nan_cnt[k] = nan_cnt.get(k, 0) + int(np.isnan(v).sum())

    A_all = np.vstack(A_list) if A_list else np.zeros((0, len(U.GRID_COLS)))
    off = np.array(off, dtype=np.int64)
    N = len(A_all)
    Aw_all = np.vstack(Aw_list) if Aw_list else np.zeros((0, len(U.GRID_COLS)))
    offw = np.array(offw, dtype=np.int64)
    Nw = len(Aw_all)
    print(f"[01] 窗 {n_win}（跳 σ 未就绪 {n_skip_sigma} / 空网格 {n_skip_empty}）, "
          f"窄网格行 {N}（rem ≤ 150）, 整窗行 {Nw}（rem ≤ 300）, 信号 {len(sig_meta)}")

    Xf = (np.vstack(X_list).astype(np.float32) if X_list
          else np.zeros((0, len(F.TICK_FULL)), dtype=np.float32))

    sig_arr = np.array(sig_feats, dtype=np.float64) if sig_feats else np.zeros((0, len(F.FULL)))
    won = np.array([m["won"] for m in sig_meta], dtype=np.int8)
    pnl = np.array([m["pnl"] for m in sig_meta], dtype=np.float64)

    np.savez_compressed(out / "signals.npz", X=sig_arr, won=won, pnl=pnl,
                        day=np.array([m["day"] for m in sig_meta], dtype=np.int32),
                        stage=np.array([m["stage"] for m in sig_meta], dtype=np.int8),
                        rem=np.array([m["rem"] for m in sig_meta], dtype=np.float64),
                        fill=np.array([m["fill"] for m in sig_meta], dtype=np.float64),
                        dev=np.array([m["dev"] for m in sig_meta], dtype=np.float64),
                        walk=np.array([m["walk"] if m["walk"] is not None else np.nan
                                       for m in sig_meta], dtype=np.float64),
                        sd=np.array([m["sd"] for m in sig_meta], dtype=np.float64),
                        gi=np.array([m["gi"] for m in sig_meta], dtype=np.int32),
                        win=np.array([m["event_start"] for m in sig_meta], dtype=np.int64))
    np.savez_compressed(out / "ticks.npz", A=A_all, off=off, X=Xf,
                        w_day=np.array([w["day"] for w in wrows], dtype=np.int32),
                        w_outcome=np.array([w["outcome"] for w in wrows], dtype=np.int8),
                        w_anchor=np.array([w["anchor"] for w in wrows], dtype=np.float64),
                        w_sd=np.array([w["sd"] for w in wrows], dtype=np.float64),
                        w_start=np.array([w["start_time"] for w in wrows], dtype=np.int64))
    (out / "windows.json").write_text(json.dumps(wrows), encoding="utf-8")

    # ── B2 产物: 整窗网格 + Up 朝向特征（窗口集合/顺序与 ticks.npz **完全一致**）──
    Xw = (np.vstack(Xw_list).astype(np.float32) if Xw_list
          else np.zeros((0, len(F.UP_TICK)), dtype=np.float32))
    np.savez_compressed(out / "ticks_wide.npz", A=Aw_all, off=offw, X=Xw,
                        w_day=np.array([w["day"] for w in wrows], dtype=np.int32),
                        w_outcome=np.array([w["outcome"] for w in wrows], dtype=np.int8),
                        w_anchor=np.array([w["anchor"] for w in wrows], dtype=np.float64),
                        w_sd=np.array([w["sd"] for w in wrows], dtype=np.float64),
                        w_start=np.array([w["start_time"] for w in wrows], dtype=np.int64))

    nan_frac = {k: (nan_cnt.get(k, 0) / N if N else 0.0) for k in F.TICK_FULL}
    nan_frac_w = {k: (nan_cnt_w.get(k, 0) / Nw if Nw else 0.0) for k in F.UP_TICK}
    meta = {"dirs": args.dirs, "n_windows": n_win, "n_ticks": N, "n_signals": len(sig_meta),
            "days": days, "grid_cols": list(U.GRID_COLS), "full": F.FULL, "core": F.CORE,
            "tick_full": F.TICK_FULL, "nan_frac_tick": nan_frac,
            "n_ticks_wide": Nw, "up_tick": F.UP_TICK, "nan_frac_wide": nan_frac_w,
            "pin": {k: v for k, v in U.PIN.items() if k != "stages"},
            "sig_pnl": float(pnl.sum()), "sig_wr": float(won.mean() * 100) if len(won) else 0.0}
    (out / "meta.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False),
                                   encoding="utf-8")

    # ── 自检 ──
    print(f"[01] 信号 {len(sig_meta)} 条  WR {meta['sig_wr']:.6f}%  P&L {meta['sig_pnl']:+.6f}U")
    if abs(meta["sig_pnl"] - U.PIN["pnl"]) > 1e-6 or len(sig_meta) != U.PIN["signals"]:
        print("[01] ❌ 信号级数据集与 pin 不符（矩阵列对齐可能错位）")
        sys.exit(1)
    # ── 追加列自检（B2）: 窄网格 = 整窗网格的 rem ≤ 150 子序列 + fill 与两侧 ask 的恒等式 ──
    bad_sub = bad_fill = 0
    for i in range(len(off) - 1):
        Aw_ = Aw_all[offw[i]:offw[i + 1]]
        sel_ = np.nonzero(Aw_[:, F.C_REM] <= U.T150)[0]
        # ⚠️ `equal_nan=True` 必需: twap/walk/basis 列有 NaN（NaN != NaN 会把相等判成不等）
        if not np.array_equal(A_all[off[i]:off[i + 1]], Aw_[sel_], equal_nan=True):
            bad_sub += 1
    f_up = Aw_all[:, F.C_SGN] > 0
    if not np.array_equal(Aw_all[:, F.C_FILL],
                          np.where(f_up, Aw_all[:, F.C_YESA], Aw_all[:, F.C_NOA])):
        bad_fill = int((Aw_all[:, F.C_FILL] !=
                        np.where(f_up, Aw_all[:, F.C_YESA], Aw_all[:, F.C_NOA])).sum())
    print(f"[01] 追加列自检: 窄网格=整窗子序列 {'✅' if bad_sub == 0 else f'❌ {bad_sub} 窗不符'}; "
          f"fill≡ask(热门侧) {'✅' if bad_fill == 0 else f'❌ {bad_fill} 行不符'}")
    if bad_sub or bad_fill:
        sys.exit(1)
    print("[01] tick 级特征 NaN 覆盖率（分子 = 该特征为 NaN 的行数）:")
    for k in F.TICK_FULL:
        print(f"       {k:<20} {nan_frac[k]*100:6.2f}%")
    print("[01] ✅ 产物:", ", ".join(sorted(p.name for p in out.glob("*"))))


if __name__ == "__main__":
    main()
