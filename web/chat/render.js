// フェーズ6 S5（リファクタリング計画）: 回答カード・trace/turn stack・welcome・出典の描画ドメインを
// chat.js から純移動（LENS_LABEL/STATUS_CLASS/STATUS_LABEL も answerHTML/statusTag でのみ使う
// 単一ドメイン定数のため併せて移動）。export は chat.js（entry）側にまだ残る呼び出し元
// （init・delegate ハンドラの welcome/initRefGraph）と、S6 で切り出された web/chat/history.js 側の
// 呼び出し元（openConversation/newConversation の renderTurnStack/attachTraceButton/appendAnswer/
// appendAssistantRaw/appendUser）と、S7 で切り出された web/chat/stream.js 側の呼び出し元
// （onNode/appendQuestion/send・questionHTML/_renderDetail/appendAssistantRaw/appendUser/
// ensureAnswerCard/finalizeAnswer/clearReveal/reveal）から参照される関数のみに絞る（内部専用の
// _renderTraceSteps/_buildTurnEl/renderPersonalSources 等は非公開のまま）。
// appendAssistantRaw/appendUser/scroll は元は chat.js「===== 描画 =====」節の先頭にあったが、
// stream.js に残る関数（send 等）と history.js（openConversation 等）の両方から呼ばれるため、
// render.js を「entry・他ドメインから import される葉」に保つ（chat.js への逆 import を作らない
// ＝地雷3節の「迷ったら entry 経由の配線に倒す」を適用し、新たな循環 import を増やさない）方針で
// ここへ純移動する。
// _renderDetail は stream.js の onNode と共用のため export する。逆方向の共用が
// setRt（renderTurnStack が使う「思考の流れ」状態ラベル更新）: 呼び出し1件のためだけに setRt 自体を
// ここへ引っ張ると stream.js 側の置き場が歪むため、setRt は stream.js に残したまま
// `import { setRt } from './stream.js'` する（S7 で chat.js↔render.js だった循環 import が
// stream.js↔render.js に付け替わった。関数宣言＝hoisted のため実行時に呼ぶ限り ESM で安全・
// share-dialog.js の copyText と同じパターン）。
'use strict';

import { S, EXAMPLES } from './state.js';
import { setRt } from './stream.js';

const $ = Sherpa.$, esc = Sherpa.esc, fmtDateTime = Sherpa.fmtDateTime, mdLite = Sherpa.mdLite, analyzerLabel = Sherpa.analyzerLabel;   // 共通ユーティリティ（common.js）

const LENS_LABEL = { impact: '影響範囲分析', troubleshoot: 'トラブルシュート', qa: '仕様問い合わせ', author: '資料を作成' };
const STATUS_CLASS = { deprecated: 'deprecated', hidden_candidate: 'hidden_candidate' };
const STATUS_LABEL = { deprecated: '廃止', hidden_candidate: '隠し候補' };

// 質問例チップのブロック（EXAMPLES が空＝管理者が非表示に設定＝空文字を返しブロック自体を出さない）。
// welcome() の初回描画・refreshWelcomeExamples() の後追い更新（管理者設定 `chat_examples` の
// 取得が welcome() より遅れて完了する経路・menus.js::loadConfig 参照）の両方から使う。
// アイコンは実際の挙動（入力欄に読み込むだけ・送信はしない）に合わせる＝送信を示す記号は使わない。
function _examplesHtml() {
  if (!EXAMPLES.length) return '';
  return '<div class="examples">' + EXAMPLES.map((t, i) =>
    `<button class="example" data-ex="${i}"><span class="exq">${esc(t)}</span><span class="exarrow">✎</span></button>`).join('') + '</div>';
}

export function welcome() {
  $('messages').innerHTML = '';
  // 初回の「ようこそ」3ステップ（初見に使い方が伝わるように）。文言は固定＝ユーザ入力を含まないため esc 不要。
  const steps =
    '<div class="headline">ようこそ Sherpa へ</div>'
    + '<ol class="welcome-steps">'
    + '<li><b>資料フォルダを登録</b><br><span class="muted">管理画面で、調べたい社内資料フォルダを登録します。</span></li>'
    + '<li><b>チャットで質問</b><br><span class="muted">例:「消費税率を変えると何に影響する？」／「夜間バッチの異常終了の原因候補は？」</span></li>'
    + '<li><b>出典から原本を確認</b><br><span class="muted">回答末尾の出典リンクから、根拠になった元ファイルを開けます。</span></li>'
    + '</ol>';
  const el = appendAssistantRaw(steps
    + '<div class="muted">気になることを、いつもの言葉で質問してください。'
    + '社内資料に基づく回答が必要なときは、<b>「ナレッジ参照」をオン</b>に。</div>' + _examplesHtml());
  el.classList.add('welcome-msg');   // 送信時に消すための目印（重なり防止）
}

// menus.js::loadConfig（GET /settings）が welcome() より後に完了した場合の後追い更新。
// ウェルカムメッセージが今も表示中（会話を開いていない・新規会話のまま）のときだけ、質問例
// ブロックをサーバ値（EXAMPLES は setChatExamples 済み）で置き換える。表示中でなければ何もしない
// （既に会話を開いていれば welcome-msg は無い＝安全に no-op）。
export function refreshWelcomeExamples() {
  const msg = document.querySelector('.welcome-msg');
  if (!msg) return;
  const html = _examplesHtml();
  const old = msg.querySelector('.examples');
  if (old) { if (html) old.outerHTML = html; else old.remove(); }
  else if (html) { msg.insertAdjacentHTML('beforeend', html); }
}

// ===== trace / turn stack（右ペイン・積み上げ表示と共通で使う描画） =====
// A3: ツール detail の「クエリ」をチップに分離（「」が無ければ従来どおり素のテキスト）。
// チップは esc()、素の場合は textContent＝どちらも XSS 安全。
export function _renderDetail(elDetail, e) {
  const d = e.detail || '';
  const m = (e.kind === 'tool') ? d.match(/「([^」]*)」/) : null;
  if (m && m[1]) {
    const rest = (d.slice(0, m.index) + d.slice(m.index + m[0].length)).trim();
    elDetail.innerHTML = `<span class="fchip">${esc(m[1])}</span>`
      + (rest ? `<span class="frest">${esc(rest)}</span>` : '');
  } else {
    elDetail.textContent = d;             // textContent ＝ XSS安全（従来どおり）
  }
}
// 確定済み trace のステップ群を container に静的描画（.active は使わない＝pulse/glow は付かない）。
function _renderTraceSteps(container, trace) {
  (trace || []).forEach((e) => {
    const el = document.createElement('div');
    el.className = 'fstep done' + (e.kind === 'tool' ? ' tool' : '');
    el.innerHTML = '<div class="fnode">✓</div><div class="fbody">'
      + '<div class="fhead"><div class="flabel"></div></div><div class="fdetail"></div></div>';
    el.querySelector('.flabel').textContent = e.label || '';    // textContent＝XSS安全
    _renderDetail(el.querySelector('.fdetail'), e);
    container.appendChild(el);
  });
}
// ===== EXT-4（拡張設計 §10）: trace_version=2 の階層描画 =====
// 既存の葉ノード描画（.fstep/.flabel/.fdetail・_renderDetail のチップ化）は階層の末端としてそのまま
// 再利用する。v1（_renderTraceSteps/onNode・stream.js）は無改修のまま残し、v2 は完全に別の描画関数
// （本節）として追加する＝「trace_version=1 の会話は従来どおり描画」契約を機械的に保証する。
const AGENT_MAIN = 'main';           // agent_run_id が無い（null/undefined/空）＝メイン run
const AGG_MIN_RUN = 3;               // 同種操作をこの件数以上ぶら下げたら集約表示に畳む（§10）

