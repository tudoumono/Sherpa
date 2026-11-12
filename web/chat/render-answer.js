// チャットの描画のうち回答カード（本文・注記・使用量・フィードバック・確定処理）。
// 設計: docs/design/chat.md「1ターンの流れ」
'use strict';

import { S } from './state.js';
import { BUDGET_EXHAUSTED_STOP_REASONS, BUDGET_NOTE_TEXT, _computeProviderSummary, _providerSummaryHTML, attachTraceButton } from './render-trace.js';
import { renderImpact, renderTrouble, renderQa, renderSources, linkDocsInBodyHTML, howText, hasReferencedDocs, renderReferencedDocs, renderImpactList, renderFoundDocs, howChipHTML, renderReconciliation, renderSourcesUnverified, renderInvestigationRecord } from './render-sources.js';

const $ = Sherpa.$, esc = Sherpa.esc, fmtDateTime = Sherpa.fmtDateTime, mdLite = Sherpa.mdLite, analyzerLabel = Sherpa.analyzerLabel;
const LENS_LABEL = { investigate: '調べる', impact: '影響範囲分析', troubleshoot: 'トラブルシュート', qa: '仕様問い合わせ', author: '資料を作成' };

// ===== 描画 =====
function scroll() { const m = $('messages'); m.scrollTop = m.scrollHeight; }
export function appendUser(text) {
  const d = document.createElement('div'); d.className = 'msg user';
  d.innerHTML = `<div style="display:flex;flex-direction:column;align-items:flex-end;max-width:78%">`
    + `<div class="bubble-user">${esc(text)}</div><button class="copybtn" data-copy>⧉ コピー</button></div>`;
  $('messages').appendChild(d); scroll();
}
export function appendAssistantRaw(innerHtml) {
  const d = document.createElement('div'); d.className = 'msg';
  d.innerHTML = `<div class="a-row"><div class="a-avatar">S</div><div class="a-body">${innerHtml}</div></div>`;
  $('messages').appendChild(d); scroll(); return d;
}
// 個人ファイルの引用は 200 文字で切り、切ったときは末尾に「…」を付ける。
export const PERSONAL_QUOTE_MAX = 200;
export function clipPersonalQuote(q) { return q.length > PERSONAL_QUOTE_MAX ? q.slice(0, PERSONAL_QUOTE_MAX) + '…' : q; }
function renderPersonalSources(personal_sources) {
  // 個人ファイル内ヒットを別枠で表示する（DL リンクなし）
  const srcs = (personal_sources || []).filter((s) => s && s.doc_id);
  if (!srcs.length) return '';
  const items = srcs.map((s) =>
    `<li><span class="src-name">${esc(s.doc_id)}</span>`
    + (s.quote ? `<pre class="src-snippet">${esc(clipPersonalQuote(String(s.quote)))}</pre>` : '')
    + '</li>').join('');
  return `<div class="personal-sources"><div class="personal-sources-h">🗂 個人ファイル内ヒット（本人のみ・共有不可）</div><ul>${items}</ul></div>`;
}
function renderCreatedFiles(created_files) {
  // Codex が作成したファイルの DL カード。data-dl は chat.js の委譲ハンドラが処理する（リンクテキストはそのままファイル名に使われる）。共有ではリンクを持たない（名前だけ）。
  const files = (created_files || []).filter((f) => f && f.name);
  if (!files.length) return '';
  const items = files.map((f) => f.download_url
    ? `<li><a href="${esc(f.download_url)}" data-dl>${esc(f.name)}</a></li>`
    : `<li>${esc(f.name)}</li>`
  ).join('');
  return `<div class="created-files"><div class="created-files-h">📎 作成したファイル</div><ul>${items}</ul>`
    + (files.some((f) => f.download_url) ? `<a href="workspace.html" class="created-files-link">マイワークスペースで開く</a>` : '')
    + '</div>';
}
// トークン使用量のターン末尾表示（入力/出力トークン数のみ）。usage が無いターンは何も出さない。クリックで内訳を展開する。
function _fmtTokensCompact(n) {
  n = Math.max(n | 0, 0);
  if (n >= 1000000) return (n / 1000000).toFixed(n >= 10000000 ? 0 : 1).replace(/\.0$/, '') + 'M';
  if (n >= 1000) return Math.round(n / 1000) + 'k';
  return String(n);
}
// cached_input_tokens は入力の内数。数値のときだけ「実入力＝入力−キャッシュ」を出し、null は総入力のみにする。
function _hasCacheBreakdown(v) { return typeof v === 'number'; }
function usageMetaHTML(u) {
  if (!u || typeof u !== 'object') return '';
  const inTotal = u.input_tokens | 0;
  const hasCache = _hasCacheBreakdown(u.cached_input_tokens);
  const cachedN = hasCache ? Math.max(u.cached_input_tokens | 0, 0) : 0;
  const actualIn = hasCache ? Math.max(inTotal - cachedN, 0) : inTotal;
  const outC = _fmtTokensCompact(u.output_tokens);
  // キャッシュが 0 のときは（+0 cache）を出さない
  const headIn = (hasCache && cachedN > 0)
    ? `${esc(_fmtTokensCompact(actualIn))} in（+${esc(_fmtTokensCompact(cachedN))} cache）`
    : `${esc(_fmtTokensCompact(inTotal))} in`;
  const cachedDetail = hasCache
    ? `（実入力 ${actualIn.toLocaleString()}・キャッシュ ${cachedN.toLocaleString()}）` : '';
  const reason = u.reasoning_output_tokens ? `（うち推論 ${(u.reasoning_output_tokens | 0).toLocaleString()}）` : '';
  return `<details class="usage-meta"><summary>🪙 ${headIn} / ${esc(outC)} out</summary>`
    + `<div class="usage-detail"><div>入力トークン: ${inTotal.toLocaleString()} <span class="muted">${cachedDetail}</span></div>`
    + `<div>出力トークン: ${(u.output_tokens | 0).toLocaleString()} <span class="muted">${reason}</span></div>`
    + _usageBreakdownRowsHTML(u.codex_usage_breakdown)
    + '</div></details>';
}
// 本体/下調べ役の内訳（usage.codex_usage_breakdown）。体数は起動した数（found+missing）。0 体なら何も出さない。
function _usageBreakdownRowsHTML(breakdown) {
  if (!breakdown || typeof breakdown !== 'object') return '';
  const found = breakdown.children_found | 0;
  const missing = breakdown.children_missing | 0;
  const total = found + missing;
  if (!total) return '';
  const parent = breakdown.parent || {}, children = breakdown.children || {};
  // キャッシュ 0 は出さない
  const cacheNote = (part) => {
    const v = part && part.cached_input_tokens;
    return (_hasCacheBreakdown(v) && (v | 0) > 0)
      ? ` <span class="muted">（うちキャッシュ ${(v | 0).toLocaleString()}）</span>` : '';
  };
  const missingNote = missing > 0
    ? ` <span class="muted">（${missing} 体は記録なし）</span>` : '';
  return `<div>本体: 入力 ${(parent.input_tokens | 0).toLocaleString()}${cacheNote(parent)}`
    + ` / 出力 ${(parent.output_tokens | 0).toLocaleString()}</div>`
    + `<div>下調べ役 ${total} 体: 入力 ${(children.input_tokens | 0).toLocaleString()}${cacheNote(children)}`
    + ` / 出力 ${(children.output_tokens | 0).toLocaleString()}${missingNote}</div>`;
}
// 下調べ（サブループ）のトークン使用量をプロファイル別に表示する。answer.usage_subs（複数）と answer.usage_sub（単一）のどちらも見る。
function usageSubMetaHTML(answer) {
  // 両キーが共存した場合は usage_subs が2件以上ならそちら、そうでなければ usage_sub を採る
  const subs = Array.isArray(answer.usage_subs) ? answer.usage_subs : [];
  const list = subs.length >= 2 ? subs : (answer.usage_sub ? [answer.usage_sub] : subs);
  if (!list.length) return '';
  const rows = list.map((u) => {
    const name = esc((u && u.profile) || '下調べ');
    const inN = ((u && u.input_tokens) | 0).toLocaleString();
    const outN = ((u && u.output_tokens) | 0).toLocaleString();
    return `<div>${name}: 入力 ${inN} / 出力 ${outN} トークン</div>`;
  }).join('');
  const summary = list.length > 1 ? `🧭 下調べの使用量（${list.length}件）` : '🧭 下調べの使用量';
  return `<details class="usage-meta usage-sub-meta"><summary>${summary}</summary>`
    + `<div class="usage-detail">${rows}</div></details>`;
}
// 回答ごとのフィードバック欄（👍/👎＋定型タグ＋任意の一言）。送信は chat.js の #messages 委譲が担う（送信先は el._messageId）。
// feedback（{rating,tags,comment}）があれば履歴復元時に前回の選択状態を再現する。省略時は未選択。
function feedbackHTML(feedback) {
  const rating = feedback && feedback.rating;
  const pickedTags = new Set((feedback && feedback.tags) || []);
  const comment = (feedback && feedback.comment) || '';
  const tagOptions = [
    ['wrong_evidence', '根拠が違う'], ['incomplete', '足りない'],
    ['outdated', '古い版'], ['slow', '遅い'],
  ].map(([v, label]) => `<label class="fbtag"><input type="checkbox" value="${v}"`
      + `${pickedTags.has(v) ? ' checked' : ''}> ${label}</label>`).join('');
  const upOn = rating === 'up' ? ' on' : '';
  const downOn = rating === 'down' ? ' on' : '';
  const panelHidden = rating === 'down' ? '' : ' hidden';
  const thanksHidden = rating ? '' : ' hidden';
  return '<div class="msg-feedback">'
    + `<button class="fbbtn${upOn}" data-fb="up">👍 <span class="fblabel">役に立った</span></button>`
    + `<button class="fbbtn${downOn}" data-fb="down">👎 <span class="fblabel">役に立たなかった</span></button>`
    + `<div class="fbpanel"${panelHidden}><div class="fbtags">${tagOptions}</div>`
    + `<textarea class="fbcomment" maxlength="500" placeholder="一言（任意）">${esc(comment)}</textarea>`
    + '<div class="fbactions"><button class="copybtn fbsend" data-fb-send>送信</button></div></div>'
    + `<span class="fbthanks"${thanksHidden}>フィードバックを送信しました</span></div>`;
}
// 回答カード本体の HTML を組み立てる（レンズごとの本文・出典・注記・使用量・フィードバック）。
function answerHTML(answer, trace, feedback) {
  // personal_sources は全レンズで末尾に追加する
  const personalHTML = renderPersonalSources(answer.personal_sources);
  const usageHTML = usageMetaHTML(answer.usage);
  const usageSubHTML = usageSubMetaHTML(answer);
  const feedbackHtml = feedbackHTML(feedback);
  // 「実行の分担」は trace_version=2 のターンだけ出す
  const summaryHTML = (answer.trace_version === 2)
    ? _providerSummaryHTML(_computeProviderSummary(trace, answer)) : '';
  if (answer.lens === 'chat') {   // 資料参照オフの通常チャット（出典枠なし）
    return `<div class="chips"><span class="chip ghost">💬 通常チャット（社内資料参照オフ）</span></div>`
      + answerNoticesHTML(answer) + `<div class="headline">${mdLite(answerBody(answer))}</div>`
      + investigationSummaryHTML(answer) + personalHTML + summaryHTML + usageHTML + usageSubHTML
      + '<button class="copybtn" data-copy>⧉ コピー</button><button class="copybtn" data-export>⬇ 書き出し</button>'
      + feedbackHtml;
  }
  // レンズ未決定（lens が null）のときは空のチップを出さない
  const lensLabel = LENS_LABEL[answer.lens] || answer.lens;
  const chip = `<div class="chips">${lensLabel && !howText(answer) ? `<span class="chip">${esc(lensLabel)}</span>` : ''}${howChipHTML(answer)}`
    + _scopeChipsHTML(answer.scope, !!answer.how)
    + ((answer.route && answer.route.path) || []).map((p) => `<span class="chip">${esc(p)}</span>`).join('')
    + _depthHeaderHTML(answer.scope, answer.duration_ms) + '</div>';
  // impact はグラフ由来（items/presumed）と検索由来（citations）の2形がある。グラフ結果が無い回答は QA と同じ引用表示にする
  const impactHasGraph = !!(answer.data && ((answer.data.items || []).length || (answer.data.presumed || []).length || (answer.data.candidates || []).length));
  // troubleshoot も、原因候補（candidates）が無ければ QA と同じ引用表示にする
  const troubleHasCandidates = !!(answer.data && (answer.data.candidates || []).length);
  const body = (answer.lens === 'impact' && impactHasGraph) ? renderImpact(answer)
    : (answer.lens === 'troubleshoot' && troubleHasCandidates) ? renderTrouble(answer)
    : hasReferencedDocs(answer) ? '' : renderQa(answer);
  // 検証バッジは trace_version=2 の回答だけに付ける
  const evidencePacketForBadges = answer.trace_version === 2 ? (answer.data && answer.data.evidence_packet) : null;
  return chip + answerNoticesHTML(answer) + `<div class="headline">${linkDocsInBodyHTML(mdLite(answerBody(answer)), answer)}</div>`
    + budgetNoteHTML(answer.data && answer.data.evidence_packet)
    + investigationSummaryHTML(answer)
    + retryHintsHTML(answer.retry_hints) + body
    + renderCreatedFiles(answer.created_files)
    + renderReconciliation(answer)
    + renderImpactList(answer)
    + (hasReferencedDocs(answer) ? renderReferencedDocs(answer, evidencePacketForBadges)
      : renderSources(answer.sources, answer.sources_verified, evidencePacketForBadges))
    + renderFoundDocs(answer)
    + renderSourcesUnverified(answer)
    + renderInvestigationRecord(answer.investigation) + personalHTML
    + summaryHTML + usageHTML + usageSubHTML
    + '<button class="copybtn" data-copy>⧉ コピー</button><button class="copybtn" data-export>⬇ 書き出し</button>'
    + feedbackHtml;
}

