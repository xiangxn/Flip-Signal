# 数据源延迟闸 + 日亏熔断 实施计划（2026-09-16）

> 需求来源（2026-09-16 用户提出）：
> 1. **需要监测数据源延迟，如果延迟太高时，信号直接判断为无效**
> 2. **单日亏损风控，可配置值，日（UTC 切分）最大亏损默认 24U。达到就停止当日交易**
>
> 本文只落方案，不动代码。文中所有数字均为本地实测（数据源 `data/v4` 纸面 09-03~09-16、
> `data/btc` 回测 08-18~08-31、`python/v4/data/trades_r1_combo.csv` 头条）。
>
> **状态：已实施（2026-09-16 当日完成）。** 三个开放项于 2026-09-16 拍板（见 §7）：
> 需求 1 只做配置化 + 可见性、paper 语义取方案 A、熔断线 24U 为终值且不做线扫描脚本。
>
> 落地记录（提交序，与 §6 的切分一致）：
> - `flip:` 延迟闸配置化 + 观测补 `spot_age_ms`（行为中性）
> - `flip:` 每窗 tick 健康度计数 + `winstats_*.jsonl` + dashboard 阈值下发
> - `flip:` 日亏熔断 —— 闸上移两模式共用（方案 A）+ 当日锁存 + `gate_reason` + dashboard 风控块
> - `python:` 07 数据源健康度审计 + 02/06 过滤被闸行（`--include-gated`）
> - `docs:` 本文与 CLAUDE.md 同步
>
> 落地期修正的三处口径（正文对应位置已就地标注）：§2.4 计数器 `Ticks` 不含 `rem==0`
> 终 tick 且新增 `TicksValid`（恒等式可当场核对）；§2.4 `winstats` 实际 schema（`kind`
> 守卫 + `lost_triggers` 为明细数组 + `skip` 原因）；§3.6 极性字段取名 `CanTrade` 而非
> `BreakerOpen`（避免与 `LiveExec` 的历史反极性字段混淆）。验收：`cmd/btreplay` 625 笔
> 逐位一致红线不变；`07_source_health_check.py` 首跑与 §1.2 基线**逐项对齐**（33 行
> 0.93% / 遮蔽 24 / 真丢 9 / 急跌过 5 / 夜间 88% / book 扫描 T=300 → n=625 +395.8U，
> T=20 少赚 62.8U / 零成交代理 8 笔 1 胜 7 负）。

---

## 0. 结论速览（TL;DR）

| 需求 | 现状 | 本计划做什么 | 关键证据 |
|------|------|--------------|----------|
| 1 延迟监测 | 三道闸**已存在但硬编码**，且被闸掉的信号**零可见性** | 阈值**配置化**（默认值全不动）+ 每窗**计数落盘**（丢了多少信号、什么原因）+ 观测行补 `spot_age_ms` + Dashboard 健康度 | **收紧 book 阈值是负收益**：14 天回测 EV +0.633→+0.551（T=20ms），单调变差。**「信号少=现货延迟」方向对但量级小**：真丢信号 9 条/14 天（0.35%），09-12/09-13 缺口主因是 `rem_low`；真正的盲区是**亚阈值（<2s）现货龄不落盘** |
| 2 日亏熔断 | `CanTrade` 已存在，但**仅 live 生效**、默认 −20U、**不锁存**、paper 不显示 | 默认 **−24U**、两模式统一闸、**当日锁存**、paper 照记照结算（反事实不丢） | 24U 线回放纸面 09-03~09-16：**+55.2U → +120.1U（Δ+64.9U）**，熔断 4 天全在亏损日，无误伤 |

**两条需求都不需要改判定逻辑**——引擎四腿判定与回测 1:1 镜像（`cmd/btreplay` 625 笔逐位一致）
必须保持，本计划所有改动都在「数据源采样层 / 执行层 / 记录层」，**不碰 `engine.decide`**。

---

## 1. 现状盘点

### 1.1 已有的三道延迟闸（硬编码常量）

