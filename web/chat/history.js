// フェーズ6 S6（リファクタリング計画）: 会話履歴ドメイン（会話一覧の組み立て・open/new/rename/pin/delete・
// 背景実行の再購読）を chat.js から純移動。export は chat.js 側に残る呼び出し元（$('convlist')/
// $('newbtn')/$('conv-title') の delegate リスナー・init の deep-link 分岐・window.__sherpaChatTest
// シーム）から参照される loadConversations/deleteConversation/togglePin/renameConversation/
// newConversation/openConversation/resumeRunningTurn のみに絞る（_ownConvHTML/_receivedConvHTML/
// unsubscribeTurn は loadConversations/newConversation/openConversation の内部専用のため非公開のまま）。
// newConversation/openConversation は render/scope/stream の複数ドメインを跨ぐオーケストレータ＝
// welcome/appendUser/appendAssistantRaw/appendAnswer/attachTraceButton/renderTurnStack は render.js
// から import。S7（リファクタリング計画）: setSendButtonStopping/startFlow/subscribeTurn/resetFlow/
// _questionAnswerState/appendRestoredQuestion は chat.js から web/chat/stream.js へ純移動したため
// import 元をそちらへ更新（history.js↔stream.js の意図した循環 import・関数宣言＝hoisted のため
// 実行時に呼ぶ限り ESM で安全＝render.js の setRt・share-dialog.js の copyText と同じパターン）。
// S8（リファクタリング計画）: renderScopePanel/setScopeLabel/applyConversationScope は
// chat.js から web/chat/scope.js へ純移動したため import 元をそちらへ更新した（history.js↔scope.js
// の意図した循環 import は発生しない＝scope.js は history.js を import しないため片方向）。
// updateShareButtonState/toast はどのドメインにも属さない横断ユーティリティのため引き続き
// chat.js 側に残り `import {...} from '../chat.js'` する（chat.js↔history.js の意図した循環 import）。
'use strict';

import { S } from './state.js';
import {
  welcome, appendUser, appendAssistantRaw, appendAnswer, attachTraceButton, renderTurnStack,
} from './render.js';
import {
  setSendButtonStopping, startFlow, subscribeTurn, resetFlow, _questionAnswerState, appendRestoredQuestion,
  invalidateStopContext, startThinkingTicker,
} from './stream.js';
import { renderScopePanel, setScopeLabel, applyConversationScope } from './scope.js';
import { resetInquiryForNewConversation, applyInquiryOpenDefault } from './inquiry.js';
import { toast, updateShareButtonState } from '../chat.js';

const $ = Sherpa.$, esc = Sherpa.esc, fmtDateTime = Sherpa.fmtDateTime, getJSON = Sherpa.getJSON;   // 共通ユーティリティ（common.js）

// ===== 会話履歴 =====

// 受領共有行を組み立て（読み取り専用・pin/削除のみ・状態ラベル付き）
function _receivedConvHTML(c) {
  // ID は必ず整数に正規化（data-* 属性に入れるので非数値を防ぐ）。
  const id = Number(c.id);
  const date = esc(fmtDateTime(c.received_at || c.updated_at));
  const status = c.share_status;              // active / expired / revoked / unavailable
  const inactive = status && status !== 'active';
  const statusLabel = status === 'expired' ? '期限切れ' : status === 'revoked' ? '共有取消' : inactive ? '利用不可' : '';
  const by = esc(c.shared_by_name || c.shared_by_user_id || '');
  const byText = by ? `${by}さんから` : '';
  return `<div class="conv${c.pinned ? ' pinned' : ''}${inactive ? ' conv-inactive' : ''}${id === S.cid ? ' on' : ''}" data-open="${id}" data-inactive="${inactive ? '1' : ''}">
     <div class="cmain">
       <div class="t">
         ${c.pinned ? '<span class="pin">📌</span>' : ''}
         <span class="badge-shared">共有</span><span class="badge-ro">🔒</span>${esc(c.title || '会話')}
         ${statusLabel ? `<span class="badge-status">${esc(statusLabel)}</span>` : ''}
       </div>
       <div class="d">${byText ? `<span class="shared-by">${byText}</span>・` : ''}${date}</div>
     </div>
     <div class="cacts">
       <button class="cact" data-pin="${id}" data-pinned="${c.pinned ? '1' : '0'}" title="${c.pinned ? 'ピンを外す' : 'ピン止め'}">${c.pinned ? '📌' : '📍'}</button>
       <button class="cact del" data-del="${id}" title="履歴から削除">🗑</button>
     </div>
   </div>`;
}

