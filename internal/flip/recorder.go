package flip

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"
)

// Record 是一条穿越观测的完整落盘记录（成功与失败都记，校准信号频率用）。
// 字段与回测 trades_v3.csv 对齐（date/event_start/side/trigger_bid/post_end/
// fill/shares/ok/cls），09-15 可直接复用 python 分析逻辑。
type Record struct {
	EventType    string  `json:"event_type"`              // 恒为 "cross"
	Ts           int64   `json:"ts"`                      // 穿越时刻（unix 毫秒）
	Date         string  `json:"date"`                    // 本地日（按日切分文件名）
	ConditionID  string  `json:"condition_id"`
	Slug         string  `json:"slug"`
	EventStart   int64   `json:"event_start"`             // 窗口起点（unix 秒）
	Side         string  `json:"side"`                    // 触发侧: "yes"/"no"
	Rem          int     `json:"rem"`                     // 穿越时窗口剩余秒
	TriggerBid   float64 `json:"trigger_bid"`             // C1
	PostEnd      float64 `json:"post_end"`                // C2
	Fill         float64 `json:"fill"`                    // 对侧真实 ask@+10s（纸面/实盘口径）
	FillComp     float64 `json:"fill_comp"`               // 互补价 1 - 触发侧bid@+10s（回测口径）
	Shares       float64 `json:"shares"`                  // stake/fill（仅 ok=true）
	OK           bool    `json:"ok"`                      // 是否通过 C1/C2
	RejectReason string  `json:"reject_reason,omitempty"` // 未通过原因
	BookLatMs    int64   `json:"book_latency_ms,omitempty"`
	TwapAgeMs    int64   `json:"twap_age_ms,omitempty"`   // TWAP 距上次推送毫秒数（诊断）
	Cls          string  `json:"cls"`                     // 整窗类别: both/only（机制诊断）
	Won          *bool   `json:"won,omitempty"`           // 结算后填充（flip 是否赢）
	PnL          float64 `json:"pnl,omitempty"`           // 结算后填充（USDC）
	ResolvedAt   string  `json:"resolved_at,omitempty"`   // 结算时间（RFC3339）
}

// Recorder 将穿越观测与信号写入 JSONL（按日切分文件）。
//
// 写入策略:
//   - 失败穿越（ok=false）窗口结束时立即落盘（不参与结算）
//   - 成功信号（ok=true）先在内存缓存（pending），官方结算后回填 won/pnl
//     一次性写完整行（每事件一行，字段完整）；崩溃时 Close 刷出部分字段行
//   - 文件按本地日切分: data/v3/crosses_YYYY-MM-DD.jsonl，追加模式，重启不丢
type Recorder struct {
	mu    sync.Mutex
	dir   string
	file  *os.File
	buf   *bufio.Writer
	day   string // 当前文件对应日
	// 内存副本（Dashboard 查询）
	pending  []*Record // 未结算信号（ok=true）
	resolved []*Record // 已结算信号
	failed   []*Record // 失败穿越（ok=false，窗口结束时已落盘）
}

// NewRecorder 创建按日切分的 recorder，输出目录为 dir。
func NewRecorder(dir string) (*Recorder, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("create recorder dir: %w", err)
	}
	return &Recorder{dir: dir}, nil
}

// RecordCross 记录一次穿越观测（窗口结束时调用，cls 已知）。
// ok=false 立即落盘；ok=true 进入 pending 等待结算。
func (r *Recorder) RecordCross(conditionID, slug string, eventStart int64, c *Cross, cls string) error {
	rec := &Record{
		EventType:    "cross",
		Ts:           c.Ts,
		Date:         time.UnixMilli(c.Ts).Format("2006-01-02"),
		ConditionID:  conditionID,
		Slug:         slug,
		EventStart:   eventStart,
		Side:         c.Side,
		Rem:          c.Rem,
		TriggerBid:   c.TriggerBid,
		PostEnd:      c.PostEnd,
		Fill:         c.Fill,
		FillComp:     c.FillComp,
		Shares:       c.Shares,
		OK:           c.OK,
		RejectReason: c.RejectReason,
		BookLatMs:    c.BookLatMs,
		TwapAgeMs:    c.TwapAgeMs,
		Cls:          cls,
	}

	r.mu.Lock()
	defer r.mu.Unlock()

	if !c.OK {
		r.failed = append(r.failed, rec)
		return r.write(rec)
	}
	r.pending = append(r.pending, rec)
	return nil
}

