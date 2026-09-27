# cmd/tail 兼作数据采集器 —— 事件 v2 落盘 `data/events`（2026-09-27）

**一句话**：常驻的扫尾盘进程（`cmd/tail`）顺手把每秒原始采样按**数据格式 v2**（与
`data/btc` 逐字段同构）落到 `runtime.events_dir`，默认开启（`"data/events"`）。
不新增进程、不新增 flag、交易路径一行未改。

**动机**：BTC 的原始采集（`cmd/collect` → `data/btc`）自 2026-08-31 起已停，
只有 `tail_*` 派生的观测行 ⇒ 离线研究（止损成交换手、成交量、OFI…）无米下锅。

**代码**：`cmd/tail/events.go`（采集器）+ `cmd/tail/main.go`（4 处接线）+
`cmd/tail/events_test.go`（单测）。决策条目见 CLAUDE.md **决策 #27**。

---

## §1 与 `data/btc` 的口径对齐（逐条有据）

| 项 | 口径 | 依据 |
|---|---|---|
| `anchor_source` | feed 词表**必须翻译**：`feed.AnchorSourceStream → "push"` | 两个包的 `"stream"` 同名不同义（`feed` 的 = 精确边界推送；`collect` 的 = 到达口径采样）。不翻译则校验 G 段把它归进 legacy 分档，且每窗白跑一次官方修正轮询 |
| `close_source` | 收盘推送在手 = `push`（与官方逐位同源），否则流值 `stream` | 决策 #15 / #19 |
| close 兜底 | `twap.Latest()` 为 0 时回退**锚价** | 校验 A 段 `twap_close_price > 0` 硬 FAIL；`|close−open|=0` 只是一个振幅为 0 的窗，比整窗丢弃温和 |
| `outcome` | `close >= open → 0`（**平局算 Up**） | 校验 H 段写死此式；≠ `internal/settle.Outcome`（严格大于） |
| `binance_open` | 初值 = 边界那一刻的现货价，+2s 由 5m K 线开盘价覆盖 | 校验 D 段要求与 K 线开盘**逐位相等** |
| tick 组装 | `LatestData()` **原始值**（不套引擎的陈旧钳零）+ `ConsumeVolume()` 秒增量 + `collect.MakePMTick` | 校验 D 段 `bin.price <= 0` 硬 FAIL；引擎 tick 没有量/深度四档，故**不复用** `lastTick` |
| 成交 | `TradeBucketer`，按**成交自身时戳**归桶、用**被选中那个桶自己的 token 表**映射 YES/NO | 本族的 upTok/downTok 每窗换装（与 `cmd/collect` 的全局 prev 变量不同） |

**顺带修掉的一个隐患**：`feed.FetchKlineOpenPrice` 失败时 adapter 里保留的是**上一窗**的
开盘价（静默陈旧）。改成返回 `float64`（失败返回 0，调用方一律用返回值），
否则会把错值写进 `binance_open` 而 D 段逐位相等直接 FAIL。`cmd/collect` 同源隐患一并修。

## §2 三条落盘红线（宁可留洞，不写脏行）

半个窗的语料比没有更坏——下游按覆盖度/行数做的统计会被静默带偏。故：

1. **元数据不全**：锚 ≤ 0（决策 #15）或 `binance_open ≤ 0` ⇒ 整窗丢弃。
2. **半窗**：`len(ticks) < 293`（常量指向校验脚本 `TICK_MIN`）⇒ 整窗丢弃。
   覆盖「进程迟入窗口」（本族 lateLimit 15s，比 `cmd/collect` 的 2s 宽）与崩溃续跑窗。
3. **迟入**：首 tick `rem < 295` ⇒ 整窗丢弃（tick 数与 rem 身份对不上脚本期望）。

丢失/丢弃**各打一行 ⚠️ 日志**（丢窗必须有痕）；进程关闭时在途窗直接丢弃，不补写半窗。

## §3 已知洞与取舍（写进文档，不修）

- **跳窗 = 语料洞**：`no_sigma`（冷启动 σ 未就绪，最长约 90 分钟）、`late` /
  `no_market` / `no_token` / `dup_record` 这些路径**从不 `BeginWindow`** ⇒ 那些窗一行
  都没有。⇒ **下游必须按 `start_time` 对齐，不能按行数/连续性假设**。
- **格式没有 mode 字段**（同构格式不能加键）⇒ live 与 paper 的事件行在同一目录无法
  区分，要区分得交叉 `tail_*` 行。
- **磁盘**：约 86 MB/日/标的（288 窗 × ~300 KB）；`cmd/compact` 重写峰值约 2×。
- **写入性能**：`WriteUniqueEvent` 每次**全文件扫描**去重，日末单文件几十 MB ⇒ 必须留在
  独立 goroutine（已如此：采样/组装/提交都在该窗自己的 goroutine 里）。
- **一标的一目录**：不要把两个标的、或 `cmd/collect -output` 指到同一目录
  （同 `start_time` 的行会被判重跳过）。`events_dir` 与 `output_dir` 相同只告警不拦。
- **每窗最后一个 tick（`rem=0`）的 PM 四档常常全空**：终点补采发生在 `endTime`，而主循环
  恰在那一刻换装窗口（`upBook, downBook = nil, nil`）⇒ 采样读到的是空簿。**不修**：
  ① 与 `cmd/collect` 的补采时点逐字同源（改时点就是改口径）；② 它落在两族判定区之外
  （tail 最晚 `rem≤60`、flip 需 `rem>180`）；③ Binance 侧量增量照常采到（补采的全部理由）。
  实测 3/900 = 0.33%，与校验脚本预期一致（旧 `data/btc` 也有同一现象，18.68% 的窗）。
- **校验脚本读到的一手分布**（首 3 窗，作为语料健康的锚）：tick 数恒 300、首 tick 偏移
  1.00s、末 tick 300.00s、无断档；`close_source`/`anchor_source` 100% `push`；
  **整侧缺腿 31.78%、全部落在 `rem ≤ 106`**——决策 #21 的尾盘撤空在本语料上直接可见。

## §4 怎么验收

```bash
go build ./... && go test ./internal/... ./cmd/... -count=1     # 含 cmd/tail 的采集单测

# 本机跑 ≥2 个完整窗（观察 [Events] 📥 行与有无 ⚠️ 丢窗）
https_proxy=http://127.0.0.1:1087 go run ./cmd/tail -config v4.config.yaml

# 离线段（结构/tick 覆盖/盘口镜像/成交桶/结算方向导出）
python/venv/bin/python python/v4/24_asset_data_check.py --asset btc --dir data/events --no-net
# 带网段（D binance K 线 / G 官方边界价逐位相等 / H gamma 方向需要代理 + 浏览器 UA）
python/venv/bin/python python/v4/24_asset_data_check.py --asset btc --dir data/events
```

修正行合并（仅在推送缺失的窗才需要）：`go run ./cmd/compact -output data/events --all`
（默认目录是 `data/btc`，**必须显式指过去**）。

## §5 运维

- 关闭采集：配置文件 `runtime.events_dir: ""`（或删键=默认值 `data/events`）。
  启动横幅会打印 `[Events] 📥 原始采集开启: <目录>` 或 `原始采集关闭`。
- 服务器那份手工维护的 tail 配置**没有** `events_dir` 键 ⇒ 走代码默认值 ⇒
  **部署即自动开启**，无需改配置。
- ⚠️ 采集是**尽力而为**：采集侧 panic 只记日志并停用采集（`recover`），
  绝不影响交易；落盘失败只记日志。这是活钱进程，采集永远是次要的。
