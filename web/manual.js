'use strict';

// マニュアル一本化（M-A・docs/proposals/2026-07-08-マニュアル一本化.md）: 正本は docs/manual/*.md。
// この画面は「MD レンダラ」に徹し、本文を一切持たない（以後の更新は docs/manual/*.md を直すだけ）。
//
// 流れ: manifest.json（章の目次）を取得 → 章を選ぶと該当 .md を取得 → marked でレンダ →
// 許可リスト方式でサニタイズ・再構築（DOMParser の不活性ドキュメント上で厳格に絞る。正本は自
// リポジトリの MD だが多層防御）→ 表示。
//
// 既存アンカー互換: manifest の id は旧 manual.js の id（start/chat/register/graph/workspace/
// settings/sysadmin 等）に合わせてある。他画面の help-link（manual.html#settings 等）は変更不要。

const esc = Sherpa.esc;

const MANIFEST_URL = 'manual-src/manifest.json';
const SRC_BASE = 'manual-src/';
const IMAGES_BASE = 'manual-images/';

let CHAPTERS = [];
let GROUPS = [];
let byId = {};
let fileToId = {};   // MD ファイル名 → 章 id（章間の相対リンクを #id へ書き換えるための対応表）
const mdCache = new Map();   // file名 → 取得済み Markdown 本文

function textOf(ch) {
  return [ch.title, ch.summary, ch.group, ...(ch.tags || [])].join(' ').toLowerCase();
}

