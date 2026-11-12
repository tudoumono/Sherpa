// システム管理「調査・回答」タブ（調べる深さ・Codex の動作・埋め込みの設定・ツール結果の予算）。
'use strict';

// 「調査・回答」タブは接続設定と独立して描画・リセットする。
function renderResearchTab(view) {
  renderResearchDefaultProvider((view.ext_keys || {}).research_default_provider);
  renderAgenticBudget(view.agentic_budget);   // ツール結果バイト予算
  renderCodexMode(view.codex_mode);
  renderDepthProfile(view.depth_profile);
  const rounds = view.max_review_rounds;
  _maxReviewRoundsBaseline = rounds.configured == null ? '' : String(rounds.configured);
  $('max-review-rounds').value = _maxReviewRoundsBaseline;
  $('max-review-rounds-hint').textContent = `現在の適用値: ${rounds.effective} 回。既定: ${rounds.default} 回。`
    + (rounds.configured == null ? '未設定です。' : 'この値で固定中です。')
    + '（既定の方式には効きません）';
  const workerModel = view.codex_worker_model;
  _codexWorkerModelBaseline = workerModel.configured == null ? '' : String(workerModel.configured);
  $('codex-worker-model').value = _codexWorkerModelBaseline;
  $('codex-worker-model').placeholder = `既定: ${workerModel.default}`;
  $('codex-worker-model-hint').textContent = workerModel.configured == null
    // `effective` は Codex(OpenAI 系) の値（Azure は本体と同じデプロイ名へ倒れる）。
    ? `未設定です（実際に適用される値: ${workerModel.effective}。ローカル（Ollama）構成の利用者は本体と同じモデル名）。`
    : `この値で固定中です（既定: ${workerModel.default}）。この値は全構成に適用されるため、ローカル（Ollama）構成の利用者がいる環境では Ollama 側にも存在するモデル名にしてください。`;
  const retention = view.codex_session_retention_days;
  _codexSessionRetentionDaysBaseline = retention.configured == null ? '' : String(retention.configured);
  $('codex-session-retention-days').value = _codexSessionRetentionDaysBaseline;
  $('codex-session-retention-days-hint').textContent =
    `現在の適用値: ${retention.effective} 日（0=無制限）。既定: ${retention.default} 日。`
    + (retention.configured == null ? '未設定です。' : 'この値で固定中です。');
}

// 素の Codex モード。depth-base-codex-reasoning と同型（空選択肢=未設定=「標準」）。
function renderCodexMode(cm) {
  cm = cm || {};
  $('codex-mode').value = cm.configured || '';
  _codexModeBaseline = cm.configured || '';
  $('codex-mode-hint').textContent = cm.configured == null
    ? '未設定です（既定の標準が適用されます）。'
    : 'この値で固定中です（既定: 標準）。';
}

// 保存済みの設定で実際に埋め込む先を説明する文。判定は `embeddings.cfg` と同じ順にする。
function embedProviderHint(ep, cloud) {
  if (ep.effective === 'ollama') return `ローカル（Ollama）で埋め込みます。使うモデル: ${ep.ollama_model}（次元はモデルから自動で決まります）`;
  const raw = String(cloud.provider_raw || '').trim().toLowerCase();
  const unusable = '回答に使うクラウドが使えない状態なので、埋め込みができません。クラウドの設定を直すか「ローカル（Ollama）」を選んでください。';
  if (raw && !(cloud.providers || ['openai']).includes(raw)) return unusable;
  if (cloud.openai_key_set) return '回答に使うクラウド（OpenAI）で埋め込みます。';
  if (raw) return unusable;
  return 'クラウドが未設定なので Ollama で埋め込みます。次元は使うモデルから自動で決まります（Ollama につながらないと測れず、ベクトル検索は使えません）。';
}

function renderEmbedProvider(ep, cloud) {
  ep = ep || {};
  _embedProviderBaseline = ep.configured || '';
  $('embed-provider').value = _embedProviderBaseline;
  $('embed-provider-hint').textContent = embedProviderHint(ep, cloud || {});
}

function embedProviderChanged() {
  return $('embed-provider').value !== _embedProviderBaseline;
}

function codexModeChanged() {
  return $('codex-mode').value !== _codexModeBaseline;
}


function maxReviewRoundsChanged() {
  return $('max-review-rounds').value.trim() !== _maxReviewRoundsBaseline;
}

function codexWorkerModelChanged() {
  return $('codex-worker-model').value.trim() !== _codexWorkerModelBaseline;
}

function codexSessionRetentionDaysChanged() {
  return $('codex-session-retention-days').value.trim() !== _codexSessionRetentionDaysBaseline;
}

function embedParallelChanged() {
  return $('embed-parallel').value.trim() !== _embedParallelBaseline;
}

function embedDimChanged() {
  return $('embed-dim').value.trim() !== _embedDimBaseline;
}

