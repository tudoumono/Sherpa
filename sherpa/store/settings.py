"""ユーザー設定と全体設定（system_settings・キャッシュ含む）の読み書き。
`set_system_settings` 内の `_audit_insert` 呼び出しは facade 属性経由で実行時に解決する。
設計: docs/design/settings.md「置き場の分担」
"""
from __future__ import annotations

import math
import time

from psycopg.types.json import Json

from .db import _connect, _ensure

# 設定の既定値（行が無いときに使う）。`agent=None` は未設定で、呼び出し側が自動選択（`agent_constructs.default_agent`）に倒す。
# `gemini_*`/`bedrock_*` の個人設定列は DB に残るが、ここにも読み書きにも含めない。
# `ollama_url` の既定は空文字（未設定）で、実際の解決は `keys.resolve_ollama_url` が「利用者の選択 → 管理者の既定 → 組み込み既定」の順で行う（ハードコード既定を DB へ焼き付けない）。
_SETTINGS_DEFAULT = {"agent": None, "openai_api_key": None, "ollama_url": "",
                     "codex_model_provider": ""}
_SETTINGS_FIELDS = tuple(_SETTINGS_DEFAULT)

# 固定 advisory lock key（"PKEY"）。`personal_api_keys_allowed` の判定と個人キー書込みを `purge_personal_api_keys()` と直列化する。`update_settings()` が個人キーを書くときだけ取り、書込み直前に同一トランザクションで再確認する。
_PERSONAL_KEY_LOCK = 0x504B4559


class PersonalKeysDisallowedError(Exception):
    """個人キーの書込み直前の再確認で `personal_api_keys_allowed` が偽だった。"""


class OpenAIEndpointSettingsConflict(ValueError):
    """`openai_endpoint_kind`/`openai_base_url` の書込み直前（advisory lock 取得後）に読み直した実効値が、kind が openai 以外なら base_url も必要という整合を満たさなかった（`admin_settings_put` が 422 に変換する）。"""


def get_settings(user_id="admin") -> dict:
    """ユーザの頭脳/モデル/キー設定（行が無ければ既定）。キーも含むためサーバ内部用。"""
    _ensure()
    with _connect() as c:
        row = c.execute(
            "SELECT agent, openai_api_key, ollama_url, codex_model_provider "
            "FROM user_settings WHERE user_id=%s", (user_id,)).fetchone()
    return {**_SETTINGS_DEFAULT, **(row or {})}


def update_settings(user_id="admin", **fields) -> dict:
    """設定を upsert する。許可フィールドのみ。`openai_api_key` は None/未指定なら変更しない（書込専用）。
    個人キー列は、この呼び出しが明示的に触れた列だけを UPDATE する（触れていない列は SET から除外し、`purge_personal_api_keys` が割り込んでも消えたキーを書き戻さない）。
    """
    cur = get_settings(user_id)
    upd = {k: v for k, v in fields.items() if k in _SETTINGS_FIELDS and v is not None}
    # 空文字はクリア指示（openai は書込専用キー）。
    if "openai_api_key" in fields and fields["openai_api_key"] == "":
        upd["openai_api_key"] = None
    merged = {**cur, **upd}
    # `agent` は明示された値（今回の `fields` に含まれる、または既存行にある）だけを保存する。選ばれていなければ空文字 `''` のままにして、解決済みの頭脳名を DB へ焼き付けない（読み出し側が `default_agent()` で都度解決する）。
    merged["agent"] = merged["agent"] or ""  # 列は NOT NULL（未設定は空文字＝読み出し時に自動選択）。
    merged["codex_model_provider"] = merged["codex_model_provider"] or ""  # 列は NOT NULL（''＝未設定＝openai）。
    # 列は NOT NULL（''＝未設定＝`keys.resolve_ollama_url` が中央既定/組み込み既定へ解決する）。
    merged["ollama_url"] = merged["ollama_url"] or ""
    # 個人キー列は、この呼び出しの `fields` に明示的に含まれていた（クリアの "" も含む）列だけを触れた列として扱い、触れていない列は SET から除外する。SQL に埋め込む列名は固定タプルの静的文字列のみ。
    _key_cols = ("openai_api_key",)
    _key_touched = {k: (k in fields and fields[k] is not None) for k in _key_cols}
    # 個人キーを実際にセットする（非空の値）呼び出しだけ、書込み直前に `personal_api_keys_allowed` を同一トランザクションで再確認する。
    _writing_personal_key = any(fields.get(k) for k in _key_cols)
    set_fragments = [
        "agent=EXCLUDED.agent", "ollama_url=EXCLUDED.ollama_url",
        "codex_model_provider=EXCLUDED.codex_model_provider",
    ]
    set_fragments += [f"{k}=EXCLUDED.{k}" for k in _key_cols if _key_touched[k]]
    set_fragments.append("updated_at=now()")
    set_sql = ", ".join(set_fragments)
    with _connect() as c:
        if _writing_personal_key:
            c.execute("SELECT pg_advisory_xact_lock(%s)", (_PERSONAL_KEY_LOCK,))
            _a6_row = c.execute(
                "SELECT value FROM system_settings WHERE key='personal_api_keys_allowed'").fetchone()
            if not bool(_a6_row["value"] if _a6_row else False):
                raise PersonalKeysDisallowedError(
                    "個人 API キーは無効化されています（管理者が中央設定でキーを管理します）")
        c.execute(
            "INSERT INTO user_settings (user_id, agent, openai_api_key, ollama_url, "
            "  codex_model_provider, updated_at) "
            "VALUES (%s,%s,%s,%s,%s, now()) "
            f"ON CONFLICT (user_id) DO UPDATE SET {set_sql}",
            (user_id, merged["agent"], merged["openai_api_key"], merged["ollama_url"],
             merged["codex_model_provider"]))
    return merged