function agentKeyOf(e) {
  const a = e && e.agent_run_id;
  return (a === null || a === undefined || a === '') ? AGENT_MAIN : String(a);
}
function parentAgentKeyOf(e) {
  const a = e && e.parent_agent_run_id;
  return (a === null || a === undefined || a === '') ? AGENT_MAIN : String(a);
}
// agent_run_id の既存命名 `sub:{profile_id}:{seq}`（exec_event.py・providers/base.py）から
// profile_id を取り出す（「担当」表示の最終フォールバック用）。ダッシュ/アンダースコアを空白へ
// 整形するだけの最小限の平文化——固定の対訳表は持たない（未知の profile_id でも壊れない・
// 専門用語ゼロ）。サーバが `metrics.name`（表示名）を持たせていれば
// そちらを常に優先する（下記 `_renderLaneHeader` 参照）。
function agentProfileOf(key) {
  const m = /^sub:([^:]+):\d+$/.exec(key || '');
  return m ? m[1] : key;
}
function _humanizeProfile(profile) {
  const cleaned = String(profile || '').replace(/^search-helper-/, '').replace(/[-_]+/g, ' ').trim();
  return cleaned || '下調べ役';
}
const AGENT_STATUS_LABEL = { active: '実行中', done: '完了', failed: '失敗', cancelled: '取消', aborted: '中断' };
function agentStatusChipHTML(status) {
  const s = AGENT_STATUS_LABEL[status] ? status : 'active';
  return `<span class="fagent-status ${s}">${esc(AGENT_STATUS_LABEL[s])}</span>`;
}
// 「担当バッジ」（ローカル/社内サーバ/クラウド/クラウド（OpenAI 互換）AI の分担）: 配置の判定は
// サーバ側の権威ある値（`metrics.is_local`/`usage.is_local`＝`sherpa.agent_constructs.is_local`・
// 4値: "local"/"on_prem"/"cloud"/"cloud_compat"）をそのまま表示するだけにし、フロントでは
// 推測しない（Codex は常に provider="codex" を名乗るため、モデル名や provider 文字列だけでは
// 配置を判定できない。"cloud_compat"＝OpenAI 本家でも Azure でもない外部の OpenAI 互換クラウド
// サービスで、"custom" というだけで一律「社内サーバ」扱いにすると誤表示になるため区別する）。
// 既知の4値のどれでもない（`null`/`undefined`/不正値）ときは「担当不明」と誠実に表示する
// （誤断定しない）。
// `Object.create(null)`（VERIFICATION_BADGE_LABEL/STOP_REASON_TOKEN_LABEL と同じ理由・
// サーバ由来の is_local が `"constructor"` 等だった場合に継承プロパティを誤って引き当てない）。
const LOCALITY_LABEL = Object.assign(Object.create(null), {
  local: 'ローカル', on_prem: '社内サーバ', cloud: 'クラウド', cloud_compat: 'クラウド（OpenAI 互換）',
});
// バッジの配色は "cloud_compat" も "cloud" と同じ（どちらも「クラウド」の一種という視覚分類・
// 文言だけを分けて誠実さを保つ）。
const _LOCALITY_BADGE_CLASS = Object.assign(Object.create(null), {
  local: 'local', on_prem: 'on_prem', cloud: 'cloud', cloud_compat: 'cloud',
});
function providerBadgeHTML(provider, model, locality) {
  if (!provider) return '';
  const who = LOCALITY_LABEL[locality];
  const cls = who ? _LOCALITY_BADGE_CLASS[locality] : 'unknown';
  const label = model ? `${who || '担当不明'}: ${model}` : (who || '担当不明');
  // 狭いペインで CSS 側が ellipsis 切り詰めをかけても、title 属性（ネイティブ tooltip）で
  // 全文を確認できるようにする（幅の都合で見えなくても情報は失わない）。
  return `<span class="provider-badge ${cls}" title="${esc(label)}">${esc(label)}</span>`;
}
// 検証バッジ（§4.6・EV-0 の最小版を超える粒度）: Evidence Packet の verification_method → 表示ラベル。
// `Object.create(null)` で prototype を持たないオブジェクトにする——サーバ由来の任意の文字列
// （壊れたデータ・将来の値）が `"constructor"`/`"toString"` 等だった場合に `Object.prototype`
// から継承した関数を誤って引き当てて truthy 扱いにしないため（`hasOwnProperty` 判定と同じ効果を
// プロトタイプ自体を無くすことで得る）。
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
  // 未知の verification_method（将来の新しい値・壊れたデータ等）を「機械検証済み」（緑）へ
  // フォールバックしない——実際には検証方法が分からないのに検証済みだと誤断定することになる。
  // 中立の「検証方法不明」を表示する（何も出さないより「検証はされたはずだが方法が不明」という
  // 事実を誠実に伝える・is_local が未知のとき「担当不明」を表示する既存の方針と同じ）。
  if (!label) return ` <span class="verif-badge unknown">${esc('検証方法不明')}</span>`;
  const cls = method === 'span_unmatched' ? 'warn' : 'ok';
  return ` <span class="verif-badge ${cls}">${esc(label)}</span>`;
}
// 登録者重要度バッジ（I2・2026-09-05）: 出典1件（`source`＝`chat_service._sources` が返す dict）が
// `importance`（"高"/"低"）を持つ時だけ表示する。"中"／未設定（キー自体が無い）は無表示——
// `chat_service._src_url` の「無ければ無い」契約（§2 truth table）を画面側でも保つ（未設定を
// 「中」と偽って見せない）。「高」は強調（accent 色）・「低」は淡色、どちらも `importance_reason`
// があれば title 属性（ホバー）へ添える。
const IMPORTANCE_BADGE_LABEL = { '高': '登録者重要度：高', '低': '登録者重要度：低' };
function importanceBadgeHTML(source) {
  const v = source && source.importance;
  if (v !== '高' && v !== '低') return '';
  const cls = v === '高' ? 'high' : 'low';
  const title = source.importance_reason ? ` title="${esc(source.importance_reason)}"` : '';
  return ` <span class="importance-badge ${cls}"${title}>${esc(IMPORTANCE_BADGE_LABEL[v])}</span>`;
}
// `source_path`（citation 単体）だけでなく `matched_doc_ids`
// （list_docs/graph_neighbors の集計 Evidence が裏付ける doc 群）も見る——集計 Evidence は
// `source_path: null` のまま複数 doc を裏付けるため、`matched_doc_ids` を見ないとそれらの出典に
// バッジが一切付かない。
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

// ---- 集約（同種操作を件数で畳む・§10「集約表示」) ----
const EVENT_TYPE_AGG_LABEL = { candidate_discovered: '候補', candidate_verified: '精読',
                               candidate_rejected: '却下', evidence_committed: '採用' };
function _bucketKeyFor(e) {
  if (e.kind === 'tool') return 'tool:' + (e.label || '');
  if (e.event_type && EVENT_TYPE_AGG_LABEL[e.event_type]) return 'ev:' + e.event_type;
  return null;   // think/agent/evaluation/hook 等は常に個別表示（milestone を隠さない）
}
function _bucketLabelFor(e, key) {
  return key.startsWith('tool:') ? (e.label || 'ツール呼び出し') : (EVENT_TYPE_AGG_LABEL[e.event_type] || e.event_type);
}

