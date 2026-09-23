# CLAUDE.md — Dog@0.2 Flip Signal

## 项目概述

**Flip Signal** 是一个针对 **Polymarket BTC 5分钟市场**（btc-updown-5m，Chainlink TWAP-60 结算）的量化交易系统。
当前策略 **「狗@0.2」**：检测 UP/DOWN 盘口某侧 ask 被砸到 ≤0.20 的 tick（下狗机会），
若该侧刚经历急跌（45s 内 ask 曾 ≥0.40）且 Binance spot 相对锚（TWAP-60 开盘）处于
**浅洞**（spot 在狗败侧浅坑，dist_s ∈ (lo(side), 0)；lo = yes −0.6 / no −1.0——
2026-09-03 组合版侧别带，见下）、窗口尚余 >180s，则买入该下狗。

### 核心原则
> **不预测涨跌，只判断「市场刚把某个 outcome 砸到 0.2 的极端折价——砸出急跌坑，
> 现货却没真正走出来」，买折价本身。**

### 当前策略状态（2026-09-02 定稿，v4 分支）
- 完整方案见 `docs/engine_plan_dog020_2026-09-02.md`，口径映射/运行说明见
  `docs/dog020_mapping_2026-09-02.md`，**09-15 双样本复验计划（预设判据/决策表）见
  `docs/dog020_oos_review_2026-09-15.md`，复验结果见 `docs/dog020_oos_result_2026-09-15.md`**，
  分析/回测脚本 `python/v4/`（权威 = `python/v4/01_backtest_r1.py`；复验裁判 =
  `python/v4/06_oos_review.py`）。
- **信号条件（触发与急跌腿来自 PM 订单簿；浅洞腿输入 Binance spot）**：
  - 触发：每事件首个有效 tick 上某侧 ask 满足 `0 < ask ≤ 0.20`（唯一触底侧即狗侧；
    两侧都 ≤0.2 的交叉态取 sgn·(spot−anchor)<0 一侧，14 天历史 0 次）
  - 急跌：m_45 = 触发前 45 个 tick 槽位内同侧 ask max ≥ 0.40
  - 浅洞：dist_s = sgn·(spot−anchor)/anchor·1e4/hist_bps ∈ (lo(side), 0) 开区间，
    侧别带 lo = yes −0.6 / no −1.0（组合版，2026-09-03 落引擎；R1 双侧 (−0.5,0) 退对照）
  - 时间：rem > 180（窗口前 ~2 分钟）
  - σ（hist_bps）= 前 ≤18 个已完窗口 |tw_close−tw_open| 均值（≥3 窗可用）
- **成交口径**：fill = 触发 tick 狗侧 ask（≤0.20，无滑点）；`shares = stake/fill`；
  赢 → `shares − stake`，输 → `−stake`（每股兑 1U）
- **结算**：官方 outcome（0=Up 1=Down），gamma `umaResolutionStatus=="resolved"` 后由
  ResolutionPoller 轮询触发
- **回测基准（14 天，2U/笔，2026-08-18~31）**：R1 m_45 纯现货 n=245，WR 29.0%，
  EV +1.078U/注，+264U/14 天；日正 12/14；双层（+dist_t）n=197，WR 30.5%，EV +1.234U/注
  ——R1 已退居回测对照（01 四规则报告前两条），不再落引擎
- **引擎现行口径 = 组合版侧别带**（2026-09-03 落引擎；回测头条）：yes → (−0.6, 0) /
  no → (−1, 0)（09-03 分桶 argmax；no=顶部恐慌族 spot 领先 TWAP → 带深一档,
  yes=破位下行中继 → 稍深；事件级基差全量校正 ≡ dist_t 带已证伪无 alpha）回测纯现货
  n=625 WR 24.6% EV +0.633U/注 +396U/14 天 日正 11/14 h1+133/h2+263；双层 n=530
  EV +0.686U/注 +364U（dist_t 层减分, 引擎取单层）。总 P&L 高于 R1（+396 vs +264）但
  EV/注摊薄（0.633 vs 1.078），09-15 OOS 核心看点 = 总收益优势能否站住（vs 纯带宽放宽
  的 in-sample 红利）；原 no(−1,0) 放宽观察（noR n=546 +331U）已被组合版吸收
- 🟡 **状态：09-15 双样本复验已完成 → 判定「不显著」，维持纸面继续攒，09-30 二次复查**
  （2026-09-16 执行，完整报告见 `docs/dog020_oos_result_2026-09-15.md`）。主窗
  09-04~09-16（12 整日）n=438 WR 20.3% EV +0.199U/注 **+87.2U**，落 (−80,+100)「不显著」段
  → **不启动实盘**。四点要点：
  - **缺口全部来自 yes 侧**：yes WR 26.6%→17.9%（z −2.42）而 **no 侧不变**（23.0%→23.9%）；
    按侧别构成校正期望 +300U vs 实际 +87.2U，yes 侧单独贡献 −236U
  - **「带宽=in-sample 红利」被推翻**：增量族（组合∖R1）EV +0.35→**+0.335**（几乎复现），
    反而是 R1 自身崩塌（+1.078→**+0.098**）——deep 带保住、浅带塌了（n=187 不足以正面确认带宽）
  - **A 层信号频率闸门未过**（36.5/日 vs 计划 44±5）：已排除引擎缺陷（`cmd/btreplay`
    625 笔逐位一致、σ 独立复算 p50 偏差 0.0000%、口径复核 0 不一致、结算 30/30 抽样核对），
    定位为行情结构——OOS 触底时点后移 ~14s（rem 中位 203→189）+ σ −33%（9.26→6.17）
  - 09-03~05 的「深 yes/浅 no 镜像」**未持续**（OOS yes −0.33σ / no −0.71σ，与回测
    −0.39/−0.75 高度吻合）——深度没变，**胜率变了**；判定对窗口切法不敏感（4 种切法皆「不显著」），
    唯计划字面窗（09-03~09-15, n=416, WR 18.0%）会触发 WR<20% 加重判负条款
- 🧭 **2026-09-16 追加：延迟可见化 + 日亏熔断**（计划/验收/决策表见
  `docs/dog020_risk_latency_plan_2026-09-16.md`）。起因是 09-15 复验的频率缺口查不下去：
  三源阈值全部配置化（默认值不动——book 收紧在 14 天回测里单调变差），并补上两类此前
  **零痕迹**的证据：`winstats_*.jsonl`（每窗 tick 健康度 + 被闸挡掉的丢信号明细）与观测
  `spot_age_ms`（亚阈值陈旧是否污染 dist_s）。日亏熔断默认 **−24U**、UTC 日、两模式同判据
  且**当日锁存**；paper 取方案 A（被闸行照记照结算 + `gate_reason`，分析脚本默认过滤）。
  已量化结论：现货断流每日只丢 ≈0.14 笔（0.35% 信号量）→ **延迟不是频率缺口的主因**，
  主因仍是触底时点后移（`rem_low` 占比升高）；24U 线在纸面 14 天回放里 Δ+64.9U 且 4 次
  触发全落在亏损日、10 个盈利日零误伤。
- 🧭 **2026-09-16 追加：锚缺失恢复**（`docs/dog020_anchor_recovery_2026-09-16.md`）——
  ⚠️ **取值口径已被 09-18 升级、再被 09-19 精确取锚取代（见下两条 / 决策 #14、#15）**，
  其锚缺失成因/镜像红线/频率实测/σ 冷启动部分仍有效。边界采样不到 TWAP-60 流值时不再直接丢整窗：官方
  `FetchOpenPrice` 重试 3 次 × 20s + 边界 ±10s 内推送直采（`feed.RecoverAnchor`），
  恢复期 tick 照常占槽只闸触发判定，3 次失败才整窗不观测。**是保险不是频率修复**——
  paper 12 天里锚缺失槽位占比 ≤0.57% 且全落在 ≥10min 断档内（= 上界，真实值待 winstats
  区分），09-15 频率缺口归因仍是 `rem_low` 后移。实测口径差：官方 open vs 边界流值
  p50 0.056bps（≈0.006σ），而迟到 30s 的推送 p90 达 0.41σ（10s 宽限的取值依据）。
