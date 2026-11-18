"""`documents.scope_rel_set`（範囲の部分木だけを歩く）の契約テスト。一時ディレクトリの実ファイルで確かめる。"""
from __future__ import annotations

import os

import pytest

from sherpa import documents, worlds


@pytest.fixture
def root(monkeypatch, tmp_path):
    wd = tmp_path.resolve() / "world"
    for rel in ("a/x/1.md", "a/2.md", "b/3.md", "a/.git/c.md", "a/_重要度.txt"):
        p = wd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    os.symlink(wd / "b", wd / "a" / "lnk")
    monkeypatch.setattr(worlds, "world_dir", lambda w: wd)
    monkeypatch.setattr(worlds, "archives_dir", lambda w: wd.parent / "no-archives")
    return wd


def test_walks_only_the_scope_subtree_with_the_same_exclusions(root):
    assert documents.scope_rel_set("w", ["a"]) == {"a/2.md", "a/x/1.md"}


def test_symlinked_scope_is_refused_and_file_scope_is_kept(root):
    assert documents.scope_rel_set("w", ["a/lnk"]) == set()
    assert documents.scope_rel_set("w", ["b/3.md"]) == {"b/3.md"}


def test_no_scope_is_the_whole_folder(root):
    assert documents.scope_rel_set("w", []) == documents.world_rel_set("w")
