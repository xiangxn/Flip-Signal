# 扫尾盘（tail sweep ⑤）回测 ↔ 引擎口径映射与运行说明（2026-09-23）

> 策略规格 = [tail_sweep_2026-09-22.md](tail_sweep_2026-09-22.md)（§1 口径 / §5 纸面登记）。
> 唯一权威规则源 = [python/v4/13_tail_sweep.py](../python/v4/13_tail_sweep.py)（主回测）
> 与 [15_tail_sweep_union_sigma.py](../python/v4/15_tail_sweep_union_sigma.py)（联合格 σ / 门槛）。
> 引擎实现 = [cmd/tail/main.go](../cmd/tail/main.go) + [internal/tail/](../internal/tail/)。
> 本文档固定**回测 ↔ 引擎记录 JSON ↔ 三个日志前缀**的映射，供纸面登记（§5.2 判据）
> 与日常对账使用。与 dog@0.2 的关系：两族独立、方向相反，共用原语（`flip.Tick` /
> `Executor` / `HistState` / `CanTrade`），互不读写对方的记录。

## 1. 常量映射

| 回测（13/15） | 引擎 config 字段 | 配置键 | 默认 | 说明 |
|---|---|---|---|---|
| `T = 60`（尾盘快照） | `Tail.RemStart` | `tail.rem_start` | 60 | 首个 `rem ≤ 60` 的**有效** tick 取决策快照（策略本体） |
| （纸面前置，§5.3） | `Tail.FrameRem` | `tail.frame_rem` | 150 | 首个 `rem ≤ 150` 的有效 tick **只记一行原始快照**，不判定不下单 |
| `PRICE_MIN = 0.80` | `Tail.PriceMin` | `tail.price_min` | 0.80 | 热门侧 ask ≥ 此值——**全部五格的前置**（见 §3.1） |
| `DEV_USD = 63` | `Tail.DevMinUSD` | `tail.dev_min_usd` | 63 | 位移腿：`dev ≥ 63 美元` 即放行 |
| `SIGMA_USD = 40` | `Tail.SigmaMinUSD` | `tail.sigma_min_usd` | 40 | σ 腿门槛：`sd ≥ 40 美元` 才允许「dev ≥ sd」放行（⑤ 相对 ④ 的唯一差别） |
| `STAKE = 2.0` | `Tail.Stake` | `tail.stake` | 2 | USDC/注。paper 股数 `= Stake/HotAsk` **精确除**（与回测 `shares = 2/fill` 恒等）；live 下单量由 `trading.OrderSpecForObs` 取 `floor2`（既有口径） |
| `MAX_LAT = 300` | `Tail.MaxBookLatMs` | `tail.max_book_lat_ms` | 300 | `book_latency_ms > 300` 的 tick 无效。与 `flip.max_book_lat_ms` 同值但**独立成键**（两族可分别部署/分别标定） |
| hist ≤18 窗、≥3 可用 | `flip.HistState`（共用类型） | —（常量） | 18 / 3 | σ 滚动窗；`flip.HistMin = 3`，`hist.Count() < 3` ⇒ 整窗跳过 `skip=no_sigma` |
| spot 龄上限 | `Feed.MaxSpotAgeMs` | `feed.max_spot_age_ms` | 2000 | 超龄 ⇒ `spot ≤ 0` ⇒ 快照行 `missing_spot`（整窗丢弃） |
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
| `tail_YYYY-MM-DD.jsonl` | 每窗 ≤ 2 行（`kind=frame` / `kind=snap`） | §5.3 的纸面登记；判定 / 成交 / 结算全在这 |
| `tailwin_YYYY-MM-DD.jsonl` | **每完成窗 1 行** | tail 自己的 σ 预热源（`flip.WindowEntry` 形状） |
| `tailstats_YYYY-MM-DD.jsonl` | **严格每窗 1 行** | tick 健康度 + `skip` 原因 + 锚状态（§5.2 辅助闸门 3） |

