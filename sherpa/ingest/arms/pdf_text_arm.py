"""PDF テキスト層アーム。テキスト層のみを決定的に抽出する軽量ティアで、`office_md` の PDF 抽出へ委譲する。

バックエンドは `pypdf`（同梱既定）または任意の pdfminer.six。どちらも到達不可なら変換不可（`None`）＝「未対応」に倒す。テキスト層ゼロのスキャン PDF は、有効なら `vision` アームが担当する。
設計: docs/design/rag.md「アーム一覧」
"""
from __future__ import annotations

from pathlib import Path

from . import ArmResult

_EXTS = {".pdf"}


class PdfTextArm:
    """PDF テキスト層を `office_md.to_markdown` へ委譲する。バックエンド無しは変換不可（None）。

    テキスト層ゼロの PDF を上位ティア（vision）が担当すべき時は受理しない（決定は `office_md.pdf_escalation_target`）。既定では上位ティア無効なので全 PDF を受理する。
    """
    name = "pdf_text"

    def available(self) -> bool:
        from .. import office_md
        return office_md.pdf_available()  # テキスト抽出バックエンド（pypdf/pdfminer）の到達性

    def accepts(self, path) -> bool:
        if Path(path).suffix.lower() not in _EXTS:
            return False
        from .. import office_md
        return office_md.pdf_escalation_target(path) is None  # 上位ティアが担当する PDF は譲る

    def convert(self, path) -> ArmResult | None:
        from .. import office_md
        if not office_md.pdf_available():  # バックエンド未導入＝このアームでは変換できない
            return None
        md = office_md.to_markdown(path)  # `office_md` の PDF 抽出に委譲する（返り値・None 意味論を維持）
        if md is None:
            return None
        backend = office_md._pdf_backend()
        return ArmResult(md=md, method="pdf_text", confidence=0.9,
                         notes=([f"backend={backend}"] if backend else []))
