// 管理者: ユーザー管理画面。GET/POST /admin/users, POST /admin/users/import, PATCH /admin/users/{uid}。
// 設計: docs/design/users.md「役割と管理者の権限」
// セキュリティ: server data は全て esc()。data-* 委譲でインライン handler なし。
'use strict';

// システム管理のタブから iframe（?embed=1）で開かれた時は、共通トップバー/ナビを隠す（CSS は .embedded 修飾）。
if (new URLSearchParams(location.search).has('embed')) {
  document.documentElement.classList.add('embedded');
}

const $ = Sherpa.$, esc = Sherpa.esc, getJSON = Sherpa.getJSON, api = Sherpa.api, fmtDateTime = Sherpa.fmtDateTime;

function toast(msg) {
  const t = $('toast'); if (!t) return;
  t.textContent = msg; t.classList.add('show');
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 1800);
}

function setButtonLoading(btn, on, label, loadingLabel) {
  if (!btn) return;
  btn.disabled = on;
  btn.innerHTML = on
    ? `<span class="loading-inline"><span class="spinner spinner-sm"></span><span>${esc(loadingLabel)}</span></span>`
    : esc(label);
}

function passwordCharsetError(value) {
  return /^[\x21-\x7E]+$/.test(value)
    ? ''
    : 'パスワードは半角英数字・記号のみを使ってください（全角文字・空白は使えません）';
}

// ===== admin ガード: /auth/me でロールを確認 =====
async function checkAdmin() {
  try {
    const u = await getJSON('/auth/me');
    if (u && u.role === 'admin') return true;
  } catch (_) { /* compat mode: admin 合成が返る */ }
  return false;
}

// ===== ユーザー一覧の描画 =====
function roleLabel(r) {
  return r === 'admin'
    ? '<span class="badge-admin">管理者</span>'
    : '<span class="badge-user">ユーザー</span>';
}
function statusLabel(s) {
  // DB契約は active/disabled/pending。disabled 以外を無条件で「有効」にすると pending が誤表示されるため個別に分岐する。
  if (s === 'disabled') return '<span class="badge-disabled">無効</span>';
  if (s === 'pending') return '<span class="badge-pending">保留</span>';
  return '<span style="color:var(--ok);font-size:var(--text-small)">● 有効</span>';
}

function renderUsers(users, total) {
  const tbody = $('user-tbody');
  const empty = $('table-empty');
  const count = $('user-count');
  tbody.setAttribute('aria-busy', 'false');
  if (count) {
    count.textContent = (total != null && total !== users.length)
      ? `(${users.length}/${total} 人)`
      : `(${users.length} 人)`;
  }
  if (!users.length) {
    tbody.innerHTML = '';
    if (empty) {
      empty.textContent = (total && total > 0)
        ? '条件に一致するユーザーがいません（検索語・状態フィルターを見直してください）'
        : 'ユーザーはまだいません';
      empty.style.display = '';
    }
    return;
  }
  if (empty) empty.style.display = 'none';
  tbody.innerHTML = users.map((u) => {
    const last = u.last_login_at
      ? esc(fmtDateTime(u.last_login_at))
      : '<span style="color:var(--ink-3)">未ログイン</span>';
    return `<tr>
      <td style="font-weight:600;font-family:var(--font-code,monospace)">${esc(u.uid)}</td>
      <td>${esc(u.display_name || '—')}</td>
      <td>${roleLabel(u.role)}</td>
      <td>${statusLabel(u.status)}</td>
      <td>${last}</td>
      <td style="white-space:nowrap">
        <button class="act-btn" data-edit="${esc(u.uid)}" data-role="${esc(u.role)}" data-status="${esc(u.status)}" data-name="${esc(u.display_name || '')}">編集</button>
      </td>
    </tr>`;
  }).join('');
}

// ===== 検索・状態フィルター（クライアント側） =====
let _allUsers = [];

function applyFilters() {
  const q = ($('f-q').value || '').trim().toLowerCase();
  const statusFilter = $('f-status').value;
  let list = _allUsers;
  // 「有効のみ」は status === 'active' に厳密一致。pending は「すべて」でのみ見える。
  if (statusFilter === 'active') list = list.filter((u) => u.status === 'active');
  else if (statusFilter === 'disabled') list = list.filter((u) => u.status === 'disabled');
  if (q) {
    list = list.filter((u) =>
      (u.uid || '').toLowerCase().includes(q) ||
      (u.display_name || '').toLowerCase().includes(q) ||
      (u.email || '').toLowerCase().includes(q));
  }
  renderUsers(list, _allUsers.length);
}

