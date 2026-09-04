"""W3 + W4 workspace hardening テスト。

テスト範囲:
W3 — users.uid の DB CHECK 制約
  - 不正 uid（../ a/b 等）の直接 SQL insert が DB レベルで拒否される。
  - 正当な uid（英数字・ドット・ハイフン等）は insert 可。
  - 制約の VALIDATE が完了していること（pg_constraint.convalidated）。

W4 — 個人 workspace ファイル TTL 自動掃除
  - アップロード時に expires_at が設定される（TTL_DAYS > 0）。
  - expires_at=NULL（無期限）のファイルは掃除されない。
  - 期限切れファイルは sweep で物理削除 + 台帳 status='expired' になる。
  - 無効化（disabled）ユーザーの期限切れファイルは sweep で削除されない（保持）。
  - sweep はベース外のパスを削除しない（base-confined）。
  - sweep は DB 接続不可なら何も削除しない（fail-safe）。
  - RAG 隔離不変条件: sweep は ES/Neo4j を参照しない。

要 Postgres。DB 不可は SKIP。
"""
from __future__ import annotations

import io
import pathlib
import tempfile
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from _test_users import register_test_uid
from sherpa import auth, store
from sherpa.api import app, _sweep_expired_workspace

# 個人 workspace ルート・TTL（90 日）は conftest.py が tests/api のどの test_*.py よりも先に
# 確定させている（sherpa.api._USERS_DIR / _WORKSPACE_TTL_DAYS は import 時定数）。
client = TestClient(app, raise_server_exceptions=True)


# ---- ヘルパ ----

def _sfx() -> str:
    return str(int(time.time() * 1000))[-8:]


from _common import _try_init


def _mk_user(sfx: str, role: str = "user", status: str = "active") -> tuple[str, str]:
    uid = f"ttl{role[:1]}{sfx}"
    pw = f"pw-{uid}"
    store.upsert_user(uid, email=f"{uid}@ex.local", display_name=uid.upper(),
                      password_hash=auth.hash_password(pw), role=role, status=status)
    register_test_uid(uid)   # テストユーザー残骸防止（tests/_test_users.py・2026-07）
    return uid, pw


def _login(uid: str, pw: str) -> None:
    r = client.post("/auth/login", json={"username": uid, "password": pw})
    assert r.status_code == 200, f"login failed: {r.text}"


def _logout() -> None:
    client.post("/auth/logout")


def _upload(filename: str, content: bytes) -> dict:
    r = client.post(
        "/workspace/files",
        files={"file": (filename, io.BytesIO(content), "text/plain")},
    )
    return r


# ===== W3 テスト =====

def test_w3_invalid_uid_rejected_by_db():
    """W3: 不正 uid の直接 SQL insert は DB CHECK 制約で拒否される。"""
    if not _try_init():
        pytest.skip("DB down")
    import psycopg

    # 不正 uid のサンプル（パストラバーサル・スラッシュ含み・空文字）。
    bad_uids = ["../evil", "a/b", "../../etc", ""]
    dsn = store._dsn()
    for bad in bad_uids:
        try:
            with psycopg.connect(dsn) as c:
                c.execute(
                    "INSERT INTO users (uid, role, status) VALUES (%s, 'user', 'active')",
                    (bad,),
                )
            # 例外なしなら制約が効いていない（FAIL）。
            assert False, f"W3 FAIL: bad uid '{bad}' was accepted by DB (constraint not enforced)"
        except psycopg.errors.CheckViolation:
            pass  # 期待通り: CHECK 違反で拒否
        except psycopg.errors.UniqueViolation:
            pass  # 空文字が UNIQUE 側で弾かれるケースも許容
        except psycopg.Error as e:
            # その他の DB エラー（例: integrity violation）も拒否扱いとする。
            err_str = str(e)
            assert ("check" in err_str.lower() or "constraint" in err_str.lower()
                    or "unique" in err_str.lower() or "null" in err_str.lower()), \
                f"W3 FAIL: unexpected DB error for bad uid '{bad}': {e}"


