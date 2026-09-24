package tail

import (
	"bufio"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/necklace/flip-signal/internal/flip"
)

// ── 三个记录家族的前缀与行 schema 标识 ──
//
// ⚠️ 三个前缀都**刻意与 flip 的不同**，且互不为前缀（有下划线分隔）:
//   - flip 是 touches_ / windows_ / winstats_;
//   - tail 是 tail_ / tailwin_ / tailstats_。
//
// 两族共同落在一个 output_dir 时，各自的 Glob 只认自己的前缀——`tail_*.jsonl`
// 匹配不到 `tailwin_*`/`tailstats_*`（下划线断开了），也匹配不到 flip 的任何文件。
// 这不是洁癖: flip 的 windows_*.jsonl 是 σ 预热的**数据源**，被统计行污染会以
// Amp=0 的样子混进其后 18 窗的 σ（CLAUDE.md 决策 #9 的红线）；tail 复制了同一
// 套纪律，只是换了自己的文件名。
const (
	recordPrefix    = "tail_"
	recordEventType = "tail"
)

// 窗口振幅日志（σ 重启本地预热的数据源）: tailwin_YYYY-MM-DD.jsonl，每完成一个
// 窗口落一行（flip.WindowEntry 形状，供 flip.RecentBlock 截连续块）。
const windowPrefix = "tailwin_"

// 窗口 tick 健康度日志: tailstats_YYYY-MM-DD.jsonl，**每窗无条件一行**（含被
// 跳过的窗口——「这窗为什么没有行」的可观测性数据源）。
const statsPrefix = "tailstats_"

// 行 schema 标识（WindowEntry/StatsRow 的 kind 字段）: 两类窗口级行互斥, 防串读。
// 取值即文件前缀，让 grep 与人读同一眼能对上。
const (
	winKindAmp   = "tailwin"   // flip.WindowEntry（σ 预热数据源）
	winKindStats = "tailstats" // StatsRow（tick 健康度审计）
)

// Recorder 追加写 tail_*.jsonl（每窗 1~3 行: 三段链的判定行/信号行, 未出信号时
// 至多两条判定行），外加窗口振幅日志（σ 预热数据源）与窗口健康度日志。
// 三族各持独立文件句柄，互不干扰。
//
// 与 flip.Recorder 的差异（有意）:
//   - 前缀与行 schema 不同（本文件顶部）——不复用 flip.Recorder，因为它的三个
//     前缀是包级常量，且 tail 的观测段（四档报价快照）与 flip 的触底段毫无交集；
//   - 其余纪律逐条照搬: 按 UTC 日切分、行级 flush（崩溃不丢）、结算回填
//     temp+rename 原子重写、启动扫描恢复内存态与 pending。
type Recorder struct {
	mu sync.Mutex

	dir     string
	recs    []*Record          // 全部观测行（时间正序: 载入序 + 追加序）
	pending map[string]*Record // 未结算 ok 信号，key = conditionID

	day  string // 当前打开文件的 UTC 日
	file *os.File
	buf  *bufio.Writer

	wins    []flip.WindowEntry // 已完成窗口振幅（时间正序）
	winDay  string
	winFile *os.File
	winBuf  *bufio.Writer

	statsDay  string // 健康度日志只追加、不载入内存（纯审计, 省一次全量读盘）
	statsFile *os.File
	statsBuf  *bufio.Writer
}

// DayPnl 单日已结算 P&L（日亏熔断的输入）。
type DayPnl struct {
	Date string  `json:"date"`
	PnL  float64 `json:"pnl"`
	N    int     `json:"n"` // 当日已结算信号数
}

// StatsRow 是一个窗口的 tick 健康度行（tailstats_*.jsonl）。
//
// Skip 非空表示该窗未采集（σ 未就绪整窗跳过 / 锚未取到 / 迟到跳窗），统计字段
// 全 0——保留一行是为了让逐日行数（≈288）本身成为「主循环有没有跑满」的证据。
//
// ⚠️ 与 flip.WindowStatsEntry 同名但不共用类型: 本族的 Skip/SigmaOK 口径与
// flip 的 lost_triggers 明细无关，且两边字段会各自演化——共用一个结构体只会
// 让两边的字段都变成可选。
type StatsRow struct {
	Ts          int64   `json:"ts"` // 落盘时刻（unix 毫秒）
	Date        string  `json:"date"`
	Kind        string  `json:"kind"` // 恒 winKindStats
	ConditionID string  `json:"condition_id,omitempty"`
	Slug        string  `json:"slug,omitempty"`
	EventStart  int64   `json:"event_start,omitempty"`
	Skip        string  `json:"skip,omitempty"` // 非空 = 本窗未采集（原因, 见 cmd/tail）
	Anchor      float64 `json:"anchor"`         // 本窗最终生效 anchor（0 = 锚未取到）
	HistBps     float64 `json:"hist_bps"`       // 本窗生效 σ（bps; 0 = 不可用）

	// AnchorExact 本窗锚是否精确命中边界那一秒的 TWAP 推送（= 官方 openPrice 口径）。
	// **刻意不带 omitempty**——false（本窗无锚）也是有效取值, 且它是 python 侧区分
	// 「09-19 精确取锚之后的行」的哨兵键（与 flip 同口径, 见决策 #15）。
	AnchorExact bool `json:"anchor_exact"`
	// AnchorSrc 锚来源（stream = TWAP 推送; 官方 HTTP 段休眠时为恒 stream）;
	// 空 = 本窗未取到锚。
	AnchorSrc string `json:"anchor_src,omitempty"`
	// AnchorArrivedMs 该推送的**本地到达时刻**距窗口边界（毫秒）——发布延迟的
	// 无偏观测（不含取锚轮询的 500ms 相位）。仅取到锚时非 0。
	AnchorArrivedMs int64 `json:"anchor_arrived_ms,omitempty"`

	WindowStats // 内嵌: ticks/ticks_valid/book_stale/book_missing/spot_missing/frames 平铺
}

