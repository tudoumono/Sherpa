// システム管理「プロバイダ＋接続先」タブ（クラウド AI・Ollama・接続先・同時実行の上限・個人ファイル・質問例）。admin-settings.js の後に読む。
'use strict';

// ===== クラウド AI プロバイダの中央設定 =====

function selectedCloudProvider() {
  const el = document.querySelector('#cloud-provider-radios input[data-cloud-provider]:checked');
  return el ? el.dataset.cloudProvider : 'openai';
}

// 選択肢が1つで保存値が未設定か openai のときは出さない。廃止済み・不正な保存値のときだけ、選び直すために出す。
function renderCloudProviderRadios(cloud) {
  const wrap = $('cloud-provider-radios');
  if (!wrap) return;
  const providers = cloud.providers || ['openai'];
  const raw = String(cloud.provider_raw || '').trim().toLowerCase();
  if (providers.length === 1 && (raw === '' || raw === 'openai')) { wrap.innerHTML = ''; return; }
  const current = cloud.provider || 'openai';
  wrap.innerHTML = providers.map((p) => {
    const meta = CLOUD_PROVIDER_LABELS[p] || { label: p, desc: '' };
    const checked = p === current ? ' checked' : '';
    return `<label class="cloud-provider-row">`
      + `<input type="radio" name="cloud-provider" data-cloud-provider="${esc(p)}"${checked}>`
      + `<span><span class="arm-t">${esc(meta.label)}</span>`
      + (meta.desc ? `<div class="arm-d">${esc(meta.desc)}</div>` : '')
      + `</span></label>`;
  }).join('');
}

// キー欄はラジオで選ばれているプロバイダ1つ分だけ表示する。
function renderCloudKeyBlock(cloud) {
  const label = $('cloud-key-label');
  const input = $('cloud-key');
  if (!label || !input) return;
  const provider = selectedCloudProvider();
  const meta = CLOUD_PROVIDER_LABELS[provider] || { label: provider };
  label.textContent = meta.label + ' の API キー';
  const keySet = !!cloud[CLOUD_KEY_SET_FIELD[provider]];
  input.value = '';
  input.placeholder = keySet ? '設定済み（変更する場合のみ入力）' : '未設定';
  // 削除できるキーが無いときはボタンを disabled にする。
  const clearBtn = $('cloud-key-clear');
  if (clearBtn) clearBtn.disabled = !keySet;
}

function renderCloudStatus(cloud) {
  // 保存済みの cloud_provider が閉じたプロバイダ（gemini/bedrock）のときは、未選択扱い（OpenAI）で動いている旨を警告する。
  const warn = $('cloud-retired-warn');
  if (warn) {
    const retired = cloud.retired_provider || '';
    warn.hidden = !retired;
    warn.textContent = retired
      ? `保存されているクラウド AI の選択（${retired}）は利用できなくなりました。現在は未選択として扱っています。OpenAI を選び直して保存してください。`
      : '';
  }
  const el = $('cloud-status');
  if (!el) return;
  const provider = cloud.provider || 'openai';
  const meta = CLOUD_PROVIDER_LABELS[provider] || { label: provider };
  el.textContent = `クラウド AI: ${meta.label}`
    + (cloud.personal_api_keys_allowed ? '（個人キーの利用を許可中）' : '（個人キーは無効・中央設定のみ）');
}

function renderCloud(cloud) {
  cloud = cloud || {};
  renderCloudProviderRadios(cloud);
  renderCloudKeyBlock(cloud);
  renderCloudStatus(cloud);
  const pk = $('personal-keys-allowed');
  if (pk) pk.checked = !!cloud.personal_api_keys_allowed;
  const ws = $('web-search-allowed');
  if (ws) ws.checked = !!cloud.web_search_allowed;
  const ourl = $('cloud-ollama-url');
  if (ourl) ourl.value = cloud.ollama_url || '';
  const res = $('cloud-key-test-res');
  if (res) { res.className = 'tres muted'; res.textContent = ''; }
  const ores = $('cloud-ollama-test-res');
  if (ores) { ores.className = 'tres muted'; ores.textContent = ''; }
}

