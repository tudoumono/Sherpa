"""個人 workspace の uid CHECK 制約と、保持期限（TTL）による自動掃除・孤児 GC の契約。要 Postgres。"""
from __future__ import annotations

import io
import os
import pathlib
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient

from _common import _try_init
from _test_users import register_test_uid
from _store_helpers import mark_workspace_file_expired
from sherpa import auth, store
from sherpa.api import (
    _USERS_DIR,
    _gc_orphan_workspace_files,
    _sweep_expired_workspace,
    app,
    ensure_workspace,
)

# 個人 workspace ルートは conftest.py が確定させている（sherpa.api._USERS_DIR は import 時定数）。
client = TestClient(app, raise_server_exceptions=True)


@pytest.fixture(autouse=True)
def _db():
    _try_init()


def _sfx() -> str:
    return str(int(time.time() * 1000))[-8:]


def _mk_user(sfx: str, role: str = "user", status: str = "active") -> tuple[str, str]:
    uid = f"ttl{role[:1]}{sfx}"
    pw = f"pw-{uid}"
    store.upsert_user(uid, email=f"{uid}@ex.local", display_name=uid.upper(),
                      password_hash=auth.hash_password(pw), role=role, status=status)
    register_test_uid(uid)
    return uid, pw


def _past(**kw) -> datetime:
    return datetime.now(timezone.utc) - timedelta(**(kw or {"seconds": 1}))


def _put_file(uid: str, name: str, content: bytes, sha: str, expires_at) -> tuple[pathlib.Path, int]:
    """物理ファイルを置いて台帳に登録する。"""
    physical = ensure_workspace(uid) / "files" / name
    physical.write_bytes(content)
    row = store.record_workspace_file(uid, name, str(physical), len(content), sha, expires_at=expires_at)
    return physical, row["id"]


def _ledger_status(fid: int) -> str:
    with psycopg.connect(store._dsn()) as c:
        row = c.execute("SELECT status FROM personal_workspace_files WHERE id=%s", (fid,)).fetchone()
    assert row is not None, "ledger row disappeared"
    return row[0]


def _sweep() -> dict:
    result = _sweep_expired_workspace()
    assert result.get("skipped") != "db_unreachable", "DB should be reachable"
    return result


# ===== users.uid の DB CHECK 制約 =====

@pytest.mark.parametrize("bad", ["../evil", "a/b", "../../etc", "", None])
def test_invalid_uid_rejected_by_db(bad):
    """不正 uid（パス traversal・スラッシュ・空・NULL）の直接 insert は DB が拒否する。"""
    with pytest.raises(psycopg.Error) as ei:
        with psycopg.connect(store._dsn()) as c:
            c.execute("INSERT INTO users (uid, role, status) VALUES (%s, 'user', 'active')", (bad,))
    assert isinstance(ei.value, (psycopg.errors.CheckViolation, psycopg.errors.UniqueViolation,
                                 psycopg.errors.NotNullViolation)) or any(
        k in str(ei.value).lower() for k in ("check", "constraint", "unique", "null"))


@pytest.mark.parametrize("fmt", ["user{}", "u.{}", "u-{}", "u_{}"])
def test_valid_uid_accepted_by_db(fmt):
    uid = fmt.format(_sfx())
    register_test_uid(uid)
    try:
        with psycopg.connect(store._dsn()) as c:
            c.execute("INSERT INTO users (uid, role, status) VALUES (%s, 'user', 'active')", (uid,))
    except psycopg.errors.CheckViolation:
        pytest.fail(f"valid uid '{uid}' was rejected by DB CHECK constraint")
    except psycopg.errors.UniqueViolation:
        pass


def test_uid_constraint_is_validated():
    with psycopg.connect(store._dsn()) as c:
        row = c.execute(
            "SELECT conname, convalidated FROM pg_constraint "
            "WHERE conname = 'users_uid_format' AND conrelid = 'users'::regclass",
        ).fetchone()
    assert row is not None, "users_uid_format constraint not found"
    assert row[1], "users_uid_format exists but NOT VALIDATED"


# ===== 期限切れファイルの掃除 =====

