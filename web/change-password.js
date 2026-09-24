'use strict';

const form = document.getElementById('form');
const errEl = document.getElementById('err');
const okEl = document.getElementById('ok');
const submitBtn = document.getElementById('submit');

function show(el, msg) {
  el.textContent = msg;
  el.classList.add('show');
}
function hide(el) {
  el.textContent = '';
  el.classList.remove('show');
}
function safeNext() {
  const p = new URLSearchParams(location.search);
  const n = (p.get('next') || '').trim();
  if (!n) return '/ui/chat.html';
  try {
    const url = new URL(n, location.origin);
    if (url.origin !== location.origin) return '/ui/chat.html';
    if (!url.pathname.startsWith('/ui/') && !url.pathname.startsWith('/share/')) return '/ui/chat.html';
    if (url.pathname.endsWith('/change-password.html')) return '/ui/chat.html';
    return url.pathname + url.search + url.hash;
  } catch (_) {
    return '/ui/chat.html';
  }
}

function passwordCharsetError(value) {
  return /^[\x21-\x7E]+$/.test(value)
    ? ''
    : 'パスワードは半角英数字・記号のみを使ってください（全角文字・空白は使えません）';
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  hide(errEl); hide(okEl);
  const current = document.getElementById('current-password').value;
  const next = document.getElementById('new-password').value;
  const confirm = document.getElementById('confirm-password').value;
  if (!current || !next || !confirm) {
    show(errEl, 'すべての項目を入力してください');
    return;
  }
  if (next !== confirm) {
    show(errEl, '新しいパスワードと確認入力が一致しません');
    return;
  }
  const charsetError = passwordCharsetError(next);
  if (charsetError) {
    show(errEl, charsetError);
    return;
  }
  if (next.length < 8) {
    show(errEl, '新しいパスワードは8文字以上にしてください');
    return;
  }

  submitBtn.disabled = true;
  submitBtn.textContent = '変更中…';
  try {
    const r = await fetch('/auth/change-password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        current_password: current,
        new_password: next,
        confirm_password: confirm,
      }),
    });
    let data = {};
    try { data = await r.json(); } catch (_) { data = {}; }
    if (!r.ok) {
      show(errEl, data.detail || 'パスワードを変更できませんでした');
      return;
    }
    show(okEl, 'パスワードを変更しました');
    setTimeout(() => { window.location.href = safeNext(); }, 350);
  } catch (_) {
    show(errEl, '通信エラーが発生しました。ページを再読み込みしてもう一度お試しください。');
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = '変更して続ける';
  }
});

(async () => {
  try {
    const r = await fetch('/auth/me');
    if (r.status === 401) {
      window.location.href = '/ui/login.html?next=' + encodeURIComponent('/ui/change-password.html' + location.search);
      return;
    }
    // ログイン済みならフラグの有無を問わず表示する——強制変更（must_change_password）専用に
    // していた頃の「フラグ無しは即リダイレクト」は、メニューからの任意のパスワード変更を
    // 一瞬で閉じてしまう（実環境指摘 2026-09-02）。
  } catch (_) {
    show(errEl, 'ログイン状態を確認できません。もう一度ログインしてください。');
  }
})();
