// 設定ページ（スタンドアロン・全ページの上部ナビ「設定」から到達）。GET/PUT /settings＋POST /settings/test。
// ここで設定するのは「チャットに使う AI」の選択と、許可されている場合の自分専用の API キー。
// 機能ごとの AI・モデル名の選択は管理者の「システム管理」（使えるモデル）で行う。
// API キーは書込専用（入力時のみ送信・再表示しない）。
'use strict';
const $ = Sherpa.$;                       // 共通ユーティリティ（nav.js・RV DRY）

const DEFAULT_SYS = '憶測で回答しないでください。不明な点は不明と伝えてください。根拠のある情報と推測を明確に分けてください。'
  + '事実確認が必要な内容については、確認できた情報をもとに回答してください。回答では、結論・理由・補足を分かりやすく整理してください。';

// RV MED（F5・2026-07-16再検証）: ページ初期化時点（fetch/verify/load 実行前）の HTML そのままの
// <option>（静的 choices＝バックエンドの BEDROCK_MODEL_CHOICES と一致）を捕捉しておく。これらは
// 「利用可能なモデルを取得」の結果に含まれていなくても常にバックエンドが受理できる値なので、
// select の再構築（setBedrockModelOptions）で欠落させない・legacy 扱いにしない（実害の再現条件＝
// 静的 Global を選択中に fetch すると Global が一覧に無いために legacy に転落し、保存が
// bedrock_model:null 送信になっていた）。settings.js は body 末尾で読み込まれる（静的 <option> は
// この時点で既に DOM に存在する）。
const STATIC_BEDROCK_OPTIONS = (() => {
  const sel = document.getElementById('bmodel');
  const map = new Map();
  if (sel) Array.from(sel.options).forEach((o) => map.set(o.value, o.textContent));
  return map;
})();

// RV MED（F5）: クライアント側の「known（＝legacy 扱いにしない）」集合。静的 choices を種に、
// (a) 列挙（fetch）結果 (b) verify 成功 (c) サーバが bedrock_model_known:true と言った保存値、を
// このセッション中に積み上げる。あくまで表示分類用のヒント（サーバ側 allowlist の写しではない・
// 保存可否の最終判定は常にサーバの `_bedrock_model_id_valid` が行う）。
const knownBedrockModels = new Map(STATIC_BEDROCK_OPTIONS);

// Bedrock モデル <select> に値が無ければ選択肢を追加してから選択する（textContent のみなので
// XSS 安全）。`known`/`label` を明示された場合（サーバの `bedrock_model_known`/`bedrock_model_label`・
// load() から渡す）はそれに従う。省略時（setBedrockModelOptions が「今の選択を維持する」ために呼ぶ
// 場合）はクライアント側 known 集合で分類する。追加した option は data-dynamic を立てる（静的
// choices ではない＝load() が再構築のたびに除去する対象・F7）。
function ensureBedrockModelOption(value, known, label) {
  const sel = $('bmodel');
  if (!sel || !value) return;
  const exists = Array.from(sel.options).some((o) => o.value === value);
  if (exists) return;
  const isKnown = known !== undefined ? known : knownBedrockModels.has(value);
  const opt = document.createElement('option');
  opt.value = value;
  opt.dataset.dynamic = '1';
  if (isKnown) {
    opt.textContent = label || knownBedrockModels.get(value) || value;
  } else {
    opt.textContent = `現在の設定: ${value}（旧設定）`;
    opt.dataset.legacy = '1';   // 保存時に allowlist 外の値を送らないようにする目印
  }
  sel.appendChild(opt);
}

// 選択中が「旧設定」プレースホルダなら bedrock_model は null（JSON null として送信され、
// サーバ側は「未指定＝変更しない」として無視する）。allowlist 外の値をそのまま再送すると
// PUT /settings が 422 を返し、他フィールドの保存まで失敗するため。
function selectedBedrockModel() {
  const sel = $('bmodel');
  const opt = sel && sel.options[sel.selectedIndex];
  return (opt && opt.dataset.legacy) ? null : sel.value.trim() || null;
}

// S6: 「利用可能なモデルを取得」の結果で <select> の選択肢を丸ごと置き換える。今選ばれている値は
// （新しい選択肢に無ければ ensureBedrockModelOption の分類で）維持する。
// RV MED（F5・2026-07-16再検証）: 静的 choices（STATIC_BEDROCK_OPTIONS）は列挙結果に含まれていなくても
// 常に残す（バックエンドは常に受理するため legacy 扱いにする理由が無い）。列挙結果は known 集合へ
// 積み上げる（このセッション内では以後も known 扱い＝再度 fetch し直さなくても再分類できる）。
// RV LOW（N4・2026-07-16 Codex RV 3巡目再検証）: 列挙結果に静的 choices の ID が混ざっていても、
// この行では option を**作らない**（下の「静的 choices の再追加」ループが正典ラベルで必ず1回だけ
// 描画する＝untagged な fetch 由来の行が静的ラベルを恒久的に上書きしたままにならない）。列挙結果
// 内の重複 ID もここでスキップする（同じ id の option が複数生成されるのを防ぐ）。
function setBedrockModelOptions(models) {
  const sel = $('bmodel');
  if (!sel || !Array.isArray(models) || !models.length) return;
  const current = sel.value;
  sel.innerHTML = '';
  const seen = new Set();
  for (const m of models) {
    if (seen.has(m.id) || STATIC_BEDROCK_OPTIONS.has(m.id)) continue;
    seen.add(m.id);
    const opt = document.createElement('option');
    opt.value = m.id;
    opt.textContent = m.label || m.id;   // サーバ整形済みラベル（textContent のみ＝XSS 安全）
    opt.dataset.dynamic = '1';
    knownBedrockModels.set(m.id, opt.textContent);
    sel.appendChild(opt);
  }
  // 静的 choices は列挙結果の有無・重複に関係なく、ここで必ず1回だけ正典ラベルで描画する
  // （data-dynamic は立てない＝常設・load() の再構築でも消えない）。
  for (const [value, label] of STATIC_BEDROCK_OPTIONS) {
    const opt = document.createElement('option');
    opt.value = value;
    opt.textContent = label;
    sel.appendChild(opt);
  }
  ensureBedrockModelOption(current);
  if (current) sel.value = current;
}

