# 扫尾盘（tail sweep ⑤）回测 ↔ 引擎口径映射与运行说明（2026-09-23）

> 🔴 **2026-09-24 重大变更（整合改造，用户决定 `a.md`）**：现行引擎口径的**权威规格**是
> [tail_integrated_2026-09-24.md](tail_integrated_2026-09-24.md)，oracle 是
> [python/v4/23_tail_integrated.py](../python/v4/23_tail_integrated.py)。本文档已按它更新，
> 但**任何冲突以整合文档为准**。三处**已被取代**的段落（正文里逐条标了 ⚠️ 作废）：
> ① §3.1 的「两帧折中（rem≤150 只记录的帧 + rem≤60 快照）」→ **三段递进判定链**
> （T=150 判 ⑤ → 不达标 T=60 再判 ⑤ → 仍不达标则每秒判 ②，任一段出信号即整窗一单）；
> ② §2.4 的「监听对账行 `kind=scan`（只记录、不下单）」→ 监听段现在**真的下单**，
> `kind=scan` 成为 legacy-only（`18_tail_scan_register.py` **作废**）；
> ③ §2.3 的「快照 tick 缺 spot ⇒ 整窗丢弃」→ **只跳过该 tick**（等下一个可判定 tick）。
> 另有两处口径升级：空侧改为 **ask 优先 / bid 兜底**（决策 #21，纯 live 改动）；
> 结算注册面改为 **所有信号**（未成交行也回填官方结果，只是 P&L 恒 0）。

> 策略规格 = [tail_sweep_2026-09-22.md](tail_sweep_2026-09-22.md)（§1 规则本体的定义
> ——价格腿/位移腿/σ 腿——**未变**，仍以它为准；§2 的两帧折中与 §5.2 的判决卡口径已下线）。
> 唯一权威规则源 = [python/v4/13_tail_sweep.py](../python/v4/13_tail_sweep.py)（主回测）
> 与 [15_tail_sweep_union_sigma.py](../python/v4/15_tail_sweep_union_sigma.py)（联合格 σ / 门槛）；
> 整合后的三段链 oracle = [23_tail_integrated.py](../python/v4/23_tail_integrated.py)。
> 引擎实现 = [cmd/tail/main.go](../cmd/tail/main.go) + [internal/tail/](../internal/tail/)。
> 本文档固定**回测 ↔ 引擎记录 JSON ↔ 三个日志前缀**的映射，供纸面登记
> 与日常对账使用。与 dog@0.2 的关系：两族独立、方向相反，共用原语（`flip.Tick` /
> `Executor` / `HistState` / `CanTrade`），互不读写对方的记录。

## 1. 常量映射

| 回测（23 三段链 / 13 / 15） | 引擎 config 字段 | 配置键 | 默认 | 说明 |
|---|---|---|---|---|
| `T150 = 150`（第一段判定点） | `Tail.T150Rem` | `tail.t150_rem` | 150 | 首个「`rem ≤ 150` 且 spot 可算」的**有效** tick 判一次 ⑤，**达标即下单**（2026-09-24 前这里只记一行原始帧） |
| `T60 = 60`（第二段判定点 = 策略本体） | `Tail.T60Rem` | `tail.t60_rem` | 60 | 第一段没出信号时，首个「`rem ≤ 60` 且 spot 可算」的**有效** tick 再判一次 ⑤ |
| 监听段 `scan60`（16 的 B 变体） | —（复用 `Tail.T60Rem`） | —（**无独立键**） | 60 | 前两段都没信号 ⇒ 此后**每秒**判 ②（价格腿 ∧ `dev ≥ 63`），达标即下单（2026-09-24 前是只记录的对账行，见 §2.4） |
| `PRICE_MIN = 0.80` | `Tail.PriceMin` | `tail.price_min` | 0.80 | 热门侧**有效价**（ask 优先 / bid 兜底）≥ 此值——**全部五格与 ② 的前置**（见 §3.1） |
| `DEV_USD = 63` | `Tail.DevMinUSD` | `tail.dev_min_usd` | 63 | 位移腿：`dev ≥ 63 美元` 即放行 |
| `SIGMA_USD = 40` | `Tail.SigmaMinUSD` | `tail.sigma_min_usd` | 40 | σ 腿门槛：`sd ≥ 40 美元` 才允许「dev ≥ sd」放行（⑤ 相对 ④ 的唯一差别） |
| `STAKE = 2.0` | `Tail.Stake` | `tail.stake` | 2 | USDC/注。paper 股数 `= Stake/HotAsk` **精确除**（与回测 `shares = 2/fill` 恒等）；live 下单量由 `trading.OrderSpecForObs` 取 `floor2`（既有口径） |
| `MAX_LAT = 300` | `Tail.MaxBookLatMs` | `tail.max_book_lat_ms` | 300 | `book_latency_ms > 300` 的 tick 无效。与 `flip.max_book_lat_ms` 同值但**独立成键**（两族可分别部署/分别标定） |
| hist ≤18 窗、≥3 可用 | `flip.HistState`（共用类型） | —（常量） | 18 / 3 | σ 滚动窗；`flip.HistMin = 3`，`hist.Count() < 3` ⇒ 整窗跳过 `skip=no_sigma` |
| spot 龄上限 | `Feed.MaxSpotAgeMs` | `feed.max_spot_age_ms` | 2000 | 超龄 ⇒ `spot ≤ 0` ⇒ 该 tick **只跳过、不推进任何段**（2026-09-24 前是整窗丢弃，见 §2.3） |
| — | `Risk.MaxDailyLoss` | `risk.max_daily_loss` | −24 | 日亏熔断（两模式同判据 + 当日锁存；paper 只标记不拦单） |
| — | `Runtime.OutputDir` | `runtime.output_dir` | `data/v4` | 三个前缀都落这里（**前缀不撞**，见 §2.1） |

