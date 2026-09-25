# 采集管线回归 + 多标的参数化（2026-09-25）

> 对应 CLAUDE.md 决策 #23。
> 一句话：把 `eth` 分支的 `cmd/collect` / `cmd/compact` / `internal/collect` 同步回 `v4`，
> **同时把 BTC/ETH 硬编码换成资产参数**（后面还要采 SOL / BNB），并首次落盘 ETH 数据
> 并用 `python/v4/24_asset_data_check.py` 对其做外部真值校验。

---

## 1. 为什么同步而不是复制一份

`v4` 清理时把采集管线整体删了（引擎跑纸面不需要它）；但策略侧要验证「狗@0.2 / 扫尾盘」
在别的标的上是否成立，就必须先有那些标的的 1s 数据。`eth` 分支上的是**唯一在跑的
采集器**，且它比 v3 多了 25 个提交。取舍：

- **同步 eth 版，不是 v3 版**。`eth` 版的行字段名是 `yes_*`/`no_*`，与 `data/btc` 现存
  3753 窗、以及 `python/v4` 全部脚本（`01`/`06`/`07`/`23`…）的读法**逐字一致**；
  v3 版是另一套命名，同步进来会让所有历史脚本立刻读不动。
- 同时把 v3 分支上、eth 分支缺的三处修复一并移植：
  ① Binance 启动重试（首拨失败真的返回错误，不重试则整个进程生命周期内 Binance 数据
  流是死的）；② `SettlementWorker.Submit` 的 `done` 通道守卫（worker 退出后阻塞入队会
  让关闭流程挂死）；③ 关闭路径直接 `WriteUniqueEvent`（不走队列）。

---

## 2. 与 eth 原实现的三处**有意差异**

同步不是照抄：eth 版的口径与 v4 引擎现行口径已经不一致，直接搬会让新采的数据无法与
引擎/回测对齐。三处改动都有证据，列在这里备查。

### 2.1 边界价改「精确推送」，删掉「流采样 close + 官方 30s 轮询」

| | eth 原实现 | v4 现行（本同步） |
|---|---|---|
| 开盘价（锚） | `Latest()`（**到达**口径，边界当场只能看到「边界前一秒」的评估值）+ 官方 HTTP 轮询 | `PushNearest(边界)` **精确等值匹配**（评估时刻 == 边界那一秒），500ms × 40 = 20s 预算 |
| 收盘价 | `Latest()` 流采样 + 官方轮询覆盖 | `PushNearest(闭市)`，10s 预算（实测该推送 p50 到达 +1.4~2.0s、无一晚于 +2s） |
| 何时拉官方 | 每窗都拉（30s 轮询，直到取到） | **只在推送缺失时**（`needs_correction`）才排队 |

依据是决策 #19 的实测：官方 `openPrice(N)` ≡ 边界那一秒的推送 `anchor(N)`
（**315/315 逐位相等**）、官方 `closePrice(N)` ≡ `anchor(N+1)`（**309/309**）——对齐是
定义级相同，不是近似。而官方接口**头几十秒给的是未收敛的临时值**（决策 #14：p90 误差
1.18bps ≈ 9.5 美元），所以「早点拉官方」这条路本身是错的。

**顺便省掉的**：eth 版每窗都要打几十次 `crypto-price`（本机实测该接口还会 429），现在
正常窗**一次都不打**。

### 2.2 空侧盘口不再丢弃整条 book 消息（决策 #21）

eth 版（其实是 v4 引擎删除前的写法）里：

```go
if book == nil || len(book.Bids) == 0 || len(book.Asks) == 0 { continue }
```

赢家侧尾盘会被**整侧撤空 ask**（决策 #21 实测），此时这条「整簿快照」被整个丢掉 ⇒
内存里留着撤单前那一份旧簿 ⇒ 采样一路报 0.95~0.99 的假价。修法：守卫只留
`book == nil`，空侧照存 ⇒ `BestAsk` 返回 0 ⇒ 该 tick 四档缺一，与回测宇宙同口径。

⚠️ **这会改变数据本身**（ETH 采到的无效 tick 占比会高于旧 BTC 数据的 3.04%），
是**对齐回测口径**而不是退化。

### 2.3 删 `MinRange` / `MaxStreamAgeMs`

这两个阈值是为「流采样 close vs 官方边界值」的量化误差标定的（`|close−open|` 小到可能
翻转 outcome 时才去拉官方）。改用精确推送后 close 与官方**是同一个数**，量化误差归零
⇒ 修正的唯一触发条件变成「这一窗没拿到推送」，与幅度/新鲜度无关。`SettlementConfig`
只剩 `PollInterval` / `MaxWait` / `MaxConcurrent`。

