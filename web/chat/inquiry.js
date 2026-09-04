// SC-6b/SC-6c（調べ方ブロック・docs/proposals/2026-08-29-調べ方ブロック.md）: 右ペイン下部の
// 固定フッター「🎯 次の質問の調べ方」（調べ方＝レンズの明示選択・調べる深さ＝探索の踏み込み度合い・
// 探す対象＝層）のドメイン。範囲（scope）・ナレッジ参照/個人ファイル参照トグルは既存どおり
// web/chat/scope.js が担当し続ける（本モジュールはその2行を含むブロック全体の開閉・chip・
// 調べ方/調べる深さ/探す対象セグメント・WEB-1 の Web 検索トグルのみを持つ）。
// export は chat.js（entry）側の #messages 委譲（出典0件時の再検索案内・SC-6d）、
// web/chat/stream.js（送信 body への lens/layer/depth_profile/web_search 反映・チップ更新）、
// web/chat/history.js（新規/会話復元時の初期状態）、
// web/chat/menus.js（WEB-1: admin 許可・接続先判定後の表示条件通知＝setWebSearchEligible）から参照される。
'use strict';

import { S } from './state.js';
import { scopeChipLabel } from './scope.js';
import { setRight } from '../chat.js';   // scope.js↔chat.js と同じ意図した循環 import（関数宣言＝hoisted）

const $ = Sherpa.$;

// 専門用語ゼロ（docs/04-画面の原則.md §6・「lens」「layer」を画面に出さない）: 確認カードの
// _CLARIFY_OPTIONS ラベルと一致させる（調べ方ブロック §2.2）。
const LENS_LABEL = { auto: '自動', impact: '影響', troubleshoot: '原因', qa: '内容', author: '作成' };
const LAYER_LABEL = { both: '資料＋コード', docs: '資料のみ', code: 'コードのみ' };
// 調べる深さ（調べ方ブロック §3.2・SC-6c）。
const DEPTH_LABEL = { standard: '標準', deep: '深く', max: '最大' };
// 検索経路トグル（調べ方ブロック §3.6・SC-6e）。キー順は要約ラベルの表示順にもなる。
const TOOL_KEYS = ['grep', 'fulltext', 'graph'];
const TOOL_LABEL = { grep: 'コマンド検索', fulltext: '全文・ベクトル', graph: 'グラフ' };
// このレンズでは層フィルタが実効しない（サーバ側 sherpa/layer.py の _LENS_NOT_APPLIED と同じ2つ・
// §3.5 裁定1）。ここでは「探す対象」セグメントをグレーアウトし理由を明示するためだけに使う
// （黙って無視しない・docs/04 §5「エラーは平文で理由＋次の一手」の作法を転用）。
const _LAYER_NOT_APPLIED = new Set(['impact', 'troubleshoot']);

// WEB-1: 「参照する」行の Web 検索トグルの表示条件（管理者許可 かつ 現在の頭脳が
// Codex＝OpenAI直結）。web/chat/menus.js が admin 許可・接続先を判定し次第 setWebSearchEligible()
// を呼ぶ（brainbadge の agent 切替と同じ「menus.js→この行の表示」配線・setKbLocked と同型）。
let _webSearchEligible = false;

// 検索経路の実接続可用性（SC-6e）。chat.js が起動時に `GET /chat/tools-availability`
// （実行側＝`agentic_search.tool_availability()` と同じ単一の真実源）を読んで
// setToolsAvailability() を呼ぶまでは楽観的に全ON扱い（既存挙動と同じ・不達なら送信後に
// サーバ側 422/graceful degrade で気づける多層防御は変わらない）。
let _toolsAvailability = { grep: true, fulltext: true, graph: true };
export function setToolsAvailability(avail) {
  _toolsAvailability = { grep: true, fulltext: true, graph: true, ...avail };
  renderInquiry();
}