⚠️ **全部策略参数不可调**（文档 §4.3）：`40` 这个门槛在 2000 次日期重采样里
一次都没成为最优（真 argmax=35），`63` 是本 14 天 1σ 折美元的中位数（换波动率
regime 会失义），`T=60` 的标定只在该 T 上成立。配置键的意义是「能读能对账」，
不是「该调」——改任何一个都等于换一条没标定过的策略。

## 2. 记录映射

### 2.1 三个前缀（独立于 flip 的 `touches_` / `windows_` / `winstats_`）

| 文件 | 粒度 | 用途 |
|---|---|---|
| `tail_YYYY-MM-DD.jsonl` | 每窗 ≤ 3 行（全部 `kind=snap`, 按 `stage` 分三段） | 纸面登记；判定行 / 信号行 / 成交 / 结算全在这（`kind=frame`/`scan` 为 legacy-only，引擎不再产出） |
| `tailwin_YYYY-MM-DD.jsonl` | **每完成窗 1 行** | tail 自己的 σ 预热源（`flip.WindowEntry` 形状） |
| `tailstats_YYYY-MM-DD.jsonl` | **严格每窗 1 行** | tick 健康度 + `skip` 原因 + 锚状态（§5.2 辅助闸门 3） |

⚠️ `tailwin_*` **不得**与 `windows_*` 共用文件：两个进程（flip / tail）双写同一文件
会以「不属于本引擎的振幅」污染 σ；且 `tail_*` 行混进 `tailwin_*` 会以 `Amp=0`
污染其后 18 窗（CLAUDE.md 决策 #9 的教训）。三个前缀经 recorder 的 schema 校验
（`event_type == "tail"`）与独立文件句柄隔离，测试里有「三前缀互不串读」回归。

### 2.2 字段映射（`tail_*` 行 ↔ python 快照）