⚠️ `tailwin_*` **不得**与 `windows_*` 共用文件：两个进程（flip / tail）双写同一文件
会以「不属于本引擎的振幅」污染 σ；且 `tail_*` 行混进 `tailwin_*` 会以 `Amp=0`
污染其后 18 窗（CLAUDE.md 决策 #9 的教训）。三个前缀经 recorder 的 schema 校验
（`event_type == "tail"`）与独立文件句柄隔离，测试里有「三前缀互不串读」回归。

### 2.2 字段映射（`tail_*` 行 ↔ python 快照）

| python（13_tail_sweep.py） | 引擎 `Observation` json | 说明 |
|---|---|---|
| `event_start` | `event_start` | 窗口起点 unix 秒（**对账主键**） |
| （按 T 的 snapshots） | `kind` + `frame_t` | `frame`+150 / `snap`+60；两行共用同一批字段 |
| — | `ts` / `rem` | 快照 tick 的采样时刻（unix 毫秒）/ 剩余秒（`≤ frame_t`） |
| `yes_bid/yes_ask/no_bid/no_ask` | 同名字段 | 四档报价（§5.3 原始字段，离线可复算任意 T） |
| `spot` | `spot` | Binance 最新价（0 = 缺失/陈旧 >2s） |
| `twap_price` | `twap` | TWAP-60 流值（0 = 缺失；⑤ 不用它，见 §2.3） |
| `twap_open_price` | `anchor` | 本窗锚（**恒 > 0**：无锚整窗不产出，见 §3.2） |
| `hist_bps` | `hist_bps` | 本窗生效 σ（bps） |
| — | `book_latency_ms` / `spot_age_ms` / `twap_age_ms` | 诊断（§5.3 要求） |
| `side`（热门侧） | `side` | yes/no；`yes_ask ≥ no_ask` → yes（**平局取 yes**） |
| `fill` | `hot_ask` | 热门侧 ask = 成交价口径 |
| `d_spot`（∈ 美元与 bps 两处语义） | `dev` | 位移（**美元**，正 = 朝押注方向）；0 且 `omitempty` = 未计算 |
| `sd`（1σ 折美元） | `sd` | `= hist_bps × anchor / 1e4`（美元）；0 且 `omitempty` = σ 不可用 |
| `sig`（bool）/ 各格过滤 | `rules` + `ok` + `reject_reason` | 四条原始腿 + 派生 `Rule1()…Rule5()`；**规则变量一律美元**，不引 bps 中间量 |
| `in_*` 各格 | `ok` | `ok=true` ⇔ ⑤ 通过（唯一会下单的格） |
| `settle_won` | `won` | 结算后回填（官方 outcome） |
| — | `pnl` / `shares` / `stake` / `cost` | 结算后回填 / 决策时计算（`cost` 仅 live） |
| — | `gate_reason` | 风控闸（`daily_loss` / `first_window`；paper 被闸行照记照结算，分析须显式过滤） |
| — | `exec_status` / `order_id` / `avg_fill_price` / `exec_note` | live 执行回填（paper 行恒空） |

### 2.3 ⚠️ 两个容易写错的口径（已固化在代码里）

1. **σ 腿必须用显式布尔**，不能靠 `sd > 0` 判：`sd = 0`（无历史）会让 `dev ≥ sd`
   恒真，③④ 会在无历史窗口全放行。引擎里 σ 可用性由 `histBps > 0` 单独传入
   `EvalRules`，且现网还有前置闸（`no_sigma` 整窗跳过）使其不可达。
2. **快照 tick 上 spot / twap 缺失 = 整窗丢弃**，不是「往后找下一个有 spot 的 tick」
   ——python 的 `continue` 落在外层事件循环，搜索在首个 `rem ≤ T` 的有效 tick 就停了。
   引擎落一行 `reject_reason=missing_spot|missing_twap` 后不再产行。⚠️ ⑤ 规则本身
   **不用 twap**，`missing_twap` 纯粹是为了与回测宇宙一致（n=1536 是在「spot 与 twap
   同时在场」的约束下算出来的）。

## 3. 与 dog@0.2 引擎的结构差异（有意为之）

