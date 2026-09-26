# CLAUDE.md — Flip Signal

## 项目概述

**Flip Signal** = 针对 **Polymarket 5 分钟涨跌市场**（`btc-updown-5m`，Chainlink TWAP-60 结算）
的量化交易系统，含**两条独立并行**的策略线（各自进程、各自 Dashboard、各自数据前缀）：

| 策略线 | 方向 | 实现 | 现行权威规格 |
|--------|------|------|--------------|
| **狗@0.2**（flip） | 买被砸到 ≤0.20 的**冷门侧**（逆向） | `cmd/flip` + `internal/flip` | `docs/engine_plan_dog020_2026-09-02.md`（方案）/ `docs/dog020_mapping_2026-09-02.md`（口径映射） |
| **扫尾盘 ⑤**（tail） | 买 ≥0.80 的**热门侧**（顺势） | `cmd/tail` + `internal/tail` | `docs/tail_integrated_2026-09-24.md` / `docs/tail_engine_mapping_2026-09-23.md` |

> **狗@0.2 的核心原则**：不预测涨跌，只判断「市场刚把某个 outcome 砸到 0.2 的极端折价
> ——砸出急跌坑，现货却没真正走出来」，买折价本身。
> **扫尾盘的核心原则**：尾盘买已经赢定的热门侧，赚的是「赢了但还没到期」的确定性溢价。

### 第一条策略线：狗@0.2（当前逻辑）

- **信号条件**（触发与急跌腿来自 PM 订单簿；浅洞腿输入 Binance spot）：
  - 触发：每事件首个有效 tick 上某侧 ask 满足 `0 < ask ≤ 0.20`（唯一触底侧即狗侧；
    两侧都 ≤0.2 的交叉态取 `sgn·(spot−anchor) < 0` 一侧，14 天历史 0 次）
  - 急跌：m_45 = 触发前 45 个 tick 槽位内同侧 ask max ≥ 0.40
  - 浅洞：`dist_s = sgn·(spot−anchor)/anchor·1e4/hist_bps ∈ (lo(side), 0)` 开区间，
    侧别带 **lo = yes −0.6 / no −1.0**（组合版）
  - 时间：`rem > 180`（窗口前 ~2 分钟）
  - σ（`hist_bps`）= 前 ≤18 个已完窗口 `|tw_close − tw_open|` 均值（≥3 窗可用）
- **成交口径**：`fill` = 触发 tick 狗侧 ask（≤0.20，无滑点）；`shares = stake/fill`；
  赢 → `shares − stake`，输 → `−stake`（每股兑 1U）
- **结算**（三层回退，决策 #19）：官方 outcome（0=Up 1=Down）——
  ① 边界推送自算（闭市 +10s，与官方逐位同源）→ ② 官方 crypto-price 接口（+45s）→
  ③ gamma `umaResolutionStatus=="resolved"` 轮询；每行落 `settle_src`
- **回测基准（14 天，2U/笔，2026-08-18~31，`python/v4/01_backtest_r1.py`）**：
  n=625 WR 24.6% EV **+0.633U/注** +396U/14 天，日正 11/14（h1 +133 / h2 +263）
- **状态**：🟡 **维持纸面、不启动实盘**。09-15 双样本复验判定「**不显著**」
  （主窗 09-04~09-16 n=438 WR 20.3% EV +0.199U/注 +87.2U，落 (−80,+100) 段；
  缺口全在 yes 侧——no 侧 WR 不变；信号频率闸门未过，归因触底时点后移 ~14s + σ −33%），
  **09-30 二次复查**。完整报告 `docs/dog020_oos_result_2026-09-15.md`，
  预设判据 `docs/dog020_oos_review_2026-09-15.md`。
- 🚫 **已推翻的调整（只此一句，不得重提——除非先有新样本）**：v1/v2/v3 flip 家族
  （flow_5s / follow-wait / 自信崩溃）、0.2 深度全市场扫、dist_t 第二层、浅洞带加宽/
  平移 k/网格重选、σ 窗 18→10、趋势条件带、rem 分桶、binance_open 换锚、Zcritical、
  v6 波动率×成交量；tail 侧的低σ放宽 dev 全部候选
  （`docs/tail_sigma_gradient_2026-09-26.md`）。历史分析/代码在 git 其他分支可查。

### 第二条策略线：扫尾盘 ⑤（当前逻辑）

- **判定链**（三段递进，任一段出信号即整窗只下一单）：
  **T=150 判 ⑤** → 不达标则 **T=60 再判 ⑤** → 仍不达标则**此后每秒判 ②**
  （② = 价格腿 ∧ `dev ≥ 63`，不含 σ 腿）。
  ⑤ = 热门侧**有效价**过 0.80（⚠️ **T=150 段严格大于**，T=60 与监听段 ≥，决策 #26）
  ∧（`dev ≥ 63 美元` ∨（`sd ≥ 40 美元` ∧ `dev ≥ sd`））；
  `dev = sgn·(spot − anchor)`（美元，正 = 朝押注方向，sgn: 押 yes +1 / no −1）；
  `sd = hist_bps × anchor / 1e4`（该窗 1σ 折美元）。
- **回测基准（14 天，2U/注，oracle = `python/v4/23_tail_integrated.py`）**：
  T=150 n=1208 WR 94.04% +15.02U / T=60 n=577 WR 99.13% +16.15U / 监听 n=348
  WR 97.99% +3.92U ⇒ **合计 n=2133 WR 96.06% +35.09U**（判定行 6412，参与判定 3640 窗）。
- ⚠️ **7 个配置键全部不可调**（`t150_rem 150` / `t60_rem 60` / `price_min 0.80` /
  `dev_min_usd 63` / `sigma_min_usd 40` / `stake 2` / `max_book_lat_ms 300`）——
  配置键的意义是「能读能对账」不是「该调」；`40` 在 2000 次重采样里一次都没成为最优。
  比较符**故意不做成配置键**（算子由段决定，见 `internal/tail.PriceLeg`）。
- **状态**：🟡 纸面登记中。`data/tail-live/` 09-24~25 已是**真实 GTC 挂单成交样本**
  （stake 10U，205 笔 +55.78U）。
- 🔴 **上 live 前的硬阻塞**：CLOB `minimum_order_size = 5 股`，而 `tail.stake=2U` 在 ≥0.80
  只有 2.0~2.5 股 ⇒ **低于交易所下限**（flip 侧 2U@0.20 = 10 股不受影响）。
  必须先定 stake ≥ 5U（每笔风险 2.5 倍，用户决定）+ 拿 1 笔小单验证下限；
  纸面 WR 量级不能直接外推到 live（live 是挂单等成交，成交样本天然偏向「热门侧走弱」，
  与回测「快照瞬间即成交」**不是同一个估计量**）。
- **待办**：① 纸面判决改离线脚本（判决卡已随整合改造整删，脚本**未写**）；
  ② 两族合并熔断未实现（flip/tail 各一条独立 −24U 线，等效 48U）；
  ③ 止损腿未落引擎（见决策 #24）。
- **ETH 上的扫尾盘 = 另一件事**（🔴 门槛不可跨标）：`dev_min_usd 63` / `sigma_min_usd 40`
  是 **BTC 价格的标定量**（ETH σ 中位 5.36 美元 vs BTC 62.56 美元）⇒ 两条腿都不可达。
  **等 ETH 采集 ≥14 个完整日后单独立项重标定**（判据/决策表预设于
  `docs/eth_data_review_2026-09-25.md` §5；**触发前不做任何阈值分析**），
  第一步是「**可成交性先行**」：`rem≤60` 决策点热门侧 `ask > 0` 占比 < 50% 直接判
  「ETH 不可执行」，不进入调参。

### 版本管理约定

- **分支即版本**：当前分支 `v4`。代码命名不带版本字眼；后续策略演进各开新分支
  （`git checkout -b v5`…），旧分支（v3/eth）保留全部旧代码/文档，可
  `git checkout v3 -- <路径>` 复活。新分支只保留有用文件；文档直接放 `docs/`。
- **提交规范**：提交信息用中文、组件前缀（`flip:` / `dashboard:` / `python:` /
  `cleanup:` / `docs:`），**不带 `Co-Authored-By` 等任何署名行**。

---

## 项目结构

