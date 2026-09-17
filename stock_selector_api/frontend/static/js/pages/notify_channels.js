/* 通知渠道配置页：四类渠道（企业微信/钉钉/飞书机器人 + 邮箱 SMTP）增删改查 + 测试发送。 */
(function () {
  let TYPES = []; // {type,label,interface,fields,secret_fields}

  function typeMeta(t) { return TYPES.find((x) => x.type === t) || { fields: [], secret_fields: [] }; }

  function render(el, ctx) {
    const canWrite = App.can('sys:notify:update');
    if (!ctx || ctx.channel_type) {
      // 支持带类型进入（菜单固定走 #/notify/channels）
    }
    el.innerHTML = `
      <div class="card">
        <div class="card-head">
          <h3>通知渠道配置</h3>
          <div class="head-actions">
            <select id="type-filter">
              <option value="">全部类型</option>
            </select>
            ${canWrite ? '<button class="btn btn-primary" id="add-ch">新增渠道</button>' : ''}
          </div>
        </div>
        <div id="ch-list"></div>
      </div>`;

    const api = window.App ? App : {};
    API.notifyEvents().then((d) => {
      TYPES = d.channel_types;
      TYPES.forEach((t) => {
        const opt = document.createElement('option');
        opt.value = t.type; opt.textContent = t.label;
        document.getElementById('type-filter').appendChild(opt);
        const meta = typeMeta(t.type);
      });
      refresh(el);
    }).catch(() => {});

    if (canWrite) {
      document.getElementById('add-ch').addEventListener('click', () => editChannel(el, null));
    }
    document.getElementById('type-filter').addEventListener('change', () => refresh(el));
  }

  function refresh(el) {
    const filter = document.getElementById('type-filter') ? document.getElementById('type-filter').value : '';
    API.notifyChannels().then((chs) => {
      const list = chs.filter((c) => !filter || c.type === filter);
      const wrap = document.getElementById('ch-list');
      if (!list.length) { wrap.innerHTML = '<div class="muted">暂无渠道，点击新增开始配置</div>'; return; }
      // 按类型分组展示
      const groups = {};
      list.forEach((c) => { (groups[c.type] = groups[c.type] || []).push(c); });
      let html = '';
      Object.keys(groups).forEach((t) => {
        const meta = typeMeta(t);
        html += '<div class="perm-group"><b>' + AppUI.escapeHtml(meta.label || t) + '</b>';
        groups[t].forEach((c) => {
          const brief = t === 'email' ? (c.host || '') : (c.webhook_url || '');
          html +=
            '<div class="row-card">' +
            '<div><code>' + AppUI.escapeHtml(c.key) + '</code> ' + AppUI.escapeHtml(c.name) +
            '<div class="muted small">' + AppUI.escapeHtml(brief) + '</div></div>' +
            '<div class="row-actions">' +
            (c.enabled ? '<span class="ok">已启用</span>' : '<span class="muted">停用</span>') +
            '<button class="btn mini" data-test="' + c.key + '" data-type="' + t + '">测试</button>' +
            '<button class="btn mini" data-edit="' + c.key + '">编辑</button>' +
            (App.can('sys:notify:update') ? '<button class="btn mini danger" data-del="' + c.key + '">删除</button>' : '') +
            '</div></div>';
        });
        html += '</div>';
      });
      wrap.innerHTML = html;

      wrap.querySelectorAll('[data-edit]').forEach((b) => b.addEventListener('click', () => editChannel(el, b.dataset.edit)));
      wrap.querySelectorAll('[data-del]').forEach((b) => b.addEventListener('click', () => removeChannel(el, b.dataset.del)));
      wrap.querySelectorAll('[data-test]').forEach((b) => b.addEventListener('click', () => testChannel(el, b.dataset.test, b.dataset.type)));
    }).catch(() => {});
  }

  function channelsByType(t) {
    return new Promise((resolve) => {
      API.notifyChannels().then((chs) => resolve(chs.filter((c) => c.type === t))).catch(() => resolve([]));
    });
  }

  function editChannel(el, key) {
    const metaFirst = TYPES[0];
    API.notifyChannels().then((chs) => {
      const existing = key ? chs.find((c) => c.key === key) : null;
      const type = existing ? existing.type : metaFirst.type;
      buildForm(el, { mode: 'edit', existing, type });
    }).catch(() => {});
  }

  function buildForm(el, state) {
    const isEmail = state.type === 'email';
    let html = AppUI.fSelect('type', '渠道类型', TYPES.map((t) => ({ value: t.type, label: t.label })), state.type) +
      AppUI.fInput('key', '标识 key', state.existing ? state.existing.key : '');
    if (state.mode === 'edit' && state.existing) {
      html += '<div class="muted small">key 为渠道唯一标识，订阅策略按它引用；编辑后将沿用原 key</div>';
    }
    if (!isEmail) {
      html += AppUI.fInput('name', '名称', (state.existing && state.existing.name) || '');
      html += AppUI.fInput('webhook_url', '机器人 Webhook 地址', (state.existing && state.existing.webhook_url) || '');
      const meta = typeMeta(state.type);
      if (meta.secret_fields.includes('secret')) {
        html += AppUI.fPassword('secret', '加签密钥(可选)');
      }
    } else {
      html += AppUI.fInput('name', '名称', (state.existing && state.existing.name) || '');
      html += AppUI.fInput('host', 'SMTP 服务器', (state.existing && state.existing.host) || '');
      html += AppUI.fInput('port', '端口', (state.existing && state.existing.port) || (state.existing && state.existing.use_ssl ? 465 : 25));
      html += AppUI.fInput('username', '账号', (state.existing && state.existing.username) || '');
      html += AppUI.fPassword('password', '密码');
      html += AppUI.fInput('from_email', '发件人邮箱', (state.existing && state.existing.from_email) || '');
      html += AppUI.fInput('from_name', '发件人名称', (state.existing && state.existing.from_name) || '');
      html += AppUI.fSelect('use_ssl', '启用 SSL', [
        { value: 'true', label: 'SSL(465)' },
        { value: 'false', label: '非 SSL(25/STARTTLS)' },
      ], state.existing && state.existing.use_ssl ? 'true' : 'false');
    }
    AppUI.openForm(state.existing ? '编辑渠道' : '新增渠道', html, async (form) => {
      const meta = typeMeta(form.type);
      const data = { type: form.type, key: form.key || '', name: form.name || '', enabled: true };
      // 取回当前 enabled 状态（编辑时不覆盖）
      if (state.existing) data.enabled = state.existing.enabled;
      if (form.type === 'email') {
        data.host = form.host || ''; data.port = Number(form.port) || (form.use_ssl === 'true' ? 465 : 25);
        data.username = form.username || ''; data.password = form.password || '';
        data.from_email = form.from_email || ''; data.from_name = form.from_name || '';
        data.use_ssl = form.use_ssl === 'true';
      } else {
        data.webhook_url = form.webhook_url || '';
        if (meta.secret_fields.includes('secret')) data.secret = form.secret || '';
      }
      if (!data.key) { throw new Error('请填写 key'); }
      await API.notifyChannelUpsert(data);
      AppUI.toast('渠道配置已保存', 'success');
      refresh(el);
    });
  }

  function removeChannel(el, key) {
    if (!AppUI.confirmBox('确认删除渠道 ' + key + '？')) return;
    API.notifyChannelDelete(key).then(() => {
      AppUI.toast('已删除', 'success'); refresh(el);
    }).catch(() => {});
  }

  function testChannel(el, key, type) {
    if (type === 'email') {
      AppUI.openForm('测试邮件', AppUI.fInput('to', '收件人(逗号分隔)', '') + AppUI.fInput('text', '内容', ''), async (form) => {
        if (!form.to) throw new Error('请填写收件人');
        await API.notifyChannelTest(key, form.text, form.to.split(',').map((s) => s.trim()).filter(Boolean));
        AppUI.toast('测试已发送', 'success');
      });
      return;
    }
    AppUI.openForm('测试发送', AppUI.fInput('text', '消息内容', '这是一条测试消息'), async (form) => {
      await API.notifyChannelTest(key, form.text, []);
      AppUI.toast('测试已发送', 'success');
    });
  }

  App.registerPage('/notify/channels', { render });
})();
