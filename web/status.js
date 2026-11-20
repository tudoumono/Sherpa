// システム状態画面（admin 専用）。GET /admin/health の軽い確認（保存先・資料の中身・AI）と、
// ボタンで流す POST /admin/connectivity/check の実際に送る確認（経路ごとの結果）を 1 画面に出す。
// 設計: docs/design/operations.md「システム状態とつながりの確認」
// セキュリティ: server data（label/detail/hint）は全て esc()。
'use strict';

// システム管理のタブから iframe（?embed=1）で開かれた時は、共通トップバー/ナビを隠す（CSS は .embedded 修飾）。
if (new URLSearchParams(location.search).has('embed')) {
  document.documentElement.classList.add('embedded');
}

const $ = Sherpa.$, esc = Sherpa.esc, getJSON = Sherpa.getJSON;

// 非表示タブは Sherpa.visibilityInterval が自動で止めるため、ここは長めでよい。
const POLL_MS = 45000;

function toast(msg) {
  const t = $('toast'); if (!t) return;
  t.textContent = msg; t.classList.add('show');
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 1800);
}

// ===== admin ガード =====
// 戻り値は3値: 'admin'（アクセス可）／'denied'（非admin・未ログイン等）／'unreachable'（5xx・ネットワーク失敗＝認証DB停止等でアクセス可否が判定できない）。
// 401/403（denied）とそれ以外の失敗（unreachable）をステータスコードだけで判別するため、getJSON ではなく素の fetch で r.status を直接見る。
async function checkAdmin() {
  let r;
  try {
    r = await fetch('/auth/me');
  } catch (_) {
    return 'unreachable';
  }
  if (r.status === 401 || r.status === 403) return 'denied';
  if (!r.ok) return 'unreachable';
  let u = null;
  try {
    u = await r.json();
  } catch (_) {
    return 'unreachable';
  }
  return u && u.role === 'admin' ? 'admin' : 'denied';
}

// ===== 表示ヘルパ =====
function fmtTime(iso) {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleString('ja-JP', { hour12: false });
  } catch (_) { return String(iso); }
}

function bannerClass(status) {
  if (status === 'ok') return 'ok';
  if (status === 'degraded') return 'warn';
  return 'danger';
}
function bannerLabel(status) {
  if (status === 'ok') return '正常';
  if (status === 'degraded') return '一部機能制限';
  return '停止';
}

// 並べ方: 保存先 → 資料の中身 → AI の順（まとまりの中はサーバーの順）。
const GROUPS = [
  { title: '保存先', ids: ['postgres', 'elasticsearch', 'neo4j'] },
  { title: '資料の中身', ids: ['es_search', 'graph_search'] },
  { title: 'AI', ids: ['answer_ai', 'codex', 'embed'] },
];

function stateCell(c) {
  if (c.ok) return { text: '● 正常', color: 'var(--ok)' };
  if (c.impact === 'down') return { text: '● 停止', color: 'var(--danger)' };
  if (c.impact === 'degraded') return { text: '● 停止（機能制限）', color: 'var(--warn)' };
  return { text: '○ 未設定/停止（参考）', color: 'var(--ink-3)' };
}

// ===== 描画 =====
function renderBanner(d) {
  const pill = $('status-pill');
  pill.className = 'status-pill ' + bannerClass(d.status);
  pill.textContent = bannerLabel(d.status);
  $('checked-at').textContent = '最終チェック: ' + fmtTime(d.checked_at);
}

function rowHtml(c) {
  const st = stateCell(c);
  const label = esc(c.label || c.id || '');
  const latency = c.latency_ms != null ? esc(String(c.latency_ms)) : '—';
  const detail = c.detail ? esc(c.detail) : '';
  const hint = c.hint ? esc(c.hint) : '';
  return `<tr>
      <td class="state-cell" style="color:${st.color}">${st.text}</td>
      <td>${label}</td>
      <td class="latency-cell">${latency}</td>
      <td class="detail-cell">${detail}</td>
      <td class="hint-cell">${hint}</td>
    </tr>`;
}

