# v4 引擎实施计划：internal/flip 全新实现「狗@0.2 R1(m_45 纯现货)」纸面策略 + 全量清理

> 2026-09-02 定稿。实施于 git 分支 **v4**（自 v3 切出）；本文档为该分支的策略/引擎口径唯一对照。
> 策略回测权威 = [python/v4/01_backtest_r1.py](../python/v4/01_backtest_r1.py)（字段/常量全对齐）。

## Context

flip「自信崩溃」纸面跑 ~3 天收益 −0.2，用户判定止损，策略转入新族。2026-09-02 分析（python/v4/01_backtest_r1.py，data/btc 14 天）唯一正 EV 规则 R1(m_45 纯现货)：
n=245, WR 29.0% [23.7,35.0], EV +1.078U/注, +264U/14 天, 日正 12/14（双层版 +1.234U/注 但引擎只跑纯现货）。

用户决策：
- 新建 git 分支 **v4**（从 v3 切出），目录/包名沿用 cmd/flip + internal/flip（flip 名保留）；
- **不看 v3 实现、以 v4 逻辑独立全新实现**，不做改造式适配——引擎/记录模型按 dog@0.2 语义重写，不保留 flip 词汇（trigger_bid/post_end/fill_comp/cls/Confirming…全部消失）；
- 最后**清理所有无用/旧代码**，v4 分支干净管理；git 历史（v3 分支）保留一切旧代码可随时复活。
- 清理范围已确认：cmd/collect + cmd/compact + internal/collect 整体删除；python/v3 全套与 docs/ 三个 flip 文档删除（保留 python/v2/lib.py——v4 回测脚本依赖）。

## 策略口径（权威 = python/v4/01_backtest_r1.py）

每 300s 窗口**每事件只观测一次**（首个满足条件 tick，无重试）：
1. **触发** = 首个「有效 tick」上某侧 ask 满足 `0 < ask ≤ 0.20`。有效 = book_latency_ms ≤ 300（MAX_LAT）且 **UP/DOWN 双侧 bid/ask 四字段 > 0**（整簿快照门控，脚本 :79 与 Go 引擎同款；2026-09-03 由「仅 UP 双齐全」扩为对称四字段——实测报价缺失为整行全空（14 天 3.04%，两侧同秒为 0），历史结果与旧版全等；缺失 ask 经 `or 1` 语义不触发——Go 侧盘口缺失为 0，触发条件必须写 `0 < ask`，ask=0 绝不能触发）
   dog = 唯一触底侧（UpAsk ≤ 0.20 → yes；否则 DownAsk ≤ 0.20 → no）；交叉态（两侧都 ≤0.20，
   2026-09-03 起镜像 Go 引擎）：取 sgn·(spot−anchor) < 0 的一侧（= 浅洞带可能成立侧；spot=锚/
   缺失退回 yes）——14 天历史 0 次，纯语义规定，行为与旧「默认 yes」全等
2. **急跌腿**：m_45 = 触发 tick 前 **45 个 tick 序号槽位**（[i−45, i−1]）内同侧 ask 的 max ≥ 0.40（CRASH_MIN）。窗口按**索引**而非时间：ring 存最近 45 个槽位完整快照（无效 tick 压 0 占槽、不贡献），窗口头自然截断（ring 未满取现有——CSV 41 行 rem>250 即此类，合法非 reject）；**先判后插**（当前触发 tick 不进窗）；引擎自计数 tick 序号等价 python 数组索引
3. **浅洞腿**：dist_s = sgn·(spot − anchor)/anchor·1e4/hist_bps ∈ (−0.5, 0) 开区间；sgn dog=yes +1/no −1；
   spot = Binance BTCUSDT @trade 最新价（本 tick 采样）；anchor = 窗口开盘 Chainlink TWAP-60 值；hist_bps = 前 ≤18 个**已结束**窗口 |tw_close−tw_open| 均值（≥3 窗可用）
4. **时间腿**：rem > 180（REM_MIN 严格大于）
5. 全过 → ok 信号：shares = stake/fill（fill = 触发 tick 狗侧 ask，≤0.20 无滑点）；不过 → 观测落盘 ok=false + reject_reason，事件 Done
6. **观察字段（不参与决策）**：dist_t（同口径用触发 tick 的 Chainlink TWAP-60 现值）供 09-15 双层版复评
7. 结算：gamma resolved → won = 狗赢（side yes→outcome 0 / no→outcome 1）；pnl = shares−stake 赢 / −stake 输

reject_reason 顺序（固定并文档化，供复验按原因计数；回测只按掩码入选无顺序概念）：
rem_low → no_hist → missing_spot → no_crash → dist_out（missing_book 不产出行）。
锚缺失窗口（anchor≤0，窗口级）2026-09-03 起**整窗早退不产观测**（镜像回测 :69 锚缺失
事件直接跳过）；missing_anchor 仅存 decide 纯函数防线，现网不可达。

