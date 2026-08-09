# Code Review — cmd/flip/main.go 全量审查

> 日期：2026-08-09  
> 审查范围：`cmd/flip/main.go` 入口文件 + 关联的 `internal/trading/`、`internal/feed/`  
> 已完成修复：P0 #1, P1 #2, P1 #3, P1 #6, P2 #7  
> 本文档记录**未修复的剩余问题**，供后续 review 参考。

---

## 剩余问题

### P1 #5 — Token 解析与 outcomes 长度不匹配可能越界

**位置**：[`cmd/flip/main.go:396-404`](../cmd/flip/main.go#L396-L404)

```go
for i, oc := range outcomes {
    switch oc {
    case "Up", "Yes":
        yesTokenID = tokenIDs[i]  // outcomes 可能比 tokenIDs 长
```

虽然 Polymarket gamma API 返回的 `outcomes` 和 `clobTokenIds` 长度保证一致，防御性不足。若 API 格式变更，此处直接 panic。

**建议修复**：
```go
for i, oc := range outcomes {
    if i >= len(tokenIDs) {
        break
    }
    switch oc {
    case "Up", "Yes":
        yesTokenID = tokenIDs[i]
    case "Down", "No":
        noTokenID = tokenIDs[i]
    }
}
```

同时在解析完成后检查 `yesTokenID == "" || noTokenID == ""` 并打 warning。

---

### P2 #8 — Binance 启动等待无超时告警

**位置**：[`cmd/flip/main.go:199-209`](../cmd/flip/main.go#L199-L209)

```go
for i := 0; i < 30; i++ {
    ...
    if btc := binance.LatestData(); btc.Price != 0 { break }
}
// 30 秒后无数据 → 静默继续
```

如果 Binance 连接失败或数据延迟，30 秒后程序静默进入主循环，所有 `Tick()` 返回 nil，直到 Binance 恢复。期间无任何告警。

**建议修复**：循环结束后检查 `binance.LatestData().Price == 0`，打 `⚠️ Binance 数据仍未就绪，市场采集将跳过所有 tick 直至连接恢复`。

---

### P3 #9 — FlipRecorder.UpdateExecution 隐式空值约定

**位置**：[`cmd/flip/main.go:483`](../cmd/flip/main.go#L483)（信号发射）和 [`cmd/flip/main.go:529`](../cmd/flip/main.go#L529)（周期末对账）

两次 `UpdateExecution` 调用形成 pending → filled/failed 的状态演进：

- 信号发射时：`UpdateExecution(conditionID, "pending", 0, 0)`（GTC 挂单中）
- 周期末对账：`UpdateExecution(conditionID, "filled", shares, price)`（已有成交）
- 无挂单时：`OnCycleEnd` 返回 `ExecInfo{}`（Status=""），调用方通过 `ei.Status != ""` 跳过

这个空值约定是隐式的——如果未来有人在 `OnCycleEnd` 中返回 `Status=""` 但带了非零 shares，就会被静默丢弃。

**建议**：将 `ExecInfo` 的 `Status` 改为 `Status string` + `HasUpdate bool`，或用一个 sentinel error（如 `ErrNoUpdate`）替代空值判断。

---

### P3 #10 — bestBid 函数命名偏窄

**位置**：[`cmd/flip/main.go:543-548`](../cmd/flip/main.go#L543-L548)

```go
func bestBid(book *sdk.OrderBook) float64 {
```

当前只用 bid 价格（最优买价），函数名准确。但如果未来需要 ask 价格（最优卖价），需添加一个对称的 `bestAsk`。建议保持现状，届时再扩展。

---

### P3 #11 — generation 变量类型

**位置**：[`cmd/flip/main.go:302`](../cmd/flip/main.go#L302)

```go
var generation int64
```

用 `int64` 声明，但 `flip.Engine.Reset(generation int64)` 确实需要 int64。5 分钟一个周期，一天 288 个，一年 ~105k 个，int64 远超出需求（int32 也能存 68 年）。不影响功能，可保留。

---

### P3 #12 — 硬编码超时散落各处

以下超时/间隔均以字面量写入代码，无集中管理：

| 值 | 位置 | 含义 |
|----|------|------|
| 30s | line 199 | Binance 启动等待上限 |
| 5s | line 337 | K 线请求超时 |
| 15s | line 365 | 市场信息请求超时 |
| 5s | line 375 | 市场信息失败重试间隔 |
| 5s | line 442 | 采集采样间隔 |
| 10s | line 244 | 结算轮询间隔 |
| 10s | line 67 | 优雅退出等待 |
| 10s | line 315 | 跳过残窗的阈值 |

**建议**：将核心运行时参数（采样间隔、轮询间隔、超时）集中到 `config.RuntimeConfig` 中，提供合理默认值，允许配置文件覆盖。

---

## 已修复问题（供参考）

| # | 问题 | 修复方式 |
|---|------|----------|
| P0 #1 | `nextStart` 计算当前边界而非下一个 | 加入 10s 阈值跳过残窗 |
| P1 #2 | 双重取消订阅 | 删除循环末尾的重复 `UnsubscribeTokens` |
| P1 #3 | `event.Outcome` 死参数 | 从 `OnCycleEnd` 签名移除 |
| P1 #6 | 过期 WS 注释 | 更新为准确描述 |
| P2 #7 | snap=nil 跳过 remaining 检查 | 加入独立的时间检查 + break |
