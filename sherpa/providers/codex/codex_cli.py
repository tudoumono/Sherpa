"""Codex CLI 固有の処理（起動前のファイルの書き込み・codex.log の終了行・usage の反映・CODEX_HOME の後始末）。
設計: docs/design/codex.md「1ターンの流れ」
"""
from __future__ import annotations

import hashlib
import os
import shutil
import time

from ... import codex_agents_md, codex_skills, investigation_ledger
from ... import depth_profile as depth_profile_mod
from ..base import _log, _log_chat_usage, _log_codex, _node, _usage_meta
from . import sandbox
from .activity import exec_failure_counts as _codex_exec_failure_counts
from .activity import summarize_turn as _summarize_codex_activity
from .mcp import _mcp_config_args, _mcp_env
from .sandbox import (
    _codex_clean_env,
    _codex_sandbox_enabled,
    _kb_read_roots,
    _openai_endpoint_kind,
    _web_search_c_args,
    _web_search_endpoint_note,
    codex_multi_agent_enabled,
)
from .structured import _claims_vs_ledger
from .turn_candidates import _pick_structured_claims
from .turn_consts import _MCP_SIDECAR_NAME, _OUTPUT_SCHEMA_PATH, _OUTPUT_SCHEMA_PATH_V2, _REASONING_AUTHOR, _int_or_none
from ...env_int import env_int
from .usage import _CHILD_USAGE_KEYS, _collect_child_token_usage, _resolve_mcp_budget


