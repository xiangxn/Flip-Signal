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
  **T=150 判 ⑤ ∧ 入场闸** → 不达标则 **T=60 再判 ⑤** → 仍不达标则**此后每秒判 ②**
  （② = 价格腿 ∧ `dev ≥ 63`，不含 σ 腿）。
  ⑤ = 热门侧**有效价**过 0.80（⚠️ **T=150 段严格大于**，T=60 与监听段 ≥，决策 #26）
  ∧（`dev ≥ 63 美元` ∨（`sd ≥ 40 美元` ∧ `dev ≥ sd`））；
  `dev = sgn·(spot − anchor)`（美元，正 = 朝押注方向，sgn: 押 yes +1 / no −1）；
  `sd = hist_bps × anchor / 1e4`（该窗 1σ 折美元）。
  ⚠️ **入场闸**（决策 #29，**只有 T=150 段**）：还要 `walk = sgn·(twap − anchor) ≥ 43 美元`
  ——`walk` 是**已写进结算线**的那部分位移（`dev = basis + walk`，basis 是还没写进去的缺口）；
  不达标 ⇒ 本段落 `reject_reason = walk_low`、**链继续**（多数改道到 T=60/监听，重入价中位
  0.990）。twap 缺失 ⇒ **放行**（fail-open）。阈值 = 配置键 **`tail.walk_min_usd`（默认 43）**
  ——键存在的理由是**跨标的各带一份自己的标定值**（43 是 BTC 价格的标定量，决策 #25），
  **在 BTC 上仍不可调**（43 是那批数据的 argmax）。
  ⚠️ **价格地板**（决策 #32 + #33，**T=60 段与监听段**）：这两段除各自规则外还要有效价
  **严格大于 `tail.floor_min_price`（默认 0.83）**；不达标 ⇒ 落一行 `reject_reason =
  floor_low` 的**影子行**（`ok=false`，**两段共用闩锁 ⇒ 每窗至多一条** = 无地板世界里
  本该成交的那一笔）且**链继续**（T=60 被拦 ⇒ 走到监听段；监听段被拦 ⇒ 同段等更贵的 tick）。
  T=150 段一字不动。
- **回测基准（14 天，2U/注，oracle = `python/v4/23_tail_integrated.py`）**：
  T=150 n=958 WR 96.24% +42.17U / T=60 n=741 WR 99.19% +15.08U / 监听 n=375
  WR 99.20% +13.06U ⇒ **合计 n=2074 WR 97.83% +70.31U**（全部行 6704，参与判定 3640 窗，
  `walk_low` 250 行 + `floor_low` 24 行）。监听段地板时（决策 #32 之后）为 T=150
  958/96.24%/+42.17U、T=60 750/99.20%/+19.07U、监听 368/99.18%/+11.34U ⇒ 合计
  **n=2076 / 97.83% / +72.58U**（行 6697）；闸前（决策 #29 之后）为 T=150 958/96.24%/+42.17U、
  T=60 750/99.20%/+19.07U、监听 372/97.85%/+2.78U ⇒ 合计 **n=2080 / 97.60% / +64.03U**（行 6686）。
  再往前（决策 #26 之后）为 T=150 1208/94.04%/+15.02U、T=60 577/99.13%/+16.15U、
  监听 348/97.99%/+3.92U ⇒ 合计 **n=2133 / 96.06% / +35.09U**（行 6412）——三次 Δ 见
  决策 #29 / #32 / #33（⚠️ #33 的 Δ 是**显著为负**的 −2.26U，见其条目）。
- ⚠️ **9 个配置键，BTC 上一律不可调**（`t150_rem 150` / `t60_rem 60` / `price_min 0.80` /
  `dev_min_usd 63` / `sigma_min_usd 40` / `stake 2` / `max_book_lat_ms 300` /
  `walk_min_usd 43` / `floor_min_price 0.83`）——配置键的意义是「能读能对账」不是「该调」；
  `40` 在 2000 次重采样里一次都没成为最优。🆕 `walk_min_usd`（决策 #29）与
  `floor_min_price`（决策 #32/#33，原名 `listen_min_price`）**存在理由与其他键不同一档**：
  它们是**给其他标的各带一份自己的标定值**用的（43 / 0.83 是 BTC 上的标定量，决策 #25）——
  **在 BTC 上照样不可调**。比较符**故意不做成配置键**（算子由段决定，见
  `internal/tail.PriceLeg` 与 `FloorLeg`）。
- **状态**：🟡 纸面登记中。`data/tail-live/` 09-24~10-01 已是**真实 GTC 挂单成交样本**
  （stake 10U，550 笔成交；其中 **≤0.83 的 34 笔 22 赢 12 输 −68.76U**、>0.83 的 516 笔
  +98.87U——整族净剩约 +30.1U。这个廉价口袋是决策 #32/#33 的全部实盘依据）。
- 🔴 **上 live 前的硬阻塞**：CLOB `minimum_order_size = 5 股`，而 `tail.stake=2U` 在 ≥0.80
  只有 2.0~2.5 股 ⇒ **低于交易所下限**（flip 侧 2U@0.20 = 10 股不受影响）。
  必须先定 stake ≥ 5U（每笔风险 2.5 倍，用户决定）+ 拿 1 笔小单验证下限；
  纸面 WR 量级不能直接外推到 live（live 是挂单等成交，成交样本天然偏向「热门侧走弱」，
  与回测「快照瞬间即成交」**不是同一个估计量**）。
- **兼作数据采集器**：本进程默认把每秒原始采样按**数据格式 v2** 落 `runtime.events_dir`
  （默认 `data/events`，与 `cmd/collect` 同构；留空串 = 关闭）——BTC 的 `cmd/collect`
  已停，离线研究靠它续料。见决策 #27 与 `docs/tail_events_2026-09-27.md`。
- **待办**：① 纸面判决改离线脚本（判决卡已随整合改造整删，脚本**未写**）；
  ② 两族合并熔断未实现（flip/tail 各一条独立 −24U 线，等效 48U）；
  ③ 止损腿未落引擎（见决策 #24）；
  ④ **#29 入场闸的前向复验**（三条预设判据写死在 `docs/tail_walk_gate_2026-09-29.md` §5：
  被闸组输率 ≥ 保留组 +20pp / 日级配对 Δ 下界 > 0 / 净 Δ ≥ +10U per 14 天——三条同时满足才扩实盘）；
  ⑤ **#32 监听段地板的前向复验**（判据写死在 `docs/tail_listen_floor_2026-10-01.md` §5，
  触发 = ≥14 完整日或 ≥10 条 `floor_low` 影子行；⚠️ 影子行**不带 outcome**，复验要离线 join）；
  ⑥ **#33 地板下延到 T=60 段的前向复验**（判据写死在 `docs/tail_floor_from_t60_2026-10-01.md` §5，
  触发 = ≥14 完整日或 ≥10 条 **t60 段** `floor_low` 影子行；⚠️ 同 #32，影子行离线 join）。
- ⚠️ **「入场闸」与「价格地板」都不是「提前/更严入场」那一族**：下面那族挪的是**检查点
  位置/阈值**（无条件推后），#29 的闸加的是**市场结构量条件**（结算线已走多远）⇒ 是该族
  **唯一过完整稳健性检验**的改动；#32/#33 的地板则是在**原检查点**上加一条**价格条件**
  ——同 #26 的算子改动一样，属于「不动检查点、只改这一段认什么」，
  ⚠️ 且其 #33 那一半回测为负（实盘口径落地，见决策 #33）。
- 🚫 **闸只闸 T=150 段；往 T=60 / 监听段延 = 已否（2026-10-01，
  `docs/tail_walk_gate_extension_2026-10-01.md`，脚本 `40_tail_walk_gate_ext.py`）**：
  ＋闸 T60 X=43 **−3.45U [−5.73,−1.51]**、X=33 **−3.18U [−5.35,−1.30]**（20~100 全格为负）；
  监听段名义 +3~4.5U 但区间全含零、5/14 天为正、08-18 一天贡献 73~146%，且机制上是
  **`fill ≤ 0.82` 廉价角落地伪装**（ρ(walk,fill)=+0.31，价格地板对照同样不显著）。
  「T60 以及以后」一起加 ≈ 平（−0.62U / +0.14U）。顺带：T150 阈值改 33 也不更好（−4.47U）。
- 🚫 **已推翻的调整（「提前 / 更严入场」整族，不得重提——除非先有新样本或新机制）**：
  T=60 段提前扫描 / T=150 改逐秒 / 检查点位置曲线（29/30/31）、下沿抬到 ≥0.82·≥0.83（32 §2.3）、
  T150→T60 空窗逐秒查 `dev>2·sd ∧ sd>63`（+0.39U）、把同一条件提前到 T150 之前（−1.45U）、
  T150 价格腿 `>0.85`（+1.90U）——配对 Δ 全含 0，共 **16+ 个变体**。
  完整口径与机制见 `docs/tail_entry_timing_2026-09-29.md`（核心：**便宜价编码的是「还没定局」**，
  唯一有判别力的是「位移能否撑到 T=60」，而它在入场那一刻不可知）。**换阈值/换检查点不算新机制**。
  🆕 **2026-10-02 新基线复测 T=100 追加判定点**（用户点名；`docs/tail_checkpoint_t100_2026-10-02.md`、
  `python/v4/43_tail_checkpoint_t100.py`）：Δ **−21.18U [−38.16,−6.79] 区间不含零**（这次是
  **显著为负**不是「含零」），改道 +2.82U 被新增 43 笔 −24.00U 吃掉；四种读法（非严格 / 去 walk 闸 /
  加地板 / 位置曲线八格 T70~135）全负；用户随后点名抬高价格腿（0.80~0.99 × `>`/`>=` 共 **40 格
  无一转正**）——「T100 落在价格波峰」假设被否（更便宜 167 / 相同 173 / 更贵仅 52），
  亏损集中 **0.85~0.95 中价带（78 笔 −13.76U = 段亏 85%）**；X=0.99 只剩 2 笔 Δ≈0 =
  **退化极限不是发现**。⇒ 不落引擎。
