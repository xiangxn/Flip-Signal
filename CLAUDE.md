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
- 已证伪：flip「自信崩溃」家族（v3，分支 v3 保留）、v1/v2 follow/wait 族、0.2 深度
  全市场扫、双层版单独 TWAP 腿等——历史分析/代码在 git 其他分支可查。

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
├── cmd/flip/                         # 策略引擎主入口（纸面/实盘同源，-mode 切换）
│   └── main.go                       # 窗口循环/数据源接线/anchor σ/执行编排注入（唯一文件; 4 个 flag, 配置见 internal/config）
├── cmd/btreplay/                     # 逐笔重放 data/btc 驱动 flip.Engine（Go↔py 口径对账红线）
├── internal/
│   ├── config/                       # 配置层（viper 三层加载 + 敏感字段 AES 解密 + 启动校验）
│   │   ├── config.go                 # AppConfig + defaults()（唯一默认值来源）+ Load()
│   │   ├── decrypt.go                # owner_key/clob_creds 密文解密（PM_CONFIG_DECRYPT_PASSWORD）
│   │   ├── validate.go               # 校验（fatal/warning），在 CLI 覆盖之后调用
│   │   └── configfile_drift_test.go  # v4.config.yaml 漂移守卫
│   ├── flip/                         # 引擎核心层（零外部依赖, 可独立测试）
│   │   ├── types.go                  # Config + Tick/Observation/Record + 状态枚举 + 闸原因常量
│   │   ├── engine.go                 # 状态机: Watching → Done（触底观测/四腿判定 + 本窗 tick 健康度计数）
│   │   ├── exec.go                   # Executor 接口 + PaperExecutor（live 实现由 trading 注入）
│   │   ├── exec_state.go             # ExecState 编排: 风控闸（两模式共用）→ paper 单步 / live submitting 两阶段 → 统一 Execute
│   │   ├── recorder.go               # JSONL 观测记录 + windows_* 窗口振幅日志 + winstats_* 健康度（按日切分）+ P&L 回填
│   │   ├── risk.go                   # CanTrade 日亏熔断判定（纯函数; 锁存在 exec_state.breakerTripped）
│   │   ├── sigma.go                  # HistState（σ 滚动窗）+ RecentBlock 截断纯函数 + AnchorUsableAtBoundary
│   │   ├── snapshot.go               # LiveSnapshot/LiveExec + Snapshotter 接口（dashboard 只读消费）
│   │   └── *_test.go                 # engine/recorder/exec_state/risk/anchor/preheat + mirrorcheck 镜像回归
│   ├── dashboard/
│   │   ├── server.go                 # HTTP server（go:embed static/）
│   │   ├── handlers.go               # /api/state, /api/observations, /api/signals, /api/config
│   │   ├── state.go                  # 运行时组件引用
│   │   └── static/                   # index.html, app.js, style.css（兼容手机浏览器）
│   ├── feed/
│   │   ├── binance_adapter.go        # Binance BTCUSDT WS（spot 浅洞输入, 本地接收龄）
│   │   ├── pmtick.go                 # PM 盘口采样（best bid/ask 陷阱）+ token 解析（原 cmd/flip 下沉）
│   │   └── twap_adapter.go           # Chainlink TWAP-60（anchor/σ）+ FetchTwapRanges 预热
│   └── trading/                      # SDK 依赖层（单向依赖 flip/feed, 由 cmd/flip 构造注入）
│       ├── live_executor.go          # LiveExecutor 真实 FAK 下单（实现 flip.Executor, 唯一 POST 点）
│       └── resolution_poller.go      # 官方结算轮询（gamma umaResolutionStatus）
├── docs/                             # 策略文档（v4 方案/口径映射）
├── python/
│   ├── v2/lib.py                     # 数据加载器（v4 回测脚本依赖，保留）
│   └── v4/                           # 回测权威脚本 + 纸面对账/复验/健康度脚本（01/02/06/07）
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
    UP/DOWN 盘口 1s        边界 TWAP 值/龄           最后价 + 本地接收龄(>2s 判 stale)
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
             Executor (PaperExecutor 模拟成交; live = LiveExecutor FAK)
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
2. 边界对齐 → 注入窗口上下文（anchor=TWAP 流值、σ）→ 订阅 UP/DOWN token
3. 每秒 1s tick：读 UP/DOWN 盘口 + Binance spot + TWAP → ProcessTick(状态机)
4. 首个触底 tick（ask≤0.20）→ 四腿判定 → 观测落盘（ok 与失败都记，即时落盘）
5. ok 信号 → 风控闸（live 命中拦 POST 记 rejected；paper 命中记 gate_reason 照常结算）
   → PaperExecutor 执行 → Register 结算轮询（窗口内完成，无窗末补判）
