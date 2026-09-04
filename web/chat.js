// チャット主入口（M9・04-画面の原則.md §3）。SSE で「思考の流れ」を右ペインに流し、答え先頭カード＋出典（原本DL）を中央に描く。
// セキュリティ: server data は全て esc()。インライン handler にデータを載せず、委譲＋data-*（固定キー/esc済）で扱う。
'use strict';

// フェーズ6（リファクタリング計画）: module 化＋分割の最終形。共有状態/定数は web/chat/state.js、
// 共有ダイアログは web/chat/share-dialog.js、回答カード・trace/turn stack・welcome・出典は
// web/chat/render.js、会話履歴（一覧・open/new/rename/pin/delete・背景実行再購読）は
// web/chat/history.js、SSE 購読・flow ライブ描画・停止フロー・送信中枢は web/chat/stream.js、
// 範囲（スコープ）セレクタ・ナレッジ参照/個人ファイル参照トグルは web/chat/scope.js、
// brain-menu・フォント・エクスポート・テーマは web/chat/menus.js へそれぞれ純移動済み（S1〜S8）。
// このエントリに残るのは: state/全モジュールの import・init（deep-link `?conv=` と
// `/world-options` の初期化順＝危険地雷6・文順を変えない）・input/send 中枢・#messages 委譲
// リスナー・個人ファイルアップロード（送信欄の一部）・updateShareButtonState/toast/copyText
// （どのドメインにも属さない横断ユーティリティ・実依存を grep で確認したうえでの意図的な据え置き）・
// 3カラムレイアウト（目標構成のどのモジュールにも割り当てが無いドメイン）・
// window.__sherpaChatTest テスト seam。scope.js/menus.js は toast/updateShareButtonState をこの
// エントリファイルから相対 import する意図した循環 import（関数宣言＝hoisted のため実行時に呼ぶ限り
// ESM で安全＝render.js の setRt・history.js の copyText と同じパターン）。
import { S, EXAMPLES } from './chat/state.js';
import { openShareDialog } from './chat/share-dialog.js';
import { welcome, initRefGraph } from './chat/render.js';
import {
  loadConversations, deleteConversation, togglePin, renameConversation,
  newConversation, openConversation, resumeRunningTurn,
} from './chat/history.js';
import { send, sendOrStop, _closeOtherTurns } from './chat/stream.js';
import { loadScopes, renderScopePanel, setScopeLabel, scopeChipLabel } from './chat/scope.js';
import { setLayer, setDepthProfile, setTools, setToolsAvailability, resetInquiryForNewConversation, refreshInquirySummary, toolsExplicitForRestore } from './chat/inquiry.js';
import { applyCachedBrain, loadConfig, exportMessages } from './chat/menus.js';

const $ = Sherpa.$, esc = Sherpa.esc;   // 共通ユーティリティ（nav.js・RV DRY）

