// 利用統計画面のデータ取得・イベント登録・初期化。最後に読む。
'use strict';

// ===== データ取得 =====
async function load(period) {
  const seq = ++_loadSeq;   // このリクエストの連番（連打時、最新以外の描画は破棄する）
  _period = period;
  _stats = null;
  $('period-range-error').textContent = '';
  _loadedAt = null;
  $('usage-export').disabled = true;
  $('usage-export-detail').disabled = true;
  $('usage-stat-panels').hidden = true;
  $('usage-period-label').textContent = `${periodText(period)}を取得中…`;
  $('usage-load-status').textContent = '利用統計を読み込んでいます…';
  setLoading();
  $('review-stats').textContent = '見直しの集計を読み込んでいます…';
  document.querySelectorAll('.period-bar .filterchip').forEach((b) => {
    const on = !period.start && Number(b.dataset.days) === period.days;
    b.classList.toggle('on', on);
    b.setAttribute('aria-pressed', String(on));
  });
  if (period.start) { $('period-start').value = period.start; $('period-end').value = period.end; }
  const query = periodApiQuery(period);
  try {
    const d = await getJSON('/admin/usage/stats?' + query);
    if (seq !== _loadSeq) return;   // 後から連打された別リクエストが既に最新＝このレスポンスは古い
    // 画面とJSON保存で同じ範囲を共有する（サーバのトークン降順を維持）。
    if (d.tokens?.by_user) d.tokens.by_user = d.tokens.by_user.slice(0, 10);
    renderSummary(d.totals || {});
    renderZeroHitTile(d.zero_hit);
    renderCharts(d.daily || [], d.period);
    renderHeatmap(d.heatmap || []);
    renderWorldBar(d.worlds || []);
    renderProviderBar(d.providers || []);
    renderWeeklyAndRetention(d.retention || {});
    renderDownloadsChart(d.downloads || {}, d.period);
    renderStopKinds(d.stop_kinds || [], d.stopped_turns);
    renderCompletions(d.completions || []);
    renderConversationTurns(d.conversation_turns || {}, d.resume_rate);
    renderResponseTime(d.response_time || {});
    renderLimits(d.limits || {});
    renderToolUse(d.tool_calls || {}, d.impact || {});
    renderReviewStats(d.rounds, d.quality_runs);
    renderTokens(d.tokens || {}, d.period);
    renderConversationsTop(d.conversations_top || []);
    _users = d.users || [];
    applySortAndRender();
    _stats = d;
    _loadedAt = new Date().toISOString();
    $('usage-period-label').textContent = `${d.period.start} ～ ${d.period.end}（JST・終了日を含む）`;
    if (!$('period-range-error').textContent) {   // 送信前に弾いた入力と理由は残す
      $('period-start').value = d.period.start;
      $('period-end').value = d.period.end;
    }
    $('usage-load-status').textContent = `取得時刻: ${new Date(_loadedAt).toLocaleString('ja-JP', { timeZone: 'Asia/Tokyo' })} JST`;
    $('usage-stat-panels').hidden = false;
    $('usage-export').disabled = false;
    $('usage-export-detail').disabled = false;
    $('usage-tbody').setAttribute('aria-busy', 'false');
    showUsageTab(usageView().tab);
  } catch (e) {
    if (seq !== _loadSeq) return;
    $('usage-period-label').textContent = `${periodText(period)}（取得失敗）`;
    $('usage-load-status').textContent = `利用統計を取得できませんでした: ${String(e)}。期間ボタンで再取得できます。`;
    $('usage-tbody').setAttribute('aria-busy', 'false');
    $('usage-tbody').innerHTML = `<tr><td colspan="8" style="color:var(--danger);padding:16px">読み込みに失敗しました: ${esc(String(e))}</td></tr>`;
    $('review-stats').textContent = '見直しの集計を読み込めませんでした。';
    toast('利用統計の読み込みに失敗しました');
  }
}

$('usage-export').addEventListener('click', () => {
  const payload = { retrieved_at: _loadedAt, timezone: 'Asia/Tokyo', period: _stats.period,
    definitions: $('usage-definitions').textContent.trim(), stats: _stats };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
  Sherpa.downloadBlob(blob, `usage-${_stats.period.start}-${_stats.period.end}.json`);
});

$('usage-export-detail').addEventListener('click', async () => {
  const btn = $('usage-export-detail');
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = '作成中…';
  $('usage-load-status').textContent = '明細ZIPを作成しています…';
  try {
    const r = await fetch('/admin/usage/export?' + periodApiQuery(_period));
    if (!r.ok) {
      let data = null;
      try { data = await r.json(); } catch (_) { data = null; }
      throw new Error((data && data.detail) || `エラー (${r.status})`);
    }
    const blob = await r.blob();
    const cd = r.headers.get('content-disposition') || '';
    const m = cd.match(/filename="([^"]+)"/);
    const name = m ? m[1] : 'usage-detail.zip';
    Sherpa.downloadBlob(blob, name);
    $('usage-load-status').textContent = `明細ZIPを保存しました: ${name}`;
  } catch (e) {
    $('usage-load-status').textContent = `明細ZIPの作成に失敗しました: ${String(e)}`;
    toast('明細ZIPの作成に失敗しました');
  } finally {
    btn.disabled = !_stats;   // 作成中に期間が変わり読み込みが失敗していたら押せる状態へ戻さない
    btn.textContent = label;
  }
});

// ===== イベント =====
document.querySelectorAll('.period-bar .filterchip').forEach((b) => {
  b.addEventListener('click', () => {
    selectPeriod({ days: Number(b.dataset.days) });
  });
});
$('period-start').max = todayJst();
$('period-end').max = todayJst();
$('period-range').addEventListener('submit', (event) => {
  event.preventDefault();
  const start = $('period-start').value, end = $('period-end').value;
  const error = rangeError(start, end);
  if (error) { $('period-range-error').textContent = error; return; }
  selectPeriod({ start, end });
});

document.querySelectorAll('th.sortable').forEach((th) => {
  th.addEventListener('click', () => {
    const key = th.dataset.sort;
    if (_sortKey === key) {
      _sortDir = _sortDir === 'desc' ? 'asc' : 'desc';
    } else {
      _sortKey = key;
      _sortDir = 'desc';
    }
    applySortAndRender();
  });
});

// 行クリックで lens 内訳などを展開（委譲・見つけやすさ重視）
$('usage-tbody').addEventListener('click', (e) => {
  const row = e.target.closest('tr.u-row');
  if (!row) return;
  row.classList.toggle('u-open');
});

// ===== 初期化 =====
(async () => {
  const isAdmin = await checkAdmin();
  if (isAdmin !== 'admin') {
    const main = $('main-content');
    const denied = $('access-denied');
    if (main) main.style.display = 'none';
    if (denied) denied.style.display = 'block';
    if (isAdmin === 'unreachable') showUnreachable(denied);
    return;
  }
  _adminReady = true;
  const view = usageView();
  if (view.periodError) showPeriodError(view);
  if (!view.periodError) await load(view.period);
})();
