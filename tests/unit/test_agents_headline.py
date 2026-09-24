"""F4（2026-07-07-フィードバック一括.md）: 回答 headline の選び方の単体テスト。

Codex は調査中に「これから〜する」という進行形の作業宣言を agent_message として複数回出すことがあり、
run が timeout/stop で途中終了すると **最後に届いた作業宣言**（実例:「…根拠の有無を切り分けます」）が
env["headline"] になってしまう（結論でなく本文途中の一文が見出しに出る）。
`_pick_codex_headline`/`_is_progress_only`/`_trim_trailing_progress` は LLM を使わず決定的に、
結論を含む message を優先して選ぶ。実利用の実例文をテストデータに使う。
"""
from __future__ import annotations

import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
from sherpa import agents as A  # noqa: E402


# ---- _is_progress_only（作業宣言だけの message か） ----

def test_progress_only_true_for_work_declarations():
    # 実例: 進行形の作業宣言だけ（結論なし）。
    assert A._is_progress_only("税率の定義箇所を特定します。夜間バッチとの接続を確認します。"
                               "根拠の有無を切り分けます。")
    assert A._is_progress_only("まず関連ファイルを探します")


def test_progress_only_false_when_conclusion_present():
    # 所見・結論の文（「参照されています」「波及します」）が1つでもあれば作業宣言だけではない。
    assert not A._is_progress_only(
        "税率テーブルは夜間バッチ NIGHTLY から参照されています。したがって税率変更は夜間バッチに波及します。")
    assert not A._is_progress_only("影響はありません。")


# ---- High-1（RV）: 単文の事実記述を作業宣言と誤判定しない（判定を絞る） ----

def test_progress_only_false_for_single_factual_sentence_ending_in_verb():
    # RV シナリオ: 語尾がたまたま「確認します」でも、単文・マーカー無しの事実記述は progress としない
    # （＝新しい方の message を残すのを安全側とする）。
    assert not A._is_progress_only("夜間バッチ NIGHTLY は税率マスタを起動時に確認します。")


def test_progress_only_true_when_marker_present_single_sentence():
    # 実利用の実例: 手順マーカー（最後に…）で始まる作業宣言は単文でも progress。
    assert A._is_progress_only(
        "最後に『停止・落ちる・エラー』と設定変更系の語で確認し、根拠の有無を切り分けます。")


def test_progress_only_true_when_multiple_work_sentences():
    # 実利用の実例: 複数文がすべて作業宣言なら progress（マーカー無しでも文が2つ以上）。
    assert A._is_progress_only("税率の定義箇所を特定します。夜間バッチとの接続を確認します。")


# ---- _trim_trailing_progress（単一段落の末尾作業宣言を落とす） ----

def test_trim_trailing_progress_drops_only_trailing_work_sentences():
    txt = "税率テーブルは夜間バッチから参照されており、税率変更は波及します。次に具体的な項目を確認します。"
    out = A._trim_trailing_progress(txt)
    assert "波及します" in out and "確認します" not in out


def test_trim_trailing_progress_keeps_markdown_untouched():
    # 改行/箇条書きを含む場合は Markdown 構造を壊さないためそのまま返す（②の安全側）。
    md = "- 起点: 税率\n- 影響先: 夜間バッチ\n次に詳細を確認します。"
    assert A._trim_trailing_progress(md) == md


def test_trim_trailing_progress_keeps_pure_conclusion():
    txt = "税率変更は夜間バッチに波及します。"
    assert A._trim_trailing_progress(txt) == txt


# ---- _pick_codex_headline（複数 agent_message からの選択） ----

def test_pick_prefers_conclusion_message_over_trailing_work_declaration():
    """実例: run が途中終了し、最後に届いたのが作業宣言。結論を含む前の message を選ぶ。

    High-1（RV）で判定を絞ったため、末尾の作業宣言は「単文・マーカー無し」だと progress 扱いされない
    （新しい方を残す安全側）。ここでは実利用に即した**複数文の作業宣言**を末尾に置く（progress 扱い）。
    """
    msgs = [
        "税率テーブル(TAX_RATE)は夜間バッチ NIGHTLY.jcl から呼ばれる BATCH01 が参照しています。"
        "したがって税率変更は夜間バッチに波及します。",
        "念のため他の経路も洗い出します。根拠の有無を切り分けます。",   # 末尾の作業宣言（複数文＝progress）
    ]
    head = A._pick_codex_headline(msgs)
    assert "波及します" in head
    assert "根拠の有無を切り分けます" not in head


def test_pick_uses_partial_when_it_has_conclusion():
    # item.completed が来なかった未完 message（timeout 保険）も候補にする。
    head = A._pick_codex_headline([], partial="税率変更は夜間バッチに影響します")
    assert head == "税率変更は夜間バッチに影響します"


def test_pick_falls_back_to_last_when_all_progress():
    """③どの message も作業宣言だけなら最後の message をそのまま（本文先頭＝best effort）。"""
    msgs = ["まず定義箇所を探します。", "次に根拠の有無を切り分けます。"]
    head = A._pick_codex_headline(msgs)
    assert head == "次に根拠の有無を切り分けます。"


def test_pick_empty_returns_empty_string():
    # 呼び出し側は "" を None に丸めて -o フォールバック→決定的回答へ委ねる。
    assert A._pick_codex_headline([], "") == ""
    assert A._pick_codex_headline(["", "   "]) == ""


def test_pick_single_conclusion_unchanged_common_case():
    # 通常ケース（単一の最終 agent_message＝結論）は素通し（回帰防止）。
    msgs = ["該当箇所が3件見つかりました。仕様上、税率は BATCH01 で参照されます。"]
    assert A._pick_codex_headline(msgs) == msgs[0]


def test_codex_headline_wired_in_run_authoring():
    """CodexProvider._run_authoring が最後の1件を鵜呑みにせず `_pick_codex_headline` で選ぶこと
    （Popen 必須のため既存慣行どおりソース検査）。"""
    import inspect
    src = inspect.getsource(A.CodexProvider._run_authoring)
    assert "_agent_msgs" in src and "_pick_codex_headline(_agent_msgs" in src
    # 旧「最後の1件で上書き」（answer = item.get(...)）に戻っていないこと。
    assert 'answer = (item.get("text") or "").strip()' not in src


def test_stream_error_prefers_o_fallback_before_pick():
    """Med-2（RV・2026-07-07）: stream 読取が途中例外で終わった場合は、集めた作業宣言に引きずられず
    完全版が入り得る `-o` 最終メッセージ読取を pick より**先に**試す（Popen 必須のためソース検査）。"""
    import inspect
    src = inspect.getsource(A.CodexProvider._run_authoring)
    assert "_stream_error = True" in src                       # except で例外フラグを立てる
    assert "if _stream_error:" in src                          # 例外有無で分岐する
    # 例外時は -o を先に（read→pick）、正常時は pick を先に（pick→read）。
    assert "_read_last_message_fallback(_last_message_path) or _picked" in src
    assert "_picked or _read_last_message_fallback(_last_message_path)" in src
