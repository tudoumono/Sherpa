"""原本読取（read_doc / read_around / grep ヒット）が複数行の鍵ブロックの本文を Codex へ渡さない。"""
from __future__ import annotations

import pytest

from sherpa import tool_dispatch, worlds

pytestmark = pytest.mark.usefixtures("upstream_only_registry")

_B64 = ["MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC" + str(i) * 10 + "AbCdEfGhIj" for i in range(6)]
_BODY_MARK = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC"
_FILLER = [f"通常の説明文 {i} 行目です。" for i in range(400)]


def _setup(monkeypatch, tmp_path, key_lines):
    wd = tmp_path / "world"
    wd.mkdir()
    der = tmp_path / "derived"
    der.mkdir()
    monkeypatch.setattr(worlds, "world_dir", lambda w: wd)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: der)
    monkeypatch.setattr(worlds, "derived_rag_dir", lambda w: der)
    monkeypatch.setattr(worlds, "observation_current_dir", lambda w: None)
    # 鍵は先頭の判定窓の外（資料の途中）に置く
    (wd / "memo.md").write_text("\n".join(_FILLER + key_lines + ["以上"]), encoding="utf-8")


def _pem(kind="RSA PRIVATE KEY", n=1):
    return [f"-----BEGIN {kind}-----", *(_B64 * n), f"-----END {kind}-----"]


def test_read_doc_hides_key_across_lines_and_from_midpoint(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, _pem())
    b = len(_FILLER)
    whole = tool_dispatch.run_tool("read_doc", {"doc_id": "memo.md", "start_line": b + 1}, "w", None)[0]
    assert "error" not in whole and _BODY_MARK not in whole["text"] and "[REDACTED]" in whole["text"]
    # 窓が鍵の途中（本文 3 行目）から始まる
    mid = tool_dispatch.run_tool("read_doc", {"doc_id": "memo.md", "start_line": b + 4}, "w", None)[0]
    assert _BODY_MARK not in mid["text"] and "[REDACTED]" in mid["text"]


def test_read_around_hides_key_when_window_cuts_the_block(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, _pem(n=200))  # 窓（±200 行）が BEGIN にも END にも届かない長さ
    b = len(_FILLER)
    for line in (b + 600, b + 200):  # 窓の先頭が鍵の途中／窓の終端が END の前
        res = tool_dispatch.run_tool("read_around", {"doc_id": "memo.md", "line": line, "window": 200}, "w", None)[0]
        assert _BODY_MARK not in res["text"] and "[REDACTED]" in res["text"]


def test_pgp_private_key_block_is_hidden(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, (
        ["-----BEGIN PGP PRIVATE KEY BLOCK-----", "", *_B64, "-----END PGP PRIVATE KEY BLOCK-----"]))
    b = len(_FILLER)
    res = tool_dispatch.run_tool("read_doc", {"doc_id": "memo.md", "start_line": b + 1}, "w", None)[0]
    assert _BODY_MARK not in res["text"] and "[REDACTED]" in res["text"]


def test_redact_hides_orphan_base64_run_without_markers():
    from sherpa.parts.read import tools
    out = tools._redact("説明\n" + "\n".join(_B64[:3]) + "\n-----END RSA PRIVATE KEY-----")
    assert _BODY_MARK not in out


def test_redact_hides_single_key_body_line_but_keeps_hash_lines():
    from sherpa.parts.read import tools
    line = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQCAbCdEfGhIj"
    assert line not in tools._redact(f"説明\n{line}\n以上")
    sha = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
    assert tools._redact(f"{sha}\n{sha.upper()}") == f"{sha}\n{sha.upper()}"


def test_redact_hides_indented_key_body_lines():
    from sherpa.parts.read import tools
    out = tools._redact("設定例:\n" + "\n".join("    " + x for x in _B64[:3]))
    assert all(x not in out for x in _B64[:3])