def test_w3_valid_uid_accepted_by_db():
    """W3: 正当な uid は DB に insert できる。"""
    if not _try_init():
        pytest.skip("DB down")
    import psycopg

    sfx = _sfx()
    valid_uids = [f"user{sfx}", f"u.{sfx}", f"u-{sfx}", f"u_{sfx}"]
    dsn = store._dsn()
    for uid in valid_uids:
        register_test_uid(uid)   # テストユーザー残骸防止（生 SQL insert は _mk_user を経由しないため個別登録）
        try:
            with psycopg.connect(dsn) as c:
                c.execute(
                    "INSERT INTO users (uid, role, status) VALUES (%s, 'user', 'active')",
                    (uid,),
                )
        except psycopg.errors.CheckViolation:
            assert False, f"W3 FAIL: valid uid '{uid}' was rejected by DB CHECK constraint"
        except psycopg.errors.UniqueViolation:
            pass  # 既存行との衝突は OK（制約は通過している）
        except psycopg.Error as e:
            # CHECK 以外のエラーは制約の問題ではないので許容（例: FK）。
            if "check" in str(e).lower():
                assert False, f"W3 FAIL: valid uid '{uid}' rejected: {e}"


def test_w3_constraint_is_validated():
    """W3: users_uid_format 制約が存在し、VALIDATE 済みであること（convalidated=true）。"""
    if not _try_init():
        pytest.skip("DB down")
    import psycopg

    dsn = store._dsn()
    with psycopg.connect(dsn) as c:
        row = c.execute(
            "SELECT conname, convalidated FROM pg_constraint "
            "WHERE conname = 'users_uid_format' AND conrelid = 'users'::regclass",
        ).fetchone()
    # row はタプル（psycopg デフォルト）または dict。
    assert row is not None, "W3 FAIL: users_uid_format constraint not found in pg_constraint"
    # convalidated は 2 番目の列（index 1）または dict キー。
    validated = row[1] if isinstance(row, tuple) else row.get("convalidated", row.get(1))
    assert validated, "W3 FAIL: users_uid_format constraint exists but NOT VALIDATED (convalidated=false)"


def test_w3_null_uid_rejected_by_db():
    """W3 HIGH-1 fix: NULL uid の直接 SQL insert は DB CHECK 制約で拒否される。

    Postgres の CHECK は NULL/UNKNOWN をデフォルトで通してしまう。
    修正後の制約は uid IS NOT NULL を含むため NULL も弾く。
    """
    if not _try_init():
        pytest.skip("DB down")
    import psycopg

    dsn = store._dsn()
    rejected = False
    try:
        with psycopg.connect(dsn) as c:
            c.execute(
                "INSERT INTO users (uid, role, status) VALUES (NULL, 'user', 'active')",
            )
        # 例外なしなら制約が NULL を通している（FAIL）。
        assert False, "W3 HIGH-1 FAIL: NULL uid was accepted by DB (CHECK constraint passes NULL)"
    except psycopg.errors.CheckViolation:
        rejected = True   # 期待通り: IS NOT NULL 条件で弾かれた
    except psycopg.errors.NotNullViolation:
        rejected = True   # users.uid が NOT NULL 列に変わっていれば DB 側で弾く（どちらでも正解）
    except psycopg.Error as e:
        # NOT NULL 制約 or CHECK 制約いずれかで拒否されるはず。
        err_str = str(e).lower()
        rejected = ("null" in err_str or "check" in err_str or "constraint" in err_str)
        assert rejected, f"W3 HIGH-1 FAIL: unexpected DB error for NULL uid: {e}"

    assert rejected, "W3 HIGH-1 FAIL: NULL uid insert did not raise any DB error"


# ===== W4 テスト =====

