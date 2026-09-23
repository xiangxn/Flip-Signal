// Package settle 实现「推送自算结算」（2026-09-24）: 用 TWAP-60 推送流里
// **边界那一秒**的两条值直接判窗口胜负，不必等 gamma 的 UMA 结算。
//
// 依据（探针 python/v4/19、20 在实盘数据上的实测）: 官方 crypto-price 的
// open(N)/close(N) **就是**边界 N 与 N+300 那两秒的推送值——实盘 240 窗
// 逐位相等（open 240/240、close(N) vs anchor(N+1) 235/235），且官方
// close(N) ≡ 官方 open(N+1)（211/211）。故用两条边界推送自算 = 官方口径。
//
// 三层回退（每一层都写进记录行的 settle_src 字段, 事后可审计）:
//
//	push     两个边界的推送都在手（实盘 ~98.2% 的窗口）——闭市后 ~10s 即定案
//	official 推送缺失（上游那一秒没发/网络丢）→ 官方接口取 open/close 比较。
//	         ⚠️ 必须**闭市 +45s 后**才取: 官方值头几十秒是未收敛的临时值
//	         （决策 #14 实测 +40s 才收敛, p90 差 9.5 美元）。
//	gamma    官方也失败（>30 天/网络）→ 交回 ResolutionPoller 轮询 UMA 状态。
//
// 本包是纯逻辑层: 不碰 HTTP、不碰磁盘——取数与落盘都由调用方注入
// （见 Options.Fetch / Settle / GiveUp）。
//
// ⚠️ 自算用的是**两个边界的精确推送**，不是引擎收尾时的「最新到达」推送:
// 后者到达延迟中位 ~1.8s，与官方值 |差| 中位 1.25 美元，实测会把 ~1% 的
// 天平窗（振幅 <2 美元）判反。边界推送这边 309 对相邻窗 0 错。
package settle

import (
	"context"
	"log"
	"sync"
	"time"
)

// 结算来源（记录行 settle_src 取值）。
const (
	SrcPush     = "push"     // 边界推送自算（主路径）
	SrcOfficial = "official" // 官方 crypto-price 接口（推送缺失时的兜底）
	SrcGamma    = "gamma"    // UMA 结算轮询（最后一道）
)

// PM 市场 outcome 词面（0=Up=yes, 1=Down=no）。与 flip.OutcomeUp/OutcomeDown
// 同值——本包刻意**不 import flip**（结算层不该依赖某一条策略），改由
// settle_test.go 的反向断言钉住二者一致。
const (
	outcomeUp   = 0
	outcomeDown = 1
)

// windowSpan 是市场窗口长度（btc-updown-5m = 300s）。
const windowSpan = 300 * time.Second

// Options 零值时的默认参数。
const (
	defaultPoll = 5 * time.Second

	// defaultPushWait 是「推送层定案」前的等待时长。原值 25s 是**照抄决策 #15 取锚通道
	// 的 20s 预算 + 5s 余量**, 但结算这条路上用不着那么久: 开窗那条推送（边界 N）在窗口
	// 开局 ~2s 内就进了 Anchors, 闭市时唯一还在等的是 close 那条（边界 N+300, 闭市那一
	// 刻才发布）——实盘 880 个命中窗口里它的本地到达延迟只有 1s(771 窗) / 2s(94 窗)
	// 两档, 没有一窗晚于 2s（决策 #15 记的「最晚 +12.1s」是取锚通道 500ms 轮询下的观测
	// 上界, 不是推送本身的延迟）。25s 的实际代价是每条信号白等 15s——用户实测「结算
	// 仍要 40-50s」的主因。2026-09-24 下调到 10s（1~2s 到达 + 8s 余量 = 5 倍实测上界）,
	// 推送层定案时点 26s → ~10-15s。
	// 官方层时点不受影响（它有自己的 45s 门槛, 与 PushWait 无关）: 真丢推送的窗口仍
	// 在闭市 +45s 走官方, 只是比原口径早 ~15s 尝试——而 45s 门槛未到, 故实为不变。
	defaultPushWait = 10 * time.Second

	defaultOfficialWait = 45 * time.Second // 官方值 ~+40s 才收敛（决策 #14）
	defaultFetchTimeout = 10 * time.Second
	defaultMaxTries     = 6
)

// Outcome 是官方结算口径: 严格 close > open 判 Up(0)，否则 Down(1)。
// 与官方 crypto-price 的裁决同式（closePrice > openPrice）; data/btc 全量 3753 窗
// 校验方向 3753/3753 一致、0 个平局窗（平局分支按官方同取 Down: 「不低于」不算涨）。
func Outcome(open, close float64) int {
	if close > open {
		return outcomeUp
	}
	return outcomeDown
}