// Resolve 结算信号（官方 outcome 到达后由 ResolutionPoller 回调）:
// won = 穿越侧未赢（outcome: 0=Up 1=Down）；pnl = shares-stake 赢 / -stake 输。
// 回填后写完整行，移入 resolved。同一 conditionID 重复调用安全（幂等）。
func (r *Recorder) Resolve(conditionID string, outcome int) error {
	r.mu.Lock()
	defer r.mu.Unlock()

	for _, rec := range r.pending {
		if rec.ConditionID != conditionID || rec.Won != nil {
			continue
		}
		won := false
		if rec.Side == "yes" {
			won = outcome == 1 // YES 触发，DOWN 赢 = flip 赢
		} else {
			won = outcome == 0 // NO 触发，UP 赢 = flip 赢
		}
		rec.Won = &won
		if won {
			rec.PnL = rec.Shares - r.stakeOf(rec)
		} else {
			rec.PnL = -r.stakeOf(rec)
		}
		rec.ResolvedAt = time.Now().UTC().Format(time.RFC3339)

		if err := r.write(rec); err != nil {
			return err
		}
		r.resolved = append(r.resolved, rec)
		r.pending = removeRecord(r.pending, rec)
		return nil
	}
	return nil
}

// stakeOf 从 shares/fill 反推本金（stake = shares × fill，与回测 2U 口径一致）。
func (r *Recorder) stakeOf(rec *Record) float64 {
	if rec.Fill > 0 {
		return rec.Shares * rec.Fill
	}
	return 0
}

// Close 刷出未结算信号（部分字段，崩溃恢复诊断）并关闭文件。
func (r *Recorder) Close() error {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, rec := range r.pending {
		if err := r.write(rec); err != nil {
			return err
		}
	}
	r.pending = nil
	if r.buf != nil {
		if err := r.buf.Flush(); err != nil {
			return err
		}
	}
	if r.file != nil {
		return r.file.Close()
	}
	return nil
}

// ── 写入 ──

// write 序列化并追加一行到当日文件（调用方持锁）。
func (r *Recorder) write(rec *Record) error {
	day := time.UnixMilli(rec.Ts).Format("2006-01-02")
	if r.file == nil || day != r.day {
		if err := r.rotate(day); err != nil {
			return err
		}
	}
	data, err := json.Marshal(rec)
	if err != nil {
		return fmt.Errorf("marshal record: %w", err)
	}
	if _, err := r.buf.Write(append(data, '\n')); err != nil {
		return fmt.Errorf("write record: %w", err)
	}
	return nil
}

// rotate 切换当日文件（调用方持锁）。
func (r *Recorder) rotate(day string) error {
	if r.buf != nil {
		if err := r.buf.Flush(); err != nil {
			return err
		}
	}
	if r.file != nil {
		if err := r.file.Close(); err != nil {
			return err
		}
	}
	path := filepath.Join(r.dir, "crosses_"+day+".jsonl")
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return fmt.Errorf("open recorder file: %w", err)
	}
	r.file = f
	r.buf = bufio.NewWriter(f)
	r.day = day
	return nil
}

// ── Dashboard 访问器 ──

// Crosses 返回最近穿越观测（含失败，按时间倒序）。
func (r *Recorder) Crosses(limit int) []*Record {
	r.mu.Lock()
	defer r.mu.Unlock()
	all := make([]*Record, 0, len(r.resolved)+len(r.pending)+len(r.failed))
	all = append(all, r.resolved...)
	all = append(all, r.pending...)
	all = append(all, r.failed...)
	sort.Slice(all, func(i, j int) bool { return all[i].Ts > all[j].Ts })
	if len(all) > limit {
		all = all[:limit]
	}
	return all
}

// Signals 返回全部信号（ok=true，含已结算与待结算）。
func (r *Recorder) Signals() []*Record {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]*Record, 0, len(r.resolved)+len(r.pending))
	out = append(out, r.resolved...)
	out = append(out, r.pending...)
	return out
}

// Stats 返回 Dashboard 汇总: 信号总数/胜/负/待结算/胜率/累计 P&L。
func (r *Recorder) Stats() (total, won, lost, pending int, winRate, cumPnl float64) {
	r.mu.Lock()
	defer r.mu.Unlock()
	total = len(r.resolved) + len(r.pending)
	pending = len(r.pending)
	for _, rec := range r.resolved {
		if rec.Won != nil && *rec.Won {
			won++
		} else {
			lost++
		}
		cumPnl += rec.PnL
	}
	if won+lost > 0 {
		winRate = float64(won) / float64(won+lost)
	}
	return
}

// DailyPnl 返回逐日 P&L（Dashboard 用）。
func (r *Recorder) DailyPnl() map[string]float64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make(map[string]float64)
	for _, rec := range r.resolved {
		out[rec.Date] += rec.PnL
	}
	return out
}

func removeRecord(list []*Record, target *Record) []*Record {
	for i, rec := range list {
		if rec == target {
			return append(list[:i], list[i+1:]...)
		}
	}
	return list
}
