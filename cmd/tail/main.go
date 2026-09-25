// Command tail 是「扫尾盘」策略引擎入口（口径文档 docs/tail_sweep_2026-09-22.md，
// 现行三段链见 docs/tail_integrated_2026-09-24.md）。
//
// 与 cmd/flip（狗@0.2）**方向相反、各自独立**：flip 买被砸到 ≤0.20 的冷门侧，本引擎
// 买**热门侧**（有效价高的一侧）——价格腿 ≥0.80，且现货相对锚的位移满足「≥63 美元」
// 或「1σ 折美元 ≥40 且位移 ≥ 1σ」（⑤）；监听段只要求前者（②）。
//
// 数据源与 flip 完全相同：PM CLOB 订单簿（UP/DOWN 四档）+ Chainlink TWAP-60（锚/σ）
// + Binance spot（位移腿；同样由 runtime.slug_prefix 经 feed.Asset 派生）。锚走决策 #15 的精确取锚（边界那一秒的推送,
// 500ms × 40 = 20s 预算）；σ 走 flip.HistState（前 ≤18 已完窗 |close−anchor| 均值）。
//
// ⚠️ 每窗走**三段递进判定链**（2026-09-24 用户决定, a.md；行数 1~3）:
//   - `rem ≤ t150_rem(150)` 的首个可判定 tick → **第一段判定行**（stage=t150）:
//     判一次完整 ⑤, 达标即下单; 不达标 → 等第二段;
//   - `rem ≤ t60_rem(60)` 的首个可判定 tick → **第二段判定行**（stage=t60）:
//     再判一次 ⑤（同一套规则, 只是时点更晚、价格更高）; 仍不达标 → 进监听段;
//   - 此后**每秒** → **监听信号行**（stage=listen）: 判 ②（价格腿 ∧ dev≥63 美元）,
//     达标即下单。被拒的 tick **不落行**（每窗约 60 个, 全落会淹没信号表）。
//
// **任一段出信号即整窗只下一单**（引擎转 Done, 之后不再判定）。全部行 `kind=snap`,
// 由 stage 区分; 旧口径的 frame/scan 两种 kind 已成为 legacy-only（引擎不再产出,
// 只保证 data/ 里的旧行照旧载入与结算）。
//
// 记录四族（前缀刻意与 flip 的 touches_/windows_/winstats_ 不重合）:
//
//	tail_YYYY-MM-DD.jsonl      每窗 1~3 行（三段判定/信号行），观测/成交/结算
//	tailwin_YYYY-MM-DD.jsonl   每完成窗 1 行（σ 重启本地预热的数据源）
//	tailstats_YYYY-MM-DD.jsonl **严格每窗 1 行**（tick 健康度 + skip 原因 + 锚状态）
//	tailhold_YYYY-MM-DD.jsonl  持仓监察: 信号**成交后**逐 tick 1 行（只记录, 不参与
//	                           任何判定——回答「止损真要出场时有没有对手方」,
//	                           见 internal/tail/hold.go 与 docs/tail_stoploss_2026-09-25.md）
//
// 成交（-mode paper|live，两模式共用同一判定与风控闸）: paper = 模拟全额成交
// （shares = stake/hot_ask，精确除）; live = 真实 CLOB **GTC 限价挂单** @ 热门侧有效价,
// **挂到闭市 rem ≤ 0 才撤**未成交余量（2026-09-23 用户口径；flip 的撤单点是策略时间腿
// rem≤180，本族没有那条时间腿——最早的下单点已在 rem≈150，再提前撤会把手里的位置全撤空）。
// 挂单终态由 trading.FillTracker 撤单时查 size_matched 定稿（闭市 +60s 硬截止兜底）。
//
// 结算与统计（2026-09-24 起，a.md 第 3 条）: **所有信号都注册结算**（被风控闸拦下、
// 下单被拒、0 成交的行也照常拿官方 outcome 并在页面上显示赢/输）, 但**未成交不计 P&L**
// （P&L 恒 0、不进胜率、不动日亏熔断与回撤）——见 tail.Record.HasPosition。
//
// Dashboard（internal/dashboard 的 tail 族; 与 flip 面板**各自一个 listener**）:
// 统计卡（判定/信号/胜·负/未成交/待结算/胜率/累计 P&L）、当前窗口的三段进度与热门侧
// 读数、决策快照表 + 信号表（含成交状态与结算结果）。开关 =
// `runtime.tail_dashboard_addr` 配置键或 `-dashboard` flag。
//
// 用法:
//
//	go run ./cmd/tail -config v4.config.yaml                   # 纸面（无需凭证, 不弹密码）
//	go run ./cmd/tail -config v4.config.yaml -mode live        # live 需配置文件里有密文凭证
//	go run ./cmd/tail -stake 5                                 # 单点覆盖（覆盖 tail.stake）
//	go run ./cmd/tail -config config.local.yaml -dashboard :8091   # 纸面 + Dashboard
//	go run ./cmd/tail -config config.local.yaml -dashboard ""      # 显式关掉配置里的地址
package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"flag"
	"fmt"
	"log"
	"math"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/tidwall/gjson"
	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"

	"github.com/necklace/flip-signal/internal/config"
	"github.com/necklace/flip-signal/internal/dashboard"
	"github.com/necklace/flip-signal/internal/feed"
	"github.com/necklace/flip-signal/internal/flip"
	"github.com/necklace/flip-signal/internal/settle"
	"github.com/necklace/flip-signal/internal/tail"
	"github.com/necklace/flip-signal/internal/trading"
)

// windowSec 是 btc-updown-5m 窗口长度（秒）。
const windowSec = 300

// prefetchLead 是市场信息预取提前量：窗口边界前多少秒开始调 FetchMarketBySlug。
const prefetchLead = 20 * time.Second

// lateLimit 是订阅迟到阈值：已落后窗口边界超过该时长则跳过本窗口。
// 本族对迟到的容忍度远高于 flip——最早的判定点在 rem≤150（边界后 150s），故迟到
// 几十秒也不影响它; 但窗口**起点**的锚采样与 σ 对账仍需完整窗口（迟到窗口的锚
// 通道预算已部分耗尽, 且三段链的时点不再对应干净的首个可判定 tick）。沿用 flip 的
// 15s 是保守选择（宁可丢窗，也不拿残缺窗口凑样本）。
const lateLimit = 15 * time.Second

// twapMaxStale 是 TWAP 推送新鲜度阈值: 超过该时长未收到推送则重建订阅
// （TwapAdapter 内建看门狗，与 cmd/flip 同值）。
const twapMaxStale = 2 * time.Minute

// twapLookbackSeconds 是结算口径 TWAP 回看窗口秒数（Chainlink TWAP-60）。
const twapLookbackSeconds = 60

// 精确取锚参数（决策 #15）: 锚 = **边界那一秒**的 TWAP-60 评估值（实测与官方
// openPrice 收敛值逐位相同）。该条推送 p50 +2.0s 到达，20s 预算 = p50 的 10 倍余量，
// 命中即终局；预算耗尽 = 本窗无锚（引擎 anchor ≤0 一行不产出，只落 tailstats）。
//
// 与 flip 的关键差别在**时序余量**: 本族最早的判定行在 rem≤150（边界后 +150s），
// 而取锚通道 +20s 就结束了——**锚在产出任何行之前早已定局**，不存在「前一段用了旧锚、
// 后一段用了新锚」的不一致窗口（引擎侧另有 emitted 冻结做双保险）。
const (
	anchorExactAttempts = 40                     // 精确取锚尝试次数（× 间隔 = 20s 预算）
	anchorExactInterval = 500 * time.Millisecond // 精确取锚尝试间隔
)

// localFreshMax 是 σ 本地预热的新鲜度上限（同 cmd/flip）: 最新已落盘窗口结束距今
// ≤ 该值才可信（= 引擎最近在跑）；停机更久则回退官方网络预热。
const localFreshMax = time.Duration(flip.HistMin*windowSec) * time.Second

