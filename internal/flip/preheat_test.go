package flip

// RecentBlock 纯函数测试: 从窗口振幅日志截最新一段连续块（断档/缺窗容忍）。
// 时间单位毫秒: 正常窗间隔 300s=300000ms, 缺一窗 600000ms 仍连续, >600000ms 断档。

import (
	"reflect"
	"testing"
)

func ws(tsMs ...int64) []WindowEntry {
	out := make([]WindowEntry, 0, len(tsMs))
	for _, ts := range tsMs {
		out = append(out, WindowEntry{Ts: ts})
	}
	return out
}

func TestRecentBlock(t *testing.T) {
	gap := int64(2 * 300 * 1000) // 600s
	cases := []struct {
		name string
		in   []WindowEntry
		want []int64 // 期望保留条目的 ts
	}{
		{name: "空列表", in: nil, want: nil},
		{name: "单条", in: ws(1000), want: []int64{1000}},
		{name: "全连续", in: ws(1000, 300100, 600100), want: []int64{1000, 300100, 600100}},
		{name: "缺一窗仍连续(600s 缺口)", in: ws(1000, 600100), want: []int64{1000, 600100}},
		{name: "断档截尾", in: ws(1000, 300100, 1_800_100, 2_100_100), want: []int64{1_800_100, 2_100_100}},
		{name: "断档后仅一条", in: ws(1000, 300100, 1_200_100), want: []int64{1_200_100}},
		{name: "两处各缺一窗仍连续", in: ws(1000, 300100, 900_100), want: []int64{1000, 300100, 900_100}},
		{name: "断档后续段含缺窗", in: ws(1000, 1_500_100, 1_800_100, 2_400_100), want: []int64{1_500_100, 1_800_100, 2_400_100}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := RecentBlock(c.in, gap)
			var gotTs []int64
			for _, e := range got {
				gotTs = append(gotTs, e.Ts)
			}
			if !reflect.DeepEqual(gotTs, c.want) {
				t.Fatalf("RecentBlock = %v, 期望保留 %v", gotTs, c.want)
			}
		})
	}
}
