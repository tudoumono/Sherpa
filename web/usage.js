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
async function checkAdmin() {
  try {
    const u = await getJSON('/auth/me');
    if (u && u.role === 'admin') return true;
  } catch (_) { /* compat */ }
  return false;
}

function fmtDate(iso) {
  if (!iso) return '—';
  return String(iso).slice(0, 16).replace('T', ' ');
}

// ===== 描画 =====
function renderSummary(totals) {
  $('t-active').textContent = fmtTokOrDash(totals.active_users);
  $('t-turns').textContent = fmtTokOrDash(totals.turns);
  $('t-conversations').textContent = fmtTokOrDash(totals.conversations);
}

function lensBarHTML(lens) {
  const total = LENS_ORDER.reduce((s, k) => s + (lens[k] || 0), 0);
  const bar = LENS_ORDER.map((k) => {
    const n = lens[k] || 0;
    if (!n) return '';
    const pct = total > 0 ? (n / total * 100) : 0;
    return `<span class="seg seg-${k}" style="flex:${pct} 0 auto" title="${esc(LENS_LABEL[k])}: ${n}件"></span>`;
  }).join('');
  const legend = LENS_ORDER.map((k) => (
    `<span class="item"><span class="dot seg-${k}"></span>${esc(LENS_LABEL[k])} ${lens[k] || 0}件</span>`
  )).join('');
  return `<div class="lensbar">${bar || ''}</div><div class="lenslegend">${legend}</div>`;
}

// ===== トレンドグラフ（日別アクティブユーザー数・日別ターン数） =====
// インライン SVG を素の JS で描画（新規チャートライブラリは追加しない）。
// 単一系列の時系列＝line（面は薄いウォッシュ）／凡例なし（タイトルが系列を示す）／クロスヘア＋ツールチップ＝ホバーとキーボードフォーカス両対応／軸・グリッドは控えめ／0日・全ゼロは明示的な空状態表示。

const _SVG_NS = 'http://www.w3.org/2000/svg';

function _svgEl(tag, attrs) {
  const el = document.createElementNS(_SVG_NS, tag);
  Object.keys(attrs).forEach((k) => el.setAttribute(k, attrs[k]));
  return el;
}

// 「きりのいい」上限値（0 / 1 / 2 / 5 / 10 の桁違い）に丸める（Y軸を素直な数字にする）。
function _niceMax(n) {
  if (!(n > 0)) return 1;
  const mag = Math.pow(10, Math.floor(Math.log10(n)));
  const norm = n / mag;
  let nice;
  if (norm <= 1) nice = 1;
  else if (norm <= 2) nice = 2;
  else if (norm <= 5) nice = 5;
  else nice = 10;
  return nice * mag;
}

// period.start〜period.end（サーバ算出の JST 暦日範囲）で連続した日付配列へ穴埋めする（API は活動があった日しか返さないため、穴埋めしないと空白日が圧縮されて時系列が歪む）。
// 「今日」をクライアント側で再計算しない（サーバの集計境界とズレると表とグラフの合計が食い違う）。
function fillDailySeries(daily, periodStart, periodEnd) {
  const byDate = new Map((daily || []).map((d) => [d.date, d]));
  const out = [];
  if (!periodStart || !periodEnd) return out;
  // 日付文字列("YYYY-MM-DD")を UTC 正午として扱う（ローカル tz による日付ズレを避ける）。
  let cur = new Date(`${periodStart}T12:00:00Z`);
  const end = new Date(`${periodEnd}T12:00:00Z`);
  while (cur <= end) {
    const dateStr = cur.toISOString().slice(0, 10);
    const row = byDate.get(dateStr);
    out.push({ date: dateStr, turns: row ? (row.turns || 0) : 0,
              active_users: row ? (row.active_users || 0) : 0 });
    cur = new Date(cur.getTime() + 86400000);
  }
  return out;
}

function renderTrendChart(svgEl, emptyEl, tipEl, wrapEl, points, opt) {
  while (svgEl.firstChild) svgEl.removeChild(svgEl.firstChild);
  const total = points.reduce((s, p) => s + (p.value || 0), 0);
  if (!points.length || total === 0) {
    svgEl.style.display = 'none';
    emptyEl.hidden = false;
    tipEl.hidden = true;
    return;
  }
  svgEl.style.display = 'block';
  emptyEl.hidden = true;

  const W = 600, H = 160;
  const padL = 30, padR = 8, padT = 10, padB = 20;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  svgEl.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svgEl.setAttribute('preserveAspectRatio', 'none');
  svgEl.setAttribute('role', 'img');
  const values = points.map((p) => p.value);
  svgEl.setAttribute('aria-label',
    `${opt.label}の推移。${points[0].date}から${points[points.length - 1].date}まで、`
    + `最小${Math.min(...values)}${opt.unit}・最大${Math.max(...values)}${opt.unit}。`);

  const maxV = _niceMax(Math.max(...values, 1));
  const n = points.length;
  const xAt = (i) => (n === 1 ? padL + plotW / 2 : padL + (plotW * i) / (n - 1));
  const yAt = (v) => padT + plotH - (plotH * v) / maxV;

  // 横グリッド線（0・中間・最大の3本のみ）＋ Y軸ラベル。
  [0, 0.5, 1].forEach((frac) => {
    const y = padT + plotH * (1 - frac);
    svgEl.appendChild(_svgEl('line', {
      x1: padL, x2: W - padR, y1: y, y2: y, stroke: 'var(--border)', 'stroke-width': 1,
    }));
    const t = _svgEl('text', {
      x: padL - 6, y: y + 3, 'text-anchor': 'end', 'font-size': 9, fill: 'var(--ink-3)',
    });
    t.textContent = String(Math.round(maxV * frac));
    svgEl.appendChild(t);
  });

  // エリア塗り（系列色の薄いウォッシュ）＋ライン（2px・角丸）。単一系列＝凡例は無し（タイトルが示す）。
  const linePts = points.map((p, i) => `${xAt(i)},${yAt(p.value)}`).join(' L ');
  svgEl.appendChild(_svgEl('path', {
    d: `M ${padL},${padT + plotH} L ${linePts} L ${xAt(n - 1)},${padT + plotH} Z`,
    fill: opt.color, 'fill-opacity': '0.12', stroke: 'none',
  }));
  svgEl.appendChild(_svgEl('path', {
    d: `M ${linePts}`, fill: 'none', stroke: opt.color, 'stroke-width': 2,
    'stroke-linejoin': 'round', 'stroke-linecap': 'round',
  }));

  // X軸ラベル（間引き・5個程度・末尾は必ず表示）。
  const labelStep = Math.max(1, Math.ceil(n / 5));
  points.forEach((p, i) => {
    if (i % labelStep !== 0 && i !== n - 1) return;
    const t = _svgEl('text', {
      x: xAt(i), y: H - 4, 'text-anchor': 'middle', 'font-size': 9, fill: 'var(--ink-3)',
    });
    t.textContent = p.date.slice(5).replace('-', '/');
    svgEl.appendChild(t);
  });

  // クロスヘア（ホバー/フォーカス位置の縦線）。
  const crosshair = _svgEl('line', {
    x1: -100, x2: -100, y1: padT, y2: padT + plotH,
    stroke: 'var(--ink-3)', 'stroke-width': 1, 'stroke-dasharray': '2,2', opacity: '0',
  });
  svgEl.appendChild(crosshair);

  function showTip(i, clientX, clientY) {
    const p = points[i];
    crosshair.setAttribute('x1', xAt(i));
    crosshair.setAttribute('x2', xAt(i));
    crosshair.setAttribute('opacity', '1');
    tipEl.hidden = false;
    tipEl.innerHTML = '';   // 値は number/date のみ（テキストは textContent で挿入・XSS対策）
    const vd = document.createElement('div');
    vd.className = 'v'; vd.textContent = `${p.value}${opt.unit}`;
    const dd = document.createElement('div');
    dd.className = 'd'; dd.textContent = p.date;
    tipEl.appendChild(vd); tipEl.appendChild(dd);
    const wrapRect = wrapEl.getBoundingClientRect();
    tipEl.style.left = `${clientX - wrapRect.left}px`;
    tipEl.style.top = `${clientY - wrapRect.top}px`;
  }
  function hideTip() {
    crosshair.setAttribute('opacity', '0');
    tipEl.hidden = true;
  }

  // ホバー/キーボードフォーカス用のヒットバンド（日ごとに等分割・当たり判定はマークより広く）。
  const bandW = plotW / n;
  points.forEach((p, i) => {
    const hit = _svgEl('rect', {
      x: padL + bandW * i, y: padT, width: Math.max(bandW, 1), height: plotH,
      class: 'hit', fill: 'transparent', 'pointer-events': 'all', tabindex: '0',
    });
    hit.addEventListener('pointerenter', (e) => showTip(i, e.clientX, e.clientY));
    hit.addEventListener('pointermove', (e) => showTip(i, e.clientX, e.clientY));
    hit.addEventListener('pointerleave', hideTip);
    hit.addEventListener('focus', () => {
      const r = hit.getBoundingClientRect();
      showTip(i, r.left + r.width / 2, r.top);
    });
    hit.addEventListener('blur', hideTip);
    svgEl.appendChild(hit);
  });
}