// 回答ヘッダに、使った範囲（古い回答は探す対象も）をチップで示す。層が効かないレンズ（layer_applied:false）は「非適用」と注記する。
const LAYER_CHIP_LABEL = { both: '資料＋コード', docs: '資料のみ', code: 'コードのみ' };
function _scopeChipsHTML(scope, hasHow) {
  if (!scope) return '';
  const paths = scope.scope_paths || [];
  const scopeLabel = paths.length ? paths.map((p) => S.scopeLabels[p] || p.split('/').pop()).join('・') : '全体';
  let out = `<span class="chip">${esc(scopeLabel)}</span>`;
  if (scope.layer && !hasHow) {   // 新しいターン（how あり）は探す対象を出さない
    const layerLabel = esc(LAYER_CHIP_LABEL[scope.layer] || scope.layer);
    out += scope.layer_applied === false
      ? `<span class="chip ghost" title="このやりたいこと（影響・原因）では探す対象の指定は使われません">${layerLabel}（非適用）</span>`
      : `<span class="chip">${layerLabel}</span>`;
  }
  return out;
}

// 調べる深さと所要時間を回答ヘッダの1チップで示す（「調べる深さ: 深く・所要 4分12秒」）。depth_profile が無ければ出さず、duration_ms が無ければ深さだけにする。
const DEPTH_CHIP_LABEL = { quick: 'クイック', standard: '標準', deep: '深く', max: '最大' };
function _fmtDurationJa(ms) {
  const totalSec = Math.round(ms / 1000);
  const m = Math.floor(totalSec / 60), s = totalSec % 60;
  return m > 0 ? `${m}分${s}秒` : `${s}秒`;
}
function _depthHeaderHTML(scope, durationMs) {
  if (!scope || !scope.depth_profile) return '';
  const label = esc(DEPTH_CHIP_LABEL[scope.depth_profile] || scope.depth_profile);
  const text = (typeof durationMs === 'number')
    ? `調べる深さ: ${label}・所要 ${_fmtDurationJa(durationMs)}` : `調べる深さ: ${label}`;
  return `<span class="chip ghost">${text}</span>`;
}