# 全体設定（system_settings・admin 書込のみ・監査つき）。優先順は system_settings > コード既定（env は初回シードのみ）。認可（admin）は呼び出し側で済ませてから `set_system_settings` を呼ぶ。

# 短TTLの読み取りキャッシュ（プロセス内）。`set_system_settings` は必ずこのキャッシュを無効化する。
_SYSTEM_SETTINGS_CACHE_TTL = 3.0
_system_settings_cache: dict | None = None
_system_settings_cache_ts: float = 0.0


def _invalidate_system_settings_cache() -> None:
    global _system_settings_cache, _system_settings_cache_ts
    _system_settings_cache = None
    _system_settings_cache_ts = 0.0


def _read_system_settings_fresh(*, connect_timeout: float | None = None,
                                statement_timeout_ms: int | None = None) -> dict:
    """system_settings 全件を DB から直接読む（共有キャッシュ `_system_settings_cache` を参照も更新もしない）。
    `get_system_settings()` のキャッシュミス時の実体で、timeout 予算の配分は同一。`providers/__init__.py::get_provider` はこちらを直接呼び、他スレッドのキャッシュ状態に依らない値を得る。
    """
    budget_started = time.monotonic()
    _ensure(connect_timeout=connect_timeout)
    connect_kwargs = {}
    if connect_timeout is not None:
        remaining = connect_timeout - (time.monotonic() - budget_started)
        if remaining <= 0:
            # `_ensure()` で予算を使い切ったら接続を試みない。
            raise TimeoutError("_read_system_settings_fresh: budget exhausted before connecting")
        connect_kwargs["connect_timeout"] = max(1, math.ceil(remaining))
    with _connect(**connect_kwargs) as c:
        if statement_timeout_ms is not None:
            elapsed_ms = (time.monotonic() - budget_started) * 1000
            remaining_ms = max(1, int(statement_timeout_ms - elapsed_ms))
            # SET LOCAL にして、返却後の接続へ statement_timeout を残さない。
            c.execute(f"SET LOCAL statement_timeout = '{remaining_ms}ms'")
        rows = c.execute("SELECT key, value FROM system_settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def get_system_settings(*, connect_timeout: float | None = None,
                        statement_timeout_ms: int | None = None) -> dict:
    """全体設定（system_settings 全件）を `{key: value}` で返す（未設定キーは含まれない・admin 未設定なら空 dict）。短TTLキャッシュ付きで、返り値は都度コピー。value は JSONB の論理値。
    `connect_timeout`/`statement_timeout_ms`（省略可・None＝無期限）は残り時間ベースで渡し、接続確立後に `SET` で発行する（キャッシュ命中時は無関係）。実体は `_read_system_settings_fresh()`。
    キャッシュに触れずに必ず DB から読みたい呼び出し元は、この関数のシグネチャを変えず `_read_system_settings_fresh()` を直接呼ぶ。
    """
    global _system_settings_cache, _system_settings_cache_ts
    now = time.monotonic()
    cached = _system_settings_cache
    if cached is not None and now - _system_settings_cache_ts < _SYSTEM_SETTINGS_CACHE_TTL:
        return dict(cached)
    data = _read_system_settings_fresh(connect_timeout=connect_timeout,
                                       statement_timeout_ms=statement_timeout_ms)
    _system_settings_cache = dict(data)
    _system_settings_cache_ts = now
    return data