function renderCharts(daily, period) {
  const filled = fillDailySeries(daily, period && period.start, period && period.end);
  renderTrendChart(
    $('chart-au-svg'), $('chart-au-empty'), $('chart-au-tip'), $('chart-au-wrap'),
    filled.map((d) => ({ date: d.date, value: d.active_users })),
    { color: 'var(--accent)', unit: '人', label: '日別アクティブユーザー数' },
  );
  renderTrendChart(
    $('chart-tn-svg'), $('chart-tn-empty'), $('chart-tn-tip'), $('chart-tn-wrap'),
    filled.map((d) => ({ date: d.date, value: d.turns })),
    { color: 'var(--ok)', unit: '件', label: '日別ターン数' },
  );
}

// ===== 利用の傾向（ゼロヒット率・ヒートマップ・world/頭脳別・定着・DL数） =====
// ヒートマップ/world別は「magnitude」＝sequential 1色（--accent）。頭脳別は「identity」＝カテゴリ色（既存 semantic token の固定順＋常設凡例）。
// 週次アクティブ・DL日別は既存 renderTrendChart をそのまま再利用する。

function renderZeroHitTile(zeroHit) {
  const rate = zeroHit?.rate;
  $('t-zerohit').textContent = zeroHit?.knowledge_turns === 0 ? '対象なし' : fmtPctOrDash(rate);
  $('zero-hit-counts').textContent = `出典なし ${fmtTokOrDash(zeroHit?.zero_hit_turns)}件 / 社内資料参照の回答 ${fmtTokOrDash(zeroHit?.knowledge_turns)}件。出典の有無は回答の正しさを示しません。`;
}

const DAY_LABELS_JST = ['日', '月', '火', '水', '木', '金', '土'];   // Postgres DOW: 0=日〜6=土

function renderHeatmap(heatmapData) {
  const svgEl = $('heatmap-svg'), emptyEl = $('heatmap-empty'), tipEl = $('heatmap-tip'), wrapEl = $('heatmap-wrap');
  while (svgEl.firstChild) svgEl.removeChild(svgEl.firstChild);
  const total = (heatmapData || []).reduce((s, c) => s + (c.count || 0), 0);
  if (!total) {
    svgEl.style.display = 'none'; emptyEl.hidden = false; tipEl.hidden = true;
    return;
  }
  svgEl.style.display = 'block'; emptyEl.hidden = true;

  const grid = {};
  let maxV = 0;
  (heatmapData || []).forEach((c) => {
    grid[`${c.weekday}-${c.hour}`] = c.count || 0;
    if ((c.count || 0) > maxV) maxV = c.count;
  });

  const W = 600, H = 150;
  const padL = 24, padR = 4, padT = 4, padB = 15;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const cellW = plotW / 24, cellH = plotH / 7;
  svgEl.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svgEl.setAttribute('preserveAspectRatio', 'none');
  svgEl.setAttribute('role', 'img');
  svgEl.setAttribute('aria-label', `時間帯×曜日の利用ヒートマップ。最も多い時間帯は${maxV}件。`);

  DAY_LABELS_JST.forEach((d, wd) => {
    const t = _svgEl('text', {
      x: padL - 5, y: padT + cellH * wd + cellH / 2 + 3, 'text-anchor': 'end', 'font-size': 9, fill: 'var(--ink-3)',
    });
    t.textContent = d;
    svgEl.appendChild(t);
  });
  [0, 6, 12, 18, 23].forEach((h) => {
    const t = _svgEl('text', {
      x: padL + cellW * h + cellW / 2, y: H - 3, 'text-anchor': 'middle', 'font-size': 8.5, fill: 'var(--ink-3)',
    });
    t.textContent = String(h);
    svgEl.appendChild(t);
  });

  function showTip(wd, h, count, clientX, clientY) {
    tipEl.hidden = false;
    tipEl.innerHTML = '';
    const vd = document.createElement('div'); vd.className = 'v'; vd.textContent = `${count}件`;
    const dd = document.createElement('div'); dd.className = 'd'; dd.textContent = `${DAY_LABELS_JST[wd]}曜 ${h}時台`;
    tipEl.appendChild(vd); tipEl.appendChild(dd);
    const wrapRect = wrapEl.getBoundingClientRect();
    tipEl.style.left = `${clientX - wrapRect.left}px`;
    tipEl.style.top = `${clientY - wrapRect.top}px`;
  }
  function hideTip() { tipEl.hidden = true; }

  for (let wd = 0; wd < 7; wd++) {
    for (let h = 0; h < 24; h++) {
      const count = grid[`${wd}-${h}`] || 0;
      // sequential 1色（--accent）: 0 は無色（枠線のみ）・値が大きいほど濃い塗り（下限を設けて可視化を保証）。
      const opacity = count > 0 ? Math.max(0.15, count / maxV) : 0;
      const rect = _svgEl('rect', {
        x: padL + cellW * h + 1, y: padT + cellH * wd + 1,
        width: Math.max(cellW - 2, 1), height: Math.max(cellH - 2, 1),
        rx: 2, class: 'cell', fill: 'var(--accent)', 'fill-opacity': String(opacity),
        stroke: count > 0 ? 'none' : 'var(--border)', 'stroke-width': count > 0 ? '0' : '1',
        tabindex: '0',
      });
      rect.addEventListener('pointerenter', (e) => showTip(wd, h, count, e.clientX, e.clientY));
      rect.addEventListener('pointermove', (e) => showTip(wd, h, count, e.clientX, e.clientY));
      rect.addEventListener('pointerleave', hideTip);
      rect.addEventListener('focus', () => {
        const r = rect.getBoundingClientRect();
        showTip(wd, h, count, r.left + r.width / 2, r.top);
      });
      rect.addEventListener('blur', hideTip);
      svgEl.appendChild(rect);
    }
  }
}

