package collect

import (
	"context"
	"log"
	"math"
	"math/rand"
	"time"
)

// OfficialPriceFunc 单次拉取某窗口官方 TWAP 开/收盘价（一次调用，不做轮询）。
// ok=true 表示 open 与 close 均有效。实现应使用 ctx 感知的请求
// （SDK FetchOpenPriceContext），ctx 取消/到期时立即返回。
type OfficialPriceFunc func(ctx context.Context, start time.Time) (open, close float64, ok bool)

// NeedsOfficialCorrection 判定事件是否需要官方结算修正：
//
//  1. 收盘时刻流推送过旧（age > maxStreamAgeMs，覆盖流冻结/WS 中断场景）；
//  2. |close-open| 小于 minRange —— outcome 由噪声决定，流采样相对官方
//     边界值的量化误差（TWAP-60 推送 ~2s 间隔，142 窗实测 2s 移动
//     p99=$1.82）足以翻转结果。
//
// 两条都不满足时流采样值可直接定稿：误差不足以翻转 outcome，
// 幅度特征相对误差 <15%（MinRange=$15 时，见 types.go 标定注释）。
func NeedsOfficialCorrection(closeAgeMs int64, open, close, minRange float64, maxStreamAgeMs int64) bool {
	if maxStreamAgeMs > 0 && closeAgeMs > maxStreamAgeMs {
		return true
	}
	return math.Abs(close-open) < minRange
}

// SettlementWorker 是队列化的写盘与结算修正器。
//
// 设计动机（2026-08-18）：官方收盘价产出延迟为分钟级（60s 轮询实测
// 142/142 全部未命中），事件写盘不能被官方价
// 拖住。窗口结束时主循环把流值口径的事件交给本 worker：
//
//  1. 立即落盘（close_source=stream，outcome 流值口径）—— 写盘延迟归零，
//     进程重启也不丢已采集窗口
//  2. 后台按 PollInterval 轮询官方开/收盘，最长 MaxWait，到达后追加
//     SettlementCorrection 修正行（WriteCorrection，同文件、按 start_time
//     去重），分析侧按 start_time 合并覆盖
//
// 修正轮询按窗口并发（每窗一个 goroutine），MaxConcurrent 限流；
// 队列与写盘由单 goroutine 串行处理，写入顺序与窗口结束顺序一致。
// 该架构供后续实盘数据沿用。
type SettlementWorker struct {
	dir   string
	fetch OfficialPriceFunc
	cfg   SettlementConfig
	queue chan queueItem
	sem   chan struct{}
	done  chan struct{}
}

// NewSettlementWorker 创建结算修正 worker。fetch 为单次官方价拉取回调。
func NewSettlementWorker(dir string, fetch OfficialPriceFunc, cfg SettlementConfig) *SettlementWorker {
	if cfg.PollInterval <= 0 {
		cfg.PollInterval = DefaultSettlementConfig().PollInterval
	}
	if cfg.MaxWait <= 0 {
		cfg.MaxWait = DefaultSettlementConfig().MaxWait
	}
	if cfg.MaxConcurrent <= 0 {
		cfg.MaxConcurrent = DefaultSettlementConfig().MaxConcurrent
	}
	return &SettlementWorker{
		dir:   dir,
		fetch: fetch,
		cfg:   cfg,
		queue: make(chan queueItem, 4),
		sem:   make(chan struct{}, cfg.MaxConcurrent),
		done:  make(chan struct{}),
	}
}

// Start 启动 worker 主循环（单 goroutine）。ctx 取消后队列内剩余事件
// 仍会落盘，随后退出；在途修正轮询随 ctx 终止。
func (w *SettlementWorker) Start(ctx context.Context) {
	go func() {
		defer close(w.done)
		for {
			select {
			case <-ctx.Done():
				// 排空队列：已采集的数据不能丢
				for {
					select {
					case item := <-w.queue:
						w.writeImmediate(ctx, item.ev, item.needsCorrection)
					default:
						return
					}
				}
			case item := <-w.queue:
				w.writeImmediate(ctx, item.ev, item.needsCorrection)
			}
		}
	}()
}

