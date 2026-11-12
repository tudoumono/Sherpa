// システム管理「取り込み」タブ（読み取り方式・旧形式の変換・視覚読み取り・必要な道具）。
'use strict';

// ===== 描画 =====
function renderArms(arms) {
  const list = $('arms-list');
  const known = arms.known || [];
  const enabled = new Set(arms.enabled || []);
  const available = arms.available || {};      // 名前→この端末で使えるか（未定義の既知アームは使える扱い）
  list.innerHTML = known.map((name) => {
    const meta = ARM_LABELS[name] || { label: name, desc: '' };
    const checked = enabled.has(name) ? ' checked' : '';
    // 未導入アーム（available===false）は選べない（disabled）＋導入案内。
    const missing = (name in available) && !available[name];
    const disabled = missing ? ' disabled' : '';
    const hint = missing && ARM_MISSING_HINT[name]
      ? `<div class="arm-d danger">${esc(ARM_MISSING_HINT[name])}</div>` : '';
    return `<label class="armrow">`
      + `<input type="checkbox" data-arm="${esc(name)}"${checked}${disabled}>`
      + `<span><span class="arm-t">${esc(meta.label)}</span>`
      + (meta.desc ? `<div class="arm-d">${esc(meta.desc)}</div>` : '')
      + hint
      + `</span></label>`;
  }).join('') || '<div class="hint">利用可能な読み取り方式がありません。</div>';
}

// 「既定に従っています」／「この一覧で固定中」の平文ヒント。
function renderArmsStatus(arms) {
  const el = $('arms-status');
  if (!el) return;
  el.textContent = (arms && arms.configured != null)
    ? 'この一覧で固定中です（既定（標準）の変更にはこの先も追従しません）。'
    : '既定（標準）に従っています（変更して保存すると、この一覧の内容で固定されます）。';
}

// 旧形式変換バックエンドのラジオ描画。応答に legacy_backend が無ければブロックごと隠す。
// 選択状態は configured があれば configured、無ければ effective（none も明示的な選択のため）。選択肢が無い応答では null（collectLegacy() の「未選択」と揃える）。
function _legacySelectedValue(lb) {
  if (!lb || !Array.isArray(lb.options) || !lb.options.length) return null;
  return (lb.configured !== undefined && lb.configured !== null) ? lb.configured : (lb.effective || 'none');
}