// 汎用の横棒チャート（world別・頭脳別で共用）。items: [{label, value, color}]。
function renderBarChart(svgEl, emptyEl, tipEl, wrapEl, items, opt) {
  while (svgEl.firstChild) svgEl.removeChild(svgEl.firstChild);
  const total = items.reduce((s, it) => s + (it.value || 0), 0);
  if (!items.length || total === 0) {
    svgEl.style.display = 'none'; emptyEl.hidden = false; tipEl.hidden = true;
    wrapEl.style.height = '';
    return;
  }
  svgEl.style.display = 'block'; emptyEl.hidden = true;

  const rowH = 30, barH = 18;
  const W = 600, H = items.length * rowH + 8;
  wrapEl.style.height = `${H}px`;
  const labelW = 128, valW = 44;
  const padL = labelW, padR = valW, padT = 4;
  const plotW = W - padL - padR;
  svgEl.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svgEl.setAttribute('preserveAspectRatio', 'none');
  svgEl.setAttribute('role', 'img');
  svgEl.setAttribute('aria-label',
    `${opt.title || ''}: ${items.map((it) => `${it.label} ${it.value}${opt.unit || ''}`).join('、')}`);

  const maxV = Math.max(...items.map((it) => it.value), 1);

  function showTip(it, clientX, clientY) {
    tipEl.hidden = false;
    tipEl.innerHTML = '';
    const vd = document.createElement('div'); vd.className = 'v'; vd.textContent = `${it.value}${opt.unit || ''}`;
    const dd = document.createElement('div'); dd.className = 'd'; dd.textContent = it.label;
    tipEl.appendChild(vd); tipEl.appendChild(dd);
    const wrapRect = wrapEl.getBoundingClientRect();
    tipEl.style.left = `${clientX - wrapRect.left}px`;
    tipEl.style.top = `${clientY - wrapRect.top}px`;
  }
  function hideTip() { tipEl.hidden = true; }

  items.forEach((it, i) => {
    const y = padT + i * rowH;
    const barW = it.value > 0 ? Math.max((plotW * it.value) / maxV, 3) : 0;

    const label = _svgEl('text', {
      x: padL - 8, y: y + barH / 2 + 3.5, 'text-anchor': 'end', 'font-size': 11, fill: 'var(--ink-2)',
    });
    label.textContent = it.label.length > 13 ? `${it.label.slice(0, 12)}…` : it.label;
    svgEl.appendChild(label);

    svgEl.appendChild(_svgEl('rect', {   // track（未達分の薄い背景・値の大きさが直感的に伝わるように）
      x: padL, y, width: plotW, height: barH, rx: 4, fill: 'var(--border)', 'fill-opacity': '0.35',
    }));
    if (barW > 0) {
      svgEl.appendChild(_svgEl('rect', {
        x: padL, y, width: barW, height: barH, rx: 4, fill: it.color || 'var(--accent)', class: 'bar',
      }));
    }
    const valLabel = _svgEl('text', {   // 値はバーの先端に（bars→value at the tip）
      x: padL + barW + 6, y: y + barH / 2 + 3.5, 'font-size': 10.5, fill: 'var(--ink)',
      'font-variant-numeric': 'tabular-nums',
    });
    valLabel.textContent = String(it.value);
    svgEl.appendChild(valLabel);

    const hit = _svgEl('rect', {
      x: 0, y, width: W, height: barH, fill: 'transparent', 'pointer-events': 'all', tabindex: '0',
    });
    hit.addEventListener('pointerenter', (e) => showTip(it, e.clientX, e.clientY));
    hit.addEventListener('pointermove', (e) => showTip(it, e.clientX, e.clientY));
    hit.addEventListener('pointerleave', hideTip);
    hit.addEventListener('focus', () => {
      const r = hit.getBoundingClientRect();
      showTip(it, r.left + r.width / 2, r.top);
    });
    hit.addEventListener('blur', hideTip);
    svgEl.appendChild(hit);
  });
}

function renderWorldBar(worlds) {
  const items = (worlds || []).map((w) => ({ label: w.world, value: w.turns || 0, color: 'var(--accent)' }));
  renderBarChart($('chart-world-svg'), $('chart-world-empty'), $('chart-world-tip'), $('chart-world-wrap'),
    items, { unit: '件', title: 'フォルダ別利用量' });
}

function renderProviderBar(providers) {
  const orderIdx = (id) => { const i = PROVIDER_ORDER.indexOf(id); return i === -1 ? 999 : i; };
  const items = (providers || [])
    .slice()
    .sort((a, b) => orderIdx(a.provider) - orderIdx(b.provider))
    .map((p) => ({
      label: PROVIDER_LABEL[p.provider] || p.provider || '不明',
      value: p.turns || 0,
      color: PROVIDER_COLOR[p.provider] || 'var(--ink-3)',
    }));
  renderBarChart($('chart-provider-svg'), $('chart-provider-empty'), $('chart-provider-tip'), $('chart-provider-wrap'),
    items, { unit: '件', title: '頭脳別利用比率' });
  // 常設の凡例（色だけに頼らない）。
  $('chart-provider-legend').innerHTML = items.map((it) =>
    `<span class="item"><span class="dot" style="background:${it.color}"></span>${esc(it.label)} ${it.value}件</span>`,
  ).join('');
}

// ターンの終了理由の分布（`sherpa/stop_kind.py` の閉じた語彙・単色バー＝各行が自分のラベルを持つため凡例配色は要らない）。
// 停止数（`stopped_turns`）は回答を保存しないため分布には現れない別集計＝バッジで併記する。
function renderStopKinds(stopKinds, stoppedTurns) {
  const byKind = new Map((stopKinds || []).map((r) => [r.stop_kind, r.turns || 0]));
  const items = STOP_KIND_ORDER
    .filter((k) => byKind.has(k))
    .map((k) => ({ label: stopKindLabel(k), value: byKind.get(k), color: 'var(--accent)' }));
  // allowlist 外の値（未知の stop_kind）が来ても取りこぼさない（fail-safe）。
  (stopKinds || []).forEach((r) => {
    if (!STOP_KIND_ORDER.includes(r.stop_kind)) {
      items.push({ label: stopKindLabel(r.stop_kind), value: r.turns || 0, color: 'var(--accent)' });
    }
  });
  renderBarChart($('chart-stopkind-svg'), $('chart-stopkind-empty'), $('chart-stopkind-tip'),
    $('chart-stopkind-wrap'), items, { unit: '件', title: 'ターンの終了理由' });
  $('stopkind-total-badge').textContent = `利用者停止 ${fmtTokOrDash(stoppedTurns)}${stoppedTurns == null ? '' : '件'}`;
}

