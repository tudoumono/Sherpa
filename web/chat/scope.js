// 範囲（フォルダ部分木）セレクタとナレッジ参照・個人ファイル参照トグルを担う。
// 設計: docs/design/scope.md「範囲（scope）＝フォルダ部分木のフィルタ」
// 範囲・参照の状態が変わるたび inquiry.js の要約を更新する（相互 import は関数宣言の hoist を前提とする）。
'use strict';

import { S } from './state.js';
import { toast } from '../chat.js';
import { refreshInquirySummary, setToolsDetailsOpen, toolsExplicitForRestore, normalizeLens } from './inquiry.js';

const $ = Sherpa.$, esc = Sherpa.esc, getJSON = Sherpa.getJSON;

// ===== 範囲セレクタ =====
// 明示選択した範囲を S.scope に持ち、送信時に scope_paths として送る。
let _scopeAvail = false;                // 選べる範囲があるか
// 既定はトップ階層のみ表示し、子持ち行のトグルで展開する。開閉・絞り込みはページ内メモリのみで、ツリー再取得ごとに初期化する。
let _scopeOpen = new Set();             // 手動で開いたフォルダの path 集合
let _scopeFilter = '';                  // 絞り込み入力の現在値
// 入力欄チップ（inquiry.js）も同じ要約語を使う。
export function scopeChipLabel() { return S.scope.length ? S.scope.map((p) => S.scopeLabels[p] || p.split('/').pop()).join('・') : '全体'; }
export function setScopeLabel(text) {
  $('scopelabel').textContent = text;
  refreshInquirySummary();
}
export function updateScopeHeader(scope) {     // answer.scope → 実際に使った範囲を表示
  S.currentScopeMeta = scope || null;    // /scopes 応答の遅延と競合しても消えないよう保持
  const paths = (scope && scope.scope_paths) || [];
  if (!paths.length) return setScopeLabel('全体');
  const labels = paths.map((p) => S.scopeLabels[p] || p.split('/').pop()).join('・');
  setScopeLabel(labels);
}
// 復元する調べ方（investigate／author）を決める。how があればそれ、無い古い回答は explicit は実効レンズ・slash はブロックの継続設定（lens_block）を読み替える。
function _lensToRestore(ans, sc) {
  if (!ans || !sc) return 'investigate';
  if (sc.lens_source === 'slash') return normalizeLens(sc.lens_block);   // スラッシュは 1 回限り＝継続設定へ戻す
  if (ans.how && ans.how.mode) return normalizeLens(ans.how.mode);
  if (sc.lens_source === 'explicit' && ans.lens) return normalizeLens(ans.lens);
  return 'investigate';
}
// 会話を開いたとき、最後の回答の範囲・調べ方・資料中心・検索経路を復元する。
export function applyConversationScope(messages) {
  const last = [...messages].reverse().find((m) => m.role !== 'user' && m.answer);
  const ans = last ? last.answer : null;
  if (ans && ans.lens) setKb(ans.lens !== 'chat');   // 直近が資料参照ならナレッジ参照オンに戻す
  const rawSc = ans ? ans.scope : null;
  // 復元する調べ方を sc のコピーへ埋め込む（後追い復元も S.currentScopeMeta 経由で同じ値を使う）
  const sc = rawSc ? { ...rawSc, lens_restore: _lensToRestore(ans, rawSc),
    doc_focus_restore: !!(ans.how && ans.how.doc_focus) } : null;
  // セレクタを会話の資料フォルダへ合わせる（別フォルダの範囲を送らない）
  const sel = $('version');
  if (sc && sc.world && sel) {
    if ([...sel.options].some((o) => o.value === sc.world)) {
      if (sc.world !== sel.value) { sel.value = sc.world; loadScopes(); }
      S.pendingConvWorld = null;
    } else {
      S.pendingConvWorld = sc.world;   // 選択肢が未読込＝/world-options 側で後追い適用
    }
  }
  const sameDir = !sc || !sc.world || !sel || sc.world === sel.value;
  S.scope = (sameDir && sc && sc.source === 'explicit') ? (sc.scope_paths || []).slice() : [];   // 同じ資料フォルダの明示選択だけ復元
  S.lens = (sameDir && sc) ? sc.lens_restore : 'investigate';
  S.docFocus = !!(sameDir && sc && sc.doc_focus_restore);
  S.depthProfile = (sameDir && sc && sc.depth_profile) ? sc.depth_profile : 'standard';
  S.webSearch = !!(sameDir && sc && sc.web_search);
  // 検索経路トグルの復元（欠落は全ON）。詳細の折りたたみは開き直すたびに閉じる
  S.tools = (sameDir && sc && sc.tools) ? { ...sc.tools } : { grep: true, fulltext: true, graph: true };
  S.toolsExplicit = toolsExplicitForRestore(S.tools, (sameDir && sc) ? sc.tools_explicit : undefined);
  setToolsDetailsOpen(false);
  if (S.scopeTree) renderScopePanel(S.scopeTree);
  updateScopeHeader(sc);
}
function _scopeAncestors(path) {   // "a/b/c" → ["a","a/b"]（自分自身は含まない）
  const parts = path.split('/');
  const out = [];
  for (let i = 1; i < parts.length; i++) out.push(parts.slice(0, i).join('/'));
  return out;
}
function _scopeForest(scopes) {   // 平坦リスト（path で親子が分かる）→ 木構造
  const byPath = new Map(scopes.map((s) => [s.path, { ...s, children: [] }]));
  const roots = [];
  for (const s of scopes) {
    const node = byPath.get(s.path);
    const parentPath = s.path.includes('/') ? s.path.slice(0, s.path.lastIndexOf('/')) : null;
    const parent = parentPath && byPath.get(parentPath);
    (parent || { children: roots }).children.push(node);
  }
  return roots;
}
function _scopeTreeRowsHtml(nodes, openSet) {
  let html = '';
  for (const node of nodes) {
    const on = S.scope.includes(node.path);
    const open = openSet.has(node.path);
    // トグル（開閉）と行本体（選択）は別クリック領域にする
    const toggle = node.children.length
      ? `<button type="button" class="sctoggle" data-toggle="${esc(node.path)}" aria-expanded="${open}">${open ? '▾' : '▸'}</button>`
      : `<span class="sctoggle sctoggle-leaf"></span>`;
    html += `<div class="scoperow-wrap" style="padding-left:${node.depth * 14}px">${toggle}`
      + `<button class="scoperow${on ? ' on' : ''}" data-scope="${esc(node.path)}">`
      + `<span class="sk">${on ? '☑' : '☐'}</span>${esc(node.label)}<span class="sc">${node.count}</span></button></div>`;
    if (node.children.length && open) html += _scopeTreeRowsHtml(node.children, openSet);
  }
  return html;
}
function _scopeFilterRowsHtml(scopes, needle) {   // 絞り込み中は平坦表示＋祖先パスをラベル前置きで示す
  const byPath = new Map(scopes.map((s) => [s.path, s]));
  const q = needle.toLowerCase();
  const matches = scopes.filter((s) => s.label.toLowerCase().includes(q));
  if (!matches.length) return `<div class="scopeempty">一致するフォルダがありません</div>`;
  return matches.map((s) => {
    const on = S.scope.includes(s.path);
    const trail = _scopeAncestors(s.path).map((p) => (byPath.get(p) || {}).label || p.split('/').pop());
    const prefix = trail.length ? `<span class="scopetrail">${trail.map(esc).join(' › ')} › </span>` : '';
    return `<button class="scoperow${on ? ' on' : ''}" data-scope="${esc(s.path)}">`
      + `<span class="sk">${on ? '☑' : '☐'}</span>${prefix}${esc(s.label)}<span class="sc">${s.count}</span></button>`;
  }).join('');
}
function _scopeRowsHtml() {
  const scopes = (S.scopeTree && S.scopeTree.scopes) || [];
  const q = _scopeFilter.trim();
  if (q) return _scopeFilterRowsHtml(scopes, q);
  const openSet = new Set(_scopeOpen);
  S.scope.forEach((p) => _scopeAncestors(p).forEach((a) => openSet.add(a)));   // 選択済みの祖先はつねに開く
  return _scopeTreeRowsHtml(_scopeForest(scopes), openSet);
}
export function renderScopePanel(tree) {
  S.scopeTree = tree;
  $('scopepanel').innerHTML =
    `<button class="scoperow${S.scope.length ? '' : ' on'}" data-scope="">📂 全体（この取込ディレクトリすべて）</button>`
    + `<input id="scopefilter" class="scopefilter" type="text" placeholder="フォルダ名で絞り込み" value="${esc(_scopeFilter)}">`
    + `<div id="scope-rows">${_scopeRowsHtml()}</div>`;
}
function updateScopeVisibility() {   // 範囲セレクタは「ナレッジ参照オン」かつ「選べる範囲あり」のときだけ
  $('scopesel').style.display = (S.kb && _scopeAvail) ? '' : 'none';
  // 調べ方ブロックの行も同じ条件で出し分ける（範囲＝参照ON＋選べる範囲あり・深さ＝参照ON）
  $('scope-row').hidden = !(S.kb && _scopeAvail);
  $('depth-row').hidden = !S.kb;
  refreshInquirySummary();
}
export function setKb(on) {           // ナレッジ参照トグル（既定ON）。オンで範囲指定が選べる
  S.kb = !!on;
  const b = $('kbtoggle'); b.setAttribute('aria-pressed', S.kb ? 'true' : 'false');
  b.classList.toggle('on', S.kb); b.querySelector('b').textContent = S.kb ? 'オン' : 'オフ';
  if (!S.kb) $('scopepanel').hidden = true;
  updateScopeVisibility();
}
$('kbtoggle').addEventListener('click', () => setKb(!S.kb));

