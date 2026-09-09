package flip

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
)

// 记录文件前缀与行 schema 标识（rotate/rewriteDay/loadPending 共用，防新旧格式混写）:
// 文件名 touches_YYYY-MM-DD.jsonl，每行一个 Record（event_type="touch"）。
const (
	recordPrefix    = "touches_"
	recordEventType = "touch"
)

// 窗口振幅日志（σ 重启本地预热的数据源）: 文件名 windows_YYYY-MM-DD.jsonl，
// 每行一个 WindowEntry（每完成一个窗口落一行，5 分钟粒度）。
// 独立于 touches_*.jsonl——结算 rewriteDay 只重写 touches 当日文件，
// 窗口行无回填需求（追加即终稿），混入同一文件会被结算重写丢掉。
const windowPrefix = "windows_"

// Recorder 追加写 JSONL 观测记录（按 UTC 日切分文件），外加每完成窗口
// 一行的窗口振幅日志 windows_*.jsonl（σ 重启本地预热的数据源，见
// LogWindowAmplitude —— 独立文件、独立句柄，与 touches 互不干扰）。
//
// 观测行在触底 tick 产生后立即落盘（bufio 行级 flush，崩溃不丢；重启用
// 文件恢复内存态）。ok 信号同时进 pending；结算回填（Resolve）用
// temp+rename 原子重写当日文件并刷新内存记录。
type Recorder struct {
	mu sync.Mutex

	dir     string
	recs    []*Record          // 全部观测（时间正序：载入序 + 追加序）
	pending map[string]*Record // 未结算 ok 信号，key = conditionID

	day  string // 当前打开文件的 UTC 日
	file *os.File
	buf  *bufio.Writer

	wins    []WindowEntry // 已完成窗口振幅（时间正序：载入序 + 追加序）
	winDay  string        // 窗口日志当前打开文件的 UTC 日
	winFile *os.File
	winBuf  *bufio.Writer
}

// DayPnl 单日已结算 P&L（Dashboard 用）。
type DayPnl struct {
	Date string  `json:"date"`
	PnL  float64 `json:"pnl"`
	N    int     `json:"n"` // 当日已结算信号数
}

// WindowEntry 是一个已完成窗口的 σ 贡献行（amp = |close − anchor|）。
// anchor/close 为 TWAP-60 流值（live 口径，与观测行/touches 同源），
// 重启时供 σ 本地预热（语义对齐回测 |close−open|，见 cmd/flip 预热段）。
type WindowEntry struct {
	Ts          int64   `json:"ts"` // 窗口结束时刻（unix 毫秒）
	Date        string  `json:"date"`
	ConditionID string  `json:"condition_id"`
	Slug        string  `json:"slug"`
	EventStart  int64   `json:"event_start"`
	Anchor      float64 `json:"anchor"` // 边界 TWAP-60 流值
	Close       float64 `json:"close"`  // 窗口结束 TWAP-60 流值
	Amp         float64 `json:"amp"`    // |close − anchor|（USD）
}

// NewRecorder 打开（必要时创建）输出目录并载入既有记录。
// loadPending 校验行 schema：event_type 非 "touch" 的行跳过并告警——
// -output 指错目录时旧格式（v3 crosses_* 等）不会静默污染统计。
// 窗口振幅日志（windows_*.jsonl）一并载入内存（σ 本地预热数据源）。
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
		log.Printf("[Recorder] 载入 %s（%d 行, %d 待结算）", filepath.Base(f), n, len(r.pending))
	}
	if skipped > 0 {
		log.Printf("⚠️ [Recorder] 跳过 %d 行不匹配 schema（event_type != %q）", skipped, recordEventType)
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
		log.Printf("[Recorder] 载入 %s（%d 窗）", filepath.Base(f), n)
	}
	return r, nil
}