// 所有会話行を組み立て（全操作可・共有ボタン付き）
function _ownConvHTML(c) {
  // ID は必ず整数に正規化（data-* 属性に入れるので非数値を防ぐ）。
  const id = Number(c.id);
  const date = esc(fmtDateTime(c.updated_at));
  return `<div class="conv${c.pinned ? ' pinned' : ''}${id === S.cid ? ' on' : ''}" data-open="${id}">
     <div class="cmain"><div class="t">${c.pinned ? '<span class="pin">📌</span>' : ''}${esc(c.title || '会話')}</div>
       <div class="d">${date}</div></div>
     <div class="cacts">
       <button class="cact" data-rename="${id}" data-title="${esc(c.title || '')}" title="名前を変更">✎</button>
       <button class="cact" data-pin="${id}" data-pinned="${c.pinned ? '1' : '0'}" title="${c.pinned ? 'ピンを外す' : 'ピン止め'}">${c.pinned ? '📌' : '📍'}</button>
       <button class="cact" data-sharecid="${id}" data-title="${esc(c.title || '会話')}" title="この会話を共有">🔗</button>
       <button class="cact del" data-del="${id}" title="この履歴を削除">🗑</button>
     </div>
   </div>`;
}

export async function loadConversations() {
  let list = [];
  try { list = await getJSON('/conversations'); } catch (e) { /* PG未起動なら空 */ }
  // origin で分割: own / received_share
  const own = list.filter((c) => !c.origin || c.origin === 'own');
  const received = list.filter((c) => c.origin === 'received_share');

  let html = '';
  if (own.length || received.length) {
    if (own.length) {
      html += '<div class="conv-section-head">自分の会話</div>';
      html += own.map(_ownConvHTML).join('');
    }
    if (received.length) {
      html += '<div class="conv-section-head">共有された会話</div>';
      html += received.map(_receivedConvHTML).join('');
    }
  } else {
    html = '<div class="muted" style="font-size:12px;padding:6px">会話はまだありません</div>';
  }
  $('convlist').innerHTML = html;
}

export async function deleteConversation(id) {                 // #6: 確認してから削除（連鎖でメッセージも消える）
  if (!confirm('このチャット履歴を削除します。元に戻せません。よろしいですか？')) return;
  try { const r = await fetch('/conversations/' + id, { method: 'DELETE' }); if (!r.ok) throw new Error(r.status); }
  catch (e) { toast('削除に失敗しました'); return; }   // 失敗（404/403等）は成功扱いにしない
  if (id === S.cid) newConversation(); else loadConversations();
  toast('履歴を削除しました');
}
export async function togglePin(id, pinned) {                  // #8: ピン止め/解除（上部に表示）
  try {
    const r = await fetch('/conversations/' + id + '/pin', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ pinned }) });
    if (!r.ok) throw new Error(r.status);
  } catch (e) { toast('変更に失敗しました'); return; }
  loadConversations();
}
export async function renameConversation(id, current) {        // 履歴タイトルの変更
  const title = prompt('チャットの名前を変更', current || '');
  if (title == null) return;                            // キャンセル
  const t = title.trim(); if (!t) return;
  try {
    const r = await fetch('/conversations/' + id, { method: 'PATCH',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: t }) });
    if (!r.ok) throw new Error(r.status);
  } catch (e) { toast('名前の変更に失敗しました'); return; }
  if (id === S.cid) $('conv-title').textContent = t;
  loadConversations(); toast('名前を変更しました');
}

