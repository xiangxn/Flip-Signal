# Flip Signal

> 针对 **Polymarket BTC 5分钟市场** 的量化交易系统。
> 核心原则：**不预测涨跌，只判断"什么时候市场过度自信"。**

---

## 管线概览

```
┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│  1. 数据采集  │───▶│  2. 数据分析  │───▶│  3. 策略回测  │───▶│  4. 纸面交易  │───▶│  5. 实盘交易  │
│   cmd/lab    │    │   python/     │    │   python/     │    │   cmd/flip   │    │   cmd/flip   │
│              │    │   analyze_*   │    │   backtest_*  │    │   +dashboard │    │   -trading   │
└──────────────┘    └──────────────┘    └──────────────┘    └──────────────┘    └──────────────┘
     JSONL              研究笔记             JSONL              JSONL              JSONL
   events.jsonl        flip_*.py         flip_signals        flip_signals        trades.jsonl
                                            .jsonl              .jsonl
```

---

## 项目结构

```
FlipSignal/
├── cmd/
│   ├── flip/main.go              # Flip 交易引擎入口（纸面 + 实盘 + Dashboard）
│   ├── lab/main.go               # 数据采集器（输出 events JSONL）
│   └── test_resolve/main.go      # 结算测试工具
├── internal/
│   ├── config/
│   │   └── config.go             # AppConfig + Load() + 敏感字段解密
│   ├── flip/                     # Flip Engine 核心（零外部依赖）
│   │   ├── types.go              #   FlipConfig, FlipSignal, ScoreParams
│   │   ├── engine.go             #   状态机: Watching → Confirming → Done
│   │   ├── features.go           #   特征提取纯函数
│   │   ├── scoring.go            #   Formula B 评分（背离 + 过度自信 + 确认回归）
│   │   ├── hist_range.go        #   历史K线波动范围追踪
│   │   ├── recorder.go           #   JSONL 信号记录 + P&L 结算
│   │   └── engine_test.go       #   单元测试
│   ├── trading/                  # 实盘交易执行层
│   │   ├── types.go              #   TradingConfig, OrderRecord, Position
│   │   ├── risk.go               #   纯函数风险检查（零外部依赖）
│   │   ├── order.go              #   信号→FAK 订单映射
│   │   ├── client.go             #   TradeClient 接口 + SDK 适配器
│   │   ├── recorder.go           #   交易 JSONL 日志
│   │   ├── trader.go             #   核心 Trader：开关、下单、结算
│   │   └── *_test.go
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

---

## 2. 数据分析

使用 Python 分析采集到的 Event 数据，理解 >0.7 穿越后的市场行为。

```bash
cd python/
pip install -r requirements.txt

# 穿越事件综合分析
python analyze_flip_comprehensive.py

# 特征预测能力分析
python analyze_features.py

# 胜率/EV 优化分析（2026-08-13，含 ask 口径修正与减法实验）
python optimize_winrate.py all
```

---

## 3. 策略回测

在历史数据上回测 Flip Signal 策略。当前版本为 **Formula B 精简公式**（2026-08-13 标定），
完整方案见 [docs/flip_strategy_plan_2026-08-13.md](docs/flip_strategy_plan_2026-08-13.md)。

```bash
cd python/

# 基础回测（默认 Formula B 参数）
python backtest_flip_scoring.py --data ../data_0/lab/

# 导出信号到 JSONL（与 Go 端格式一致）
python backtest_flip_scoring.py --data ../data_0/lab/ --export signals.jsonl --verbose
```

**基准结果**（data_0，1719 事件，按对侧 ASK 成交）：n=126、WR 46.0%、P&L +26.91、
PF 2.60、avg_fill 0.247、6 个完整日全部正 EV。

**Formula B 信号条件**（三个条件与策略哲学一一对应）:

| # | 条件 | 规则 | 得分 |
|---|------|------|------|
| **B1** | **BTC 背离硬要求** | 穿越时刻 BTC 必须与 PM 反向（YES侧触发要求 `btc_pos < -0.05`，NO侧要求 `> 0.05`）；同向/中性穿越 EV≈0，直接否决 | 硬过滤 |
| B2 | RangeExpansion（过度自信）| BTC 振幅 < 历史平均的一半（BTC 没动但 PM 已 0.7+）| +2 |
| B3 | OtherDelta（确认回归）| 确认期对侧 bid 回升 > 0.05 / 0.02 / 0.01 | +3 / +2 / +1 |
| F0 | RangeExpansion 过大 | ≥ 1.5（真突破，PM 是对的）| 否决 |
| Gate | 成交价上限（**ask 口径**）| 确认时刻对侧 ask = 1 - 触发侧 bid > 0.45 | 信号无效 |

- `score_entry = 2`：任一核心信号成立即触发，无需特征堆叠
- 已停用：振荡（无统计效力）、低价入场（方向相反）、btc_extreme 加分（由 B1 取代）、
  path_eff<0.4 与 noise>3 否决
- **成交口径**：`yes_price/no_price` 存的是各订单簿 best bid，实盘 FAK 买对侧成交在
  ask = 1 - 触发侧 bid，回测 fill 与 gate 均按此口径

---

## 4. 纸面交易

Go 版 Flip Engine 实时检测信号，记录为 JSONL，市场到期后自动结算 P&L。**不执行真实订单**。

```bash
# 纸面交易 + 实时看板（默认模式）
go run ./cmd/flip -output data/flip_signals.jsonl -dashboard :8090

