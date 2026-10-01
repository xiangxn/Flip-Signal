#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v7 决策模拟器：给「候选 tick 上的触发判定」一个通用外壳, 输出整窗一单的 P&L 行。

两条实验线共用它:
  * Track A（阈值动态化）= 换个 `Rules` 再跑同一条链;
  * Track B（GBM 直接下单）= decide 换成「首个 `p̂ − fill > δ` 的 tick」。

**§0 自检**: 用现行规则当触发函数跑, 必须复现 oracle 的 +70.314372U / 2074 单
（这条走的是 **numpy 行** 路径, 与 `lib_universe.replay` 的 **元组** 路径是两套代码
——两边同时对上, 说明「判定 → 成交 → P&L」这一段没有实现差异）。
"""
import json
import sys
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import lib_universe as U          # noqa: E402


class Data:
    """`01_build_dataset.py` 产物的只读访问层（判定行 + 特征 + 窗口元数据）。"""

    def __init__(self, d=None):
        d = Path(d or (BASE / "data"))
        z = np.load(d / "ticks.npz")
        self.A, self.off, self.X = z["A"], z["off"], z["X"]
        self.w_day, self.w_outcome = z["w_day"], z["w_outcome"]
        self.w_anchor, self.w_sd = z["w_anchor"], z["w_sd"]
        self.win = json.loads((d / "windows.json").read_text(encoding="utf-8"))
        self.meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        self.days = self.meta["days"]
        self.cols = list(self.meta["tick_full"])
        # ── B2（06 号）: 整窗网格（rem ≤ 300）+ Up 朝向特征; 窗口集合/顺序与窄网格一致 ──
        wz = d / "ticks_wide.npz"
        if wz.exists():
            z2 = np.load(wz)
            self.Aw, self.off_w, self.Xw = z2["A"], z2["off"], z2["X"]
            self.cols_w = list(self.meta["up_tick"])
        else:
            self.Aw = self.off_w = self.Xw = None
            self.cols_w = []
        self.sig = np.load(d / "signals.npz")
        self.sig_meta = self.meta
        self._pos = {}

    def __len__(self):
        return len(self.win)

    def slice(self, i):
        return slice(int(self.off[i]), int(self.off[i + 1]))

    def window(self, i):
        """一个窗口的完整上下文（decide 的输入）。"""
        w = dict(self.win[i])
        w["i"] = i
        sl = self.slice(i)
        w["A"] = self.A[sl]                     # 判定行（np 视图, rem ≤ 150）
        w["X"] = self.X[sl]                     # 同序特征矩阵
        w["rows"] = [self.A[j] for j in range(sl.start, sl.stop)]
        if i not in self._pos:
            self._pos[i] = {int(self.A[j][0]): j - sl.start
                            for j in range(sl.start, sl.stop)}
        w["gi2pos"] = self._pos[i]
        return w

    def col(self, name):
        return self.X[:, self.cols.index(name)]

    # ── 整窗网格（B2/06 号; 未建库时 `.Aw is None`） ──

    def wide_slice(self, i):
        return slice(int(self.off_w[i]), int(self.off_w[i + 1]))

    def window_wide(self, i):
        """一个窗口的整窗上下文: `Aw`(网格, rem ≤ 300) / `Xw`(Up 朝向特征) / 窗口元数据。"""
        w = dict(self.win[i])
        w["i"] = i
        sl = self.wide_slice(i)
        w["Aw"] = self.Aw[sl]
        w["Xw"] = self.Xw[sl]
        return w


def chain_decide(rules=None):
    """现行三段链的 decide（Track A 换 `rules` 即换阈值）。"""
    rules = rules or U.Rules()

    def decide(w):
        rs, _ = U.chain(w["rows"], w["sd"], w["date"], w["outcome"], rules, w)
        for r in rs:
            if r.get("ok"):
                return r
        return None
    return decide


def simulate(data, decide, stake=U.STAKE, idx=None, with_reject=False):
    """逐窗跑 decide → 每窗至多一单的 P&L 行。

    返回 [{date, day, event_start, condition_id, stage, rem, side, fill, won, pnl, gi, ...}]
    `with_reject=True` 时把 decide 返回的 `(row, reject_dict)` 二元组也收下来（供诊断）。
    """
    out = []
    for i in (range(len(data)) if idx is None else idx):
        w = data.window(i)
        if w["outcome"] is None:
            continue
        r = decide(w)
        if with_reject:
            r, info = r
        if r is None:
            continue
        fill = float(r["fill"])
        won = 1 if ((w["outcome"] == 0) if r["side"] == "yes" else (w["outcome"] == 1)) else 0
        row = {"date": w["date"], "day": w["day"], "event_start": w["start_time"],
               "condition_id": w["condition_id"], "stage": r.get("stage"),
               "rem": float(r["rem"]), "side": r["side"], "fill": fill, "won": won,
               "pnl": (stake / fill - stake) if won else -stake,
               "gi": int(r["gi"]), "sd": w["sd"], "dev": float(r.get("dev", 0.0)),
               "walk": r.get("walk"), "win_i": i}
        if with_reject:
            row["info"] = info
        out.append(row)
    return out


def summarize(rows, stake=U.STAKE):
    n = len(rows)
    pnl = sum(r["pnl"] for r in rows)
    wr = (sum(r["won"] for r in rows) / n * 100) if n else 0.0
    return {"n": n, "wr": wr, "pnl": pnl}


def main():
    """§0: 现行规则当触发函数 → 必须复现 oracle 的 pin。"""
    data = Data()
    rows = simulate(data, chain_decide(U.Rules()))
    s = summarize(rows)
    ok = (s["n"] == U.PIN["signals"] and abs(s["pnl"] - U.PIN["pnl"]) < 1e-6
          and abs(s["wr"] - U.PIN["wr"]) < 1e-6)
    print(f"  §0 模拟器 " + ("✅" if ok else "❌") +
          f"  n={s['n']} WR={s['wr']:.6f}% P&L={s['pnl']:+.6f}U "
          f"（oracle {U.PIN['signals']} / {U.PIN['wr']:.6f}% / {U.PIN['pnl']:+.6f}U）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
