
// 全ページ共通の favicon（各 HTML に <link> を書かず、読み込まれた時点で1回だけ挿入する）
(function(){
  if (!document.querySelector('link[rel="icon"]')) {
    var l = document.createElement('link');
    l.rel = 'icon'; l.type = 'image/x-icon'; l.href = 'favicon.ico';
    document.head.appendChild(l);
  }
})();
// 全ページ共通ユーティリティ（window.Sherpa）。フェーズ6 S2（リファクタリング計画）で nav.js から
// 分離した。全 HTML で nav.js の直前に読み込む classic script（module 化しない — nav.js と同じ
// 理由＝読み込み順が全ページに波及するため単純さを優先）。
'use strict';

// 全ページ共通ユーティリティ（各 *.js で重複していた esc/$/api/getJSON の単一の真実源・RV DRY）。
// nav.js は全 HTML で page script より前に読まれるので、ここに置けば HTML 追加なしで共有できる。
// `opts.timeoutMs`（省略可）: 指定すると応答本文の読了までを含めた締切を設ける（`fetch()` が
// すぐ解決しても `r.json()` の本文読み取りが詰まるケースも締切の対象にするため、`fetch()`＋
// `json()` 全体を `Promise.race` で締切と競わせる——`fetch()` 単体に `AbortController` を
// 付けるだけだと、ヘッダ受信後に本文ストリームが詰まる場合を締切から取りこぼす）。締切超過・
// 通信断（ネットワーク断・DNS失敗等）・本文が不正な JSON（ステータスに関わらず）のいずれも
// 「サーバーに書込みが実際に届いたかどうか分からない曖昧な失敗」として `err.ambiguous = true`
// を立てて投げる（呼び出し側は発行系 API のように「送信済みかもしれないが結果が確認できない」
// 失敗を回復する用途に使う）。締切超過は追加で `err.timeout = true` も立てる（従来からの区別・
// 呼び出し側で締切固有の文言を出したい場合用）。**本文の JSON 解析可否を先に判定する**——
// 妥当な JSON を持つ非2xx応答（自分のアプリが明示的に拒否を返した）だけが曖昧ではない確定的な
// 失敗になる。本文が JSON として解析できない非2xx（例: リバースプロキシ/ゲートウェイが返す
// HTML の 502/504）は、自分のアプリの整形されたエラー応答ではない＝アプリ側の処理が実際には
// 完了していた可能性を排除できないため、ambiguous 扱いにする（ステータス判定より先に行う）。
const _sherpaApi = async (method, url, body, opts) => {
  const o = opts || {};
  const opt = { method, headers: { 'Content-Type': 'application/json' } };
  if (body !== undefined) opt.body = JSON.stringify(body);
  const controller = o.timeoutMs ? new AbortController() : null;
  if (controller) opt.signal = controller.signal;

  const run = async () => {
    let r;
    try {
      r = await fetch(url, opt);
    } catch (e) {
      if (e && e.name === 'AbortError') {
        const err = new Error('応答がありません（タイムアウト）');
        err.timeout = true; err.ambiguous = true;
        throw err;
      }
      // fetch 自体の失敗（ネットワーク断・DNS失敗等）＝サーバーに届いたかどうか分からない。
      const err = new Error('通信に失敗しました（サーバーに届いたか確認できません）');
      err.ambiguous = true;
      throw err;
    }
    let data = null;
    let parseFailed = false;
    try { data = await r.json(); } catch (_) { parseFailed = true; }
    if (parseFailed) {
      // ステータス判定より先に評価する: 妥当な JSON を返せていない応答は、ステータスが非2xx
      // でも自分のアプリからの整形されたエラーとは限らない（中間のプロキシ障害等）ため、
      // 常に曖昧（結果不明）として扱う。
      const err = new Error(`応答の形式が不正です（HTTP ${r.status}・サーバーの処理結果が確認できません）`);
      err.ambiguous = true;
      throw err;
    }
    if (!r.ok) {
      // ここに来るのは妥当な JSON を持つ非2xx＝自分のアプリが明示的に拒否した確定的な失敗。
      // `status`/`body`（応答 JSON 全体）を Error へ載せる——`message` だけでは、応答本文に
      // 追加フィールド（例: usage_chat の 502/503 応答の `provider_used`/`endpoint_kind`）を
      // 持つ場合に呼び出し側がそれを読めない（既存の呼び出し元は `message` のみ参照するため
      // 影響なし＝追加のみ）。
      const err = new Error((data && (data.detail || data.message)) || `エラー (${r.status})`);
      err.status = r.status;
      err.body = data;
      throw err;
    }
    return data;
  };

  if (!o.timeoutMs) return run();
  let timer = null;
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      const err = new Error('応答がありません（タイムアウト）');
      err.timeout = true; err.ambiguous = true;
      reject(err);
    }, o.timeoutMs);
  });
  try {
    return await Promise.race([run(), deadline]);
  } finally {
    clearTimeout(timer);
  }
};
// 日時表示の共通ヘルパー（RV DRY・S3 2026-07 修正）。サーバは常に timezone 付き ISO 8601（`+00:00` 等）
// を返す（psycopg の timestamptz はタイムゾーン付き datetime→FastAPI がオフセット込みで直列化）。
// `String(iso).slice(0,16).replace('T',' ')` のような素朴な文字列切り出しは UTC の時刻をそのまま
// ローカル表示してしまう不具合の元（例: 16:12 表示なのに実際は 9 時間ズレた JST 1:13）。
// 必ず `new Date(iso)` を経由し、**端末ロケール（実質 JST）**へ変換してから表示する。
const _fmtDateTime = (iso, opts) => {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const p = (n) => String(n).padStart(2, '0');
  const date = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  if (opts && opts.dateOnly) return date;
  const time = `${p(d.getHours())}:${p(d.getMinutes())}` + (opts && opts.seconds ? `:${p(d.getSeconds())}` : '');
  return `${date} ${time}`;
};
// UI フィードバック3（2026-07-03・原本DLの「フリーズ」修正）: blob ダウンロードは `a.click()` の
// 直後に `URL.revokeObjectURL()` すると、ブラウザの保存ダイアログ（「名前を付けて保存」設定時）が
// まだ blob を読み切る前に無効化してしまい、保存・キャンセルいずれの結果でもダウンロードが完了せず
// 固まって見える不具合の原因になる（chat.js/ingest.js の原本DLハンドラで実際に踏んでいたパターン）。
// 保存ダイアログが閉じたことを検知できる汎用イベントは無い（特にキャンセル時）ため、そのイベント待ち
// に依存せず、十分な猶予（数秒）を置いてから revoke するタイムアウト方式にする。
const _sherpaDownloadBlob = (blob, filename) => {
  const a = document.createElement('a');
  const url = URL.createObjectURL(blob);
  a.href = url; a.download = filename || 'download';
  document.body.appendChild(a);   // Safari 等での click() 信頼性のため一時的に DOM へ
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
};
const _sherpaEsc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// 非表示タブでの定期ポーリング停止（Page Visibility API・RV DRY＝各所の setInterval が個別に
// 同じ判定を書かない単一の真実源）。document.hidden の間はタイマーを止め、可視化に戻った瞬間に
// fn を1回即時実行してから再開する（間隔いっぱい待たせない）。ストリーミング中の SSE ティックや
// 取り込み run 進捗の自己ポーリング（run_id 追跡中）はこの対象外——それぞれ独自の停止条件を
// 既に持つため個別のまま（既存の即時初回呼び出しは呼び出し側の責務のまま・ここでは繰り返しのみ扱う）。
// 戻り値の stop() は setInterval のクリアと visibilitychange リスナーの解除を両方行う
// （connectedCallback の再実行等で作り直す前に必ず呼ぶこと＝多重登録防止）。
const _sherpaVisibilityInterval = (fn, ms) => {
  let timer = null;
  const start = () => { if (timer === null) timer = setInterval(fn, ms); };
  const stop = () => { if (timer !== null) { clearInterval(timer); timer = null; } };
  const onVisibility = () => {
    if (document.hidden) { stop(); return; }
    // fn() が同期例外を投げても再開（start）は必ず行う——再開が死んで以後ずっと
    // ポーリングされなくなる事故を避ける。
    try { fn(); } finally { start(); }
  };
  document.addEventListener('visibilitychange', onVisibility);
  if (!document.hidden) start();
  return { stop: () => { stop(); document.removeEventListener('visibilitychange', onVisibility); } };
};

