// チャットの描画のうち影響範囲・トラブル・出典・根拠・突合の表示。
// 設計: docs/design/chat.md「1ターンの流れ」
'use strict';

import { verificationBadgeHTML, _verificationMethodByDoc } from './render-trace.js';

const $ = Sherpa.$, esc = Sherpa.esc, fmtDateTime = Sherpa.fmtDateTime, mdLite = Sherpa.mdLite, analyzerLabel = Sherpa.analyzerLabel;
const STATUS_CLASS = { deprecated: 'deprecated', hidden_candidate: 'hidden_candidate' };
const STATUS_LABEL = { deprecated: '廃止', hidden_candidate: '未使用の疑い' };

function statusTag(s) {
  return STATUS_CLASS[s] ? `<span class="statustag ${STATUS_CLASS[s]}">${esc(STATUS_LABEL[s])}</span>` : '';
}
// 経路チップ列（trace のノード名列。並びは影響→…→起点で、起点=末尾を強調）
function impactRouteChipsHTML(trace) {
  if (!trace || !trace.length) return '';
  return trace.map((n, i) => {
    const arr = i ? '<span class="arr">←</span>' : '';
    return arr + `<span class="chip${i === trace.length - 1 ? ' origin' : ''}">${esc(n)}</span>`;
  }).join('');
}
// 影響の詳細「なぜつながっているか」の平文。関係の種類（via・辺の型）と解決の規則は内部の値を出さず、ここの対応表の文言に直す。
const _WHY_VIA = {
  call: 'を呼び出しています', extends: 'を継承しています', implements: 'を実装しています', field_type: 'を型として使っています',
  inject: 'を注入して使っています', include: 'を取り込んでいます', import: 'を取り込んでいます', copy: 'を COPY で取り込んでいます',
  bean_class: 'を設定でクラスとして指定しています', mapper_type: 'を設定で指定しています', mapper_namespace: 'を設定で指定しています',
  action_class: 'を設定で指定しています', config_value: 'を設定の値として参照しています', config_key: 'を設定のキーとして参照しています',
  exec_sql: 'の表を SQL で参照しています', exec_proc: 'を呼び出しています', include_member: 'を取り込んでいます',
  cics_xctl: 'を呼び出しています', cics_link: 'を呼び出しています', mapper_sql: 'の表を SQL で参照しています', vba_sql: 'の表を SQL で参照しています',
  write_file: 'に書き出されています', read_file: 'を読み込んでいます', use: 'を使っています',
};
const _WHY_TYPE = { INVOKES: 'を呼び出しています', COPIES: 'を取り込んでいます', ACCESSES: 'を参照しています', CONTAINS: 'を含んでいます', BUILT_FROM: 'を材料にして作られています' };
const _WHY_RULE = {
  alias: '別名の指定で決まりました', single_import: 'import で指定された型に一致しました', same_package: '同じ package（名前空間）の名前に一致しました',
  wildcard: 'import（まとめて指定）の範囲で一致しました', qualified_name: '完全な名前で一致しました', path_exact: '書かれたパスのとおりに一致しました',
  path_suffix: 'パスの末尾で一致しました', config_key_all: '同じ名前の設定キーすべてに一致しました', schema_exact: 'スキーマ名まで一致しました',
  schema_unqualified: '名前が一致しました（定義側にスキーマ名なし）', table_name: '表の名前で一致しました', nearest_name: '一番近い場所の同じ名前に一致しました',
  di_qualifier: '注入で指定された名前が実装の名前に一致しました', di_primary: '実装のうち優先の指定が付いたものに決まりました',
  di_single_impl: '実装が 1 つだけでした',
  build_definition: 'ビルドの定義に書かれた実行名と材料の組で決まりました',
  dd_name_match: 'プログラムのファイル名の指定と、ジョブのデータセットの指定が一致しました', copy_item_name: 'COPY で取り込んだ項目の名前に一致しました',
};
// 辺 1 本の「参照元」の平文（文書と行を複数・切った分は「ほか N 件」）。根拠（sources）のある辺だけ出す。
function _whySourcesText(e) {
  const srcs = Array.isArray(e.sources) ? e.sources : [];
  const one = (doc, line, loc) => esc(doc) + (loc ? `〔${esc(loc)}〕` : (line ? `〔行 ${esc(line)}〕` : ''));
  if (!srcs.length) return '';
  const list = srcs.filter((x) => x.doc_id).map((x) => one(x.doc_id, x.line, x.locator));
  if (!list.length) return '';
  const more = Number(e.sources_overflow_count) > 0 ? ` ほか ${esc(e.sources_overflow_count)} 件` : '';
  return `参照元: ${list.join(' / ')}${more}`;
}
function _whyListHTML(trace, evidence) {
  const edges = evidence || [];
  if (!edges.length) return '';
  const lis = edges.map((e, i) => {
    const verb = _WHY_VIA[e.via] || _WHY_TYPE[e.type] || 'とつながっています';
    const head = (trace[i] && trace[i + 1]) ? `<b>${esc(trace[i])}</b> は <b>${esc(trace[i + 1])}</b> ${esc(verb)}` : esc(verb.replace(/^を/, ''));
    const rule = _WHY_RULE[e.rule] ? `<div class="why-rule">${esc(_WHY_RULE[e.rule])}</div>` : '';
    const src = _whySourcesText(e);
    return `<div class="why-item"><div>${head}</div>${rule}${src ? `<div class="ev">${src}</div>` : ''}</div>`;
  }).join('');
  return `<div class="why"><div class="why-h">なぜつながっているか</div>${lis}</div>`;
}
// 影響 1 件の行（詳細＝経路チップ＋なぜつながっているか。トグルは行全体・キーボード操作は chat.js の委譲側）。
function _impactRowHTML(it) {
  const trace = it.trace || [];                      // 影響の経路（ノード名列）
  const chain = trace.join(' ← ');
  // evidence は代表経路の辺ごとの {type, doc, line, via?, rule?, sources?, sources_overflow_count?}（quote は presumed のみが持つ）
  const why = _whyListHTML(trace, (it.evidence || []).filter((e) => e.doc));
  const hasDetail = !!(chain || why);
  const detail = hasDetail
    ? `<div class="ixdetail">${trace.length ? `<div class="ix-route">${impactRouteChipsHTML(trace)}</div>` : ''}`
      + `<div class="path"><div class="chain">${esc(chain)}</div>${why}</div></div>`
    : '';
  const topAttrs = hasDetail ? ' role="button" tabindex="0" aria-expanded="false" data-toggle' : '';
  const toggle = hasDetail ? '<span class="pathbtn" aria-hidden="true">経路 <span class="caret">▾</span></span>' : '';
  // 担当アナライザの来歴（analyzer が null なら出さない）
  const analyzerNote = it.analyzer ? `<small>（解析: ${esc(analyzerLabel(it.analyzer))}）</small>` : '';
  return `<li><div class="top"${topAttrs}>`
    + `<span class="kind">${esc(it.category)}</span><span class="nm">${esc(it.name)}${analyzerNote}</span>${statusTag(it.status)}`
    + `<span class="spacer"></span>${toggle}</div>${detail}</li>`;
}
// データの流れの候補（ファイルの受け渡し・項目の使用）。構造の影響とは別枠で、折りたたみにする。
function _flowCandidatesHTML(candidates) {
  if (!candidates.length) return '';
  return `<details class="flow-candidates"><summary>データの流れの候補（ファイル受け渡し・レコード単位の過大を含む）${esc(candidates.length)} 件</summary>`
    + `<ul class="ilist">${candidates.map(_impactRowHTML).join('')}</ul></details>`;
}
// 影響範囲の回答本文（影響一覧。構造の依存が見つからなかったときは資料から見つけた関連を別枠で出す。データの流れの候補は折りたたみの別枠）。
export function renderImpact(a) {
  const items = (a.data && a.data.items) || [];
  const presumed = (a.data && a.data.presumed) || [];
  const candidates = (a.data && a.data.candidates) || [];
  if (!items.length && !presumed.length && !candidates.length) return '';
  const ilist = items.length ? `<ul class="ilist">${items.map(_impactRowHTML).join('')}</ul>` : '';
  let pres = '';
  if (presumed.length) {                              // 構造の依存が見つからなかったとき、資料から見つけた関連を別枠で出す
    const pl = presumed.map((p) => {
      const e0 = (p.evidence || [])[0] || {};
      const q = e0.quote ? `<div class="path"><div class="ev">根拠: ${esc(e0.quote)}${e0.doc ? `〔${esc(e0.doc)}〕` : ''}</div></div>` : '';
      return `<li><div class="top"><span class="kind">${esc(p.category)}</span><span class="nm">${esc(p.name)}</span></div>${q}</li>`;
    }).join('');
    pres = '<div class="muted" style="margin:6px 0 2px">構造の依存は見つかりませんでした。資料から見つけた関連:</div>'
      + `<ul class="ilist">${pl}</ul>`;
  }
  return ilist + pres + _flowCandidatesHTML(candidates);
}
// 原因候補の表示件数。超えた分は「ほか N 件」の折りたたみで全件を見られる（総数は summary.total と候補の長い方）。
const TROUBLE_SHOWN_MAX = 8;
function _candidateHTML(c) {
  return `<div class="cand"><span class="nm">${esc(c.name)}</span><span class="role">${esc(c.role)}</span>`
    + (c.path && c.path.length ? `<div class="chain">${esc(c.path.join(' → '))}</div>` : '') + '</div>';
}
export function renderTrouble(a) {
  const cands = (a.data && a.data.candidates) || [];
  const head = cands.slice(0, TROUBLE_SHOWN_MAX).map(_candidateHTML).join('');
  const total = Math.max(cands.length, Number.isInteger(a.summary && a.summary.total) ? a.summary.total : 0);
  if (total <= TROUBLE_SHOWN_MAX) return head;
  const rest = cands.slice(TROUBLE_SHOWN_MAX);
  const label = `ほか ${total - TROUBLE_SHOWN_MAX} 件${rest.length ? 'を見る' : '（一覧には保存されていません）'}`;
  return head + (rest.length
    ? `<details class="cand-more"><summary>${esc(label)}</summary>${rest.map(_candidateHTML).join('')}</details>`
    : `<div class="muted cand-more">${esc(label)}</div>`);
}
// 出典の世代（資料フォルダ直下のフォルダ）と原本の更新時刻。取れた欄だけ「（5期・2026-07-01 12:30 更新）」の形で添える。
export function provenanceText(item) {
  const gen = item && typeof item.generation === 'string' ? item.generation.trim() : '';
  const at = item && typeof item.modified_at === 'string' ? fmtDateTime(item.modified_at) : '';
  const parts = [gen, at ? `${at} 更新` : ''].filter(Boolean);
  return parts.length ? `（${parts.join('・')}）` : '';
}
const provenanceHTML = (item) => {
  const t = provenanceText(item), note = item && item.note;
  return (t ? `<span class="muted cite-prov" title="資料フォルダ直下のフォルダと、原本の更新時刻">${esc(t)}</span>` : '') + (typeof note === 'string' && note ? `<span class="muted cite-prov">（${esc(note)}）</span>` : '');
};
// 引用（該当箇所）カード。既定は折りたたみで、件数だけ見える見出しボタン＋hidden な本体にする。
export function renderQa(a) {
  const cites = (a.data && a.data.citations) || [];
  if (!cites.length) return '';
  const items = cites.map((c) =>
    `<div class="cite"><span class="doc">${esc(c.doc_id)}</span>${provenanceHTML(c)}<span class="sp">行 ${esc(c.span && c.span[0])}–${esc(c.span && c.span[1])}</span><pre>${esc(c.quote)}</pre></div>`).join('');
  return `<div class="cites"><button class="cites-h" type="button" data-cites aria-expanded="false">`
    + `📄 該当箇所 (${cites.length})<span class="caret">▾</span></button>`
    + `<div class="cites-body" hidden>${items}</div></div>`;
}
// 出典（原本ダウンロードリンク）。0件でも明示する。
// 設計: docs/design/chat.md「1ターンの流れ」
export function renderSources(sources, verifiedDocIds, evidencePacket) {
  if (!sources || !sources.length) {
    return `<div class="sources"><div class="h">出典（原本をダウンロード）</div>`
      + '<span class="muted" style="font-size:var(--text-small)">確証のある資料は見つかりませんでした</span></div>';
  }
  // Evidence Packet の verification_method があれば doc_id ごとに検証バッジを添える
  const vmap = _verificationMethodByDoc(evidencePacket);
  // リンクが無い出典（保存されていない・安全に通せない）は href を空にせず、ダウンロードできないと分かる表示にする。
  const link = (s) => (s.download_url
    ? `<a href="${esc(s.download_url)}" data-dl>📄 ${esc(s.doc_id)}</a>`
    : `<span class="muted" title="この画面ではダウンロードできません">📄 ${esc(s.doc_id)}（ダウンロードできません）</span>`)
    + `${provenanceHTML(s)}${verificationBadgeHTML(vmap.get(s.doc_id))}`;
  // sources_verified（精読済み doc_id）があれば、出典を「根拠（精読済み）」と「参考（ヒットのみ）」に分ける（除外はしない）
  if (Array.isArray(verifiedDocIds)) {
    const verified = new Set(verifiedDocIds);
    const grounded = sources.filter((s) => verified.has(s.doc_id));
    const reference = sources.filter((s) => !verified.has(s.doc_id));
    const group = (label, items) => items.length
      ? `<div class="sources-group"><div class="sources-group-h">${esc(label)}</div>${items.map(link).join('')}</div>`
      : '';
    return `<div class="sources"><div class="h">出典（原本をダウンロード）</div>`
      + group('根拠（精読済み）', grounded) + group('参考（ヒットのみ）', reference) + '</div>';
  }
  return `<div class="sources"><div class="h">出典（原本をダウンロード）</div>${sources.map(link).join('')}</div>`;
}
// 新しい欄（answer.referenced_docs / impact_list / found_docs / how）。無い・壊れた欄（旧形式）は空として扱い、空の表や「影響なし」を作らない。
const IMPACT_STATE_LABEL = { candidate: '候補', inspected: '確かめた', used: '根拠に使った', unmapped: '対応不明' };
const _cnt = (v) => (Number.isInteger(v) && v > 0 ? v : 0);
const _isLine = (v) => Number.isInteger(v) && v > 0;
export function referencedDocRows(answer) {
  const list = answer && Array.isArray(answer.referenced_docs) ? answer.referenced_docs : [];
  const rows = list.filter((r) => r && typeof r.path === 'string' && r.path.trim()).map((r) => ({
    path: r.path, unopened: r.unopened === true, mentionedOnly: r.mentioned_only === true, rangesMore: _cnt(r.ranges_more),
    ranges: (Array.isArray(r.ranges) ? r.ranges : []).filter((x) => Array.isArray(x) && x.length === 2 && _isLine(x[0]) && _isLine(x[1]) && x[1] >= x[0]),
  }));
  return { rows, hidden: _cnt(answer && answer.referenced_docs_hidden), more: _cnt(answer && answer.referenced_docs_more) };
}
// 本文の中の資料名を、出典の欄でダウンロードできる資料へのリンクにする。対象は HTML の文字のノードだけ（`<a>`・`<pre>` の中は除く）。
// リンクにするのは、インラインコード・「」・『』の中身がそのまま資料名（パスか、出典の欄の中で一意なファイル名。後ろの行番号は除く）のものと、
// 囲まれていなくても出典の資料のパスとそのまま一致するものだけ（サーバー側の拾い方と同じ）。エスケープ済みの文字のまま置き換えるので、エスケープは変わらない。
export function linkDocsInBodyHTML(html, answer) {
  const { rows } = referencedDocRows(answer);
  const urls = new Map((Array.isArray(answer && answer.sources) ? answer.sources : [])
    .filter((s) => s && typeof s.doc_id === 'string' && typeof s.download_url === 'string' && s.download_url)
    .map((s) => [s.doc_id, s.download_url]));
  // ファイル名が一意かは、表示しきれなかった分を含む出典の資料全部で数える
  const baseCount = new Map();
  const allDocs = (Array.isArray(answer && answer.sources) ? answer.sources : []).filter((x) => x && typeof x.doc_id === 'string').map((x) => x.doc_id);
  new Set([...rows.map((r) => r.path), ...allDocs]).forEach((p) => { const b = p.split('/').pop(); baseCount.set(b, (baseCount.get(b) || 0) + 1); });
  // 「確認できなかった資料」に同じ名前がある（範囲の中で名前が重なる）ファイル名は、ファイル名だけではリンクにしない。
  // 確認できなかった資料に表示しきれなかった分があるときは、ファイル名だけのリンクはどれも付けない
  (Array.isArray(answer && answer.sources_unverified) ? answer.sources_unverified : [])
    .filter((u) => u && typeof u.path === 'string').forEach((u) => { const b = u.path.split('/').pop(); baseCount.set(b, (baseCount.get(b) || 0) + 2); });
  const unverifiedMore = Number.isInteger(answer && answer.sources_unverified_more) && answer.sources_unverified_more > 0;
  const targets = new Map();
  rows.forEach((r) => {
    if (!urls.has(r.path)) return;
    const base = r.path.split('/').pop();
    targets.set(esc(r.path), urls.get(r.path));
    if (!unverifiedMore && baseCount.get(base) === 1 && !targets.has(esc(base))) targets.set(esc(base), urls.get(r.path));
  });
  if (!targets.size) return html;
  const lookup = (name) => targets.get(name.trim().replace(/:\d+(?:-\d+)?(?:,\s*\d+(?:-\d+)?)*\s*$/, ''));
  const link = (name, url) => `<a href="${esc(url)}" data-dl>${name}</a>`;
  const pathAlts = [...targets.keys()].filter((t) => t.includes('/')).sort((a, b) => b.length - a.length)
    .map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  const re = new RegExp('(「|『)([^「」『』]+)(」|』)' + (pathAlts.length ? '|(' + pathAlts.join('|') + ')' : ''), 'g');
  // 囲まれていないパスは、英数字・`/`・`\`・`:`・`@`・`=`・`?`・`&`・`#` の続き（長いパス・URL の一部）や、名前の続きの前ではリンクにしない
  const badBefore = (ch) => !!ch && /[A-Za-z0-9_.\-\/\\:@=?&#\uFF10-\uFF19\uFF21-\uFF3A\uFF41-\uFF5A]/.test(ch);
  const charBefore = (tok, at) => (tok.slice(0, at).endsWith('&amp;') ? '&' : tok[at - 1]);
  const badAfter = (rest) => /^(?:&amp;|[A-Za-z0-9_\-\/\\@?#=\uFF10-\uFF19\uFF21-\uFF3A\uFF41-\uFF5A]|\.[A-Za-z0-9]|:(?!\d))/.test(rest);
  let skip = 0;
  let code = 0;
  return String(html).replace(/<[^>]*>|[^<]+/g, (tok) => {
    if (tok[0] === '<') {
      const m = /^<(\/?)(a|pre|code)(?=[\s>\/])/i.exec(tok);
      if (m && m[2].toLowerCase() === 'code') code = Math.max(0, code + (m[1] ? -1 : 1));
      else if (m) skip = Math.max(0, skip + (m[1] ? -1 : 1));
      return tok;
    }
    if (skip) return tok;
    if (code) {
      const url = lookup(tok);
      return url ? tok.replace(tok.trim(), (name) => link(name, url)) : tok;
    }
    return tok.replace(re, (all, open, inner, close, path, ...rest) => {
      if (open) {
        const url = lookup(inner);
        return url ? `${open}${link(inner, url)}${close}` : all;
      }
      const at = rest[rest.length - 2];
      return badBefore(charBefore(tok, at)) || badAfter(tok.slice(at + path.length)) ? all : link(path, targets.get(path));
    });
  });
}
export function impactListView(answer) {
  const s = answer && answer.impact_list;
  if (!s || typeof s !== 'object' || typeof s.traced !== 'boolean') return null;
  const rows = (Array.isArray(s.rows) ? s.rows : []).filter((r) => r && typeof r.name === 'string' && r.name.trim() && IMPACT_STATE_LABEL[r.state])
    .map((r) => ({ name: r.name, path: typeof r.path === 'string' ? r.path : '', origin: r.role === 'origin',
      state: r.state, label: IMPACT_STATE_LABEL[r.state], reason: typeof r.reason === 'string' ? r.reason : '' }));
  const reasons = (Array.isArray(s.reasons) ? s.reasons : []).filter((x) => typeof x === 'string' && x.trim());
  return { traced: s.traced, rows, reasons, more: _cnt(s.more), hidden: _cnt(s.hidden) };
}
export function foundDocRows(answer) {
  const list = answer && Array.isArray(answer.found_docs) ? answer.found_docs : [];
  return { rows: list.filter((r) => r && typeof r.path === 'string' && r.path.trim()).map((r) => r.path),
    hidden: _cnt(answer && answer.found_docs_hidden), more: _cnt(answer && answer.found_docs_more) };
}
export function howText(answer) {
  const h = answer && answer.how;
  if (!h || typeof h !== 'object') return '';
  const base = h.mode === 'author' ? '作る' : h.mode === 'investigate' ? '調べる' : '';
  return base ? (h.doc_focus === true && h.mode === 'investigate' ? '資料中心の指定で調べました' : base) : '';
}
export const MENTIONED_ONLY_NOTE = '（本文で名前が出た資料・読み取り機能で開いた記録なし）';
// 本文で名前が出ただけの資料の印。開いた行の範囲がある行（下調べ役が開いた資料など）では開いた記録なしと言わない。
export const mentionedOnlyNote = (r) => (r.ranges.length || r.rangesMore ? '（本文で名前が出た資料）' : MENTIONED_ONLY_NOTE);
export const rangeText = (r) => r.ranges.map((x) => (x[0] === x[1] ? `行${x[0]}` : `行${x[0]}-${x[1]}`)).join('、');
// 新しい形の回答（`referenced_docs` の欄がある）。無い古い回答は出典の 2 群（`renderSources`）と該当箇所（`renderQa`）で出す。
export const hasReferencedDocs = (answer) => !!answer && Array.isArray(answer.referenced_docs);
// 参照した資料（原本をダウンロード）。資料ごとに原本のリンク・読み取り機能で開いた行の範囲・中身を確かめていない注記を出す。
// 設計: docs/design/chat.md「1ターンの流れ」
export function renderReferencedDocs(answer, evidencePacket) {
  const { rows, hidden, more } = referencedDocRows(answer);
  const head = '<div class="sources referenced-docs"><div class="h">参照した資料（原本をダウンロード）</div>';
  if (!rows.length && !hidden && !more) return `${head}<span class="muted" style="font-size:var(--text-small)">参照した資料はありません</span></div>`;
  const vmap = _verificationMethodByDoc(evidencePacket);
  const byDoc = new Map((Array.isArray(answer.sources) ? answer.sources : []).filter((s) => s && typeof s.doc_id === 'string').map((s) => [s.doc_id, s]));
  const link = (r) => {
    const s = byDoc.get(r.path) || { doc_id: r.path };
    return (s.download_url
      ? `<a href="${esc(s.download_url)}" data-dl>📄 ${esc(r.path)}</a>`
      : `<span class="muted" title="この画面ではダウンロードできません">📄 ${esc(r.path)}（ダウンロードできません）</span>`)
      + `${provenanceHTML(s)}${verificationBadgeHTML(vmap.get(r.path))}`;
  };
  return head
    + rows.map((r) => `<div class="sources-unverified-row">${link(r)}${r.ranges.length ? `<span class="muted">（読み取り機能で開いた: ${esc(rangeText(r))}${r.rangesMore ? `、ほか ${r.rangesMore} か所` : ''}）</span>` : (r.rangesMore ? `<span class="muted">（読み取り機能で開いた: ほか ${r.rangesMore} か所）</span>` : '')}`
      + `${r.unopened && !r.mentionedOnly ? '<span class="muted">（読み取り機能で開いた記録なし）</span>' : ''}`
      + `${r.mentionedOnly ? `<span class="muted">${mentionedOnlyNote(r)}</span>` : ''}</div>`).join('')
    + (more ? `<div class="sources-unverified-row muted">ほか ${more} 件</div>` : '')
    + (hidden ? `<div class="sources-unverified-row muted">名前を表示できない資料 ${hidden} 件</div>` : '') + '</div>';
}
export function renderImpactList(answer) {
  const v = impactListView(answer);
  if (!v) return '';
  const notes = v.reasons.map((x) => `<div class="sources-unverified-row muted">${esc(x)}</div>`).join('');
  if (!v.traced) {
    return '<div class="sources impact-list"><div class="h">影響一覧</div><div class="muted">影響をたどっていません</div>' + notes + '</div>';
  }
  const body = v.rows.map((r) => `<tr class="impact-${r.state}"><td data-label="名前">${esc(r.name)}${r.origin ? '<span class="impact-origin">起点</span>' : ''}${r.path ? `<div class="muted">${esc(r.path)}</div>` : ''}</td>`
    + `<td data-label="状態"><span class="impact-state">${esc(r.label)}</span></td><td data-label="理由">${esc(r.reason)}</td></tr>`).join('');
  const affected = v.rows.filter((r) => !r.origin).length;
  const empty = !affected && !v.more ? `<div class="muted">${v.reasons.length ? '影響先は見つかっていません（すべてをたどれたとは言えません）' : 'グラフでたどれる影響先は見つかりませんでした'}</div>` : '';
  return '<div class="sources impact-list"><div class="h">影響一覧</div>'
    + (v.rows.length ? `<table class="recon-table"><thead><tr><th>名前</th><th>状態</th><th>理由</th></tr></thead><tbody>${body}</tbody></table>` : '')
    + empty + notes
    + (v.more ? `<div class="sources-unverified-row muted">ほか ${v.more} 件</div>` : '')
    + (v.hidden ? `<div class="sources-unverified-row muted">名前を表示できない項目 ${v.hidden} 件</div>` : '') + '</div>';
}
export function renderFoundDocs(answer) {
  const { rows, hidden, more } = foundDocRows(answer);
  if (!rows.length && !hidden && !more) return '';
  return '<details class="sources found-docs"><summary class="h">ほかに見つかった資料（読み取り機能で開いた記録なし）</summary>'
    + rows.map((p) => `<div class="sources-unverified-row">${esc(p)}</div>`).join('')
    + (more ? `<div class="sources-unverified-row muted">ほか ${more} 件（上限で省略）</div>` : '')
    + (hidden ? `<div class="sources-unverified-row muted">名前を表示できない資料 ${hidden} 件</div>` : '') + '</details>';
}
export function howChipHTML(answer) {
  const t = howText(answer);
  return t ? `<span class="chip">${esc(t)}</span>` : '';
}
// 設計書とソースの照らし合わせ（answer.data.reconciliation）。Codex が両方を確かめた項目だけ。旧形式・壊れた行は出さない。
const RECON_VERDICT_LABEL = { match: '一致', mismatch: '食い違い（ソースが正）', spec_missing: '設計書に記述なし',
  source_missing: 'ソースに見当たらない', unverified: '未確認' };
export function reconciliationRows(answer) {
  const list = answer && answer.data && Array.isArray(answer.data.reconciliation) ? answer.data.reconciliation : [];
  const ref = (v) => {
    const o = { doc_id: v && typeof v.doc_id === 'string' && v.doc_id ? v.doc_id : '',
      loc: v && typeof v.loc === 'string' ? v.loc : '',
      line: v && Number.isInteger(v.line) && v.line > 0 ? v.line : 0,
      lineEnd: v && Number.isInteger(v.line) && v.line > 0 && Number.isInteger(v.line_end) && v.line_end > v.line ? v.line_end : 0 };
    const pos = o.loc || (o.line ? `${o.line}${o.lineEnd ? `-${o.lineEnd}` : ''}` : '');
    o.label = o.doc_id + (pos ? `:${pos}` : '');
    return o;
  };
  const side = (v) => ({ text: v && typeof v.text === 'string' ? v.text : '', ...ref(v),
    also: v && Array.isArray(v.also) ? v.also.map(ref).filter((x) => x.doc_id) : [] });
  return list.filter((r) => r && typeof r.item === 'string' && r.item.trim() && RECON_VERDICT_LABEL[r.verdict])
    .map((r) => ({ item: r.item, verdict: r.verdict, label: RECON_VERDICT_LABEL[r.verdict], spec: side(r.spec), source: side(r.source) }));
}
function _reconLinkHTML(sd, world) {
  const fileName = sd.doc_id.split('/').filter(Boolean).pop() || 'download';
  return world
    ? `<a href="/documents/download?world=${encodeURIComponent(world)}&rel=${encodeURIComponent(sd.doc_id)}" data-dl data-name="${esc(fileName)}">📄 ${esc(sd.label)}</a>`
    : `<span class="muted">📄 ${esc(sd.label)}</span>`;
}
function _reconRefHTML(sd, world) {
  if (!sd.doc_id) return '';
  return _reconLinkHTML(sd, world) + (sd.also || []).map((x) => `<div>${_reconLinkHTML(x, world)}</div>`).join('');
}
export function renderReconciliation(answer) {
  const rows = reconciliationRows(answer);
  if (!rows.length) return '';
  const world = answer.scope && typeof answer.scope.world === 'string' ? answer.scope.world : '';
  const cell = (sd, empty) => `${sd.text ? `<div>${esc(sd.text)}</div>` : (empty ? `<div class="muted">${empty}</div>` : '')}${_reconRefHTML(sd, world)}`;
  const body = rows.map((r) => `<tr class="recon-${r.verdict}">`
    + `<td data-label="項目">${esc(r.item)}</td>`
    + `<td data-label="設計書の記述">${cell(r.spec, r.verdict === 'spec_missing' ? '記述なし' : '')}</td>`
    + `<td data-label="ソースの実装">${cell(r.source, r.verdict === 'source_missing' ? '見当たらない' : '')}</td>`
    + `<td data-label="判定"><span class="recon-verdict">${esc(r.label)}</span></td></tr>`).join('');
  return '<div class="sources reconcile"><div class="h">設計書とソースの照らし合わせ</div>'
    + '<table class="recon-table"><thead><tr><th>項目</th><th>設計書の記述</th><th>ソースの実装</th><th>判定</th></tr></thead>'
    + `<tbody>${body}</tbody></table></div>`;
}
// 出典の下の「確認できなかった資料」（answer.sources_unverified: [{path, reason}]）。秘匿の資料は名前を出さず件数だけ。
export function unverifiedSourceRows(answer) {
  const list = answer && Array.isArray(answer.sources_unverified) ? answer.sources_unverified : [];
  const rows = list.filter((u) => u && typeof u.path === 'string' && u.path.trim())
    .map((u) => ({ path: u.path, reason: typeof u.reason === 'string' ? u.reason : '' }));
  const num = (v) => (Number.isInteger(v) && v > 0 ? v : 0);
  return { rows, hidden: num(answer && answer.sources_unverified_hidden), more: num(answer && answer.sources_unverified_more) };
}
export function renderSourcesUnverified(answer) {
  const { rows, hidden, more } = unverifiedSourceRows(answer);
  if (!rows.length && !hidden && !more) return '';
  return '<div class="sources sources-unverified"><div class="h">確認できなかった資料</div>'
    + rows.map((u) => `<div class="sources-unverified-row">${esc(u.path)}${u.reason ? `<span class="muted">（${esc(u.reason)}）</span>` : ''}</div>`).join('')
    + (more ? `<div class="sources-unverified-row muted">ほか ${more} 件</div>` : '')
    + (hidden ? `<div class="sources-unverified-row muted">名前を表示できない資料 ${hidden} 件</div>` : '')
    + '</div>';
}
// 調査台帳が保存された回答にだけ、Markdown のダウンロード導線を出す。href は chat.js の data-investigation-dl 委譲ハンドラがクリック時に組み立てる。
export function renderInvestigationRecord(investigation) {
  if (!investigation || !investigation.recorded) return '';
  return '<div class="sources investigation-record"><div class="h">調査の記録</div>'
    + '<a href="#" data-investigation-dl>📋 Markdown でダウンロード</a></div>';
}

