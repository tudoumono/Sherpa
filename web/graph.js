// ナレッジグラフ可視化（cytoscape）。管理用の関係検索とグラフ質問を含む。
'use strict';
const $ = Sherpa.$, esc = Sherpa.esc, getJSON = Sherpa.getJSON;     // 共通ユーティリティ（nav.js・RV DRY）

const COLOR = {
  Module: '#0d9488', Copybook: '#0891b2', DataItem: '#64748b',
  Batch: '#ea580c', Document: '#16a34a', Table: '#0e7490', Config: '#a855f7',
};
const TYPE_JA = {
  Module: 'プログラム', Copybook: 'コピーブック', DataItem: '項目',
  Document: '文書', Batch: 'バッチ', Table: 'テーブル', Config: '設定',
};
const FIELD_LABEL = {
  category: 'カテゴリ', phase: '工程', role: '種別', top_scope: '最上位フォルダ', status: '状態',
};
let cy = null, _world = null, _fullGraph = null, _truncated = false;   // _truncated=表示中は主要ノードのみ（全体ではない）
let _countText = '', _overviewPositions = null;

function setGraphLoading(on, message, error) {
  const main = document.querySelector('.graphmain');
  const el = $('graph-loading');
  if (main) main.setAttribute('aria-busy', on ? 'true' : 'false');
  if (!el) return;
  if (!on) { el.hidden = true; return; }
  el.hidden = false;
  if (error) {
    el.innerHTML = `<div style="text-align:center"><div style="font-weight:700;color:var(--danger);margin-bottom:6px">${esc(message)}</div>`
      + '<button class="btn-secondary" id="graph-retry" type="button">再試行</button></div>';
  } else {
    el.innerHTML = `<span class="spinner"></span><span>${esc(message || 'ナレッジグラフを読み込んでいます...')}</span>`;
  }
}

function styleFor(dark) {
  const labelColor = dark ? '#e6edf3' : '#1f2937';
  const outline = dark ? '#0f1419' : '#ffffff';
  // 未選択の関係線は背景に対して 3:1 以上（非テキストの目安）。関係名は選択時にしか出ないので線だけで向きが読めること。
  const edge = dark ? '#526577' : '#7c8b99';
  return [
    { selector: 'node', style: {
      'background-color': 'data(color)',
      'label': 'data(label)', 'font-size': 14, 'font-family': getComputedStyle(document.body).fontFamily,
      'min-zoomed-font-size': 11, 'color': labelColor, 'width': 28, 'height': 28,
      'text-valign': 'bottom', 'text-margin-y': 6, 'border-width': 2, 'border-color': outline,
      'text-max-width': 160, 'text-wrap': 'ellipsis',
    } },
    { selector: 'node[status="deprecated"]', style: { 'opacity': 0.45, 'border-style': 'dashed' } },
    { selector: 'node[status="hidden_candidate"]', style: { 'opacity': 0.55 } },
    { selector: 'edge', style: {
      'width': 1.4, 'line-color': edge, 'target-arrow-color': edge, 'target-arrow-shape': 'triangle',
      'curve-style': 'straight', 'arrow-scale': 0.8, 'label': '', 'font-size': 12,
      'min-zoomed-font-size': 10, 'color': labelColor, 'text-rotation': 'autorotate',
    } },
    { selector: 'edge[?curved]', style: { 'curve-style': 'bezier' } },
    { selector: 'node.dim', style: { 'background-color': dark ? '#293540' : '#dce4ec', 'text-opacity': 0 } },
    { selector: 'edge.dim', style: { 'line-color': dark ? '#293540' : '#e0e7ee',
      'target-arrow-color': dark ? '#293540' : '#e0e7ee' } },
    { selector: 'node.hi', style: { 'border-color': '#0d9488', 'border-width': 4, 'min-zoomed-font-size': 0 } },
    { selector: 'node.near, node.hi', style: { 'text-outline-width': 2, 'text-outline-color': outline } },
    { selector: 'edge.related', style: { 'label': 'data(label)', 'width': 2,
      'line-color': dark ? '#9fb0c0' : '#60788b', 'target-arrow-color': dark ? '#9fb0c0' : '#60788b',
      'text-background-color': outline, 'text-background-opacity': 1, 'text-background-padding': 3 } },
    { selector: '.filtered, .outside', style: { 'display': 'none' } },
  ];
}

