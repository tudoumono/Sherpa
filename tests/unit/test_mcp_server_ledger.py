"""Sherpa MCP サーバの台帳ツール（ledger_manifest_set／ledger_item_put／ledger_status）の単体テスト。"""
from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")
os.environ["SHERPA_MCP_WORLD"] = "v1"
os.environ.pop("SHERPA_MCP_SCOPE", None)
from sherpa import mcp_server as M   # noqa: E402


@pytest.fixture(autouse=True)
def _reset_duplicate_call_cache():
    M._seen_tool_calls.clear()
    yield
    M._seen_tool_calls.clear()


def _rpc(method, params=None, id=1):
    msg = {"jsonrpc": "2.0", "id": id, "method": method}
    if params is not None:
        msg["params"] = params
    return M.handle(msg)


def _call(name, arguments=None):
    return _rpc("tools/call", {"name": name, "arguments": arguments or {}})


def _ledger_call(name, arguments):
    response = _call(name, arguments)["result"]
    body = json.loads(response["content"][0]["text"])
    assert response["isError"] is bool(body.get("error"))
    return body


def _ledger_item(item_id="a", **changes):
    return {"id": item_id, "kind": "row", "subject": "対象", "required_checks": ["source"],
            "evidence": [{"kind": "source", "path": "src/a.py", "line": 1}],
            "status": "source_confirmed", "reason": "", "owner": "parent", **changes}


def _review(**changes):
    return {"purpose": "依頼の目的", "perspectives": ["画面"], "summary": "分かったこと",
            "added_items": [], "removed_items": [], "verdict": "mostly_answered",
            "extra_perspectives": [], **changes}


def test_ledger_tools_exposed_with_exact_input_fields():
    definitions = {tool["name"]: tool for tool in _rpc("tools/list")["result"]["tools"]}
    expected = {"ledger_manifest_set": {"question_kind", "items"},
                "ledger_item_put": set(_ledger_item()), "ledger_status": set()}
    for name, fields in expected.items():
        schema = definitions[name]["inputSchema"]
        assert set(schema["properties"]) == fields
        assert set(schema.get("required", [])) == fields
        assert schema["additionalProperties"] is False
        assert "ファイルを直接書かず" in definitions[name]["description"]


def test_ledger_tools_persist_and_preserve_created_at(tmp_path, monkeypatch):
    directory = tmp_path / "investigation"
    monkeypatch.setenv("SHERPA_MCP_LEDGER_DIR", str(directory))
    fresh = _ledger_call("ledger_status", {})
    assert fresh["manifest_invalid"] is True and fresh["items"] == 0
    assert _ledger_call("ledger_manifest_set", {"question_kind": "list", "items": ["a"]}) == {"ok": True}
    created_at = json.loads((directory / "manifest.json").read_text())["created_at"]
    assert created_at
    assert _ledger_call("ledger_status", {})["missing"] == ["a"]
    item = _ledger_item()
    assert _ledger_call("ledger_item_put", item) == {"ok": True, "id": "a"}
    assert json.loads((directory / "items/a.json").read_text()) == item
    done = _ledger_call("ledger_status", {})
    assert done["complete"] is True and done["counts"]["source_confirmed"] == 1
    assert _ledger_call("ledger_manifest_set", {"question_kind": "compare", "items": ["a", "b"]}) == {"ok": True}
    assert json.loads((directory / "manifest.json").read_text()) == {
        "question_kind": "compare", "items": ["a", "b"], "created_at": created_at}
    assert _ledger_call("ledger_status", {})["missing"] == ["b"]


def test_ledger_review_put_rejected_before_any_item_terminalizes_then_accepted(tmp_path, monkeypatch):
    """終端の item が 1 件も無い時点の ledger_review_put は書き込まず拒否・終端後は受理され terminal_count が付く。"""
    directory = tmp_path / "investigation"
    monkeypatch.setenv("SHERPA_MCP_LEDGER_DIR", str(directory))
    _ledger_call("ledger_manifest_set", {"question_kind": "list", "items": ["a"]})
    _ledger_call("ledger_item_put", _ledger_item(status="pending", evidence=[]))
    assert _ledger_call("ledger_review_put", _review())["error"] == "ledger_review_rejected"
    assert not (directory / "reviews.jsonl").exists()
    _ledger_call("ledger_item_put", _ledger_item())
    assert _ledger_call("ledger_review_put", _review()) == {"ok": True}
    assert json.loads((directory / "reviews.jsonl").read_text().splitlines()[0])["terminal_count"] == 1


