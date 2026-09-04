// フェーズ6 S8（リファクタリング計画・最終スライス）: brain-menu（#1 AI/実行環境クイック切替）・
// フォントサイズ（#5）・エクスポート（#6）・テーマ切替のドメインを chat.js から純移動。
// PROVIDERS/_agent/_models（brain-menu 内で完結・単一ドメイン）もここへ併せて移動。
// export は chat.js（entry）側に残る呼び出し元（init の applyCachedBrain()/loadConfig() 呼び出し・
// $('messages') の data-export 委譲リスナーが呼ぶ exportMessages）から参照される
// applyCachedBrain/loadConfig/exportMessages のみに絞る（setBrainBadge/renderBrainMenu/saveModel/
// testModel/setAgent/applyThemeIcon・_stamp/_exportName/_scopeText/_answerLines/_buildText/_download/
// exportChat/renderExportMenu は本ファイル内で完結する単一ドメインのため非公開のまま）。
// toast() はどのドメインにも属さない横断ユーティリティのため chat.js に残る
// （chat.js↔menus.js の意図した循環 import・関数宣言＝hoisted のため実行時に呼ぶ限り ESM で安全＝
// render.js の setRt・history.js の copyText 等これまでのスライスと同じパターン）。
// WEB-1: web/chat/inquiry.js への `setWebSearchEligible` 一方向 import を追加
// （inquiry.js/scope.js は本モジュールを import しない＝新たな循環は増やさない）。
'use strict';

import { S, setChatExamples } from './state.js';
import { setKbLocked } from './scope.js';   // Codex構成は資料参照ON固定（決定 2026-08-15）
import { setWebSearchEligible } from './inquiry.js';   // WEB-1: 表示条件（admin許可×Codex×OpenAI直結）の通知
import { refreshWelcomeExamples } from './render.js';   // 管理者設定 chat_examples の後追い反映
import { toast } from '../chat.js';

const $ = Sherpa.$, esc = Sherpa.esc, getJSON = Sherpa.getJSON;   // 共通ユーティリティ(common.js)

let _agent = null;                      // 現在のAI/実行環境（#1・brain-menu 内で完結・単一ドメイン）
let _models = {};                       // プロバイダ別モデル（brain-menu 内で完結・単一ドメイン）
// 利用できるAI・実行環境（将来ここに追記すれば選択肢が増える・データ駆動）
// 実行構成の一覧はサーバ（GET /settings の constructs_available・`sherpa/agent_constructs.py`）から
// 受け取る＝この環境で使えるものだけを出す。バッジを開いたときに取得し、失敗時は前回値のまま。
let PROVIDERS = [];
let _constructId = null;         // 現在の構成 id（codex_openai / codex_ollama を区別するため agent とは別に持つ）
// WEB-1: GET /settings の web_search_available（管理者許可）・openai_endpoint_kind（接続先種別）。
// 調べ方ブロックの Web 検索行は「管理者許可 かつ 頭脳が Codex（OpenAI 直結＝construct codex_openai）
// かつ 接続先が OpenAI 直結」の時だけ出す。`_agent === 'codex'` だけでは codex_ollama も含んでしまう
// （Codex(Ollama) は OpenAI の web_search（ホスト型検索）に接続できない・サーバ側 sandbox.py も
// endpoint_kind="ollama" 扱いで常に無効化する）ため、construct_id で厳密に絞る。
let _webSearchAvailable = false;
let _openaiEndpointKind = 'openai';

// `_constructId`/`_webSearchAvailable`/`_openaiEndpointKind` のいずれかが変わるたびに呼ぶ
// （setBrainBadge・GET /settings 取得の両方から）。inquiry.js 側は行の表示/非表示だけを担当する。
function _syncWebSearchEligibility() {
  setWebSearchEligible(_webSearchAvailable && _constructId === 'codex_openai' && _openaiEndpointKind === 'openai');
}