| python（23_tail_integrated.py / 13_tail_sweep.py） | 引擎 `Observation` json | 说明 |
|---|---|---|
| `event_start` | `event_start` | 窗口起点 unix 秒（**对账主键**） |
| （按段的判定点） | `kind` + `stage` + `frame_t` | `kind` 恒 `snap`；`stage ∈ {t150, t60, listen}`（空 = legacy 旧行）；`frame_t` = 本段的时间腿（t150→150 / t60·listen→60） |
| — | `ts` / `rem` | 该段 tick 的采样时刻（unix 毫秒）/ 剩余秒（`≤ frame_t`） |
| `yes_bid/yes_ask/no_bid/no_ask` | 同名字段 | 四档报价（§5.3 原始字段，离线可复算任意 T） |
| `spot` | `spot` | Binance 最新价（0 = 缺失/陈旧 >2s） |
| `twap_price` | `twap` | TWAP-60 流值（0 = 缺失；⑤/② 都不用**它**，仅诊断） |
| `twap_open_price` | `anchor` | 本窗锚（**恒 > 0**：无锚整窗不产出，见 §3.2） |
| `hist_bps` | `hist_bps` | 本窗生效 σ（bps） |
| — | `book_latency_ms` / `spot_age_ms` / `twap_age_ms` | 诊断（§5.3 要求） |
| `side`（热门侧） | `side` | yes/no；按**有效价**比大小（`有效价 = ask > 0 ? ask : bid`，**平局取 yes**） |
| `fill` | `hot_ask` | 热门侧**有效价** = 成交价口径（ask 优先、bid 兜底，见 §2.5） |
| — | `hot_src` | 有效价来源：`ask` / `bid`（空 = legacy 行）。`bid` = 那侧 ask 被整侧撤空, live 大概率不成交 |
| `d_spot`（∈ 美元与 bps 两处语义） | `dev` | 位移（**美元**，正 = 朝押注方向）；0 且 `omitempty` = 未计算 |
| `sd`（1σ 折美元） | `sd` | `= hist_bps × anchor / 1e4`（美元）；0 且 `omitempty` = σ 不可用 |
| `sig`（bool）/ 各格过滤 | `rules` + `ok` + `reject_reason` | 四条原始腿 + 派生 `Rule1()…Rule5()`；**规则变量一律美元**，不引 bps 中间量 |
| `in_*` 各格 | `ok` | `ok=true` ⇔ 该段放行（t150/t60 = ⑤ 通过；listen = ② 通过）——**唯一会下单的行** |
| `settle_won` | `won` | 结算后回填（官方 outcome；**未成交行也回填**，见 §5.5） |
| — | `pnl` / `shares` / `stake` / `cost` | 结算后回填 / 决策时计算（`cost` 仅 live）；**无仓位行 `pnl` 恒 0** |
| — | `settle_src` | **结算来源**（2026-09-24 起，§5.5）：`push` / `official` / `gamma`；空 = 旧行 |
| — | `gate_reason` | 风控闸（`daily_loss` / `first_window`）。⚠️ 2026-09-24 起被闸行 = `exec_status=rejected` + **无仓位** |
| — | `exec_status` / `order_id` / `avg_fill_price` / `cost` / `exec_note` | live 执行回填（paper 行恒空） |

### 2.3 ⚠️ 两个容易写错的口径（已固化在代码里）

1. **σ 腿必须用显式布尔**，不能靠 `sd > 0` 判：`sd = 0`（无历史）会让 `dev ≥ sd`
   恒真，③④ 会在无历史窗口全放行。引擎里 σ 可用性由 `histBps > 0` 单独传入
   `EvalRules`，且现网还有前置闸（`no_sigma` 整窗跳过）使其不可达。
2. **缺 spot 的 tick 只跳过、不推进任何段**（2026-09-24 起）——等下一个可判定 tick。
   旧口径（→ ⚠️ **作废**）是「快照 tick 缺 spot/twap ⇒ 整窗丢弃」：python 的 `continue`
   落在外层事件循环，搜索在首个有效 tick 就停了，故 n=1536 那个时代的口径必须与它一致。
   现在两处都不再要求 twap（⑤/② 都不用 twap），且 14 天数据里首个判定 tick 上的
   `spot` **零缺失** ⇒ 新旧口径在历史数据上是 **0 窗**差异（`missing_spot` 只剩纯函数防线
   意义）。oracle 的宇宙过滤因此也只要求「spot 在场」（见 §5.3）。

### 2.4 ⚠️ **作废**：监听对账行 `kind=scan`（2026-09-23 曾用，2026-09-24 删除）

**历史（保留以便读旧数据）**：2026-09-23 的引擎里，「监听口径」是与快照口径 A 对照的
**反事实样本**——`rem ≤ 60` 起逐 tick 找第一个 ⑤ 达标的 tick，落一行 `kind=scan`
**只记录、不下单**（`ExecState.HandleScan` 不碰 Executor、不调 `gate()`），但**照常结算**
（否则「快照被拒」的窗口拿不到 outcome，就没法与 A 配对）。14 天里 B 比 A 只多
**+7.00U**、日级 bootstrap 95% 区间 **[−9.1, +22.5] 跨 0**（n=485，WR 98.14%）。

**2026-09-24 起作废（`a.md` 第 1 条）**：监听段从「对照物」升格为**策略本体**——
三段链的第三段，达标即**真下单**。故：

