// チャット主入口。SSE で「思考の流れ」を右ペインに流し、答え先頭カード＋出典（原本DL）を中央に描く。
// 設計: docs/design/chat.md「1ターンの流れ」
// セキュリティ: server data は全て esc()。インライン handler にデータを載せず、委譲＋data-*（固定キー/esc済）で扱う。
'use strict';

// 分割構成: 共有状態/定数は web/chat/state.js、共有ダイアログは share-dialog.js、回答カード・trace/turn stack・welcome・出典は render.js、
// 会話履歴は history.js、SSE 購読・flow ライブ描画・停止・送信中枢は stream.js、範囲セレクタ・参照トグルは scope.js、brain-menu・フォント・エクスポート・テーマは menus.js。
// このエントリに残るのは: 全モジュールの import・init（deep-link `?conv=` と `/world-options` の初期化順＝文順を変えない）・input/send 中枢・#messages 委譲リスナー・
// 個人ファイルアップロード・updateShareButtonState/toast/copyText・3カラムレイアウト・window.__sherpaChatTest テスト seam。
// scope.js/menus.js は toast/updateShareButtonState をこのファイルから相対 import する（関数宣言＝hoisted のため、循環 import でも実行時に呼ぶ限り安全）。
import { S, EXAMPLES } from './chat/state.js';
import { openShareDialog } from './chat/share-dialog.js';
import { welcome, initRefGraph, deriveTraceStopReason } from './chat/render.js';
import {
  loadConversations, deleteConversation, togglePin, renameConversation,
  newConversation, openConversation, resumeRunningTurn, forkConversation, syncConvParam,
} from './chat/history.js';
// 副作用のみの import（#hist-search の配線・#convlist 再描画の監視は history-search.js 内で完結する）。
import './chat/history-search.js';
import { send, sendOrStop, _closeOtherTurns, currentTurnGen } from './chat/stream.js';
import { loadScopes, renderScopePanel, setScopeLabel, scopeChipLabel, setKb } from './chat/scope.js';
import { setLayer, setDepthProfile, setTools, setToolsAvailability, resetInquiryForNewConversation, refreshInquirySummary, toolsExplicitForRestore } from './chat/inquiry.js';
import { applyCachedBrain, loadConfig, exportMessages } from './chat/menus.js';

const $ = Sherpa.$, esc = Sherpa.esc;   // 共通ユーティリティ（nav.js）

