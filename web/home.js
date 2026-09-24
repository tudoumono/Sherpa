// トップ画面（運営掲示板）。GET /announcements で一覧表示、admin は同一画面で投稿・編集・削除・公開切替。
// チャットへの導線は 1 クリック（.home-cta）＝タスク起点・チャット主入口の原則（04-画面の原則.md）を崩さない。
// セキュリティ: 本文は esc() 後に改行だけ <br> に変換（Markdown/HTML は一切描画しない＝XSS 回避）。
'use strict';

const $ = Sherpa.$, esc = Sherpa.esc, getJSON = Sherpa.getJSON, api = Sherpa.api, fmtDateTime = Sherpa.fmtDateTime;

const CATEGORY_LABEL = { maintenance: 'メンテナンス', case: '活用事例', notice: 'お知らせ' };
const CATEGORY_ORDER = ['notice', 'case', 'maintenance'];

let _isAdmin = false;
let _items = [];          // 直近の GET 結果（admin は include_unpublished=1 で非公開も含む）
let _editingId = null;    // 編集中の announcement id（同時に1件のみ）
let _postFormOpen = false;   // 投稿フォームは既定で折りたたみ＝お知らせ一覧を先に見せる（トリガーボタンで開閉）

function toast(msg) {
  const t = $('toast'); if (!t) return;
  t.textContent = msg; t.classList.add('show');
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 1800);
}

function nl2br(s) {
  return esc(s).replace(/\n/g, '<br>');
}

function fmtDate(iso) {
  return fmtDateTime(iso, { dateOnly: true });
}

// S4: datetime-local input ⇄ ISO 8601（UTC）変換（公開/削除タイマー）。<input type=datetime-local> の値は
// ブラウザのローカル時刻（実質 JST）として解釈される＝ new Date(value) で正しくローカル→UTC 変換できる
// （S3 の時刻教訓＝素朴な文字列 slice は使わない）。
function dtLocalToISO(value) {
  if (!value) return '';
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? '' : d.toISOString();
}
function isoToDtLocal(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}
const STATUS_BADGE = {
  scheduled: '<span class="unpub-badge" title="公開日時になると自動的に公開されます">⏳ 予約公開待ち</span>',
  expired: '<span class="unpub-badge" title="掲載終了日時を過ぎています（間もなく自動削除）">⌛ 掲載終了</span>',
};

// ===== admin ガード（/auth/me の role で出し分け・nav.js の _isAdminUser の流儀） =====
async function checkAdmin() {
  try {
    const u = await getJSON('/auth/me');
    if (u && u.role === 'admin') return true;
  } catch (_) { /* compat: 未ログイン等はお知らせ閲覧のみ */ }
  return false;
}

// ===== 投稿フォーム（admin のみ・既定は折りたたみ） =====
function renderPostForm() {
  const wrap = $('post-form-wrap');
  if (!_isAdmin) { wrap.innerHTML = ''; return; }
  if (!_postFormOpen) {
    wrap.innerHTML = '<button class="btn-ghost post-form-toggle" id="pf-open" type="button">＋ お知らせを投稿</button>';
    $('pf-open').addEventListener('click', () => { _postFormOpen = true; renderPostForm(); $('pf-title').focus(); });
    return;
  }
  wrap.innerHTML = `
    <div class="card post-card">
      <div style="padding:16px 18px">
        <h2>お知らせを投稿</h2>
        <div class="pform">
          <div class="pform-row">
            <div class="col-title">
              <label for="pf-title">タイトル</label>
              <input id="pf-title" type="text" placeholder="例: 定期メンテナンスのお知らせ">
            </div>
            <div class="col-cat">
              <label for="pf-cat">種別</label>
              <select id="pf-cat">
                <option value="notice">お知らせ</option>
                <option value="case">活用事例</option>
                <option value="maintenance">メンテナンス</option>
              </select>
            </div>
          </div>
          <div>
            <label for="pf-body">本文</label>
            <textarea id="pf-body" placeholder="本文（改行はそのまま反映されます）"></textarea>
          </div>
          <div class="pform-row">
            <div class="col-cat">
              <label for="pf-publish">公開日時（省略=今すぐ）</label>
              <input id="pf-publish" type="datetime-local">
            </div>
            <div class="col-cat">
              <label for="pf-expire">掲載終了日時（省略=無期限）</label>
              <input id="pf-expire" type="datetime-local">
            </div>
          </div>
          <div class="pform-row" style="align-items:center">
            <label class="checkbox" style="margin:0"><input type="checkbox" id="pf-pinned">📌 ピン留めする</label>
            <div class="spacer"></div>
            <button class="mini" id="pf-cancel" type="button">キャンセル</button>
            <button class="btn-primary" id="pf-submit">投稿</button>
          </div>
          <div class="pform-err" id="pf-err"></div>
        </div>
      </div>
    </div>`;
  $('pf-submit').addEventListener('click', submitNewAnnouncement);
  $('pf-cancel').addEventListener('click', closePostForm);
}