// NewRecorder 打开（必要时创建）输出目录并载入既有记录。
// 载入校验行 schema（event_type == "tail"）: 指错目录时旧格式不会静默污染统计。
// 窗口振幅日志一并载入内存（σ 本地预热数据源）。
func NewRecorder(dir string) (*Recorder, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("NewRecorder: 创建目录: %w", err)
	}
	r := &Recorder{dir: dir, pending: map[string]*Record{}}

	matches, err := filepath.Glob(filepath.Join(dir, recordPrefix+"*.jsonl"))
	if err != nil {
		return nil, fmt.Errorf("NewRecorder: glob: %w", err)
	}
	sort.Strings(matches)
	skipped := 0
	for _, f := range matches {
		n, s, err := r.loadFileLocked(f)
		if err != nil {
			return nil, err
		}
		skipped += s
		log.Printf("[Tail] 载入 %s（%d 行, %d 待结算）", filepath.Base(f), n, len(r.pending))
	}
	if skipped > 0 {
		log.Printf("⚠️ [Tail] 跳过 %d 行不匹配 schema（event_type != %q）", skipped, recordEventType)
	}

	wMatches, err := filepath.Glob(filepath.Join(dir, windowPrefix+"*.jsonl"))
	if err != nil {
		return nil, fmt.Errorf("NewRecorder: glob 窗口日志: %w", err)
	}
	sort.Strings(wMatches)
	for _, f := range wMatches {
		n, err := r.loadWindowFileLocked(f)
		if err != nil {
			return nil, err
		}
		log.Printf("[Tail] 载入 %s（%d 窗）", filepath.Base(f), n)
	}
	return r, nil
}

// loadWindowFileLocked 读入一个窗口日志日文件（调用方已持锁）。返回行数。
func (r *Recorder) loadWindowFileLocked(path string) (int, error) {
	f, err := os.Open(path)
	if err != nil {
		return 0, fmt.Errorf("Tail: 打开 %s: %w", path, err)
	}
	defer f.Close()

	rows := 0
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 64*1024), 1024*1024)
	for sc.Scan() {
		line := sc.Bytes()
		if len(line) == 0 {
			continue
		}
		var e flip.WindowEntry
		if err := json.Unmarshal(line, &e); err != nil {
			log.Printf("⚠️ [Tail] %s: 窗口行解析失败跳过: %v", path, err)
			continue
		}
		if e.Ts <= 0 || e.Date == "" {
			log.Printf("⚠️ [Tail] %s: 跳过异常窗口行（ts=%d）", path, e.Ts)
			continue
		}
		// schema 守卫: 只收振幅行。空串 = 无 kind 的历史行（放行）; 其余一律拒收
		// ——统计行的 Ts/Date 合法但无 amp, 放进来会被当振幅 0 污染 σ（本文件是
		// σ 预热数据源）。
		if e.Kind != "" && e.Kind != winKindAmp {
			log.Printf("⚠️ [Tail] %s: 跳过非振幅行（kind=%q）——防污染 σ 预热", path, e.Kind)
			continue
		}
		r.wins = append(r.wins, e)
		rows++
	}
	if err := sc.Err(); err != nil {
		return 0, fmt.Errorf("Tail: 读 %s: %w", path, err)
	}
	return rows, nil
}

// loadFileLocked 读入一个日文件的行（调用方已持锁）。返回行数/跳过数。
func (r *Recorder) loadFileLocked(path string) (int, int, error) {
	f, err := os.Open(path)
	if err != nil {
		return 0, 0, fmt.Errorf("Tail: 打开 %s: %w", path, err)
	}
	defer f.Close()

	rows, skipped := 0, 0
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 64*1024), 1024*1024)
	for sc.Scan() {
		line := sc.Bytes()
		if len(line) == 0 {
			continue
		}
		var rec Record
		if err := json.Unmarshal(line, &rec); err != nil {
			log.Printf("⚠️ [Tail] %s: 行解析失败跳过: %v", path, err)
			skipped++
			continue
		}
		if rec.EventType != recordEventType || rec.Date == "" {
			log.Printf("⚠️ [Tail] %s: 跳过旧格式行（event_type=%q）", path, rec.EventType)
			skipped++
			continue
		}
		// 行 schema 守卫: kind 必须是 frame/snap/scan（isKnownKind）。空串 = 无法判类型,
		// 拒收（本族的行**恒**带 kind, 与 flip 的 touches_ 相反——那里的旧行没有这个键）。
		if !isKnownKind(rec.Kind) {
			log.Printf("⚠️ [Tail] %s: 跳过 kind=%q 的行（非 frame/snap/scan）", path, rec.Kind)
			skipped++
			continue
		}
		r.recs = append(r.recs, &rec)
		// 崩溃恢复: pending 收**所有未结算的 ok 行**（isSettlable = 未被官方 outcome 定案、
		// 且不是未定稿的 submitting/resting）——2026-09-24 起含被闸/0 成交行:
		// 它们没有仓位（P&L 恒 0）, 但页面要显示官方结果, 故照常注册结算。
		// 未定稿的行（submitting/resting/成交未知）不能自动补单, 只告警人工核对。
		if isSettlable(&rec) {
			r.pending[rec.ConditionID] = &rec // 重启后重新注册结算轮询
		} else if rec.OK && rec.Won == nil && rec.ConditionID != "" && rec.Kind == KindSnap &&
			(rec.ExecStatus == flip.ExecStatusSubmitting || rec.ExecStatus == flip.ExecStatusResting ||
				strings.HasPrefix(rec.ExecNote, flip.ExecNoteUnknown)) {
			log.Printf("⚠️ [Tail] %s: condition=%s exec=%s 执行中断待定稿——勿自动补单, 按 order_id 去 data-api 核对。slug=%s event_start=%d ts=%d side=%s hot_ask=%.3f stake=%.1f note=%q",
				filepath.Base(path), rec.ConditionID, rec.ExecStatus,
				rec.Slug, rec.EventStart, rec.Ts, rec.Side, rec.HotAsk, rec.Stake, rec.ExecNote)
		}
		rows++
	}
	if err := sc.Err(); err != nil {
		return 0, 0, fmt.Errorf("Tail: 读 %s: %w", path, err)
	}
	return rows, skipped, nil
}

