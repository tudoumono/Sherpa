// システム管理（全体設定・admin 専用）。GET/PUT /admin/settings。
// 設計: docs/design/settings.md「管理画面（システム管理）の設定」
// 認可の正は API 側の _require_admin。ここでの #access-denied 表示は UX のみ。
'use strict';
const $ = Sherpa.$, esc = Sherpa.esc, getJSON = Sherpa.getJSON, api = Sherpa.api;

// アーム名 → 平文の説明（専門用語ゼロ）。未知アームは名前をそのまま出す。
const ARM_LABELS = {
  ooxml: {
    label: 'Office 文書から直接読み取り',
    desc: 'Word / Excel / PowerPoint（.docx / .xlsx / .pptx など）を直接テキスト化します（標準・推奨）。',
  },
  pdf_text: {
    label: 'PDF の文字を抽出',
    desc: 'PDF に埋め込まれた文字を抽出します。画像だけの PDF は、この方式だけでは読み取れません（OCR や視覚読み取りが有効なら読める場合があります）。',
  },
  vision: {
    label: '画像・スキャン文書を AI が見て読み取り（視覚読み取り）',
    desc: '画像やスキャンした文書を AI（視覚モデル）が見て読み取ります。読み取りには「プロバイダ＋接続先」タブの既定接続先の Ollama を使います。クラウド AI は下の「視覚読み取りの AI」で明示的に許可したときだけ使います。',
  },
};

// 未導入アームの導入案内（arms.available が false のとき arm-d に併記）。
const ARM_MISSING_HINT = {
  pdf_text: 'この環境では PDF 抽出ライブラリが見つかりません（通常は同梱されています。復旧: pip install pypdf）。',
  vision: '視覚読み取りに使う AI が使えません。下の「視覚読み取りの AI」設定を確認してください（クラウドを選ぶ場合は許可とキーが必要です）。',
};

// 旧形式（.doc/.xls/.ppt）の変換バックエンド → 平文の説明。「（既定）」は renderLegacy が実際の既定と一致する選択肢にだけ付ける。
const LEGACY_LABELS = {
  none: {
    label: '使わない',
    desc: '古い形式（.doc / .xls / .ppt）のファイルは読み取り対象にしません。',
  },
  libreoffice: {
    label: 'LibreOffice で変換',
    desc: '追加ソフト（LibreOffice）だけで動きます。図の配置がずれることがあります。',
  },
  office_com: {
    label: 'Office 連携',
    // 同一マシンなら設定不要（WSL 連携で Windows の Office を直接呼ぶ）。
    desc: '同じ Windows に Office があれば、そのまま使えます（設定不要）。別のマシンの Office を使うときだけ接続先を設定します。最も忠実に変換します。',
  },
};

// クラウド AI プロバイダ名 → 平文の説明。
const CLOUD_PROVIDER_LABELS = {
  openai: { label: 'OpenAI', desc: 'OpenAI API に直結します（Codex 経由の接続にも使われます）。' },
};
const CLOUD_KEY_SET_FIELD = { openai: 'openai_key_set' };

// OpenAI 互換 API の接続先（本家／Azure OpenAI／その他 OpenAI 互換）。
const OPENAI_ENDPOINT_KIND_LABELS = {
  openai: { label: 'OpenAI 本家' },
  azure: { label: 'Azure OpenAI' },
  custom: { label: 'その他 OpenAI 互換' },
};

// 使えるモデル（model_catalog）。用途名 → 平文の表示名。
const MC_USAGE_LABELS = {
  chat: '接続テスト用', intent: '依頼の仕分け',
  embed: '埋め込み', subsearch: '簡易回答のモデル', codex: 'Codex',
  render: '検索用文書の整形',
};
// クラウド選択かつキー未設定のとき。キーは「プロバイダ＋接続先」タブで設定する。
const VLM_KEY_MISSING_TEXT = 'クラウド（OpenAI）を選んでいますが、OpenAI の API キーが設定されていません。'
  + '「プロバイダ＋接続先」タブでキーを設定してください。設定するまで視覚読み取りは行われません。';