- 🧭 **2026-09-18 追加：锚升级**（`docs/dog020_anchor_upgrade_2026-09-18.md`）——
  ⚠️ **取值口径已被 09-19 的精确取锚取代（见下条 / 决策 #15）**，其「官方 open 收敛实测」
  与「对齐推送 == 收敛官方」的证据仍是今天口径的直接依据。简史：锚 = 结算线，必须对齐
  **官方那一秒**——官方 `openPrice` 就是「边界那一秒」的推送值（3753 窗里 1717 窗逐位
  相等），而 t=0 的 `Latest()` 是**最新到达**的一条 → 实际拿到「边界前一秒」的值，与官方
  差 p90 0.24bps。09-18 的做法是「缓存 100 条按评估时刻重选 + 官方 open 在
  +2/+5/+10/+20/+40s 轮询、只有 +40s 收敛点那次作权威覆盖」；当日实测：官方 `openPrice`
  头几十秒是**未收敛的临时值**（+6.9s≠+33s；17 窗配对 13/17 不等, |差| p90 1.18bps ≈
  9.5 美元，比要修的 t=0 误差还大），而**对齐推送与收敛官方只差 3 厘美元**。
  **不是 P&L 杠杆**（625 条信号换锚后掉出 11/新增 9 ≈ 中性）。
- 🧭 **2026-09-19 追加：锚 = 边界那一秒的推送（精确取锚，取代上条的取值口径）**
  （`docs/dog020_anchor_exact_open_2026-09-19.md`）。既然「评估时刻 == 边界」的那条推送
  就是官方 open（8/8 窗逐位相同），就只留精确那一条：`PushNearest` **精确等值匹配**（删
  容差/删「取最近」），取锚通道按 **500ms × 40 = 20s** 重试（该推送到达 p50 **+2.0s**、
  观测到最晚 +12.1s），命中即终局；窗口开局 **`BeginWindow(0, 0)` 不设过渡锚**——锚未到手
  期间 tick 照常占槽、`lost_triggers(anchor_pending)` 留痕、只闸住触发判定，**20s 取不到
  则本窗不产出观测**（宁可丢窗也不拿近似锚判定）；官方 HTTP 路径**休眠**（+40s 才收敛，
  本窗机会早过；工具代码保留可随时接回）。winstats 改 `anchor_exact` + `anchor_recovered_ms`
  （改为该推送**本地到达时刻**距边界 = 发布延迟的无偏观测），删 `anchor_init`/`anchor_pick_ms`。
  历史 625 笔里最早信号 `rem=295`（边界后 5s）⇒ 正常路径（+2s 到手）**零损失**。
  见决策 #15。
- 已证伪：flip「自信崩溃」家族（v3，分支 v3 保留）、v1/v2 follow/wait 族、0.2 深度
  全市场扫、双层版单独 TWAP 腿等——历史分析/代码在 git 其他分支可查。

### 第二条策略线：「扫尾盘」⑤（2026-09-23 落引擎，与狗@0.2 独立并行）
- 完整规格见 `docs/tail_sweep_2026-09-22.md`，口径映射/运行说明见
  `docs/tail_engine_mapping_2026-09-23.md`，实现 `cmd/tail/` + `internal/tail/`。
- **逻辑（方向与狗@0.2 相反：买热门侧）**：每窗在 **`rem ≤ 60` 的第一个有效 tick**
  取快照，买当时 **ask 较高的一侧**（热门侧），当 `hot_ask ≥ 0.80` 且
  `dev ≥ 63 美元` **或**（`sd ≥ 40 美元` ∧ `dev ≥ sd`）时下注。
  `dev = sgn·(spot − anchor)`（美元，正 = 朝押注方向，sgn: 押 yes +1 / no −1）；
  `sd = hist_bps × anchor / 1e4`（该窗 1σ 折美元）。
- **回测基准（14 天，2U/注）**：n=1536，WR 99.61%，+36.93U，0 亏损日；同批快照可
  离线算 ①~⑤ 五格（WR 97.6~99.6%），但**只有 ⑤ 落引擎下单**。
- ⚠️ **参数全部不可调**：`40` 这个门槛在 2000 次日期重采样里一次都没成为最优
  （真 argmax=35）——不许调参，只纸面登记。
- 🟡 **状态：纸面登记中**，判据（文档 §5.2，先定后看）= 14 个完整 UTC 日且 n ≥ 800 时
  做首次判决，主判据为**日级 bootstrap（重采样日期 2000 次）P&L 的 95% 区间**：
  下界 > 0 通过 / 上界 < 0 判负 / 跨 0 不显著（延到 28 日）。**未上实盘**
  （live 是 GTC 挂单等成交，成交样本天然偏向「热门侧走弱」，与回测不是同一个估计量）。

### 版本管理约定（2026-08-31 起生效，v4 分支沿用）
- **分支即版本**：当前分支 `v4`（从 v3 切出，dog@0.2 全新独立实现）。
  代码命名不带版本字眼；后续策略演进各开新分支（`git checkout -b v5`…），
  旧分支（v3/eth）保留全部旧代码/文档，可 `git checkout v3 -- <路径>` 复活。
- 新分支只保留有用文件；文档直接放 `docs/`，不按版本建子目录。
- **提交规范**：提交信息用中文、组件前缀（`flip:` / `dashboard:` / `python:` /
  `cleanup:` / `docs:`），**不带 `Co-Authored-By` 等任何署名行**（2026-08-31 用户要求）。

---

## 项目结构

```
FlipSignal/
├── cmd/flip/                         # 狗@0.2 引擎主入口（纸面/实盘同源，-mode 切换）
│   └── main.go                       # 窗口循环/数据源接线/anchor σ/执行编排注入（唯一文件; 4 个引擎 flag + -encrypt 工具 flag, 配置见 internal/config）
├── cmd/tail/                         # 扫尾盘 ⑤ 引擎主入口（独立进程, 无 Dashboard; -config/-mode/-stake 三 flag）
│   └── main.go                       # 同上接线, 但两帧闩锁/尾盘闸/GTC 挂到闭市（决策 #17）
├── cmd/btreplay/                     # 逐笔重放 data/btc 驱动 flip.Engine（Go↔py 口径对账红线）
├── internal/
│   ├── config/                       # 配置层（viper 三层加载 + 敏感字段 AES 解密 + 启动校验）
│   │   ├── config.go                 # AppConfig + defaults()（唯一默认值来源）+ Load()
│   │   ├── encrypt.go                # 明文→密文（-encrypt 工具的逻辑; 与 decrypt 共用密码来源）
│   │   ├── decrypt.go                # owner_key/clob_creds/relayer_key 密文解密（PM_CONFIG_DECRYPT_PASSWORD）
│   │   ├── validate.go               # 校验（fatal/warning），在 CLI 覆盖之后调用
│   │   └── configfile_drift_test.go  # v4.config.yaml 漂移守卫
│   ├── flip/                         # 引擎核心层（零外部依赖, 可独立测试）
│   │   ├── types.go                  # Config + Tick/Observation/Record + 状态枚举 + 闸原因常量
│   │   ├── engine.go                 # 状态机: Watching → Done（触底观测/四腿判定 + 本窗 tick 健康度计数）
│   │   ├── exec.go                   # Executor 接口 + PaperExecutor（live 实现由 trading 注入）
│   │   ├── exec_state.go             # ExecState 编排: 风控闸（两模式共用）→ paper 单步 / live submitting 两阶段 → 统一 Execute
│   │   ├── recorder.go               # JSONL 观测记录 + windows_* 窗口振幅日志 + winstats_* 健康度（按日切分）+ P&L 回填
│   │   ├── risk.go                   # CanTrade 日亏熔断判定（纯函数; 锁存在 exec_state.breakerTripped）
│   │   ├── sigma.go                  # HistState（σ 滚动窗）+ RecentBlock 截断纯函数
│   │   ├── snapshot.go               # LiveSnapshot/LiveExec + Snapshotter 接口（dashboard 只读消费）
│   │   └── *_test.go                 # engine/recorder/exec_state/risk/latency/preheat/winstats + mirrorcheck 镜像回归
│   ├── dashboard/
│   │   ├── server.go                 # HTTP server（go:embed static/）
│   │   ├── handlers.go               # /api/state, /api/observations, /api/signals, /api/config
│   │   ├── state.go                  # 运行时组件引用
│   │   └── static/                   # index.html, app.js, style.css（兼容手机浏览器）
│   ├── feed/
│   │   ├── anchor_recover.go         # 取锚通道（精确命中边界那一秒的推送, 500ms×40 重试；官方 open HTTP 段保留但休眠）
│   │   ├── binance_adapter.go        # Binance BTCUSDT WS（spot 浅洞输入, 本地接收龄）
│   │   ├── pmtick.go                 # PM 盘口采样（best bid/ask 陷阱）+ token 解析（原 cmd/flip 下沉）
│   │   └── twap_adapter.go           # Chainlink TWAP-60（anchor/σ）+ FetchTwapRanges 预热 + 推送缓存/PushNearest(精确)/CacheStat
│   ├── tail/                         # 扫尾盘 ⑤ 引擎核心层（零外部依赖; 只复用 flip 的原语, 反向不依赖）
│   │   ├── config.go                 # Config + DefaultConfig()（7 个键, 全部不可调; 见决策 #17）
│   │   ├── types.go                  # Observation/Rules/Record/WindowStats + 行类型/闸原因常量 + 状态机
│   │   ├── decide.go                 # 纯函数 EvalRules + Rules.Rule1()…Rule5()（价格腿 = 全部五格前置）
│   │   ├── engine.go                 # 状态机: 两个独立一次性闩锁（rem≤150 帧 / rem≤60 快照）, ProcessTick 返回 0~2 行
│   │   ├── recorder.go               # tail_* / tailwin_* / tailstats_* 三前缀（独立于 flip 三前缀, 决策 #9 红线）
│   │   ├── exec_state.go             # 风控闸 + 两模式两阶段下单编排（flip.ExecState 的精简镜像）
│   │   └── *_test.go                 # decide/engine/recorder/exec_state/parity（对账 opt-in, 不在 -race 快跑里）
│   └── trading/                      # SDK 依赖层（单向依赖 flip/feed, 由 cmd/flip 与 cmd/tail 各自构造注入）
│       ├── live_executor.go          # LiveExecutor 真实 GTC 限价挂单（实现 flip.Executor, 唯一 POST 点）
│       ├── fill_tracker.go           # GTC 挂单跟踪: rem≤RemMin（flip）/ 闭市（tail）撤单 + 查 size_matched 定稿回调（决策 #16/#17）
│       ├── prefetch.go               # 每窗预热 tickSize/negRisk/feeRate（下单路径零额外网调）
│       └── resolution_poller.go      # 官方结算轮询（gamma umaResolutionStatus）
├── docs/                             # 策略文档（v4 方案/口径映射; 扫尾盘见 tail_sweep_*/tail_engine_mapping_*）
├── python/
│   ├── v2/lib.py                     # 数据加载器（v4 回测脚本依赖，保留）
│   └── v4/                           # 回测权威脚本 + 纸面对账/复验/健康度脚本（01/02/06/07）+ 扫尾盘（13/14/15）
├── v4.config.yaml                    # 全量配置示例（= 代码默认值, 有漂移守卫测试; 调参请复制成 config.local.yaml）
├── go.mod / go.sum
└── CLAUDE.md                         # 本文件
```

