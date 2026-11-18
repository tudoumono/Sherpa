// 資料の画面の「優先と除外」タブ（admin 専用）。フォルダの木（GET /admin/path-rules）とフォルダ直下のファイル（GET /admin/path-rules/files）に、
// 「なし／優先／除外」と、優先の「範囲の外でも読む」を付ける。保存は「保存する…」で変更の一覧を確かめてから PUT /admin/path-rules。
// 設計: docs/design/settings.md「優先と除外」
// セキュリティ: server data（フォルダ名・ファイル名）は全て esc()。インラインハンドラ無し（委譲）。
'use strict';

(() => {
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
  buildSaved: new Map(),  // path → 'read'｜'skip'
  buildDraft: new Map(),
  layout: new Set(),      // 置き方から読むフォルダ
  filter: 'all',
  query: '',
  reveal: null,           // {path, dir}（資料の画面の「設定」から来た行）
};

function toast(msg) {
  const t = $('toast'); if (!t) return;
  t.textContent = msg; t.classList.add('show');
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 2800);
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

const BUILD_LABEL = { '': '置き方に従う', read: '読む場所にする', skip: '読む場所にしない' };
const changedBuild = () => [...new Set([...state.buildDraft.keys(), ...state.buildSaved.keys()])]
  .filter((p) => (state.buildDraft.get(p) || '') !== (state.buildSaved.get(p) || '')).sort();
const isDirty = () => changedPaths().length > 0 || changedBuild().length > 0;
const inLayout = (p, dir) => (dir && state.layout.has(p)) || ancestors(p).some((a) => state.layout.has(a));

function buildCell(e) {
  const own = state.buildDraft.get(e.path) || '';
  const opt = (v) => `<option value="${v}"${own === v ? ' selected' : ''}>${BUILD_LABEL[v]}</option>`;
  const anc = ancestors(e.path).find((a) => state.buildDraft.has(a));
  const note = own ? '' : (anc ? `親の「${base(anc)}」の印に従います` : (inLayout(e.path, e.dir) ? '置き方から: ビルドの定義を読む場所' : ''));
  return `<span class="tset"><select data-build="${esc(e.path)}" aria-label="${esc(base(e.path))}のビルドの定義の印">${opt('')}${opt('read')}${opt('skip')}</select>`
    + (note ? `<span class="tnote">${esc(note)}</span>` : '') + '</span>';
}

function setBuild(path, mark) {
  if (mark) state.buildDraft.set(path, mark); else state.buildDraft.delete(path);
  render();
}

// 保存していない変更があるまま離れるときだけ、ブラウザ標準の確認を出す。
window.addEventListener('beforeunload', (e) => {
  if (isDirty()) e.preventDefault();
});

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
  const pinned = [...state.draft.keys(), ...state.saved.keys(), ...state.buildDraft.keys()];
  if (state.reveal && !state.reveal.dir) pinned.push(state.reveal.path);
  for (const p of pinned) if (!seen.has(p)) { seen.add(p); entries.push({ path: p, dir: false }); }
  entries.sort(Sherpa.comparePath);
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
  const buildChanged = (state.buildDraft.get(e.path) || '') !== (state.buildSaved.get(e.path) || '');
  const revealed = state.reveal && state.reveal.path === e.path;
  return `<div class="trow${changed || buildChanged ? ' changed' : ''}${revealed ? ' reveal' : ''}" data-row="${esc(e.path)}">${name}${set}${buildCell(e)}</div>`;
}

function render() {
  const rows = visibleRows();
  $('rules-tree').innerHTML = rows.length ? rows.map(rowHTML).join('')
    : '<div class="muted" style="padding:14px">該当するフォルダ・ファイルはありません</div>';
  $('rules-tree').setAttribute('aria-busy', 'false');
  const rm = state.more.get('');
  $('rootmore').innerHTML = rm
    ? `<button class="btn-ghost" type="button" data-more="">資料フォルダ直下のファイルの続きを読み込む（あと ${esc(String(rm.total - (state.files.get('') || []).length))} 件）</button>` : '';
  document.querySelectorAll('.filterchip[data-f]').forEach((b) => b.classList.toggle('on', b.dataset.f === state.filter));
  const own = [...state.draft.entries()].sort((a, b) => (a[0] < b[0] ? -1 : 1));
  const pri = own.filter(([, r]) => r.kind === 'priority').length;
  const exc = own.filter(([, r]) => r.kind === 'exclude').length;
  $('sidesub').textContent = own.length ? `優先 ${pri} 件・除外 ${exc} 件` : 'まだ何も設定していません';
  $('sidelist').innerHTML = own.map(([p, r]) =>
    `<div class="item"><span class="pill ${r.kind === 'priority' ? 'pri' : 'exc'}">${esc(LABEL[r.kind])}</span><span>${esc(p)}${
      r.kind === 'priority' && r.outside ? ' <span class="tnote">（範囲の外でも読む）</span>' : ''}</span></div>`).join('');
  const dirty = isDirty();
  $('savebtn').disabled = !dirty;
  $('discardbtn').disabled = !dirty;
  $('rules-dot').hidden = !dirty;
}

