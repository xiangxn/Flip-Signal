# TWAP 真实结算下翻转特征重新挖掘（2026-08-15）

`data_0/lab_resolved`（1719 事件，TWAP 真实结算 `market_outcome`）的
从零特征挖掘。详细结论见 [report.md](report.md)。

## 运行

```bash
cd python/twap_reanalysis_2026-08-15
../venv/bin/python 01_baseline.py       # 基率与 EV 结构
../venv/bin/python 02_feature_screen.py # 单特征筛选（χ² + EV + 逐日）
../venv/bin/python 03_filter_search.py  # train/test 阈值搜索 + 组合
../venv/bin/python 04_verify.py         # flow_5s 过滤器深度验证
../venv/bin/python 05_refine.py         # 精化条件逐日对比
../venv/bin/python 06_final.py          # 最终候选对比与规格
../venv/bin/python 07_split_check.py    # train/test 冻结检验
```

## 文件

| 文件 | 内容 |
|---|---|
| `lib.py` | 数据加载、因果穿越收集、特征提取纯函数、统计工具 |
| `01_baseline.py` | 翻转基率 24.3%（事件级）、EV 结构、YES/NO 不对称、按天稳定性 |
| `02_feature_screen.py` | 15 个特征的四分位/符号分桶 + χ² + 专项交互（机械拖拽等） |
| `03_filter_search.py` | 冻结阈值搜索（train=前6天 / test=后2天）+ 贪心组合 |
| `04_verify.py` | 幸存过滤器验证：阈值平台、聚类、bootstrap、二项检验 |
| `05_refine.py` | 附加条件逐日对比（剩余时间/触发价/小时/穿越次序） |
| `06_final.py` | 最终候选对比、PF、阈值扰动稳健性 |
| `07_split_check.py` | 最终候选的 train/test 冻结检验 |
| `report.md` | 完整结论文档 |

## 核心结论（详见 report.md）

- 翻转基率 24.3%，基线 EV ≈ 0（市场把翻转概率定价进了 fill）
- **正 EV 特征：穿越时刻 Binance 主动流反向（flow_5s ≥ 0.1）**，
  EV +0.08~+0.15/股，7/8 天正 P&L，train/test 两集同号
- 推荐变体 E：`flow_5s ≥ 0.1 且 非 12-18 UTC`（EV +0.122，PF 1.89）
- 证伪：价格位置背离（旧 B1）、振幅扩张（旧 B2）、动量类、
  TWAP 机械拖拽假设
- 铁律：特征只用 ≤ 穿越 tick 数据，历史振幅只回看已完成窗口，
  结算只用 market_outcome
