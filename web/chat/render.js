// チャットの描画。回答カード、右ペインの思考の流れ（trace・ターンの積み上げ）、welcome、出典・根拠の表示を担う。
// 設計: docs/design/chat.md「1ターンの流れ」
// 他モジュール（history.js・stream.js・chat.js）から呼ばれる葉モジュール。stream.js の setRt とは hoist を前提に相互 import する。
'use strict';

import { S, EXAMPLES } from './state.js';
import { setRt } from './stream.js';

const $ = Sherpa.$, esc = Sherpa.esc, fmtDateTime = Sherpa.fmtDateTime, mdLite = Sherpa.mdLite, analyzerLabel = Sherpa.analyzerLabel;

const LENS_LABEL = { impact: '影響範囲分析', troubleshoot: 'トラブルシュート', qa: '仕様問い合わせ', author: '資料を作成' };
const STATUS_CLASS = { deprecated: 'deprecated', hidden_candidate: 'hidden_candidate' };
const STATUS_LABEL = { deprecated: '廃止', hidden_candidate: '未使用の疑い' };

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
    '<div class="headline">ようこそ Sherpa へ</div>'
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

// ===== trace / turn stack（右ペイン） =====
// ツール detail の「」内のクエリをチップに分けて描く（「」が無ければ素のテキスト）。
export function _renderDetail(elDetail, e) {
  const d = e.detail || '';
  const m = (e.kind === 'tool') ? d.match(/「([^」]*)」/) : null;
  if (m && m[1]) {
    const rest = (d.slice(0, m.index) + d.slice(m.index + m[0].length)).trim();
    elDetail.innerHTML = `<span class="fchip">${esc(m[1])}</span>`
      + (rest ? `<span class="frest">${esc(rest)}</span>` : '');
  } else {
    elDetail.textContent = d;
  }
}
// 確定済み trace のステップ群を container に静的に描く（v1）。
function _renderTraceSteps(container, trace) {
  (trace || []).forEach((e) => {
    const el = document.createElement('div');
    el.className = 'fstep done' + (e.kind === 'tool' ? ' tool' : '');
    el.innerHTML = '<div class="fnode">✓</div><div class="fbody">'
      + '<div class="fhead"><div class="flabel"></div></div><div class="fdetail"></div></div>';
    el.querySelector('.flabel').textContent = e.label || '';
    _renderDetail(el.querySelector('.fdetail'), e);
    container.appendChild(el);
  });
}
// ===== trace_version=2 の階層描画 =====
// 葉ノード（.fstep）は v1 と同じ描画を再利用し、階層（担当ごとのレーン・集約）を v2 専用の関数で組む。
const AGENT_MAIN = 'main';           // agent_run_id が無い（null/undefined/空）＝メイン run
const AGG_MIN_RUN = 3;               // 同種操作をこの件数以上ぶら下げたら集約表示に畳む

function agentKeyOf(e) {
  const a = e && e.agent_run_id;
  return (a === null || a === undefined || a === '') ? AGENT_MAIN : String(a);
}
function parentAgentKeyOf(e) {
  const a = e && e.parent_agent_run_id;
  return (a === null || a === undefined || a === '') ? AGENT_MAIN : String(a);
}
// agent_run_id（sub:{profile_id}:{seq}）から profile_id を取り出す（担当表示の最終フォールバック。metrics.name があればそちらを優先する）。
function agentProfileOf(key) {
  const m = /^sub:([^:]+):\d+$/.exec(key || '');
  return m ? m[1] : key;
}
function _humanizeProfile(profile) {
  const cleaned = String(profile || '').replace(/^search-helper-/, '').replace(/[-_]+/g, ' ').trim();
  return cleaned || '下調べ';
}
const AGENT_STATUS_LABEL = { active: '実行中', done: '完了', failed: '失敗', cancelled: '取消', aborted: '中断' };
function agentStatusChipHTML(status) {
  const s = AGENT_STATUS_LABEL[status] ? status : 'active';
  return `<span class="fagent-status ${s}">${esc(AGENT_STATUS_LABEL[s])}</span>`;
}
// 担当バッジの配置ラベル。サーバの is_local（local/on_prem/cloud/cloud_compat）をそのまま表示し、フロントでは推測しない。
// 既知の値以外は「担当不明」と表示する。Object.create(null) は継承プロパティの誤参照を避けるため。
const LOCALITY_LABEL = Object.assign(Object.create(null), {
  local: 'ローカル', on_prem: '社内サーバ', cloud: 'クラウド', cloud_compat: 'クラウド（OpenAI 互換）',
});
// バッジの配色（cloud_compat は cloud と同じ）
const _LOCALITY_BADGE_CLASS = Object.assign(Object.create(null), {
  local: 'local', on_prem: 'on_prem', cloud: 'cloud', cloud_compat: 'cloud',
});
function providerBadgeHTML(provider, model, locality) {
  if (!provider) return '';
  const who = LOCALITY_LABEL[locality];
  const cls = who ? _LOCALITY_BADGE_CLASS[locality] : 'unknown';
  const label = model ? `${who || '担当不明'}: ${model}` : (who || '担当不明');
  // 切り詰め表示でも全文が見えるよう title に入れる
  return `<span class="provider-badge ${cls}" title="${esc(label)}">${esc(label)}</span>`;
}
// 検証バッジ: Evidence Packet の verification_method → 表示ラベル（Object.create(null) は継承プロパティの誤参照を避けるため）。
const VERIFICATION_BADGE_LABEL = Object.assign(Object.create(null), {
  span_verified: '機械検証済み（該当箇所一致）',
  exists_no_span: '機械検証済み（実在確認）',
  list_docs_verified: '機械検証済み（一覧確認）',
  graph_verified: '機械検証済み（グラフ）',
  graph_node_verified: '機械検証済み（グラフ）',
  span_unmatched: '要確認（該当箇所不一致）',
});
function verificationBadgeHTML(method) {
  if (!method) return '';
  const label = VERIFICATION_BADGE_LABEL[method];
  // 未知の verification_method は「検証済み」と断定せず「検証方法不明」を出す
  if (!label) return ` <span class="verif-badge unknown">${esc('検証方法不明')}</span>`;
  const cls = method === 'span_unmatched' ? 'warn' : 'ok';
  return ` <span class="verif-badge ${cls}">${esc(label)}</span>`;
}
// 登録者重要度バッジ。出典の importance が「高」「低」のときだけ表示する（「中」・未設定は出さない）。importance_reason があれば title に添える。
const IMPORTANCE_BADGE_LABEL = { '高': '登録者重要度：高', '低': '登録者重要度：低' };
function importanceBadgeHTML(source) {
  const v = source && source.importance;
  if (v !== '高' && v !== '低') return '';
  const cls = v === '高' ? 'high' : 'low';
  const title = source.importance_reason ? ` title="${esc(source.importance_reason)}"` : '';
  return ` <span class="importance-badge ${cls}"${title}>${esc(IMPORTANCE_BADGE_LABEL[v])}</span>`;
}
// doc ごとの verification_method を集める。source_path に加え、集計 Evidence の matched_doc_ids も見る。
function _verificationMethodByDoc(evidencePacket) {
  const map = new Map();
  const list = (evidencePacket && Array.isArray(evidencePacket.evidence)) ? evidencePacket.evidence : [];
  for (const ev of list) {
    if (!ev) continue;
    const method = ev.verification_method || null;
    const used = !!ev.used;
    const setDoc = (doc) => {
      if (!doc || typeof doc !== 'string') return;
      if (!map.has(doc) || used) map.set(doc, method);   // used 側を優先
    };
    setDoc(ev.source_path);
    if (Array.isArray(ev.matched_doc_ids)) ev.matched_doc_ids.forEach(setDoc);
  }
  return map;
}

