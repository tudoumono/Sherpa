// チャット1ターンの実行表示。送信（開始 POST）・SSE 購読・右ペイン「思考の流れ」のライブ描画・停止・確認カードの復元を担う。
// 設計: docs/design/chat.md「1ターンの流れ」「停止と同時実行」
// history.js・render.js・chat.js とは関数宣言の hoist を前提に相互 import する（実行時に呼ぶ限り安全）。
'use strict';

import { S } from './state.js';
import {
  questionHTML, _renderDetail, appendAssistantRaw, appendUser,
  ensureAnswerCard, finalizeAnswer, clearReveal, reveal,
  TraceTreeV2, deriveTraceStopReason, stopReasonInfo, stopReasonCategoryFromError,
} from './render.js';
import { loadConversations, syncConvParam } from './history.js';
import { updateScopeHeader } from './scope.js';
import { isSimpleMode, setInquiryOpen, toolsForSend, toolsExplicitForSend } from './inquiry.js';
import { updateShareButtonState } from '../chat.js';

const $ = Sherpa.$, esc = Sherpa.esc, fmtDateTime = Sherpa.fmtDateTime;

// ===== 思考の流れ（右ペイン） =====
// 右ペイン見出しの状態表示を更新する（render.js からも呼ぶ）。
export function setRt(text, live) {
  const rt = $('rt'); rt.classList.toggle('live', !!live);
  rt.lastChild.textContent = text;
}
// 右ペインを初期状態に戻す（会話切替でも呼ぶ。終了理由は出さずティックだけ止める）。
export function resetFlow() {
  _stopThinkingTicker();
  if (S.liveTraceTree) { S.liveTraceTree.destroy(); S.liveTraceTree = null; }
  $('flow').innerHTML = '<div class="hint">質問すると、考えた流れがここに流れます。</div>';
  S.nodes = {}; S.liveTurnId = null; S.turnSeq = 0;
  setRt('待機中', false);
}
// exceptEl 以外の過去ターンを全て閉じる（単一展開）。chat.js の data-showtrace ハンドラからも呼ぶ。
export function _closeOtherTurns(exceptEl) {
  $('flow').querySelectorAll('details.fturn[open]').forEach((d) => { if (d !== exceptEl) d.open = false; });
}
// 新ターン（ライブ）を右ペインの末尾に積む。開いている過去ターンは畳み、プレースホルダがあれば消す。
export function startFlow(question) {
  const flow = $('flow');
  _closeOtherTurns(null);
  const hint = flow.querySelector(':scope > .hint'); if (hint) hint.remove();
  const id = `fturn-${S.turnSeq++}`;
  const det = document.createElement('details');
  det.className = 'fturn'; det.id = id; det.open = true;
  det.innerHTML = '<summary class="fturn-head"><span class="fturn-q"></span><span class="fturn-time"></span></summary>'
    + '<div class="fturn-body"></div>';
  det.querySelector('.fturn-q').textContent = (question || '').slice(0, 40);
  det.querySelector('.fturn-time').textContent = fmtDateTime(new Date().toISOString());
  flow.appendChild(det);
  S.nodes = {}; S.liveTurnId = id;
  setRt('リアルタイム', true);
}
// 経過時間の表示形式（1.2s）
function _fmtElapsed(ms) { return (ms / 1000).toFixed(1) + 's'; }
// 思考/ツールのノードを id で追加・更新する（v1 の描画）。
function onNode(e) {
  let el = S.nodes[e.id];
  if (!el) {
    el = document.createElement('div');
    el.className = 'fstep' + (e.kind === 'tool' ? ' tool' : '');
    el.innerHTML = '<div class="fnode"></div><div class="fbody">'
      + '<div class="fhead"><div class="flabel"></div><span class="ftime" hidden></span></div>'
      + '<div class="fdetail"></div></div>';
    el._details = [];                      // detail 履歴（変化時のみ蓄積）
    el._t0 = performance.now();            // 初回受信時刻
    // ライブ中のターン要素（.fturn-body）に追記する（無ければ #flow 直下）
    const liveBody = S.liveTurnId && document.getElementById(S.liveTurnId)
      ? document.getElementById(S.liveTurnId).querySelector('.fturn-body') : null;
    (liveBody || $('flow')).appendChild(el); S.nodes[e.id] = el;
  }
  el.classList.remove('active', 'done'); el.classList.add(e.status);
  el.querySelector('.flabel').textContent = e.label;
  _renderDetail(el.querySelector('.fdetail'), e);
  el.querySelector('.fnode').textContent = e.status === 'done' ? '✓' : '';
  // detail が変わったら履歴として蓄積する（直近は .fdetail・過去分は隠し領域）
  const d = e.detail || '';
  if (d && d !== el._details[el._details.length - 1]) el._details.push(d);
  if (el._details.length >= 2) {                        // 2件以上で履歴ボタンを出す
    let hb = el.querySelector('.fhist');
    let list = el.querySelector('.fhist-list');
    if (!hb) {
      hb = document.createElement('button');
      hb.className = 'fhist'; hb.type = 'button'; hb.setAttribute('aria-expanded', 'false');
      hb.innerHTML = '<span class="fhtxt"></span> <span class="caret" aria-hidden="true">▾</span>';
      list = document.createElement('div'); list.className = 'fhist-list';
      const body = el.querySelector('.fbody'); body.appendChild(hb); body.appendChild(list);
    }
    hb.querySelector('.fhtxt').textContent = `履歴 ${el._details.length}`;
    list.textContent = '';
    el._details.slice(0, -1).forEach((t) => {
      const it = document.createElement('div'); it.className = 'fhist-item'; it.textContent = t;
      list.appendChild(it);
    });
  }
  // done になった初回だけ経過時間（300ms 以上）を右肩に出す
  if (e.status === 'done' && !el._timed) {
    el._timed = true;
    const ms = performance.now() - el._t0;
    if (ms >= 300) {
      const t = el.querySelector('.ftime'); t.textContent = _fmtElapsed(ms); t.hidden = false;
    }
  }
  $('flow').scrollTop = $('flow').scrollHeight;
}
// ===== trace_version=2 のライブ階層描画 =====
// ストリーム先頭の trace_meta で v2 と分かったときだけ S.liveTraceTree を張る（v1 のターンでは生成しない）。
function _liveBodyEl() {
  const liveBody = S.liveTurnId && document.getElementById(S.liveTurnId)
    ? document.getElementById(S.liveTurnId).querySelector('.fturn-body') : null;
  return liveBody || $('flow');
}
function onTraceMeta(e) {
  if (S.liveTraceTree) { S.liveTraceTree.destroy(); S.liveTraceTree = null; }
  // タブが非表示のときは live:false で生成する（ティックだけ止め、ノードは描画する）
  if (e.trace_version === 2) {
    const hidden = typeof document !== 'undefined' && document.hidden;
    S.liveTraceTree = new TraceTreeV2(_liveBodyEl(), { live: !hidden, startedAtMs: S.turnStartedAtMs });
  }
}
// ターン終端（answer/stopped/error/question）で必ず呼ぶ（呼ばないとティックの setInterval が残る）。
// stopInfo は render.js の deriveTraceStopReason/stopReasonInfo が組む {text, interrupted}（null＝終了理由を出さず畳むだけ）。
// 戻り値は finalize したツリー（張られていなければ null）。停止 POST 結果待ちの onerror が後で終了理由を訂正するのに使う。
function _finalizeLiveTraceTree(stopInfo) {
  if (!S.liveTraceTree) return null;
  const tree = S.liveTraceTree;
  tree.finalize(stopInfo || null);
  S.liveTraceTree = null;
  return tree;
}
// 待ち時間の「AI が考えています（N秒）」表示を1秒ごとに更新する（v1/v2 共通）。
// thinking 要素が DOM から外れたら自己停止し、ターン終端の各経路でも明示的に止める（同時に動くのは1本）。
// history.js の resumeRunningTurn（再購読）からも使う。
let _thinkingTickerStop = null;
export function startThinkingTicker(thinkingEl) {
  if (_thinkingTickerStop) _thinkingTickerStop();
  const startedAt = S.turnStartedAtMs || Date.now();
  const timer = setInterval(() => {
    if (!thinkingEl.isConnected) { stop(); return; }
    const span = thinkingEl.querySelector('.thinking span:last-child');
    if (!span) { stop(); return; }
    const secs = Math.round((Date.now() - startedAt) / 1000);
    span.textContent = secs >= 2 ? `AI が考えています（${secs}秒）` : '回答を準備しています...';
  }, 1000);
  function stop() { clearInterval(timer); if (_thinkingTickerStop === stop) _thinkingTickerStop = null; }
  _thinkingTickerStop = stop;
}
function _stopThinkingTicker() { if (_thinkingTickerStop) _thinkingTickerStop(); }
// タブが非表示になったらティックを止める（再表示時の自動再開はしない）。
if (typeof document !== 'undefined') {
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) return;
    _stopThinkingTicker();
    if (S.liveTraceTree) S.liveTraceTree.destroy();
  });
}
function appendQuestion(q) {
  const el = appendAssistantRaw(questionHTML(q));
  el._question = q;
  return el;
}
// 履歴に保存された確認カード（answer.question）の復元。
// 回答済み判定は「以降の user メッセージに同じ『確認ID: {interaction_id}』が含まれるか」。未回答の最後の1件だけ操作可能にする。
function _extractSelection(content) {   // 回答メッセージの整形文から選択/補足を取り出す
  const lines = String(content || '').split('\n');
  const val = (prefix) => { const l = lines.find((x) => x.startsWith(prefix)); return l ? l.slice(prefix.length) : ''; };
  const pickStr = val('選択: ');
  return { picked: pickStr ? pickStr.split('、') : [], free: val('補足: ') };
}
// 保存メッセージ列から、確認カードごとの回答済み選択と操作可能なカードの index を求める。
export function _questionAnswerState(messages) {
  const answered = {};        // msgIndex -> {picked:[], free:''}（回答済みのみ）
  const unanswered = [];      // 未回答の question メッセージ index（昇順）
  for (let i = 0; i < messages.length; i++) {
    const q = messages[i].role !== 'user' && messages[i].answer && messages[i].answer.question;
    if (!q) continue;
    let ans = null;
    if (q.interaction_id) {
      const needle = `確認ID: ${q.interaction_id}`;
      for (let j = i + 1; j < messages.length; j++) {
        if (messages[j].role === 'user' && String(messages[j].content || '').includes(needle)) {
          ans = _extractSelection(messages[j].content); break;
        }
      }
    }
    if (ans) answered[i] = ans; else unanswered.push(i);
  }
  return { answered, operableIdx: unanswered.length ? unanswered[unanswered.length - 1] : -1 };
}
// 保存済み確認カードを再構築する。operable（未回答の最新）以外は選択内容を反映して disabled にする。
export function appendRestoredQuestion(q, answeredSel, operable) {
  const el = appendQuestion(q);
  if (operable) return el;
  const card = el.querySelector('.askcard');
  if (card) card.classList.add('answered');
  if (answeredSel) {
    el.querySelectorAll('[data-qopt]').forEach((inp) => {
      if (answeredSel.picked.includes(inp.dataset.label)) inp.checked = true;
    });
    const freeEl = el.querySelector('[data-qfree]');
    if (freeEl && answeredSel.free) freeEl.value = answeredSel.free;
    if (card) {
      const sel = [answeredSel.picked.join('、'), answeredSel.free].filter(Boolean).join(' / ');
      const note = document.createElement('div');
      note.className = 'askanswered muted';
      note.textContent = sel ? `回答済み: ${sel}` : '回答済み';
      card.appendChild(note);
    }
  }
  el.querySelectorAll('input,textarea,button').forEach((x) => { x.disabled = true; });
  return el;
}
function onQuestion(thinking, q) {
  turnConcluded = true;
  _stopThinkingTicker();
  _finalizeLiveTraceTree(null);   // 確認待ちは終了理由を出さない
  if (thinking && thinking.isConnected) thinking.remove();
  S.cid = q.conversation_id || S.cid;
  appendQuestion(q);
  setRt('確認待ち', false);
  setSendButtonStopping(false);
  $('messages').setAttribute('aria-busy', 'false');
  if (S.es) { S.es.close(); S.es = null; S.turnId = null; }
  loadConversations();
}
// ターンが異常終了したときの表示
function onTurnFailed(thinking, message) {
  turnConcluded = true;
  clearReveal();
  _stopThinkingTicker();
  _finalizeLiveTraceTree(stopReasonInfo(stopReasonCategoryFromError(message)));
  if (thinking && thinking.isConnected) thinking.remove();
  appendAssistantRaw(`<div class="stopped-note muted">（${esc(message || 'エラーが発生しました')}）</div>`);
  setRt('エラー', false);
  setSendButtonStopping(false);
  $('messages').setAttribute('aria-busy', 'false');
  if (S.es) { S.es.close(); S.es = null; }
  S.turnId = null;
  loadConversations();
}