- 引擎不再产出 `kind=scan`（`KindScan` 常数保留为 **legacy-only**，`isKnownKind` 继续接受，
  历史 `data/v4-tail/` 旧行仍可载入、仍被正确分类为「无仓位」——见 §5.5）；
- `python/v4/18_tail_scan_register.py`（A/B 配对判定）**作废**：A/B 之分不再存在；
- Dashboard 的 `/api/scans` 路由与「监听对账」表删除；
- 旧行在统计里的正确待遇：`HasPosition() = Kind != KindScan ∧ IsFilled()` ⇒ 旧 scan 行
  **恒无仓位**，P&L 恒 0、不进胜率（这正是它当初的设计意图：假想股数不是仓位）。

### 2.5 空侧口径：ask 优先 / bid 兜底（2026-09-24，纯 live 改动）

**背景（决策 #21，`book_empty_ask_2026-09-24.md`）**：行情一边倒时**赢家侧的 ask 会被整侧
撤空**（实盘探针 4 窗，空侧起始 rem = 12/28/45/47，一直空到收盘），此时该侧只剩 bid。

新口径：**每侧有效价 = `ask > 0 ? ask : bid`**，热门侧 = 有效价高的一侧（平局取 yes）；
价格腿、股数 `stake/有效价`、`hot_ask` 字段、live 的 GTC 限价**全部用有效价**；
**四档全空**才算「无有效盘口」（该 tick 无效，计入 `book_missing`）。

- 落盘 `hot_src ∈ {ask, bid}` 审计；`bid` 是红旗：paper 按「限价即成交」记账（**偏乐观**），
  live 实际是挂单等成交、大概率不成交。
- **回测/parity 数字不受影响**：14 天 542800 个 tick 里「四档**部分**缺」**0 次**
  （只有 361 个整簿全空，两套门一致判无效）⇒ python 的「四档齐全」门与 Go 的「有效价」门
  在历史数据上同源。parity 用「全部行 `hot_src == ask`」把这条钉住，一旦分家测试立刻红。

## 3. 与 dog@0.2 引擎的结构差异（有意为之）

### 3.1 三段递进判定链（用户决定，2026-09-24）

```
Watching ──首个「rem ≤ t150_rem(150) 且 spot 可算」的有效 tick──▶ [判 ⑤]
   ├─ OK   → 落信号行（下单）→ Done
   └─ 拒绝 → Await60
             （首个可判定 tick 已在 rem ≤ t60_rem 时: 整段跳过, **不伪造 t150 行**）
Await60  ──首个「rem ≤ t60_rem(60) 且 spot 可算」的有效 tick──▶ [判 ⑤]
   ├─ OK   → 落信号行（下单）→ Done
   └─ 拒绝 → Listening
Listening ──此后**每秒**: 有效 tick ∧ ② 达标──▶ 落信号行（下单）→ Done
   └─ rem == 0 → Done
```

- **任一段出信号即整窗只下一单**，之后不再判定（与「每窗最多一个仓位」的既有约束一致）。
- **两个判定段各落一行**（成功与否都落盘），**监听段只在达标时落行**（否则每窗白写 ~60 行）。
  ⇒ `tail_*` 每窗 ≤ 3 行。
- **历史沿革**（→ ⚠️ 作废）：2026-09-23 的方案是「两帧折中」——`rem ≤ 150` 首帧
  `kind=frame`（**只记录**，不判定不下单）+ `rem ≤ 60` 首帧 `kind=snap`（判定+执行），
  外加 §2.4 的 `kind=scan` 对账行。三行里只有 snap 是交易路径。新三段链把 frame/scan
  两个「只记录」的角色一并取消：**三次都是真判定**。
- **锚仍共用同一个**（首行落盘即冻结，`UpgradeAnchor` 此后拒收）——否则 t150 行与
  t60 行的 `dev` 基准不同源。**代价同样保留：T 不可再调**（离线只能复算 150/60 两个值，
  且 60 之后是逐秒的 ②）。
- **崩溃重启续跑**：`Engine.Resume(t150Done, t60Done)` 按磁盘真相回填已完成段
  （两个都完成 ⇒ 直接进 Listening），已判过的段不重判；重入的判据是该窗**是否已有
  OK 行**（`Recorder.HasSignal`）——有就整窗跳过（防同窗双单）。

### 3.2 锚：精确取锚 + 「产出任何行之前已定局」