**已删除（v4 清理，v3 分支保留可复活）**：cmd/collect、cmd/compact、internal/collect
（数据采集管线整体移除——引擎所需盘口采样/token 解析见 internal/feed/pmtick.go）；
internal/feed/orderbook_adapter.go；
twap_adapter 的 PollOfficialOpen/ClosePrice；python/v3、docs 三份 2026-08-31 flip 文档。

---

## 架构与数据流

```
   Polymarket CLOB          Chainlink TWAP-60         Binance BTCUSDT spot
   ┌─────────────┐          ┌──────────────┐          ┌──────────────────┐
   │ MarketMonitor│         │ TwapAdapter  │          │ BinanceAdapter   │
   │ (UP/DOWN books)│       │ (anchor/σ)   │          │ (浅洞腿输入)      │
   └──────┬──────┘          └──────┬───────┘          └───────┬──────────┘
          ▼                        ▼                          ▼
    UP/DOWN 盘口 1s     推送缓存(精确取边界那一秒)      最后价 + 本地接收龄(>2s 判 stale)
                        + 窗末 close + σ 预热
          │                        │                          │
          └──────────────┬─────────┴──────────┬───────────────┘
                         ▼
               Flip Engine (1s tick)
            Watching → (触底) → Done
                         │
                         ▼
             风控闸 gate（paper/live 同判据: 首窗禁单 + 日亏熔断锁存）
                         │
                         ▼
             Executor (PaperExecutor 模拟成交; live = LiveExecutor GTC 挂单
                       → trading.FillTracker: rem≤RemMin 撤单 + 查 size_matched 定稿, 见决策 #16)
                         │
                         ▼
   Recorder (touches_* 观测 + windows_* 窗口振幅 + winstats_* 健康度, 按日切分 + P&L)
                         │
                         ▼
             ResolutionPoller (gamma 结算轮询)
```

### 市场循环流程

```
1. 计算下个 5分钟对齐时间戳；预取 gamma 市场信息（边界前 20s）
2. 边界对齐 → 准入闸（σ < `flip.HistMin` 窗即整窗跳过 `skip=no_sigma`，决策 #13）
   → 注入窗口上下文（**anchor=0、σ=0，不设过渡锚**）→ 订阅 UP/DOWN token
   → **并行起取锚通道**（每窗都起；决策 #15）
3. 每秒 1s tick：读 UP/DOWN 盘口 + Binance spot + TWAP → ProcessTick(状态机)
   （取锚通道同时**精确**`PushNearest` 命中「评估时刻 == 边界」的那条推送——立即试一次后
   每 500ms 一次、最多 40 次 = 20s，命中即 `UpgradeAnchor(price, σ)` 回填并关通道；
   锚未到手期间 tick 照常占槽、`lost_triggers(anchor_pending)` 留痕、只闸住触发判定。
   20s 未命中 → 本窗不产出观测、不计入 σ，见决策 #15）
4. 首个触底 tick（ask≤0.20）→ 四腿判定 → 观测落盘（ok 与失败都记，即时落盘）
5. ok 信号 → 风控闸（live 命中拦 POST 记 rejected；paper 命中记 gate_reason 照常结算）
   → Executor 执行（paper 即时定稿; live = GTC 挂单 → resting 交 FillTracker 每 2s
   查询, **到 rem ≤ RemMin 撤掉未成交余量并定稿**）→ **定稿后** Register 结算轮询
   （结算在闭市后数分钟, 口径不变）
6. 窗口结束（rem=0）→ 收尾取锚通道（cancel + join）→ |close−anchor| 追加进 σ 滚动窗
   并落盘 windows_*.jsonl（重启 σ 预热本地优先：windows_* 新鲜即毫秒级恢复，
   不足/过旧回退官方网络预热 FetchTwapRanges——停机期窗口只有官方能取）；
   同刻本窗 tick 健康度落盘 winstats_*.jsonl（含被延迟闸挡掉的丢信号明细与
   锚命中/来源/到达延迟）→ 下一窗口
```

### 引擎状态机

```
Watching ──首个触底观测(ask≤0.20, 四腿判定)──▶ Done
   ▲                                        │
   └────────── 窗口结束(rem==0) ◀───────────┘
```

- **Watching**: 1s tick 更新两侧状态；锚未就绪期（**每窗开局必经历，正常 ~2s**）anchor≤0，
  取锚通道可能稍后 `UpgradeAnchor` 回填——期间**照常占槽计数、只闸住触发判定**，
  命中即转正常判定，始终未回填（20s 预算耗尽）则整窗不产出观测；有效 tick
  （latency≤300 且 UP/DOWN 双侧报价齐全——整簿快照门控，实测缺失为整行全空）
  上检查 up/down ask 是否 ≤0.20
- **判定顺序**（一次完成）：rem_low → no_hist → missing_spot → no_crash → dist_out
  （missing_anchor / no_hist 现网不可达——取锚通道与 σ 未就绪整窗跳过在前，
  二者仅 decide 纯函数防线）；全过 → ok（shares = stake/fill）
- **Done**: 事件内不再检测（与回测每事件仅首个观测一致，无 fallback 重试）；
  锚未就绪期占槽的 tick 不追溯触发，只记 lost_triggers(anchor_pending)
- 数据质量：无效 tick 压 0 占槽（不进触发检查，不贡献急跌窗 max）

### 扫尾盘引擎状态机（`cmd/tail`，独立进程）

```
Watching ──首个有效 tick 且 rem≤150──▶ 落 frame 行（只记录）
        ──首个有效 tick 且 rem≤60 ──▶ 落 snap 行（判定 ⑤ + 执行）──▶ Done
   ▲                                                                    │
   └────────────────────── 窗口结束(rem==0) ◀───────────────────────────┘
```

- 两个闩锁**互相独立**、各自一次性；无效 tick（延迟 >`tail.max_book_lat_ms` /
  四档报价不全 / `rem ≤ 0`）不推进任何闩锁。首个有效 tick 若已在尾盘 → 两行同发。
- 锚在产出任何行之前已定局：取锚通道 +20s 结束（`rem≈280`），最早的行在
  `rem≤150`（`+150s`）——`anchor ≤ 0` ⇒ 本窗一行不产出（只在 `tailstats_*` 记
  `anchor_exact=false`）。
- σ 未就绪（`hist.Count() < 3`）⇒ 整窗跳过 `skip=no_sigma`；`tailwin_*` 是 tail
  自己的 σ 预热源（独立于 `windows_*`，不交叉读写）。

