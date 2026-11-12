// 利用統計画面のトークンタブ・会話別の補助 AI 使用量・ユーザー表の描画。
'use strict';

// ===== トークン（入力/出力トークン数のみ） =====
function providerLabel(p) { return PROVIDER_LABEL[p] || p || '不明'; }

// 用途別（kind）内訳の平文日本語ラベル。未知 kind は生の kind をそのまま表示する。
const KIND_LABEL = {
  'chat-sub': '下調べ',
  research: '外部連携の調査',
  extract: 'ナレッジ抽出（旧機能）',
  propose: '概念の候補づくり（旧機能）',
  chat: '会話', intent: '依頼の仕分け',
  embed: '埋め込み', graph_ask: 'グラフへの質問', vlm: '画像の読み取り',
  // 複数プロファイル自動選択の計画呼び出し。
  'chat-plan': '進め方の計画',
  usage_chat: '利用統計チャット',
  // 清書前のメイン査読（根拠の十分性判定・限定ツール精読）。
  'chat-review': '根拠の査読',
  // 取り込み後にバックグラウンドで後追い実行する rag.md の LLM 成形。
  rag_render: '検索用文書の整形',
};
function kindLabel(k) { return KIND_LABEL[k] || k; }
// トークン列は null（プロバイダが usage を報告しなかった「報告不能」マーカー）なら「未計測」、項目欠落なら「未取得」で表示する。
function fmtTokOrDash(v) { return v === undefined ? '未取得' : v === null ? '未計測' : Number(v).toLocaleString('ja-JP'); }
// キャッシュへの書き込み量。全件が不明（null）なら「不明」（0とは区別）、不明の行が混ざる合計は「（不明を含む）」を添える。項目欠落（古いサーバー）は「未取得」。
function fmtCacheWrite(r) {
  if (r.cache_write === undefined) return '未取得';
  if (r.cache_write === null) return '不明';
  const v = Number(r.cache_write).toLocaleString('ja-JP');
  return r.cache_write_unknown > 0 ? `${v}（不明を含む）` : v;
}
function renderTokenKindTable(rows) {
  const card = $('token-kind-card');
  const tb = $('token-kind-tbody');
  if (!tb) return;
  if (!rows || !rows.length) {
    if (card) card.hidden = true;   // 空/不在ならカードごと隠す
    return;
  }
  if (card) card.hidden = false;
  tb.innerHTML = rows.map((r) => {
    const name = `${esc(providerLabel(r.provider))}`
      + (r.model ? ` <span class="user-uid">${esc(r.model)}</span>` : '');
    return `<tr>
      <td class="nw">${esc(kindLabel(r.kind))}</td>
      <td>${name}</td>
      <td class="num">${(r.calls || 0).toLocaleString('ja-JP')}</td>
      <td class="num">${fmtTokOrDash(r.input)}</td>
      <td class="num">${fmtTokOrDash(r.cached_input)}</td>
      <td class="num">${fmtCacheWrite(r)}</td>
      <td class="num">${fmtTokOrDash(r.output)}</td>
      <td class="num">${fmtTokOrDash(r.reasoning_output)}</td>
      <td class="num">${fmtSecOrDash(r.elapsed_ms_total)}</td>
      <td class="num">${fmtSecOrDash(r.elapsed_ms_avg)}</td>
      <td class="num">${(r.elapsed_n || 0).toLocaleString('ja-JP')}</td>
    </tr>`;
  }).join('');
}