const MC_COLUMN_LABELS = { ollama: 'ローカル（Ollama）', codex: 'Codex' };

let _view = null;       // 直近の GET/PUT 応答（描画・保存の基準）
let _extKeys = [];        // 直近取得した外部連携キー一覧（GET /ext/v1/admin/keys の keys）

// ダーティ判定は render() 時点の基準値（baseline）と今の値の差で行う（元に戻せば PUT 対象から外れる）。
// 各タブの render 関数が対応する baseline を書き換える。書込専用の秘密（キー入力）は baseline が常に空文字。
let _cloudBaseline = { provider: 'openai', providerRaw: null, personalAllowed: false, webSearchAllowed: false, openaiDirectVisible: false, ollamaUrl: '' };

// cloud_provider だけは値差分でなく、ラジオが実際にクリックされたか（_cloudProviderTouched）で判定する。
// 実効値は既定込みで常に 'openai' のため、未選択と明示選択を値で区別できない（Ollama fallback の有無に関わる）。
// render() の度に false へ戻す。
let _cloudProviderTouched = false;

// raw が無い場合に加え、不正値のまま残っている場合も未確定として扱う。
function _normalizedProviderRaw() {
  return (_cloudBaseline.providerRaw || '').trim().toLowerCase() || null;
}

function cloudProviderNeedsExplicitSave(provider) {
  return _cloudProviderTouched && _normalizedProviderRaw() !== provider;
}
let _ollamaAllowlistBaseline = [];
let _webhookAllowlistBaseline = [];   // Webhook 宛先の SSRF allowlist（ollama_allowlist と同型）
let _openaiEndpointBaseline = { kind: 'openai', base_url: '', auth_header: 'bearer', api_version: '' };
// チャット画面のクイック入力例（chat_examples）の基準値。初回描画が {enabled, items} で上書きする。
let _chatExamplesBaseline = { enabled: true, items: [] };
let _armsBaseline = [];
let _legacyBaseline = null;
let _vlmBaseline = null;
// rag.md の LLM 成形トグルの保存済み実効値（真偽・PUT では "on"/"off" 文字列で送る）。
let _ragLlmRenderBaseline = true;
let _extKeysAllowedBaseline = false;
let _extKeysQuotaBaseline = '';
let _extKeysResearchProviderBaseline = 'ollama';
// 保存値が ollama/openai のどちらでもないときに select へ挿入する専用オプションの値（保存では送らない）。
const _RESEARCH_PROVIDER_INVALID = '__invalid__';

// 整数2項目の入力欄 id と PUT/GET のキー名対応（GET は view.depth_profile.<view>・PUT は body.<put>）。
const _DEPTH_BASE_FIELDS = [
  { view: 'grep_max_hits', put: 'depth_base_grep_max_hits', id: 'depth-base-grep-max-hits',
    label: '資料検索のヒット件数上限' },
  { view: 'read_window', put: 'depth_base_read_window', id: 'depth-base-read-window',
    label: '1回に読み取る前後の行数' },
];
let _depthProfileBaseline = {};    // put キー -> 文字列化した configured（''=未設定）
// configured を基準にする（''=未設定＝既定に従う）。
let _depthReasoningBaseline = '';
let _maxReviewRoundsBaseline = '';
let _codexWorkerModelBaseline = '';
let _codexSessionRetentionDaysBaseline = '';
// 素の Codex モード。configured を基準にする（''=未設定＝「標準」）。
let _codexModeBaseline = '';
let _embedParallelBaseline = '';   // 埋め込みの同時送信数
let _embedDimBaseline = '';   // 埋め込みの細かさ（''=既定）
let _embedProviderBaseline = '';   // 埋め込みの接続先（''=回答と同じクラウド・既定）

// 同時実行の上限（effective_limits）。_DEPTH_BASE_FIELDS と同型（GET は view.chat_max_turns.<view>・PUT は body.<put>）。
const _CHAT_MAX_TURNS_FIELDS = [
  { view: 'per_user', put: 'chat_max_turns_per_user', id: 'chat-max-turns-per-user',
    label: '同時に実行できる質問の数（1 人あたり）' },
  { view: 'global', put: 'chat_max_turns_global', id: 'chat-max-turns-global',
    label: '同時に実行できる質問の数（全員の合計）' },
];
let _chatMaxTurnsBaseline = {};    // put キー -> 文字列化した configured（''=未設定）