function applyBuild(marks) {
  state.buildSaved = new Map(marks.map((m) => [m.path, m.mark]));
  state.buildDraft = new Map(state.buildSaved);
}

// 設定を読み込んだ・保存したときに呼ぶ先（取り込み状況の表の印）。
let _onRules = null;
function onRules(fn) { _onRules = fn; }

function applySaved(rules) {
  state.saved = new Map(rules.map((r) => [r.path, { kind: r.kind, outside: !!r.outside_scope }]));
  state.draft = new Map(state.saved);
  if (_onRules) _onRules(rules.map((r) => ({ path: r.path, kind: r.kind, outside_scope: !!r.outside_scope })));
}

async function loadFiles(folder, more) {
  const files = state.files;
  if (files.has(folder) && !more) return;
  const off = more && state.more.has(folder) ? state.more.get(folder).next : 0;
  const d = await api('GET', `/admin/path-rules/files?world=${encodeURIComponent(state.world)}&folder=${encodeURIComponent(folder)}&offset=${off}`);
  if (files !== state.files) return;   // 読み込み中にタブが読み直された
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
  const bch = changedBuild();
  const buildRows = bch.map((p) => `<div class="item" style="padding:4px 0;border-bottom:1px solid var(--border)"><b>${esc(p)}</b><br>`
    + `<span class="muted">ビルドの定義: ${esc(BUILD_LABEL[state.buildSaved.get(p) || ''])} → ${esc(BUILD_LABEL[state.buildDraft.get(p) || ''])}</span></div>`).join('');
  const dropped = droppedBySave();
  const note = dropped.length
    ? `<div class="banner">親のフォルダに設定を付けたため、その下に個別に付けていた設定 ${dropped.length} 件は保存と同時に消えます。<br>`
      + dropped.map((p) => esc(p)).join('<br>') + '</div>' : '';
  $('dlgbody').innerHTML = (ch.length ? `<div>${ch.length} 件を変えます。保存すると、次の質問からすぐ検索に効きます（作り直しは要りません）。</div>${note}${rows}` : '')
    + (bch.length ? `<div>ビルドの定義の印を ${bch.length} 件変えます。次の「更新」でグラフだけを作り直して効かせます。</div>${buildRows}` : '');
  $('rules-ovl').hidden = false;
  $('okbtn').focus();
}