// UIフィードバック（2026-07-03・AI回答のMarkdown表示）: 外部ライブラリ非依存の安全サブセット・
// レンダラ。**必ず esc() で全文エスケープしてからパターン変換する**＝変換元に <script> 等の実タグは
// 一切存在しない状態でのみ正規表現を当てるため、構造的に XSS が起こり得ない（`<img onerror=...>` も
// `[text](javascript:...)` もエスケープ済みの見えるだけの文字列にしかならない＝リンク構文は
// 意図的に未対応＝自動リンク化しない）。
// 対応: **太字**・*斜体*・`インラインコード`・```コードブロック```・箇条書き（- のみ／1. 等の番号付き）・
// 見出し（# 〜 ### は太字段落程度の控えめな表現）・改行。それ以外は素の段落として通す。
function _mdInlineSafe(escaped) {
  // escaped は esc() 済み文字列。バッククォート区間で split すると奇数インデックスに中身が入る
  // （String#split の capture-group 仕様）ので、コード区間だけ bold/italic の再処理から外せる
  // （`**not bold**` のようにコード内の記号をそのまま見せる。プレースホルダ文字列は使わない＝
  // 元テキストにたまたま似た文字列があっても衝突しない）。
  const parts = escaped.split(/`([^`]+?)`/);
  return parts.map((part, idx) => (idx % 2 === 1 ? `<code>${part}</code>` : part
    .replace(/\*\*([^*]+?)\*\*/g, '<strong>$1</strong>')        // 太字（*斜体*より先に処理）
    .replace(/(^|[^*])\*([^*]+?)\*(?!\*)/g, '$1<em>$2</em>')    // 斜体
  )).join('');
}
function _mdLite(raw) {
  const escaped = _sherpaEsc(String(raw ?? '').replace(/\r\n/g, '\n'));
  const lines = escaped.split('\n');
  const out = [];
  let listBuf = null;   // {type:'ul'|'ol', items:[]}
  const flushList = () => {
    if (!listBuf) return;
    out.push(`<${listBuf.type}>${listBuf.items.map((it) => `<li>${_mdInlineSafe(it)}</li>`).join('')}</${listBuf.type}>`);
    listBuf = null;
  };
  let paraBuf = [];
  const flushPara = () => {
    if (!paraBuf.length) return;
    out.push(`<p>${_mdInlineSafe(paraBuf.join('<br>'))}</p>`);
    paraBuf = [];
  };
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const fence = /^```\S*\s*$/.test(line);          // esc() はバッククォートを変換しないのでそのまま判定可
    if (fence) {
      flushPara(); flushList();
      const codeLines = [];
      i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) { codeLines.push(lines[i]); i++; }
      i++;   // 閉じフェンスをスキップ（無ければ末尾まで＝寛容に扱う）
      out.push(`<pre class="md-code"><code>${codeLines.join('\n')}</code></pre>`);
      continue;
    }
    const heading = line.match(/^#{1,3}\s+(.*)$/);
    if (heading) {
      flushPara(); flushList();
      out.push(`<p><strong>${_mdInlineSafe(heading[1])}</strong></p>`);
      i++; continue;
    }
    const ulItem = line.match(/^-\s+(.*)$/);
    if (ulItem) {
      flushPara();
      if (!listBuf || listBuf.type !== 'ul') { flushList(); listBuf = { type: 'ul', items: [] }; }
      listBuf.items.push(ulItem[1]);
      i++; continue;
    }
    const olItem = line.match(/^\d+\.\s+(.*)$/);
    if (olItem) {
      flushPara();
      if (!listBuf || listBuf.type !== 'ol') { flushList(); listBuf = { type: 'ol', items: [] }; }
      listBuf.items.push(olItem[1]);
      i++; continue;
    }
    flushList();
    if (line.trim() === '') { flushPara(); i++; continue; }
    paraBuf.push(line);
    i++;
  }
  flushPara(); flushList();
  return out.join('');
}