// 個人ファイル（workspace）。GET は view.workspace.<view>・PUT は body.<put>。上限は MB 単位で入力し（保存/GET は bytes・1MB=1048576）、境界でだけ変換する。
const _WORKSPACE_FIELDS = [
  { view: 'max_bytes', put: 'workspace_max_bytes', id: 'workspace-max-mb', label: '1 件あたりのアップロード上限',
    unit: 1048576, unitLabel: 'MB' },
  { view: 'ttl_days', put: 'workspace_ttl_days', id: 'workspace-ttl-days', label: '保持日数',
    unit: 1, unitLabel: '日' },
];
let _workspaceBaseline = {};    // put キー -> 表示単位での configured（''=未設定）

// Codex の MCP ツール結果1件あたりのバイト予算。入力欄は KB 単位（保存/GET は bytes・1KB=1024）。loBytes/hiBytes はサーバ側 Field(ge,le) と同じ範囲。
const _AGENTIC_BUDGET_FIELDS = [
  { view: 'per_result', put: 'agentic_budget_per_result', id: 'agentic-budget-per-result',
    label: 'ツール結果1件あたりの上限', loBytes: 1024, hiBytes: 8 * 1024 * 1024 },
];
let _agenticBudgetBaseline = {};   // put キー -> 文字列化した configured（KB・''=未設定）

let _mcState = {};       // 編集中の model_catalog（provider -> usage -> {allowed,default}）
let _mcBaseline = {};    // render() 時点の model_catalog（差分判定の基準）
let _mcBuiltin = {};     // 組み込み既定のみ（管理者設定を一切重ねない・差分強調の基準）
// 管理者が実際に保存した生値（model_catalog.configured・未設定なら null）。他タブの未保存編集を巻き込まずに部分更新するための土台。
let _mcConfiguredRaw = null;
// このセッションで編集した「provider/usage」キーの集合（render() 時点でクリア）。保存時、この集合に無いセルは _mcConfiguredRaw の値をそのまま維持する。
let _mcTouched = new Set();
let _mcUsages = [];       // 表の行（用途一覧・GET /admin/settings の model_catalog.usages）
let _mcCloudProvider = 'openai';   // 表の1列目（選択中のクラウド AI）
let _mcModalTarget = null;   // 編集モーダルの対象 {provider, usage}

function toast(msg) {
  const t = $('toast'); if (!t) return;
  t.textContent = msg; t.classList.add('show');
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 1800);
}

let _adminErr = '';
// 戻り値は 'admin'／'denied'（401・403・管理者でない）／'unreachable'（判定できなかった。理由は _adminErr）。
async function checkAdmin() {
  try {
    const u = await getJSON('/auth/me');
    return u && u.role === 'admin' ? 'admin' : 'denied';
  } catch (e) {
    if (e && (e.status === 401 || e.status === 403)) return 'denied';
    _adminErr = (e && e.message) || '';
    return 'unreachable';
  }
}

function showUnreachable(denied) {
  const d = denied && denied.querySelectorAll('div');
  if (!d || d.length < 3) return;
  d[1].textContent = '権限を確認できませんでした: ' + _adminErr;
  d[2].textContent = '時間をおいて画面を開き直してください。';
}

function render(view) {
  if (!view.embed_parallel) {
    throw new Error('設定項目が不足しています。サーバーを更新・再起動してから、画面を再読み込みしてください。');
  }
  _view = view;
  renderModelsTab(view);   // _mcState を先に更新（renderProviderTab の埋め込み欄表示が読むため）
  renderProviderTab(view);
  renderResearchTab(view);
  renderChatTab(view);
  renderIngestTab(view);
  renderExtKeysTab(view);
  _nullPlan = [];   // 保存が成功した描画で、既定に戻した予定とその案内を消す
  document.querySelectorAll('[id^="tab-reset-res-"], #chat-examples-reset-res').forEach((el) => {
    el.className = 'tres muted'; el.textContent = '';
  });
  applyConfigChangedHighlights(view);
  refreshTabDots();
}

