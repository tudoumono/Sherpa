// システム管理（全体設定・admin 専用）。GET/PUT /admin/settings。
// docs/proposals/2026-07-08-設定分離とUI整備.md S1: 取り込みアーム（全ユーザーに効く）。
// （コスト単価/為替の設定は撤去・2026-07-08 フィードバック⑦＝金額表示をやめトークン数のみに）。
// 個人に効く設定（頭脳/モデル/自分のキー/表示）は settings.html（個人設定）で行う。
// 認可の正は API 側の _require_admin。ここでの #access-denied 表示は UX のみ（audit.js と同じ流儀）。
'use strict';
const $ = Sherpa.$, esc = Sherpa.esc, getJSON = Sherpa.getJSON, api = Sherpa.api;

// アーム名 → 平文の説明（専門用語ゼロ・04-画面の原則.md）。未知アームは名前をそのまま出す（fail-safe）。
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

// 未導入アームの導入案内（arms.available が false のとき arm-d に併記・通常は同梱済みのため出ない）。専門用語ゼロ（04-画面の原則.md）。
const ARM_MISSING_HINT = {
  pdf_text: 'この環境では PDF 抽出ライブラリが見つかりません（通常は同梱されています。復旧: pip install pypdf）。',
  vision: '視覚読み取りに使う AI が使えません。下の「視覚読み取りの AI」設定を確認してください（クラウドを選ぶ場合は許可とキーが必要です）。',
};

// 旧形式（.doc/.xls/.ppt）の変換バックエンド（W0）→ 平文の説明（専門用語ゼロ・04-画面の原則.md）。
// Med4（RV 2026-07-08）: label に「（既定）」を固定で書かない（env 既定が libreoffice の環境で
// 「使わない（既定）」と誤表示する）。「（既定）」は renderLegacy が view.legacy_backend.default と
// 一致する選択肢にだけ動的に付ける。
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
    // W2'（2026-07-08）: 同一マシンなら設定不要（WSL 連携で Windows の Office を直接呼ぶ）。
    desc: '同じ Windows に Office があれば、そのまま使えます（設定不要）。別のマシンの Office を使うときだけ接続先を設定します。最も忠実に変換します。',
  },
};

// クラウド AI プロバイダ名 → 平文の説明（専門用語ゼロ）。
const CLOUD_PROVIDER_LABELS = {
  openai: { label: 'OpenAI', desc: 'OpenAI API に直結します（Codex 経由の接続にも使われます）。' },
  gemini: { label: 'Gemini（Google）', desc: 'Google の Gemini API を使います。' },
  bedrock: { label: 'AWS Bedrock (Claude)', desc: 'AWS 経由で Claude を使います。' },
};
const CLOUD_KEY_SET_FIELD = { openai: 'openai_key_set', gemini: 'gemini_key_set', bedrock: 'bedrock_key_set' };

// STAT-2: 利用統計チャット専用の AI 選択（openai/ollama のみ・cloud_provider とは独立）。
const USAGE_CHAT_PROVIDER_LABELS = { openai: 'OpenAI', ollama: 'ローカル（Ollama）' };

// SET-2c: OpenAI 互換 API の接続先（本家／Azure OpenAI／その他 OpenAI 互換）。
const OPENAI_ENDPOINT_KIND_LABELS = {
  openai: { label: 'OpenAI 本家' },
  azure: { label: 'Azure OpenAI' },
  custom: { label: 'その他 OpenAI 互換' },
};

// 使えるモデル（model_catalog）。用途名 → 平文の表示名（個人設定ページの既存の言い回しに合わせる）。
const MC_USAGE_LABELS = {
  chat: 'チャット', intent: '依頼の仕分け',
  embed: '検索の索引づくり', route: '振り分け', subsearch: '下調べ', codex: 'Codex',
  render: '検索用文書の整形',
};
const MC_COLUMN_LABELS = { ollama: 'ローカル（Ollama）', codex: 'Codex' };

let _view = null;       // 直近の GET/PUT 応答（描画・保存の基準）
let _extKeys = [];        // 直近取得した外部連携キー一覧（GET /ext/v1/admin/keys の keys）

// ダーティ判定は「触ったか」ではなく「render() 時点の基準値（baseline）と今の値が異なるか」で行う。
// 値を元に戻せば丸印も PUT 対象からも外れる（触っただけで戻していない項目を誤って送らない）。
// 各タブの render 関数（renderProviderTab/renderModelsTab/renderIngestTab/renderUsageTab）が
// 対応する baseline を書き換える。書込専用の秘密（キー入力）は baseline が常に空文字＝
// 「今の入力が空でなければ変更あり」という自然な判定になる。
let _cloudBaseline = { provider: 'openai', providerRaw: null, personalAllowed: false, webSearchAllowed: false, ollamaUrl: '' };

// `cloud_provider`（A7）だけは値差分判定の例外——`_cloudBaseline.provider` は既定込みの実効値
// （未選択でも常に 'openai'）のため、値差分だけでは「一度も選んでいない（raw なし）」と「明示的に
// openai を選んだ（raw あり）」を区別できない。この区別は Ollama fallback の有無を左右するため
// （FBK-1）、区別できないと初期表示 openai のまま保存しても raw が残らず未選択のままになる。
// `_cloudProviderTouched` はラジオが実際にクリックされたか（`change` でなく `click` を見る＝既に
// 選択中の radio を再クリックしても `change` は発火しないため）だけを追跡する一時フラグ。
// ラジオ群はどの選択肢をクリックしても「今回の明示選択」として扱ってよく（一度 gemini を見てから
// openai へ戻すのも正当な明示選択）、他フィールドの「変更してから元に戻す」問題（touched flag を
// 使わない一般則の理由）に相当する「操作したのに取り消したい」状態が存在しないため、touched を
// 残しても実害がない。render() の度に false へ戻す（`renderProviderTab` 参照）。
let _cloudProviderTouched = false;

// RV2（FBK-1・2026-09-01・境界回帰#4）: raw が「無い」場合だけでなく「不正値のまま残っている」
// 場合も対象にする——不正 raw（例: 旧データ・env 誤記由来）は表示上は既定 openai へ丸められる
// ため、admin が案内どおり openai を選び直しても、丸め後の値と一致するだけで「変更なし」に
// 見えて保存対象から漏れていた（正規化した raw と現在選択が食い違う＝実質的に raw は無いのと
// 同じ「未確定」状態）。
function _normalizedProviderRaw() {
  return (_cloudBaseline.providerRaw || '').trim().toLowerCase() || null;
}

function cloudProviderNeedsExplicitSave(provider) {
  return _cloudProviderTouched && _normalizedProviderRaw() !== provider;
}
let _ollamaAllowlistBaseline = [];
let _webhookAllowlistBaseline = [];   // PART-6: Webhook 宛先の SSRF allowlist（ollama_allowlist と同型）
let _openaiEndpointBaseline = { kind: 'openai', base_url: '', auth_header: 'bearer', api_version: '' };
// チャット画面のクイック入力例（`chat_examples`・sherpa/chat_examples.py）。実際の baseline は
// 初回描画（`renderChatExamples`）が `{enabled, items}`（items は配列）で上書きする——この初期値は
// その描画がまだ走っていない間だけ参照されうるフォールバックなので、実描画後の形（`items` 配列）
// と揃えておく（RV是正・rv-periphery #6：旧 `text: ''` は `chatExamplesChanged()` が参照する
// `.items` と形が食い違い、描画前に呼ばれると誤って「変更あり」と判定しかねなかった）。
let _chatExamplesBaseline = { enabled: true, items: [] };
let _armsBaseline = [];
let _legacyBaseline = null;
let _vlmBaseline = null;
// L5（U1）: rag.md の LLM 成形トグルの保存済み実効値（真偽・PUT では "on"/"off" 文字列で送る）。
let _ragLlmRenderBaseline = true;
let _usageChatProviderBaseline = null;
// 応答形状が不正（`usage_chat` 欠落・providers/effective の型不一致等）なら、カードを隠したり
// 'openai' へ黙って補完したりせず明示エラーを出す（`renderUsageChatAi` 参照）。
// この値は「今、保存可能な有効なデータが描画されているか」。
let _usageChatAiAvailable = false;
// 保存値（`usage_chat.configured`）が選択肢に無い（旧データ・DB直接編集等）間、まだ選び直して
// いないことを表す専用の値（select 版の `_RESEARCH_PROVIDER_INVALID` と同型）。この値は
// baseline とラジオの選択中の値の**両方**に使うが、実際に PUT で送る値としては使わない
// （`save()` のガード参照）——baseline を「未選択＝null」のままにすると、他の項目だけを
// 変えた保存でも選択中の値（同じく null）との比較で「選択が変わった」と誤認し、
// `usage_chat_provider: null` を送って不正値を暗黙に解除してしまう。baseline も選択中の値も
// 同じセンチネルに揃えておけば、実際に openai/ollama のどちらかを選ぶまで「未変更」のまま
// 保てる（`renderUsageChatAi`/`usageChatProviderChanged` 参照）。
const _USAGE_CHAT_PROVIDER_INVALID = '__invalid__';
// 「頭脳の選択に合わせる（既定）」選択肢の DOM 上の値（`data-usage-chat-provider=""`）。
// `configured`（生の保存値・null＝未設定）と 1:1 対応させる——選択/dirty 判定・PUT で送る値の
// 生成は全てこの値と `configured` の対応（`''` ⇔ `null`）を介して行う。ラジオの3択
// （頭脳の選択に合わせる／OpenAI に固定／ローカル(Ollama) に固定）は、`effective`（A7 連動で
// 解決された「いま実際に使われる値」・毎回変わりうる）ではなく、常にこの `configured` を
// 基準に選択状態を決める——`effective` を基準にすると、「未設定なので今はたまたま openai」と
// 「明示的に openai へ固定」が画面上で区別できず、admin が「OpenAI が選ばれている」ように
// 見える状態のまま A7 を変更して保存すると、実は未固定だったため usage_chat_provider が
// PUT から省略され、A7 の新しい解決結果へ黙って反転してしまう。
const _USAGE_CHAT_FOLLOW_VALUE = '';
// 直近の `renderUsageChatAi` 描画で保存値が不正だったか（`renderUsageTab` が baseline を
// `_USAGE_CHAT_PROVIDER_INVALID` に揃えるために参照する）。
let _usageChatSavedInvalid = false;
let _extKeysAllowedBaseline = false;
let _extKeysQuotaBaseline = '';
let _extKeysResearchProviderBaseline = 'ollama';
// 保存値が ollama/openai のどちらでもない（例: system_extras.py が返す "(不正な保存値)"）
// ときに select へ挿入する専用オプションの値。実在の provider コードと衝突しない固定文字列
// （このままでは保存を送らない＝下の save() のダーティ判定が dirty=false を保つ限り送信されない）。
const _RESEARCH_PROVIDER_INVALID = '__invalid__';

