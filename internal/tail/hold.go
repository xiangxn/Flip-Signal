package tail

import "github.com/necklace/flip-signal/internal/flip"

// ── 持仓监察（2026-09-25）──
//
// 回答一个**离线数据答不了**的问题：止损真要出场那一刻，**有没有对手方**。
//
// 起因（docs/tail_stoploss_2026-09-25.md）：离线回放的 14 天里，持仓侧 `bid == 0`
// 出现 **0 次**，止损因此一路按「按 bid 全额成交」记账、Δ = +10.44U。但那个 0 是
// **构造性产物**——旧采集守卫 `if len(book.Asks) == 0 { continue }` 把空侧盘口
// 整条消息丢掉，内存里留着撤单前那一份旧簿（决策 #21），所以「没有买单」这种状态
// 根本没有机会落进数据。而决策 #21 的活体取证（SDK WS + 公共 REST 双通道）恰恰
// 证明：事件趋于确定后，**对应「押最终输家」的那条腿会被整侧撤空**（探针四窗的
// 空侧起始 rem = 12/28/45/47，ETH 首采 rem ∈ [0,34] / [0,79]）。止损要出场的那一刻，
// 正是持仓从赢家变成输家的那一刻 ⇒ **离线 Δ 只是上界**。
//
// 所以先补观测、不改判定：本监察只做一件事——持仓期间逐秒把**持仓侧盘口**记下来，
// 跑一两周后用「触发止损的那些时刻，bid 到底存不存在、有多深」回答可成交性。
//
// 硬边界（三条，任何一条被破坏都等于把风险引回引擎）：
//  1. **只记录**：不参与判定、不下单、不进 P&L / 熔断 / 胜率，也不写任何已有的
//     记录族——独立前缀 tailhold_，与 tail_/tailwin_/tailstats_ 互不沾染（决策 #9）。
//  2. **不碰引擎状态**：本文件是纯函数 + 一个数据结构，没有 Engine 方法、不读锁、
//     不改 e.state / e.stats / 三个闩锁 ⇒ parity 红线（6412/2133/3640/3）零影响。
//  3. **门控与判定路径刻意不同**（见 HoldWatchRow）。

const (
	// StopWatchBid 持仓监察的「哨兵」价格线（只用于给行打 stop_cand 标记, 不做任何事）。
	//
	// 取值 = 26 号脚本扫描出的止损价格腿默认值（bid < 0.30）。它出现在本文件里的
	// 唯一目的是：将来分析时能把「若当时真挂了止损，这一秒会不会触发」直接筛出来，
	// 不必重算门槛。**它不是配置项、不参与判定、改它不影响引擎任何行为。**
	StopWatchBid = 0.30
	// StopWatchDev 同上, 位移腿（dev < −20 美元）。
	StopWatchDev = -20.0
)

// HoldRow 是一条持仓监察行（tailhold_YYYY-MM-DD.jsonl）。
//
// 与 Observation 的关系：它**不是**判定行——没有 rules / ok / reject_reason，
// 也不带执行字段。它是「信号成交之后，持仓侧盘口逐秒长什么样」的原始流水，
// 因此在 schema 上独立成类（字段全部自主命名，不复用 Observation 的键）。
//
// ⚠️ 消费侧须知道的三件事：
//   - **每行 = 一个 tick**，不是每窗一行（对照 tailstats_ 是严格每窗一行）；
//   - 只覆盖**信号成交之后**的部分（入场那一秒不记，从下一个 tick 起）；
//   - 行数受门控影响（见 HoldWatchRow），缺行 ≠ 没信号。
type HoldRow struct {
	Kind string `json:"kind"` // 恒 holdKind（"tailhold"）——与前缀一起构成双保险
	Ts   int64  `json:"ts"`   // tick 采样时刻（unix 毫秒）
	Date string `json:"date"` // UTC 日（按日切分文件名）
	Rem  int    `json:"rem"`  // 窗口剩余秒（恒 > 0；rem == 0 是终 tick, 不记）

	// ── 身份：这一行监察的是哪一笔仓位 ──
	ConditionID string `json:"condition_id"`
	Slug        string `json:"slug"`
	Stage       string `json:"stage"` // 信号来自哪一段（t150 | t60 | listen）
	Side        string `json:"side"`  // 持仓侧（yes | no）= 信号行当时的热门侧
	// 入场信息取自**信号行本身**（不是重算）：锚在窗口内是冻结的（首行落盘即冻结,
	// UpgradeAnchor 此后拒收）, 所以信号行的 anchor/hist_bps 就是本窗的权威值,
	// 与本行的 dev 同源可比。
	EntryFill float64 `json:"entry_fill"` // 信号行的成交价（= 当时的热门侧有效价）
	EntryRem  int     `json:"entry_rem"`  // 信号行的 rem
	Anchor    float64 `json:"anchor"`     // 本窗锚（信号行口径, 恒 > 0）
	HistBps   float64 `json:"hist_bps"`   // 本窗 σ（信号行口径）

	// ── 持仓侧盘口（本行的**核心观测**）──
	//
	// HoldBid == 0 是**合法读数**且是本监察存在的主要理由：它 = 该侧整侧没有买单
	//（决策 #21 的「输家侧 bid 被整侧撤空」），此刻止损**卖不掉**。
	// ⚠️ HoldBid == 0 且 HoldAsk == 0 = 该侧整簿空; 只有 bid == 0 而 ask > 0 才是
	//「有人卖、没人买」——两者需要用 HoldAsk 区分, 所以两个都落盘。
	HoldBid  float64 `json:"hold_bid"`  // 持仓侧最优买价（0 = 无买单）
	HoldAsk  float64 `json:"hold_ask"`  // 持仓侧最优卖价（0 = 无卖单）
	HoldBid5 float64 `json:"hold_bid5"` // 买侧前 5 档累计股数（0 = 无买盘；调用方回填, 见下）
	HoldAsk5 float64 `json:"hold_ask5"` // 卖侧前 5 档累计股数

	// ── 与判定同一口径的派生量（便于直接和 26 号脚本的触发条件对齐）──
	Spot      float64 `json:"spot"`       // Binance spot（恒 > 0）
	Dev       float64 `json:"dev"`        // sgn·(spot − anchor)，美元（正 = 朝持仓方向）
	BookLatMs int64   `json:"book_latency_ms"`
	SpotAgeMs int64   `json:"spot_age_ms"` // -1 = 尚无推送

	// StopCand 本 tick 是否满足 26 号脚本的止损条件（bid < StopWatchBid ∧ dev < StopWatchDev）。
	// **纯派生标记**——引擎不会因为它是 true 而做任何事，它只是让分析脚本能一步筛出
	// 「若真挂了止损，这一秒会不会触发」。深度与可成交性的判断仍要看 HoldBid5。
	StopCand bool `json:"stop_cand"`
}

