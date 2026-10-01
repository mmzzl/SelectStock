/* 系统监控页：CPU / 内存 / 磁盘使用率（后端 psutil 采样，页面定时轮询） */
(function () {
  const esc = AppUI.escapeHtml;

  let host = null;
  let timer = null;
  let auto = true;

  function gib(bytes) {
    return ((Number(bytes) || 0) / 1073741824).toFixed(2) + ' GiB';
  }

  function gibNum(bytes) {
    return ((Number(bytes) || 0) / 1073741824).toFixed(2);
  }

  function uptimeText(sec) {
    const s = Math.max(0, Math.floor(Number(sec) || 0));
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return (d ? d + ' 天 ' : '') + h + ' 小时 ' + m + ' 分';
  }

  function color(p) {
    return p >= 90 ? '#f56c6c' : (p >= 70 ? '#e6a23c' : '#67c23a');
  }

  function gauge(label, percent, sub) {
    const p = Math.max(0, Math.min(100, Number(percent) || 0));
    return '<div class="card num">' + esc(label) +
      '<b style="color:' + color(p) + '">' + p.toFixed(1) + '%</b>' +
      '<div style="background:#f0f0f0;height:8px;border-radius:4px;margin-top:8px">' +
      '<div style="width:' + p + '%;height:8px;border-radius:4px;background:' + color(p) + '"></div></div>' +
      '<div class="hint" style="margin-top:6px">' + esc(sub || '') + '</div></div>';
  }

  function diskRows(disks) {
    if (!disks || !disks.length) return '<tr><td class="empty" colspan="5">没有可统计的分区</td></tr>';
    return disks.map((d) => {
      const p = Math.max(0, Math.min(100, Number(d.percent) || 0));
      return '<tr>' +
        '<td>' + esc(d.path) + '</td>' +
        '<td>' + esc(d.device) + '</td>' +
        '<td>' + esc(d.fstype) + '</td>' +
        '<td style="white-space:nowrap">' + esc(gibNum(d.used)) + ' / ' + esc(gib(d.total)) + '</td>' +
        '<td><div style="background:#f0f0f0;height:8px;border-radius:4px;min-width:90px">' +
        '<div style="width:' + p + '%;height:8px;border-radius:4px;background:' + color(p) + '"></div></div>' +
        '<span class="hint" style="font-size:12px">' + p.toFixed(1) + '%</span></td>' +
        '</tr>';
    }).join('');
  }

  function draw(m) {
    const c = m.cpu || {}, mem = m.memory || {}, proc = m.process || {};
    const load = (c.load_avg || []).join(' / ');
    document.getElementById('sys-cards').innerHTML =
      gauge('CPU', c.percent, c.cores_logical + ' 逻辑核' + (load ? ' · 负载 ' + load : '')) +
      gauge('内存', mem.percent, gib(mem.used) + ' / ' + gib(mem.total)) +
      gauge('Swap', mem.swap_percent, mem.swap_total ? gib(mem.swap_used) + ' / ' + gib(mem.swap_total) : '无交换分区') +
      gauge('后端进程 RSS', proc.rss / (mem.total || 1) * 100,
        gib(proc.rss) + ' · ' + (proc.threads || '-') + ' 线程 · pid ' + (proc.pid || '-'));

    document.getElementById('sys-disks').innerHTML =
      '<div class="tbl-scroll"><table class="tbl"><thead><tr><th>挂载点</th><th>设备</th><th>类型</th><th>容量</th><th>使用率</th></tr></thead>' +
      '<tbody>' + diskRows(m.disks) + '</tbody></table></div>';

    document.getElementById('sys-meta').textContent =
      (m.hostname || '-') + ' · ' + (m.platform || '') + ' · 已运行 ' + uptimeText(m.uptime_seconds);
    document.getElementById('sys-at').textContent =
      '采样时间 ' + new Date((m.sampled_at || 0) * 1000).toLocaleString('zh-CN');
  }

  async function refresh() {
    const el = document.getElementById('sys-cards');
    if (!el) { stop(); return; }
    try {
      draw(await API.systemMetrics());
    } catch (e) { /* request 已经 toast，轮询失败时停掉，避免刷屏 */ stop(); }
  }

  function start() {
    stop();
    timer = setInterval(() => {
      if (!host || !host.isConnected) { stop(); return; }
      refresh();
    }, 5000);
  }

  function stop() {
    if (timer) { clearInterval(timer); timer = null; }
  }

  async function render(el) {
    host = el;
    stop();
    el.innerHTML =
      '<div class="card"><div class="toolbar">' +
      '<button class="btn btn-sm" id="sys-refresh">立即刷新</button>' +
      '<label class="chk"><input type="checkbox" id="sys-auto"' + (auto ? ' checked' : '') + '>每 5 秒自动刷新</label>' +
      '<span class="hint" id="sys-at"></span>' +
      '<span class="hint" id="sys-meta"></span>' +
      '</div><div class="hint">只读采样：CPU、内存、磁盘容量与使用率，用于判断回测 worker 是否把机器吃满。</div></div>' +
      '<div class="cards" id="sys-cards"></div>' +
      '<div class="card" id="sys-disks"></div>';

    document.getElementById('sys-refresh').addEventListener('click', refresh);
    document.getElementById('sys-auto').addEventListener('change', (e) => {
      auto = e.target.checked;
      if (auto) start(); else stop();
    });

    await refresh();
    if (auto) start();
  }

  App.registerPage('/system', { render });
})();
