"""ターンの準備の段: 会話ロックと永続 CODEX_HOME の確認（`enter_conversation`）・起動準備（`prepare_run`）。
設計: docs/design/codex.md「1ターンの流れ」
"""
from __future__ import annotations

import shutil
import threading
from pathlib import Path

from ... import investigation_ledger
from ... import layer as layer_mod
from ..base import _log, _node
from .codex_cli import build_launch, log_turn_start, resolve_reasoning
from . import sandbox
from .ledger_gate import (
    _FINAL_ANSWER_VOICE,
    _investigation_tree_has_symlink,
    _ledger_source_required_extra,
    _restore_investigation_ledger,
)
from .sandbox import (
    _codex_sandbox_enabled,
    _direct_read_roots,
    _remove_dir_best_effort,
    _safe_codex_sessions_home,
    _scope_deny_entries,
    _venv_root,
)
from .turn_consts import _MCP_SIDECAR_NAME


# 永続 CODEX_HOME（`.codex-sessions/{conversation_id}`）は同一会話の複数ターンが同じ固定パスを共有するため、同一会話の2実行が重なると config.toml の再作成や session JSONL への書込、終了時の削除が競合する。
# conversation_id をキーにした非ブロッキング lock で、同一会話の永続 CODEX_HOME を使う実行だけを直列化する（別会話・非永続セッションは対象外）。
_CONVERSATION_LOCKS: dict = {}
_CONVERSATION_LOCKS_GUARD = threading.Lock()


def _conversation_lock(conversation_id) -> threading.Lock:
    with _CONVERSATION_LOCKS_GUARD:
        lk = _CONVERSATION_LOCKS.get(conversation_id)
        if lk is None:
            lk = _CONVERSATION_LOCKS[conversation_id] = threading.Lock()
        return lk


def enter_conversation(self, ctx, st, decision):
    """会話単位の lock を取り、永続 CODEX_HOME の安全を確認する。同一会話の別の回答が実行中なら、その旨を返して True（終了）を返す。"""
    uid = st.uid
    users_dir = st.users_dir
    # 会話継続（Codex ネイティブ resume）: conversation_id があるターンだけセッションを永続化する。conversation_id 無しの直接呼出しは per-request 使い捨て CODEX_HOME＋`--ephemeral`。
    st._persist_session = ctx.conversation_id is not None
    st.resume_sid = ctx.codex_session_id if st._persist_session else None
    st.thread_id = None  # 捕捉した Codex session/thread id（`_session_persistence_enabled` のときだけ env に載せる）
    # `SHERPA_CODEX_SANDBOX=0` は常に `--ephemeral` で resume 不能のため、DB へ永続化してよいかはこの専用フラグで判定する。
    st._session_persistence_enabled = st._persist_session and _codex_sandbox_enabled()
    # 永続 CODEX_HOME を使う実行だけ、同一会話単位で非ブロッキング lock を取る。削除エンドポイント（`routers/conversations.py::conversation_delete`）も DB 変更前に同じロックを取る。`.codex-sessions/{cid}` の mkdir や会話の生存確認はロック取得後に行う（削除との競合で孤児ディレクトリを作らないため）。
    st._conv_lock = _conversation_lock(ctx.conversation_id) if st._session_persistence_enabled else None
    if st._conv_lock is not None:
        st._conv_lock_acquired = st._conv_lock.acquire(blocking=False)
    if st._conv_lock is not None and not st._conv_lock_acquired:
        msg = "この会話の別の回答を実行中です。終わってからもう一度お試しください。"
        yield _node("codex", "think", "Codex が調べる",
                   "（この会話の別の回答を実行中のため今回は実行しません）", "done")
        yield {"type": "answer_delta", "text": msg}
        sm = layer_mod.scope_with_layer(ctx.scope_meta, world=ctx.world, lens=decision["lens"])
        sm["source"] = "busy"
        env = {"lens": decision["lens"], "headline": msg, "summary": {"total": 0},
              "data": {}, "sources": [], "busy": True, "scope": sm}
        yield {"type": "_result", "env": env,
              "decision": {"lens": decision["lens"], "input": ctx.message,
                          "reason": "同一会話の Codex 実行が進行中"}}
        return True
    # 永続 CODEX_HOME（`.codex-sessions/{cid}`）は固定パスのため、symlink を事前に仕込まれると封じ込めが崩れる。`ws_authoring` と同じ fail-closed 契約で、安全確認できなければ Codex を起動しない（決定的回答へ）。
    # ロック取得後に会話の生存（所有 DB 行）も再確認する（削除済みなら mkdir せず実行しない）。DB 到達不可はこの確認の対象外（fail-open）。
    st._safe_persistent_codex_home = None
    st._codex_home_ok = True
    if st._session_persistence_enabled:
        from ... import store as _store
        try:
            _conv_alive = _store.owns_conversation(uid, ctx.conversation_id)
        except Exception:
            _conv_alive = True
        if not _conv_alive:
            st._codex_home_ok = False
        else:
            st._safe_persistent_codex_home = _safe_codex_sessions_home(users_dir, uid, ctx.conversation_id)
            st._codex_home_ok = st._safe_persistent_codex_home is not None
    st._auto_continue_count = 0  # Codex を起動しない経路でも参照するため起動条件の外で初期化
    st._multi_agent_enabled = False  # env["codex_multi_agent"] 用: Codex を起動しない経路では常に偽
    return False