- **ETH 上的扫尾盘 = 另一件事**（🔴 门槛不可跨标）：`dev_min_usd 63` / `sigma_min_usd 40`
  / `walk_min_usd 43` 三个都是 **BTC 价格的标定量**（ETH σ 中位 5.36 美元 vs BTC 62.56 美元）
  ⇒ 前两条腿都不可达（`walk_min_usd` / `floor_min_price` 已是配置键、可另行标定，
  但**不许搬 43 / 0.83**）。
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
│   ├── main.go                       # 同上接线, 但三段判定链/尾盘闸/GTC 挂到闭市 + 窗口运行时载体（采集器 4 处接线 + 曲线采样 1 处）
│   ├── events.go                     # **兼作数据采集器**: 每秒原始采样落 events_*（数据格式 v2, 决策 #27）+ events_test.go
│   └── curve.go                      # **页面曲线缓冲**: 本窗 anchor/twap/spot 逐秒点 + 两条派生阈值线（rem≤60 的临界价 / rem∈[60,150] 的外推临界价; 旁路, 决策 #30）——两个公式本体已搬到 internal/tail, 这里只剩缓冲与换窗 + curve_test.go
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
│   │   ├── tail_server.go            # go:embed tail + 路由装配（/api/state, /api/curve, /api/snaps, /api/signals, /api/daily, /api/config）
│   │   ├── tail_handlers.go          # 上述 tail API 的 handler + 映射（除 /api/curve 外）
│   │   ├── tail_curve.go             # /api/curve 全部逻辑: 无参数 = 实况缓冲; ?event_start=N = 该窗曲线（实况命中优先, 否则 events 重建; 决策 #31）
│   │   ├── tail_state.go             # TailState + NewTailState（含**只读**的 eventsDir）
│   │   ├── flip/                     # flip 前端三件套 index.html, app.js, style.css（手机优先）
│   │   └── tail/                     # tail 前端三件套（统计九卡 + 信号表 + 决策表 + 逐日弹窗 + 点行看曲线弹窗）
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
│   │   ├── query.go                  # **按窗口起点回读事件行**: LoadEventByStart（页面「点行看曲线」用, 决策 #31）+ 文件命名唯一来源 eventPath/DayForStart
│   │   └── book_utils.go / market_utils.go / *_test.go
│   ├── settle/                       # 结算编排（零外部依赖, 不 import flip; 两族共用）
│   │   ├── settle.go                 # Outcome/Anchors（按边界秒存推送）+ Resolver 三层回退
│   │   └── settle_test.go            # 判定词表钉在 flip 常量上 + 三层时点/次数/幂等/剪枝
│   ├── tail/                         # 扫尾盘 ⑤ 引擎核心层（零外部依赖; 只复用 flip 的原语, 反向不依赖）
│   │   ├── config.go                 # Config + DefaultConfig()（9 个键; BTC 上全部不可调）
│   │   ├── types.go                  # Observation/Rules/Record/WindowStats + stage/kind/闸原因常量 + 状态机 + IsFilled/HasPosition
│   │   ├── decide.go                 # 纯函数 HotBook（ask 优先/bid 兜底）/ SgnFor / DevUSD / SigmaUSD / EvalRules + 三条段相关腿（PriceLeg 比较符 / WalkLeg / FloorLeg）+ Rule1()…Rule5()
│   │   ├── engine.go                 # 三段递进判定链（Watching→Await60→Listening→Done）+ Resume 崩溃续跑, ProcessTick 返回 0~1 行
│   │   ├── recorder.go               # tail_* / tailwin_* / tailstats_* / tailhold_* 四前缀（独立于 flip 三前缀, 决策 #9 红线）+ recomputePnL
│   │   │                             # ⚠️ 另有第五路输出 events_*（cmd/tail/events.go 的采集器, 走 internal/collect, 与本层无关）
│   │   ├── hold.go                   # 持仓监察（纯函数 HoldWatchRow + HoldRow）: 信号成交后逐 tick 记持仓侧盘口, **只记录不判定**
│   │   ├── exec_state.go             # 风控闸 + 两模式两阶段下单编排（flip.ExecState 的精简镜像; HandleDecision 单入口）
│   │   ├── snapshot.go               # LiveSnapshot/LiveExec + Snapshotter 接口（dashboard 只读消费）
│   │   ├── curve.go                  # 曲线派生量的**唯一公式来源**: RequiredPrice/ExtrapPrice/TieAt + WindowSec/TwapLookbackSeconds（实况与「点行看曲线」的重建共用, 决策 #31）
│   │   └── *_test.go                 # decide（含 HotBook/PriceLeg）/engine/recorder/exec_state/hold/curve/parity（opt-in, 钉 23 的 oracle）
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
| `tail_events_2026-09-27.md` | cmd/tail 兼作采集器：口径对齐表 / 三条红线 / 已知洞（决策 #27） |
| `tail_loss_features_2026-09-29.md` | **输单能不能躲开**：24 逐笔 + 5 日级特征全否（只成交价过置换检验但只是校准）；唯一负口袋 ≤0.81 登记观察 |
| `tail_entry_timing_2026-09-29.md` | **买更早/买更严三问全否**（空窗检查 +0.39U / pre 段 −1.45U / T150 价格腿 >0.85 +1.90U）；机制 = 便宜价编码「没定局」 |
| `tail_stoploss_timing_2026-09-29.md` | 止损优势重审：触发时 `dev_bin −28` 而 `dev_feed +33` ⇒ **赚的是 feed 滞后不是预测**（换结算线坐标优势归零） |
| `tail_dev_decomp_2026-09-29.md` | `dev = basis + walk` 精确分解；walk 当止损腿否掉、当入场闸留 ≈2pp 残差（不稳） |
| `tail_walk_gate_2026-09-29.md` | **T=150 段入场闸 `walk ≥ 43 美元`**：三种语义对照（只闸段 1 = +28.93U）、稳健性、落地清单与偏差（决策 #29） |
| `tail_walk_gate_extension_2026-10-01.md` | **闸往 T=60 / 监听段延全否**：T60 段砍的全是赢单（−3.45U 显著为负）；监听段名义正但不显著、集中在 08-18、机制上是 `fill ≤ 0.82` 廉价角落地伪装（价格地板对照同样不显著） |
| `tail_dashboard_note_2026-09-30.md` | **tail 页面四件事**：撤下的「标定参数」段原文存档 + 本窗曲线图（决策 #30）+ 点行看曲线（决策 #31）+ 图下盘口读数行（§4） |
| `tail_stoploss_crossing_2026-09-30.md` | **止损第四问（下穿/上穿/停留时长）全否**：100 个 τ 格 + 30 个 k 格无一站住；停留携带信息但市场报得更快；闸与止损是替代品 |
| `tail_listen_floor_2026-10-01.md` | **监听段价格地板 `> 0.83`**（决策 #32）：实盘廉价口袋证据、阈值扫描（+8.55U 区间含零）、细账（4 个整窗死亡撑起全部）、安慰剂 p=0.0002、**前向判据**与影子行离线复算口径 |
| `tail_floor_from_t60_2026-10-01.md` | **地板下延到 T=60 段**（决策 #33）：⚠️ 回测显著变差（Δ −2.26U [−4.73,−0.29]）、实盘依据（三段 ≤0.83 口袋 34 笔 −68.76U vs 516 笔 +98.87U）、改道税细账、配置键改名 `floor_min_price`、**前向判据**与离线 join |
| `tail_checkpoint_t100_2026-10-02.md` | **T=100 追加判定点全否**（用户 2026-10-02 提案）：新基线复测 Δ **−21.18U [−38.16,−6.79] 显著为负**、改道 349 笔 +2.82U vs 新增 43 笔 −24.00U、位置曲线 T70~135 八格全负、**价格腿 0.80~0.99 四十格无一转正**（波峰假设被否：更便宜 167/相同 173/更贵仅 52；亏损集中 0.85~0.95 中价带）⇒ 不落引擎，链语义与旧曲线族可比 |
| `Price_required.md` | 「外推临界价」（曲线第 5 条）的**用户口径与推导**：假设现货保持当前速度，进入最后 60s 时得站上哪儿 |

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
| `29/30/31` | T=60 段提前扫描 / T=150 改逐秒 / 追加检查点位置曲线（全否, 见决策 #26 附近） |
| `32_tail_loss_features.py` | **输单特征挖掘**（逐笔/日级/连胜/坏日 + 币安 K 线; 宇宙与 oracle 逐位一致） |
| `33/34/35` | 入场时点三问（空窗逐秒 / pre 段提前 / T150 价格腿 0.85；**全否**，见 `tail_entry_timing_2026-09-29.md`）——34/35 经 importlib 依赖 33，三个须同目录 |
| `36_tail_stoploss_timing.py` | 止损优势的坐标归因（feed 滞后 vs binance 口径） |
| `37_tail_dev_decomp.py` | `dev = basis + walk` 分解 + 两个用途的残差检验 |
| `38_tail_walk_gate.py` | **入场闸三种语义 × 阈值扫描 + 稳健性**（§0 自检钉闸前基线；§8 搜索校正置换）——落地依据，`docs/tail_walk_gate_2026-09-29.md` |
| `39_tail_stoploss_crossing.py` | **止损第四问**：持仓价下穿的**停留时长 τ**（100 格）与**穿越次数 k**（30 格）全扫 + 可成交性折算；§0 自检钉现 pin、§0b 闸前/闸后对照（读 36 的缓存）——**全否**，`docs/tail_stoploss_crossing_2026-09-30.md` |
| `40_tail_walk_gate_ext.py` | **入场闸延伸到 T=60 / 监听段**：闸逐段参数化 × 阈值扫描（X=20~100）+ 改道归因 + 分半 + 判别力置换 + **价格地板对照**（§9 拆穿「闸=廉价角落地伪装」）——**全否**，`docs/tail_walk_gate_extension_2026-10-01.md` |
| `41_tail_listen_floor.py` | **监听段价格地板落地依据**（§0 三把 pin 自检 / §1 廉价口袋 / §2 阈值扫描 / §3 细账与分半 / §4 安慰剂 `--nperm`（400 秒级、4000 约 9 分钟）/ §5 前向判据）——`docs/tail_listen_floor_2026-10-01.md` |
| `42_tail_floor_from_t60.py` | **地板下延到 T=60 段的落地依据**（§0 三把 pin 自检 = C43/#32/#33 / §1 廉价口袋 / §2 阈值扫描与负 Δ 警告 / §3 细账：整窗死亡 × 改道税 / §4 实盘对照 09-24~10-01 / §5 前向判据）——`docs/tail_floor_from_t60_2026-10-01.md`；⚠️ 41 号脚本的链**冻结在「只拦监听段」的历史形态**，现行口径看 42 号 |
| `43_tail_checkpoint_t100.py` | **T=100 追加判定点否证**（§0 三把 pin 自检 + `chain(cks=())` 与 #33 链逐位一致 + 丢失信号 0 / §1~§3 主体与分解 / §4 来路（walk_low 回收 −1.17U）/ §5 敏感性与位置曲线 / §6 价格腿扫描 0.80~0.99 × `>`/`>=` 40 格 + 波峰检验 + 分桶）——`docs/tail_checkpoint_t100_2026-10-02.md`；链语义沿用 31 号（每个检查点消费上一个被消费 tick 之后首个 `rem ≤ 阈值` 的 tick） |

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
Watching ──首个「rem≤t150_rem(150) 且 spot 可算」的有效 tick──▶ [判 ⑤ ∧ walk ≥ 43 美元]
   ├─ OK   → 落信号行（下单）→ Done
   └─ 拒绝 → 落判定行 → Await60
             （首个可判定 tick 已在 rem≤t60_rem(60) 时: 整段跳过, **不伪造 t150 行**）
