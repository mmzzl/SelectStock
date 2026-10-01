/* 订阅策略页：多条策略列表 + 新增/编辑/启停/删除。
   一条策略 = 一组事件订阅（各自节流间隔）+ 绑定渠道实例（含邮件收件人）。 */
(function () {
  let META = { events: [], channel_types: [] };
  let CHANNELS = [];
  let LIST = [];
  let EDITING = null;   // 正在编辑的策略对象（null=新增）
  let ROOT = null;      // 页面容器

  function esc(s) { return AppUI.escapeHtml(s); }

  function fmtTime(value) {
    const s = String(value || '');
    if (!s) return '';
    const d = new Date(s);
    return Number.isNaN(d.getTime()) ? s : d.toLocaleString('zh-CN', { hour12: false });
  }

  function canWrite() { return App.can('sys:notify:update'); }

  function render(el) {
    ROOT = el;
    el.innerHTML = `
      <div class="card">
        <div class="card-head"><h3>订阅策略</h3></div>
        <div class="muted small" style="margin-bottom:10px">
          一条策略 = 若干事件订阅（各自节流）+ 绑定渠道。多条策略订阅同一事件互不影响，可分别投递给不同收件人。
        </div>
        <div id="pl-list" class="muted">加载中…</div>
      </div>
      <div id="pl-editor"></div>`;
    API.notifyEvents().then((d) => {
      META = d;
      return Promise.all([API.notifyPolicies(), API.notifyChannels()]);
    }).then(([pl, chs]) => {
      LIST = pl; CHANNELS = chs;
      drawList();
    }).catch(() => {});
  }

  function subscribedEvents(p) {
    return META.events.filter((ev) => ((p.events || {})[ev.code] || {}).is_on);
  }

  function boundChannels(p) {
    return Object.keys((p.channels || {})).filter((t) => (p.channels[t] || {}).is_on);
  }

  function drawList() {
    const wrap = document.getElementById('pl-list');
    AppTable.renderTable(wrap, {
      columns: [
        { key: 'name', label: '名称', width: '170px', render: (r) => esc(r.name) },
        { key: 'is_on', label: '状态', width: '80px', render: (r) =>
          r.is_on ? '<span class="tag green">启用</span>' : '<span class="tag gray">停用</span>' },
        { key: 'events', label: '订阅事件', render: (r) => {
          const evs = subscribedEvents(r);
          if (!evs.length) return '<span class="muted">未订阅任何事件</span>';
          return esc(evs.map((e) => e.label).join('、'));
        } },
        { key: 'channels', label: '投递渠道', width: '190px', render: (r) => {
          const cts = boundChannels(r);
          if (!cts.length) return '<span class="muted">未绑定渠道</span>';
          return esc(cts.map((t) => {
            const cfg = r.channels[t] || {};
            const meta = META.channel_types.find((x) => x.type === t) || { label: t };
            return meta.label + (cfg.channel_key ? '(' + cfg.channel_key + ')' : '(未选实例)');
          }).join('、'));
        } },
        { key: 'updated_at', label: '更新', width: '150px', render: (r) => esc(fmtTime(r.updated_at)) },
      ],
      rows: LIST,
      total: LIST.length,
      page: 1,
      size: Math.max(LIST.length, 1),
      toolbar: canWrite() ? ['<button class="btn btn-primary btn-sm" id="add-pl">新增策略</button>'] : [],
      actions: [
        { key: 'toggle', label: '停用', visible: (r) => canWrite() && r.is_on, onClick: toggle },
        { key: 'toggle', label: '启用', visible: (r) => canWrite() && !r.is_on, onClick: toggle },
        { key: 'edit', label: '编辑', visible: () => canWrite(), onClick: (r) => openEditor(r) },
        { key: 'del', label: '删除', visible: () => canWrite(), onClick: remove },
      ],
    });
    document.getElementById('add-pl')?.addEventListener('click', () => openEditor(null));
  }

  function reload() {
    return API.notifyPolicies().then((pl) => { LIST = pl; drawList(); }).catch(() => {});
  }

  function toggle(row) {
    API.notifyPolicyUpsert(row.id, {
      name: row.name, is_on: !row.is_on, events: row.events, channels: row.channels,
    }).then(() => {
      AppUI.toast(row.is_on ? '策略已停用' : '策略已启用', 'success');
      return reload();
    }).catch(() => {});
  }

  function remove(row) {
    if (!AppUI.confirmBox('确认删除订阅策略「' + row.name + '」？')) return;
    API.notifyPolicyDelete(row.id).then(() => {
      AppUI.toast('策略已删除', 'success');
      return reload();
    }).catch(() => {});
  }

  // ---------------- 编辑器 ----------------

  function openEditor(policy) {
    EDITING = policy;
    const p = policy || blank();
    let eventsHtml = '';
    META.events.forEach((ev) => {
      const cfg = (p.events && p.events[ev.code]) || { is_on: false, interval: { value: 0, unit: 'm' } };
      const intv = cfg.interval || { value: 0, unit: 'm' };
      eventsHtml += `
        <div class="row-card">
          <div><strong>${esc(ev.label)}</strong><div class="muted small">${esc(ev.description || '')}</div></div>
          <div class="row-actions">
            <label class="chk"><input type="checkbox" data-ev-on="${ev.code}" ${cfg.is_on ? 'checked' : ''}>订阅</label>
            <label class="muted small">发送间隔</label>
            <input class="mini-num" type="number" min="0" data-ev-v="${ev.code}" value="${Number(intv.value) || 0}">
            <select class="mini-sel" data-ev-u="${ev.code}">
              <option value="m" ${(intv.unit || 'm') === 'm' ? 'selected' : ''}>分钟</option>
              <option value="h" ${intv.unit === 'h' ? 'selected' : ''}>小时</option>
              <option value="d" ${intv.unit === 'd' ? 'selected' : ''}>天</option>
            </select>
          </div>
        </div>`;
    });

    let chHtml = '';
    META.channel_types.forEach((ct) => {
      const cfg = (p.channels && p.channels[ct.type]) || { is_on: false, channel_key: '', to: [] };
      chHtml += `
        <div class="row-card">
          <div><strong>${esc(ct.label)}</strong><div class="muted small">${ct.interface === 'smtp' ? '邮箱需填收件人' : '机器人 webhook 已指向群'}</div></div>
          <div class="row-actions">
            <label class="chk"><input type="checkbox" data-ch-on="${ct.type}" ${cfg.is_on ? 'checked' : ''}>启用</label>
            ${ct.interface === 'smtp'
              ? `<input class="mini-num mini-wide" placeholder="收件人邮箱(逗号分隔)" data-ch-to="${ct.type}" value="${esc((cfg.to || []).join(','))}">`
              : ''}
          </div>
          <div class="row-actions sub">
            <label class="muted small">渠道实例</label>
            <select class="mini-sel mini-wide" data-ch-key="${ct.type}">
              <option value="">— 选择 —</option>
              ${CHANNELS.filter((c) => c.type === ct.type).map((c) =>
                `<option value="${esc(c.key)}" ${cfg.channel_key === c.key ? 'selected' : ''}>${esc(c.key)}（${esc(c.name || '')}）${c.enabled ? '' : '·停用'}</option>`
              ).join('')}
            </select>
          </div>
        </div>`;
    });

    document.getElementById('pl-editor').innerHTML = `
      <div class="card">
        <div class="card-head"><h3>${policy ? '编辑策略' : '新增策略'}</h3></div>
        <div class="toolbar">
          <label class="muted small">策略名称</label>
          <input class="mini-num mini-wide" id="pl-name" maxlength="64" placeholder="如：安全事件→运维群" value="${esc(p.name)}">
          <label class="chk"><input type="checkbox" id="pl-on" ${p.is_on ? 'checked' : ''}>启用</label>
        </div>
        <b class="muted">事件</b>
        ${eventsHtml}
        <b class="muted">渠道</b>
        ${chHtml}
        <div class="muted small" style="margin-top:8px">业务模块发通知：在代码里调用 notify_service.emit("事件码", ...)（见 docs/新增功能模块指南.md「发通知」章节）。</div>
        <div class="toolbar" style="margin-top:12px">
          <button class="btn btn-primary" id="pl-save">保存</button>
          <button class="btn" id="pl-cancel">取消</button>
        </div>
      </div>`;
    document.getElementById('pl-cancel').addEventListener('click', closeEditor);
    document.getElementById('pl-save').addEventListener('click', save);
    document.getElementById('pl-editor').scrollIntoView({ block: 'nearest' });
  }

  function blank() {
    const evs = {};
    META.events.forEach((ev) => { evs[ev.code] = { is_on: false, interval: { value: 0, unit: 'm' } }; });
    const chs = {};
    META.channel_types.forEach((ct) => { chs[ct.type] = { is_on: false, channel_key: '', to: [] }; });
    return { id: '', name: '', is_on: true, events: evs, channels: chs };
  }

  function collect() {
    const name = document.getElementById('pl-name').value.trim();
    if (!name) throw new Error('请填写策略名称');
    const events = {};
    META.events.forEach((ev) => {
      const value = Number(document.querySelector('[data-ev-v="' + ev.code + '"]').value) || 0;
      const original = (EDITING && EDITING.events && EDITING.events[ev.code]) || {};
      const entry = {
        is_on: document.querySelector('[data-ev-on="' + ev.code + '"]').checked,
        interval: { value: value < 0 ? 0 : value, unit: document.querySelector('[data-ev-u="' + ev.code + '"]').value },
      };
      // 标题/模板只能在接口侧配置，编辑器不展示，原值需带回去否则被清空
      if (original.subject) entry.subject = original.subject;
      events[ev.code] = entry;
    });
    const channels = {};
    META.channel_types.forEach((ct) => {
      const original = (EDITING && EDITING.channels && EDITING.channels[ct.type]) || {};
      const toEl = document.querySelector('[data-ch-to="' + ct.type + '"]');
      const entry = {
        is_on: document.querySelector('[data-ch-on="' + ct.type + '"]').checked,
        channel_key: document.querySelector('[data-ch-key="' + ct.type + '"]').value || '',
      };
      if (ct.interface === 'smtp') {
        entry.to = toEl.value.split(',').map((s) => s.trim()).filter(Boolean);
      }
      if (original.template) entry.template = original.template;
      channels[ct.type] = entry;
    });
    return { name, is_on: document.getElementById('pl-on').checked, events, channels };
  }

  function save() {
    let payload;
    try {
      payload = collect();
    } catch (e) {
      AppUI.toast(e.message || '请填写策略名称', 'error');
      return;
    }
    const id = EDITING ? EDITING.id : '';
    API.notifyPolicyUpsert(id, payload).then(() => {
      AppUI.toast('订阅策略已保存', 'success');
      closeEditor();
      return reload();
    }).catch(() => {});
  }

  function closeEditor() {
    EDITING = null;
    document.getElementById('pl-editor').innerHTML = '';
  }

  App.registerPage('/notify/policy', { render });
})();