function renderTable(components) {
  const tbody = $('health-tbody');
  if (!components.length) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="5">コンポーネント情報がありません</td></tr>';
    return;
  }
  const known = new Set(GROUPS.flatMap((g) => g.ids));
  const groups = GROUPS.map((g) => ({
    title: g.title, rows: components.filter((c) => g.ids.includes(c.id)),
  }));
  const rest = components.filter((c) => !known.has(c.id));
  if (rest.length) groups.push({ title: 'その他', rows: rest });
  tbody.innerHTML = groups.filter((g) => g.rows.length).map((g) =>
    `<tr class="group-row"><td colspan="5">${esc(g.title)}</td></tr>` + g.rows.map(rowHtml).join('')).join('');
}

// バックエンドに到達できない場合（PostgreSQL/認証データベース停止等）のフォールバック表示。
function renderUnreachable(e) {
  const pill = $('status-pill');
  pill.className = 'status-pill danger';
  pill.textContent = '接続不可';
  $('checked-at').textContent = '最終チェック: —';
  const tbody = $('health-tbody');
  const reason = e && e.status && e.status !== 503
    ? `状態を取得できませんでした: ${esc(e.message)}`
    : 'バックエンドに到達できません（PostgreSQL/認証データベース停止の可能性）。'
      + 'ターミナルで make up を実行して復旧を確認してください。';
  tbody.innerHTML = `<tr class="empty-row"><td colspan="5">${reason}</td></tr>`;
}

// ===== 取得 =====
async function loadHealth(refresh) {
  try {
    const url = '/admin/health' + (refresh ? '?refresh=1' : '');
    const d = await getJSON(url);
    renderBanner(d);
    renderTable(d.components || []);
  } catch (e) {
    renderUnreachable(e);
    toast('システム状態の取得に失敗しました');
  }
}

// 「再チェック」クリックで、ボタンをスピナー付きの「確認中...」表示にし、各行の状態セルにも「確認中…」を反映する（AI の実接続確認は数秒かかることがあるため）。
function showChecking() {
  document.querySelectorAll('#health-tbody .state-cell').forEach((td) => {
    td.textContent = '確認中…'; td.style.color = 'var(--ink-3)';
  });
}
$('recheck-btn').addEventListener('click', () => {
  const btn = $('recheck-btn');
  const orig = btn.textContent;
  btn.disabled = true;
  btn.innerHTML = '<span class="loading-inline" role="status"><span class="spinner spinner-sm"></span><span>確認中...</span></span>';
  showChecking();
  loadHealth(true).finally(() => { btn.disabled = false; btn.textContent = orig; });
});

// ===== 実際に送って確かめる =====
function sendStateCell(r) {
  if (r.status === 'ok') return { text: '● つながる', color: 'var(--ok)' };
  if (r.status === 'ng') return { text: '● つながらない', color: 'var(--danger)' };
  if (r.status === 'incomplete') return { text: '▲ 確かめきれなかった', color: 'var(--warn)' };
  return { text: '○ 確かめていない', color: 'var(--ink-3)' };
}

function renderPath(p) {
  const rows = (p.results || []).map((r) => {
    const st = sendStateCell(r);
    const label = esc(r.label || r.id || '') + (r.paid ? ' <span class="hint">（有料）</span>' : '');
    const note = r.note ? ' <span class="hint">' + esc(r.note) + '</span>' : '';
    return `<tr>
      <td>${label}${note}</td>
      <td class="host-cell">${esc(r.host || '—')}</td>
      <td>${esc(r.route || '—')}</td>
      <td class="state-cell" style="color:${st.color}">${st.text}</td>
      <td class="time-cell">${r.status === 'skipped' ? '—' : esc(String(r.elapsed_ms)) + ' ms'}</td>
      <td class="reason-cell">${esc(r.reason || '')}</td>
    </tr>`;
  }).join('');
  return `<div class="card" style="overflow-x:auto">
    <div class="card-head"><div><div class="title">${esc(p.label || p.path)}</div></div></div>
    <table>
      <thead><tr><th>確認するもの</th><th>宛先</th><th>経路</th><th>結果</th><th>時間</th><th>理由</th></tr></thead>
      <tbody>${rows || '<tr class="empty-row"><td colspan="6">確認結果がありません</td></tr>'}</tbody>
    </table>
  </div>`;
}

