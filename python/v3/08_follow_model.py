#!/usr/bin/env python3
"""
v3 follow 线 —— LightGBM 按天时间序列筛选（镜像 02_model 的 flip 方法）。

目标: 找到 follow（买穿越侧）中 WR - fill > 0 的可预测子集。
市场有效假设: 模型预测的赢率 ≈ fill（市场定价），EV≈0。
若模型在测试窗分桶中出现 WR > fill（EV>0）且稳定, 说明存在市场未定价的结构。

决策时刻 A（穿越即入）: 只用穿越时刻及之前的信息; fill = follow_fill0s（触发侧 ask@穿越）。
决策时刻 B（+10s 确认）: 全部特征; fill = follow_fill10s。

划分（铁律: 按天, 禁止随机 split, 与 flip 完全同口径）:
  train  = 08-18 ~ 08-28（含 08-18 半天）
  test   = 08-29 ~ 08-30（2 个完整日, 样本外）
  extra  = 08-31（半天, 仅参考）
  长测试窗: train ≤08-26 / test 08-27~08-30
"""

import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent


def load():
    df = pd.read_pickle(BASE / "data" / "featmat.pkl")
    df = df[df["follow_fill0s"].notna()]
    return df


def pre_only_cols(all_cols: list[str]) -> list[str]:
    exclude_prefix = ("post_", "reprice_", "tape_")
    exclude_exact = {"post_dip", "post_end", "other_delta2s", "other_delta10s",
                     "twap_delta", "twap_gap"}
    return [c for c in all_cols
            if not c.startswith(exclude_prefix) and c not in exclude_exact]


def _auc(y, p) -> float:
    y = np.asarray(y)
    p = np.asarray(p)
    pos = y == 1
    npos, nneg = pos.sum(), (~pos).sum()
    if npos == 0 or nneg == 0:
        return float("nan")
    ranks = np.argsort(np.argsort(p)) + 1
    return float((ranks[pos].sum() - npos * (npos + 1) / 2) / (npos * nneg))


def main():
    df = load()
    dates = sorted(df["date"].unique())
    train_dates = [d for d in dates if d <= "2026-08-28"]
    test_dates = [d for d in dates if d in ("2026-08-29", "2026-08-30")]
    extra_dates = [d for d in dates if d == "2026-08-31"]
    train2_dates = [d for d in dates if d <= "2026-08-26"]
    test2_dates = [d for d in dates if d in ("2026-08-27", "2026-08-28",
                                             "2026-08-29", "2026-08-30")]

    print(f"follow 观测 {len(df)}  |  基线 WR {df['follow_won'].mean()*100:.1f}%  "
          f"fill10s {df['follow_fill10s'].mean():.3f}  "
          f"EV {df['follow_won'].mean()-df['follow_fill10s'].mean():+.4f}/股")
    print(f"train {train_dates[0]}~{train_dates[-1]} / test {test_dates[0]}~{test_dates[-1]} / extra")
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
    meta_cols = {"flip_won", "flip_fill0s", "flip_fill2s", "flip_fill10s",
                 "follow_won", "follow_fill0s", "follow_fill2s", "follow_fill10s",
                 "cls", "side", "rem", "event_start", "date"}
    all_feats = [c for c in df.columns if c not in meta_cols]

    for name, feats, fill_key, tr_dates, te_dates in [
        ("A 穿越即入", pre_only_cols(all_feats), "follow_fill0s", train_dates, test_dates),
        ("B +10s确认", all_feats, "follow_fill10s", train_dates, test_dates),
    ]:
        tr = df[df["date"].isin(tr_dates)]
        val_dates = sorted(tr_dates)[-2:]
        va = tr[tr["date"].isin(val_dates)]
        tr = tr[~tr["date"].isin(val_dates)]
        te = df[df["date"].isin(te_dates)]
        ex = df[df["date"].isin(extra_dates)] if extra_dates else pd.DataFrame()

        Xtr, ytr = tr[feats].fillna(-999.0), tr["follow_won"]
        Xva, yva = va[feats].fillna(-999.0), va["follow_won"]
        Xte, yte = te[feats].fillna(-999.0), te["follow_won"]

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
            print(f"  {'预测分十等份':<14s} {'n':>4s} {'followWR':>8s} {'fill':>6s} {'EV/股':>8s}")
            order = np.argsort(p)
            for k, sl in enumerate(np.array_split(order, 10)):
                yy, pp, ff = y.iloc[sl], p[sl], fill.iloc[sl]
                wr, f_, ev = yy.mean(), ff.mean(), yy.mean() - ff.mean()
                flag = " ★" if (ev > 0 and len(sl) >= 60) else ""
                print(f"  D{k+1:<3d} (p<{pp.max():.3f}){'':>4s} {len(sl):>4d} "
                      f"{wr*100:>7.1f}% {f_:>6.3f} {ev:>+8.4f}{flag}")
            pd.DataFrame({"p": p, "y": y.values, "fill": fill.values}).to_pickle(
                BASE / f"data/follow_pred_{tag}.pkl")

        print(f"\n{'='*78}\nfollow 配置 {name} | 特征 {len(feats)} 个\n{'='*78}")
        report(Xte, yte, te[fill_key], f"{name}_test")
        if len(ex):
            report(ex[feats].fillna(-999.0), ex["follow_won"], ex[fill_key], f"{name}_extra")

        imp = pd.Series(m.feature_importances_, index=feats).sort_values(ascending=False)
        print("\n  Top 15 特征 (gain):")
        for c, v in imp.head(15).items():
            print(f"    {c:<24s} {v:>6.0f}")

        # 长测试窗
        tr2 = df[df["date"].isin(train2_dates)]
        va2 = tr2[tr2["date"].isin(sorted(train2_dates)[-2:])]
        tr2 = tr2[~tr2["date"].isin(sorted(train2_dates)[-2:])]
        te2 = df[df["date"].isin(test2_dates)]
        p2_refit = dict(params)
        p2_refit.pop("n_estimators")
        m2 = lgb.LGBMClassifier(**params)
        m2.fit(tr2[feats].fillna(-999.0), tr2["follow_won"],
               eval_X=va2[feats].fillna(-999.0), eval_y=va2["follow_won"],
               callbacks=[lgb.early_stopping(50, verbose=False)])
        nb = m2.best_iteration_
        m2 = lgb.LGBMClassifier(**p2_refit, n_estimators=nb)
        m2.fit(pd.concat([tr2, va2])[feats].fillna(-999.0),
               pd.concat([tr2, va2])["follow_won"])
        p2 = m2.predict_proba(te2[feats].fillna(-999.0))[:, 1]
        y2, f2 = te2["follow_won"], te2[fill_key]
        print(f"\n  [长测试窗 08-27~08-30] n={len(y2)} AUC {_auc(y2, p2):.3f} "
              f"整体 WR {y2.mean()*100:.1f}%")
        for sl in np.array_split(np.argsort(p2), 10):
            yy, ff = y2.iloc[sl], f2.iloc[sl]
            wr, ev = yy.mean(), yy.mean() - ff.mean()
            flag = " ★" if (ev > 0 and len(sl) >= 60) else ""
            print(f"    D({len(sl):>4d}) WR {wr*100:>6.1f}% fill {ff.mean():.3f} "
                  f"EV {ev:>+8.4f}{flag}")


if __name__ == "__main__":
    main()
