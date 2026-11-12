// システム管理「外部連携」タブ（API キーの発行・一覧・失効）。
'use strict';

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
}

// 簡易回答に使う AI（ollama/openai の2択）。保存値がどちらでもない場合は、select の一時的な選択肢とヒントでその旨を示す。
function renderResearchDefaultProvider(rdp) {
  rdp = rdp || {};
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
  // 保存値が破損している間は、その破損状態自体を基準値にする（'ollama' を基準にすると未保存の変更なしに見えて破損に気付けない）。
  _extKeysResearchProviderBaseline = rdpKnown ? rdp.effective : _RESEARCH_PROVIDER_INVALID;
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
    if (wrap) wrap.innerHTML = `<div class="hint danger">キー一覧を読み込めませんでした: ${esc(e.message)}</div>`;
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

// 外部連携（API キー）タブの描画（利用量タブとは baseline を分けて持つ）。
function renderExtKeysTab(view) {
  renderExtKeysToggle(view.ext_keys);
  const extKeys = view.ext_keys || {};
  _extKeysAllowedBaseline = !!extKeys.user_api_keys_allowed;
  const quota = extKeys.daily_quota_default || {};
  _extKeysQuotaBaseline = quota.configured != null ? String(quota.configured) : '';
}
