// 右ペイン下部「次の質問の調べ方」ブロック。調べ方・調べる深さ・探す対象・検索経路・Web 検索の選択と、要約チップ・開閉を扱う。
// 設計: docs/design/chat.md「文脈と構成」
// 範囲・ナレッジ参照・個人ファイル参照トグルは scope.js が担当する。
'use strict';

import { S } from './state.js';
import { scopeChipLabel } from './scope.js';
import { setRight } from '../chat.js';

const $ = Sherpa.$;

// 画面に出す表示名（確認カードの選択肢ラベルと一致させる）
const LENS_LABEL = { auto: '自動', impact: '影響', troubleshoot: '原因', qa: '内容', author: '作成' };
const LAYER_LABEL = { both: '資料＋コード', docs: '資料のみ', code: 'コードのみ' };
const DEPTH_LABEL = { quick: 'クイック', standard: '標準', deep: '深く', max: '最大' };
// 検索経路トグル。キー順は要約ラベルの表示順にもなる。
const TOOL_KEYS = ['grep', 'fulltext', 'graph'];
const TOOL_LABEL = { grep: '語句そのまま検索', fulltext: '意味・表記ゆれも探す', graph: 'グラフ' };
// 探す対象（層）が効かない調べ方（サーバ側 sherpa/layer.py の _LENS_NOT_APPLIED と同じ）。セグメントを無効化して注記を出す。
const _LAYER_NOT_APPLIED = new Set(['impact', 'troubleshoot']);

// Web 検索トグルの表示条件（管理者許可 かつ 現在の頭脳が Codex＝OpenAI直結）。menus.js が setWebSearchEligible() で通知する。
let _webSearchEligible = false;

// 簡易（agent=simple）では調べ方・深さ・検索経路・Web 検索は効かないため隠す。menus.js が setSimpleMode() で通知する。
let _simpleMode = false;
export function isSimpleMode() { return _simpleMode; }

// 検索経路の実接続可用性。chat.js が `GET /chat/tools-availability` を読んで setToolsAvailability() を呼ぶまでは全ON扱い。
let _toolsAvailability = { grep: true, fulltext: true, graph: true };
export function setToolsAvailability(avail) {
  _toolsAvailability = { grep: true, fulltext: true, graph: true, ...avail };
  renderInquiry();
}

// 送信 body 用の検索経路トグルを組み立てる。
// - 全軸が未操作なら既定値のまま＝丸ごと省略する。
// - 1軸でも操作済みなら「今の完全な状態」を送る。ただし未操作かつ不達の軸は落とす（不達チップは触れないため）。
// - 操作済みの軸は不達でも含める（不達判定はサーバ側の実接続チェックに委ねる）。
// explicit・availability の既定は S.toolsExplicit・_toolsAvailability。確認カード再送等の override 経路は全軸 true を渡す。
export function toolsForSend(tools, explicit = S.toolsExplicit, availability = _toolsAvailability) {
  if (!TOOL_KEYS.some((k) => explicit[k])) return {};   // 全軸未操作＝完全な既定値のまま
  const out = {};
  for (const k of TOOL_KEYS) {
    if (!explicit[k] && !availability[k]) continue;   // 未操作かつ不達の軸だけ落とす
    out[k] = !!tools[k];
  }
  return out;
}

function _setSeg(sel, dataAttr, value) {
  $(sel).querySelectorAll('.segbtn').forEach((b) => b.classList.toggle('on', b.dataset[dataAttr] === value));
}

// チップ/折りたたみ見出しの要約文（調べ方・範囲・探す対象・深さ）。
// 検索経路は全ONのとき付けず、非既定のときだけ「使う検索: グラフのみ」のように付記する。
// Web 検索は S.webSearch が ON の間は常に付記し、非 eligible のときは「現在の構成では利用不可」と明示する。
function _toolsSummary() {
  const on = TOOL_KEYS.filter((k) => S.tools[k]);
  if (on.length === TOOL_KEYS.length) return '';
  const label = on.length === 1 ? `${TOOL_LABEL[on[0]]}のみ` : on.map((k) => TOOL_LABEL[k]).join('・');
  return ` · 使う検索: ${label}`;
}

