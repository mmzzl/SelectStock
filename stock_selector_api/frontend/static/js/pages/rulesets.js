/* 规则集页：自定义卖出/加仓判据 → 「应用」切换线上生效判定并投递全市场回放算持有可信度 */
(function () {
  const esc = AppUI.escapeHtml;

  let opts = null;          // /rule-sets/options
  let rows = [];
  let curPage = 1;
  let timer = null;
  let host = null;
  let active = null;        // 当前生效的规则集快照

  const REPLAY = {
    queued: ['排队中', 'gray'],
    running: ['回放中', 'orange'],
    done: ['已回放', 'green'],
    failed: ['回放失败', 'red'],
  };

  function fmtTime(value) {
    const s = String(value || '');
    if (!s) return '';
    // SQLite 读回来是 naive UTC，补 Z 才能按本地时区显示
    const hasOffset = /Z$|[+-]\d{2}:?\d{2}$/.test(s);
    const d = new Date(hasOffset ? s : s.replace(' ', 'T') + 'Z');
    return Number.isNaN(d.getTime()) ? s : d.toLocaleString('zh-CN', { hour12: false });
  }

  function fmtNum(v, digits) {
    const n = Number(v);
    return Number.isFinite(n) ? n.toFixed(digits) : '-';
  }

  function fmtPct(v, digits) {
    const n = Number(v);
    return Number.isFinite(n) ? n.toFixed(digits === undefined ? 2 : digits) + '%' : '-';
  }

  // lift≈1 或配对 t 不够，就说明这个状态和随机挑一只没差别；页面必须把这话说出来
  function isFlat(r) {
    const lift = Number(r && r.lift), t = Math.abs(Number(r && r.vs_base_t));
    if (!Number.isFinite(lift) || !Number.isFinite(t)) return true;
    return Math.abs(lift - 1) < 0.03 || t < 2;
  }

  // ---------- 条件编辑器（state → DOM → state，没有框架就靠这一条闭环） ----------
  function fieldSelect(key, value) {
    const groups = [];
    (opts.fields || []).forEach((f) => {
      if (!groups.length || groups[groups.length - 1].group !== f.group) {
        groups.push({ group: f.group, items: [] });
      }
      groups[groups.length - 1].items.push(f);
    });
    return '<select class="mini-sel mini-wide" data-k="' + key + '">' +
      groups.map((g) => '<optgroup label="' + esc(g.group) + '">' +
        g.items.map((f) => '<option value="' + esc(f.field) + '"' +
          (f.field === value ? ' selected' : '') + '>' + esc(f.label) + '</option>').join('') +
        '</optgroup>').join('') + '</select>';
  }

  function modeOf(cond) {
    if (cond.ref) return (cond.plus && cond.plus.field) ? 'ref_plus' : 'ref';
    return 'value';
  }

  function rightControls(cond) {
    const mode = modeOf(cond);
    const num = (v) => (v === undefined || v === null ? '' : esc(v));
    let out = '<select class="mini-sel" data-k="mode">' +
      '<option value="value"' + (mode === 'value' ? ' selected' : '') + '>阈值</option>' +
      '<option value="ref"' + (mode === 'ref' ? ' selected' : '') + '>列 × 系数</option>' +
      '<option value="ref_plus"' + (mode === 'ref_plus' ? ' selected' : '') + '>列 × 系数 ± 列</option>' +
      '</select>';
    if (mode === 'value') {
      return out + '<input class="mini-num" data-k="value" type="number" step="any" placeholder="阈值"' +
        ' value="' + num(cond.value) + '">';
    }
    out += fieldSelect('ref', cond.ref || 'ma20') +
      '<span class="muted small">×</span>' +
      '<input class="mini-num" data-k="coef" type="number" step="0.01" placeholder="1"' +
      ' value="' + num(cond.coef) + '">';
    if (mode === 'ref') return out;
    const plus = cond.plus || {};
    return out + '<select class="mini-sel" data-k="plus_sign">' +
      '<option value="+"' + (plus.sign !== '-' ? ' selected' : '') + '>+ 列 × 系数</option>' +
      '<option value="-"' + (plus.sign === '-' ? ' selected' : '') + '>− 列 × 系数</option></select>' +
      fieldSelect('plus_field', plus.field || 'atr14') +
      '<span class="muted small">×</span>' +
      '<input class="mini-num" data-k="plus_coef" type="number" step="0.01" placeholder="1"' +
      ' value="' + num(plus.coef) + '">';
  }

  function condRow(side, ri, ci, cond) {
    return '<div class="row-card rs-cond" data-side="' + side + '" data-rule="' + ri +
      '" data-cond="' + ci + '">' +
      fieldSelect('field', cond.field || 'close') +
      '<select class="mini-sel" data-k="op">' +
      (opts.ops || []).map((op) => '<option value="' + esc(op) + '"' +
        (cond.op === op ? ' selected' : '') + '>' + esc(op) + '</option>').join('') +
      '</select>' + rightControls(cond) +
      '<a class="act" data-del-cond>删条件</a></div>';
  }

  function ruleCard(side, ri, rule) {
    const conds = rule.conditions || [];
    const head = '<div class="row-card">' +
      '<input class="mini-sel mini-wide" data-k="label" placeholder="规则名（会进告警文案）"' +
      ' value="' + esc(rule.label || '') + '">' +
      '<button class="btn mini" data-add-cond="' + side + '" data-rule="' + ri + '">＋条件</button>' +
      '<button class="btn mini danger" data-del-rule="' + side + '" data-rule="' + ri + '">删规则</button>' +
      '</div>';
    const cap = conds.length >= opts.max_conditions
      ? '<div class="hint small">一条规则最多 ' + opts.max_conditions + ' 个条件（同条内是「且」）。</div>' : '';
    const empty = conds.length ? '' :
      '<div class="hint small">还没有条件：空规则保存时会被丢掉。</div>';
    return '<div class="rs-rule">' + head +
      conds.map((c, ci) => condRow(side, ri, ci, c)).join('') + empty + cap + '</div>';
  }

  function sideBlock(title, side, list) {
    const addBtn = list.length >= opts.max_rules ? '' :
      '<button class="btn mini" data-add-rule="' + side + '">＋添加' + title + '</button>';
    return '<div data-side-block="' + side + '">' +
      '<div class="card-head"><h3>' + esc(title) + '</h3><span class="hint">一条规则内是「且」，' +
      '多条规则之间是「或」：任一触发就执行。最多 ' + opts.max_rules + ' 条。</span></div>' +
      list.map((r, ri) => ruleCard(side, ri, r)).join('') + addBtn + '</div>';
  }

  function editorBody(state) {
    const d = opts.disaster;
    return '<div class="frow"><label>名称</label><input id="rs-name" value="' + esc(state.name) + '"></div>' +
      '<div class="frow"><label>说明（为什么这么定，方便日后回看）</label>' +
      '<input id="rs-remark" value="' + esc(state.remark) + '"></div>' +
      '<div class="frow"><label>灾难线：浮亏超过该百分比立即卖出（%，范围 ' + d.min + '~' + d.max + '）</label>' +
      '<input id="rs-disaster" type="number" min="' + d.min + '" max="' + d.max + '" step="0.5" value="' +
      esc(state.disaster_stop_pct) + '"></div>' +
      sideBlock('卖出规则', 'sell', state.sell) +
      sideBlock('加仓规则', 'add', state.add) +
      '<div class="hint">买入判据不在这里改（仍由选股规则决定）；风控那几条（ST、流动性、涨跌停可成交等）' +
      '照常先跑，这套规则只改「何时卖 / 何时加」。</div>';
  }

  function collectSide(mask, side) {
    const scope = mask.querySelector('[data-side-block="' + side + '"]');
    if (!scope) return [];
    const out = [];
    scope.querySelectorAll('.rs-rule').forEach((ruleEl) => {
      const labelEl = ruleEl.querySelector('[data-k="label"]');
      out.push({
        label: labelEl ? labelEl.value : '',
        conditions: Array.from(ruleEl.querySelectorAll('.rs-cond')).map(readCond),
      });
    });
    return out;
  }

  function readCond(el) {
    const node = (k) => el.querySelector('[data-k="' + k + '"]');
    const val = (k) => {
      const n = node(k);
      if (!n) return null;
      const s = String(n.value).trim();
      return s === '' ? null : Number(s);
    };
    const text = (k) => (node(k) ? node(k).value : null);
    const cond = { field: text('field'), op: text('op') };
    if ((text('mode') || 'value') === 'value') {
      cond.value = val('value');
      return cond;
    }
    cond.ref = text('ref');
    cond.coef = val('coef');
    if (text('mode') === 'ref_plus') {
      cond.plus = { sign: text('plus_sign'), field: text('plus_field'), coef: val('plus_coef') };
    }
    return cond;
  }

  function syncState(mask, state) {
    state.name = (mask.querySelector('#rs-name') || {}).value || '';
    state.remark = (mask.querySelector('#rs-remark') || {}).value || '';
    const disaster = (mask.querySelector('#rs-disaster') || {}).value;
    if (disaster !== '' && disaster != null) state.disaster_stop_pct = Number(disaster);
    state.sell = collectSide(mask, 'sell');
    state.add = collectSide(mask, 'add');
    return state;
  }

  function repaint(mask, state) {
    mask.querySelector('.modal-body').innerHTML = editorBody(state);
  }

  function bindEditor(mask) {
    const body = mask.querySelector('.modal-body');
    const rerender = () => repaint(mask, syncState(mask, mask.__rsState));
    // 右值类型决定这一行渲染哪几个控件，只能整体重绘这一侧
    body.addEventListener('change', (e) => {
      if (e.target && e.target.dataset.k === 'mode') rerender();
    });
    body.addEventListener('click', (e) => {
      const t = e.target.closest('button,a');
      if (!t) return;
      const s = syncState(mask, mask.__rsState);
      if (t.dataset.addRule) {
        s[t.dataset.addRule].push({ label: '', conditions: [{ field: 'close', op: '<', value: null }] });
      } else if (t.dataset.addCond) {
        s[t.dataset.addCond][Number(t.dataset.rule)].conditions.push({ field: 'close', op: '<', value: null });
      } else if (t.dataset.delRule) {
        s[t.dataset.delRule].splice(Number(t.dataset.rule), 1);
      } else if (t.hasAttribute('data-del-cond')) {
        const row = t.closest('.rs-cond');
        s[row.dataset.side][Number(row.dataset.rule)].conditions.splice(Number(row.dataset.cond), 1);
      } else {
        return;
      }
      repaint(mask, s);
    });
  }

  function defaultState() {
    return {
      name: '', remark: '', disaster_stop_pct: opts.disaster.default,
      sell: [
        { label: 'RSI 超买兑现', conditions: [{ field: 'rsi14', op: '>', value: 90 }] },
        { label: '跌破 10 日线 5%', conditions: [{ field: 'close', op: '<=', ref: 'ma10', coef: 0.95 }] },
      ],
      add: [{ label: '站上 20 日线', conditions: [{ field: 'close', op: '>', ref: 'ma20' }] }],
    };
  }

  function stateFromRow(row) {
    const side = (arr) => (arr || []).map((r) =>
      ({ label: r.label || '', conditions: r.conditions || [] }));
    return {
      name: row.name || '', remark: row.remark || '',
      disaster_stop_pct: row.disaster_stop_pct,
      sell: side(row.sell_conditions), add: side(row.add_conditions),
    };
  }

  async function openEditor(row) {
    if (!opts) opts = await API.ruleSetOptions();
    const state = row ? stateFromRow(await API.ruleSetDetail(row.id)) : defaultState();
    document.querySelector('.modal-mask')?.remove();
    const mask = document.createElement('div');
    mask.className = 'modal-mask';
    mask.innerHTML = '<div class="modal" style="width:920px;max-width:96vw">' +
      '<div class="modal-title">' + (row ? '编辑规则集' : '新建规则集') +
      '<span class="modal-close" data-close>×</span></div>' +
      '<div class="modal-body">' + editorBody(state) + '</div>' +
      '<div class="modal-foot"><button class="btn" data-close>取消</button>' +
      '<button class="btn btn-primary" id="rs-save">保存</button></div></div>';
    document.body.appendChild(mask);
    mask.__rsState = state;
    bindEditor(mask);
    mask.querySelectorAll('[data-close]').forEach((el) =>
      el.addEventListener('click', () => mask.remove()));

    mask.querySelector('#rs-save').addEventListener('click', async () => {
      const s = syncState(mask, mask.__rsState);
      const body = {
        name: (s.name || '').trim(), remark: s.remark,
        disaster_stop_pct: Number(s.disaster_stop_pct),
        sell_conditions: s.sell, add_conditions: s.add,
      };
      if (!body.name) { AppUI.toast('名称不能为空', 'error'); return; }
      if (!body.sell_conditions.some((r) => (r.conditions || []).length)) {
        AppUI.toast('至少要有一条带条件的卖出规则：「全部未触发」这个状态就是这么定义的', 'error');
        return;
      }
      try {
        const saved = row ? await API.updateRuleSet(row.id, body) : await API.createRuleSet(body);
        AppUI.toast('已保存：' + saved.name + '。保存不等于生效，点「应用」才会切换线上判定。', 'success');
        mask.remove();
        await loadRows();
      } catch (e) { /* toast 已在 API 层提示，弹窗留着好改 */ }
    });
  }

  // ---------- 列表 ----------
  function replayCell(row) {
    const st = REPLAY[row.replay_status];
    if (!st) return '<span class="tag gray">未回放</span>';
    const pct = Math.max(0, Math.min(100, Number(row.replay_progress) || 0));
    const bar = (row.replay_status === 'queued' || row.replay_status === 'running')
      ? '<div style="background:#f0f0f0;border-radius:3px;height:8px;min-width:90px;margin-top:4px">' +
        '<div style="width:' + pct + '%;height:8px;border-radius:3px;background:#409eff"></div></div>' +
        '<span class="hint small">' + pct + '% ' + esc(row.replay_stage || '') + '</span>' : '';
    const err = row.replay_status === 'failed' && row.replay_error
      ? '<div class="hint small">' + esc(String(row.replay_error).slice(0, 90)) + '</div>' : '';
    return '<span class="tag ' + st[1] + '">' + esc(st[0]) + '</span>' + bar + err;
  }

  function confidenceCell(row) {
    if (row.hold_confidence_pct === null || row.hold_confidence_pct === undefined) {
      const note = row.hold_confidence_note ||
        (row.replay_status === 'done' ? '没有可显示的数' : '回放跑完才有持有可信度');
      return '<span class="hint">' + esc(note) + '</span>';
    }
    const flat = isFlat({ lift: row.hold_confidence_lift, vs_base_t: row.hold_confidence_t });
    return '<b style="font-size:16px">' + fmtPct(row.hold_confidence_pct) + '</b>' +
      '<div class="vd-list" style="margin-top:2px">' +
      '<span class="hint small">基准 ' + fmtPct(row.hold_confidence_base_pct) +
      ' · lift ' + fmtNum(row.hold_confidence_lift, 3) +
      ' · t ' + fmtNum(row.hold_confidence_t, 2) + '</span>' +
      (flat ? '<span class="vd no">无区分度</span>' : '<span class="vd ok">有区分度</span>') + '</div>';
  }

  function rulesCell(row) {
    const labels = (arr) => (arr || []).map((r) => r.label || '未命名').join('、') || '—';
    return '<div class="small">' + esc(labels(row.sell_conditions)) + '</div>' +
      '<div class="hint small">加仓：' + esc(labels(row.add_conditions)) + '</div>';
  }

  const COLUMNS = [
    { label: '名称', width: '150px', render: (r) => esc(r.name) +
      (r.is_active ? ' <span class="tag green">生效中</span>' : '') },
    { label: '规则', render: rulesCell },
    { label: '灾难线', width: '70px', render: (r) => fmtNum(r.disaster_stop_pct, 1) + '%' },
    { label: '持有可信度', width: '190px', render: confidenceCell },
    { label: '回放', width: '150px', render: replayCell },
    { label: '更新', width: '150px', render: (r) => '<span class="small">' + esc(fmtTime(r.updated_at)) + '</span>' },
  ];

  const ACTIONS = [
    { key: 'edit', label: '编辑', perm: 'rule_set:update',
      onClick: (r) => openEditor(r).catch((e) => AppUI.toast(e.message, 'error')) },
    { key: 'apply', label: '应用', perm: 'rule_set:apply', visible: (r) => !r.is_active, onClick: applyNow },
    { key: 'replay', label: '重跑回放', perm: 'rule_set:apply',
      visible: (r) => r.is_active && r.replay_status !== 'queued' && r.replay_status !== 'running',
      onClick: replayNow },
    { key: 'backtest', label: '回测', perm: 'backtest:run', onClick: gotoBacktest },
    { key: 'detail', label: '可信度', onClick: openDetail },
    { key: 'del', label: '删除', perm: 'rule_set:delete', onClick: deleteNow },
  ];

  async function loadRows() {
    if (!host || !host.isConnected) return;
    const el = host.querySelector('#rs-jobs');
    if (!el) return;
    const [page, act] = await Promise.all([
      API.ruleSets({ page: curPage, size: 20 }),
      API.ruleSetActive().catch(() => null),
    ]);
    rows = page.items || [];
    active = act;
    AppTable.renderTable(el, {
      columns: COLUMNS,
      rows,
      actions: ACTIONS.filter((a) => !a.perm || App.can(a.perm)),
      total: page.total || 0,
      page: curPage,
      size: 20,
      onPage: (p) => { curPage = p; loadRows(); },
    });
    renderStatusCard();
    const pending = rows.some((r) => r.replay_status === 'queued' || r.replay_status === 'running');
    stopPolling();
    if (pending) timer = setInterval(() => loadRows().catch(() => stopPolling()), 6000);
  }

  function renderStatusCard() {
    const el = host && host.querySelector('#rs-active');
    if (!el) return;
    el.innerHTML = active
      ? '<span class="tag green">当前生效：' + esc(active.name) + '</span>' +
        '<span class="hint">持仓判定、盘中告警与持有可信度都按这套规则；生效文件已写盘，8002 插件热读。</span>'
      : '<span class="tag gray">未应用任何规则集</span>' +
        '<span class="hint">持仓判定与可信度退回代码里的默认规则（那两条从未标定过，建议先定一套再应用）。</span>';
  }

  function stopPolling() {
    if (timer) { clearInterval(timer); timer = null; }
  }

  function gotoBacktest(row) {
    // 回测页按 rule_set_id 让后端注入这套判据的快照，作业参数里记的就是那一刻的规则
    sessionStorage.setItem('bt_prefill_rule_set', row.id);
    window.location.hash = '#/backtest';
  }

  async function applyNow(row) {
    const tip = '应用「' + row.name + '」后：持仓判定与盘中告警立即改用这套卖出/加仓规则，' +
      '并投递一次全市场回放（约 1 小时）来算持有可信度。确认应用？';
    if (!AppUI.confirmBox(tip)) return;
    try {
      const out = await API.applyRuleSet(row.id);
      const rep = out.replay || {};
      if (rep.status === 'failed') {
        AppUI.toast('规则已生效，但回放没投出去：' + (rep.error || ''), 'error');
      } else if ((out.cancelled_replays || []).length) {
        AppUI.toast('已应用；旧的在跑回放已作废，新回放已投递', 'success');
      } else {
        AppUI.toast('已应用，回放已投递（跑完才有持有可信度）', 'success');
      }
      await loadRows();
    } catch (e) { /* toast 已提示 */ }
  }

  async function replayNow(row) {
    try {
      await API.replayRuleSet(row.id);
      AppUI.toast('回放任务已投递', 'success');
      await loadRows();
    } catch (e) { /* toast 已提示 */ }
  }

  async function deleteNow(row) {
    const tip = row.is_active
      ? '删除正在生效的「' + row.name + '」：持仓判定与可信度会退回代码里的默认规则。确认删除？'
      : '删除规则集「' + row.name + '」？确认删除？';
    if (!AppUI.confirmBox(tip)) return;
    try {
      const out = await API.deleteRuleSet(row.id);
      AppUI.toast(out.reverted_to_default ? '已删除，判定退回默认规则' : '已删除', 'success');
      await loadRows();
    } catch (e) { /* toast 已提示 */ }
  }

  // ---------- 可信度详情 ----------
  function statCards(p) {
    const j = p.joint_hold || {};
    const u = p.universe || {};
    const items = [
      ['持有可信度（全部未触发）', p.hold_confidence_pct == null ? '样本不足' : fmtPct(p.hold_confidence_pct)],
      ['全体基准', fmtPct(j.base_pct)],
      ['lift', fmtNum(j.lift, 3)],
      ['配对 t', fmtNum(j.vs_base_t, 2)],
      ['样本 N', (j.n || 0) + ' 个股·日'],
      ['覆盖', (u.days || '-') + ' 交易日 / ' + (u.files || '-') + ' 只'],
    ];
    return '<div class="cards">' + items.map((c) =>
      '<div class="card num" style="min-width:120px">' + esc(c[0]) +
      '<b style="font-size:18px">' + esc(c[1]) + '</b></div>').join('') + '</div>';
  }

  function ruleStats(list, kind) {
    if (!list || !list.length) return '';
    const title = kind === 'sell' ? '卖出规则逐条回放' : '加仓规则逐条回放';
    const head = ['规则', '触发样本', '交易日', '未跌超3%', '涨超3%', '跌超8%', '中位收益', '20日超额', '判定'];
    const body = list.map((r) => {
      const verdict = !r.enough ? '<span class="vd no">样本不足</span>'
        : (isFlat(r) ? '<span class="vd no">无区分度</span>' : '<span class="vd ok">有区分度</span>');
      const cells = [r.label, (r.n || 0) + ' 个股·日', r.days || 0, fmtPct(r.flat_pct),
        fmtPct(r.fly_pct), fmtPct(r.tail8_pct), fmtPct(r.median_ret_pct, 2),
        fmtNum(r.exc20_pp, 2) + 'pp'];
      return '<tr>' + cells.map((v) => '<td>' + esc(v) + '</td>').join('') +
        '<td>' + verdict + '</td></tr>';
    }).join('');
    return '<div class="card-head"><h3>' + title + '</h3></div>' +
      '<div class="tbl-scroll"><table class="tbl"><thead><tr>' +
      head.map((h) => '<th>' + h + '</th>').join('') + '</tr></thead><tbody>' + body +
      '</tbody></table></div>';
  }

  function exprBlock(row) {
    return '<div class="card" style="margin-top:12px"><div class="hint small">卖出判据 <code>' +
      esc(row.sell_expr || '-') + '</code><br>加仓判据 <code>' +
      esc(row.add_expr || '-') + '</code><br>用到的预处理字段：' +
      esc(row.used_fields || '-') + '</div></div>';
  }

  function detailHtml(row) {
    const st = REPLAY[row.replay_status];
    const tag = st ? '<span class="tag ' + st[1] + '">' + esc(st[0]) + '</span> ' : '';
    const p = row.posterior;
    if (!p) {
      return tag + '<div class="hint">这条规则集还没有回放结果。' +
        (row.is_active ? '点「重跑回放」会投递一次全市场扫描。' : '「应用」后才会投递全市场回放——可信度描述的是当前生效的这套规则。') +
        '</div>' + exprBlock(row);
    }
    const c = (opts && opts.replay_contract) || {};
    return '<div class="hint">口径：观测窗 T+' + (c.horizon_days || 20) + ' 个交易日、日等权、' +
      '不含成本；样本门槛 N≥' + (c.min_samples || 200) + '、覆盖≥' + (c.min_days || 30) + ' 个交易日。' +
      esc(p.hold_confidence_note || '') + '</div>' + statCards(p) +
      '<div class="verdict"><b>持有可信度是什么</b><span class="hint">' +
      '历史上「全部卖出规则同时都不触发」的那些时点里，后 20 日没有跌破 −3% 的占比。' +
      '这是状态频率，不是个股预测；lift≈1 就等于「和随机挑一只没差别」。</span></div>' +
      ruleStats(p.sell_rules, 'sell') + ruleStats(p.add_rules, 'add') + exprBlock(row);
  }

  async function openDetail(row) {
    const full = await API.ruleSetDetail(row.id);
    document.querySelector('.modal-mask')?.remove();
    const mask = document.createElement('div');
    mask.className = 'modal-mask';
    mask.innerHTML = '<div class="modal" style="width:880px;max-width:95vw">' +
      '<div class="modal-title">' + esc(full.name) + ' · 持有可信度' +
      '<span class="modal-close" data-close>×</span></div>' +
      '<div class="modal-body">' + detailHtml(full) + '</div>' +
      '<div class="modal-foot"><button class="btn" data-close>关闭</button></div></div>';
    document.body.appendChild(mask);
    mask.querySelectorAll('[data-close]').forEach((el) =>
      el.addEventListener('click', () => mask.remove()));
  }

  async function render(el) {
    host = el;
    stopPolling();
    if (!opts) opts = await API.ruleSetOptions();
    el.innerHTML =
      '<div class="card"><div class="toolbar">' +
      '<button class="btn btn-primary" id="rs-new">新建规则集</button>' +
      '<span id="rs-active"></span></div>' +
      '<div class="hint">条件从预处理好的日线字段里选（RSI、均线、ATR、量能、形态列等）。' +
      '保存只是存草稿；点「应用」才会切走线上判定，同一时刻只有一套规则生效，删掉生效那条即退回默认规则。</div></div>' +
      '<div id="rs-jobs"></div>';
    const btn = document.getElementById('rs-new');
    btn.addEventListener('click', () => openEditor(null).catch((e) => AppUI.toast(e.message, 'error')));
    if (!App.can('rule_set:create')) btn.disabled = true;
    await loadRows();
  }

  App.registerPage('/rulesets', { render });
})();