// 回答の完了状態（`answer.completion` の4値＋旧形式の 'unknown'）の内訳を終了理由の下に1行で出す。
const COMPLETION_LABEL = { complete: '最後まで回答', partial: '途中までの回答', stopped: '停止', failed: '回答できず', unknown: '不明（旧形式）' };
function renderCompletions(completions) {
  const rows = (completions || []).filter((r) => r && (r.turns || 0) > 0);
  $('completion-note').textContent = rows.length
    ? '回答の状態: ' + rows.map((r) => `${COMPLETION_LABEL[r.completion] || r.completion} ${r.turns}件`).join('・') : '';
}

// ミリ秒を秒表記へ（null は未計測・欠落は未取得）。回答時間・所要時間の各テーブル/カードで共用。
function fmtSecOrDash(ms) {
  if (ms === undefined) return '未取得';
  if (ms === null) return '未計測';
  return `${(Number(ms) / 1000).toFixed(1)}秒`;
}
// 件数（会話あたりのやり取り回数の avg/median/p90 等）の小数表示（null は未計測・欠落は未取得）。
function fmtNumOrDash(v, digits) {
  if (v === undefined) return '未取得';
  if (v === null) return '未計測';
  return Number(v).toFixed(digits === undefined ? 1 : digits);
}
// 割合（0〜1）を%表示へ（null は未計測・欠落は未取得）。
function fmtPctOrDash(v) {
  if (v === undefined) return '未取得';
  if (v === null) return '未計測';
  return `${Math.round(v * 100)}%`;
}

// 会話あたりのやり取り回数（avg/median/max/p90）と resume 率。対象会話が無ければ全て「—」
// （タイル系カードは token-kind-card と違いカードごと隠さない＝サマリタイルと同じ流儀）。
function renderConversationTurns(turns, resumeRate) {
  const t = turns || {};
  $('t-turns-avg').textContent = fmtNumOrDash(t.avg);
  $('t-turns-median').textContent = fmtNumOrDash(t.median);
  $('t-turns-p90').textContent = fmtNumOrDash(t.p90);
  $('t-turns-max').textContent = fmtTokOrDash(t.max);
  $('t-resume-rate').textContent = t.session_eligible === 0 ? '対象なし' : fmtPctOrDash(resumeRate);
  $('session-counts').textContent = `IDあり ${fmtTokOrDash(t.session_recorded)}件 / 対象会話 ${fmtTokOrDash(t.session_eligible)}件`;
}

// 回答時間の分布（全体＋経路別）。`overall` は API 契約上つねに存在する行（対象0件でも
// avg/median/p90/max=null・n=0）＝カードは隠さず「全体」行だけの表として描画する。
function renderResponseTime(rt) {
  const tb = $('response-time-tbody');
  if (!tb) return;
  const overall = (rt && rt.overall) || {};
  const byProvider = (rt && rt.by_provider) || [];
  const rows = [{ label: '全体', row: overall }]
    .concat(byProvider.map((r) => ({ label: providerLabel(r.provider), row: r })));
  tb.innerHTML = rows.map(({ label, row }) => `<tr>
    <td>${esc(label)}</td>
    <td class="num">${fmtSecOrDash(row.avg)}</td>
    <td class="num">${fmtSecOrDash(row.median)}</td>
    <td class="num">${fmtSecOrDash(row.p90)}</td>
    <td class="num">${fmtSecOrDash(row.max)}</td>
    <td class="num">${fmtTokOrDash(row.n)}</td>
  </tr>`).join('');
}

// 「打ち切りの内訳」（`InvestigationState.limits`・制限そのものは変えない計測専用）。
// 行=経路（provider）・列=各項目の「対象ターン数のうち当たったターン数」（回数系は「n件（合計m回）」・
// bool系は件数のみ）——`stats.limits.by_provider` が空（対象ターンが1件も無い期間）なら空表のまま。
function renderLimits(limits) {
  const tb = $('limits-tbody');
  if (!tb) return;
  const rows = (limits && limits.by_provider) || [];
  const cnt = (turns, total) => turns == null ? '未計測' : (total === undefined
    ? `${turns.toLocaleString('ja-JP')}件`
    : `${turns.toLocaleString('ja-JP')}件（計${fmtTokOrDash(total)}回）`);
  tb.innerHTML = rows.map((r) => `<tr>
    <td>${esc(providerLabel(r.provider))}</td>
    <td class="num">${(r.turns || 0).toLocaleString('ja-JP')}</td>
    <td class="num">${cnt(r.tool_result_clipped_turns, r.tool_result_clipped_total)}</td>
    <td class="num">${cnt(r.total_budget_hit_turns)}</td>
    <td class="num">${cnt(r.context_compactions_turns, r.context_compactions_total)}</td>
    <td class="num">${cnt(r.synthesis_truncated_turns)}</td>
    <td class="num">${cnt(r.depth_escalated_turns)}</td>
    <td class="num">${cnt(r.search_truncated_turns, r.search_truncated_total)}</td>
    <td class="num">${cnt(r.auto_continues_turns, r.auto_continues_total)}</td>
    <td class="num">${cnt(r.duplicate_tool_call_turns, r.duplicate_tool_call_total)}</td>
    <td class="num">${cnt(r.tool_calls_exhausted_turns)}</td>
    <td class="num">${cnt(r.backend_unavailable_fulltext_turns)}</td>
    <td class="num">${cnt(r.backend_unavailable_graph_turns)}</td>
    <td class="num">${cnt(r.graph_reingest_required_turns)}</td>
  </tr>`).join('');
}

// 「道具の使われ方」と「影響一覧」（件数だけ・資料名や検索語は出さない）。古い期間（新しい欄が 0・欠け）でも空の表で出す。
function renderToolUse(tc, impact) {
  const n = (v) => (v || 0).toLocaleString('ja-JP');
  const tb = $('tool-calls-tbody');
  if (tb) {
    tb.innerHTML = ((tc && tc.tools) || []).map((t) => {
      const r = t.by_role || {};
      const avg = t.calls ? `${Math.round((t.ms || 0) / t.calls).toLocaleString('ja-JP')} ms` : '—';
      return `<tr><td>${esc(t.tool)}</td><td class="num">${n(t.calls)}</td><td class="num">${n(t.found)}</td>`
        + `<td class="num">${avg}</td><td class="num">${n(t.errors)}</td><td class="num">${n(t.truncated)}</td>`
        + `<td class="num">${n((r.parent || {}).calls)}</td><td class="num">${n((r.child || {}).calls)}</td><td class="num">${n((r.undetermined || {}).calls)}</td></tr>`;
    }).join('');
  }
  const sum = $('tool-calls-summary');
  if (sum && tc) {
    sum.textContent = `記録のあるターン ${n(tc.turns)}件・記録の欠け ${n(tc.missing)}行・開いた資料 ${n(tc.opened_docs)}件`
      + `（記録が不完全なターン ${n(tc.opened_unknown_turns)}件）。`
      + `道具を使わずに答えようとして調べ直しを促したターン ${n(tc.nudged_turns)}件・促した後に使った ${n(tc.nudged_then_used)}件・`
      + `それでも使わなかった ${n(tc.no_tool_use_turns)}件。`;
  }
  const im = $('impact-summary');
  if (im && impact) {
    im.textContent = `影響をたどったターン ${n(impact.traced_turns)}件（たどらなかった ${n(impact.untraced_turns)}件）・`
      + `候補 ${n(impact.candidate)}件・確かめた ${n(impact.inspected)}件・根拠に使った ${n(impact.used)}件・対応が分からない ${n(impact.unmapped)}件${impact.more ? `・上限で省いた行 ${n(impact.more)}件（状態は数えていません）` : ''}${impact.hidden ? `・名前を出せない行 ${n(impact.hidden)}件（状態は数えていません）` : ''}。`;
  }
}