// カタログ参照の <select> を組み立てる汎用ヘルパー（自由入力ではない）。`info`（`{allowed,
// default}` 形）から選択肢を組み立て、`current`（保存済みの値）が一覧外なら「現在の値（一覧外）」
// を選択肢へ追加して選択状態にする（移行期の寛容・保存時は弾かない＝サーバ側と同じ方針）。空の
// 選択肢（値=""）は「管理者の既定を使う」＝保存すると明示的に空文字が送られ、既存の保存値を
// 既定へ戻す（save() 参照）。全て textContent のみで組み立てる（XSS 安全）。戻り値は「現在の値が
// 一覧外だった（警告表示が必要）」かどうか。現在の唯一の呼び出し元は `fillOllamaUrlSelect`
// （個人設定に残る Ollama 接続先の選択・モデル名欄は個人設定に無い）。
function fillModelSelect(id, info, current) {
  const sel = $(id);
  if (!sel) return false;
  const i = info || { allowed: [], default: '' };
  const allowed = i.allowed || [];
  sel.textContent = '';
  const optDefault = document.createElement('option');
  optDefault.value = '';
  optDefault.textContent = i.default ? `管理者の既定を使う（${i.default}）` : '管理者の既定を使う';
  sel.appendChild(optDefault);
  allowed.forEach((m) => {
    const opt = document.createElement('option');
    opt.value = m;
    opt.textContent = m;
    sel.appendChild(opt);
  });
  const warn = !!current && !allowed.includes(current);
  if (warn) {
    const opt = document.createElement('option');
    opt.value = current;
    opt.textContent = `現在の値（一覧外）: ${current}`;
    opt.dataset.legacy = '1';
    sel.appendChild(opt);
  }
  sel.value = current || '';
  return warn;
}
// Ollama 接続先は管理者の許可ホスト一覧（`ollama_url_choice`＝ model_catalog のフィールドと同じ
// `{allowed, default}` 形）から選ぶ（自由入力ではない）。`allowed` は完全 URL（scheme 込み）を保持
// する（host:port へ丸めると https が http に化ける・IPv6 の角括弧が失われる等の劣化が起きるため・
// サーバ側 `system.py::_ollama_url_choice` 参照）。空の選択肢（値=""）は「管理者の既定を使う」＝
// fillModelSelect と同じ規約（save() が明示的に空文字を送る）。
function fillOllamaUrlSelect(info, current) {
  return fillModelSelect('ourl', info, current);
}
function showModelWarn(warnId, warn, label) {
  const el = $(warnId);
  if (!el) return;
  el.hidden = !warn;
  if (warn) el.textContent = `現在の設定（${label}）は管理者の一覧にありません。選び直すと消えます。`;
}