def test_ledger_review_put_ignores_terminal_items_outside_the_manifest(tmp_path, monkeypatch):
    """terminal_count は manifest 登録済み id の終端だけを数える（目録外 "b" が終端でも "a" 非終端なら拒否）。"""
    directory = tmp_path / "investigation"
    monkeypatch.setenv("SHERPA_MCP_LEDGER_DIR", str(directory))
    _ledger_call("ledger_manifest_set", {"question_kind": "list", "items": ["a"]})
    _ledger_call("ledger_item_put", _ledger_item("a", status="pending", evidence=[]))
    _ledger_call("ledger_item_put", _ledger_item("b"))
    assert _ledger_call("ledger_review_put", _review())["error"] == "ledger_review_rejected"
    assert not (directory / "reviews.jsonl").exists()

    _ledger_call("ledger_manifest_set", {"question_kind": "list", "items": ["a", "b"]})
    _ledger_call("ledger_item_put", _ledger_item("a"))
    assert _ledger_call("ledger_review_put", _review()) == {"ok": True}
    assert json.loads((directory / "reviews.jsonl").read_text().splitlines()[-1])["terminal_count"] == 2


def test_ledger_status_reports_unsatisfied_invalid_and_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("SHERPA_MCP_LEDGER_DIR", str(tmp_path))
    _ledger_call("ledger_manifest_set", {"question_kind": "list", "items": ["a", "bad", "missing"]})
    item = _ledger_item(status="spec_only", required_checks=["source", "spec_doc"],
                        evidence=[{"kind": "spec_doc", "path": "docs/spec.md", "line": 2}])
    assert _ledger_call("ledger_item_put", item) == {"ok": True, "id": "a"}
    (tmp_path / "items/bad.json").write_text("{broken", encoding="utf-8")
    status = _ledger_call("ledger_status", {})
    assert status["complete"] is False and status["manifest_invalid"] is False and status["items"] == 2
    assert status["non_terminal"] == ["a"] and status["unsatisfied"] == {"a": ["source"]}
    assert status["invalid"] == ["bad"] and status["missing"] == ["missing"]
    assert status["counts"]["spec_only"] == 1

    # SHERPA_MCP_LEDGER_REQUIRED_EXTRA があれば、item が source を宣言していなくても unsatisfied に載る。
    monkeypatch.setenv("SHERPA_MCP_LEDGER_REQUIRED_EXTRA", "source")
    assert _ledger_call("ledger_manifest_set",
                        {"question_kind": "list", "items": ["a", "bad", "missing", "c"]}) == {"ok": True}
    no_source_declared = _ledger_item(
        item_id="c", status="spec_only", required_checks=["spec_doc"],
        evidence=[{"kind": "spec_doc", "path": "docs/other.md", "line": 1}])
    assert _ledger_call("ledger_item_put", no_source_declared) == {"ok": True, "id": "c"}
    assert _ledger_call("ledger_status", {})["unsatisfied"]["c"] == ["source"]


@pytest.mark.parametrize("arguments", [{"question_kind": "list"},
                                      {"question_kind": "", "items": ["a"]},
                                      {"question_kind": "list", "items": [1]}])
def test_ledger_manifest_invalid_returns_problems_without_writing(tmp_path, monkeypatch, arguments):
    monkeypatch.setenv("SHERPA_MCP_LEDGER_DIR", str(tmp_path))
    result = _ledger_call("ledger_manifest_set", arguments)
    assert result["error"] == "ledger_manifest_invalid" and result["problems"]
    assert not (tmp_path / "manifest.json").exists()


@pytest.mark.parametrize("changes", [{"status": "done"}, {"required_checks": []},
                                    {"evidence": []}, {"extra": "body"},
                                    {"id": "../outside"}, {"id": "a/b"}, {"id": ""}, {"id": ".hidden"}])
def test_ledger_item_invalid_returns_problems_without_replacing_item(tmp_path, monkeypatch, changes):
    monkeypatch.setenv("SHERPA_MCP_LEDGER_DIR", str(tmp_path))
    item = _ledger_item()
    _ledger_call("ledger_item_put", item)
    result = _ledger_call("ledger_item_put", {**item, **changes})
    assert result["error"] == "ledger_item_invalid" and result["problems"]
    assert json.loads((tmp_path / "items/a.json").read_text()) == item
    assert [path.name for path in (tmp_path / "items").iterdir()] == ["a.json"]