// 反復計算・ランダムな移動を避け、つながりが多い要素を中心に一度だけ配置する。
// concentric は各円の件数に合わせて半径を広げるため、枝が多くてもノードが重ならない。
function graphLayout() {
  return { name: 'concentric', animate: false, padding: 40, minNodeSpacing: 36,
    concentric: (n) => n.degree(), levelWidth: () => 1, nodeDimensionsIncludeLabels: false };
}

// ── 段階読み込み＋ETag キャッシュ（②graph 軽量化 2026-07-08）──
// 都度の全件転送を避ける: 初期は主要ノード（サーバ既定の limit）だけを取り、「すべて表示」で
// limit=0（全件）を明示取得。内容が変わっていなければ ETag 一致で 304＝localStorage の JSON を再利用。
const GRAPH_CACHE_KEY = 'sherpa-graph-cache';
const GRAPH_CACHE_MAX = 3_000_000;   // localStorage 肥大防止（超過は保存せず 304 最適化のみ諦める）
const graphToken = (limitParam) => (limitParam === 0 ? 'all' : 'default');

function readGraphCache(world, token) {
  try {
    const c = JSON.parse(localStorage.getItem(GRAPH_CACHE_KEY) || 'null');
    if (c && c.world === world && c.token === token && c.etag && c.data) return c;
  } catch (e) { }
  return null;
}

function writeGraphCache(world, token, etag, data) {
  try {
    const raw = JSON.stringify({ world, token, etag, data });   // 1 world+token 分のみ保持（後勝ちで上書き）
    if (raw.length > GRAPH_CACHE_MAX) { localStorage.removeItem(GRAPH_CACHE_KEY); return; }
    localStorage.setItem(GRAPH_CACHE_KEY, raw);
  } catch (e) { try { localStorage.removeItem(GRAPH_CACHE_KEY); } catch (e2) { } }
}

async function fetchGraph(limitParam) {
  const token = graphToken(limitParam);
  const cached = readGraphCache(_world, token);
  const qs = new URLSearchParams({ world: _world });
  if (limitParam != null) qs.set('limit', String(limitParam));   // null=サーバ既定 limit（主要ノード）
  const headers = cached ? { 'If-None-Match': cached.etag } : {};
  const r = await fetch('/graph?' + qs.toString(), { headers, cache: 'no-store' });
  if (r.status === 304 && cached) return cached.data;            // 未変更＝キャッシュ再利用（再転送なし）
  if (!r.ok) throw new Error('グラフデータを取得できませんでした');
  const g = await r.json();
  const etag = r.headers.get('ETag');
  if (etag) writeGraphCache(_world, token, etag, g);
  return g;
}

async function load() {
  // 登録済みの資料フォルダを基準に表示（未登録なら空＝サンプル v1 を勝手に出さない）。
  setGraphLoading(true, 'ナレッジグラフを読み込んでいます...');
  try {
    const wr = await fetch('/worlds');
    if (!wr.ok) throw new Error('資料フォルダ一覧を取得できませんでした');
    const ws = ((await wr.json()).worlds) || [];
    if (!ws.length) {
      $('gcount').textContent = '資料フォルダ未登録（「資料フォルダ」画面で登録してください）';
      setGraphLoading(false);
      return;
    }
    _world = ws[0].world_id;
    const g = await fetchGraph(null);            // 初期＝主要ノードのみ（サーバ既定 limit）
    _fullGraph = g;
    renderGraph(g);
    await loadFacets(g);
    setGraphLoading(false);
  } catch (e) {
    $('gcount').textContent = '読み込みに失敗しました';
    setGraphLoading(true, 'ナレッジグラフの読み込みに失敗しました', true);
  }
}

