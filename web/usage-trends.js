// 利用統計画面の傾向タブ（ゼロヒット・ヒートマップ・棒グラフ・応答時間・レビュー・定着・DL 数）の描画。
'use strict';

// ===== 利用の傾向（ゼロヒット率・ヒートマップ・world/頭脳別・定着・DL数） =====
// ヒートマップ/world別は「magnitude」＝sequential 1色（--accent）。頭脳別は「identity」＝カテゴリ色（既存 semantic token の固定順＋常設凡例）。
// 週次アクティブ・DL日別は既存 renderTrendChart をそのまま再利用する。

function renderZeroHitTile(zeroHit) {
  const rate = zeroHit?.rate;
  $('t-zerohit').textContent = zeroHit?.knowledge_turns === 0 ? '対象なし' : fmtPctOrDash(rate);
  $('zero-hit-counts').textContent = `出典なし ${fmtTokOrDash(zeroHit?.zero_hit_turns)}件 / 社内資料参照の回答 ${fmtTokOrDash(zeroHit?.knowledge_turns)}件。出典の有無は回答の正しさを示しません。`;
}

const DAY_LABELS_JST = ['日', '月', '火', '水', '木', '金', '土'];   // Postgres DOW: 0=日〜6=土

function renderHeatmap(heatmapData) {
  const svgEl = $('heatmap-svg'), emptyEl = $('heatmap-empty'), tipEl = $('heatmap-tip'), wrapEl = $('heatmap-wrap');
  while (svgEl.firstChild) svgEl.removeChild(svgEl.firstChild);
  const total = (heatmapData || []).reduce((s, c) => s + (c.count || 0), 0);
  if (!total) {
    svgEl.style.display = 'none'; emptyEl.hidden = false; tipEl.hidden = true;
    return;
  }
  svgEl.style.display = 'block'; emptyEl.hidden = true;

  const grid = {};
  let maxV = 0;
  (heatmapData || []).forEach((c) => {
    grid[`${c.weekday}-${c.hour}`] = c.count || 0;
    if ((c.count || 0) > maxV) maxV = c.count;
  });

  const W = 600, H = 150;
  const padL = 24, padR = 4, padT = 4, padB = 15;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const cellW = plotW / 24, cellH = plotH / 7;
  svgEl.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svgEl.setAttribute('preserveAspectRatio', 'none');
  svgEl.setAttribute('role', 'img');
  svgEl.setAttribute('aria-label', `時間帯×曜日の利用ヒートマップ。最も多い時間帯は${maxV}件。`);

  DAY_LABELS_JST.forEach((d, wd) => {
    const t = _svgEl('text', {
      x: padL - 5, y: padT + cellH * wd + cellH / 2 + 3, 'text-anchor': 'end', 'font-size': 9, fill: 'var(--ink-3)',
    });
    t.textContent = d;
    svgEl.appendChild(t);
  });
  [0, 6, 12, 18, 23].forEach((h) => {
    const t = _svgEl('text', {
      x: padL + cellW * h + cellW / 2, y: H - 3, 'text-anchor': 'middle', 'font-size': 8.5, fill: 'var(--ink-3)',
    });
    t.textContent = String(h);
    svgEl.appendChild(t);
  });

  function showTip(wd, h, count, clientX, clientY) {
    tipEl.hidden = false;
    tipEl.innerHTML = '';
    const vd = document.createElement('div'); vd.className = 'v'; vd.textContent = `${count}件`;
    const dd = document.createElement('div'); dd.className = 'd'; dd.textContent = `${DAY_LABELS_JST[wd]}曜 ${h}時台`;
    tipEl.appendChild(vd); tipEl.appendChild(dd);
    const wrapRect = wrapEl.getBoundingClientRect();
    tipEl.style.left = `${clientX - wrapRect.left}px`;
    tipEl.style.top = `${clientY - wrapRect.top}px`;
  }
  function hideTip() { tipEl.hidden = true; }

  for (let wd = 0; wd < 7; wd++) {
    for (let h = 0; h < 24; h++) {
      const count = grid[`${wd}-${h}`] || 0;
      // sequential 1色（--accent）: 0 は無色（枠線のみ）・値が大きいほど濃い塗り（下限を設けて可視化を保証）。
      const opacity = count > 0 ? Math.max(0.15, count / maxV) : 0;
      const rect = _svgEl('rect', {
        x: padL + cellW * h + 1, y: padT + cellH * wd + 1,
        width: Math.max(cellW - 2, 1), height: Math.max(cellH - 2, 1),
        rx: 2, class: 'cell', fill: 'var(--accent)', 'fill-opacity': String(opacity),
        stroke: count > 0 ? 'none' : 'var(--border)', 'stroke-width': count > 0 ? '0' : '1',
        tabindex: '0',
      });
      rect.addEventListener('pointerenter', (e) => showTip(wd, h, count, e.clientX, e.clientY));
      rect.addEventListener('pointermove', (e) => showTip(wd, h, count, e.clientX, e.clientY));
      rect.addEventListener('pointerleave', hideTip);
      rect.addEventListener('focus', () => {
        const r = rect.getBoundingClientRect();
        showTip(wd, h, count, r.left + r.width / 2, r.top);
      });
      rect.addEventListener('blur', hideTip);
      svgEl.appendChild(rect);
    }
  }
}

