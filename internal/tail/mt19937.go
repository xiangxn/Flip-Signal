package tail

// CPython `random.Random` 的 Mersenne Twister 复刻（只含 bootstrap 用得到的部分）。
//
// 为什么要有这个文件: 纸面判决用的日级 bootstrap（docs/tail_sweep_2026-09-22.md §5.2）
// 的参考实现在 python/v4/13_tail_sweep.py:424 `boot_days`, 用 `random.Random(42)`
// 抽日期。Go 的 math/rand 是另一套算法（Additive Lagged Fibonacci / PCG）, 拿它算
// 出的 95% 区间与 python 差一个蒙特卡罗噪声量级——判决线附近足以让「通过 / 不显著」
// 翻面。故这里按 CPython `Modules/_randommodule.c` 的口径逐位复刻:
//
//	init_by_array(小端 32 位字数组) → genrand_uint32 → getrandbits(k) → randrange(n)
//
// 黄金向量（venv python 3.13.2 生成）钉在 judge_test.go 里; 改本文件任何一处都会
// 让那个测试失败——这正是它存在的意义（两套实现必须给出同一个 CI）。
//
// 未实现: random()/shuffle/负数 seed 的按位取反分支（都用不到; seed 恒为 42）。

const (
	mtN         = 624
	mtM         = 397
	mtMatrixA   = 0x9908b0df
	mtUpperMask = 0x80000000
	mtLowerMask = 0x7fffffff
)

// mt19937 是 Mersenne Twister 状态机（state + index, 与 CPython RandomObject 同构）。
type mt19937 struct {
	state [mtN]uint32
	index int
}

// newMT19937 按 `random.Random(seed)` 的口径构造（seed ≥ 0）。
// CPython 把非负整数 seed 拆成**小端** 32 位字数组再交给 init_by_array;
// seed=0 时字数组为 [0]（bits==0 的守卫等价于至少一个词）。
func newMT19937(seed int64) *mt19937 {
	var key []uint32
	for v := uint64(seed); ; {
		key = append(key, uint32(v))
		v >>= 32
		if v == 0 {
			break
		}
	}
	m := &mt19937{}
	m.initByArray(key)
	return m
}

// initGenrand 对应 CPython init_genrand（以单个 32 位种子初始化）。
func (m *mt19937) initGenrand(s uint32) {
	m.state[0] = s
	for i := 1; i < mtN; i++ {
		m.state[i] = 1812433253*(m.state[i-1]^(m.state[i-1]>>30)) + uint32(i)
	}
	m.index = mtN
}

// initByArray 对应 CPython init_by_array（字数组种子, seed(int) 走的就是这条）。
func (m *mt19937) initByArray(key []uint32) {
	m.initGenrand(19650218)

	i, j := 1, 0
	k := mtN
	if len(key) > k {
		k = len(key)
	}
	for ; k > 0; k-- {
		m.state[i] = (m.state[i] ^ ((m.state[i-1] ^ (m.state[i-1] >> 30)) * 1664525)) +
			key[j] + uint32(j)
		i++
		j++
		if i >= mtN {
			m.state[0] = m.state[mtN-1]
			i = 1
		}
		if j >= len(key) {
			j = 0
		}
	}
	for k = mtN - 1; k > 0; k-- {
		m.state[i] = (m.state[i] ^ ((m.state[i-1] ^ (m.state[i-1] >> 30)) * 1566083941)) - uint32(i)
		i++
		if i >= mtN {
			m.state[0] = m.state[mtN-1]
			i = 1
		}
	}
	m.state[0] = 0x80000000
}

// nextUint32 对应 CPython genrand_uint32（含 624 个一组的 twisting）。
func (m *mt19937) nextUint32() uint32 {
	if m.index >= mtN {
		var y uint32
		kk := 0
		for ; kk < mtN-mtM; kk++ {
			y = (m.state[kk] & mtUpperMask) | (m.state[kk+1] & mtLowerMask)
			m.state[kk] = m.state[kk+mtM] ^ (y >> 1) ^ mtMag01(y)
		}
		for ; kk < mtN-1; kk++ {
			y = (m.state[kk] & mtUpperMask) | (m.state[kk+1] & mtLowerMask)
			m.state[kk] = m.state[kk+(mtM-mtN)] ^ (y >> 1) ^ mtMag01(y)
		}
		y = (m.state[mtN-1] & mtUpperMask) | (m.state[0] & mtLowerMask)
		m.state[mtN-1] = m.state[mtM-1] ^ (y >> 1) ^ mtMag01(y)
		m.index = 0
	}

	y := m.state[m.index]
	m.index++
	y ^= y >> 11
	y ^= (y << 7) & 0x9d2c5680
	y ^= (y << 15) & 0xefc60000
	y ^= y >> 18
	return y
}

// mtMag01 对应 CPython 的 mag01[y & 1]。
func mtMag01(y uint32) uint32 {
	if y&1 == 1 {
		return mtMatrixA
	}
	return 0
}

// getrandbits 对应 CPython random_getrandbits 的快路径（k ≤ 32）:
// 单次 32 位抽取后**逻辑右移** 32−k 位。
func (m *mt19937) getrandbits(k uint) uint32 {
	return m.nextUint32() >> (32 - k)
}

// randrange 对应 Python `random.Random.randrange(n)`（n ≥ 1）:
// k = n.bit_length(), 反复 getrandbits(k) 直到 < n（拒绝采样, 被拒的那次同样消耗
// 随机数——顺序因此与 python 严格一致）。
func (m *mt19937) randrange(n int) int {
	if n <= 0 {
		return 0 // Python 会抛 ValueError; 本包只用于「抽日期」, n ≥ 1 恒成立
	}
	k := bitsLen(uint32(n))
	r := m.getrandbits(k)
	for r >= uint32(n) {
		r = m.getrandbits(k)
	}
	return int(r)
}

// bitsLen 是 Python int.bit_length() 的 uint32 版本（n=0 → 0, n=14 → 4）。
func bitsLen(n uint32) uint {
	k := uint(0)
	for n > 0 {
		n >>= 1
		k++
	}
	return k
}
