// 利用統計画面（admin 専用）。GET /admin/usage/stats?days= でユーザー別/全体の利用量を集計表示する。
// 狙い＝よく使うユーザーを見つけてヒアリング候補にする（本文・タイトルは API 側で一切返さない）。
// セキュリティ: server data は全て esc()。data-* 委譲でインライン handler なし。
'use strict';

// UI-TABS2（2026-09-04）: システム管理のタブから iframe（?embed=1）で開かれた時は、自ページの
// 共通トップバー/ナビを隠す（CSS 側は .embedded 修飾・usage.html の <style>）。単独 URL 直開き
// （?embed 無し）では何もしない＝この画面の機能・見た目は完全に不変。
if (new URLSearchParams(location.search).has('embed')) {
  document.documentElement.classList.add('embedded');
}

const $ = Sherpa.$, esc = Sherpa.esc, getJSON = Sherpa.getJSON, mdLite = Sherpa.mdLite, api = Sherpa.api;

const LENS_LABEL = { impact: '影響分析', qa: '仕様問い合わせ', troubleshoot: 'トラブルシュート', chat: '素の会話' };
const LENS_ORDER = ['impact', 'qa', 'troubleshoot', 'chat'];

// バッチ3（2026-07-03）: 頭脳別利用比率の表示名（brainmenu/chat.js の PROVIDERS と同じラベルに揃える）。
// 色は dataviz skill 呼び出し済み: このアプリに専用カテゴリランプが無いため、既存の semantic token
// （固定順・lens ミニバーと同じ折衷）をカテゴリ色に転用し、常設の凡例＋直接ラベルで色だけに頼らない
// （[[dataviz-skill-limited-palette]] の判断を踏襲）。
const PROVIDER_LABEL = {
  heuristic: '簡易（AIなし）', codex: 'Codex', openai: 'OpenAI API', gemini: 'Gemini',
  ollama: 'ローカルLLM (Ollama)', bedrock: 'AWS Bedrock (Claude)', unknown: '不明',
};
const PROVIDER_ORDER = ['heuristic', 'codex', 'openai', 'gemini', 'bedrock', 'ollama', 'unknown'];
const PROVIDER_COLOR = {
  heuristic: 'var(--ink-3)', codex: 'var(--accent)', openai: 'var(--ok)', gemini: 'var(--warn)',
  bedrock: 'var(--danger)', ollama: 'var(--accent-ink)', unknown: 'var(--border)',
};

let _days = 30;
let _sortKey = 'turns';
let _sortDir = 'desc';
let _users = [];
let _loadSeq = 0;   // 期間ボタン連打対策: 後着の古いレスポンスで表示が巻き戻らないようにする（RV ラウンド3 LOW）

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
  $('t-active').textContent = (totals.active_users || 0).toLocaleString('ja-JP');
  $('t-turns').textContent = (totals.turns || 0).toLocaleString('ja-JP');
  $('t-conversations').textContent = (totals.conversations || 0).toLocaleString('ja-JP');
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
// インライン SVG を素の JS で描画（vendor は cytoscape のみ＝新規チャートライブラリ追加禁止）。
// dataviz skill 準拠: 単一系列の時系列＝line（面は薄いウォッシュ）／凡例なし（タイトルが系列を示す）／
// クロスヘア＋ツールチップ＝ホバーとキーボードフォーカス両対応／軸・グリッドは recessive／
// 0日・全ゼロは明示的な空状態表示。

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

// period.start〜period.end（サーバ算出の JST 暦日範囲）で連続した日付配列へ穴埋めする（API は
// 活動があった日しか返さないため、そのまま繋ぐと空白日が圧縮されて時系列が歪む＝穴埋めして初めて
// 正しい折れ線になる）。「今日」をクライアント側で再計算しない＝API が返す日付範囲をそのまま使う
// （RV ラウンド3 MEDIUM: サーバの集計境界とフロントの描画範囲がズレると表とグラフの合計が食い違う）。
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

  // 横グリッド線（0・中間・最大の3本のみ＝recessive・hairline）＋ Y軸ラベル。
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

// ===== バッチ3（2026-07-03）: 利用の傾向（ゼロヒット率・ヒートマップ・world/頭脳別・定着・DL数） =====
// dataviz skill 呼び出し済み。ヒートマップ/world別は「magnitude」＝sequential 1色（--accent）。
// 頭脳別は「identity」＝カテゴリ色（既存 semantic token の固定順流用＋常設凡例で secondary encoding）。
// 週次アクティブ・DL日別は既存 renderTrendChart（1系列の折れ線）をそのまま再利用する。

