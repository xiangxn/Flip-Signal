package tail

import "testing"

// 黄金向量由 venv python 3.13.2 生成（`random.Random(42)`）:
//
//	[p.getrandbits(32) for _ in range(8)]
//	[p.randrange(14) for _ in range(48)]
//
// 这些数字是「Go 判决 == python 判决」的唯一证据链——任何一处状态机/掩码/移位写错
// 都会在这里暴露（MT19937 是逐位敏感的, 差一位后面全散）。
var (
	mtGoldenBits32 = []uint32{
		2746317213, 478163327, 107420369, 3184935163, 1181241943, 1051802512, 958682846, 599310825,
	}
	mtGoldenRandrange14 = []int{
		10, 1, 0, 11, 4, 3, 3, 2, 11, 1, 10, 11, 8, 1, 9, 6, 0, 0, 1, 3, 3, 8, 9, 0,
		8, 3, 11, 10, 11, 8, 6, 3, 7, 9, 4, 12, 13, 0, 12, 12, 2, 11, 6, 5, 4, 2, 3, 12,
	}
	mtGoldenSeed0Bits32 = []uint32{3626764237, 1654615998, 3255389356, 3823568514}
	mtGoldenRandrange7  = []int{5, 0, 0, 5, 2, 1, 1, 1, 5, 0, 5, 5}
)

// TestMT19937GoldenVector 钉住 Mersenne Twister 的原始输出（getrandbits(32) = 未截断的
// 单次抽取, 覆盖 twisting 与 tempering 全链路）。
func TestMT19937GoldenVector(t *testing.T) {
	m := newMT19937(42)
	for i, want := range mtGoldenBits32 {
		if got := m.getrandbits(32); got != want {
			t.Fatalf("seed=42 getrandbits(32) 第 %d 个: got %d, want %d", i, got, want)
		}
	}

	m0 := newMT19937(0)
	for i, want := range mtGoldenSeed0Bits32 {
		if got := m0.getrandbits(32); got != want {
			t.Fatalf("seed=0 getrandbits(32) 第 %d 个: got %d, want %d", i, got, want)
		}
	}
}

// TestMT19937Randrange 钉住拒绝采样路径（randrange(14) 的 k=4: 抽到 14/15 要重抽,
// 被拒的那次同样消耗随机数——序列因此才能与 python 对齐）。
func TestMT19937Randrange(t *testing.T) {
	m := newMT19937(42)
	for i, want := range mtGoldenRandrange14 {
		if got := m.randrange(14); got != want {
			t.Fatalf("seed=42 randrange(14) 第 %d 个: got %d, want %d", i, got, want)
		}
	}

	m7 := newMT19937(42)
	for i, want := range mtGoldenRandrange7 {
		if got := m7.randrange(7); got != want {
			t.Fatalf("seed=42 randrange(7) 第 %d 个: got %d, want %d", i, got, want)
		}
	}
}

// TestBitsLen 对 Python int.bit_length() 的少量固定点。
func TestBitsLen(t *testing.T) {
	for _, tc := range []struct {
		n    uint32
		want uint
	}{{0, 0}, {1, 1}, {2, 2}, {7, 3}, {8, 4}, {14, 4}, {15, 4}, {16, 5}} {
		if got := bitsLen(tc.n); got != tc.want {
			t.Errorf("bitsLen(%d) = %d, want %d", tc.n, got, tc.want)
		}
	}
}