```
FlipSignal/
├── cmd/flip/                         # 狗@0.2 引擎主入口（纸面/实盘同源, -mode 切换）
│   └── main.go                       # 窗口循环/数据源接线/anchor σ/执行编排注入（唯一文件; 4 个引擎 flag + -encrypt 工具 flag, 配置见 internal/config）
├── cmd/tail/                         # 扫尾盘 ⑤ 引擎主入口（独立进程, 自带 Dashboard; -config/-mode/-stake/-dashboard 四 flag）
│   └── main.go                       # 同上接线, 但三段判定链/尾盘闸/GTC 挂到闭市 + 窗口运行时载体
├── cmd/collect/                      # 数据格式 v2 高频采集器（**任意标的**）
│   └── main.go                       # 6 flag: -config/-asset/-slug/-symbol/-output/-custom-feature；资产由 feed.Asset 派生
├── cmd/compact/                      # 采集数据的合并修正（把 settlement_correction 行并回事件行; 与标的无关）
├── cmd/btreplay/                     # 逐笔重放 data/btc 驱动 flip.Engine（Go↔py 口径对账红线）
├── cmd/twapprobe/                    # 边界对齐探针（一次性实盘逐秒 TWAP 推送采集, 产 data/probe/; 证据工具, 运行期不参与引擎）
├── cmd/bookprobe/                    # 空侧盘口探针（一次性: 引擎同一条 SDK WS 逐秒记 raw/eng 双视角; 证据工具）
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
│   ├── dashboard/                    # 两族各自的 listener + 各自的静态目录（单包, 按族分文件）
│   │   ├── common.go                 # 共用: 分页信封/查询参数/JSON 响应/日志/单页入口/阈值 struct/逐日聚合
│   │   ├── flip_server.go            # go:embed flip + 路由装配（/api/state, /api/observations, /api/signals, /api/daily, /api/config）
│   │   ├── flip_handlers.go          # 上述 flip API 的 handler + 映射
│   │   ├── flip_state.go             # FlipState + NewFlipState（运行时组件引用）
│   │   ├── tail_server.go            # go:embed tail + 路由装配（/api/state, /api/snaps, /api/signals, /api/daily, /api/config）
│   │   ├── tail_handlers.go          # 上述 tail API 的 handler + 映射
│   │   ├── tail_state.go             # TailState + NewTailState
│   │   ├── flip/                     # flip 前端三件套 index.html, app.js, style.css（手机优先）
│   │   └── tail/                     # tail 前端三件套（统计九卡 + 信号表 + 决策表 + 逐日弹窗）
│   ├── feed/
│   │   ├── asset.go                  # 资产参数（btc → slug/Binance 交易对/Chainlink 符号/目录; 消除硬编码）
│   │   ├── anchor_recover.go         # 取锚通道（精确命中边界那一秒的推送, 500ms×40 重试；官方 open HTTP 段保留但休眠）
│   │   ├── binance_adapter.go        # Binance spot WS（交易对由资产派生; 本地接收龄）
│   │   ├── official_pair.go          # 官方 open+close 取数闭包（PricePairFetcher, 结算第二层用）
│   │   ├── pmtick.go                 # PM 盘口采样（best bid/ask 陷阱）+ token 解析
│   │   └── twap_adapter.go           # Chainlink TWAP-60（anchor/σ）+ FetchTwapRanges 预热 + 推送缓存/PushNearest(精确)/CacheStat
│   ├── collect/                      # 采集核心层（数据格式 v2 结构 + 成交分桶 + 修正队列）
│   │   ├── types.go                  # Event/HFTick/BinTick/PMTick/TwapTick/TradeAgg + 来源常量（push|official|stream）
│   │   ├── settle.go                 # SettlementWorker: 只服务推送缺失窗, 官方 open+close 轮询纠正
│   │   ├── trade_bucketer.go         # PM last_trade_price 按 token×秒聚合（OFI/max单/vwap）
│   │   ├── dedupe.go                 # transaction_hash 窗口内去重（防 WS 重连重放）
│   │   ├── compact.go                # 修正行合并进事件行（cmd/compact 的逻辑）
│   │   └── book_utils.go / market_utils.go / *_test.go
│   ├── settle/                       # 结算编排（零外部依赖, 不 import flip; 两族共用）
│   │   ├── settle.go                 # Outcome/Anchors（按边界秒存推送）+ Resolver 三层回退
│   │   └── settle_test.go            # 判定词表钉在 flip 常量上 + 三层时点/次数/幂等/剪枝
│   ├── tail/                         # 扫尾盘 ⑤ 引擎核心层（零外部依赖; 只复用 flip 的原语, 反向不依赖）
│   │   ├── config.go                 # Config + DefaultConfig()（7 个键, 全部不可调）
│   │   ├── types.go                  # Observation/Rules/Record/WindowStats + stage/kind/闸原因常量 + 状态机 + IsFilled/HasPosition
│   │   ├── decide.go                 # 纯函数 HotBook（ask 优先/bid 兜底）/ SgnFor / DevUSD / SigmaUSD / EvalRules + PriceLeg + Rule1()…Rule5()
│   │   ├── engine.go                 # 三段递进判定链（Watching→Await60→Listening→Done）+ Resume 崩溃续跑, ProcessTick 返回 0~1 行
│   │   ├── recorder.go               # tail_* / tailwin_* / tailstats_* / tailhold_* 四前缀（独立于 flip 三前缀, 决策 #9 红线）+ recomputePnL
│   │   ├── hold.go                   # 持仓监察（纯函数 HoldWatchRow + HoldRow）: 信号成交后逐 tick 记持仓侧盘口, **只记录不判定**
│   │   ├── exec_state.go             # 风控闸 + 两模式两阶段下单编排（flip.ExecState 的精简镜像; HandleDecision 单入口）
│   │   ├── snapshot.go               # LiveSnapshot/LiveExec + Snapshotter 接口（dashboard 只读消费）
│   │   └── *_test.go                 # decide（含 HotBook/PriceLeg）/engine/recorder/exec_state/hold/parity（opt-in, 钉 23 的 oracle）
│   └── trading/                      # SDK 依赖层（单向依赖 flip/feed, 由 cmd/flip 与 cmd/tail 各自构造注入）
│       ├── live_executor.go          # LiveExecutor 真实 GTC 限价挂单（实现 flip.Executor, 唯一 POST 点）
│       ├── fill_tracker.go           # GTC 挂单跟踪: rem≤RemMin（flip）/ 闭市（tail）撤单 + 查 size_matched 定稿回调
│       ├── prefetch.go               # 每窗预热 tickSize/negRisk/feeRate（下单路径零额外网调）
│       └── resolution_poller.go      # gamma 结算轮询（umaResolutionStatus; 结算第三层）
├── docs/                             # 策略文档（见下表）
├── python/
│   ├── v2/lib.py                     # 数据加载器（v4 回测脚本依赖，保留）
│   └── v4/                           # 分析/回测脚本（见下表）
├── v4.config.yaml                    # 全量配置示例（= 代码默认值, 有漂移守卫测试; 调参请复制成 config.local.yaml）
├── go.mod / go.sum
└── CLAUDE.md                         # 本文件
```

### 文档地图（`docs/`）

| 文档 | 内容 |
|------|------|
| `engine_plan_dog020_2026-09-02.md` | 狗@0.2 完整方案（现行） |
| `dog020_mapping_2026-09-02.md` | 狗@0.2 口径映射/运行说明（现行） |
| `dog020_oos_review_2026-09-15.md` / `dog020_oos_result_2026-09-15.md` | 09-15 双样本复验：预设判据 / 结果 |
| `dog020_risk_latency_plan_2026-09-16.md` | 延迟可见化 + 日亏熔断（决策 #9/#10 依据） |
| `dog020_anchor_recovery_2026-09-16.md` | 锚恢复（决策 #12/#13，多已被 #15 取代，σ 冷启动段仍有效） |
| `dog020_anchor_upgrade_2026-09-18.md` / `dog020_anchor_exact_open_2026-09-19.md` | 锚取值口径的两次迭代（现行 = 「精确那一秒」） |
| `tail_integrated_2026-09-24.md` | **扫尾盘现行权威规格**（三段链 / 空侧 / 全信号结算 / §6 严格大于） |
| `tail_sweep_2026-09-22.md` / `tail_engine_mapping_2026-09-23.md` | 扫尾盘规则 ⑤ 本体 / 口径映射 |
| `tail_stoploss_2026-09-25.md` | 止损评估 + 持仓监察（决策 #24） |
| `tail_sigma_gradient_2026-09-26.md` | 低σ放宽 dev 全部候选已证伪 + 机制定位（§12） |
| `settle_self_2026-09-24.md` | 结算自算三层回退的取证（决策 #19） |
| `book_empty_ask_2026-09-24.md` | 尾盘赢家侧 ask 整侧撤空（决策 #21） |
| `collect_eth_sync_2026-09-25.md` | 采集管线回归 + 多标的参数化（决策 #23） |
| `eth_data_review_2026-09-25.md` | ETH 服务器数据复核 + 阈值重标定待办（决策 #25） |