// HoldIdent 是被监察的那笔仓位的**身份**（全部取自信号行本身，不重算）。
//
// 为什么锚/σ 也从信号行取：锚在窗口内是冻结的（首行落盘即冻结，UpgradeAnchor
// 此后拒收，决策 #22），所以信号行的 anchor/hist_bps 就是本窗的权威值 —— 本监察行
// 的 dev 与信号行的 dev 因此同源可比，不会出现「监察说 dev=−25 而信号行说 −19」
// 这种口径错位。
type HoldIdent struct {
	ConditionID string
	Slug        string
	Stage       string  // 信号来自哪一段（t150 | t60 | listen）
	Side        string  // 持仓侧（yes | no）
	EntryFill   float64 // 信号行的成交价（= 当时的热门侧有效价）
	EntryRem    int     // 信号行的 rem
	Anchor      float64 // 本窗锚（信号行口径）
	HistBps     float64 // 本窗 σ（信号行口径）
}

// HoldWatchRow 采集一条持仓监察行（**纯函数**：只读入参，不碰引擎状态、不做 I/O）。
//
// 门控与判定路径**刻意不同**（这是本函数唯一需要小心的地方）：
//   - 只要求 `rem > 0` ∧ `BookLatMs ≤ maxLatMs` ∧ `spot > 0` ∧ `anchor > 0`；
//   - **不要求持仓侧 bid > 0** —— bid == 0 正是要观测的东西，26 号脚本的 `post()`
//     把这类 tick 当「卖不掉」直接丢掉，本监察必须把它们留住；
//   - **不要求四档齐全** —— 只看持仓侧，对手侧无关；
//   - **不要求 rem ≤ 150** —— 持仓后一路看到闭市（与 26 号 post 同）。
//
// 深度（HoldBid5/HoldAsk5）不在本函数内填：算它要读 SDK 簿，而本包零外部依赖
// （见包注释）——由 cmd/tail 采样后回填，缺省 0 = 未采集。
//
// 返回 nil = 本 tick 无有效读数（窗口已结束 / 盘口延迟超阈 / spot 缺失 / 锚不可用）。
func HoldWatchRow(id HoldIdent, maxLatMs int64, t flip.Tick) *HoldRow {
	if t.Rem <= 0 || t.BookLatMs > maxLatMs || !(t.BinPrice > 0) || !(id.Anchor > 0) {
		return nil
	}
	bid, ask := t.DownBid, t.DownAsk
	if id.Side == flip.SideYes {
		bid, ask = t.UpBid, t.UpAsk
	}
	dev := DevUSD(id.Side, t.BinPrice, id.Anchor)
	return &HoldRow{
		Kind:        holdKind,
		Ts:          t.Ts,
		Date:        utcDate(t.Ts),
		Rem:         t.Rem,
		ConditionID: id.ConditionID,
		Slug:        id.Slug,
		Stage:       id.Stage,
		Side:        id.Side,
		EntryFill:   id.EntryFill,
		EntryRem:    id.EntryRem,
		Anchor:      id.Anchor,
		HistBps:     id.HistBps,
		HoldBid:     bid,
		HoldAsk:     ask,
		Spot:        t.BinPrice,
		Dev:         dev,
		BookLatMs:   t.BookLatMs,
		SpotAgeMs:   t.SpotAgeMs,
		StopCand:    bid < StopWatchBid && dev < StopWatchDev,
	}
}
