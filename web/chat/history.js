// 会話履歴の一覧組み立て、会話の開く・新規・名前変更・ピン止め・削除、実行中ターンの再購読を担う。
// 設計: docs/design/chat.md「会話の保存と継続」
// stream.js・scope.js・render.js・chat.js とは関数宣言の hoist を前提に相互 import する（実行時に呼ぶ限り安全）。
'use strict';

import { S } from './state.js';
import {
  welcome, appendUser, appendAssistantRaw, appendAnswer, attachTraceButton, renderTurnStack,
} from './render.js';
import {
  setSendButtonStopping, startFlow, subscribeTurn, resetFlow, _questionAnswerState, appendRestoredQuestion,
  invalidateStopContext, startThinkingTicker,
} from './stream.js';
import { renderScopePanel, setScopeLabel, applyConversationScope, setKb } from './scope.js';
import { resetInquiryForNewConversation, applyInquiryOpenDefault } from './inquiry.js';
import { toast, updateShareButtonState } from '../chat.js';

const $ = Sherpa.$, esc = Sherpa.esc, fmtDateTime = Sherpa.fmtDateTime, getJSON = Sherpa.getJSON;

// ===== 会話履歴 =====

// 受領共有行の HTML（読み取り専用・pin/削除のみ・状態ラベル付き）
function _receivedConvHTML(c) {
  // id は data-* に入れるため整数に正規化する。
  const id = Number(c.id);
  const date = esc(fmtDateTime(c.received_at || c.updated_at));
  const status = c.share_status;              // active / expired / revoked / unavailable
  const inactive = status && status !== 'active';
  const statusLabel = status === 'expired' ? '期限切れ' : status === 'revoked' ? '共有取消' : inactive ? '利用不可' : '';
  const by = esc(c.shared_by_name || c.shared_by_user_id || '');
  const byText = by ? `${by}さんから` : '';
  // 期限まで 7 日以内の有効な共有だけ、いつまで読めるかを添える。
  const expMs = c.share_expires_at ? new Date(c.share_expires_at).getTime() : NaN;
  const expText = (status === 'active' && expMs - Date.now() <= 7 * 86400 * 1000)
    ? `この共有は ${esc(new Date(expMs).toLocaleDateString('sv-SE'))} まで閲覧できます` : '';
  return `<div class="conv${c.pinned ? ' pinned' : ''}${inactive ? ' conv-inactive' : ''}${id === S.cid ? ' on' : ''}" data-open="${id}" data-inactive="${inactive ? '1' : ''}">
     <button class="cmain" type="button" title="${esc(c.title || '会話')}" aria-label="${esc(c.title || '会話')}を開く">
       <span class="t">
         ${c.pinned ? '<span class="pin">📌</span>' : ''}
         <span class="badge-shared">共有</span><span class="badge-ro">🔒</span>${esc(c.title || '会話')}
         ${statusLabel ? `<span class="badge-status">${esc(statusLabel)}</span>` : ''}
       </span>
       <span class="d">${byText ? `<span class="shared-by">${byText}</span>・` : ''}${date}${expText ? `・${expText}` : ''}</span>
     </button>
     <button class="conv-more" type="button" data-conv-menu="${id}" aria-expanded="false"
       aria-controls="conv-actions-${id}" aria-label="${esc(c.title || '会話')}のその他の操作" title="その他の操作">
       <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="2" fill="currentColor"/><circle cx="12" cy="12" r="2" fill="currentColor"/><circle cx="19" cy="12" r="2" fill="currentColor"/></svg>
     </button>
     <div class="cacts" id="conv-actions-${id}" hidden role="group" aria-label="${esc(c.title || '会話')}の操作">
       <button class="cact" data-pin="${id}" data-pinned="${c.pinned ? '1' : '0'}" title="${c.pinned ? 'ピンを外す' : 'ピン止め'}">${c.pinned ? 'ピンを外す' : 'ピン止め'}</button>
       <button class="cact del" data-del="${id}" title="履歴から削除">履歴から削除</button>
     </div>
   </div>`;
}