// ===== 委譲 =====
// #2: 影響一覧の行展開/折りたたみ（行全体が role=button・aria-expanded で開閉。max-height は app.css 側）
// セレクタは .ilist 内の data-toggle に限定（refgraph-h は data-rg・別ハンドラ＝衝突させない）
$('messages').addEventListener('click', async (e) => {
  const tg = e.target.closest('.ilist [data-toggle]');
  if (tg) {
    const li = tg.closest('li'); if (!li) return;
    const open = li.classList.toggle('open');
    tg.setAttribute('aria-expanded', open ? 'true' : 'false'); return;
  }
  const dl = e.target.closest('[data-dl]');
  if (dl) {
    e.preventDefault();
    const r = await fetch(dl.getAttribute('href'));
    if (!r.ok) { alert((await r.json().catch(() => ({}))).detail || '原本は未取り込みです'); return; }
    const blob = await r.blob();
    // 保存名は doc_id（rel_path）末尾＝原本のファイル名（深い階層でも綺麗な名前に・サーバの Content-Disposition basename と一致）
    const name = dl.textContent.replace(/^📄\s*/, '').split('/').filter(Boolean).pop() || 'download';
    Sherpa.downloadBlob(blob, name);   // UI フィードバック3: revoke タイミング問題を共通ヘルパで回避
  }
});
// #2: 行トグル（role=button）のキーボード操作。実クリック処理へ委譲（ロジック一本化・セレクタは同様に .ilist 内に限定）
$('messages').addEventListener('keydown', (e) => {
  if (e.key !== 'Enter' && e.key !== ' ' && e.key !== 'Spacebar') return;
  const tg = e.target.closest('.ilist [data-toggle][role="button"]'); if (!tg) return;
  e.preventDefault(); tg.click();
});
// RV2 #1: chat_router._SLASH_LENS の逆写像（実効レンズ→スラッシュ語）。確認カードが
// lens_source==="slash" のとき、再送本文の先頭へ元の接頭辞を復元して既存のスラッシュ解決経路
// （サーバ側 _resolve_lens の extract_slash_lens）へそのまま乗せるために使う。
const _SLASH_WORD_FOR_LENS = { impact: '影響', troubleshoot: '原因', qa: '内容', author: '作成' };
// AI/tool からの確認カード: 選択内容を同じ会話の次メッセージとして送る
$('messages').addEventListener('click', (e) => {
  const btn = e.target.closest('[data-ask-submit]'); if (!btn) return;
  const msg = btn.closest('.msg'), q = msg && msg._question;
  if (!q) return;
  const picked = [...msg.querySelectorAll('[data-qopt]:checked')].map((x) => x.dataset.label || x.value);
  const freeEl = msg.querySelector('[data-qfree]');
  const free = freeEl ? freeEl.value.trim() : '';
  if (!picked.length && !free) { toast('選択してください'); return; }
  const lines = [`確認事項: ${q.prompt || ''}`];
  if (q.interaction_id) lines.push(`確認ID: ${q.interaction_id}`);   // router clarify(lens-*)の識別＝再質問ループ防止（generic ask_user と区別）
  if (picked.length) lines.push(`選択: ${picked.join('、')}`);
  if (free) lines.push(`補足: ${free}`);
  if (q.original_message) lines.push(`元の依頼: ${q.original_message}`);
  msg.querySelectorAll('input,textarea,button').forEach((x) => { x.disabled = true; });
  // RV1 #3/RV2 #1/SC-6e: 「確認してから進めて」の確認カード（interaction_id が confirm-*）は、
  // 確認が出た時点で解決済みだった調べ方・探す対象・範囲・検索経路トグルを payload に持つ
  // （chat_router.confirm_first_question）。回答の再送は1回だけそれへ戻す（ブロックの継続設定
  // S.lens/S.layer/S.scope/S.tools は変えない）。`lens_source==="slash"` は実効レンズ（q.lens）を
  // ChatReq.lens として直接送ると「1回限り」契約が崩れる（次に会話を開き直すと explicit 扱いに
  // なる）ため、既存のスラッシュ接頭辞（/影響 等）を再送本文の先頭へ復元し、送信 override の
  // lens にはブロックの継続設定（q.lens_block）を渡して既存の _resolve_lens 経路へそのまま乗せる。
  // lens 選択の確認カード（interaction_id が lens-*）は本文の「選択:」から chat_router 側で
  // 解決するため対象外（通常どおり send() を呼ぶ）。
  const isConfirmFirst = typeof q.interaction_id === 'string' && q.interaction_id.startsWith('confirm-');
  let resendText = lines.join('\n');
  let overrideLens = q.lens;
  if (isConfirmFirst && q.lens_source === 'slash' && _SLASH_WORD_FOR_LENS[q.lens]) {
    resendText = `/${_SLASH_WORD_FOR_LENS[q.lens]} ${resendText}`;
    overrideLens = q.lens_block;
  }
  $('input').value = resendText;
  send(isConfirmFirst ? { lens: overrideLens, layer: q.layer, scope_paths: q.scope_paths, tools: q.tools } : undefined);
});
// UIフィードバック（2026-07-03）: 過去ターンの「思考の流れ」ボタン → 右ペインの該当ターンを
// 展開してスクロール（積み上げ表示に統合・別表示への切替はしない）。
$('messages').addEventListener('click', (e) => {
  const btn = e.target.closest('[data-showtrace]'); if (!btn) return;
  const msg = btn.closest('.msg'); if (!msg || !msg._turnId) return;
  const turnEl = document.getElementById(msg._turnId); if (!turnEl) return;
  if (turnEl.tagName === 'DETAILS') { _closeOtherTurns(turnEl); turnEl.open = true; }
  turnEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
});
// 質問例クリック → 入力欄に流し込む（自動送信せず、編集してから送れる）
$('messages').addEventListener('click', (e) => {
  const ex = e.target.closest('[data-ex]'); if (!ex) return;
  $('input').value = EXAMPLES[Number(ex.dataset.ex)] || '';
  $('input').focus();
  $('input').setSelectionRange(0, $('input').value.length);   // 置き換え対象を選択状態に
});
// UI フィードバック2: 引用（該当箇所）カードの折りたたみ（refgraph と同じ見出しボタン開閉パターン）。
$('messages').addEventListener('click', (e) => {
  const h = e.target.closest('[data-cites]'); if (!h) return;
  const body = h.parentNode.querySelector('.cites-body');
  const open = !body.hidden;
  body.hidden = open; h.querySelector('.caret').textContent = open ? '▾' : '▴';
  h.setAttribute('aria-expanded', open ? 'false' : 'true');
});
// 参照したナレッジグラフの折りたたみ（#5・開いた時だけ cytoscape を遅延 init）
$('messages').addEventListener('click', (e) => {
  const rg = e.target.closest('[data-rg]'); if (!rg) return;
  const body = rg.parentNode.querySelector('.refgraph-body');
  const open = !body.hidden;
  body.hidden = open; rg.querySelector('.caret').textContent = open ? '▾' : '▴';
  if (!open && !body._cy) {
    body.innerHTML = '<div class="rg-canvas"></div>';
    try { body._cy = initRefGraph(body.querySelector('.rg-canvas'), JSON.parse(rg.dataset.rg)); }
    catch (err) { body.innerHTML = '<div class="muted" style="padding:10px">グラフを表示できませんでした</div>'; }
    setTimeout(() => { if (body._cy) { body._cy.resize(); body._cy.fit(undefined, 16); } }, 40);
  }
});
// 回答フィードバック（👍/👎＋定型タグ/一言）。👍は即送信・👎はタグ/一言のポップを開閉する。
// 送信は所有会話のみ許可（サーバ側 403）＝共有された会話を開いている場合はエラー toast にする。
async function _sendFeedback(btn, rating, tags, comment) {
  const wrap = btn.closest('.msg-feedback');
  const msg = btn.closest('.msg');
  const mid = msg && msg._messageId;
  if (!mid || !S.cid || !wrap) return;
  try {
    const fb = await Sherpa.api('POST', `/chat/${S.cid}/messages/${mid}/feedback`, { rating, tags, comment });
    wrap.querySelectorAll('.fbbtn').forEach((b) => b.classList.toggle('on', b.dataset.fb === fb.rating));
    wrap.querySelector('.fbpanel').hidden = true;
    wrap.querySelector('.fbthanks').hidden = false;
  } catch (err) {
    toast(err.message || 'フィードバックを送信できませんでした');
  }
}
$('messages').addEventListener('click', (e) => {
  const fb = e.target.closest('[data-fb]');
  if (fb) {
    if (fb.dataset.fb === 'down') {
      const panel = fb.closest('.msg-feedback').querySelector('.fbpanel');
      panel.hidden = !panel.hidden;
      return;
    }
    _sendFeedback(fb, 'up', [], '');
    return;
  }
  const send = e.target.closest('[data-fb-send]'); if (!send) return;
  const wrap = send.closest('.msg-feedback');
  const tags = [...wrap.querySelectorAll('.fbtags input:checked')].map((x) => x.value);
  const comment = wrap.querySelector('.fbcomment').value.trim().slice(0, 500);
  _sendFeedback(wrap.querySelector('[data-fb="down"]'), 'down', tags, comment);
});
// SC-6d（出典0件時の再検索案内・調べ方ブロック §5）: 案内ボタンを押すと該当設定を広げ、
// 直前の質問（この回答の1つ手前の user 発言）をそのまま同じ内容で再送する
// （「言葉から推定しない」原則どおり、利用者が選んで1回クリックで再検索・依頼の設計）。
$('messages').addEventListener('click', (e) => {
  const btn = e.target.closest('.retry-hint-btn'); if (!btn) return;
  const msg = btn.closest('.msg'); if (!msg) return;
  // RV1 #11: 壊れた data-retry-action を `{}` へ黙って縮退させない——解析失敗はここで例外にして
  // 止める（「操作が成功したように見える」silent fallback を作らない）。
  const action = JSON.parse(btn.dataset.retryAction);
  // Codex タイムアウト継続（続きを調べる注記のボタン）: 直前の質問を広げて再送する他の kind とは
  // 別系統——scope 等は変えず固定文言をそのまま送るだけ（resume はサーバ側の codex_session_id
  // 継続に委ねる・chat_service._finalize が付与する action.message をそのまま入力欄へ入れて送信）。
  if (btn.dataset.retryKind === 'resume') {
    // action.message が無い/非文字列の壊れた data-retry-action を汎用（scope 拡大）経路へ
    // フォールスルーさせない——scope_paths 等を持たない action なので誤って「範囲を全体に
    // 広げて再送」と解釈されてしまう（黙って縮退させない・上の RV1 #11 と同じ精神）。
    if (typeof action.message !== 'string') {
      throw new Error(`resume retry hint の action.message が文字列ではありません: ${JSON.stringify(action)}`);
    }
    // 送信中/購読中に入力欄を上書きしない（他ターン進行中に書きかけの下書きを消さない）。
    // send() 自体も同じ条件で二重送信を防ぐが、それより前に $('input').value を書き換えると
    // 下書きが消えたまま何も送信されない事故になるため、代入前にここで弾く。
    if (S.es || S.sending) return;
    $('input').value = action.message;
    send();
    return;
  }
  let prev = msg.previousElementSibling;
  while (prev && !prev.classList.contains('user')) prev = prev.previousElementSibling;
  const bubble = prev && prev.querySelector('.bubble-user');
  if (!bubble) return;
  // RV1 #7: まず元回答（msg._answer.scope）の設定を基準にし、選択された1軸だけを広げて送信する
  // （現在のブロック設定＝この回答の後に変わっているかもしれない値を直接広げない）。
  const origScope = (msg._answer && msg._answer.scope) || {};
  const scopePaths = Object.prototype.hasOwnProperty.call(action, 'scope_paths')
    ? (action.scope_paths || []) : (origScope.scope_paths || []);
  const layer = Object.prototype.hasOwnProperty.call(action, 'layer') ? action.layer : (origScope.layer || 'both');
  // SC-6c: 調べる深さの軸（action.depth_profile）も範囲/探す対象と同型で反映する。
  const depthProfile = Object.prototype.hasOwnProperty.call(action, 'depth_profile')
    ? action.depth_profile : (origScope.depth_profile || 'standard');
  // SC-6e: 検索経路トグルの軸（action.tools）も同型で反映する（欠落=元回答の値・さらに無ければ全ON）。
  const tools = Object.prototype.hasOwnProperty.call(action, 'tools')
    ? action.tools : (origScope.tools || { grep: true, fulltext: true, graph: true });
  S.scope = scopePaths.slice();
  if (S.scopeTree) renderScopePanel(S.scopeTree);
  setScopeLabel(scopeChipLabel());   // 既存 setter（scope.js）を再利用
  setLayer(layer);                  // 既存 setter（inquiry.js）を再利用
  setDepthProfile(depthProfile);    // 既存 setter（inquiry.js）を再利用
  setTools(tools);                  // 既存 setter（inquiry.js）を再利用
  $('input').value = bubble.textContent;
  send();
});
// A2: 思考ステップの detail 履歴を開閉（<button> なので Enter/Space はブラウザが click に変換＝キーボード対応）。
$('flow').addEventListener('click', (e) => {
  const h = e.target.closest('.fhist'); if (!h) return;
  const step = h.closest('.fstep'); if (!step) return;
  const open = step.classList.toggle('hist-open');
  h.setAttribute('aria-expanded', open ? 'true' : 'false');
});
$('convlist').addEventListener('click', (e) => {
  const rn = e.target.closest('[data-rename]'); if (rn) { e.stopPropagation(); return renameConversation(Number(rn.dataset.rename), rn.dataset.title || ''); }
  const del = e.target.closest('[data-del]'); if (del) { e.stopPropagation(); return deleteConversation(Number(del.dataset.del)); }
  const pin = e.target.closest('[data-pin]'); if (pin) { e.stopPropagation(); return togglePin(Number(pin.dataset.pin), pin.dataset.pinned !== '1'); }
  const sh = e.target.closest('[data-sharecid]'); if (sh) { e.stopPropagation(); return openShareDialog(Number(sh.dataset.sharecid), sh.dataset.title || ''); }
  const c = e.target.closest('[data-open]');
  if (c) {
    if (c.dataset.inactive === '1') {
      // 期限切れ/取消済みは内容を開かず状態メッセージのみ表示。
      toast('この共有は期限切れまたは取消済みのため開けません');
      return;
    }
    openConversation(Number(c.dataset.open));
  }
});
// 現在の会話はヘッダのタイトルクリックでも改名できる
$('conv-title').addEventListener('click', () => { if (S.cid) renameConversation(S.cid, $('conv-title').textContent); });
$('conv-title').style.cursor = 'pointer'; $('conv-title').title = 'クリックで名前を変更';
$('newbtn').addEventListener('click', newConversation);
// 共有ボタン（ヘッダ）: 現在の会話があれば共有ダイアログを開く。
// Feature C: 個人コンテンツを含む会話はボタン disabled＋ガードで拒否。
$('sharebtn').addEventListener('click', () => {
  if (!S.cid) { toast('共有したい会話を開いてください'); return; }
  if (S.convHasPersonal) { toast('個人ファイルを参照した会話は共有できません'); return; }
  openShareDialog(S.cid, $('conv-title').textContent || '会話');
});