// 背景実行（覗き窓方式）: 別会話へ移動・新規会話などで購読を外すのは「購読解除」だけでよい
// （ターンはサーバ側 background thread で続く＝この機能の主目的そのもの）。旧 UI フィードバック1の
// 頃は EventSource がターンの実体を兼ねていたため停止要求も併走させていたが、現在は明示的な
// 「■ 停止」ボタン（stopStream()）だけがサーバへ停止要求を送る。
function unsubscribeTurn() {
  // 会話遷移（新規チャット・会話切替・削除後の遷移は newConversation 経由）の共通経路。S.es が
  // 既に null（onerror が先着済みだが、その停止 POST 自体はまだ結果待ち／開始 POST の応答待ち中
  // ＝send() が S.sending を立てた直後でまだ subscribeTurn に到達していない）でも、保留中の
  // 停止・開始いずれの遅延結果も遷移先の画面の #rt・思考枠を後から上書きしないよう、ここで必ず
  // 世代を進めて無効化する。
  invalidateStopContext();
  // `S.sending`（開始 POST 応答待ち中の二重送信ガード）も必ず解除する——ここで戻さないと、
  // 旧 POST の応答が届く（＝ send() 自身が S.sending=false する）まで新しい画面で一切送信できない
  // （旧 POST が応答しない/失敗さえしなければ実質恒久的に送信不能になる）。旧 POST が後から
  // 届いても世代照合（turnGen）で結果は破棄される＝ここで先に解除しても二重送信にはならない。
  S.sending = false;
  // 送信 UI（ボタンの disabled／aria-busy）は S.es の有無に関わらず必ず遷移先の既定状態へ戻す
  // （開始 POST 応答待ち中に遷移した場合、send() 自身は `$('send').disabled = true` を立てた
  // ままここへ来るため、S.es が無いからと戻さずに抜けるとボタンが押せないまま新しい画面に残る）。
  setSendButtonStopping(false);
  $('messages').setAttribute('aria-busy', 'false');
  if (!S.es) return;
  S.es.close(); S.es = null;
}
export function newConversation() {
  unsubscribeTurn();
  S.cid = null; $('conv-title').textContent = '新しい会話';
  // SC-6e: 遅延中の /world-options 応答（chat.js の pendingConvWorld 後追い経路）が、後から
  // 届いたときに旧会話の範囲/調べ方/検索経路トグル等を新規会話へ誤って再適用しないよう、
  // ここで両方 null にする（後追い経路は S.pendingConvWorld が null なら何もしない）。
  S.pendingConvWorld = null; S.currentScopeMeta = null;
  S.scope = []; if (S.scopeTree) renderScopePanel(S.scopeTree); setScopeLabel('全体');   // 範囲を全体に戻す
  resetInquiryForNewConversation();   // SC-6b: 調べ方/探す対象も自動・両方に戻し、ブロックを開く（§8 裁定12）
  S.convHasPersonal = false; updateShareButtonState();   // Feature C: 新規会話は共有可能状態にリセット
  welcome(); resetFlow(); loadConversations();
}
export async function openConversation(cid) {
  // 会話取得が成功するまで画面遷移を確定しない＝ unsubscribeTurn（世代の無効化を含む）は
  // 取得成功後に呼ぶ（取得失敗時は今の画面に留まる＝暫定「停止しました」等の訂正待ちを
  // 無効化してしまわないため）。
  const data = await getJSON(`/conversations/${cid}`);
  unsubscribeTurn();
  S.cid = cid; $('conv-title').textContent = data.conversation.title || '会話';
  $('messages').innerHTML = '';
  // Feature C: 会話の contains_personal_workspace フラグを反映。
  S.convHasPersonal = !!(data.conversation && data.conversation.contains_personal_workspace);
  updateShareButtonState();
  // UIフィードバック（2026-07-03・RV再検証 HIGH#1）: 右ペインに全ターンを時系列で積み上げ表示するため、
  // **user 発言ごとにターンを起こす**（clarify/停止等で assistant 応答が無いターンも取りこぼさない）。
  // 直後に assistant が来ればその trace を紐づけ、来なければ trace:null（「（記録なし）」）のまま残す。
  const turns = [];   // [{question, time, trace}]（trace は無ければ null＝「（記録なし）」表示）
  const qState = _questionAnswerState(data.messages);   // S1: 保存された確認カードの回答済み判定・操作可否
  data.messages.forEach((m, mi) => {
    if (m.role === 'user') {
      appendUser(m.content);
      turns.push({ question: m.content, time: m.created_at, trace: null });
      return;
    }
    // S1（ask_user-improvements.md）: 保存された確認カード（answer.question）は askcard を再構築する
    // （回答済みは選択内容つき・disabled、未回答の最新カードのみ操作可能）。受領共有では store 側で
    // question を伏せているため、question が無い clarify メッセージ＝確認のやり取りのプレースホルダを出す。
    let el;
    if (m.answer && m.answer.question) {
      el = appendRestoredQuestion(m.answer.question, qState.answered[mi], mi === qState.operableIdx);
    } else if (m.answer && m.answer.lens === 'clarify') {
      el = appendAssistantRaw('<div class="muted">（確認のやり取り）</div>');
    } else {
      el = appendAnswer(m.answer, m.id, m.trace, m.feedback);
    }
    let turn = turns[turns.length - 1];
    if (!turn) { turn = { question: null, time: null, trace: null }; turns.push(turn); }   // 想定外の保険
    turn.time = m.created_at || turn.time;   // 回答時刻の方が有益なので上書き
    turn.trace = (m.trace && m.trace.length) ? m.trace : null;
    // EXT-4（拡張設計 §10）: trace_version は answer envelope 側（`m.answer.trace_version`）に付く
    // （messages.trace 自体は v1/v2 とも配列のまま・§2.3）。無ければ v1 として描画する。
    turn.traceVersion = (m.answer && m.answer.trace_version === 2) ? 2 : 1;
    // 終了理由（`deriveTraceStopReason`）は answer.data.evidence_packet を見るため、
    // trace 配列だけでなく answer 自体も _buildTurnEl（render.js）へ渡す。
    turn.answer = m.answer;
    if (el && turn.trace) attachTraceButton(el, `fturn-${turns.length - 1}`);
  });
  // 範囲・調べ方（明示時のみ）・探す対象は同じ関数の pendingConvWorld 後追い経路で復元する
  // （RV1 #4・独立の sameDir 判定を増やさない）。開閉状態だけはこの会話専用の別の関心事なので
  // ここで別途呼ぶ。
  applyConversationScope(data.messages);   // 最後の回答の範囲/調べ方/探す対象をヘッダ/選択に反映（RV Med#1）
  applyInquiryOpenDefault(data.messages.length === 0);
  $('messages').scrollTop = 0;             // 復元は先頭から表示（初回メッセージがヘッダに被らない）
  resetFlow();
  // UIフィードバック（RV再検証 HIGH#2）: 積み上げを出さないのは**受領共有**（route/trace を返さない
  // 既存 posture）だけ。自分の会話は全ターン trace 無し（旧会話・ナレッジ参照オフ等）でも
  // 「（記録なし）」の積み上げとして描画する（trace の有無では出し分けない）。
  const isReceivedShare = !!(data.conversation && data.conversation.origin === 'received_share');
  if (!isReceivedShare && turns.length) renderTurnStack(turns);
  loadConversations();
  if (!isReceivedShare) resumeRunningTurn(cid, turns);   // 背景実行: 実行中ターンがあれば自動で再購読
}

