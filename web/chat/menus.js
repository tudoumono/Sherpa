// チャット画面のヘッダ周辺メニュー。AI・実行環境の切替（頭脳バッジ）、文字サイズ、テーマ、履歴の操作メニュー、会話の書き出しを扱う。
// 設計: docs/design/chat.md「2つの頭脳と、頭脳の選び方」
// toast は chat.js から、Web 検索の表示条件は inquiry.js へ通知する。
'use strict';

import { S, setChatExamples } from './state.js';
import { setSimpleMode, setWebSearchEligible } from './inquiry.js';
import { refreshWelcomeExamples, answerBody, answerNotices, unverifiedSourceRows, reconciliationRows, referencedDocRows, rangeText, impactListView, foundDocRows, howText, budgetNoteText, clipPersonalQuote } from './render.js';
import { toast } from '../chat.js';

const $ = Sherpa.$, esc = Sherpa.esc, getJSON = Sherpa.getJSON;

let _agent = null;                      // 現在のAI/実行環境
// 利用できる実行構成の一覧。GET /settings の constructs_available から、バッジを開いたときに取得する（失敗時は前回値）。
let PROVIDERS = [];
let _constructId = null;         // 現在の構成 id（codex_openai / codex_ollama を区別するため agent とは別に持つ）
// GET /settings の web_search_available（管理者許可）・openai_endpoint_kind（接続先種別）。
// Web 検索行は「管理者許可 かつ construct が codex_openai かつ接続先が OpenAI 直結」のときだけ出す（Codex(Ollama) は不可）。
let _webSearchAvailable = false;
let _openaiEndpointKind = 'openai';

// 構成・許可・接続先種別のいずれかが変わるたびに Web 検索の表示条件を inquiry.js へ通知する。
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
  setSimpleMode(_agent === 'simple');    // 簡易は調べ方・深さ・検索経路の行を隠す
  _syncWebSearchEligibility();
  $('brain-label').textContent = c.label || '…';
  $('brain-model').textContent = (c.model && c.model !== '—') ? '· ' + c.model : '';
}
// リロード直後、サーバ応答前に前回の選択を即表示する。
export function applyCachedBrain() {
  try { setBrainBadge(JSON.parse(localStorage.getItem('sherpa-brain') || 'null')); } catch (e) { }
}
// /config と /settings を読んで頭脳バッジと Web 検索の表示条件を更新する。
export async function loadConfig() {
  try {
    const c = await getJSON('/config');
    setBrainBadge(c);
    try { localStorage.setItem('sherpa-brain', JSON.stringify(c)); } catch (e) { }   // 次回の即時表示用
  } catch (e) { }
  // /config には construct_id と Web 検索の表示条件が無いため /settings も取得する
  try {
    const s = await getJSON('/settings');
    _constructId = s.construct_id || _constructId;
    _webSearchAvailable = !!s.web_search_available;
    _openaiEndpointKind = s.openai_endpoint_kind || 'openai';
    _syncWebSearchEligibility();
    // 管理者設定の質問例（未設定・取得失敗時は組み込み既定のまま）
    setChatExamples(s.chat_examples);
    refreshWelcomeExamples();
  } catch (e) { }
}
// 頭脳バッジのメニュー（AI・実行環境の切替）。モデルは管理画面で決まる。
function renderBrainMenu() {
  const isSimple = _agent === 'simple';
  const showModelNote = _agent === 'codex' || isSimple;
  const note = isSimple ? 'モデルは管理画面（簡易回答に使う AI）で選びます。' : 'モデルは管理画面（管理者設定）で選びます。';
  const modelBlock = showModelNote
    ? `<div class="bm-model">${note}</div>`
    : '';
  $('brainmenu').innerHTML = '<div class="bm-h">利用するAI・実行環境</div>'
    + '<div class="bm-note">ここでの変更は個人設定（既定）として保存され、以後の会話にも適用されます</div>'
    + PROVIDERS.map((p) => `<button class="brainitem${p.id === _constructId ? ' on' : ''}" data-exec="${esc(p.id)}">`
      + `<b>${esc(p.label)}</b><small>${esc(p.hint)}</small></button>`).join('')
    + modelBlock
    + '<button class="brainitem cfg" data-cfg="1">⚙ APIキー等の詳細設定</button>';
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
  } catch (e) { toast('切替に失敗しました'); await loadConfig(); renderBrainMenu(); return; }  // 失敗時はサーバ状態へ戻す
  await loadConfig(); toast('AIを切り替えました');
}
$('brainbadge').addEventListener('click', async (e) => {
  e.stopPropagation();
  try {
    const s = await getJSON('/settings');
    // 実行構成はサーバが返すものだけを出す
    PROVIDERS = s.constructs_available || PROVIDERS;
    _constructId = s.construct_id || _constructId;
    // 最新の許可・接続先種別で表示条件を確定させる
    _webSearchAvailable = !!s.web_search_available;
    _openaiEndpointKind = s.openai_endpoint_kind || 'openai';
    _syncWebSearchEligibility();
  } catch (_) { /* keep cache */ }
  renderBrainMenu();
  $('brainmenu').hidden = !$('brainmenu').hidden;
});
$('brainmenu').addEventListener('click', (e) => {
  e.stopPropagation();   // 外側クリックの閉じる判定を発火させない
  const cfg = e.target.closest('[data-cfg]'); if (cfg) { window.location.href = 'settings.html'; return; }
  const it = e.target.closest('[data-exec]'); if (!it) return;
  setConstruct(it.dataset.exec); renderBrainMenu();
});
document.addEventListener('click', (e) => { if (!e.target.closest('.brainwrap')) $('brainmenu').hidden = true; });