// ---- 集約（同種操作を件数で畳む） ----
const EVENT_TYPE_AGG_LABEL = { candidate_discovered: '候補', candidate_verified: '精読',
                               candidate_rejected: '却下', evidence_committed: '採用' };
function _bucketKeyFor(e) {
  if (e.kind === 'tool') return 'tool:' + (e.label || '');
  if (e.event_type && EVENT_TYPE_AGG_LABEL[e.event_type]) return 'ev:' + e.event_type;
  return null;   // think/agent/evaluation/hook 等は常に個別表示（milestone を隠さない）
}
function _bucketLabelFor(e, key) {
  return key.startsWith('tool:') ? (e.label || '調べる操作の回数') : (EVENT_TYPE_AGG_LABEL[e.event_type] || e.event_type);
}

// ---- 葉ノード（.fstep）: v1 の見た目に kind/status の拡張・担当バッジを加えたもの ----
function _fstepClassV2(e) {
  const status = AGENT_STATUS_LABEL[e.status] ? e.status : (e.status === 'active' ? 'active' : 'done');
  const kind = e.kind || 'think';
  return 'fstep ' + status + (kind !== 'think' ? ' ' + kind : '');
}
function _currentOpText(e) {   // 「AI が考えています」の代わりに出す「今なにをしているか」の短文
  if (!e) return '';
  const d = e.detail || '';
  const m = d.match(/「([^」]*)」/);
  return (e.label || '') + (m && m[1] ? `: ${m[1]}` : '');
}
function _buildLeafElV2(e) {
  const el = document.createElement('div');
  el.className = _fstepClassV2(e);
  // .fchildren は parent_id で子に指定されたノードの入れ子先
  el.innerHTML = '<div class="fnode"></div><div class="fbody"><div class="fhead"><div class="flabel"></div></div>'
    + '<div class="fdetail"></div><div class="fchildren"></div></div>';
  _updateLeafElV2(el, e);
  return el;
}
function _updateLeafElV2(el, e) {
  el.className = _fstepClassV2(e);
  el.querySelector('.flabel').textContent = e.label || '';
  const head = el.querySelector('.fhead');
  const badge = head.querySelector('.provider-badge');
  const m = e.metrics;
  const badgeHTML = (m && m.provider) ? providerBadgeHTML(m.provider, m.model, _normalizeLocality(m.is_local)) : '';
  if (badgeHTML) { if (badge) badge.outerHTML = badgeHTML; else head.insertAdjacentHTML('beforeend', badgeHTML); }
  else if (badge) { badge.remove(); }
  _renderDetail(el.querySelector('.fdetail'), e);
  el.querySelector('.fnode').textContent =
    e.status === 'done' ? '✓' : e.status === 'failed' ? '✕' : e.status === 'cancelled' ? '–' : '';
}
// is_local は4値＋null。それ以外は「担当不明」（null）に寄せる。
function _normalizeLocality(v) { return LOCALITY_LABEL[v] ? v : null; }

