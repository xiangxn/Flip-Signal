// 扫尾盘 ⑤ 纸面监控前端。
// 轮询: /api/state 5s（窗口/盘口/统计）、/api/curve 1s（本窗曲线）、
// /api/signals + /api/snaps 15s。
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
  var curveInterval = 1000; // 曲线服务端每秒采一点, 拉快于此没有意义

  // 判定失败原因的中文映射（决策表用）
  var REJECT_CN = {
    missing_spot: '现货缺失',
    missing_twap: 'TWAP 缺失', // legacy 旧行（新口径不再产出）
    no_hist: 'σ 窗口不足',
    price_low: '价格腿不过',
    leg_out: '两腿都不过',
    walk_low: '入场闸不过', // T=150 段: ⑤ 达标但 walk < 43 美元（决策 #29, 链继续）
    floor_low: '地板不过' // T=60 的 ⑤ / 监听段的 ② 达标但有效价 ≤ 0.83（决策 #32/#33, 影子行, 链继续）
  };
  // 整窗跳过原因（tailstats_*.jsonl 的 skip 字段 = cmd/tail 主循环里的字面量）
  var SKIP_CN = {
    no_market: '无市场',
    no_token: '无 token',
    dup_record: '重复记录',
    no_sigma: 'σ 未就绪'
  };
  // 风控闸原因（= internal/tail Gate* 常量字面量）
  // ⚠️ first_window 只剩历史行（2026-09-29 决策 #28 起引擎不再产出）——标签保留是为了照显旧数据。
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
    // TWAP-60 流值: T=150 段的入场闸由它算 walk（walk = sgn·(twap−anchor) ≥ 43）;
    // 其余段只作诊断（以及 σ 的 close 口径）
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

  // ── 本窗动态曲线（anchor / twap / spot 三线同轴, 美元）──
  //
  // 数据由服务端按 tick 逐秒采样（/api/curve, 见 cmd/tail/curve.go）——前端只画,
  // 不自己攒点: /api/state 是 5s 轮询, 用它攒点会漏掉 4/5 的采样。
  //
  // 三条线的取值差常常只有几十美元（BTC 十万量级）, 所以 y 量程**必须**按数据自适应,
  // 否则三线叠成一条; 代价是量程逐秒可能微调, 用「取整到好看步长」把它压到不晃眼。
  // 第 4/5 条是两条派生阈值线（虚线, 中性墨色）, 也进量程但**限量**——见 drawChart 里的
  // TIE_ROOM 与 SERIES 的注释。
  //
  // 换窗: event_start 变化 = 换窗 ⇒ 整条重画; 新窗还没采样时服务端返回的仍是**上一窗**
  // （points 非空）⇒ 这里什么都不做, 旧曲线一直留到新窗第一个点到来。
  //
  // 两处用同一套绘制: 这里是本窗主图; 点信号表/决策表的某一行时会开一个弹窗画**那一行
  // 所属窗口**的曲线（历史窗从原始采集重建, 决策 #31）。
  //
  // 配色 = 分类槽 1/2/3（蓝/橙/水绿）, 经 dataviz 校验器在 #161b22 暗面上全项通过
  // （定义在 style.css 的 --c-* 里; 换色前后都要重跑 scripts/validate_palette.js）。
  //
  // 图**实例**（决策 #31）: 主图与「点行看曲线」弹窗图共用同一套绘制代码, 但数据/悬停下标/
  // 换算参数必须**按实例分开**——原来是模块级单例（CURVE/hoverIdx/geom）, 两张图并存时
  // 弹窗一开会把主图的悬停下标套到弹窗的数据上（下标错位 ⇒ 准星乱跳）。
  // mark = 要标注的竖线时刻（ms; 0 = 不画）——弹窗用来标「那一行发生在第几秒」。
  // bk = 图下第二条读数行（那一秒的 PM 盘口, 见 setBook）。
  function makeChart(cvId, roId, bkId) {
    return {
      cv: $(cvId), ro: $(roId), bk: $(bkId),
      data: null, hover: -1, geom: null, mark: 0, empty: '等待本窗数据…'
    };
  }
  var mainChart = makeChart('curveCanvas', 'curveReadout', 'curveBook');             // 本窗曲线（1s 轮询 /api/curve）
  var winChart = makeChart('winCurveCanvas', 'winCurveReadout', 'winCurveBook');     // 点行弹窗（冻结的历史窗）
  var COLORS = null;   // 各线颜色（从 CSS 变量读一次后缓存; 两图共一份）

  // ⚠️ 后两条（tie / extrap）是**派生量**（所谓「临界价」——现货得走到哪才结算 Up）,
  // 不是读数: 它们走**虚线 + 中性墨色**, 不占分类槽（分类槽没有第 4 色可用, 见 style.css）,
  // 两条之间靠**虚线节奏**区分。二者的定义域**正好拼满整窗**、假设正好相反:
  //   - tie（长划）: rem ≤ 60 —— 从现在起**守住一个价**不动（运动停住）;
  //   - extrap（点线）: rem ∈ [60, 150] —— **保持当前速度**线性走下去（运动继续）,
  //     从三段链的第一个判定点 T=150 起画。
  // hint 是图例的悬浮说明（可选）。图例由这里生成（renderLegend）——主图与弹窗图两处
  // 共用同一份定义, 不然加一条线要记得改两处（第 4/5 条线加进来时正是这么漏的）。
  var SERIES = [
    { key: 'anchor', cn: 'Anchor', fallback: '#3987e5' },              // 边界那一秒的 TWAP 推送（本窗冻结）
    { key: 'twap', cn: 'TWAP', fallback: '#199e70' },              // TWAP-60 流值 = 结算线本身
    { key: 'spot', cn: 'Spot', fallback: '#d95926' },              // Binance 现货（领先量）
    {
      key: 'tie', cn: '临界价', fallback: '#8b949e', derived: true, dash: [5, 4],
      hint: '假设现货从此刻起一直不动: 守在它之上 ⇒ 闭市 TWAP 压在锚上 ⇒ 结算 Up（rem ≤ 60 才有值）'
    },
    {
      key: 'extrap', cn: '外推临界价', fallback: '#8b949e', derived: true, dash: [1.5, 3.5],
      hint: '假设现货保持当前速度线性运行: 进入最后 60s 时须达到的价格, 才能让闭市 TWAP 等于锚（rem 60~150 才有值）'
    }
  ];
  var CURVE_FONT = '10px -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif';
  // 派生线能把 y 量程在三条实测线之外**每侧**撑开多少（单位 = 三线 span 的倍数）。
  // 0.5 ⇒ 量程最多涨到三线 span 的 2 倍, 三条实测线因此至少占约 1/3 图高（量程还要过
  // 「取整到好看步长」那一关, 实际比值看当窗数据）。
  // 为什么不是 1.0（= 三线 span 的 3 倍）: 三条实测线的可读性是这张图的第一目的, 而
  // 0.5→1.0 换来的可见秒数极少（真实窗实测 41s → 41s, 一动不动; 极端合成窗 45s → 48s）,
  // 代价却是三线占高 36% → 26%。0.5 落在实测的**平台中段**: 0.25~1.0 之间在真实数据上
  // 给出的是同一个量程（步长取整把它们吸到了一起）, 取中段最不易被某一次取整甩出去。
  // 调大 = 临界价在画上留得更久、三线更挤; 调小 = 反过来。
  var TIE_ROOM = 0.5;

  function seriesColors() {
    if (!COLORS) {
      var cs = getComputedStyle(document.documentElement);
      COLORS = {};
      SERIES.forEach(function (s) {
        COLORS[s.key] = cs.getPropertyValue('--c-' + s.key).trim() || s.fallback;
      });
    }
    return COLORS;
  }

  // 图例（主图与弹窗图各调一次, 内容同一份 SERIES）。色块是 <i class="sw <key>">——虚线那两条
  // 也靠 CSS 里的同款虚线色块, 与图上编码一致。
  function renderLegend(el) {
    el.innerHTML = SERIES.map(function (s) {
      return '<span class="lg"' + (s.hint ? ' title="' + esc(s.hint) + '"' : '') + '>' +
        '<i class="sw ' + s.key + '"></i>' + s.cn + '</span>';
    }).join('');
  }

  // 刻度步长取 1/2/5×10^n 里第一个 ≥ raw 的——每格都是整数, 量程逐秒重算也不晃。
  function niceStep(raw) {
    var steps = [0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000];
    for (var i = 0; i < steps.length; i++) { if (steps[i] >= raw) return steps[i]; }
    return Math.ceil(raw / 1000) * 1000;
  }

  // 采样点的窗口内秒数（以 ts 为准; ts 缺失时退回 窗口长−rem）
  function elapsedSec(p, start, winSec) {
    return start > 0 ? p.ts / 1000 - start : winSec - p.rem;
  }

  // 盘口读数行: 那一秒的 PM 两侧最优价（UP=YES / DOWN=NO; bid / ask）。
  //
  // 为什么单独一行而不是塞进上面那行: 上面那行是**曲线上的序列**（每条线一个值, 带色块）,
  // 盘口是**市场读数**、不是图里的线（四条 0~1 的价格线塞进美元量程会把三线压平）——
  // 混在一行里读者分不清哪个值属于哪条线。
  //
  // 0 = 该侧无报价 ⇒ 「—」: 尾盘「押最终输家」那条腿被整侧撤空是常态（决策 #21）,
  // 而**看它什么时候被撤空**恰恰是这张图的一个用处。四个全 0 = 那一刻整簿都没有
  // （窗首首份快照未到, 决策 #25 的窗首瞬态）⇒ 直说「无盘口」, 不和「某侧空」混同。
  function bookHTML(p, sec) {
    if (!p) return '盘口 —';
    var head = '<span class="bit"><b>盘口 t=' + Math.round(sec) + 's</b></span>';
    if (!(p.yes_bid > 0 || p.yes_ask > 0 || p.no_bid > 0 || p.no_ask > 0)) {
      return head + ' <span class="bit">无盘口</span>';
    }
    var q = function (v) { return v > 0 ? v.toFixed(2) : '—'; };
    return head +
      ' <span class="bit">UP ' + q(p.yes_bid) + ' / ' + q(p.yes_ask) + '</span>' +
      ' <span class="bit">DOWN ' + q(p.no_bid) + ' / ' + q(p.no_ask) + '</span>';
  }

  // lastBooked 是没悬停时的默认点: 往前找**最后一个有报价**的采样点, 一个都没有才退回
  // 最后一点。为什么不直接用最后一点: 采集行的收尾补采 tick（闭市那一刻精确补一次）
  // 盘口四档**恒空**（本机三个采集文件的 63 窗里末 tick 全零 43 窗——构造性产物, 不是
  // 市场读数）, 而弹窗是冻结的历史窗、默认就落在它上面 ⇒ 常驻状态会恒显「无盘口」,
  // 看起来像这个功能坏了。标签自带 t=, 因此回退不冒充「最后一秒」。
  function lastBooked(pts) {
    for (var i = pts.length - 1; i >= 0; i--) {
      var p = pts[i];
      if (p.yes_bid > 0 || p.yes_ask > 0 || p.no_bid > 0 || p.no_ask > 0) return i;
    }
    return pts.length - 1;
  }

  // 写盘口读数行: 有悬停就看**悬停那一秒**, 否则看默认点（见 lastBooked）——两种情形都
  // 带 t= 标签, 所以「现在」与「某一秒」不会被读混（主图上默认点 ≈ 当前, 弹窗上它是
  // 那一窗最后一个有报价的时刻）。
  function setBook(ch, pts, secs) {
    if (!ch.bk) return;
    if (!pts.length) { ch.bk.textContent = '盘口 —'; return; }
    var i = (ch.hover >= 0 && ch.hover < pts.length) ? ch.hover : lastBooked(pts);
    ch.bk.innerHTML = bookHTML(pts[i], secs[i]);
  }

  // 画一张曲线图（ch = makeChart 出来的实例）。主图与弹窗图都走这里, 区别只有数据源与
  // ch.mark（弹窗才标竖线）。
  function drawChart(ch) {
    var cv = ch.cv;
    var wrap = cv.parentNode;
    var W = wrap.clientWidth, H = wrap.clientHeight;
    if (!W || !H) return; // 隐藏中（卡片折叠等）: 不画也不改尺寸
    var dpr = window.devicePixelRatio || 1;
    if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) {
      cv.width = Math.round(W * dpr);
      cv.height = Math.round(H * dpr);
    }
    var ctx = cv.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    var css = getComputedStyle(document.documentElement);
    var muted = css.getPropertyValue('--muted').trim() || '#8b949e';
    var grid = css.getPropertyValue('--grid').trim() || '#21262d';
    var surface = css.getPropertyValue('--card').trim() || '#161b22';

    var pts = (ch.data && ch.data.points) || [];
    var winSec = (ch.data && ch.data.window_sec) || 300;
    var start = ch.data ? ch.data.event_start : 0;

    // 左侧留出整数价刻度（110432.15 这类 9 字符）; 右侧留一格给末位时间刻度
    var padL = 56, padR = 12, padT = 10, padB = 18;
    var pw = W - padL - padR, ph = H - padT - padB;
    if (pw < 40 || ph < 40) return;
    ch.geom = null;
    ctx.font = CURVE_FONT;

    // y 量程: 三条**实测**线打底, 两条派生线**也参与**（同轴同刻度才可比——临界价是价格,
    // 存在的意义就是跟现货直接比高低）, 但参与的**取值要限幅**; 一个有效读数都没有 = 等待数据。
    // ⚠️ tie 必须限幅: 它在 rem→0 时发散（要几秒扳回整段偏差, rem=5 放大 11 倍、rem=1
    // 放大 59 倍）, 全额放进来会把三条价格线压成一条平线。做法 = 它在量程里的**取值先夹到
    // 三线 span 的 ±TIE_ROOM 倍以内**再取 min/max: rem 还大时它完整可见（这正是修的那个
    // bug——原样全额取量程会立刻把三线压扁, 完全不取量程则它一涨出三线范围就贴边消失）,
    // 发散到离谱时量程停涨、超出的部分由下面的 clip 裁掉。
    // 用**夹取**而不是「超了就整段退回三线量程」是为了**连续性**: 量程随临界价平缓长大、
    // 到顶后停住, 画面不会跳一下。
    // ⚠️ extrap 过这一关是**恒等变换**: 它是 Spot 与 anchor 的凸组合, 而这两个值本身就在
    // 三线 min/max 里 ⇒ 它永远落在三线之间, 夹不夹都一样。放进来只为「派生量统一处理」,
    // 免得日后有人以为这里漏了它（真正让它保持可读的是这个凸组合性质, 不是这行代码）。
    var lo = Infinity, hi = -Infinity;
    pts.forEach(function (p) {
      SERIES.forEach(function (s) {
        if (s.derived) return;
        var v = p[s.key];
        if (v > 0) { if (v < lo) lo = v; if (v > hi) hi = v; }
      });
    });
    if (!isFinite(lo)) {
      ctx.fillStyle = muted;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(ch.empty, W / 2, H / 2);
      if (ch.bk) ch.bk.textContent = '盘口 —'; // 一条线都没有时不谈盘口（本窗无数据）
      return;
    }
    if (hi - lo < 0.5) { var mid = (lo + hi) / 2; lo = mid - 0.5; hi = mid + 0.5; } // 单点/极窄
    var room = (hi - lo) * TIE_ROOM;                 // 派生线每侧最多撑开这么多
    var capLo = lo - room, capHi = hi + room;
    pts.forEach(function (p) {
      SERIES.forEach(function (s) {
        if (!s.derived) return;
        var v = p[s.key];
        if (!(v > 0)) return;
        if (v < capLo) v = capLo; else if (v > capHi) v = capHi;
        if (v < lo) lo = v; else if (v > hi) hi = v;
      });
    });
    var pad = (hi - lo) * 0.12;
    lo -= pad; hi += pad;
    var step = niceStep((hi - lo) / 3);
    lo = Math.floor(lo / step) * step;
    hi = Math.ceil(hi / step) * step;
    var lines = Math.round((hi - lo) / step);

    var xOf = function (sec) { return padL + Math.min(Math.max(sec, 0), winSec) / winSec * pw; };
    var yOf = function (val) { return padT + (hi - val) / (hi - lo) * ph; };
    var secs = pts.map(function (p) { return elapsedSec(p, start, winSec); });
    ch.geom = { padL: padL, pw: pw, winSec: winSec, secs: secs };

    // 网格与刻度（实线发丝线, 比表面亮一档——虚线会读成阈值/预测）
    ctx.lineWidth = 1;
    ctx.strokeStyle = grid;
    ctx.fillStyle = muted;
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    for (var i = 0; i <= lines; i++) {
      var gy = Math.round(yOf(lo + i * step)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(padL, gy);
      ctx.lineTo(padL + pw, gy);
      ctx.stroke();
      ctx.fillText((lo + i * step).toFixed(2), padL - 6, gy);
    }
    ctx.textAlign = 'center';
    var xStep = winSec > 300 ? 120 : 60;
    for (i = 0; i <= winSec; i += xStep) {
      var gx = Math.round(xOf(i)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(gx, padT);
      ctx.lineTo(gx, padT + ph);
      ctx.stroke();
      ctx.fillText(String(i), gx, padT + ph + 9);
    }

    // 标记线（弹窗才有）: 标出「这一行发生在第几秒」。画在网格之上、五条线**之下**——
    // 它是**注释**不是数据序列, 压在上面会把「那一刻价在哪」盖住。
    // 与悬停准星同色（都是竖线）, 靠**顶部标签**区分（准星没有标签）; 也不占分类色槽。
    if (ch.mark > 0 && start > 0) {
      var markSec = ch.mark / 1000 - start;
      if (markSec >= 0 && markSec <= winSec) {
        var mx = Math.round(xOf(markSec)) + 0.5;
        var rightSide = mx > padL + pw / 2;
        ctx.save();
        ctx.strokeStyle = 'rgba(230, 237, 243, 0.4)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(mx, padT);
        ctx.lineTo(mx, padT + ph);
        ctx.stroke();
        // 标签贴线写, 靠右半边就翻到线左侧（免得跑出画框）; 贴着画框上沿
        ctx.fillStyle = muted;
        ctx.textAlign = rightSide ? 'right' : 'left';
        ctx.textBaseline = 'top';
        ctx.fillText('t=' + Math.round(markSec) + 's', mx + (rightSide ? -4 : 4), padT + 1);
        ctx.restore();
      }
    }

    // 曲线（2px; 该点无读数就断线, 不跨着空洞连——两条派生线的定义域不重叠, 所以这一段
    // 里每一条都只有半窗有值, 断线是它们正常的写法而不是缺数据）。**派生量先画、垫在
    // 下面**: 阈值不是读数, 三条实测线压在上面才读得清; 量程已经给 tie 让过路（TIE_ROOM）,
    // 但 rem→0 它仍可能跑出去 ⇒ 裁到绘图矩形内——线贴边消失 = 量程依然装不下它 =
    // 「现货离守住差得远」（确切数字看读数行）。
    var colors = seriesColors();
    var order = SERIES.filter(function (s) { return s.derived; })
      .concat(SERIES.filter(function (s) { return !s.derived; }));
    order.forEach(function (s) {
      ctx.save();
      ctx.strokeStyle = colors[s.key];
      ctx.lineWidth = 2;
      ctx.lineJoin = 'round';
      ctx.lineCap = 'round';
      if (s.derived) {
        ctx.setLineDash(s.dash); // 同一支中性墨色, 靠**虚线节奏**分两条（不再有可用的分类色）
        ctx.beginPath();
        ctx.rect(padL, padT, pw, ph);
        ctx.clip();
      }
      ctx.beginPath();
      var pen = false;
      for (var j = 0; j < pts.length; j++) {
        var v = pts[j][s.key];
        if (!(v > 0)) { pen = false; continue; }
        if (pen) ctx.lineTo(xOf(secs[j]), yOf(v));
        else { ctx.moveTo(xOf(secs[j]), yOf(v)); pen = true; }
      }
      ctx.stroke();
      ctx.restore();
    });

    // 悬停/触摸: 竖直准星 + 三个点 + 线下读数（点/触即得, 不靠悬浮窗遮挡曲线）
    var ro = ch.ro;
    if (ch.hover >= 0 && ch.hover < pts.length) {
      var hx = xOf(secs[ch.hover]);
      ctx.strokeStyle = 'rgba(230, 237, 243, 0.4)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(Math.round(hx) + 0.5, padT);
      ctx.lineTo(Math.round(hx) + 0.5, padT + ph);
      ctx.stroke();
      SERIES.forEach(function (s) {
        var v = pts[ch.hover][s.key];
        if (!(v > 0)) return;
        ctx.save();
        if (s.derived) { // tie 可能落在量程外 ⇒ 与线同一刀裁掉
          ctx.beginPath();
          ctx.rect(padL, padT, pw, ph);
          ctx.clip();
        }
        ctx.beginPath();
        ctx.arc(hx, yOf(v), 3, 0, 6.2832);
        if (s.derived) { // 空心点: 与虚线同一套「这不是读数」的编码
          ctx.strokeStyle = colors[s.key];
          ctx.lineWidth = 2;
          ctx.stroke();
        } else {
          ctx.fillStyle = colors[s.key];
          ctx.fill();
          ctx.strokeStyle = surface; // 2px 表面色描边: 三线重叠时也分得开
          ctx.lineWidth = 2;
          ctx.stroke();
        }
        ctx.restore();
      });
      // 每项包一个 .bit（nowrap: 「现货」不能和它后面那个数分家）: 窄屏放不下时在
      // **项与项之间**折行——5 项合起来在手机上必然两行。
      // ⚠️ join 的分隔符必须是**一个空格**, 不能是空串: .bit 是 nowrap 的 inline,
      // 相邻两个之间没有空白就**没有断行机会**, 整行会横着溢出视口（实测 597px）。
      // 视觉上的「 · 」分隔由 CSS 的 .bit::before 加, 它跟着后一项走, 不会留在行尾。
      var bits = SERIES.map(function (s) {
        var v = pts[ch.hover][s.key];
        return '<span class="bit"><i class="sw ' + s.key + '"></i>' + s.cn + ' ' +
          (v > 0 ? v.toFixed(2) : '—') + '</span>';
      });
      ro.innerHTML = '<span class="bit"><b>t=' + Math.round(secs[ch.hover]) + 's</b></span> ' +
        bits.join(' ');
    } else {
      ro.textContent = '窗口内秒 0 → ' + winSec + '（0 = 边界）· 点按曲线查看某秒读数';
    }
    setBook(ch, pts, secs); // 图下第二条读数行: 盘口（悬停跟随光标, 否则默认点见 lastBooked）
  }

  // 悬停/触摸 → 最近采样点（1s 一个点, 命中区就是整列, 不必精确对准）
  function hoverChart(ch, clientX) {
    if (!ch.geom || !ch.data || !ch.data.points.length) return;
    var rect = ch.cv.getBoundingClientRect();
    var sec = (clientX - rect.left - ch.geom.padL) / ch.geom.pw * ch.geom.winSec;
    var best = -1, bestD = Infinity;
    ch.geom.secs.forEach(function (s, i) {
      var d = Math.abs(s - sec);
      if (d < bestD) { bestD = d; best = i; }
    });
    if (best === ch.hover) return;
    ch.hover = best;
    drawChart(ch);
  }

  // 一张图的四条指针事件（两图共用）。被动监听: 触摸要能顺着页面滚动（touch-action: pan-y）。
  function bindChartHover(ch) {
    ch.cv.addEventListener('mousemove', function (e) { hoverChart(ch, e.clientX); });
    ch.cv.addEventListener('mouseleave', function () { resetHover(ch); });
    ch.cv.addEventListener('touchstart', function (e) {
      if (e.touches[0]) hoverChart(ch, e.touches[0].clientX);
    }, { passive: true });
    ch.cv.addEventListener('touchmove', function (e) {
      if (e.touches[0]) hoverChart(ch, e.touches[0].clientX);
    }, { passive: true });
    ch.cv.addEventListener('touchend', function () { resetHover(ch); });
  }

  function resetHover(ch) {
    if (ch.hover === -1) return; // 没悬停过就不重画（轮询那边每秒也在画, 别白画两次）
    ch.hover = -1;
    drawChart(ch);
  }

  // 主图的 1s 轮询回来（永远是「本窗」）——弹窗图不走这里, 它是冻结的历史窗, 不轮询。
  function onCurve(d) {
    d.points = d.points || [];
    var ch = mainChart;
    if (ch.data && d.event_start === ch.data.event_start) {
      ch.data = d;          // 同窗: 追加的点直接上屏
    } else if (d.points.length > 0) {
      ch.data = d;          // 换窗且已有采样: 整条换装
      ch.hover = -1;
    } else {
      return;               // 换窗但还没有采样 / 服务端重启: 保留旧曲线不擦
    }
    $('curveTitle').textContent = d.event_start > 0
      ? '本窗曲线 · ' + fmtTime(d.event_start * 1000) + ' 起 · ' + d.points.length + ' 点'
      : '本窗曲线';
    drawChart(ch);
  }

  // ── 信号 / 决策列表（服务端分页）──
  // 50 条/页，第 1 页 = 最新。自动轮询只刷第 1 页；翻历史页后暂停该表轮询
  // （避免正在看的行被新数据顶走），回到第 1 页自动恢复。
  var PAGE_SIZE = 50;
  var LISTS = {
    signals: { url: '/api/signals', cardId: 'signalsCard', pagerId: 'signalsPager', infoId: 'signalsPgInfo', page: 1, pages: 1, total: 0, auto: true },
    snaps: { url: '/api/snaps', cardId: 'snapsCard', pagerId: 'snapsPager', infoId: 'snapsPgInfo', page: 1, pages: 1, total: 0, auto: true }
  };

  // 让一行可点（信号表与决策表都点）: 带上该行所属窗口的起点与时刻, 点击弹窗画那一窗的曲线。
  // ⚠️ 定位原始采集只认 event_start, **不能**拿 date 反推——date 是**行 ts** 的 UTC 日,
  // 跨午夜的窗会差一天, 而 events 文件按**窗口起点**归日（决策 #31）。
  function clickableRow(tr, r) {
    tr.className = 'row-click';
    tr.title = '点击看这一窗的曲线';
    tr.setAttribute('data-es', r.event_start || 0);
    tr.setAttribute('data-ts', r.ts || 0);
    return tr;
  }

  // 信号表: 时间 侧 rem ask dev$ sd$ 份额 状态 结果 P&L
  function renderSignals(resp) {
    var tb = document.querySelector('#signalsTable tbody');
    tb.innerHTML = '';
    $('signalsEmpty').hidden = resp.items.length > 0;
    resp.items.forEach(function (r) {
      var tr = clickableRow(document.createElement('tr'), r);
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
      var tr = clickableRow(document.createElement('tr'), r);
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
      // 输列: 有仓位的已结算信号里判负的注数（>0 标红 = 一眼看出哪天在输）
      '<td class="' + (r.lost > 0 ? 'neg' : 'muted') + '">' + r.lost + '</td>' +
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

  // ── 点行看曲线弹窗（信号表 / 决策表任意一行）──
  // 画的是**那一行所属窗口**的完整曲线 + 一条标出该行时刻的竖线。数据两条路, 由服务端定:
  // 正好是内存里这一窗 ⇒ 实况; 否则从原始采集（events_*.jsonl）重建（决策 #31）。
  // 它是冻结的历史窗, 所以**不轮询**——1s 的 /api/curve 只喂主图。
  function openWindowCurve(es, ts) {
    // ⚠️ 先解锁遮罩再画: hidden 时 wrap.clientWidth === 0, drawChart 会直接 return（什么都不画）
    $('winCurveMask').hidden = false;
    winChart.data = null;
    winChart.hover = -1;
    winChart.mark = ts > 0 ? ts : 0;
    winChart.empty = '加载中…';
    $('winCurveNote').hidden = true;
    $('winCurveNote').textContent = '';
    $('winCurveSub').textContent = '加载中…';
    drawChart(winChart);

    if (!(es > 0)) { // 旧口径的行（frame / scan）没记 event_start, 定位不到窗口
      winChart.empty = '这一行没有窗口起点';
      $('winCurveSub').textContent = '无法定位窗口';
      $('winCurveNote').textContent = '旧口径的行（frame / scan）没记 event_start, 因此无法回读它的原始采集。';
      $('winCurveNote').hidden = false;
      drawChart(winChart);
      return;
    }
    $('winCurveTitle').textContent = '窗口曲线 · ' + fmtTime(es * 1000) + ' 起';
    fetchJSON('/api/curve?event_start=' + es, function (d) {
      d.points = d.points || [];
      var ch = winChart;
      ch.data = d;
      ch.hover = -1;
      if (d.points.length === 0) ch.empty = '这一窗没有曲线数据';
      var src = d.source === 'live' ? '实况采样' : (d.source === 'events' ? '原始采集重建' : '无数据');
      $('winCurveSub').textContent =
        (ts > 0 ? '点击时刻 ' + fmtTime(ts) + ' · ' : '') + d.points.length + ' 点 · ' + src;
      if (d.note) {
        $('winCurveNote').textContent = d.note;
        $('winCurveNote').hidden = false;
      }
      drawChart(ch);
    });
  }
  function closeWindowCurve() {
    $('winCurveMask').hidden = true;
  }
  $('winCurveClose').addEventListener('click', closeWindowCurve);
  $('winCurveMask').addEventListener('click', function (e) {
    if (e.target === this) closeWindowCurve();
  });
  // 行点击用**委托**（表体每次轮询整块重渲染, 逐行绑定会全丢）。单击即开: 双击的第一次
  // click 也会触发它, 所以「双击」同样能用; 而 iOS Safari 的双击是缩放, dblclick 不可靠。
  ['signalsTable', 'snapsTable'].forEach(function (id) {
    $(id).addEventListener('click', function (e) {
      var tr = e.target && e.target.closest ? e.target.closest('tr[data-es]') : null;
      if (!tr) return; // 落在表头/空白/分页上: 不是「点行」
      openWindowCurve(Number(tr.getAttribute('data-es')), Number(tr.getAttribute('data-ts')));
    });
  });

  // Esc 两个弹窗都关（逐日明细与窗口曲线）
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    closeDaily();
    closeWindowCurve();
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

  // 标定参数只服务闩锁闸值（T150/T60 的 rem 阈值）与 sd 标红（sigma_min_usd）
  fetchJSON('/api/config', function (c) { CFG = c; });

  // 曲线: 主图 1s 轮询 + 两图各自的悬停/触摸读数; 标签页在后台时不拉（无人在看）
  renderLegend($('curveLegend'));
  renderLegend($('winCurveLegend'));
  bindChartHover(mainChart);
  bindChartHover(winChart);
  // 宽度变化要两张都重画（弹窗关着时 clientWidth = 0, drawChart 自己会跳过）
  window.addEventListener('resize', function () { drawChart(mainChart); drawChart(winChart); });
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) tickCurve();
  });

  function tickCurve() {
    if (document.hidden) return;
    fetchJSON('/api/curve', onCurve);
  }

  setInterval(tick, stateInterval);
  setInterval(tickLists, listInterval);
  setInterval(tickCurve, curveInterval);
  tick();
  tickLists();
  tickCurve(); // 首帧: 立刻画出「等待本窗数据…」或上一窗的曲线
})();