// テーマ切替
function applyThemeIcon() { $('themebtn').textContent = document.documentElement.dataset.theme === 'dark' ? '☀️' : '🌙'; }
$('themebtn').addEventListener('click', () => {
  const d = document.documentElement, next = d.dataset.theme === 'dark' ? 'light' : 'dark';
  d.dataset.theme = next; localStorage.setItem('sherpa-theme', next); applyThemeIcon();
});
applyThemeIcon();

// 頭脳バッジ＋設定
function setBrainBadge(c) {
  if (!c) return;
  _agent = c.agent || _agent;
  setKbLocked(_agent === 'codex');       // Codex は常に資料を参照する（トグルON固定）
  _syncWebSearchEligibility();           // WEB-1: agent 切替のたびに表示条件を再評価
  $('brain-label').textContent = c.label || '…';
  $('brain-model').textContent = (c.model && c.model !== '—') ? '· ' + c.model : '';
}
// S8: chat.js entry の init（リロード直後・サーバ応答前に前回の選択を即表示）から呼ばれるため export。
export function applyCachedBrain() {   // リロード直後、サーバ応答前に前回の選択を即表示（記録の保持）
  try { setBrainBadge(JSON.parse(localStorage.getItem('sherpa-brain') || 'null')); } catch (e) { }
}
// S8: chat.js entry の init から呼ばれるため export。
export async function loadConfig() {
  try {
    const c = await getJSON('/config');
    setBrainBadge(c);
    try { localStorage.setItem('sherpa-brain', JSON.stringify(c)); } catch (e) { }   // 次回アクセス/リロードで即復元
  } catch (e) { }
  // WEB-1: `/config` は agent/label/model のみで construct_id・web_search の表示条件
  // （admin許可・接続先種別）を持たないため、ページ初回表示でも正しく判定できるよう
  // `/settings` を別途取得する（`_constructId` もここで更新しないと、ブラウザバッジを一度も
  // 開いていない間 codex_openai/codex_ollama を区別できず、表示条件が常に false のままになる）。
  try {
    const s = await getJSON('/settings');
    _constructId = s.construct_id || _constructId;
    _webSearchAvailable = !!s.web_search_available;
    _openaiEndpointKind = s.openai_endpoint_kind || 'openai';
    _syncWebSearchEligibility();
    // chat_examples（管理者設定・quick入力例のカスタマイズ）: null=未設定は組み込み既定のまま・
    // 取得失敗時（catch）は呼ばない＝どちらも fail-open で既定表示を保つ。
    setChatExamples(s.chat_examples);
    refreshWelcomeExamples();
  } catch (e) { }
}
// #1: バッジ＝AI/実行環境のクイック切替メニュー（接続テストもここで完結／モデルは管理画面）
// モデル名は個人設定に無い（管理者の使えるモデル一覧・選択中のクラウドプロバイダだけで決まる）。
// 唯一の例外は Bedrock（実在確認済みモデルの専用機構が個人設定側にあり、自由入力のまま）。
function _modelFieldHtml() {
  const current = _models[_agent] || '';
  return `<input id="bm-modelinput" aria-label="モデル名" placeholder="モデル名" value="${esc(current)}">`;
}
function renderBrainMenu() {
  const canEditModel = _agent === 'bedrock';
  const showModelNote = ['openai', 'gemini', 'ollama', 'codex'].includes(_agent);
  const canTest = ['openai', 'gemini', 'ollama', 'bedrock'].includes(_agent);   // Codex は CLI なので接続テストは出さない
  const modelBlock = canEditModel
    ? `<div class="bm-model">${_modelFieldHtml()}`
      + '<button class="mini" id="bm-modelsave">保存</button>'
      + (canTest ? '<button class="mini" id="bm-modeltest">接続テスト</button>' : '') + '</div>'
      + (canTest ? '<div class="bm-tres muted" id="bm-tres"></div>' : '')
    : showModelNote
      ? '<div class="bm-model">モデルは管理画面（管理者設定）で選びます。'
        + (canTest ? '<button class="mini" id="bm-modeltest">接続テスト</button>' : '') + '</div>'
        + (canTest ? '<div class="bm-tres muted" id="bm-tres"></div>' : '')
      : '';
  $('brainmenu').innerHTML = '<div class="bm-h">利用するAI・実行環境</div>'
    + '<div class="bm-note">ここでの変更は個人設定（既定）として保存され、以後の会話にも適用されます</div>'
    + PROVIDERS.map((p) => `<button class="brainitem${p.id === _constructId ? ' on' : ''}" data-exec="${esc(p.id)}">`
      + `<b>${esc(p.label)}</b><small>${esc(p.hint)}</small></button>`).join('')
    + modelBlock
    + '<button class="brainitem cfg" data-cfg="1">⚙ APIキー等の詳細設定</button>';
}
async function saveModel() {
  // Bedrock（唯一の個人設定モデル欄）のみここへ配線される（他プロバイダは保存ボタン自体が無い）。
  const el = $('bm-modelinput');
  if (!el) return;
  const v = el.value.trim();
  if (!v) return;   // 自由入力は空欄なら何もしない（誤クリア防止）
  try {
    const r = await fetch('/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ [_agent + '_model']: v }) });
    if (!r.ok) throw new Error(r.status);
    _models[_agent] = v; loadConfig(); toast('モデルを保存しました');
  } catch (e) { toast('保存に失敗しました'); }
}
async function testModel() {
  const tr = $('bm-tres'); if (!tr) return;
  tr.className = 'bm-tres muted'; tr.textContent = '確認中…';
  const body = { provider: _agent };
  const modelEl = $('bm-modelinput');
  if (modelEl) body[_agent + '_model'] = modelEl.value.trim();   // モデル欄が無い場合は管理者の既定に任せる
  try {
    const d = await (await fetch('/settings/test', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })).json();
    tr.className = 'bm-tres ' + (d.ok ? 'ok' : 'danger');
    tr.textContent = d.ok ? '✓ 接続OK' : '✗ ' + (d.detail || '失敗');
  } catch (e) { tr.className = 'bm-tres danger'; tr.textContent = '✗ テスト失敗'; }
}
async function setConstruct(id) {
  const c = PROVIDERS.find((p) => p.id === id);
  if (!c) return;
  _constructId = id; _agent = c.agent;
  try {
    const r = await fetch('/settings', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ agent: c.agent, codex_model_provider: c.codex_model_provider || null }),
    });
    if (!r.ok) throw new Error(r.status);
  } catch (e) { toast('切替に失敗しました'); await loadConfig(); renderBrainMenu(); return; }  // 失敗時はサーバ状態へ戻す（楽観更新の取消）
  await loadConfig(); toast('AIを切り替えました');   // バッジ＋localStorage を更新
}
$('brainbadge').addEventListener('click', async (e) => {
  e.stopPropagation();
  try {
    const s = await getJSON('/settings');
    _models = { bedrock: s.bedrock_model };   // モデル欄は Bedrock のみ（他は管理画面で管理）
    // 実行構成はサーバが返すものだけを出す（env で無効な AI は並べない）。
    PROVIDERS = s.constructs_available || PROVIDERS;
    _constructId = s.construct_id || _constructId;
    // WEB-1: このメニューを開くたびに最新の admin 許可・接続先種別で表示条件を確定させる。
    _webSearchAvailable = !!s.web_search_available;
    _openaiEndpointKind = s.openai_endpoint_kind || 'openai';
    _syncWebSearchEligibility();
  } catch (_) { /* keep cache */ }
  renderBrainMenu();
  $('brainmenu').hidden = !$('brainmenu').hidden;
});
$('brainmenu').addEventListener('click', (e) => {
  e.stopPropagation();                                              // メニュー内クリックで外側クリック判定（=閉じる）を発火させない
  if (e.target.closest('#bm-modelsave')) return saveModel();        // モデルを保存（チャット画面で完結）
  if (e.target.closest('#bm-modeltest')) return testModel();        // 接続テスト（キー＋モデル）
  if (e.target.closest('#bm-modelinput')) return;                   // 入力クリックでは閉じない
  const cfg = e.target.closest('[data-cfg]'); if (cfg) { window.location.href = 'settings.html'; return; }
  const it = e.target.closest('[data-exec]'); if (!it) return;
  setConstruct(it.dataset.exec); renderBrainMenu();                 // 即座にモデル欄へ反映（切替後もメニューは開いたまま）
});
document.addEventListener('click', (e) => { if (!e.target.closest('.brainwrap')) $('brainmenu').hidden = true; });

