"""`user_settings` のモデル名/Ollama接続先列の DEFAULT・既存行の扱いの実 DB テスト。

裁定（RV 3巡目 #1）: 値ベースの一括移行（「旧ハードコード既定と完全一致する既存行を空文字へ
戻す」）は撤回した。保存済みデータには provenance（自動実体化された値か、利用者が能動的に
選んだ値かの区別）が無く、値の一致だけでは判定できないため（例: `ollama_url` を意図的に
`http://localhost:11434` と選んだ利用者の行も、自由入力時代の既定値のまま一度も触っていない
行も、DB 上は区別できない）。列 DEFAULT（新規行向け）の空文字化だけを維持し、既存行は保守的に
維持する。中央既定へ戻したい利用者は、個人設定の「管理者の既定を使う」（空選択肢）を選ぶ。

要 Postgres（`sherpa_test` 分離DB）。DB 不可は SKIP（他の tests/unit/test_*_store_roundtrip.py と同じ流儀）。
"""
from __future__ import annotations

import time

import pytest

from sherpa import store


def _sfx() -> str:
    return str(int(time.time() * 1000))[-8:]


def _try_init() -> None:
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"DB down: {e}")


def test_fresh_user_settings_row_default_is_empty_not_hardcoded():
    """新規行（`CREATE TABLE`/`ADD COLUMN` 由来の列 DEFAULT）は空文字であること
    （`sherpa/store/db.py` の `ALTER COLUMN ... SET DEFAULT ''` 群）。`_SETTINGS_DEFAULT`
    （アプリ側の既定値マージ）だけでなく DB 列 DEFAULT 自体がこの契約を満たすことを、
    行を明示挿入せず素の INSERT で確認する。"""
    _try_init()
    uid = f"coldef-fresh-{_sfx()}"
    with store._connect() as c:
        c.execute("DELETE FROM user_settings WHERE user_id=%s", (uid,))
        c.execute("INSERT INTO user_settings (user_id) VALUES (%s)", (uid,))
        row = c.execute(
            "SELECT openai_model, gemini_model, ollama_model, ollama_url, codex_model "
            "FROM user_settings WHERE user_id=%s", (uid,)).fetchone()
    assert row["openai_model"] == "" and row["gemini_model"] == "" and row["ollama_model"] == ""
    assert row["ollama_url"] == "" and row["codex_model"] == ""


def test_existing_row_matching_old_hardcoded_default_is_preserved_across_schema_init():
    """裁定（RV 3巡目 #1）: 既存行の値が、かつての自由入力時代のハードコード既定
    （例: `ollama_url='http://localhost:11434'`）と完全一致していても、`init_schema()`
    （`_SCHEMA` の全文再実行を含む・毎起動走る）はその値を書き換えない。provenance が無い
    以上、これは「自動実体化された値」かもしれないし「利用者が明示的に選んだ値」かもしれない
    ため、値の一致だけを根拠に自動で空文字化してはならない。"""
    _try_init()
    uid = f"coldef-preserve-{_sfx()}"
    store.upsert_user(uid, email=f"{uid}@coldef.local", display_name=uid,
                      password_hash="x", role="user", status="active")
    with store._connect() as c:
        c.execute(
            "INSERT INTO user_settings (user_id, openai_model, gemini_model, ollama_model, "
            "ollama_url, codex_model) VALUES (%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (user_id) DO UPDATE SET openai_model=EXCLUDED.openai_model, "
            "gemini_model=EXCLUDED.gemini_model, ollama_model=EXCLUDED.ollama_model, "
            "ollama_url=EXCLUDED.ollama_url, codex_model=EXCLUDED.codex_model",
            (uid, "gpt-5.5", "gemini-2.5-flash", "qwen2.5", "http://localhost:11434", "gpt-5.5"))

    store.init_schema()   # 通常の起動と同じ経路（毎起動 _SCHEMA 全文実行）

    row = store.get_settings(uid)
    assert row["openai_model"] == "gpt-5.5"
    assert row["gemini_model"] == "gemini-2.5-flash"
    assert row["ollama_model"] == "qwen2.5"
    assert row["ollama_url"] == "http://localhost:11434"
    assert row["codex_model"] == "gpt-5.5"


def test_existing_row_with_actively_chosen_value_is_also_preserved():
    """比較対照: 旧既定と異なる（能動的に選ばれたことが明らかな）値も、当然そのまま維持される。"""
    _try_init()
    uid = f"coldef-chosen-{_sfx()}"
    store.upsert_user(uid, email=f"{uid}@coldef.local", display_name=uid,
                      password_hash="x", role="user", status="active")
    with store._connect() as c:
        c.execute(
            "INSERT INTO user_settings (user_id, openai_model) VALUES (%s,%s) "
            "ON CONFLICT (user_id) DO UPDATE SET openai_model=EXCLUDED.openai_model",
            (uid, "gpt-5.4-mini"))

    store.init_schema()

    assert store.get_settings(uid)["openai_model"] == "gpt-5.4-mini"


def test_user_can_return_to_central_default_by_choosing_empty_explicitly():
    """既存利用者が中央既定/カタログ既定へ戻したい場合の導線: 個人設定で「管理者の既定を使う」
    （空文字を明示送信）を選べば、保存値は空文字になる（自動移行ではなく、利用者の明示操作）。"""
    _try_init()
    uid = f"coldef-return-{_sfx()}"
    store.upsert_user(uid, email=f"{uid}@coldef.local", display_name=uid,
                      password_hash="x", role="user", status="active")
    store.update_settings(uid, ollama_url="http://localhost:11434")
    assert store.get_settings(uid)["ollama_url"] == "http://localhost:11434"

    store.update_settings(uid, ollama_url="")   # 「管理者の既定を使う」に相当する明示クリア
    assert store.get_settings(uid)["ollama_url"] == ""