// loadWindowFileLocked 读入一个窗口日志日文件（调用方已持锁）。返回行数。
func (r *Recorder) loadWindowFileLocked(path string) (int, error) {
	f, err := os.Open(path)
	if err != nil {
		return 0, fmt.Errorf("Recorder: 打开 %s: %w", path, err)
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
		var e WindowEntry
		if err := json.Unmarshal(line, &e); err != nil {
			log.Printf("⚠️ [Recorder] %s: 窗口行解析失败跳过: %v", path, err)
			continue
		}
		if e.Ts <= 0 || e.Date == "" {
			log.Printf("⚠️ [Recorder] %s: 跳过异常窗口行（ts=%d）", path, e.Ts)
			continue
		}
		r.wins = append(r.wins, e)
		rows++
	}
	if err := sc.Err(); err != nil {
		return 0, fmt.Errorf("Recorder: 读 %s: %w", path, err)
	}
	return rows, nil
}

// loadFileLocked 读入一个日文件的行（调用方已持锁）。返回行数/跳过数。
func (r *Recorder) loadFileLocked(path string) (int, int, error) {
	f, err := os.Open(path)
	if err != nil {
		return 0, 0, fmt.Errorf("Recorder: 打开 %s: %w", path, err)
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
			log.Printf("⚠️ [Recorder] %s: 行解析失败跳过: %v", path, err)
			skipped++
			continue
		}
		if rec.EventType != recordEventType || rec.Date == "" {
			log.Printf("⚠️ [Recorder] %s: 跳过旧格式行（event_type=%q）", path, rec.EventType)
			skipped++
			continue
		}
		r.recs = append(r.recs, &rec)
		// 崩溃恢复: pending 重建只收「真实持仓待结算」行（ok 信号 && 未结算 &&
		// paper 或 live filled/partial——unfilled/rejected/submitting 无真实持仓,
		// 不注册结算轮询; submitting/unknown-rejected 行打 ⚠️ 人工核对）
		if rec.OK && rec.Won == nil && rec.ConditionID != "" {
			if isSettlable(&rec) {
				r.pending[rec.ConditionID] = &rec // 重启后重新注册结算轮询
			} else if rec.ExecStatus == ExecStatusSubmitting ||
				(rec.ExecStatus == ExecStatusRejected && strings.HasPrefix(rec.ExecNote, ExecNoteUnknown)) {
				// 执行中断（下单后崩溃/POST 结果不明）: order_id 大概率不可得,
				// 不自动补单——按 maker+时间窗去 data-api 核对该窗是否真成交
				log.Printf("⚠️ [Recorder] %s: condition=%s exec=%s 执行中断需人工核对——勿自动补单, 按 maker+时间窗去 data-api 核对。slug=%s event_start=%d ts=%d side=%s fill=%.3f stake=%.1f note=%q",
					filepath.Base(path), rec.ConditionID, rec.ExecStatus,
					rec.Slug, rec.EventStart, rec.Ts, rec.Side, rec.Fill, rec.Stake, rec.ExecNote)
			}
		}
		rows++
	}
	if err := sc.Err(); err != nil {
		return 0, 0, fmt.Errorf("Recorder: 读 %s: %w", path, err)
	}
	return rows, skipped, nil
}

// newRecord 构造记录行（时间/日期从观测快照派生）。
func newRecord(condID, slug string, eventStart int64, obs *Observation, stake float64) *Record {
	return &Record{
		Observation: *obs, // 值拷贝：观测判定后即快照
		EventType:   recordEventType,
		Date:        utcDate(obs.Ts),
		ConditionID: condID,
		Slug:        slug,
		EventStart:  eventStart,
		Stake:       stake,
	}
}

// RecordObservation 将一次触底观测立即落盘（paper 模拟成交: ok 与失败观测都记录,
// ExecStatus 空串）。返回生成的记录行（已入内存 + flush 到当日文件）。
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

// ── live 两阶段落盘（2026-09-10 实盘接入）──

// SubmitLiveObservation 落盘 live 下单的 submitting 行（两阶段第一步, POST 发起
// 前立即写, 行级 flush 崩溃不丢）。不入 pending、不注册结算——成交结果由
// CompleteExecution 回填后按 isSettlable 决定。返回记录行。
func (r *Recorder) SubmitLiveObservation(condID, slug string, eventStart int64, obs *Observation, stake float64) (*Record, error) {
	r.mu.Lock()
	defer r.mu.Unlock()

	rec := newRecord(condID, slug, eventStart, obs, stake)
	rec.ExecStatus = ExecStatusSubmitting
	if err := r.writeLocked(rec); err != nil {
		return nil, err
	}
	return rec, nil
}

