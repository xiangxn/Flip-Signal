# 锚缺失恢复：官方开盘价 3×20s 重试 + 边界窄窗口推送直采（2026-09-16）

> 需求来源（2026-09-16 用户提出，逐字）：
> **「当出现"锚缺失，本窗不观测"时，可以尝试用 sdk.PolymarketClient 的 FetchOpenPrice
> 拉取 3 次，间隔 20s。同时等 twap 的推送。3 次失败就不观测了」**
>
> **状态：已实施（2026-09-16 当日完成）。** 两个设计口径当日拍板：
> ① 取值 = **官方优先 + 窄推送**；② 恢复期 tick = **喂 ring 只闸观测**。
> 验收红线不变：`cmd/btreplay` 625 笔逐位一致（`ok n=625 WR 24.6% EV +0.633U/注
> P&L +395.8U side map[no:339 yes:286]`），`go test ./internal/...` 全绿（新增 8 用例，
> 含 `-race`）。另做了 3 个窗口的真机纸面冒烟（§9），并顺带撞出两个**不属于本改动**
> 的既有缺陷（`skip=late` 重复行 / 冷启动首窗 σ=0）。
>
> 本文只记录**落地后的口径与实测**，不含待办方案；推演期草稿（10s 宽限期取值、
> `abs` 判据等）已并入正文。

---

## 0. 结论速览（TL;DR）

| 项 | 改前 | 改后 | 依据 |
|------|------|------|------|
| 锚缺失窗口 | 整窗丢 tick、零观测、**零痕迹** | 起恢复通道（官方 3 次 × 20s + 推送直采）；期间 tick 照常占槽计数、**只闸住触发判定** | 边界一次推送抖动白丢 5 分钟窗口，而触发截止在 `rem>180`（边界后 ~120s） |
| 取值来源 | — | **官方 `FetchOpenPrice` 优先**；推送仅当 `\|到达时刻 − 边界\| ≤ 10s`（`feed.max_twap_age_ms`）时直采 | 官方 open 与「边界正常采样」口径差 p50 **0.056bps ≈ 0.006σ**；而迟到 30s 的推送 p50 0.14σ / p90 0.41σ（§2.2 实测表） |
| 兜底 | `anchor≤0` → 本窗不观测 | **不变**（3 次全失败仍不观测） | `06_oos_review.py:161-167` 对 anchor/hist_bps/fill 为 0 的硬检查 = 本项目红线 |
| 可观测性 | 无（连窗口被跳过都看不出来） | `winstats_*` 增 `anchor_src` / `anchor_recovered_ms`；失败窗 `anchor_missing=true`；触底留痕 `lost_triggers.reason=anchor_pending`；dashboard 文案区分 | 「本窗为什么没信号」必须可查（沿用 09-16 延迟闸文档 §2.4 的可见性口径） |
| 同类口子·σ 冷启动窗（§9.2，同日补丁） | 整窗 σ=0 → 触底恒 `no_hist`（必然零信号）且落 `hist_bps=0` 观测，踩 06 硬检查 | `hist.Count() < flip.HistMin` → **整窗跳过**，落 `skip=no_sigma` 的 winstats 行 | 与锚缺失同一条原则：数据源不可信 → 本窗不观测（决策 #13） |
| 同类口子·winstats 重复行（§9.1，同日补丁） | 每窗多一行假 `skip=late`（**同 `event_start` 两行**），07 的逐日行数翻倍、跳过列全假 | 步骤 1 迟到分支按**上一窗身份**（`prevWindowStart`）静默顺延，不告警不落行 | 收尾 `rem = int()` 截断使循环总在边界前 0~1s 回来，`floor(now/300)` 回指刚跑完的那一窗（生产实测 82/82 窗全中） |

**一句话**：这是**保险不是修复**——实测频率低（§6：paper 12 天里锚缺失槽位占比 ≤0.57%，
且全部落在 ≥10min 的断档里），但此前这类窗口是**纯损失且不可见**，救回来几乎零成本
（官方接口在触发截止前 ~100s 就能拿到真值）。

---

## 1. 什么算「锚缺失」，为什么会丢整窗

