// システム管理のタブ切り替え・未保存の丸印・変更の強調・保存操作・初期化（admin ガード）。最後に読む。
'use strict';

// ===== タブ切り替え（URL ハッシュで記憶）・未保存タブの丸印 =====
const TAB_KEYS = ['provider', 'research', 'chat', 'models', 'ingest', 'extkeys'];
// 埋め込みタブ（管理系ページを iframe で表示）。保存対象がないため TAB_DIRTY を持たず、切替に未保存確認は挟まない。
const EMBED_TAB_KEYS = ['users', 'usage-page', 'feedback', 'audit', 'status', 'connectivity'];
const ALL_TAB_KEYS = TAB_KEYS.concat(EMBED_TAB_KEYS);
function activateTab(tabKey, opts) {
  if (tabKey === 'agentic-budget-card') {
    activateTab('research', { updateHash: false });
    if (!opts || opts.updateHash !== false) location.hash = tabKey;
    $(tabKey).scrollIntoView({ block: 'start' });
    $(tabKey).focus({ preventScroll: true });
    return;
  }
  if (!ALL_TAB_KEYS.includes(tabKey)) tabKey = TAB_KEYS[0];
  ALL_TAB_KEYS.forEach((k) => {
    const btn = document.querySelector(`.tab-btn[data-tab="${k}"]`);
    const panel = $('tabpanel-' + k);
    const active = k === tabKey;
    if (btn) {
      btn.setAttribute('aria-selected', String(active));
      btn.tabIndex = active ? 0 : -1;
    }
    if (panel) panel.hidden = !active;
  });
  if (EMBED_TAB_KEYS.includes(tabKey)) loadEmbedFrame(tabKey);
  if (!opts || opts.updateHash !== false) location.hash = tabKey;
}
// 埋め込みタブの iframe は遅延ロード: data-src を初回選択時にだけ src へ移す。
function loadEmbedFrame(tabKey) {
  const frame = $('embed-frame-' + tabKey);
  if (frame && !frame.getAttribute('src') && frame.dataset.src) frame.setAttribute('src', frame.dataset.src);
}
document.querySelectorAll('.tab-btn[data-tab]').forEach((btn) =>
  btn.addEventListener('click', () => activateTab(btn.dataset.tab)));
// iframe の読み込みは選択時だけ。矢印キーはフォーカス移動に限定する。
$('admin-tabs').addEventListener('keydown', (e) => {
  const current = e.target.closest('[role="tab"]');
  if (!current) return;
  const tabs = [...$('admin-tabs').querySelectorAll('[role="tab"]')]
    .filter((tab) => !tab.disabled && tab.getClientRects().length);
  const index = tabs.indexOf(current);
  let next;
  if (e.key === 'ArrowDown') next = tabs[(index + 1) % tabs.length];
  else if (e.key === 'ArrowUp') next = tabs[(index - 1 + tabs.length) % tabs.length];
  else if (e.key === 'Home') next = tabs[0];
  else if (e.key === 'End') next = tabs[tabs.length - 1];
  else return;
  e.preventDefault();
  next.focus();
});
window.addEventListener('hashchange', () => activateTab(location.hash.replace('#', ''), { updateHash: false }));

