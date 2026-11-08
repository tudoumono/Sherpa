// 資料の優先と除外の画面（admin 専用）。フォルダの木（GET /admin/path-rules）とフォルダ直下のファイル（GET /admin/path-rules/files）に、
// 「なし／優先／除外」と、優先の「範囲の外でも読む」を付ける。保存は「保存する…」で変更の一覧を確かめてから PUT /admin/path-rules。
// 設計: docs/design/settings.md「優先と除外」
// セキュリティ: server data（フォルダ名・ファイル名）は全て esc()。インラインハンドラ無し（委譲）。
'use strict';

const $ = Sherpa.$, esc = Sherpa.esc, api = Sherpa.api;

const LABEL = { priority: '優先', exclude: '除外', none: 'なし' };
const state = {
  world: '', label: '',
  folders: [],            // [{path, depth, count}]（path 昇順）
  files: new Map(),       // フォルダ path（''＝資料フォルダ直下）→ ファイル path の配列。開いたフォルダだけ
  more: new Map(),        // フォルダ path → {next, total}（続きがあるときだけ）
  open: new Set(['']),
  saved: new Map(),       // path → {kind, outside}
  draft: new Map(),
  filter: 'all',
  query: '',
};

function toast(msg) {
  const t = $('toast'); if (!t) return;
  t.textContent = msg; t.classList.add('show');
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 2800);
}

async function checkAdmin() {
  let r;
  try { r = await fetch('/auth/me'); } catch (_) { return 'unreachable'; }
  if (r.status === 401 || r.status === 403) return 'denied';
  if (!r.ok) return 'unreachable';
  try { const u = await r.json(); return u && u.role === 'admin' ? 'admin' : 'denied'; } catch (_) { return 'unreachable'; }
}

const base = (p) => p.split('/').pop();
const parentOf = (p) => (p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '');
const ruleOf = (m, p) => m.get(p) || { kind: 'none', outside: false };
const sameRule = (a, b) => a.kind === b.kind && (a.kind !== 'priority' || !!a.outside === !!b.outside);

function ancestors(p) {
  const out = [];
  for (let q = parentOf(p); q; q = parentOf(q)) out.push(q);
  return out;
}
// 親に付いている設定のうち、いちばん上のもの（親に付いていれば配下は従うだけ）
function ruledAncestor(p, m) {
  const a = ancestors(p).reverse().find((q) => ruleOf(m, q).kind !== 'none');
  return a === undefined ? null : a;
}
function effective(p, m) {
  const own = ruleOf(m, p);
  if (own.kind !== 'none') return { ...own, from: p };
  const a = ruledAncestor(p, m);
  return a === null ? { kind: 'none', outside: false, from: null } : { ...ruleOf(m, a), from: a };
}

const changedPaths = () => [...new Set([...state.draft.keys(), ...state.saved.keys()])]
  .filter((p) => !sameRule(ruleOf(state.draft, p), ruleOf(state.saved, p))).sort();

function setRule(path, kind, outside) {
  if (ruledAncestor(path, state.draft) !== null) return;  // 親に付いている間は、配下は個別に変えられない
  if (kind === 'none') {
    state.draft.delete(path);
  } else {
    state.draft.set(path, { kind, outside: kind === 'priority' && !!outside });
    // 親に付けたら、配下の個別の設定は消える（保存の確認でも知らせる）
    for (const k of [...state.draft.keys()]) if (k.startsWith(path + '/')) state.draft.delete(k);
  }
  render();
}

function describe(r) {
  if (r.kind === 'none') return 'なし';
  return LABEL[r.kind] + (r.kind === 'priority' && r.outside ? '（範囲の外でも読む）' : '');
}

// 保存で消える配下の個別の設定（下書きに残っているが、親に付けたため落ちるもの）はここには残らない。
// 保存済みにあって、下書きの親に設定が付いたことで消える分を数える。
function droppedBySave() {
  return [...state.saved.keys()].filter((p) => !state.draft.has(p) && ruledAncestor(p, state.draft) !== null
    && ruleOf(state.draft, p).kind === 'none').sort();
}

