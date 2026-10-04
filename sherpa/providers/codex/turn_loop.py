"""ターンの本体の段: 初回実行・resume 失敗時の作り直し・自動継続と台帳ゲートの継続ループ・見直しの安全弁・台帳の最終判定。
設計: docs/design/codex.md「1ターンの流れ」
"""
from __future__ import annotations

from ... import investigation_ledger
from ..base import _log, _node
from .codex_attempt import _absorb_last_message_fallback, _attempt
from .codex_cli import release_codex_home, write_run_files
from .continuation import _CONTINUE_PROMPT, _CONTINUE_PROMPT_SCHEMA
from .ledger_gate import (
    _LEDGER_CONTINUE_CAP,
    _LEDGER_MANIFEST_INVALID_PROMPT,
    _LEDGER_MANIFEST_MISSING_PROMPT,
    _LEDGER_REVIEW_CAP,
    _LEDGER_REVIEW_PROMPT,
    _ledger_continue_prompt,
    _ledger_progressed,
    _ledger_review_is_worse,
    _review_continuation_note,
)
from .turn_candidates import (
    _absorb_mcp_sidecar,
    _candidate_final,
    _continuation_pending,
    _update_structured_state,
)
from ...env_int import env_int


def _resume_fallback(self, ctx, st, decision):
    """resume を試みて出力が無かった（セッション消失・出力なしの異常終了）ときだけ、履歴 priming 済みのプロンプトで新規セッションへ作り直す。"""
    uid = st.uid
    _agent_msgs = st._agent_msgs
    mcp_neighbors = st.mcp_neighbors
    _structured_answers = st._structured_answers
    _wall_clock_state = st._wall_clock_state
    _all_parent_thread_ids = st._all_parent_thread_ids
    # resume を試みて1行も --json イベントが出なかった（セッション消失等）場合、履歴 priming 済みのプロンプトで新規セッションへ即座にフォールバックする。ask_user 確認で終了した/途中停止されたターンは再試行しない。
    # 「非ゼロ終了かつ agent_message が1つも無い」場合も resume 失敗とみなす。retry は resume 試行時に1回だけ。
    _stopped = (ctx.stop_event is not None and ctx.stop_event.is_set()) or _wall_clock_state["hit"]
    _no_agent_output = not _agent_msgs and not st._agent_partial
    _resume_attempt_failed = (not st.got_any_line) or (
        st.attempt_returncode not in (0, None) and _no_agent_output)
    if st.resume_sid and _resume_attempt_failed and st.codex_question is None and not _stopped:
        _log.warning(
            "codex resume failed (no output) sid=%s conv=%s uid=%s; falling back to a fresh session",
            st.resume_sid, ctx.conversation_id, uid)
        _agent_msgs.clear()
        st._agent_partial, st._stream_error = "", False
        mcp_neighbors.clear()
        st.codex_usage, st.ran, st.codex_question, st.thread_id = None, False, None, None
        # 失敗した resume attempt の構造化状態（`_latest_structured`・`_structured_answers`）は新規セッションへ持ち越さない。
        _structured_answers.clear()
        st._structured_answers_valid_from = 0
        st._latest_structured = None
        st._resume_fallback_happened = True
        yield from _attempt(self, ctx, st, decision, False)
        _absorb_last_message_fallback(st)
        _update_structured_state(st)
        _absorb_mcp_sidecar(st)
        # 利用統計 activity: フォールバック後の新しい thread_id も控える（前段の attempt 分と合わせて2件を1つの parent エントリへ合算する）。
        if st.thread_id and st.thread_id not in _all_parent_thread_ids:
            _all_parent_thread_ids.append(st.thread_id)


