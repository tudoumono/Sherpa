"""軽量テキスト枠（`sherpa.ingest.text_kind`）の判定単体テスト。

ユーザー裁定 2026-09-02: 未登録拡張子のテキストファイルも台帳・出典・grep/glob・ES全文までは
通す（ベクトル・グラフ・LLM は一切通さない）。本ファイルは `text_kind` モジュール単体（純関数）
の判定を固定する——`corpus_docs`/`es_index` との結線は別ファイル（`test_corpus_docs_text_kind.py`）。
"""
from __future__ import annotations

from sherpa.ingest import text_kind


# ---- 第1段: 拡張子マップ ----

def test_classify_ext_general_languages_are_code():
    # `.java` は含めない——`JavaAnalyzer`（登録簿）に専用対応済みのため、この軽量テキスト枠には
    # もう来ない（`text_kind.CODE_EXT` から外している・CODE-1d）。
    for ext in (".sql", ".sh", ".py", ".js", ".c", ".h", ".cs", ".vb", ".pl", ".ps1", ".bat"):
        assert text_kind.classify_ext(ext) == "code", ext


def test_classify_ext_config_files_are_code():
    for ext in (".ini", ".cfg", ".conf", ".properties", ".yaml", ".yml", ".json", ".xml", ".toml"):
        assert text_kind.classify_ext(ext) == "code", ext


def test_sensitive_ext_excluded_from_code_map():
    """`.key`（秘匿ファイルの慣習的拡張子）は CODE_EXT/DOCUMENT_EXT のどちらにも無い——
    `agentic_search.verify_doc_exists()` の既存の安全側判定（doctype 分類に無い付帯物は
    文書として実在しない）と衝突しないよう、専用の `SENSITIVE_EXT` へ切り出す。"""
    for ext in text_kind.SENSITIVE_EXT:
        assert text_kind.classify_ext(ext) is None
        assert ext not in text_kind.CODE_EXT
        assert ext not in text_kind.DOCUMENT_EXT
    assert ".key" in text_kind.SENSITIVE_EXT


def test_is_sensitive_covers_key_extension_and_dotenv_names():
    """`.env` はドットファイル（`Path(".env").suffix` は空文字＝拡張子集合では捕まらない）ので
    ファイル名で判定する（`.env`/`.env.local`/`.env.production` 等）。"""
    assert text_kind.is_sensitive("config.key", ".key")
    assert text_kind.is_sensitive(".env", "")
    assert text_kind.is_sensitive(".env.local", ".local")
    assert text_kind.is_sensitive(".env.production", ".production")
    assert not text_kind.is_sensitive("app.py", ".py")
    assert not text_kind.is_sensitive(".envelope", "")   # ".env" で始まるが別名（".env." 区切りが無い）


# ---- 秘匿バイパスの回帰テスト ----

def test_h1_dotenv_suffix_form_is_sensitive():
    """`dev.env`/`prod.env`（"env" が本物の拡張子になる suffix 形）は `SENSITIVE_EXT` に
    `.env` を追加したことで捕まる（実測バイパス: 以前は `.env` が拡張子集合に無かった）。"""
    assert text_kind.is_sensitive("dev.env", ".env")
    assert text_kind.is_sensitive("prod.env", ".env")
    assert ".env" in text_kind.SENSITIVE_EXT


def test_h1_uppercase_dotenv_is_sensitive():
    """大文字表記（`.ENV`/`.Env.production`）もバイパスさせない（名前は小文字化して比較）。"""
    assert text_kind.is_sensitive(".ENV", "")
    assert text_kind.is_sensitive(".Env.production", ".production")


def test_h1_id_rsa_family_is_sensitive():
    """SSH秘密鍵 `id_rsa`（拡張子なし・`.pub`/`.old` 等の派生・大文字表記）。"""
    assert text_kind.is_sensitive("id_rsa", "")
    assert text_kind.is_sensitive("id_rsa.pub", ".pub")
    assert text_kind.is_sensitive("id_rsa.old", ".old")
    assert text_kind.is_sensitive("ID_RSA", "")


def test_h1_credentials_and_dotfiles_are_sensitive():
    """AWS/gcloud等の `credentials`（ini形式・第2段sniffで `code` 誤判定していた実測バイパス）・
    `.netrc`／`.npmrc`／`.git-credentials`。"""
    assert text_kind.is_sensitive("credentials", "")
    assert text_kind.is_sensitive(".netrc", "")
    assert text_kind.is_sensitive(".npmrc", "")
    assert text_kind.is_sensitive(".git-credentials", "")


def test_h1_pem_and_ppk_extensions_are_sensitive():
    """`server.pem`（秘密鍵/証明書）・`key.ppk`（PuTTY秘密鍵）。"""
    assert text_kind.is_sensitive("server.pem", ".pem")
    assert text_kind.is_sensitive("key.ppk", ".ppk")


def test_h1_private_key_pem_header_vetoes_content_regardless_of_name():
    """秘匿ファイルの慣習に一致しない名前（リネーム済み秘密鍵）でも、内容の PEM ヘッダを
    検知したら `sniff_content` は無条件で対象外（`"binary"`）にする。"""
    renamed_key = ("-----BEGIN RSA PRIVATE KEY-----\n"
                   "MIIEpAIBAAKCAQEA1234567890abcdefghijklmnopqrstuvwxyz\n"
                   "-----END RSA PRIVATE KEY-----\n")
    assert text_kind.sniff_content(renamed_key) == "binary"