// SC-6c（調べる深さの基準値・§3.2）: 整数6項目の入力欄 id と PUT/GET のキー名対応
// （GET 応答は `view.depth_profile.<view>`・PUT は `body.<put>`）。
const _DEPTH_BASE_FIELDS = [
  { view: 'max_turns', put: 'depth_base_max_turns', id: 'depth-base-max-turns', label: '探索の反復回数' },
  { view: 'grep_max_hits', put: 'depth_base_grep_max_hits', id: 'depth-base-grep-max-hits',
    label: '資料検索のヒット件数上限' },
  { view: 'qa_max_hits', put: 'depth_base_qa_max_hits', id: 'depth-base-qa-max-hits',
    label: '内容の質問でのヒット件数上限' },
  { view: 'read_window', put: 'depth_base_read_window', id: 'depth-base-read-window',
    label: '1回に読み取る前後の行数' },
  { view: 'impact_depth', put: 'depth_base_impact_depth', id: 'depth-base-impact-depth',
    label: '影響を調べる深さ' },
  { view: 'troubleshoot_depth', put: 'depth_base_troubleshoot_depth', id: 'depth-base-troubleshoot-depth',
    label: '原因を調べる近傍の深さ' },
];
let _depthProfileBaseline = {};    // put キー -> 文字列化した configured（''=未設定）
// 他の6項目と同じく configured を基準にする（''=未設定＝「環境設定の既定に従う」の空選択肢）。
let _depthReasoningBaseline = '';

// BUDGET-1（2026-09-02-RAG表現の全形式展開と文脈保持.md §3.4）: agentic search の tool-result
// バイト予算。入力欄は人に読みやすい KB 単位（保存/GET は bytes・1KB=1024 換算）——`loBytes`/
// `hiBytes` はサーバ側の Field(ge,le)（`sherpa/routers/system_extras.py::SystemSettingsReq`）と
// 同じ範囲（HTML の min/max もこの換算値）。
const _AGENTIC_BUDGET_FIELDS = [
  { view: 'per_result', put: 'agentic_budget_per_result', id: 'agentic-budget-per-result',
    label: '検索結果1件あたりの上限', loBytes: 1024, hiBytes: 8 * 1024 * 1024 },
  { view: 'total', put: 'agentic_budget_total', id: 'agentic-budget-total',
    label: '1回の検索全体の上限', loBytes: 4096, hiBytes: 64 * 1024 * 1024 },
];
let _agenticBudgetBaseline = {};   // put キー -> 文字列化した configured（KB・''=未設定）

let _mcState = {};       // 編集中の model_catalog（provider -> usage -> {allowed,default}）
let _mcBaseline = {};    // render() 時点の model_catalog（差分判定の基準）
let _mcBuiltin = {};     // 組み込み既定のみ（管理者設定を一切重ねない・差分強調の基準）
// 管理者が実際に保存した生値（`model_catalog.configured`・未設定なら null）。「使えるモデル」タブの
// 未保存編集（_mcState）とは独立に保つ＝プロバイダタブから埋め込みセル1つだけをリセットする時、
// 現在保存済みの他セルの構成を壊さず・かつ他タブの未保存編集を巻き込まずに部分更新するための土台。
let _mcConfiguredRaw = null;
// このセッションで実際にユーザーが編集した「provider/usage」キーの集合（render() 時点でクリア）。
// 保存時（buildModelCatalogBody）は、この集合に無いセルは _mcConfiguredRaw の値をそのまま維持する
// （組み込み既定と同値のセルを過去に明示保存していた場合でも、別セルの編集・保存だけでは
// 消えない＝固定意図と provenance を保つ）。
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
    // 未導入アーム（available===false）はチェックしても実際には変換できない＝選べない（disabled）＋導入案内
    // （soffice 未検出時に LibreOffice を disabled にする renderLegacy と同型・fail-safe）。
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

// Med2: 「既定に従っています」／「この一覧で固定中」の平文ヒント（専門用語ゼロ・04-画面の原則.md）。
function renderArmsStatus(arms) {
  const el = $('arms-status');
  if (!el) return;
  el.textContent = (arms && arms.configured != null)
    ? 'この一覧で固定中です（既定（標準）の変更にはこの先も追従しません）。'
    : '既定（標準）に従っています（変更して保存すると、この一覧の内容で固定されます）。';
}

