"""未登録拡張子のテキストファイルを「コード」か「資料」かに振り分け、秘匿ファイルを判定する。

担当なしの拡張子（言語アナライザ・Office/画像・`.md`/`.txt` のいずれでもない）に対して
`corpus_docs.classify_document()` が使う。台帳・出典・grep・ES 全文・read_around まで通し、
ベクトル・グラフ・LLM は通さない。判定は 2 段:
- 第1段（`classify_ext`）: 固定の拡張子表。内容は読まない。
- 第2段（`sniff_content`）: 第1段で決まらない拡張子だけ、先頭数KBの中身から推定する（迷ったら資料）。
秘匿ファイル（`is_sensitive`）は名前・拡張子で両段とも対象外。内容による秘密鍵 PEM ヘッダ検知は第2段のみ。
`re`/`pathlib` 以外を import しない葉ノードとして保つ。
設計: docs/design/rag.md「サイズガードと文字コード・対象外」
"""
from __future__ import annotations

import re
from pathlib import Path

# ---- 第1段: 拡張子マップ（固定表）----------------------------------------------------------
# 一般言語＋設定ファイル系はコード側。言語アナライザ登録簿（`sherpa.ingest.analyzers.registry`）に
# 専用アナライザがある拡張子はここに置かない。
CODE_EXT = frozenset({
    ".py", ".ts",
    ".cpp", ".hpp", ".pl", ".ps1", ".psm1",
    ".go", ".rb", ".php", ".kt", ".swift", ".scala", ".lua", ".r", ".awk",
    # 設定ファイル系（key=value が支配的＝コード側）。環境変数ファイルは `SENSITIVE_EXT` に置く
    ".ini", ".cfg", ".conf", ".json", ".toml",
})

# 資料側（自然文・表データ等）。
DOCUMENT_EXT = frozenset({".csv", ".tsv", ".rtf", ".log"})

# ノイズ拡張子（一時ファイル・対象外）。`.log` は含めない
NOISE_EXT = frozenset({".tmp", ".bak", ".swp", ".lock"})

# 秘匿ファイルの拡張子（実在確認・grep・ES・read_around に出さない。`verify_doc_exists()` が前提にする）
SENSITIVE_EXT = frozenset({".key", ".pem", ".ppk", ".env"})

# 拡張子集合では捕まらない秘匿ファイル名（ドットファイル形の環境変数ファイル・`id_rsa*`・`credentials*`・`.netrc` 等）。
# 比較はファイル名を小文字化してから行う
_SENSITIVE_NAME_EXACT = frozenset({".env", ".netrc", ".npmrc", ".git-credentials"})
# `credentials` はプレフィックス扱い（`credentials.xlsx` 等も秘匿）
_SENSITIVE_NAME_PREFIXES = (".env.", "id_rsa", "credentials")

# 一時ファイルの前綴り（例: Office のロックファイル `~$foo.docx`）。
NOISE_NAME_PREFIXES = ("~$",)

# サイズ上限（grep の上限と同じ 8MiB）。超過は呼び出し側が `reason="size_exceeded"` として台帳へ載せる
MAX_BYTES = 8 * 1024 * 1024

# 表示用 doctype ラベル
CODE_DOCTYPE_LABEL = "コード（汎用）"
DOCUMENT_DOCTYPE_LABEL = "テキスト資料"


def is_noise(name: str, ext: str) -> bool:
    """一時ファイル/ノイズか（拡張子または前綴りで判定・`.log` は対象外に含めない）。"""
    if name.startswith(NOISE_NAME_PREFIXES):
        return True
    return ext in NOISE_EXT


def is_sensitive(name: str, ext: str) -> bool:
    """秘匿ファイルか（`SENSITIVE_EXT` の拡張子、または秘匿名・大文字表記を含む）。

    `ext` は小文字化済みを受け取り、`name` はここで小文字化する。
    """
    if ext in SENSITIVE_EXT:
        return True
    name_l = name.lower()
    return name_l in _SENSITIVE_NAME_EXACT or name_l.startswith(_SENSITIVE_NAME_PREFIXES)


