// ---- 資料の探し方の設定（COPY の取り込み元の場所・パスの別名・名前の対応）----
let _resolveWid = null;
const ruleLines = (rules) => (rules || []).map((r) => `${r.from} = ${r.to}`).join('\n');
function showRules(rules) {
  $('rs-names').value = ruleLines(rules);
  if ((rules || []).length) $('rs-names').closest('details').open = true;
}
function resolveMsg(html) { $('rs-msg').innerHTML = html; }
async function loadResolveSettings(wid) {
  _resolveWid = wid;
  try {
    const d = await api('GET', `/worlds/${encodeURIComponent(wid)}/resolve-settings`);
    $('rs-copy').value = (d.copy_paths || []).join('\n');
    $('rs-alias').value = Object.entries(d.path_aliases || {}).map(([k, v]) => `${k} = ${v}`).join('\n');
    showRules(d.name_rules);
    resolveMsg(resolveWarnings(d));
  } catch (e) {
    resolveMsg(`<span class="danger">設定を取得できません: ${esc(e.message)}</span>`);
  }
}
function resolveWarnings(d) {
  return (d.warnings || []).map((w) => `<div class="danger">※ ${esc(w)}</div>`).join('');
}
async function saveResolveSettings() {
  if (!_resolveWid) return;
  const copy_paths = $('rs-copy').value.split('\n').map((l) => l.trim()).filter(Boolean);
  const path_aliases = {};
  for (const line of $('rs-alias').value.split('\n').map((l) => l.trim()).filter(Boolean)) {
    const i = line.indexOf('=');
    if (i < 0) { resolveMsg(`<span class="danger">別名は「別名 = 場所」の形で書いてください: ${esc(line)}</span>`); return; }
    path_aliases[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  }
  const name_rules = [];
  for (const line of $('rs-names').value.split('\n').map((l) => l.trim()).filter(Boolean)) {
    const i = line.indexOf('=');
    if (i < 0) { resolveMsg(`<span class="danger">名前の対応は「参照の名前 = 定義の名前」の形で書いてください: ${esc(line)}</span>`); return; }
    name_rules.push({ from: line.slice(0, i).trim(), to: line.slice(i + 1).trim() });
  }
  if (!confirm('保存すると資料フォルダ全体を取り込み直します（時間がかかります）。保存しますか？')) return;
  $('rs-save').disabled = true;
  try {
    const d = await api('PUT', `/worlds/${encodeURIComponent(_resolveWid)}/resolve-settings`, { copy_paths, path_aliases, name_rules });
    $('rs-copy').value = d.copy_paths.join('\n');
    $('rs-alias').value = Object.entries(d.path_aliases).map(([k, v]) => `${k} = ${v}`).join('\n');
    showRules(d.name_rules);
    resolveMsg(`${d.changed && !d.refresh_started ? '⏳' : '✓'} ${esc(d.note)}${resolveWarnings(d)}`);
    if (d.refresh_started) loadStat(_resolveWid);
  } catch (e) {
    resolveMsg(`<span class="danger">保存できません: ${esc(e.message)}</span>`);
  } finally {
    $('rs-save').disabled = false;
  }
}
Sherpa.$('rs-save').addEventListener('click', saveResolveSettings);
