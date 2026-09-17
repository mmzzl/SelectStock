/* 工作台首页 */
(function () {
  async function render(el) {
    let summary = null;
    try {
      summary = await API.deviceSummary();
    } catch (e) { /* 无权限时不展示 */ }
    const esc = AppUI.escapeHtml;
    el.innerHTML = '<div class="card">欢迎使用安全管控平台模板。</div>'
      + (summary
        ? '<div class="cards">'
          + '<div class="card num">设备总数<br><b>' + summary.total + '</b></div>'
          + Object.keys(summary.by_status || {}).map((k) =>
            '<div class="card num">' + esc(k) + '<br><b>' + summary.by_status[k] + '</b></div>').join('')
          + '</div>'
        : '');
  }
  App.registerPage('/dashboard', { render });
})();
