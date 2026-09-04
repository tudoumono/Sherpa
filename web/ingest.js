// 資料（統合画面・S3-A）＝「資料フォルダの登録・取り込み」と「取り込み状況の確認」を1画面に統合。
//   上＝資料フォルダ（登録フォーム＋登録済み一覧・差分/更新/削除。旧 ingest-new.js）
//   下＝取り込み状況（文書一覧・状態・フォルダツリー・全文検索・原本DL。旧 M10・04-画面の原則.md §3.3）
// 旧 ingest-new.html はこの画面へのリダイレクトに縮退（挙動は不変）。専門用語は出さず状態3つに集約。
// XSS: 全データ esc()・インラインハンドラ無し（委譲）。API は相対パス。
'use strict';

const $ = Sherpa.$, esc = Sherpa.esc, api = Sherpa.api, getJSON = Sherpa.getJSON, analyzerLabel = Sherpa.analyzerLabel;     // 共通ユーティリティ（nav.js・RV DRY）

// UI フィードバック5（2026-07-03）: 読み込み中表示を統一（既存の .loading-inline/spinner 流儀）。
const _LOADING_INLINE = '<div class="loading-inline" role="status"><span class="spinner spinner-sm"></span><span>読み込み中...</span></div>';

// =====================================================================
// 上: 資料フォルダ（登録・一覧）＝旧 ingest-new.js。鏡モデル・専門用語ゼロ。
// フォルダを「選んで」追加／今すぐ更新（全削除して作り直し）／削除。パスは手入力させず picker で選ぶ。
// =====================================================================

// ---- /worlds 取得の共有（RV Med3・2026-07-08: 初期ロード時の loadList と下段セレクタ初期化の
// 同時多発フェッチを1本化）。同じ tick 内の複数呼び出しは同一 Promise を返す（結果はキャッシュしない
// ＝完了すれば次回呼び出しは必ず新規フェッチ＝mutation 後も古いデータを掴まない）。
// 行ごとの /worlds/{id}/status（loadStat）は件数に比例するファンアウトの性質を持つが、これは旧
// ingest-new 画面から不変のもの＝今回は対象外（据え置き・RV Med3）。
let _worldsInFlight = null;
function fetchWorldsShared() {
  if (!_worldsInFlight) {
    _worldsInFlight = api('GET', '/worlds')
      .then((d) => d.worlds || [])
      .finally(() => { _worldsInFlight = null; });
  }
  return _worldsInFlight;
}

// ---- 登録中のフォルダ（単一World・登録済みなら1件だけ表示） ----
// 資料フォルダは全体で1本（決定 2026-08-15）。未登録なら登録カード、登録済みなら現在のフォルダと
// 操作（更新／削除）だけを出す＝更新の入口を1つにする。
async function loadList() {
  $('list').setAttribute('aria-busy', 'true');
  $('list').innerHTML = '<div class="loading" role="status" style="padding:14px 0"><span class="spinner spinner-sm"></span><span>資料フォルダを読み込んでいます...</span></div>';
  try {
    const ws = await fetchWorldsShared();
    const w = ws[0] || null;
    $('regcard').hidden = !!w;                          // 登録済みなら登録フォームは出さない
    $('currentcard').hidden = !w;
    if (!w) {
      $('list').innerHTML = '';
      return;
    }
    $('list').innerHTML = `<div class="row" data-wid="${esc(w.world_id)}" data-path="${esc(w.root_path)}">`
      + `<div class="rowmain"><span class="nm">${esc(w.label || w.world_id)}</span>`
      + `<span class="pth" title="${esc(w.root_path)}">${esc(w.root_path)}</span>`
      + `<button class="btn-primary mini" data-refresh="${esc(w.world_id)}">更新</button>`
      + `<button class="btn-ghost mini" data-rag-rules="${esc(w.world_id)}" title="AI が読みやすく整えた検索用データを、AI を使わない元の形へ作り直します（監査等で AI 出力を一掃したい場合）">規則版で再生成</button>`
      + `<button class="btn-ghost mini danger" data-del="${esc(w.world_id)}" title="検索用データのみ削除（元のフォルダ・ファイルは消えません）">削除</button></div>`
      + `<div class="rowstat" data-stat="${esc(w.world_id)}"><span class="loading-inline" role="status"><span class="spinner spinner-sm"></span><span>状況を確認中...</span></span></div></div>`;
    loadStat(w.world_id);                               // 入った件数/未対応/グラフ
  } catch (e) {
    $('list').innerHTML = `<div class="danger">登録情報を取得できません: ${esc(e.message)}</div>`;
    $('currentcard').hidden = false;
  } finally {
    $('list').setAttribute('aria-busy', 'false');
  }
}

// ---- 追加 ----
let _chosen = null;            // 追加フォーム用に選んだフォルダ path
function setChosen(p) {
  _chosen = p || null;
  $('chosen').innerHTML = '選択中のフォルダ: <b>' + (p ? esc(p) : '（未選択）') + '</b>';
  $('regbtn').disabled = !p;
  $('diffbtn').disabled = !p;
  $('diffout').innerHTML = '';                       // フォルダを変えたら前の差分結果は消す
  if (p && !$('label').value.trim()) $('label').value = p.split('/').filter(Boolean).pop() || '';
}

async function register() {
  if (!_chosen) return;
  const path = _chosen, label = $('label').value.trim();
  $('regmsg').textContent = ''; $('regbtn').disabled = true; $('diffout').innerHTML = '';
  try {
    // ING-3: 登録は即受付・取り込みは背景で継続する（`run_id`/`joined` のみ返る）。完了状況は
    // 下段（取り込み状況）の行が数秒間隔でポーリングして表示する（`loadStat` 参照）。
    const res = await api('POST', '/worlds', { path, label: label || null });
    $('regmsg').innerHTML = `✓ ${esc(res.note)}`;
    // ②是正（利用者報告 2026-09-03）: 受付直後は世界行がまだ `GET /worlds` に現れない（背景で
    // 作成中）——`trackNewRegistration` がそれを検出するまで待たせず、受付応答自身が返す
    // `world_id` で楽観的なプレースホルダ行を即時表示する。実際の行（操作ボタン付き）は
    // 行が現れ次第 `reloadAll` の `loadList()` が上書きする（既存の run 追跡機構の範囲内・
    // 新しいポーリングは増やさない）。
    showOptimisticRegisteredRow(res.world_id, label || path.split('/').filter(Boolean).pop() || res.world_id, path);
    setChosen(null); $('label').value = '';
    trackNewRegistration(res.world_id, res.run_id);
  } catch (e) {
    $('regmsg').innerHTML = `<span class="danger">取り込めません: ${esc(e.message)}</span>`;
    $('regbtn').disabled = !_chosen;
  }
}

function showOptimisticRegisteredRow(worldId, label, path) {
  $('regcard').hidden = true;
  $('currentcard').hidden = false;
  $('list').innerHTML = `<div class="row" data-wid="${esc(worldId)}" data-path="${esc(path)}">`
    + `<div class="rowmain"><span class="nm">${esc(label)}</span>`
    + `<span class="pth" title="${esc(path)}">${esc(path)}</span></div>`
    + `<div class="rowstat" data-stat="${esc(worldId)}"><span class="loading-inline" role="status">`
    + `<span class="spinner spinner-sm"></span><span>取り込み中…（登録処理を開始しています）</span></span></div></div>`;
}

