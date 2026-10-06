"""会話・共有・ユーザー・監査等の永続化ドメインを束ねる facade パッケージ（docstring と re-export のみでロジックを持たない）。
Postgres の読み書きはドメイン別モジュールに分かれる:

    db.py             DSN・接続・advisory lock・init_schema・_SCHEMA
    conversations.py  会話・メッセージ・所有権確認・個人参照フラグ
    shares.py         会話共有・sanitized snapshot
    users.py          ユーザー管理・セッション
    workspace_files.py 個人 workspace ファイル台帳・TTL
    documents.py      文書台帳
    ingest.py         ingest_runs
    worlds.py         world レジストリ
    audit.py          監査ログ・チェーン検証
    settings.py       ユーザー設定＋全体設定 system_settings
    api_keys.py       外部連携 API キー
    usage.py          利用統計集計
    announcements.py  ホーム掲示板
    feedback.py       回答ごとの利用者フィードバック（message_feedback）

呼び出し側は `store.get_user(...)` のようにパッケージ属性を参照するため、monkeypatch は `store.X` に対して行う。
パッケージ内から `_audit_insert` を呼ぶ settings.py `set_system_settings` と audit.py `audit()` は、循環 import を避けて関数内で `from sherpa import store as _facade` と実行時に解決する。
新規コードは `from sherpa.store.<mod> import ...` の直 import を推奨する。
`tests/unit/test_store_surface.py` が `dir(sherpa.store)` の公開名一覧と `_SCHEMA` の内容ハッシュを固定している。
設計: docs/design/data.md「PostgreSQL（主な表と役割）」
"""
from __future__ import annotations

from .db import (
    _KB_ID,
    _SCHEMA,
    _connect,
    _dsn,
    _ensure,
    init_schema,
    schema_ready,
    workspace_file_lock,
    world_lock,
    world_registry_lock,
)  # noqa: F401
# `_inited`/`dict_row` は db.py 側の実装詳細で、公開名一覧を保つためだけに re-export する。
from .db import _inited, dict_row  # noqa: F401

# announcements・api_keys の全名（私的名含む）を re-export する。
from .announcements import (
    AnnouncementOrderError,
    create_announcement,
    delete_announcement,
    delete_expired_announcements,
    get_announcement,
    list_announcements,
    restore_announcement,
    restore_announcement_state,
    update_announcement,
)  # noqa: F401
from .api_keys import (
    SELF_ISSUED_DAILY_QUOTA_DEFAULT_FALLBACK,
    ClientOpIdConflictError,
    SelfIssuedQuotaExceededError,
    UserApiKeysDisallowedError,
    api_key_by_hash,
    apply_system_settings_and_revoke_if_disabled,
    count_ext_api_calls_by_key,
    count_self_issued_active_api_keys,
    get_api_key_webhook,
    insert_api_key,
    list_api_keys,
    list_webhook_keys_for_world,
    resolve_self_issued_daily_quota_cap,
    revoke_api_key,
    revoke_self_issued_api_keys,
    revoke_unconfirmed_key_by_client_op_id,
    touch_api_key,
)  # noqa: F401

# usage の全名（私的名含む）を re-export する。
from .usage import (
    _JST,
    UsagePeriodError,
    _USAGE_TOKEN_WHERE,
    _USAGE_TURN_CTE,
    _usage_period_bounds,
    _usage_tok,
    _usage_token_sum_cols,
    depth_quality_stats,
    record_depth_quality_run,
    usage_stats,
)  # noqa: F401

# usage_events（チャット以外の LLM 呼び出し計測）を re-export する。`sherpa/metering.py` は `sherpa.store.usage_events` を直接 import する。
from .usage_events import add_usage_event  # noqa: F401

# documents・ingest の re-export。
from .documents import (  # noqa: F401
    count_documents, document_exists, list_document_worlds, list_documents,
    list_documents_page, replace_documents,
)
from .ingest import (  # noqa: F401
    add_ingest_run,
    downgrade_orphaned_extracting_runs,
    fail_close_if_extracting,
    finish_ingest_run,
    finish_ingest_run_and_confirm_world,
    finish_ingest_run_and_delete_world,
    get_latest_es_run_summary,
    get_recent_es_attempts,
    get_latest_published_run_summary,
    get_latest_run_summary,
    list_ingest_runs,
    start_ingest_run,
    update_ingest_run_progress,
)