// クラウド設定が render() 時点の基準値（_cloudBaseline）から変わっているか。キー入力欄は基準値が常に空文字＝入力が空でなければ変更あり。
function cloudChanged() {
  const keyInput = (($('cloud-key') || {}).value || '').trim();
  const provider = selectedCloudProvider();
  return provider !== _cloudBaseline.provider
    || cloudProviderNeedsExplicitSave(provider)
    || keyInput !== ''
    || !!($('personal-keys-allowed') || {}).checked !== _cloudBaseline.personalAllowed
    || !!($('web-search-allowed') || {}).checked !== _cloudBaseline.webSearchAllowed
    || (($('cloud-ollama-url') || {}).value || '').trim() !== _cloudBaseline.ollamaUrl;
}

function _sortedUniqueLines(text) {
  const lines = (text || '').split('\n').map((s) => s.trim()).filter(Boolean);
  return Array.from(new Set(lines)).sort();
}

function ollamaAllowlistChanged() {
  return JSON.stringify(_sortedUniqueLines(($('cloud-ollama-allowlist') || {}).value))
    !== JSON.stringify(_ollamaAllowlistBaseline);
}

// Webhook 許可リストの変更判定（ollamaAllowlistChanged() と同型）。
function webhookAllowlistChanged() {
  return JSON.stringify(_sortedUniqueLines(($('webhook-allowlist') || {}).value))
    !== JSON.stringify(_webhookAllowlistBaseline);
}

function collectCloud(body) {
  const provider = selectedCloudProvider();
  if (provider !== _cloudBaseline.provider || cloudProviderNeedsExplicitSave(provider)) body.cloud_provider = provider;
  const keyInput = (($('cloud-key') || {}).value || '').trim();
  if (keyInput !== '') body[provider + '_api_key'] = keyInput;   // 空文字＝クリア（サーバ側で正規化）
  const personalNow = !!($('personal-keys-allowed') || {}).checked;
  if (personalNow !== _cloudBaseline.personalAllowed) body.personal_api_keys_allowed = personalNow;
  const webSearchNow = !!($('web-search-allowed') || {}).checked;
  if (webSearchNow !== _cloudBaseline.webSearchAllowed) body.web_search_allowed = webSearchNow;
  const ollamaUrlNow = (($('cloud-ollama-url') || {}).value || '').trim();
  if (ollamaUrlNow !== _cloudBaseline.ollamaUrl) body.ollama_url = ollamaUrlNow || null;   // 空文字＝既定（localhost）へ戻す
  if (ollamaAllowlistChanged()) {
    const lines = _sortedUniqueLines(($('cloud-ollama-allowlist') || {}).value);
    body.ollama_allowlist = lines.length ? lines : null;
  }
  if (webhookAllowlistChanged()) {
    const lines = _sortedUniqueLines(($('webhook-allowlist') || {}).value);
    body.webhook_allowlist = lines.length ? lines : null;
  }
}

// Ollama の許可ホスト一覧。
function renderOllamaAllowlist(info) {
  const ta = $('cloud-ollama-allowlist');
  if (!ta || !info) return;
  ta.value = (info.configured || []).join('\n');
}

// Webhook 宛先の許可ホスト一覧（ollama_allowlist と同型の UI）。
function renderWebhookAllowlist(info) {
  const ta = $('webhook-allowlist');
  if (!ta || !info) return;
  ta.value = (info.configured || []).join('\n');
}

// ===== OpenAI 互換 API の接続先（本家／Azure OpenAI／その他 OpenAI 互換） =====

function selectedOpenaiEndpointKind() {
  const el = document.querySelector('#openai-endpoint-radios input[data-openai-endpoint-kind]:checked');
  return el ? el.dataset.openaiEndpointKind : 'openai';
}

function renderOpenaiEndpointRadios(oe) {
  const wrap = $('openai-endpoint-radios');
  if (!wrap) return;
  const kinds = oe.kinds || ['openai', 'azure', 'custom'];
  const current = (oe.effective || {}).kind || 'openai';
  wrap.innerHTML = kinds.map((k) => {
    const meta = OPENAI_ENDPOINT_KIND_LABELS[k] || { label: k };
    const checked = k === current ? ' checked' : '';
    return `<label class="cloud-provider-row">`
      + `<input type="radio" name="openai-endpoint-kind" data-openai-endpoint-kind="${esc(k)}"${checked}>`
      + `<span><span class="arm-t">${esc(meta.label)}</span></span></label>`;
  }).join('');
}