| 数据源 | 用途 | 阈值 | 位置 | 超阈后果 | 配置面 |
|--------|------|------|------|----------|--------|
| PM 盘口 | 触发腿 + 急跌腿 m_45 | `book_latency_ms ≤ 300` | [engine.go:7](internal/flip/engine.go#L7) `maxLat` | tick 无效：压 0 占槽、不触发 | ❌ 常量 |
| Binance spot | 浅洞腿 dist_s | `接收龄 ≤ 2000ms` | [main.go:64](cmd/flip/main.go#L64) `spotFreshMs` | 置 `BinPrice=0` → `missing_spot` 否决 | ❌ 常量 |
| Chainlink TWAP | 锚（窗口级）+ σ + dist_t 观察 | `龄 ≤ 10000ms` | [main.go:79](cmd/flip/main.go#L79) `twapCloseFreshMs` | 边界：整窗跳过；窗末：本窗不计入 σ | ❌ 常量 |

触发 tick 的 `book_latency_ms` 与 `twap_age_ms` 已随观测落盘（`Observation` 字段），
但 **spot 的接收龄没有落盘**（只落了价格 `spot`）。

### 1.2 数据实测：三道闸在真实数据上表现如何

**(a) PM 盘口延迟（纸面 445 条 ok 行）**：p10=8 / p50=10 / p90=23 / p99=142 / **max=275ms**。
没有任何观测行 >300ms（构造使然：>300 的 tick 根本不会触发）。**闸从未在触发点生效过。**

**(b) 14 天回测阈值扫描**（`data/btc`，组合带纯现货，逐阈值**重算**触发与 m_45，非事后过滤）：

| 阈值 T(ms) | 组合 n | WR | EV/注 | PnL |
|-----------|-------|-----|-------|-----|
| 300（现行） | 625 | 24.6% | **+0.633** | **+395.8** |
| 150 | 623 | 24.6% | +0.626 | +390.3 |
| 100 | 621 | 24.6% | +0.636 | +394.8 |
| 50 | 614 | 24.6% | +0.632 | +387.9 |
| 30 | 610 | 24.1% | +0.573 | +349.6 |
| 20 | 604 | 23.8% | +0.551 | +333.0 |
| 10 | 469 | 22.6% | +0.452 | +211.8 |
| 8 | 204 | 25.5% | +0.719 | +146.6 |

**结论：300ms 在平台期上，往下收紧单调变差**（T=20ms 少赚 62.8U）。纸面数据里
`≥20ms` 那 53 笔看着很差（EV −0.575 vs <20ms 的 +0.213），但**与 14 天回测方向相反**，
n=53 且本项目在这个数据上已反复踩过「小样本切片」的坑（见 09-11 趋势带、09-10 带宽）。
**不建议动这个阈值。**

**(c) spot 2s 闸 —— 用户 2026-09-16 假设「信号少的天数可能是现货延迟被过掉了」的复核**

纸面 09-03~09-16 触发 tick 现货缺失（`spot==0`，即本地接收龄 >2s）共 **33 行 / 3541 = 0.93%**。
但其中**只有 9 行**是真正丢掉的信号，**24 行被前序腿遮蔽**：

| 分类 | n | 含义 |
|------|---|------|
| `rem_low` + `spot==0` | **24** | rem ≤ 180 已先否——**本来就不成立**，遮蔽无害，但在 reject 统计里完全不可见 |
| `missing_spot`（rem > 180） | **9** | **真·丢信号**（0.67/日 = 信号量的 1.7%） |

对这 9 行离线复核**急跌腿（`m_45` 已随行落盘）**：**5 行过**、4 行本就不过。
洞腿无法离线复核（`spot==0` 时 `dist_s` 不计算）。按「rem>180 且急跌过」的条件通过率
36.9% 折算 → **实际少赚的信号约 1.8 笔 / 13.5 天 = 0.14 笔/日 ≈ 信号量的 0.35%**。

**按日看，用户直觉抓到的是真东西**：09-12 有 6 行、09-13 有 8 行（缺失率 2.1% / 2.8%），
相对其余 12 天的基线 0.64% 显著抬升（二项检验 p<0.01 / p<0.001）；同期
**PM book 延迟完全正常**（p50=11ms，p99 169/179ms）→ 是 **Binance 链路侧**问题，
不是整机网络。小时分布高度集中：**UTC 20:00~06:00 占 29/33 = 88%**（20 点 4、21 点 5、22 点 7）。

**但量级解释不了那两天的缺口**：09-12 / 09-13 的 ok 只有 **13 / 20 笔**（其余完整日中位数 ~40），
缺口 −27 / −20 笔；现货断流按上折算只贡献 **≈0.4 / ≈0 笔**。那两天的真实主因是
**触底时点后移**——`rem_low` 占比 **56% / 47%**（典型 ~45%，09-12 的 160 次为全样本最高），
与 09-15 复验「OOS 触底时点后移 ~14s」是同一现象。

回测侧代理（`bin.ticks` = 每秒成交笔数，「触发 tick 起往前连续零成交秒数」）：
625 笔里 **8 笔**触发时有 ≥1s 无成交（1.3%），战绩 **1 胜 7 负**（EV −2.0 ~ −0.33）——
**方向与假设一致，但 n=8 无统计意义**；「近 2s 至少 1 笔成交」过滤后 n 一笔未变。

→ **结论：现货延迟确实在丢信号，但每天约 0.14 笔（0.35%），不是那两天的解释**。
可以量化的部分全部记录在案；**真正看不见的是 <2s 的亚阈值陈旧**（见下 §1.2(e)）。

**(d) TWAP 龄**：纸面观测里 `twap_age_ms > 10s` 共 **10 行**（最长 **127.6s**，2026-09-04），
其中 **1 笔 ok**（09-09，46.8s 陈旧，亏 2U）。窗口级：09-06 起 2913 个窗口里
**12 个缺 σ 行**（0.41%，多为窗末 close 采样撞上断流），最长连续缺 2 窗。
→ 当前 TWAP 断流**不是主要矛盾**（2min 看门狗 + 边界守卫基本兜住）。

> ⚠️ 上面查这个时踩了个坑，记录备查：`windows_2026-09-05.jsonl` 只有 85 行、09-05 00:00~16:50
> 整段「有观测无 σ 行」——不是数据源问题，是**该功能当天 16:55 才上线**（`LogWindowAmplitude`
> 引入于 09-05/06）。凡按 windows 文件做逐日统计，**09-05 及更早不可比**。

**(e) 真正的盲区：亚阈值（<2s）陈旧 —— 现在完全看不见**

上面 (c) 的 33 行是「现货直接判死」的冰山顶。更值得注意的是**没被判死的那部分**：
观测行落了 `spot` 价格，却**没落它的年龄**，所以 0.1s 与 1.9s 的行在数据里长得一模一样。

为什么这要紧：决策腿是 `dist_s = sgn·(spot−anchor)/anchor/σ`，用的是**触发那一刻的 spot**。
触发时刻恰恰是急动时刻——

- 常态 1~2s 的 BTC 位移 ≈ 0.9~1.3bps ≈ **0.15~0.2σ**（σ 取 OOS 实测 6.17bps）→ 影响不大
- 但触底正是急动：崩盘秒内 1s 位移可达 5~10bps ≈ **0.8~1.6σ**，而 yes 侧浅洞带**总宽度只有 0.6σ**
  → **1 秒的现货陈旧足以把一笔单从带内推到带外，或反向**

也就是说：现货龄对判定的敏感度**在触发点被放大**，而这部分**零可观测**。
(c) 的回测代理只有 8 笔样本（1 胜 7 负，方向一致但不足以定论），纸面侧更是完全无从查起。

→ 这是 `spot_age_ms` 落盘（§2.3）从「锦上添花」升级为 **P0** 的直接理由：
它同时回答两个问题——(i) 现货断流丢了多少信号（已知 ≈0.14 笔/日）；
(ii) 亚阈值陈旧有没有在污染 `dist_s`（**完全未知，且可能是量级更大的一项**）。

### 1.3 真正的缺口：被闸掉的信号「零可见性」

引擎在 tick 无效（延迟 >300ms）时走 `pushSlots` 直接 `return nil`（[engine.go:93-103](internal/flip/engine.go#L93-L103)）——
**如果这一 tick 其实某侧 ask 已经砸到 0.2，这个信号就凭空消失了，磁盘上没有任何痕迹**：
不进观测、不进 reject 原因统计、Dashboard 也看不到。

这条与 09-15 复验的一个已知异常**直接相关**：A 层信号频率闸门未过
（实测 36.5/日 vs 计划 44±5，`docs/dog020_oos_result_2026-09-15.md`）。
当时的归因是「触底时点后移 + σ −33%」，但**延迟导致的静默丢信号是当时排查不到的盲区**——
因为没有任何计数器。本计划的 P0 之一就是把这个盲区点亮（不改判定，只加计数）。

### 1.4 现状：日亏熔断

已实现（[risk.go](internal/flip/risk.go) + [exec_state.go:71-91](internal/flip/exec_state.go#L71-L91)）：

- `CanTrade(todayPnl, maxDailyLoss) bool` 纯函数，口径 = **当日已结算 P&L ≤ 线 → 停单**
- 无状态现算：每次从 `Recorder.DailyPnl()`（磁盘真相）取今日已结算 P&L → **重启安全、跨 UTC 日自动归零**
- 默认 `--max-daily-loss -20`（main.go:115），**只在 `if x.Live` 分支**生效，paper 完全不过闸
- 被拦时 live 落 `exec_status=rejected` 行（`RecordLiveRejected`），不注册结算

三个待补的洞：

1. **paper 不过闸** → 无法在纸面上验证这条风控（而纸面正是当前唯一在跑的数据源）
2. **不锁存** → 若触发后已结算累计因**在途单结算**回升到线上，熔断会**自动复牌**。
   样本内恰好没发生（我用真实结算滞后 60/90/120s 三种口径回放，均无一例），
   但这是**靠运气**：在 paper（gated 行继续结算）下它会**必然**发生——所以锁存是本计划的必需项而非保险
3. **默认值 −20 与需求不符**（要 24U），且启动横幅只在 live 分支打印这条线

### 1.5 数据实测：24U 线回放（纸面 09-03~09-16，UTC 日，2U/笔）

按触发顺序逐日走，累计已结算 P&L ≤ −24U 即当日停单（含在途结算时序，滞后 60/90/120s 三口径结论一致）：

| 日期 | 笔数 | 裸 PnL | 闸后 | 是否熔断 |
|------|------|--------|------|----------|
| 09-03 | 14 | −28.0 | **−24.0** | ⛔ (停于第 12 笔) |
| 09-06 | 40 | −33.8 | **−24.9** | ⛔ (停于第 18 笔) |
| 09-10 | 40 | −60.0 | **−24.0** | ⛔ (停于第 22 笔) |
| 09-13 | 20 | −40.0 | **−24.0** | ⛔ (停于第 12 笔) |
| 其余 10 天 | — | +217.0 | +217.0 | 未触发 |
| **合计** | 454 | **+55.2** | **+120.1** | **Δ +64.9U** |

- 4 次熔断**全部落在亏损日**，10 个盈利日**一次都没误伤**（最大单日 +76.6U 那天没被碰）
- 变体：含未结算敞口（每笔在途按 −stake 计入）在 120s 滞后下 Δ+74.9U（多 10U，来自 09-03 早停 1 笔）；
  已结算口径已吃到绝大部分收益，**建议维持已结算口径**（简单、与回测/复验口径一致）
- 样本口径提醒：09-03 是纸面首日（半天），「09-04 起」口径为 Δ+60.9U / 熔断 3 天

---

## 2. 需求 1 方案：数据源延迟

### 2.1 设计原则（**硬约束**）

1. **不碰 `engine.decide` 与判定顺序** —— `cmd/btreplay` 625 笔逐位一致 + `01_backtest_r1.py`
   镜像必须保持不变（这是本项目判定代码正确性的唯一硬验收，见 CLAUDE.md「口径与回测 1:1」）
2. **闸的语义保持"数据源不可信 → 该 tick/该窗不产出信号"**，不新增"事后否决"路径
3. **纸面与实盘同源**：闸在两模式行为一致，差异只在记录形态

### 2.2 变更 A1：阈值配置化（采样层，不改判定）

三个常量 → 三个 flag（默认值 = 现行值 = 数据支持值）：

| flag | 默认 | 替换 | 说明 |
|------|------|------|------|
| `--max-book-lat-ms` | 300 | `engine.go:7 maxLat` → `Config.MaxBookLatMs` | 与回测 `MAX_LAT=300` 同名同值 |
| `--max-spot-age-ms` | 2000 | `main.go:64 spotFreshMs` | 采样层，不进入引擎 |
| `--max-twap-age-ms` | 10000 | `main.go:79 twapCloseFreshMs` | 边界 anchor + 窗末 close 共用 |

启动时**校验并打印**：`--max-book-lat-ms < 100` 打 ⚠️ 警告（数据上 <100ms 是负收益区，
见 §1.2b），`< 0` 直接 `log.Fatalf`。

> spot 龄闸刻意留在采样层（`sampleTick` 里把陈旧 spot 置 0），不搬进引擎：
> 回测数据没有 spot 龄字段，搬进去会让 mirror 对账多一个不可比输入。

### 2.3 变更 A2：观测行补 `spot_age_ms`（P0，最小改动）

- `Tick` 增 `SpotAgeMs int64`（[types.go:110](internal/flip/types.go#L110)）
- `Observation` 增 `SpotAgeMs int64 \`json:"spot_age_ms,omitempty"\``
- `main.sampleTick` 填充；`engine.decide` 原样透传（**不参与判定**，纯记录）

**理由（§1.2(c)(e) 实测驱动，这是本需求里信息增益最大的一行）**：
现在只能看到 `spot=0`（已判死），看不到**"多陈旧"**——1.9s 与 15s 在数据里长得一样。
漏掉的正是量级可能更大的那一类：<2s 的亚阈值陈旧在触发点直接污染 `dist_s`
（急动秒 1s ≈ 0.8~1.6σ，而 yes 带总宽 0.6σ）。补齐后：

- 可直接给出**每笔信号的现货龄分布**（分 ok / 各 reject 原因）
- 可复核 §1.2(c) 那 24 行「被 `rem_low` 遮蔽」的现货异常在别的维度长什么样
- 可定位 09-12/09-13 那类**日级断流**（UTC 20:00~06:00 集中）是链路还是本地

`omitempty` + 新键，对 `python/v4` 现有脚本零影响（旧行读作 0，分析时按 `>0` 过滤）。

> 注意：`spot_age_ms` 与 `spot` 落盘口径必须同源——都取 `sampleTick` 里那一次
> `LatestData()`（同一 tick 只读一次），避免"价格是新的、年龄是旧的"这种自相矛盾。

### 2.4 变更 A3：信号丢失可见化（P0，本计划核心）

引擎新增**每窗统计计数器**（只计数，不改任何判定分支的走向）：

```go
// windowStats: 本窗 tick 健康度（Watching 态内统计; Done 后不再计）
type windowStats struct {
    Ticks        int // 进入有效性分类的 tick 数（不含 rem==0 终 tick、不含 Done 后的 tick）
    TicksValid   int // 过延迟闸 + 整簿门控的 tick（= Ticks − BookStale − BookMissing）
    BookStale    int // BookLatMs > MaxBookLatMs 而无效的 tick
    BookMissing  int // 整簿四字段不全而无效的 tick（镜像回测 :79 门控）
    LostTriggers []lostTrigger // 「本会触发但被上两条挡掉」的 tick
}
type lostTrigger struct {
    Ts int64; Side string; Rem int; Ask float64
    BookLatMs int64; Reason string // stale_book | book_missing
}
```

> 口径修正（2026-09-16 落地时）：`Ticks` 计的是**进入有效性分类的 tick**，不含
> `rem==0` 的窗口终 tick（该 tick 只做窗末结算，不进触发检查），全窗 300 槽位下
> 典型值为 299；`TicksValid` 是新增字段，让恒等式
> `Ticks == TicksValid + BookStale + BookMissing` 可当场核对（07 脚本 A 段即查这条）。
> 另加 `AnchorMissing bool` 标记整窗不观测（锚缺失），此情形下计数恒 0。

- `LostTriggers` 判据：`state==Watching` 且该 tick 任一侧 `0 < ask ≤ TriggerAskMax`，
  但 tick 因延迟/缺快照被判无效 → 记录。**不改变状态机**（该 tick 依旧走 `pushSlots`，事件未 Done）
- 新增 `Engine.WindowStats() windowStats`（内部锁，返回拷贝）
- **窗口结束时无条件落盘**（含锚缺失被跳过的窗口）：

`windows_*.jsonl` 每窗一行不够用——该文件是 σ 预热的**数据源**，
混入统计行会污染 σ（`loadWindowFileLocked` 只校验 `Ts>0 && Date!=""`，会把统计行当振幅行读进来，
Amp=0 直接污染其后 18 窗）。故：

**新增独立文件 `winstats_YYYY-MM-DD.jsonl`**（追加写、不重写、不载入内存、纯分析用）：

```json
{"ts":..., "date":"2026-09-16", "kind":"winstats", "condition_id":"0x..", "slug":"btc-updown-5m-...",
 "event_start":..., "anchor":69000.0, "hist_bps":9.3,
 "ticks":299, "ticks_valid":297, "book_stale":2, "book_missing":0,
 "lost_triggers":[{"ts":...,"side":"yes","rem":196,"ask":0.19,
 "book_lat_ms":412,"reason":"stale_book"}]}
```

> 口径修正（2026-09-16 落地时）：`kind` 恒为 `"winstats"`（跨类型误读的第二道守卫，
> 第一道是文件前缀）；`lost_triggers` 是**明细数组**而非计数（计数即数组长度）；
> 未采集的窗口落 `"skip"`（取值 `late` 窗口来不及 / `no_market` gamma 无市场 /
> `no_token` token 解析失败 / `dup_record` 条件 id 重复）且统计字段全 0——保留该行是为了让
> 逐日行数（≈288）本身成为"主循环是否跑满"的证据；锚缺失窗落 `"anchor_missing":true`。
> 原稿的 `rem_end`/`anchor_ok`/`triggered`/`ok` 字段未实现（触发与结算结果已在
> `touches_*.jsonl` 里，同键重复只会带来两处口径打架）。

加载器 `loadWindowFileLocked` **同时加一道 schema 守卫**（过滤 `kind != ""` 的统计行），
双保险——即使将来误写进 windows 文件也不会污染 σ。

### 2.5 变更 A4：Dashboard 健康度（P1）

三源现值已在页面（`book_lat / twap_age / spot(+age)`，[app.js:109-121](internal/dashboard/static/app.js#L109-L121)），缺的是：

- **三个阈值一起下发**（`/api/state` 增 `limits: {book_lat_ms, spot_age_ms, twap_age_ms}`），
  前端按阈值标红（现在只有 spot 硬编码 2000 标红，book/twap 无颜色语义）
- **本窗统计**：`ticks_valid / book_stale / book_missing / lost_triggers` 与最近一次
  `lost_triggers` 明细（一行文字：`丢信号: yes rem=196 ask=0.19 (book 412ms)`）
- **风控块**见 §3.6

### 2.6 明确**不做**的两件事（附理由）

1. **不收紧 book 阈值**（§1.2b 证据：单调变差）。阈值配置化的意义是"能调"，不是"该调"
2. **不给「触发 tick 的 TWAP 龄」加否决腿**：TWAP 不参与判定（`dist_s` 只用到窗口级
   `anchor`，已在边界用同一阈值守卫；`dist_t` 是观察腿）。加这条会凭空改变判定宇宙、
   破坏镜像，却没有机制支撑

可选 P2（低优先，一行改动）：`decide` 写 `DistT` 时若 `TwapAgeMs > 阈值` 则不写
（对齐 `dist_s` 已被 spot 新鲜度隐含门控的现状）。影响：10 行/13 天的诊断字段消失，
**需确认 `02/05/06` 脚本对缺失 `dist_t` 的处理是跳过而非当 0**，否则不做。

### 2.7 验证脚本 `python/v4/07_source_health_check.py`（P1）

纯标准库（与 06 同风格）：

1. 读 `winstats_*.jsonl` → 逐日/逐窗：无效 tick 占比、丢信号数、原因分解
2. 读 `touches_*.jsonl` → 三源龄分布（分 ok/否决），**含 `spot_age_ms` 分桶战绩**
   （这是新字段的主要用途：`spot_age_ms` 桶 × WR/EV，直接回答 §1.2(e) 的亚阈值污染问题）
3. **现货断流专项**：按日/按 UTC 小时列缺失率（复核 §1.2(c) 的 09-12/09-13 抬升与
   20:00~06:00 集中是否持续）+ 复算「被 `rem_low` 遮蔽」的行数
4. 读 `data/btc` → 重跑 §1.2b 的 book 阈值扫描与 §1.2(c) 的 `bin.ticks` 现货龄代理
   （保证任何阈值改动都有据可依）
5. 输出"延迟导致的信号损失率"——直接对应 09-15 复验里那个查不下去的频率缺口

交付后第一次跑，应对齐本文 §1.2 的全部基线数字（33 行 / 9 真丢 / 24 遮蔽 / 8 笔回测代理等），
**对齐不上说明口径写错了**。

---

## 3. 需求 2 方案：日亏熔断

### 3.1 口径定义（写死进文档与注释）

| 项 | 取值 |
|----|------|
| 日界 | **UTC 日**（与记录文件切分、`DailyPnl`、`LiveSummary` 同口径，`utcDate` 唯一实现） |
| 线值 | `--max-daily-loss`，**默认改为 −24**（负值口径，见 §3.2） |
| 计量 | **当日已结算 P&L**（`Recorder.DailyPnl()` 现算，磁盘真相；含被闸行——见 §3.4） |
| 判据 | `todayPnl ≤ 线` → 停单；**当日锁存**（一旦触发，本 UTC 日不再开单） |
| 检查点 | 每个 ok 信号执行**之前**（既有位置），逐笔判 |
| 不变项 | 不重试、不补单；被闸行照落盘（观测与信号频率口径不受影响） |

### 3.2 变更 B1：默认值 −20 → −24，且对非法值快速失败

- [main.go:115](cmd/flip/main.go#L115) `--max-daily-loss` 默认 `-20` → `-24`
- 传 `≥ 0` 的值直接 `log.Fatalf`：正值语义反转（`PnL > 线` 会变成"必须盈利才能开单"），
  静默接受是最危险的失败模式
- **保留负值口径不改 flag 名**：CLAUDE.md/部署脚本/文档已是 `--max-daily-loss -20`，
  改名会让旧脚本静默落到新默认值（行为漂移无声无息），比"负数不直观"风险大
- paper/live 两模式的启动横幅都打印这条线（现在只在 live 分支）

### 3.3 变更 B2：闸上移到两模式共用（执行层）

现在闸埋在 `if x.Live { ... }` 里（[exec_state.go:71-91](internal/flip/exec_state.go#L71-L91)）。
拆成：

```go
// 模式无关的闸（paper/live 同一判据、同一顺序）
func (x *ExecState) gate() (reason string, blocked bool) {
    if x.Live && x.FirstWindow { return GateFirstWindow, true }   // 仅 live
    if x.breakerTripped()      { return GateDailyLoss, true }     // 两模式
    return "", false
}
```

- **live 命中**：走既有 `RecordLiveRejected`（`exec_status=rejected` + `exec_note`），
  不注册结算（无持仓）；额外写 `gate_reason`
- **paper 命中**：走既有 `RecordObservation`（**行 schema 与今天一致**，
  `exec_status` 仍为空）+ 额外写 `gate_reason` → `IsFilled()==true` → **照常注册结算**
- 这样**纸面与实盘的行为差异仍然只有"是否真实 POST / 是否真有持仓"**，
  闸判据本身 100% 同源（符合项目「纸面/实盘同源」原则）

### 3.4 变更 B3：锁存（**必需项，非保险**）+ 记录字段

**为什么必需**：paper 下被闸行继续结算 → 累计 P&L 会因后到的结算**回升过线** →
无锁存则当日**自动复牌**，风控形同虚设。live 下同理（触发瞬间可能有在途单）。

**实现（沿用「无状态 + 磁盘真相」哲学，不引入新状态文件）**：

```go
// Recorder: 当日是否已有被闸行（跨重启天然恢复）
func (r *Recorder) GatedOn(date, reason string) bool
```

- 语义：本 UTC 日已存在 `gate_reason == "daily_loss"` 的行 → 熔断保持打开
- 重启后：`DailyPnl()` 现算 + `GatedOn(今日)` 扫描 → 状态完整恢复，**无需落任何额外状态**
- 跨日：`date` 变化 → 自动归零（与 `todaySettledPnl` 同源）

`Record` 新增：

```go
GateReason string `json:"gate_reason,omitempty"` // daily_loss | first_window; 空 = 未被闸
```

常量 `GateDailyLoss = "daily_loss"` / `GateFirstWindow = "first_window"`。
`omitempty` + 新键，对既有 python 脚本零影响；但**分析脚本要显式过滤**
（见 §3.7）——被闸行不是真成交。

### 3.5 变更 B4：paper 语义 —— **已定：方案 A**（2026-09-16 用户批准）

| 方案 | 行为 | 优点 | 缺点 |
|------|------|------|------|
| **A ✅ 采用：闸生效 + 行照记照结算** | 被闸行写 `gate_reason`，仍结算回填 won/pnl | 纸面/实盘闸判据**完全同源**；反事实（不熔断会怎样）**照样可算**；只多一个字段 | 需分析脚本过滤，否则纸面 P&L 会混入未成交行 |
| B 影子：只标记不影响统计 | 同 A 但语义上声明"仍算成交" | 分析脚本零改动 | 与 live 行为不同源，违背项目原则 |
| C 硬停：被闸行不落盘 | 当日后续信号消失 | — | 丢失当日信号频率口径与反事实；**不推荐** |

**采用 A**。理由：纸面是当前唯一在跑的 live-like 样本，任何"砍数据"的方案都会
削弱 09-30 复盘的统计力；而 A 在信息上严格优于 B 和 C（两个序列都能重建）。

**实现要点（A 的落法）**：闸判据（`gate()`）在 `HandleObservation` 里**两模式共用**，
paper 与 live 走同一段代码、同一个 `todayPnl` 来源，唯一差别是 live 的闸会拦下真实
POST、paper 只写 `gate_reason`。被闸行**不改变** `IsFilled()` 的既有返回值
（观测/结算链路逐位不动），只多一个 `gate_reason` 字段供下游过滤——这样
`recorder`/`resolution_poller` 零改动，回归面最小。

### 3.6 变更 B5：Dashboard 风控块（两模式都显示）

新增 `LiveSnapshot.Risk *RiskSummary`（paper 也填，`live` 块保持原样不动）：

```go
type RiskSummary struct {
    TodayPnl     float64 `json:"today_pnl"`
    MaxDailyLoss float64 `json:"max_daily_loss"`
    CanTrade     bool    `json:"can_trade"`      // 熔断未触发 = 可开单（含当日锁存）
    GatedToday   int     `json:"gated_today"`    // 今日被闸笔数
    Enforced     bool    `json:"enforced"`       // 是否拦下真实 POST：live=true / paper=false
                                                 // （闸判据本身两模式同源，见 §3.5 方案 A）
}
```

> 实现修正（2026-09-16 落地时）：极性字段取名 `CanTrade` 而非 `BreakerOpen`。
> 既有 `LiveExec.BreakerOpen` 是**历史遗留的反极性命名**（`true` = 可开单，
> app.js 用 `if (!l.breaker_open)` 显示"熔断停单"），新增类型沿用同名反极性只会
> 制造第二个语义陷阱——新类型直说 `CanTrade`，`LiveExec` 保持原样不动
> （其取值已改为与闸同源：`!breakerTripped()`，否则面板会显示"可开单"而实际已停单）。

前端在现有 live 卡片旁加一行：`风控 今日 −18.0 / −24.0U  ·  熔断已触发（拦 6 笔）`，
paper 模式加"影子"角标。

### 3.7 变更 B6：分析脚本适配（P0，否则数字会错）

- `02_paper_compare.py` / `06_oos_review.py`：**默认排除 `gate_reason=="daily_loss"` 行**
  （实盘不会开这一笔），加 `--include-gated` 开关保留旧口径
- **不做**熔断线扫描脚本（2026-09-16 用户：暂不需要，24U 为终值）——本文 §1.5 的
  回放口径已固化在文档里，将来若要换线，按同一口径手算即可，不必先写脚本
- ⚠️ 已知坑继承：`trades_r1_combo.csv` 曾被 `--rem-max` 实验覆盖过（09-16 记录在案），
  任何回测重跑**必须用默认参数**并核对头条 n=625 / +395.8U

---

## 4. 变更清单（按文件）

| 文件 | 变更 | 优先级 |
|------|------|--------|
| `internal/flip/types.go` | `Config.MaxBookLatMs`；`Tick.SpotAgeMs`；`Observation.SpotAgeMs`；`Record.GateReason`；`GateDailyLoss/GateFirstWindow` 常量 | P0 |
| `internal/flip/engine.go` | `maxLat` → `cfg.MaxBookLatMs`；`windowStats` 计数 + `WindowStats()`；**判定分支零改动** | P0 |
| `internal/flip/exec_state.go` | 闸上移两模式共用（`gate()`）；`breakerTripped()` 含锁存；`RiskSummary()` | P0 |
| `internal/flip/risk.go` | 注释补口径（UTC/已结算/锁存）；`CanTrade` 签名不变 | P0 |
| `internal/flip/recorder.go` | `GatedOn(date, reason)`；`LogWindowStats(...)` + `winstats_*` 句柄；`loadWindowFileLocked` schema 守卫 | P0 |
| `internal/flip/snapshot.go` | `LiveSnapshot.Risk`；`RiskSummary` 类型 | P1 |
| `cmd/flip/main.go` | 3 个延迟 flag 替换常量；`--max-daily-loss` 默认 −24 + 非法值 Fatal；窗末落 winstats；启动横幅 | P0 |
| `cmd/btreplay/main.go` | 显式 `MaxBookLatMs: 300`（保持对账基线） | P0 |
| `internal/dashboard/{handlers.go,state.go,static/app.js,index.html}` | 阈值下发 + 标红 + 本窗统计 + 风控块 | P1 |
| `python/v4/02_paper_compare.py`, `06_oos_review.py` | 过滤被闸行 + `--include-gated` | P0 |
| `python/v4/07_source_health_check.py` | 新增（延迟审计 + 阈值扫描 + 现货断流专项） | P1 |
| `CLAUDE.md` | 运行参数段补 4 个 flag；关键设计决策补「延迟闸配置化 + 丢信号计数」「日亏熔断锁存/两模式同闸」 | P1 |

---

## 5. 测试与验收

**Go 单测**（沿用 table-driven 风格）：

1. `engine` 计数器：构造 tick 序列 → 断言 `BookStale/BookMissing/LostTriggers` 计数与明细；
   **并断言判定结果与改动前逐位一致**（同序列 `ProcessTick` 返回值不变）
2. `engine`：`MaxBookLatMs=300`（默认）下，`cmd/btreplay` 625 笔逐位一致 —— **硬验收**
3. `risk`：`CanTrade` 边界（既有 8 例）+ 锁存（`GatedOn` 命中后当日恒停）+ 跨日归零
4. `exec_state`：paper/live 同一 `todayPnl` 输入下闸判据一致；paper 被闸行仍 `IsFilled()==true` 且带 `gate_reason`
5. `recorder`：`winstats_*` 写入/重启载入；windows 加载器忽略统计行（防污染 σ）

**端到端**：

```bash
go build ./... && go test ./internal/... -v
go run ./cmd/btreplay ...            # 625 笔逐位一致（回归红线）
python/venv/bin/python python/v4/01_backtest_r1.py    # 头条仍是 n=625 / +395.8U
python/venv/bin/python python/v4/06_oos_review.py     # 复验结论不变
go run ./cmd/flip -output data/v4 -dashboard :8090    # 跑满 1 天
python/venv/bin/python python/v4/07_source_health_check.py
```

**跑满一天后的核对项**：

- `winstats_*.jsonl` 行数 ≈ 288/日（含被跳过窗口——这是可见性的关键）
- `ticks = ticks_valid + book_stale + book_missing` 恒等
- 若当日触发熔断：`gate_reason=daily_loss` 行出现，且其后当日 ok 行全部被闸
- 三源龄分布与 09-03~09-16 基线可比（book p50≈10ms / p99≈142ms）

---

## 6. 风险与回滚

| 风险 | 缓解 |
|------|------|
| 阈值误配（如 `--max-book-lat-ms 1`）打光信号 | 启动校验 + `<100` 警告 + 横幅打印生效阈值 |
| 计数器实现误动 ring 语义 → 破坏回测镜像 | 计数器只累加，**禁止**改 `pushSlots`/`decide`；btreplay 逐位对账为红线 |
| `winstats` 污染 σ 预热 | 独立文件 + windows 加载器 schema 守卫（双保险） |
| 被闸行混入 P&L 统计 | 02/06 默认过滤 + `--include-gated` 显式对照；`gate_reason` 字段可直接 grep 核对 |
| 熔断把纸面序列砍断（若选方案 C） | **不采纳 C** |
| 回滚 | 全量 flag 默认值 = 现行行为：不加 flag 时延迟闸与今日**逐位同行为**；熔断默认值变化是唯一"非中性"改动，回滚只需 `--max-daily-loss -20` |

**建议提交切分**（便于单独回滚）：

1. `flip:` 延迟闸配置化 + 观测 `spot_age_ms`（纯配置面，行为中性）
2. `flip:` 每窗 tick 健康度计数 + `winstats_*` + dashboard
3. `flip:` 日亏熔断两模式同闸 + 默认 −24 + 锁存 + `gate_reason`
4. `python:` 07 脚本 + 02/06 过滤被闸行
5. `docs:` CLAUDE.md 同步

---

## 7. 已确认决策（2026-09-16 用户拍板，动代码依据）

| # | 议题 | 结论 | 落法 |
|---|------|------|------|
| 1 | 需求 1 的力度 | **只做配置化 + 可见性**，默认阈值一个不动（book 300 / spot 2000 / TWAP 10000） | §2.2 三个 flag 的默认值 = 现行常量；`spot_age_ms` 与 `winstats_*` 是新增观测，不改判定 |
| 2 | §3.5 paper 语义 | **方案 A**：闸生效 + 被闸行照记照结算（写 `gate_reason`） | §3.5 已改为「已定」，实现要点见该节 |
| 3 | 熔断线 | **−24U 是终值**，不做线扫描脚本（`08_breaker_replay.py` 取消） | §1.5 的回放口径固化在文档；换线时按同口径手算 |
| 4 | 阈值收紧 | **不做**（除非 `07` 脚本将来给出反证） | §2.6 已列「明确不做」 |

### 7.1 关于「信号极少的天数」的结论（对应需求 1 的原始动机）

用户 2026-09-16 的假设是「信号少的天数很可能是现货延迟太高被过掉了」。复核结论
（完整数据见 §1.2(c)(e)）：

- **方向正确，量级不足**：14 天里因 spot 陈旧被闸、且 `rem > 180`（时间腿真能过）的
  行只有 **9 条**，其中过了急跌腿的只有 **5 条** ≈ 0.14 条/日 ≈ 信号量的 0.35%。
- **09-12 / 09-13 的少信号另有主因**：那两天的多数未触发窗口是 `rem_low`
  （触底发生在窗末 3 分钟内，时间腿本就不过），现货断流在两天的窗口级占比虽抬升
  （约 2 倍于基线），但不足以解释频率缺口。
- **真正没答案的是「看不见的那部分」**：book 延迟超 300ms 或整簿缺失的 tick
  **完全不留痕**，当前无法回答「有多少信号死在这一步」——这正好是 §2.4
  `winstats_*` 要补的口子，也是 09-15 OOS 复验里频率闸门（36.5/日 vs 44±5）
  查不下去的原因之一。
- **附带发现（比阈值更值得关注）**：现货龄是**记录有价、没有龄**——`spot_age_ms`
  缺失导致触发时刻的 spot 可能是 1s 前的价。用 σ 位移算：1s 的 BTC 位移
  就能吃掉 yes 侧 0.6σ 带宽的一大截，即**亚阈值（<2s）污染在四个都过的信号里
  也可能存在**。这是 §2.3 把 `spot_age_ms` 定为 P0 的直接依据。

### 7.2 动代码前的最后一件事

按 §4 变更清单实施；**第一条验收红线**是 `cmd/btreplay` 625 笔逐位一致，
第二条是 `01_backtest_r1.py` 头条仍为 n=625 / WR 24.6% / EV +0.633 / +395.8U。
两项任一不过 → 停手回退，不带病往下做。