// 出典0件時の再検索案内ボタン。押すと該当設定を広げて同じ質問を再送する（chat.js の #messages リスナーが処理し、data-retry-action は action の JSON）。
function retryHintsHTML(hints) {
  if (!hints || !hints.length) return '';
  return '<div class="retry-hints">' + hints.map((h) =>
    `<button class="retry-hint-btn" data-retry-kind="${esc(h.kind)}" data-retry-action="${esc(JSON.stringify(h.action || {}))}">${esc(h.label)}</button>`
  ).join('') + '</div>';
}

// 調査予算の到達で打ち切られたターンに、本文とは別要素で注記を出す（evidence_packet.stop_reason だけが根拠）。
// 予算で止まった旨の文言（該当しなければ空）。画面と書き出しが同じ文言を使う。
export function budgetNoteText(evidencePacket) {
  const raw = evidencePacket && evidencePacket.stop_reason;
  return BUDGET_EXHAUSTED_STOP_REASONS.has(raw) ? BUDGET_NOTE_TEXT : '';
}
function budgetNoteHTML(evidencePacket) {
  const text = budgetNoteText(evidencePacket);
  return text ? `<div class="budget-note">${esc(text)}</div>` : '';
}

// 回答の本文。旧形式の行（body が無い）は headline を本文として読む。
export function answerBody(answer) {
  return typeof answer.body === 'string' ? answer.body : (answer.headline || '');
}
// 回答の注記（answer.notices: [{kind, text}]）。本文の上に控えめな帯で出す。旧形式の行・壊れた要素は出さない。
export function answerNotices(answer) {
  const list = answer && Array.isArray(answer.notices) ? answer.notices : [];
  return list.filter((n) => n && typeof n.text === 'string' && n.text.trim());
}
function answerNoticesHTML(answer) {
  const list = answerNotices(answer);
  if (!list.length) return '';
  return '<div class="answer-notices">' + list.map((n) =>
    `<div class="answer-notice" data-notice-kind="${esc(String(n.kind || ''))}">${mdLite(n.text)}</div>`).join('') + '</div>';
}
// 調べた範囲（answer.investigation_summary.items: [{label, text}]）。items が空なら何も出さない。
function investigationSummaryHTML(answer) {
  const s = answer && answer.investigation_summary;
  const items = s && Array.isArray(s.items) ? s.items.filter((i) => i && typeof i.text === 'string' && i.text.trim()) : [];
  if (!items.length) return '';
  return '<div class="investigation-summary">' + items.map((i) =>
    `<div class="investigation-summary-item"><span class="investigation-summary-label">${esc(String(i.label || ''))}</span> ${esc(i.text)}</div>`).join('') + '</div>';
}