async function showAllNodes() {
  // 「すべて表示」＝主要ノード上限を外して全件取得（数が多いと重くなることがある操作なので明示）。
  if (!_world) return;
  $('showall').disabled = true;
  setGraphLoading(true, 'すべてのつながりを読み込んでいます...');
  try {
    const g = await fetchGraph(0);               // limit=0＝全件
    _fullGraph = g;
    renderGraph(g);
    setGraphLoading(false);
  } catch (e) {
    setGraphLoading(false);
    $('gcount').textContent = '読み込みに失敗しました';
  } finally {
    $('showall').disabled = false;
  }
}

function emptyGraph(g) {
  const docs = (g.counts && g.counts.documents) || 0;
  $('gcount').textContent = `関係グラフは空（文書 ${docs} 件は取り込み済み）`;
  $('cy').innerHTML = '<div style="padding:48px 24px;text-align:center;max-width:560px;margin:0 auto;'
    + 'line-height:1.7;color:var(--ink-3,#8b95a3)">'
    + '<div style="font-size:var(--text-body);font-weight:600;color:var(--ink,#1f2937);margin-bottom:8px">関係グラフはまだありません</div>'
    + '関係グラフは<b>ソースコード（COBOL/JCL/コピーブック）</b>から作られます。'
    + 'このフォルダが文書（Office/テキスト）のみの場合、関係グラフは空のままです（想定どおりです）。'
    + '文書そのものは取り込み済みで、検索からも使えます。</div>';
}

function renderGraph(g, searched) {
  if (cy) { cy.destroy(); cy = null; }
  _overviewPositions = null;
  clearSelection();
  $('cy').innerHTML = '';
  $('gsearch').value = '';
  $('gresults-section').hidden = true;
  _truncated = !searched && !!g.truncated;
  $('greset').hidden = !searched;
  clearFilter(false);
  $('showall').hidden = true;                     // 既定は隠す（主要ノードで切れている時だけ下で出す）
  if (!g.nodes.length) {
    renderLegend([]);
    if (searched) {
      $('gcount').textContent = '検索結果 0件';
      $('cy').innerHTML = '<div class="gempty">一致するグラフ要素はありません</div>';
      $('greset').hidden = false;
      return;
    }
    emptyGraph(g);
    return;
  }
  if (searched) {
    $('gcount').textContent = `検索結果 ノード ${g.nodes.length}・関係 ${g.edges.length}`;
  } else if (g.truncated) {                        // 主要ノードのみ＝残りは「すべて表示」で辿る（専門用語ゼロ）
    $('gcount').textContent = `主要な ${g.nodes.length} 件を表示しています（全 ${g.total_nodes} 件）`;
    $('showall').hidden = false;
  } else {
    $('gcount').textContent = `ノード ${g.nodes.length}・関係 ${g.edges.length}`;
  }
  _countText = $('gcount').textContent;
  // 並行する関係と自己参照は曲線で分け、それ以外は描画負荷の小さい直線にする。
  const pairKey = (e) => JSON.stringify([e.source, e.target].sort());
  const pairs = new Map();
  g.edges.forEach((e) => { const key = pairKey(e); pairs.set(key, (pairs.get(key) || 0) + 1); });
  const els = [
    ...g.nodes.map((n) => ({ data: {
      id: n.id, label: n.name, color: COLOR[n.type] || '#64748b',
      type: n.type, type_ja: n.type_ja || TYPE_JA[n.type] || n.type,
      status: n.status, value: n.value, parent: n.parent, category: n.category,
      phase: n.phase, top_scope: n.top_scope, path: n.path,
    } })),
    ...g.edges.map((e) => ({ data: { source: e.source, target: e.target, label: e.type, status: e.status,
      curved: e.source === e.target || pairs.get(pairKey(e)) > 1 } })),
  ];
  cy = cytoscape({
    container: $('cy'), elements: els, style: styleFor(document.documentElement.dataset.theme === 'dark'),
    layout: graphLayout(),
    wheelSensitivity: 0.2, maxZoom: 2.5, minZoom: 0.01,
  });
  renderLegend(g.nodes);
  cy.on('tap', 'node', (evt) => focusNode(evt.target));
  cy.on('tap', (evt) => { if (evt.target === cy) clearSelection(); });
}