### 脚本地图（`python/v4/`）

| 脚本 | 内容 |
|------|------|
| `01_backtest_r1.py` | 狗@0.2 回测权威（四条规则对照） |
| `02_paper_compare.py` / `06_oos_review.py` / `07_source_health_check.py` | 纸面对账 / 09-15 复验裁判 / 数据源健康度审计 |
| `23_tail_integrated.py` | **扫尾盘现行 oracle**（parity 测试钉它） |
| `13/14/15/16/17` | 扫尾盘早期回测与分格 |
| `19/20` | 边界价探针（决策 #19 证据） |
| `21/22` | 活体盘口探针 / 空侧审计（决策 #21 证据） |
| `24_asset_data_check.py` | 采集数据正确性校验（**任意标的**） |
| `25/26/27` | 止损评估本体 / 三段链+止损腿存档（**冻结在旧口径**）/ 成交量专题 |
| `28_tail_sigma_gradient.py` | σ 梯度阈值分析 + 机制检验（§12） |

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
                         │  PendingSignals()（待结算行, isSettlable 已过滤）
                         ▼
      settle.Resolver 三层回退（决策 #19; 锚推送缓存是它的输入之一）
        ① push     闭市 +10s   两条边界推送在手 ⇒ 自算定案  ← 实测覆盖 ~96.5%
        ② official 闭市 +45s   官方 open+close（须已收敛）  ← 推送缺一条即走这里
        ③ gamma    约 +75s     ResolutionPoller 兜底
                         │  每行落 settle_src
                         ▼
              Recorder.Resolve(conditionID, outcome, at, src) → P&L 回填
```

### 市场循环流程（flip）

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
   查询, **到 rem ≤ RemMin 撤掉未成交余量并定稿**）→ **定稿后**该行进 pending,
   由 settle.Resolver 按三层回退结算
6. 窗口结束（rem=0）→ 收尾取锚通道（cancel + join）→ |close−anchor| 追加进 σ 滚动窗
   并落盘 windows_*.jsonl（重启 σ 预热本地优先：windows_* 新鲜即毫秒级恢复，
   不足/过旧回退官方网络预热 FetchTwapRanges——停机期窗口只有官方能取）；
   同刻本窗 tick 健康度落盘 winstats_*.jsonl（含被延迟闸挡掉的丢信号明细与
   锚命中/来源/到达延迟）→ 下一窗口
```

### flip 引擎状态机

```
Watching ──首个触底观测(ask≤0.20, 四腿判定)──▶ Done
   ▲                                        │
   └────────── 窗口结束(rem==0) ◀───────────┘
```

- **Watching**: 1s tick 更新两侧状态；锚未就绪期（**每窗开局必经历，正常 ~2s**）anchor≤0，
  取锚通道可能稍后 `UpgradeAnchor` 回填——期间**照常占槽计数、只闸住触发判定**，
  命中即转正常判定，始终未回填（20s 预算耗尽）则整窗不产出观测；有效 tick
  （`latency ≤ 300` 且 UP/DOWN 双侧报价齐全——整簿快照门控）上检查两侧 ask 是否 ≤0.20
- **判定顺序**（一次完成）：rem_low → no_hist → missing_spot → no_crash → dist_out
  （missing_anchor / no_hist 现网不可达——取锚通道与 σ 未就绪整窗跳过在前，
  二者仅 decide 纯函数防线）；全过 → ok（`shares = stake/fill`）
- **Done**: 事件内不再检测（与回测每事件仅首个观测一致，无 fallback 重试）；
  锚未就绪期占槽的 tick 不追溯触发，只记 `lost_triggers(anchor_pending)`
- 数据质量：无效 tick 压 0 占槽（不进触发检查，不贡献急跌窗 max）

### 扫尾盘引擎状态机（`cmd/tail`，独立进程）

```
Watching ──首个「rem≤t150_rem(150) 且 spot 可算」的有效 tick──▶ [判 ⑤]
   ├─ OK   → 落信号行（下单）→ Done
   └─ 拒绝 → 落判定行 → Await60
             （首个可判定 tick 已在 rem≤t60_rem(60) 时: 整段跳过, **不伪造 t150 行**）
Await60  ──首个「rem≤t60_rem(60) 且 spot 可算」的有效 tick──▶ [判 ⑤]
   ├─ OK   → 落信号行（下单）→ Done
   └─ 拒绝 → 落判定行 → Listening
Listening ──此后**每秒**: 有效 tick ∧ ② 达标──▶ 落信号行（下单）→ Done
   └─ rem == 0 → Done
```

- **有效 tick** = `BookLatMs ≤ tail.max_book_lat_ms` ∧ 热门侧**有效价 > 0** ∧
  `spot > 0` ∧ `rem > 0`（有效价 = `ask > 0 ? ask : bid`，四档全空才无有效价——
  决策 #21 修「空侧整簿不得丢弃」，与回测宇宙在 14 天数据上同源）。
  **缺 spot 的 tick 只跳过、不推进任何段**。
- **任一段出信号即整窗只下一单**；两个判定段**成功与否都落一行**，监听段**只在达标时落行**
  ⇒ `tail_*` 每窗 ≤3 行（全部 `kind=snap`，按 `stage` 分 t150/t60/listen）。
  行类型 `kind=frame`/`kind=scan` 为 legacy-only（`isKnownKind` 仍认历史旧行）。
- 锚在产出任何行之前已定局：取锚通道 +20s 结束（`rem≈280`），最早的行在
  `rem≤150`（`+150s`）——`anchor ≤ 0` ⇒ 本窗一行不产出（只在 `tailstats_*` 记
  `anchor_exact=false`）；首行落盘即冻结，`UpgradeAnchor` 此后拒收（三段同锚）。
- **崩溃重启续跑**：`Engine.Resume(t150Done, t60Done)` 按磁盘真相回填已完成段；
  重入判据 = 该窗**是否已有 OK 行**（`HasSignal`，有则整窗跳过，防同窗双单）。
- σ 未就绪（`hist.Count() < 3`）⇒ 整窗跳过 `skip=no_sigma`；`tailwin_*` 是 tail
  自己的 σ 预热源（独立于 `windows_*`，不交叉读写）。
- **结算 = 所有信号**（决策 #22）：`isSettlable` 只排除未定稿的 `submitting`/`resting`，
  被闸/被拒/0 成交行**也回填官方 `won`**（页面照显赢/输），但 `pnl` 恒 0；
  `DailyPnl`/`MaxDrawdown`/`LiveSummary` 按 **`HasPosition()`**
  （= `Kind != KindScan` ∧ `IsFilled()`）过滤 ⇒ 未成交行不动熔断。分类恒等式
  `signals = won + lost + pending + noexec`，胜率分母**只含 won+lost**。
- Dashboard（`runtime.tail_dashboard_addr`）**只读**这条流水线：四个闩锁
  （T150/T60/监听/信号）+ 锚冻结标记、热门侧读数（含 `hot_src`）、锚/σ 就绪、今日健康度
  （读当日 `tailstats_*`）都是现算——不参与判定、不写任何文件。

---

## 依赖库

| 库 | 用途 |
|----|------|
| `github.com/xiangxn/go-polymarket-sdk` v0.7.2 | Polymarket REST/WS 客户端 |
| `github.com/gorilla/websocket` | Binance WebSocket 连接（feed adapter） |
| `github.com/tidwall/gjson` | JSON 解析（SDK 依赖）|
| `github.com/spf13/viper` | 配置文件加载（internal/config，同 master 分支）|
| `golang.org/x/term` | 解密密码无回显终端输入（nohup 场景走环境变量，不走它）|

### 本地开发 replace 指令（默认**不启用**）

`go.mod` 当前**没有** replace：SDK 直接依赖发布版本 `v0.7.2`。只有需要**改 SDK 源码**时
才临时加回（典型场景是 GTD——SDK 的 `PostOrder` 把 expiration 硬编码成 `"0"` 且不在签名
结构里，见决策 #16）：

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

### 测试与常用命令