// 汎用の横棒チャート（world別・頭脳別で共用）。items: [{label, value, color}]。
function renderBarChart(svgEl, emptyEl, tipEl, wrapEl, items, opt) {
  while (svgEl.firstChild) svgEl.removeChild(svgEl.firstChild);
  const total = items.reduce((s, it) => s + (it.value || 0), 0);
  if (!items.length || total === 0) {
    svgEl.style.display = 'none'; emptyEl.hidden = false; tipEl.hidden = true;
    wrapEl.style.height = '';
    return;
  }
  svgEl.style.display = 'block'; emptyEl.hidden = true;

  const rowH = 30, barH = 18;
  const W = 600, H = items.length * rowH + 8;
  wrapEl.style.height = `${H}px`;
  const labelW = 128, valW = 44;
  const padL = labelW, padR = valW, padT = 4;
  const plotW = W - padL - padR;
  svgEl.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svgEl.setAttribute('preserveAspectRatio', 'none');
  svgEl.setAttribute('role', 'img');
  svgEl.setAttribute('aria-label',
    `${opt.title || ''}: ${items.map((it) => `${it.label} ${it.value}${opt.unit || ''}`).join('、')}`);

  const maxV = Math.max(...items.map((it) => it.value), 1);

  function showTip(it, clientX, clientY) {
    tipEl.hidden = false;
    tipEl.innerHTML = '';
    const vd = document.createElement('div'); vd.className = 'v'; vd.textContent = `${it.value}${opt.unit || ''}`;
    const dd = document.createElement('div'); dd.className = 'd'; dd.textContent = it.label;
    tipEl.appendChild(vd); tipEl.appendChild(dd);
    const wrapRect = wrapEl.getBoundingClientRect();
    tipEl.style.left = `${clientX - wrapRect.left}px`;
    tipEl.style.top = `${clientY - wrapRect.top}px`;
  }
  function hideTip() { tipEl.hidden = true; }

  items.forEach((it, i) => {
    const y = padT + i * rowH;
    const barW = it.value > 0 ? Math.max((plotW * it.value) / maxV, 3) : 0;

    const label = _svgEl('text', {
      x: padL - 8, y: y + barH / 2 + 3.5, 'text-anchor': 'end', 'font-size': 11, fill: 'var(--ink-2)',
    });
    label.textContent = it.label.length > 13 ? `${it.label.slice(0, 12)}…` : it.label;
    svgEl.appendChild(label);

    svgEl.appendChild(_svgEl('rect', {   // track（未達分の薄い背景・値の大きさが直感的に伝わるように）
      x: padL, y, width: plotW, height: barH, rx: 4, fill: 'var(--border)', 'fill-opacity': '0.35',
    }));
    if (barW > 0) {
      svgEl.appendChild(_svgEl('rect', {
        x: padL, y, width: barW, height: barH, rx: 4, fill: it.color || 'var(--accent)', class: 'bar',
      }));
    }
    const valLabel = _svgEl('text', {   // 値はバーの先端に（bars→value at the tip）
      x: padL + barW + 6, y: y + barH / 2 + 3.5, 'font-size': 10.5, fill: 'var(--ink)',
      'font-variant-numeric': 'tabular-nums',
    });
    valLabel.textContent = String(it.value);
    svgEl.appendChild(valLabel);

    const hit = _svgEl('rect', {
      x: 0, y, width: W, height: barH, fill: 'transparent', 'pointer-events': 'all', tabindex: '0',
    });
    hit.addEventListener('pointerenter', (e) => showTip(it, e.clientX, e.clientY));
    hit.addEventListener('pointermove', (e) => showTip(it, e.clientX, e.clientY));
    hit.addEventListener('pointerleave', hideTip);
    hit.addEventListener('focus', () => {
      const r = hit.getBoundingClientRect();
      showTip(it, r.left + r.width / 2, r.top);
    });
    hit.addEventListener('blur', hideTip);
    svgEl.appendChild(hit);
  });
}

