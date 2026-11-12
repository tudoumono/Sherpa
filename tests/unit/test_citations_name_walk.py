"""ファイル名だけの参照が台帳の索引で見つからないとき、範囲内の原本の木を期限つきで 1 回歩いて引き直す。

一時ディレクトリ上の実ファイルと実物の台帳を使う（差し替えは資料フォルダの場所の解決だけ）。
"""
from __future__ import annotations

import logging
import uuid

import pytest

from sherpa import documents, scope_infer, store, worlds
from sherpa.ingest import worker
from sherpa.providers.codex import citations as C
from tests._replace_row import offline_row

pytestmark = pytest.mark.unit

_INGESTED = {
    "gen1/design/known.md": "# known\n",
    "gen1/design/dup.md": "# dup 1\n",
    "gen2/design/other.md": "# other\n",
}


@pytest.fixture
def world(monkeypatch, tmp_path):
    wid = "namewalk_" + uuid.uuid4().hex[:8]
    wd, arch, der = tmp_path / "world", tmp_path / "arch", tmp_path / "derived"
    for rel, text in _INGESTED.items():
        p = wd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    arch.mkdir()
    der.mkdir()
    monkeypatch.setattr(worlds, "world_dir", lambda w: wd)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: der)
    monkeypatch.setattr(worlds, "archives_dir", lambda w: arch)
    store.replace_documents(wid, worker._ledger_rows(wid), row=offline_row(wid))   # 取り込み済みはここまで。以降に置く資料は台帳に無い
    yield wid, wd
    store.replace_documents(wid, [], row=offline_row(wid))


def _put(wd, rel: str, text: str = "# new\n") -> None:
    p = wd / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _resolve(wid, name, scope_paths=None, layer=None):
    return C.resolve_referenced_docs_detail([[[name]]], wid, scope_paths, layer)


def test_name_missing_from_ledger_but_unique_in_scope_is_matched(world):
    wid, wd = world
    _put(wd, "gen1/design/late.md")
    res = _resolve(wid, "late.md", ["gen1"])
    assert res["verified"] == ["gen1/design/late.md"]
    assert res["mappings"][0]["class"] == C.RES_UNIQUE


def test_two_same_names_in_scope_stay_ambiguous(world):
    wid, wd = world
    _put(wd, "gen1/design/late.md")
    _put(wd, "gen1/src/late.md")
    res = _resolve(wid, "late.md", ["gen1"])
    assert res["verified"] == []
    assert res["unverified"] == [{"path": "late.md", "reason": C.REASON_AMBIGUOUS}]
    assert res["mappings"][0]["class"] == C.RES_AMBIGUOUS


def test_name_only_outside_scope_is_not_matched(world):
    wid, wd = world
    _put(wd, "gen2/design/late.md")
    res = _resolve(wid, "late.md", ["gen1"])
    assert res["verified"] == []
    assert res["unverified"] == [{"path": "late.md", "reason": C.REASON_UNREADABLE}]
    assert res["mappings"][0]["class"] == C.RES_NOT_FOUND


def test_sensitive_name_is_not_matched(world):
    wid, wd = world
    _put(wd, "gen1/design/credentials.xlsx", "x")
    res = _resolve(wid, "gen1/design/credentials.xlsx", ["gen1"])
    assert res["verified"] == []
    assert res["unverified"] == [{"path": "gen1/design/credentials.xlsx", "reason": C.REASON_SENSITIVE}]
    assert res["hidden"] == 0


def test_name_in_ledger_does_not_walk(world, monkeypatch):
    wid, _wd = world

    def boom(*a, **kw):
        raise AssertionError("台帳で見つかる名前なのに資料フォルダを歩いた")
    monkeypatch.setattr(scope_infer, "safe_files", boom)
    res = _resolve(wid, "known.md", ["gen1"])
    assert res["verified"] == ["gen1/design/known.md"]
    assert res["mappings"][0]["class"] == C.RES_UNIQUE


def test_walk_runs_once_for_several_missing_names(world, monkeypatch):
    wid, wd = world
    _put(wd, "gen1/design/late.md")
    calls = []
    real = documents.scope_rel_set

    def counting(*a, **kw):
        calls.append(1)
        return real(*a, **kw)
    monkeypatch.setattr(documents, "scope_rel_set", counting)
    res = C.resolve_referenced_docs_detail([[["late.md"]], [["absent1.md"]], [["absent2.md"]]], wid, ["gen1"], None)
    assert res["verified"] == ["gen1/design/late.md"]
    assert len(calls) == 1