// 背景実行（proposal §3・再接続）: 会話を開いたとき実行中ターンがあれば、末尾（回答未保存の
// ユーザー発言）をライブ状態にして cursor=0 から replay→追従する（積み上げ済みの過去ターンに
// 続けて表示・renderTurnStack が既に描いた末尾の「（記録なし）」プレースホルダはそのまま流用する）。
export async function resumeRunningTurn(cid, turns) {
  let running;
  try { running = await getJSON('/chat/turns/running'); } catch (e) { return; }
  // 世代チェック。上の await（/chat/turns/running の応答待ち）の間に
  // 別の会話（または新規会話）へ遷移していたら、この応答はもう古い＝今の画面には使わない
  // （さもないと会話Aの遅延応答が会話Bの画面に誤って購読を張ってしまう）。S.cid は「いま画面に
  // 表示している会話」を指す唯一の状態変数なので、これと比較するだけで十分。
  if (S.cid !== cid) return;
  // 開始 POST 応答待ち中（S.sending）なら、利用者が同じ会話で既に新しい送信を始めている——
  // ここで見つかる「実行中ターン」はその新しい送信より前に始まった旧ターンの可能性が高い。
  // 割り込んで再購読すると `subscribeTurn` が turnGen を進めてしまい、新しい送信の開始応答が
  // 届いた時点でそれが世代不一致で破棄され（`send()` の turnGen 契約）、しかも `S.sending` を
  // 解除する責任者が誰もいなくなって送信不能のまま固着する。新しい送信を必ず優先し、ここでは
  // 何もしない（新しい送信自身が `subscribeTurn` を呼ぶ）。
  if (S.sending) return;
  const hit = (running.turns || []).find((t) => t.conversation_id === cid);
  if (!hit) return;
  // 同一 turnId への遅延/重複応答（GET /chat/turns/running が複数回呼ばれる・片方の応答が
  // 遅れて後から届く等）で、既に購読中の同じターンへ何度も「再開」処理を走らせない——素通り
  // させると `.thinking` プレースホルダーや `startFlow` の積み上げ表示を毎回二重に作ってしまい、
  // `subscribeTurn` も無駄に既存の EventSource を閉じて張り直す（受信中のストリームを
  // 一瞬でも切る実害がある）。
  if (S.es && S.turnId === hit.turn_id) return;
  // turnId 照合: 応答順が逆転し、この await の間に別経路（利用者自身の送信の完了）で既に
  // "別の" turnId へ購読が確立していた場合も、その turnId を上書きしない（ここで見つかった
  // 実行中ターンへ張り替えると、新しい送信の購読を巻き戻して孤児化させてしまう）。
  if (S.es && S.turnId && S.turnId !== hit.turn_id) return;
  S.turnId = hit.turn_id;
  // サーバの started_at（TIMESTAMPTZ isoformat＝tz 付き）を起点に＝遷移・リロードで経過秒が 0 に戻らない
  S.turnStartedAtMs = Date.parse(hit.started_at) || Date.now();
  const lastTurn = turns[turns.length - 1];
  const question = (lastTurn && !lastTurn.trace) ? lastTurn.question : null;
  setSendButtonStopping(true);
  $('messages').setAttribute('aria-busy', 'true');
  const thinking = appendAssistantRaw('<div class="thinking loading-inline" role="status"><span class="spinner spinner-sm"></span><span>回答を作成しています...</span></div>');
  startThinkingTicker(thinking);
  startFlow(question || '');
  subscribeTurn(thinking);
}