Await60  ──首个「rem≤t60_rem(60) 且 spot 可算」的有效 tick──▶ [判 ⑤]
   ├─ OK 且有效价 > floor_min_price(0.83) → 落信号行（下单）→ Done
   ├─ ⑤ 达标但价 ≤ 地板（决策 #33）→ 落一行 floor_low **影子行**（每窗至多一条）→ Listening
   └─ 拒绝 → 落判定行 → Listening
Listening ──此后**每秒**: 有效 tick ∧ ② 达标 ∧ 有效价 > floor_min_price(0.83)──▶ 信号行 → Done
   ├─ ② 达标但价 ≤ 地板（决策 #32）→ 落一行 floor_low **影子行**（与 T=60 段共用闩锁 ⇒ 每窗至多一条）, **继续听**
   └─ rem == 0 → Done
```

- **有效 tick** = `BookLatMs ≤ tail.max_book_lat_ms` ∧ 热门侧**有效价 > 0** ∧
  `spot > 0` ∧ `rem > 0`（有效价 = `ask > 0 ? ask : bid`，四档全空才无有效价——
  决策 #21 修「空侧整簿不得丢弃」，与回测宇宙在 14 天数据上同源）。
  **缺 spot 的 tick 只跳过、不推进任何段**。
- **任一段出信号即整窗只下一单**；两个判定段**成功与否都落一行**，监听段**只在达标时落行**
  （外加至多一条地板影子行）⇒ `tail_*` 每窗 ≤4 行（全部 `kind=snap`，按 `stage` 分 t150/t60/listen）。
  行类型 `kind=frame`/`kind=scan` 为 legacy-only（`isKnownKind` 仍认历史旧行）。
- **T=150 段入场闸 `walk ≥ 43 美元`（决策 #29）**：`walk = sgn·(twap − anchor)`（结算线已走的
  位移，**不含**缺口 `basis = sgn·(spot − twap)`），`dev = basis + walk` 恒等。
  只闸段 1（段 2/监听段一字不动，见 #26 仍是价格腿算子差异）；不达标 ⇒ `reject_reason =
  walk_low` 且**链继续**（不是整窗弃单）；`twap ≤ 0` **放行**（fail-open，与 oracle
  `walk is None ⇒ 放行` 同口径）。阈值 = 配置键 **`tail.walk_min_usd`（默认 43）**，
  ⚠️ BTC 价格的标定量、**在 BTC 上不可调**（决策 #29）。
- **价格地板 `有效价 > 0.83`（决策 #32 监听段 + #33 下延到 T=60 段）**：**T=60 与监听这两段**
  在各自规则之外还要有效价**严格大于** `tail.floor_min_price`（默认 0.83，原名
  `listen_min_price`）才成交；被拦 ⇒ 落**一行** `floor_low` **影子行**（`ok=false`，
  **两段共用闩锁 ⇒ 每窗至多一条** = 无地板世界里本该成交的那一笔）且**链继续**
  （T=60 被拦 ⇒ 已转入监听段；监听段被拦 ⇒ 同段继续等更贵的 tick）。**T=150 段一字不动**。
  ⚠️ 影子行 `ok=false` ⇒ `HasSignal`（防双单）/ `HasStage`（Resume）/`isSettlable`（结算）
  都不认它 ⇒ 不进信号/仓位/熔断/胜率、**行内没有 `won`**（前向复验靠离线 join，
  见 `docs/tail_listen_floor_2026-10-01.md` §5 与 `docs/tail_floor_from_t60_2026-10-01.md` §5）。
  ⚠️ **#33 这一半在回测上是负的**（Δ −2.26U，日级配对 95% [−4.73,−0.29]）——落地依据是
  **实盘口径**（≤0.83 的口袋是全族唯一系统性输钱的一格），不是回测增益。
- **拒绝原因链**：`missing_spot → no_hist → price_low → leg_out → walk_low`。⚠️ `walk_low`
  **排在最后**是有意的：这样它只覆盖「闸是唯一拦路者」的行（14 天 250 行 = 被闸的 250 笔
  T150 信号），离线反事实才干净；排在 `leg_out` 之前会把本来就没过 dev/σ 的行也读成被闸。
  ⚠️ **`floor_low` 不在链里**——它只出现在 T=60/监听段的影子行（`ok=false`），判定行的链一字未动
  （T=60 段的判定行仍是「⑤ 不达标」那一族，地板拦下时落的是影子行而非判定行）。
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
  **本窗曲线**（anchor/twap/spot 三线 + 两条派生阈值线：`rem ≤ 60` 的「临界价」与
  `rem ∈ [60, 150]` 的「外推临界价」，`/api/curve`）是**同一性质的第 5 项读数**：
  缓冲在主循环里每秒采一点（`cmd/tail/curve.go`），窗末保留到下一窗首个采样到达才换装，
  同样不参与判定（决策 #30）。
  **点行看曲线**（点信号表/决策表任意一行 → 弹窗画那一窗; 决策 #31）是第 6 项：它**只读**
  `runtime.events_dir` 里的原始采集（不新落任何盘），实况命中就还是那条内存缓冲。

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
go test ./internal/... ./cmd/... -count=1  # 引擎/编排/记录器/盘口工具/结算/配置 + cmd/tail 采集器（含 YAML 漂移守卫）
go test ./internal/... -race           # 同上 + 竞态（含扫尾盘 parity 全量重放, ~2min）
go test ./cmd/tail/ -race              # 采集器的窗口换装/成交路由竞态回归
go test ./internal/tail/ -run TestParityBacktest -v   # 扫尾盘 Go↔py 逐窗对账（opt-in; data/btc 缺失即 skip）
go build ./...
go run ./cmd/flip -config v4.config.yaml -dashboard :8090     # 运行引擎 + Dashboard
go run ./cmd/tail -config v4.config.yaml                      # 扫尾盘（**兼采集 events_* → data/events**）
go run ./cmd/collect -config v4.config.yaml -asset eth        # 采集 ETH → data/eth

# python 分析/回测脚本一律用项目内 venv（系统 python3 无 numpy/pandas）
python/venv/bin/python python/v4/01_backtest_r1.py
python/venv/bin/python python/v4/06_oos_review.py                      # 09-15 复验裁判（纯标准库）
python/venv/bin/python python/v4/07_source_health_check.py [--bt-scan] # 数据源健康度审计
python/venv/bin/python python/v4/13_tail_sweep.py                      # 扫尾盘早期回测
python/venv/bin/python python/v4/24_asset_data_check.py --asset eth    # 采集数据校验（任意标的）
python/venv/bin/python python/v4/24_asset_data_check.py --asset btc --dir data/events   # 校验 cmd/tail 采下来的事件行
```

> ⚠️ `internal/feed` 的 `TestRecoverAnchorSettleGuard` 是**既有**的时序敏感用例
> （10/30/60ms 采样点 vs 50ms 收敛点），整套并行跑有约 1/4 概率误判，与业务逻辑无关。

**验收红线（改 flip/tail 判定链必跑）**：

- `cmd/btreplay` 全量重放 `data/btc` **625 笔逐位一致**（n=625 WR 24.6% EV +0.633U +395.8U）
  ——Go 引擎与 `01_backtest_r1.py` 的口径对账，只喂 `BeginWindow(ev.TwapOpen, σ)`、
  不碰 `internal/feed`。