// Anchors 记「每个边界那一秒的 TWAP 推送值」（边界 unix 秒 → 价格 USD）。
// 只由取锚通道的精确命中写入（feed.PushNearest），与引擎锚同源同值。
// 纯内存: 进程重启即清空，此时结算自然落到 official 层。
type Anchors struct {
	mu sync.RWMutex
	m  map[int64]float64
}

// NewAnchors 构造空的边界锚表。
func NewAnchors() *Anchors {
	return &Anchors{m: make(map[int64]float64)}
}

// Put 记一个边界值（eventStart ≤0 或 price ≤0 忽略——不是有效推送）。
func (a *Anchors) Put(eventStart int64, price float64) {
	if a == nil || eventStart <= 0 || price <= 0 {
		return
	}
	a.mu.Lock()
	a.m[eventStart] = price
	a.mu.Unlock()
}

// Get 取某个边界的推送值。
func (a *Anchors) Get(eventStart int64) (float64, bool) {
	if a == nil {
		return 0, false
	}
	a.mu.RLock()
	defer a.mu.RUnlock()
	v, ok := a.m[eventStart]
	return v, ok
}

// OutcomeFor 用「本窗起点」与「下一窗起点」两条边界锚算胜负; 缺任一条返回 false
// （→ 调用方走官方兜底）。
func (a *Anchors) OutcomeFor(eventStart int64) (int, bool) {
	open, ok := a.Get(eventStart)
	if !ok {
		return 0, false
	}
	close, ok := a.Get(eventStart + int64(windowSpan/time.Second))
	if !ok {
		return 0, false
	}
	return Outcome(open, close), true
}

// Row 是一条待结算行（记录器里未结算的真实持仓）。
type Row struct {
	ConditionID string
	Slug        string
	EventStart  int64 // 窗口起点（unix 秒）
}

// Options 是 Resolver 的注入参数（零值可用: 时间/次数取默认）。
type Options struct {
	Poll         time.Duration // 扫描间隔（≤0 = 5s）
	PushWait     time.Duration // 边界后 ≥ 此值才判「推送缺失」（≤0 = 10s）
	OfficialWait time.Duration // 边界后 ≥ 此值才允许官方取数（≤0 = 45s）
	FetchTimeout time.Duration // 单次官方请求超时（≤0 = 10s）
	MaxTries     int           // 官方最多次数（≤0 = 6; 用尽即交 gamma）

	// Pending 返回当前未结算的真实持仓行（Recorder.PendingSignals 的投影）。
	Pending func() []Row
	// Settle 回填结算结果（Recorder.ResolveFrom）; 返回 false = 该行已不在待结算集合。
	Settle func(row Row, outcome int, src string) bool
	// GiveUp 是三层都拿不到时的兜底注册（交回 gamma 轮询）; 每行至多调一次。
	GiveUp func(row Row)

	// Fetch 官方 open/close 取数（nil = 无官方层, 直接交 gamma）。
	// 签名与 feed.PricePairFetcher 一致（同一函数可直接赋值）。
	Fetch func(ctx context.Context, windowStart, windowEnd time.Time) (open, close float64)
}

// Resolver 按 Poll 间隔扫描待结算行，按 push → official → gamma 的顺序定案。
//
// 单 goroutine 语义: Run 只应被调用一次; tries/given 两本账只在 Run 的 goroutine 里
// 读写（Settle/GiveUp/Pending 回调也不得回头调 Resolver）。
type Resolver struct {
	opt     Options
	anchors *Anchors

	tries map[string]int  // conditionID → 官方已试次数
	given map[string]bool // conditionID → 已交 gamma（防重复注册）
}

// New 构造 Resolver。anchors 为 nil 时每条都走官方层（等价于「推送全丢」）。
func New(anchors *Anchors, opt Options) *Resolver {
	return &Resolver{opt: opt, anchors: anchors, tries: map[string]int{}, given: map[string]bool{}}
}

// Run 阻塞轮询直到 ctx 取消。
func (r *Resolver) Run(ctx context.Context) {
	ticker := time.NewTicker(r.poll())
	defer ticker.Stop()
	log.Printf("[Settle] 🚀 结算自算启动（扫描 %v; 推送层 ≥+%v, 官方层 ≥+%v, 官方上限 %d 次）",
		r.poll(), r.pushWait(), r.officialWait(), r.maxTries())

	r.tick(ctx, time.Now())
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			r.tick(ctx, time.Now())
		}
	}
}

