// フェーズ6 S4（リファクタリング計画）: 共有ダイアログドメイン（招待チップ・入力補完・送信）。chat.js から純移動。
// 入口は openShareDialog(cid, title)（呼び出し元の状態チェック＝「会話が開かれているか」「個人ファイルを
// 参照済みでないか」は呼び出し側 chat.js の責務のまま・cid/title は引数で渡される＝実依存を grep で
// 確認した結果このモジュール自体は S（共有状態）に依存しない）。
'use strict';

import { copyText } from '../chat.js';

const esc = Sherpa.esc;   // 共通ユーティリティ（common.js・グローバル参照のまま）

// 所有者のみ: POST /conversations/{cid}/shares → 一度だけ表示する URL。
// バッチ2・5番（2026-07-03）: GET /users/suggest による入力補完（デバウンス200ms）→
// クリック/Enter で確定しチップ化。既存のカンマ/スペース区切り手入力も引き続き動く（後方互換）。

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

export function openShareDialog(cid, title) {
  const overlay = document.getElementById('share-overlay');
  const tidEl = document.getElementById('share-dialog-title');
  const result = document.getElementById('share-result');
  const form = document.getElementById('share-form');
  if (!overlay) return;

  // フォームをリセット
  tidEl.textContent = esc(title);
  result.hidden = true;
  form.hidden = false;
  document.getElementById('share-invitees').value = '';
  document.getElementById('share-days').value = '7';
  document.getElementById('share-err').textContent = '';
  _inviteeChips = [];
  renderInviteeChips();
  hideInviteeSuggest();
  overlay.dataset.cid = String(cid);
  overlay.hidden = false;
}

// フェーズ6 S3（地雷7対応）: DOMContentLoaded 依存を即時実行へ（S4 module 化後は動的 import 等で
// DCL 待ちのままだと初期化されない死ダイアログになるため）。classic script の本体末尾での実行なら
// DOM は既にパース済み＝挙動は同値。module（static import）でも同じ理由で安全
// （module の評価は常に DOMContentLoaded より後）。
(() => {
  const overlay = document.getElementById('share-overlay');
  if (!overlay) return;

  // overlay 外クリックで閉じる
  overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.hidden = true; });
  const closeBtn = document.getElementById('share-close');
  if (closeBtn) closeBtn.addEventListener('click', () => { overlay.hidden = true; });

  // コピーボタン
  document.getElementById('share-copy')?.addEventListener('click', () => {
    const url = document.getElementById('share-url-val').textContent;
    if (!url) return;
    copyText(url);
  });

  // チップの削除（× クリック・イベント委譲）
  document.getElementById('share-invitee-chips')?.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-rm-invitee]');
    if (!btn) return;
    _inviteeChips.splice(Number(btn.dataset.rmInvitee), 1);
    renderInviteeChips();
  });

  const inviteesInput = document.getElementById('share-invitees');

  // 最後のトークン（検索クエリとして送った部分）だけを入力欄から取り除く（残りの自由入力は温存）。
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

  // 入力補完（デバウンス200ms）: カンマ/スペース区切りの手入力時は、最後のトークンだけを検索クエリにする。
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

  // キーボード操作（↑↓で候補移動・Enterでハイライト確定・Escで閉じる）
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

  // 送信
  document.getElementById('share-submit')?.addEventListener('click', async () => {
    const cid = Number(overlay.dataset.cid);
    const rawInvitees = document.getElementById('share-invitees').value;
    // '0' = 無期限。Number('0')||7 は 0 が falsy で 7 に化けてしまうため、'0' は先に判定する。
    const daysRaw = document.getElementById('share-days').value;
    const days = daysRaw === '0' ? 0 : (Number(daysRaw) || 7);
    const errEl = document.getElementById('share-err');

    // チップ確定分 ＋ 自由入力（カンマ/スペース区切り）の両方を合わせる（既存の手入力との後方互換）。
    const freeText = rawInvitees.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
    const invitees = Array.from(new Set([..._inviteeChips.map((c) => c.uid), ...freeText]));
    if (!invitees.length) { errEl.textContent = '招待するユーザー名を入力してください'; return; }

    errEl.textContent = '';
    const expires = days === 0 ? null : new Date(Date.now() + days * 86400 * 1000).toISOString();
    const submitBtn = document.getElementById('share-submit');
    if (submitBtn.disabled) return;   // 多重クリック防止（連打で共有リンクを二重作成しない）
    submitBtn.disabled = true;
    try {
      const d = await (await fetch(`/conversations/${cid}/shares`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ invitee_user_ids: invitees, expires_at: expires }),
      })).json();
      if (d.ok) {
        // 共有 URL は一度だけ表示（絶対 URL に補完）。
        const absUrl = location.origin + d.url;
        document.getElementById('share-url-val').textContent = absUrl;
        document.getElementById('share-form').hidden = true;
        document.getElementById('share-result').hidden = false;
      } else {
        errEl.textContent = d.detail || '共有に失敗しました';
      }
    } catch (err) {
      errEl.textContent = '通信エラーが発生しました';
    } finally {
      submitBtn.disabled = false;   // 失敗時に再試行できるよう戻す（成功時はフォーム自体が隠れる）
    }
  });
})();