---

## 依赖库

| 库 | 用途 |
|----|------|
| `github.com/xiangxn/go-polymarket-sdk` v0.7.2 | Polymarket REST/WS 客户端 |
| `github.com/gorilla/websocket` | Binance WebSocket 连接（feed adapter） |
| `github.com/tidwall/gjson` | JSON 解析（SDK 依赖）|
| `github.com/spf13/viper` | 配置文件加载（internal/config，同 master 分支）|
| `golang.org/x/term` | 解密密码无回显终端输入（nohup 场景走环境变量，不走它）|

### 本地开发 replace 指令（2026-09-21 起默认**不启用**）

`go.mod` 当前**没有** replace：SDK 直接依赖发布版本 `github.com/xiangxn/go-polymarket-sdk
v0.7.2`（走模块代理/本地模块缓存，本机也没有 `/tmp/go-polymarket-sdk` 这个目录）。
只有需要**改 SDK 源码**时才临时加回（典型场景是 GTD——SDK 的 `PostOrder` 把 expiration
硬编码成 `"0"` 且不在签名结构里，见决策 #16）：

```
replace github.com/xiangxn/go-polymarket-sdk => /tmp/go-polymarket-sdk
```

⚠️ 它指向**本机路径**：带着这行提交后，换台机器 / 重新克隆 / `/tmp` 被清过的本机构建
会直接失败（`replacement directory … does not exist`）——而部署链路是「本机 `build.sh`
交叉编译 → `deploy.sh` scp 二进制」（服务器不构建），所以这个坑**不在部署时暴露**，只在
下次有人重新构建时炸；且 SDK 改动不体现在 `go.sum` 里，产物无法从仓库复现。故改完 SDK
要么删掉这行再提交，要么把 fork 发一个版本号、`go.mod` 指过去。

---

## Go 代码规范与最佳实践

（沿用既有规范，本分支全量适用）

- **注释语言一律使用中文**，技术专有名词保留英文（如 BTC、PM、UP/DOWN 等）
- **标准库优先**；`internal/flip/` 核心计算层零外部依赖，可独立测试
- **纯函数优先**：特征提取/判定逻辑为纯函数，无副作用，table-driven 测试
- 命名：文件 `snake_case.go`、导出 `PascalCase`、私有 `camelCase`、常量 `stateXxx`
- 配置用专用 struct + `DefaultConfig()` 工厂；可调参数集中在 config struct，
  通过注释标注出处（如 `01_backtest_r1.py` 常量）
- 状态机用 `type xxxState int` + iota + `switch e.state`
- 并发：只在必要边界加锁（`sync.Mutex` 写、`sync.RWMutex` 读多写少），
  简单标志用 `atomic`；Context 优雅关闭
- 错误处理：返回 `(result, error)` 不 panic；`fmt.Errorf("context: %w", err)`
- 日志：`log.Printf("[Component] message")`，关键状态用 emoji ⚠️🔒🟢🔴🟡🔥🎯
- I/O：`bufio.Writer` 包装，JSONL 每行一个 object，追加模式支持重启不丢数据

### 测试
```bash
go test ./internal/... -count=1        # 引擎/编排/记录器/盘口工具/结算轮询/配置（含 YAML 漂移守卫）
go test ./internal/... -race           # 同上 + 竞态（含扫尾盘 parity 全量重放, ~2min）
go test ./internal/tail/ -run TestParityBacktest -v   # 扫尾盘 Go↔py 逐窗对账（opt-in; data/btc 缺失即 skip）
go build ./...                         # 全量编译检查
go run ./cmd/flip -config v4.config.yaml -dashboard :8090   # 运行引擎 + Dashboard

# python 分析/回测脚本一律用项目内 venv（系统 python3 无 numpy/pandas）
python/venv/bin/python python/v4/01_backtest_r1.py
python/venv/bin/python python/v4/06_oos_review.py    # 09-15 复验裁判（纯标准库）
python/venv/bin/python python/v4/07_source_health_check.py            # 数据源健康度审计（纯标准库）
python/venv/bin/python python/v4/07_source_health_check.py --bt-scan  # + book 阈值扫描/零成交代理
python/venv/bin/python python/v4/13_tail_sweep.py                     # 扫尾盘主回测（14/15 见 mapping 文档 §6）
```

---

## 关键设计决策

1. **纸面/实盘同源**：成交执行抽象为 `Executor` 接口，`mode: paper|live` 配置区分。
   纸面 = `PaperExecutor`（校验 fill>0，无其它边界，成交即时定稿）；live =
   `LiveExecutor`（**GTC 限价挂单**）+ `trading.FillTracker`（rem≤RemMin 撤单 + 定稿，
   见决策 #16）。
   两者共用同一 `ExecState` 编排与风控闸，main 单点经接口调用，不散落两条路径。
2. **口径与回测 1:1**：触发/急跌窗/浅洞（侧别带 yes −0.6 / no −1.0）/时间腿/σ/P&L
   全部映射回测 `01_backtest_r1.py`（详见 `docs/dog020_mapping_2026-09-02.md`），
   观测 JSONL 字段对齐回测 CSV——对照基准 `trades_r1_combo.csv`（组合版），
   09-15 用 `python/v4/02_paper_compare.py` 映射复核（ok ⇔ in_pure=组合版、won ⇔
   settle_won）。
3. **首触不重试**：事件内首个触底 tick 即观测，判定失败即 Done（本窗不再检）——
   与回测刻意一致，非引擎缺陷。
4. **观测全落盘**：失败观测同样落盘（reject_reason 分解），供信号频率校准与
   09-15 原因分布对比；触发即落盘 + 结算仅 ok 行注册轮询。
5. **数据采集已删除**：cmd/collect 整体移除（API 压力考量，保留备用已无意义）；
   未来需增采按 `docs/dog020_mapping_2026-09-02.md §5` 复活（注意键名炸弹）。
6. **即时落盘 + 重启恢复**：观测在触发 tick 立即落盘（行级 flush，崩溃不丢）；
   结算回填 temp+rename 原子重写当日文件；重启扫描 JSONL 恢复内存态并自动重新
   注册未结算信号的结算轮询。
7. **单 WS 订阅复用**：MarketMonitor 重启恢复模式沿用；TwapAdapter 内建新鲜度
   看门狗（推送停更超 2min 自动重建订阅）；BinanceAdapter 首拨失败由 main 侧
   指数退避重试、断线后 runReadLoop 自愈。
8. **σ 启动预热本地优先**（2026-09-06）：每完成窗口落盘一行
   `windows_YYYY-MM-DD.jsonl`（|close−anchor| + anchor/close 流值，独立于
   touches——结算重写只动 touches 当日文件）；重启时取最近 ≤histWindows 窗并
   **截到最新一段连续块**（相邻结束缺口 >2 窗判断档，防停机前旧 regime 条目
   混入），连续块 ≥histMin 窗且最新窗距现在 ≤localFreshMax(15min) 才本地
   seed（「马上重启」毫秒级恢复、零上游 API 压力、与 live push 同源口径），
   否则回退官方 FetchTwapRanges 网络预热（停机期窗口本地没有，只有官方接口
   能取）。截断决策为纯函数 `RecentBlock`（internal/flip/sigma.go，table 测试）。
9. **数据源延迟闸配置化 + 丢信号可见化**（2026-09-16）：三源新鲜度阈值由
   `Config.MaxBookLatMs` 与 `feed.max_spot_age_ms/feed.max_twap_age_ms` 驱动
   （落地时是三个 flag，同日 config 重构后改为配置键，见决策 #11），
   **默认值 = 现行值 = 数据支持值**（配置化的意义是"能调"而非"该调"——book 收紧
   在 14 天回测里单调变差：T=20ms 少赚 62.8U）。判定分支不动，只把盲区点亮：无效
   tick 上"本会触发"的 tick 落 `winstats_YYYY-MM-DD.jsonl`（**严格每窗一行**，含
   `ticks/ticks_valid/book_stale/book_missing/lost_triggers` 明细与恒等式；2026-09-16
   修过「收尾 `rem` 截断使下一轮把刚跑完的窗判成迟到、多落一行 `skip=late`」，
   主循环改按上一窗身份静默顺延，见 anchor 文档 §9.1），触底
   观测补 `spot_age_ms`（亚阈值陈旧是否污染 `dist_s` 从"零可观测"变为可审计）。
   ⚠️ `winstats_*` 必须独立于 `windows_*`：后者是 σ 预热数据源，混入统计行会以
   Amp=0 污染其后 18 窗（文件前缀 + `kind` 字段双保险）。
