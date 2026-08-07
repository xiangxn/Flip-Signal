# CLAUDE.md — LastTrading

## 项目概述

**LastTrading** 是一个针对 **Polymarket BTC 5分钟市场** 的量化交易系统。核心引擎 **Flip Signal Detection** 检测 Polymarket YES/NO 价格穿越 0.7 后的反转信号，基于 7 特征复合评分进行方向性下注。

### 核心原则
> **不预测涨跌，只判断"什么时候市场过度自信"。**

---

## 项目结构

```
LastTrading/
├── cmd/
│   ├── flip/main.go                     # Flip Signal 检测引擎 + Dashboard
│   ├── lab/main.go                      # 实验室数据采集 (ResearchSnapshot)
│   └── test_resolve/                    # 解析测试工具
├── internal/
│   ├── lab/
│   │   ├── types.go                     # ResearchSnapshot + Event 结构体
│   │   └── collector.go                 # 5秒生成 ResearchSnapshot
│   ├── flip/
│   │   ├── types.go                     # FlipConfig + FlipSignal + ScoreParams
│   │   ├── engine.go                    # 状态机: Watching → Confirming → Done
│   │   ├── features.go                  # 特征提取: PathEff, NoiseRatio, CountFlips...
│   │   ├── scoring.go                   # 7特征复合评分 (Formula A)
│   │   ├── hist_range.go               # 历史K线波动范围追踪器
│   │   ├── recorder.go                  # JSONL 信号记录 + P&L 结算
│   │   └── engine_test.go              # 单元测试
│   ├── feed/
│   │   ├── binance_adapter.go           # Binance WS: 价格/量/深度
│   │   └── orderbook_adapter.go         # Polymarket WS: YES/NO 盘口
│   └── dashboard/
│       ├── server.go                    # HTTP server
│       ├── handlers.go                  # /api/state, /api/signals, /api/snapshots...
│       ├── state.go                     # 运行时组件引用
│       ├── templates.go                 # 嵌入式 HTML 模板
│       └── static/                      # app.js, style.css, index.html
├── config.example.yaml
├── go.mod / go.sum
└── CLAUDE.md                           # 本文件
```

---

## 架构与数据流

```
                          Binance                  Polymarket
                     ┌───── WS ─────┐         ┌──── WS ──────┐
                     │ btcusdt@trade│         │ MarketMonitor│
                     │ @depth20     │         │ (CLOB books) │
                     └──┬───────┬───┘         └──────┬───────┘
                        │       │                    │
                        ▼       ▼                    ▼
                   BTC价格/量  深度              YES/NO价格
                        │       │                    │
                        └───────┼────────────────────┘
                                ▼
                     lab.Collector (5秒定时器)
                                │
                                ▼
                        ResearchSnapshot
                                │
                                ▼
                         Flip Engine
                     (状态机 + 7特征评分)
                                │
                                ▼
                         FlipRecorder
                      (JSONL + P&L结算)
```

### 数据源分工

| 数据 | 来源 | 方式 |
|------|------|------|
| BTC 价格 | Binance `btcusdt@trade` | WebSocket |
| BTC 5m 开盘价 | Binance REST `/api/v3/klines?interval=5m` | HTTP (每周期) |
| 买卖成交量 | Binance `btcusdt@trade` (aggressor side) | WebSocket |
| 订单簿深度 | Binance `btcusdt@depth20@100ms` | WebSocket |
| YES/NO 盘口 | Polymarket `MarketMonitor` | WebSocket |

### 市场循环流程

```
1. 计算下个 5分钟对齐时间戳
2. 等待窗口开始 + 2秒 (确保 Binance kline 已生成)
3. GET /api/v3/klines → 获取当前5m K线开盘价
4. GET gamma-api/markets/slug/btc-updown-5m-<ts> → 获取市场信息
5. MarketMonitor.SubscribeTokens() → 订阅新市场的 YES/NO token
6. Collector.StartEvent() → 设置 conditionID/openPrice/endTime
7. 每5秒采集 ResearchSnapshot + Flip Engine 检测 (持续 ~298秒)
8. 市场结束 → FinalizeEvent → Resolve → UnsubscribeTokens → goto 1
```

