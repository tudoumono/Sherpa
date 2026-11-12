// 利用統計画面の共通描画（サマリ・lens 棒・日別トレンドグラフ）。usage.js の後に読む。
'use strict';

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