function renderLegacy(lb) {
  const block = $('legacy-block');
  if (!block) return;
  if (!lb || !Array.isArray(lb.options) || !lb.options.length) { block.hidden = true; return; }
  block.hidden = false;
  const selected = _legacySelectedValue(lb);
  const defaultName = lb.default || 'none';
  const loOk = !!(lb.libreoffice && lb.libreoffice.available);
  // Office 連携は実際に到達できるとき（http ワーカー or direct で Office を検出）だけ選べる。
  const oc = lb.office_com || {};
  const ocOk = !!oc.available;
  $('legacy-radios').innerHTML = lb.options.map((name) => {
    const meta = LEGACY_LABELS[name] || { label: name, desc: '' };
    const checked = name === selected ? ' checked' : '';
    // 変換手段が無い選択肢は選べない（disabled）。LibreOffice は soffice 未検出時・Office 連携は到達不可時。
    const disabled = ((name === 'libreoffice' && !loOk) || (name === 'office_com' && !ocOk)) ? ' disabled' : '';
    const reason = (name === 'libreoffice' && !loOk) ? 'LibreOffice が入っていません'
      : ((name === 'office_com' && !ocOk) ? 'Office 連携が使えません' : '');
    let ver = '';
    if (name === 'libreoffice' && loOk && lb.libreoffice.version) {
      ver = ` <code>${esc(lb.libreoffice.version)}</code>`;
    } else if (name === 'office_com' && ocOk) {
      const vs = ocVersionSummary(oc.versions);
      if (vs) ver = ` <code>${esc(vs)}</code>`;
      // 動作形態を平文で添える（同一マシン直接 or 別ホストのワーカー）。
      const modeNote = oc.mode === 'direct' ? '（このパソコンの Office を直接使用）'
        : (oc.mode === 'http' ? '（別のマシンのワーカー経由）' : '');
      if (modeNote) ver += ` <span class="muted">${esc(modeNote)}</span>`;
    }
    // 「（既定）」マーカーは実際の既定と一致する選択肢にだけ動的に付ける。
    const label = meta.label + (name === defaultName ? '（既定）' : '');
    return `<label class="armrow">`
      + `<input type="radio" name="legacy-backend" data-legacy="${esc(name)}"${checked}${disabled}>`
      + `<span><span class="arm-t">${esc(label)}</span>${ver}`
      + (meta.desc ? `<div class="arm-d">${esc(meta.desc)}</div>` : '')
      + (reason ? `<div class="arm-d danger">${esc(reason)}</div>` : '')
      + `</span></label>`;
  }).join('');
  const miss = $('legacy-lo-missing');
  if (miss) {
    if (!loOk) {
      miss.hidden = false;
      miss.textContent = 'この環境では LibreOffice が見つかりません'
        + '（インストール: sudo apt-get install libreoffice-writer libreoffice-calc libreoffice-impress）。';
    } else { miss.hidden = true; miss.textContent = ''; }
  }
  // Office 連携（office_com）が使えない場合の案内（3形態を区別）。
  const ocmiss = $('legacy-oc-missing');
  if (ocmiss) {
    const hasOc = Array.isArray(lb.options) && lb.options.indexOf('office_com') !== -1;
    if (hasOc && !ocOk) {
      ocmiss.hidden = false;
      ocmiss.textContent = oc.configured_url
        // http モード: 別ホストのワーカー URL は設定済みだが到達できない。
        ? '別のマシンの Office 連携ワーカーに接続できません。そのマシンでワーカーが起動しているか確認してください'
          + '（起動例: powershell -ExecutionPolicy Bypass -STA -File deploy\\office-com-worker.ps1）。'
        // unavailable: 同一マシンの直接連携（WSL 連携で powershell.exe）が見つからず、別ホストの URL 未設定。
        : '同じ Windows の Office を直接使う準備が見つかりませんでした（この環境から Windows 連携が使えるかご確認ください）。'
          + '別のマシンの Office を使う場合は、そのマシンでワーカーを起動して接続先 SHERPA_OFFICE_COM_URL を設定してください'
          + '（起動例: powershell -ExecutionPolicy Bypass -STA -File deploy\\office-com-worker.ps1）。';
    } else { ocmiss.hidden = true; ocmiss.textContent = ''; }
  }
}

// office_com healthz の versions（{word,excel,powerpoint}）を短い表示文字列へ（検出できたものだけ）。
function ocVersionSummary(versions) {
  if (!versions || typeof versions !== 'object') return '';
  const parts = [];
  const labels = { word: 'Word', excel: 'Excel', powerpoint: 'PowerPoint' };
  for (const key of ['word', 'excel', 'powerpoint']) {
    const v = versions[key];
    if (v && typeof v !== 'boolean') parts.push(`${labels[key]} ${v}`);
  }
  return parts.join(' / ');
}

// 「既定に従っています（今の既定: ...）」／「この選択で固定中」の平文ヒント。
function renderLegacyStatus(lb) {
  const el = $('legacy-status');
  if (!el || !lb) return;
  const defaultName = lb.default || 'none';
  const defaultLabel = (LEGACY_LABELS[defaultName] || { label: defaultName }).label;
  el.textContent = (lb.configured !== undefined && lb.configured !== null)
    ? 'この選択で固定中です（既定の変更にはこの先も追従しません）。'
    : `既定に従っています（今の既定: ${defaultLabel}）。`;
}

