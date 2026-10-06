"""Codex の 1 ターンの状態（`CodexTurnState`）。段をまたいで読み書きする値を 1 つのオブジェクトに集める。
属性名は `_run_authoring` の元のローカル変数名と同じ（先頭の `_` も含む）。`__slots__` を持つため、綴りの誤りの代入は例外になる。
設計: docs/design/codex.md「1ターンの流れ」
"""
from __future__ import annotations

import re
import uuid

from ...env_int import env_int
from .usage import _CHILD_USAGE_KEYS

TOOL_USE_USED = "used"
TOOL_USE_UNUSED = "unused"
TOOL_USE_UNDETERMINED = "undetermined"


class TurnToolUse:
    """1 ターン（初回・同じターンの全 resume／自動の続き・resume 失敗後の新しいセッション）を通した道具の使用の合算。
    実行ごとの値（`_attempt_ran_tools` など）とは別に持ち、次の利用者のターンで作り直す（`CodexTurnState` と同じ寿命）。
    設計: docs/proposals/2026-10-07-調べ方と下調べの整理.md 決定 15
    """
    __slots__ = ("parent_shell_events", "parent_sherpa_events", "child_spawned", "record_unreliable")

    def __init__(self):
        self.parent_shell_events = 0  # 親の実行の記録に現れたシェルのコマンドのイベント数
        self.parent_sherpa_events = 0  # 親の実行の記録に現れた Sherpa の道具（MCP）のイベント数
        self.child_spawned = False  # 子のエージェントを起動した（子のシェルは親の記録に残らない）
        self.record_unreliable = False  # 実行の記録を最後まで読めなかった

    def verdict(self) -> str:
        """親が使っていれば used・使っていなくても子の起動か記録の欠けがあれば undetermined（「使っていない」と断定しない）・それ以外は unused。"""
        if self.parent_shell_events or self.parent_sherpa_events:
            return TOOL_USE_USED
        if self.child_spawned or self.record_unreliable:
            return TOOL_USE_UNDETERMINED
        return TOOL_USE_UNUSED