function _freshLaneStats() {
  return { cycles: 0, toolCalls: 0, candidates: 0, evidenceIds: new Set(), evidenceCount: 0,
          hasCandidateSignal: false, hasEvidenceSignal: false,
          tokens: 0, elapsedMs: 0, status: 'active', stopReason: '', skill: null,
          startedLabel: null, firstLabel: null, serverName: null,
          provider: null, model: null, locality: null };
}
function _updateLaneStats(stats, e) {
  if (stats.firstLabel == null && e.label) stats.firstLabel = e.label;
  const et = e.event_type;
  if (et === 'agent_started') stats.startedLabel = e.label;
  if (et === 'evaluation_completed') stats.cycles++;
  if (et === 'tool_started' || (e.kind === 'tool' && !et)) stats.toolCalls++;
  // 候補/根拠のイベントを1回でも見たレーンだけ件数を出す（未計測を「0件」と見せない）
  if (et === 'candidate_discovered' || et === 'candidate_verified' || et === 'candidate_rejected') {
    stats.hasCandidateSignal = true;
    if (et === 'candidate_discovered') stats.candidates++;
  }
  if (Array.isArray(e.evidence_ids) && e.evidence_ids.length) {
    stats.hasEvidenceSignal = true;
    e.evidence_ids.forEach((id) => stats.evidenceIds.add(id));
  }
  if (et === 'evidence_committed') {
    stats.hasEvidenceSignal = true;
    if (!(e.evidence_ids && e.evidence_ids.length)) stats.evidenceCount++;
  }
  const m = e.metrics;
  if (m && typeof m === 'object') {
    if (typeof m.tokens === 'number') stats.tokens += m.tokens;
    if (typeof m.elapsed_ms === 'number') stats.elapsedMs = Math.max(stats.elapsedMs, m.elapsed_ms);
    if (typeof m.skill === 'string' && !stats.skill) stats.skill = m.skill;
    if (typeof m.stop_reason === 'string' && m.stop_reason && !stats.stopReason) stats.stopReason = m.stop_reason;
    if (m.provider && !stats.provider) {
      stats.provider = m.provider; stats.model = m.model || null; stats.locality = _normalizeLocality(m.is_local);
    }
    // サーバの表示名があれば内部 slug（profile_id）より優先する
    if (typeof m.name === 'string' && m.name && !stats.serverName) stats.serverName = m.name;
  }
  if (et === 'agent_completed') stats.status = 'done';
  else if (et === 'agent_failed') { stats.status = 'failed'; if (!stats.stopReason) stats.stopReason = e.detail || ''; }
  else if (et === 'agent_cancelled') { stats.status = 'cancelled'; if (!stats.stopReason) stats.stopReason = e.detail || ''; }
}
function _fmtElapsedV2(ms) { return (ms / 1000).toFixed(ms < 10000 ? 1 : 0) + 's'; }
// trace_version=2 の階層描画。ストリーミング（1件ずつ addOrUpdate）と履歴復元（配列を順に addOrUpdate）で共通に使う。
// id で重複を除き、agent_run_id ごとにレーン（.fagent の入れ子 details）を作り、同種操作は件数で集約する。
// live=true のときだけ「考え中」表示と経過時間のカウントアップを行う。
export class TraceTreeV2 {
  constructor(container, { live = false, startedAtMs = 0 } = {}) {
    this.container = container;
    this.live = live;
    this.lanes = new Map();
    this.nodesById = new Map();
    // 親がまだ届いていない子の待ち registry（parentId -> Set<childId>）。親が届いたら _attachPendingChildren が付け替える
    this._pendingChildren = new Map();
    // レーン直下の要素の到着順の通し番号（_insertBySeq が兄弟位置を決めるのに使う）
    this._seq = 0;
    this._destroyed = false;
    this._stopNoteEl = null;   // 終了理由の note（correctStopReason が訂正する）
    const main = { key: AGENT_MAIN, parentKey: null, headerEl: null, opEl: null, bodyEl: container,
                  buckets: new Map(), stats: _freshLaneStats(), startedAt: startedAtMs || Date.now(),
                  lastEventAt: Date.now(), activeOpText: '' };
    main.stats.status = 'active';
    this.lanes.set(AGENT_MAIN, main);
    if (live) {
      const think = document.createElement('div');
      think.className = 'fthinking muted';
      container.appendChild(think);
      main.opEl = think;
      this._tick();
      this._tickTimer = setInterval(() => this._tick(), 1000);
    }
  }
  destroy() { if (this._tickTimer) clearInterval(this._tickTimer); this._destroyed = true; }
  // ターン終端で呼ぶ。① ティックを止める ② 実行中のレーンを畳む（interrupted なら「中断」・それ以外は「完了」）
  // ③ 終了理由の note を出す。stopInfo は {text, interrupted}（省略は完了扱い・note なし）。
  finalize(stopInfo) {
    if (this._tickTimer) { clearInterval(this._tickTimer); this._tickTimer = null; }
    const interrupted = !!(stopInfo && stopInfo.interrupted);
    this.lanes.forEach((lane) => {
      if (lane.stats.status === 'active') lane.stats.status = interrupted ? 'aborted' : 'done';
      if (lane.key !== AGENT_MAIN) this._renderLaneHeader(lane);
      if (lane.opEl) { lane.opEl.remove(); lane.opEl = null; }
    });
    if (stopInfo && stopInfo.text) {
      const note = document.createElement('div');
      note.className = 'ftrace-stopreason muted';
      note.textContent = `終了理由: ${stopInfo.text}`;
      this.container.appendChild(note);
      this._stopNoteEl = note;
    }
  }
  // 暫定表示した終了理由 note の文言だけを訂正する（停止 POST が失敗と分かったとき stream.js が呼ぶ。レーン状態は変えない）。
  correctStopReason(stopInfo) {
    if (this._stopNoteEl) {
      if (stopInfo && stopInfo.text) this._stopNoteEl.textContent = `終了理由: ${stopInfo.text}`;
      else { this._stopNoteEl.remove(); this._stopNoteEl = null; }
    } else if (stopInfo && stopInfo.text) {
      const note = document.createElement('div');
      note.className = 'ftrace-stopreason muted';
      note.textContent = `終了理由: ${stopInfo.text}`;
      this.container.appendChild(note);
      this._stopNoteEl = note;
    }
  }
  _tick() {
    if (this._destroyed) return;
    const now = Date.now();
    this.lanes.forEach((lane) => {
      if (lane.stats.status !== 'active') return;
      // 再購読の replay ではイベント側 elapsed_ms が通算値を持つ＝ローカル起点との大きい方を採る
      lane.stats.elapsedMs = Math.max(lane.stats.elapsedMs, now - lane.startedAt);
      if (lane.key === AGENT_MAIN) {
        if (lane.opEl) lane.opEl.textContent = this._thinkingText(now, lane);
      } else {
        this._renderLaneHeader(lane);
      }
    });
  }
  _thinkingText(now, lane) {
    if (lane.activeOpText) return lane.activeOpText;
    const secs = Math.max(0, Math.round((now - lane.lastEventAt) / 1000));
    return `AI が考えています（${secs}秒）`;
  }
  _ensureLane(key, parentKey) {
    if (this.lanes.has(key)) return this.lanes.get(key);
    const parentLane = this.lanes.get(parentKey) || this.lanes.get(AGENT_MAIN);
    const det = document.createElement('details');
    det.className = 'fagent'; det.open = true;
    det.innerHTML = '<summary class="fagent-head"></summary><div class="fagent-body"></div>';
    det._seq = this._seq++;
    parentLane.bodyEl.appendChild(det);
    const lane = { key, parentKey: parentLane.key, rootEl: det, headerEl: det.querySelector('.fagent-head'),
                  opEl: null, bodyEl: det.querySelector('.fagent-body'), buckets: new Map(),
                  stats: _freshLaneStats(), startedAt: Date.now(), lastEventAt: Date.now(), activeOpText: '' };
    if (this.live) {
      const op = document.createElement('div'); op.className = 'fagent-op muted';
      det.insertBefore(op, det.querySelector('.fagent-body'));
      lane.opEl = op;
    }
    this.lanes.set(key, lane);
    this._renderLaneHeader(lane);
    return lane;
  }
  _renderLaneHeader(lane) {
    const s = lane.stats;
    // 担当名はサーバの表示名（metrics.name）を優先し、無ければ profile_id を平文化する
    const role = s.serverName || _humanizeProfile(agentProfileOf(lane.key));
    const name = s.startedLabel || s.firstLabel || role;
    const bits = [];
    bits.push(`<span class="fagent-name">${esc(name)}</span>`);
    bits.push(`<span class="fagent-role">${esc(role)}</span>`);
    if (s.provider) bits.push(providerBadgeHTML(s.provider, s.model, s.locality));
    bits.push(agentStatusChipHTML(s.status));
    if (s.skill) bits.push(`<span class="fagent-stat">得意分野: ${esc(s.skill)}</span>`);
    bits.push(`<span class="fagent-stat">調査の回数 ${s.cycles}</span>`);
    bits.push(`<span class="fagent-stat">調べる操作の回数 ${s.toolCalls}</span>`);
    // 候補/根拠は、イベントを1回でも見たレーンだけ出す（未計測を0件と見せない）
    if (s.hasCandidateSignal) bits.push(`<span class="fagent-stat">候補 ${s.candidates}</span>`);
    if (s.hasEvidenceSignal) {
      const evCount = s.evidenceIds.size || s.evidenceCount;
      bits.push(`<span class="fagent-stat">根拠 ${evCount}</span>`);
    }
    if (s.elapsedMs) bits.push(`<span class="fagent-stat">${_fmtElapsedV2(s.elapsedMs)}</span>`);
    if (s.tokens) bits.push(`<span class="fagent-stat">🪙 ${s.tokens.toLocaleString()}</span>`);
    if (s.stopReason) bits.push(`<span class="fagent-stopreason">${esc(s.stopReason)}</span>`);
    lane.headerEl.innerHTML = bits.join('');
    if (lane.opEl) lane.opEl.textContent = s.status === 'active' ? this._thinkingText(Date.now(), lane) : '';
  }
  // ノードの子の入れ子先（.fbody > .fchildren）
  _childrenContainerOf(entry) {
    return entry.el.querySelector(':scope > .fbody > .fchildren');
  }
  // entry を集約バケットの帳簿（count/leafEls/表示件数）から外す。要素を別の親へ移す前に必ず呼ぶ。
  // 0件になったバケットは削除し、件数が AGG_MIN_RUN 未満になったら集約枠を解体して個別表示へ戻す。
  _detachFromBucket(entry) {
    const key = entry.bucketKey;
    if (!key) return;
    entry.bucketKey = null;
    const b = entry.lane.buckets.get(key);
    if (!b) return;
    if (b.leafEls) {
      const idx = b.leafEls.indexOf(entry.el);
      if (idx !== -1) b.leafEls.splice(idx, 1);
    }
    b.count = Math.max(0, b.count - 1);
    if (b.count <= 0) {
      if (b.aggEl) b.aggEl.remove();
      entry.lane.buckets.delete(key);
      return;
    }
    if (b.aggEl && b.count < AGG_MIN_RUN) {
      // 集約枠を解体し、残りの要素を到着順の位置へ個別に戻す（detach 対象自身は除く）
      const remaining = Array.from(b.aggBody.children).filter((x) => x !== entry.el);
      b.aggEl.remove();
      remaining.forEach((x) => this._insertBySeq(entry.lane.bodyEl, x));
      b.aggEl = null;
      b.aggBody = null;
      b.leafEls = remaining;
      return;
    }
    if (b.aggEl) {
      // 枠は存続する。残存メンバーの最古の _seq へ枠の順序を更新し、正しい兄弟位置へ移し直す（全メンバーは _seq を持つ契約）
      const remainingSeqs = Array.from(b.aggBody.children)
        .filter((x) => x !== entry.el)
        .map((x) => x._seq);
      if (!remainingSeqs.length || remainingSeqs.some((s) => typeof s !== 'number')) {
        throw new Error('TraceTreeV2._detachFromBucket: 集約枠の残存メンバーに _seq が無い');
      }
      b.aggEl._seq = Math.min(...remainingSeqs);
      this._insertBySeq(entry.lane.bodyEl, b.aggEl);
      b.aggEl.querySelector('.fagg-head').textContent = `${b.label}×${b.count}`;
    }
  }
  // el._seq（到着順）に従い、bodyEl の直接の子の中の正しい位置へ挿入する（_seq を持たない子は比較から除く）。
  _insertBySeq(bodyEl, el) {
    let ref = null;
    for (const child of bodyEl.children) {
      if (child === el) continue;
      if (typeof child._seq === 'number' && child._seq > el._seq) { ref = child; break; }
    }
    if (ref) bodyEl.insertBefore(el, ref); else bodyEl.appendChild(el);
  }
  // 親が届くまでレーン直下に置いていた子を、子コンテナへ付け替える（移動前に _detachFromBucket でバケットの帳簿を清算する）。
  _attachPendingChildren(parentId) {
    const pending = this._pendingChildren.get(parentId);
    if (!pending || !pending.size) return;
    const parentEntry = this.nodesById.get(parentId);
    if (!parentEntry) return;
    const container = this._childrenContainerOf(parentEntry);
    pending.forEach((childId) => {
      const childEntry = this.nodesById.get(childId);
      if (!childEntry) return;
      this._detachFromBucket(childEntry);
      container.appendChild(childEntry.el);
    });
    this._pendingChildren.delete(parentId);
  }
  _placeLeaf(lane, e) {
    const existing = this.nodesById.get(e.id);
    if (existing) { _updateLeafElV2(existing.el, e); this._attachPendingChildren(e.id); return; }
    const parentId = e.parent_id || null;
    const parentEntry = parentId ? this.nodesById.get(parentId) : null;
    if (parentEntry) {
      // 親が既にあれば子コンテナへネストする（ネスト先は集約せず常に個別表示）
      const el = _buildLeafElV2(e);
      this._childrenContainerOf(parentEntry).appendChild(el);
      this.nodesById.set(e.id, { lane, el, bucketKey: null });
      this._attachPendingChildren(e.id);
      return;
    }
    // 親が無い・まだ届いていない場合はレーン直下へ置く（集約の対象はここだけ）。親待ちなら pending に登録する
    const bucketKey = _bucketKeyFor(e);
    if (!bucketKey) {
      const el = _buildLeafElV2(e);
      el._seq = this._seq++;
      lane.bodyEl.appendChild(el);
      this.nodesById.set(e.id, { lane, el, bucketKey: null });
      if (parentId) {
        let pending = this._pendingChildren.get(parentId);
        if (!pending) { pending = new Set(); this._pendingChildren.set(parentId, pending); }
        pending.add(e.id);
      }
      this._attachPendingChildren(e.id);
      return;
    }
    let b = lane.buckets.get(bucketKey);
    if (!b) { b = { label: _bucketLabelFor(e, bucketKey), count: 0, leafEls: [], aggEl: null, aggBody: null }; lane.buckets.set(bucketKey, b); }
    b.count++;
    const el = _buildLeafElV2(e);
    el._seq = this._seq++;
    if (b.count < AGG_MIN_RUN) {
      lane.bodyEl.appendChild(el);
      b.leafEls.push(el);
    } else if (b.count === AGG_MIN_RUN) {
      const anchor = b.leafEls[0];
      const det = document.createElement('details');
      det._seq = anchor._seq;   // 枠が代表する最古の到着順
      det.className = 'fagg';
      det.innerHTML = '<summary class="fagg-head"></summary><div class="fagg-body"></div>';
      lane.bodyEl.insertBefore(det, anchor);
      const body = det.querySelector('.fagg-body');
      b.leafEls.forEach((x) => body.appendChild(x));
      body.appendChild(el);
      b.aggEl = det; b.aggBody = body; b.leafEls = null;
      det.querySelector('.fagg-head').textContent = `${b.label}×${b.count}`;
    } else {
      b.aggBody.appendChild(el);
      b.aggEl.querySelector('.fagg-head').textContent = `${b.label}×${b.count}`;
    }
    this.nodesById.set(e.id, { lane, el, bucketKey });
    if (parentId) {
      let pending = this._pendingChildren.get(parentId);
      if (!pending) { pending = new Set(); this._pendingChildren.set(parentId, pending); }
      pending.add(e.id);
    }
    this._attachPendingChildren(e.id);
  }
  addOrUpdate(e) {
    if (!e || e.type !== 'node' || !e.id) return;
    const laneKey = agentKeyOf(e);
    const lane = laneKey === AGENT_MAIN ? this.lanes.get(AGENT_MAIN) : this._ensureLane(laneKey, parentAgentKeyOf(e));
    lane.lastEventAt = Date.now();
    lane.activeOpText = e.status === 'active' ? _currentOpText(e) : '';
    _updateLaneStats(lane.stats, e);
    if (lane.key !== AGENT_MAIN) this._renderLaneHeader(lane);
    else if (lane.opEl) lane.opEl.textContent = this._thinkingText(Date.now(), lane);
    this._placeLeaf(lane, e);
  }
}
// 終了理由は answer.data.evidence_packet.stop_reason だけを根拠にする。stopInfo は {text, interrupted}。
// evidence_packet 経由は常に interrupted:false、SSE の終端イベント（stopped/timeout/error）だけが true。
const STOP_REASON_UNKNOWN_TEXT = '終了理由を確認できませんでした';
// SSE 終端イベント種別（stream.js が判定・常に中断扱い）
const SSE_STOP_REASON_LABEL = { stopped: '停止操作', timeout: '期限', error: 'エラー' };
export function stopReasonInfo(category) {
  return { text: SSE_STOP_REASON_LABEL[category] || STOP_REASON_UNKNOWN_TEXT, interrupted: true };
}
// エラー文言（サーバ発行の「タイムアウト」文言）から終了種別を判定する。
export function stopReasonCategoryFromError(message) {
  return /タイムアウト/.test(message || '') ? 'timeout' : 'error';
}
// stop_reason の語彙 → 表示文言。表に無い値は「終了理由を確認できませんでした」（Object.create(null) は継承プロパティの誤参照を避けるため）。
const STOP_REASON_TOKEN_LABEL = Object.assign(Object.create(null), {
  no_tool_calls: '自然終了', evaluation_sufficient: '自然終了',
  // unknown は表に無い値と同じ文言を直書きする（語彙一致テストがキーをリテラルとして読むため）
  unknown: '終了理由を確認できませんでした',
  turns_exhausted: '調査の上限に到達', budget_exceeded: '調査の上限に到達',
  tools_per_turn_exceeded: '調べる操作の回数の上限に到達',
  evaluation_blocked: '根拠不足で中断', evidence_verification_failed: '根拠不足で中断',
  refusal: 'AI が回答を控えた',
  truncated: '出力上限で途中終了', content_filtered: '内容の制限で終了',
});
function _stopReasonText(raw) {
  if (!raw || typeof raw !== 'string') return STOP_REASON_UNKNOWN_TEXT;
  return STOP_REASON_TOKEN_LABEL[raw] || STOP_REASON_UNKNOWN_TEXT;
}
// 調査予算（ターン数・呼び出し予算・1応答あたりの操作回数）の到達で打ち切られた stop_reason。本文とは別に「途中までの結果」注記を出す対象。
const BUDGET_EXHAUSTED_STOP_REASONS = new Set(['turns_exhausted', 'budget_exceeded', 'tools_per_turn_exceeded']);
const BUDGET_NOTE_TEXT = '調査の上限に達したため、途中までの結果で答えています。'
  + '範囲（フォルダ）を絞るか、もう一度「続きを調べて」と送ると続きから調べられます。';