// RecordLiveRejected 落盘 live 风控闸拒绝行（熔断/首窗禁单等: 未发起下单, 无
// submitting 中间态）。OK=true（策略信号本身成立）但 ExecStatus=rejected →
// IsFilled=false 不注册结算; 观测保留供 live 信号频率口径。
func (r *Recorder) RecordLiveRejected(condID, slug string, eventStart int64, obs *Observation, stake float64, note string) (*Record, error) {
	r.mu.Lock()
	defer r.mu.Unlock()

	rec := newRecord(condID, slug, eventStart, obs, stake)
	rec.ExecStatus = ExecStatusRejected
	rec.ExecNote = note
	if err := r.writeLocked(rec); err != nil {
		return nil, err
	}
	return rec, nil
}

// CompleteExecution 回填一个 submitting 行的执行结果（两阶段第二步, POST 同步
// 响应后立即调用）: 更新 ExecStatus/OrderID/FillPrice/Shares/Cost/ExecNote,
// filled/partial（真实持仓）且 ok 时入 pending; rewriteDayLocked 原子回填当日
// 文件（temp+rename, 崩溃不产生半行）。Shares 仅成交时覆盖为实际股数
// （unfilled/rejected 保留目标股数供分析）。返回更新后记录与是否已成交。
// submitting 行不存在或状态不符 → error（重启缝隙/重复回填, 告警不静默）。
func (r *Recorder) CompleteExecution(conditionID string, res ExecResult) (*Record, bool, error) {
	r.mu.Lock()
	defer r.mu.Unlock()

	switch res.Status {
	case ExecStatusFilled, ExecStatusPartial, ExecStatusUnfilled, ExecStatusRejected:
	default:
		return nil, false, fmt.Errorf("CompleteExecution: %s 非法状态 %q", conditionID, res.Status)
	}
	var rec *Record
	for _, x := range r.recs {
		if x.ConditionID == conditionID {
			rec = x
			break
		}
	}
	if rec == nil {
		return nil, false, fmt.Errorf("CompleteExecution: %s 无落盘行（重启缝隙?）", conditionID)
	}
	if rec.ExecStatus != ExecStatusSubmitting {
		return nil, false, fmt.Errorf("CompleteExecution: %s 行状态 %q 非 submitting（重复回填或行损坏）", conditionID, rec.ExecStatus)
	}

	rec.ExecStatus = res.Status
	rec.OrderID = res.OrderID
	rec.ExecNote = res.Note
	if res.Status == ExecStatusFilled || res.Status == ExecStatusPartial {
		rec.FillPrice = res.FillPrice
		rec.Shares = res.Shares // 目标股数 → 实际成交股数
		rec.Cost = res.Cost
	}
	filled := rec.IsFilled()
	if isSettlable(rec) {
		r.pending[conditionID] = rec
	}
	if err := r.rewriteDayLocked(rec.Date); err != nil {
		log.Printf("⚠️ [Recorder] 执行回填重写失败: %v", err)
	}
	return rec, filled, nil
}

// Resolve 结算一个 ok 信号：按官方 outcome 判定狗侧输赢并回填记录，
// 原子重写该日文件。命中返回 true。
func (r *Recorder) Resolve(conditionID string, outcome int, at time.Time) bool {
	r.mu.Lock()
	defer r.mu.Unlock()

	rec, found := r.pending[conditionID]
	if !found {
		return false
	}
	won := WonFor(rec.Side, outcome)
	// cost 口径（2026-09-10 live 接入）: 实际成交花费 Cost（live filled/partial
	// 回填）; paper 行无 Cost → Stake 兜底, 公式与旧版恒等
	cost := rec.Cost
	if cost <= 0 {
		cost = rec.Stake
	}
	pnl := -cost
	if won {
		pnl = rec.Shares - cost // 每股兑 1U（Shares = 实际成交股数）
	}
	rec.Won = &won
	rec.PnL = pnl
	rec.ResolvedAt = at.UTC().Format(time.RFC3339)
	delete(r.pending, conditionID)

	if err := r.rewriteDayLocked(rec.Date); err != nil {
		log.Printf("⚠️ [Recorder] 结算重写失败: %v", err)
	}
	return true
}