async function load() {
  try {
    render(await getJSON('/admin/settings'));
  } catch (e) {
    const message = `設定を読み込めませんでした: ${e.message}。サーバーの更新・稼働状態を確認して、画面を再読み込みしてください。`;
    $('msg').innerHTML = `<span class="danger">${esc(message)}</span>`;
    // 初期描画が止まった場合も、各タブに失敗を表示する。未読込の値は保存させない。
    document.querySelectorAll('.tabpanel:not(.tabpanel-embed)').forEach(panel => {
      const notice = document.createElement('p');
      notice.className = 'hint danger';
      notice.setAttribute('role', 'alert');
      notice.textContent = message;
      panel.prepend(notice);
      panel.querySelectorAll('button, input, select, textarea').forEach(el => { el.disabled = true; });
    });
    $('save').disabled = true;
  }
}

// ===== 収集・差分判定 =====
function collectArms() {
  return Array.from(document.querySelectorAll('#arms-list input[data-arm]:checked'))
    .map((el) => el.dataset.arm);
}
function armsChanged() {
  return JSON.stringify([...collectArms()].sort()) !== JSON.stringify(_armsBaseline);
}

// 選択中の旧形式変換バックエンド（none|libreoffice）。ラジオは常に1つ選択されている。
function collectLegacy() {
  const el = document.querySelector('#legacy-radios input[data-legacy]:checked');
  return el ? el.dataset.legacy : null;
}
function legacyChanged() {
  return collectLegacy() !== _legacyBaseline;
}

// 視覚読み取りの VLM 設定を収集（provider/model/cloud_allowed）。
function collectVlm() {
  const provider = ($('vlm-provider') || {}).value || 'ollama';
  const model = (($('vlm-model') || {}).value || '').trim();
  const cloud_allowed = !!($('vlm-cloud-allowed') || {}).checked;
  const out = { provider, cloud_allowed };
  if (model) out.model = model;   // 空なら送らない＝既定モデルへ（未設定扱い）
  return out;
}
function vlmChanged() {
  if (!_vlmBaseline) return false;
  const current = collectVlm();
  return current.provider !== _vlmBaseline.provider
    || (current.model || '') !== (_vlmBaseline.model || '')
    || current.cloud_allowed !== _vlmBaseline.cloud_allowed;
}

// rag.md の LLM 成形トグル。真偽で比較し、PUT では "on"/"off" 文字列に変換して送る。
function ragLlmRenderChanged() {
  return !!($('rag-llm-render') || {}).checked !== _ragLlmRenderBaseline;
}
function extKeysAllowedChanged() {
  return !!($('ext-keys-user-allowed') || {}).checked !== _extKeysAllowedBaseline;
}
function extKeysQuotaChanged() {
  return (($('ext-keys-user-quota-default') || {}).value || '').trim() !== _extKeysQuotaBaseline;
}
function extKeysResearchProviderChanged() {
  const v = ($('ext-research-default-provider') || {}).value || 'ollama';
  return v !== _extKeysResearchProviderBaseline;
}