// 読込中はフィルターを無効化し、成功時だけ再有効化する（空のまま・古い _allUsers のまま誤描画しないため）。
function setFiltersEnabled(enabled) {
  const q = $('f-q'), s = $('f-status');
  if (q) q.disabled = !enabled;
  if (s) s.disabled = !enabled;
}

function setUsersLoading(message) {
  const tbody = $('user-tbody');
  const empty = $('table-empty');
  const count = $('user-count');
  setFiltersEnabled(false);
  if (empty) empty.style.display = 'none';
  if (count) count.textContent = '';
  tbody.setAttribute('aria-busy', 'true');
  tbody.innerHTML = `<tr><td colspan="6" style="padding:18px">
    <span class="loading-inline" role="status">
      <span class="spinner spinner-sm"></span><span>${esc(message)}</span>
    </span>
  </td></tr>`;
}

async function loadUsers() {
  setUsersLoading('ユーザー一覧を読み込んでいます...');
  try {
    const d = await getJSON('/admin/users');
    _allUsers = d.users || [];
    applyFilters();
    setFiltersEnabled(true);
  } catch (e) {
    $('user-tbody').setAttribute('aria-busy', 'false');
    $('user-tbody').innerHTML = `<tr><td colspan="6" style="color:var(--danger);padding:16px">読み込みに失敗しました: ${esc(String(e))}</td></tr>`;
    // フィルターは無効のまま（setUsersLoading で無効化済み）＝失敗表示中の操作で誤描画させない。
  }
}

// ===== 新規ユーザー作成 =====
$('nu-submit').addEventListener('click', async () => {
  const uid = ($('nu-uid').value || '').trim();
  const display_name = ($('nu-name').value || '').trim();
  const role = $('nu-role').value;
  const password = $('nu-pw').value;
  const errEl = $('nu-err');
  errEl.textContent = '';

  if (!uid) { errEl.textContent = 'ユーザー名は必須です'; return; }
  if (!password) { errEl.textContent = '初期パスワードは必須です'; return; }
  const charsetError = passwordCharsetError(password);
  if (charsetError) { errEl.textContent = charsetError; return; }
  setButtonLoading($('nu-submit'), true, '追加', '追加中...');
  try {
    await api('POST', '/admin/users', { uid, display_name: display_name || undefined, role, password });
    // フォームをクリア
    $('nu-uid').value = ''; $('nu-name').value = ''; $('nu-pw').value = '';
    $('nu-role').value = 'user';
    toast('ユーザーを追加しました');
    await loadUsers();
  } catch (e) {
    errEl.textContent = String(e);
  } finally {
    setButtonLoading($('nu-submit'), false, '追加', '追加中...');
  }
});

// ===== CSV で一括追加 =====
$('csv-submit').addEventListener('click', async () => {
  const errEl = $('csv-err');
  errEl.textContent = '';
  const f = $('csv-file').files[0];
  if (!f) { errEl.textContent = 'CSV ファイルを選んでください'; return; }
  const fd = new FormData();
  fd.append('file', f, f.name);
  setButtonLoading($('csv-submit'), true, '取り込む', '取り込み中...');
  try {
    const r = await fetch('/admin/users/import', { method: 'POST', body: fd });
    let d = null;
    try { d = await r.json(); } catch (_) {}
    if (r.ok) {
      $('csv-file').value = '';
      toast(`${d.created} 人を追加しました`);
      await loadUsers();
    } else if (d && Array.isArray(d.errors)) {
      errEl.innerHTML = '<div>次の行を直してください（誰も追加されていません）</div><ul>' +
        d.errors.map((e) => `<li>${esc(String(e.line))} 行目${e.uid ? `（${esc(e.uid)}）` : ''}: ${esc(e.reason)}</li>`).join('') + '</ul>';
    } else {
      errEl.textContent = (d && (d.detail || d.message)) || `エラー (${r.status})`;
    }
  } catch (e) {
    errEl.textContent = '通信に失敗しました: ' + String(e);
  } finally {
    setButtonLoading($('csv-submit'), false, '取り込む', '取り込み中...');
  }
});

// ===== 編集ダイアログ =====
function closeEditDialog() {
  $('edit-overlay').hidden = true;
}

// ダイアログを開いた時点の値。保存時にここと比較し、実際に変わったキーだけを PATCH へ送る。
let _editOriginal = null;