// writeLocked 追加写一行并 flush（调用方已持锁）。
func (r *Recorder) writeLocked(rec *Record) error {
	if err := r.openDayLocked(rec.Date); err != nil {
		return err
	}
	r.recs = append(r.recs, rec)
	line, err := json.Marshal(rec)
	if err != nil {
		return fmt.Errorf("Recorder: marshal: %w", err)
	}
	if _, err := r.buf.Write(line); err != nil {
		return err
	}
	if err := r.buf.WriteByte('\n'); err != nil {
		return err
	}
	return r.buf.Flush()
}

// rewriteDayLocked 原子重写一个日文件（temp+rename），行 = 该日全部记录
// （含刚回填的结算字段）。目标日 == 当前打开日时先关闭再重开（调用方已持锁）。
func (r *Recorder) rewriteDayLocked(date string) error {
	if date == r.day {
		if err := r.closeCurrentLocked(); err != nil {
			return err
		}
	}
	tmp := recordFilePath(r.dir, date) + ".tmp"
	f, err := os.Create(tmp)
	if err != nil {
		return fmt.Errorf("Recorder: 创建临时文件: %w", err)
	}
	w := bufio.NewWriter(f)
	for _, rec := range r.recs {
		if rec.Date != date {
			continue
		}
		line, err := json.Marshal(rec)
		if err != nil {
			f.Close()
			return fmt.Errorf("Recorder: marshal: %w", err)
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
		return fmt.Errorf("Recorder: rename: %w", err)
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
		return fmt.Errorf("Recorder: 打开 %s: %w", date, err)
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
// 仅当窗口数据有效时调用（与 cmd/flip histState.push 同分支）；落盘失败返回
// 错误由调用方告警继续——σ 内存窗不受影响，只是下次重启本地预热缺此窗。
func (r *Recorder) LogWindowAmplitude(condID, slug string, eventStart int64, end time.Time, anchor, close_, amp float64) error {
	r.mu.Lock()
	defer r.mu.Unlock()

	date := utcDate(end.UnixMilli())
	if err := r.openWinDayLocked(date); err != nil {
		return err
	}
	e := WindowEntry{
		Ts:          end.UnixMilli(),
		Date:        date,
		ConditionID: condID,
		Slug:        slug,
		EventStart:  eventStart,
		Anchor:      anchor,
		Close:       close_,
		Amp:         amp,
	}
	r.wins = append(r.wins, e)
	line, err := json.Marshal(e)
	if err != nil {
		return fmt.Errorf("Recorder: marshal 窗口行: %w", err)
	}
	if _, err := r.winBuf.Write(line); err != nil {
		return err
	}
	if err := r.winBuf.WriteByte('\n'); err != nil {
		return err
	}
	return r.winBuf.Flush()
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
		return fmt.Errorf("Recorder: 打开窗口日志 %s: %w", date, err)
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

// Close flush 并关闭当前文件（观测 + 窗口日志）。
func (r *Recorder) Close() error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if err := r.closeWinDayLocked(); err != nil {
		return err
	}
	return r.closeCurrentLocked()
}

// ── 只读访问（Dashboard / 恢复用）──

// Observations 返回全部观测（时间正序）。
func (r *Recorder) Observations() []*Record {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]*Record(nil), r.recs...)
}

// Signals 返回全部 ok 信号（时间正序）。
func (r *Recorder) Signals() []*Record {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]*Record, 0, len(r.recs))
	for _, rec := range r.recs {
		if rec.OK {
			out = append(out, rec)
		}
	}
	return out
}

// RecentWindows 返回最近 n 个已完成窗口振幅行（时间正序；不足则返回全部）。
// cmd/flip 启动时据此做 σ 本地预热（见其 histState 预热段）。
func (r *Recorder) RecentWindows(n int) []WindowEntry {
	r.mu.Lock()
	defer r.mu.Unlock()
	start := 0
	if len(r.wins) > n {
		start = len(r.wins) - n
	}
	return append([]WindowEntry(nil), r.wins[start:]...)
}