function _summary() {
  if (_simpleMode) return `簡易 · ${scopeChipLabel()} · ${LAYER_LABEL[S.layer] || '資料＋コード'}`;
  let ws = '';
  if (S.webSearch && S.kb) ws = _webSearchEligible ? ' · Web検索' : ' · Web検索（現在の構成では利用不可）';
  return `${LENS_LABEL[S.lens] || '自動'} · ${scopeChipLabel()} · ${LAYER_LABEL[S.layer] || '資料＋コード'}`
    + ` · ${DEPTH_LABEL[S.depthProfile] || '標準'}${_toolsSummary()}${ws}`;
}

// 検索経路チップを描く。不達のチップは隠し、利用可能で ON の最後の1つは OFF にできない。
function _renderToolsSeg() {
  const onAndAvailable = TOOL_KEYS.filter((k) => _toolsAvailability[k] && S.tools[k]);
  $('tools-seg').querySelectorAll('[data-tool]').forEach((b) => {
    const key = b.dataset.tool;
    const available = !!_toolsAvailability[key];
    b.hidden = !available;
    if (!available) { b.disabled = true; b.classList.remove('on'); b.title = ''; return; }
    const on = !!S.tools[key];
    b.classList.toggle('on', on);
    const isLastOn = on && onAndAvailable.length <= 1;
    b.disabled = isLastOn;
    b.title = isLastOn ? '最後の1つはOFFにできません（検索経路が0個になってしまいます）' : '';
  });
}

function renderInquiry() {
  // Sherpa.$ は getElementById なので id は # 無しで渡す
  _setSeg('lens-seg', 'lens', S.lens);
  _setSeg('layer-seg', 'layer', S.layer);
  _setSeg('depth-seg', 'depth', S.depthProfile);
  _renderToolsSeg();
  // 探す対象の行が非表示（ナレッジ参照オフ）のときは注記も出さない
  const notApplied = _LAYER_NOT_APPLIED.has(S.lens) && !$('layer-row').hidden;
  $('layer-seg').querySelectorAll('.segbtn').forEach((b) => { b.disabled = notApplied; });
  $('layer-note').hidden = !notApplied;
  // 表示条件を満たさないときは行ごと非表示
  const wsBtn = $('websearchtoggle');
  wsBtn.hidden = !_webSearchEligible;
  wsBtn.setAttribute('aria-pressed', S.webSearch ? 'true' : 'false');
  wsBtn.classList.toggle('on', S.webSearch);
  wsBtn.querySelector('b').textContent = S.webSearch ? 'オン' : 'オフ';
  // 簡易: 効かない行を隠し注記を出す
  $('lens-row').hidden = _simpleMode;
  $('depth-row').hidden = _simpleMode;
  $('tools-details').hidden = _simpleMode;
  $('simple-note').hidden = !_simpleMode;
  if (_simpleMode || !S.kb) wsBtn.hidden = true;   // 資料参照オフでは Web 検索も使わない
  const sum = _summary();
  $('inquiry-sum').textContent = sum;
  $('inquiry-chip-label').textContent = sum;
}

export function setSimpleMode(on) {
  _simpleMode = !!on;
  renderInquiry();
}

export function setLens(lens) {
  S.lens = lens;
  renderInquiry();
}

export function setLayer(layer) {
  S.layer = layer;
  renderInquiry();
}

export function setDepthProfile(depthProfile) {
  S.depthProfile = depthProfile;
  renderInquiry();
}

// 検索経路を1つ切り替える。ON が残り1つになる操作は無視する。
export function setTool(key, on) {
  if (!TOOL_KEYS.includes(key)) return;
  const next = { ...S.tools, [key]: !!on };
  if (!TOOL_KEYS.some((k) => next[k])) return;   // 3つとも false は不可
  S.tools = next;
  S.toolsExplicit = { ...S.toolsExplicit, [key]: true };   // 操作した軸は送信で省略しない
  renderInquiry();
}

// 検索経路を3つまとめて置き換える（出典0件案内の「OFF にした検索を戻す」用）。
export function setTools(tools) {
  const next = { grep: true, fulltext: true, graph: true, ...tools };
  if (!TOOL_KEYS.some((k) => next[k])) return;   // 3つとも false は不可
  S.tools = next;
  // 3軸まとめての操作＝全軸を明示扱いにする
  S.toolsExplicit = { grep: true, fulltext: true, graph: true };
  renderInquiry();
}

