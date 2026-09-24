package main

// 盘口探针（2026-09-24）: 用**引擎同一条 SDK WS 通道**（MarketMonitor.SubscribeOrderBook）
// 逐秒记录两侧 token 的盘口, 回答用户提出的问题:
//
//	「事件几乎趋于确定了, 可能就没有 ask 了 —— 监听一个完整事件, 看最后几十秒到底
//	 有没有 ask」
//
// 为什么必须现采: 引擎落盘的 `tail_*` / `touches_*` 只有四档**价格**（没有挂单量、
// 没有空簿状态）, 而「有没有 ask」是盘口**存在性**问题——空簿在现有 JSONL 里不留任何
// 痕迹。SDK 的 REST `/book` 与 WS 又是两条口, 必须看引擎真正吃的那条。
//
// 每条秒记录同时落两种视角（这是本探针的关键）:
//
//	raw  = SDK WS 最近一条整簿消息的**原样**状态（`n_asks = 0` ⇒ 真的没有 ask）
//	eng  = **引擎视角**: cmd/flip 与 cmd/tail 的 WS 循环里有一句守卫
//	       `if len(book.Bids)==0 || len(book.Asks)==0 { continue }`——空侧消息被丢弃,
//	       内存里留下的是**撤单前那一份旧簿**。故 eng 会显示一个早已不存在的 ask
//	       （常为 0.97~0.99）, 而 raw 显示空。两者之差 = 引擎的陈旧读数。
//
// 还落 `ask_wire5` / `bid_wire5`: WS 消息里**原样顺序**的前 5 个价位（不做排序），
// 用来实证 pmtick.go 注释所称的「asks 降序（0.99 在前）/ bids 升序」是否属实。
//
// 输出: data/probe/book_YYYY-MM-DD.jsonl（kind=sec 每秒一行; kind=evt 空侧翻转各一行）
//
// 用法:
//
//	https_proxy=http://127.0.0.1:1087 go run ./cmd/bookprobe -config v4.config.yaml -minutes 12
//
// 采完跑: python/venv/bin/python python/v4/22_book_empty_ask_audit.py
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
	"sync"
	"syscall"
	"time"

	"github.com/xiangxn/go-polymarket-sdk/orders"
	sdk "github.com/xiangxn/go-polymarket-sdk/polymarket"

	"github.com/necklace/flip-signal/internal/config"
	"github.com/necklace/flip-signal/internal/feed"
)

const windowSec = 300 // 5 分钟窗（与 btc-updown-5m 对齐）

// tokView 是**一条**秒记录里某个 token 的两套读数。
type tokView struct {
	Asset        string       `json:"asset,omitempty"`
	Msgs         int          `json:"msgs"`           // 本秒收到的整簿消息数
	EmptyAskMsgs int          `json:"empty_ask_msgs"` // 累计: asks 为空的整簿消息数
	EmptyBidMsgs int          `json:"empty_bid_msgs"` // 累计: bids 为空的整簿消息数
	RawTs        int64        `json:"raw_ts"`         // 最近一条整簿消息里的服务器时间戳
	RawAgeMs     int64        `json:"raw_age_ms"`     // 该消息的本地接收龄（探针侧现算, 无偏）
	NAks         int          `json:"n_asks"`         // raw: 卖档数（0 = 真的没有 ask）
	NBids        int          `json:"n_bids"`
	Ask          float64      `json:"ask"`    // raw: 最优卖价（空侧 = 0）
	AskSz        float64      `json:"ask_sz"` // raw: 最优卖档的股数
	Bid          float64      `json:"bid"`    // raw: 最优买价
	BidSz        float64      `json:"bid_sz"`
	AskWire5     []float64    `json:"ask_wire5"` // 原样顺序前 5 档价（实证排序约定）
	BidWire5     []float64    `json:"bid_wire5"`
	AskLv        [][2]float64 `json:"ask_lv"`  // 最优起逐档 [价, 股]（已排序, 最多 levels 档）
	EngAsk       float64      `json:"eng_ask"` // 引擎视角（丢弃空侧消息后的陈旧簿）
	EngAskSz     float64      `json:"eng_ask_sz"`
	EngAgeMs     int64        `json:"eng_age_ms"` // 引擎视角的簿龄（= 陈旧程度）
}

type secLine struct {
	Kind   string  `json:"kind"`
	Ts     int64   `json:"ts"`
	Window int64   `json:"window"`
	Rem    int     `json:"rem"`
	Slug   string  `json:"slug"`
	Sub    bool    `json:"subscribed"`
	UP     tokView `json:"up"`
	Down   tokView `json:"down"`
}

type evtLine struct {
	Kind   string  `json:"kind"`
	Ts     int64   `json:"ts"`
	Window int64   `json:"window"`
	Rem    int     `json:"rem"`
	Tok    string  `json:"tok"`
	Event  string  `json:"event"` // ask_empty / ask_back
	NAks   int     `json:"n_asks"`
	Ask    float64 `json:"ask"`
	AskSz  float64 `json:"ask_sz"`
}

