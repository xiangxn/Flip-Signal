# CLAUDE.md — LastTrading (MQS)

## 项目概述

**LastTrading** 是一个针对 **Polymarket BTC 5分钟市场** 的尾盘量化交易系统。核心引擎 **MQS（Market Quality Specification）** 不预测价格方向，只评估市场状态是否足够健康，使得尾盘方向性下注具有正期望。

### 核心原则
> **不预测涨跌，只判断"值不值得下注"。**

---

## 项目结构

```
LastTrading/
├── cmd/mqs/main.go                     # 独立运行入口 + 市场循环
├── internal/
│   ├── snapshot/
│   │   ├── types.go                    # Snapshot 结构体 (PRD §1.2)
│   │   ├── ring_buffer.go              # 线程安全环形缓冲区 (容量300)
│   │   └── collector.go                # 每秒生成 Snapshot
│   ├── mqs/
│   │   ├── types.go                    # MarketQuality + 评分工具函数
│   │   ├── engine.go                   # 编排器: Snapshot[] → MarketQuality
│   │   ├── trend.go                    # 趋势评分 (30%): ER + R² + 方向一致性
│   │   ├── noise.go                    # 噪声评分 (25%): 翻转 + 路径噪声 + 波动稳定
│   │   ├── health.go                   # 健康评分 (20%): 动量衰减 + 量支撑 + 回撤
│   │   ├── flow.go                     # 订单流评分 (15%): 买卖比 + 签名流趋势
│   │   ├── liquidity.go                # 流动性评分 (10%): 深度失衡 + 稳定 + 点差
│   │   └── engine_test.go              # 9个单元测试
│   ├── decision/
│   │   ├── rules.go                    # 交易准入规则 (§9)
│   │   └── recorder.go                 # JSONL 决策日志 (§10)
│   ├── feed/
│   │   ├── binance_adapter.go          # Binance WS: 价格/量/深度
│   │   ├── orderbook_adapter.go        # Polymarket WS: YES/NO 盘口
│   │   └── snapshot_feed.go            # 合并双数据源 → 1秒Snapshot
│   └── strategy/
│       └── mqs_strategy.go             # polypilot 策略插件
├── config.example.yaml
├── DESIGN.md                           # 完整设计文档
├── prd.md                              # 原始 PRD
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
                     SnapshotFeed (1秒定时器)
                                │
                                ▼
                         SnapshotCollector
                          → RingBuffer
                                │
                                ▼
                          MQS Engine
                     (5维度评分 → 0-100)
                                │
                                ▼
                      Decision Engine
                     (禁止/标准/强信号)
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

Polymarket 5分钟市场是连续滚动的。每5分钟一个新市场。

```
1. 计算下个 5分钟对齐时间戳
2. 等待窗口开始 + 2秒 (确保 Binance kline 已生成)
3. GET /api/v3/klines → 获取当前5m K线开盘价
4. GET gamma-api/markets/slug/btc-updown-5m-<ts> → 获取市场信息
5. MarketMonitor.SubscribeTokens() → 订阅新市场的 YES/NO token
6. SnapshotFeed.Reset() → 清空 RingBuffer, 设置新 marketID/endTime
7. 每秒采集 Snapshot + 计算 MQS (持续 ~298秒)
8. 尾盘窗口 (最后60秒): 评估交易规则
9. 市场结束 → UnsubscribeTokens → goto 1
```

---

## 依赖库

| 库 | 版本 | 用途 |
|----|------|------|
| `github.com/xiangxn/go-polymarket-sdk` | v0.6.20 | Polymarket REST/WS 客户端 |
| `github.com/xiangxn/polypilot` | v0.2.16 | 交易机器人框架 (策略集成) |
| `github.com/gorilla/websocket` | v1.5.3 | Binance WebSocket 连接 |
| `github.com/spf13/viper` | v1.20.1 | 配置管理 |
| `github.com/tidwall/gjson` | v1.18.0 | JSON 解析 (SDK 依赖) |

### 本地开发 replace 指令

由于网络原因，go.mod 使用 replace 指向本地克隆：
```
replace (
    github.com/xiangxn/go-polymarket-sdk => /tmp/go-polymarket-sdk
    github.com/xiangxn/polypilot => /tmp/polypilot
)
```

---

## 代码规范

### Go 代码风格
- **标准库优先**: 能用标准库就不引入第三方依赖
- **零外部依赖核心**: `internal/snapshot/` 和 `internal/mqs/` 不依赖任何外部包，可独立测试
- **接口隔离**: SDK 集成层 (`internal/feed/`, `internal/strategy/`) 与核心计算层分离

### 命名约定
- 文件名: `snake_case.go`
- 包名: 小写单词, 与目录名一致
- 导出类型: `PascalCase`
- 私有函数: `camelCase`
- 评分函数: `ComputeXxxScore(window []*snapshot.Snapshot) float64`

### 评分函数规范
- 所有评分函数签名统一: `func ComputeXxxScore(window []*snapshot.Snapshot) float64`
- 返回值范围: `0-100`
- 子指标遵循 PRD 阈值表, 默认使用 `thresholdScore()` / `inverseThresholdScore()` 辅助函数
- `NoiseScore` 特殊: 内部计算 "噪声质量" (100=干净), 对外暴露 "噪声水平" (100=极乱)

### 并发安全
- `RingBuffer`: `sync.RWMutex` 保护
- `BinanceAdapter`: `sync.Mutex` 保护连接, `sync.RWMutex` 保护数据, `atomic.Bool` 保护启动状态
- `SnapshotFeed`: `sync.Mutex` 保护 collector 切换
- `Recorder`: `sync.Mutex` 保护文件写入

### 日志规范
- `log.Printf("[ComponentName] message")` — 统一前缀格式
- 关键状态用 emoji: ⚠️🔒🟢🔴🟡🔥
- 禁止 `fmt.Println` 用于运行日志

---

## 如何修改

### 添加新的评分维度
1. 在 `internal/mqs/` 新建文件如 `momentum.go`
2. 实现 `func ComputeMomentumScore(window []*snapshot.Snapshot) float64`
3. 在 `engine.go` 的 `ComputeMQS()` 中加入调用和权重
4. 更新 `MarketQuality` 结构体和 `engine_test.go`

### 添加新的数据源
1. 在 `internal/feed/` 新建 adapter
2. 在 `internal/snapshot/collector.go` 添加对应的 `UpdateXxx()` 方法
3. 在 `snapshot_feed.go` 的 Tick 循环中调用新数据源

### 修改交易规则
- 编辑 `internal/decision/rules.go` 的 `Decide()` 函数
- 阈值常量定义在文件顶部

### 修改 Binance 币种
- `main.go`: 传入 `BinanceConfig{Symbol: "ETHUSDT"}`
- 或修改 `config.example.yaml` 添加 `binance.symbol` 字段

### 测试
```bash
go test ./internal/mqs/ -v          # MQS 评分器单元测试
go build ./...                       # 全量编译检查
go run ./cmd/mqs                     # 运行（需要网络）
```

---

## 关键设计决策

1. **Binance 直连而非通过 Polymarket 转发**: Polymarket 的 `CryptoPriceMonitor` 只提供价格，没有成交量、深度、订单流。直连 Binance WS 获取完整 BTC 市场微观结构数据。

2. **单 WS 连接多流复用**: `btcusdt@trade` + `btcusdt@depth20` 合并为一个连接 — 一个流异常意味着整体数据不可靠，无需独立重连。

3. **OpenPrice 来自 5m K线而非首笔成交**: 与 Polymarket 5分钟窗口精确对齐。延迟 1-2s 获取确保 Binance 已生成新 K 线。

4. **核心计算层零外部依赖**: `snapshot` 和 `mqs` 包可独立编译、离线测试，不依赖任何网络或 SDK。

5. **无密钥只读模式**: 未配置私钥时自动生成临时密钥创建 PolymarketClient，仅用于数据读取，不下单。

6. **市场循环**: 不依赖定时器检测窗口切换，而是在每轮循环中计算下个对齐时间戳并 sleep 到窗口开始。

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
