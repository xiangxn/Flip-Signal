// 扫尾盘 ⑤ 纸面监控前端。
// 轮询: /api/state 5s（窗口/盘口/统计）、/api/signals + /api/snaps 15s。
// 阈值一律由服务端下发（/api/state 的 limits、/api/config），前端不得硬编码
// ——调参后颜色语义与文案必须跟随实际生效值。
//
// ⚠️ 2026-09-24 起判决速览/五格对照/监听对账/原始帧四块全部下线（判决机器与两个
// legacy 行类型一并删除, 见 docs/tail_integrated_2026-09-24.md §4）；纸面判决改由
// 离线脚本 python/v4/23_tail_integrated.py 做。
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };
  var stateInterval = 5000;
  var listInterval = 15000;

  // 判定失败原因的中文映射（决策表用）
  var REJECT_CN = {
    missing_spot: '现货缺失',
    missing_twap: 'TWAP 缺失', // legacy 旧行（新口径不再产出）
    no_hist: 'σ 窗口不足',
    price_low: '价格腿不过',
    leg_out: '两腿都不过'
  };
  // 整窗跳过原因（tailstats_*.jsonl 的 skip 字段 = cmd/tail 主循环里的字面量）
  var SKIP_CN = {
    no_market: '无市场',
    no_token: '无 token',
    dup_record: '重复记录',
    no_sigma: 'σ 未就绪'
  };
  // 风控闸原因（= internal/tail Gate* 常量字面量）
  var GATE_CN = {
    daily_loss: '日亏熔断',
    first_window: '首窗禁单'
  };
  var GATE_SHORT = { first_window: '闸·首窗', daily_loss: '闸·熔断' };
  // live 执行状态（= internal/flip ExecStatus* 常量）
  var EXEC_CN = {
    submitting: '下单中',
    resting: '挂单中',
    filled: '成交',
    partial: '部分成交',
    unfilled: '未成交',
    rejected: '下单被拒'
  };
  // 成交结果不明（= internal/flip ExecNoteUnknown）: 仓位悬而未决, 需人工核对
  var NOTE_UNKNOWN = '未知结果';
  // 判定段（= internal/tail Stage* 常量; 空 = legacy 旧行）
  var STAGE_CN = { t150: 'T150 段', t60: 'T60 段', listen: '监听段' };

  var CFG = null; // /api/config（标定参数，一次拉取）

  function fmtTime(tsMs) {
    var d = new Date(tsMs);
    function p(n) { return n < 10 ? '0' + n : '' + n; }
    return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }

  function fmtPnl(v) {
    if (v == null || v === 0) return '0.00';
    return (v > 0 ? '+' : '') + v.toFixed(2);
  }

  // 美元量（dev / sd）: 带符号 2 位; |值| < 0.005 归零，防 toFixed 产生 "-0.00"
  function fmtUsd(v) {
    if (v == null) return '—';
    if (Math.abs(v) < 0.005) return '0.00';
    return (v > 0 ? '+' : '') + v.toFixed(2);
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

  function tag(cls, text, title) {
    return '<span class="tag ' + cls + '"' + (title ? ' title="' + esc(title) + '"' : '') + '>' + esc(text) + '</span>';
  }

  // 押注侧标签: yes=UP 配色 / no=DOWN 配色（元素 class 用 yes/no 而非 up/down）
  function sideTag(side) {
    if (!side) return '<span class="muted">—</span>';
    return '<span class="side-tag ' + side + '">' + side.toUpperCase() + '</span>';
  }

  function formatUptime(uptimeSec) {
    var sec = Math.floor(uptimeSec);
    var days = Math.floor(sec / 86400);
    var hours = Math.floor((sec % 86400) / 3600);
    var minutes = Math.floor((sec % 3600) / 60);
    var parts = [];
    if (days > 0) parts.push(days + 'D');
    if (hours > 0) parts.push(hours + 'H');
    if (minutes > 0 || parts.length === 0) parts.push(minutes + 'm');
    return parts.join(' ');
  }

  // ── 信号表的状态/结果/份额单元格 ──
  // 口径（a.md 第 3/4 条）: 每一笔信号都注册结算并显示官方结果; **未成交**（被风控拦/
  // 下单被拒/0 成交/挂单未定稿/成交未知）不进胜率、P&L 恒 0 —— 前端据此把份额与 P&L
  // 显示成「—」, 但结果列照显赢/输。

  // 该行是否有真实仓位（服务端已按 HasPosition 判过, 这里只读）
  function hasPosition(r) { return !!r.has_position; }

  // 状态列: 成交信息（含未成交/被闸）
  function statusCell(r) {
    if (r.gate_reason) {
      return tag('warn', GATE_SHORT[r.gate_reason] || r.gate_reason,
        '被风控闸拦下（未成交）: ' + (GATE_CN[r.gate_reason] || r.gate_reason) + (r.exec_note ? ' | ' + r.exec_note : ''));
    }
    if (r.exec_note && r.exec_note.indexOf(NOTE_UNKNOWN) === 0) {
      return tag('warn', '成交未知', r.exec_note + '（无仓位, 按 order_id 去 data-api 核对）');
    }
    var cn = EXEC_CN[r.exec_status];
    if (cn) {
      var warn = (r.exec_status === 'unfilled' || r.exec_status === 'rejected') ? 'warn' : '';
      var title = r.exec_note || '';
      if (r.hot_src === 'bid') title += (title ? ' | ' : '') + '该侧 ask 为空, 按 bid 挂单（成交概率低）';
      return tag(warn, cn, title);
    }
    return '<span class="muted">纸面成交</span>';
  }

  // 结果列: 官方结果（未成交行照显; 未结算的未成交行显示「—」而不是「待结算」——
  // 它永远不会变成持仓）
  function resultCell(r) {
    if (!r.ok) return '<span class="muted">—</span>';
    if (r.won == null) {
      return hasPosition(r) ? '<span class="muted">待结算</span>' : '<span class="muted">—</span>';
    }
    return r.won ? '<span class="won">赢</span>' : '<span class="lost">输</span>';
  }

  function pnlCell(r) {
    if (!hasPosition(r) || r.won == null) return '<td class="muted">—</td>';
    return '<td class="' + (r.pnl > 0 ? 'pos' : (r.pnl < 0 ? 'neg' : '')) + '">' + fmtPnl(r.pnl) + '</td>';
  }

  // ── 运行状态 ──

  var S = null; // 最近一次 /api/state

  function renderState(s) {
    S = s;
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

    // 日亏熔断（两模式都显示; paper 是"影子"——闸判据同源, 但只标记不拦单）
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

    // 统计（恒等式: 信号 = 胜 + 负 + 待结算 + 未成交; 胜率分母只含胜+负）
    $('statDecision').textContent = s.decision_count;
    $('statSignal').textContent = s.signal_count;
    $('statWL').textContent = s.won_count + ' / ' + s.lost_count;
    $('statNoExec').textContent = s.noexec_count;
    $('statPending').textContent = s.pending_count;
    $('statWr').textContent = (s.won_count + s.lost_count) > 0 ? (s.win_rate * 100).toFixed(1) + '%' : '—';
    var pnl = $('statPnl');
    pnl.textContent = fmtPnl(s.cumulative_pnl);
    pnl.classList.toggle('pos', s.cumulative_pnl > 0);
    pnl.classList.toggle('neg', s.cumulative_pnl < 0);

    // 最大回撤（负数越低越深）
    var dd = $('statDd');
    dd.textContent = fmtPnl(s.max_drawdown);
    dd.classList.toggle('neg', s.max_drawdown < 0);

    // 逐日（盈利日数 / 有结算的天数）
    $('statDay').textContent = s.day_total > 0 ? s.day_pnl_pos + '/' + s.day_total + ' 天' : '—';

    // 今日采集健康度（读当日 tailstats 文件）
    var th = $('todayHealth');
    if (s.today_stats_day) {
      var skips = s.today_skips || {};
      var sk = Object.keys(skips).map(function (k) {
        return (SKIP_CN[k] || k) + ' ' + skips[k];
      });
      th.textContent = '今日采集 ' + s.today_windows + ' 窗（行 ' + s.today_rows + '）· 无锚 ' +
        s.today_no_anchor + ' 窗 · skip ' + (sk.length ? sk.join(' · ') : '无');
    } else {
      th.textContent = '今日采集：无健康度文件（本日还没跑过窗口）';
    }

    // 当前窗口
    $('engineState').textContent = s.engine_state;
    $('engineState').className = 'engine-state ' + String(s.engine_state).toLowerCase();
    var slugLink = $('winSlugLink');
    if (s.slug) {
      slugLink.textContent = s.slug;
      slugLink.href = 'https://polymarket.com/zh/event/' + s.slug;
    } else {
      slugLink.textContent = '等待下一个窗口…';
      slugLink.removeAttribute('href');
    }

    // 三段链的四个闩锁 + 锚（闸值取自 /api/config）
    var t1 = CFG ? CFG.t150_rem : 150, t6 = CFG ? CFG.t60_rem : 60;
    $('latchT150').textContent = 'T150（rem≤' + t1 + '）· ' + (s.t150_sent ? '已判' : '未判');
    $('latchT150').className = 'latch' + (s.t150_sent ? ' on' : '');
    $('latchT60').textContent = 'T60（rem≤' + t6 + '）· ' + (s.t60_sent ? '已判' : '未判');
    $('latchT60').className = 'latch' + (s.t60_sent ? ' on' : '');
    // 监听段进入与否是**单调**的（出信号后仍为真）: 四种组合分别对应
    // 监听段出的信号 / 前段出的信号（本段没进）/ 正在监听 / 一直没达标。
    var listenTxt;
    if (s.listening) listenTxt = s.signal_sent ? '监听 · 已出信号' : '监听 · 进行中';
    else if (s.signal_sent) listenTxt = '监听 · 未进入（信号在前段）';
    else if (s.engine_state === 'Done') listenTxt = '监听 · 无（本窗未达标）';
    else listenTxt = '监听 · 未开始';
    $('latchListen').textContent = listenTxt;
    $('latchListen').className = 'latch' + (s.listening ? ' on' : '');
    $('latchSignal').textContent = '信号 · ' + (s.signal_sent ? '已出（整窗一单）' : '未出');
    $('latchSignal').className = 'latch' + (s.signal_sent ? ' on' : '');
    var anchorTxt;
    if (s.anchor > 0) {
      anchorTxt = '锚 ' + s.anchor.toFixed(2) + (s.anchor_exact ? '（边界精确命中' : '（未精确命中') +
        (s.anchor_arrived_ms ? '，到达 +' + (s.anchor_arrived_ms / 1000).toFixed(1) + 's' : '') + '）';
    } else {
      anchorTxt = '锚 未取到（本窗一行不产出）';
    }
    $('latchAnchor').textContent = anchorTxt;
    $('latchAnchor').className = 'latch' + (s.anchor > 0 ? ' on' : '');

    // 两侧报价 + 热门侧高亮（**有效价**高的一侧 = 押注侧）
    var hot = s.hot_ask > 0 ? s.hot_side : '';
    $('yesSide').classList.toggle('hot', hot === 'yes');
    $('noSide').classList.toggle('hot', hot === 'no');
    $('yesHot').textContent = hot === 'yes' ? '热门' : '';
    $('noHot').textContent = hot === 'no' ? '热门' : '';
    $('yesBid').textContent = s.yes_bid > 0 ? s.yes_bid.toFixed(3) : '—';
    $('yesAsk').textContent = s.yes_ask > 0 ? s.yes_ask.toFixed(3) : '—';
    $('noBid').textContent = s.no_bid > 0 ? s.no_bid.toFixed(3) : '—';
    $('noAsk').textContent = s.no_ask > 0 ? s.no_ask.toFixed(3) : '—';

    // 中间列: twap − anchor（TWAP 位移, 美元）——与 win-meta 的 dev = 符号·(spot − anchor)
    // 是**两个不同的量**（2026-09-24 修: 此前中间列误绑 dev, 与 meta 行数值恒等）。
    // 锚未取到或 twap 无推送 ⇒ 「—」；正绿负红为原值方向, 不按侧别取符号（同 flip）
    var twapDeltaEl = $('winTwapDelta');
    var twapDelta = (s.twap_price > 0 && s.anchor > 0) ? s.twap_price - s.anchor : null;
    twapDeltaEl.textContent = twapDelta == null ? '—' : fmtUsd(twapDelta);
    twapDeltaEl.classList.toggle('pos', twapDelta != null && twapDelta > 0);
    twapDeltaEl.classList.toggle('neg', twapDelta != null && twapDelta < 0);

    $('winRem').textContent = s.remaining_sec;
    // 热门侧有效价: ask 优先、bid 兜底（bid = 该侧卖单被撤空, 只能按买价挂单）
    $('winHot').innerHTML = s.hot_ask > 0
      ? sideTag(s.hot_side) + ' ' + (s.hot_src === 'bid' ? 'bid' : 'ask') + ' ' + s.hot_ask.toFixed(3)
      : '—';
    $('winDevUsd').textContent = s.dev ? fmtUsd(s.dev) + ' $' : '—';
    // sd 门槛标红: σ 腿放行要求 sd ≥ sigma_min_usd
    var sdEl = $('winSd');
    sdEl.textContent = s.sd ? fmtUsd(s.sd) + ' $' : '—';
    sdEl.className = (CFG && s.sd > 0 && s.sd < CFG.sigma_min_usd) ? 'stale' : '';
    $('winAnchor').textContent = s.anchor > 0 ? s.anchor.toFixed(2) : '—';
    $('winHist').textContent = s.hist_bps > 0 ? s.hist_bps.toFixed(2) + ' bps' : '—';

    // 三源新鲜度: 阈值由服务端下发（limits），前端不得硬编码
    var lim = s.limits || {};
    var latEl = $('winLat');
    latEl.textContent = s.book_latency_ms;
    latEl.className = (lim.book_lat_ms && s.book_latency_ms > lim.book_lat_ms) ? 'stale' : '';

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
    // TWAP-60 流值: ⑤ 判定不用它（只作诊断与 σ 的 close 口径）
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

    // 本窗 tick 健康度（延迟闸挡掉的 tick 在此可见; 窗口间隐藏）
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
      $('whRows').textContent = '本窗行数 ' + ws.frames;
      $('whRows').className = 'lost' + (ws.ticks_valid === 0 ? ' warn' : '');
    }

    $('foot').textContent = 'TS ' + s.ts + ' · 判定 ' + s.decision_count + ' 条 · 信号 ' +
      s.signal_count + ' 条 · 未成交 ' + s.noexec_count + ' 条';
  }

  // ── 配置（标定参数; 一次拉取，用于文案与闩锁闸值）──

  function renderConfig() {
    if (!CFG) return;
    $('cfgNote').innerHTML = '三段链（<b>不可调</b>，仅纸面登记）：rem ≤ <b>' + CFG.t150_rem +
      's</b> 判 ⑤ → 不达标则 rem ≤ <b>' + CFG.t60_rem + 's</b> 再判 ⑤ → 仍不达标则此后每秒判 ②。' +
      '规则：热门侧**有效价**（ask 优先、bid 兜底）≥ <b>' + CFG.price_min +
      '</b> 且（位移 dev ≥ <b>' + CFG.dev_min_usd + ' $</b> 或 <b>' + CFG.sigma_min_usd +
      ' $ ≤ sd ≤ dev</b>）；② 只要求价格腿 + dev 腿。每注 <b>' + CFG.stake +
      ' U</b> · 盘口延迟闸 <b>' + CFG.max_book_lat_ms +
      'ms</b>。实盘为 GTC 挂单等成交（挂到闭市撤余量），与回测「瞬时即成交」不是同一个估计量。';
  }

  // ── 信号 / 决策列表（服务端分页）──
  // 50 条/页，第 1 页 = 最新。自动轮询只刷第 1 页；翻历史页后暂停该表轮询
  // （避免正在看的行被新数据顶走），回到第 1 页自动恢复。
  var PAGE_SIZE = 50;
  var LISTS = {
    signals: { url: '/api/signals', cardId: 'signalsCard', pagerId: 'signalsPager', infoId: 'signalsPgInfo', page: 1, pages: 1, total: 0, auto: true },
    snaps: { url: '/api/snaps', cardId: 'snapsCard', pagerId: 'snapsPager', infoId: 'snapsPgInfo', page: 1, pages: 1, total: 0, auto: true }
  };

  // 信号表: 时间 侧 rem ask dev$ sd$ 份额 状态 结果 P&L
  function renderSignals(resp) {
    var tb = document.querySelector('#signalsTable tbody');
    tb.innerHTML = '';
    $('signalsEmpty').hidden = resp.items.length > 0;
    resp.items.forEach(function (r) {
      var tr = document.createElement('tr');
      var shares = hasPosition(r) ? r.shares.toFixed(1) : '—';
      tr.innerHTML =
        '<td class="muted">' + fmtTime(r.ts) + '</td>' +
        '<td>' + sideTag(r.side) + '</td>' +
        '<td>' + r.rem + '</td>' +
        '<td title="' + (r.hot_src === 'bid' ? 'ask 为空, 按 bid 兜底' : 'ask') + '">' +
        (r.hot_ask ? r.hot_ask.toFixed(3) : '—') + (r.hot_src === 'bid' ? '*' : '') + '</td>' +
        '<td>' + (r.dev ? fmtUsd(r.dev) : '—') + '</td>' +
        '<td>' + (r.sd ? r.sd.toFixed(1) : '—') + '</td>' +
        '<td>' + shares + '</td>' +
        '<td>' + statusCell(r) + '</td>' +
        '<td>' + resultCell(r) + '</td>' +
        pnlCell(r);
      tb.appendChild(tr);
    });
  }

  // 决策表: 时间 侧 rem ask dev$ sd$ 判定 份额（rem 天然区分 T150 / T60 两段）
  function renderSnaps(resp) {
    var tb = document.querySelector('#snapsTable tbody');
    tb.innerHTML = '';
    $('snapsEmpty').hidden = resp.items.length > 0;
    resp.items.forEach(function (r) {
      var tr = document.createElement('tr');
      var verdict = r.ok
        ? '<span class="won">信号</span>'
        : '<span class="muted">' + (REJECT_CN[r.reject_reason] || esc(r.reject_reason || '—')) + '</span>';
      tr.innerHTML =
        '<td class="muted">' + fmtTime(r.ts) + '</td>' +
        '<td>' + sideTag(r.side) + '</td>' +
        '<td title="' + (STAGE_CN[r.stage] || '旧口径') + '">' + r.rem + '</td>' +
        '<td>' + (r.hot_ask ? r.hot_ask.toFixed(3) : '—') + (r.hot_src === 'bid' ? '*' : '') + '</td>' +
        '<td>' + (r.dev ? fmtUsd(r.dev) : '—') + '</td>' +
        '<td>' + (r.sd ? r.sd.toFixed(1) : '—') + '</td>' +
        '<td>' + verdict + '</td>' +
        '<td>' + (r.ok ? r.shares.toFixed(1) : '—') + '</td>';
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
      if (name === 'signals') renderSignals(resp);
      else renderSnaps(resp);
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
  // 收起时整张贴 + 分页条 + 空态一起隐藏，卡片只剩标题栏一行；状态按表名存
  // localStorage（key 带 tail. 前缀，与 flip 面板同浏览器互不干扰）。默认决策表收起
  // （它是判定原稿，信号表才是结论）。localStorage 不可用（隐私模式）时静默降级。
  var COLLAPSE_KEY = 'tail.collapsedTables';
  var COLLAPSE_DEFAULT = { signals: false, snaps: true };

  function loadCollapsed() {
    try {
      var v = JSON.parse(localStorage.getItem(COLLAPSE_KEY));
      // 非对象（含 null / 被手改坏的值）一律按默认——严格模式下往原始值上写属性会抛错
      return (v && typeof v === 'object') ? v : {};
    } catch (e) {
      return {}; /* 无 localStorage / JSON 损坏 → 全部按默认 */
    }
  }

  var collapsed = loadCollapsed();

  function saveCollapsed() {
    try { localStorage.setItem(COLLAPSE_KEY, JSON.stringify(collapsed)); }
    catch (e) { /* 隐私模式写失败: 仅本次会话有效，不影响功能 */ }
  }

  function isCollapsed(name) {
    return Object.prototype.hasOwnProperty.call(collapsed, name)
      ? !!collapsed[name] : !!COLLAPSE_DEFAULT[name];
  }

  // 把状态刷到 DOM（卡片 class 驱动隐藏与箭头方向，aria-expanded 供读屏）
  function applyCollapse(name) {
    var L = LISTS[name];
    var on = isCollapsed(name);
    $(L.cardId).classList.toggle('collapsed', on);
    document.querySelector('#' + L.cardId + ' .list-toggle').setAttribute('aria-expanded', on ? 'false' : 'true');
  }

  function toggleCollapse(name) {
    collapsed[name] = !isCollapsed(name);
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

  // ── 逐日明细弹窗（/api/daily，UTC 日聚合）— 日行进 tbody，合计行放 tfoot 吸底 ──
  function dailyRowHtml(r, isTotal) {
    var wr = (r.won + r.lost) > 0 ? (r.win_rate * 100).toFixed(1) + '%' : '—';
    var pnlCls = r.pnl > 0 ? 'pos' : (r.pnl < 0 ? 'neg' : '');
    var tag = isTotal ? '合计' : r.date;
    var noexecCls = r.noexec > 0 ? 'neg' : 'muted';
    return '<tr>' +
      '<td class="' + (isTotal ? 'muted' : '') + '">' + tag + '</td>' +
      '<td>' + r.decisions + '</td>' +
      '<td>' + r.t150 + '</td>' +
      '<td>' + r.t60 + '</td>' +
      '<td>' + r.listen + '</td>' +
      '<td>' + r.signals + '</td>' +
      // 未成交列: 无仓位（被闸/被拒/0 成交/挂单未定稿）, 不进胜率与 P&L
      '<td class="' + noexecCls + '">' + r.noexec + '</td>' +
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
    var sub = '按 UTC 日切分 · 胜率/P&L 仅计有仓位的已结算信号 · 未成交不进胜率';
    if (d.days.length > 0) sub += ' · 日均 ' + (d.total.signals / d.days.length).toFixed(1) + ' 注/日';
    $('dailySub').textContent = sub;
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
    if (LISTS.signals.auto && !isCollapsed('signals')) fetchList('signals');
    if (LISTS.snaps.auto && !isCollapsed('snaps')) fetchList('snaps');
  }

  $('btnSignals').addEventListener('click', function () { fetchList('signals'); });
  $('btnSnaps').addEventListener('click', function () { fetchList('snaps'); });
  bindPager('signals');
  bindPager('snaps');
  bindCollapse('signals');
  bindCollapse('snaps');

  fetchJSON('/api/config', function (c) { CFG = c; renderConfig(); });

  setInterval(tick, stateInterval);
  setInterval(tickLists, listInterval);
  tick();
  tickLists();
})();