// SC-6e: 送信 body 用の検索経路トグルを組み立てる。
// - 全軸が未操作（`explicit` が全て false）なら既定値のまま＝丸ごと省略する
//   （§4.2 裁定3「既定は省略」・以前からの契約）。
// - 1軸でも操作済みなら「今の完全な状態」を送る（他の軸だけ変えたつもりでも、未操作かつ
//   available な軸は現在値のまま含める——真偽値だけでは「既定のまま未操作」と「明示的に
//   その値にした」を区別できないため、単純な部分パッチにはしない）。
// - ただし未操作かつ不達の軸だけは例外で落とす——不達チップは隠れて触れられないため、その
//   軸の内部値（既定 true のまま）を他の軸の変更に巻き込んで送ると、意図せず「不達なのに
//   明示 ON」として 422 になってしまう。
// - 逆に操作済みの軸は不達でも必ず含める——「OFFにした検索を戻す」直後や OFF→ON 操作後に
//   接続が不達へ変わった場合でも、明示的な希望を黙って落とさず、422 で気づけるようにする
//   （不達判定そのものは常にサーバ側の実接続チェック・受付/実行で同一 snapshot に委ねる。
//   クライアント側の起動時1回きりの `_toolsAvailability` はチップ表示可否にのみ使う）。
// `explicit`（省略可・既定 `S.toolsExplicit`）／`availability`（省略可・既定 `_toolsAvailability`）:
// 確認カード再送等の override 経路（`stream.js::send`）は `S` の操作履歴と無関係な既に
// 解決済みの値のため、呼び出し元が全軸 true（＝丸ごと明示扱い）を渡す——既存の
// 「override はそのまま渡す」契約（`lens`/`layer`/`scope_paths` と同様）を tools でも保つ。
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

// チップ/折りたたみ見出しの要約文（調べ方・範囲・探す対象を同じ3つ組で統一・§2.3）。
// WEB-1: S.webSearch が ON の間は行の表示条件（_webSearchEligible）に関わらず常に付記する——
// 復元直後に行が非表示（頭脳切替・管理者OFF 等）でも ON のまま送信され得るため、チップで
// 見えなくならないようにする。ただし非 eligible（例: Codex(Ollama) へ切り替えた・管理者が
// 後から許可を外した）の間はサーバ側で必ず無効化されるため、ON のまま黙って有効に見せず
// 「現在の構成では利用不可」と明示する（docs/04 §5「エラーは平文で理由＋次の一手」の作法）。
// 全ONのときは何も付けない（既定＝現行挙動）。非既定のときだけ「使う検索: グラフのみ」のように付記する。
function _toolsSummary() {
  const on = TOOL_KEYS.filter((k) => S.tools[k]);
  if (on.length === TOOL_KEYS.length) return '';
  const label = on.length === 1 ? `${TOOL_LABEL[on[0]]}のみ` : on.map((k) => TOOL_LABEL[k]).join('・');
  return ` · 使う検索: ${label}`;
}

function _summary() {
  let ws = '';
  if (S.webSearch) ws = _webSearchEligible ? ' · Web検索' : ' · Web検索（現在の構成では利用不可）';
  return `${LENS_LABEL[S.lens] || '自動'} · ${scopeChipLabel()} · ${LAYER_LABEL[S.layer] || '資料＋コード'}`
    + ` · ${DEPTH_LABEL[S.depthProfile] || '標準'}${_toolsSummary()}${ws}`;
}

// 検索経路トグル（SC-6e）: 不達（`_toolsAvailability[key]===false`）のチップは表示しない
// （実効検索経路0を選べる状態を作らない）。「最後の1つ」判定は available ∩ requested で行う
// （3つとも False はサーバも 422・grep は常に available）。
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
  // RV1 #1: Sherpa.$ は getElementById（common.js:207）なので id はセレクタ記法（#付き）ではなく
  // 素の id 文字列で渡す。# 付きで渡すと $() が null を返し null.querySelectorAll() で例外停止する。
  _setSeg('lens-seg', 'lens', S.lens);
  _setSeg('layer-seg', 'layer', S.layer);
  _setSeg('depth-seg', 'depth', S.depthProfile);
  _renderToolsSeg();
  // 探す対象の行自体が非表示（ナレッジ参照オフ・scope.js の updateScopeVisibility が判定）のときは
  // 注記も出さない（無い行の理由を説明しても意味が無い）。
  const notApplied = _LAYER_NOT_APPLIED.has(S.lens) && !$('layer-row').hidden;
  $('layer-seg').querySelectorAll('.segbtn').forEach((b) => { b.disabled = notApplied; });
  $('layer-note').hidden = !notApplied;
  // WEB-1: 条件を満たさない（管理者未許可 or 頭脳が Codex でない）ときは行ごと非表示。
  const wsBtn = $('websearchtoggle');
  wsBtn.hidden = !_webSearchEligible;
  wsBtn.setAttribute('aria-pressed', S.webSearch ? 'true' : 'false');
  wsBtn.classList.toggle('on', S.webSearch);
  wsBtn.querySelector('b').textContent = S.webSearch ? 'オン' : 'オフ';
  const sum = _summary();
  $('inquiry-sum').textContent = sum;
  $('inquiry-chip-label').textContent = sum;
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