function renderWorldBar(worlds) {
  const items = (worlds || []).map((w) => ({ label: w.world, value: w.turns || 0, color: 'var(--accent)' }));
  renderBarChart($('chart-world-svg'), $('chart-world-empty'), $('chart-world-tip'), $('chart-world-wrap'),
    items, { unit: '件', title: 'フォルダ別利用量' });
}

function renderProviderBar(providers) {
  const orderIdx = (id) => { const i = PROVIDER_ORDER.indexOf(id); return i === -1 ? 999 : i; };
  const items = (providers || [])
    .slice()
    .sort((a, b) => orderIdx(a.provider) - orderIdx(b.provider))
    .map((p) => ({
      label: PROVIDER_LABEL[p.provider] || p.provider || '不明',
      value: p.turns || 0,
      color: PROVIDER_COLOR[p.provider] || 'var(--ink-3)',
    }));
  renderBarChart($('chart-provider-svg'), $('chart-provider-empty'), $('chart-provider-tip'), $('chart-provider-wrap'),
    items, { unit: '件', title: '頭脳別利用比率' });
  // 常設の凡例（色だけに頼らない）。
  $('chart-provider-legend').innerHTML = items.map((it) =>
    `<span class="item"><span class="dot" style="background:${it.color}"></span>${esc(it.label)} ${it.value}件</span>`,
  ).join('');
}

// ターンの終了理由の分布（`sherpa/stop_kind.py` の閉じた語彙・単色バー＝各行が自分のラベルを持つため凡例配色は要らない）。
// 停止数（`stopped_turns`）は回答を保存しないため分布には現れない別集計＝バッジで併記する。
function renderStopKinds(stopKinds, stoppedTurns) {
  const byKind = new Map((stopKinds || []).map((r) => [r.stop_kind, r.turns || 0]));
  const items = STOP_KIND_ORDER
    .filter((k) => byKind.has(k))
    .map((k) => ({ label: stopKindLabel(k), value: byKind.get(k), color: 'var(--accent)' }));
  // allowlist 外の値（未知の stop_kind）が来ても取りこぼさない（fail-safe）。
  (stopKinds || []).forEach((r) => {
    if (!STOP_KIND_ORDER.includes(r.stop_kind)) {
      items.push({ label: stopKindLabel(r.stop_kind), value: r.turns || 0, color: 'var(--accent)' });
    }
  });
  renderBarChart($('chart-stopkind-svg'), $('chart-stopkind-empty'), $('chart-stopkind-tip'),
    $('chart-stopkind-wrap'), items, { unit: '件', title: 'ターンの終了理由' });
  $('stopkind-total-badge').textContent = `利用者停止 ${fmtTokOrDash(stoppedTurns)}${stoppedTurns == null ? '' : '件'}`;
}

// 回答の完了状態（`answer.completion` の4値＋旧形式の 'unknown'）の内訳を終了理由の下に1行で出す。
const COMPLETION_LABEL = { complete: '最後まで回答', partial: '途中までの回答', stopped: '停止', failed: '回答できず', unknown: '不明（旧形式）' };
function renderCompletions(completions) {
  const rows = (completions || []).filter((r) => r && (r.turns || 0) > 0);
  $('completion-note').textContent = rows.length
    ? '回答の状態: ' + rows.map((r) => `${COMPLETION_LABEL[r.completion] || r.completion} ${r.turns}件`).join('・') : '';
}

// ミリ秒を秒表記へ（null は未計測・欠落は未取得）。回答時間・所要時間の各テーブル/カードで共用。
function fmtSecOrDash(ms) {
  if (ms === undefined) return '未取得';
  if (ms === null) return '未計測';
  return `${(Number(ms) / 1000).toFixed(1)}秒`;
}
// 件数（会話あたりのやり取り回数の avg/median/p90 等）の小数表示（null は未計測・欠落は未取得）。
function fmtNumOrDash(v, digits) {
  if (v === undefined) return '未取得';
  if (v === null) return '未計測';
  return Number(v).toFixed(digits === undefined ? 1 : digits);
}
// 割合（0〜1）を%表示へ（null は未計測・欠落は未取得）。
function fmtPctOrDash(v) {
  if (v === undefined) return '未取得';
  if (v === null) return '未計測';
  return `${Math.round(v * 100)}%`;
}

