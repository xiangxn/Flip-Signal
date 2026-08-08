package trading

import (
	"context"
	"fmt"
	"log"
	"sync"
	"time"

	"github.com/xiangxn/go-polymarket-sdk/orders"
	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"

	"github.com/necklace/flip-signal/internal/flip"
)

// Trader 是实盘交易执行器。
//
// 并发模型：mu (sync.RWMutex) 保护所有状态字段。
// 写路径：主市场循环（OnSignal/NewCycle/OnCycleEnd）、后台结算 goroutine、
// Enable/Disable/ClosePosition。
type Trader struct {
	mu           sync.RWMutex
	cfg          TradingConfig
	client       TradeClient
	resolved     <-chan *sdk.ResolvedInfo // WS 结算事件源
	now          func() time.Time         // 可注入时钟（测试用，nil 则用 time.Now）
	lastDayCheck time.Time                // 上次日切检查时间

	exec      ExecutionState
	orders    []*OrderRecord
	positions []Position
	recorder  *TradeRecorder
	liveOK    bool // Enable() 调用后为 true
}

// NewTrader 构造 Trader。client 为 nil 时仅纸面可用。
func NewTrader(cfg TradingConfig, client TradeClient, resolved <-chan *sdk.ResolvedInfo) *Trader {
	return &Trader{
		cfg:          cfg,
		client:       client,
		resolved:     resolved,
		now:          nil,
		lastDayCheck: time.Now(),
		exec: ExecutionState{
			Enabled: cfg.Enabled,
		},
	}
}

// timeNow 返回当前时间（支持注入时钟）。
func (t *Trader) timeNow() time.Time {
	if t.now != nil {
		return t.now()
	}
	return time.Now()
}

// ── 生命周期 ──

// Start 启动后台 goroutine（结算事件监听），并初始化 recorder。
func (t *Trader) Start(ctx context.Context) error {
	var err error
	t.recorder, err = NewTradeRecorder(t.cfg.OutputPath)
	if err != nil {
		return fmt.Errorf("创建交易记录器失败: %w", err)
	}

	go t.resolutionWatcher(ctx)
	return nil
}

// Close 关闭 Trader，刷新并关闭交易记录器。
func (t *Trader) Close() {
	if t.recorder != nil {
		t.recorder.Close()
	}
}

// ── 运行时开关 ──

// Enable 启用实盘下单。client 为 nil 时返回错误。
func (t *Trader) Enable() error {
	t.mu.Lock()
	defer t.mu.Unlock()

	if t.client == nil {
		return fmt.Errorf("无 CLOB 客户端（未配置凭证），无法启用实盘")
	}
	t.exec.Enabled = true
	t.cfg.Enabled = true
	t.liveOK = true
	log.Printf("[Trading] 🔒 实盘已启用")
	return nil
}

// Disable 禁用实盘下单。
func (t *Trader) Disable() {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.exec.Enabled = false
	t.cfg.Enabled = false
	log.Printf("[Trading] 🔓 实盘已禁用")
}

// Enabled 返回当前是否启用。
func (t *Trader) Enabled() bool {
	t.mu.RLock()
	defer t.mu.RUnlock()
	return t.exec.Enabled
}

// ── 市场周期钩子 ──

// NewCycle 在新市场周期开始时调用。
//
//   - 检查并执行日切重置（DailyPnl/DailySignals/DailyLimitHit）
//   - 处理上一周期遗留的未结算持仓（fallback 结算）
//   - 记录当前 conditionID
func (t *Trader) NewCycle(conditionID string, _ /*yesTokenID*/, _ /*noTokenID*/ string) {
	t.mu.Lock()
	defer t.mu.Unlock()

	// ── 日切检查 ──
	now := t.timeNow()
	if IsNewDay(t.lastDayCheck, now) {
		t.exec.DailyPnl = 0
		t.exec.DailySignals = 0
		t.exec.DailyLimitHit = false
		t.lastDayCheck = now
		log.Printf("[Trading] 📅 日切重置: dailyPnl=0 dailySignals=0")
	}

	t.exec.CurrentCondition = conditionID
}

