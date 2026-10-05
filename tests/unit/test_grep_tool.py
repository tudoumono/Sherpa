"""grep_tool（直接 grep ツール）の契約テスト。

読込バイト上限・ヒット引用バイト上限・打切り申告・ストリーミング走査のメモリ上界・deadline・
重要度 top-K・layer・rag 優先の派生 MD・秘匿名/サイズ超過の除外を確かめる。
"""
from __future__ import annotations

import pathlib
import time
import tracemalloc

import pytest

from sherpa import grep_tool as G

# 拡張子ごとの固定分類（コード/資料・サイズ上限）を前提にするため、登録簿を上流限定に固定する。
pytestmark = pytest.mark.usefixtures("upstream_only_registry")


def _write(tmp_path: pathlib.Path, name: str, content) -> pathlib.Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return p


def _derived_world(monkeypatch, tmp_path, legacy=None, rag=None, obs=None, world_files=None):
    """`grep_search`（roots=None）が読む world_dir/派生 MD/rag/観測ディレクトリを tmp_path 配下へ差し替える。
    legacy/rag/obs/world_files は {相対名: 本文}。"""
    from sherpa import worlds as W
    world_root = tmp_path / "world"
    world_root.mkdir()
    der = tmp_path / "derived" / "md"
    der_rag = tmp_path / "derived" / "rag"
    obs_root = tmp_path / "observations" / "gen1" if obs is not None else None
    for root, files in ((world_root, world_files), (der, legacy), (der_rag, rag), (obs_root, obs)):
        for name, body in (files or {}).items():
            _write(root, name, body)
    der.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(W, "world_dir", lambda w: world_root)
    monkeypatch.setattr(W, "derived_md_dir", lambda w: der)
    monkeypatch.setattr(W, "derived_rag_dir", lambda w: der_rag)
    monkeypatch.setattr(W, "observation_current_dir", lambda w: obs_root)


def _clock_exceeding_after(monkeypatch, ok_calls: int):
    """`ok_calls` 回までは期限内・以降は期限超過を返す monotonic。呼び出し回数を返す dict を戻す。"""
    calls = {"n": 0}

    def _clock():
        calls["n"] += 1
        return 0.0 if calls["n"] <= ok_calls else 100.0

    monkeypatch.setattr(G.time, "monotonic", _clock)
    return calls


# ===== 読込上限（cap）・打切り申告 =====

def test_file_cap_bounds_read_and_reports_file_truncated(monkeypatch, tmp_path):
    """cap より前のヒットは返り（file_truncated=True）、後ろは検索されない。全量 read_text はしない。"""
    line1 = "NEEDLE line one\n"
    _write(tmp_path, "doc.txt", line1 + "x" * 200 + "\n" + "NEEDLE line far beyond cap\n")
    monkeypatch.setattr(G, "_GREP_FILE_CAP_BYTES", len(line1.encode("utf-8")) + 10)

    def _boom(*a, **kw):
        raise AssertionError("read_text は呼ばれない実装であるべき")
    monkeypatch.setattr(pathlib.Path, "read_text", _boom)

    hits = G.grep_search("NEEDLE", world="v1", roots=[tmp_path])
    assert len(hits) == 1 and hits[0]["line"] == 1
    assert "NEEDLE line one" in hits[0]["text"] and hits[0]["file_truncated"] is True


def test_truncated_mid_line_not_returned_as_hit(monkeypatch, tmp_path):
    """cap が行の途中に落ちるとき、query を含み終えていても切れた最終行は採用しない。cap 無しなら返る。"""
    line1 = "header no match\n"
    content = line1 + "NEEDLE tail padding padding padding padding\n"
    _write(tmp_path, "doc.txt", content)
    full = G.grep_search("NEEDLE", world="v1", roots=[tmp_path])
    assert len(full) == 1 and full[0]["line"] == 2

    cap = len(line1.encode("utf-8")) + len("NEEDLE") + 5
    assert cap < len(content.encode("utf-8"))
    monkeypatch.setattr(G, "_GREP_FILE_CAP_BYTES", cap)
    assert G.grep_search("NEEDLE", world="v1", roots=[tmp_path]) == []