// 個人ファイル参照トグル
function setPersonal(on) {
  S.personal = on;
  const b = $('personaltoggle'); if (!b) return;
  b.setAttribute('aria-pressed', on ? 'true' : 'false');
  b.classList.toggle('on', on); b.querySelector('b').textContent = on ? 'オン' : 'オフ';
}
$('personaltoggle').addEventListener('click', () => setPersonal(!S.personal));

// 選択中の資料フォルダの範囲ツリーを取得してパネルを描く。
export async function loadScopes() {
  _scopeOpen.clear(); _scopeFilter = '';
  let tree = null;
  try { tree = await getJSON('/scopes?world=' + encodeURIComponent($('version').value)); } catch (e) { }
  const scopes = (tree && tree.scopes) || [];
  const leaves = scopes.filter((s) => !scopes.some((o) => o.path !== s.path && o.path.startsWith(s.path + '/')));
  _scopeAvail = leaves.length > 1;   // 選べる末端が1つ以下なら範囲指定は出さない
  if (!_scopeAvail) { updateScopeVisibility(); return; }
  S.scopeTree = tree; S.scopeLabels = {}; scopes.forEach((s) => { S.scopeLabels[s.path] = s.label; });
  renderScopePanel(tree);
  if (S.currentScopeMeta) updateScopeHeader(S.currentScopeMeta);
  else setScopeLabel(scopeChipLabel());
  updateScopeVisibility();
}
$('scopebtn').addEventListener('click', (e) => {
  e.stopPropagation();
  const pn = $('scopepanel'); pn.hidden = !pn.hidden;
  if (!pn.hidden && S.scopeTree) renderScopePanel(S.scopeTree);
});
$('scopepanel').addEventListener('click', (e) => {
  const tg = e.target.closest('[data-toggle]');
  if (tg) {   // 開閉のみ（選択は変えない）。#scope-rows だけ再描画する
    // stopPropagation 必須（再描画で外れたノードを外側クリックと誤判定してパネルが閉じる）
    e.stopPropagation();
    const path = tg.dataset.toggle;
    if (_scopeOpen.has(path)) _scopeOpen.delete(path); else _scopeOpen.add(path);
    const rows = $('scope-rows'); if (rows) rows.innerHTML = _scopeRowsHtml();
    return;
  }
  const r = e.target.closest('[data-scope]'); if (!r) return;
  const path = r.dataset.scope;
  if (!path) S.scope = [];                                   // 「全体」＝選択クリア
  else if (S.scope.includes(path)) S.scope = S.scope.filter((p) => p !== path);
  else S.scope = S.scope.concat(path);                        // 複数選択
  // 各行の選択状態だけその場で更新する（連続選択でパネルが閉じない）
  $('scopepanel').querySelectorAll('[data-scope]').forEach((row) => {
    const p = row.dataset.scope;
    const on = p ? S.scope.includes(p) : S.scope.length === 0;   // 「全体」行は未選択時に on
    row.classList.toggle('on', on);
    const sk = row.querySelector('.sk'); if (sk) sk.textContent = on ? '☑' : '☐';
  });
  setScopeLabel(scopeChipLabel());
});
$('scopepanel').addEventListener('input', (e) => {   // 絞り込み入力（入力欄は作り直さずフォーカスを保つ）
  if (e.target.id !== 'scopefilter') return;
  _scopeFilter = e.target.value;
  const rows = $('scope-rows'); if (rows) rows.innerHTML = _scopeRowsHtml();
});
document.addEventListener('click', (e) => {                 // パネル外クリックで閉じる
  if (!$('scopesel').contains(e.target)) $('scopepanel').hidden = true;
});