function currentId() {
  const id = decodeURIComponent((location.hash || '').replace(/^#/, ''));
  return byId[id] ? id : 'start';
}

// ===== サニタイズ（Codex RV 2026-07-08 High1）=====
// ブロックリスト（script/on*/javascript: だけ除去）は iframe srcdoc・object/embed・form・
// style・meta refresh・data:text/html・外部 img 自動読み込みなどを通してしまう。ここでは
// **許可リスト方式**へ転換: DOMParser の不活性ドキュメント（onload/onerror 等は発火しない）で
// 一度パースし、許可タグ・許可属性だけを新規 DOM として組み立て直す（元ノードの属性を素通しせず、
// 検査した値だけを新しい要素にコピーする）。ライブ DOM への挿入は呼び出し側が最後に一度だけ行う。

// 内容ごと除去するタグ（script 実行・外部読み込み・フォーム送信・スタイル注入・メタリフレッシュ等、
// 中身を見せる意味がなく危険な経路になり得るもの）。
const _DROP_SUBTREE_TAGS = new Set([
  'SCRIPT', 'STYLE', 'IFRAME', 'OBJECT', 'EMBED', 'FORM', 'INPUT', 'BUTTON', 'TEXTAREA',
  'SELECT', 'OPTION', 'LINK', 'META', 'BASE', 'SVG', 'MATH', 'VIDEO', 'AUDIO', 'SOURCE',
  'TRACK', 'CANVAS', 'NOSCRIPT', 'TEMPLATE', 'APPLET', 'FRAME', 'FRAMESET', 'MARQUEE', 'XMP',
]);
// タグ自体だけ外し、子（テキスト・許可された子孫）は残す（unwrap）。想定外の不明タグ・
// del/sup/sub 等の未対応インライン装飾がここに落ちる＝見た目が少し変わるだけの安全側フォールバック。
const _ALLOWED_TAGS = new Set([
  'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'P', 'UL', 'OL', 'LI', 'TABLE', 'THEAD', 'TBODY', 'TR',
  'TH', 'TD', 'BLOCKQUOTE', 'PRE', 'CODE', 'STRONG', 'EM', 'B', 'I', 'A', 'IMG', 'HR', 'BR',
  'FIGURE', 'FIGCAPTION', 'SPAN', 'DIV',
]);

// MD 内の相対画像参照（`images/xxx.png`・`./images/xxx.png`・クエリ付きも可）を、api.py の既存配信
// （/ui/manual-images/）へ書き換える。対象外（外部 URL・data: 等）は null を返し、呼び出し側が
// img 自体を落とす（自動読み込み＝オフライン契約・情報漏えい経路のため許可しない）。
function _resolveImgSrc(raw) {
  let src = String(raw || '').trim();
  if (src.startsWith('./')) src = src.slice(2);
  if (src.toLowerCase().startsWith('images/')) return IMAGES_BASE + src.slice('images/'.length);
  return null;
}

// `<a href>` の解決（Codex RV Med1 も兼ねる）。優先順:
// 1) `#...` の同一ページ内アンカーはそのまま許可。
// 2) 章間の相対 MD リンク（`12-....md` や `./12-....md`・`#節` 付きは章頭に落とす）は
//    manifest の id 対応表から `#<章id>` へ書き換える。manifest に無い .md 参照（例: 上位
//    ディレクトリの設計ドキュメントへのリンク）はプレーンテキスト化（リンク解除）。
// 3) `//`・`http(s)://` は外部リンクとして許可（利用者クリック起点＝オフライン契約に反しない）。
// 4) それ以外の相対パス（スキーム無し）はそのまま許可。
// 5) `javascript:`/`data:` 等の未知スキームは不許可（プレーンテキスト化）。
function _resolveAnchorHref(raw) {
  const href = String(raw || '').trim();
  if (!href) return { href: null };
  if (href.startsWith('#')) return { href };
  const hashIdx = href.indexOf('#');
  let path = hashIdx >= 0 ? href.slice(0, hashIdx) : href;
  if (path.startsWith('./')) path = path.slice(2);
  if (path.toLowerCase().endsWith('.md')) {
    // marked は href を URI エンコードして出力する（日本語ファイル名は %E4%BD%BF... のように
    // パーセントエンコードされる）。manifest の file は生の Unicode 文字列なので、比較前に復号する
    // （復号に失敗する不正なエスケープは raw のまま比較にフォールバック）。
    let decoded = path;
    try { decoded = decodeURIComponent(path); } catch (_) { /* malformed escape; keep raw */ }
    const chId = fileToId[decoded] || fileToId[path];
    return chId ? { href: `#${chId}` } : { href: null };
  }
  if (/^(https?:)?\/\//i.test(href)) return { href, external: true };
  if (!/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(href)) return { href };   // スキーム無し＝相対パスとして許可
  return { href: null };   // javascript: / data: / mailto: 等の未知スキームは不許可
}

// 許可した要素だけを新規 DOM として組み立て直す（元要素の属性は一切コピーせず、検査した値だけを
// 新要素に設定する）。戻り値は「置き換え後のノード配列」（0件＝除去・2件以上＝unwrap の子）。
function _sanitizeNode(node) {
  if (node.nodeType === Node.TEXT_NODE) return [document.createTextNode(node.nodeValue)];
  if (node.nodeType !== Node.ELEMENT_NODE) return [];   // コメント等は除去

  const tag = node.tagName;
  if (_DROP_SUBTREE_TAGS.has(tag)) return [];   // 内容ごと除去

  const children = Array.from(node.childNodes).flatMap((c) => _sanitizeNode(c));
  if (!_ALLOWED_TAGS.has(tag)) return children;   // 不明タグは unwrap（子だけ残す）

  if (tag === 'IMG') {
    const src = _resolveImgSrc(node.getAttribute('src'));
    if (!src) return [];   // manual-images/ に解決できない画像は除去（外部読み込み禁止）
    const img = document.createElement('img');
    img.setAttribute('src', src);
    const alt = node.getAttribute('alt');
    if (alt != null) img.setAttribute('alt', alt);
    const title = node.getAttribute('title');
    if (title != null) img.setAttribute('title', title);
    return [img];   // img は空要素＝子を持たない
  }

  const clean = document.createElement(tag);
  if (tag === 'A') {
    const { href, external } = _resolveAnchorHref(node.getAttribute('href'));
    if (href != null) {
      clean.setAttribute('href', href);
      if (external) {
        clean.setAttribute('target', '_blank');
        clean.setAttribute('rel', 'noopener');
      }
    }
  } else {
    const cls = node.getAttribute('class');   // code の言語クラス（language-xxx）表示用のみ許可
    if (cls) clean.setAttribute('class', cls);
  }
  children.forEach((c) => clean.appendChild(c));
  return [clean];
}

function sanitizeHtml(html) {
  const parsed = new DOMParser().parseFromString(html, 'text/html');   // 不活性ドキュメント＝非実行
  const out = document.createElement('div');
  Array.from(parsed.body.childNodes).flatMap((c) => _sanitizeNode(c)).forEach((n) => out.appendChild(n));
  return out.innerHTML;
}

function addCodeCopyButtons(root) {
  root.querySelectorAll('pre').forEach((pre) => {
    const btn = document.createElement('button');
    btn.className = 'manual-copy';
    btn.type = 'button';
    btn.textContent = 'コピー';
    btn.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(pre.innerText.replace(/\s*コピー$/, ''));
        btn.textContent = 'コピー済み';
        setTimeout(() => { btn.textContent = 'コピー'; }, 1200);
      } catch (_) {
        btn.textContent = '失敗';
        setTimeout(() => { btn.textContent = 'コピー'; }, 1200);
      }
    });
    pre.appendChild(btn);
  });
}