async function loadFacets(g) {
  let rels = [...new Set((g.edges || []).map((e) => e.type))].sort();
  let fields = Object.keys(FIELD_LABEL);
  try {
    const f = await (await fetch('/graph/facets')).json();
    rels = (f.relationship_types || rels).slice().sort();
    fields = (f.condition_fields || fields).filter((x) => FIELD_LABEL[x]);
  } catch (e) { }
  $('relfilter').innerHTML = '<option value="">関係</option>'
    + rels.map((r) => `<option value="${esc(r)}">${esc(r)}</option>`).join('');
  $('fieldfilter').innerHTML = '<option value="">条件</option>'
    + fields.map((f) => `<option value="${esc(f)}">${esc(FIELD_LABEL[f] || f)}</option>`).join('');
}

function renderLegend(nodes) {
  const counts = new Map();
  nodes.forEach((n) => counts.set(n.type, (counts.get(n.type) || 0) + 1));
  const types = [...new Set(nodes.map((n) => n.type))].sort((a, b) => (TYPE_JA[a] || a).localeCompare(TYPE_JA[b] || b, 'ja'));
  $('legtypes').innerHTML = types.map((t) =>
    `<button type="button" class="legrow ftog" data-ftype="${esc(t)}" aria-pressed="true"><span class="legdot" style="background:${COLOR[t] || '#64748b'}"></span>${esc(TYPE_JA[t] || t)}<span class="legcount">${counts.get(t)}</span></button>`).join('');
}

// ── 絞り込み（種別 / 状態。cytoscape の display で適用）──
const filt = { types: new Set(), hideDep: false };

function applyFilter() {
  if (!cy) return;
  cy.batch(() => {
    cy.nodes().forEach((n) => {
      const st = n.data('status');
      const off = filt.types.has(n.data('type')) || (filt.hideDep && st && st !== 'active');
      n.toggleClass('filtered', !!off);
    });
    cy.edges().forEach((e) => {
      e.toggleClass('filtered', e.source().hasClass('filtered') || e.target().hasClass('filtered'));
    });
  });
  search($('gsearch').value);
  const active = filt.types.size || filt.hideDep;
  $('fclear').hidden = !active;
}

function toggleRow(el, on) {
  el.classList.toggle('off', !!el.dataset.ftype && on);
  el.setAttribute('aria-pressed', String(el.dataset.ftype ? !on : on));
}

function onLegendClick(e) {
  const row = e.target.closest('.ftog'); if (!row) return;
  if (row.dataset.ftype) { const t = row.dataset.ftype; filt.types.has(t) ? filt.types.delete(t) : filt.types.add(t); toggleRow(row, filt.types.has(t)); }
  else if (row.dataset.fdep) { filt.hideDep = !filt.hideDep; toggleRow(row, filt.hideDep); }
  applyFilter();
}

function clearFilter(update) {
  filt.types.clear(); filt.hideDep = false;
  document.querySelectorAll('.graphlegend .ftog').forEach((el) => toggleRow(el, false));
  $('fclear').hidden = true;
  if (update !== false) applyFilter();
}

function clearSelection() {
  if (cy) {
    cy.batch(() => {
      cy.elements().removeClass('dim hi near related outside');
      if (_overviewPositions) cy.nodes().positions((n) => _overviewPositions.get(n.id()));
    });
    if (_overviewPositions) cy.fit(cy.elements().not('.filtered'), 40);
    const visible = cy.nodes().not('.filtered');
    $('gcount').textContent = visible.length === cy.nodes().length ? _countText
      : `表示 ${visible.length} / ${cy.nodes().length} ノード（絞り込み中）`;
  }
  _overviewPositions = null;
  $('nodecard').classList.remove('show');
  $('nodecard-empty').hidden = false;
  $('gselection-clear').hidden = true;
}