// 送信に添える「利用者が実際に切り替えた軸」。サーバは会話メタへ保存するだけで、会話を開き直したときの復元にだけ使う。
export function toolsExplicitForSend(explicit = S.toolsExplicit) {
  return TOOL_KEYS.filter((k) => explicit[k]);
}

// 会話復元時の検索経路の明示状態を求める（scope.js の applyConversationScope と chat.js の後追い復元が使う）。
// 保存された明示状態 saved があればそれを使い、無い回答は「全ONなら全軸未操作・1軸でもOFFなら全軸明示」とする。
export function toolsExplicitForRestore(tools, saved) {
  if (Array.isArray(saved)) {
    return { grep: saved.includes('grep'), fulltext: saved.includes('fulltext'), graph: saved.includes('graph') };
  }
  const isDefault = TOOL_KEYS.every((k) => tools[k]);
  return { grep: !isDefault, fulltext: !isDefault, graph: !isDefault };
}

// menus.js が判定した Web 検索の表示条件（管理者許可・接続先種別）を受け取る。
export function setWebSearchEligible(on) {
  _webSearchEligible = !!on;
  renderInquiry();
}

function setWebSearch(on) {
  S.webSearch = !!on;
  renderInquiry();
}

// scope.js が範囲・kb トグルの変更のたびに要約を更新するために使う。
export { renderInquiry as refreshInquirySummary };

$('lens-seg').addEventListener('click', (e) => {
  const b = e.target.closest('[data-lens]'); if (!b) return;
  setLens(b.dataset.lens);
});
$('layer-seg').addEventListener('click', (e) => {
  const b = e.target.closest('[data-layer]'); if (!b || b.disabled) return;
  setLayer(b.dataset.layer);
});
$('depth-seg').addEventListener('click', (e) => {
  const b = e.target.closest('[data-depth]'); if (!b) return;
  setDepthProfile(b.dataset.depth);
});
$('websearchtoggle').addEventListener('click', () => {
  if ($('websearchtoggle').hidden) return;
  setWebSearch(!S.webSearch);
});
$('tools-seg').addEventListener('click', (e) => {
  const b = e.target.closest('[data-tool]'); if (!b || b.disabled) return;
  setTool(b.dataset.tool, !S.tools[b.dataset.tool]);
});

// ===== 「詳細」折りたたみ（既定閉・会話ごとの永続はしない）=====
export function setToolsDetailsOpen(open) {
  $('tools-details-body').hidden = !open;
  $('tools-details-head').setAttribute('aria-expanded', open ? 'true' : 'false');
}
$('tools-details-head').addEventListener('click', () => {
  setToolsDetailsOpen($('tools-details-body').hidden);
});

// ===== 開閉（常に既定オープン・送信で畳む・ヘッダ/チップで再び開ける）=====
export function setInquiryOpen(open) {
  $('inquiry-body').hidden = !open;
  $('inquiry-head').setAttribute('aria-expanded', open ? 'true' : 'false');
}
// 会話が空なら右ペインも開く。ブロックは常に開いた状態にする（畳むのはメッセージ送信時のみ）。
export function applyInquiryOpenDefault(messagesEmpty) {
  if (messagesEmpty) setRight(true);
  setInquiryOpen(true);
}
$('inquiry-head').addEventListener('click', () => {
  setInquiryOpen($('inquiry-body').hidden);
});

// ===== 入力欄の要約チップ（右ペインが閉じている/狭幅でもブロックへ到達できる）=====
$('inquiry-chip').addEventListener('click', () => {
  // 狭幅でも右ペインを左ペインより優先して開く
  setRight(true, { preferRight: true });
  setInquiryOpen(true);
  $('inquiry').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
});

// ===== 新規会話（history.js から呼ぶ。会話復元時の調べ方・探す対象は scope.js の applyConversationScope が担う）=====
export function resetInquiryForNewConversation() {
  // 新規会話は自動・両方・標準・Web検索オフ・検索経路は全ON・詳細は閉じる
  S.lens = 'auto'; S.layer = 'both'; S.depthProfile = 'standard'; S.webSearch = false;
  S.tools = { grep: true, fulltext: true, graph: true };
  S.toolsExplicit = { grep: false, fulltext: false, graph: false };
  setToolsDetailsOpen(false);
  applyInquiryOpenDefault(true);
  renderInquiry();
}

renderInquiry();