```bash
go test ./internal/... -count=1        # 引擎/编排/记录器/盘口工具/结算/配置（含 YAML 漂移守卫）
go test ./internal/... -race           # 同上 + 竞态（含扫尾盘 parity 全量重放, ~2min）
go test ./internal/tail/ -run TestParityBacktest -v   # 扫尾盘 Go↔py 逐窗对账（opt-in; data/btc 缺失即 skip）
go build ./...
go run ./cmd/flip -config v4.config.yaml -dashboard :8090     # 运行引擎 + Dashboard
go run ./cmd/collect -config v4.config.yaml -asset eth        # 采集 ETH → data/eth

# python 分析/回测脚本一律用项目内 venv（系统 python3 无 numpy/pandas）
python/venv/bin/python python/v4/01_backtest_r1.py
python/venv/bin/python python/v4/06_oos_review.py                      # 09-15 复验裁判（纯标准库）
python/venv/bin/python python/v4/07_source_health_check.py [--bt-scan] # 数据源健康度审计
python/venv/bin/python python/v4/13_tail_sweep.py                      # 扫尾盘早期回测
python/venv/bin/python python/v4/24_asset_data_check.py --asset eth    # 采集数据校验（任意标的）
```

> ⚠️ `internal/feed` 的 `TestRecoverAnchorSettleGuard` 是**既有**的时序敏感用例
> （10/30/60ms 采样点 vs 50ms 收敛点），整套并行跑有约 1/4 概率误判，与业务逻辑无关。

**验收红线（改 flip/tail 判定链必跑）**：

- `cmd/btreplay` 全量重放 `data/btc` **625 笔逐位一致**（n=625 WR 24.6% EV +0.633U +395.8U）
  ——Go 引擎与 `01_backtest_r1.py` 的口径对账，只喂 `BeginWindow(ev.TwapOpen, σ)`、
  不碰 `internal/feed`。
- `TestParityBacktest` 五格 + 三闩锁计数逐位一致：现行 pin =
  **6412 / 2133 / 3640 / 3 / 96.061885% / +35.092748U**（t150 3638/1208、
  t60 2426/577、listen 348/348），oracle = `python/v4/23_tail_integrated.py`。
  ⚠️ 两条对账口径：宇宙须过滤「快照 tick 上 spot+twap 同时在场」；σ 须**按事件索引**现算
  （python 语义）后注入，**不能喂 `flip.HistState`**（后者只记已 push 的振幅，缺窗时条数不同）。

---

## 关键设计决策

> 编号**沿用历史、不重排**（代码与文档按号引用，如 `决策 #16`）。被取代的条目已删或压成
> 一行 tombstone；完整历史见 git 与 `docs/`。

1. **纸面/实盘同源**：成交执行抽象为 `Executor` 接口，`mode: paper|live` 配置区分。
   纸面 = `PaperExecutor`（校验 fill>0，成交即时定稿）；live = `LiveExecutor`（GTC 限价挂单）
   + `trading.FillTracker`（rem≤RemMin 撤单 + 定稿，见 #16）。两者共用同一 `ExecState`
   编排与风控闸，main 单点经接口调用，不散落两条路径。
2. **口径与回测 1:1**：触发/急跌窗/浅洞（侧别带 yes −0.6 / no −1.0）/时间腿/σ/P&L
   全部映射回测脚本（详见 `docs/dog020_mapping_2026-09-02.md`），观测 JSONL 字段对齐
   回测 CSV——对照基准 `trades_r1_combo.csv`，用 `python/v4/02_paper_compare.py` 映射复核。
3. **首触不重试**：事件内首个触底 tick 即观测，判定失败即 Done（本窗不再检）——
   与回测刻意一致，非引擎缺陷。
4. **观测全落盘**：失败观测同样落盘（`reject_reason` 分解），供信号频率校准与原因分布对比。
   触发即落盘 + 结算仅 ok 且可结算的行进 pending。
6. **即时落盘 + 重启恢复**：观测在触发 tick 立即落盘（行级 flush）；结算回填 temp+rename
   原子重写当日文件；重启扫描 JSONL 恢复内存态，未结算行重建 pending 后由
   `settle.Resolver` 的 Pending 扫描自动接回（重启不是特殊路径）。
7. **单 WS 订阅复用**：MarketMonitor 重启恢复模式沿用；TwapAdapter 内建新鲜度看门狗
   （推送停更超 2min 自动重建订阅）；BinanceAdapter 首拨失败由 main 侧指数退避重试、
   断线后 `runReadLoop` 自愈。
8. **σ 启动预热本地优先**：每完成窗口落盘一行 `windows_YYYY-MM-DD.jsonl`
   （`|close−anchor|` + anchor/close 流值，**独立于 touches**——结算重写只动 touches 当日文件）；
   重启时取最近 ≤`histWindows` 窗并**截到最新一段连续块**（相邻结束缺口 >2 窗判断档，
   防停机前旧 regime 混入），连续块 ≥`histMin` 窗且最新窗距现在 ≤`localFreshMax`(15min)
   才本地 seed，否则回退官方 `FetchTwapRanges` 网络预热（停机期窗口本地没有）。
   截断决策为纯函数 `RecentBlock`（`internal/flip/sigma.go`，table 测试）。
9. **数据源延迟闸配置化 + 丢信号可见化**：三源新鲜度阈值由 `flip.max_book_lat_ms` 与
   `feed.max_spot_age_ms`/`feed.max_twap_age_ms` 驱动，**默认值 = 现行值 = 数据支持值**
   （配置化的意义是「能调」而非「该调」——book 收紧在 14 天回测里单调变差）。
   判定分支不动，只把盲区点亮：无效 tick 上「本会触发」的 tick 落
   `winstats_YYYY-MM-DD.jsonl`（**严格每窗一行**，含 `ticks/ticks_valid/book_stale/
   book_missing/lost_triggers` 明细），触底观测补 `spot_age_ms`。
   ⚠️ `winstats_*` 必须**独立于** `windows_*`：后者是 σ 预热数据源，混入统计行会以
   Amp=0 污染其后 18 窗（文件前缀 + `kind` 字段双保险）。
10. **日亏熔断：两模式同闸 + 当日锁存**：`CanTrade(todayPnl, 线)` 纯函数
    （`todayPnl > 线` 才可交易），闸在 `HandleObservation` 里两模式共用同一判据——
    live 命中拦下真实 POST（rejected 行），paper 命中只写 `gate_reason` 且行照记照结算
    （方案 A：被闸行即「不熔断会怎样」的反事实）。**必须锁存**：判据叠加「当日已有被闸行」
    （`Recorder.GatedOn`，磁盘真相 ⇒ 重启自动恢复、UTC 跨日自动归零）。
    分析脚本（02/06/07）默认过滤 `gate_reason` 非空行，`--include-gated` 恢复旧口径。
11. **配置分层：CLI flag > 配置文件 > 代码默认值（无 env 层）**：配置包 `internal/config`
    （viper），`main()` 只留 4 个引擎 flag（+ 1 个工具 flag `-encrypt`）。
    默认值**唯一来源**是 `defaults()`，`Load()` 把它预置成 `UnmarshalExact` 的目标
    ——「文件缺哪个键，哪个键就是默认值」，文件可只写要改的项。
    - **不带环境变量层**：viper 的 `AutomaticEnv()` 单独用是**假生效**
      （`Unmarshal → AllKeys()` 不含 env 探测到的键）。**以后别再加回来**。
    - **`UnmarshalExact`（拼错的键 = 启动失败）是有意的**：策略参数静默回落比崩溃危险。
    - `v4.config.yaml` = 默认值镜像（入 git），有漂移守卫测试；调参复制成
      `config.local.yaml`（gitignored）。⚠️ `sdk.http_timeout: 10s` **必须带单位**。
    - SDK 默认值以 `sdk.DefaultConfig()` 为**基底**，只覆盖两处：`OwnerKey` 清空
      （SDK 的占位私钥会被「非空即密文」判成密文）、`RateLimit*` 复位 0
      （否则会盖掉 SDK 内建兜底）。
12. ⚠️ **已被 #15 取代**（取值口径）。仍有效的一条：数据源不可信 ⇒ **本窗不观测**
    （与 #13 同原则）。详见 `docs/dog020_anchor_recovery_2026-09-16.md`。
13. **σ 未就绪整窗跳过（`skip=no_sigma`）**：冷启动本地预热不可用会回退**异步**网络预热
    （实测 >70s）——边界落在它完成之前时 `hist.Bps` 恒 0 且**无热更新路径**，任何触底都被
    `no_hist` 拒（必然零信号）却仍落一条 `hist_bps=0` 观测。故 `hist.Count() < flip.HistMin`
    即**整窗跳过**：不预取/不订阅、落 `skip=no_sigma` 的 `winstats_*` 行。
    **不取「预热完成后热更新 σ」**：会把窗口级常量 σ 变成时变量，同一事件的 `dist_s`
    取决于 σ 何时落地（不可复现），回测也无对应形态；收益仅 ≈0.15 笔/冷启动，不值。
    副作用：本窗无 close 采样 ⇒ `windows_*` 缺一行，等价于停机窗（`RecentBlock` 缺口容差吸收）。
