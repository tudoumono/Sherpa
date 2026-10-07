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
    desc: 'PDF に埋め込まれた文字を抽出します（画像だけの PDF は読み取れません）。',
  },
  vision: {
    label: '画像・スキャン文書を AI が見て読み取り（視覚読み取り）',
    desc: '画像やスキャンした文書を AI（視覚モデル）が見て読み取ります。既定はこのパソコンのローカル AI（Ollama）で処理します。クラウド AI は下の「視覚読み取りの AI」で明示的に許可したときだけ使います。',
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
  chat: 'チャット', intent: '依頼の仕分け',
  embed: '検索の索引づくり', subsearch: '簡易回答のモデル', codex: 'Codex',
  render: '検索用文書の整形',
};
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

// 整数5項目の入力欄 id と PUT/GET のキー名対応（GET は view.depth_profile.<view>・PUT は body.<put>）。
const _DEPTH_BASE_FIELDS = [
  { view: 'grep_max_hits', put: 'depth_base_grep_max_hits', id: 'depth-base-grep-max-hits',
    label: '資料検索のヒット件数上限' },
  { view: 'qa_max_hits', put: 'depth_base_qa_max_hits', id: 'depth-base-qa-max-hits',
    label: '内容の質問でのヒット件数上限' },
  { view: 'read_window', put: 'depth_base_read_window', id: 'depth-base-read-window',
    label: '1回に読み取る前後の行数' },
  { view: 'impact_depth', put: 'depth_base_impact_depth', id: 'depth-base-impact-depth',
    label: '影響分析でたどる段数' },
  { view: 'troubleshoot_depth', put: 'depth_base_troubleshoot_depth', id: 'depth-base-troubleshoot-depth',
    label: '原因調査でたどる段数' },
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

async function checkAdmin() {
  try {
    const u = await getJSON('/auth/me');
    if (u && u.role === 'admin') return true;
  } catch (_) { /* compat */ }
  return false;
}

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
      + `<span><span class="arm-t">${esc(meta.label)}</span> <code>${esc(name)}</code>`
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
      keyMiss.textContent = 'クラウド（OpenAI）を選んでいますが、OpenAI の API キー（OPENAI_API_KEY）が設定されていません。'
        + 'キーを設定するまで視覚読み取りは行われません。';
    } else { keyMiss.hidden = true; keyMiss.textContent = ''; }
  }
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

// ===== クラウド AI プロバイダの中央設定 =====

function selectedCloudProvider() {
  const el = document.querySelector('#cloud-provider-radios input[data-cloud-provider]:checked');
  return el ? el.dataset.cloudProvider : 'openai';
}

function renderCloudProviderRadios(cloud) {
  const wrap = $('cloud-provider-radios');
  if (!wrap) return;
  const providers = cloud.providers || ['openai'];
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
  el.textContent = `現在選択中のクラウド AI: ${meta.label}`
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
    const d = await (await fetch('/admin/settings/openai-endpoint-test', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    })).json();
    res.className = 'tres ' + (d.ok ? 'ok' : 'danger');
    res.textContent = (d.ok ? '✓ 接続OK' : '✗ ' + (d.detail || '失敗')) + (d.model ? `（${d.model}）` : '');
  } catch (e) {
    res.className = 'tres danger'; res.textContent = '✗ テストに失敗しました';
  } finally {
    _endpointTestBusy = false;
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
    const d = await (await fetch('/settings/test', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    })).json();
    res.className = 'tres ' + (d.ok ? 'ok' : 'danger');
    res.textContent = (d.ok ? '✓ 接続OK' : '✗ ' + (d.detail || '失敗')) + (d.model ? `（${d.model}）` : '');
  } catch (e) {
    res.className = 'tres danger'; res.textContent = '✗ テストに失敗しました';
  }
}

// 中央 API キーの削除。確認ダイアログを挟み、確定した操作でだけ空文字を PUT する（キー欄を空のまま保存しても「未入力＝変更しない」）。
// 要求時点のプロバイダ・世代を捕捉し、応答到着時に両方一致する時だけ反映する（不一致なら _view・表示に触れず捨てる）。
let _cloudKeyClearGen = 0;
// プロバイダ切替・キー入力・保存・タブのリセットのいずれでも呼ぶ。破棄した削除待ちの結果表示をニュートラルへ戻す。
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

// ===== 外部連携（API キー）=====

