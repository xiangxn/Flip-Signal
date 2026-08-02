package snapshot

import "sync"

const DefaultCapacity = 300 // 5 minutes × 60 seconds

// RingBuffer is a thread-safe circular buffer for Snapshot data.
type RingBuffer struct {
	mu       sync.RWMutex
	buf      []*Snapshot
	head     int // next write position
	size     int // current number of elements
	capacity int
}

// NewRingBuffer creates a new RingBuffer with the given capacity.
func NewRingBuffer(capacity int) *RingBuffer {
	if capacity <= 0 {
		capacity = DefaultCapacity
	}
	return &RingBuffer{
		buf:      make([]*Snapshot, capacity),
		capacity: capacity,
	}
}

// Push adds a snapshot to the buffer. O(1).
func (rb *RingBuffer) Push(s *Snapshot) {
	rb.mu.Lock()
	defer rb.mu.Unlock()

	rb.buf[rb.head] = s
	rb.head = (rb.head + 1) % rb.capacity
	if rb.size < rb.capacity {
		rb.size++
	}
}

// Last returns the most recent snapshot. O(1). Returns nil if empty.
func (rb *RingBuffer) Last() *Snapshot {
	rb.mu.RLock()
	defer rb.mu.RUnlock()

	if rb.size == 0 {
		return nil
	}
	idx := (rb.head - 1 + rb.capacity) % rb.capacity
	return rb.buf[idx]
}

// SnapshotAt returns the snapshot from N seconds ago. Returns nil if not available.
func (rb *RingBuffer) SnapshotAt(ago int) *Snapshot {
	rb.mu.RLock()
	defer rb.mu.RUnlock()

	if ago < 0 || ago >= rb.size {
		return nil
	}
	idx := (rb.head - 1 - ago + rb.capacity) % rb.capacity
	return rb.buf[idx]
}

// Window returns the most recent n snapshots in chronological order.
func (rb *RingBuffer) Window(n int) []*Snapshot {
	rb.mu.RLock()
	defer rb.mu.RUnlock()

	if n <= 0 || rb.size == 0 {
		return nil
	}
	if n > rb.size {
		n = rb.size
	}

	result := make([]*Snapshot, n)
	for i := 0; i < n; i++ {
		idx := (rb.head - n + i + rb.capacity) % rb.capacity
		result[i] = rb.buf[idx]
	}
	return result
}

// Size returns the current number of elements in the buffer.
func (rb *RingBuffer) Size() int {
	rb.mu.RLock()
	defer rb.mu.RUnlock()
	return rb.size
}

// Len is an alias for Size.
func (rb *RingBuffer) Len() int {
	return rb.Size()
}

// Clear removes all elements from the buffer.
func (rb *RingBuffer) Clear() {
	rb.mu.Lock()
	defer rb.mu.Unlock()
	rb.head = 0
	rb.size = 0
}
