# python — 策略分析工具（TWAP 结算时代）

PM BTC 5 分钟市场的策略研究与回测工具。
背景与结论见 `docs/twap_flip_rederivation_2026-08-15.md`，
新数据采集计划见 `docs/data_recollection_plan_2026-08-16.md`（格式 v2，1s 分辨率）。

## 目录结构

```
python/
├── flip/                  # 翻转线（买对侧，目标 both 类，EV 上界 +0.51）
│   ├── backtest.py        #   模式 A 首个穿越 / 模式 B 双穿后 / 类别上界
│   └── analyze.py         #   首个穿越观测 + only/both 训练标签 + 特征 χ² 筛选
├── follow/                # follow 线（买穿越侧，目标 only 类，EV 上界 +0.19）
│   ├── backtest.py        #   模式 A 首个穿越（--filter: od0_pe4 等）/ 模式 B wait
│   └── analyze.py         #   特征筛选（同 flip 结构）
├── legacy/                # 旧 5s 快照格式时代全部脚本（归档参考，见 legacy/README.md）
├── requirements.txt
└── venv/
```

两条策略线**完全独立**（各自目录/回测/评估，P&L 分开，绝不合并）。

## 环境

```bash
cd python/
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

依赖：numpy, scipy, pandas；运行用 `venv/bin/python`。

## 快速开始

```bash
# follow 线回测（旧 5s 数据: data_0 补丁 / data/btc_5s）
./venv/bin/python follow/backtest.py --data ../data_0/lab_resolved --outcome-field market_outcome --mode both
./venv/bin/python follow/backtest.py --data ../data/btc_5s --outcome-field outcome --filter od0_pe4

# 翻转线回测
./venv/bin/python flip/backtest.py --data ../data_0/lab_resolved --outcome-field market_outcome
```

新数据（v2 格式，`data/btc`）到位后：先做基率复核，再扩展 flip/follow 两个
analyze.py 的新特征族（PM tape 流向、1s OFI、reprice 速度），按计划 §7 执行。