6. 窗口结束（rem=0）→ |close−anchor| 追加进 σ 滚动窗并落盘 windows_*.jsonl
   （重启 σ 预热本地优先：windows_* 新鲜即毫秒级恢复，不足/过旧回退官方网络
   预热 FetchTwapRanges——停机期窗口只有官方能取）；同刻本窗 tick 健康度落盘
   winstats_*.jsonl（含被延迟闸挡掉的丢信号明细）→ 下一窗口
```

### 引擎状态机

```
Watching ──首个触底观测(ask≤0.20, 四腿判定)──▶ Done
   ▲                                        │
   └────────── 窗口结束(rem==0) ◀───────────┘
```

- **Watching**: 1s tick 更新两侧状态；锚缺失窗口（anchor≤0，窗口级）整窗不观测
  （镜像回测 :69 锚缺失事件跳过）；有效 tick（latency≤300 且 UP/DOWN 双侧报价齐全
  ——整簿快照门控，实测缺失为整行全空）上检查 up/down ask 是否 ≤0.20
- **判定顺序**（一次完成）：rem_low → no_hist → missing_spot → no_crash → dist_out
  （missing_anchor 现网不可达，仅 decide 纯函数防线）；全过 → ok（shares = stake/fill）
- **Done**: 事件内不再检测（与回测每事件仅首个观测一致，无 fallback 重试）
- 数据质量：无效 tick 压 0 占槽（不进触发检查，不贡献急跌窗 max）

---

## 依赖库

| 库 | 用途 |
|----|------|
| `github.com/xiangxn/go-polymarket-sdk` | Polymarket REST/WS 客户端 |
| `github.com/gorilla/websocket` | Binance WebSocket 连接（feed adapter） |
| `github.com/tidwall/gjson` | JSON 解析（SDK 依赖）|
| `github.com/spf13/viper` | 配置文件加载（internal/config，同 master 分支）|
| `golang.org/x/term` | 解密密码无回显终端输入（nohup 场景走环境变量，不走它）|

### 本地开发 replace 指令

```
replace (
    github.com/xiangxn/go-polymarket-sdk => /tmp/go-polymarket-sdk
)
```

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
go test ./internal/... -v              # 引擎/编排/记录器/盘口工具/结算轮询/配置（含 YAML 漂移守卫）
go build ./...                         # 全量编译检查
go run ./cmd/flip -config v4.config.yaml -dashboard :8090   # 运行引擎 + Dashboard

# python 分析/回测脚本一律用项目内 venv（系统 python3 无 numpy/pandas）
python/venv/bin/python python/v4/01_backtest_r1.py
python/venv/bin/python python/v4/06_oos_review.py    # 09-15 复验裁判（纯标准库）
python/venv/bin/python python/v4/07_source_health_check.py            # 数据源健康度审计（纯标准库）
python/venv/bin/python python/v4/07_source_health_check.py --bt-scan  # + book 阈值扫描/零成交代理
```

---

## 关键设计决策