const REVIEW_LABELS = {
  confirmed: '確定', inferred: '推定', unknown: '不明',
  sufficient: '十分', insufficient: '根拠不足', undecidable: '判定できず',
  not_found_in_scope: '範囲内で見つからない', unexplored: '未調査', conflict: '食い違い', budget: '調査の上限', unreadable: '読み取り不可',
  source_missing: 'ソース未確認', spec_missing: '設計書未確認', definition_missing: '定義未確認',
  log_missing: 'ログ・設定未確認', callgraph_missing: '呼出関係未確認',
  rerun: '次の見直しへ', rounds_exhausted: '見直しの回数に到達',
  user_stop: '利用者が停止', ask_user: '利用者に確認', review_failed: '確認に失敗', failed: '失敗',
  tool_result_clipped: '1件の読取量を制限', total_budget_hit: '累計の読取量に到達',
  context_compactions: '会話履歴を整理', synthesis_truncated: '回答用の情報量を制限',
  depth_escalated: '自動で深く調べた',
  search_truncated: '検索件数を制限', auto_continues: '続きを自動で実行',
  duplicate_tool_call: '同じ条件の再検索を省略',
  tool_calls_exhausted: '調査の回数上限に到達',
  backend_unavailable_fulltext: '全文検索が使えなかった', backend_unavailable_graph: 'グラフが使えなかった',
  graph_reingest_required: 'グラフは再取り込み待ち',
};
const REVIEW_DEPTH_LABELS = { quick: 'クイック', standard: '標準', deep: '深く', max: '最大' };
const REVIEW_CONDITION_LABELS = { main: '本番相当', 'depth2-quick': '見直しなし', 'depth2-standard': '標準（見直し 2 回）', 'depth2-deep': '深く（4 回）', 'depth2-max': '最大' };
function reviewCounts(counts) {
  const totals = new Map();
  Object.entries(counts).forEach(([key, count]) => {
    const label = Object.hasOwn(REVIEW_LABELS, key) ? REVIEW_LABELS[key] : 'その他';
    totals.set(label, (totals.get(label) || 0) + count);
  });
  return Array.from(totals, ([label, count]) => `${label}: ${fmtNumOrDash(count, 0)}`)
    .join('／') || '記録なし';
}
function renderReviewStats(rounds, quality) {
  const table = (heads, rows) => '<table><thead><tr>'
    + heads.map(h => `<th scope="col">${esc(h)}</th>`).join('') + '</tr></thead><tbody>'
    + (rows.length ? rows.map(row => '<tr>' + row.map(cell => `<td>${esc(String(cell))}</td>`).join('') + '</tr>').join('')
      : `<tr><td colspan="${heads.length}">この期間の記録はありません。</td></tr>`)
    + '</tbody></table>';
  const roundTable = (rows, byRound) => table([
    '深さ', '使った AI', ...(byRound ? ['見直しの順番'] : []), '記録数', '増えた出典の合計',
    '平均所要時間（秒）', '判断の内訳', '制限に当たった回数', '見直しの判定', '終了・継続の理由', '足りなかった点',
  ], rows.map(r => [
    REVIEW_DEPTH_LABELS[r.depth_profile] || '不明', providerLabel(r.provider), ...(byRound ? [r.round_no ?? '不明'] : []),
    r.rounds, fmtNumOrDash(r.citations_delta_total, 0), fmtSecOrDash(r.elapsed_ms_avg),
    reviewCounts(r.claims), reviewCounts(r.limits), reviewCounts(r.verdicts), reviewCounts(r.stops), reviewCounts(r.missing_codes),
  ]));
  $('review-stats').innerHTML = (!(rounds?.by_depth_provider?.length || rounds?.by_round?.length || quality?.by_rounds?.length)
    ? '<p class="hint">見直しの機能はこの環境では未導入です。</p>' : '')
    + '<h3>深さ・使った AI ごと</h3>' + roundTable(rounds?.by_depth_provider ?? [], false)
    + '<h3>何回目の見直しかで比べる</h3>' + roundTable(rounds?.by_round ?? [], true)
    + '<h3>正解付きの比較</h3>'
    + (quality?.period
      ? `<p class="hint">集計期間: ${esc(quality.period.start ?? '不明')} 〜 ${esc(quality.period.end ?? '不明')}。見直しの記録とは別に採点した結果です。</p>`
      : '')
    + table(['条件', '見直しの回数', '比較件数', '正解', '誤った断定', '回答漏れ', '以前より悪化', '未採点', '費用合計（米ドル）'],
      (quality?.by_rounds ?? []).map(r => [
        Object.hasOwn(REVIEW_CONDITION_LABELS, r.condition) ? REVIEW_CONDITION_LABELS[r.condition] : '不明',
        r.rounds, r.runs, r.correct, r.wrong_assertion, r.missing, r.regressed, r.unrated,
        fmtNumOrDash(r.cost_usd_total, 2)]));
}

function renderWeeklyAndRetention(retention) {
  const weekly = (retention && retention.weekly) || [];
  renderTrendChart(
    $('chart-weekly-svg'), $('chart-weekly-empty'), $('chart-weekly-tip'), $('chart-weekly-wrap'),
    weekly.map((w) => ({ date: w.week_start, value: w.active_users })),
    { color: 'var(--accent)', unit: '人', label: '週次アクティブユーザー数' },
  );
  const rate = retention && retention.revisit_rate;
  $('revisit-rate-val').textContent = (rate === null || rate === undefined)
    ? '算出できません（データ不足）' : `${Math.round(rate * 100)}%`;
}

function renderDownloadsChart(downloads, period) {
  const dailyForFill = ((downloads && downloads.daily) || []).map((d) => ({ date: d.date, turns: d.count }));
  const filled = fillDailySeries(dailyForFill, period && period.start, period && period.end);
  renderTrendChart(
    $('chart-dl-svg'), $('chart-dl-empty'), $('chart-dl-tip'), $('chart-dl-wrap'),
    filled.map((d) => ({ date: d.date, value: d.turns })),
    { color: 'var(--ok)', unit: '件', label: '原本ダウンロード数（日別）' },
  );
  // 見出し脇に期間合計を表示（日別グラフだけだと合計が読み取りにくい）。
  const total = (downloads && downloads.total) || 0;
  $('dl-total-badge').textContent = `期間合計 ${total.toLocaleString('ja-JP')}件`;
}