// 未登録フォルダの新規登録は World 行自体が背景（`worlds.register`）で作られるため、受付直後は
// 通常の /worlds 一覧・status にまだ現れない——行が現れるまでは受付 run（run_id）自身を
// /ingest/runs で追跡する（既存の run_id は世界行と無関係に検索できる）。行が現れたら通常の
// reloadAll/loadStat のポーリングへ切り替える。行が現れないまま run が terminal（failed）に
// 達した場合は登録失敗として表示する（例: 極小窓の同時登録競合）。
async function trackNewRegistration(worldId, runId) {
  try {
    const ws = await fetchWorldsShared();
    if (ws.some((w) => w.world_id === worldId)) {
      reloadAll(worldId);
      return;
    }
    const { runs } = await api('GET', `/ingest/runs?world=${encodeURIComponent(worldId)}`);
    const run = (runs || []).find((r) => r.id === runId);
    if (run && run.status !== 'extracting') {
      $('regmsg').innerHTML = `<span class="danger">登録に失敗しました（状態: ${esc(run.status)}）。`
        + `もう一度お試しください。</span>`;
      $('regbtn').disabled = !_chosen;
      reloadAll();
      return;
    }
    setTimeout(() => trackNewRegistration(worldId, runId), 3000);
  } catch (e) {
    $('regmsg').innerHTML = `<span class="danger">状況を確認できません: ${esc(e.message)}</span>`;
    reloadAll();
  }
}

// ---- 差分チェック（read-only・登録しない）----
function _difflist(title, arr) {
  if (!arr || !arr.length) return '';
  const cap = 50, shown = arr.slice(0, cap).map((r) => `<li>${esc(r)}</li>`).join('');
  const more = arr.length > cap ? `<div class="muted">…他 ${arr.length - cap} 件</div>` : '';
  return `<div><b>${esc(title)}（${arr.length}）</b><ul>${shown}</ul>${more}</div>`;
}
function renderDiff(el, d, where) {
  const a = (d.added || []).length, c = (d.changed || []).length, r = (d.removed || []).length;
  const head = where ? `<div class="muted" style="margin-bottom:6px">${esc(where)}</div>` : '';
  if (!a && !c && !r) {
    el.innerHTML = head + `<div class="diffsum">差分なし（取り込み済みと同じ ／ 現在 ${esc(d.total)} 件）</div>`
      + (d.registered ? '' : `<div class="difflist muted">※ まだ未登録のフォルダです。</div>`);
    return;
  }
  el.innerHTML = head
    + `<div class="diffsum"><span class="pill add">追加 ${a}</span><span class="pill chg">変更 ${c}</span>`
    + `<span class="pill del">削除 ${r}</span>現在 ${esc(d.total)} 件 / 取込済 ${esc(d.indexed)} 件</div>`
    + `<div class="difflist">${_difflist('追加', d.added)}${_difflist('変更', d.changed)}${_difflist('削除', d.removed)}</div>`
    + (d.registered ? '' : `<div class="difflist muted">※ 未登録。「このフォルダを登録」で ${a} 件を取り込みます。</div>`);
}

async function checkDiff() {                          // 追加カード: 選んだフォルダを登録せず確認
  if (!_chosen) return;
  $('diffout').innerHTML = '<span class="loading-inline" role="status"><span class="spinner spinner-sm"></span><span>差分を確認中...</span></span>';
  try {
    renderDiff($('diffout'), await api('POST', '/worlds/diff', { path: _chosen }));
  } catch (e) {
    $('diffout').innerHTML = `<span class="danger">差分を取得できません: ${esc(e.message)}</span>`;
  }
}

// ---- 取り込み状況の要約（正直化: 入った/未対応/グラフ）----
function summaryText(s) {
  if (!s) return '';
  const p = [`文書 ${esc(s.indexed)} 件を検索可能に`];
  if (s.office_md) p.push(`うち Office ${esc(s.office_md)} 件をテキスト化`);
  if (s.skipped_office) p.push(`未対応 ${esc(s.skipped_office)} 件（PDF/旧形式）`);
  if (s.office_failed) p.push(`変換失敗 ${esc(s.office_failed)} 件`);
  // accepts() 全滅（担当アナライザは居たが内容判定で不採用）の内訳: 既存の資料種別に該当すれば
  // 「資料扱い」（indexed に含まれる）、該当しなければ「未対応」（skipped_other に含まれる）
  // ——§7 裁定10「既存の資料種別に該当するものは資料・それ以外は未対応」を可視化する。
  if (s.analyzer_declined_as_document || s.analyzer_declined) {
    const declined = [];
    if (s.analyzer_declined_as_document) declined.push(`担当なし（資料扱い）${esc(s.analyzer_declined_as_document)} 件`);
    if (s.analyzer_declined) declined.push(`未対応 ${esc(s.analyzer_declined)} 件`);
    p.push(declined.join('／'));
  }
  if (s.skipped_other) p.push(`除外 ${esc(s.skipped_other)} 件`);
  p.push(`関係グラフ ${esc(s.graph_nodes)} 件`);
  if (s.es_chunks != null) p.push(`全文検索 ${esc(s.es_chunks)} 片`);
  else if (s.indexed > 0) p.push('全文検索 未接続');
  return p.join(' ／ ');
}

// ING-2: 件数の集計時刻＋再集計ボタン（`GET /worlds/{id}/status` はキャッシュを読むだけでフォルダを
// 歩かない・保存済み集計が無い world は counts_as_of=null＝「未集計」を促す）。
function countsAsOfNote(s, wid) {
  const at = s.counts_as_of ? `（${esc(Sherpa.fmtDateTime(s.counts_as_of))} 時点）` : '（未集計）';
  return ` <span class="muted" data-countsof="${esc(wid)}">${at}</span>`
    + ` <button class="mini" data-recount="${esc(wid)}">再集計</button>`;
}

// ING-1: 理由コード → 平文（`failure_reason_catalog` はサーバ側の単一の真実源・docs/04 平文原則。
// 未知コードは raw のまま出す＝fail-open）。
function reasonInfo(catalog, code) {
  return (catalog && catalog[code]) || { label: code, advice: '' };
}

function stageSummaryHtml(stage) {
  if (!stage) return '';
  const lines = [];
  if (stage.office_md) {
    lines.push(`MD変換: 変換 ${esc(stage.office_md.converted)} 件・失敗 ${esc(stage.office_md.failed)} 件・`
      + `未対応 ${esc(stage.office_md.unsupported)} 件`);
  }
  if (stage.es) {
    lines.push(`全文検索: ${stage.es.chunks != null ? esc(stage.es.chunks) + ' 片' : '-'}`
      + (stage.es.error ? `（エラー: ${esc(stage.es.error)}）` : ''));
  }
  if (stage.neo4j) {
    lines.push(`関係グラフ: ノード ${esc(stage.neo4j.nodes)} 件・関係 ${esc(stage.neo4j.edges)} 件`
      + (stage.neo4j.duration_sec != null ? `（${esc(stage.neo4j.duration_sec)} 秒）` : ''));
  }
  return lines.length ? `<div class="ingest-stage"><b>各段の要約</b><ul>${lines.map((l) => `<li>${l}</li>`).join('')}</ul></div>` : '';
}