// ---- 葉ノード（.fstep）: v1 の見た目を踏襲しつつ kind/status の新設値・担当バッジに対応 ----
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
  // `.fchildren`＝parent_id で子に指定されたノードの入れ子先（空なら CSS の :empty で余白を出さない）。
  el.innerHTML = '<div class="fnode"></div><div class="fbody"><div class="fhead"><div class="flabel"></div></div>'
    + '<div class="fdetail"></div><div class="fchildren"></div></div>';
  _updateLeafElV2(el, e);
  return el;
}
function _updateLeafElV2(el, e) {
  el.className = _fstepClassV2(e);
  el.querySelector('.flabel').textContent = e.label || '';   // textContent＝XSS安全
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
// `metrics.is_local`/`usage.is_local` は4値（"local"/"on_prem"/"cloud"/"cloud_compat"）＋null の
// 契約（サーバ側 `agent_constructs.is_local`・null＝判定不能）。不正値（壊れた/将来の payload）は
// 「担当不明」（null）へ寄せる＝誤断定より安全側。
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
  // サーバがまだ候補/根拠を専用イベントで発行していない現状では、一度もその種のイベントを
  // 見ていないレーンに「候補 0」「根拠 0」を出すと「調べて0件だった」と誤解される
  // （実際は「未計測」）。実際にそれらしいイベントを1回でも見た時だけ表示対象にする
  // （`hasCandidateSignal`/`hasEvidenceSignal`・`_renderLaneHeader` 参照）。
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
    // サーバの表示名（`search_helper.resolve()` 等の "name"）があれば内部 slug（profile_id）より
    // 優先する（専門用語ゼロ・「担当」を利用者向けの平文で見せる）。
    if (typeof m.name === 'string' && m.name && !stats.serverName) stats.serverName = m.name;
  }
  if (et === 'agent_completed') stats.status = 'done';
  else if (et === 'agent_failed') { stats.status = 'failed'; if (!stats.stopReason) stats.stopReason = e.detail || ''; }
  else if (et === 'agent_cancelled') { stats.status = 'cancelled'; if (!stats.stopReason) stats.stopReason = e.detail || ''; }
}
function _fmtElapsedV2(ms) { return (ms / 1000).toFixed(ms < 10000 ? 1 : 0) + 's'; }
// TraceTreeV2: ストリーミング（1件ずつ addOrUpdate）と履歴復元（配列を順に addOrUpdate）の**両方**が
// 使う共通ビルダ（EXT-4 要件「同じ階層描画になること」）。id で dedup（v1 の onNode と同じ契約）し、
// agent_run_id ごとにレーン（`.fagent` の入れ子 details）を作り、同種操作は件数で集約する。
// `live=true`（ストリーミング）のときだけ「考え中」表示・経過時間のカウントアップを行う
// （履歴復元は最終状態を静的に出すだけ＝ティックしない）。
export class TraceTreeV2 {
  constructor(container, { live = false, startedAtMs = 0 } = {}) {
    this.container = container;
    this.live = live;
    this.lanes = new Map();
    this.nodesById = new Map();
    // parent_id が指す親がまだ届いていない子（拡張設計 §2/§10・親子は id/parent_id で
    // 表す）を一時的にレーン直下へ置いた上で待たせる registry（`parentId -> Set<childId>`）。
    // 親が後から届いた時点で `_attachPendingChildren` が実 DOM 要素を子コンテナへ付け替える。
    this._pendingChildren = new Map();
    // レーン直下（`lane.bodyEl` の直接の子）に置かれた要素の到着順を表す通し番号。集約枠の
    // 解体（`_detachFromBucket`）で複数要素をまとめて元の位置へ戻す時、別種ノードが間に
    // 挟まっていても正しい兄弟位置へ個別に戻すために使う（`_insertBySeq` 参照）。
    this._seq = 0;
    this._destroyed = false;
    this._stopNoteEl = null;   // finalize() が書いた「終了理由」note（correctStopReason が後から訂正する）
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
  // ターン終端（answer/stopped/error/question）で呼ぶ: ティックを止め、実行中のまま残ったレーンを
  // 畳み、終了理由（自然終了／調査の上限に到達／根拠不足で中断／停止操作／期限／エラー等）を明示する。
  // `stopInfo`＝`{text, interrupted}`（`deriveTraceStopReason`/`stopReasonInfo` が組む・render.js
  // 内で完結）。`interrupted` が
  // レーンを「完了」ではなく「中断」にするかどうかの唯一の判定材料——SSE の終端イベント種別
  // （停止操作/期限/エラー）だけが `true`。evidence_packet 経由（既知/未知いずれの stop_reason で
  // あっても回答の合成まで到達している）は常に `false`＝未知の値を誤って「中断」扱いにしない。
  // `stopInfo` 省略（onQuestion 等の確認待ちで note を出さずティックだけ止めたいケース）も
  // 同様に「完了」扱いにする。
  finalize(stopInfo) {
    if (this._tickTimer) { clearInterval(this._tickTimer); this._tickTimer = null; }
    const interrupted = !!(stopInfo && stopInfo.interrupted);
    this.lanes.forEach((lane) => {
      if (lane.stats.status === 'active') lane.stats.status = interrupted ? 'aborted' : 'done';
      if (lane.key !== AGENT_MAIN) this._renderLaneHeader(lane);
      if (lane.opEl) { lane.opEl.remove(); lane.opEl = null; }   // 「考え中」行は完了後は残さない
    });
    if (stopInfo && stopInfo.text) {
      const note = document.createElement('div');
      note.className = 'ftrace-stopreason muted';
      note.textContent = `終了理由: ${stopInfo.text}`;   // textContent＝XSS安全
      this.container.appendChild(note);
      this._stopNoteEl = note;
    }
  }
  // 停止 POST の結果待ち中に接続が切れると、onerror が暫定的に「停止操作」として finalize
  // してしまう（停止起因かどうかまだ確定していない時点での楽観的表示）。
  // 停止 POST が実は失敗だった（＝停止起因ではない本物の接続断だった）と後から判明したとき、
  // stream.js 側がこのメソッドで note の文言だけを訂正する（レーン状態は一度確定した「完了」を
  // 「中断」へ格下げしない＝表示の後退よりは多少不正確な状態表示の方が実害が小さいと判断）。
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
    det._seq = this._seq++;   // レーン直下の到着順比較の対象（_insertBySeq 参照）に含める
    parentLane.bodyEl.appendChild(det);
    const lane = { key, parentKey: parentLane.key, rootEl: det, headerEl: det.querySelector('.fagent-head'),
                  opEl: null, bodyEl: det.querySelector('.fagent-body'), buckets: new Map(),
                  stats: _freshLaneStats(), startedAt: Date.now(), lastEventAt: Date.now(), activeOpText: '' };
    if (this.live) {
      const op = document.createElement('div'); op.className = 'fagent-op muted';
      det.insertBefore(op, det.querySelector('.fagent-body'));   // summary 直後・本文の前に「考え中」行を置く
      lane.opEl = op;
    }
    this.lanes.set(key, lane);
    this._renderLaneHeader(lane);
    return lane;
  }
  _renderLaneHeader(lane) {
    const s = lane.stats;
    // サーバの表示名（`metrics.name`）を最優先し、無ければ profile_id を平文化
    // （`_humanizeProfile`）する。内部 slug（"researcher"・"search-helper-openai" 等）を
    // そのまま出さない（専門用語ゼロ）。
    const role = s.serverName || _humanizeProfile(agentProfileOf(lane.key));
    const name = s.startedLabel || s.firstLabel || role;
    const bits = [];
    bits.push(`<span class="fagent-name">${esc(name)}</span>`);
    bits.push(`<span class="fagent-role">${esc(role)}</span>`);
    if (s.provider) bits.push(providerBadgeHTML(s.provider, s.model, s.locality));
    bits.push(agentStatusChipHTML(s.status));
    if (s.skill) bits.push(`<span class="fagent-stat">得意分野: ${esc(s.skill)}</span>`);
    bits.push(`<span class="fagent-stat">調査の回数 ${s.cycles}</span>`);
    bits.push(`<span class="fagent-stat">道具の使用回数 ${s.toolCalls}</span>`);
    // サーバが候補/根拠の専用イベントをまだ発行していない現状では、一度もそれらしいイベントを
    // 見ていないレーンに「候補 0」「根拠 0」を出さない（誤って「調べて0件だった」と読めて
    // しまうため・実際は「未計測」）。
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
  // ノード自身の `.fbody > .fchildren` 要素（parent_id で子に指定されたノードの入れ子先）。
  _childrenContainerOf(entry) {
    return entry.el.querySelector(':scope > .fbody > .fchildren');
  }
  // `entry` が集約バケットに登録されたまま（レーン直下へ一時配置された pending child）なら、
  // そのバケットの帳簿（count/leafEls/表示件数）からも取り除いてから登録を外す。取り除かず
  // DOM 要素だけを移動すると、①`leafEls` に残った要素が既に別の親へ移動済みなのに
  // `insertBefore(det, anchor)` の `anchor` として参照され続け `NotFoundError` になる、
  // ②集約枠 `.fagg-body` へ later `leafEls.forEach((x) => body.appendChild(x))` で回収され、
  // 既に確立した親子関係を引き剥がして集約枠へ戻してしまう、③件数表示が実際の子要素数と
  // ずれる、の3つの実害が起きる。0件になったバケット（集約枠含む）は跡を残さず削除し、
  // 次の同じ bucketKey のイベントが汚れていない新規バケットから始められるようにする。
  // 件数が AGG_MIN_RUN 未満へ縮小した場合は、集約枠自体も解体して残りを個別表示へ戻す
  // （「AGG_MIN_RUN 未満は常に個別表示」という不変条件を件数の増減どちらでも保つ——集約枠を
  // 残したまま「×2」のような閾値未満の集約表示が居座り続けることを許さない）。
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
      // 集約枠を解体し、`.fagg-body` に残っている要素（detach 対象自身は除く＝呼び出し元が
      // この直後に別の場所へ appendChild して移動する）をレーン直下へ個別表示として戻す。
      // 枠の旧位置へまとめて insertBefore すると、枠の外側に別種ノードが挟まっていた場合に
      // 到着順が壊れる（本レーンで実際に踏んだ不具合）ため、`_insertBySeq` で各要素を自分の
      // 到着順が指す位置へ個別に戻す。`b.leafEls` を実配列として復元することで、後で再び
      // 閾値に達した時は「初めて集約する」経路をそのまま再利用できる。
      const remaining = Array.from(b.aggBody.children).filter((x) => x !== entry.el);
      b.aggEl.remove();
      remaining.forEach((x) => this._insertBySeq(entry.lane.bodyEl, x));
      b.aggEl = null;
      b.aggBody = null;
      b.leafEls = remaining;
      return;
    }
    if (b.aggEl) {
      // 枠は存続する（count は AGG_MIN_RUN 以上のまま＝残存メンバーは必ず2件以上）。detach
      // 対象が枠の代表する最古参だった場合、枠の到着順（_seq）と DOM 上の位置が古い
      // （取り除かれたメンバーの位置のまま）取り残される（本レーンで実際に踏んだ不具合）。
      // 残存メンバーの中の最古参 _seq へ更新し、`_insertBySeq` で bodyEl 上の正しい兄弟位置へ
      // 枠自体を移動し直す。バケットの全メンバーは `_placeLeaf` が必ず数値の `_seq` を刻む
      // 契約——欠けている／空なら実装がどこかで契約を破っている不具合であり、古い `_seq` を
      // 残したまま黙って処理を続けない（`Math.min()` の空/非数値混入による無音の `Infinity` 化
      // を防ぐ）。
      const remainingSeqs = Array.from(b.aggBody.children)
        .filter((x) => x !== entry.el)
        .map((x) => x._seq);
      if (!remainingSeqs.length || remainingSeqs.some((s) => typeof s !== 'number')) {
        throw new Error('TraceTreeV2._detachFromBucket: 集約枠の残存メンバーに _seq が無い');
      }
      b.aggEl._seq = Math.min(...remainingSeqs);
      this._insertBySeq(entry.lane.bodyEl, b.aggEl);
      b.aggEl.querySelector('.fagg-head').textContent = `${b.label}×${b.count}`;   // textContent＝XSS安全
    }
  }
  // `el._seq`（到着順の通し番号）をもとに、bodyEl の直接の子の中で正しい兄弟位置へ挿入する。
  // 集約枠の解体で複数要素をまとめて枠の旧位置へ戻すと、枠の外側に挟まった別種ノードとの
  // 到着順が壊れる（本レーンで実際に踏んだ不具合）ため、要素ごとに自分の到着順が指す位置へ
  // 個別に戻す。`_seq` を持たない子（`live` 時の「考え中」プレースホルダ等・アプリの UI chrome
  // であって到着順管理の対象外）は比較から除外する。
  _insertBySeq(bodyEl, el) {
    let ref = null;
    for (const child of bodyEl.children) {
      if (child === el) continue;
      if (typeof child._seq === 'number' && child._seq > el._seq) { ref = child; break; }
    }
    if (ref) bodyEl.insertBefore(el, ref); else bodyEl.appendChild(el);
  }
  // `parentId` を親に持つ子で、親がまだ届いていなかったため一時的にレーン直下へ置いていたものを、
  // 親が届いた今の時点で実 DOM 要素ごと子コンテナへ付け替える。移動前に必ず `_detachFromBucket`
  // で集約バケットの帳簿を清算する（`appendChild` 自体は既存の親から自動的に DOM 上は detach
  // するが、バケット側の JS 上の参照は別途消さないと残り続ける）。
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
      // 親が既に存在する＝そのまま子コンテナへネストする。集約（同種操作の件数畳み込み）は
      // レーン直下の並びだけを対象にする既存契約のまま拡張しない（ネスト先での集約は本対応の
      // スコープ外＝常に個別表示する）。
      const el = _buildLeafElV2(e);
      this._childrenContainerOf(parentEntry).appendChild(el);
      this.nodesById.set(e.id, { lane, el, bucketKey: null });
      this._attachPendingChildren(e.id);
      return;
    }
    // 親が無い（parent_id 自体が無い）、または親がまだ届いていない＝従来どおりレーン直下へ置く
    // （集約の対象はここだけ）。`parentId` があるのに親が見つからない場合は、後で親が届いた時に
    // 付け替えられるよう pending へ登録する。
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
    el._seq = this._seq++;   // 集約/解体をまたいでも本来の到着順を覚えておく（_insertBySeq 参照）
    // `_detachFromBucket` が閾値未満への縮小時に集約枠を必ず解体する（`b.aggEl=null`/
    // `b.leafEls` を実配列へ復元）ため、「初めて閾値に到達したか」は `b.count === AGG_MIN_RUN`
    // という値比較だけで判定してよい（`b.aggEl` の有無で場合分けする必要は無い）。
    if (b.count < AGG_MIN_RUN) {
      lane.bodyEl.appendChild(el);
      b.leafEls.push(el);
    } else if (b.count === AGG_MIN_RUN) {
      const anchor = b.leafEls[0];
      const det = document.createElement('details');
      det._seq = anchor._seq;   // 枠が代表する最も古い到着順（他バケットの解体時の比較対象になる）
      det.className = 'fagg';
      det.innerHTML = '<summary class="fagg-head"></summary><div class="fagg-body"></div>';
      lane.bodyEl.insertBefore(det, anchor);
      const body = det.querySelector('.fagg-body');
      b.leafEls.forEach((x) => body.appendChild(x));
      body.appendChild(el);
      b.aggEl = det; b.aggBody = body; b.leafEls = null;
      det.querySelector('.fagg-head').textContent = `${b.label}×${b.count}`;   // textContent＝XSS安全
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
// 終了理由: `answer.data.evidence_packet.stop_reason` を**唯一の根拠**にする
// （trace 配列を漁って推測しない）。`stopInfo` の形は `{text, interrupted}`——`interrupted`
// （true/false）が `TraceTreeV2.finalize` の「レーンを完了/中断のどちらにするか」の唯一の
// 判定材料。回答へ実際にたどり着いた経路（evidence_packet 経由）は既知/未知の stop_reason を
// 問わず常に `interrupted:false`、SSE の終端イベント種別（stopped/timeout/error）だけが
// `interrupted:true`（未知の値を誤って「中断」扱いにしない）。
const STOP_REASON_UNKNOWN_TEXT = '終了理由を確認できませんでした';
// SSE 終端イベント種別（stream.js 側で判定・evidence_packet を経由しない＝常に「中断」扱い）。
const SSE_STOP_REASON_LABEL = { stopped: '停止操作', timeout: '期限', error: 'エラー' };
export function stopReasonInfo(category) {
  return { text: SSE_STOP_REASON_LABEL[category] || STOP_REASON_UNKNOWN_TEXT, interrupted: true };
}
// chat_turns.py の reaper が発行する固定文言（"応答がタイムアウトしました。..."）で判定する
// （新しいエラーコードを作らず、既存の利用者向け文言をそのまま再利用する）。
export function stopReasonCategoryFromError(message) {
  return /タイムアウト/.test(message || '') ? 'timeout' : 'error';
}
// stop_reason のクローズド語彙（agentic_search.py／providers/base.py が実際に設定する値）→
// 表示文言。同じ「上限到達」系でも何の上限かで文言を変える。対応表に無い値は
// STOP_REASON_UNKNOWN_TEXT（「終了理由を確認できませんでした」）へ落ちる（未知＝不明・誤断定しない）。
// `Object.create(null)`（VERIFICATION_BADGE_LABEL と同じ理由・サーバ由来の stop_reason が
// `"constructor"` 等だった場合に継承プロパティを誤って引き当てない）。
const STOP_REASON_TOKEN_LABEL = Object.assign(Object.create(null), {
  no_tool_calls: '自然終了', evaluation_sufficient: '自然終了',
  // `unknown`（agentic_search.py::_incomplete_stop_reason の理由欠落/非文字列/未知値の正規化先）は
  // 対応表に無い値と同じ文言を明示的なキーとして持つ（STOP_REASON_UNKNOWN_TEXT と同一文字列——
  // 語彙一致テストが対応表のキーをリテラル文字列として抽出するため、変数参照ではなく直書きする）。
  unknown: '終了理由を確認できませんでした',
  turns_exhausted: '調査の上限に到達', budget_exceeded: '調査の上限に到達',
  tools_per_turn_exceeded: '道具の使用回数の上限に到達',
  evaluation_blocked: '根拠不足で中断', evidence_verification_failed: '根拠不足で中断',
  refusal: 'AI が回答を控えた',
  truncated: '出力上限で途中終了', content_filtered: '内容の制限で終了',
});
function _stopReasonText(raw) {
  if (!raw || typeof raw !== 'string') return STOP_REASON_UNKNOWN_TEXT;
  return STOP_REASON_TOKEN_LABEL[raw] || STOP_REASON_UNKNOWN_TEXT;
}
// STOP-1: 調査予算（ターン数／呼び出し予算／1応答あたりの道具の使用回数）到達で打ち切られた
// ターン——本文が「途中までの結果」であることに利用者が気づけるよう、答弁本文とは別に注記を出す
// 対象の stop_reason（根拠不足・出力上限・安全フィルタ・回答拒否・理由不明は対象外＝それぞれ別の
// 意味を持つため、この注記の「範囲を絞る／続きを調べて」という案内は当てはまらない）。
const BUDGET_EXHAUSTED_STOP_REASONS = new Set(['turns_exhausted', 'budget_exceeded', 'tools_per_turn_exceeded']);
const BUDGET_NOTE_TEXT = '調査の上限に達したため、途中までの結果で答えています。'
  + '範囲（フォルダ）を絞るか、もう一度「続きを調べて」と送ると続きから調べられます。';
export function deriveTraceStopReason(answer) {
  // clarify（確認カード）は Evidence Packet を持たない正常な一時停止——「終了理由」の対象外
  // （履歴の確認カードでも同じ判定にする）。
  if (!answer || answer.lens === 'clarify') return null;
  const raw = answer.data && answer.data.evidence_packet && answer.data.evidence_packet.stop_reason;
  // evidence_packet 経由＝どの結果でも回答の合成まで到達している（既知/未知を問わず「中断」ではない）。
  return { text: _stopReasonText(raw), interrupted: false };
}
// 履歴復元（静的）: 配列を順に流し込むだけ（ライブ時と同じ addOrUpdate を使う＝描画経路の共通化）。
// 完了済みターンなので終了理由も併記する（利用者決定「履歴再表示では最終状態のカードとして同じ
// 情報を表示」）。
function _renderTraceStepsV2(container, trace, answer) {
  const tree = new TraceTreeV2(container, { live: false });
  (trace || []).forEach((e) => tree.addOrUpdate(e));
  tree.finalize(deriveTraceStopReason(answer));
}

// 「実行の分担」サマリ: ローカル/社内サーバ/クラウド AI のどちらが何回担当したかを
// trace（v2）＋ answer.usage から集計する。新しいサーバ側集計は作らない（フロントの既存
// フィールド読み取りのみ）。配置の判定はサーバの権威ある `is_local` をそのまま使う
// （フロントで provider 文字列から推測しない）。`answer.usage` が無い/provider が無いときは
// 「回答の合成」の担当を**誤断定せず**「担当不明」の1件として計上する
// （usage 欠落＝いずれの配置でもなく「不明」）。
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
    // `agent_completed`（下調べ役が1ステップを終えた合図）は担当バッジ表示用に metrics を持つが、
    // それ自体は新しい作業ではなく既に集計済みの作業の完了通知なので、ここでは数えない
    // （数えると「その他の処理」に実体のない1回が水増しされる）。
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
    else bump(null, null, null, '回答の合成');   // usage 欠落＝担当不明（「すべてローカル」等への誤断定を防ぐ）
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
  // 「すべて...」への縮退は、判定が確定している（locality が既知の4値のどれか）単一バケットの
  // ときだけ行う（担当不明の1件だけで「すべて◯◯」と言い切らない＝誤断定しない）。
  if (summary.length === 1 && LOCALITY_LABEL[summary[0].locality]) {
    return `<div class="provider-summary muted">🧭 すべて${LOCALITY_LABEL[summary[0].locality]} AI が担当しました</div>`;
  }
  const parts = summary.map((b) => {
    const ops = [...b.counts.entries()].map(([k, n]) => `${esc(k)} ${n} 回`).join('・');
    return `${_summaryWhoLabel(b)}が${ops}を担当`;
  });
  return `<div class="provider-summary muted">🧭 ${parts.join('／')}</div>`;
}

