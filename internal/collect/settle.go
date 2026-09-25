package collect

import (
	"context"
	"log"
	"math/rand"
	"time"
)

// OfficialPriceFunc 单次拉取某窗口官方 TWAP 开/收盘价（一次调用，不做轮询）。
// ok=true 表示 open 与 close 均有效。实现应使用 ctx 感知的请求
// （SDK FetchOpenPriceContext），ctx 取消/到期时立即返回。
type OfficialPriceFunc func(ctx context.Context, start time.Time) (open, close float64, ok bool)

// SettlementWorker 是队列化的写盘与结算修正器。
//
// 设计动机（2026-08-18）：官方收盘价产出延迟为分钟级（60s 轮询实测
// 142/142 全部未命中，见 cmd/probe_close 实测），事件写盘不能被官方价
// 拖住。窗口结束时主循环把已定稿的事件交给本 worker：
//
//  1. 立即落盘 —— 写盘延迟归零，进程重启也不丢已采集窗口
//  2. 仅当**边界推送缺失**（事件行 close_source/anchor_source 非 SourcePush）
//     时，后台按 PollInterval 轮询官方开/收盘，最长 MaxWait，到达后追加
//     SettlementCorrection 修正行（WriteCorrection，同文件、按 start_time
//     去重），分析侧按 start_time 合并覆盖
//
// ⚠️ 2026-09-25 同步时口径变更：eth 版是「按幅度阈值判断是否需要官方修正」
// （|close−open| < MinRange 时流采样误差可能翻转 outcome），因为它的收盘价
// 是**到达口径**流采样、与官方边界值差约 2s 的量化误差。现改用**边界那一秒
// 的推送**（与官方逐位同源，决策 #19），误差归零 ⇒ 修正的唯一触发条件变成
// 「这一窗没拿到推送」，与幅度/新鲜度无关（NeedsOfficialCorrection 及
// SettlementConfig 的 MinRange/MaxStreamAgeMs 一并删除）。
//
// 修正轮询按窗口并发（每窗一个 goroutine），MaxConcurrent 限流；
// 队列与写盘由单 goroutine 串行处理，写入顺序与窗口结束顺序一致。
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

// Submit 投递一个已定稿的事件。窗口结束顺序即投递顺序。
// needsCorrection=true 时后台轮询官方开/收盘并追加修正行（推送缺失的窗口）；
// false 时推送值与官方同源，直接定稿。
//
// ⚠️ 必须同时等 done：worker 已退出（ctx 取消后的收尾路径）时队列无人消费，
// 裸发送在队列满（容量 4）时永久阻塞 —— 主循环收尾会卡死在这里。
// done 分支直接写盘（此时修正轮询已无意义：ctx 已死，轮到也不会有结果）。
func (w *SettlementWorker) Submit(ev *Event, needsCorrection bool) {
	select {
	case w.queue <- queueItem{ev: ev, needsCorrection: needsCorrection}:
	case <-w.done:
		log.Printf("[Settle] ⚠️ worker 已退出，改为直接落盘 %s", ev.ConditionID)
		if written, err := WriteUniqueEvent(w.dir, ev); err != nil {
			log.Printf("[Settle] 事件写入失败: %v", err)
		} else if !written {
			log.Printf("[Settle] ⚠️ %s 重复窗口，跳过写入", ev.ConditionID)
		}
	}
}

// Done 返回 worker 退出信号（测试/优雅关闭用）。
func (w *SettlementWorker) Done() <-chan struct{} {
	return w.done
}

// writeImmediate 立即落盘事件；需要修正时启动后台官方价轮询。
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
		log.Printf("[Settle] %s 已落盘（推送缺失，等待官方修正）", ev.ConditionID)
		start := time.Unix(ev.StartTime, 0).UTC()
		go w.settleWindow(ctx, start)
		return
	}
	log.Printf("[Settle] %s 已落盘（推送口径定稿，与官方同源，无需修正）", ev.ConditionID)
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
				CloseSource:    SourceOfficial,
				AnchorSource:   SourceOfficial,
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