14. ⚠️ **已被 #15 取代**（取值口径）。其结论仍是 #15 的依据：**官方 `openPrice` 头几十秒
    是未收敛的临时值**（p90 1.18bps ≈ 9.5 美元），而**对齐推送 == 收敛官方**（逐位相同）。
    详见 `docs/dog020_anchor_upgrade_2026-09-18.md`。
15. **锚 = 边界那一秒的 TWAP 推送（精确取锚）**：官方 `openPrice` **就是**「评估时刻 == 边界」
    那条推送（逐位相同），故**只留精确那一条**，不设过渡锚、不做官方 HTTP 覆盖。
    - **精确匹配**：`PushNearest(windowStart)` 只认 `tsMs == windowStart.UnixMilli()`，删容差/
      删「取最近」；返回 `(price, arrivedMs, ok)`（`arrivedMs` = 该条推送的**本地到达时刻**
      = 发布延迟的无偏观测）；重复时间戳取**后到者**。`CacheStat()` 供失败日志区分
      **服务器没发 / 我们收晚了 / 时间戳缺失**。
    - **取锚通道**（`feed.RecoverAnchor`，每窗都起、**主循环零等待**）：① 精确段——立即试
      一次后每 **500ms** 一次、最多 **40 次 = 20s**（该推送到达 p50 **+2.0s**），**命中即终局**
      并关通道；② 官方段——代码保留但**生产休眠**（`Schedule` 为空即零请求，
      `TestRecoverAnchorOfficialDormant` 钉住），将来允许 40s+ 延迟时接回即可。
    - **窗口开局 `BeginWindow(0, 0)`**：锚未到手期间 tick 照常占槽与计数、
      `lost_triggers(anchor_pending)` 留痕、**只闸住触发判定**（不加新闸）；
      **20s 未命中 = 本窗不产出观测、不计入 σ**（宁可丢窗也不拿近似锚判定）。
      625 笔组合信号最早 `rem=295`（边界后 +5s）⇒ 正常路径（+2s 到手）**历史样本零损失**。
    - **可见性**：`winstats_*` 用 **`anchor_exact`**（不带 `omitempty`，false = 本窗无锚，
      也是 python 侧区分口径版本的哨兵键）+ `anchor_src` + `anchor_recovered_ms`
      （= 该推送本地到达时刻距边界）。`stats.AnchorMissing` 每窗开头都会短暂为真，
      **期末**仍为真才是「整窗无锚」。
    - **不是 P&L 杠杆**：换锚后信号掉出 11 / 新增 9（≈ 中性）；收益是口径正确 + 锚缺失窗口
      不再白丢 + 「锚偏了多少」逐窗落盘。**σ 的 close 口径未改**（仍 `lastTick.TwapPrice`
      到达口径，与 open 的评估口径有 ~1.5s 不对称，已知未做）。
16. **live 下单 = GTC 限价挂单 + `rem ≤ RemMin` 撤单 + 撤单时定稿**（保住盈亏比）。
    FAK 已删除，`orders.GTC` 是唯一提交路径。动机是**脆弱性**：tick 采样 → POST 到达有
    1~3s 延迟，触发那一瞬挂在 0.20 的卖单常已被吃走；GTC 挂着等，谁在窗口内砸出来就接住谁。
    - **挂单只挂到 `rem ≤ flip.rem_min`(180)**（策略时间腿，不是硬编码常量）：到点自己发
      `DELETE /order` 撤掉未成交余量。回测前提是「触发瞬间必成交」，rem ≤ 180 之后才成交的
      样本不是这条策略要的（砸到 0.2 后一路拖到尾盘才被吃掉的那批，逆向选择最重）。
      撤单**尽力而为**：失败每 2s 重试到成功或硬截止；撤单点之前不撤、已满额成交不撤、
      **查询失败时照撤**。
    - **POST 不再是终态**：GTC 响应只描述 POST 那一瞬。新状态 **`ExecStatusResting`** =
      订单在簿、成交量待定稿；`filled` 只留给「即时全额成交」。**`unfilled` 只有定稿
      （撤单后读到 `size_matched`）才知道**。
    - **`trading.FillTracker`**（长驻 goroutine）：每 **2s** `GetOpenOrders(Id)` 读
      `size_matched` + 到点撤单 → 定稿回调 `ExecState.ApplyFillFinal` →
      `Recorder.CompleteRestingFill`。终态五条：① `status ∈ {MATCHED, CANCELED}`；
      ② **挂单表已无此单**——仅在**本进程至少见过一次**（`sighted`）或**重启接管**
      （`adopted`）时才可推定；③ `size_matched ≥ 请求股数`；④ 撤单成功却仍在簿 →
      **撤单确认宽限 15s** 用尽即按末次观测定稿（note 标注）；⑤ 硬截止 = 闭市 + **60s**
      宽限。任一未定 ⇒ 保持 `resting`。
    - **绝不臆造仓位**：无法确认 ⇒ 行留 `resting` + `ExecNoteUnknown`，只更新 note 让
      `NeedsReconcile` 捞出来人工核对。**成本口径 `cost = shares × 限价`**（挂单成交必是
      maker 成交 = 限价本身，即时 taker 那部分只会更便宜 ⇒ 至多略微高估，保守且与回测同口径）。
    - **已知样本偏差**（不是免费午餐）：挂单越久，成交样本越偏向「价格继续下探」的那批；
      撤在 rem ≤ 180 只是把偏差**截短**，不消除它——真答案仍要等实盘样本。
    - **GTD 不可用**（试过，放弃）：CLOB 的 GTD 规则是「stated expiration 前 60s 就被安全阈值
      撤」，最小有效挂单期 ~2 分钟，对 5 分钟窗口几乎等于挂到闭市；更硬的一层是 SDK
      `PostOrder` 把 expiration 硬编码为 `"0"` 且不在 EIP-712 签名结构里（v0.7.2 复核过，
      `UserOrder.Expiration` 只流进未签名的结构体字段）。故撤单由我们自己发。
      **下单路径仍是单次网调**：`CreateOrder` 只查 tickSize/negRisk，两者由
      `PrefetchTokenInfo` 每窗预热；撤单是独立的 `DELETE /order`（一分钟一笔量级）。
17. **扫尾盘 = 独立进程 + 独立引擎包 + 复用原语**：`internal/tail` **单向依赖 `internal/flip`**
    （取 `Tick`/`Executor`/`PaperExecutor`/`HistState`/`WindowEntry`/`CanTrade`/`SideYes|No`/
    `WonFor`/`ExecStatus*`），反向不依赖——两族独立演进，但共用同一批经对账的原语。
    - **前缀独立**：`tail_*` / `tailwin_*`（σ 预热源）/ `tailstats_*`（严格每窗 1 行）/
      `tailhold_*`——**不得**复用 `windows_*`（双进程双写会毁 σ）。flip 与 tail 可同目录并行。
    - **撤单点 = 闭市 `rem ≤ 0`**：`trading.CancelAtClose` 哨兵 = `time.Nanosecond`，
      ⚠️ **必须微小正数**——`NewFillTracker` 对 `cancelLead ≤ 0` 一律回退 180s，而 tail 在
      `rem≈60` 才挂单，回退会让第一轮轮询就撤单（表现为「策略零成交」）。
    - **参数全部不可调**（见项目概述）；`internal/config` 只做**结构性**校验
      （阈值 > 0、价格腿 ∈ (0,1]、`t150_rem > t60_rem > 0`）。
18. **tail Dashboard = 同包并列 + 独立 listener**：`internal/dashboard` 是**双策略并列的单包**
    ——`common.go`（分页信封/查询参数/JSON/日志/单页入口/逐日聚合骨架）+ 每族三件
    （`{flip,tail}_server.go` / `_handlers.go` / `_state.go`）+ 每族自己的静态目录
    `flip/` `tail/`。**不拆子包**：两族 handler 方法同名，靠 receiver 类型区分。
    - **各自一个 listener、各自 embed 自己的静态目录**（URL 前缀都还是 `/static/`，FS 根不同）；
      两族静态资源**有意复制**（不共享 CSS/JS）。
    - **启动方式 = 键 `runtime.tail_dashboard_addr` + `cmd/tail` 的 `-dashboard` flag**
      （`flag.Visit` 语义同 flip：显式空串 = 关掉配置文件里的地址）。**不复用**
      `runtime.dashboard_addr`：两族独立进程、各自监听、同机并行须给不同端口。
    - **零行为改动**：dashboard 侧复算现窗口读数用的纯函数（`SideOfHot` / `DevUSD` /
      `SigmaUSD`）就是引擎自己调的那三个（抽出来单一实现）。