def log_turn_end(ctx, st, env):
    """サブプロセスの後始末の後に、MCP 呼び出しの計測行と `codex.log` の終了行を 1 ターンにつき 1 回出す（本文・資料名は書かない）。"""
    uid = st.uid
    _mcp_calls = st._mcp_calls
    _event_type_counts = st._event_type_counts
    _schema_v2 = st._schema_v2
    _schema_on = st._schema_on
    _codex_run_started_at = st._codex_run_started_at
    # サブプロセス後始末の直後、以降のどの分岐（schema-era エラーの re-raise・ask_user の早期 return・通常終了）を通っても必ず1回だけ出す（MCP ツールの並走計測・「1実行あたり1行」を保つため早期 return より前に置く）。UI・env には載せない。
    _log.info("codex mcp calls: total=%d max_in_flight=%d conv=%s uid=%s",
              _mcp_calls["total"], _mcp_calls["max_in_flight"], ctx.conversation_id, uid)
    # codex.log 終了行（1実行1回・本文/資料名は書かない）。usage 合計は turn.completed の最新 snapshot（セッション累計であり足し算ではない）。
    _codex_usage_total_tokens = (
        (st.codex_usage.get("input_tokens", 0) + st.codex_usage.get("output_tokens", 0))
        if st.codex_usage else 0)
    # 本文・資料名・秘密は載せない（固定語彙のコードと件数・所要時間だけ）。トークン内訳は親（`codex_usage`）・子合算（`_child_usage_totals`）ともこのターンで確定済みの集計値をそのまま出す（cached は input に、reasoning_output は output に包含される内訳・二重計上しない）。
    # 台帳の終了状態は env["investigation"]["stopped_reason"] の4値より粗い3値（missing/complete/incomplete）。詳細（no_progress/cap 等）は env 側にだけ持つ。
    if not _schema_v2:
        _ledger_log_state = "off"  # 台帳を使わない構成（Codex(Ollama)・素の Codex 等）
    elif st._investigation_verdict is None or st._investigation_verdict.manifest_invalid:
        _ledger_log_state = "missing"
    elif st._investigation_verdict.complete:
        _ledger_log_state = "complete"
    else:
        _ledger_log_state = "incomplete"
    # claims_downgraded（末尾ログ用）: この行は `if answer:` 分岐（claims 確定）より前に出るため、ここでも `_claims_vs_ledger` を呼んで求める。根拠種別ゲートを経ていない生の confirmed 主張が対象のため、実際に envelope へ適用される件数の上限値になる。
    _claims_for_log = _pick_structured_claims(st) if _schema_on else []
    _, _log_ledger_check = _claims_vs_ledger(
        _claims_for_log,
        investigation_ledger.load_ledger(st._investigation_dir)
        if st._investigation_dir is not None
        else investigation_ledger.LedgerSnapshot(manifest=None, items={}, invalid_ids=()),
        # `_retire_investigation_ledger`/台帳ゲート本体と同じ「ファイル存在」基準（`load_ledger` の `manifest=None` だけでは「ファイル無し」と「内容不正」を区別できない）。
        manifest_file_exists=(
            (st._investigation_dir / "manifest.json").is_file()
            if st._investigation_dir is not None else False))
    _claims_downgraded_for_log = _log_ledger_check.get("downgraded", 0)
    # 確認できなかった item の件数（`unverified`/`not_found_in_scope`/`unreadable`/`unavailable`・降格適用後）を codex.log 終了行へ残す（本文・subject は含めず件数のみ）。
    _ledger_unconfirmed_for_log = (
        sum(st._investigation_verdict.terminal_counts.get(s, 0)
           for s in investigation_ledger.UNCONFIRMED_STATUSES)
        if st._investigation_verdict is not None else 0)
    # exec_failed/sandbox_failed（サンドボックスの実行中検知）。`env["activity"]` は直前の finally ブロックで確定済み（未確定＝要約自体が失敗したターンは `exec_failure_counts(None)` が (0, 0) を返す）。
    _exec_failed_for_log, _sandbox_failed_for_log = (
        _codex_exec_failure_counts(env.get("activity")))
    _log_codex.info(
        "end conv=%s uid=%s returncode=%s thread_id=%s events=%s mcp_calls=%d "
        "spawn_agents=%d usage_tokens=%d input=%d cached_input=%d output=%d "
        "reasoning_output=%d child_input=%d child_cached_input=%d child_output=%d "
        "child_reasoning_output=%d children_found=%d children_missing=%d "
        "error_code=%s clipped=%d budget_hit=%s elapsed=%.1fs ledger=%s continuations=%d "
        "ledger_unconfirmed=%d claims_downgraded=%d exec_failed=%d sandbox_failed=%d "
        "ledger_review_attempted=%s ledger_review_rounds=%d ledger_review_items_added=%d",
        ctx.conversation_id, uid, st.attempt_returncode, st.thread_id, _event_type_counts,
        _mcp_calls["total"], st._child_usage_detected, _codex_usage_total_tokens,
        (st.codex_usage.get("input_tokens", 0) if st.codex_usage else 0),
        (st.codex_usage.get("cached_input_tokens", 0) if st.codex_usage else 0),
        (st.codex_usage.get("output_tokens", 0) if st.codex_usage else 0),
        (st.codex_usage.get("reasoning_output_tokens", 0) if st.codex_usage else 0),
        st._child_usage_totals.get("input_tokens", 0),
        st._child_usage_totals.get("cached_input_tokens", 0),
        st._child_usage_totals.get("output_tokens", 0),
        st._child_usage_totals.get("reasoning_output_tokens", 0),
        st._child_usage_found, st._child_usage_missing,
        st._turn_failed_code, st._mcp_tool_result_clipped, st._mcp_total_budget_hit,
        time.monotonic() - _codex_run_started_at, _ledger_log_state, st._ledger_continuations,
        _ledger_unconfirmed_for_log, _claims_downgraded_for_log,
        _exec_failed_for_log, _sandbox_failed_for_log,
        st._ledger_review_attempted, st._ledger_review_rounds, st._ledger_review_items_added)
    if _sandbox_failed_for_log:
        # api.log（`_log`＝"sherpa" ロガー）へ一目で気付ける形で残す。本文・資料名は含めない（件数と会話IDのみ）。
        _log.warning(
            "Codex のサンドボックスでコマンドが失敗しています"
            "（make doctor の「Codex のサンドボックス」を確認）conv=%s 回数=%d",
            ctx.conversation_id, _sandbox_failed_for_log)


