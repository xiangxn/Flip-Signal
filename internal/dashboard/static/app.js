// Dog@0.2 纸面监控前端。
// 轮询: /api/state 5s（窗口/盘口/统计）、/api/signals 与 /api/observations 15s。
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };
  var stateInterval = 5000;
  var listInterval = 15000;

  // 触底判定状态的中文映射（观测表/信号表共用）
  var REJECT_CN = {
    rem_low: '时间不足',
    no_hist: 'σ 窗口不足',
    missing_spot: '现货缺失',
    missing_anchor: '锚缺失',
    no_crash: '无急跌',
    dist_out: '浅洞外'
  };

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

  // 狗侧标签: yes=UP 配色 / no=DOWN 配色（元素 class 用 yes/no 而非 up/down）
  function sideTag(side) {
    return '<span class="side-tag ' + side + '">' + side.toUpperCase() + '</span>';
  }

  // dist 显示: 0 = 输入缺失未计算（dist_s ∈ (−0.5,0) 开区间，0 不可能是有效值）
  function fmtDist(d) {
    return d ? d.toFixed(2) : '—';
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

    // 最大回撤（负数越低越深）
    var dd = $('statDd');
    dd.textContent = fmtPnl(s.max_drawdown);
    dd.classList.toggle('neg', s.max_drawdown < 0);

    // 逐日盈利（已结算天数）
    var day = $('statDay');
    day.textContent = s.day_total > 0 ? s.day_pnl_pos + '/' + s.day_total + ' 天' : '—';

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

    // spot: −1 无推送（灰）；>2s 陈旧（红，引擎已判现货缺失）；否则正常
    var spotAge = $('winSpotAge');
    if (s.spot_age_ms < 0 || s.spot_price === 0) {
      $('winSpot').textContent = '—';
      spotAge.textContent = '无推送';
      spotAge.className = 'spot-age stale';
    } else {
      $('winSpot').textContent = s.spot_price.toFixed(2);
      spotAge.textContent = s.spot_age_ms + 'ms';
      spotAge.className = 'spot-age' + (s.spot_age_ms > 2000 ? ' stale' : '');
    }

    $('foot').textContent = 'TS ' + s.ts + ' · 触底观测 ' + s.observation_count + ' 条';
  }

  // 信号行: 时间 侧 rem fill m45 dist_s 份额 结果 P&L
  function renderSignals(list) {
    var tb = document.querySelector('#signalsTable tbody');
    tb.innerHTML = '';
    $('signalsEmpty').hidden = list.length > 0;
    list.forEach(function (r) {
      var tr = document.createElement('tr');
      var pnlCls = r.pnl > 0 ? 'pos' : (r.pnl < 0 ? 'neg' : '');
      tr.innerHTML =
        '<td class="muted">' + fmtTime(r.ts) + '</td>' +
        '<td>' + sideTag(r.side) + '</td>' +
        '<td>' + r.rem + '</td>' +
        '<td>' + r.fill.toFixed(3) + '</td>' +
        '<td>' + (r.m_45 ? r.m_45.toFixed(2) : '—') + '</td>' +
        '<td>' + fmtDist(r.dist_s) + '</td>' +
        '<td>' + (r.shares ? r.shares.toFixed(1) : '—') + '</td>' +
        '<td>' + (r.won == null ? '<span class="muted">待结算</span>' : (r.won ? '<span class="won">赢</span>' : '<span class="lost">输</span>')) + '</td>' +
        '<td class="' + pnlCls + '">' + fmtPnl(r.pnl) + '</td>';
      tb.appendChild(tr);
    });
  }

  // 观测行: 时间 侧 rem fill m45 dist_s 判定
  function renderObservations(list) {
    var tb = document.querySelector('#obsTable tbody');
    tb.innerHTML = '';
    $('obsEmpty').hidden = list.length > 0;
    list.forEach(function (r) {
      var tr = document.createElement('tr');
      var status;
      if (r.ok) status = '<span class="won">信号</span>';
      else status = '<span class="muted">' + (REJECT_CN[r.reject_reason] || esc(r.reject_reason)) + '</span>';
      tr.innerHTML =
        '<td class="muted">' + fmtTime(r.ts) + '</td>' +
        '<td>' + sideTag(r.side) + '</td>' +
        '<td>' + r.rem + '</td>' +
        '<td>' + r.fill.toFixed(3) + '</td>' +
        '<td>' + (r.m_45 ? r.m_45.toFixed(2) : '—') + '</td>' +
        '<td>' + fmtDist(r.dist_s) + '</td>' +
        '<td>' + status + '</td>';
      tb.appendChild(tr);
    });
  }

  function fetchJSON(url, cb, timeoutMs) {
    // 超时兜底: 服务器卡死/断网时中止请求，避免 setInterval 堆积挂起连接
    timeoutMs = timeoutMs || 8000;
    var ctrl = new AbortController();
    var timer = setTimeout(function () {
      ctrl.abort();
      console.warn(url, 'fetch timeout');
      setLive(false);
    }, timeoutMs);
    fetch(url, { signal: ctrl.signal })
      .then(function (r) { return r.json(); })
      .then(function (d) { clearTimeout(timer); cb(d); })
      .catch(function (e) {
        if (e.name === 'AbortError') return; // 超时已单独告警
        clearTimeout(timer);
        console.warn(url, e);
        setLive(false);
      });
  }

  function tick() {
    fetchJSON('/api/state', renderState);
  }
  function tickLists() {
    fetchJSON('/api/signals', renderSignals);
    fetchJSON('/api/observations?limit=50', renderObservations);
  }

  $('btnSignals').addEventListener('click', function () { fetchJSON('/api/signals', renderSignals); });
  $('btnObs').addEventListener('click', function () { fetchJSON('/api/observations?limit=50', renderObservations); });

  setInterval(tick, stateInterval);
  setInterval(tickLists, listInterval);
  tick();
  tickLists();
})();
