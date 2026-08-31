#!/usr/bin/env python3
"""
v3 阶段 2 —— LightGBM 按天时间序列筛选（flip 单线: WR ≥ 35% 且 EV = WR - fill > 0）。

决策时刻 A（穿越即入）: 只用穿越时刻及之前的信息; fill = flip_fill0s（对侧 ask@穿越）。
决策时刻 B（+10s 确认）: 全部特征; fill = flip_fill10s。

划分（铁律: 按天, 禁止随机 split）:
  train  = 08-18 ~ 08-28（含 08-18 半天）
  test   = 08-29 ~ 08-30（2 个完整日, 样本外）
  extra  = 08-31（半天, 仅参考）

输出: 每配置 AUC / 预测分十等份的 flip WR 与 EV / top 特征 gain。
"""

import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from featlib import DATA  # noqa: E402


def load():
    df = pd.read_pickle(BASE / "data" / "featmat.pkl")
    df = df[df["flip_fill0s"].notna()]  # fill 可得性
    return df


# 决策时刻 A: 穿越时刻及之前可得的特征
def pre_only_cols(all_cols: list[str]) -> list[str]:
    exclude_prefix = ("post_", "reprice_", "tape_")
    exclude_exact = {"post_dip", "post_end", "other_delta2s", "other_delta10s",
                     "twap_delta", "twap_gap"}
    return [c for c in all_cols
            if not c.startswith(exclude_prefix) and c not in exclude_exact]