def apply_codex_usage(ctx, st, env):
    """`turn.completed` の usage（セッション累計）を env へ反映する。resume ターンは前ターンの累計との差分を `env["usage"]` にする。"""
    # turn.completed の usage は Codex CLI の契約でセッション累計（`codex exec resume` は前回までの累計を復元してから加算する）。累計値そのものは env["codex_usage_total"] に必ず残す（次ターンの差分計算の元・usage が取れなければ両方載せない）。resume が効いた（フォールバックしていない）ターンは、前ターンの累計（ctx.codex_usage_prev_total）との差分を answer.usage にする（session_id が今回の resume 先と一致する時だけ。新規セッション・フォールバック・prev 無し・session_id 不一致は累計をそのまま使う）。
    _usage_depth_extra = st._usage_depth_extra
    _turn_t0 = st._turn_t0
    codex_usage = st.codex_usage
    if codex_usage:
        env["codex_usage_total"] = {
            "session_id": st.thread_id,
            "input_tokens": codex_usage.get("input_tokens"),
            "cached_input_tokens": codex_usage.get("cached_input_tokens"),
            "output_tokens": codex_usage.get("output_tokens"),
            "reasoning_output_tokens": codex_usage.get("reasoning_output_tokens"),
        }
        _prev_total = ctx.codex_usage_prev_total
        if (st.resume_sid and not st._resume_fallback_happened and _prev_total
                and _prev_total.get("session_id") == st.resume_sid):
            env["usage"] = _usage_meta(
                "codex", codex_usage.get("model"),
                input_tokens=max(0, (codex_usage.get("input_tokens") or 0)
                                 - (_prev_total.get("input_tokens") or 0)),
                cached_input_tokens=max(0, (codex_usage.get("cached_input_tokens") or 0)
                                        - (_prev_total.get("cached_input_tokens") or 0)),
                output_tokens=max(0, (codex_usage.get("output_tokens") or 0)
                                  - (_prev_total.get("output_tokens") or 0)),
                reasoning_output_tokens=max(0, (codex_usage.get("reasoning_output_tokens") or 0)
                                           - (_prev_total.get("reasoning_output_tokens") or 0)),
                is_local=codex_usage.get("is_local"))
        else:
            env["usage"] = codex_usage
        # 差分計算／累計そのものの両方に同じ深さメタを載せる（`_usage_depth_extra` は `_reason`/`_base_reason` 確定時に計算済み）。
        env["usage"].update(_usage_depth_extra)
        # 子スレッド（`spawn_agent`）の usage を加算する。`env["codex_usage_total"]`（次ターンの差分計算の元）は親のスナップショットのまま変えない（子の usage は別系統の累計のため）。合算は `env["usage"]`（このターンの表示・計上値）だけに行い、内訳（親／子／未取得件数）を別途残す。multi_agent 無効では `_child_usage_found`/`_child_usage_missing` が両方 0 のままで素通りする。
        # 本体ターンは巡ごとの内訳を取れないため、巡別の `chat-round` は記録しない。親＋子の usage 合計だけをこのターンの正本として `env["usage"]`/`sherpa.usage` ログに残す。
        if st._child_usage_found or st._child_usage_missing:
            # 親分の内訳は `codex_usage`（セッション累計）ではなく `env["usage"]`（このターンの計上値）から作る（resume ターンでは `codex_usage` に前ターン分が混入するため）。
            _parent_only = {k: env["usage"].get(k) or 0 for k in _CHILD_USAGE_KEYS}
            env["codex_usage_children"] = {
                "found": st._child_usage_found, "missing": st._child_usage_missing,
                **st._child_usage_totals,
            }
            env["usage"]["codex_usage_breakdown"] = {
                "parent": _parent_only, "children": dict(st._child_usage_totals),
                "children_found": st._child_usage_found, "children_missing": st._child_usage_missing,
            }
            for _k in _CHILD_USAGE_KEYS:
                env["usage"][_k] = (env["usage"].get(_k) or 0) + st._child_usage_totals.get(_k, 0)
        # Codex 経路も `sherpa.usage` ログ 1 行（kind=chat・深さ・推論レベル付き）を出す。
        _log_chat_usage(env["usage"], time.monotonic() - _turn_t0, ctx.world)


