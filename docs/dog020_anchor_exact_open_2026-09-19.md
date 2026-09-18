# 锚 = 边界那一秒的 TWAP 推送（精确取锚，2026-09-19）

**一句话**：锚（结算线口径）只认 TWAP 推送里**评估时刻恰好等于窗口边界**的那一条——它与官方
`openPrice` 收敛值逐位相同；窗口开局**不设过渡锚**，取锚通道按 500ms × 40（20s）重试，取不到
则本窗不产出观测；官方 HTTP 路径**休眠**（+40s 才收敛，本窗机会早过）。

**性质**：口径修正 + 观测口径简化，**不是 P&L 杠杆**。btreplay 625 笔逐位不变（回测路径未动）。

---

## 1. 为什么改

| 事实 | 证据 |
|------|------|
| 「评估时刻 == 边界」的推送 **就是**官方 openPrice | 独立探针 8/8 窗与官方收敛值逐位相同，差 0.0002–0.0006bps（≈3 厘美元）；上线后 3 窗再验同样逐位相同（09-18 doc §9.1） |
| 该条推送到达时刻 | p50 **+2.0s**（探针 8 窗：+1760/+1761/+2008/+2009/+2009/+2011/+2161/+2258ms；旧引擎 20 窗 +1996~+2020ms；新引擎 +2000/+2008/+2002/+3001ms），最晚观测到 **+12.1s** |
| 官方 HTTP open | **+40s 才收敛**；头几十秒是临时值（同窗 +2.8s 与 +10.3s 同为 80721.68、+40.4s 才跳到 80722.35），17 窗配对 13/17 不等、\|差\| p90 **1.18bps ≈ 9.5 美元**——比它要修的 t=0 误差还大 |
| 本策略对时间的要求 | 信号全部落在窗口前 2 分钟（`rem > 180`）；回测 625 笔里最早一笔 `rem=295`（边界后 5s）、`rem≥290` 仅 2 笔 |

⇒ 09-18 的「`Latest()` 初值 + 按评估时刻重选 + 官方收敛点覆盖」三件套里：
- `Latest()` 初值是**近似**（发布延迟 ~2s，t=0 只能看到「边界**前**一秒」的评估值，与官方差 p90 0.24bps）；
- 官方覆盖**太慢**（+40s），对 5 分钟窗口是废的。

既然精确那一条就是结算线，就只留精确那一条。

## 2. 改了什么

| 位置 | 变化 |
|------|------|
| `feed.TwapAdapter.PushNearest` | **精确等值匹配**（`tsMs == windowStart.UnixMilli()`），删容差/删「取最近」；返回 `(price, arrivedMs, ok)`；同一评估时刻多条取**后到者**（反序扫描首个命中） |
| `feed.TwapAdapter.CacheStat` | 新增诊断快照 `(条数, 最新一条评估偏移, 缺时间戳条数)`，供失败日志归因 |
| `feed.TwapAdapter.consume` | `payload.timestamp <= 0` 的推送**不再入缓存**（精确口径下按到达时刻兜底的条目永远命不中，只会污染诊断）；`Latest()` 照常刷新（看门狗/dashboard 用） |
| `feed.RecoverAnchor` | 改为两段：① **精确取锚**（立即试一次 + 每 500ms 一次 × 40 = 20s，命中即终局并关通道）；② **官方段**（到点采样 / 收敛门 / last-wins **原样保留**，但 `Schedule` 为空即整段跳过——**生产默认休眠**） |
| `cmd/flip/main.go` | 删 `Latest()` 取锚 + 新鲜度守卫 + seed 单调门 + `NewOpenPriceFetcher` 接线；`BeginWindow(0, 0)` 开局；取锚 goroutine 命中即 `UpgradeAnchor(price, hist.Bps(price))` 并打日志；预算耗尽打一行缓存诊断 |
| `internal/flip` | 删 `AnchorUsableAtBoundary`（唯一调用点消失）；winstats 删 `anchor_init`/`anchor_pick_ms`、增 **`anchor_exact`**（不带 omitempty，兼作 python 侧新旧行哨兵键） |
| `python/v4/07_source_health_check.py` | A2 段重写为**命中率 + 到达延迟分位数 + 未命中窗清单**；A 段「锚缺」列改判「**期末**仍无锚」（新行看 `anchor_exact`，老行回退 `anchor_missing`——否则每窗开头 2s 的短暂待命中会把该列刷成 ~100%） |
| dashboard | `anchor_missing` 文案改为「锚待精确命中（边界推送 p50 +2.0s 到达），本窗判定暂缓」——每窗开头都会短暂出现，**不是异常** |

