"""glob_search の続きの取得（offset／generation）。実 DB の台帳と一時の資料フォルダを使い、差し替えるのは資料フォルダの場所の解決だけ。"""
from __future__ import annotations

import json
import os
import uuid

import pytest

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")
from sherpa import mcp_server as M  # noqa: E402
from sherpa import path_rules, store, tool_call_log, tool_dispatch, worlds  # noqa: E402
from sherpa.ingest import worker  # noqa: E402
from tests._replace_row import offline_row   # noqa: E402

pytestmark = pytest.mark.unit

_N = 450


def _make_corpus(wd, n, pad=""):
    names = []
    for i in range(n):
        rel = f"top/{pad}f{i:04d}.jcl"
        p = wd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("//JOB\n", encoding="utf-8")
        names.append(rel)
    return names


@pytest.fixture
def make_world(monkeypatch, tmp_path):
    """`make_world(n, pad, ingest)` → (world, 名前の昇順リスト)。台帳は ingest=True のときだけ作る（無ければ資料フォルダを歩く）。"""
    created = []

    def _make(n=_N, pad="", ingest=True):
        wid = "globpage_" + uuid.uuid4().hex[:8]
        wd = tmp_path / "world"
        names = _make_corpus(wd, n, pad)
        der = tmp_path / "derived"
        der.mkdir(exist_ok=True)
        monkeypatch.setattr(worlds, "world_dir", lambda w: wd)
        monkeypatch.setattr(worlds, "derived_md_dir", lambda w: der)
        monkeypatch.setenv("SHERPA_MCP_WORLD", wid)
        created.append(wid)
        if ingest:
            _ingest(wid)
        return wid, sorted(names)

    yield _make
    for wid in created:
        store.replace_documents(wid, [], row=offline_row(wid))
        with store._connect() as c:
            c.execute("DELETE FROM ingest_runs WHERE version=%s", (wid,))


def _ingest(wid):
    store.replace_documents(wid, worker._ledger_rows(wid), row=offline_row(wid))


@pytest.fixture(autouse=True)
def _reset_dedup():
    M._seen_tool_calls.clear()
    yield
    M._seen_tool_calls.clear()


def _run_for(wid):
    return lambda args: tool_dispatch.run_tool("glob_search", args, wid, None)[0]


def _call(args):
    resp = M.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "glob_search", "arguments": args}})
    return json.loads(resp["result"]["content"][0]["text"])


def _take_all(call, pattern="*.jcl"):
    got, found, args = [], 0, {"pattern": pattern}
    while True:
        r = call(args)
        got += r["paths"]
        found += tool_call_log.summarize_result("glob_search", args, r, False, {})["count"]
        if r["next_offset"] is None:
            return got, found, r
        args = {"pattern": pattern, "offset": r["next_offset"], "generation": r["generation"]}


def test_offset_で取り切ると重複も欠けも無く_found_の合計が総数(make_world):
    wid, names = make_world()
    got, found, last = _take_all(_run_for(wid))
    assert got == names
    assert found == _N == last["count"]
    assert last["ledger_at"] and last["generation"].startswith(last["ledger_at"] + f"#{_N}:")


def test_台帳が入れ替わると_generation_違いのエラー(make_world):
    wid, _ = make_world()
    run = _run_for(wid)
    first = run({"pattern": "*.jcl"})
    _ingest(wid)  # 取り込み直し＝台帳の入れ替え
    r = run({"pattern": "*.jcl", "offset": first["next_offset"], "generation": first["generation"]})
    assert r["error"] == "glob_generation_mismatch" and "offset 0" in r["message"]
    assert run({"pattern": "*.jcl", "offset": 200})["error"] == "glob_generation_required"


