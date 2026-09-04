// ナレッジグラフ可視化（cytoscape）。管理用の関係検索とグラフ質問を含む。
'use strict';
const $ = Sherpa.$, esc = Sherpa.esc, getJSON = Sherpa.getJSON;     // 共通ユーティリティ（nav.js・RV DRY）

const COLOR = {
  Module: '#0d9488', Copybook: '#0891b2', DataItem: '#64748b',
  Batch: '#ea580c', Document: '#16a34a', Table: '#0e7490',
};
const TYPE_JA = {
  Module: 'プログラム', Copybook: 'コピーブック', DataItem: '項目',
  Document: '文書', Batch: 'バッチ', Table: 'テーブル',
};
const FIELD_LABEL = {
  category: 'カテゴリ', phase: '工程', role: '種別', top_scope: '最上位フォルダ', status: '状態',
};
let cy = null, _world = null, _fullGraph = null, _truncated = false;   // _truncated=表示中は主要ノードのみ（全体ではない）

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
  const edge = dark ? '#3a4550' : '#cbd5e1';
  return [
    { selector: 'node', style: {
      'background-color': (e) => COLOR[e.data('type')] || '#64748b',
      'label': 'data(label)', 'font-size': 10, 'color': labelColor, 'width': 22, 'height': 22,
      'text-valign': 'bottom', 'text-margin-y': 3, 'border-width': 2, 'border-color': outline,
      'text-outline-width': 2, 'text-outline-color': outline, 'text-max-width': 110, 'text-wrap': 'ellipsis',
    } },
    { selector: 'node[status="deprecated"]', style: { 'opacity': 0.45, 'border-style': 'dashed' } },
    { selector: 'node[status="hidden_candidate"]', style: { 'opacity': 0.55 } },
    { selector: 'edge', style: {
      'width': 1.4, 'line-color': edge, 'target-arrow-color': edge, 'target-arrow-shape': 'triangle',
      'curve-style': 'bezier', 'arrow-scale': 0.8, 'label': 'data(label)', 'font-size': 7,
      'color': dark ? '#6b7a8a' : '#94a3b8', 'text-rotation': 'autorotate',
    } },
    { selector: '.dim', style: { 'opacity': 0.1 } },
    { selector: '.hi', style: { 'border-color': '#0d9488', 'border-width': 4 } },
  ];
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
    + '<div style="font-size:15px;font-weight:600;color:var(--ink,#1f2937);margin-bottom:8px">関係グラフはまだありません</div>'
    + '関係グラフは<b>ソースコード（COBOL/JCL/コピーブック）</b>から作られます。'
    + 'このフォルダが文書（Office/テキスト）のみの場合、関係グラフは空のままです（想定どおりです）。'
    + '文書そのものは取り込み済みで、検索からも使えます。</div>';
}

function renderGraph(g, searched) {
  if (cy) { cy.destroy(); cy = null; }
  $('nodecard').classList.remove('show');
  $('cy').innerHTML = '';
  clearFilter(false);
  $('showall').hidden = true;                     // 既定は隠す（主要ノードで切れている時だけ下で出す）
  if (!g.nodes.length) {
    if (searched) {
      $('gcount').textContent = '検索結果 0件';
      $('cy').innerHTML = '<div class="gempty">一致するグラフ要素はありません</div>';
      $('greset').hidden = false;
      renderLegend([]);
      return;
    }
    emptyGraph(g);
    return;
  }
  // _truncated: 現在 cy に載っているのが主要ノードのみ（全体ではない）＝クイック名検索の案内文言に使う。
  // 検索結果（/graph/search）は node-limit 由来の truncation とは別概念なので false 扱い。
  _truncated = !searched && !!g.truncated;
  if (searched) {
    $('gcount').textContent = `検索結果 ノード ${g.nodes.length}・関係 ${g.edges.length}`;
  } else if (g.truncated) {                        // 主要ノードのみ＝残りは「すべて表示」で辿る（専門用語ゼロ）
    $('gcount').textContent = `主要な ${g.nodes.length} 件を表示しています（全 ${g.total_nodes} 件）`;
    $('showall').hidden = false;
  } else {
    $('gcount').textContent = `ノード ${g.nodes.length}・関係 ${g.edges.length}`;
  }
  $('greset').hidden = !searched;
  const els = [
    ...g.nodes.map((n) => ({ data: {
      id: n.id, label: n.name, type: n.type, type_ja: n.type_ja || TYPE_JA[n.type] || n.type,
      status: n.status, value: n.value, parent: n.parent, category: n.category,
      phase: n.phase, top_scope: n.top_scope, path: n.path,
    } })),
    ...g.edges.map((e) => ({ data: { source: e.source, target: e.target, label: e.type, status: e.status } })),
  ];
  cy = cytoscape({
    container: $('cy'), elements: els, style: styleFor(document.documentElement.dataset.theme === 'dark'),
    layout: { name: 'cose', animate: true, idealEdgeLength: 95, nodeRepulsion: 9000, padding: 50, randomize: true, fit: true },
    wheelSensitivity: 0.2, maxZoom: 1.6, minZoom: 0.12,
  });
  cy.one('layoutstop', () => cy.fit(undefined, 50));
  renderLegend(g.nodes);
  cy.on('tap', 'node', (evt) => selectNode(evt.target));
  cy.on('tap', (evt) => { if (evt.target === cy) { cy.elements().removeClass('dim hi'); $('nodecard').classList.remove('show'); } });
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
  const types = [...new Set(nodes.map((n) => n.type))].sort((a, b) => (TYPE_JA[a] || a).localeCompare(TYPE_JA[b] || b, 'ja'));
  $('legtypes').innerHTML = types.map((t) =>
    `<div class="legrow ftog" data-ftype="${esc(t)}"><span class="legdot" style="background:${COLOR[t] || '#64748b'}"></span>${esc(TYPE_JA[t] || t)}</div>`).join('');
}

