# Flip Signal Detection — HTTP Dashboard Design

> **状态**: 待确认  
> **日期**: 2026-08-07  
> **目标**: 为 `cmd/flip/main.go` 添加实时 HTTP 看板，可视化 Flip Signal Detection 引擎的运行状态

---

## 1. 架构总览

```
┌──────────────────────────────────────────────┐
│ cmd/flip/main.go                             │
│                                              │
│  Market Loop (goroutine 1)                   │
│  ┌──────────────────────────────────────┐    │
│  │ Binance WS → Collector → Engine      │    │
│  │   → FlipRecorder (JSONL)             │    │
│  └──────────────┬───────────────────────┘    │
│                 │ 读写引用                    │
│  ┌──────────────▼───────────────────────┐    │
│  │ internal/dashboard/ (goroutine 2)    │    │
│  │                                      │    │
│  │  HTTP Server :8090                   │    │
│  │  ├─ GET  /         → HTML 页面       │    │
│  │  ├─ GET  /api/state → 实时状态 JSON  │    │
│  │  ├─ GET  /api/signals → 信号列表     │    │
│  │  ├─ GET  /api/snapshots → 当前周期   │    │
│  │  ├─ GET  /api/histrange → 历史振幅   │    │
│  │  └─ GET  /api/config → 当前配置      │    │
│  └──────────────────────────────────────┘    │
└──────────────────────────────────────────────┘
```

### 设计原则

- **零阻塞**: HTTP server 在独立 goroutine 中运行，不影响主交易循环
- **只读**: Dashboard 只读取状态，不修改任何引擎/采集器数据
- **可选启用**: 通过 `-dashboard :8090` flag 控制，默认关闭
- **嵌入式前端**: 单个 HTML 文件内嵌 CSS/JS，无构建步骤，CDN 加载 Chart.js
- **无外部依赖**: 仅使用 `net/http` 标准库 + 已有的 `encoding/json`

---

## 2. 新增文件结构

```
internal/dashboard/
├── server.go          # HTTP server 启动 + 路由注册
├── handlers.go        # API handler 函数
├── state.go           # DashboardState 聚合结构体 + 线程安全读取
└── templates.go       # 内嵌 HTML 模板 (embed 单文件)

cmd/flip/main.go        # 添加 -dashboard flag + 启动 server
internal/flip/engine.go # 新增少量 getter 方法
```

### 不修改的文件

- `internal/flip/types.go` — FlipSignal 已有所需的 JSON tag
- `internal/flip/recorder.go` — 无需修改
- `internal/flip/features.go` / `scoring.go` — 纯计算层不涉及
- `internal/lab/` — ResearchSnapshot 已有 JSON tag

---

## 3. 需要新增的 Engine Getter 方法

Engine 的内部字段均为 unexported，需要暴露以下只读接口：

```go
// internal/flip/engine.go 新增方法

func (e *Engine) State() flipState           // 当前状态机状态
func (e *Engine) Generation() int64          // 当前周期编号
func (e *Engine) SnapCount() int             // 已采集快照数
func (e *Engine) CurrentSide() string        // 当前检测的 side（confirming 时）
func (e *Engine) CrossSnap() *lab.ResearchSnapshot // 穿越时刻快照
func (e *Engine) T0Features() *T0Features    // T=0 特征快照
```

`T0Features` 是一个新的只读结构体，聚合穿越时刻计算的中间特征：

```go
type T0Features struct {
    PathEff        float64
    NoiseRatio     float64
    Flips          int
    Oscillating    bool
    RangeExpansion float64
    BTCPosition    float64
    BTCExtreme     bool
}
```

**变更量**: engine.go 新增约 30 行 getter + T0Features 类型定义。

---

## 4. API 设计

### 4.1 `GET /` — Dashboard HTML

返回完整的单页 HTML 看板，内嵌 CSS + JS。使用 `embed` 嵌入模板文件。

### 4.2 `GET /api/state` — 实时状态