- `TestParityBacktest` 五格 + 三闩锁计数逐位一致：现行 pin =
  **6704 / 2074 / 3640 / 3 / 250 / 24 / 97.830280% / +70.314372U**（t150 3638/958、
  t60 2676/741、listen 390/375；250 = `walk_low` 行数、24 = `floor_low` 影子行数，
  其中 t60 段 9 条 / 监听段 15 条），
  oracle = `python/v4/23_tail_integrated.py`。#32 地板（只拦监听段）= 6697 / 2076 /
  3640 / 3 / 250 / 15 / 97.832370% / +72.578473U；#29 闸后（地板前）= 6686 / 2080 /
  3640 / 3 / 250 / 97.596154% / +64.026354U；闸前（决策 #26 之后）= 6412 / 2133 /
  3640 / 3 / 96.061885% / +35.092748U。
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
      `missing_spot → no_hist → price_low → leg_out`〔#29 之后末尾追加 `walk_low`，
      见「扫尾盘引擎状态机」与决策 #29〕。**监听段的拒绝 tick 不落行**（每窗 ~60 个
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
    脚本 `python/v4/{25,26,27}`；后续三轮：`36`（坐标归因）/`37`（walk）/`39`（下穿结构与停留时长））：
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
    - ❌ **下穿 / 停留时长维度已否（09-30 第四轮，`docs/tail_stoploss_crossing_2026-09-30.md`，
      脚本 `python/v4/39_tail_stoploss_crossing.py`）**：把「持仓侧 bid 跌破 L 后**连续停留
      ≥ τ 秒**」在 10 档价位 × 10 个 τ 上全扫（100 格），再换「**第 k 次**下穿才卖」扫 30 格——
      **无一格能站住**。最好三格 `L=0.20 τ=3s` +5.67U [−1.33,+12.64] / `L=0.40 τ=8s`
      +5.34U [−3.13,+13.73] / `L=0.70 k=3` +4.87U [−10.26,+20.29] **全部含零**；
      全表 **CI 下界 > 0 的只有一格**（`L=0.20 τ=30s` +1.04U，触发 32 笔、**出场价均值
      0.015 美元**、胜率 0% = 卖的是已经归零的仓位）。折算后最好 +2.33U。
      - **为什么停留时长没用**：输率**确实**随 τ 单调上行（0.60 档 `τ≥0s` 41.7% → `τ≥30s`
        91.5%），但那正是**报价同步下行**所预示的那件事。按 36 §3 的判据（出场优于持有 ⟺
        `bid > P(win)`）把「实际输率」与「市场隐含输率 `1 − 出场中位 bid`」相减，100 格里
        中位数 **−2.0pp**、正值仅 20 格 ⇒ **市场报得比实际更快**。`k` 越大越好是同一件事
        （第一次跌破绝大多数是假摔），上限也一样。
      - ⚠️ **入场闸与止损是替代品**（本轮 §0b，最要紧的一条）：同一个参照规则
        `bid<.30 ∧ dev<−20` 在**闸前** n=2133 上 +9.54U [+1.41,+18.47]、在**闸后** n=2080 上
        只剩 +4.99U [−2.25,+12.37]。两者吃的是**同一个结构**（报价先塌、结算线位移后到）——
        #29 的 `walk` 闸问的正是「滞后之外还剩多少真写进账本」⇒ 止损的增量价值已被闸拿走。
        再次落地止损前**必须先看它在现行宇宙里的 Δ**，不能用 09-25 的 +10.44U。
      - ⚠️ **口径坑**：「下穿之后有没有回到 L 之上」**不能当判据**——亏损窗最终必然跌回任何
        L 之下，`回来过又输` 的窗 100% 会再次跌破（构造性恒等式，脚本 §6 存档）。
        要问必须用固定时间窗（τ）或穿越次数（k）。
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
26. **T=150 段价格腿改严格大于（`> 0.80`）：三段之间唯一的规则差异**〔⚠️ 本条写就时的表述，
    2026-09-29 起 #29 又给 T=150 段加了 `walk ≥ 43` 入场闸 ⇒ **不再是唯一的段相关差异**；
    本条自身的内容未被改动〕。**只改 T=150 段**——
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
27. **cmd/tail 兼作数据采集器：每秒原始采样落 `events_*`（数据格式 v2）**（2026-09-27 用户需求;
    `cmd/tail/events.go` + `cmd/tail/main.go` 4 处接线; 完整口径表与已知洞见
    `docs/tail_events_2026-09-27.md`）。**动机**：BTC 原始采集（`cmd/collect`）自 2026-08-31
    起已停 ⇒ 离线研究（止损成交换手、成交量、OFI…）无米下锅，而**常驻进程本来就在采**。
    - **默认开启**：`runtime.events_dir` 默认 `"data/events"`，留空串 = 关闭。服务器那份手工
      维护的 tail 配置没有这个键 ⇒ 走默认值 ⇒ **部署即生效**，不必改配置（也没加任何 flag）。
    - **采样不挂在交易 tick 循环里**：每窗一个独立 goroutine 自带 1s ticker（循环内有同步
      gamma 预取、live POST、持仓监察落盘，卡顿会丢 tick），终点精确补采一次，再等精确收盘
      推送（≤10s）→ 组装 → `SettlementWorker.Submit`。两个 goroutine 入口各一个 `recover`：
      **采集侧 panic 绝不允许杀活钱进程**，只停用采集。
    - **与 `data/btc` 逐字段同构**，口径全部照 `cmd/collect`：锚来源词表**必须翻译**
      （`feed.AnchorSourceStream → collect.SourcePush`——两个包的 `"stream"` 同名不同义，
      不翻译则校验 G 段白丢一道红线 + 每窗白跑官方修正）、`outcome` 平局算 Up、
      close 推送缺失回退流值/锚价、tick 用 `LatestData()` **原始值**（不套引擎的陈旧钳零——
      落 0 是校验 D 段硬 FAIL）。
    - **成交按自身时戳选桶、用被选中那个桶自己的 token 表**映射 YES/NO（本族 upTok/downTok
      每窗换装，不能像 `cmd/collect` 那样用全局 prev 变量）；边界后 1~2s 才推送的尾盘笔由此
      回填进上一窗。
    - **三条落盘红线（宁可留洞，不写脏行）**：锚 ≤ 0 / `binance_open` ≤ 0 / tick 数 < 293
      （校验 `TICK_MIN`）或首 tick `rem` < 295 ⇒ 整窗丢弃并各打一行 ⚠️。
    - **跳窗路径从不 BeginWindow**（`no_sigma`/`late`/`no_market`/`no_token`/`dup_record`）
      ⇒ 那些窗一行都没有 ⇒ **下游按 `start_time` 对齐，不能按行数/连续性假设**。
    - **顺带修掉的隐患**：`feed.FetchKlineOpenPrice` 失败时 adapter 里留的是**上一窗**的开盘价
      （静默陈旧）⇒ 改成返回 `float64`（失败返 0），调用方一律用返回值（`cmd/collect` 同源隐患
      一并修）。
    - **有意不重构 `cmd/collect`、不下沉 `internal/collect`**：下沉会让格式层反向依赖 feed 的
      数据源层；两族同目录双写会互相盖行（同 `start_time` 判重跳过）。这是刻意的重复。
28. **删掉 tail 的 live 首窗禁单：闸只剩日亏熔断一条**（2026-09-29）。原闸 = live 模式下
    「重启后首个完整窗口一律不下单」（`tail.ExecState.FirstWindow`，`cmd/tail` 主循环首个完整
    窗口跑完才解除），动机是防重启残留窗双单。删除依据 = **重启路径本身已经撞不上同一市场**：
    - 重启落在边界后 `> lateLimit`(15s) ⇒ **整窗跳过**，旧进程交易过的那一窗根本不重入；
    - 重启落在边界后 `≤ 15s`（会 join 本窗）⇒ 旧进程在该窗**物理上不可能已下单**——本族最早决策点
      在 `+150s`（T=150 段），旧进程要落单必须活过 `+150s`，与「新进程 ≤15s 就起来了」矛盾；
    - 万一真重入同窗：`Recorder.HasSignal`（`dup_record` 整窗跳过）+ `Engine.Resume` 续跑已判过的段。
    ⇒ 原闸唯一还挡着的只剩「**两实例同时在跑**」（滚动重启先起新再杀旧，或忘了杀旧）——那是运维
    纪律（重启用「**先杀旧、再起新**」），不该每启一次就白丢一窗信号。
    - ⚠️ **它不是空闸**（这是删除的直接动因，也是别把它当无害的理由）：`data/tail-live` 09-24~27
      有 **5 条** `gate_reason=first_window` 行（t150×3 / t60×2，stake 10U），有效价全部 ≥0.94
      且**全赢**。删除时被否掉的一个前提是「重启后那个窗口天然不会出信号」——上面 5 行即反例；
      成立的是它的前半句「等前一个窗口跑完」，故结论保留、论证换成上面三条护栏。
    - **改动面**：`internal/tail/exec_state.go` 删字段与闸分支（`gate()` 只剩 `breakerTripped`，
      `gateNote` 只剩日亏一条）；`cmd/tail/main.go` 删初始化与「首窗结束解禁」块。常量
      `GateFirstWindow` **按 legacy 保留**（历史行要照显、前端标签要能认——同 `KindFrame`/`KindScan`
      的老行口径），但引擎**不再产出**。
    - ✅ **flip 侧不动**：同一套论证对 flip 也成立（它最早触发点在 `rem>180`，同样远晚于 15s），
      但 flip 仍是纸面、live 路径未启用 ⇒ 要动等它上实盘时单独立项。