10. **日亏熔断：两模式同闸 + 当日锁存**（2026-09-16）：`CanTrade(todayPnl, 线)` 纯
   函数不变（`todayPnl > 线` 才可交易），闸（`gate()`）在 `HandleObservation` 里
   两模式共用同一判据——live 命中拦下真实 POST（rejected 行），paper 命中只写
   `gate_reason` 且行照记照结算（方案 A：纸面是唯一在跑的 live-like 样本，砍数据
   削弱统计力；被闸行即"不熔断会怎样"的反事实）。**必须锁存**：paper 下被闸行照常
   结算会让当日 P&L 回升过线、无锁存则自动复牌，故判据叠加"当日已有被闸行"
   （`Recorder.GatedOn`，磁盘真相 → 重启自动恢复、UTC 跨日自动归零）。
   `LiveExec.BreakerOpen` 是**历史反极性字段**（true = 可开单），新类型改用
   `RiskSummary.CanTrade` 直说极性。分析脚本（02/06/07）默认过滤 `gate_reason`
   非空行，`--include-gated` 恢复旧口径。
11. **配置分层：CLI flag > 配置文件 > 代码默认值（无 env 层）**（2026-09-16）：
   配置包 `internal/config`（viper，同 master），`main()` 只留 4 个引擎 flag
   （+ 1 个工具 flag `-encrypt`，见「敏感字段」节）。
   默认值**唯一来源**是 `defaults()`，`Load()` 把它预置成 `UnmarshalExact` 的目标
   ——mapstructure 只写输入 map 里出现的键，所以"文件缺哪个键，哪个键就是默认值"，
   文件可只写要改的项。
   - **为什么不带环境变量层**：viper 的 `AutomaticEnv()` 单独用是**假生效**——
     `Unmarshal → getSettings(v.AllKeys())` 而 `AllKeys()` 只汇总 aliases/override/
     pflags/**显式 BindEnv**/文件/SetDefault，env 探测到的键不在内；要修得逐叶
     `SetDefault` 注册（还得给 nil 指针子树单独 `BindEnv`），判定不值当。
     **以后别再加回来**（viper 的 env 系 API 一个都没调）。
   - **`UnmarshalExact`（拼错的键 = 启动失败）是有意的**：策略参数静默回落默认值
     比崩溃危险得多。
   - **敏感字段**（`sdk.polymarket.owner_key`/`clob_creds`/`relayer_key`）沿用 master：非空即密文
     （AES-256-CBC, key=SHA256(密码)），密码走 `PM_CONFIG_DECRYPT_PASSWORD` 或终端
     无回显输入；四项全空则**不弹密码**（纸面运行永不卡在输入）。
     `POLYMARKET_*` 环境变量已**全部废弃**，凭证只能来自配置文件。
   - `v4.config.yaml` = 默认值镜像（入 git），有漂移守卫测试（逐键 `DeepEqual`
     `defaults()` + 覆盖度检查）；调参复制成 `config.local.yaml`（gitignored）。
   - ⚠️ `sdk.http_timeout: 10s` **必须带单位**（写 `10` = 10 纳秒）。
   - SDK 默认值以 `sdk.DefaultConfig()` 为**基底**（端点 URL/超时/签名类型不手抄），
     只覆盖两处：`OwnerKey` 清空（SDK 给占位私钥 `1111…`，会被「非空即密文」判成
     密文，且 main 以空串为「只读纸面」判据）、`RateLimit*` 复位 0（SDK 的
     3 次/500ms 会盖掉它自己的内建兜底 6 次/1000ms，≤0 才走兜底）。
12. ⚠️ **已被决策 #14 / #15 相继取代（2026-09-18 / 09-19）**——「锚缺失才恢复」不再是
   独立分支，取值口径与「到达龄 abs」判据都已换掉（见 #14/#15）。本条保留作为**历史与
   理由**：其 §1（锚缺失成因）、§5.1（镜像红线）、§6（频率实测）、§9（σ 冷启动 / 假 late）
   仍然有效；其中「迟到 30s 的推送带 0.3-0.5σ 漂移」是 09-18 `anchorPickTol` 取 10s 的
   依据（已随精确匹配废除），也是今天**只用精确那一秒**的动机之一。
   原文：**锚缺失恢复：官方 3×20s 重试 + 边界窄窗口推送直采**（2026-09-16，见
   `docs/dog020_anchor_recovery_2026-09-16.md`）：边界采样不到 TWAP-60 流值时不再直接
   丢整窗——起 `feed.RecoverAnchor`（窗口级 ctx，`cmd/flip` 常量 3 次/20s/10s 超时），
   源优先级 **官方 `FetchOpenPrice` > 推送**，且推送仅在**到达时刻距边界 ≤
   `feed.max_twap_age_ms`(10s)**（判据取绝对值——边界前到达的陈旧推送正是故障源本身）
   时直采，因为迟到 30s 的推送值带 ~0.3-0.5σ 漂移、足以改变浅洞带的进出。
   三个要点：
   - **恢复期 tick 照常喂 ring，只闸住触发判定**：m_45 急跌腿与回测 1:1（回测锚从不
     缺失），期间触底逐 tick 记 `lost_triggers.reason = anchor_pending` 留痕，恢复后
     不追溯；锚未就绪期**不**重复记 stale_book/book_missing。`SetAnchor` 幂等，成功即清
     `AnchorMissing`，`stats.AnchorMissing` 新语义 = **窗口结束时仍未恢复**。
   - **3 次失败 = 整窗不观测**（此刻任何可用推送早已错过窄窗口），保证永不产出
     `anchor=0` 的观测行——`06_oos_review.py:161-167` 对 anchor/hist_bps/fill 为 0 的
     硬检查即是这条红线。`anchor > 0` 的路径逐字节不动（btreplay 625 笔对账）。
   - 可见性：`winstats_*` 增 `anchor_src`（official\|push）与 `anchor_recovered_ms`
     （边界后多久恢复），失败窗口 `anchor_missing=true`；dashboard「丢信号」栏区分
     `anchor_pending` 与 `无快照`。
13. **σ 未就绪整窗跳过（`skip=no_sigma`）**（2026-09-16，见
   `docs/dog020_anchor_recovery_2026-09-16.md` §9.2）：冷启动时本地 `windows_*`
   预热不可用（断档/最新窗超 `localFreshMax`）会回退**异步**网络预热（`warmupSigma`
   goroutine：18 窗逐窗 1s + 429 退避，实测 >70s）——边界落在它完成之前时 `hist.Bps`
   恒 0、`BeginWindow(anchor, 0)` 把 σ 冻住整窗且**无热更新路径**：任何触底都被
   `no_hist` 拒（首触即 Done → 必然零信号），却仍落一条 `hist_bps=0` 观测，踩中
   `06_oos_review.py:161-167` 硬检查。与锚缺失同一原则（数据源不可信 → 本窗不观测）
   → `hist.Count() < flip.HistMin` 即**整窗跳过**：不预取/不订阅、落 `skip=no_sigma`
   的 `winstats_*` 行，下一窗（5min 后）预热早已完成，不会连跳。
   - **不取「预热完成后热更新 σ」**（`SetHistBps` 式）：会把「窗口级常量 σ」变成时变量，
     同一事件的观测行 `dist_s` 取决于 σ 何时落地（不可复现），回测也无对应形态
     （btreplay 每事件 σ 固定）；收益仅 ≈0.15 笔/冷启动，不值。
   - 副作用：本窗无 close 采样 → `windows_*` 缺一行，等价于停机窗（`RecentBlock`
     600s 缺口容差吸收，同 `dup_record`）。`no_hist` 由此与 `missing_anchor` 一样
     **现网不可达**，`decide` 分支保留为纯函数防线。
   - 频率：全量纸面数据 3544 条观测里 `hist_bps=0` 仅 2 行（09-03 15:40Z /
     09-16 13:10Z），均为冷启动窗；修后不再新增（历史两行留在数据里，复查须知）。