function renderNav(filter = '') {
  const nav = document.getElementById('manual-nav');
  const q = filter.trim().toLowerCase();
  const matches = q ? CHAPTERS.filter((ch) => textOf(ch).includes(q)) : CHAPTERS;
  const matchIds = new Set(matches.map((ch) => ch.id));

  document.getElementById('manual-filter-result').hidden = !q;
  document.getElementById('manual-filter-result').textContent = q ? `${matches.length}件のページが一致しました` : '';

  nav.innerHTML = GROUPS.map((group) => {
    const chs = CHAPTERS.filter((ch) => ch.group === group && matchIds.has(ch.id));
    if (!chs.length) return '';
    return `<div class="manual-nav-group"><h2>${esc(group)}</h2>${chs.map((ch) => (
      `<a href="#${esc(ch.id)}" data-doc="${esc(ch.id)}">${esc(ch.title)}<small>${esc(ch.summary)}</small></a>`
    )).join('')}</div>`;
  }).join('');
  highlightNav(currentId());
}

function highlightNav(id) {
  document.querySelectorAll('.manual-nav a').forEach((a) => {
    a.classList.toggle('on', a.dataset.doc === id);
  });
}

async function fetchMd(file) {
  if (mdCache.has(file)) return mdCache.get(file);
  const res = await fetch(SRC_BASE + encodeURIComponent(file));
  if (!res.ok) throw new Error(`このページを読み込めませんでした（${res.status}）`);
  const text = await res.text();
  mdCache.set(file, text);
  return text;
}

async function renderDoc(id) {
  const ch = byId[id] || byId.start;
  document.getElementById('doc-title').textContent = ch.title;
  document.getElementById('doc-summary').textContent = ch.summary;
  const article = document.getElementById('manual-doc');
  const tagsHtml = `<div class="manual-tags">${(ch.tags || []).map((t) => `<span>${esc(t)}</span>`).join('')}</div>`;
  article.innerHTML = `${tagsHtml}<p class="manual-loading">読み込み中...</p>`;
  highlightNav(ch.id);
  const main = document.querySelector('.manual-main');

  try {
    const raw = await fetchMd(ch.file);
    const html = sanitizeHtml(window.marked.parse(raw));
    article.innerHTML = tagsHtml + html;
    addCodeCopyButtons(article);

    if (main) main.scrollTop = 0;
    if (ch.anchor) {
      const heading = Array.from(article.querySelectorAll('h1, h2, h3'))
        .find((h) => h.textContent.trim() === ch.anchor);
      if (heading) heading.scrollIntoView({ block: 'start' });
    }
  } catch (err) {
    article.innerHTML = `${tagsHtml}<p class="manual-error">このページを読み込めませんでした。時間をおいて再度お試しください。</p>`;
    if (main) main.scrollTop = 0;
  }
}

function applyThemeIcon() {
  const b = document.getElementById('themebtn');
  if (b) b.textContent = document.documentElement.dataset.theme === 'dark' ? '☀️' : '🌙';
}

async function init() {
  const res = await fetch(MANIFEST_URL);
  if (!res.ok) throw new Error(`目次を読み込めませんでした（${res.status}）`);
  const manifest = await res.json();
  CHAPTERS = manifest.chapters || [];
  GROUPS = [...new Set(CHAPTERS.map((ch) => ch.group))];
  byId = Object.fromEntries(CHAPTERS.map((ch) => [ch.id, ch]));
  fileToId = Object.fromEntries(CHAPTERS.map((ch) => [ch.file, ch.id]));

  document.getElementById('doc-search').addEventListener('input', (e) => {
    renderNav(e.target.value);
  });
  window.addEventListener('hashchange', () => renderDoc(currentId()));
  document.addEventListener('click', (e) => {
    if (!e.target.closest('#themebtn')) return;
    const d = document.documentElement;
    const next = d.dataset.theme === 'dark' ? 'light' : 'dark';
    d.dataset.theme = next;
    localStorage.setItem('sherpa-theme', next);
    applyThemeIcon();
  });

  renderNav('');
  renderDoc(currentId());
  applyThemeIcon();
}

init().catch(() => {
  const nav = document.getElementById('manual-nav');
  if (nav) nav.innerHTML = '<p class="manual-error" style="padding:12px">目次を読み込めませんでした。</p>';
  document.getElementById('doc-title').textContent = '読み込みエラー';
  document.getElementById('manual-doc').innerHTML = '<p class="manual-error">マニュアルを読み込めませんでした。時間をおいて再度お試しください。</p>';
});
