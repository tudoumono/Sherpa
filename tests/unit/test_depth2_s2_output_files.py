"""DEPTH-2 S2（§2.7・docs/archive/2026-09-17-深さの再定義とレビュー巡.md）単体テスト。

- write_output_file ツール本体（`agentic_search._run_write_output_file`）: 個人 workspace の
  files/ 台帳（`personal_workspace_files`）へ登録・marp:true の pdf/pptx 化（marp_render はモック）。
  要 Postgres（`_try_init` が DB down を skip）。
"""
from __future__ import annotations

import os
import time

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

import pytest

from sherpa import output_files as OF
from sherpa import store

_REAL_DOC = "4期/04_運用/障害記録.md"   # fixtures/corpus/v1 実在ファイル


def _try_init() -> None:
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"DB down: {e}")


def _mk_user(tag: str) -> str:
    uid = f"unit-depth2s2-{tag}"
    store.upsert_user(uid, display_name="D2S2", password_hash="x", status="active")
    return uid


def _sfx() -> str:
    return str(int(time.time() * 1000))[-8:]


# ===== write_output_file ツール本体 =====

def test_write_output_file_registers_ledger_row_and_download_url(tmp_path, monkeypatch):
    _try_init()
    uid = _mk_user(_sfx())
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))

    result = OF._run_write_output_file({"filename": "一覧.md", "content": "# 消費税率\n8%/10%"}, uid)
    assert "error" not in result, result
    assert result["rel_path"] == "一覧.md"
    assert result["download_url"].startswith("/workspace/files/")
    assert result["download_url"].endswith("/download")

    rows = store.list_workspace_files(uid)
    assert any(r["rel_path"] == "一覧.md" for r in rows)
    dst = tmp_path / "users" / uid / "workspace" / "files" / "一覧.md"
    assert dst.read_text(encoding="utf-8") == "# 消費税率\n8%/10%"


def test_write_output_file_no_uid_is_error_and_does_not_raise():
    result = OF._run_write_output_file({"filename": "x.md", "content": "本文"}, None)
    assert "error" in result


def test_write_output_file_rejects_path_traversal_filename(tmp_path, monkeypatch):
    _try_init()
    uid = _mk_user(_sfx())
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))
    for bad in ("../escape.md", "a/b.md", "..", "."):
        result = OF._run_write_output_file({"filename": bad, "content": "x"}, uid)
        assert "error" in result, f"{bad!r} が拒否されていない: {result}"


def test_write_output_file_avoids_overwrite_with_suffix(tmp_path, monkeypatch):
    _try_init()
    uid = _mk_user(_sfx())
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))
    r1 = OF._run_write_output_file({"filename": "重複.md", "content": "1回目"}, uid)
    r2 = OF._run_write_output_file({"filename": "重複.md", "content": "2回目"}, uid)
    assert r1["rel_path"] == "重複.md"
    assert r2["rel_path"] != "重複.md"   # 上書きせず別名（重複_1.md 等）
    assert r2["rel_path"].startswith("重複")
    rows = {r["rel_path"] for r in store.list_workspace_files(uid)}
    assert r1["rel_path"] in rows and r2["rel_path"] in rows


def test_write_output_file_rejects_symlinked_workspace_of_another_user(tmp_path, monkeypatch):
    """C38 是正: `users/{uid}/workspace` が他人の workspace への symlink に差し替えられていると、
    「実体が users_dir 配下か」だけの検査（旧実装）は素通りしてしまう——実体が**本人の**
    `users_dir/{uid}/workspace/` に一致することまで検証し、他人の workspace へは書けない。"""
    _try_init()
    users_root = tmp_path / "users"
    tag = _sfx()
    uid_a, uid_b = _mk_user(tag + "a"), _mk_user(tag + "b")
    monkeypatch.setenv("SHERPA_USERS_DIR", str(users_root))

    (users_root / uid_b / "workspace").mkdir(parents=True)
    (users_root / uid_a).mkdir(parents=True)
    (users_root / uid_a / "workspace").symlink_to(users_root / uid_b / "workspace")

    result = OF._run_write_output_file({"filename": "乗っ取り.md", "content": "x"}, uid_a)
    assert "error" in result, f"他人 workspace への symlink 経由の書込みが拒否されていない: {result}"
    assert not (users_root / uid_b / "workspace" / "files" / "乗っ取り.md").exists()
    rows = store.list_workspace_files(uid_a)
    assert not any(r["rel_path"] == "乗っ取り.md" for r in rows)