// newRecord 构造记录行（时间/日期从观测快照派生）。
func newRecord(condID, slug string, eventStart int64, obs *Observation, stake float64) *Record {
	return &Record{
		Observation: *obs, // 值拷贝: 观测判定后即快照
		EventType:   recordEventType,
		Date:        utcDate(obs.Ts),
		ConditionID: condID,
		Slug:        slug,
		EventStart:  eventStart,
		Stake:       stake,
	}
}

// RecordObservation 落盘一条观测（判定行与 paper 的信号行都走这里）并立即 flush。
// ok 行入 pending（等待结算回填, 判据 = isSettlable）。
// 返回生成的记录行。
//
// paper 侧的成交是**模拟**的: 行里不写任何 exec 字段（ExecStatus 空 → IsFilled
// 恒真）, 结算走 Stake 兜底——与 flip 纸面同口径，也是回测镜像基准。
func (r *Recorder) RecordObservation(condID, slug string, eventStart int64, obs *Observation, stake float64) (*Record, error) {
	r.mu.Lock()
	defer r.mu.Unlock()

	rec := newRecord(condID, slug, eventStart, obs, stake)
	if err := r.writeLocked(rec); err != nil {
		return nil, err
	}
	if isSettlable(rec) {
		r.pending[rec.ConditionID] = rec
	}
	return rec, nil
}

// ── live 两阶段落盘 ──

// SubmitLiveObservation 落盘 live 下单的 submitting 行（两阶段第一步, POST 发起前
// 立即写, 行级 flush 崩溃不丢）。不入 pending、不注册结算——成交结果由
// CompleteExecution 回填后按 isSettlable 决定。
func (r *Recorder) SubmitLiveObservation(condID, slug string, eventStart int64, obs *Observation, stake float64) (*Record, error) {
	r.mu.Lock()
	defer r.mu.Unlock()

	rec := newRecord(condID, slug, eventStart, obs, stake)
	rec.ExecStatus = flip.ExecStatusSubmitting
	if err := r.writeLocked(rec); err != nil {
		return nil, err
	}
	return rec, nil
}

// RecordRejected 落盘被风控闸拦下的信号行（**paper 与 live 共用**; 未发起下单,
// 无 submitting 中间态）。
//
// OK=true（策略信号本身成立）但 ExecStatus=rejected ⇒ HasPosition 为假 ⇒ 未成交:
// 不入胜率、P&L 恒 0（a.md「被风控拦」= 未成交）。**仍然入 pending**: 官方 outcome
// 回来时照常回填 won（页面要显示这一笔的结果）, 只是 PnL 由 recomputePnL 归零。
//
// ⚠️ flip 侧仍是方案 A（paper 被闸行照常成交照常结算, 作反事实样本）——tail 已按
// 用户决定改成统一形态, 见 docs/tail_integrated_2026-09-24.md §3.3。
func (r *Recorder) RecordRejected(condID, slug string, eventStart int64, obs *Observation, stake float64, gateReason, note string) (*Record, error) {
	r.mu.Lock()
	defer r.mu.Unlock()

	rec := newRecord(condID, slug, eventStart, obs, stake)
	rec.ExecStatus = flip.ExecStatusRejected
	rec.GateReason = gateReason
	rec.ExecNote = note
	if err := r.writeLocked(rec); err != nil {
		return nil, err
	}
	if isSettlable(rec) {
		r.pending[rec.ConditionID] = rec
	}
	return rec, nil
}

// CompleteExecution 回填一个 submitting 行的执行结果（两阶段第二步, POST 同步响应
// 后立即调用）: 更新 ExecStatus/OrderID/FillPrice/Shares/Cost/ExecNote, filled/partial
// （真实持仓）且 ok 时入 pending; 原子重写当日文件（temp+rename）。
// Shares 仅在成交时覆盖为实际股数（unfilled/rejected/resting 保留目标股数供分析）。
// 返回更新后记录与是否已成交。
//
// GTC: 状态可能是 resting——挂单在簿、成交量未定。此时**不写** Shares/Cost/FillPrice
// （写了就等于把半个仓位当持仓）, 只记 order_id 与 note, 终态由 FillTracker 定稿后
// 走 CompleteRestingFill 回填。
func (r *Recorder) CompleteExecution(conditionID string, res flip.ExecResult) (*Record, bool, error) {
	r.mu.Lock()
	defer r.mu.Unlock()

	switch res.Status {
	case flip.ExecStatusFilled, flip.ExecStatusPartial, flip.ExecStatusUnfilled,
		flip.ExecStatusRejected, flip.ExecStatusResting:
	default:
		return nil, false, fmt.Errorf("CompleteExecution: %s 非法状态 %q", conditionID, res.Status)
	}
	rec := r.findSnapLocked(conditionID)
	if rec == nil {
		return nil, false, fmt.Errorf("CompleteExecution: %s 无落盘行（重启缝隙?）", conditionID)
	}
	if rec.ExecStatus != flip.ExecStatusSubmitting {
		return nil, false, fmt.Errorf("CompleteExecution: %s 行状态 %q 非 submitting（重复回填或行损坏）", conditionID, rec.ExecStatus)
	}

	rec.ExecStatus = res.Status
	rec.OrderID = res.OrderID
	rec.ExecNote = res.Note
	if res.Status == flip.ExecStatusFilled || res.Status == flip.ExecStatusPartial {
		rec.FillPrice = res.FillPrice
		rec.Shares = res.Shares // 目标股数 → 实际成交股数
		rec.Cost = res.Cost
	}
	recomputePnL(rec) // 已结算过的行（先结算后定稿的竞态）按真实成交补算 P&L
	filled := rec.IsFilled()
	if isSettlable(rec) {
		r.pending[conditionID] = rec
	}
	if err := r.rewriteDayLocked(rec.Date); err != nil {
		log.Printf("⚠️ [Tail] 执行回填重写失败: %v", err)
	}
	return rec, filled, nil
}