function visibleRows() {
  const rows = [];
  const q = state.query.trim().toLowerCase();
  const matches = (p) => {
    const own = ruleOf(state.draft, p);
    const eff = effective(p, state.draft);
    if (state.filter === 'pri' && eff.kind !== 'priority') return false;
    if (state.filter === 'exc' && eff.kind !== 'exclude') return false;
    if (state.filter === 'unsaved' && sameRule(own, ruleOf(state.saved, p))) return false;
    if (q && !p.toLowerCase().includes(q)) return false;
    return true;
  };
  const filtering = state.filter !== 'all' || q;
  const entries = [];
  for (const f of state.folders) entries.push({ path: f.path, dir: true, count: f.count });
  const seen = new Set(state.folders.map((f) => f.path));
  for (const list of state.files.values()) for (const p of list) if (!seen.has(p)) { seen.add(p); entries.push({ path: p, dir: false }); }
  // まだ開いていないフォルダの中のファイルでも、設定が付いているものは一覧に出す（絞り込みで見つけられるように）
  for (const p of [...state.draft.keys(), ...state.saved.keys()]) if (!seen.has(p)) { seen.add(p); entries.push({ path: p, dir: false }); }
  entries.sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
  const keep = new Set();
  if (filtering) {
    for (const e of entries) if (matches(e.path)) { keep.add(e.path); ancestors(e.path).forEach((a) => keep.add(a)); }
  }
  for (const e of entries) {
    if (filtering && !keep.has(e.path)) continue;
    if (!filtering && ancestors(e.path).some((a) => !state.open.has(a))) continue;
    rows.push(e);
  }
  return rows;
}

function moreBtn(e) {
  const m = e.dir && state.open.has(e.path) ? state.more.get(e.path) : null;
  return m ? `<button class="btn-ghost" type="button" data-more="${esc(e.path)}">ファイルの続きを読み込む（あと ${esc(String(m.total - (state.files.get(e.path) || []).length))} 件）</button>` : '';
}

function rowHTML(e) {
  const own = ruleOf(state.draft, e.path);
  const anc = ruledAncestor(e.path, state.draft);
  const changed = !sameRule(own, ruleOf(state.saved, e.path));
  const ind = ancestors(e.path).length * 18;
  const tog = e.dir
    ? `<button class="tog" type="button" data-tog="${esc(e.path)}" aria-expanded="${state.open.has(e.path)}" aria-label="${esc(base(e.path))}を開閉">${state.open.has(e.path) ? '▾' : '▸'}</button>`
    : '<span style="width:22px"></span>';
  const name = `<span class="tname" style="padding-left:${ind}px">${tog}<span>${e.dir ? '📁' : '📄'}</span><span>${esc(base(e.path))}</span>${
    e.dir ? `<span class="muted tnote">${esc(String(e.count))} 件</span>` : ''}${moreBtn(e)}</span>`;
  let set;
  if (anc !== null) {
    const pr = ruleOf(state.draft, anc);
    set = `<span class="tset"><span class="pill ${pr.kind === 'priority' ? 'pri' : 'exc'}">${esc(LABEL[pr.kind])}</span>`
      + `<span class="tnote">親の「${esc(base(anc))}」に従います${pr.kind === 'priority' && pr.outside ? '（範囲の外でも読む）' : ''}</span></span>`;
  } else {
    const opt = (v) => `<option value="${v}"${own.kind === v ? ' selected' : ''}>${LABEL[v]}</option>`;
    set = `<span class="tset"><select data-set="${esc(e.path)}" aria-label="${esc(base(e.path))}の設定">${opt('none')}${opt('priority')}${opt('exclude')}</select>`
      + (own.kind === 'priority'
        ? `<label><input type="checkbox" data-out="${esc(e.path)}"${own.outside ? ' checked' : ''}> 範囲の外でも読む</label>` : '')
      + (changed ? '<span class="tnote">（まだ保存していません）</span>' : '') + '</span>';
  }
  return `<div class="trow${changed ? ' changed' : ''}" data-row="${esc(e.path)}">${name}${set}</div>`;
}