export function deriveTraceStopReason(answer) {
  // clarify（確認カード）は終了理由の対象外
  if (!answer || answer.lens === 'clarify') return null;
  const raw = answer.data && answer.data.evidence_packet && answer.data.evidence_packet.stop_reason;
  // evidence_packet 経由は回答の合成まで到達しているため中断ではない
  return { text: _stopReasonText(raw), interrupted: false };
}
// 履歴復元（静的）。ライブと同じ addOrUpdate で流し込み、終了理由も併記する。
function _renderTraceStepsV2(container, trace, answer) {
  const tree = new TraceTreeV2(container, { live: false });
  (trace || []).forEach((e) => tree.addOrUpdate(e));
  tree.finalize(deriveTraceStopReason(answer));
}

// 「実行の分担」サマリ。ローカル/社内サーバ/クラウド AI のどれが何回担当したかを trace（v2）と answer.usage から集計する。
// 配置はサーバの is_local をそのまま使う。answer.usage が無いときは「回答の合成」を「担当不明」として数える。
function _computeProviderSummary(trace, answer) {
  const buckets = new Map();
  const bump = (provider, model, locality, opLabel) => {
    const key = (provider || '?') + '|' + (model || '') + '|' + String(locality);
    let b = buckets.get(key);
    if (!b) { b = { provider, model, locality: _normalizeLocality(locality), counts: new Map() }; buckets.set(key, b); }
    b.counts.set(opLabel, (b.counts.get(opLabel) || 0) + 1);
  };
  (Array.isArray(trace) ? trace : []).forEach((e) => {
    if (!e || e.type !== 'node') return;
    // agent_completed は完了通知であり新しい作業ではないので数えない
    if (e.event_type === 'agent_completed') return;
    const m = e.metrics;
    if (!m || !m.provider) return;
    let label = 'その他の処理';
    if (e.kind === 'tool') label = '資料の読み込み';
    else if (e.event_type === 'evaluation_completed') label = '調査状況の評価';
    else if (e.event_type === 'evidence_committed') label = '根拠の確定';
    bump(m.provider, m.model, m.is_local, label);
  });
  if (answer) {
    if (answer.usage && answer.usage.provider) bump(answer.usage.provider, answer.usage.model, answer.usage.is_local, '回答の合成');
    else bump(null, null, null, '回答の合成');   // usage 欠落＝担当不明
  }
  if (!buckets.size) return null;
  return [...buckets.values()];
}
function _summaryWhoLabel(b) {
  const who = LOCALITY_LABEL[b.locality];
  return who ? `${who} AI${b.model ? `（${esc(b.model)}）` : ''}` : '担当不明';
}
function _providerSummaryHTML(summary) {
  if (!summary) return '';
  // 「すべて…」にまとめるのは、担当が確定した単一バケットのときだけ
  if (summary.length === 1 && LOCALITY_LABEL[summary[0].locality]) {
    return `<div class="provider-summary muted">🧭 すべて${LOCALITY_LABEL[summary[0].locality]} AI が担当しました</div>`;
  }
  const parts = summary.map((b) => {
    const ops = [...b.counts.entries()].map(([k, n]) => `${esc(k)} ${n} 回`).join('・');
    return `${_summaryWhoLabel(b)}が${ops}を担当`;
  });
  return `<div class="provider-summary muted">🧭 ${parts.join('／')}</div>`;
}

