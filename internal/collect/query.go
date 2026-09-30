package collect

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"strconv"
)

// LoadEventByStart 按窗口起点（unix 秒）从 dir 回读该窗的事件行（数据格式 v2）。
//
// 用途：Dashboard 的「点行看曲线」——历史窗的曲线由 events 行**纯读**重建
// （决策 #31），不新落盘任何曲线数据。
//
// 返回三态：
//   - (ev, true,  nil) 命中
//   - (nil, false, nil) dir 为空 / 文件不存在 / 那天没有这一窗（含被落盘红线丢弃的窗）
//   - (nil, false, err) 读失败
//
// ⚠️ 文件按**窗口起点**归日（DayForStart），传进来的必须是窗口起点：拿「这行数据落在
// 哪天」或交易时刻的 UTC 日去查，跨午夜的窗会查到隔壁文件里，结果是「明明有采集却报
// 没有」——不会报错，只会静默查空。
func LoadEventByStart(dir string, startTime int64) (*Event, bool, error) {
	if dir == "" || startTime <= 0 {
		return nil, false, nil
	}
	path := eventPath(dir, DayForStart(startTime))
	f, err := os.Open(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, false, nil
		}
		return nil, false, fmt.Errorf("open %s: %w", path, err)
	}
	defer f.Close()

	// ⚠️ 一行实测 ~210KB，远超 bufio.Scanner 默认上限 64KB（token too long）
	// ——与 scanLines 同一颗雷，同样显式调大。
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 0, 64*1024), 16<<20)

	// 行内预筛的字面量：本包写侧一律 json.Marshal（紧凑、无空格），故这个形状是稳的。
	// 若哪天换了带缩进的编码器，预筛会**全部落空**（表现为「有采集却报没有」）。
	needle := strconv.AppendInt([]byte(`"start_time":`), startTime, 10)
	for scanner.Scan() {
		line := scanner.Bytes()
		// 先子串预筛再整行解码：一行 210KB 的 json.Unmarshal 是毫秒级，一天几百行就是
		// 几百毫秒——而「这行是不是目标窗」看 start_time 一个字面量就够了。
		if !lineHasStartTime(line, needle) {
			continue
		}
		// 借匿名嵌入读 event_type：Event 自己没有这个字段，而修正行与事件行**同
		// start_time**（本就是同一个窗），不区分就会把没有 ticks 的修正行当事件返回。
		var row struct {
			Event
			EventType string `json:"event_type"`
		}
		if err := json.Unmarshal(line, &row); err != nil {
			continue // 坏行跳过：一行坏掉不该废掉整个回读
		}
		if row.EventType != "" || row.StartTime != startTime {
			continue
		}
		return &row.Event, true, nil
	}
	if err := scanner.Err(); err != nil {
		return nil, false, fmt.Errorf("scan %s: %w", path, err)
	}
	return nil, false, nil
}

// lineHasStartTime 报告 line 里是否存在**恰好等于** needle（`"start_time":<N>`）的字段。
//
// ⚠️ 必须比到分隔符：不然后面那位数字会顶上来——找 1000 命中 10001，回读会返回**隔壁
// 窗口**的整条事件（曲线画的是另一场，且看不出来）。
func lineHasStartTime(line, needle []byte) bool {
	for off := 0; off < len(line); {
		i := bytes.Index(line[off:], needle)
		if i < 0 {
			return false
		}
		end := off + i + len(needle)
		if end == len(line) || line[end] == ',' || line[end] == '}' {
			return true
		}
		off = end
	}
	return false
}
