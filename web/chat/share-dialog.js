// 会話の共有ダイアログ。招待者の入力補完・チップ、共有リンクの発行、既存共有の取消・延長・更新を扱う。
// 設計: docs/design/chat.md「共有」
// 入口は openShareDialog(cid, title)。共有できる会話かの判定は呼び出し側（chat.js）の責務。
'use strict';

import { copyText, toast } from '../chat.js';

const esc = Sherpa.esc, fmtDateTime = Sherpa.fmtDateTime;

// 所有者のみ: POST /conversations/{cid}/shares で共有リンクを発行する（URL は一度だけ表示）。
// 招待者は GET /users/suggest の入力補完（デバウンス200ms）からチップとして確定する。カンマ/スペース区切りの手入力も使える。

let _inviteeChips = [];          // [{uid, display_name}] クリック/Enter で確定した候補
let _inviteeSuggestTimer = null;
let _inviteeSuggestItems = [];   // 直近の候補（矢印キー・確定処理で参照）
let _inviteeSuggestActive = -1;  // ハイライト中のインデックス（-1=無し）

function renderInviteeChips() {
  const box = document.getElementById('share-invitee-chips');
  if (!box) return;
  box.innerHTML = _inviteeChips.map((u, i) =>
    `<span class="chip invitee-chip">${esc(u.display_name || u.uid)}`
    + `<button type="button" data-rm-invitee="${i}" aria-label="削除">✕</button></span>`).join('');
}

function addInviteeChip(uid, displayName) {
  if (!uid || _inviteeChips.some((c) => c.uid === uid)) return;
  _inviteeChips.push({ uid, display_name: displayName });
  renderInviteeChips();
}

function hideInviteeSuggest() {
  const box = document.getElementById('share-invitee-suggest');
  if (box) { box.hidden = true; box.innerHTML = ''; }
  _inviteeSuggestItems = [];
  _inviteeSuggestActive = -1;
}

function renderInviteeSuggest(items) {
  const box = document.getElementById('share-invitee-suggest');
  if (!box) return;
  if (!items.length) { hideInviteeSuggest(); return; }
  _inviteeSuggestItems = items;
  _inviteeSuggestActive = -1;
  box.innerHTML = items.map((u, i) =>
    `<button type="button" class="share-suggest-item" data-pick-invitee="${i}">`
    + `${esc(u.display_name || u.uid)}<small>${esc(u.uid)}</small></button>`).join('');
  box.hidden = false;
}

async function fetchInviteeSuggest(q) {
  try {
    const d = await (await fetch('/users/suggest?q=' + encodeURIComponent(q))).json();
    renderInviteeSuggest((d.users || []).filter((u) => !_inviteeChips.some((c) => c.uid === u.uid)));
  } catch (_) { hideInviteeSuggest(); }
}

// 「この会話の共有」一覧（招待者・期限・状態・サニタイズ有無）。各行に取消・延長、サニタイズ済かつ未取消の行に「最新の内容に更新」を出す。
function _shareStatusLabel(s) {
  if (s.revoked_at) return '取消済み';
  if (s.expires_at && new Date(s.expires_at).getTime() <= Date.now()) return '期限切れ';
  return '有効';
}

function _shareExistingRowHTML(s) {
  const invitees = (s.invitees || []).map((i) => esc(i.name || i.uid)).join('、') || '（招待者なし）';
  const expiresText = `期限: ${esc(fmtDateTime(s.expires_at))}`;
  const sanitizedText = s.sanitized ? '個人部分を除いて共有' : '会話をそのまま共有';
  const active = !s.revoked_at;
  const refreshedText = s.refreshed_at ? `・最終更新: ${esc(fmtDateTime(s.refreshed_at))}` : '';
  return `<div class="share-existing-row" data-share-id="${s.share_id}">
    <div class="share-existing-main">
      <div class="share-existing-invitees">${invitees}</div>
      <div class="share-existing-meta">${esc(_shareStatusLabel(s))}・${esc(sanitizedText)}・${esc(expiresText)}${refreshedText}</div>
    </div>
    <div class="share-existing-acts">
      ${(s.sanitized && active) ? `<button class="mini" data-share-refresh="${s.share_id}">最新の内容に更新</button>` : ''}
      ${active ? `<button class="mini" data-share-extend="${s.share_id}">延長</button>` : ''}
      ${active ? `<button class="mini ek-danger" data-share-revoke="${s.share_id}">取消</button>` : ''}
    </div>
  </div>`;
}

let _shareListGen = 0;   // loadShareExistingList の世代カウンタ（古い応答の破棄用）