// anchorInfo 是本窗锚的可见性字段（tailstats 落盘用）。
// 只由取锚 goroutine 写、主循环在 cancel + join 之后读——channel close 建立的
// happens-before 保证无数据竞争。
type anchorInfo struct {
	src  string // feed.AnchorSourceStream（空 = 本窗未取到锚; 官方段休眠时恒 stream）
	atMs int64  // 该推送的**本地到达时刻**（unix 毫秒; 仅取到锚时非 0）
}

// marketCache 缓存下一窗口的市场信息（稳态预取: 本窗 tick 尾部预取，loop 顶部复用）。
type marketCache struct {
	slug string
	res  *gjson.Result
}

// runtimeState 是主循环/Dashboard 共用的窗口现场快照载体——只背「当前窗口长什么样」
// （引擎/适配器/窗口元/盘口闭包）; 成交编排（执行器/闸/两阶段落盘/风控）收敛在
// Exec（tail.ExecState, 见 internal/tail exec_state.go）。
//
// 与 cmd/flip 的同名类型形制一致, 字段集不同: 本族的窗口进度是**两个一次性闩锁**
// （LatchState）而不是触底观测。mu 保护每窗口换装的字段（Engine/ConditionID/Slug/
// EventStart）: 主循环写（窗口起点 setWindow 换装, 跳窗路径 clearWindow 清空——清空后
// Dashboard 显示「等待下一窗口…」而非上一窗陈旧状态）, Dashboard goroutine 经
// Snapshot 读。
type runtimeState struct {
	mu sync.RWMutex

	Engine      *tail.Engine                            // 当前窗口引擎（首个窗口边界前/跳窗后 nil, Snapshot 判空）
	TwapAdapter *feed.TwapAdapter                       // 锚/σ 数据源
	Binance     *feed.BinanceAdapter                    // 位移腿 spot 数据源
	ConditionID string                                  // 当前窗口 conditionId（窗口起点换装）
	Slug        string                                  // 当前窗口 slug
	EventStart  int64                                   // 当前窗口起点（unix 秒）
	Mode        string                                  // 成交模式: paper/live（构造后不变, live 缺凭证降级为 paper）
	StartedAt   time.Time                               // 进程启动时刻（构造后不变）
	Exec        *tail.ExecState                         // 行编排（HandleDecision 单入口 + LiveSummary; 构造后不变）
	books       func() (*sdk.OrderBook, *sdk.OrderBook) // 当前窗口 UP/DOWN 盘口闭包

	// 锚可见性（取锚 goroutine 写 → 主循环 join 后读, Dashboard 也在读）。
	// 单独一把锁、**不与窗口换装的 mu 嵌套**: 前者在窗口内异步写、每窗清零,
	// 后者只在窗口边界换装——生命周期不同, 混用会让读写面变复杂。
	anchorMu  sync.RWMutex
	anchorSrc string // feed.AnchorSourceStream（空 = 本窗未取到锚; 官方段休眠时恒 stream）
	anchorAt  int64  // 该推送的**本地到达时刻**（unix 毫秒; 仅取到锚时非 0）
}

// Snapshot 实现 dashboard.Snapshotter（Dashboard 每 5s 轮询取快照）。
//
// 窗口读数（热门侧/有效价/dev/sd）在**采样时刻现算**——与引擎落盘行走的是同一组纯函数
// （tail.HotBook / DevUSD / SigmaUSD, 见 decide.go）: 页面上的 dev/sd 必须与
// 落盘行的 dev/sd 同一口径, 否则「为什么这一窗没过 ⑤」会被两个数忽悠。
// 差别只在输入新鲜度: 页面用**当前**盘口/现货, 落盘行用快照 tick 那一刻的值。
//
// ⚠️ Engine 为 nil 的场景: 启动空窗（Dashboard 先于窗口循环开服, 最长等 ~5 分钟才
// setWindow）与跳窗路径（clearWindow——迟到/市场获取失败/token 缺失/防重入/σ 未就绪）
// ——判空, nil 时状态留空（前端显示「等待下一个窗口…」）。
func (rt *runtimeState) Snapshot() tail.LiveSnapshot {
	yb, nb := rt.books()
	pm := feed.NewPMTick(yb, nb)
	twapPrice, twAge := rt.TwapAdapter.Latest()
	bin := rt.Binance.LatestData()

	// spot 显示口径: 未推送显示 0/−1（前端判灰）；有推送则显示最近价与本地接收龄
	// （前端按 >2s 标红——与引擎判 stale 的阈值一致）
	spotPrice, spotAgeMs := 0.0, int64(-1)
	if bin.RxAtMs > 0 {
		spotPrice = bin.Price
		spotAgeMs = time.Now().UnixMilli() - bin.RxAtMs
	}

	rt.mu.RLock()
	eng := rt.Engine // 引擎引用（窗口换装时替换; 计数器/闩锁读取放到锁外）
	engineState := ""
	anchor, histBps := 0.0, 0.0
	if eng != nil {
		engineState = eng.State().String()
		anchor, histBps = eng.WindowAnchor()
	}
	snap := tail.LiveSnapshot{
		Mode:        rt.Mode,
		StartedAt:   rt.StartedAt,
		ConditionID: rt.ConditionID,
		Slug:        rt.Slug,
		EventStart:  rt.EventStart,
		EngineState: engineState,
		Anchor:      anchor,
		HistBps:     histBps,
		YesBid:      pm.UpBid,
		YesAsk:      pm.UpAsk,
		NoBid:       pm.DownBid,
		NoAsk:       pm.DownAsk,
		BookLatMs:   pm.BookLatMs,
		TwapAgeMs:   twAge,
		TwapPrice:   twapPrice,
		SpotPrice:   spotPrice,
		SpotAgeMs:   spotAgeMs,
	}
	rt.mu.RUnlock()

	// 尾盘读数: 热门侧 = **有效价**高的一侧（每侧 ask 优先、ask 空则退 bid; 平局取
	// yes）——与引擎同一实现（tail.HotBook）。dev/sd 需输入齐备才算——缺锚或缺现货时
	// 留 0（前端显示「—」, 不是「恰好为 0」）。
	hotSide, hotPx, hotSrc := tail.HotBook(pm.UpBid, pm.UpAsk, pm.DownBid, pm.DownAsk)
	snap.HotSide = hotSide
	snap.HotAsk = hotPx
	snap.HotSrc = hotSrc
	if spotPrice > 0 && anchor > 0 {
		snap.Dev = tail.DevUSD(snap.HotSide, spotPrice, anchor)
	}
	snap.Sd = tail.SigmaUSD(histBps, anchor)

	// 锚可见性（决策 #15: 精确命中边界那一秒的推送才算 exact）
	ai := rt.anchorInfo()
	snap.AnchorExact = ai.src != ""
	snap.AnchorSrc = ai.src
	if ai.atMs > 0 && snap.EventStart > 0 {
		snap.AnchorArrivedMs = ai.atMs - snap.EventStart*1000
	}

	// 本窗 tick 健康度与四个闩锁放锁外: 引擎自锁（诊断计数, 与本窗同一窗口上下文）。
	// 窗口间（clearWindow 后 Engine=nil）为 nil——前端隐藏本窗统计块、闩锁全灭。
	if eng != nil {
		st := eng.WindowStats()
		snap.Stats = &st
		l := eng.Latches()
		snap.T150Sent, snap.T60Sent, snap.Listening =
			l.T150, l.T60, l.Listening
		snap.SignalSent, snap.AnchorFrozen = l.Signal, l.Frozen
	}

	// live/风控摘要放锁外: Exec 构造后不变且方法内部自锁（Recorder 域, 与窗口快照无关）
	// ——全量观测遍历不阻塞 setWindow/clearWindow 的窗口换装写锁。
	snap.Live = rt.Exec.LiveSummary()
	snap.Risk = rt.Exec.RiskSummary() // 日亏熔断摘要（两模式都填, 与闸判据同源）
	return snap
}

