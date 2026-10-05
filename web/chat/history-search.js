// チャット履歴の検索。タイトルは即時（NFKC+lower 正規化）、本文は入力停止後に GET /conversations?q= で絞り込む。
// 設計: docs/design/chat.md「会話の保存と継続」
// history.js の行 HTML は変えず、hidden 切替と抜粋 span の挿入だけを行う。再描画は MutationObserver で検知して再適用する。
'use strict';

const $ = Sherpa.$, getJSON = Sherpa.getJSON;

const _input = $('hist-search');
const _clearBtn = $('hist-search-clear');
const _noHit = $('hist-nohit');
const _searchError = $('hist-search-error');
const _list = $('convlist');

let _query = '';                // 正規化済みの現在の検索語（空 = 未検索）
let _messageHits = new Map();   // 会話 id(number) -> 本文一致の抜粋
let _fetchTimer = null;
let _fetchSeq = 0;              // 入力/クリアのたびに進める世代カウンタ（古い応答の破棄用）

function _normalize(s) {
  // NFKC＋lower に加え、カタカナをひらがなへ畳み込む（かな/カナを区別しない）。
  return (s || '').normalize('NFKC').toLowerCase()
    .replace(/[ァ-ヶ]/g, (c) => String.fromCharCode(c.charCodeAt(0) - 0x60));
}

function _clearSnippet(row) {
  const el = row.querySelector('.hist-snippet');
  if (el) el.remove();
}

// 抜粋は行内の `.d` に挿入する（無ければ行末に作る）。
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
  // 本文検索の失敗表示中は「見つかりません」を出さない。
  _noHit.hidden = !_searchError.hidden || anyVisible;
}

function _scheduleFetch(rawQuery) {
  clearTimeout(_fetchTimer);
  // 空でも世代は進める（応答待ちの旧 fetch の結果を捨てる）。
  const seq = ++_fetchSeq;
  if (!rawQuery) return;                // 空はサーバへ問い合わせない
  _fetchTimer = setTimeout(async () => {
    let rows;
    try {
      rows = await getJSON('/conversations?q=' + encodeURIComponent(rawQuery));
    } catch (e) {
      // 本文一致は未確認＝「見つかりません」を出さず、本文検索の失敗を別途表示する。
      if (seq === _fetchSeq) { _searchError.hidden = false; _noHit.hidden = true; }
      return;
    }
    if (seq !== _fetchSeq) return;      // 古い入力への応答は破棄
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
  _messageHits = new Map();
  _clearBtn.hidden = !_input.value;
  _searchError.hidden = true;
  _applyFilter();
  _scheduleFetch(trimmed);
}

function _reset() {
  _input.value = '';
  _query = ''; _messageHits = new Map();
  _clearBtn.hidden = true;
  _searchError.hidden = true;
  clearTimeout(_fetchTimer);
  _fetchSeq++;
  _applyFilter();
}

_input.addEventListener('input', _onInput);
_input.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && _input.value) { e.stopPropagation(); _reset(); }
});
_clearBtn.addEventListener('click', () => { _reset(); _input.focus(); });

// loadConversations() が #convlist を差し替えたら、直近の検索語で絞り込み直す。
new MutationObserver(_applyFilter).observe(_list, { childList: true });
