"""1 回の実行（attempt）の最終出力から、回答の候補・継続の要否・MCP サイドカーの取り込みを決める補助。
設計: docs/design/codex.md「1ターンの流れ」
"""
from __future__ import annotations

import json
import re

from . import usage
from .continuation import _needs_continuation
from .process import _read_last_message_fallback
from .structured import _parse_structured, _parse_structured_v2


def _continuation_msgs(st) -> list[str]:
    """`_needs_continuation` へ渡す completed message＝最新 attempt の分（`_agent_msgs[_attempt_msgs_start:]`）。その attempt が agent_message を1つも出さず（`_agent_partial` も空）に終わった場合は、それ以前の蓄積（`_agent_msgs` 全件）で判定する。"""
    latest = st._agent_msgs[st._attempt_msgs_start:]
    return latest if (latest or st._agent_partial) else st._agent_msgs


def _update_structured_state(st) -> None:
    """`_schema_on` のとき、最新 attempt の最終出力（`-o` を第一候補・無ければ最後の完了 agent_message）を `_parse_structured` で検証し、`_latest_structured` を更新する（継続要否の判定はこの1件だけを見る）。毎 attempt 終了直後に呼ぶ。`_schema_on` が偽なら何もしない。
    見出し候補（`_structured_answers`）には最新 attempt の agent_message 全件を順に検証して合格したものを積む（後続の message が壊れていても先の有効な `final` を失わないため）。最終候補（`-o` 優先）は、最後の agent_message と同一テキストでない別ソースのときだけ追加で積む。
    """
    if not st._schema_on:
        return
    _parse_fn = _parse_structured_v2 if st._schema_v2 else _parse_structured
    _latest_msgs = st._agent_msgs[st._attempt_msgs_start:]
    for _m in _latest_msgs:
        _parsed = _parse_fn(_m)
        if _parsed is not None:
            st._structured_answers.append(_parsed)
    _fb = _read_last_message_fallback(st._last_message_path, st._answer_notices, st._attempt_no)
    if _fb and _fb == st._stale_last_message:
        _fb = None  # `_absorb_last_message_fallback` と同じ staleness 規則
    _last_msg = _latest_msgs[-1] if _latest_msgs else None
    _final_text = _fb or _last_msg
    st._latest_structured = _parse_fn(_final_text) if _final_text else None
    if st._latest_structured is not None and _final_text != _last_msg:
        st._structured_answers.append(st._latest_structured)


def _continuation_pending(st) -> bool:
    """継続要否。`_schema_on` は最新 attempt の構造化出力の status で判定する（無し/不正/`in_progress` はすべて未完了）。無効時は平文ヒューリスティック。"""
    if st._schema_on:
        return st._latest_structured is None or st._latest_structured["status"] == "in_progress"
    return _needs_continuation(_continuation_msgs(st), st._agent_partial)


def _candidate_final(st) -> dict | None:
    """`_structured_answers_valid_from` 以降で最後の `final`（見出し/主張として採用しうる候補）。台帳ゲートは `_latest_structured` ではなくこの候補の有無で判定する（final の後に in_progress が出ても、その final は見出し選択の対象になり得るため）。`_pick_structured_headline`/`_pick_structured_claims` もこの関数を使い、ゲートと見出し選択が別々の final を見ないようにする。"""
    # 回答の後に届いた通知（`<subagent_notification>`）への短い返事も `final` で来る。最後の1件を鵜呑みにせず、出典の行（『参照した資料』）を持つ最後の final、次に主張（`claims`）を持つ最後の final を優先する。
    _finals = [s for s in st._structured_answers[st._structured_answers_valid_from:]
               if s.get("status") == "final"]
    for _has in (lambda s: "参照した資料" in (s.get("answer") or ""),
                 lambda s: bool(s.get("claims"))):
        for s in reversed(_finals):
            if _has(s):
                return s
    return _finals[-1] if _finals else None


_TRUNCATED_NOTICE = "回答が途中で切れた可能性があります。\n\n"
_LEDGER_UNFINISHED_NOTICE = "調査台帳の確認が終わる前の回答です（未確認の項目があります）。\n\n"


