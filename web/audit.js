// 監査ログ閲覧画面（Slice3・admin 専用）。GET /admin/audit でフィルタ検索。
// セキュリティ: server data は全て esc()。detail/before_state/after_state は既にサーバ側で redact 済み。
'use strict';

// UI-TABS2（2026-09-04）: システム管理のタブから iframe（?embed=1）で開かれた時は、自ページの
// 共通トップバー/ナビを隠す（CSS 側は .embedded 修飾・audit.html の <style>）。単独 URL 直開き
// （?embed 無し）では何もしない＝この画面の機能・見た目は完全に不変。
if (new URLSearchParams(location.search).has('embed')) {
  document.documentElement.classList.add('embedded');
}

const $ = Sherpa.$, esc = Sherpa.esc, getJSON = Sherpa.getJSON, fmtDateTime = Sherpa.fmtDateTime;

const PAGE = 100;
let _offset = 0;
let _lastCount = 0;

function toast(msg) {
  const t = $('toast'); if (!t) return;
  t.textContent = msg; t.classList.add('show');
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 1800);
}

// ===== admin ガード =====
async function checkAdmin() {
  try {
    const u = await getJSON('/auth/me');
    if (u && u.role === 'admin') return true;
  } catch (_) { /* compat */ }
  return false;
}

// ===== 検索クエリ組み立て =====
function buildFilterParams() {
  const p = new URLSearchParams();
  const actor = ($('f-actor').value || '').trim();
  const action = ($('f-action').value || '').trim();
  const outcome = $('f-outcome').value;
  const severity = $('f-severity').value;
  const from = $('f-from').value;
  const to = $('f-to').value;

  if (actor) p.set('actor', actor);
  if (action) p.set('action', action);
  if (outcome) p.set('outcome', outcome);
  if (severity) p.set('severity', severity);
  // datetime-local → ISO 8601 に変換（秒まで）
  if (from) p.set('time_from', from.replace('T', 'T') + ':00');
  if (to) p.set('time_to', to.replace('T', 'T') + ':59');
  return p;
}

function buildParams(offset) {
  const p = buildFilterParams();
  p.set('limit', String(PAGE));
  p.set('offset', String(offset));
  return p.toString();
}

// ===== テーブル描画 =====
function outcomeClass(o) {
  if (o === 'success') return 'oc-success';
  if (o === 'deny') return 'oc-deny';
  if (o === 'pending') return 'oc-pending';
  return 'oc-error';   // failure・error・その他未知の値はまとめてエラー色。
}
function sevClass(s) {
  if (s === 'critical') return 'sev-critical';
  if (s === 'warning') return 'sev-warning';
  return '';
}
// 表示ラベルのみ日本語化（value・API 送信値・監査ログの生データは英語のまま）。
// pending: 送信前に確保する監査行（例: admin.usage_chat_asked の外部送信 fail-closed 契約）。
// 対応する結果行（success/failure）とは request_id で対応付く（行クリックで開く詳細を参照）。
const OUTCOME_LABEL = { success: '成功', deny: '拒否', failure: '失敗', error: 'エラー', pending: '送信前記録' };
const SEVERITY_LABEL = { info: '情報', warning: '警告', critical: '重大' };

function fmtDetail(v) {
  if (v == null) return '';
  try {
    const s = typeof v === 'string' ? v : JSON.stringify(v, null, 0);
    // 長すぎる場合は省略（UI表示のみ・値は既にサーバ側 redact 済み）。
    return s.length > 200 ? s.slice(0, 200) + '…' : s;
  } catch (_) { return String(v); }
}

let _rowsData = [];   // 行クリック展開（詳細の完全表示）用に生データを保持

