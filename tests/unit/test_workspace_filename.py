from __future__ import annotations


def test_workspace_filename_allows_japanese_and_leading_parenthesis():
    from sherpa.api import _safe_workspace_filename

    assert _safe_workspace_filename("(何でもいい)_20260628-0111.md") == "(何でもいい)_20260628-0111.md"


def test_workspace_filename_strips_path_components_and_rejects_unsafe_names():
    from sherpa.api import _safe_workspace_filename

    assert _safe_workspace_filename("../memo.md") == "memo.md"
    assert _safe_workspace_filename(r"..\memo.md") == "memo.md"
    assert _safe_workspace_filename(".env") is None
    assert _safe_workspace_filename("memo?.md") is None
    assert _safe_workspace_filename("memo\x00.md") is None