// 実行構成（2026-08-15・agent_constructs）: 選択肢はサーバが返す `constructs_available` だけを描画する。
// env で無効な AI は一覧に入らない＝画面に出ない（textContent のみ＝XSS 安全）。
// 各 option には保存すべき設定値（agent / codex_model_provider）を data 属性で持たせ、save() が
// 「実際に選び直した時だけ」そのまま送る（agentConstructChanged 参照）。保存済みの実値
// （currentAgent/currentCodexModelProvider）が一覧に無い場合（例: SHERPA_EXTRA_AGENTS で有効化して
// いない・無効化された頭脳）は、fillModelSelect と同じ「一覧外」プレースホルダで実値を保持する
// （先頭候補へ黙って差し替えない＝ユーザーが明示的に選び直すまで実際の agent を失わない）。
let _constructs = [];
let _agentBaseline = { agent: '', codexModelProvider: '' };
function _selectedAgentDataset() {
  const opt = $('agent').selectedOptions[0];
  return {
    agent: (opt && opt.dataset && opt.dataset.agent) || '',
    codexModelProvider: (opt && opt.dataset && opt.dataset.codexModelProvider) || '',
  };
}
// 実行構成の選択が render() 時点の基準値（_agentBaseline）から変わっているか。値を元に戻せば
// 送信対象からも外れる（触っただけで戻していない項目を誤って送らない・admin-settings.js の
// ダーティ判定と同型）。基準値が null（不明＝直前の保存が通信例外/5xx で結果不確定だった）の
// ときは値の比較をせず常に真を返す＝次の保存では必ず agent を送り直す（save() 参照）。
function agentConstructChanged() {
  if (_agentBaseline === null) return true;
  const cur = _selectedAgentDataset();
  return cur.agent !== _agentBaseline.agent || cur.codexModelProvider !== _agentBaseline.codexModelProvider;
}
function renderConstructOptions(choices, currentId, currentAgent, currentCodexModelProvider) {
  const sel = $('agent');
  if (!sel) return;
  _constructs = choices || [];
  sel.textContent = '';
  _constructs.forEach((c) => {
    const opt = document.createElement('option');
    opt.value = c.id;
    opt.textContent = c.label || c.id;
    opt.dataset.agent = c.agent || '';
    opt.dataset.codexModelProvider = c.codex_model_provider || '';
    sel.appendChild(opt);
  });
  const ids = _constructs.map((c) => c.id);
  if (!ids.includes(currentId) && currentAgent) {
    const opt = document.createElement('option');
    opt.value = currentId || currentAgent;
    opt.textContent = `現在の設定（一覧外）: ${currentAgent}`;
    opt.dataset.agent = currentAgent;
    opt.dataset.codexModelProvider = currentCodexModelProvider || '';
    opt.dataset.legacy = '1';
    sel.appendChild(opt);
    sel.value = opt.value;
  } else {
    sel.value = ids.includes(currentId) ? currentId : (ids[0] || '');
  }
  _agentBaseline = _selectedAgentDataset();
  showConstructHint();
}
function showConstructHint() {
  const opt = $('agent').selectedOptions[0];
  const cur = _constructs.find((c) => c.id === $('agent').value);
  let hint = cur ? (cur.hint || '') : '';
  if (!cur && opt && opt.dataset && opt.dataset.legacy) {
    hint = 'この環境では選べない設定です（選び直すと元に戻せません）。';
  }
  if ($('agent-hint')) $('agent-hint').textContent = hint;
}
// 選択肢と入力欄はセットで出す（決定 2026-08-15）: env で有効化していない AI は
// キーの入力欄を出さない。
function applyEnabledAgents(constructs) {
  const enabled = new Set((constructs || []).map((c) => c.agent));
  ['gemini', 'bedrock'].forEach((name) => {
    const grp = $('grp-' + name);
    if (grp) grp.hidden = !enabled.has(name);
  });
}

// 個人キーの入力欄は「個人キーが許可されている（A6）」かつ
// 「このクラウド AI が現在選択されている（A7）」の両方を満たすときだけ見せる。片方でも欠けたら
// 隠して理由別の注記に差し替える（settings_put も両方を 422 で拒否する・こちらは UI 側の対応）。
const _CLOUD_KEY_PROVIDER = { okey: 'openai', gkey: 'gemini', bkey: 'bedrock' };
function applyCloudKeyVisibility(personalAllowed, cloudProvider) {
  Object.keys(_CLOUD_KEY_PROVIDER).forEach((prefix) => {
    const selected = _CLOUD_KEY_PROVIDER[prefix] === cloudProvider;
    const visible = personalAllowed && selected;
    const row = $(prefix + '-row');
    const note = $(prefix + '-disabled-note');
    if (row) row.hidden = !visible;
    if (note) {
      note.hidden = visible;
      if (!visible) {
        note.textContent = !personalAllowed
          ? 'キーは管理者が設定します（このパソコン・利用者ごとの入力はできません）。'
          : 'このクラウド AI は現在選択されていません（管理画面で切り替えると入力できます）。';
      }
    }
  });
}

async function fetchBedrockModels() {
  const btn = $('bmodel-fetch');
  const res = $('bmodel-fetch-res');
  btn.disabled = true;
  res.className = 'tres muted';
  res.innerHTML = '<span class="loading-inline" role="status"><span class="spinner spinner-sm"></span><span>取得中...</span></span>';
  try {
    const r = await fetch('/settings/bedrock-models');
    let d = null;
    try { d = await r.json(); } catch (_) { d = null; }
    if (r.ok && d && d.models && d.models.length) {
      setBedrockModelOptions(d.models);
      res.className = 'tres ok';
      res.textContent = `✓ ${d.models.length}件のモデルを取得しました`;
    } else {
      res.className = 'tres danger';
      // RV「バッチ2」1番: d.error はサーバの固定日本語理由（S6・200 応答）、d.detail は認証エラー等
      // FastAPI 既定形状（非 200 応答）。どちらも無い（＝ネットワーク層や想定外の応答形状）場合だけ
      // HTTP status を出す最終フォールバックにする（以前は常に generic 文言に落ちていた疑い）。
      const reason = (d && (d.error || d.detail)) || `取得に失敗しました（HTTP ${r.status}）`;
      res.textContent = '✗ ' + reason;
    }
  } catch (e) {
    res.className = 'tres danger';
    res.textContent = '✗ 通信エラーが発生しました';
  } finally {
    btn.disabled = false;
  }
}

