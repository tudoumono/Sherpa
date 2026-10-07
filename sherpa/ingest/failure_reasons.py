"""取り込み失敗の生 reason を、利用者向けの原因と対処（閉じた語彙）へ分類する。

`office_md.build_derived()` の各段 `*_failures` の reason 文字列を `REASON_CATALOG` のコードへ写す。
認識できない reason は `other` とし、原文を `detail` に残す。
設計: docs/design/rag.md「サイズガードと文字コード・対象外」
"""
from __future__ import annotations

# 理由コード → 平文ラベル・対処（利用者が見る文言）
REASON_CATALOG: dict[str, dict[str, str]] = {
    "legacy_conversion_timeout": {
        "label": "タイムアウト",
        "advice": "変換に時間がかかりすぎました。ファイルを分割するか、"
                  "管理者が SHERPA_LEGACY_TIMEOUT を延ばすと通ることがあります。",
    },
    "legacy_conversion_failed": {
        "label": "旧形式の変換に失敗",
        "advice": "旧形式（.doc/.xls/.ppt）から新しい形式への変換に失敗しました。"
                  "ファイルが壊れていないか確認するか、新しい形式で保存し直してください。",
    },
    "password_protected": {
        "label": "パスワード保護／暗号化",
        "advice": "パスワードで保護（暗号化）されているため読み取れません。パスワードを解除して保存し直してください。",
    },
    "malformed_structure": {
        "label": "ファイル破損",
        "advice": "ファイルの内部構造が壊れているため読み取れません。開いて保存し直すか、正常な状態に復元してください。",
    },
    "size_exceeded": {
        "label": "サイズ超過",
        "advice": "ファイルが大きすぎて処理できませんでした。ファイルを分割するか、内容を軽量化してください。",
    },
    "cell_count_exceeded": {
        "label": "セル数超過",
        "advice": "シート内のセル数が多すぎて処理できませんでした（xlsx はファイルの圧縮率が高く、"
                  "サイズが小さくてもセル数が多いことがあります）。シートを分割するか、使用範囲を絞ってください。",
    },
    "uncompressed_size_exceeded": {
        "label": "展開後サイズ超過",
        "advice": "ファイルを展開（解凍）した後のサイズが大きすぎて処理できませんでした。"
                  "内容（画像・書式・シート数等）を減らして保存し直してください。",
    },
    "source_parse_failed": {
        "label": "読み取りに失敗（失敗の知らせ）",
        "advice": "中身を読み取れなかったため、検索には失敗の知らせだけが載っています。"
                  "ファイルを開いて保存し直したあと、「再変換」で変換し直してください。",
    },
    "reconvert_reflect_failed": {
        "label": "再変換の反映が終わっていない",
        "advice": "変換し直せましたが、関係グラフか全文検索への反映が終わっていません。"
                  "もう一度「再変換」を押してください。",
    },
    "write_failed": {
        "label": "書き込み失敗",
        "advice": "派生ファイルの書き込みに失敗しました。ディスクの空き容量や権限を確認し、時間をおいて再試行してください。",
    },
    # 文字コード/内容の読み取り判定（`corpus_docs`/`text_encoding`/`ingest.text_kind` 由来）
    "encoding_undetermined": {
        "label": "文字コードを判別できない",
        "advice": "UTF-8でもCP932（Shift-JIS系）でも文字化けするため読み取れません。"
                  "文字コードを確認し、UTF-8で保存し直してください。",
    },
    "binary": {
        "label": "バイナリ",
        "advice": "文字として読み取れない形式（バイナリ）のため対象外です。",
    },
    # アーカイブ取り込み（`ingest.archive_extract`）で展開しない3種類
    "archive_encrypted": {
        "label": "未対応（暗号化）",
        "advice": "パスワード付き（暗号化）のため中身を取り込めません。パスワードを解除して保存し直してください。",
    },
    "archive_nested": {
        "label": "未対応（入れ子）",
        "advice": "アーカイブの中に別のアーカイブ（zip/tar）が入っているため、その中身は取り込めません。"
                  "展開してから登録し直してください。",
    },
    "archive_too_large": {
        "label": "未対応（大きすぎる）",
        "advice": "展開後の件数またはサイズが大きすぎるため取り込めません。アーカイブを分割してください。",
    },
    "other": {
        "label": "その他の失敗",
        "advice": "原因を特定できませんでした。管理者にお問い合わせください。",
    },
}

# 抽出不完全の疑い（失敗ではない別枠・`REASON_CATALOG` には含めない）
PARTIAL_EXTRACTION_LABEL = "抽出不完全の疑い（要確認）"
PARTIAL_EXTRACTION_ADVICE = "本文の一部しか読み取れていない可能性があります。開いて確認し、必要なら保存し直すか再変換してください。"

# `document_ir_failed:<detail>` の detail のうち、そのまま理由コードになるもの
_DOCUMENT_IR_KNOWN_DETAILS = frozenset({"malformed_structure", "password_protected", "size_exceeded"})

# そのまま理由コードとして通す reason
_PASSTHROUGH = frozenset({
    "legacy_conversion_timeout", "legacy_conversion_failed", "size_exceeded",
    "cell_count_exceeded", "uncompressed_size_exceeded", "source_parse_failed", "reconvert_reflect_failed",
})

# 書込失敗の別名
_WRITE_FAILED_ALIASES = frozenset({"write_failed", "manifest_write_failed", "fallback_write_failed"})

# detail を利用者に見せない prefix（`other` へ流す）
_GENERIC_EXCEPTION_PREFIXES = (
    "fallback_failed:", "render_failed:", "build_failed:",
    "unhandled_os_error:", "unhandled_exception:",
)


def classify(raw_reason: str | None) -> str:
    """生 reason を `REASON_CATALOG` のキーへ分類する。認識できなければ `other`。"""
    if not isinstance(raw_reason, str) or not raw_reason:
        return "other"
    if raw_reason in _PASSTHROUGH:
        return raw_reason
    if raw_reason in _WRITE_FAILED_ALIASES:
        return "write_failed"
    if raw_reason.startswith("document_ir_failed:"):
        detail = raw_reason.split(":", 1)[1]
        return detail if detail in _DOCUMENT_IR_KNOWN_DETAILS else "other"
    if raw_reason.startswith(_GENERIC_EXCEPTION_PREFIXES):
        return "other"
    return "other"


def describe(raw_reason: str | None) -> dict[str, str]:
    """`{"code", "label", "advice", "detail"}` を返す（`detail` は元の生 reason）。"""
    code = classify(raw_reason)
    info = REASON_CATALOG[code]
    return {"code": code, "label": info["label"], "advice": info["advice"], "detail": raw_reason or ""}