14. ⚠️ **取值口径已被决策 #15 取代（2026-09-19）**——「`Latest()` 初值 + 按评估时刻重选
   （容差内取最近）+ 官方 +40s 覆盖」整套已被**精确匹配那一秒**取代；`anchorPickTol`、
   `AnchorUsableAtBoundary`、`anchor_init`/`anchor_pick_ms` 均已删除。本条保留作为
   **历史与实测依据**：其「官方 open 头几十秒是临时值、+40s 才收敛」（p90 1.18bps ≈ 9.5
   美元）与「对齐推送 == 收敛官方（8 窗逐位相同）」正是 #15 的直接依据。
   ~~**锚升级：推送缓存按「评估时刻」重选 + 官方 open 间隔轮询**~~（2026-09-18，见
   `docs/dog020_anchor_upgrade_2026-09-18.md`，取代决策 #12 的取值口径）。起因：锚是
   结算线口径，而 t=0 的 `Latest()` 是**最新到达**的那条推送——服务器发布延迟 ~1.3-2.3s，
   所以它拿到的是「边界**前**一秒」的评估值。实测：官方 `openPrice` **就是边界那一秒的
   推送值**（3753 窗里 1717 窗逐位相等，对应推送到达 p50 +1.64s），t=0 口径与官方差
   p90 0.24bps、|差|>0.5bps 占 7.3%。三件事：
   - **推送缓存**：`TwapAdapter` 环存最近 `twapPushCap=100` 条（@1 条/s ≈ 100s，须 >
     末点 +40s + fetch 超时 10s），`PushNearest(边界, tol)` 取**评估时刻**（`payload.
     timestamp`，整秒格）距边界最近的一条，返回 `pickMs` 偏移（并列取后到者）。
     `price<=0` 不入环；缺时间戳回退本地到达并打一次日志。**删除 `LatestStamped`**。
   - **升级通道**（`feed.RecoverAnchor`，**每窗都起**，取代「锚缺失才起」）：t=0 仍用
     `Latest()` 当初始 open；+1s 起每秒重选（边界那一秒的推送 p90 在 +2.28s 才到，
     定点单采会漏），**仅 `|pick| < |基准|` 严格变小才升级**（基准 = t=0 的偏移，
     两套口径在乱序/重连补发下不单调）；官方 open 在 **+2/+5/+10/+20/+40s** 各试一次
     （单次超时 10s），**卡顿跨过的点不补发**（迟到值已无意义）。
     HTTP 取数下沉 `feed.NewOpenPriceFetcher`（`cmd/flip` 只构造注入）。
   - ⚠️ **官方值要 ~+10~40s 才收敛，故只有最后一个采样点（+40s）作权威覆盖**
     （当日追加, doc §9）：官方接口头几十秒返回的是**未收敛的临时值**——同窗实测
     15:35 窗 +6.9s=80718.92 → +33.3s/+62.9s=80721.40；15:40 窗 +2.8s 与 +10.3s 同为
     80721.68 → +40.4s 才跳到 80722.35。17 窗配对检验（引擎 +2.5s 取值 vs 事后重取）
     **13/17 不等, |差| p90 1.18bps ≈ 9.5 美元**——比它要修的 t=0 误差（p90 0.59bps）还大。
     而**对齐推送与收敛官方只差 0.0002-0.0006bps（3 厘美元）= 同一个值**（8 窗）。
     故 `AnchorUpgradeOpts.SettleAfter`（生产 = +40s = 末点）之前的官方成功值**仅在无流值
     锚时兜底**（否则会把已知精确的边界那一秒推送换成未收敛临时值），收敛点及之后
     **每次成功都采纳（last-wins）**；通道**不再因官方成功而关闭**（ctx / Schedule 用尽 /
     硬停才关）。不要用「两次采样同值」当收敛判据（15:40 窗 +2.8s 与 +10.3s 同值却仍是
     临时值）。副作用：`rem>260` 的早触发信号（观测里 22%）锚记成 `stream`（真值，不吃亏）。
   - **兜底与冻结**：官方全失败 → 沿用流值锚照常判定；**连 t=0 初值都没有且全窗无
     可用值 → 本窗不产出观测**（引擎 `anchor<=0` 天然路径，**不加新闸**）。
     `SetAnchor` → `UpgradeAnchor(anchor, histBps) bool`：`state != Watching`（产出观测
     或 `rem==0` 终 tick 两种 Done）即拒——改锚**不追溯**已落盘的观测行；**锚与 σ 必须
     同源同换**。可见性：`anchor_src` 取值改 `official|stream`，`winstats_*` 增
     `anchor_init`（t=0 初值）与 `anchor_pick_ms`（评估偏移；**0 = 最好档**，故不带
     `omitempty`；非整千 = 本机与服务器时钟漂移探针），`windows_*` 行补 `anchor_src`
     （`RecentBlock` 只识断档、不识这种水平位移）。
   - **不是 P&L 杠杆**：14 天 625 条组合信号换锚后掉出 11 / 新增 9（≈ 中性）；真实收益
     是口径正确 + 锚缺失窗口不再白丢 + 「锚偏了多少」首次逐窗落盘。**σ 的 close 口径
     未改**（仍 `lastTick.TwapPrice`，到达口径）——open/close 有 ~1.5s 不对称，已知未做。
     验收红线不变：btreplay 625 笔逐位一致 + `go test ./internal/... -race` 全绿。
15. **锚 = 边界那一秒的 TWAP 推送（精确取锚）**（2026-09-19，见
   `docs/dog020_anchor_exact_open_2026-09-19.md`，取代决策 #14 的取值口径）。既然
   「评估时刻 == 边界」那条推送就是官方 `openPrice`（8/8 窗逐位相同，差 ≤0.0006bps ≈
   3 厘美元），就**只留精确那一条**，不再有近似锚、不再有官方 HTTP 覆盖：
   - **精确匹配**：`PushNearest(windowStart)` 只认 `tsMs == windowStart.UnixMilli()`
     （整秒格），删容差/删「取最近」；返回 `(price, arrivedMs, ok)`，`arrivedMs` 是**该条
     推送的本地到达时刻**（发布延迟的无偏观测——用轮询时刻会被 500ms 相位虚高）；
     重复时间戳取**后到者**。新增 `CacheStat()` 诊断快照（条数 / 最新一条评估偏移 /
     缺时间戳条数），专供失败日志区分**服务器没发 / 我们收晚了 / 时间戳缺失**。
   - **取锚通道**（`feed.RecoverAnchor`，每窗都起、**主循环零等待**）：① 精确段——
     立即试一次后每 **500ms** 一次、最多 **40 次 = 20s**（该推送到达 p50 **+2.0s**、
     实测最晚 +12.1s），**命中即终局**并关通道；② 官方段——代码原样保留但**生产休眠**
     （`Schedule` 为空即零请求，回归测试 `TestRecoverAnchorOfficialDormant` 钉住），
     将来允许 40s+ 延迟时在 `cmd/flip` 接回 fetcher + `Schedule` 即可。
   - **不设过渡锚**：窗口开局 `BeginWindow(0, 0)`（不再取 `Latest()`，`Latest()` 仅剩
     看门狗/dashboard 用途）；锚未到手期间 tick 照常占槽与计数、
     `lost_triggers(anchor_pending)` 留痕、**只闸住触发判定**（决策 #12/#13 同原则，
     **不加新闸**）；**20s 未命中 = 本窗不产出观测、不计入 σ**（明确取舍：宁可丢窗，
     也不拿近似锚判定）。代价上界有硬数据：625 笔组合信号最早 `rem=295`（边界后 +5s）
     且 `rem≥290` 仅 2 笔 ⇒ 正常路径（+2s 到手）**历史样本零损失**。
   - **可见性**：`winstats_*` 删 `anchor_init`/`anchor_pick_ms`、增 **`anchor_exact`**
     （**不带 `omitempty`**：false = 本窗无锚，且它是 python 侧区分 09-18/09-19 行的
     哨兵键）；`anchor_src` 保留（官方休眠时恒 `stream`）；`anchor_recovered_ms` 语义
     改为「该推送本地到达时刻距边界」。`stats.AnchorMissing` 语义随之变化：**每窗开头
     都会短暂为真**，期末仍为真才是「整窗无锚」——`07_source_health_check.py` 的
     「锚缺」列据此改判**期末**（新行看 `anchor_exact`，老行回退 `anchor_missing`）。
   - **不是 P&L 杠杆**：btreplay 625 笔逐位一致（回测路径只喂 `BeginWindow(ev.TwapOpen,
     σ)`，不碰 `internal/feed`）、01 四条基线不变；σ 的 close 口径仍取
     `lastTick.TwapPrice`（**到达**口径，与 open 的评估口径有 ~1.5s 不对称，已知未做）。