// tick 是单轮扫描（测试直调; 生产只经 Run）。
func (r *Resolver) tick(ctx context.Context, now time.Time) {
	if r.opt.Pending == nil || r.opt.Settle == nil {
		return
	}
	rows := r.opt.Pending()
	live := make(map[string]bool, len(rows))
	for _, row := range rows {
		live[row.ConditionID] = true
		sinceEnd := now.Sub(time.Unix(row.EventStart, 0).Add(windowSpan))

		// ── ① 推送层: 两个边界推送都在手 → 直接定案 ──
		// 未到 PushWait 时**什么都不做**（不是走官方）: 开窗那条推送（边界 N）若命中
		// 早已入表, 闭市时还在等的只有 close 那条（边界 N+300, 闭市那一刻才发布）,
		// 此刻「没有」只说明它还在路上, 不能算缺失（实测到达 +1~2s, 见 defaultPushWait）。
		if sinceEnd < r.pushWait() {
			continue
		}
		if outcome, ok := r.anchors.OutcomeFor(row.EventStart); ok {
			r.settle(row, outcome, SrcPush)
			continue
		}

		// ── ② 官方层: 推送缺失 → 官方 open/close（须等过收敛点）──
		if r.opt.Fetch == nil {
			r.giveUp(row) // 未接官方层: 除 gamma 外无路可走, 直接交
			continue
		}
		if sinceEnd < r.officialWait() {
			continue
		}
		if r.given[row.ConditionID] {
			continue // 已交 gamma, 不再重复取数
		}
		if r.tries[row.ConditionID] >= r.maxTries() {
			r.giveUp(row)
			continue
		}
		r.tries[row.ConditionID]++
		open, close := r.fetch(ctx, row)
		if open <= 0 || close <= 0 {
			log.Printf("[Settle] ⚠️ 官方取数未就绪 %s（第 %d/%d 次）",
				row.ConditionID, r.tries[row.ConditionID], r.maxTries())
			continue // 下轮再试（或用尽后交 gamma）
		}
		r.settle(row, Outcome(open, close), SrcOfficial)
	}

	// 簿记清理: 已离开待结算集合的行（结算完成/被移除）不再占位。
	for k := range r.tries {
		if !live[k] {
			delete(r.tries, k)
		}
	}
	for k := range r.given {
		if !live[k] {
			delete(r.given, k)
		}
	}
}

// settle 回填一行并记日志; 回填失败（行已不在待结算集合）只留痕。
func (r *Resolver) settle(row Row, outcome int, src string) {
	if !r.opt.Settle(row, outcome, src) {
		log.Printf("[Settle] ⚠️ 结算回填未命中 %s outcome=%d src=%s（pending 中无此市场）",
			row.ConditionID, outcome, src)
		return
	}
	log.Printf("[Settle] ✅ 结算 %s outcome=%d src=%s（窗口 %s）",
		row.ConditionID, outcome, src, time.Unix(row.EventStart, 0).UTC().Format("15:04"))
}

// giveUp 把一行交给 gamma 轮询（每行至多一次）。
func (r *Resolver) giveUp(row Row) {
	r.given[row.ConditionID] = true
	log.Printf("[Settle] 🔻 %s 推送与官方均不可得（官方 %d 次尝试用尽）, 交回 gamma 轮询",
		row.ConditionID, r.tries[row.ConditionID])
	if r.opt.GiveUp != nil {
		r.opt.GiveUp(row)
	}
}

// fetch 带超时调用官方取数（返回 (0, 0) = 未就绪/失败/超时）。
func (r *Resolver) fetch(ctx context.Context, row Row) (float64, float64) {
	start := time.Unix(row.EventStart, 0)
	if t := r.fetchTimeout(); t > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, t)
		defer cancel()
	}
	return r.opt.Fetch(ctx, start, start.Add(windowSpan))
}

// ── 参数兜底（Options 零值 → 默认）──

func (r *Resolver) poll() time.Duration {
	if r.opt.Poll > 0 {
		return r.opt.Poll
	}
	return defaultPoll
}

func (r *Resolver) pushWait() time.Duration {
	if r.opt.PushWait > 0 {
		return r.opt.PushWait
	}
	return defaultPushWait
}

func (r *Resolver) officialWait() time.Duration {
	if r.opt.OfficialWait > 0 {
		return r.opt.OfficialWait
	}
	return defaultOfficialWait
}

func (r *Resolver) fetchTimeout() time.Duration {
	if r.opt.FetchTimeout > 0 {
		return r.opt.FetchTimeout
	}
	return defaultFetchTimeout
}

func (r *Resolver) maxTries() int {
	if r.opt.MaxTries > 0 {
		return r.opt.MaxTries
	}
	return defaultMaxTries
}