function failedFilesHtml(wid, ff, catalog) {
  if (!ff || !ff.items || !ff.items.length) return '';
  const rows = ff.items.map((it) => {
    const info = reasonInfo(catalog, it.reason);
    return `<li><span class="fname">${esc(it.doc)}</span> — ${esc(info.label)}`
      + (info.advice ? `<div class="muted" style="font-size:11.5px">${esc(info.advice)}</div>` : '')
      + `<button class="mini" data-reconvert-wid="${esc(wid)}" data-rel="${esc(it.doc)}">再変換</button></li>`;
  }).join('');
  const more = ff.truncated ? `<div class="muted">…他 ${esc(ff.total - ff.items.length)} 件</div>` : '';
  return `<div class="ingest-failed"><b>変換失敗 ${esc(ff.total)} 件</b><ul>${rows}</ul>${more}</div>`;
}

function partialSuspectedHtml(ps, advice) {
  if (!ps || !ps.items || !ps.items.length) return '';
  const rows = ps.items.map((it) => `<li>${esc(it.doc)}</li>`).join('');
  const more = ps.truncated ? `<div class="muted">…他 ${esc(ps.total - ps.items.length)} 件</div>` : '';
  return `<div class="ingest-partial"><b>抽出不完全の疑い（要確認） ${esc(ps.total)} 件</b>`
    + (advice ? `<div class="muted" style="font-size:11.5px">${esc(advice)}</div>` : '')
    + `<ul>${rows}</ul>${more}</div>`;
}

// 失敗一覧／各段の要約／抽出不完全の疑いを1つの折りたたみへまとめる（新しい詳細画面は作らない・
// 資料画面の各行の下に出す）。何も無ければ折りたたみ自体を出さない。
function ingestDetailHtml(wid, s) {
  const body = stageSummaryHtml(s.stage_summary)
    + failedFilesHtml(wid, s.failed_files, s.failure_reason_catalog)
    + partialSuspectedHtml(s.partial_extraction_suspected, s.partial_extraction_advice);
  if (!body) return '';
  return `<details class="adv"><summary>詳細を表示</summary>${body}</details>`;
}
// ING-3: 実行中 run の逐次進捗（`running_progress`）。段の平文はサーバ側で確定済み（`stage_label`）。
// ステッパー表示（2026-09-04 実環境フィードバック）: 数時間級の取り込みで「あと何段あるのか」が
// 見えるよう、済んだ段✓・いまの段▶（件数付き）・残りの段を1行に並べる。段の並びはサーバの
// `worker.STAGE_LABELS` の取り込み系5段のミラー（キーが未知の段＝削除等は従来の1行表示へ
// フォールバック＝ズレても壊れない）。ラベルはステッパー用の短縮形（正式な平文は stage_label）。
const INGEST_STAGE_STEPS = [
  ['scanning', 'フォルダ確認'],
  ['office_md', '読める写し(MD)作成'],
  ['graph_build', '関係グラフ'],
  ['es_index', '全文索引・ベクトル化'],
  ['finalize', '仕上げ'],
];
function progressNote(s) {
  const p = s && s.running_progress;
  if (!p) return '';
  // total 不明の段（走査中）は件数のみ「N件確認済み」表示（実環境フィードバック 2026-09-04）。
  const counts = (p.done != null && p.total != null) ? `（${esc(p.done)}/${esc(p.total)}）`
    : (p.done != null ? `（${esc(p.done)}件確認済み）` : '');
  const idx = INGEST_STAGE_STEPS.findIndex(([k]) => k === p.stage);
  if (idx < 0) {                                   // 未知の段（accepted/deleting 等）＝従来の1行表示
    return `<div class="muted" style="margin-top:3px"><span class="loading-inline" role="status">`
      + `<span class="spinner spinner-sm"></span><span>${esc(p.stage_label)}${counts}</span></span></div>`;
  }
  const steps = INGEST_STAGE_STEPS.map(([k, label], i) => {
    if (i < idx) return `<span class="step done">✓ ${esc(label)}</span>`;
    if (i === idx) return `<span class="step now" role="status"><span class="spinner spinner-sm"></span>${esc(label)}${counts}</span>`;
    return `<span class="step todo">${esc(label)}</span>`;
  }).join('<span class="step-arrow">→</span>');
  return `<div class="muted ingest-steps" style="margin-top:3px">${steps}</div>`;
}
function summaryNote(s, wid) {
  if (!s) return '';
  const hints = [];
  if (s.skipped_office) {
    const exts = Object.entries(s.skipped_ext || {})
      .filter(([k]) => /^\.(pdf|doc|xls|ppt)$/i.test(k))
      .map(([k, v]) => `${esc(k)} ${esc(v)}`).join('・');
    hints.push(`PDF・旧形式（${exts}）はまだ未対応です（今後対応）`);
  }
  if (s.office_failed) hints.push(`${esc(s.office_failed)} 件は変換に失敗しました（ファイル破損などの可能性）`);
  if (s.graph_nodes === 0 && s.indexed > 0) {
    hints.push('関係グラフはソースコード（COBOL/JCL等）から作られます。文書のみのフォルダでは空です（文書は検索で使えます）');
  }
  const warns = s.last_run_warnings || [];
  const dangers = [];
  if (s.last_run_status === 'failed') {
    // `failed` は派生物の公開後（グラフ反映・台帳更新等）の失敗も含みうるため、「検索は前回成功
    // 時点のまま」とは断定しない（派生物自体は今回分に更新済みのことがある）。
    dangers.push('前回の取り込みは失敗しました（次回の取り込みで自動的に再試行されます）');
  }
  if (warns.some(w => typeof w === 'string' && w.startsWith('es_index_failed'))) {
    dangers.push('前回の取り込みで全文検索への反映に失敗しました。検索結果が古い可能性があります（「今すぐ更新」でやり直せます）');
  }
  if (warns.some(w => typeof w === 'string' && w.startsWith('reconcile_failed'))) {
    dangers.push('前回の取り込みで不要ファイルの掃除に失敗しました（次回の取り込みで再試行されます）');
  }
  if (warns.some(w => typeof w === 'string' && w.startsWith('office_md:'))) {
    dangers.push('前回の取り込みでOffice文書のテキスト化処理自体に問題がありました（次回の取り込みで再試行されます）');
  }
  // `office_md_blocked:{doc}\t{reason}`（区切りはタブ・doc/reasonとも`:`を含みうるため`:`では分割しない）。
  const blockedDocs = warns
    .filter(w => typeof w === 'string' && w.startsWith('office_md_blocked:'))
    .map((w) => {
      const rest = w.slice('office_md_blocked:'.length);
      const tab = rest.indexOf('\t');
      return tab >= 0 ? rest.slice(0, tab) : rest;
    })
    .filter(Boolean);
  if (blockedDocs.length) {
    dangers.push(`前回の取り込みで一部の文書（${blockedDocs.map(esc).join('・')}）を想定外のエラーで`
      + '変換できませんでした（次回の取り込みで再試行されます）');
  }
  // 不可読コードによる全体停止（`unreadable_code_file`）: 対象ファイルを名指しする
  // （`last_run_warnings` は reason のみ＝doc が届かないため `last_run_blocked` を使う）。
  const unreadableDocs = (s.last_run_blocked || [])
    .filter((b) => b && b.reason === 'unreadable_code_file' && b.doc)
    .map((b) => b.doc);
  if (unreadableDocs.length) {
    dangers.push(`⚠ 取り込みを止めました: ${unreadableDocs.map(esc).join('・')} を読み取れませんでした（やり直す）`);
  }
  const note = hints.length ? `<div class="muted" style="margin-top:3px">※ ${hints.join('。')}</div>` : '';
  const dangerNote = dangers.length ? `<div class="danger" style="margin-top:3px">※ ${dangers.join('。')}</div>` : '';
  const countsNote = wid ? countsAsOfNote(s, wid) : '';
  const detail = wid ? ingestDetailHtml(wid, s) : '';
  return `<span>${summaryText(s)}</span>${countsNote}${progressNote(s)}${note}${dangerNote}${detail}`;
}
// ING-3: 実行中（`running_progress` あり）は行の操作ボタンを無効化する（多重クリックはサーバ側の
// world 単位の単一実行〔既存 run への合流〕で安全だが、UI 側でも明示的に抑止する）。
function setIngestBusy(world_id, busy) {
  document.querySelectorAll(`[data-refresh="${world_id}"],`
    + `[data-rag-rules="${world_id}"],[data-del="${world_id}"]`)
    .forEach((b) => { b.disabled = busy; });
}