def test_w4_upload_sets_expires_at():
    """W4: アップロード時に expires_at が設定される（TTL_DAYS > 0）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = _mk_user(sfx)
    _login(uid, pw)

    r = _upload(f"ttl_upload_{sfx}.txt", b"ttl test content")
    assert r.status_code == 200, r.text

    # 一覧で expires_at が設定されていることを確認。
    r = client.get("/workspace/files")
    assert r.status_code == 200, r.text
    files = r.json()["files"]
    assert files, "no files in list"
    f = files[0]
    assert "expires_at" in f, "expires_at missing from file list"
    assert f["expires_at"] is not None, "expires_at is None (expected non-null for TTL_DAYS=90)"
    # おおよそ 90 日後であることを確認（±1 日の誤差許容）。
    exp = datetime.fromisoformat(f["expires_at"].replace(" ", "T").split("+")[0])
    now = datetime.utcnow()
    diff_days = (exp - now).days
    assert 88 <= diff_days <= 92, f"expires_at is not ~90 days from now: diff={diff_days} days"

    _logout()


def test_w4_null_expiry_file_not_swept():
    """W4: expires_at=NULL（無期限）のファイルは sweep で削除されない。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = _mk_user(sfx)

    # 台帳に expires_at=NULL で直接登録（無期限ファイル）。
    from sherpa.api import ensure_workspace
    files_dir = ensure_workspace(uid) / "files"
    fname = f"no_expiry_{sfx}.txt"
    content = b"no expiry content"
    physical = files_dir / fname
    physical.write_bytes(content)
    row = store.record_workspace_file(uid, fname, str(physical), len(content),
                                      "abc123", expires_at=None)
    fid = row["id"]

    # sweep を実行。
    result = _sweep_expired_workspace()
    assert result.get("skipped") != "db_unreachable", "DB should be reachable"

    # ファイルが台帳に 'uploaded' のまま残っている。
    wf = store.get_workspace_file(uid, fid)
    assert wf is not None, "W4 FAIL: no-expiry file was removed from ledger by sweep"
    assert wf["status"] == "uploaded", f"W4 FAIL: no-expiry file status changed to {wf['status']}"

    # 物理ファイルも残っている。
    assert physical.exists(), "W4 FAIL: no-expiry physical file was deleted by sweep"

    # クリーンアップ。
    store.delete_workspace_file(uid, fid)
    physical.unlink(missing_ok=True)


def test_w4_expired_file_is_swept():
    """W4: 期限切れファイルは sweep で物理削除 + 台帳 status='expired' になる。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = _mk_user(sfx)

    # 台帳に過去の expires_at で登録（即時期限切れ）。
    from sherpa.api import ensure_workspace
    files_dir = ensure_workspace(uid) / "files"
    fname = f"expired_{sfx}.txt"
    content = b"expired content"
    physical = files_dir / fname
    physical.write_bytes(content)

    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    row = store.record_workspace_file(uid, fname, str(physical), len(content),
                                      "def456", expires_at=past)
    fid = row["id"]

    # 物理ファイルが存在することを確認。
    assert physical.exists(), "setup: physical file not created"

    # sweep を実行。
    result = _sweep_expired_workspace()
    assert result.get("skipped") != "db_unreachable", "DB should be reachable"
    assert result.get("deleted", 0) >= 1, \
        f"W4 FAIL: sweep did not delete expired file (result={result})"

    # 台帳が status='expired' になっている。
    import psycopg
    with psycopg.connect(store._dsn()) as c:
        ledger = c.execute(
            "SELECT status FROM personal_workspace_files WHERE id=%s", (fid,)
        ).fetchone()
    assert ledger is not None, "W4 FAIL: ledger row disappeared"
    status = ledger[0] if isinstance(ledger, tuple) else ledger["status"]
    assert status == "expired", f"W4 FAIL: expected status='expired', got '{status}'"

    # 物理ファイルが削除されている。
    assert not physical.exists(), "W4 FAIL: physical file still exists after sweep"


def test_w4_disabled_user_file_preserved():
    """W4: 無効化（disabled）ユーザーの期限切れファイルは sweep で削除されない。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = _mk_user(sfx, status="disabled")

    # 台帳に過去の expires_at で登録。
    from sherpa.api import ensure_workspace
    files_dir = ensure_workspace(uid) / "files"
    fname = f"disabled_exp_{sfx}.txt"
    content = b"disabled user content"
    physical = files_dir / fname
    physical.write_bytes(content)

    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    row = store.record_workspace_file(uid, fname, str(physical), len(content),
                                      "ghi789", expires_at=past)
    fid = row["id"]

    # sweep を実行（disabled ユーザーの行は除外される）。
    result = _sweep_expired_workspace()
    assert result.get("skipped") != "db_unreachable", "DB should be reachable"

    # 台帳行が 'uploaded' のまま（expired_workspace_files が除外しているので sweep 対象外）。
    import psycopg
    with psycopg.connect(store._dsn()) as c:
        ledger = c.execute(
            "SELECT status FROM personal_workspace_files WHERE id=%s", (fid,)
        ).fetchone()
    assert ledger is not None, "ledger row disappeared"
    status = ledger[0] if isinstance(ledger, tuple) else ledger["status"]
    assert status == "uploaded", \
        f"W4 FAIL: disabled user's expired file was swept (status={status})"

    # 物理ファイルが残っている。
    assert physical.exists(), "W4 FAIL: disabled user's physical file was deleted by sweep"

    # クリーンアップ。
    store.mark_workspace_file_expired(fid)
    physical.unlink(missing_ok=True)