// バッチ2・1番（2026-07-03）: 検証つき手動追加。<select> に無ければ1件追加して選択状態にする
// （サーバ整形済み label のみ・textContent なので XSS 安全）。既にある場合は選択するだけ。
// RV MED（2026-07-15・核心バグ修正）: 既存 option が「旧設定」（data-legacy）のまま残っていると、
// 同じ ID を verify で検証し直して選び直しても `selectedBedrockModel` が legacy 扱いで null を返し、
// 保存が「成功」表示なのに実際は bedrock_model が送信されない（サーバは直前の値を保持し続ける）。
// 検証成功＝正当な値になった、ということなので legacy マーカーを外し、表示ラベルも検証結果に更新する。
// F5（2026-07-16再検証）: 検証成功は known 集合にも積む（以後の setBedrockModelOptions 再分類でも
// legacy に転落しなくなる）。
// RV LOW（R4-4・2026-07-16 Codex RV 4巡目再検証）: id が静的 choices（STATIC_BEDROCK_OPTIONS）の
// 1つの場合は**選択するだけ**にする（textContent・dataset には触れない）。静的 option は常に
// バックエンドで保存可能＝verify する意味自体は無いが、モデルIDを直接入力欄に静的な値をたまたま
// 入力して検証しても、正典ラベル（例:「Claude Haiku 4.5（JP 推論プロファイル・既定）」）が
// verify 応答の汎用ラベル（例:「...（検証済み）」）で恒久的に上書きされてしまう表示退行を防ぐ。
// knownBedrockModels の静的エントリも同じ理由で上書きしない。
function addOrSelectBedrockModelOption(id, label) {
  const sel = $('bmodel');
  if (!sel) return;
  const isStatic = STATIC_BEDROCK_OPTIONS.has(id);
  if (!isStatic) knownBedrockModels.set(id, label || id);
  const existing = Array.from(sel.options).find((o) => o.value === id);
  if (existing) {
    if (!isStatic) {
      delete existing.dataset.legacy;
      existing.textContent = label || id;
    }
    sel.value = id;
    return;
  }
  const opt = document.createElement('option');
  opt.value = id;
  opt.textContent = label || id;
  if (!isStatic) opt.dataset.dynamic = '1';
  sel.appendChild(opt);
  sel.value = id;
}

async function verifyBedrockModel() {
  const input = $('bmodel-manual');
  const btn = $('bmodel-verify');
  const res = $('bmodel-verify-res');
  const modelId = (input.value || '').trim();
  if (!modelId) {
    res.className = 'tres danger';
    res.textContent = '✗ モデルIDを入力してください';
    return;
  }
  btn.disabled = true;
  res.className = 'tres muted';
  res.innerHTML = '<span class="loading-inline" role="status"><span class="spinner spinner-sm"></span><span>検証中...</span></span>';
  try {
    const r = await fetch('/settings/bedrock-models/verify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model_id: modelId }),
    });
    let d = null;
    try { d = await r.json(); } catch (_) { d = null; }
    if (r.ok && d && d.ok) {
      addOrSelectBedrockModelOption(d.id, d.label);
      res.className = 'tres ok';
      res.textContent = `✓ 検証OK・選択肢に追加しました（保存するには下部の「保存」を押してください）`;
      input.value = '';
    } else {
      res.className = 'tres danger';
      const reason = (d && (d.error || d.detail)) || `検証に失敗しました（HTTP ${r.status}）`;
      res.textContent = '✗ ' + reason;
    }
  } catch (e) {
    res.className = 'tres danger';
    res.textContent = '✗ 通信エラーが発生しました';
  } finally {
    btn.disabled = false;
  }
}

// RV LOW（L3・2026-07-16 Codex RV 5巡目再検証）: 成否を boolean で返す（例外は投げない＝ページ初期化
// 時点の fire-and-forget 呼び出し `load()`（本ファイル末尾）を壊さないため）。呼び出し元（save()）は
// これを見て「保存はできたが再読込に失敗した」ことを利用者に伝え分ける。
// 接続先が OpenAI 直結（既定）以外のとき、短い注記を出す（Azure ＝ デプロイ名は管理者側の
// 設定・custom ＝ 接続先ホストのみ）。管理画面「接続先」欄（DB）の設定なのでここでは表示のみ・
// 入力は受け付けない。textContent のみ使う（host はサーバ由来だが念のため XSS 安全な代入に揃える＝
// 他の動的表示と同じ流儀）。
function renderOpenAIEndpointNote(s) {
  const el = $('openai-endpoint-note');
  if (!el) return;
  const kind = s.openai_endpoint_kind || 'openai';
  const host = s.openai_base_url_host || '';
  if (kind === 'azure') {
    // このページにモデル欄は無い（モデル名は個人設定に無く、管理者の「使えるモデル」で
    // 管理する）。Azure の「デプロイ名」もそちらで設定する。
    el.textContent = '接続先: Azure OpenAI' + (host ? '（' + host + '）' : '')
      + '。モデル（Azure の「デプロイ名」）は管理者の「使えるモデル」で設定されています。';
    el.hidden = false;
  } else if (kind === 'custom') {
    el.textContent = '接続先: ' + (host || 'カスタム（OpenAI 互換）');
    el.hidden = false;
  } else {
    el.hidden = true;
  }
}