// setWindow 在窗口起点换装引擎与元字段，并清掉上一窗的锚可见性（主循环持有）。
func (rt *runtimeState) setWindow(engine *tail.Engine, conditionID, slug string, eventStart int64) {
	rt.mu.Lock()
	rt.Engine = engine
	rt.ConditionID = conditionID
	rt.Slug = slug
	rt.EventStart = eventStart
	rt.mu.Unlock()
	rt.setAnchor("", 0) // 新窗口从「锚未到手」开始（取锚通道 +20s 内注入）
}

// clearWindow 清空当前窗口快照（语义 = 无窗口进行中），跳窗 continue 路径调用:
// 否则 Dashboard 在最长一个完整窗口周期内停留在上一窗的陈旧引擎/conditionID
// （上一窗本就一行不产出, 清空零副作用）。Snapshot 判 nil Engine, 前端显示
// 「等待下一窗口…」, 与启动空窗同口径。
func (rt *runtimeState) clearWindow() {
	rt.mu.Lock()
	rt.Engine = nil
	rt.ConditionID = ""
	rt.Slug = ""
	rt.EventStart = 0
	rt.mu.Unlock()
}

// setAnchor 记录本窗锚的来源与该推送的本地到达时刻（取锚 goroutine 调用）。
func (rt *runtimeState) setAnchor(src string, atMs int64) {
	rt.anchorMu.Lock()
	rt.anchorSrc, rt.anchorAt = src, atMs
	rt.anchorMu.Unlock()
}

// anchorInfo 返回锚可见性副本（主循环 join 后读 / tailstats 落盘用）。
func (rt *runtimeState) anchorInfo() anchorInfo {
	rt.anchorMu.RLock()
	defer rt.anchorMu.RUnlock()
	return anchorInfo{src: rt.anchorSrc, atMs: rt.anchorAt}
}

func init() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
}