def write_run_files(self, ctx, st):
    """AGENTS.md・skills・`config.toml` を書く（AGENTS.md と skills は fail-open・設定の書き込みは fail-closed）。Web 検索の制限の注記ノードを出す。"""
    _plain = st._plain
    run_dir = st.run_dir
    uid = st.uid
    users_dir = st.users_dir
    codex_home = st.codex_home
    _sidecar_path = st._sidecar_path
    _schema_on = st._schema_on
    _schema_v2 = st._schema_v2
    _direct_read_ok = st._direct_read_ok
    sp = st.sp
    _layer = st._layer
    _review_rounds = st._review_rounds
    _review_rounds_escalation = st._review_rounds_escalation
    _reason = st._reason
    _ask_disabled = st._ask_disabled
    _direct_roots = st._direct_roots
    _deny_roots = st._deny_roots
    _sensitive_deny = st._sensitive_deny
    _mcp_budget_env = st._mcp_budget_env
    # AGENTS.md はベストエフォート（書けなくても Codex 実行は継続・fail-open）。気づけるよう warning は残す（containment/grounding の短縮形はプロンプトにも置いてある）。
    try:
        if _plain:
            codex_agents_md.write_agents_md(run_dir, plain=True)
        else:
            codex_agents_md.write_agents_md(run_dir, output_schema=_schema_on,
                                            direct_read=_direct_read_ok,
                                            output_schema_v2=_schema_v2,
                                            multi_agent=st._multi_agent_enabled,
                                            review_rounds=_review_rounds,
                                            layer=_layer,
                                            review_rounds_escalation=_review_rounds_escalation,
                                            source_required=bool(st._ledger_required_extra))
    except Exception as e:
        _log.warning("AGENTS.md write failed (fail-open, prompt still has containment): %s", e)
    # スキル配備もベストエフォート（fail-open）。knowledge=ON の Codex 実行全部で配備する（author に限定しない）。plain は investigate-* スキルを置かない（xlsx/docx/pptx/marp は配備する）。
    try:
        codex_skills.deploy_skills(run_dir, uid, users_dir,
                                   skip_prefix="investigate-" if _plain else None)
    except Exception as e:
        _log.warning("skills deploy failed (fail-open): %s", e)
    # profile config はここで書く（FileExistsError 等は fail-closed で例外→answer=None→CODEX_HOME 削除→決定的回答）。marp/Chromium を read root に足す必要は無い（レンダは Sherpa 本体側）。
    # 会話ごとの CODEX_HOME は毎ターン再利用するため、前ターンの config.toml（creds を含む）の残骸が無いことを確認してから書く。
    if codex_home is not None:
        try:
            (codex_home / "config.toml").unlink(missing_ok=True)
        except Exception:
            pass
        # 永続 CODEX_HOME は毎ターン再利用するため、前ターンのサイドカー残骸を吸収しないよう config.toml と同じくここで空から始める（`missing_ok=True`）。unlink が失敗したら、このターンは `_absorb_mcp_sidecar` を無効のままにする（`_sidecar_init_ok` を立てない）。本文・パスは伏せ、例外型と errno だけ warning に残す。
        try:
            _sidecar_path.unlink(missing_ok=True)
            _sidecar_unlink_ok = True
        except Exception as e:
            _sidecar_unlink_ok = False
            _log.warning(
                "mcp sidecar pre-unlink failed (sidecar absorb disabled this turn): %s errno=%s",
                type(e).__name__, getattr(e, "errno", None))
        sandbox._write_codex_authoring_config(
            codex_home, _kb_read_roots(ctx.world), _reason,
            True, ctx.world, sp, self._web_search, _ask_disabled,
            ollama_base_url=self._ollama_base_url, system_settings=self._system_settings,
            layer=_layer, direct_read_roots=_direct_roots, sensitive_deny=_sensitive_deny,
            sidecar_path=str(_sidecar_path),
            deny_roots=_deny_roots,
            multi_agent=st._multi_agent_enabled, orchestrator_model=self.model,
            extra_mcp_env=_mcp_budget_env)
        # ここまで（事前 unlink・設定生成）が両方例外なく終わって初めて、このターンのサイドカー吸収を許可する。
        if _sidecar_unlink_ok:
            st._sidecar_init_ok = True
        # 接続先が Azure 等へリダイレクトされて web_search が強制 OFF になっている時だけ、理由を1回（このターンにつき1回）伝える。Codex(Ollama) 構成は対象外。
        if self._ollama_base_url is None:
            _ws_note = _web_search_endpoint_note(
                self._web_search, _openai_endpoint_kind(self._system_settings),
                self._system_settings)
            if _ws_note:
                yield _node("web_search_endpoint", "think", "Web検索の制限", _ws_note, "done")