def _continue_until_done(self, ctx, st, decision):
    """自動継続（途中経過で止まった）と台帳ゲート（回答が確定でも台帳が未完了）・見直しの一巡を、1 つのループで扱う。"""
    _schema_on = st._schema_on
    _schema_v2 = st._schema_v2
    _structured_answers = st._structured_answers
    _wall_clock_state = st._wall_clock_state
    # 自動継続（「途中経過で止まった」を検出）と台帳ゲート（「`final` だが台帳が未完了」を検出）は1つの while ループで扱う。毎周「今の状態がどちらの継続を必要とするか」を判定し直す。上限は別枠（`_continue_limit`＝自動継続／`_LEDGER_CONTINUE_CAP`＝台帳／`_LEDGER_REVIEW_CAP`＝見直しの一巡）。優先順位: 台帳未完了（`final` かつ未完了）＞ 見直しの一巡（台帳完了直後・上限内の1回）＞ 途中経過（`final` でない）。
    # 台帳ゲート分岐のトリガーは `_latest_structured` ではなく `_candidate_final()`（`_pick_structured_headline` と同じ採用候補の選び方）を使う（ゲートを通っていない final が受理されるのを防ぐ）。
    _continue_limit = env_int("SHERPA_CODEX_AUTO_CONTINUE", 3, 0, 5)
    _ledger_manifest_retry_used = False
    _ledger_no_progress_streak = 0
    # attempt の種類を問わず、ループの各周の先頭で `_ledger_progressed`（前周と今周の `LedgerSnapshot` の比較）を見て、進捗があれば streak をリセットする（自動継続の間に起きた進捗を見逃さないため）。
    _ledger_prev_snapshot: investigation_ledger.LedgerSnapshot | None = None
    # 見直し（`reviews.jsonl`）の件数も進捗の判定材料に加える（見直しだけでは item の状態が変わらず `_ledger_progressed` が検知できないため）。
    _ledger_prev_reviews: tuple[dict, ...] = ()
    while True:
        _stopped_for_continue = (
            (ctx.stop_event is not None and ctx.stop_event.is_set())
            or _wall_clock_state["hit"])
        if st.codex_question is not None or _stopped_for_continue:
            break
        if not (st._session_persistence_enabled and (st.thread_id or st.resume_sid)):
            break
        _ledger_gate_active = _schema_v2 and st._investigation_dir is not None
        _ledger_verdict = None
        if _ledger_gate_active:
            _ledger_snapshot = investigation_ledger.load_ledger(st._investigation_dir)
            _ledger_reviews = (investigation_ledger.load_reviews(st._investigation_dir)
                               if st._ledger_require_review else ())
            _ledger_verdict = investigation_ledger.ledger_complete(
                _ledger_snapshot, required_extra=st._ledger_required_extra,
                reviews=_ledger_reviews, require_review=st._ledger_require_review,
                require_continuation_resolved=st._ledger_require_continuation_resolved)
            if (_ledger_prev_snapshot is not None
                    and (_ledger_progressed(_ledger_prev_snapshot, _ledger_snapshot,
                                            required_extra=st._ledger_required_extra)
                         or len(_ledger_reviews) > len(_ledger_prev_reviews))):
                _ledger_no_progress_streak = 0
            _ledger_prev_snapshot = _ledger_snapshot
            _ledger_prev_reviews = _ledger_reviews
        _has_candidate_final = _candidate_final(st) is not None
        if _ledger_gate_active and _has_candidate_final:
            # ---- 台帳ゲート分岐（`_schema_v2` のときだけ効かせる）----
            if _ledger_verdict.complete:
                # ---- 見直しの一巡 ----
                # 完了と判定した回答の候補を受け取る前に、上限に達していなければ一度だけ Codex へ続きを頼む（台帳継続と同じ resume の流儀・別枠の上限 `_LEDGER_REVIEW_CAP`＝頼んだ回数で数える）。
                if st._ledger_review_requests >= _LEDGER_REVIEW_CAP:
                    break
                # 前の見直し以後が失敗・未完了・悪化のまま台帳が完了した場合は、次の見直しを頼まず、ループ後の安全弁で前の見直しの直前へ戻す。
                if (st._ledger_review_pre_candidate is not None
                        and (st._turn_failed or _continuation_pending(st)
                             or _ledger_review_is_worse(st._ledger_review_pre_candidate,
                                                        _candidate_final(st)))):
                    break
                st._ledger_review_requests += 1
                st._ledger_review_attempted = True
                st._ledger_review_pre_candidate = _candidate_final(st)
                st._ledger_review_pre_len = len(_structured_answers)
                st._ledger_review_pre_valid_from = st._structured_answers_valid_from
                st._ledger_review_pre_turn_failed = st._turn_failed
                st._ledger_review_pre_turn_failed_code = st._turn_failed_code
                st._ledger_review_pre_attempt_msgs_start = st._attempt_msgs_start
                st._ledger_review_pre_latest_structured = st._latest_structured
                st._ledger_review_pre_codex_question = st.codex_question
                _ledger_review_ids_before = (
                    set(_ledger_snapshot.manifest["items"])
                    if _ledger_snapshot.manifest is not None else set())
                yield _node(
                    f"ledger-review-{st._ledger_review_requests}", "think", "回答前の点検",
                    f"確定する前に見直します（{st._ledger_review_requests}/"
                    f"{_LEDGER_REVIEW_CAP}）", "done")
                yield from _attempt(self, ctx, st, decision, True, prompt_text=_LEDGER_REVIEW_PROMPT)
                _absorb_last_message_fallback(st)
                _update_structured_state(st)
                _absorb_mcp_sidecar(st)
                _ledger_review_snapshot_after = investigation_ledger.load_ledger(
                    st._investigation_dir)
                _ledger_review_ids_after = (
                    set(_ledger_review_snapshot_after.manifest["items"])
                    if _ledger_review_snapshot_after.manifest is not None else set())
                _ledger_review_added_ids = _ledger_review_ids_after - _ledger_review_ids_before
                if _ledger_review_added_ids:
                    # 目録が増えた＝見直した: 台帳継続へ戻り、増えた項目を調べさせてから改めて完了判定へ戻る。`_ledger_review_pre_candidate`/`_ledger_review_pre_len` はループを抜けるまで保持し、悪化していればループ後の安全弁で切り戻す。
                    st._ledger_review_rounds += 1
                    st._ledger_review_items_added += len(_ledger_review_added_ids)
                    continue
                # 目録が増えず、見直しの実行自体が失敗/未完了/確認で終わったらループを抜ける（ループ後の安全弁が見直し前へ戻す）。
                if st._turn_failed or st.codex_question is not None or _continuation_pending(st):
                    break
                # 目録は増えなかったが台帳が完了でなくなった（item を in_progress へ差し戻した等）ときは、通常の台帳ゲートへ戻す（打ち切り理由を "cap" にしない）。
                _ledger_review_verdict_after = investigation_ledger.ledger_complete(
                    _ledger_review_snapshot_after, required_extra=st._ledger_required_extra,
                    reviews=(investigation_ledger.load_reviews(st._investigation_dir)
                            if st._ledger_require_review else ()),
                    require_review=st._ledger_require_review,
                    require_continuation_resolved=st._ledger_require_continuation_resolved)
                if not _ledger_review_verdict_after.complete:
                    continue
                break
            # 台帳起因の催促（未完了／内容不正／不存在の3種）はどれも上限を超えたら発行しない。「不存在は1回だけ」は cap の内側の追加制約（cap 到達後は催促せず cap で受理する）。
            if st._ledger_continuations >= _LEDGER_CONTINUE_CAP:
                st._investigation_stopped_reason = "cap"
                break
            if _ledger_verdict.manifest_invalid:
                # 「ファイル不存在」（台帳を作らない依頼＝1回だけ催促して受理・fail-open）と「ファイルは存在するが内容が規約に合わない」（壊れた台帳＝cap まで修復を促す）を区別する。`_retire_investigation_ledger` と同じ「ファイル存在」の基準を使う。
                if (st._investigation_dir / "manifest.json").is_file():
                    _ledger_prompt_text = _LEDGER_MANIFEST_INVALID_PROMPT
                else:
                    if _ledger_manifest_retry_used:
                        st._investigation_stopped_reason = "ledger_missing"
                        break
                    _ledger_manifest_retry_used = True
                    _ledger_prompt_text = _LEDGER_MANIFEST_MISSING_PROMPT
            else:
                if _ledger_no_progress_streak >= 2:
                    st._investigation_stopped_reason = "no_progress"
                    break
                _ledger_prompt_text = _ledger_continue_prompt(_ledger_verdict)
                # この継続を「進捗なし」の1回として仮計上する（次周の先頭の進捗比較で何か終端化していれば 0 へ戻る）。
                _ledger_no_progress_streak += 1
            # この final は拒否済み＝以後の見出し/主張候補から除外する（`_pick_structured_headline`/`_pick_structured_claims` はこの境界より後だけを見る）。
            st._structured_answers_valid_from = len(_structured_answers)
            st._ledger_continuations += 1
            yield _node(
                f"ledger-continue-{st._ledger_continuations}", "think", "調査台帳を確認",
                f"未完了の調査項目があるため続けます（{st._ledger_continuations}/"
                f"{_LEDGER_CONTINUE_CAP}）", "done")
            yield from _attempt(self, ctx, st, decision, True, prompt_text=_ledger_prompt_text)
            _absorb_last_message_fallback(st)
            _update_structured_state(st)
            _absorb_mcp_sidecar(st)
            continue
        # ---- 自動継続分岐（「途中経過で止まった」を検出）----
        # 正常終了（returncode 0）で agent_message が「作業宣言だけ」（結論文が1つも無い、または構造化出力が in_progress/不正）なら、Codex セッションの続きを自動で呼ぶ。`got_any_line` を要求するのは、継続 attempt 自身が無出力で終わったときに古い作業宣言だけを根拠に空振りを繰り返さないため。判定は `_continuation_msgs()`（直前の attempt の message だけ）で行う。
        if not (st.attempt_returncode == 0 and st.got_any_line and _continuation_pending(st)):
            break
        if st._auto_continue_count >= _continue_limit:
            break
        # この if を通過＝実際に continuation attempt を1回発行する（limits 計測）。
        st._auto_continue_count += 1
        yield _node(f"cx-continue-{st._auto_continue_count}", "think", "続きを実行",
                   f"途中経過で止まったため続きを調べます（{st._auto_continue_count}/"
                   f"{_continue_limit}）", "done")
        yield from _attempt(self, ctx, st, decision,
            True, prompt_text=_CONTINUE_PROMPT_SCHEMA if _schema_on else _CONTINUE_PROMPT)
        _absorb_last_message_fallback(st)
        _update_structured_state(st)
        _absorb_mcp_sidecar(st)
        # ツールを1つも呼ばずに終わった continuation は打ち切る（同じ宣言の空振りを繰り返さない）。ただし、これがまだ `_continuation_pending()` かつ final 候補が無い場合だけ。`_candidate_final()` で final 候補の有無を確認し、候補があるならループ先頭の台帳ゲートへ戻す（台帳ゲートを通っていない final が採用されるのを防ぐ）。
        if (not st._attempt_ran_tools and _continuation_pending(st)
                and _candidate_final(st) is None):
            break


