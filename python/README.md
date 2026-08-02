# Feature Research Lab — Python 端

BTC 5分钟 Polynarket 扫尾盘特征研究系统（Python 端）。

**对应 PRD2**：`docs/prd2.md`

---

## 环境初始化

```bash
cd python/

# 创建虚拟环境
python -m venv venv

# 激活虚拟环境
source venv/bin/activate        # macOS / Linux
# venv\Scripts\activate         # Windows

# 安装依赖
pip install -r requirements.txt
```

**依赖**：pandas, numpy, scipy（标准数据科学三件套）。

---

## 快速开始

```bash
# 1. 先运行 Go 端采集数据（另一个终端，持续运行 24h+）
cd .. && go run ./cmd/lab -output data/lab/

# 2. 积累足够数据后，运行特征分析
cd python/
python run_lab.py --data ../data/lab/ --output ../reports/

# 3. 查看报告
open ../reports/feature_report_*.md
```

---

## 目录结构

```
python/
├── README.md                          # 本文件
├── requirements.txt                   # Python 依赖
│
├── loader.py                          # [数据加载] JSONL → pandas DataFrame
├── run_lab.py                         # [入口] 编排加载→特征计算→分析→报告
│
├── features/                          # [特征层] 7个核心特征
│   ├── __init__.py                    #   特征注册表
│   ├── base.py                        #   Feature 抽象基类
│   ├── distance_to_strike.py          #   F1: DistanceToStrike
│   ├── safety_ratio.py                #   F2: SafetyRatio
│   ├── reversal_capacity.py           #   F3: ReversalCapacity
│   ├── volatility_expansion.py        #   F4: VolatilityExpansion
│   ├── direction_persistence.py       #   F5: DirectionPersistence
│   ├── signed_flow.py                 #   F6: SignedFlow
│   └── volume_acceleration.py         #   F7: VolumeAcceleration
│
├── analysis/                          # [分析层] 统计验证
│   ├── __init__.py
│   ├── buckets.py                     #   分档分析 (§8.1)
│   ├── time_condition.py              #   剩余时间条件分析 (§8.2)
│   ├── monotonicity.py                #   Spearman 单调性 (§8.3)
│   └── combination.py                 #   二维特征组合 (§8.4)
│
└── report/                            # [报告层] Markdown 输出
    ├── __init__.py
    └── generator.py                   #   完整研究报告生成器 (§9)
```

---

## 模块说明

### loader.py — 数据加载

将 Go 端生成的 `events_*.jsonl` 文件加载为 pandas DataFrame，每行一个 Snapshot。

```python
from loader import load_events, summary

df = load_events("data/lab/")
print(summary(df))
```

DataFrame 列：
- 所有 [ResearchSnapshot 字段](../internal/lab/types.go)（ts, event_id, remaining_sec, open, price, ret_1s, ...）
- `outcome` — 从 Event 层级展平（1=YES/Up, 0=NO/Down）
- `close_price` — Event 的最终价格

### features/ — 特征层

每个特征是一个 `Feature` 子类，实现 `compute(df) -> pd.Series`。

**7 个第一批核心特征**：

| # | 特征 | 文件 | 含义 |
|---|------|------|------|
| 1 | DistanceToStrike | `distance_to_strike.py` | 当前价格距开盘价的距离（方向归一化） |
| 2 | SafetyRatio | `safety_ratio.py` | 优势相对于近期波动是否够大 |
| 3 | ReversalCapacity | `reversal_capacity.py` | 市场反转能力 / 当前优势（越低越安全） |
| 4 | VolatilityExpansion | `volatility_expansion.py` | 短期波动 / 中期波动（>1=异常放大） |
| 5 | DirectionPersistence | `direction_persistence.py` | 方向上 1s return 占比（越高越持续） |
| 6 | SignedFlow | `signed_flow.py` | 主动买卖量差（方向归一化） |
| 7 | VolumeAcceleration | `volume_acceleration.py` | 近10s量 / 前10s量（>1=放量） |

**添加新特征**：
1. 在 `features/` 下新建文件，继承 `Feature`
2. 在 `features/__init__.py` 注册
3. 特征会自动出现在分析和报告中

### analysis/ — 分析层

每个分析模块独立可用：

```python
from analysis.buckets import analyze_buckets, bucket_summary_table
from analysis.time_condition import analyze_time_conditioned, time_condition_table
from analysis.monotonicity import analyze_monotonicity
from analysis.combination import analyze_combination, combination_matrix
```

**分析方法**（对应 PRD2 §8）：

| 模块 | 方法 | 输出 |
|------|------|------|
| `buckets.py` | 分10档统计胜率 + EV | Bucket 表 |
| `time_condition.py` | 按60s/30s/15s/5s分别分析 | 时间条件表 |
| `monotonicity.py` | Spearman Rank 相关系数 | ρ + p-value + 解读 |
| `combination.py` | 二维 4×4 网格胜率矩阵 | 组合矩阵 |

### report/generator.py — 报告生成

自动生成完整 Markdown 报告，包含所有特征的：
- Bucket 分析表
- 时间条件分析表
- Spearman 单调性
- 最佳区间
- 特征组合矩阵

---

## 设计原则

1. **数据 → 事实 → 特征 → 验证 → 组合 → 模型 → 交易**（严格遵守此顺序）
2. Go 端只采集纯事实，不做任何 Feature 计算
3. Python 端特征之间无耦合，可独立迭代
4. 所有分析面向"尾盘下注"场景：按剩余时间分档，关注尾盘60秒内的表现

---

## 第二阶段（待定）

当第一批 7 个特征被验证有效后：

- 特征组合发现（3+ 特征 AND 条件）
- 更多剩余时间 checkpoint（45s, 20s, 10s, 3s）
- 按市场时间段分层（亚洲盘/欧美盘）
- 滚动窗口 stability 分析（特征是否随时间退化）

## 第三阶段（待定）

- LightGBM 模型：P(after-fee profit | features)
- 实时特征计算管道
- 集成回现有 MQS 交易引擎
