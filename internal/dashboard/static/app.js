// ── Flip Signal Detection Dashboard ──

const STATE_CLASSES = {
  'Idle': 'state-idle',
  'Watching': 'state-watching',
  'Confirming': 'state-confirming',
  'Done': 'state-done',
};

// ── Chart ──
const ctx = document.getElementById('priceChart').getContext('2d');
const chart = new Chart(ctx, {
  type: 'line',
  data: {
    labels: [],
    datasets: [
      {
        label: 'Price',
        data: [],
        borderColor: '#58a6ff',
        backgroundColor: 'rgba(88,166,255,0.05)',
        yAxisID: 'y-btc',
        pointRadius: 0,
        borderWidth: 1.5,
        tension: 0.1,
      },
      {
        label: 'YES',
        data: [],
        borderColor: '#3fb950',
        yAxisID: 'y-pm',
        pointRadius: 0,
        borderWidth: 1.2,
        tension: 0.1,
      },
      {
        label: 'NO',
        data: [],
        borderColor: '#f85149',
        yAxisID: 'y-pm',
        pointRadius: 0,
        borderWidth: 1.2,
        tension: 0.1,
      },
      {
        label: 'Trigger 0.7',
        data: [],
        borderColor: 'rgba(210,153,29,0.6)',
        yAxisID: 'y-pm',
        pointRadius: 0,
        borderWidth: 0.8,
        borderDash: [5, 5],
        tension: 0,
      },
      {
        label: 'Open',
        data: [],
        borderColor: '#d2991d',
        yAxisID: 'y-btc',
        pointRadius: 0,
        borderWidth: 1,
        borderDash: [4, 4],
        tension: 0,
      },
    ],
  },
  options: {
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: 200 },
    interaction: { intersect: false, mode: 'index' },
    plugins: {
      legend: {
        labels: { color: '#8b949e', usePointStyle: true, boxWidth: 8, padding: 16, font: { size: 10 } },
      },
    },
    scales: {
      x: {
        display: true,
        ticks: { color: '#484f58', font: { size: 9 }, maxTicksLimit: 12 },
        grid: { color: 'rgba(48,54,61,0.5)' },
      },
      'y-btc': {
        type: 'linear',
        position: 'left',
        ticks: { color: '#58a6ff', font: { size: 9 }, callback: v => '$' + v.toFixed(0) },
        grid: { color: 'rgba(48,54,61,0.3)' },
      },
      'y-pm': {
        type: 'linear',
        position: 'right',
        min: 0,
        max: 1,
        ticks: { color: '#8b949e', font: { size: 9 }, callback: v => v.toFixed(2) },
        grid: { display: false },
      },
    },
  },
});

let lastSnapCount = 0;

function updateChart(snapshots) {
  if (!snapshots || snapshots.length === lastSnapCount) return;
  lastSnapCount = snapshots.length;

  const labels = snapshots.map(s => {
    const d = new Date(s.ts);
    return d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  });
  chart.data.labels = labels;
  chart.data.datasets[0].data = snapshots.map(s => s.price);
  chart.data.datasets[1].data = snapshots.map(s => s.yes_price);
  chart.data.datasets[2].data = snapshots.map(s => s.no_price);
  chart.data.datasets[3].data = snapshots.map(() => 0.7);
  chart.data.datasets[4].data = snapshots.map(s => s.open);
  chart.update('none');
}

// ── Data fetching ──

let refreshSec = 5;
let scoreEntryCfg = 5; // 从 /api/state 同步的 Score 入场阈值，用于前端着色

