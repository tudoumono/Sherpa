"""POST /ext/v1/convert: 上限超過で一時ファイルが残らないこと・変換の同時実行が専用の上限までであること。"""
from __future__ import annotations

import asyncio
import io
import tempfile
import threading
import time
import zipfile
from pathlib import Path

import httpx
import pytest

from sherpa import ext, ext_api, store
from sherpa.api import app
from sherpa.ext.auth import require_api_key

_DOCX_XML = ('<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
             '<w:body><w:p><w:r><w:t>x</w:t></w:r></w:p></w:body></w:document>')


@pytest.fixture(autouse=True)
def _env():
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"DB down: {e}")
    ext_api._audit_writer.start()
    app.dependency_overrides[require_api_key] = lambda: {"id": 0, "label": "t"}
    yield
    app.dependency_overrides.pop(require_api_key, None)


def _docx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", _DOCX_XML)
    return buf.getvalue()


def _post_all(n: int, payload: bytes) -> list[httpx.Response]:
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            return await asyncio.gather(*[
                c.post("/ext/v1/convert", files={"file": (f"a{i}.docx", payload, "application/octet-stream")})
                for i in range(n)])
    return asyncio.run(run())


def test_over_limit_returns_413_and_leaves_no_tempfile(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(ext.convert, "_CONVERT_MAX_BYTES", 1000)
    (r,) = _post_all(1, b"x" * 200_000)
    assert r.status_code == 413
    assert list(tmp_path.iterdir()) == []


def test_conversions_run_at_most_concurrency_limit(monkeypatch):
    monkeypatch.setattr(ext.convert, "_CONVERT_CONCURRENCY", 2)
    ext.convert._slot = None
    lock = threading.Lock()
    state = {"now": 0, "peak": 0}

    def slow(path: Path):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.3)
        with lock:
            state["now"] -= 1
        return "md"

    monkeypatch.setattr(ext.convert.office_md, "to_markdown", slow)
    rs = _post_all(3, _docx())
    assert [r.status_code for r in rs] == [200, 200, 200]
    assert state["peak"] == 2


def _slow_convert(monkeypatch, tmp_path, seconds: float):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    ext.convert._slot = None

    def slow(path: Path):
        time.sleep(seconds)
        return "md"

    monkeypatch.setattr(ext.convert.office_md, "to_markdown", slow)


def test_requests_beyond_waiter_limit_get_503_without_tempfile(monkeypatch, tmp_path):
    monkeypatch.setattr(ext.convert, "_CONVERT_CONCURRENCY", 2)
    monkeypatch.setattr(ext.convert, "_CONVERT_MAX_WAITERS", 1)
    _slow_convert(monkeypatch, tmp_path, 0.5)
    rs = _post_all(4, _docx())
    codes = sorted(r.status_code for r in rs)
    assert codes == [200, 200, 200, 503]
    assert next(r for r in rs if r.status_code == 503).headers["retry-after"] == "30"
    assert list(tmp_path.iterdir()) == []
    assert ext.convert._inflight == 0


def test_wait_timeout_gets_503_and_removes_tempfile(monkeypatch, tmp_path):
    monkeypatch.setattr(ext.convert, "_CONVERT_CONCURRENCY", 1)
    monkeypatch.setattr(ext.convert, "_CONVERT_WAIT_S", 0.1)
    _slow_convert(monkeypatch, tmp_path, 0.6)
    rs = _post_all(2, _docx())
    assert sorted(r.status_code for r in rs) == [200, 503]
    assert next(r for r in rs if r.status_code == 503).headers["retry-after"] == "30"
    assert list(tmp_path.iterdir()) == []
    assert ext.convert._inflight == 0
