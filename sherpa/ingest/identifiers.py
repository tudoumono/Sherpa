"""コード識別子（COBOL の項目名・プログラム名・コピーブック名など）の正規化。"""
from __future__ import annotations


def normalize_code_name(s) -> str:
    """コード識別子を正規化（前後空白/末尾ドット除去・大文字化）。None/空は ""（None 安全）。"""
    return (s or "").strip().rstrip(".").upper()