// タブごとの未保存インジケータ。render() 時点の基準値と今の値が異なるタブだけ丸印を出す
// （埋め込みデプロイ名欄は物理的に「プロバイダ＋接続先」タブにあるため、そちらで判定する）。
const TAB_DIRTY = {
  provider: () => cloudChanged() || ollamaAllowlistChanged() || webhookAllowlistChanged()
    || openaiEndpointChanged() || mcEmbedChanged() || embedProviderChanged() || nullPlanActive('provider'),
  research: () => depthProfileChanged() || maxReviewRoundsChanged()
    || codexWorkerModelChanged() || codexSessionRetentionDaysChanged() || agenticBudgetChanged() || codexModeChanged()
    || extKeysResearchProviderChanged() || nullPlanActive('research'),
  chat: () => chatMaxTurnsChanged() || workspaceChanged() || chatExamplesChanged() || nullPlanActive('chat'),
  models: () => mcCatalogChangedExcludingEmbed(),
  ingest: () => armsChanged() || legacyChanged() || vlmChanged() || ragLlmRenderChanged() || ocrSwitchChanged()
    || embedParallelChanged() || embedDimChanged() || embedTuningChanged() || nullPlanActive('ingest'),
  extkeys: () => extKeysAllowedChanged() || extKeysQuotaChanged(),
};
function refreshTabDots() {
  TAB_KEYS.forEach((k) => {
    const dot = $('tab-dot-' + k);
    if (dot) dot.hidden = !TAB_DIRTY[k]();
  });
  $('unsaved-note').hidden = !document.querySelector('#admin-tabs .tab-dot:not([hidden])');
}
// 保存していない変更（タブの丸印）があるまま離れるときだけ、ブラウザ標準の確認を出す。
window.addEventListener('beforeunload', (e) => {
  if (document.querySelector('#admin-tabs .tab-dot:not([hidden])')) e.preventDefault();
});

// 接続先関連（種別ラジオ・埋め込みデプロイ名）の変更監視は、対象の id/属性だけで判定する（DOM 構造に依存しない）。埋め込み欄は 'change' で反映する。
// このブロックは下の refreshTabDots 登録より前に置く: どちらも document に直接束縛するため登録順が実行順になり、先に状態を更新してから refreshTabDots で読ませる必要がある。
document.addEventListener('change', (e) => {
  if (e.target.matches('input[data-openai-endpoint-kind]')) updateOpenaiEndpointFieldsVisibility();
  if (e.target.id === 'openai-endpoint-embed-deployment') applyEmbedDeploymentFieldEdit();
});

// 各カードのリスナーは対象要素に束縛されバブリングで document に届くため、document 直接束縛のリスナーより後に走る。
// document を対象にするのは、モーダルが #main-content の外にあり、そこでの操作も拾うため。
['input', 'change', 'click'].forEach((evt) => document.addEventListener(evt, refreshTabDots));