let _sending = false;

// 前回の表を残さず、今回は確認できなかったことを出す。
function showSendFailed(reason) {
  $('paths').innerHTML = `<div class="hint">確認できませんでした: ${esc(String(reason))}</div>`;
  $('send-checked-at').textContent = '最終確認: 失敗（' + fmtTime(new Date().toISOString()) + '）';
  toast('確認に失敗しました');
}

async function runSendCheck(includePaid) {
  if (_sending) return;
  _sending = true;
  const free = $('check-free-btn'), paid = $('check-paid-btn');
  const origFree = free.textContent, origPaid = paid.textContent;
  free.disabled = true; paid.disabled = true;
  (includePaid ? paid : free).innerHTML =
    '<span class="loading-inline" role="status"><span class="spinner spinner-sm"></span><span>確認中...</span></span>';
  try {
    const r = await fetch('/admin/connectivity/check', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ include_paid: includePaid }),
    });
    if (r.status === 409) { toast('ほかの確認が実行中です。終わってからもう一度お試しください'); return; }
    if (!r.ok) {
      const body = await r.json().catch(() => null);
      showSendFailed((body && typeof body.detail === 'string' && body.detail) || `HTTP ${r.status}`);
      return;
    }
    const d = await r.json();
    $('paths').innerHTML = (d.paths || []).map(renderPath).join('');
    $('send-checked-at').textContent = '最終確認: ' + fmtTime(new Date().toISOString());
  } catch (e) {
    showSendFailed((e && e.message) || '通信に失敗しました');
  } finally {
    free.textContent = origFree; paid.textContent = origPaid;
    free.disabled = false; paid.disabled = false;
    _sending = false;
  }
}

$('check-free-btn').addEventListener('click', () => runSendCheck(false));
$('check-paid-btn').addEventListener('click', () => {
  if (!confirm('OpenAI に短い文を送ります（埋め込み・回答の AI・Codex の分。数円程度）。続けますか？')) return;
  runSendCheck(true);
});

// ===== 初期化 =====
// タイマーは1つだけ保持（多重登録防止）。状態が確定するまでは init() 自体を再実行して
// 回復（認証DB復旧）を検知し、admin 確定後は通常の loadHealth ポーリングに切り替える。
// 非表示タブでは Sherpa.visibilityInterval が自動で止め、可視化に戻った瞬間に1回即時実行する。
let _statusTimer = null;

function _scheduleNext(fn) {
  if (_statusTimer) _statusTimer.stop();
  _statusTimer = Sherpa.visibilityInterval(fn, POLL_MS);
}

async function init() {
  const state = await checkAdmin();
  const main = $('main-content');
  const denied = $('access-denied');
  if (state === 'admin') {
    if (main) main.style.display = '';
    if (denied) denied.style.display = 'none';
    await loadHealth(false);
    _scheduleNext(() => loadHealth(false));
    return;
  }
  if (state === 'denied') {
    if (main) main.style.display = 'none';
    if (denied) denied.style.display = 'block';
    if (_statusTimer) { _statusTimer.stop(); _statusTimer = null; }
    return;
  }
  // 'unreachable': アクセス可否が判定できない（認証DB停止等）。main-content は表示したまま
  // 「アクセス権限がありません」ではなく「接続不可」のフォールバックを出し、回復を待つ。
  if (main) main.style.display = '';
  if (denied) denied.style.display = 'none';
  renderUnreachable();
  _scheduleNext(init);
}

init();