def test_upload_sets_expires_at_about_90_days():
    uid, pw = _mk_user(_sfx())
    r = client.post("/auth/login", json={"username": uid, "password": pw})
    assert r.status_code == 200, r.text
    r = client.post("/workspace/files", files={"file": ("ttl_upload.txt", io.BytesIO(b"ttl"), "text/plain")})
    assert r.status_code == 200, r.text
    r = client.get("/workspace/files")
    assert r.status_code == 200, r.text
    files = r.json()["files"]
    assert files, "no files in list"
    assert files[0].get("expires_at") is not None
    exp = datetime.fromisoformat(files[0]["expires_at"].replace(" ", "T").split("+")[0])
    assert 88 <= (exp - datetime.utcnow()).days <= 92
    client.post("/auth/logout")


def test_sweep_expires_only_expired_files_of_active_users():
    """期限切れ→物理削除＋status='expired'。無期限・無効化ユーザー分は残る。"""
    sfx = _sfx()
    uid, _ = _mk_user(sfx)
    dis_uid, _ = _mk_user(sfx + "d", status="disabled")
    keep, keep_id = _put_file(uid, f"no_expiry_{sfx}.txt", b"no expiry", "abc123", None)
    gone, gone_id = _put_file(uid, f"expired_{sfx}.txt", b"expired", "def456", _past())
    dis, dis_id = _put_file(dis_uid, f"disabled_exp_{sfx}.txt", b"disabled", "ghi789", _past())

    result = _sweep()
    assert result.get("deleted", 0) >= 1, result

    assert _ledger_status(gone_id) == "expired"
    assert not gone.exists()
    assert store.get_workspace_file(uid, keep_id)["status"] == "uploaded"
    assert keep.exists()
    assert _ledger_status(dis_id) == "uploaded"
    assert dis.exists()

    store.delete_workspace_file(uid, keep_id)
    mark_workspace_file_expired(dis_id)


def test_sweep_base_confined():
    """台帳の original_path がベース外を指しても sweep は unlink しない。"""
    sfx = _sfx()
    uid, _ = _mk_user(sfx)
    fd, outside_path = tempfile.mkstemp(prefix=f"sherpa_sentinel_{sfx}_", suffix=".txt")
    os.close(fd)
    pathlib.Path(outside_path).write_bytes(b"sentinel - must not be deleted by sweep")
    with psycopg.connect(store._dsn()) as c:
        fid = c.execute(
            "INSERT INTO personal_workspace_files "
            "  (user_id, rel_path, original_path, size_bytes, sha256, status, expires_at) "
            "VALUES (%s, %s, %s, 100, 'fakehash', 'uploaded', %s) RETURNING id",
            (uid, f"outside_{sfx}.txt", outside_path, _past()),
        ).fetchone()[0]
    try:
        _sweep()
        assert pathlib.Path(outside_path).exists(), "sentinel outside files_dir was deleted by sweep"
    finally:
        pathlib.Path(outside_path).unlink(missing_ok=True)
        with psycopg.connect(store._dsn()) as c:
            c.execute("DELETE FROM personal_workspace_files WHERE id=%s", (fid,))


def test_sweep_db_unreachable_no_deletion(monkeypatch):
    uid, _ = _mk_user(_sfx())
    physical, fid = _put_file(uid, "failsafe.txt", b"failsafe", "jkl000", _past())

    def _broken():
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(store, "expired_workspace_files", _broken)
    result = _sweep_expired_workspace()
    monkeypatch.undo()

    assert result.get("skipped") == "db_unreachable", result
    assert physical.exists()
    mark_workspace_file_expired(fid)


def test_sweep_does_not_reference_rag_stores():
    """RAG 隔離: sweep の実コードは ES/Neo4j/グラフを参照しない（コメント・docstring は除く）。"""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(_sweep_expired_workspace)))
    fn_def = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    if fn_def.body and isinstance(fn_def.body[0], ast.Expr) and isinstance(fn_def.body[0].value, ast.Constant):
        fn_def.body.pop(0)
    code = ast.unparse(fn_def)
    assert "es_index" not in code
    assert "world_graph" not in code
    assert "neo4j" not in code.lower()