// bookState 是某个 token 的运行时读数（一个 goroutine 写、主循环读，故加锁）。
type bookState struct {
	mu           sync.Mutex
	raw          *sdk.OrderBook // 最近一条整簿消息（含空侧）
	rawAtMs      int64
	eng          *sdk.OrderBook // 最近一条**通过引擎守卫**的整簿（空侧被丢弃）
	engAtMs      int64
	msgs         int
	emptyAskMsgs int
	emptyBidMsgs int
	lastNAks     int
	asset        string
}

func (s *bookState) on(book *sdk.OrderBook) (evt string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := time.Now().UnixMilli()
	s.msgs++
	s.raw, s.rawAtMs = book, now
	if s.asset == "" {
		s.asset = book.AssetId
	}
	if len(book.Asks) == 0 {
		s.emptyAskMsgs++
	}
	if len(book.Bids) == 0 {
		s.emptyBidMsgs++
	}
	// 引擎守卫（cmd/flip:224 与 cmd/tail:363 同款）——空侧消息被丢弃, eng 不变
	if len(book.Bids) > 0 && len(book.Asks) > 0 {
		s.eng, s.engAtMs = book, now
	}
	// 空侧翻转事件（以**引擎看到的**档数为准: 引擎丢空侧消息 ⇒ 它察觉不到翻转,
	// 事件流按 raw 记, 供事后对比「引擎晚了多久才知道」）
	na := len(book.Asks)
	if (na == 0) != (s.lastNAks == 0) {
		if na == 0 {
			evt = "ask_empty"
		} else {
			evt = "ask_back"
		}
	}
	s.lastNAks = na
	return evt
}

// view 生成本秒的 token 读数（levels = 逐档记录档数）。
func (s *bookState) view(levels int) tokView {
	s.mu.Lock()
	defer s.mu.Unlock()
	v := tokView{Asset: s.asset, Msgs: s.msgs, EmptyAskMsgs: s.emptyAskMsgs,
		EmptyBidMsgs: s.emptyBidMsgs}
	now := time.Now().UnixMilli()
	if s.raw != nil {
		v.RawTs, v.RawAgeMs = s.raw.Timestamp, now-s.rawAtMs
		v.NAks, v.NBids = len(s.raw.Asks), len(s.raw.Bids)
		// 原样顺序前 5 档（不排序——这是对 pmtick.go 排序约定的实证）
		for i, l := range s.raw.Asks {
			if i >= 5 {
				break
			}
			v.AskWire5 = append(v.AskWire5, l.Price)
		}
		for i, l := range s.raw.Bids {
			if i >= 5 {
				break
			}
			v.BidWire5 = append(v.BidWire5, l.Price)
		}
		ask := append([]lvl(nil), toLevels(s.raw.Asks)...)
		bid := toLevels(s.raw.Bids)
		sortAsk(ask)
		sortBid(bid)
		if len(ask) > 0 {
			v.Ask, v.AskSz = ask[0][0], ask[0][1]
			if len(ask) > levels {
				ask = ask[:levels]
			}
			v.AskLv = ask
		}
		if len(bid) > 0 {
			v.Bid, v.BidSz = bid[0][0], bid[0][1]
		}
	}
	if s.eng != nil {
		eng := toLevels(s.eng.Asks)
		sortAsk(eng)
		if len(eng) > 0 {
			v.EngAsk, v.EngAskSz = eng[0][0], eng[0][1]
		}
		v.EngAgeMs = now - s.engAtMs
	}
	return v
}

// lvl 是 [价, 量] 的别名（写起来短一点）。
type lvl = [2]float64

func toLevels(bs []orders.Book) []lvl {
	out := make([]lvl, 0, len(bs))
	for _, b := range bs {
		out = append(out, lvl{b.Price, b.Size})
	}
	return out
}

func sortAsk(l []lvl) { // 最优卖 = 最低价
	for i := 1; i < len(l); i++ {
		for j := i; j > 0 && l[j][0] < l[j-1][0]; j-- {
			l[j], l[j-1] = l[j-1], l[j]
		}
	}
}

func sortBid(l []lvl) { // 最优买 = 最高价
	for i := 1; i < len(l); i++ {
		for j := i; j > 0 && l[j][0] > l[j-1][0]; j-- {
			l[j], l[j-1] = l[j-1], l[j]
		}
	}
}

