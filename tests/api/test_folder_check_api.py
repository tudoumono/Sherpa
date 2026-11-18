"""`GET /worlds/{wid}/folder-check`（フォルダの置き方の点検）。変更一覧だけ資料フォルダから読む。台帳（Postgres）が要る。
置き場の環境変数は試験ごとに設定し、同じプロセスの他の試験ファイルを巻き込まない。
"""
from __future__ import annotations

import os
import shutil

import pytest
from fastapi.testclient import TestClient

from sherpa import store
from sherpa.api import app
from sherpa.ingest.check import inspect_folder, summarize
from sherpa.routers.folder_check import NO_LEDGER_MESSAGE, READ_LIMIT_BYTES, _build_state, read_text_of

W = f"fcheck{os.getpid()}"
EMPTY_W = f"fcheckempty{os.getpid()}"
RELS = [
    "サブA/第1期/設計/画面一覧.md", "サブA/第1期/ソース/処理A.txt",
    "サブA/第2期/設計/画面一覧.md", "サブA/第2期/ソース/処理A.txt",
    "サブA/第2期/変更一覧.md",
    "サブB/設計/概要.md", "共通メモ.md",
]
CHANGE_LIST = "第2期 の変更一覧（基準: 第1期）\n\n- 設計/画面一覧.md: 変更\n"


@pytest.fixture
def kb(monkeypatch, tmp_path):
    monkeypatch.setenv("SHERPA_AUTH_DISABLED", "1")
    monkeypatch.setenv("SHERPA_KB_DIR", str(tmp_path.resolve()))
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str((tmp_path / "derived").resolve()))
    monkeypatch.setenv("SHERPA_DISABLE_EMBED", "1")
    for w in (W, EMPTY_W):
        (tmp_path / w).mkdir()
    root = tmp_path / W
    for rel in RELS:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(CHANGE_LIST if rel.endswith("変更一覧.md") else "本文", encoding="utf-8")
    store.replace_documents(W, [{"name": rel} for rel in RELS])
    store.replace_documents(EMPTY_W, [])
    yield root
    shutil.rmtree(root, ignore_errors=True)
    store.replace_documents(W, [])


@pytest.fixture
def client(kb):
    return TestClient(app, raise_server_exceptions=False)


def test_counts_match_inspect_folder(client, kb):
    r = client.get(f"/worlds/{W}/folder-check")
    assert r.status_code == 200, r.text
    body = r.json()
    direct = inspect_folder(RELS, read_text_of(W), build=_build_state(W))
    assert body["summary"] == summarize(direct.items)
    assert len(body["items"]) == len(direct.items)
    assert body["ledger_at"]


def test_markdown_download(client, kb):
    r = client.get(f"/worlds/{W}/folder-check", params={"format": "md"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert "attachment" in r.headers["content-disposition"] and "%E7%82%B9%E6%A4%9C.md" in r.headers["content-disposition"]
    assert r.text.startswith("# フォルダの点検報告")


def test_no_ledger_is_409_and_does_not_walk(client, kb, monkeypatch):
    from sherpa import scope_infer
    monkeypatch.setattr(scope_infer, "safe_files", lambda *a, **k: pytest.fail("資料フォルダを歩いた"))
    r = client.get(f"/worlds/{EMPTY_W}/folder-check")
    assert r.status_code == 409 and NO_LEDGER_MESSAGE in r.text


def test_non_admin_gets_403(kb, monkeypatch):
    from sherpa.routers import folder_check
    monkeypatch.setattr(folder_check, "_current_user", lambda request, **k: {"role": "user", "id": "u"})
    r = TestClient(app, raise_server_exceptions=False).get(f"/worlds/{W}/folder-check")
    assert r.status_code == 403


def test_read_text_decodes_cp932_and_skips_oversize_sensitive_and_outside(kb, tmp_path):
    (kb / "sjis.md").write_bytes("第2期 の変更一覧".encode("cp932"))
    (kb / "bom.md").write_bytes("﻿第1期".encode("utf-8"))
    (kb / "big.md").write_bytes(b"a" * (READ_LIMIT_BYTES + 1))
    (kb / "credentials.xlsx").write_bytes(b"secret")
    outside = tmp_path.parent / f"{W}_outside.txt"
    outside.write_text("外の本文", encoding="utf-8")
    try:
        (kb / "link.md").symlink_to(outside)
    except OSError as e:
        pytest.skip(f"symlink 非対応の環境: {e}")
    rd = read_text_of(W)
    assert rd("sjis.md") == "第2期 の変更一覧"
    assert rd("bom.md") == "第1期"
    assert rd("big.md") is None
    assert rd("credentials.xlsx") is None
    assert rd("link.md") is None
    assert rd("../" + outside.name) is None
    assert rd("無い.md") is None
    outside.unlink()


def test_build_marks_save_and_reject_missing_place(client, kb):
    r = client.put("/admin/build-marks", json={"world": W, "marks": [{"path": "サブA/第2期/ソース", "mark": "read"}]})
    assert r.status_code == 200, r.text
    got = client.get("/admin/build-marks", params={"world": W}).json()
    assert got["marks"] == [{"path": "サブA/第2期/ソース", "mark": "read"}]
    assert "サブA/第1期/ソース" in got["layout_folders"]
    assert client.put("/admin/build-marks", json={"world": W, "marks": [{"path": "無い場所", "mark": "read"}]}).status_code == 422
    assert client.put("/admin/build-marks", json={"world": W, "marks": [{"path": "サブA", "mark": "?"}]}).status_code == 422
    assert client.put("/admin/build-marks", json={"world": W, "marks": []}).status_code == 200
