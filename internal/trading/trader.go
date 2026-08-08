package trading

import (
	"context"
	"fmt"
	"log"
	"sync"
	"time"

	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"
	"github.com/xiangxn/go-polymarket-sdk/orders"

	"github.com/necklace/flip-signal/internal/flip"
)

// Trader 是实盘交易执行器。
//
// 并发模型：mu (sync.RWMutex) 保护所有状态字段。
// 写路径：主市场循环（OnSignal/NewCycle/OnCycleEnd）、后台结算 goroutine、
// Enable/Disable/ClosePosition（无 Dashboard handler，但预留）。
type Trader struct {
	mu       sync.RWMutex
	cfg      TradingConfig
	client   TradeClient
	resolved <-chan *sdk.ResolvedInfo // WS 结算事件源
	now      func() time.Time         // 可注入时钟（测试用，nil 则用 time.Now）

	exec       ExecutionState
	orders     []*OrderRecord
	positions  []Position
	recorder   *TradeRecorder
	liveOK     bool // 是否具备 CLOB 凭证（Enable 前置条件）
	done       chan struct{}
}

// NewTrader 构造 Trader。client 为 nil 时仅纸面可用。
func NewTrader(cfg TradingConfig, client TradeClient, resolved <-chan *sdk.ResolvedInfo) *Trader {
	return &Trader{
		cfg:      cfg,
		client:   client,
		resolved: resolved,
		now:      nil, // 用 time.Now
		exec: ExecutionState{
			Enabled: cfg.Enabled,
		},
		done: make(chan struct{}),
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

	// 后台：结算事件监听
	go t.resolutionWatcher(ctx)

	return nil
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

// NewCycle 在新市场周期开始时调用，记录当前 condition/token。
func (t *Trader) NewCycle(conditionID, yesTokenID, noTokenID string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.exec.CurrentCondition = conditionID
	// 存储 token ID 用于 OnSignal 查找
	// （通过 exec 之外的方式传递——在 OnSignal 参数中直接传入）
	_ = yesTokenID
	_ = noTokenID
}

// OnSignal 在信号发射点调用，执行风控→FAK 下单→记录。
//
// yesTokenID / noTokenID 由调用方（main.go）传入，因为 Trader 不持有 orderbook adapter。
func (t *Trader) OnSignal(sig *flip.FlipSignal, yesTokenID, noTokenID string) error {
	t.mu.Lock()
	defer t.mu.Unlock()

	// ── 风控闸门 ──
	now := t.timeNow()
	verdict := CheckRisk(t.exec, t.cfg, now)
	if !verdict.OK {
		t.exec.LastSkipReason = verdict.Reason
		return fmt.Errorf("风控拒绝: %s", verdict.Reason)
	}

	// ── 信号→订单映射 ──
	tokenID, tokenSide, err := SignalToOrder(sig, yesTokenID, noTokenID)
	if err != nil {
		t.exec.LastSkipReason = err.Error()
		return fmt.Errorf("信号映射失败: %w", err)
	}

	// ── 计算价格上限 ──
	maxPrice := CalcMaxPrice(sig.EntryPrice, t.cfg.MaxSlippage)
	if maxPrice <= 0 {
		t.exec.LastSkipReason = "价格上限计算无效"
		return fmt.Errorf("价格上限无效: entry=%.4f slippage=%.2f", sig.EntryPrice, t.cfg.MaxSlippage)
	}

	// ── 记录订单 ──
	rec := &OrderRecord{
		ConditionID:  sig.ConditionID,
		TokenID:      tokenID,
		Side:         string(orders.BUY),
		TokenSide:    tokenSide,
		Price:        maxPrice,
		RequestedAmt: t.cfg.StakePerSignal,
		State:        OrderPending,
		CreatedAt:    now,
	}
	t.orders = append(t.orders, rec)

	// ── 纸面模式：记录但不提交 ──
	if !t.liveOK || t.client == nil {
		rec.State = OrderFilled
		rec.FilledShares = ComputeShares(t.cfg.StakePerSignal, sig.EntryPrice)
		rec.AvgFillPrice = sig.EntryPrice
		rec.UpdatedAt = t.timeNow()
		t.recorder.AppendOrder(rec)
		t.exec.DailySignals++
		t.exec.LastSkipReason = ""
		log.Printf("[Trading] 📝 纸面信号记录: side=%s entry=%.4f shares=%.0f maxPrice=%.4f",
			tokenSide, sig.EntryPrice, rec.FilledShares, maxPrice)
		return nil
	}

	// ── 实盘：创建 FAK 市价单 ──
	signedOrder, err := t.client.CreateMarketOrder(&orders.UserMarketOrder{
		TokenID:   tokenID,
		Price:     &maxPrice,
		Amount:    t.cfg.StakePerSignal,
		Side:      orders.BUY,
		OrderType: orders.MARKET_FAK,
	}, orders.CreateOrderOptions{})
	if err != nil {
		rec.State = OrderFailed
		rec.ErrorMsg = fmt.Sprintf("CreateMarketOrder: %v", err)
		rec.UpdatedAt = t.timeNow()
		t.recorder.AppendOrder(rec)
		t.exec.LastSkipReason = rec.ErrorMsg
		return fmt.Errorf("创建订单失败: %w", err)
	}

	// ── 提交 FAK ──
	resp, err := t.client.PostOrder(signedOrder, orders.FAK, false)
	if err != nil {
		rec.State = OrderFailed
		rec.ErrorMsg = fmt.Sprintf("PostOrder: %v", err)
		rec.UpdatedAt = t.timeNow()
		t.recorder.AppendOrder(rec)
		t.exec.LastSkipReason = rec.ErrorMsg
		return fmt.Errorf("提交订单失败: %w", err)
	}

	orderID, success, errMsg := ParsePostOrderResp(resp)
	rec.ID = orderID
	rec.UpdatedAt = t.timeNow()

	if !success || orderID == "" {
		rec.State = OrderFailed
		rec.ErrorMsg = errMsg
		t.recorder.AppendOrder(rec)
		t.exec.LastSkipReason = fmt.Sprintf("PostOrder 失败: %s", errMsg)
		return fmt.Errorf("PostOrder 返回失败: %s", errMsg)
	}

	// ── 提交成功 → 查询成交状态 ──
	rec.State = OrderSubmitted

	openOrders, err := t.client.GetOpenOrders(&orders.OpenOrderParams{Id: &orderID}, true, nil)
	if err != nil {
		log.Printf("[Trading] ⚠️ 查询订单状态失败: %v", err)
	}
	if len(openOrders) > 0 {
		oo := openOrders[0]
		rec.FilledShares = oo.SizeMatched
		if oo.SizeMatched > 0 && rec.FilledShares > 0 {
			// 用请求金额和实际成交股数估算成交均价
			rec.AvgFillPrice = t.cfg.StakePerSignal / oo.SizeMatched
		}
		if oo.Status == "MATCHED" || oo.SizeMatched >= ComputeShares(t.cfg.StakePerSignal, maxPrice) {
			rec.State = OrderFilled
		}
	}

	t.recorder.AppendOrder(rec)

	// ── 建仓 ──
	if rec.State == OrderFilled && rec.FilledShares > 0 {
		pos := &Position{
			ConditionID: sig.ConditionID,
			TokenID:     tokenID,
			TokenSide:   tokenSide,
			Shares:      rec.FilledShares,
			AvgPrice:    rec.AvgFillPrice,
			CostUSDC:    rec.FilledShares * rec.AvgFillPrice,
			OrderID:     orderID,
			OpenedAt:    t.timeNow(),
		}
		t.exec.Position = pos
		log.Printf("[Trading] 🎯 实盘成交: side=%s shares=%.1f avgPrice=%.4f cost=%.2f orderID=%s",
			tokenSide, pos.Shares, pos.AvgPrice, pos.CostUSDC, orderID)
	} else {
		log.Printf("[Trading] ⚠️ FAK 未完全成交: orderID=%s state=%v filled=%.1f",
			orderID, rec.State, rec.FilledShares)
	}

	t.exec.DailySignals++
	t.exec.LastSkipReason = ""
	return nil
}

// OnCycleEnd 市场结束时调用：等待结算。
//
// resolutionWatcher goroutine 在后台监听 WS 结算事件并自动结算匹配的持仓。
// 此方法等待 ResolutionTimeoutSec 后检查是否已结算，未结算则回退到模拟 outcome。
//
// simulatedOutcome 来自 BTC 价格（0=Up 1=Down）。
func (t *Trader) OnCycleEnd(conditionID string, simulatedOutcome int) {
	// 等待 WS 结算或超时
	timeout := time.Duration(t.cfg.ResolutionTimeoutSec) * time.Second
	deadline := time.After(timeout)

	// 轮询检查是否已结算（resolutionWatcher 会处理）
	ticker := time.NewTicker(1 * time.Second)
	defer ticker.Stop()

	for {
		t.mu.RLock()
		pos := t.exec.Position
		t.mu.RUnlock()

		// 已无持仓 → 已结算
		if pos == nil || pos.ConditionID != conditionID {
			return
		}

		select {
		case <-deadline:
			// 超时：强制模拟结算
			t.mu.RLock()
			pos := t.exec.Position
			t.mu.RUnlock()

			if pos == nil || pos.ConditionID != conditionID {
				return
			}

			log.Printf("[Trading] ⚠️ 结算超时 %v，使用模拟 outcome", timeout)
			won := (pos.TokenSide == "yes" && simulatedOutcome == 0) ||
				(pos.TokenSide == "no" && simulatedOutcome == 1)
			outcomeStr := "Up"
			if simulatedOutcome == 1 {
				outcomeStr = "Down"
			}
			t.settleWithOutcome(pos, won, outcomeStr, "simulated_fallback")
			return
		case <-ticker.C:
			// 继续轮询
		}
	}
}

// settleWithOutcome 按指定结果结算持仓。
func (t *Trader) settleWithOutcome(pos *Position, won bool, outcomeStr, source string) {
	t.mu.Lock()
	defer t.mu.Unlock()

	pos.Won = won
	pos.Outcome = 0
	if won && pos.TokenSide == "no" {
		pos.Outcome = 1
	} else if won && pos.TokenSide == "yes" {
		pos.Outcome = 0
	} else if !won && pos.TokenSide == "yes" {
		pos.Outcome = 1
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
	// TODO: 实现 market SELL
	return nil, fmt.Errorf("手动平仓尚未实现")
}

// ── 后台 goroutine ──

// resolutionWatcher 监听 WS 结算事件，结算匹配的持仓。
func (t *Trader) resolutionWatcher(ctx context.Context) {
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.done:
			return
		case info := <-t.resolved:
			t.mu.RLock()
			pos := t.exec.Position
			t.mu.RUnlock()
			if pos != nil && info.Market == pos.ConditionID {
				won := info.WinningAssetId == pos.TokenID
				t.settleWithOutcome(pos, won, info.WinningOutcome, "ws")
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