// ===== 外部連携（自分の API キー）=====
// 管理者が「利用者のキー発行を許可する」を ON にしたときだけカードを表示する（個人キー許可
// トグルと同型の出し分け）。対象フォルダのスコープは常にサーバ側が本人のアクセス範囲へ強制
// するため、この画面では対象フォルダの入力は出さない（管理画面の発行フォームとは異なる・
// 個人向けは「発行者」列も不要＝常に自分自身）。
let _extKeys = [];
let _extKeysDailyQuotaDefault = null;   // load() が GET /settings から拾う既定/上限（発行欄のプレースホルダ用）

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
  const esc = Sherpa.esc, fmt = Sherpa.fmtDateTime;
  const rowsHtml = rows.map((r) => {
    const st = extKeyStatus(r);
    const revokeBtn = r.revoked_at ? ''
      : `<button class="mini ek-danger" type="button" data-ek-revoke="${r.id}">失効</button>`;
    // PART-6: Webhook 登録の有無（host:port のみ・secret は絶対に出さない）。
    const webhookText = r.webhook ? (r.webhook_host || '登録済み') : '—';
    return `<tr>`
      + `<td>${esc(r.label)}</td>`
      + `<td><code>${esc(r.key_prefix)}</code></td>`
      + `<td>${esc(fmt(r.created_at))}</td>`
      + `<td class="ek-muted">${r.last_used_at ? esc(fmt(r.last_used_at)) : '未使用'}</td>`
      + `<td>${r.call_count}</td>`
      + `<td class="ek-muted">${r.expires_at ? esc(fmt(r.expires_at)) : '無期限'}</td>`
      + `<td class="ek-muted">${esc(webhookText)}</td>`
      + `<td><span class="ek-badge ${st.cls}">${esc(st.label)}</span></td>`
      + `<td>${revokeBtn}</td>`
      + `</tr>`;
  }).join('');
  wrap.innerHTML = `<div style="overflow-x:auto"><table class="ek-table"><thead><tr>`
    + `<th>ラベル</th><th>キーの識別部分</th><th>作成日</th><th>最終利用</th>`
    + `<th>呼出数（30日）</th><th>期限</th><th>Webhook</th><th>状態</th><th></th></tr></thead>`
    + `<tbody>${rowsHtml}</tbody></table></div>`;
}

// 一覧 GET の世代番号（管理画面と同型）。後から発行したのに先に届いた新しい応答を、遅れて
// 届いた古い応答が上書きしてしまう事故を防ぐ。
let _ekListGen = 0;

