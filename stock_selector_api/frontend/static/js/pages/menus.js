/* 菜单管理页（树形表格：动态菜单是它的数据源） */
(function () {
  function load(el) {
    return API.menusManage().then((menus) => {
      renderTable(el, menus);
    });
  }

  function renderTable(el, menus) {
    const can = (code) => (App.currentUser?.permissions?.includes(code) || App.currentUser?.is_superuser);
    // 构建父子映射，前面加层级缩进
    const parentMap = {};
    menus.forEach((m) => { (parentMap[m.parent_id || ''] = parentMap[m.parent_id || ''] || []).push(m); });
    function walk(parentId, depth, acc) {
      (parentMap[parentId] || []).forEach((m) => {
        acc.push({ ...m, _depth: depth });
        walk(m.id, depth + 1, acc);
      });
    }
    const rows = [];
    walk('', 0, rows);

    AppTable.renderTable(el, {
      columns: [
        { key: 'name', label: '名称', render: (r) => '<span style="padding-left:' + r._depth * 20 + 'px">' + (r._depth ? '└ ' : '') + AppUI.escapeHtml(r.name) + '</span>' },
        { key: 'menu_type', label: '类型', render: (r) => ({ dir: '目录', menu: '菜单', button: '按钮' }[r.menu_type] || r.menu_type) },
        { key: 'path', label: '路由', render: (r) => AppUI.escapeHtml(r.path) },
        { key: 'permission_code', label: '权限码', render: (r) => AppUI.escapeHtml(r.permission_code) || '-' },
        { key: 'sort_order', label: '排序', render: (r) => r.sort_order },
        { key: 'is_visible', label: '可见', render: (r) => r.is_visible ? '是' : '否' },
      ],
      rows,
      total: rows.length,
      page: 1,
      size: 500,
      toolbar: [can('sys:menu:create') ? '<button class="btn btn-primary btn-sm" id="btn-add">新增</button>' : ''],
      actions: [
        { key: 'edit', label: '编辑', visible: () => can('sys:menu:update'),
          onClick: (row) => AppUI.openForm('编辑菜单',
            AppUI.fInput('name', '名称', row.name)
            + AppUI.fSelect('menu_type', '类型', [{ value: 'dir', label: '目录' }, { value: 'menu', label: '菜单' }, { value: 'button', label: '按钮' }], row.menu_type)
            + AppUI.fInput('path', '路由(#/xxx)', row.path)
            + AppUI.fInput('parent_id', '父ID', row.parent_id)
            + AppUI.fInput('sort_order', '排序', row.sort_order)
            + AppUI.fInput('permission_code', '权限码', row.permission_code)
            + AppUI.fSelect('is_visible', '可见', [{ value: 'true', label: '是' }, { value: 'false', label: '否' }], String(row.is_visible)),
            async (f) => { await API.updateMenu(row.id, { ...f, is_visible: f.is_visible === 'true', sort_order: Number(f.sort_order) || 0 }); AppUI.toast('已保存', 'success'); load(el).catch(() => {}); }) },
        { key: 'del', label: '删除', visible: () => can('sys:menu:delete'),
          onClick: (row) => {
            if (AppUI.confirmBox('确认删除菜单 ' + row.name + ' ?')) {
              API.deleteMenu(row.id).then(() => { AppUI.toast('已删除', 'success'); load(el).catch(() => {}); });
            }
          } },
      ],
    });
    el.querySelector('#btn-add')?.addEventListener('click', () => {
      AppUI.openForm('新增菜单',
        AppUI.fInput('name', '名称')
        + AppUI.fSelect('menu_type', '类型', [{ value: 'dir', label: '目录' }, { value: 'menu', label: '菜单' }, { value: 'button', label: '按钮' }])
        + AppUI.fInput('path', '路由(#/xxx)')
        + AppUI.fInput('parent_id', '父ID（留空为根）', '')
        + AppUI.fInput('sort_order', '排序', 0)
        + AppUI.fInput('permission_code', '权限码'),
        async (f) => { await API.createMenu({ ...f, is_visible: true, sort_order: Number(f.sort_order) || 0 }); AppUI.toast('已创建', 'success'); load(el).catch(() => {}); });
    });
  }

  App.registerPage('/menus', { render: load });
})();
