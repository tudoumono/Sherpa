// 資料の画面のタブ切り替え（取り込み状況／優先と除外／設定と点検）。パネルは消さず hidden で隠す。
(() => {
  const area = document.getElementById('tabarea');
  const tabs = [...area.querySelectorAll('[role="tab"]')];
  let current = 'status';

  function select(name) {
    if (!tabs.some((t) => t.dataset.tab === name)) return;
    const changed = name !== current;
    current = name;
    tabs.forEach((t) => {
      const on = t.dataset.tab === name;
      t.setAttribute('aria-selected', on ? 'true' : 'false');
      document.getElementById('tab-' + t.dataset.tab).hidden = !on;
    });
    if (changed && name === 'rules' && window.RulesTab) window.RulesTab.open();
  }

  tabs.forEach((t) => t.addEventListener('click', () => select(t.dataset.tab)));
  // 資料フォルダが無くなってタブごと隠れたら、次に出るときは既定のタブから始める。
  new MutationObserver(() => {
    if (!area.hidden) return;
    select('status');
    if (window.RulesTab) window.RulesTab.discard();
  }).observe(area, { attributes: true, attributeFilter: ['hidden'] });

  window.IngestTabs = { select };
})();