def test_w4_sweep_base_confined():
    """W4: sweep はベース外のパスを削除しない（base-confined）。

    LOW fix: sentinel ファイルを /tmp に実際に作成してから sweep を実行し、
    sweep 後も sentinel が残っていることを検証する。
    （sweep がベース外の unlink を試みたとしても sentinel が消えていれば失敗）
    """
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = _mk_user(sfx)

    # /tmp に sentinel ファイルを実際に作成する（sweep がここを触らないことを確認するため）。
    import tempfile as _tempfile
    sentinel_fd, outside_path = _tempfile.mkstemp(prefix=f"sherpa_sentinel_{sfx}_", suffix=".txt")
    import os as _os
    _os.close(sentinel_fd)
    pathlib.Path(outside_path).write_bytes(b"sentinel content - must not be deleted by sweep")
    assert pathlib.Path(outside_path).exists(), "sentinel file must exist before sweep"

    past = datetime.now(timezone.utc) - timedelta(seconds=1)

    import psycopg
    with psycopg.connect(store._dsn()) as c:
        row = c.execute(
            "INSERT INTO personal_workspace_files "
            "  (user_id, rel_path, original_path, size_bytes, sha256, status, expires_at) "
            "VALUES (%s, %s, %s, 100, 'fakehash', 'uploaded', %s) "
            "RETURNING id",
            (uid, f"outside_{sfx}.txt", outside_path, past),
        ).fetchone()
    fid = row[0] if isinstance(row, tuple) else row["id"]

    try:
        # sweep を実行。
        result = _sweep_expired_workspace()
        assert result.get("skipped") != "db_unreachable", "DB should be reachable"

        # 核心の確認: sentinel ファイルが sweep 後も存在する（ベース外は unlink されない）。
        assert pathlib.Path(outside_path).exists(), \
            f"LOW FAIL: sentinel file outside files_dir was deleted by sweep: {outside_path}"
    finally:
        # クリーンアップ（sentinel + 台帳行）。
        pathlib.Path(outside_path).unlink(missing_ok=True)
        with psycopg.connect(store._dsn()) as c:
            c.execute("DELETE FROM personal_workspace_files WHERE id=%s", (fid,))


