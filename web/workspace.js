// 個人ワークスペース（マイワークスペース）画面スクリプト。
// セキュリティ: サーバから受け取った全ての文字列は esc() を通す。インラインハンドラなし。
// 不変条件: このページの検索ヒットは「個人ファイル内ヒット」。共有 KB の引用ではない。
'use strict';

const $ = Sherpa.$, esc = Sherpa.esc, api = Sherpa.api, getJSON = Sherpa.getJSON, fmtDateTime = Sherpa.fmtDateTime;

function toast(msg) {
  const t = $('toast'); if (!t) return;
  t.textContent = msg; t.classList.add('show');
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 2000);
}

function fmtSize(bytes) {
  if (bytes == null) return '—';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / 1024 / 1024).toFixed(1) + ' MB';
}

function fmtDate(s) {
  if (!s) return '—';
  return fmtDateTime(s) || '—';
}

// ===== ファイル一覧 =====

async function loadFiles() {
  const el = $('file-list');
  const cnt = $('file-count');
  if (!el) return;
  el.setAttribute('aria-busy', 'true');
  el.innerHTML = '<div class="loading" role="status" style="padding:16px 0"><span class="spinner spinner-sm"></span><span>個人ワークスペースを読み込んでいます...</span></div>';
  try {
    const data = await getJSON('/workspace/files');
    const files = data.files || [];
    if (cnt) cnt.textContent = `(${files.length} 件)`;
    if (!files.length) {
      el.innerHTML = '<div class="muted" style="padding:16px 0;text-align:center">ファイルがありません</div>';
      return;
    }
    // data-* 属性に ID を持たせて委譲クリックで削除（インラインハンドラなし）。
    const rows = files.map((f) =>
      `<tr>
        <td style="font-family:var(--font-code,monospace);font-size:12.5px">${esc(f.rel_path)}</td>
        <td style="text-align:right;white-space:nowrap;color:var(--ink-3)">${esc(fmtSize(f.size_bytes))}</td>
        <td style="color:var(--ink-3);white-space:nowrap">${esc(fmtDate(f.created_at))}</td>
        <td style="color:var(--ink-3);white-space:nowrap">${esc(fmtDate(f.expires_at) || '—')}</td>
        <td><button class="del-btn" data-fileid="${esc(String(f.id))}" data-name="${esc(f.rel_path)}">削除</button></td>
      </tr>`
    ).join('');
    el.innerHTML = `<table class="file-table"><thead><tr>
      <th>ファイル名</th><th style="text-align:right">サイズ</th>
      <th>アップロード日時</th><th>有効期限</th><th></th>
    </tr></thead><tbody>${rows}</tbody></table>`;
  } catch (e) {
    el.innerHTML = `<div class="muted" style="color:var(--danger);padding:12px 0">読み込みエラー: ${esc(e.message)}。ページを再読み込みするか、ログイン状態を確認してください。</div>`;
  } finally {
    el.setAttribute('aria-busy', 'false');
  }
}

// 委譲クリック: file-list 内の削除ボタン。
document.addEventListener('click', async (ev) => {
  const btn = ev.target.closest('[data-fileid]');
  if (!btn) return;
  const fid = btn.dataset.fileid;
  const name = btn.dataset.name || fid;
  if (!confirm(`「${name}」を削除しますか？`)) return;
  btn.disabled = true;
  try {
    await api('DELETE', `/workspace/files/${fid}`);
    toast('削除しました');
    loadFiles();
  } catch (e) {
    btn.disabled = false;
    toast('削除に失敗しました: ' + e.message);
  }
});

// ===== アップロード =====

