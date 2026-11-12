"""Codex への指示: 続きが取れる打ち切りと取れない打ち切りの書き分け。"""
from __future__ import annotations

import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
from sherpa import agents, codex_agents_md   # noqa: E402

_PAGED = ("graph_neighbors", "folders_truncated")


def test_paged_tools_are_not_listed_as_having_no_way_to_continue(tmp_path):
    prompt = agents.CodexProvider()._prompt_mcp("影響は?", "troubleshoot", "v1")
    d = tmp_path / "authoring"
    d.mkdir()
    codex_agents_md.write_agents_md(d)
    md = (d / "AGENTS.md").read_text(encoding="utf-8")
    for text in (prompt, md):
        head, _, tail = text.partition("取得する手段が無い打ち切り")
        no_continue = tail.split("。")[0]
        assert not any(name in no_continue for name in _PAGED)
        assert "es_search" in no_continue
        assert "next_offset" in text and all(name in text for name in _PAGED)


def test_text_only_model_is_not_told_to_check_page_images(tmp_path):
    """画像を読めない構成では「ページ画像を開いて確かめる」と指示せず、画像にしか無い根拠は確かめられないと回答に書かせる。読める構成は従来どおり。"""
    from sherpa import codex_agents_md as M, image_input
    M.write_agents_md(tmp_path, image_input=False)
    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "view_image" not in text and "pdf_page_image" not in text and "確かめられなかったと回答に書く" in text
    M.write_agents_md(tmp_path)
    assert "tools.view_image(" in (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert image_input.codex_image_input({}, ollama=True) is False and image_input.codex_image_input({}, ollama=False) is True
    assert image_input.codex_image_input({"codex_image_input": {"ollama": True}}, ollama=True) is True
    assert image_input.codex_image_input({"codex_image_input": {"ollama": "yes"}}, ollama=True) is False