// ===== 保存 =====
async function save() {
  // 埋め込みデプロイ名欄は保存直前にも確定させる（フォーカスが残ったまま保存した場合の安全網）。
  applyEmbedDeploymentFieldEdit();
  // 調べる深さの基準値は範囲外の値を送る前に弾く。
  const depthProfileErrors = validateDepthProfileInputs();
  const chatMaxTurnsErrors = validateChatMaxTurnsInputs();
  const agenticBudgetErrors = validateAgenticBudgetInputs();
  const workspaceErrors = validateWorkspaceInputs();
  const rangeErrors = depthProfileErrors.concat(chatMaxTurnsErrors).concat(agenticBudgetErrors).concat(workspaceErrors);
  const maxReviewRounds = $('max-review-rounds');
  if (!maxReviewRounds.checkValidity()) rangeErrors.push('最大の見直しの回数は1〜32の整数で指定してください');
  const codexSessionRetentionDays = $('codex-session-retention-days');
  if (!codexSessionRetentionDays.checkValidity()) rangeErrors.push('Codex の会話セッションを保存する日数は0以上の整数で指定してください');
  const embedParallel = $('embed-parallel');
  if (!embedParallel.checkValidity()) rangeErrors.push('埋め込みの同時送信数は1〜16の整数で指定してください');
  if (EMBED_TUNING.some(({ id }) => !$(id).checkValidity())) rangeErrors.push('埋め込みの待ち時間は10〜600秒、件数は1〜200の整数で指定してください');
  if (rangeErrors.length) {
    $('msg').innerHTML = `<span class="danger">${esc(rangeErrors.join('／'))}</span>`;
    return;
  }
  // personal_api_keys_allowed を OFF で保存すると全ユーザーの個人キーが削除される。保有者がいるときは確認ダイアログ（人数表示）を出し、キャンセルは保存全体を中断する。
  const personalNow = !!($('personal-keys-allowed') || {}).checked;
  const savingPersonalKeysOff = personalNow !== _cloudBaseline.personalAllowed && !personalNow;
  if (savingPersonalKeysOff) {
    const n = (_view && _view.cloud && _view.cloud.personal_keys_in_use_count) || 0;
    if (n > 0 && !window.confirm(
      `個人キーを許可しない設定で保存すると、現在 ${n} 人の利用者に保存されている個人キー`
      + '（全プロバイダ分）が削除されます。続けますか？')) {
      $('msg').textContent = '保存を取り消しました';
      return;
    }
  }
  // user_api_keys_allowed を OFF で保存すると、利用者が発行した外部連携キーがすべて失効する（同様に確認ダイアログ）。
  const extKeysAllowedNow = !!($('ext-keys-user-allowed') || {}).checked;
  const savingExtKeysOff = extKeysAllowedNow !== _extKeysAllowedBaseline && !extKeysAllowedNow;
  if (savingExtKeysOff) {
    const n = (_view && _view.ext_keys && _view.ext_keys.self_issued_active_count) || 0;
    if (n > 0 && !window.confirm(
      `利用者のキー発行を許可しない設定で保存すると、現在有効な利用者発行キー ${n} 件が`
      + '失効します。続けますか？')) {
      $('msg').textContent = '保存を取り消しました';
      return;
    }
  }
  // 保存もキー削除待ちの世代を進める: この保存の応答が先に _view へ入るため、古い削除応答で巻き戻さない。
  _invalidateCloudKeyClear();
  $('save').disabled = true;
  $('msg').innerHTML = '<span class="loading-inline" role="status"><span class="spinner spinner-sm"></span><span>保存中...</span></span>';
  const body = {};
  if (armsChanged()) body.arms_enabled = collectArms();   // 触っていない・元に戻していれば送らない
  // 旧形式変換も値が変わったときだけ送る（none も明示的な選択として送る）。
  if (legacyChanged()) { const lb = collectLegacy(); if (lb) body.legacy_backend = lb; }
  if (vlmChanged()) body.vlm = collectVlm();
  if (ragLlmRenderChanged()) body.rag_llm_render = ($('rag-llm-render') || {}).checked ? 'on' : 'off';
  collectCloud(body); collectOcrSwitch(body);
  collectOpenaiEndpoint(body);
  // 使えるモデルを変えたときだけ送る。全置換の契約のため buildModelCatalogBody で組み立てる（_mcState をそのまま送らない）。
  // 埋め込みデプロイ名欄の編集も _mcState に反映済み。
  if (mcCatalogChanged()) body.model_catalog = buildModelCatalogBody();
  if (extKeysAllowedChanged()) body.user_api_keys_allowed = extKeysAllowedNow;
  if (extKeysQuotaChanged()) {
    const v = ($('ext-keys-user-quota-default') || {}).value;
    body.user_api_keys_daily_quota_default = v === '' ? null : Number(v);
  }
  if (extKeysResearchProviderChanged()) {
    const v = ($('ext-research-default-provider') || {}).value || 'ollama';
    // 破損状態を示す一時的な選択肢（"__invalid__"）が選ばれていたら送らない。
    if (v !== _RESEARCH_PROVIDER_INVALID) body.research_default_provider = v;
  }
  collectDepthProfile(body);   // 調べる深さの基準値（変わった項目だけ送る）
  if (maxReviewRoundsChanged()) {
    body.max_review_rounds = maxReviewRounds.value === '' ? null : Number(maxReviewRounds.value);
  }
  if (codexWorkerModelChanged()) {
    const v = $('codex-worker-model').value.trim();
    body.codex_worker_model = v === '' ? null : v;
  }
  if (embedProviderChanged()) {
    body.embed_provider = $('embed-provider').value || null;
  }
  if (codexModeChanged()) {
    body.codex_mode = $('codex-mode').value || null;
  }
  if (codexSessionRetentionDaysChanged()) {
    body.codex_session_retention_days = codexSessionRetentionDays.value === ''
      ? null : Number(codexSessionRetentionDays.value);
  }
  if (embedParallelChanged()) {
    body.embed_parallel = embedParallel.value === '' ? null : Number(embedParallel.value);
  }
  collectEmbedTuning(body);
  if (embedDimChanged()) {
    body.embed_dim = $('embed-dim').value === '' ? null : Number($('embed-dim').value);
  }
  collectChatMaxTurns(body);   // 同時実行の上限（変わった項目だけ送る）
  collectWorkspace(body);   // 個人ファイルの上限・保持日数（変わった項目だけ送る）
  if (chatExamplesChanged()) body.chat_examples = collectChatExamples();   // チャットの質問例
  collectAgenticBudget(body);   // ツール結果1件あたりの予算（変わった項目だけ送る）
  applyNullPlan(body);   // 「既定に戻す」で欄を既定にした固定中の項目は null で解除する
  const ocrTurnedOn = body.ocr_enabled === true;   // 切→入のときだけ送られる
  try {
    const view = await api('PUT', '/admin/settings', body);
    render(view);
    await loadExtKeys();   // OFF 保存で一括失効された可能性があるため一覧も再取得する
    $('msg').innerHTML = '<span class="ok">✓ 保存しました</span>' + (ocrTurnedOn
      ? '<span class="hint"> 画像の文字の読み取りは、「今すぐ更新」を押すと、止めていた間の分から始まります。</span>' : '');
  } catch (e) {
    $('msg').innerHTML = `<span class="danger">${esc(e.message)}</span>`;
  } finally {
    $('save').disabled = false;
  }
}