// ING-3b（利用者報告 2026-09-04）: 登録ボタン（`pickbtn`）は上の行ボタンと違い world_id に
// 紐付かない（登録前は世界がまだ無い）ため、`loadStat` が集計した「実行中の world_id 集合」で
// 管理する——`worlds.register` は登録処理全体（多くの場合 es_index 段を含み数時間かかりうる）を
// グローバル advisory lock の下で行うため、実行中に別の登録を投げると新規リクエストが完了まで
// ブロックされてしまう（サーバ側で弾かれず「固まって見える」）。Set のまま（world 単位で複数を
// 素朴に集計するだけ）にしておき、現行の単一 world 運用が将来複数に広がっても書き直し不要にする。
const _runningWorldIds = new Set();
function _updatePickbtnState() {
  const b = $('pickbtn');
  if (!b) return;
  const busy = _runningWorldIds.size > 0;
  b.disabled = busy;
  b.title = busy ? '取り込みの実行中は登録できません' : '';
}

async function loadStat(world_id) {                   // 各行の状況を非同期で表示（実行中は自己ポーリング）
  const el = document.querySelector(`[data-stat="${world_id}"]`);
  if (!el) return;
  try {
    const s = await api('GET', `/worlds/${encodeURIComponent(world_id)}/status`);
    el.innerHTML = summaryNote(s, world_id);
    const running = !!s.running_progress;
    setIngestBusy(world_id, running);
    if (running) _runningWorldIds.add(world_id); else _runningWorldIds.delete(world_id);
    _updatePickbtnState();
    if (running) setTimeout(() => loadStat(world_id), 3000);   // 実行中の間だけ数秒間隔で追跡
  } catch (e) {
    if (e && e.status === 404) {          // 削除完了＝world 行自体が消えた（一覧側の表示を戻す）
      _runningWorldIds.delete(world_id);
      _updatePickbtnState();
      reloadAll();
      return;
    }
    el.innerHTML = '<span class="muted">状況を取得できませんでした</span>';
  }
}


// ---- 削除 ----
async function removeWorld(world_id) {
  const w = (await fetchWorldsShared()).find((x) => x.world_id === world_id) || {};
  if (!confirm(`「${(w.label || world_id)}」の登録を解除します。\n\n`
    + `削除されるのは Sherpa の検索用データ（索引・テキスト化した写し・関係グラフ）だけです。\n`
    + `元の Windows フォルダとファイルは一切消えません。\n\n`
    + `削除すると、別のフォルダを登録できるようになります。よろしいですか？`)) return;
  $('listmsg').innerHTML = '<span class="loading-inline" role="status"><span class="spinner spinner-sm"></span><span>削除を受け付けています...</span></span>';
  try {
    // ING-3: 削除も即受付・派生物wipeは背景で継続する。行の「削除中」表示は loadStat のポーリングが
    // 示し、完了（world 行が消えて status が 404 になる）を検知すると一覧を自動で再同期する。
    const res = await api('DELETE', `/worlds/${encodeURIComponent(world_id)}`);
    $('listmsg').innerHTML = `✓ ${esc(res.note)}`;
    reloadAll();
  } catch (e) {
    $('listmsg').innerHTML = `<span class="danger">削除できません: ${esc(e.message)}</span>`;
  }
}

// ---- 今すぐ更新（変更検知して再取り込み）----
async function refresh(world_id) {
  $('listmsg').innerHTML = '<span class="loading-inline" role="status"><span class="spinner spinner-sm"></span><span>更新を受け付けています...</span></span>';
  try {
    // ING-3: 更新は即受付・再取り込みは背景で継続する。進捗・完了は行のポーリング（loadStat）が示す。
    const res = await api('POST', `/worlds/${encodeURIComponent(world_id)}/refresh`);
    $('listmsg').innerHTML = `✓ ${esc(res.note)}`;
    // 更新受付後は下段も再同期し、対象の資料フォルダの状況を表示する。
    reloadAll(world_id);
  } catch (e) {
    $('listmsg').innerHTML = `<span class="danger">更新できません: ${esc(e.message)}</span>`;
  }
}

// ---- 再集計（ING-2・`corpus_docs.scan_report` を明示的にやり直す唯一の実走査）----
async function recount(world_id) {
  const el = document.querySelector(`[data-stat="${world_id}"]`);
  if (el) el.innerHTML = '<span class="loading-inline" role="status"><span class="spinner spinner-sm"></span><span>集計しています...</span></span>';
  try {
    await api('POST', `/worlds/${encodeURIComponent(world_id)}/recount`);
  } catch (e) {
    if (el) el.innerHTML = `<span class="danger">集計できません: ${esc(e.message)}</span>`;
    return;
  }
  loadStat(world_id);
}

// ---- 再変換（ING-1・失敗一覧の1件をやり直す＝更新と同じ world 全体 sync が走る）----
async function reconvertFile(world_id, rel) {
  if (!confirm(`「${rel}」を再変換します。\n\n更新（今すぐ取り込み直す）と同じ処理が資料フォルダ全体に対して走ります。続けますか？`)) return;
  const el = document.querySelector(`[data-stat="${world_id}"]`);
  if (el) el.innerHTML = '<span class="loading-inline" role="status"><span class="spinner spinner-sm"></span><span>再変換しています...</span></span>';
  try {
    await api('POST', `/worlds/${encodeURIComponent(world_id)}/reconvert`, { rel });
  } catch (e) {
    if (el) el.innerHTML = `<span class="danger">再変換できません: ${esc(e.message)}</span>`;
    return;
  }
  loadStat(world_id);
}

