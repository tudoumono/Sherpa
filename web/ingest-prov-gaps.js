// 来歴バッジのうち、読み取りの足りなかった分を知らせるもの（表示のみ）。
const OBSERVATION_GAP_TIP = '画像から読み取った内容の一部を、読み込めなかった・本文に合流できなかったため使えていません。原本を確認してください';
const PPTX_FLAT_GAP_TIP = '図形をたどれなかったスライドは、文字だけを取り出しています。取り消し線や図形の重なりは反映されていないため、原本を確認してください';
const OFFICE_DISPLAY_GAP_TIP = 'Excel の見たままの表示で補う処理が足りなかったため、足りないセルは基本の読み方の値のままです。原本を確認してください';

function provGapBadges(p) {
  const out = [];
  if (Array.isArray(p.observation_issues) && p.observation_issues.length) {
    out.push(`<span class="provbadge warn" title="${esc(OBSERVATION_GAP_TIP)}">画像の読み取り結果の一部を使えていません</span>`);
  }
  if (p.pptx_flat_slides > 0) {
    out.push(`<span class="provbadge warn" title="${esc(PPTX_FLAT_GAP_TIP)}">一部のスライドで図形の取り消し線・重なりを読み取れていません</span>`);
  }
  const d = p.office_display;
  if (d && (d.status === 'partial' || d.status === 'fallback_linux')) {
    const text = d.status === 'partial' ? 'Excel の表示の一部を補えていません' : 'Excel の表示を補えていません';
    out.push(`<span class="provbadge warn" title="${esc(OFFICE_DISPLAY_GAP_TIP)}">${text}</span>`);
  }
  return out;
}