// UIフィードバック（2026-07-03）: 1ターン分の見出し（質問文40字＋時刻）＋（trace があれば）折りたたみ本体。
// trace が無いターンは折りたたみにせず「（記録なし）」だけを見出しに出す（開閉しても何も出ない空表示を避ける）。
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
// UIフィードバック（2026-07-03）: 会話ロード時、右ペインへ全ターンを時系列で積み上げ表示（最新だけ展開）。
export function renderTurnStack(turns) {
  const flow = $('flow'); flow.innerHTML = ''; S.nodes = {};
  turns.forEach((t, i) => flow.appendChild(_buildTurnEl(t, `fturn-${i}`, i === turns.length - 1)));
  S.turnSeq = turns.length; S.liveTurnId = null;
  setRt('過去の記録', false);
}
// 回答カードに「この回答の思考の流れ」ボタンを添える（trace があるターンだけ・既存 .copybtn の見た目を流用）。
// クリックで右ペインの該当ターン（turnId＝#fturn-N）を展開してスクロール（統合表示・別表示への切替はしない）。
export function attachTraceButton(el, turnId) {
  el._turnId = turnId;
  const body = el.querySelector('.a-body'); if (!body) return;
  const btn = document.createElement('button');
  btn.type = 'button'; btn.className = 'copybtn'; btn.dataset.showtrace = '1';
  btn.textContent = '🕓 この回答の思考の流れ';
  body.appendChild(btn);
}

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
  // LOW fix: 個人ファイル内ヒットを別枠で表示（esc() でサニタイズ・DL リンクなし）。
  // _personal_citations() は {doc_id, quote, source} 形式で返す。
  const srcs = (personal_sources || []).filter((s) => s && s.doc_id);
  if (!srcs.length) return '';
  const items = srcs.map((s) =>
    `<li><span class="src-name">${esc(s.doc_id)}</span>`
    + (s.quote ? `<pre class="src-snippet">${esc(String(s.quote).slice(0, 200))}</pre>` : '')
    + '</li>').join('');
  return `<div class="personal-sources"><div class="personal-sources-h">🗂 個人ファイル内ヒット（本人のみ・共有不可）</div><ul>${items}</ul></div>`;
}
function renderCreatedFiles(created_files) {
  // P1-c（Codex 強化計画 Phase1）: Codex が authoring 直下に作成→files/ 登録したファイルの DL カード。
  // data-dl は #messages の既存委譲ハンドラ（fetch→blob→Sherpa.downloadBlob）にそのまま乗る
  // （リンクテキストに絵文字を付けない＝dl.textContent をそのままファイル名として使う既存ロジックと整合）。
  const files = (created_files || []).filter((f) => f && f.name && f.download_url);
  if (!files.length) return '';
  const items = files.map((f) =>
    `<li><a href="${esc(f.download_url)}" data-dl>${esc(f.name)}</a></li>`
  ).join('');
  return `<div class="created-files"><div class="created-files-h">📎 作成したファイル</div><ul>${items}</ul>`
    + `<a href="workspace.html" class="created-files-link">マイワークスペースで開く</a></div>`;
}
// F3（2026-07-07）: トークン使用量のターン末尾表示。usage が無いターンは何も出さない。
// クリック（<details>＝ネイティブ・JS 委譲不要）で内訳（キャッシュ/推論）を展開する。
// 数値はサーバ由来だが number 化して埋め込む＝XSS 安全。model 名は esc()。
// （金額換算は撤去・2026-07-08 フィードバック⑦＝入力/出力トークン数のみ表示）。
function _fmtTokensCompact(n) {
  n = Math.max(n | 0, 0);
  if (n >= 1000000) return (n / 1000000).toFixed(n >= 10000000 ? 0 : 1).replace(/\.0$/, '') + 'M';
  if (n >= 1000) return Math.round(n / 1000) + 'k';
  return String(n);
}
function usageMetaHTML(u) {
  if (!u || typeof u !== 'object') return '';
  const inC = _fmtTokensCompact(u.input_tokens), outC = _fmtTokensCompact(u.output_tokens);
  const cached = u.cached_input_tokens ? `（うちキャッシュ ${(u.cached_input_tokens | 0).toLocaleString()}）` : '';
  const reason = u.reasoning_output_tokens ? `（うち推論 ${(u.reasoning_output_tokens | 0).toLocaleString()}）` : '';
  return `<details class="usage-meta"><summary>🪙 ${esc(inC)} in / ${esc(outC)} out</summary>`
    + `<div class="usage-detail"><div>入力トークン: ${(u.input_tokens | 0).toLocaleString()} <span class="muted">${cached}</span></div>`
    + `<div>出力トークン: ${(u.output_tokens | 0).toLocaleString()} <span class="muted">${reason}</span></div>`
    + '</div></details>';
}
// S4-e（複数プロファイル並用・UI表示・§6.3）: サブループ（下調べ役等）のトークン使用量を
// プロファイル別に additive 表示する。`answer.usage_subs`（複数・S4 プランナ実行時）と
// `answer.usage_sub`（単一・S3 以来のハイブリッド）のどちらも見る（無ければ何も出さない＝
// 旧メッセージ・非プランナ経路は従来どおり何も出ない）。折りたたみは既存 usage-meta の流儀
// （`<details>`＋`.usage-detail`）を流用し、行の文言は平文（`docs/04-画面の原則.md`）。
// profile はサーバが `name`（表示名）優先で組む文字列（無い場合だけ内部 slug＝profile_id へ
// フォールバックする・providers/base.py 参照）。esc() は必須（生値は信頼しない）。
function usageSubMetaHTML(answer) {
  // S4-e RV 是正（LOW）: 本体契約は排他（実行1件→usage_sub 単数のみ・2件以上→usage_subs のみ＝
  // 両キーは共存しない）。壊れた保存データで両キーが共存した場合の順位はこう決める:
  // usage_subs が2件以上→情報量の多い usage_subs 側が正（1件分しか持てない usage_sub を採ると
  // 内訳が欠落する）／usage_subs が1件以下→S3 互換の usage_sub 単数側。usage_subs が1件だけで
  // usage_sub が無い変則データはその1件を表示する。
  const subs = Array.isArray(answer.usage_subs) ? answer.usage_subs : [];
  const list = subs.length >= 2 ? subs : (answer.usage_sub ? [answer.usage_sub] : subs);
  if (!list.length) return '';
  const rows = list.map((u) => {
    const name = esc((u && u.profile) || '下調べ役');
    const inN = ((u && u.input_tokens) | 0).toLocaleString();
    const outN = ((u && u.output_tokens) | 0).toLocaleString();
    return `<div>${name}: 入力 ${inN} / 出力 ${outN} トークン</div>`;
  }).join('');
  const summary = list.length > 1 ? `🧭 下調べ役の使用量（${list.length}件）` : '🧭 下調べ役の使用量';
  return `<details class="usage-meta usage-sub-meta"><summary>${summary}</summary>`
    + `<div class="usage-detail">${rows}</div></details>`;
}
// 回答ごとの利用者フィードバック（👍/👎＋定型タグ＋任意の一言）。押下時の送信・上書きは
// chat.js の #messages 委譲（data-fb/data-fb-send）が担う（送信先は el._messageId＝appendAnswer/
// finalizeAnswer が呼び出し元から渡す assistant メッセージ id）。`feedback`（{rating,tags,comment}）
// を渡すと、会話履歴の復元時に前回の選択状態（押下済みボタン・👎ならタグ/一言を開いた状態）を
// 再現する（上書き前に現在値を見せる）。ライブ回答（feedback 省略）は常に未選択の初期表示。
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
function answerHTML(answer, trace, feedback) {
  // personal_sources がある場合は全レンズで末尾に追加する。
  const personalHTML = renderPersonalSources(answer.personal_sources);
  const usageHTML = usageMetaHTML(answer.usage);   // F3: ターン末尾のトークン使用量（無ければ空）
  const usageSubHTML = usageSubMetaHTML(answer);   // S4-e: プロファイル別内訳（無ければ空・additive）
  const feedbackHtml = feedbackHTML(feedback);
  // trace_version=2 のターンだけ「実行の分担」（ローカル/クラウド AI のどちらが何を担当したか）
  // を出す（v1 は従来どおり何も出さない）。
  const summaryHTML = (answer.trace_version === 2)
    ? _providerSummaryHTML(_computeProviderSummary(trace, answer)) : '';
  if (answer.lens === 'chat') {   // ナレッジ参照オフ＝素の会話（出典枠なし・通常チャット表示）
    return `<div class="chips"><span class="chip ghost">💬 通常チャット（ナレッジ参照オフ）</span></div>`
      + `<div class="headline">${mdLite(answer.headline)}</div>`
      + personalHTML + summaryHTML + usageHTML + usageSubHTML
      + '<button class="copybtn" data-copy>⧉ コピー</button><button class="copybtn" data-export>⬇ 書き出し</button>'
      + feedbackHtml;
  }
  const chip = `<div class="chips"><span class="chip">${esc(LENS_LABEL[answer.lens] || answer.lens)}</span>`
    + _scopeChipsHTML(answer.scope)
    + ((answer.route && answer.route.path) || []).map((p) => `<span class="chip">${esc(p)}</span>`).join('')
    + _depthHeaderHTML(answer.scope, answer.duration_ms) + '</div>';
  // impact はグラフ由来（items/presumed）と反復ツール検索由来（citations）の2形がある。
  // グラフ結果が無い回答は QA と同じ引用表示へ落とす（本文だけで根拠が見えない状態を作らない）。
  const impactHasGraph = !!(answer.data && ((answer.data.items || []).length || (answer.data.presumed || []).length));
  const body = (answer.lens === 'impact' && impactHasGraph) ? renderImpact(answer)
    : answer.lens === 'troubleshoot' ? renderTrouble(answer) : renderQa(answer);
  // 検証バッジ（verification_method 別）は trace_version=2 の回答に限定する
  // （v1・version 欠落の回答は従来どおり EV-0 の根拠/参考2区分のみ＝byte-identical を保つ）。
  const evidencePacketForBadges = answer.trace_version === 2 ? (answer.data && answer.data.evidence_packet) : null;
  return chip + `<div class="headline">${mdLite(answer.headline)}</div>`
    + budgetNoteHTML(answer.data && answer.data.evidence_packet) + codexTimeoutNoteHTML(answer)
    + retryHintsHTML(answer.retry_hints) + body
    + refGraphHTML(answer) + renderCreatedFiles(answer.created_files)
    + renderSources(answer.sources, answer.sources_verified, evidencePacketForBadges) + personalHTML
    + summaryHTML + usageHTML + usageSubHTML
    + '<button class="copybtn" data-copy>⧉ コピー</button><button class="copybtn" data-export>⬇ 書き出し</button>'
    + feedbackHtml;
}

