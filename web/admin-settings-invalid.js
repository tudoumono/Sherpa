// 保存されている接続先（埋め込み・視覚読み取り）の値が不正なときの警告。
const INVALID_PROVIDER_TEXT = '保存されている接続先の設定が正しくありません。接続先を選び直してください。';

function showInvalidProvider(anchorId, id, invalid) {
  const anchor = document.getElementById(anchorId);
  if (!anchor) return;
  let el = document.getElementById(id);
  if (!el) {
    el = document.createElement('div');
    el.id = id;
    el.className = 'hint';
    el.setAttribute('role', 'alert');
    el.style.color = 'var(--danger, #c62828)';
    anchor.insertAdjacentElement('afterend', el);
  }
  el.hidden = !invalid;
  el.textContent = invalid ? INVALID_PROVIDER_TEXT : '';
}

function showInvalidProviders(view) {
  showInvalidProvider('embed-provider-hint', 'embed-provider-invalid', !!(view.embed_provider || {}).invalid);
  showInvalidProvider('vlm-status', 'vlm-provider-invalid', !!(view.vlm || {}).invalid);
}