// ユーザー別 × 用途別内訳。利用者に紐付かない呼び出しを含まないため、同一 kind の合計は token-kind-tbody の当該行以下になりうる。
// 空/不在ならカードごと隠す（token-kind-card と同じ流儀）。
function renderTokenUserKindTable(rows) {
  const card = $('token-user-kind-card');
  const tb = $('token-user-kind-tbody');
  if (!tb) return;
  if (!rows || !rows.length) {
    if (card) card.hidden = true;
    return;
  }
  if (card) card.hidden = false;
  tb.innerHTML = rows.map((r) => `<tr>
    <td><div class="user-name">${esc(r.display_name || r.uid)}</div><div class="user-uid">${esc(r.uid)}</div></td>
    <td class="nw">${esc(kindLabel(r.kind))}</td>
    <td class="num">${(r.calls || 0).toLocaleString('ja-JP')}</td>
    <td class="num">${fmtTokOrDash(r.input)}</td>
    <td class="num">${fmtTokOrDash(r.cached_input)}</td>
    <td class="num">${fmtCacheWrite(r)}</td>
    <td class="num">${fmtTokOrDash(r.output)}</td>
    <td class="num">${fmtTokOrDash(r.reasoning_output)}</td>
    <td class="num">${fmtSecOrDash(r.elapsed_ms_total)}</td>
    <td class="num">${fmtSecOrDash(r.elapsed_ms_avg)}</td>
  </tr>`).join('');
}
function renderTokenModelTable(rows) {
  const tb = $('token-model-tbody');
  if (!rows.length) {
    tb.innerHTML = '<tr class="empty-row"><td colspan="7">この期間のトークン記録はまだありません</td></tr>';
    return;
  }
  tb.innerHTML = rows.map((r) => {
    const name = `${esc(providerLabel(r.provider))}`
      + (r.model ? ` <span class="user-uid">${esc(r.model)}</span>` : '');
    return `<tr>
      <td>${name}</td>
      <td class="num">${(r.turns || 0).toLocaleString('ja-JP')}</td>
      <td class="num">${fmtTokOrDash(r.input)}</td>
      <td class="num">${fmtTokOrDash(r.cached_input)}</td>
      <td class="num">${fmtCacheWrite(r)}</td>
      <td class="num">${fmtTokOrDash(r.output)}</td>
      <td class="num">${fmtTokOrDash(r.reasoning_output)}</td>
    </tr>`;
  }).join('');
}
function renderTokenUserTable(rows) {
  const tb = $('token-user-tbody');
  const top = rows || [];   // 取得時に画面・保存共通で上位10件へ絞っている。
  if (!top.length) {
    tb.innerHTML = '<tr class="empty-row"><td colspan="5">この期間のトークン記録はまだありません</td></tr>';
    return;
  }
  tb.innerHTML = top.map((u, i) => `<tr>
    <td><span class="${i === 0 ? 'rank top1' : 'rank'}">${i + 1}</span></td>
    <td><div class="user-name">${esc(u.display_name || u.uid)}</div><div class="user-uid">${esc(u.uid)}</div></td>
    <td class="num">${(u.turns || 0).toLocaleString('ja-JP')}</td>
    <td class="num">${fmtTokOrDash(u.input)}</td>
    <td class="num">${fmtTokOrDash(u.output)}</td>
  </tr>`).join('');
}
function renderTokens(tokens, period) {
  const t = tokens || {};
  const tot = t.totals || {};
  $('t-tok-input').textContent = fmtTokOrDash(tot.input);
  $('t-tok-output').textContent = fmtTokOrDash(tot.output);
  const daily = t.daily || [];
  renderTrendChart(
    $('chart-tokin-svg'), $('chart-tokin-empty'), $('chart-tokin-tip'), $('chart-tokin-wrap'),
    fillDailySeries(daily.map((d) => ({ date: d.date, turns: d.input })), period && period.start, period && period.end)
      .map((d) => ({ date: d.date, value: d.turns })),
    { color: 'var(--accent)', unit: 'tok', label: '入力トークン数（日別）' },
  );
  renderTrendChart(
    $('chart-tokout-svg'), $('chart-tokout-empty'), $('chart-tokout-tip'), $('chart-tokout-wrap'),
    fillDailySeries(daily.map((d) => ({ date: d.date, turns: d.output })), period && period.start, period && period.end)
      .map((d) => ({ date: d.date, value: d.turns })),
    { color: 'var(--ok)', unit: 'tok', label: '出力トークン数（日別）' },
  );
  renderTokenModelTable(t.by_model || []);
  renderTokenUserTable(t.by_user || []);
  renderTokenKindTable(t.by_kind || []);
  renderTokenUserKindTable(t.by_user_kind || []);
}

// 会話ごとの補助 AI 使用量: トークン合計降順で上位20件・タイトル/本文は含まない。
// 「用途別」列は用途ごとに回数・トークン・所要時間を1行ずつ積む（`UsageConversationKindRow` の null の意味は用途別テーブルと同じ）。会話 id はテキスト表示のみ（リンクにしない）。
function conversationKindsSummaryHTML(kinds) {
  return `<ul class="convkinds">${(kinds || []).map((k) => {
    // 入力/出力のどちらかが null（報告不能マーカー・token-kind-tbody と同じ意味）なら合算しない。
    const hasTokens = k.input !== null && k.input !== undefined && k.output !== null && k.output !== undefined;
    const tokTotal = hasTokens ? (k.input + k.output) : null;
    return `<li><b>${esc(kindLabel(k.kind))}</b> ${(k.calls || 0).toLocaleString('ja-JP')}回`
      + `・トークン計${fmtTokOrDash(tokTotal)}`
      + `・所要時間${fmtSecOrDash(k.elapsed_ms_total)}</li>`;
  }).join('')}</ul>`;
}
function renderConversationsTop(rows) {
  const card = $('conversations-top-card');
  const tb = $('conversations-top-tbody');
  if (!tb) return;
  if (!rows || !rows.length) {
    if (card) card.hidden = true;
    return;
  }
  if (card) card.hidden = false;
  tb.innerHTML = rows.map((r) => `<tr>
    <td>#${esc(String(r.conversation_id))}</td>
    <td><div class="user-name">${esc(r.display_name || r.uid)}</div><div class="user-uid">${esc(r.uid)}</div></td>
    <td>${r.world ? esc(r.world) : '<span style="color:var(--ink-3)">—</span>'}</td>
    <td class="num">${(r.user_turns || 0).toLocaleString('ja-JP')}</td>
    <td>${conversationKindsSummaryHTML(r.kinds)}</td>
    <td class="num">${fmtSecOrDash(r.response_time_avg_ms)}</td>
  </tr>`).join('');
}