def _top_level_answer(text: str) -> str:
    """途中で切れていてもよい JSON オブジェクトから、最上位キー `answer` の文字列値だけを取り出す（入れ子の値・配列・文字列以外は対象外）。取れなければ空文字。"""
    n, i, depth = len(text), 0, 0
    while i < n:
        c = text[i]
        if c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
        elif c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            if depth == 1 and text[i + 1:j] == "answer":
                k = j + 1
                while k < n and text[k] in " \t\r\n":
                    k += 1
                if k < n and text[k] == ":":
                    k += 1
                    while k < n and text[k] in " \t\r\n":
                        k += 1
                    if k < n and text[k] == '"':
                        m = k + 1
                        while m < n and text[m] != '"':
                            m += 2 if text[m] == "\\" else 1
                        frag = text[k + 1:m]
                        for cut in (frag, frag[:-1]):  # 末尾が `\` で切れた断片は 1 文字落として復号する
                            try:
                                return json.loads(f'"{cut}"').strip()
                            except ValueError:
                                continue
                    return ""
            i = j
        i += 1
    return ""


def _salvage_body(text: str) -> str:
    """構造化として解釈できなかった出力から、利用者へ見せられる本文を取り出す。JSON らしい出力は最上位の `answer` 文字列だけ、JSON でない平文はそのまま使う。取れなければ空文字。"""
    _t = (text or "").strip()
    if not _t:
        return ""
    if _t.startswith(("{", "[")):
        return _top_level_answer(_t) if _t.startswith("{") else ""
    return _t


def _pick_structured_headline(st) -> str | None:
    """新しい final、差し戻した final、途中本文の順に回答本文を選ぶ。
    差し戻した final を返すときは、後続の途中本文を注記に残す。
    """
    _empty = "回答を取り出せませんでした。もう一度お試しください。"
    _cand = _candidate_final(st)
    if _cand is not None and _cand["answer"].strip():
        return _cand["answer"].strip()
    _valid = st._structured_answers[st._structured_answers_valid_from:]
    _rejected = [x for x in st._structured_answers[:st._structured_answers_valid_from]
                 if x.get("status") == "final" and (x.get("answer") or "").strip()]
    if _rejected and not any(s.get("status") == "final" and s["answer"].strip() for s in _valid):
        _answer = _rejected[-1]["answer"].strip()
        _followup_msgs = st._agent_msgs[st._attempt_msgs_start:]
        for _i, _m in enumerate(st._agent_msgs):
            if _salvage_body(_m) == _answer:
                _followup_msgs = st._agent_msgs[_i + 1:]
        _partial_bodies = [s["answer"].strip() for s in _valid]
        _partial_bodies.extend(_salvage_body(m) for m in [*_followup_msgs, st._agent_partial])
        _partial_bodies = list(dict.fromkeys(b for b in _partial_bodies if b and b != _answer))
        st._answer_notices.append(("ledger_unfinished", _LEDGER_UNFINISHED_NOTICE))
        if _partial_bodies:
            st._answer_notices.append(
                ("partial_followup", "続きの調査で得た途中の内容（未確定）:\n\n" + "\n\n".join(_partial_bodies)))
        return _answer
    if _valid:
        for _s in reversed(_valid):
            if _s["answer"].strip():
                return _s["answer"].strip()
    if st._agent_msgs or st._agent_partial:
        # 構造化として読めた出力が無い（壁時計の打ち切り等で JSON が途中で切れた）。最新 attempt の出力から取れる本文があれば固定文言より優先して返す。
        for _m in reversed([*st._agent_msgs[st._attempt_msgs_start:], st._agent_partial]):
            _body = _salvage_body(_m)
            if _body:
                st._answer_notices.append(("truncated", _TRUNCATED_NOTICE))
                return _body
        for _m in reversed(st._agent_msgs[:st._attempt_msgs_start]):
            _body = _salvage_body(_m)
            if _body:
                st._answer_notices.append(("truncated", _TRUNCATED_NOTICE))
                return _body
        return _empty
    return None