function focusNode(n) {
  if (!_overviewPositions) _overviewPositions = new Map(cy.nodes().map((node) => [node.id(), { ...node.position() }]));
  const around = n.closedNeighborhood().not('.filtered');
  cy.batch(() => {
    cy.elements().difference(around).addClass('outside');
    around.removeClass('outside');
  });
  around.layout({ ...graphLayout(), minNodeSpacing: 70, concentric: (node) => node === n ? 1 : 0 }).run();
  selectNode(n);
  $('gcount').textContent = `周辺 ${around.nodes().length} ノード・関係 ${around.edges().length}（読み込み済み ${cy.nodes().length} ノード）`;
}

function selectNode(n) {
  cy.batch(() => {
    cy.elements().removeClass('hi near related').addClass('dim');
    n.removeClass('dim').addClass('hi');
    n.neighborhood().removeClass('dim').addClass('near');
    n.connectedEdges().addClass('related');
  });
  const d = n.data();
  const rows = [];
  if (d.value != null) rows.push(`<div class="nrow">値: <b>${esc(d.value)}</b></div>`);
  if (d.parent) rows.push(`<div class="nrow">所属: ${esc(d.parent)}</div>`);
  if (d.category) rows.push(`<div class="nrow">カテゴリ: ${esc(d.category)}</div>`);
  if (d.phase) rows.push(`<div class="nrow">工程: ${esc(d.phase)}</div>`);
  if (d.path) rows.push(`<div class="nrow">資料: ${esc(d.path)}</div>`);
  if (d.status !== 'active') rows.push(`<div class="nrow">状態: ${d.status === 'deprecated' ? '廃止' : '未使用の疑い'}</div>`);
  // 主要ノードの上限・種別フィルターを適用した表示範囲で数える。
  rows.push(`<div class="nrow">表示中のつながり数: ${n.connectedEdges().not('.filtered').length} 本</div>`);
  const connections = n.connectedEdges().not('.filtered').map((edge) => {
    const outgoing = edge.source().id() === n.id();
    const other = outgoing ? edge.target() : edge.source();
    return `<button type="button" class="gresult" data-node="${esc(other.id())}">${esc(other.data('label'))}`
      + `<small>${outgoing ? 'この要素 → 接続先' : '接続先 → この要素'} · ${esc(edge.data('label'))}</small></button>`;
  }).join('');
  $('nodecard').innerHTML = `<span class="nt">${esc(d.type_ja)}</span><div class="nn">${esc(d.label)}</div>`
    + rows.join('')
    + `<button class="btn-secondary ask" data-ask="${esc(d.label)}">この語で影響を調べる</button>`
    + (connections ? '<h3>つながり</h3><div class="nconnections">' + connections + '</div>' : '');
  $('nodecard').classList.add('show');
  $('nodecard-empty').hidden = true;
  $('gselection-clear').hidden = false;
}

function search(q) {
  // クイック名検索は現在 cy に読み込み済みのノードだけが対象（主要ノードのみの表示中は部分一致）。
  if (!cy) return;
  clearSelection();
  q = q.trim().toLowerCase();
  const visible = cy.nodes().not('.filtered');
  $('gresults-section').hidden = !q;
  if (!q) return;
  const m = visible.filter((n) => n.data('label').toLowerCase().includes(q));
  // 一覧は先頭50件を明記。グラフ上の強調は一致した全件に適用する。
  $('gresults-count').textContent = `${m.length} 件`;
  $('gresults').innerHTML = m.slice(0, 50).map((n) =>
    `<button type="button" class="gresult" data-node="${esc(n.id())}">${esc(n.data('label'))}<small>${esc(n.data('type_ja'))}</small></button>`).join('')
    + (m.length > 50 ? '<p class="leg-help">先頭 50 件を表示しています。名前を追加して絞り込んでください。</p>' : '');
  if (!m.length) {
    $('gcount').textContent = _truncated
      ? '表示中には見つかりません。「すべて表示」で全体から探せます'
      : `「${q}」に一致なし`;
    $('gresults').textContent = $('gcount').textContent;
    return;
  }
  cy.batch(() => {
    cy.elements().addClass('dim'); m.removeClass('dim').addClass('hi');
    m.neighborhood().removeClass('dim');
  });
  cy.fit(m, 60);
}

