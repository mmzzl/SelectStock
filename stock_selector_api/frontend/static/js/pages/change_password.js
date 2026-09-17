/* 修改密码页：支持强制改密（must_change_password）与个人自助改密。 */
(function () {
  const FORCED = sessionStorage.getItem('must_change_password') === '1';

  App.registerPage('/change-password', {
    render: (el, ctx) => {
      el.innerHTML = `
        <div class="card">
          <h3>修改密码${FORCED ? '<span class="tag-warn">密码已过期，必须先修改密码</span>' : ''}</h3>
          <div class="form">
            <label>旧密码<input id="oldp" type="password" autocomplete="current-password" /></label>
            <label>新密码<input id="newp" type="password" autocomplete="new-password" placeholder="复杂度按系统策略" /></label>
            <label>确认新密码<input id="newp2" type="password" autocomplete="new-password" /></label>
            <div class="btn-row">
              <button class="btn btn-primary" id="save-pw">保存</button>
              ${FORCED ? '' : '<button class="btn" id="cancel-pw">取消</button>'}
            </div>
          </div>
        </div>`;

      document.getElementById('save-pw').addEventListener('click', async () => {
        const oldp = document.getElementById('oldp').value;
        const np = document.getElementById('newp').value;
        const np2 = document.getElementById('newp2').value;
        if (!oldp || !np) { AppUI.toast('请完整填写', 'error'); return; }
        if (np !== np2) { AppUI.toast('两次新密码不一致', 'error'); return; }
        try {
          await API.changePassword(oldp, np);
          sessionStorage.removeItem('must_change_password');
          AppUI.toast('密码修改成功，请使用新密码重新登录', 'success');
          // 后端已吊销会话（session 模式）或建议重新登录：清令牌回登录页
          Auth.clearTokens();
          window.location.reload();
        } catch (e) { /* toast 已提示 */ }
      });
      const cancel = document.getElementById('cancel-pw');
      if (cancel) cancel.addEventListener('click', () => { window.location.hash = '#/dashboard'; });
    },
  });
})();