### 2.4 删 `internal/collect/writer.go`

`JSONLWriter` 全仓零调用者（eth 分支里也是死代码），同步时不留。

---

## 3. 硬编码消除：`internal/feed.Asset`

同一条市场在三个子系统里各有各的名字，全部由**资产名**派生：

```
btc → slug btc-updown-5m / Binance BTCUSDT / Chainlink BTC / 目录 data/btc
eth → slug eth-updown-5m / Binance ETHUSDT / Chainlink ETH / 目录 data/eth
sol → slug sol-updown-5m / Binance SOLUSDT / Chainlink SOL / 目录 data/sol
```

`internal/feed/asset.go` 提供 `AssetFor(name)` / `AssetFromSlug(slug)` /`Asset.DataDir()` /
`Asset.ApplyBinance(cfg)`，四条消费路径全部接线：

| 消费方 | 怎么接的 |
|---|---|
| `cmd/collect` | 新增 `-asset` / `-slug` / `-symbol` / `-output` 四个 flag，优先级 `-slug` > `-asset` > `runtime.slug_prefix`；`twapAdapter` / `pairFetch` / Binance 交易对 / 输出目录全部由 `asset` 派生 |
| `cmd/flip` | `feed.AssetFromSlug(cfg.Runtime.SlugPrefix)` → TWAP 符号 / Binance 交易对 / `PricePairFetcher` / `FetchTwapRanges` 四处 |
| `cmd/tail` | 同上四处（镜像） |
| `config` | `binance.symbol` 默认值改成 `""` = **留空即派生**（显式填则优先，应对引号货币不是 USDT 的标的，如 `1000XXXUSDT`） |

`v4.config.yaml` 的漂移守卫（`configfile_drift_test.go`）已同步更新——它是逐键
`DeepEqual`，改默认值不改 YAML 会让测试红。

⚠️ `sdk.CryptoPriceSymbol` 本身只是个 `string` 类型（SDK 里为 SOL/BTC/ETH/XRP 提供了
常量，但任何资产名都能直接转），所以派生不需要维护映射表。

---

## 4. 一个必须记下来的口径陷阱：两个包的 `"stream"` 不是一个意思

同步后立刻踩到：`feed.AnchorSourceStream` 的字面值是 `"stream"`，但它的语义是
**「评估时刻恰好等于边界的那条推送」= 精确边界推送**（决策 #15 里 `anchor_src` 取
`official|stream` 的历史命名）；而 `collect.SourceStream` 的 `"stream"` 是**到达口径
采样**（`Latest()`，可能陈旧 ~2s）。

采集侧原本直接 `anchorSrc = rec.Source` 落盘 ⇒ 取锚**成功**的窗被标成「到达口径」⇒
`needsCorrection` 判真 ⇒ 每窗白排队一次官方修正，并把已经逐位精确的值用官方值覆盖
（数值相同、来源标签被改坏）。首窗实测就复现了这个（日志 `锚 2717.21（stream, 边界后
+1410ms）`）。

修法：`cmd/collect/main.go` 的纯函数 `anchorSource(feedSrc)` 显式翻译词表——
`feed.AnchorSourceOfficial → collect.SourceOfficial`、其余（即精确推送）→
`collect.SourcePush`；`collect.SourceStream` **只**由调用点的兜底初值产生。

---

## 5. 首次 ETH 采集与数据校验

### 5.1 怎么跑

```bash
https_proxy=http://127.0.0.1:1087 \
  go run ./cmd/collect -config v4.config.yaml -asset eth        # → data/eth
```

启动横幅会打全派生结果，用来确认参数接对了：

```
 高频数据采集 v2 — eth-updown-5m（资产 eth）| 输出: data/eth（全新目录）
 数据源: [Binance ETHUSDT aggTrade+depth20 1s聚合] + [PM CLOB books 1s快照]
        + [PM last_trade_price 秒聚合] + [Chainlink TWAP-60 ETH]
```

### 5.2 校验脚本 `python/v4/24_asset_data_check.py`

**不自己说自话**：每一段都拿外部真值或内部恒等式去对。八段：

