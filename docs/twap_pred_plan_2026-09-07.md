# v5 TWAP-pred 回测计划（2026-09-07 落盘）

策略构思见 `docs/twap-pred.md`（v5 分支，未提交）；本文为其回测实现计划（python/v5，data/btc）。

## 一、策略回顾（摘要）

Polymarket BTC-5m（Chainlink TWAP-60 结算）。当某侧 ask ≤ 0.3 时密切观察，用
**线性速度 v + 结算窗历史滚出** 的统一离散公式实时预测最终结算 TWAP 相对 Open
的穿越量 D_pred，D_pred 越过安全边际 M 才买入该便宜侧。

- v1：未来路径 = 线性 `P̂_k = P0 + v·k`（唯一建模核心；后续可升级大单/订单流/盘口/加速度路径）
- doc 原话要点：已知部分用真实 Binance 历史价格；结算输赢 = 官方 outcome（Chainlink 口径）

## 二、数据事实（data/btc，已核实）

- v2 格式：`events_YYYY-MM-DD.jsonl` 每窗口一行 + `settlement_correction` 行
  （须过滤/合并）；每窗 300 个 1s ticks，ts 覆盖 start+1001ms → start+300000ms，
  rem = floor((END−ts)/1000)，末 tick（ts==END）为 rem=0 重复采样。
- 覆盖：12 个整天 × 288 窗 + 08-18(206) / 08-31(91) 两个半天（08-18~08-31，14 天，
  与 v4 dog@0.2 回测同期）。无重复窗口。
- tick 字段：
  - `pm.{yes,no}_{bid,ask}` + `book_latency_ms`（实测 ≤172，无缺失）
  - `bin.price`（Binance 逐秒，无缺失；秒差中位 0、p95 ≈3.65$/s → v 常为 0，
    必须用真实 ts 差估计）
  - `twap.price`（Chainlink 滚动 TWAP-60，末 tick 与 `twap_close_price` 逐位相等
    ⇒ 结算窗 = 以 END 为终点的最后 60 秒）
  - `bin.{buy_vol,sell_vol,ticks,bid5,ask5,bid10,ask10}`（增强特征输入）
- 事件：`twap_open_price`（Open 锚）、`twap_close_price`、`outcome`（0=Up 1=Down，
  官方口径，lib 已 correction 覆盖）、`trades` 数组（PM 侧成交汇总，v1 不用）。
- 运行环境：`python/venv/bin/python`（numpy 2.5.1 / pandas 3.0.5）。

## 三、口径映射（与 doc 离散公式 1:1）

连续时间化（真实 ts 差，秒级 bin 对齐数据网格）：

- END = start_time + 300s；结算窗 = 60 个 tick（`rem ≤ 59 且 ts < END`）。
- 入场 tick i：R = (END − ts_i)/1000；P0 = bin.price_i。
- **v(L)**：tick i 前 L 秒内 bin.price 对连续 ts 的 OLS 斜率（$/s）；样本数
  < max(3, 0.8L) 该 (tick, L) 无效（doc §十：用实际 ts 差，勿假设严格 1s）。
- TWAP_pred = (窗内 ts≤ts_i 真实 tick 的 bin.price 之和
  + 窗内 ts>ts_i 的 (P0 + v·Δt) 之和) / 60；
  D_pred = TWAP_pred − twap_open_price。
- R ≥ 60 自动退化纯外推（等价 doc `P0 + v(R−29.5)`）；R < 60 已知部分随 tick 增长。
- 全程 Binance 口径预测 vs 官方 outcome 结算——与 dog@0.2 家族「现货预测腿 vs
  官方结算」同构；Binance↔Chainlink 系统基差影响在 v1 注释标注，留后续变体分析。

## 四、入场语义（约定，偏离 doc 处显式标注）

- 侧别：买 yes 需 `D_pred > +M 且 yes_ask ≤ 0.3`；买 no 需 `D_pred < −M 且 no_ask ≤ 0.3`。
  （两侧同 tick 双 ≤0.3 数学上不可能——yes_ask≤0.3 ⟺ no_bid≳0.7，无需特判。）
- **方向腿（2026-09-07 并入为第 3 核心腿）**：速度 v 带方向——buy yes 需 `v ≥ 0` /
  buy no 需 `v ≤ 0`；速度若朝该侧恶化方向（背向下注侧）则该 tick 禁止入场、
  观察延续至 v 转好或窗口结束（v==0 中性放行）。用户确认此为策略核心
  （"动态计算速度，方向向恶化方向不能下注"）。无腿口径（并入前）保留为 B′ 对照。
- **逐 tick 连续观察**：首个「ask≤0.3 且 D_pred 越 ±M 且方向腿过」tick 入场，
  fill = 该 tick ask——doc 字面语义（"开始密切观察"意味着命中可在观察段第 2+ 秒），
  区别于 dog@0.2 的「首触即判」（首触不重试是 dog 回测/引擎刻意口径，不迁移到本策略；
  方向腿拒绝只是跳过该 tick，不入场即停）。