// 「OpenAI 本家」選択時は base URL 等の詳細欄を隠す（本家以外を選んだときだけ必要な設定のため）。
function updateOpenaiEndpointFieldsVisibility() {
  const fields = $('openai-endpoint-fields');
  if (fields) fields.hidden = selectedOpenaiEndpointKind() === 'openai';
}

// 埋め込みのデプロイ名欄は「使えるモデル」（model_catalog.openai.embed）の値の唯一の表示先。
// どちらの面で編集しても _mcState が単一の真実源のため、変更時はこの関数で表示を同期する。
function syncEmbedDeploymentField() {
  const el = $('openai-endpoint-embed-deployment');
  if (el) el.value = ((_mcState.openai || {}).embed || {}).default || '';
}

function renderOpenaiEndpoint(oe) {
  oe = oe || {};
  renderOpenaiEndpointRadios(oe);
  const cfg = oe.configured || {};
  const baseUrl = $('openai-endpoint-base-url');
  if (baseUrl) baseUrl.value = cfg.base_url || '';
  const authHeader = $('openai-endpoint-auth-header');
  if (authHeader) authHeader.value = cfg.auth_header || 'bearer';
  const apiVersion = $('openai-endpoint-api-version');
  if (apiVersion) apiVersion.value = cfg.api_version || '';
  syncEmbedDeploymentField();
  updateOpenaiEndpointFieldsVisibility();
  const res = $('openai-endpoint-test-res');
  if (res) { res.className = 'tres muted'; res.textContent = ''; }
}

// 保存（collectOpenaiEndpoint）と接続テストが共有する pending 生成処理。フォームの現在値を返す。
// 「本家」選択時は他の3項目を含めない（llm.py は kind=openai なら無視する契約）。
function collectOpenaiEndpointPending() {
  const kind = selectedOpenaiEndpointKind();
  const pending = { openai_endpoint_kind: kind };
  if (kind !== 'openai') {
    pending.openai_base_url = (($('openai-endpoint-base-url') || {}).value || '').trim();
    pending.openai_auth_header = ($('openai-endpoint-auth-header') || {}).value || 'bearer';
    pending.openai_api_version = (($('openai-endpoint-api-version') || {}).value || '').trim();
  }
  return pending;
}

// 接続先が render() 時点の基準値から変わっているか。認証ヘッダ・APIバージョンは #openai-endpoint-fields の外にあるため、値そのものを比較する。
function openaiEndpointChanged() {
  const kind = selectedOpenaiEndpointKind();
  if (kind !== _openaiEndpointBaseline.kind) return true;
  if (kind === 'openai') return false;   // 本家選択時は他の3項目を見ない（常に無視される値のため）
  const base = (($('openai-endpoint-base-url') || {}).value || '').trim();
  const auth = ($('openai-endpoint-auth-header') || {}).value || 'bearer';
  const ver = (($('openai-endpoint-api-version') || {}).value || '').trim();
  return base !== _openaiEndpointBaseline.base_url || auth !== _openaiEndpointBaseline.auth_header
    || ver !== _openaiEndpointBaseline.api_version;
}

function collectOpenaiEndpoint(body) {
  if (!openaiEndpointChanged()) return;
  const pending = collectOpenaiEndpointPending();
  body.openai_endpoint_kind = pending.openai_endpoint_kind;
  if (pending.openai_endpoint_kind === 'openai') return;   // 他の3項目は触らない（現在の保存値を維持）
  body.openai_base_url = pending.openai_base_url || null;
  body.openai_auth_header = pending.openai_auth_header;
  body.openai_api_version = pending.openai_api_version || null;
}

// 埋め込みのデプロイ名欄の編集確定（'change'）時の反映先は model_catalog（openai/embed）。
// 表の直接編集と同じ規約（default が allowed に無ければ先頭へ足す）で _mcState を更新し、表の表示も追従させる。
function applyEmbedDeploymentFieldEdit() {
  const el = $('openai-endpoint-embed-deployment');
  if (!el) return;
  const value = (el.value || '').trim();
  const cur = (_mcState.openai || {}).embed || { allowed: [], default: '' };
  if (value === (cur.default || '')) return;   // 変化なし
  const allowed = value && !(cur.allowed || []).includes(value) ? [value, ...(cur.allowed || [])] : (cur.allowed || []);
  _mcState.openai = _mcState.openai || {};
  _mcState.openai.embed = { allowed, default: value };
  _mcTouched.add('openai/embed');
  if (_mcCloudProvider === 'openai') renderModelCatalogTable();
}