@pytest.mark.parametrize("name", ["ledger_manifest_set", "ledger_item_put", "ledger_status"])
def test_ledger_tools_require_directory_env(monkeypatch, name):
    monkeypatch.delenv("SHERPA_MCP_LEDGER_DIR", raising=False)
    assert _ledger_call(name, {}) == {"error": "ledger_unavailable"}


def test_ledger_tools_exempt_from_clipping_duplicates_and_sidecar(tmp_path, monkeypatch):
    monkeypatch.setenv("SHERPA_MCP_LEDGER_DIR", str(tmp_path / "investigation"))
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "1")
    sidecar = tmp_path / "sidecar.jsonl"
    sidecar.write_text("", encoding="utf-8")
    monkeypatch.setenv("SHERPA_MCP_SIDECAR", str(sidecar))
    for name, arguments in [("ledger_manifest_set", {"question_kind": "list", "items": ["a"]}),
                            ("ledger_item_put", _ledger_item()), ("ledger_status", {})]:
        first = _ledger_call(name, arguments)
        assert _ledger_call(name, arguments) == first
        assert "error" not in first and "truncated" not in first
    assert first["complete"] is True
    assert sidecar.read_text() == ""


@pytest.mark.parametrize("name,arguments", [
    ("ledger_manifest_set", {"question_kind": "list", "items": ["a"]}),
    ("ledger_item_put", _ledger_item()),
])
def test_ledger_write_failure_returns_error_and_log(tmp_path, monkeypatch, capsys, name, arguments):
    destination = tmp_path / "not-a-directory"
    destination.write_text("keep", encoding="utf-8")
    monkeypatch.setenv("SHERPA_MCP_LEDGER_DIR", str(destination))
    result = _ledger_call(name, arguments)
    assert result["error"] == "ledger_write_failed" and result["problems"]
    assert f"{name} failed:" in capsys.readouterr().err
    assert destination.read_text() == "keep"


def test_ledger_item_put_refuses_symlinked_items_dir(tmp_path, monkeypatch):
    """items/ を外部への symlink に差し替えても、リンク先へ書かない（fail-closed）。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    directory = tmp_path / "investigation"
    directory.mkdir()
    (directory / "items").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("SHERPA_MCP_LEDGER_DIR", str(directory))
    assert _ledger_call("ledger_item_put", _ledger_item())["error"] == "ledger_write_failed"
    assert list(outside.iterdir()) == []


def test_ledger_manifest_set_repairs_invalid_regular_file(tmp_path, monkeypatch):
    """規約に合わない manifest（通常ファイル）は検証済み入力で修復できる。created_at は既存が文字列なら保持。"""
    directory = tmp_path / "investigation"
    directory.mkdir()
    monkeypatch.setenv("SHERPA_MCP_LEDGER_DIR", str(directory))
    manifest = directory / "manifest.json"
    manifest.write_text(json.dumps({"question_kind": "list", "items": ["a"]}))
    assert _ledger_call("ledger_manifest_set", {"question_kind": "list", "items": ["a"]}) == {"ok": True}
    repaired = json.loads(manifest.read_text())
    assert repaired["created_at"] and set(repaired) == {"question_kind", "created_at", "items"}
    manifest.write_text(json.dumps({"created_at": "2026-09-22T00:00:00+00:00", "items": []}))
    assert _ledger_call("ledger_manifest_set", {"question_kind": "compare", "items": ["a"]}) == {"ok": True}
    assert json.loads(manifest.read_text()) == {
        "question_kind": "compare", "created_at": "2026-09-22T00:00:00+00:00", "items": ["a"]}
    manifest.write_text("{broken")
    assert _ledger_call("ledger_manifest_set", {"question_kind": "list", "items": ["a"]}) == {"ok": True}


def test_ledger_manifest_set_refuses_symlinked_manifest(tmp_path, monkeypatch):
    directory = tmp_path / "investigation"
    directory.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    (directory / "manifest.json").symlink_to(outside)
    monkeypatch.setenv("SHERPA_MCP_LEDGER_DIR", str(directory))
    body = _ledger_call("ledger_manifest_set", {"question_kind": "list", "items": ["a"]})
    assert body["error"] == "ledger_manifest_invalid" and outside.read_text() == "{}"
