// つながりの確認画面（admin 専用）。POST /admin/connectivity/check で経路ごとの確認結果を取得して表にする。
// 設計: docs/design/operations.md「つながりの確認」
// セキュリティ: server data（label/host/reason/route）は全て esc()。
'use strict';

// システム管理のタブから iframe（?embed=1）で開かれた時は、共通トップバー/ナビを隠す（CSS は .embedded 修飾）。
if (new URLSearchParams(location.search).has('embed')) {
  document.documentElement.classList.add('embedded');
}

const $ = Sherpa.$, esc = Sherpa.esc;

function toast(msg) {
  const t = $('toast'); if (!t) return;
  t.textContent = msg; t.classList.add('show');
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 2400);
}

// 戻り値は3値: 'admin'／'denied'／'unreachable'（status.js と同じ判定）。
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

function stateCell(r) {
  if (r.status === 'ok') return { text: '● つながる', color: 'var(--ok)' };
  if (r.status === 'ng') return { text: '● つながらない', color: 'var(--danger)' };
  if (r.status === 'incomplete') return { text: '▲ 確かめきれなかった', color: 'var(--warn)' };
  return { text: '○ 確かめていない', color: 'var(--ink-3)' };
}

function renderPath(p) {
  const rows = (p.results || []).map((r) => {
    const st = stateCell(r);
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

let _busy = false;

async function runCheck(includePaid) {
  if (_busy) return;
  _busy = true;
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
    if (!r.ok) { toast('確認に失敗しました'); return; }
    const d = await r.json();
    $('paths').innerHTML = (d.paths || []).map(renderPath).join('');
    $('checked-at').textContent = '最終確認: ' + new Date().toLocaleString('ja-JP', { hour12: false });
  } catch (_) {
    toast('確認に失敗しました');
  } finally {
    free.textContent = origFree; paid.textContent = origPaid;
    free.disabled = false; paid.disabled = false;
    _busy = false;
  }
}

$('check-free-btn').addEventListener('click', () => runCheck(false));
$('check-paid-btn').addEventListener('click', () => {
  if (!confirm('OpenAI に短い文を送ります（埋め込み・回答の AI・Codex の分。数円程度）。続けますか？')) return;
  runCheck(true);
});

async function init() {
  const state = await checkAdmin();
  const main = $('main-content'), denied = $('access-denied');
  if (state === 'denied') {
    if (main) main.style.display = 'none';
    if (denied) denied.style.display = 'block';
  }
}

init();