```json
{
  "ts": "2026-08-07T14:35:42Z",
  "generation": 42,
  "engine_state": "watching",
  "engine_state_label": "Watching",
  "current_side": "",
  "snap_count": 23,
  "remaining_sec": 185,
  "condition_id": "0xabc...",
  "market_slug": "btc-updown-5m-1754563200",
  "open_price": 87100.50,
  "current_price": 87234.00,
  "yes_price": 0.5200,
  "no_price": 0.4800,
  "buy_vol_5s": 1.23,
  "sell_vol_5s": 0.89,
  "bid_depth": 45.2,
  "ask_depth": 38.7,
  "hist_ready": true,
  "hist_avg_range": 156.23,
  "hist_window_n": 18,
  "signal_count": 34,
  "won_count": 18,
  "win_rate": 0.529,
  "cumulative_pnl": 10.09,
  "profit_factor": 2.7,
  "cross_features": null
}
```

当引擎处于 `confirming` 状态时，`cross_features` 包含 T0 特征：

```json
{
  "cross_features": {
    "side": "yes",
    "path_eff": 0.65,
    "noise_ratio": 1.8,
    "flips": 3,
    "is_oscillating": true,
    "range_expansion": 0.35,
    "btc_position": -0.15,
    "btc_extreme": true,
    "other_delta": 0.03,
    "entry_price": 0.22,
    "confirm_ticks_waited": 1
  }
}
```

### 4.3 `GET /api/signals?limit=50` — 信号历史

```json
{
  "signals": [
    {
      "time": "2026-08-07T14:30:15Z",
      "condition_id": "0xabc...",
      "side": "yes",
      "score": 7,
      "entry_price": 0.18,
      "shares": 1,
      "remaining_sec": 125,
      "path_eff": 0.55,
      "noise_ratio": 2.1,
      "flips": 4,
      "is_oscillating": true,
      "range_expansion": 0.30,
      "btc_position": -0.20,
      "btc_extreme": true,
      "other_delta": 0.04,
      "won": true,
      "pnl": 0.8200
    }
  ],
  "total": 34,
  "won": 18,
  "lost": 14,
  "pending": 2,
  "win_rate": 0.5625,
  "cumulative_pnl": 10.09,
  "profit_factor": 2.7
}
```

信号来源：从 FlipRecorder 的内存 pending map 中获取已解析信号 + 定期从 JSONL 文件回读历史。

### 4.4 `GET /api/snapshots` — 当前周期快照

```json
{
  "condition_id": "0xabc...",
  "snapshots": [
    {
      "ts": 1754563200000,
      "remaining_sec": 295,
      "open": 87100.50,
      "price": 87101.20,
      "ret_10s": 0.000008,
      "buy_vol_5s": 0.50,
      "sell_vol_5s": 0.30,
      "signed_flow_5s": 0.20,
      "vol_10s": 0.0001,
      "vol_30s": 0.0003,
      "bid_depth": 45.0,
      "ask_depth": 38.0,
      "yes_price": 0.5100,
      "no_price": 0.4900
    }
  ],
  "count": 23
}
```

返回当前周期所有已采集的快照，用于前端绘制 BTC/YES/NO 价格曲线。

### 4.5 `GET /api/histrange` — 历史振幅

```json
{
  "ready": true,
  "window_n": 18,
  "avg_range": 156.23,
  "ranges": [120.5, 145.2, 189.3, ...],
  "count": 18
}
```

### 4.6 `GET /api/config` — 当前配置

```json
{
  "trigger_threshold": 0.7,
  "min_pre_snaps": 5,
  "max_remaining_sec": 260,
  "confirm_delay_ticks": 1,
  "score_entry": 5,
  "score_add": 99,
  "path_eff_oscillating": 0.8,
  "noise_ratio_oscillating": 1.5,
  "flips_oscillating": 1,
  "range_exp_threshold": 0.5,
  "range_exp_max": 2.0,
  "other_delta_vstrong": 0.05,
  "other_delta_strong": 0.02,
  "other_delta_weak": 0.01,
  "btc_pos_max": 0.1,
  "btc_pos_min": -0.1,
  "entry_cheap_strong": 0.20,
  "entry_cheap_weak": 0.25
}
```

---

## 5. 前端看板设计

