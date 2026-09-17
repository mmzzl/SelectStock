/* 用户管理页 */
(function () {
  const pageState = { page: 1, size: 20, keyword: '' };
  let allRoles = [];

  function load(el) {
    return Promise.all([API.users(pageState), API.roles()]).then(([res, roles]) => {
      allRoles = roles;
      renderTable(el, res);
    });
  }

  function roleSelectHtml(selectedIds) {
    if (!allRoles.length) return '';
    return allRoles.map((r) => {
      const sel = (selectedIds || []).includes(r.id) ? ' checked' : '';
      return '<label class="chk"><input type="checkbox" data-field="role_' + r.id + '"' + sel + '> ' + AppUI.escapeHtml(r.name) + '</label>';
    }).join('');
  }

  function renderTable(el, res) {
    const can = (code) => (App.currentUser?.permissions?.includes(code) || App.currentUser?.is_superuser);
    AppTable.renderTable(el, {
      columns: [
        { key: 'username', label: '用户名', render: (r) => AppUI.escapeHtml(r.username) + (r.is_superuser ? ' <span class="tag">超管</span>' : '') },
        { key: 'display_name', label: '显示名', render: (r) => AppUI.escapeHtml(r.display_name) },
        { key: 'email', label: '邮箱', render: (r) => AppUI.escapeHtml(r.email) },
        { key: 'roles', label: '角色', render: (r) => r.role_names.map((n) => '<span class="tag gray">' + AppUI.escapeHtml(n) + '</span>').join(' ') },
        { key: 'is_active', label: '状态', render: (r) => r.is_active ? '<span class="tag green">启用</span>' : '<span class="tag red">禁用</span>' },
        { key: 'created_at', label: '创建时间', render: (r) => (r.created_at || '').replace('T', ' ').slice(0, 19) },
      ],
      rows: res.items,
      total: res.total,
      page: res.page,
      size: res.size,
      keyword: pageState.keyword,
      toolbar: [
        can('sys:user:create') ? '<button class="btn btn-primary btn-sm" id="btn-add">新增</button>' : '<span class="hint">(只读)</span>',
      ],
      onPage: (p) => { pageState.page = p; load(el).catch(() => {}); },
      onSearch: (kw) => { pageState.keyword = kw; pageState.page = 1; load(el).catch(() => {}); },
      actions: [
        { key: 'edit', label: '编辑', visible: () => can('sys:user:update'),
          onClick: (row) => AppUI.openForm('编辑用户',
            AppUI.fInput('display_name', '显示名', row.display_name)
            + AppUI.fInput('email', '邮箱', row.email)
            + AppUI.fInput('is_active', '启用', row.is_active ? 'true' : 'false')
            + '<div class="frow"><label>角色</label><div>' + roleSelectHtml(row.role_ids) + '</div></div>'
            + AppUI.fPassword('password', '重置密码(留空不改)'),
            async (f) => {
              const role_ids = allRoles.filter((r) => f['role_' + r.id]).map((r) => r.id);
              const payload = { display_name: f.display_name, email: f.email, is_active: f.is_active === 'true', role_ids };
              if (f.password) payload.password = f.password;
              await API.updateUser(row.id, payload);
              AppUI.toast('已保存', 'success'); load(el).catch(() => {});
            }) },
        { key: 'del', label: '删除', visible: () => can('sys:user:delete'),
          onClick: (row) => {
            if (AppUI.confirmBox('确认删除用户 ' + row.username + ' ?')) {
              API.deleteUser(row.id).then(() => { AppUI.toast('已删除', 'success'); load(el).catch(() => {}); });
            }
          } },
      ],
    });
    el.querySelector('#btn-add')?.addEventListener('click', () => {
      AppUI.openForm('新增用户',
        AppUI.fInput('username', '用户名')
        + AppUI.fPassword('password', '密码')
        + AppUI.fInput('display_name', '显示名')
        + AppUI.fInput('email', '邮箱')
        + '<div class="frow"><label>角色</label><div>' + roleSelectHtml([]) + '</div></div>',
        async (f) => {
          const role_ids = allRoles.filter((r) => f['role_' + r.id]).map((r) => r.id);
          await API.createUser({ username: f.username, password: f.password, display_name: f.display_name, email: f.email, role_ids });
          AppUI.toast('已创建', 'success'); load(el).catch(() => {});
        });
    });
  }

  App.registerPage('/users', { render: load });
})();