// 会話あたりのやり取り回数（avg/median/max/p90）と resume 率。対象会話が無ければ全て「—」
// （タイル系カードは token-kind-card と違いカードごと隠さない＝サマリタイルと同じ流儀）。
function renderConversationTurns(turns, resumeRate) {
  const t = turns || {};
  $('t-turns-avg').textContent = fmtNumOrDash(t.avg);
  $('t-turns-median').textContent = fmtNumOrDash(t.median);
  $('t-turns-p90').textContent = fmtNumOrDash(t.p90);
  $('t-turns-max').textContent = fmtTokOrDash(t.max);
  $('t-resume-rate').textContent = t.session_eligible === 0 ? '対象なし' : fmtPctOrDash(resumeRate);
  $('session-counts').textContent = `IDあり ${fmtTokOrDash(t.session_recorded)}件 / 対象会話 ${fmtTokOrDash(t.session_eligible)}件`;
}

// 回答時間の分布（全体＋経路別）。`overall` は API 契約上つねに存在する行（対象0件でも
// avg/median/p90/max=null・n=0）＝カードは隠さず「全体」行だけの表として描画する。
function renderResponseTime(rt) {
  const tb = $('response-time-tbody');
  if (!tb) return;
  const overall = (rt && rt.overall) || {};
  const byProvider = (rt && rt.by_provider) || [];
  const rows = [{ label: '全体', row: overall }]
    .concat(byProvider.map((r) => ({ label: providerLabel(r.provider), row: r })));
  tb.innerHTML = rows.map(({ label, row }) => `<tr>
    <td>${esc(label)}</td>
    <td class="num">${fmtSecOrDash(row.avg)}</td>
    <td class="num">${fmtSecOrDash(row.median)}</td>
    <td class="num">${fmtSecOrDash(row.p90)}</td>
    <td class="num">${fmtSecOrDash(row.max)}</td>
    <td class="num">${fmtTokOrDash(row.n)}</td>
  </tr>`).join('');
}

// 「打ち切りの内訳」（`InvestigationState.limits`・制限そのものは変えない計測専用）。
// 行=経路（provider）・列=各項目の「対象ターン数のうち当たったターン数」（回数系は「n件（合計m回）」・
// bool系は件数のみ）——`stats.limits.by_provider` が空（対象ターンが1件も無い期間）なら空表のまま。
function renderLimits(limits) {
  const tb = $('limits-tbody');
  if (!tb) return;
  const rows = (limits && limits.by_provider) || [];
  const cnt = (turns, total) => turns == null ? '未計測' : (total === undefined
    ? `${turns.toLocaleString('ja-JP')}件`
    : `${turns.toLocaleString('ja-JP')}件（計${fmtTokOrDash(total)}回）`);
  tb.innerHTML = rows.map((r) => `<tr>
    <td>${esc(providerLabel(r.provider))}</td>
    <td class="num">${(r.turns || 0).toLocaleString('ja-JP')}</td>
    <td class="num">${cnt(r.tool_result_clipped_turns, r.tool_result_clipped_total)}</td>
    <td class="num">${cnt(r.total_budget_hit_turns)}</td>
    <td class="num">${cnt(r.context_compactions_turns, r.context_compactions_total)}</td>
    <td class="num">${cnt(r.synthesis_truncated_turns)}</td>
    <td class="num">${cnt(r.depth_escalated_turns)}</td>
    <td class="num">${cnt(r.search_truncated_turns, r.search_truncated_total)}</td>
    <td class="num">${cnt(r.auto_continues_turns, r.auto_continues_total)}</td>
    <td class="num">${cnt(r.duplicate_tool_call_turns, r.duplicate_tool_call_total)}</td>
    <td class="num">${cnt(r.tool_calls_exhausted_turns)}</td>
    <td class="num">${cnt(r.backend_unavailable_fulltext_turns)}</td>
    <td class="num">${cnt(r.backend_unavailable_graph_turns)}</td>
    <td class="num">${cnt(r.graph_reingest_required_turns)}</td>
  </tr>`).join('');
}

