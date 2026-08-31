package flip

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

// Record 是一条穿越观测的完整落盘记录（成功与失败都记，校准信号频率用）。
//
// ⚠️ 与回测 trades_v3.csv 的差异（09-15 复验须经 python/v3/reuse_signals.py 映射）:
//   - side 值域 "up"/"down"（回测 csv 为 "yes"/"no"）
//   - fill 为对侧真实 ask@+10s（纸面/实盘口径）；回测 fill = flip_fill10s = 1 - 触发侧
//     bid@+10s = 本记录的 FillComp（回测口径），对比时须用 FillComp
//   - date 为 UTC 日（与回测一致）；won 即回测 flip_won
type Record struct {
	EventType    string  `json:"event_type"` // 恒为 "cross"
	Ts           int64   `json:"ts"`         // 穿越时刻（unix 毫秒）
	Date         string  `json:"date"`       // UTC 日（按日切分文件名，与回测 date 口径一致）
	ConditionID  string  `json:"condition_id"`
	Slug         string  `json:"slug"`
	EventStart   int64   `json:"event_start"`             // 窗口起点（unix 秒）
	Side         string  `json:"side"`                    // 触发侧: "up"/"down"
	Rem          int     `json:"rem"`                     // 穿越时窗口剩余秒
	TriggerBid   float64 `json:"trigger_bid"`             // C1
	PostEnd      float64 `json:"post_end"`                // C2
	Fill         float64 `json:"fill"`                    // 对侧真实 ask@+10s（纸面/实盘口径）
	FillComp     float64 `json:"fill_comp"`               // 互补价 1 - 触发侧bid@+10s（回测口径）
	Shares       float64 `json:"shares"`                  // stake/fill（仅 ok=true）
	Stake        float64 `json:"stake,omitempty"`         // 每笔投入 USDC（结算 P&L 基准；全部行记录，失败行无 P&L）
	OK           bool    `json:"ok"`                      // 是否通过 C1/C2
	RejectReason string  `json:"reject_reason,omitempty"` // 未通过原因
	BookLatMs    int64   `json:"book_latency_ms,omitempty"`
	TwapAgeMs    int64   `json:"twap_age_ms,omitempty"` // TWAP 距上次推送毫秒数（诊断）
	Cls          string  `json:"cls"`                   // 整窗类别: both/only（机制诊断）
	Won          *bool   `json:"won,omitempty"`         // 结算后填充（flip 是否赢）
	PnL          float64 `json:"pnl,omitempty"`         // 结算后填充（USDC）
	ResolvedAt   string  `json:"resolved_at,omitempty"` // 结算时间（RFC3339）
}

// Recorder 将穿越观测与信号写入 JSONL（按日切分文件）。
//
// 写入策略:
//   - 全部穿越观测（ok=false/ok=true）窗口结束时立即落盘，行级 Flush
//     —— 崩溃/断电不丢已记录事件；ok=true 行先写 won/pnl=null 部分字段
//   - 结算后 Resolve 用 conditionID 匹配内存 pending，回填 won/pnl 并
//     整体重写当日文件（temp+rename 原子替换，每事件仍一行）
//   - 启动时扫描已有文件恢复内存态（pending/resolved/failed），
//     未结算信号的 conditionID 由调用方重新注册结算轮询（崩溃重启不丢 P&L）
//   - 文件按 UTC 日切分: data/v3/crosses_YYYY-MM-DD.jsonl，追加模式
type Recorder struct {
	mu   sync.RWMutex
	dir  string
	file *os.File
	buf  *bufio.Writer
	day  string // 当前文件对应日（UTC）
	// 内存副本（Dashboard 查询 + 结算匹配）
	pending  []*Record // 未结算信号（ok=true）
	resolved []*Record // 已结算信号
	failed   []*Record // 失败穿越（ok=false，窗口结束时已落盘）
}

// NewRecorder 创建按日切分的 recorder，输出目录为 dir。
// 启动时扫描已有 JSONL 恢复内存态（崩溃重启后未结算信号可重新注册结算轮询）。
func NewRecorder(dir string) (*Recorder, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("create recorder dir: %w", err)
	}
	r := &Recorder{dir: dir}
	if err := r.loadPending(); err != nil {
		return nil, fmt.Errorf("恢复历史记录: %w", err)
	}
	return r, nil
}

