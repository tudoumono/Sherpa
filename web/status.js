// システム状態画面（admin 専用）。GET /admin/health でコンポーネント別の健全性を取得。
// セキュリティ: server data（label/detail/hint）は全て esc()。
'use strict';

// UI-TABS2（2026-09-04）: システム管理のタブから iframe（?embed=1）で開かれた時は、自ページの
// 共通トップバー/ナビを隠す（CSS 側は .embedded 修飾・status.html の <style>）。単独 URL 直開き
// （?embed 無し）では何もしない＝この画面の機能・見た目は完全に不変。
if (new URLSearchParams(location.search).has('embed')) {
  document.documentElement.classList.add('embedded');
}

const $ = Sherpa.$, esc = Sherpa.esc, getJSON = Sherpa.getJSON;

// 非表示タブは Sherpa.visibilityInterval が自動で止める（性能台帳 QW4）ので、可視タブの
// 定常負荷を下げるためここは長めにしてよい。
const POLL_MS = 45000;

function toast(msg) {
  const t = $('toast'); if (!t) return;
  t.textContent = msg; t.classList.add('show');
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 1800);
}

// ===== admin ガード =====
// 戻り値は3値: 'admin'（アクセス可）／'denied'（非admin・未ログイン等）／
// 'unreachable'（5xx・ネットワーク失敗＝認証DB停止等でアクセス可否が判定できない）。
// getJSON（`common.js::api`）は非2xxでも妥当な JSON 本文を持つ場合に限り例外へ
// `err.status`/`err.body` を載せるが、本文が JSON として解析できない応答（ネットワーク
// 障害・不正な JSON 等）は status を持たない曖昧な失敗として扱う——401/403（denied）と
// それ以外の失敗（unreachable）を確実にステータスコードだけで判別したいここでは、
// 例外経由ではなく素の fetch で `r.status` を直接見る。
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

// 表示順: 失敗×down → 失敗×degraded → 失敗×none → 正常（止まっているものが常に最上部）。
function rank(c) {
  if (!c.ok) {
    if (c.impact === 'down') return 0;
    if (c.impact === 'degraded') return 1;
    return 2;
  }
  return 3;
}

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

function renderTable(components) {
  const tbody = $('health-tbody');
  if (!components.length) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="5">コンポーネント情報がありません</td></tr>';
    return;
  }
  const sorted = components.slice().sort((a, b) => rank(a) - rank(b));
  tbody.innerHTML = sorted.map((c) => {
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
  }).join('');
}

// バックエンドに到達できない場合（PostgreSQL/認証データベース停止等）のフォールバック表示。
function renderUnreachable() {
  const pill = $('status-pill');
  pill.className = 'status-pill danger';
  pill.textContent = '接続不可';
  $('checked-at').textContent = '最終チェック: —';
  const tbody = $('health-tbody');
  tbody.innerHTML = '<tr class="empty-row"><td colspan="5">'
    + 'バックエンドに到達できません（PostgreSQL/認証データベース停止の可能性）。'
    + 'ターミナルで make up を実行して復旧を確認してください。'
    + '</td></tr>';
}

// ===== 取得 =====
async function loadHealth(refresh) {
  try {
    const url = '/admin/health' + (refresh ? '?refresh=1' : '');
    const d = await getJSON(url);
    renderBanner(d);
    renderTable(d.components || []);
  } catch (e) {
    renderUnreachable();
    toast('システム状態の取得に失敗しました');
  }
}

// UI フィードバック4（2026-07-03）: 「再チェック」クリックで実際に何が起きているか分かるよう、
// ボタンをスピナー付きの「確認中...」表示にし、各行の状態セルにも「確認中…」を反映する
// （AI の実接続確認は数秒かかることがあるため、何も反応が無いように見えないようにする）。
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
