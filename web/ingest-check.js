// 資料の画面の「フォルダの置き方を点検」。押すと GET /worlds/{wid}/folder-check を呼び、3 段の件数と項目の表を出す。
// 設計: docs/design/scope.md「資料フォルダの登録・解決・付け替え（registry）」
(() => {
  const $ = Sherpa.$, esc = Sherpa.esc, getJSON = Sherpa.getJSON;
  const ORDER = ['合わない', '注意', '合う'];
  const TAG = { '合う': 'b-static', '注意': 'b-llm', '合わない': 'statustag deprecated' };

  function counts(items) {
    const n = { '合う': 0, '注意': 0, '合わない': 0 };
    items.forEach((it) => { n[it.result] += 1; });
    return n;
  }

  function row(it) {
    const ex = (it.examples || []).length
      ? `<div class="muted" style="font-size:var(--text-small);margin-top:4px">${it.examples.map(esc).join('<br>')}</div>` : '';
    return `<tr><td><span class="badge ${TAG[it.result]}">${esc(it.result)}</span></td>`
      + `<td>${esc(it.path || '（全体）')}</td>`
      + `<td>${esc(it.what)}${ex}</td><td>${esc(it.treatment)}</td><td>${esc(it.fix || '')}</td></tr>`;
  }

  function table(items) {
    return `<table class="grid"><thead><tr><th style="width:80px">結果</th><th style="width:160px">場所</th>`
      + `<th>何が</th><th>次の取り込み直しの版での扱い</th><th>次の取り込み直しの版に向けた直し方</th></tr></thead><tbody>${items.map(row).join('')}</tbody></table>`;
  }

  function render(world, data) {
    const items = [...data.items].sort((a, b) => ORDER.indexOf(a.result) - ORDER.indexOf(b.result));
    const n = counts(items);
    const first = items.filter((it) => it.result !== '合う'), good = items.filter((it) => it.result === '合う');
    const at = data.ledger_at ? `最後の取り込み（${esc(Sherpa.fmtDateTime(data.ledger_at))}）の時点の名前で点検しました。` : '';
    $('chk-out').innerHTML =
      `<div style="margin-bottom:8px"><span class="badge b-static">合う ${n['合う']} 件</span> `
      + `<span class="badge b-llm">注意 ${n['注意']} 件</span> `
      + `<span class="badge statustag deprecated">合わない ${n['合わない']} 件</span></div>`
      + (at ? `<div class="muted" style="font-size:var(--text-small);margin-bottom:8px">${at}</div>` : '')
      + (first.length ? table(first) : '<div class="empty">注意・合わないものはありません</div>')
      + (good.length ? `<details style="margin-top:8px"><summary>合うもの（${good.length} 件）</summary>${table(good)}</details>` : '')
      + ((data.not_counted || []).length ? `<details style="margin-top:8px"><summary>数えていないもの（${data.not_counted.length} 件）</summary>${data.not_counted.map((t) => `<div class="muted" style="font-size:var(--text-small)">${esc(t)}</div>`).join('')}</details>` : '')
      + `<div style="margin-top:8px"><a id="chk-md" href="/worlds/${encodeURIComponent(world)}/folder-check?format=md" download>点検の結果をダウンロード（Markdown）</a></div>`;
  }

  async function run() {
    const world = $('version').value, btn = $('chk-btn');
    btn.disabled = true;
    $('chk-out').innerHTML = '<div class="loading"><span class="spinner spinner-sm"></span>点検中…</div>';
    try {
      render(world, await getJSON(`/worlds/${encodeURIComponent(world)}/folder-check`));
    } catch (e) {
      $('chk-out').innerHTML = `<div class="danger">${esc(e.message)}</div>`;
    } finally {
      btn.disabled = false;
    }
  }
  $('chk-btn').addEventListener('click', run);
})();