与 flip 同口径（决策 #15）：锚 = 边界那一秒的 TWAP 推送，`feed.RecoverAnchor`
500ms × 40 = 20s 预算，`BeginWindow(0, 0)` **不设过渡锚**。
tail 独有的时序性质：取锚通道在边界 **+20s（rem≈280）** 就结束，而最早的行在
**rem ≤ 150（边界 +150s）**——**锚在产出任何一行之前就已定局**，不存在「前段用旧锚、
后段用新锚」的不一致窗口。故 `anchor ≤ 0` ⇒ 本窗一行不产出（只在 `tailstats` 里
记 `anchor_exact=false`），与「宁可丢窗也不拿近似锚判定」一致。

### 3.3 σ 冷启动整窗跳过

`hist.Count() < 3` ⇒ `skip=no_sigma`：不预取、不订阅、不产出任何行（决策 #13 同款）。
`tailwin_*` 本地预热优先（`RecentBlock` 截连续块 + 15min 新鲜度），不足回退
`feed.FetchTwapRanges` 网络预热。

### 3.4 风控闸与撤单点

- **风控闸**：`flip.CanTrade` + 当日锁存，两模式同判据（决策 #10）。⚠️ 这比文档
  §5.3 写的「本族不接入风控闸」多一层——那句是「只登记不下单」时期写的；既已做
  live 代码，就按现行两模式同闸执行。
  ⚠️ **2026-09-24 起被闸行两模式统一为 `exec_status=rejected` + 无仓位**（a.md 把
  「被风控拦」归入**未成交**）——本族的「方案 A」（paper 被闸行照记照结算、并因此算持仓）
  **作废**；flip 侧仍是方案 A。**熔断的锁存判据不变**（`Recorder.GatedOn`，磁盘真相，
  跨日自动归零、重启自动恢复）。
- **撤单点 = 闭市（`rem ≤ 0`）**（用户决定，2026-09-23）：`trading.CancelAtClose`
  （= `time.Nanosecond` 哨兵，**必须微小正数**——`≤0` 会被 `NewFillTracker` 当成
  「未配置」回退 180s，而 tail 在 `rem≈60` 才挂单，回退会让第一轮轮询就撤单）。
  硬截止 = 闭市 + 60s 宽限（撤单一直失败时的兜底）。

## 4. 与回测的已知差异（写在这里，免得日后当新发现）

| 维度 | 回测 | 引擎 | 影响 |
|---|---|---|---|
| **成交** | 判定瞬间按热门侧**有效价**成交 | **paper**：同回测（`shares = Stake/HotAsk` 精确除）；**live**：GTC 限价挂单，挂到闭市 | live 的成交样本天然偏向「热门侧在尾盘走弱」的子样本（反弹回去的单子根本不成交）——**与回测不是同一个估计量**。**首次上线只跑纸面** |
| **空侧兜底** | 宇宙要求四档齐全（`all(k > 0)`） | 每侧**有效价** = `ask > 0 ? ask : bid`；四档全空才算无效 | 14 天里「四档部分缺」**0 次** ⇒ 回测数字不受影响（§2.5）；差异只在 live（赢家侧 ask 被撤空时, 回测会整窗丢弃、引擎照判） |
| 锚 | 官方 `twap_open_price` | 边界那一秒的 TWAP 推送（= 官方 openPrice 口径，8/8 窗逐位相同） | 已对齐（决策 #15） |
| σ | 前 ≤18 个**事件**索引的 `|close−open|` 均值（缺端点的窗仍占索引位、不贡献） | `flip.HistState`：最近 ≤18 个**已 push** 振幅 | 两者在「窗口缺锚/缺 close」时条数不同（`RecentBlock` 截断 + 跳过窗都会少 push）。对账测试里 σ 按 python 语义现算后注入，故各段数字逐位一致；现网差异上界 = 每 18 窗最多几窗 |
| close | 官方 `twap_close_price` | 窗末 tick 的 TWAP 流值（到达口径，龄 >10s 判缺失、本窗不计 σ） | open/close 有 ~1.5s 不对称，**已知未做**（与 flip 同款遗留） |
| tick 相位 | 数据行内 rem | 1s ticker，相位 ±1-2 tick | 各段的闸都是 `≤` 判定 + 取首个有效 tick，相位只影响落在闸上那一两秒——记录时按 `ts` 对齐，勿用 `rem` 逐笔对账 |
| **频率** | 三段合计 2135 注/14 天 ≈ 153 注/日（§5.3） | 同 oracle；每窗最多 1 注 | 旧口径「只在 T=60 判 ⑤」= 1536 注/14 天 ≈ 110 注/日。§5.2 的频率闸门 90~120 注/日是按旧口径定的，三段链下**必然超标**（三倍频次的设计意图），该闸门随之作废 |
| **跨进程熔断** | — | flip 与 tail 各自一条独立 24U 日亏线（等效 48U） | 文档 §4.7 要求两族同开实盘时**仓位与熔断必须合并**——**未实现**，只写在这里。要做需另开特性（共用账本 / 单进程双引擎） |
| **监听口径 B** | ~~`scan60∖snap60` 增量（n=485, WR 98.14%, +7.00U）~~ | 监听段 = 三段链第三段（**真下单**） | A/B 之分消解（§2.4 作废）；旧对照结论只留在历史 |

