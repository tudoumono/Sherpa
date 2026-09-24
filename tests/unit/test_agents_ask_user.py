"""S2（ask_user-improvements.md）: Codex の ask_user（MCP ツール）→ question 化 & 乱用ガードの単体テスト。

Codex サブプロセスは起動しない。①question 化ヘルパ `_codex_ask_question`／ガード②の判定ヘルパ
`_codex_ask_capture` を**実行ベース**で直接検証し（RV Low-3・2026-07-07）、②ラッパー
（CodexProvider._run_authoring）本体は Popen 必須のため A2 テスト（test_agents_a2.py）や
test_codex_created_files.py と同じ「ソース検査」方式で固定する（subprocess モックはこの
コードベースに既存慣行が無いため無理に導入しない）。③プロンプト/AGENTS.md に使用条件・
確認ID ガード・author 親和の文言があること。
"""
from __future__ import annotations

import inspect
import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
from sherpa import agents as A  # noqa: E402


def _ask_item(**args):
    return {"tool": "ask_user", "arguments": args}


# ---- ① question 化ヘルパ ----

def test_codex_ask_question_rounds_ask_user_item():
    q = A._codex_ask_question(_ask_item(
        mode="single", prompt="対象範囲は？",
        options=[{"id": "a", "label": "A案"}, {"id": "b", "label": "B案"}],
        allow_free_text=True))
    assert q["type"] == "question" and q["mode"] == "single"
    assert q["prompt"] == "対象範囲は？" and q["allow_free_text"] is True
    assert [o["label"] for o in q["options"]] == ["A案", "B案"]
    assert q["interaction_id"]                                   # フロントの回答再送/回答済み判定に必須
    assert "original_message" not in q                           # 元の依頼は chat_service が setdefault で補完


def test_codex_ask_question_none_for_non_ask_user():
    assert A._codex_ask_question({"tool": "graph_neighbors", "arguments": {"name": "x"}}) is None
    assert A._codex_ask_question("nope") is None
    assert A._codex_ask_question({}) is None


def test_codex_ask_question_defaults_when_args_missing():
    # 非 dict 引数でも落とさず既定質問（はい/いいえ）へ丸める（_question_from_args の堅牢性を継承）。
    q = A._codex_ask_question({"tool": "ask_user", "arguments": None})
    assert q["type"] == "question" and len(q["options"]) >= 2


# ---- ①' ガード②の判定ヘルパ（実行ベース・RV Low-3） ----

def test_codex_ask_capture_returns_question_when_enabled():
    item = _ask_item(mode="single", prompt="対象範囲は？", options=[{"label": "A"}, {"label": "B"}])
    q = A._codex_ask_capture(item, ask_disabled=False)
    assert q is not None and q["type"] == "question" and q["prompt"] == "対象範囲は？"


def test_codex_ask_capture_returns_none_when_disabled():
    """ガード②: 確認ID 付き再送（ask_disabled=True）では ask_user を無視する（None）。"""
    item = _ask_item(mode="single", prompt="対象範囲は？", options=[{"label": "A"}, {"label": "B"}])
    assert A._codex_ask_capture(item, ask_disabled=True) is None


# ---- ② ラッパー（_run_authoring）の配線（ソース検査・Popen 必須のため） ----

def _src():
    return inspect.getsource(A.CodexProvider._run_authoring)


def test_ask_disabled_flag_computed_and_wired_to_capture():
    """`_ask_disabled` は ctx.message の『確認ID:』marker から立て、
    `_codex_ask_capture` の呼び出しに渡すこと（判定自体は実行ベースの上のテストで固定済み）。"""
    src = _src()
    assert "_ask_disabled" in src and "確認ID" in src
    assert "_codex_ask_capture(item, _ask_disabled)" in src, \
        "ask_user 捕捉が _codex_ask_capture（確認ID ガード）を通っていない"


def test_ask_user_captured_then_break():
    """ガード③: 1実行1回。ask_user を捕まえたらループを抜ける（proc は finally で後始末）。"""
    src = _src()
    assert 'if tool == "ask_user":' in src
    i_cap = src.index("codex_question = _codex_ask_capture(item, _ask_disabled)")
    i_break = src.index("break", i_cap)
    assert i_break > i_cap, "ask_user 捕捉後に loop を抜けていない"