19. **结算自算：推送优先的三层回退（flip / tail 共用）**：探针实测官方 `open(N)` ≡ 边界那一秒
    的推送 `anchor(N)`（315/315 逐位相等）、官方 `close(N)` ≡ `anchor(N+1)`（309/309）、
    `close(N)` ≡ `open(N+1)`（228/228）——**对齐是定义级相同而非近似**。
    - **三层与时点**：闭市 **+10s** 推送自算（close 那条推送闭市那一刻才发布，实盘 880 个
      命中窗里到达延迟只有 1s/2s 两档，10s = 5 倍上界）→ 闭市 **+45s** 官方接口
      （**必须等收敛**，头几十秒是临时值；每 5s 一次 × 至多 6 次）→ **+75s** 交回 gamma 轮询
      （UMA 是市场结算本身，最后一层）。
    - **为什么值**：判定与官方**逐位同源**，误差归零——原先 close 是**到达口径**流值，
      实测 315 窗里 3 窗（0.95%）方向被贴线漂移带反；顺带日亏熔断从「等 UMA（分钟级）」
      提前到**闭市 +10s**。缺失面：854 个实盘窗里 `anchor_exact=false` 仅 15 窗（1.76%），
      一次结算要两条推送 ⇒ 约 **3.5%** 走官方层。
    - **实现**：`internal/settle`（**零外部依赖、不 import `internal/flip`**——判定词表用常量
      相等断言钉住），`internal/feed/official_pair.go` 提供 SDK 侧 `PricePairFetcher`。
      `Record.SettleSrc`（`push|official|gamma`）逐行落盘供事后分桶对账。
    - **触发点收敛**：只有 `GiveUp` 一处驱动；`Pending()` 直接扫 `Recorder.PendingSignals()`
      （`isSettlable` 已过滤），重启恢复免费复用——两个驱动抢同一行会有一边报「回填未命中」。
20. **无仓位行独立成类：`signal = won + lost + pending + noexec`**（flip Dashboard）。
    起因：被风控闸拦截、从未下单的行在页面上显示「待结算」**永远不会变**（无仓位 ⇒ 永不被
    结算），而旧的 `lost = sigCount − won − pending` 反算又把它计进「负」——一笔从未下过的单
    同时虚增亏损笔数、压低胜率。
    - **分类**：`FlipState.tally` 逐行只落一类——`won` / `lost` / `pending`（**在
      `Recorder.PendingSignals()` 里**，即 `isSettlable` 过滤后的真实持仓行，判据单一来源）/
      `noexec`（其余）。`/api/state` 增 `noexec_count`，`/api/daily` 的「待结算」列拆出
      「未成交」列。胜率分母**只**含赢+输。
    - **可见性**：响应补 `gate_reason` / `exec_status` / `exec_note`，前端结果列三态合一
      （已结算 → 赢/输；被闸 → 闸·首窗/熔断；执行状态 → 挂单中/未成交/下单被拒；否则待结算）。
      份额与 P&L 对无仓位行显示「—」（不拿目标股数冒充成交）。
    - ⚠️ **口径红线**：`pending` ≠ `won == nil`（后者含永不结算的行）；分析脚本筛「未结算」
      样本时必须区分「在途」与「无仓位」。
21. **空侧整簿不得丢弃：赢家侧在尾盘整侧没有 ask**（见 `docs/book_empty_ask_2026-09-24.md`）。
    - **事实**（SDK WS 与公共 REST 两路独立取证）：事件趋于确定后**赢家侧的卖单被整侧撤空**
      （档数慢慢降、最后整侧撤空），对手侧（输家）始终有 ask。**赢家侧空 asks ≡ 输家侧空
      bids**（镜像同一事实）⇒ 两个 token 各缺一侧。**被清空的永远是对应「押最终输家」的那条腿**。
    - **SDK 无错**：`book` 事件是**整簿快照**，asks 空数组 = 真的空。**0.99 是引擎自己造出来的**
      ——旧代码 `if book == nil || len(book.Bids) == 0 || len(book.Asks) == 0 { continue }`
      把这类消息整个丢掉 ⇒ 内存里留着**撤单前那一份旧簿** ⇒ 页面与判定都用假卖价。
    - **修法**：守卫只留 `book == nil`。空侧照存 ⇒ `feed.bestAsk/bestBid` 返回 0 ⇒ 两族引擎的
      四档门控判该 tick **无效**（与回测宇宙同口径：四档缺一即丢）⇒ 不再产出基于假卖价的信号。
      测试钉在 `TestNewPMTickEmptySide`。
    - ⚠️ **纸面数据的 0.99 分两种窗**：空侧发生在 `rem≤60` **之后** ⇒ 快照那一刻卖单还在，
      是**真读数**；发生在 `rem≤60` **之前** ⇒ 快照读的是**冻结簿**，是**买不到的纸面价**
      （修后这类窗一行快照都不产）。**代价**：修后快照样本变少——这是与回测宇宙对齐，不是退化。
    - ⚠️ **两个未做的（须各自单独立项）**：① **读龄未纳入延迟闸**（`BookLatMs` 是接收时刻的
      传输延迟，不含此后累积的簿龄；改判定 = 改口径 ⇒ 必须走回测）；② `minimum_order_size = 5 股`
      而 tail 的 `stake=2U` 低于下限（见项目概述的上 live 阻塞项）。
22. **扫尾盘整合：三段判定链 + 空侧 ask 优先 + 全信号结算**（规格
    `docs/tail_integrated_2026-09-24.md`；⑤ 本体定义不变）。
    - **① 三段判定链**：见「扫尾盘引擎状态机」。原「rem≤150 原始帧（只记录）」不再只记录
      ——**它本身就是下单点**。迟到接入（首个可判定 tick 已在 rem ≤ 60）跳过 T150 段且
      **不伪造 t150 行**。`ProcessTick` 返回**至多一行**。
    - **② 空侧不算无效**：纯函数 `HotBook(upBid, upAsk, downBid, downAsk) (side, px, src)`
      ——每侧**有效价** = `ask > 0 ? ask : bid`，热门侧 = 有效价高的一侧（平局取 yes），
      四档全空才无有效价。价格腿 / 股数 / live 限价**全用有效价**；落盘增
      `hot_src ∈ {ask, bid}` 审计字段。14 天 542800 tick 里「四档部分缺」**0 次** ⇒ 这是
      **纯 live 改动**（BTC 历史数据的缺腿 0 次是旧守卫丢消息的构造性产物），parity 用
      `hot_src 恒 ask` 钉住。
    - **③ 所有信号都注册结算**：见「扫尾盘引擎状态机」。⚠️ 判据必须是
      **`HasPosition()` = `Kind != KindScan ∧ IsFilled()`** 而非 `IsFilled()`（legacy 的 scan
      对账行恒无仓位但 `ExecStatus` 为空，会被 `IsFilled()` 误判成 paper 成交而混进胜率）。
      **tail 的方案 A 作废**（被风控拦 = 未成交）：paper 与 live 统一成一个 `rejected` 形态，
      paper 被闸行不再算持仓、不进胜率/P&L（**flip 侧不受影响，仍是方案 A**）。
    - **④ Dashboard 大改**：删判决卡/五格对照/监听对账/原始帧表，新增「未成交」卡与**信号**表；
      路由删 `/api/judge`、`/api/scans`、`/api/frames`，新增 `/api/signals`。
    - **行类型不新增 kind**：三段行一律 `kind = snap` + 新字段 `stage ∈ {t150,t60,listen}`；
      `KindFrame`/`KindScan` 降为 **legacy-only**（`isKnownKind` 仍认、引擎不再产出）。拒绝原因链：
      `missing_spot → no_hist → price_low → leg_out`。**监听段的拒绝 tick 不落行**（每窗 ~60 个
      tick，全落会淹没信号表）；两个判定段成功与否都落盘。
    - **防双单 + 崩溃续跑**：重入同窗时 `HasSignal(conditionID)` 有 OK 行即整窗跳过（只有拒绝行
      则续跑）；`Engine.Resume(t150Done, t60Done)` 按磁盘真相回填段闩锁与状态，**`emitted` 保持
      false**（否则 `UpgradeAnchor` 被自己的冻结判据挡死、锚永远进不来）。
    - **已知取舍（写进文档，不修）**：① ask 空按 bid 成交时 paper 按「限价即成交」记账（偏乐观），
      live 是挂单等成交、大概率不成交 ⇒ `hot_src=bid` 的样本在实盘须单独看；② 两族合并熔断未实现。