// キャンセル・投稿成功の両方から使う共通の折りたたみ処理。消えるボタンの代わりに、
// 再表示される開くボタンへ明示的にフォーカスを移す（DOM から消えた要素にはフォーカスできず
// 何もしなければ body へ落ちてしまうため）。
function closePostForm() {
  _postFormOpen = false;
  renderPostForm();
  $('pf-open').focus();
}

async function submitNewAnnouncement() {
  const title = ($('pf-title').value || '').trim();
  const body = ($('pf-body').value || '').trim();
  const category = $('pf-cat').value;
  const pinned = $('pf-pinned').checked;
  const publishAt = dtLocalToISO($('pf-publish').value);
  const expireAt = dtLocalToISO($('pf-expire').value);
  const errEl = $('pf-err');
  errEl.textContent = '';
  if (!title) { errEl.textContent = 'タイトルは必須です'; return; }
  if (!body) { errEl.textContent = '本文は必須です'; return; }
  const btn = $('pf-submit');
  const cancelBtn = $('pf-cancel');
  // 送信中はキャンセル・再送信を無効化する。無効化しないと、POST 待機中にキャンセルで
  // フォーム DOM が破棄され、その後 POST が成功した際に `$('pf-title')` 等が null になり
  // （一覧更新・成功通知が走らないまま）サーバには投稿済み＝二重投稿を誘発する。
  btn.disabled = true;
  cancelBtn.disabled = true;
  try {
    const d = await api('POST', '/admin/announcements', {
      title, body, category, pinned, published: true,
      publish_at: publishAt || null, expire_at: expireAt || null,
    });
    _items = [d.announcement, ..._items];
    toast('お知らせを投稿しました');
    closePostForm();   // 折りたたみへ戻す（フィールドは DOM ごと破棄されるので個別クリア不要）
    renderList();
  } catch (e) {
    errEl.textContent = String(e);
    btn.disabled = false;
    cancelBtn.disabled = false;
  }
}

// ===== お知らせ一覧 =====
function sortItems(items) {
  return [...items].sort((a, b) => {
    if (a.pinned !== b.pinned) return a.pinned ? -1 : 1;
    return String(b.created_at).localeCompare(String(a.created_at));
  });
}

function adminControlsHTML(item) {
  if (!_isAdmin) return '';
  const pubLabel = item.published ? '非公開にする' : '公開する';
  return `
    <div class="ann-admin">
      <button class="act-btn" data-edit="${item.id}">編集</button>
      <button class="act-btn" data-toggle-pub="${item.id}">${esc(pubLabel)}</button>
      <button class="act-btn danger" data-delete="${item.id}">削除</button>
    </div>`;
}

function editFormHTML(item) {
  const cats = CATEGORY_ORDER.map((c) => (
    `<option value="${c}"${c === item.category ? ' selected' : ''}>${esc(CATEGORY_LABEL[c])}</option>`
  )).join('');
  return `
    <div class="pform">
      <div class="pform-row">
        <div class="col-title">
          <label>タイトル</label>
          <input type="text" id="ef-title-${item.id}" value="${esc(item.title)}">
        </div>
        <div class="col-cat">
          <label>種別</label>
          <select id="ef-cat-${item.id}">${cats}</select>
        </div>
      </div>
      <div>
        <label>本文</label>
        <textarea id="ef-body-${item.id}">${esc(item.body)}</textarea>
      </div>
      <div class="pform-row">
        <div class="col-cat">
          <label>公開日時（空=今すぐ）</label>
          <input type="datetime-local" id="ef-publish-${item.id}" value="${esc(isoToDtLocal(item.publish_at))}">
        </div>
        <div class="col-cat">
          <label>掲載終了日時（空=無期限）</label>
          <input type="datetime-local" id="ef-expire-${item.id}" value="${esc(isoToDtLocal(item.expire_at))}">
        </div>
      </div>
      <div class="pform-row" style="align-items:center">
        <label class="checkbox" style="margin:0">
          <input type="checkbox" id="ef-pinned-${item.id}"${item.pinned ? ' checked' : ''}>📌 ピン留めする
        </label>
        <div class="spacer"></div>
        <button class="btn-primary" data-save="${item.id}" style="height:34px">保存</button>
        <button class="mini" data-cancel="${item.id}">キャンセル</button>
      </div>
      <div class="pform-err" id="ef-err-${item.id}"></div>
    </div>`;
}

