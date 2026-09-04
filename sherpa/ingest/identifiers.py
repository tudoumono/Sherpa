"""コード識別子の正規化（COBOL 項目名/プログラム名/コピーブック名 等）の**単一の真実源**。

複数モジュール（static_analysis/world_graph/impact_service）が同じ正規化
（`strip().rstrip(".").upper()`）を各自で定義/再定義していたのを1箇所に集約（RV: DRY）。
※ `scope._norm`（パス prefix 用 `strip("/")`）は目的が違うので統合しない。
"""
from __future__ import annotations


def normalize_code_name(s) -> str:
    """コード識別子を正規化（前後空白/末尾ドット除去・大文字化）。None/空は ""（None 安全）。"""
    return (s or "").strip().rstrip(".").upper()