async function runGraphSearch() {
  if (!_world) return;
  const rel = $('relfilter').value;
  const field = $('fieldfilter').value;
  const value = $('condvalue').value.trim();
  if (!rel && !field && !value) { resetGraphSearch(); return; }
  const qs = new URLSearchParams({ world: _world, op: $('opfilter').value || 'eq' });
  if (rel) qs.append('relationship', rel);
  if (field) qs.set('field', field);
  if (value) qs.set('value', value);
  $('gfilter').disabled = true;
  const main = document.querySelector('.graphmain');
  if (main) main.setAttribute('aria-busy', 'true');
  $('gcount').textContent = '検索中…';
  try {
    const r = await fetch('/graph/search?' + qs.toString());
    if (!r.ok) throw new Error(await r.text());
    renderGraph(await r.json(), true);
  } catch (e) {
    $('gcount').textContent = '検索に失敗しました';
  } finally {
    $('gfilter').disabled = false;
    if (main) main.setAttribute('aria-busy', 'false');
  }
}

function resetGraphSearch() {
  $('relfilter').value = '';
  $('fieldfilter').value = '';
  $('condvalue').value = '';
  if (_fullGraph) renderGraph(_fullGraph);
}

function renderAskResult(res) {
  const cited = res.cited_nodes || [];
  const paths = cited.map((n) => {
    const p = (n.path || []).join(' → ');
    return `<div class="gpath"><b>${esc(n.name || '')}</b>${p ? `<span>${esc(p)}</span>` : ''}</div>`;
  }).join('');
  const s = res.summary || null;
  const summary = s ? `<div class="ganswer-cites">
      <div class="gpath"><b>対象</b><span>${esc(s.world || '')} / ${esc((s.scope_paths && s.scope_paths.length ? s.scope_paths.join(', ') : '全体'))}</span></div>
      <div class="gpath"><b>件数</b><span>文書 ${esc(s.documents)} 件・ノード ${esc(s.graph_nodes)} 件・関係 ${esc(s.graph_edges)} 件</span></div>
      <div class="gpath"><b>弱点候補</b><span>孤立ノード ${esc(s.isolated_node_count)} 件・関係が薄い文書 ${esc(s.weak_document_count)} 件</span></div>
    </div>` : '';
  // status="llm_unavailable"/"failed" は通常の回答ではなくエラー（AI未接続・生成失敗）＝danger色で明示する。
  // "no_graph_evidence" はグラフに根拠が無かっただけの正常回答なので通常表示のまま。
  const isError = res.status === 'llm_unavailable' || res.status === 'failed';
  const answerStyle = isError ? ' style="color:var(--danger)"' : '';
  $('ganswer').innerHTML = `<div class="ganswer-text"${answerStyle}>${esc(res.answer || '回答なし')}</div>`
    + summary
    + (paths ? `<div class="ganswer-cites">${paths}</div>` : '');
}

async function askGraph() {
  const q = $('gask').value.trim();
  if (!q || !_world) return;
  $('gaskbtn').disabled = true;
  $('ganswer').innerHTML = '<div class="loading-inline" role="status"><span class="spinner spinner-sm"></span><span>ナレッジ状況を確認しています...</span></div>';
  try {
    const r = await fetch('/graph/ask', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: q, world: _world, scope_paths: [] }),
    });
    if (!r.ok) throw new Error('graph ask failed');
    renderAskResult(await r.json());
  } catch (e) {
    $('ganswer').innerHTML = '<div class="muted" style="color:var(--danger)">質問に失敗しました。AI接続とNeo4jの状態を確認して、もう一度お試しください。</div>';
  } finally {
    $('gaskbtn').disabled = false;
  }
}