### 3.1 两帧折中（用户决定，2026-09-23）

§5.3 的完整要求在 `rem ≤ 180` 起每秒一行（≈52k 行/日），代价太大；退为**两帧**：
`rem ≤ 150` 首帧（`kind=frame`，只记录）+ `rem ≤ 60` 首帧（`kind=snap`，判定+执行）。
**代价：T 不可再调**——离线只能复算 T=60 与 T=150 两个值。两行**共用同一个锚**
（首行落盘即冻结，`UpgradeAnchor` 此后拒收），否则帧行与快照行的 `dev` 基准不同源。

两个闩锁是**独立的一次性闩锁**：无效 tick 不推进任何闩锁；首个有效 tick 若已
`rem ≤ 60`，则两行**同发**（与 python 对两个 T 各取一次首帧等价）。

### 3.2 锚：精确取锚 + 「产出任何行之前已定局」

与 flip 同口径（决策 #15）：锚 = 边界那一秒的 TWAP 推送，`feed.RecoverAnchor`
500ms × 40 = 20s 预算，`BeginWindow(0, 0)` **不设过渡锚**。
tail 独有的时序性质：取锚通道在边界 **+20s（rem≈280）** 就结束，而最早的产出帧在
**rem ≤ 150（边界 +150s）**——**锚在产出任何一行之前就已定局**，不存在「帧用旧锚、
快照用新锚」的不一致窗口。故 `anchor ≤ 0` ⇒ 本窗一行不产出（只在 `tailstats` 里
记 `anchor_exact=false`），与「宁可丢窗也不拿近似锚判定」一致。

### 3.3 σ 冷启动整窗跳过

`hist.Count() < 3` ⇒ `skip=no_sigma`：不预取、不订阅、不产出任何行（决策 #13 同款）。
`tailwin_*` 本地预热优先（`RecentBlock` 截连续块 + 15min 新鲜度），不足回退
`feed.FetchTwapRanges` 网络预热。

### 3.4 风控闸与撤单点

- **风控闸**：`flip.CanTrade` + 当日锁存，两模式同判据（决策 #10）。⚠️ 这比文档
  §5.3 写的「本族不接入风控闸」多一层——那句是「只登记不下单」时期写的；既已做
  live 代码，就按现行两模式同闸执行。
- **撤单点 = 闭市（`rem ≤ 0`）**（用户决定，2026-09-23）：`trading.CancelAtClose`
  （= `time.Nanosecond` 哨兵，**必须微小正数**——`≤0` 会被 `NewFillTracker` 当成
  「未配置」回退 180s，而 tail 在 `rem≈60` 才挂单，回退会让第一轮轮询就撤单）。
  硬截止 = 闭市 + 60s 宽限（撤单一直失败时的兜底）。

## 4. 与回测的已知差异（写在这里，免得日后当新发现）