async function loadExtKeys() {
  const myGen = ++_ekListGen;
  try {
    const d = await Sherpa.getJSON('/ext/v1/keys');
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
// 新しい操作のボタン状態を巻き戻す事故を構造的に防ぐ（管理画面と同型）。一覧の再取得
// （`loadExtKeys()`）は発行の成否判定から意図的に切り離す（await しない）。
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

// モーダルが開いている間、背後を `inert` にする（管理画面と同型・キーボード/クリックのどちらでも
// 背後に到達できなくする・`#toast` は通知の読み上げを妨げないため対象外）。
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
  $('ek-issue-form').hidden = false;
  $('ek-reveal').hidden = true;
  _ekClearRevealedKey();
  $('ek-copy-res').textContent = '';
  $('ek-issue-err').textContent = '';
  $('ek-label').value = '';
  $('ek-expires').value = '';
  $('ek-expires').min = _todayLocalDateStr();   // 過去日を選ばせない（サーバ側422と二重防御）
  $('ek-quota').value = '';
  $('ek-quota').placeholder = _extKeysDailyQuotaDefault
    ? `空欄で既定（${_extKeysDailyQuotaDefault}件）` : '';
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
  // 開く前にフォーカスがあった要素（通常は「発行」ボタン）へ復帰する。
  if (_ekOpenerEl && typeof _ekOpenerEl.focus === 'function') _ekOpenerEl.focus();
  _ekOpenerEl = null;
}

// POST の結果が不明（タイムアウト・通信断・不正な形の応答）なときの回復導線。専用エンドポイント
// （`POST /ext/v1/keys/recover`）へこの試行の `client_op_id` を渡し、サーバー側で「自分（本人）
// が発行操作を試みた・未失効の」キーを**単一の原子的操作**で照合・失効する（一覧取得→別
// リクエストで DELETE、という2段構成は隙があるため使わない・管理画面と同型）。POST が実際には
// まだコミットされていない競合を閉じるため、有界に再試行する（3回×2秒間隔）。`found: true` を
// 確認できた場合のみ「失効しました」と表示する（確認できなければ失敗を失敗として表示する）。
async function _ekRecoverFromAmbiguousIssue(myOp, clientOpId) {
  if (_ekActiveOp !== myOp) return;
  $('ek-issue-err').textContent = '発行が完了したか確認しています…';
  const attempts = 3;
  const gapMs = 2000;
  let outcome = 'not_found';
  for (let i = 0; i < attempts; i++) {
    if (_ekActiveOp !== myOp) return;
    try {
      const res = await Sherpa.api('POST', '/ext/v1/keys/recover',
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
  const body = { label };
  const expiresRaw = ($('ek-expires').value || '').trim();
  // `min` 属性は手入力・貼り付けで過去日を直接セットされると効かないため、送信前にも文字列
  // 比較（YYYY-MM-DD＝辞書順=時系列順）で確実に弾き、過去日では POST 自体を発生させない
  // （サーバ側422はあくまで最後の砦・管理画面と同型）。
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
    d = await Sherpa.api('POST', '/ext/v1/keys', body, { timeoutMs: 30000 });
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
// フォールバック）。キー本体・Webhook secret のどちらの「今だけ表示」欄でも使う共通処理
// （管理画面 admin-settings.js::_ekCopyTextTo と同型）。
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
// #ext-keys-list は loadExtKeys() が丸ごと innerHTML を入れ替えるため、常に存在するコンテナへの
// 委譲リスナー1本にする。
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
    await Sherpa.api('DELETE', `/ext/v1/keys/${id}`);
    await loadExtKeys();
  } catch (err) {
    window.alert('失効に失敗しました: ' + err.message);
    btn.disabled = false;
  }
});

// `search_helper` の保存値が既知の選択肢（''/ollama/openai）のどれとも一致しない場合、
// <select> は暗黙に先頭 option（''）を選んだことになり、後続の無関係な保存でその不正値が
// 黙って ''（使わない）へ上書きされてしまう（黙った上書きを防ぐ・Bedrock モデル select の
// legacy option と同じ手当て・`ensureBedrockModelOption` 参照）。
function ensureSearchHelperOption(value) {
  const sel = $('search_helper');
  if (!sel) return;
  Array.from(sel.querySelectorAll('option[data-legacy]')).forEach((o) => o.remove());
  if (!value) return;
  const exists = Array.from(sel.options).some((o) => o.value === value);
  if (exists) return;
  const opt = document.createElement('option');
  opt.value = value;
  opt.textContent = `現在の設定: ${value}（不正な値・設定画面で選び直してください）`;
  opt.dataset.legacy = '1';
  sel.appendChild(opt);
}
// 選択中が「不正な値」プレースホルダなら search_helper は null（JSON null として送信され、
// サーバ側は「未指定＝変更しない」として無視する）。不正値をそのまま再送すると PUT /settings が
// 422 を返し、他フィールドの保存まで失敗するため。
function selectedSearchHelper() {
  const sel = $('search_helper');
  const opt = sel && sel.options[sel.selectedIndex];
  return (opt && opt.dataset.legacy) ? null : (sel ? sel.value : '');
}

async function load() {
  try {
    const r = await fetch('/settings');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const s = await r.json();
    renderConstructOptions(s.constructs_available, s.construct_id, s.agent, s.codex_model_provider);
    applyEnabledAgents(s.constructs_available);
    applyCloudKeyVisibility(!!s.personal_api_keys_allowed, s.cloud_provider || 'openai');
    // 管理者が許可したときだけ「外部連携」カードを出す（個人キー許可トグルと同型の出し分け）。
    const extKeysCard = $('ext-keys-card');
    _extKeysDailyQuotaDefault = s.user_api_keys_daily_quota_default || null;
    if (extKeysCard) {
      extKeysCard.hidden = !s.user_api_keys_allowed;
      if (s.user_api_keys_allowed) loadExtKeys();
    }
    ensureSearchHelperOption(s.search_helper || '');
    $('search_helper').value = s.search_helper || '';
    // このページにモデル選択欄は無い。以前の画面で選んだ個人モデル指定が DB に残っていても、
    // 実行時解決はもう読まない（常に管理者の使えるモデル一覧の既定に従う）。GET /settings が
    // 返す search_helper_model の生値は不活性な旧データの表示用のみ（クリア送信はできない
    // ＝個人設定にこのフィールドは無い）。
    const legacyNote = $('search-helper-legacy-note');
    if (legacyNote) {
      if (s.search_helper_model) {
        legacyNote.hidden = false;
        legacyNote.textContent = '以前この画面で選んだモデルの指定は現在使われません'
          + '（管理者の既定が適用されます）。';
      } else {
        legacyNote.hidden = true;
      }
    }
    renderOpenAIEndpointNote(s);
    showModelWarn('ourl-warn', fillOllamaUrlSelect(s.ollama_url_choice, s.ollama_url), s.ollama_url);
    // RV MED（F7・2026-07-16再検証）: load() が何度走っても（reload だけでなく save() 内の自動
    // load() のように同一 DOM 内で繰り返し走る場合も含めて）「旧設定」option や、前回の fetch/verify
    // で追加された option が積み残らないよう、適用前に**静的 choices 以外の option を全て除去**して
    // から再構築する（data-dynamic が目印・F5 の STATIC_BEDROCK_OPTIONS/ensureBedrockModelOption/
    // setBedrockModelOptions 参照）。fetch 結果は再取得すれば戻る＝一方向の再分類の穴を根絶する
    // （legacy option だけを消していた旧実装は、fetch で追加された非 legacy option が残留しえた）。
    const bsel = $('bmodel');
    if (bsel) Array.from(bsel.querySelectorAll('option[data-dynamic]')).forEach((o) => o.remove());
    // F5: サーバが「known」と言った保存値は、以後このセッション内でも known 集合に積んでおく
    // （setBedrockModelOptions の「今の選択を維持する」再分類でも legacy に転落しないように）。
    // RV LOW（L4・2026-07-16 Codex RV 5巡目再検証）: ただし静的 choices の正典ラベルは上書きしない
    // （R4-4 で addOrSelectBedrockModelOption/setBedrockModelOptions 側は対応済みだったが、ここ
    // load() 自身の set() が保存値=静的IDの時にサーバのラベルで静的エントリを上書きしてしまう
    // 抜け穴が残っていた）。
    if (s.bedrock_model_known && !STATIC_BEDROCK_OPTIONS.has(s.bedrock_model)) {
      knownBedrockModels.set(s.bedrock_model, s.bedrock_model_label || s.bedrock_model);
    }
    ensureBedrockModelOption(s.bedrock_model, s.bedrock_model_known, s.bedrock_model_label);
    $('bmodel').value = s.bedrock_model || 'jp.anthropic.claude-haiku-4-5-20251001-v1:0';
    $('okey').value = '';
    $('okey').placeholder = s.openai_key_set ? '設定済み（変更する時だけ入力）' : '未設定（sk-...）';
    $('gkey').value = '';
    $('gkey').placeholder = s.gemini_key_set ? '設定済み（変更する時だけ入力）' : '未設定（AIza...）';
    $('bkey').value = '';
    $('bkey').placeholder = s.bedrock_key_set ? '設定済み（変更する時だけ入力）' : '未設定（Bedrock コンソールで発行）';
    $('sysprompt').value = s.system_prompt || '';
    return true;
  } catch (e) {
    $('msg').innerHTML = '<span class="danger">設定を読み込めませんでした</span>';
    return false;
  }
}

async function save() {
  $('save').disabled = true;
  $('msg').innerHTML = '<span class="loading-inline" role="status"><span class="spinner spinner-sm"></span><span>設定を保存中...</span></span>';
  const body = {
    search_helper: selectedSearchHelper(),
    // Ollama 接続先（select の値は完全 URL・空文字＝管理者の既定を使う）。モデル名は
    // 管理者の「使えるモデル」カタログに従う（このページからは変更しない）。
    ollama_url: $('ourl').value.trim(),
    bedrock_model: selectedBedrockModel(),
    system_prompt: $('sysprompt').value,
  };
  // 実行構成は「実際に選び直した」時だけ送る（触っていない・一覧外の現在値のままなら送らない）。
  // 常に送ると、一覧に無い保存値（env で無効化された頭脳等）が先頭候補へ黙って上書きされたり、
  // 未選択（自動選択）の状態がその時点の解決値で焼き付いてしまう（agentConstructChanged 参照）。
  if (agentConstructChanged()) {
    const ds = _selectedAgentDataset();
    body.agent = ds.agent;
    body.codex_model_provider = ds.codexModelProvider || null;
  }
  const k = $('okey').value.trim(); if (k) body.openai_api_key = k;    // 入力時のみ更新（書込専用）
  const gk = $('gkey').value.trim(); if (gk) body.gemini_api_key = gk;
  const bk = $('bkey').value.trim(); if (bk) body.bedrock_api_key = bk;
  try {
    let r;
    try {
      r = await fetch('/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    } catch (networkErr) {
      // 通信例外＝応答そのものが届かない＝サーバ側で実際にコミットされたかどうか分からない
      // （処理後、応答を書き出す前に接続が切れた可能性がある）。次回の保存が値の一致だけを見て
      // 送信を省略しないよう、基準値を「不明」（null）にする（agentConstructChanged 参照）。
      if ('agent' in body) _agentBaseline = null;
      throw networkErr;
    }
    if (!r.ok) {
      let detail = '';
      try { detail = (await r.json()).detail || ''; } catch (_) { /* body なし */ }
      // 5xx はサーバ側の処理結果が応答から確認できない（4xx＝明確な拒否＝未適用、とは扱いを
      // 変える）。次回保存では値の一致に関わらず必ず agent を送り直す（不明＝安全側）。
      if (r.status >= 500 && 'agent' in body) _agentBaseline = null;
      throw new Error(detail || ('保存に失敗しました (' + r.status + ')'));
    }
    // 実行構成を送信した場合、基準値もこの時点（PUT 成功が確定した瞬間）で送信済みの値へ進める
    // （直後の load()＝GET の成否を待たない）。load() 任せにすると、この PUT の直後に GET が失敗した
    // 場合に基準値が送信前の値のまま残り、その後「送信前の値へ選び直して保存」すると
    // agentConstructChanged() が差分なしと誤判定して agent を送らない＝直前の PUT で書き換わった
    // サーバ側の値がそのまま取り残される（load() が成功すればどのみち同じ値で上書きされる＝無害）。
    if ('agent' in body) {
      _agentBaseline = { agent: body.agent, codexModelProvider: body.codex_model_provider || '' };
    }
    // RV LOW（L3・2026-07-16 Codex RV 5巡目再検証→C2・6巡目再検証でスナップショット比較に是正）:
    // PUT 成功が確定した時点で、送信済みの書込専用キー入力欄をローカルでクリアする（load() の
    // 成否に関係なく）。ただし**送信時点の値（snapshot: k/gk/bk）とまだ一致している時だけ**クリア
    // する。素朴に無条件でクリアすると、「キー A を送信 → この PUT が保留中の間に利用者がキー B へ
    // 打ち直す → PUT 成功・reload 失敗」という順序で、まだ送信していない B までここで消えてしまう
    // （利用者は B を送ったつもりで実際には A が保存され、かつ入力欄も空になって何が起きたか
    // 分からなくなる）。フィールドごとに「今の値が snapshot と同じか」を見てから消す。
    if ($('okey').value.trim() === k) $('okey').value = '';
    if ($('gkey').value.trim() === gk) $('gkey').value = '';
    if ($('bkey').value.trim() === bk) $('bkey').value = '';
    // PUT 成功が確定した時点で（load()＝GET の成否を待たず）基準値を更新する。load() の成否でしか
    // 更新しないと、PUT 成功→直後の GET 失敗の順で再読込が失敗した場合に、次の保存が古い基準値と
    // 比較してしまう（既に反映済みの変更を再度「変わった」と誤検知する）。
    // RV LOW（R4-3・2026-07-16 Codex RV 4巡目再検証）: load() の完了を待ってから「保存しました」表示
    // ＋保存ボタンの再有効化を行う。以前は load() を待たずに成功表示・再有効化していたため、
    // サーバの応答が遅い時に、ユーザーが「保存しました」を見て安心して新しい入力（書込専用キー等）
    // を始めてしまうと、遅れて完了した load() のフィールド再描画（$('bmodel').value=... 等）が
    // その新しい入力を上書きしてしまう競合があった。
    // RV LOW（L3・2026-07-16 Codex RV 5巡目再検証）: PUT 成功→GET/load() 失敗（ネットワーク断・
    // 500 等）を load() が内部で握りつぶすと、save() がそのまま「保存しました」を表示してしまい、
    // 利用者は画面の情報が古いままなのに気付けない。load() の成否（boolean）を見て、失敗時は
    // 「保存はできたが再読込に失敗した」ことが分かる表示にする（load() 自身が既に
    // 「設定を読み込めませんでした」を $('msg') に出しているので、それを追記の形で残す）。
    const loaded = await load();
    if (loaded) {
      $('msg').innerHTML = '<span class="ok">✓ 保存しました</span>';
    } else {
      $('msg').innerHTML = '<span class="ok">✓ 保存しました</span>　'
        + '<span class="danger">（画面の再読込に失敗しました。ページを再読み込みしてください）</span>';
    }
  } catch (e) {
    $('msg').innerHTML = `<span class="danger">${e.message}</span>`;
  } finally {
    $('save').disabled = false;
  }
}

// 接続テスト（入力中のキーで1回だけ試す・保存しない）。モデルは管理者のカタログ既定を使う
// （サーバ側 `model_catalog.resolve_model` が未指定時に解決する）。
async function test(provider) {
  const res = $('t-' + provider);
  res.className = 'tres muted';
  res.innerHTML = '<span class="loading-inline" role="status"><span class="spinner spinner-sm"></span><span>接続を確認中...</span></span>';
  const body = { provider };
  if (provider === 'openai') { const k = $('okey').value.trim(); if (k) body.openai_api_key = k; }
  if (provider === 'gemini') { const k = $('gkey').value.trim(); if (k) body.gemini_api_key = k; }
  if (provider === 'ollama') {
    body.ollama_url = $('ourl').value.trim();   // select の値は完全 URL（scheme 込み）
  }
  if (provider === 'codex') { const k = $('okey').value.trim(); if (k) body.openai_api_key = k; }
  if (provider === 'bedrock') { body.bedrock_model = $('bmodel').value.trim(); const k = $('bkey').value.trim(); if (k) body.bedrock_api_key = k; }
  try {
    const d = await (await fetch('/settings/test', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })).json();
    res.className = 'tres ' + (d.ok ? 'ok' : 'danger');
    res.textContent = (d.ok ? '✓ 接続OK' : '✗ ' + (d.detail || '失敗')) + (d.model ? `（${d.model}）` : '');
  } catch (e) {
    res.className = 'tres danger'; res.textContent = '✗ テストに失敗しました';
  }
}

$('save').addEventListener('click', save);
// S3: Ctrl+S（Mac は Cmd+S）でも保存できる（ブラウザの「ページを保存」を横取り・保存ボタン未押下でも効く）。
// ただし API キー発行モーダルが開いている間は、既定動作だけを止めて実際の保存は呼ばない
// （管理画面と同型・発行モーダル操作中に PUT /settings が意図せず走ることを防ぐ）。
document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
    e.preventDefault();
    const ekOverlay = $('ek-overlay');
    if (ekOverlay && ekOverlay.classList.contains('open')) return;
    if (!$('save').disabled) save();
  }
});
$('sysdefault').addEventListener('click', () => { $('sysprompt').value = DEFAULT_SYS; });
// 一覧外の警告（showConstructHint の legacy 注記）は選び直した時点で追従させる
// （初期描画のままだと、一覧にある構成へ変更した後も古い注記が残ってしまう）。
$('agent').addEventListener('change', showConstructHint);
document.querySelectorAll('[data-test]').forEach((b) => b.addEventListener('click', () => test(b.dataset.test)));
$('bmodel-fetch').addEventListener('click', fetchBedrockModels);
$('bmodel-verify').addEventListener('click', verifyBedrockModel);
$('bmodel-manual').addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); verifyBedrockModel(); } });

function applyThemeIcon() { const b = $('themebtn'); if (b) b.textContent = document.documentElement.dataset.theme === 'dark' ? '☀️' : '🌙'; }
document.addEventListener('click', (e) => {
  if (!e.target.closest('#themebtn')) return;
  const d = document.documentElement, next = d.dataset.theme === 'dark' ? 'light' : 'dark';
  d.dataset.theme = next; localStorage.setItem('sherpa-theme', next); applyThemeIcon();
});
applyThemeIcon();
load();