func main() {
	// ── CLI 参数（与 cmd/flip 同形, 去掉 -encrypt）──
	configPath := flag.String("config", "", "配置文件路径（YAML; 空 = 只用代码默认值）")
	dashboardAddr := flag.String("dashboard", "", "Dashboard 监听地址（覆盖 runtime.tail_dashboard_addr; 显式空串 = 本次不开）")
	mode := flag.String("mode", "", "成交模式: paper|live（覆盖 runtime.mode）")
	stake := flag.Float64("stake", 0, "每信号投入 USDC（覆盖 tail.stake）")
	flag.Parse()

	// set 记录「哪些 flag 被显式给出」（用 flag.Visit 而非比零值）: -stake 0 会走
	// 校验报错（响亮）而不是静默回退。
	set := map[string]bool{}
	flag.Visit(func(f *flag.Flag) { set[f.Name] = true })

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	// ── 配置加载（CLI flag > 配置文件 > 代码默认值; 不读环境变量）──
	cfg, err := config.Load(*configPath)
	if err != nil {
		log.Fatalf("[Config] %v", err)
	}
	if set["mode"] {
		cfg.Runtime.Mode = *mode
	}
	if set["stake"] {
		cfg.Tail.Stake = *stake
	}
	if set["dashboard"] {
		cfg.Runtime.TailDashboardAddr = *dashboardAddr // 显式空串 = 关掉配置文件里的地址
	}
	// 校验判的是最终生效值，必须在覆盖之后（config.Validate 同时校验 flip 节——
	// 一个结构体一份配置, 两节都在里面）
	warns, verr := config.Validate(cfg)
	if verr != nil {
		log.Fatalf("[Config] 配置校验失败: %v", verr)
	}
	for _, w := range warns {
		log.Printf("⚠️  [Config] %s", w)
	}

	// ── 资产参数（由 runtime.slug_prefix 派生：slug / Binance 交易对 / Chainlink 符号）──
	// 一处配置驱动三处命名，换标的（eth-updown-5m 等）不改代码，见 feed.Asset。
	asset := feed.AssetFromSlug(cfg.Runtime.SlugPrefix)

	// ── Polymarket 客户端（未写 owner_key 则生成临时密钥，只读运行）──
	cfgSDK := cfg.SDK
	readOnly := false
	if cfgSDK.Polymarket.OwnerKey == "" {
		key := make([]byte, 32)
		if _, err := rand.Read(key); err != nil {
			log.Fatalf("生成临时密钥失败: %v", err)
		}
		cfgSDK.Polymarket.OwnerKey = hex.EncodeToString(key)
		readOnly = true
	}
	client := sdk.NewClient(&cfgSDK)
	if readOnly {
		log.Println("[Tail] ⚠️  配置文件未提供 sdk.polymarket.owner_key —— 只读运行（纸面交易）")
	}

	// ── 成交执行器 + live 分流 ──
	effMode, executor := resolveLiveMode(cfg.Runtime.Mode, cfgSDK, readOnly, client)

	// ── PM 订单簿订阅（SDK MarketMonitor）──
	monitor := sdk.NewMarketMonitor(cfgSDK.Polymarket.ClobWSBaseURL, false, client, false)
	var (
		subMu     sync.RWMutex
		subTokens []string
		tokMu     sync.RWMutex
		upTok     string
		downTok   string
		bookMu    sync.RWMutex
		upBook    *sdk.OrderBook
		downBook  *sdk.OrderBook
	)
	go func() {
		ch := monitor.SubscribeOrderBook()
		for {
			select {
			case <-ctx.Done():
				return
			case book := <-ch:
				// ⚠️ 只丢 nil, **不再丢「单侧为空」的整簿消息**（2026-09-24）。
				// SDK 的 `book` 事件是**整簿快照**（market_monitor.go onOrderBook 原样
				// 解析 bids/asks，空数组就是空）, 空 asks 是市场的真实状态——事件趋于
				// 确定后热门侧的卖单被撤空, 实盘探针（cmd/bookprobe）在闭市前 10~30s
				// 逐秒读到 `asks = []`。旧守卫把它当噪声丢掉, 内存里留下**撤单前那一份
				// 旧簿**（常是 0.99）, 于是引擎与 Dashboard 继续报一个早已不存在的卖价。
				// 现在照存: bestAsk/bestBid 返回 0 ⇒ 该 tick 被四档门控判无效（与回测
				// 宇宙同口径）, Dashboard 显示「—」——「没人卖」如实呈现。
				if book == nil {
					continue
				}
				tokMu.RLock()
				yt, nt := upTok, downTok
				tokMu.RUnlock()
				bookMu.Lock()
				switch book.AssetId {
				case yt:
					upBook = book
				case nt:
					downBook = book
				}
				bookMu.Unlock()
			}
		}
	}()
	// monitor 重启恢复（SDK 清空订阅，无条件重启）
	go func() {
		for {
			err := monitor.Run(ctx)
			if ctx.Err() != nil {
				return
			}
			if err != nil {
				log.Printf("[Tail] ⚠️ MarketMonitor 异常退出: %v —— 5 秒后重启", err)
			} else {
				log.Printf("[Tail] ⚠️ MarketMonitor 干净退出 —— 5 秒后重启")
			}
			select {
			case <-ctx.Done():
				return
			case <-time.After(5 * time.Second):
			}
			subMu.RLock()
			tokens := append([]string(nil), subTokens...)
			subMu.RUnlock()
			monitor.SubscribeTokens(tokens...)
		}
	}()

	// ── Chainlink TWAP-60（anchor/σ 数据源，结算口径）──
	// symbol 后缀: SDK 将资产符号解析为 30s 窗口，须显式 "_60" 才订阅 twap_sixty
	// （TwapAdapter 内部拼 `<symbol>_<windowSec>`）。
	twapAdapter := feed.NewTwapAdapter(client, string(asset.Chainlink), sdk.ChainlinkTwapWindowSixty, twapMaxStale)
	twapAdapter.StartWithMonitor(ctx)

	// ── Binance spot（位移腿现货参考价，不出单；交易对由资产派生）──
	binance := feed.NewBinanceAdapterWithConfig(asset.ApplyBinance(cfg.Binance))
	go func() {
		backoff := time.Second
		for {
			err := binance.Start(ctx)
			if err == nil || ctx.Err() != nil {
				return
			}
			log.Printf("[Binance] ⚠️ 首次拨号失败: %v —— %v 后重试", err, backoff.Round(time.Millisecond))
			select {
			case <-ctx.Done():
				return
			case <-time.After(backoff):
			}
			backoff = min(backoff*2, 30*time.Second)
		}
	}()

	// ── 记录器（tail_ / tailwin_ / tailstats_ 三族，互不干扰）──
	recorder, err := tail.NewRecorder(cfg.Runtime.OutputDir)
	if err != nil {
		log.Fatalf("[Tail] 记录器创建失败: %v", err)
	}
	defer recorder.Close()

	// ── 🔴 未决 GTC 挂单 × 非 live 模式 = 拒绝启动（与 cmd/flip 同一条红线）──
	// resting 行只可能来自 live（paper 即时定稿, 永不落 resting）——那笔真单还在 CLOB
	// 簿上等对手方。paper 模式既不能撤单也不能定稿: 重启后它会一直吃到闭市，行悬在
	// 磁盘上、日亏熔断与 P&L 都看不见它。故宁可拒绝启动，逼运维带凭证以 live 重启接管。
	if effMode != "live" {
		if pend := pendingResting(recorder.Observations()); len(pend) > 0 {
			for _, rec := range pend {
				log.Printf("[Tail] 🔴 未决挂单遗留: order=%q status=%s slug=%s event_start=%d note=%q",
					rec.OrderID, rec.ExecStatus, rec.Slug, rec.EventStart, rec.ExecNote)
			}
			log.Fatalf("[Tail] 🔴 检测到 %d 条未决 GTC 挂单（磁盘 resting 行）, 而本次是 %s 模式——该模式既不能撤单也不能定稿。请带凭证以 -mode live 重启接管（首轮查询即定稿并撤余量）; 若该单早已了结, 按 order_id 去 data-api/UI 核对后人工结清该行再跑 paper",
				len(pend), effMode)
		}
	}

	resolutionPoller := trading.NewResolutionPoller(
		client.FetchMarketBySlug,
		10*time.Second,
		func(conditionID string, outcome int) error {
			// Resolve 返回 false = recorder.pending 中无此市场（重复回调/已结算摘除）。
			// ⚠️ 帧行不注册结算, 注册的只有 snap 与 scan 行——而两者**按窗口互斥**
			// （scan 只在 snap 未达标时产出）, 故 pending 里每窗至多一条, 无歧义。
			if !recorder.Resolve(conditionID, outcome, time.Now(), settle.SrcGamma) {
				log.Printf("[Tail] ⚠️ 结算回填未命中 %s outcome=%d（pending 中无此市场）",
					conditionID, outcome)
			}
			return nil
		},
	)

	// ── 结算编排（2026-09-24, internal/settle; 与 cmd/flip 同一套）──
	// 主路径 = **推送自算**: 官方 open/close 实测就是边界 N 与 N+300 那两秒的推送值
	// （逐位相等）, 故两条边界推送在手即闭市 +10s 定案（2026-09-24 由 25s 下调）,
	// 不必等 UMA 结算。推送缺失的
	// 窗口才取官方接口（**闭市 +45s 后**——官方值头几十秒是未收敛的临时值）,
	// 官方也失败才交回 gamma 轮询。三层来源都落盘行 settle_src 可事后审计。
	// 缺失面: 实测 854 个实盘窗里 15 个（1.76%）拿不到精确推送, 而一次结算要**两条**
	// 边界推送（N 与 N+300）⇒ 约 3.5% 的结算会走官方层（两边界缺一即算）。
	//
	// 触发点（snap/scan 行落盘、挂单定稿、重启恢复）全部收敛到这里的 Pending 扫描
	// ——**不再往 gamma 预先注册**: 两个注册点抢同一行会有一边报「结算回填未命中」。
	anchors := settle.NewAnchors()
	resolver := settle.New(anchors, settle.Options{
		Fetch: feed.NewPricePairFetcher(client, asset.Chainlink, sdk.Fiveminute, twapLookbackSeconds),
		Pending: func() []settle.Row {
			sigs := recorder.PendingSignals()
			rows := make([]settle.Row, 0, len(sigs))
			for _, sig := range sigs {
				rows = append(rows, settle.Row{
					ConditionID: sig.ConditionID,
					Slug:        sig.Slug,
					EventStart:  sig.EventStart,
				})
			}
			return rows
		},
		Settle: func(row settle.Row, outcome int, src string) bool {
			return recorder.Resolve(row.ConditionID, outcome, time.Now(), src)
		},
		GiveUp: func(row settle.Row) { resolutionPoller.Register(row.ConditionID, row.Slug) },
	})
	go resolver.Run(ctx)
	if n := len(recorder.PendingSignals()); n > 0 {
		log.Printf("[Tail] 🔄 重启恢复 %d 条未结算行（交结算编排: 推送→官方→gamma）", n)
	}
	go resolutionPoller.Run(ctx)

	if effMode == "live" {
		warnLiveStartup(recorder)
	}

	// ── 信号执行编排（paper/live 同源, 差异仅在 Ex 是否真实 POST）──
	exec := &tail.ExecState{
		Rec:          recorder,
		Ex:           executor,
		Live:         effMode == "live",
		Stake:        cfg.Tail.Stake,
		MaxDailyLoss: cfg.Risk.MaxDailyLoss,
		FirstWindow:  effMode == "live", // live 首窗禁单（重启防双单缝隙）
		Tokens: func() (string, string) {
			tokMu.RLock()
			defer tokMu.RUnlock()
			return upTok, downTok
		},
	}

	// ── 运行时状态载体（主循环 + Dashboard 共用; 窗口现场由 setWindow 换装）──
	runtime := &runtimeState{
		TwapAdapter: twapAdapter,
		Binance:     binance,
		Mode:        effMode,
		StartedAt:   time.Now(),
		Exec:        exec,
	}
	runtime.books = func() (*sdk.OrderBook, *sdk.OrderBook) {
		bookMu.RLock()
		defer bookMu.RUnlock()
		return upBook, downBook
	}

	// ── GTC 挂单跟踪 ──
	// 撤单点 = **闭市**（trading.CancelAtClose）: 快照在 rem≈60 产出，挂单只等 ~1 分钟
	// 就撤等于白挂（且会把「热门侧走弱」的那批位置全部让出）。挂到闭市由 CLOB 自动
	// 结清, 我们在 rem ≤ 0 时主动撤掉余量并查 size_matched 定稿——闭市后 +60s 硬截止兜底。
	// ⚠️ cancelLead 必须是**微小正数**: ≤0 会被 NewFillTracker 当成"未配置"回退 180s。
	fillTracker := trading.NewFillTracker(&trading.SdkClient{Client: client},
		trading.CancelAtClose, func(f flip.FillFinal) {
			// 定稿后行即进 recorder.pending → 结算编排（resolver）下轮自动接管;
			// 这里不再注册 gamma——注册点已收敛到 settle.Resolver 的 GiveUp。
			exec.ApplyFillFinal(f)
		})
	go fillTracker.Run(ctx)
	// 重启接管: 进程死在挂单期间 → 磁盘上的 resting 行交回跟踪
	if effMode == "live" {
		for _, rec := range recorder.Observations() {
			if rec.ExecStatus == flip.ExecStatusResting {
				fillTracker.RegisterOrder(fillOrderOf(rec), time.Unix(rec.EventStart, 0).Add(windowSec*time.Second), true)
			}
		}
	}

	// ── Dashboard（internal/dashboard 的 tail 族; 与 flip 面板各自一个 listener）──
	// 2026-09-24 起**只读流水线**: 判决速览/五格对照/监听对账/原始帧全部下线（判决机器
	// tail.Judge/mt19937 一并删除），纸面判决改由离线脚本 python/v4/23_tail_integrated.py
	// 做。面板只呈现引擎真正落下的行, 现算的东西仅限本窗读数与 tally——不碰判定/执行路径。
	if cfg.Runtime.TailDashboardAddr != "" {
		// 三源新鲜度阈值下发（前端按阈值标红——勿在前端硬编码）
		limits := dashboard.SourceLimits{
			BookLatMs: cfg.Tail.MaxBookLatMs,
			SpotAgeMs: cfg.Feed.MaxSpotAgeMs,
			TwapAgeMs: cfg.Feed.MaxTwapAgeMs,
		}
		dashState := dashboard.NewTailState(recorder, runtime, cfg.Tail, effMode, limits)
		go dashState.ListenAndServe(cfg.Runtime.TailDashboardAddr)
	}

	// σ 启动预热（本地 tailwin_*.jsonl 优先, 不足/陈旧回退官方网络预热）
	hist := flip.NewHistState()
	warmupSigma(hist, recorder, client, asset)

	log.Println("========================================")
	if effMode == "live" {
		log.Printf(" 扫尾盘⑤ — 🔒 实盘交易（GTC 限价挂单 @ 热门侧**有效价**, 挂到闭市撤余量, 日亏熔断 ≤%.1fU）",
			cfg.Risk.MaxDailyLoss)
	} else {
		log.Printf(" 扫尾盘⑤ — 纸面交易（mode=%s, 日亏熔断 ≤%.1fU 影子: 只标记不拦单）",
			effMode, cfg.Risk.MaxDailyLoss)
	}
	log.Printf(" 输出: %s（tail_/tailwin_/tailstats_ 三族） |  Slug: %s",
		cfg.Runtime.OutputDir, cfg.Runtime.SlugPrefix)
	log.Printf(" 参数: 三段链 rem≤%ds(判⑤)/rem≤%ds(判⑤)/此后每秒判② 热门侧有效价≥%.2f 位移≥%.0f美元 或 (1σ≥%.0f美元 且 位移≥1σ) stake=%.0fUSDC",
		cfg.Tail.T150Rem, cfg.Tail.T60Rem, cfg.Tail.PriceMin, cfg.Tail.DevMinUSD, cfg.Tail.SigmaMinUSD, cfg.Tail.Stake)
	log.Printf(" 新鲜度闸: book_lat≤%dms + spot_age≤%dms + twap_age≤%dms（tail.max_book_lat_ms / feed.*）",
		cfg.Tail.MaxBookLatMs, cfg.Feed.MaxSpotAgeMs, cfg.Feed.MaxTwapAgeMs)
	log.Println(" 数据源: [PM CLOB books 1s] + [Chainlink TWAP-60 锚/σ] + [Binance spot 位移]")
	log.Println("========================================")

	// logStats 落盘一行本窗 tick 健康度（tailstats_*.jsonl, **每窗无条件一行**）。
	// 含被跳过的窗口（skip 非空）: 逐日行数（≈288）本身即「主循环跑满」的证据。
	logStats := func(condID, slug string, eventStart int64, anchor, hb float64,
		st tail.WindowStats, skip string, ar anchorInfo) {
		e := tail.StatsRow{
			Ts: time.Now().UnixMilli(), ConditionID: condID, Slug: slug,
			EventStart: eventStart, Skip: skip, Anchor: anchor, HistBps: hb,
			WindowStats: st, AnchorExact: ar.src != "", AnchorSrc: ar.src,
		}
		if ar.atMs > 0 {
			e.AnchorArrivedMs = ar.atMs - eventStart*1000 // 该推送本地到达时刻距边界
		}
		if err := recorder.LogWindowStats(e); err != nil {
			log.Printf("[Cycle] ⚠️ 窗口健康度落盘失败: %v", err)
		}
	}

	// ── 市场周期主循环 ──
	var nextCache *marketCache
	// prevWindowStart 是上一轮迭代处理的窗口起点（unix 秒; 0 = 本进程还没处理过）。
	// 用途同 cmd/flip: 区分「刚跑完的窗口」与真·迟到——collectLoop 收尾总在边界前
	// 0~1s 回来，此刻 floor(now/300) 仍指回刚结束的那一窗（假 late 会污染逐日行数）。
	var prevWindowStart int64
	for {
		select {
		case <-ctx.Done():
			log.Println("[Tail] 正在关闭...")
			return
		default:
		}

		// 步骤 1: 对齐下一个 5 分钟窗口
		now := time.Now()
		alignedTs := now.Unix() / windowSec * windowSec
		nextStart := time.Unix(alignedTs, 0)
		if elapsed := time.Since(nextStart); elapsed > lateLimit {
			if nextStart.Unix() != prevWindowStart {
				log.Printf("[Cycle] ⚠️ 已落后窗口边界 %v（>%v），跳过本窗口 %s",
					elapsed.Round(time.Second), lateLimit,
					nextStart.UTC().Format(time.RFC3339))
				logStats("", "", nextStart.Unix(), 0, 0, tail.WindowStats{}, "late", anchorInfo{})
			}
			nextStart = nextStart.Add(windowSec * time.Second)
		}
		prevWindowStart = nextStart.Unix()
		slug := fmt.Sprintf("%s-%d", cfg.Runtime.SlugPrefix, nextStart.Unix())

		// 步骤 2: 市场信息（优先用本窗 tick 期间预取的缓存；未命中则边界前 20s 预取）
		var cachedMarket *gjson.Result
		var cachedErr error
		if nextCache != nil && nextCache.slug == slug {
			cachedMarket = nextCache.res
			log.Printf("[Cycle] ✅ 使用预取缓存 %s", slug)
			nextCache = nil
		} else {
			nextCache = nil // 丢弃过期缓存（窗口被跳过或预取失败）
			if wait := time.Until(nextStart.Add(-prefetchLead)); wait > 0 {
				log.Printf("[Cycle] 下一窗口 %s, 等待 %v（边界前 %ds 预取）",
					nextStart.UTC().Format(time.RFC3339), wait.Round(time.Second),
					int(prefetchLead/time.Second))
				select {
				case <-ctx.Done():
					return
				case <-time.After(wait):
				}
			}
			for time.Until(nextStart) > 2*time.Second {
				cachedMarket, cachedErr = client.FetchMarketBySlug(slug)
				if cachedErr == nil {
					log.Printf("[Cycle] ✅ 市场预取成功 %s（边界前 %.1fs）", slug, time.Until(nextStart).Seconds())
					break
				}
				log.Printf("[Cycle] 预取市场失败: %v —— 5s 后重试", cachedErr)
				wait := 5 * time.Second
				if rem := time.Until(nextStart.Add(-2 * time.Second)); rem < wait {
					wait = rem
				}
				if wait <= 0 {
					break
				}
				select {
				case <-ctx.Done():
					return
				case <-time.After(wait):
				}
			}
		}
		if wait := time.Until(nextStart); wait > 0 {
			select {
			case <-ctx.Done():
				return
			case <-time.After(wait):
			}
		}

		// 步骤 3: 市场信息 → conditionId + token
		var marketData *gjson.Result
		if cachedMarket != nil && cachedErr == nil {
			marketData = cachedMarket
		} else {
			var err error
			marketData, err = client.FetchMarketBySlug(slug)
			if err != nil {
				log.Printf("[Cycle] 获取市场失败: %v —— 跳过本窗口", err)
				logStats("", slug, nextStart.Unix(), 0, 0, tail.WindowStats{}, "no_market", anchorInfo{})
				runtime.clearWindow() // 跳窗 → Dashboard 显示「等待下一窗口…」而非上一窗陈旧状态
				waitTo(nextStart.Add(windowSec*time.Second), ctx)
				continue
			}
		}
		conditionID := marketData.Get("conditionId").String()
		upTokenID, downTokenID := feed.ParseMarketTokens(marketData)
		if upTokenID == "" || downTokenID == "" {
			log.Printf("[Cycle] ⚠️ 市场 %s token 解析为空，跳过本窗口", slug)
			logStats(conditionID, slug, nextStart.Unix(), 0, 0, tail.WindowStats{}, "no_token", anchorInfo{})
			runtime.clearWindow()
			waitTo(nextStart.Add(windowSec*time.Second), ctx)
			continue
		}
		// 防重入（一窗至多一单）: 已有 **OK 行**即整窗跳过——该窗已经下过单（或正准备
		// 下单, 见 AsSignal）, 重跑会写下第二条信号行（Recorder.pending 以 conditionID
		// 为键, 后记覆盖先记 → 先记的一笔永不结算）且可能同窗二次下单。
		// ⚠️ 判据是 **OK 行**（HasSignal, 三种 legacy 行都算）而不是「有快照」: 判定行
		// （t150/t60 未达标）的存在**不足以**跳窗——其中间可能就是崩溃点, 那两行本身
		// 已经完成了它们的使命（后续段由 Resume 续跑, 已判过的段不重判）。
		if recorder.HasSignal(conditionID) {
			log.Printf("[Cycle] ⚠️ 窗口 %s 已有 OK 行（%s 残留，快速重启重入），跳过整窗防双记", slug, conditionID)
			logStats(conditionID, slug, nextStart.Unix(), 0, 0, tail.WindowStats{}, "dup_record", anchorInfo{})
			runtime.clearWindow()
			waitTo(nextStart.Add(windowSec*time.Second), ctx)
			continue
		}
		// 续跑本窗: 已判过的段按磁盘真相回填（引擎不再重复判那一 tick）, 只剩未完成的段。
		resumeT150 := recorder.HasStage(conditionID, tail.StageT150)
		resumeT60 := recorder.HasStage(conditionID, tail.StageT60)
		if resumeT150 || resumeT60 {
			log.Printf("[Cycle] ↩️ 窗口 %s 已有判定行（t150=%v t60=%v，崩溃于三段链中途），续跑本窗剩余段",
				slug, resumeT150, resumeT60)
		}
		// σ 未就绪整窗跳过（同 cmd/flip 决策 #13）: hist.Bps 恒 0 时任何快照都被
		// no_hist 拒（且观测行 hist_bps=0 会踩对账硬检查）。与锚缺失同一条原则——
		// 数据源不可信 → 本窗不产出样本。下一窗 5min 后, 预热早已完成。
		if hist.Count() < flip.HistMin {
			log.Printf("[Cycle] ⚠️ 窗口 %s σ 未就绪（%d < %d 窗，预热中），跳过本窗口",
				slug, hist.Count(), flip.HistMin)
			logStats(conditionID, slug, nextStart.Unix(), 0, 0, tail.WindowStats{}, "no_sigma", anchorInfo{})
			runtime.clearWindow()
			waitTo(nextStart.Add(windowSec*time.Second), ctx)
			continue
		}
		if effMode == "live" {
			trading.PrefetchTokenInfo(client, marketData, []string{upTokenID, downTokenID})
		}
		log.Printf("[Cycle] conditionId=%s UP=%s DOWN=%s", conditionID, upTokenID, downTokenID)

		// 步骤 4: 订阅切换
		subMu.Lock()
		if len(subTokens) > 0 {
			monitor.UnsubscribeTokens(subTokens...)
		}
		subTokens = []string{upTokenID, downTokenID}
		subMu.Unlock()
		monitor.SubscribeTokens(upTokenID, downTokenID)

		tokMu.Lock()
		upTok, downTok = upTokenID, downTokenID
		tokMu.Unlock()
		bookMu.Lock()
		upBook, downBook = nil, nil
		bookMu.Unlock()

		// 步骤 5: 1s tick 采集 + 三段判定链
		// **不设过渡锚**（决策 #15）: 锚留 0，由取锚通道精确命中后经 UpgradeAnchor 注入;
		// 锚未到手期间 tick 照常占槽与计数, 只是不推进任何段（引擎 anchor≤0 路径）。
		engine := tail.NewEngine(cfg.Tail)
		engine.BeginWindow(0, 0)
		engine.Resume(resumeT150, resumeT60) // 崩溃重入: 已判过的段不重判（见上）
		endTime := nextStart.Add(windowSec * time.Second)
		// 换装 Dashboard 的窗口现场（锚可见性一并清零, 由取锚通道稍后注入）
		runtime.setWindow(engine, conditionID, slug, nextStart.Unix())

		var (
			cancelAnch context.CancelFunc
			anchorDone chan struct{}
		)
		winCtx, cancel := context.WithCancel(ctx)
		cancelAnch = cancel
		anchorDone = make(chan struct{})
		go func() {
			defer close(anchorDone)
			ups := feed.RecoverAnchor(winCtx, nil, twapAdapter.PushNearest, nextStart, endTime,
				feed.AnchorUpgradeOpts{
					Attempts: anchorExactAttempts,
					Interval: anchorExactInterval,
				})
			for r := range ups {
				bps := hist.Bps(r.Price)
				// 边界锚入结算表（无条件: 无论引擎是否接纳, 这条值就是官方 open/close
				// 口径, 结算要用——引擎拒收只代表本窗不再判定, 不影响已成交行的结算）。
				anchors.Put(nextStart.Unix(), r.Price)
				if !engine.UpgradeAnchor(r.Price, bps) {
					continue // 本窗已产出帧（锚已冻结）或窗口已结束
				}
				runtime.setAnchor(r.Source, r.AtMs)
				log.Printf("[Anchor] ✅ 窗口 %s 锚 %.2f（%s, σ=%.2fbps; 边界后 +%dms）",
					conditionID, r.Price, r.Source, bps, r.AtMs-nextStart.UnixMilli())
			}
			if runtime.anchorInfo().src == "" {
				n, newestOff, noTs := twapAdapter.CacheStat()
				log.Printf("[Anchor] ⚠️ 窗口 %s %d 次 × %v 未取到边界那一秒的 open"+
					"（缓存 %d 条, 最新一条评估偏移 %+dms, 缺时间戳 %d 条）, 本窗不产出样本",
					conditionID, anchorExactAttempts, anchorExactInterval, n, newestOff, noTs)
			}
		}()

		log.Printf("[Cycle] event=%s 窗口开始（锚待精确命中, 不设过渡锚; σ %d 窗就绪; 三段链 rem≤%d/≤%d/每秒②）",
			conditionID, hist.Count(), cfg.Tail.T150Rem, cfg.Tail.T60Rem)

		ticker := time.NewTicker(time.Second)
		lastTick := flip.Tick{}
		// 上一条 tick 的**采样真实时刻**（wall clock）——与 lastTick.Ts 分开: Ts 是
		// ticker 的计划时刻, 循环被卡住时它会明显落后于真实时刻, 而窗末 close 的
		// 迟到判据恰恰要靠真实时刻（见窗口结束后的 σ 段）。
		var lastSampleAt time.Time

		// 持仓监察（只记录, 见 internal/tail/hold.go）: 本窗出信号且**有仓位**
		// 之后开始逐 tick 记持仓侧盘口, 直到闭市。nil = 本窗尚未（或不会）建仓。
		var holdID *tail.HoldIdent

	collectLoop:
		for {
			select {
			case <-ctx.Done():
				ticker.Stop()
				return
			case tickTime := <-ticker.C:
				rem := int(endTime.Sub(tickTime).Seconds())
				rem = max(rem, 0)
				lastSampleAt = time.Now()
				lastTick = runtime.tick(tickTime, rem, cfg.Feed.MaxSpotAgeMs)

				// 引擎驱动: 本 tick 的**至多一行**（三段链任一段出信号即整窗只下一单）。
				// 判定行（t150/t60 未达标）只落盘; 信号行（OK）才进风控闸与执行路径——
				// 判定由 HandleDecision 内部的 OK 分支区分（单点 dispatch, 无 kind 分支）。
				if o := engine.ProcessTick(lastTick); o != nil {
					rec := exec.HandleDecision(o, conditionID, slug, nextStart.Unix())
					if rec == nil || !rec.OK {
						continue
					}
					// 结算不再在这里注册（落盘即进 pending, 交 settle.Resolver 统一编排）。
					// GTC 的 resting 行仓位未定——先交 FillTracker 定稿, 定稿后才进 pending。
					if rec.ExecStatus == flip.ExecStatusResting {
						fillTracker.RegisterOrder(fillOrderOf(rec), endTime, false)
					}
					// 持仓监察起点: 用 HasPosition（= 非 legacy scan ∧ 已成交）而非
					// IsFilled——被闸/被拒/挂单未定稿的行没有仓位可监察。
					if rec.HasPosition() {
						holdID = &tail.HoldIdent{
							ConditionID: conditionID, Slug: slug, Stage: rec.Stage,
							Side: rec.Side, EntryFill: rec.HotAsk, EntryRem: rec.Rem,
							Anchor: rec.Anchor, HistBps: rec.HistBps,
						}
						log.Printf("[Tail] 🔍 持仓监察开启 event=%s side=%s stage=%s "+
							"fill=%.2f anchor=%.2f", conditionID, rec.Side, rec.Stage,
							rec.HotAsk, rec.Anchor)
					}
				}
				// 持仓监察（**只记录**: 不参与判定、不下单、不进 P&L, 见 hold.go）。
				// 与上面那段的关键差别是门控——它**不要求持仓侧 bid > 0**（bid == 0
				// 正是要观测的东西）、不要求四档齐全。落盘失败只记日志、不上抛。
				if holdID != nil {
					if h := tail.HoldWatchRow(*holdID, cfg.Tail.MaxBookLatMs, lastTick); h != nil {
						upB, downB := runtime.books()
						if holdID.Side == flip.SideYes {
							h.HoldBid5, h.HoldAsk5 = bookTop5(upB)
						} else {
							h.HoldBid5, h.HoldAsk5 = bookTop5(downB)
						}
						if err := recorder.LogHoldTick(*h); err != nil {
							log.Printf("[Tail] ⚠️ 持仓监察落盘失败 event=%s: %v", conditionID, err)
						}
					}
				}
				if rem == 0 {
					ticker.Stop()
					break collectLoop
				}
				// 稳态预取: 本窗 rem≤20 预取下一窗市场信息, rem%5==0 提供失败重试点
				if nextCache == nil && rem <= 20 && rem%5 == 0 {
					nextSlug := fmt.Sprintf("%s-%d", cfg.Runtime.SlugPrefix, nextStart.Add(windowSec*time.Second).Unix())
					res, err := client.FetchMarketBySlug(nextSlug)
					if err != nil {
						log.Printf("[Cycle] ⚠️ 下一窗预取失败 %s: %v（rem=%d 时重试）", nextSlug, err, rem)
					} else {
						nextCache = &marketCache{slug: nextSlug, res: res}
						log.Printf("[Cycle] ✅ 下一窗预取成功 %s（rem=%d）", nextSlug, rem)
					}
				}
				// 每 30s 打印一次窗口进度（三段链: rem≤t150_rem 判⑤ → rem≤t60_rem 判⑤ → 每秒判②）
				if rem%30 == 0 {
					log.Printf("[Event] %s rem=%ds up=%.3f/%.3f down=%.3f/%.3f spot=%.2f state=%s",
						conditionID, rem, lastTick.UpBid, lastTick.UpAsk,
						lastTick.DownBid, lastTick.DownAsk, lastTick.BinPrice, engine.State())
				}
			}
		}

		// 取锚通道收尾: 取消 + join（通道通常早在 +2s 就已命中并关闭, join 立即返回）。
		// channel close 建立 happens-before——此后读锚可见性 / 引擎锚无数据竞争
		// （载体侧另有 anchorMu 保护, Dashboard goroutine 也在读）。
		cancelAnch()
		<-anchorDone
		anchor, histBps := engine.WindowAnchor()
		ai := runtime.anchorInfo() // 本窗锚可见性（tailstats + tailwin_ 落盘用）

		// 步骤 6: 窗口结束 → σ 滚动窗追加本窗振幅（严格只用已结束窗口）。
		// 判据与 cmd/flip 逐条一致（缺锚/close 缺失/流值陈旧/采样迟到 —— 假振幅会
		// 污染其后 18 窗的 σ 尺子, 而本族的 σ 腿直接吃它）。
		switch {
		case anchor <= 0 || lastTick.TwapPrice <= 0:
			log.Printf("[Cycle] ⚠️ 窗口结束 %s 但 close/anchor 缺失，本窗不计入 σ", conditionID)
		case lastTick.TwapAgeMs > cfg.Feed.MaxTwapAgeMs:
			log.Printf("[Cycle] ⚠️ 窗口结束 %s TWAP 陈旧（龄 %dms），本窗不计入 σ",
				conditionID, lastTick.TwapAgeMs)
		case lastSampleAt.Sub(endTime) > time.Second:
			log.Printf("[Cycle] ⚠️ 窗口结束 %s close 采样迟到（边界后 +%v, 循环卡顿跨窗），本窗不计入 σ",
				conditionID, lastSampleAt.Sub(endTime).Round(time.Millisecond))
		default:
			amp := math.Abs(lastTick.TwapPrice - anchor)
			hist.Push(amp)
			// 落盘一行供下次重启 σ 本地预热（失败只告警——σ 内存窗不受影响）
			if err := recorder.LogWindowAmplitude(flip.WindowEntry{
				Ts: time.Now().UnixMilli(), ConditionID: conditionID, Slug: slug,
				EventStart: nextStart.Unix(),
				Anchor:     anchor, Close: lastTick.TwapPrice, Amp: amp, AnchorSrc: ai.src,
			}); err != nil {
				log.Printf("[Cycle] ⚠️ 窗口振幅落盘失败: %v（重启本地预热将缺此窗）", err)
			}
			log.Printf("[Cycle] 窗口结束 %s: |close−anchor|=%.2f, σ 现 %d 窗",
				conditionID, amp, hist.Count())
		}

		// 本窗 tick 健康度无条件落盘（含锚缺失/σ 未计的窗口——可见性优先于整洁）
		logStats(conditionID, slug, nextStart.Unix(), anchor, histBps, engine.WindowStats(), "", ai)

		// live 首窗禁单解除: 首个完整跑完的窗口结束后置 false。窗口被跳过（continue）
		// 则顺延——保守多禁一窗，防重启残留窗双单的缝隙优先于交易频率。
		if exec.FirstWindow {
			exec.FirstWindow = false
			log.Println("[Trading] 重启后首窗结束, 禁单解除")
		}
	}
}

