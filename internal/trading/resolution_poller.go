package trading

import (
	"context"
	"log"
	"strconv"
	"sync"
	"time"

	"github.com/tidwall/gjson"
	"github.com/xiangxn/go-polymarket-sdk/utils"
)

// ResolveHandler 结算回调：当 Polymarket gamma API 确认市场已结算时调用。
// outcome: 0=Up(YES 胜出), 1=Down(NO 胜出)。
// 返回 error: 回调失败时保持 pending，下次轮询重试。
type ResolveHandler func(conditionID string, outcome int) error

// PendingResolution 一个等待 Polymarket 结算的市场。
type PendingResolution struct {
	ConditionID  string
	MarketSlug   string
	RegisteredAt time.Time // 注册时刻（老龄化判定用）
}

// maxPendingWait 是待结算上限: 超过该时长仍未结算（争议/无效/异常市场）
// 放弃轮询并移除，避免 pending 无限膨胀 + 每轮串行查询放大延迟。
const maxPendingWait = 24 * time.Hour

// ResolutionPoller 定期轮询 Polymarket gamma API，等待市场结算。
//
// 设计原则：
//   - 缓存待结算事件，由独立 goroutine 统一轮询
//   - 仅当 umaResolutionStatus=="resolved" 且 outcomePrices 胜方 > 0.5 时判定为已结算
//     （归一化容忍 "1"/"1.0"/"0.999…" 浮点序列化）
//   - 超过 maxPendingWait 未结算的市场移除轮询（记日志，诊断行留在记录器）
//   - 替代原有的 fallbackSettlementTimer（BTC 模拟结算）
type ResolutionPoller struct {
	mu      sync.RWMutex
	pending map[string]*PendingResolution // conditionID → 待结算事件

	fetchMarket  func(slug string) (*gjson.Result, error) // gamma API 调用
	pollInterval time.Duration
	onResolved   ResolveHandler // 结算回调（FlipRecorder）
}

// NewResolutionPoller 构造 ResolutionPoller。
//
//	fetchMarket: gamma API 查询函数（通常为 PolymarketClient.FetchMarketBySlug）
//	pollInterval: 轮询间隔（建议 10–15s）
//	onResolved: 市场确认结算后的回调
func NewResolutionPoller(
	fetchMarket func(slug string) (*gjson.Result, error),
	pollInterval time.Duration,
	onResolved ResolveHandler,
) *ResolutionPoller {
	if pollInterval <= 0 {
		pollInterval = 10 * time.Second // time.NewTicker(0) 会 panic，构造处兜底
	}
	return &ResolutionPoller{
		pending:      make(map[string]*PendingResolution),
		fetchMarket:  fetchMarket,
		pollInterval: pollInterval,
		onResolved:   onResolved,
	}
}

// Register 注册一个待结算市场。同一 conditionID 重复注册会被忽略。
func (rp *ResolutionPoller) Register(conditionID, marketSlug string) {
	rp.mu.Lock()
	defer rp.mu.Unlock()

	if _, exists := rp.pending[conditionID]; exists {
		return // 已注册，跳过
	}
	rp.pending[conditionID] = &PendingResolution{
		ConditionID:  conditionID,
		MarketSlug:   marketSlug,
		RegisteredAt: time.Now(),
	}
	log.Printf("[ResolutionPoller] 📋 注册待结算: %s slug=%s 待处理=%d",
		conditionID, marketSlug, len(rp.pending))
}

// Run 启动轮询循环，阻塞直到 ctx 取消。
func (rp *ResolutionPoller) Run(ctx context.Context) {
	ticker := time.NewTicker(rp.pollInterval)
	defer ticker.Stop()

	log.Printf("[ResolutionPoller] 🚀 启动结算轮询（间隔 %v）", rp.pollInterval)

	// 启动后立即执行一次
	rp.pollAll()

	for {
		select {
		case <-ctx.Done():
			log.Printf("[ResolutionPoller] 停止轮询，剩余 %d 个未结算事件", rp.PendingCount())
			return
		case <-ticker.C:
			rp.pollAll()
		}
	}
}

// PendingCount 返回当前待结算市场数量。
func (rp *ResolutionPoller) PendingCount() int {
	rp.mu.RLock()
	defer rp.mu.RUnlock()
	return len(rp.pending)
}