---

## 依赖库

| 库 | 版本 | 用途 |
|----|------|------|
| `github.com/xiangxn/go-polymarket-sdk` | v0.6.20 | Polymarket REST/WS 客户端 |
| `github.com/gorilla/websocket` | v1.5.3 | Binance WebSocket 连接 |
| `github.com/tidwall/gjson` | v1.18.0 | JSON 解析 (SDK 依赖) |

### 本地开发 replace 指令

```
replace (
    github.com/xiangxn/go-polymarket-sdk => /tmp/go-polymarket-sdk
)
```

---

## 代码规范

### Go 代码风格
- **标准库优先**: 能用标准库就不引入第三方依赖
- **零外部依赖核心**: `internal/flip/` 和 `internal/lab/` 不依赖任何外部包，可独立测试
- **接口隔离**: SDK 集成层 (`internal/feed/`) 与核心计算层分离

### 命名约定
- 文件名: `snake_case.go`
- 包名: 小写单词, 与目录名一致
- 导出类型: `PascalCase`
- 私有函数: `camelCase`

### 并发安全
- `BinanceAdapter`: `sync.Mutex` 保护连接, `sync.RWMutex` 保护数据, `atomic.Bool` 保护启动状态, `sync.Mutex` 保护成交量
- `HistRangeTracker`: `sync.RWMutex` 保护
- `FlipRecorder`: `sync.Mutex` 保护文件写入

### 日志规范
- `log.Printf("[ComponentName] message")` — 统一前缀格式
- 关键状态用 emoji: ⚠️🔒🟢🔴🟡🔥🎯
- 禁止 `fmt.Println` 用于运行日志

---

## 如何修改

### 添加新的评分特征
1. 在 `internal/flip/types.go` 的 `FlipConfig` 和 `ScoreParams` 中添加新字段
2. 在 `internal/flip/scoring.go` 的 `ComputeFlipScore()` 中加入新特征
3. 在 `internal/flip/engine.go` 的 `enterConfirming()` 或 `onConfirmed()` 中计算特征值

### 添加新的数据源
1. 在 `internal/feed/` 新建 adapter
2. 在 `internal/lab/collector.go` 的 `Tick()` 中调用新数据源
3. 在 `ResearchSnapshot` 结构体中添加新字段

### 修改 Flip 检测参数
- 编辑 `internal/flip/types.go` 的 `DefaultConfig()` 函数
- 或启动时通过 flag 覆盖

### 测试
```bash
go test ./internal/flip/ -v           # Flip Engine 单元测试
go build ./...                         # 全量编译检查
go run ./cmd/flip -dashboard :8090     # 运行 Flip 检测 + Dashboard
```

---

## 关键设计决策

1. **Binance 直连而非通过 Polymarket 转发**: Polymarket 的 `CryptoPriceMonitor` 只提供价格，没有成交量、深度、订单流。

2. **单 WS 连接多流复用**: `btcusdt@trade` + `btcusdt@depth20` 合并为一个连接。

3. **OpenPrice 来自 5m K线而非首笔成交**: 与 Polymarket 5分钟窗口精确对齐。

4. **核心计算层零外部依赖**: `flip` 和 `lab` 包可独立编译、离线测试。

5. **无密钥只读模式**: 未配置私钥时自动生成临时密钥，仅用于数据读取。

6. **5秒采样**: 与 Polymarket CLOC 盘口更新频率匹配，减少噪声。

---

## 环境变量

| 变量 | 说明 | 必填 |
|------|------|------|
| `POLYMARKET_OWNER_KEY` | 钱包私钥 (hex) | 仅交易模式 |
| `POLYMARKET_CLOB_KEY` | CLOB API Key | 仅交易模式 |
| `POLYMARKET_CLOB_SECRET` | CLOB API Secret | 仅交易模式 |
| `POLYMARKET_CLOB_PASSPHRASE` | CLOB API Passphrase | 仅交易模式 |
| `POLYMARKET_FUNDER` | 代理钱包地址 | 可选 |
| `POLYMARKET_PROXY` | SOCKS5 代理 | 可选 |
