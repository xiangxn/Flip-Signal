// 扫尾盘 ⑤ 纸面监控前端。
// 轮询: /api/state 5s（窗口/盘口/统计）、/api/snaps+/api/frames 15s、
//       /api/judge 60s（判决速览要全量行 × 6 格 × 2000 次 bootstrap，无需跟 5s 一起抖）。
// 阈值一律由服务端下发（/api/state 的 limits、/api/judge 的 meta、/api/config），
// 前端不得硬编码——调参后颜色语义与文案必须跟随实际生效值。
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };
  var stateInterval = 5000;
  var listInterval = 15000;
  var judgeInterval = 60000;

  // 判定失败原因的中文映射（决策快照表用）
  var REJECT_CN = {
    missing_spot: '现货缺失',
    missing_twap: 'TWAP 缺失',
    no_hist: 'σ 窗口不足',
    price_low: '价格腿不过（热门侧 < 0.80）',
    leg_out: '两腿都不过'
  };
  // 整窗跳过原因（tailstats_*.jsonl 的 skip 字段 = cmd/tail 主循环里的字面量）
  var SKIP_CN = {
    no_market: '无市场',
    no_token: '无 token',
    dup_record: '重复记录',
    no_sigma: 'σ 未就绪'
  };
  // 风控闸原因
  var GATE_CN = {
    daily_loss: '日亏熔断',
    first_window: '首窗禁单'
  };
  // 判词（与 internal/tail/judge.go 的 Verdict* 常量一一对应）
  var VERDICT_CN = {
    pending: '未到判决时点',
    pass: '通过',
    fail: '判负',
    inconclusive: '不显著'
  };
  var GRID_CHARS = ['①', '②', '③', '④', '⑤'];

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

  // 五格徽章: 由服务端下发的四条原始腿现算（①=价格腿, ②…⑤ 见 internal/tail/types.go）
  function gridBadges(r) {
    if (r.kind !== 'snap') return '<span class="muted">—</span>'; // 帧行不判定
    var p = !!r.rule_price, d = !!r.rule_dev63, s = !!r.rule_sigma, u = !!r.rule_sigma_usd40;
    var on = [p, p && d, p && s, p && (d || s), p && (d || u)];
    var out = '';
    for (var i = 0; i < 5; i++) {
      out += '<span class="grid-badge' + (on[i] ? ' on' : '') + '">' + GRID_CHARS[i] + '</span>';
    }
    return out;
  }

  // ── 判决卡 ──

  // 进度条: pct 截到 100%，达标（val ≥ max）时整条转绿
  function setBar(barId, wrapId, val, max) {
    var bar = $(barId), wrap = $(wrapId);
    var done = max > 0 && val >= max;
    bar.style.width = (max > 0 ? Math.min(100, val / max * 100) : 0).toFixed(1) + '%';
    wrap.classList.toggle('done', done);
  }

  function renderJudge(j) {
    var m = j.meta || {};
    var g = j.ruler5 || {};
    var grids = j.grids || [];

    var vb = $('judgeVerdict');
    vb.textContent = VERDICT_CN[g.verdict] || g.verdict || '—';
    vb.className = 'verdict ' + (g.verdict || 'pending');

    $('judgeDays').textContent = (g.days || 0) + ' / ' + m.min_days + ' 日';
    $('judgeN').textContent = (g.n || 0) + ' / ' + m.min_n + ' 注';
    setBar('judgeDaysBar', 'judgeDaysBarWrap', g.days || 0, m.min_days);
    setBar('judgeNBar', 'judgeNBarWrap', g.n || 0, m.min_n);

    $('judgeCi').textContent = '[' + fmtPnl(g.ci_lo) + ', ' + fmtPnl(g.ci_hi) + '] U';
    $('judgeLosing').textContent = (g.losing_days || 0) + ' / ' + (g.days || 0) + ' 日';
    $('judgeWr').textContent = g.n > 0 ? (g.wr * 100).toFixed(1) + '%' : '—';

    // 频率闸（判决辅助闸门 1）: ⑤ 应落 90~120 注/日
    var freq = $('judgeFreq');
    if (g.days > 0) {
      freq.textContent = g.notes_per_day.toFixed(1) + ' 注/日（带 ' + m.freq_lo + '~' + m.freq_hi + '）' +
        (g.freq_ok ? '' : ' ⚠️ 越界');
      freq.className = g.freq_ok ? '' : 'lost';
    } else {
      freq.textContent = '—';
    }

    $('judgeNote').innerHTML = '判决口径（先定后看，不许事后挑格）：UTC 日满 <b>' + m.min_days +
      '</b> 日且 ⑤ 已结算注数 ≥ <b>' + m.min_n + '</b> 时，按<b>日</b>有放回重采样 <b>' + m.boot_b +
      '</b> 次取 P&amp;L 的 95% 区间（seed=<b>' + m.boot_seed + '</b>，与 python/v4/13_tail_sweep.py 的 ' +
      '<b>boot_days</b> 逐位一致）；下界 &gt; 0 通过、上界 &lt; 0 判负、跨 0 不显著（延到 <b>' + m.days2 +
      '</b> 日，届时仍跨 0 即判负）。辅助闸门：频率 <b>' + m.freq_lo + '~' + m.freq_hi +
      '</b> 注/日。';

    renderGrids(grids);
  }

  function renderGrids(grids) {
    var tb = document.querySelector('#gridTable tbody');
    tb.innerHTML = '';
    var by = {};
    grids.forEach(function (g) {
      by[g.rule] = g;
      var tr = document.createElement('tr');
      if (g.rule === 't150') tr.className = 'sep';
      tr.innerHTML =
        '<td>' + esc(g.label) + '</td>' +
        '<td>' + g.n + '</td>' +
        '<td>' + (g.n > 0 ? (g.wr * 100).toFixed(2) + '%' : '—') + '</td>' +
        '<td class="' + (g.pnl > 0 ? 'pos' : (g.pnl < 0 ? 'neg' : '')) + '">' + fmtPnl(g.pnl) + '</td>' +
        '<td>' + g.losing_days + ' / ' + g.days + '</td>' +
        '<td>[' + fmtPnl(g.ci_lo) + ', ' + fmtPnl(g.ci_hi) + ']</td>' +
        '<td>' + (g.days > 0 ? g.notes_per_day.toFixed(1) : '—') + '</td>' +
        '<td><span class="verdict ' + g.verdict + '">' + (VERDICT_CN[g.verdict] || g.verdict) + '</span></td>';
      tb.appendChild(tr);
    });

    // 增量：⑤ 相对 ②（纯美元）与 ④（联合）——文档 §5.2 称「本项目最该盯的问题」
    var bits = [];
    if (by['5'] && by['2']) bits.push(deltaHtml('⑤ − ②', by['5'], by['2']));
    if (by['5'] && by['4']) bits.push(deltaHtml('⑤ − ④', by['5'], by['4']));
    $('gridDelta').innerHTML = bits.length
      ? '增量（同批快照口径）：' + bits.join(' &nbsp;·&nbsp; ')
      : '增量：—';
  }

  function deltaHtml(name, a, b) {
    var dWr = (a.n > 0 && b.n > 0) ? ((a.wr - b.wr) * 100).toFixed(2) + ' 个百分点' : '—';
    return '<b>' + name + '</b> 注数 ' + (a.n - b.n) + ' · P&amp;L ' + fmtPnl(a.pnl - b.pnl) +
      ' U · 胜率 ' + dWr;
  }

  // 「⑤ 本窗过不过」——判决卡上的实时读数，回答「现在为什么没有信号」
  function renderLive() {
    var el = $('judgeLive');
    if (!CFG || !S) { el.textContent = '—'; return; }
    if (!(S.hot_ask > 0)) { el.textContent = '无有效盘口'; return; }
    var legs = [];
    legs.push('ask ' + (S.hot_ask >= CFG.price_min ? '过' : '不过'));
    if (S.dev >= CFG.dev_min_usd) {
      legs.push('dev ' + fmtUsd(S.dev) + '$ 过');
    } else if (S.sd >= CFG.sigma_min_usd && S.dev >= S.sd) {
      legs.push('sd ' + fmtUsd(S.sd) + '$ 腿过');
    } else {
      legs.push('dev ' + fmtUsd(S.dev) + '$ 不过');
    }
    var pass = S.hot_ask >= CFG.price_min &&
      (S.dev >= CFG.dev_min_usd || (S.sd >= CFG.sigma_min_usd && S.dev >= S.sd));
    el.innerHTML = esc(S.hot_side || '—') + ' 侧 ' + legs.join(' · ') +
      ' → ' + (pass ? '<span class="pos">过 ⑤</span>' : '<span class="muted">未过 ⑤</span>');
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

    // 统计（快照口径: 帧行不算，见 recorder.Counts 注释）
    $('statSnap').textContent = s.snap_count;
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

    // 逐日（盈利日数 / 有数据的日数）
    $('statDay').textContent = s.day_total > 0 ? s.day_pnl_pos + '/' + s.day_total + ' 天' : '—';

    // 今日采集健康度（判决辅助闸门 3）
    $('judgeToday').textContent = s.today_stats_day
      ? s.today_windows + ' 窗（行 ' + s.today_rows + '）· 无锚 ' + s.today_no_anchor + ' 窗'
      : '（无今日健康度文件）';
    var skips = s.today_skips || {};
    var sk = Object.keys(skips).map(function (k) {
      return (SKIP_CN[k] || k) + ' ' + skips[k];
    });
    $('judgeSkips').textContent = sk.length ? sk.join(' · ') : '无';
    renderLive();

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

    // 两个闩锁 + 锚（帧 rem≤150 / 快照 rem≤60，闸值取自配置）
    var fr = CFG ? CFG.frame_rem : 150, rs = CFG ? CFG.rem_start : 60;
    $('latchFrame').textContent = '帧（rem≤' + fr + '）· ' + (s.frame_sent ? '已落' : '未落');
    $('latchFrame').className = 'latch' + (s.frame_sent ? ' on' : '');
    $('latchSnap').textContent = '快照（rem≤' + rs + '）· ' + (s.snap_sent ? '已落' : '未落');
    $('latchSnap').className = 'latch' + (s.snap_sent ? ' on' : '');
    var anchorTxt;
    if (s.anchor > 0) {
      anchorTxt = '锚 ' + s.anchor.toFixed(2) + (s.anchor_exact ? '（边界精确命中' : '（未精确命中') +
        (s.anchor_arrived_ms ? '，到达 +' + (s.anchor_arrived_ms / 1000).toFixed(1) + 's' : '') + '）';
    } else {
      anchorTxt = '锚 未取到（本窗一行不产出）';
    }
    $('latchAnchor').textContent = anchorTxt;
    $('latchAnchor').className = 'latch' + (s.anchor > 0 ? ' on' : '');

    // 两侧报价 + 热门侧高亮（ask 高的一侧 = 押注侧）
    var hot = s.hot_ask > 0 ? s.hot_side : '';
    $('yesSide').classList.toggle('hot', hot === 'yes');
    $('noSide').classList.toggle('hot', hot === 'no');
    $('yesHot').textContent = hot === 'yes' ? '热门' : '';
    $('noHot').textContent = hot === 'no' ? '热门' : '';
    $('yesBid').textContent = s.yes_bid > 0 ? s.yes_bid.toFixed(3) : '—';
    $('yesAsk').textContent = s.yes_ask > 0 ? s.yes_ask.toFixed(3) : '—';
    $('noBid').textContent = s.no_bid > 0 ? s.no_bid.toFixed(3) : '—';
    $('noAsk').textContent = s.no_ask > 0 ? s.no_ask.toFixed(3) : '—';

    // 中间列: dev（位移, 美元; 正 = 朝热门侧方向）——0 表示输入缺失未计算
    var devEl = $('winDev');
    devEl.textContent = s.dev ? fmtUsd(s.dev) : '—';
    devEl.classList.toggle('pos', s.dev > 0);
    devEl.classList.toggle('neg', s.dev < 0);

    $('winRem').textContent = s.remaining_sec;
    $('winHot').innerHTML = s.hot_ask > 0 ? sideTag(s.hot_side) + ' ask ' + s.hot_ask.toFixed(3) : '—';
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
      $('whFrames').textContent = '本窗行数 ' + ws.frames;
      $('whFrames').className = 'lost' + (ws.ticks_valid === 0 ? ' warn' : '');
    }

    $('foot').textContent = 'TS ' + s.ts + ' · 决策快照 ' + s.snap_count + ' 条 · 信号 ' + s.signal_count + ' 条';
  }

  // ── 配置（标定参数; 一次拉取，用于文案与闩锁闸值）──

  function renderConfig() {
    if (!CFG) return;
    $('cfgNote').innerHTML = '标定（<b>不可调</b>，仅纸面登记）：热门侧 ask ≥ <b>' + CFG.price_min +
      '</b> · 位移腿 dev ≥ <b>' + CFG.dev_min_usd + ' $</b> 或 <b>' + CFG.sigma_min_usd +
      ' $ ≤ sd ≤ dev</b> · 决策时点 rem ≤ <b>' + CFG.rem_start + 's</b>（帧 ' + CFG.frame_rem +
      's）· 每注 <b>' + CFG.stake + ' U</b> · 盘口延迟闸 <b>' + CFG.max_book_lat_ms +
      'ms</b>。实盘为 GTC 挂单等成交（挂到闭市），与回测「快照瞬间即成交」不是同一个估计量。';
    renderLive();
  }

  // ── 快照/帧列表（服务端分页）──
  // 50 条/页，第 1 页 = 最新。自动轮询只刷第 1 页；翻历史页后暂停该表轮询
  // （避免正在看的行被新数据顶走），回到第 1 页自动恢复。
  var PAGE_SIZE = 50;
  var LISTS = {
    snaps: { url: '/api/snaps', cardId: 'snapsCard', pagerId: 'snapsPager', infoId: 'snapsPgInfo', page: 1, pages: 1, total: 0, auto: true },
    frames: { url: '/api/frames', cardId: 'framesCard', pagerId: 'framesPager', infoId: 'framesPgInfo', page: 1, pages: 1, total: 0, auto: true }
  };

  // 快照行: 时间 侧 rem ask dev$ sd$ 五格 判定 份额 结果 P&L
  function renderSnaps(resp) {
    var tb = document.querySelector('#snapsTable tbody');
    tb.innerHTML = '';
    $('snapsEmpty').hidden = resp.items.length > 0;
    resp.items.forEach(function (r) {
      var tr = document.createElement('tr');
      var status;
      if (r.ok) {
        status = '<span class="won">信号</span>' +
          (r.gate_reason ? ' <span class="muted">被闸 ' + (GATE_CN[r.gate_reason] || esc(r.gate_reason)) + '</span>' : '');
      } else {
        status = '<span class="muted">' + (REJECT_CN[r.reject_reason] || esc(r.reject_reason || '—')) + '</span>';
      }
      var result = '—';
      if (r.ok) {
        result = (r.won == null) ? '<span class="muted">待结算</span>'
          : (r.won ? '<span class="won">赢</span>' : '<span class="lost">输</span>');
      }
      var pnlCell = (r.ok && r.won != null)
        ? '<td class="' + (r.pnl > 0 ? 'pos' : (r.pnl < 0 ? 'neg' : '')) + '">' + fmtPnl(r.pnl) + '</td>'
        : '<td class="muted">—</td>';
      tr.innerHTML =
        '<td class="muted">' + fmtTime(r.ts) + '</td>' +
        '<td>' + sideTag(r.side) + '</td>' +
        '<td>' + r.rem + '</td>' +
        '<td>' + (r.hot_ask ? r.hot_ask.toFixed(3) : '—') + '</td>' +
        '<td>' + (r.dev ? fmtUsd(r.dev) : '—') + '</td>' +
        '<td>' + (r.sd ? r.sd.toFixed(1) : '—') + '</td>' +
        '<td>' + gridBadges(r) + '</td>' +
        '<td>' + status + '</td>' +
        '<td>' + (r.shares ? r.shares.toFixed(1) : '—') + '</td>' +
        '<td>' + result + '</td>' + pnlCell;
      tb.appendChild(tr);
    });
  }

  // 帧行: 时间 侧 rem ask dev$ sd$ anchor hist_bps（只记录，不判定）
  function renderFrames(resp) {
    var tb = document.querySelector('#framesTable tbody');
    tb.innerHTML = '';
    $('framesEmpty').hidden = resp.items.length > 0;
    resp.items.forEach(function (r) {
      var tr = document.createElement('tr');
      tr.innerHTML =
        '<td class="muted">' + fmtTime(r.ts) + '</td>' +
        '<td>' + sideTag(r.side) + '</td>' +
        '<td>' + r.rem + '</td>' +
        '<td>' + (r.hot_ask ? r.hot_ask.toFixed(3) : '—') + '</td>' +
        '<td>' + (r.dev ? fmtUsd(r.dev) : '—') + '</td>' +
        '<td>' + (r.sd ? r.sd.toFixed(1) : '—') + '</td>' +
        '<td>' + (r.anchor ? r.anchor.toFixed(2) : '—') + '</td>' +
        '<td>' + (r.hist_bps ? r.hist_bps.toFixed(2) : '—') + '</td>';
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
      if (name === 'snaps') renderSnaps(resp); else renderFrames(resp);
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
  // localStorage（key 带 tail. 前缀，与 flip 面板同浏览器互不干扰）。默认帧表收起
  // （它是原稿，快照表才是结论）。localStorage 不可用（隐私模式）时静默降级。
  var COLLAPSE_KEY = 'tail.collapsedTables';
  var COLLAPSE_DEFAULT = { snaps: false, frames: true };

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
    return '<tr>' +
      '<td class="' + (isTotal ? 'muted' : '') + '">' + tag + '</td>' +
      '<td>' + r.frames + '</td>' +
      '<td>' + r.snaps + '</td>' +
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
    // 日均注数 = 判决频率闸的对照量（合计行的 notes_per_day）
    var sub = '按 UTC 日切分 · 胜率/P&L 仅计已结算信号';
    if (d.total.notes_per_day > 0) sub += ' · 日均 ' + d.total.notes_per_day.toFixed(1) + ' 注/日';
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
  function tickJudge() {
    // 判决要全量行 × 6 格 × 2000 次重采样，给足超时
    fetchJSON('/api/judge', renderJudge, 15000);
  }
  // 自动轮询只刷第 1 页（最新）; 用户翻历史页期间暂停对应表; 收起的表不拉（展开时补拉）
  function tickLists() {
    if (LISTS.snaps.auto && !isCollapsed('snaps')) fetchList('snaps');
    if (LISTS.frames.auto && !isCollapsed('frames')) fetchList('frames');
  }

  $('btnSnaps').addEventListener('click', function () { fetchList('snaps'); });
  $('btnFrames').addEventListener('click', function () { fetchList('frames'); });
  $('btnJudge').addEventListener('click', tickJudge);
  bindPager('snaps');
  bindPager('frames');
  bindCollapse('snaps');
  bindCollapse('frames');

  fetchJSON('/api/config', function (c) { CFG = c; renderConfig(); });

  setInterval(tick, stateInterval);
  setInterval(tickLists, listInterval);
  setInterval(tickJudge, judgeInterval);
  tick();
  tickLists();
  tickJudge();
})();