def _revert_review_if_worse(st):
    """見直しの一巡が悪化・失敗で終わったとき、見直しの直前の回答へ戻す。"""
    _structured_answers = st._structured_answers
    # 見直しの一巡の安全弁（悪化したら見直し前の回答を使う）: 最後の見直し以後（見直しの実行と、目録が増えたときの台帳継続）が失敗・未完了・確認・回答なし・悪化で終わったら、見直しの直前（台帳ゲートを通った完成回答）へ戻す。見直し前の完成回答は `_structured_answers[:_ledger_review_pre_len]` に残っているので、それより後を切り、有効範囲の起点も見直し前へ戻す。見直しで足した項目はディスクの台帳に残り、調べ終わっていなければ「確認できなかった項目」の節に出る。
    if st._ledger_review_pre_candidate is not None:
        _ledger_review_post = _candidate_final(st)
        if (_ledger_review_post is None or st._turn_failed or st.codex_question is not None
                or _continuation_pending(st)
                or _ledger_review_is_worse(st._ledger_review_pre_candidate, _ledger_review_post)):
            del _structured_answers[st._ledger_review_pre_len:]
            st._structured_answers_valid_from = min(
                st._structured_answers_valid_from, st._ledger_review_pre_valid_from)
            st._turn_failed = st._ledger_review_pre_turn_failed
            st._turn_failed_code = st._ledger_review_pre_turn_failed_code
            st._attempt_msgs_start = st._ledger_review_pre_attempt_msgs_start
            st._latest_structured = st._ledger_review_pre_latest_structured
            st.codex_question = st._ledger_review_pre_codex_question
            st._ledger_review_reverted = True