// 調べ方ブロック（SC-6b §2.5・§3.2）: 回答ヘッダに使った範囲・探す対象を1つずつチップで示す。
// 層フィルタが実効しないレンズ（impact/troubleshoot）は `layer_applied:false` を明示し、
// 「非適用」と注記する（黙って無視しない・docs/04 §5 の作法）。
const LAYER_CHIP_LABEL = { both: '資料＋コード', docs: '資料のみ', code: 'コードのみ' };
function _scopeChipsHTML(scope) {
  if (!scope) return '';
  const paths = scope.scope_paths || [];
  const scopeLabel = paths.length ? paths.map((p) => S.scopeLabels[p] || p.split('/').pop()).join('・') : '全体';
  let out = `<span class="chip">${esc(scopeLabel)}</span>`;
  if (scope.layer) {
    const layerLabel = esc(LAYER_CHIP_LABEL[scope.layer] || scope.layer);
    out += scope.layer_applied === false
      ? `<span class="chip ghost" title="この調べ方（影響・原因）では探す対象の指定は使われません">${layerLabel}（非適用）</span>`
      : `<span class="chip">${layerLabel}</span>`;
  }
  return out;
}

// 調べる深さ＋所要時間（SC-6c・調べ方ブロック §3.2）: 「調べる深さ: 深く・所要 4分12秒」のように
// 回答ヘッダへ1チップで示す。`scope.depth_profile` が無い（SC-6c 導入前の旧回答）ときは何も出さない
// （`_scopeChipsHTML` の `scope.layer` 欠落時と同じ後方互換の作法）。`duration_ms`（LOG-1a）が
// 無ければ調べる深さだけを出す。
const DEPTH_CHIP_LABEL = { standard: '標準', deep: '深く', max: '最大' };
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