// ── 絞り込み（種別 / 状態。cytoscape の display で適用）──
const filt = { types: new Set(), hideDep: false };

function applyFilter() {
  if (!cy) return;
  cy.batch(() => {
    cy.nodes().forEach((n) => {
      const st = n.data('status');
      const off = filt.types.has(n.data('type')) || (filt.hideDep && st && st !== 'active');
      n.style('display', off ? 'none' : 'element');
    });
    cy.edges().forEach((e) => {
      const vis = e.source().style('display') !== 'none' && e.target().style('display') !== 'none';
      e.style('display', vis ? 'element' : 'none');
    });
  });
  const total = cy.nodes().length;
  const shown = cy.nodes().filter((n) => n.style('display') !== 'none').length;
  $('gcount').textContent = (shown === total) ? `ノード ${total}・関係 ${cy.edges().length}` : `表示 ${shown} / ${total} ノード（絞り込み中）`;
  const active = filt.types.size || filt.hideDep;
  $('fclear').hidden = !active;
}

function toggleRow(el, on) { el.classList.toggle('off', on); el.style.opacity = on ? '.4' : ''; el.style.textDecoration = on ? 'line-through' : ''; }

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

function selectNode(n) {
  cy.elements().addClass('dim'); cy.elements().removeClass('hi');
  n.removeClass('dim'); n.neighborhood().removeClass('dim'); n.addClass('hi');
  const d = n.data();
  const rows = [];
  if (d.value != null) rows.push(`<div class="nrow">値: <b>${esc(d.value)}</b></div>`);
  if (d.parent) rows.push(`<div class="nrow">所属: ${esc(d.parent)}</div>`);
  if (d.category) rows.push(`<div class="nrow">カテゴリ: ${esc(d.category)}</div>`);
  if (d.phase) rows.push(`<div class="nrow">工程: ${esc(d.phase)}</div>`);
  if (d.status !== 'active') rows.push(`<div class="nrow">状態: ${d.status === 'deprecated' ? '廃止' : '未使用の疑い'}</div>`);
  // n.degree() は現在 cy に載っているノードだけを数える（主要ノードのみの表示中は部分グラフ上の値）。
  rows.push(`<div class="nrow">表示中のつながり数: ${n.degree()} 本</div>`);
  $('nodecard').innerHTML = `<span class="nt">${esc(d.type_ja)}</span><div class="nn">${esc(d.label)}</div>`
    + rows.join('') + `<button class="btn-secondary ask" data-ask="${esc(d.label)}">💬 この語で影響を調べる</button>`;
  $('nodecard').classList.add('show');
}

function search(q) {
  // クイック名検索は現在 cy に読み込み済みのノードだけが対象（主要ノードのみの表示中は部分一致）。
  if (!cy) return;
  cy.elements().removeClass('dim hi');
  q = q.trim().toLowerCase();
  if (!q) return;
  const m = cy.nodes().filter((n) => n.data('label').toLowerCase().includes(q));
  if (!m.length) {
    $('gcount').textContent = _truncated
      ? '表示中には見つかりません。「すべて表示」で全体から探せます'
      : `「${q}」に一致なし`;
    return;
  }
  cy.elements().addClass('dim'); m.removeClass('dim'); m.neighborhood().removeClass('dim'); m.addClass('hi');
  cy.animate({ fit: { eles: cy.elements(), padding: 40 } }, { duration: 400 });
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
});
$('gsearch').addEventListener('input', (e) => search(e.target.value));
$('gfilter').addEventListener('click', runGraphSearch);
$('greset').addEventListener('click', resetGraphSearch);
$('showall').addEventListener('click', showAllNodes);
$('condvalue').addEventListener('keydown', (e) => { if (e.key === 'Enter') runGraphSearch(); });
$('gaskbtn').addEventListener('click', askGraph);
$('gask').addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) askGraph(); });
$('relayout').addEventListener('click', () => cy && cy.layout({ name: 'cose', animate: true, idealEdgeLength: 95, nodeRepulsion: 9000, randomize: true }).run());
$('fit').addEventListener('click', () => cy && cy.animate({ fit: { padding: 40 } }, { duration: 400 }));
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