function renderRows(rows) {
  const tbody = $('audit-tbody');
  _rowsData = rows;
  if (!rows.length) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="8">該当する監査ログはありません</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map((r, i) => {
    const ts = esc(fmtDateTime(r.created_at, { seconds: true }));
    const actor = esc(r.actor_user_id || '—');
    const action = esc(r.action || '');
    const res = esc((r.resource_type || '') + (r.resource_id ? ':' + r.resource_id : ''));
    const oc = esc(OUTCOME_LABEL[r.outcome] || r.outcome || '');
    const sev = esc(SEVERITY_LABEL[r.severity] || r.severity || '');
    const reason = esc(r.reason || '');
    const detail = esc(fmtDetail(r.detail));
    return `<tr class="audit-row" data-idx="${i}">
      <td class="ts-cell col-ts">${ts}</td>
      <td class="col-actor" style="font-family:var(--font-code,monospace);font-size:11.5px">${actor}</td>
      <td class="action-cell col-action">${action}</td>
      <td class="col-resource" style="font-size:11px;color:var(--ink-3);font-family:var(--font-code,monospace)">${res}</td>
      <td class="col-outcome ${outcomeClass(r.outcome)}"${r.request_id ? ` title="request_id: ${esc(r.request_id)}"` : ''}>${oc}</td>
      <td class="col-severity ${sevClass(r.severity)}">${sev}</td>
      <td class="col-reason" style="font-size:11.5px;color:var(--ink-3)">${reason}</td>
      <td class="detail-cell col-detail"><button type="button" class="detail-toggle" data-detail-toggle>▸</button> ${detail}</td>
    </tr>`;
  }).join('');
}

// ===== 表示項目カスタマイズ（UI フィードバック6・2026-07-03）=====
// 端末ローカル保存。テーマ切替と同じ思想: localStorage は try/catch で包む
// （プライベートモード等でアクセス不可でも既定表示に落ちるだけにする）。
const AUDIT_COLUMNS = ['ts', 'actor', 'action', 'resource', 'outcome', 'severity', 'reason', 'detail'];
const AUDIT_COL_KEY = 'sherpa_audit_columns';
const AUDIT_COL_DEFAULT = { ts: true, actor: true, action: true, resource: true, outcome: true, severity: true, reason: true, detail: false };

function loadColumnPrefs() {
  try {
    const raw = localStorage.getItem(AUDIT_COL_KEY);
    if (!raw) return { ...AUDIT_COL_DEFAULT };
    const parsed = JSON.parse(raw);
    return { ...AUDIT_COL_DEFAULT, ...parsed };
  } catch (_) { return { ...AUDIT_COL_DEFAULT }; }
}
function saveColumnPrefs(prefs) {
  try { localStorage.setItem(AUDIT_COL_KEY, JSON.stringify(prefs)); } catch (_) { /* no-op */ }
}
function applyColumnPrefs(prefs) {
  const table = $('audit-table');
  if (table) table.dataset.hide = AUDIT_COLUMNS.filter((id) => !prefs[id]).join(' ');
  document.querySelectorAll('#colcfg input[data-col]').forEach((cb) => {
    cb.checked = !!prefs[cb.dataset.col];
  });
}

let _colPrefs = loadColumnPrefs();
applyColumnPrefs(_colPrefs);
document.querySelectorAll('#colcfg input[data-col]').forEach((cb) => {
  cb.addEventListener('change', () => {
    _colPrefs = { ..._colPrefs, [cb.dataset.col]: cb.checked };
    saveColumnPrefs(_colPrefs);
    applyColumnPrefs(_colPrefs);
  });
});

// ===== 詳細の行クリック展開（既定非表示の「詳細」列を、列に関わらず個別に開ける）=====
function toggleDetailRow(tr) {
  const next = tr.nextElementSibling;
  if (next && next.classList.contains('detail-row')) {
    next.remove();
    return;
  }
  document.querySelectorAll('#audit-tbody .detail-row').forEach((el) => el.remove());
  const idx = Number(tr.dataset.idx);
  const row = _rowsData[idx];
  const full = row && row.detail != null
    ? (typeof row.detail === 'string' ? row.detail : JSON.stringify(row.detail, null, 2))
    : '（詳細なし）';
  // request_id: pending 行と結果行（success/failure）を対応付ける相関 ID（例: admin.usage_chat_asked
  // の外部送信 fail-closed 契約・audit.js::OUTCOME_LABEL 参照）。detail の前に独立した行として示す。
  const reqLine = row && row.request_id ? `request_id: ${row.request_id}\n\n` : '';
  const detailTr = document.createElement('tr');
  detailTr.className = 'detail-row';
  const td = document.createElement('td');
  td.colSpan = AUDIT_COLUMNS.length;
  td.textContent = reqLine + full;   // textContent で描画＝エスケープ不要（XSS 安全）
  detailTr.appendChild(td);
  tr.after(detailTr);
}
$('audit-tbody').addEventListener('click', (e) => {
  const tr = e.target.closest('tr.audit-row');
  if (!tr) return;
  toggleDetailRow(tr);
});