def _system_settings_snapshot(conn, keys) -> dict:
    """指定キーの現在値スナップショット（`{key: value|None}`・監査の before/補償用）。"""
    snap: dict = {}
    for k in keys:
        row = conn.execute("SELECT value FROM system_settings WHERE key=%s", (k,)).fetchone()
        snap[k] = row["value"] if row else None
    return snap


def _system_settings_apply(conn, updates: dict, uid) -> None:
    """updates を適用する（value=None は行削除＝未設定へ戻す・それ以外は upsert）。"""
    for k, v in updates.items():
        if v is None:
            conn.execute("DELETE FROM system_settings WHERE key=%s", (k,))
        else:
            conn.execute(
                "INSERT INTO system_settings (key, value, updated_by) VALUES (%s,%s,%s) "
                "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now(), "
                "  updated_by=EXCLUDED.updated_by",
                (k, Json(v), uid))


# 接続先 URL を持つキーは、監査（audit_log・JSONB・平文）には host 表現（`llm._redact_url_for_error`）だけを残す。`secret_keys` の有無に関わらず常に畳む。
_URL_SETTINGS_KEYS = frozenset({"openai_base_url", "ollama_url"})


def _redact_secret_settings(state: dict, secret_keys: frozenset | None = None) -> dict:
    """監査用: `secret_keys` に含まれるキーの値は `<set>`/`<cleared>` に畳み、キー値（中央 API キー等）を audit_log へ書かない。`_URL_SETTINGS_KEYS`（`openai_base_url`/`ollama_url`）は host 表現のみへ畳む。DB への実際の書込（`_system_settings_apply`）はこの関数を経由しない。
    `openai_base_url` は次の固定文字列に畳む（どの分岐も例外を上げない）:
    - `None`/空文字列 → `<cleared>`。
    - 非文字列（破損した値）→ `(不正な保存値)`。型チェックを先に行い `assert_openai_base_url_allowed` へは渡さない。
    - 文字列だが `llm.assert_openai_base_url_allowed()` の形式検証に落ちる → `<不正なURL>`（不合格なら host 表現を作らない）。
    """
    from sherpa import llm
    secret_keys = secret_keys or frozenset()
    out: dict = {}
    for k, v in state.items():
        if k in secret_keys:
            out[k] = "<set>" if v else "<cleared>"
        elif k == "openai_base_url":
            if v is None or v == "":
                out[k] = "<cleared>"
            elif not isinstance(v, str):
                # 非文字列（`{}`/`[]`/`0`/`False` 等）は `<cleared>` と区別して残す（復旧 PUT の before に破損値を証跡として残すため）。
                out[k] = "(不正な保存値)"
            else:
                try:
                    llm.assert_openai_base_url_allowed(v)
                except Exception:
                    out[k] = "<不正なURL>"
                else:
                    out[k] = llm._redact_url_for_error(v) or "<不正なURL>"
        elif k in _URL_SETTINGS_KEYS:
            out[k] = (llm._redact_url_for_error(v) or "<不正なURL>") if v else "<cleared>"
        else:
            out[k] = v
    return out