# 同时输出 lab 数据
go run ./cmd/flip -output data/flip_signals.jsonl -lab-output data/lab -dashboard :8090
```

打开 http://localhost:8090 查看实时看板。

### Flip Engine 信号检测流程

每个 snapshot（~5s）调用 `ProcessSnapshot()`，引擎内部按以下两阶段运行：

```
┌──────────────────────────────────────────────────────────────────┐
│ 阶段 1: enterConfirming() — 穿越时刻立即执行                      │
│                                                                   │
│ 用穿越点及之前的 BTC 价格序列计算 T=0 特征:                        │
│                                                                   │
│   btc_position    = (price - open) / hist_avg_range               │
│   divergence      = ±btc_position (YES侧取负) — 正=BTC与PM反向    │
│   range_expansion = |price - open| / hist_avg_range               │
│                                                                   │
│   硬过滤 (任一不通过 → 立即否决，不等待确认):                      │
│   • B1 背离不足    divergence < 0.05 → BTC 与 PM 同向，EV≈0      │
│   • F0 真突破      range_expansion ≥ 1.5 → PM 是对的              │
│                                                                   │
│   全部通过 → 保存特征，状态切换到 stateConfirming                  │
│   等待 confirm_delay_ticks 个 snapshot…                            │
└──────────────────────────────────────────────────────────────────┘
                               │
                               │ 等待 confirm_delay_ticks × 5s
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│ 阶段 2: onConfirmed() — 确认数据到达后才执行                       │
│                                                                   │
│   唯一需要等待的特征:                                              │
│   other_delta = 对面 bid[t+N] - 对面 bid[t]                       │
│     YES>0.7 → 对面=NO,  买 NO 赌 DOWN → NoPrice 涨 = +分         │
│     NO>0.7  → 对面=YES, 买 YES 赌 UP  → YesPrice 涨 = +分        │
│                                                                   │
│   成交价 (ask 口径): fill = 1 - 触发侧 bid[t+N]                   │
│   gate: fill > max_entry_price(0.45) → 信号无效                   │
│                                                                   │
│   Formula B 评分:                                                 │
│   B3 other_delta > 0.05   +3                                      │
│   B3 other_delta > 0.02   +2                                      │
│   B3 other_delta > 0.01   +1                                      │
│   B2 振幅 < 0.5           +2   (过度自信)                         │
│                                                                   │
│   总分 ≥ score_entry(2) → 🎯 FlipSignal                           │
│   总分 < 2 → 回 Watching，尝试 pending crossings                  │
└──────────────────────────────────────────────────────────────────┘
```

**状态机**: `Watching → Confirming → Done`，多穿越重试模式下每个上升沿都触发检测，首个评分通过者下注，每周期最多一注。

**信号文件格式**（`data/flip_signals.jsonl`）:

```json
{"time":"2026-08-08T12:05:15Z","condition_id":"0x...","side":"no","score":2,
 "entry_price":0.21,"shares":1,"remaining_sec":45,
 "btc_divergence":0.27,"range_expansion":0.3,
 "btc_position":0.27,"other_delta":0.005}
// 结算后追加一行
{"type":"resolution","condition_id":"0x...","side":"no","entry_price":0.21,"shares":1,"won":true,"pnl":0.79}
```

---

## 5. 实盘交易

信号触发时通过 Polymarket CLOB 提交 **FAK（Fill-And-Kill）市价单**，依据真实成交与市场结算计算 realized P&L。

### 前置条件

- 配置 Polymarket 钱包私钥 + CLOB API 凭证
- （推荐）纸面交易积累 ≥ 100 信号，胜率 > 50%

### 快速开始

```bash
# 配置文件方式：设置 trading.enabled: true
go run ./cmd/flip -config config.yaml -dashboard :8090