async function fetchState() {
  try {
    const r = await fetch('/api/state');
    if (!r.ok) return;
    const d = await r.json();

    // Mode badge
    const mb = document.getElementById('mode-badge');
    mb.textContent = d.mode === 'live' ? 'LIVE' : 'PAPER';
    mb.className = 'mode-badge ' + (d.mode === 'live' ? 'mode-live' : 'mode-paper');

    document.getElementById('gen').textContent = d.generation;
    document.getElementById('clock').textContent = new Date(d.ts).toLocaleTimeString();
    document.getElementById('refresh-cnt').textContent = refreshSec;
    scoreEntryCfg = d.score_entry_cfg; // 同步 Score 阈值，供信号表着色使用

    // Stats
    document.getElementById('stat-total').textContent = d.signal_count;
    document.getElementById('stat-won').textContent = d.won_count;
    document.getElementById('stat-lost').textContent = d.lost_count;
    document.getElementById('stat-failed').textContent = d.failed_count ?? 0;
    document.getElementById('stat-wr').textContent = (d.win_rate * 100).toFixed(1) + '%';
    document.getElementById('stat-pending').textContent = d.pending_count;

    // PnL 来自 FlipRecorder（已通过 UpdateExecution 同步了纸面/实盘的成交数据）
    const pnl = d.cumulative_pnl;
    const pnlEl = document.getElementById('stat-pnl');
    pnlEl.textContent = (pnl >= 0 ? '+' : '') + pnl.toFixed(4);
    pnlEl.className = 'value ' + (pnl >= 0 ? 'val-green' : 'val-red');

    // Engine state
    const badge = document.getElementById('eng-state');
    badge.textContent = d.engine_state_label;
    badge.className = 'state-badge ' + (STATE_CLASSES[d.engine_state_label] || 'state-idle');

    const multixEl = document.getElementById('eng-multix');
    multixEl.textContent = d.allow_retry_crossings ? 'ON' : 'OFF';
    multixEl.className = d.allow_retry_crossings ? 'val-green' : '';

    document.getElementById('eng-retries').textContent = d.retry_count || 0;
    document.getElementById('eng-snaps').textContent = d.snap_count;
    document.getElementById('eng-remaining').textContent = d.remaining_sec + 's';
    document.getElementById('eng-side').textContent = d.current_side || '-';
    document.getElementById('eng-hrange').textContent =
      d.hist_ready ? '$' + d.hist_avg_range.toFixed(2) + ' (ready)' : '$' + d.hist_avg_range.toFixed(2) + ' (warming)';
    const cidEl = document.getElementById('eng-cid');
    cidEl.textContent = d.condition_id || '-';
    cidEl.title = 'Tap to copy';

    // Prices
    document.getElementById('price-btc').textContent = '$' + d.current_price.toFixed(2);
    document.getElementById('price-open').textContent = '$' + d.open_price.toFixed(2);
    document.getElementById('price-yes').textContent = d.yes_price.toFixed(4);
    document.getElementById('price-no').textContent = d.no_price.toFixed(4);
    document.getElementById('price-vol').textContent =
      d.buy_vol_5s.toFixed(2) + ' / ' + d.sell_vol_5s.toFixed(2);
    document.getElementById('price-depth').textContent =
      d.bid_depth.toFixed(1) + ' / ' + d.ask_depth.toFixed(1);

    // Cross features (active confirming)
    const cp = document.getElementById('cross-panel');
    const cg = document.getElementById('cross-grid');
    if (d.cross_features) {
      cp.style.display = '';
      const f = d.cross_features;
      const items = [
        ['Side', f.side],
        ['Path Eff', f.path_eff.toFixed(3)],
        ['Noise Ratio', f.noise_ratio.toFixed(2)],
        ['Flips', f.flips],
        ['Oscillating', f.is_oscillating ? '✓' : '✗'],
        ['Range Exp.', f.range_expansion.toFixed(2)],
        ['BTC Position', f.btc_position.toFixed(2)],
        ['BTC Extreme', f.btc_extreme ? '✓' : '✗'],
        ['Confirm Ticks', f.confirm_ticks_waited],
        ['Latency', (f.order_book_latency || 0) + 'ms'],
      ];
      cg.innerHTML = items.map(([l, v]) => {
        let cls = '';
        if (typeof v === 'boolean' || v === '✓' || v === '✗')
          cls = v === true || v === '✓' ? 'bool-true' : 'bool-false';
        return `<div class="cross-item"><div class="label">${l}</div><div class="value ${cls}">${v}</div></div>`;
      }).join('');
    } else {
      cp.style.display = 'none';
    }

    // Last failed cross features (multi-crossing retry diagnostics)
    const fp = document.getElementById('failed-panel');
    const fg = document.getElementById('failed-grid');
    if (d.last_failed_features) {
      fp.style.display = '';
      const f = d.last_failed_features;

      // 特征评估辅助函数：返回 true 表示该特征未通过（应加红边框）
      const isFail = (label, rawVal) => {
        switch (label) {
          case 'Path Eff':     return rawVal < d.path_eff_veto_min_cfg;
          case 'Noise Ratio':  return rawVal > d.noise_ratio_veto_max_cfg;
          case 'Oscillating':  return !rawVal;
          case 'Range Exp.':   return rawVal >= d.range_exp_threshold_cfg;
          case 'BTC Extreme':  return !rawVal;
          case 'Other Delta':  return rawVal <= d.other_delta_weak_cfg;
          case 'Entry Price':  return rawVal >= d.entry_cheap_weak_cfg;
          case 'Score':        return rawVal < d.score_entry_cfg;
          case 'Latency':      return d.max_latency_ms_cfg > 0 && rawVal > d.max_latency_ms_cfg;
          default:             return false;
        }
      };

      // [label, displayValue, rawValueForEval, evaluable]
      const items = [
        ['Side',          f.side,                              f.side,                     false],
        ['Path Eff',      f.path_eff.toFixed(3),               f.path_eff,                 true],
        ['Noise Ratio',   f.noise_ratio.toFixed(2),            f.noise_ratio,              true],
        ['Oscillating',   f.is_oscillating ? '✓' : '✗',       f.is_oscillating,           true],
        ['Range Exp.',    f.range_expansion.toFixed(2),        f.range_expansion,          true],
        ['BTC Position',  f.btc_position.toFixed(2),           f.btc_position,             false],
        ['BTC Extreme',   f.btc_extreme ? '✓' : '✗',          f.btc_extreme,              true],
        ['Latency',       (f.order_book_latency || 0) + 'ms',  f.order_book_latency || 0,  true],
      ];

      // 附加评分阶段特征（仅当 onConfirmed 已执行时存在）
      if (f.other_delta !== undefined && f.other_delta !== 0) {
        items.push(['Other Delta', f.other_delta.toFixed(3),   f.other_delta,              true]);
      }
      if (f.entry_price !== undefined && f.entry_price !== 0) {
        items.push(['Entry Price', f.entry_price.toFixed(3),   f.entry_price,              true]);
      }
      if (f.score !== undefined) {
        items.push(['Score',       f.score + '/' + scoreEntryCfg, f.score,                 true]);
      }

      fg.innerHTML = items.map(([l, v, raw, evaluable]) => {
        let cls = '';
        if (typeof v === 'boolean' || v === '✓' || v === '✗')
          cls = v === true || v === '✓' ? 'bool-true' : 'bool-false';
        const failed = evaluable && isFail(l, raw);
        const itemCls = 'cross-item' + (failed ? ' feature-fail' : '');
        return `<div class="${itemCls}"><div class="label">${l}</div><div class="value ${cls}">${v}</div></div>`;
      }).join('');
    } else {
      fp.style.display = 'none';
    }
  } catch (e) {
    console.error('state fetch:', e);
  }
}