实测分布（3640 触底窗校准期望值）：rem≤180 首触 1445（39.7%）、无急跌腿 935、CSV 入选行 rem∈[181,289] 且其中 41 行 rem>250（窗头截断真实存在）、fill∈[0.10,0.20]。

**观察变体（python-only，2026-09-03 起，未落引擎；引擎维持 R1 (−0.5, 0) 双侧）**：
侧别带组合 yes → (−0.6, 0) / no → (−1, 0)（sgn 口径同 §3；09-03 分桶扫描 per-side
argmax）。机理：Binance spot 领先 Chainlink TWAP 的基差是侧别系统性的——no 触底 =
顶部恐慌族（触发时 spot 领先 TWAP 中位 +0.84σ，现货尚未真跌 → dist_s 天然深一档，
R1 带内 14 天仅 38 例 vs yes 207 例）；yes 触底 = 破位下行中继（领先 −0.38σ），现货
深度即破位度。固定带 = 对族级中位领先做常数补偿的最简形态；事件级全量校正（按各触发
基差平移带左界）≡ dist_t 决策带，已证伪无 alpha（触发总体 n=1175 EV +0.06U/注）——
领先量本身是信号，不能校掉。14 天结果（01 脚本四规则报告：对照两条 = R1 原带；头条
两条 = 组合带，明细导出 `python/v4/data/trades_r1_combo.csv`；同列名 in_pure/in_dual
指组合版两条，勿与基准 `trades_r1.csv` 混读）：
- 组合 纯现货：n=625（no 339/yes 286）WR 24.6% EV +0.633U/注 +396U/14 天
  （日正 11/14；h1 +132.7U / h2 +263.1U）
- 组合 双层：n=530（no 290/yes 240）WR 25.1% EV +0.686U/注 +364U/14 天
  （dist_t 层对组合族减分——与「TWAP 口径单独无 alpha」同源；头条取单层，双层仅对照）
- 权衡：总 P&L 高于 R1 对照（+396 vs +264）但 EV/注摊薄（0.633 vs 1.078）——多收
  2.5× 量换总收益；09-15 OOS 核心看点 = 总收益优势能否站住（而非纯带宽放宽的
  in-sample 红利）
- 早前 no(−1,0) 放宽观察（noR，n=546 +331U，明细 `trades_r1_noR.csv` 已删）被组合版
  吸收，不再单独报告
落地与否待 09-15 双样本复验后决策；落地需 per-side 带参数（引擎/回测/文档同步改）。

**live 与回测数据口径差异（已知、可接受）**：
- 数据首 tick rem=298/297，live ticker 首 tick ≈299/300 → tick 序号 ±1~2 相位差；rem>180 门槛与 45 窗边缘 ~10 行/14天 受影响；**复验对账用 |trigger_ts − event_start·1000| 对齐，不用 rem**
- anchor：脚本用官方开盘 TWAP；live 用边界瞬间流值（实测首 tick 流 vs 官方 p50 0.08 / p99 1.14 bps，σ≈7bps，个别尾巴事件可能推过带边界——记录在案）；close 腿流值与官方恒等 → hist 的 close 侧 1:1
- spot：脚本按秒桶「无成交秒 = 0 = 缺失」；live 适配器保留最后价 → 需本地接收新鲜度（RxAgeMs>2s 判 stale = missing_spot）

## 文件级改动

### A. 全新实现（不复用 flip 语义代码）
- internal/flip/types.go 重写：`Config{TriggerAskMax .2, CrashMinAsk .40, CrashWindow 45, DistLo −.5, DistHi 0, RemMin 180, Stake 2}` + `DefaultConfig()`；状态仅 stateWatching/stateDone；`Tick{Ts, Rem, UpBid/UpAsk/DownBid/DownAsk, BookLatMs, BinPrice, TwapPrice, TwapAgeMs}`（BinPrice≤0=缺 spot）；`Observation`（json 键与 CSV 对齐：ts/date/condition_id/slug/event_start/side("yes"/"no")/rem/fill/m_20/m_30/m_45/dist_s/dist_t/ok/reject_reason/shares/stake/book_latency_ms/won/pnl/resolved_at）
- internal/flip/engine.go 重写（零外部依赖纯逻辑，决策输入全部经 Tick/窗口上下文注入，绝不反向读适配器）：
  - `NewEngine(cfg)`；`BeginWindow(anchor, histBps float64)`（边界瞬间由 main 注入，窗口内不变，≤0 表示缺）；`ProcessTick(Tick) *Observation`（首个有效触底 tick 一次性判定并置 Done；此后 tick 返回 nil，**无 Finalize**——判定/执行/记录都发生在触发 tick 时刻）；`State()`/`Config()`
  - 内部：up/down 各一条 45 槽**索引槽位** ring（每 tick 处理完压入当前快照：latency≤300 存 ask、否则存 0；先判后插，触发 tick 不进窗）；ring 内过滤取 max 得 m_20/m_30/m_45（回测 CSV 同款三列）；`rem==0` 终 tick 处理完置 Done（Dashboard 终态语义）
  - 包内显式映射注释/常量：Up=yes、Down=no、outcome 0=Up（防 dog↔outcome 反向写错）