function renderExtKeysToggle(extKeys) {
  const cb = $('ext-keys-user-allowed');
  if (cb) cb.checked = !!(extKeys && extKeys.user_api_keys_allowed);
  const quota = (extKeys && extKeys.daily_quota_default) || {};
  const input = $('ext-keys-user-quota-default');
  if (input) input.value = quota.configured != null ? quota.configured : '';
  const hint = $('ext-keys-user-quota-default-hint');
  if (hint) {
    const base = quota.configured != null
      ? `この値で固定中です（既定値: ${quota.effective}件）。`
      : `未設定です（組み込みの既定 ${quota.effective}件が適用されます）。`;
    // 今後の新規発行にのみ効く（発行済みキーの上限は発行時の値のまま）。
    hint.textContent = base + '変更は新規発行から適用されます（発行済みのキーの上限は変わりません）。';
  }
  // 簡易回答に使う AI（ollama/openai の2択）。保存値がどちらでもない場合は、select の一時的な選択肢とヒントでその旨を示す。
  const rdp = (extKeys && extKeys.research_default_provider) || {};
  const rdpKnown = rdp.effective === 'openai' || rdp.effective === 'ollama';
  const rdpSel = $('ext-research-default-provider');
  if (rdpSel) {
    let invalidOpt = rdpSel.querySelector(`option[value="${_RESEARCH_PROVIDER_INVALID}"]`);
    if (!rdpKnown) {
      if (!invalidOpt) {
        invalidOpt = document.createElement('option');
        invalidOpt.value = _RESEARCH_PROVIDER_INVALID;
        rdpSel.insertBefore(invalidOpt, rdpSel.firstChild);
      }
      invalidOpt.textContent = String(rdp.effective);
      rdpSel.value = _RESEARCH_PROVIDER_INVALID;
    } else {
      if (invalidOpt) invalidOpt.remove();
      rdpSel.value = rdp.effective;
    }
  }
  const rdpHint = $('ext-research-default-provider-hint');
  const rdpInvalid = $('ext-research-default-provider-invalid');
  if (rdpInvalid) rdpInvalid.hidden = rdpKnown;
  if (rdpHint) {
    const label = (v) => (v === 'openai' ? 'クラウド（OpenAI）' : 'ローカル（Ollama）');
    if (!rdpKnown) {
      rdpHint.textContent = '';
      if (rdpInvalid) {
        rdpInvalid.textContent = '保存されている値が正しくありません。'
          + 'ローカル（Ollama）またはクラウド（OpenAI）を選び直して保存してください。';
      }
    } else {
      rdpHint.textContent = rdp.configured
        ? `この AI で固定中です（未設定に戻すと ${label(rdp.default)} になります）。`
        : `未設定です（組み込みの既定 ${label(rdp.default)} が適用されます）。`;
    }
  }
}

function extKeyStatus(row) {
  if (row.revoked_at) return { cls: 'ek-revoked', label: '失効済み' };
  if (row.expires_at && new Date(row.expires_at).getTime() <= Date.now()) {
    return { cls: 'ek-expired', label: '期限切れ' };
  }
  return { cls: 'ek-active', label: '有効' };
}

function renderExtKeysList(rows) {
  _extKeys = rows;
  const wrap = $('ext-keys-list');
  if (!wrap) return;
  if (!rows.length) {
    wrap.innerHTML = '<div class="hint">発行済みのキーはありません。</div>';
    return;
  }
  const rowsHtml = rows.map((r) => {
    const st = extKeyStatus(r);
    const worldsText = r.allowed_worlds ? r.allowed_worlds.join(', ') : '全て';
    const ownerText = r.owner_uid ? `${r.owner_uid}（本人発行）` : r.created_by;
    const revokeBtn = r.revoked_at ? ''
      : `<button class="mini ek-danger" type="button" data-ek-revoke="${r.id}">失効</button>`;
    // Webhook 登録の有無（host:port のみ・secret は出さない）。
    const webhookText = r.webhook ? (r.webhook_host || '登録済み') : '—';
    return `<tr>`
      + `<td>${esc(r.label)}</td>`
      + `<td><code>${esc(r.key_prefix)}</code></td>`
      + `<td>${esc(worldsText)}</td>`
      + `<td>${esc(ownerText)}</td>`
      + `<td>${esc(Sherpa.fmtDateTime(r.created_at))}</td>`
      + `<td class="ek-muted">${r.last_used_at ? esc(Sherpa.fmtDateTime(r.last_used_at)) : '未使用'}</td>`
      + `<td>${r.call_count}</td>`
      + `<td class="ek-muted">${r.expires_at ? esc(Sherpa.fmtDateTime(r.expires_at)) : '無期限'}</td>`
      + `<td class="ek-muted">${esc(webhookText)}</td>`
      + `<td><span class="ek-badge ${st.cls}">${esc(st.label)}</span></td>`
      + `<td>${revokeBtn}</td>`
      + `</tr>`;
  }).join('');
  wrap.innerHTML = `<div style="overflow-x:auto"><table class="ek-table"><thead><tr>`
    + `<th>ラベル</th><th>キーの識別部分</th><th>対象フォルダ</th><th>発行者</th><th>作成日</th>`
    + `<th>最終利用</th><th>呼出数（30日）</th><th>期限</th><th>Webhook</th><th>状態</th><th></th></tr></thead>`
    + `<tbody>${rowsHtml}</tbody></table></div>`;
}

// 一覧 GET の世代番号。応答到着時に、より新しい呼び出しが始まっていれば破棄する（最後に発行した呼び出しの結果だけを採用）。
let _ekListGen = 0;

async function loadExtKeys() {
  const myGen = ++_ekListGen;
  try {
    const d = await getJSON('/ext/v1/admin/keys');
    if (myGen !== _ekListGen) return;   // 自分より新しい loadExtKeys() が既に呼ばれている
    renderExtKeysList(d.keys || []);
  } catch (e) {
    if (myGen !== _ekListGen) return;
    const wrap = $('ext-keys-list');
    if (wrap) wrap.innerHTML = '<div class="hint danger">キー一覧を読み込めませんでした</div>';
  }
}