### 5.1 布局

```
┌──────────────────────────────────────────────────────────────────┐
│  🔄 Flip Signal Detection Dashboard             BTCUSDT 5m       │
│  Cycle #42 | 2026-08-07 14:35:42 UTC | refresh: 2s             │
├──────────┬──────────┬──────────┬──────────┬──────────┬──────────┤
│ Signals  │  Won     │  Lost    │  WinRate │  P&L     │  PF      │
│   34     │   18     │   14     │  52.9%   │ +10.09   │  2.70    │
├──────────┴──────────┴──────────┴──────────┴──────────┴──────────┤
│                                                       full width│
├─────────────────────────────┬────────────────────────────────────┤
│ Engine State                │ Live Prices                        │
│ ┌─────────────────────┐     │ BTC:  $87,234.00  ▲ +133.50       │
│ │  State: Watching    │     │ Open: $87,100.50                   │
│ │  Snaps: 23          │     │ YES:  0.5200                       │
│ │  Remaining: 185s    │     │ NO:   0.4800                       │
│ │  Hist Avg Range:    │     │                                    │
│ │    $156.23 (ready)  │     │ Volume 5s: B 1.23 / S 0.89        │
│ │  Side: -            │     │ Depth: Bid 45.2 / Ask 38.7        │
│ └─────────────────────┘     │                                    │
├─────────────────────────────┴────────────────────────────────────┤
│ Cross Features (only when confirming/signaled)                   │
│ PathEff  NoiseRatio  Flips  Oscillating  RangeExp  BTCPos  OthΔ │
│  0.65      1.8         3       ✓          0.35     -0.15   0.03 │
├──────────────────────────────────────────────────────────────────┤
│                                                       full width│
│ Price Chart — BTC + YES/NO (dual Y-axis)                        │
│ ┌──────────────────────────────────────────────────────────────┐ │
│ │  ● BTC (L)  ● YES (R)  ● NO (R)  ─ open  ─ 0.7 trigger     │ │
│ │  ████████████████████████████████████████████████████████████ │ │
│ │  ████████████████████████████████████████████████████████████ │ │
│ │  ████████████████████████████████████████████████████████████ │ │
│ │  ████████████████████████████████████████████████████████████ │ │
│ └──────────────────────────────────────────────────────────────┘ │
├──────────────────────────────────────────────────────────────────┤
│ Signal History (last 30)                                         │
│ Time         Side  Score  Entry  Shrs  PathEff  Noise  OthΔ  W/L│
│ 14:30:15     YES     7   0.18     1    0.55    2.1   0.04  🟢  │
│ 14:20:50     NO      5   0.22     1    0.70    1.6   0.02  🔴  │
│ 14:15:10     YES     8   0.15     1    0.48    3.2   0.06  🟢  │
│ ...                                                              │
└──────────────────────────────────────────────────────────────────┘
```

### 5.2 技术选型

- **Chart.js v4** (CDN) — 轻量、零依赖、canvas 渲染
- **Vanilla JS** — 无框架，`fetch` + `setInterval` 轮询
- **CSS Grid** 布局
- **等宽字体** (JetBrains Mono / system monospace) 用于数字显示
- **暗色主题** — 适合长时间监控

### 5.3 轮询策略

- `/api/state` — 每 2 秒
- `/api/snapshots` — 每 5 秒 (仅在 watching/confirming 状态时)
- `/api/signals` — 每 10 秒
- `/api/histrange` — 每 30 秒
- Charts 增量更新 — 仅追加新数据点

### 5.4 状态指示

| 状态 | 颜色 | 说明 |
|------|------|------|
| `watching` | 🟡 黄色 | 等待 YES/NO 穿越 0.7 |
| `confirming` | 🔵 蓝色 | 穿越后等待 T+5s 确认 |
| `done` | 🟢 绿色 | 已产生信号（等待下一周期） |
| `idle` | ⚪ 灰色 | 等待 Reset |

---

## 6. 实现步骤

### Step 1: Engine Getter 方法 (`internal/flip/engine.go`)

新增 `T0Features` 结构体 + 6 个 getter 方法。约 30 行代码。