def _final_ledger_verdict(st):
    """台帳の最終判定を記録する（`env["investigation"]` と退避判定の根拠）。"""
    _schema_v2 = st._schema_v2
    # 台帳の最終判定: `_schema_v2` が有効なときだけ記録する。ゲート自体が1回も継続を発行しなくても（resume 不能・ask_user/停止で打ち切り等）、受理する回答の実状を記録する（`env["investigation"]`／退避判定の根拠）。
    if _schema_v2 and st._investigation_dir is not None:
        # 完了判定の前に、coverage.jsonl の記録に基づき `not_found_in_scope` を必要なら `unverified` へ機械的に置き換える（on-disk の item を書き換える・確認済みの状態は変えない）。以降の再読込はこの書換え後の状態を見る。
        st._investigation_snapshot = investigation_ledger.apply_unverified_downgrades(
            st._investigation_dir, investigation_ledger.load_ledger(st._investigation_dir),
            exclude_ids=st._investigation_pre_turn_not_found_ids)
        # 中間の見直し（本文を持つため台帳 snapshot とは別に読む）。末尾の「追加で調べますか？」注記と退避の判定の両方が `investigation_ledger.pending_continuation_review()` 経由で使う。
        st._investigation_reviews = (investigation_ledger.load_reviews(st._investigation_dir)
                                  if st._ledger_require_review else ())
        st._review_continuation_note_text = _review_continuation_note(st._investigation_reviews)
        st._investigation_verdict = investigation_ledger.ledger_complete(
            st._investigation_snapshot, required_extra=st._ledger_required_extra,
            reviews=st._investigation_reviews, require_review=st._ledger_require_review,
            require_continuation_resolved=st._ledger_require_continuation_resolved)
        if st._investigation_stopped_reason is None:
            if st._investigation_verdict.complete:
                st._investigation_stopped_reason = "complete"
            elif st._investigation_verdict.manifest_invalid:
                st._investigation_stopped_reason = "ledger_missing"
            else:
                # ゲート未実行（`_schema_v2` 無効・resume 不能等）で打ち切り条件のどれにも該当しないまま終わった残余ケースは、「これ以上は続けられない」として cap 側へ寄せる（4分類のみ使う）。
                st._investigation_stopped_reason = "cap"


