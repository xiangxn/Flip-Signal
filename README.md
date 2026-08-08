# Flip Signal

> 针对 **Polymarket BTC 5分钟市场** 的量化交易系统。
> 核心原则：**不预测涨跌，只判断"什么时候市场过度自信"。**

---

## 管线概览

```
┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│  1. 数据采集  │───▶│  2. 数据分析  │───▶│  3. 策略回测  │───▶│  4. 纸面交易  │───▶│  5. 实盘交易  │
│   cmd/lab    │    │   python/     │    │   python/     │    │   cmd/flip   │    │   (规划中)    │
│              │    │   analyze_*   │    │   backtest_*  │    │   +dashboard │    │              │
└──────────────┘    └──────────────┘    └──────────────┘    └──────────────┘    └──────────────┘
     JSONL              研究笔记             JSONL              JSONL
   events.jsonl        flip_*.py         flip_signals        flip_signals
                                            .jsonl              .jsonl
```

---

## 项目结构

```
FlipSignal/
├── cmd/
│   ├── flip/main.go              # Flip 检测引擎 + Dashboard（纸面交易入口）
│   ├── lab/main.go               # 数据采集器（输出 events JSONL）
│   └── test_resolve/main.go      # 结算测试工具
├── internal/
│   ├── flip/                     # Flip Engine 核心（零外部依赖）
│   │   ├── types.go              #   FlipConfig, FlipSignal, ScoreParams
│   │   ├── engine.go             #   状态机: Watching → Confirming → Done
│   │   ├── features.go           #   特征提取纯函数
│   │   ├── scoring.go            #   7特征复合评分（Formula A）
│   │   ├── hist_range.go        #   历史K线波动范围追踪
│   │   ├── recorder.go           #   JSONL 信号记录 + P&L 结算
│   │   └── engine_test.go       #   单元测试
│   ├── lab/                      # 数据采集层
│   │   ├── types.go              #   ResearchSnapshot, Event
│   │   ├── collector.go          #   5秒采样 + 事件封装
│   │   └── writer.go             #   JSONL 按日持久化
│   ├── feed/                     # 数据源适配层
│   │   ├── binance_adapter.go    #   Binance WS: 价格/量/深度
│   │   └── orderbook_adapter.go  #   Polymarket WS: YES/NO 盘口
│   └── dashboard/                # HTTP 实时看板
│       ├── server.go / handlers.go / state.go
│       └── static/               #   前端: app.js, style.css
├── python/                       # Python 回测 & 分析工具
│   ├── backtest_flip_scoring.py  #   回测入口
│   ├── backtest_flip_config.py   #   参数配置（与 Go 端对齐）
│   ├── backtest_flip_utils.py    #   特征提取 + 信号检测
│   └── analyze_*.py              #   分析脚本
├── docs/                         # 策略设计 & 分析文档
├── data/                         # 运行时数据（JSONL 输出）
├── go.mod / go.sum
└── README.md                     # 本文件
```

---

## 1. 数据采集

采集 Binance BTC 价格/量/深度 + Polymarket YES/NO 盘口，每 5 秒生成 ResearchSnapshot，按 5 分钟窗口组织为 Event，输出 JSONL。

```bash
# 启动数据采集（输出到 data/lab/）
go run ./cmd/lab -output data/lab -symbol BTCUSDT

# 输出文件按日切分
# data/lab/events_2026-08-08.jsonl
```

**数据源**:

| 数据 | 来源 | 协议 |
|------|------|------|
| BTC 价格 | Binance `btcusdt@trade` | WebSocket |
| 买卖量 | Binance `btcusdt@trade`（aggressor side）| WebSocket |
| 订单簿 | Binance `btcusdt@depth20@100ms` | WebSocket |
| 5m 开盘价 | Binance REST `/api/v3/klines` | HTTP |
| YES/NO 盘口 | Polymarket `MarketMonitor` | WebSocket |

**Snapshot 字段**（每个事件约 60 条 snapshot）:

| 字段 | 含义 |
|------|------|
| `ts` | 时间戳（ms）|
| `open` / `price` | BTC 开盘价 / 当前价 |
| `remaining_sec` | 距市场到期剩余秒数 |
| `buy_vol_5s` / `sell_vol_5s` | 5秒买卖成交量 |
| `yes_price` / `no_price` | Polymarket YES/NO 中间价 |
| `bid_depth` / `ask_depth` | 订单簿深度（前 5 档）|

---

## 2. 数据分析

使用 Python 分析采集到的 Event 数据，理解 >0.7 穿越后的市场行为。

```bash
cd python/
pip install -r requirements.txt

# 穿越事件综合分析（入门分析）
python analyze_flip_comprehensive.py

# 7特征预测能力分析
python analyze_features.py

# 过滤管线逐级杀灭率
python analyze_filter_pipeline.py
```

**分析流程**：

1. `analyze_flip_comprehensive.py` — 先看清楚：穿越后市场到底怎么走？
2. `analyze_flip_deep.py` — 深入 BTC 穿越后行为
3. `analyze_flip_strategy.py` — 尝试 wait-and-see 策略
4. `analyze_flip_final.py` — 入场价 + BTC 跑道优化

---

## 3. 策略回测

