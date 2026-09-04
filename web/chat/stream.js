// フェーズ6 S7（リファクタリング計画）: SSE 購読・flow ライブ描画・停止フロー・send を chat.js から
// 純移動（危険地雷4「ack 後も _es を close しない」設計コメント・地雷5 resumeRunningTurn 世代チェックの
// 前提はコードごと不変のまま移した）。resumeRunningTurn 自体は S6 で既に web/chat/history.js に配置済み
// （会話を開いた時の再購読という役割で history ドメインに定着済み・現在地は chat.js ではなく history.js
// のため本スライスの「chat.js からの純移動」対象には含まれない）。ただし history.js が呼んでいた
// startFlow/subscribeTurn/setSendButtonStopping/resetFlow がこのファイルへ移るため、history.js 側の
// import 元を '../chat.js' からこのファイルへ更新した（history.js 冒頭コメント参照）。
// _questionAnswerState/appendRestoredQuestion/appendQuestion/_extractSelection（確認カード復元系）は
// S6 で「物理的には『思考の流れ』節＝stream.js 予定に属する」と判定済みのため、ここに含めて純移動する。
// export は chat.js（entry）側に残る呼び出し元（$('send') クリック・$('input') keydown・
// data-ask-submit／data-showtrace の delegate ハンドラ）と、web/chat/history.js 側の呼び出し元
// （newConversation/openConversation/resumeRunningTurn が呼ぶ startFlow/subscribeTurn/
// setSendButtonStopping/resetFlow/_questionAnswerState/appendRestoredQuestion、および
// unsubscribeTurn が呼ぶ invalidateStopContext）から参照される関数のみに絞る
// （onNode/onQuestion/onTurnFailed/onStopped/stopStream/_fmtElapsed/appendQuestion/
// _extractSelection は内部専用のまま非公開）。
// S8（リファクタリング計画）: updateScopeHeader は chat.js から web/chat/scope.js へ純移動したため
// import 元をそちらへ更新した。updateShareButtonState はどのドメインにも属さない横断ユーティリティの
// ため引き続き chat.js 側にあり `import {...} from '../chat.js'` する（chat.js↔stream.js の
// 意図した循環 import。関数宣言＝hoisted のため実行時に呼ぶ限り ESM で安全＝render.js の setRt・
// history.js の copyText 等これまでのスライスと同じパターン）。
'use strict';

import { S } from './state.js';
import {
  questionHTML, _renderDetail, appendAssistantRaw, appendUser,
  ensureAnswerCard, finalizeAnswer, clearReveal, reveal,
  TraceTreeV2, deriveTraceStopReason, stopReasonInfo, stopReasonCategoryFromError,
} from './render.js';
import { loadConversations } from './history.js';
import { updateScopeHeader } from './scope.js';
import { migrateInquiryOpenPref, toolsForSend } from './inquiry.js';
import { updateShareButtonState } from '../chat.js';

const $ = Sherpa.$, esc = Sherpa.esc, fmtDateTime = Sherpa.fmtDateTime;   // 共通ユーティリティ（common.js）

