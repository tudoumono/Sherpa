"""最終回答の先頭にある点検の前置き段落を除く守り（strip_review_preamble）。"""
from sherpa.providers.codex.structured import strip_review_preamble

_BODY = "請求書の締め処理は月末に実行されます。\n\n参照した資料:\n- a/b.md"


def test_strips_preamble_with_result_phrase():
    text = ("点検の結果、2) に当てはまるものがありました。前回回答では区分Aを省いていたため答え直します。\n\n"
            + _BODY)
    assert strip_review_preamble(text) == _BODY


def test_strips_preamble_with_ledger_added():
    text = "点検結果: 台帳に区分Bの項目を追加して再確認し、答え直しました。\n\n" + _BODY
    assert strip_review_preamble(text) == _BODY


def test_keeps_non_preamble_head():
    text = "結論: 月末に実行されます。点検の結果は問題ありません。\n\n" + _BODY
    assert strip_review_preamble(text) == text


def test_keeps_when_nothing_would_remain():
    text = "点検の結果、答え直します。"
    assert strip_review_preamble(text) == text
    assert strip_review_preamble(text + "\n\n  \n") == text + "\n\n  \n"


def test_keeps_when_paragraph_cites_file_line():
    text = "点検の結果、sample/Batch.cbl:120 の分岐を確認し答え直します。\n\n" + _BODY
    assert strip_review_preamble(text) == text


def test_keeps_when_only_references_remain():
    text = "点検の結果、答え直します。\n\n参照した資料:\n- a/b.md"
    assert strip_review_preamble(text) == text


def test_keeps_inspection_result_that_is_the_answer():
    text = "点検結果: 問題ありません。\n\n" + _BODY
    assert strip_review_preamble(text) == text
    text2 = "点検の結果、台帳に項目を追加して再確認しました。\n\n" + _BODY
    assert strip_review_preamble(text2) == text2