1. **纸面/实盘同源**：成交执行抽象为 `Executor` 接口，`mode: paper|live` 配置区分。
   阶段一仅 `PaperExecutor`（校验 fill>0，无其它边界）；live（FAK）接口与配置已预留，
   纸面验证通过后从 eth 分支历史恢复实盘路径接入。
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
   tick 上"本会触发"的 tick 落 `winstats_YYYY-MM-DD.jsonl`（每窗一行，含
   `ticks/ticks_valid/book_stale/book_missing/lost_triggers` 明细与恒等式），触底
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
   配置包 `internal/config`（viper，同 master），`main()` 只留 4 个 flag。
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
   - **敏感字段**（`sdk.polymarket.owner_key`/`clob_creds`）沿用 master：非空即密文
     （AES-256-CBC, key=SHA256(密码)），密码走 `PM_CONFIG_DECRYPT_PASSWORD` 或终端
     无回显输入；四项全空则**不弹密码**（纸面运行永不卡在输入）。
     `POLYMARKET_*` 环境变量已**全部废弃**，凭证只能来自配置文件。
   - `v4.config.yaml` = 默认值镜像（入 git），有漂移守卫测试（逐键 `DeepEqual`
     `defaults()` + 覆盖度检查）；调参复制成 `config.local.yaml`（gitignored）。
   - ⚠️ `sdk.http_timeout: 10s` **必须带单位**（写 `10` = 10 纳秒）；
     别用 `sdk.DefaultConfig()` 当默认值（它设 3 次/500ms 退避，会静默改掉
     v4 现行的 6 次/1000ms 内建兜底）。

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
```

### CLI flag（main() 只有这 4 个）

| flag | 默认 | 含义 |
|------|------|------|
| `-config` | `""` | 配置文件路径。**空 = 不读任何文件**，直接用代码默认值 |
| `-dashboard` | `""` | 覆盖 `runtime.dashboard_addr`；显式传空串 = 本次不开 Dashboard |
| `-mode` | `""` | 覆盖 `runtime.mode`（paper\|live）|
| `-stake` | `0` | 覆盖 `flip.stake`；**没给**则用配置值，显式给 0 会校验报错 |

用 `flag.Visit` 区分「没给」与「显式给空/0」——所以 `-dashboard ""` 是有效的关闭操作，
不是"用默认值"。其余全部参数见 `v4.config.yaml`（全量带注释），主要几项：

| 配置键 | 默认 | 含义 |
|------|------|------|
| `flip.max_book_lat_ms` | 300 | PM 盘口延迟闸：`book_latency_ms` 超此值的 tick 无效（**回测 `MAX_LAT` 同值，收紧是负收益**，见 `docs/dog020_risk_latency_plan_2026-09-16.md` §1.2b） |
| `feed.max_spot_age_ms` | 2000 | Binance spot 新鲜度：距本地接收超此值判现货缺失（`missing_spot`） |
| `feed.max_twap_age_ms` | 10000 | TWAP-60 新鲜度：窗口起 anchor 与窗末 close 共用，超龄按缺失处理 |
| `risk.max_daily_loss` | **−24** | 日亏熔断线（负值）：当日（UTC）已结算 P&L ≤ 此值即当日停单并锁存；两模式同源（paper 只标记不拦单） |
| `runtime.output_dir` | `data/v4` | 观测 JSONL 输出目录（live 建议独立目录，见启动时的 paper/live 混行告警） |
| `runtime.slug_prefix` | `btc-updown-5m` | 市场 slug 前缀 |

启动校验（`internal/config/validate.go`，判**最终生效值**）：三阈值必须 > 0、
`risk.max_daily_loss` 必须 < 0、`runtime.mode ∈ {paper, live}`、`flip.stake > 0`、
`runtime.output_dir` 非空 —— 任一不满足即启动失败；`flip.max_book_lat_ms < 100` 只告警。
配置文件里拼错的键（`UnmarshalExact`）也是启动失败，不静默回落默认值。

### 敏感字段（`sdk.polymarket.*`）

凭证**只能来自配置文件**（`POLYMARKET_*` 环境变量已不再读取）：`owner_key` /
`clob_creds.{key,secret,passphrase}` 留空 = 只读运行（引擎自动生成临时密钥跑纸面）；
填 **密文**（`pmutils.NewEncryptor(密码).Encrypt(明文)`，AES-256-CBC）则实盘可用。
判定语义是**非空即密文**——明文写进去会在解密时启动失败（没有"看起来像明文"的兜底）。

| 变量 | 说明 | 必填 |
|------|------|------|
| `PM_CONFIG_DECRYPT_PASSWORD` | 密文凭证的解密密码；不设则终端无回显输入（**nohup/systemd 无终端 → 必须设它**） | 仅当配置文件里有密文 |

> 本地运行记得 `export https_proxy=http://127.0.0.1:1087`（Polymarket 直连超时；这是
> Go 标准库 `http.ProxyFromEnvironment` 读的，与上面的配置系统无关）；
> 部署机勿设指向不通代理的 HTTP(S)_PROXY（Binance 拨号走环境代理）。