# ===== 孤児 GC =====

def test_gc_orphan_deletes_untracked_file():
    uid, _ = _mk_user(_sfx())
    orphan = ensure_workspace(uid) / "files" / "orphan.txt"
    orphan.write_bytes(b"no ledger row")
    res = _gc_orphan_workspace_files()
    assert res.get("skipped") != "db_unreachable"
    assert not orphan.exists(), res


@pytest.mark.parametrize("kind", ["live_ledger", "disabled_user", "unknown_user"])
def test_gc_orphan_preserves(kind):
    """台帳に生きている行・無効化ユーザー・DB に居ない uid の領域は GC で消さない。"""
    sfx = _sfx()
    if kind == "live_ledger":
        uid, _ = _mk_user(sfx)
        kept, _fid = _put_file(uid, f"live_{sfx}.txt", b"tracked", "abc123", None)
    elif kind == "disabled_user":
        uid, _ = _mk_user(sfx, status="disabled")
        kept = ensure_workspace(uid) / "files" / f"orphan_{sfx}.txt"
        kept.write_bytes(b"disabled user data")
    else:
        files_dir = _USERS_DIR / f"ghost{sfx}" / "workspace" / "files"
        files_dir.mkdir(parents=True, exist_ok=True)
        kept = files_dir / f"orphan_{sfx}.txt"
        kept.write_bytes(b"unknown user data")
    _gc_orphan_workspace_files()
    assert kept.exists(), f"GC deleted a file that must be preserved ({kind})"


def test_gc_orphan_db_unreachable_no_deletion(monkeypatch):
    uid, _ = _mk_user(_sfx())
    orphan = ensure_workspace(uid) / "files" / "orphan.txt"
    orphan.write_bytes(b"keep on db failure")

    def _broken(_uid):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "get_user", _broken)
    _gc_orphan_workspace_files()
    monkeypatch.undo()
    assert orphan.exists(), "GC deleted file while DB unreachable"


def test_gc_orphan_parent_symlink_rejected():
    """workspace/ が symlink なら触らない（symlink 先の外部削除を防ぐ）。"""
    uid, _ = _mk_user(_sfx())
    outside = pathlib.Path(tempfile.mkdtemp(prefix="sherpa_outside_"))
    (outside / "files").mkdir()
    bait = outside / "files" / "victim.txt"
    bait.write_bytes(b"external file must NOT be deleted")
    udir = _USERS_DIR / uid
    udir.mkdir(parents=True, exist_ok=True)
    ws = udir / "workspace"
    if ws.is_symlink():
        ws.unlink()
    elif ws.exists():
        shutil.rmtree(ws)
    ws.symlink_to(outside)
    _gc_orphan_workspace_files()
    assert bait.exists(), "GC followed a workspace symlink and deleted external file"


# ===== claim の直列化・再アップロード・境界 =====

def test_claim_double_call_second_returns_none():
    """二重 claim（並行 sweep の直列化）: 1 回目は行を返し、2 回目は None。"""
    uid, _ = _mk_user(_sfx())
    physical, fid = _put_file(uid, "doubleclaim.txt", b"double claim", "dc1", _past())
    first = store.claim_workspace_file_expired(fid)
    assert first is not None
    assert first["id"] == fid and first["user_id"] == uid and first["rel_path"] == "doubleclaim.txt"
    assert store.claim_workspace_file_expired(fid) is None
    physical.unlink(missing_ok=True)


def test_reupload_after_claim_revives_same_ledger_row():
    """claim 後の再アップロードは同じ行（id 不変）を uploaded に戻し、no_live_upload_for_path は False。"""
    uid, _ = _mk_user(_sfx())
    physical, fid = _put_file(uid, "reupload.txt", b"first content", "ru1", _past())
    assert store.claim_workspace_file_expired(fid) is not None
    assert store.get_workspace_file(uid, fid) is None

    physical.write_bytes(b"second content after reupload")
    revived = store.record_workspace_file(uid, "reupload.txt", str(physical), 30, "ru2", expires_at=None)
    assert revived["id"] == fid
    assert revived["status"] == "uploaded"
    wf = store.get_workspace_file(uid, fid)
    assert wf is not None and wf["status"] == "uploaded"
    assert store.no_live_upload_for_path(uid, "reupload.txt") is False

    store.delete_workspace_file(uid, fid)
    physical.unlink(missing_ok=True)