// OnSignal 在信号发射点调用，执行风控→FAK 下单→记录。
//
// 锁仅在状态读写时持有，SDK 网络调用在锁外执行。
func (t *Trader) OnSignal(sig *flip.FlipSignal, yesTokenID, noTokenID string) error {
	// ── 阶段 1：锁内预检查 ──
	t.mu.Lock()
	verdict := CheckRisk(t.exec, t.cfg, t.timeNow())
	if !verdict.OK {
		reason := verdict.Reason
		t.exec.LastSkipReason = reason
		t.mu.Unlock()
		return fmt.Errorf("风控拒绝: %s", reason)
	}

	tokenID, tokenSide, err := SignalToOrder(sig, yesTokenID, noTokenID)
	if err != nil {
		t.exec.LastSkipReason = err.Error()
		t.mu.Unlock()
		return fmt.Errorf("信号映射失败: %w", err)
	}

	maxPrice := CalcMaxPrice(sig.EntryPrice, t.cfg.MaxSlippage)
	if maxPrice <= 0 {
		t.exec.LastSkipReason = "价格上限计算无效"
		t.mu.Unlock()
		return fmt.Errorf("价格上限无效: entry=%.4f slippage=%.2f", sig.EntryPrice, t.cfg.MaxSlippage)
	}

	rec := &OrderRecord{
		ConditionID:  sig.ConditionID,
		TokenID:      tokenID,
		Side:         string(orders.BUY),
		TokenSide:    tokenSide,
		Price:        maxPrice,
		RequestedAmt: t.cfg.StakePerSignal,
		State:        OrderPending,
		CreatedAt:    t.timeNow(),
	}
	t.orders = append(t.orders, rec)

	liveOK := t.liveOK
	client := t.client
	cfg := t.cfg
	t.mu.Unlock()

	// ── 阶段 2：锁外 SDK 调用 ──

	// 纸面模式：仅记录，不提交
	if !liveOK || client == nil {
		t.mu.Lock()
		rec.State = OrderFilled
		rec.FilledShares = ComputeShares(cfg.StakePerSignal, sig.EntryPrice)
		rec.AvgFillPrice = sig.EntryPrice
		rec.UpdatedAt = t.timeNow()
		t.recorder.AppendOrder(rec)
		t.exec.DailySignals++
		t.exec.LastSkipReason = ""
		t.mu.Unlock()
		log.Printf("[Trading] 📝 纸面信号记录: side=%s entry=%.4f shares=%.0f maxPrice=%.4f",
			tokenSide, sig.EntryPrice, rec.FilledShares, maxPrice)
		return nil
	}

	// 实盘：CreateMarketOrder
	umo := orders.UserMarketOrder{
		TokenID:   tokenID,
		Price:     &maxPrice,
		Amount:    cfg.StakePerSignal,
		Side:      orders.BUY,
		OrderType: orders.MARKET_FAK,
	}
	signedOrder, err := client.CreateMarketOrder(&umo, orders.CreateOrderOptions{})
	if err != nil {
		t.mu.Lock()
		rec.State = OrderFailed
		rec.ErrorMsg = fmt.Sprintf("CreateMarketOrder: %v", err)
		rec.UpdatedAt = t.timeNow()
		t.recorder.AppendOrder(rec)
		t.exec.LastSkipReason = rec.ErrorMsg
		t.mu.Unlock()
		return fmt.Errorf("创建订单失败: %w", err)
	}

	// PostOrder
	resp, err := client.PostOrder(signedOrder, orders.FAK, false)
	if err != nil {
		t.mu.Lock()
		rec.State = OrderFailed
		rec.ErrorMsg = fmt.Sprintf("PostOrder: %v", err)
		rec.UpdatedAt = t.timeNow()
		t.recorder.AppendOrder(rec)
		t.exec.LastSkipReason = rec.ErrorMsg
		t.mu.Unlock()
		return fmt.Errorf("提交订单失败: %w, Price: %.4f, Amount: %.4f", err, *umo.Price, umo.Amount)
	}

	orderID, success, errMsg := ParsePostOrderResp(resp)

	// GetOpenOrders 查询成交
	var filledShares, avgFillPrice float64
	var orderState OrderState
	if success && orderID != "" {
		orderState = OrderSubmitted
		openOrders, oErr := client.GetOpenOrders(&orders.OpenOrderParams{Id: &orderID}, true, nil)
		if oErr != nil {
			log.Printf("[Trading] ⚠️ 查询订单状态失败: %v", oErr)
		}
		if len(openOrders) > 0 {
			oo := openOrders[0]
			filledShares = oo.SizeMatched

			// 判断是否完全成交：使用 entryPrice（信号触发价）计算期望股数，
			// 比 maxPrice（价格上限）更准确，避免阈值偏低导致过早判定为已成交。
			expectedShares := ComputeShares(cfg.StakePerSignal, sig.EntryPrice)
			fullyFilled := oo.Status == "MATCHED" || (expectedShares > 0 && oo.SizeMatched >= expectedShares)

			if oo.SizeMatched > 0 {
				if fullyFilled {
					// 完全成交：均价 = 总投入 / 总股数
					avgFillPrice = cfg.StakePerSignal / oo.SizeMatched
				} else if oo.OriginalSize > 0 {
					// 部分成交：按 OriginalSize（takerAmount）与实际成交比例估算实际花费
					fillRatio := oo.SizeMatched / oo.OriginalSize
					if fillRatio > 1.0 {
						fillRatio = 1.0
					}
					actualCost := cfg.StakePerSignal * fillRatio
					avgFillPrice = actualCost / oo.SizeMatched
				}
			}

			if fullyFilled {
				orderState = OrderFilled
			}
		}
	} else {
		orderState = OrderFailed
	}

	// ── 阶段 3：锁内状态更新 ──
	t.mu.Lock()
	defer t.mu.Unlock()

	rec.ID = orderID
	rec.State = orderState
	rec.FilledShares = filledShares
	rec.AvgFillPrice = avgFillPrice
	rec.UpdatedAt = t.timeNow()
	if orderState == OrderFailed {
		rec.ErrorMsg = errMsg
	}
	t.recorder.AppendOrder(rec)

	if orderState == OrderFilled && filledShares > 0 {
		pos := &Position{
			ConditionID: sig.ConditionID,
			TokenID:     tokenID,
			TokenSide:   tokenSide,
			Shares:      filledShares,
			AvgPrice:    avgFillPrice,
			CostUSDC:    filledShares * avgFillPrice,
			OrderID:     orderID,
			OpenedAt:    t.timeNow(),
		}
		t.exec.Position = pos
		t.exec.LastSkipReason = ""
		log.Printf("[Trading] 🎯 实盘成交: side=%s shares=%.1f avgPrice=%.4f cost=%.2f orderID=%s",
			tokenSide, pos.Shares, pos.AvgPrice, pos.CostUSDC, orderID)
	} else {
		t.exec.LastSkipReason = fmt.Sprintf("FAK 未完全成交: state=%v filled=%.1f", orderState, filledShares)
		log.Printf("[Trading] ⚠️ %s", t.exec.LastSkipReason)
	}

	t.exec.DailySignals++
	return nil
}