// ===== 委譲 =====
// 影響一覧の行展開/折りたたみ（行全体が role=button・aria-expanded で開閉）。
// セレクタは .ilist 内の data-toggle に限定（refgraph-h の data-rg とは別ハンドラ）。
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
    // 保存名は doc_id（rel_path）末尾＝原本のファイル名（サーバの Content-Disposition basename と一致）
    const name = dl.textContent.replace(/^📄\s*/, '').split('/').filter(Boolean).pop() || 'download';
    Sherpa.downloadBlob(blob, name);   // revoke のタイミング問題は共通ヘルパで回避
  }
  // 調査台帳の href は、クリック時に .msg._messageId と S.cid から組み立てる（answer 自体は会話id/メッセージidを持たない）。
  const idl = e.target.closest('[data-investigation-dl]');
  if (idl) {
    e.preventDefault();
    const msg = idl.closest('.msg');
    const mid = msg && msg._messageId;
    if (!mid || !S.cid) return;
    const r = await fetch(`/conversations/${S.cid}/messages/${mid}/investigation?format=md`);
    if (!r.ok) { alert('記録を取得できませんでした'); return; }
    const blob = await r.blob();
    Sherpa.downloadBlob(blob, 'investigation.md');
  }
});
// 行トグル（role=button）のキーボード操作。実クリック処理へ委譲（セレクタは .ilist 内に限定）。
$('messages').addEventListener('keydown', (e) => {
  if (e.key !== 'Enter' && e.key !== ' ' && e.key !== 'Spacebar') return;
  const tg = e.target.closest('.ilist [data-toggle][role="button"]'); if (!tg) return;
  e.preventDefault(); tg.click();
});
// chat_router._SLASH_LENS の逆写像（実効レンズ→スラッシュ語）。確認カードが lens_source==="slash" のとき、
// 再送本文の先頭へ元の接頭辞を復元して既存のスラッシュ解決経路（サーバ側 _resolve_lens）に乗せるために使う。
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
  if (q.interaction_id) lines.push(`確認ID: ${q.interaction_id}`);   // router clarify(ask-*) の識別（再質問ループ防止・generic ask_user と区別）
  if (picked.length) lines.push(`選択: ${picked.join('、')}`);
  if (free) lines.push(`補足: ${free}`);
  if (q.original_message) lines.push(`元の依頼: ${q.original_message}`);
  msg.querySelectorAll('input,textarea,button').forEach((x) => { x.disabled = true; });
  // 「確認してから進めて」の確認カード（interaction_id が confirm-*）は、確認が出た時点で解決済みだった調べ方・探す対象・範囲・検索経路トグルを payload に持つ（chat_router.confirm_first_question）。
  // 回答の再送は1回だけそれへ戻す（ブロックの継続設定 S.lens/S.layer/S.scope/S.tools は変えない）。
  // lens_source==="slash" は、既存のスラッシュ接頭辞（/影響 等）を再送本文の先頭へ復元し、送信 override の lens にはブロックの継続設定（q.lens_block）を渡す（lens を直接送ると「1回限り」契約が崩れる）。
  // lens 選択の確認カード（interaction_id が ask-*）は本文の「選択:」から chat_router 側で解決するため対象外。
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
// 過去ターンの「思考の流れ」ボタン → 右ペインの該当ターンを展開してスクロール。
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
// 引用（該当箇所）カードの折りたたみ（refgraph と同じ見出しボタン開閉パターン）。
$('messages').addEventListener('click', (e) => {
  const h = e.target.closest('[data-cites]'); if (!h) return;
  const body = h.parentNode.querySelector('.cites-body');
  const open = !body.hidden;
  body.hidden = open; h.querySelector('.caret').textContent = open ? '▾' : '▴';
  h.setAttribute('aria-expanded', open ? 'false' : 'true');
});
// 参照したナレッジグラフの折りたたみ（開いた時だけ cytoscape を遅延 init）
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
// 出典0件時の再検索案内: 案内ボタンを押すと該当設定を広げ、直前の質問（この回答の1つ手前の user 発言）をそのまま再送する（言葉から推定せず、利用者が選んで1回クリックで再検索）。
$('messages').addEventListener('click', (e) => {
  const btn = e.target.closest('.retry-hint-btn'); if (!btn) return;
  const msg = btn.closest('.msg'); if (!msg) return;
  // 壊れた data-retry-action を `{}` へ黙って縮退させず、解析失敗はここで例外にして止める。
  const action = JSON.parse(btn.dataset.retryAction);
  // Codex タイムアウト継続（続きを調べる注記のボタン）: scope 等は変えず固定文言をそのまま送るだけ（resume はサーバ側の codex_session_id 継続に委ねる）。
  if (btn.dataset.retryKind === 'resume') {
    // action.message が無い/非文字列の壊れた data-retry-action を汎用（scope 拡大）経路へフォールスルーさせない（誤って「範囲を全体に広げて再送」と解釈されるため）。
    if (typeof action.message !== 'string') {
      throw new Error(`resume retry hint の action.message が文字列ではありません: ${JSON.stringify(action)}`);
    }
    // 送信中/購読中に入力欄を上書きしない（send() も同条件で二重送信を防ぐが、代入前にここで弾かないと下書きが消えたまま何も送信されない）。
    if (S.es || S.sending) return;
    $('input').value = action.message;
    send();
    return;
  }
  let prev = msg.previousElementSibling;
  while (prev && !prev.classList.contains('user')) prev = prev.previousElementSibling;
  const bubble = prev && prev.querySelector('.bubble-user');
  if (!bubble) return;
  // まず元回答（msg._answer.scope）の設定を基準にし、選択された1軸だけを広げて送信する。
  const origScope = (msg._answer && msg._answer.scope) || {};
  const scopePaths = Object.prototype.hasOwnProperty.call(action, 'scope_paths')
    ? (action.scope_paths || []) : (origScope.scope_paths || []);
  const layer = Object.prototype.hasOwnProperty.call(action, 'layer') ? action.layer : (origScope.layer || 'both');
  // 調べる深さの軸（action.depth_profile）も範囲/探す対象と同型で反映する。
  const depthProfile = Object.prototype.hasOwnProperty.call(action, 'depth_profile')
    ? action.depth_profile : (origScope.depth_profile || 'standard');
  // 検索経路トグルの軸（action.tools）も同型で反映する（欠落=元回答の値・無ければ全ON）。
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
// 思考ステップの detail 履歴を開閉（<button> なので Enter/Space は click に変換される）。
$('flow').addEventListener('click', (e) => {
  const h = e.target.closest('.fhist'); if (!h) return;
  const step = h.closest('.fstep'); if (!step) return;
  const open = step.classList.toggle('hist-open');
  h.setAttribute('aria-expanded', open ? 'true' : 'false');
});
$('convlist').addEventListener('click', (e) => {
  if (e.target.closest('[data-conv-menu]')) return;
  const rn = e.target.closest('[data-rename]'); if (rn) { e.stopPropagation(); return renameConversation(Number(rn.dataset.rename), rn.dataset.title || ''); }
  const del = e.target.closest('[data-del]'); if (del) { e.stopPropagation(); return deleteConversation(Number(del.dataset.del)); }
  const pin = e.target.closest('[data-pin]'); if (pin) { e.stopPropagation(); return togglePin(Number(pin.dataset.pin), pin.dataset.pinned !== '1'); }
  const sh = e.target.closest('[data-sharecid]'); if (sh) { e.stopPropagation(); return openShareDialog(Number(sh.dataset.sharecid), sh.dataset.title || ''); }
  if (e.target.closest('.cacts')) return;
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
// 共有ボタン（ヘッダ）: 現在の会話があれば共有ダイアログを開く。個人コンテンツを含む会話はボタン disabled＋ガードで拒否。
$('sharebtn').addEventListener('click', () => {
  if (!S.cid) { toast('共有したい会話を開いてください'); return; }
  if (S.convHasPersonal) { toast('個人ファイルを参照した会話は共有できません'); return; }
  openShareDialog(S.cid, $('conv-title').textContent || '会話');
});
// 引き継いで質問: 受領共有を開いている間だけ表示される（history.js::updateForkButtonState）。
$('forkbtn').addEventListener('click', () => {
  const wid = Number($('forkbtn').dataset.wid);
  if (!wid) return;
  forkConversation(wid);
});

$('send').addEventListener('click', sendOrStop);
// IME の変換確定の Enter では送信しない（isComposing が無い組み合わせに備えて keyCode 229 も見る・usage.js と同じ）。
$('input').addEventListener('keydown', (e) => { if (e.isComposing || e.keyCode === 229) return; if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } });

// コピー（メッセージ全体）。secure context 外では textarea フォールバック。
// export: share-dialog.js の共有URLコピーボタンから参照される（toast() 依存があるため chat.js に置く。関数宣言＝hoisted のため相互 import でも安全）。
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
  // .headline は mdLite() で HTML 整形して描画しているため、コピーは生テキストのまま（変換しない）にする: 本文部分だけ元データ（_answer.body・旧形式は headline）に差し替えてから抽出する。注記の帯（.answer-notices）はそのまま本文の前に残る。
  if (msg._answer && typeof (msg._answer.body ?? msg._answer.headline) === 'string') {
    const h = clone.querySelector('.headline');
    if (h) h.textContent = typeof msg._answer.body === 'string' ? msg._answer.body : msg._answer.headline;
  }
  // 完了状態（途中・停止・失敗）は画面の終了表示と同じ文言で先頭に付ける。
  const a = msg._answer;
  const state = a && ['partial', 'stopped', 'failed'].includes(a.completion) ? deriveTraceStopReason(a) : null;
  copyText((state ? `${state.text}\n` : '') + clone.textContent.trim());
});

// 個人ファイルのアップロード（送信欄の一部）。参照トグルの setPersonal/setKb は web/chat/scope.js の担当。
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

// 共有ボタンの enabled/disabled 状態を更新する（個人コンテンツ含む会話は共有不可）。history.js・stream.js から呼ばれるため export。
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
// preferRight（チップから明示的に右ペインを開いた操作専用）は、狭幅で中央最小幅（CMIN）を確保できない場合に、縮小順を「左を畳む→中央最小幅を残幅へクランプ」に変える。通常の開閉・ドラッグは変えない。
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
    if (need() > 0) { R = 0; cmin = lm.CMIN; }   // 極端な狭さでは右を諦める（中央が潰れて入力の吹き出しが縦書き状に崩れるため）
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
// 調べ方ブロックの入力欄チップ（web/chat/inquiry.js）が、右ペインが閉じている/狭幅でも必ずブロックへ到達できるよう export する。
// opts は updateLayout() へそのまま渡す（{preferRight:true} はチップ専用）。
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

// ユーザー表示・ログアウトは全ページ共通の上部ナビ（nav.js の #topbar-user）にある。

applyCachedBrain();   // 前回のモデル/プロバイダを即反映（その後 loadConfig がサーバ値で確定）
// トップバーの「⏳ 回答作成中」インジケータ（nav.js）から chat.html?conv=<id> で遷移してきた場合、その会話を自動で開く（実行中ターンがあれば resumeRunningTurn が自動再購読する）。
{
  const _convRaw = new URLSearchParams(location.search).get('conv');
  const _convParam = Number(_convRaw);
  const _wantShare = new URLSearchParams(location.search).get('share') === '1';
  const _startNew = () => { welcome(); resetInquiryForNewConversation(); loadConversations(); };
  if (_convParam) {
    // 開けない番号（削除済み・他人の会話）はアドレス欄から外し、新しいチャットの画面にする。
    // 待つ間に送信・新しいチャット・別の会話を開いた（世代が進んだ）なら、画面はもう触らない。
    const _gen = currentTurnGen();
    openConversation(_convParam).then(() => {
      // 通知（共有の期限が近い）からの遷移: 共有ダイアログ（一覧＋延長）を開く。
      if (_wantShare) $('sharebtn').click();
    }).catch(() => {
      if (currentTurnGen() !== _gen) return;
      syncConvParam(null); toast('会話を開けませんでした'); _startNew();
    });
  } else {
    if (_convRaw !== null) syncConvParam(null);
    _startNew();
  }
}
loadConfig();

// 検索経路トグルの実接続可用性（実行側と同じ判定関数・不達ならチップ自体を出さない）。失敗時は楽観的な既定（全ON）のまま据え置く（サーバ側の 422/graceful degrade が最終防衛線）。
fetch('/chat/tools-availability').then((r) => r.json()).then(setToolsAvailability).catch(() => { });

// 取込ディレクトリ選択肢を /world-options（ログイン必須・admin 不要）から読む（/worlds は admin 専用のため）。
fetch('/world-options').then((r) => r.json()).then((d) => {
  const names = d.worlds || [];
  const lbls = d.labels || {};
  S.verLabels = {}; names.forEach((n) => { S.verLabels[n] = lbls[n] || n; });
  // 資料フォルダが1つも登録されていない環境では、資料参照を送ると 404 になるため、未登録なら明示OFFへ倒す。
  // S.kbForcedOff も立てる: newConversation() が「未確認（読込前/失敗）」と「空で確定」を区別して、後者のときだけ新規会話も OFF のままにするため。
  if (names.length === 0) { S.kbForcedOff = true; setKb(false); }
  const sel = $('version');
  if (sel) {
    sel.innerHTML = names.length
      ? names.map((n) => `<option value="${esc(n)}">${esc(lbls[n] || n)}</option>`).join('')
      : '<option value="">（資料フォルダ未登録）</option>';
    // 資料フォルダは全体で1本＝選ぶ余地が無いので選択UIは出さない。select 自体は送信 body（stream.js）と範囲ツリー（scope.js）が読む値として残す。
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
        // 調べ方（lens）・探す対象（layer）も同じ後追い経路で復元する（scope.js の applyConversationScope が sc.lens_restore を計算済み）。
        S.lens = sc.lens_restore || 'auto';
        S.layer = sc.layer || 'both';
        S.depthProfile = sc.depth_profile || 'standard';   // 同じ後追い経路で調べる深さも復元する
        S.webSearch = !!sc.web_search;   // 同じ後追い経路で Web 検索希望も復元する
        S.tools = sc.tools || { grep: true, fulltext: true, graph: true };   // 同じ後追い経路で検索経路トグルも復元する
        S.toolsExplicit = toolsExplicitForRestore(S.tools, sc.tools_explicit);   // 触った軸だけ明示扱い（scope.js と同じ規則）
        refreshInquirySummary();
      }
      S.pendingConvWorld = null;                             // 選択肢に無い（削除済み）場合もここで諦める
    }
  }
  const v = document.querySelector('.verselect');
  if (v && names.length <= 1) v.style.display = 'none';   // 1つ（または未登録）ならセレクタは隠す
}).catch(() => { }).finally(() => loadScopes());
// 取込ディレクトリを切替えたら範囲をクリアして読み直す（別ディレクトリに古い範囲を送らない）。選択は端末ローカルに記憶する。
// 会話復元による自動切替（updateScopeHeader）は change を発火しないため、記憶されるのは利用者の明示選択だけ。
$('version').addEventListener('change', () => {
  try { localStorage.setItem('sherpa-world', $('version').value); } catch (_) { /* no-op */ }
  S.scope = []; S.scopeTree = null; S.scopeLabels = {}; S.currentScopeMeta = null;
  loadScopes();
});
// グラフからの「この語で影響を調べる」を受け取って入力に流し込む
const _ask = localStorage.getItem('sherpa-ask');
if (_ask) { localStorage.removeItem('sherpa-ask'); $('input').value = _ask; $('input').focus(); }

// ===== テスト専用 seam =====
// e2e（Playwright）が chat.js の内部変数・関数へ触れる唯一の入口。module スコープの内部は外から見えないため、window に明示公開する。
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