// ===== トークン（入力/出力トークン数のみ） =====
function providerLabel(p) { return PROVIDER_LABEL[p] || p || '不明'; }

// 用途別（kind）内訳の平文日本語ラベル。未知 kind は生の kind をそのまま表示する。
const KIND_LABEL = {
  'chat-sub': '下調べ',
  research: '外部連携の調査',
  extract: 'ナレッジ抽出（旧機能）',
  propose: '概念の候補づくり（旧機能）',
  chat: '会話', intent: '依頼の仕分け',
  embed: '検索の索引づくり', graph_ask: 'グラフへの質問', vlm: '画像の読み取り',
  // 複数プロファイル自動選択の計画呼び出し。
  'chat-plan': '進め方の計画',
  usage_chat: '利用統計チャット',
  // 清書前のメイン査読（根拠の十分性判定・限定ツール精読）。
  'chat-review': '根拠の査読',
  // 取り込み後にバックグラウンドで後追い実行する rag.md の LLM 成形。
  rag_render: '検索用文書の整形',
};
function kindLabel(k) { return KIND_LABEL[k] || k; }
// トークン列は null（プロバイダが usage を報告しなかった「報告不能」マーカー）なら「未計測」、項目欠落なら「未取得」で表示する。
function fmtTokOrDash(v) { return v === undefined ? '未取得' : v === null ? '未計測' : Number(v).toLocaleString('ja-JP'); }
// キャッシュへの書き込み量。全件が不明（null）なら「不明」（0とは区別）、不明の行が混ざる合計は「（不明を含む）」を添える。項目欠落（古いサーバー）は「未取得」。
function fmtCacheWrite(r) {
  if (r.cache_write === undefined) return '未取得';
  if (r.cache_write === null) return '不明';
  const v = Number(r.cache_write).toLocaleString('ja-JP');
  return r.cache_write_unknown > 0 ? `${v}（不明を含む）` : v;
}
function renderTokenKindTable(rows) {
  const card = $('token-kind-card');
  const tb = $('token-kind-tbody');
  if (!tb) return;
  if (!rows || !rows.length) {
    if (card) card.hidden = true;   // 空/不在ならカードごと隠す
    return;
  }
  if (card) card.hidden = false;
  tb.innerHTML = rows.map((r) => {
    const name = `${esc(providerLabel(r.provider))}`
      + (r.model ? ` <span class="user-uid">${esc(r.model)}</span>` : '');
    return `<tr>
      <td>${esc(kindLabel(r.kind))}</td>
      <td>${name}</td>
      <td class="num">${(r.calls || 0).toLocaleString('ja-JP')}</td>
      <td class="num">${fmtTokOrDash(r.input)}</td>
      <td class="num">${fmtTokOrDash(r.cached_input)}</td>
      <td class="num">${fmtCacheWrite(r)}</td>
      <td class="num">${fmtTokOrDash(r.output)}</td>
      <td class="num">${fmtTokOrDash(r.reasoning_output)}</td>
      <td class="num">${fmtSecOrDash(r.elapsed_ms_total)}</td>
      <td class="num">${fmtSecOrDash(r.elapsed_ms_avg)}</td>
      <td class="num">${(r.elapsed_n || 0).toLocaleString('ja-JP')}</td>
    </tr>`;
  }).join('');
}

// ユーザー別 × 用途別内訳。利用者に紐付かない呼び出しを含まないため、同一 kind の合計は token-kind-tbody の当該行以下になりうる。
// 空/不在ならカードごと隠す（token-kind-card と同じ流儀）。
function renderTokenUserKindTable(rows) {
  const card = $('token-user-kind-card');
  const tb = $('token-user-kind-tbody');
  if (!tb) return;
  if (!rows || !rows.length) {
    if (card) card.hidden = true;
    return;
  }
  if (card) card.hidden = false;
  tb.innerHTML = rows.map((r) => `<tr>
    <td><div class="user-name">${esc(r.display_name || r.uid)}</div><div class="user-uid">${esc(r.uid)}</div></td>
    <td>${esc(kindLabel(r.kind))}</td>
    <td class="num">${(r.calls || 0).toLocaleString('ja-JP')}</td>
    <td class="num">${fmtTokOrDash(r.input)}</td>
    <td class="num">${fmtTokOrDash(r.cached_input)}</td>
    <td class="num">${fmtCacheWrite(r)}</td>
    <td class="num">${fmtTokOrDash(r.output)}</td>
    <td class="num">${fmtTokOrDash(r.reasoning_output)}</td>
    <td class="num">${fmtSecOrDash(r.elapsed_ms_total)}</td>
    <td class="num">${fmtSecOrDash(r.elapsed_ms_avg)}</td>
  </tr>`).join('');
}
function renderTokenModelTable(rows) {
  const tb = $('token-model-tbody');
  if (!rows.length) {
    tb.innerHTML = '<tr class="empty-row"><td colspan="7">この期間のトークン記録はまだありません</td></tr>';
    return;
  }
  tb.innerHTML = rows.map((r) => {
    const name = `${esc(providerLabel(r.provider))}`
      + (r.model ? ` <span class="user-uid">${esc(r.model)}</span>` : '');
    return `<tr>
      <td>${name}</td>
      <td class="num">${(r.turns || 0).toLocaleString('ja-JP')}</td>
      <td class="num">${fmtTokOrDash(r.input)}</td>
      <td class="num">${fmtTokOrDash(r.cached_input)}</td>
      <td class="num">${fmtCacheWrite(r)}</td>
      <td class="num">${fmtTokOrDash(r.output)}</td>
      <td class="num">${fmtTokOrDash(r.reasoning_output)}</td>
    </tr>`;
  }).join('');
}
function renderTokenUserTable(rows) {
  const tb = $('token-user-tbody');
  const top = rows || [];   // 取得時に画面・保存共通で上位10件へ絞っている。
  if (!top.length) {
    tb.innerHTML = '<tr class="empty-row"><td colspan="5">この期間のトークン記録はまだありません</td></tr>';
    return;
  }
  tb.innerHTML = top.map((u, i) => `<tr>
    <td><span class="${i === 0 ? 'rank top1' : 'rank'}">${i + 1}</span></td>
    <td><div class="user-name">${esc(u.display_name || u.uid)}</div><div class="user-uid">${esc(u.uid)}</div></td>
    <td class="num">${(u.turns || 0).toLocaleString('ja-JP')}</td>
    <td class="num">${fmtTokOrDash(u.input)}</td>
    <td class="num">${fmtTokOrDash(u.output)}</td>
  </tr>`).join('');
}
function renderTokens(tokens, period) {
  const t = tokens || {};
  const tot = t.totals || {};
  $('t-tok-input').textContent = fmtTokOrDash(tot.input);
  $('t-tok-output').textContent = fmtTokOrDash(tot.output);
  const daily = t.daily || [];
  renderTrendChart(
    $('chart-tokin-svg'), $('chart-tokin-empty'), $('chart-tokin-tip'), $('chart-tokin-wrap'),
    fillDailySeries(daily.map((d) => ({ date: d.date, turns: d.input })), period && period.start, period && period.end)
      .map((d) => ({ date: d.date, value: d.turns })),
    { color: 'var(--accent)', unit: 'tok', label: '入力トークン数（日別）' },
  );
  renderTrendChart(
    $('chart-tokout-svg'), $('chart-tokout-empty'), $('chart-tokout-tip'), $('chart-tokout-wrap'),
    fillDailySeries(daily.map((d) => ({ date: d.date, turns: d.output })), period && period.start, period && period.end)
      .map((d) => ({ date: d.date, value: d.turns })),
    { color: 'var(--ok)', unit: 'tok', label: '出力トークン数（日別）' },
  );
  renderTokenModelTable(t.by_model || []);
  renderTokenUserTable(t.by_user || []);
  renderTokenKindTable(t.by_kind || []);
  renderTokenUserKindTable(t.by_user_kind || []);
}