def test_question_emitted_before_result_and_file_registration():
    """S2: question 優先＝出たターンは env/_result・成果物台帳登録を出さずに return する
    （agentic の {"question":..}→return と同じ意味論）。

    `_run_authoring` は honest failure の早期 return（MCP 無効＋層限定等）でも `_result` を
    yield するため、関数内に複数の `_result` yield 箇所が存在しうる——`i_result` は
    `created_files` 構築（この検証が対象とする「通常完了時の最終応答」の直前）より**後**に
    現れる箇所を探す（先頭一致だと無関係な早期 return の `_result` を誤って拾う）。
    """
    src = _src()
    assert "if codex_question is not None:" in src
    i_emit = src.index("yield codex_question")
    i_return = src.index("return", i_emit)
    i_created = src.index('env["created_files"] = [')      # 代入の実箇所（型注釈コメントと区別・末尾 = [ 必須）
    i_result = src.index('yield {"type": "_result"', i_created)
    assert i_emit < i_return < i_created, \
        "question emit/return が created_files 構築より後（成果物を台帳登録してしまう）"
    assert i_return < i_result, "question emit/return が _result より後（env を出してしまう）"


def test_ask_user_node_forced_done_when_captured():
    """RV Low-2（2026-07-07）: 捕捉して break する場合は item.completed を待たずにループを抜けるため、
    実際の done フラグに関わらず「ユーザに確認」ノードを "done" で確定表示すること
    （さもないと履歴に active のまま残る）。"""
    src = _src()
    i_node = src.index('_node(f"cx-{iid}", "tool", "ユーザに確認",')
    i_status = src.index('"done" if node_done else "active"', i_node)
    assert i_status > i_node
    i_calc = src.index("node_done = done or (codex_question is not None)")
    assert i_calc < i_node, "node_done の算出が yield より前に無い"


def test_parent_codex_node_closed_before_question_return():
    """RV Low-2: 親ノード（id='codex'・冒頭で active のまま止まる）も question return 前に
    'done' で閉じること。`if codex_question is not None:` は break 用（loop 内）と早期 return 用
    （loop 後）の2箇所あるため、**後者**（rindex）を early-return ブロックとして特定する。"""
    src = _src()
    i_ret_block = src.rindex("if codex_question is not None:")
    i_emit = src.index("yield codex_question", i_ret_block)
    i_close = src.index('_node("codex", "think", "Codex が調べる"', i_ret_block)
    assert i_ret_block < i_close < i_emit, "親ノード 'codex' の done 確定が question emit より前に無い"


def test_last_message_unlinked_before_question_early_return():
    """RV Low-1（2026-07-07）: question の早期 return が `-o` 一時ファイル（last-message-*.txt）の
    削除をバイパスして .tmp/ に蓄積するのを防ぐ＝早期 return 前に best-effort unlink すること。"""
    src = _src()
    i_ret_block = src.rindex("if codex_question is not None:")
    i_emit = src.index("yield codex_question", i_ret_block)
    i_unlink = src.index("_last_message_path.unlink(missing_ok=True)", i_ret_block)
    assert i_ret_block < i_unlink < i_emit, \
        "question early return 前に last-message unlink が無い"


# ---- ③ プロンプト/AGENTS.md の使用条件・確認ID ガード・author 親和 ----

def test_prompt_mcp_carries_ask_user_conditions_and_confirm_id_guard():
    p = A.CodexProvider()
    mcp_prompt = p._prompt_mcp("案件一覧を Excel にまとめて", "author", "v1")
    assert "ask_user" in mcp_prompt and "確認ID" in mcp_prompt        # 使用条件＋確認ID ガード（多層防御）
    assert "着手前" in mcp_prompt                                     # author 親和（曖昧なら着手前に確認）


def test_agents_md_carries_ask_user_conditions():
    from sherpa import codex_agents_md
    assert "ask_user" in codex_agents_md.AGENTS_MD and "確認ID" in codex_agents_md.AGENTS_MD