// tick 读取当前盘口/现货/TWAP 构造一条引擎 tick（1s 粒度）。
// 盘口缺失时 bid/ask 为 0（引擎判无效 tick、不推进闩锁）；spot 新鲜度超阈值置 0
// （= missing_spot，快照行会据此整窗丢弃）。
func (rt *runtimeState) tick(t time.Time, rem int, maxSpotAgeMs int64) flip.Tick {
	yb, nb := rt.books()
	pm := feed.NewPMTick(yb, nb)

	bin := rt.Binance.LatestData()
	spot, spotAge := 0.0, int64(-1)
	if bin.RxAtMs > 0 {
		spotAge = t.UnixMilli() - bin.RxAtMs
		if spotAge <= maxSpotAgeMs {
			spot = bin.Price
		}
	}

	twapPrice, twAge := rt.TwapAdapter.Latest()
	return flip.Tick{
		Ts:        t.UnixMilli(),
		Rem:       rem,
		UpBid:     pm.UpBid,
		UpAsk:     pm.UpAsk,
		DownBid:   pm.DownBid,
		DownAsk:   pm.DownAsk,
		BookLatMs: pm.BookLatMs,
		BinPrice:  spot,
		SpotAgeMs: spotAge,
		TwapPrice: twapPrice,
		TwapAgeMs: twAge,
	}
}