| 维度 | 回测 | 引擎 | 影响 |
|---|---|---|---|
| **成交** | 快照瞬间按热门侧 ask 成交 | **paper**：同回测（`shares = Stake/HotAsk` 精确除）；**live**：GTC 限价挂单，挂到闭市 | live 的成交样本天然偏向「热门侧在尾盘走弱」的子样本（反弹回去的单子根本不成交）——**与回测不是同一个估计量**。挂单期 ≤60s 把偏差截短但不消除。**首次上线只跑纸面** |
| 锚 | 官方 `twap_open_price` | 边界那一秒的 TWAP 推送（= 官方 openPrice 口径，8/8 窗逐位相同） | 已对齐（决策 #15） |
| σ | 前 ≤18 个**事件**索引的 `|close−open|` 均值（缺端点的窗仍占索引位、不贡献） | `flip.HistState`：最近 ≤18 个**已 push** 振幅 | 两者在「窗口缺锚/缺 close」时条数不同（`RecentBlock` 截断 + 跳过窗都会少 push）。对账测试里 σ 按 python 语义现算后注入，故五格数字逐位一致；现网差异上界 = 每 18 窗最多几窗 |
| close | 官方 `twap_close_price` | 窗末 tick 的 TWAP 流值（到达口径，龄 >10s 判缺失、本窗不计 σ） | open/close 有 ~1.5s 不对称，**已知未做**（与 flip 同款遗留） |
| tick 相位 | 数据行内 rem | 1s ticker，相位 ±1-2 tick | 尾盘闸是 `≤` 判定、两帧各取首帧，相位只影响落在闸上那一两秒——记录时按 `ts` 对齐，勿用 `rem` 逐笔对账 |
| **频率** | ⑤ 110 注/日（样本内均值，区间 27~192） | 见 §5.2 辅助闸门 1：90~120 注/日 | 引擎侧可选：每窗最多 1 注（快照唯一），故频率 = 通过 ⑤ 的窗数 |
| **跨进程熔断** | — | flip 与 tail 各自一条独立 24U 日亏线（等效 48U） | 文档 §4.7 要求两族同开实盘时**仓位与熔断必须合并**——**本次未实现**，只写在这里。要做需另开特性（共用账本 / 单进程双引擎） |

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
- `tail_*.jsonl`：每窗 ≤2 行；有 `ok=true` 就该在结算后看到 `won`/`pnl` 回填
- `tailwin_*.jsonl`：每完整窗口 1 行（行数 ≈ 当日已跑窗数）
- `tailstats_*.jsonl`：**严格每窗 1 行**（≈288/日）——行数本身就是「主循环跑满」的证据；
  `skip` 取值：`late`（迟到跳窗）/ `no_market` / `no_token` / `dup_record` / `no_sigma`

### 5.2 判据 = 文档 §5.2（先定后看）

14 个完整 UTC 日且 n ≥ 800 时首次判决，**主判据 = 日级 bootstrap（重采样日期 2000 次）
P&L 的 95% 区间**：下界 > 0 通过 / 上界 < 0 判负 / 跨 0 不显著（延到 28 日）。
辅助闸门（任一不满足 ⇒ 只记录不下结论，先查原因）：① 频率 90~120 注/日；
② 随机抽 30 笔与 gamma 官方 outcome 逐笔核对；③ 锚与 σ 就绪（`anchor_exact=false` 或
`hist_bps=0` 的窗口整窗不产出，必须在 `tailstats_*` 里可分辨）。

### 5.3 验收红线（2026-09-23 全绿）

```bash
go test ./internal/... -race                      # 全绿（含 flip/trading/config 既有回归）
go test ./internal/tail/ -run TestParityBacktest  # 14 天全量对账（opt-in, data/btc 不存在则跳过）
```

`parity_test.go` 流式重放 `data/btc/events_*.jsonl`（3753 窗）驱动 `internal/tail`
的**真实引擎**（规则判定不复制，只喂数据 + σ 注入），实测：

| 格 | n | WR | P&L（14 天，2U/注） |
|---|---|---|---|
| ① 热门侧 ask≥0.80 | 3109 | 97.65% | +72.10U |
| ② ①∧dev≥63 | 1458 | 99.59% | +32.69U |
| ③ ①∧dev≥1σ | 1469 | 99.46% | +22.27U |
| ④ ①∧(dev≥63∨dev≥1σ) | 1747 | 99.37% | +33.98U |
| **⑤** ①∧(dev≥63∨(sd≥40∧dev≥1σ)) | **1536** | **99.61%** | **+36.93U** |

外加两个闩锁计数：frame 宇宙 **3643** 窗 / snap 宇宙 **3634** 窗（= python
`snapshots(T=150/60)` 的 n）。**n 逐位相等、WR/P&L 在两位小数舍入内相等**。

⚠️ 两条对账口径：① 宇宙过滤必须含「快照 tick 上 spot 与 twap 同时在场」（python
整窗丢弃、Go 落行后继续——聚合时要对齐）；② σ 必须**按事件索引**现算后注入
（python 语义），**不能喂 `flip.HistState`**（后者只记已 push 的振幅，缺窗时条数不同）。

### 5.4 Dashboard（判决速览）口径