def release_codex_home(ctx, st, env):
    """子の usage を集め、利用統計 activity を要約してから、CODEX_HOME を後始末する（永続セッションは `sessions/` を残し、使い捨ては削除）。"""
    codex_home = st.codex_home
    _sidecar_path = st._sidecar_path
    _all_parent_thread_ids = st._all_parent_thread_ids
    _child_thread_ids = st._child_thread_ids
    _turn_started_wall = st._turn_started_wall
    _activity_settings = st._activity_settings
    # 利用統計 activity の対象親（このターンで使った全ての親・resume 失敗のフォールバックで切り替わった分も含む）。現在の `thread_id` が記録漏れで欠けていない保険としてここでも足す。
    _activity_parent_ids = list(_all_parent_thread_ids)
    if st.thread_id and st.thread_id not in _activity_parent_ids:
        _activity_parent_ids.append(st.thread_id)
    if _child_thread_ids or st.thread_id or _activity_parent_ids:
        # 子の session JSONL（`sessions/**`）は非永続セッションだと直後の rmtree で消えるため、削除より前に読む。読めなくても fail-open。`thread_id`（今回の親）だけでも呼ぶ（`_child_thread_ids` が空でも `parent_thread_id` 突合で子を拾える）。
        try:
            (st._child_usage_totals, st._child_usage_found, st._child_usage_missing,
             st._child_usage_detected) = (
                _collect_child_token_usage(codex_home, _child_thread_ids, st.thread_id,
                                           _turn_started_wall))
        except Exception:
            pass
        # 利用統計 activity: codex_home 削除より前に、同じ fail-open 方針で要約する（読めなくても本体ターンは落とさず、型名/errno だけ warning ログに出す）。settings は開始行の直後で確定済みの値を渡す。prepare/agent はここで初めて確定する。prepare は `ctx.turn_started_mono`／`_agent_start_mono` のどちらかが無ければキー自体を置かない（0埋めしない）。
        try:
            _activity_agent_ms = (
                max(0, round((st._agent_end_mono - st._agent_start_mono) * 1000))
                if (st._agent_start_mono is not None and st._agent_end_mono is not None)
                else 0)
            _activity_phases_ms = {"agent": _activity_agent_ms}
            if ctx.turn_started_mono is not None and st._agent_start_mono is not None:
                _activity_phases_ms["prepare"] = max(
                    0, round((st._agent_start_mono - ctx.turn_started_mono) * 1000))
            env["activity"] = _summarize_codex_activity(
                codex_home, parent_thread_ids=_activity_parent_ids,
                child_thread_ids=_child_thread_ids,
                turn_started_wall=_turn_started_wall, settings=_activity_settings,
                phases_ms=_activity_phases_ms)
        except Exception as _act_exc:
            _log.warning("codex activity summarize failed: %s errno=%s",
                        type(_act_exc).__name__, getattr(_act_exc, "errno", None))
    if st._persist_session:
        # セッション実体（`sessions/` の JSONL）は次ターンの resume のために保持する。creds を含む config.toml・`auth.json`（実 `~/.codex/auth.json` への symlink）・サイドカーは毎ターン削除する（次ターンは再作成する・前ターンの読取記録を持ち越さない）。
        try:
            (codex_home / "config.toml").unlink(missing_ok=True)
        except Exception:
            pass
        try:
            (codex_home / "auth.json").unlink(missing_ok=True)
        except Exception:
            pass
        try:
            _sidecar_path.unlink(missing_ok=True)
        except Exception:
            pass
    else:
        # per-request CODEX_HOME（profile＋auth symlink）を後始末する（symlink の指す先は消えない）。サイドカーはこの codex_home 配下なので rmtree で併せて消える。
        try:
            shutil.rmtree(codex_home, ignore_errors=True)
        except Exception:
            pass


def resolve_reasoning(self, ctx, st, decision):
    """推論の強さ（minimal は low へ）・multi_agent の可否・見直しの回数を決めて `st` へ置く。"""
    _plain = st._plain
    # reasoning=minimal は image_gen/web_search と非互換で API 400 になるため low へ引き上げる。author（作成）は専用の `_REASONING_AUTHOR` を使う。通常レンズは現行のまま。
    _is_author = decision["lens"] == "author"
    # 調べる深さ: 通常レンズの基準値だけ管理画面の基準値編集（system_settings）を反映する。クイックだけ `codex_reasoning_for` が1段下げ、標準以上と author は基準値のまま `codex exec` へ渡す。
    _base_reason = (_REASONING_AUTHOR if _is_author
                   else depth_profile_mod.effective_base(
                       self._system_settings, "codex_reasoning", self._reason))
    # author は専用の推論設定を持ち、クイックの1段下げも通さない。素の Codex（plain）は「調べる深さ」を効かせない（基準値のまま）。
    _reason_raw = _base_reason if (_is_author or _plain) else depth_profile_mod.codex_reasoning_for(
        _base_reason, (ctx.scope_meta or {}).get("depth_profile"))
    _reason = "low" if str(_reason_raw).lower() == "minimal" else _reason_raw
    # usage メタへ足す「実際に codex exec へ渡した model_reasoning_effort」（`_reason`）と基準値（minimal→low の丸め前）（`_base_reason`）。一致なら `reasoning_base` は省略する（`usage_reasoning_extras` の契約）。
    _usage_depth_extra = depth_profile_mod.usage_reasoning_extras(
        (ctx.scope_meta or {}).get("depth_profile"), _base_reason, _reason)
    # multi_agent は既定で常時有効にする（深さに関わらず）。判定は `codex_multi_agent_enabled`（sandbox.py・唯一の真実源）に委ねる: サンドボックス無効は対象外、既定 OpenAI・Azure・Ollama は常に対象、独自エンドポイント（custom）は `codex_worker_model` の明示設定が無いと対象外。
    # `_review_rounds` は AGENTS.md へ埋め込む見直しの回数（クイック 0／標準 2／深く 4／最大は管理画面の設定値で頭打ち）。Codex 自身にこの回数は強制されない（指示のみ）。
    # plain では下調べ役・見直し役を使わず、`codex_multi_agent_enabled` に関わらず常に偽。
    st._multi_agent_enabled = False if _plain else codex_multi_agent_enabled(
        ollama_base_url=self._ollama_base_url, system_settings=self._system_settings)
    _review_rounds = 0 if _plain else depth_profile_mod.review_rounds_for(
        (ctx.scope_meta or {}).get("depth_profile"), self._system_settings)
    # 共通上限（管理画面の設定値）に余地があるターンだけ、本体の自己判断による見直し追加1回を AGENTS.md で許可する（`_review_rounds` は `review_rounds_for` が上限で頭打ち済み）。
    _review_rounds_escalation = (not _plain) and _review_rounds < depth_profile_mod.effective_max_review_rounds(
        self._system_settings)
    st._reason, st._usage_depth_extra = _reason, _usage_depth_extra
    st._review_rounds, st._review_rounds_escalation = _review_rounds, _review_rounds_escalation


