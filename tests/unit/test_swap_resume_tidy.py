"""再開で古い派生（`.retired`）を消せなかったときは、掃除済みとして扱わず例外を上へ伝える。"""
from __future__ import annotations

import os

import pytest

from sherpa import worlds
from sherpa.ingest import swap_resume


@pytest.fixture
def layer(tmp_path, monkeypatch):
    md = tmp_path / "md"
    md.mkdir()
    retired = tmp_path / "md.retired"
    (retired / "sub").mkdir(parents=True)
    (retired / "sub" / "a.txt").write_text("old", encoding="utf-8")
    monkeypatch.setattr(worlds, "derived_md_dir", lambda world: md)
    yield md, retired
    if (retired / "sub").exists():
        os.chmod(retired / "sub", 0o755)


def test_tidy_removes_the_retired_layer(layer):
    md, retired = layer
    assert swap_resume._tidy("w", "md") is True
    assert not retired.exists()


def test_tidy_is_fine_when_there_is_nothing_to_remove(layer):
    md, retired = layer
    swap_resume._tidy("w", "md")
    assert swap_resume._tidy("w", "md") is True


def test_tidy_propagates_a_failure_to_remove_the_retired_layer(layer):
    md, retired = layer
    os.chmod(retired / "sub", 0o555)   # 中のファイルを消せない
    if os.access(retired / "sub", os.W_OK):
        pytest.skip("権限で消せない状況を作れない環境")
    with pytest.raises(OSError):
        swap_resume._tidy("w", "md")
    assert (retired / "sub" / "a.txt").exists()