def test_最大時刻でない行を消すと続きは_generation_違いのエラー(make_world):
    wid, names = make_world()
    run = _run_for(wid)
    first = run({"pattern": "*.jcl"})
    with store._connect() as c:  # ledger_at（最大の更新時刻）を変えずに 1 行だけ消す
        c.execute("DELETE FROM documents WHERE version=%s AND name=%s", (wid, names[5]))
    again = run({"pattern": "*.jcl"})
    assert again["ledger_at"] == first["ledger_at"] and again["count"] == _N - 1
    r = run({"pattern": "*.jcl", "offset": first["next_offset"], "generation": first["generation"]})
    assert r["error"] == "glob_generation_mismatch"


def test_歩いたときは_generation_が_walk_で_notice(make_world):
    wid, names = make_world(n=300, ingest=False)
    run = _run_for(wid)
    first = run({"pattern": "*.jcl"})
    assert "ledger_at" not in first and first["generation"] == "walk" and "変わる" in first["notice"]
    got, _, _ = _take_all(run)
    assert got == names


def test_エラーの後の取り直しは重複扱いにならず_ふつうの同じ呼び出しは今どおり(make_world):
    wid, _ = make_world()
    gen = _call({"pattern": "*.jcl"})["generation"]
    assert _call({"pattern": "*.jcl"})["error"] == "duplicate_tool_call"
    _ingest(wid)
    assert _call({"pattern": "*.jcl", "offset": 200, "generation": gen})["error"] == "glob_generation_mismatch"
    again = _call({"pattern": "*.jcl"})
    assert "paths" in again and again["generation"] != gen
    assert _call({"pattern": "*.jcl"})["error"] == "duplicate_tool_call"


def test_generation_なしのエラーの後の先頭からの同じ呼び出しは重複のまま(make_world):
    make_world()
    assert "paths" in _call({"pattern": "*.jcl"})
    assert _call({"pattern": "*.jcl", "offset": 200})["error"] == "glob_generation_required"
    assert _call({"pattern": "*.jcl"})["error"] == "duplicate_tool_call"


def test_最後のクリップを超えても構造が残り_続きを取り切れる(make_world, monkeypatch):
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "20000")
    _, names = make_world(pad="d" * 200 + "/")
    first = _call({"pattern": "*.jcl"})
    assert "text" not in first and first["count"] == _N and first["generation"].startswith(first["ledger_at"] + "#")
    assert first["truncated"] and first["next_offset"] == len(first["paths"]) < 200
    M._seen_tool_calls.clear()
    got, _, _ = _take_all(_call)
    assert got == names


def test_1パスが予算より大きいと続きの位置を返さず予算不足(make_world, monkeypatch):
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "600")
    make_world(n=5, pad="d" * 200 + "/" + "e" * 200 + "/" + "g" * 200 + "/")
    r = _call({"pattern": "*.jcl"})
    assert r["error"] == "tool_result_budget_too_small" and "next_offset" not in r


def _exclude_env(monkeypatch, wid, path):
    from sherpa import path_rules
    monkeypatch.setenv(path_rules.ENV_KEY, json.dumps({wid: [{"path": path, "kind": "exclude"}]}))


def test_ページの間で除外の規則を保存すると続きは_generation_違いのエラー(make_world, monkeypatch):
    wid, names = make_world()
    first = _call({"pattern": "*.jcl"})
    _exclude_env(monkeypatch, wid, names[0])
    r = _call({"pattern": "*.jcl", "offset": first["next_offset"], "generation": first["generation"]})
    assert r["error"] == "glob_generation_mismatch"


def test_offset_0_の_generation_と_pattern_の空白大文字は同じ検索として重複(make_world):
    make_world(n=5)
    assert "paths" in _call({"pattern": "*.jcl"})
    assert _call({"pattern": "*.jcl", "generation": "stale"})["error"] == "duplicate_tool_call"
    assert _call({"pattern": " *.JCL "})["error"] == "duplicate_tool_call"


def test_ページの間で範囲の外でも読む優先を保存すると続きは_generation_違いのエラー(make_world, monkeypatch):
    from sherpa import path_rules
    wid, names = make_world()
    first = _call({"pattern": "*.jcl"})
    monkeypatch.setenv(path_rules.ENV_KEY, json.dumps({wid: [{"path": "top", "kind": "priority", "outside_scope": True}]}))
    r = _call({"pattern": "*.jcl", "offset": first["next_offset"], "generation": first["generation"]})
    assert r["error"] == "glob_generation_mismatch"


