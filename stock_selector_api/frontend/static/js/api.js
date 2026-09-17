/* API 封装：自动带 token、统一错误提示、401 跳登录 */
(function () {
  const API_PREFIX = '/api/v1';

  function getToken() {
    return localStorage.getItem('access_token');
  }

  function setTokens(data) {
    if (data && data.access_token) {
      localStorage.setItem('access_token', data.access_token);
    }
    if (data && data.refresh_token) {
      localStorage.setItem('refresh_token', data.refresh_token);
    }
  }

  function clearTokens() {
    localStorage.removeItem('access_token');
    localStorage.removeItem('refresh_token');
  }

  function hashToLogin() {
    // 回到登录（刷新以重跑 boot：session 模式也正确回登录页）
    window.location.hash = '#/';
    window.location.reload();
  }

  async function request(method, path, body, opts = {}) {
    const headers = { 'Content-Type': 'application/json' };
    const token = getToken();
    if (token) headers['Authorization'] = 'Bearer ' + token;

    let res;
    try {
      res = await fetch(API_PREFIX + path, {
        method,
        headers,
        body: body ? JSON.stringify(body) : undefined,
        credentials: 'same-origin',
      });
    } catch (e) {
      AppUI.toast('网络请求失败: ' + e.message, 'error');
      throw e;
    }

    // 401：尝试刷新一次
    if (res.status === 401 && !opts.noRefresh) {
      const refreshed = await tryRefresh();
      if (refreshed) {
        return request(method, path, body, { ...opts, noRefresh: true });
      }
      clearTokens();
      hashToLogin();
      throw new Error('未登录');
    }

    // 428：密码已过期，仅允许改密/登出等豁免接口，跳强制改密页
    if (res.status === 428) {
      sessionStorage.setItem('must_change_password', '1');
      if (window.App && window.App.gotoChangePassword) window.App.gotoChangePassword();
      const data428 = await res.json().catch(() => null);
      throw new Error((data428 && data428.message) || '密码已过期，请先修改密码');
    }

    let data = null;
    try { data = await res.json(); } catch (e) { /* 非 JSON */ }
    if (!res.ok || (data && data.success === false)) {
      const msg = (data && data.message) || ('请求失败 (' + res.status + ')');
      AppUI.toast(msg, 'error');
      throw new Error(msg);
    }
    return data ? data.data : data;
  }

  async function tryRefresh() {
    const refresh = localStorage.getItem('refresh_token');
    if (!refresh) return false;
    try {
      const res = await fetch(API_PREFIX + '/auth/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: refresh }),
      });
      const data = await res.json();
      if (res.ok && data.success) {
        setTokens(data.data);
        return true;
      }
    } catch (e) { /* ignore */ }
    return false;
  }

  // 登录/登出/当前用户/菜单
  const api = {
    login: (u, p, captchaId, captchaCode) => request('POST', '/auth/login', {
      username: u, password: p,
      captcha_id: captchaId || null, captcha_code: captchaCode || null,
    }),
    captcha: () => request('GET', '/auth/captcha'),
    logout: () => request('POST', '/auth/logout'),
    changePassword: (oldPassword, newPassword) => request('POST', '/auth/change-password', {
      old_password: oldPassword, new_password: newPassword,
    }),
    updateProfile: (data) => request('PUT', '/auth/me', data),
    resetPassword: (userId, newPassword) => request('POST', '/sys/users/' + userId + '/password', {
      new_password: newPassword,
    }),
    me: () => request('GET', '/auth/me'),
    menus: () => request('GET', '/auth/menus'),

    // 用户
    users: (p) => request('GET', '/sys/users?page=' + (p.page || 1) + '&size=' + (p.size || 20) + (p.keyword ? '&keyword=' + encodeURIComponent(p.keyword) : '')),
    createUser: (d) => request('POST', '/sys/users', d),
    updateUser: (id, d) => request('PUT', '/sys/users/' + id, d),
    deleteUser: (id) => request('DELETE', '/sys/users/' + id),

    // 角色
    roles: () => request('GET', '/sys/roles'),
    createRole: (d) => request('POST', '/sys/roles', d),
    updateRole: (id, d) => request('PUT', '/sys/roles/' + id, d),
    deleteRole: (id) => request('DELETE', '/sys/roles/' + id),
    assignRolePerms: (id, ids) => request('POST', '/sys/roles/' + id + '/permissions', { permission_ids: ids }),

    // 权限
    permissions: (module) => request('GET', '/sys/permissions' + (module ? '?module=' + encodeURIComponent(module) : '')),
    permissionModules: () => request('GET', '/sys/permissions/modules'),
    permissionTree: () => request('GET', '/sys/permissions/tree'),
    roleDetail: (id) => request('GET', '/sys/roles/' + id),

    // 通知
    notifyEvents: () => request('GET', '/notify/events'),
    notifyChannels: () => request('GET', '/notify/channels'),
    notifyChannelUpsert: (data) => (data.key
      ? request('PUT', '/notify/channels/' + encodeURIComponent(data.key), data)
      : request('POST', '/notify/channels', data)),
    notifyChannelDelete: (key) => request('DELETE', '/notify/channels/' + encodeURIComponent(key)),
    notifyChannelTest: (key, text, to) => request('POST', '/notify/channels/' + encodeURIComponent(key) + '/test', { text, to }),
    notifyPolicyGet: () => request('GET', '/notify/policy'),
    notifyPolicyPut: (policy) => request('PUT', '/notify/policy', { policy }),

    // 菜单
    menusManage: () => request('GET', '/sys/menus'),
    menuTree: () => request('GET', '/sys/menus/tree'),
    createMenu: (d) => request('POST', '/sys/menus', d),
    updateMenu: (id, d) => request('PUT', '/sys/menus/' + id, d),
    deleteMenu: (id) => request('DELETE', '/sys/menus/' + id),

    // 设备（示例）
    devices: (p) => request('GET', '/devices?page=' + (p.page || 1) + '&size=' + (p.size || 20) + (p.keyword ? '&keyword=' + encodeURIComponent(p.keyword) : '') + (p.status ? '&status=' + p.status : '')),
    deviceSummary: () => request('GET', '/devices/summary'),
    createDevice: (d) => request('POST', '/devices', d),
    updateDevice: (id, d) => request('PUT', '/devices/' + id, d),
    deleteDevice: (id) => request('DELETE', '/devices/' + id),
  };

  window.API = api;
  window.Auth = { getToken, setTokens, clearTokens };
  window.AppUI = window.AppUI || {};
})();
