"""`sherpa.fd_response`（documents/ext_api 両ルータが共有する fd ベース配信）の単体テスト。

外部サービス不要（fastapi の `TestClient` のみ・DB/Neo4j/ES 不要）。検証済み fd 1本から
配信する契約（Accept-Ranges/Last-Modified/ETag・単一 Range の 206・範囲外の 416・複数
Range/構文不正は単純配信 200 へのフォールバック・fd の確実な close）を固定する。
"""
from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sherpa.fd_response import FdFileResponse, FdOwner, content_disposition


def _app(tmp_path, data: bytes):
    p = tmp_path / "f.bin"
    p.write_bytes(data)
    mtime = p.stat().st_mtime
    owners: list[FdOwner] = []

    app = FastAPI()

    @app.get("/f")
    def f():
        fd = os.open(str(p), os.O_RDONLY)
        owner = FdOwner(fd)
        owners.append(owner)
        headers = {"Content-Disposition": content_disposition("f.bin")}
        return FdFileResponse(owner, len(data), mtime, media_type="text/plain", headers=headers)

    return TestClient(app), owners


def test_fd_owner_idempotent_close(tmp_path):
    """`close()` は複数回呼んでも安全（実際に一度だけ `os.close` される）。"""
    p = tmp_path / "f.txt"
    p.write_bytes(b"hello")
    fd = os.open(str(p), os.O_RDONLY)
    owner = FdOwner(fd)
    owner.close()
    with pytest.raises(OSError):
        os.fstat(fd)   # 実際に close 済み（同じ fd 番号で fstat が失敗する）
    owner.close()       # 2回目は何も起きない（例外なし・冪等）


def test_full_response_has_validators_and_closes_fd(tmp_path):
    data = b"0123456789" * 10   # 100 bytes
    client, owners = _app(tmp_path, data)
    r = client.get("/f")
    assert r.status_code == 200 and r.content == data
    assert r.headers.get("accept-ranges") == "bytes"
    assert r.headers.get("content-length") == "100"
    assert r.headers.get("last-modified")
    assert r.headers.get("etag", "").startswith('"') and r.headers["etag"].endswith('"')
    assert 'filename="f.bin"' in r.headers.get("content-disposition", "")
    assert owners[0]._closed is True   # 正常終了後も fd は閉じている


def test_single_range_returns_206_with_content_range(tmp_path):
    data = b"0123456789" * 10
    client, _ = _app(tmp_path, data)
    r = client.get("/f", headers={"Range": "bytes=10-19"})
    assert r.status_code == 206
    assert r.content == data[10:20]
    assert r.headers.get("content-range") == "bytes 10-19/100"
    assert r.headers.get("content-length") == "10"


def test_suffix_range_returns_last_n_bytes(tmp_path):
    data = b"0123456789" * 10
    client, _ = _app(tmp_path, data)
    r = client.get("/f", headers={"Range": "bytes=-10"})
    assert r.status_code == 206 and r.content == data[-10:]


def test_open_ended_range_returns_rest_of_file(tmp_path):
    data = b"0123456789" * 10
    client, _ = _app(tmp_path, data)
    r = client.get("/f", headers={"Range": "bytes=90-"})
    assert r.status_code == 206 and r.content == data[90:]
    assert r.headers.get("content-range") == "bytes 90-99/100"


def test_out_of_range_returns_416_with_full_size(tmp_path):
    data = b"0123456789" * 10
    client, _ = _app(tmp_path, data)
    r = client.get("/f", headers={"Range": "bytes=9999-"})
    assert r.status_code == 416
    assert r.headers.get("content-range") == "bytes */100"


def test_multi_range_falls_back_to_full_content(tmp_path):
    """複数 Range（`multipart/byteranges`）は本モジュールでは非対応——単純配信 200 にフォールバックする。"""
    data = b"0123456789" * 10
    client, _ = _app(tmp_path, data)
    r = client.get("/f", headers={"Range": "bytes=0-9,20-29"})
    assert r.status_code == 200 and r.content == data


def test_malformed_range_falls_back_to_full_content(tmp_path):
    data = b"0123456789" * 10
    client, _ = _app(tmp_path, data)
    for bad in ("bytes=50-10", "notbytes=1-2", "bytes=", "bytes=-0"):
        r = client.get("/f", headers={"Range": bad})
        assert r.status_code == 200 and r.content == data, bad