| 段 | 检查 | 真值来源 |
|---|---|---|
| A | 键齐全、`start_time` 5 分钟对齐、slug 与 start_time 一致、无重复窗 | 内部 |
| B | 每窗 tick 数、ts 单调、首/末 tick 偏移、`rem` 恒等式 | 内部 |
| C | `yes_bid + no_ask = 1`、`yes_ask + no_bid = 1`（±0.01） | 套利约束 |
| D | 逐分钟 **成交笔数** / **成交量** / **价格区间**；`binance_open` vs 5m K 线开盘价 | Binance REST `klines` |
| E | TWAP 采样龄、零价条数、与现货的偏离 | 内部 + Binance |
| F | 成交桶整秒、token 闭集、每 token×秒唯一、价格 ∈ [0,1] | 内部 |
| G | **官方 `crypto-price` 的 open/close vs 行内值**（决定性） | Polymarket 官方接口 |
| H | `umaResolutionStatus=resolved` 的 `outcomePrices` vs 行内 outcome | gamma |

参数化：`--asset eth` 派生目录/slug/交易对/符号，`--dir` / `--symbol` / `--twap-symbol`
可单独覆盖；`--no-net` 只跑离线段。**跨标的通用**（BTC 也能跑）。

网络两个坑（脚本内已处理）：
- gamma / `polymarket.com` 对裸 `urllib` 默认 UA 直接 **403**（Cloudflare），要带浏览器 UA；
- `crypto-price` 会 **429**（上游 Chainlink 限流），逐窗留 0.35s 间隔 + 429 退避重试。

### 5.3 先在旧 BTC 数据上自检脚本（这一步很重要）

对 `data/btc`（3753 窗，已知良好、且**是旧口径采的**）跑一遍，既验证脚本本身，也正好
把新旧口径的差**量化**出来：

```
【B】tick 数: 中位 300，范围 [299,300]；首 tick 偏移中位 1.50s；tick 间隔全部 ≤1.5s
【C】四档全空 3.04%；单侧空簿 0.00%；价格互补 99.87%；前 5 档数量镜像 98.38%
【D】成交笔数比 中位 0.9995（p05 0.9821 / p95 1.0145）
     成交量比   中位 0.9999（p05 0.9916 / p95 1.0077）
     binance_open vs 5m K 线开盘价: 6/6 逐位相等
【E】age_ms p50 531 / p90 1194 / p99 2104；零价 53 条（0.00%）
【G】旧口径行（close_source=stream）: 开盘价逐位相等 2/5，收盘价逐位相等 0/5，
     最大差 1.38 美元     ← 这正是被换掉的口径
【H】方向与 gamma 一致 6/6
```

D 段的结果说明采集侧对 Binance 的聚合**逐位可信**（成交量比中位 0.9999）；G 段的
2/5 与 0/5 则是旧口径的直接证据——**同一个脚本**对新口径数据的期望是 **5/5**。

### 5.4 ETH 首次采集结果（离线七段）

`data/eth/events_2026-09-25.jsonl` 首窗 `eth-updown-5m-1790340600`
（12:50:00Z ~ 12:55:00Z，outcome=0 即 Up）：

```
【A】事件行 1；close_source {'push': 1}；anchor_source {'push': 1}；推送口径覆盖 1/1
【B】tick 301，范围 [301,301]；首 tick +0.98s；末 tick +300.00s；无 >1.5s 断档
【C】四档全空 8 = 2.66%；整侧缺腿 36 = 11.96%（rem ∈ [0,34]）；互补达标 291/293 = 99.32%
【E】TWAP 有效采样 301，零价 0；age_ms p50 419 / p90 959 / max 7484
【F】成交聚合 176 行（YES 97 / NO 79）；token 闭集、每 token×秒唯一
【D】Binance ETHUSDT 1m K 线：笔数比 1.0000 / 量比 1.0000；binance_open 1/1 逐位相等
【G】官方 crypto-price：开盘价 1/1 **逐位相等**、收盘价 1/1 **逐位相等**（差 0.000000 美元）
```

**A 段是本次改造的核心验收点**：`close_source` / `anchor_source` 双 `push`，
即 §4 那个 `"stream"` 词表 bug 已修——修复前这一行会是 `stream`，并触发一次
本不该有的官方修正排队。

### 5.5 ETH 首两窗的整侧缺腿（**比例偏高，且未独立取证——不要在服务器复核前当估计量用**）

C 段最初报「单侧空簿 11.96% vs BTC 基线 0.00%」，追下去分成**已确立**与**未证实**两层：

**① 已确立：BTC 的 0.00% 是旧守卫的构造性产物，不是市场属性。** 旧代码

```go
if book == nil || len(book.Bids) == 0 || len(book.Asks) == 0 { continue }
```