窗口边界（`nextStart`）采样 Chainlink TWAP-60 流值当锚，附一道新鲜度守卫
（[cmd/flip/main.go:528-540](../cmd/flip/main.go#L528-L540)）：

```go
anchor, anchorAgeMs := twapAdapter.Latest()
anchorUsable := flip.AnchorUsableAtBoundary(anchor, anchorAgeMs, cfg.Feed.MaxTwapAgeMs)
if !anchorUsable { ...; anchor = 0 }   // anchor=0 即「锚缺失」
```

`AnchorUsableAtBoundary`（[internal/flip/sigma.go:91](../internal/flip/sigma.go#L91)）=
`price > 0 && ageMs <= freshMs`，`freshMs = feed.max_twap_age_ms = 10000`。
**两种情况触发**：① 推送断流/订阅重建间隙，边界瞬间无值；② 有值但龄 > 10s（陈旧流值当锚
会把整窗 `dist_s` 算歪，比不要这个窗口更糟）。

改前：`anchor=0` → 引擎 `ProcessTick` 首个分支直接 `return nil`（整窗不观测），
tick 连槽位都不占。窗口白丢，磁盘上**零痕迹**。

改后：`anchor=0` 只在**触发判定**这一层生效（§2.4），窗口级 ctx 起一条恢复通道（§2.1）。

**时间预算**：触发时间腿是 `rem > 180`，即边界后 ~120s 内触底才算。官方开放价通常在
边界后几十秒内就绪，恢复最多等 `3×20s = 40s`（实际更早：第 3 次失败即返回）——
离触发截止仍有 ~80s 余量，救回来的窗口**和正常窗口一样能出信号**。

---

## 2. 规则

### 2.1 取值优先级：官方优先 + 窄窗口推送（用户拍板）

```
边界锚缺失
   │
   ├─ 推送直采: |推送到达时刻 − 边界| ≤ max_twap_age_ms(10s)  → 立即采纳（source=push）
   │
   └─ 官方: 边界 +0s / +20s / +40s 各拉一次 openPrice（每次 10s 超时）
            ├─ 任一次成功 → 采纳（source=official），清 AnchorMissing，本窗照常判定
            └─ 3 次全失败 → 本窗不观测（保持 anchor=0）
```

官方 `FetchOpenPriceContext` 的 `openPrice` **就是窗口边界真值**（回测 anchor 同源口径），
实测与「边界后首个 tick 的流值」差 p50 0.056bps / p90 0.41bps（3753 个回测窗口逐窗核验）
——用它回填锚，`dist_s` 与「边界正常采样」路径的偏差在 0.01σ 量级，**几乎不可见**。

### 2.2 为什么推送只做「窄窗口」（实测依据）

TWAP-60 是**滚动**值：边界后 L 秒的推送值 ≈ 锚 + 边界以来 L 秒的位移。把它当锚，
等于给 `dist_s` 叠一个系统性偏移。用 14 天回测（`data/btc`，3753 窗，逐步 tick 的
`twap.price` 1s 分辨率）实测「边界后 L 秒的流值 vs 官方 open」在 **σ 单位**下的偏移：

| 边界后滞后 L | p50 | p75 | p90 | p95 | \|偏移\|>0.1σ | >0.2σ | >0.3σ | >0.5σ |
|---|---|---|---|---|---|---|---|---|
| 5s | 0.021σ | 0.045σ | 0.083σ | 0.114σ | 6.6% | 1.0% | 0.4% | 0.1% |
| **10s（采用上限）** | **0.048σ** | **0.093σ** | **0.155σ** | **0.199σ** | **22.1%** | **4.9%** | **1.4%** | **0.2%** |
| 20s | 0.096σ | 0.180σ | 0.287σ | 0.373σ | 48.1% | 21.0% | 9.1% | 2.2% |
| 30s | 0.143σ | 0.261σ | 0.412σ | 0.559σ | 62.0% | 35.5% | 20.0% | 6.3% |
| 60s | 0.263σ | 0.496σ | 0.779σ | 0.992σ | 77.9% | 59.6% | 44.7% | 24.7% |

> 口径：σ = 该窗前 ≤18 个已完成窗口 `|twap_close − twap_open|` 均值（镜像 btreplay 的
> `histBps`，≥3 窗）；偏移 = `|twap(边界+L) − 官方open| / anchor × 1e4 / σ`。
> 随机游走标度 √(L/300) 给 30s → 0.316σ，与实测 p50 0.14σ / p90 0.41σ 同量级（趋势项抬尾）。

**10s 的取舍**：浅洞带边界（`0` 与 `lo(side)`）是锐利的——`dist_s` 平移 0.15σ 会让
边界附近的一撮样本换边（±1 个触底侧的进出都算）。10s 处 p90 = 0.155σ，与本策略
**既有的**边界容差同量级；再放宽到 30s，p90 到 0.41σ、20% 的窗口偏移 >0.3σ，
足以把带外样本搬进带内（正是 09-10 文档「dist_s 是基差腿」那一族的失真形态）。
**宁可少一个窗口，也不要一个偏锚的信号**——本项目一贯的「不可信数据源不产生信号」闸语义。

**与边界守卫对称**：`max_twap_age_ms` 同时是边界采样的新鲜度阈值与推送宽限，
所以恢复的推送路径**只会采纳边界守卫本来就会接受的那类推送**（到达时刻距边界 ≤10s），
外加边界后 0~10s 新到的那部分（边界当刻还看不到的新信息）。一个参数两个用途，
不会出现「守卫认为陈旧、恢复却采纳」的自相矛盾。

### 2.3 `abs` 判据（必须取绝对值）

```go
diff := atMs - boundaryMs
if diff < 0 { diff = -diff }     // ← 关键
if diff > graceMs { return 0, 0, false }
```

触发锚缺失的那条推送**就是边界前发出的**（典型形态：边界前 30s 到达、龄超阈值）。
若只判 `atMs - boundaryMs <= grace`，这条老推送（负值恒满足）会在恢复循环第一轮被
立刻误判成「恢复成功」，把整窗锚钉在 30s 前的陈旧值上——比不恢复更糟。
`abs` 同时把「恰在边界前 ±10s 内到达」的合法推送纳入（与 `AnchorUsableAtBoundary`
同容差），是同一容差的两种表述。

### 2.4 恢复期的 tick：喂 ring，只闸观测（用户拍板）

锚未就绪期间 `ProcessTick` **照常**走完分类与计数（[internal/flip/engine.go:188-217](../internal/flip/engine.go#L188-L217)）：

- 无效 tick 仍压 0 占槽（延迟超阈 / 整簿缺失），与 `anchor>0` 路径逐行同构；
- 有效 tick 走 `pushSlots` **占槽但不触发判定**——`dist_s` 无锚算不出来，强判即失真；
- 该 tick 若触底（`0 < ask ≤ 0.20`）逐 tick 记 `lost_triggers.reason = "anchor_pending"` 留痕；
  **不重复**记 `stale_book` / `book_missing`（一次 tick 只留一条痕迹，原因取最根本的那层）。

**为什么照常占槽**：`m_45` 急跌腿读的是最近 45 个 tick 槽位的同侧 ask max。回测里锚
**从不缺失**（§5.1 实测：3753 个回测窗口 open/close 全齐），所以回测的 `m_45` 永远是
「满 45 槽真实盘口」。若恢复期吃槽为 0，锚回填后引擎看到的 `m_45` 会出现**真实性缺口**——
本该 ≥0.40 的急跌被算成无急跌，信号口径与回测分叉。占槽使 tick 序列与回测 1:1。

**为什么不追溯**：恢复前的触底发生在锚未知时，用它去判定等于用「当时并不知道 dist_s 的
一刻」下注（延迟 20~40s 的 ask 也可能已经走掉）。这与既有结构一致——无效 tick 不触发、
**其后第一条有效 tick 才算首触**。留痕只用于回答「恢复期到底丢了多少触底」。

`SetAnchor`（[:131](../internal/flip/engine.go#L131)）幂等（已设不覆盖）、成功即清
`stats.AnchorMissing`。`AnchorMissing` 的**新语义 = 窗口结束时仍未恢复**
（pending 期间 true，回填即 false）——落盘的 `anchor_missing:true` 准确对应
「本窗因锚缺失没有产出任何观测」。

### 2.5 兜底：3 次失败 = 本窗不观测

尝试次数用尽**立即返回**，不空等下一次（此时任何推送早已超出 10s 窄窗口，再等只能拿到
偏锚值）。未恢复的窗口保持 `anchor=0`：引擎不产出任何观测行 —— 观测行 `anchor` 恒 >0，
`06_oos_review.py:161-167` 的硬检查永不见异常；σ 也不计入该窗（与回测 :69 锚缺失事件跳过同构）。

---

## 3. 实现映射

| 位置 | 内容 |
|------|------|
| [internal/feed/anchor_recover.go](../internal/feed/anchor_recover.go)（新） | `RecoverAnchor(ctx, fetch, push, windowStart, opts)`：1s 粒度轮询推送 + 到点发官方请求；`AnchorRecoverOpts{Attempts, Interval, PushGrace, FetchTimeout}`；注入式 fetcher/push（零网络可测） |
| 同文件 | 每次官方请求带独立 `FetchTimeout` 子 ctx——SDK 内部 429 `Retry-After` 退避可阻塞数分钟（v3 排障记录实测把写盘拖了 ~5 分钟），不设超时会把重试节奏拖死 |
| [internal/feed/twap_adapter.go](../internal/feed/twap_adapter.go) | 新增 `LatestStamped() (price, arrivedAtMs)`（复用 `lastUpdateAt`；`Latest()` 不动）——恢复要判「到达时刻离边界多近」，而不是「现在多旧」 |
| [internal/flip/engine.go](../internal/flip/engine.go) | `SetAnchor` / `WindowAnchor` / `LostReasonAnchorPending` / `lostReason(anchorPending, qualityReason)`；`ProcessTick` 的 `anchor≤0` 分支重构（§2.4），`anchor>0` 路径**逐字节不动** |
| [cmd/flip/main.go:82-90](../cmd/flip/main.go#L82-L90) | 常量 `anchorRecoverAttempts=3` / `anchorRecoverInterval=20s` / `anchorRecoverFetchTimeout=10s`（与 `prefetchLead` / `lateLimit` / `twapLookbackSeconds` 同为 main 常量，**暂不配置化**）；`PushGrace` 复用 `cfg.Feed.MaxTwapAgeMs` |
| [cmd/flip/main.go:547-592](../cmd/flip/main.go#L547-L592) | 恢复通道接线：`context.WithCancel(ctx)` 窗口级 ctx + goroutine；成功 `engine.SetAnchor` + 记 `anchorRecovery{src, atMs}` + 日志；失败（且 ctx 未取消）打「官方 3 次均未就绪，本窗不观测」 |
| [cmd/flip/main.go:647-654](../cmd/flip/main.go#L647-L654) | 窗口收尾 `cancel() + <-anchorDone`（channel close 建立 happens-before）→ 再读 `engine.WindowAnchor()` 供 σ 与落盘。**不 join 就存在数据竞争**（恢复 goroutine 可能正在 `SetAnchor`） |
| [internal/flip/recorder.go:112-116](../internal/flip/recorder.go#L112-L116) | `WindowStatsEntry` 增 `anchor_src`（official\|push）、`anchor_recovered_ms`（自边界起算，omitempty） |
| [internal/dashboard/static/app.js:161-175](../internal/dashboard/static/app.js#L161-L175) | `anchor_missing` 文案「锚缺失（恢复中），本窗暂不观测」；丢信号后缀区分 `anchor_pending (锚未就绪)` / `stale_book` / `无快照` |

**σ 计入规则**（[main.go:669-680](../cmd/flip/main.go#L669-L680)）：恢复成功的窗口
`anchor > 0` → **照常计入 σ**（官方开盘价比边界流值更贴回测口径，见 §2.1 的 0.006σ）；
只有始终未恢复才落 `anchor ≤ 0` 分支跳过。

---

## 4. 可见性字段

`winstats_YYYY-MM-DD.jsonl` 每窗一行（09-16 延迟闸文档 §2.4 的 schema），本次新增：

```json
{"ts":..., "kind":"winstats", "slug":"btc-updown-5m-...", "event_start":...,
 "anchor":69021.70, "hist_bps":6.35,
 "anchor_src":"official",           // 新增: 仅锚缺失窗口非空; official | push
 "anchor_recovered_ms":21300,       // 新增: 自窗口边界起算多久拿到锚
 "ticks":299, "ticks_valid":297, "book_stale":2, "book_missing":0,
 "lost_triggers":[{"ts":...,"side":"yes","rem":246,"ask":0.19,
                   "book_lat_ms":0,"reason":"anchor_pending"}]}
```

- 未恢复的窗口：`anchor_missing: true` + `anchor_src` 缺省 + `anchor_recovered_ms` 缺省，
  观测行数为 0。
> ⚠️ 「每窗一行」在当前主循环下**并非严格成立**：窗口收尾 tick 因 `rem` 取整落在边界前
> 几毫秒时，下一轮循环会把**刚跑完的窗口**再判一次 `skip=late`，多落一行同 `event_start`
> 的空行（实测 2/2 窗）。详见 §9.1——本改动不涉及该路径。

- `lost_triggers.reason` 现取值 `stale_book` / `book_missing` / **`anchor_pending`** 三种；
  恒等式 `ticks == ticks_valid + book_stale + book_missing` 在锚缺失窗口同样成立
  （待恢复期照常分类计数）。`python/v4/07_source_health_check.py` 按 reason 字符串自动分组，
  无需改动；`amiss` 列语义 = 「整窗未恢复」。

Dashboard：`anchor_missing` 期间「丢信号」栏显示「锚缺失（恢复中），本窗暂不观测」，
恢复成功后自动转为正常丢信号计数（`/api/state` 5s 轮询，`WindowStats()` 每 tick 重算）。

---

## 5. 与既有口径的边界

### 5.1 回测 1:1 红线（`cmd/btreplay` 625 笔）

- `anchor > 0` 的路径**逐字节未改**；恢复通道只在 `anchorUsable == false` 时启动，
  btreplay 不构造该通道。
- **回测数据完全不含锚缺失**：`data/btc/events_*.jsonl` 共 3753 个事件，
  `twap_open_price` / `twap_close_price` 缺值 **0 / 0**（逐文件核验）→ btreplay 里的
  `skipped["no_anchor"]` 分支在本数据集**不可达**，本改动天然中性。
- 验收实测：`go run ./cmd/btreplay` → `ok n=625 WR 24.6% EV +0.633U/注 P&L +395.8U
  side map[no:339 yes:286]`（与 09-13 基线条目一致）。

### 5.2 `06_oos_review.py` 硬检查

`06_oos_review.py:161-167` 对 `anchor` / `hist_bps` / `fill` 为 0 的行做硬检查（OOS 基线 = 0 异常）。
本改动**不产生任何新的 anchor=0 观测行**——恢复失败窗口沿用「整窗不观测」，
语义与改前完全等价。

### 5.3 覆盖前文表述

> ⚠️ **本文覆盖 `docs/dog020_risk_latency_plan_2026-09-16.md` §2.4 的
> 「另加 `AnchorMissing bool` 标记整窗不观测（锚缺失），此情形下计数恒 0」。**
> 新语义：锚缺失窗口**照常分类计数**（`ticks/ticks_valid/book_stale/book_missing`
> 与恒等式同正常窗口），`AnchorMissing` = **窗口结束时仍未恢复**；
> 触底留痕为 `anchor_pending`（不重复记 stale/missing）。
> 该文档其余部分（延迟闸、熔断线、`skip` 原因、`kind` 守卫、恒等式）不受影响。

### 5.4 未配置化

`3 次 / 20s / 10s 超时` 是 main 常量而非配置键（与 `prefetchLead` / `lateLimit` /
`twapLookbackSeconds` 同款）。理由：它们是**恢复通道的机械参数**，不是策略参数——
调它们不改变任何判定口径（`PushGrace` 已经由 `feed.max_twap_age_ms` 驱动，
因为那个阈值本身就是「TWAP 多旧算没用」的策略定义）；配置化的意义是「能调」，
而这里没有会想调的量。

---

## 6. 实测频率（这个口子有多大）

**paper 09-05~09-16**（`data/v4/windows_*.jsonl`，即 σ 预热文件——只有成功计入 σ 的窗口
才落行）：跨度 3003 个窗口槽位，实得 2986 行，缺 **17 槽 = 0.57%**，且 17 槽**全部落在
10 段 ≥10min 的时间戳断档里**（最长 20min，共 10 段）。该文件无法区分「进程停机」与
「连续多窗锚缺失/close 陈旧」——两者都表现为槽位空缺。**故 0.57% 是锚缺失的上界，
真实值待 `winstats_*`（本改动落地后才有数据）区分。**

量级参照（09-16 延迟闸文档 §1 的同类量化）：现货断流每日只丢 ≈0.14 笔（0.35% 信号量），
窗口级损失 0.57% × 每窗 ≈1 条观测 ≈ 0.12 笔信号期望 → **同日量级的小口子**。
**09-15 OOS 复验的频率缺口（36.5/日 vs 计划 44±5）归因仍是 `rem_low` 触底时点后移，
不是锚缺失**——本改动是止损保险，不是频率修复。

---

## 7. 测试

`go test ./internal/...` 全绿（feed 新增 6 + flip 新增 2 = **8 个新用例**，另有 2 个既有用例
随语义更新；全部零网络、毫秒级时长，`-race` 通过）：

| 文件 | 用例 | 钉住的语义 |
|------|------|------------|
| [internal/feed/anchor_recover_test.go](../internal/feed/anchor_recover_test.go) | `TestRecoverAnchorOfficialFirst` | 首次即成功 → 只发 1 次请求，不空跑剩余尝试 |
| 同 | `TestRecoverAnchorAllAttemptsFail` | 3 次全失败 → 恰 3 次调用、按 `i×Interval` 节奏（±40ms），第 3 次失败即刻返回 |
| 同 | `TestRecoverAnchorPushWithinGrace` | 表驱动 6 例：±2s / 恰在边界 / −3s 采纳；**−30s（陈旧推送）**/ +30s / +15s 拒绝 |
| 同 | `TestRecoverAnchorPushDuringWait` | 首次官方失败后等待期推送到位 → 采纳推送（「同时等推送」不必等满 20s） |
| 同 | `TestRecoverAnchorContextCancel` | ctx 取消（窗口收尾）→ 立即返回，不发第 2 次 |
| 同 | `TestRecoverAnchorNilFetch` | 未配 fetcher 不 panic |
| [internal/flip/engine_test.go](../internal/flip/engine_test.go) | `TestAnchorPendingWindow` | **核心约定**：恢复前 tick 占槽不产观测、触底记 `anchor_pending`；`SetAnchor` 后照常触发，且 `m_45 = 0.55` 证明**恢复前的 ask 仍在 ring 里**（占槽的价值） |
| 同 | `TestAnchorZeroWindow`（更新） | 整窗不恢复 → 无观测、状态机走向不变、`WindowAnchor()` 锚仍为 0 |
| 同 | `TestSetAnchorInvalid` | 非法锚（≤0）被忽略 |
| [internal/flip/winstats_test.go](../internal/flip/winstats_test.go) | `TestWindowStatsAnchorMissing`（更新） | 锚未就绪窗计数与恒等式同正常窗、逐 tick `anchor_pending` |
| 同 | `TestWindowStatsCounters` | 新增 `anchor_pending` 后恒等式与深拷贝语义不变 |
| 同 | `TestTouchSideMirrorsTrigger` | 诊断镜像与真实触发 switch 等价（**未破**） |

---

## 8. 未做 / 观察项

1. **重启恢复的锚缺失窗口**：进程重启后 `windows_*` 本地预热已覆盖 σ，但锚缺失窗口
   在重启瞬间的恢复通道不会重建（恢复只在新窗口开跑时启动）。影响面 = 重启那一个窗口，
   与改前等价，不额外处理。
2. **官方 open 与边界流值的口径差**（p50 0.056bps）已量化，但**恢复成功窗口的信号
   是否与正常窗口可比**，要等 `anchor_src=official` 的样本攒出来再对照——目前样本量 0
   （paper 12 天未观测到运行期锚缺失，§6）。落盘字段已备好，新数据可直接分组。
3. **推送路径的有效期只有边界 +10s**：10s 之后任何新到的推送都会被 `abs` 判据拒掉，
   恢复循环的推送轮询从那时起实际是空转（官方重试继续到 +40s）。这是**有意为之**
   （§2.2），不是缺陷——但日志里「等推送」的表述容易让人以为会一直等，故在此点明。
4. **`btreplay_out.tsv`**（仓库根目录）是 `cmd/btreplay` 的输出产物，未入 `.gitignore`，
   跑一次对账就会在工作区留一份未跟踪文件。

---

## 9. 冒烟实测与顺带发现（2026-09-16 21:09~21:25 UTC，本机 paper 3 窗）

**本次改动在真实运行下的验证**（`https_proxy=127.0.0.1:1087 go run ./cmd/flip -config
v4.config.yaml -dashboard :8090`）：

- 锚可用窗口（3/3）**不启动恢复通道**、无多余日志，`winstats` 行 `anchor_src` /
  `anchor_recovered_ms` 缺省（omitempty 生效）——正常路径零扰动；
- 窗口收尾 `cancelAnch == nil` 分支直接跳过，`WindowAnchor()` 读到边界采样值
  （`anchor=75680.88` / `75728.57`），σ 正常追加（第 2 窗起 `hist_bps=8.46`）；
- `winstats_2026-09-16.jsonl` 首次生成，字段与 §4 一致；进程无 panic、无死锁。

> 该窗口的锚缺失路径**未被真实触发**（实测锚缺失是低频事件，§6）——恢复通道的
> 正确性由 §7 的注入式单测覆盖（含 `abs` 判据、3 次节奏、ctx 取消），不靠碰运气等现场。

### 9.1 顺带发现 ①：`skip=late` 重复行（同日已修）

**根因**：主循环步骤 1（[main.go:398-419](../cmd/flip/main.go#L398-L419)）用
`alignedTs := now.Unix()/300*300`（**floor**）定位当前窗口；而 collectLoop 的收尾 tick 用
`rem = int(endTime.Sub(tickTime).Seconds())` **截断**取整——`rem==0` 在边界**前 0~1s** 就成立，
循环因此总带着几毫秒~几百毫秒的提前量回来，此刻 `time.Now()` 仍落在**上一窗**内，
floor 回指的就是刚跑完的那一窗：

```
21:14:59.996786 [Cycle] 窗口结束 0xbb64… |close−anchor|=47.68, σ 现 18 窗
21:14:59.996871 [Cycle] ⚠️ 已落后窗口边界 5m0s（>15s），跳过本窗口 2026-09-16T13:10:00Z
```

该 `nextStart` 上一轮已当作正常窗口跑满（σ／健康度都已落盘），这里却被
`elapsed ≈ 5m > lateLimit(15s)` 判成迟到 → 多打一条 ⚠️ 日志、多落一行 `skip=late` 空行。

**生产实测规模**（`data/v4/winstats_2026-09-16.jsonl`，bug 生效期 07:40Z 启动 ~ 14:30Z 停）：
**163 行 / 82 个窗口——除进程启动那一窗，每窗都是两行**（81/81 孪生）。07 的逐日
「行数」（本应 ≈288，被当作「主循环跑满」的证据）正好翻倍，「跳过」列 82 条**全是假的**。
指纹（脚本核验 81/81 全中）：同一 `event_start` 恒为 {一行有数据, 一行 `skip=late` 且
`ticks=0`}。**影响面仅限 winstats 的计数/关联，不碰任何判定**（`touches_*` / `windows_*`
均为一窗一行，未受影响）。

**变体**：若该窗恰好是 σ 冷启动窗（§9.2），孪生的假行不是 `late` 而是
「`no_sigma` 真行 + `late` 假行」两行（14:10Z 冒烟实测）——故判据必须是
「`nextStart` == 上一轮处理过的窗口」，与 `skip` 取值无关。

**修法（2026-09-16 同日已落地）**：**按身份判定，不用时间启发式**——主循环记
`prevWindowStart`（上一轮迭代处理的窗口起点 unix 秒；进程重启后为 0，天然不复用），
步骤 1 的迟到分支加一层 `nextStart.Unix() == prevWindowStart` → **静默顺延到下一窗**
（不告警、不落 skip 行），否则维持原有告警 + `skip=late`。赋值点在顺延之后、`slug`
拼装之前，因此跳过通道（`no_market`/`no_token`/`dup_record`/`no_sigma`）与跑满窗口的
末路径**全部出口都被覆盖**——每条出口处理的都是同一窗。

为什么不改成放宽 `elapsed` 阈值：提前量（0~1s）与真·迟到（≥15s，进程启动／上游卡顿）
同在一条时间轴上，而 `int()` 截断让「提前 0.99s」与「迟到 0.01s」落在**同一秒格**里，
无法用 `elapsed` 分辨；身份判定零歧义，且不引入新常量（`lateLimit` 语义不变）。

**验收**（2026-09-16 22:35~22:52 UTC 本机 paper，空 `output_dir` 冷启动，`/tmp/dupl_smoke`，
修后二进制）：

| 观测点 | 结果 |
|------|------|
| 启动合法迟到 | 22:35:56 `已落后 56s` + `skip=late` 行**保留**（进程启动那一窗本就该跳） |
| 收尾边界前 9.5ms | 22:44:59.990472 窗口结束 → 22:45:00.000955 下一窗开始，**无 ⚠️、无 dup 行** |
| 收尾边界前 933ms | 22:49:59.066854 窗口结束 → 22:50:00.000643 下一窗开始，**无 ⚠️** |
| winstats | 3 行 / 3 窗 / **无重复**（1 条合法启动 `late` + 2 条真行），σ 连续（9.21→9.55→10.36，恒 18 窗），**无级联丢窗** |

对照：修前同一行为必出 dup（21:14:59.996871 / 22:44:59.990472 两例，见上），
且生产 82 窗 100% 复现——故本表两次「无 ⚠️」即为判据达成。

### 9.2 顺带发现 ②：冷启动首窗恒 `no_hist`（发现于本次冒烟，**同日补丁已修**）

σ 预热的网络分支要 10~30s（本地窗口陈旧/断档 → 走官方网络预热），本次实测 **77s**
（18 窗逐窗 1s + 429 退避）；且它是**异步 goroutine**（`warmupSigma`），不阻塞主循环。
而 `engine.BeginWindow(anchor, histBps)` 在**窗口开始时**就把 σ 冻住了、无热更新路径
→ **边界落在预热完成之前的那个窗口，整窗 σ=0**，任何触底都被 `no_hist` 拒，
且观测行 `hist_bps=0` 落盘——**正好踩中 `06_oos_review.py:161-167` 的关键字段 0 异常硬检查**。
判定顺序 `rem_low` 在 `no_hist` 之前，还会让窗口前 2 分钟（rem≤180）的触底以
`rem_low` 落盘、把 σ=0 藏起来——**真实发生次数比观测到的多**（无触底的窗口更是零痕迹）。

实测证据（本次冒烟 + 历史数据各一例）：

| 数据源 | 窗口 | 表现 |
|------|------|------|
| 本次冒烟 | `2026-09-16T13:10Z` | 窗口起 `hist_bps=0.00（0 窗）`，13:10:26 预热才完成；13:10:32 触底 → `no_hist`、观测行 `hist_bps=0` |
| 历史纸面 | `2026-09-03T15:40Z` | 同类冷启动窗，触底（rem=120）→ `rem_low`（该次时间腿先挡，但 `hist_bps=0` 已落盘）。该行是**整个纸面系列的第一条观测**（首行 ts 15:42:59），冷启动无疑 |

（`data/v4` 全量 **3544 条观测**里 `hist_bps=0` 共 2 行，均为冷启动窗口。09-15 复验的主窗
09-04~09-16 恰好未覆盖 09-03 那行，09-16 这行是本次冒烟新增的——**两行都不是锚缺失，
是 σ 冷启动**。频率量级：2 次/14 天 ≈ 1~4 次/月，每次最多 1 窗（预热彻底失败时最坏
连续 3 窗——要等本进程自己攒满 `HistMin` 窗），期望损失 ≈0.15 笔/次。）

**修法（2026-09-16 同日补丁已落地）**：上稿三个候选取 ③ 并收紧为「σ 未就绪**整窗跳过**」——
`cmd/flip` 在订阅/预取之前加准入闸 `hist.Count() < flip.HistMin` → 整窗跳过、落
`skip=no_sigma` 的 `winstats_*` 行、不产观测行（与 `late`/`dup_record` 同形；闸放在
预取之前，因该段到步骤 5 之间只有同步调用、σ 取值不变）。与锚缺失同一条原则：
数据源不可信 → 本窗不观测——σ=0 的窗口必然零信号（首触即 Done），留着那条
`hist_bps=0` 观测只有害无益。`06` 硬检查由此恢复「0 = 口径断裂」的纯度（决策 #13）。
新增 [internal/flip/sigma_test.go](../internal/flip/sigma_test.go) 3 例钉住这块判据
（`Count() < HistMin ⇔ Bps()==0`、滚动窗截断、空 Seed 不清空）。落地验收（2026-09-16
22:10 本机 paper，空 `output_dir` 冷启动）：边界当刻 `σ 未就绪（0 < 3 窗）` → 跳过日志 +
`skip=no_sigma` 行，`touches_*` **零行**。

**未采纳 ②（预热完成后热更新 σ，`SetHistBps`）**：它能把该窗剩余的 `rem>180` 时段救回来
（本次冒烟 σ 在 +26s 到位，可用余量还有 ~94s），即 ①③ 丢掉的那 ~0.15 笔/冷启动期望；
但代价是「窗口级常量 σ」变成时变量——同一事件的观测行 `dist_s` 取决于 σ 何时落地
（不可复现），回测也无对应形态（btreplay 每事件 σ 固定，只有数据头部 ≤2 个事件为 0），
且 `no_hist` 从终态变可续会动到「不碰 decide／判定顺序」这条线。0.15 笔换这些口径分叉，不值。

**副作用（已在决策 #13 记录）**：本窗无 close 采样 → `windows_*` 缺一行，等价于停机窗
（`RecentBlock` 600s 缺口容差吸收，同 `dup_record`）；`no_hist` 与 `missing_anchor` 一样
变为现网不可达，`decide` 分支只作纯函数防线。

### 9.3 本次冒烟写入纸面数据的位置（如需剔除）

- `touches_2026-09-16.jsonl` +1 行（13:10:32 `no_hist`，`hist_bps=0`）
- `windows_2026-09-16.jsonl` +2 行（13:10 / 13:15 的 σ 振幅）
- `winstats_2026-09-16.jsonl` 新建 5 行（§9.1 的 2 行重复含在内）
- 冒烟环境自身降级（代理 + `slow consumer` WS 报错，13:15 窗 `book_missing` 偏高），
  这 3 个窗口的 tick 健康度**不代表生产水平**。

### 9.4 `data/v4/winstats_2026-09-16.jsonl` 的 81 行假 `late`（如需剔除）

§9.1 的 bug 在当天生产进程里全量生效：**163 行里 81 行是假 `skip=late`**（孪生指纹见 §9.1）。
**未自动改动数据文件**——剔除命令（**须在引擎停跑时执行**：in-place 重写是 temp+rename，
运行中的进程仍持有旧 inode 的 append 句柄，之后的行会写进已 unlink 的文件）：

```bash
cd <repo> && cp data/v4/winstats_2026-09-16.jsonl /tmp/winstats_2026-09-16.orig.jsonl
python/venv/bin/python - <<'PY'
import json, collections
p = 'data/v4/winstats_2026-09-16.jsonl'
rows = [json.loads(l) for l in open(p)]
cnt = collections.Counter(r['event_start'] for r in rows)
keep = [r for r in rows if not (r.get('skip') == 'late' and cnt[r['event_start']] > 1)]
with open(p, 'w') as f:
    for r in keep:
        f.write(json.dumps(r, ensure_ascii=False) + '\n')
print(f'{len(rows)} → {len(keep)} 行（剔除 {len(rows) - len(keep)} 行假 late）')
PY
```

判据只丢「`skip=late` 且有孪生」的行——进程启动那一窗的合法 `late`（07:40Z，
无孪生）会保留。剔除后当天即恢复「每窗一行」，07 的逐日行数／跳过列口径随之复原。