// ===== タブ単位「既定に戻す」=====
// 欄を既定の値にするだけで確定は「保存」。欄を既定にしても save が null を送らない（固定中が残る）項目だけ _nullPlan に持ち、
// 保存時に欄が既定のままの項目だけ null を送る。API キー欄は対象外。
let _nullPlan = [];   // { tab, key, isDefault }。保存が成功した render() で空にする

function _planNull(tab, key, isDefault) {
  _nullPlan = _nullPlan.filter((p) => p.key !== key);
  _nullPlan.push({ tab, key, isDefault });
}
function _clearNullPlan(tab) {
  _nullPlan = _nullPlan.filter((p) => p.tab !== tab);
}
function nullPlanActive(tab) {
  return _nullPlan.some((p) => p.tab === tab && p.isDefault());
}
function applyNullPlan(body) {
  _nullPlan.forEach((p) => { if (p.isDefault()) body[p.key] = null; });
}

function _setVal(id, v) { const el = $(id); if (el) el.value = v; }
function _setChecked(id, v) { const el = $(id); if (el) el.checked = !!v; }
function _val(id) { return (($(id) || {}).value || '').trim(); }

function _markResetDone(resEl) {
  const el = $(resEl);
  if (el) { el.className = 'tres muted'; el.textContent = '既定の値を入れました。下の「保存」で確定します'; }
  refreshTabDots();
}