// ---- 規則版で再生成（L5・§8.6-2「規則版で再生成」管理操作・AI 成形の一掃）----
async function regenerateRagRules(world_id) {
  const w = (await fetchWorldsShared()).find((x) => x.world_id === world_id) || {};
  if (!confirm(`「${w.label || world_id}」の検索用データを、AI を使わない元の形（規則版）へ作り直します。\n`
    + `AI が読みやすく整えた版は消えます（AI 成形は無効化していない限り、後で改めて作られることがあります）。続けますか？`)) return;
  $('listmsg').innerHTML = '<span class="loading-inline" role="status"><span class="spinner spinner-sm"></span><span>規則版への再生成を受け付けています...</span></span>';
  try {
    // ING-3: 即受付・作り直しは背景で継続する。進捗・完了は行のポーリング（loadStat）が示す。
    const res = await api('POST', `/worlds/${encodeURIComponent(world_id)}/rag_regenerate_rules`, {});
    $('listmsg').innerHTML = `✓ ${esc(res.note)}`;
    reloadAll(world_id);
  } catch (e) {
    $('listmsg').innerHTML = `<span class="danger">規則版へ再生成できません: ${esc(e.message)}</span>`;
  }
}

// 業務語↔コードの対応づけは GRAPH-SRC（2026-09-04・K9-K11）で辞書突合による言及エッジ（S2）へ
// 置き換え済み。旧・LLM 提案の承認フロー（手動「業務語↔コード対応」ボタン・自動橋渡し・
// /worlds/{id}/concepts/* API）は概念ごと撤去し、復活させない。

// ---- フォルダ選択モーダル（追加フォーム用）----
let _mode = null;              // {kind:'register'}（rebind UI は撤去）
let _cur = '';                // 現在表示中のパス（''=トップ＝ドライブ一覧）

async function openPicker(mode) { _mode = mode; await showDir(mode.start || ''); $('ovl').classList.add('open'); }
function closePicker() { $('ovl').classList.remove('open'); _mode = null; }

async function showDir(path) {
  try {
    const d = await api('GET', '/fs/list?path=' + encodeURIComponent(path || ''));
    _cur = d.path || '';
    $('pickcur').textContent = _cur || 'ドライブを選択';
    $('upbtn').disabled = !_cur;          // トップでは上へ不可
    $('pchoose').disabled = !_cur;        // ドライブ一覧そのものは選べない
    const items = d.entries || [];
    $('pbody').innerHTML = items.length
      ? items.map((e) => `<div class="fitem" data-cd="${esc(e.path)}">📁 ${esc(e.name)}</div>`).join('')
      : '<div class="muted" style="padding:10px">サブフォルダはありません。「このフォルダにする」で確定できます。</div>';
    $('pbody').dataset.parent = d.parent || '';
  } catch (e) {
    // 失敗時は選択状態をリセット（古いパスで誤登録させない・RV Med#5）。
    _cur = ''; $('pchoose').disabled = true; $('upbtn').disabled = true; $('pbody').dataset.parent = '';
    $('pickcur').textContent = '読み込めませんでした';
    $('pbody').innerHTML = `<div class="danger" style="padding:10px">${esc(e.message)}</div>`;
  }
}

function choose() {           // フォルダ選択の確定（追加フォーム用）
  if (!_cur) return;
  closePicker();
  setChosen(_cur);
}

// ---- 配線（委譲・インラインハンドラ無し）----
$('pickbtn').addEventListener('click', () => openPicker({ kind: 'register' }));
$('regbtn').addEventListener('click', register);
$('diffbtn').addEventListener('click', checkDiff);
$('list').addEventListener('click', (e) => {
  const rr = e.target.closest('[data-rag-rules]'); if (rr) return regenerateRagRules(rr.dataset.ragRules);
  const rf = e.target.closest('[data-refresh]'); if (rf) return refresh(rf.dataset.refresh);
  const dl = e.target.closest('[data-del]'); if (dl) return removeWorld(dl.dataset.del);
  const rc = e.target.closest('[data-recount]'); if (rc) return recount(rc.dataset.recount);
  const rv = e.target.closest('[data-reconvert-wid]'); if (rv) return reconvertFile(rv.dataset.reconvertWid, rv.dataset.rel);
});
$('pbody').addEventListener('click', (e) => {
  const cd = e.target.closest('[data-cd]'); if (cd) return showDir(cd.dataset.cd);
});
$('upbtn').addEventListener('click', () => showDir($('pbody').dataset.parent || ''));
$('pchoose').addEventListener('click', choose);
$('pcancel').addEventListener('click', closePicker);
$('ovl').addEventListener('click', (e) => { if (e.target === $('ovl')) closePicker(); });

// =====================================================================
// 下: 取り込み状況（文書一覧・全文検索・原本DL）＝旧 ingest.js。
// 「どの資料が使えるか確認・失敗をやり直す」。専門用語は出さず状態3つに集約。
// =====================================================================

const STATE = {  // state → 表示（記号・クラス）。enum のみ。
  ready: { mark: '✓ 使えます', cls: 'done' },
  processing: { mark: '⏳ 処理中…', cls: 'wait' },
  failed: { mark: '⚠ 失敗', cls: 'fail' },
  unreadable: { mark: '⚠ 読み取り不可', cls: 'fail' },   // 内容判定に必要なヘッダが読み取れない（`STATE.ready` へ倒さない）
  unknown: { mark: '❓ 状態を確認できませんでした', cls: 'fail' },  // 直近取り込みの状況が確認できない（`STATE.ready` へ倒さない）
};
const ABBR = { Module: 'Mod', Copybook: 'Cpy', DataItem: '項目', Document: '文書', Batch: 'ジョブ', Table: '表' };

let _pv = null, _q = '', _type = '', _state = 'all', _folder = '';

// unreadable/unknown は「⚠ 失敗」フィルタ・理由・やり直すの対象にも含める（バッジ表示は個別）
// ——不可読/確認不能の行が失敗フィルタで消えたり、理由/再実行の導線を失ったりしないようにする。
function isFailureState(state) {
  return state === 'failed' || state === 'unreadable' || state === 'unknown';
}

// 内部コード（`reason`）を平文に写像する（専門用語ゼロ・docs/04-画面の原則.md）。未知の値は
// raw のまま表示する（fail-open＝想定外の新規コードでも黙って消さない・原因追跡の手がかりを残す）。
const REASON_JA = {
  read_failed: 'ファイルを読み取れませんでした',
  unreadable_code_file: 'コードを読み取れなかったため取り込みを止めました',
};
// Office 変換失敗の内側の理由（`office_md_blocked:{doc}\t{innerReason}` の innerReason 部）→ 平文。
// 例外クラス名（`unhandled_exception:RuntimeError` 等の `:` 以降）はそのまま出さない
// （内部実装の詳細・専門用語ゼロの原則）。
const OFFICE_INNER_REASON_JA = { manifest_write_failed: '記録の書き込みに失敗しました' };
function officeBlockedReasonText(reason) {
  const rest = reason.slice('office_md_blocked:'.length);
  const tab = rest.indexOf('\t');
  const inner = tab >= 0 ? rest.slice(tab + 1) : '';
  if (inner.startsWith('unhandled_os_error:') || inner.startsWith('unhandled_exception:')) {
    return 'Office 文書を変換できませんでした（想定外のエラー）';
  }
  const innerJa = OFFICE_INNER_REASON_JA[inner];
  return innerJa ? `Office 文書を変換できませんでした（${innerJa}）` : 'Office 文書を変換できませんでした';
}
function reasonText(reason) {
  if (typeof reason === 'string' && reason.startsWith('office_md_blocked:')) return officeBlockedReasonText(reason);
  return REASON_JA[reason] || reason;
}

