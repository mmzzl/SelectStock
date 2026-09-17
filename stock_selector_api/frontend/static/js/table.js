/* 轻量表格渲染工具（无构建） */
(function () {
  // renderTable(el, {columns, rows, actions, page, total, size, onPage, onSearch})
  // columns: [{key, label, width, render(row)}]
  function renderTable(el, cfg) {
    const { columns, rows, actions } = cfg;
    const esc = AppUI.escapeHtml;
    let html = '';
    // toolbar
    const toolbar = cfg.toolbar || [];
    let toolbarHtml = '';
    if (toolbar.length || cfg.onSearch) {
      toolbarHtml += '<div class="toolbar">';
      toolbar.forEach((t) => { toolbarHtml += t; });
      if (cfg.onSearch) {
        toolbarHtml += '<input id="tbl-kw" class="tbl-search" placeholder="搜索..." value="' + esc(cfg.keyword || '') + '" />' +
          '<button class="btn btn-sm" id="tbl-search-btn">搜索</button>';
      }
      toolbarHtml += '</div>';
    }
    // table
    html += toolbarHtml;
    html += '<table class="tbl"><thead><tr>';
    columns.forEach((c) => {
      html += '<th' + (c.width ? ' style="width:' + c.width + '"' : '') + '>' + esc(c.label) + '</th>';
    });
    if (actions && actions.length) html += '<th style="width:180px">操作</th>';
    html += '</tr></thead><tbody>';
    if (rows.length) {
      rows.forEach((row) => {
        html += '<tr>';
        columns.forEach((c) => {
          let v = c.render ? c.render(row) : (row[c.key] ?? '');
          if (v === null || v === undefined) v = '';
          html += '<td>' + v + '</td>';
        });
        if (actions && actions.length) {
          html += '<td>';
          actions.forEach((a) => {
            if (a.visible && !a.visible(row)) return;
            html += '<a class="act" data-action="' + a.key + '" data-id="' + row.id + '">' + esc(a.label) + '</a>';
          });
          html += '</td>';
        }
        html += '</tr>';
      });
    } else {
      html += '<tr><td colspan="' + (columns.length + (actions && actions.length ? 1 : 0)) + '" class="empty">暂无数据</td></tr>';
    }
    html += '</tbody></table>';

    // pagination
    const totalPages = Math.max(1, Math.ceil(cfg.total / (cfg.size || 20)));
    html += '<div class="pager">';
    html += '<span>共 ' + cfg.total + ' 条</span>';
    html += '<button class="btn btn-sm" id="pg-prev"' + (cfg.page <= 1 ? ' disabled' : '') + '>上一页</button>';
    html += '<span class="pager-cur">' + cfg.page + ' / ' + totalPages + '</span>';
    html += '<button class="btn btn-sm" id="pg-next"' + (cfg.page >= totalPages ? ' disabled' : '') + '>下一页</button>';
    html += '</div>';

    el.innerHTML = html;

    // events
    el.querySelectorAll('[data-action]').forEach((a) => {
      a.addEventListener('click', () => {
        const row = rows.find((r) => String(r.id) === String(a.dataset.id));
        const handler = actions.find((x) => x.key === a.dataset.action);
        if (handler && row) handler.onClick(row);
      });
    });
    if (cfg.onSearch) {
      const kw = el.querySelector('#tbl-kw');
      el.querySelector('#tbl-search-btn').addEventListener('click', () => cfg.onSearch(kw.value));
      kw.addEventListener('keydown', (e) => { if (e.key === 'Enter') cfg.onSearch(kw.value); });
    }
    el.querySelector('#pg-prev')?.addEventListener('click', () => { if (cfg.page > 1) cfg.onPage(cfg.page - 1); });
    el.querySelector('#pg-next')?.addEventListener('click', () => { if (cfg.page < totalPages) cfg.onPage(cfg.page + 1); });
  }

  window.AppTable = { renderTable };
})();