// RecordCross 记录一次穿越观测（窗口结束时调用，cls 已知）。
// 无论成败立即落盘；ok=true 额外进入 pending 等待结算回填。
func (r *Recorder) RecordCross(conditionID, slug string, eventStart int64, c *Cross, cls string, stake float64) error {
	rec := &Record{
		EventType:    "cross",
		Ts:           c.Ts,
		Date:         time.UnixMilli(c.Ts).UTC().Format("2006-01-02"),
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
		Stake:        stake,
		OK:           c.OK,
		RejectReason: c.RejectReason,
		BookLatMs:    c.BookLatMs,
		TwapAgeMs:    c.TwapAgeMs,
		Cls:          cls,
	}

	r.mu.Lock()
	defer r.mu.Unlock()

	// 全部观测立即落盘（ok=true 行 won/pnl 留空，结算时回填重写）
	if err := r.write(rec); err != nil {
		return err
	}
	if !c.OK {
		r.failed = append(r.failed, rec)
	} else {
		r.pending = append(r.pending, rec)
	}
	return nil
}

// Resolve 结算信号（官方 outcome 到达后由 ResolutionPoller 回调）:
// won = 穿越侧未赢（outcome: 0=Up 1=Down）；pnl = shares-stake 赢 / -stake 输。
// 回填后整体重写当日文件（行数不变），移入 resolved。
// 写盘失败回滚结算字段保持可重试；同一 conditionID 重复调用安全（幂等）。
func (r *Recorder) Resolve(conditionID string, outcome int) error {
	r.mu.Lock()
	defer r.mu.Unlock()

	for _, rec := range r.pending {
		if rec.ConditionID != conditionID || rec.Won != nil {
			continue
		}
		won := false
		if rec.Side == "up" {
			won = outcome == 1 // UP 触发，DOWN 赢 = flip 赢
		} else {
			won = outcome == 0 // DOWN 触发，UP 赢 = flip 赢
		}
		rec.Won = &won
		if won {
			rec.PnL = rec.Shares - rec.Stake
		} else {
			rec.PnL = -rec.Stake
		}
		rec.ResolvedAt = time.Now().UTC().Format(time.RFC3339)

		if err := r.rewriteDay(rec); err != nil {
			// 写盘失败: 回滚结算字段，保持可重试（poller 回调失败不移除 pending）
			rec.Won, rec.PnL, rec.ResolvedAt = nil, 0, ""
			return fmt.Errorf("回写结算记录 %s: %w", rec.ConditionID, err)
		}
		r.resolved = append(r.resolved, rec)
		r.pending = removeRecord(r.pending, rec)
		return nil
	}
	return nil
}

// Close 关闭当前文件（所有观测已即时落盘，无待刷数据）。
func (r *Recorder) Close() error {
	r.mu.Lock()
	defer r.mu.Unlock()
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

// rewriteDay 将一条记录的结算字段回写当日 JSONL。
// 当日文件按行重写（日 ~200 行，成本可忽略），temp+rename 原子替换；
// 若重写的是当前打开文件，需重开文件句柄（rename 后旧句柄指向失效 inode）。
// 调用方持锁。
func (r *Recorder) rewriteDay(rec *Record) error {
	day := time.UnixMilli(rec.Ts).UTC().Format("2006-01-02")
	path := filepath.Join(r.dir, "crosses_"+day+".jsonl")
	data, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("读取记录文件: %w", err)
	}
	var out strings.Builder
	written := false
	for _, ln := range strings.Split(strings.TrimRight(string(data), "\n"), "\n") {
		if ln == "" {
			continue
		}
		if !written {
			var cur Record
			if err := json.Unmarshal([]byte(ln), &cur); err == nil &&
				cur.ConditionID == rec.ConditionID && cur.Ts == rec.Ts {
				b, err := json.Marshal(rec)
				if err != nil {
					return fmt.Errorf("marshal record: %w", err)
				}
				out.Write(b)
				out.WriteByte('\n')
				written = true
				continue
			}
		}
		out.WriteString(ln)
		out.WriteByte('\n')
	}
	if !written {
		return fmt.Errorf("记录文件中未找到 conditionID=%s ts=%d", rec.ConditionID, rec.Ts)
	}
	tmp, err := os.CreateTemp(r.dir, "crosses_*.tmp")
	if err != nil {
		return fmt.Errorf("创建临时文件: %w", err)
	}
	if _, err := tmp.WriteString(out.String()); err != nil {
		tmp.Close()
		os.Remove(tmp.Name())
		return fmt.Errorf("写入临时文件: %w", err)
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmp.Name())
		return fmt.Errorf("关闭临时文件: %w", err)
	}
	if err := os.Rename(tmp.Name(), path); err != nil {
		os.Remove(tmp.Name())
		return fmt.Errorf("替换记录文件: %w", err)
	}
	if day == r.day {
		// 重开当前文件（旧句柄指向 rename 前的 inode，追加会丢失）
		return r.rotate(day)
	}
	return nil
}