// 使えるモデルのセルを組み込み既定へ戻す（保存時 buildModelCatalogBody が既定と同じセルを外す）。
function _mcResetCell(provider, usage) {
  const b = (_mcBuiltin[provider] || {})[usage];
  if (!b) return;
  _mcState[provider] = _mcState[provider] || {};
  _mcState[provider][usage] = JSON.parse(JSON.stringify(b));
  _mcTouched.add(provider + '/' + usage);
}

function resetProviderTab() {
  const view = _view || {};
  _clearNullPlan('provider');
  const radios = document.querySelectorAll('#cloud-provider-radios input[data-cloud-provider]');
  if (radios.length) {
    radios.forEach((r) => { r.checked = r.dataset.cloudProvider === 'openai'; });
    _cloudProviderTouched = true;
  }
  if ((view.cloud || {}).provider_raw != null) _planNull('provider', 'cloud_provider', () => selectedCloudProvider() === 'openai');
  _setChecked('personal-keys-allowed', false);
  _setChecked('web-search-allowed', false);
  // 未設定のときの表示値は既定の接続先そのもの。固定中のときだけ空にして、保存で既定へ戻す。
  _setVal('cloud-ollama-url', _cloudBaseline.ollamaUrl === 'http://localhost:11434' ? _cloudBaseline.ollamaUrl : '');
  _setVal('cloud-ollama-allowlist', '');
  _setVal('webhook-allowlist', '');
  _setVal('embed-provider', '');
  const kind = document.querySelector('#openai-endpoint-radios input[data-openai-endpoint-kind="openai"]');
  if (kind) kind.checked = true;
  _setVal('openai-endpoint-base-url', '');
  _setVal('openai-endpoint-auth-header', 'bearer');
  _setVal('openai-endpoint-api-version', '');
  updateOpenaiEndpointFieldsVisibility();
  const cfg = (view.openai_endpoint || {}).configured || {};
  if (cfg.kind) _planNull('provider', 'openai_endpoint_kind', () => selectedOpenaiEndpointKind() === 'openai');
  if (cfg.base_url) _planNull('provider', 'openai_base_url', () => _val('openai-endpoint-base-url') === '');
  if (cfg.auth_header) {
    _planNull('provider', 'openai_auth_header', () => (_val('openai-endpoint-auth-header') || 'bearer') === 'bearer');
  }
  if (cfg.api_version) _planNull('provider', 'openai_api_version', () => _val('openai-endpoint-api-version') === '');
  _mcResetCell('openai', 'embed');
  syncEmbedDeploymentField();
  renderModelCatalogTable();
  _markResetDone('tab-reset-res-provider');
}

function _resetChatExamplesFields() {
  _clearNullPlan('chat');
  _setChecked('chat-examples-enabled', true);
  _setVal('chat-examples-items', '');
  const ce = (_view || {}).chat_examples || {};
  if (ce.configured != null) {
    _planNull('chat', 'chat_examples', () => !!($('chat-examples-enabled') || {}).checked && _collectChatExamplesItems().length === 0);
  }
}

function resetChatTab() {
  _CHAT_MAX_TURNS_FIELDS.forEach(({ id }) => _setVal(id, ''));
  _WORKSPACE_FIELDS.forEach(({ id }) => _setVal(id, ''));
  _resetChatExamplesFields();
  _markResetDone('tab-reset-res-chat');
}

function resetResearchTab() {
  _clearNullPlan('research');
  _DEPTH_BASE_FIELDS.forEach(({ id }) => _setVal(id, ''));
  _setVal('depth-base-codex-reasoning', '');
  _setVal('codex-mode', '');
  _setVal('max-review-rounds', '');
  _setVal('codex-worker-model', '');
  _setVal('codex-session-retention-days', '');
  _AGENTIC_BUDGET_FIELDS.forEach(({ id }) => _setVal(id, ''));
  const rdp = ((_view || {}).ext_keys || {}).research_default_provider || {};
  const def = rdp.default === 'openai' ? 'openai' : 'ollama';
  _setVal('ext-research-default-provider', def);
  if (rdp.configured) {
    _planNull('research', 'research_default_provider', () => _val('ext-research-default-provider') === def);
  }
  _markResetDone('tab-reset-res-research');
}