function renderZeroHitTile(zeroHit) {
  const rate = zeroHit && zeroHit.rate;
  $('t-zerohit').textContent = (rate === null || rate === undefined) ? '—' : `${Math.round(rate * 100)}%`;
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
    const valLabel = _svgEl('text', {   // 値はバーの先端に（skill 規約: bars→value at the tip）
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
  // 常設の凡例（色だけに頼らない・skill の secondary encoding 規約）。
  $('chart-provider-legend').innerHTML = items.map((it) =>
    `<span class="item"><span class="dot" style="background:${it.color}"></span>${esc(it.label)} ${it.value}件</span>`,
  ).join('');
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
  // RV LOW（2026-07-03再検証）: 見出し脇に期間合計を表示（日別グラフだけだと合計が読み取りにくい）。
  const total = (downloads && downloads.total) || 0;
  $('dl-total-badge').textContent = `期間合計 ${total.toLocaleString('ja-JP')}件`;
}

// ===== トークン（F3・2026-07-07／2026-07-08 金額表示は撤去＝入力/出力トークン数のみ） =====
function providerLabel(p) { return PROVIDER_LABEL[p] || p || '不明'; }

// S1（2026-07-15-LLMオーケストレーション実装計画.md §3）: 用途別（kind）内訳の平文日本語ラベル。
// 未知 kind は生の kind をそのまま表示（fail-safe）。
const KIND_LABEL = {
  'chat-sub': '下調べ（サブAI）',
  research: '外部連携の調査',
  extract: 'ナレッジ抽出（旧機能）',
  propose: '概念の候補づくり（旧機能）',
  chat: '会話', intent: '意図の判定',
  embed: '検索用ベクトル化', graph_ask: 'グラフへの質問', vlm: '画像の読み取り',
  // S4-c（2026-07-15-LLMオーケストレーション実装計画.md §6.3）: 複数プロファイル自動選択の計画呼び出し。
  'chat-plan': '進め方の計画',
  usage_chat: '利用統計チャット',
  // EXT-2c: 清書前のメイン査読（根拠の十分性判定・限定ツール精読）。
  'chat-review': '根拠の査読',
  // M1（§8.6-4）: 取り込み後にバックグラウンドで後追い実行する rag.md の LLM 成形。
  rag_render: 'ナレッジの読みやすさ整形',
};
function kindLabel(k) { return KIND_LABEL[k] || k; }
// トークン列は null（プロバイダが usage を報告しなかった「報告不能」マーカー）なら「—」で表示する。
function fmtTokOrDash(v) { return (v === null || v === undefined) ? '—' : Number(v).toLocaleString('ja-JP'); }
function renderTokenKindTable(rows) {
  const card = $('token-kind-card');
  const tb = $('token-kind-tbody');
  if (!tb) return;
  if (!rows || !rows.length) {
    if (card) card.hidden = true;   // 空/不在＝旧 API 応答との前方互換でカードごと隠す
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
      <td class="num">${fmtTokOrDash(r.output)}</td>
      <td class="num">${fmtTokOrDash(r.reasoning_output)}</td>
    </tr>`;
  }).join('');
}
function renderTokenModelTable(rows) {
  const tb = $('token-model-tbody');
  if (!rows.length) {
    tb.innerHTML = '<tr class="empty-row"><td colspan="6">この期間のトークン記録はまだありません</td></tr>';
    return;
  }
  tb.innerHTML = rows.map((r) => {
    const name = `${esc(providerLabel(r.provider))}`
      + (r.model ? ` <span class="user-uid">${esc(r.model)}</span>` : '');
    return `<tr>
      <td>${name}</td>
      <td class="num">${(r.turns || 0).toLocaleString('ja-JP')}</td>
      <td class="num">${(r.input || 0).toLocaleString('ja-JP')}</td>
      <td class="num">${(r.cached_input || 0).toLocaleString('ja-JP')}</td>
      <td class="num">${(r.output || 0).toLocaleString('ja-JP')}</td>
      <td class="num">${(r.reasoning_output || 0).toLocaleString('ja-JP')}</td>
    </tr>`;
  }).join('');
}
function renderTokenUserTable(rows) {
  const tb = $('token-user-tbody');
  const top = (rows || []).slice(0, 10);   // 上位ユーザー（サーバ側でトークン降順）
  if (!top.length) {
    tb.innerHTML = '<tr class="empty-row"><td colspan="5">この期間のトークン記録はまだありません</td></tr>';
    return;
  }
  tb.innerHTML = top.map((u, i) => `<tr>
    <td><span class="${i === 0 ? 'rank top1' : 'rank'}">${i + 1}</span></td>
    <td><div class="user-name">${esc(u.display_name || u.uid)}</div><div class="user-uid">${esc(u.uid)}</div></td>
    <td class="num">${(u.turns || 0).toLocaleString('ja-JP')}</td>
    <td class="num">${(u.input || 0).toLocaleString('ja-JP')}</td>
    <td class="num">${(u.output || 0).toLocaleString('ja-JP')}</td>
  </tr>`).join('');
}
function renderTokens(tokens, period) {
  const t = tokens || {};
  const tot = t.totals || {};
  $('t-tok-input').textContent = (tot.input || 0).toLocaleString('ja-JP');
  $('t-tok-output').textContent = (tot.output || 0).toLocaleString('ja-JP');
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
      <div class="g"><b style="font-size:11.5px">${worlds}</b><span>利用フォルダ</span></div>
    </div>`;
}

function zeroHitCellHTML(u) {
  if (u.zero_hit_rate === null || u.zero_hit_rate === undefined) {
    return '<td class="num zhr-cell" title="ナレッジ参照ターンがありません">—</td>';
  }
  const pct = Math.round(u.zero_hit_rate * 100);
  const tip = `${(u.knowledge_turns || 0).toLocaleString('ja-JP')}件中${(u.zero_hit_turns || 0).toLocaleString('ja-JP')}件がゼロヒット`;
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
async function load(days) {
  const seq = ++_loadSeq;   // このリクエストの連番（連打時、最新以外の描画は破棄する）
  _days = days;
  setLoading();
  document.querySelectorAll('.period-bar .filterchip').forEach((b) => {
    b.classList.toggle('on', Number(b.dataset.days) === days);
  });
  try {
    const d = await getJSON('/admin/usage/stats?days=' + encodeURIComponent(days));
    if (seq !== _loadSeq) return;   // 後から連打された別リクエストが既に最新＝このレスポンスは古い
    renderSummary(d.totals || {});
    renderZeroHitTile(d.zero_hit || {});
    renderCharts(d.daily || [], d.period);
    renderHeatmap(d.heatmap || []);
    renderWorldBar(d.worlds || []);
    renderProviderBar(d.providers || []);
    renderWeeklyAndRetention(d.retention || {});
    renderDownloadsChart(d.downloads || {}, d.period);
    renderTokens(d.tokens || {}, d.period);
    _users = d.users || [];
    applySortAndRender();
  } catch (e) {
    if (seq !== _loadSeq) return;
    $('usage-tbody').setAttribute('aria-busy', 'false');
    $('usage-tbody').innerHTML = `<tr><td colspan="8" style="color:var(--danger);padding:16px">読み込みに失敗しました: ${esc(String(e))}</td></tr>`;
    toast('利用統計の読み込みに失敗しました');
  }
}

// ===== イベント =====
document.querySelectorAll('.period-bar .filterchip').forEach((b) => {
  b.addEventListener('click', () => load(Number(b.dataset.days)));
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

// ===== 統計チャット（POST /admin/usage/chat・本文はページ内メモリのみ・サーバに永続化しない） =====
// サーバ側の会話履歴の上限（`sherpa/usage_chat.py::HISTORY_MAX_ITEMS`/`HISTORY_ITEM_MAX_LEN`）と
// 合わせておく。1件の長さはサーバ側も超過分を切り詰めて受理する（拒否しない）が、ここでも
// 送信前に切り詰める＝長い正常回答（AIの答え）がそのまま積まれて次のターン以降ずっと同じ
// エラーを出し続ける、ということが起きないようにする（利用者の再読込に頼らない）。
const UC_HISTORY_MAX = 20;
const UC_HISTORY_ITEM_MAX_LEN = 4000;
// サーバ（`usage_chat.py::_TRUNCATION_SUFFIX`）と同じ印。無言で末尾を落とさない。
const UC_TRUNCATION_SUFFIX = '…（省略）';
// `String.length`/`slice()` は UTF-16 コード単位（サロゲート対の絵文字等は2として数える）だが、
// サーバ側（Python）の `len()`/スライスはコードポイント単位。単位が食い違うと同じ文字列でも
// 双方で切り詰め位置がズレ、UTF-16 単位でのそのままの `slice` はサロゲート対の片割れだけを
// 残して文字列を壊しうる。`Array.from(s)` はコードポイント単位で反復するため、ここで揃える。
function ucClip(s) {
  s = String(s == null ? '' : s);
  const cps = Array.from(s);
  if (cps.length <= UC_HISTORY_ITEM_MAX_LEN) return { text: s, clipped: false };
  const text = cps.slice(0, UC_HISTORY_ITEM_MAX_LEN - UC_TRUNCATION_SUFFIX.length).join('') + UC_TRUNCATION_SUFFIX;
  return { text, clipped: true };
}
let _ucHistory = [];
let _ucSending = false;
// STAT-2: 「次の送信先」（provider を省略して送った場合に使われる見込みの値）。GET
// /admin/settings の usage_chat.effective・および既定送信の応答（provider_used）で更新する。
// この画面から専用設定そのものを変更する導線は無い（変更は管理画面）が、「今回だけ」トグルに
// よるリクエスト単位の一時切替は持つ（`_ucProviderOverride`・保存しない）。取得できるまで
// （またはできなかった場合）'openai' 等の未確認の値を絶対に表示しない・送信もさせない
// （`null`＝未確認）。
let _ucDefaultProvider = null;
// openai 接続先の種別（`openai_endpoint.effective.kind`＝"openai"|"azure"|"custom"）。
// **GET /admin/settings でのみ更新する**（チャット応答の `endpoint_kind` では上書きしない）
// ——応答の `endpoint_kind` は「その1回の送信で実際に使った接続先」（ollama 使用時は
// 常に `null`）であり、既定 ollama の送信結果でこの値を書き換えると、その後「今回だけ
// openai」を選んだ時に本来の openai 接続先種別（Azure/custom 等）が失われ「OpenAI」と
// 誤表示する。openai が実際には Azure/その他 OpenAI 互換エンドポイントへ
// 向いている場合、送信先表示を「OpenAI」のままにすると実態と異なる（Azure 等へ送っているのに
// OpenAI 社へ送っていると誤解させる）ため区別する。
let _ucOpenaiEndpointKind = null;
// A7（`cloud_provider`）の現在値。GET /admin/settings でのみ更新する（`null`＝未確認）。
// A7 が openai 以外だと、A7 の排他選択契約（非選択クラウドのキーは使わない）により
// 「今回だけ OpenAI」は中央 OpenAI キーが使えず 503（未接続）になる——挙動自体は契約どおりだが、
// 理由が分かるよう「今回だけ OpenAI」ボタンの近くに注記を出すために使う
// （`ucUpdateOpenaiKeyHint` 参照）。
let _ucCloudProvider = null;
// A7 の値ラベル（admin-settings.js::CLOUD_PROVIDER_LABELS の簡略版・この画面専用）。
const UC_CLOUD_PROVIDER_LABELS = { openai: 'OpenAI', gemini: 'Gemini（Google）', bedrock: 'AWS Bedrock (Claude)' };
// 画面の「今回だけ」トグル（リクエスト単位の一時上書き・保存しない）。null＝上書きなし（既定に従う）。
let _ucProviderOverride = null;
// 直近の GET /admin/settings で usage_chat.effective が有効だったか（既定送信の可否を決める）。
let _ucSettingsReady = false;
// 直近の GET /admin/settings 自体が成功したか（usage_chat.effective の妥当性とは無関係）。
// openai 接続先種別（`_ucOpenaiEndpointKind`）が最新かどうかの判定に使う——「今回だけ」上書き
// 送信は usage_chat.effective の妥当性に左右されず、この値が true であることだけを要求する
// （既定送信の可否＝`_ucSettingsReady` とは別の関門）。
let _ucSettingsFetchOk = false;
// 「次の送信先」欄に出す明示エラー文言（null＝正常）。取得失敗（ネットワーク/応答形式不正）と、
// 「保存値は取得できたが不正（既定送信の送信先が確定できない）」を別の文言で区別する。
// どちらも `_ucSettingsReady=false`（既定送信を止め「今回だけ」のみ許可）。「前回の送信先」
// （`_ucLastSentProvider` 系・別要素）はこの状態と独立に保つ——直前に実際へ送った事実と、
// 次に何が起きるかの見込みを混ぜない。
let _ucNoticeError = null;
// 「前回の送信先」（直近に完了した送信で実際に使われた provider/endpoint_kind）。「次の送信先」
// （上記・現在の選択に基づく見込み）とは**別状態・別表示**にする——同じ1行に混ぜると、送信中に
// トグルを変えた場合や既定送信の応答到着が遅れた場合に、画面がどちらの情報を出しているか
// 予測できず、「表示は前回の送信先のままなのに、次に実際に送られる先は現在の選択」という
// 食い違いが起き得る。`null`＝まだ送信していない。
let _ucLastSentProvider = null;
let _ucLastSentEndpointKind = null;

// STAT-2: この画面専用のラベル（`PROVIDER_LABEL`＝頭脳別利用比率チャート用の全プロバイダ一覧とは
// 別物・「ローカルLLM」のような専門寄りの表現を混ぜない・平文原則）。Azure/custom（OpenAI 互換の
// 別エンドポイント）は「OpenAI」ではなく「クラウド（OpenAI 互換）」と表示し、実際の送信先の
// 実態（OpenAI 社そのものではない）を隠さない。
function ucProviderLabel(provider, endpointKind) {
  if (provider === 'ollama') return 'ローカル（Ollama）';
  if (provider === 'openai') {
    return (endpointKind === 'azure' || endpointKind === 'custom') ? 'クラウド（OpenAI 互換）' : 'OpenAI';
  }
  return provider || '';
}

// 「次の送信先」（現在の選択＝上書き中ならその値・無ければ既定に基づく見込み）。上書き中は
// 現在の選択を必ず優先する——設定取得の失敗/不正状態でも、明示した上書きは無視しない
// （エラー案内は `ucUpdateSettingsErrorNote` の別行に併記し、消さない）。
function ucUpdateProviderNote() {
  const el = $('usage-chat-provider-note');
  if (el) {
    const provider = _ucProviderOverride || _ucDefaultProvider;
    if (provider) {
      el.classList.remove('uc-error');
      el.textContent = '送信先: ' + ucProviderLabel(provider, _ucOpenaiEndpointKind);
    } else if (_ucNoticeError) {
      el.classList.add('uc-error');
      el.textContent = _ucNoticeError;
    } else {
      el.classList.remove('uc-error');
      el.textContent = '確認中…';   // 取得前は未確認の値を出さない
    }
  }
  ucUpdateSettingsErrorNote();
}

// 設定取得エラーの別行案内。「次の送信先」（上記）が上書き選択中でエラー文言を表示できない
// 間も、エラー自体は消さずに見える状態を保つ（上書きが無い時は「次の送信先」欄が既にエラーを
// 表示しているため、二重に出さない）。
function ucUpdateSettingsErrorNote() {
  const el = $('usage-chat-settings-error-note');
  if (!el) return;
  const showHere = !!(_ucNoticeError && _ucProviderOverride);
  el.hidden = !showHere;
  el.textContent = showHere ? _ucNoticeError : '';
}

// A7（cloud_provider）が openai 以外の間、「今回だけ OpenAI」ボタンの近くに、中央 OpenAI
// キーが使えない理由を注記する（A7 の排他選択契約＝非選択クラウドのキーは使わない・
// `sherpa/usage_chat.py::_resolve_cfg` が honest failure（503）にする挙動と、
// admin-settings.js の「OpenAI に固定」ラジオの注記に揃える）。A7 が未確認（`null`）の間は
// 出さない（確認できていないことを誤って断定しない）。選択中かどうかに関わらず常に出す
// （選ぶ前に理由が分かるようにするため）。
function ucUpdateOpenaiKeyHint() {
  const el = $('usage-chat-openai-key-hint');
  if (!el) return;
  const show = _ucCloudProvider != null && _ucCloudProvider !== 'openai';
  el.hidden = !show;
  el.textContent = show
    ? `OpenAI のキーは実行構成が OpenAI のときだけ使えます`
      + `（現在: ${UC_CLOUD_PROVIDER_LABELS[_ucCloudProvider] || _ucCloudProvider}）。`
    : '';
}

// 「前回の送信先」（直近に完了した送信の確定値・既定/「今回だけ」上書きのどちらの送信でも
// 更新する）。`_ucNoticeError`（「次の送信先」欄の状態）は一切参照/変更しない——「次の送信先」が
// 確認中/エラーであっても、直前に実際へ送った事実の表示は独立して正しく保つ。
function ucUpdateLastSentNote() {
  const el = $('usage-chat-last-sent-note');
  if (!el) return;
  el.textContent = (_ucLastSentProvider == null)
    ? '前回の送信先: （まだ送信していません）'
    : '前回の送信先: ' + ucProviderLabel(_ucLastSentProvider, _ucLastSentEndpointKind);
}
function ucRecordLastSentProvider(provider, endpointKind) {
  _ucLastSentProvider = provider;
  _ucLastSentEndpointKind = endpointKind;
  ucUpdateLastSentNote();
}

// 送信可否＝「今回だけ」上書きが選ばれているか、管理設定の取得に成功しているかのどちらか
// （どちらも無ければ送信先が分からないため送信させない）。送信中は常に無効。
function ucUpdateSendAvailability() {
  const btn = $('usage-chat-send');
  if (btn) btn.disabled = _ucSending || (!_ucProviderOverride && !_ucSettingsReady);
}

// 初期読み込みと送信直前の再取得が並行して in-flight になり得る（例: ページ読み込み直後に
// 即座に送信ボタンを押す）。`await` の間に呼び出しが重なると、後から開始した呼び出しの
// 応答が先に返り、その後で古い呼び出しの応答が遅れて返って新しい状態を上書きしてしまう
// レースがあり得る——世代番号で「自分より新しい呼び出しが既に始まっているか」を判定し、
// 追い越された（自分より新しい世代が既に始まっている）応答は状態を変えずに捨てる。
let _ucSettingsGeneration = 0;

// GET /admin/settings を読み、「次の送信先」表示に使う usage_chat.effective/openai_endpoint
// を更新する。初期化時と、送信の直前（既定/「今回だけ」上書きのどちらも・他セッションによる
// 設定変更との食い違い防止）の両方から呼ぶ。
async function ucLoadSettings() {
  const myGen = ++_ucSettingsGeneration;
  try {
    const settingsView = await getJSON('/admin/settings');
    if (myGen !== _ucSettingsGeneration) return;   // 追い越された＝この応答は捨てる
    const uc = settingsView.usage_chat;
    if (!uc || typeof uc.effective !== 'string' || !Array.isArray(uc.providers)) {
      throw new Error('usage_chat が応答に含まれていないか形式が不正です');
    }
    // openai 接続先種別は usage_chat.effective の妥当性とは無関係な別設定
    // （openai_endpoint_kind/openai_base_url 由来）——usage_chat.effective が不正な間も
    // 「今回だけ openai」の送信では引き続き必要になるため、妥当性チェックより先に読む
    // （後段で早期 return しても、この代入を素通りさせない）。
    const oe = settingsView.openai_endpoint && settingsView.openai_endpoint.effective;
    _ucOpenaiEndpointKind = (oe && oe.kind) || null;
    // A7（cloud_provider）は usage_chat.effective の妥当性とは無関係な別設定——
    // `_ucOpenaiEndpointKind` と同じ理由で妥当性チェックより先に読む。
    _ucCloudProvider = (settingsView.cloud && settingsView.cloud.provider) || null;
    _ucSettingsFetchOk = true;
    if (!uc.providers.includes(uc.effective)) {
      // 保存値は取得できたが不正（"(不正な保存値)" 等・選択肢に無い）＝既定送信の送信先が
      // 確定できない。既定送信は止め、「今回だけ」の明示指定のみ許可する（黙って選択肢の
      // どれかへ丸めたり、既定 openai として送信したりしない）。
      _ucDefaultProvider = null;
      _ucSettingsReady = false;
      _ucNoticeError = '既定の AI 設定が不正です。「今回だけ」で AI を選んで送信してください。';
      ucUpdateProviderNote();
      ucUpdateSendAvailability();
      ucUpdateOpenaiKeyHint();
      return;
    }
    _ucDefaultProvider = uc.effective;
    _ucSettingsReady = true;
    _ucNoticeError = null;
  } catch (e) {
    if (myGen !== _ucSettingsGeneration) return;   // 追い越された＝この応答（エラー）は捨てる
    // 初回取得の成功後に送信直前の再取得が失敗した場合、`_ucDefaultProvider` を残したままだと
    // override なしの「次の送信先」欄が（エラーではなく）古い既定値をそのまま表示し続けて
    // しまう——POST 自体は `_ucSettingsReady=false` で止まるが、表示は誤って「送信できる」
    // ように見える。既定送信の表示状態は他の失敗経路（保存値不正）と同じく丸ごと破棄する。
    _ucDefaultProvider = null;
    _ucSettingsReady = false;
    _ucSettingsFetchOk = false;
    _ucOpenaiEndpointKind = null;
    _ucCloudProvider = null;
    _ucNoticeError = '送信先を取得できませんでした（再読み込みしてください）';
  }
  ucUpdateProviderNote();
  ucUpdateSendAvailability();
  ucUpdateOpenaiKeyHint();
}

function ucSetProviderOverride(p) {
  _ucProviderOverride = p || null;
  document.querySelectorAll('#usage-chat-provider-toggle .uc-provider-btn').forEach((b) => {
    b.setAttribute('aria-pressed', String(b.dataset.ucProvider === (_ucProviderOverride || '')));
  });
  ucUpdateProviderNote();
  ucUpdateSendAvailability();
}

function ucAppendUser(text) {
  const wrap = $('usage-chat-messages'); if (!wrap) return;
  const d = document.createElement('div'); d.className = 'msg user';
  d.innerHTML = `<div style="display:flex;justify-content:flex-end">`
    + `<div class="bubble-user" style="max-width:78%">${esc(text)}</div></div>`;
  wrap.appendChild(d); wrap.scrollTop = wrap.scrollHeight;
}
function ucAppendAssistant(innerHtml) {
  const wrap = $('usage-chat-messages'); if (!wrap) return null;
  const d = document.createElement('div'); d.className = 'msg';
  d.innerHTML = `<div class="a-row"><div class="a-avatar">S</div><div class="a-body">${innerHtml}</div></div>`;
  wrap.appendChild(d); wrap.scrollTop = wrap.scrollHeight;
  return d;
}
async function ucSend() {
  const ta = $('usage-chat-input');
  const q = (ta && ta.value || '').trim();
  // 上書き選択が無いのに設定を取得できていない場合は送信させない（ボタンは無効化されている
  // はずだが、Enter キー送信はボタンの disabled を経由しないため、ここでも防御する）。
  if (!q || _ucSending || (!_ucProviderOverride && !_ucSettingsReady)) return;
  // この送信で実際に使う一時上書き値をここで確定させる。以降このリクエストに関する判定
  // （再取得の要否・body.provider・応答の反映先）は全てこの値を使う——可変な
  // `_ucProviderOverride` を応答到着後に読み直すと、送信中にトグルを操作された場合に
  // 「実際に送ったのとは違う値」を参照してしまう。
  const overrideForThisSend = _ucProviderOverride;
  ta.value = '';
  ucAppendUser(q);
  const placeholder = ucAppendAssistant(
    '<span class="loading-inline"><span class="spinner spinner-sm"></span><span>考えています...</span></span>');
  _ucSending = true;
  ucUpdateSendAvailability();
  try {
    // STAT-2: 既定/「今回だけ」上書きのどちらの送信でも、送信直前に設定を再取得する。
    // 既定送信は表示中の「送信先」が最新の管理設定と一致していることの再確認（ページ読み込み後に
    // 他セッションが usage_chat_provider を変更している食い違いを防ぐ）。上書き送信も、openai
    // 接続先種別（`_ucOpenaiEndpointKind`＝Azure/custom 等）を送信前に確定させるために必要
    // （usage_chat.effective 自体の妥当性とは無関係な別設定のため、上書き送信の可否は
    // `_ucSettingsFetchOk`＝再取得自体の成否だけで判定し、`_ucSettingsReady`＝既定の送信先が
    // 有効かどうかは問わない）。
    await ucLoadSettings();
    if (!overrideForThisSend && !_ucSettingsReady) {
      throw new Error('送信先の設定を確認できなかったため送信を中止しました。再読み込みしてください。');
    }
    if (overrideForThisSend && !_ucSettingsFetchOk) {
      throw new Error('接続先の設定を確認できなかったため送信を中止しました。再読み込みしてください。');
    }
    const body = { question: q, history: _ucHistory };
    if (overrideForThisSend) body.provider = overrideForThisSend;
    const d = await api('POST', '/admin/usage/chat', body);
    if (!overrideForThisSend) {
      // provider を省略した送信だけ、応答の確定 provider で「次の送信先」用の既定値を
      // 更新する——送信前の表示は「予定」であり、GET と POST の間に他セッションが専用設定を
      // 変更した競合を、応答時点の確定値で吸収する。「今回だけ」上書きの送信は、上書き自体が
      // その1回の送信先を確定させているため、ここで既定側の状態を書き換えない（上書き解除後の
      // 「次の送信先」表示が直前の一時的な選択で汚染されるのを防ぐ）。openai 接続先の種別
      // （`_ucOpenaiEndpointKind`）はここでは更新しない——GET /admin/settings 由来の別状態
      // であり、この応答の `endpoint_kind`（ollama 使用時は常に `null`）で上書きすると
      // 無関係な情報が混ざる。
      _ucDefaultProvider = d.provider_used;
      ucUpdateProviderNote();
    }
    // 「前回の送信先」は「次の送信先」とは別状態・別表示で、既定/「今回だけ」上書きのどちらの
    // 送信でも独立して更新する。
    ucRecordLastSentProvider(d.provider_used, d.endpoint_kind);
    const qClip = ucClip(q), ansClip = ucClip(d.answer);
    // 表示は切り詰めない（全文を見せる）。会話の記憶（次回送信する history）だけを切り詰める
    // ため、それが起きたことは画面上にも一言添える（無言で記憶が欠けたように見せない）。
    let noteHtml = ansClip.clipped
      ? '<div class="uc-hint">（この回答は長いため、次の質問への引き継ぎでは一部だけ記憶します）</div>' : '';
    // サーバ側の notes（例: 改善ログの要約を取得できなかった旨）も同じ枠でそのまま見せる
    // （黙って回答だけ返すと、参照データが欠けていたことに利用者が気付けない）。
    (d.notes || []).forEach((n) => { noteHtml += `<div class="uc-hint">（${esc(n)}）</div>`; });
    if (placeholder) placeholder.querySelector('.a-body').innerHTML = `<div class="headline">${mdLite(d.answer)}</div>${noteHtml}`;
    // 切り詰め済みの本文（省略印付き）をそのまま送る＝サーバ側も省略印の有無を切り詰めの
    // 証拠として扱うため（4000字ちょうどに切った文字列は「超過」ではなくなり、サーバの
    // 素朴な長さ比較だけでは切り詰めが起きた事実が監査に残らない・usage_chat.py 参照）。
    _ucHistory.push({ role: 'user', content: qClip.text }, { role: 'assistant', content: ansClip.text });
    if (_ucHistory.length > UC_HISTORY_MAX) _ucHistory = _ucHistory.slice(-UC_HISTORY_MAX);
  } catch (e) {
    // 502（実送信を試みたが失敗＝実際に使った送信先は確定している）だけ「前回の送信先」を
    // 応答の provider_used/endpoint_kind で更新する。503（送信前に拒否・未送信）やその他の
    // エラーでは更新しない——未送信なのに「前回の送信先」を書き換えると、実際には送って
    // いないのに送信結果があったかのように見えてしまう（`common.js::api` が非2xx応答の
    // JSON 本文を `err.status`/`err.body` として渡す契約を利用する）。
    if (e && e.status === 502 && e.body && e.body.provider_used) {
      ucRecordLastSentProvider(e.body.provider_used, e.body.endpoint_kind);
    }
    if (placeholder) {
      placeholder.querySelector('.a-body').innerHTML =
        `<div class="headline uc-error">${esc(String((e && e.message) || e))}</div>`;
    }
  } finally {
    _ucSending = false;
    ucUpdateSendAvailability();
    if (ta) ta.focus();
  }
}
const ucSendBtn = $('usage-chat-send');
if (ucSendBtn) ucSendBtn.addEventListener('click', ucSend);
const ucInput = $('usage-chat-input');
if (ucInput) {
  ucInput.addEventListener('keydown', (e) => {
    // IME変換中の確定 Enter では送信しない（e.isComposing が使えないブラウザ/IME の組み合わせ
    // に備え、変換確定イベントの伝統的な合図 keyCode===229 も合わせて見る）。
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ucSend(); }
  });
}
const ucProviderToggle = $('usage-chat-provider-toggle');
if (ucProviderToggle) {
  ucProviderToggle.addEventListener('click', (e) => {
    const btn = e.target.closest('.uc-provider-btn');
    if (!btn) return;
    ucSetProviderOverride(btn.dataset.ucProvider || null);
  });
}

// ===== 初期化 =====
ucUpdateProviderNote();       // 「確認中…」を即座に反映（'openai' 等の未確認の値は出さない）
ucUpdateLastSentNote();       // 「（まだ送信していません）」を即座に反映
ucUpdateSendAvailability();   // 設定取得前は送信不可
ucUpdateOpenaiKeyHint();      // A7 未確認のうちは非表示
(async () => {
  const isAdmin = await checkAdmin();
  if (!isAdmin) {
    const main = $('main-content');
    const denied = $('access-denied');
    if (main) main.style.display = 'none';
    if (denied) denied.style.display = 'block';
    return;
  }
  await ucLoadSettings();
  await load(_days);
})();