// 1ターン分の見出し（質問文40字＋時刻）と、trace があれば折りたたみ本体を作る。trace が無いターンは「（記録なし）」の見出しだけにする。
function _buildTurnEl(turn, id, isLatest) {
  const qtext = (turn.question || '').slice(0, 40);
  const time = fmtDateTime(turn.time || '');
  if (!turn.trace) {
    const div = document.createElement('div');
    div.className = 'fturn fturn-empty'; div.id = id;
    div.innerHTML = '<div class="fturn-head"><span class="fturn-q"></span><span class="fturn-time"></span>'
      + '<span class="fturn-note">（記録なし）</span></div>';
    div.querySelector('.fturn-q').textContent = qtext;
    div.querySelector('.fturn-time').textContent = time;
    return div;
  }
  const det = document.createElement('details');
  det.className = 'fturn'; det.id = id;
  if (isLatest) det.open = true;
  det.innerHTML = '<summary class="fturn-head"><span class="fturn-q"></span><span class="fturn-time"></span></summary>'
    + '<div class="fturn-body"></div>';
  det.querySelector('.fturn-q').textContent = qtext;
  det.querySelector('.fturn-time').textContent = time;
  if (turn.traceVersion === 2) _renderTraceStepsV2(det.querySelector('.fturn-body'), turn.trace, turn.answer);
  else _renderTraceSteps(det.querySelector('.fturn-body'), turn.trace);
  return det;
}
// 会話ロード時、右ペインへ全ターンを時系列で積み上げる（最新だけ展開）。
export function renderTurnStack(turns) {
  const flow = $('flow'); flow.innerHTML = ''; S.nodes = {};
  turns.forEach((t, i) => flow.appendChild(_buildTurnEl(t, `fturn-${i}`, i === turns.length - 1)));
  S.turnSeq = turns.length; S.liveTurnId = null;
  setRt('過去の記録', false);
}
// 回答カードに「この回答の思考の流れ」ボタンを添える（trace があるターンだけ）。クリックで右ペインの該当ターン（turnId）を展開する。
export function attachTraceButton(el, turnId) {
  el._turnId = turnId;
  const body = el.querySelector('.a-body'); if (!body) return;
  const btn = document.createElement('button');
  btn.type = 'button'; btn.className = 'copybtn'; btn.dataset.showtrace = '1';
  btn.textContent = '🕓 この回答の思考の流れ';
  body.appendChild(btn);
}

