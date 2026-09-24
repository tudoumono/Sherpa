"""アーム④の軽量パス（PDF テキスト層）＝`office_md` の現行 PDF 抽出へ委譲する（挙動不変）。

INGEST-MD §5.6 の「④ PDF系」のうち、テキスト層のみを決定的に抽出する軽量ティア（§5 ティア制の第1段）。
バックエンド `pypdf` は `requirements.txt` に**同梱既定**（2026-07-08）。pdfminer.six は任意の追加
バックエンド。いずれも到達不可なら変換不可（`None`）＝従来どおり「未対応」に倒す（fail-safe）。視覚読み取り・
テキスト層ゼロのスキャンPDFだけは、任意の vision アームへ委譲できる。
"""
from __future__ import annotations

from pathlib import Path

from . import ArmResult

_EXTS = {".pdf"}


class PdfTextArm:
    """PDF テキスト層を `office_md.to_markdown` へ委譲（バイト一致）。バックエンド無しは変換不可（None）。

    ティア制（§5・A2）: テキスト層ゼロの PDF を vision が担当すべき時は
    受理しない（決定は `office_md.pdf_escalation_target` に集約＝登録順に依存しない）。既定（`ooxml,pdf_text`＝
    上位ティア無効）では常に `target is None`＝全 PDF を従来どおり受理する（**挙動不変**）。
    """
    name = "pdf_text"

    def available(self) -> bool:
        from .. import office_md
        return office_md.pdf_available()          # テキスト抽出バックエンド（pypdf/pdfminer）の到達性

    def accepts(self, path) -> bool:
        if Path(path).suffix.lower() not in _EXTS:
            return False
        from .. import office_md
        return office_md.pdf_escalation_target(path) is None    # 上位ティアが担当する PDF は譲る

    def convert(self, path) -> ArmResult | None:
        from .. import office_md
        if not office_md.pdf_available():          # バックエンド未導入＝このアームでは変換できない（fail-safe）
            return None
        md = office_md.to_markdown(path)           # 現行の決定的 PDF 抽出に委譲（返り値・None 意味論を維持）
        if md is None:
            return None
        backend = office_md._pdf_backend()
        return ArmResult(md=md, method="pdf_text", confidence=0.9,
                         notes=([f"backend={backend}"] if backend else []))