// 「道具の使われ方」と「影響一覧」（件数だけ・資料名や検索語は出さない）。古い期間（新しい欄が 0・欠け）でも空の表で出す。
function renderToolUse(tc, impact) {
  const n = (v) => (v || 0).toLocaleString('ja-JP');
  const tb = $('tool-calls-tbody');
  if (tb) {
    tb.innerHTML = ((tc && tc.tools) || []).map((t) => {
      const r = t.by_role || {};
      const avg = t.calls ? `${Math.round((t.ms || 0) / t.calls).toLocaleString('ja-JP')} ms` : '—';
      return `<tr><td>${esc(t.tool)}</td><td class="num">${n(t.calls)}</td><td class="num">${n(t.found)}</td>`
        + `<td class="num">${avg}</td><td class="num">${n(t.errors)}</td><td class="num">${n(t.truncated)}</td>`
        + `<td class="num">${n((r.parent || {}).calls)}</td><td class="num">${n((r.child || {}).calls)}</td><td class="num">${n((r.undetermined || {}).calls)}</td></tr>`;
    }).join('');
  }
  const sum = $('tool-calls-summary');
  if (sum && tc) {
    sum.textContent = `記録のあるターン ${n(tc.turns)}件・記録の欠け ${n(tc.missing)}行・開いた資料 ${n(tc.opened_docs)}件`
      + `（記録が不完全なターン ${n(tc.opened_unknown_turns)}件）。`
      + `道具を使わずに答えようとして調べ直しを促したターン ${n(tc.nudged_turns)}件・促した後に使った ${n(tc.nudged_then_used)}件・`
      + `それでも使わなかった ${n(tc.no_tool_use_turns)}件。`;
  }
  const im = $('impact-summary');
  if (im && impact) {
    im.textContent = `影響をたどったターン ${n(impact.traced_turns)}件（たどらなかった ${n(impact.untraced_turns)}件）・`
      + `候補 ${n(impact.candidate)}件・確かめた ${n(impact.inspected)}件・根拠に使った ${n(impact.used)}件・対応が分からない ${n(impact.unmapped)}件${impact.more ? `・上限で省いた行 ${n(impact.more)}件（状態は数えていません）` : ''}${impact.hidden ? `・名前を出せない行 ${n(impact.hidden)}件（状態は数えていません）` : ''}。`;
  }
}

