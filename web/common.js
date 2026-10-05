
// 全ページ共通の favicon（各 HTML に <link> を書かず、読み込まれた時点で1回だけ挿入する）
(function(){
  if (!document.querySelector('link[rel="icon"]')) {
    var l = document.createElement('link');
    l.rel = 'icon'; l.type = 'image/x-icon'; l.href = 'favicon.ico';
    document.head.appendChild(l);
  }
})();
// 全ページ共通ユーティリティ（window.Sherpa）。全 HTML で nav.js の直前に読み込む classic script（module 化しない）。
'use strict';

// 全ページ共通ユーティリティ（esc/$/api/getJSON）。nav.js は全 HTML で page script より前に読まれるため、ここに置けば HTML 追加なしで共有できる。
// opts.timeoutMs（省略可）: 指定すると本文の読了までを含めた締切を設け、fetch()＋json() 全体を Promise.race で締切と競わせる。
// 締切超過・通信断・本文が不正な JSON のいずれも「書込みが届いたか分からない曖昧な失敗」として err.ambiguous = true を立てて投げる（締切超過は追加で err.timeout = true）。
// 本文の JSON 解析可否を先に判定する: 妥当な JSON を持つ非2xx応答だけが確定的な失敗で、解析できない非2xx（プロキシの HTML 502/504 等）は ambiguous 扱い。
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
      // ステータス判定より先に評価する: 妥当な JSON を返せていない応答は、非2xx でも自アプリの整形エラーとは限らないため常に曖昧（結果不明）として扱う。
      const err = new Error(`応答の形式が不正です（HTTP ${r.status}・サーバーの処理結果が確認できません）`);
      err.ambiguous = true;
      throw err;
    }
    if (!r.ok) {
      // ここに来るのは妥当な JSON を持つ非2xx＝自アプリが明示的に拒否した確定的な失敗。status/body（応答 JSON 全体）を Error へ載せる（既存の呼び出し元は message のみ参照するため追加のみ）。
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
// 日時表示の共通ヘルパー。サーバは常に timezone 付き ISO 8601 を返すため、文字列切り出しでは UTC の時刻がそのまま表示される。
// 必ず new Date(iso) を経由し、端末ロケール（実質 JST）へ変換してから表示する。
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
// blob ダウンロードは a.click() の直後に URL.revokeObjectURL() すると、保存ダイアログが blob を読み切る前に無効化されダウンロードが固まる。
// 保存ダイアログの終了は検知できないため、数秒の猶予を置いてから revoke する。
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

// 非表示タブでの定期ポーリング停止（Page Visibility API）。document.hidden の間はタイマーを止め、可視化に戻った瞬間に fn を1回即時実行してから再開する。
// SSE ティックや取り込み run 進捗の自己ポーリングは独自の停止条件を持つため対象外。
// 戻り値の stop() は setInterval のクリアと visibilitychange リスナーの解除を両方行う（作り直す前に必ず呼ぶ＝多重登録防止）。
const _sherpaVisibilityInterval = (fn, ms) => {
  let timer = null;
  const start = () => { if (timer === null) timer = setInterval(fn, ms); };
  const stop = () => { if (timer !== null) { clearInterval(timer); timer = null; } };
  const onVisibility = () => {
    if (document.hidden) { stop(); return; }
    // fn() が同期例外を投げても再開（start）は必ず行う。
    try { fn(); } finally { start(); }
  };
  document.addEventListener('visibilitychange', onVisibility);
  if (!document.hidden) start();
  return { stop: () => { stop(); document.removeEventListener('visibilitychange', onVisibility); } };
};

// AI回答の Markdown 表示: 外部ライブラリ非依存の安全サブセット・レンダラ。
// 必ず esc() で全文エスケープしてからパターン変換する（変換元に実タグが存在しないため XSS が起こり得ない）。リンクは [text](http(s)://…) だけを <a> にする（javascript:・data: 等の他スキームと裸の URL は文字のまま）。
// 対応: 太字・斜体・インラインコード・コードブロック・リンク・箇条書き（-／*／+）・番号付き・入れ子リスト・見出し（太字段落程度）・表・引用・水平線・改行。それ以外は素の段落。
// CommonMark/GFM の再実装ではなく、バックスラッシュエスケープ全般などは意図的に非対応。
function _mdInlineSafe(escaped) {
  // escaped は esc() 済み文字列。コードスパンを制御文字のプレースホルダへ退避 → 結合済み文字列に太字/斜体/リンクを適用 → 最後にコードスパンを復元する。
  const codeSpans = [];
  const withPlaceholders = escaped.replace(/`([^`]+?)`/g, (_, code) => {
    codeSpans.push(code);
    return `\x00${codeSpans.length - 1}\x00`;
  });
  const inline = withPlaceholders
    // 太字（*斜体*より先に処理）。単独の `*`（`**COUNT(*)**` 等）は許す。
    .replace(/\*\*((?:(?!\*\*)[\s\S])+?)\*\*/g, '<strong>$1</strong>')
    // 斜体: 開き `*` の直後・閉じ `*` の直前の空白を禁止する（SQL のワイルドカード・COBOL の乗算・glob の `*` を誤って強調にしない）。
    // 中身に `<`/`>` を許さない（太字済みタグ境界を跨いだ対応付けを防ぐ）。
    .replace(/(^|[^*])\*(?!\s)([^*<>]+?)(?<!\s)\*(?!\*)/g, '$1<em>$2</em>')
    // リンク: URL は http(s) のみ・空白を含まない範囲＋1段の対応括弧を許す。太字/斜体の後に処理するため、リンク文字列側の <strong> 等はそのまま包める。
    .replace(/\[([^\]]+?)\]\((https?:\/\/(?:[^\s()]|\([^\s()]*\))+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  return inline.replace(/\x00(\d+)\x00/g, (_, idx) => `<code>${codeSpans[Number(idx)]}</code>`);
}
// 表・リスト項目・コードフェンスで共通に使う行パターン。
const _MD_ITEM_RE = /^(\s*)([-*+]|\d+\.)\s+(.*)$/;
// 開き fence: 3連バッククォートの前後（インデント最大4桁・info string との間・後）に空白を許す。info string の内容は使わない。
const _MD_FENCE_OPEN = /^(\s{0,4})(`{3,})[ \t]*[^`\s]*[ \t]*$/;
// 閉じ fence: 開きと同数以上のバッククォートのみの行。開きの本数を捕捉して判定する（4連で開いたフェンスは内側の3連では閉じない）。
function _mdFenceClose(n) { return new RegExp('^\\s{0,4}`{' + n + ',}\\s*$'); }
// 引用: `>` の直後が空白または行末のときだけ（`>=` などの比較演算子を誤認しない）。
const _MD_QUOTE_RE = /^\s*&gt;(?:\s|$)/;
function _mdStripIndent(line, n) {
  if (n <= 0) return line;
  const lead = line.match(/^\s*/)[0];
  return line.slice(Math.min(lead.length, n));
}
function _mdTableCells(line) {
  // バッククォート区間の外・かつ直前が `\` でない `|` だけで分割する。外側の `|` は任意（境界の空セルは捨てる）。残った `\|` は表示直前に `|` へ戻す。
  const src = line.trim();
  const cells = [];
  let buf = '';
  // 行内で閉じないバッククォート（奇数個）はコードスパンではなく通常文字として扱う（区切りの `|` を取りこぼさないため）。
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
// 表行の構造判定: 外側の `|` は任意・2セル以上。1セルしか無い行は表として扱わない。
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
  // list = {type, items:[{text, children:[list…], extraHtml:[...]}], start}。extraHtml はリスト項目内の字下げコードフェンスなど、インライン処理に通さない組み立て済みブロック HTML。start は先頭項目の番号（<ol> のみ・1 のときは省略）。
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
      // ヘッダの列数を超えるセルは最後のセルへ `|` で連結して 1 行として描画する。
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
    // リスト（- * + ／ 1.）: 連続する項目行を集め、字下げ幅で入れ子にする。項目間の空行1行・項目の内容列以上の字下げの継続行はリストを打ち切らない。
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
          // 空行1行はリスト継続。次行が項目、または現在の項目への継続行なら打ち切らない。
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
            // 字下げされた表の開始行・引用行は項目テキストへ連結せず、リストを閉じて外側のブロック処理に返す。
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

// 担当アナライザの来歴表示: 内部名（Analyzer.name・現行は cobol/copybook/jcl）を平文の表示ラベルへ写像する。ingest.js と chat/render.js が共有する。
// own-property のみを見る（プロトタイプ継承プロパティを返さない）。未知の名前は加工せず String(name) をそのまま返す。
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
