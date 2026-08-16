# legacy/ — 旧数据分析代码归档

> 自 2026-08-16 新采集程序（`cmd/collect`，数据格式 v2，1s 分辨率）启用后，
> 本目录下的脚本不再使用，仅作历史参考。它们全部面向旧 5s 快照格式
> （`data/btc_5s` / `data_0`），**不能读 v2 事件**。
> 当前活跃代码在 `python/flip/`（翻转线）与 `python/follow/`（follow 线）。

## 时代脉络（按文件名）

### 第一阶段：Binance 结算口径的翻转策略（2026-08-05 ~ 08-13，Formula B 时代）

| 文件 | 用途 |
|------|------|
| `backtest_flip_config.py` | Formula B 回测参数（2026-08-13 标定，Binance 口径） |
| `backtest_flip_utils.py` | 回测工具：事件加载/特征/评分/穿越检测（ask 口径成交） |
| `backtest_flip_scoring.py` | Formula B 回测入口 |
| `backtest_btc_position.py` | BTC 位置因子回测 |
| `analyze_flip_base_rate.py` | 0.7 穿越翻转基率（Binance 结算口径版） |
| `analyze_flip_comprehensive.py` / `_deep.py` / `_final.py` / `_strategy.py` | 翻转特征挖掘（多轮迭代） |
| `analyze_features.py` | 特征筛选 |
| `analyze_confirm_delay.py` | 确认延迟参数分析 |
| `analyze_price_gate.py` | 入场价 gate 分析（幽灵成交教训，gate=0.45） |
| `analyze_filter_pipeline.py` | 过滤链分析 |
| `analyze_od_killed.py` / `analyze_pending_bug.py` / `analyze_remsec.py` | 单项排查脚本 |
| `optimize_winrate.py` / `compare_configs.py` / `sweep_*.py` | 参数扫描/配置对比 |

### 第二阶段：TWAP 结算口径重推导（2026-08-14 ~ 08-16）

| 文件 | 用途 |
|------|------|
| `analyze_twap_calibration.py` | TWAP vs Binance 口径四组合对照（WR 43%→17% 崩塌证据） |
| `analyze_twap_flip_derivation.py` | 五段式重推导：基率/特征筛选/组合/split/镜像（only/both 类别结构来源） |
| `analyze_twap_influence.py` | Binance→TWAP 影响函数（基差 -68$、机械拖拽、均值回归） |
| `analyze_twap_pricing.py` | TWAP 定价分析 |
| `fetch_market_outcomes.py` | 按 slug 抓 PM 真实结算补 data_0（产出 data_0/lab_resolved/） |
| `twap_reanalysis_2026-08-15/` | 1719 事件真实结算重挖（flow_5s 反向流研究） |

## 运行注意

- 旧脚本默认数据路径 `../data/btc` 与 `../data_0/lab_resolved` 按 CWD 解析，
  从 `python/` 目录运行即可
- 依赖 scipy（`venv/bin/python`）