29. **T=150 段加入场闸 `walk ≥ 43 美元`（2026-09-29 用户决定「只闸 T=150，X=43 美元」）**。
    依据 `docs/tail_walk_gate_2026-09-29.md`（落地清单 §5），前置分解
    `docs/tail_dev_decomp_2026-09-29.md`，脚本 `python/v4/38_tail_walk_gate.py`。
    - **量**：`walk = sgn·(twap − anchor)` = **结算线已经走掉的位移**（唯一写在账本上的部分）；
      `basis = sgn·(spot − twap)` = 现货缺口（**领先量**，随现货回摆而消失）。两者
      **精确恒等** `dev = basis + walk`（逐 tick 残差 0）。闸只认 walk ⇒ 拦的是「dev 全靠缺口
      撑起来、结算线自己没动」的单。
    - **形态 = 只闸段 1（mode C）**，三选一的钱：A 逐段 AND（每段都要求）+28.31U 区间
      [−1.72,+61.59] 含零 / B 整窗闸（首个判定点不达标即整窗弃单）+14.48U [−15.91,+49.59] 含零 /
      **C 只闸 T=150 +28.93U [+5.34,+59.93]**。A 与 C 净收益相当但 A 白砍 258 笔零效果交易；
      B 更差是因为它把**改道这条路也堵死**（改道那 197 笔绝对 P&L 是 **+1.78U**，不是负的）。
    - **机制（钱从哪来）**：250 笔 T=150 信号被闸 ⇒ **53 笔整窗死亡**（这批量里 26 笔输，
      输率 **49.06%** vs 保留段 3.98%，基线里 −46.14U）**+ 197 笔改道**到 T=60/监听以
      中位 **0.990** 重入（同一批窗 P&L 从 +18.99U 压到 +1.78U，即 −17.21U 改道税）
      ⇒ 净 **+28.93U**。闸的价值 = 「躲开那 53 个真的不会长好的」−「197 个少赚的差价」。
    - **稳健性**（把「扫过阈值」算进去）：搜索校正置换（层内打乱 walk0、每次重扫 12 个阈值取最大）
      **p = 0.0015**；分层 = 精确 fill × sd 四分位 × rem0 十秒档 × 侧别 = 127 层；分半切点 08-25
      两半同号（+11.04 / +17.90）；安慰剂闸（按 fill 分层随机砍同样笔数 4000 次）
      **p = 0.0000**；σ 倍口径等效（0.7σ +29.02U / 1.0σ +28.07U）⇒ **美元口径即可**。
    - **实现**：纯函数 `WalkUSD` / `WalkLeg(cfg, stage, side, twap, anchor)`（段相关性写在函数里，
      非 T150 段恒放行）；`engine.decision()` switch 末尾加一条 `case !WalkLeg(...)`。
      **`twap ≤ 0` 放行**（fail-open，与 oracle 的 `walk is None ⇒ 放行` 同口径，14 天零命中）。
    - **阈值 = 配置键 `tail.walk_min_usd`（默认 43）**〔2026-09-29 同日稍后由常量改成键，用户
      决定〕：与 `dev_min_usd`/`sigma_min_usd` 同性质——43 是 **BTC 价格的标定量**，键存在的
      **唯一**理由就是让 ETH 等其他标的各带一份**自己的**标定值（决策 #25），`DefaultConfig()`
      仍是回测标定的单一真相（oracle 23 恒定 43，parity pin 只在默认值下成立）。
      **在 BTC 上它仍不可调**（43 是那批数据的 argmax，见上条风险 ①）。
      校验只加一条**结构性**的：`walk_min_usd ≥ 0`（负值 = 闸形同虚设却仍按「有闸」落
      `walk_low` 行 ⇒ 静默退化）；`0` 是合法弱闸。`/api/config` 与启动横幅均已下发该值。
    - ⚠️ **拒绝链 `missing_spot → no_hist → price_low → leg_out → walk_low`：`walk_low` 排在
      `leg_out` 之后是有意的**（与本文档早期草稿的顺序相反）——这样它只覆盖「闸是唯一拦路者」
      的行，计数恰好等于被闸的信号数（14 天 **250** 行），离线反事实才干净；排在前面会把
      dev/σ 本来就不达标的行也读成「被闸」，凭空多一批。
    - **新 pin**（oracle 23 同日更新）：行 6412→**6686**、信号 2133→**2080**、WR 96.061885%→
      **97.596154%**、P&L +35.092748→**+64.026354U**、输单 84→**50**、`walk_low` **250** 行；
      窗数 3640 与 `no_sigma` 3 不变。分段：t150 3638/**958**/96.242171/+42.173827、
      t60 2676/**750**/99.200000/+19.067819、listen 372/**372**/97.849462/+2.784708。
    - ⚠️ **风险三条**（结论不变，但决定落地方式）：① **X=43 是同一批 14 天数据上的 argmax**
      ——已把搜索代价算进 p 值，但**真正的独立样本仍然没有**（曲线在 X∈[30,50] 是平台不是尖峰，
      σ 倍口径复现同量级 ⇒ 不像噪声，但要复验）；② **整个效应由 26 个输单撑起**（49.06% 的
      Wilson 95% 约 [36%,62%]），**08-26 单日贡献 +12.52U（占 43%）**；③ **改道价 0.990 与
      实盘 stake 的交互**：`stake=2U` 在 0.99 只有 **2.02 股 < CLOB `minimum_order_size = 5 股`**
      ⇒ **stake 不提到 ≥5U，这 197 笔在实盘根本挂不出去**（这不是本闸引入的，但本闸的经济学
      **依赖**改道能成交）。
    - ⚠️ **与已推翻家族的关系**：`docs/tail_entry_timing_2026-09-29.md` 否掉的是**无条件**推后
      （空窗检查 / pre 段提前 / T150 价格腿 >0.85，共 16+ 变体）；本闸是**有条件的**推后，
      条件用的是**市场结构量**（结算线已走多远）而**不是挪某个检查点或换阈值** ⇒ 不适用
      「换阈值/换检查点不算新机制」那条纪律，属于新机制。
    - **前向判据（先写死再跑）**：① 被闸组（`walk0 < 43` 的 T150 信号）输率 ≥ 保留组 **+20pp**
      （现在 49% vs 4%）；② 链的日级配对 Δ 的 95% **下界 > 0**；③ 净 Δ ≥ **+10U / 14 天**。
      三条同时满足才扩实盘；任一不满足 ⇒ 撤回（保留记录行，关掉闸）。
    - ✅ **flip 侧不动**：本闸建立在 tail 的入场结构上（尾盘买已经赢定的热门侧，walk 就是
      「赢定了多少」），flip 是逆向买冷门侧，没有对应的量。