function resetModelsTab() {
  // 保存時に既定と同じセルは外れ、全部外れれば model_catalog が null になる。
  const touched = new Set();
  [_mcState, _mcBuiltin, _mcConfiguredRaw || {}].forEach((cat) => {
    Object.keys(cat).forEach((provider) => Object.keys(cat[provider] || {}).forEach((usage) => touched.add(provider + '/' + usage)));
  });
  touched.forEach((key) => {
    const [provider, usage] = key.split('/');
    if ((_mcBuiltin[provider] || {})[usage]) _mcResetCell(provider, usage);
    else if (_mcState[provider]) delete _mcState[provider][usage];
    _mcTouched.add(key);
  });
  renderModelCatalogTable();
  syncEmbedDeploymentField();
  _markResetDone('tab-reset-res-models');
}

function resetIngestTab() {
  const view = _view || {};
  _clearNullPlan('ingest');
  _setVal('embed-parallel', '');
  _setVal('embed-dim', '');
  EMBED_TUNING.forEach(({ id }) => _setVal(id, ''));

  const arms = view.arms || {};
  const armsDefault = [...(arms.env_default || [])].sort();
  document.querySelectorAll('#arms-list input[data-arm]').forEach((el) => {
    el.checked = armsDefault.includes(el.dataset.arm);
  });
  if (arms.configured != null) {
    _planNull('ingest', 'arms_enabled', () => JSON.stringify([...collectArms()].sort()) === JSON.stringify(armsDefault));
  }

  const lb = view.legacy_backend;
  if (lb && Array.isArray(lb.options) && lb.options.length) {
    const legacyDefault = lb.default || 'none';
    document.querySelectorAll('#legacy-radios input[data-legacy]').forEach((el) => {
      el.checked = el.dataset.legacy === legacyDefault;
    });
    if (lb.configured != null) _planNull('ingest', 'legacy_backend', () => collectLegacy() === legacyDefault);
  }

  const vlm = view.vlm;
  if (vlm && vlm.effective) {
    const d = vlm.default || {};
    const defProvider = d.provider === 'openai' ? 'openai' : 'ollama';
    _setVal('vlm-provider', defProvider);
    _setVal('vlm-model', d.model || '');
    _setChecked('vlm-cloud-allowed', d.cloud_allowed);
    updateVlmKeyHint();
    updateVlmUrlHint();
    if (vlm.configured != null) {
      _planNull('ingest', 'vlm', () => {
        const cur = collectVlm();
        return cur.provider === defProvider && (!cur.model || cur.model === (d.model || ''))
          && cur.cloud_allowed === !!d.cloud_allowed;
      });
    }
  }

  const rr = view.rag_llm_render;
  if (rr) {
    _setChecked('rag-llm-render', rr.default);
    if (rr.configured != null) {
      _planNull('ingest', 'rag_llm_render', () => !!($('rag-llm-render') || {}).checked === !!rr.default);
    }
  }
  resetOcrSwitch(view);
  _markResetDone('tab-reset-res-ingest');
}

// 外部連携（API キー）タブ。利用者のキー発行の許可と呼び出し上限だけが対象。
function resetExtKeysTab() {
  _setChecked('ext-keys-user-allowed', false);
  _setVal('ext-keys-user-quota-default', '');
  _markResetDone('tab-reset-res-extkeys');
}

const _TAB_RESET_HANDLERS = {
  provider: resetProviderTab, research: resetResearchTab, chat: resetChatTab, models: resetModelsTab,
  ingest: resetIngestTab, extkeys: resetExtKeysTab,
};
document.querySelectorAll('[data-reset-tab]').forEach((b) => {
  const handler = _TAB_RESET_HANDLERS[b.dataset.resetTab];
  if (handler) b.addEventListener('click', handler);
});

// チャットの質問例カードだけの「未設定に戻す」。
const _chatExamplesReset = $('chat-examples-reset');
if (_chatExamplesReset) _chatExamplesReset.addEventListener('click', () => {
  _resetChatExamplesFields();
  _markResetDone('chat-examples-reset-res');
});