// waitTo 睡到指定时刻（期间可被 ctx 取消）。窗口被跳过时用它整窗等待。
func waitTo(t time.Time, ctx context.Context) {
	if wait := time.Until(t); wait > 0 {
		select {
		case <-ctx.Done():
		case <-time.After(wait):
		}
	}
}

// fillOrderOf 把一条 tail.Record 折成 FillTracker 的登记参数。
// 限价取 HotAsk（= 快照时的热门侧 ask，即挂单限价）; 投入取 Stake（目标股数
// = floor2(Stake/HotAsk), 由 FillTracker 自己算）。
func fillOrderOf(rec *tail.Record) trading.FillOrder {
	return trading.FillOrder{
		OrderID:     rec.OrderID,
		ConditionID: rec.ConditionID,
		Slug:        rec.Slug,
		Limit:       rec.HotAsk,
		Stake:       rec.Stake,
	}
}

// bookTop5 返回一本 SDK 订单簿买/卖侧**前 5 档累计股数**（0 = 该侧无档位）。
//
// 只服务持仓监察（hold.go）: 止损那一秒「有没有对手方」不但要看 bid 价格在不在,
// 还要看**够不够吃下这一笔**（stake/HotAsk ≈ 2~2.5 股）——决策 #21 实测 CLOB 的
// `minimum_order_size = 5 股`, 所以「bid 存在但只有 3 股」与「没有 bid」对实盘
// 是一回事, 价格字段分辨不出来, 必须落深度。
//
// ⚠️ 档位顺序: SDK 的 bids **升序**（最优价在末尾）、asks **升序**（最优价在开头）。
// 「前 5 档」= 最有竞争力的 5 档 ⇒ bids 取末尾 5 个、asks 取开头 5 个。
func bookTop5(book *sdk.OrderBook) (bid5, ask5 float64) {
	if book == nil {
		return 0, 0
	}
	lo := len(book.Bids) - 5
	if lo < 0 {
		lo = 0
	}
	for _, b := range book.Bids[lo:] {
		bid5 += b.Size
	}
	n := len(book.Asks)
	if n > 5 {
		n = 5
	}
	for _, a := range book.Asks[:n] {
		ask5 += a.Size
	}
	return bid5, ask5
}

