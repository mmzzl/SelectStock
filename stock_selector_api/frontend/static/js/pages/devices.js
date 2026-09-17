/* 设备管理（示例模块页面） */
(function () {
  const pageState = { page: 1, size: 20, keyword: '', status: '' };

  function load(el) {
    return API.devices(pageState).then((res) => {
      renderTable(el, res);
    });
  }

  function renderTable(el, res) {
    const can = (code) => (App.currentUser?.permissions?.includes(code) || App.currentUser?.is_superuser);
    AppTable.renderTable(el, {
      columns: [
        { key: 'name', label: '名称', render: (r) => AppUI.escapeHtml(r.name) },
        { key: 'ip', label: 'IP', render: (r) => AppUI.escapeHtml(r.ip) },
        { key: 'vendor', label: '厂商', render: (r) => AppUI.escapeHtml(r.vendor) },
        { key: 'model', label: '型号', render: (r) => AppUI.escapeHtml(r.model) },
        { key: 'status', label: '状态', render: (r) => {
          const map = { online: ['在线', 'green'], offline: ['离线', 'red'], maintenance: ['维护', 'orange'] };
          const [txt, cls] = map[r.status] || [r.status, ''];
          return '<span class="tag ' + cls + '">' + txt + '</span>';
        } },
      ],
      rows: res.items,
      total: res.total,
      page: res.page,
      size: res.size,
      keyword: pageState.keyword,
      toolbar: [
        can('device:create')
          ? '<button class="btn btn-primary btn-sm" id="btn-add">新增</button>'
          : '<span class="hint">(只读)</span>',
        '<select id="f-status"><option value="">全部状态</option><option value="online">在线</option><option value="offline">离线</option><option value="maintenance">维护</option></select>',
      ],
      onPage: (p) => { pageState.page = p; load(el).catch(() => {}); },
      onSearch: (kw) => { pageState.keyword = kw; pageState.page = 1; load(el).catch(() => {}); },
      actions: [
        { key: 'edit', label: '编辑', visible: () => can('device:update'),
          onClick: (row) => AppUI.openForm('编辑设备',
            AppUI.fInput('name', '名称', row.name)
            + AppUI.fInput('ip', 'IP', row.ip)
            + AppUI.fInput('vendor', '厂商', row.vendor)
            + AppUI.fInput('model', '型号', row.model)
            + AppUI.fSelect('status', '状态',
                [{ value: 'online', label: '在线' }, { value: 'offline', label: '离线' }, { value: 'maintenance', label: '维护' }],
                row.status)
            + AppUI.fInput('owner', '负责人', row.owner)
            + AppUI.fInput('location', '位置', row.location)
            + AppUI.fTextarea('remark', '备注', row.remark),
            async (f) => { await API.updateDevice(row.id, f); AppUI.toast('已保存', 'success'); load(el).catch(() => {}); }) },
        { key: 'del', label: '删除', visible: () => can('device:delete'),
          onClick: (row) => { if (AppUI.confirmBox('确认删除设备 ' + row.name + ' ?')) API.deleteDevice(row.id).then(() => { AppUI.toast('已删除', 'success'); load(el).catch(() => {}); }); } },
      ],
    });

    // toolbar 事件
    el.querySelector('#btn-add')?.addEventListener('click', () => {
      AppUI.openForm('新增设备',
        AppUI.fInput('name', '名称')
        + AppUI.fInput('ip', 'IP')
        + AppUI.fInput('vendor', '厂商')
        + AppUI.fInput('model', '型号')
        + AppUI.fSelect('status', '状态', [{ value: 'online', label: '在线' }, { value: 'offline', label: '离线' }, { value: 'maintenance', label: '维护' }])
        + AppUI.fInput('owner', '负责人')
        + AppUI.fInput('location', '位置'),
        async (f) => { await API.createDevice(f); AppUI.toast('已创建', 'success'); load(el).catch(() => {}); });
    });
    el.querySelector('#f-status')?.addEventListener('change', (e) => { pageState.status = e.target.value; pageState.page = 1; load(el).catch(() => {}); });
  }

  App.registerPage('/devices', { render: load });
})();