30. **tail 页面加本窗曲线图（anchor / twap / spot 三线同轴 + 两条派生阈值线）+
    页面撤下「标定参数」段**
    （2026-09-30 用户需求；文档 `docs/tail_dashboard_note_2026-09-30.md`）。**只动 Dashboard
    侧，引擎判定链一字未改**。
    - **撤下的那段**（窗口卡底部的「三段链（不可调，仅纸面登记）…」整块 + 前端
      `renderConfig()`）**原文存档**在文档 §1——理由：它把「规则 / 标定值 / 执行口径」混在
      一行里，数字又会随配置键变（`stake` 已从 2U 到 10U），**同一句话在页面与文档两处维护
      必然漂移**。页面只留实时读数，规则去文档看。`/api/config` 接口**照旧保留**（闩锁标签、
      逐日明细等处仍在用），删的只是那段说明文字。
    - **采样在服务端主循环**（`cmd/tail/curve.go` 的 `curveBuf`，主循环每秒 tick 后
      `sampleCurve` 一点）：前端按 `/api/state` 的 **5s** 轮询攒点的话，300s 窗口只剩 60 点
      ⇒ **漏掉 4/5 的采样**。曲线因此走**独立路由 `/api/curve` + 独立 1s 轮询**，
      与 `/api/state` 的节奏解耦。
    - **红线：曲线是纯旁路**。三个值取自与判定同源的读数（`flip.Tick.BinPrice/TwapPrice`
      + `engine.WindowAnchor()`），但**不参与任何判定**、不写任何文件、缓冲丢了只影响页面上
      那条线——与持仓监察（#24）同一性质，分开前缀/分开路由，不碰引擎状态。
    - **换窗语义在服务端**：`curveBuf.add` 见 `eventStart` 变化即整条换装；新窗第一个采样
      到达**之前** `/api/curve` 返回的**仍是上一窗**（`event_start` 与 `points` 都不变）
      ⇒ 前端「保留旧曲线到下一窗数据到达」是数据流的自然结果，不是前端的定时擦除。
      `cmd/tail/curve_test.go` 钉住这条（含 `Anchor=0`/`Spot=0` 原样下发、前端断线不画）。
    - **实现面**：`internal/tail.Curve/CurvePoint` 类型 + `Snapshotter` 接口加 `Curve()`
      （**两族接口各自的 `fakeTailSnap` 同步加方法**）；前端 Canvas 2D 手绘（无库，
      devicePixelRatio 缩放，y 量程按数据自适应取整到 1/2/5×10ⁿ）+ 悬停准星读数行。
    - ⚠️ **配色不走页面 accent 色**：`--accent/--pos/--down` 那套在暗面 `#161b22` 上跑
      dataviz 校验器**不合格**（绿↔黄色盲分离度 ΔE 5.1 < 6.0 下限），改用分类槽
      蓝 `#3987e5` / 水绿 `#199e70` / 橙 `#d95926`（全项通过）。**换色或换表面色前必须重跑
      `scripts/validate_palette.js`**（用法写在 `style.css` 注释里）。
    - 🆕 **第四条「临界价」是派生量不是读数**（`cmd/tail.RequiredPrice` + `curveBuf.tieFor`，
      `rem ≤ 60` 起才有值）：

      ```
      P = (60 · TWAP_open − HistoricalSum) / rem
      ```

      `HistoricalSum` = 结算窗里**已经定局那 (60−rem) 秒**的价格之和（逐秒采样加总）；含义是
      「现货从现在起一直守在 P 之上不动 ⇒ 闭市 TWAP-60 恰好压在 anchor 上 ⇒ 结算 Up」。
      ⚠️ **不写成展开式** `anchor + (anchor − past)·(60−rem)/rem`（`past` = 已定局秒的均价）：
      两者**恒等**（代入 `past = HistoricalSum/(60−rem)` 即得），但**部分和形式正好是手上攒着
      的那个量**，少一次「先除成均价、再乘回秒数」的来回。边界：`rem = 60` ⇒ 和为空 ⇒
      `P = 60·anchor/60 = anchor`；`rem → 0` ⇒ 发散（只剩几秒扳回整段偏差，是真的）；无定义
      ⇒ 0（前端断线、读数行显「—」）。⚠️ **缺样本的秒必须补**（`sum/n × (60−rem)`）：只把手上
      有的样本加起来会让 `(60·anchor − HistoricalSum)` **凭空少掉整秒的价格**；样本不足期望
      一半（`2n < span+1`）则整个点返 0——宁可不画。三条口径让路：
      - **近似不是口径**：`HistoricalSum` 取自**逐秒 Binance 现货**而结算走 Chainlink ⇒ 有已知
        水平差（memory「spot − TWAP 水平差」）。**严格口径做不到**——上游只推 TWAP-60 本身，相邻
        差分只给出 `x(t) − x(t−60)`，逐秒价反解不出。它**不参与判定、不进 P&L**，纯看图。
      - **在 y 量程里限幅参与**（2026-09-30 当天修）：量程以三条实测线为基准，临界价**也
        参与**，但取值**先夹到三线 span 的 ±`TIE_ROOM` 倍以内**再取 min/max（`app.js`
        常量，默认 `0.5` ⇒ 量程最多涨到三线 span 的 2 倍 ⇒ 三线至少占约 1/3 图高）；超出
        限幅的部分仍旧**裁到绘图矩形内**。⚠️ 原先「完全不进量程」是**错的**：它一涨出三线
        范围就只剩一截断头线挂在画框下沿、之后什么都没有，**与「无数据」的断线长得一模
        一样**（实测某窗 `rem=19` 时它已跌到 83204.63 而三线下沿 83532.47 ⇒ 整条线只可见
        十几秒）；而**全额**放进量程同样不行——`rem→0` 发散（`rem=5` 放大 11 倍、`rem=1`
        放大 59 倍）会把整窗的三线压成平线。夹取（而非「超了就退回三线量程」）是为了
        **连续性**：量程平缓长大、到顶停住，画面不跳。⚠️ `TIE_ROOM` 取 `0.5` 是**扫出来的**：
        `0.25~1.0` 在真实窗上给出同一个量程（`niceStep` 取整把各档吸到一起），而 `0.5→1.0`
        可见秒数 41s→41s 一动不动、三线占高却 36%→26%。实测收益（真实窗）：量程
        `83600–84000`→`83400–84000`，临界价可见 21s→**41s**（`rem 40`→`rem 20`），三线占高
        54%→36%。代价 = 量程会随临界价变宽并**朝那一侧平移**（取全窗 min/max）；`rem < 20`
        那段仍裁掉，那是限幅在起作用。线贴边消失 = 量程仍装不下它 =「现货离守住差得远」，
        确切数字看读数行。
      - **不占第 4 个分类色槽**：槽 4（黄 `#c98500`）在本表面与橙色的色盲分离度只有
        ΔE 4.8（< 6.0），换洋红更差（与水绿 1.6）⇒ **第四个可区分的颜色不存在**。改走中性
        墨色 `--c-tie #8b949e` + **虚线**（图例色块同款虚线条），与实测线在**形状**上区分。
        三条实测线维持原样、原校验结论不变。
      ⚠️ 「网格不用虚线」那条规矩**只管网格**：临界价是**真的**阈值，虚线正是它该有的编码。
    - 🆕 **第五条「外推临界价」也是派生量**（同日稍后加，用户口径与推导
      `docs/Price_required.md`；`cmd/tail.ExtrapPrice`，`rem ∈ [60, remMax]` 才有值，`remMax` 由调用点
      传 `cfg.Tail.T150Rem`——**从策略第一个判定点起画**）：

      ```
      S = Spot·(1 − k) + TWAP_open·k      k = (rem − 60) / (rem − 30)
      ```

      **含义**：假设现货从此刻起保持当前速度 `v` 线性运行，最后 60s 的均值 = 它中心点
      （距现在 `rem−30` 秒）处的价格 `S₀ + v·(rem−30)`；令其等于 `anchor` 解出临界速度
      `v_critical = (anchor − S₀)/(rem−30)`，而**进入最后 60s 那一刻**（距现在 `rem−60` 秒）
      的价格就是上式。等价说法：**当前速度必须 ≥ `v_critical`**。
      - **两条派生线拼满整窗，而且正好对上三段链的两个检查点**：外推管
        `rem ∈ [60, 150]`（T=150，假设运动**继续**）、临界价管 `rem ∈ (0, 60]`（T=60，假设
        运动**停住、守住一个价**）。两者的假设正好相反 ⇒ **`rem = 60` 处二者不相等**
        （外推 = `Spot`，因为只剩「现在」速度来不及起作用；临界价 = `anchor`）。画面上是
        t=240s 处一上一下错开的两条线——**不是断线也不是 bug，是两个问题的答案本来就不同**。
      - ⚠️ **它不需要任何量程照顾**：`k ∈ [0, 0.75]` ⇒ 它恒是 `Spot` 与 `anchor` 的**凸组合**，
        而这两个值本身就在三线 min/max 里 ⇒ 永远落在实测线之间（真实窗实测外推范围
        `[83706.40, 83820.74]` ⊂ 三线联合量程 `[83668.06, 83884.00]`）。前端仍让它过限幅那
        一关，但那是**恒等变换**（只为「派生量统一处理」），不是它需要——对比临界价在
        同一窗已跌到 `83006.38`、要靠 `TIE_ROOM` 才收得住。
      - **编码**：与临界价**共用**那支中性墨色（第 5 条更没有新色可用），靠**虚线节奏**
        区分——临界价长划 `[5,4]`、外推临界价点线 `[1.5,3.5]`（图例色块 `repeating-linear-
        gradient` 同款两种节奏）。形状本来就是阈值该用的编码通道。
      - ⚠️ **图例容器的 `white-space: nowrap` 只许加在每个条目上**：加在容器上会让图例的
        最小宽度 = 整宽 ⇒ flex 去挤标题（390px 下实测标题被压成一列三行）。容器管换行
        （`flex-wrap`）、条目管不折；`.curve-head` 同样要 `flex-wrap`（5 项在窄屏整体落到
        标题下一行）。同理悬停读数行每项包 `.bit`（nowrap + `::before` 挂分隔符）：5 项在
        390px 必然两行，折点只能在**项与项之间**，且窄屏常态就占住两行（`min-height: 34px`）
        ——触摸设备上按一下就顶下去一行比多占 17px 难看得多。
        ⚠️ 图例**自本条之后**改由 `app.js` 的 `SERIES` 生成（`renderLegend`）——主图与
        §31 的弹窗图共用一份定义, `title` 提示也在 `hint` 字段里。
    - 🆕 **图下第二条读数行 = 那一秒的盘口**（同日稍后加, 用户需求；文档同 §4）：
      `盘口 t=175s · UP 0.94 / 0.95 · DOWN 0.05 / 0.06`（UP=YES / DOWN=NO 各给 bid/ask）,
      **主图与弹窗图都有**、与曲线共用同一套悬停逻辑。`CurvePoint` 增
      `yes_bid/yes_ask/no_bid/no_ask` 四字段（实况路径照抄引擎 tick 的 `UpBid/…`**原样**,
      重建路径照抄 `ticks[i].pm.*`）。
      - **它是纯读数、不过判定路径的延迟闸**（延迟闸管「这一 tick 能不能用来判定」,
        看图上某一秒的盘口是另一件事——与 `/api/state` 现窗口读数同源同口径）;
        **不画成线**: 四条 0~1 的价格线进美元量程会把三线压平。
      - **0 = 该侧无报价 ⇒ 前端显「—」**（决策 #21 的整侧撤空是常态, 而「何时被撤空」
        恰是这张图的一个用处——0 不能被当缺数据吃掉）; 四个全 0（窗首瞬态, 决策 #25）
        ⇒ 直说「无盘口」, 不和「某侧空」混同。
      - ⚠️ **不悬停时的默认点 = 最后一个**有报价**的采样点**, 不是最后一点:
        采集行的收尾补采 tick（闭市那一刻精确补一次）盘口四档**恒空**——本机三个采集
        文件 63 个窗里末 tick 全零 **43 窗**（采集侧构造性产物, 不是市场读数）,
        而弹窗默认正好落在它上面 ⇒ 常驻「无盘口」, 看起来像坏了。读数行自带 `t=` 标签,
        回退不冒充「最后一秒」。
    - 🆕 **悬停读数行补 dev / walk**（2026-10-02 用户需求; 文档同 §5）: **第一条**读数行
      （序列那条）在 `t=` 之后加两项, **只在光标在图上时**出现——没悬停时那行仍是操作
      提示, 不常驻数字。`dev = sgn·(spot − anchor)` / `walk = sgn·(twap − anchor)`（美元;
      sgn 取**该秒热门侧**, 与引擎判定同一条路 ⇒ 市场窗内翻边时读数跟着换号, 那是「当时
      看到的量」不是事后按最终赢家重算的）。**不画成线**: 它们是美元差值（几十）而图上是
      价格（十万）, 同轴是两条贴地线头; 而且 `anchor + dev` 在 sgn=+1 时就**是** Spot
      那条线本身（信息冗余）。公式只写一份 = `tail.CurvePoint.DevWalk`（实况
      `cmd/tail.sampleCurve` + 重建 `dashboard.eventCurve` 各调一次, 前端只显示——定号
      规则（有效价 ask 优先 / 平局取 yes）不复制进 JS）。**0 = 读不出来 ⇒ 前端显「—」**
      （与信号表 dev 列同一约定）: 四档全空 / `anchor ≤ 0` / 单项输入缺失（现货超龄只砸
      dev、缺 TWAP 只砸 walk）。「点行看曲线」弹窗图共用同一套绘制 ⇒ 两处都有。