// 発行モーダルの状態機械: 'idle'（入力中）→ 'issuing'（応答待ち・閉鎖不可）→ 'revealed'（キー本体を表示中）。issuing の間は閉じる手段を全て無効化する。
// 操作トークン（_ekActiveOp）: open/close のたびに新しい値を発行し、非同期処理は自分のトークンと一致する時だけ画面状態を変える。
// 一覧の再取得（loadExtKeys()）は発行の成否判定から切り離す（await しない）。
let _ekModalState = 'idle';
let _ekOpSeq = 0;
let _ekActiveOp = 0;

function _ekClearRevealedKey() {
  // 平文はモーダルを開く/閉じるたびに DOM から消す。
  const el = $('ek-reveal-key');
  if (el) el.textContent = '';
  const wh = $('ek-reveal-webhook-secret');
  if (wh) wh.textContent = '';
  const whWrap = $('ek-reveal-webhook');
  if (whWrap) whWrap.hidden = true;
}

function _genOpId() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  // crypto.randomUUID が無い環境向けの UUID v4 フォールバック（サーバーは client_op_id を UUID 形式のみ受理）。
  const hex = () => Math.floor(Math.random() * 16).toString(16);
  const h = (n) => Array.from({ length: n }, hex).join('');
  const variant = (8 + Math.floor(Math.random() * 4)).toString(16);
  return `${h(8)}-${h(4)}-4${h(3)}-${variant}${h(3)}-${h(12)}`;
}

function _todayLocalDateStr() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function _ekSleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// モーダルが開いている間、背後を inert にする。#toast は対象外。
function _ekSetBackgroundInert(on) {
  document.querySelectorAll('body > *').forEach((el) => {
    if (el.id === 'ek-overlay' || el.id === 'toast' || el.tagName === 'SCRIPT') return;
    if (on) el.setAttribute('inert', ''); else el.removeAttribute('inert');
  });
}

let _ekOpenerEl = null;   // モーダルを開く直前にフォーカスがあった要素（閉じる時に復帰させる）。

function openExtKeyModal() {
  if (_ekModalState === 'issuing') return;   // 応答待ち中の再入は拒否（open 側の入口でも防ぐ）
  _ekOpenerEl = document.activeElement;
  _ekActiveOp = ++_ekOpSeq;   // 新しいモーダルセッション＝それ以前の遅延処理の結果を無効化する
  _ekModalState = 'idle';
  $('ek-modal-title').textContent = 'API キーを発行';
  $('ek-issue-form').hidden = false;
  $('ek-reveal').hidden = true;
  _ekClearRevealedKey();
  $('ek-copy-res').textContent = '';
  $('ek-issue-err').textContent = '';
  $('ek-label').value = '';
  $('ek-worlds').value = '';
  $('ek-expires').value = '';
  $('ek-expires').min = _todayLocalDateStr();   // 過去日を選ばせない（サーバ側422と二重防御）
  $('ek-quota').value = '';
  $('ek-webhook-url').value = '';
  $('ek-modal-submit').hidden = false;
  $('ek-modal-submit').disabled = false;
  $('ek-overlay').classList.add('open');
  _ekSetBackgroundInert(true);
  $('ek-label').focus();
}

function closeExtKeyModal() {
  if (_ekModalState === 'issuing') return;   // 応答待ちの間は閉じない
  _ekActiveOp = ++_ekOpSeq;   // 閉じたら、以後に届く遅延結果（timeout回復等）も無効化する
  $('ek-overlay').classList.remove('open');
  _ekSetBackgroundInert(false);
  _ekClearRevealedKey();
  // 開く前にフォーカスがあった要素へ復帰する。
  if (_ekOpenerEl && typeof _ekOpenerEl.focus === 'function') _ekOpenerEl.focus();
  _ekOpenerEl = null;
}

// POST の結果が不明なときの回復導線。POST /ext/v1/admin/keys/recover へ client_op_id を渡し、サーバー側で該当キーを単一の原子的操作で照合・失効する。
// 有界に再試行する（3回×2秒間隔）。found: true を確認できた場合のみ「失効しました」と表示する。
async function _ekRecoverFromAmbiguousIssue(myOp, clientOpId) {
  if (_ekActiveOp !== myOp) return;
  $('ek-issue-err').textContent = '発行が完了したか確認しています…';
  const attempts = 3;
  const gapMs = 2000;
  let outcome = 'not_found';
  for (let i = 0; i < attempts; i++) {
    if (_ekActiveOp !== myOp) return;
    try {
      const res = await api('POST', '/ext/v1/admin/keys/recover',
        { client_op_id: clientOpId }, { timeoutMs: 10000 });
      if (res && res.found === true) { outcome = 'revoked'; break; }
      else if (res && res.found === false) { outcome = 'not_found'; }
      else { outcome = 'error'; }   // found が true/false のどちらでもない不正な応答＝再試行対象
    } catch (e) {
      outcome = 'error';
    }
    if (i < attempts - 1) await _ekSleep(gapMs);
  }
  if (_ekActiveOp !== myOp) return;
  if (outcome === 'revoked') {
    $('ek-issue-err').textContent = '発行は完了していましたが、キーを表示できなかったため'
      + '失効しました。もう一度発行してください。';
    loadExtKeys();
  } else if (outcome === 'error') {
    $('ek-issue-err').textContent = '発行が完了したかどうか確認できませんでした。'
      + '一覧を確認するか、しばらくしてからもう一度お試しください。';
  } else {
    $('ek-issue-err').textContent = '発行に失敗した可能性があります。もう一度お試しください。';
  }
  if (_ekActiveOp !== myOp) return;
  _ekModalState = 'idle';
  $('ek-modal-submit').disabled = false;
}