def test_w4_sweep_db_unreachable_no_deletion():
    """W4: DB 接続不可時は sweep が何も削除しない（fail-safe）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = _mk_user(sfx)

    # 台帳に期限切れファイルを登録。
    from sherpa.api import ensure_workspace
    files_dir = ensure_workspace(uid) / "files"
    fname = f"failsafe_{sfx}.txt"
    content = b"failsafe content"
    physical = files_dir / fname
    physical.write_bytes(content)

    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    row = store.record_workspace_file(uid, fname, str(physical), len(content),
                                      "jkl000", expires_at=past)
    fid = row["id"]

    # expired_workspace_files に例外を投げるモンキーパッチで DB 障害を模擬。
    import sherpa.store as _store
    original_fn = _store.expired_workspace_files

    def _broken():
        raise RuntimeError("simulated DB failure")

    _store.expired_workspace_files = _broken
    try:
        result = _sweep_expired_workspace()
    finally:
        _store.expired_workspace_files = original_fn

    assert result.get("skipped") == "db_unreachable", \
        f"W4 FAIL: sweep did not return skipped=db_unreachable (result={result})"

    # 物理ファイルが残っている。
    assert physical.exists(), "W4 FAIL: physical file was deleted even when DB was unreachable"

    # クリーンアップ。
    store.mark_workspace_file_expired(fid)
    physical.unlink(missing_ok=True)


def test_w4_sweep_rag_isolation():
    """W4: sweep は ES/Neo4j を参照しない（不変条件）。

    コメント行・docstring（先頭のトリプルクォート文字列ブロック）を除いたコードで確認。
    """
    import ast
    import inspect
    import textwrap
    from sherpa.api import _sweep_expired_workspace as _fn
    src = inspect.getsource(_fn)
    # コメント行（# で始まる）を除去。
    code_lines = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
    code_no_comments = "\n".join(code_lines)
    # AST でパースして docstring（先頭 Expr(Constant)）を除いたコードを得る。
    try:
        tree = ast.parse(textwrap.dedent(code_no_comments))
        # 関数定義のボディ先頭が Expr(Constant) = docstring なら除去する。
        fn_def = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)), None)
        if fn_def and fn_def.body and isinstance(fn_def.body[0], ast.Expr) and \
                isinstance(fn_def.body[0].value, ast.Constant):
            # docstring の行番号範囲を特定して除去。
            ds_node = fn_def.body[0]
            ds_lines_to_remove = set(range(ds_node.lineno, (ds_node.end_lineno or ds_node.lineno) + 1))
            code_lines_indexed = list(enumerate(code_no_comments.splitlines(), start=1))
            code_only = "\n".join(
                ln for lno, ln in code_lines_indexed if lno not in ds_lines_to_remove
            )
        else:
            code_only = code_no_comments
    except SyntaxError:
        # パース失敗時はコメントなし版で継続（フォールバック）。
        code_only = code_no_comments
    assert "es_index" not in code_only, \
        "W4 RAG isolation FAIL: _sweep_expired_workspace references es_index"
    assert "world_graph" not in code_only, \
        "W4 RAG isolation FAIL: _sweep_expired_workspace references world_graph"
    assert "neo4j" not in code_only.lower(), \
        "W4 RAG isolation FAIL: _sweep_expired_workspace references neo4j"


def test_gc_orphan_deletes_untracked_file():
    """W4-GC: 台帳に無い物理ファイル（孤児）は GC で削除される。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa.api import ensure_workspace, _gc_orphan_workspace_files
    sfx = _sfx()
    uid, _ = _mk_user(sfx)
    files_dir = ensure_workspace(uid) / "files"
    orphan = files_dir / f"orphan_{sfx}.txt"
    orphan.write_bytes(b"no ledger row")           # 台帳登録なし = 孤児
    assert orphan.exists()
    res = _gc_orphan_workspace_files()
    assert res.get("skipped") != "db_unreachable"
    assert not orphan.exists(), f"GC did not delete orphan (res={res})"


def test_gc_orphan_keeps_live_ledger_file():
    """W4-GC: 台帳に生きている（status='uploaded'）ファイルは GC で消さない。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa.api import ensure_workspace, _gc_orphan_workspace_files
    sfx = _sfx()
    uid, _ = _mk_user(sfx)
    files_dir = ensure_workspace(uid) / "files"
    fname = f"live_{sfx}.txt"; content = b"tracked"
    physical = files_dir / fname
    physical.write_bytes(content)
    store.record_workspace_file(uid, fname, str(physical), len(content), "abc123", expires_at=None)
    _gc_orphan_workspace_files()
    assert physical.exists(), "GC wrongly deleted a live ledger-tracked file"


def test_gc_orphan_disabled_user_preserved():
    """W4-GC: 無効化ユーザーの領域は GC で触らない（孤児でも保持）。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa.api import ensure_workspace, _gc_orphan_workspace_files
    sfx = _sfx()
    uid, _ = _mk_user(sfx, status="disabled")
    files_dir = ensure_workspace(uid) / "files"
    orphan = files_dir / f"orphan_{sfx}.txt"
    orphan.write_bytes(b"disabled user data")
    _gc_orphan_workspace_files()
    assert orphan.exists(), "GC deleted a disabled user's file (should preserve)"