把「空侧整簿快照」整条消息丢掉 ⇒ 内存里留着撤单前的旧簿 ⇒ **这类 tick 在旧数据里
根本无法以「缺腿」形态出现**。所以旧 BTC 数据的 0.00% 不构成基线，两边不可直接比。

**② 未证实：ETH 的 12%~27% 是不是市场真相。** 首两窗实测：

| 窗 | outcome | 缺腿 tick | 占比 | rem 范围 | 缺哪条腿 |
|---|---|---|---|---|---|
| 12:50Z | 0（YES 赢） | 36/301 | 11.96% | [0, 34] | 卖腿 `yes_ask+no_bid` |
| 12:55Z | 0（YES 赢） | 81/300 | 27.00% | [0, 79] | 卖腿 `yes_ask+no_bid` |

形态与决策 #21 的「赢家侧整侧撤空」一致（两窗都是 YES 赢、缺的都是卖腿），
**但比例比 BTC 高一个量级，且本次只走了采集器一条通道**——本机网络/订阅侧同样能
造出「某 token 的簿停更在一份空 ask 快照上」的读数。决策 #21 当初对 BTC 是
**两条独立通道**（SDK WS + 公共 REST）取证的。

⇒ **用户在 2026-09-25 判断「有可能是我的网络问题，可以等部署服务器跑数据再验证」，
据此把本条降级为待验证项**：服务器部署后按决策 #21 的方法（`cmd/bookprobe` 双视角
逐秒对照 SDK WS 与公共 REST）复核。**在那之前不要把 12%~27% 当作 ETH 尾盘撤空率
的估计量**——它上界性地影响下游对 tail 策略可成交性的判断（决策 #21 §实盘端到端复核）。

#### 5.5.1 用户的网络假设：对**现象**已被反证（对**比率**仍待复核）

写到这里时用户提出「可能是我的网络问题」。为此对 13:00Z 窗尾盘做了一次**独立通道**
取数——公共 CLOB REST（`https://clob.polymarket.com/book?token_id=…`，与采集器的
SDK WS 订阅完全无关，每次新发 HTTP 请求）：

```
rem 40 | Up bid 0.00(0档)  ask 0.01(52档) | Down bid 0.99(52档) ask 0.00(0档)
rem 34 | Up bid 0.00(0档)  ask 0.01(47档) | Down bid 0.99(52档) ask 0.00(0档)
rem  2 | Up bid 0.00(0档)  ask 0.01(46档) | Down bid 0.99(46档) ask 0.00(0档)
```

**Up 的买盘 0 档、Down 的卖盘 0 档**，持续整个尾盘（档数 52→47→46 在变，
证明是实时数据而非缓存）。本窗 Down 赢 ⇒ 撤空的正是 Down 的 ask 与 Up 的 bid，
与决策 #21 的规则逐字一致。

⚠️ **方向映射值得记下来**：本窗（Down 赢）缺的是采集器的**买腿**
（`yes_bid`=0 且 `no_ask`=0），而 §5.5 表里那两窗（Up 赢）缺的是**卖腿**
（`yes_ask`=0 且 `no_bid`=0）。**同一条规则、两副面孔**——被清空的永远是对应
「押最终输家」的那条腿（输家的 bid 无人接、赢家的 ask 无人卖）。

**结论分两层**：
- **现象已证实**：整侧撤空是真的，不是本机订阅残留——独立 REST 通道给出同样读数。
- **比率仍待复核**：REST 对拍只验了**一窗的尾盘**；采集器报的 12%~27% 仍可能被本机
  WS 滞后放大（持有空 ask 快照的时间比市场实际持续更久）。服务器部署后按决策 #21
  的 `cmd/bookprobe` 双视角逐秒对照复核。

另有 8 个 rem ∈ [283,291] 的四档全空 tick（开盘时盘口未到位），与 BTC 的 3.04% 同源。

**③ 顺带标定掉一个阈值**：C 段原有的逐窗 0.5% FAIL 线太紧。用新逻辑重扫
`data/btc` 3753 窗（1,091,652 个有效 tick）：合计越界 **1373 = 0.126%**，
逐窗 **p99 = 1.0%、max = 1.667%**（5/300）——BTC 自己就有窗超 0.5%。
阈值改为两级：合计率 > 1% 判失败、单窗 > 5% 判失败（留 3 倍余量），
实测分布写进脚本常量注释。

