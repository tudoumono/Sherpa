// ログイン画面（Slice3）。POST /auth/login → 成功で next= or /ui/chat.html へ redirect。
// セキュリティ: esc() は textContent 専用のため innerHTML に使わない。エラー文言は固定文字列のみ。
'use strict';

const form = document.getElementById('form');
const errEl = document.getElementById('err');
const submitBtn = document.getElementById('submit');

function showErr(msg) {
  errEl.textContent = msg;
  errEl.classList.add('show');
}
function clearErr() {
  errEl.textContent = '';
  errEl.classList.remove('show');
}

// ?next= パラメータを読む（共有リンククリック後のリダイレクト先）。
// オープンリダイレクト防止: 必ず同一オリジンかつ許可パスに限定する。
function nextUrl() {
  const p = new URLSearchParams(location.search);
  const n = (p.get('next') || '').trim();
  if (!n) return '/ui/chat.html';
  // URL として parse して同一オリジン・許可パス（/ui/ または /share/）か検証する。
  try {
    const url = new URL(n, location.origin);
    if (url.origin !== location.origin) return '/ui/chat.html';
    // 許可するパスプレフィックス（/ui/ と /share/）のみ通す。
    const allowed = ['/ui/', '/share/'];
    if (!allowed.some((prefix) => url.pathname.startsWith(prefix))) return '/ui/chat.html';
    return url.pathname + url.search + url.hash;
  } catch (_) {
    return '/ui/chat.html';
  }
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  clearErr();
  const username = document.getElementById('username').value.trim();
  const password = document.getElementById('password').value;
  if (!username || !password) { showErr('ユーザー名とパスワードを入力してください'); return; }

  submitBtn.disabled = true;
  submitBtn.textContent = 'ログイン中…';
  try {
    const r = await fetch('/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    if (r.ok) {
      let data = {};
      try { data = await r.json(); } catch (_) { data = {}; }
      if (data.must_change_password) {
        window.location.href = '/ui/change-password.html?next=' + encodeURIComponent(nextUrl());
      } else {
        window.location.href = nextUrl();
      }
    } else {
      // 401 / その他はすべて汎用メッセージ（サーバ側のユーザー存在情報を漏らさない）。
      showErr('ユーザー名またはパスワードが正しくありません');
    }
  } catch (_) {
    showErr('通信エラーが発生しました。ページを再読み込みしてもう一度お試しください。');
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = 'ログイン';
  }
});