31. **点行看曲线：信号表 / 决策表任意一行 → 弹窗画那一窗的完整曲线**（2026-10-01 用户需求；
    文档 `docs/tail_dashboard_note_2026-09-30.md` §3）。**只动 Dashboard 侧, 判定链一字未改**。
    - **动机 + 障碍**：`curveBuf` 只保**当前一窗**（见 #30 换窗语义），而本族一窗最多下一单
      ⇒ 信号表 50 行里最多 1 行能画, 功能会退化成「只能看刚刚那一笔」。
    - **解法 = 回读 `cmd/tail` 自己的原始采集（决策 #27），不新落任何盘** ⇒ #30 那条
      「曲线不写文件」的红线**不碰**（新增的是**读**）。events 行内**逐秒**含曲线全部输入:
      锚 = 顶层 `twap_open_price`、TWAP/现货 = `ticks[i].twap.price` / `.bin.price`、
      横轴 = `ticks[i].ts`/`rem`。
    - **两条路, 服务端定 `source`**：请求的那一窗**正是内存里这一窗** ⇒ `live`
      （**它才是当时屏幕上那条**, 也含引擎的钳零口径, 比磁盘重建准）; 否则 `events` 重建;
      拿不到 ⇒ `none` + `note`（**HTTP 仍 200**, `note` 是给用户看的原因, 不是错误）。
    - ⚠️ **归日只认窗口起点**: events 文件按 `DayForStart(start_time)` 归日, 而行的 `date`
      是**行 ts** 的 UTC 日——跨午夜的窗两者**差一天**。故 `/api/signals`、`/api/snaps`
      下发 `event_start`（`Record.EventStart`, 本来就有）, 前端**只能**用它定位文件;
      旧口径行（`kind=frame`/`scan`）没有它 ⇒ 弹窗说明「无法定位窗口」, 不猜。
    - ⚠️ **`TieAt` 的契约 = 「`pts` 不含本点」**：实时路径那一刻缓冲里还没追加本点,
      重建路径必须传 `pts[:i]`（传 `pts[:i+1]` 本点算两次）。`TestTieAtExcludesCurrentPoint` 钉住。
    - **实现**（三处新文件 + 一份公式搬家）：`internal/tail/curve.go` 收 `RequiredPrice` /
      `ExtrapPrice` / `TieAt` + `WindowSec` / `TwapLookbackSeconds`（`cmd/tail` 的常量改成
      **别名**, **一份公式两处用**）; `internal/collect/query.go` 的 `LoadEventByStart`
      （子串预筛 `"start_time":N,`——**必须带分隔符**, 否则 1000 命中 10001; scanner 缓冲
      显式提到 16MB, **一行实测 ~210KB, 默认 64KB 直接 token too long**）;
      `internal/dashboard/tail_curve.go` 收 `/api/curve` 的**全部**逻辑（无参数 = 主图那条,
      行为一字未变）+ `TailState.eventsDir`（**只读**）。依赖方向
      `dashboard → collect`（只用标准库, 无环）; **`tail.Snapshotter` 接口没动**（重建在
      handler 层）。
    - **成本实测**: 5.8MB 当日文件扫到目标行 **26ms**（整窗 300 点返回）, 满一天 288 窗
      ≈ 62MB ⇒ 百毫秒级。**不做缓存**——用户点一下的代价, 不值一个索引。
    - **前端**: 单击（**不用 `dblclick`**——双击的第一次 click 也会开, 而 iOS Safari 的双击是
      缩放、`dblclick` 不可靠）; 行挂 `data-es`/`data-ts` + `row-click` 类; **委托**绑在两张表上
      （表体每 15s 整块重渲染, 逐行绑定会全丢）; 弹窗图**不轮询**（冻结的历史窗）;
      **先解锁遮罩再画**（`hidden` 时 `clientWidth === 0`, `drawChart` 直接 return）;
      `Esc` 与遮罩空白点击**两个弹窗都关**。
    - **一条绘制代码两张图**: `makeChart(cvId, roId)` 出实例（`data`/`hover`/`geom`/`mark`/
      `empty`）, `drawChart(ch)` 不再认 `#curveCanvas`——原来是模块级单例
      （`CURVE`/`hoverIdx`/`geom`）, 两张图并存时**悬停下标会串台**。
    - **标记线（`ch.mark`）画在网格之上、五条线之下**（它是**注释**不是序列, 压上去会盖住
      「那一刻价在哪」）, 与悬停准星**同色**、靠**顶部小标签** `t=239s` 区分（准星无标签）,
      **不占第 4 个分类色槽**。
    - **口径差异（弹窗以 `note` 明示）**：重建取**原始值**（events 落 `LatestData()`, 不套引擎
      的陈旧钳零）⇒ 重建线**更完整、锚从第一秒就有**, 看图形足够, **不等于**当时页面上那条。
    - **无数据的四类**（页面长得一样, 一行 `note` 说清是「没采集」而不是「采了画不出」）：
      `events_dir` 为空串 / 那天没文件 / 该窗没被采到（跳窗路径从不产出行）/ 被落盘红线丢弃。
32. **监听段价格地板 `有效价 > 0.83`（2026-10-01 用户决定「落价格地板 >0.83」）**。
    〔⚠️ **范围已被 #33 扩展**（同日下午）：地板现在同时拦 **T=60 段**、配置键改名
    `tail.floor_min_price`。本条余下内容（阈值标定、实盘证据、前向判据）**原文有效**，
    只是「只拦监听段」这个**范围**表述以 #33 为准。〕
    依据 `docs/tail_listen_floor_2026-10-01.md`（§5 落地清单与偏差 / §5 前向判据），
    脚本 `python/v4/41_tail_listen_floor.py`（§0 三把 pin 自检）。
    - **起因 = 实盘**（`data/tail-live` 09-24~09-30，10U/注）：监听段已成交换手 34 笔
      −6.4256U，其中 **`≤0.83` 的 3 笔净 −17.4912U**（0.80 赢 +2.5U；0.81/0.82 两笔输
      −9.9954/−9.9958U，**正是用户当天看到的那两笔**）⇒ 监听段的净亏损 100% 来自这个
      口袋；`>0.83` 的 31 笔零输 +11.0656U。⚠️ 实盘样本只有 3 笔，不定案。
    - **形态 = 只拦监听段**：② 达标 ∧ 有效价 ≤ 0.83 ⇒ 本 tick 不成交、落一行
      `reject_reason = floor_low` 的**影子行**（`ok=false`，**每窗至多一条**）且
      **链继续**（同段等更贵的 tick）。T=150 / T=60 一字不动（段相关的价格腿仍只有
      `PriceLeg` 那一个比较符差异）。**拒绝原因链不含 `floor_low`**——它只出现在影子行，
      判定行的链仍是 `missing_spot → no_hist → price_low → leg_out → walk_low`。
    - **为什么只拦监听段**（回测口径，C43 基线）：廉价角只有在监听段是负的
      （[0.80,0.82] n=15 输率 33.33% −5.40U），而 T=150 n=29 **+5.27U**、T=60 n=9 **+3.98U**
      ⇒ 另外两段是另一族决策（段 1 已被 walk 闸先拦一道、动段 2 = 改检查点族，已全否）。
    - **为什么用价格而不是 walk**：两者同向但**不是一回事**——`ρ(walk, fill) = +0.308`，
      `≤0.82` 里 walk<43 占 87%、`>0.82` 里也占 66% ⇒ 用 walk 拦 = 换把钝刀还多砍 16 倍的笔
      （40 号已否，见决策 #29 的姊妹条）。
    - **钱从哪来**（14 天，C43 基线 +64.026354U）：拦 15 笔 = **4 个窗整窗死亡**
      （08-18 三笔 rem 55/45/38 + 08-19 一笔 rem=7，**全输**，省 **+8.00U**）+ 11 笔同段改道
      （新 fill 中位 **0.87**，改道这一侧反而多赚 0.55U）= **+8.55U**；n 2080→**2076**、
      输单 50→**45**、WR 97.596154%→**97.832370%**、行 6686→**6697**（+15 影子行 −4 信号）。
    - **阈值扫描**（vs C43，日级配对 95%）：>0.80 +3.70U / >0.81 +3.31U / >0.82 +6.94U /
      **>0.83 +8.55U [−0.45, +21.87]** / >0.85 +6.64U / >0.90 +4.07U —— 0.82~0.85 是**平台**
      不是尖峰，但 0.83 仍是这批数据上的 argmax，**所有区间都含零**。⚠️ **没做**搜索校正置换
      （理由：结论就是「不显著、交前向复验」，不做校正也不会变强）。
    - **安慰剂**（随机拦同样 15 笔、语义一致，4000 次）：中位 −0.09U、p95 +0.16U、最大 +2.23U
      ⇒ **按价格挑 p = 0.0002**（nperm=400 时 0.0025）。⚠️ 它证明「选中的这批确实更差」，
      抵消不了「0.83 是扫出来的」。
    - **实现**：纯函数 `FloorLeg(cfg, stage, px)`〔原名 `ListenFloorLeg`，#33 改〕
      （非监听段恒放行）；`engine` 的
      `stateListening` 分支调用 + `floorShadowed` 闩锁（**不上 Resume**——崩溃重启可能重落
      一条影子行，无害）；阈值 = 配置键 **`tail.floor_min_price`（默认 0.83）**〔原名
      `listen_min_price`，#33 改〕，校验
      只加结构性一条 `[0,1)`（`≥1` 让监听段信号静默归零、`<0` 让地板形同虚设）。
    - ⚠️ **影子行的红线**：`ok=false` ⇒ `HasSignal`（防双单）/ `HasStage`（Resume）/
      `isSettlable`（结算）都不认它 ⇒ 不进信号/仓位/熔断/胜率，**也不带 `won` 与 P&L**。
      **复验靠离线 join**：同窗有信号行（11/15）⇒ 由它的 `won` 反推 outcome；整窗死亡
      （4/15）⇒ 读 `runtime.events_dir` 采集行（`condition_id` 对齐，行内含官方 outcome，
      决策 #27/#31 同一套 join）。⚠️ 架构原因：`Recorder.pending` 是**按 conditionID 唯一的
      map**，一窗挂不下两行 ⇒ 不能让影子行参与结算。
    - **前向判据（先写死再跑）**：① 被拦组输率 ≥ 监听段保留组 +20pp（回测 33.3% vs 0.82%）；
      ② 影子行现算的日级配对 Δ 的 95% **下界 > 0**；③ 净 Δ ≥ **+10U / 14 天 @2U**
      （回测现值 +8.55U，**本身没到线**）。触发 = ≥14 完整日 或 ≥10 条 `floor_low` 影子行。
    - ⚠️ **与已推翻家族的关系**：那族挪的是**检查点位置/阈值**（无条件推后）；本条加的是
      监听段的一条**新条件**且不动段 1/2 ⇒ 与 `docs/tail_entry_timing_2026-09-29.md`
      否掉的那批**不冲突**（同 #29 的论证）。
    - ✅ **flip 侧不动**。