## 5. 运行、验收与纸面登记

### 5.1 运行

```bash
# 纸面（无需凭证；与 flip 并行跑不冲突——前缀不撞、无共享状态）
go run ./cmd/tail -config v4.config.yaml -dashboard :8091

# 单点覆盖: -config / -mode / -stake / -dashboard（比 flip 少了 -encrypt）
go run ./cmd/tail -config config.local.yaml -stake 2 -mode paper -dashboard ""
# Dashboard 地址来自 runtime.tail_dashboard_addr（**独立于** flip 的 dashboard_addr），
# 空串 = 不开——两个进程各自的 listener、各自的静态目录与 API 前缀（见 §5.4）

# 交叉编译交付（build.sh 自动探测 cmd/）
./build.sh tail
```

⚠️ 本地跑记得 `export https_proxy=http://127.0.0.1:1087`（Polymarket 直连超时）。
⚠️ **live 务必换 `runtime.output_dir`**（如 `data/v4tail-live`）：与纸面同目录会把
live 行混进 `tail_*.jsonl`（前缀相同、无法事后区分），且启动时会打印混行告警。

启动后自查（当日文件）：
- `tail_*.jsonl`：每窗 ≤3 行（两个判定行 + 至多一条信号行，全部 `kind=snap`）；
  有 `ok=true` 就该在结算后看到 `won` 回填（**未成交行也有 `won`**，只是 `pnl` 恒 0）
- `tailwin_*.jsonl`：每完整窗口 1 行（行数 ≈ 当日已跑窗数）
- `tailstats_*.jsonl`：**严格每窗 1 行**（≈288/日）——行数本身就是「主循环跑满」的证据；
  `skip` 取值：`late`（迟到跳窗）/ `no_market` / `no_token` / `dup_record` / `no_sigma`

### 5.2 判据：旧判决卡口径**作废**，改由离线脚本做

文档 §5.2 的「14 个完整 UTC 日且 n ≥ 800 + 日级 bootstrap 95% 区间」仍在，但**判决卡
（`/api/judge` + Go 侧 `internal/tail/judge.go`/`mt19937.go`）已随整合改造整删**
（a.md 第 4 条：删「判决速览」）。三条连带变化：

- **改成离线脚本做**：吃 `tail_*.jsonl` 的信号行（只计 `HasPosition()` 的行），
  oracle `python/v4/23_tail_integrated.py` 第一节已经给出三段与合计的日级 bootstrap 区间
  （14 天回测口径）。**纸面判决脚本本次未写**——见 `tail_integrated_2026-09-24.md` §6 待办 1。
- **辅助闸门 ①（频率 90~120 注/日）作废**：那是按旧口径（只在 T=60 判 ⑤，110 注/日）
  定的；三段链 = 153 注/日，**必然超标**（三份信号本来就是这个设计的目的）。
- **辅助闸门 ②③ 保留**：30 笔抽样核对官方 outcome；锚/σ 就绪度看 `tailstats_*`
  （`anchor_exact=false` 或 `hist_bps=0` 的窗整窗不产出）。

### 5.3 验收红线（2026-09-24 全绿）

```bash
go test ./internal/... -race                      # 全绿（含 flip/trading/config 既有回归）
go test ./internal/tail/ -run TestParityBacktest  # 14 天全量对账（opt-in, data/btc 不存在则跳过）
```

`parity_test.go` 流式重放 `data/btc/events_*.jsonl`（3753 窗）驱动 `internal/tail`
的**真实引擎**（规则判定不复制，只喂数据 + σ 注入），逐位断言 oracle
`python/v4/23_tail_integrated.py` 第六节的表：