function itemHTML(item) {
  // category は API 側で3値 enum に検証済みだが、防御的に許可リスト以外は notice 扱いへ落とす（Sherpa.esc 徹底の趣旨）。
  const cat = CATEGORY_ORDER.includes(item.category) ? item.category : 'notice';
  const catCls = `cat-${cat}`;
  const catLabel = CATEGORY_LABEL[cat];
  const isEditing = _editingId === item.id;
  return `<div class="ann" data-item="${item.id}">
    <div class="ann-head">
      ${item.pinned ? '<span class="pin" title="ピン留め">📌</span>' : ''}
      <span class="cat-badge ${catCls}">${esc(catLabel)}</span>
      ${!item.published ? '<span class="unpub-badge">非公開</span>' : (STATUS_BADGE[item.status] || '')}
      <span class="ann-title">${esc(item.title)}</span>
      <span class="ann-date">${esc(fmtDate(item.created_at))}</span>
    </div>
    ${isEditing ? editFormHTML(item) : `<div class="ann-body">${nl2br(item.body)}</div>${adminControlsHTML(item)}`}
  </div>`;
}

function renderList() {
  const list = $('ann-list');
  const items = sortItems(_items);
  if (!items.length) {
    list.innerHTML = '<div class="ann-empty">お知らせはまだありません</div>';
    return;
  }
  list.innerHTML = items.map(itemHTML).join('');
}

async function loadAnnouncements() {
  $('ann-list').innerHTML = '<div class="ann-empty"><span class="loading-inline"><span class="spinner spinner-sm"></span><span>読み込んでいます...</span></span></div>';
  try {
    // admin は include_unpublished=1 で非公開記事も取得（再発見・再公開できるように）。
    const params = new URLSearchParams({ limit: '50' });
    if (_isAdmin) params.set('include_unpublished', '1');
    const d = await getJSON('/announcements?' + params.toString());
    _items = d.announcements || [];
    renderList();
  } catch (e) {
    $('ann-list').innerHTML = `<div class="ann-empty" style="color:var(--danger)">読み込みに失敗しました: ${esc(String(e))}</div>`;
  }
}

// ===== admin 操作（編集/削除/公開切替） =====
function findItem(id) {
  return _items.find((a) => a.id === id);
}

async function saveEdit(id) {
  const title = ($(`ef-title-${id}`).value || '').trim();
  const body = ($(`ef-body-${id}`).value || '').trim();
  const category = $(`ef-cat-${id}`).value;
  const pinned = $(`ef-pinned-${id}`).checked;
  const publishAt = dtLocalToISO($(`ef-publish-${id}`).value);
  const expireAt = dtLocalToISO($(`ef-expire-${id}`).value);
  const errEl = $(`ef-err-${id}`);
  if (errEl) errEl.textContent = '';
  if (!title) { if (errEl) errEl.textContent = 'タイトルは必須です'; return; }
  if (!body) { if (errEl) errEl.textContent = '本文は必須です'; return; }
  try {
    // S4: このフォームは常に両方の日時欄を送る（未変更でも）。空欄は ""＝サーバ側で NULL クリアと解釈される
    // （書込専用キーと同じ「未指定(省略)=変更しない・""=クリア」規約・空文字を明示的に送るのが重要）。
    const d = await api('PATCH', `/admin/announcements/${encodeURIComponent(id)}`,
      { title, body, category, pinned, publish_at: publishAt, expire_at: expireAt });
    _items = _items.map((a) => (a.id === id ? d.announcement : a));
    _editingId = null;
    toast('保存しました');
    renderList();
  } catch (e) {
    if (errEl) errEl.textContent = String(e);
  }
}

async function togglePublished(id) {
  const item = findItem(id);
  if (!item) return;
  try {
    const d = await api('PATCH', `/admin/announcements/${encodeURIComponent(id)}`, { published: !item.published });
    _items = _items.map((a) => (a.id === id ? d.announcement : a));
    toast(d.announcement.published ? '公開しました' : '非公開にしました');
    renderList();
  } catch (e) {
    toast('変更に失敗しました: ' + String(e));
  }
}