def main():
    df = load()
    dates = sorted(df["date"].unique())
    train_dates = [d for d in dates if d <= "2026-08-28"]
    test_dates = [d for d in dates if d in ("2026-08-29", "2026-08-30")]
    extra_dates = [d for d in dates if d == "2026-08-31"]

    # 第二个划分（更长测试窗）: train 至 08-26, test 08-27~08-30
    train2_dates = [d for d in dates if d <= "2026-08-26"]
    test2_dates = [d for d in dates if d in ("2026-08-27", "2026-08-28",
                                             "2026-08-29", "2026-08-30")]

    print(f"数据: {DATA}  |  观测 {len(df)}  |  flip 基线 WR {df['flip_won'].mean()*100:.1f}%")
    print(f"train {train_dates[0]}~{train_dates[-1]} ({len(train_dates)}d) / "
          f"test {test_dates[0]}~{test_dates[-1]} ({len(test_dates)}d) / "
          f"extra {extra_dates}")
    print(f"另一划分: train ≤08-26 / test 08-27~08-30")
    print("=" * 78)

    params = dict(
        objective="binary",
        learning_rate=0.05,
        num_leaves=15,
        min_data_in_leaf=30,
        feature_fraction=0.8,
        bagging_fraction=0.8,
        bagging_freq=1,
        n_estimators=500,
        verbosity=-1,
        seed=42,
    )
    meta_cols = {"flip_won", "flip_fill0s", "flip_fill2s", "flip_fill10s", "cls",
                 "side", "rem", "event_start", "date"}
    all_feats = [c for c in df.columns if c not in meta_cols]

    for name, feats, fill_key, tr_dates, te_dates in [
        ("A 穿越即入", pre_only_cols(all_feats), "flip_fill0s", train_dates, test_dates),
        ("B +10s确认", all_feats, "flip_fill10s", train_dates, test_dates),
    ]:
        tr = df[df["date"].isin(tr_dates)]
        # 早停在 train 末 2 天
        val_dates = sorted(tr_dates)[-2:]
        va = tr[tr["date"].isin(val_dates)]
        tr = tr[~tr["date"].isin(val_dates)]
        te = df[df["date"].isin(te_dates)]
        ex = df[df["date"].isin(extra_dates)] if extra_dates else pd.DataFrame()

        Xtr, ytr = tr[feats].fillna(-999.0), tr["flip_won"]
        Xva, yva = va[feats].fillna(-999.0), va["flip_won"]
        Xte, yte = te[feats].fillna(-999.0), te["flip_won"]

        p_refit = dict(params)
        p_refit.pop("n_estimators")
        m = lgb.LGBMClassifier(**params)
        m.fit(Xtr, ytr, eval_X=Xva, eval_y=yva,
              callbacks=[lgb.early_stopping(50, verbose=False)])
        n_tree = m.best_iteration_
        m = lgb.LGBMClassifier(**p_refit, n_estimators=n_tree)
        m.fit(pd.concat([Xtr, Xva]), pd.concat([ytr, yva]))

        def report(X, y, fill, tag):
            if len(y) == 0:
                print(f"\n  [{tag}] 无数据")
                return
            p = m.predict_proba(X)[:, 1]
            auc = _auc(y, p)
            print(f"\n  [{tag}] n={len(y)}  基线 WR {y.mean()*100:.1f}%  AUC {auc:.3f}  "
                  f"(tree={n_tree})")
            print(f"  {'预测分十等份':<14s} {'n':>4s} {'flipWR':>7s} {'fill':>6s} {'EV/股':>7s}")
            order = np.argsort(p)
            idx = np.array_split(order, 10)
            for k, sl in enumerate(idx):
                yy, pp, ff = y.iloc[sl], p[sl], fill.iloc[sl]
                wr = yy.mean()
                f_ = ff.mean()
                ev = wr - f_
                flag = " ★" if (wr >= 0.35 and ev > 0 and len(sl) >= 80) else ""
                print(f"  D{k+1:<3d} (p<{pp.max():.3f}){'':>4s} {len(sl):>4d} "
                      f"{wr*100:>6.1f}% {f_:>6.3f} {ev:>+7.4f}{flag}")
            # 保存预测
            pd.DataFrame({"p": p, "y": y.values, "fill": fill.values}).to_pickle(
                BASE / f"data/pred_{tag}.pkl")

        print(f"\n{'='*78}\n配置 {name} | 特征 {len(feats)} 个\n{'='*78}")
        report(Xte, yte, te[fill_key], f"{name}_test")
        if len(ex):
            report(ex[feats].fillna(-999.0), ex["flip_won"], ex[fill_key], f"{name}_extra")

        # top 特征（gain）
        imp = pd.Series(m.feature_importances_, index=feats).sort_values(ascending=False)
        print("\n  Top 15 特征 (gain):")
        for c, v in imp.head(15).items():
            print(f"    {c:<24s} {v:>6.0f}")

        # 长测试窗: 重训并在 4 天测试窗评估（只报整体 WR 与 top 分位）
        tr2 = df[df["date"].isin(train2_dates)]
        va2 = tr2[tr2["date"].isin(sorted(train2_dates)[-2:])]
        tr2 = tr2[~tr2["date"].isin(sorted(train2_dates)[-2:])]
        te2 = df[df["date"].isin(test2_dates)]
        p2_refit = dict(params)
        p2_refit.pop("n_estimators")
        m2 = lgb.LGBMClassifier(**params)
        m2.fit(tr2[feats].fillna(-999.0), tr2["flip_won"],
               eval_X=va2[feats].fillna(-999.0), eval_y=va2["flip_won"],
               callbacks=[lgb.early_stopping(50, verbose=False)])
        nb = m2.best_iteration_
        m2 = lgb.LGBMClassifier(**p2_refit, n_estimators=nb)
        m2.fit(pd.concat([tr2, va2])[feats].fillna(-999.0),
               pd.concat([tr2, va2])["flip_won"])
        p2 = m2.predict_proba(te2[feats].fillna(-999.0))[:, 1]
        y2, f2 = te2["flip_won"], te2[fill_key]
        auc2 = _auc(y2, p2)
        print(f"\n  [长测试窗 08-27~08-30] n={len(y2)} AUC {auc2:.3f} "
              f"整体 WR {y2.mean()*100:.1f}%")
        order = np.argsort(p2)
        for sl in np.array_split(order, 10):
            yy, ff = y2.iloc[sl], f2.iloc[sl]
            wr, ev = yy.mean(), yy.mean() - ff.mean()
            flag = " ★" if (wr >= 0.35 and ev > 0 and len(sl) >= 80) else ""
            print(f"    D({len(sl):>4d}) WR {wr*100:>6.1f}% fill {ff.mean():.3f} "
                  f"EV {ev:>+7.4f}{flag}")


def _auc(y, p) -> float:
    """AUC 手写（无 sklearn）。"""
    y = np.asarray(y)
    p = np.asarray(p)
    pos = y == 1
    npos, nneg = pos.sum(), (~pos).sum()
    if npos == 0 or nneg == 0:
        return float("nan")
    ranks = np.argsort(np.argsort(p)) + 1
    return float((ranks[pos].sum() - npos * (npos + 1) / 2) / (npos * nneg))


if __name__ == "__main__":
    main()