| 段 | 全部行 | 信号 n | WR | P&L（14 天，2U/注） |
|---|---|---|---|---|
| T=150（⑤） | 3638 | 1220 | 93.9344% | +16.0226U |
| T=60（⑤） | 2414 | 568 | 99.1197% | +15.7562U |
| 监听（②/秒） | 347 | 347 | 97.9827% | +3.8959U |
| **合计** | **6399** | **2135** | **95.9719%** | **+35.6747U** |

外加三条钉死的口径：参与判定 **3640** 窗 / σ 未就绪整窗跳过 **3** 窗（决策 #13，引擎前置闸
同源）/ **全部行 `hot_src == ask`**（= §2.5 的「回测宇宙四档齐全」与引擎「有效价」门同源的
证据，一旦分家测试立刻红）。

⚠️ 两条对账口径（不变）：① 宇宙过滤按 oracle（只要求「spot 在场」+ 四档齐全 + `rem ≤ 150`）；
② σ 必须**按事件索引**现算后注入（python 语义），**不能喂 `flip.HistState`**
（后者只记已 push 的振幅，缺窗时条数不同）。

**旧 parity 表（五格 + 三闩锁计数，2026-09-23 时代）已删除**：它断言的是旧两帧/三闩锁口径
（① n=3109 … ⑤ n=1536 + frame 3643 / snap 3634 / scan 485）。那些数字在历史
`docs` 与 git 里仍可查，但引擎不再产出对应的行类型。

### 5.4 Dashboard 口径（2026-09-24 改版：判决卡全删）

`cmd/tail -dashboard :8091`（或配置键 `runtime.tail_dashboard_addr`）起一块只读面板：
**页面不参与判定、不写任何文件**，全部读数现算。五个口（删 2 增 1）：

| 路由 | 内容 |
|---|---|
| `/api/state` | 当前窗口（热门侧/`dev`/`sd`/锚与 σ/**四个闩锁**/三源新鲜度）+ 统计汇总 + **今日健康度**（读当日 `tailstats_*`） |
| `/api/snaps` | **判定行**分页（t150/t60，成功 + 否决，各带 `stage` 与结算回填） |
| `/api/signals` | **信号行分页**（`ok=true`；判定表与它的差别 = 这里只看真正下单的行） |
| `/api/daily` | 逐日：判定 / **T150** / **T60** / **监听** / 注数 / 未成交 / 待结算 / 胜率 / P&L |
| `/api/config` | `tail.*` 7 键（键名 = mapstructure tag，前端展示标定参数） |

~~`/api/judge`（判决速览本尊：①~⑤ 五格 + T=150 对照格 + 监听增量 B∖A 格）~~ /
~~`/api/scans`（监听对账行）~~ / ~~`/api/frames`（原始帧行）~~ **三个路由已删除**，
`internal/tail/judge.go` 与 `mt19937.go`（含测试）随之整删（a.md 第 4 条）。

**页面统计口径（与 §3 的分类恒等式同源）**：

```
signals = won + lost + pending + noexec      ← 分类恒等式, 逐日表也用它
won / lost / pending  要求 HasPosition()（实际成交 ∧ 非 legacy 对账行）
noexec = ok ∧ ¬HasPosition()（被闸 / 下单被拒 / 0 成交 / 挂单未定稿）
胜率分母只含 won + lost（未成交不进胜率 —— a.md 第 4 条）
```

页面其余三处口径（保留自旧版，逐条仍成立）：

1. **σ 用的是落盘时的本窗值**（`hist_bps`），与 python 按事件索引现算的口径一致；
   但页面**实时**读数（`/api/state` 的 `dev`/`sd`）用的是**当前**盘口与 spot——同一窗内
   与落盘行不会逐位相同（落盘行是该段 tick 那一刻的值）。**判定路径不读页面**。
2. **今日健康度只读当日 `tailstats_*`**（本族**唯一**的磁盘读口）：窗数 / `skip` 分布 /
   `anchor_exact=false` 计数。健康度行**不载入内存**（纯审计），故 `/api/state` 每次请求
   现读；读失败只在日志留痕、不阻断状态页（窗数恒 0 即信号）。
3. **`hot_src == bid` 的行在信号表里可辨**（ask 列带 `*` 与 tooltip）——它是「paper 偏乐观、
   live 大概率不成交」的那批（§2.5）。