// OnCycleEnd 市场结束时调用。
//
// 非阻塞：启动后台 goroutine 等待 WS 结算，超时后回退到模拟 outcome。
// resolutionWatcher 在 WS 事件到达时即时结算，此处仅作兜底。
func (t *Trader) OnCycleEnd(conditionID string, simulatedOutcome int) {
	t.mu.RLock()
	pos := t.exec.Position
	t.mu.RUnlock()

	if pos == nil || pos.ConditionID != conditionID {
		return
	}

	// 启动后台兜底计时器（非阻塞）
	timeout := time.Duration(t.cfg.ResolutionTimeoutSec) * time.Second
	go t.fallbackSettlementTimer(pos, simulatedOutcome, timeout)
}

// fallbackSettlementTimer 在超时后检查持仓是否仍未结算，若是则回退到模拟结算。
func (t *Trader) fallbackSettlementTimer(pos *Position, simulatedOutcome int, timeout time.Duration) {
	time.Sleep(timeout)

	// 超时：检查是否已被 resolutionWatcher 结算
	t.mu.RLock()
	currentPos := t.exec.Position
	t.mu.RUnlock()

	if currentPos == nil || currentPos.ConditionID != pos.ConditionID {
		return // 已被 WS 结算
	}

	won := (pos.TokenSide == "yes" && simulatedOutcome == 0) ||
		(pos.TokenSide == "no" && simulatedOutcome == 1)
	log.Printf("[Trading] ⚠️ 结算超时 %v，使用模拟 outcome", timeout)
	t.settleWithOutcome(pos, won, "simulated_fallback")
}