def is_sensitive_doc_id(doc_id: str) -> bool:
    """`doc_id`（相対パス文字列）から `is_sensitive` を判定する。"""
    p = Path(doc_id)
    return is_sensitive(p.name, p.suffix.lower())


def classify_ext(ext: str) -> str | None:
    """第1段: 拡張子だけで判定。`"code"`／`"document"`／`None`（未知＝第2段の対象）。"""
    if ext in CODE_EXT:
        return "code"
    if ext in DOCUMENT_EXT:
        return "document"
    return None


# ---- 第2段: 内容推定（未知拡張子・拡張子なしのみ）--------------------------------------------

# 置換文字の比率がこれを超えたら実質バイナリ
_REPLACEMENT_RATIO_THRESHOLD = 0.02

# `key=value`/`key: value` 行が支配的ならコード寄り。キーは ASCII 識別子に限る（日本語の箇条書きを除くため）
_KV_LINE_RATIO_THRESHOLD = 0.5
_KV_LINE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*\s*[:=]\s*\S")

# 秘密鍵 PEM ヘッダ。名前が秘匿慣習に当たらない秘密鍵も内容で対象外にする
_PRIVATE_KEY_MARKER = "PRIVATE KEY-----"

# 構造記号の出現比率がこれを超えたらコード寄り
_SYMBOL_CHARS = frozenset("{}();<>[]")
_SYMBOL_RATIO_THRESHOLD = 0.02

# コメント行の比率がこれを超えたらコード寄り
_COMMENT_PREFIXES = ("//", "#", "/*", "--", "*")
_COMMENT_LINE_RATIO_THRESHOLD = 0.3

# 日本語（漢字・かな）の比率がこれを超えたら資料
_CJK_RATIO_THRESHOLD = 0.1
_CJK_RANGES = ((0x3040, 0x309F), (0x30A0, 0x30FF), (0x4E00, 0x9FFF))

# 平均行長がこれを超えたら資料寄り
_AVG_LINE_LEN_DOCUMENT_THRESHOLD = 40


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def sniff_content(text: str) -> str:
    """先頭数KB（decode 済み）から `"binary"`／`"code"`／`"document"` を推定する（迷ったら資料）。

    判定順は 秘密鍵ヘッダ→バイナリ→シバン→日本語比率→kv/記号/コメント密度→平均行長。
    日本語比率を kv/記号密度より先にするのは、日本語の箇条書きを `code` と誤判定しないため。
    """
    if not text:
        return "document"                          # 空ファイルは判定材料なし＝資料に倒す
    if _PRIVATE_KEY_MARKER in text:                  # 秘密鍵PEMヘッダ＝名前/拡張子に関わらず無条件対象外
        return "binary"
    if "\x00" in text:
        return "binary"
    repl = text.count("�")
    if repl and repl / len(text) > _REPLACEMENT_RATIO_THRESHOLD:
        return "binary"
    if text.lstrip().startswith("#!"):               # シバン＝スクリプト確定
        return "code"
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return "document"
    cjk_hits = sum(1 for c in text if _is_cjk(c))
    if cjk_hits / len(text) > _CJK_RATIO_THRESHOLD:
        return "document"
    kv_hits = sum(1 for ln in lines if _KV_LINE_RE.match(ln.strip()))
    if kv_hits / len(lines) > _KV_LINE_RATIO_THRESHOLD:
        return "code"
    symbol_hits = sum(1 for c in text if c in _SYMBOL_CHARS)
    if symbol_hits / len(text) > _SYMBOL_RATIO_THRESHOLD:
        return "code"
    comment_hits = sum(1 for ln in lines if ln.strip().startswith(_COMMENT_PREFIXES))
    if comment_hits / len(lines) > _COMMENT_LINE_RATIO_THRESHOLD:
        return "code"
    avg_len = sum(len(ln) for ln in lines) / len(lines)
    if avg_len > _AVG_LINE_LEN_DOCUMENT_THRESHOLD:
        return "document"
    return "document"                                # 上記いずれにも強く倒れない＝資料に倒す
