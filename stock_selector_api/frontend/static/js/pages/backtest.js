/* 回测页：发起回测（投递 Celery worker）+ 作业进度轮询 + 结果查看 */
(function () {
  const esc = AppUI.escapeHtml;

  const STATUS = {
    queued: ['排队中', 'gray'],
    running: ['运行中', 'orange'],
    done: ['已完成', 'green'],
    failed: ['失败', 'red'],
  };

  const FIELDS = [
    { key: 'start', label: '起始日期', type: 'date' },
    { key: 'end', label: '结束日期', type: 'date' },
    { key: 'strategy', label: '策略来源', type: 'select' },
    { key: 'rule_set_id', label: '规则集判据（留空=用 MongoDB trading_rules；与策略来源二选一）', type: 'rule_set' },
    { key: 'capital', label: '初始资金', type: 'number', step: '1000' },
    { key: 'max_positions', label: '持仓只数上限', type: 'number' },
    { key: 'position_cap_pct', label: '单票仓位上限（0~1）', type: 'number', step: '0.01' },
    { key: 'target_amount', label: '单笔买入金额（需小于初始资金×单票上限）', type: 'number', step: '100' },
    { key: 'universe', label: '股票池只数（0=全市场，仅策略模式生效）', type: 'number' },
    { key: 'max_hold_days', label: '最长持有交易日（0=不限）', type: 'number' },
    { key: 'stop_loss_pct', label: '止损比例（0=关闭）', type: 'number', step: '0.01' },
    { key: 'take_profit_pct', label: '止盈比例（0=关闭）', type: 'number', step: '0.01' },
    { key: 'commission_rate', label: '佣金率', type: 'number', step: '0.00001' },
    { key: 'commission_min', label: '最低佣金', type: 'number', step: '0.5' },
    { key: 'stamp_tax_rate', label: '卖出印花税率', type: 'number', step: '0.0001' },
  ];

  let opts = null;
  let curPage = 1;
  let timer = null;
  let host = null;

  function fmtMoney(v) {
    const n = Number(v);
    return Number.isFinite(n) ? n.toLocaleString('zh-CN', { maximumFractionDigits: 2 }) : '-';
  }

  function fmtRatio(v) {
    const n = Number(v);
    return Number.isFinite(n) ? (n * 100).toFixed(2) + '%' : '-';
  }

  function fmtNum(v, digits) {
    const n = Number(v);
    return Number.isFinite(n) ? n.toFixed(digits) : '-';
  }

  // 目标 ①+④ 的达标判定：单笔期望、赔率、胜率、利润因子四项**同时**过线才算达标。
  // 四个数缺一不可——赔率可以靠截断亏损凑出来而期望仍为负，胜率可以靠"多买多亏"
  // 压到 50% 以下换高赔率，利润因子在胜率很低时照样能过 2。所以这里逐项列出来，
  // 让人看见是哪一项没过，而不是只给一个"达标/不达标"的结论。
  const TARGETS = { payoff: 2.0, winRate: 0.5, profitFactor: 2.0, minTrades: 30 };

  function verdictHtml(m, tradeCount) {
    const n = Number(m.trade_count ?? tradeCount);
    const exp = Number(m.expectancy);
    const payoff = Number(m.payoff_ratio);
    const pf = Number(m.profit_factor);
    const wr = Number(m.win_rate);
    if (!Number.isFinite(exp)) return '';
    const items = [
      ['单笔期望>0', Number.isFinite(exp) && exp > 0, exp.toFixed(0) + ' 元'],
      ['赔率≥' + TARGETS.payoff.toFixed(1), Number.isFinite(payoff) && payoff >= TARGETS.payoff, fmtNum(payoff, 2)],
      ['胜率≥' + (TARGETS.winRate * 100).toFixed(0) + '%', Number.isFinite(wr) && wr >= TARGETS.winRate, fmtRatio(wr)],
      ['利润因子≥' + TARGETS.profitFactor.toFixed(1), Number.isFinite(pf) && pf >= TARGETS.profitFactor, fmtNum(pf, 2)],
      ['成交≥' + TARGETS.minTrades + ' 笔', Number.isFinite(n) && n >= TARGETS.minTrades, (Number.isFinite(n) ? n : '-') + ' 笔'],
    ];
    const all = items.every((it) => it[1]);
    const chips = items.map((it) =>
      '<span class="vd ' + (it[1] ? 'ok' : 'no') + '">' + (it[1] ? '✓' : '✗') + ' ' + esc(it[0]) +
      ' ' + esc(it[2]) + '</span>').join('');
    return '<div class="verdict"><b>' + (all ? '①+④ 全项达标' : '①+④ 未全项达标') +
      '</b><span class="hint">（胜率与赔率是两个口径：赔率=均盈/均亏，利润因子=总盈/总亏，' +
      '只有胜率过半时两者才同向）</span><div class="vd-list">' + chips + '</div></div>';
  }

  function fmtTime(value) {
    const s = String(value || '');    if (!s) return '';
    // SQLite 读回来是 naive UTC，补 Z 才能按本地时区显示；自带偏移的（Mongo）原样交给 Date
    const hasOffset = /Z$|[+-]\d{2}:?\d{2}$/.test(s);
    const d = new Date(hasOffset ? s : s.replace(' ', 'T') + 'Z');
    return Number.isNaN(d.getTime()) ? s : d.toLocaleString('zh-CN', { hour12: false });
  }

  function statusTag(job) {
    const s = STATUS[job.status] || [job.status, 'gray'];
    return '<span class="tag ' + s[1] + '">' + esc(s[0]) + '</span>';
  }

  function bar(job) {
    const pct = Math.max(0, Math.min(100, Number(job.progress) || 0));
    return '<div style="background:#f0f0f0;border-radius:3px;height:10px;min-width:90px">' +
      '<div style="width:' + pct + '%;height:10px;border-radius:3px;background:' +
      (job.status === 'failed' ? '#f56c6c' : '#409eff') + '"></div></div>' +
      '<span class="hint" style="font-size:12px">' + pct + '% ' +
      esc(job.stage || '') + '</span>';
  }

  function formHtml(sets, prefillId) {
    const d = opts.defaults || {};
    const strategyOptions = [{ value: '', label: '规则回测（MongoDB trading_rules）' }]
      .concat((opts.strategies || []).map((s) => ({ value: s, label: '选股策略同源：' + s })));
    const ruleSetOptions = [{ value: '', label: '不用规则集（按 MongoDB trading_rules 判定）' }]
      .concat((sets || []).map((s) => ({ value: s.id, label: s.name + (s.is_active ? '（生效中）' : '') })));
    if (prefillId && !ruleSetOptions.some((o) => o.value === prefillId)) {
      // 从规则集页跳转过来，但当前账号列不出规则集（或翻页没看到）：仍按 id 提交，
      // 判据串由后端注入快照，回测结论对的就是那一套规则
      ruleSetOptions.push({ value: prefillId, label: '规则集 ' + String(prefillId).slice(0, 8) + '…（规则集页跳转）' });
    }
    return FIELDS.map((f) => {
      if (f.type === 'rule_set') {
        return AppUI.fSelect(f.key, f.label, ruleSetOptions, prefillId || '');
      }
      const value = d[f.key] === null || d[f.key] === undefined ? '' : String(d[f.key]);
      if (f.type === 'select') {
        return AppUI.fSelect(f.key, f.label, strategyOptions, value);
      }
      const limit = (opts.numeric || {})[f.key] || {};
      const range = (limit.min !== null && limit.min !== undefined ? ' min="' + limit.min + '"' : '') +
        (limit.max !== null && limit.max !== undefined ? ' max="' + limit.max + '"' : '');
      return '<div class="frow"><label>' + esc(f.label) + '</label>' +
        '<input data-field="' + f.key + '" type="' + (f.type === 'date' ? 'date' : 'number') +
        '" step="' + (f.step || '1') + '"' + range + ' value="' + esc(value) + '"></div>';
    }).join('') +
      '<div class="chk"><input data-field="no_save" type="checkbox" id="no-save">' +
      '<label for="no-save" style="margin:0">仅试跑，不写回测结果库</label></div>';
  }

  function collect(form) {
    const params = {};
    FIELDS.forEach((f) => {
      const raw = form[f.key];
      if (raw === '' || raw === null || raw === undefined) return;
      params[f.key] = (f.type === 'number') ? Number(raw) : raw;
    });
    params.no_save = !!form.no_save;
    return params;
  }

  async function openRunForm() {
    if (!opts) opts = await API.backtestOptions();
    // 规则集页「回测」按钮留下的跳转：读一次就用掉，避免下次打开表单还带着旧规则
    const prefillId = sessionStorage.getItem('bt_prefill_rule_set') || '';
    sessionStorage.removeItem('bt_prefill_rule_set');
    const sets = App.can('rule_set:list')
      ? (await API.ruleSets({ page: 1, size: 100 })).items || []
      : [];
    AppUI.openForm('发起回测', formHtml(sets, prefillId), async (form) => {
      const data = await API.backtestRun(collect(form));
      AppUI.toast('回测任务已投递：' + data.job_id, 'success');
      await loadJobs();
    });
  }

  function sparkline(points) {
    if (!points || points.length < 2) return '<div class="hint">净值数据不足</div>';
    const w = 700, h = 150, pad = 8;
    const vals = points.map((p) => Number(p.total_value) || 0);
    const min = Math.min.apply(null, vals), max = Math.max.apply(null, vals);
    const span = (max - min) || 1;
    const step = (w - pad * 2) / (vals.length - 1);
    const poly = vals.map((v, i) =>
      (pad + i * step).toFixed(1) + ',' + (h - pad - ((v - min) / span) * (h - pad * 2)).toFixed(1)).join(' ');
    return '<svg viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none" ' +
      'style="width:100%;height:150px;background:#fafafa;border-radius:4px">' +
      '<polyline fill="none" stroke="#1890ff" stroke-width="1.5" points="' + poly + '"/></svg>' +
      '<div class="hint">' + esc(points[0].date) + ' ~ ' + esc(points[points.length - 1].date) +
      '，净值区间 ' + fmtMoney(min) + ' ~ ' + fmtMoney(max) + '</div>';
  }

  function tradesTable(trades) {
    if (!trades || !trades.length) return '<div class="hint">本次没有成交</div>';
    const head = ['日期', '代码', '名称', '方向', '价格', '股数', '金额', '费用', '盈亏'];
    const rows = trades.slice().reverse().map((t) => '<tr>' +
      [t.date, t.code, t.name, t.side, fmtMoney(t.price), t.shares, fmtMoney(t.amount),
        fmtMoney(t.fee), t.pnl === null || t.pnl === undefined ? '' : fmtMoney(t.pnl)]
        .map((v) => '<td>' + esc(v) + '</td>').join('') + '</tr>').join('');
    return '<div class="tbl-scroll"><table class="tbl"><thead><tr>' + head.map((h) => '<th>' + h + '</th>').join('') +
      '</tr></thead><tbody>' + rows + '</tbody></table></div>' +
      '<div class="hint">仅展示最近 ' + trades.length + ' 笔，全量成交见回测结果库</div>';
  }

  function detailHtml(job) {
    const r = job.result || {};
    const p = job.params || {};
    const m = r.metrics || {};
    const cards = [
      ['总收益', fmtRatio(m.total_return)], ['年化', fmtRatio(m.annual_return)],
      ['最大回撤', fmtRatio(m.max_drawdown)], ['胜率', fmtRatio(m.win_rate)],
      ['夏普', fmtNum(m.sharpe, 2)], ['利润因子(总盈/总亏)', fmtNum(m.profit_factor, 2)],
      ['赔率(均盈/均亏)', fmtNum(m.payoff_ratio, 2)], ['单笔期望(元)', fmtNum(m.expectancy, 0)],
    ];
    // 规则集回测要把当时注入的判据串亮出来：结果能不能和规则集页面对上，看这一行
    const snapshot = p.sell_rule
      ? '<div class="hint">判据快照（规则集 ' + esc(String(p.rule_set_id || '-')) + '）：卖出 ' +
        esc(p.sell_rule) + ' ｜ 加仓 ' + esc(p.add_rule || '无') + '</div>'
      : '';
    return '<div class="hint">策略 ' + esc(job.result ? r.strategy : '-') + ' · ' +
      esc(r.start_date || '') + ' ~ ' + esc(r.end_date || '') +
      ' · 内核任务号 ' + esc(r.task_id || '-') + '</div>' + snapshot +
      '<div class="cards">' + cards.map((c) =>
        '<div class="card num" style="min-width:110px">' + esc(c[0]) + '<b style="font-size:20px">' +
        esc(c[1]) + '</b></div>').join('') + '</div>' +
      sparkline(r.equity) + verdictHtml(m, r.trade_count) + tradesTable(r.recent_trades);
  }

  function openDetailMask(title, bodyHtml) {
    document.querySelector('.modal-mask')?.remove();
    const mask = document.createElement('div');
    mask.className = 'modal-mask';
    mask.innerHTML = '<div class="modal" style="width:780px;max-width:94vw">' +
      '<div class="modal-title">' + esc(title) + '<span class="modal-close" data-close>×</span></div>' +
      '<div class="modal-body">' + bodyHtml + '</div>' +
      '<div class="modal-foot"><button class="btn" data-close>关闭</button></div></div>';
    document.body.appendChild(mask);
    mask.querySelectorAll('[data-close]').forEach((el) =>
      el.addEventListener('click', () => mask.remove()));
  }

  async function showDetail(jobId) {
    const job = await API.backtestRunDetail(jobId);
    if (job.status !== 'done') {
      openDetailMask('回测作业 ' + job.job_id,
        statusTag(job) + '<div class="hint">' + esc(job.error || job.stage || '尚未产出结果') + '</div>');
      return;
    }
    openDetailMask('回测结果 · ' + job.job_id, detailHtml(job));
  }

  const COLUMNS = [
    { label: '创建时间', width: '150px', render: (r) => esc(fmtTime(r.created_at)) },
    { label: '策略', width: '140px', render: (r) => {
      const p = r.params || {};
      if (p.strategy) return esc(p.strategy);
      // 规则集回测和裸规则回测必须看得出区别，否则「用这套规则跑出来的结果」没法对账
      return p.rule_set_id ? esc('规则集 ' + String(p.rule_set_id).slice(0, 8)) : '规则回测';
    } },
    { label: '区间', width: '170px', render: (r) => esc((r.params || {}).start + ' ~ ' + (r.params || {}).end) },
    { label: '状态', width: '90px', render: statusTag },
    { label: '进度', width: '200px', render: bar },
    { label: '总收益', width: '90px', render: (r) => (r.result ? esc(fmtRatio(r.result.metrics && r.result.metrics.total_return)) : '-') },
    { label: '成交', width: '70px', render: (r) => (r.result ? r.result.trade_count : '-') },
    { label: '说明', render: (r) => '<span class="hint">' + esc(r.error || '') + '</span>' },
  ];

  async function loadJobs() {
    const el = document.getElementById('bt-jobs');
    if (!el) return;
    const data = await API.backtestRuns({ page: curPage, size: 20 });
    const rows = (data.items || []).map((r) => ({ ...r, id: r.job_id }));
    AppTable.renderTable(el, {
      columns: COLUMNS,
      rows,
      total: data.total,
      page: curPage,
      size: 20,
      onPage: (p) => { curPage = p; loadJobs(); },
      actions: [{
        key: 'detail',
        label: '查看',
        visible: (r) => r.status !== 'queued' && r.status !== 'running',
        onClick: (r) => showDetail(r.job_id),
      }],
    });
    // 有未结束的作业才继续轮询，全部落定就停掉定时器
    const pending = rows.some((r) => r.status === 'queued' || r.status === 'running');
    stopPolling();
    if (pending && host && host.isConnected) {
      timer = setInterval(() => {
        if (!host || !host.isConnected) { stopPolling(); return; }
        loadJobs().catch(() => stopPolling());
      }, 4000);
    }
    refreshWorkerChip();
  }

  function refreshWorkerChip() {
    const chip = document.getElementById('bt-worker');
    if (!chip) return;
    API.backtestWorkers().then((w) => {
      chip.innerHTML = w.online
        ? '<span class="tag green">worker 在线：' + esc(w.workers.join(', ')) + '</span>'
        : '<span class="tag red">worker 不在线，任务会一直排队</span>';
    }).catch(() => { /* 探测失败不影响列表 */ });
  }

  function stopPolling() {
    if (timer) { clearInterval(timer); timer = null; }
  }

  async function render(el) {
    host = el;
    stopPolling();
    if (!opts) opts = await API.backtestOptions();
    el.innerHTML =
      '<div class="card"><div class="toolbar">' +
      '<button class="btn btn-primary" id="bt-run">发起回测</button>' +
      '<span class="hint" id="bt-worker"></span>' +
      (App.can('backtest:run') ? '' : '<span class="hint">当前账号缺少 backtest:run 权限，只能查看历史作业</span>') +
      '</div><div class="hint">回测由 Celery worker 执行内核，后端只投递与查询进度；' +
      '明细结果写入回测结果库（MongoDB backtest_results / trades / equity）。</div></div>' +
      '<div id="bt-jobs"></div>';
    if (App.can('backtest:run')) {
      document.getElementById('bt-run').addEventListener('click', () =>
        openRunForm().catch((e) => AppUI.toast(e.message, 'error')));
    } else {
      document.getElementById('bt-run').disabled = true;
    }
    await loadJobs();
  }

  App.registerPage('/backtest', { render });
})();