// settleWithOutcome 按指定结果结算持仓（调用方自行加锁）。
func (t *Trader) settleWithOutcome(pos *Position, won bool, source string) {
	t.mu.Lock()
	defer t.mu.Unlock()

	// 防御：已经被结算过了
	if t.exec.Position == nil || t.exec.Position.ConditionID != pos.ConditionID {
		return
	}

	pos.Won = won
	if pos.TokenSide == "no" {
		pos.Outcome = 1 // 赌 DOWN
	} else {
		pos.Outcome = 0 // 赌 UP
	}
	pos.PnL = CalcPnL(pos.Shares, pos.AvgPrice, won)
	pos.ResolutionSource = source
	pos.SettledAt = t.timeNow()

	t.exec.DailyPnl += pos.PnL
	t.exec.Position = nil

	// 亏损冷却
	if pos.PnL < 0 {
		t.exec.CooldownUntil = t.timeNow().Add(time.Duration(t.cfg.CooldownAfterLossSec) * time.Second)
	}

	// 日亏检查
	if t.exec.DailyPnl <= -t.cfg.MaxDailyLoss {
		t.exec.DailyLimitHit = true
	}

	t.positions = append(t.positions, *pos)
	t.recorder.AppendPosition(pos)

	log.Printf("[Trading] 💰 结算: side=%s won=%v pnl=%.4f source=%s dailyPnl=%.2f",
		pos.TokenSide, won, pos.PnL, source, t.exec.DailyPnl)
}

// ── 手动平仓（预留）──

// ClosePosition 以市价卖出当前持仓平仓。
func (t *Trader) ClosePosition() (*Position, error) {
	t.mu.Lock()
	defer t.mu.Unlock()

	if t.exec.Position == nil {
		return nil, fmt.Errorf("无持仓可平")
	}
	return nil, fmt.Errorf("手动平仓尚未实现")
}

// ── 后台 goroutine ──

// resolutionWatcher 监听 WS 结算事件，即时结算匹配的持仓。
func (t *Trader) resolutionWatcher(ctx context.Context) {
	for {
		select {
		case <-ctx.Done():
			return
		case info, ok := <-t.resolved:
			if !ok {
				return
			}
			t.mu.RLock()
			pos := t.exec.Position
			t.mu.RUnlock()
			if pos != nil && info.Market == pos.ConditionID {
				won := info.WinningAssetId == pos.TokenID
				t.settleWithOutcome(pos, won, "ws")
			}
		}
	}
}

// ── Dashboard 访问器（只读）──

// Status 返回执行状态快照。
func (t *Trader) Status() ExecutionState {
	t.mu.RLock()
	defer t.mu.RUnlock()
	return t.exec
}

// Positions 返回已结算持仓列表。
func (t *Trader) Positions() []Position {
	t.mu.RLock()
	defer t.mu.RUnlock()
	out := make([]Position, len(t.positions))
	copy(out, t.positions)
	return out
}

// Orders 返回最近的订单记录。
func (t *Trader) Orders(limit int) []OrderRecord {
	t.mu.RLock()
	defer t.mu.RUnlock()
	n := len(t.orders)
	if limit > n {
		limit = n
	}
	start := n - limit
	if start < 0 {
		start = 0
	}
	out := make([]OrderRecord, limit)
	for i := 0; i < limit; i++ {
		out[i] = *t.orders[start+i]
	}
	return out
}
