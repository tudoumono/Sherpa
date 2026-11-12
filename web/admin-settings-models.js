// システム管理「使えるモデル」タブ（model_catalog の表と編集モーダル）。
'use strict';

// ===== 使えるモデル（model_catalog） =====
// 1枚の表＝行=用途・列=選択中のクラウド AI＋Ollama＋Codex のみ。
function mcColumns() {
  const cloudMeta = CLOUD_PROVIDER_LABELS[_mcCloudProvider] || { label: _mcCloudProvider };
  return [
    { key: _mcCloudProvider, label: cloudMeta.label, editable: true },
    { key: 'ollama', label: MC_COLUMN_LABELS.ollama, editable: true },
    { key: 'codex', label: MC_COLUMN_LABELS.codex, editable: true },
  ];
}

function mcCell(provider, usage) {
  return (_mcState[provider] || {})[usage];
}

// 2セル（{allowed,default}）が一致するか。allowed は順序込みで比較する。
function _mcCellEquals(a, b) {
  if (!a || !b) return false;
  return JSON.stringify(a.allowed || []) === JSON.stringify(b.allowed || [])
    && (a.default || '') === (b.default || '');
}

// このセルが組み込み既定（_mcBuiltin）と異なるか（configured にセルがあるだけでは判定しない）。
function mcCellChanged(provider, usage) {
  const cur = mcCell(provider, usage);
  if (!cur) return false;
  const builtin = (_mcBuiltin[provider] || {})[usage];
  if (!builtin) return true;   // 組み込み既定に無い用途/プロバイダの組み合わせ＝差分として扱う
  return !_mcCellEquals(cur, builtin);
}

function renderModelCatalogTable() {
  const wrap = $('model-catalog-table');
  if (!wrap) return;
  const cols = mcColumns();
  let html = '<div style="overflow-x:auto"><table class="mc-table"><thead><tr><th>用途</th>'
    + cols.map((c) => `<th>${esc(c.label)}</th>`).join('') + '</tr></thead><tbody>';
  _mcUsages.forEach((usage) => {
    html += `<tr><td>${esc(MC_USAGE_LABELS[usage] || usage)}</td>`;
    cols.forEach((c) => {
      if (!c.editable) { html += '<td class="mc-na">—</td>'; return; }
      const cell = mcCell(c.key, usage);
      if (!cell) { html += '<td class="mc-na">—</td>'; return; }
      const allowed = cell.allowed || [];
      // default が空のときは、空の「（未設定）」を先頭に明示して選択済みにする（先頭モデルが既定に見えるのを防ぐ）。
      const options = (cell.default ? '' : '<option value="" selected>（未設定）</option>')
        + allowed.map((m) =>
          `<option value="${esc(m)}"${m === cell.default ? ' selected' : ''}>${esc(m)}</option>`).join('');
      const changedCls = mcCellChanged(c.key, usage) ? ' mc-changed' : '';
      html += `<td class="${changedCls.trim()}"><select class="mc-default" data-provider="${esc(c.key)}" data-usage="${esc(usage)}">`
        + (options || '<option value="">（未登録）</option>') + '</select> '
        + `<button type="button" class="mini mc-edit" data-provider="${esc(c.key)}" data-usage="${esc(usage)}">一覧を編集</button></td>`;
    });
    html += '</tr>';
  });
  html += '</tbody></table></div>';
  wrap.innerHTML = html;
}

function renderModelCatalog(mc, cloudProvider) {
  const card = $('model-catalog-card');
  if (!card) return;
  if (!mc) { card.hidden = true; return; }
  card.hidden = false;
  _mcState = JSON.parse(JSON.stringify(mc.effective || {}));
  _mcBaseline = JSON.parse(JSON.stringify(mc.effective || {}));
  _mcBuiltin = mc.builtin || {};
  _mcConfiguredRaw = mc.configured ? JSON.parse(JSON.stringify(mc.configured)) : null;
  _mcTouched = new Set();   // 新しいサーバ状態を基準にする＝このセッションの編集はまだ無い
  _mcUsages = mc.usages || [];
  _mcCloudProvider = cloudProvider || 'openai';
  renderModelCatalogTable();
}

// 「使えるモデル」タブに属する未保存編集があるか（埋め込みデプロイ名は mcEmbedChanged() で別途判定）。
function _mcStateSansEmbed(state) {
  const clone = JSON.parse(JSON.stringify(state || {}));
  if (clone.openai) delete clone.openai.embed;
  return clone;
}
function mcCatalogChanged() {
  return JSON.stringify(_mcState) !== JSON.stringify(_mcBaseline);
}
function mcCatalogChangedExcludingEmbed() {
  return JSON.stringify(_mcStateSansEmbed(_mcState)) !== JSON.stringify(_mcStateSansEmbed(_mcBaseline));
}
function mcEmbedChanged() {
  return JSON.stringify((_mcState.openai || {}).embed || null)
    !== JSON.stringify((_mcBaseline.openai || {}).embed || null);
}