// CompleteRestingFill 回填一笔 GTC 挂单的终态成交（FillTracker 撤单时查 CLOB
// size_matched 得到, 见 trading.FillTracker doc; 本族撤单点 = 闭市）:
//   - filled/partial: 写实际 Shares/Cost/FillPrice → isSettlable 入 pending;
//   - unfilled: 0 成交, 保留目标股数（与 CompleteExecution 同口径）;
//   - resting: **仍未确认**（查询失败/重启遗留从未观测到该单）——只更新 ExecNote,
//     行保持 resting: 不入 pending、不注册结算、NeedsReconcile 继续计它为待核对
//     （宁可悬着, 不按 0 成交记）。
//
// 行不存在或状态非 resting → error（重复回填/行损坏, 告警不静默）。
func (r *Recorder) CompleteRestingFill(f flip.FillFinal) (*Record, error) {
	r.mu.Lock()
	defer r.mu.Unlock()

	switch f.Status {
	case flip.ExecStatusFilled, flip.ExecStatusPartial, flip.ExecStatusUnfilled, flip.ExecStatusResting:
	default:
		return nil, fmt.Errorf("CompleteRestingFill: %s 非法终态 %q", f.ConditionID, f.Status)
	}
	if (f.Status == flip.ExecStatusFilled || f.Status == flip.ExecStatusPartial) && f.Shares <= 0 {
		return nil, fmt.Errorf("CompleteRestingFill: %s 成交状态 %q 但股数 %.4f ≤ 0", f.ConditionID, f.Status, f.Shares)
	}
	rec := r.findSnapLocked(f.ConditionID)
	if rec == nil {
		return nil, fmt.Errorf("CompleteRestingFill: %s 无落盘行（重启缝隙?）", f.ConditionID)
	}
	if rec.ExecStatus != flip.ExecStatusResting {
		return nil, fmt.Errorf("CompleteRestingFill: %s 行状态 %q 非 resting（重复回填或行损坏）", f.ConditionID, rec.ExecStatus)
	}

	rec.ExecStatus = f.Status
	rec.ExecNote = f.Note
	if f.Status == flip.ExecStatusFilled || f.Status == flip.ExecStatusPartial {
		rec.Shares = f.Shares // 目标股数 → 实际成交股数
		rec.Cost = f.Cost
		rec.FillPrice = f.Cost / f.Shares
	}
	recomputePnL(rec) // 已结算过的行（先结算后定稿的竞态）按真实成交补算 P&L
	if isSettlable(rec) {
		r.pending[f.ConditionID] = rec
	}
	if err := r.rewriteDayLocked(rec.Date); err != nil {
		log.Printf("⚠️ [Tail] 挂单终态回填重写失败: %v", err)
	}
	return rec, nil
}

// Resolve 结算一个 ok 信号: 按官方 outcome 判定所押侧输赢并回填记录, 原子重写该日
// 文件。命中返回 true。
//
// ⚠️ tail 的结算口径与回测一致: 赢 = 押注侧即官方赢家（不是「价格」, 是 outcome）;
// 每股兑 1 USDC ⇒ 赢 shares−cost / 输 −cost（cost 缺省回退 Stake, paper 行即如此）。
//
// 2026-09-24 起**所有 ok 行都注册结算**（含被闸/0 成交/下单被拒——a.md 第 3 条要求
// 页面上能看见每一笔的官方结果）, 故这里必须区分「有没有仓位」: 无仓位的行照常写
// Won（显示赢/输）, 但 PnL 由 recomputePnL 归零——不成交就没有盈亏。
//
// src 是结算来源（internal/settle 的 SrcPush|SrcOfficial|SrcGamma; 空 = 未记），
// 落进 settle_src 供事后按层核对（与 flip 同编排, 见 internal/settle 包注释）。
func (r *Recorder) Resolve(conditionID string, outcome int, at time.Time, src string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()

	rec, found := r.pending[conditionID]
	if !found {
		return false
	}
	won := flip.WonFor(rec.Side, outcome)
	rec.Won = &won
	recomputePnL(rec)
	rec.ResolvedAt = at.UTC().Format(time.RFC3339)
	rec.SettleSrc = src
	delete(r.pending, conditionID)

	if err := r.rewriteDayLocked(rec.Date); err != nil {
		log.Printf("⚠️ [Tail] 结算重写失败: %v", err)
	}
	return true
}