// 旧形式変換バックエンド（W0）のラジオ描画。応答に legacy_backend が無ければブロックごと隠す（前方互換）。
// 旧形式変換の選択状態は「configured があれば configured、無ければ effective」で決める
// （未設定 null と明示的な選択を区別する。arms のチェック状態＝enabled と同じ「実効値」だが、
// legacy は none も有効な明示選択なので configured を優先して見せる方が意図に忠実）。
// 選択肢が無い（画面に描画されない）応答では null（= collectLegacy() が返す「未選択」と揃える）。
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
  // W2'（2026-07-08）: Office 連携は「到達可（http ワーカー or direct の Office 検出）」または
  // 「同一マシンで direct 検出済み（powershell 連携あり＝Office が未導入でも選択自体は許可）」なら選べる。
  const oc = lb.office_com || {};
  const ocOk = !!(oc.available || oc.mode === 'direct');
  $('legacy-radios').innerHTML = lb.options.map((name) => {
    const meta = LEGACY_LABELS[name] || { label: name, desc: '' };
    const checked = name === selected ? ' checked' : '';
    // 変換手段が無い選択肢は選べない（disabled）＝fail-safe（選んでも変換不可）。
    // LibreOffice は soffice 未検出時・Office 連携は到達不可（http 不達 かつ direct 未検出）時。
    const disabled = ((name === 'libreoffice' && !loOk) || (name === 'office_com' && !ocOk)) ? ' disabled' : '';
    let ver = '';
    if (name === 'libreoffice' && loOk && lb.libreoffice.version) {
      ver = ` <code>${esc(lb.libreoffice.version)}</code>`;
    } else if (name === 'office_com' && ocOk) {
      const vs = ocVersionSummary(oc.versions);
      if (vs) ver = ` <code>${esc(vs)}</code>`;
      // 動作形態を平文で添える（同一マシン直接 or 別ホストのワーカー・専門用語ゼロ・04-画面の原則.md）。
      const modeNote = oc.mode === 'direct' ? '（このパソコンの Office を直接使用）'
        : (oc.mode === 'http' ? '（別のマシンのワーカー経由）' : '');
      if (modeNote) ver += ` <span class="muted">${esc(modeNote)}</span>`;
    }
    // 「（既定）」マーカーは固定文言でなく、実際の既定（env 既定）と一致する選択肢にだけ動的に付ける。
    const label = meta.label + (name === defaultName ? '（既定）' : '');
    return `<label class="armrow">`
      + `<input type="radio" name="legacy-backend" data-legacy="${esc(name)}"${checked}${disabled}>`
      + `<span><span class="arm-t">${esc(label)}</span>${ver}`
      + (meta.desc ? `<div class="arm-d">${esc(meta.desc)}</div>` : '')
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
  // Office 連携（office_com）が使えない場合の案内（3形態を区別・W2'）。
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

// Med4: 「既定に従っています（今の既定: ...）」／「この選択で固定中」の平文ヒント（arms-status と同型）。
function renderLegacyStatus(lb) {
  const el = $('legacy-status');
  if (!el || !lb) return;
  const defaultName = lb.default || 'none';
  const defaultLabel = (LEGACY_LABELS[defaultName] || { label: defaultName }).label;
  el.textContent = (lb.configured !== undefined && lb.configured !== null)
    ? 'この選択で固定中です（既定の変更にはこの先も追従しません）。'
    : `既定に従っています（今の既定: ${defaultLabel}）。`;
}

// ⑤: 視覚読み取りの VLM 設定を描画（応答に vlm が無ければブロックごと隠す＝前方互換）。
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

// L5（U1）: rag.md の LLM 成形トグル。renderVlm と同様、
// キー不在（旧 API 応答との前方互換）ならカードごと隠す。
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

// STAT-2: 利用統計チャット専用の AI 選択。3択（頭脳の選択に合わせる／OpenAI に固定／
// ローカル(Ollama) に固定）——選択状態は常に `configured`（生の保存値）を基準にする
// （`_USAGE_CHAT_FOLLOW_VALUE` 参照・`effective` を基準にしない理由も同所参照）。
// 実サーバは常にこのキーを持つ（`AdminSettingsView` の drift ガードで保証）ため、欠落/形状
// 不正は「古い/壊れた応答」の明示的な合図であり、隠したり値を捏造したりせず画面上にそのまま
// 伝える（黙って openai 扱いにすると、実際には保存できない状態のまま保存操作を許してしまう）。
function renderUsageChatAi(uc, cloud) {
  const card = $('usage-chat-ai-card');
  if (!card) return;
  card.hidden = false;
  const wrap = $('usage-chat-ai-radios');
  if (!wrap) return;
  // `configured` は `null`（未設定＝実行構成に合わせる）も正当な値のため、
  // `uc.configured` が falsy/undefined かどうかではなくキー自体の有無で判定する——
  // `hasOwnProperty` を使わないと、キーが丸ごと欠落した応答（`undefined`）と
  // 明示的な `null`（未設定）を区別できず、欠落を「未設定」と黙って同一視してしまう。
  const shapeValid = !!uc && Array.isArray(uc.providers) && typeof uc.effective === 'string'
    && Object.prototype.hasOwnProperty.call(uc, 'configured');
  _usageChatAiAvailable = shapeValid;
  if (!shapeValid) {
    wrap.innerHTML = '<div class="hint danger">利用統計チャットに使う AI の設定を読み込めませんでした'
      + '（応答の形式が不正です。再読み込みしてください）。</div>';
    return;
  }
  // 保存値（configured）が `null`（未設定＝実行構成に合わせる）でも、選択肢
  // （openai/ollama＝明示固定）でもない場合は不正（"(不正な保存値)" 等・旧データ/手動編集）。
  // ラジオを固定表示せず（どれにもチェックを付けない）明示の注意文を出す（黙って既定へ丸めて
  // 正常な選択のように見せない）。選び直して保存すれば直せるため、保存対象
  // （`_usageChatAiAvailable`）からは外さない。
  const configured = uc.configured;
  const invalidSaved = configured != null && !uc.providers.includes(configured);
  _usageChatSavedInvalid = invalidSaved;
  const warningHtml = invalidSaved
    ? '<div class="hint danger">保存されている値が不正です。下から選び直して保存してください。</div>'
    : '';
  // 保存値が不正な間、非表示の第四の選択肢（`_USAGE_CHAT_PROVIDER_INVALID`）をチェック状態で
  // 混ぜておく——同じ `name` のラジオグループなので、admin が3択のどれかを選べば自然に
  // こちらのチェックは外れる（`selectedUsageChatProvider` 参照）。
  // `hidden` だけでもブラウザ標準では tab 順・AX ツリーの両方から外れるが、CSS 上書き等の
  // 事故に頼らず明示する（`aria-hidden`＝スクリーンリーダーに存在ごと知らせない・
  // `tabindex="-1"`＝Tab キーで到達させない）。
  const invalidRadio = invalidSaved
    ? `<input type="radio" name="usage-chat-provider" `
      + `data-usage-chat-provider="${_USAGE_CHAT_PROVIDER_INVALID}" checked hidden `
      + `aria-hidden="true" tabindex="-1">`
    : '';
  // A7（cloud_provider）が openai でない間、「OpenAI に固定」を選んでも中央 OpenAI キーは
  // 使えず 503（未接続）になる（A7 の排他選択契約＝非選択クラウドのキーは使わない・
  // `sherpa/usage_chat.py::_resolve_cfg` の openai 分岐が `resolve_api_key(..., strict=True)`
  // で honest failure にする）。挙動自体は契約どおりだが、理由が分からないと壊れて見えるため
  // 選択肢の横に注記する。
  const cloudProvider = (cloud && cloud.provider) || 'openai';
  const cloudLabelFor = (p) => (CLOUD_PROVIDER_LABELS[p] || { label: p }).label;
  const options = [{ value: _USAGE_CHAT_FOLLOW_VALUE, label: '頭脳の選択に合わせる（既定）' }]
    .concat(uc.providers.map((p) => ({ value: p, label: `${USAGE_CHAT_PROVIDER_LABELS[p] || p} に固定` })));
  wrap.innerHTML = warningHtml + invalidRadio + options.map((opt) => {
    const isFollow = opt.value === _USAGE_CHAT_FOLLOW_VALUE;
    const checked = !invalidSaved && (isFollow ? configured == null : opt.value === configured)
      ? ' checked' : '';
    const openaiKeyHint = (opt.value === 'openai' && cloudProvider !== 'openai')
      ? `<div class="hint">OpenAI のキーは頭脳の選択が OpenAI のときだけ使えます`
        + `（現在: ${esc(cloudLabelFor(cloudProvider))}）。</div>`
      : '';
    return `<label class="cloud-provider-row">`
      + `<input type="radio" name="usage-chat-provider" data-usage-chat-provider="${esc(opt.value)}"${checked}>`
      + `<span class="arm-t">${esc(opt.label)}</span></label>${openaiKeyHint}`;
  }).join('');
}
// 「いま実際に使われるのは」欄: 固定/連動のどちらでも、解決結果（`effective`）と、その根拠
// （実行構成＝A7 `cloud_provider` の現在値）の両方を示す——固定中でも「今の実行構成のままなら
// 何が選ばれるか」が分かるようにする。
function renderUsageChatAiStatus(uc, cloud) {
  const el = $('usage-chat-ai-status');
  if (!el) return;
  if (!_usageChatAiAvailable || _usageChatSavedInvalid) { el.textContent = ''; return; }
  const label = (p) => USAGE_CHAT_PROVIDER_LABELS[p] || p;
  const cloudProvider = (cloud && cloud.provider) || 'openai';
  const cloudLabel = (CLOUD_PROVIDER_LABELS[cloudProvider] || { label: cloudProvider }).label;
  const lead = (uc.configured != null)
    ? 'この設定で固定中です（既定の変更にはこの先も追従しません）。'
    : '頭脳の選択に合わせています（変更して保存すると、この内容で固定されます）。';
  el.textContent = `${lead} いま実際に使われるのは: ${label(uc.effective)}（頭脳の選択: ${cloudLabel}）。`;
}
function selectedUsageChatProvider() {
  if (!_usageChatAiAvailable) return null;
  const el = document.querySelector('#usage-chat-ai-radios input[data-usage-chat-provider]:checked');
  return el ? el.dataset.usageChatProvider : null;
}

// ===== クラウド AI プロバイダの中央設定 =====

function selectedCloudProvider() {
  const el = document.querySelector('#cloud-provider-radios input[data-cloud-provider]:checked');
  return el ? el.dataset.cloudProvider : 'openai';
}

function renderCloudProviderRadios(cloud) {
  const wrap = $('cloud-provider-radios');
  if (!wrap) return;
  const providers = cloud.providers || ['openai', 'gemini', 'bedrock'];
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

// キー欄はラジオで選ばれているプロバイダ1つ分だけ表示する（A7: 非選択プロバイダの入力欄は出さない）。
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
  // 削除できるキーが無い（未設定・削除直後）ときはボタンを disabled にする（誤操作の確認
  // ダイアログを無駄に出さない）。
  const clearBtn = $('cloud-key-clear');
  if (clearBtn) clearBtn.disabled = !keySet;
}

function renderCloudStatus(cloud) {
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

// クラウド設定（プロバイダ／個人キー／個人キー許可／Ollama 既定接続先）が render() 時点の
// 基準値（_cloudBaseline）から変わっているか。キー入力欄は書込専用のため基準値は常に空文字＝
// 入力中の値が空でなければ「変更あり」として扱う。
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

// PART-6: Webhook 許可リストの変更判定（`ollamaAllowlistChanged()` と同型）。
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
  // Ollama の許可ホスト一覧（利用者の <select> の選択肢の元）。
  if (ollamaAllowlistChanged()) {
    const lines = _sortedUniqueLines(($('cloud-ollama-allowlist') || {}).value);
    body.ollama_allowlist = lines.length ? lines : null;
  }
  // PART-6: Webhook 宛先の許可リスト。
  if (webhookAllowlistChanged()) {
    const lines = _sortedUniqueLines(($('webhook-allowlist') || {}).value);
    body.webhook_allowlist = lines.length ? lines : null;
  }
}

// Ollama の許可ホスト一覧（画面が無かった system_settings 設定を管理画面へ出す）。
function renderOllamaAllowlist(info) {
  const ta = $('cloud-ollama-allowlist');
  if (!ta || !info) return;
  ta.value = (info.configured || []).join('\n');
}

// PART-6: Webhook 宛先の許可ホスト一覧（`ollama_allowlist` と同型の UI）。
function renderWebhookAllowlist(info) {
  const ta = $('webhook-allowlist');
  if (!ta || !info) return;
  ta.value = (info.configured || []).join('\n');
}

// ===== SET-2c: OpenAI 互換 API の接続先（本家／Azure OpenAI／その他 OpenAI 互換） =====

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

// 埋め込みのデプロイ名欄は「使えるモデル」（model_catalog.openai.embed）の値をそのまま表示する
// 唯一の表示先（二重の保存先を持たない）。「使えるモデル」タブの表・モーダルで同じセルを変えた
// ときもこの関数を呼んで表示を同期させる（どちらの面で編集しても _mcState が単一の真実源）。
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

// 保存（collectOpenaiEndpoint）と接続テスト（testOpenaiEndpoint）が共有する pending 生成処理。
// フォームに現在入力されている値をそのまま返す。「本家」選択時は他の3項目を含めない
// （`llm.py` は kind=openai なら base_url/auth_header/api_version を常に無視する契約なので、
// 保存時にわざわざ null 化しない＝azure→openai→azure と往復しても値が保持される）。
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

// 接続先（種別／URL／認証ヘッダ／APIバージョン）が render() 時点の基準値から変わっているか。
// 認証ヘッダ・APIバージョンは DOM 上「詳細」折りたたみの中にあり `#openai-endpoint-fields` の
// 外に置かれているため、要素の親子関係に頼らずここで値そのものを比較する。
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

// 埋め込みのデプロイ名欄を編集し終えた（'change'＝blur/Enter）ときの反映先は model_catalog
// （openai/embed）そのもの（唯一の真実源）。「使えるモデル」タブの表を直接触った場合と同じ規約
// （default が allowed に無ければ先頭へ足す）で `_mcState` を更新し、その場で表側の表示も
// 追従させる。'input'（1文字ごと）ではなく'change'で呼ぶ＝入力途中の値が allowed へ蓄積されない。
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

// 接続テスト: 個人設定用 /settings/test の流用をやめ、admin 専用の
// POST /admin/settings/openai-endpoint-test へ分離。中央キー・中央モデルだけを使い、保存とテストで
// 共通の pending 生成処理（collectOpenaiEndpointPending）を使う・保存しない。
let _endpointTestBusy = false;   // 多重クリック防止（実送信＋監査記録を伴うため・閉域実機 2026-09-04）
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

// 接続テスト（POST /settings/test・入力中のキーで1回だけ試す・保存しない・admin 本人のログインで実行）。
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

// 中央 API キーの削除（書込専用欄には現在の値が表示されないため、キー欄を空のまま保存しても
// 「未入力＝変更しない」として無視される＝クリアする手段が無かった。ここは唯一の明示クリア導線
// のため確認ダイアログを挟み、確定した操作でだけ空文字を PUT する）。
// 削除待ち中にプロバイダを切り替える／キー入力・保存・タブのリセットが割り込む／連続で削除操作を
// 始めると、後から届いた古い応答が切替先の未保存編集・表示や、より新しい操作の結果を上書きして
// しまう（U-6 と同じ世代照合の型）。要求時点のプロバイダ・世代を捕捉し、応答到着時にどちらも
// 一致する時だけ判定する（判定を先に行い、不一致なら _view・表示のどちらにも一切触れない）。
// 応答の値だけを見て「provider のキーが削除された」の1点を無条件に信用して反映する案は、この
// 削除より後に同じ provider へ完了した別の保存（新しいキーの設定）の結果を巻き戻してしまうため
// 採らない（不一致＝この応答はもう「今の真実」を代表しない、として丸ごと捨てる）。
let _cloudKeyClearGen = 0;
// プロバイダ切替・キー入力・保存・タブのリセットのいずれでも呼ぶ（U-6 の invalidate と同型）。
// 破棄した削除待ちの結果表示（「削除しています...」「✓ 削除しました」等）がそのまま残ると、
// 実際にはもう関係ない操作が今も進行中/完了したかのように見えてしまうため、ニュートラルへ戻す。
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
    // 判定を先に行う: 世代・プロバイダが不一致なら、この応答はもう「今の真実」を代表しない
    // （この削除より後に同じ provider へ保存された新しいキー等、より新しい操作の結果を
    // 巻き戻しかねないため）。_view・表示のどちらにも一切触れずに捨てる。
    if (myGen !== _cloudKeyClearGen || selectedCloudProvider() !== provider) return;
    // renderProviderTab(view) は呼ばない＝同タブの未保存編集（プロバイダ選択・個人キー許可・
    // Ollama URL/allowlist・OpenAI 接続先）を保存済み値で無言破棄してしまう（baseline も
    // 巻き戻り丸印が消える）。ここで実際に変わったのはキーの設定有無だけなので、キー欄の表示
    // （設定済みバッジ）と、キー有無に依存する他の案内（VLM のキー未設定警告）だけを更新する。
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
// 1枚の表＝行=用途・列=選択中のクラウド AI（A7）＋Ollama＋Codex のみ。Bedrock はモデル一覧が
// 実在確認つきの動的取得（`GET /settings/bedrock-models`・個人設定側）のため、この静的一覧の
// 対象外（列は出すが編集不可）。
function mcColumns() {
  const cloudMeta = CLOUD_PROVIDER_LABELS[_mcCloudProvider] || { label: _mcCloudProvider };
  return [
    { key: _mcCloudProvider, label: cloudMeta.label, editable: _mcCloudProvider !== 'bedrock' },
    { key: 'ollama', label: MC_COLUMN_LABELS.ollama, editable: true },
    { key: 'codex', label: MC_COLUMN_LABELS.codex, editable: true },
  ];
}

function mcCell(provider, usage) {
  return (_mcState[provider] || {})[usage];
}

// 2セル（{allowed,default} 形）が一致するか。`allowed` は並び順も契約（描画・API とも保持する）
// ため、順序込みで比較する＝候補の並べ替えだけの変更も差分として扱う。
function _mcCellEquals(a, b) {
  if (!a || !b) return false;
  return JSON.stringify(a.allowed || []) === JSON.stringify(b.allowed || [])
    && (a.default || '') === (b.default || '');
}

// 差分表示: このセル（保存済みまたは未保存編集中の値）が組み込み既定（`_mcBuiltin`）と
// 異なるかどうかで判定する（`configured` にセルが存在するというだけでは、既定と同じ値を
// 明示保存した場合を区別できない）。
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
      // RV 4巡目 #11: `cell.default` が空（既定未設定＝組み込み既定へ解決）のとき、どの
      // <option> にも selected を付けないと、ブラウザは先頭の実モデル名を選択済みとして表示
      // してしまい「先頭のモデルが既定」だと誤認させる。空の「（未設定）」を先頭に明示して
      // 選択済みにする（実際に既定が無いことをそのまま見せる）。
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
  if (_mcCloudProvider === 'bedrock') {
    html += '<div class="hint">Bedrock は個人設定の「利用可能なモデルを取得」で実在確認つきの一覧から選びます。</div>';
  }
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

// 「使えるモデル」タブに属する未保存編集があるか（プロバイダ＋接続先タブの埋め込みデプロイ名は
// 別軸＝ mcEmbedChanged() で判定する）。
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

// 保存する model_catalog は `_mcState`（表示中の全セル）をそのまま送らない。全置換の契約
// （セルが1つでも含まれていれば「管理者が明示設定した」ことになる）のため、`_mcState` を丸ごと
// 送ると (a) 触っていないセルまで組み込み既定の値で明示固定される、(b) リセットで未設定へ
// 戻したセルが後続の保存で復活する、という実害がある。
//
// 未編集セル（`_mcTouched` に無いキー）は `_mcConfiguredRaw`（保存済みの生 configured）の値を
// そのまま維持する＝組み込み既定と同値のセルを過去に明示保存していた場合でも、別セルの編集・
// 保存だけでは provenance（管理者が明示固定した事実）を失わない。このセッションで実際に編集した
// セル（`_mcTouched`）は3通りに分ける: (1) 現在値が保存済みの生 configured（`raw`）と一致＝
// 別候補へ変更してから元の明示固定値へ戻した＝pin を維持（raw をそのまま載せる。組み込み既定と
// 偶然同値でも明示固定の事実は落とさない）。(2) raw と異なり、かつ組み込み既定とも異なる＝
// 現在値を載せる。(3) それ以外（raw が無い、または raw と異なるが組み込み既定と同値になった）＝
// 除外し明示設定を落として組み込み既定への追従に戻す。
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
      // 別候補へ変更してから元の明示固定値（raw）へ戻した＝pin は維持する
      // （組み込み既定と偶然同値でも、明示固定した事実は落とさない）。
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
    // 非遡及: ここでの変更は今後の新規発行にのみ効く（発行済みキーの上限は発行時の値のまま）。
    hint.textContent = base + '変更は新規発行から適用されます（発行済みのキーの上限は変わりません）。';
  }
  // AI 下調べ検索の既定 AI（ollama/openai の2択・vlm-provider と同型の描画）。保存値が
  // どちらでもない（`system_extras.py` の "(不正な保存値)" 等）場合は黙って既定へ丸めず、
  // その旨を select の一時的な選択肢とヒントでそのまま示す（管理者が破損に気付けるように）。
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
    // PART-6: Webhook 登録の有無（host:port のみ・secret は絶対に出さない・一覧レスポンス自体に含まれない）。
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

// 一覧 GET の世代番号。呼び出しのたびに採番し、応答が届いた時点で「自分より新しい呼び出しが
// 既に始まっていないか」を確認してから描画する——複数回の `loadExtKeys()`（発行成功直後・
// タイムアウト回復後 等）が重なったとき、後から発行したのに先に届いた新しい応答を、遅れて
// 届いた古い応答が上書きしてしまう事故を防ぐ（GET はサーバー側で順序を保証しないため、
// クライアント側で「一番新しく発行した呼び出しの結果だけを採用する」規律を持たせる）。
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

// 発行モーダルの状態機械: 'idle'（フォーム入力中）→ 'issuing'（応答待ち・閉鎖不可）→
// 'revealed'（発行成功・キー本体を表示中）。'issuing' の間は閉じる手段（✕・キャンセル・背景
// クリック）を全て無効化する——発行はしたがキーを一度も見せないまま閉じてしまうと、有効な
// キーだけが残って利用者が控えを取れない事故になるため。
//
// 操作トークン（`_ekActiveOp`）: `openExtKeyModal`/`closeExtKeyModal` のたびに新しい値を
// 発行し、以後の非同期処理（POST応答・一覧再取得・是正フロー）は自分の発行時点のトークンと
// 現在の `_ekActiveOp` が一致する時だけ画面状態を変更する。古い処理の `finally` 相当の後始末が
// 新しい操作のボタン状態を巻き戻す事故（例: 1本目の一覧再取得待ちの間に閉じて2本目を発行、
// その後1本目の後処理が2本目の submit を誤って再有効化する）を構造的に防ぐ。
// 一覧の再取得（`loadExtKeys()`）は発行の成否判定から意図的に切り離す（await しない・
// 発行の成功表示は一覧取得の遅延/失敗に引きずられない）。
let _ekModalState = 'idle';
let _ekOpSeq = 0;
let _ekActiveOp = 0;

function _ekClearRevealedKey() {
  // 平文はモーダルを開く/閉じるたびに DOM から確実に消す（開いたままにしない）。
  const el = $('ek-reveal-key');
  if (el) el.textContent = '';
  const wh = $('ek-reveal-webhook-secret');
  if (wh) wh.textContent = '';
  const whWrap = $('ek-reveal-webhook');
  if (whWrap) whWrap.hidden = true;
}

function _genOpId() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  // crypto.randomUUID が無い環境向けの UUID v4 形式フォールバック（サーバーは client_op_id を
  // UUID 形式のみ受理する＝任意形式の文字列を送ると 422 になる）。
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

// モーダルが開いている間、背後（トップバー・本文・保存バー等）を `inert` にする——キーボード
// 操作（Tab）・クリックのどちらでも背後の要素に到達できなくする（フォーカストラップの代替・
// スクリーンリーダーからも隠れる）。`#toast` は通知の読み上げを妨げないため対象外にする。
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
  // 開く前にフォーカスがあった要素（通常は「発行」ボタン）へ復帰する（inert 解除後・
  // フォーカスが失われて body に落ちたままにしない）。
  if (_ekOpenerEl && typeof _ekOpenerEl.focus === 'function') _ekOpenerEl.focus();
  _ekOpenerEl = null;
}