def test_sweep_skips_physical_delete_when_reupload_races_claim(monkeypatch):
    """claim 直後に再アップロードが割り込んでも、sweep は新しい物理ファイルを unlink しない。"""
    uid, _ = _mk_user(_sfx())
    physical, fid = _put_file(uid, "race.txt", b"original content", "race1", _past())
    new_content = b"new content written by racing reupload"
    orig_claim = store.claim_workspace_file_expired

    def _racing_claim(file_id):
        result = orig_claim(file_id)
        if result is not None and file_id == fid:
            physical.write_bytes(new_content)
            store.record_workspace_file(uid, "race.txt", str(physical), len(new_content), "race2",
                                        expires_at=None)
        return result

    monkeypatch.setattr(store, "claim_workspace_file_expired", _racing_claim)
    _sweep()
    monkeypatch.undo()

    assert physical.exists()
    assert physical.read_bytes() == new_content
    wf = store.get_workspace_file(uid, fid)
    assert wf is not None and wf["status"] == "uploaded"

    store.delete_workspace_file(uid, fid)
    physical.unlink(missing_ok=True)


def test_claim_boundary_expires_at_equals_now_is_expired():
    """expires_at == now() ちょうどは期限切れ（`<=`）。同一トランザクション内の now() は同値のため、
    実装と同じ WHERE 条件の複製 SQL で判定する（複製のズレは次の pin テストが検知）。"""
    uid, _ = _mk_user(_sfx())
    physical = ensure_workspace(uid) / "files" / "boundary.txt"
    physical.write_bytes(b"boundary content")
    dsn = store._dsn()
    with psycopg.connect(dsn) as c:
        fid = c.execute(
            "INSERT INTO personal_workspace_files "
            "  (user_id, rel_path, original_path, size_bytes, sha256, status, expires_at) "
            "VALUES (%s, 'boundary.txt', %s, 17, 'boundary', 'uploaded', now()) RETURNING id",
            (uid, str(physical)),
        ).fetchone()[0]
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
    assert claimed is not None
    with psycopg.connect(dsn) as c:
        c.execute("DELETE FROM personal_workspace_files WHERE id=%s", (fid,))
    physical.unlink(missing_ok=True)


def test_boundary_operator_pinned_to_implementation():
    """実装の期限判定 SQL が（テーブル alias 込みで）`<= now()` のままであること。
    変わったら上の境界テストの複製 SQL も同時に更新する。"""
    import inspect

    from sherpa.store import workspace_files as _wf

    for fn, aliased in ((_wf.expired_workspace_files, "f.expires_at <= now()"),
                        (_wf.claim_workspace_file_expired, "p.expires_at <= now()")):
        assert aliased in inspect.getsource(fn), f"{fn.__name__} の期限判定 SQL が変わった"


def test_expired_unswept_row_is_hidden_from_download_list_and_live_paths():
    """掃除前でも、期限切れの uploaded 行はダウンロード（get）・一覧・live_workspace_rel_paths に出ない。"""
    sfx = _sfx()
    uid, _ = _mk_user(sfx)
    old = store.record_workspace_file(uid, f"old_{sfx}.txt", "/x/old", 1, "h1", expires_at=_past(days=1))
    store.record_workspace_file(uid, f"new_{sfx}.txt", "/x/new", 1, "h2",
                                expires_at=datetime.now(timezone.utc) + timedelta(days=1))
    store.record_workspace_file(uid, f"forever_{sfx}.txt", "/x/forever", 1, "h3", expires_at=None)
    expected = {f"new_{sfx}.txt", f"forever_{sfx}.txt"}
    assert store.get_workspace_file(uid, old["id"]) is None
    assert store.live_workspace_rel_paths(uid) == expected
    assert {r["rel_path"] for r in store.list_workspace_files(uid)} == expected
