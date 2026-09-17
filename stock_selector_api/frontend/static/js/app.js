/* 主框架：登录态管理、动态侧栏菜单渲染、hash 路由到各页面 */
(function () {
  const PAGES = {};
  function registerPage(name, module) { PAGES[name] = module; }

  // ---------- 动态菜单渲染 ----------
  function renderMenu(menus, currentPath) {
    const aside = document.getElementById('sidebar');
    if (!aside) return;
    let html = '';
    for (const m of menus) {
      const active = m.path
        && m.path.replace(/^#/, '') === currentPath.replace(/^#/, '')
        ? ' class="active"' : '';
      if (m.children && m.children.length) {
        const childHtml = m.children.map((c) =>
          '<div class="menu-item sub' + (c.path && c.path.replace(/^#/, '') === currentPath.replace(/^#/, '') ? ' active' : '') + '" data-path="' + c.path + '">' +
          AppUI.escapeHtml(c.name) + '</div>'
        ).join('');
        html += '<div class="menu-group"><div class="menu-title">' +
          AppUI.escapeHtml(m.name) + '</div>' + childHtml + '</div>';
      } else {
        html += '<div class="menu-item' + active + '" data-path="' + m.path + '">' +
          AppUI.escapeHtml(m.name) + '</div>';
      }
    }
    aside.innerHTML = '<div class="brand">' + AppUI.escapeHtml(document.title || '控制台') + '</div>' +
      '<nav>' + html + '</nav>';

    // 绑定跳转
    aside.querySelectorAll('[data-path]').forEach((el) => {
      el.addEventListener('click', () => {
        document.querySelectorAll('.menu-item').forEach((x) => x.classList.remove('active'));
        el.classList.add('active');
        window.location.hash = el.dataset.path.replace(/^#/, '');
      });
    });
  }

  // ---------- 主框架模板 ----------
  const layoutHtml = `
    <div class="layout">
      <aside id="sidebar" class="sidebar"></aside>
      <main class="main">
        <header class="topbar">
          <div id="crumb" class="crumb"></div>
          <div class="userbox">
            <span id="username"></span>
            <button class="btn btn-sm" id="logout">退出</button>
          </div>
        </header>
        <section id="content" class="content"></section>
      </main>
    </div>`;

  let currentUser = null;
  let menus = [];

  function refreshMenus() {
    return API.menus().then((tree) => {
      menus = tree;
      return tree;
    });
  }

  async function showApp() {
    document.getElementById('root').innerHTML = layoutHtml;
    document.getElementById('logout').addEventListener('click', async () => {
      try { await API.logout(); } catch (e) { /* ignore */ }
      Auth.clearTokens();
      window.location.hash = '#/login';
    });
    // 加载用户信息 + 菜单
    const [me, tree] = await Promise.all([API.me(), refreshMenus()]);
    currentUser = me;
    document.getElementById('username').textContent = me.display_name || me.username;
    route();
  }

  function showLogin() {
    document.getElementById('root').innerHTML = `
      <div class="login-wrap">
        <form class="login-box" id="login-form">
          <h2>安全管控平台</h2>
          <label>用户名<input id="u" autocomplete="username" /></label>
          <label>密码<input id="p" type="password" autocomplete="current-password" /></label>
          <label>验证码
            <div class="captcha-row">
              <input id="c" autocomplete="off" placeholder="不区分大小写" />
              <img id="cimg" class="captcha-img" alt="验证码" title="点击刷新" />
            </div>
          </label>
          <button type="submit" class="btn btn-primary btn-block">登 录</button>
        </form>
      </div>`;

    let captchaId = '';
    async function refreshCaptcha() {
      try {
        const d = await API.captcha();
        captchaId = d.captcha_id;
        document.getElementById('cimg').src = 'data:image/png;base64,' + d.image_base64;
        document.getElementById('c').value = '';
      } catch (e) { /* toast 已提示 */ }
    }
    refreshCaptcha();
    document.getElementById('cimg').addEventListener('click', refreshCaptcha);

    document.getElementById('login-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      try {
        const data = await API.login(
          document.getElementById('u').value,
          document.getElementById('p').value,
          captchaId,
          document.getElementById('c').value
        );
        Auth.setTokens(data);
        if (data.password_expired) {
          sessionStorage.setItem('must_change_password', '1');
          AppUI.toast(data.message || '密码已过期，请先修改密码', 'warning');
          showApp().then(() => { window.location.hash = '#/change-password'; });
          return;
        }
        if (data.password_expire_warning && data.password_expire_in_days != null) {
          AppUI.toast('密码将在 ' + data.password_expire_in_days + ' 天后过期，请及时修改', 'warning');
        }
        AppUI.toast('登录成功', 'success');
        showApp();
      } catch (err) {
        refreshCaptcha();  // 登录失败刷新验证码，提升安全性
      }
    });
  }

  // ---------- hash 路由 ----------
  function route() {
    const hash = window.location.hash.replace(/^#/, '') || '/dashboard';
    const page = PAGES[hash];
    const el = document.getElementById('content');
    const crumb = document.getElementById('crumb');
    if (!page) {
      if (el) el.innerHTML = '<div class="empty">页面不存在</div>';
      return;
    }
    // 菜单高亮
    document.querySelectorAll('.menu-item').forEach((x) => {
      x.classList.toggle('active', (x.dataset.path || '') === hash);
    });
    // breadcrumb
    const names = [];
    (function find(mms) {
      for (const m of mms) {
        if (m.path === '#' + hash) { names.push(m.name); return true; }
        if (m.children && m.children.length && find(m.children)) { names.unshift(m.name); return true; }
      }
      return false;
    })(menus);
    if (crumb && names.length) crumb.textContent = names.join(' / ');
    page.render(el, { user: currentUser, menus, refreshMenus }).catch((err) => {
      if (el) el.innerHTML = '<div class="empty">加载失败: ' + AppUI.escapeHtml(err.message) + '</div>';
    });
  }

  window.addEventListener('hashchange', route);

  function gotoChangePassword() {
    // 428 网关或登录标记触发的强制改密跳转
    sessionStorage.setItem('must_change_password', '1');
    if (document.getElementById('content')) {
      window.location.hash = '#/change-password';
    } else {
      showApp().then(() => { window.location.hash = '#/change-password'; });
    }
  }

  function hasPerm(code) {
    if (!currentUser) return false;
    if (currentUser.is_superuser) return true;
    return (currentUser.permissions || []).includes(code);
  }

  window.App = {
    registerPage, PAGES, route, showLogin, showApp, refreshMenus,
    gotoChangePassword, can: hasPerm,
    get currentUser() { return currentUser; },
    set currentUser(v) { currentUser = v; },
  };

  // 启动
  async function boot() {
    if (Auth.getToken()) {
      try {
        await showApp();
        return;
      } catch (e) {
        Auth.clearTokens();
      }
    }
    showLogin();
  }

  document.addEventListener('DOMContentLoaded', boot);
})();