// recomputePnL 按**当前**的成交字段重算一条已结算记录的 P&L。三个调用点共用
// （Resolve / CompleteExecution / CompleteRestingFill）, 堵的是同一个竞态:
// resting 行可能在 FillTracker 定稿**之前**就被结算编排（闭市 +10s）定案——
// 若无条件只在 Resolve 里算一次, 定稿后成交了却永远留着 P&L=0。
//
// 口径:
//   - 未结算（Won == nil）→ 不动（P&L 由 Resolve 写）;
//   - 无仓位（HasPosition 假: 被闸/0 成交/下单被拒/legacy 对账行）→ **P&L 恒 0**
//     （a.md: 未成交不计算 P&L）;
//   - 有仓位 → 赢 shares−cost / 输 −cost; cost 缺省回退 Stake（paper 行不写 Cost）。
func recomputePnL(rec *Record) {
	if rec.Won == nil {
		return
	}
	if !rec.HasPosition() {
		rec.PnL = 0
		return
	}
	cost := rec.Cost
	if cost <= 0 {
		cost = rec.Stake // paper 行无 Cost（模拟成交不写）, 用投入兜底
	}
	if *rec.Won {
		rec.PnL = rec.Shares - cost // 每股兑 1U（Shares = 实际成交股数）
		return
	}
	rec.PnL = -cost
}

// findSnapLocked 按 conditionID 找**本次执行对应的**那一行（调用方已持锁）。
//
// 两条口径:
//   - **必须按 Kind 过滤**: legacy 的帧行/对账行没有执行语义, 按 conditionID 找第一行
//     会把执行回填打到它们身上;
//   - **只认 OK 行、取最后一条**（向后扫）: 三段链下一个窗口**原本就有多条 snap 行**
//     （t150 判定行、t60 判定行、信号行都是 KindSnap, 见 types.go 的 Stage）——但只有
//     OK=true 的那一条会被执行, 且 submitting 行是紧接 POST 写的、必是最后一条。
//     ⚠️ 不能只按「最后一条 snap」取: 监听段在信号之后不再产行是对的, 但若未来有谁
//     在 OK 行之后再补一条普通判定行, 回填就会打到没有 exec 语义的那条上。
//
// 重复的 **OK** 行才是数据完整性告警（HasSignal 守卫 + 引擎「出信号即 Done」都不允许
// 它出现）——真出现时打一条 ⚠️ 不静默吞掉。
func (r *Recorder) findSnapLocked(conditionID string) *Record {
	var found *Record
	for i := len(r.recs) - 1; i >= 0; i-- {
		rec := r.recs[i]
		if rec.ConditionID != conditionID || rec.Kind != KindSnap || !rec.OK {
			continue
		}
		if found != nil {
			log.Printf("⚠️ [Tail] %s 有多条 OK 行（重复决策? 检查是否有进程重入）——回填取最后一条", conditionID)
			break
		}
		found = rec
	}
	return found
}

// writeLocked 追加写一行并 flush（调用方已持锁）。
func (r *Recorder) writeLocked(rec *Record) error {
	if err := r.openDayLocked(rec.Date); err != nil {
		return err
	}
	r.recs = append(r.recs, rec)
	line, err := json.Marshal(rec)
	if err != nil {
		return fmt.Errorf("Tail: marshal: %w", err)
	}
	if _, err := r.buf.Write(line); err != nil {
		return err
	}
	if err := r.buf.WriteByte('\n'); err != nil {
		return err
	}
	return r.buf.Flush()
}

// rewriteDayLocked 原子重写一个日文件（temp+rename），行 = 该日全部记录。
func (r *Recorder) rewriteDayLocked(date string) error {
	if date == r.day {
		if err := r.closeCurrentLocked(); err != nil {
			return err
		}
	}
	tmp := recordFilePath(r.dir, date) + ".tmp"
	f, err := os.Create(tmp)
	if err != nil {
		return fmt.Errorf("Tail: 创建临时文件: %w", err)
	}
	w := bufio.NewWriter(f)
	for _, rec := range r.recs {
		if rec.Date != date {
			continue
		}
		line, err := json.Marshal(rec)
		if err != nil {
			f.Close()
			return fmt.Errorf("Tail: marshal: %w", err)
		}
		w.Write(line)
		w.WriteByte('\n')
	}
	if err := w.Flush(); err != nil {
		f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmp, recordFilePath(r.dir, date)); err != nil {
		return fmt.Errorf("Tail: rename: %w", err)
	}
	if date == r.day {
		return r.openDayLocked(date)
	}
	return nil
}

// openDayLocked 打开（必要时轮转）指定 UTC 日的追加文件（调用方已持锁）。
func (r *Recorder) openDayLocked(date string) error {
	if r.day == date && r.file != nil {
		return nil
	}
	if err := r.closeCurrentLocked(); err != nil {
		return err
	}
	f, err := os.OpenFile(recordFilePath(r.dir, date), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return fmt.Errorf("Tail: 打开 %s: %w", date, err)
	}
	r.day, r.file, r.buf = date, f, bufio.NewWriter(f)
	return nil
}

// closeCurrentLocked flush 并关闭当前文件（调用方已持锁）。
func (r *Recorder) closeCurrentLocked() error {
	if r.file == nil {
		return nil
	}
	if err := r.buf.Flush(); err != nil {
		r.file.Close()
		return err
	}
	if err := r.file.Close(); err != nil {
		return err
	}
	r.day, r.file, r.buf = "", nil, nil
	return nil
}

// ── 窗口振幅日志（σ 本地预热数据源）──

// LogWindowAmplitude 落盘一个已完成窗口的 σ 贡献行（行级 flush，崩溃不丢）。
// 仅当窗口数据有效时调用（与 histState.Push 同分支）；落盘失败返回错误由调用方
// 告警继续——σ 内存窗不受影响，只是下次重启本地预热缺此窗。
//
// e.Kind 由本函数强制填 winKindAmp（调用方不必关心, 也不会写错）。
func (r *Recorder) LogWindowAmplitude(e flip.WindowEntry) error {
	r.mu.Lock()
	defer r.mu.Unlock()

	e.Kind = winKindAmp
	if e.Ts <= 0 {
		return fmt.Errorf("LogWindowAmplitude: ts 非法（%d）", e.Ts)
	}
	e.Date = utcDate(e.Ts)
	if err := r.openWinDayLocked(e.Date); err != nil {
		return err
	}
	r.wins = append(r.wins, e)
	line, err := json.Marshal(e)
	if err != nil {
		return fmt.Errorf("Tail: marshal 窗口行: %w", err)
	}
	if _, err := r.winBuf.Write(line); err != nil {
		return err
	}
	if err := r.winBuf.WriteByte('\n'); err != nil {
		return err
	}
	return r.winBuf.Flush()
}