**保持不动的红线**：引擎 `decide` 判定链 / `BeginWindow`/`UpgradeAnchor`/`WindowAnchor` 语义 /
btreplay / `01_backtest_r1.py` / 默认配置值。锚 ≤0 的既有路径（只占槽、闸住触发判定、不产出观测、
不计入 σ）正是本次要用的路径，**未加任何新闸**。

## 3. 不设过渡锚的代价（用户拍板）

- 锚未到手期间（正常 ~2s，最坏 20s）：tick 照常占槽与计数、`lost_triggers(anchor_pending)` 留痕，
  但**触发判定被闸**——历史 625 笔里最早信号在 `rem=295`（+5s），**正常路径零损失**。
- 20s 预算耗尽 = 本窗 anchor 恒 0：不产出观测、不计入 σ（`windows_*` 缺一行，被 `RecentBlock`
  的 600s 缺口容差吸收；连续 ≥2 窗丢失才会被误判成断档）。
  **明确取舍**：宁可丢窗，也不拿近似锚判定出信号。
- 引擎 `stats.AnchorMissing` 语义随之变化：**每窗开头都会短暂为真**，期末仍为真才是「整窗无锚」。

## 4. 可见性

`winstats_*.jsonl`（每窗一行）：

| 字段 | 含义 |
|------|------|
| `anchor_exact` | 本窗锚是否精确命中边界那一秒（**不带 omitempty**；false = 本窗无锚、不产出观测） |
| `anchor_src` | `stream`（TWAP 推送；官方段休眠时唯一取值）/ `official` / 空（无锚） |
| `anchor_recovered_ms` | 该推送的**本地到达时刻**距窗口边界——发布延迟的**无偏**观测（不含取锚轮询的 500ms 相位；旧口径取轮询时刻，会把 p50 2010ms 读成 2500ms） |

日志：命中一行 `[Anchor] ✅ 窗口 … 锚 …（stream, σ=…; 边界后 +2000ms）`；失败一行
`[Anchor] ⚠️ 窗口 … 40 次 × 500ms 未取到边界那一秒的 open（缓存 N 条, 最新一条评估偏移 ±Xms,
缺时间戳 M 条）, 本窗不产出观测`——缓存快照直接区分**服务器没发 / 我们收晚了 / 时间戳缺失**。

## 5. 验收

1. `go test ./internal/... -race` 全绿；`go build ./...`。
2. `go run ./cmd/btreplay` → **625 笔逐位一致**（`ok n=625 WR 24.6% EV +0.633U/注 P&L +395.8U`）。
3. `python/v4/01_backtest_r1.py` 四条基线不变。
4. 纸面实跑 ≥12 窗：`anchor_exact` 命中率 **≥99%**、`anchor_recovered_ms` **p90 ≤ 3000ms**；
   主循环 tick 不因取锚停顿（`[Event] rem=` 每秒一条、无跳秒）。
5. ⚠️ `cmd/btreplay` 只喂 `BeginWindow(ev.TwapOpen, σ)`，不碰 `internal/feed`——锚路径改动**不可能**
   影响回测对账（= 红线可证）。

## 6. 未做 / 已知

- **官方 HTTP 路径保留但休眠**：`NewOpenPriceFetcher` / `OpenPriceFetcher` / `fetchWithTimeout` /
  `SettleAfter` 全在 `internal/feed/anchor_recover.go`，恢复只需在 `cmd/flip` 接回 fetcher 并给
  `AnchorUpgradeOpts` 填 `Schedule`。回归测试 `TestRecoverAnchorOfficialDormant` 钉住「空 Schedule →
  零请求」。
- σ 的 close 口径仍取 `lastTick.TwapPrice`（**到达**口径，与 open 的评估口径有 ~1.5s 不对称）——已知未做。
- `twapPushCap = 100`（≈100s）必须覆盖取锚预算（20s）+ 余量；拉长预算时同步放大。
