"""全件・一覧・影響範囲を求める依頼の判定（検索の打ち切りを回答に出すかどうかに使う）。"""
from sherpa.providers.codex.turn_finish import _is_exhaustive_request


def test_exhaustive_wording_and_impact_lens_are_detected():
    assert _is_exhaustive_request("この項目の桁数を変えた場合の影響範囲を全件挙げてください", "investigate")
    assert _is_exhaustive_request("帳票の一覧を出してください", "qa")
    assert _is_exhaustive_request("どこで使われていますか", "investigate")
    assert _is_exhaustive_request("何か", "impact")


def test_single_value_questions_are_not_exhaustive():
    assert not _is_exhaustive_request("7月の受注額はいくらですか", "investigate")
    assert not _is_exhaustive_request(None, None)