// LogWindowStats 落盘一个窗口的 tick 健康度行（每窗无条件一行, 行级 flush）。
// e.Date 由 Ts 派生（调用方无需自算 UTC 日）。只追加、不载入内存。
func (r *Recorder) LogWindowStats(e StatsRow) error {
	r.mu.Lock()
	defer r.mu.Unlock()

	e.Kind = winKindStats
	e.Date = utcDate(e.Ts)
	if err := r.openStatsDayLocked(e.Date); err != nil {
		return err
	}
	line, err := json.Marshal(e)
	if err != nil {
		return fmt.Errorf("Tail: marshal 健康度行: %w", err)
	}
	if _, err := r.statsBuf.Write(line); err != nil {
		return err
	}
	if err := r.statsBuf.WriteByte('\n'); err != nil {
		return err
	}
	return r.statsBuf.Flush()
}

// openWinDayLocked 打开（必要时轮转）指定 UTC 日的窗口日志文件（调用方已持锁）。
func (r *Recorder) openWinDayLocked(date string) error {
	if r.winDay == date && r.winFile != nil {
		return nil
	}
	if err := r.closeWinDayLocked(); err != nil {
		return err
	}
	f, err := os.OpenFile(windowFilePath(r.dir, date), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return fmt.Errorf("Tail: 打开窗口日志 %s: %w", date, err)
	}
	r.winDay, r.winFile, r.winBuf = date, f, bufio.NewWriter(f)
	return nil
}

// closeWinDayLocked flush 并关闭窗口日志文件（调用方已持锁）。
func (r *Recorder) closeWinDayLocked() error {
	if r.winFile == nil {
		return nil
	}
	if err := r.winBuf.Flush(); err != nil {
		r.winFile.Close()
		return err
	}
	if err := r.winFile.Close(); err != nil {
		return err
	}
	r.winDay, r.winFile, r.winBuf = "", nil, nil
	return nil
}

// openStatsDayLocked 打开（必要时轮转）指定 UTC 日的健康度日志文件（调用方已持锁）。
func (r *Recorder) openStatsDayLocked(date string) error {
	if r.statsDay == date && r.statsFile != nil {
		return nil
	}
	if err := r.closeStatsDayLocked(); err != nil {
		return err
	}
	f, err := os.OpenFile(statsFilePath(r.dir, date), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return fmt.Errorf("Tail: 打开健康度日志 %s: %w", date, err)
	}
	r.statsDay, r.statsFile, r.statsBuf = date, f, bufio.NewWriter(f)
	return nil
}

// closeStatsDayLocked flush 并关闭健康度日志文件（调用方已持锁）。
func (r *Recorder) closeStatsDayLocked() error {
	if r.statsFile == nil {
		return nil
	}
	if err := r.statsBuf.Flush(); err != nil {
		r.statsFile.Close()
		return err
	}
	if err := r.statsFile.Close(); err != nil {
		return err
	}
	r.statsDay, r.statsFile, r.statsBuf = "", nil, nil
	return nil
}

// Close flush 并关闭三族文件句柄。
func (r *Recorder) Close() error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if err := r.closeStatsDayLocked(); err != nil {
		return err
	}
	if err := r.closeWinDayLocked(); err != nil {
		return err
	}
	return r.closeCurrentLocked()
}

// ── 只读访问（恢复/风控/启动摘要用）──

// copyRecords 深拷贝一批记录（调用方已持锁）。
//
// ⚠️ 只复制切片是不够的: Resolve 会在**结算轮询 goroutine** 里回填 rec.Won/PnL/
// ResolvedAt（持 r.mu 写），而调用方（主循环/启动摘要）会读这些字段——只复制切片
// 等于把字段读写留在锁外, 是实打实的 data race（flip 侧 2026-09-19 review 用
// -race 探针复现过）。故对外的记录读口一律返回**字段副本**。
func copyRecords(recs []*Record) []*Record {
	out := make([]*Record, 0, len(recs))
	for _, rec := range recs {
		c := *rec
		out = append(out, &c)
	}
	return out
}

// Observations 返回全部观测行的**副本**（时间正序; 见 copyRecords）。
func (r *Recorder) Observations() []*Record {
	r.mu.Lock()
	defer r.mu.Unlock()
	return copyRecords(r.recs)
}

// Signals 返回**判定通过**的行副本（时间正序）——Dashboard 信号表的输入。
//
// ⚠️ 含被闸行（gate_reason 非空）与 0 成交行: 它们是「信号成立但没成交」,
// 页面必须显示（a.md 第 3/4 条: 所有信号都注册结算并显示状态, 未成交不进胜率）。
// 分类是**消费端**的责任——判据统一用 Record.HasPosition（dashboard 的 tally）。
//
// legacy 的 frame（OK 恒 false）/ scan 行天然不入（后者虽 OK, 但从未下单、无仓位,
// 不属任何信号——见 HasPosition）。
func (r *Recorder) Signals() []*Record {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]*Record, 0, len(r.recs))
	for _, rec := range r.recs {
		if rec.Kind == KindSnap && rec.OK {
			out = append(out, rec)
		}
	}
	return copyRecords(out)
}

