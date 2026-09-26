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
- **结算**（2026-09-24 起三层回退，决策 #19）：官方 outcome（0=Up 1=Down）——
  ① 边界推送自算（闭市 +10s，与官方逐位同源）→ ② 官方 crypto-price 接口（+45s）→
  ③ gamma `umaResolutionStatus=="resolved"` 轮询；每行落 `settle_src`
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
- 🚫 **已推翻的策略调整（只此一句）**：flip 的 dist_t 第二层、浅洞带加宽/平移 k/网格重选、
  σ 窗 18→10、趋势条件带、rem 分桶、binance_open 换锚、Zcritical、v6 波动率×成交量，
  与 tail 的低σ放宽 dev 全部候选（`docs/tail_sigma_gradient_2026-09-26.md`）
  ——细节见记忆与 git，**不得重提**（除非先有新样本）。

### 第二条策略线：「扫尾盘」⑤（2026-09-23 落引擎；2026-09-24 三段链整合，与狗@0.2 独立并行）
- **现行权威规格 = `docs/tail_integrated_2026-09-24.md`**（2026-09-24 起；规则 ⑤ 本体仍见
  `docs/tail_sweep_2026-09-22.md` §1），口径映射/运行说明见
  `docs/tail_engine_mapping_2026-09-23.md`，实现 `cmd/tail/` + `internal/tail/`，
  oracle = `python/v4/23_tail_integrated.py`（parity 测试钉它）。
- **逻辑（方向与狗@0.2 相反：买热门侧）= 三段递进判定链**（a.md 第 1 条，任一段出信号即
  整窗只下一单）：**T=150 判 ⑤** → 不达标则 **T=60 再判 ⑤** → 仍不达标则**此后每秒判 ②**
  （②= 价格腿 ∧ `dev ≥ 63`，不含 σ 腿）。
  ⑤ = 热门侧**有效价**过 0.80（⚠️ **T=150 段严格大于**，T=60 与监听段 ≥，2026-09-26 起，
  见决策 #26）∧（`dev ≥ 63 美元` ∨（`sd ≥ 40 美元` ∧ `dev ≥ sd`））；
  `dev = sgn·(spot − anchor)`（美元，正 = 朝押注方向，sgn: 押 yes +1 / no −1）；
  `sd = hist_bps × anchor / 1e4`（该窗 1σ 折美元）。
- **回测基准（14 天，2U/注，oracle；2026-09-26 改严格大于之后）**：T=150 n=1208 WR 94.04%
  +15.02U / T=60 n=577 WR 99.13% +16.15U / 监听 n=348 WR 97.99% +3.92U ⇒ **合计 n=2133
  WR 96.06% +35.09U**（判定行 6412 = 3638+2426+348；参与判定 3640 窗；σ 未就绪整窗跳过 3 窗）。
  改前（T=150 用 ≥）: n=2135 WR 95.97% +35.67U / 6399 行——**配对 Δ −0.58U, CI 含 0**。
  对照旧口径「只在 T=60 判 ⑤」n=1536 WR 99.61% +36.93U ⇒ **总 P&L 基本持平、注数 +39%、
  EV/注摊薄**（T=150 段仅 +0.0131U/注）——a.md 的显式取舍，**不调参**。
- ⚠️ **参数全部不可调**：`40` 这个门槛在 2000 次日期重采样里一次都没成为最优
  （真 argmax=35）——不许调参，只纸面登记。（三段链的两条时间腿同样不可调：离线只能复算
  150/60 两个值 + 60 之后的逐秒 ②。）
- 🟡 **状态：纸面登记中**，**未上实盘**（live 是 GTC 挂单等成交，成交样本天然偏向
  「热门侧走弱」，与回测不是同一个估计量）。
  ⚠️ 判决卡（`/api/judge` + Go 侧 `internal/tail/judge.go`/`mt19937.go`）**已随整合改造整删**，
  纸面判决改由**离线脚本**做（吃 `tail_*.jsonl` 的信号行）——**本次未写**（待办 1）。
  （注：`data/tail-live/` 09-24~25 的行带 `order_id` / `exec_status=filled` / `stake=10` / 真实
  `cost`——已是**真实挂单成交样本**，上面这条「未上实盘」需按它更新。）
- 🧭 **2026-09-26 追加：T=150 段价格腿改严格大于**（`hot > 0.80`，决策 #26，见
  `docs/tail_integrated_2026-09-24.md` §6）。**只有 T=150 段改**，T=60 的 ⑤ 与监听段 ② 仍是 ≥。
  依据：0.80 这一格在两个样本里都是**唯一负 EV 档**——实盘 09-24~25 那 4 笔（全在 t150 段）
  WR 50% **−14.38U**，而同批 >0.80 的 201 笔 WR 98.51% +70.15U；14 天回测 0.80 桶 n=17
  WR 76.47% −1.50U（梯度上唯一负档）。⚠️ 两个样本都薄（4 / 17 笔）；14 天重跑后合计
  n=2135→**2133**、P&L +35.67→**+35.09U**、WR 95.97→**96.06%**、行数 6399→**6412**
  （12 笔 0.80 的 t150 信号被拦下 → 9 笔在 t60 段以更高价重入 + 1 笔落监听段 ⇒ 净 −2 笔），
  **配对 Δ = −0.58U，95% 区间 [−6.26, +5.59] 含 0 = 与基线不可区分**——买的是「去掉唯一负 EV 档」
  的口径干净，**不是** P&L 增益。比较符**故意不做成配置键**（`price_min` 只给阈值，
  算子由段决定，见 `internal/tail.PriceLeg`）。
- 🧭 **2026-09-24 整合改造（a.md 四块，决策 #22）**：三段链 + 空侧 ask 优先/bid 兜底 +
  **所有信号都注册结算**（未成交行照显官方结果、P&L 恒 0）+ Dashboard 大改。
- 🧭 **2026-09-23 → 2026-09-24 作废项**：`kind=frame`/`kind=scan` 两行类型不再产出
  （legacy-only，`isKnownKind` 仍认）；监听段从「只记录的对账行」升格为**策略本体**
  （真下单）⇒ **A/B 之分消解**，`python/v4/18_tail_scan_register.py` **作废**；
  旧「两帧折中」（rem≤150 只记录的帧 + rem≤60 快照）与「被闸行按方案 A 照算持仓」
  在 tail 侧一并作废（被闸 = 未成交）。
- 🧭 **2026-09-24 追加：尾盘没有对手方 + 交易所下限**（实盘活体取证，见
  `docs/book_empty_ask_2026-09-24.md` 与决策 #21）。**赢家侧的 ask 侧在尾盘被整侧撤空**
  ——空侧起始 rem **不固定**：探针四窗 12/28/45/47（在 rem≤60 **之后** ⇒ 快照那一刻的
  卖单确实存在，样本 0.90×23 股 / 0.93×135 股，但 GTC 挂上去只有 ~10-15s 成交窗口），
  而修后实盘复核的两窗是 **68/112**（在 rem≤60 **之前** ⇒ 快照闩锁从未推进，一行不产；
  旧代码这里会拿冻结簿产一条买不到的 0.95~0.97 纸面信号）。比例随当日趋势摆动
  （行情一边倒时 rem≈130 就已 0.95+）。更硬的一条：CLOB `minimum_order_size = 5 股`，
  而 `tail.stake=2U` 在 ≥0.80 只有 **2.02~2.5 股** ⇒ **低于交易所下限**（flip 的
  2U@0.20 = 10 股不受影响）。⇒ **tail 上 live 前必须先解决 stake ≥ 5U**（每笔风险 2.5 倍，
  用户决定）+ 拿 1 笔小单验证下限；纸面 WR 99.6% 不能直接外推到 live。
- 🧭 **2026-09-25 追加：持仓止损评估 + 持仓监察**（`docs/tail_stoploss_2026-09-25.md`，
  决策 #24）。离线支持加止损（Δ **+10.44U / +29.3%**，配对 CI `[+2.24, +19.59]`；
  杀赢 5 / 救输 63 = 12.6:1 vs 临界 6.2:1），但**能不能成交是离线答不了的坎** ⇒
  **止损腿未落引擎**；本次只落**持仓监察**（`internal/tail/hold.go` + 第四族
  `tailhold_*.jsonl`，只记录不判定、门控刻意放宽到不要求持仓侧 bid > 0），跑一两周后再判。
  用户提的「砸盘那一刻量比」**已否**（止损那一档 AUC 0.482 = 噪声）。
