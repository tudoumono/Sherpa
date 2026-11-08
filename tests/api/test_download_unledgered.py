"""原本ダウンロードは、台帳に無くても出典の確かめ（`verify_doc_exists`）と同じ条件を満たす資料なら配信する。
取り込みの後に置かれた資料が、出典に載ってリンクが付いたのに 404 になる食い違いを固定する。
台帳（Postgres）が要る。置き場の環境変数は試験ごとに設定し、同じプロセスの他の試験ファイルを巻き込まない。
"""
from __future__ import annotations

import atexit
import os
import pathlib
import shutil
import tempfile

import pytest

_KB = os.path.realpath(tempfile.mkdtemp(prefix="sherpa_kb_"))
_DER = tempfile.mkdtemp(prefix="sherpa_der_")
atexit.register(lambda: (shutil.rmtree(_KB, ignore_errors=True), shutil.rmtree(_DER, ignore_errors=True)))

from fastapi.testclient import TestClient   # noqa: E402

from sherpa import store   # noqa: E402
from sherpa.api import app   # noqa: E402

W = f"dlunledger{os.getpid()}"
_CLIENT = TestClient(app)
_ROOT = pathlib.Path(_KB) / W
LEDGERED = "設計/台帳あり.txt"
LATE = "設計/取り込み後.txt"
_BODY = "取り込み後に置いた本文_ZZ1"


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    monkeypatch.setenv("SHERPA_AUTH_DISABLED", "1")
    monkeypatch.setenv("SHERPA_KB_DIR", _KB)          # 同じプロセスの他の試験ファイルが書き換えても、この試験の置き場を使う
    monkeypatch.setenv("SHERPA_DERIVED_DIR", _DER)
    monkeypatch.setenv("SHERPA_DISABLE_EMBED", "1")
    (_ROOT / "設計").mkdir(parents=True, exist_ok=True)
    (_ROOT / LEDGERED).write_text("台帳にある本文", encoding="utf-8")
    store.replace_documents(W, [{"name": LEDGERED}])
    yield
    shutil.rmtree(_ROOT, ignore_errors=True)


def _get(rel):
    return _CLIENT.get("/documents/download", params={"rel": rel, "world": W})


def _downloads(rel):
    rows = store.list_audit(action="document.downloaded", resource_id=f"{W}:{rel}")
    return len(rows)


def test_unledgered_existing_document_is_served_and_audited():
    (_ROOT / LATE).write_text(_BODY, encoding="utf-8")
    before = _downloads(LATE)
    r = _get(LATE)
    assert r.status_code == 200 and r.content.decode("utf-8") == _BODY
    assert _downloads(LATE) == before + 1


def test_ledgered_document_is_served_as_before():
    r = _get(LEDGERED)
    assert r.status_code == 200 and r.content.decode("utf-8") == "台帳にある本文"


def test_unledgered_sensitive_importance_vcs_missing_are_404(tmp_path):
    from sherpa.ingest import importance
    (_ROOT / ".git").mkdir()
    cases = {
        "設計/credentials.xlsx": b"PK\x03\x04FAKE",
        f"設計/{importance.CONTROL_FILENAME}": "設定".encode(),
        ".git/config": b"[core]",
        "設計/無い.txt": None,
    }
    for rel, body in cases.items():
        if body is not None:
            (_ROOT / rel).write_bytes(body)
        assert _get(rel).status_code == 404, rel
        assert _downloads(rel) == 0, rel


def test_unledgered_symlink_and_outside_root_are_404(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("外の本文", encoding="utf-8")
    try:
        (_ROOT / "設計/link.txt").symlink_to(secret)
    except OSError as e:
        pytest.skip(f"symlink 非対応の環境: {e}")
    assert _get("設計/link.txt").status_code == 404
    assert _get("../secret.txt").status_code == 404
    assert _get(str(secret)).status_code == 404