// 会話ごとの補助 AI 使用量: トークン合計降順で上位20件・タイトル/本文は含まない。
// 「用途別」列は用途ごとに回数・トークン・所要時間を1行ずつ積む（`UsageConversationKindRow` の null の意味は用途別テーブルと同じ）。会話 id はテキスト表示のみ（リンクにしない）。
function conversationKindsSummaryHTML(kinds) {
  return `<ul class="convkinds">${(kinds || []).map((k) => {
    // 入力/出力のどちらかが null（報告不能マーカー・token-kind-tbody と同じ意味）なら合算しない。
    const hasTokens = k.input !== null && k.input !== undefined && k.output !== null && k.output !== undefined;
    const tokTotal = hasTokens ? (k.input + k.output) : null;
    return `<li><b>${esc(kindLabel(k.kind))}</b> ${(k.calls || 0).toLocaleString('ja-JP')}回`
      + `・トークン計${fmtTokOrDash(tokTotal)}`
      + `・所要時間${fmtSecOrDash(k.elapsed_ms_total)}</li>`;
  }).join('')}</ul>`;
}
function renderConversationsTop(rows) {
  const card = $('conversations-top-card');
  const tb = $('conversations-top-tbody');
  if (!tb) return;
  if (!rows || !rows.length) {
    if (card) card.hidden = true;
    return;
  }
  if (card) card.hidden = false;
  tb.innerHTML = rows.map((r) => `<tr>
    <td>#${esc(String(r.conversation_id))}</td>
    <td><div class="user-name">${esc(r.display_name || r.uid)}</div><div class="user-uid">${esc(r.uid)}</div></td>
    <td>${r.world ? esc(r.world) : '<span style="color:var(--ink-3)">—</span>'}</td>
    <td class="num">${(r.user_turns || 0).toLocaleString('ja-JP')}</td>
    <td>${conversationKindsSummaryHTML(r.kinds)}</td>
    <td class="num">${fmtSecOrDash(r.response_time_avg_ms)}</td>
  </tr>`).join('');
}

function detailHTML(u) {
  const worlds = (u.worlds || []).length
    ? u.worlds.map((w) => `<span class="worldtag">${esc(w)}</span>`).join('')
    : '<span style="color:var(--ink-3)">—</span>';
  return `
    ${lensBarHTML(u.lens || {})}
    <div class="u-meta">
      <div class="g"><b>${(u.personal_turns || 0).toLocaleString('ja-JP')}</b><span>個人ファイル参照ターン</span></div>
      <div class="g"><b>${(u.logins || 0).toLocaleString('ja-JP')}</b><span>ログイン回数</span></div>
      <div class="g"><b>${(u.downloads || 0).toLocaleString('ja-JP')}</b><span>原本ダウンロード</span></div>
      <div class="g"><b>${(u.uploads || 0).toLocaleString('ja-JP')}</b><span>個人ファイルアップロード</span></div>
      <div class="g"><b>${(u.shares || 0).toLocaleString('ja-JP')}</b><span>会話共有の発行</span></div>
      <div class="g"><b style="font-size:var(--text-caption)">${worlds}</b><span>利用フォルダ</span></div>
    </div>`;
}

function zeroHitCellHTML(u) {
  if (u.zero_hit_rate === null || u.zero_hit_rate === undefined) {
    const label = u.knowledge_turns === 0 ? '対象なし' : fmtPctOrDash(u.zero_hit_rate);
    return `<td class="num zhr-cell">${label}</td>`;
  }
  const pct = Math.round(u.zero_hit_rate * 100);
  const tip = `${(u.knowledge_turns || 0).toLocaleString('ja-JP')}件中${(u.zero_hit_turns || 0).toLocaleString('ja-JP')}件が出典なし`;
  return `<td class="num zhr-cell" title="${esc(tip)}">${pct}%</td>`;
}

function renderRows(users) {
  const tbody = $('usage-tbody');
  if (!users.length) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="8">この期間の利用はまだありません</td></tr>';
    return;
  }
  tbody.innerHTML = users.map((u, i) => {
    const rank = i + 1;
    const rankCls = rank === 1 ? 'rank top1' : 'rank';
    const name = esc(u.display_name || u.uid);
    return `<tr class="u-row" data-uid="${esc(u.uid)}">
      <td><span class="${rankCls}">${rank}</span></td>
      <td><div class="user-name">${name}</div><div class="user-uid">${esc(u.uid)}</div></td>
      <td class="num">${(u.turns || 0).toLocaleString('ja-JP')}</td>
      <td class="num">${(u.conversations || 0).toLocaleString('ja-JP')}</td>
      <td class="num">${(u.active_days || 0).toLocaleString('ja-JP')}</td>
      <td>${esc(fmtDate(u.last_active))}</td>
      <td class="num">${(u.personal_turns || 0).toLocaleString('ja-JP')}</td>
      ${zeroHitCellHTML(u)}
    </tr>
    <tr class="u-detail"><td colspan="8">${detailHTML(u)}</td></tr>`;
  }).join('');
}

function sortUsers(users) {
  const dir = _sortDir === 'asc' ? 1 : -1;
  return [...users].sort((a, b) => {
    if (_sortKey === 'last_active') {
      const av = a.last_active || '', bv = b.last_active || '';
      return av < bv ? -1 * dir : av > bv ? 1 * dir : 0;
    }
    if (_sortKey === 'zero_hit_rate') {
      const av = a.zero_hit_rate === null || a.zero_hit_rate === undefined ? -1 : a.zero_hit_rate;
      const bv = b.zero_hit_rate === null || b.zero_hit_rate === undefined ? -1 : b.zero_hit_rate;
      return (av - bv) * dir;
    }
    return ((a.turns || 0) - (b.turns || 0)) * dir;
  });
}