async function save() {
  const rules = [...state.draft.entries()].map(([path, r]) => ({ path, kind: r.kind, outside_scope: r.kind === 'priority' && !!r.outside }));
  $('okbtn').disabled = true;
  try {
    if (changedPaths().length) {
      const d = await api('PUT', '/admin/path-rules', { world: state.world, rules });
      applySaved(d.rules || []);
    }
    if (changedBuild().length) {
      const marks = [...state.buildDraft.entries()].map(([path, mark]) => ({ path, mark }));
      const b = await api('PUT', '/admin/build-marks', { world: state.world, marks });
      applyBuild(b.marks || []);
    }
    $('rules-ovl').hidden = true;
    toast('保存しました');
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
  $('rules-tree').addEventListener('click', (ev) => {
    const t = ev.target.closest('[data-tog]');
    if (t) toggle(t.dataset.tog);
    onMore(ev);
  });
  $('rootmore').addEventListener('click', onMore);
  $('rules-tree').addEventListener('change', (ev) => {
    const bsel = ev.target.closest('[data-build]');
    if (bsel) { setBuild(bsel.dataset.build, bsel.value); return; }
    const sel = ev.target.closest('[data-set]');
    if (sel) { setRule(sel.dataset.set, sel.value, ruleOf(state.draft, sel.dataset.set).outside); return; }
    const out = ev.target.closest('[data-out]');
    if (out) setRule(out.dataset.out, 'priority', out.checked);
  });
  document.querySelectorAll('.filterchip[data-f]').forEach((b) => b.addEventListener('click', () => { state.filter = b.dataset.f; render(); }));
  $('rules-q').addEventListener('input', (ev) => { state.query = ev.target.value; render(); });
  $('savebtn').addEventListener('click', openConfirm);
  $('discardbtn').addEventListener('click', () => { state.draft = new Map(state.saved); state.buildDraft = new Map(state.buildSaved); render(); });
  $('cancelbtn').addEventListener('click', () => { $('rules-ovl').hidden = true; });
  $('okbtn').addEventListener('click', save);
  $('rules-ovl').addEventListener('keydown', (ev) => { if (ev.key === 'Escape') $('rules-ovl').hidden = true; });
}

// タブを開くたびに読み直す。読み込み前に、前回の木・設定・開閉・消えた場所の知らせを空に戻す。
let _loadSeq = 0;
function resetState() {
  state.reveal = null;
  state.world = ''; state.label = '';
  state.folders = []; state.files = new Map(); state.more = new Map(); state.open = new Set(['']);
  state.saved = new Map(); state.draft = new Map();
  state.buildSaved = new Map(); state.buildDraft = new Map(); state.layout = new Set();
  $('rules-ovl').hidden = true;
  $('removed-banner').hidden = true;
  $('world-title').textContent = '資料フォルダ';
}

function showTreeMessage(html) {
  render();
  $('rules-tree').innerHTML = html;
  $('rules-tree').setAttribute('aria-busy', 'false');
}

async function load() {
  const seq = ++_loadSeq;
  resetState();
  render();
  $('rules-tree').innerHTML = '<div class="muted" style="padding:14px">読み込み中…</div>';
  $('rules-tree').setAttribute('aria-busy', 'true');
  const ws = (await api('GET', '/worlds')).worlds || [];
  if (!ws.length) {
    // 登録が無くても、検索が使う既定の資料フォルダ（開発用の試験データなど）があればそれを使う
    const opt = await api('GET', '/world-options').catch(() => ({ worlds: [] }));
    if ((opt.worlds || []).length) ws.push({ world_id: opt.worlds[0] });
  }
  if (seq !== _loadSeq) return;
  if (!ws.length) {
    showTreeMessage('<div class="muted" style="padding:14px">資料フォルダがまだ登録されていません</div>');
    return;
  }
  const world = ws[0].world_id;
  const d = await api('GET', `/admin/path-rules?world=${encodeURIComponent(world)}`);
  if (seq !== _loadSeq) return;
  state.world = world;
  state.label = d.label || world;
  $('world-title').textContent = `資料フォルダ「${state.label}」`;
  state.folders = d.folders || [];
  applySaved(d.rules || []);
  try {
    const b = await api('GET', `/admin/build-marks?world=${encodeURIComponent(world)}`);
    if (seq !== _loadSeq) return;
    applyBuild(b.marks || []);
    state.layout = new Set(b.layout_folders || []);
  } catch (_) { toast('ビルドの定義の印を読み込めませんでした'); }
  if (d.removed_missing) {
    const b = $('removed-banner');
    b.textContent = `消えた場所の設定を ${d.removed_missing} 件取り除きました`;
    b.hidden = false;
  }
  try { await loadFiles(''); } catch (_) { /* 直下のファイルが読めなくてもフォルダの木は使える */ }
  if (seq !== _loadSeq) return;
  render();
}

// 資料の画面のタブから呼ぶ。保存していない変更があるときは読み直さない（下書きを消さない）。
let _opening = Promise.resolve();
function open() {
  if (isDirty()) return _opening;
  const seq = _loadSeq + 1;
  _opening = (async () => {
    try { await load(); } catch (e) {
      if (seq !== _loadSeq) return;
      resetState();
      showTreeMessage(`<div class="muted" style="padding:14px">読み込めませんでした: ${esc(e.message)}</div>`);
    }
  })();
  return _opening;
}

// 資料の画面の表の「設定」から呼ぶ。① タブを移す ② 読み込みを待つ ③ 絞り込みを外し親を開く ④ その行を見える所へ出す。
// 連打されたときは最後の呼び出しだけが残る。
let _revealSeq = 0;
async function reveal(path, isDir) {
  const seq = ++_revealSeq;
  window.IngestTabs.select('rules');
  await _opening;
  if (seq !== _revealSeq || !state.world) return;
  state.filter = 'all'; state.query = '';
  $('rules-q').value = '';
  const files = state.files;
  const parents = ancestors(path);
  for (const a of parents) state.open.add(a);
  const loaded = await Promise.all(parents.map((a) => loadFiles(a).then(() => true, () => false)));
  if (seq !== _revealSeq || files !== state.files) return;
  state.reveal = { path, dir: !!isDir };
  render();
  if (loaded.includes(false)) toast('ファイルの一覧を読み込めませんでした');
  const row = [...$('rules-tree').querySelectorAll('[data-row]')].find((el) => el.dataset.row === path);
  if (!row) { toast('この場所は「優先と除外」の一覧にありません'); return; }
  row.scrollIntoView({ block: 'center' });
  const sel = [...row.querySelectorAll('select[data-set]')][0];
  if (sel) sel.focus();
}

// 資料フォルダが無くなったときに呼ぶ。下書きは元のフォルダと一緒に無効になるので捨てる。
function discard() {
  _loadSeq++;
  resetState();
  render();
}

bind();
window.RulesTab = { open, isDirty, discard, reveal, onRules };
})();