def run_session(self, ctx, st, decision, env):
    """Codex の実行を 1 ターン分行う: ファイルの書き込み → 初回実行 → 作り直し → 継続ループ → 安全弁 → 台帳の最終判定。
    例外は握って `st.answer = None`・`st._stream_error = True` にし、サイドカーの取り込みと CODEX_HOME の後始末は必ず行う。"""
    _all_parent_thread_ids = st._all_parent_thread_ids
    codex_home = st.codex_home
    # prepare/agent（`ctx.turn_started_mono`/`_agent_start_mono`/`_agent_end_mono` から）は最初の Popen・最後の wait が確定してから finally ブロックでまとめて計算する。
    try:
        yield from write_run_files(self, ctx, st)
        yield from _attempt(self, ctx, st, decision, bool(st.resume_sid))
        _absorb_last_message_fallback(st)
        _update_structured_state(st)
        _absorb_mcp_sidecar(st)
        # この attempt が捕捉した thread_id を控える（直後の resume 失敗判定で `thread_id` が None へリセットされる前に控える）。
        if st.thread_id and st.thread_id not in _all_parent_thread_ids:
            _all_parent_thread_ids.append(st.thread_id)
        yield from _resume_fallback(self, ctx, st, decision)
        yield from _continue_until_done(self, ctx, st, decision)
        _revert_review_if_worse(st)
        _final_ledger_verdict(st)
    except Exception:
        st.answer = None
        st._stream_error = True
    finally:
        # サイドカーは codex_home の削除・後始末より前に必ず一度吸収する（`_attempt()` の途中で例外が起きた経路の取りこぼし防止・fail-open）。
        _absorb_mcp_sidecar(st)
        if codex_home is not None:
            release_codex_home(ctx, st, env)
