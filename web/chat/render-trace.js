// チャットの描画のうち右ペインの思考の流れ（trace・ターンの積み上げ・終了理由）。
// 設計: docs/design/chat.md「1ターンの流れ」
'use strict';

import { S } from './state.js';
import { setRt } from './stream.js';

const $ = Sherpa.$, esc = Sherpa.esc, fmtDateTime = Sherpa.fmtDateTime, mdLite = Sherpa.mdLite, analyzerLabel = Sherpa.analyzerLabel;

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
export function verificationBadgeHTML(method) {
  if (!method) return '';
  const label = VERIFICATION_BADGE_LABEL[method];
  // 未知の verification_method は「検証済み」と断定せず「検証方法不明」を出す
  if (!label) return ` <span class="verif-badge unknown">${esc('検証方法不明')}</span>`;
  const cls = method === 'span_unmatched' ? 'warn' : 'ok';
  return ` <span class="verif-badge ${cls}">${esc(label)}</span>`;
}
// doc ごとの verification_method を集める。source_path に加え、集計 Evidence の matched_doc_ids も見る。
export function _verificationMethodByDoc(evidencePacket) {
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
// 保存された回答の完了状態と終了理由を、思考の流れの表示に使う。
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
export const BUDGET_EXHAUSTED_STOP_REASONS = new Set(['turns_exhausted', 'budget_exceeded', 'tools_per_turn_exceeded']);
export const BUDGET_NOTE_TEXT = '調査の上限に達したため、途中までの結果で答えています。'
  + '範囲（フォルダ）を絞るか、もう一度「続きを調べて」と送ると続きから調べられます。';
export function deriveTraceStopReason(answer) {
  // clarify（確認カード）は終了理由の対象外
  if (!answer || answer.lens === 'clarify') return null;
  if (answer.completion === 'stopped') return stopReasonInfo('stopped');
  if (answer.completion === 'partial') return { text: '途中までの回答', interrupted: true };
  if (answer.completion === 'failed' || answer.agentic_failure === 'error') return stopReasonInfo('error');
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
export function _computeProviderSummary(trace, answer) {
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
export function _providerSummaryHTML(summary) {
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
  const discarded = Number.isInteger(q.discarded_files) && q.discarded_files > 0
    ? `<div class="muted" style="font-size:var(--text-small)">確認の前に AI が作ったファイル ${q.discarded_files} 件は、資料の作成の依頼ではないため保存していません。</div>` : '';
  return `<div class="askcard"><div class="askeyebrow">確認が必要です</div><div class="askprompt">${esc(q.prompt || '確認したいことがあります。')}</div>${discarded}`
    + `<div class="askopts">${opts}</div>`
    + (q.allow_free_text ? '<textarea class="askfree" data-qfree rows="2" placeholder="補足があれば入力"></textarea>' : '')
    + '<div class="askactions"><button class="btn-primary asksend" data-ask-submit>この内容で続ける</button></div></div>';
}