def _assert_openai_endpoint_update_consistent(conn, updates: dict) -> None:
    """`openai_endpoint_kind`/`openai_base_url` の実効値（この更新後に有効になる値）を、advisory lock 取得後の同一コネクションから読み直して整合検証する（kind が openai 以外なら base_url も必要・`llm.assert_openai_endpoint_consistent` が判定する）。
    不整合なら `OpenAIEndpointSettingsConflict`（呼び出し元が 422 へ変換）を送出し、書込みは行わない。
    一操作復旧: 既存の `openai_base_url` が非文字列に破損している状態で `openai_endpoint_kind` を `"openai"` へ保存する PUT は、`updates["openai_base_url"] = None` を補って 1 回で復旧する（kind=openai では base_url は使われない）。
    """
    from sherpa import llm
    row_kind = conn.execute("SELECT value FROM system_settings WHERE key=%s",
                            ("openai_endpoint_kind",)).fetchone()
    row_base = conn.execute("SELECT value FROM system_settings WHERE key=%s",
                            ("openai_base_url",)).fetchone()
    cur_kind = row_kind["value"] if row_kind else None
    cur_base = row_base["value"] if row_base else None
    if (updates.get("openai_endpoint_kind") == "openai"
            and "openai_base_url" not in updates
            and cur_base is not None and not isinstance(cur_base, str)):
        updates["openai_base_url"] = None
    eff_kind = (updates["openai_endpoint_kind"] if "openai_endpoint_kind" in updates
               else cur_kind) or "openai"
    eff_base = (updates["openai_base_url"] if "openai_base_url" in updates
               else cur_base)
    # `None` だけを未設定として扱う（falsy な非文字列を `or ""` で潰すと破損値の復旧判定が効かなくなる）。
    if eff_base is not None and not isinstance(eff_base, str):
        raise OpenAIEndpointSettingsConflict(
            "接続先 URL（openai_base_url）の値が不正です（文字列ではありません）")
    try:
        llm.assert_openai_endpoint_consistent(eff_kind, eff_base or "")
    except ValueError as e:
        raise OpenAIEndpointSettingsConflict(str(e)) from e


def set_system_settings(uid, updates: dict, secret_keys: frozenset | None = None, *,
                        in_txn=None) -> dict:
    """全体設定を部分更新する（admin 認可は呼び出し側前提）。
    `updates` は `{key: value}`。`value=None` は未設定へ戻す（該当行を削除＝env/既定へフォールバック）、それ以外は upsert する。変更を監査する（`system_settings.updated`・before/after・severity=warning）。
    `secret_keys`（省略可）のキーは、DB へは平文で書くが、監査の before/after だけ `<set>`/`<cleared>` に畳む。
    `in_txn`（省略可）: `(conn, uid, updates)` を受け取る callable。設定変更・監査と同一トランザクションで追加の書込みを行うフック（例: `api_keys.apply_system_settings_and_revoke_if_disabled`）。例外は設定変更ごとロールバックされる。`system_settings.updated` の監査 INSERT より前に呼ぶこと（lock → 更新 → 監査の順序を崩さない）。
    設定変更（before スナップショット→適用）と監査行 INSERT を同一トランザクションで行う。監査が失敗すれば設定変更も rollback され、例外は呼び出し側へ伝播する（500 に変換される）。キャッシュ無効化は commit 成功後に 1 回だけ行う。返り値は適用した updates。
    `_audit_insert` は `_facade._audit_insert`（facade 属性）経由で実行時に解決する（関数内 import はパッケージ初期化中の循環 import を避けるため）。
    """
    _ensure()
    from sherpa import store as _facade
    with _connect() as c:
        # `_ENV_SEED_LOCK` を env シード・追いつき移行と共有し、system_settings への複数行書込みを直列化する。
        c.execute("SELECT pg_advisory_xact_lock(%s)", (_ENV_SEED_LOCK,))
        if "openai_endpoint_kind" in updates or "openai_base_url" in updates:
            _assert_openai_endpoint_update_consistent(c, updates)
        # `keys` は一操作復旧（`updates` への `openai_base_url` の補完）の後に確定させる。
        keys = list(updates)
        before = _system_settings_snapshot(c, keys)
        _system_settings_apply(c, updates, uid)
        # in_txn を監査 INSERT より前に呼ぶ。`_audit_insert` が取る `_AUDIT_CHAIN_LOCK` と in_txn 側のロックを、全経路で「in_txn 側のロック → `_AUDIT_CHAIN_LOCK`」の一方向に統一し、デッドロックを防ぐ。
        if in_txn is not None:
            in_txn(c, uid, updates)
        # `secret_keys` の有無に関わらず常に畳む（URL キーの host 化は `_redact_secret_settings` が担う）。
        audit_before = _redact_secret_settings(before, secret_keys)
        audit_after = _redact_secret_settings(updates, secret_keys)
        _facade._audit_insert(c, uid, "system_settings.updated", "system_settings", None,
                      before_state=audit_before, after_state=audit_after, severity="warning")
    _invalidate_system_settings_cache()
    return updates