// ===== 思考の流れ（右ペイン） =====
// render.js の renderTurnStack からも呼ばれるため export（S7 で chat.js↔render.js だった循環 import が
// stream.js↔render.js に付け替わった。関数宣言＝hoisted のため実行時に呼ぶ限り安全＝既存パターンと同型）。
export function setRt(text, live) {
  const rt = $('rt'); rt.classList.toggle('live', !!live);
  rt.lastChild.textContent = text;
}
export function resetFlow() {
  // 会話切替（history.js の unsubscribeTurn→newConversation/openConversation が呼ぶ）でも両方の
  // ティックを止める（この時点でターン自体が終端したとは限らないため、finalize の終了理由 note は
  // 出さず destroy/stop だけに留める）。
  _stopThinkingTicker();
  if (S.liveTraceTree) { S.liveTraceTree.destroy(); S.liveTraceTree = null; }   // ティックの残骸を片付ける
  $('flow').innerHTML = '<div class="hint">質問すると、考えた道筋がここに流れます。</div>';
  S.nodes = {}; S.liveTurnId = null; S.turnSeq = 0;
  setRt('待機中', false);
}
// UIフィードバック（RV再検証 MEDIUM）: 単一展開（アコーディオン）を徹底する共有ヘルパ。
// querySelector は最初の1件しか返さないため querySelectorAll で**全て**閉じる
// （過去ターンをボタンで開いた後に送信すると複数 open が残っていた不具合の修正）。
// S7: エントリ（chat.js）の data-showtrace ハンドラからも呼ばれるため export。
export function _closeOtherTurns(exceptEl) {
  $('flow').querySelectorAll('details.fturn[open]').forEach((d) => { if (d !== exceptEl) d.open = false; });
}
// UIフィードバック（2026-07-03）: 積み上げ表示。既存の積み上げ（会話ロード直後 or 前ターン）が
// あれば開いている過去ターンを全て畳んでから、新ターン（ライブ）を末尾に追記する。積み上げが無い
// （新規会話・最初のターン）場合はプレースホルダを消して最初のターンとして追加する。
export function startFlow(question) {
  const flow = $('flow');
  _closeOtherTurns(null);
  const hint = flow.querySelector(':scope > .hint'); if (hint) hint.remove();
  const id = `fturn-${S.turnSeq++}`;
  const det = document.createElement('details');
  det.className = 'fturn'; det.id = id; det.open = true;
  det.innerHTML = '<summary class="fturn-head"><span class="fturn-q"></span><span class="fturn-time"></span></summary>'
    + '<div class="fturn-body"></div>';
  det.querySelector('.fturn-q').textContent = (question || '').slice(0, 40);   // textContent＝XSS安全
  det.querySelector('.fturn-time').textContent = fmtDateTime(new Date().toISOString());
  flow.appendChild(det);
  S.nodes = {}; S.liveTurnId = id;
  setRt('リアルタイム', true);
}
// A1: 経過時間の表示形式（0.8s / 1.2s・tabular-nums）。300ms 未満は呼び出し側で出さない。
function _fmtElapsed(ms) { return (ms / 1000).toFixed(1) + 's'; }
function onNode(e) {                       // 思考/ツールのノードを id で動的に追加・更新（数は可変）
  let el = S.nodes[e.id];
  if (!el) {
    el = document.createElement('div');
    el.className = 'fstep' + (e.kind === 'tool' ? ' tool' : '');
    el.innerHTML = '<div class="fnode"></div><div class="fbody">'
      + '<div class="fhead"><div class="flabel"></div><span class="ftime" hidden></span></div>'
      + '<div class="fdetail"></div></div>';
    el._details = [];                      // A2: detail 履歴（変化時のみ蓄積）
    el._t0 = performance.now();            // A1: 初回受信時刻
    // 積み上げ表示: ライブ中のターン要素（#fturn-N の .fturn-body）に追記する。万一見つからなければ
    // 従来どおり #flow 直下へ（フォールバック・起き得ない想定だが描画は止めない）。
    const liveBody = S.liveTurnId && document.getElementById(S.liveTurnId)
      ? document.getElementById(S.liveTurnId).querySelector('.fturn-body') : null;
    (liveBody || $('flow')).appendChild(el); S.nodes[e.id] = el;
  }
  el.classList.remove('active', 'done'); el.classList.add(e.status);
  el.querySelector('.flabel').textContent = e.label;    // textContent ＝ XSS安全
  _renderDetail(el.querySelector('.fdetail'), e);       // A3: ツールのクエリはチップ化
  el.querySelector('.fnode').textContent = e.status === 'done' ? '✓' : '';
  // A2: detail が変わったら履歴として蓄積（直近は .fdetail に表示済み・過去分は隠し領域へ）。
  const d = e.detail || '';
  if (d && d !== el._details[el._details.length - 1]) el._details.push(d);
  if (el._details.length >= 2) {                        // 2件以上で展開可能（履歴インジケータを出す）
    let hb = el.querySelector('.fhist');
    let list = el.querySelector('.fhist-list');
    if (!hb) {                                          // 履歴が要るステップにだけ制御を追加（DOM を汚さない）
      hb = document.createElement('button');
      hb.className = 'fhist'; hb.type = 'button'; hb.setAttribute('aria-expanded', 'false');
      hb.innerHTML = '<span class="fhtxt"></span> <span class="caret" aria-hidden="true">▾</span>';
      list = document.createElement('div'); list.className = 'fhist-list';
      const body = el.querySelector('.fbody'); body.appendChild(hb); body.appendChild(list);
    }
    hb.querySelector('.fhtxt').textContent = `履歴 ${el._details.length}`;
    list.textContent = '';                              // 過去 detail を textContent で再構築（XSS安全）
    el._details.slice(0, -1).forEach((t) => {
      const it = document.createElement('div'); it.className = 'fhist-item'; it.textContent = t;
      list.appendChild(it);
    });
  }
  // A1: done になった初回に経過時間（≥300ms）を右肩へ。以後は上書きしない（ストリーム後も残す）。
  if (e.status === 'done' && !el._timed) {
    el._timed = true;
    const ms = performance.now() - el._t0;
    if (ms >= 300) {
      const t = el.querySelector('.ftime'); t.textContent = _fmtElapsed(ms); t.hidden = false;
    }
  }
  $('flow').scrollTop = $('flow').scrollHeight;
}
// ===== EXT-4（拡張設計 §10）: trace_version=2 のライブ階層描画 =====
// v1（onNode・直上）は無改修のまま残す。ストリーム先頭の `trace_meta` マーカー（chat_service.py が
// 送出）で今回のターンが v2 かどうかを判定してから初めて `S.liveTraceTree` を張る＝v1 のターンでは
// 一切生成されず、集約等の新しい見た目が一切混じらない（後方互換契約）。
function _liveBodyEl() {
  const liveBody = S.liveTurnId && document.getElementById(S.liveTurnId)
    ? document.getElementById(S.liveTurnId).querySelector('.fturn-body') : null;
  return liveBody || $('flow');
}
function onTraceMeta(e) {
  if (S.liveTraceTree) { S.liveTraceTree.destroy(); S.liveTraceTree = null; }   // 前ターンの残骸を多層防御で片付ける
  // タブが非表示のまま生成すると、visibilitychange の隠蔽時破棄（生成済みのツリーだけが対象）を
  // すり抜けて誰も見ていないティック（setInterval）が動き続ける。非表示中は live:false で生成し、
  // ノード自体は addOrUpdate で引き続き正しく描画される（ティックだけを止める）。
  if (e.trace_version === 2) {
    const hidden = typeof document !== 'undefined' && document.hidden;
    S.liveTraceTree = new TraceTreeV2(_liveBodyEl(), { live: !hidden, startedAtMs: S.turnStartedAtMs });
  }
}
// ターン終端（answer/stopped/error/question）で必ず呼ぶ（呼ばないとティック用 setInterval が
// 残り続ける）。`stopInfo` は `render.js` の `deriveTraceStopReason`/`stopReasonInfo` が組む
// `{text, interrupted}`（または note を出さず畳むだけの `null`＝onQuestion 等）。
// 戻り値は finalize したツリー本体（`null`＝そもそも張られていなかった）。呼び出し元が「終了理由は
// 暫定表示で、後から訂正が必要かもしれない」ケース（停止 POST 結果待ち中の onerror）で
// `correctStopReason` を後で呼べるように渡す。
function _finalizeLiveTraceTree(stopInfo) {
  if (!S.liveTraceTree) return null;
  const tree = S.liveTraceTree;
  tree.finalize(stopInfo || null);
  S.liveTraceTree = null;
  return tree;
}
// 待ち時間（LLM 応答待ち・特にサーバが最初のイベントを出すまでの無音区間）を
// 「止まって見えない」ようにする——ティックは v1/v2 どちらの会話にも共通で効かせる（trace 階層描画とは
// 独立の改善・trace_version の判定を待たずに動く）。`thinking` 要素が DOM から外れたら（回答到着/停止/
// エラーいずれの経路でも）自己判定で止める＝個々の除去箇所を毎回書き換えなくてよい。
// 自己判定（isConnected）だけに頼らず、ターン終端の各経路
// （onQuestion/onTurnFailed/onStopped/停止 POST 失敗/onerror/visibilitychange）で明示的にも
// 止める（`_thinkingTickerStop` に現在稼働中のティックの停止関数を保持・同時に1本しか動かない
// 前提＝このチャット画面は同時に1ターンしか実行しない）。
// history.js の resumeRunningTurn（会話に戻ったときの再購読）でも同じ「考え中」ティックを使うため export。
let _thinkingTickerStop = null;
export function startThinkingTicker(thinkingEl) {
  if (_thinkingTickerStop) _thinkingTickerStop();   // 前ターンの残骸を多層防御で片付ける
  const startedAt = S.turnStartedAtMs || Date.now();   // 再購読＝遷移後もターン開始からの通算
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
// タブが非表示になったら both のティックを止める（バックグラウンドタブでの無駄な再描画・
// ネットワークが黙って切れた場合に interval だけ残り続ける事故を避ける）。
// 再表示時の自動再開はしない（実イベントが届けば addOrUpdate が引き続き正しく描画するため、
// ここで復元すべき状態は「考え中」の秒数表示だけ＝優先度低いと判断）。
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
// S1（ask_user-improvements.md）: 履歴に保存された確認カード（answer.question）の復元。
// 回答済み判定は「それ以降の user メッセージに同じ『確認ID: {interaction_id}』が含まれるか」。
// 回答すると新ターンが起きる設計なので、未回答は通常1件（最後尾）。未回答の最後だけ操作可能にする。
function _extractSelection(content) {   // 回答メッセージ（[data-ask-submit] が組み立てた整形文）から選択/補足を取り出す
  const lines = String(content || '').split('\n');
  const val = (prefix) => { const l = lines.find((x) => x.startsWith(prefix)); return l ? l.slice(prefix.length) : ''; };
  const pickStr = val('選択: ');
  return { picked: pickStr ? pickStr.split('、') : [], free: val('補足: ') };
}
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
// 保存済み確認カードを再構築する。operable=未回答の最新カード（そのまま操作可能・_question で再送）。
// それ以外（回答済み／未回答だが最新でない）は選択内容を反映して disabled にする。
export function appendRestoredQuestion(q, answeredSel, operable) {
  const el = appendQuestion(q);   // questionHTML で askcard を再構築し el._question をセット（操作可能時の再送に使う）
  if (operable) return el;
  const card = el.querySelector('.askcard');
  if (card) card.classList.add('answered');
  if (answeredSel) {              // 回答済み: 選択済みオプション/補足を見た目にも反映してから disabled にする
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
  turnConcluded = true;   // このターンは終端イベント（question）で決着済み
  _stopThinkingTicker();
  _finalizeLiveTraceTree(null);   // 確認待ちは終了理由を出さずティックだけ止める
  if (thinking && thinking.isConnected) thinking.remove();
  S.cid = q.conversation_id || S.cid;
  appendQuestion(q);
  setRt('確認待ち', false);
  setSendButtonStopping(false);
  $('messages').setAttribute('aria-busy', 'false');
  if (S.es) { S.es.close(); S.es = null; S.turnId = null; }   // サーバ側は question 送出後に自然終了済み（停止要求は不要）
  loadConversations();
}
// ターンが background thread 側で未捕捉の例外に倒れた場合の表示（多層防御・通常は起こらない想定）。
function onTurnFailed(thinking, message) {
  turnConcluded = true;   // このターンは終端イベント（error）で決着済み
  clearReveal();
  _stopThinkingTicker();
  _finalizeLiveTraceTree(stopReasonInfo(stopReasonCategoryFromError(message)));   // 期限/エラーを区別する
  if (thinking && thinking.isConnected) thinking.remove();
  appendAssistantRaw(`<div class="stopped-note muted">（${esc(message || 'エラーが発生しました')}）</div>`);
  setRt('エラー', false);
  setSendButtonStopping(false);
  $('messages').setAttribute('aria-busy', 'false');
  if (S.es) { S.es.close(); S.es = null; }
  S.turnId = null;
  loadConversations();
}

// ===== 送信（背景実行・覗き窓方式）=====
// 停止フロー専用の状態（このファイル内で完結・S には乗せない）。
//   turnGen: 新しいターンを開始する（開始 POST を投げる）たび、および会話遷移（history.js の
//     unsubscribeTurn 経由の invalidateStopContext）のたびに進む世代カウンタ。停止処理は
//     開始時の世代を捕捉し、await の後で世代が進んでいたら（＝次のターンが既に開始済み、または
//     今のターンへの関心を手放す遷移が起きた。開始 POST 発行〜購読確立の間もこの世代とみなす）
//     一切何もしない。
//   turnConcluded: 捕捉した世代のまま、そのターンが終端イベント（stopped/error/answer/question）
//     で既に決着済みか。決着後に届く停止 POST の遅延応答は、次のターンが始まっていなくても
//     もう関係ない（決着済みの表示を上書きしない）ため no-op にする。
//   stopState: null=停止要求なし／'pending'=停止 POST の結果待ち／'ok'=停止確認済み／'failed'=停止 POST 失敗。
//   stopOnerrorFired: stopState が 'pending' の間に onerror が先着したか。DOM（思考枠が
//     まだ繋がっているか）とは独立に持つ（部分回答が既に思考枠を消していても、onerror 先着の
//     事実そのものは変わらないため）。
//   pendingStopThinking: stopOnerrorFired 時点の思考枠への参照（ベストエフォート）。停止 POST の
//     結果が確定した時点で、暫定表示が誤り（POST 失敗＝本物の接続断だった）と分かったら、
//     思考枠がまだ存在すればその文言を訂正する（#rt の訂正は思考枠の有無に関わらず必須）。
// stopState/stopOnerrorFired/pendingStopThinking/turnConcluded は新しいターンの開始時に
// リセットするため次ターンへは持ち越さない。
let turnGen = 0;
let turnConcluded = false;
let stopState = null;
let stopOnerrorFired = false;
let pendingStopThinking = null;
// pendingStopThinking と同じ役割の v2 版——停止 POST 結果待ち中に onerror が先着して
// 「停止操作」を暫定表示した TraceTreeV2 への参照。停止 POST が実は失敗だったと後から判明したら
// stopStream 側がこれの終了理由を「接続エラー」へ訂正する。
let pendingStopTraceTree = null;
// UI フィードバック1（途中停止）: 送信ボタンはストリーミング中「■ 停止」に切り替わる（同じボタンを併用）。
export function setSendButtonStopping(on) {
  const btn = $('send');
  btn.classList.toggle('stopping', !!on);
  btn.textContent = on ? '■' : '↑';
  btn.title = on ? '停止' : '送信';
  btn.disabled = false;
}
// S7: エントリ（chat.js）の $('send') クリックリスナーから呼ばれるため export。
export function sendOrStop() {
  if (S.es) { stopStream(); return; }
  send();
}
// history.js の unsubscribeTurn（新規チャット・会話切替の共通経路）から呼ぶ。会話遷移の時点で
// 世代を進め、保留中の停止 POST（onerror が既に先着し、S.es は null になっているが停止 POST
// 自体はまだ結果待ちのケースを含む）の遅延応答を、遷移先の画面に対する no-op にする
// （turnConcluded 等は次の subscribeTurn/停止処理が改めて初期化するため、ここでは世代の
// 前進だけで十分＝ stopStream() の世代ガードに乗る）。
export function invalidateStopContext() {
  turnGen++;
}
async function stopStream() {
  if (!S.es) return;
  const myGen = turnGen;   // 停止対象のターン世代を捕捉
  stopState = 'pending'; stopOnerrorFired = false; pendingStopThinking = null; pendingStopTraceTree = null;
  const tid = S.turnId;
  $('send').disabled = true;   // 停止処理中は二重クリックを防ぐ（結果が来たら setSendButtonStopping が戻す）
  let acknowledged = false;
  if (tid) {
    try {
      const r = await fetch(`/chat/turns/${encodeURIComponent(tid)}/stop`, { method: 'POST' });
      const d = await r.json().catch(() => ({}));
      acknowledged = r.ok && d.ok === true;
    } catch (e) { /* ネットワークエラー → 下のフォールバックへ */ }
  }
  // 世代が変わっていた（次のターンが既に開始済み）、またはこのターンが既に終端イベントで
  // 決着済みなら、この結果はもう関係ない。共有状態（pendingStopThinking 等）には一切触れない
  // 純粋な no-op にする（他ターンの停止処理が使っている途中の状態を消してしまわないため）。
  if (turnGen !== myGen || turnConcluded) return;
  stopState = acknowledged ? 'ok' : 'failed';
  if (!acknowledged && stopOnerrorFired) {
    // onerror が結果待ちの間に先着し「停止しました」を暫定表示していたが、停止 POST 自体が
    // 失敗した（＝停止起因ではない本物の接続断だった）ため #rt を訂正する（必須）。思考枠は
    // 部分回答の描画で既に消えている場合があるため、まだ繋がっていれば文言も訂正する。
    setRt('接続エラー。もう一度お試しください。', false);
    if (pendingStopThinking && pendingStopThinking.isConnected) {
      const t = pendingStopThinking.querySelector('.thinking');
      if (t) t.textContent = '接続エラー。もう一度お試しください。';
    }
    // onerror が同じ楽観的想定で v2 トレースの終了理由を「停止操作」として表示済みなので、
    // #rt/思考枠と同じく「接続エラー」へ訂正する。
    if (pendingStopTraceTree) pendingStopTraceTree.correctStopReason(stopReasonInfo('error'));
  }
  pendingStopThinking = null;
  pendingStopTraceTree = null;
  if (acknowledged) { $('send').disabled = false; return; }   // サーバの {"type":"stopped"} を待つ（onStopped が UI を戻す）。
  if (stopOnerrorFired) return;   // onerror が既に UI（es/turnId/ボタン/aria-busy）を戻し終えている＝表示訂正のみで完了
  // ここから下は onerror がまだ発火していない場合のフォールバック。ここで即 S.es.close() すると、
  // サーバがちょうど {"type":"stopped"} を配送中だった場合に受信前にコネクションを切ってしまい
  // 表示が更新されない競合が起きる（e2e で実際に踏んだ）。停止要求そのものが失敗/対象なしの
  // ときだけ、クライアント側だけで閉じて UI を戻す。
  _stopThinkingTicker();          // 停止 POST 失敗経路
  _finalizeLiveTraceTree(stopReasonInfo('error'));
  if (S.es) { S.es.close(); S.es = null; }
  S.turnId = null;
  setRt('待機中', false);
  setSendButtonStopping(false);
  $('messages').setAttribute('aria-busy', 'false');
}
function onStopped(thinking) {
  turnConcluded = true;   // このターンは終端イベント（stopped）で決着済み
  clearReveal();   // 逐次描画（answer_delta のタイピング演出）を止める
  _stopThinkingTicker();
  _finalizeLiveTraceTree(stopReasonInfo('stopped'));
  if (thinking && thinking.isConnected) thinking.remove();
  // S.ansEl（逐次表示中だった回答カード）が既にあればそのまま残す＝部分表示はサーバに保存されない
  // （リロード/会話再読込では消える）だけで、このセッション内の表示としては自然に見せる。
  appendAssistantRaw('<div class="stopped-note muted">（停止しました）</div>');
  setRt('停止しました', false);
  setSendButtonStopping(false);
  $('messages').setAttribute('aria-busy', 'false');
  if (S.es) { S.es.close(); S.es = null; }
  S.turnId = null;
  loadConversations();
}
// EventSource の onmessage/onerror を1本化（新規送信・実行中ターンへの再購読の両方から呼ぶ）。
// cursor は常に 0（バッファは有界・軽量なので全replayで十分＝サーバ側の cursor 対応は
// 「途中切断→明示的な再購読」の API 契約として提供する・ブラウザ標準の自動再接続も同じ URL
// （cursor=0）にそのまま乗るので追加のカーソル管理は不要）。
export function subscribeTurn(thinking) {
  // 呼び出し元（resumeRunningTurn の世代チェック等）が確認済みでも、念のため
  // 既存の購読があれば閉じてから新規作成する（EventSource の張りっぱなしリーク・二重購読の多層防御）。
  if (S.es) { S.es.close(); S.es = null; }
  // send() は開始 POST 発行前に既に世代を進めている（購読確立を待たない）が、ここでも
  // 進める（二重に進めても実害は無い・resumeRunningTurn（history.js）等 send() を経由しない
  // 呼び出し元のための保険）。
  turnGen++;
  stopState = null; stopOnerrorFired = false; pendingStopThinking = null; pendingStopTraceTree = null; turnConcluded = false;   // 新規購読ごとに初期化（前ターンの状態を持ち越さない）
  S.es = new EventSource(`/chat/turns/${encodeURIComponent(S.turnId)}/stream?cursor=0`);
  S.es.onmessage = (ev) => {
    const e = JSON.parse(ev.data);
    if (e.type === 'trace_meta') { onTraceMeta(e); return; }   // EXT-4: 必ず最初の1件・v1/v2 の判定
    if (e.type === 'node') { if (S.liveTraceTree) S.liveTraceTree.addOrUpdate(e); else onNode(e); return; }
    if (e.type === 'question') { onQuestion(thinking, e); return; }
    if (e.type === 'stopped') { onStopped(thinking); return; }
    if (e.type === 'error') { onTurnFailed(thinking, e.message); return; }
    if (e.type === 'answer_delta') { ensureAnswerCard(thinking); reveal(e.text); return; }   // 最終回答を逐次描画
    if (e.type === 'answer') {
      turnConcluded = true;   // このターンは終端イベント（answer）で決着済み
      S.cid = e.conversation_id;
      // 積み上げ表示: このターンに trace があれば、ライブ中に描画したターン要素（S.liveTurnId）への
      // 遡及ボタンを回答カードに添える（後続ターンが始まって畳まれても再展開できるように）。
      const turnId = (e.message.trace && e.message.trace.length) ? S.liveTurnId : null;
      // v2 のときだけ終了理由（自然終了／上限到達／根拠不足）を明示して階層ツリーを畳む
      // （唯一の根拠＝ evidence_packet.stop_reason）。
      _stopThinkingTicker();
      _finalizeLiveTraceTree(e.message.answer.trace_version === 2
        ? deriveTraceStopReason(e.message.answer) : null);
      finalizeAnswer(thinking, e.message.answer, turnId, e.message.id, e.message.trace);
      updateScopeHeader(e.message.answer.scope);   // 実際に使われた範囲をヘッダに反映（推定含む）
      // Feature C: 個人コンテンツが含まれていたら共有不可状態にする。
      if (e.message.answer && (e.message.answer.personal_sources || e.message.answer.codex_wrote_files)) {
        S.convHasPersonal = true; updateShareButtonState();
      }
      setRt('完了', false); S.es.close(); S.es = null; S.turnId = null;
      setSendButtonStopping(false); $('messages').setAttribute('aria-busy', 'false'); loadConversations();
    }
  };
  S.es.onerror = () => {
    // 停止 POST が成功済み（'ok'）なら停止起因と確定。結果待ち（'pending'）の間に onerror が先に
    // 発火した場合は、停止起因かどうかまだ分からないため一旦「停止しました」と暫定表示し、
    // 結果確定後に stopStream 側で必要なら訂正する（同じ障害で停止 POST 自体も失敗した場合の保険）。
    // 停止要求が無い（null）／停止 POST が失敗済み（'failed'）は本物の接続断として扱う。
    const treatAsStopped = stopState === 'ok' || stopState === 'pending';
    const wasPending = stopState === 'pending';
    if (wasPending) { stopOnerrorFired = true; pendingStopThinking = thinking; } else { turnConcluded = true; }
    _stopThinkingTicker();   // ティックの残骸を片付ける
    // `wasPending` のときは楽観的な暫定表示——停止 POST の結果が後で失敗と判明したら
    // stopStream 側が `pendingStopTraceTree.correctStopReason` で訂正する。
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
// S7: エントリ（chat.js）の data-ask-submit ハンドラ・$('input') keydown からも呼ばれるため export。
// `override`（省略可・RV1 #3）: `{lens, layer, scope_paths}` のいずれかを指定すると、この1回の
// 送信だけブロックの現在設定（S.lens/S.layer/S.scope）の代わりにその値を使う（ブロック自体は
// 変えない＝スラッシュ接頭辞と同じ「1回限り」の扱い）。確認してから進めての確認カード
// （`chat_router.confirm_first_question` が payload に埋め込んだ解決済み値）の回答再送専用。
export async function send(override) {
  // 二重送信防止: `S.es`（ストリーミング中）に加え `S.sending`（開始 POST の応答待ち中）も見る。
  // `S.es` は購読確立（`subscribeTurn`）後にしか立たないため、開始 POST がまだ応答していない間は
  // これだけでは防げない——$('input') の Enter キー押下は `sendOrStop()`（S.es ガード込み）ではなく
  // `send()` を直接呼ぶため、素早い二重 Enter/クリックで開始 POST が2本飛び、1本目のターンが
  // 孤児化する（購読も停止もされない）事故になっていた。
  if (S.es || S.sending) return;
  const message = $('input').value.trim();
  if (!message) return;
  S.sending = true;
  S.turnStartedAtMs = Date.now();   // 新しい送信＝経過表示の起点を今に（再購読時は resumeRunningTurn がサーバ値で上書き）
  $('input').value = '';
  const w = $('messages').querySelector('.welcome-msg'); if (w) w.remove();   // 初期メッセージを消してから（重なり防止）
  appendUser(message);
  S.ansEl = null; S.ansHead = null; clearReveal();
  setSendButtonStopping(true);
  $('send').disabled = true;   // 開始 POST 応答待ち中は二重クリックを防ぐ（結果が来たら setSendButtonStopping が戻す）
  $('messages').setAttribute('aria-busy', 'true');
  const thinking = appendAssistantRaw('<div class="thinking loading-inline" role="status"><span class="spinner spinner-sm"></span><span>回答を準備しています...</span></div>');
  startThinkingTicker(thinking);
  startFlow(message);
  const body = { message, world: $('version').value, knowledge: !!S.kb, personal: !!S.personal };   // Feature B: 個人ファイル参照
  if (S.cid) body.conversation_id = S.cid;
  // WEB-1: true のときだけ載せる（既定は省略・表示条件を満たさない構成でもサーバ側で無効化される）。
  if (S.webSearch) body.web_search = true;
  // 範囲・調べ方・探す対象・調べる深さはナレッジ参照オンのときだけ送る（D・空/既定なら推定/自動/
  // 両方/標準＝送らない・調べ方ブロック §4.2 裁定3「既定は省略」）。`override` が指定されたキーは
  // この1回だけそちらを使う（RV1 #3・S.scope/S.lens/S.layer/S.depthProfile＝ブロックの継続設定は
  // 変えない）。
  if (S.kb) {
    const ov = override || {};
    const scopePaths = Object.prototype.hasOwnProperty.call(ov, 'scope_paths') ? ov.scope_paths : S.scope;
    const lens = Object.prototype.hasOwnProperty.call(ov, 'lens') ? ov.lens : S.lens;
    const layer = Object.prototype.hasOwnProperty.call(ov, 'layer') ? ov.layer : S.layer;
    const depthProfile = Object.prototype.hasOwnProperty.call(ov, 'depth_profile') ? ov.depth_profile : S.depthProfile;
    // SC-6e: 検索経路トグルも他の軸と同型（override優先・既定=全ONなら省略）。override（確認
    // カード再送等）は `S.toolsExplicit`（このブロックの操作履歴）とは無関係な既に解決済みの
    // 値のため、丸ごと明示扱い（全軸 true）にして省略せずそのまま渡す。
    const isOverride = Object.prototype.hasOwnProperty.call(ov, 'tools');
    const tools = isOverride ? ov.tools : S.tools;
    body.scope_paths = scopePaths || [];
    if (lens && lens !== 'auto') body.lens = lens;
    if (layer && layer !== 'both') body.layer = layer;
    if (depthProfile && depthProfile !== 'standard') body.depth_profile = depthProfile;
    // SC-6e: 未操作の既定 ON だけを省略する（明示 ON は不達でも省略しない・toolsForSend 参照）。
    // 省略の結果 body.tools が空になる（=全軸が未操作の既定値）場合は body.tools 自体を省く。
    if (tools) {
      const sendTools = toolsForSend(tools, isOverride ? { grep: true, fulltext: true, graph: true } : undefined);
      if (Object.keys(sendTools).length) body.tools = sendTools;
    }
  }
  // 開始 POST を投げる前に世代を進める（購読確立＝subscribeTurn 呼び出しまで待つと、直前の
  // ターンの停止結果がこの POST の待ち時間中に遅れて届いた場合にこのターンの表示を壊せてしまう）。
  // 自分の世代を捕捉し、await の後で世代が進んでいたら（会話切替・二重送信等で次の呼び出しが
  // 既に始まっている）純粋 no-op にする（stopStream の turnGen 契約と同型）——背景ターン自体は
  // 開始済みで走り続けるため、ここで何もしなくても resumeRunningTurn が後から拾える。
  turnGen++;
  const myGen = turnGen;
  let started;
  try {
    // timeoutMs 必須: 省略時（common.js::_sherpaApi）は無期限に待つため、サーバ/ネットワークが
    // 応答しない場合 `S.sending`（二重送信ガード）が解除されず永久に送信できなくなる
    // （admin-settings.js/settings.js の書込み系 POST と同じ 30 秒）。締切超過時は
    // `err.timeout=true`/`err.ambiguous=true` 付きで reject される——背景実行（HTTP 接続と
    // 無関係に完走する）そのものは変わらないため、締切超過後にサーバ側でターンが実際には
    // 開始済みだった場合の重複は resumeRunningTurn（GET /chat/turns/running）側の既存の
    // 再接続経路に委ねる。
    started = await Sherpa.api('POST', '/chat/turns', body, { timeoutMs: 30000 });
  } catch (e) {
    // 世代照合を S.sending の解除より先に行う: 世代不一致（会話遷移・後続の send() が既に
    // 開始済み）のときは S.sending を含む共有状態に一切触れない。触れてしまうと、後続の
    // 世代（例: 会話遷移後に始めた別ターン）がまだ応答待ちのまま S.sending が誤って解除され、
    // 3つ目の送信（Enter は disabled ボタンを経由しないため通ってしまう）がその後続の世代と
    // 衝突し、後続の世代のターンが孤児化する（購読されない）事故になる。
    if (turnGen !== myGen) return;
    S.sending = false;
    setRt('待機中', false); setSendButtonStopping(false); $('messages').setAttribute('aria-busy', 'false');
    if (thinking.isConnected) {
      const t = thinking.querySelector('.thinking');
      if (t) t.textContent = (e && e.message) || '送信に失敗しました。もう一度お試しください。';
    }
    return;
  }
  // 上と同じ理由で、世代照合を S.sending の解除より先に行う（unsubscribeTurn 側か、
  // 既に始まっている後続の send() だけが S.sending の一次責任者になる）。
  if (turnGen !== myGen) return;
  S.sending = false;
  $('send').disabled = false;   // 開始 POST が成功＝ここからは通常どおり「■ 停止」をクリック可能にする
  // SC-6b: 新規会話（S.cid が未確定だった）が初めて本物の conversation_id を得た瞬間、
  // それまで「new」バケットに記録していた調べ方ブロックの開閉状態をこの会話へ引き継ぐ。
  if (!S.cid) migrateInquiryOpenPref(started.conversation_id);
  S.cid = started.conversation_id;
  S.turnId = started.turn_id;
  subscribeTurn(thinking);
}