// ===== 送信（背景実行）=====
// 停止フロー専用の状態（このファイル内で完結）。新しいターンの購読開始時にリセットし、持ち越さない。
//   turnGen: 開始 POST の発行・会話遷移（invalidateStopContext）のたびに進む世代。非同期処理は開始時の世代を捕捉し、await 後に進んでいたら何もしない。
//   turnConcluded: そのターンが終端イベント（stopped/error/answer/question）で決着済みか。決着後の停止 POST 応答は無視する。
//   stopState: null=停止要求なし／'pending'=停止 POST の結果待ち／'ok'=停止確認済み／'failed'=停止 POST 失敗。
//   stopOnerrorFired: 'pending' の間に onerror が先着したか。
//   pendingStopThinking: その時点の思考枠。停止 POST が失敗と分かったら文言を訂正する。
let turnGen = 0;
let turnConcluded = false;
let stopState = null;
let stopOnerrorFired = false;
let pendingStopThinking = null;
// pendingStopThinking の v2 版。暫定表示した TraceTreeV2 の終了理由を、停止 POST 失敗時に「接続エラー」へ訂正する。
let pendingStopTraceTree = null;
// 送信ボタンを「■ 停止」と「↑ 送信」で切り替える。
export function setSendButtonStopping(on) {
  const btn = $('send');
  btn.classList.toggle('stopping', !!on);
  btn.textContent = on ? '■' : '↑';
  btn.title = on ? '停止' : '送信';
  btn.disabled = false;
}
// 実行中なら停止、そうでなければ送信する。
export function sendOrStop() {
  if (S.es) { stopStream(); return; }
  send();
}
// 会話遷移時（history.js の unsubscribeTurn）に世代を進め、保留中の停止 POST の遅延応答を無効にする。
export function invalidateStopContext() {
  turnGen++;
}
// 今の世代。遅れて届いた結果がまだ画面の持ち主かを呼び出し元が確かめるのに使う。
export function currentTurnGen() {
  return turnGen;
}
// 実行中ターンの停止要求を送り、結果に応じて表示を戻す。
async function stopStream() {
  if (!S.es) return;
  const myGen = turnGen;
  stopState = 'pending'; stopOnerrorFired = false; pendingStopThinking = null; pendingStopTraceTree = null;
  const tid = S.turnId;
  $('send').disabled = true;
  let acknowledged = false;
  if (tid) {
    try {
      const r = await fetch(`/chat/turns/${encodeURIComponent(tid)}/stop`, { method: 'POST' });
      const d = await r.json().catch(() => ({}));
      acknowledged = r.ok && d.ok === true;
    } catch (e) { /* ネットワークエラー → 下のフォールバックへ */ }
  }
  // 世代が進んだ・決着済みなら結果は無関係＝共有状態に触れず何もしない
  if (turnGen !== myGen || turnConcluded) return;
  stopState = acknowledged ? 'ok' : 'failed';
  if (!acknowledged && stopOnerrorFired) {
    // 暫定表示した「停止しました」を、本物の接続断として訂正する
    setRt('接続エラー。もう一度お試しください。', false);
    if (pendingStopThinking && pendingStopThinking.isConnected) {
      const t = pendingStopThinking.querySelector('.thinking');
      if (t) t.textContent = '接続エラー。もう一度お試しください。';
    }
    if (pendingStopTraceTree) pendingStopTraceTree.correctStopReason(stopReasonInfo('error'));
  }
  pendingStopThinking = null;
  pendingStopTraceTree = null;
  if (acknowledged) { $('send').disabled = false; return; }   // サーバの stopped イベントを待つ
  if (stopOnerrorFired) return;   // onerror が UI を戻し済み
  // 停止要求が失敗/対象なしのときだけクライアント側で閉じる（成功時に閉じると stopped イベントを取りこぼす）
  _stopThinkingTicker();
  _finalizeLiveTraceTree(stopReasonInfo('error'));
  if (S.es) { S.es.close(); S.es = null; }
  S.turnId = null;
  setRt('待機中', false);
  setSendButtonStopping(false);
  $('messages').setAttribute('aria-busy', 'false');
}
function onStopped(thinking) {
  turnConcluded = true;
  clearReveal();
  _stopThinkingTicker();
  _finalizeLiveTraceTree(stopReasonInfo('stopped'));
  if (thinking && thinking.isConnected) thinking.remove();
  // 逐次表示中だった回答カードはそのまま残す（部分表示は保存されない）
  appendAssistantRaw('<div class="stopped-note muted">（停止しました）</div>');
  setRt('停止しました', false);
  setSendButtonStopping(false);
  $('messages').setAttribute('aria-busy', 'false');
  if (S.es) { S.es.close(); S.es = null; }
  S.turnId = null;
  loadConversations();
}
// ターンの SSE（GET /chat/turns/{id}/stream）を cursor=0 から購読し、イベントごとに表示を更新する。新規送信・再購読の両方から呼ぶ。
export function subscribeTurn(thinking) {
  // 既存の購読があれば閉じてから作る
  if (S.es) { S.es.close(); S.es = null; }
  // send() を経由しない呼び出し元（resumeRunningTurn）のためにここでも世代を進める
  turnGen++;
  stopState = null; stopOnerrorFired = false; pendingStopThinking = null; pendingStopTraceTree = null; turnConcluded = false;
  S.es = new EventSource(`/chat/turns/${encodeURIComponent(S.turnId)}/stream?cursor=0`);
  S.es.onmessage = (ev) => {
    const e = JSON.parse(ev.data);
    if (e.type === 'trace_meta') { onTraceMeta(e); return; }   // 先頭の1件で v1/v2 を判定する
    if (e.type === 'node') { if (S.liveTraceTree) S.liveTraceTree.addOrUpdate(e); else onNode(e); return; }
    if (e.type === 'question') { onQuestion(thinking, e); return; }
    if (e.type === 'stopped') { onStopped(thinking); return; }
    if (e.type === 'error') { onTurnFailed(thinking, e.message); return; }
    if (e.type === 'answer_delta') { ensureAnswerCard(thinking); reveal(e.text); return; }
    if (e.type === 'answer') {
      turnConcluded = true;
      S.cid = e.conversation_id;
      // trace があれば、ライブ中のターン要素への再展開ボタンを回答カードに添える
      const turnId = (e.message.trace && e.message.trace.length) ? S.liveTurnId : null;
      // v2 のときだけ終了理由（evidence_packet.stop_reason）を明示して階層ツリーを畳む
      _stopThinkingTicker();
      _finalizeLiveTraceTree(e.message.answer.trace_version === 2
        ? deriveTraceStopReason(e.message.answer) : null);
      finalizeAnswer(thinking, e.message.answer, turnId, e.message.id, e.message.trace);
      updateScopeHeader(e.message.answer.scope);   // 実際に使われた範囲をヘッダに反映
      // 個人コンテンツを含む会話は共有不可にする
      if (e.message.answer && (e.message.answer.personal_sources || e.message.answer.codex_wrote_files)) {
        S.convHasPersonal = true; updateShareButtonState();
      }
      const completion = e.message.answer.completion;
      setRt(completion === 'stopped' ? '停止しました' : completion === 'partial' ? '途中までの回答'
        : completion === 'failed' || e.message.answer.agentic_failure === 'error' ? 'エラー' : '完了', false);
      S.es.close(); S.es = null; S.turnId = null;
      setSendButtonStopping(false); $('messages').setAttribute('aria-busy', 'false'); loadConversations();
    }
  };
  S.es.onerror = () => {
    // 停止 POST 成功済み（'ok'）なら停止、結果待ち（'pending'）なら暫定で停止表示（stopStream が必要なら訂正する）。
    // 停止要求なし／失敗済みは本物の接続断として扱う。
    const treatAsStopped = stopState === 'ok' || stopState === 'pending';
    const wasPending = stopState === 'pending';
    if (wasPending) { stopOnerrorFired = true; pendingStopThinking = thinking; } else { turnConcluded = true; }
    _stopThinkingTicker();
    const tree = _finalizeLiveTraceTree(stopReasonInfo(treatAsStopped ? 'stopped' : 'error'));
    if (wasPending) pendingStopTraceTree = tree;
    setRt(treatAsStopped ? '停止しました' : '待機中', false); S.es.close(); S.es = null; S.turnId = null; clearReveal();
    setSendButtonStopping(false); $('messages').setAttribute('aria-busy', 'false');
    if (thinking.isConnected) {
      const t = thinking.querySelector('.thinking');
      if (t) t.textContent = treatAsStopped ? '（停止しました）' : '接続エラー。もう一度お試しください。';
    }
  };
}
// 入力欄の質問を送信する（POST /chat/turns → SSE 購読）。
// override（省略可）: {lens, layer, scope_paths, depth_profile, tools} の指定キーだけ、この1回の送信でブロックの設定の代わりに使う（確認カードの回答再送用）。
export async function send(override) {
  // S.es（ストリーミング中）と S.sending（開始 POST 応答待ち）の両方で二重送信を防ぐ
  if (S.es || S.sending) return;
  const message = $('input').value.trim();
  if (!message) return;
  S.sending = true;
  S.turnStartedAtMs = Date.now();
  $('input').value = '';
  const w = $('messages').querySelector('.welcome-msg'); if (w) w.remove();
  appendUser(message);
  S.ansEl = null; S.ansHead = null; clearReveal();
  setSendButtonStopping(true);
  $('send').disabled = true;
  $('messages').setAttribute('aria-busy', 'true');
  const thinking = appendAssistantRaw('<div class="thinking loading-inline" role="status"><span class="spinner spinner-sm"></span><span>回答を準備しています...</span></div>');
  startThinkingTicker(thinking);
  startFlow(message);
  const body = { message, world: $('version').value, knowledge: !!S.kb, personal: !!S.personal && !!S.kb };   // 社内資料オフでは個人ファイルも読まない
  if (S.cid) body.conversation_id = S.cid;
  // Web 検索は true のときだけ載せる
  if (S.kb && S.webSearch && !isSimpleMode()) body.web_search = true;
  // 範囲・調べ方・探す対象・深さ・検索経路はナレッジ参照オンのときだけ送り、既定値は省略する。override のキーはこの1回だけ優先する
  if (S.kb) {
    const ov = override || {};
    const scopePaths = Object.prototype.hasOwnProperty.call(ov, 'scope_paths') ? ov.scope_paths : S.scope;
    const lens = Object.prototype.hasOwnProperty.call(ov, 'lens') ? ov.lens : S.lens;
    const layer = Object.prototype.hasOwnProperty.call(ov, 'layer') ? ov.layer : S.layer;
    const depthProfile = Object.prototype.hasOwnProperty.call(ov, 'depth_profile') ? ov.depth_profile : S.depthProfile;
    // override の tools は解決済みの値＝全軸明示扱いで省略せず渡す
    const isOverride = Object.prototype.hasOwnProperty.call(ov, 'tools');
    const tools = isOverride ? ov.tools : S.tools;
    body.scope_paths = scopePaths || [];
    // 簡易は調べ方・深さ・検索経路が効かない（画面でも隠している）＝送らない。
    const simple = isSimpleMode();
    if (!simple && lens && lens !== 'auto') body.lens = lens;
    if (layer && layer !== 'both') body.layer = layer;
    if (!simple && depthProfile && depthProfile !== 'standard') body.depth_profile = depthProfile;
    // 未操作の既定 ON だけを省略する（toolsForSend）。空になれば body.tools 自体を省く
    if (tools && !simple) {
      const sendTools = toolsForSend(tools, isOverride ? { grep: true, fulltext: true, graph: true } : undefined);
      if (Object.keys(sendTools).length) body.tools = sendTools;
      // 会話へ保存する明示状態は実際の操作履歴（S.toolsExplicit）だけ。override の全軸 true は保存しない
      const sendExplicit = toolsExplicitForSend(S.toolsExplicit);
      if (sendExplicit.length) body.tools_explicit = sendExplicit;
    }
  }
  // 開始 POST の前に世代を進めて捕捉する（await 後に世代が進んでいたら何もしない。ターンは続行し resumeRunningTurn が拾う）
  turnGen++;
  const myGen = turnGen;
  let started;
  try {
    // timeoutMs 必須（省略すると無期限待ちで S.sending が解除されず送信不能になる）
    started = await Sherpa.api('POST', '/chat/turns', body, { timeoutMs: 30000 });
  } catch (e) {
    // 世代照合は S.sending の解除より先に行う（世代不一致なら後続のターンの共有状態に触れない）
    if (turnGen !== myGen) return;
    S.sending = false;
    setRt('待機中', false); setSendButtonStopping(false); $('messages').setAttribute('aria-busy', 'false');
    if (thinking.isConnected) {
      const t = thinking.querySelector('.thinking');
      if (t) t.textContent = (e && e.message) || '送信に失敗しました。もう一度お試しください。';
    }
    return;
  }
  // ここでも世代照合を先に行う
  if (turnGen !== myGen) return;
  S.sending = false;
  $('send').disabled = false;
  // 受理されたら調べ方ブロックを畳む
  setInquiryOpen(false);
  S.cid = started.conversation_id;
  syncConvParam(S.cid);
  S.turnId = started.turn_id;
  subscribeTurn(thinking);
  loadConversations();
}