// queueItem 是队列元素：事件 + 是否需要官方结算修正。
type queueItem struct {
	ev              *Event
	needsCorrection bool
}

// Submit 投递一个已定稿（流值口径）的事件。窗口结束顺序即投递顺序。
// needsCorrection=true 时后台轮询官方开/收盘并追加修正行；
// false 时流采样值直接定稿（幅度/新鲜度满足阈值，见 NeedsOfficialCorrection）。
//
// worker 已退出（ctx 取消后排空队列即退出）时不再入队：此时队列无消费者，
// 阻塞发送会让调用方（如关闭路径）永久挂起，事件记录日志后丢弃。
func (w *SettlementWorker) Submit(ev *Event, needsCorrection bool) {
	item := queueItem{ev: ev, needsCorrection: needsCorrection}
	select {
	case w.queue <- item:
	case <-w.done:
		log.Printf("[Settle] ⚠️ worker 已退出，丢弃待写事件 %s", ev.ConditionID)
	}
}

// Done 返回 worker 退出信号（测试/优雅关闭用）。
func (w *SettlementWorker) Done() <-chan struct{} {
	return w.done
}

// writeImmediate 立即落盘事件（流值口径）；需要修正时启动后台官方价轮询。
func (w *SettlementWorker) writeImmediate(ctx context.Context, ev *Event, needsCorrection bool) {
	written, err := WriteUniqueEvent(w.dir, ev)
	if err != nil {
		log.Printf("[Settle] 事件写入失败: %v", err)
		return
	}
	if !written {
		log.Printf("[Settle] ⚠️ %s 重复窗口，跳过写入", ev.ConditionID)
		return
	}
	if needsCorrection {
		log.Printf("[Settle] %s 已落盘（流值口径，等待官方修正）", ev.ConditionID)
		start := time.Unix(ev.StartTime, 0).UTC()
		go w.settleWindow(ctx, start)
		return
	}
	log.Printf("[Settle] %s 已落盘（流值定稿，幅度/新鲜度满足阈值，跳过官方修正）", ev.ConditionID)
}

// settleWindow 后台轮询官方开/收盘价，到达后追加修正行。
// 最长轮询 cfg.MaxWait；ctx 取消立即退出。
func (w *SettlementWorker) settleWindow(ctx context.Context, start time.Time) {
	select {
	case w.sem <- struct{}{}: // 并发限流（FIFO 近似）
	case <-ctx.Done():
		return
	}
	defer func() { <-w.sem }()

	deadline := time.Now().Add(w.cfg.MaxWait)
	for {
		if time.Now().After(deadline) || ctx.Err() != nil {
			return
		}
		open, close, ok := w.fetch(ctx, start)
		if ok {
			corr := &SettlementCorrection{
				EventType:      "settlement_correction",
				StartTime:      start.Unix(),
				TwapOpenPrice:  open,
				TwapClosePrice: close,
				CloseSource:    "official",
			}
			if close >= open {
				corr.Outcome = 0 // Up（平局算 Up，与事件行口径一致）
			} else {
				corr.Outcome = 1 // Down
			}
			written, err := WriteCorrection(w.dir, corr)
			if err != nil {
				log.Printf("[Settle] 修正行写入失败: %v", err)
			} else if written {
				log.Printf("[Settle] ✅ %s 官方结算修正: open=%.2f close=%.2f outcome=%d",
					start.Format("15:04"), open, close, corr.Outcome)
			}
			return
		}
		// 固定间隔 + 0-2s 抖动：多窗口修正轮询错峰，避免同拍打限速接口
		wait := w.cfg.PollInterval + time.Duration(rand.Int63n(int64(2*time.Second)))
		select {
		case <-ctx.Done():
			return
		case <-time.After(wait):
		}
	}
}
