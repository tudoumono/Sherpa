// フェーズ6 S8（リファクタリング計画）: 範囲（スコープ）セレクタ・ナレッジ参照/個人ファイル参照トグルの
// ドメインを chat.js から純移動。export は chat.js（entry）側に残る呼び出し元（`/world-options` 読込・
// `$('version')` change リスナー・deep-link 初期化＝地雷6 の文順は entry 側で不変のまま）と、
// history.js 側の呼び出し元（newConversation/openConversation の renderScopePanel/setScopeLabel/
// applyConversationScope）と、stream.js 側の呼び出し元（subscribeTurn の updateScopeHeader）から
// 参照される loadScopes/setScopeLabel/updateScopeHeader/applyConversationScope/renderScopePanel に、
// SC-6b（調べ方ブロック）で web/chat/inquiry.js から参照される scopeChipLabel を加えたものに絞る
// （updateScopeVisibility/setKb/setPersonal・_scopeAvail は本ファイル内で閉じる単一ドメインのため
// 非公開のまま）。
// 個人ファイルの実アップロード（uploadPersonalFilesFromChat・$('chat-upload-btn')/$('chat-file-input')）と
// updateShareButtonState は chat.js に残る（前者は送信欄の一部・後者はどのドメインにも属さない横断
// ユーティリティのため・実依存を grep で確認したうえでの意図的な据え置き）。
// SC-6b: 範囲・探す対象・参照するの3行は同じ調べ方ブロック（#inquiry）に同居し、範囲/kb状態が
// 変わるたびチップ・折りたたみ見出しの要約（web/chat/inquiry.js 担当）も更新が要るため、
// scopeChipLabel（inquiry.js から import）と refreshInquirySummary（本ファイルから import）で
// 相互参照する（chat.js↔scope.js と同じ意図した循環 import・関数宣言＝hoisted のため実行時に
// 呼ぶ限り ESM で安全）。
'use strict';

import { S } from './state.js';
import { toast } from '../chat.js';   // 横断ユーティリティ（menus.js と同じ循環 import パターン）
import { refreshInquirySummary, setToolsDetailsOpen, toolsExplicitForRestore } from './inquiry.js';

const $ = Sherpa.$, esc = Sherpa.esc, getJSON = Sherpa.getJSON;   // 共通ユーティリティ（common.js）

