"""旧形式（.doc/.xls/.ppt）の先変換（`arms/legacy_prefetch.py`）の単体テスト。

偽 soffice（複数ファイルの一括変換に対応）で、先変換が本体ループの結果を変えないこと・二重変換しないこと・失敗/タイムアウト/出力欠落で
本体ループの 1 件変換へ戻ることを確かめる。実 soffice のテストは無ければスキップする。
"""
from __future__ import annotations

import io
import json
import stat
import tempfile
import zipfile
from pathlib import Path

import pytest

from sherpa.ingest import office_md
from sherpa.ingest.arms import legacy_convert, legacy_prefetch

_DOCX_XML = ('<?xml version="1.0"?>'
             '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
             '<w:body><w:p><w:r><w:t>旧資料の中身テキストXYZ</w:t></w:r></w:p></w:body></w:document>')

_FAKE_BATCH_SOFFICE = """#!/usr/bin/env bash
if [ "$1" = "--version" ]; then echo "LibreOffice 7.5.0.0 fake"; exit 0; fi
fmt=""; outdir=""; inputs=()
while [ $# -gt 0 ]; do
  case "$1" in
    --headless) shift;;
    -env:*) shift;;
    --convert-to) fmt="$2"; shift 2;;
    --outdir) outdir="$2"; shift 2;;
    *) inputs+=("$1"); shift;;
  esac
done
n=${#inputs[@]}
echo "$n" >> "$FAKE_LAUNCHES"
for f in "${inputs[@]}"; do cksum < "$f" | tr ' ' _ >> "$FAKE_SEEN"; done
if [ "$n" -gt 1 ] && [ -n "$FAKE_BATCH_SLEEP" ]; then sleep "$FAKE_BATCH_SLEEP"; fi
if [ "$n" -gt 1 ] && [ -n "$FAKE_BATCH_EXIT" ]; then exit "$FAKE_BATCH_EXIT"; fi
for f in "${inputs[@]}"; do
  LC_ALL=C grep -aq BAD "$f" && continue
  stem=$(basename "$f"); stem="${stem%.*}"
  cp "$FAKE_TEMPLATE" "$outdir/$stem.$fmt"
done
"""


