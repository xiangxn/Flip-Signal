# 高频数据重采计划（2026-08-16，全新数据格式 v2）

> 状态：计划定稿，实施中。**全新采集程序 + 全新数据目录 + 全新事件格式**，
> 与旧 lab（5s 快照、data/btc_5s）完全隔离，互不写入、互不依赖。
> 目标：为翻转/flip 线的"both/only 类别预测"提供微观信息源，
> 2-4 周后重走"基率 → 因子 → 回测 → 实盘"四步法。

## 1. 背景：为什么必须重采数据

翻转线重推导的完整结论（见 `docs/twap_flip_rederivation_2026-08-15.md`）：

- 整窗仅一侧穿越 0.7（only 类）→ 该侧 ~97%+ 赢（follow 方向，EV 上界 +0.19/股）
- 两侧都穿越（both 类）→ 首个穿越侧只赢 ~20%（**翻转方向，EV 上界 +0.51/股**）

> ⚠️ **实盘条件尚未成立**：only/both 是**整窗（未来信息）分类**，+0.19/+0.51
> 只是信息上界，不是策略收益——决策时刻并不知道对侧后来会不会反穿 0.7。
> 当前实时可知的近似版本（follow 的 wait 模式 B：rem≤60 单侧确认，EV
> +0.01~0.02/股；od0_pe4 过滤：EV +0.02~0.07/股）与上界差距巨大，且数据
> 缓冲薄（1.3~6.9pp），无一达到可实盘的标准。**本计划的全部意义**：用 1s
> 微观数据（PM 成交流向、1s OFI、reprice 速度）把"类别预测"变成可实盘的
> 实时判定条件，2-4 周数据积累后按四步法重新检验。

特征挖掘已穷尽旧 5s 快照能算出的所有特征族。唯一一致的正 EV 线索
（spot_ret 反向，n=31）卡在分辨率上：确认期只有 2 个数据点。确认期特征
od>0.05 统计力最强（翻转率 +13.8pp）却被 10 秒内的盘口 reprice 吃掉——
证明 PM 市场 10 秒内消化了价格层面的信息，剩下的钱在**市场没看见的微观
结构**里，而 5s 快照恰好把它丢了。

## 2. 旧数据的死穴（本计划补齐项）

| 旧数据（data/btc_5s, 5s 快照，已移走） | 丢失的信息 → 新格式补齐 |
|---------|-----------|
| Binance 5s 点价 + 5s 聚合 flow/vol | 5 秒内微观路径 → **1s OHLC + 主动买卖量 + tick 数** |
| PM 只存 yes/no **best bid** | 无 ask、无深度结构 → **1s bid/ask + top5 数量** |
| PM 成交 tape | 从未采集 → **price_change 逐笔（side/size/price/best）** |
| 盘口 reprice 速度 | 无时戳 → **book_ts + latency** |
| TWAP 5s 采样 | → **每 tick 附 twap_price + twap_age_ms** |

## 3. 新数据格式 v2（全新目录，与旧数据完全隔离）

### 3.1 目录与文件

```
data/btc/                          # 新格式 v2 输出目录
└── events_YYYY-MM-DD.jsonl       # 每行 = 一个 5 分钟窗口的完整事件
```

> ⚠️ **启动前必须先把旧的 5s 快照目录 `data/btc` 改名移走（如 `data/btc_5s`）**：
> 新旧格式文件名相同（events_YYYY-MM-DD.jsonl），直接写会把两种格式混进同一文件。

- 每个窗口一行 JSON，包含窗口元数据 + 1s tick 数组 + 聚合成交数组
- 按日切分、`O_APPEND` 追加，重启不丢数据
- **不写旧格式、不读旧数据、不动旧 lab 代码** —— 旧管线保持原样继续运行

### 3.2 事件 JSON 结构

```json
{
  "condition_id": "0x...",
  "slug": "btc-updown-5m-1785957300",
  "start_time": 1785957300,
  "twap_open_price": 62834.65,        // 官方开（窗口边界起后台轮询修正，同旧 lab）
  "twap_close_price": 62738.22,       // 官方收（窗口结束后轮询修正）
  "outcome": 1,                       // 0=Up 1=Down（TWAP 官方口径）
  "binance_open": 62896.01,           // Binance 5m K 线开盘（研究对照）
  "ticks": [                          // 1s 行 × ~300
    {
      "ts": 1785955824927,            // unix 毫秒
      "rem": 76,                      // 剩余秒数
      "bin": {                        // P0-1 Binance 1s 聚合
        "price": 62841.87,
        "buy_vol": 1.586,             // 本秒主动买量
        "sell_vol": 0.567,            // 本秒主动卖量
        "ticks": 12,                  // 本秒成交笔数
        "bid5": 7.86, "ask5": 5.41,   // depth20 前 5 档数量和
        "bid10": 12.3, "ask10": 9.87  // depth20 前 10 档数量和
      },
      "pm": {                         // P0-2 PM 盘口 1s 快照
        "yes_bid": 0.62, "yes_ask": 0.63,
        "yes_bid_top5": 1500, "yes_ask_top5": 800,   // top5 数量（股）
        "no_bid": 0.37, "no_ask": 0.38,
        "no_bid_top5": 900, "no_ask_top5": 1400,
        "book_ts": 1785955824000,     // 盘口更新时戳（reprice 速度）
        "book_latency_ms": 120        // 传输延迟
      },
      "twap": {"price": 62778.24, "age_ms": 664}     // TWAP-60 流采样
    }
  ],
  "trades": [                         // P0-3 PM price_change 聚合（每 token 每秒一行）
    {
      "ts": 1785955825000,            // 本秒桶起始（unix 毫秒，与 ticks 对齐）
      "rem": 75,
      "token": "YES",                 // 按订阅 token 映射
      "n_buy": 3, "buy_size": 150,    // 本秒主动买笔数/量
      "n_sell": 1, "sell_size": 20,   // 本秒主动卖笔数/量
      "max_size": 100,                // 本秒最大单笔（大单检测）
      "vwap": 0.72,                   // 本秒成交量加权均价
      "last_price": 0.73,
      "best_bid": 0.71, "best_ask": 0.73
    }
  ]
}
```

