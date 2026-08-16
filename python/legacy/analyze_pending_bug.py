#!/usr/bin/env python3
"""
精确分析 Go 引擎 pendingCrossings 被过早清空的 bug。

核心场景 (delay=5):
  tick 30: YES>0.7 → enterConfirming → state=Confirming
  tick 31: NO>0.7  → pendingCrossings += [{31, NO}]
  tick 32-34: confirmCount++
  tick 35: confirmCount=5 → onConfirmed → 失败!
           → 试 pending[{31, NO}]
           → confIdx = 31+5=36, buf.len=36 → 36>=36 → nil (数据不够!)
           → pendingCrossings 清空!  ← BUG
           → returnToWatching
  tick 36: 数据到了, 但 pending 已清空, 永久丢失!

这里与 Python 批量回测对比:
  Python 遍历到 tick 31 时，snaps[36] 已经存在（完整数据）
  → 直接评分 → 可能通过！
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest_flip_config import FlipBacktestConfig
from backtest_flip_utils import load_events_from_dir, run_backtest, _score_crossing

DATA_DIR = str(Path(__file__).resolve().parent.parent / "data_0" / "lab")
events = load_events_from_dir(DATA_DIR)

for delay in [3, 4, 5]:
    cfg = FlipBacktestConfig()
    cfg.confirm_delay_ticks = delay

    # Python 批量回测 (标准，拥有完整数据)
    signals, _, _ = run_backtest(events, cfg)
    sig_set = {(s.event_time, s.side, s.remaining_sec) for s in signals}
    print(f"\n{'='*70}")
    print(f"  delay={delay}: Python 批量回测 = {len(signals)} 信号")

    # 模拟 Go 引擎: 按 snapshot 顺序处理，pending 评估时只能用已有 buffer
    go_signals = 0
    lost_by_early_clear = 0  # pending 数据不够被清空
    lost_by_window = 0       # 窗口末尾
    lost_examples = []

    for event in events:
        if event.get("hist_avg_range") is None:
            continue

        snaps = event["snapshots"]
        state = "watching"
        cross_idx = cross_side = None
        confirm_count = 0
        pending = []  # [(idx, side)]
        yes_was, no_was = False, False
        found = False

        for i, s in enumerate(snaps):
            if found:
                break

            buf_len = i + 1  # buffer 当前长度 (Go: 先 append)

            yes_above = (s["yes_price"] > 0.7 and s["remaining_sec"] < 260
                        and s["remaining_sec"] > 35)
            no_above = (s["no_price"] > 0.7 and s["remaining_sec"] < 260
                       and s["remaining_sec"] > 35)
            yes_rising = yes_above and not yes_was and i >= cfg.min_pre_snaps
            no_rising = no_above and not no_was and i >= cfg.min_pre_snaps
            yes_was, no_was = yes_above, no_above

            if state == "watching":
                if yes_rising or no_rising:
                    side = "yes" if yes_rising else "no"
                    conf_needed = i + delay
                    # Go: if data already in buffer, sync eval
                    if conf_needed < buf_len:
                        sig = _score_crossing(event, side, i, cfg)
                        if sig:
                            go_signals += 1
                            found = True
                    else:
                        # 进入 confirming，等待数据
                        state = "confirming"
                        cross_idx, cross_side = i, side
                        confirm_count = 0

            elif state == "confirming":
                if yes_rising:
                    pending.append((i, "yes"))
                if no_rising:
                    pending.append((i, "no"))

                confirm_count += 1
                if confirm_count >= delay:
                    # 评估第一个穿越
                    sig = _score_crossing(event, cross_side, cross_idx, cfg)
                    if sig:
                        go_signals += 1
                        found = True
                        break

                    # 处理 pending — 关键: 只能用 buf_len 以内的数据
                    pending_found = False
                    still_waiting = []
                    for pi, ps in pending:
                        need = pi + delay
                        if need >= buf_len:
                            # BUG: Go 的 evaluateCrossingAt 返回 nil,
                            # afterFailedConfirm 清空全部 pending
                            # 检查这个 pending 如果用完整数据能否通过
                            sig = _score_crossing(event, ps, pi, cfg)
                            if sig:
                                still_waiting.append((pi, ps, need, buf_len, sig.score))
                        else:
                            sig = _score_crossing(event, ps, pi, cfg)
                            if sig:
                                go_signals += 1
                                found = True
                                pending_found = True
                                break

                    if pending_found:
                        break

                    if still_waiting:
                        lost_by_early_clear += len(still_waiting)
                        if len(lost_examples) < 20:
                            lost_examples.append({
                                "ts": event["start_time"],
                                "first_cross": f"{cross_side}@{cross_idx}",
                                "pendings": still_waiting,
                                "total_snaps": len(snaps),
                            })

                    # Go: 清空 pending, 回到 watching
                    pending = []
                    state = "watching"
                    cross_idx = cross_side = None
                    confirm_count = 0

    print(f"  Go 引擎模拟 (流式):             {go_signals} 信号")
    print(f"  Go 丢失 (pending 提前清空):     {lost_by_early_clear}")
    print(f"  预期 Go 应有 (修复后):          {go_signals + lost_by_early_clear}")

    if lost_examples:
        print(f"\n  丢失案例 (前 10):")
        for ex in lost_examples[:10]:
            for pi, ps, need, buf_len, score in ex["pendings"][:2]:
                print(f"    ts={ex['ts']} first={ex['first_cross']} "
                      f"pending={ps}@{pi} need_data@{need} buf={buf_len} "
                      f"would_score={score} total_snaps={ex['total_snaps']}")