function detailHTML(u) {
  const worlds = (u.worlds || []).length
    ? u.worlds.map((w) => `<span class="worldtag">${esc(w)}</span>`).join('')
    : '<span style="color:var(--ink-3)">—</span>';
  return `
    ${lensBarHTML(u.lens || {})}
    <div class="u-meta">
      <div class="g"><b>${(u.personal_turns || 0).toLocaleString('ja-JP')}</b><span>個人ファイル参照ターン</span></div>
      <div class="g"><b>${(u.logins || 0).toLocaleString('ja-JP')}</b><span>ログイン回数</span></div>
      <div class="g"><b>${(u.downloads || 0).toLocaleString('ja-JP')}</b><span>原本ダウンロード</span></div>
      <div class="g"><b>${(u.uploads || 0).toLocaleString('ja-JP')}</b><span>個人ファイルアップロード</span></div>
      <div class="g"><b>${(u.shares || 0).toLocaleString('ja-JP')}</b><span>会話共有の発行</span></div>
      <div class="g"><b style="font-size:var(--text-caption)">${worlds}</b><span>利用フォルダ</span></div>
    </div>`;
}

function zeroHitCellHTML(u) {
  if (u.zero_hit_rate === null || u.zero_hit_rate === undefined) {
    const label = u.knowledge_turns === 0 ? '対象なし' : fmtPctOrDash(u.zero_hit_rate);
    return `<td class="num zhr-cell">${label}</td>`;
  }
  const pct = Math.round(u.zero_hit_rate * 100);
  const tip = `${(u.knowledge_turns || 0).toLocaleString('ja-JP')}件中${(u.zero_hit_turns || 0).toLocaleString('ja-JP')}件が出典なし`;
  return `<td class="num zhr-cell" title="${esc(tip)}">${pct}%</td>`;
}

function renderRows(users) {
  const tbody = $('usage-tbody');
  if (!users.length) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="8">この期間の利用はまだありません</td></tr>';
    return;
  }
  tbody.innerHTML = users.map((u, i) => {
    const rank = i + 1;
    const rankCls = rank === 1 ? 'rank top1' : 'rank';
    const name = esc(u.display_name || u.uid);
    return `<tr class="u-row" data-uid="${esc(u.uid)}">
      <td><span class="${rankCls}">${rank}</span></td>
      <td><div class="user-name">${name}</div><div class="user-uid">${esc(u.uid)}</div></td>
      <td class="num">${(u.turns || 0).toLocaleString('ja-JP')}</td>
      <td class="num">${(u.conversations || 0).toLocaleString('ja-JP')}</td>
      <td class="num">${(u.active_days || 0).toLocaleString('ja-JP')}</td>
      <td>${esc(fmtDate(u.last_active))}</td>
      <td class="num">${(u.personal_turns || 0).toLocaleString('ja-JP')}</td>
      ${zeroHitCellHTML(u)}
    </tr>
    <tr class="u-detail"><td colspan="8">${detailHTML(u)}</td></tr>`;
  }).join('');
}

function sortUsers(users) {
  const dir = _sortDir === 'asc' ? 1 : -1;
  return [...users].sort((a, b) => {
    if (_sortKey === 'last_active') {
      const av = a.last_active || '', bv = b.last_active || '';
      return av < bv ? -1 * dir : av > bv ? 1 * dir : 0;
    }
    if (_sortKey === 'zero_hit_rate') {
      const av = a.zero_hit_rate === null || a.zero_hit_rate === undefined ? -1 : a.zero_hit_rate;
      const bv = b.zero_hit_rate === null || b.zero_hit_rate === undefined ? -1 : b.zero_hit_rate;
      return (av - bv) * dir;
    }
    return ((a.turns || 0) - (b.turns || 0)) * dir;
  });
}

function applySortAndRender() {
  renderRows(sortUsers(_users));
  document.querySelectorAll('th.sortable').forEach((th) => {
    const arrow = th.querySelector('.arrow');
    if (!arrow) return;
    arrow.textContent = th.dataset.sort === _sortKey ? (_sortDir === 'desc' ? '▼' : '▲') : '';
  });
}

function setLoading() {
  $('usage-tbody').setAttribute('aria-busy', 'true');
  $('usage-tbody').innerHTML = '<tr><td colspan="8"><div class="loading" role="status" style="padding:16px">'
    + '<span class="spinner spinner-sm"></span><span>利用統計を読み込んでいます...</span></div></td></tr>';
}