// ===== チャット欄の文字サイズ（localStorage に保持）=====
const FONTS = [['小', 'var(--chatfont-small)'], ['標準', 'var(--chatfont-standard)'],
  ['大', 'var(--chatfont-large)'], ['特大', 'var(--chatfont-largest)']];
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

// 履歴行の「その他の操作」メニューの開閉・キーボード操作
function closeConversationMenu(restoreFocus = false) {
  const trigger = $('convlist').querySelector('[data-conv-menu][aria-expanded="true"]');
  if (!trigger) return;
  $(trigger.getAttribute('aria-controls')).hidden = true;
  trigger.setAttribute('aria-expanded', 'false');
  if (restoreFocus) trigger.focus();
}
$('convlist').addEventListener('click', (e) => {
  const trigger = e.target.closest('[data-conv-menu]');
  if (trigger) {
    const wasOpen = trigger.getAttribute('aria-expanded') === 'true';
    closeConversationMenu();
    if (wasOpen) return;
    const menu = $(trigger.getAttribute('aria-controls'));
    menu.hidden = false;
    trigger.setAttribute('aria-expanded', 'true');
    const rect = trigger.getBoundingClientRect();
    menu.style.left = Math.max(8, Math.min(rect.left, innerWidth - menu.offsetWidth - 8)) + 'px';
    menu.style.top = Math.max(8, Math.min(rect.bottom + 4, innerHeight - menu.offsetHeight - 8)) + 'px';
    menu.querySelector('button').focus();
  } else if (e.target.closest('.cacts button')) {
    closeConversationMenu(true);
  }
});
$('convlist').addEventListener('keydown', (e) => {
  const menu = e.target.closest('.cacts');
  if (e.key === 'Escape') {
    if (!$('convlist').querySelector('[data-conv-menu][aria-expanded="true"]')) return;
    e.preventDefault();
    closeConversationMenu(true);
  } else if (menu && ['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) {
    e.preventDefault();
    const buttons = [...menu.querySelectorAll('button')];
    const index = buttons.indexOf(document.activeElement);
    const next = e.key === 'Home' ? 0 : e.key === 'End' ? buttons.length - 1
      : (index + (e.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length;
    buttons[next].focus();
  }
});
document.addEventListener('click', (e) => {
  if (!e.target.closest('.cacts, [data-conv-menu]')) closeConversationMenu();
});
document.addEventListener('focusin', (e) => {
  if (!e.target.closest('.cacts, [data-conv-menu]')) closeConversationMenu();
});
// スクロール・リサイズ時はメニューを閉じる
$('convlist').closest('.pane-body').addEventListener('scroll', () => closeConversationMenu(true));
window.addEventListener('resize', () => closeConversationMenu(true));

// ===== 書き出し（会話全体・回答単位／Markdown・テキスト・JSON・PDF(印刷)）=====
function _stamp() {
  const d = new Date(), p = (n) => String(n).padStart(2, '0');
  return { human: `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`,
    file: `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}` };
}
function _exportName(title, ext) {
  const safe = (title || 'chat').replace(/[\\/:*?"<>|\s]+/g, '_').slice(0, 40) || 'chat';
  return `${safe}_${_stamp().file}.${ext}`;
}
const LENS_FULL = { investigate: '調べる', impact: '影響範囲分析', troubleshoot: 'トラブルシュート', qa: '仕様問い合わせ', chat: '通常チャット', author: '資料を作成' };
const COMPLETION_TEXT = { partial: '途中までの回答', stopped: '停止した時点までの回答', failed: '回答できませんでした' };
function _scopeText(ans) {   // 参照範囲を1行にする
  const sc = ans.scope || {};
  if (sc.source === 'off') return '社内資料参照オフ';
  const r = (sc.scope_paths && sc.scope_paths.length) ? sc.scope_paths.join('、') : '全体';
  return (sc.world ? (S.verLabels[sc.world] || '名称未設定の資料フォルダ') + ' / ' : '') + r;
}
// shared=true は共有された会話の書き出し（個人の出典は出さない）。
function _answerLines(ans, md, shared) {
  const L = [(md ? '**回答（' : '回答（') + (LENS_FULL[ans.lens] || ans.lens || '未判定') + (md ? '）**' : '）'),
    (md ? '_範囲: ' : '範囲: ') + _scopeText(ans) + (md ? '_' : '')];
  // 注記・完了状態は本文の前に書き出す（画面と同じ並び）。旧形式の行は headline が本文になり、注記は無い。
  const how = howText(ans);
  if (how) L.push((md ? '**調べ方:** ' : '調べ方: ') + how);
  const state = COMPLETION_TEXT[ans.completion];
  if (state) L.push((md ? '**状態:** ' : '状態: ') + state);
  const notices = answerNotices(ans);
  notices.forEach((n) => L.push((md ? '> ' : '※ ') + n.text.trim().replace(/\n+/g, md ? '\n> ' : ' ')));
  // 予算で止まった旨（簡易の古い形の evidence_packet.stop_reason）。画面の注記と同じ文言。
  const budget = budgetNoteText(ans.data && ans.data.evidence_packet);
  if (budget) L.push((md ? '> ' : '※ ') + budget);
  if (md && (notices.length || budget)) L.push('');
  L.push(answerBody(ans));
  const sum = ans.investigation_summary;
  ((sum && Array.isArray(sum.items)) ? sum.items : []).forEach((i) => {
    if (i && typeof i.text === 'string' && i.text.trim()) L.push(`${md ? '- ' : '・'}${i.label ? i.label + ': ' : ''}${i.text}`);
  });
  const d = ans.data || {};
  // 画面（render.js）と同じ条件で並べる。グラフ由来の結果が無い impact／troubleshoot は引用を書き出す
  const impactHasGraph = !!((d.items || []).length || (d.presumed || []).length);
  const troubleHasCandidates = !!(d.candidates || []).length;
  const newRefs = Array.isArray(ans.referenced_docs);  // 新しい形の回答は該当箇所・根拠／参考を出さず、参照した資料の行だけにする
  const showCitations = !newRefs && (ans.lens === 'qa' || ans.lens === 'investigate'
    || (ans.lens === 'impact' && !impactHasGraph)
    || (ans.lens === 'troubleshoot' && !troubleHasCandidates));
  if (ans.lens === 'impact') (d.items || []).forEach((it) => L.push(`${md ? '- ' : '・'}${it.category}｜${it.name}`));
  if (ans.lens === 'impact') (d.presumed || []).forEach((p) => L.push(`${md ? '- ' : '・'}資料から見つけた関連｜${p.category}｜${p.name}`));
  if (ans.lens === 'troubleshoot') (d.candidates || []).forEach((c) => L.push(`${md ? '- ' : '・'}${c.name}（${c.role || ''}）`));
  if (showCitations) (d.citations || []).forEach((c) => L.push(`${md ? '> ' : ''}${c.doc_id}（行${(c.span || [])[0]}-${(c.span || [])[1]}）: ${c.quote || ''}`));
  const recon = reconciliationRows(ans);
  if (recon.length) {
    const ref = (sd) => sd.doc_id + (sd.line ? `:${sd.line}${sd.lineEnd ? `-${sd.lineEnd}` : ''}` : '');
    const cellText = (sd) => [sd.text, ref(sd) ? `（${ref(sd)}）` : ''].join('').replace(/\s+/g, ' ').replace(/\|/g, '/').trim();
    if (md) {
      L.push('**設計書とソースの照らし合わせ**', '', '| 項目 | 設計書の記述 | ソースの実装 | 判定 |', '| --- | --- | --- | --- |');
      recon.forEach((r) => L.push(`| ${r.item.replace(/\s+/g, ' ').replace(/\|/g, '/').trim()} | ${cellText(r.spec)} | ${cellText(r.source)} | ${r.label} |`));
    } else {
      L.push('設計書とソースの照らし合わせ');
      recon.forEach((r) => L.push(`・${r.item.replace(/\s+/g, ' ').trim()}｜設計書: ${cellText(r.spec) || '-'}｜ソース: ${cellText(r.source) || '-'}｜${r.label}`));
    }
  }
  const bullet = md ? '- ' : '・';
  const il = impactListView(ans);
  if (il) {
    L.push(md ? '**影響一覧**' : '影響一覧');
    if (!il.traced) L.push('影響をたどっていません');
    il.rows.forEach((r) => L.push(`${bullet}${r.name}${r.origin ? '（起点）' : ''}｜${r.label}${r.reason ? '｜' + r.reason : ''}`));
    il.reasons.forEach((x) => L.push(`${bullet}${x}`));
    if (il.more) L.push(`${bullet}ほか ${il.more} 件`);
    if (il.hidden) L.push(`${bullet}名前を表示できない項目 ${il.hidden} 件`);
  }
  const ref = referencedDocRows(ans);
  if (newRefs) {
    L.push(md ? '**参照した資料**' : '参照した資料');
    if (!ref.rows.length && !ref.hidden && !ref.more) L.push('参照した資料はありません');
    const rangeNote = (r) => (r.ranges.length || r.rangesMore ? '（' + [r.ranges.length ? rangeText(r) : '', r.rangesMore ? `ほか ${r.rangesMore} か所` : ''].filter(Boolean).join('、') + '）' : '');
    ref.rows.forEach((r) => L.push(`${bullet}${r.path}${rangeNote(r)}${r.unopened ? '（Codex の読み取り記録なし）' : ''}`));
    if (ref.more) L.push(`${bullet}ほか ${ref.more} 件`);
    if (ref.hidden) L.push(`${bullet}名前を表示できない資料 ${ref.hidden} 件`);
  }
  const fnd = foundDocRows(ans);
  if (fnd.rows.length || fnd.hidden || fnd.more) {
    L.push(md ? '**ほかに見つかった資料（Codex の読み取り記録なし）**' : 'ほかに見つかった資料（Codex の読み取り記録なし）');
    fnd.rows.forEach((p) => L.push(`${bullet}${p}`));
    if (fnd.more) L.push(`${bullet}ほか ${fnd.more} 件（上限で省略）`);
    if (fnd.hidden) L.push(`${bullet}名前を表示できない資料 ${fnd.hidden} 件`);
  }
  if (!newRefs && (ans.sources || []).length) {
    // sources_verified があれば根拠/参考の2区分で書き出す（render.js と同じ）
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
  const unv = unverifiedSourceRows(ans);
  if (unv.rows.length || unv.hidden || unv.more) {
    L.push((md ? '**確認できなかった資料:** ' : '確認できなかった資料: ')
      + [...unv.rows.map((u) => u.path + (u.reason ? `（${u.reason}）` : '')),
        ...(unv.more ? [`ほか ${unv.more} 件`] : []), ...(unv.hidden ? [`名前を表示できない資料 ${unv.hidden} 件`] : [])].join(', '));
  }
  // 個人の出典は本人の書き出しだけに入れる（共有された会話では出さない）。
  const personal = shared ? [] : (ans.personal_sources || []).filter((p) => p && p.doc_id);
  if (personal.length) {
    L.push((md ? '**個人ファイル内ヒット（本人のみ）:** ' : '個人ファイル内ヒット（本人のみ）: '));
    personal.forEach((p) => L.push(`${md ? '- ' : '・'}${p.doc_id}${p.quote ? ': ' + clipPersonalQuote(String(p.quote)).replace(/\s+/g, ' ') : ''}`));
  }
  if ((ans.created_files || []).length) L.push((md ? '**作成したファイル:** ' : '作成したファイル: ') + ans.created_files.map((f) => f.name).join(', '));
  return L;
}
function _buildText(title, messages, md, shared) {
  const L = [md ? `# ${title}` : title, (md ? '> ' : '') + `エクスポート: ${_stamp().human}`, ''];
  for (const m of messages) {
    if (m.role === 'user') L.push(md ? '## 質問' : '■ 質問', m.content || '', '');
    else if (m.answer) L.push(..._answerLines(m.answer, md, shared), '');
  }
  return L.join('\n');
}
function _download(name, content, mime) {
  Sherpa.downloadBlob(new Blob([content], { type: mime }), name);
}
// 回答単位の書き出し（chat.js の data-export 委譲リスナーが呼ぶ）。
export function exportMessages(title, messages, format, shared = false) {
  if (format === 'pdf') { window.print(); return; }   // 印刷ダイアログから PDF 保存
  if (format === 'json') _download(_exportName(title, 'json'), JSON.stringify({ title, exported_at: _stamp().human, messages }, null, 2), 'application/json');
  else if (format === 'txt') _download(_exportName(title, 'txt'), _buildText(title, messages, false, shared), 'text/plain;charset=utf-8');
  else _download(_exportName(title, 'md'), _buildText(title, messages, true, shared), 'text/markdown;charset=utf-8');
  toast('エクスポートしました');
}
async function exportChat(format) {
  const title = $('conv-title').textContent || 'chat';
  let messages = [], shared = false;
  if (S.cid) {
    try {
      const data = await getJSON('/conversations/' + S.cid);
      messages = data.messages;
      shared = !!(data.conversation && data.conversation.origin === 'received_share');
    } catch (e) { }
  }
  if (!messages.length) { toast('書き出す内容がありません'); return; }
  exportMessages(title, messages, format, shared);
}
function renderExportMenu() {
  $('exportmenu').innerHTML = '<div class="bm-h">この会話を書き出し</div>'
    + [['md', 'Markdown'], ['txt', 'テキスト'], ['json', 'JSON'], ['pdf', 'PDF（印刷）']].map(([f, l]) => `<button class="fontitem" data-exp="${f}">${l}</button>`).join('');
}
$('exportbtn').addEventListener('click', (e) => { e.stopPropagation(); renderExportMenu(); $('exportmenu').hidden = !$('exportmenu').hidden; });
$('exportmenu').addEventListener('click', (e) => { const it = e.target.closest('[data-exp]'); if (!it) return; $('exportmenu').hidden = true; exportChat(it.dataset.exp); });
document.addEventListener('click', (e) => { if (!e.target.closest('.exportsel')) $('exportmenu').hidden = true; });