async function submitExtKeyIssue() {
  if (_ekModalState === 'issuing') return;   // 応答待ち中の再入は拒否（submit 側の入口でも防ぐ）
  const label = ($('ek-label').value || '').trim();
  $('ek-issue-err').textContent = '';
  if (!label) { $('ek-issue-err').textContent = 'ラベルを入力してください'; return; }
  const worldsRaw = ($('ek-worlds').value || '').split('\n').map((s) => s.trim()).filter(Boolean);
  const body = { label };
  if (worldsRaw.length) body.allowed_worlds = Array.from(new Set(worldsRaw));
  const expiresRaw = ($('ek-expires').value || '').trim();
  // min 属性は手入力・貼り付けを防げないため、送信前にも文字列比較（YYYY-MM-DD は辞書順=時系列順）で過去日を弾く。
  if (expiresRaw && expiresRaw < _todayLocalDateStr()) {
    $('ek-issue-err').textContent = '有効期限は今日以降の日付を指定してください';
    return;
  }
  if (expiresRaw) {
    // 選択日を包含＝翌日 00:00（ローカル）に失効させる（「この日まで有効」の実装）。
    const d = new Date(`${expiresRaw}T00:00:00`);
    d.setDate(d.getDate() + 1);
    body.expires_at = d.toISOString();
  }
  const quotaRaw = ($('ek-quota').value || '').trim();
  if (quotaRaw) body.daily_quota = Number(quotaRaw);
  const webhookUrlRaw = ($('ek-webhook-url').value || '').trim();
  if (webhookUrlRaw) body.webhook_url = webhookUrlRaw;
  const clientOpId = _genOpId();
  body.client_op_id = clientOpId;

  const myOp = ++_ekOpSeq;
  _ekActiveOp = myOp;
  _ekModalState = 'issuing';
  $('ek-modal-submit').disabled = true;
  let d;
  try {
    d = await api('POST', '/ext/v1/admin/keys', body, { timeoutMs: 30000 });
  } catch (e) {
    if (_ekActiveOp !== myOp) return;   // このモーダルは既に閉じられた/次の操作が始まっている
    if (e.ambiguous) {
      await _ekRecoverFromAmbiguousIssue(myOp, clientOpId);
    } else {
      _ekModalState = 'idle';
      $('ek-issue-err').textContent = e.message;
      $('ek-modal-submit').disabled = false;
    }
    return;
  }
  if (_ekActiveOp !== myOp) return;   // 応答が届く前に閉じられた等＝この結果はもう表示しない
  if (!d || typeof d.key !== 'string' || !d.key) {
    // 2xx だが期待する形でない＝書込みの成否が不明。
    await _ekRecoverFromAmbiguousIssue(myOp, clientOpId);
    return;
  }
  $('ek-issue-form').hidden = true;
  $('ek-reveal').hidden = false;
  $('ek-reveal-key').textContent = d.key;
  // webhook_url を指定して発行した場合のみ、secret も同じレスポンスに1度だけ含まれる。
  if (d.webhook_secret) {
    $('ek-reveal-webhook-secret').textContent = d.webhook_secret;
    $('ek-reveal-webhook').hidden = false;
  }
  $('ek-modal-submit').hidden = true;
  _ekModalState = 'revealed';   // ここで初めて閉鎖可能に戻す（見せる前に閉じられない）
  $('ek-copy').focus();   // 表示直後にコピー操作へ導く（フォーム欄にフォーカスが残らない）
  loadExtKeys();   // 発行のライフサイクルから切り離す（await しない・一覧取得の遅延/失敗と無関係）
}

const _ekIssueOpen = $('ext-key-issue-open');
if (_ekIssueOpen) _ekIssueOpen.addEventListener('click', openExtKeyModal);
const _ekOverlay = $('ek-overlay');
if (_ekOverlay) {
  _ekOverlay.addEventListener('click', (e) => { if (e.target === _ekOverlay) closeExtKeyModal(); });
  $('ek-modal-close').addEventListener('click', closeExtKeyModal);
  $('ek-modal-cancel').addEventListener('click', closeExtKeyModal);
  $('ek-modal-submit').addEventListener('click', submitExtKeyIssue);
}
// クリップボードへコピー（navigator.clipboard 不可なら execCommand('copy')）。キー本体・Webhook secret の「今だけ表示」欄で共通。
async function _ekCopyTextTo(text, resEl) {
  let ok = false;
  try {
    await navigator.clipboard.writeText(text);
    ok = true;
  } catch (e) {
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta); ta.select();
    try { ok = document.execCommand('copy'); } catch (_e) { ok = false; }
    ta.remove();
  }
  if (!resEl) return;
  if (ok) { resEl.className = 'tres ok'; resEl.textContent = '✓ コピーしました'; }
  else { resEl.className = 'tres danger'; resEl.textContent = '✗ コピーできませんでした（選択してコピーしてください）'; }
}
const _ekCopy = $('ek-copy');
if (_ekCopy) _ekCopy.addEventListener('click', () => {
  _ekCopyTextTo($('ek-reveal-key').textContent || '', $('ek-copy-res'));
});
const _ekCopyWebhookSecret = $('ek-copy-webhook-secret');
if (_ekCopyWebhookSecret) _ekCopyWebhookSecret.addEventListener('click', () => {
  _ekCopyTextTo($('ek-reveal-webhook-secret').textContent || '', $('ek-copy-webhook-secret-res'));
});
// #ext-keys-list は loadExtKeys() が丸ごと innerHTML を入れ替えるため、常に存在するコンテナへの委譲リスナー1本にする。
const _extKeysList = $('ext-keys-list');
if (_extKeysList) _extKeysList.addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-ek-revoke]');
  if (!btn) return;
  const id = btn.dataset.ekRevoke;
  const row = _extKeys.find((r) => String(r.id) === String(id));
  if (!window.confirm(`このキー「${row ? row.label : id}」を失効しますか？`
    + '以後このキーでの呼び出しはできなくなります（元に戻せません）。')) return;
  btn.disabled = true;
  try {
    await api('DELETE', `/ext/v1/admin/keys/${id}`);
    await loadExtKeys();
  } catch (err) {
    window.alert('失効に失敗しました: ' + err.message);
    btn.disabled = false;
  }
});