// 保存する model_catalog は _mcState をそのまま送らない（全置換の契約のため、触っていないセルまで明示固定される）。
// 未編集セルは _mcConfiguredRaw の値を維持する。編集したセル（_mcTouched）は
// ① 現在値が raw と一致＝raw を載せる ② raw とも組み込み既定とも異なる＝現在値を載せる ③ それ以外＝除外して組み込み既定への追従に戻す。
function buildModelCatalogBody() {
  const out = {};
  if (_mcConfiguredRaw) {
    Object.keys(_mcConfiguredRaw).forEach((provider) => {
      Object.keys(_mcConfiguredRaw[provider] || {}).forEach((usage) => {
        if (_mcTouched.has(provider + '/' + usage)) return;   // 下のループで扱う
        out[provider] = out[provider] || {};
        out[provider][usage] = _mcConfiguredRaw[provider][usage];
      });
    });
  }
  _mcTouched.forEach((key) => {
    const [provider, usage] = key.split('/');
    const cur = (_mcState[provider] || {})[usage];
    const raw = (_mcConfiguredRaw && _mcConfiguredRaw[provider]) ? _mcConfiguredRaw[provider][usage] : undefined;
    if (raw && _mcCellEquals(cur, raw)) {
      // 元の明示固定値（raw）へ戻した＝pin は維持する。
      out[provider] = out[provider] || {};
      out[provider][usage] = raw;
    } else if (mcCellChanged(provider, usage)) {
      out[provider] = out[provider] || {};
      out[provider][usage] = cur;
    } else if (out[provider]) {
      delete out[provider][usage];
    }
  });
  Object.keys(out).forEach((provider) => {
    if (Object.keys(out[provider]).length === 0) delete out[provider];
  });
  return Object.keys(out).length ? out : null;
}

function openMcModal(provider, usage) {
  const cell = mcCell(provider, usage) || { allowed: [] };
  _mcModalTarget = { provider, usage };
  const cols = mcColumns();
  const colLabel = (cols.find((c) => c.key === provider) || { label: provider }).label;
  $('mc-modal-title').textContent = `${colLabel} — ${MC_USAGE_LABELS[usage] || usage}`;
  $('mc-modal-textarea').value = (cell.allowed || []).join('\n');
  $('mc-overlay').classList.add('open');
  $('mc-modal-textarea').focus();
}
function closeMcModal() { $('mc-overlay').classList.remove('open'); _mcModalTarget = null; }
function saveMcModal() {
  if (!_mcModalTarget) return;
  const { provider, usage } = _mcModalTarget;
  const lines = ($('mc-modal-textarea').value || '').split('\n').map((s) => s.trim()).filter(Boolean);
  const allowed = Array.from(new Set(lines));
  const prevDefault = (mcCell(provider, usage) || {}).default || '';
  const nextDefault = allowed.includes(prevDefault) ? prevDefault : (allowed[0] || '');
  _mcState[provider] = _mcState[provider] || {};
  _mcState[provider][usage] = { allowed, default: nextDefault };
  _mcTouched.add(provider + '/' + usage);
  if (provider === 'openai' && usage === 'embed') syncEmbedDeploymentField();
  closeMcModal();
  renderModelCatalogTable();
}

function renderModelsTab(view) {
  renderModelCatalog(view.model_catalog, (view.cloud || {}).provider);
  renderMcVlmLine(view.vlm);
}

// 視覚読み取りのモデルはこの表では変えない。取り込みタブの設定を読むだけで見せる。
function renderMcVlmLine(vlm) {
  const el = $('mc-vlm-line');
  if (!el) return;
  const eff = vlm && vlm.effective;
  if (!eff) { el.hidden = true; el.textContent = ''; return; }
  const prov = eff.provider === 'openai' ? 'クラウド（OpenAI）' : 'Ollama';
  el.hidden = false;
  el.textContent = `視覚読み取りのモデル（今の設定）: ${prov} の ${eff.model || '未設定'}`;
}

// 画像の文字の読み取り（OCR）の状態。GET /ingest/workers の ocr_message を平文で言い換える（表示だけ）。
function ocrStatusText(msg) {
  return msg || '稼働中です。';
}
async function loadOcrStatus() {
  const el = $('ocr-status');
  if (!el) return;
  try {
    const w = await getJSON('/ingest/workers');
    el.textContent = '今の状態: ' + ocrStatusText(w && w.ocr_message);
  } catch (e) {
    el.textContent = '今の状態: 確認できませんでした（' + (e && e.message ? e.message : '通信に失敗しました') + '）';
  }
}

// 使えるモデル（#model-catalog-table は render() の度に innerHTML が入れ替わるため、常に存在するコンテナへの委譲リスナー1本にする）。
const _mcTable = $('model-catalog-table');
if (_mcTable) {
  _mcTable.addEventListener('click', (e) => {
    const btn = e.target.closest('.mc-edit');
    if (!btn) return;
    openMcModal(btn.dataset.provider, btn.dataset.usage);
  });
  _mcTable.addEventListener('change', (e) => {
    const sel = e.target.closest('.mc-default');
    if (!sel) return;
    const { provider, usage } = sel.dataset;
    _mcState[provider] = _mcState[provider] || {};
    _mcState[provider][usage] = { allowed: (mcCell(provider, usage) || {}).allowed || [], default: sel.value };
    _mcTouched.add(provider + '/' + usage);
    if (provider === 'openai' && usage === 'embed') syncEmbedDeploymentField();
    renderModelCatalogTable();
  });
}
const _mcOverlay = $('mc-overlay');
if (_mcOverlay) {
  _mcOverlay.addEventListener('click', (e) => { if (e.target === _mcOverlay) closeMcModal(); });
  $('mc-modal-close').addEventListener('click', closeMcModal);
  $('mc-modal-cancel').addEventListener('click', closeMcModal);
  $('mc-modal-save').addEventListener('click', saveMcModal);
}