- 🧭 **2026-09-25 追加：ETH 服务器数据复核 + 扫尾盘阈值重标定待办**
  （`docs/eth_data_review_2026-09-25.md`，决策 #25）。首批服务器数据 23 窗（2 小时）
  证实两件事：① **尾盘整侧撤空在服务器数据上复现**（30 段 / **21.5%** 的 tick，
  30 段里 29 段的起点热门侧已 ≥0.96 ⇒ 与市场定局精确耦合，**本机网络假设排除**）；
  ② **扫尾盘的 `dev_min_usd 63` / `sigma_min_usd 40` 是 BTC 价格的标定量**——
  ETH 的 σ 中位 5.36 美元 vs BTC 62.56 美元 ⇒ 两个分支都不可达，**量纲错了，不是参数偏**。
  ⇒ **等 ETH 采集 ≥14 个完整日后单独立项做阈值重标定**（判据/决策表已预先写在 doc §5，
  触发前不做任何阈值分析）；那件事的**第一步是「可成交性先行」**——首批实测
  `rem≤60` 且热门侧 ≥0.80 的 tick 里 **82.6% 热门侧没有卖单**（2 小时样本，仅指示性），
  可成交率 < 50% 就直接判「ETH 不可执行」，不进入调参。

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
├── cmd/tail/                         # 扫尾盘 ⑤ 引擎主入口（独立进程, 自带 Dashboard; -config/-mode/-stake/-dashboard 四 flag）
│   └── main.go                       # 同上接线, 但三段判定链/尾盘闸/GTC 挂到闭市（决策 #17）+ 窗口运行时载体（决策 #18）
├── cmd/collect/                      # 数据格式 v2 高频采集器（**任意标的**; 2026-09-25 从 eth 分支同步, 决策 #23）
│   └── main.go                       # 6 flag: -config/-asset/-slug/-symbol/-output/-custom-feature；资产由 feed.Asset 派生
├── cmd/compact/                      # 采集数据的合并修正（把 settlement_correction 行并回事件行; 与标的无关）
├── cmd/btreplay/                     # 逐笔重放 data/btc 驱动 flip.Engine（Go↔py 口径对账红线）
├── cmd/twapprobe/                    # 边界对齐探针（一次性的实盘逐秒 TWAP 推送采集, 产 data/probe/; 决策 #19 的证据工具, 运行期不参与引擎）
├── cmd/bookprobe/                    # 空侧盘口探针（一次性: 引擎同一条 SDK WS 逐秒记 raw/eng 双视角, 产 data/probe/book_*.jsonl; 决策 #21 的证据工具, 运行期不参与引擎）
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
│   ├── dashboard/                    # 两族各自的 listener + 各自的静态目录（单包, 按族分文件; 决策 #18）
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
│   │   ├── asset.go                  # 资产参数（btc → slug/Binance 交易对/Chainlink 符号/目录; 消除硬编码, 决策 #23）
│   │   ├── anchor_recover.go         # 取锚通道（精确命中边界那一秒的推送, 500ms×40 重试；官方 open HTTP 段保留但休眠）
│   │   ├── binance_adapter.go        # Binance spot WS（交易对由资产派生; 本地接收龄）
│   │   ├── official_pair.go          # 官方 open+close 取数闭包（PricePairFetcher, 结算第三层用; 决策 #19）
│   │   ├── pmtick.go                 # PM 盘口采样（best bid/ask 陷阱）+ token 解析（原 cmd/flip 下沉）
│   │   └── twap_adapter.go           # Chainlink TWAP-60（anchor/σ）+ FetchTwapRanges 预热 + 推送缓存/PushNearest(精确)/CacheStat
│   ├── collect/                      # 采集核心层（数据格式 v2 结构 + 成交分桶 + 修正队列; 决策 #23）
│   │   ├── types.go                  # Event/HFTick/BinTick/PMTick/TwapTick/TradeAgg + 来源常量（push|official|stream）
│   │   ├── settle.go                 # SettlementWorker: 只服务推送缺失窗, 官方 open+close 轮询纠正
│   │   ├── trade_bucketer.go         # PM last_trade_price 按 token×秒聚合（OFI/max单/vwap）
│   │   ├── dedupe.go                 # transaction_hash 窗口内去重（防 WS 重连重放）
│   │   ├── compact.go                # 修正行合并进事件行（cmd/compact 的逻辑）
│   │   └── book_utils.go / market_utils.go / *_test.go
│   ├── settle/                       # 结算编排（零外部依赖, 不 import flip; 两族共用; 决策 #19）
│   │   ├── settle.go                 # Outcome/Anchors（按边界秒存推送）+ Resolver 三层回退（push +10s → official +45s → gamma）
│   │   └── settle_test.go            # 判定词表钉在 flip 常量上 + 三层时点/次数/幂等/剪枝
│   ├── tail/                         # 扫尾盘 ⑤ 引擎核心层（零外部依赖; 只复用 flip 的原语, 反向不依赖）
│   │   ├── config.go                 # Config + DefaultConfig()（7 个键, 全部不可调; 见决策 #17/#22）
│   │   ├── types.go                  # Observation/Rules/Record/WindowStats + stage/kind/闸原因常量 + 状态机 + IsFilled/HasPosition
│   │   ├── decide.go                 # 纯函数 HotBook（ask 优先/bid 兜底）/ SgnFor / DevUSD / SigmaUSD / EvalRules + Rule1()…Rule5()
│   │   ├── engine.go                 # 三段递进判定链（Watching→Await60→Listening→Done）+ Resume 崩溃续跑, ProcessTick 返回 0~1 行
│   │   ├── recorder.go               # tail_* / tailwin_* / tailstats_* / tailhold_* 四前缀（独立于 flip 三前缀, 决策 #9 红线）+ recomputePnL
│   │   ├── hold.go                   # 持仓监察（纯函数 HoldWatchRow + HoldRow）: 信号成交后逐 tick 记持仓侧盘口, **只记录不判定**（决策 #24）
│   │   ├── exec_state.go             # 风控闸 + 两模式两阶段下单编排（flip.ExecState 的精简镜像; HandleDecision 单入口）
│   │   ├── snapshot.go               # LiveSnapshot/LiveExec + Snapshotter 接口（dashboard 只读消费; 决策 #18）
│   │   └── *_test.go                 # decide（含 HotBook）/engine/recorder/exec_state/hold/parity（opt-in, 钉 23 的 oracle）
│   └── trading/                      # SDK 依赖层（单向依赖 flip/feed, 由 cmd/flip 与 cmd/tail 各自构造注入）
│       ├── live_executor.go          # LiveExecutor 真实 GTC 限价挂单（实现 flip.Executor, 唯一 POST 点）
│       ├── fill_tracker.go           # GTC 挂单跟踪: rem≤RemMin（flip）/ 闭市（tail）撤单 + 查 size_matched 定稿回调（决策 #16/#17）
│       ├── prefetch.go               # 每窗预热 tickSize/negRisk/feeRate（下单路径零额外网调）
│       └── resolution_poller.go      # gamma 结算轮询（umaResolutionStatus; 决策 #19 后降为结算第三层）
├── docs/                             # 策略文档（v4 方案/口径映射; 扫尾盘见 tail_integrated_2026-09-24.md（现行权威）/ tail_sweep_*/tail_engine_mapping_*/tail_stoploss_2026-09-25.md; 结算见 settle_self_2026-09-24.md; 空侧盘口见 book_empty_ask_2026-09-24.md; 采集见 collect_eth_sync_2026-09-25.md）
├── python/
│   ├── v2/lib.py                     # 数据加载器（v4 回测脚本依赖，保留）
│   └── v4/                           # 回测权威脚本 + 纸面对账/复验/健康度脚本（01/02/06/07）+ 扫尾盘（13/14/15/16/17/18/23, 23 = 现行 oracle）+ 边界价探针（19/20, 决策 #19）+ 活体盘口探针/空侧审计（21/22, 决策 #21）+ 采集数据校验（24, 任意标的）+ 止损评估（25 = 评估本体 / 26 = 三段链+止损腿, 23 的拷贝 / 27 = 成交量专题, 决策 #24）
├── v4.config.yaml                    # 全量配置示例（= 代码默认值, 有漂移守卫测试; 调参请复制成 config.local.yaml）
├── go.mod / go.sum
└── CLAUDE.md                         # 本文件
```

**已删除（v4 清理，v3 分支保留可复活）**：internal/feed/orderbook_adapter.go；
twap_adapter 的 PollOfficialOpen/ClosePrice；python/v3、docs 三份 2026-08-31 flip 文档。
internal/collect/writer.go（JSONLWriter 无调用者，2026-09-25 同步时删）。

⚠️ **cmd/collect + cmd/compact + internal/collect 2026-09-25 已从 eth 分支回归**
（数据格式 v2 采集管线，见决策 #23）——上面这条曾写着「采集管线整体移除」，
现只对引擎侧成立：引擎所需盘口采样/token 解析仍在 internal/feed/pmtick.go。

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
   查询, **到 rem ≤ RemMin 撤掉未成交余量并定稿**）→ **定稿后**该行进 pending,
   由 settle.Resolver 按三层回退结算（决策 #19: push +10s → official +45s → gamma）
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
  **缺 spot 的 tick 只跳过、不推进任何段**（等下一个可判定 tick）。
- **任一段出信号即整窗只下一单**；两个判定段**成功与否都落一行**，监听段**只在达标时落行**
  ⇒ `tail_*` 每窗 ≤3 行（全部 `kind=snap`，按 `stage` 分 t150/t60/listen）。
  行类型 `kind=frame`/`kind=scan` 为 legacy-only（`isKnownKind` 仍认历史旧行）。
- 锚在产出任何行之前已定局：取锚通道 +20s 结束（`rem≈280`），最早的行在
  `rem≤150`（`+150s`）——`anchor ≤ 0` ⇒ 本窗一行不产出（只在 `tailstats_*` 记
  `anchor_exact=false`）；首行落盘即冻结，`UpgradeAnchor` 此后拒收（三段同锚）。
- **崩溃重启续跑**：`Engine.Resume(t150Done, t60Done)` 按磁盘真相回填已完成段
  （已判过的段不重判）；重入判据 = 该窗**是否已有 OK 行**（`HasSignal`，有则整窗跳过，
  防同窗双单）。
- σ 未就绪（`hist.Count() < 3`）⇒ 整窗跳过 `skip=no_sigma`；`tailwin_*` 是 tail
  自己的 σ 预热源（独立于 `windows_*`，不交叉读写）。
- **结算 = 所有信号**（决策 #22）：`isSettlable` 只排除未定稿的 `submitting`/`resting`，
  被闸/被拒/0 成交行**也回填官方 `won`**（页面照显赢/输），但 `pnl` 恒 0；
  `DailyPnl`/`MaxDrawdown`/`LiveSummary` 按 **`HasPosition()`**（= `Kind != KindScan`
  ∧ `IsFilled()`）过滤 ⇒ 未成交行不动熔断。分类恒等式
  `signals = won + lost + pending + noexec`，胜率分母**只含 won+lost**。
- Dashboard（`runtime.tail_dashboard_addr`，决策 #18）**只读**这条流水线：四个闩锁
  （T150/T60/监听/信号）+ 锚冻结标记、热门侧读数（含 `hot_src`）、锚/σ 就绪、今日健康度
  （读当日 `tailstats_*`）都是现算——不参与判定、不写任何文件。
  ⚠️ 判决卡已删（决策 #22），纸面判决改由离线脚本做（**未写**，见待办）。

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
go run ./cmd/collect -config v4.config.yaml -asset eth      # 采集 ETH → data/eth（见决策 #23）

# python 分析/回测脚本一律用项目内 venv（系统 python3 无 numpy/pandas）
python/venv/bin/python python/v4/01_backtest_r1.py
python/venv/bin/python python/v4/06_oos_review.py    # 09-15 复验裁判（纯标准库）
python/venv/bin/python python/v4/07_source_health_check.py            # 数据源健康度审计（纯标准库）
python/venv/bin/python python/v4/07_source_health_check.py --bt-scan  # + book 阈值扫描/零成交代理
python/venv/bin/python python/v4/13_tail_sweep.py                     # 扫尾盘主回测（14/15 见 mapping 文档 §6）
python/venv/bin/python python/v4/24_asset_data_check.py --asset eth   # 采集数据正确性校验（任意标的, 决策 #23）
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
   09-15 原因分布对比；触发即落盘 + 结算仅 ok 且可结算的行进 pending（决策 #19）。
5. **数据采集已删除**：cmd/collect 整体移除（API 压力考量，保留备用已无意义）；
   未来需增采按 `docs/dog020_mapping_2026-09-02.md §5` 复活（注意键名炸弹）。
6. **即时落盘 + 重启恢复**：观测在触发 tick 立即落盘（行级 flush，崩溃不丢）；
   结算回填 temp+rename 原子重写当日文件；重启扫描 JSONL 恢复内存态，
   未结算行重建 pending 后由 `settle.Resolver` 的 Pending 扫描自动接回（决策 #19
   ——原先的「重新注册 gamma 轮询」分支已删，重启不再是特殊路径）。
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
    - **结算注册后移**：resting 行 `IsFilled()==false`、不进 pending；改由定稿回调
      在 `ApplyFillFinal` 之后进 pending（决策 #19 后由 `settle.Resolver` 的 Pending
      扫描接管，闭市 +10s 即可定案——挂单定稿本来就远早于此，不影响）。
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
17. ⚠️ **口径已被决策 #22 修订（2026-09-24）**——「三个闩锁 = 两帧折中 + 监听对账行」已被
    **三段判定链**取代（第一个闩锁本身就是下单点、监听段真下单、`kind=frame|scan` 降为
    legacy-only、配置键改名 `t150_rem`/`t60_rem`）。本条保留作为**历史与仍有效的部分**：
    §「独立进程 / 独立引擎包 / 单向依赖 flip / 三前缀隔离」、§撤单点/哨兵、§参数不可调、
    §已知未做（合并熔断、live 估量偏差）全部仍然有效。原文：
    **扫尾盘 ⑤ = 独立进程 + 独立引擎包 + 复用原语**（2026-09-23，用户要求「按文档逻辑
    与 flip 代码结构实现纸面与实盘到 ./cmd/tail」）。规格见
    `docs/tail_sweep_2026-09-22.md`，映射见 `docs/tail_engine_mapping_2026-09-23.md`。
    `internal/tail` **单向依赖 `internal/flip`**（取 `Tick`/`Executor`/`PaperExecutor`/
    `HistState`/`WindowEntry`/`CanTrade`/`SideYes|No`/`WonFor`/`ExecStatus*`），反向不依赖
    ——两族独立演进，但共用同一批经对账的原语。四条关键决定：
    - **三个独立的一次性闩锁**（前两个 = 两帧折中）：`rem ≤ frame_rem(150)` 首帧落**原始快照**
      （只记录、不判定、不下单）+ `rem ≤ rem_start(60)` 首帧落**决策快照**（判定 ⑤
      + 执行）+ 快照**未达标**时开**监听段**（`kind=scan`，首个 ⑤ 达标 tick 只记录——
      与快照同窗互斥，见上「监听口径对账行」条）。`ProcessTick` 返回 `[]Observation`
      （0~2 行，首个有效 tick 若已在尾盘则两行同发）。代价：**T 不可再调**（离线只能
      复算 60/150）。三行共用同一锚——首行落盘即冻结（`UpgradeAnchor` 此后拒收），
      否则帧行与快照行 `dev` 基准不同源。
    - **前缀独立**：`tail_*`（≤3 行/窗）/ `tailwin_*`（每完成窗 1 行, σ 预热源）/
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
      （⚠️ 阈值本身仍未变，但 `price_min` 后面的**算子**已随段而变——T=150 段严格大于，
      2026-09-26 用户决定，见决策 #26 与 `internal/tail.PriceLeg`；它**不是**配置键，
      故「不可调」依旧成立。）
    - **验收红线**：`internal/tail/parity_test.go`（**opt-in**, `data/btc` 不存在即
      skip）流式重放 14 天 3753 窗驱动真实引擎，断言五格聚合 = python oracle
      （① n=3109 WR 97.65% +72.10U … ⑤ n=1536 WR 99.61% +36.93U）+ 三个闩锁计数
      （frame 3643 / snap 3634 窗；监听增量 B∖A **485 行 WR 98.14% +7.00U**，oracle 来自
      `16_tail_t150_scan.py` 的 `scan60∖snap60`）——实测 n 逐位相等、WR/P&L 在两位小数内相等。
      ⚠️ 两条对账口径：宇宙须过滤「快照 tick 上 spot+twap 同时在场」；σ 须**按事件
      索引**现算（python 语义）后注入，**不能喂 `flip.HistState`**（后者只记已 push
      的振幅，缺窗时条数不同）。
    - **已知未做/差异**（写进 mapping 文档 §4）：① live 是 GTC 挂单等成交（挂到闭市），
      成交样本天然偏向「热门侧走弱」，与回测「快照瞬间即成交」**不是同一个估计量**
      ——**首次只跑纸面**；② 两族同开实盘时日亏熔断**各自一条独立 24U 线**（等效 48U），
      文档 §4.7 要求的合并**未实现**。
18. ⚠️ **判决部分已被决策 #22 取代（2026-09-24）**——「判决（含区间）在服务端算」整块下线
    （`internal/tail/judge.go` + `mt19937.go` 及两个测试全删，路由删 `/api/judge`、`/api/scans`、
    `/api/frames`，新增 `/api/signals`，前端四块卡片/表删除）。本条其余部分（**同包并列 +
    各自 listener/embed/独立键 `runtime.tail_dashboard_addr` + `cmd/tail` 第 4 个 flag +
    页面健康度读 `tailstats_*` + 零行为改动原则**）仍然有效。原文：
    **tail Dashboard = 同包并列 + 独立 listener + 判决在 Go 里现算**（2026-09-23，用户要求
    「static 改 flip、tail 再开一个 tail 目录；server 与 handlers 分文件，可共用的提到一个
    文件」）。`internal/dashboard` 从「flip 专用包」改成**双策略并列的单包**：
    `common.go`（分页信封/查询参数/JSON/日志/单页入口/`SourceLimits`/逐日聚合骨架）+
    每族三件（`{flip,tail}_server.go` / `_handlers.go` / `_state.go`）+ 每族自己的静态目录
    `flip/` `tail/`。**不拆子包**：两族的 handler 方法同名（`handleState` 等）靠 receiver
    类型区分，共用辅助函数留在 `common.go` 且保持非导出。五条要点：
    - **各自一个 listener、各自 embed 自己的静态目录**（URL 前缀都还是 `/static/`，FS 根
      不同）——`flip/index.html` 对 `/static/style.css` 的引用一字未改。两族的静态资源
      **有意复制**（不共享 CSS/JS）：两族页面会长成各自的样子，复制比耦合省事。
    - **启动方式 = 新键 `runtime.tail_dashboard_addr` + `cmd/tail` 第 4 个 flag
      `-dashboard`**（`flag.Visit` 语义同 flip：显式空串 = 关掉配置文件里的地址）。
      **不复用 `runtime.dashboard_addr`**：两族是独立进程，各自监听、可只开其一，
      同机并行须给不同端口。
    - **判决（含区间）在服务端算并直接给结论**（前端不写死 14/800/2000，口径常量随
      `/api/judge` 的 `meta` 下发）：`internal/tail/judge.go` 的 `Judge` 吃全量行 →
      ①~⑤ 五格 + T=150 对照格 + **监听增量 B∖A 格**（`pickScans`：只吃已结算 scan 行、
      不重判规则、不给它造仓位），每格给 n / 胜率 / P&L / 亏损日 / **日级 bootstrap 95%
      区间**（2000 次，seed 42）/ 判词 / 频率闸。`BootstrapCI` 在 Go 里**复刻 CPython
      的 MT19937 + `randrange`**（含 CPython 3.12+ `sum()` 的 Neumaier 补偿求和），
      与 `python/v4/13_tail_sweep.py:424 boot_days` **逐位一致**（黄金向量 + 实测期望值
      钉在 `judge_test.go`）——判决卡上的数字必须与复验脚本同一个数。
      ⚠️ 两处**口径差异**（写进 mapping §5.4）：① T=150 对照格的结果借自**同窗已结算的
      快照行**（帧行自己不挂结算），故它只覆盖「该窗快照成交过」的窗口，比 python 的
      全样本离线复算窄——它是**同一批窗口内 T=60 vs T=150 的配对比较**，不是全样本对照。
      胜负按**帧行自己的热门侧**重算（帧与快照的 hot side 可能不同），P&L 按
      `cfg.Stake / frame.HotAsk` 现算（帧行没有仓位）。② 监听增量格 = **只对 scan 行本身**
      算区间（速览），**不是**配对 Δ——配对（同一批重采样日期上算 B−A）由离线脚本
      `python/v4/18_tail_scan_register.py` 做。
    - **页面健康度读 `tailstats_*` 当日文件**（`Recorder.TodayStats()`，本族**唯一**的磁盘
      读口）：窗数 / `skip` 分布 / `anchor_exact=false` 计数 = 文档 §5.2 的**辅助闸门 3**。
      健康度行**不载入内存**（纯审计，省一次全量读盘），故 `/api/state` 每次请求现读当日
      文件；读失败只在日志留痕、不阻断状态页（窗数恒 0 即信号）。flip 的 `/api/state`
      不读盘（它的 winstats 只落盘）——这是本族多出来的那一段。
    - **零行为改动**：dashboard 侧复算现窗口读数用的纯函数（`SideOfHot` / `DevUSD` /
      `SigmaUSD`）就是引擎自己调的那三个（抽出来单一实现），`Latches()` 只读三个闩锁
      与锚冻结标记。验收红线不变：btreplay 625 笔逐位一致 + tail parity 五格与三闩锁计数
      （含监听增量 485 行）逐位一致 + `go test ./internal/... -race` 全绿。
19. **结算自算：推送优先的三层回退（flip / tail 共用）**（2026-09-24，见
    `docs/settle_self_2026-09-24.md`）。用户提问「官方结算价对齐的是窗口第 0 秒还是前一秒
    （59s）？若新窗 open == 前窗 close，就能用推送自算，只在丢推送时才拉官方」。
    探针（`python/v4/19_boundary_price_probe.py`，837 个实盘窗 / 官方缓存覆盖 315 窗）：
    官方 `open(N)` ≡ 边界那一秒的推送 `anchor(N)`（**315/315 逐位相等**）、官方
    `close(N)` ≡ `anchor(N+1)`（**309/309**）、官方 `close(N)` ≡ 官方 `open(N+1)`
    （**228/228**）；**错位对照** `close(N)` vs `anchor(N)` 差 p50 **51.83 美元 = 1.00σ**
    ——对齐是**定义级相同**而非近似。官方 API 方向 vs 市场实际结算方向 113/113 一致
    ⇒ 换层只改时延不改编号。
    - **三层与时点**：闭市 **+10s** 推送自算（闭市时唯一还在等的是 close 那条推送——边界
      N+300，闭市那一刻才发布；open 那条开局 ~2s 内已入表。实盘 880 个命中窗里它的到达
      延迟只有 1s(771) / 2s(94) 两档、**无一晚于 2s**，10s = 5 倍上界。⚠️ 决策 #15 记的
      「最晚 +12.1s」是取锚通道 500ms 轮询下的观测上界, 不是推送本身延迟——首版照抄取锚
      通道的 20s 预算取了 25s，2026-09-24 用户实测「还要 40-50s」后下调，见
      `docs/settle_self_2026-09-24.md` §6）→ 闭市 **+45s** 官方接口（**必须等收敛**，头几十
      秒是临时值，见决策 #14；每 5s 一次 × 至多 6 次；该门槛与 PushWait 无关，丢推送的窗口
      时点不变）→ **+75s** 交回 gamma 轮询（UMA 是市场结算本身，最后一层）。
    - **为什么值**：新口径下**判定与官方逐位同源**，误差归零——而引擎原先的 close 是
      **到达口径**流值，实测 315 窗里 3 窗（0.95%）方向被贴线漂移带反（三例官方 Δ 仅
      −0.079/−1.09/−0.080 美元，而窗口振幅中位 51.83 美元）；顺带日亏熔断从「等 UMA
      （闭市后数分钟）」提前到**闭市 +10s**。
    - **缺失面**：854 个实盘窗里 `anchor_exact=false` 仅 **15 窗（1.76%）**，且全部
      `ticks=298`（整窗 tick 齐全 ⇒ 上游那一秒没发，不是我们掉线）。一次结算要**两条**边界
      推送 ⇒ 约 **3.5%** 的结算走官方层。
    - **实现**：新增 `internal/settle`（**零外部依赖、不 import `internal/flip`**——判定词表
      用常量相等断言钉住，不靠依赖），`internal/feed/official_pair.go` 提供 SDK 侧
      `PricePairFetcher`（一次调用取 open+close）。`Record.SettleSrc`
      （`push|official|gamma`，`omitempty`）逐行落盘供事后分桶对账。
    - **触发点收敛**：原先三处预先 `Register` gamma（信号落盘 / 挂单定稿 / 重启恢复）**全部
      删除**，只保留 `GiveUp` 一处——两个驱动抢同一行会有一边报「结算回填未命中」；且
      `Pending()` 直接扫 `Recorder.PendingSignals()`（`isSettlable` 已过滤），重启恢复免费复用。
    - **σ 的 close 口径未改**（仍是 `lastTick.TwapPrice` 到达口径，与本次改的结算 close
      相差 p50 1.234 美元）——动它会改 σ ⇒ 改浅洞带，属策略参数问题，须单独走一轮回测。
    - 验收：btreplay 625 笔逐位一致（n=625 WR 24.6% EV +0.633U +395.8U）+ tail parity
      三闩锁计数一致（frame 3643 / snap 3634 / scan 485）+ `go test ./internal/... -race`
      全绿 + `TestResolveSettleSrcPersisted`（两族各一，含重启与 `omitempty` 钉子）。
20. **无仓位行独立成类：`signal = won + lost + pending + noexec`**（2026-09-24，flip
    Dashboard）。起因：用户从日志看到一条 `信号被风控闸拦截未下单: 重启后首窗禁单`，
    页面上却找不到它——`/api/signals` 里这行**是在的**（`Signals()` 只按 `OK` 过滤），
    但结果列只会渲染 `won == null` ⇒「待结算」，**永远不会变**（无仓位 ⇒ 永不被结算），
    而旧的 `lost = sigCount − won − pending` 反算又把它计进「负」——一笔从未下过的单
    同时虚增亏损笔数与压低胜率。两处都改：
    - **分类**：`FlipState.tally` 逐行只落一类——`won` / `lost` / `pending`（**在
      `Recorder.PendingSignals()` 里**，即 `isSettlable` 过滤后的真实持仓行，判据单一来源
      不另写一遍）/ `noexec`（其余）。`/api/state` 增 `noexec_count`，`/api/daily` 的
      「待结算」列拆出「未成交」列（`flipDailyRow` 包 `dayAgg`，同 tail 的
      `tailDailyRow` 形制；共用骨架 `dayAgg` 不动 ⇒ tail 数字零变化）。胜率分母**只**
      含赢+输。
    - **可见性**：`recordResponse` 补 `gate_reason` / `exec_status` / `exec_note`
      （此前三个字段根本没进响应），前端结果列三态合一：已结算 → 赢/输（paper 方案 A
      下被闸行照常结算，故附 `闸·首窗`/`闸·熔断` 标记）→ 被闸 → 执行状态（`挂单中`/
      `未成交`/`下单被拒`/`下单中`，`未知结果` 前缀单列「成交未知」）→ 待结算。份额与
      P&L 对无仓位行显示「—」（不拿目标股数冒充成交）；观测表判定列同步标闸。
    - ⚠️ **口径红线**：`pending` ≠ `won == nil`（后者含永不结算的行）；分析脚本若按
      「未结算」筛样本，必须区分「在途」与「无仓位」。paper 方案 A 的被闸行仍照常结算
      ⇒ `noexec` 里**永远不含** paper 行（它只出现在 live 的 rejected/unfilled/resting/
      成交未知）。
    - 验收：`TestFlipNoExecClassification`（6 行混排钉住恒等式 6=1+1+1+3、胜率 0.5、
      逐日同口径、三个键逐行透传）+ `go test ./internal/... -race` 全绿。
    - **tail 侧未动**（用户本次只报 flip）：tail 的 `collectDaily` 与 `lost = sig − won
      − pending` 有同一反算口径，但 tail 实盘未上、被闸行恒为 paper（照常结算）⇒ 现网
      无症状；真上实盘前应同步这套分类。
21. **空侧整簿不得丢弃：赢家侧在尾盘整侧没有 ask**（2026-09-24，见
    `docs/book_empty_ask_2026-09-24.md`；证据工具 `cmd/bookprobe` + `python/v4/{21,22}`）。
    起因是用户看纸面 tail 数据后提「60s 检查点很多其实已经没有 ask 单了，实盘可能不能
    成交」，要求**核实 SDK 数据**并把 Dashboard 上的假 0.99 改成「—」。实测（SDK WS 与
    公共 REST 两路独立取证）：
    - **事实**：事件趋于确定后**赢家侧的卖单被整侧撤空**——4 个实盘窗里空侧起始 rem =
      **12 / 28 / 45 / 47**，一直空到收盘（档数轨迹 `9 8 7 7 7 6 5 4 4 4 3 3 0`：慢慢撤、
      最后整侧撤空）。对手侧（输家）始终有 ask（0.01 价位 3 万~20 万股）。**赢家侧空
      asks ≡ 输家侧空 bids**（镜像同一事实）⇒ 两个 token 各缺一侧。
    - **SDK 无错**：`book` 事件是**整簿快照**，`market_monitor.go onOrderBook` 原样解析，
      asks 空数组 = 真的空。**0.99 是引擎自己造出来的**：`cmd/{flip,tail}` WS 循环的
      `if book == nil || len(book.Bids) == 0 || len(book.Asks) == 0 { continue }` 把这类
      消息整个丢掉 ⇒ 内存里留着**撤单前那一份旧簿**（常 0.97~0.99）⇒ Dashboard 与判定
      都用假卖价（实测引擎视角的簿龄一路涨到 **46.9s** 而页面照旧报 0.99）。
    - **修法**：守卫只留 `book == nil`。空侧照存 ⇒ `feed.bestAsk/bestBid` 返回 0 ⇒
      两族引擎的四档门控判该 tick **无效**（与回测宇宙同口径：四档缺一即丢）⇒ 不再产出
      基于假卖价的信号；Dashboard 前端**无需改动**（`> 0 ? 价 : '—'` 本来就在，错的是
      喂给它的数）。测试钉在 `TestNewPMTickEmptySide`。
    - **实盘端到端复核（修后跑真引擎 `cmd/tail` 纸面 + 轮询 `/api/state`，doc §7）**：
      空侧窗口页面如实显示 `no_bid 0.99 / no_ask 「—」`（判决卡「无有效盘口」），
      假 0.99 消失；`no_ask` 是**精确 0**（不是四舍五入——引擎 `book_missing` 只在
      四档缺一时累加，同一窗记 78 个 tick，`internal/tail/engine.go:153`）。
      同一批数据里 `book_stale` = **25~143 / ≈299 tick（8%~48% 被 300ms 延迟闸判无效）**，
      即下面 ① 的规模；12:15Z 窗的帧行落在 rem=**71**（rem≤150 起的 79 秒无一个有效 tick）。
    - ⚠️ **纸面数据的 0.99 分两种窗**（修后跑真引擎端到端复核，doc §7）：空侧发生在
      rem≤60 **之后**的窗（探针四窗 rem 12/28/45/47）⇒ 快照那一刻卖单还在，是**真读数**
      （样本 0.90×23 / 0.93×135 / 0.88×154 股）；空侧发生在 rem≤60 **之前**的窗 ⇒
      快照读的是**冻结簿**（旧守卫留的空侧前那一份），是**买不到的纸面价**。实盘复核
      2/2 窗都是后者（12:25Z/12:30Z 两窗空侧起始 rem=**68/112**，`book_missing 78/116`
      个 tick、快照闩锁从未推进 ⇒ 修后这两窗**一行快照都不产**；旧代码会产一条
      `hot_ask≈0.95-0.97` 的纸面信号）。⚠️ 220 条行的镜像恒等式**分不出这两类**
      （两 token 同一瞬间一起冻结，恒等式照样成立）⇒ 「全部为真」不成立，比例随当日
      趋势摆动（今天行情一边倒 ⇒ rem≈130 就已 0.95+）。**代价**：修后快照样本变少，
      §5.2 的 `n ≥ 800` 闸门走得更慢——这是与回测宇宙（四档缺一即丢）对齐，不是退化。
    - ⚠️ **两个未做的（不在本次范围，须各自单独立项）**：① **读龄未纳入延迟闸**——
      `BookLatMs` 是**接收时刻**的传输延迟，不含此后累积的簿龄；探针期间 SDK
      `MarketMonitor` 多次被服务端以 `slow consumer: send buffer full` 断开重连
      （一窗内 `raw_age_ms > 1.5s` 有 41 秒、最长 **12.4s**，位置在窗口**中段**），
      冻结期四档看起来完全正常 ⇒ flip 侧「输家侧 ask ≤ 0.20 且 rem > 180 恰落在冻结秒」
      会产生基于旧价的触发（本样本未出现，**未量化**）。改判定 = 改口径（回测
      `MAX_LAT=300` 同口径）⇒ 必须走回测。② **`minimum_order_size = 5 股`**（CLOB 元数据
      实测；`minimum_tick_size=0.01`、`neg_risk=false`）而 `tail.stake=2U` 在 ≥0.80 只有
      **2.02~2.5 股** ⇒ 低于交易所下限，tail 上 live 前必须先验 1 笔（flip 侧 2U@0.20 = 10
      股不受影响）；要把 stake 提到 ≥5U = 每笔风险 2.5 倍，属用户决定。
    - 验收红线不变：btreplay 625 笔逐位一致（n=625 WR 24.6% EV +0.633U +395.8U）+ tail
      parity 三闩锁计数一致（frame 3643 / snap 3634 / scan 485）+ `go test ./internal/...`
      全绿。（`internal/feed` 的 `TestRecoverAnchorSettleGuard` 是**既有**时序敏感用例：
      10/30/60ms 采样点 vs 50ms 收敛点，整套并行跑约 1/4 概率误判，与本次改动无关。）
22. **扫尾盘整合改造：三段判定链 + 空侧 ask 优先 + 全信号结算**（2026-09-24，用户书面决定
    `a.md`，落地规格 `docs/tail_integrated_2026-09-24.md`，它取代 `tail_sweep_2026-09-22.md`
    §2 的两帧折中与 mapping 文档的冲突段；⑤ 本体定义不变）。四块内容与四个已确认口径决定：
    - **① 三段判定链**（`internal/tail/engine.go`，`Watching → Await60 → Listening → Done`）：
      T=150（`t150_rem`）的首个可判定 tick 判一次完整 ⑤ → 不达标则 T=60（`t60_rem`）再判 ⑤
      → 仍不达标则**此后每秒**判 **②**（`hot ≥ 0.80 ∧ dev ≥ 63`，**不含 σ 腿**）→ 任一段出信号
      即整窗只下一单并 `Done`。原「rem≤150 原始帧（只记录）」不再是只记录——**它本身就是下单点**。
      迟到接入（首个可判定 tick 已在 rem ≤ 60）跳过 T150 段且**不伪造 t150 行**。
      `ProcessTick` 由返回 0~2 行改为**至多一行**（`*Observation`），`cmd/tail` 收敛成单点
      dispatch。**可判定 tick** = `BookLatMs ≤ max_book_lat_ms ∧ 热门侧有效价 > 0 ∧ spot > 0
      ∧ rem > 0`；**缺 spot 只跳过不落行、不推进段**（历史数据是 0 窗差异）。
    - **② 空侧不算无效**：新纯函数 `HotBook(upBid, upAsk, downBid, downAsk) (side, px, src)`
      ——每侧**有效价** = `ask > 0 ? ask : bid`，热门侧 = 有效价高的一侧（平局取 yes），
      四档全空才 `px = 0`。价格腿 / 股数 `stake/有效价` / live 限价**全用有效价**；落盘增
      `hot_src ∈ {ask, bid}` 审计字段。motivation 是决策 #21（赢家侧尾盘整侧撤空 ask，
      旧口径直接丢 tick）。14 天 542800 tick 里「四档部分缺」**0 次** ⇒ 这是**纯 live 改动**，
      parity 用 `hot_src 恒 ask` 钉住（将来真出现单侧空簿，测试立刻红、须重做 oracle）。
    - **③ 所有信号都注册结算**：`isSettlable` = `kind = snap（含 legacy scan）∧ ok ∧
      未结算 ∧ conditionID 非空`，唯一排除项是**未定稿**的 `submitting`/`resting`（被闸行、
      下单被拒行、0 成交行都拿到官方 outcome 并显示赢/输）。**未成交不计 P&L**：`recomputePnL`
      对 `!HasPosition()` 归零；`DailyPnl`/`MaxDrawdown`/`LiveSummary` 的 P&L 段同过滤
      ⇒ 未成交行不动熔断、不进回撤、不进累计。⚠️ 判据必须是 **`HasPosition()` =
      `Kind != KindScan ∧ IsFilled()`** 而非 `IsFilled()`：legacy 的 scan 对账行恒无仓位但
      `ExecStatus` 为空，会被 `IsFilled()` 误判成 paper 成交而混进胜率与 P&L。
      **被闸行口径变更：tail 的方案 A 作废**（a.md 把「被风控拦」明确归未成交）——
      paper 与 live 统一成一个 `rejected` 形态（`exec_status=rejected` + `gate_reason`），
      paper 被闸行不再算持仓、不进胜率/P&L；代价是 paper 不再保留「当日不熔断会怎样」的
      反事实样本（**flip 侧不受影响，仍是方案 A**）。熔断锁存判据（`GatedOn`）不变。
      分类恒等式（dashboard tally 与逐日表共用）：`signals = won + lost + pending + noexec`，
      胜率分母**只含 won + lost**。
    - **④ Dashboard 大改**：删「判决速览」/「五格对照」/「监听对账」表/「原始帧」表；
      胜、负两卡合并为「胜/负」+ 新增「未成交」卡（= `noexec` = 下单失败 + 被闸 + 0 成交）；
      新增**信号**表（时间/侧/rem/ask/dev$/sd$/份额/状态/结果/P&L，状态含未成交、未成交行
      P&L 显示「—」）；决策快照表删「五格」「结果」「P&L」三列。路由：删 `/api/judge`、
      `/api/scans`、`/api/frames`，**新增 `/api/signals`**（`/api/snaps` = 全部判定行）。
    - **判决机器 Go 侧整删**：`internal/tail/judge.go` + `mt19937.go` + 两个测试全删——
      14 天 / n≥800 / 日级 bootstrap 的纸面判据随卡片下线，改由**离线脚本**做（`python/v4/23`
      现只产 oracle 数字，判决脚本**本次未写**）。`python/v4/18_tail_scan_register.py`（A/B 配对）
      **作废**：scan 行不再产出——监听段现在**真的下单**，「监听口径 B」不再是对照物而是策略本体。
    - **行类型不新增 kind**：三段行一律 `kind = snap` + 新字段 `stage ∈ {t150,t60,listen}`；
      `KindFrame`/`KindScan` 降为 **legacy-only**（`isKnownKind` 仍认、引擎不再产出）。拒绝原因链
      去掉 `missing_twap`（⑤ 不用 twap）：`missing_spot → no_hist → price_low → leg_out`。
      监听段的**拒绝 tick 不落行**（每窗 ~60 个 tick，全落会淹没信号表）；两个判定段成功与否都落盘。
    - **防双单 + 崩溃续跑**：`cmd/tail` 重入同窗时 `Recorder.HasSignal(conditionID)` 有 OK 行即整窗
      跳过（只有拒绝行则续跑）；新增 **`Engine.Resume(t150Done, t60Done)`** 按磁盘真相回填段闩锁
      与状态（两个都完成 ⇒ Listening），**`emitted` 保持 false**（否则 `UpgradeAnchor` 被自己的
      冻结判据挡死、锚永远进不来；锚由同一条边界推送确定性重取，与旧行基准同源）。`Latches` 结构改
      `{T150, T60, Listening, Signal, Frozen}`。
    - **配置键改名**（语义已从「帧/快照」变成两个判定点）：`tail.frame_rem → tail.t150_rem`（150）、
      `tail.rem_start → tail.t60_rem`（60）；其余五键与「全部不可调」的约定不变（决策 #17）。
      `internal/config/validate.go` 改判 `t150_rem > t60_rem > 0`。
    - **验收数字（oracle = `python/v4/23_tail_integrated.py`，`parity_test.go` 逐位钉）**：
      ⚠️ **2026-09-26 已被决策 #26 改口径**（T=150 价格腿严格大于）：现行 = n=2133 /
      96.06% / +35.09U / 6412 行（t150 1208、t60 577、listen 348）；下列是当时的数。
      14 天 2U/注 **n=2135 WR 95.97% +35.67U**（t150 1220/93.93%/+16.02U 亏损日 5/14、
      t60 568/99.12%/+15.76U 亏损日 1/14、listen 347/97.98%/+3.90U 亏损日 3/14）；
      判定行 6399（3638/2414/347），参与判定 3640 窗，σ 未就绪整窗跳过 3 窗（决策 #13）。
      对照旧口径「只在 T=60 判 ⑤」n=1536 / 99.61% / +36.93U ⇒ **总 P&L 基本持平、注数 +39%、
      EV/注摊薄**（T=150 段仅 +0.0131U/注）——a.md 的显式选择，**不调参**。
      ⚠️ 与落库前的预估表（n=2136 / +35.78U）差的正是那 1 笔：预估不跳 σ 未就绪窗，已逐窗
      复核为 t60 段 `fill 0.95` / dev 63.12 / 赢 / +0.1053U（15.7562+0.1053=15.8615≈15.86U 逐位吻合）。
    - **已知取舍（写进文档，不修）**：① ask 空按 bid 成交时 paper 按「限价即成交」记账（偏乐观），
      live 是挂单等成交、大概率不成交 ⇒ `hot_src=bid` 的样本在实盘须单独看；② 两族合并熔断
      （文档 §4.7）仍未实现，tail 与 flip 各一条独立 24U 线；③ `minimum_order_size = 5 股`
      而 `stake=2U` 在 ≥0.80 只有 2.0~2.5 股（决策 #21 ②）**仍未解决**，tail 上 live 前必须先定。
    - 验收：btreplay 625 笔逐位一致（n=625 WR 24.6% EV +0.633U +395.8U）+ tail parity 新表
      逐位一致（6399/2135/3640/3 + `hot_src` 恒 ask）+ `go test ./internal/... -race` 全绿。

23. **采集管线回归 + 多标的参数化（`internal/feed.Asset`）**（2026-09-25，用户要求
    「看 eth 分支的 collect/compact 同步过来并适配 ETH」，随后追加「把 btc/eth 的硬编码
    处理了，统一成参数，后面还要采 sol/bnb」）。完整报告见
    `docs/collect_eth_sync_2026-09-25.md`。
    - **同步 eth 版而非 v3 版**：`eth` 版行字段名 `yes_*`/`no_*` 与现存 `data/btc` 3753 窗
      及全部 `python/v4` 脚本逐字一致；另移植 v3 独有的三处修复（Binance 启动重试、
      `Submit` 的 `done` 守卫、关闭路径直写 `WriteUniqueEvent`）。删 `internal/collect/writer.go`
      （`JSONLWriter` 零调用者，eth 分支里也是死代码）。
    - **三处与 eth 原实现的有意差异**（口径对齐 v4 引擎，均有证据）：
      ① **边界价改精确推送**——`PushNearest` 精确等值匹配取代「`Latest()` 流采样 + 官方
      30s 轮询」，依据决策 #19（官方 open ≡ 边界推送 315/315、close ≡ anchor(N+1) 309/309）
      与决策 #14（官方头几十秒是未收敛临时值）；正常窗**一次官方请求都不打**，只在推送
      缺失时排队修正。② **空侧盘口不丢消息**（决策 #21）——守卫只留 `book == nil`，
      赢家侧尾盘整侧撤空 ask 时照存 ⇒ 该侧价为 0 ⇒ 该 tick 四档缺一（与回测宇宙同口径）。
      ③ **删 `MinRange` / `MaxStreamAgeMs`**——它们是为「流采样 close vs 官方边界值」的
      量化误差标定的；close 与官方同一个数后该误差归零，修正的唯一触发条件变成「没拿到
      推送」（`SettlementConfig` 只剩 PollInterval/MaxWait/MaxConcurrent）。
    - **硬编码消除**：新增 `internal/feed/asset.go`（`AssetFor(name)` / `AssetFromSlug(slug)` /
      `DataDir()` / `ApplyBinance(cfg)`），一处资产名派生四条命名（slug / Binance 交易对 /
      Chainlink 符号 / 数据目录）。接线：`cmd/collect` 增 `-asset`/`-slug`/`-symbol`/`-output`
      四 flag（优先级 `-slug` > `-asset` > `runtime.slug_prefix`）；`cmd/flip`/`cmd/tail` 各
      4 处（TWAP 符号 / Binance 交易对 / `PricePairFetcher` / `FetchTwapRanges`）；
      `binance.symbol` 默认值改 **`""` = 留空即派生**（显式填优先，应对 `1000XXXUSDT`
      这类引号货币不是 USDT 的标的）——`v4.config.yaml` 与漂移守卫同步。
      ⚠️ `sdk.CryptoPriceSymbol` 只是 `string` 类型（SDK 的 SOL/BTC/ETH/XRP 是便利常量），
      派生不需要映射表。
    - ⚠️ **口径陷阱（同步时踩到并修掉）**：两个包的 `"stream"` **同名不同义**——
      `feed.AnchorSourceStream` = **精确边界推送**（决策 #15 起 `anchor_src` 取
      `official|stream` 的历史命名），`collect.SourceStream` = **到达口径采样**。采集侧
      原先直接 `anchorSrc = rec.Source` 落盘 ⇒ 取锚**成功**的窗被标成到达口径 ⇒
      `needsCorrection` 判真 ⇒ 每窗白排队一次官方修正并用官方值覆盖已逐位精确的值
      （数值相同、来源标签被改坏；首窗实测复现）。修法为纯函数
      `cmd/collect/main.go:anchorSource(feedSrc)` 显式翻译词表。
    - **数据校验 `python/v4/24_asset_data_check.py`**（纯标准库，**任意标的**）：八段全部
      拿外部真值或内部恒等式对——A 结构与元数据 / B tick 覆盖与 rem 恒等式 / C 盘口互补
      （`yes_bid+no_ask=1`）/ D **Binance REST K 线**逐分钟对成交笔数·成交量·价格区间 +
      `binance_open` vs 5m K 线开盘价 / E TWAP 龄 / F 成交桶 / G **官方 `crypto-price`**
      vs 行内边界价（决定性：口径为 push 的行必须**逐位相等**）/ H gamma 结算方向。
      网络两个坑已在脚本内处理：gamma 与 `polymarket.com` 对裸 urllib 的默认 UA 直接
      **403**（要浏览器 UA）；`crypto-price` 会 **429**（逐窗 0.35s 间隔 + 退避重试）。
    - **自检**（对旧 `data/btc` 3753 窗跑一遍，既验脚本也量化新旧口径差）：D 段成交笔数比
      中位 **0.9995**、成交量比中位 **0.9999**、`binance_open` 6/6 与 5m K 线开盘价逐位
      相等 ⇒ 采集侧对 Binance 的聚合逐位可信；G 段旧口径行（`close_source=stream`）
      开盘价逐位相等 2/5、**收盘价 0/5**、最大差 1.38 美元 ⇒ 正是被换掉的那个口径。
      H 段方向 6/6 与 gamma 一致。
    - ✅ **决定性验收（G 段：官方边界价 vs 行内值逐位比较）**——ETH 首窗推送口径
      **开盘价 1/1、收盘价 1/1 逐位相等（最大差 0.000000 美元）**；对照 BTC 旧口径行
      （`close_source=stream`）**开盘 2/5、收盘 0/5、最大差 1.38 美元**。即决策 #19 的
      「官方 `openPrice(N)` ≡ 边界推送、`closePrice(N)` ≡ `anchor(N+1)`」**在 ETH 上同样
      成立** ⇒ 采集/引擎/结算三处同一口径，ETH 数据可直接喂 v4 引擎、无需换算。
      D 段同时确认 Binance ETHUSDT 聚合逐位可信（笔数比 1.0000 / 量比 1.0000 /
      `binance_open` 与 5m K 线开盘价 1/1 逐位相等）。⚠️ **H 段（gamma 方向）未验**：
      `umaResolutionStatus` 分钟级延迟，首窗校验时尚未 `resolved`。
    - **首窗 ETH 实测**（`data/eth/events_2026-09-25.jsonl` 首窗，12:50Z）：
      `close_source` / `anchor_source` 双 **`push`**（验收 §4 的词表修复生效）、301 tick
      无断档、TWAP 零价 0、成交桶 176 行。两处非缺陷但需记账的观察：
      - ⚠️ **整侧缺腿：首窗 36/301 = 11.96%（rem ∈ [0,34]）、次窗 81/300 = 27%
        （rem ∈ [0,79]）**——两窗都 outcome=0（YES 赢），缺的都是卖腿 `yes_ask+no_bid`，
        即赢家侧卖单被撤空，形态与决策 #21 一致。**但比例远高于 BTC，且未独立取证**：
        - BTC 的「0.00%」**确定是旧守卫的构造性产物**（空侧消息被整条丢弃 ⇒ 这类 tick
          在旧数据里根本无法以缺腿形态出现），不是市场属性——这条成立。
        - 而 ETH 的 12%~27% **是不是市场真相，尚未证实**：本机网络/订阅侧同样能产生
          「某 token 的簿停更在某一份空 ask 快照上」的读数。决策 #21 当初对 BTC 是
          用**两条独立通道**（SDK WS + 公共 REST）取证的，ETH 这次只走了采集器一条路。
        - ⇒ **待服务器部署后按决策 #21 的双通道方法复核**（用户 2026-09-25 判断
          「有可能是我的网络问题，可以等部署服务器跑数据再验证」）。**在那之前，
          不要把 12%~27% 当作 ETH 尾盘撤空率的估计量用**。
        - 🔍 **用户假设的当场反证（同一晚）**：对 13:00Z 窗尾盘走**公共 CLOB REST**
          （与采集器 WS 订阅完全无关的独立通道）取数——`Up bid 0.00(0档)/ask 0.01(52档)
          | Down bid 0.99(52档)/ask 0.00(0档)`，整个尾盘如此，档数 52→47→46 在变
          （实时非缓存）。**现象因此被证实是真的**，不是本机订阅残留。
          ⚠️ **两副面孔记下来**：该窗 Down 赢 ⇒ 缺的是**买腿**（`yes_bid=0 ∧ no_ask=0`）；
          而 §5.5 表里两窗 Up 赢 ⇒ 缺**卖腿**（`yes_ask=0 ∧ no_bid=0`）。同一条规则
          ——**被清空的永远是对应「押最终输家」的那条腿**（输家的 bid 无人接、
          赢家的 ask 无人卖）。**比率仍待复核**（REST 只验了一窗尾盘；12%~27% 可能被
          本机 WS 滞后放大 ⇒ 持有空快照的时间长于市场实际）。
        - 要真正的 BTC 基线同样须用新代码重采（未做）。
      - **互补越界的合理阈值**：用新逻辑重扫 BTC 3753 窗（1,091,652 有效 tick）得合计
        **0.126%**、逐窗 **p99 1.0% / max 1.667%**——原先那条逐窗 0.5% 的 FAIL 线
        BTC 自己就超。改成两级（合计 > 1% / 单窗 > 5% 判失败），实测分布写进脚本注释。
        成因是**两本簿采样错位**：一行的 `yes_*` 与 `no_*` 来自两条独立 `book` 消息，
        而 `MakePMTick` 的 `book_ts` 只取两簿**较大者**，行内分辨不出两个瞬间
        （ETH 首窗那 2 个越界 tick 正落在 rem 94/96 的价格急跌处）。下游同时读两侧的
        判定（如 flip 四档门控）须知此瞬态错位存在。

24. **持仓监察（只记录）+ 止损评估：离线支持、能不能成交是唯一的坎；成交量维度已否**（2026-09-25，
    见 `docs/tail_stoploss_2026-09-25.md`，脚本 `python/v4/{25,26,27}`）。起因是用户提
    「尾盘被砸时能不能靠止损保命」，并追问「整窗/砸盘那一刻的 BTC 成交量能否提高止损精度」。
    - **止损腿规格（未落引擎）**：触发 = 持仓侧 `bid < 0.30 ∧ dev < −20 美元`；出场 = 触发
      那一秒按**持仓侧 bid** 卖出，`P&L = shares·bid − STAKE`。阈值取自 25 的扫描平台
      （bid 0.25~0.30 × dev −20 整片同号），不是单点拟合。⚠️ 本条全部数字是 **09-25 冻结
      基线**（`≥ 0.80` 口径, 由脚本 26 存档）——2026-09-26 的决策 #26 换了 T=150 的算子后
      现行基线是 **+35.09U**，重跑评估须先把 26 的 `r5` 改成 `strict_price=True`。
      14 天 2U/注：基线 **+35.67U**
      → **+46.11U**（Δ **+10.44U / +29.3%**，亏损日 3/14 → **2/14**），**配对** bootstrap
      95% CI **`[+2.24, +19.59]`** 下界 > 0。
      ⚠️ 显著性判据必须是**配对 Δ 的区间**（`boot_delta`），不是止损后 P&L 的绝对区间
      ——后者与基线高度相关，基线点估计落在里面很正常。
    - **杀赢 vs 救输（用户判据）**：触发 68 次（占信号 3.19%）= **杀赢 5 / 救输 63**。
      出场价 `p̄ = 0.139` ⇒ 打平所需救输:杀赢 = `(1−p̄):p̄` = **6.2:1**，实际 **12.6:1**
      ⇒ **留 2.0 倍余量**，用户的否决条件（「杀赢亏得比不止损还差」）**不成立**。分解：
      救输 **+18.22U** + 杀赢 **−7.78U** = **+10.44U**（每笔：救输 +0.289U / 杀赢 −1.557U）。
      **阈值建议 `bid < 0.25` 而非 0.30**：Δ 更高（+11.53 vs +10.44）**且**杀赢更少（3 vs 5）
      ——不对称：杀赢的损失是无条件的，救输的收益取决于实盘可成交性，宁可不杀。
    - **两种胜率口径必须分开看**：**官方 outcome 口径 WR 95.97% → 95.97%（±0）**；
      **落袋口径 95.97% → 95.74%（−0.23pp）**——`fill ≥ 0.80` 而止损价 < 0.30 ⇒ 被止损行
      必然亏损，WR 一定降。这是记账的必然，不是策略恶化。
    - **翻转解剖**（86 个最终亏损样本）：**86/86 全都在 bid 0.5 处下穿**（假下穿极少：
      2046 个赢家里仅 62 个曾跌破 0.5 = **3.03%**）；翻转 rem 中位 **75**、**翻转时 dev 仍
      是 +10.2 美元**——市场先动、现货滞后，与「浅洞腿实质是基差腿」（记忆
      `dog020-dists-is-basis-not-hole`）同源。
    - ❌ **成交量维度否**（用户假设逐条验证，脚本 27）：
      - `V_win = Σ(bin.buy_vol + bin.sell_vol)`（BTC，可直接从 tick 聚合）p10/p50/p90 =
        **23.8 / 66.0 / 204.9**（极差 598×）。三条独立证据都说没用：① 对入场无预测力
        ρ(V_win, 结算结果) = **−0.023**；② 与 σ 腿半冗余 ρ(V_win, sd) = **+0.579**；
        ③ 当开关也没用（高量窗 Δ 掉到 +1.67(p75)/−0.35(p90)，低量窗 +0.76(p25)，
        CI 全含 0；最好一档 `<p90` 只比不切好 0.34U）。
      - **用户的「砸盘那一刻量比」方向对、强度不够，且判别力恰好在止损那一档衰减到噪声**
        （最近 k 秒量 ÷ **剔除被砸窗口**的基准速率；基准含被砸段会把放量自我归一化掉）：
        跌破 **0.60** AUC **0.574** / **0.50** 0.532 / **0.40** 0.493 / **0.30（止损触发处）
        0.482**（bid 跌幅同形：0.583→0.484）。效应量：跌破 0.60 的 179 笔按量比分三档，
        高量比档最终输 **60.0%** vs 低量比档 **43.3%**——16.7pp、**1.8σ、p ≈ 0.06 不显著**。
        道理：砸到 0.60 时「是不是真有事」还有悬念，砸到 0.30 时**事情已经发生完了**。
        ⇒ 量比测得出「砸得凶不凶」，测不出「砸完会不会弹回来」，而后者才是止损要判的。
      - 🧭 **顺带浮现的线索（是另一条策略，不在本次范围）**：持仓期跌破 0.60 后最终输的
        概率 **48%（86/179）**，在 0.60 出场的临界比只有 **0.67:1**（vs 止损在 0.30 的 5.5:1）
        ——杀错一次只亏 0.40/股、救对一次省 0.60/股，几乎对半开就够本。⚠️ 但它是**早退策略**
        不是止损腿，且同样吃对手方上界 ⇒ **须单独立项，不要顺手加**。
    - ⚠️ **上界声明（读数前必读）**：所有离线 Δ 都假设触发那一秒有 bid 可吃。但决策 #21
      的活体取证证明尾盘「押最终输家」那条腿**被整侧撤空**（BTC 探针四窗空侧起始
      rem = 12/28/45/47；ETH 首采 rem ∈ [0,34]/[0,79]）——而**止损要出场的那一刻，正是
      持仓从赢家变成输家的那一刻**。历史 14 天里「持仓侧 bid == 0」出现 **0 次**，那是旧
      采集守卫丢弃空侧消息的**构造性产物**，不是市场属性。另有滑点未计、CLOB
      `minimum_order_size = 5 股`（决策 #21 ②）。⇒ **全部 Δ 都只是上界。**
    - **应对 = 持仓监察（本次唯一落地的代码，只记录不判定）**：`internal/tail/hold.go`
      （`StopWatchBid/StopWatchDev` + `HoldIdent` + `HoldRow` + 纯函数 `HoldWatchRow`）+
      recorder **第四族** `tailhold_YYYY-MM-DD.jsonl`（`LogHoldTick`）+ `cmd/tail` 旁路接线
      （信号成交后置 `holdID`，逐 tick 调用；深度字段由 `bookTop5` 从 SDK 簿现算回填
      ——`internal/tail` 零外部依赖不引 SDK）。**门控与判定路径刻意不同**（这是它存在的
      全部理由）：`rem > 0 ∧ 延迟 ≤ 300ms ∧ spot > 0 ∧ 锚可用`——**不要求持仓侧 bid > 0**
      （bid == 0 正是要观测的东西）、**不要求四档齐全**、**不要求 rem ≤ 150**。
      行内 `stop_cand` 是纯派生标记（= 26 的止损条件，分析时一步筛出「这一秒会不会触发」），
      引擎不会因为它做任何事。**硬边界三条**：只记录（不进 P&L / 熔断 / 胜率 / 任何判定）；
      不碰引擎状态（独立前缀 + `kind` 双保险，与 `tail_`/`tailwin_`/`tailstats_` 互不沾染，
      决策 #9 红线）；落盘失败只记日志、不上抛主循环。所有浮点字段**不带 `omitempty`**
      ——`hold_bid: 0` 必须原样落盘（这是整条链路上唯一会丢它的地方，`TestLogHoldTickKeepsZeroBid`
      钉住）。**为什么必须记深度**：`minimum_order_size = 5 股`，所以「bid 存在但只有 3 股」
      与「没有 bid」对实盘是一回事，价格字段分辨不出来。
      **读法**：跑一两周后看 `stop_cand=true` 那些时刻里 `hold_bid == 0` 的占比与 `hold_bid5`
      分布——占比低 + 深度够 ⇒ +10.44U 大致可信、可认真考虑上止损；占比高 ⇒ Δ 要按实际
      可成交比例打折甚至归零，**那时不上止损**。
    - **与既有决策的关系**：**不影响 parity 红线**（纯增量：新前缀、新纯函数、`cmd/tail`
      一段旁路，不碰 `ProcessTick` 任何分支、不动三个闩锁与锚冻结判据）；**未改任何配置键**
      （监察恒开、零风险纯记录，决策 #17/#22「参数全部不可调」不动）；**止损腿本身也未落
      引擎**——真要上须先拿监察数据回答可成交性，再走一次「回测 → 复验 → 实盘」完整流程。
      验收：`go test ./internal/... -race` 全绿 + `TestParityBacktest` 逐位一致
      （t150 3638/1220/93.9344%/+16.0226U、t60 2414/568/99.1197%/+15.7562U、listen
      347/347/97.9827%/+3.8959U、合计 6399/2135/3640/3/95.9719%/+35.6747U）——
      ⚠️ **这组 pin 已于 2026-09-26 被决策 #26 更新**为 6412/2133/3640/3/96.061885%/+35.092748U
      （t150 3638/1208、t60 2426/577、listen 348/348）。
      （回顾：当时 `internal/dashboard/tail/index.html` 有一处未提交的标题改名
      「扫尾盘 监控」→「Tail 监控」会让 `TestTailRoutes` 失败——已在 `f4eaf01` 连同路由
      测试断言一起提交，现网已一致。）

25. **ETH 服务器数据复核：撤空复现 + 扫尾盘门槛不可跨标（阈值重标定待办）**（2026-09-25，
    见 `docs/eth_data_review_2026-09-25.md`）。用户下的首批**服务器**数据
    （`data/eth`，23 窗 / 13:20Z~15:10Z / 约 2 小时，`./down.sh eth` 取）复核结论：
    - **结构可与 BTC 数据同构使用**：字段逐字一致、每窗 300 tick、零断档、
      `book_latency_ms` p50 11ms（>300ms 仅 0.06%）、TWAP/Binance 零价 0、
      22/23 窗双 `push`。
    - ⚠️ **窗首瞬态**：每窗前 1~4 个 tick（rem 295~298）四档全零且 `book_ts=0`
      ——订阅首份快照未到，不是撤空。落在策略判定区之外（狗要 rem>180、tail 要 rem≤150）。
    - ✅ **决策 #21 的「尾盘整侧撤空」在服务器数据上复现**：30 段 / **1482 tick = 21.5%**，
      起始 rem ∈ [0,137]、22/30 到闭市、段长中位 56s。四条证据：① **镜像 100%**
      （1534 个缺腿 tick 全是「赢家卖腿+输家买腿」成对为 0，零例外）；② 互补恒等式
      `yes_bid+no_ask ≡ 1` 的 p99 偏差 **0.0000**；③ **30 段里 29 段的起点热门侧已
      ≥0.96（中位 0.98）**——与市场定局精确耦合，网络抖动不会这样发生；④ 数据来自
      服务器 ⇒ **决策 #23 §5.5 的「本机网络」假设排除**。⚠️ **证的仍是「现象」不是
      「比率」**：持有空快照的时间可能长于市场实际，21.5% 是**上界性质的指示**。
    - **对策略的量化影响**（这条是本次真正的发现）：
      - **狗@0.2 不受影响**——rem>180 的四档齐全率 **99.08%**，缺腿全落在不交易的尾盘；
      - **扫尾盘 ⑤ 受致命影响**——热门侧 ≥0.80 的 tick 里热门侧**没有卖单**的占
        `rem≤150` **50.4%** / `rem≤60` **82.6%** / `rem≤30` **90.8%**；
      - **止损腿**（决策 #24）出场那一刻亏损侧 bid=0 占 **80.5%**（rem≤60）
        ⇒ 那 +10.44U 是**硬上界**；
      - ⚠️ **决策 #22 的 bid 兜底在 BTC 回测数据里根本无法被触发**（542800 tick
        缺腿 0 次，是旧守卫丢消息的**构造性产物**，决策 #23 已查明），
        而 ETH 上它是**主要**路径（82.6%）⇒ **纸面「有效价即成交」在 ETH 上等于假成交**。
      - 正面数：**有卖单时**深度够（`rem≤60`，n=278，前 5 档中位 230 股，<5 股的仅 0.4%）
        ⇒ 瓶颈是「有没有人卖」不是「量够不够」。
    - 🔴 **扫尾盘门槛不可跨标（本次要记的待办）**：7 个键里 5 个无量纲/时间可迁移
      （`t150_rem`/`t60_rem`/`price_min`/`max_book_lat_ms`/`stake`），
      **`dev_min_usd 63` 与 `sigma_min_usd 40` 是 BTC 价格的标定量**——ETH σ 中位
      **5.36 美元** vs BTC **62.56 美元**（引擎同一定义：前 ≤18 窗 `|close−open|/open`
      的均值），⇒ ETH 上分支 A 要求 11.7σ 的位移（ETH 位移 p90 才 10.00 美元）、
      分支 B 要求 σ ≥ 148.6 基点（ETH σ p90 仅 22.8）——**两条腿都不可达**。
      **⇒ 等 ETH 采集 ≥14 个完整日后单独立项做阈值重标定**（≈4032 窗 + tick 覆盖率
      ≥99% 为触发条件；**触发前不做任何阈值分析**）。预设判据/决策表已写死在
      `docs/eth_data_review_2026-09-25.md` §5，其中**第一步是「可成交性先行」**：
      算 `rem≤60` 决策点的热门侧 `ask > 0` 占比，**< 50% 直接判「ETH 不可执行」**
      （不进入调参）。⚠️ 两个折算锚点（按基点 63→2.18 / 40→1.39 美元；按 σ 63→5.40 /
      40→3.43 美元）**差 2.5 倍**，只作交叉验证，不作结论。前置工作很小：oracle
      `23_tail_integrated.py` 只有三处写死（`:53` `DEV_USD`、`:54` `SD_MIN_USD`、
      `:196` `load_events("data/btc")`）。
      ⚠️ ETH 版回测有一条**有意不同**的口径：**交割必须只用「热门侧有卖单」的子样本**
      （`hot_src == ask`）——ETH 数据做得到，BTC 历史数据做不到。
    - **狗@0.2 上 ETH 是另一件事**（不在本待办内）：浅洞腿是 σ 归一的、结构可迁移，
      但 ETH 的**相对**波动是 BTC 的 **2.5 倍**（σ 19.95 vs 8.10 基点）
      ⇒ 浅洞带要在 ETH 数据上重跑分桶，另行立项。
    - **2 行结算修正**（14:00Z / 14:05Z）来自**同一个边界 14:05:00** 的推送没到
      （一条缺 close、一条缺锚），差官方 0.42 美元；`outcome` 均未翻转。
      成因是 `cmd/collect/main.go:431` 的兜底初值 `Latest()`（到达口径）+ 官方修正队列
      ——**采集器的预期行为，不是缺陷**（⚠️ 引擎侧相反：那 20s 取不到锚就整窗不观测）。
      合并前须跑 `cmd/compact` 并回，否则下游会读到带 0.42 美元锚误差的事件行。

26. **T=150 段价格腿改严格大于（`> 0.80`）：三段之间唯一的规则差异**（2026-09-26，用户
    决定；报告 `docs/tail_integrated_2026-09-24.md` §6）。**只改 T=150 段**——T=60 的 ⑤
    与监听段的 ② 一字未动（仍 `≥ 0.80`），其余腿（dev/σ/时间腿/门控）全部不变。
    - **依据 = 0.80 这一格在两个样本里都是唯一负 EV 档**（用户先看实盘数据提出）：
      **实盘**（`data/tail-live` 09-24~25，真实 GTC 挂单，stake 10U，205 笔 +55.78U）——
      入场价**恰为 0.80 的 4 笔全在 t150 段，WR 50% / −14.38U / EV −3.59U**，而同批
      `> 0.80` 的 201 笔 WR 98.51% / +70.15U；**14 天回测**同分桶 n=17 WR 76.47% −1.50U
      （EV −0.0882U/注）也是整条价格梯度上唯一的负档（0.81~0.85 起转正 +0.0299U）。
      ⚠️ **两个样本都薄（4 笔 / 17 笔）**，一致的是「它是最差档」这个**排序**，不是亏 14U
      这个量；回测里那 12 笔 t150 恰好 0.80 的信号其实是 **+1.00U**（正的）。
    - **14 天重跑（oracle 23 同步改，parity 逐位钉）**：t150 信号 1220→**1208**、t60 568→
      **577**、监听 347→**348** ⇒ 合计 2135→**2133**、WR 95.97→**96.06%**、P&L +35.67→
      **+35.09U**、行数 6399→**6412**。**机制**：t150 那一格被拦下 ≠ 该窗不下单——落一条
      `price_low` 判定行后按链走到 t60（多数）或监听段**以更高的价重入**，故 12 笔里 9 笔回来、
      1 笔落监听段、2 笔彻底消失。**配对 Δ（日级 bootstrap 2000 次 seed 42）= −0.58U，
      95% 区间 [−6.26, +5.59] 含 0 ⇒ 与基线不可区分**——这不是 P&L 增益型改动，买的是
      「去掉唯一负 EV 档」的口径干净 + 实盘那 4 笔的形态；与「参数不可调」约定不冲突的理由
      只有一条：**它是用户对规则本身的决定，不是拿回测网格挑出来的参数**（`price_min` 阈值
      一字未动，改的是算子）。
    - **实现**：`internal/tail.PriceLeg`（纯函数, `stage == StageT150 ? > : >=`）是本包**唯一**
      的段相关腿；`EvalRules` 增 `stage` 参数（**必须传**——否则行里的 `rules.price` 会与
      `reject_reason` 自相矛盾：`price = false` ⇔ `price_low` 这条自洽不变量由 parity 逐行断言）。
      比较符**故意不做成配置键**：`price_min` 只给阈值，算子由段决定（做成键会让人按行情调它）。
    - **验收**：`TestPriceLegStageOperator`（3 个 stage × {0.79, 0.80, 0.81} 算子矩阵 +
      「同一批输入在 t150/t60 上结论相反」的漏传哨兵）；parity 表更新为 6412 / 2133 / 3640 /
      3 / 96.061885% / +35.092748U（旧值留在 git 与测试注释里）；`go test ./internal/... -race`
      全绿（含 tail parity 全量重放 ~100s）。
    - ⚠️ **`python/v4/26_tail_integrated_stoploss.py` 保持冻结**在旧口径（文件头有冻结声明）：
      它是 09-25 止损评估的存档，四个数字与 Δ **不是**现行引擎的数字——要重跑评估须先把它
      的 r5 改成 `strict_price=True` 并同步 ORACLE。**flip（狗@0.2）侧零改动**（btreplay
      625 笔红线不受影响）。

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
| `runtime.dashboard_addr` | `""` | **flip 进程**的 Dashboard 监听地址（空 = 不启动；`:8090` 直接给端口） |
| `runtime.tail_dashboard_addr` | `""` | **tail 进程**的 Dashboard 监听地址（独立键——两个进程各自的 listener，详见决策 #18） |
| `runtime.output_dir` | `data/v4` | 观测 JSONL 输出目录（live 建议独立目录，见启动时的 paper/live 混行告警；**flip 与 tail 可共用**——三前缀各自不撞） |
| `runtime.slug_prefix` | `btc-updown-5m` | 市场 slug 前缀 |
| `tail.*` | 见下 | 扫尾盘 7 键（`t150_rem 150` / `t60_rem 60` / `price_min 0.80` / `dev_min_usd 63` / `sigma_min_usd 40` / `stake 2` / `max_book_lat_ms 300`）——⚠️ **全部不可调**，见决策 #17/#22 与 `docs/tail_sweep_2026-09-22.md` §4.3。两条时间腿 = 三段链的两个**下单点**（旧名 `frame_rem`/`rem_start` 已废，语义已从「帧/快照」变成真判定）；⚠️ `price_min` 只给阈值，**比较符随段而变**（T=150 段严格大于、T=60/监听段 ≥，见决策 #26 与 `internal/tail.PriceLeg`） |

`cmd/tail` 只认 **4 个 flag**（`-config` / `-mode` / `-stake` / `-dashboard`，语义同
flip 那四个，`-stake` 覆盖的是 `tail.stake`、`-dashboard` 覆盖的是
`runtime.tail_dashboard_addr`）——没有 `-encrypt`（用 flip 的那个）。两族的
`-dashboard` 是**两个独立的键**：同机并行跑时给不同端口（如 flip `:8090` /
tail `:8091` ），各自的静态目录与 API 前缀独立（决策 #18）。

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