def build_launch(self, ctx, st):
    """`codex exec` の起動引数・環境変数・`-o` の出力先・サイドカーの置き場・出力スキーマを決めて `st` へ置く。"""
    _plain = st._plain
    sp = st.sp
    _layer = st._layer
    _ask_disabled = st._ask_disabled
    _tmp = st._tmp
    run_dir = st.run_dir
    users_dir = st.users_dir
    uid = st.uid
    _reason = st._reason
    _last_message_path = _tmp / f"last-message-{hashlib.sha1(os.urandom(8)).hexdigest()[:12]}.txt"
    # MCP ツール結果の予算・grep ヒット上限・読み取り窓を親側で1回だけ解決し、子プロセスの env として渡す（`mcp_server.py::_env_budget_bytes` が最優先で読む）。
    _mcp_budget_env: dict[str, str]
    _mcp_budget_env = _resolve_mcp_budget(
        self._system_settings,
        None if _plain else (ctx.scope_meta or {}).get("depth_profile"))
    if _plain:
        # 素の Codex モード: MCP サーバへツールの絞り込みを渡す。台帳のディレクトリ・required_extra は渡さない（台帳ツール自体を出さない）。
        _mcp_budget_env["SHERPA_MCP_TOOLSET"] = "plain"
    else:
        # 解決済み（realpath）のパスを渡す（MCP サーバ側が書込のたびに「書込先の経路に symlink が無い」ことを検査するため）。
        _mcp_budget_env["SHERPA_MCP_LEDGER_DIR"] = os.path.realpath(st._investigation_dir)
        # `ledger_status`（mcp_server.py）の自己確認も provider 側のゲートと同じ required_extra を見られるようにする（ターンの最初に決めた値・カンマ区切り・空なら空文字列）。
        _mcp_budget_env["SHERPA_MCP_LEDGER_REQUIRED_EXTRA"] = ",".join(st._ledger_required_extra)
    codex_home = None
    # サイドカーの置き場（`_sidecar_path`）は、本サーバ側は書けるが model-shell からは書込許可外の場所に決める。両分岐の中で確定し、`_absorb_mcp_sidecar` のガードはこの変数と `_sidecar_init_ok` を見る。既定は非サンドボックス経路の置き場（サンドボックス有効時のみ codex_home 配下へ差し替える）。
    _sidecar_path = _tmp / _MCP_SIDECAR_NAME
    if _codex_sandbox_enabled():
        # permission profile で読取を KB(RO)＋authoring(RW) に封じ込め＋env 洗浄する。CODEX_HOME は authoring の外（workspace 直下・`:root=deny` で shell から不可視）。
        # conversation_id があるターンは会話ごとの固定ディレクトリ（`workspace/.codex-sessions/{cid}`）を CODEX_HOME にして毎ターン再利用する（`sessions/` 配下の JSONL が resume の実体＝finally では削除しない）。無い場合は per-request 使い捨て（実行後 rmtree・`--ephemeral`）。
        # `_safe_persistent_codex_home` は外側で検証済み（ここで再計算しない）。ここに来た時点で `_session_persistence_enabled` かつ `_codex_home_ok` は保証済み。
        if st._session_persistence_enabled:
            codex_home = st._safe_persistent_codex_home
        else:
            _rand = hashlib.sha1(os.urandom(8)).hexdigest()[:12]
            codex_home = users_dir / uid / "workspace" / f".codexhome-{_rand}"
        # codex_home（`:root deny`＝model-shell から不可視・MCP サーバは別プロセスなので書ける）配下に置く。run_dir 直下だと Codex の shell ツールが偽の `{"kind":"read",...}`/`{"kind":"ask_user",...}` 行を追記でき、未読資料を根拠ゲートへ通したり任意の確認カードでターンを潰せてしまう。
        _sidecar_path = codex_home / _MCP_SIDECAR_NAME
        argv_base = ["codex", "exec", "--json", "--strict-config", "--skip-git-repo-check",
                    "-o", str(_last_message_path),
                    "-C", str(run_dir), "-m", self.model,
                    "-c", f"model_reasoning_effort={_reason}"]
        # 管理者環境の既定値に依存させず明示指定する。無効時（`codex_worker_model` 未設定の独自エンドポイント等）も明示的に false にする（`[agents.*]` の層が無いまま機能だけ有効になるのを防ぐ）。
        argv_base += ["-c", f"features.multi_agent={'true' if st._multi_agent_enabled else 'false'}"]
        if not st._session_persistence_enabled:
            argv_base.append("--ephemeral")
        # `self._openai_api_key` は Codex(OpenAI) 構成で接続先が Azure 等の時だけ `_select_provider` が渡す（それ以外は常に None＝env に渡さない）。
        popen_env = _codex_clean_env(codex_home, _tmp, openai_api_key=self._openai_api_key)
    else:
        # フォールバック（SHERPA_CODEX_SANDBOX=0）＝`-s workspace-write`（読取全開・多層防御は OS ユーザ分離に依存）。resume 非対応（常に使い捨て）で、ここで捕捉する thread_id は env に載らない。
        st.resume_sid = None
        argv_base = ["codex", "exec", "--json", "--skip-git-repo-check",
                    "--ephemeral", "-o", str(_last_message_path),
                    "-s", "workspace-write", "-C", str(run_dir),
                    "-m", self.model, "-c", f"model_reasoning_effort={_reason}"]
        # このフォールバック経路は常にサンドボックス無効（`_multi_agent_enabled` は常に偽）。config.toml を書かないため `[agents.*]` の層が無く、明示的に無効化する。
        argv_base += ["-c", "features.multi_agent=false"]
        # `--strict-config` が無い経路（config.toml でなく -c）なので同等をここで足す。
        argv_base += _web_search_c_args(self._web_search, self._system_settings)
        # `.tmp/`（run_dir 配下）はこの経路では model-shell からも見える（封じ込めが無い前提の経路）。`_sidecar_append` が書く内容は doc_id／ツール名／種別／時刻（と ask_user の質問）だけで本文は書かないが、shell が偽の行を追記できる可能性は残る。
        _sidecar_env = {"SHERPA_MCP_SIDECAR": str(_sidecar_path)}
        argv_base += _mcp_config_args(ctx.world, sp, _ask_disabled, layer=_layer,
                                      extra_env={**_mcp_budget_env, **_sidecar_env})
        popen_env = {**os.environ, **_mcp_env(ctx.world, sp, _ask_disabled, layer=_layer),
                    **_mcp_budget_env, **_sidecar_env}
        # `.tmp/` は run_dir 生成のたびに空から始まるため、前ターン残骸の事前 unlink は不要。ここで吸収を許可してよい。
        st._sidecar_init_ok = True
    # 出力スキーマ: OpenAI 系構成のみ（Codex(Ollama) は対象外）。`SHERPA_CODEX_OUTPUT_SCHEMA=0` で無効化・`=1` で v1。既定は v2（`claims` 付き）。
    # plain は env の値に関わらず常に 0（平文の回答・`_pick_codex_headline` を使う）。台帳ゲート（`_schema_v2 and ...`）・claims の格下げ判定は `_schema_v2` が偽になることで無効になる。
    _schema_level = 0 if _plain else env_int("SHERPA_CODEX_OUTPUT_SCHEMA", 2, 0, 2)
    _schema_on = self._ollama_base_url is None and _schema_level >= 1
    _schema_v2 = _schema_on and _schema_level == 2
    # ログ・利用統計には実際に効いている値を出す（Codex(Ollama) は出力スキーマを使わない＝0）。
    _schema_level_effective = 2 if _schema_v2 else (1 if _schema_on else 0)
    if _schema_on:
        argv_base += ["--output-schema",
                     str(_OUTPUT_SCHEMA_PATH_V2 if _schema_v2 else _OUTPUT_SCHEMA_PATH)]
    # 中間の見直し（`ledger_review_put`）を完了判定へ必須にするのは Codex の標準モード（`_schema_v2`）だけ。素の Codex・Codex(Ollama) は対象外。このターンの台帳ゲート呼び出し全て（while ループ・退避・最終判定）へ同じ値を渡す。`ledger_status`（MCP の自己確認）にも同じ値を env で伝える（`SHERPA_CODEX_SANDBOX=0` の経路だけは env が乗らず自己確認がわずかに楽観的になるが、実際のゲートは `_ledger_require_review` を直接使う）。
    st._ledger_require_review = _schema_v2
    if not _plain:
        _mcp_budget_env["SHERPA_MCP_LEDGER_REQUIRE_REVIEW"] = "1" if st._ledger_require_review else "0"
        # `ledger_status` の自己確認にも同じ義務フラグを伝える。
        _mcp_budget_env["SHERPA_MCP_LEDGER_REQUIRE_CONTINUATION_RESOLVED"] = (
            "1" if st._ledger_require_continuation_resolved else "0")
    st._last_message_path, st._mcp_budget_env = _last_message_path, _mcp_budget_env
    st.codex_home, st._sidecar_path, st.argv_base, st.popen_env = codex_home, _sidecar_path, argv_base, popen_env
    st._schema_on, st._schema_v2, st._schema_level_effective = _schema_on, _schema_v2, _schema_level_effective


