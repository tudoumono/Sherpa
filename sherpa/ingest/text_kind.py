"""軽量テキスト枠の種別判定（未登録拡張子のテキストファイル→コード/資料への振り分け）。

ユーザー裁定 2026-09-02: 未登録拡張子のテキストファイルも台帳・出典・コマンド検索(grep/glob)まで
通す（ベクトル・グラフ・LLM は一切通さない＝取り込みコスト増ゼロ）。判定は
`corpus_docs.classify_document()` の「担当なし」経路（既存の言語アナライザ登録簿・Office/画像・
`.md`/`.txt` のいずれにも該当しない拡張子）に対してのみ適用する——既存の判定は常に優先し、
ここでは重複させない（本モジュール自身の拡張子集合も既存登録簿と重ならないよう選定する）。

判定は2段構え（**2段の扱いは非対称**）:
- 第1段（`classify_ext`）: 固定の拡張子マップ。一般言語＋設定ファイル系はコード側、
  csv/tsv/rtf/log 等はコード資料側。内容は読まない。**台帳・出典DL・grep/glob・ES全文**まで
  対象（検索可能集合＝引用可能集合の契約を保つため、通常の文書と同格に扱える）。
- 第2段（`sniff_content`）: 第1段で判定できない拡張子（未知拡張子・拡張子なし）だけ、
  先頭数KB（`corpus_docs._read_head` が既に読んでいるものを再利用）からバイナリ／コード／
  資料を推定する。**迷ったら資料に倒す**（ユーザー裁定）。**台帳・出典DL・glob のみ**——
  ES全文には含めない（`es_index.py` が拡張子で判別して除外する）。read_around（引用検証・
  precise read）・`verify_doc_exists`（doctype ベースの確定判定を経由する経路）が拒否する
  ことと整合させ、「ES で見つかるのに引用検証できない/精読できない」という非対称を避ける。

秘匿ファイル（`.env`/`.pem`/`.ppk`/`.key`・SSH秘密鍵 `id_rsa`系・`credentials`/`.netrc`/
`.npmrc`/`.git-credentials`）は名前/拡張子で両段とも対象外（`is_sensitive`・小文字化して判定）。
**内容による秘密鍵PEMヘッダ検知は第2段（未知拡張子の sniff）のみ**——第1段は設計上ファイル内容を
読まない（O(1)/コスト増ゼロ契約）ため、第1段拡張子（.json/.yaml 等）の中身に秘密が書かれている
場合は検知しない（`.txt` 直行の既存経路と同じ残余リスク・受容記録はバックログ参照）。

このモジュールは `corpus_docs`／`grep_tool` 等どこからでも安全に import できる葉ノードとして保つ
（`re` 以外の標準ライブラリのみ・sherpa 内の他モジュールを import しない）。
"""
from __future__ import annotations

import re

# ---- 第1段: 拡張子マップ（固定表）----------------------------------------------------------
# 一般言語＋設定ファイル系＝固定でコード側。既存の言語アナライザ登録簿（cobol/copybook/jcl・
# `sherpa.ingest.analyzers.registry`）や Office/MD 系拡張子とは重複しない（重複時は既存が
# 常に優先＝呼び出し側 `corpus_docs.classify_document` が先に確定させるため、ここには来ない）。
CODE_EXT = frozenset({
    # 一般言語（`.java` は含めない——`sherpa.ingest.analyzers.registry` に専用の `JavaAnalyzer`
    # が登録済みのため、ここに残すと本モジュール自身の docstring が謳う「既存登録簿と重複しない」
    # 契約に反する＝CODE-1d で新言語を1つ足した際に判明・以後は「登録簿に専用アナライザが
    # 増えたら、その拡張子はここから外す」運用とする）。
    ".sql", ".sh", ".bash", ".zsh", ".py", ".js", ".ts",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".vb", ".pl", ".ps1", ".psm1", ".bat", ".cmd",
    ".go", ".rb", ".php", ".kt", ".swift", ".scala", ".lua", ".r", ".awk",
    # 設定ファイル系（構造化された key=value / key: value が支配的＝コード側扱い）。
    # `.env` は要件の列挙に含まれるが `SENSITIVE_EXT`（下記）へ切り出す——両方の集合に置くと
    # `scope._CONTENT_EXT`（`CODE_EXT` を直接参照）で範囲ツリーには数えるのに実際は
    # 台帳/grep/ES から除外される、という矛盾した見え方になるため。
    ".ini", ".cfg", ".conf", ".properties", ".yaml", ".yml", ".json", ".xml", ".toml",
})