def test_range_against_empty_file_is_unsatisfiable(tmp_path):
    client, _ = _app(tmp_path, b"")
    r = client.get("/f", headers={"Range": "bytes=0-0"})
    assert r.status_code == 416 and r.headers.get("content-range") == "bytes */0"


def test_if_range_gates_range_on_current_validators(tmp_path):
    """If-Range が現在の ETag/Last-Modified と一致する時だけ Range を適用する。不一致（＝配信直前の
    検証以降に内容が変わった疑い）なら Range を無視して全量 200 にする——更新跨ぎで前後の断片を
    連結した壊れたファイルを組み立てさせない（RV3 是正 #1）。"""
    data = b"0123456789" * 10
    client, _ = _app(tmp_path, data)
    full = client.get("/f")
    etag, lm = full.headers["etag"], full.headers["last-modified"]

    r_etag = client.get("/f", headers={"Range": "bytes=0-9", "If-Range": etag})
    assert r_etag.status_code == 206 and r_etag.content == data[:10]

    r_lm = client.get("/f", headers={"Range": "bytes=0-9", "If-Range": lm})
    assert r_lm.status_code == 206 and r_lm.content == data[:10]

    r_stale = client.get("/f", headers={"Range": "bytes=0-9", "If-Range": '"stale-etag"'})
    assert r_stale.status_code == 200 and r_stale.content == data

    r_no_range = client.get("/f", headers={"If-Range": etag})   # Range 無し・If-Range だけは無視
    assert r_no_range.status_code == 200 and r_no_range.content == data


def test_disconnect_via_receive_aborts_streaming_without_send_raising():
    """`send` が例外を投げない ASGI transport でも、`receive()` の `http.disconnect` を検出して
    配信を打ち切り、fd を閉じる（`StreamingResponse` 継承由来の切断監視・RV3 是正 #2・
    退行解消——fd 直配信化前の `StreamingResponse` はこれを備えていた）。"""
    import asyncio
    import tempfile

    chunk = 262144
    data = b"x" * (chunk * 20)   # 十分大きい（複数チャンクに分割される・早期打ち切りを検出しやすくする）
    fh = tempfile.NamedTemporaryFile(delete=False)
    fh.write(data)
    fh.close()
    mtime = os.stat(fh.name).st_mtime
    fd = os.open(fh.name, os.O_RDONLY)
    owner = FdOwner(fd)
    resp = FdFileResponse(owner, len(data), mtime, media_type="application/octet-stream", headers={})

    sent_bodies: list[bytes] = []

    async def _send(message):
        if message["type"] == "http.response.body":
            sent_bodies.append(message["body"])   # 例外は投げない（旧世代 ASGI サーバ相当）

    async def _receive():
        return {"type": "http.disconnect"}   # 即座に切断を通知する

    scope = {"type": "http", "headers": []}
    asyncio.run(asyncio.wait_for(resp(scope, _receive, _send), timeout=10))
    assert owner._closed is True
    assert sum(len(b) for b in sent_bodies) < len(data), "切断後も配信を続けてしまっている"
    os.unlink(fh.name)


def test_content_disposition_ascii_vs_non_ascii():
    assert content_disposition("plain.txt") == 'attachment; filename="plain.txt"'
    non_ascii = content_disposition("設計書.docx")
    assert non_ascii.startswith("attachment; filename*=utf-8''")
    assert "設計書" not in non_ascii   # 生の非ASCIIは header に出ない（%XX 済み）


def test_fd_closed_on_send_failure():
    """応答送信中に例外が起きても（クライアント早期切断相当）fd は確実に閉じる
    （body iterator の終了頼みにせず、`__call__` 自身の finally で閉じる）。"""
    import asyncio
    import tempfile

    fh = tempfile.NamedTemporaryFile(delete=False)
    fh.write(b"x" * 100)
    fh.close()
    fd = os.open(fh.name, os.O_RDONLY)
    owner = FdOwner(fd)
    resp = FdFileResponse(owner, 100, os.fstat(fd).st_mtime, media_type="text/plain", headers={})

    async def _boom_send(message):
        raise ConnectionResetError("simulated client disconnect")

    async def _receive():   # 切断監視タスクは動くが disconnect は来ない側のシナリオ（send 側の例外で確定）
        return await asyncio.Future()

    with pytest.raises(ConnectionResetError):
        asyncio.run(asyncio.wait_for(
            resp({"type": "http", "headers": []}, _receive, _boom_send), timeout=5))
    assert owner._closed is True
    os.unlink(fh.name)
