"""M3 案2（proposals/2026-07-07-Marpスライド作成.md）実機統合テスト。

実 marp CLI ＋ 実 Chromium ＋ 実 unshare を使って、`sherpa/marp_render.py` が実際に
HTML/PDF/PPTX を生成できることを検証する（tests/unit/test_marp_render.py はフェイク marp
バイナリで配線だけを見るため、実バイナリでの成功を別途確認する）。

marp CLI／Chromium／unshare のいずれかが無い環境では SKIP（runner を赤にしない・
tests/integration の既存流儀＝到達できなければ SKIP）。DB 不要。
"""
from __future__ import annotations

import pathlib
import shutil
import tempfile

import pytest

from sherpa import agents as A
from sherpa import marp_render as R

_MD = """\
---
marp: true
theme: sherpa
paginate: true
---

# テストスライド

M3 実機統合テスト用の最小 Marp Markdown。

---

## 2枚目

- 項目1
- 項目2
"""


def _skip_reason() -> str | None:
    if A._marp_bin() is None:
        return "この環境には marp CLI（tools/marp）が導入されていない"
    if A._detect_chrome_path() is None:
        return "この環境には Chromium（CHROME_PATH/Playwright）が無い"
    if not R._unshare_available():
        # RV round2: バイナリ有無でなく実プローブ（lo UP まで）で判定＝unshare はあるが
        # user namespace 禁止の環境でも正しく skip する。
        return "この環境では unshare -rn（lo UP まで）が使えない"
    return None


def test_render_outputs_real_marp_all_three_formats():
    reason = _skip_reason()
    if reason:
        pytest.skip(reason)

    with tempfile.TemporaryDirectory() as td:
        tdp = pathlib.Path(td)
        md = tdp / "テスト資料.md"
        md.write_text(_MD, encoding="utf-8")

        got = R.render_outputs(
            [md], marp_bin=A._marp_bin(), chrome_path=A._detect_chrome_path(),
            theme_dirs=[pathlib.Path(__file__).resolve().parents[2]
                        / "sherpa" / "skills_base" / "marp" / "themes"],
            containment_root=tdp,
        )
        by_suffix = {p.suffix: p for p in got}
        assert set(by_suffix) == {".html", ".pdf", ".pptx"}

        html_bytes = by_suffix[".html"].read_bytes()
        assert b"<html" in html_bytes.lower()

        pdf_bytes = by_suffix[".pdf"].read_bytes()
        assert pdf_bytes[:4] == b"%PDF"

        pptx_bytes = by_suffix[".pptx"].read_bytes()
        assert pptx_bytes[:2] == b"PK"          # PPTX は zip 形式