16. **live 下单 = GTC 限价挂单 + `rem ≤ RemMin` 撤单 + 撤单时定稿**（2026-09-19，
    用户决定「保住盈亏比」）。FAK（taker-limit：POST 那一刻吃 ≤ 限价的档位、余量
    立即撤）已删除，`orders.GTC` 是唯一提交路径。动机是**脆弱性**：tick 采样 → POST
    到达有 1~3s 延迟，触发那一瞬挂在 0.20 的卖单常已被吃走/撤走，FAK 只能吃零头或
    整单落空；GTC 挂着等，谁在窗口内砸出来就接住谁。
    - **挂单只挂到 rem ≤ 180s**（策略时间腿 `flip.rem_min`，`cmd/flip` 传给
      FillTracker；**不是硬编码常量**）：到点由我们自己发 `DELETE /order` 撤掉未成交
      余量（新增 `TradeClient.CancelOrder`）。用户口径：**回测前提是「触发瞬间必成
      交」**，rem ≤ 180 之后才成交的样本不是这条策略要的（砸到 0.2 后一路拖到窗口
      尾盘才被吃掉的那批，正是逆向选择最重的子样本）。撤单是**尽力而为**：失败每
      2s 重试到成功或硬截止，撤单没成功的行照常按实际成交落盘（note 里不会有
      「余量已撤」）。撤单点之前不撤（挂单继续等对手方），已满额成交不撤（没余量，
      省一次废请求）；**查询失败时照撤**（漏撤的代价远大于一次废请求）。
    - **POST 不再是终态**：GTC 响应只描述 POST 那一瞬（`status=live` 即「已挂上簿」，
      `taking/making` 是当下已成交部分）。新状态 **`ExecStatusResting`** ＝ 订单在簿、
      成交量待定稿；`filled` 只留给「即时全额成交」（sanity 全过且 shares ≈ 请求量）。
      **`unfilled` 不再是 POST 能给出的结论**——只有定稿（撤单后读到 `size_matched`）
      才知道它是 0 成交。
    - **`trading.FillTracker`**（新文件，长驻 goroutine 同 ResolutionPoller 形制）:
      `Register`（POST 返回 resting / live 启动接管磁盘残留）→ 每 **2s**
      `GetOpenOrders(Id)` 读 `size_matched` + 到点撤单 → 定稿回调
      `ExecState.ApplyFillFinal` → `Recorder.CompleteRestingFill` 落盘。终态判据五条：
      ① `status ∈ {MATCHED, CANCELED}`；② **挂单表已无此单**（我们撤单生效 / 闭市撤
      回）——仅在**本进程至少见过一次**该单（`sighted`）或**重启接管**（`adopted`）时
      才可推定，否则「查不到」可能只是索引延迟；③ `size_matched ≥ 请求股数`；
      ④ 撤单成功却仍在簿 → **撤单确认宽限 15s** 用尽即按末次观测定稿（note 标注）；
      ⑤ 硬截止 = 闭市 + **60s** 宽限（撤单一直失败的兜底；先查询后判截止，重启接管
      也能拿到真值）。任一未定 ⇒ 保持 `resting`。
    - **接管遗留行**: 重启扫盘登记的 resting 行撤单点通常早已过去——第一轮查询若发现
      它还活在簿上就立即撤掉（崩溃前挂的单不该继续吃成交）；查不到则照旧转人工。
    - **绝不臆造仓位**：无法确认（从未查到 / `size_matched` 语义不明）⇒ 行留
      `resting` + `ExecNoteUnknown`，只更新 note 让 `NeedsReconcile` 捞出来人工核对
      （按 `order_id` 去 data-api/UI）。**成本口径 `cost = shares × 限价`**：挂单成交
      必是 maker 成交 = 限价本身，即时 taker 那部分只会**更便宜** ⇒ 成本至多略微高估
      （保守，且与回测 `shares = stake/fill` 同口径）。
    - **结算注册后移**：resting 行 `IsFilled()==false`、不进结算轮询；改由定稿回调
      在 `ApplyFillFinal` 之后注册（gamma 结算在闭市后数分钟，口径不受影响）。
      重启时 main 扫盘把残留 resting 行按 `adopted` 重新登记。
    - **已知样本偏差**（不是免费午餐）：挂单越久，成交样本越偏向「价格继续下探」的
      那批——反弹回去的单子根本不会成交。回测 WR 24.6% 对应「触发瞬间拿到位置」，
      挂单成交的子样本会系统性更差；撤在 rem ≤ 180 只是把偏差**截短**，不消除它，
      真答案仍要等实盘样本（撤单点越早、样本越接近回测口径，但也越难成交）。
    - **GTD 不可用**（试过，放弃）：CLOB 的 GTD 规则是「stated expiration 前 60s 就
      被安全阈值撤」，且 expiration 必须 ≥ now+180s ⇒ 最小有效挂单期 ~2 分钟——对
      5 分钟窗口（触发时 rem 只剩 ~3 分钟）几乎等于挂到闭市；更硬的一层是 SDK
      `PostOrder` 把 expiration 硬编码为 `"0"`（`orders.OrderToDTO(..., "0")`）且它
      不在 EIP-712 签名结构里，客户端要用上 GTD 得改 SDK。故撤单由我们自己发。
      （2026-09-21 在 v0.7.2 上复核过：结论不变。该版本新增的 `UserOrder.Expiration`
      是**幌子**——它只流进 `Order.Expiration` 这个**未签名**的结构体字段，而
      `_ORDER_EIP712_TYPES` 仍是 11 个字段、不含 expiration；真正上线的 DTO expiration
      仍是 `polymarket/polymarket.go:555`（`PostOrder`）与 `:612`（`PostOrders`）里写死的
      `"0"` 实参，且没有任何入口能改它。）
      **下单路径仍是单次网调**：`CreateOrder` 只查 tickSize/negRisk，两者由
      `PrefetchTokenInfo` 每窗预热（SDK 内部不读 feeRate，`ResolveFeeRateBps` 无调用
      点），`PostOrder` 无附加请求；撤单是独立的 `DELETE /order`（一分钟一笔量级）。
17. **扫尾盘 ⑤ = 独立进程 + 独立引擎包 + 复用原语**（2026-09-23，用户要求「按文档逻辑
    与 flip 代码结构实现纸面与实盘到 ./cmd/tail」）。规格见
    `docs/tail_sweep_2026-09-22.md`，映射见 `docs/tail_engine_mapping_2026-09-23.md`。
    `internal/tail` **单向依赖 `internal/flip`**（取 `Tick`/`Executor`/`PaperExecutor`/
    `HistState`/`WindowEntry`/`CanTrade`/`SideYes|No`/`WonFor`/`ExecStatus*`），反向不依赖
    ——两族独立演进，但共用同一批经对账的原语。四条关键决定：
    - **两个独立的一次性闩锁（两帧折中）**：`rem ≤ frame_rem(150)` 首帧落**原始快照**
      （只记录、不判定、不下单）+ `rem ≤ rem_start(60)` 首帧落**决策快照**（判定 ⑤
      + 执行）。`ProcessTick` 返回 `[]Observation`（0~2 行，首个有效 tick 若已在尾盘
      则两行同发）。代价：**T 不可再调**（离线只能复算 60/150）。两行共用同一锚——
      首行落盘即冻结（`UpgradeAnchor` 此后拒收），否则帧行与快照行 `dev` 基准不同源。
    - **前缀独立**：`tail_*`（≤2 行/窗）/ `tailwin_*`（每完成窗 1 行, σ 预热源）/
      `tailstats_*`（**严格每窗 1 行**, 健康度 + `skip` + 锚状态）——**不得**复用
      `windows_*`（双进程双写会毁 σ, 决策 #9 的教训）。flip 与 tail 可同目录并行。
    - **撤单点 = 闭市 `rem ≤ 0`**（用户决定）：`trading.CancelAtClose` 哨兵 =
      `time.Nanosecond`，⚠️ **必须微小正数**——`NewFillTracker` 对 `cancelLead ≤ 0`
      一律回退 180s，而 tail 在 `rem≈60` 才挂单，回退会让第一轮轮询就撤单
      （表现为「策略零成交」+ 一行看似无害的警告）。为此 `FillTracker` 增
      `RegisterOrder(FillOrder,…)`（加法式，`Register(*flip.Record,…)` 转调它，flip
      调用点与测试零改动）。
    - **参数全部不可调**：`price_min 0.80` / `dev_min_usd 63` / `sigma_min_usd 40` /
      `rem_start 60` / `frame_rem 150` / `max_book_lat_ms 300` 都是回测标定值，且
      文档 §4.3 已判定 `40` 不可辨识（真 argmax=35，2000 次重采样里 40 一次没中）
      ——配置键的意义是「能读能对账」，不是「该调」。`internal/config` 只做**结构性**
      校验（阈值 > 0、价格腿 ∈ (0,1]、`frame_rem ≥ rem_start`）。
    - **验收红线**：`internal/tail/parity_test.go`（**opt-in**, `data/btc` 不存在即
      skip）流式重放 14 天 3753 窗驱动真实引擎，断言五格聚合 = python oracle
      （① n=3109 WR 97.65% +72.10U … ⑤ n=1536 WR 99.61% +36.93U）+ 两个闩锁计数
      （frame 3643 / snap 3634 窗）——实测 n 逐位相等、WR/P&L 在两位小数内相等。
      ⚠️ 两条对账口径：宇宙须过滤「快照 tick 上 spot+twap 同时在场」；σ 须**按事件
      索引**现算（python 语义）后注入，**不能喂 `flip.HistState`**（后者只记已 push
      的振幅，缺窗时条数不同）。
    - **已知未做/差异**（写进 mapping 文档 §4）：① live 是 GTC 挂单等成交（挂到闭市），
      成交样本天然偏向「热门侧走弱」，与回测「快照瞬间即成交」**不是同一个估计量**
      ——**首次只跑纸面**；② 两族同开实盘时日亏熔断**各自一条独立 24U 线**（等效 48U），
      文档 §4.7 要求的合并**未实现**。