// POST の結果が不明（タイムアウト・通信断・不正な形の応答）なときの回復導線。専用エンドポイント
// （`POST /ext/v1/admin/keys/recover`）へこの試行の `client_op_id` を渡し、サーバー側で
// 「自分（admin本人）が発行操作を試みた・未失効の」キーを**単一の原子的操作**で照合・失効する
// （一覧取得→別リクエストで DELETE、という2段構成は「一覧に他人の行も混じる」「その間に別の
// 変更が起こる」隙があるため使わない）。POST が実際にはまだコミットされていない（サーバー側の
// 処理が遅延している）競合を閉じるため、有界に再試行する（3回×2秒間隔）。`found: true` を
// 確認できた場合のみ「失効しました」と表示する（確認できなければ失敗を失敗として表示する・
// 曖昧なまま成功したかのように見せない）。
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
  // `min` 属性はネイティブの日付ピッカー経由の操作しか防げない（手入力・貼り付け・自動入力で
  // 過去日を直接セットされると HTML の制約検証を経ずに値が入りうる）。送信前にも文字列比較
  // （YYYY-MM-DD 形式は辞書順=時系列順）で確実に弾き、過去日では POST 自体を発生させない
  // （サーバ側422はあくまで最後の砦）。
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
    // 2xx だが期待する形でない＝サーバーの書込みが実際に成功したかどうか分からない（曖昧）。
    await _ekRecoverFromAmbiguousIssue(myOp, clientOpId);
    return;
  }
  $('ek-issue-form').hidden = true;
  $('ek-reveal').hidden = false;
  $('ek-reveal-key').textContent = d.key;
  // PART-6: webhook_url を指定して発行した場合のみ、secret も同じレスポンスに1度だけ含まれる。
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
// クリップボードへコピー（`navigator.clipboard` 不可の環境向けに `execCommand('copy')` へ
// フォールバック）。キー本体・Webhook secret のどちらの「今だけ表示」欄でも使う共通処理。
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
// #ext-keys-list は render() 系と独立に loadExtKeys() が丸ごと innerHTML を入れ替えるため、
// 常に存在するコンテナへの委譲リスナー1本にする（#arms-list 等と同じ流儀）。
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