// isSettlable 判断记录是否应挂结算（真实持仓 + 未结算）: ok 信号 && 未结算 &&
// paper 行（ExecStatus 空, 模拟成交）或 live filled/partial。unfilled/rejected/
// submitting 行无真实持仓——不入 pending、不注册结算轮询。
func isSettlable(rec *Record) bool {
	if !rec.OK || rec.Won != nil || rec.ConditionID == "" {
		return false
	}
	switch rec.ExecStatus {
	case "", ExecStatusFilled, ExecStatusPartial:
		return true
	}
	return false
}

// HasRecord 判断某 conditionID（窗口市场）是否已有落盘记录（观测 ok/否决都算）。
// cmd/flip 快速重启防重入用: 崩溃后 ≤15s 内重启会按「迟到准入」重入上一进程
// 未跑完的同一窗口——已触发落盘的窗口不允许二次运行（防同窗双记录 + pending
// 覆盖致一笔悬空不结算）。live submitting 行同样算已触发（双单防护）。
func (r *Recorder) HasRecord(conditionID string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, rec := range r.recs {
		if rec.ConditionID == conditionID {
			return true
		}
	}
	return false
}

// NeedsReconcile 返回执行中断待人工核对的行数（live: submitting = 下单后崩溃;
// rejected 且 note 以 ExecNoteUnknown 开头 = POST 结果不明, 可能已成交）。
// 重启后 cmd/flip 据此汇总告警——不自动补单、不自动注册。
func (r *Recorder) NeedsReconcile() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	n := 0
	for _, rec := range r.recs {
		switch rec.ExecStatus {
		case ExecStatusSubmitting:
			n++
		case ExecStatusRejected:
			if strings.HasPrefix(rec.ExecNote, ExecNoteUnknown) {
				n++
			}
		}
	}
	return n
}

// PendingSignals 返回未结算的 ok 信号（重启后据此重新注册结算轮询）。
func (r *Recorder) PendingSignals() []*Record {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]*Record, 0, len(r.pending))
	for _, rec := range r.pending {
		out = append(out, rec)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Ts < out[j].Ts })
	return out
}

// Counts 统计观测/信号/已赢笔数（Dashboard 状态栏）。
func (r *Recorder) Counts() (obs, sig, won int) {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, rec := range r.recs {
		obs++
		if rec.OK {
			sig++
			if rec.Won != nil && *rec.Won {
				won++
			}
		}
	}
	return
}

// DailyPnl 按日汇总已结算 P&L（正序；Dashboard /api/state）。
func (r *Recorder) DailyPnl() []DayPnl {
	r.mu.Lock()
	defer r.mu.Unlock()
	byDay := map[string]*DayPnl{}
	for _, rec := range r.recs {
		if !rec.OK || rec.Won == nil {
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
func (r *Recorder) MaxDrawdown() float64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	cum, peak, dd := 0.0, 0.0, 0.0
	for _, rec := range r.recs {
		if !rec.OK || rec.Won == nil {
			continue
		}
		cum += rec.PnL
		if cum > peak {
			peak = cum
		}
		if c := cum - peak; c < dd {
			dd = c
		}
	}
	return dd
}

// ── 词面映射与工具 ──

// WonFor 狗侧是否赢：side=yes → outcome==OutcomeUp(0)；side=no → outcome==OutcomeDown(1)。
func WonFor(side string, outcome int) bool {
	if side == SideYes {
		return outcome == OutcomeUp
	}
	return outcome == OutcomeDown
}

// utcDate 把 unix 毫秒时间戳映射为 UTC 日（YYYY-MM-DD，文件名/记录共用）。
func utcDate(tsMs int64) string {
	return time.UnixMilli(tsMs).UTC().Format("2006-01-02")
}

// recordFilePath 生成日文件名（集中一处，rotate/rewrite/load 共用）。
func recordFilePath(dir, date string) string {
	return filepath.Join(dir, recordPrefix+date+".jsonl")
}

// windowFilePath 生成窗口日志日文件名（集中一处，rotate/load 共用）。
func windowFilePath(dir, date string) string {
	return filepath.Join(dir, windowPrefix+date+".jsonl")
}