// ===== タブ単位の描画 =====
// 各タブの render はそのタブのフィールドの baseline も更新する。タブ単位リセットは対応する関数だけを呼び、他タブには触れない。

function renderProviderTab(view) {
  const cloud = view.cloud || {};
  renderCloud(cloud);
  renderOllamaAllowlist(view.ollama_allowlist);
  renderWebhookAllowlist(view.webhook_allowlist);
  renderOpenaiEndpoint(view.openai_endpoint);   // _mcState（使えるモデル）を先に更新してから読む
  renderEmbedProvider(view.embed_provider);
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
  renderChatMaxTurns(view.chat_max_turns);
  renderWorkspace(view.workspace);
  renderChatExamples(view.chat_examples);
}

// 「調査・回答」タブは接続設定と独立して描画・リセットする。
function renderResearchTab(view) {
  renderAgenticBudget(view.agentic_budget);   // ツール結果バイト予算
  renderCodexMode(view.codex_mode);
  renderDepthProfile(view.depth_profile);
  const rounds = view.max_review_rounds;
  _maxReviewRoundsBaseline = rounds.configured == null ? '' : String(rounds.configured);
  $('max-review-rounds').value = _maxReviewRoundsBaseline;
  $('max-review-rounds-hint').textContent = `現在の適用値: ${rounds.effective} 回。既定: ${rounds.default} 回。`
    + (rounds.configured == null ? '未設定です。' : 'この値で固定中です。');
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
  const parallel = view.embed_parallel;
  _embedParallelBaseline = parallel.configured == null ? '' : String(parallel.configured);
  $('embed-parallel').value = _embedParallelBaseline;
  $('embed-parallel-hint').textContent = parallel.configured == null
    ? `未設定です（既定 ${parallel.effective} 件が適用されます）。`
    : `この値で固定中です（既定: ${parallel.default} 件）。`;
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

function renderEmbedProvider(ep) {
  ep = ep || {};
  _embedProviderBaseline = ep.configured || '';
  $('embed-provider').value = _embedProviderBaseline;
  $('embed-provider-hint').textContent = ep.effective === 'ollama'
    ? `ローカル（Ollama）で埋め込みます。使うモデル: ${ep.ollama_model}`
    : '回答に使うクラウドと同じ接続先で埋め込みます（既定）。';
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

// 同時実行の上限（chat_max_turns）。renderDepthProfile/collectDepthProfile/depthProfileChanged と同じ流儀。「プロバイダ＋接続先」タブの追加カード。
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

// チャット画面のクイック入力例（chat_examples）。「プロバイダ＋接続先」タブの3つ目のカード。
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

// 調べる深さの基準値と同じ流儀の別カード（取り込みタブ）。表示/入力欄は KB 単位、GET/PUT（bytes）とは境界でだけ変換する。
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

function renderModelsTab(view) {
  renderModelCatalog(view.model_catalog, (view.cloud || {}).provider);
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
  renderRequiredTools(view.required_tools);
  renderArms(view.arms || {});
  renderArmsStatus(view.arms || {});
  renderLegacy(view.legacy_backend);
  renderLegacyStatus(view.legacy_backend);
  renderVlm(view.vlm);
  renderVlmStatus(view.vlm);
  renderRagLlmRender(view.rag_llm_render);
  renderRagLlmRenderStatus(view.rag_llm_render);
  _armsBaseline = [...((view.arms || {}).enabled || [])].sort();
  _legacyBaseline = _legacySelectedValue(view.legacy_backend);
  const vlm = view.vlm;
  _vlmBaseline = (vlm && vlm.effective) ? {
    provider: vlm.effective.provider === 'openai' ? 'openai' : 'ollama',
    model: vlm.effective.model || '', cloud_allowed: !!vlm.effective.cloud_allowed,
  } : null;
  _ragLlmRenderBaseline = !!(view.rag_llm_render && view.rag_llm_render.effective);
}

// 外部連携（API キー）タブの描画（利用量タブとは baseline を分けて持つ）。
function renderExtKeysTab(view) {
  renderExtKeysToggle(view.ext_keys);
  const extKeys = view.ext_keys || {};
  _extKeysAllowedBaseline = !!extKeys.user_api_keys_allowed;
  const quota = extKeys.daily_quota_default || {};
  _extKeysQuotaBaseline = quota.configured != null ? String(quota.configured) : '';
  const rdp = extKeys.research_default_provider || {};
  // 保存値が破損している間は、その破損状態自体を基準値にする（'ollama' を基準にすると未保存の変更なしに見えて破損に気付けない）。
  _extKeysResearchProviderBaseline = (rdp.effective === 'openai' || rdp.effective === 'ollama')
    ? rdp.effective : _RESEARCH_PROVIDER_INVALID;
}

function render(view) {
  if (!view.embed_parallel) {
    throw new Error('設定項目が不足しています。サーバーを更新・再起動してから、画面を再読み込みしてください。');
  }
  _view = view;
  renderModelsTab(view);   // _mcState を先に更新（renderProviderTab の埋め込み欄表示が読むため）
  renderProviderTab(view);
  renderResearchTab(view);
  renderIngestTab(view);
  renderExtKeysTab(view);
  applyConfigChangedHighlights(view);
  refreshTabDots();
}

async function load() {
  try {
    render(await getJSON('/admin/settings'));
  } catch (e) {
    $('msg').innerHTML = '<span class="danger">設定を読み込めませんでした</span>';
    // 初期描画が止まった場合も、各タブに失敗を表示する。未読込の値は保存させない。
    const message = '設定を読み込めませんでした。サーバーの更新・稼働状態を確認して、画面を再読み込みしてください。';
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
  collectCloud(body);
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
  collectChatMaxTurns(body);   // 同時実行の上限（変わった項目だけ送る）
  collectWorkspace(body);   // 個人ファイルの上限・保持日数（変わった項目だけ送る）
  if (chatExamplesChanged()) body.chat_examples = collectChatExamples();   // チャットの質問例
  collectAgenticBudget(body);   // ツール結果1件あたりの予算（変わった項目だけ送る）
  try {
    const view = await api('PUT', '/admin/settings', body);
    render(view);
    await loadExtKeys();   // OFF 保存で一括失効された可能性があるため一覧も再取得する
    $('msg').innerHTML = '<span class="ok">✓ 保存しました</span>';
  } catch (e) {
    $('msg').innerHTML = `<span class="danger">${esc(e.message)}</span>`;
  } finally {
    $('save').disabled = false;
  }
}

// ===== タブ単位「既定に戻す」=====
// 秘密（API キー）は対象外（キー欄を個別にクリアしてもらう）。真偽値トグル（personal_api_keys_allowed／user_api_keys_allowed）は実効既定と同値の明示 false を送る（一括削除・失効は値が厳密に false のときだけ発火する）。
async function _putResetBody(body, resEl) {
  // 5タブ共通のリセット送信口。削除待ちの応答を無効化する（保存済みのクラウドキー削除待ちが巻き戻し得るため）。
  _invalidateCloudKeyClear();
  const el = $(resEl);
  el.className = 'tres muted';
  el.textContent = '既定に戻しています...';
  try {
    return await api('PUT', '/admin/settings', body);
  } catch (e) {
    el.className = 'tres danger';
    el.textContent = '✗ ' + e.message;
    throw e;
  }
}
function _markResetOk(resEl) {
  const el = $(resEl);
  el.className = 'tres ok';
  el.textContent = '✓ 既定に戻しました';
}

// プロバイダタブから「埋め込みのデプロイ名」だけを既定へ戻すための model_catalog body を作る。
// _mcConfiguredRaw（保存済みの生値）から openai.embed キーだけを取り除く（他タブの未保存編集は含めない）。
function _configuredRawWithoutOpenaiEmbed() {
  if (!_mcConfiguredRaw) return null;
  const clone = JSON.parse(JSON.stringify(_mcConfiguredRaw));
  if (clone.openai) {
    delete clone.openai.embed;
    if (Object.keys(clone.openai).length === 0) delete clone.openai;
  }
  return Object.keys(clone).length ? clone : null;
}

async function resetProviderTab() {
  if (_view && _view.cloud && _view.cloud.personal_api_keys_allowed) {
    const n = _view.cloud.personal_keys_in_use_count || 0;
    if (n > 0 && !window.confirm(
      `既定に戻すと個人キーの許可がオフになり、現在 ${n} 人の利用者の個人キーが削除されます。続けますか？`)) {
      return;
    }
  }
  const resEl = 'tab-reset-res-provider';
  const body = {
    cloud_provider: null,
    personal_api_keys_allowed: false,
    web_search_allowed: false,
    ollama_url: null,
    ollama_allowlist: null,
    webhook_allowlist: null,
    openai_endpoint_kind: null,
    openai_base_url: null,
    openai_auth_header: null,
    openai_api_version: null,
    embed_provider: null,
    // 埋め込みのデプロイ名（model_catalog.openai.embed）だけ組み込み既定へ戻す。
    model_catalog: _configuredRawWithoutOpenaiEmbed(),
    chat_max_turns_per_user: null,
    chat_max_turns_global: null,
    workspace_max_bytes: null,
    workspace_ttl_days: null,
    chat_examples: null,
  };
  let view;
  try { view = await _putResetBody(body, resEl); } catch (e) { return; }
  _view = view;
  // 埋め込みセルだけ最新の実効値へ同期する。列プロバイダは応答の cloud_provider へ追従させる。
  const mc = view.model_catalog || {};
  const eff = mc.effective || {};
  const embedEff = (eff.openai || {}).embed || (_mcBuiltin.openai || {}).embed || { allowed: [], default: '' };
  _mcState.openai = _mcState.openai || {};
  _mcState.openai.embed = JSON.parse(JSON.stringify(embedEff));
  _mcBaseline.openai = _mcBaseline.openai || {};
  _mcBaseline.openai.embed = JSON.parse(JSON.stringify(embedEff));
  _mcConfiguredRaw = mc.configured ? JSON.parse(JSON.stringify(mc.configured)) : null;
  _mcCloudProvider = (view.cloud || {}).provider || 'openai';
  renderProviderTab(view);
  renderModelCatalogTable();   // 表の埋め込みセル・列プロバイダの見た目も追従させる
  applyConfigChangedHighlights(view);
  refreshTabDots();
  _markResetOk(resEl);
}

async function resetResearchTab() {
  const resEl = 'tab-reset-res-research';
  const body = Object.fromEntries(_DEPTH_BASE_FIELDS.map(({ put }) => [put, null]));
  body.depth_base_codex_reasoning = null;
  body.embed_parallel = null;
  body.max_review_rounds = null;
  body.codex_worker_model = null;
  body.codex_session_retention_days = null;
  body.agentic_budget_per_result = null;
  body.codex_mode = null;
  let view;
  try { view = await _putResetBody(body, resEl); } catch (e) { return; }
  _view = view;
  renderResearchTab(view);
  applyConfigChangedHighlights(view);
  refreshTabDots();
  _markResetOk(resEl);
}

async function resetModelsTab() {
  const resEl = 'tab-reset-res-models';
  // 対象キーのみ・null で送る。model_catalog を丸ごと既定へ戻すため、プロバイダタブの埋め込みデプロイ名も一緒に戻る。
  const body = { model_catalog: null };
  let view;
  try { view = await _putResetBody(body, resEl); } catch (e) { return; }
  _view = view;
  renderModelsTab(view);
  // 列プロバイダは「プロバイダタブの現在の未保存選択」に合わせる（保存済み値へ戻さない）。
  _mcCloudProvider = selectedCloudProvider();
  renderModelCatalogTable();
  syncEmbedDeploymentField();   // プロバイダ＋接続先タブ側の表示も新しい実効値へ追従させる
  applyConfigChangedHighlights(view);
  refreshTabDots();
  _markResetOk(resEl);
}

async function resetIngestTab() {
  const resEl = 'tab-reset-res-ingest';
  const body = {
    arms_enabled: null, legacy_backend: null, vlm: null, rag_llm_render: null,
  };
  let view;
  try { view = await _putResetBody(body, resEl); } catch (e) { return; }
  _view = view;
  renderIngestTab(view);
  applyConfigChangedHighlights(view);
  refreshTabDots();
  _markResetOk(resEl);
}

// 外部連携（API キー）タブの既定リセット。対象は user_api_keys_allowed（明示 false）と quota のみ。
async function resetExtKeysTab() {
  if (_view && _view.ext_keys && _view.ext_keys.user_api_keys_allowed) {
    const n = _view.ext_keys.self_issued_active_count || 0;
    if (n > 0 && !window.confirm(
      `既定に戻すと利用者のキー発行が許可されなくなり、現在有効な利用者発行キー ${n} 件が`
      + '失効します。続けますか？')) {
      return;
    }
  }
  const resEl = 'tab-reset-res-extkeys';
  const body = {
    user_api_keys_allowed: false,
    user_api_keys_daily_quota_default: null,
    research_default_provider: null,
  };
  let view;
  try { view = await _putResetBody(body, resEl); } catch (e) { return; }
  _view = view;
  renderExtKeysTab(view);
  await loadExtKeys();   // OFF リセットで一括失効された可能性があるため一覧も再取得する
  applyConfigChangedHighlights(view);
  refreshTabDots();
  _markResetOk(resEl);
}

const _TAB_RESET_HANDLERS = {
  provider: resetProviderTab, research: resetResearchTab, models: resetModelsTab, ingest: resetIngestTab,
  extkeys: resetExtKeysTab,
};
document.querySelectorAll('[data-reset-tab]').forEach((b) => {
  const handler = _TAB_RESET_HANDLERS[b.dataset.resetTab];
  if (handler) b.addEventListener('click', handler);
});

// チャットの質問例カードだけの「未設定に戻す」（_putResetBody を共用）。
const _chatExamplesReset = $('chat-examples-reset');
if (_chatExamplesReset) _chatExamplesReset.addEventListener('click', async () => {
  const resEl = 'chat-examples-reset-res';
  let view;
  try { view = await _putResetBody({ chat_examples: null }, resEl); } catch (e) { return; }
  _view = view;
  renderChatExamples(view.chat_examples);
  applyConfigChangedHighlights(view);
  refreshTabDots();
  _markResetOk(resEl);
});

// ===== タブ切り替え（URL ハッシュで記憶）・未保存タブの丸印 =====
const TAB_KEYS = ['provider', 'research', 'models', 'ingest', 'extkeys'];
// 埋め込みタブ（管理系ページを iframe で表示）。保存対象がないため TAB_DIRTY を持たず、切替に未保存確認は挟まない。
const EMBED_TAB_KEYS = ['users', 'usage-page', 'feedback', 'audit', 'status'];
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
    || openaiEndpointChanged() || mcEmbedChanged() || embedProviderChanged()
    || chatMaxTurnsChanged() || workspaceChanged() || chatExamplesChanged(),
  research: () => depthProfileChanged() || embedParallelChanged() || maxReviewRoundsChanged()
    || codexWorkerModelChanged() || codexSessionRetentionDaysChanged() || agenticBudgetChanged() || codexModeChanged(),
  models: () => mcCatalogChangedExcludingEmbed(),
  ingest: () => armsChanged() || legacyChanged() || vlmChanged() || ragLlmRenderChanged(),
  extkeys: () => extKeysAllowedChanged() || extKeysQuotaChanged() || extKeysResearchProviderChanged(),
};
function refreshTabDots() {
  TAB_KEYS.forEach((k) => {
    const dot = $('tab-dot-' + k);
    if (dot) dot.hidden = !TAB_DIRTY[k]();
  });
  $('unsaved-note').hidden = !document.querySelector('#admin-tabs .tab-dot:not([hidden])');
}

// 接続先関連（種別ラジオ・埋め込みデプロイ名）の変更監視は、対象の id/属性だけで判定する（DOM 構造に依存しない）。埋め込み欄は 'change' で反映する。
// このブロックは下の refreshTabDots 登録より前に置く: どちらも document に直接束縛するため登録順が実行順になり、先に状態を更新してから refreshTabDots で読ませる必要がある。
document.addEventListener('change', (e) => {
  if (e.target.matches('input[data-openai-endpoint-kind]')) updateOpenaiEndpointFieldsVisibility();
  if (e.target.id === 'openai-endpoint-embed-deployment') applyEmbedDeploymentFieldEdit();
});

// 各カードのリスナーは対象要素に束縛されバブリングで document に届くため、document 直接束縛のリスナーより後に走る。
// document を対象にするのは、モーダルが #main-content の外にあり、そこでの操作も拾うため。
['input', 'change', 'click'].forEach((evt) => document.addEventListener(evt, refreshTabDots));

// 既定から変えた項目だけ強調する（5タブすべて）。組み込み既定の値そのものと比較する。使えるモデルの表はセル単位で mcCellChanged() が強調する。
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
  mark($('rag-llm-render-card'), !!ragRender.effective !== !!ragRender.default);
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

const _openaiEndpointTest = $('openai-endpoint-test');
if (_openaiEndpointTest) _openaiEndpointTest.addEventListener('click', testOpenaiEndpoint);

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
// VLM 設定の provider 変更時はキー未設定案内だけ即時更新する（入力中の値を消さない）。
function updateVlmKeyHint() {
  const keyMiss = $('vlm-key-missing');
  const provSel = $('vlm-provider');
  if (!keyMiss || !provSel) return;
  const keyPresent = !!(_view && _view.vlm && _view.vlm.openai_key_present);
  const needKey = provSel.value === 'openai' && !keyPresent;
  if (needKey) {
    keyMiss.hidden = false;
    keyMiss.textContent = 'クラウド（OpenAI）を選んでいますが、OpenAI の API キー（OPENAI_API_KEY）が設定されていません。'
      + 'キーを設定するまで視覚読み取りは行われません。';
  } else { keyMiss.hidden = true; keyMiss.textContent = ''; }
}
const _vlmBlock = $('vlm-block');
if (_vlmBlock) _vlmBlock.addEventListener('change', (e) => {
  if (e.target.matches('#vlm-provider, #vlm-model, #vlm-cloud-allowed')) updateVlmKeyHint();
});
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
function applyThemeIcon() { const b = $('themebtn'); if (b) b.textContent = document.documentElement.dataset.theme === 'dark' ? '☀️' : '🌙'; }
function syncFrameTheme(frame) {
  frame.contentDocument.documentElement.dataset.theme = document.documentElement.dataset.theme;
}
document.querySelectorAll('.embed-frame').forEach((frame) => {
  frame.addEventListener('load', () => syncFrameTheme(frame));
});
document.addEventListener('click', (e) => {
  if (!e.target.closest('#themebtn')) return;
  const d = document.documentElement, next = d.dataset.theme === 'dark' ? 'light' : 'dark';
  d.dataset.theme = next; localStorage.setItem('sherpa-theme', next); applyThemeIcon();
  document.querySelectorAll('.embed-frame[src]').forEach(syncFrameTheme);
});
applyThemeIcon();

// ===== 初期化（admin ガード）=====
(async () => {
  const isAdmin = await checkAdmin();
  if (!isAdmin) {
    const main = $('main-content'), denied = $('access-denied'), bar = $('save-bar');
    if (main) main.style.display = 'none';
    if (denied) denied.style.display = 'block';
    if (bar) bar.style.display = 'none';   // 非 admin には保存バーも出さない
    return;
  }
  activateTab(location.hash.replace('#', ''), { updateHash: false });
  load();
  loadExtKeys();   // 独立取得（GET /ext/v1/admin/keys は /admin/settings と別エンドポイント）
})();