class CodexTurnState:
    __slots__ = (
        # ターンの外側（`_run_authoring` の先頭）
        "turn_uid", "_tool_use", "_call_log_dir", "_parent_mcp_tools", "call_log",
        "_tool_zero_nudged", "_tool_zero_notice",
        "_turn_t0", "_plain", "_skip_presearch", "users_dir", "uid", "run_dir",
        "answer", "ran", "_codex_silent_failure", "_codex_stopped_early", "_stream_error",
        "codex_question", "codex_usage", "_agent_start_mono", "_agent_end_mono",
        "_wall_clock_limit_s", "_wall_clock_state", "_resume_fallback_happened", "_sidecar_init_ok",
        "_graph_schema_era_error", "_mcp_error_codes", "_mcp_tool_result_clipped", "_mcp_total_budget_hit",
        "_mcp_duplicate_tool_call", "_mcp_search_truncated", "_mcp_tool_calls_exhausted", "_mcp_coverage_write_failed",
        "_ask_disabled",
        "mcp_neighbors", "mcp_graph_results", "_mcp_read_docs", "_mcp_listed_docs", "_mcp_calls", "_event_type_counts",
        "_child_thread_ids", "_all_parent_thread_ids", "_child_usage_totals", "_child_usage_found",
        "_child_usage_missing", "_child_usage_detected", "codex_created_files",
        "_created_file_rows", "_created_files_failed", "_discarded_files", "_marp_failed", "_marp_failed_formats", "_record_notes",
        # 会話ロック・調査台帳
        "_conv_lock", "_conv_lock_acquired", "_investigation_dir", "_ledger_home", "_ledger_required_extra", "_scope_source_state",
        "_ledger_require_review", "_ledger_require_continuation_resolved", "_investigation_restored",
        "_investigation_verdict", "_investigation_snapshot", "_investigation_reviews",
        "_review_continuation_note_text", "_investigation_record_payload",
        "_investigation_pre_turn_not_found_ids", "_ledger_continuations", "_ledger_review_requests",
        "_ledger_review_rounds", "_ledger_review_items_added", "_ledger_review_attempted",
        "_ledger_review_pre_candidate", "_ledger_review_pre_len", "_ledger_review_pre_valid_from",
        "_ledger_review_pre_turn_failed", "_ledger_review_pre_turn_failed_code",
        "_ledger_review_pre_attempt_msgs_start", "_ledger_review_pre_latest_structured",
        "_ledger_review_pre_msgs_len",
        "_ledger_review_pre_codex_question", "_ledger_review_reverted", "_investigation_stopped_reason",
        "_investigation_retire_done",
        # 会話の継続と起動前の準備
        "_persist_session", "resume_sid", "thread_id", "_session_persistence_enabled",
        "_safe_persistent_codex_home", "_codex_home_ok", "_auto_continue_count", "_multi_agent_enabled",
        "_agent_msgs", "_agent_partial", "_latest_structured", "_structured_answers",
        "_structured_answers_valid_from", "sp", "_layer", "ws_files", "_before_ws_files",
        "_reason", "_usage_depth_extra", "_review_rounds", "_review_rounds_escalation",
        "_direct_roots", "_deny_roots", "_sensitive_deny", "_direct_read_ok", "_tmp", "prompt", "prompt_with_history",
        "_last_message_path", "_mcp_budget_env", "codex_home", "_sidecar_path", "argv_base", "popen_env",
        "_schema_on", "_schema_v2", "_schema_level_effective", "_codex_run_started_at", "_turn_started_wall",
        "_activity_settings",
        # `_attempt` ごとの状態
        "got_any_line", "attempt_returncode", "_attempt_ran_tools", "_attempt_no", "_attempt_msgs_start",
        "_stale_last_message", "_turn_failed", "_turn_failed_code",
        "_answer_notices", "_invalid_output_events", "_trimmed",
    )

    def __init__(self, ctx, *, turn_t0: float, plain: bool, skip_presearch: bool):
        self._turn_t0 = turn_t0  # `sherpa.usage` ログ 1 行の elapsed（このターン全体）
        # 利用者の 1 ターンの ID（MCP へ渡す）と、ターンを通した道具の使用の合算。何回目の実行かは `_attempt_no`（ターン内の通し番号）。
        self.turn_uid = uuid.uuid4().hex
        self._tool_use = TurnToolUse()
        # 道具の呼び出しの記録: プロセスごとのファイルの置き場・親の `--json` の mcp_tool_call の道具名の並び（実行ごと）・ターンの終わりにまとめた結果（`tool_call_log.MergedCallLog`）。
        self._call_log_dir = None
        self._parent_mcp_tools: dict[int, list[str]] = {}
        self.call_log = None
        self._tool_zero_nudged = False  # 道具ゼロの促しを出したか（1 ターン 1 回まで）
        self._tool_zero_notice = False  # 促した後も道具ゼロだった（注記「資料を調べずに答えています」を付ける）
        self._plain = plain  # 素の Codex モード（ターンの最初に 1 回だけ決める）
        self._skip_presearch = skip_presearch
        self.users_dir = None
        self.uid = None
        self.run_dir = None
        self.answer, self.ran = None, False
        # 回答の注記（文字列、または (kind, 文) の組）。仕上げで `answer.notices` へ移す。
        self._answer_notices: list = []
        # 本文から外した前置き・末尾の作業宣言（内部記録・`answer.trimmed`）。
        self._trimmed: list[dict] = []
        self._invalid_output_events = 0
        # 「CLI はあるが認証が無い」と codex exec は即座に非ゼロ終了し JSON を1行も出さない。起動前ガードで一度も起動していないケースと区別するため、if ブロック内でだけ True にする（スキップされた経路は False のまま＝既存の決定的回答フォールバック）。
        self._codex_silent_failure = False
        # 自動継続の `env["codex_stopped_early"]` 判定用フラグも既定 False（`_agent_msgs` 等は if ブロック内にしか無い）。
        self._codex_stopped_early = False
        # Popen を試みていない経路（`ws_authoring`/`run_dir` が None・shutil.which 不在等）は技術的失敗ではないため既定 False（第3分岐で参照するためここで定義する）。
        self._stream_error = False
        self.codex_question = None  # ask_user 由来の question（出たら env/_result を出さずターン終了）
        self.codex_usage = None  # turn.completed の usage（best-effort・出なければ None）
        # 利用統計 activity: Codex 専用区間の開始/終了（`time.monotonic()`）。開始は最初の `subprocess.Popen` 直前（1回だけ）、終了は直近の `proc.wait()` 直後（attempt ごとに更新）。準備や後処理を含めない。
        self._agent_start_mono: float | None = None
        self._agent_end_mono: float | None = None
        # 1ターン全体（自動継続・台帳継続を含む）の壁時計上限（既定90分・0=無制限・`SHERPA_CODEX_WALL_CLOCK_LIMIT_S`）。`_agent_start_mono` を起点にし、継続 attempt も同じ起点からの残り時間で打ち切る。
        self._wall_clock_limit_s = env_int("SHERPA_CODEX_WALL_CLOCK_LIMIT_S", 90 * 60, 0, 24 * 3600)
        self._wall_clock_state = {"hit": False}  # いずれかの attempt が上限で打ち切られたら True（attempt をまたいで保持）
        # resume 試行が失敗し新規セッションへ切り替わったら True（usage のターン差分判定に使う）。
        self._resume_fallback_happened = False
        # サイドカーの吸収を許可してよいかの唯一のゲート。sandbox 有効時は事前 unlink・設定生成が両方成功した時、非サンドボックス経路は env 配線が済んだ時点で立てる（`_absorb_mcp_sidecar` 参照）。
        self._sidecar_init_ok = False
        # `graph_neighbors`・`graph_resolve`・`graph_impact` の mcp_tool_call item が旧世代グラフの構造化エラー（`_graph_schema_era_from_item`）を運んできたら捕まえる。調査は止めない（Codex は grep/原本読取ツールを使える）。「縮退した」という印として、終了後の env に冒頭告知と統計フラグを載せる。
        self._graph_schema_era_error = None
        # 子（worker/evaluator）がサイドカー経由で報告した障害コード（`mcp_server._SIDECAR_ERROR_CODES`）。
        self._mcp_error_codes: list = []
        # MCP ツール結果のバイト予算（`mcp_server.py` の `{"kind":"limit",...}`）: 1件あたりのクリップ件数（累算）と、累計予算到達（bool）。`env["limits"]` へ合流させる。
        self._mcp_tool_result_clipped = 0
        self._mcp_total_budget_hit = False
        # 同一クエリの重複実行の抑止（`field: "duplicate_tool_call"`）の件数。`env["limits"]` へ合流させる。
        self._mcp_duplicate_tool_call = 0
        # `run_tool()` 自身の内部切り詰め（`field: "search_truncated"`）の件数。`env["limits"]["search_truncated"]` へ合流させる。
        self._mcp_search_truncated = 0
        # ツール呼び出し回数の上限到達（`field: "tool_calls_exhausted"`）。bool・一度立てば真のまま。`env["limits"]` へ載せる。
        self._mcp_tool_calls_exhausted = False
        # 台帳の coverage.jsonl へ書けなかった回数（子がサイドカーで報告・回答の注記に出す）。
        self._mcp_coverage_write_failed = 0
        # 確認ID 付き再送（前の質問への回答）では ask_user を無視する（再質問ループ防止）。
        self._ask_disabled = bool(re.search(r"確認ID[:：]", ctx.message or ""))
        self.mcp_neighbors: list = []  # Codex が graph_neighbors で引いた近傍（UI カードに反映）
        self.mcp_graph_results: list = []  # graph_resolve／graph_impact の結果の要約（`mcp._graph_tool_summary`・思考ノードの補足と同じ文）
        # MCP の read 系ツール（read_doc/read_around/doc_outline/compare_documents）の引数から集めた doc_id。attempt をまたいで合算し、最終 answer の「参照した資料:」の解析結果に合流させ、機械検証してから env["sources"] へ足す。
        self._mcp_read_docs: list = []
        # `xlsx_sheets`（シート一覧のみ）は上と分けて集める。sources には合流させるが、根拠ゲート（sources_verified）には数えない。
        self._mcp_listed_docs: list = []
        # MCP ツール呼び出しの並走計測（run 全体の合算値）。item id は codex exec プロセスごとに振り直されるため、id の集合（`seen`/`open`）は `_attempt` 内のローカル変数として作り直し、attempt 終了時（finally）にこの dict へ合算する。attempt は逐次実行のため、`max_in_flight` は attempt ごとの最大値の最大を取る。
        self._mcp_calls = {"total": 0, "max_in_flight": 0}
        # `codex.log` 向け: `--json` イベントの種類別件数（attempt をまたいで合算・観測専用）。
        self._event_type_counts: dict[str, int] = {}
        # `spawn_agent` した子スレッドの id（`collab_tool_call` item から捕捉・run 全体で合算する set）。multi_agent 無効では常に空。子検出の片方（旧形式）にすぎず、`_collect_child_token_usage` は親の `thread_id` があれば `parent_thread_id` 突合（新形式）でも子を見つける。
        self._child_thread_ids: set = set()
        # 利用統計 activity: このターンで「親」として使った thread_id を時刻順に重複なく記録する（run 全体で合算）。resume が失敗して新規スレッドへフォールバックしたターンは2件になるため、`thread_id` が上書きされる前に控える。
        self._all_parent_thread_ids: list = []
        self._child_usage_totals = dict.fromkeys(_CHILD_USAGE_KEYS, 0)
        self._child_usage_found = 0
        self._child_usage_missing = 0
        # 起動を検出できた子の総数（found + missing）。`spawn_agents`（codex.log 終了行）で使う。
        self._child_usage_detected = 0
        self.codex_created_files: list[str] = []  # 実行後に台帳登録する新規ファイルの絶対パス
        self._created_file_rows: list[dict] = []  # 台帳登録に成功した行（env["created_files"] 用）
        # move／台帳登録が1件でも失敗したら True（run_dir を消さず回収用に残し、回答本文へ注記を足す判定に使う）。
        self._created_files_failed = False
        # 資料作成の依頼でないターンに作られて保存しなかったファイルの件数／Marp の書き出しに失敗したか／調査記録を縮めた旨などの注記（回答の注記に出す）。
        self._discarded_files = 0
        self._marp_failed = False
        self._marp_failed_formats: list[str] = []  # 書き出しを試みて失敗した形式（html／pdf／pptx）
        self._record_notes: list[str] = []
        # 会話単位ロック（`_session_persistence_enabled` のときだけ後段で実値になる）。finally が参照できるよう既定値を確定する（`_conv_lock_acquired` は自分が取得できた時だけ True）。
        self._conv_lock = None
        self._conv_lock_acquired = False
        # 調査台帳: `.tmp/investigation/` 作成前に早期 return する経路でも finally が安全に参照できるよう既定値を先に確定する。
        self._investigation_dir = None  # run_dir/.tmp/investigation（.tmp 作成直後に確定）
        self._ledger_home = None  # workspace/.codex-sessions/{cid}（永続会話のみ）
        # 台帳の完了判定へ足す追加の必須根拠種別。`ledger_complete`/`no_progress`/`_retire_investigation_ledger` の全呼び出しへ同じ値を渡す（ターンの最初に1回だけ決める）。早期 return でも finally が参照できるよう先に空で確定し、`sp`/`decision["lens"]` の確定後に上書きする。
        self._ledger_required_extra: tuple[str, ...] = ()
        self._scope_source_state = "unknown"  # 範囲のソースの有無（`ledger_gate._scope_source_state`）
        # 中間の見直しを完了判定へ必須にするか（`_schema_v2` の確定後に上書きする）。早期 return 経路向けに先に確定する。
        self._ledger_require_review = False
        # 台帳に残っている「追加の観点」の義務の解決を要求するか（既定False・義務の有無は `investigation_ledger.pending_continuation_review()` が見直しの列だけから決める）。
        self._ledger_require_continuation_resolved = False
        self._investigation_restored = False
        self._investigation_verdict = None  # investigation_ledger.Verdict（ゲート確定後に埋める）
        # `_investigation_verdict` と対になる、降格適用済みの LedgerSnapshot（「確認できなかった項目」節が読む・ゲート確定後に埋める）。
        self._investigation_snapshot = None
        # このターン確定時点の中間の見直し（`load_reviews` の戻り値・ゲート確定後に埋める）と、末尾へ付ける「追加で調べますか？」の定型文（無ければ空文字）。
        self._investigation_reviews: tuple[dict, ...] = ()
        self._review_continuation_note_text = ""
        # `_result` の env とは別項目として chat_service へ渡す台帳の正規形（manifest/items/coverage）。`env["investigation"]` を組み立てるのと同じ箇所で埋める。ゲートが走らなかったターンは `None`（chat_service は DB 保存をスキップする）。
        self._investigation_record_payload: dict | None = None
        # このターンの開始時点（前ターンの退避台帳の復元直後）で既に `not_found_in_scope` だった item の id 集合。`apply_unverified_downgrades` はこの id を降格対象から除く。`_investigation_dir` 確定後に一度だけ埋める。
        self._investigation_pre_turn_not_found_ids: frozenset[str] = frozenset()
        self._ledger_continuations = 0
        # 「見直しの一巡」の状態。`_ledger_review_requests`＝見直しを頼んだ回数（上限 `_LEDGER_REVIEW_CAP`）、`_ledger_review_rounds`＝そのうち目録が増えた回数、`_ledger_review_items_added`＝見直しで足された item の延べ件数、`_ledger_review_attempted`＝1回でも頼んだか。
        self._ledger_review_requests = 0
        self._ledger_review_rounds = 0
        self._ledger_review_items_added = 0
        self._ledger_review_attempted = False
        # 最後に頼んだ見直しの直前の状態（台帳ゲートを通った完成回答・`_structured_answers` の長さと有効範囲・実行ごとの状態）。台帳ループを抜けた直後の安全弁が、見直し以後が失敗・未完了・確認・回答なし・悪化で終わったときにこの状態へ戻す。
        # `_ledger_review_reverted` は、その後にサイドカーの確認（ask_user）を採らない印（`_read_mcp_sidecar` は毎回先頭から読み直すため）。
        self._ledger_review_pre_candidate: dict | None = None
        self._ledger_review_pre_len = 0
        self._ledger_review_pre_msgs_len = 0
        self._ledger_review_pre_valid_from = 0
        self._ledger_review_pre_turn_failed = False
        self._ledger_review_pre_turn_failed_code = None
        self._ledger_review_pre_attempt_msgs_start = 0
        self._ledger_review_pre_latest_structured = None
        self._ledger_review_pre_codex_question = None
        self._ledger_review_reverted = False
        self._investigation_stopped_reason = None
        # 通常終了時は `env["investigation"]` を組み立てる前に退避（`_retire_investigation_ledger`）を実行してこの flag を立てる。外側 finally の退避（切断・例外時のフォールバック）は flag が立っていれば実行しない。
        self._investigation_retire_done = False
        # 会話継続（Codex ネイティブ resume）と、Codex を起動しない経路でも参照する値。
        self._persist_session = False
        self.resume_sid = None
        self.thread_id = None  # 捕捉した Codex session/thread id（`_session_persistence_enabled` のときだけ env に載せる）
        self._session_persistence_enabled = False
        self._safe_persistent_codex_home = None
        self._codex_home_ok = True
        self._auto_continue_count = 0  # Codex を起動しない経路でも参照するため起動条件の外で初期化
        self._multi_agent_enabled = False  # env["codex_multi_agent"] 用: Codex を起動しない経路では常に偽
        # agent_message は run 中に複数届く（作業宣言＋結論）。全部集めて後で結論を選ぶ（`_pick_codex_headline`）。try の外で初期化する（Popen 失敗の except 経路でも使うため）。
        self._agent_msgs: list[str] = []
        self._agent_partial = ""
        # 出力スキーマ有効時（`_schema_on`）だけ使う状態: `_latest_structured` は最新 attempt の最終出力の検証結果（合格した dict・不合格/欠落は None）。`_structured_answers` は attempt をまたいで合格した dict を積む。
        self._latest_structured: dict | None = None
        self._structured_answers: list[dict] = []
        # 台帳ゲートの回答候補・主張の対象範囲。境界より前の本文も部分回答として保持する。
        self._structured_answers_valid_from = 0
        # 起動前の準備（`_run_authoring` が起動条件を満たしたあとに確定する値）。
        self.sp = None
        self._layer = None
        self.ws_files = None
        self._before_ws_files: set = set()
        self._reason = None  # codex exec へ渡す推論レベル
        self._usage_depth_extra = None
        self._review_rounds = 0
        self._review_rounds_escalation = False
        self._direct_roots = None
        self._deny_roots = None
        self._sensitive_deny = None
        self._direct_read_ok = False
        self._tmp = None
        self.prompt = None
        self.prompt_with_history = None
        self._last_message_path = None
        self._mcp_budget_env: dict[str, str] = {}
        self.codex_home = None
        self._sidecar_path = None
        self.argv_base = None
        self.popen_env = None
        self._schema_on = False
        self._schema_v2 = False
        self._schema_level_effective = 0
        self._codex_run_started_at = None
        self._turn_started_wall = None
        self._activity_settings = None
        # `_attempt` ごとの状態。自動継続がツール未実行のまま宣言だけを繰り返すのを打ち切るための `_attempt_ran_tools` は attempt 開始ごとに False へ戻す。
        self.got_any_line = False  # resume 試行で1行も --json イベントを受け取れなければ resume 失敗とみなす
        self.attempt_returncode = None  # fallback 判定用
        self._attempt_ran_tools = False
        # item id は codex exec プロセスごとに振り直されるため、継続 attempt が同じ id を使うとノードを上書きして履歴が消える。2回目以降の attempt の node id 接頭辞に使う連番（初回=1・以降 +1）。
        self._attempt_no = 0
        # `_needs_continuation`／`codex_stopped_early` の判定を最新 attempt の message だけに絞る境界（この attempt 開始時点の `_agent_msgs` の長さ）。`_pick_codex_headline` は `_agent_msgs` 全件を見る。
        self._attempt_msgs_start = 0
        # attempt 開始時に消せなかった前 attempt の `-o` 本文（吸収で読み飛ばす対象・無ければ None）。
        self._stale_last_message = None
        # トップレベル `turn.failed`／`error` イベントを見た attempt かどうか。診断コード（`error.code`）だけ控え、本文はログにも利用者向け文言にも貼らない。
        self._turn_failed = False
        self._turn_failed_code = None