async function fetchSnapshots() {
  try {
    const r = await fetch('/api/snapshots');
    if (!r.ok) return;
    const d = await r.json();
    updateChart(d.snapshots);
  } catch (e) {
    console.error('snapshots fetch:', e);
  }
}

async function fetchSignals() {
  try {
    const r = await fetch('/api/signals?limit=30');
    if (!r.ok) return;
    const d = await r.json();
    const tb = document.getElementById('sig-body');

    if (!d.signals || d.signals.length === 0) {
      tb.innerHTML = '<tr><td colspan="15" class="empty-state">No signals yet</td></tr>';
      return;
    }

    tb.innerHTML = d.signals.map(s => {
      const t = new Date(s.time).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      let wl = '<span class="pending">-</span>';
      let pnl = '-';
      let pnlCls = '';
      if (s.resolved_at) {
        wl = s.won ? '<span class="won">WIN</span>' : '<span class="lost">LOSS</span>';
        const v = s.pnl || 0;
        pnl = (v >= 0 ? '+' : '') + v.toFixed(4);
        pnlCls = v >= 0 ? 'won' : 'lost';
      }
      const sideCls = s.side === 'yes' ? 'side-yes' : 'side-no';
      let execHtml = '<span class="pending">&hellip;</span>';
      if (s.exec_status === 'filled') execHtml = '<span class="won">&#10003;</span>';
      else if (s.exec_status === 'failed') execHtml = '<span class="lost">&#10007;</span>';
      const latMs = s.order_book_latency || 0;
      const scoreCls = s.score < scoreEntryCfg ? 'score-low' : '';
      return `<tr>
        <td>${t}</td>
        <td class="${sideCls}">${s.side === 'yes' ? 'DOWN' : 'UP'}</td>
        <td class="${scoreCls}">${s.score}</td>
        <td>${s.exec_status === 'filled' ? s.avg_fill_price.toFixed(3) : s.entry_price.toFixed(3)}</td>
        <td>${(s.shares || 0).toFixed(2)}</td>
        <td>${execHtml}</td>
        <td>${s.path_eff?.toFixed(2) || '-'}</td>
        <td>${s.noise_ratio?.toFixed(2) || '-'}</td>
        <td>${s.is_oscillating ? '✓' : '-'}</td>
        <td>${s.range_expansion?.toFixed(2) || '-'}</td>
        <td>${s.btc_position?.toFixed(2) || '-'}</td>
        <td>${s.other_delta?.toFixed(3) || '-'}</td>
        <td>${latMs}ms</td>
        <td>${wl}</td>
        <td class="${pnlCls}">${pnl}</td>
      </tr>`;
    }).join('');
  } catch (e) {
    console.error('signals fetch:', e);
  }
}

// ── Toast helper ──
let toastTimer = 0;
function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 1500);
}

// ── Tap-to-copy Condition ID ──
document.getElementById('eng-cid').addEventListener('click', async () => {
  const el = document.getElementById('eng-cid');
  const text = el.textContent;
  if (!text || text === '-') return;
  try {
    await navigator.clipboard.writeText(text);
    el.classList.add('copied');
    el.title = 'Copied!';
    showToast('✓ Condition ID copied');
    setTimeout(() => { el.classList.remove('copied'); el.title = 'Tap to copy'; }, 1500);
  } catch (_) {
    // clipboard not available, ignore
  }
});

// ── Polling ──

function pollAll() {
  Promise.all([fetchState(), fetchSnapshots(), fetchSignals()]);
}

pollAll();
setInterval(pollAll, refreshSec*1000);