// HasSignal 判断某窗口是否已有 **OK 行**（= 已下过单或至少已经产出过信号行）。
// cmd/tail 的防双单判据: 崩溃重启重入同一窗口时, 只要磁盘上已有 OK 行就整窗跳过。
//
// ⚠️ 与「有没有仓位」无关（被闸/0 成交的行也算已产出信号——它们同样不该再下一单）,
// 也**不按 stage 过滤**: 三段任一 OK 都是「本窗的信号」。
// legacy 的 scan 行（只记录、从不下单）也算在内——那是旧口径的反事实样本,
// 重入时保守跳过即可（历史只有 09-23/09-24 两天会遇到）。
func (r *Recorder) HasSignal(conditionID string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, rec := range r.recs {
		if rec.ConditionID == conditionID && rec.OK {
			return true
		}
	}
	return false
}

// HasStage 判断某窗口是否已产出过指定段的判定行（cmd/tail 重入时回填引擎用,
// 见 Engine.Resume）。legacy 行没有 stage（空串）, 故对它们的查询恒 false——
// 旧口径的「决策已做」由 HasSignal 兜住（旧 snap 若 OK 则有信号; 若被拒则新链
// 从 T=60 段续跑, 同一窗仍至多一条 OK 行）。
func (r *Recorder) HasStage(conditionID, stage string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, rec := range r.recs {
		if rec.ConditionID == conditionID && rec.Kind == KindSnap && rec.Stage == stage {
			return true
		}
	}
	return false
}

// RecentWindows 返回最近 n 个已完成窗口振幅行（时间正序；不足则返回全部）。
// cmd/tail 启动时据此做 σ 本地预热（配合 flip.RecentBlock 截连续块）。
func (r *Recorder) RecentWindows(n int) []flip.WindowEntry {
	r.mu.Lock()
	defer r.mu.Unlock()
	start := 0
	if len(r.wins) > n {
		start = len(r.wins) - n
	}
	return append([]flip.WindowEntry(nil), r.wins[start:]...)
}

// PendingSignals 返回未结算 ok 信号的**副本**（重启后据此重新注册结算轮询）。
func (r *Recorder) PendingSignals() []*Record {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]*Record, 0, len(r.pending))
	for _, rec := range r.pending {
		out = append(out, rec)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Ts < out[j].Ts })
	return copyRecords(out)
}

// HasKind 判断某窗口是否已落过指定类型的行。
//
// ⚠️ 2026-09-24 起本族的防重入判据改用 HasSignal（有 OK 行 = 已下过单, 整窗跳过）
// 与 HasStage（按段续跑, 见 Engine.Resume）——「有行就跳过」在旧口径下是对的
// （帧与快照是两个独立闩锁）, 在新口径下会把「只做了第一段判定」的窗口误判成整窗完成。
// 本函数保留给 legacy 行查询与测试。
func (r *Recorder) HasKind(conditionID, kind string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, rec := range r.recs {
		if rec.ConditionID == conditionID && rec.Kind == kind {
			return true
		}
	}
	return false
}

// GatedOn 本 UTC 日是否已有指定原因的被闸行（日亏熔断的当日锁存判据）。
//
// 为什么锁存必需: paper 方案 A 下被闸行照常结算 → 累积 P&L 可能因后到的结算回升
// 过线, 只看「现算 P&L ≤ 线」会当日自动复牌、风控形同虚设。判据取磁盘真相
// （本函数扫内存记录 = 磁盘已载入内容）——重启后锁存自动恢复, 跨日自动归零。
func (r *Recorder) GatedOn(date, reason string) bool {
	return r.GatedToday(date, reason) > 0
}

// GatedToday 当日（date，UTC）指定原因的被闸行数。
func (r *Recorder) GatedToday(date, reason string) int {
	r.mu.Lock()
	defer r.mu.Unlock()

	n := 0
	for _, rec := range r.recs {
		if rec.Date == date && rec.GateReason == reason {
			n++
		}
	}
	return n
}

// DailyPnl 按日汇总已结算 P&L（正序; 日亏熔断的输入）。
//
// 只统计**有仓位**的已结算信号（HasPosition = 实际成交 ∧ 非 legacy 对账行）:
// 被闸/下单被拒/0 成交行的 P&L 恒 0（它们不算成交, a.md 第 3 条）, legacy scan 行
// 更是从未下单——任何一条混进来都会让不存在的盈亏去开关熔断闸。
func (r *Recorder) DailyPnl() []DayPnl {
	r.mu.Lock()
	defer r.mu.Unlock()
	byDay := map[string]*DayPnl{}
	for _, rec := range r.recs {
		if !rec.OK || rec.Won == nil || !rec.HasPosition() {
			continue
		}
		d := byDay[rec.Date]
		if d == nil {
			d = &DayPnl{Date: rec.Date}
			byDay[rec.Date] = d
		}
		d.PnL += rec.PnL
		d.N++
	}
	out := make([]DayPnl, 0, len(byDay))
	for _, d := range byDay {
		out = append(out, *d)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Date < out[j].Date })
	return out
}

// MaxDrawdown 基于已结算信号的累计 P&L 最大回撤（USDC，负值表示回撤）。
// 与 flip.Recorder.MaxDrawdown 同口径, 准入条件同 DailyPnl: **有仓位**的已结算信号
// （未成交行不动回撤——它们没有盈亏, 见 recomputePnL）。
func (r *Recorder) MaxDrawdown() float64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	cum, peak, dd := 0.0, 0.0, 0.0
	for _, rec := range r.recs {
		if !rec.OK || rec.Won == nil || !rec.HasPosition() {
			continue
		}
		cum += rec.PnL
		if cum > peak {
			peak = cum
		}
		if d := cum - peak; d < dd {
			dd = d
		}
	}
	return dd
}