// 所有会話行の HTML（全操作可・共有ボタン付き）
function _ownConvHTML(c) {
  // id は data-* に入れるため整数に正規化する。
  const id = Number(c.id);
  const date = esc(fmtDateTime(c.updated_at));
  // 引き継いだ会話は出所（共有元の名前・日時）を表示する。
  const f = c.forked_from;
  const forkedFromLine = f
    ? `<span class="d forked-from">出所: ${esc(f.name || f.user_id)}さんの共有（${esc(fmtDateTime(f.at))}）</span>` : '';
  return `<div class="conv${c.pinned ? ' pinned' : ''}${id === S.cid ? ' on' : ''}" data-open="${id}">
     <button class="cmain" type="button" title="${esc(c.title || '会話')}" aria-label="${esc(c.title || '会話')}を開く"><span class="t">${c.pinned ? '<span class="pin">📌</span>' : ''}${esc(c.title || '会話')}</span>
       <span class="d">${date}</span>${forkedFromLine}</button>
     <button class="conv-more" type="button" data-conv-menu="${id}" aria-expanded="false"
       aria-controls="conv-actions-${id}" aria-label="${esc(c.title || '会話')}のその他の操作" title="その他の操作">
       <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="2" fill="currentColor"/><circle cx="12" cy="12" r="2" fill="currentColor"/><circle cx="19" cy="12" r="2" fill="currentColor"/></svg>
     </button>
     <div class="cacts" id="conv-actions-${id}" hidden role="group" aria-label="${esc(c.title || '会話')}の操作">
       <button class="cact" data-rename="${id}" data-title="${esc(c.title || '')}" title="名前を変更">名前を変更</button>
       <button class="cact" data-pin="${id}" data-pinned="${c.pinned ? '1' : '0'}" title="${c.pinned ? 'ピンを外す' : 'ピン止め'}">${c.pinned ? 'ピンを外す' : 'ピン止め'}</button>
       <button class="cact" data-sharecid="${id}" data-title="${esc(c.title || '会話')}" title="この会話を共有">この会話を共有</button>
       <button class="cact del" data-del="${id}" title="この履歴を削除">履歴を削除</button>
     </div>
   </div>`;
}

// 会話一覧を取得して描く。取得が重なったときは後から始めた取得の結果だけを描く。
// 失敗時は今の一覧を残し、まだ何も描いていないときだけ空の案内を出す。
let _convListGen = 0, _convListShown = 0;
export async function loadConversations() {
  const myGen = ++_convListGen;
  let list = null;
  try { list = await getJSON('/conversations'); } catch (e) { /* 下で扱う */ }
  if (list === null) {
    if (_convListShown || $('convlist').querySelector('.conv')) return;
    list = [];
  } else {
    if (myGen < _convListShown) return;
    _convListShown = myGen;
  }
  // origin で自分の会話と共有された会話に分ける
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
    html = '<div class="muted" style="font-size:var(--text-small);padding:6px">会話はまだありません</div>';
  }
  // 再描画後もフォーカスを同じ会話（無ければ近い行）へ戻す
  const focused = document.activeElement;
  const focusedRow = focused.closest('#convlist .conv');
  const rowIndex = focusedRow ? [...$('convlist').querySelectorAll('.conv')].indexOf(focusedRow) : -1;
  $('convlist').innerHTML = html;
  if (focusedRow) {
    const rows = [...$('convlist').querySelectorAll('.conv')];
    const row = rows.find((item) => item.dataset.open === focusedRow.dataset.open)
      || rows[Math.min(rowIndex, rows.length - 1)];
    const target = row ? row.querySelector(focused.classList.contains('cmain') ? '.cmain' : '.conv-more') : $('newbtn');
    target.focus();
  }
}