def test_walk_deadline_is_shown_as_unchecked_and_logs(world, monkeypatch, caplog):
    wid, wd = world
    _put(wd, "gen1/design/late.md")
    monkeypatch.setattr(scope_infer, "USER_OP_WALK_SECONDS", -1.0)
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        res = _resolve(wid, "late.md", ["gen1"])
    assert res["verified"] == []
    assert res["unverified"] == [{"path": "late.md", "reason": C.REASON_UNCHECKED}]
    assert res["mappings"][0]["class"] == C.RES_UNCHECKED
    assert sum("exceeded deadline" in r.getMessage() for r in caplog.records) == 1


def test_walked_name_uses_confirmed_layer_not_extension(world):
    """拡張子では資料に見えても中身がソースのファイルは、資料だけの会話の名前の引き直しに出さない。"""
    wid, wd = world
    _put(wd, "gen1/conf/late.zzz", "A=1\nB=2\n")
    assert _resolve(wid, "late.zzz", ["gen1"], "docs")["verified"] == []
    assert _resolve(wid, "late.zzz", ["gen1"], "code")["verified"] == ["gen1/conf/late.zzz"]



def test_unreadable_subtree_is_shown_as_unchecked(world):
    import os
    if os.geteuid() == 0:
        pytest.skip("root では権限で読めなくできない")
    wid, wd = world
    _put(wd, "gen1/design/late.md")
    _put(wd, "gen1/locked/hidden.md")
    locked = wd / "gen1" / "locked"
    locked.chmod(0)
    try:
        res = _resolve(wid, "late.md", ["gen1"])
    finally:
        locked.chmod(0o755)
    assert res["verified"] == []
    assert res["mappings"][0]["class"] == C.RES_UNCHECKED


def test_file_the_ingest_would_skip_is_not_a_same_name_candidate(world):
    wid, wd = world
    _put(wd, "gen1/design/late.zzz", "text\n")
    (wd / "gen1" / "src").mkdir(parents=True)
    (wd / "gen1" / "src" / "late.zzz").write_bytes(b"\x00\x01\x02\x00binary")
    for layer in (None, "docs"):
        res = _resolve(wid, "late.zzz", ["gen1"], layer)
        assert res["verified"] == ["gen1/design/late.zzz"]


def test_name_lookup_without_scope_shows_unreadable_subtree_as_unchecked(world):
    import os
    if os.geteuid() == 0:
        pytest.skip("root では権限で読めなくできない")
    wid, wd = world
    _put(wd, "gen1/design/late.md")
    _put(wd, "gen1/locked/hidden.md")
    locked = wd / "gen1" / "locked"
    locked.chmod(0)
    try:
        res = _resolve(wid, "late.md")
    finally:
        locked.chmod(0o755)
    assert res["verified"] == []
    assert res["mappings"][0]["class"] == C.RES_UNCHECKED


def test_name_lookup_with_unreachable_world_dir_is_unchecked(world, monkeypatch):
    wid, _wd = world
    monkeypatch.setattr(worlds, "world_dir", lambda w: None)
    res = _resolve(wid, "late.md", ["gen1"])
    assert res["verified"] == []
    assert res["mappings"][0]["class"] == C.RES_UNCHECKED


def test_deadline_passing_during_the_layer_check_is_unchecked(world, monkeypatch):
    """最後の候補の分類で期限を過ぎたら、見つかった候補を確定として返さない。"""
    import time
    from sherpa import doc_reach
    wid, wd = world
    _put(wd, "gen1/design/late.md")
    monkeypatch.setattr(scope_infer, "USER_OP_WALK_SECONDS", 0.3)
    real = doc_reach.in_layer_confirmed

    def slow(*a, **kw):
        out = real(*a, **kw)
        time.sleep(0.4)
        return out
    monkeypatch.setattr(doc_reach, "in_layer_confirmed", slow)
    res = _resolve(wid, "late.md", ["gen1"])
    assert res["verified"] == []
    assert res["mappings"][0]["class"] == C.RES_UNCHECKED
