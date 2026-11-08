"""除外の資料は、既知のパスからでも原本をダウンロードできない（`/documents/download`。`/ext/v1/doc` は `test_ext_api.py`）。台帳（Postgres）が要る。"""
from __future__ import annotations

import atexit
import json
import os
import pathlib
import shutil
import tempfile

import pytest

_KB = os.path.realpath(tempfile.mkdtemp(prefix="sherpa_kb_"))
_DER = tempfile.mkdtemp(prefix="sherpa_der_")
atexit.register(lambda: (shutil.rmtree(_KB, ignore_errors=True), shutil.rmtree(_DER, ignore_errors=True)))

from fastapi.testclient import TestClient   # noqa: E402

from sherpa import path_rules as PR   # noqa: E402
from sherpa import store   # noqa: E402
from sherpa.api import app   # noqa: E402

W = f"prdl{os.getpid()}"
_CLIENT = TestClient(app)
_ROOT = pathlib.Path(_KB) / W
OLD = "旧/旧版.txt"
NEW = "現行/現行.txt"


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    monkeypatch.setenv("SHERPA_AUTH_DISABLED", "1")
    monkeypatch.setenv("SHERPA_KB_DIR", _KB)
    monkeypatch.setenv("SHERPA_DERIVED_DIR", _DER)
    monkeypatch.setenv("SHERPA_DISABLE_EMBED", "1")
    for rel, body in ((OLD, "旧い本文"), (NEW, "現行の本文")):
        (_ROOT / rel).parent.mkdir(parents=True, exist_ok=True)
        (_ROOT / rel).write_text(body, encoding="utf-8")
    store.replace_documents(W, [{"name": OLD}, {"name": NEW}])
    yield
    shutil.rmtree(_ROOT, ignore_errors=True)


def _exclude(monkeypatch, path):
    monkeypatch.setenv(PR.ENV_KEY, json.dumps({W: [{"path": path, "kind": "exclude", "outside_scope": False}]}))


def test_download_refuses_excluded_even_when_ledgered(monkeypatch):
    get = lambda rel: _CLIENT.get("/documents/download", params={"rel": rel, "world": W})  # noqa: E731
    assert get(OLD).status_code == 200
    _exclude(monkeypatch, "旧")
    assert get(OLD).status_code == 404
    assert get("現行/../旧/旧版.txt").status_code == 404
    assert get(NEW).status_code == 200


def test_documents_list_and_total_are_not_filtered_by_exclusion(monkeypatch):
    """資料の一覧と総数には除外を当てない（名前が出るのは許す）。ダウンロードは除外で 404 のまま。"""
    names = [f"旧/{i}.txt" for i in range(3)] + ["現行/a.txt"]
    for n in names:
        (_ROOT / n).parent.mkdir(parents=True, exist_ok=True)
        (_ROOT / n).write_text("x", encoding="utf-8")
    store.replace_documents(W, [{"name": n} for n in names])
    base = _CLIENT.get("/documents", params={"world": W}).json()
    _exclude(monkeypatch, "旧")
    got = _CLIENT.get("/documents", params={"world": W}).json()
    assert got == base and got["total"] == 4 and any(d["name"].startswith("旧/") for d in got["documents"])
    assert _CLIENT.get("/documents/download", params={"rel": "旧/0.txt", "world": W}).status_code == 404