---

## 运行方式

配置优先级：**CLI flag > 配置文件 > 代码默认值**（三层，**不含环境变量层**——
唯一被读取的环境变量是 `PM_CONFIG_DECRYPT_PASSWORD`）。默认值唯一来源 =
`internal/config/config.go defaults()`，根目录 `v4.config.yaml` 是它的逐键镜像
（`configfile_drift_test.go` 守着，改值会测试失败）。

```bash
# 纸面运行（默认值 + Dashboard）
go run ./cmd/flip -config v4.config.yaml -dashboard :8090

# 不带 -config = 完全不读文件、纯代码默认值（不开 Dashboard）
go run ./cmd/flip

# 单点覆盖（最高优先级；-dashboard "" 能真的关掉配置文件里的地址）
go run ./cmd/flip -config config.local.yaml -stake 5 -mode live

# 扫尾盘（第二条策略线, 独立进程可并行跑；无 Dashboard、无 -encrypt）
go run ./cmd/tail -config v4.config.yaml
go run ./cmd/tail -config config.local.yaml -stake 2 -mode paper
```

### CLI flag（`cmd/flip` main() 只有这 4 个引擎参数 + 1 个工具 flag）

| flag | 默认 | 含义 |
|------|------|------|
| `-config` | `""` | 配置文件路径。**空 = 不读任何文件**，直接用代码默认值 |
| `-dashboard` | `""` | 覆盖 `runtime.dashboard_addr`；显式传空串 = 本次不开 Dashboard |
| `-mode` | `""` | 覆盖 `runtime.mode`（paper\|live）|
| `-stake` | `0` | 覆盖 `flip.stake`；**没给**则用配置值，显式给 0 会校验报错 |
| `-encrypt` | `false` | **凭证加密工具**（唯一「跑完即退」的分支）：读明文 → 密文写 stdout → 退出。不读配置文件（`-config` 一并忽略）、不碰数据源、不跑引擎 |

用 `flag.Visit` 区分「没给」与「显式给空/0」——所以 `-dashboard ""` 是有效的关闭操作，
不是"用默认值"。其余全部参数见 `v4.config.yaml`（全量带注释），主要几项：

| 配置键 | 默认 | 含义 |
|------|------|------|
| `flip.rem_min` | 180 | 策略时间腿：仅 `rem > 180` 的触底才判定（与回测 1:1）。**同时是 live 撤单点**——`cmd/flip` 把它传给 FillTracker，到 rem ≤ 180 撤掉未成交挂单（决策 #16） |
| `flip.max_book_lat_ms` | 300 | PM 盘口延迟闸：`book_latency_ms` 超此值的 tick 无效（**回测 `MAX_LAT` 同值，收紧是负收益**，见 `docs/dog020_risk_latency_plan_2026-09-16.md` §1.2b） |
| `feed.max_spot_age_ms` | 2000 | Binance spot 新鲜度：距本地接收超此值判现货缺失（`missing_spot`） |
| `feed.max_twap_age_ms` | 10000 | TWAP-60 新鲜度：**只**管窗末 close（锚走精确匹配，不吃到达龄），超龄按缺失处理。历史上与 `anchorPickTol`(10s) 同值但语义无关，后者已随决策 #15 废除 |
| `risk.max_daily_loss` | **−24** | 日亏熔断线（负值）：当日（UTC）已结算 P&L ≤ 此值即当日停单并锁存；两模式同源（paper 只标记不拦单） |
| `runtime.output_dir` | `data/v4` | 观测 JSONL 输出目录（live 建议独立目录，见启动时的 paper/live 混行告警；**flip 与 tail 可共用**——三前缀各自不撞） |
| `runtime.slug_prefix` | `btc-updown-5m` | 市场 slug 前缀 |
| `tail.*` | 见下 | 扫尾盘 7 键（`rem_start 60` / `frame_rem 150` / `price_min 0.80` / `dev_min_usd 63` / `sigma_min_usd 40` / `stake 2` / `max_book_lat_ms 300`）——⚠️ **全部不可调**，见决策 #17 与 `docs/tail_sweep_2026-09-22.md` §4.3 |

`cmd/tail` 只认 **3 个 flag**（`-config` / `-mode` / `-stake`，语义同 flip 那三个，
`-stake` 覆盖的是 `tail.stake`）——没有 `-dashboard`（Dashboard 硬绑 `*flip.Recorder`，
泛化不划算）也没有 `-encrypt`（用 flip 的那个）。

启动校验（`internal/config/validate.go`，判**最终生效值**）：三阈值必须 > 0、
`risk.max_daily_loss` 必须 < 0、`runtime.mode ∈ {paper, live}`、`flip.stake > 0`、
`runtime.output_dir` 非空 —— 任一不满足即启动失败；`flip.max_book_lat_ms < 100` 只告警。
配置文件里拼错的键（`UnmarshalExact`）也是启动失败，不静默回落默认值。

### 敏感字段（`sdk.polymarket.*`）

凭证**只能来自配置文件**（`POLYMARKET_*` 环境变量已不再读取）：`owner_key` /
`clob_creds.{key,secret,passphrase}` / `relayer_key.{key,key_address}` 留空 = 只读运行
（引擎自动生成临时密钥跑纸面）；填 **密文**（`pmutils.NewEncryptor(密码).Encrypt(明文)`，
AES-256-CBC）则实盘可用。判定语义是**非空即密文**——明文写进去会在解密时启动失败
（没有"看起来像明文"的兜底）。**凭证块一律整块加密**：`relayer_key` 的 `key_address`
虽是地址（公开信息）也走同一口径——半加密会让另一个字段以密文形态被当成地址发出去
（`RELAYER_API_KEY_ADDRESS` 头），且"哪个子字段该明文"是本包刻意不留的规则。
`builder_creds` 目前**不在**加密名单（本引擎未使用；要接 builder 时一并加进
`decrypt.go` 的 `appendCredTargets`）。

**生成密文用 `-encrypt`**（2026-09-20 新增，逻辑在 `internal/config/encrypt.go`，
与 `decrypt.go` 对称、**共用同一个密码来源**——密文只能用启动时那个密码解开）：

```bash
go run ./cmd/flip -encrypt                    # 终端: 无回显粘贴明文 → 密文打到 stdout
printf %s "$RELAYER_KEY" | PM_CONFIG_DECRYPT_PASSWORD=… go run ./cmd/flip -encrypt   # 管道整段读取
```

- **明文不进命令行**: 没有 `-encrypt <明文>` 这种形态（会进 shell 历史与 `ps`）。
  stdin 是终端 → `term.ReadPassword` 无回显; 是管道 → 读**整段**（不是逐行——多行
  秘密会被静默切成多个密文）并 TrimSpace。
- **stdout 只有密文**（可管道进剪贴板/脚本），提示与自检指纹走 stderr——指纹 =
  长度 + 首尾各 4 字符，短于 24 字符只报长度（贴错东西时密文照样合法，启动不会报错，
  这是唯一的人工自检点）。
- 加密方案由 SDK 决定，**不自行改动**（换 IV/加盐 = 既有密文全解不开）: 代价是确定性
  ——固定 IV（= SHA256(密码) 前 16 字节）意味着同密码 + 同明文 → 同密文。
- 与引擎无关: 该分支在 `config.Load` 之前 return，不读配置、不校验、不落盘。

| 变量 | 说明 | 必填 |
|------|------|------|
| `PM_CONFIG_DECRYPT_PASSWORD` | 密文凭证的解密密码；不设则终端无回显输入（**nohup/systemd 无终端 → 必须设它**） | 仅当配置文件里有密文 |

> 本地运行记得 `export https_proxy=http://127.0.0.1:1087`（Polymarket 直连超时；这是
> Go 标准库 `http.ProxyFromEnvironment` 读的，与上面的配置系统无关）；
> 部署机勿设指向不通代理的 HTTP(S)_PROXY（Binance 拨号走环境代理）。