# CLI 方式：-trading 启用
go run ./cmd/flip -trading -stake 5 -max-loss 10 -dashboard :8090
```

### 交易参数

| 参数 | 默认值 | CLI flag | 说明 |
|------|--------|----------|------|
| `enabled` | false | `-trading` | 启用实盘 |
| `stake_per_signal` | 5.0 | `-stake` | 每信号 USDC 预算 |
| `max_price` | 0.45 | — | 最高允许价格（绝对限价，ask 口径与回测 gate 一致）|
| `max_daily_loss` | 10.0 | `-max-loss` | 日亏上限，触发后当日停止 |
| `cooldown_after_loss_sec` | 300 | — | 亏损后冷却 5 分钟 |

### 订单执行流程

```
信号触发 → 风控闸门 → CreateMarketOrder(FAK) → PostOrder → GetOpenOrders 确认
                                                              │
                                                         Filled → 建仓
                                                         Failed → skip
```

- **FAK（Fill-And-Kill）**：按 orderbook ≤ maxPrice 的流动性立即成交，未成交部分自动取消
- 结算：WS 真实结算优先，超时回退 BTC 模拟结算
- 风控：日亏上限 / 亏损冷却 / 并发持仓限制

### 交易记录

实盘订单与结算写入 `data/trades.jsonl`（格式见 `internal/trading/types.go`），与纸面信号文件独立。

---

## 运行模式

| 模式 | 条件 | 行为 |
|------|------|------|
| 只读（纸面）| 未配置 `PM_SDK_POLYMARKET_OWNER_KEY` | 生成临时密钥，仅读取数据 + 纸面信号记录 |
| 实盘（未启用）| 已配置凭证，`trading.enabled: false` | 纸面记录 + 实盘待命（Dashboard 显示 "live"）|
| 实盘（已启用）| 已配置凭证，`trading.enabled: true` 或 `-trading` | 纸面 + 真实 FAK 下单 + 风控 |

---

## 环境变量

所有环境变量使用 `PM_` 前缀，点号替换为下划线，由 viper 自动映射。
例如 `PM_SDK_POLYMARKET_OWNER_KEY` → `sdk.polymarket.owner_key`。

| 变量 | 说明 | 必填 |
|------|------|------|
| `PM_SDK_POLYMARKET_OWNER_KEY` | 钱包私钥（hex，支持加密存储）| 仅交易模式 |
| `PM_SDK_POLYMARKET_CLOB_CREDS_KEY` | CLOB API Key | 仅交易模式 |
| `PM_SDK_POLYMARKET_CLOB_CREDS_SECRET` | CLOB API Secret | 仅交易模式 |
| `PM_SDK_POLYMARKET_CLOB_CREDS_PASSPHRASE` | CLOB API Passphrase | 仅交易模式 |
| `PM_SDK_POLYMARKET_FUNDER_ADDRESS` | 代理钱包地址 | 可选 |
| `PM_SDK_SOCKS_PROXY` | SOCKS5 代理 | 可选 |
| `PM_CONFIG_DECRYPT_PASSWORD` | 解密密码（替代交互式输入）| 可选 |
| `PM_RUNTIME_SYMBOL` | Binance 交易对 | 可选 |
| `PM_FLIP_TRIGGER_THRESHOLD` 等 | Flip 引擎参数 | 可选 |

敏感字段（`owner_key`、`clob_creds.*`）支持 AES-256-CBC 加密存储。
用 `pmutils.NewEncryptor(password).Encrypt(plaintext)` 生成密文写入 config.yaml，
启动时通过 `PM_CONFIG_DECRYPT_PASSWORD` 或交互式终端输入密码解密。

---

## 命令速查

```bash
# 数据采集
go run ./cmd/lab -output data/lab

# 纸面交易 + 看板（默认模式）
go run ./cmd/flip -dashboard :8090

# 纸面交易 + 同时采集 lab 数据
go run ./cmd/flip -lab-output data/lab -dashboard :8090

# 实盘交易（启用 FAK 下单）
go run ./cmd/flip -trading -dashboard :8090

# 实盘 + 自定义 stake + 日亏上限
go run ./cmd/flip -trading -stake 10 -max-loss 20 -dashboard :8090

# Python 回测
cd python && python backtest_flip_scoring.py --data ../data/lab/ --verbose

# 编译 & 测试
go build ./...
go test ./internal/... -v
```

---

## 参考文档

- [CLAUDE.md](CLAUDE.md) — Go 代码规范与架构细节
- [docs/live_trading_plan.md](docs/live_trading_plan.md) — 实盘交易实施方案
- [python/README.md](python/README.md) — Python 分析工具说明
- `docs/flip_strategy_plan_2026-08-13.md` — Formula B 实施方案（当前策略规格）
- `docs/flip_optimization_analysis_2026-08-13.md` — 胜率/EV 优化分析过程
- `docs/flip_backtest_plan.md` — 回测方案设计（历史版本，Formula B 前的设计）
- `docs/flip_signal_evaluation_report.md` — 信号评估报告