⚠️ **trades 不落原始逐笔数据**（数据量过大）：price_change 在内存中按
token × 秒 聚合，只落聚合行。分析所需的主动买卖不平衡（OFI）、大单占比、
vwap 均可从聚合行恢复；hash/event_ts 等逐笔字段不保留。

### 3.3 数据量估算

- ticks：~300 行/窗口 × 288 窗口 ≈ 8.6 万行/天，每行 ~250B → **~22MB/天**
- trades 聚合：≤ 600 行/窗口（2 token × 300s）≈ 17 万行/天，每行 ~120B → **~5MB/天**
- 合计 < 30MB/天，7×24 无压力

## 4. 数据源与采集逻辑（全新 Go 程序 `cmd/collect`）

| 数据 | 来源 | 方式 |
|------|------|------|
| Binance 1s 聚合 | 现有 `aggTrade` + `depth20` WS 流（复用 `internal/feed.BinanceAdapter`，仅加一个只增不减的 `TradeCount` 字段用于每笔计数，无行为变更） | 1s 定时器：`ConsumeVolume()` 取本秒主动买卖量 + `LatestData()` 取价格/深度/笔数 |
| PM 盘口 1s 快照 | SDK `MarketMonitor` book 流（v0.6.27） | 1s 采样最新 book（bid/ask/top5/时戳） |
| PM price_change | SDK `MarketMonitor.SubscribePriceChange()`（**v0.6.27 已支持**，用户已更新；A/B 实测 price_change 上行与 customFeatureEnabled 无关，false 即可） | 内存按 token×秒 聚合后落盘（不落原始逐笔），按订阅 token 映射 YES/NO |
| TWAP-60 | SDK `CryptoPriceMonitor`（复用 `internal/feed.TwapAdapter`） | 每 tick 附采样 |
| 官方 TWAP 开/收盘 | crypto-price 接口（复用 `feed.PollOfficialOpenPrice/ClosePrice`） | 开盘边界轮询修正、收盘结束后修正 |

市场循环沿用旧 lab 的对齐逻辑：5 分整边界 → gamma 按 slug 取市场 →
订阅 token（先退订旧的）→ 1s 采集 300 秒 → 封存写入。

## 5. 代码组织

```
cmd/collect/main.go          # 新采集程序入口（市场循环 + 接线）
internal/collect/
├── writer.go                   # 【抽出的通用工具】按日切分 JSONL Writer
│                               #   （bufio + O_APPEND + 互斥 + 日切，任意结构，
│                               #   从 lab.Writer 泛化抽出，旧 lab 不动）
├── types.go                    # HFTick / TradeAgg / Event 结构
├── book_utils.go               # 纯函数: top5 数量、best bid/ask 提取
├── trade_bucketer.go           # price_change 按 token×秒 内存聚合
└── market_utils.go             # gamma 市场 JSON → YES/NO token 解析
```

- 旧 `cmd/lab`、`internal/lab`、`internal/feed` 一律不改（feed 只读复用）
- 引擎照常纸面运行，与新采集程序无耦合

## 6. 采集周期

- **2 周起步**：~2880 窗口，both 类 ~800 个，spot_ret 反向事件 ~60-100 个，
  第一次 checkpoint 评估
- **4 周更稳**：覆盖涨/跌/震荡至少各一段 regime（当前所有结论只有下行
  regime 数据，这是最大隐患）

## 7. 验证计划（数据到位后，沿用四步法）

1. **基率复核**：新数据重算翻转基率与 only/both 结构，确认老结论可复现
   （管线 sanity check）
2. **因子重挖**（新特征族，按优先级）：
   - PM 成交 tape：穿越后 10s 主动买卖不平衡、大单占比 → 预判 both/only
   - Binance 1s OFI / micro-zigzag：确认期形态（加速度、首反转时刻）→
     精确择时，抢在 reprice 前
   - PM top5 深度不平衡 + reprice 速度（book_ts 间隔）：穿越时的承接结构
3. **回测检验**：新数据内部按天 split 两半同号才算数（老纪律）
4. **实盘**：follow 线（od0_pe4）先纸面验证成交口径；翻转线因子达标后再上

## 8. 实施清单

1. ✅ SDK v0.6.27（price_change 支持）—— 用户已更新
2. ✅ `internal/collect/`：writer.go（抽出通用 JSONL Writer）+ types.go + book_utils.go + trade_bucketer.go + market_utils.go
3. ✅ `cmd/collect/main.go`：新采集程序（市场循环 + 1s ticker + price_change 聚合落盘）
4. ✅ `go build ./...` + 实采验证：完整窗口 ticks=287 / trades_agg=578（原始 8.6 万笔聚合）；
   A/B 实测 customFeatureEnabled 与 price_change 无关（默认 false）
5. ⬜ 正式采集 2 周 → checkpoint → 扩展 `python/flip/analyze.py` 与 `follow/analyze.py` 新特征族

## 9. 运行方式

```bash
# 仅需 https_proxy（SDK REST 与 gorilla WS 均走环境代理，无需 POLYMARKET_PROXY）
# 启动前: mv data/btc data/btc_5s   # 旧 5s 数据先移走，避免同名混写
https_proxy=http://127.0.0.1:1087 go run ./cmd/collect -output data/btc
```