def test_検索が使わない引数とスラッシュなしの表記は同じ検索として重複(make_world):
    make_world()
    assert "paths" in _call({"pattern": "*.jcl"})
    assert _call({"pattern": "**/*.jcl"})["error"] == "duplicate_tool_call"
    assert _call({"pattern": "*.jcl", "unused": 1})["error"] == "duplicate_tool_call"


@pytest.fixture
def save_exclude(monkeypatch):
    """`save_exclude(world, path)` で除外の規則を実 DB へ保存し、調べものの道具の子プロセスと同じく env の写しでも渡す（`SHERPA_MCP_WORLD` があると DB は読まれない）。後片付けで規則を消す。"""
    worlds_saved = []

    def _save(wid, path, kind=path_rules.KIND_EXCLUDE, outside_scope=False):
        worlds_saved.append(wid)
        items = [path_rules.Rule(path, kind, outside_scope, target_type="file")]
        path_rules.save_world("admin", wid, items)
        monkeypatch.setenv(path_rules.ENV_KEY, path_rules.snapshot_env(wid, path_rules.Rules(items))[path_rules.ENV_KEY])

    yield _save
    for wid in worlds_saved:
        path_rules.save_world("admin", wid, [])


def test_1ページ目の後に除外の規則を保存すると続きは_generation_違いのエラー(make_world, save_exclude):
    wid, names = make_world()
    run = _run_for(wid)
    first = run({"pattern": "*.jcl"})
    save_exclude(wid, names[0])
    r = run({"pattern": "*.jcl", "offset": first["next_offset"], "generation": first["generation"]})
    assert r["error"] == "glob_generation_mismatch"
    again = run({"pattern": "*.jcl"})
    assert names[0] not in again["paths"] and again["generation"] != first["generation"]


def test_pattern_の表記と_offset_0_の_generation_が違っても同じ検索は重複(make_world):
    make_world()
    assert "paths" in _call({"pattern": "*.jcl"})
    assert _call({"pattern": " *.JCL ", "generation": "stale"})["error"] == "duplicate_tool_call"
    assert _call({"pattern": " *.JCL ", "offset": 0, "generation": "stale"})["error"] == "duplicate_tool_call"


def test_1ページ目の後に範囲を広げる優先を保存すると続きは_generation_違いのエラー(make_world, save_exclude):
    wid, names = make_world()
    run = _run_for(wid)
    first = run({"pattern": "*.jcl"})
    save_exclude(wid, names[0], kind=path_rules.KIND_PRIORITY, outside_scope=True)
    r = run({"pattern": "*.jcl", "offset": first["next_offset"], "generation": first["generation"]})
    assert r["error"] == "glob_generation_mismatch"


def test_同じ_generation_で_offset_の型が違っても同じページは重複(make_world):
    make_world()
    gen = _call({"pattern": "*.jcl"})["generation"]
    assert "paths" in _call({"pattern": "*.jcl", "offset": 200, "generation": gen})
    assert _call({"pattern": "*.jcl", "offset": "200", "generation": gen})["error"] == "duplicate_tool_call"
    assert _call({"pattern": "**/*.jcl"})["error"] == "duplicate_tool_call"   # スラッシュなしは検索で **/ を補う
    assert _call({"pattern": "*.jcl", "unused": 1})["error"] == "duplicate_tool_call"   # 検索が使わない引数は同じ検索


def test_歩いたときもページの間で除外の規則を保存すると続きは_generation_違いのエラー(make_world, save_exclude):
    wid, names = make_world(ingest=False)
    run = _run_for(wid)
    first = run({"pattern": "*.jcl"})
    assert first["generation"] == "walk" and first["next_offset"]
    save_exclude(wid, names[0])
    r = run({"pattern": "*.jcl", "offset": first["next_offset"], "generation": first["generation"]})
    assert r["error"] == "glob_generation_mismatch"
