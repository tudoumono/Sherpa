// 回答への評価の画面（admin 専用）。GET /admin/feedback/summary・/admin/feedback/items で集計と一覧を出す。
// 設計: docs/design/usage.md「回答への評価（管理者の画面）」
// セキュリティ: 一言・質問などサーバの文字列は全て esc() を通す（HTML として解釈しない）。インライン handler なし。
'use strict';

// システム管理のタブから iframe（?embed=1）で開かれた時は、共通トップバー/ナビを隠す（CSS は .embedded 修飾）。
if (new URLSearchParams(location.search).has('embed')) {
  document.documentElement.classList.add('embedded');
}

const $ = Sherpa.$, esc = Sherpa.esc, getJSON = Sherpa.getJSON;

// 表示名は chat/render.js の feedbackHTML・usage.js と同じ言葉に揃える。
const TAG_LABEL = { wrong_evidence: '根拠が違う', incomplete: '足りない', outdated: '古い版', slow: '遅い' };
const MODE_LABEL = {
  investigate: '調べる', author: '作る', impact: '影響分析', qa: '仕様問い合わせ',
  troubleshoot: 'トラブルシュート', chat: '素の会話', unknown: '不明',
};
const PROVIDER_LABEL = {
  heuristic: '簡易（AIなし）', codex: 'Codex', openai: 'OpenAI API', gemini: 'Gemini',
  ollama: 'ローカルLLM (Ollama)', bedrock: 'AWS Bedrock (Claude)', unknown: '不明',
};
const COMPLETION_LABEL = { complete: '完了', partial: '途中', stopped: '停止', failed: '失敗' };
const label = (map, k) => (k == null ? '—' : (map[k] || k));

let _days = 30;
let _nextBefore = null;
let _seq = 0;   // 期間・絞り込みの連打で、後着の古い応答が表示を巻き戻さないようにする

function fmtDate(iso) { return iso ? String(iso).slice(0, 16).replace('T', ' ') : '—'; }
function fmtMs(ms) { return ms == null ? '—' : (ms >= 1000 ? `${(ms / 1000).toFixed(1)} 秒` : `${ms} ms`); }

let _adminErr = '';
// 戻り値は 'admin'／'denied'（401・403・管理者でない）／'unreachable'（判定できなかった。理由は _adminErr）。
async function checkAdmin() {
  try {
    const u = await getJSON('/auth/me');
    return u && u.role === 'admin' ? 'admin' : 'denied';
  } catch (e) {
    if (e && (e.status === 401 || e.status === 403)) return 'denied';
    _adminErr = (e && e.message) || '';
    return 'unreachable';
  }
}

function showUnreachable(denied) {
  const d = denied && denied.querySelectorAll('div');
  if (!d || d.length < 3) return;
  d[1].textContent = '権限を確認できませんでした: ' + _adminErr;
  d[2].textContent = '時間をおいて画面を開き直してください。';
}

function pairRows(rows, keyName, map) {
  if (!rows.length) return '<div class="hint">まだ評価がありません</div>';
  return '<table><tbody>' + rows.map((r) => `<tr><td>${esc(label(map, r[keyName]))}</td>`
    + `<td class="num up">👍 ${r.up}</td><td class="num down">👎 ${r.down}</td></tr>`).join('') + '</tbody></table>';
}

function renderSummary(s) {
  $('t-rated').textContent = s.rated;
  $('t-up').textContent = s.up;
  $('t-down').textContent = s.down;
  const note = $('trunc-note');
  note.hidden = !s.truncated;
  note.textContent = s.truncated ? `件数が多いため最新の ${s.max_rows} 件だけで集計しています。` : '';
  $('t-rate').textContent = s.rated ? `${Math.round(s.down / s.rated * 100)}%` : '—';
  $('tags-box').innerHTML = s.tags.length
    ? '<table><tbody>' + s.tags.map((t) => `<tr><td>${esc(label(TAG_LABEL, t.tag))}</td>`
      + `<td class="num">${t.count} 件</td><td class="num down">うち 👎 ${t.down}</td></tr>`).join('') + '</tbody></table>'
    : '<div class="hint">タグの付いた評価はまだありません</div>';
  $('mode-box').innerHTML = pairRows(s.by_mode, 'mode', MODE_LABEL);
  $('provider-box').innerHTML = pairRows(s.by_provider, 'provider', PROVIDER_LABEL);
  const max = Math.max(1, ...s.daily.map((d) => d.up + d.down));
  $('daily-box').innerHTML = s.daily.length
    ? '<div class="dailybars">' + s.daily.map((d) => `<div class="day" title="${esc(d.date)}　👍 ${d.up}　👎 ${d.down}">`
      + `<div class="seg-down" style="height:${d.down / max * 100}%"></div>`
      + `<div class="seg-up" style="height:${d.up / max * 100}%"></div></div>`).join('') + '</div>'
      + `<div class="hint">${esc(s.daily[0].date)} 〜 ${esc(s.daily[s.daily.length - 1].date)}</div>`
    : '<div class="hint">まだ評価がありません</div>';
}