// 「どう読み取ったか」の平文バッジ（S2・専門用語ゼロ・04-画面の原則.md）。データ源は取り込み時の来歴（provenance）＝
// 追加の記録はしない・表示のみ。method 1個＋（該当時のみ）旧形式変換／照合差分の最大2〜3個に抑える。
const PROV_METHOD = {                       // 主たる読み取り方法 → 平文
  ooxml: 'Office から直接読み取り',
  pdf_text: 'PDF の文字を抽出',
  markitdown: '文書を広く読み取り（MarkitDown）',
  markitdown_ocr: 'AI が画像を見て読み取り（数値は要確認）',   // 視覚読み取り（VLM）・tesseract 撤去後の唯一の OCR
  // 後方互換のみ（RV Med 2026-07-08 R1）: tesseract 直の `ocr` アームは撤去済み。次回の派生 md 全再ビルドで
  // method="ocr" の来歴は出なくなるが、それまでは既存の派生 meta.json にこの値が残る＝表示だけ維持する。
  ocr: '画像から文字を読み取り（旧方式）',
};
const PROV_LEGACY = {                       // 旧形式（.doc/.xls/.ppt）の前段変換バックエンド → 平文
  libreoffice: '旧形式を変換してから読み取り（LibreOffice）',
  office_com: '旧形式を変換してから読み取り（Office 連携）',
};
const CONFLICT_TIP = '別の方法で読むと追加の内容が見つかりました。原本を確認してください';

function provBadges(p) {                     // 文書一覧の来歴バッジ（無ければ空文字＝後方互換）
  if (!p) return '';
  const out = [];
  const m = PROV_METHOD[p.method];
  if (m) out.push(`<span class="provbadge">${esc(m)}</span>`);
  const lb = PROV_LEGACY[p.legacy_backend];
  if (lb) out.push(`<span class="provbadge">${esc(lb)}</span>`);
  if (p.has_conflicts) out.push(`<span class="provbadge warn" title="${esc(CONFLICT_TIP)}">照合で差分あり</span>`);
  return out.length ? `<div class="provrow">${out.join('')}</div>` : '';
}

// 文書一覧: 担当アナライザの来歴（コード文書のみ・§7 裁定2の受入条件＝取り込み画面と影響分析の
// 根拠表示で参照できるようにする）。`d.analyzer`（`Analyzer.name`）を表示ラベルへ写像する
// （`d.doctype` は種別表示用の別項目——現行構成では同値だが独立した概念のため取り違えない）。
function analyzerBadgeRow(d) {
  if (d.branch !== 'source' || !d.analyzer) return '';
  return `<div class="provrow"><span class="provbadge">解析: ${esc(analyzerLabel(d.analyzer))}</span></div>`;
}

// 重要度バッジ（登録フォルダの `_重要度.txt` による登録者の注記・無ければ空文字＝後方互換）。
const IMP_CLASS = { 高: 'imp-high', 中: 'imp-mid', 低: 'imp-low' };
function impBadge(d) {
  const cls = IMP_CLASS[d.importance];
  if (!cls) return '';   // 高/中/低以外（未知値・欠落）は何も表示しない（「中」などへ推測して見せない）
  const tip = ['登録者による注記：重要度 ' + d.importance,
    d.importance_reason ? `理由: ${d.importance_reason}` : '',
    d.importance_source ? `由来: ${d.importance_source}` : ''].filter(Boolean).join(' ／ ');
  return `<span class="impbadge ${cls}" title="${esc(tip)}">${esc(d.importance)}</span>`;
}

function renderImportanceDiagnostics() {      // 重要度設定の構文エラーをツリー付近に小さく知らせる
  const diags = (_pv && _pv.importance_diagnostics) || [];
  const el = $('impdiag');
  if (!diags.length) { el.hidden = true; el.innerHTML = ''; return; }
  el.hidden = false;
  el.innerHTML = `⚠ 重要度の設定（_重要度.txt）に読み取れない行があります（${diags.length}件）`
    + `<ul style="margin:4px 0 0 18px;padding:0">`
    + diags.slice(0, 5).map((d) =>
        `<li>${esc(d.config_path)}${d.line != null ? `:${esc(d.line)}行目` : ''} — ${esc(d.message)}</li>`).join('')
    + `</ul>`;
}

// UI-ING是正1（利用者報告 2026-09-03）: `/ingest/preview` は world 未指定/空文字を 422 で拒否する
// （バックエンドの入口検証としては妥当・変更しない）ため、worlds 0件（未登録）のまま素通しで
// fetch すると `_pv.documents` を欠いた応答（`{"detail": [...]}`）が返り、後続の `.map` が
// 例外を投げてスピナー（`_LOADING_INLINE`）が永久に残っていた。worlds 0件は fetch 自体を
// 行わず平文の空状態へ倒し、それ以外の失敗（503・ネットワーク断等）も try/catch で拾って
// 平文のエラー表示に倒す（スピナー放置を構造的に無くす）。
const _NO_WORLD_MSG = 'まだ資料フォルダが登録されていません。上の「登録」から追加してください。';

async function load() {
  const world = $('version').value;
  if (!world) {
    _pv = null;
    $('type').innerHTML = '<option value="">すべて</option>';
    renderImportanceDiagnostics();
    $('tree').innerHTML = `<div class="empty">${esc(_NO_WORLD_MSG)}</div>`;
    $('rows').innerHTML = `<tr><td colspan="6"><div class="empty">${esc(_NO_WORLD_MSG)}</div></td></tr>`;
    $('count').textContent = '';
    return;
  }
  $('tree').innerHTML = _LOADING_INLINE;
  $('rows').innerHTML = `<tr><td colspan="6">${_LOADING_INLINE}</td></tr>`;
  try {
    _pv = await getJSON(`/ingest/preview?world=${encodeURIComponent(world)}`);
    const types = [...new Set(_pv.documents.map((d) => d.doctype))].sort();
    $('type').innerHTML = '<option value="">すべて</option>' + types.map((t) => `<option>${esc(t)}</option>`).join('');
    renderImportanceDiagnostics();
    renderTree();
    render();
  } catch (e) {
    _pv = null;
    $('tree').innerHTML = `<div class="danger">取り込み状況を取得できません: ${esc(e.message)}</div>`;
    $('rows').innerHTML = `<tr><td colspan="6"><div class="danger">取り込み状況を取得できません: ${esc(e.message)}</div></td></tr>`;
    $('count').textContent = '';
  }
}

function setState(btn) {
  btn.parentNode.querySelectorAll('.filterchip').forEach((c) => c.classList.remove('on'));
  btn.classList.add('on'); _state = btn.dataset.state; render();
}

function inFolder(d) {                       // 範囲（フォルダ）絞り込み: 選択フォルダ配下か
  return !_folder || d.folder === _folder || (d.folder || '').startsWith(_folder + '/');
}