// 検索経路トグル（調べ方ブロック §3.6・SC-6e）: 残り1つは無視する（`_renderToolsSeg` が
// disabled にしているのでクリックは通常ここに届かないが、多層防御として関数側でも守る）。
export function setTool(key, on) {
  if (!TOOL_KEYS.includes(key)) return;
  const next = { ...S.tools, [key]: !!on };
  if (!TOOL_KEYS.some((k) => next[k])) return;   // 3つとも false は不可
  S.tools = next;
  S.toolsExplicit = { ...S.toolsExplicit, [key]: true };   // SC-6e: チップ操作＝明示（送信で省略しない）
  renderInquiry();
}

// SC-6d 連携（出典0件案内「OFF にした検索を戻す」）: 3つまとめて置き換える版。
export function setTools(tools) {
  const next = { grep: true, fulltext: true, graph: true, ...tools };
  if (!TOOL_KEYS.some((k) => next[k])) return;   // 3つとも false は不可（来ないはずだが多層防御）
  S.tools = next;
  // SC-6e: 「OFFにした検索を戻す」等・3軸まとめての明示操作＝全軸を明示扱いにする——さもないと
  // 「戻した」直後の送信で「既定 ON のまま未操作」と区別できず省略され、戻したはずの軸が不達でも
  // 422 にならず黙って OFF のまま実行されてしまう。
  S.toolsExplicit = { grep: true, fulltext: true, graph: true };
  renderInquiry();
}

// SC-6e: 会話ロード（`scope.js::applyConversationScope`）・pendingConvWorld 後追い復元
// （`chat.js`）の両方が使う——復元した検索経路トグルの明示状態を計算する。復元値が全ON
// （既定）なら全軸未操作（既定ONは次の送信で省略できる契約のまま）にする一方、1軸でも
// OFF なら復元した3軸すべてを明示状態にする。全軸を一律「未操作」のままにすると、無操作で
// 次の質問を送るだけで「既定ONは省略」の規則により復元した非既定値（例: grep OFF）が
// 送信 body から消え、黙って全ONへ戻ってしまう（不達のまま復元したONも同様に省略され
// 422にならない）。
export function toolsExplicitForRestore(tools) {
  const isDefault = TOOL_KEYS.every((k) => tools[k]);
  return { grep: !isDefault, fulltext: !isDefault, graph: !isDefault };
}

// WEB-1: web/chat/menus.js が admin 許可・接続先種別（openai/azure/custom）を判定した結果を通知する
// （brainbadge の agent 切替のたびに呼ばれる・setKbLocked と同型の「判定は他モジュール・表示はここ」
// の分担）。
export function setWebSearchEligible(on) {
  _webSearchEligible = !!on;
  renderInquiry();
}

function setWebSearch(on) {
  S.webSearch = !!on;
  renderInquiry();
}

// scope.js（範囲・kb/personal トグル）から呼ぶ: 選択できる範囲の再描画・kb トグル切替のたびに
// チップ/折りたたみ見出しの要約も更新する必要があるため、export して scope.js 側の変更点に足す。
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
  if ($('websearchtoggle').hidden) return;   // 非表示中は無効（念のための多層防御）
  setWebSearch(!S.webSearch);
});
$('tools-seg').addEventListener('click', (e) => {
  const b = e.target.closest('[data-tool]'); if (!b || b.disabled) return;
  setTool(b.dataset.tool, !S.tools[b.dataset.tool]);
});