def test_gc_orphan_db_unreachable_no_deletion():
    """W4-GC: DB 不可なら孤児でも削除しない（fail-safe）。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa.api import ensure_workspace
    import sherpa.api as api_mod
    sfx = _sfx()
    uid, _ = _mk_user(sfx)
    files_dir = ensure_workspace(uid) / "files"
    orphan = files_dir / f"orphan_{sfx}.txt"
    orphan.write_bytes(b"keep on db failure")
    orig = store.get_user
    def _broken(_uid):
        raise RuntimeError("db down")
    store.get_user = _broken
    try:
        api_mod._gc_orphan_workspace_files()
    finally:
        store.get_user = orig
    assert orphan.exists(), "GC deleted file while DB unreachable (fail-safe broken)"


def test_gc_orphan_unknown_user_preserved():
    """W4-GC RV BLOCKER: DB に居ない uid（get_user=None）の領域は削除しない（fail-safe）。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa.api import _gc_orphan_workspace_files, _USERS_DIR
    sfx = _sfx()
    uid = f"ghost{sfx}"                                # users 台帳に作らない = unknown
    files_dir = _USERS_DIR / uid / "workspace" / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    orphan = files_dir / f"orphan_{sfx}.txt"
    orphan.write_bytes(b"unknown user data")
    _gc_orphan_workspace_files()
    assert orphan.exists(), "GC deleted an unknown user's file (BLOCKER: must preserve)"


# ===== フェーズ7 S5-1: claim_workspace_file_expired の直接テスト（残ギャップ）=====

def test_w4_claim_double_call_second_returns_none():
    """W4: 二重 claim（並行 sweep worker の直列化）。1回目は行を claim・2回目は None
    （status が既に 'uploaded' でなくなっているため WHERE 条件に一致しない）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = _mk_user(sfx)
    from sherpa.api import ensure_workspace
    files_dir = ensure_workspace(uid) / "files"
    fname = f"doubleclaim_{sfx}.txt"
    content = b"double claim content"
    physical = files_dir / fname
    physical.write_bytes(content)

    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    row = store.record_workspace_file(uid, fname, str(physical), len(content),
                                      "dc1", expires_at=past)
    fid = row["id"]

    first = store.claim_workspace_file_expired(fid)
    assert first is not None, "1回目の claim が失敗した（期限切れ行のはずが不成立）"
    assert first["id"] == fid and first["user_id"] == uid and first["rel_path"] == fname

    second = store.claim_workspace_file_expired(fid)
    assert second is None, (
        f"W4 二重claim FAIL: 2回目の claim が None でなく行を返した（並行 sweep の直列化が壊れている）: {second}"
    )

    # クリーンアップ（既に status='expired' なので物理ファイルだけ消す）。
    physical.unlink(missing_ok=True)


def test_w4_reupload_after_claim_revives_same_ledger_row():
    """W4: claim 後の再アップロード。record_workspace_file の ON CONFLICT (user_id, rel_path)
    経路は行を新規作成せず、claim 済み（status='expired'）の**同じ行**を status='uploaded'・
    deleted_at=NULL に戻す（id は不変）。これにより no_live_upload_for_path は「生きた行あり」を
    正しく検出し、sweep の再アップロード race ガードが機能する前提が成立する。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = _mk_user(sfx)
    from sherpa.api import ensure_workspace
    files_dir = ensure_workspace(uid) / "files"
    fname = f"reupload_{sfx}.txt"
    physical = files_dir / fname
    physical.write_bytes(b"first content")

    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    row = store.record_workspace_file(uid, fname, str(physical), 13, "ru1", expires_at=past)
    fid = row["id"]

    claimed = store.claim_workspace_file_expired(fid)
    assert claimed is not None, "claim が不成立（期限切れ行のはず）"

    # claim 直後は台帳から見えない（status='expired'・deleted_at 設定済み）。
    assert store.get_workspace_file(uid, fid) is None, "claim 後も台帳から生きて見えている（claim が効いていない）"

    # 再アップロード（同じ rel_path） → ON CONFLICT DO UPDATE で同じ行が復活する。
    physical.write_bytes(b"second content after reupload")
    revived = store.record_workspace_file(uid, fname, str(physical), 30, "ru2", expires_at=None)
    assert revived["id"] == fid, (
        f"W4 再アップロード FAIL: ON CONFLICT が新規行を作った（id={revived['id']} != claim済み行 id={fid}）"
    )
    assert revived["status"] == "uploaded"

    wf = store.get_workspace_file(uid, fid)
    assert wf is not None and wf["status"] == "uploaded", "再アップロード後に台帳から生きて見えない"

    # sweep が使う「生きた行があるか」ガードは False（=生きた行あり=unlink 禁止）を返すはず。
    assert store.no_live_upload_for_path(uid, fname) is False, (
        "W4 FAIL: 再アップロード後も no_live_upload_for_path が True（sweep の re-upload ガードが機能しない）"
    )

    # クリーンアップ。
    store.delete_workspace_file(uid, fid)
    physical.unlink(missing_ok=True)