function applySortAndRender() {
  renderRows(sortUsers(_users));
  document.querySelectorAll('th.sortable').forEach((th) => {
    const arrow = th.querySelector('.arrow');
    if (!arrow) return;
    arrow.textContent = th.dataset.sort === _sortKey ? (_sortDir === 'desc' ? '▼' : '▲') : '';
  });
}

function setLoading() {
  $('usage-tbody').setAttribute('aria-busy', 'true');
  $('usage-tbody').innerHTML = '<tr><td colspan="8"><div class="loading" role="status" style="padding:16px">'
    + '<span class="spinner spinner-sm"></span><span>利用統計を読み込んでいます...</span></div></td></tr>';
}

// ===== データ取得 =====
async function load(period) {
  const seq = ++_loadSeq;   // このリクエストの連番（連打時、最新以外の描画は破棄する）
  _period = period;
  _stats = null;
  $('period-range-error').textContent = '';
  _loadedAt = null;
  $('usage-export').disabled = true;
  $('usage-export-detail').disabled = true;
  $('usage-stat-panels').hidden = true;
  $('usage-period-label').textContent = `${periodText(period)}を取得中…`;
  $('usage-load-status').textContent = '利用統計を読み込んでいます…';
  setLoading();
  $('review-stats').textContent = '見直しの集計を読み込んでいます…';
  document.querySelectorAll('.period-bar .filterchip').forEach((b) => {
    const on = !period.start && Number(b.dataset.days) === period.days;
    b.classList.toggle('on', on);
    b.setAttribute('aria-pressed', String(on));
  });
  if (period.start) { $('period-start').value = period.start; $('period-end').value = period.end; }
  const query = periodApiQuery(period);
  try {
    const d = await getJSON('/admin/usage/stats?' + query);
    if (seq !== _loadSeq) return;   // 後から連打された別リクエストが既に最新＝このレスポンスは古い
    // 画面とJSON保存で同じ範囲を共有する（サーバのトークン降順を維持）。
    if (d.tokens?.by_user) d.tokens.by_user = d.tokens.by_user.slice(0, 10);
    renderSummary(d.totals || {});
    renderZeroHitTile(d.zero_hit);
    renderCharts(d.daily || [], d.period);
    renderHeatmap(d.heatmap || []);
    renderWorldBar(d.worlds || []);
    renderProviderBar(d.providers || []);
    renderWeeklyAndRetention(d.retention || {});
    renderDownloadsChart(d.downloads || {}, d.period);
    renderStopKinds(d.stop_kinds || [], d.stopped_turns);
    renderCompletions(d.completions || []);
    renderConversationTurns(d.conversation_turns || {}, d.resume_rate);
    renderResponseTime(d.response_time || {});
    renderLimits(d.limits || {});
    renderToolUse(d.tool_calls || {}, d.impact || {});
    renderReviewStats(d.rounds, d.quality_runs);
    renderTokens(d.tokens || {}, d.period);
    renderConversationsTop(d.conversations_top || []);
    _users = d.users || [];
    applySortAndRender();
    _stats = d;
    _loadedAt = new Date().toISOString();
    $('usage-period-label').textContent = `${d.period.start} ～ ${d.period.end}（JST・終了日を含む）`;
    if (!$('period-range-error').textContent) {   // 送信前に弾いた入力と理由は残す
      $('period-start').value = d.period.start;
      $('period-end').value = d.period.end;
    }
    $('usage-load-status').textContent = `取得時刻: ${new Date(_loadedAt).toLocaleString('ja-JP', { timeZone: 'Asia/Tokyo' })} JST`;
    $('usage-stat-panels').hidden = false;
    $('usage-export').disabled = false;
    $('usage-export-detail').disabled = false;
    $('usage-tbody').setAttribute('aria-busy', 'false');
    showUsageTab(usageView().tab);
  } catch (e) {
    if (seq !== _loadSeq) return;
    $('usage-period-label').textContent = `${periodText(period)}（取得失敗）`;
    $('usage-load-status').textContent = `利用統計を取得できませんでした: ${String(e)}。期間ボタンで再取得できます。`;
    $('usage-tbody').setAttribute('aria-busy', 'false');
    $('usage-tbody').innerHTML = `<tr><td colspan="8" style="color:var(--danger);padding:16px">読み込みに失敗しました: ${esc(String(e))}</td></tr>`;
    $('review-stats').textContent = '見直しの集計を読み込めませんでした。';
    toast('利用統計の読み込みに失敗しました');
  }
}

$('usage-export').addEventListener('click', () => {
  const payload = { retrieved_at: _loadedAt, timezone: 'Asia/Tokyo', period: _stats.period,
    definitions: $('usage-definitions').textContent.trim(), stats: _stats };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
  Sherpa.downloadBlob(blob, `usage-${_stats.period.start}-${_stats.period.end}.json`);
});

$('usage-export-detail').addEventListener('click', async () => {
  const btn = $('usage-export-detail');
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = '作成中…';
  $('usage-load-status').textContent = '明細ZIPを作成しています…';
  try {
    const r = await fetch('/admin/usage/export?' + periodApiQuery(_period));
    if (!r.ok) {
      let data = null;
      try { data = await r.json(); } catch (_) { data = null; }
      throw new Error((data && data.detail) || `エラー (${r.status})`);
    }
    const blob = await r.blob();
    const cd = r.headers.get('content-disposition') || '';
    const m = cd.match(/filename="([^"]+)"/);
    const name = m ? m[1] : 'usage-detail.zip';
    Sherpa.downloadBlob(blob, name);
    $('usage-load-status').textContent = `明細ZIPを保存しました: ${name}`;
  } catch (e) {
    $('usage-load-status').textContent = `明細ZIPの作成に失敗しました: ${String(e)}`;
    toast('明細ZIPの作成に失敗しました');
  } finally {
    btn.disabled = !_stats;   // 作成中に期間が変わり読み込みが失敗していたら押せる状態へ戻さない
    btn.textContent = label;
  }
});

// ===== イベント =====
document.querySelectorAll('.period-bar .filterchip').forEach((b) => {
  b.addEventListener('click', () => {
    selectPeriod({ days: Number(b.dataset.days) });
  });
});
$('period-start').max = todayJst();
$('period-end').max = todayJst();
$('period-range').addEventListener('submit', (event) => {
  event.preventDefault();
  const start = $('period-start').value, end = $('period-end').value;
  const error = rangeError(start, end);
  if (error) { $('period-range-error').textContent = error; return; }
  selectPeriod({ start, end });
});

document.querySelectorAll('th.sortable').forEach((th) => {
  th.addEventListener('click', () => {
    const key = th.dataset.sort;
    if (_sortKey === key) {
      _sortDir = _sortDir === 'desc' ? 'asc' : 'desc';
    } else {
      _sortKey = key;
      _sortDir = 'desc';
    }
    applySortAndRender();
  });
});

// 行クリックで lens 内訳などを展開（委譲・見つけやすさ重視）
$('usage-tbody').addEventListener('click', (e) => {
  const row = e.target.closest('tr.u-row');
  if (!row) return;
  row.classList.toggle('u-open');
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
  _adminReady = true;
  const view = usageView();
  if (view.periodError) showPeriodError(view);
  if (!view.periodError) await load(view.period);
})();
