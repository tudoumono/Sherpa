// 利用統計画面（admin 専用）。GET /admin/usage/stats?days=（または from/to）でユーザー別/全体の利用量を集計表示する。
// 設計: docs/design/usage.md「管理画面が読む `GET /admin/usage/stats`」
// 狙い＝よく使うユーザーを見つけてヒアリング候補にする（本文・タイトルは API 側で一切返さない）。
// セキュリティ: server data は全て esc()。data-* 委譲でインライン handler なし。
'use strict';

// システム管理のタブから iframe（?embed=1）で開かれた時は、共通トップバー/ナビを隠す（CSS は .embedded 修飾）。
if (new URLSearchParams(location.search).has('embed')) {
  document.documentElement.classList.add('embedded');
}

const $ = Sherpa.$, esc = Sherpa.esc, getJSON = Sherpa.getJSON, mdLite = Sherpa.mdLite, api = Sherpa.api;

const LENS_LABEL = { investigate: '調べる', author: '作る', impact: '影響分析', qa: '仕様問い合わせ', troubleshoot: 'トラブルシュート', chat: '素の会話' };
const LENS_ORDER = ['investigate', 'author', 'impact', 'qa', 'troubleshoot', 'chat'];

// 頭脳別利用比率の表示名（brainmenu/chat.js の PROVIDERS と同じラベルに揃える）。
// 専用のカテゴリランプが無いため、既存の semantic token（固定順）をカテゴリ色に転用し、常設の凡例＋直接ラベルで色だけに頼らない。
const PROVIDER_LABEL = {
  heuristic: '簡易（AIなし）', codex: 'Codex', openai: 'OpenAI API', gemini: 'Gemini',
  ollama: 'ローカルLLM (Ollama)', bedrock: 'AWS Bedrock (Claude)', unknown: '不明',
};
const PROVIDER_ORDER = ['heuristic', 'codex', 'openai', 'gemini', 'bedrock', 'ollama', 'unknown'];
const PROVIDER_COLOR = {
  heuristic: 'var(--ink-3)', codex: 'var(--accent)', openai: 'var(--ok)', gemini: 'var(--warn)',
  bedrock: 'var(--danger)', ollama: 'var(--accent-ink)', unknown: 'var(--border)',
};

// ターンの終了理由（`sherpa/stop_kind.py` の閉じた8値＋'unknown'）の平文ラベル。未知の値は生の値をそのまま表示する（KIND_LABEL と同じ流儀）。
const STOP_KIND_LABEL = {
  completed: '完了', stopped_by_user: '利用者が停止', budget: '調査の上限', no_evidence: '根拠不足',
  transport_error: '通信エラー', timeout: 'タイムアウト', codex_silent: 'Codex 無応答',
  codex_partial: 'Codex 途中終了', unknown: '不明',
};
const STOP_KIND_ORDER = ['completed', 'stopped_by_user', 'budget', 'no_evidence',
  'transport_error', 'timeout', 'codex_silent', 'codex_partial', 'unknown'];
function stopKindLabel(k) { return STOP_KIND_LABEL[k] || k; }