@pytest.mark.parametrize("cap_at, query, line, truncated_key", [
    ("full", "NEEDLE", 2, False),          # サイズちょうど cap の完全なファイルは打切り扱いにしない
    ("after_line1", "header", 1, True),    # 改行直後で cap ＝ line1 は完全に読めている
])
def test_cap_boundary_keeps_last_complete_line(monkeypatch, tmp_path, cap_at, query, line, truncated_key):
    line1 = "header no match\n"
    content = line1 + "NEEDLE full line exactly at cap\n"
    _write(tmp_path, "doc.txt", content)
    cap = len(content.encode("utf-8")) if cap_at == "full" else len(line1.encode("utf-8"))
    monkeypatch.setattr(G, "_GREP_FILE_CAP_BYTES", cap)
    hits = G.grep_search(query, world="v1", roots=[tmp_path])
    assert len(hits) == 1 and hits[0]["line"] == line
    assert ("file_truncated" in hits[0]) is truncated_key


def test_default_cap_finds_hit_beyond_old_8mib_default(tmp_path):
    """既定 cap は旧 8MiB より大きい（旧既定より後ろの一致が見つかり、打切りにならない）。"""
    old_default = 8 * 1024 * 1024
    unit = "x" * 999 + "\n"
    content = unit * ((old_default // len(unit)) + 100) + "NEEDLE_BEYOND_OLD_CAP\n"
    _write(tmp_path, "big.txt", content)     # .md だと見出し無しの1節が text クリップに掛かるため .txt
    assert len(content.encode("utf-8")) > old_default
    hits = G.grep_search("NEEDLE_BEYOND_OLD_CAP", world="v1", roots=[tmp_path])
    assert len(hits) == 1 and "NEEDLE_BEYOND_OLD_CAP" in hits[0]["text"]
    assert "file_truncated" not in hits[0]


def test_truncated_docs_reports_file_with_no_hits(monkeypatch, tmp_path):
    """ヒット 0 件の打切り文書も truncated_docs に載る。渡さない呼び出し元は従来どおり。"""
    _derived_world(monkeypatch, tmp_path, legacy={"big.docx.md": "x" * 400 + "\nNEEDLE_TAIL\n"})
    monkeypatch.setattr(G, "_GREP_FILE_CAP_BYTES", 100)
    truncated: list = []
    assert G.grep_search("NEEDLE_TAIL", world="anyworld", truncated_docs=truncated) == []
    assert truncated == ["big.docx"]
    assert G.grep_search("NEEDLE_TAIL", world="anyworld") == []


def test_truncated_docs_misses_files_past_the_early_exit_point_when_imp_map_empty(monkeypatch, tmp_path):
    """imp_map 空で max_hits により早期終了したとき、それより後の打切り文書は申告されない。"""
    _derived_world(monkeypatch, tmp_path, legacy={
        "a.docx.md": "NEEDLE\n", "z_big.docx.md": "x" * 400 + "\nNEEDLE\n"})
    monkeypatch.setattr(G, "_GREP_FILE_CAP_BYTES", 100)
    truncated: list = []
    hits = G.grep_search("NEEDLE", world="anyworld", max_hits=1, truncated_docs=truncated)
    assert [h["doc_id"] for h in hits] == ["a.docx"] and truncated == []


def test_truncated_docs_deduped_and_empty_when_not_truncated(monkeypatch, tmp_path):
    _derived_world(monkeypatch, tmp_path, legacy={"small.docx.md": "## 節\nNEEDLE ここ\n\n## 節2\nNEEDLE そこ\n"})
    truncated: list = []
    hits = G.grep_search("NEEDLE", world="anyworld", truncated_docs=truncated)
    assert hits and truncated == [] and all("file_truncated" not in h for h in hits)


def test_line_overflow_reports_truncation_even_without_file_cap(monkeypatch, tmp_path):
    """1 行が _GREP_LINE_MAX_BYTES を超えて改行が来ないとき、cap 内でも打切りを申告する。"""
    monkeypatch.setattr(G, "_GREP_LINE_MAX_BYTES", 100)
    _write(tmp_path, "bigline.txt", "x" * 150 + "NEEDLE_TAIL\n")
    truncated: list = []
    assert G.grep_search("NEEDLE_TAIL", world="v1", roots=[tmp_path], truncated_docs=truncated) == []
    assert truncated == ["bigline.txt"]


# ===== 引用クリップ・env 検証 =====

def test_hit_text_clipped_to_max_bytes_and_valid_utf8(monkeypatch, tmp_path):
    monkeypatch.setattr(G, "_GREP_HIT_TEXT_MAX_BYTES", 100)
    _write(tmp_path, "big.md", "NEEDLE " + ("あ" * 5000))   # 見出し無し＝1 節がファイル全体
    hits = G.grep_search("NEEDLE", world="v1", roots=[tmp_path])
    assert len(hits) == 1
    assert len(hits[0]["text"].encode("utf-8")) <= 100


def test_clip_utf8_bytes_does_not_break_multibyte_boundary():
    clipped = G._clip_utf8_bytes("あ" * 100, 10)
    assert len(clipped.encode("utf-8")) <= 10


# ===== 正常系の形 =====

def test_normal_small_file_hit_shape(tmp_path):
    content = "# 見出しA\n消費税率テスト値を含む一行\n本文2行目\n"
    _write(tmp_path, "sub/doc.md", content)
    hits = G.grep_search("消費税率", world="v1", roots=[tmp_path])
    assert len(hits) == 1
    assert hits[0] == {"doc_id": "sub/doc.md", "path": hits[0]["path"], "ext": ".md", "line": 2,
                       "span": [1, 3], "text": content.strip(), "match": "消費税率"}


def test_normal_source_file_context_window_unaffected(tmp_path):
    lines = [f"line {i}" for i in range(1, 8)]
    lines[3] = "line 4 TAX-RATE"
    _write(tmp_path, "PROG.cbl", "\n".join(lines) + "\n")
    hits = G.grep_search("TAX-RATE", world="v1", roots=[tmp_path])
    assert len(hits) == 1 and hits[0]["line"] == 4 and hits[0]["span"] == [2, 6]
    assert hits[0]["text"] == "\n".join(lines[1:6])


def test_streaming_source_multiple_hits_adjacent_and_far_apart(tmp_path):
    lines = [f"line {i}" for i in range(1, 21)]
    lines[2] = "line 3 NEEDLE"
    lines[3] = "line 4 NEEDLE"
    lines[14] = "line 15 NEEDLE"
    _write(tmp_path, "PROG.cbl", "\n".join(lines) + "\n")
    hits = {h["line"]: h for h in G.grep_search("NEEDLE", world="v1", roots=[tmp_path], max_hits=10)}
    assert set(hits) == {3, 4, 15}
    assert hits[3]["span"] == [1, 5] and hits[3]["text"] == "\n".join(lines[0:5])
    assert hits[4]["span"] == [2, 6] and hits[4]["text"] == "\n".join(lines[1:6])
    assert hits[15]["span"] == [13, 17] and hits[15]["text"] == "\n".join(lines[12:17])


def test_grep_search_excludes_declined_registered_code_extension(monkeypatch, tmp_path):
    """登録拡張子でも accepts() が全滅し既存の資料種別にも該当しなければ grep 対象外（classify_document が実行ゲート）。"""
    from sherpa.ingest.analyzers import registry
    from sherpa.ingest.analyzers._base import Analyzer, DefResult, RefResult

    class _AlwaysDeclineCobol(Analyzer):
        name = "decline_cobol"
        extensions = frozenset({".cbl"})

        def accepts(self, rel_path, head_text=""):
            return False

        def collect_defs(self, text, rel_path):
            return DefResult()

        def extract_refs(self, text, rel_path):
            return RefResult()

    monkeypatch.setattr(registry, "_ANALYZERS", (_AlwaysDeclineCobol(),))
    _write(tmp_path, "PROG.cbl", "line 1\nline 2 TAX-RATE\nline 3\n")
    assert G.grep_search("TAX-RATE", world="v1", roots=[tmp_path]) == []


@pytest.mark.parametrize("body", [
    "先頭行\f2行目のはず\r3行目のはず\x854行目 NEEDLE を含む\n6行目...ではなく5行目\n",   # \f・\r・NEL
    "1行目\n\n\n4行目 NEEDLE\n",                                                       # 連続改行＝空行
])
def test_line_numbers_match_splitlines(monkeypatch, tmp_path, body):
    """行番号は read_around/read_doc と同じ str.splitlines() の数え方。"""
    _derived_world(monkeypatch, tmp_path, legacy={"doc.docx.md": body})
    hits = G.grep_search("NEEDLE", world="anyworld")
    assert len(hits) == 1
    assert hits[0]["line"] == 4 == next(i + 1 for i, ln in enumerate(body.splitlines()) if "NEEDLE" in ln)


def test_grep_search_bom_utf8_first_heading_recognized(tmp_path):
    _write(tmp_path, "doc.md", b"\xef\xbb\xbf" + "# 見出し\nNEEDLE本文\n".encode("utf-8"))
    hits = G.grep_search("NEEDLE", world="v1", roots=[tmp_path])
    assert len(hits) == 1
    assert "﻿" not in hits[0]["text"] and hits[0]["text"].startswith("# 見出し")


def test_grep_search_cp932_detected_despite_long_ascii_prefix(monkeypatch, tmp_path):
    from sherpa import text_encoding
    monkeypatch.setattr(text_encoding, "_SCAN_CHUNK_BYTES", 8)
    raw = ("A" * 64 + "\r\n").encode("ascii") + "架空の締め処理の通知メッセージ\r\n".encode("cp932")
    _write(tmp_path, "batch.cbl", raw)
    hits = G.grep_search("締め処理", world="v1", roots=[tmp_path])
    assert len(hits) == 1 and "�" not in hits[0]["text"] and "締め処理" in hits[0]["text"]


# ===== 派生 MD（rag 優先・legacy 縮退）・秘匿名 =====

def test_strip_derived_suffix_priority_and_passthrough():
    assert G.strip_derived_suffix("report.docx.rag.md") == "report.docx"
    assert G.strip_derived_suffix("image.png.rag_observations.md") == "image.png"
    assert G.strip_derived_suffix("report.docx.md") == "report.docx"
    assert G.strip_derived_suffix("設計/資料.pdf.md") == "設計/資料.pdf"
    assert G.strip_derived_suffix("PROG.cbl") == "PROG.cbl"


def test_grep_search_prefers_rag_over_legacy_and_falls_back_per_document(monkeypatch, tmp_path):
    """rag.md がある文書は rag 版だけ（二重ヒットなし）、無い文書は legacy。OCR 観測は rag.md へ統合済みで
    観測専用ツリーは走査しない。判定は文書ごと。"""
    _derived_world(
        monkeypatch, tmp_path,
        legacy={"report.docx.md": "legacy NEEDLE in report\n", "plain.xlsx.md": "legacy-only NEEDLE in plain\n"},
        rag={"report.docx.rag.md": "## 見出し\nrag NEEDLE in report\n", "scan.png.rag.md": "## AI観測\nOCR NEEDLE in scan\n"},
        obs={"scan.png.rag_observations.md": "OCR NEEDLE in scan（観測専用ツリー・grep対象外）\n"})
    hits = {h["doc_id"]: h for h in G.grep_search("NEEDLE", world="anyworld", max_hits=10)}
    assert set(hits) == {"report.docx", "plain.xlsx", "scan.png"}
    assert hits["report.docx"]["ext"] == ".docx"
    assert "rag NEEDLE" in hits["report.docx"]["text"] and "legacy NEEDLE in report" not in hits["report.docx"]["text"]
    assert "legacy-only NEEDLE" in hits["plain.xlsx"]["text"]
    assert "OCR NEEDLE" in hits["scan.png"]["text"] and "観測専用" not in hits["scan.png"]["text"]


def test_grep_search_excludes_derived_md_with_sensitive_original_name(monkeypatch, tmp_path):
    """派生 MD は classify_document を通らないため、復元した原本名が秘匿名なら実在してもヒットさせない。"""
    _derived_world(monkeypatch, tmp_path, legacy={
        "credentials.xlsx.md": "SECRET NEEDLE body\n", "id_rsa.docx.md": "SECRET NEEDLE body\n",
        "normal.docx.md": "normal NEEDLE body\n"})
    assert [h["doc_id"] for h in G.grep_search("NEEDLE", world="anyworld")] == ["normal.docx"]


def test_grep_search_uses_preferred_derived_name_helper(monkeypatch, tmp_path):
    """rag/legacy 優先を再実装せず共有ヘルパー preferred_derived_name を経由する。"""
    _derived_world(monkeypatch, tmp_path, legacy={"report.docx.md": "legacy NEEDLE\n"},
                   rag={"report.docx.rag.md": "## 見出し\nrag NEEDLE\n"})
    calls = []
    orig = G.preferred_derived_name

    def spy(root, rel):
        calls.append(rel)
        return orig(root, rel)

    monkeypatch.setattr(G, "preferred_derived_name", spy)
    hits = G.grep_search("NEEDLE", world="anyworld")
    assert len(hits) == 1 and hits[0]["doc_id"] == "report.docx" and "report.docx" in calls


def test_grep_search_no_derived_files_yields_no_hits(monkeypatch, tmp_path):
    _derived_world(monkeypatch, tmp_path)
    assert G.grep_search("NEEDLE", world="anyworld") == []


# ===== deadline =====

@pytest.mark.parametrize("n_files, query, world", [
    (G._DEADLINE_CHECK_ENTRIES + 10, "NEEDLE", "v1"),
    (48, "NEEDLE", "v1"),        # 間引き間隔未満でも開始直後の確認で即例外（列挙・読込をしない）
    (0, "", "v1"),               # 空 query・不正 world でも期限切れが先
    (0, "NEEDLE", "../bad"),
])
def test_grep_search_deadline_already_past_raises(tmp_path, n_files, query, world):
    for i in range(n_files):
        _write(tmp_path, f"f{i:04d}.md", "NEEDLE\n")
    with pytest.raises(G.GrepDeadlineExceeded):
        list(G.grep_search(query, world=world, roots=[tmp_path], deadline=time.monotonic() - 1))


def test_grep_search_deadline_checked_mid_enumeration_for_huge_entry_count(monkeypatch, tmp_path):
    """列挙段階で _DEADLINE_CHECK_ENTRIES 件ごとに継続して確認する（1 回目は通し 2 回目で超過）。"""
    for i in range(G._DEADLINE_CHECK_ENTRIES * 3 + 10):
        _write(tmp_path, f"f{i:04d}.md", "NEEDLE\n")
    calls = _clock_exceeding_after(monkeypatch, 1)
    with pytest.raises(G.GrepDeadlineExceeded):
        list(G.grep_search("NEEDLE", world="v1", roots=[tmp_path], deadline=50.0))
    assert calls["n"] >= 2


@pytest.mark.parametrize("n_files, n_lines, max_hits, ok_calls, expected_calls, exact", [
    (5, 1, 10, 2, 3, False),                          # 各エントリ処理ごとの確認（列挙の間引きは発火しない件数）
    (1, 1, 10, 3, 4, True),                           # ファイル読込直後（decode・走査の前）
    (1, G._DEADLINE_CHECK_LINES + 10, 10, 4, 5, True),  # 行走査ループ内の間引き確認
    (1, 1, 1, 4, 5, True),                            # max_hits 早期 return の直前（部分成功も例外）
])
def test_grep_search_deadline_checkpoints(monkeypatch, tmp_path, n_files, n_lines, max_hits, ok_calls,
                                          expected_calls, exact):
    body = "NEEDLE\n" if n_lines == 1 else "\n".join("x" for _ in range(n_lines)) + "\n"
    for i in range(n_files):
        _write(tmp_path, f"f{i:04d}.md", body)
    calls = _clock_exceeding_after(monkeypatch, ok_calls)
    with pytest.raises(G.GrepDeadlineExceeded):
        list(G.grep_search("NEEDLE", world="v1", roots=[tmp_path], max_hits=max_hits, deadline=50.0))
    assert (calls["n"] == expected_calls) if exact else (calls["n"] >= expected_calls)


def test_grep_search_deadline_not_exceeded_keeps_sorted_order(tmp_path):
    for name in ("b.md", "a.md", "c.md"):
        _write(tmp_path, name, "NEEDLE here\n")
    without = [h["doc_id"] for h in G.grep_search("NEEDLE", world="v1", roots=[tmp_path])]
    withd = [h["doc_id"] for h in G.grep_search(
        "NEEDLE", world="v1", roots=[tmp_path], deadline=time.monotonic() + 3600)]
    assert without == withd == ["a.md", "b.md", "c.md"]


def test_grep_search_stops_scanning_at_max_hits_when_imp_map_empty(monkeypatch, tmp_path):
    """imp_map 空は max_hits 到達でファイル境界で終了し、2 文書目へ進まない（deadline 確認 5 回のみ）。"""
    _write(tmp_path, "a.md", "NEEDLE\n")
    _write(tmp_path, "b.md", "NEEDLE\n")
    calls = _clock_exceeding_after(monkeypatch, 10**9)
    hits = G.grep_search("NEEDLE", world="v1", roots=[tmp_path], max_hits=1, deadline=50.0)
    assert [h["doc_id"] for h in hits] == ["a.md"] and calls["n"] == 5


def test_grep_search_continues_scanning_past_max_hits_when_imp_map_nonempty(monkeypatch, tmp_path):
    """_重要度.txt がある world は max_hits 到達後も全量走査する（2 文書目で期限超過すれば例外）。"""
    _derived_world(monkeypatch, tmp_path, world_files={
        "a.md": "NEEDLE\n", "b.md": "NEEDLE\n", "_重要度.txt": "a.md: 中\n"})
    calls = _clock_exceeding_after(monkeypatch, 6)
    with pytest.raises(G.GrepDeadlineExceeded):
        list(G.grep_search("NEEDLE", world="anyworld", max_hits=1, deadline=50.0))
    assert calls["n"] >= 7


# ===== valid_world =====

@pytest.mark.parametrize("value, ok", [
    ("v1", True), ("test_world-2", True), ("a" * 64, True),
    ("v1\n", False), ("v1\r\n", False),        # fullmatch（`$` が末尾改行に当たる抜け穴を塞ぐ）
    ("", False), (None, False), ("../etc", False), ("a b", False), ("a" * 65, False),
])
def test_valid_world(value, ok):
    assert G.valid_world(value) is ok


# ===== layer =====

@pytest.mark.parametrize("kwargs, expected", [
    ({}, {"設計/仕様.md", "src/PROG.cbl"}),
    ({"layer": "both"}, {"設計/仕様.md", "src/PROG.cbl"}),
    ({"layer": "code"}, {"src/PROG.cbl"}),
    ({"layer": "docs"}, {"設計/仕様.md"}),
])
def test_grep_search_layer_filter(tmp_path, kwargs, expected):
    _write(tmp_path, "設計/仕様.md", "# 見出し\nNEEDLE を含む資料\n")
    _write(tmp_path, "src/PROG.cbl", "NEEDLE を含む行\n" + "\n" * 3)
    assert {h["doc_id"] for h in G.grep_search("NEEDLE", world="v1", roots=[tmp_path], **kwargs)} == expected


def test_grep_search_layer_invalid_value_raises(tmp_path):
    _write(tmp_path, "src/PROG.cbl", "NEEDLE\n")
    with pytest.raises(ValueError):
        G.grep_search("NEEDLE", world="v1", roots=[tmp_path], layer="bogus")


def test_grep_search_layer_code_with_derived_office_md_is_always_zero_hits(monkeypatch, tmp_path):
    """決定的 MD（Office/PDF 由来）は常に docs 判定。"""
    _derived_world(monkeypatch, tmp_path, legacy={"report.docx.md": "NEEDLE 本文\n"})
    assert G.grep_search("NEEDLE", world="anyworld", layer="code") == []
    docs_hits = G.grep_search("NEEDLE", world="anyworld", layer="docs")
    assert len(docs_hits) == 1 and docs_hits[0]["doc_id"] == "report.docx"


# ===== 重要度（_重要度.txt）の除外と top-K 選抜 =====

def test_grep_search_excludes_importance_control_file(tmp_path):
    _write(tmp_path, "a.md", "NEEDLE here\n")
    _write(tmp_path, "_重要度.txt", "NEEDLE: 高\n")
    assert {h["doc_id"] for h in G.grep_search("NEEDLE", world="v1", roots=[tmp_path])} == {"a.md"}


def test_grep_search_no_control_file_preserves_discovery_order(tmp_path):
    for name in ("d.md", "b.md", "a.md", "c.md", "e.md"):
        _write(tmp_path, name, "NEEDLE here\n")
    hits = G.grep_search("NEEDLE", world="v1", roots=[tmp_path], max_hits=3)
    assert [h["doc_id"] for h in hits] == ["a.md", "b.md", "c.md"]
    assert all("importance" not in h and "importance_reason" not in h for h in hits)


def test_grep_search_prioritizes_high_importance_hit_discovered_after_max_hits(monkeypatch, tmp_path):
    _derived_world(monkeypatch, tmp_path, world_files={
        "a.md": "NEEDLE here\n", "b.md": "NEEDLE here\n", "z_important.md": "NEEDLE here\n",
        "_重要度.txt": "z_important.md: 高\n"})
    hits = G.grep_search("NEEDLE", world="anyworld", max_hits=2)
    doc_ids = [h["doc_id"] for h in hits]
    assert len(doc_ids) == 2 and doc_ids[0] == "z_important.md" and hits[0]["importance"] == "高"
    assert "a.md" in doc_ids or "b.md" in doc_ids


def test_grep_search_low_importance_hit_dropped_first_when_over_capacity(monkeypatch, tmp_path):
    _derived_world(monkeypatch, tmp_path, world_files={
        "a_low.md": "NEEDLE here\n", "b.md": "NEEDLE here\n", "c.md": "NEEDLE here\n",
        "_重要度.txt": "a_low.md: 低\n"})
    hits = G.grep_search("NEEDLE", world="anyworld", max_hits=2)
    assert [h["doc_id"] for h in hits] == ["b.md", "c.md"]


def test_grep_search_importance_reason_is_conditional_key(monkeypatch, tmp_path):
    _derived_world(monkeypatch, tmp_path, world_files={
        "a.md": "NEEDLE here\n", "b.md": "NEEDLE here\n", "_重要度.txt": "a.md: 高  # 契約書\nb.md: 低\n"})
    hits = {h["doc_id"]: h for h in G.grep_search("NEEDLE", world="anyworld", max_hits=10)}
    assert hits["a.md"]["importance"] == "高" and hits["a.md"]["importance_reason"] == "契約書"
    assert hits["b.md"]["importance"] == "低" and "importance_reason" not in hits["b.md"]


# ===== ページング =====

def test_grep_search_offset_pages_through_all_hits_without_gaps_or_duplicates(tmp_path):
    for name in ("d.md", "b.md", "a.md", "c.md", "e.md"):
        _write(tmp_path, name, "NEEDLE here\n")
    full = [h["doc_id"] for h in G.grep_search("NEEDLE", world="v1", roots=[tmp_path], max_hits=10)]
    assert full == ["a.md", "b.md", "c.md", "d.md", "e.md"]
    explicit = [h["doc_id"] for h in G.grep_search("NEEDLE", world="v1", roots=[tmp_path], max_hits=3, offset=0)]
    assert explicit == full[:3]

    collected: list = []
    offset = 0
    while True:
        doc_ids = [h["doc_id"] for h in G.grep_search("NEEDLE", world="v1", roots=[tmp_path], max_hits=2, offset=offset)]
        if not doc_ids:
            break
        collected.extend(doc_ids)
        offset += 2
    assert collected == full


def test_grep_search_offset_extends_heap_capacity_so_later_hits_become_reachable(tmp_path):
    """早期終了の閾値は heap_cap（max_hits+offset）。offset を進めると発見順の後方にも届く。"""
    for name in ("d.md", "b.md", "a.md", "c.md", "e.md"):
        _write(tmp_path, name, "NEEDLE here\n")
    first = [h["doc_id"] for h in G.grep_search("NEEDLE", world="v1", roots=[tmp_path], max_hits=2)]
    assert first == ["a.md", "b.md"]
    later = [h["doc_id"] for h in G.grep_search("NEEDLE", world="v1", roots=[tmp_path], max_hits=2, offset=3)]
    assert later == ["d.md", "e.md"]


# ===== ストリーミング走査のメモリ上界 =====

def _peak_bytes(fn):
    tracemalloc.start()
    try:
        result = fn()
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return result, peak


def test_large_file_normal_lines_bounded_memory(tmp_path):
    line = "x" * 200 + "\n"
    content = line * ((20 * 1024 * 1024) // len(line) + 10) + "NEEDLE_TAIL_LINE\n"
    _write(tmp_path, "large.txt", content)
    assert len(content.encode("utf-8")) > 20 * 1024 * 1024
    hits, peak = _peak_bytes(lambda: G.grep_search("NEEDLE_TAIL_LINE", world="v1", roots=[tmp_path]))
    assert len(hits) == 1 and peak < 2 * 1024 * 1024


def test_many_matches_in_single_file_bounded_memory_and_respects_max_hits(tmp_path):
    """1 ファイルに max_hits を大きく超える一致があっても、ピーク割当は一致総数に比例しない。"""
    _write(tmp_path, "many.txt", "NEEDLE\n" * 200_000)
    hits, peak = _peak_bytes(lambda: G.grep_search("NEEDLE", world="v1", roots=[tmp_path], max_hits=5))
    assert [h["line"] for h in hits] == [1, 2, 3, 4, 5] and peak < 3 * 1024 * 1024


def test_single_huge_line_bounded_memory(tmp_path):
    _write(tmp_path, "huge_single_line.txt", "x" * (30 * 1024 * 1024))
    hits, peak = _peak_bytes(lambda: G.grep_search("NEEDLE", world="v1", roots=[tmp_path]))
    assert hits == [] and peak < 15 * 1024 * 1024


# ===== 軽量テキスト枠・登録コードのサイズ超過は台帳/ES と同じ text_kind.MAX_BYTES で除外し申告 =====

@pytest.mark.parametrize("name, content", [
    ("doc.csv", "x" * 100 + "NEEDLE\n"),
    ("script.py", "x = 1\n" * 20 + "NEEDLE = 1\n"),
    ("NEEDLE.cbl", "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. NEEDLE.\n" + "       DISPLAY 'X'.\n" * 10),
])
def test_grep_search_excludes_oversized_text_and_reports_truncated(monkeypatch, tmp_path, name, content):
    monkeypatch.setattr("sherpa.ingest.text_kind.MAX_BYTES", 50)
    assert len(content.encode("utf-8")) > 50
    _write(tmp_path, name, content)
    truncated: list = []
    assert G.grep_search("NEEDLE", world="v1", roots=[tmp_path], truncated_docs=truncated) == []
    assert truncated == [name]


def test_grep_search_size_exclusion_single_source_of_truth_via_text_kind_max_bytes(monkeypatch, tmp_path):
    """値を上げれば同じファイルが再び検索対象に戻り、申告も消える。"""
    content = "x" * 100 + "NEEDLE\n"
    size = len(content.encode("utf-8"))
    _write(tmp_path, "doc.csv", content)
    monkeypatch.setattr("sherpa.ingest.text_kind.MAX_BYTES", size - 1)
    assert G.grep_search("NEEDLE", world="v1", roots=[tmp_path]) == []
    monkeypatch.setattr("sherpa.ingest.text_kind.MAX_BYTES", size + 1)
    truncated: list = []
    assert len(G.grep_search("NEEDLE", world="v1", roots=[tmp_path], truncated_docs=truncated)) == 1
    assert truncated == []


def test_grep_search_size_exclusion_does_not_apply_to_registered_doc_extensions(monkeypatch, tmp_path):
    """`.txt` は text_kind の対象外＝cap 内なら MAX_BYTES を超えても検索される。"""
    monkeypatch.setattr("sherpa.ingest.text_kind.MAX_BYTES", 50)
    _write(tmp_path, "doc.txt", "x" * 100 + "NEEDLE\n")
    truncated: list = []
    assert len(G.grep_search("NEEDLE", world="v1", roots=[tmp_path], truncated_docs=truncated)) == 1
    assert truncated == []
