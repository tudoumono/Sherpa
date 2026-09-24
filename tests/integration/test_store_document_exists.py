"""`store.document_exists`（文書台帳の完全一致確認・要 Postgres）の単体テスト。

`/documents/download` の別名拒否（RV1 是正 #1/#2）が依拠する契約を、DB 直接操作で固定する:
`UNIQUE (kb_id, version, name)` に対する厳密一致のみ True（大文字小文字違いは別名として False）。
"""
from __future__ import annotations

import os

from sherpa import store

WORLD = f"doc_exists_test_{os.getpid()}"


def _cleanup():
    store.replace_documents(WORLD, [])


def test_document_exists_exact_match_only():
    _cleanup()
    try:
        assert store.document_exists(WORLD, "a/b.txt") is False   # 台帳が空＝常に False
        store.replace_documents(WORLD, [{"name": "a/b.txt"}])
        assert store.document_exists(WORLD, "a/b.txt") is True
        assert store.document_exists(WORLD, "A/b.txt") is False   # 大文字小文字違いは別名＝False
        assert store.document_exists(WORLD, "a/b.TXT") is False
        assert store.document_exists(WORLD, "a/b.txt ") is False  # 末尾空白違いも別名＝False
        assert store.document_exists("other-world", "a/b.txt") is False   # world が違えば False
    finally:
        _cleanup()


def test_document_exists_reflects_replace_documents_idempotently():
    """`replace_documents` は丸ごと入れ替え（冪等）——古い行は消え、新しい行だけが見える。"""
    _cleanup()
    try:
        store.replace_documents(WORLD, [{"name": "old.md"}])
        assert store.document_exists(WORLD, "old.md") is True
        store.replace_documents(WORLD, [{"name": "new.md"}])
        assert store.document_exists(WORLD, "old.md") is False
        assert store.document_exists(WORLD, "new.md") is True
    finally:
        _cleanup()