export async function deleteConversation(id) {                 // 確認してから削除（メッセージも消える）
  if (!confirm('このチャット履歴を削除します。元に戻せません。よろしいですか？')) return;
  try { const r = await fetch('/conversations/' + id, { method: 'DELETE' }); if (!r.ok) throw new Error(r.status); }
  catch (e) { toast('削除に失敗しました'); return; }
  if (id === S.cid) newConversation(); else loadConversations();
  toast('履歴を削除しました');
}
export async function togglePin(id, pinned) {                  // ピン止め/解除
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

// 会話遷移の共通経路。購読を解除する（ターン自体はサーバ側で続く・停止は「■ 停止」だけ）。
function unsubscribeTurn() {
  // ① 保留中の停止・開始の遅延結果が遷移先の画面を上書きしないよう世代を進める
  invalidateStopContext();
  // ② 開始 POST 応答待ちの二重送信ガードを解除する（旧応答は世代照合で破棄される）
  S.sending = false;
  // ③ 送信 UI は S.es の有無に関わらず既定状態へ戻す
  setSendButtonStopping(false);
  $('messages').setAttribute('aria-busy', 'false');
  if (!S.es) return;
  S.es.close(); S.es = null;
}
// 開いている会話の番号をアドレス欄（?conv=）に出す。戻る履歴は積まない（replaceState）。
export function syncConvParam(cid) {
  try {
    const url = new URL(location.href);
    if (cid) url.searchParams.set('conv', String(cid)); else url.searchParams.delete('conv');
    if (url.href !== location.href) history.replaceState(history.state, '', url);
  } catch (_) { /* アドレス欄の更新は補助 */ }
}
export function newConversation() {
  unsubscribeTurn();
  S.cid = null; $('conv-title').textContent = '新しい会話';
  syncConvParam(null);
  // 遅延中の /world-options 応答が旧会話の設定を新規会話へ再適用しないよう null にする
  S.pendingConvWorld = null; S.currentScopeMeta = null;
  S.scope = []; if (S.scopeTree) renderScopePanel(S.scopeTree); setScopeLabel('全体');   // 範囲を全体に戻す
  // 資料参照は既定ONに戻す（資料フォルダが未登録と確定している S.kbForcedOff のときだけOFF）
  setKb(!S.kbForcedOff);
  resetInquiryForNewConversation();   // 調べ方/資料中心を調べる・オフに戻す
  S.convHasPersonal = false; updateShareButtonState();
  updateForkButtonState(null);
  welcome(); resetFlow(); loadConversations();
}

// 「引き継いで質問」ボタンを、受領共有を開いた画面でだけ出す（無効・個人ブロックの共有は除く）。
// data は GET /conversations/{cid} の応答。対象外は null。
function updateForkButtonState(data) {
  const btn = $('forkbtn');
  if (!btn) return;
  const isReceivedShare = !!(data && data.conversation && data.conversation.origin === 'received_share');
  const blocked = data && (data.share_status === 'unavailable' || data.share_status === 'personal_blocked');
  if (isReceivedShare && !blocked) {
    btn.hidden = false;
    btn.dataset.wid = String(data.conversation.id);
  } else {
    btn.hidden = true;
    delete btn.dataset.wid;
  }
}

// 受領共有 wid を自分の会話として複製し、そのまま開く。
export async function forkConversation(wid) {
  let d;
  try {
    const r = await fetch(`/conversations/${wid}/fork`, { method: 'POST' });
    d = await r.json().catch(() => null);
    if (!r.ok || !d || !d.ok) { toast((d && d.detail) || '引き継ぎに失敗しました'); return; }
  } catch (e) { toast('通信エラーが発生しました'); return; }
  toast('会話を引き継ぎました');
  await openConversation(d.conversation_id);
}
export async function openConversation(cid) {
  // 取得が成功してから購読を解除する（失敗時は今の画面に留まる）
  const data = await getJSON(`/conversations/${cid}`);
  unsubscribeTurn();
  S.cid = cid; $('conv-title').textContent = data.conversation.title || '会話';
  syncConvParam(cid);
  $('messages').innerHTML = '';
  // 個人コンテンツ参照済みフラグを反映
  S.convHasPersonal = !!(data.conversation && data.conversation.contains_personal_workspace);
  updateShareButtonState();
  updateForkButtonState(data);
  // 右ペインの積み上げ表示用に、user 発言ごとにターンを作り、直後の assistant 応答の trace を紐づける。
  const turns = [];   // [{question, time, trace}]（trace 無しは null＝「（記録なし）」）
  const qState = _questionAnswerState(data.messages);   // 確認カードの回答済み判定・操作可否
  data.messages.forEach((m, mi) => {
    if (m.role === 'user') {
      appendUser(m.content);
      turns.push({ question: m.content, time: m.created_at, trace: null });
      return;
    }
    // 保存された確認カードを再構築する（回答済みは disabled・未回答の最新だけ操作可）。question の無い clarify はプレースホルダ。
    let el;
    if (m.answer && m.answer.question) {
      el = appendRestoredQuestion(m.answer.question, qState.answered[mi], mi === qState.operableIdx);
    } else if (m.answer && m.answer.lens === 'clarify') {
      el = appendAssistantRaw('<div class="muted">（確認のやり取り）</div>');
    } else {
      el = appendAnswer(m.answer, m.id, m.trace, m.feedback);
    }
    let turn = turns[turns.length - 1];
    if (!turn) { turn = { question: null, time: null, trace: null }; turns.push(turn); }
    turn.time = m.created_at || turn.time;
    turn.trace = (m.trace && m.trace.length) ? m.trace : null;
    // trace_version は answer 側に付く（無ければ v1）
    turn.traceVersion = (m.answer && m.answer.trace_version === 2) ? 2 : 1;
    // 終了理由の導出に answer.data.evidence_packet が要るため answer も渡す
    turn.answer = m.answer;
    if (el && turn.trace) attachTraceButton(el, `fturn-${turns.length - 1}`);
  });
  applyConversationScope(data.messages);   // 最後の回答の範囲/調べ方/資料中心をヘッダ/選択に反映
  applyInquiryOpenDefault(data.messages.length === 0);
  $('messages').scrollTop = 0;
  resetFlow();
  // 積み上げ表示は受領共有（trace を返さない）では出さない
  const isReceivedShare = !!(data.conversation && data.conversation.origin === 'received_share');
  if (!isReceivedShare && turns.length) renderTurnStack(turns);
  loadConversations();
  if (!isReceivedShare) resumeRunningTurn(cid, turns);   // 実行中ターンがあれば再購読
}

// 会話を開いたとき実行中ターンがあれば、末尾の質問をライブ状態にして cursor=0 から再購読する。
// 設計: docs/design/chat.md「停止と同時実行」
export async function resumeRunningTurn(cid, turns) {
  let running;
  try { running = await getJSON('/chat/turns/running'); } catch (e) { return; }
  // ① 応答待ちの間に別会話へ遷移していたら使わない
  if (S.cid !== cid) return;
  // ② 新しい送信の開始応答待ち中は割り込まない（再購読すると新しい送信の世代が壊れる）
  if (S.sending) return;
  const hit = (running.turns || []).find((t) => t.conversation_id === cid);
  if (!hit) return;
  // ③ 購読中の同じターンへは再開処理を重ねない（表示の二重化・ストリームの張り直しを防ぐ）
  if (S.es && S.turnId === hit.turn_id) return;
  // 別の turnId を購読済みなら上書きしない
  if (S.es && S.turnId && S.turnId !== hit.turn_id) return;
  S.turnId = hit.turn_id;
  // 経過秒はサーバの started_at を起点にする
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