// 確認カード（ask_user の質問）の HTML。
export function questionHTML(q) {
  const mode = q.mode === 'multiple' ? 'checkbox' : 'radio';
  const name = 'ask-' + String(q.interaction_id || Date.now()).replace(/[^A-Za-z0-9_-]/g, '');
  const opts = (q.options || []).map((o, i) => {
    const id = `${name}-${i}`;
    return `<label class="askopt" for="${esc(id)}">`
      + `<input id="${esc(id)}" type="${mode}" name="${name}" value="${esc(o.id || o.label || i)}" data-qopt data-label="${esc(o.label || '')}">`
      + `<span><b>${esc(o.label || '')}</b>${o.description ? `<small>${esc(o.description)}</small>` : ''}</span></label>`;
  }).join('');
  return `<div class="askcard"><div class="askeyebrow">確認が必要です</div><div class="askprompt">${esc(q.prompt || '確認したいことがあります。')}</div>`
    + `<div class="askopts">${opts}</div>`
    + (q.allow_free_text ? '<textarea class="askfree" data-qfree rows="2" placeholder="補足があれば入力"></textarea>' : '')
    + '<div class="askactions"><button class="btn-primary asksend" data-ask-submit>この内容で続ける</button></div></div>';
}

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
function renderPersonalSources(personal_sources) {
  // 個人ファイル内ヒットを別枠で表示する（DL リンクなし）
  const srcs = (personal_sources || []).filter((s) => s && s.doc_id);
  if (!srcs.length) return '';
  const items = srcs.map((s) =>
    `<li><span class="src-name">${esc(s.doc_id)}</span>`
    + (s.quote ? `<pre class="src-snippet">${esc(String(s.quote).slice(0, 200))}</pre>` : '')
    + '</li>').join('');
  return `<div class="personal-sources"><div class="personal-sources-h">🗂 個人ファイル内ヒット（本人のみ・共有不可）</div><ul>${items}</ul></div>`;
}
function renderCreatedFiles(created_files) {
  // Codex が作成したファイルの DL カード。data-dl は chat.js の委譲ハンドラが処理する（リンクテキストはそのままファイル名に使われる）
  const files = (created_files || []).filter((f) => f && f.name && f.download_url);
  if (!files.length) return '';
  const items = files.map((f) =>
    `<li><a href="${esc(f.download_url)}" data-dl>${esc(f.name)}</a></li>`
  ).join('');
  return `<div class="created-files"><div class="created-files-h">📎 作成したファイル</div><ul>${items}</ul>`
    + `<a href="workspace.html" class="created-files-link">マイワークスペースで開く</a></div>`;
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
      + `<div class="headline">${mdLite(answer.headline)}</div>`
      + personalHTML + summaryHTML + usageHTML + usageSubHTML
      + '<button class="copybtn" data-copy>⧉ コピー</button><button class="copybtn" data-export>⬇ 書き出し</button>'
      + feedbackHtml;
  }
  // レンズ未決定（lens が null）のときは空のチップを出さない
  const lensLabel = LENS_LABEL[answer.lens] || answer.lens;
  const chip = `<div class="chips">${lensLabel ? `<span class="chip">${esc(lensLabel)}</span>` : ''}`
    + _scopeChipsHTML(answer.scope)
    + ((answer.route && answer.route.path) || []).map((p) => `<span class="chip">${esc(p)}</span>`).join('')
    + _depthHeaderHTML(answer.scope, answer.duration_ms) + '</div>';
  // impact はグラフ由来（items/presumed）と検索由来（citations）の2形がある。グラフ結果が無い回答は QA と同じ引用表示にする
  const impactHasGraph = !!(answer.data && ((answer.data.items || []).length || (answer.data.presumed || []).length));
  // troubleshoot も、原因候補（candidates）が無ければ QA と同じ引用表示にする
  const troubleHasCandidates = !!(answer.data && (answer.data.candidates || []).length);
  const body = (answer.lens === 'impact' && impactHasGraph) ? renderImpact(answer)
    : (answer.lens === 'troubleshoot' && troubleHasCandidates) ? renderTrouble(answer) : renderQa(answer);
  // 検証バッジは trace_version=2 の回答だけに付ける
  const evidencePacketForBadges = answer.trace_version === 2 ? (answer.data && answer.data.evidence_packet) : null;
  return chip + `<div class="headline">${mdLite(answer.headline)}</div>`
    + budgetNoteHTML(answer.data && answer.data.evidence_packet) + codexTimeoutNoteHTML(answer)
    + codexStoppedEarlyNoteHTML(answer)
    + retryHintsHTML(answer.retry_hints) + body
    + refGraphHTML(answer) + renderCreatedFiles(answer.created_files)
    + renderSources(answer.sources, answer.sources_verified, evidencePacketForBadges)
    + renderInvestigationRecord(answer.investigation) + personalHTML
    + summaryHTML + usageHTML + usageSubHTML
    + '<button class="copybtn" data-copy>⧉ コピー</button><button class="copybtn" data-export>⬇ 書き出し</button>'
    + feedbackHtml;
}

