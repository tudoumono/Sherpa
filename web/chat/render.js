// チャットの描画の入口。welcome を持ち、回答・trace・出典の各 render-*.js の名前をここから再公開する。
// 設計: docs/design/chat.md「1ターンの流れ」
'use strict';

import { EXAMPLES } from './state.js';
import { appendAssistantRaw } from './render-answer.js';

const $ = Sherpa.$, esc = Sherpa.esc, fmtDateTime = Sherpa.fmtDateTime, mdLite = Sherpa.mdLite, analyzerLabel = Sherpa.analyzerLabel;

// 質問例チップのブロック HTML。EXAMPLES が空なら空文字（ブロックごと出さない）。
function _examplesHtml() {
  if (!EXAMPLES.length) return '';
  return '<div class="examples">' + EXAMPLES.map((t, i) =>
    `<button class="example" data-ex="${i}"><span class="exq">${esc(t)}</span><span class="exarrow">✎</span></button>`).join('') + '</div>';
}

// 初期画面（ようこそ・使い方3ステップ・質問例）を描く。
export function welcome() {
  $('messages').innerHTML = '';
  // 固定文言のみ（利用者入力を含まない）
  const steps =
    '<div class="headline">ようこそ Slouch へ</div>'
    + '<ol class="welcome-steps">'
    + '<li><b>資料フォルダを登録</b><br><span class="muted">管理画面で、調べたい社内資料フォルダを登録します。</span></li>'
    + '<li><b>チャットで質問</b><br><span class="muted">例:「消費税率を変えると何に影響する？」／「夜間バッチの異常終了の原因候補は？」</span></li>'
    + '<li><b>出典から原本を確認</b><br><span class="muted">回答末尾の出典リンクから、根拠になった元ファイルを開けます。</span></li>'
    + '</ol>';
  const el = appendAssistantRaw(steps
    + '<div class="muted">気になることを、いつもの言葉で質問してください。'
    + '社内資料に基づく回答が必要なときは、<b>「社内資料」をオン</b>に。</div>' + _examplesHtml());
  el.classList.add('welcome-msg');   // 送信時に消すための目印
}

// 管理者設定の質問例が welcome() より後に届いたとき、表示中の welcome の質問例を置き換える（表示中でなければ何もしない）。
export function refreshWelcomeExamples() {
  const msg = document.querySelector('.welcome-msg');
  if (!msg) return;
  const html = _examplesHtml();
  const old = msg.querySelector('.examples');
  if (old) { if (html) old.outerHTML = html; else old.remove(); }
  else if (html) { msg.insertAdjacentHTML('beforeend', html); }
}


export * from './render-trace.js';
export * from './render-answer.js';
export * from './render-sources.js';