// ===== 「詳細」折りたたみ（既定閉・SC-6e）: 会話ごとの永続はしない（毎回既定閉から）=====
export function setToolsDetailsOpen(open) {
  $('tools-details-body').hidden = !open;
  $('tools-details-head').setAttribute('aria-expanded', open ? 'true' : 'false');
}
$('tools-details-head').addEventListener('click', () => {
  setToolsDetailsOpen($('tools-details-body').hidden);
});

// ===== 開閉（会話ごとに localStorage・空の会話は既定オープン・§8 裁定12）=====
function _openKey(cid) { return `sherpa-inquiry-open:${cid == null ? 'new' : cid}`; }
function _loadOpenPref(cid) {
  try { const v = localStorage.getItem(_openKey(cid)); return v === null ? null : v === '1'; }
  catch (e) { return null; }
}
function _saveOpenPref(cid, open) {
  try { localStorage.setItem(_openKey(cid), open ? '1' : '0'); } catch (e) { /* no-op */ }
}
export function setInquiryOpen(open) {
  $('inquiry-body').hidden = !open;
  $('inquiry-head').setAttribute('aria-expanded', open ? 'true' : 'false');
  _saveOpenPref(S.cid, open);
}
// 会話が空（メッセージ0件）なら開く（右ペイン自体も開く・RV1 #5・§8 裁定12）。既存の会話は
// 前回の開閉状態を保つ（記録が無い＝初回訪問は開く）。
export function applyInquiryOpenDefault(messagesEmpty) {
  if (messagesEmpty) { setRight(true); setInquiryOpen(true); return; }
  const pref = _loadOpenPref(S.cid);
  setInquiryOpen(pref === null ? true : pref);
}
// 新規会話（cid 未確定）の間に選んだ開閉状態を、送信後に確定した本物の会話IDへ引き継ぐ
// （stream.js の send() が conversation_id を得た直後に呼ぶ）。
export function migrateInquiryOpenPref(cid) {
  const pref = _loadOpenPref(null);
  if (pref !== null) _saveOpenPref(cid, pref);
}
$('inquiry-head').addEventListener('click', () => {
  setInquiryOpen($('inquiry-body').hidden);   // 現在 hidden なら開く・開いていれば畳む
});

// ===== 入力欄の要約チップ（§2.3・§2.4: 右ペインが閉じている/狭幅でも必ずブロックへ到達できる）=====
$('inquiry-chip').addEventListener('click', () => {
  // RV1 #5: 狭幅で明示的にチップから開いた場合は、既存の3カラム grid 計算で右ペインを左ペインより
  // 優先する（さもないとレイアウト計算が「中央最小幅を確保できない」と判断して右トラックを
  // 即座に 0 へ戻し、開いたはずのブロックが見えないまま終わる）。
  setRight(true, { preferRight: true });
  setInquiryOpen(true);
  $('inquiry').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
});

// ===== 新規会話（web/chat/history.js から呼ぶ・render/scope と同じオーケストレーション対象）=====
// 調べ方（lens）・探す対象（layer）の会話復元は scope.js の applyConversationScope が範囲と同じ
// pendingConvWorld 後追い経路へ統合済み（RV1 #4）——history.js は会話ロード時にそちらと
// applyInquiryOpenDefault（開閉状態のみ）を呼ぶ。
export function resetInquiryForNewConversation() {
  // WEB-1: 新規会話は常にオフ。SC-6c: 新規会話は常に標準（依頼の設計どおり）。
  // SC-6e: 新規会話は常に全ON（依頼の設計どおり）・詳細折りたたみも既定閉に戻す。
  S.lens = 'auto'; S.layer = 'both'; S.depthProfile = 'standard'; S.webSearch = false;
  S.tools = { grep: true, fulltext: true, graph: true };
  S.toolsExplicit = { grep: false, fulltext: false, graph: false };   // SC-6e: 新規会話は未操作の既定 ON へ戻す
  setToolsDetailsOpen(false);
  applyInquiryOpenDefault(true);
  renderInquiry();
}

renderInquiry();   // 初期表示（自動・全体・資料＋コード・標準・検索経路は全ON）
