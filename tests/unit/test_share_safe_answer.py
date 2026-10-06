"""中身を伏せる共有（`shares._safe_share_answer`）が、画面と同じ意味で残すべき情報を落とさず、個人由来・未知の形を持ち込まないこと。
DB 非依存（純関数）。"""
from __future__ import annotations

from sherpa.store import shares


def _answer(**over):
    base = {
        "lens": "author", "scope": {"world": "w"}, "body": "本文", "headline": "本文", "answer_schema": 2, "completion": "partial",
        "notices": [{"kind": "stopped_early", "text": "途中までの結果です。"}],
        "sources": [{"doc_id": "a/b.md", "quote": "q",
                     "download_url": "/documents/download?world=w&rel=a%2Fb.md"}],
        "codex_stopped_early": True, "stop_kind": "codex_partial",
        "retry_hints": [{"kind": "resume", "label": "続きを調べる", "action": {"message": "続きを調べて"}},
                        {"kind": "depth", "label": "深さを上げる", "action": {"depth_profile": "max"}}],
        "limits": {"wall_clock_hit": True, "search_truncated": 2},
        "created_files": [{"name": "out.xlsx", "download_url": "/workspace/files/9/download"}],
    }
    base.update(over)
    return base


def test_share_keeps_download_link_stop_flags_retry_hints_limits_and_created_file_names():
    out = shares._safe_share_answer(_answer())
    assert out["sources"][0]["download_url"] == "/documents/download?world=w&rel=a%2Fb.md"
    assert out["codex_stopped_early"] is True and out["stop_kind"] == "codex_partial"
    assert [h["kind"] for h in out["retry_hints"]] == ["resume", "depth"]
    assert out["limits"] == {"wall_clock_hit": True, "search_truncated": 2}
    assert out["lens"] == "author" and out["completion"] == "partial"
    assert out["notices"] == [{"kind": "stopped_early", "text": "途中までの結果です。"}]
    # 作ったファイルは名前だけ（個人の作業領域のリンクは出さない）。
    assert out["created_files"] == [{"name": "out.xlsx"}]


def test_share_drops_forged_links_unknown_hint_actions_and_personal_keys():
    out = shares._safe_share_answer(_answer(
        sources=[{"doc_id": "a/b.md", "download_url": "https://evil.example/x"},
                 {"doc_id": "c.md", "download_url": "/documents/download?world=w&rel=other.md"},
                 {"doc_id": "d.md", "download_url": "/workspace/files/1/download"},
                 {"doc_id": "e.md", "download_url": "/documents/download?world=w2&rel=e.md"}],
        retry_hints=[{"kind": "resume", "label": "x", "action": {"message": "個人の質問文"}},
                     {"kind": "scope", "label": "x", "action": {"scope_paths": ["個人/秘密"]}}],
        limits={"search_truncated": 1, "private_note": "x"},
        personal_sources=[{"doc_id": "個人.txt", "quote": "秘密"}], _personal_facts=["x"]))
    assert all("download_url" not in s for s in out["sources"])
    assert "retry_hints" not in out
    assert out["limits"] == {"search_truncated": 1}
    assert "personal_sources" not in out and "_personal_facts" not in out


def test_redaction_wording_follows_the_actual_reason():
    assert shares._redaction_text({"codex_wrote_files": ["o.xlsx"]}) == shares._REDACTED_FILES_TEXT
    assert shares._redaction_text({"codex_wrote_files": ["o.xlsx"],
                                   "personal_sources": [{"doc_id": "p"}]}) == shares._REDACTED_TEXT
    assert shares._redaction_text({"headline": "x"}) == shares._REDACTED_TEXT
    assert shares._REDACTED_FILES_TEXT != shares._REDACTED_TEXT


def test_share_drops_download_link_when_answer_world_is_missing():
    out = shares._safe_share_answer(_answer(scope={}))
    assert "download_url" not in out["sources"][0]


def test_every_notice_kind_emitted_in_sherpa_is_in_the_closed_vocabulary():
    import pathlib
    import re
    from sherpa import answer_shape
    pats = (r'add_notice\(\s*[\w.\[\]"]+,\s*"([a-z_]+)"', r'append_answer_notice\(\s*\w+,\s*"([a-z_]+)"',
            r'_answer_notices\.append\(\s*\(\s*"([a-z_]+)"')
    used = set()
    for f in pathlib.Path(answer_shape.__file__).parent.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        for pat in pats:
            used.update(re.findall(pat, text))
    assert len(used) >= 15, used
    assert used - set(answer_shape.NOTICE_KINDS) == set()
