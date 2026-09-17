/* 角色管理页（含权限树勾选分配） */
(function () {
  function load(el) {
    return API.roles().then((roles) => {
      renderTable(el, roles);
    });
  }

  function renderTable(el, roles) {
    const can = (code) => (App.currentUser?.permissions?.includes(code) || App.currentUser?.is_superuser);
    AppTable.renderTable(el, {
      columns: [
        { key: 'name', label: '名称', render: (r) => AppUI.escapeHtml(r.name) },
        { key: 'code', label: 'Code', render: (r) => AppUI.escapeHtml(r.code) + (r.builtin ? ' <span class="tag gray">内置</span>' : '') },
        { key: 'description', label: '描述', render: (r) => AppUI.escapeHtml(r.description) },
        { key: 'permissions', label: '权限数', render: (r) => r.permission_codes.length },
      ],
      rows: roles,
      total: roles.length,
      page: 1,
      size: 200,
      toolbar: [can('sys:role:create') ? '<button class="btn btn-primary btn-sm" id="btn-add">新增</button>' : ''],
      actions: [
        { key: 'perms', label: '分配权限', visible: () => can('sys:role:update'),
          onClick: (row) => openPerms(row) },
        { key: 'edit', label: '编辑', visible: () => can('sys:role:update'),
          onClick: (row) => AppUI.openForm('编辑角色',
            AppUI.fInput('name', '名称', row.name)
            + AppUI.fInput('description', '描述', row.description),
            async (f) => { await API.updateRole(row.id, f); AppUI.toast('已保存', 'success'); load(el).catch(() => {}); }) },
        { key: 'del', label: '删除', visible: (r) => can('sys:role:delete') && !r.builtin,
          onClick: (row) => {
            if (AppUI.confirmBox('确认删除角色 ' + row.name + ' ?')) {
              API.deleteRole(row.id).then(() => { AppUI.toast('已删除', 'success'); load(el).catch(() => {}); });
            }
          } },
      ],
    });
    el.querySelector('#btn-add')?.addEventListener('click', () => {
      AppUI.openForm('新增角色',
        AppUI.fInput('name', '名称')
        + AppUI.fInput('code', 'Code（小写字母下划线）')
        + AppUI.fInput('description', '描述'),
        async (f) => { await API.createRole(f); AppUI.toast('已创建', 'success'); load(el).catch(() => {}); });
    });
  }

  function openPerms(role) {
    const wrap = document.createElement('div');
    elForPerms(wrap, role);
    document.body.appendChild(wrap);
    const mask = wrap.firstElementChild;
    // 用权限树（模块→功能→动作）渲染复选框，方便逐模块/逐功能勾选
    const loadPerms = () => API.roleDetail(role.id).then((detail) => {
      const sel = new Set(detail.permission_codes);
      return API.permissionTree().then((tree) => {
        let html = '';
        tree.forEach((mod) => {
          html += '<div class="perm-group"><b>' + AppUI.escapeHtml(mod.module) + '</b>';
          mod.features.forEach((feat) => {
            html += '<div class="perm-feature"><span class="perm-feature-name">' + AppUI.escapeHtml(feat.feature) + '</span>';
            feat.permissions.forEach((p) => {
              html += '<label class="chk"><input type="checkbox" data-code="' + p.code + '"' + (sel.has(p.code) ? ' checked' : '') + '><code>' + AppUI.escapeHtml(p.action || p.code) + '</code></label>';
            });
            html += '</div>';
          });
          html += '</div>';
        });
        mask.querySelector('#perm-list').innerHTML = html || '<div class="muted">无权限项</div>';
      });
    });
    loadPerms().catch(() => {});
  }

  function elForPerms(mask, role) {
    mask.innerHTML = modalHtmlForPerms(role);
    mask.firstElementChild.querySelectorAll('[data-close]').forEach((el) => el.addEventListener('click', () => mask.remove()));
    mask.firstElementChild.querySelector('#perm-ok').addEventListener('click', async () => {
      const ids = [];
      // 重新拉全量权限以 code→id 对照
      const perms = await API.permissions();
      mask.firstElementChild.querySelectorAll('#perm-list input:checked').forEach((inp) => {
        const p = perms.find((x) => x.code === inp.dataset.code);
        if (p) ids.push(p.id);
      });
      await API.assignRolePerms(role.id, ids);
      AppUI.toast('已保存', 'success');
      mask.remove();
      location.reload();
    });
  }

  function modalHtmlForPerms(role) {
    return '<div class="modal-mask"><div class="modal">' +
      '<div class="modal-title">分配权限 - ' + AppUI.escapeHtml(role.name) + '<span class="modal-close" data-close>×</span></div>' +
      '<div class="modal-body" id="perm-list" style="max-height:60vh;overflow:auto">加载中...</div>' +
      '<div class="modal-foot"><button class="btn" data-close>取消</button><button class="btn btn-primary" id="perm-ok">保存</button></div>' +
      '</div></div>';
  }

  App.registerPage('/roles', { render: load });
})();