function openEditDialog(uid, role, status, displayName) {
  const normName = displayName || '';
  $('edit-uid').value = uid;
  $('edit-name').value = normName;
  $('edit-role').value = role;
  const statusSel = $('edit-status');
  // 「保留（現状のまま）」は元状態が pending のユーザーだけに表示・選択可能にする（実 API は active/disabled のみ許可）。
  let pendingOpt = statusSel.querySelector('option[value="pending"]');
  if (status === 'pending') {
    if (!pendingOpt) {
      pendingOpt = document.createElement('option');
      pendingOpt.value = 'pending';
      pendingOpt.textContent = '保留（現状のまま）';
      statusSel.appendChild(pendingOpt);
    }
  } else if (pendingOpt) {
    pendingOpt.remove();
  }
  statusSel.value = status;
  $('edit-pw').value = '';
  $('edit-err').textContent = '';
  _editOriginal = { role, status, display_name: normName };
  $('edit-modal-title').textContent = `ユーザーを編集: ${uid}`;
  $('edit-overlay').hidden = false;
}

$('edit-submit').addEventListener('click', async () => {
  const uid = $('edit-uid').value;
  const displayNameRaw = $('edit-name').value || '';
  const role = $('edit-role').value;
  const status = $('edit-status').value;
  const password = $('edit-pw').value || undefined;
  const errEl = $('edit-err');
  errEl.textContent = '';
  if (password) {
    const charsetError = passwordCharsetError(password);
    if (charsetError) { errEl.textContent = charsetError; return; }
  }
  // 実際に変わったキーだけを送る。表示名は raw（前後空白を保ったまま）で元値と比較し、変わっていた場合だけ trim して送る（空文字への変更はサーバ契約で表示名を「消す」）。
  const patch = {};
  if (_editOriginal) {
    if (displayNameRaw !== _editOriginal.display_name) patch.display_name = displayNameRaw.trim();
    if (role !== _editOriginal.role) patch.role = role;
    if (status !== _editOriginal.status) patch.status = status;
  }
  if (password) patch.password = password;
  if (!Object.keys(patch).length) {
    errEl.textContent = '変更点がありません';
    return;
  }
  const disabling = patch.status === 'disabled';
  setButtonLoading($('edit-submit'), true, '保存', '保存中...');
  try {
    await api('PATCH', `/admin/users/${encodeURIComponent(uid)}`, patch);
    closeEditDialog();
    // 無効化した行は既定フィルターで一覧から消えるため、削除と誤認されないよう明示する（フィルターは自動で切り替えない）。
    toast(disabling ? '無効化しました。『すべて』または『無効のみ』で確認できます' : '変更しました');
    await loadUsers();
  } catch (e) {
    errEl.textContent = String(e);
  } finally {
    setButtonLoading($('edit-submit'), false, '保存', '保存中...');
  }
});

// 委譲クリック: 編集ボタン
$('user-tbody').addEventListener('click', (e) => {
  const btn = e.target.closest('[data-edit]');
  if (btn) openEditDialog(btn.dataset.edit, btn.dataset.role, btn.dataset.status, btn.dataset.name);
});

// 検索・状態フィルター（クライアント側フィルタ・入力のたびに即時再描画）
$('f-q').addEventListener('input', applyFilters);
$('f-status').addEventListener('change', applyFilters);

// overlay 外クリックで閉じる
$('edit-overlay').addEventListener('click', (e) => {
  if (e.target === $('edit-overlay')) closeEditDialog();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !$('edit-overlay').hidden) closeEditDialog();
});

// テーマ切替
function applyThemeIcon() {
  const tb = $('themebtn');
  if (tb) tb.textContent = document.documentElement.dataset.theme === 'dark' ? '☀️' : '🌙';
}
const themebtn = $('themebtn');
if (themebtn) {
  themebtn.addEventListener('click', () => {
    const d = document.documentElement;
    const next = d.dataset.theme === 'dark' ? 'light' : 'dark';
    d.dataset.theme = next; localStorage.setItem('sherpa-theme', next); applyThemeIcon();
  });
}
applyThemeIcon();

// ===== 初期化 =====
(async () => {
  closeEditDialog();
  const isAdmin = await checkAdmin();
  if (!isAdmin) {
    // admin でなければアクセス拒否メッセージを表示してコンテンツを隠す。
    const main = $('main-content');
    const denied = $('access-denied');
    if (main) main.style.display = 'none';
    if (denied) denied.style.display = 'block';
    return;
  }
  await loadUsers();
})();