// 接続テスト（POST /admin/settings/openai-endpoint-test）。中央キー・中央モデルだけを使い、保存しない。
// pending 生成は保存と共通（collectOpenaiEndpointPending）。
let _endpointTestBusy = false;   // 多重クリック防止
async function testOpenaiEndpoint() {
  if (_endpointTestBusy) return;
  _endpointTestBusy = true;
  const btn = $('openai-endpoint-test');
  if (btn) btn.disabled = true;
  const res = $('openai-endpoint-test-res');
  res.className = 'tres muted';
  res.innerHTML = '<span class="loading-inline" role="status"><span class="spinner spinner-sm"></span><span>接続を確認中...</span></span>';
  const body = { provider: 'openai', ...collectOpenaiEndpointPending() };
  // クラウドキー欄が OpenAI 選択中かつ入力中なら、それも一緒に試す（保存前のキーで試せるように）。
  if (selectedCloudProvider() === 'openai') {
    const k = ($('cloud-key') || {}).value.trim();
    if (k) body.openai_api_key = k;
  }
  try {
    const r = await fetch('/admin/settings/openai-endpoint-test', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const d = await r.json().catch(() => null);
    if (!d) { res.className = 'tres danger'; res.textContent = `✗ サーバーが処理できませんでした（HTTP ${r.status}）`; return; }
    res.className = 'tres ' + (d.ok ? 'ok' : 'danger');
    res.textContent = (d.ok ? '✓ 接続OK' : '✗ ' + (d.detail || '失敗')) + (d.model ? `（${d.model}）` : '');
  } catch (e) {
    res.className = 'tres danger'; res.textContent = '✗ 通信に失敗しました。ネットワークを確認してください';
  } finally {
    _endpointTestBusy = false;
    if (btn) btn.disabled = false;
  }
}

// Ollama の接続テスト（POST /admin/settings/ollama-test）。入力中の既定接続先で1回だけ試し、保存しない。
let _ollamaTestBusy = false;   // 多重クリック防止
async function testCentralOllama() {
  if (_ollamaTestBusy) return;
  _ollamaTestBusy = true;
  const btn = $('cloud-ollama-test');
  if (btn) btn.disabled = true;
  const res = $('cloud-ollama-test-res');
  res.className = 'tres muted';
  res.innerHTML = '<span class="loading-inline" role="status"><span class="spinner spinner-sm"></span><span>接続を確認中...</span></span>';
  const body = { ollama_url: (($('cloud-ollama-url') || {}).value || '').trim() };
  try {
    const r = await fetch('/admin/settings/ollama-test', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const d = await r.json().catch(() => null);
    if (!d) { res.className = 'tres danger'; res.textContent = `✗ サーバーが処理できませんでした（HTTP ${r.status}）`; return; }
    if (!d.ok) {
      res.className = 'tres danger';
      res.textContent = '✗ ' + (typeof d.detail === 'string' ? d.detail : '失敗');
      return;
    }
    const models = d.models || [];
    const missing = d.missing || [];
    let text = `✓ 接続OK（${d.host}）。取得済みのモデル: ${models.length ? models.join(', ') : 'なし'}`;
    if (missing.length) text += `。使えるモデル一覧のうち未取得: ${missing.join(', ')}（相手のパソコンで ollama pull を実行してください）`;
    const noTools = d.no_tools || [];
    if (noTools.length) text += `。道具の呼び出しに対応していないモデル: ${noTools.join(', ')}（資料を調べる回答には使えません。「使えるモデル」で別のモデルを選んでください）`;
    res.className = 'tres ' + (missing.length || noTools.length ? 'danger' : 'ok');
    res.textContent = text;
  } catch (e) {
    res.className = 'tres danger'; res.textContent = '✗ 通信に失敗しました。ネットワークを確認してください';
  } finally {
    _ollamaTestBusy = false;
    if (btn) btn.disabled = false;
  }
}

// 接続テスト（POST /settings/test・入力中のキーで1回だけ試す・保存しない）。
async function testCloudKey() {
  const provider = selectedCloudProvider();
  const res = $('cloud-key-test-res');
  res.className = 'tres muted';
  res.innerHTML = '<span class="loading-inline" role="status"><span class="spinner spinner-sm"></span><span>接続を確認中...</span></span>';
  const body = { provider };
  const k = ($('cloud-key') || {}).value.trim();
  if (k) body[provider + '_api_key'] = k;
  try {
    const r = await fetch('/settings/test', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const d = await r.json().catch(() => null);
    if (!d) { res.className = 'tres danger'; res.textContent = `✗ サーバーが処理できませんでした（HTTP ${r.status}）`; return; }
    res.className = 'tres ' + (d.ok ? 'ok' : 'danger');
    res.textContent = (d.ok ? '✓ 接続OK' : '✗ ' + (d.detail || '失敗')) + (d.model ? `（${d.model}）` : '');
  } catch (e) {
    res.className = 'tres danger'; res.textContent = '✗ 通信に失敗しました。ネットワークを確認してください';
  }
}

// 中央 API キーの削除。確認ダイアログを挟み、確定した操作でだけ空文字を PUT する（キー欄を空のまま保存しても「未入力＝変更しない」）。
// 要求時点のプロバイダ・世代を捕捉し、応答到着時に両方一致する時だけ反映する（不一致なら _view・表示に触れず捨てる）。
let _cloudKeyClearGen = 0;
// プロバイダ切替・キー入力・保存のいずれでも呼ぶ。破棄した削除待ちの結果表示をニュートラルへ戻す。
function _invalidateCloudKeyClear() {
  _cloudKeyClearGen++;
  const res = $('cloud-key-clear-res');
  if (res) { res.className = 'tres muted'; res.textContent = ''; }
}

async function clearCloudKey() {
  const provider = selectedCloudProvider();
  const meta = CLOUD_PROVIDER_LABELS[provider] || { label: provider };
  if (!window.confirm(`${meta.label} の中央 API キーを削除しますか？`
    + '削除すると、個人キーが無いユーザーはこのクラウド AI を呼び出せなくなります（元に戻せません）。')) {
    return;
  }
  _invalidateCloudKeyClear();   // 新しい削除操作自体も、それ以前の削除待ちを無効化する対象
  const myGen = _cloudKeyClearGen;
  const res = $('cloud-key-clear-res');
  if (res) { res.className = 'tres muted'; res.textContent = '削除しています...'; }
  try {
    const view = await api('PUT', '/admin/settings', { [provider + '_api_key']: '' });
    // 判定を先に行う: 世代・プロバイダが不一致なら _view・表示に触れず捨てる。
    if (myGen !== _cloudKeyClearGen || selectedCloudProvider() !== provider) return;
    // renderProviderTab(view) は呼ばない（同タブの未保存編集を破棄してしまう）。
    // キー欄の表示と、キー有無に依存する案内（VLM のキー未設定警告）だけを更新する。
    _view = view;
    renderCloudKeyBlock(view.cloud || {});
    updateVlmKeyHint();
    applyConfigChangedHighlights(view);
    refreshTabDots();
    if (res) { res.className = 'tres ok'; res.textContent = '✓ 削除しました'; }
  } catch (e) {
    if (myGen !== _cloudKeyClearGen || selectedCloudProvider() !== provider) return;
    if (res) { res.className = 'tres danger'; res.textContent = '✗ ' + e.message; }
  }
}

// ===== タブ単位の描画 =====
// 各タブの render はそのタブのフィールドの baseline も更新する。

function renderProviderTab(view) {
  const cloud = view.cloud || {};
  renderCloud(cloud);
  renderOllamaAllowlist(view.ollama_allowlist);
  renderWebhookAllowlist(view.webhook_allowlist);
  renderOpenaiEndpoint(view.openai_endpoint);   // _mcState（使えるモデル）を先に更新してから読む
  renderEmbedProvider(view.embed_provider, cloud); showInvalidProviders(view);
  _cloudBaseline = {
    provider: cloud.provider || 'openai',
    providerRaw: cloud.provider_raw || null,
    personalAllowed: !!cloud.personal_api_keys_allowed,
    webSearchAllowed: !!cloud.web_search_allowed,
    ollamaUrl: cloud.ollama_url || '',
  };
  _cloudProviderTouched = false;   // サーバの現況を基準に描画し直すたび、この描画以降の操作だけを追跡する
  _ollamaAllowlistBaseline = [...((view.ollama_allowlist || {}).configured || [])].sort();
  _webhookAllowlistBaseline = [...((view.webhook_allowlist || {}).configured || [])].sort();
  const oe = view.openai_endpoint || {};
  const cfg = oe.configured || {};
  _openaiEndpointBaseline = {
    kind: (oe.effective || {}).kind || 'openai',
    base_url: cfg.base_url || '',
    auth_header: cfg.auth_header || 'bearer',
    api_version: cfg.api_version || '',
  };
}

// 「チャットと個人ファイル」タブ。接続設定と独立して描画・リセットする。
function renderChatTab(view) {
  renderChatMaxTurns(view.chat_max_turns);
  renderWorkspace(view.workspace);
  renderChatExamples(view.chat_examples);
}

// 同時実行の上限（chat_max_turns）。renderDepthProfile/collectDepthProfile/depthProfileChanged と同じ流儀。「チャットと個人ファイル」タブのカード。
function renderChatMaxTurns(cmt) {
  cmt = cmt || {};
  _chatMaxTurnsBaseline = {};
  _CHAT_MAX_TURNS_FIELDS.forEach(({ view, put, id }) => {
    const info = cmt[view] || {};
    const input = $(id);
    if (input) input.value = info.configured != null ? info.configured : '';
    _chatMaxTurnsBaseline[put] = info.configured != null ? String(info.configured) : '';
    const hint = $(id + '-hint');
    if (hint) {
      hint.textContent = info.configured != null
        ? `この値で固定中です（既定値: ${info.default}）。`
        : `未設定です（組み込みの既定 ${info.effective} が適用されます）。`;
    }
  });
}
function collectChatMaxTurns(body) {
  _CHAT_MAX_TURNS_FIELDS.forEach(({ put, id }) => {
    const v = (($(id) || {}).value || '').trim();
    if (v !== (_chatMaxTurnsBaseline[put] || '')) body[put] = v === '' ? null : Number(v);
  });
}
function chatMaxTurnsChanged() {
  return _CHAT_MAX_TURNS_FIELDS.some(({ put, id }) =>
    ((($(id) || {}).value || '').trim()) !== (_chatMaxTurnsBaseline[put] || ''));
}
// validateDepthProfileInputs と同じ流儀。
function validateChatMaxTurnsInputs() {
  const errors = [];
  _CHAT_MAX_TURNS_FIELDS.forEach(({ id, label }) => {
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

// 個人ファイル（workspace）。renderChatMaxTurns と同じ流儀（表示単位は MB／日）。
function renderWorkspace(ws) {
  ws = ws || {};
  _workspaceBaseline = {};
  _WORKSPACE_FIELDS.forEach(({ view, put, id, unit, unitLabel }) => {
    const info = ws[view] || {};
    const shown = info.configured != null ? Math.round(info.configured / unit) : '';
    const input = $(id);
    if (input) input.value = shown;
    _workspaceBaseline[put] = shown === '' ? '' : String(shown);
    const hint = $(id + '-hint');
    if (hint) {
      const dflt = Math.round(info.default / unit);
      hint.textContent = info.configured != null
        ? `この値で固定中です（既定値: ${dflt}${unitLabel}）。`
        : `未設定です（組み込みの既定 ${Math.round(info.effective / unit)}${unitLabel} が適用されます）。`;
    }
  });
}
function collectWorkspace(body) {
  _WORKSPACE_FIELDS.forEach(({ put, id, unit }) => {
    const v = (($(id) || {}).value || '').trim();
    if (v !== (_workspaceBaseline[put] || '')) body[put] = v === '' ? null : Math.round(Number(v) * unit);
  });
}
function workspaceChanged() {
  return _WORKSPACE_FIELDS.some(({ put, id }) =>
    ((($(id) || {}).value || '').trim()) !== (_workspaceBaseline[put] || ''));
}
function validateWorkspaceInputs() {
  const errors = [];
  _WORKSPACE_FIELDS.forEach(({ id, label, unitLabel }) => {
    const el = $(id);
    if (!el) return;
    const raw = (el.value || '').trim();
    if (raw === '') return;
    const n = Number(raw);
    const lo = Number(el.min), hi = Number(el.max);
    if (!Number.isFinite(n) || !Number.isInteger(n) || n < lo || n > hi) {
      errors.push(`${label}は${lo}〜${hi}（${unitLabel}）の整数で指定してください`);
    }
  });
  return errors;
}

// チャット画面のクイック入力例（chat_examples）。「チャットと個人ファイル」タブのカード。
function renderChatExamples(ce) {
  ce = ce || {};
  const configured = ce.configured;
  const enabledInput = $('chat-examples-enabled');
  const itemsInput = $('chat-examples-items');
  const enabled = configured != null ? (configured.enabled !== false) : true;
  const items = configured != null ? (configured.items || []) : [];
  if (enabledInput) enabledInput.checked = enabled;
  if (itemsInput) itemsInput.value = items.join('\n');
  _chatExamplesBaseline = { enabled, items };
  const status = $('chat-examples-status');
  if (status) {
    status.textContent = configured != null
      ? `固定中です（${(ce.effective || []).length ? (ce.effective || []).length + '件を表示' : '非表示'}）。`
      : `未設定です（組み込みの既定 ${(ce.default || []).length}例が表示されます）。`;
  }
}
function _collectChatExamplesItems() {
  return (($('chat-examples-items') || {}).value || '').split('\n')
    .map((s) => s.trim()).filter((s) => s !== '');
}
function collectChatExamples() {
  return { enabled: !!($('chat-examples-enabled') || {}).checked, items: _collectChatExamplesItems() };
}
function chatExamplesChanged() {
  const enabledNow = !!($('chat-examples-enabled') || {}).checked;
  const itemsNow = _collectChatExamplesItems();
  return enabledNow !== _chatExamplesBaseline.enabled
    || JSON.stringify(itemsNow) !== JSON.stringify(_chatExamplesBaseline.items);
}
// #cloud-provider-radios は render() の度に innerHTML が入れ替わるため委譲リスナー1本にする。プロバイダを切り替えたらキー欄を再描画し、前のプロバイダ向けのキー値を誤って送らない。
const _cloudRadios = $('cloud-provider-radios');
// click（change ではない）で明示操作を記録する: 選択中の radio の再クリックでは change が発火しない（_cloudProviderTouched 参照）。
if (_cloudRadios) _cloudRadios.addEventListener('click', (e) => {
  if (e.target.matches('input[data-cloud-provider]')) _cloudProviderTouched = true;
});
if (_cloudRadios) _cloudRadios.addEventListener('change', (e) => {
  if (!e.target.matches('input[data-cloud-provider]')) return;
  // プロバイダ切替＝削除待ちの応答はもう今の操作対象ではない（_invalidateCloudKeyClear 内で結果表示もクリアする）。
  _invalidateCloudKeyClear();
  renderCloudKeyBlock((_view && _view.cloud) || {});
  _mcCloudProvider = selectedCloudProvider();
  renderModelCatalogTable();
});
const _cloudKeyTest = $('cloud-key-test');
if (_cloudKeyTest) _cloudKeyTest.addEventListener('click', testCloudKey);
const _cloudKeyClear = $('cloud-key-clear');
if (_cloudKeyClear) _cloudKeyClear.addEventListener('click', clearCloudKey);
const _cloudKeyInput = $('cloud-key');
// キー入力中＝これから保存/削除が起きうる状態。入力の時点で世代を進め、古い削除応答の影響を避ける。
if (_cloudKeyInput) _cloudKeyInput.addEventListener('input', () => _invalidateCloudKeyClear());

const _cloudOllamaTest = $('cloud-ollama-test');
if (_cloudOllamaTest) _cloudOllamaTest.addEventListener('click', testCentralOllama);
const _openaiEndpointTest = $('openai-endpoint-test');
if (_openaiEndpointTest) _openaiEndpointTest.addEventListener('click', testOpenaiEndpoint);