# worlds の re-export。`rebind_bind_invalidate_sig` は内部専用のため re-export せず、必要なら `from sherpa.store.worlds import ...` の直 import を使う。
from .worlds import (
    backfill_doc_count,
    backfill_manifest_and_doc_count,
    delete_world_row,
    get_world,
    get_world_status_row,
    list_worlds_db,
    restore_bind_invalidate_sig,
    set_resolve_settings,
    set_scan_report,
    set_scan_report_if_unchanged,
    set_world_sig,
    upsert_world,
    world_by_root,
)  # noqa: F401

# audit（監査ログ・チェーン一式）の全名（私的名含む）を re-export する（tests が `store._audit_insert` を monkeypatch する）。
from .audit import (
    _AUDIT_CANON_FIELDS,
    _REDACT_KEYS,
    _audit_canonical,
    _audit_entry_hash,
    _audit_insert,
    _redact,
    audit,
    get_messages_by_ids,
    list_audit,
    verify_audit_chain,
)  # noqa: F401

# settings の全名（私的名含む）を re-export する。`set_system_settings` 内の `_audit_insert` 呼び出しは facade 属性経由で実行時に解決する。`_system_settings_cache`/`_system_settings_cache_ts` は公開名一覧を保つためだけの re-export。
from .settings import (
    OpenAIEndpointSettingsConflict,
    PersonalKeysDisallowedError,
    _SETTINGS_FIELDS,
    _invalidate_system_settings_cache,
    _read_system_settings_fresh,
    _system_settings_cache,
    _system_settings_cache_ts,
    count_users_with_personal_keys,
    get_settings,
    get_system_settings,
    purge_personal_api_keys,
    seed_user_agent_once,
    seed_system_settings_once,
    set_system_settings,
    update_settings,
)  # noqa: F401

# users の全名を re-export する。
from .users import (
    create_session,
    create_user,
    create_users_bulk,
    get_user,
    get_user_by_email,
    get_user_by_uid,
    list_users,
    revoke_session,
    session_user,
    set_last_login,
    suggest_users,
    upsert_user,
)  # noqa: F401

# workspace_files の全名を re-export する。`set_contains_personal_workspace` は conversations テーブルを更新するため conversations.py に置く。
from .workspace_files import (
    claim_workspace_file_expired,
    delete_workspace_file,
    expired_workspace_files,
    get_workspace_file,
    list_workspace_files,
    live_workspace_rel_paths,
    no_live_upload_for_path,
    record_workspace_file,
)  # noqa: F401

# conversations・shares の全名（私的名含む）を re-export する。
from .conversations import (
    add_message,
    append_answer_notice,
    conversation_has_personal_message,
    create_conversation,
    delete_conversation,
    get_codex_usage_total,
    get_session_id,
    is_personal_tainted,
    conversation_is_personal_tainted,
    list_conversations,
    list_export_messages,
    owns_assistant_message,
    owns_conversation,
    recent_messages,
    rename_conversation,
    search_conversations,
    set_contains_personal_workspace,
    set_message_personal,
    set_pinned,
    set_session_id,
)  # noqa: F401

# message_feedback の全名を re-export する。
from .feedback import (
    MESSAGE_FEEDBACK_COMMENT_MAX_LEN,
    MESSAGE_FEEDBACK_TAGS,
    get_feedback_by_message_ids,
    get_feedback_by_message_ids_for_user,
    upsert_message_feedback,
)  # noqa: F401
# turn_metrics（集計専用の細い写像表）の公開関数を re-export する。
from .turn_metrics import (
    backfill_all,
    upsert,
    upsert_best_effort,
)  # noqa: F401
from .shares import (
    _REDACTED_TEXT,
    _SANITIZED_TITLE,
    _safe_evidence_item,
    _safe_evidence_packet,
    _safe_locator,
    _safe_share_answer,
    _strip_shared_message,
    ForkNotAllowedError,
    ShareNotSanitizedError,
    ShareUnavailableError,
    accept_share,
    create_sanitized_snapshot,
    create_share,
    extend_share,
    fork_received_share,
    get_conversation_for_read,
    is_invited,
    list_expiring_shares_for_owner,
    list_shares_for_conversation,
    refresh_sanitized_share,
    resolve_share_by_token,
    revoke_share,
)  # noqa: F401