// グラフ→チャット連携：ノードからそのまま影響を質問
$('nodecard').addEventListener('click', (e) => {
  const a = e.target.closest('[data-ask]');
  if (a) { localStorage.setItem('sherpa-ask', a.dataset.ask + 'を変えたい。影響は？'); location.href = 'chat.html'; }
  const connection = e.target.closest('[data-node]');
  if (connection) {
    const hadFocus = document.activeElement === connection;
    focusNode(cy.getElementById(connection.dataset.node));
    if (hadFocus) $('gselection-clear').focus();
  }
});
// 「解除」は押すと自分が hidden になる＝フォーカスの行き先を名前検索欄へ移す（キーボード操作を途切れさせない）。
$('gselection-clear').addEventListener('click', () => { clearSelection(); $('gsearch').focus(); });
$('gresults').addEventListener('click', (e) => {
  const result = e.target.closest('[data-node]');
  if (!result || !cy) return;
  const n = cy.getElementById(result.dataset.node);
  focusNode(n);
});
$('gsearch').addEventListener('input', (e) => search(e.target.value));
$('gfilter').addEventListener('click', runGraphSearch);
$('greset').addEventListener('click', resetGraphSearch);
$('showall').addEventListener('click', showAllNodes);
$('condvalue').addEventListener('keydown', (e) => { if (e.key === 'Enter') runGraphSearch(); });
$('gaskbtn').addEventListener('click', askGraph);
$('gask').addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) askGraph(); });
$('relayout').addEventListener('click', () => { if (cy) { clearSelection(); cy.layout(graphLayout()).run(); } });
$('fit').addEventListener('click', () => { if (cy) { clearSelection(); cy.fit(cy.elements().not('.filtered'), 40); } });
document.querySelector('.graphlegend').addEventListener('click', onLegendClick);
$('fclear').addEventListener('click', clearFilter);
document.addEventListener('click', (e) => { if (e.target && e.target.id === 'graph-retry') load(); });

// テーマ
function applyThemeIcon() { $('themebtn').textContent = document.documentElement.dataset.theme === 'dark' ? '☀️' : '🌙'; }
$('themebtn').addEventListener('click', () => {
  const d = document.documentElement, next = d.dataset.theme === 'dark' ? 'light' : 'dark';
  d.dataset.theme = next; localStorage.setItem('sherpa-theme', next); applyThemeIcon();
  if (cy) cy.style(styleFor(next === 'dark'));
});
applyThemeIcon();

// ===== admin ガード（CLEAN-1・2026-09-03・W1 の残・同型是正）: `/graph`・`/graph/facets`・
// `/graph/search`・`POST /graph/ask` は全て admin 限定 API。この画面は丸ごと admin 専用＝
// ingest.js/admin-settings.js/audit.js と同じ自前 checkAdmin() パターン（nav.js の _isAdminUser は
// カスタムエレメント内部の非公開状態のため外部から参照できない）。判定失敗時は fail-safe で
// access-denied 側に倒す。
async function checkAdmin() {
  try {
    const u = await getJSON('/auth/me');
    return u && u.role === 'admin';
  } catch (_) { /* compat */ }
  return false;
}

// 初期化（admin ガード）: 非 admin には access-denied だけを見せ、グラフデータ（/worlds・/graph・
// /graph/facets）は取得しない（ingest.js と同じ「本体を読まずに弾く」パターン）。
(async () => {
  const isAdmin = await checkAdmin();
  if (!isAdmin) {
    const main = $('main-content'), denied = $('access-denied');
    if (main) main.style.display = 'none';
    if (denied) denied.style.display = 'block';
    return;
  }
  load();
})();