function renderTree() {                       // 文書の folder からフォルダツリー（範囲）を作る
  const docs = _pv ? _pv.documents : [];
  const counts = {};
  for (const d of docs) {
    let path = '';
    for (const p of (d.folder || '').split('/').filter(Boolean)) { path = path ? path + '/' + p : p; counts[path] = (counts[path] || 0) + 1; }
  }
  let html = `<div class="tnode ${_folder === '' ? 'on' : ''}" data-folder="">📂 すべて<span class="tc">${docs.length}</span></div>`;
  for (const path of Object.keys(counts).sort()) {
    const depth = path.split('/').length - 1, name = path.split('/').pop();
    // 鏡モデルでは common layer 概念は撤去済（フォルダは全て同列）＝旧 layer-common 強調は廃止（rv-full2 C3）
    html += `<div class="tnode ${_folder === path ? 'on' : ''}" data-folder="${esc(path)}" style="padding-left:${8 + depth * 15}px">📁 ${esc(name)}<span class="tc">${counts[path]}</span></div>`;
  }
  $('tree').innerHTML = html;
}

function render() {
  const docs = (_pv ? _pv.documents : []).filter((d) =>
    inFolder(d)
    && (!_q || d.name.toLowerCase().includes(_q.toLowerCase()))
    && (!_type || d.doctype === _type)
    && (_state === 'all' || (_state === 'failed' ? isFailureState(d.state) : d.state === _state)));
  const scoped = (_pv ? _pv.documents : []).filter(inFolder);
  const n = { ready: 0, processing: 0, failed: 0 };
  // 集計は3状態（専門用語ゼロ・ファイル冒頭のコメント参照）に寄せる——unreadable/unknown は行の
  // 表示は個別（`STATE.unreadable`/`STATE.unknown`）だが集計上は failed の一種として数える
  // （黙って数から漏らさない）。
  scoped.forEach((d) => { const b = isFailureState(d.state) ? 'failed' : d.state; n[b] = (n[b] || 0) + 1; });
  $('count').textContent = (_folder ? `範囲: ${_folder} ・ ` : '') + `使えます ${n.ready | 0} ・ 処理中 ${n.processing | 0} ・ 失敗 ${n.failed | 0}`;
  $('rows').innerHTML = docs.map(row).join('')
    || `<tr><td colspan="6"><div class="empty">該当する資料がありません</div></td></tr>`;
  updateEsScope();
}

function row(d) {
  const st = STATE[d.state] || STATE.ready;
  const icon = d.branch === 'source' ? '📜' : '📄';
  const place = d.folder || (d.branch === 'source' ? 'プログラム' : '設計書・仕様書');
  const reason = isFailureState(d.state) && d.reason
    ? `<div class="muted" style="font-size:11.5px;margin-top:3px">理由: ${esc(reasonText(d.reason))}</div>` : '';
  const rerun = isFailureState(d.state) ? `<button class="mini" data-rerun="${esc(d.name)}">やり直す</button>` : '';
  return `<tr>
    <td><span class="fname">${icon} ${esc(d.name)}</span>${reason}${provBadges(d.provenance)}${analyzerBadgeRow(d)}</td>
    <td><span class="dtype">${esc(d.doctype)}</span></td>
    <td><span class="status ${st.cls}"><span class="d"></span>${esc(st.mark)}</span></td>
    <td class="muted">${esc(place)}</td>
    <td>${impBadge(d)}</td>
    <td><div class="rowact">${rerun}<button class="mini" data-dl="${esc(d.name)}">原本をひらく</button></div></td>
  </tr>`;
}

// ===== 全文検索（共有KB・read-only） =====
function updateEsScope() {
  const world = $('version').selectedOptions[0] ? $('version').selectedOptions[0].textContent : $('version').value;
  $('es-scope').textContent = (_folder ? `範囲: ${_folder}` : `${world || '資料フォルダ'} 全体`);
}

// ヒットの由来（小さく・専門用語ゼロ＝略語を出さない・RV Low 2026-07-08）。
// ヒットカードはスペースが狭いので一覧側（PROV_METHOD）より短い平文にする。
// ocr は後方互換のみ（tesseract 撤去済み・次回全再ビルドで消える・RV Med 2026-07-08 R1）。
const ES_METHOD = { ooxml: 'Office から抽出', pdf_text: 'PDF から抽出',
                    markitdown: '広く読み取り', markitdown_ocr: 'AI が画像から読み取り',
                    ocr: '画像から読み取り（旧）' };
function esProvBadges(h) {                   // ヒットカードの由来バッジ（無ければ空文字＝従来どおり）
  const out = [];
  const m = ES_METHOD[h.extraction_method];
  if (m) out.push(`<span class="provbadge">${esc(m)}</span>`);
  if (h.has_conflicts) out.push(`<span class="provbadge warn" title="${esc(CONFLICT_TIP)}">照合で差分あり</span>`);
  return out.join('');
}

function renderEsHits(hits) {
  if (!hits.length) {
    $('eshits').innerHTML = '<div class="empty">該当するヒットはありません</div>';
    return;
  }
  $('eshits').innerHTML = hits.map((h) => {
    const nscore = Number(h.score);
    const score = Number.isFinite(nscore) ? `score ${nscore.toFixed(2)}` : '';
    const line = h.line == null ? '' : `行 ${esc(h.line)}`;
    const ext = h.ext ? esc(h.ext) : '';
    return `<div class="eshit"><div class="meta"><span class="doc">${esc(h.doc_id)}</span>`
      + [line, score, ext].filter(Boolean).map((x) => `<span>${x}</span>`).join('')
      + esProvBadges(h)
      + `</div><pre>${esc(h.snippet || '')}</pre></div>`;
  }).join('');
}

async function searchEs() {
  const q = $('esq').value.trim();
  if (!q) { $('eshits').innerHTML = '<div class="muted">検索語を入力してください</div>'; return; }
  const ps = new URLSearchParams({ world: $('version').value, query: q });
  if (_folder) ps.append('scope_paths', _folder);
  $('eshits').setAttribute('aria-busy', 'true');
  $('eshits').innerHTML = '<div class="loading-inline" role="status"><span class="spinner spinner-sm"></span><span>社内資料を検索しています...</span></div>';
  try {
    const d = await api('GET', `/admin/es/search?${ps.toString()}`);
    renderEsHits(d.hits || []);
  } catch (e) {
    $('eshits').innerHTML = `<div class="danger">検索できません: ${esc(e.message)}。検索語、資料フォルダ、ログイン状態を確認してください。</div>`;
  } finally {
    $('eshits').setAttribute('aria-busy', 'false');
  }
}

// ===== admin ガード（W1・2026-09-03）: 資料フォルダ登録/更新/削除・取り込み状況
// （/ingest/preview）・全文検索（/admin/es/search）は全て admin 限定 API。この画面は丸ごと admin 専用
// ＝admin-settings.js / audit.js と同じ自前 checkAdmin() パターン（nav.js の _isAdminUser はカスタム
// エレメント内部の非公開状態のため外部から参照できない）。判定失敗時は fail-safe で非表示のまま。 =====
async function checkAdmin() {
  try {
    const u = await getJSON('/auth/me');
    if (u && u.role === 'admin') return true;
  } catch (_) { /* compat */ }
  return false;
}