// ===== 範囲（スコープ）セレクタ（D・04-画面の原則.md §3.3）=====
// 明示選択した範囲を S.scope に持ち、送信時に scope_paths として送る。空なら言葉から推定（サーバ）。
let _scopeAvail = false;                // 選べる範囲があるか（scope パネル内で完結・単一ドメイン）
// 折りたたみツリー化（実環境指摘 2026-09-02）: 登録フォルダが67件超などの規模だと平坦一覧は探しづらい
// ため、既定はトップ階層のみ表示し子持ち行のトグルで段階展開する。開閉状態・絞り込み文字列はページ内
// メモリのみ（localStorage 不使用）で、ツリーの再取得（loadScopes＝world 切替を含む）ごとに初期化する
// ——持ち越すと別 world の同名パスが意図せず開いたり、旧絞り込みで新 world のフォルダが隠れる。
let _scopeOpen = new Set();             // 手動で開いたフォルダの path 集合
let _scopeFilter = '';                  // 絞り込み入力の現在値（行だけ再描画する際も保持する）
// SC-6b: 調べ方ブロックの入力欄チップ（web/chat/inquiry.js）も同じ要約語を使うため export。
export function scopeChipLabel() { return S.scope.length ? S.scope.map((p) => S.scopeLabels[p] || p.split('/').pop()).join('・') : '全体'; }
export function setScopeLabel(text) {
  $('scopelabel').textContent = text;
  refreshInquirySummary();   // SC-6b: チップ/折りたたみ見出しの要約（調べ方・範囲・探す対象）も追随させる
}
// S7: stream.js の subscribeTurn（答え受信後の範囲反映）からも呼ばれるため export。
export function updateScopeHeader(scope) {     // answer.scope（world/scope_paths/source）→ 実際に使った範囲を表示
  S.currentScopeMeta = scope || null;    // /scopes 応答の遅延と競合しても消えないよう保持（RV）
  const paths = (scope && scope.scope_paths) || [];
  if (!paths.length) return setScopeLabel('全体');
  const labels = paths.map((p) => S.scopeLabels[p] || p.split('/').pop()).join('・');
  setScopeLabel(labels);   // 鏡では source=explicit/all のみ（auto-scope 推定は撤去）
}
// RV1 #2: `lens_source` から実際に復元すべき調べ方を1箇所で決める。"explicit" は実効レンズ
// （`ans.lens`）、"slash"（1回限りの明示で上書きされた）はブロックの継続設定（`sc.lens_block`）、
// それ以外（"auto"／欠落＝旧回答）は自動に戻す。
function _lensToRestore(ans, sc) {
  if (!ans || !sc) return 'auto';
  if (sc.lens_source === 'explicit' && ans.lens) return ans.lens;
  if (sc.lens_source === 'slash') return sc.lens_block || 'auto';
  return 'auto';
}
export function applyConversationScope(messages) {   // 会話を開いた時、最後の回答の範囲/参照モードを復元
  const last = [...messages].reverse().find((m) => m.role !== 'user' && m.answer);
  const ans = last ? last.answer : null;
  if (ans && ans.lens) setKb(ans.lens !== 'chat');   // 直近が資料参照ならナレッジ参照オンに戻す
  const rawSc = ans ? ans.scope : null;
  // RV1 #4: 調べ方ブロック（lens/layer）の復元は、範囲と同じ sameDir 判定・pendingConvWorld
  // 後追い経路に統合する（独立の sameDir 判定を増やさない）。復元後に使う値を sc のコピーへ
  // 埋め込み、S.currentScopeMeta（下の updateScopeHeader が保持）経由で後追い（chat.js の
  // /world-options 読込）からも同じ値を参照できるようにする。
  const sc = rawSc ? { ...rawSc, lens_restore: _lensToRestore(ans, rawSc) } : null;
  // 会話の取込ディレクトリへセレクタを合わせる（別ディレクトリの範囲を現在のディレクトリに送らない）。
  const sel = $('version');
  if (sc && sc.world && sel) {
    if ([...sel.options].some((o) => o.value === sc.world)) {
      if (sc.world !== sel.value) { sel.value = sc.world; loadScopes(); }   // ディレクトリが変われば範囲ツリーも読み直す
      S.pendingConvWorld = null;
    } else {
      S.pendingConvWorld = sc.world;   // 選択肢が未読込＝/world-options 側で後追い適用（保存値より会話を優先）
    }
  }
  const sameDir = !sc || !sc.world || !sel || sc.world === sel.value;
  S.scope = (sameDir && sc && sc.source === 'explicit') ? (sc.scope_paths || []).slice() : [];   // 同一ディレクトリの明示選択だけ復元
  S.lens = (sameDir && sc) ? sc.lens_restore : 'auto';
  S.layer = (sameDir && sc && sc.layer) ? sc.layer : 'both';
  S.depthProfile = (sameDir && sc && sc.depth_profile) ? sc.depth_profile : 'standard';   // SC-6c: 調べる深さの復元（§4.3・欠落=旧回答は標準）
  S.webSearch = !!(sameDir && sc && sc.web_search);   // WEB-1: 直近回答の Web 検索希望を復元
  // SC-6e: 検索経路トグルの復元（欠落=旧回答は全ON）。折りたたみ自体は会話ごとに永続しない＝
  // 開き直すたびに既定閉へ戻す（inquiry-head の開閉状態＝localStorage 永続とは別軸）。
  S.tools = (sameDir && sc && sc.tools) ? { ...sc.tools } : { grep: true, fulltext: true, graph: true };
  // 復元値が全ON（既定）なら未操作、1軸でもOFFなら明示状態にする
  // （`inquiry.js::toolsExplicitForRestore` 参照・無操作の次送信で非既定値が消えないように）。
  S.toolsExplicit = toolsExplicitForRestore(S.tools);
  setToolsDetailsOpen(false);
  if (S.scopeTree) renderScopePanel(S.scopeTree);
  updateScopeHeader(sc);   // setScopeLabel 経由で refreshInquirySummary() も呼ぶ（チップ/要約を追随）
}
function _scopeAncestors(path) {   // "a/b/c" → ["a","a/b"]（自分自身は含まない）
  const parts = path.split('/');
  const out = [];
  for (let i = 1; i < parts.length; i++) out.push(parts.slice(0, i).join('/'));
  return out;
}
function _scopeForest(scopes) {   // 平坦リスト（path で親子が分かる＝鏡モデルの同一性=パス）→ 木構造
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
    // トグル（開閉）と行本体（選択）を別クリック領域にする（要件3・▸ クリックで選択を変えない）
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
  // 鏡モデルでは scope_tree は {world,label,scopes} のみ（common 概念は撤去）＝旧 tree.common 分岐は撤去（rv-full2 #7）
  const scopes = (S.scopeTree && S.scopeTree.scopes) || [];
  const q = _scopeFilter.trim();
  if (q) return _scopeFilterRowsHtml(scopes, q);
  const openSet = new Set(_scopeOpen);
  S.scope.forEach((p) => _scopeAncestors(p).forEach((a) => openSet.add(a)));   // 選択済みの祖先はつねに開く（要件2）
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
  // SC-6b: 調べ方ブロックの行自体も同じ条件で出し分ける（範囲＝ナレッジ参照ON＋選べる範囲あり・
  // 探す対象＝ナレッジ参照ONのみ・調べ方ブロック §3.3/§3.4「範囲・層は社内資料を参照するがONの
  // ときだけ選べる」）。ラベルだけ残して中身が空の行を見せない。
  $('scope-row').hidden = !(S.kb && _scopeAvail);
  $('layer-row').hidden = !S.kb;
  $('depth-row').hidden = !S.kb;   // SC-6c: 調べる深さも社内資料を参照するがONのときだけ選べる
  refreshInquirySummary();
}
function setKb(on) {                  // ナレッジ参照トグル（既定オフ）。オンで範囲指定が選べる
  S.kb = S.kbLocked ? true : on;      // Codex構成はON固定（decision 2026-08-15）
  const b = $('kbtoggle'); b.setAttribute('aria-pressed', S.kb ? 'true' : 'false');
  b.classList.toggle('on', S.kb); b.querySelector('b').textContent = S.kb ? 'オン' : 'オフ';
  if (!S.kb) $('scopepanel').hidden = true;   // オフにしたら範囲パネルも閉じる（再オンで開きっぱを防ぐ）
  updateScopeVisibility();
}
// Codex 構成は資料参照ON固定（Codex CLI は read-only 実行でも自分で grep できるため、
// 「参照オフのつもりで KB を覗く」状態を作らない）。トグルは見えるが押しても変わらないことを
// 明示するため aria-disabled と注記を付ける。サーバ側でも強制する（routers/chat.py::_knowledge_for）。
export function setKbLocked(locked) {
  S.kbLocked = !!locked;
  const b = $('kbtoggle');
  if (!b) return;
  b.setAttribute('aria-disabled', S.kbLocked ? 'true' : 'false');
  b.classList.toggle('locked', S.kbLocked);
  b.title = S.kbLocked ? 'Codex は常に資料を参照します（切り替えできません）'
    : '社内ナレッジ（資料）を参照する';
  if (S.kbLocked) setKb(true);
}
$('kbtoggle').addEventListener('click', () => {
  if (S.kbLocked) { toast('Codex は常に資料を参照します'); return; }
  setKb(!S.kb);
});