function updatePager(count, offset) {
  const prev = $('prev-btn');
  const next = $('next-btn');
  const info = $('pager-info');
  const rc = $('result-count');
  prev.disabled = offset === 0;
  next.disabled = count < PAGE;
  info.textContent = count > 0 ? `${offset + 1}–${offset + count} 件` : '';
  rc.textContent = count > 0 ? `（${count} 行取得）` : '';
}

// ===== 検索実行 =====
async function search(offset) {
  _offset = offset;
  $('audit-tbody').setAttribute('aria-busy', 'true');
  $('audit-tbody').innerHTML = '<tr><td colspan="8"><div class="loading" role="status" style="padding:12px"><span class="spinner spinner-sm"></span><span>監査ログを読み込んでいます...</span></div></td></tr>';
  try {
    const d = await getJSON('/admin/audit?' + buildParams(offset));
    _lastCount = (d.rows || []).length;
    renderRows(d.rows || []);
    updatePager(_lastCount, offset);
  } catch (e) {
    $('audit-tbody').innerHTML = `<tr><td colspan="8" style="color:var(--danger);padding:16px">${esc(String(e))}</td></tr>`;
    toast('検索に失敗しました');
  } finally {
    $('audit-tbody').setAttribute('aria-busy', 'false');
  }
}

async function exportAudit(format) {
  const status = $('export-status');
  const buttons = [$('export-csv'), $('export-jsonl')].filter(Boolean);
  buttons.forEach((b) => { b.disabled = true; });
  if (status) {
    status.setAttribute('aria-busy', 'true');
    status.innerHTML = '<span class="loading-inline"><span class="spinner spinner-sm"></span><span>エクスポート中...</span></span>';
  }
  try {
    const p = buildFilterParams();
    p.set('format', format);
    const r = await fetch('/admin/audit/export?' + p.toString());
    if (!r.ok) {
      let data = null;
      try { data = await r.json(); } catch (_) { data = null; }
      throw new Error((data && data.detail) || `エラー (${r.status})`);
    }
    const blob = await r.blob();
    const cd = r.headers.get('content-disposition') || '';
    const m = cd.match(/filename="([^"]+)"/);
    const name = m ? m[1] : `sherpa-audit.${format}`;
    Sherpa.downloadBlob(blob, name);   // UI フィードバック3: revoke タイミング問題を共通ヘルパで回避
    if (status) status.textContent = `エクスポートしました: ${name}`;
  } catch (e) {
    if (status) status.textContent = `エクスポート失敗: ${e.message}`;
    toast('エクスポートに失敗しました');
  } finally {
    buttons.forEach((b) => { b.disabled = false; });
    if (status) status.setAttribute('aria-busy', 'false');
  }
}

// イベント
$('search-btn').addEventListener('click', () => search(0));
$('prev-btn').addEventListener('click', () => { if (_offset >= PAGE) search(_offset - PAGE); });
$('next-btn').addEventListener('click', () => { if (_lastCount >= PAGE) search(_offset + PAGE); });
$('export-csv').addEventListener('click', () => exportAudit('csv'));
$('export-jsonl').addEventListener('click', () => exportAudit('jsonl'));
// フィルターで Enter → 検索
['f-actor', 'f-action'].forEach((id) => {
  const el = $(id); if (el) el.addEventListener('keydown', (e) => { if (e.key === 'Enter') search(0); });
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
  const isAdmin = await checkAdmin();
  if (!isAdmin) {
    const main = $('main-content');
    const denied = $('access-denied');
    if (main) main.style.display = 'none';
    if (denied) denied.style.display = 'block';
    return;
  }
  // ページロード時は最新100件を取得。
  search(0);
})();