`cmd/tail -dashboard :8091`（或配置键 `runtime.tail_dashboard_addr`）起一块只读面板，
把 §5.2 的判据做成常显读数——**页面不参与判定、不写任何文件**，全部读数现算。六个口：

| 路由 | 内容 |
|---|---|
| `/api/state` | 当前窗口（热门侧/`dev`/`sd`/锚与 σ/两个闩锁/三源新鲜度）+ 统计汇总 + **今日健康度**（读当日 `tailstats_*`） |
| `/api/snaps` | 决策快照行分页（成功 + 否决 + 被闸，各带判定段与结算回填） |
| `/api/frames` | 原始帧行分页（rem≤150，只记录——「那一刻市场长什么样」的原稿） |
| `/api/daily` | 逐日：帧/快照/注数/**注/日**/待结算/胜率/P&L |
| `/api/judge` | **判决速览本尊**：①~⑤ 五格 + T=150 对照格 |
| `/api/config` | `tail.*` 7 键（键名 = mapstructure tag，前端展示标定参数） |

**判决口径（`internal/tail/judge.go`，与 §5.2 逐条对应）**：

- 样本门槛 `Days ≥ 14 ∧ N ≥ 800` 未过 ⇒ 判词 `pending`（页面显示「未到判决时点」+ 进度条）；
- **主判据 = 日级 bootstrap**：把每格的注按 UTC 日汇总成日 P&L 序列，按**日**重采样
  2000 次（seed 42），取第 `0.025·B` / `0.975·B` 个排序值 = 95% 区间；下界 > 0 `pass` /
  上界 < 0 `fail` / 跨 0 `inconclusive`（28 日仍跨 0 ⇒ `fail`）。
  **Go 侧复刻了 CPython 的 MT19937 + `randrange`**（含 3.12+ `sum()` 的 Neumaier 补偿
  求和），与 `python/v4/13_tail_sweep.py:424 boot_days` **逐位一致**——判决卡上的区间
  与复验脚本跑出来的是同一个数（黄金向量钉在 `internal/tail/judge_test.go`）；
- 频率闸 90~120 注/日按 `N/Days` 现算（§5.2 辅助闸门 1）；
- 被闸行（`gate_reason` 非空）**不进任何格**（§2.2 红线），但仍在快照表里可见；
- 未结算行不进 n/胜率/P&L（判决只吃已结算样本）。

⚠️ **三处已知口径差异**（别把页面读数当成 python 的等价物）：

1. **T=150 对照格的结果是借来的**：帧行自己不挂结算（recorder 只对 snap+ok+成交的行
   注册结算轮询），故按 `condition_id` 借**同窗快照行**的官方结果。这意味着该格只覆盖
   「该窗快照成交过」的窗口，比 python 的全样本离线复算窄——它是**同一批窗口内
   T=60 vs T=150 的配对比较**，不是全样本对照（`③ 纯 σ` 等其它格的宇宙不受影响）。
2. **胜负按帧行自己的热门侧重算**（帧与快照的 hot side 可能不同——rem≤150 时 ask 高的
   一侧到 rem≤60 可能已反过来），P&L 按 `cfg.Stake / frame.HotAsk` 现算（帧行没有仓位）。
3. **σ 用的是落盘时的本窗值**（`hist_bps`），与 python 按事件索引现算的口径一致；
   但页面**实时**读数（`/api/state` 的 `dev`/`sd`）用的是**当前**盘口与 spot——同一窗内
   与落盘行不会逐位相同（落盘行是快照 tick 那一刻的值）。判定路径不读页面。

## 6. 复现

```bash
python/venv/bin/python python/v4/13_tail_sweep.py              # 主回测 / 候选格排名
python/venv/bin/python python/v4/14_tail_sweep_sigma.py        # σ 尺子 / 窗长 / 日级 bootstrap
python/venv/bin/python python/v4/15_tail_sweep_union_sigma.py  # 联合格 σ / 门槛稳定性
go test ./internal/tail/ -run TestParityBacktest -v            # Go↔py 逐窗对账
```