### 5.5 结算口径（2026-09-24 起：推送优先的三层回退，与 flip 同一套）

两族**共用同一个结算编排**（`internal/settle`，由 `cmd/tail` 与 `cmd/flip` 各自构造注入；
`internal/tail` 本身不 import 它）。原先的「落盘即注册 `ResolutionPoller`」已删除——
待结算行由 `Recorder.PendingSignals()` 统一暴露，`settle.Resolver` 按三层回退取官方 outcome：

| 层 | 触发时点 | 依据 | 落 `settle_src` |
|---|---|---|---|
| ① 推送自算 | 闭市 **+10s** | 边界 N 与 N+300 那两秒的 TWAP 推送——实测与官方 `openPrice`/`closePrice` 逐位相等 | `push` |
| ② 官方接口 | 闭市 **+45s** | crypto-price API（须已收敛） | `official` |
| ③ gamma 轮询 | 约 **+75s** | UMA 结算本身 | `gamma` |

（① 层原为 +25s，2026-09-24 下调到 +10s：实盘 880 个命中窗里闭市那条推送的到达延迟只有
1s/2s 两档、无一晚于 2s。② 层的 45s 门槛与 PushWait 无关，丢推送的窗口时点不变。）

对本族的三条具体影响：

1. **注册面 = 所有信号**（2026-09-24 起，a.md 第 3 条）：`isSettlable` = `kind ∈ {snap, scan} ∧ ok
   ∧ 未结算 ∧ conditionID 非空`，**唯一排除项是未定稿的 `submitting`/`resting`**。故被闸行、
   下单被拒行、0 成交行**也会拿到官方 outcome 并在页面上显示赢/输**——只是 P&L 恒 0
   （`recomputePnL`：`if !HasPosition() { pnl = 0 }`）。
   P&L 隔离红线**不受影响**：`DailyPnl`/`MaxDrawdown`/`LiveSummary` 仍按 `HasPosition`
   （= 有仓位）过滤，未成交行**不动日亏熔断**。
2. **判据是 `HasPosition()` 而不是 `IsFilled()`**：两者差在 legacy 的 `kind=scan` 对账行
   （§2.4）——它恒无仓位，但 `ExecStatus` 为空又会被 `IsFilled()` 判成「paper 模拟成交」。
   只按 `IsFilled()` 过滤会让历史目录里的 scan 行混进胜率与 P&L。
3. **live 的 GTC 挂单行**（撤单点 = 闭市 `CancelAtClose`）在 FillTracker 定稿回调里才进
   队列，比闭市稍晚；resolver 按 `event_start` 算时点（不是按入队时刻），故晚入队不影响
   它落在哪一层。⚠️ 竞态已堵：resting 行可能先被结算（闭市 +10s）后由 FillTracker 定稿——
   `Resolve`/`CompleteExecution`/`CompleteRestingFill` 共用 `recomputePnL`，定稿时若已结算
   会**补算** P&L，否则会出现「成交了却永远 0 盈亏」。

⚠️ **live 行的入队时刻（写进代码注释）**：本族挂单撤到闭市，定稿在闭市后第一轮轮询
（≤2s）+ 撤单确认宽限（至多 15s）之内 ⇒ 正常落在 **闭市 +2s ~ +17s**，**晚于** ① 层的
+10s 闸；此时下一轮扫描立刻按 `now − 闭市` 判层——**三层判据只看时钟，不看入队时刻**，
晚入队不改变它属于哪一层（① 层要的两条边界推送那时早已在缓存里）。

## 6. 复现

```bash
python/venv/bin/python python/v4/23_tail_integrated.py         # 现行 oracle：三段链 n/WR/P&L + 逐日 + parity 表
python/venv/bin/python python/v4/13_tail_sweep.py              # 主回测 / 候选格排名（规则本体标定）
python/venv/bin/python python/v4/14_tail_sweep_sigma.py        # σ 尺子 / 窗长 / 日级 bootstrap
python/venv/bin/python python/v4/15_tail_sweep_union_sigma.py  # 联合格 σ / 门槛稳定性
python/venv/bin/python python/v4/18_tail_scan_register.py      # ⚠️ 作废（scan 行不再产出, 见 §2.4）
go test ./internal/tail/ -run TestParityBacktest -v            # Go↔py 逐窗对账（钉 23 的 oracle）
```