def prepare_run(self, ctx, st, decision):
    """Codex を起動する準備: 層・直読の可否の確認（失敗は正直に伝えて True＝終了）・作業領域・調査台帳の復元・推論の設定・プロンプト・起動引数・開始ログ。"""
    _plain = st._plain
    uid = st.uid
    users_dir = st.users_dir
    run_dir = st.run_dir
    st._agent_partial = ""
    # stream 読取が途中例外で終わったか。例外時は完全版が入り得る `-o` 最終メッセージファイルを先に試す。
    st._stream_error = False
    # 出力スキーマ有効時（`_schema_on`）だけ使う状態: `_latest_structured` は最新 attempt の最終出力の検証結果（合格した dict・不合格/欠落は None）。`_structured_answers` は attempt をまたいで合格した dict を積む。
    st._latest_structured = None
    # 台帳ゲートが「未完了のため受理しない」と判断した final は、見出し/主張候補として拾わない。`_structured_answers[:_structured_answers_valid_from]` は拒否済み扱いで、`_pick_structured_headline`/`_pick_structured_claims` はこの境界より後だけを走査する（台帳ゲートが継続を発行する直前に境界を更新する）。
    st._structured_answers_valid_from = 0
    sp = (ctx.scope_meta or {}).get("scope_paths")
    # Codex 自身の追加探索（MCP／直接grep）への層フィルタは qa レンズだけに渡す。author・impact/troubleshoot は対象外。
    _layer = (ctx.scope_meta or {}).get("layer") if decision["lens"] == "qa" else None
    st.sp, st._layer = sp, _layer
    # 台帳の完了判定へ足す source の必須化は、ターンの最初に1回だけ決め、このターンの台帳ゲート呼び出し全て（required_extra・AGENTS.md の台帳段落・ledger_status への env）へ同じ値を渡す。判定は MCP へ実際に渡す実効の層＝`_layer` を使う。plain も台帳ツールを出さないため計算しない。
    if not _plain:
        st._ledger_required_extra = _ledger_source_required_extra(ctx.world, sp, _layer)
    _layer_restricted = _layer not in (None, "both")
    # 層のフィルタは MCP ツール側（run_tool）だけが担う。sandbox 無効の構成では層の指定をツールに渡す経路が無いため、黙って無視せず実行前に正直に失敗する。
    _layer_enforcement_ready = _codex_sandbox_enabled()
    if _layer_restricted and not _layer_enforcement_ready:
        # 黙って層を無視した回答を返さず、実行せず正直に失敗を伝える（Codex CLI は起動しない）。利用者向け文言は専門用語ゼロ（MCP/sandbox を出さない）。具体的な理由は decision.reason（監査・管理者ログ専用）にだけ残す。
        msg = "この構成では探す対象の限定はできません。管理者に設定の確認を依頼してください。"
        yield _node("codex", "think", "Codex が調べる", "探す対象の限定に対応していません", "done")
        yield {"type": "answer_delta", "text": msg}
        env = {"lens": decision["lens"], "headline": msg, "summary": {"total": 0},
              "data": {}, "sources": [],
              "agentic_failure": "error",  # 実行していないターン＝完了として数えない
              "scope": layer_mod.scope_with_layer(ctx.scope_meta, world=ctx.world,
                                                  lens=decision["lens"])}
        _reason = "sandbox 無効時は探す対象の限定に対応できません"
        yield {"type": "_result", "env": env,
              "decision": {"lens": decision["lens"], "input": ctx.message, "reason": _reason}}
        return True
    # authoring/ = Codex の書込先（cwd）。files/ = ユーザーアップロード（cwd 外・Codex から隔離）。files/ の symlink チェックはアップロード grep 側（`chat_service._personal_grep_hits`）で行う。
    ws_files = users_dir / uid / "workspace" / "files"
    if ws_files.is_symlink():
        ws_files = None  # type: ignore[assignment]
    else:
        ws_files.mkdir(parents=True, exist_ok=True)
    # 実行前の run_dir スナップショット（新規ファイル検出用）。
    _before_ws_files: set = set()
    if run_dir.is_dir():
        # `.agents`（配備したスキル）配下・ルート直下の AGENTS.md・`.mcp_sidecar.jsonl` は台帳登録スキャン対象外にする（AGENTS.md は write_agents_md() が毎回書くため、除外しないと毎回新規ファイルとして登録される。サイドカーはフォールバック経路で run_dir 直下に残る場合の保険）。
        _before_ws_files = {
            p for p in run_dir.rglob("*")
            if p.is_file() and not p.is_symlink()
            and p.relative_to(run_dir) not in (Path("AGENTS.md"), Path(_MCP_SIDECAR_NAME))
            and not ({".tmp", ".agents"} & set(p.relative_to(run_dir).parts))
        }
    resolve_reasoning(self, ctx, st, decision)
    # 原本直読の read root と秘匿 deny の列挙は、プロンプトの文言（direct_read フラグ）と permission profile（`_write_codex_authoring_config`）の両方が使うため、プロンプト組立の前に1回だけ計算する。列挙失敗（RuntimeError＝fail-closed）時は両方とも「直読不可」に揃える。
    _base_roots = _direct_read_roots(ctx.world)
    _direct_roots, _deny_roots = _base_roots, []
    _venv_for_deny = _venv_root()
    try:
        _scope_deny = _scope_deny_entries(_base_roots, sp)
        if _base_roots and all(r in _scope_deny for r in _base_roots):
            raise RuntimeError("scope_enum_failed:no_scope_in_roots")  # 範囲がどの root にも無い
        _sensitive_deny = _scope_deny + sandbox._enumerate_sensitive(
            _base_roots + ([str(_venv_for_deny)] if _venv_for_deny else []))
        _direct_read_ok = True
    except RuntimeError as e:
        _direct_roots, _deny_roots, _sensitive_deny, _direct_read_ok = [], _base_roots, [], False
        _log.warning("codex direct read disabled: %s", e)
    if not _direct_read_ok and _plain:
        # 素の Codex（plain）は直接参照だけが資料を読む手段のため、直読を許可しないと何も調べられない。黙って空振りの回答を返さず、実行前に正直に失敗する（利用者向け文言は専門用語ゼロ）。
        msg = "この資料フォルダは今回読み取りの準備ができませんでした。管理者に確認を依頼してください。"
        yield _node("codex", "think", "Codex が調べる", "資料の読み取り準備に失敗", "done")
        yield {"type": "answer_delta", "text": msg}
        env = {"lens": decision["lens"], "headline": msg, "summary": {"total": 0},
              "data": {}, "sources": [],
              "agentic_failure": "error",  # 実行していないターン＝完了として数えない
              "scope": layer_mod.scope_with_layer(ctx.scope_meta, world=ctx.world,
                                                  lens=decision["lens"])}
        yield {"type": "_result", "env": env,
              "decision": {"lens": decision["lens"], "input": ctx.message,
                           "reason": "素の Codex で直読の準備（秘匿ファイル列挙／範囲）に失敗"}}
        return True
    # `--ephemeral`（セッションをディスクに残さない）と `-o`（最終メッセージのファイル出力＝JSON 抽出が空だった時の保険）は sandbox/fallback どちらでも共通。`.tmp/` は台帳登録スキャンから除外済み。
    # run_dir は実行ごとの新規作成（`mkdir(exist_ok=False)`）のため前ターンの残存はなく、symlink にすり替わっていた場合は rmtree が例外を送出する（fail-closed のまま残す）。
    _tmp = run_dir / ".tmp"
    if _tmp.exists() or _tmp.is_symlink():
        shutil.rmtree(_tmp)
    _tmp.mkdir(parents=True, exist_ok=True)
    st._tmp = _tmp
    # 調査台帳の置き場。`.tmp` と同じく成果物登録スキャンの対象外。空の `items/` を毎 run 用意する。
    st._investigation_dir = _tmp / "investigation"
    (st._investigation_dir / "items").mkdir(parents=True, exist_ok=True)
    # 前ターン退避の扱い（会話 id が無い非永続ターンは何もしない）: 永続台帳ディレクトリ（`workspace/.codex-sessions/{cid}`）に未完了台帳が残っていれば、「続き」宣言のときだけ復元し、それ以外は消す。判定は `_session_persistence_enabled`（conversation_id あり かつ サンドボックス有効）で行う（`SHERPA_CODEX_SANDBOX=0` は常に `--ephemeral` で `.codex-sessions/{cid}` を作らない）。
    # 素の Codex（plain）は台帳を使わず、standard が退避した台帳を復元も削除も退避もしない。
    if st._session_persistence_enabled and not _plain:
        st._ledger_home = _safe_codex_sessions_home(users_dir, uid, ctx.conversation_id)
    if st._ledger_home is not None:
        _retired_investigation = st._ledger_home / "investigation"
        if _retired_investigation.is_dir():
            # 退避先に symlink が1つでもあれば復元せず削除する（「続き」宣言かどうかに関わらず）。
            if _investigation_tree_has_symlink(_retired_investigation):
                _log.warning(
                    "investigation ledger restore skipped: symlink detected in retired dir")
                _remove_dir_best_effort(_retired_investigation)
            elif (ctx.message or "").strip().startswith(("続き", "つづき", "続けて")):
                if _restore_investigation_ledger(_retired_investigation, st._investigation_dir, _tmp):
                    st._investigation_restored = True
                else:
                    # 復元が途中で失敗したターンでは、この run の退避（削除・置換）を一切行わず、元の退避台帳を保持する（次ターンの「続き」で再試行できる）。
                    st._investigation_retire_done = True
            else:
                _remove_dir_best_effort(_retired_investigation)
    # 復元直後・まだ何も探していない時点の状態を控える（`apply_unverified_downgrades` の降格対象から除く id）。
    st._investigation_pre_turn_not_found_ids = frozenset(
        item_id for item_id, item in
        investigation_ledger.load_ledger(st._investigation_dir).items.items()
        if item.get("status") == "not_found_in_scope")
    # 「続き」での追加の観点: 復元後の `_investigation_dir` から見直しを読み、義務（`investigation_ledger.pending_continuation_review()`）が残っていれば、Codex へ「これを新しい項目として調べてから答えて」と伝える（復元される台帳は完了扱いのため、これが無いとすぐ `final` を返してしまう）。
    # 復元が行われなかった・失敗したターンは `_investigation_dir` が空のままで `pending_continuation_review` は `None` を返し、注入しない側へ倒れる。
    _investigation_reviews_for_prompt = investigation_ledger.load_reviews(st._investigation_dir)
    _continuation_review_note = ""
    if (not _plain
            and (ctx.message or "").strip().startswith(("続き", "つづき", "続けて"))):
        _pending_review = investigation_ledger.pending_continuation_review(
            _investigation_reviews_for_prompt)
        # 義務が残っているかどうかは `pending_continuation_review()` だけを根拠に決める。このターンで `complete` を受理する前に義務の解決を要求するかどうか（`ledger_complete()` 呼び出し全てへ渡す・退避判断も同じ関数）。
        st._ledger_require_continuation_resolved = _pending_review is not None
        if _pending_review is not None:
            # 末尾の定型文（`_review_continuation_note`）と同じ `sanitize_review_text_list` を通す。
            _extras_text = investigation_ledger.sanitize_review_text_list(
                _pending_review.get("extra_perspectives"))
            if _extras_text:
                _continuation_review_note = (
                    f"前回の見直しで追加に調べられるとした観点: {_extras_text}。"
                    "これを新しい調査項目として ledger_manifest_set に足してから調べて"
                    "ください。" + _FINAL_ANSWER_VOICE)
    # personal_facts を注入したプロンプトを組む。
    _codex_msg = ctx.message
    if ctx.personal_facts:
        _codex_msg = (f"{ctx.message}\n\n"
                      f"【個人ファイル内ヒット（本人のみ・共有不可）】\n{ctx.personal_facts}")
    if _continuation_review_note:
        _codex_msg = f"{_codex_msg}\n\n【前回の続き】\n{_continuation_review_note}"
    def _build_prompt(with_history: bool) -> str:
        if _plain:
            return self._prompt_plain(_codex_msg, decision["lens"], ctx.world, with_history=with_history)
        return self._prompt_mcp(_codex_msg, decision["lens"], ctx.world,
                                direct_read=_direct_read_ok, layer=_layer, with_history=with_history)
    build_launch(self, ctx, st)
    log_turn_start(self, ctx, st)
    # 起動前の準備で決まった値を `st` へ渡す（以後の段と `_attempt` は `st` から読む）。
    st.ws_files, st._before_ws_files = ws_files, _before_ws_files
    st._direct_roots, st._deny_roots, st._sensitive_deny, st._direct_read_ok = (
        _direct_roots, _deny_roots, _sensitive_deny, _direct_read_ok)
    # resume が成立する初回の試行（`build_launch` がセッション非永続で `resume_sid` を落とした場合を除く）は、Codex 側のセッションが履歴を持つため前置しない。新規セッションで始めるとき・resume 失敗後の作り直し（`_resume_fallback`）は履歴つきのプロンプトを使う。
    st.prompt_with_history = _build_prompt(True)
    st.prompt = _build_prompt(False) if st.resume_sid else st.prompt_with_history
    return False