- internal/flip/exec.go 调整：Executor.Execute(obs *Observation)；PaperExecutor **只校验 fill>0**——v3 的 fill∈[0.05,0.95] 边界必须整体删除（v4 回测无任何 fill 边界，保留会在执行层吞掉 ok 信号）；-mode live 仍预留未实现
- internal/flip/recorder.go 重写 schema：文件前缀 **touches_YYYY-MM-DD.jsonl**、event_type="touch"（前缀集中为一个常量，rotate/rewriteDay/loadPending 共用）；`RecordObservation(cond, slug, start, obs, stake)` 触发即落盘（行级 flush）；`Resolve` won 映射翻转（side=="yes" → outcome==0，no → outcome==1）；**loadPending 增加 schema/event_type 校验**——不匹配行跳过并告警（防 -output 指错目录时旧格式行静默落进 failed 桶污染统计）；rewriteDay temp+rename、PendingSignals/`Observations`/Signals/Stats/DailyPnl/MaxDrawdown 机制复用；结算轮询只注册 ok 行（v3 每窗无条件注册删除；ok 行写盘成功后才 Register，崩溃由重启恢复兜底）
- cmd/flip/pmtick.go 新建：从 internal/collect 吸收 `MakePMTick/BestBid/BestAsk`（含 2026-08-18 审计注释：asks 降序、最优在 len−1）与 `ParseMarketTokens`，包内私有 helper（engine 仍零依赖，SDK 类型只在 main 层出现）+ 小表驱动测试 pmtick_test.go
- cmd/flip/main.go 重写策略部分：
  - flags 换新（-trigger-ask-max/-crash-min-ask/-crash-window/-dist-lo/-dist-hi/-rem-min/-stake/-output data/v4/-dashboard/-slug/-mode）
  - **Binance spot 数据源**：feed.NewBinanceAdapterWithConfig(feed.DefaultBinanceConfig()) + main 侧带退避的重启 goroutine（`Start` 首次拨号失败不自愈，须包装成 monitor 同款重试）；**adapter 补本地接收时间戳字段**（每次收到 @trade 消息记 time.Now，LatestData 附带 RxAgeMs——交易所时间戳不能当新鲜度）；每 tick 采样 price，RxAgeMs>2s → BinPrice=0（missing_spot；BTC 常态每秒成交，几乎不误报）
  - 窗口上下文：对齐完成后、首 tick 起动前立即采一次 TwapAdapter.Latest() 作 anchor（贴近边界）；窗口尾（rem==0）tick 采样 close；**hist 冷启动用 feed.FetchTwapRanges 预热**（官方范围回填 ≤18 窗历史 → 重启不再前 3 窗 no_hist，且与回测 hist 口径一致；live 窗结束再追加自己的窗），滚动数组 ≤18、≥3 可用、push 在窗口结束后
  - `lateLimit` 40s→15s（v4 从窗起点就观测判定，无 v3 前 40s 盲区；迟到 >15s 整窗跳过，防 m45 证据缺失的假截断）；删除 v3 的 staleBookThresholdMs=5s 盘口清零分支（引擎 latency≤300 过滤已覆盖，重复）
  - 触发 tick 即 handleObservation→Execute+RecordObservation+Register 结算轮询（ok 行）；日志/统计文案全部换 dog 语义
- internal/flip/engine_test.go 重写 + recorder_test.go 适配：
  - 触发（valid/invalid latency、up 报价缺失、缺 ask 不触发、交叉态双侧 ask≤0.2 按现货偏离选边、锚缺失窗口整窗不观测、事件内仅首观测）
  - 四腿判定与各 reject_reason（rem_low/no_hist/missing_spot/no_crash/dist_out；missing_anchor 直调 decide 防线）、m_45 窗截断/无效 tick 不贡献但占槽、dist_s 符号/开区间边界、done 后不再检
  - recorder：touches_ 文件名/切日、won 映射、rewriteDay 原子回填、重启恢复分流