# 資料側（自然文・表データ等）。
DOCUMENT_EXT = frozenset({".csv", ".tsv", ".rtf", ".log"})

# ノイズ拡張子（一時ファイル・対象外のまま）。`.log` は含めない（トラブルシュートで価値がある・
# サイズ上限で守る＝要件どおり）。
NOISE_EXT = frozenset({".tmp", ".bak", ".swp", ".lock"})

# 秘匿ファイルの慣習的拡張子（ノイズ/一時ファイルとは別枠——意味が違うので NOISE_EXT に混ぜない）。
# `sherpa.agentic_search.verify_doc_exists()` は「`status_document_doctype()` が doctype 分類に
# 無い付帯物を返す＝ None」を、`.env`/鍵ファイル等の秘匿ファイルが実在確認・grep・ES・read_around を
# 素通りしない安全側の性質として使っている（`tests/unit/test_ext2_evidence.py::
# test_verify_doc_exists_false_for_dotenv_and_key_files` が固定する既存契約）。軽量テキスト枠は
# 「設定ファイル系はコード側」という要件を満たしつつ、この既存の安全側判定と衝突しないよう、
# 秘匿ファイルの慣習を持つ拡張子だけを対象外に据え置く（`.env` は要件の設定ファイル系リストに
# 含まれるが、実務上は秘密情報を持つ慣習が強いため例外的にここで除外する——判断に迷ったら
# 安全側に倒す・除外の是非はユーザー確認を推奨）。
# `.pem`（秘密鍵/証明書）・`.ppk`（PuTTY秘密鍵）も対象。`.env` 自体もここへ含める——
# `dev.env`/`prod.env`（"env" が本物の拡張子になる suffix 形）を拾うため。
# `scope._CONTENT_EXT` は `CODE_EXT`/`DOCUMENT_EXT` だけを見るため `SENSITIVE_EXT` を混ぜても
# 「範囲ツリーに出るのに実際は除外される」矛盾は起きない。
SENSITIVE_EXT = frozenset({".key", ".pem", ".ppk", ".env"})

# `SENSITIVE_EXT`（拡張子集合）では捕まらない秘匿ファイル慣習:
# - ドットファイル形（`.env` 単体・`.env.local`/`.env.production`）——`Path(".env").suffix` は
#   空文字（pathlib は先頭ドットを拡張子区切りと見なさない）で拡張子集合に来ない。
# - 拡張子を持たない慣習名（SSH秘密鍵 `id_rsa`/`id_rsa.pub`/`id_rsa.old` 等・AWS/gcloud等の
#   `credentials`（ini形式）・`.netrc`／`.npmrc`／`.git-credentials`）。
# ファイル名は **小文字化してから** 比較する（`.ENV`／`.Env.production` 等の大文字表記も
# バイパスさせないため）。
_SENSITIVE_NAME_EXACT = frozenset({".env", "credentials", ".netrc", ".npmrc", ".git-credentials"})
_SENSITIVE_NAME_PREFIXES = (".env.", "id_rsa")

# 一時ファイルの前綴り（例: Office のロックファイル `~$foo.docx`）。
NOISE_NAME_PREFIXES = ("~$",)

# サイズ上限＝grep 上限と同じ 8MiB（`grep_tool._GREP_FILE_CAP_BYTES` の既定値と同一の固定値・
# 本モジュールは grep_tool を import しない葉ノードのため値は独立に持つ）。超過は失敗内訳へ
# `size_exceeded`（`sherpa.ingest.failure_reasons.REASON_CATALOG` の既存語彙コード・呼び出し側が
# 参照して `state="unreadable"`/`reason="size_exceeded"` として台帳へ載せる）。
MAX_BYTES = 8 * 1024 * 1024

# 表示用 doctype（`corpus_docs._OFFICE_DOCTYPE`/`_NONCODE_DOCTYPE` と同じ固定ラベル）。
CODE_DOCTYPE_LABEL = "コード（汎用）"
DOCUMENT_DOCTYPE_LABEL = "テキスト資料"


def is_noise(name: str, ext: str) -> bool:
    """一時ファイル/ノイズか（拡張子または前綴りで判定・`.log` は対象外に含めない）。"""
    if name.startswith(NOISE_NAME_PREFIXES):
        return True
    return ext in NOISE_EXT


