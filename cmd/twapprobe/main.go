package main

// TWAP 推送探针（2026-09-24）: 订阅 Chainlink TWAP-60 推送流, 把**每一条推送**
// 逐条落盘（评估时刻 / 本地到达时刻 / 价格）, 供离线对照官方 crypto-price 接口的
// open/close —— 回答「官方结算价对齐的是窗口第 0 秒，还是前一秒（59s）」。
//
// 为什么需要独立探针: 引擎只落盘**每窗一条**锚（windows_*.jsonl 的 anchor =
// 边界那一秒的推送, 决策 #15），而这个问题需要看**边界前后每一秒**的推送值
// （t−1 / t=0 / t=1 …），历史数据里没有 —— 只能现采。
//
// 输出: 每日一个 JSONL, 每行一条推送
//
//	{"ts":1789...000, "arrived":1789...123, "price":76393.31953905817}
//
//	ts      = payload.timestamp = **服务器侧 TWAP 评估时刻**（1 秒整格, unix 毫秒）
//	arrived = 本地收到该条的时刻（unix 毫秒）
//	二者之差 = 发布延迟 + 本机与服务器的时钟偏差（探针不做任何校正, 原样落盘）
//
// 与 engine 的差异（有意）: ① 缺 payload.timestamp 的推送**照常落盘**（标 ts=0,
// 引擎会丢弃它们）—— 丢了多少条本身是要看的证据; ② 不设环形缓存、不做精确匹配,
// 只做无过滤的记录。
//
// 用法:
//
//	https_proxy=http://127.0.0.1:1087 go run ./cmd/twapprobe -duration 60m
//	go run ./cmd/twapprobe -out data/probe -duration 0     # 0 = 直到 Ctrl-C
//
// 采完跑: python/venv/bin/python python/v4/20_boundary_push_align.py
import (
	"bufio"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"syscall"
	"time"

	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"
)

// pushLine 是一条落盘记录（字段名与本文件头注释一致）。
type pushLine struct {
	Ts      int64   `json:"ts"`
	Arrived int64   `json:"arrived"`
	Price   float64 `json:"price"`
}

// probe 是探针运行时状态（单 goroutine 写, 心跳读用 mu）。
type probe struct {
	mu       sync.Mutex
	out      *bufio.Writer
	file     *os.File
	day      string // 当前文件对应的 UTC 日（按日切分）
	outDir   string
	total    int
	dropped  int // 缺 payload.timestamp 的条数
	zeroPx   int // 非正价格条数
	delays   []int64
	lastTs   int64
	lastN    int // 上次心跳时的条数（算速率）
	lastAt   time.Time
	boundary int // 已覆盖的 5 分钟边界数
}

func main() {
	symbol := flag.String("symbol", "btc", "标的（与 SDK 订阅名一致, 小写）")
	window := flag.Int64("window", 60, "TWAP 窗口秒数（60 = TWAP-60, 与 btc-updown-5m 结算口径一致）")
	dur := flag.Duration("duration", 0, "运行时长（0 = 直到 Ctrl-C）")
	outDir := flag.String("out", "data/probe", "输出目录（按 UTC 日切分 twap_push_YYYY-MM-DD.jsonl）")
	flag.Parse()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if *dur > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, *dur)
		defer cancel()
	}

	if err := os.MkdirAll(*outDir, 0o755); err != nil {
		log.Fatalf("[Probe] 无法创建输出目录: %v", err)
	}

	// SDK 客户端: 与引擎的只读路径同款 —— 空 OwnerKey 时 SDK 的占位私钥会被
	// HexToECDSA 接受但毫无意义, 这里显式换成随机密钥（探针只用公开行情 WS,
	// 不签名、不下单）。RateLimit 复位 0 走 SDK 内建兜底（同 internal/config）。
	cfgSDK := *sdk.DefaultConfig()
	key := make([]byte, 32)
	if _, err := rand.Read(key); err != nil {
		log.Fatalf("[Probe] 生成临时密钥失败: %v", err)
	}
	cfgSDK.Polymarket.OwnerKey = hex.EncodeToString(key)
	cfgSDK.RateLimitMaxRetries = 0
	cfgSDK.RateLimitBaseDelay = 0
	client := sdk.NewClient(&cfgSDK)

	p := &probe{outDir: *outDir, lastAt: time.Now()}
	defer p.close()

	symbols := []string{fmt.Sprintf("%s_%d", strings.ToLower(*symbol), *window)}
	log.Printf("[Probe] 🎯 订阅 %v（TWAP-%ds）→ %s/twap_push_<UTC 日>.jsonl", symbols, *window, *outDir)

	// 重连循环: monitor.Run 返回（断线/服务器踢）即重建订阅——探针要能连续跑几小时,
	// 断流期留白本身就是证据（对齐时能看到缺哪一秒）。
	sub := func() <-chan sdk.ExternalPrice {
		m := sdk.NewCryptoPriceMonitor(client, sdk.MonitorChainlinkTwap, symbols...)
		ch := m.Subscribe()
		go func() {
			if err := m.Run(ctx); err != nil && ctx.Err() == nil {
				log.Printf("[Probe] ⚠️ monitor 退出: %v（2s 后重连）", err)
			}
		}()
		return ch
	}

	ch := sub()
	go p.heartbeat(ctx)
	for {
		select {
		case <-ctx.Done():
			p.mu.Lock()
			log.Printf("[Probe] 🛑 结束: 共 %d 条推送, 缺时间戳 %d 条, 非正价 %d 条, 覆盖边界 %d 个",
				p.total, p.dropped, p.zeroPx, p.boundary)
			p.mu.Unlock()
			return
		case ep, ok := <-ch:
			if !ok {
				time.Sleep(2 * time.Second)
				ch = sub()
				continue
			}
			if !strings.EqualFold(ep.Symbol, *symbol) || ep.WindowSeconds != *window {
				continue
			}
			p.write(ep)
		}
	}
}