// ── 内部方法 ──

// pollAll 对当前所有待结算事件执行一轮查询。
// defer recover 兜底: 单轮查询/回调 panic 不让轮询 goroutine 退出
// （否则全部待结算信号永久悬挂）。
func (rp *ResolutionPoller) pollAll() {
	defer func() {
		if r := recover(); r != nil {
			log.Printf("[ResolutionPoller] ⚠️ pollAll 崩溃已恢复: %v", r)
		}
	}()

	rp.mu.Lock()
	if len(rp.pending) == 0 {
		rp.mu.Unlock()
		return
	}
	snapshot := make([]*PendingResolution, 0, len(rp.pending))
	for _, p := range rp.pending {
		snapshot = append(snapshot, p)
	}
	rp.mu.Unlock()

	for _, p := range snapshot {
		// 老龄化: 超过上限未结算（争议/无效/异常市场）放弃轮询
		if time.Since(p.RegisteredAt) > maxPendingWait {
			rp.mu.Lock()
			delete(rp.pending, p.ConditionID)
			remaining := len(rp.pending)
			rp.mu.Unlock()
			log.Printf("[ResolutionPoller] ⚠️ 市场 %s 超过 %v 未结算，放弃轮询（剩余 %d，记录保持未结算状态）",
				p.ConditionID, maxPendingWait, remaining)
			continue
		}
		resolved, outcome := rp.checkResolved(p)
		if !resolved {
			continue
		}
		// 先回调再移除: 回调失败（如落盘失败）保持 pending，下次轮询重试
		if rp.onResolved != nil {
			if err := rp.onResolved(p.ConditionID, outcome); err != nil {
				log.Printf("[ResolutionPoller] ⚠️ 结算回调失败 %s: %v（保留 pending 下次重试）",
					p.ConditionID, err)
				continue
			}
		}
		rp.mu.Lock()
		delete(rp.pending, p.ConditionID)
		remaining := len(rp.pending)
		rp.mu.Unlock()

		log.Printf("[ResolutionPoller] ✅ 市场已结算: %s outcome=%d 剩余=%d",
			p.ConditionID, outcome, remaining)
	}
}

// checkResolved 查询 gamma API 判断市场是否已结算。
// 这个只针对Polymarket的结果判定，不同预测市场有可能是不同的需要针对调整
// 返回 (已结算, outcome)。
func (rp *ResolutionPoller) checkResolved(p *PendingResolution) (bool, int) {
	result, err := rp.fetchMarket(p.MarketSlug)
	if err != nil {
		log.Printf("[ResolutionPoller] ⚠️ 查询市场失败 %s: %v（将在下次轮询重试）", p.MarketSlug, err)
		return false, 0
	}

	// 必须 umaResolutionStatus=="resolved" 才认为已结算（UMA 预言机确认）
	umaStatus := result.Get("umaResolutionStatus").String()
	if umaStatus != "resolved" {
		return false, 0
	}

	// 解析 outcomePrices：取值为 "1" 的索引决定结果
	// outcomePrices[0] → YES token, outcomePrices[1] → NO token
	outcomePrices := utils.GetStringArray(result, "outcomePrices")
	if len(outcomePrices) < 2 {
		log.Printf("[ResolutionPoller] ⚠️ 市场 %s umaResolutionStatus=resolved 但 outcomePrices 不足 2 个元素",
			p.ConditionID)
		return false, 0
	}

	// 归一化: 容忍 "1"/"1.0"/"0.999…" 浮点序列化，胜方 = 价格 > 0.5 的一侧；
	// 0.5/0.5 平局（争议/无效）两侧都不 >0.5 → 保持等待，直至超时移除
	price0, err0 := strconv.ParseFloat(outcomePrices[0], 64)
	price1, err1 := strconv.ParseFloat(outcomePrices[1], 64)
	if err0 == nil && price0 > 0.5 && (err1 != nil || price1 < 0.5) {
		return true, 0 // UP (YES) 胜出
	}
	if err1 == nil && price1 > 0.5 && (err0 != nil || price0 < 0.5) {
		return true, 1 // DOWN (NO) 胜出
	}

	// 价格仍为非确定性状态，尚未最终结算，继续等待
	return false, 0
}
