"""取り込み失敗の閉じた理由語彙（ING-1・利用者向けの原因＋対処辞書）。

`office_md.build_derived()` の各段 `*_failures`（`office_md_arm`/`legacy_convert` 由来の
`reason` 文字列）を、利用者に平文で説明できる**閉じた**コード集合へ分類する。分類元の
生 `reason` 文字列は主に3系統:
  - `legacy_convert.ensure_ooxml()` 失敗（`office_md._build_derived_into_staging` が
    `legacy_conversion_timeout`／`legacy_conversion_failed` として既に確定済み・そのまま通す）。
  - `document_ir_failed:<detail>`（`ooxml_arm.OoxmlArm.convert()` が例外クラス名または
    `malformed_structure`／`password_protected`／`size_exceeded` を埋め込む）。
  - 各段の書込失敗（`write_failed`／`manifest_write_failed`／`fallback_write_failed`）と
    その他の想定外例外（`fallback_failed:`／`render_failed:`／`build_failed:`／
    `unhandled_os_error:`／`unhandled_exception:` の各プレフィックス）。

語彙に無い/認識できない reason は `other`（原文を `detail` として残す＝情報を捨てない）。
新しい reason を生む変更をする側は、ここへ分類を足すか `other` へ流れることを許容するかを
選ぶ（自動的に語彙が壊れることはない＝fail-safe）。
"""
from __future__ import annotations

# 閉じた理由コード → 平文ラベル・対処（docs/04-画面の原則.md の平文原則・利用者はこの文言だけを見る）。
# 対処は「利用者が自分でできること」を先に、次点で管理者向け（環境変数等）を添える。
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
    "write_failed": {
        "label": "書き込み失敗",
        "advice": "派生ファイルの書き込みに失敗しました。ディスクの空き容量や権限を確認し、時間をおいて再試行してください。",
    },
    "other": {
        "label": "その他の失敗",
        "advice": "原因を特定できませんでした。管理者にお問い合わせください。",
    },
}

# 「抽出不完全の疑い（要確認）」＝失敗ではない別枠（`REASON_CATALOG` には含めない・取り込み自体は成功のまま）。
PARTIAL_EXTRACTION_LABEL = "抽出不完全の疑い（要確認）"
PARTIAL_EXTRACTION_ADVICE = "本文の一部しか読み取れていない可能性があります。開いて確認し、必要なら保存し直すか再変換してください。"

# `document_ir_failed:<detail>` の detail のうち、それ自体が既に閉じた語彙のコードであるもの
# （`ooxml_arm.OoxmlArm.convert()` が例外クラス名の代わりに埋め込む・detail 側の単一の真実源）。
_DOCUMENT_IR_KNOWN_DETAILS = frozenset({"malformed_structure", "password_protected", "size_exceeded"})

# そのまま閉じた語彙として通す reason（`office_md` 側で既に分類済み）。
_PASSTHROUGH = frozenset({
    "legacy_conversion_timeout", "legacy_conversion_failed", "size_exceeded",
    "cell_count_exceeded", "uncompressed_size_exceeded",  # MEM-2（xlsx セル数/非圧縮サイズガード）
})

# 書込失敗の別名（段によって文字列が違うだけで意味は同じ）。
_WRITE_FAILED_ALIASES = frozenset({"write_failed", "manifest_write_failed", "fallback_write_failed"})

# 「例外クラス名を含む prefix:detail」形式のうち、detail 側の情報量が乏しく利用者に見せても
# 意味が無い段（render/build/fallback/unhandled 系）＝ prefix だけで `other` へ流す。
_GENERIC_EXCEPTION_PREFIXES = (
    "fallback_failed:", "render_failed:", "build_failed:",
    "unhandled_os_error:", "unhandled_exception:",
)


def classify(raw_reason: str | None) -> str:
    """`*_failures` の生 `reason` 文字列 → `REASON_CATALOG` のキー（閉じた語彙）。認識できなければ `other`。"""
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
    """`raw_reason` → `{"code", "label", "advice", "detail"}`（`detail`＝分類できた元の生文字列・`other` の内訳表示用）。"""
    code = classify(raw_reason)
    info = REASON_CATALOG[code]
    return {"code": code, "label": info["label"], "advice": info["advice"], "detail": raw_reason or ""}
