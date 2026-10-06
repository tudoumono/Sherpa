"""ターンの終わりの段（作成ファイルの登録・回答の組み立て）。
設計: docs/design/codex.md「1ターンの流れ」
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from ... import agentic_search, investigation_ledger, workspace_limits
from ...investigation_record_render import describe_dropped
from ...store.investigation_records import trim_record
from ... import layer as layer_mod
from ...answer_shape import STOPPED_EARLY_NOTICE, STOPPED_NOTICE, add_notice, seal, set_body
from ..base import _evidence_gate_note, _log, _node, _verified_sources
from .citations import parse_referenced_doc_lines, resolve_referenced_docs
from .codex_cli import apply_codex_usage, log_turn_end
from .ledger_gate import _format_unconfirmed_items_section, _retire_investigation_ledger, _unconfirmed_items_list
from .mcp import _apply_codex_neighbors
from .continuation import _pick_codex_headline
from .process import _CONTEXT_WINDOW_EXCEEDED_CODE, _masked_run_dir_path, _read_last_message_fallback
from .sandbox import _detect_chrome_path, _marp_bin
from .structured import _DEMOTED_CLAIMS_NOTE, _INVALID_CLAIMS_NOTE, split_review_preamble, _apply_codex_evidence_gate, _claims_vs_ledger, verify_reconciliation
from .turn_candidates import _continuation_pending, _pick_structured_claims, _pick_structured_claims_invalid, _pick_structured_headline, _pick_structured_reconciliation
from .turn_consts import (_CREATED_FILES_FAILURE_NOTE, _MARP_FAILURE_NOTE, _MCP_SIDECAR_NAME, _SKILLS_BASE,
                          _WALL_CLOCK_LIMIT_NOTE)


# 「確認できなかった資料」に載せる件数の上限（超えた分は件数だけ）。
_SOURCES_UNVERIFIED_MAX = 20


# 「使った検索」に数える道具（原本の読み取りと一覧は数えない）。
_SEARCH_TOOL_NAMES = frozenset({"ripgrep_search", "es_search", "glob_search", "graph_neighbors",
                                "graph_resolve", "graph_impact"})


def _count_searches(activity) -> int:
    """利用統計の活動記録（親と子の全エージェント）から、検索の道具の呼出し回数を数える。取れなければ 0。"""
    total = 0
    for agent in (activity or {}).get("agents") or [] if isinstance(activity, dict) else []:
        tools = agent.get("tools") if isinstance(agent, dict) else None
        for name, stats in (tools.items() if isinstance(tools, dict) else []):
            calls = stats.get("calls") if isinstance(stats, dict) else None
            if (isinstance(name, str) and name.split("__")[-1] in _SEARCH_TOOL_NAMES
                    and isinstance(calls, int) and not isinstance(calls, bool)):
                total += calls
    return total


def _new_run_files(run_dir, before) -> list:
    """作業領域（Codex の cwd）に、実行前の一覧 `before` に無い新規ファイルがあれば昇順で返す。`.tmp`・`.agents`・AGENTS.md・サイドカーは対象外（`before` を作る側と対）。"""
    if not run_dir.is_dir():
        return []
    after = {
        p for p in run_dir.rglob("*")
        if p.is_file() and not p.is_symlink()
        and p.relative_to(run_dir) not in (Path("AGENTS.md"), Path(_MCP_SIDECAR_NAME))
        and not ({".tmp", ".agents"} & set(p.relative_to(run_dir).parts))
    }
    return sorted(after - before)


def close_session(self, ctx, st, decision, env):
    """後始末の後の段: 終了ログ・「確認で終了」・調査台帳の記録・回答の選択・失敗の印・新規ファイルの検出。
    ユーザへの確認で終えるターンは確認を出して True を返す（呼び出し側はそこで終了する）。"""
    run_dir = st.run_dir
    _schema_on = st._schema_on
    _last_message_path = st._last_message_path
    _agent_msgs = st._agent_msgs
    _before_ws_files = st._before_ws_files
    codex_created_files = st.codex_created_files
    _wall_clock_state = st._wall_clock_state
    log_turn_end(ctx, st, env)
    # ask_user が出たターンは question 優先＝env/_result・成果物台帳登録を出さずここで終了する（回答は chat.js の整形再送＝新 codex exec で拾う）。proc は直上の finally で後始末済み。chat_service はこの question を answer.question として保存する。
    codex_question = st.codex_question
    if codex_question is not None and not (ctx.stop_event is not None and ctx.stop_event.is_set()):
        # 親ノード（"Codex が調べる"）も "active" のまま止まっているため、通常経路の完了 yield と同様にここで "done" に確定させる。
        yield _node("codex", "think", "Codex が調べる", "ユーザに確認するため終了しました", "done")
        # `-o` 一時ファイル（last-message-*.txt）の削除を通常経路と同じ best-effort で先に消す（早期 return で .tmp/ に蓄積しないように）。
        try:
            _last_message_path.unlink(missing_ok=True)
        except Exception:
            pass
        # 利用統計 activity: この経路は env/_result を出さないため、finally で確定済みの `env["activity"]` は question イベント経由で運ぶ（運ばないと chat_service の確認カード保存側が source:"none" で作り直す）。
        if env.get("activity") is not None:
            codex_question["activity"] = env["activity"]
        # 確認で終えるターンに作られたファイルは保存しない（作らせない方針）。捨てた件数は確認カードに添える。
        try:
            _discarded_for_question = len(_new_run_files(run_dir, _before_ws_files))
        except OSError:
            _discarded_for_question = 0
        if _discarded_for_question:
            codex_question["discarded_files"] = _discarded_for_question
        yield codex_question
        return True
    # 調査台帳（回答 envelope への記録）: 本文・path は含めず、id 一覧は先頭50件に打ち切る。ゲートが1度も走らなかった（`_investigation_dir` が未確定＝早期 return 済み）ターンには載せない。
    if st._investigation_verdict is not None:
        # 退避処理をここで先に実行して成否を確定し、`retained` に実際の値を書く（予測値ではない）。外側 finally はこの turn では二重に実行しない（`_investigation_retire_done` を見る・切断/例外時のフォールバックのみ）。復元が途中で失敗したターン（`_investigation_retire_done` は復元失敗時点で立っている）は退避を行わず元の退避台帳を保持する。
        _investigation_retained = False
        if st._ledger_home is not None and not st._investigation_retire_done:
            # 退避の判断は `_retire_investigation_ledger` 内部で `pending_continuation_review()` を直接使う（単一の純関数に一本化）。
            _investigation_retained = _retire_investigation_ledger(
                st._investigation_dir, st._ledger_home, required_extra=st._ledger_required_extra,
                require_review=st._ledger_require_review)
            st._investigation_retire_done = True
        _ledger_id_report_limit = 50
        env["investigation"] = {
            "complete": st._investigation_verdict.complete,
            "manifest_invalid": st._investigation_verdict.manifest_invalid,
            "counts": dict(st._investigation_verdict.terminal_counts),
            "non_terminal": list(st._investigation_verdict.non_terminal_ids[:_ledger_id_report_limit]),
            "invalid": list(st._investigation_verdict.invalid_ids[:_ledger_id_report_limit]),
            "invalid_total": len(st._investigation_verdict.invalid_ids),
            "missing": list(st._investigation_verdict.missing_ids[:_ledger_id_report_limit]),
            "continuations": st._ledger_continuations,
            "stopped_reason": st._investigation_stopped_reason,
            "retained": _investigation_retained,
            "restored": st._investigation_restored,
            # 見直しの一巡の有無・回数・足した項目数（本文なし・件数だけ）。
            "review": {
                "attempted": st._ledger_review_attempted,
                "rounds": st._ledger_review_rounds,
                "items_added": st._ledger_review_items_added,
            },
            # 中間の見直し（本体別枠・本文なし・件数と未充足 id だけ）。
            "mid_review": {
                "count": len(st._investigation_reviews),
                "missing": st._investigation_verdict.review_missing,
                "pending": list(st._investigation_verdict.review_pending_ids[:_ledger_id_report_limit]),
            },
        }
        env["limits"] = {**(env.get("limits") or {}),
                         "ledger_incomplete": not st._investigation_verdict.complete}
        # この時点の台帳の正規形を env とは別項目に積む。本文・path は `_investigation_snapshot` 側の検証（`validate_item`）で除外済み。`load_coverage` は item ごとの outcome タプルのみ（本文を持たない）。`reviews`（本文を持つ・`validate_review_entry()` 済みの正規形のみ）も調査の記録の一部として積む（`investigation_record_render.py` の素材）。
        # 保存の大きさの上限で落とすものがあれば、ここで先に切り詰めて内訳を記録に残し、回答の注記にも出す。
        _reviews_report = investigation_ledger.load_reviews_report(st._investigation_dir)[1]
        (_rec_manifest, _rec_items, _rec_coverage, _rec_reviews, _rec_cov_detail,
         _rec_dropped) = trim_record(
            st._investigation_snapshot.manifest, dict(st._investigation_snapshot.items),
            {k: list(v) for k, v in investigation_ledger.load_coverage(st._investigation_dir).items()},
            list(st._investigation_reviews),
            investigation_ledger.load_coverage_detail(st._investigation_dir))
        _extras: dict = {}
        if _rec_dropped:
            _extras["dropped"] = _rec_dropped
            st._record_notes.append(describe_dropped(_rec_dropped))
        if _reviews_report["invalid"] or _reviews_report["over_count"] or _reviews_report["over_bytes"]:
            _extras["reviews_report"] = _reviews_report
            env["investigation"]["reviews_report"] = _reviews_report
        st._investigation_record_payload = {
            "complete": st._investigation_verdict.complete,
            "manifest": _rec_manifest,
            "items": _rec_items,
            "coverage": _rec_coverage,
            "reviews": _rec_reviews,
            "coverage_detail": _rec_cov_detail,
            "extras": _extras,
        }
    # `_schema_on` は構造化 message から見出しを選ぶ（生 JSON をそのまま出さない・平文ヒューリスティックへは戻さない）。無効時は現行どおりの選び方（下記）。
    if _schema_on:
        st.answer = _pick_structured_headline(st)
    else:
        # 集めた agent_message から結論を優先して headline を選ぶ（進行中の作業宣言を見出しにしない・最後の1件を鵜呑みにしない）。
        _picked = _pick_codex_headline(_agent_msgs, st._agent_partial,
                                       prefer_marker="参照した資料", dropped=st._trimmed) or None
        # `-o` は保険。--json の agent_message から拾えなかった時だけ最終メッセージファイルを読む。使い終わったら必ず削除する（.tmp/ に溜め続けないため）。途中例外時は完全版が入り得る `-o` を先に試し、空/無いときだけ pick に委ねる。正常終了時は pick が主・`-o` は従。
        _stream_error = st._stream_error
        if _stream_error:
            st.answer = _read_last_message_fallback(_last_message_path, st._answer_notices, st._attempt_no) or _picked
        else:
            st.answer = _picked or _read_last_message_fallback(_last_message_path, st._answer_notices, st._attempt_no)
    if st.answer:
        st.answer, _preamble = split_review_preamble(st.answer)
        if _preamble:
            st._trimmed.append({"kind": "review_preamble", "text": _preamble})
    try:
        _last_message_path.unlink(missing_ok=True)
    except Exception:
        pass
    # codex exec を実際に起動した（attempt_returncode is not None）のに stdout に JSON を1行も出さず（got_any_line=False）、answer も得られない場合だけ「正直に伝える」文言に切り替える。ユーザーの stop_event・壁時計上限（`_wall_clock_state`）による打ち切りは失敗ではないため対象外。
    _stopped_final = (ctx.stop_event is not None and ctx.stop_event.is_set()) or _wall_clock_state["hit"]
    if st.answer and (st._turn_failed or st.attempt_returncode not in (None, 0)) and not _stopped_final:
        st._answer_notices.append("後続の調査または回答の処理が失敗しました。回収済みの回答を残しています。")
    # `turn.failed`／`error` で閉じた attempt は、JSON が読めていても（got_any_line=True）agent_message が無いままの失敗のため、`_turn_failed` を OR で加える。
    if (not st.answer and (not st.got_any_line or st._turn_failed) and st.attempt_returncode is not None
            and not _stopped_final):
        st._codex_silent_failure = True
    # 自動継続を尽くしてもなお（上限0・セッション非永続・継続 attempt が無出力/異常終了を含む）作業宣言だけなら、本文（headline）は書き換えず印だけ立てる（「途中までの結果」と伝えて続きを促す）。利用者の明示停止は途中結果として扱わない。
    st._codex_stopped_early = (
        _continuation_pending(st)
        and not _stopped_final and not st._turn_failed)
    # run_dir の新規ファイルを検出して台帳登録する（personal_workspace_files に登録・ES/Neo4j には一切書かない）。Codex の cwd = run_dir のため、個人アップロード（files/）は読み取り・書き込み不可。
    if run_dir.is_dir():
        # `.tmp`（TMPDIR）配下・`.agents`（配備したスキル）配下・ルート直下の AGENTS.md・`.mcp_sidecar.jsonl` は台帳登録しない（before 側と対）。
        new_authoring = _new_run_files(run_dir, _before_ws_files)
        if decision["lens"] != "author" and new_authoring:
            # 作成の依頼（画面の「資料を作成」／依頼文が作成と判定）以外では成果物にしない。登録せず、run_dir の削除で一緒に消す。
            _log.info("codex created %d file(s) in a non-authoring turn; discarded",
                      len(new_authoring))
            st._discarded_files = len(new_authoring)
            new_authoring = []
        for fp in new_authoring:
            codex_created_files.append(str(fp))
        # Marp レンダは sandbox の外＝Sherpa 本体が network 隔離（unshare）下で実行する（Codex は .md を書くだけ）。ベストエフォート（fail-open）: 失敗しても .md 自体は台帳登録対象に入っている。
        try:
            from ... import marp_render
            _mds = [p for p in new_authoring if p.suffix == ".md"]
            _marp_failed_formats: list = []
            _rendered = marp_render.render_outputs(
                [p for p in _mds if marp_render.is_marp_markdown(p)],
                marp_bin=_marp_bin(), chrome_path=_detect_chrome_path(),
                theme_dirs=[run_dir / ".agents" / "skills" / "marp" / "themes",
                            _SKILLS_BASE / "marp" / "themes"],
                containment_root=run_dir, failures=_marp_failed_formats)  # 入出力を run_dir 内実体に強制
            codex_created_files.extend(str(p) for p in _rendered)
            st._marp_failed_formats = sorted(set(_marp_failed_formats))
            # marp の原稿があるのに 1 つも書き出せなかった（marp 未導入・全形式の失敗）ときは注記する。
            if any(marp_render.is_marp_markdown(p) for p in _mds) and not _rendered:
                st._marp_failed = True
        except Exception as e:
            st._marp_failed = True
            _log.warning("marp_render: レンダ処理が例外で終了（fail-open）: %s", e)
    return False


def register_created_files(st):
    """作成されたファイルを `files/` へ移して台帳に登録する（失敗は `st._created_files_failed` に印を残して続行）。"""
    uid, run_dir, ws_files = st.uid, st.run_dir, st.ws_files
    codex_created_files = st.codex_created_files
    _created_file_rows = st._created_file_rows
    try:
        from ... import store as _store
        import datetime as _dt
        import shutil as _shutil
        _ttl_days = workspace_limits.ttl_days()
        _expires = (
            _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=_ttl_days)
            if _ttl_days > 0 else None
        )
        # ws_files が有効（非 symlink）なら files/ に移動して登録する。symlink の場合は登録スキップ（fail-closed）。files/ 移動時に同名ファイルが存在する場合は別名化する（上書き禁止）。
        _dest_dir = ws_files if (ws_files is not None and ws_files.is_dir()) else None
        if _dest_dir is None:
            # symlink or files/ が使えない → fail-closed（登録なし・grep/delete 対象外）。成果物は run_dir に残ったままなので、保存失敗として run_dir を消さず残し、回答へ注記する。
            st._created_files_failed = True
            _log.warning("codex created files could not be registered: files/ unavailable "
                        "(run_dir=%s)", run_dir.name)
        else:
            for _fp in codex_created_files:
                try:
                    _p = Path(_fp)
                    if not _p.is_file():
                        continue
                    _stem, _suf = _p.stem, _p.suffix
                    # 同名回避の名前確定も lock 内で行う（並行 HTTP upload と衝突して live ファイルを move で上書きするのを防ぐ）。候補名ごとに lock を取り、「物理未存在かつ生きた台帳なし」を確認できた名前にだけ move+登録する。
                    _i = 0
                    while _i <= 10000:  # 無限ループ防止
                        _rel = _p.name if _i == 0 else f"{_stem}_{_i}{_suf}"
                        _dst = _dest_dir / _rel
                        with _store.workspace_file_lock(uid, _rel):
                            if _dst.exists() or not _store.no_live_upload_for_path(uid, _rel):
                                _i += 1
                                continue  # この名前は埋まっている → 次 suffix へ
                            _shutil.move(str(_p), str(_dst))
                            try:
                                _data = _dst.read_bytes()
                                _sha = hashlib.sha256(_data).hexdigest()
                                _row = _store.record_workspace_file(
                                    uid, _rel, str(_dst), len(_data), _sha, expires_at=_expires)
                                _created_file_rows.append(_row)  # created_files カード用
                            except Exception:
                                # move は成功したが台帳登録に失敗した場合は、files/ に台帳の無い孤児を残さないよう同じ lock 内で run_dir 側へ戻す（回収は run_dir 保持側の責務に一本化する）。
                                try:
                                    _shutil.move(str(_dst), str(_p))
                                except Exception as _move_back_err:
                                    # 差し戻し（2回目の move）にも失敗した場合は、黙って握り潰さず明示的に記録する。`OSError`/`shutil.Error` は絶対パスを本文に含むため、相対パスと型・errno だけ残す。
                                    st._created_files_failed = True
                                    _log.warning(
                                        "codex created file could not be moved back to "
                                        "run_dir after registration failure (orphaned in "
                                        "files/): %s: type=%s errno=%s",
                                        _masked_run_dir_path(_fp, run_dir),
                                        type(_move_back_err).__name__,
                                        getattr(_move_back_err, "errno", None))
                                raise
                        break
                    else:
                        # 同名回避の候補（`_i` が 10001 通り）をすべて使い切った＝この成果物は保存できていない。
                        st._created_files_failed = True
                        _log.warning("codex created file: no free file name after %d candidates: %s",
                                    _i, _masked_run_dir_path(_fp, run_dir))
                except Exception as e:
                    # 通常の move 失敗も、差し戻し失敗と同じく型と errno だけ記録する（フルパスは出さない）。
                    st._created_files_failed = True
                    _log.warning("codex created file move/registration failed for %s: type=%s errno=%s",
                                _masked_run_dir_path(_fp, run_dir),
                                type(e).__name__, getattr(e, "errno", None))
    except Exception as e:
        # 個別ファイルのループへ入る前の設定段階（store import・_expires 計算等）の失敗。この時点で全件が run_dir に残ったまま。
        st._created_files_failed = True
        _log.warning("codex created files registration setup failed (run_dir=%s): %s",
                    run_dir.name, e)


def assemble_result(self, ctx, st, decision, env):
    """env への合流（limits・縮退・usage・作成ファイル）・回答の組み立て・`answer_delta` と `_result` を出す。"""
    sp = st.sp
    _schema_on = st._schema_on
    _skip_presearch = st._skip_presearch
    uid = st.uid
    _wall_clock_state = st._wall_clock_state
    _mcp_error_codes = st._mcp_error_codes
    mcp_neighbors = st.mcp_neighbors
    _mcp_read_docs = st._mcp_read_docs
    _mcp_listed_docs = st._mcp_listed_docs
    codex_created_files = st.codex_created_files
    _created_file_rows = st._created_file_rows
    # limits（利用統計「打ち切りの内訳」）: 自動継続はこの `run()` 自身が判定しているためここで数える（`search_truncated`／`tool_result_clipped`／`duplicate_tool_call` は `mcp_server.py` がサイドカー経由で報告した値を下で合流させる）。
    if st._auto_continue_count and isinstance(env, dict):
        env["limits"] = {**(env.get("limits") or {}), "auto_continues": st._auto_continue_count}
    # 深さ案内（`chat_service._depth_actually_helps`）が本体と同じ判定を使うための受け渡し口。`codex_multi_agent_enabled` の結果を usage の is_local から再現できない（Azure も既定 OpenAI と同じ "cloud"）ため、結果そのものを渡す。
    if isinstance(env, dict):
        env["codex_multi_agent"] = st._multi_agent_enabled
    # グラフ・全文検索の縮退（親が検知した世代不一致＋子がサイドカーで報告した障害コード）を1つの印にまとめる。世代不一致が1件でもあれば「再取り込み待ち」を優先する。通知文言・グラフ側の計数は `chat_service._finalize`（`_apply_graph_degraded`）が env のこの印から組む。
    if isinstance(env, dict):
        # 事前検索（`chat_service._dispatch`）が既に世代不一致を立てていれば、子の接続断で上書きしない（「再取り込み待ち」を優先する）。
        _era = (st._graph_schema_era_error is not None
                or agentic_search.GRAPH_REINGEST_ERROR_CODE in _mcp_error_codes
                or env.get("graph_degraded") == agentic_search.GRAPH_REINGEST_ERROR_CODE)
        if _era:
            env["graph_degraded"] = agentic_search.GRAPH_REINGEST_ERROR_CODE
        elif "graph_unavailable" in _mcp_error_codes:
            env["graph_degraded"] = "graph_unavailable"
        if any(c in _mcp_error_codes for c in ("es_unavailable", "es_query_failed")):
            env["limits"] = {**(env.get("limits") or {}), "backend_unavailable_fulltext": True}
        # MCP ツール結果のバイト予算（per-call クリップ／累計予算到達）を利用統計「打切りの内訳」へ合流させる（`_CONTEXT_WINDOW_EXCEEDED_CODE` 分岐が既に `total_budget_hit` を立てていれば上書きしない）。
        if st._mcp_tool_result_clipped:
            env["limits"] = {**(env.get("limits") or {}),
                             "tool_result_clipped": (env.get("limits") or {}).get(
                                 "tool_result_clipped", 0) + st._mcp_tool_result_clipped}
        if st._mcp_total_budget_hit:
            env["limits"] = {**(env.get("limits") or {}), "total_budget_hit": True}
        # 同一クエリの重複実行の抑止（`mcp_server.py::_is_duplicate_tool_call`）の件数も同じ経路で合流させる。
        if st._mcp_duplicate_tool_call:
            env["limits"] = {**(env.get("limits") or {}),
                             "duplicate_tool_call": (env.get("limits") or {}).get(
                                 "duplicate_tool_call", 0) + st._mcp_duplicate_tool_call}
        # `run_tool()` 自身の内部切り詰め（grep ヒット上限・件数上限等）も同じ経路で合流させる（API 経路の `_record_run_tool_limits` と同じ語彙 `search_truncated`）。
        if st._mcp_search_truncated:
            env["limits"] = {**(env.get("limits") or {}),
                             "search_truncated": (env.get("limits") or {}).get(
                                 "search_truncated", 0) + st._mcp_search_truncated}
        # MCP ツール呼び出し回数の上限到達も同じ経路で合流させる（`total_budget_hit` と同じ bool 方式）。
        if st._mcp_tool_calls_exhausted:
            env["limits"] = {**(env.get("limits") or {}), "tool_calls_exhausted": True}
    # troubleshoot は Codex が実際に引いた近傍を UI カードにする（`_gather` 由来を Codex の実調査由来で上書きする）。
    _apply_codex_neighbors(env, mcp_neighbors, decision.get("lens") if decision else None)
    apply_codex_usage(ctx, st, env)
    # 使った検索と読んだ資料の数（取れる範囲）と、調査記録を縮めた旨を調査台帳の欄に添える（「調べた範囲」の素材）。
    if isinstance(env.get("investigation"), dict):
        env["investigation"]["effort"] = {"searches": _count_searches(env.get("activity")),
                                          "docs_read": len(set(st._mcp_read_docs))}
        if st._record_notes:
            env["investigation"]["record_notes"] = list(st._record_notes)
    # 捕捉した session/thread id を env に載せる（chat_service が `store.set_session_id` で永続化し次ターンの resume 判定に使う）。ゲートは `_session_persistence_enabled`（conversation_id あり かつ サンドボックス有効）を使う（`SHERPA_CODEX_SANDBOX=0` は使い捨て thread_id のため DB に保存すると次回の resume が必ず失敗する）。
    if st._session_persistence_enabled and st.thread_id:
        env["codex_session_id"] = st.thread_id
    # Codex がファイルを作成した場合は env に記録する（chat_service が contains_personal を立てる）。files/ 外への書き込みも含めて codex_wrote_files フラグを立てる。
    if codex_created_files:
        env["codex_wrote_files"] = [Path(f).name for f in codex_created_files] or True
    # 台帳登録に成功したファイルを UI の「作成したファイル」カード用に env へ載せる（既存の /workspace/files DL API を再利用・rel_path は同名衝突回避後の最終名）。
    if _created_file_rows:
        env["created_files"] = [
            {"name": r["rel_path"], "download_url": f"/workspace/files/{r['id']}/download"}
            for r in _created_file_rows
        ]
    if st.answer:
        env["headline"] = st.answer
        # 直読した資料は MCP の結果に載らず env["sources"] に反映されない。回答末尾の「参照した資料:」ブロックを解析し、read 系 MCP ツール引数から拾った doc_id（記載漏れの補完・出現順で後ろに合流）と合わせて機械検証（実在・文書種別・scope・秘匿名除外）を通ったものだけを sources の先頭へ足す（`_gather` 由来の既存 sources は後ろに残す・doc_id 重複は除外）。verified が0件なら参照ブロックの記載を消さず本文をそのまま残す。
        _body, _listed_lines = parse_referenced_doc_lines(st.answer)
        _ref_candidates: list = list(_listed_lines)
        _ref_seen: set[str] = set()
        for _r in _mcp_read_docs:
            if _r and _r not in _ref_seen:
                _ref_seen.add(_r)
                _ref_candidates.append(_r)
        # `xlsx_sheets`（シート一覧のみ）の doc_id も参照候補（sources）には合流させる。ただし「参照した資料:」にも書かれておらず本文精読ツール（`_mcp_read_docs`）でも読まれていない doc_id は、根拠ゲート（sources_verified）に数えない。参照ブロック＋本文精読ツールだけで確定する「精読済み」集合を先に確定させ、`xlsx_sheets` を足した後の verified との差分（`_listed_only_ids`）として区別する。
        _verified_listed, _unverified_refs, _sensitive_refs = resolve_referenced_docs(
            _ref_candidates, ctx.world, sp)
        _verified_before_listed = set(_verified_listed)
        for _r in _mcp_listed_docs:
            if _r and _r not in _ref_seen:
                _ref_seen.add(_r)
                _ref_candidates.append(_r)
        _verified_refs = resolve_referenced_docs(_ref_candidates, ctx.world, sp)[0]
        _listed_only_ids = set(_verified_refs) - _verified_before_listed
        if _verified_refs and ctx.make_sources:
            _ref_sources, _ = _verified_sources(ctx.make_sources, set(_verified_refs), ctx.world, sp)
            # 参照ブロックの記載順（→ MCP 引数の順）に並べ直し、既存（`_gather` 由来）と重複する資料は先頭側（参照した資料）を残す。
            _order = {d: i for i, d in enumerate(_verified_refs)}
            _ref_sources = sorted(_ref_sources, key=lambda s: _order.get(s.get("doc_id"), len(_order)))
            _ref_ids = {s.get("doc_id") for s in _ref_sources}
            env["sources"] = _ref_sources + [s for s in (env.get("sources") or [])
                                             if s.get("doc_id") not in _ref_ids]
            # 実際に開いて根拠にした資料＝API 経路の「精読済み」と同じ意味＝出典の 2 区分（根拠／参考）に載せる。`xlsx_sheets` だけで到達した doc_id（`_listed_only_ids`）は除く。
            env["sources_verified"] = sorted(_ref_ids - _listed_only_ids)
        env["codex_referenced_docs"] = {"listed": len(_ref_candidates), "verified": len(_verified_refs)}
        # 「参照した資料」の行のうち検証を通らなかったもの（理由つき・秘匿名は名前を出さず件数だけ）。
        _unverified_total = len(_unverified_refs) + _sensitive_refs
        if _unverified_total:
            env["sources_unverified"] = _unverified_refs[:_SOURCES_UNVERIFIED_MAX]
            if _sensitive_refs:
                env["sources_unverified_hidden"] = _sensitive_refs
            if len(_unverified_refs) > _SOURCES_UNVERIFIED_MAX:
                env["sources_unverified_more"] = len(_unverified_refs) - _SOURCES_UNVERIFIED_MAX
            add_notice(env, "sources_unverified",
                       f"回答が出典として挙げた資料のうち {_unverified_total} 件は、確認できなかったため出典に載せていません"
                       "（出典の欄の下の「確認できなかった資料」を参照）"
                       + ("。名前を表示できない資料があります。" if _sensitive_refs else "。"))
        env["headline"] = _body if (_verified_refs and _body.strip()) else st.answer  # 空本文には差し替えない
        if _schema_on:
            # v2 のときだけ主張配列を持つ（v1 は常に空リスト）。区分と理由コードを envelope にも載せる（共有・監査で消えないよう `sherpa/store/shares.py::_safe_claim` が既知フィールドのみで再構築する）。
            _claims = _pick_structured_claims(st)
            _claims_invalid = _pick_structured_claims_invalid(st)
            # 正当な 0 件（挨拶など）は何も付けない。不正で除かれた主張があったターンは、残りの主張を通常どおり検証し、件数を残して注記する。
            if _claims or _claims_invalid:
                # API 経路と同じ最終ゲートを Codex にも掛ける。確定主張のうち必須の根拠種別を欠くものを推定へ格下げし、ターン単位の不足を headline 冒頭に前置する（本文は書き換えない・作成系は成果物の中身に混ざるため注記を出さない）。
                _claims, _gate_meta, _gate_missing, _gate_unavailable = _apply_codex_evidence_gate(
                    _claims, lens=decision["lens"], world=ctx.world, scope_paths=sp,
                    layer=layer_mod.effective_layer(ctx.scope_meta, decision["lens"]),
                    personal_facts=ctx.personal_facts)
                # 台帳突合（claims は台帳からの投影）は根拠種別ゲートの後に適用する（両方の格下げが独立に効く）。
                _claims, _claims_ledger_check = _claims_vs_ledger(
                    _claims,
                    investigation_ledger.load_ledger(st._investigation_dir)
                    if st._investigation_dir is not None
                    else investigation_ledger.LedgerSnapshot(manifest=None, items={}, invalid_ids=()),
                    manifest_file_exists=(
                        (st._investigation_dir / "manifest.json").is_file()
                        if st._investigation_dir is not None else False))
                env.setdefault("data", {})["claims"] = _claims
                env["data"]["evidence_gate"] = _gate_meta
                if _claims_invalid:
                    env["data"]["claims_invalid"] = _claims_invalid
                if st._investigation_verdict is not None:
                    env["investigation"]["claims_check"] = _claims_ledger_check
                _omitted = _claims_ledger_check.get("omitted_items") or []
                _omitted_hidden = _claims_ledger_check.get("omitted_hidden", 0)
                # 形式不正で除いた主張が項目を指していた可能性があるターンは数えない。
                if (_omitted or _omitted_hidden) and not _claims_invalid and "investigation" in env:
                    env["investigation"]["omitted_items"] = _omitted
                    if _omitted_hidden:
                        env["investigation"]["omitted_hidden"] = _omitted_hidden
                    add_notice(env, "claims_omitted",
                               f"調べたが回答に入っていない項目が {len(_omitted) + _omitted_hidden} 件あります。")
                env["limits"] = {**(env.get("limits") or {}),
                                 "claims_unmatched": _claims_ledger_check.get("downgraded", 0) > 0}
                if decision["lens"] != "author":
                    _gate_note = _evidence_gate_note(_gate_missing, _gate_unavailable)
                    # 種別ゲート・台帳突合のどちらかで confirmed が1件でも格下げされたら同じ注記を出す（二重には付けない・`_gate_missing` があるターンは既に注記が格下げを示唆しているため重ねない）。
                    _any_claims_demoted = (
                        _gate_meta["demoted"] > 0
                        or _claims_ledger_check.get("downgraded", 0) > 0)
                    if not _gate_missing and _any_claims_demoted:
                        _gate_note = _DEMOTED_CLAIMS_NOTE + _gate_note
                    if _claims_invalid:
                        _gate_note = _INVALID_CLAIMS_NOTE + _gate_note
                    if _gate_note:
                        add_notice(env, "evidence_gate", _gate_note)
        if _schema_on:
            # 設計書とソースの照らし合わせ: 根拠の参照を検証した行だけを表に出す（落とした・未確認に下げた件数は注記で伝える）。
            _recon_rows, _recon_invalid = _pick_structured_reconciliation(st)
            if _recon_rows:
                _recon_rows, _recon_meta = verify_reconciliation(_recon_rows, ctx.world, sp)
                env.setdefault("data", {})["reconciliation"] = _recon_rows
                if _recon_meta["unverified"]:
                    add_notice(env, "reconciliation_unverified",
                               f"設計書とソースの照らし合わせのうち {_recon_meta['unverified']} 件は、根拠の資料を確認できなかったため「未確認」にしています。")
                if _recon_meta["more"]:
                    add_notice(env, "reconciliation_more",
                               f"設計書とソースの照らし合わせは件数が多いため、{_recon_meta['more']} 件を表に出していません。")
            if _recon_invalid:
                add_notice(env, "reconciliation_invalid",
                           f"設計書とソースの照らし合わせのうち {_recon_invalid} 件は形式が不正で、表に出せませんでした。")
        # 実際に回答を生成できたターンは、`_dispatch` がツール遮断時に立てた `agentic_failure`（`agentic_search.tools_blocked_env`）を消す（Codex は遮断状態を見ずに調査を続行し得るため）。
        env.pop("agentic_failure", None)
        # 「追加で調べますか？」: 最後の中間の見直しが mostly_answered かつ extra_perspectives を挙げていたら定型文を付ける（AI の自由記述はそのまま流さない・`_review_continuation_note_text` は観点を短く切って並べた決定的な文字列）。台帳は既に退避済みで、「続き」で復元されれば `_investigation_dir`／`reviews.jsonl` がそのまま戻る。
        if st._review_continuation_note_text:
            add_notice(env, "review_continuation", st._review_continuation_note_text)
        # 自動継続を尽くしてもなお進行中の宣言文が headline に残ったターンは、本文を書き換えず `_codex_stopped_early` を根拠に envelope へ印を付ける。chat_service._finalize が予算到達時の途中結果・出典0件時の案内と同形式（headline 直下の独立注記＋案内ボタン）で UI に出す（`stop_reason` の閉じた語彙とは別のマーカー）。
        if st._codex_stopped_early:
            env["codex_stopped_early"] = True
        yield _node("codex", "think", "Codex が調べる",
                    "調べて回答をまとめました" if st.ran else "回答をまとめました", "done")
    elif st._codex_silent_failure:
        # 利用統計の終了理由分布（`stop_kind_mod.resolve`）がこの分岐を `codex_silent` と判定できるよう印を立てる。
        env["codex_silent_failure"] = True
        # `_gather` が組み立てた決定的回答をそのまま返さず、利用者に「AI が答えていない」ことが伝わるよう `_UnwiredProvider` と同じ文体の正直な文言に上書きする。summary/sources は `_gather` の実結果のまま残すが、sources が空なら data も `{}` へ揃える（`chat_service._no_genuine_results` の honest failure 規約と一致させる）。
        # 無出力失敗はプロキシ/CA 証明書の不備・sandbox の起動失敗・CLI のクラッシュでも起きるため、認証だけに断定せず、観測事実（応答を返す前に終了）を述べて考えられる原因を複数挙げる。returncode は利用者向け本文には出さずログにだけ残す。stderr は破棄している。
        # `turn.failed`／`error` で閉じた attempt（agent_message 無し）は「回答を返せずに終了しました」とし、既存の無出力失敗と文言を分ける（スキーマ違反なども原因になり得るため）。
        _reason = ("回答を返せずに終了しました" if st._turn_failed
                  else "応答を返す前に終了しました")
        if st._turn_failed_code:
            # 閉集合ではない生の診断コード（統計・分類には使わない・管理者向け補助情報）。
            env["codex_error_code"] = st._turn_failed_code
        if st._turn_failed_code == _CONTEXT_WINDOW_EXCEEDED_CODE:
            # 1回の調査で集めたツール結果だけで文脈枠を使い切った場合は、認証/ネットワーク不調と混同させず、範囲を絞る具体的な次の一手を示す。
            env["headline"] = (
                "調べる範囲が広すぎて、今回は回答をまとめられませんでした。"
                "範囲（フォルダ）を絞るか、質問を分けてやり直してください。"
            )
            env["limits"] = {**(env.get("limits") or {}), "total_budget_hit": True}
        else:
            env["headline"] = (
                f"Codex に接続できませんでした（Codex CLI が{_reason}）。考えられる原因はいくつかあります: "
                "認証が設定されていない（`codex login`）／プロキシや CA 証明書などのネットワーク設定が"
                "不足している／サンドボックスの起動に失敗した／Codex CLI 自体が異常終了した、のいずれかです。"
                "管理者にログの確認を依頼してください。詳細は管理者向けの Codex ログ（codex.log）を参照してください。"
            )
        if not env.get("sources"):
            env["data"] = {}
        _log.warning(
            "codex silent failure: returncode=%s turn_failed=%s code=%s conv=%s uid=%s",
            st.attempt_returncode, st._turn_failed, st._turn_failed_code,
            ctx.conversation_id, uid)
        yield _node("codex", "think", "Codex が調べる",
                    "応答がありませんでした（原因未特定・決定的回答は使いません）", "done")
    else:
        # 本文（answer）は空だが、完全な沈黙（`_codex_silent_failure`）ではないケース（command_execution 等は実行できたが結論の agent_message が無いまま打ち切られた）。silent failure 分岐は headline で既に告知しているため対象外のまま、env["headline"] が `_gather` の headline のままの場合にも注記を出す（presearch を省いたターンは `_NO_PRESEARCH_HEADLINE` のまま）。`_stream_error` は Popen 完走前の例外でも立つため、`attempt_returncode is None` のまま `_codex_silent_failure` が計算されずここに落ちても終了理由の分布から漏らさない（`stop_kind.resolve` の codex_silent 判定に必要な印）。
        if st._stream_error:
            env["codex_silent_failure"] = True
        if st._codex_stopped_early:
            env["codex_stopped_early"] = True
        # presearch を省いたターンは決定的回答へ「切替」ようが無い（下調べを実行していない）ため、決定的回答を使ったかのような文言にしない。
        _no_answer_detail = ("（未応答のため回答を出せませんでした）" if _skip_presearch
                             else "（未応答のため決定的回答に切替）")
        yield _node("codex", "think", "Codex が調べる", _no_answer_detail, "done")
    # 台帳の確認できなかった項目を、回答の末尾へ機械的に付ける（AI は使わない・無ければ付けない）。本文を選べなかったターン（無出力・未応答）でも台帳から作って返す。`_investigation_snapshot` は降格適用後の状態で `env["investigation"]["counts"]` と一致する。同じリストを `env["investigation"]["unconfirmed_items"]` にも構造化形で載せる（Codex ジョブ API の `unconfirmed_items` の正本）。
    if st._investigation_snapshot is not None:
        _unconfirmed_items = _unconfirmed_items_list(st._investigation_snapshot)
        env.setdefault("investigation", {})["unconfirmed_items"] = _unconfirmed_items
        _unconfirmed_section = _format_unconfirmed_items_section(_unconfirmed_items)
        if _unconfirmed_section:
            add_notice(env, "unconfirmed_items", _unconfirmed_section)
    if st._discarded_files:
        add_notice(env, "files_discarded",
                   f"資料の作成を求められていない依頼のため、AI が作ったファイル {st._discarded_files} 件は保存していません。"
                   "ファイルが必要なときは「資料を作成」から依頼してください。")
    if st._marp_failed or st._marp_failed_formats:
        _fmts = "・".join(f.upper() for f in st._marp_failed_formats)
        add_notice(env, "marp_failed", _MARP_FAILURE_NOTE
                   if st._marp_failed and not _fmts else
                   f"スライドの書き出しのうち {_fmts} への変換に失敗しました。できた形式と Markdown の原稿は保存しています。")
    if st._mcp_coverage_write_failed:
        add_notice(env, "coverage_write_failed",
                   f"調査の確認記録を {st._mcp_coverage_write_failed} 回書き込めませんでした。"
                   "「確認できなかった項目」が実際より多く（または少なく）見えている可能性があります。")
    for _note in st._record_notes:
        add_notice(env, "investigation_record_trimmed", _note)
    if st._created_files_failed:
        # headline がどの分岐で組み立てられていても、保存できなかった成果物がある事実は一律に伝える。
        add_notice(env, "created_files_failed", _CREATED_FILES_FAILURE_NOTE)
    if _wall_clock_state["hit"]:
        # headline がどの分岐で組み立てられていても、時間の上限で打ち切った事実は一律に伝える。「打ち切りの内訳」（利用統計）へも記録する。
        add_notice(env, "wall_clock", _WALL_CLOCK_LIMIT_NOTE)
        env["limits"] = {**(env.get("limits") or {}), "wall_clock_hit": True}
    _finish_partial_status(ctx, st, env)
    yield {"type": "answer_delta", "text": env["headline"]}  # Codex は一括→フロントで段階表示
    yield {"type": "_result", "env": env, "decision": decision,
          "investigation_record": st._investigation_record_payload}


def _finish_partial_status(ctx, st, env):
    """回収済みの本文は書き換えず、注記を `answer.notices` へ足し、停止・部分回答の終端を記録して回答の形を確定する。"""
    if st._invalid_output_events:
        st._answer_notices.append(("invalid_output",
            f"壊れた出力イベント {st._invalid_output_events} 件を解釈できず除外しました（失われた本文の文字数不明）。"))
    for _n in st._answer_notices:
        _kind, _text = _n if isinstance(_n, tuple) else ("answer_recovery", _n)
        add_notice(env, _kind, _text)
    if st._codex_stopped_early:
        add_notice(env, "stopped_early", STOPPED_EARLY_NOTICE)
    if st._trimmed:
        env["trimmed"] = [dict(t) for t in st._trimmed]
    if ctx.stop_event is not None and ctx.stop_event.is_set():
        env["_terminal"] = "stopped"
        env["stopped_by_user"] = True
        env["completion"] = "stopped"
        add_notice(env, "stopped", STOPPED_NOTICE)
    elif (st._answer_notices or st._stream_error or st._turn_failed or st._codex_stopped_early
          or st._wall_clock_state["hit"] or st._created_files_failed
          or (env.get("limits") or {}).get("ledger_incomplete")):
        env["completion"] = "partial" if st.answer else "failed"
    else:
        env["completion"] = "failed" if st._codex_silent_failure else "complete"
    seal(env)


def recovered_result(ctx, st, decision, env, exc):
    """回答の仕上げの例外でも回収済みの本文・検証済み出典・台帳を返す。"""
    body = st.answer
    if not body:
        if st._schema_on:
            body = _pick_structured_headline(st)
        else:
            body = _pick_codex_headline(st._agent_msgs, st._agent_partial, prefer_marker="参照した資料")
    if not body:
        return None
    st.answer = body
    if not env.get("codex_referenced_docs"):
        set_body(env, body)
    st._stream_error = True
    st._answer_notices.append(f"回答の仕上げでエラーが発生しました（{type(exc).__name__}）。回収済みの回答を返します。")
    env["codex_multi_agent"] = st._multi_agent_enabled
    env.pop("agentic_failure", None)
    _finish_partial_status(ctx, st, env)
    return {"type": "_result", "env": env, "decision": decision,
            "investigation_record": st._investigation_record_payload}
