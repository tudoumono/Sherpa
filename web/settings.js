// 設定ページ（スタンドアロン・全ページの上部ナビ「設定」から到達）。GET/PUT /settings＋POST /settings/test。
// 設計: docs/design/settings.md「個人設定に残るもの」
// ここで設定するのは「チャットに使う AI」の選択と、許可されている場合の自分専用の API キー。機能ごとの AI・モデル名の選択は管理者の「システム管理」（使えるモデル）で行う。
// API キーは書込専用（入力時のみ送信・再表示しない）。
'use strict';
const $ = Sherpa.$;   // 共通ユーティリティ（nav.js）

// カタログ参照の <select> を組み立てる汎用ヘルパー（自由入力ではない）。`info`（`{allowed, default}` 形）から選択肢を組み立て、`current`（保存済みの値）が一覧外なら「現在の値（一覧外）」を選択肢へ追加して選択状態にする（保存時は弾かない＝サーバ側と同じ方針）。
// 空の選択肢（値=""）は「管理者の既定を使う」＝保存すると明示的に空文字が送られ、既存の保存値を既定へ戻す（save() 参照）。
// 全て textContent のみで組み立てる（XSS 安全）。戻り値は「現在の値が一覧外だった（警告表示が必要）」かどうか。
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
// Ollama 接続先は管理者の許可ホスト一覧（`ollama_url_choice`・`{allowed, default}` 形）から選ぶ（自由入力ではない）。
// `allowed` は完全 URL（scheme 込み）を保持する（host:port へ丸めると https が http に化ける・IPv6 の角括弧が失われるため）。
// 空の選択肢（値=""）は「管理者の既定を使う」＝fillModelSelect と同じ規約（save() が明示的に空文字を送る）。
function fillOllamaUrlSelect(info, current) {
  return fillModelSelect('ourl', info, current);
}
function showModelWarn(warnId, warn, label) {
  const el = $(warnId);
  if (!el) return;
  el.hidden = !warn;
  if (warn) el.textContent = `現在の設定（${label}）は管理者の一覧にありません。選び直すと消えます。`;
}

// 実行構成: 選択肢はサーバが返す `constructs_available` だけを描画する（env で無効な AI は一覧に入らない・textContent のみ＝XSS 安全）。
// 各 option には保存すべき設定値（agent / codex_model_provider）を data 属性で持たせ、save() が「実際に選び直した時だけ」そのまま送る（agentConstructChanged 参照）。
// 保存済みの実値（currentAgent/currentCodexModelProvider）が一覧に無い場合は、fillModelSelect と同じ「一覧外」プレースホルダで実値を保持する（先頭候補へ黙って差し替えない）。
let _constructs = [];
let _agentBaseline = { agent: '', codexModelProvider: '' };
function _selectedAgentDataset() {
  const opt = $('agent').selectedOptions[0];
  return {
    agent: (opt && opt.dataset && opt.dataset.agent) || '',
    codexModelProvider: (opt && opt.dataset && opt.dataset.codexModelProvider) || '',
  };
}
// 実行構成の選択が render() 時点の基準値（_agentBaseline）から変わっているか。値を元に戻せば送信対象からも外れる（admin-settings.js のダーティ判定と同型）。
// 基準値が null（直前の保存が通信例外/5xx で結果不確定）のときは値の比較をせず常に真を返す＝次の保存では必ず agent を送り直す（save() 参照）。
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
// 個人キーの入力欄は「個人キーが許可されている」かつ「このクラウド AI が現在選択されている」の両方を満たすときだけ見せる。片方でも欠けたら隠して理由別の注記に差し替える（settings_put も両方を 422 で拒否する）。
const _CLOUD_KEY_PROVIDER = { okey: 'openai' };
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

