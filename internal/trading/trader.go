package trading

import (
	"context"
	"fmt"
	"log"
	"sync"
	"time"

	sdkModel "github.com/xiangxn/go-polymarket-sdk/model"
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

// Start 启动后台 goroutine（结算事件监听 + TradeMonitor 事件循环），并初始化 recorder。
// tradeMon 为 nil 时仅纸面可用（事件循环不启动）。
func (t *Trader) Start(ctx context.Context, tradeMon *sdk.TradeMonitor) error {
	var err error
	t.recorder, err = NewTradeRecorder(t.cfg.OutputPath)
	if err != nil {
		return fmt.Errorf("创建交易记录器失败: %w", err)
	}

	go t.resolutionWatcher(ctx)
	if tradeMon != nil {
		go t.tradeEventLoop(ctx, tradeMon.SubscribeEvents())
	}
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
	t.exec.PendingOrder = nil
}

// OnSignal 在信号发射点调用，执行风控→GTC 下单→记录。
//
// 返回 ExecInfo 供调用方回填 FlipRecorder（纸面/实盘路径统一）。
// 锁仅在状态读写时持有，SDK 网络调用在锁外执行。
func (t *Trader) OnSignal(sig *flip.FlipSignal, yesTokenID, noTokenID string) (ExecInfo, error) {
	failInfo := ExecInfo{Status: "failed"}

	// ── 阶段 1：锁内预检查 ──
	t.mu.Lock()
	verdict := CheckRisk(t.exec, t.cfg, t.timeNow())
	if !verdict.OK {
		reason := verdict.Reason
		t.exec.LastSkipReason = reason
		t.mu.Unlock()
		return failInfo, fmt.Errorf("风控拒绝: %s", reason)
	}

	tokenID, tokenSide, err := SignalToOrder(sig, yesTokenID, noTokenID)
	if err != nil {
		t.exec.LastSkipReason = err.Error()
		t.mu.Unlock()
		return failInfo, fmt.Errorf("信号映射失败: %w", err)
	}

	maxPrice := CalcMaxPrice(sig.EntryPrice, t.cfg.MaxSlippage)
	if maxPrice <= 0 {
		t.exec.LastSkipReason = "价格上限计算无效"
		t.mu.Unlock()
		return failInfo, fmt.Errorf("价格上限无效: entry=%.4f slippage=%.2f", sig.EntryPrice, t.cfg.MaxSlippage)
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

	// 纸面模式：模拟成交，创建 Position 走统一结算路径
	if !liveOK || client == nil {
		t.mu.Lock()
		filledShares := ComputeShares(cfg.StakePerSignal, sig.EntryPrice)
		rec.State = OrderFilled
		rec.FilledShares = filledShares
		rec.AvgFillPrice = sig.EntryPrice
		rec.UpdatedAt = t.timeNow()
		t.recorder.AppendOrder(rec)

		// 创建纸面 Position（与实盘路径一致）
		pos := &Position{
			ConditionID: sig.ConditionID,
			TokenID:     tokenID,
			TokenSide:   tokenSide,
			Shares:      filledShares,
			AvgPrice:    sig.EntryPrice, // 纸面：入场价即成交价
			CostUSDC:    filledShares * sig.EntryPrice,
			OrderID:     "paper",
			OpenedAt:    t.timeNow(),
		}
		t.exec.Position = pos
		t.exec.DailySignals++
		t.exec.LastSkipReason = ""
		t.mu.Unlock()
		log.Printf("[Trading] 📝 纸面信号: side=%s entry=%.4f shares=%.0f cost=%.2f",
			tokenSide, sig.EntryPrice, filledShares, pos.CostUSDC)
		return ExecInfo{Status: "filled", FilledShares: filledShares, AvgFillPrice: sig.EntryPrice}, nil
	}

	// 实盘：GTC 限价单
	size := ComputeShares(cfg.StakePerSignal, maxPrice)
	uo := orders.UserOrder{
		TokenID: tokenID,
		Price:   maxPrice,
		Size:    size,
		Side:    orders.BUY,
	}
	signedOrder, err := client.CreateOrder(&uo, orders.CreateOrderOptions{})
	if err != nil {
		t.mu.Lock()
		rec.State = OrderFailed
		rec.ErrorMsg = fmt.Sprintf("CreateOrder: %v", err)
		rec.UpdatedAt = t.timeNow()
		t.recorder.AppendOrder(rec)
		t.exec.LastSkipReason = rec.ErrorMsg
		t.mu.Unlock()
		return failInfo, fmt.Errorf("创建订单失败: %w", err)
	}

	// PostOrder（GTC）
	resp, err := client.PostOrder(signedOrder, orders.GTC, false)
	if err != nil {
		t.mu.Lock()
		rec.State = OrderFailed
		rec.ErrorMsg = fmt.Sprintf("PostOrder: %v", err)
		rec.UpdatedAt = t.timeNow()
		t.recorder.AppendOrder(rec)
		t.exec.LastSkipReason = rec.ErrorMsg
		t.mu.Unlock()
		return failInfo, fmt.Errorf("提交订单失败: %w, Price: %.4f, Size: %.4f", err, uo.Price, uo.Size)
	}

	orderID, success, errMsg := ParsePostOrderResp(resp)
	if !success || orderID == "" {
		// PostOrder 返回失败
		t.mu.Lock()
		rec.State = OrderFailed
		rec.ErrorMsg = errMsg
		rec.UpdatedAt = t.timeNow()
		t.recorder.AppendOrder(rec)
		t.exec.LastSkipReason = rec.ErrorMsg
		t.exec.DailySignals++
		t.mu.Unlock()
		return ExecInfo{Status: "failed"}, nil
	}

	// ── 阶段 3：锁内状态更新 — GTC 订单已提交，TradeMonitor 事件驱动成交追踪 ──
	t.mu.Lock()
	defer t.mu.Unlock()

	rec.ID = orderID
	rec.State = OrderSubmitted
	rec.UpdatedAt = t.timeNow()
	t.recorder.AppendOrder(rec)

	// 创建 pendingGtcOrder，由 TradeMonitor 事件循环更新成交数据
	t.exec.PendingOrder = &pendingGtcOrder{
		OrderID:     orderID,
		TokenID:     tokenID,
		TokenSide:   tokenSide,
		ConditionID: sig.ConditionID,
		MaxPrice:    maxPrice,
		StakeUSDC:   cfg.StakePerSignal,
		Rec:         rec,
	}
	t.exec.LastSkipReason = ""
	t.exec.DailySignals++

	log.Printf("[Trading] 📝 GTC 订单已挂单: side=%s price=%.4f size=%.0f orderID=%s",
		tokenSide, maxPrice, size, orderID)
	return ExecInfo{Status: "pending"}, nil
}

// OnCycleEnd 市场结束时调用。
//
//   - 对账 GTC 挂单：TradeMonitor 已实时追踪成交，此处仅做最终处理
//   - 有成交且无持仓 → 创建 Position；有成交已有持仓（processTrade 已建）→ 仅清理 PendingOrder
//   - 无成交 → 取消挂单
//   - 兜底：若 PendingOrder 存在但 TradeMonitor 未收到事件，用 GetOpenOrders 查询
//   - 返回 ExecInfo 供调用方在 Resolve 前回填 FlipRecorder
//   - 非阻塞：启动后台 goroutine 等待 WS 结算，超时后回退到模拟 outcome
func (t *Trader) OnCycleEnd(conditionID string, simulatedOutcome int) ExecInfo {
	var result ExecInfo

	t.mu.RLock()
	pending := t.exec.PendingOrder
	client := t.client
	t.mu.RUnlock()

	// 对账未成交的 GTC 订单
	if pending != nil && client != nil {
		filledShares := pending.FilledShares
		totalCost := pending.TotalCost

		// 兜底：若 TradeMonitor 未收到任何事件，用 GetOpenOrders 查询
		if filledShares == 0 && pending.TradeCount == 0 && pending.Status == "" {
			openOrders, oErr := client.GetOpenOrders(&orders.OpenOrderParams{Id: &pending.OrderID}, true, nil)
			if oErr != nil {
				log.Printf("[Trading] ⚠️ 查询 GTC 订单状态失败: %v", oErr)
			}
			if len(openOrders) > 0 {
				oo := openOrders[0]
				filledShares = oo.SizeMatched
				if oo.SizeMatched > 0 {
					totalCost = t.cfg.StakePerSignal // 估算：无逐笔数据，假设全额花费
				}
			}
		}

		// 需取消：完全未成交（无 TradeMonitor 事件且 GetOpenOrders 也无成交）
		needCancel := filledShares == 0

		// 先做网络调用（取消订单），锁外执行
		if needCancel {
			_, cErr := client.CancelOrder(&orders.OrderPayload{OrderID: pending.OrderID})
			if cErr != nil {
				log.Printf("[Trading] ⚠️ 取消 GTC 订单失败: %v (orderID=%s)", cErr, pending.OrderID)
			} else {
				log.Printf("[Trading] 🧹 已取消未成交 GTC 订单: %s", pending.OrderID)
			}
		}

		// ── 锁内状态更新 ──
		t.mu.Lock()

		// 重新读取持仓（processTrade 可能已在事件循环中创建了 Position）
		pos := t.exec.Position

		if filledShares > 0 {
			// 计算均价（优先用 TradeMonitor 累积的真实成交价）
			avgFillPrice := totalCost / filledShares
			if avgFillPrice <= 0 {
				avgFillPrice = t.cfg.StakePerSignal / filledShares // fallback 估算
			}

			if pos == nil {
				// 创建持仓（正常路径：TradeMonitor 未建仓或未收到事件）
				pending.Rec.State = OrderFilled
				pending.Rec.FilledShares = filledShares
				pending.Rec.AvgFillPrice = avgFillPrice
				pending.Rec.UpdatedAt = t.timeNow()
				t.recorder.AppendOrder(pending.Rec)

				newPos := &Position{
					ConditionID: pending.ConditionID,
					TokenID:     pending.TokenID,
					TokenSide:   pending.TokenSide,
					Shares:      filledShares,
					AvgPrice:    avgFillPrice,
					CostUSDC:    filledShares * avgFillPrice,
					OrderID:     pending.OrderID,
					OpenedAt:    t.timeNow(),
				}
				t.exec.Position = newPos
				log.Printf("[Trading] 🎯 GTC 周期末建仓: side=%s shares=%.1f avgPrice=%.4f trades=%d orderID=%s",
					pending.TokenSide, filledShares, avgFillPrice, pending.TradeCount, pending.OrderID)
			} else {
				// Position 已由 processTrade 创建，同步更新以对齐兜底数据
				if t.exec.Position != nil {
					t.exec.Position.Shares = filledShares
					t.exec.Position.AvgPrice = avgFillPrice
					t.exec.Position.CostUSDC = filledShares * avgFillPrice
				}
				pending.Rec.State = OrderFilled
				pending.Rec.FilledShares = filledShares
				pending.Rec.AvgFillPrice = avgFillPrice
				pending.Rec.UpdatedAt = t.timeNow()
				t.recorder.AppendOrder(pending.Rec)
				log.Printf("[Trading] ✅ GTC 周期末确认: side=%s shares=%.1f avgPrice=%.4f（TradeMonitor 已建仓）",
					pending.TokenSide, filledShares, avgFillPrice)
			}
			t.exec.LastSkipReason = ""
			result = ExecInfo{Status: "filled", FilledShares: filledShares, AvgFillPrice: avgFillPrice}
		} else {
			// 未成交：仅清理 PendingOrder
			t.exec.LastSkipReason = fmt.Sprintf("GTC 订单窗口内未成交，已取消: %s", pending.OrderID)
			result = ExecInfo{Status: "failed"}
		}

		t.exec.PendingOrder = nil
		t.mu.Unlock()
	}

	// 重新读取持仓（GTC 对账可能已创建新持仓）
	t.mu.RLock()
	pos := t.exec.Position
	t.mu.RUnlock()

	if pos == nil || pos.ConditionID != conditionID {
		return result
	}

	// 启动后台兜底计时器（非阻塞）
	timeout := time.Duration(t.cfg.ResolutionTimeoutSec) * time.Second
	go t.fallbackSettlementTimer(pos, simulatedOutcome, timeout)
	return result
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

// ── TradeMonitor 事件循环 ──

// tradeEventLoop 监听 TradeMonitor 的实时成交/订单事件，驱动 GTC 订单的成交追踪。
func (t *Trader) tradeEventLoop(ctx context.Context, eventCh <-chan sdk.TradeEvent) {
	for {
		select {
		case <-ctx.Done():
			return
		case ev, ok := <-eventCh:
			if !ok {
				return
			}
			switch ev.EventType {
			case sdk.TradeEventTypeTrade:
				if ev.Trade != nil {
					t.processTrade(ev.Trade)
				}
			case sdk.TradeEventTypeOrder:
				if ev.Order != nil {
					t.processOrder(ev.Order)
				}
			}
		}
	}
}

// processTrade 处理逐笔成交事件，累积成交股数和花费（真实成交价）。
func (t *Trader) processTrade(trade *sdkModel.WSTrade) {
	t.mu.Lock()
	defer t.mu.Unlock()

	pending := t.exec.PendingOrder
	if pending == nil {
		return
	}
	// 匹配 taker 角色：TakerOrderId 等于我们的挂单 ID
	if trade.TakerOrderId != pending.OrderID {
		return
	}

	// 累积成交数据
	pending.FilledShares += trade.Size
	pending.TotalCost += trade.Size * trade.Price
	pending.TradeCount++

	avgPrice := pending.TotalCost / pending.FilledShares

	// 更新订单记录
	pending.Rec.FilledShares = pending.FilledShares
	pending.Rec.AvgFillPrice = avgPrice
	pending.Rec.UpdatedAt = t.timeNow()

	// 若尚无持仓 → 创建持仓
	if t.exec.Position == nil {
		pos := &Position{
			ConditionID: pending.ConditionID,
			TokenID:     pending.TokenID,
			TokenSide:   pending.TokenSide,
			Shares:      pending.FilledShares,
			AvgPrice:    avgPrice,
			CostUSDC:    pending.TotalCost,
			OrderID:     pending.OrderID,
			OpenedAt:    t.timeNow(),
		}
		t.exec.Position = pos
		log.Printf("[Trading] 🎯 GTC 逐笔成交: side=%s shares=%.1f price=%.4f cost=%.4f total=%.1f orderID=%s",
			pending.TokenSide, trade.Size, trade.Price, trade.Size*trade.Price, pending.FilledShares, pending.OrderID)
	} else {
		// 更新已有持仓（渐进式部分成交）
		t.exec.Position.Shares = pending.FilledShares
		t.exec.Position.AvgPrice = avgPrice
		t.exec.Position.CostUSDC = pending.TotalCost
	}
}

// processOrder 处理订单状态变更事件（LIVE → MATCHED / CANCELED）。
func (t *Trader) processOrder(order *sdkModel.WSOrder) {
	t.mu.Lock()
	defer t.mu.Unlock()

	pending := t.exec.PendingOrder
	if pending == nil || order.Id != pending.OrderID {
		return
	}

	pending.Status = order.Status

	switch order.Status {
	case "MATCHED":
		pending.Rec.State = OrderFilled
		pending.Rec.FilledShares = order.SizeMatched
		pending.Rec.UpdatedAt = t.timeNow()
		t.recorder.AppendOrder(pending.Rec)
		log.Printf("[Trading] ✅ GTC 订单已完全成交: orderID=%s matched=%.1f", order.Id, order.SizeMatched)
	case "CANCELED":
		pending.Rec.State = OrderFailed
		pending.Rec.UpdatedAt = t.timeNow()
		t.recorder.AppendOrder(pending.Rec)
		t.exec.PendingOrder = nil
		log.Printf("[Trading] ❌ GTC 订单已被取消: orderID=%s", order.Id)
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