function itemRow(it) {
  const user = it.user.display_name ? `${it.user.display_name}（${it.user.uid}）` : it.user.uid;
  const tags = it.tags.map((t) => `<span class="tagchip">${esc(label(TAG_LABEL, t))}</span>`).join('');
  return `<tr><td class="nw">${esc(fmtDate(it.created_at))}</td><td>${esc(user)}</td>`
    + `<td class="nw ${it.rating === 'up' ? 'up' : 'down'}">${it.rating === 'up' ? '👍' : '👎'}</td>`
    + `<td>${tags}</td><td>${esc(it.comment || '')}</td><td>${esc(it.question_head || '')}</td>`
    + `<td class="nw">${esc(label(MODE_LABEL, it.mode))}</td><td class="nw">${esc(label(PROVIDER_LABEL, it.provider))}</td>`
    + `<td class="nw">${esc(label(COMPLETION_LABEL, it.completion))}</td><td class="num nw">${esc(fmtMs(it.duration_ms))}</td></tr>`;
}

function itemsQuery(before) {
  const q = new URLSearchParams({ days: String(_days), rating: $('f-rating').value, limit: '50' });
  if ($('f-tag').value) q.set('tag', $('f-tag').value);
  if (before != null) q.set('before', String(before));
  return q.toString();
}

async function loadItems(append) {
  const seq = _seq;
  const d = await getJSON('/admin/feedback/items?' + itemsQuery(append ? _nextBefore : null));
  if (seq !== _seq) return;
  const tbody = $('items-tbody');
  if (!append) tbody.innerHTML = '';
  if (!append && !d.items.length) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="10">この条件の評価はありません</td></tr>';
  } else {
    tbody.insertAdjacentHTML('beforeend', d.items.map(itemRow).join(''));
  }
  _nextBefore = d.next_before;
  $('more-btn').hidden = _nextBefore == null;
}

async function load() {
  const seq = ++_seq;
  $('load-status').textContent = '読み込んでいます…';
  document.querySelectorAll('.period-bar .filterchip').forEach((b) => {
    const on = Number(b.dataset.days) === _days;
    b.classList.toggle('on', on);
    b.setAttribute('aria-pressed', String(on));
  });
  $('export-csv').href = `/admin/improvement-log/export?days=${_days}&format=csv`;
  $('export-jsonl').href = `/admin/improvement-log/export?days=${_days}&format=jsonl`;
  try {
    const s = await getJSON(`/admin/feedback/summary?days=${_days}`);
    if (seq !== _seq) return;
    renderSummary(s);
    await loadItems(false);
    if (seq === _seq) $('load-status').textContent = `直近 ${_days} 日間の評価です。`;
  } catch (e) {
    if (seq === _seq) $('load-status').textContent = `読み込めませんでした: ${e.message}`;
  }
}

document.querySelectorAll('.period-bar .filterchip').forEach((b) => b.addEventListener('click', () => {
  _days = Number(b.dataset.days);
  load();
}));
['f-rating', 'f-tag'].forEach((id) => $(id).addEventListener('change', () => {
  _seq++;
  loadItems(false).catch((e) => { $('load-status').textContent = `読み込めませんでした: ${e.message}`; });
}));
$('more-btn').addEventListener('click', () => {
  loadItems(true).catch((e) => { $('load-status').textContent = `読み込めませんでした: ${e.message}`; });
});

(async () => {
  const adminState = await checkAdmin();
  if (adminState !== 'admin') {
    const main = $('main-content'), denied = $('access-denied');
    if (main) main.style.display = 'none';
    if (denied) denied.style.display = 'block';
    if (adminState === 'unreachable') showUnreachable(denied);
    return;
  }
  await load();
})();