// 接続先が OpenAI 直結（既定）以外のとき、短い注記を出す（Azure ＝ デプロイ名は管理者側の設定・custom ＝ 接続先ホストのみ）。表示のみで入力は受け付けない。textContent のみ使う。
function renderOpenAIEndpointNote(s) {
  const el = $('openai-endpoint-note');
  if (!el) return;
  const kind = s.openai_endpoint_kind || 'openai';
  const host = s.openai_base_url_host || '';
  if (kind === 'azure') {
    // このページにモデル欄は無い（モデル名は管理者の「使えるモデル」で管理する。Azure の「デプロイ名」もそちら）。
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
// 管理者が「利用者のキー発行を許可する」を ON にしたときだけカードを表示する。対象フォルダのスコープは常にサーバ側が本人のアクセス範囲へ強制するため、この画面では対象フォルダの入力を出さない（「発行者」列も不要＝常に自分自身）。
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
    // Webhook 登録の有無（host:port のみ・secret は出さない）。
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

// 一覧 GET の世代番号（管理画面と同型）。遅れて届いた古い応答が、後から発行した新しい応答を上書きしないようにする。
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

// 発行モーダルの状態機械: 'idle'（入力中）→ 'issuing'（応答待ち・閉鎖不可）→ 'revealed'（キー本体を表示中）。issuing の間は閉じる手段（✕・キャンセル・背景クリック）を全て無効化する。
// 操作トークン（_ekActiveOp）: open/close のたびに新しい値を発行し、非同期処理は自分のトークンと一致する時だけ画面状態を変える（管理画面と同型）。
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

// モーダルが開いている間、背後を inert にする（管理画面と同型）。#toast は対象外。
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

// POST の結果が不明（タイムアウト・通信断・不正な形の応答）なときの回復導線。POST /ext/v1/keys/recover へ client_op_id を渡し、サーバー側で本人が試みた未失効のキーを単一の原子的操作で照合・失効する（管理画面と同型）。
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
  // min 属性は手入力・貼り付けでは効かないため、送信前にも文字列比較（YYYY-MM-DD＝辞書順=時系列順）で過去日を弾く（管理画面と同型）。
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
// クリップボードへコピー（`navigator.clipboard` 不可なら `execCommand('copy')`）。キー本体・Webhook secret の「今だけ表示」欄で共通（admin-settings.js と同型）。
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

// 設定を読み込んで描画する。成否を boolean で返す（例外は投げない＝ページ初期化時の fire-and-forget 呼び出しを壊さないため）。
// save() はこれを見て「保存はできたが再読込に失敗した」ことを伝え分ける。
async function load() {
  try {
    const r = await fetch('/settings');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const s = await r.json();
    renderConstructOptions(s.constructs_available, s.construct_id, s.agent, s.codex_model_provider);
    applyCloudKeyVisibility(!!s.personal_api_keys_allowed, s.cloud_provider || 'openai');
    // 管理者が許可したときだけ「外部連携」カードを出す。
    const extKeysCard = $('ext-keys-card');
    _extKeysDailyQuotaDefault = s.user_api_keys_daily_quota_default || null;
    if (extKeysCard) {
      extKeysCard.hidden = !s.user_api_keys_allowed;
      if (s.user_api_keys_allowed) loadExtKeys();
    }
    renderOpenAIEndpointNote(s);
    showModelWarn('ourl-warn', fillOllamaUrlSelect(s.ollama_url_choice, s.ollama_url), s.ollama_url);
    $('okey').value = '';
    $('okey').placeholder = s.openai_key_set ? '設定済み（変更する時だけ入力）' : '未設定（sk-...）';
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
    // Ollama 接続先（select の値は完全 URL・空文字＝管理者の既定を使う）。モデル名は管理者の「使えるモデル」カタログに従う。
    ollama_url: $('ourl').value.trim(),
  };
  // 実行構成は「実際に選び直した」時だけ送る。常に送ると、一覧に無い保存値が先頭候補へ黙って上書きされたり、未選択（自動選択）がその時点の解決値で固定されてしまう（agentConstructChanged 参照）。
  if (agentConstructChanged()) {
    const ds = _selectedAgentDataset();
    body.agent = ds.agent;
    body.codex_model_provider = ds.codexModelProvider || null;
  }
  const k = $('okey').value.trim(); if (k) body.openai_api_key = k;    // 入力時のみ更新（書込専用）
  try {
    let r;
    try {
      r = await fetch('/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    } catch (networkErr) {
      // 通信例外＝応答が届かず、サーバ側でコミットされたか分からない。次回の保存が送信を省略しないよう、基準値を「不明」（null）にする（agentConstructChanged 参照）。
      if ('agent' in body) _agentBaseline = null;
      throw networkErr;
    }
    if (!r.ok) {
      let detail = '';
      try { detail = (await r.json()).detail || ''; } catch (_) { /* body なし */ }
      // 5xx はサーバ側の処理結果が確認できない（4xx＝明確な拒否＝未適用とは別扱い）。次回保存では値の一致に関わらず必ず agent を送り直す。
      if (r.status >= 500 && 'agent' in body) _agentBaseline = null;
      throw new Error(detail || ('保存に失敗しました (' + r.status + ')'));
    }
    // 実行構成を送信した場合、基準値は PUT 成功が確定した時点で送信済みの値へ進める（load() の成否を待たない）。
    // load() 任せにすると、直後の GET 失敗時に基準値が古いまま残り、送信前の値へ選び直した保存が差分なしと誤判定される。
    if ('agent' in body) {
      _agentBaseline = { agent: body.agent, codexModelProvider: body.codex_model_provider || '' };
    }
    // PUT 成功が確定した時点で、送信済みの書込専用キー入力欄をローカルでクリアする（load() の成否に関係なく）。
    // ただし送信時点の値（snapshot: k）とまだ一致している時だけクリアする（PUT 保留中に打ち直した未送信の値を消さないため）。
    if ($('okey').value.trim() === k) $('okey').value = '';
    // PUT 成功が確定した時点で（load() の成否を待たず）基準値を更新する。load() の成否でしか更新しないと、再読込失敗時に次の保存が古い基準値と比較してしまう。
    // load() の完了を待ってから「保存しました」表示と保存ボタンの再有効化を行う（待たないと、遅れて完了した load() の再描画が新しい入力を上書きする）。
    // load() の成否（boolean）を見て、失敗時は「保存はできたが再読込に失敗した」ことが分かる表示にする（load() 自身が出した「設定を読み込めませんでした」を追記の形で残す）。
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
  if (provider === 'ollama') {
    body.ollama_url = $('ourl').value.trim();   // select の値は完全 URL（scheme 込み）
  }
  if (provider === 'codex') { const k = $('okey').value.trim(); if (k) body.openai_api_key = k; }
  try {
    const d = await (await fetch('/settings/test', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })).json();
    res.className = 'tres ' + (d.ok ? 'ok' : 'danger');
    res.textContent = (d.ok ? '✓ 接続OK' : '✗ ' + (d.detail || '失敗')) + (d.model ? `（${d.model}）` : '');
  } catch (e) {
    res.className = 'tres danger'; res.textContent = '✗ テストに失敗しました';
  }
}

$('save').addEventListener('click', save);
// Ctrl+S（Mac は Cmd+S）でも保存できる（ブラウザの「ページを保存」を横取りする）。ただし API キー発行モーダルが開いている間は、既定動作だけを止めて保存は呼ばない（発行モーダル操作中に PUT /settings が意図せず走らないため）。
document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
    e.preventDefault();
    const ekOverlay = $('ek-overlay');
    if (ekOverlay && ekOverlay.classList.contains('open')) return;
    if (!$('save').disabled) save();
  }
});
// 一覧外の警告（showConstructHint の legacy 注記）は選び直した時点で追従させる
// （初期描画のままだと、一覧にある構成へ変更した後も古い注記が残ってしまう）。
$('agent').addEventListener('change', showConstructHint);
document.querySelectorAll('[data-test]').forEach((b) => b.addEventListener('click', () => test(b.dataset.test)));

function applyThemeIcon() { const b = $('themebtn'); if (b) b.textContent = document.documentElement.dataset.theme === 'dark' ? '☀️' : '🌙'; }
document.addEventListener('click', (e) => {
  if (!e.target.closest('#themebtn')) return;
  const d = document.documentElement, next = d.dataset.theme === 'dark' ? 'light' : 'dark';
  d.dataset.theme = next; localStorage.setItem('sherpa-theme', next); applyThemeIcon();
});
applyThemeIcon();
load();
