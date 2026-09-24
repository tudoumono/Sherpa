// H チャット履歴の検索（docs/proposals/2026-09-07-履歴検索と下調べ並列化.md §1）。
// タイトルの部分一致は即時（クライアント側・NFKC+lower 正規化＝大文字小文字・全角半角・かなカナを
// 区別しない）で行を絞り、本文一致は入力停止 300ms 後に GET /conversations?q= を呼んで抜粋つきで
// 行に足す（タイトル不一致でも本文一致なら表示）。history.js の行 HTML（`[data-open]`・
// `.cmain[title]`・`.d`）は変更せず、hidden 切替と抜粋 span の挿入だけを上乗せする。history.js の
// 再描画（loadConversations の #convlist innerHTML 差し替え）はこのモジュールから見えないため、
// MutationObserver で検知して直近の検索語を再適用する（history.js には一切手を入れない）。
'use strict';

const $ = Sherpa.$, getJSON = Sherpa.getJSON;

const _input = $('hist-search');
const _clearBtn = $('hist-search-clear');
const _noHit = $('hist-nohit');
const _searchError = $('hist-search-error');
const _list = $('convlist');

let _query = '';                // 正規化済み（NFKC+lower）の現在の検索語（空 = 未検索）
let _messageHits = new Map();   // 会話 id(number) -> 抜粋（本文一致・GET /conversations?q= 応答由来）
let _fetchTimer = null;
let _fetchSeq = 0;              // 入力/クリアのたびに進める世代カウンタ（古い fetch 応答の破棄用）

function _normalize(s) {
  // NFKC＋lower に加え、カタカナ（U+30A1〜U+30F6）をひらがな（同じ並び順で -0x60）へ畳み込み、
  // 「バッチ」で検索しても「ばっち」の行を拾える（仕様: かな/カナを区別しない）。
  return (s || '').normalize('NFKC').toLowerCase()
    .replace(/[ァ-ヶ]/g, (c) => String.fromCharCode(c.charCodeAt(0) - 0x60));
}

function _clearSnippet(row) {
  const el = row.querySelector('.hist-snippet');
  if (el) el.remove();
}

// 抜粋の挿入先は行内の `.d`（無ければ行末＝仕様どおり。現行の行構造には常に .d がある保険）。
function _applySnippet(row, snippet) {
  let host = row.querySelector('.d');
  if (!host) { host = document.createElement('span'); host.className = 'd'; row.appendChild(host); }
  let el = host.querySelector('.hist-snippet');
  if (!el) { el = document.createElement('span'); el.className = 'hist-snippet'; host.appendChild(el); }
  el.textContent = `…${snippet}…`;
}

function _applyFilter() {
  const rows = [..._list.querySelectorAll('.conv[data-open]')];
  if (!_query) {
    rows.forEach((row) => { row.hidden = false; _clearSnippet(row); });
    _noHit.hidden = true;
    return;
  }
  let anyVisible = false;
  rows.forEach((row) => {
    const cmain = row.querySelector('.cmain');
    const titleMatch = !!cmain && _normalize(cmain.title).includes(_query);
    const snippet = _messageHits.get(Number(row.dataset.open));
    if (titleMatch) {
      row.hidden = false; _clearSnippet(row); anyVisible = true;
    } else if (snippet) {
      row.hidden = false; _applySnippet(row, snippet); anyVisible = true;
    } else {
      row.hidden = true; _clearSnippet(row);
    }
  });
  // 本文検索の失敗表示中は「見つかりません」を排他で出さない（未確認なだけで実際は一致するかもしれない）。
  _noHit.hidden = !_searchError.hidden || anyVisible;
}

function _scheduleFetch(rawQuery) {
  clearTimeout(_fetchTimer);
  // 空でも世代は進める＝進行中（応答待ち）の旧 fetch がこの後で失敗/成功しても seq 不一致で無視される
  // （空文字だから新規に schedule しないだけで、既に投げた問い合わせの結果まで有効にはしない）。
  const seq = ++_fetchSeq;
  if (!rawQuery) return;                // 空はサーバへ問い合わせない（422 対象・タイトル絞り込みのみで足りる）
  _fetchTimer = setTimeout(async () => {
    let rows;
    try {
      rows = await getJSON('/conversations?q=' + encodeURIComponent(rawQuery));
    } catch (e) {
      // 通信/検証/サーバエラーでは本文一致の有無が確認できていない。「見つかりません」を確定
      // 表示すると実際は一致する会話があっても隠れたままになるため、タイトル一致だけの絞り込み
      // 結果を示しつつ、本文検索が失敗したことを別途伝える（古い入力への遅延失敗は無視）。
      if (seq === _fetchSeq) { _searchError.hidden = false; _noHit.hidden = true; }
      return;
    }
    if (seq !== _fetchSeq) return;      // 古い入力に対する遅延応答は破棄（新しい入力/クリアが既に進めた）
    _searchError.hidden = true;
    _messageHits = new Map();
    (rows || []).forEach((r) => {
      if (r && r.match && r.match.where === 'message') _messageHits.set(Number(r.id), r.match.snippet);
    });
    _applyFilter();
  }, 300);
}

function _onInput() {
  const trimmed = _input.value.trim();
  _query = _normalize(trimmed);
  _messageHits = new Map();             // 新しい検索語＝前回の本文一致抜粋は無効
  _clearBtn.hidden = !_input.value;
  _searchError.hidden = true;           // 新しい入力＝前回の失敗表示は無効
  _applyFilter();
  _scheduleFetch(trimmed);
}

function _reset() {
  _input.value = '';
  _query = ''; _messageHits = new Map();
  _clearBtn.hidden = true;
  _searchError.hidden = true;
  clearTimeout(_fetchTimer);
  _fetchSeq++;                          // 遅延中の fetch があれば応答到着時に破棄させる
  _applyFilter();
}

_input.addEventListener('input', _onInput);
_input.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && _input.value) { e.stopPropagation(); _reset(); }
});
_clearBtn.addEventListener('click', () => { _reset(); _input.focus(); });

// history.js::loadConversations() は #convlist を innerHTML ごと差し替える（新規会話・pin/削除/
// rename 後の再描画）。差し替え後も直近の検索語で絞り込みを保つ。
new MutationObserver(_applyFilter).observe(_list, { childList: true });