在历史数据上回测 Flip Signal 策略，评估 7 特征复合评分（Formula A）的表现。

```bash
cd python/

# 基础回测
python backtest_flip_scoring.py --data ../data/lab/

# 导出信号到 JSONL（与 Go 端格式一致）
python backtest_flip_scoring.py --data ../data/lab/ --export signals.jsonl --verbose

# 参数扫描
python sweep_other_delta.py
```

**回测逻辑**（与 Go Engine 完全一致）：

```
遍历每个 Event:
  ├─ YES/NO 首次 > 0.7 → 触发检测
  ├─ 等待 1 tick（5s）确认
  ├─ 计算 7 特征复合评分（Formula A）
  ├─ score ≥ 5 → 产生信号
  └─ Event 结束时按 BTC outcome 结算 P&L
```

**Formula A 评分项**:

| # | 特征 | 条件 | 得分 |
|---|------|------|------|
| F1v | OtherDelta（对侧确认）| > 0.05 | +3 |
| F1 | OtherDelta | > 0.02 | +2 |
| F2 | OtherDelta | > 0.01 | +1 |
| F3 | IsOscillating（振荡衰竭）| 三项全满足 | +2 |
| F4 | EntryPrice（便宜入场）| < 0.20 | +1 |
| F5 | EntryPrice | < 0.25 | +1 |
| F6 | RangeExpansion（BTC 未动）| < 0.5 | +2 |
| F7 | BTCExtreme（BTC 背离）| 方向背离 | +1 |
| **F0** | **RangeExpansion 过大** | **≥ 2.0** | **否决** |

---

## 4. 纸面交易

Go 版 Flip Engine 实时检测信号，记录为 JSONL，市场到期后自动结算 P&L。**不执行真实订单**。

```bash
# 纸面交易 + 实时看板
go run ./cmd/flip -output data/flip_signals.jsonl -dashboard :8090

# 同时输出 lab 数据（不需要单独跑 cmd/lab）
go run ./cmd/flip -output data/flip_signals.jsonl -lab-output data/lab -dashboard :8090
```

打开 http://localhost:8090 查看实时看板：

- **状态**: Engine 状态（Idle / Watching / Confirming / Done）
- **Snapshots**: 当前周期已采集的 snapshot 数量
- **信号列表**: 全部历史信号 + 结算结果（Won / Lost / PnL）
- **特征详情**: T=0 穿越时刻的 7 特征值

**信号文件格式**（`data/flip_signals.jsonl`）:

```json
{"time":"2026-08-08T12:05:15Z","condition_id":"0x...","side":"no","score":6,
 "entry_price":0.18,"shares":1,"remaining_sec":45,
 "path_eff":0.52,"noise_ratio":2.1,"flips":2,"is_oscillating":true,
 "range_expansion":0.3,"btc_position":-0.15,"btc_extreme":true,"other_delta":0.025}
// 结算后追加一行
{"won":true,"pnl":0.82}
```

**运行模式**:

| 模式 | 条件 | 行为 |
|------|------|------|
| 只读模式 | 未配置 `POLYMARKET_OWNER_KEY` | 自动生成临时密钥，仅读取数据 + 纸面记录 |
| 交易模式 | 已配置钱包 + CLOB 凭证 | 纸面记录（暂不执行真实订单）|

---

## 5. 实盘交易

> 🚧 **规划中** — 当前纸面交易阶段积累足够信号和统计置信度后，接入 Polymarket CLOB 执行真实订单。

前置条件：
- [ ] 纸面交易信号 > 100 笔
- [ ] 胜率 > 50%，Profit Factor > 2.0（维持回测水平）
- [ ] 单笔风险可控（固定 share 数，不改）

---

## 环境变量

| 变量 | 说明 | 必填 |
|------|------|------|
| `POLYMARKET_OWNER_KEY` | 钱包私钥（hex）| 仅交易模式 |
| `POLYMARKET_CLOB_KEY` | CLOB API Key | 仅交易模式 |
| `POLYMARKET_CLOB_SECRET` | CLOB API Secret | 仅交易模式 |
| `POLYMARKET_CLOB_PASSPHRASE` | CLOB API Passphrase | 仅交易模式 |
| `POLYMARKET_FUNDER` | 代理钱包地址 | 可选 |
| `POLYMARKET_PROXY` | SOCKS5 代理 | 可选 |

---

## 命令速查

```bash
# 数据采集
go run ./cmd/lab -output data/lab

# 纸面交易 + 看板
go run ./cmd/flip -output data/flip_signals.jsonl -dashboard :8090

# 纸面交易 + 同时采集 lab 数据
go run ./cmd/flip -output data/flip_signals.jsonl -lab-output data/lab -dashboard :8090

# Python 回测
cd python && python backtest_flip_scoring.py --data ../data/lab/ --verbose

# 编译 & 测试
go build ./...
go test ./internal/flip/ -v
```

---

## 参考文档

- [CLAUDE.md](CLAUDE.md) — Go 代码规范与架构细节
- [python/README.md](python/README.md) — Python 分析工具说明
- `docs/flip_backtest_plan.md` — 回测方案设计
- `docs/flip_signal_evaluation_report.md` — 信号评估报告
- `docs/flip_paper_trading_plan.md` — 纸面交易实施计划