function render() {
  const rows = visibleRows();
  $('tree').innerHTML = rows.length ? rows.map(rowHTML).join('')
    : '<div class="muted" style="padding:14px">該当するフォルダ・ファイルはありません</div>';
  $('tree').setAttribute('aria-busy', 'false');
  const rm = state.more.get('');
  $('rootmore').innerHTML = rm
    ? `<button class="btn-ghost" type="button" data-more="">資料フォルダ直下のファイルの続きを読み込む（あと ${esc(String(rm.total - (state.files.get('') || []).length))} 件）</button>` : '';
  document.querySelectorAll('.filterchip').forEach((b) => b.classList.toggle('on', b.dataset.f === state.filter));
  const own = [...state.draft.entries()].sort((a, b) => (a[0] < b[0] ? -1 : 1));
  const pri = own.filter(([, r]) => r.kind === 'priority').length;
  const exc = own.filter(([, r]) => r.kind === 'exclude').length;
  $('sidesub').textContent = own.length ? `優先 ${pri} 件・除外 ${exc} 件` : 'まだ何も設定していません';
  $('sidelist').innerHTML = own.map(([p, r]) =>
    `<div class="item"><span class="pill ${r.kind === 'priority' ? 'pri' : 'exc'}">${esc(LABEL[r.kind])}</span><span>${esc(p)}${
      r.kind === 'priority' && r.outside ? ' <span class="tnote">（範囲の外でも読む）</span>' : ''}</span></div>`).join('');
  const dirty = changedPaths().length > 0;
  $('savebtn').disabled = !dirty;
  $('discardbtn').disabled = !dirty;
}

function applySaved(rules) {
  state.saved = new Map(rules.map((r) => [r.path, { kind: r.kind, outside: !!r.outside_scope }]));
  state.draft = new Map(state.saved);
}

async function loadFiles(folder, more) {
  if (state.files.has(folder) && !more) return;
  const off = more && state.more.has(folder) ? state.more.get(folder).next : 0;
  const d = await api('GET', `/admin/path-rules/files?world=${encodeURIComponent(state.world)}&folder=${encodeURIComponent(folder)}&offset=${off}`);
  state.files.set(folder, (off ? state.files.get(folder) || [] : []).concat(d.files || []));
  if (d.next_offset === null || d.next_offset === undefined) state.more.delete(folder);
  else state.more.set(folder, { next: d.next_offset, total: d.total });
}

async function loadMore(folder) {
  try { await loadFiles(folder, true); } catch (_) { toast('続きを読み込めませんでした'); }
  render();
}

async function toggle(path) {
  if (state.open.has(path)) state.open.delete(path);
  else {
    state.open.add(path);
    try { await loadFiles(path); } catch (_) { toast('ファイルの一覧を読み込めませんでした'); }
  }
  render();
}

function openConfirm() {
  const ch = changedPaths();
  const rows = ch.map((p) => `<div class="item" style="padding:4px 0;border-bottom:1px solid var(--border)"><b>${esc(p)}</b><br>`
    + `<span class="muted">${esc(describe(ruleOf(state.saved, p)))} → ${esc(describe(ruleOf(state.draft, p)))}</span></div>`).join('');
  const dropped = droppedBySave();
  const note = dropped.length
    ? `<div class="banner">親のフォルダに設定を付けたため、その下に個別に付けていた設定 ${dropped.length} 件は保存と同時に消えます。<br>`
      + dropped.map((p) => esc(p)).join('<br>') + '</div>' : '';
  $('dlgbody').innerHTML = `<div>${ch.length} 件を変えます。保存すると、次の質問からすぐ検索に効きます（作り直しは要りません）。</div>${note}${rows}`;
  $('ovl').hidden = false;
  $('okbtn').focus();
}

