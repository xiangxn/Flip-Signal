// Flip Signal 纸面监控前端。
// 轮询: /api/state 5s（窗口/盘口/统计）、/api/signals 与 /api/crosses 15s。
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };
  var stateInterval = 5000;
  var listInterval = 15000;

  function fmtTime(tsMs) {
    var d = new Date(tsMs);
    function p(n) { return n < 10 ? '0' + n : '' + n; }
    return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }

  function fmtPnl(v) {
    if (v == null || v === 0) return '0.00';
    var s = v > 0 ? '+' : '';
    return s + v.toFixed(2);
  }

  function esc(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function setLive(ok) {
    $('liveDot').classList.toggle('live', ok);
  }

  function renderState(s) {
    setLive(true);
    $('modeBadge').textContent = s.mode === 'live' ? '实盘' : '纸面';
    $('uptime').textContent = '运行 ' + Math.floor(s.uptime_sec / 60) + 'm';

    $('statSignal').textContent = s.signal_count;
    $('statWon').textContent = s.won_count;
    $('statLost').textContent = s.lost_count;
    $('statPending').textContent = s.pending_count;
    $('statWr').textContent = (s.win_rate * 100).toFixed(1) + '%';
    var pnl = $('statPnl');
    pnl.textContent = fmtPnl(s.cumulative_pnl);
    pnl.classList.toggle('pos', s.cumulative_pnl > 0);
    pnl.classList.toggle('neg', s.cumulative_pnl < 0);

    $('engineState').textContent = s.engine_state;
    $('engineState').className = 'engine-state ' + s.engine_state.toLowerCase();
    // 当前窗口 slug → Polymarket 事件页（点击跳转查看实时盘口）
    var slugLink = $('winSlugLink');
    if (s.slug) {
      slugLink.textContent = s.slug;
      slugLink.href = 'https://polymarket.com/zh/event/' + s.slug;
    } else {
      slugLink.textContent = '等待下一个窗口…';
      slugLink.removeAttribute('href');
    }
    $('winCond').textContent = s.condition_id ? s.condition_id.slice(0, 8) : '';

    $('upBid').textContent = s.up_bid > 0 ? s.up_bid.toFixed(3) : '—';
    $('upAsk').textContent = s.up_ask > 0 ? s.up_ask.toFixed(3) : '—';
    $('downBid').textContent = s.down_bid > 0 ? s.down_bid.toFixed(3) : '—';
    $('downAsk').textContent = s.down_ask > 0 ? s.down_ask.toFixed(3) : '—';
    $('winRem').textContent = s.remaining_sec;
    $('winLat').textContent = s.book_latency_ms;
    $('winTwap').textContent = s.twap_age_ms;

    $('foot').textContent = 'TS ' + s.ts + ' · 穿越观测 ' + s.cross_count + ' 条';
  }

  function renderSignals(list) {
    var tb = document.querySelector('#signalsTable tbody');
    tb.innerHTML = '';
    $('signalsEmpty').hidden = list.length > 0;
    list.forEach(function (r) {
      var tr = document.createElement('tr');
      var pnlCls = r.pnl > 0 ? 'pos' : (r.pnl < 0 ? 'neg' : '');
      tr.innerHTML =
        '<td class="muted">' + fmtTime(r.ts) + '</td>' +
        '<td><span class="side-tag ' + r.side + '">' + r.side.toUpperCase() + '</span></td>' +
        '<td>' + r.trigger_bid.toFixed(3) + '</td>' +
        '<td>' + r.post_end.toFixed(3) + '</td>' +
        '<td>' + r.fill.toFixed(3) + '</td>' +
        '<td>' + r.shares.toFixed(1) + '</td>' +
        '<td>' + (r.won == null ? '<span class="muted">待结算</span>' : (r.won ? '<span class="won">赢</span>' : '<span class="lost">输</span>')) + '</td>' +
        '<td class="' + pnlCls + '">' + fmtPnl(r.pnl) + '</td>';
      tb.appendChild(tr);
    });
  }

  function renderCrosses(list) {
    var tb = document.querySelector('#crossesTable tbody');
    tb.innerHTML = '';
    $('crossesEmpty').hidden = list.length > 0;
    list.forEach(function (r) {
      var tr = document.createElement('tr');
      var status;
      if (r.ok) status = '<span class="won">信号</span>';
      else if (r.reject_reason === 'missing_book') status = '<span class="muted">数据缺失</span>';
      else if (r.reject_reason === 'trigger_bid_too_low') status = '<span class="muted">C1 不足</span>';
      else if (r.reject_reason === 'post_end_too_high') status = '<span class="muted">C2 未崩</span>';
      else status = '<span class="muted">' + esc(r.reject_reason) + '</span>';
      tr.innerHTML =
        '<td class="muted">' + fmtTime(r.ts) + '</td>' +
        '<td><span class="side-tag ' + r.side + '">' + r.side.toUpperCase() + '</span></td>' +
        '<td>' + r.trigger_bid.toFixed(3) + '</td>' +
        '<td>' + r.post_end.toFixed(3) + '</td>' +
        '<td class="muted">' + esc(r.cls) + '</td>' +
        '<td>' + status + '</td>';
      tb.appendChild(tr);
    });
  }

  function fetchJSON(url, cb) {
    fetch(url).then(function (r) { return r.json(); }).then(cb)
      .catch(function (e) { console.warn(url, e); setLive(false); });
  }

  function tick() {
    fetchJSON('/api/state', renderState);
  }
  function tickLists() {
    fetchJSON('/api/signals', renderSignals);
    fetchJSON('/api/crosses?limit=50', renderCrosses);
  }

  $('btnSignals').addEventListener('click', function () { fetchJSON('/api/signals', renderSignals); });
  $('btnCrosses').addEventListener('click', function () { fetchJSON('/api/crosses?limit=50', renderCrosses); });

  setInterval(tick, stateInterval);
  setInterval(tickLists, listInterval);
  tick();
  tickLists();
})();
