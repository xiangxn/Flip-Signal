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

  // ── 信号/观测列表（服务端分页）──
  // 50 条/页，第 1 页 = 最新。自动轮询只刷第 1 页；翻历史页后暂停该表轮询
  // （避免正在看的行被新数据顶走），回到第 1 页自动恢复。翻页时如遇数据被清
  // （重启清零），fetchList 内兜底收敛页码重拉。
  var PAGE_SIZE = 50;
  var LISTS = {
    signals: { url: '/api/signals', pagerId: 'signalsPager', infoId: 'signalsPgInfo', page: 1, pages: 1, total: 0, auto: true },
    obs: { url: '/api/observations', pagerId: 'obsPager', infoId: 'obsPgInfo', page: 1, pages: 1, total: 0, auto: true }
  };

  // 信号行: 时间 侧 rem fill m45 dist_s 份额 结果 P&L
  function renderSignals(resp) {
    var tb = document.querySelector('#signalsTable tbody');
    tb.innerHTML = '';
    $('signalsEmpty').hidden = resp.items.length > 0;
    resp.items.forEach(function (r) {
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
  function renderObservations(resp) {
    var tb = document.querySelector('#obsTable tbody');
    tb.innerHTML = '';
    $('obsEmpty').hidden = resp.items.length > 0;
    resp.items.forEach(function (r) {
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

  // 拉取指定列表当前页，刷新表格与分页条
  function fetchList(name) {
    var L = LISTS[name];
    fetchJSON(L.url + '?page=' + L.page + '&limit=' + PAGE_SIZE, function (resp) {
      var pages = resp.total > 0 ? Math.ceil(resp.total / PAGE_SIZE) : 1;
      L.total = resp.total;
      if (L.page > pages) { // 重启清零等兜底: 页码收敛到末页后重拉
        L.page = pages;
        fetchList(name);
        return;
      }
      L.pages = pages;
      if (name === 'signals') renderSignals(resp); else renderObservations(resp);
      updatePager(name);
    });
  }

  // 分页条: 页码/总数文案 + 首末页/上下页禁用态（total=0 时整条隐藏）
  function updatePager(name) {
    var L = LISTS[name];
    var pager = $(L.pagerId);
    pager.hidden = L.total === 0;
    var paused = L.auto ? '' : ' · 暂停自动刷新';
    $(L.infoId).textContent = '第 ' + L.page + '/' + L.pages + ' 页 · 共 ' + L.total + ' 条' + paused;
    var btns = pager.querySelectorAll('button.pg');
    for (var i = 0; i < btns.length; i++) {
      var a = btns[i].getAttribute('data-act');
      var atEnd = (a === 'first' || a === 'prev') ? L.page <= 1 : L.page >= L.pages;
      btns[i].disabled = atEnd;
    }
  }

  // 翻页; 目标非第 1 页时暂停该表自动轮询，回第 1 页恢复
  function gotoPage(name, act) {
    var L = LISTS[name];
    var p = L.page;
    if (act === 'first') p = 1;
    else if (act === 'prev') p = Math.max(1, p - 1);
    else if (act === 'next') p = Math.min(L.pages, p + 1);
    else if (act === 'last') p = L.pages;
    if (p === L.page) return;
    L.page = p;
    L.auto = p === 1;
    fetchList(name);
  }

  function bindPager(name) {
    var btns = $(LISTS[name].pagerId).querySelectorAll('button.pg');
    for (var i = 0; i < btns.length; i++) {
      btns[i].addEventListener('click', gotoPage.bind(null, name, btns[i].getAttribute('data-act')));
    }
  }

  // 逐日盈利弹窗（/api/daily，UTC 日聚合）— 日行进 tbody，合计行放 tfoot 吸底
  function dailyRowHtml(r, isTotal) {
    var wr = (r.won + r.lost) > 0 ? (r.win_rate * 100).toFixed(1) + '%' : '—';
    var pnlCls = r.pnl > 0 ? 'pos' : (r.pnl < 0 ? 'neg' : '');
    var tag = isTotal ? '合计' : r.date;
    return '<tr>' +
      '<td class="' + (isTotal ? 'muted' : '') + '">' + tag + '</td>' +
      '<td>' + r.obs + '</td>' +
      '<td>' + r.signals + '</td>' +
      '<td>' + r.pending + '</td>' +
      '<td>' + wr + '</td>' +
      '<td class="' + pnlCls + '">' + fmtPnl(r.pnl) + '</td></tr>';
  }

  function renderDaily(d) {
    var tb = document.querySelector('#dailyTable tbody');
    var tf = document.querySelector('#dailyTable tfoot');
    tb.innerHTML = '';
    tf.innerHTML = '';
    $('dailyEmpty').hidden = d.days.length > 0;
    // 倒序渲染（最新日在顶，打开即见今天，无需滚到底）
    d.days.slice().reverse().forEach(function (r) { tb.insertAdjacentHTML('beforeend', dailyRowHtml(r, false)); });
    // 合计吸底：仅在有数据时填 tfoot（CSS 负责 sticky bottom）
    if (d.days.length > 0) tf.insertAdjacentHTML('beforeend', dailyRowHtml(d.total, true));
  }

  function openDaily() {
    $('dailyMask').hidden = false;
    fetchJSON('/api/daily', renderDaily);
  }
  function closeDaily() {
    $('dailyMask').hidden = true;
  }
  $('statDayCard').addEventListener('click', openDaily);
  $('dailyClose').addEventListener('click', closeDaily);
  // 点击遮罩空白处关闭
  $('dailyMask').addEventListener('click', function (e) {
    if (e.target === this) closeDaily();
  });
  // Esc 关闭
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeDaily();
  });

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
  // 自动轮询只刷第 1 页（最新）; 用户翻历史页期间暂停对应表
  function tickLists() {
    if (LISTS.signals.auto) fetchList('signals');
    if (LISTS.obs.auto) fetchList('obs');
  }

  $('btnSignals').addEventListener('click', function () { fetchList('signals'); });
  $('btnObs').addEventListener('click', function () { fetchList('obs'); });
  bindPager('signals');
  bindPager('obs');

  setInterval(tick, stateInterval);
  setInterval(tickLists, listInterval);
  tick();
  tickLists();
})();