def is_sensitive(name: str, ext: str) -> bool:
    """秘匿ファイルの慣習を持つか（`SENSITIVE_EXT` の拡張子、または `.env`系/`id_rsa`系/
    `credentials`/`.netrc`/`.npmrc`/`.git-credentials` の名前・大文字表記も含む）。

    `agentic_search.verify_doc_exists()` の既存の安全側判定（`SENSITIVE_EXT` docstring参照）と
    衝突しないよう、軽量テキスト枠はこれらを対象外に据え置く。`ext` は呼び出し側（`corpus_docs`）が
    既に `.lower()` 済みだが、`name`（ファイル名そのもの）はここで小文字化する
    （`.ENV`/`ID_RSA` 等の大文字表記バイパスを防ぐ）。
    """
    if ext in SENSITIVE_EXT:
        return True
    name_l = name.lower()
    return name_l in _SENSITIVE_NAME_EXACT or name_l.startswith(_SENSITIVE_NAME_PREFIXES)


def classify_ext(ext: str) -> str | None:
    """第1段: 拡張子だけで判定。`"code"`／`"document"`／`None`（未知＝第2段の対象）。"""
    if ext in CODE_EXT:
        return "code"
    if ext in DOCUMENT_EXT:
        return "document"
    return None


# ---- 第2段: 内容推定（未知拡張子・拡張子なしのみ）--------------------------------------------

# 置換文字（デコード不能バイトの目印）の許容比率。これを超えたら「実質バイナリ」と見なす
# （1文字程度の孤立したノイズでは誤爆させない・閾値は経験則）。
_REPLACEMENT_RATIO_THRESHOLD = 0.02

# `key=value`/`key: value` 行が支配的なら設定ファイル的＝コード寄りと判定する閾値。
# キー側は ASCII 識別子のみに限定する: `\w` は Python の re が Unicode 既定のため日本語
# （漢字/かな）にもマッチし、「用語1: 説明」のような日本語用語集の箇条書きが `code` に
# 誤判定されてしまう。設定ファイルの key は実務上ほぼ ASCII のため、この制限で正規の
# 設定ファイル判定は損なわない。
_KV_LINE_RATIO_THRESHOLD = 0.5
_KV_LINE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*\s*[:=]\s*\S")

# 秘密鍵 PEM ヘッダ（`-----BEGIN ... PRIVATE KEY-----`）検知——ファイル名/拡張子で秘匿ファイルの
# 慣習に一致しない改名済み秘密鍵でも、内容ベースで無条件に対象外へ倒す（リネームされた秘密鍵
# ファイルが第2段 sniff を素通りしてしまう経路を塞ぐ）。
_PRIVATE_KEY_MARKER = "PRIVATE KEY-----"

# 構造記号（波括弧・丸括弧・角括弧・不等号・セミコロン）の出現比率がこれを超えたらコード寄り。
_SYMBOL_CHARS = frozenset("{}();<>[]")
_SYMBOL_RATIO_THRESHOLD = 0.02

# コメント行（行頭がコメント記号）の比率がこれを超えたらコード寄り。
_COMMENT_PREFIXES = ("//", "#", "/*", "--", "*")
_COMMENT_LINE_RATIO_THRESHOLD = 0.3

# 日本語比率がこれを超えたら自然文＝資料と判定する（CJK 統合漢字・ひらがな・カタカナ）。
_CJK_RATIO_THRESHOLD = 0.1
_CJK_RANGES = ((0x3040, 0x309F), (0x30A0, 0x30FF), (0x4E00, 0x9FFF))

# 平均行長がこれを超えたら長文の自然文＝資料寄りと判定する（コードは1行が短く改行が多い傾向）。
_AVG_LINE_LEN_DOCUMENT_THRESHOLD = 40


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def sniff_content(text: str) -> str:
    """先頭数KB（decode 済み・`errors="replace"`）からの中身推定。

    戻り値: `"binary"`（対象外・NUL/デコード不能/秘密鍵PEMヘッダが支配的）／`"code"`（シバン・
    記号密度・key=value 構造が支配的）／`"document"`（それ以外＝**迷ったら資料に倒す**・
    ユーザー裁定）。

    判定順序: 秘密鍵ヘッダ→バイナリ→シバン→**日本語比率→kv/記号/コメント密度**
    →平均行長、の順——日本語比率判定を kv/記号密度より**先**にする（実測: 日本語の自然文
    （用語集の箇条書き等）が kv/記号密度判定に先に拾われて `code` に誤判定されていた）。
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