// ===== チャット欄のみ文字サイズ（#5・localStorage 保持）=====
const FONTS = [['小', '13px'], ['標準', '14.5px'], ['大', '16.5px'], ['特大', '19px']];
let _font = localStorage.getItem('sherpa-chatfont') || '標準';
function applyFont() {
  const f = FONTS.find((x) => x[0] === _font) || FONTS[1];
  document.documentElement.style.setProperty('--chatfont', f[1]);
}
function renderFontMenu() {
  $('fontmenu').innerHTML = FONTS.map(([name]) => `<button class="fontitem${name === _font ? ' on' : ''}" data-fs="${name}">${name}</button>`).join('');
}
$('fontbtn').addEventListener('click', (e) => { e.stopPropagation(); renderFontMenu(); $('fontmenu').hidden = !$('fontmenu').hidden; });
$('fontmenu').addEventListener('click', (e) => {
  const it = e.target.closest('[data-fs]'); if (!it) return;
  _font = it.dataset.fs; localStorage.setItem('sherpa-chatfont', _font); applyFont();
  $('fontmenu').hidden = true;
});
document.addEventListener('click', (e) => { if (!e.target.closest('.fontsel')) $('fontmenu').hidden = true; });
applyFont();

// ===== エクスポート（#6・会話全体＋回答単位／Markdown・テキスト・JSON・PDF(印刷)）=====
function _stamp() {
  const d = new Date(), p = (n) => String(n).padStart(2, '0');
  return { human: `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`,
    file: `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}` };
}
function _exportName(title, ext) {   // ファイル名にタイトル＋日時
  const safe = (title || 'chat').replace(/[\\/:*?"<>|\s]+/g, '_').slice(0, 40) || 'chat';
  return `${safe}_${_stamp().file}.${ext}`;
}
const LENS_FULL = { impact: '影響範囲分析', troubleshoot: 'トラブルシュート', qa: '仕様問い合わせ', chat: '通常チャット', author: '資料を作成' };
function _scopeText(ans) {   // 参照範囲（world/scope/source）を1行に
  const sc = ans.scope || {};
  if (sc.source === 'off') return 'ナレッジ参照オフ';
  const r = (sc.scope_paths && sc.scope_paths.length) ? sc.scope_paths.join('、') : '全体';
  return (sc.world ? (S.verLabels[sc.world] || sc.world) + ' / ' : '') + r;   // 表示は実名(4期)
}
function _answerLines(ans, md) {
  const L = [(md ? '**回答（' : '回答（') + (LENS_FULL[ans.lens] || ans.lens) + (md ? '）**' : '）'),
    (md ? '_範囲: ' : '範囲: ') + _scopeText(ans) + (md ? '_' : ''), ans.headline || ''];
  const d = ans.data || {};
  if (ans.lens === 'impact') (d.items || []).forEach((it) => L.push(`${md ? '- ' : '・'}${it.category}｜${it.name}`));
  if (ans.lens === 'impact') (d.presumed || []).forEach((p) => L.push(`${md ? '- ' : '・'}推定｜${p.category}｜${p.name}`));
  if (ans.lens === 'troubleshoot') (d.candidates || []).slice(0, 8).forEach((c) => L.push(`${md ? '- ' : '・'}${c.name}（${c.role || ''}）`));
  if (ans.lens === 'qa') (d.citations || []).forEach((c) => L.push(`${md ? '> ' : ''}${c.doc_id}（行${(c.span || [])[0]}-${(c.span || [])[1]}）: ${c.quote || ''}`));
  if ((ans.sources || []).length) {
    // EV-0（拡張設計 §4.4）: sources_verified があれば書き出しも根拠/参考の2区分にする（render.js と同じ区分・除外はしない）。
    const verified = Array.isArray(ans.sources_verified) ? new Set(ans.sources_verified) : null;
    if (verified) {
      const grounded = ans.sources.filter((s) => verified.has(s.doc_id)).map((s) => s.doc_id);
      const reference = ans.sources.filter((s) => !verified.has(s.doc_id)).map((s) => s.doc_id);
      if (grounded.length) L.push((md ? '**根拠:** ' : '根拠: ') + grounded.join(', '));
      if (reference.length) L.push((md ? '**参考:** ' : '参考: ') + reference.join(', '));
    } else {
      L.push((md ? '**出典:** ' : '出典: ') + ans.sources.map((s) => s.doc_id).join(', '));
    }
  }
  if ((ans.created_files || []).length) L.push((md ? '**作成したファイル:** ' : '作成したファイル: ') + ans.created_files.map((f) => f.name).join(', '));
  return L;
}
function _buildText(title, messages, md) {
  const L = [md ? `# ${title}` : title, (md ? '> ' : '') + `エクスポート: ${_stamp().human}`, ''];
  for (const m of messages) {
    if (m.role === 'user') L.push(md ? '## 質問' : '■ 質問', m.content || '', '');
    else if (m.answer) L.push(..._answerLines(m.answer, md), '');
  }
  return L.join('\n');
}
function _download(name, content, mime) {
  Sherpa.downloadBlob(new Blob([content], { type: mime }), name);   // UI フィードバック3: revoke タイミング問題を共通ヘルパで回避
}
// S8: chat.js entry の $('messages') data-export 委譲リスナー（回答単位の書き出し）から呼ばれるため export。
export function exportMessages(title, messages, format) {
  if (format === 'pdf') { window.print(); return; }   // ブラウザの印刷ダイアログ→PDF保存（将来は専用出力に差し替え可）
  if (format === 'json') _download(_exportName(title, 'json'), JSON.stringify({ title, exported_at: _stamp().human, messages }, null, 2), 'application/json');
  else if (format === 'txt') _download(_exportName(title, 'txt'), _buildText(title, messages, false), 'text/plain;charset=utf-8');
  else _download(_exportName(title, 'md'), _buildText(title, messages, true), 'text/markdown;charset=utf-8');
  toast('エクスポートしました');
}
async function exportChat(format) {
  const title = $('conv-title').textContent || 'chat';
  let messages = [];
  if (S.cid) { try { messages = (await getJSON('/conversations/' + S.cid)).messages; } catch (e) { } }
  if (!messages.length) { toast('書き出す内容がありません'); return; }
  exportMessages(title, messages, format);
}
function renderExportMenu() {
  $('exportmenu').innerHTML = '<div class="bm-h">この会話を書き出し</div>'
    + [['md', 'Markdown'], ['txt', 'テキスト'], ['json', 'JSON'], ['pdf', 'PDF（印刷）']].map(([f, l]) => `<button class="fontitem" data-exp="${f}">${l}</button>`).join('');
}
$('exportbtn').addEventListener('click', (e) => { e.stopPropagation(); renderExportMenu(); $('exportmenu').hidden = !$('exportmenu').hidden; });
$('exportmenu').addEventListener('click', (e) => { const it = e.target.closest('[data-exp]'); if (!it) return; $('exportmenu').hidden = true; exportChat(it.dataset.exp); });
document.addEventListener('click', (e) => { if (!e.target.closest('.exportsel')) $('exportmenu').hidden = true; });
