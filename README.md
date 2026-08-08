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
│   │   ├── scoring.go            #   7特征复合评分（Formula A）
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

# 7特征预测能力分析
python analyze_features.py
```

---

## 3. 策略回测

在历史数据上回测 Flip Signal 策略，评估 7 特征复合评分（Formula A）的表现。

```bash
cd python/

# 基础回测
python backtest_flip_scoring.py --data ../data/lab/

# 导出信号到 JSONL（与 Go 端格式一致）
python backtest_flip_scoring.py --data ../data/lab/ --export signals.jsonl --verbose
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
# 纸面交易 + 实时看板（默认模式）
go run ./cmd/flip -output data/flip_signals.jsonl -dashboard :8090

# 同时输出 lab 数据
go run ./cmd/flip -output data/flip_signals.jsonl -lab-output data/lab -dashboard :8090
```

打开 http://localhost:8090 查看实时看板。

**信号文件格式**（`data/flip_signals.jsonl`）:

```json
{"time":"2026-08-08T12:05:15Z","condition_id":"0x...","side":"no","score":6,
 "entry_price":0.18,"shares":1,"remaining_sec":45,
 "path_eff":0.52,"noise_ratio":2.1,"flips":2,"is_oscillating":true,
 "range_expansion":0.3,"btc_position":-0.15,"btc_extreme":true,"other_delta":0.025}
// 结算后追加一行
{"type":"resolution","condition_id":"0x...","side":"no","entry_price":0.18,"shares":1,"won":true,"pnl":0.82}
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
| `max_slippage` | 0.07 (7%) | — | 价格上限 = entry×1.07 |
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
- `docs/flip_backtest_plan.md` — 回测方案设计
- `docs/flip_signal_evaluation_report.md` — 信号评估报告
