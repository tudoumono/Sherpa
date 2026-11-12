// 取り込みタブの「画像の文字を OCR で読み取る」スイッチ。admin-settings.js の取り込みタブの描画・保存・既定に戻す・変更の目印から呼ばれる。
let _ocrBaseline = false;

function renderOcrSwitch(view) {
  const cb = document.getElementById('ocr-enabled');
  const info = view.ocr;
  if (!cb || !info) return;
  const override = info.env_override;
  cb.checked = !!info.effective;
  cb.disabled = override != null;
  _ocrBaseline = !!info.effective;
  const status = document.getElementById('ocr-enabled-status');
  if (!status) return;
  if (override != null) {
    status.textContent = '運用の設定（環境変数）で切に固定されているため、ここでは変えられません。'
      + (info.effective ? '' : '環境変数で止めると次の同期で読み取り済みの文字も検索から外れます。');
  } else {
    status.textContent = (info.configured != null)
      ? 'この設定で固定中です（既定は切です）。'
      : '既定に従っています（既定は切です。変更して保存すると、この内容で固定されます）。';
  }
}

function ocrSwitchChanged() {
  const cb = document.getElementById('ocr-enabled');
  return !!cb && !cb.disabled && cb.checked !== _ocrBaseline;
}

function collectOcrSwitch(body) {
  if (ocrSwitchChanged()) body.ocr_enabled = document.getElementById('ocr-enabled').checked;
}

// 「未設定に戻す」: 欄を既定にし、固定中のときだけ保存で null を送る予定に入れる。
function resetOcrSwitch(view) {
  const info = view.ocr;
  const cb = document.getElementById('ocr-enabled');
  if (!info || !cb || cb.disabled) return;
  cb.checked = !!info.default;
  if (info.configured != null) {
    _planNull('ingest', 'ocr_enabled', () => cb.checked === !!info.default);
  }
}

function markOcrSwitch(mark, view) {
  const info = view.ocr || {};
  mark(document.getElementById('ocr-card'), !!info.effective !== !!info.default);
}