### B. Dashboard 适配（无 v3 语义残留）
- internal/dashboard/handlers.go：crossResponse → observationResponse（列：time/side/rem/fill/m_45/dist_s/dist_t/ok/reject_reason/shares/won/pnl）；/api/crosses → **/api/observations**（去掉旧路径）；stateResponse CrossCount→observation_count、加 spot 字段；/api/config 键换新参
- internal/dashboard/state.go：LiveSnapshot 增 SpotPrice/SpotAgeMs；EngineState 注释改 Watching/Done（触底观测）
- static/index.html + app.js：信号表列 time/side/rem/fill/m45/dist_s/shares/结果/P&L；观测表列 time/side/rem/fill/m45/dist_s/判定(ok/原因中文映射)；win-meta 加 spot；side 显示 YES/NO（app.js 内 side class 映射 yes→up 配色/no→down）；文案 穿越→触底；style.css 加 `.side-tag.yes/.no` 两条

### C. 删除（清理清单——git v3 分支保留全部，可 `git checkout v3 -- <路径>` 复活）
- cmd/collect/、cmd/compact/、整个 internal/collect/（数据采集/压缩管线；引擎所需盘口工具已吸收进 cmd/flip/pmtick.go）
- internal/feed/orderbook_adapter.go + test（仓库零调用者的死代码；cmd/flip 用 SDK MarketMonitor 内联重启）
- internal/feed/twap_adapter.go 裁剪：**PollOfficialOpenPrice / PollOfficialClosePrice 删除**（仅 cmd/collect 用过/已零调用者）；**FetchTwapRanges 保留**（v4 做 hist 冷启动预热）；对应测试用例随删
- python/v3/（flip 家族分析全套，flip 已弃）——保留 python/v2/lib.py（01_backtest_r1.py 依赖的数据加载器）与 python/v4/
- docs/ 三个 flip 文档（strategy_plan/report/paper_plan_2026-08-31.md）
- CLAUDE.md 全面改写策略现状段（R1 口径/基准数字/纸面实施中/清理说明）+ 结构树 + 运行方式 flags
- down.sh：v3 → v4 下载 data/v4；build.sh 提示行（collect/compact 不再存在）；.env/deploy.sh 不动
- ⚠️ **collect 复活键名炸弹**（写进文档）：现网 data/btc 的 pm 键是 `yes_*/no_*`，v3 分支 cmd/collect 写 `up_*/down_*`——复活后增采须先改键名/loader 兼容，否则 python 侧静默读出 0

### D. 复验脚本
- python/v4/02_paper_compare.py 新建（v3 reuse_signals.py 的 v4 版）：读 touches_*.jsonl → 按记录字段重算规则（ok=in_pure），输出 n/日、WR、EV、fill、side、dist_s/dist_t、reject 原因分布；与 trades_r1.csv 同字段名对照口径一致性；对账对齐键 = |trigger_ts − event_start·1000| 与 event_start 双重对齐，不用 rem

## 验证

1. `go build ./...` + `go vet ./...` + `go test ./internal/flip/ ./internal/trading/ ./cmd/flip/`（引擎/记录器重写测试 + pmtick 小测）
2. 基线复算确认：`python python/v4/01_backtest_r1.py` 输出与基准一致（纯现货 n=245 WR 29.0% EV +1.078U/注）
3. 本地短跑冒烟（有代理）：`go run ./cmd/flip -output /tmp/dog -dashboard :8090`，观察日志/观测行 JSON 字段（side/rem/fill/m_45/dist_s/ok/reject_reason）/恢复逻辑（重启后 PendingSignals 重新注册）
4. 部署：build.sh → deploy.sh → 服务器 `nohup ./flip -output data/v4 -dashboard :8090`；**部署前确认服务器 Binance 直连可达且无指向不通代理的 HTTP(S)_PROXY**（适配器拨号走 http.ProxyFromEnvironment）；跑 1-2 天看信号频率 ≈ 17.5/日（触底 ~260/日、ok ~17-18/日）、reject 分布接近实测（rem_low ~40% 等）、WR/fill 接近回测；down.sh v4 拉记录，02_paper_compare.py 对账
5. 09-15：python/v4 复验（现有 14 天数据 + 纸面记录双样本）

## 提交序列（中文、组件前缀、不带署名行；全部在 v4 分支）

实施第一步：`git checkout -b v4`（当前 v3 工作区干净，直接切出）；每步实施后跑对应验证。
1. `flip:` 引擎/记录模型按 dog@0.2 全新实现 + 测试（types/engine/exec/recorder + 两测试文件）
2. `flip:` main 接线（Binance spot、anchor/σ 窗口、触发即执行记录、flags）+ pmtick.go；dashboard schema（handlers/state/static）
3. `cleanup:` 删除 collect 管线/orderbook_adapter/python v3/docs 旧 flip 文件；更新 build.sh/down.sh/CLAUDE.md；新增 v4 引擎映射 doc
4. `python:` 02_paper_compare.py 复验脚本
