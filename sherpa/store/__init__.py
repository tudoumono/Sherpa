"""会話・共有・ユーザー・監査等の永続化ドメインを束ねる facade パッケージ。

出発点は M8（DATA-MODEL conversations/messages の MVP 部分集合）。Postgres への実際の
読み書きはドメイン別モジュールに分割済み（リファクタリング計画フェーズ4 S1〜S11・
docs/proposals/2026-07-02-リファクタリング計画.md）:

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

このファイル自体は**docstring と re-export のみ**でロジックを持たない。**facade＝パッケージ
属性がシーム**: 呼び出し側（sherpa/ 各所・tests/）は `from sherpa import store` の上で
`store.get_user(...)` のように毎回パッケージ属性を参照するため、monkeypatch は常に
`store.X`（このパッケージの属性）に対して行う（各ドメインモジュール内部の名前束縛を
直接差し替えても facade 側には反映されない・例外はパッケージ内から `_audit_insert` を呼ぶ
2箇所＝settings.py `set_system_settings` と audit.py `audit()` で、どちらもパッケージ初期化中の
循環 import を避けるため関数内で `from sherpa import store as _facade` と実行時解決している・
詳細は settings.py／audit.py の docstring 参照）。**新規コードは `from sherpa.store.<mod> import ...`
（例: `from sherpa.store.conversations import get_conversation`）の直 import を推奨**する
（facade 経由の間接参照は既存呼び出し側・テスト互換のために維持している）。

`tests/unit/test_store_surface.py` が `dir(sherpa.store)` の公開名一覧（フィルタ後）と
`_SCHEMA` 内容ハッシュを golden 固定しており、re-export 漏れ・意図しない挙動変化を検知する。
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
# `_inited`/`dict_row` は db.py 側の実装詳細（`_inited` は import 時点のスナップショットで
# db.py 側の以後の更新には追随しない・値を読むコードはどこにも無い）。
# tests/unit/test_store_surface.py の公開名スナップショット（store.py → store/ パッケージ化の
# 前後で dir(sherpa.store) が不変であることの担保）を壊さないためだけに re-export する。
from .db import _inited, dict_row  # noqa: F401

# フェーズ4 S2: announcements（運営掲示板）・api_keys（外部連携APIキー）純移動。facade 維持のため
# 全名（私的名含む）を明示 re-export する（tests/unit/test_store_surface.py 参照）。
from .announcements import (
    _ANNOUNCEMENT_FIELDS,
    _UNSET,
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
    insert_api_key,
    list_api_keys,
    list_webhook_keys_for_world,
    resolve_self_issued_daily_quota_cap,
    revoke_api_key,
    revoke_self_issued_api_keys,
    revoke_unconfirmed_key_by_client_op_id,
    touch_api_key,
)  # noqa: F401

# フェーズ4 S3: usage（利用統計集計）純移動。facade 維持のため全名（私的名含む）を明示 re-export する
# （tests/unit/test_store_surface.py 参照・tests は `store._compute_retention` を直接 monkeypatch する）。
from .usage import (
    _JST,
    _USAGE_AUDIT_ACTIONS,
    _USAGE_KNOWN_PROVIDERS,
    _USAGE_TOKEN_WHERE,
    _USAGE_TURN_CTE,
    _compute_retention,
    _usage_period_bounds,
    _usage_tok,
    _usage_token_sum_cols,
    usage_stats,
)  # noqa: F401

# S1（2026-07-15-LLMオーケストレーション実装計画.md）: usage_events（チャット以外の LLM 呼び出し計測）。
# facade 維持のため re-export する（`sherpa/metering.py` は `sherpa.store.usage_events` を直接 import する
# ため facade 経由ではないが、テストの monkeypatch 対象は facade 属性ではなくサブモジュール属性
# ＝`sherpa.store.usage_events.add_usage_event` 自体・metering.py の docstring 参照）。
from .usage_events import add_usage_event  # noqa: F401

# フェーズ4 S4: documents（文書台帳）・ingest（取り込み run）純移動。facade 維持。
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
    get_latest_published_run_summary,
    get_latest_run_summary,
    list_ingest_runs,
    start_ingest_run,
    update_ingest_run_progress,
)

# フェーズ4 S5: worlds（world レジストリ）純移動。facade 維持。`world_lock` は S1 で db.py に
# 純移動済み（すでに .db から re-export 済み・ここでは扱わない）。
# `rebind_bind_invalidate_sig` は facade に re-export **しない**（内部専用）:
# 呼び出し元は `worlds.py::rebind()` のみで、モジュール直 import（`from sherpa.store.worlds import
# rebind_bind_invalidate_sig`・このファイル冒頭で推奨している新規コードの流儀）で足りる。
# facade へ増やすたびに `tests/unit/test_store_surface.py` の公開名 golden 更新が要るため、
# 本当に外部（tests 含む）から `store.X` 属性として参照される名前だけを re-export する。
from .worlds import (
    backfill_doc_count,
    backfill_manifest_and_doc_count,
    delete_world_row,
    get_world,
    get_world_status_row,
    list_worlds_db,
    restore_bind_invalidate_sig,
    set_scan_report,
    set_scan_report_if_unchanged,
    set_world_sig,
    upsert_world,
    world_by_root,
)  # noqa: F401

# フェーズ4 S6: audit（監査ログ・チェーン一式）純移動。チェーン一式（redaction・insert・verify・
# hash 計算）は不可分のため1モジュールにまとめてある（audit.py 参照）。facade 維持のため
# 全名（私的名含む）を明示 re-export する（tests/unit/test_store_surface.py 参照・
# tests/api/test_system_settings.py が `store._audit_insert` を monkeypatch する）。
from .audit import (
    _AUDIT_CANON_FIELDS,
    _AUDIT_CHAIN_LOCK,
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

# フェーズ4 S7: settings（ユーザー設定＋全体設定 system_settings・キャッシュ含む）純移動。
# facade 維持のため全名（私的名含む）を明示 re-export する（tests/unit/test_store_surface.py・
# tests/api/test_system_settings.py・tests/api/test_settings_key_redaction.py・arms 系の
# get_system_settings patch 箇所が参照する）。`set_system_settings` 内の `_audit_insert` 呼び出しは
# facade 属性経由の実行時解決（settings.py の docstring 参照・patch 先はこの `store._audit_insert`
# 属性のまま変更していない）。`_system_settings_cache`/`_system_settings_cache_ts` は S1 の `_inited`
# と同様、import 時点のスナップショット値（settings.py 側の以後の更新には追随しない）。golden の
# 公開名一覧を保つためだけの re-export で、これらを直接読むコードはどこにも無い。
from .settings import (
    DEFAULT_SYSTEM_PROMPT,
    OpenAIEndpointSettingsConflict,
    PersonalKeysDisallowedError,
    _BEDROCK_VERIFIED_MODELS_MAX,
    _SETTINGS_DEFAULT,
    _SETTINGS_FIELDS,
    _SYSTEM_SETTINGS_CACHE_TTL,
    _bedrock_key_fingerprint,
    _invalidate_system_settings_cache,
    _read_system_settings_fresh,
    _system_settings_apply,
    _system_settings_cache,
    _system_settings_cache_ts,
    _system_settings_snapshot,
    add_bedrock_verified_models,
    catchup_ollama_allowlist_for_env_seeded_url_v2,
    count_users_with_personal_keys,
    get_settings,
    get_system_settings,
    migrate_marker_if_legacy_exists,
    purge_personal_api_keys,
    seed_system_settings_once,
    set_system_settings,
    update_settings,
)  # noqa: F401

# フェーズ4 S8: users（ユーザー管理・セッション）純移動。facade 維持のため全名を明示 re-export する
# （tests/api/test_auth_api.py・test_health_api.py・test_workspace_ttl.py が store.get_user/
# store.session_user を直接 monkeypatch する。facade 属性への実行時アクセスであり settings.py の
# `_audit_insert` のような相互呼び出しではないため追加の解決は不要・詳細は users.py 参照）。
from .users import (
    create_session,
    create_user,
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

# フェーズ4 S9: workspace_files（個人ファイル台帳・TTL）純移動。facade 維持のため全名を明示
# re-export する（tests/api/test_workspace_ttl.py が store.expired_workspace_files/store.get_user
# を直接 monkeypatch する・facade 属性への実行時アクセスなので追加の解決は不要・詳細は
# workspace_files.py 参照）。`set_contains_personal_workspace` は conversations テーブルを
# 更新するため conversations.py 側（S10）に置く。
from .workspace_files import (
    claim_workspace_file_expired,
    delete_workspace_file,
    expired_workspace_files,
    get_workspace_file,
    list_workspace_files,
    live_workspace_rel_paths,
    mark_workspace_file_expired,
    no_live_upload_for_path,
    record_workspace_file,
)  # noqa: F401

# フェーズ4 S10: conversations（会話・メッセージ・所有権確認・個人参照フラグ）・shares（会話共有・
# sanitized snapshot）純移動（隣接1スライス）。facade 維持のため全名（私的名含む）を明示
# re-export する（詳細・accept_share ↔ delete_conversation の FOR UPDATE 直列化契約は
# conversations.py・shares.py 参照）。
from .conversations import (
    add_message,
    conversation_has_personal_message,
    create_conversation,
    delete_conversation,
    get_conversation,
    get_session_id,
    is_personal_tainted,
    list_conversations,
    list_export_messages,
    owns_assistant_message,
    owns_conversation,
    recent_messages,
    rename_conversation,
    set_contains_personal_workspace,
    set_message_personal,
    set_pinned,
    set_session_id,
)  # noqa: F401

# message_feedback（回答ごとの利用者フィードバック）。facade 維持のため全名を明示 re-export する
# （tests/unit/test_store_surface.py・ルーターが `store.upsert_message_feedback` 等を facade 属性
# 経由で参照/monkeypatch する）。
from .feedback import (
    MESSAGE_FEEDBACK_COMMENT_MAX_LEN,
    MESSAGE_FEEDBACK_TAGS,
    get_feedback_by_message_ids,
    get_feedback_by_message_ids_for_user,
    upsert_message_feedback,
)  # noqa: F401
from .shares import (
    _REDACTED_TEXT,
    _SANITIZED_TITLE,
    _SHARE_SAFE_LENS,
    _safe_evidence_item,
    _safe_evidence_packet,
    _safe_locator,
    _safe_share_answer,
    _strip_shared_message,
    accept_share,
    create_sanitized_snapshot,
    create_share,
    get_conversation_for_read,
    is_invited,
    resolve_share_by_token,
    revoke_share,
)  # noqa: F401