// 回答カードを追加する。
export function appendAnswer(answer, messageId, trace, feedback) {
  const el = answer ? appendAssistantRaw(answerHTML(answer, trace, feedback)) : appendAssistantRaw('<div class="muted">（内容なし）</div>');
  if (el && answer) el._answer = answer;   // 回答単位の書き出し用
  if (el && messageId != null) el._messageId = messageId;   // フィードバック送信先
  return el;
}

// answer_delta で届いた回答を、そのまま一度に描く。
export function showAnswerText(text) {
  if (!S.ansHead) return;
  S.ansHead.textContent += text;
  scroll();
}
// 逐次表示用の回答カードを（無ければ）作る。
export function ensureAnswerCard(thinking) {
  if (S.ansEl) return;
  if (thinking) thinking.remove();
  S.ansEl = appendAssistantRaw('<div class="headline"></div>');
  S.ansHead = S.ansEl.querySelector('.headline');
}
// 逐次表示中のカードを確定した回答で置き換える（無ければ新規に追加し、trace があれば思考の流れボタンを添える）。
export function finalizeAnswer(thinking, answer, turnId, messageId, trace) {
  let el;
  if (!S.ansEl) {
    if (thinking) thinking.remove();
    el = appendAnswer(answer, messageId, trace);
  } else {
    S.ansEl.querySelector('.a-body').innerHTML = answerHTML(answer, trace);
    S.ansEl._answer = answer;
    if (messageId != null) S.ansEl._messageId = messageId;
    el = S.ansEl;
    S.ansEl = null; S.ansHead = null;
  }
  if (turnId && el) attachTraceButton(el, turnId);
}