# 固定 advisory lock key（"SEED"）。env→system_settings シード試行を直列化し、`set_system_settings` にも同じ lock を取らせて、`ollama_url`/`ollama_allowlist` の行ロック取得順序が経路ごとに違ってもデッドロックしないようにする。
_ENV_SEED_LOCK = 0x53454544


def seed_system_settings_once(updates: dict, guard_key: str,
                              secret_keys: frozenset | None = None, *,
                              ollama_allowlist_merge: tuple[str, str] | None = None) -> tuple[dict, dict]:
    """env→system_settings の初回シード専用の書込み（`sherpa.api._seed_settings_from_env`/`_seed_ollama_url_from_env` 専用）。
    `set_system_settings` と違い既存の行を上書きしない。各 INSERT は `guard_key`（完了マーカー）の行がその INSERT の時点で存在しない場合に限り実行する（`WHERE NOT EXISTS` を INSERT 文へ埋め込む）。`_ENV_SEED_LOCK` は `set_system_settings` とも共有し、複数行書込みの行ロック順序差によるデッドロックを防ぐ。
    `ollama_allowlist_merge`（省略可）: `(url_key, host_entry)`。`url_key` が実際に新規 INSERT できたときだけ、`host_entry`（正規化済み host:port）を `ollama_allowlist` の現在値へ追記する（`SELECT ... FOR UPDATE` で最新値を読む）。`updates` に `ollama_allowlist` を含めると `ValueError`。
    戻り値: `(applied, conflicts)`。`applied` は実際に INSERT/マージされた `{key: value}`、`conflicts` は INSERT がスキップされた `{key: 現在の DB 値（無ければ None）}`。
    """
    _ensure()
    if ollama_allowlist_merge is not None and "ollama_allowlist" in updates:
        raise ValueError("ollama_allowlist_merge 使用時は updates に ollama_allowlist を含められません")
    from sherpa import store as _facade
    applied: dict = {}
    conflicts: dict = {}
    with _connect() as c:
        c.execute("SELECT pg_advisory_xact_lock(%s)", (_ENV_SEED_LOCK,))
        for k, v in updates.items():
            row = c.execute(
                "INSERT INTO system_settings (key, value, updated_by) "
                "SELECT %s, %s, %s WHERE NOT EXISTS ("
                "  SELECT 1 FROM system_settings WHERE key = %s"
                ") ON CONFLICT (key) DO NOTHING RETURNING key",
                (k, Json(v), "system", guard_key)).fetchone()
            if row is not None:
                applied[k] = v
            else:
                cur = c.execute("SELECT value FROM system_settings WHERE key=%s", (k,)).fetchone()
                conflicts[k] = cur["value"] if cur else None
        before_allowlist = None
        if ollama_allowlist_merge is not None:
            url_key, host_entry = ollama_allowlist_merge
            if url_key in applied and host_entry:
                # 先に行を確保（ON CONFLICT DO NOTHING）してから `FOR UPDATE` する（行が無いままではロックできない）。
                c.execute(
                    "INSERT INTO system_settings (key, value, updated_by) VALUES "
                    "('ollama_allowlist', '[]'::jsonb, 'system') ON CONFLICT (key) DO NOTHING")
                al_row = c.execute(
                    "SELECT value FROM system_settings WHERE key='ollama_allowlist' FOR UPDATE").fetchone()
                current = list((al_row["value"] if al_row else None) or [])
                before_allowlist = current
                if host_entry not in current:
                    merged = [*current, host_entry]
                    c.execute(
                        "UPDATE system_settings SET value=%s, updated_at=now(), updated_by='system' "
                        "WHERE key='ollama_allowlist'", (Json(merged),))
                    applied["ollama_allowlist"] = merged
        if applied:
            audit_before = {"ollama_allowlist": before_allowlist} if before_allowlist is not None else None
            # `secret_keys` の有無に関わらず常に畳む（env シードで取り込む URL も生のまま audit_log へ残さない）。
            audit_after = _redact_secret_settings(applied, secret_keys)
            _facade._audit_insert(c, "system", "system_settings.env_seeded", "system_settings", None,
                          before_state=audit_before, after_state=audit_after, severity="info")
    if applied:
        _invalidate_system_settings_cache()
    return applied, conflicts