async function loadShareExistingList(cid) {
  const box = document.getElementById('share-existing-list');
  if (!box) return;
  const gen = ++_shareListGen;
  box.textContent = '読み込み中...';
  let html;
  try {
    const shares = await (await fetch(`/conversations/${cid}/shares`)).json();
    html = shares.length ? shares.map(_shareExistingRowHTML).join('')
      : '<div class="muted" style="font-size:var(--text-small)">まだ共有していません</div>';
  } catch (_) {
    html = '<div class="muted" style="font-size:var(--text-small)">共有一覧を読み込めませんでした</div>';
  }
  // 古い応答（新しい世代が進んだ・別会話向けに開き直された）は捨てる
  const overlay = document.getElementById('share-overlay');
  if (gen !== _shareListGen || !overlay || overlay.dataset.cid !== String(cid)) return;
  box.innerHTML = html;
}

let shareReturnSelector;
function closeShareDialog() {
  document.getElementById('share-overlay').hidden = true;
  document.querySelector(shareReturnSelector).focus();
}

// 共有ダイアログを開く（フォームを初期化し、既存の共有一覧を読み込む）。
export function openShareDialog(cid, title) {
  const overlay = document.getElementById('share-overlay');
  const tidEl = document.getElementById('share-dialog-title');
  const result = document.getElementById('share-result');
  const form = document.getElementById('share-form');
  if (!overlay) return;

  // ① フォームをリセットする
  tidEl.textContent = esc(title);
  result.hidden = true;
  form.hidden = false;
  document.getElementById('share-invitees').value = '';
  document.getElementById('share-days').value = '30';
  document.getElementById('share-err').textContent = '';
  _inviteeChips = [];
  renderInviteeChips();
  hideInviteeSuggest();
  overlay.dataset.cid = String(cid);
  // ② 閉じたときに戻すフォーカス先を決める（履歴の再描画後も現在のトリガーへ戻す）
  shareReturnSelector = document.activeElement.matches('[data-conv-menu]')
    ? `[data-conv-menu="${document.activeElement.dataset.convMenu}"]` : '#sharebtn';
  overlay.hidden = false;
  document.getElementById('share-invitees').focus();
  loadShareExistingList(cid);
}