// write 落盘一条推送并按需打印边界行。
func (p *probe) write(ep sdk.ExternalPrice) {
	now := time.Now().UnixMilli()
	p.mu.Lock()
	defer p.mu.Unlock()
	if ep.Timestamp <= 0 {
		p.dropped++
		if p.dropped == 1 {
			log.Printf("[Probe] ⚠️ 收到缺 payload.timestamp 的推送（照常落盘 ts=0）")
		}
	}
	if ep.Price <= 0 {
		p.zeroPx++
	}
	if err := p.rotateLocked(now); err != nil {
		log.Printf("[Probe] ⚠️ 切换输出文件失败: %v", err)
	}
	line, _ := json.Marshal(pushLine{Ts: ep.Timestamp, Arrived: now, Price: ep.Price})
	if _, err := p.out.Write(append(line, '\n')); err == nil {
		_ = p.out.Flush() // 行级 flush: 崩溃/断电不丢已采样本
	}
	p.total++
	p.lastTs = ep.Timestamp
	if d := now - ep.Timestamp; ep.Timestamp > 0 && d >= 0 {
		p.delays = append(p.delays, d)
		if len(p.delays) > 512 {
			p.delays = p.delays[len(p.delays)-512:]
		}
	}
	// 边界行: 评估时刻落在 5 分钟整点的那一条（= 引擎取锚要的那条）
	if ep.Timestamp > 0 && ep.Timestamp%(5*60*1000) == 0 {
		p.boundary++
		t := time.UnixMilli(ep.Timestamp).UTC()
		log.Printf("[Probe] 🎯 边界推送 %s  price=%.6f  到达 +%.2fs",
			t.Format("15:04:05"), ep.Price, float64(now-ep.Timestamp)/1000)
	}
}

// rotateLocked 按 UTC 日切换输出文件（同引擎 recorder 的按日切分）。
// 调用方须持有 p.mu（因此内部只调 closeLocked, 不能调会加锁的 close）。
func (p *probe) rotateLocked(nowMs int64) error {
	day := time.UnixMilli(nowMs).UTC().Format("2006-01-02")
	if day == p.day && p.out != nil {
		return nil
	}
	p.closeLocked()
	name := filepath.Join(p.outDir, "twap_push_"+day+".jsonl")
	f, err := os.OpenFile(name, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	p.file, p.out, p.day = f, bufio.NewWriter(f), day
	log.Printf("[Probe] 📝 输出 → %s", name)
	return nil
}

func (p *probe) close() {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.closeLocked()
}

func (p *probe) closeLocked() {
	if p.out != nil {
		_ = p.out.Flush()
	}
	if p.file != nil {
		_ = p.file.Close()
		p.file, p.out = nil, nil
	}
}

// heartbeat 每 60s 打印一次健康度: 速率 / 最新评估时刻偏移 / 到达延迟分位。
func (p *probe) heartbeat(ctx context.Context) {
	tk := time.NewTicker(60 * time.Second)
	defer tk.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tk.C:
			p.mu.Lock()
			n, lastTs, delays := p.total, p.lastTs, append([]int64(nil), p.delays...)
			rate := float64(n-p.lastN) / time.Since(p.lastAt).Minutes()
			p.lastN, p.lastAt = n, time.Now()
			p.mu.Unlock()
			sort.Slice(delays, func(i, j int) bool { return delays[i] < delays[j] })
			q := func(f float64) int64 {
				if len(delays) == 0 {
					return 0
				}
				return delays[int(float64(len(delays)-1)*f)]
			}
			off := int64(0)
			if lastTs > 0 {
				off = time.Now().UnixMilli() - lastTs
			}
			log.Printf("[Probe] 💓 %d 条（%.1f 条/分）, 最新评估偏移 %.2fs, 到达延迟 p50=%.2fs p90=%.2fs",
				n, rate, float64(off)/1000, float64(q(0.5))/1000, float64(q(0.9))/1000)
		}
	}
}