// 既定から変えた項目だけ強調する（全タブ）。組み込み既定の値そのものと比較する。使えるモデルの表はセル単位で mcCellChanged() が強調する。
function applyConfigChangedHighlights(view) {
  const mark = (el, changed) => { if (el) el.classList.toggle('cfg-changed', !!changed); };
  const cloud = view.cloud || {};
  const oe = (view.openai_endpoint || {}).configured || {};
  mark($('cloud-provider-radios'), (cloud.provider || 'openai') !== 'openai');
  mark($('personal-keys-allowed'), !!cloud.personal_api_keys_allowed);
  mark($('web-search-allowed'), !!cloud.web_search_allowed);
  mark($('cloud-ollama-url'), !!(cloud.ollama_url && cloud.ollama_url !== 'http://localhost:11434'));
  mark($('cloud-ollama-allowlist'), !!(view.ollama_allowlist && (view.ollama_allowlist.configured || []).length));
  mark($('webhook-allowlist'), !!(view.webhook_allowlist && (view.webhook_allowlist.configured || []).length));
  mark($('embed-provider'), (view.embed_provider || {}).effective !== (view.embed_provider || {}).default);
  mark($('openai-endpoint-radios'), !!(oe.kind && oe.kind !== 'openai'));
  mark($('openai-endpoint-base-url'), !!oe.base_url);
  mark($('openai-endpoint-auth-header'), !!(oe.auth_header && oe.auth_header !== 'bearer'));
  mark($('openai-endpoint-api-version'), !!oe.api_version);
  const dp = view.depth_profile || {};
  _DEPTH_BASE_FIELDS.forEach(({ view: vk, id }) => {
    const info = dp[vk] || {};
    mark($(id), info.effective !== info.default);
  });
  const reasoning = dp.codex_reasoning || {};
  mark($('depth-base-codex-reasoning'), reasoning.effective !== reasoning.default);
  mark($('codex-mode'), view.codex_mode.effective !== view.codex_mode.default);
  mark($('max-review-rounds'), view.max_review_rounds.effective !== view.max_review_rounds.default);
  mark($('codex-worker-model'), view.codex_worker_model.effective !== view.codex_worker_model.default);
  mark($('codex-session-retention-days'),
    view.codex_session_retention_days.effective !== view.codex_session_retention_days.default);
  mark($('embed-parallel'), view.embed_parallel.effective !== view.embed_parallel.default);
  mark($('embed-dim'), (view.embed_dim || {}).effective !== (view.embed_dim || {}).default);
  EMBED_TUNING.forEach(({ key, id }) => mark($(id), (view[key] || {}).effective !== (view[key] || {}).default));
  const cmt = view.chat_max_turns || {};
  _CHAT_MAX_TURNS_FIELDS.forEach(({ view: vk, id }) => {
    const info = cmt[vk] || {};
    mark($(id), info.effective !== info.default);
  });
  const ws = view.workspace || {};
  _WORKSPACE_FIELDS.forEach(({ view: vk, id }) => {
    const info = ws[vk] || {};
    mark($(id), info.effective !== info.default);
  });
  const ce = view.chat_examples || {};
  mark($('chat-examples-card'), (ce.configured != null));
  const arms = view.arms || {};
  mark($('arms-list'), JSON.stringify([...(arms.enabled || [])].sort())
    !== JSON.stringify([...(arms.env_default || [])].sort()));
  const legacy = view.legacy_backend || {};
  mark($('legacy-radios'), !!legacy.effective && legacy.effective !== (legacy.default || 'none'));
  const vlm = view.vlm || {};
  const vlmDefault = vlm.default || {};
  mark($('vlm-block'), !!vlm.effective && (
    vlm.effective.provider !== (vlmDefault.provider || 'ollama')
    || (vlm.effective.model || '') !== (vlmDefault.model || '')
    || !!vlm.effective.cloud_allowed !== !!vlmDefault.cloud_allowed));
  const ragRender = view.rag_llm_render || {};
  mark($('rag-llm-render-card'), !!ragRender.effective !== !!ragRender.default); markOcrSwitch(mark, view);
  const ab = view.agentic_budget || {};
  _AGENTIC_BUDGET_FIELDS.forEach(({ view: vk, id }) => {
    const info = ab[vk] || {};
    mark($(id), info.effective !== info.default);
  });
  mark($('ext-keys-user-allowed'), !!(view.ext_keys && view.ext_keys.user_api_keys_allowed));
  const quota = (view.ext_keys || {}).daily_quota_default || {};
  mark($('ext-keys-user-quota-default'), quota.effective !== quota.default);
  const rdp = (view.ext_keys || {}).research_default_provider || {};
  mark($('ext-research-default-provider'), rdp.effective !== rdp.default);
}

$('save').addEventListener('click', save);
// Ctrl+S（Cmd+S）でも保存。ただし API キー発行モーダルが開いている間は、ブラウザの既定動作だけを止めて保存は呼ばない。
document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
    e.preventDefault();
    const ekOverlay = $('ek-overlay');
    if (ekOverlay && ekOverlay.classList.contains('open')) return;
    if (!$('save').disabled) save();
  }
});
function syncFrameTheme(frame) {
  frame.contentDocument.documentElement.dataset.theme = document.documentElement.dataset.theme;
}
document.querySelectorAll('.embed-frame').forEach((frame) => {
  frame.addEventListener('load', () => syncFrameTheme(frame));
});
document.addEventListener('sherpa-themechange', () => {
  document.querySelectorAll('.embed-frame[src]').forEach(syncFrameTheme);
});

// ===== 初期化（admin ガード）=====
(async () => {
  const isAdmin = await checkAdmin();
  if (isAdmin !== 'admin') {
    const main = $('main-content'), denied = $('access-denied'), bar = $('save-bar');
    if (main) main.style.display = 'none';
    if (denied) denied.style.display = 'block';
    if (isAdmin === 'unreachable') showUnreachable(denied);
    if (bar) bar.style.display = 'none';   // 非 admin には保存バーも出さない
    return;
  }
  activateTab(location.hash.replace('#', ''), { updateHash: false });
  load();
  loadExtKeys();   // 独立取得（GET /ext/v1/admin/keys は /admin/settings と別エンドポイント）
})();