// 回答ヘッダに、使った範囲・探す対象をチップで示す。層が効かないレンズ（layer_applied:false）は「非適用」と注記する。
const LAYER_CHIP_LABEL = { both: '資料＋コード', docs: '資料のみ', code: 'コードのみ' };
function _scopeChipsHTML(scope) {
  if (!scope) return '';
  const paths = scope.scope_paths || [];
  const scopeLabel = paths.length ? paths.map((p) => S.scopeLabels[p] || p.split('/').pop()).join('・') : '全体';
  let out = `<span class="chip">${esc(scopeLabel)}</span>`;
  if (scope.layer) {
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
function budgetNoteHTML(evidencePacket) {
  const raw = evidencePacket && evidencePacket.stop_reason;
  if (!BUDGET_EXHAUSTED_STOP_REASONS.has(raw)) return '';
  return `<div class="budget-note">${esc(BUDGET_NOTE_TEXT)}</div>`;
}

// Codex の実行が時間切れで打ち切られたターン（answer.codex_timed_out）の注記。stop_reason とは別マーカー。
const CODEX_TIMEOUT_NOTE_TEXT = '調査の時間上限に達したため途中までの結果です。'
  + '「続きを調べる」を押すと続きから調べられます。';
function codexTimeoutNoteHTML(answer) {
  if (!answer || !answer.codex_timed_out) return '';
  return `<div class="budget-note">${esc(CODEX_TIMEOUT_NOTE_TEXT)}</div>`;
}

// Codex が自動継続を尽くしても結論に届かず終了したターン（answer.codex_stopped_early）の注記。timeout とは別マーカーで、同じ resume ボタンで案内する。
const CODEX_STOPPED_EARLY_NOTE_TEXT = 'AI が途中経過を伝えたまま調査を終えたため、途中までの結果です。'
  + '「続きを調べる」を押すと続きから調べられます。';
function codexStoppedEarlyNoteHTML(answer) {
  if (!answer || !answer.codex_stopped_early) return '';
  return `<div class="budget-note">${esc(CODEX_STOPPED_EARLY_NOTE_TEXT)}</div>`;
}

// 回答が参照したノード/関係から小さな部分グラフを組む（impact=経路、troubleshoot=近傍チェーン）
function subgraphFromAnswer(a) {
  const items = a.lens === 'impact' ? ((a.data && a.data.items) || [])
    : a.lens === 'troubleshoot' ? ((a.data && a.data.candidates) || []) : [];
  const nodes = new Map(), edges = new Set(), SEP = '';
  const add = (name) => { if (name && !nodes.has(name)) nodes.set(name, { name }); return nodes.get(name); };
  const ts = a.lens === 'troubleshoot';   // 経路の向きが逆（impact=影響→…→起点・troubleshoot=起点→…→候補）
  for (const it of items) {
    if (it.name) add(it.name).affected = true;
    const path = (ts ? it.path : it.trace) || [];
    path.forEach((nm, k) => { add(nm); if (k > 0 && path[k - 1] && nm) edges.add(path[k - 1] + SEP + nm); });
    if (path.length) add(path[ts ? 0 : path.length - 1]).start = true;   // ts は先頭=起点／impact は末端=起点
  }
  return { nodes: [...nodes.values()], edges: [...edges].map((e) => e.split(SEP)) };
}
function refGraphHTML(answer) {   // 「参照したナレッジグラフ」の折りたたみ（impact/troubleshoot）
  if (answer.lens !== 'impact' && answer.lens !== 'troubleshoot') return '';
  const sub = subgraphFromAnswer(answer);
  if (sub.nodes.length < 2) return '';
  return `<div class="refgraph"><button class="refgraph-h" data-rg="${esc(JSON.stringify(sub))}">`
    + `🕸 参照したナレッジグラフ（${sub.nodes.length}件・つながり${sub.edges.length}本）<span class="caret">▾</span></button>`
    + '<div class="refgraph-body" hidden></div></div>';
}
export function initRefGraph(el, sub) {   // 部分グラフを cytoscape で描く（起点=オレンジ大／影響=teal）
  const dark = document.documentElement.dataset.theme === 'dark';
  const els = [
    ...sub.nodes.map((n) => ({ data: { id: n.name, label: n.name, role: n.start ? 'start' : (n.affected ? 'affected' : 'mid') } })),
    ...sub.edges.filter(([a, b]) => a && b).map(([a, b]) => ({ data: { source: a, target: b } })),
  ];
  const cy = cytoscape({
    container: el, elements: els, wheelSensitivity: 0.2, maxZoom: 1.6, minZoom: 0.1,
    style: [
      { selector: 'node', style: { label: 'data(label)', 'font-size': 9, width: 16, height: 16,
        'background-color': '#94a3b8', color: dark ? '#e6edf3' : '#1f2937', 'text-valign': 'bottom', 'text-margin-y': 2,
        'text-outline-width': 2, 'text-outline-color': dark ? '#0f1419' : '#fff', 'text-max-width': 90, 'text-wrap': 'ellipsis' } },
      { selector: 'node[role="start"]', style: { 'background-color': '#d97706', width: 22, height: 22 } },
      { selector: 'node[role="affected"]', style: { 'background-color': '#0d9488' } },
      { selector: 'edge', style: { width: 1.2, 'line-color': dark ? '#3a4550' : '#cbd5e1', 'target-arrow-shape': 'triangle',
        'target-arrow-color': dark ? '#3a4550' : '#cbd5e1', 'curve-style': 'bezier', 'arrow-scale': 0.7 } },
    ],
    layout: { name: 'breadthfirst', directed: true, padding: 12, spacingFactor: 1.05,
      roots: sub.nodes.filter((n) => n.start).map((n) => n.name) },
  });
  cy.one('layoutstop', () => cy.fit(undefined, 16));
  return cy;
}
// 回答カードを追加する。
export function appendAnswer(answer, messageId, trace, feedback) {
  const el = answer ? appendAssistantRaw(answerHTML(answer, trace, feedback)) : appendAssistantRaw('<div class="muted">（内容なし）</div>');
  if (el && answer) el._answer = answer;   // 回答単位の書き出し用
  if (el && messageId != null) el._messageId = messageId;   // フィードバック送信先
  return el;
}

// 最終回答の段階表示（answer_delta を一定ペースで描く）
let _revPending = '', _revTimer = null;
export function clearReveal() { if (_revTimer) clearInterval(_revTimer); _revTimer = null; _revPending = ''; }
export function reveal(text) {
  _revPending += text;
  if (_revTimer || !S.ansHead) return;
  // 一括到着でも一定ペースで描くよう、1回の文字数に上限を設ける
  _revTimer = setInterval(() => {
    if (!_revPending) { clearInterval(_revTimer); _revTimer = null; return; }
    const n = Math.max(2, Math.min(6, Math.ceil(_revPending.length / 40)));
    S.ansHead.textContent += _revPending.slice(0, n); _revPending = _revPending.slice(n); scroll();
  }, 20);
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
  clearReveal();
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
};
const _WHY_TYPE = { INVOKES: 'を呼び出しています', COPIES: 'を取り込んでいます', ACCESSES: 'を参照しています', CONTAINS: 'を含んでいます' };
const _WHY_RULE = {
  alias: '別名の指定で決まりました', single_import: 'import で指定された型に一致しました', same_package: '同じ package（名前空間）の名前に一致しました',
  wildcard: 'import（まとめて指定）の範囲で一致しました', qualified_name: '完全な名前で一致しました', path_exact: '書かれたパスのとおりに一致しました',
  path_suffix: 'パスの末尾で一致しました', config_key_all: '同じ名前の設定キーすべてに一致しました', schema_exact: 'スキーマ名まで一致しました',
  schema_unqualified: '名前が一致しました（定義側にスキーマ名なし）', table_name: '表の名前で一致しました', nearest_name: '一番近い場所の同じ名前に一致しました',
  di_qualifier: '注入で指定された名前が実装の名前に一致しました', di_primary: '実装のうち優先の指定が付いたものに決まりました',
  di_single_impl: '実装が 1 つだけでした',
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
// 影響範囲の回答本文（影響一覧。構造の依存が見つからなかったときは資料から見つけた関連を別枠で出す）。
function renderImpact(a) {
  const items = (a.data && a.data.items) || [];
  const presumed = (a.data && a.data.presumed) || [];
  if (!items.length && !presumed.length) return '';
  const lis = items.map((it) => {
    const trace = it.trace || [];                      // 影響の経路（ノード名列）
    const chain = trace.join(' ← ');
    // evidence は代表経路の辺ごとの {type, doc, line, via?, rule?, sources?, sources_overflow_count?}（quote は presumed のみが持つ）
    const why = _whyListHTML(trace, (it.evidence || []).filter((e) => e.doc));
    const hasDetail = !!(chain || why);
    // 詳細（経路チップ＋なぜつながっているか）は行データにあるものだけで構成する。トグルは行全体（キーボード操作は chat.js の委譲側）
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
  }).join('');
  const ilist = items.length ? `<ul class="ilist">${lis}</ul>` : '';
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
  return ilist + pres;
}
function renderTrouble(a) {
  return ((a.data && a.data.candidates) || []).slice(0, 8).map((c) =>
    `<div class="cand"><span class="nm">${esc(c.name)}</span><span class="role">${esc(c.role)}</span>`
    + (c.path && c.path.length ? `<div class="chain">${esc(c.path.join(' → '))}</div>` : '') + '</div>').join('');
}
// 引用（該当箇所）カード。既定は折りたたみで、件数だけ見える見出しボタン＋hidden な本体にする。
function renderQa(a) {
  const cites = (a.data && a.data.citations) || [];
  if (!cites.length) return '';
  const items = cites.map((c) =>
    `<div class="cite"><span class="doc">${esc(c.doc_id)}</span><span class="sp">行 ${esc(c.span && c.span[0])}–${esc(c.span && c.span[1])}</span><pre>${esc(c.quote)}</pre></div>`).join('');
  return `<div class="cites"><button class="cites-h" type="button" data-cites aria-expanded="false">`
    + `📄 該当箇所 (${cites.length})<span class="caret">▾</span></button>`
    + `<div class="cites-body" hidden>${items}</div></div>`;
}
// 出典（原本ダウンロードリンク）。0件でも明示する。
// 設計: docs/design/chat.md「1ターンの流れ」
function renderSources(sources, verifiedDocIds, evidencePacket) {
  if (!sources || !sources.length) {
    return `<div class="sources"><div class="h">出典（原本をダウンロード）</div>`
      + '<span class="muted" style="font-size:var(--text-small)">確証のある資料は見つかりませんでした</span></div>';
  }
  // Evidence Packet の verification_method があれば doc_id ごとに検証バッジを添える
  const vmap = _verificationMethodByDoc(evidencePacket);
  const link = (s) => `<a href="${esc(s.download_url)}" data-dl>📄 ${esc(s.doc_id)}</a>${verificationBadgeHTML(vmap.get(s.doc_id))}${importanceBadgeHTML(s)}`;
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
// 調査台帳が保存された回答にだけ、Markdown のダウンロード導線を出す。href は chat.js の data-investigation-dl 委譲ハンドラがクリック時に組み立てる。
function renderInvestigationRecord(investigation) {
  if (!investigation || !investigation.recorded) return '';
  return '<div class="sources investigation-record"><div class="h">調査の記録</div>'
    + '<a href="#" data-investigation-dl>📋 Markdown でダウンロード</a></div>';
}