def count_users_with_personal_keys() -> int:
    """個人秘密キー（openai か、閉じたプロバイダ gemini/bedrock の旧キー列）を保存中のユーザー数（`personal_api_keys_allowed` を OFF にする前の確認ダイアログ用）。"""
    _ensure()
    with _connect() as c:
        row = c.execute(
            "SELECT count(*) AS n FROM user_settings WHERE "
            "openai_api_key IS NOT NULL OR gemini_api_key IS NOT NULL OR bedrock_api_key IS NOT NULL"
        ).fetchone()
    return int(row["n"]) if row else 0


def seed_user_agent_once(agent: str, guard_key: str, version: int) -> int:
    """env→個人設定の頭脳の初回シード専用の書込み（`sherpa.api._seed_user_agent_from_env` 専用）。
    `guard_key`（完了マーカー）が無いときだけ、頭脳が未選択（空）の既存利用者全員（設定行が無い利用者は行を作る）へ `agent` を保存し、マーカーを同じトランザクションで確定する。
    選択済みの利用者は変えない。戻り値は保存した人数（マーカーが既にあれば 0）。
    """
    _ensure()
    from sherpa import store as _facade
    with _connect() as c:
        c.execute("SELECT pg_advisory_xact_lock(%s)", (_ENV_SEED_LOCK,))
        if c.execute("SELECT 1 FROM system_settings WHERE key=%s", (guard_key,)).fetchone():
            return 0
        n = len(c.execute(
            "UPDATE user_settings SET agent=%s, updated_at=now() WHERE COALESCE(agent,'')='' "
            "RETURNING user_id", (agent,)).fetchall())
        n += len(c.execute(
            "INSERT INTO user_settings (user_id, agent, updated_at) "
            "SELECT u.uid, %s, now() FROM users u WHERE u.uid IS NOT NULL "
            "  AND NOT EXISTS (SELECT 1 FROM user_settings s WHERE s.user_id = u.uid) "
            "ON CONFLICT (user_id) DO NOTHING RETURNING user_id", (agent,)).fetchall())
        c.execute(
            "INSERT INTO system_settings (key, value, updated_by) VALUES (%s, %s, 'system') "
            "ON CONFLICT (key) DO NOTHING", (guard_key, Json(version)))
        _facade._audit_insert(c, "system", "system_settings.env_seeded", "system_settings", None,
                              after_state={"user_agent_seeded": n, "agent": agent}, severity="info")
    _invalidate_system_settings_cache()
    return n


def purge_personal_api_keys(actor: str = "system") -> int:
    """`personal_api_keys_allowed` が偽のとき、全ユーザーの個人秘密キーを NULL へ一括削除する。呼び出し側（管理画面の保存時・起動時）が偽のときに呼ぶ。
    冪等: 既に NULL の行は対象外で、実際に変更した行数だけが `RETURNING` に乗る。0件のときは監査行も作らない。
    `_PERSONAL_KEY_LOCK` を `update_settings()` の個人キー書込みと共有する。
    """
    _ensure()
    from sherpa import store as _facade
    with _connect() as c:
        c.execute("SELECT pg_advisory_xact_lock(%s)", (_PERSONAL_KEY_LOCK,))
        rows = c.execute(
            "UPDATE user_settings SET openai_api_key=NULL, gemini_api_key=NULL, bedrock_api_key=NULL, "
            "  updated_at=now() "
            "WHERE openai_api_key IS NOT NULL OR gemini_api_key IS NOT NULL OR bedrock_api_key IS NOT NULL "
            "RETURNING user_id").fetchall()
        count = len(rows)
        if count:
            _facade._audit_insert(c, actor, "user_settings.personal_keys_purged", "user_settings", None,
                          detail={"count": count}, severity="warning")
    return count
