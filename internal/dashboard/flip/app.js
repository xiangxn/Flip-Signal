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

  // 风控闸原因（= internal/flip Gate* 常量字面量）与结果列短标签。
  // ⚠️ 被闸行**没有仓位**: live 直接不 POST; paper（方案 A）只标记、行照常结算。
  var GATE_CN = { first_window: '重启后首窗禁单', daily_loss: '日亏熔断（锁存）' };
  var GATE_SHORT = { first_window: '闸·首窗', daily_loss: '闸·熔断' };

  // live 执行状态（= internal/flip ExecStatus* 常量）; 无仓位的那几个在结果列直接显示。
  var EXEC_CN = {
    submitting: '下单中',
    resting: '挂单中',
    unfilled: '未成交',
    rejected: '下单被拒'
  };
  // 成交结果不明（= internal/flip ExecNoteUnknown）: 仓位悬而未决, 需人工核对
  var NOTE_UNKNOWN = '未知结果';

  // 结果列短标签: 悬停给全称 + 说明
  function tag(cls, text, title) {
    return '<span class="tag ' + cls + '"' + (title ? ' title="' + esc(title) + '"' : '') + '>' + esc(text) + '</span>';
  }

  // 该行是否有仓位（决定「份额/P&L」显示成数字还是「—」）:
  //   paper 正常行 exec_status 缺省; live 只有 filled/partial 真成交;
  //   已结算行必然有仓位。被闸/被拒/未成交/挂单未定稿/成交未知 = 没有。
  function hasPosition(r) {
    if (r.won != null) return true;
    var e = r.exec_status;
    return e === undefined || e === '' || e === 'filled' || e === 'partial';
  }

  // 结果列（结算结果 / 被闸 / 下单状态 三态合一）:
  //   已结算     → 赢/输（paper 方案 A 下被闸行照常结算, 附闸标记）
  //   被闸未结算 → 「闸·熔断」等（live 不 POST, 永无仓位 ⇒ 不可能是待结算）
  //   其他       → 执行状态; 全空 = paper 正常行 → 待结算
  function resultCell(r) {
    var gate = r.gate_reason
      ? tag('gate', GATE_SHORT[r.gate_reason] || r.gate_reason,
        '被风控闸拦下: ' + (GATE_CN[r.gate_reason] || r.gate_reason) + (r.exec_note ? ' | ' + r.exec_note : ''))
      : '';
    if (r.won != null) {
      return (r.won ? '<span class="won">赢</span>' : '<span class="lost">输</span>') + gate;
    }
    if (r.gate_reason) return gate;
    if (r.exec_note && r.exec_note.indexOf(NOTE_UNKNOWN) === 0) {
      return tag('warn', '成交未知', r.exec_note + '（无仓位, 按 order_id 去 data-api 核对）');
    }
    var cn = EXEC_CN[r.exec_status];
    if (cn) {
      var warn = (r.exec_status === 'unfilled' || r.exec_status === 'rejected') ? 'warn' : '';
      return tag(warn, cn, r.exec_note);
    }
    return '<span class="muted">待结算</span>';
  }

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

  // 美元位移（dev/twap_dev，同 tail 的 dev$）: 带符号 2 位; |值| < 0.005 归零，
  // 防 toFixed 产生 "-0.00"。0/缺失 = 输入缺失（或现货恰在锚上）→ 上游判 0 显示「—」
  function fmtUsd(v) {
    if (v == null) return '—';
    if (Math.abs(v) < 0.005) return '0.00';
    return (v > 0 ? '+' : '') + v.toFixed(2);
  }

  // TWAP 差（twap − twap_open）: 带符号 2 位；0 不带符号；
  // |差| < 0.005 归零，防 toFixed 产生 "−0.00"
  function fmtTwapDiff(v) {
    if (Math.abs(v) < 0.005) return '0.00';
    return (v > 0 ? '+' : '') + v.toFixed(2);
  }

  function formatUptime(uptimeSec) {
    const sec = Math.floor(uptimeSec);
    const days = Math.floor(sec / 86400);
    const hours = Math.floor((sec % 86400) / 3600);
    const minutes = Math.floor((sec % 3600) / 60);

    const parts = [];
    if (days > 0) parts.push(`${days}D`);
    if (hours > 0) parts.push(`${hours}H`);
    if (minutes > 0 || parts.length === 0) parts.push(`${minutes}m`);

    return parts.join(' ');
}

  function renderState(s) {
    setLive(true);
    $('modeBadge').textContent = s.mode === 'live' ? '实盘' : '纸面';
    $('uptime').textContent = '运行 ' + formatUptime(s.uptime_sec);

    // 实盘执行摘要（paper 恒空 → 隐藏）
    var lb = $('liveBar');
    if (s.live) {
      lb.style.display = '';
      var l = s.live;
      var bits = ['今日实盘 成交 ' + l.today_filled + ' 笔', 'P&L ' + fmtPnl(l.today_pnl)];
      if (!l.breaker_open) bits.push('⛔ 熔断停单（≤' + fmtPnl(l.max_daily_loss) + '）');
      if (l.reconciling > 0) bits.push('⚠️ 待人工核对 ' + l.reconciling + ' 行');
      lb.textContent = bits.join(' · ');
      lb.className = 'livebar ' + (l.today_pnl < 0 ? 'neg' : l.today_pnl > 0 ? 'pos' : '');
    } else {
      lb.style.display = 'none';
    }

    // 日亏熔断（两模式都显示; paper 是"影子"——闸判据同源, 但只标记不拦 POST）
    var rb = $('riskBar');
    if (s.risk) {
      var rk = s.risk;
      rb.style.display = '';
      var rbits = ['风控 今日 ' + fmtPnl(rk.today_pnl) + ' / ' + fmtPnl(rk.max_daily_loss) + 'U'];
      if (!rk.can_trade) {
        rbits.push('⛔ 熔断停单' + (rk.gated_today > 0 ? '（今日拦 ' + rk.gated_today + ' 笔）' : '（锁存中）'));
      }
      if (!rk.enforced) rbits.push('影子（纸面只标记）');
      rb.textContent = rbits.join(' · ');
      rb.className = 'riskbar' + (rk.can_trade ? '' : ' neg');
    } else {
      rb.style.display = 'none';
    }

    $('statSignal').textContent = s.signal_count;
    $('statWon').textContent = s.won_count;
    $('statLost').textContent = s.lost_count;
    $('statPending').textContent = s.pending_count;
    $('statNoexec').textContent = s.noexec_count;
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

    $('upBid').textContent = s.up_bid > 0 ? s.up_bid.toFixed(3) : '—';
    $('upAsk').textContent = s.up_ask > 0 ? s.up_ask.toFixed(3) : '—';
    $('downBid').textContent = s.down_bid > 0 ? s.down_bid.toFixed(3) : '—';
    $('downAsk').textContent = s.down_ask > 0 ? s.down_ask.toFixed(3) : '—';
    $('winRem').textContent = s.remaining_sec;

    // 三源新鲜度: 阈值由服务端下发（limits），前端不得硬编码——调 flag 后
    // 颜色语义必须跟随实际生效值
    var lim = s.limits || {};
    var latEl = $('winLat');
    latEl.textContent = s.book_latency_ms;
    latEl.className = lim.book_lat_ms && s.book_latency_ms > lim.book_lat_ms ? 'stale' : '';

    // spot: −1 无推送（灰）；超阈陈旧（红，引擎已判现货缺失）；否则正常
    var spotAge = $('winSpotAge');
    if (s.spot_age_ms < 0 || s.spot_price === 0) {
      $('winSpot').textContent = '—';
      spotAge.textContent = '无推送';
      spotAge.className = 'src-age stale';
    } else {
      $('winSpot').textContent = s.spot_price.toFixed(2);
      spotAge.textContent = s.spot_age_ms + 'ms';
      spotAge.className = 'src-age' + (s.spot_age_ms > (lim.spot_age_ms || 2000) ? ' stale' : '');
    }

    // TWAP-60: 最新流值 + 接收龄（与 spot 同款）/ 本窗开盘值
    // 无推送（price=0，此时 age 也是 0）→ 显示「无推送」，不显示 0ms；
    // 锚缺失（窗口间、恢复中）→ twap_open 显示「—」
    var twapAge = $('winTwapAge');
    if (s.twap_price > 0) {
      $('winTwapPrice').textContent = s.twap_price.toFixed(2);
      twapAge.textContent = s.twap_age_ms + 'ms';
      twapAge.className = 'src-age' + (lim.twap_age_ms && s.twap_age_ms > lim.twap_age_ms ? ' stale' : '');
    } else {
      $('winTwapPrice').textContent = '—';
      twapAge.textContent = '无推送';
      twapAge.className = 'src-age stale';
    }
    $('winTwapOpen').textContent = s.twap_open > 0 ? s.twap_open.toFixed(2) : '—';

    // 中间列: twap − twap_open（锚到现在的位移）——只有数值，绿涨红跌；
    // 无数据（无推送或锚缺失）显示「—」。classList 只增删状态类，不覆盖结构类
    var deltaEl = $('winTwapDelta');
    var diff = (s.twap_price > 0 && s.twap_open > 0) ? s.twap_price - s.twap_open : null;
    deltaEl.textContent = diff == null ? '—' : fmtTwapDiff(diff);
    deltaEl.classList.toggle('pos', diff != null && diff > 0);
    deltaEl.classList.toggle('neg', diff != null && diff < 0);

    // 本窗 tick 健康度: 「本窗为什么没信号」的现场（延迟闸挡掉的本会触发 tick）
    var wh = $('winHealth');
    var ws = s.window_stats;
    if (!ws) {
      wh.hidden = true;
    } else {
      wh.hidden = false;
      $('whTicks').textContent = ws.ticks;
      $('whValid').textContent = ws.ticks_valid;
      $('whStale').textContent = ws.book_stale;
      $('whMissing').textContent = ws.book_missing;
      var lost = ws.lost_triggers || [];
      var lEl = $('whLost');
      if (ws.anchor_missing) {
        // 锚待精确命中: 窗口起 0~20s 内取锚通道在等「评估时刻 == 边界」的那条推送
        // （实测 p50 边界后 +2.0s 到达; 命中即转为正常判定并清标记, 取不到则本窗不产出
        // 观测）。每窗开头都会短暂出现, **不是异常**。
        lEl.textContent = '锚待精确命中（边界推送 p50 +2.0s 到达），本窗判定暂缓';
        lEl.className = 'lost warn';
      } else if (lost.length === 0) {
        lEl.textContent = '丢信号 0';
        lEl.className = 'lost';
      } else {
        var t = lost[lost.length - 1]; // 最近一笔（落盘明细见 winstats_*.jsonl）
        var why = t.reason === 'stale_book' ? ' (book ' + t.book_lat_ms + 'ms)'
          : t.reason === 'anchor_pending' ? ' (锚未就绪)' : ' (无快照)';
        lEl.textContent = '丢信号 ' + lost.length + ' 笔 · 最近 ' + t.side + ' rem=' + t.rem +
          ' ask=' + t.ask.toFixed(2) + why;
        lEl.className = 'lost warn';
      }
    }

    $('foot').textContent = 'TS ' + s.ts + ' · 触底观测 ' + s.observation_count + ' 条';
  }

  // ── 信号/观测列表（服务端分页）──
  // 50 条/页，第 1 页 = 最新。自动轮询只刷第 1 页；翻历史页后暂停该表轮询
  // （避免正在看的行被新数据顶走），回到第 1 页自动恢复。翻页时如遇数据被清
  // （重启清零），fetchList 内兜底收敛页码重拉。
  var PAGE_SIZE = 50;
  var LISTS = {
    signals: { url: '/api/signals', cardId: 'signalsCard', pagerId: 'signalsPager', infoId: 'signalsPgInfo', page: 1, pages: 1, total: 0, auto: true },
    obs: { url: '/api/observations', cardId: 'obsCard', pagerId: 'obsPager', infoId: 'obsPgInfo', page: 1, pages: 1, total: 0, auto: true }
  };

  // 信号行: 时间 侧 rem fill m45 dist_s 份额 结果 P&L
  // 份额/P&L 只对**有仓位**的行给数字: 被闸与下单失败的行没有成交, 拿目标股数冒充
  // 成交会让页面看起来像「有仓位在途」——正是用户反馈的那类混淆。
  function renderSignals(resp) {
    var tb = document.querySelector('#signalsTable tbody');
    tb.innerHTML = '';
    $('signalsEmpty').hidden = resp.items.length > 0;
    resp.items.forEach(function (r) {
      var tr = document.createElement('tr');
      var pos = hasPosition(r);
      var pnlCls = r.pnl > 0 ? 'pos' : (r.pnl < 0 ? 'neg' : '');
      tr.innerHTML =
        '<td class="muted">' + fmtTime(r.ts) + '</td>' +
        '<td>' + sideTag(r.side) + '</td>' +
        '<td>' + r.rem + '</td>' +
        '<td>' + r.fill.toFixed(3) + '</td>' +
        '<td>' + (r.m_45 ? r.m_45.toFixed(2) : '—') + '</td>' +
        '<td>' + fmtDist(r.dist_s) + '</td>' +
        '<td>' + (r.dev_usd ? fmtUsd(r.dev_usd) : '—') + '</td>' +
        '<td>' + (r.twap_dev_usd ? fmtUsd(r.twap_dev_usd) : '—') + '</td>' +
        '<td>' + (pos && r.shares ? r.shares.toFixed(1) : '—') + '</td>' +
        '<td>' + resultCell(r) + '</td>' +
        '<td class="' + (pos ? pnlCls : 'muted') + '">' + (pos ? fmtPnl(r.pnl) : '—') + '</td>';
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
      if (r.ok) {
        // 被风控闸拦下的信号在这里也标一下（信号表的完整口径见结果列）
        status = '<span class="won">信号</span>' +
          (r.gate_reason ? ' ' + tag('gate', GATE_SHORT[r.gate_reason] || r.gate_reason, GATE_CN[r.gate_reason] || r.gate_reason) : '');
      } else {
        status = '<span class="muted">' + (REJECT_CN[r.reject_reason] || esc(r.reject_reason)) + '</span>';
      }
      tr.innerHTML =
        '<td class="muted">' + fmtTime(r.ts) + '</td>' +
        '<td>' + sideTag(r.side) + '</td>' +
        '<td>' + r.rem + '</td>' +
        '<td>' + r.fill.toFixed(3) + '</td>' +
        '<td>' + (r.m_45 ? r.m_45.toFixed(2) : '—') + '</td>' +
        '<td>' + fmtDist(r.dist_s) + '</td>' +
        '<td>' + (r.dev_usd ? fmtUsd(r.dev_usd) : '—') + '</td>' +
        '<td>' + (r.twap_dev_usd ? fmtUsd(r.twap_dev_usd) : '—') + '</td>' +
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

  // ── 卡片标题栏点击折叠/展开 ──
  // 点击点 = 标题栏的 h2（撑满标题栏剩余宽度，刷新按钮不在其内）: 收起时整张贴 +
  // 分页条 + 空态一起隐藏，卡片只剩标题栏一行（两个长列表默认各占半屏，收起一个
  // 即可同屏看另一个）。状态按表名存 localStorage，刷新页面后保持; localStorage
  // 不可用（隐私模式）时静默降级为仅本次会话有效。收起期间暂停该表轮询（表体不
  // 可见），展开时立即补拉一次。
  var COLLAPSE_KEY = 'flip.collapsedTables';

  function loadCollapsed() {
    try {
      var v = JSON.parse(localStorage.getItem(COLLAPSE_KEY));
      // 非对象（含 null / 被手改坏的值）一律按展开——严格模式下往原始值上写属性会抛错
      return (v && typeof v === 'object') ? v : {};
    } catch (e) {
      return {}; /* 无 localStorage / JSON 损坏 → 全部按展开 */
    }
  }

  var collapsed = loadCollapsed();

  function saveCollapsed() {
    try { localStorage.setItem(COLLAPSE_KEY, JSON.stringify(collapsed)); }
    catch (e) { /* 隐私模式写失败: 仅本次会话有效，不影响功能 */ }
  }

  // 把状态刷到 DOM（卡片 class 驱动隐藏与箭头方向，aria-expanded 供读屏）
  function applyCollapse(name) {
    var L = LISTS[name];
    var on = !!collapsed[name];
    $(L.cardId).classList.toggle('collapsed', on);
    document.querySelector('#' + L.cardId + ' .list-toggle').setAttribute('aria-expanded', on ? 'false' : 'true');
  }

  function toggleCollapse(name) {
    collapsed[name] = !collapsed[name];
    saveCollapsed();
    applyCollapse(name);
    if (!collapsed[name]) fetchList(name); // 收起期不轮询 → 展开即补拉当前页
  }

  function bindCollapse(name) {
    var h = document.querySelector('#' + LISTS[name].cardId + ' .list-toggle');
    h.addEventListener('click', function () { toggleCollapse(name); });
    // 键盘可达: 标题 tabindex=0，Enter/空格与点击同效
    h.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        toggleCollapse(name);
      }
    });
    applyCollapse(name);
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
      '<td>' + r.noexec + '</td>' +
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
  // 自动轮询只刷第 1 页（最新）; 用户翻历史页期间暂停对应表; 收起的表不拉（展开时补拉）
  function tickLists() {
    if (LISTS.signals.auto && !collapsed.signals) fetchList('signals');
    if (LISTS.obs.auto && !collapsed.obs) fetchList('obs');
  }

  $('btnSignals').addEventListener('click', function () { fetchList('signals'); });
  $('btnObs').addEventListener('click', function () { fetchList('obs'); });
  bindPager('signals');
  bindPager('obs');
  bindCollapse('signals');
  bindCollapse('obs');

  setInterval(tick, stateInterval);
  setInterval(tickLists, listInterval);
  tick();
  tickLists();
})();