def test_w4_sweep_skips_physical_delete_when_reupload_races_claim():
    """W4: claim 成功直後（sweep の lock 取得前）に再アップロードが割り込んだレースを実際に
    起こし、_sweep_expired_workspace がその物理ファイル（＝再アップロードの新しい内容）を
    unlink しないことを確認する（no_live_upload_for_path による race ガードのエンドツーエンド確認）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = _mk_user(sfx)
    from sherpa.api import ensure_workspace
    files_dir = ensure_workspace(uid) / "files"
    fname = f"race_{sfx}.txt"
    physical = files_dir / fname
    physical.write_bytes(b"original content")

    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    row = store.record_workspace_file(uid, fname, str(physical), 17, "race1", expires_at=past)
    fid = row["id"]

    new_content = b"new content written by racing reupload"

    orig_claim = store.claim_workspace_file_expired

    def _racing_claim(file_id):
        result = orig_claim(file_id)
        if result is not None and file_id == fid:
            # claim 成功直後・sweep がまだ lock/unlink していないタイミングで再アップロードが割り込む。
            physical.write_bytes(new_content)
            store.record_workspace_file(uid, fname, str(physical), len(new_content), "race2", expires_at=None)
        return result

    import sherpa.store as _store
    _store.claim_workspace_file_expired = _racing_claim
    try:
        result = _sweep_expired_workspace()
    finally:
        _store.claim_workspace_file_expired = orig_claim

    assert result.get("skipped") != "db_unreachable", "DB should be reachable"

    # 核心: race ガードにより unlink されず、再アップロードの新しい内容がそのまま残る。
    assert physical.exists(), "W4 race FAIL: 再アップロードされたばかりの物理ファイルが sweep で消された"
    assert physical.read_bytes() == new_content, "W4 race FAIL: 物理ファイルの内容が再アップロード後のものでない"

    # 台帳も再アップロード後の 'uploaded' のまま（claim による 'expired' には戻っていない）。
    wf = store.get_workspace_file(uid, fid)
    assert wf is not None and wf["status"] == "uploaded", "W4 race FAIL: 再アップロード後の行が台帳から消えている"

    # クリーンアップ。
    store.delete_workspace_file(uid, fid)
    physical.unlink(missing_ok=True)


def test_w4_claim_boundary_expires_at_equals_now_is_expired():
    """W4 境界: expires_at == now() ちょうどの行は「期限切れ」扱い（claim される）。

    claim_workspace_file_expired（sherpa/store/workspace_files.py）の SQL 条件は
    `expires_at <= now()`（`<` ではなく `<=`）＝境界を含む。この等号境界を実際の関数呼び出し
    2回（別トランザクション＝別 now()）で再現するのは実時間が必ず前進するため不可能なので、
    同一トランザクション内で now() を2回参照する（Postgres の now() はトランザクション開始時刻で
    安定＝同一トランザクション内では同じ値を返す）ことで expires_at を "ちょうど" now() と
    厳密に等しくし、claim_workspace_file_expired と同一の WHERE 条件で判定させる。
    """
    if not _try_init():
        pytest.skip("DB down")
    import psycopg
    sfx = _sfx()
    uid, pw = _mk_user(sfx)
    from sherpa.api import ensure_workspace
    files_dir = ensure_workspace(uid) / "files"
    fname = f"boundary_{sfx}.txt"
    physical = files_dir / fname
    physical.write_bytes(b"boundary content")

    dsn = store._dsn()
    with psycopg.connect(dsn) as c:
        # INSERT と UPDATE を同一トランザクション内（同一コネクション・未 commit）で行うことで
        # 両方の now() が同一値になる。
        ins = c.execute(
            "INSERT INTO personal_workspace_files "
            "  (user_id, rel_path, original_path, size_bytes, sha256, status, expires_at) "
            "VALUES (%s, %s, %s, 17, 'boundary', 'uploaded', now()) "
            "RETURNING id",
            (uid, fname, str(physical)),
        ).fetchone()
        fid = ins[0] if isinstance(ins, tuple) else ins["id"]

        # claim_workspace_file_expired（sherpa/store/workspace_files.py）と同一の WHERE 条件。
        claimed = c.execute(
            "UPDATE personal_workspace_files p "
            "SET status='expired', deleted_at=now() "
            "FROM users u "
            "WHERE p.id = %s "
            "  AND p.user_id = u.uid "
            "  AND p.status = 'uploaded' "
            "  AND p.deleted_at IS NULL "
            "  AND p.expires_at IS NOT NULL "
            "  AND p.expires_at <= now() "
            "  AND u.status <> 'disabled' "
            "RETURNING p.id",
            (fid,),
        ).fetchone()

    assert claimed is not None, (
        "W4 境界 FAIL: expires_at == now() ちょうどの行が claim されなかった "
        "（実装の比較演算子は `<=` なので境界は「期限切れ」扱いのはず）"
    )

    # クリーンアップ（トランザクションは with ブロック終了時に commit 済み）。
    with psycopg.connect(dsn) as c:
        c.execute("DELETE FROM personal_workspace_files WHERE id=%s", (fid,))
    physical.unlink(missing_ok=True)


def test_w4_boundary_operator_pinned_to_implementation():
    """RV MED（2026-07-14 フェーズ7 1巡目）: 上の境界テストは実装の WHERE 条件を同一トランザクション
    内 SQL として**複製**している（実関数は別トランザクション＝別 now() になるため「ちょうど」を
    作れない・複製が唯一の現実的手法）。複製と実装が乖離する退行（`<=` が `<` に変わる等）を
    検知するため、実装ソースに期待する比較式が現存することを pin する。この pin が落ちたら、
    実装の意図変更を確認のうえ**上の境界テストの複製 SQL と本 pin を同時に更新**すること。"""
    import inspect

    from sherpa.store import workspace_files as _wf

    # RV MED（2巡目）: 両関数の docstring にも裸の 'expires_at <= now()' が含まれるため、裸の
    # 部分文字列検査では「SQL だけ < に変えて docstring を残す」退行を素通りする。実 SQL にしか
    # 現れない **テーブル alias 込み**の形（f./p.）で検査する。
    for fn, aliased in ((_wf.expired_workspace_files, "f.expires_at <= now()"),
                        (_wf.claim_workspace_file_expired, "p.expires_at <= now()")):
        src = inspect.getsource(fn)
        assert aliased in src, (
            f"{fn.__name__} の期限判定 SQL が '{aliased}' から変わった＝"
            "本ファイルの境界テスト（複製 SQL）が実装とズレている可能性。両方を同時に更新すること"
        )


def test_gc_orphan_parent_symlink_rejected():
    """W4-GC RV HIGH: workspace/ が symlink の場合は触らない（symlink 先の外部削除を防ぐ）。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa.api import _gc_orphan_workspace_files, _USERS_DIR
    sfx = _sfx()
    uid, _ = _mk_user(sfx)
    # 外部に「餌」ファイルを置き、workspace を そこ配下への symlink にする
    outside = pathlib.Path(tempfile.mkdtemp(prefix="sherpa_outside_"))
    (outside / "files").mkdir()
    bait = outside / "files" / "victim.txt"
    bait.write_bytes(b"external file must NOT be deleted")
    udir = _USERS_DIR / uid
    udir.mkdir(parents=True, exist_ok=True)
    # 既存 workspace を消して symlink に差し替え
    import shutil as _sh
    if (udir / "workspace").is_symlink() or (udir / "workspace").exists():
        if (udir / "workspace").is_symlink():
            (udir / "workspace").unlink()
        else:
            _sh.rmtree(udir / "workspace")
    (udir / "workspace").symlink_to(outside)
    _gc_orphan_workspace_files()
    assert bait.exists(), "GC followed a workspace symlink and deleted external file (HIGH)"