// ===== タブ単位の描画（各タブの render はそのタブが持つフィールドの baseline も更新する。
// タブを1つだけ再描画したいとき（タブ単位リセット）は対応する関数だけを呼び、他タブの
// 表示・baseline には触れない＝他タブの未保存編集を巻き込まない）。

function renderProviderTab(view) {
  const cloud = view.cloud || {};
  renderCloud(cloud);
  renderOllamaAllowlist(view.ollama_allowlist);
  renderWebhookAllowlist(view.webhook_allowlist);
  renderOpenaiEndpoint(view.openai_endpoint);   // _mcState（使えるモデル）を先に更新してから読む
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
  renderDepthProfile(view.depth_profile);
  renderChatExamples(view.chat_examples);
}

// SC-6c: 調べる深さの基準値（標準時の値）。同じ「プロバイダ＋接続先」タブの2つ目のカード。
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
      : `未設定です（環境設定の既定 ${reasoning.effective} が適用されます）。`;
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

// チャット画面のクイック入力例（`chat_examples`）。同じ「プロバイダ＋接続先」タブの3つ目のカード。
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
// 保存前にサーバと同じ範囲（HTML の min/max 属性・実 API の Field(ge,le) と同じ値）を検証し、
// 日本語で案内する。pydantic の 422 応答（`detail` が配列）をそのまま共通のエラー表示へ渡すと
// `[object Object]` になり読めないため（`web/common.js::_sherpaApi` の汎用処理）、通常の
// 保存操作ではここで弾いて 422 に到達させない。空欄（未設定へ戻す）は対象外。
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