// 出典0件時の再検索案内（SC-6d・調べ方ブロック §5）。押すと該当設定を広げて同じ質問を再送する
// （委譲は chat.js の #messages リスナー・data-retry-action は JSON 文字列＝action オブジェクト）。
function retryHintsHTML(hints) {
  if (!hints || !hints.length) return '';
  return '<div class="retry-hints">' + hints.map((h) =>
    `<button class="retry-hint-btn" data-retry-kind="${esc(h.kind)}" data-retry-action="${esc(JSON.stringify(h.action || {}))}">${esc(h.label)}</button>`
  ).join('') + '</div>';
}

// STOP-1: 調査予算到達（turns_exhausted/budget_exceeded/tools_per_turn_exceeded）で打ち切られた
// ターンに、本文とは別要素として注記を出す（サーバは本文へ文字列連結しない＝`evidence_packet.
// stop_reason` だけを根拠にフロント側で判定する・`deriveTraceStopReason` と同じ唯一の根拠）。
function budgetNoteHTML(evidencePacket) {
  const raw = evidencePacket && evidencePacket.stop_reason;
  if (!BUDGET_EXHAUSTED_STOP_REASONS.has(raw)) return '';
  return `<div class="budget-note">${esc(BUDGET_NOTE_TEXT)}</div>`;
}

