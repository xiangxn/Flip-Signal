# Flip Signal — Python 回测与分析

PM BTC 5分钟市场 **Flip Signal Detection** 策略的 Python 回测与分析工具。

对应 Go 引擎: `internal/flip/`，特征体系: `docs/flip_backtest_plan.md`

---

## 环境

```bash
cd python/
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

依赖：numpy, scipy, pandas（数据分析标准三件套）

---

## 快速开始

```bash
# 回测（核心入口）
python backtest_flip_scoring.py --data ../data/lab/

# 回测 + 导出信号 JSONL
python backtest_flip_scoring.py --data ../data/lab/ --export signals.jsonl

# 回测 + 打印每笔信号详情
python backtest_flip_scoring.py --data ../data/lab/ --verbose
```

---

## 目录结构

```
python/
├── README.md
├── requirements.txt
│
├── backtest_flip_config.py      # [配置] Formula A 参数定义 + 默认值
├── backtest_flip_utils.py       # [核心] 特征提取 + 信号检测 + 回测引擎
├── backtest_flip_scoring.py     # [入口] 回测主程序（CLI）
│
├── sweep_other_delta.py         # [调参] other_delta 阈值网格搜索
├── analyze_features.py          # [分析] 7特征预测能力全面分析
├── analyze_filter_pipeline.py   # [分析] 过滤管线逐级杀灭率分析
├── analyze_od_killed.py         # [分析] other_delta 硬过滤影响
├── analyze_remsec.py            # [分析] remaining_sec 对胜率影响
│
├── analyze_flip_comprehensive.py # [研究] 穿越 0.7 事件综合分析
├── analyze_flip_deep.py          # [研究] BTC 穿越后行为深度分析
├── analyze_flip_strategy.py      # [研究] wait-and-see 策略设计
└── analyze_flip_final.py         # [研究] 入场价 + BTC 跑道优化
```

## 模块说明

### 回测核心（3 文件）

| 文件 | 职责 |
|------|------|
| `backtest_flip_config.py` | `FlipBacktestConfig` dataclass，与 Go 端 `FlipConfig` 一一对应 |
| `backtest_flip_utils.py` | 纯函数：特征提取（PathEff, NoiseRatio, CountFlips…）、信号检测、回测循环 |
| `backtest_flip_scoring.py` | CLI 入口，加载数据 → 运行回测 → 打印摘要 / 导出 JSONL |

### 调参工具

| 文件 | 用途 |
|------|------|
| `sweep_other_delta.py` | 扫描 other_delta 硬过滤阈值，找到最优设置 |
| `analyze_features.py` | 分析每个特征的独立预测能力，找最优阈值 |
| `analyze_filter_pipeline.py` | 查看每级过滤干掉多少候选 |
| `analyze_od_killed.py` | 评估 other_delta 硬过滤的影响面 |
| `analyze_remsec.py` | 按剩余时间分层分析胜率 |

### 研究笔记（4 文件）

`analyze_flip_*.py` 系列是按阶段演进的分析脚本：
1. `comprehensive` — 先了解 >0.7 后市场到底怎么走
2. `deep` — 深入 BTC 穿越后行为
3. `strategy` — 尝试 wait-and-see 策略
4. `final` — 最终参数优化

## 回测逻辑

```
1. 加载 events JSONL（来自 Go cmd/flip -lab-output 或 cmd/lab）
2. 计算 hist_avg_range（前 N 个 event 的 K线振幅均值）
3. 遍历每个 event 的 snapshots:
   a. YES/NO > 0.7 → 触发检测（仅首次穿越, 先 YES 后 NO）
   b. 等待 1 tick（5s）确认
   c. 计算 7 特征复合评分
   d. score ≥ 5 → 产生信号
   e. event 结束时根据 BTC outcome 结算 P&L
4. 输出摘要：信号数、胜率、总 P&L、Profit Factor
```

与 Go 端 `flip.Engine` 严格对齐：相同配置、相同逻辑、相同评分公式。

## 设计原则

1. **纯函数核心** — `backtest_flip_utils.py` 无 IO 依赖，所有特征函数可直接 import 使用
2. **与 Go 引擎一致** — config、特征计算、评分逻辑与 `internal/flip/` 保持同步
3. **分析脚本独立** — 每个 `analyze_*.py` 可独立运行，不互相依赖
4. **先回测、再分析、再调参** — 标准 workflow