const REVIEW_LABELS = {
  confirmed: '確定', inferred: '推定', unknown: '不明',
  sufficient: '十分', insufficient: '根拠不足', undecidable: '判定できず',
  not_found_in_scope: '範囲内で見つからない', unexplored: '未調査', conflict: '食い違い', budget: '調査の上限', unreadable: '読み取り不可',
  source_missing: 'ソース未確認', spec_missing: '設計書未確認', definition_missing: '定義未確認',
  log_missing: 'ログ・設定未確認', callgraph_missing: '呼出関係未確認',
  rerun: '次の見直しへ', rounds_exhausted: '見直しの回数に到達',
  user_stop: '利用者が停止', ask_user: '利用者に確認', review_failed: '確認に失敗', failed: '失敗',
  tool_result_clipped: '1件の読取量を制限', total_budget_hit: '累計の読取量に到達',
  context_compactions: '会話履歴を整理', synthesis_truncated: '回答用の情報量を制限',
  depth_escalated: '自動で深く調べた',
  search_truncated: '検索件数を制限', auto_continues: '続きを自動で実行',
  duplicate_tool_call: '同じ条件の再検索を省略',
  tool_calls_exhausted: '調査の回数上限に到達',
  backend_unavailable_fulltext: '全文検索が使えなかった', backend_unavailable_graph: 'グラフが使えなかった',
  graph_reingest_required: 'グラフは再取り込み待ち',
};
const REVIEW_DEPTH_LABELS = { quick: 'クイック', standard: '標準', deep: '深く', max: '最大' };
const REVIEW_CONDITION_LABELS = { main: '本番相当', 'depth2-quick': '見直しなし', 'depth2-standard': '標準（見直し 2 回）', 'depth2-deep': '深く（4 回）', 'depth2-max': '最大' };
function reviewCounts(counts) {
  const totals = new Map();
  Object.entries(counts).forEach(([key, count]) => {
    const label = Object.hasOwn(REVIEW_LABELS, key) ? REVIEW_LABELS[key] : 'その他';
    totals.set(label, (totals.get(label) || 0) + count);
  });
  return Array.from(totals, ([label, count]) => `${label}: ${fmtNumOrDash(count, 0)}`)
    .join('／') || '記録なし';
}
function renderReviewStats(rounds, quality) {
  const table = (heads, rows) => '<table><thead><tr>'
    + heads.map(h => `<th scope="col">${esc(h)}</th>`).join('') + '</tr></thead><tbody>'
    + (rows.length ? rows.map(row => '<tr>' + row.map(cell => `<td>${esc(String(cell))}</td>`).join('') + '</tr>').join('')
      : `<tr><td colspan="${heads.length}">この期間の記録はありません。</td></tr>`)
    + '</tbody></table>';
  const roundTable = (rows, byRound) => table([
    '深さ', '使った AI', ...(byRound ? ['見直しの順番'] : []), '記録数', '増えた出典の合計',
    '平均所要時間（秒）', '判断の内訳', '制限に当たった回数', '見直しの判定', '終了・継続の理由', '足りなかった点',
  ], rows.map(r => [
    REVIEW_DEPTH_LABELS[r.depth_profile] || '不明', providerLabel(r.provider), ...(byRound ? [r.round_no ?? '不明'] : []),
    r.rounds, fmtNumOrDash(r.citations_delta_total, 0), fmtSecOrDash(r.elapsed_ms_avg),
    reviewCounts(r.claims), reviewCounts(r.limits), reviewCounts(r.verdicts), reviewCounts(r.stops), reviewCounts(r.missing_codes),
  ]));
  $('review-stats').innerHTML = (!(rounds?.by_depth_provider?.length || rounds?.by_round?.length || quality?.by_rounds?.length)
    ? '<p class="hint">見直しの機能はこの環境では未導入です。</p>' : '')
    + '<h3>深さ・使った AI ごと</h3>' + roundTable(rounds?.by_depth_provider ?? [], false)
    + '<h3>何回目の見直しかで比べる</h3>' + roundTable(rounds?.by_round ?? [], true)
    + '<h3>正解付きの比較</h3>'
    + (quality?.period
      ? `<p class="hint">集計期間: ${esc(quality.period.start ?? '不明')} 〜 ${esc(quality.period.end ?? '不明')}。見直しの記録とは別に採点した結果です。</p>`
      : '')
    + table(['条件', '見直しの回数', '比較件数', '正解', '誤った断定', '回答漏れ', '以前より悪化', '未採点', '費用合計（米ドル）'],
      (quality?.by_rounds ?? []).map(r => [
        Object.hasOwn(REVIEW_CONDITION_LABELS, r.condition) ? REVIEW_CONDITION_LABELS[r.condition] : '不明',
        r.rounds, r.runs, r.correct, r.wrong_assertion, r.missing, r.regressed, r.unrated,
        fmtNumOrDash(r.cost_usd_total, 2)]));
}

function renderWeeklyAndRetention(retention) {
  const weekly = (retention && retention.weekly) || [];
  renderTrendChart(
    $('chart-weekly-svg'), $('chart-weekly-empty'), $('chart-weekly-tip'), $('chart-weekly-wrap'),
    weekly.map((w) => ({ date: w.week_start, value: w.active_users })),
    { color: 'var(--accent)', unit: '人', label: '週次アクティブユーザー数' },
  );
  const rate = retention && retention.revisit_rate;
  $('revisit-rate-val').textContent = (rate === null || rate === undefined)
    ? '算出できません（データ不足）' : `${Math.round(rate * 100)}%`;
}

function renderDownloadsChart(downloads, period) {
  const dailyForFill = ((downloads && downloads.daily) || []).map((d) => ({ date: d.date, turns: d.count }));
  const filled = fillDailySeries(dailyForFill, period && period.start, period && period.end);
  renderTrendChart(
    $('chart-dl-svg'), $('chart-dl-empty'), $('chart-dl-tip'), $('chart-dl-wrap'),
    filled.map((d) => ({ date: d.date, value: d.turns })),
    { color: 'var(--ok)', unit: '件', label: '原本ダウンロード数（日別）' },
  );
  // 見出し脇に期間合計を表示（日別グラフだけだと合計が読み取りにくい）。
  const total = (downloads && downloads.total) || 0;
  $('dl-total-badge').textContent = `期間合計 ${total.toLocaleString('ja-JP')}件`;
}