// BUDGET-1: 人に読みやすいバイト表示（KB/MB 換算・平文原則）。1024 進数（`web/workspace.js::
// fmtSize` と同じ換算）。
function _fmtBytesHuman(bytes) {
  if (bytes == null) return '—';
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)}MB`;
  return `${Math.round(bytes / 1024)}KB`;
}

// SC-6c の depth_profile と同じ「プロバイダ＋接続先」流儀の別カード（取り込みタブに配置）。
// 表示/入力欄は KB 単位、GET/PUT のやり取り（bytes）とは境界でだけ変換する。
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
        ? `この値（${_fmtBytesHuman(info.configured)}）で固定中です（既定値: ${_fmtBytesHuman(info.default)}）。`
        : `未設定です（組み込みの既定 ${_fmtBytesHuman(info.effective)} が適用されます）。`;
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
// validateDepthProfileInputs と同じ流儀（422 の配列表示が読みにくいため保存操作では先に弾く）。
// HTML の min/max（KB）＝サーバの `loBytes`/`hiBytes` を1024で割った値と一致させてある。
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

// ===== BUDGET-2（2026-09-02-RAG表現の全形式展開と文脈保持.md §3.4・2026-09-03 裁定・
// モデルが一度に読める量との連動）: 現在のモデルのヒント表示＋一度に読める量の管理者登録表（追加/上書き/削除）。=====

const _MODEL_WINDOW_PROVIDER_LABELS = {
  openai: 'OpenAI', gemini: 'Gemini（Google）', bedrock: 'AWS Bedrock (Claude)',
  ollama: 'ローカル（Ollama）', codex: 'Codex',
};
const _MODEL_WINDOW_SOURCE_LABELS = {
  registered: 'この画面で登録した値', api: 'AI から自動取得', seed: 'このアプリに組み込みの一覧',
  unknown: '不明',
};

// 現在のモデル・一度に読める量・出所・自動調整後の上限（ヒント表示のみ・入力欄ではない）。
function renderAgenticBudgetWindow(ab) {
  const status = $('agentic-budget-window-status');
  const unknownBox = $('agentic-budget-window-unknown');
  const w = (ab || {}).window || {};
  if (!status || !unknownBox) return;
  const providerLabel = _MODEL_WINDOW_PROVIDER_LABELS[w.provider] || w.provider || '不明';
  const modelLabel = w.model || '(未設定)';
  if (w.source === 'unknown' || w.window_tokens == null) {
    status.textContent = `現在のモデル: ${providerLabel} / ${modelLabel}`;
    unknownBox.hidden = false;
  } else {
    const sourceLabel = _MODEL_WINDOW_SOURCE_LABELS[w.source] || w.source;
    status.textContent = `現在のモデル: ${providerLabel} / ${modelLabel}　一度に読める量: `
      + `${w.window_tokens.toLocaleString('ja-JP')} トークン（出所: ${sourceLabel}）　`
      + `自動調整後の上限: ${_fmtBytesHuman(w.derived_cap_bytes)}`;
    unknownBox.hidden = true;
  }
}

let _modelWindowsBaseline = {};   // render() 時点の登録値（"provider:model" -> tokens）

function _modelWindowsRowHtml(provider, model, tokens) {
  const providers = ((_view || {}).model_catalog || {}).providers || ['openai', 'gemini', 'ollama', 'bedrock', 'codex'];
  const opts = providers.map((p) =>
    `<option value="${esc(p)}"${p === provider ? ' selected' : ''}>${esc(_MODEL_WINDOW_PROVIDER_LABELS[p] || p)}</option>`
  ).join('');
  return `<tr>
    <td><select class="mw-provider">${opts}</select></td>
    <td><input type="text" class="mw-model" value="${esc(model || '')}" placeholder="例: gpt-4o" autocomplete="off"></td>
    <td><input type="number" class="mw-tokens" value="${tokens != null ? tokens : ''}" min="1" max="10000000" step="1" style="max-width:140px"></td>
    <td><button class="btn-ghost mw-remove" type="button">削除</button></td>
  </tr>`;
}

function renderModelWindowsTable(mw) {
  const tbody = $('agentic-model-windows-rows');
  if (!tbody) return;
  const configured = (mw || {}).configured || {};
  _modelWindowsBaseline = { ...configured };
  tbody.innerHTML = Object.keys(configured).sort().map((key) => {
    const [provider, ...rest] = key.split(':');
    return _modelWindowsRowHtml(provider, rest.join(':'), configured[key]);
  }).join('');
}

// 行の削除は動的に増減する要素のため、コンテナへのイベント委譲で拾う（他の動的リストと同じ流儀・
// `_ekIssueOpen`/`_mcTable` 等の直下トップレベル `if (elem) elem.addEventListener(...)` に揃える）。
const _mwAddBtn = $('agentic-model-windows-add');
if (_mwAddBtn) _mwAddBtn.addEventListener('click', () => {
  const tbody = $('agentic-model-windows-rows');
  if (tbody) tbody.insertAdjacentHTML('beforeend', _modelWindowsRowHtml('openai', '', null));
});
const _mwRows = $('agentic-model-windows-rows');
if (_mwRows) _mwRows.addEventListener('click', (ev) => {
  if (ev.target && ev.target.classList.contains('mw-remove')) {
    const tr = ev.target.closest('tr');
    if (tr) tr.remove();
  }
});

// 現在の行の内容（空行は無視）を "provider:model" -> tokens の dict にする。
function _collectModelWindowsRows() {
  const rows = Array.from(document.querySelectorAll('#agentic-model-windows-rows tr'));
  const out = {};
  rows.forEach((tr) => {
    const provider = (tr.querySelector('.mw-provider') || {}).value || '';
    const model = ((tr.querySelector('.mw-model') || {}).value || '').trim();
    const tokensRaw = ((tr.querySelector('.mw-tokens') || {}).value || '').trim();
    if (!model || tokensRaw === '') return;   // 未入力行は無視（保存対象にしない）
    out[`${provider}:${model}`] = Number(tokensRaw);
  });
  return out;
}

function modelWindowsTableChanged() {
  return JSON.stringify(_collectModelWindowsRows()) !== JSON.stringify(_modelWindowsBaseline);
}

function collectModelWindowsTable(body) {
  if (!modelWindowsTableChanged()) return;
  const rows = _collectModelWindowsRows();
  body.model_context_windows = Object.keys(rows).length ? rows : null;
}

// 保存操作の事前チェック（422 の配列表示が読みにくいため先に弾く・他の validate* と同じ流儀）。
function validateModelWindowsInputs() {
  const errors = [];
  Array.from(document.querySelectorAll('#agentic-model-windows-rows tr')).forEach((tr) => {
    const model = ((tr.querySelector('.mw-model') || {}).value || '').trim();
    const tokensRaw = ((tr.querySelector('.mw-tokens') || {}).value || '').trim();
    if (!model && tokensRaw === '') return;   // 完全な空行は無視
    if (!model) { errors.push('モデルが一度に読める量の登録: モデル名を入力してください'); return; }
    const n = Number(tokensRaw);
    if (tokensRaw === '' || !Number.isFinite(n) || !Number.isInteger(n) || n < 1 || n > 10_000_000) {
      errors.push(`モデルが一度に読める量の登録（${model}）: 一度に読める量（トークン数）は1〜10,000,000の整数で指定してください`);
    }
  });
  return errors;
}

function renderModelsTab(view) {
  renderModelCatalog(view.model_catalog, (view.cloud || {}).provider);
}

function renderIngestTab(view) {
  renderArms(view.arms || {});
  renderArmsStatus(view.arms || {});
  renderLegacy(view.legacy_backend);
  renderLegacyStatus(view.legacy_backend);
  renderVlm(view.vlm);
  renderVlmStatus(view.vlm);
  renderRagLlmRender(view.rag_llm_render);
  renderRagLlmRenderStatus(view.rag_llm_render);
  renderAgenticBudget(view.agentic_budget);   // BUDGET-1（§3.4）
  renderAgenticBudgetWindow(view.agentic_budget);      // BUDGET-2（§3.4）
  renderModelWindowsTable((view.agentic_budget || {}).model_windows);   // BUDGET-2（§3.4）
  _armsBaseline = [...((view.arms || {}).enabled || [])].sort();
  _legacyBaseline = _legacySelectedValue(view.legacy_backend);
  const vlm = view.vlm;
  _vlmBaseline = (vlm && vlm.effective) ? {
    provider: vlm.effective.provider === 'openai' ? 'openai' : 'ollama',
    model: vlm.effective.model || '', cloud_allowed: !!vlm.effective.cloud_allowed,
  } : null;
  _ragLlmRenderBaseline = !!(view.rag_llm_render && view.rag_llm_render.effective);
}

function renderUsageTab(view) {
  renderUsageChatAi(view.usage_chat, view.cloud);
  renderUsageChatAiStatus(view.usage_chat, view.cloud);
  // 応答が不正（`_usageChatAiAvailable === false`）なら baseline も null にする——
  // selectedUsageChatProvider() も null を返すため、null !== null で「未変更」のまま安全。
  // 保存値そのものが不正（`_usageChatSavedInvalid`）なら、baseline も選択中の第四の選択肢と
  // 同じ `_USAGE_CHAT_PROVIDER_INVALID` に揃える。それ以外は `configured`（生の保存値・
  // `null`＝「実行構成に合わせる」を表す `_USAGE_CHAT_FOLLOW_VALUE` へ変換）を基準にする
  // ——`effective`（A7 連動の解決結果）を基準にしないのは `_USAGE_CHAT_FOLLOW_VALUE` 定義の
  // コメント参照。
  const uc = view.usage_chat || {};
  _usageChatProviderBaseline = !_usageChatAiAvailable ? null
    : _usageChatSavedInvalid ? _USAGE_CHAT_PROVIDER_INVALID
    : (uc.configured == null ? _USAGE_CHAT_FOLLOW_VALUE : uc.configured);
}

// 外部連携（API キー）タブの描画（利用量タブとは baseline を分けて持つ）。
function renderExtKeysTab(view) {
  renderExtKeysToggle(view.ext_keys);
  const extKeys = view.ext_keys || {};
  _extKeysAllowedBaseline = !!extKeys.user_api_keys_allowed;
  const quota = extKeys.daily_quota_default || {};
  _extKeysQuotaBaseline = quota.configured != null ? String(quota.configured) : '';
  const rdp = extKeys.research_default_provider || {};
  // 保存値が破損している（ollama/openai のどちらでもない）間は、その破損状態自体を基準値に
  // する——黙って 'ollama' を基準にすると、画面が 'ollama' を表示している間は「未保存の変更なし」
  // に見えてしまい、管理者が破損に気付かないまま何も保存されない状態が続く。
  _extKeysResearchProviderBaseline = (rdp.effective === 'openai' || rdp.effective === 'ollama')
    ? rdp.effective : _RESEARCH_PROVIDER_INVALID;
}

function render(view) {
  _view = view;
  renderModelsTab(view);   // _mcState を先に更新（renderProviderTab の埋め込み欄表示が読むため）
  renderProviderTab(view);
  renderIngestTab(view);
  renderUsageTab(view);
  renderExtKeysTab(view);
  applyConfigChangedHighlights(view);
  refreshTabDots();
}

async function load() {
  try {
    render(await getJSON('/admin/settings'));
  } catch (e) {
    $('msg').innerHTML = '<span class="danger">設定を読み込めませんでした</span>';
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

// ⑤: 視覚読み取りの VLM 設定を収集（provider/model/cloud_allowed）。
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

// L5（U1）: rag.md の LLM 成形トグル。真偽で比較し、PUT では "on"/"off" 文字列に変換して送る
// （バックエンドの _validate_rag_llm_render が受け付けるのは文字列 "on"/"off" のみ）。
function ragLlmRenderChanged() {
  return !!($('rag-llm-render') || {}).checked !== _ragLlmRenderBaseline;
}
function usageChatProviderChanged() {
  if (!_usageChatAiAvailable) return false;   // データ不正時は保存対象に含めない（save() 参照）
  return selectedUsageChatProvider() !== _usageChatProviderBaseline;
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
  // 埋め込みデプロイ名欄は 'change'（blur/Enter）で確定するため、フォーカスが残ったまま保存
  // ボタンを押した場合に備えて保存直前にも確定させる（安全網・値が変わっていなければ no-op）。
  applyEmbedDeploymentFieldEdit();
  // 調べる深さの基準値は範囲外の値を送る前にここで弾く（422 の配列表示が [object Object] に
  // なる問題を、保存操作では到達させないことで避ける）。
  const depthProfileErrors = validateDepthProfileInputs();
  // BUDGET-1（§3.4）: agentic search の tool-result バイト予算も同じ理由で保存前に弾く。
  const agenticBudgetErrors = validateAgenticBudgetInputs();
  // BUDGET-2（§3.4）: モデルが一度に読める量の登録表も同様に保存前に弾く。
  const modelWindowsErrors = validateModelWindowsInputs();
  const rangeErrors = depthProfileErrors.concat(agenticBudgetErrors).concat(modelWindowsErrors);
  if (rangeErrors.length) {
    $('msg').innerHTML = `<span class="danger">${esc(rangeErrors.join('／'))}</span>`;
    return;
  }
  // A6（個人 API キー原則）: personal_api_keys_allowed を OFF で保存すると全ユーザーの個人キーが
  // 削除される。個人キーを保有する利用者が1人以上いるときは確認ダイアログ（人数表示）を出し、
  // キャンセルは保存全体を中断する（他フィールドの変更も含めて何も送らない）。
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
  // user_api_keys_allowed を OFF で保存すると、利用者が発行した外部連携キーが
  // すべて失効する（A6 と同型の確認ダイアログ）。
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
  // 保存もキー削除待ちの世代を進める（U-6 と同型の invalidate）: この保存の応答（サーバの
  // 全体スナップショット）が先に _view へ入るため、それより古い削除応答が後から届いても
  // 巻き戻せないようにする。
  _invalidateCloudKeyClear();
  $('save').disabled = true;
  $('msg').innerHTML = '<span class="loading-inline" role="status"><span class="spinner spinner-sm"></span><span>保存中...</span></span>';
  const body = {};
  if (armsChanged()) body.arms_enabled = collectArms();   // 触っていない・元に戻していれば送らない
  // 旧形式変換も値が変わったときだけ送る（none も明示的な選択として送る＝env が libreoffice でも尊重）。
  if (legacyChanged()) { const lb = collectLegacy(); if (lb) body.legacy_backend = lb; }
  if (vlmChanged()) body.vlm = collectVlm();
  if (ragLlmRenderChanged()) body.rag_llm_render = ($('rag-llm-render') || {}).checked ? 'on' : 'off';
  if (usageChatProviderChanged()) {
    const v = selectedUsageChatProvider();
    // 破損状態を示す一時的な選択肢がそのまま選ばれていたら送らない（研究用既定 AI の
    // `_RESEARCH_PROVIDER_INVALID` と同じ理由・通常は baseline と一致し changed() が
    // false になるためここへは来ないが、直接呼び出し等に備えた構造的な保証）。「実行構成に
    // 合わせる」（`_USAGE_CHAT_FOLLOW_VALUE`＝`''`）が選ばれていれば `null`（未設定へ戻す）を
    // 送り、それ以外（openai/ollama への明示固定）は effective の現在値と一致していても
    // 必ずそのまま送る（`configured` が変わった以上、A7 が変わっても追従させないという
    // 意思決定そのものが変わったということ・省略すると PUT 後に A7 依存で黙って再計算
    // されてしまう）。
    if (v !== _USAGE_CHAT_PROVIDER_INVALID) {
      body.usage_chat_provider = (v === _USAGE_CHAT_FOLLOW_VALUE) ? null : v;
    }
  }
  // クラウド AI プロバイダの中央設定も変わった項目だけ送る。
  collectCloud(body);
  // 接続先（種別/URL/認証ヘッダ/APIバージョン）が変わったときだけ送る。
  collectOpenaiEndpoint(body);
  // 使えるモデルを変えたときだけ送る。全置換の契約（sherpa/model_catalog.py::validate_catalog）
  // のため、`_mcState` をそのまま送らず、組み込み既定と異なるセルだけを拾って組み立てる
  // （buildModelCatalogBody 参照・触っていないセルの明示固定・リセット直後の復活を防ぐ）。
  // 埋め込みのデプロイ名欄への直接編集も同じ `_mcState` に反映済みのためここでまとめて載る。
  if (mcCatalogChanged()) body.model_catalog = buildModelCatalogBody();
  if (extKeysAllowedChanged()) body.user_api_keys_allowed = extKeysAllowedNow;
  if (extKeysQuotaChanged()) {
    const v = ($('ext-keys-user-quota-default') || {}).value;
    body.user_api_keys_daily_quota_default = v === '' ? null : Number(v);
  }
  if (extKeysResearchProviderChanged()) {
    const v = ($('ext-research-default-provider') || {}).value || 'ollama';
    // 破損状態を示す一時的な選択肢がそのまま選ばれていたら送らない（サーバへ無意味な値
    // "__invalid__" を送って 422 になるのを避ける・ollama/openai のどちらかへ選び直させる）。
    if (v !== _RESEARCH_PROVIDER_INVALID) body.research_default_provider = v;
  }
  collectDepthProfile(body);   // SC-6c: 調べる深さの基準値（変わった項目だけ送る）
  if (chatExamplesChanged()) body.chat_examples = collectChatExamples();   // チャットの質問例
  collectAgenticBudget(body);  // BUDGET-1（§3.4）: 検索の情報量予算（変わった項目だけ送る）
  collectModelWindowsTable(body);   // BUDGET-2（§3.4）: モデルが一度に読める量の登録表（変わっていれば送る）
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
// 秘密（API キー）は対象外＝「既定」という概念が無く、意図せぬ全社キー喪失を避けるため個別に
// キー欄をクリアしてもらう。真偽値トグル（personal_api_keys_allowed／user_api_keys_allowed）は
// 実効既定と同値の明示 false を送る（バックエンドの一括削除・失効は値が厳密に false になった
// ときだけ発火し、null（未指定へ戻す）ではこれらの副作用が起きない）。
async function _putResetBody(body, resEl) {
  // 5タブ共通のリセット送信口＝どのタブのリセットも「保存」と同じ書換え操作なので、削除待ちの
  // 応答を無効化する（U-6 と同型の invalidate・プロバイダタブ以外のリセットでも保存済みの
  // クラウドキー削除待ちが巻き戻し得るため、個別のリセット関数ではなくここに一本化する）。
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
// `_mcState`（使えるモデルタブの未保存編集を含みうる）は使わず、`_mcConfiguredRaw`（管理者が
// 実際に保存済みの生値）から openai.embed キーだけを取り除く（＝そのセルだけ未設定へ戻し、
// 他の全プロバイダ・全用途は保存済みの構成のまま・他タブの未保存編集は一切含まない）。
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
    // 埋め込みのデプロイ名（model_catalog.openai.embed）だけ組み込み既定へ戻す（他タブの
    // 未保存編集は同送しない・上の _configuredRawWithoutOpenaiEmbed 参照）。
    model_catalog: _configuredRawWithoutOpenaiEmbed(),
    // SC-6c: 調べる深さの基準値も「プロバイダ＋接続先」タブの一部（このタブの既定に戻す対象）。
    depth_base_max_turns: null,
    depth_base_grep_max_hits: null,
    depth_base_qa_max_hits: null,
    depth_base_read_window: null,
    depth_base_impact_depth: null,
    depth_base_troubleshoot_depth: null,
    depth_base_codex_reasoning: null,
    chat_examples: null,
  };
  let view;
  try { view = await _putResetBody(body, resEl); } catch (e) { return; }
  _view = view;
  // 埋め込みセルだけ最新の実効値へ同期する（「使えるモデル」タブの他セル・他の未保存編集には
  // 触れない）。列プロバイダは応答の cloud_provider（このリセットで変わった）へ追従させる。
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

async function resetModelsTab() {
  const resEl = 'tab-reset-res-models';
  // 対象キーのみ・null で送る（他タブの未保存編集は一切含めない）。model_catalog を丸ごと
  // 既定へ戻すため、プロバイダタブ側に表示されている埋め込みデプロイ名も一緒に戻る
  // （同じキーの一部＝このリセットが正しく対象にする範囲）。
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
    // BUDGET-1（§3.4）: 検索の情報量予算も「取り込み」タブの一部（このタブの既定に戻す対象）。
    agentic_budget_per_result: null, agentic_budget_total: null,
    // BUDGET-2（§3.4）: モデルが一度に読める量の登録表も同じタブの一部。
    model_context_windows: null,
  };
  let view;
  try { view = await _putResetBody(body, resEl); } catch (e) { return; }
  _view = view;
  renderIngestTab(view);
  applyConfigChangedHighlights(view);
  refreshTabDots();
  _markResetOk(resEl);
}

async function resetUsageTab() {
  const resEl = 'tab-reset-res-usage';
  const body = { usage_chat_provider: null };
  let view;
  try { view = await _putResetBody(body, resEl); } catch (e) { return; }
  _view = view;
  renderUsageTab(view);
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
  provider: resetProviderTab, models: resetModelsTab, ingest: resetIngestTab, usage: resetUsageTab,
  extkeys: resetExtKeysTab,
};
document.querySelectorAll('[data-reset-tab]').forEach((b) => {
  const handler = _TAB_RESET_HANDLERS[b.dataset.resetTab];
  if (handler) b.addEventListener('click', handler);
});

// チャットの質問例カードだけの「未設定に戻す」（タブ全体のリセットとは別に、この項目単体を
// 未設定へ戻す・_putResetBody を共用してクラウドキー削除待ちの無効化等の共通処理に乗せる）。
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
const TAB_KEYS = ['provider', 'models', 'ingest', 'usage', 'extkeys'];
// 埋め込みタブ（管理系ページを iframe で表示・UI-TABS2・2026-09-04）。設定タブと違い保存対象が
// ないため TAB_DIRTY を持たない＝ここへの切替に未保存確認は挟まない（画面を離れないため不要）。
const EMBED_TAB_KEYS = ['users', 'usage-page', 'audit', 'status'];
const ALL_TAB_KEYS = TAB_KEYS.concat(EMBED_TAB_KEYS);
function activateTab(tabKey, opts) {
  if (!ALL_TAB_KEYS.includes(tabKey)) tabKey = TAB_KEYS[0];
  ALL_TAB_KEYS.forEach((k) => {
    const btn = document.querySelector(`.tab-btn[data-tab="${k}"]`);
    const panel = $('tabpanel-' + k);
    const active = k === tabKey;
    if (btn) btn.setAttribute('aria-selected', String(active));
    if (panel) panel.hidden = !active;
  });
  if (EMBED_TAB_KEYS.includes(tabKey)) loadEmbedFrame(tabKey);
  if (!opts || opts.updateHash !== false) location.hash = tabKey;
}
// 埋め込みタブの iframe は遅延ロード: data-src を初回選択時にだけ src へ移す（未選択のうちは
// src 属性を持たない＝ページを開いた瞬間に4画面分のリクエストが飛ぶのを避ける）。
function loadEmbedFrame(tabKey) {
  const frame = $('embed-frame-' + tabKey);
  if (frame && !frame.getAttribute('src') && frame.dataset.src) frame.setAttribute('src', frame.dataset.src);
}
document.querySelectorAll('.tab-btn[data-tab]').forEach((btn) =>
  btn.addEventListener('click', () => activateTab(btn.dataset.tab)));
window.addEventListener('hashchange', () => activateTab(location.hash.replace('#', ''), { updateHash: false }));

// タブごとの未保存インジケータ。render() 時点の基準値と今の値が異なるタブだけ丸印を出す
// （埋め込みデプロイ名欄は物理的に「プロバイダ＋接続先」タブにあるため、そちらで判定する）。
const TAB_DIRTY = {
  provider: () => cloudChanged() || ollamaAllowlistChanged() || webhookAllowlistChanged()
    || openaiEndpointChanged() || mcEmbedChanged()
    || depthProfileChanged() || chatExamplesChanged(),
  models: () => mcCatalogChangedExcludingEmbed(),
  ingest: () => armsChanged() || legacyChanged() || vlmChanged() || ragLlmRenderChanged()
    || agenticBudgetChanged() || modelWindowsTableChanged(),
  usage: () => usageChatProviderChanged(),
  extkeys: () => extKeysAllowedChanged() || extKeysQuotaChanged() || extKeysResearchProviderChanged(),
};
function refreshTabDots() {
  TAB_KEYS.forEach((k) => {
    const dot = $('tab-dot-' + k);
    if (dot) dot.hidden = !TAB_DIRTY[k]();
  });
}

// 接続先関連（種別ラジオ・埋め込みデプロイ名）の変更監視は、各要素が DOM 上どこに置かれているか
// （「詳細」折りたたみの中かどうか）に依存せず、対象の id/属性だけで判定する（構造に依存すると、
// 要素を後から折りたたみへ移した際に監視漏れが起きる）。埋め込み欄は 'input'（1文字ごと）ではなく
// 'change'（確定時＝blur/Enter）で反映する＝入力途中の値が allowed 一覧へ蓄積される事故を防ぐ。
// このブロックは下の refreshTabDots 登録より**前**に置く: どちらも document 自身に直接束縛する
// リスナーのため、子孫要素向けの委譲リスナーと違ってバブリングの「常に最後に走る」保証が効かず、
// 同一 target・同一イベント種別では登録順がそのまま実行順になる（先に状態を更新してから
// refreshTabDots で読ませる必要がある）。
document.addEventListener('change', (e) => {
  if (e.target.matches('input[data-openai-endpoint-kind]')) updateOpenaiEndpointFieldsVisibility();
  if (e.target.id === 'openai-endpoint-embed-deployment') applyEmbedDeploymentFieldEdit();
});

// 各カードの change/input/click リスナーは対象の要素（子孫）に直接束縛されているため、バブリングで
// document まで届いた時点で状態は既に確定している＝登録順に関係なく常に最後に走る（ただし
// document 自身に直接束縛したリスナー同士は登録順が優先されるため、上のブロックは必ずこれより
// 前に置く）。document を対象にするのは、モーダル（#mc-overlay・#ek-overlay）が #main-content の
// 外（body直下）にあり、そこでの操作（例: 使えるモデルの「反映」ボタン）も拾う必要があるため。
['input', 'change', 'click'].forEach((evt) => document.addEventListener(evt, refreshTabDots));

// 既定から変えた項目だけ強調する（5タブすべて）。組み込み既定の値そのもの（openai・false・
// localhost 既定 URL・空の allowlist・kind=openai）と比較する。使えるモデルの表は
// セル単位で `mcCellChanged()` が別途強調する。
function applyConfigChangedHighlights(view) {
  const mark = (el, changed) => { if (el) el.classList.toggle('cfg-changed', !!changed); };
  // プロバイダ＋接続先
  const cloud = view.cloud || {};
  const oe = (view.openai_endpoint || {}).configured || {};
  mark($('cloud-provider-radios'), (cloud.provider || 'openai') !== 'openai');
  mark($('personal-keys-allowed'), !!cloud.personal_api_keys_allowed);
  mark($('web-search-allowed'), !!cloud.web_search_allowed);
  mark($('cloud-ollama-url'), !!(cloud.ollama_url && cloud.ollama_url !== 'http://localhost:11434'));
  mark($('cloud-ollama-allowlist'), !!(view.ollama_allowlist && (view.ollama_allowlist.configured || []).length));
  mark($('webhook-allowlist'), !!(view.webhook_allowlist && (view.webhook_allowlist.configured || []).length));
  mark($('openai-endpoint-radios'), !!(oe.kind && oe.kind !== 'openai'));
  mark($('openai-endpoint-base-url'), !!oe.base_url);
  mark($('openai-endpoint-auth-header'), !!(oe.auth_header && oe.auth_header !== 'bearer'));
  mark($('openai-endpoint-api-version'), !!oe.api_version);
  // SC-6c: 調べる深さの基準値（標準時の値）。
  const dp = view.depth_profile || {};
  _DEPTH_BASE_FIELDS.forEach(({ view: vk, id }) => {
    const info = dp[vk] || {};
    mark($(id), info.effective !== info.default);
  });
  const reasoning = dp.codex_reasoning || {};
  mark($('depth-base-codex-reasoning'), reasoning.effective !== reasoning.default);
  const ce = view.chat_examples || {};
  mark($('chat-examples-card'), (ce.configured != null));
  // 取り込み
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
  // BUDGET-1（§3.4）: 検索の情報量予算（1件あたり／累計）。
  const ab = view.agentic_budget || {};
  _AGENTIC_BUDGET_FIELDS.forEach(({ view: vk, id }) => {
    const info = ab[vk] || {};
    mark($(id), info.effective !== info.default);
  });
  // 利用量
  const uc = view.usage_chat || {};
  mark($('usage-chat-ai-card'), !!uc.effective && uc.effective !== (uc.default || 'openai'));
  mark($('ext-keys-user-allowed'), !!(view.ext_keys && view.ext_keys.user_api_keys_allowed));
  const quota = (view.ext_keys || {}).daily_quota_default || {};
  mark($('ext-keys-user-quota-default'), quota.effective !== quota.default);
  const rdp = (view.ext_keys || {}).research_default_provider || {};
  mark($('ext-research-default-provider'), rdp.effective !== rdp.default);
}

// #cloud-provider-radios も render() の度に innerHTML が入れ替わるため委譲リスナー1本にする。
// プロバイダを切り替えたら、キー欄は選択中プロバイダに合わせて再描画し直す
// （前のプロバイダ向けに入力しかけていたキー値を、切替後のプロバイダへ誤って送らないため）。
const _cloudRadios = $('cloud-provider-radios');
// `click`（`change` ではない）で明示操作を記録する: 既に選択中の radio を再クリックしても
// `change` は発火しないため（既定表示のまま明示的にクリックして確定する操作を拾い漏らす・
// `_cloudProviderTouched` docstring 参照）。
if (_cloudRadios) _cloudRadios.addEventListener('click', (e) => {
  if (e.target.matches('input[data-cloud-provider]')) _cloudProviderTouched = true;
});
if (_cloudRadios) _cloudRadios.addEventListener('change', (e) => {
  if (!e.target.matches('input[data-cloud-provider]')) return;
  // プロバイダ切替＝削除待ちの応答はもう今の操作対象ではない（前のプロバイダに対する削除結果
  // 表示「✓ 削除しました」等が新しいプロバイダの結果に見えてしまう分も _invalidateCloudKeyClear
  // 内でまとめてクリアする）。
  _invalidateCloudKeyClear();
  renderCloudKeyBlock((_view && _view.cloud) || {});
  // 使えるモデル表の1列目（選択中のクラウド AI）も切替に追従させる（保存前でも見た目を一致させる）。
  _mcCloudProvider = selectedCloudProvider();
  renderModelCatalogTable();
});
const _cloudKeyTest = $('cloud-key-test');
if (_cloudKeyTest) _cloudKeyTest.addEventListener('click', testCloudKey);
const _cloudKeyClear = $('cloud-key-clear');
if (_cloudKeyClear) _cloudKeyClear.addEventListener('click', clearCloudKey);
const _cloudKeyInput = $('cloud-key');
// キー入力中＝これから保存/削除いずれかの新しい操作が起きうる状態。古い削除応答が入力中の
// 値の解釈に影響しないよう、入力の時点で世代を進める。
if (_cloudKeyInput) _cloudKeyInput.addEventListener('input', () => _invalidateCloudKeyClear());

const _openaiEndpointTest = $('openai-endpoint-test');
if (_openaiEndpointTest) _openaiEndpointTest.addEventListener('click', testOpenaiEndpoint);

// 使えるモデル（#model-catalog-table は render() の度に丸ごと innerHTML が入れ替わるため、
// 常に存在するコンテナへの委譲リスナー1本にする＝ #arms-list と同じ流儀）。
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
// ⑤: VLM 設定の provider 変更時はキー未設定案内だけ即時更新する（フォーム値は再描画しない＝
// 入力中の値を消さない）。
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
// settings.html と同じく Ctrl+S（Cmd+S）でも保存。ただし API キー発行モーダルが開いている間
// （idle・issuing・キー表示中のいずれの状態でも）は、ブラウザの既定動作（「ページを保存」）
// だけを止めて実際の保存は呼ばない——発行モーダルの操作中に画面全体の設定 PUT が意図せず
// 走ってしまう事故を防ぐ（モーダルの状態機械とは無関係に、開いている間は常に無効化する）。
document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
    e.preventDefault();
    const ekOverlay = $('ek-overlay');
    if (ekOverlay && ekOverlay.classList.contains('open')) return;
    if (!$('save').disabled) save();
  }
});
function applyThemeIcon() { const b = $('themebtn'); if (b) b.textContent = document.documentElement.dataset.theme === 'dark' ? '☀️' : '🌙'; }
document.addEventListener('click', (e) => {
  if (!e.target.closest('#themebtn')) return;
  const d = document.documentElement, next = d.dataset.theme === 'dark' ? 'light' : 'dark';
  d.dataset.theme = next; localStorage.setItem('sherpa-theme', next); applyThemeIcon();
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
  // URL ハッシュに選択中のタブを記憶する（無ければ既定＝プロバイダ＋接続先）。
  activateTab(location.hash.replace('#', ''), { updateHash: false });
  load();
  loadExtKeys();   // 独立取得（GET /ext/v1/admin/keys は /admin/settings と別エンドポイント）
})();