// 調べる深さの基準値（標準時の値）。表示上は適用先ごとに分け、保存済みのキーを引き継ぐ。
function renderDepthProfile(dp) {
  dp = dp || {};
  _depthProfileBaseline = {};
  _DEPTH_BASE_FIELDS.forEach(({ view, put, id }) => {
    const info = dp[view] || {};
    const input = $(id);
    if (input) input.value = info.configured != null ? info.configured : '';
    _depthProfileBaseline[put] = info.configured != null ? String(info.configured) : '';
    const hint = $(id + '-hint');
    if (hint) {
      hint.textContent = info.configured != null
        ? `この値で固定中です（既定値: ${info.default}）。`
        : `未設定です（組み込みの既定 ${info.effective} が適用されます）。`;
    }
  });
  const reasoning = dp.codex_reasoning || {};
  const sel = $('depth-base-codex-reasoning');
  if (sel) sel.value = reasoning.configured || '';
  _depthReasoningBaseline = reasoning.configured || '';
  const reasoningHint = $('depth-base-codex-reasoning-hint');
  if (reasoningHint) {
    reasoningHint.textContent = reasoning.configured != null
      ? `この値で固定中です（既定値: ${reasoning.default}）。`
      : `未設定です（組み込みの既定 ${reasoning.effective} が適用されます）。`;
  }
}

function collectDepthProfile(body) {
  _DEPTH_BASE_FIELDS.forEach(({ put, id }) => {
    const v = (($(id) || {}).value || '').trim();
    if (v !== (_depthProfileBaseline[put] || '')) body[put] = v === '' ? null : Number(v);
  });
  const sel = $('depth-base-codex-reasoning');
  if (sel && sel.value !== _depthReasoningBaseline) body.depth_base_codex_reasoning = sel.value || null;
}
function depthProfileChanged() {
  const intChanged = _DEPTH_BASE_FIELDS.some(({ put, id }) =>
    ((($(id) || {}).value || '').trim()) !== (_depthProfileBaseline[put] || ''));
  const sel = $('depth-base-codex-reasoning');
  return intChanged || (!!sel && sel.value !== _depthReasoningBaseline);
}

// 保存前にサーバと同じ範囲を検証し、日本語で案内する（422 の detail 配列をそのまま表示すると読めないため、ここで弾く）。空欄（未設定へ戻す）は対象外。
function validateDepthProfileInputs() {
  const errors = [];
  _DEPTH_BASE_FIELDS.forEach(({ id, label }) => {
    const el = $(id);
    if (!el) return;
    const raw = (el.value || '').trim();
    if (raw === '') return;
    const n = Number(raw);
    const lo = Number(el.min), hi = Number(el.max);
    if (!Number.isFinite(n) || !Number.isInteger(n) || n < lo || n > hi) {
      errors.push(`${label}は${lo}〜${hi}の整数で指定してください`);
    }
  });
  return errors;
}

// 人に読みやすいバイト表示（KB/MB・1024 進）。
function _fmtBytesHuman(bytes) {
  if (bytes == null) return '—';
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)}MB`;
  return `${Math.round(bytes / 1024)}KB`;
}

// 文章中心のA4資料を1ページ1,000字、日本語1字をUTF-8で約3バイトとして概算。
function _fmtDocumentPages(bytes) {
  if (bytes == null) return '—';
  const pages = bytes / 3000;
  return pages < 1 ? 'A4資料1ページ未満' : `A4資料約${Math.round(pages).toLocaleString('ja-JP')}ページ分`;
}

// 調べる深さの基準値と同じ流儀の別カード（調査・回答タブ）。表示/入力欄は KB 単位、GET/PUT（bytes）とは境界でだけ変換する。
function renderAgenticBudget(ab) {
  ab = ab || {};
  _agenticBudgetBaseline = {};
  _AGENTIC_BUDGET_FIELDS.forEach(({ view, put, id }) => {
    const info = ab[view] || {};
    const input = $(id);
    const kb = info.configured != null ? Math.round(info.configured / 1024) : '';
    if (input) input.value = kb;
    _agenticBudgetBaseline[put] = kb === '' ? '' : String(kb);
    const hint = $(id + '-hint');
    if (hint) {
      hint.textContent = info.configured != null
        ? `この値（${_fmtBytesHuman(info.configured)}）で固定中です（既定値: ${_fmtBytesHuman(info.default)}・${_fmtDocumentPages(info.default)}）。`
        : `未設定です（組み込みの既定 ${_fmtBytesHuman(info.default)}・${_fmtDocumentPages(info.default)}）。`;
      hint.textContent += ` 現在の上限: ${_fmtBytesHuman(info.effective)}（${_fmtDocumentPages(info.effective)}）。`;
    }
  });
}

function collectAgenticBudget(body) {
  _AGENTIC_BUDGET_FIELDS.forEach(({ put, id }) => {
    const v = (($(id) || {}).value || '').trim();
    if (v !== (_agenticBudgetBaseline[put] || '')) body[put] = v === '' ? null : Math.round(Number(v) * 1024);
  });
}
function agenticBudgetChanged() {
  return _AGENTIC_BUDGET_FIELDS.some(({ put, id }) =>
    ((($(id) || {}).value || '').trim()) !== (_agenticBudgetBaseline[put] || ''));
}
// validateDepthProfileInputs と同じ流儀。HTML の min/max（KB）はサーバの loBytes/hiBytes を1024で割った値。
function validateAgenticBudgetInputs() {
  const errors = [];
  _AGENTIC_BUDGET_FIELDS.forEach(({ id, label }) => {
    const el = $(id);
    if (!el) return;
    const raw = (el.value || '').trim();
    if (raw === '') return;
    const n = Number(raw);
    const lo = Number(el.min), hi = Number(el.max);
    if (!Number.isFinite(n) || !Number.isInteger(n) || n < lo || n > hi) {
      errors.push(`${label}は${lo}〜${hi}（KB）の整数で指定してください`);
    }
  });
  return errors;
}
