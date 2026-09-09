package flip

import "sync"

// σ 滚动窗常量: 容量与可用下限——与回测 hist 窗口数一致
// （python/v4/01_backtest_r1.py）。导出供 cmd/flip 启动预热段取窗数
// （Recorder.RecentWindows）与新鲜度判定（localFreshMax 派生）。
const (
	HistWindows = 18 // σ 容量: 前 ≤18 个已完成窗口振幅的均值
	HistMin     = 3  // σ 可用所需最少窗口数: 不足则 no_hist（冷启动期）
)

// HistState 维护 σ 的滚动窗口（线程安全）: hist_bps = 前 ≤HistWindows 个
// 已完成窗口 |tw_close − tw_open| 的均值，换算成 bps = mean/anchor·1e4
// （与回测口径一致，dist = Δ价/anchor·1e4/hist_bps）。
//
// 启动预热: 优先本地 windows_*.jsonl（Recorder 落盘的引擎自身窗口振幅，见
// cmd/flip warmupSigma），不足/陈旧时回退官方历史范围（feed.FetchTwapRanges）
// ——消除冷启动 no_hist 期。live 每窗口结束追加本窗 |close − anchor|（并同步
// 落盘一行）。Push 严格发生在窗口结束后——σ 永远只用已结束窗口，不混入当前窗。
type HistState struct {
	mu   sync.Mutex
	vals []float64 // 振幅（$），时间正序
}

// NewHistState 构造空 σ 滚动窗。
func NewHistState() *HistState { return &HistState{} }

// Seed 预热: 官方范围整表替换（热启动段在 ~19s 内完成，先于任何 live push）。
func (h *HistState) Seed(vals []float64) {
	if len(vals) == 0 {
		return
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	if len(vals) > HistWindows {
		vals = vals[len(vals)-HistWindows:] // 只留最近的
	}
	h.vals = append(h.vals[:0], vals...)
}

// Push 窗口结束后追加一个振幅（$）。
func (h *HistState) Push(amp float64) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.vals = append(h.vals, amp)
	if len(h.vals) > HistWindows {
		h.vals = append(h.vals[:0], h.vals[len(h.vals)-HistWindows:]...)
	}
}

// Bps 返回当前可用 σ（bps 口径）；不足 HistMin 窗返回 0（不可用）。anchor ≤0 恒不可用。
func (h *HistState) Bps(anchor float64) float64 {
	h.mu.Lock()
	defer h.mu.Unlock()
	if len(h.vals) < HistMin || anchor <= 0 {
		return 0
	}
	var sum float64
	for _, v := range h.vals {
		sum += v
	}
	return sum / float64(len(h.vals)) / anchor * 1e4
}

// Count 返回已收集窗口数（日志用）。
func (h *HistState) Count() int {
	h.mu.Lock()
	defer h.mu.Unlock()
	return len(h.vals)
}

// RecentBlock 从窗口振幅日志行（时间正序）截出最新一段连续块: 相邻条目结束
// 时刻缺口 ≤ gapMs 视为连续（正常 300s；σ 新鲜度守卫缺一窗恰为 600s），首个
// >gapMs 的缺口之前属更早的波动率 regime（停机/长期断档），整段丢弃。
// 调用方以块长与块内最新窗新鲜度决定是否本地 seed。
func RecentBlock(wins []WindowEntry, gapMs int64) []WindowEntry {
	for i := len(wins) - 1; i >= 1; i-- {
		if wins[i].Ts-wins[i-1].Ts > gapMs {
			return wins[i:]
		}
	}
	return wins
}

// AnchorUsableAtBoundary 判定窗口边界 anchor 采样是否可用: TWAP 尚未收到推送
// （price≤0）或推送陈旧（龄 > freshMs——阈值由调用方传入: cmd/flip 与窗口结束
// σ 采样共用 twapCloseFreshMs，正常推送龄 p99≈1.7s，10s 余量充足）判不可用。
// 不可用时调用方把 anchor 置 0 → 引擎按锚缺失整窗跳过（镜像回测 :69），
// σ 亦不计入（既有 anchor≤0 分支，零额外路径）。
func AnchorUsableAtBoundary(price float64, ageMs, freshMs int64) bool {
	return price > 0 && ageMs <= freshMs
}