def log_turn_start(self, ctx, st):
    """開始ログ（`codex.log`）と、利用統計 activity.settings の値を決めて `st` へ置く。"""
    _plain = st._plain
    uid = st.uid
    _review_rounds = st._review_rounds
    _schema_level_effective = st._schema_level_effective
    _reason = st._reason
    _mcp_budget_env = st._mcp_budget_env
    _codex_run_started_at = time.monotonic()
    # 子 rollout の mtime 下限（壁時計）。`_collect_child_token_usage` の新形式判定はこれより前のファイルを前ターンの子として除外する。
    _turn_started_wall = time.time()
    _codex_config_kind = "ollama" if self._ollama_base_url is not None \
        else _openai_endpoint_kind(self._system_settings)
    _depth_label = (ctx.scope_meta or {}).get("depth_profile") or "standard"
    # 予算/上限の各値は `_mcp_budget_env`（`_resolve_mcp_budget` が1回だけ解決した実効値）からそのまま読む（ログ用に別計算しない）。`budget_total`／`max_calls`／`window_source`／`window_cli` は行の形（キー名）だけ残し、値は常に "none"（統計側がキー名を読むため）。
    _codex_mode_label = "plain" if _plain else "standard"
    _log_codex.info(
        "start conv=%s uid=%s mode=%s config=%s multi_agent=%s depth=%s review_rounds=%s "
        "schema_level=%s model=%s reasoning=%s budget_per_result=%s budget_total=%s "
        "max_hits=%s window_cap=%s max_calls=%s window_source=%s window_cli=%s",
        ctx.conversation_id, uid, _codex_mode_label, _codex_config_kind, st._multi_agent_enabled,
        _depth_label, _review_rounds, _schema_level_effective, self.model, _reason,
        _mcp_budget_env.get("SHERPA_MCP_TOOL_BUDGET_BYTES", "-"),
        "none",
        _mcp_budget_env.get("SHERPA_MCP_TOOL_MAX_HITS", "-"),
        _mcp_budget_env.get("SHERPA_MCP_TOOL_WINDOW_CAP", "-"),
        "none",
        "none",
        "none")
    # 利用統計 activity.settings: 開始行と同じ実行時解決済み値をそのまま使う（"-" は取れない値として None）。
    _activity_settings = {
        "provider": "codex", "mode": _codex_mode_label, "config": _codex_config_kind,
        "model": self.model,
        "reasoning": _reason, "depth": _depth_label, "review_rounds": _review_rounds,
        "schema_level": _schema_level_effective, "multi_agent": st._multi_agent_enabled,
        "budget_per_result": _int_or_none(_mcp_budget_env.get("SHERPA_MCP_TOOL_BUDGET_BYTES")),
        "max_hits": _int_or_none(_mcp_budget_env.get("SHERPA_MCP_TOOL_MAX_HITS")),
        "window_cap": _int_or_none(_mcp_budget_env.get("SHERPA_MCP_TOOL_WINDOW_CAP")),
    }
    st._codex_run_started_at, st._turn_started_wall, st._activity_settings = (
        _codex_run_started_at, _turn_started_wall, _activity_settings)