def test_write_output_file_rejects_toctou_swap_of_files_dir_after_check(tmp_path, monkeypatch):
    """C42 是正: C38 の「各階層が symlink でないか確認してから resolve() で実体を照合する」検査は、
    検査（stat/resolve）と実際の作成（open/write）の間に window があり、その間に `files`（または
    その親）が他人 workspace への symlink へ差し替えられても検出できない（TOCTOU）——旧実装では
    `resolve()` が差替え後の実体を辿ってしまい、検査を素通りして他人の workspace/files へ書けた。

    ここでは `files` を開く直前（前段の `workspace` オープンという「検査」の直後）に、攻撃者が
    `files` という名前を他人 workspace の files への symlink へ差し替える窓を再現する。dir_fd による
    段階的オープン（各階層を `O_NOFOLLOW` で個別に開く）なら、差し替えられた名前を開こうとした
    瞬間に ELOOP で拒否され、他人側にはファイルが作られない。"""
    _try_init()
    users_root = tmp_path / "users"
    tag = _sfx()
    uid_a, uid_b = _mk_user(tag + "a"), _mk_user(tag + "b")
    monkeypatch.setenv("SHERPA_USERS_DIR", str(users_root))

    (users_root / uid_b / "workspace" / "files").mkdir(parents=True)
    (users_root / uid_a / "workspace").mkdir(parents=True)

    real_open_dir_fd = OF._open_workspace_dir_fd

    def _swap_files_then_open(parent_fd: int, name: str) -> int:
        if name == "files":
            # 前段（`workspace`）の検査が終わった直後・`files` を開く直前に、攻撃者が
            # `files` という名前を他人 workspace の files への symlink へ差し替える。
            (users_root / uid_a / "workspace" / "files").symlink_to(
                users_root / uid_b / "workspace" / "files")
        return real_open_dir_fd(parent_fd, name)

    monkeypatch.setattr(OF, "_open_workspace_dir_fd", _swap_files_then_open)

    result = OF._run_write_output_file({"filename": "乗っ取り2.md", "content": "x"}, uid_a)
    assert "error" in result, f"TOCTOU 差替え後も書込みが拒否されていない: {result}"
    assert not (users_root / uid_b / "workspace" / "files" / "乗っ取り2.md").exists()
    rows = store.list_workspace_files(uid_a)
    assert not any(r["rel_path"] == "乗っ取り2.md" for r in rows)


def test_write_output_file_registration_failure_does_not_leave_orphan(tmp_path, monkeypatch):
    """台帳登録（`record_workspace_file`）が失敗したら error を返し、files/ に台帳の無い
    孤児ファイルを残さない（fail-closed・Codex 経路と同じ規律）。"""
    _try_init()
    uid = _mk_user(_sfx())
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))

    def _boom(*_a, **_k):
        raise RuntimeError("db down (test)")

    monkeypatch.setattr(store, "record_workspace_file", _boom)
    result = OF._run_write_output_file({"filename": "失敗.md", "content": "x"}, uid)
    assert "error" in result
    dst = tmp_path / "users" / uid / "workspace" / "files" / "失敗.md"
    assert not dst.exists(), "登録失敗なのにファイルが残っている（孤児）"


# ===== marp:true → marp_render 経路（マージ・モック） =====

def test_write_output_file_marp_true_calls_marp_render_and_registers_rendered(tmp_path, monkeypatch):
    _try_init()
    uid = _mk_user(_sfx())
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))

    from sherpa import marp_render
    calls = {"render": 0}

    def fake_render_outputs(md_paths, *, marp_bin, chrome_path, theme_dirs, containment_root, timeout=180):
        calls["render"] += 1
        out = []
        for p in md_paths:
            pdf = p.with_suffix(".pdf")
            pdf.write_bytes(b"%PDF-fake")
            out.append(pdf)
        return out

    monkeypatch.setattr(marp_render, "is_marp_markdown", lambda p: True)
    monkeypatch.setattr(marp_render, "render_outputs", fake_render_outputs)

    result = OF._run_write_output_file(
        {"filename": "スライド.md", "content": "---\nmarp: true\n---\n# タイトル", "marp": True}, uid)

    assert "error" not in result, result
    assert calls["render"] == 1, "marp_render.render_outputs が呼ばれていない"
    assert result.get("rendered"), f"rendered が無い: {result}"
    assert result["rendered"][0]["rel_path"] == "スライド.pdf"
    assert result["rendered"][0]["download_url"].startswith("/workspace/files/")

    rows = {r["rel_path"] for r in store.list_workspace_files(uid)}
    assert "スライド.md" in rows and "スライド.pdf" in rows


def test_write_output_file_marp_render_failure_is_fail_open(tmp_path, monkeypatch):
    """marp 変換の失敗は注記だけで継続する（.md 自体の保存は成功のまま・fail-open）。"""
    _try_init()
    uid = _mk_user(_sfx())
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))

    from sherpa import marp_render

    def _boom(*_a, **_k):
        raise RuntimeError("marp crashed (test)")

    monkeypatch.setattr(marp_render, "is_marp_markdown", lambda p: True)
    monkeypatch.setattr(marp_render, "render_outputs", _boom)

    result = OF._run_write_output_file(
        {"filename": "壊れる.md", "content": "---\nmarp: true\n---\n# x", "marp": True}, uid)
    assert "error" not in result, "marp 失敗で全体がエラーになってはいけない（fail-open）"
    assert result.get("marp_note"), "marp 失敗の注記が無い"
    rows = {r["rel_path"] for r in store.list_workspace_files(uid)}
    assert "壊れる.md" in rows   # .md 自体は保存されている