// Feature B/C: 個人ファイル参照トグル（kbtoggle のパターンを踏襲）。
function setPersonal(on) {
  S.personal = on;
  const b = $('personaltoggle'); if (!b) return;
  b.setAttribute('aria-pressed', on ? 'true' : 'false');
  b.classList.toggle('on', on); b.querySelector('b').textContent = on ? 'オン' : 'オフ';
}
$('personaltoggle').addEventListener('click', () => setPersonal(!S.personal));

// S8: chat.js entry 側の `/world-options` 読込（.finally）と `$('version')` change リスナーから
// 呼ばれるため export（deep-link 初期化順＝地雷6 は entry 側の文順で保証・本関数の置き場所には無関係）。
export async function loadScopes() {
  _scopeOpen.clear(); _scopeFilter = '';   // ツリー再取得＝開閉・絞り込みを初期化（上の宣言コメント参照）
  let tree = null;
  try { tree = await getJSON('/scopes?world=' + encodeURIComponent($('version').value)); } catch (e) { }
  const scopes = (tree && tree.scopes) || [];
  const leaves = scopes.filter((s) => !scopes.some((o) => o.path !== s.path && o.path.startsWith(s.path + '/')));
  _scopeAvail = leaves.length > 1;   // 選べる末端が1つ以下なら範囲指定は出さない
  if (!_scopeAvail) { updateScopeVisibility(); return; }
  S.scopeTree = tree; S.scopeLabels = {}; scopes.forEach((s) => { S.scopeLabels[s.path] = s.label; });
  renderScopePanel(tree);
  if (S.currentScopeMeta) updateScopeHeader(S.currentScopeMeta);   // 会話を先に開いていたら範囲表示を維持
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
  if (tg) {   // 開閉のみ（選択は変えない・要件3）。行の再構築が要るため #scope-rows だけ再描画する
    // stopPropagation 必須（#scopebtn と同じ理由）: 再描画でクリックされたトグル自身が DOM から
    // 外れるため、伝播を止めないと document の外側クリック判定（contains(e.target)）が
    // detached ノードを「外」と誤判定してパネルを閉じてしまう。
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
  // 作り直さず**その場で各行の選択状態だけ更新**（連続選択でパネルが閉じない）
  $('scopepanel').querySelectorAll('[data-scope]').forEach((row) => {
    const p = row.dataset.scope;
    const on = p ? S.scope.includes(p) : S.scope.length === 0;   // 「全体」行は未選択時に on
    row.classList.toggle('on', on);
    const sk = row.querySelector('.sk'); if (sk) sk.textContent = on ? '☑' : '☐';
  });
  setScopeLabel(scopeChipLabel());
});
$('scopepanel').addEventListener('input', (e) => {   // 絞り込み入力（要件4）。入力欄自体は作り直さずフォーカスを保つ
  if (e.target.id !== 'scopefilter') return;
  _scopeFilter = e.target.value;
  const rows = $('scope-rows'); if (rows) rows.innerHTML = _scopeRowsHtml();
});
document.addEventListener('click', (e) => {                 // パネル外クリックで閉じる
  if (!$('scopesel').contains(e.target)) $('scopepanel').hidden = true;
});