def _install(tmp_path, monkeypatch, *, workers="2", batch="4", **env):
    script = tmp_path / "fake_soffice.sh"
    script.write_text(_FAKE_BATCH_SOFFICE, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    template = tmp_path / "template.docx"
    if not template.exists():                                          # 2 回目以降も同じバイト列（zip の時刻が変わると比較できない）
        with zipfile.ZipFile(template, "w") as z:
            z.writestr("word/document.xml", _DOCX_XML)
    launches, seen = tmp_path / "launches.txt", tmp_path / "seen.txt"
    monkeypatch.setenv("SHERPA_SOFFICE_BIN", str(script))
    monkeypatch.setenv("SHERPA_MCP_LEGACY_BACKEND", "libreoffice")
    monkeypatch.setenv("SHERPA_LEGACY_WORKERS", workers)
    monkeypatch.setenv("SHERPA_LEGACY_BATCH", batch)
    monkeypatch.setenv("FAKE_TEMPLATE", str(template))
    monkeypatch.setenv("FAKE_LAUNCHES", str(launches))
    monkeypatch.setenv("FAKE_SEEN", str(seen))
    monkeypatch.delenv("SHERPA_MCP_ARMS", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    legacy_convert._version_cache.clear()
    return launches, seen


def _src(tmp_path, names, bad=()):
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    for n in names:
        (src / n).write_bytes(b"\xd0\xcf\x11\xe0 " + (b"BAD" if n in bad else n.encode()))
    return src


def _lines(p: Path) -> list[str]:
    return p.read_text(encoding="utf-8").split() if p.exists() else []


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file() and "_legacy_cache" not in p.parts and "_conv_cache" not in p.parts}


def _build(src, derived):
    return office_md.build_derived(src, derived)


NAMES = [f"旧{i}.doc" for i in range(6)]


def test_batches_share_one_launch_and_no_file_is_converted_twice(tmp_path, monkeypatch):
    launches, seen = _install(tmp_path, monkeypatch)
    src = _src(tmp_path, NAMES)
    rep = _build(src, tmp_path / "d" / "w" / "md")
    assert rep["converted"] == 6 and rep["failed"] == 0 and rep["legacy_converted"] == 6
    assert sorted(_lines(launches)) == ["2", "4"]                    # 6 件が 4 件＋2 件の 2 回の起動で済む
    assert len(_lines(seen)) == len(set(_lines(seen))) == 6          # 同じ原本を 2 度変換していない


@pytest.mark.parametrize("env", [
    {"FAKE_BATCH_EXIT": "1"},
    {"FAKE_BATCH_SLEEP": "3", "SHERPA_LEGACY_TIMEOUT": "0.5"},
], ids=["batch_exit", "batch_timeout"])
def test_failed_batch_falls_back_to_single_conversion_with_same_result(tmp_path, monkeypatch, env):
    """バッチが失敗・タイムアウトしても、本体ループが 1 件ずつ変換し、先変換を通さない場合と同じ派生物・同じ結果になる。失敗は記録されない。"""
    src = _src(tmp_path, NAMES)
    _install(tmp_path, monkeypatch, workers="0")
    base_rep = _build(src, tmp_path / "base" / "w" / "md")
    _install(tmp_path, monkeypatch, workers="2", **env)
    rep = _build(src, tmp_path / "pre" / "w" / "md")
    assert rep["converted"] == base_rep["converted"] == 6 and rep["failed"] == 0
    assert rep["legacy_conversion_failures"] == [] and rep["legacy_converted"] == 6
    assert _tree(tmp_path / "pre" / "w" / "md") == _tree(tmp_path / "base" / "w" / "md")


def test_prefetched_result_is_identical_to_serial_conversion(tmp_path, monkeypatch):
    src = _src(tmp_path, NAMES)
    _install(tmp_path, monkeypatch, workers="0")
    _build(src, tmp_path / "base" / "w" / "md")
    _install(tmp_path, monkeypatch, workers="2")
    _build(src, tmp_path / "pre" / "w" / "md")
    base, pre = _tree(tmp_path / "base" / "w" / "md"), _tree(tmp_path / "pre" / "w" / "md")
    assert base and pre == base
    meta = json.loads((tmp_path / "pre" / "w" / "md" / "旧0.doc.md.meta.json").read_text(encoding="utf-8"))
    assert any(n.startswith("soffice=") for n in meta["notes"])


def test_missing_output_leaves_only_that_file_to_the_main_loop(tmp_path, monkeypatch):
    """バッチの出力が欠けた原本は先変換が放置し、本体ループが今まで通り 1 件変換して失敗を報告する。ほかの原本は先変換で済む。"""
    names = ["良1.doc", "壊れた.doc", "良2.doc"]
    src = _src(tmp_path, names, bad={"壊れた.doc"})
    launches, seen = _install(tmp_path, monkeypatch, workers="1")
    rep = _build(src, tmp_path / "d" / "w" / "md")
    assert rep["failed"] == 1 and rep["legacy_converted"] == 2
    assert rep["legacy_conversion_failures"] == [{"doc": "壊れた.doc", "reason": "legacy_conversion_failed"}]
    assert sorted(_lines(launches)) == ["1", "3"]                    # バッチ 1 回＋壊れた 1 件を本体ループが単独で
    assert len(_lines(seen)) == 4                                    # 壊れた 1 件だけが 2 度目（本体ループ）


def test_workers_zero_disables_prefetch(tmp_path, monkeypatch):
    launches, _ = _install(tmp_path, monkeypatch, workers="0")
    src = _src(tmp_path, NAMES)
    rep = _build(src, tmp_path / "d" / "w" / "md")
    assert rep["converted"] == 6
    assert _lines(launches) == ["1"] * 6


def test_cached_and_no_backend_files_are_not_prefetched(tmp_path, monkeypatch):
    launches, _ = _install(tmp_path, monkeypatch)
    src = _src(tmp_path, NAMES)
    derived = tmp_path / "d" / "w" / "md"
    _build(src, derived)
    launched = len(_lines(launches))
    _build(src, derived)                                              # 2 回目は変換済み＝先変換を始めない
    assert len(_lines(launches)) == launched
    monkeypatch.setenv("SHERPA_MCP_LEGACY_BACKEND", "none")
    assert legacy_prefetch.start([(src / NAMES[0], NAMES[0])], tmp_path / "cache") is None


def test_originals_and_temp_dirs_are_left_clean(tmp_path, monkeypatch):
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    _install(tmp_path, monkeypatch)
    src = _src(tmp_path, NAMES)
    before = {p.name: p.read_bytes() for p in src.iterdir()}
    _build(src, tmp_path / "d" / "w" / "md")
    assert {p.name: p.read_bytes() for p in src.iterdir()} == before   # 原本の中身・並びは不変（横にロックファイル等を作らない）
    assert list(tmp.iterdir()) == []                                  # 一時 dir・プロファイルは残らない


def test_settle_claims_unstarted_file_for_the_main_loop(tmp_path, monkeypatch):
    """未着手の原本は `settle` が先変換の対象から外す（以後その原本は先変換が触らない）。"""
    _install(tmp_path, monkeypatch, workers="1", batch="1", FAKE_BATCH_SLEEP="0")
    src = _src(tmp_path, NAMES[:3])
    items = [(src / n, n) for n in NAMES[:3]]
    pf = legacy_prefetch.LegacyPrefetcher(items, tmp_path / "cache", workers=0, batch=1)   # スレッドを起動しない
    pf.settle(NAMES[2])
    assert NAMES[2] not in pf._pending and pf._entries[NAMES[2]].state == "claimed"
    assert pf._take_batch()[0].rel == NAMES[0]
    pf.close()


# ---- 実 soffice（無ければスキップ）----

@pytest.mark.skipif(not legacy_convert.soffice_available(), reason="soffice が無い")
def test_real_soffice_xls_batch_keeps_saved_values_and_leaves_no_lock_file(tmp_path, monkeypatch):
    import openpyxl

    from test_legacy_convert import _stale_xls

    stale = _stale_xls(tmp_path, None)
    src = tmp_path / "world"
    src.mkdir()
    names = ["a.xls", "b.xls"]
    for n in names:
        (src / n).write_bytes(stale.read_bytes())
    monkeypatch.setenv("SHERPA_MCP_LEGACY_BACKEND", "libreoffice")
    monkeypatch.setenv("SHERPA_LEGACY_WORKERS", "1")
    monkeypatch.setenv("SHERPA_LEGACY_BATCH", "8")
    cache = tmp_path / "cache"
    with legacy_prefetch.start([(src / n, n) for n in names], cache) as pf:
        for n in names:
            pf.settle(n)
    assert pf.stored == 2
    for n in names:
        got = legacy_convert.ensure_ooxml(src / n, n, cache)             # 変換せずキャッシュに当たる
        assert got is not None
        sheet = openpyxl.load_workbook(io.BytesIO(got[0].read_bytes()), data_only=True)["s"]
        assert sheet["A2"].value == 99
    assert sorted(p.name for p in src.iterdir()) == names                # ロックファイルを作らない