// 視覚読み取りの VLM 設定を描画（応答に vlm が無ければブロックごと隠す）。
function renderVlm(vlm) {
  const block = $('vlm-block');
  if (!block) return;
  if (!vlm || !vlm.effective) { block.hidden = true; return; }
  block.hidden = false;
  const eff = vlm.effective;
  const provSel = $('vlm-provider');
  if (provSel) provSel.value = (eff.provider === 'openai') ? 'openai' : 'ollama';
  const modelInput = $('vlm-model');
  if (modelInput) {
    modelInput.value = eff.model || '';
    const def = (vlm.default && vlm.default.model) || 'qwen2.5vl';
    modelInput.placeholder = def;
  }
  const cloud = $('vlm-cloud-allowed');
  if (cloud) cloud.checked = !!eff.cloud_allowed;
  // クラウド選択かつ OpenAI キー未設定の案内（画像は送られない＝読み取りできない）。
  const keyMiss = $('vlm-key-missing');
  if (keyMiss) {
    const needKey = (provSel && provSel.value === 'openai') && !vlm.openai_key_present;
    if (needKey) {
      keyMiss.hidden = false;
      keyMiss.textContent = VLM_KEY_MISSING_TEXT;
    } else { keyMiss.hidden = true; keyMiss.textContent = ''; }
  }
  updateVlmUrlHint(vlm);
}

// Ollama を選んでいるときだけ、画像の実際の送り先（応答の vlm.effective.ollama_url）を見せる。
function updateVlmUrlHint(vlm) {
  const el = $('vlm-ollama-url');
  const provSel = $('vlm-provider');
  if (!el || !provSel) return;
  const url = String(((vlm || (_view && _view.vlm) || {}).effective || {}).ollama_url || '').trim();
  if (provSel.value === 'ollama' && url) {
    el.hidden = false;
    el.textContent = `今の接続先: ${url}（画像はこの接続先の Ollama へ送られます）`;
  } else { el.hidden = true; el.textContent = ''; }
}

function renderVlmStatus(vlm) {
  const el = $('vlm-status');
  if (!el || !vlm) return;
  el.textContent = (vlm.configured != null)
    ? 'この設定で固定中です（既定の変更にはこの先も追従しません）。'
    : '既定に従っています（変更して保存すると、この内容で固定されます）。';
}

// rag.md の LLM 成形トグル。キー不在ならカードごと隠す。
function renderRagLlmRender(rr) {
  const card = $('rag-llm-render-card');
  if (!card) return;
  if (!rr) { card.hidden = true; return; }
  card.hidden = false;
  const cb = $('rag-llm-render');
  if (cb) cb.checked = !!rr.effective;
}

function renderRagLlmRenderStatus(rr) {
  const el = $('rag-llm-render-status');
  if (!el || !rr) return;
  el.textContent = (rr.configured != null)
    ? 'この設定で固定中です（既定の変更にはこの先も追従しません）。'
    : '既定に従っています（変更して保存すると、この内容で固定されます）。';
}

// 「必要な道具」表。応答に required_tools が無ければカードごと隠す（前方互換）。
function renderRequiredTools(tools) {
  const card = $('required-tools-card');
  if (!card) return;
  if (!Array.isArray(tools) || !tools.length) { card.hidden = true; return; }
  card.hidden = false;
  $('required-tools-body').innerHTML = tools.map((t) => {
    const state = t.installed
      ? `入っています${t.version ? ' <code>' + esc(t.version) + '</code>' : ''}`
      : `<span class="danger">入っていません</span>${t.detail ? ' <span class="muted">（' + esc(t.detail) + '）</span>' : ''}`;
    return `<tr data-tool="${esc(t.id)}"><td>${esc(t.label)}</td><td>${state}</td>`
      + `<td>${(t.used_by || []).map(esc).join('<br>')}</td>`
      + `<td class="${t.installed ? 'mc-na' : ''}">${t.installed ? '―' : '<code>' + esc(t.how_to_install) + '</code>'}</td></tr>`;
  }).join('');
}