let _stats = null;
let _loadedAt = null;
const USAGE_TABS = ['overview', 'users', 'quality', 'tokens'];
// 期間は {days}（7/30/90）か {start, end}（JST 暦日・両日を含む）。URL の # に同じ形で持つ。
const PERIOD_MAX_DAYS = 365;   // API の from/to の上限と同じ
function dateSerial(s) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(s || '')) return null;
  const [y, m, d] = s.split('-').map(Number);
  const t = Date.UTC(y, m - 1, d);
  return new Date(t).toISOString().slice(0, 10) === s ? t / 86400000 : null;
}
function nextDate(s) {
  const [y, m, d] = s.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, d + 1)).toISOString().slice(0, 10);
}
function todayJst() {
  return new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Tokyo' }).format(new Date());
}
function rangeError(start, end) {
  const a = dateSerial(start), b = dateSerial(end);
  if (a === null || b === null) return '開始日と終了日を入れてください';
  if (a > b) return '開始日は終了日以前にしてください';
  if (b > dateSerial(todayJst())) return '終了日は今日以前にしてください';
  if (b - a + 1 > PERIOD_MAX_DAYS) return `期間は最大${PERIOD_MAX_DAYS}日です`;
  return '';
}
// p が null＝URL の期間指定が不正で未取得（タブ移動などは不正な指定をそのまま引き継ぐ）。
let _invalidPeriodQuery = 'days=30';
function periodQuery(p) { return !p ? _invalidPeriodQuery : p.start ? `start=${p.start}&end=${p.end}` : `days=${p.days}`; }
// API へ渡す期間クエリ（URL の # とは別の組み立て＝開始日は JST の日初から、終了日の翌日の日初まで・API は半開区間 [from, to)）。
// 集計取得（load）と明細ZIP保存の両方がこれを使う（表示中の期間と食い違う range を送らない）。
function periodApiQuery(period) {
  return period.start
    ? `from=${encodeURIComponent(`${period.start}T00:00:00+09:00`)}&to=${encodeURIComponent(`${nextDate(period.end)}T00:00:00+09:00`)}`
    : `days=${encodeURIComponent(period.days)}`;
}
function periodText(p) { return p.start ? `${p.start} ～ ${p.end}` : `${p.days}日間`; }
function usageView() {
  const [tab, query] = location.hash.slice(1).split('?');
  const q = new URLSearchParams(query);
  const days = Number(q.get('days'));
  const start = q.get('start'), end = q.get('end');
  const hasRange = q.has('start') || q.has('end');
  const periodError = hasRange ? rangeError(start, end) : '';
  const period = hasRange && !periodError ? { start, end } : { days: [7, 30, 90].includes(days) ? days : 30 };
  return { tab: USAGE_TABS.includes(tab) ? tab : 'overview', period, periodError, start, end };
}
let _period = usageView().periodError ? null : usageView().period;
function showUsageTab(tab) {
  const standalone = $('usage-standalone');
  standalone.hidden = !new URLSearchParams(location.search).has('embed');
  standalone.href = `usage.html#${tab}?${periodQuery(_period)}`;
  document.querySelectorAll('[data-usage-panel]').forEach((el) => { el.hidden = el.dataset.usagePanel !== tab; });
  document.querySelectorAll('[data-usage-tab]').forEach((el) => {
    const selected = el.dataset.usageTab === tab;
    el.setAttribute('aria-selected', String(selected));
    el.tabIndex = selected ? 0 : -1;
  });
  document.querySelectorAll('[data-usage-link]').forEach((el) => {
    el.href = `#${el.dataset.usageLink}?${periodQuery(_period)}`;
  });
}
function navigateUsage(tab, period = _period) {
  location.hash = `${tab}?${periodQuery(period)}`;
}
function selectPeriod(period) {
  if (_period && periodQuery(period) === periodQuery(_period)) load(period);
  else navigateUsage(usageView().tab, period);
}
// URL の期間指定が不正なら取得せず、理由を出す（別の期間を選ぶと取得する）。
function showPeriodError(view) {
  _loadSeq++;   // 取得中の応答は捨てる
  _period = null;
  _invalidPeriodQuery = new URLSearchParams({ start: view.start || '', end: view.end || '' }).toString();
  _stats = null;
  $('usage-export').disabled = true;
  $('usage-export-detail').disabled = true;
  $('usage-stat-panels').hidden = true;
  document.querySelectorAll('.period-bar .filterchip').forEach((b) => {
    b.classList.remove('on');
    b.setAttribute('aria-pressed', 'false');
  });
  $('period-start').value = view.start || '';
  $('period-end').value = view.end || '';
  $('period-range-error').textContent = view.periodError;
  $('usage-period-label').textContent = '期間の指定に誤りがあります';
  $('usage-load-status').textContent = `${view.periodError}。期間を選び直してください。`;
  showUsageTab(view.tab);
}
document.querySelectorAll('[data-usage-tab]').forEach((btn) => {
  btn.addEventListener('click', () => navigateUsage(btn.dataset.usageTab));
  btn.addEventListener('keydown', (event) => {
    const index = USAGE_TABS.indexOf(btn.dataset.usageTab);
    let next;
    if (event.key === 'ArrowRight') next = (index + 1) % USAGE_TABS.length;
    else if (event.key === 'ArrowLeft') next = (index + USAGE_TABS.length - 1) % USAGE_TABS.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = USAGE_TABS.length - 1;
    else return;
    event.preventDefault();
    document.querySelector(`[data-usage-tab="${USAGE_TABS[next]}"]`).focus();
    navigateUsage(USAGE_TABS[next]);
  });
});
let _adminReady = false;
window.addEventListener('hashchange', () => {
  const view = usageView();
  if (_adminReady && view.periodError) showPeriodError(view);
  else if (_adminReady && (!_period || periodQuery(view.period) !== periodQuery(_period))) load(view.period);
  showUsageTab(view.tab);
});
showUsageTab(usageView().tab);

let _sortKey = 'turns';
let _sortDir = 'desc';
let _users = [];
let _loadSeq = 0;   // 期間ボタン連打対策: 後着の古いレスポンスで表示が巻き戻らないようにする

function toast(msg) {
  const t = $('toast'); if (!t) return;
  t.textContent = msg; t.classList.add('show');
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 1800);
}

// ===== admin ガード =====
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

function fmtDate(iso) {
  if (!iso) return '—';
  return String(iso).slice(0, 16).replace('T', ' ');
}