// resolveLiveMode 决定成交模式与执行器（默认纸面; -mode live 且凭证齐 → 真实下单）。
// 与 cmd/flip 同源（同一批凭证判据），只是撤单点语义由 FillTracker 的 cancelLead 决定
// （本族传 trading.CancelAtClose = 挂到闭市）。
func resolveLiveMode(mode string, cfgSDK sdk.Config, readOnly bool, client *sdk.PolymarketClient) (string, flip.Executor) {
	if mode != "live" {
		return "paper", flip.NewExecutor("paper")
	}
	creds := cfgSDK.Polymarket.CLOBCreds
	var missing []string
	if readOnly {
		missing = append(missing, "sdk.polymarket.owner_key")
	}
	if creds == nil || creds.Key == "" || creds.Secret == "" || creds.Passphrase == "" {
		missing = append(missing, "sdk.polymarket.clob_creds.key/secret/passphrase")
	}
	if cfgSDK.Polymarket.FunderAddress == "" {
		missing = append(missing, "sdk.polymarket.funder_address")
	}
	if len(missing) > 0 {
		log.Printf("[Trading] ⚠️ -mode live 但凭证缺失（%s）—— 降级纸面执行", strings.Join(missing, ", "))
		return "paper", flip.NewExecutor("paper")
	}
	addr := cfgSDK.Polymarket.FunderAddress
	if len(addr) > 12 {
		addr = addr[:6] + "…" + addr[len(addr)-4:]
	}
	log.Printf("[Trading] 🔒 live 就绪: maker=%s（GTC 限价挂单 @ 热门侧有效价, 挂到闭市才撤余量; 首窗禁单）", addr)
	return "live", trading.NewLiveExecutor(&trading.SdkClient{Client: client})
}