func main() {
	configPath := flag.String("config", "", "配置文件路径（空 = 代码默认值）")
	outDir := flag.String("out", "data/probe", "输出目录")
	minutes := flag.Float64("minutes", 12, "运行时長（分钟）")
	levels := flag.Int("levels", 8, "逐档记录档数")
	flag.Parse()

	cfg, err := config.Load(*configPath)
	if err != nil {
		log.Fatalf("[BookProbe] 配置加载失败: %v", err)
	}
	cfgSDK := cfg.SDK
	if cfgSDK.Polymarket.OwnerKey == "" { // 只读运行: 生成临时密钥（不碰任何凭证）
		key := make([]byte, 32)
		if _, err := rand.Read(key); err != nil {
			log.Fatalf("[BookProbe] 生成临时密钥失败: %v", err)
		}
		cfgSDK.Polymarket.OwnerKey = hex.EncodeToString(key)
		log.Println("[BookProbe] 未提供 owner_key —— 只读运行（不订阅私有频道、不下单）")
	}
	client := sdk.NewClient(&cfgSDK)

	if err := os.MkdirAll(*outDir, 0o755); err != nil {
		log.Fatalf("[BookProbe] 建输出目录失败: %v", err)
	}
	day := time.Now().UTC().Format("2006-01-02")
	path := filepath.Join(*outDir, fmt.Sprintf("book_%s.jsonl", day))
	f, err := os.Create(path)
	if err != nil {
		log.Fatalf("[BookProbe] 建输出文件失败: %v", err)
	}
	defer f.Close()
	w := bufio.NewWriter(f)
	defer w.Flush()

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()

	monitor := sdk.NewMarketMonitor(cfgSDK.Polymarket.ClobWSBaseURL, false, client, false)
	upS, downS := &bookState{}, &bookState{}

	go func() {
		ch := monitor.SubscribeOrderBook()
		for {
			select {
			case <-ctx.Done():
				return
			case book := <-ch:
				if book == nil {
					continue
				}
				var st *bookState
				switch book.AssetId {
				case upS.assetOf():
					st = upS
				case downS.assetOf():
					st = downS
				default:
					continue
				}
				evt := st.on(book)
				if evt != "" {
					writeJSON(w, evtLine{Kind: "evt", Ts: time.Now().UnixMilli(),
						Window: curWindow(), Rem: rem(), Tok: st.assetOf(), Event: evt,
						NAks: len(book.Asks), Ask: bestAsk(book), AskSz: bestAskSz(book)})
					w.Flush()
				}
			}
		}
	}()
	go func() {
		for {
			if err := monitor.Run(ctx); err != nil && ctx.Err() == nil {
				log.Printf("[BookProbe] ⚠️ MarketMonitor 退出: %v —— 5 秒后重启", err)
			}
			if ctx.Err() != nil {
				return
			}
			time.Sleep(5 * time.Second)
		}
	}()

	log.Printf("[BookProbe] 🎯 启动: 每窗 %ds, 运行 %.1f 分钟 → %s", windowSec, *minutes, path)
	tEnd := time.Now().Add(time.Duration(*minutes * float64(time.Minute)))
	var curWin int64
	var slug string
	for time.Now().Before(tEnd) {
		if ctx.Err() != nil {
			break
		}
		ws := curWindow()
		if ws != curWin { // 新窗: 换订阅
			curWin = ws
			slug = fmt.Sprintf("%s-%d", cfg.Runtime.SlugPrefix, ws)
			up, down, err := fetchTokens(client, slug)
			if err != nil {
				log.Printf("[BookProbe] ⚠️ 取市场失败 %s: %v", slug, err)
			} else {
				if old := upS.assetOf(); old != "" {
					monitor.UnsubscribeTokens(old, downS.assetOf())
				}
				upS.setAsset(up)
				downS.setAsset(down)
				monitor.SubscribeTokens(up, down)
				log.Printf("[BookProbe] 📡 订阅 %s (up=%s… down=%s…)", slug, up[:8], down[:8])
			}
		}
		writeJSON(w, secLine{Kind: "sec", Ts: time.Now().UnixMilli(), Window: curWin,
			Rem: rem(), Slug: slug, Sub: upS.assetOf() != "",
			UP: upS.view(*levels), Down: downS.view(*levels)})
		w.Flush()
		time.Sleep(time.Second)
	}
	log.Printf("[BookProbe] ✅ 完成 → %s", path)
}

func writeJSON(w *bufio.Writer, v any) {
	b, err := json.Marshal(v)
	if err != nil {
		return
	}
	w.Write(b)
	w.WriteByte('\n')
}

func curWindow() int64 { return time.Now().Unix() / windowSec * windowSec }
func rem() int         { return int(curWindow() + windowSec - time.Now().Unix()) }

func bestAsk(b *sdk.OrderBook) float64 {
	best := 0.0
	for _, l := range b.Asks {
		if best == 0 || l.Price < best {
			best = l.Price
		}
	}
	return best
}

func bestAskSz(b *sdk.OrderBook) float64 {
	best, sz := 0.0, 0.0
	for _, l := range b.Asks {
		if best == 0 || l.Price < best {
			best, sz = l.Price, l.Size
		}
	}
	return sz
}

func fetchTokens(client *sdk.PolymarketClient, slug string) (string, string, error) {
	data, err := client.FetchMarketBySlug(slug)
	if err != nil {
		return "", "", err
	}
	if data == nil {
		return "", "", fmt.Errorf("市场不存在: %s", slug)
	}
	up, down := feed.ParseMarketTokens(data)
	if up == "" || down == "" {
		return "", "", fmt.Errorf("token 解析失败: %s", slug)
	}
	return up, down, nil
}

func (s *bookState) assetOf() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.asset
}

func (s *bookState) setAsset(a string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.asset, s.raw, s.eng, s.lastNAks = a, nil, nil, 0
}