def test_classify_ext_document_side():
    for ext in (".csv", ".tsv", ".rtf", ".log"):
        assert text_kind.classify_ext(ext) == "document", ext


def test_classify_ext_unknown_returns_none():
    assert text_kind.classify_ext(".xyz123") is None
    assert text_kind.classify_ext("") is None


def test_code_and_document_ext_do_not_overlap_registered_analyzers():
    """text_kind の拡張子集合は既存の言語アナライザ登録簿と重複しない（既存が常に優先・要件）。"""
    from sherpa.ingest.analyzers import registry
    reg = registry.registered_extensions()
    assert not (text_kind.CODE_EXT & reg)
    assert not (text_kind.DOCUMENT_EXT & reg)


def test_code_and_document_ext_are_disjoint():
    assert not (text_kind.CODE_EXT & text_kind.DOCUMENT_EXT)


# ---- ノイズ/一時ファイル ----

def test_is_noise_by_extension():
    assert text_kind.is_noise("a.tmp", ".tmp")
    assert text_kind.is_noise("a.bak", ".bak")
    assert text_kind.is_noise("a.swp", ".swp")
    assert text_kind.is_noise("a.lock", ".lock")


def test_is_noise_by_prefix():
    assert text_kind.is_noise("~$foo.docx", ".docx")


def test_log_is_not_noise():
    """`.log` は対象外にしない（トラブルシュートで価値がある・サイズ上限で守る＝要件）。"""
    assert not text_kind.is_noise("app.log", ".log")


def test_ordinary_file_is_not_noise():
    assert not text_kind.is_noise("readme", "")
    assert not text_kind.is_noise("main.py", ".py")


# ---- 第2段: 内容推定 ----

def test_sniff_content_binary_on_nul_byte():
    assert text_kind.sniff_content("abc\x00def") == "binary"


def test_sniff_content_binary_on_heavy_replacement_chars():
    text = "�" * 50 + "a" * 10
    assert text_kind.sniff_content(text) == "binary"


def test_sniff_content_shebang_is_code():
    assert text_kind.sniff_content("#!/usr/bin/env python\nprint(1)\n") == "code"


def test_sniff_content_key_value_lines_are_code():
    text = "host=localhost\nport=1234\ntimeout=30\nretries=3\n"
    assert text_kind.sniff_content(text) == "code"


def test_sniff_content_symbol_dense_is_code():
    text = "function f(a, b) {\n  if (a > b) { return a; }\n  return b;\n}\n"
    assert text_kind.sniff_content(text) == "code"


def test_sniff_content_comment_dense_is_code():
    text = "\n".join(["# comment line " + str(i) for i in range(10)])
    assert text_kind.sniff_content(text) == "code"


def test_sniff_content_japanese_prose_is_document():
    text = ("この文書は業務手順について説明しています。まず最初に申請を行い、"
            "承認を得てから作業を開始してください。詳細は別紙を参照すること。")
    assert text_kind.sniff_content(text) == "document"


def test_sniff_content_long_lines_are_document():
    text = "\n".join(["This is a reasonably long line of natural language prose without any code symbols at all"
                      for _ in range(5)])
    assert text_kind.sniff_content(text) == "document"


def test_sniff_content_empty_is_document():
    assert text_kind.sniff_content("") == "document"


def test_sniff_content_ambiguous_falls_back_to_document():
    """迷ったら資料に倒す（ユーザー裁定）。"""
    assert text_kind.sniff_content("short\nlines\nhere\n") == "document"


# ---- 判定順序・kv正規表現の回帰テスト ----

def test_sniff_content_japanese_glossary_kv_style_is_document():
    """日本語用語集の「用語N: 説明」箇条書きは `code` に誤判定しない——`_KV_LINE_RE` の `\\w` が
    Unicode既定で日本語（漢字/かな）にもマッチしていたのを ASCII 識別子限定へ制限した是正
    （実測バイパス: 以前は kv 行密度判定が先に拾い `code` へ倒れていた）。"""
    text = ("用語1: これは最初の説明です\n"
            "用語2: これは二番目の説明です\n"
            "用語3: これは三番目の説明です\n")
    assert text_kind.sniff_content(text) == "document"


def test_sniff_content_japanese_markdown_bullets_with_parens_is_document():
    """日本語の箇条書き（ASCII括弧を含む用語表記）は記号密度2%閾値で `code` に誤判定しない——
    CJK比率判定を kv/記号密度判定より**先**に評価する順序是正（実測バイパス: 以前は記号密度
    判定が先に拾い `code` へ倒れていた）。"""
    text = ("- 用語1(略語): これは最初の項目に関する説明です。\n"
            "- 用語2(表記): これは二番目の項目に関する説明です。\n"
            "- 用語3(備考): これは三番目の項目に関する説明です。\n")
    assert text_kind.sniff_content(text) == "document"


# ---- サイズ上限 ----

def test_max_bytes_matches_grep_cap_default():
    assert text_kind.MAX_BYTES == 8 * 1024 * 1024
