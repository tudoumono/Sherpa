"""`sherpa/store/conversations.py` の純関数（DB 不要）単体テスト。

`is_personal_tainted` は `messages.personal` 列導入前の未バックフィル行でも判定できるよう、
`answer` 内の旧マーカー（personal_sources／_personal_facts／codex_wrote_files）も見る
（`shares.py::create_sanitized_snapshot` の taint 判定と改善ログエクスポートの個人情報除外が
この1関数を共有する）。
"""
from __future__ import annotations

from sherpa.store.conversations import is_personal_tainted


def test_is_personal_tainted_true_for_personal_column():
    assert is_personal_tainted({"personal": True, "answer": {}}) is True


def test_is_personal_tainted_false_when_no_marker_present():
    assert is_personal_tainted({"personal": False, "answer": {"lens": "qa"}}) is False
    assert is_personal_tainted({"personal": False, "answer": None}) is False
    assert is_personal_tainted({}) is False


def test_is_personal_tainted_true_for_legacy_personal_sources_marker():
    """personal 列が未バックフィルの旧データ（personal=False のまま）でも answer 内の
    personal_sources があれば個人情報由来と判定する。"""
    assert is_personal_tainted(
        {"personal": False, "answer": {"personal_sources": [{"doc_id": "x"}]}}) is True


def test_is_personal_tainted_true_for_legacy_personal_facts_marker():
    assert is_personal_tainted(
        {"personal": False, "answer": {"_personal_facts": "個人ファイルの抜粋"}}) is True


def test_is_personal_tainted_true_for_legacy_codex_wrote_files_marker():
    assert is_personal_tainted({"personal": False, "answer": {"codex_wrote_files": True}}) is True


def test_is_personal_tainted_false_for_empty_legacy_markers():
    """空リスト/空文字/False の旧マーカーは taint 扱いにしない（「無かった」と「あった」を区別する）。"""
    assert is_personal_tainted(
        {"personal": False, "answer": {"personal_sources": [], "_personal_facts": "",
                                       "codex_wrote_files": False}}) is False