def _absorb_mcp_sidecar(st) -> None:
    """子（worker/evaluator）のサイドカーを毎 attempt 終了直後（自動継続の判定より前）に取り込む（待つと、子の `ask_user` があっても親が in_progress のまま自動継続を回してしまう）。読んだ doc_id は `_mcp_read_docs`/`_mcp_listed_docs` へ重複なく合流させる。
    ガードは `_sidecar_path`（決定済みか）と `_sidecar_init_ok`（このターンの初期化が無事終わったか）の2つ。sandbox 有効時は事前 unlink・設定生成が両方成功した時、非サンドボックス経路は `.tmp/` への env 配線が済んだ時に `_sidecar_init_ok` を立てる。
    非サンドボックス経路（`codex_home is None`）は model-shell が同じファイルへ偽の行を追記し得るため、読取記録（read/listed）・確認カード（ask_user）・障害コード（error）は吸収せず、数値の計数（limit）だけを取り込む。
    `_sidecar_init_ok` は、前ターンの残骸 unlink 失敗や設定生成失敗の際に、finally の無条件呼び出しが残骸や存在しないサイドカーを読むのを防ぐ。
    """
    if st._sidecar_path is None or not st._sidecar_init_ok:
        return
    _sc_reads, _sc_listed, _sc_ask, _sc_errors, _sc_limits = usage._read_mcp_sidecar(st._sidecar_path)
    if st.codex_home is not None:
        for _c in _sc_errors:
            if _c not in st._mcp_error_codes:
                st._mcp_error_codes.append(_c)
        # サイドカーが model-shell から不可視（permission profile の `:root deny` 配下）のときだけ、出典・会話の制御に効く行を信用する。
        for _d in _sc_reads:
            if _d not in st._mcp_read_docs:
                st._mcp_read_docs.append(_d)
        for _d in _sc_listed:
            if _d not in st._mcp_listed_docs and _d not in st._mcp_read_docs:
                st._mcp_listed_docs.append(_d)
        if st.codex_question is None and _sc_ask is not None and not st._ledger_review_reverted:
            st.codex_question = _sc_ask
    # `_read_mcp_sidecar` は毎回先頭から全件を読み直すため、`+=` で加算せず最新の全量スナップショットで上書きする（二重計上しない）。
    st._mcp_tool_result_clipped = _sc_limits.get("tool_result_clipped", 0)
    if _sc_limits.get("total_budget_hit"):
        st._mcp_total_budget_hit = True
    st._mcp_duplicate_tool_call = _sc_limits.get("duplicate_tool_call", 0)
    st._mcp_search_truncated = _sc_limits.get("search_truncated", 0)
    if _sc_limits.get("tool_calls_exhausted"):
        st._mcp_tool_calls_exhausted = True
    st._mcp_coverage_write_failed = _sc_limits.get("coverage_write_failed", 0)


def _pick_structured_claims(st) -> list[dict]:
    """`_pick_structured_headline`（`_candidate_final`）と同じ選び方（`final` を優先・無ければ最後の構造化 message）で、その message の `claims` を返す（v1 形・`_schema_v2` 無効時は空リスト）。台帳ゲートを通っていない final の claims も `_candidate_final` の境界で除外する。"""
    if not st._schema_v2:
        return []
    _cand = _candidate_final(st)
    if _cand is not None:
        return _cand.get("claims") or []
    _valid = st._structured_answers[st._structured_answers_valid_from:]
    if _valid:
        return _valid[-1].get("claims") or []
    return []


def _pick_structured_claims_invalid(st) -> int:
    """`_pick_structured_claims` と同じ message の `claims_invalid`（形式不正で除かれた主張の件数）。v1 形・`_schema_v2` 無効時は 0。"""
    if not st._schema_v2:
        return 0
    _cand = _candidate_final(st)
    if _cand is None:
        _valid = st._structured_answers[st._structured_answers_valid_from:]
        _cand = _valid[-1] if _valid else None
    return int((_cand or {}).get("claims_invalid") or 0)


def _pick_structured_reconciliation(st) -> tuple[list[dict], int]:
    """`_pick_structured_claims` と同じ message の照らし合わせ `(行, 形式不正で除かれた件数)`。v1 形・`_schema_v2` 無効時は `([], 0)`。"""
    if not st._schema_v2:
        return [], 0
    _cand = _candidate_final(st)
    if _cand is None:
        _valid = st._structured_answers[st._structured_answers_valid_from:]
        _cand = _valid[-1] if _valid else None
    return (_cand or {}).get("reconciliation") or [], int((_cand or {}).get("reconciliation_invalid") or 0)