23. **采集管线 + 多标的参数化（`internal/feed.Asset`）**（`docs/collect_eth_sync_2026-09-25.md`）：
    `cmd/collect` + `cmd/compact` + `internal/collect` 组成数据格式 v2 采集管线（**任意标的**）。
    - **三处口径与引擎对齐**：① 边界价改**精确推送**（`PushNearest` 精确等值匹配，取代
      「`Latest()` 流采样 + 官方 30s 轮询」；正常窗**一次官方请求都不打**，只在推送缺失时排队修正）；
      ② **空侧盘口不丢消息**（守卫只留 `book == nil`，见 #21）；③ 删 `MinRange`/`MaxStreamAgeMs`
      （为「流采样 close vs 官方边界值」的量化误差标定的，close 与官方同一个数后该误差归零）。
    - **硬编码消除**：`internal/feed/asset.go`（`AssetFor` / `AssetFromSlug` / `DataDir` /
      `ApplyBinance`）一处资产名派生四条命名（slug / Binance 交易对 / Chainlink 符号 / 数据目录）。
      `cmd/collect` 四个 flag（优先级 `-slug` > `-asset` > `runtime.slug_prefix`）；
      `binance.symbol` 默认 `""` = 留空即派生（显式填优先，应对 `1000XXXUSDT` 这类标的）。
    - ⚠️ **口径陷阱**：两个包的 `"stream"` **同名不同义**——`feed.AnchorSourceStream` = **精确边界
      推送**，`collect.SourceStream` = **到达口径采样**；采集侧落盘前必须经
      `cmd/collect/main.go:anchorSource()` 显式翻译（否则取锚成功的窗被标成到达口径、每窗白排队
      一次官方修正）。
    - **数据校验 `python/v4/24_asset_data_check.py`**（纯标准库，任意标的）：八段拿外部真值或
      内部恒等式对（结构/tick 覆盖/盘口互补/Binance K 线/官方边界价 vs 行内值/gamma 方向…）。
      ⚠️ 网络坑已在脚本内处理：gamma 与 `polymarket.com` 要浏览器 UA（否则 403）；
      `crypto-price` 会 429（逐窗 0.35s 间隔 + 退避重试）。**决定性一段**：口径为 push 的行，
      边界价必须与官方**逐位相等**（BTC 旧口径行收盘价 0/5、最大差 1.38 美元 ⇒ 正是被换掉的那个口径；
      ETH 新口径 1/1 逐位相等）。
    - ⚠️ **互补恒等式越界**：一行的 `yes_*` 与 `no_*` 来自两条独立 `book` 消息、`book_ts` 只取较大者，
      行内分辨不出瞬态错位。实测 BTC 3753 窗合计 0.126%、逐窗 p99 1.0% ⇒ 阈值定为两级
      （合计 > 1% / 单窗 > 5% 判失败）。下游同时读两侧的判定须知此瞬态存在。
24. **持仓监察（只记录）+ 止损评估结论**（`docs/tail_stoploss_2026-09-25.md`，
    脚本 `python/v4/{25,26,27}`）：
    - **止损腿未落引擎**：离线支持加止损（触发 = 持仓侧 `bid < 0.30 ∧ dev < −20 美元`，
      出场按持仓侧 bid）——14 天基线 **+35.67U → +46.11U**（Δ **+10.44U**，
      **配对** bootstrap 95% CI `[+2.24, +19.59]`；杀赢 5 / 救输 63 = 12.6:1 vs 临界 6.2:1）。
      ⚠️ 显著性判据必须是**配对 Δ 的区间**，不是止损后 P&L 的绝对区间。
      阈值建议 `bid < 0.25` 而非 0.30（Δ 更高且杀赢更少——宁可不杀）。
      （上述数字是 09-25 冻结基线，`python/v4/26` 存档；要重跑须先把它的 r5 改成
      `strict_price=True` 并同步 oracle。）
    - **未落地原因 = 对手方**：所有离线 Δ 都假设触发那一秒有 bid 可吃，但 #21 的活体取证证明
      尾盘「押最终输家」那条腿**被整侧撤空**，而**止损要出场的那一刻正是持仓从赢家变成输家的
      那一刻**（ETH 服务器数据实测该时刻亏损侧 bid=0 占 **80.5%**）⇒ **全部 Δ 都只是上界**。
    - **落地的只有持仓监察**：`internal/tail/hold.go` + 第四前缀 `tailhold_YYYY-MM-DD.jsonl`
      + `cmd/tail` 旁路（成交后逐 tick 记持仓侧盘口，**只记录不判定**）。**门控与判定路径刻意
      不同**（这是它存在的全部理由）：不要求持仓侧 bid > 0（bid == 0 正是要观测的东西）、
      不要求四档齐全、不要求 rem ≤ 150。行内 `stop_cand` 是纯派生标记。**硬边界三条**：
      只记录（不进 P&L / 熔断 / 胜率 / 任何判定）；不碰引擎状态（独立前缀 + `kind` 双保险）；
      落盘失败只记日志。所有浮点字段**不带 `omitempty`**——`hold_bid: 0` 必须原样落盘
      （`TestLogHoldTickKeepsZeroBid` 钉住；**为什么记深度**：`minimum_order_size = 5 股`，
      「bid 存在但只有 3 股」与「没有 bid」对实盘是一回事）。
      **读法**：跑一两周后看 `stop_cand=true` 那些时刻里 `hold_bid == 0` 的占比与 `hold_bid5`
      分布——占比低 + 深度够 ⇒ 可认真考虑上止损；占比高 ⇒ Δ 要打折甚至归零，**那时不上止损**。
    - ❌ **成交量维度已否**：「砸盘那一刻量比」方向对、强度不够，且判别力恰好在止损那一档衰减到
      噪声（跌破 0.60 处 AUC 0.574 → 0.30 处 0.482）；整窗成交量对入场无预测力
      （ρ = −0.023）且与 σ 腿半冗余（ρ = +0.579）。
25. **ETH：撤空复现 + 扫尾盘门槛不可跨标**（`docs/eth_data_review_2026-09-25.md`）：
    - ✅ **尾盘整侧撤空在**服务器**数据上复现**（23 窗里 30 段 / 21.5% 的 tick，镜像 100%，
      30 段里 29 段起点热门侧已 ≥0.96 ⇒ 与市场定局精确耦合，**本机网络假设排除**）。
      ⚠️ 证的仍是「现象」不是「比率」（持有空快照的时间可能长于市场实际）。
    - **对两族的影响**：狗@0.2 **不受影响**（rem>180 的四档齐全率 99.08%，缺腿全落在不交易的
      尾盘）；扫尾盘受**致命**影响（热门侧 ≥0.80 的 tick 里没有卖单的占 `rem≤150` 50.4% /
      `rem≤60` **82.6%** / `rem≤30` 90.8%）⇒ 纸面「有效价即成交」在 ETH 上等于假成交。
      正面数：**有卖单时**深度够（`rem≤60` 前 5 档中位 230 股，<5 股的仅 0.4%）
      ⇒ 瓶颈是「有没有人卖」不是「量够不够」。
    - 🔴 **待办**：扫尾盘阈值重标定（见项目概述末条）。⚠️ **ETH 版回测有一条有意不同**的口径：
      交割**只用「热门侧有卖单」的子样本**（`hot_src == ask`）——ETH 数据做得到、BTC 历史做不到。
    - ⚠️ **窗首瞬态**：每窗前 1~4 个 tick 四档全零且 `book_ts=0`（订阅首份快照未到，不是撤空），
      落在策略判定区之外。
    - **狗@0.2 上 ETH 是另一件事**：浅洞腿是 σ 归一的、结构可迁移，但 ETH 的**相对**波动是
      BTC 的 2.5 倍 ⇒ 浅洞带要在 ETH 数据上重跑分桶，另行立项。