$('send').addEventListener('click', sendOrStop);
$('input').addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } });

// コピー（メッセージ全体）。secure context 外では textarea フォールバック。
// export: web/chat/share-dialog.js の共有URLコピーボタンから参照される
// （toast() 依存があるため chat.js に留め置き・実依存を grep で確認したうえでの意図的な循環 import。
// copyText は関数宣言＝hoisted のため、chat.js↔share-dialog.js の相互 import でも安全に解決できる）。
export async function copyText(text) {
  try { await navigator.clipboard.writeText(text); }
  catch { const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select(); try { document.execCommand('copy'); } catch (e) { } ta.remove(); }
  toast('コピーしました');
}
export function toast(msg) {
  const t = $('toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 1500);
}
$('messages').addEventListener('click', (e) => {
  const cp = e.target.closest('[data-copy]');
  if (!cp) return;
  const msg = cp.closest('.msg');
  if (msg.classList.contains('user')) { copyText(msg.querySelector('.bubble-user').textContent); return; }
  const clone = cp.closest('.a-body').cloneNode(true);
  clone.querySelectorAll('.copybtn,.chips').forEach((el) => el.remove());
  // UIフィードバック（AI回答のMarkdown表示）: .headline は mdLite() で HTML 整形して描画しているため、
  // textContent 抽出だと **/`` 等の記法が失われる。コピーは生テキストのまま（変換しない）にするため、
  // 見出し部分だけ元データ（_answer.headline）に差し替えてから抽出する。
  if (msg._answer && typeof msg._answer.headline === 'string') {
    const h = clone.querySelector('.headline');
    if (h) h.textContent = msg._answer.headline;
  }
  copyText(clone.textContent.trim());
});

// Feature B/C: 個人ファイルのアップロード（送信欄の一部＝入力/送信中枢に同居。参照トグルの
// setPersonal/setKb は範囲ドメイン＝web/chat/scope.js の担当）。
async function uploadPersonalFilesFromChat(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;
  const btn = $('chat-upload-btn');
  const status = $('chat-upload-status');
  if (btn) btn.disabled = true;
  if (status) {
    status.setAttribute('aria-busy', 'true');
    status.innerHTML = '<span class="loading-inline"><span class="spinner spinner-sm"></span><span>個人ワークスペースへ保存しています...</span></span>';
  }
  const ok = [], ng = [];
  for (const file of files) {
    const fd = new FormData();
    fd.append('file', file, file.name);
    try {
      const r = await fetch('/workspace/files', { method: 'POST', body: fd });
      let data = null;
      try { data = await r.json(); } catch (_) { data = null; }
      if (!r.ok) throw new Error((data && (data.detail || data.message)) || `エラー (${r.status})`);
      ok.push((data && data.rel_path) || file.name);
    } catch (e) {
      ng.push(`${file.name}: ${e.message}`);
    }
  }
  if (status) {
    status.setAttribute('aria-busy', 'false');
    if (ok.length) {
      status.textContent = `${ok.join(', ')} を個人ワークスペースへ保存しました。参照するには「個人ファイル参照」をオンにしてください。参照した会話は共有できません。`;
    } else {
      status.textContent = `アップロードできませんでした。${ng[0] || 'ファイル形式やサイズを確認してください。'}`;
    }
  }
  if (ok.length) toast(`${ok.length} 件を個人ファイルへ保存しました`);
  if (ng.length) toast(`${ng.length} 件のアップロードに失敗しました`);
  if (btn) btn.disabled = false;
}

$('chat-upload-btn').addEventListener('click', () => $('chat-file-input').click());
$('chat-file-input').addEventListener('change', (e) => {
  uploadPersonalFilesFromChat(e.target.files);
  e.target.value = '';
});

// Feature C: 共有ボタンの enabled/disabled 状態を更新する（個人コンテンツ含む会話は共有不可）。
// history.js（newConversation/openConversation）・stream.js（subscribeTurn）から呼ばれるため export。
export function updateShareButtonState() {
  const btn = $('sharebtn');
  const note = $('personal-blocked-note');
  if (!btn) return;
  if (S.convHasPersonal) {
    btn.disabled = true;
    btn.title = '個人ファイルを参照した会話は共有できません';
    if (note) note.style.display = '';
  } else {
    btn.disabled = false;
    btn.title = 'この会話を共有';
    if (note) note.style.display = 'none';
  }
}

// ===== 3カラムのレイアウト（CSS Grid・全画面/リサイズ対応・ユーザ幅保持・左右折りたたみ）=====
const _cols = { L: 264, R: 300 };                                 // ユーザ設定の左右幅（px・保持）
let _sideOpen = localStorage.getItem('sherpa-sidebar') !== '0';   // 左サイドバー（既定 開）
let _rightOpen = localStorage.getItem('sherpa-right') !== '0';    // 右カラム（既定 開）
const _clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const _app = document.querySelector('.app');
const _setvar = (k, v) => document.documentElement.style.setProperty(k, v);
function _limits() {                                              // 最小=固定／最大=画面比（max≥min 保証）／中央の最小
  const W = innerWidth;
  return { Lmin: 200, Lmax: Math.max(200, Math.min(460, Math.round(W * 0.34))),
    Rmin: 280, Rmax: Math.max(280, Math.min(480, Math.round(W * 0.34))), CMIN: 460 };
}
// RV1 #5: `preferRight`（チップから明示的に右ペインを開いた操作専用）は、狭幅で中央最小幅
// （CMIN）を確保できない場合に既存の「右→左→右畳み→左畳み」の縮小順を「左を畳む→中央最小幅を
// 残幅へクランプ」に変える——通常の開閉（ヘッダの開く/閉じるボタン・ドラッグ）はレイアウト計算が
// 右トラックを 0 に戻す既存の挙動のまま（回帰させない）。
function updateLayout(opts) {              // 画面幅に合わせて3トラックを再計算（横スクロールを出さない）
  const preferRight = !!(opts && opts.preferRight);
  const W = innerWidth, lm = _limits();
  let L = _sideOpen ? _clamp(_cols.L, lm.Lmin, lm.Lmax) : 0;
  let R = _rightOpen ? _clamp(_cols.R, lm.Rmin, lm.Rmax) : 0;
  let cmin = lm.CMIN;
  const need = () => L + (L > 0 ? 5 : 0) + R + (R > 0 ? 5 : 0) + cmin - W;   // 中央最小を確保した超過量
  if (preferRight && R > 0) {
    if (need() > 0 && L > 0) L = Math.max(0, L - need());         // 右を優先し、まず左を畳む
    if (need() > 0) cmin = Math.max(240, cmin - need());          // 中央は入力が読める最小幅（240px）を死守する
    if (need() > 0) R = Math.max(lm.Rmin, R - need());            // 次に右を最小幅まで縮める
    if (need() > 0) { R = 0; cmin = lm.CMIN; }                    // 極端な狭さでは右を諦める（中央が1文字幅まで潰れて
                                                                  // 入力の吹き出しが縦書き状に崩れる実害の方が大きい）
  } else {
    if (need() > 0 && R > 0) R = Math.max(lm.Rmin, R - need());   // まず右を最小まで縮める
    if (need() > 0 && L > 0) L = Math.max(lm.Lmin, L - need());   // 次に左を最小まで（中央優先）
    if (need() > 0 && R > 0) R = 0;                               // それでも無理なら右を折りたたむ
    if (need() > 0 && L > 0) L = 0;                               // 極端に狭ければ左も
  }
  _setvar('--tL', L + 'px'); _setvar('--tSL', (L > 0 ? 5 : 0) + 'px');
  _setvar('--tR', R + 'px'); _setvar('--tSR', (R > 0 ? 5 : 0) + 'px');
  _app.classList.toggle('lzero', L === 0); _app.classList.toggle('rzero', R === 0);
}
let _layoutRAF = 0;
function scheduleLayout() { if (_layoutRAF) return; _layoutRAF = requestAnimationFrame(() => { _layoutRAF = 0; updateLayout(); }); }
function setupSplitter(el, side) {
  if (!el || (side !== 'L' && side !== 'R')) return;
  el.addEventListener('pointerdown', (e) => {
    e.preventDefault(); el.setPointerCapture(e.pointerId); el.classList.add('drag');
    const lm = _limits(), startX = e.clientX, startW = _cols[side];
    const [min, max] = side === 'L' ? [lm.Lmin, lm.Lmax] : [lm.Rmin, lm.Rmax];
    const move = (ev) => { _cols[side] = _clamp(startW + (side === 'L' ? ev.clientX - startX : startX - ev.clientX), min, max); updateLayout(); };
    const up = () => { el.classList.remove('drag'); document.removeEventListener('pointermove', move); document.removeEventListener('pointerup', up);
      try { localStorage.setItem('sherpa-cols', JSON.stringify(_cols)); } catch (e) { } };
    document.addEventListener('pointermove', move); document.addEventListener('pointerup', up);
  });
}
function setSidebar(open) { _sideOpen = open; try { localStorage.setItem('sherpa-sidebar', open ? '1' : '0'); } catch (e) { } updateLayout(); }
// SC-6b: 調べ方ブロックの入力欄チップ（web/chat/inquiry.js）が「右ペインが閉じている/狭幅」でも
// 必ずブロックへ到達できるよう export する（chat.js↔scope.js と同じ意図した循環 import）。
// `opts`（省略可）は updateLayout() へそのまま渡す（`{preferRight:true}` は RV1 #5・チップ専用）。
export function setRight(open, opts) { _rightOpen = open; try { localStorage.setItem('sherpa-right', open ? '1' : '0'); } catch (e) { } updateLayout(opts); }
(function initLayout() {
  try { const c = JSON.parse(localStorage.getItem('sherpa-cols') || 'null'); if (c) { _cols.L = c.L || _cols.L; _cols.R = c.R || _cols.R; } } catch (e) { }
  setupSplitter($('splitL'), 'L'); setupSplitter($('splitR'), 'R');
  $('sideclose').addEventListener('click', () => setSidebar(false));
  $('sideopen').addEventListener('click', () => setSidebar(true));
  const rc = $('rightclose'), ro = $('rightopen');
  if (rc) rc.addEventListener('click', () => setRight(false));
  if (ro) ro.addEventListener('click', () => setRight(true));
  addEventListener('resize', scheduleLayout);                     // 通常リサイズ
  document.addEventListener('fullscreenchange', scheduleLayout);  // 全画面切替
  updateLayout();
})();

// 回答単位の書き出し（Markdown・#messages 委譲＝exportMessages は web/chat/menus.js から import）
$('messages').addEventListener('click', (e) => {
  const ex = e.target.closest('[data-export]'); if (!ex) return;
  const msg = ex.closest('.msg');
  if (msg && msg._answer) exportMessages(($('conv-title').textContent || 'chat') + '_回答', [{ role: 'assistant', answer: msg._answer }], 'md');
  else toast('この回答は書き出せません');
});

// ユーザー表示・ログアウトは全ページ共通の上部ナビ（nav.js の #topbar-user ドロップダウン）へ移動済み。

applyCachedBrain();   // 前回のモデル/プロバイダを即反映（その後 loadConfig がサーバ値で確定）
// トップバーの「⏳ 回答作成中」インジケータ（nav.js）から `chat.html?conv=<id>` で遷移してきた場合、
// その会話を自動で開く（実行中ターンがあれば openConversation → resumeRunningTurn が自動再購読する）。
{
  const _convParam = Number(new URLSearchParams(location.search).get('conv'));
  if (_convParam) { openConversation(_convParam); } else { welcome(); resetInquiryForNewConversation(); loadConversations(); }
}
loadConfig();

// SC-6e: 検索経路トグルの実接続可用性（実行側と同じ判定関数・不達なら設定を待たず
// チップ自体を出さない）。失敗時は楽観的な既定（全ON扱い）のまま据え置く（fail-open・
// サーバ側の 422/graceful degrade が最終防衛線のため画面が固まることはない）。
fetch('/chat/tools-availability').then((r) => r.json()).then(setToolsAvailability).catch(() => { });

// 取込ディレクトリ選択肢を /world-options（ログイン必須・admin 不要）から読む（/worlds は admin 専用のため）。
fetch('/world-options').then((r) => r.json()).then((d) => {
  const names = d.worlds || [];
  const lbls = d.labels || {};
  S.verLabels = {}; names.forEach((n) => { S.verLabels[n] = lbls[n] || n; });
  const sel = $('version');
  if (sel) {
    sel.innerHTML = names.length
      ? names.map((n) => `<option value="${esc(n)}">${esc(lbls[n] || n)}</option>`).join('')
      : '<option value="">（資料フォルダ未登録）</option>';
    // 資料フォルダは全体で1本（決定 2026-08-15）＝選ぶ余地が無いので選択UIは出さない。
    // select 自体は送信 body（stream.js）と範囲ツリー（scope.js）が読む値として残す。
    const box = sel.closest('.verselect');
    if (box) box.style.display = names.length > 1 ? '' : 'none';
    try {                                                   // 復元の優先順: 会話の world（deep-link）＞ 前回の明示選択 ＞ 先頭
      const saved = localStorage.getItem('sherpa-world');
      const want = (S.pendingConvWorld && names.includes(S.pendingConvWorld)) ? S.pendingConvWorld
        : (saved && names.includes(saved)) ? saved : null;
      if (want) sel.value = want;
      if (saved && !names.includes(saved)) localStorage.removeItem('sherpa-world');   // 削除済みフォルダの残骸掃除
    } catch (_) { /* no-op */ }
    if (S.pendingConvWorld) {                                // 会話復元が先に走っていた場合の後追い（範囲・調べ方・探す対象の明示選択も復元）
      const sc = S.currentScopeMeta;
      if (sel.value === S.pendingConvWorld && sc && sc.world === S.pendingConvWorld) {
        S.scope = (sc.source === 'explicit') ? (sc.scope_paths || []).slice() : [];
        // RV1 #4: 調べ方（lens）・探す対象（layer）も同じ後追い経路で復元する（scope.js の
        // applyConversationScope が sc.lens_restore を計算済み・独立の sameDir 判定を増やさない）。
        S.lens = sc.lens_restore || 'auto';
        S.layer = sc.layer || 'both';
        S.depthProfile = sc.depth_profile || 'standard';   // SC-6c: 同じ後追い経路で調べる深さも復元する
        S.webSearch = !!sc.web_search;   // WEB-1: 同じ後追い経路で Web 検索希望も復元する
        S.tools = sc.tools || { grep: true, fulltext: true, graph: true };   // SC-6e: 同じ後追い経路で検索経路トグルも復元する
        S.toolsExplicit = toolsExplicitForRestore(S.tools);   // 復元値が非既定なら明示状態にする（scope.js と同じ規則）
        refreshInquirySummary();
      }
      S.pendingConvWorld = null;                             // 選択肢に無い（削除済み）場合もここで諦める
    }
  }
  const v = document.querySelector('.verselect');
  if (v && names.length <= 1) v.style.display = 'none';   // 1つ（または未登録）ならセレクタは隠す
}).catch(() => { }).finally(() => loadScopes());
// 取込ディレクトリを切替えたら範囲をクリアして読み直す（別ディレクトリに古い範囲を送らない）。
// 選択は端末ローカルに記憶（複数フォルダ運用で毎回選び直さない）。会話復元による自動切替（updateScopeHeader）は
// change を発火しない＝ユーザーの明示選択だけを記憶する。
$('version').addEventListener('change', () => {
  try { localStorage.setItem('sherpa-world', $('version').value); } catch (_) { /* no-op */ }
  S.scope = []; S.scopeTree = null; S.scopeLabels = {}; S.currentScopeMeta = null;
  loadScopes();
});
// グラフからの「この語で影響を調べる」を受け取って入力に流し込む
const _ask = localStorage.getItem('sherpa-ask');
if (_ask) { localStorage.removeItem('sherpa-ask'); $('input').value = _ask; $('input').focus(); }

// ===== テスト専用 seam（リファクタリング計画フェーズ6 S1）=====
// e2e（Playwright）はこれまで chat.js の内部変数・関数へ page.evaluate() で bare identifier
// として直接触れていた（classic script のトップレベル let/function は同一 realm から素通しで
// 見える）。chat.js の module 化（フェーズ6 S4）後は内部の let/function がモジュールスコープに
// 閉じ、外側の実行コンテキストからは見えなくなるため、e2e の唯一の入口としてこの窓を先に用意する。
// module 化後もここは window に明示公開したまま保つ（内部実装がどう分割されても同じ形で触れる）。
window.__sherpaChatTest = {
  openConversation,
  resumeRunningTurn,
  get cid() { return S.cid; },
  set cid(v) { S.cid = v; },
  get turnId() { return S.turnId; },
  set turnId(v) { S.turnId = v; },
  get es() { return S.es; },
  set es(v) { S.es = v; },
  get sending() { return S.sending; },
  set sending(v) { S.sending = v; },
};
