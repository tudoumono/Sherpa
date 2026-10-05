"""Codex 1 回分の実行（`codex exec` の起動・`--json` イベントの翻訳・後始末）と、`-o` 最終メッセージの取り込み。
設計: docs/design/codex.md「1ターンの流れ」
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time

from ...mcp_server import COMPARE_DOC_ID_ARGS, LISTED_DOC_TOOLS, READ_DOC_TOOLS
from ..base import _log, _node
from . import process
from .ledger_gate import _LEDGER_TOOL_DETAILS
from .mcp import (
    GRAPH_SUMMARY_MAX_CHARS, _codex_ask_capture, _graph_schema_era_from_item, _graph_tool_failure, _graph_tool_summary, _mcp_neighbors_from,
)
from .process import (
    _classify_turn_failure,
    _humanize_cmd,
    _killpg,
    _log_startup_stderr,
    _read_last_message_fallback,
    _spawn_stop_watcher,
    _spawn_wall_clock_watcher,
    _strip_control_markers,
)
from .usage import _accumulate_codex_usage, _usage_from_turn_completed


def _build_argv(st, use_resume: bool) -> list:
    """codex exec を組み立てる。resume 分岐は `codex exec resume [SESSION_ID] [PROMPT]` の位置引数どおり、共通オプションの後・末尾プロンプトの前に `resume <sid>` を挿む。resume 先 id は `thread_id`（`thread.started` で捕捉した最新値）を優先し、未捕捉なら `resume_sid` を使う。
    プロンプト本文は argv に載せず（`ps` で読まれるため）、`-` だけを置いて標準入力から読ませる。本文は `_attempt` が Popen 後に `proc.stdin` へ書く。
    """
    av = list(st.argv_base)
    if use_resume:
        sid = st.thread_id or st.resume_sid
        if sid:
            av += ["resume", sid]
    av.append("-")
    return av


def _attempt(self, ctx, st, decision, use_resume: bool, prompt_text: str | None = None):
    """1回分の codex exec 実行（node/answer_delta を yield）。proc はこの1回限りのローカル状態。`prompt_text` は自動継続用（省略時は通常プロンプト）。"""
    _ask_disabled = st._ask_disabled
    _wall_clock_limit_s = st._wall_clock_limit_s
    _wall_clock_state = st._wall_clock_state
    _agent_msgs = st._agent_msgs
    _event_type_counts = st._event_type_counts
    _mcp_read_docs = st._mcp_read_docs
    _mcp_listed_docs = st._mcp_listed_docs
    _mcp_calls = st._mcp_calls
    mcp_neighbors = st.mcp_neighbors
    _child_thread_ids = st._child_thread_ids
    _last_message_path = st._last_message_path
    popen_env = st.popen_env
    prompt = st.prompt
    run_dir = st.run_dir
    uid = st.uid
    st.got_any_line = False
    st.attempt_returncode = None
    _stderr_f = None
    st._attempt_ran_tools = False
    st._turn_failed = False
    st._turn_failed_code = None
    st._attempt_no += 1
    # 前 attempt の未完 message（item.updated だけで completed が来なかった分）は履歴へ退避してから境界を引く（最新 attempt の判定に前 attempt の途中経過が混ざらないように）。
    if st._agent_partial.strip():
        _agent_msgs.append(st._agent_partial)
    st._agent_partial = ""
    st._attempt_msgs_start = len(_agent_msgs)
    # `-o` は attempt をまたいで同じパス。前 attempt の内容を残すと、何も書かずに終わった attempt が古い文を回答として吸収してしまう。消せないときは残った本文を控え、終了後の吸収でその本文だけ読み飛ばす。
    st._stale_last_message = None
    try:
        _last_message_path.unlink(missing_ok=True)
    except OSError as exc:
        st._stale_last_message = _read_last_message_fallback(_last_message_path)
        _log.warning("codex last-message cleanup failed: %s errno=%s conv=%s uid=%s",
                     type(exc).__name__, getattr(exc, "errno", None), ctx.conversation_id, uid)
    # このプロセス内だけで完結する id 集合（run-level `_mcp_calls` への合算は finally で行う）。
    _attempt_mcp_seen: set = set()
    _mcp_read_done: set = set()  # 収集済み item id（同じ item の再送で二重に数えない）
                                     # item id は attempt ごとに振り直される
    _attempt_mcp_open: set = set()
    _attempt_mcp_max_in_flight = 0
    argv = _build_argv(st, use_resume)
    _stdin_text = prompt if prompt_text is None else prompt_text
    proc = None
    # finally（reap する側）と監視スレッド（waitid で覗く側）の間で「回収済みか」を直列化する。回収済みの後は sid が再利用され得るため、判定・実行はこのロックの中でだけ行う。
    _reap_lock = threading.Lock()
    _reaped = {"done": False}
    try:
        # Popen 直前の最終防衛線（`_select_provider` の選択時チェックを迂回する経路があっても、起動直前にもう一度確認する）。Codex(Ollama) 構成は OpenAI 系 I/O ではないため対象外。
        if self._ollama_base_url is None:
            from ... import llm
            llm.assert_openai_io_allowed()
        # start_new_session で独立プロセスグループにし、停止/後始末で MCP subprocess / shell child まで group ごと確実に殺す。
        # stderr は名前の無い一時ファイルへ向け、`--json` のイベントを1件も出さずに異常終了したときだけ先頭を読んでログに残す（`_log_startup_stderr`）。それ以外は読まずに捨てる（応答が流れ始めた後の stderr には資料名・本文が混ざり得るため）。
        if st._agent_start_mono is None:  # 利用統計 activity: 最初の Popen だけを起点にする
            st._agent_start_mono = time.monotonic()
        _stderr_f = tempfile.TemporaryFile()
        proc = subprocess.Popen(
            argv, env=popen_env, cwd=str(run_dir), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=_stderr_f, text=True,
            start_new_session=True)
        # プロンプト本文を標準入力へ書いて即座に閉じる（argv には `-` しか載っていない）。書き込みは別スレッドにする（本文が大きいとパイプバッファでブロックし、直後の `proc.stdout` の読み取りと双方向でデッドロックするため）。
        def _write_stdin(_p=proc, _text=_stdin_text):
            try:
                _p.stdin.write(_text)
            except Exception:
                pass
            finally:
                try:
                    _p.stdin.close()
                except Exception:
                    pass
        threading.Thread(target=_write_stdin, daemon=True).start()
        # 途中停止の有無によらず常に起動する（別グループの子が pipe を握って離さないケースの pipe 閉じ役も兼ねる・`_spawn_stop_watcher` 参照）。
        _spawn_stop_watcher(proc, ctx.stop_event, _reap_lock, _reaped)
        if _wall_clock_limit_s > 0:
            # 1ターン全体（自動継続込み）の壁時計上限。継続 attempt も最初の Popen 直前に確定した `_agent_start_mono` からの残り時間で打ち切る。
            _spawn_wall_clock_watcher(
                proc, st._agent_start_mono + _wall_clock_limit_s, _reap_lock, _reaped,
                _wall_clock_state)
        node_n = 0
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            st.got_any_line = True
            _et = e.get("type")
            if isinstance(_et, str):
                _event_type_counts[_et] = _event_type_counts.get(_et, 0) + 1
            if e.get("type") == "thread.started":  # session/thread id 捕捉（resume 先の id）
                st.thread_id = e.get("thread_id") or st.thread_id
                continue
            if e.get("type") == "turn.completed":  # ターンのトークン使用量（item ではない）
                _u = _usage_from_turn_completed(
                    e, self.model,
                    codex_model_provider="ollama" if self._ollama_base_url is not None else "openai",
                    system_settings=self._system_settings)
                # 自動継続の attempt をまたいで合算する。
                st.codex_usage = _accumulate_codex_usage(st.codex_usage, _u)
                continue
            if e.get("type") in ("turn.failed", "error"):  # 失敗終了の明示
                st._turn_failed = True
                if st._turn_failed_code is None:
                    _err = e.get("error")
                    _err_dict = _err if isinstance(_err, dict) else {}
                    # `codex_error_info`（`error` dict 内・トップレベルのどちらでも拾う）は `error.code` より粒度が細かい診断コード（例 "context_window_exceeded"）。取れたら優先する。
                    _info = _err_dict.get("codex_error_info") or e.get("codex_error_info")
                    st._turn_failed_code = (
                        _info if isinstance(_info, str) and _info
                        else (_err_dict.get("code") or e.get("code")))
                    if not st._turn_failed_code:
                        # `codex_error_info` を持たない CLI 版のための救済。本文は保存もログ出力もせず、固定語彙へ分類するためだけに読む。
                        st._turn_failed_code = _classify_turn_failure(
                            _err_dict.get("message") or e.get("message"))
                    _log.warning("codex turn failed: code=%s conv=%s uid=%s",
                                 st._turn_failed_code, ctx.conversation_id, uid)
                continue
            item = e.get("item") or {}
            it = item.get("type")
            iid = item.get("id")
            if not iid:  # id 無し item でも node を上書き衝突させない
                iid = f"cx-auto-{node_n}"
                node_n += 1
            if st._attempt_no > 1:  # 2回目以降の attempt は id 空間を分離する（前 attempt のノードを上書きしない）
                iid = f"a{st._attempt_no}-{iid}"
            if it in ("web_search", "file_change"):  # ネイティブ Web 検索／ファイル変更もツール実行（継続打ち切り判定用・表示ノードは追加しない）
                st._attempt_ran_tools = True
            if it == "command_execution":  # Codex 自身の grep/参照を逐次表示
                st.ran = True
                st._attempt_ran_tools = True
                label, detail = _humanize_cmd(item.get("command", ""))
                if item.get("status") == "completed" or e.get("type") == "item.completed":
                    ec = item.get("exit_code")
                    yield _node(f"cx-{iid}", "tool", label,
                                detail + (f"  → exit {ec}" if ec is not None else ""), "done")
                else:
                    yield _node(f"cx-{iid}", "tool", label, detail, "active")
            elif it == "mcp_tool_call":  # Codex の MCP ツール呼びを可視化＋近傍を収集
                st.ran = True
                st._attempt_ran_tools = True
                tool = item.get("tool", "")
                a = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}  # 非 dict 引数で落とさない
                done = e.get("type") == "item.completed" or item.get("status") in ("completed", "failed")
                # 並走計測（このプロセス内のみ・run 全体への合算は `_attempt` の finally）。id が無い item は対象外。初見かつ未完了のときだけ in-flight に加える（初見でいきなり完了した item は total には数えるが in-flight 幅には寄与しない）。再送は seen 済みなので二重に数えない。
                _mcp_id = item.get("id")
                # read 系ツールの引数から実際に読んだ資料の doc_id を集める（「参照した資料:」の記載漏れの補完）。読取が成功して完了した item だけ（失敗・エラー結果・進行中は出典にも根拠にも載せない）。
                _read_ok = (e.get("type") == "item.completed"
                            and item.get("status") not in ("failed", "error")
                            and not (isinstance(item.get("result"), dict) and item["result"].get("isError"))
                            and not item.get("error"))
                if _read_ok and (not _mcp_id or _mcp_id not in _mcp_read_done):
                    if _mcp_id:
                        _mcp_read_done.add(_mcp_id)
                    # 原本読取ツール（xlsx_range/docx_paragraphs/pptx_slides/pdf_pages/file_head）も doc_id 引数を取る読取ツールとして収集する。`xlsx_sheets` はシート一覧だけで本文を読んでいないため、`_mcp_listed_docs`（sources には合流するが sources_verified には数えない）へ分ける。
                    if tool in READ_DOC_TOOLS:
                        _d = a.get("doc_id")
                        if isinstance(_d, str) and _d:
                            _mcp_read_docs.append(_d)
                    elif tool in LISTED_DOC_TOOLS:
                        _d = a.get("doc_id")
                        if isinstance(_d, str) and _d:
                            _mcp_listed_docs.append(_d)
                    elif tool == "compare_documents":
                        for _k in COMPARE_DOC_ID_ARGS:
                            _d = a.get(_k)
                            if isinstance(_d, str) and _d:
                                _mcp_read_docs.append(_d)
                if _mcp_id and _mcp_id not in _attempt_mcp_seen:
                    _attempt_mcp_seen.add(_mcp_id)
                    if not done:
                        _attempt_mcp_open.add(_mcp_id)
                        _attempt_mcp_max_in_flight = max(
                            _attempt_mcp_max_in_flight, len(_attempt_mcp_open))
                elif _mcp_id and done:
                    _attempt_mcp_open.discard(_mcp_id)
                if tool == "ask_user":
                    # ask_user は question 優先（agentic の {"question":..}→return と同じ意味論）。確認ID 付き再送では無視し、1実行1回（codex_question is None で強制）。質問を捕まえたらループを抜け、finally で proc を後始末してから emit してターンを終える。
                    codex_question = st.codex_question
                    if codex_question is None:
                        codex_question = st.codex_question = _codex_ask_capture(item, _ask_disabled)
                    # 捕捉して break する場合は item.completed を待たずに抜けるため、実際の done フラグに関わらずノードを "done" で確定表示する。
                    node_done = done or (codex_question is not None)
                    yield _node(f"cx-{iid}", "tool", "ユーザに確認",
                                f"「{str(a.get('prompt') or '確認が必要です')[:60]}」",
                                "done" if node_done else "active")
                    if codex_question is not None:
                        break
                    continue
                # folder_tree/compare_documents は MCP 経由で Codex にも公開済み（`mcp_server.py::_tool_defs`）のため、表示用ラベル辞書にも対応を持たせる。
                tlabel = {"graph_neighbors": "関係グラフをたどる", "graph_resolve": "影響調査の起点を探す",
                          "graph_impact": "影響先をたどる", "ripgrep_search": "資料を検索（語句そのまま）",
                          "es_search": "資料を検索（全文）", "read_around": "該当箇所を精読",
                          "list_docs": "資料の一覧を確認", "folder_tree": "フォルダ構成を確認",
                          "compare_documents": "世代間の差分を比較",
                          "read_doc": "文書を通読", "doc_outline": "見出し構造を確認",
                          "glob_search": "ファイル名で検索",
                          # agentic_search の `_ORIGINAL_READ_LABELS`／改善ログの `_TOOL_CALL_LABELS` と同じ文言（`xlsx_sheets` はシート一覧のみで `xlsx_range` とは別ラベル・`_FILES_READ_LABEL` からも外れる）。
                          "xlsx_sheets": "原本のシート一覧を確認", "xlsx_range": "原本を読む（Excel）",
                          "docx_paragraphs": "原本を読む（Word）",
                          "pptx_slides": "原本を読む（PowerPoint）",
                          "pdf_pages": "原本を読む（PDF）",
                          "file_head": "原本を読む（先頭）",
                          # 調査台帳の MCP ツール（`mcp_server._LEDGER_TOOLS`）。引数の本文は出さず、件数・状態語彙（閉集合）だけを表示する。
                          "ledger_manifest_set": "調査台帳に項目を登録",
                          "ledger_item_put": "調査台帳の項目を更新",
                          "ledger_status": "調査台帳の状態を確認",
                          "ledger_review_put": "回答前に中間の見直しを記録"}.get(
                              tool, "その他の処理")
                if tool in _LEDGER_TOOL_DETAILS:
                    detail = _LEDGER_TOOL_DETAILS[tool](a)
                elif tool == "graph_impact":
                    detail = ""  # 起点の識別子は検証を通った結果の起点（パス）だけを、完了時に記録する
                else:
                    detail = "「" + str(a.get("name") or a.get("query") or a.get("doc_id")
                                        or a.get("path_prefix") or a.get("name_pattern")
                                        or a.get("pattern") or "") + "」"
                if done and tool in ("graph_neighbors", "graph_resolve", "graph_impact") and item.get("status") == "completed":
                    # 旧世代グラフの構造化エラー（`mcp_server.py::handle` が isError で返す）を先に見る。検知したら `_mcp_neighbors_from` は呼ばない。
                    _era_err = _graph_schema_era_from_item(
                        item, ctx.world, decision.get("lens") if decision else None)
                    if _era_err is not None:
                        st._graph_schema_era_error = _era_err
                        if tool != "graph_neighbors":
                            detail = "調べられませんでした"
                    elif tool == "graph_neighbors":
                        mcp_neighbors.extend(_mcp_neighbors_from(item))
                    elif _graph_tool_failure(item):
                        detail = _graph_tool_failure(item)  # 失敗応答では起点の識別子などの引数を記録しない
                    else:
                        _summary = _graph_tool_summary(tool, item)
                        if _summary:
                            st.mcp_graph_results.append({"tool": tool, "summary": _summary})
                            detail = f"{detail} {_summary}".strip()[:GRAPH_SUMMARY_MAX_CHARS]
                yield _node(f"cx-{iid}", "tool", tlabel, detail, "done" if done else "active")
            elif (it == "collab_tool_call" and e.get("type") == "item.completed"
                  and item.get("tool") == "spawn_agent"):
                # 子スレッド id の捕捉のみ（表示ノードは追加しない）。multi_agent 無効ではこのイベント自体が出ない。
                for _tid in item.get("receiver_thread_ids") or []:
                    if isinstance(_tid, str) and _tid:
                        _child_thread_ids.add(_tid)
            elif it == "reasoning" and e.get("type") == "item.completed":
                txt = [ln for ln in (_strip_control_markers(ln).strip() for ln in
                                     (item.get("text") or "").splitlines()) if ln]
                if txt:
                    yield _node(f"cx-{iid}", "think", "考える", txt[-1][:80], "done")
            elif it == "agent_message" and e.get("type") in ("item.completed", "item.updated"):
                # 最後の1件で上書きせず集める（完了分はリストへ・未完分は partial に保持）。結論の選択は loop 後に `_pick_codex_headline` で決定的に行う。
                _txt = (item.get("text") or "").strip()
                if e.get("type") == "item.completed":
                    if _txt:
                        _agent_msgs.append(_txt)
                    st._agent_partial = ""
                else:  # item.updated＝成長中の未完 message（打ち切り保険）
                    st._agent_partial = _txt
    except Exception:
        st._stream_error = True
    finally:
        # このプロセスで観測した分だけ run-level へ合算する（例外で打ち切られても、見えていた分は計測に残す）。
        _mcp_calls["total"] += len(_attempt_mcp_seen)
        _mcp_calls["max_in_flight"] = max(_mcp_calls["max_in_flight"], _attempt_mcp_max_in_flight)
        if proc:
            try:
                _killpg(proc)  # group ごと（MCP child 含む）確実に後始末
            except Exception:
                pass
            # proc（このセッションのリーダー）を `wait()` で回収する前に呼ぶ（回収後は pid が再利用されうるため）。監視スレッドと直列化するため `_reap_lock` の中で行う。
            with _reap_lock:
                process._kill_session(proc.pid)  # setpgid で group を抜けた孤児を session 単位で回収
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
                _reaped["done"] = True
            st.attempt_returncode = proc.returncode  # fallback 判定の材料（回収後の値）
            # 利用統計 activity: 直近の wait 完了直後を終端にする（attempt ごとに更新＝最後の attempt の終端が残る）。
            st._agent_end_mono = time.monotonic()
        if _stderr_f is not None:
            _log_startup_stderr(_stderr_f, st.attempt_returncode, st.got_any_line,
                                ctx.conversation_id, uid)


def _absorb_last_message_fallback(st) -> None:
    """attempt が `--json` に agent_message を出さず `-o` 最終メッセージファイルにだけ結論を書いたケースを拾う（毎 attempt 終了直後に呼ぶ）。`_last_message_path` は attempt をまたいで使い回すため、最新 attempt の分（`_agent_msgs[_attempt_msgs_start:]`）と同一（strip 比較）なら追加しない（過去の attempt と同文だからと落とすと、この attempt の結論が判定対象から消える）。"""
    _last_message_path = st._last_message_path
    _agent_msgs = st._agent_msgs
    _fb = _read_last_message_fallback(_last_message_path)
    if _fb and _fb == st._stale_last_message:
        return
    if _fb and _fb.strip() not in {m.strip() for m in _agent_msgs[st._attempt_msgs_start:]}:
        _agent_msgs.append(_fb)