26. **T=150 段价格腿改严格大于（`> 0.80`）：三段之间唯一的规则差异**。**只改 T=150 段**——
    T=60 的 ⑤ 与监听段的 ② 一字未动（仍 `≥ 0.80`），其余腿全部不变。
    - **依据 = 0.80 这一格在两个样本里都是唯一负 EV 档**：实盘（09-24~25）恰为 0.80 的 4 笔
      WR 50% / −14.38U，而同批 `> 0.80` 的 201 笔 WR 98.51% / +70.15U；14 天回测同分桶 n=17
      WR 76.47% −1.50U（整条价格梯度上唯一的负档）。⚠️ 两个样本都薄（4 / 17 笔），一致的只是
      「它是最差档」这个**排序**。
    - **14 天重跑**：t150 1220→**1208**、t60 568→**577**、监听 347→**348** ⇒ 合计 2135→**2133**、
      P&L +35.67→**+35.09U**。机制：t150 那一格被拦下 ≠ 该窗不下单——落一条 `price_low` 判定行后
      按链走到 t60（多数）或监听段**以更高的价重入**。**配对 Δ = −0.58U，95% 区间
      [−6.26, +5.59] 含 0 ⇒ 与基线不可区分**——买的是「去掉唯一负 EV 档」的口径干净，
      **不是** P&L 增益。
    - **实现**：`internal/tail.PriceLeg`（纯函数, `stage == StageT150 ? > : >=`）是本包**唯一**的
      段相关腿；`EvalRules` 增 `stage` 参数（**必须传**——否则行里的 `rules.price` 会与
      `reject_reason` 自相矛盾）。比较符**故意不做成配置键**（做成键会让人按行情调它）。
    - 与「参数不可调」不冲突的理由只有一条：**它是用户对规则本身的决定，不是拿回测网格挑出来的
      参数**（`price_min` 阈值一字未动，改的是算子）。

---

## 运行方式

配置优先级：**CLI flag > 配置文件 > 代码默认值**（三层，**不含环境变量层**——
唯一被读取的环境变量是 `PM_CONFIG_DECRYPT_PASSWORD`）。

```bash
# 纸面运行（默认值 + Dashboard）
go run ./cmd/flip -config v4.config.yaml -dashboard :8090

# 不带 -config = 完全不读文件、纯代码默认值（不开 Dashboard）
go run ./cmd/flip

# 单点覆盖（最高优先级；-dashboard "" 能真的关掉配置文件里的地址）
go run ./cmd/flip -config config.local.yaml -stake 5 -mode live

# 扫尾盘（第二条策略线, 独立进程可并行跑；自带 Dashboard, 无 -encrypt）
go run ./cmd/tail -config v4.config.yaml -dashboard :8091
go run ./cmd/tail -config config.local.yaml -stake 2 -mode paper -dashboard ""
```

### CLI flag（`cmd/flip` main() 只有这 4 个引擎参数 + 1 个工具 flag）

| flag | 默认 | 含义 |
|------|------|------|
| `-config` | `""` | 配置文件路径。**空 = 不读任何文件**，直接用代码默认值 |
| `-dashboard` | `""` | 覆盖 `runtime.dashboard_addr`；显式传空串 = 本次不开 Dashboard |
| `-mode` | `""` | 覆盖 `runtime.mode`（paper\|live）|
| `-stake` | `0` | 覆盖 `flip.stake`；**没给**则用配置值，显式给 0 会校验报错 |
| `-encrypt` | `false` | **凭证加密工具**（唯一「跑完即退」的分支）：读明文 → 密文写 stdout → 退出。不读配置文件、不碰数据源、不跑引擎 |

用 `flag.Visit` 区分「没给」与「显式给空/0」——所以 `-dashboard ""` 是有效的关闭操作，
不是「用默认值」。`cmd/tail` 只认 **4 个 flag**（`-config` / `-mode` / `-stake` / `-dashboard`，
`-dashboard` 覆盖的是 `runtime.tail_dashboard_addr`），没有 `-encrypt`（用 flip 的那个）。

### 主要配置键（`v4.config.yaml` 全量带注释）

| 配置键 | 默认 | 含义 |
|------|------|------|
| `flip.rem_min` | 180 | 策略时间腿：仅 `rem > 180` 的触底才判定（与回测 1:1）。**同时是 live 撤单点**——`cmd/flip` 把它传给 FillTracker |
| `flip.max_book_lat_ms` | 300 | PM 盘口延迟闸：`book_latency_ms` 超此值的 tick 无效（**回测 `MAX_LAT` 同值，收紧是负收益**） |
| `feed.max_spot_age_ms` | 2000 | Binance spot 新鲜度：距本地接收超此值判现货缺失（`missing_spot`） |
| `feed.max_twap_age_ms` | 10000 | TWAP-60 新鲜度：**只**管窗末 close（锚走精确匹配，不吃到达龄），超龄按缺失处理 |
| `risk.max_daily_loss` | **−24** | 日亏熔断线（负值）：当日（UTC）已结算 P&L ≤ 此值即当日停单并锁存；两模式同源 |
| `runtime.dashboard_addr` | `""` | **flip 进程**的 Dashboard 监听地址（空 = 不启动；`:8090` 直接给端口） |
| `runtime.tail_dashboard_addr` | `""` | **tail 进程**的 Dashboard 监听地址（独立键） |
| `runtime.output_dir` | `data/v4` | 观测 JSONL 输出目录（live 建议独立目录；**flip 与 tail 可共用**——前缀各自不撞） |
| `runtime.slug_prefix` | `btc-updown-5m` | 市场 slug 前缀 |
| `tail.*` | — | 扫尾盘 7 键——⚠️ **全部不可调**（见项目概述）|

启动校验（`internal/config/validate.go`，判**最终生效值**）：三阈值必须 > 0、
`risk.max_daily_loss` 必须 < 0、`runtime.mode ∈ {paper, live}`、`flip.stake > 0`、
`runtime.output_dir` 非空 —— 任一不满足即启动失败；`flip.max_book_lat_ms < 100` 只告警。
配置文件里拼错的键（`UnmarshalExact`）也是启动失败，不静默回落默认值。

### 敏感字段（`sdk.polymarket.*`）

凭证**只能来自配置文件**（`POLYMARKET_*` 环境变量已不再读取）：`owner_key` /
`clob_creds.{key,secret,passphrase}` / `relayer_key.{key,key_address}` 留空 = 只读运行
（引擎自动生成临时密钥跑纸面）；填 **密文**（AES-256-CBC）则实盘可用。判定语义是
**非空即密文**——明文写进去会在解密时启动失败（没有「看起来像明文」的兜底）。
**凭证块一律整块加密**：`relayer_key` 的 `key_address` 虽是地址也走同一口径——半加密会让
另一个字段以密文形态被当成地址发出去，且「哪个子字段该明文」是本包刻意不留的规则。
`builder_creds` 目前**不在**加密名单（本引擎未使用；要接 builder 时一并加进
`decrypt.go` 的 `appendCredTargets`）。

**生成密文用 `-encrypt`**（逻辑在 `internal/config/encrypt.go`，与 `decrypt.go` 对称、
**共用同一个密码来源**——密文只能用启动时那个密码解开）：

```bash
go run ./cmd/flip -encrypt                    # 终端: 无回显粘贴明文 → 密文打到 stdout
printf %s "$RELAYER_KEY" | PM_CONFIG_DECRYPT_PASSWORD=… go run ./cmd/flip -encrypt   # 管道整段读取
```

- **明文不进命令行**：没有 `-encrypt <明文>` 这种形态（会进 shell 历史与 `ps`）。
  stdin 是终端 → `term.ReadPassword` 无回显；是管道 → 读**整段**（不是逐行——多行秘密会被
  静默切成多个密文）并 TrimSpace。
- **stdout 只有密文**（可管道进剪贴板/脚本），提示与自检指纹走 stderr——指纹 = 长度 + 首尾
  各 4 字符，短于 24 字符只报长度（贴错东西时密文照样合法，启动不会报错，这是唯一的人工自检点）。
- 加密方案由 SDK 决定，**不自行改动**（换 IV/加盐 = 既有密文全解不开）：代价是确定性
  ——固定 IV（= SHA256(密码) 前 16 字节）意味着同密码 + 同明文 → 同密文。
- 与引擎无关：该分支在 `config.Load` 之前 return，不读配置、不校验、不落盘。

| 变量 | 说明 | 必填 |
|------|------|------|
| `PM_CONFIG_DECRYPT_PASSWORD` | 密文凭证的解密密码；不设则终端无回显输入（**nohup/systemd 无终端 → 必须设它**） | 仅当配置文件里有密文 |

> 本地运行记得 `export https_proxy=http://127.0.0.1:1087`（Polymarket 直连超时；这是
> Go 标准库 `http.ProxyFromEnvironment` 读的，与上面的配置系统无关）；
> 部署机勿设指向不通代理的 HTTP(S)_PROXY（Binance 拨号走环境代理）。