// 担当アナライザの来歴表示（§7 裁定2の受入条件＝取り込み画面と影響分析の根拠表示で参照できる
// ようにする）。内部名（`Analyzer.name`・現行は cobol/copybook/jcl）を平文の表示ラベルへ写像する
// ——ingest.js（文書一覧・プレビュー）と chat/render.js（影響結果）が共有する単一の真実源（DRY）。
// own-property のみを見る（`hasOwnProperty` 経由・`constructor`/`__proto__`/`toString` 等の
// プロトタイプ継承プロパティを誤って返さない）。未知の名前（新規言語追加時）は加工せず
// `String(name)` をそのまま返す（大文字化しない・黙って空にもしない）。
const _ANALYZER_LABEL = { cobol: 'COBOL', copybook: 'コピーブック', jcl: 'JCL' };
const _analyzerLabel = (name) => {
  if (!name) return null;
  const key = String(name);
  return Object.prototype.hasOwnProperty.call(_ANALYZER_LABEL, key) ? _ANALYZER_LABEL[key] : key;
};

window.Sherpa = window.Sherpa || {
  $: (id) => document.getElementById(id),
  esc: _sherpaEsc,
  api: _sherpaApi,
  getJSON: (url) => _sherpaApi('GET', url),
  fmtDateTime: _fmtDateTime,
  downloadBlob: _sherpaDownloadBlob,
  analyzerLabel: _analyzerLabel,
  mdLite: _mdLite,
  visibilityInterval: _sherpaVisibilityInterval,
};