// loadPending 扫描已有 JSONL 恢复内存态（崩溃重启后未结算信号可重新注册结算轮询）。
// 调用方: NewRecorder（无锁，构造阶段）。
func (r *Recorder) loadPending() error {
	paths, err := filepath.Glob(filepath.Join(r.dir, "crosses_*.jsonl"))
	if err != nil {
		return err
	}
	for _, path := range paths {
		f, err := os.Open(path)
		if err != nil {
			return fmt.Errorf("打开 %s: %w", path, err)
		}
		sc := bufio.NewScanner(f)
		sc.Buffer(make([]byte, 0, 4096), 1<<20)
		for sc.Scan() {
			line := strings.TrimSpace(sc.Text())
			if line == "" {
				continue
			}
			var rec Record
			if err := json.Unmarshal([]byte(line), &rec); err != nil {
				continue // 跳过损坏行，不影响其余恢复
			}
			switch {
			case rec.OK && rec.Won == nil:
				r.pending = append(r.pending, &rec)
			case rec.OK && rec.Won != nil:
				r.resolved = append(r.resolved, &rec)
			default:
				r.failed = append(r.failed, &rec)
			}
		}
		closeErr := sc.Err()
		f.Close()
		if closeErr != nil {
			return fmt.Errorf("扫描 %s: %w", path, closeErr)
		}
	}
	return nil
}

// PendingSignals 返回未结算信号副本（崩溃重启后重新注册结算轮询用）。
func (r *Recorder) PendingSignals() []*Record {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return append([]*Record(nil), r.pending...)
}

// ── 写入 ──

// write 序列化并追加一行到当日文件（调用方持锁）。
// 按 UTC 日切分（与回测 date 口径一致，避免本地时区跨日错位）。
func (r *Recorder) write(rec *Record) error {
	day := time.UnixMilli(rec.Ts).UTC().Format("2006-01-02")
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
	// 每行立即刷盘: 穿越观测低频（日 ~200 条），崩溃/断电不丢已记录事件
	if err := r.buf.Flush(); err != nil {
		return fmt.Errorf("flush record: %w", err)
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
	r.mu.RLock()
	defer r.mu.RUnlock()
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

// Signals 返回全部信号（ok=true，含已结算与待结算），按时间倒序。
func (r *Recorder) Signals() []*Record {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make([]*Record, 0, len(r.resolved)+len(r.pending))
	out = append(out, r.resolved...)
	out = append(out, r.pending...)
	sort.Slice(out, func(i, j int) bool { return out[i].Ts > out[j].Ts })
	return out
}

// Count 返回穿越观测总数（含失败；Dashboard 汇总用，避免全量拷贝排序）。
func (r *Recorder) Count() int {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return len(r.resolved) + len(r.pending) + len(r.failed)
}

// Stats 返回 Dashboard 汇总: 信号总数/胜/负/待结算/胜率/累计 P&L。
func (r *Recorder) Stats() (total, won, lost, pending int, winRate, cumPnl float64) {
	r.mu.RLock()
	defer r.mu.RUnlock()
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

// DailyPnl 返回逐日 P&L 与盈利天数（已结算信号，Dashboard 用）。
func (r *Recorder) DailyPnl() (map[string]float64, int) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make(map[string]float64)
	for _, rec := range r.resolved {
		out[rec.Date] += rec.PnL
	}
	pos := 0
	for _, v := range out {
		if v > 0 {
			pos++
		}
	}
	return out, pos
}

// MaxDrawdown 返回已结算信号的累计 P&L 最大回撤（USDC，按结算时间顺序）。
func (r *Recorder) MaxDrawdown() float64 {
	r.mu.RLock()
	defer r.mu.RUnlock()
	resolved := append([]*Record(nil), r.resolved...)
	sort.Slice(resolved, func(i, j int) bool { return resolved[i].Ts < resolved[j].Ts })
	var cum, peak, maxDD float64
	for _, rec := range resolved {
		cum += rec.PnL
		if cum > peak {
			peak = cum
		}
		if dd := peak - cum; dd > maxDD {
			maxDD = dd
		}
	}
	return maxDD
}

func removeRecord(list []*Record, target *Record) []*Record {
	for i, rec := range list {
		if rec == target {
			return append(list[:i], list[i+1:]...)
		}
	}
	return list
}