33. **价格地板下延到 T=60 段 + 配置键改名 `tail.floor_min_price`（2026-10-01 用户决定
    「把 listen_min_price 提到从 T=60 开始吧，观察实盘……亏就是很严重」）**。
    依据 `docs/tail_floor_from_t60_2026-10-01.md`，脚本 `python/v4/42_tail_floor_from_t60.py`
    （§0 三把 pin 自检 = C43 / #32 / #33）。
    - **形态**：**T=60 段**也要求有效价 **> 0.83** 才成交（⑤ 达标但价 ≤ 地板 ⇒ 落一行
      `floor_low` 影子行、`ok=false`、**链继续**转入监听段）；监听段一字不动；**T=150 段
      一字不动**。闩锁 **`floorShadowed` 两段共用** ⇒ **每窗至多一条**影子行。
      **拒绝原因链（判定行）一字未动**——`floor_low` 仍不在链里（判定行仍是「⑤ 不达标」）。
    - ⚠️ **这是一次「逆着回测」的落地**（唯二之一，另一条是 #26 的算子改动）：14 天
      Δ = **−2.26U**，日级配对 95% **[−4.73, −0.29]**（**区间整体在零下方 ⇒ 显著变差**）。
      落地依据**不是**回测增益，而是**实盘口径**（下条）+ 用户决定。
    - **实盘依据**（`data/tail-live` 09-24~10-01，8 天，10U/注）：**≤0.83 的口袋是全族唯一
      系统性输钱的一格**——三段合计 34 笔 22 赢 12 输 **−68.76U**，而 >0.83 的 516 笔
      **+98.87U**（整族净剩约 +30.1U）。其中 **t60 段 6 笔 4 赢 2 输 −11.38U**
      （09-29 起，样本极薄、09-30~10-01 两天占 5 笔，**不能单独定案**）。
      机制：定价 p 的盈亏平衡胜率就是 p ⇒ `fill ≤ 0.83` 是在赌 17% 以上的不确定性，
      而 10U/注下赢 +2.0~2.5U / 输 −10U = **4:1 盈亏比**，「亏就是很严重」有一半来自 stake。
    - **钱从哪来（回测口径，9 笔 t60 影子行）**：**2 个整窗死亡**（08-20/08-25 各一）——
      那两笔在 #32 世界里**都是赢单**（+0.88U，地板把它们省掉了 = 负贡献）+ **7 笔同段改道**
      （新 fill 中位 **0.87** vs 旧 0.82；这批窗 +3.11U → +1.72U，**改道税 −1.39U**）
      ⇒ 回测合计 **−0.51U**（与 Δ −2.26U 的差额落在被影子行挪位的其它窗）。
    - ⚠️ **与「闸往 T60 延」（已否，`docs/tail_walk_gate_extension_2026-10-01.md`）完全同形**：
      两者在 T=60 段都是「砍的全是赢单」、Δ 都是负的。**差别不在统计上而在依据上**——
      那次用的是 walk（价格腿的钝刀伪装，ρ(walk,fill)=+0.31），这次用的是**价格本身**；
      那次结论「不显著 ⇒ 不动」，这次是**用户按实盘口径决定「宁可不买也不赌这一格」**。
    - **实现面**：`decide.FloorLeg(cfg, stage, px)`〔原 `ListenFloorLeg` 改名〕——
      `stage ∈ {t60, listen}` 时 `px > cfg.FloorMinPrice`，T=150 恒放行；`engine` 的
      `stateAwait60` 分支在 `decision()` 之后加一步 `o.OK && !FloorLeg(...)` ⇒
      `OK=false / Shares=0 / reject_reason=floor_low / floorShadowed=true`（**状态已转入
      监听段 ⇒ 链自然继续**）；`HasStage(cid,"t60")` 对 t60 影子行为真（该段**确实已判过**，
      续跑语义正确）。配置键 `tail.listen_min_price` → **`tail.floor_min_price`**
      （默认 0.83 不变；`/api/config`、启动横幅、前端 `REJECT_CN.floor_low`「地板不过」同步）。
      ⚠️ **改名 = 破坏性变更**：`UnmarshalExact` 让**旧键名 = 启动失败** ⇒
      `v4.config.yaml`（漂移守卫）与**服务器那份手工维护的 live 配置**都必须同步改名，
      否则部署即起不来。
    - **新 pin**（oracle 23 同日更新）：行 6697→**6704**、信号 2076→**2074**、WR
      97.832370%→**97.830280%**、P&L +72.578473→**+70.314372U**、`floor_low` 15→**24** 行
      （t60 段 9 + 监听段 15）；分段：t150 3638/958/96.242171/+42.173827（不变）、
      t60 2676/**741**/99.190283/**+15.082833**、listen **390**/**375**/99.200000/**+13.057712**。
      窗数 3640、`no_sigma` 3、`walk_low` 250 均不变。
    - **前向判据（先写死再跑）**：① 被拦组（**t60 段** `floor_low` 影子行那批）输率 ≥
      t60 保留组 +20pp；② 影子行现算的日级配对 Δ 的 95% **下界 > 0**；③ 净 Δ ≥
      **+10U / 14 天 @2U**（⚠️ 回测现值 **−2.26U**，**离这条线最远**——前向要证明的正是
      「实盘口径下它省钱」，回测永远给不出这个答案）。触发 = **≥14 完整日**（自 2026-10-01 起）
      或 **≥10 条 t60 段影子行**（先到为准）；任一不满足 ⇒ 撤回（保留记录行、关掉 t60 那一半）。
    - ⚠️ **风险**：① 实盘 t60 段只有 6 笔（09-30~10-01 占 5 笔），「系统性输钱」在 t60 段上
      **还没有统计支撑**，支撑它的是三段合计 34 笔的形状；② **10U/注放大了形状**——
      真按风险预算看应同时评估 stake 而不是只加地板；③ 改道价中位 0.99 与
      `minimum_order_size = 5 股` 的交互（同 #29）——**本改动的经济学依赖改道能成交**。
    - ⚠️ **与已推翻家族的关系**：那族挪的是**检查点位置/阈值**（无条件推后）；本条加的是
      T=60 段的一条**新价格条件**、不动检查点、不动段 1 ⇒ 同 #29/#32 的论证，不冲突。
      #26 的「段相关腿只此一条」自 #29 起已不成立；本条再添一条（`FloorLeg` 的段集合
      从 `{listen}` 变成 `{t60, listen}`）。
    - ✅ **flip 侧不动**。

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
# **兼作数据采集器**: 默认把每秒原始采样按 v2 格式落到 runtime.events_dir（data/events）
go run ./cmd/tail -config v4.config.yaml -dashboard :8091
go run ./cmd/tail -config config.local.yaml -stake 2 -mode paper -dashboard ""
```

采集关闭只认一个开关: 配置文件 `runtime.events_dir: ""`（没有独立 flag）。
启动横幅打印 `[Events] 📥 原始采集开启: <目录>`；每窗收尾打印
`[Events] 📥 窗口 <slug> 采集完成: ticks=… trades=… close=…(<src>) outcome=…`，
被红线拦下则打印 `[Events] ⚠️ 窗口 <slug> 不落盘: <原因>`（丢窗必须有痕，见决策 #27）。

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
| `runtime.events_dir` | `data/events` | **cmd/tail 的原始采集目录**（数据格式 v2，与 `cmd/collect` 同构）。留空串 = 关闭；一标的一目录（见决策 #27） |
| `tail.walk_min_usd` | 43 | **T=150 段入场闸**阈值：`walk = sgn·(twap − anchor) ≥ 此值`（决策 #29）。⚠️ **BTC 标定量**——键是为 ETH 等标的重标定而存在，不是让你在 BTC 上调（见项目概述）|
| `tail.floor_min_price` | 0.83 | **价格地板**：**T=60 段与监听段**的有效价**严格大于**此值才成交（决策 #32 + #33；原名 `listen_min_price`）。⚠️ **BTC 标定量**，同上一行同理 |
| `tail.*`（其余 6 键）| — | 扫尾盘 6 键——⚠️ **BTC 上全部不可调**（见项目概述）|

启动校验（`internal/config/validate.go`，判**最终生效值**）：三阈值必须 > 0、
`risk.max_daily_loss` 必须 < 0、`runtime.mode ∈ {paper, live}`、`flip.stake > 0`、
`runtime.output_dir` 非空、`tail.floor_min_price ∈ [0,1)` —— 任一不满足即启动失败；
`flip.max_book_lat_ms < 100` 只告警。
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