- **每窗每侧最多一单、每窗最多一单**：先命中侧先到先得，入场即停
  （为将来落 Go 引擎 Watching→Done 单信号惯例铺路）。
- 数据门控（镜像 python/v4/01_backtest_r1.py）：pm 四字段 > 0、book_latency ≤ 300ms、
  rem ≥ 1（剔除结算边界 tick）。
- 收益口径与 v4 全同：2U/注，shares = stake/fill，赢 → shares−stake，输 → −stake；
  输赢按官方 outcome。

## 五、产出与报告

### python/v5/01_backtest_twap_pred.py（主线，自包含）

报告节（全中文）：

- **A 数据概览**：窗口/事件数；ask≤0.3 观察 episode（连续 tick 段）分侧计数
  （全部 & rem≥60 子集）——校准信号频率认知（收盘前常态性 ≤0.3 需注意）。
- **B 网格主线决策表**：(look × M)（默认 5×7=35 格）每格一行：
  n / WR(wilson) / 均 fill / EV(U/注) / P&L / 日正 / h1:h2 / R<60 已知滚出族 n；
  P&L argmax 行标注「in-sample 仅参考」（复验思路同 dog@0.2 的 09-15 OOS）。
  **现行口径 = 三腿（含方向腿）**；CSV 明细行 = 三腿口径。
- **B′ 方向腿对照**（2026-09-07 新增）：M=0 各 look + argmax 行 + L=15/M=10 同格
  输出「无腿（并入前历史口径）」与「现行（含方向腿）」两行——量化第 3 腿剔除的
  恶化方向单贡献（补扫 `scan_legless`，不进主表/CSV）。
- **C 分桶统计**（doc §十.4；对 argmax 行 + 自然对照 L=15/M=10 两行）：
  rem 桶（≥180 / 60–179 / <60 已知滚出）× |v| 桶（=0 / (0,0.05] / (0.05,0.2] / >0.2 $/s）
  的 n/WR/EV，加逐日 P&L。
- CSV 明细 `python/v5/data/trades_twap_pred.csv`：
  date, event_start, tick_i, side, rem, look, margin, d_pred, fill, v, known_n,
  settle_won, actual_d(twap_close−twap_open) + 入场 tick 特征 buy10/sell10/buy120/
  sell120、depth_bid5/depth_ask5（02 与未来 OOS 免重扫数据）。

### python/v5/02_features_aux.py（增强探索，纯 CSV 统计）

- **预测校准审计**（M=0 行）：D_pred 符号一致率 vs actual_d 符号（按 rem 桶）、
  corr(D_pred, actual_d)——线性假设质量检视。
- **辅助特征条件评估**（M=0 基线，逐 L）：v1 命中单上加条件看 WR/EV 增量
  （doc §十.3 + §五，只作 v2 P̂ 升级/M 动态化线索，不进 v1 头条）：
  - 反向大单：buy/sell 10s vol 相对 120s 中位数 spike 倍数 θ（参数化）——方向与
    穿越方向相反侧的放量；
  - 订单流同向：10s 净买量符号 = 穿越方向；
  - 盘口失衡同向：bid5/(bid5+ask5) 偏 0.5 方向 = 穿越方向；
  - 输出：基线 / 各条件 / 组合 的 n、WR、EV/注。

## 六、参数（argparse，默认值与出处）

| 参数 | 默认 | 说明 |
|------|------|------|
| `--data` | data/btc | 事件目录（lib.load_events） |
| `--trigger-ask` | 0.3 | 观察/入场阈值（doc §二：0.3 更宽松；可试 0.25/0.2） |
| `--stake` | 2 | 每注 USDC（与 v4 同口径） |
| `--looks` | 5,10,15,20,30 | v 估计回看秒数（doc §十 未定，网格扫描） |
| `--ms` | 0,5,10,15,20,30,50 | M 安全边际档（$，doc §四「优化重点」） |
| `--lat` | 300 | pm book_latency_ms 上限（v4 同） |

## 七、验证

1. 两脚本退出码 0；01 内置断言：每窗每格 ≤1 单、fill∈(0, trigger]、
   v/known_n 有限、结算窗 tick 数恒 60。
2. 防回看泄漏：known/future 集合仅由 ts/rem/END 决定；outcome/twap_close 只进
   结算与 actual_d，不进任何预测特征（v、known、d_pred）。
3. `--trigger-ask 0.2` 抽查复跑（0.2 事件 ⊂ 0.3 事件，量级 sanity）。
4. CSV 行数 = 报告 n；02 结果与 01 的 M=0 自算一致。
5. 结论与 dog@0.2 参考线对比（n=625 / WR 24.6% / EV +0.633U/注 / +396U/14 天）。

## 八、执行与规范

- 不动 Go 引擎、不改 data/btc；提交待用户确认，遵循仓库规范
  （分支 v5；中文提交 + `python:` 前缀；无 Co-Authored-By）。
- python/v5/data/ 被 .gitignore 的 `data` 规则忽略（同 python/v4/data 待遇）——
  CSV 本地保留，脚本入库。
