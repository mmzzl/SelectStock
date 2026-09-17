/* 订阅策略页：总开关 + 每事件开关/节流间隔 + 每渠道开关/绑定渠道/邮件收件人/自定义模板（对齐 secvisual 告警策略语义）。 */
(function () {
  let META = { events: [], channel_types: [] };

  function render(el, ctx) {
    el.innerHTML = `
      <div class="card">
        <div class="card-head"><h3>订阅策略</h3></div>
        <div id="policy-box" class="muted">加载中…</div>
      </div>`;
    API.notifyEvents().then((d) => { META = d; return API.notifyPolicyGet(); })
      .then((p) => build(el, p))
      .catch(() => {});
  }

  function build(el, p) {
    const canWrite = App.can('sys:notify:update');
    const unit = (i) => i && i.unit ? i.unit : 'm';
    let eventsHtml = '';
    META.events.forEach((ev) => {
      const cfg = (p.events && p.events[ev.code]) || { is_on: false, interval: { value: 0, unit: 'm' } };
      const intv = cfg.interval || { value: 0, unit: 'm' };
      eventsHtml += `
        <div class="row-card">
          <div><strong>${AppUI.escapeHtml(ev.label)}</strong><div class="muted small">${AppUI.escapeHtml(ev.description || '')}</div></div>
          <div class="row-actions">
            <label class="chk"><input type="checkbox" data-ev-on="${ev.code}" ${cfg.is_on ? 'checked' : ''}>启用</label>
            <label class="muted small">发送间隔</label>
            <input class="mini-num" type="number" min="0" data-ev-v="${ev.code}" value="${intv.value || 0}">
            <select class="mini-sel" data-ev-u="${ev.code}">
              <option value="m" ${unit(intv) === 'm' ? 'selected' : ''}>分钟</option>
              <option value="h" ${unit(intv) === 'h' ? 'selected' : ''}>小时</option>
              <option value="d" ${unit(intv) === 'd' ? 'selected' : ''}>天</option>
            </select>
          </div>
        </div>`;
    });

    let chHtml = '';
    META.channel_types.forEach((ct) => {
      const cfg = (p.channels && p.channels[ct.type]) || { is_on: false, channel_key: '', to: [] };
      chHtml += `
        <div class="row-card">
          <div><strong>${AppUI.escapeHtml(ct.label)}</strong><div class="muted small">${ct.interface === 'smtp' ? '邮箱需填收件人' : '机器人 webhook 已指向群'}</div></div>
          <div class="row-actions">
            <label class="chk"><input type="checkbox" data-ch-on="${ct.type}" ${cfg.is_on ? 'checked' : ''}>启用</label>
            ${ct.interface === 'smtp'
              ? `<input class="mini-num mini-wide" placeholder="收件人邮箱(逗号分隔)" data-ch-to="${ct.type}" value="${(cfg.to || []).join(',')}">`
              : ''}
          </div>
          <div class="row-actions sub">
            <label class="muted small">渠道实例</label>
            <select class="mini-sel mini-wide" data-ch-key="${ct.type}"><option value="">— 选择 —</option></select>
          </div>
        </div>`;
    });

    document.getElementById('policy-box').innerHTML = `
      <div class="row-card" style="margin-bottom:10px">
        <div><strong>总开关</strong><div class="muted small">关闭后所有订阅策略不生效；开启后下方事件按配置投递</div></div>
        <div class="row-actions"><label class="chk"><input type="checkbox" id="m-on" ${p.is_on ? 'checked' : ''}>启用</label></div>
      </div>
      <b class="muted">事件（触发点：账号锁定 / 登录失败 / 定时任务失败 / 自定义）</b>
      ${eventsHtml}
      <b class="muted">渠道</b>
      ${chHtml}
      ${canWrite ? '<button class="btn btn-primary" id="save-policy">保存策略</button>' : '<div class="muted small">只读角色无保存权限</div>'}
      ${ctx === 'hint' ? '' : ''}
      <div class="muted small" style="margin-top:8px">业务模块发通知：在代码里调用 notify_service.emit("事件码", ...)（见 docs/新增功能模块指南.md「发通知」章节）。</div>`;

    // 填充各渠道可选实例
    API.notifyChannels().then((chs) => {
      META.channel_types.forEach((ct) => {
        const sel = document.querySelector('[data-ch-key="' + ct.type + '"]');
        if (!sel) return;
        const cfg = (p.channels && p.channels[ct.type]) || {};
        chs.filter((c) => c.type === ct.type).forEach((c) => {
          const o = document.createElement('option');
          o.value = c.key;
          o.textContent = c.key + '（' + (c.name || '') + '）' + (c.enabled ? '' : '·停用');
          sel.appendChild(o);
        });
        if (cfg.channel_key) sel.value = cfg.channel_key;
      });
    }).catch(() => {});

    if (canWrite) {
      document.getElementById('save-policy').addEventListener('click', () => save(el, p));
    }
  }

  function save(el, p) {
    const policy = {
      is_on: document.getElementById('m-on').checked,
      events: {},
      channels: {},
    };
    META.events.forEach((ev) => {
      policy.events[ev.code] = {
        is_on: document.querySelector('[data-ev-on="' + ev.code + '"]').checked,
        interval: {
          value: Number(document.querySelector('[data-ev-v="' + ev.code + '"]').value) || 0,
          unit: document.querySelector('[data-ev-u="' + ev.code + '"]').value,
        },
      };
    });
    META.channel_types.forEach((ct) => {
      const toEl = document.querySelector('[data-ch-to="' + ct.type + '"]');
      policy.channels[ct.type] = {
        is_on: document.querySelector('[data-ch-on="' + ct.type + '"]').checked,
        channel_key: document.querySelector('[data-ch-key="' + ct.type + '"]').value || '',
        to: toEl ? toEl.value.split(',').map((s) => s.trim()).filter(Boolean) : [],
      };
    });
    API.notifyPolicyPut(policy).then(() => { AppUI.toast('订阅策略已保存', 'success'); }).catch(() => {});
  }

  App.registerPage('/notify/policy', { render });
})();