越界的成因是**两本簿的采样错位**：一行的 `yes_*` 与 `no_*` 来自两条独立的 `book`
消息，而 `MakePMTick` 的 `book_ts` 只取两簿**较大者**，行内无从分辨两个瞬间。
ETH 首窗那 2 个越界 tick 落在 rem 94/96——正是价格 0.33→0.13 两秒急跌处，
两本簿错开一拍就会把两个瞬间并进同一行。**不是数据损坏**，是采样结构使然；
但下游若同时读两侧（如 flip 的四档门控），需知道这里存在瞬态错位。

### 5.6 BTC 回归（阈值改完后重跑，确认没有放松判定）

```
【C】四档全空 3.04%；整侧缺腿 0；互补达标 99.87%（0.13% 越界，均在阈值内）
【E】age_ms p50 531 / p90 1194 / p99 2104；零价 53 条
✅ 全部检查通过
```

### 5.7 决定性验收：G 段在 ETH 上逐位相等

G 段是本次改造的判据——**官方边界价 vs 行内值的逐位比较**：

```
【G】覆盖 1 窗（推送口径 1 / 旧口径 0）
  [推送 push] 开盘价逐位相等 1/1，最大差 0.000000 美元
              收盘价逐位相等 1/1，最大差 0.000000 美元
```

对照 §5.3 里 BTC 旧口径行的同一项指标：

| | 开盘价逐位相等 | 收盘价逐位相等 | 最大差 |
|---|---|---|---|
| BTC 旧口径（`close_source=stream`，5 窗） | 2/5 | **0/5** | 1.38 美元 |
| **ETH 新口径（`close_source=push`，1 窗）** | **1/1** | **1/1** | **0.000000 美元** |

即决策 #19 的「官方 `openPrice(N)` ≡ 边界那一秒推送、`closePrice(N)` ≡ `anchor(N+1)`」
在 ETH 上**同样成立** ⇒ 采集侧 `/` 引擎侧 `/` 结算侧三处共用同一口径，
ETH 数据可以直接喂 v4 引擎而不需要任何换算。

D 段同时确认 Binance ETHUSDT 侧的聚合逐位可信（笔数比 1.0000、量比 1.0000、
`binance_open` 与 5m K 线开盘价 1/1 逐位相等）。

⚠️ **H 段（gamma 结算方向）本次未验**：`umaResolutionStatus` 有分钟级延迟，
首窗跑校验时还没 `resolved`。需要采集累积若干窗后重跑（`24_asset_data_check.py`
不带 `--no-net` 即可，H 段会跳过未结算窗）。

---

## 6. 未做 / 待办

- **官方 `-asset` 的引擎侧校验**：`cmd/flip` / `cmd/tail` 现在能按 `runtime.slug_prefix`
  跑任意标的，但它们的**策略参数（浅洞带 / 63 美元 / 40 美元…）全部是 BTC 14 天标定的**
  ——换标的等于换一条没标定过的策略，不要在 ETH 上直接跑实盘。
- **采集器与引擎同机并行**：`data/<asset>` 目录各自独立，但两族的日亏熔断与 API 压力
  未做合并评估。
- **`cmd/twapprobe` 仍是 `-symbol btc` 默认值**：一次性证据工具，未纳入本次参数化。
- **H 段（gamma 结算方向）待重跑**：首窗校验时 UMA 未 `resolved`（分钟级延迟）。
  累积若干窗后不带 `--no-net` 重跑 `24_asset_data_check.py --asset eth` 即可。
- ⭐ **服务器部署后复核「整侧撤空」的比率**（用户 2026-09-25 指定的验证时机）：
  §5.5.1 已用独立 REST 通道证实**现象为真**，但 12%~27% 这个**比率**是在本机网络上
  测的，可能被 WS 滞后放大。服务器上按决策 #21 的方法复核：
  ① `cmd/bookprobe` 双视角（SDK WS + 公共 REST）逐秒对照，看两个视角的空侧起始 rem
  是否一致；② 若一致，把「尾盘撤空起始 rem 分布」当作可成交性的上界输入喂给 tail。
- **BTC 的真实缺腿基线待重采**：§5.5 已证 BTC 的 0.00% 是旧守卫的构造性产物。
  要知道 BTC 尾盘整侧撤空的真实比例，须用新代码重采一段 BTC（本次未做）。
- **更多 ETH 窗待累积**：本次只验了 1 窗，A/C/E/F 的统计量样本都很小。
  采集器（`/tmp/flipcollect2 -config v4.config.yaml -asset eth`）持续跑即可，
  跑满一天后再跑一次全量校验更有说服力。