async function save() {
  const rules = [...state.draft.entries()].map(([path, r]) => ({ path, kind: r.kind, outside_scope: r.kind === 'priority' && !!r.outside }));
  $('okbtn').disabled = true;
  try {
    const d = await api('PUT', '/admin/path-rules', { world: state.world, rules });
    applySaved(d.rules || []);
    $('ovl').hidden = true;
    toast('保存しました。次の質問から効きます');
  } catch (e) {
    toast('保存できませんでした: ' + (e && e.message ? e.message : '時間をおいてやり直してください'));
  } finally {
    $('okbtn').disabled = false;
  }
  render();
}

function bind() {
  const onMore = (ev) => {
    const m = ev.target.closest('[data-more]');
    if (m) loadMore(m.dataset.more);
  };
  $('tree').addEventListener('click', (ev) => {
    const t = ev.target.closest('[data-tog]');
    if (t) toggle(t.dataset.tog);
    onMore(ev);
  });
  $('rootmore').addEventListener('click', onMore);
  $('tree').addEventListener('change', (ev) => {
    const sel = ev.target.closest('[data-set]');
    if (sel) { setRule(sel.dataset.set, sel.value, ruleOf(state.draft, sel.dataset.set).outside); return; }
    const out = ev.target.closest('[data-out]');
    if (out) setRule(out.dataset.out, 'priority', out.checked);
  });
  document.querySelectorAll('.filterchip').forEach((b) => b.addEventListener('click', () => { state.filter = b.dataset.f; render(); }));
  $('q').addEventListener('input', (ev) => { state.query = ev.target.value; render(); });
  $('savebtn').addEventListener('click', openConfirm);
  $('discardbtn').addEventListener('click', () => { state.draft = new Map(state.saved); render(); });
  $('cancelbtn').addEventListener('click', () => { $('ovl').hidden = true; });
  $('okbtn').addEventListener('click', save);
  $('ovl').addEventListener('keydown', (ev) => { if (ev.key === 'Escape') $('ovl').hidden = true; });
}

async function load() {
  const ws = (await api('GET', '/worlds')).worlds || [];
  if (!ws.length) {
    // 登録が無くても、検索が使う既定の資料フォルダ（開発用の試験データなど）があればそれを使う
    const opt = await api('GET', '/world-options').catch(() => ({ worlds: [] }));
    if ((opt.worlds || []).length) ws.push({ world_id: opt.worlds[0] });
  }
  if (!ws.length) {
    $('tree').innerHTML = '<div class="muted" style="padding:14px">資料フォルダがまだ登録されていません</div>';
    $('tree').setAttribute('aria-busy', 'false');
    return;
  }
  state.world = ws[0].world_id;
  const d = await api('GET', `/admin/path-rules?world=${encodeURIComponent(state.world)}`);
  state.label = d.label || state.world;
  $('world-title').textContent = `資料フォルダ「${state.label}」`;
  state.folders = d.folders || [];
  applySaved(d.rules || []);
  if (d.removed_missing) {
    const b = $('removed-banner');
    b.textContent = `消えた場所の設定を ${d.removed_missing} 件取り除きました`;
    b.hidden = false;
  }
  try { await loadFiles(''); } catch (_) { /* 直下のファイルが読めなくてもフォルダの木は使える */ }
  render();
}

async function init() {
  const adminState = await checkAdmin();
  if (adminState === 'denied') {
    $('main-content').style.display = 'none';
    $('access-denied').style.display = 'block';
    return;
  }
  bind();
  try { await load(); } catch (_) { toast('読み込めませんでした。時間をおいてやり直してください'); }
}

init();