// Codex CLI 実行がタイムアウトで打ち切られ、進行中の宣言文がそのまま headline に残ったターン
// （`chat_service._is_codex_timed_out_partial` が根拠・`answer.codex_timed_out`）。STOP-1 の
// budget-note と同じ「headline 直下の独立要素」形式で注記する——`evidence_packet.stop_reason` の
// 閉じた語彙とは無関係の別マーカー（Codex CLI は agentic_search を経由しない別の実行系のため）。
const CODEX_TIMEOUT_NOTE_TEXT = '調査の時間上限に達したため途中までの結果です。'
  + '「続きを調べる」を押すと続きから調べられます。';
function codexTimeoutNoteHTML(answer) {
  if (!answer || !answer.codex_timed_out) return '';
  return `<div class="budget-note">${esc(CODEX_TIMEOUT_NOTE_TEXT)}</div>`;
}

// 回答が参照したノード/関係から小さな部分グラフを組む（impact=経路、troubleshoot=近傍チェーン）
function subgraphFromAnswer(a) {
  const items = a.lens === 'impact' ? ((a.data && a.data.items) || [])
    : a.lens === 'troubleshoot' ? ((a.data && a.data.candidates) || []) : [];
  const nodes = new Map(), edges = new Set(), SEP = '';
  const add = (name) => { if (name && !nodes.has(name)) nodes.set(name, { name }); return nodes.get(name); };
  const ts = a.lens === 'troubleshoot';   // 経路向きが逆: impact=affected→…→起点 / troubleshoot=起点(anchor)→…→候補
  for (const it of items) {
    if (it.name) add(it.name).affected = true;
    const path = (ts ? it.path : it.trace) || [];   // 鏡: impact=trace(ノード名列) / troubleshoot=path
    path.forEach((nm, k) => { add(nm); if (k > 0 && path[k - 1] && nm) edges.add(path[k - 1] + SEP + nm); });
    if (path.length) add(path[ts ? 0 : path.length - 1]).start = true;   // ts は先頭=起点／impact は末端=起点
  }
  return { nodes: [...nodes.values()], edges: [...edges].map((e) => e.split(SEP)) };
}
function refGraphHTML(answer) {   // #5: 折りたたみで「参照したナレッジグラフ」を出す（impact/troubleshoot）
  if (answer.lens !== 'impact' && answer.lens !== 'troubleshoot') return '';
  const sub = subgraphFromAnswer(answer);
  if (sub.nodes.length < 2) return '';
  return `<div class="refgraph"><button class="refgraph-h" data-rg="${esc(JSON.stringify(sub))}">`
    + `🕸 参照したナレッジグラフ（${sub.nodes.length} ノード・${sub.edges.length} 関係）<span class="caret">▾</span></button>`
    + '<div class="refgraph-body" hidden></div></div>';
}
export function initRefGraph(el, sub) {   // 小さな部分グラフを cytoscape で描く（起点=オレンジ大／影響=teal）
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
export function appendAnswer(answer, messageId, trace, feedback) {
  const el = answer ? appendAssistantRaw(answerHTML(answer, trace, feedback)) : appendAssistantRaw('<div class="muted">（内容なし）</div>');
  if (el && answer) el._answer = answer;   // 回答単位エクスポート用に元データを保持（#6）
  if (el && messageId != null) el._messageId = messageId;   // フィードバック送信先
  return el;
}

// 最終回答の段階表示（OpenAI/Ollama=本物のトークン / Codex・簡易=一括を段階描画）
let _revPending = '', _revTimer = null;   // 逐次描画（answer_delta）専用・reveal/clearReveal 内で完結（単一ドメイン。ansEl/ansHead は S へ集約済み）
export function clearReveal() { if (_revTimer) clearInterval(_revTimer); _revTimer = null; _revPending = ''; }
export function reveal(text) {
  _revPending += text;
  if (_revTimer || !S.ansHead) return;
  // 段階描画のテンポ（§3）: Codex は一括到着＝『残り÷N』だと先頭で大量ダンプ→末尾トリクルで不自然。
  // チャンクに上限を設けて**ほぼ一定ペースのタイピング感**にする（OpenAI/Ollama の本物トークンは
  // 毎回 _revPending が小さく n=2 で従来どおり）。柔らかい表示（soft）寄り。
  _revTimer = setInterval(() => {
    if (!_revPending) { clearInterval(_revTimer); _revTimer = null; return; }
    const n = Math.max(2, Math.min(6, Math.ceil(_revPending.length / 40)));
    S.ansHead.textContent += _revPending.slice(0, n); _revPending = _revPending.slice(n); scroll();
  }, 20);
}
export function ensureAnswerCard(thinking) {
  if (S.ansEl) return;
  if (thinking) thinking.remove();
  S.ansEl = appendAssistantRaw('<div class="headline"></div>');
  S.ansHead = S.ansEl.querySelector('.headline');
}
export function finalizeAnswer(thinking, answer, turnId, messageId, trace) {
  clearReveal();
  let el;
  if (!S.ansEl) {
    if (thinking) thinking.remove();
    el = appendAnswer(answer, messageId, trace);
  } else {
    S.ansEl.querySelector('.a-body').innerHTML = answerHTML(answer, trace);   // 段階表示の見出し→確定（本体/出典を補完）
    S.ansEl._answer = answer;   // 回答単位エクスポート用（#6）
    if (messageId != null) S.ansEl._messageId = messageId;   // フィードバック送信先
    el = S.ansEl;
    S.ansEl = null; S.ansHead = null;
  }
  // 積み上げ表示（2026-07-03）: このターンに trace があれば遡及ボタンを添える。
  if (turnId && el) attachTraceButton(el, turnId);
}
function statusTag(s) {
  return STATUS_CLASS[s] ? `<span class="statustag ${STATUS_CLASS[s]}">${esc(STATUS_LABEL[s])}</span>` : '';
}
// #2: 経路チップ列（実データ＝trace のノード名列。並びは affected→…→起点。起点=末尾を強調）
function impactRouteChipsHTML(trace) {
  if (!trace || !trace.length) return '';
  return trace.map((n, i) => {
    const arr = i ? '<span class="arr">←</span>' : '';
    return arr + `<span class="chip${i === trace.length - 1 ? ' origin' : ''}">${esc(n)}</span>`;
  }).join('');
}
function renderImpact(a) {
  const items = (a.data && a.data.items) || [];
  const presumed = (a.data && a.data.presumed) || [];   // 実影響0件時の「資料からの関連推定」
  if (!items.length && !presumed.length) return '';
  const lis = items.map((it) => {
    const trace = it.trace || [];                      // 鏡: 影響の経路はノード名列 trace
    const chain = trace.join(' ← ');
    // 実データのみ: evidence は {doc, line}（quote は presumed のみが持つ・無い物は作らない）
    const evText = (it.evidence || []).filter((e) => e.doc)
      .map((e) => esc(e.doc) + (e.line ? `〔行 ${esc(e.line)}〕` : '')).join(' / ');
    const hasDetail = !!(chain || evText);
    // #2: 詳細（経路チップ＋根拠）は行データにある物だけで構成。トグルは行全体（role=button・キーボード対応は委譲側）
    const detail = hasDetail
      ? `<div class="ixdetail">${trace.length ? `<div class="ix-route">${impactRouteChipsHTML(trace)}</div>` : ''}`
        + `<div class="path"><div class="chain">${esc(chain)}</div>${evText ? `<div class="ev">根拠: ${evText}</div>` : ''}</div></div>`
      : '';
    const topAttrs = hasDetail ? ' role="button" tabindex="0" aria-expanded="false" data-toggle' : '';
    const toggle = hasDetail ? '<span class="pathbtn" aria-hidden="true">経路 <span class="caret">▾</span></span>' : '';
    // 担当アナライザの来歴（コード以外/L由来は analyzer=null＝出さない・§7 裁定2の受入条件）。
    const analyzerNote = it.analyzer ? `<small>（解析: ${esc(analyzerLabel(it.analyzer))}）</small>` : '';
    return `<li><div class="top"${topAttrs}>`
      + `<span class="kind">${esc(it.category)}</span><span class="nm">${esc(it.name)}${analyzerNote}</span>${statusTag(it.status)}`
      + `<span class="spacer"></span>${toggle}</div>${detail}</li>`;
  }).join('');
  const ilist = items.length ? `<ul class="ilist">${lis}</ul>` : '';
  let pres = '';
  if (presumed.length) {                              // 実影響が無いとき: 資料から辿った関連コードを推定として
    const pl = presumed.map((p) => {
      const e0 = (p.evidence || [])[0] || {};
      const q = e0.quote ? `<div class="path"><div class="ev">根拠: ${esc(e0.quote)}${e0.doc ? `〔${esc(e0.doc)}〕` : ''}</div></div>` : '';
      return `<li><div class="top"><span class="conf warn"><span class="d"></span>推定</span>`
        + `<span class="kind">${esc(p.category)}</span><span class="nm">${esc(p.name)}</span></div>${q}</li>`;
    }).join('');
    pres = '<div class="muted" style="margin:6px 0 2px">実影響は登録されていません。資料からの関連（推定）:</div>'
      + `<ul class="ilist">${pl}</ul>`;
  }
  return ilist + pres;
}
function renderTrouble(a) {
  return ((a.data && a.data.candidates) || []).slice(0, 8).map((c) =>
    `<div class="cand"><span class="nm">${esc(c.name)}</span><span class="role">${esc(c.role)}</span>`
    + (c.path && c.path.length ? `<div class="chain">${esc(c.path.join(' → '))}</div>` : '') + '</div>').join('');
}
// UI フィードバック2（2026-07-03）: 引用カードは既定で折りたたみ（件数だけ見える見出し）。
// refGraphHTML と同じ「見出しボタン＋hidden な本体」パターン（#5 で確立済み・アニメ控えめ）を流用する。
function renderQa(a) {
  const cites = (a.data && a.data.citations) || [];
  if (!cites.length) return '';
  const items = cites.map((c) =>
    `<div class="cite"><span class="doc">${esc(c.doc_id)}</span><span class="sp">行 ${esc(c.span && c.span[0])}–${esc(c.span && c.span[1])}</span><pre>${esc(c.quote)}</pre></div>`).join('');
  return `<div class="cites"><button class="cites-h" type="button" data-cites aria-expanded="false">`
    + `📄 該当箇所 (${cites.length})<span class="caret">▾</span></button>`
    + `<div class="cites-body" hidden>${items}</div></div>`;
}
function renderSources(sources, verifiedDocIds, evidencePacket) {   // 04-画面の原則.md §4: 出典は常に表示（0件でも明示）
  if (!sources || !sources.length) {
    return `<div class="sources"><div class="h">出典（原本をダウンロード）</div>`
      + '<span class="muted" style="font-size:12px">確証のある資料は見つかりませんでした</span></div>';
  }
  // EXT-4（拡張設計 §10「検証バッジ」）: Evidence Packet の verification_method（あれば）を doc_id ごとに
  // 添える。無い回答（Packet を持たない/該当エントリが無い）は従来どおりバッジ無し（byte-identical）。
  const vmap = _verificationMethodByDoc(evidencePacket);
  const link = (s) => `<a href="${esc(s.download_url)}" data-dl>📄 ${esc(s.doc_id)}</a>${verificationBadgeHTML(vmap.get(s.doc_id))}${importanceBadgeHTML(s)}`;
  // EV-0（拡張設計 §4.4）: agentic 経路の回答は answer.sources_verified（read_around で実際に精読した
  // doc_id の集合）を持つ。あれば出典を「根拠（精読済み）」／「参考（ヒットのみ）」の2区分に分ける
  // （除外はしない＝recall 不変・表示の誠実化のみ）。持たない回答（impact/troubleshoot 等）は
  // 従来どおり単一リストのまま（byte-identical）。
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