async function uploadFile(file) {
  const msgEl = $('upload-msg');
  const pw = $('progress-wrap');
  const pb = $('progress-bar');
  if (msgEl) {
    msgEl.setAttribute('role', 'status');
    msgEl.innerHTML = `<span class="loading-inline"><span class="spinner spinner-sm"></span><span>アップロード中: ${esc(file.name)}</span></span>`;
  }
  if (pw) pw.style.display = '';
  if (pb) pb.style.width = '30%';
  const fd = new FormData();
  fd.append('file', file, file.name);
  try {
    const r = await fetch('/workspace/files', { method: 'POST', body: fd });
    let data = null;
    try { data = await r.json(); } catch (_) {}
    if (!r.ok) throw new Error((data && (data.detail || data.message)) || `エラー (${r.status})`);
    if (pb) pb.style.width = '100%';
    const savedName = (data && data.rel_path) || file.name;
    if (msgEl) msgEl.textContent = `アップロード完了: ${savedName}`;
    return true;
  } catch (e) {
    if (msgEl) msgEl.textContent = `エラー (${file.name}): ${e.message}`;
    return false;
  } finally {
    setTimeout(() => { if (pw) pw.style.display = 'none'; if (pb) pb.style.width = '0%'; }, 1200);
  }
}

async function handleFiles(fileList) {
  let ok = 0, ng = 0;
  for (const f of Array.from(fileList)) {
    const r = await uploadFile(f);
    if (r) ok++; else ng++;
  }
  toast(`${ok} 件アップロード${ng ? ` / ${ng} 件エラー` : ''}`);
  loadFiles();
}

function initUpload() {
  const zone = $('dropzone');
  const inp = $('file-input');
  if (!zone || !inp) return;

  zone.addEventListener('click', () => inp.click());
  inp.addEventListener('change', () => { if (inp.files && inp.files.length) handleFiles(inp.files); inp.value = ''; });

  zone.addEventListener('dragover', (e) => { e.preventDefault(); zone.classList.add('drag'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('drag'));
  zone.addEventListener('drop', (e) => {
    e.preventDefault(); zone.classList.remove('drag');
    if (e.dataTransfer && e.dataTransfer.files.length) handleFiles(e.dataTransfer.files);
  });
}

// ===== 個人 grep 検索 =====
// 不変条件: /workspace/search の結果は個人ファイル内ヒットのみ。共有 KB とは無関係。

async function doSearch() {
  const q = ($('search-q') || { value: '' }).value.trim();
  const res = $('search-results');
  if (!q || !res) return;
  res.setAttribute('aria-busy', 'true');
  res.innerHTML = '<div class="loading-inline" role="status"><span class="spinner spinner-sm"></span><span>個人ファイルを検索しています...</span></div>';
  try {
    const params = new URLSearchParams({ q });
    const data = await getJSON(`/workspace/search?${params}`);
    const hits = data.hits || [];
    if (!hits.length) {
      res.innerHTML = '<div class="muted" style="padding:8px 0">一致なし</div>';
      return;
    }
    // 個人ファイル内ヒットバッジ付きで表示。
    const badge = '<span class="personal-badge" style="margin-right:6px">個人ファイル内ヒット</span>';
    res.innerHTML = `<div style="font-size:12px;color:var(--ink-3);margin-bottom:8px">${badge}${esc(String(hits.length))} 件</div>`
      + hits.map((h) =>
        `<div class="hit-card">
          <div class="hit-meta">${esc(h.rel_path)} — ${esc(String(h.line))} 行目</div>
          <pre>${esc(h.text)}</pre>
        </div>`
      ).join('');
  } catch (e) {
    res.innerHTML = `<div class="muted" style="color:var(--danger)">検索エラー: ${esc(e.message)}。検索語とログイン状態を確認してください。</div>`;
  } finally {
    res.setAttribute('aria-busy', 'false');
  }
}

function initSearch() {
  const btn = $('search-btn');
  const inp = $('search-q');
  if (btn) btn.addEventListener('click', doSearch);
  if (inp) inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') doSearch(); });
}

// ===== テーマ切替（全ページ共通パターン）=====
function initTheme() {
  const btn = $('themebtn');
  if (!btn) return;
  const root = document.documentElement;
  btn.addEventListener('click', () => {
    const next = root.dataset.theme === 'light' ? 'dark' : 'light';
    root.dataset.theme = next;
    localStorage.setItem('sherpa-theme', next);
    btn.textContent = next === 'dark' ? '☀️' : '🌙';
  });
  btn.textContent = root.dataset.theme === 'dark' ? '☀️' : '🌙';
}

// ===== 初期化 =====
document.addEventListener('DOMContentLoaded', () => {
  initUpload();
  initSearch();
  initTheme();
  loadFiles();
});