function renderIngestTab(view) {
  loadOcrStatus();
  renderRequiredTools(view.required_tools);
  renderArms(view.arms || {});
  renderArmsStatus(view.arms || {});
  renderLegacy(view.legacy_backend);
  renderLegacyStatus(view.legacy_backend);
  renderVlm(view.vlm);
  renderVlmStatus(view.vlm);
  renderRagLlmRender(view.rag_llm_render);
  renderRagLlmRenderStatus(view.rag_llm_render); renderOcrSwitch(view);
  _armsBaseline = [...((view.arms || {}).enabled || [])].sort();
  _legacyBaseline = _legacySelectedValue(view.legacy_backend);
  const vlm = view.vlm;
  _vlmBaseline = (vlm && vlm.effective) ? {
    provider: vlm.effective.provider === 'openai' ? 'openai' : 'ollama',
    model: vlm.effective.model || '', cloud_allowed: !!vlm.effective.cloud_allowed,
  } : null;
  _ragLlmRenderBaseline = !!(view.rag_llm_render && view.rag_llm_render.effective);
  const parallel = view.embed_parallel;
  _embedParallelBaseline = parallel.configured == null ? '' : String(parallel.configured);
  $('embed-parallel').value = _embedParallelBaseline;
  $('embed-parallel-hint').textContent = parallel.configured == null
    ? `未設定です（既定 ${parallel.effective} 件が適用されます）。`
    : `この値で固定中です（既定: ${parallel.default} 件）。`;
  const dim = view.embed_dim || {};
  _embedDimBaseline = dim.configured == null ? '' : String(dim.configured);
  $('embed-dim').value = _embedDimBaseline;
  $('embed-dim').min = dim.min || 64;
  $('embed-dim').max = dim.max || 4096;
  $('embed-dim').disabled = dim.applies === false;
  $('embed-dim-hint').textContent = dim.applies === false
    ? 'ローカル（Ollama）で埋め込む間は、この設定は使われません（モデルごとの値になります）。'
    : dim.configured == null
      ? `未設定です（${dim.default} が適用されます。これまでと同じ値です）。`
      : dim.configured !== dim.effective
        ? `この値はモデルが対応する値より大きいため、${dim.effective} が適用されます。`
        : `この値で固定中です（空欄のときは ${dim.default}）。`;
  renderEmbedTuning(view);
}

// 埋め込みの 1 回の待ち時間と件数（`embed_request_timeout_s`・`embed_batch_size`）。画面の部品・単位・保存のキーを 1 か所に持つ。
const EMBED_TUNING = [
  { key: 'embed_request_timeout_s', id: 'embed-request-timeout', unit: '秒' },
  { key: 'embed_batch_size', id: 'embed-batch-size', unit: '件' },
];
const _embedTuningBaseline = {};

function renderEmbedTuning(view) {
  EMBED_TUNING.forEach(({ key, id, unit }) => {
    const info = view[key] || {};
    _embedTuningBaseline[key] = info.configured == null ? '' : String(info.configured);
    $(id).value = _embedTuningBaseline[key];
    $(id).min = info.min || 1;
    $(id).max = info.max || 1000;
    $(id + '-hint').textContent = info.configured == null
      ? `未設定です（既定 ${info.effective} ${unit}が適用されます）。`
      : `この値で固定中です（既定: ${info.default} ${unit}）。`;
  });
}

function embedTuningChanged() {
  return EMBED_TUNING.some(({ key, id }) => $(id).value.trim() !== _embedTuningBaseline[key]);
}

function collectEmbedTuning(body) {
  EMBED_TUNING.forEach(({ key, id }) => {
    if ($(id).value.trim() !== _embedTuningBaseline[key]) body[key] = $(id).value === '' ? null : Number($(id).value);
  });
}

// VLM 設定の provider 変更時はキー未設定案内だけ即時更新する（入力中の値を消さない）。
function updateVlmKeyHint() {
  const keyMiss = $('vlm-key-missing');
  const provSel = $('vlm-provider');
  if (!keyMiss || !provSel) return;
  const keyPresent = !!(_view && _view.vlm && _view.vlm.openai_key_present);
  const needKey = provSel.value === 'openai' && !keyPresent;
  if (needKey) {
    keyMiss.hidden = false;
    keyMiss.textContent = VLM_KEY_MISSING_TEXT;
  } else { keyMiss.hidden = true; keyMiss.textContent = ''; }
}
const _vlmBlock = $('vlm-block');
if (_vlmBlock) _vlmBlock.addEventListener('change', (e) => {
  if (e.target.matches('#vlm-provider, #vlm-model, #vlm-cloud-allowed')) { updateVlmKeyHint(); updateVlmUrlHint(); }
});