### Step 2: Dashboard 包 (`internal/dashboard/`)

| 文件 | 职责 | 规模 |
|------|------|------|
| `state.go` | `DashboardState` 聚合体，组合 Engine/Collector/HistRange/Recorder 引用 | ~40 行 |
| `server.go` | HTTP server 启动、路由、中间件（CORS/logging） | ~60 行 |
| `handlers.go` | 6 个 JSON API handler | ~150 行 |
| `templates.go` | `embed` 嵌入 `dashboard.html` + `templateHandler` | ~20 行 |
| `dashboard.html` | 完整单页看板 (HTML+CSS+JS) | ~400 行 |

### Step 3: main.go 集成

```go
// 新增 flag
dashboardAddr := flag.String("dashboard", "", "HTTP dashboard address (e.g. :8090)")

// 在 market loop 启动前
if *dashboardAddr != "" {
    dash := dashboard.New(collector, flipEngine, histTracker, flipRecorder)
    go dash.ListenAndServe(*dashboardAddr)
    log.Printf("[Dashboard] http://localhost%s", *dashboardAddr)
}
```

### Step 4: 可选 — lab 数据读取

当 `-lab-output` 启用时，dashboard 也可以列出历史 event 文件供回顾（后续迭代）。

---

## 7. 线程安全

| 数据 | 读取方式 | 保护 |
|------|---------|------|
| Engine state/features | Getter 方法 | 无锁 — 所有字段在单 goroutine 中修改，HTTP goroutine 只读，Go memory model 保证最终一致性 |
| Collector snapshots | 直接读取切片 | 同一 goroutine 写，HTTP 只读 — 安全 |
| HistRangeTracker | Getter 方法 | 已有 `sync.RWMutex` |
| FlipRecorder pending | 需要新增方法 | 已有 `sync.Mutex`，新增 `PendingSignals()` 方法加锁拷贝 |
| JSONL 信号历史 | 需要新增读取方法 | 加锁读取文件或从 pending map 获取 |

### 关于 Engine 无锁读取的说明

Engine 的所有字段修改发生在 `ProcessSnapshot()` 中，该函数仅在 market loop 的单个 goroutine 中调用。HTTP handler 在另一个 goroutine 中只读这些字段。Go 的 goroutine 调度保证写入最终可见。对于 dashboard 这种展示用途，读取到略微过时的值是完全可以接受的。

---

## 8. 使用方式

```bash
# 启动 flip 引擎 + dashboard
go run ./cmd/flip -dashboard :8090

# 同时输出 lab 数据
go run ./cmd/flip -dashboard :8090 -lab-output data/lab

# 浏览器打开
open http://localhost:8090
```

---

## 9. 不做什么

- ❌ 不添加 WebSocket / SSE — 轮询对于 2s 刷新间隔足够，避免复杂度
- ❌ 不做历史回放 — dashboard 只看当前运行态
- ❌ 不支持多实例 — 一个 flip 进程对应一个 dashboard
- ❌ 不做告警/通知 — 纯可视化
- ❌ 不引入前端构建工具链 — 单文件 HTML 足够
- ❌ 不引入第三方 HTTP 路由库 — `net/http` 足够

---

## 10. 后续迭代方向

1. **信号详情页**: 点击某条信号，展开该周期的完整 BTC/YES/NO 走势图
2. **历史回放**: 从 JSONL file 读取历史 event，逐周期回放图表
3. **暗色/亮色主题切换**
4. **配置热更新**: 通过 dashboard 修改阈值并实时生效
5. **Export 按钮**: 导出当前信号列表为 CSV

---

## 11. 依赖分析

| 依赖 | 是否新增 | 说明 |
|------|---------|------|
| `net/http` | 否 | 标准库 |
| `embed` | 否 | 标准库 (Go 1.16+) |
| `encoding/json` | 否 | 标准库 |
| Chart.js | CDN | 前端，无后端依赖 |

**零新增 Go 依赖。**

---

> **确认后实施。预估工作量: ~600 行 Go + ~400 行 HTML/CSS/JS。**
