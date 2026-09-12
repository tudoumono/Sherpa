
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

// AI回答の Markdown 表示: 外部ライブラリ非依存の安全サブセット・レンダラ。
// **必ず esc() で全文エスケープしてからパターン変換する**＝変換元に <script> 等の実タグは
// 一切存在しない状態でのみ正規表現を当てるため、構造的に XSS が起こり得ない（`<img onerror=...>` は
// エスケープ済みの見えるだけの文字列にしかならない）。リンクは `[text](http(s)://…)` だけを
// `<a>` にする（`javascript:`・`data:` 等の他スキームと裸の URL は文字のまま＝自動リンク化しない）。
// 対応: **太字**・*斜体*・`インラインコード`・```コードブロック```・[text](http(s)://…)・
// 箇条書き（-／*／+）・番号付き（1.）・入れ子リスト（行頭の字下げ 2 桁以上／タブ）・
// 見出し（# 〜 ###### は太字段落程度の控えめな表現）・表（| a | b | ＋ 区切り行）・引用（>）・
// 水平線（---／***／___）・改行。それ以外は素の段落として通す。CommonMark/GFM 全体の再実装では
// なく実害の出やすい形だけを狙う安全サブセットのため、意図的に非対応のままの箇所がある
// （バックスラッシュエスケープ全般・パイプで囲んだ1行の直後に来る水平線との衝突）。
function _mdInlineSafe(escaped) {
  // escaped は esc() 済み文字列。コードスパンをまたぐ強調やコードスパンを含むリンクにも対応する
  // ため、バッククォート区間で断片に分割してから個別に処理するのではなく、コードスパンを
  // 制御文字のプレースホルダへ退避 → 結合済み文字列に太字/斜体/リンクを適用 → 最後にコードスパンを
  // 復元する（プレースホルダは esc() 済みテキストに現れない制御文字を使うため元テキストと衝突しない）。
  const codeSpans = [];
  const withPlaceholders = escaped.replace(/`([^`]+?)`/g, (_, code) => {
    codeSpans.push(code);
    return `\x00${codeSpans.length - 1}\x00`;
  });
  const inline = withPlaceholders
    // 太字（*斜体*より先に処理）。中身は「連続する **」を含まないことだけを要求し、単独の `*`
    // （`**COUNT(*)**` 等）は許す。
    .replace(/\*\*((?:(?!\*\*)[\s\S])+?)\*\*/g, '<strong>$1</strong>')
    // 斜体: 開き `*` の直後・閉じ `*` の直前の空白を禁止する（CommonMark のフランキング規則の実用形）。
    // SQL のワイルドカード・COBOL の乗算演算子・glob の `*` のような単発の記号を誤って強調にしない。
    .replace(/(^|[^*])\*(?!\s)([^*]+?)(?<!\s)\*(?!\*)/g, '$1<em>$2</em>')
    // リンク: URL は http(s) のみ・空白を含まない範囲＋1段の対応括弧を許す（esc 済みなので
    // " ' < > は入り得ない）。太字/斜体の後に処理するため、リンク文字列側に <strong> 等が
    // 入っていてもそのまま包める。
    .replace(/\[([^\]]+?)\]\((https?:\/\/(?:[^\s()]|\([^\s()]*\))+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  return inline.replace(/\x00(\d+)\x00/g, (_, idx) => `<code>${codeSpans[Number(idx)]}</code>`);
}
// 表・リスト項目・コードフェンスで共通に使う行パターン。
const _MD_ITEM_RE = /^(\s*)([-*+]|\d+\.)\s+(.*)$/;
// 開き fence: 3連バッククォートの前後（インデント最大4桁＝リスト内の字下げも兼ねる・info string
// との間・info string の後）に空白を許す。info string 自体の内容は使わない（言語名クラスを
// 付けない）ため中身は問わない。
const _MD_FENCE_OPEN = /^(\s{0,4})(`{3,})[ \t]*[^`\s]*[ \t]*$/;
// 閉じ fence: 開きと同数以上のバッククォートのみの行（info string 不可）。開きの本数を捕捉して
// 判定する＝4 連で開いたフェンスは内側の 3 連では閉じない（フェンス自体を見せる書き方）。
function _mdFenceClose(n) { return new RegExp('^\\s{0,4}`{' + n + ',}\\s*$'); }
// 引用: `>` の直後が空白または行末のときだけ（`>=` のような比較演算子を引用と誤認識しない）。
const _MD_QUOTE_RE = /^\s*&gt;(?:\s|$)/;
function _mdStripIndent(line, n) {
  if (n <= 0) return line;
  const lead = line.match(/^\s*/)[0];
  return line.slice(Math.min(lead.length, n));
}
function _mdTableCells(line) {
  // バッククォート区間の外・かつ直前が `\` でない `|` だけで分割する（コードスパン内の `|` や
  // GFM エスケープ `\|` をセルの継ぎ目にしない）。外側の `|` は任意＝境界の空セルは捨てる。
  // 残った `\|` は表示直前に `|` へ戻す。
  const src = line.trim();
  const cells = [];
  let buf = '';
  // 行内で閉じないバッククォート（奇数個）はコードスパンではなく通常文字として扱う＝
  // 区切りの `|` を取りこぼして本体行の取り込みが途切れないようにする。
  const codeAware = (src.match(/`/g) || []).length % 2 === 0;
  let inCode = false;
  for (let idx = 0; idx < src.length; idx++) {
    const ch = src[idx];
    if (ch === '`' && codeAware) { inCode = !inCode; buf += ch; continue; }
    if (ch === '|' && !inCode && src[idx - 1] !== '\\') { cells.push(buf); buf = ''; continue; }
    buf += ch;
  }
  cells.push(buf);
  if (cells.length > 1 && cells[0].trim() === '') cells.shift();
  if (cells.length > 1 && cells[cells.length - 1].trim() === '') cells.pop();
  return cells.map((c) => c.trim().replace(/\\\|/g, '|'));
}
// 表行の構造判定: 外側の `|` は任意・2セル以上（ヘッダ・区切り・本体で共通の1つの判定）。
// 1セルしか無い行（パイプで囲んだだけの1行文など）は表として扱わない＝偽の1列表も防ぐ。
function _mdIsTableRow(line) {
  return _mdTableCells(line).length >= 2;
}
function _mdIsTableSep(line) {
  return _mdIsTableRow(line) && _mdTableCells(line).every((c) => /^:?-+:?$/.test(c));
}
function _mdTableAlign(sepLine) {
  return _mdTableCells(sepLine).map((c) => {
    const l = c.startsWith(':'), r = c.endsWith(':');
    return l && r ? 'center' : r ? 'right' : l ? 'left' : '';
  });
}
function _mdRenderList(list) {
  // list = {type, items:[{text, children:[list…], extraHtml:[...]}], start}。
  // extraHtml はリスト項目内の字下げコードフェンスなど、テキストと同列に描画するがインライン
  // 処理には通さない（既に組み立て済みの）ブロック HTML。start は先頭項目の番号（<ol> のみ・
  // 1 のときは省略）。
  const startAttr = list.type === 'ol' && list.start && list.start !== 1 ? ` start="${list.start}"` : '';
  const items = list.items.map((it) => (
    `<li>${_mdInlineSafe(it.text)}${(it.extraHtml || []).join('')}${it.children.map(_mdRenderList).join('')}</li>`
  )).join('');
  return `<${list.type}${startAttr}>${items}</${list.type}>`;
}
function _mdBlocks(lines) {
  // lines は esc() 済み行の配列（引用の中身を再帰的に処理するため、生文字列ではなく行列を受ける）。
  const out = [];
  let paraBuf = [];
  const flushPara = () => {
    if (!paraBuf.length) return;
    out.push(`<p>${_mdInlineSafe(paraBuf.join('<br>'))}</p>`);
    paraBuf = [];
  };
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const fenceOpen = line.match(_MD_FENCE_OPEN);   // esc() はバッククォートを変換しないのでそのまま判定可
    if (fenceOpen) {
      flushPara();
      const stripN = fenceOpen[1].replace(/\t/g, '  ').length;
      const closeRe = _mdFenceClose(fenceOpen[2].length);
      const codeLines = [];
      i++;
      while (i < lines.length && !closeRe.test(lines[i])) { codeLines.push(_mdStripIndent(lines[i], stripN)); i++; }
      i++;   // 閉じフェンスをスキップ（無ければ末尾まで＝寛容に扱う）
      out.push(`<pre class="md-code"><code>${codeLines.join('\n')}</code></pre>`);
      continue;
    }
    // 水平線（箇条書き `- ` や表の区切り行より先に判定。`- - -` の形は水平線として扱う）
    if (/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(line)) {
      flushPara(); out.push('<hr>'); i++; continue;
    }
    const heading = line.match(/^#{1,6}\s+(.*)$/);
    if (heading) {
      flushPara();
      out.push(`<p><strong>${_mdInlineSafe(heading[1])}</strong></p>`);
      i++; continue;
    }
    // 引用: `>` は esc() で &gt; になっている。連続する引用行をまとめ、中身を再帰的に処理する。
    if (_MD_QUOTE_RE.test(line)) {
      flushPara();
      const inner = [];
      while (i < lines.length && _MD_QUOTE_RE.test(lines[i])) {
        inner.push(lines[i].replace(/^\s*&gt;\s?/, ''));
        i++;
      }
      out.push(`<blockquote>${_mdBlocks(inner)}</blockquote>`);
      continue;
    }
    // 表: ヘッダ行＋区切り行が揃い、かつセル数が一致したときだけ。以降の行を本体行として取り込む。
    if (_mdIsTableRow(line) && i + 1 < lines.length && _mdIsTableSep(lines[i + 1]) &&
        _mdTableCells(lines[i + 1]).length === _mdTableCells(line).length) {
      flushPara();
      const header = _mdTableCells(line);
      const align = _mdTableAlign(lines[i + 1]);
      const td = (cell, k, tag) => {
        const a = align[k] ? ` class="md-al-${align[k]}"` : '';
        return `<${tag}${a}>${_mdInlineSafe(cell)}</${tag}>`;
      };
      const thead = `<thead><tr>${header.map((c, k) => td(c, k, 'th')).join('')}</tr></thead>`;
      const rows = [];
      i += 2;
      // ヘッダの列数を超えるセルは最後のセルへ `|` で連結して 1 行として描画する（余剰セルを
      // 消さず、後続の正常な本体行も表に残す）。
      while (i < lines.length && _mdIsTableRow(lines[i])) {
        let cells = _mdTableCells(lines[i]);
        if (cells.length > header.length) {
          cells = cells.slice(0, header.length - 1).concat([cells.slice(header.length - 1).join(' | ')]);
        }
        rows.push(`<tr>${header.map((_, k) => td(cells[k] ?? '', k, 'td')).join('')}</tr>`);
        i++;
      }
      out.push(`<table class="md-table">${thead}<tbody>${rows.join('')}</tbody></table>`);
      continue;
    }
    // リスト（- * + ／ 1.）: 連続する項目行を集め、字下げ幅で入れ子にする。項目間の空行1行・
    // 項目の内容列以上の字下げが付いた継続行（コードフェンスならブロックとして、それ以外は
    // 現在の項目のテキストへ連結）はリストを打ち切らない。
    const item = line.match(_MD_ITEM_RE);
    if (item) {
      flushPara();
      // root は「項目」と同じ形（children にトップレベルのリストが並ぶ）にして扱いを揃える。
      const root = { children: [] };
      const stack = [{ indent: -1, items: [root] }];   // 番兵: root を唯一の項目に持つ疑似リスト
      const openList = (type, indent, start) => {
        const parentList = stack[stack.length - 1];
        const parentItem = parentList.items[parentList.items.length - 1];
        const list = { type, items: [], indent, start };
        parentItem.children.push(list);
        stack.push(list);
        return list;
      };
      let lastItem = null;
      let lastItemCol = 0;
      while (i < lines.length) {
        const raw = lines[i];
        if (raw.trim() === '') {
          // 空行1行はリスト継続。次行が項目、または現在の項目への継続行なら打ち切らない
          // （2連続の空行・無関係な内容が続く場合はここで終了し、外側ループに処理を戻す）。
          const next = lines[i + 1] ?? '';   // 末尾の空行（次行なし）は空行扱い＝ここでリスト終了
          const nextBlank = next.trim() === '';
          const nextIsItem = _MD_ITEM_RE.test(next);
          const nextIndent = next.match(/^(\s*)/)[1].replace(/\t/g, '  ').length;
          const nextIsContinuation = !nextBlank && !nextIsItem && lastItem !== null &&
            next.trim() !== '' && nextIndent >= lastItemCol;
          if (!nextBlank && (nextIsItem || nextIsContinuation)) { i++; continue; }
          break;
        }
        const m = raw.match(_MD_ITEM_RE);
        if (!m) {
          const indent = raw.match(/^(\s*)/)[1].replace(/\t/g, '  ').length;
          if (lastItem !== null && indent >= lastItemCol) {
            // 字下げされた表の開始行・引用行は項目テキストへ連結せず、リストを閉じて外側の
            // ブロック処理に返す（表・引用として描画される＝行はそのまま残す）。
            if (_MD_QUOTE_RE.test(raw) ||
                (_mdIsTableRow(raw) && i + 1 < lines.length && _mdIsTableSep(lines[i + 1]))) break;
            // fence は項目の内容列を基準に判定する（2 段目以降の入れ子でも字下げ 4 桁超で認識する）。
            const fm = _mdStripIndent(raw, lastItemCol).match(_MD_FENCE_OPEN);
            if (fm) {
              const stripN = lastItemCol + fm[1].replace(/\t/g, '  ').length;
              const closeRe = _mdFenceClose(fm[2].length);
              const codeLines = [];
              i++;
              // 閉じ fence も項目の内容列を剥がしてから判定する（開きと同じ字下げ規則）
              while (i < lines.length && !closeRe.test(_mdStripIndent(lines[i], lastItemCol))) { codeLines.push(_mdStripIndent(lines[i], stripN)); i++; }
              i++;   // 閉じフェンスをスキップ（無ければ末尾まで＝寛容に扱う）
              (lastItem.extraHtml = lastItem.extraHtml || []).push(
                `<pre class="md-code"><code>${codeLines.join('\n')}</code></pre>`);
              continue;
            }
            if (lastItem.extraHtml && lastItem.extraHtml.length) {
              // フェンスより後ろの継続行は出現順を保つため text ではなくブロック列の末尾へ足す
              lastItem.extraHtml.push(`<div>${_mdInlineSafe(raw.trim())}</div>`);
              i++;
              continue;
            }
            lastItem.text += `<br>${raw.trim()}`;
            i++;
            continue;
          }
          break;
        }
        const indent = m[1].replace(/\t/g, '  ').length;
        const type = /^\d+\.$/.test(m[2]) ? 'ol' : 'ul';
        const start = type === 'ol' ? Number.parseInt(m[2], 10) : undefined;
        while (stack.length > 1 && indent < stack[stack.length - 1].indent) stack.pop();
        let cur = stack[stack.length - 1];
        if (stack.length === 1 || indent > cur.indent) {
          cur = openList(type, indent, start);      // 新しい階層（トップ、またはひとつ前の項目の子）
        } else if (cur.type !== type) {
          stack.pop();                              // 同じ階層で種類が変わった＝別リストを兄弟として続ける
          cur = openList(type, indent, start);
        }
        const newItem = { text: m[3], children: [] };
        cur.items.push(newItem);
        lastItem = newItem;
        lastItemCol = m[0].length - m[3].length;
        i++;
      }
      out.push(root.children.map(_mdRenderList).join(''));
      continue;
    }
    if (line.trim() === '') { flushPara(); i++; continue; }
    paraBuf.push(line);
    i++;
  }
  flushPara();
  return out.join('');
}
function _mdLite(raw) {
  const escaped = _sherpaEsc(String(raw ?? '').replace(/\r\n/g, '\n'));
  return _mdBlocks(escaped.split('\n'));
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