async function deleteItem(id) {
  if (!window.confirm('このお知らせを削除しますか？')) return;
  try {
    await api('DELETE', `/admin/announcements/${encodeURIComponent(id)}`);
    _items = _items.filter((a) => a.id !== id);
    toast('削除しました');
    renderList();
  } catch (e) {
    toast('削除に失敗しました: ' + String(e));
  }
}

// 委譲クリック（編集/保存/キャンセル/公開切替/削除）
$('ann-list').addEventListener('click', (e) => {
  const editBtn = e.target.closest('[data-edit]');
  if (editBtn) { _editingId = Number(editBtn.dataset.edit); renderList(); return; }
  const cancelBtn = e.target.closest('[data-cancel]');
  if (cancelBtn) { _editingId = null; renderList(); return; }
  const saveBtn = e.target.closest('[data-save]');
  if (saveBtn) { saveEdit(Number(saveBtn.dataset.save)); return; }
  const togBtn = e.target.closest('[data-toggle-pub]');
  if (togBtn) { togglePublished(Number(togBtn.dataset.togglePub)); return; }
  const delBtn = e.target.closest('[data-delete]');
  if (delBtn) { deleteItem(Number(delBtn.dataset.delete)); return; }
});

// ===== 通知（NOTIFY-1・非同期処理の完了/要対応・掲示板とは別区画・既読管理なし） =====
// 取り込み run の完了/失敗は誰でも見える。グラフ drift／LLM整形完了／OCR反映待ちは admin にだけ
// サーバ側（GET /notifications）が返す——ここでは受け取った内容をそのまま出すだけで役割判定はしない。
const NOTIF_POLL_MS = 45000;   // status.js の健全性ポーリングと同じ間隔（新しい常時ポーリングは追加しない）
const NOTIF_STATUS_ICON = { done: '✅', failed: '⚠️', warn: '⚠️' };
let _notifTimer = null;

function notifItemHTML(n) {
  const icon = NOTIF_STATUS_ICON[n.status] || 'ℹ️';
  const action = n.action ? `<div class="notif-actions">
      <button class="act-btn" data-notif-method="${esc(n.action.method)}" data-notif-path="${esc(n.action.path)}"
        data-notif-ask="${n.action.confirm ? '1' : ''}">${esc(n.action.label)}</button>
    </div>` : '';
  return `<div class="notif" data-notif="${esc(n.id)}">
    <div class="notif-head">
      <span class="notif-icon">${icon}</span>
      <span class="notif-world">${esc(n.world_label)}</span>
      <span class="notif-date">${esc(fmtDateTime(n.created_at))}</span>
    </div>
    <div class="notif-body">${esc(n.message)}</div>
    ${action}
  </div>`;
}

function renderNotifications(items) {
  const list = $('notif-list');
  if (!list) return;
  if (!items.length) { list.innerHTML = '<div class="ann-empty">通知はありません</div>'; return; }
  list.innerHTML = items.map(notifItemHTML).join('');
}

async function loadNotifications() {
  try {
    const d = await getJSON('/notifications');
    renderNotifications(d.notifications || []);
  } catch (_) {
    // 通知は補助表示。読み込み失敗（未ログイン等）はお知らせ本体の表示を妨げないよう静かに諦める。
  }
}

// 通知内の操作ボタン（再抽出を実行／更新する）。既存の資料フォルダ管理エンドポイントへ委譲するだけ
// （POST /worlds/{wid}/extract・POST /worlds/{wid}/refresh）——ここに新しい変異ロジックは作らない。
const notifList = $('notif-list');
if (notifList) {
  notifList.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-notif-method]');
    if (!btn) return;
    if (btn.dataset.notifAsk === '1'
        && !window.confirm('この操作を実行しますか？（AI 抽出のため時間・コストがかかることがあります）')) return;
    btn.disabled = true;
    try {
      await api(btn.dataset.notifMethod, btn.dataset.notifPath, {});
      toast('受け付けました');
      loadNotifications();
    } catch (err) {
      toast('失敗しました: ' + String(err));
      btn.disabled = false;
    }
  });
}

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
  _isAdmin = await checkAdmin();
  renderPostForm();
  await loadAnnouncements();
  await loadNotifications();
  _notifTimer = Sherpa.visibilityInterval(loadNotifications, NOTIF_POLL_MS);
})();