// pendingResting 挑出磁盘上未决的 GTC 挂单行（resting = 订单仍在 CLOB 簿上、成交量
// 未定稿）。只可能来自 live; live 模式下由 FillTracker 接管，非 live 模式必须拒绝启动。
func pendingResting(recs []*tail.Record) []*tail.Record {
	var out []*tail.Record
	for _, rec := range recs {
		if rec.ExecStatus == flip.ExecStatusResting {
			out = append(out, rec)
		}
	}
	return out
}

// warnLiveStartup 打印 live 启动告警（载入期逐条 ⚠️ 已打, 这里给总量与目录提示）。
func warnLiveStartup(r *tail.Recorder) {
	if n := r.NeedsReconcile(); n > 0 {
		log.Printf("[Trading] ⚠️ %d 条执行中断记录待人工核对（submitting/未知结果, 见上方逐条告警）—— 勿自动补单, 按 maker+时间窗去 data-api 核对", n)
	}
	// 混合目录提示: 当日已有 paper 行（ExecStatus 空）混入会污染信号频率口径与日亏
	// 现算线——live 建议独立 runtime.output_dir（如 data/tail-live）。
	// ⚠️ 判据必须限定 **KindSnap ∧ OK**: 2026-09-24 起本族只产 KindSnap 一种行
	//（监听段改为真下单, 不再产只记录的对账行）, 而 OK=false 的判定行恒无 exec 字段
	// 且**从不下单**——它是标准输出的一部分（live 下也照记）, 拿它当「目录里混了
	// paper 行」的证据会让本告警在 live 每次启动都误报。
	// ⚠️ 两族（flip/tail）也建议分目录: 同一个目录会各写各的前缀, 不会串读,
	// 但「当日 P&L」这类按目录现算的口径会把两族混在一起。
	today := time.Now().UTC().Format("2006-01-02")
	for _, rec := range r.Observations() {
		if rec.Date == today && rec.Kind == tail.KindSnap && rec.OK && rec.ExecStatus == "" {
			log.Printf("[Trading] ⚠️ 输出目录今日已含 paper 行（tail_%s.jsonl）—— live 建议独立 runtime.output_dir 目录, 否则当日 paper/live 混行会污染信号口径与日亏现算线", today)
			return
		}
	}
}

// warmupSigma 做 σ 启动预热: 优先本地 tailwin_*.jsonl（recorder 每窗落盘的
// |close−anchor|）——「马上重启」场景毫秒级恢复，且与 live push 同源同口径、
// 零上游 API 压力。
//
// 本地可用条件（与 cmd/flip 逐条同源，只是数据源换成 tailwin_*）:
//  1. 最近 ≤flip.HistWindows 窗截到最新一段连续块（flip.RecentBlock）——断档前的
//     条目属更早的波动率 regime，混入会把 σ 尺度拉偏，只 seed 连续块;
//  2. 连续块 ≥ flip.HistMin 窗（不足时本地意义小，走网络更接近回测）;
//  3. 块内最新窗结束距今 ≤ localFreshMax（引擎最近在跑）。
//
// ⚠️ 两族各自独立预热（flip 读 windows_*, tail 读 tailwin_*）: 若两族都要「回测
// 口径的历史 σ」，网络预热取的是官方 TWAP 历史（同源）; 本地预热则各读各的——
// 同一目录下互不串读（前缀不同），分别部署时也各自完整。
func warmupSigma(hist *flip.HistState, r *tail.Recorder, client *sdk.PolymarketClient, asset feed.Asset) {
	seeded := 0
	if wins := r.RecentWindows(flip.HistWindows); len(wins) > 0 {
		if block := flip.RecentBlock(wins, int64(2*windowSec*1000)); len(block) >= flip.HistMin &&
			time.Since(time.UnixMilli(block[len(block)-1].Ts)) <= localFreshMax {
			amps := make([]float64, len(block))
			for i := range block {
				amps[i] = block[i].Amp
			}
			hist.Seed(amps)
			seeded = len(block)
			log.Printf("[Cycle] σ 本地预热: %d 窗（%s ~ %s）",
				len(block),
				time.UnixMilli(block[0].Ts).UTC().Format("15:04:05"),
				time.UnixMilli(block[len(block)-1].Ts).UTC().Format("15:04:05"))
		}
	}
	if seeded == 0 {
		go func() {
			log.Printf("[Cycle] σ 网络预热: 本地窗口不足/断档/陈旧，拉取官方 TWAP 历史范围（≤%d 窗）...", flip.HistWindows)
			vals := feed.FetchTwapRanges(client, asset.Chainlink, flip.HistWindows, windowSec, twapLookbackSeconds)
			hist.Seed(vals)
			log.Printf("[Cycle] σ 预热完成: %d 窗可用", len(vals))
		}()
	}
}
