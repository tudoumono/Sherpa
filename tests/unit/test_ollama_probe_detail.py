"""許可済みの Ollama の接続の失敗は、名前が引けない・接続を拒否された・時間切れを分けた定型の文で返す（`routers/system._ollama_probe_detail`）。"""
from __future__ import annotations

import socket
import urllib.error
import urllib.request

import pytest

from sherpa.ingest import graph_extract
from sherpa.routers import system


def _real_failure(kind: str) -> Exception:
    if kind == "refused":
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/tags", timeout=2)
        except Exception as e:
            return e
    if kind == "dns":
        try:
            urllib.request.urlopen("http://no-such-host.invalid/api/tags", timeout=5)
        except Exception as e:
            return e
    return urllib.error.URLError(TimeoutError("timed out"))


@pytest.mark.parametrize("kind,expect", [("refused", "拒否"), ("dns", "名前"), ("timeout", "時間切れ")])
def test_real_failures_get_distinct_fixed_sentences(kind, expect):
    detail = graph_extract._safe_detail(_real_failure(kind))
    out = system._ollama_probe_detail(detail)
    assert expect in out
    assert "127.0.0.1" not in out and "invalid" not in out


def test_unknown_failure_and_auth_failure():
    assert system._ollama_probe_detail("ValueError: x") == "Ollama への接続に失敗しました"
    assert system._ollama_probe_detail("401 unauthorized: bad token").startswith("401")
