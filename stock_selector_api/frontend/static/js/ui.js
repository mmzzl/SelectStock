/* 通用 UI 工具：toast 提示、弹窗、确认框、HTML 转义 */
(function () {
  function escapeHtml(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function toast(msg, type = 'info') {
    let box = document.getElementById('toast-box');
    if (!box) {
      box = document.createElement('div');
      box.id = 'toast-box';
      box.style.cssText = 'position:fixed;top:16px;right:16px;z-index:9999;max-width:360px;';
      document.body.appendChild(box);
    }
    const el = document.createElement('div');
    const color = type === 'error' ? '#f56c6c' : (type === 'success' ? '#67c23a' : '#909399');
    el.style.cssText =
      'background:#fff;border-left:4px solid ' + color + ';color:#303133;padding:10px 14px;' +
      'border-radius:4px;box-shadow:0 2px 8px rgba(0,0,0,.12);margin-bottom:8px;font-size:14px;';
    el.textContent = msg;
    box.appendChild(el);
    setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .3s'; }, 2500);
    setTimeout(() => el.remove(), 2900);
  }

  function confirmBox(msg) {
    return window.confirm(msg);
  }

  // 简单 modal（返回 Promise<formData> 或 null）
  function modalHtml(title, fieldsHtml) {
    return (
      '<div class="modal-mask" id="modal-mask">' +
        '<div class="modal">' +
          '<div class="modal-title">' + escapeHtml(title) + '<span class="modal-close" data-close>×</span></div>' +
          '<div class="modal-body">' + fieldsHtml + '</div>' +
          '<div class="modal-foot">' +
            '<button class="btn" data-close>取消</button>' +
            '<button class="btn btn-primary" id="modal-ok">确定</button>' +
          '</div>' +
        '</div>' +
      '</div>'
    );
  }

  // 打开一个表单弹窗；onOk(formData) 返回 Promise
  function openForm(title, fieldsHtml, onOk) {
    document.querySelector('.modal-mask')?.remove();
    const wrap = document.createElement('div');
    wrap.innerHTML = modalHtml(title, fieldsHtml);
    document.body.appendChild(wrap.firstElementChild ? wrap.firstElementChild : wrap);
    const mask = document.getElementById('modal-mask');
    const inputs = Array.from(mask.querySelectorAll('[data-field]'));
    mask.querySelectorAll('[data-close]').forEach((el) => {
      el.addEventListener('click', () => mask.remove());
    });
    mask.querySelector('#modal-ok').addEventListener('click', async () => {
      const form = {};
      inputs.forEach((i) => { form[i.dataset.field] = i.type === 'checkbox' ? i.checked : i.value; });
      try {
        await onOk(form);
        mask.remove();
      } catch (e) { /* toast 已提示 */ }
    });
  }

  // 表单字段辅助：input / select / textarea
  function fInput(field, label, value = '') {
    return '<div class="frow"><label>' + escapeHtml(label) + '</label><input data-field="' + field + '" value="' + escapeHtml(value) + '"></div>';
  }
  function fPassword(field, label) {
    return '<div class="frow"><label>' + escapeHtml(label) + '</label><input data-field="' + field + '" type="password" value=""></div>';
  }
  function fSelect(field, label, options, value) {
    const opts = options.map((o) =>
      '<option value="' + escapeHtml(o.value) + '"' + (o.value === value ? ' selected' : '') + '>' + escapeHtml(o.label) + '</option>'
    ).join('');
    return '<div class="frow"><label>' + escapeHtml(label) + '</label><select data-field="' + field + '">' + opts + '</select></div>';
  }
  function fTextarea(field, label, value = '') {
    return '<div class="frow"><label>' + escapeHtml(label) + '</label><textarea data-field="' + field + '">' + escapeHtml(value) + '</textarea></div>';
  }

  window.AppUI = {
    ...(window.AppUI || {}),
    escapeHtml, toast, confirmBox, openForm, fInput, fPassword, fSelect, fTextarea,
  };
})();