// ダイアログのイベント配線（モジュール評価時に即時実行する）
(() => {
  const overlay = document.getElementById('share-overlay');
  if (!overlay) return;

  // 閉じる操作と Tab のフォーカス循環
  overlay.addEventListener('click', (e) => { if (e.target === overlay) closeShareDialog(); });
  const closeBtn = document.getElementById('share-close');
  if (closeBtn) closeBtn.addEventListener('click', closeShareDialog);
  overlay.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !e.defaultPrevented) {
      e.preventDefault();
      closeShareDialog();
    } else if (e.key === 'Tab') {
      const items = [...overlay.querySelectorAll('button,input,select,a[href]')]
        .filter((el) => !el.disabled && el.getClientRects().length);
      const first = items[0], last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault(); last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault(); first.focus();
      }
    }
  });

  // コピーボタン
  document.getElementById('share-copy')?.addEventListener('click', () => {
    const url = document.getElementById('share-url-val').textContent;
    if (!url) return;
    copyText(url);
  });

  // 既存共有一覧の取消・延長・更新（イベント委譲）
  document.getElementById('share-existing-list')?.addEventListener('click', async (e) => {
    const cid = overlay.dataset.cid;
    const revokeBtn = e.target.closest('[data-share-revoke]');
    if (revokeBtn) {
      if (!confirm('この共有を取消します。招待者は開けなくなります。よろしいですか？')) return;
      revokeBtn.disabled = true;
      try {
        const r = await fetch(`/conversation-shares/${revokeBtn.dataset.shareRevoke}/revoke`, { method: 'POST' });
        if (!r.ok) throw new Error(String(r.status));
      } catch (_) { revokeBtn.disabled = false; return; }
      loadShareExistingList(cid);
      return;
    }
    const extendBtn = e.target.closest('[data-share-extend]');
    if (extendBtn) {
      extendBtn.disabled = true;
      try {
        const r = await fetch(`/conversation-shares/${extendBtn.dataset.shareExtend}/extend`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ days: 30 }),
        });
        if (!r.ok) throw new Error(String(r.status));
      } catch (_) { extendBtn.disabled = false; toast('延長できませんでした'); return; }
      await loadShareExistingList(cid);
      toast('期限を今から30日後に延ばしました');
      return;
    }
    const refreshBtn = e.target.closest('[data-share-refresh]');
    if (refreshBtn) {
      refreshBtn.disabled = true;
      try {
        const r = await fetch(`/conversation-shares/${refreshBtn.dataset.shareRefresh}/refresh`, { method: 'POST' });
        if (!r.ok) throw new Error(String(r.status));
      } catch (_) { refreshBtn.disabled = false; return; }
      await loadShareExistingList(cid);
      toast('更新しました（招待者には次に開いたときから最新が見えます）');
    }
  });

  // チップの削除（イベント委譲）
  document.getElementById('share-invitee-chips')?.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-rm-invitee]');
    if (!btn) return;
    _inviteeChips.splice(Number(btn.dataset.rmInvitee), 1);
    renderInviteeChips();
  });

  const inviteesInput = document.getElementById('share-invitees');

  // 検索クエリにした最後のトークンだけを入力欄から取り除く
  function removeLastInviteeToken() {
    const parts = inviteesInput.value.split(/([\s,]+)/);
    parts.pop();
    inviteesInput.value = parts.join('');
  }

  function confirmInviteeSuggestion(item) {
    if (!item) return;
    addInviteeChip(item.uid, item.display_name);
    removeLastInviteeToken();
    hideInviteeSuggest();
    inviteesInput.focus();
  }

  // 入力補完（デバウンス200ms）。最後のトークンだけを検索クエリにする
  inviteesInput?.addEventListener('input', () => {
    clearTimeout(_inviteeSuggestTimer);
    const tail = inviteesInput.value.split(/[\s,]+/).pop().trim();
    if (!tail) { hideInviteeSuggest(); return; }
    _inviteeSuggestTimer = setTimeout(() => fetchInviteeSuggest(tail), 200);
  });

  // 候補クリックで確定
  document.getElementById('share-invitee-suggest')?.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-pick-invitee]');
    if (!btn) return;
    confirmInviteeSuggestion(_inviteeSuggestItems[Number(btn.dataset.pickInvitee)]);
  });

  // キーボード操作（↑↓で候補移動・Enterで確定・Escで閉じる）
  inviteesInput?.addEventListener('keydown', (e) => {
    if (!_inviteeSuggestItems.length) return;
    const box = document.getElementById('share-invitee-suggest');
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      _inviteeSuggestActive = Math.min(_inviteeSuggestActive + 1, _inviteeSuggestItems.length - 1);
      Array.from(box.children).forEach((el, i) => el.classList.toggle('active', i === _inviteeSuggestActive));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      _inviteeSuggestActive = Math.max(_inviteeSuggestActive - 1, 0);
      Array.from(box.children).forEach((el, i) => el.classList.toggle('active', i === _inviteeSuggestActive));
    } else if (e.key === 'Enter' && _inviteeSuggestActive >= 0) {
      e.preventDefault();
      confirmInviteeSuggestion(_inviteeSuggestItems[_inviteeSuggestActive]);
    } else if (e.key === 'Escape') {
      hideInviteeSuggest();
    }
  });

  // 送信（共有リンクの発行）
  document.getElementById('share-submit')?.addEventListener('click', async () => {
    const cid = Number(overlay.dataset.cid);
    const rawInvitees = document.getElementById('share-invitees').value;
    const days = Number(document.getElementById('share-days').value) || 30;
    const errEl = document.getElementById('share-err');

    // チップ確定分と自由入力を合わせる
    const freeText = rawInvitees.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
    const invitees = Array.from(new Set([..._inviteeChips.map((c) => c.uid), ...freeText]));
    if (!invitees.length) { errEl.textContent = '招待するユーザー名を入力してください'; return; }

    errEl.textContent = '';
    const expires = new Date(Date.now() + days * 86400 * 1000).toISOString();
    const submitBtn = document.getElementById('share-submit');
    if (submitBtn.disabled) return;   // 二重発行の防止
    submitBtn.disabled = true;
    try {
      const d = await (await fetch(`/conversations/${cid}/shares`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ invitee_user_ids: invitees, expires_at: expires }),
      })).json();
      if (d.ok) {
        // 共有 URL は一度だけ表示する
        const absUrl = location.origin + d.url;
        document.getElementById('share-url-val').textContent = absUrl;
        document.getElementById('share-form').hidden = true;
        document.getElementById('share-result').hidden = false;
        loadShareExistingList(cid);
      } else {
        errEl.textContent = d.detail || '共有に失敗しました';
      }
    } catch (err) {
      errEl.textContent = '通信エラーが発生しました';
    } finally {
      submitBtn.disabled = false;
    }
  });
})();