// TodayStats 读取**当日**（UTC）健康度日志 tailstats_*.jsonl 的全部行（时间正序）。
//
// ⚠️ 这是本 Recorder 唯一的磁盘读口: 健康度行只追加、**不载入内存**（纯审计,
// 省一次全量读盘）, 而 Dashboard 要的「今日辅助闸门」（窗数 / skip 分布 /
// anchor_exact=false 计数）恰好只存在于这个文件里。
//
// 坏行/半行跳过（进程正在追写时最后一行可能不完整）——审计视图不该因一行解析失败
// 整体失败; 文件不存在返回 nil, nil（当日还没跑过任何窗口, 不是错误）。
func (r *Recorder) TodayStats() ([]StatsRow, error) {
	r.mu.Lock()
	dir := r.dir
	r.mu.Unlock()

	path := statsFilePath(dir, utcToday())
	f, err := os.Open(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, fmt.Errorf("TodayStats: 打开 %s: %w", path, err)
	}
	defer f.Close()

	out := make([]StatsRow, 0, 288) // 一天 288 窗（5 分钟一窗）
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 64*1024), 1024*1024)
	for sc.Scan() {
		line := sc.Bytes()
		if len(line) == 0 {
			continue
		}
		var e StatsRow
		if err := json.Unmarshal(line, &e); err != nil {
			log.Printf("⚠️ [Tail] %s: 健康度行解析失败跳过: %v", filepath.Base(path), err)
			continue
		}
		out = append(out, e)
	}
	if err := sc.Err(); err != nil {
		return nil, fmt.Errorf("TodayStats: 读 %s: %w", path, err)
	}
	return out, nil
}

// NeedsReconcile 返回执行中断待人工核对的行数（live）:
//   - submitting = 下单后崩溃（order_id 大概率不可得）;
//   - note 以 flip.ExecNoteUnknown 开头 = 成交结果不明, 任何状态都算;
//   - resting 且**无法跟踪**（缺 order_id/限价/投入）= 永远无人定稿的遗留行。
//
// 正常在途的 resting 行不算（它由 FillTracker 在闭市撤单时定稿, 计进来会让告警
// 常亮——live 下几乎每个窗口都有一段挂单在簿）。
func (r *Recorder) NeedsReconcile() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	n := 0
	for _, rec := range r.recs {
		switch {
		case rec.ExecStatus == flip.ExecStatusSubmitting:
			n++
		case strings.HasPrefix(rec.ExecNote, flip.ExecNoteUnknown):
			n++
		case rec.ExecStatus == flip.ExecStatusResting && !trackableResting(rec):
			n++
		}
	}
	return n
}

// trackableResting 判断一条 resting 行能否被 FillTracker 接手: 条件是
// trading.FillTracker.RegisterOrder 的入表判据（缺 order_id/限价/投入即只告警
// 跳过、不入表）——接不了的行永远停在 resting, 必须计进待人工核对。
//
// ⚠️ 限价取 **HotAsk**（观测里下给 CLOB 的那个限价, 与 OrderSpecForObs 的 price
// 同值）, 不是 FillPrice: 后者的语义是「已确认的成交均价」, 而 resting 行的成交
// 量尚未定稿、行里根本不写它（见 CompleteExecution）——用它当判据会让每条在途
// 挂单都被判成「接不了」, 告警常亮、真问题反而看不见。
func trackableResting(rec *Record) bool {
	return rec.OrderID != "" && rec.HotAsk > 0 && rec.Stake > 0
}

// isSettlable 判断记录是否应挂结算: **所有 ok 且未结算的行**——2026-09-24 起含
// 被闸/下单被拒/0 成交行（a.md 第 3 条「所有信号都要注册结算, 并在 dashboard 中
// 显示结算状态」）, 因为页面上每一笔信号都要显示官方结果。
//
// 唯一的排除项是**未定稿**的两种状态:
//   - submitting: POST 已发出、结果未知（行还没拿到 order_id 与终态）;
//   - resting:    GTC 挂单在簿、成交量未定（等 FillTracker 撤单时查 size_matched）。
//
// 两者都由定稿回调（CompleteExecution / CompleteRestingFill）推进后再注册——
// 提前注册会在成交量未知时就定案, 而 P&L 口径依赖真实成交。
//
// ⚠️ 注册 ≠ 有仓位: 无仓位的行（HasPosition 假）P&L 恒 0（recomputePnL）,
// 不进胜率、不动熔断与回撤——挂它们只为把官方 outcome 显示出来。
// legacy 的 scan 行同样注册（旧口径的反事实样本, 需要 outcome 才能算它的纸面盈亏）。
func isSettlable(rec *Record) bool {
	if (rec.Kind != KindSnap && rec.Kind != KindScan) || !rec.OK || rec.Won != nil || rec.ConditionID == "" {
		return false
	}
	switch rec.ExecStatus {
	case flip.ExecStatusSubmitting, flip.ExecStatusResting:
		return false
	}
	return true
}

// ── 词面映射与工具 ──

// utcDate 把 unix 毫秒时间戳映射为 UTC 日（YYYY-MM-DD，文件名/记录共用）。
func utcDate(tsMs int64) string {
	return time.UnixMilli(tsMs).UTC().Format("2006-01-02")
}

// utcToday 当前 UTC 日（日界唯一实现: 文件切分/日亏熔断同源）。
func utcToday() string {
	return utcDate(time.Now().UnixMilli())
}

// recordFilePath 生成日文件名（集中一处，rotate/rewrite/load 共用）。
func recordFilePath(dir, date string) string {
	return filepath.Join(dir, recordPrefix+date+".jsonl")
}

// windowFilePath 生成窗口日志日文件名（集中一处，rotate/load 共用）。
func windowFilePath(dir, date string) string {
	return filepath.Join(dir, windowPrefix+date+".jsonl")
}

// statsFilePath 生成窗口健康度日志日文件名（集中一处，rotate 共用；无载入路径）。
func statsFilePath(dir, date string) string {
	return filepath.Join(dir, statsPrefix+date+".jsonl")
}