const statusTag = (s) => (s && s !== 'active')
  ? `<span class="statustag ${s === 'deprecated' ? 'deprecated' : 'hidden_candidate'}">${s === 'deprecated' ? '廃止' : '未使用の疑い'}</span>` : '';

function openPrev() {
  if (!_pv) return;
  const c = _pv.counts;
  $('pv-bar').innerHTML = `抽出された要素 <b>${c.entities}</b>`
    + ` ・ 関係 <b>${c.relations}</b> ・ 廃止/隠し <b>${c.deprecated + c.hidden}</b>`;
  // 担当アナライザの来歴（コード以外は analyzer=null＝出さない・§7 裁定2）。
  $('pv-ents').innerHTML = _pv.entities.map((e) => `<div class="ent"><span class="ico">${esc(ABBR[e.label] || e.label)}</span>`
    + `<span class="nm">${esc(e.name)}${statusTag(e.status)}<small>${esc(e.label)}${e.parent ? ' ・ ' + esc(e.parent) : ''}${e.value != null ? ' ・ 値 ' + esc(e.value) : ''}${e.analyzer ? ' ・ 解析: ' + esc(analyzerLabel(e.analyzer)) : ''}</small></span></div>`).join('');
  $('pv-rels').innerHTML = _pv.relations.map((r) => `<div class="rel"><div class="chain">${esc(r.src)} ─${esc(r.type)}→ ${esc(r.dst)}</div>`
    + `<div class="meta">${statusTag(r.status)}${r.doc ? '<span>' + esc(r.doc) + '</span>' : ''}</div></div>`).join('');
  $('overlay').classList.add('open');
}
function closePrev() { $('overlay').classList.remove('open'); }

async function download(name) {
  // doc_id＝rel_path（slash を含む）＝パス基準DL（world＋rel を query で渡す・鏡モデル）
  const world = $('version').value;
  const r = await fetch(`/documents/download?world=${encodeURIComponent(world)}&rel=${encodeURIComponent(name)}`);
  if (!r.ok) { alert((await r.json().catch(() => ({}))).detail || '原本が見つかりません'); return; }
  const blob = await r.blob();
  Sherpa.downloadBlob(blob, name.split('/').pop());   // UI フィードバック3: revoke タイミング問題を共通ヘルパで回避
}

async function rerun() {
  // 鏡＝即反映ライブ鏡: world 全体のクリーン rebuild（doc 単位の差分やり直しは無い）。ING-3:
  // 即受付・背景実行のため、他のボタン（更新/削除）と同じ共通 api()＋エラー確認＋reloadAll()へ
  // 統一する（進捗・完了は上段の行ポーリング loadStat が示す）。
  try {
    const res = await api('POST', '/ingest/rerun', { world: $('version').value });
    alert(res.note || '再取り込みを受け付けました');
    reloadAll(res.world_id);
  } catch (e) {
    alert(`再取り込みできません: ${e.message}`);
  }
}

// 委譲（検索・フィルタ・行操作）
$('q').addEventListener('input', (e) => { _q = e.target.value; render(); });
$('type').addEventListener('change', (e) => { _type = e.target.value; render(); });
$('tree').addEventListener('click', (e) => {                 // 範囲で絞る
  const t = e.target.closest('[data-folder]'); if (!t) return;
  _folder = t.dataset.folder; renderTree(); render();
  // 全文検索結果は範囲依存＝範囲変更で古い結果を残さない（検索語があれば新範囲で再検索・RV Med）
  if ($('esq').value.trim()) searchEs();
  else $('eshits').innerHTML = '<div class="muted">検索語を入力してください</div>';
});
$('detailbtn').addEventListener('click', openPrev);
$('esbtn').addEventListener('click', searchEs);
$('esq').addEventListener('keydown', (e) => { if (e.key === 'Enter') searchEs(); });
$('rows').addEventListener('click', (e) => {
  const dl = e.target.closest('[data-dl]'); if (dl) return download(dl.dataset.dl);
  const rr = e.target.closest('[data-rerun]'); if (rr) return rerun(rr.dataset.rerun);
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closePrev(); });

// 選択中フォルダの参照先パスを表示（どのフォルダを見ているか明示）。
let _roots = {};   // world_id → root_path（登録済みのみ）
function showCurPath() {
  const p = _roots[$('version').value];
  $('curpath').textContent = p ? `参照中のフォルダ: ${p}` : '';
}
$('version').addEventListener('change', () => { showCurPath(); $('eshits').innerHTML = '<div class="muted">検索語を入力してください</div>'; load(); });

// ---- 下段（取り込み状況）の再同期（登録/削除/更新後に呼び直す） ----
// 資料フォルダは全体で1本のため選択の余地が無い。`#version` は下段の各処理が参照する値として
// 残し（load()/searchEs()/scope ツリーが読む）、UI としては常に隠す。
async function reloadStatusSection(_preferredWorldId) {
  let ws = [];
  try {
    ws = await fetchWorldsShared();
  } catch (e) {
    ws = [];
  }
  _roots = Object.fromEntries(ws.map((x) => [x.world_id, x.root_path]));
  $('version').innerHTML = ws.length
    ? ws.map((x) => `<option value="${esc(x.world_id)}">${esc(x.label || x.world_id)}</option>`).join('')
    : '<option value="">（資料フォルダ未登録）</option>';
  const f = $('version').closest('.field');
  if (f) f.style.display = 'none';
  $('version').value = ws[0] ? ws[0].world_id : '';
  showCurPath();
  await load();
}

// ---- 上段一覧＋下段セレクタの同時再同期（fetchWorldsShared() を共有＝/worlds 取得は1回・RV Med3） ----
function reloadAll(preferredWorldId) {
  loadList();
  reloadStatusSection(preferredWorldId);
}

// =====================================================================
// 共通: テーマ切替（単一ハンドラ・両セクション共通）
// =====================================================================
function applyThemeIcon() { const b = $('themebtn'); if (b) b.textContent = document.documentElement.dataset.theme === 'dark' ? '☀️' : '🌙'; }
$('themebtn').addEventListener('click', () => {
  const d = document.documentElement, next = d.dataset.theme === 'dark' ? 'light' : 'dark';
  d.dataset.theme = next; localStorage.setItem('sherpa-theme', next); applyThemeIcon();
});
applyThemeIcon();

// =====================================================================
// 初期化（admin ガード・W1）: この画面のデータは全て admin 限定 API のため、非 admin には
// access-denied だけを見せ、資料フォルダ一覧・取り込み状況・全文検索のいずれも取得しない
// （admin-settings.js と同じ「本体を読まずに弾く」パターン）。
// admin 判明後に上＝一覧／下＝セレクタを読む（取込ディレクトリ確定後に状況を読む。
// /worlds 取得は1回に共有・RV Med3）。
(async () => {
  const isAdmin = await checkAdmin();
  if (!isAdmin) {
    const main = $('main-content'), denied = $('access-denied');
    if (main) main.style.display = 'none';
    if (denied) denied.style.display = 'block';
    return;
  }
  $('detailbtn').hidden = false;
  reloadAll();
})();
