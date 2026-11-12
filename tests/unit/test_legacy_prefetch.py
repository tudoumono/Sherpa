"""旧形式（.doc/.xls/.ppt）の先変換（`arms/legacy_prefetch.py`）の単体テスト。

偽 soffice（複数ファイルの一括変換に対応）で、先変換が本体ループの結果を変えないこと・二重変換しないこと・失敗/タイムアウト/出力欠落で
本体ループの 1 件変換へ戻ること（時間切れを確定した原本だけは変換し直さず、時間切れの失敗として記録すること）を確かめる。実 soffice のテストは無ければスキップする。
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
from tests._replace_row import offline_row

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
if [ "$n" -eq 1 ] && [ -n "$FAKE_ONE_SLEEP" ]; then LC_ALL=C grep -aq SLOW "${inputs[0]}" && sleep "$FAKE_ONE_SLEEP"; fi
for f in "${inputs[@]}"; do
  LC_ALL=C grep -aq BAD "$f" && continue
  if [ "$n" -gt 1 ]; then
    LC_ALL=C grep -aq SLOW "$f" && sleep 30
    LC_ALL=C grep -aq MID "$f" && sleep "${FAKE_MID_SLEEP:-1.2}"
  fi
  stem=$(basename "$f"); stem="${stem%.*}"
  cp "$FAKE_TEMPLATE" "$outdir/$stem.$fmt"
done
"""


_FAKE_XLS_SOFFICE = r"""#!/usr/bin/env bash
if [ "$1" = "--version" ]; then echo "LibreOffice 7.5.0.0 fake"; exit 0; fi
mode=""; profile=""; macro=""
for a in "$@"; do
  case "$a" in
    --terminate_after_init) mode=init;;
    -env:UserInstallation=*) profile="${a#-env:UserInstallation=file://}";;
    macro:*) mode=macro; macro="$a";;
  esac
done
if [ "$mode" = init ]; then
  mkdir -p "$profile/user/basic/Standard"
  [ -n "$FAKE_INIT_SLEEP" ] && sleep "$FAKE_INIT_SLEEP"
  exit 0
fi
outdir=$(printf %s "$macro" | sed -E 's/.*","([^"]*)"\).*/\1/')
touch "$outdir/0.start"
sleep 30
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
    return office_md.build_derived(src, derived, row=offline_row("w"))


NAMES = [f"旧{i}.doc" for i in range(6)]


def test_batches_share_one_launch_and_no_file_is_converted_twice(tmp_path, monkeypatch):
    launches, seen = _install(tmp_path, monkeypatch)
    src = _src(tmp_path, NAMES)
    rep = _build(src, tmp_path / "d" / "w" / "md")
    assert rep["converted"] == 6 and rep["failed"] == 0 and rep["legacy_converted"] == 6
    assert sorted(_lines(launches)) == ["2", "4"]                    # 6 件が 4 件＋2 件の 2 回の起動で済む
    assert len(_lines(seen)) == len(set(_lines(seen))) == 6          # 同じ原本を 2 度変換していない


def test_failed_batch_falls_back_to_single_conversion_with_same_result(tmp_path, monkeypatch):
    """バッチが異常終了しても、本体ループが 1 件ずつ変換し、先変換を通さない場合と同じ派生物・同じ結果になる。失敗は記録されない。"""
    src = _src(tmp_path, NAMES)
    _install(tmp_path, monkeypatch, workers="0")
    base_rep = _build(src, tmp_path / "base" / "w" / "md")
    _install(tmp_path, monkeypatch, workers="2", FAKE_BATCH_EXIT="1")
    rep = _build(src, tmp_path / "pre" / "w" / "md")
    assert rep["converted"] == base_rep["converted"] == 6 and rep["failed"] == 0
    assert rep["legacy_conversion_failures"] == [] and rep["legacy_converted"] == 6
    assert _tree(tmp_path / "pre" / "w" / "md") == _tree(tmp_path / "base" / "w" / "md")


def test_single_file_batch_that_hit_the_limit_is_not_converted_again_and_is_recorded_as_timeout(tmp_path, monkeypatch):
    """--convert-to の経路でまとめた件数が 1 件のときは、時間切れの原本が確定する＝本体ループで変換し直さず、時間切れの失敗として記録する。
    ほかの原本は今まで通り変換される。"""
    names = ["良1.doc", "遅い.doc", "良2.doc"]
    src = tmp_path / "src"
    src.mkdir()
    for n in names:
        (src / n).write_bytes(b"\xd0\xcf\x11\xe0 " + (b"SLOW" if n == "遅い.doc" else n.encode()))
    launches, _ = _install(tmp_path, monkeypatch, workers="1", batch="1", SHERPA_LEGACY_TIMEOUT="1", FAKE_ONE_SLEEP="30")
    rep = _build(src, tmp_path / "d" / "w" / "md")
    assert rep["failed"] == 1 and rep["legacy_converted"] == 2
    assert rep["legacy_conversion_failures"] == [{"doc": "遅い.doc", "reason": "legacy_conversion_timeout"}]
    assert sorted(_lines(launches)) == ["1", "1", "1"]               # 遅い は先変換の 1 回だけ（本体ループは変換し直さない）


def test_multi_file_convert_to_batch_stopped_by_limit_is_not_confirmed_and_goes_to_the_main_loop(tmp_path, monkeypatch):
    """--convert-to の経路で 2 件以上をまとめたバッチが時間切れになっても、資料ごとの開始が分からないので確定しない。全件を本体ループが変換する。"""
    src = _src(tmp_path, NAMES[:3])
    launches, _ = _install(tmp_path, monkeypatch, workers="1", batch="3",
                           FAKE_BATCH_SLEEP="5", SHERPA_LEGACY_TIMEOUT="0.5")
    rep = _build(src, tmp_path / "d" / "w" / "md")
    assert rep["failed"] == 0 and rep["legacy_converted"] == 3
    assert rep["legacy_conversion_failures"] == []
    assert sorted(_lines(launches)) == ["1", "1", "1", "3"]


def _xls_prefetcher(tmp_path, monkeypatch, **env):
    script = tmp_path / "fake_xls_soffice.sh"
    script.write_text(_FAKE_XLS_SOFFICE, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("SHERPA_SOFFICE_BIN", str(script))
    monkeypatch.setenv("SHERPA_MCP_LEGACY_BACKEND", "libreoffice")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    legacy_convert._version_cache.clear()
    src = _src(tmp_path, ["a.xls", "b.xls"])
    items = [(src / n, n) for n in ("a.xls", "b.xls")]
    return legacy_prefetch.LegacyPrefetcher(items, tmp_path / "cache", workers=1, batch=2)


def test_xls_batch_stopped_during_profile_init_confirms_no_file(tmp_path, monkeypatch):
    """プロファイルの初期化で上限を使い切って止まったとき、まだ変換を始めていない先頭の .xls を時間切れにしない。"""
    with _xls_prefetcher(tmp_path, monkeypatch, SHERPA_LEGACY_TIMEOUT="1", FAKE_INIT_SLEEP="30") as pf:
        pf.settle("a.xls")
        pf.settle("b.xls")
        assert not pf.timed_out("a.xls") and not pf.timed_out("b.xls")


def test_xls_batch_confirms_only_the_file_that_started_and_hit_the_limit(tmp_path, monkeypatch):
    """.xls は開始の印がある資料が上限まで使って止まったときだけ時間切れと確定する。印のない後続は確定しない。"""
    with _xls_prefetcher(tmp_path, monkeypatch, SHERPA_LEGACY_TIMEOUT="1") as pf:
        pf.settle("a.xls")
        pf.settle("b.xls")
        assert pf.timed_out("a.xls") and not pf.timed_out("b.xls")


def _marked(tmp_path, monkeypatch, *, starts: dict[int, float], oks=()):
    """.xls 3 件のバッチで、指定の開始の印（番号 → 開始からの経過秒）と完了の印を置いた出力 dir を返す。"""
    import os
    import time

    monkeypatch.setenv("SHERPA_LEGACY_TIMEOUT", "10")
    src = _src(tmp_path, ["a.xls", "b.xls", "c.xls"])
    pf = legacy_prefetch.LegacyPrefetcher([(src / n, n) for n in ("a.xls", "b.xls", "c.xls")], tmp_path / "cache", workers=0, batch=3)
    batch = pf._take_batch()
    out = tmp_path / "out"
    out.mkdir()
    for i, age in starts.items():
        (out / f"{i}.start").touch()
        os.utime(out / f"{i}.start", (time.time() - age, time.time() - age))
    for i in oks:
        (out / f"{i}.ok").touch()
    pf._mark_timed_out(batch, [0, 1, 2], str(out), ".xls", ".xlsx")
    return pf


def test_xls_without_start_marker_is_never_confirmed(tmp_path, monkeypatch):
    pf = _marked(tmp_path, monkeypatch, starts={})
    assert pf._timed_out == set()


def test_xls_finished_file_is_not_confirmed_even_if_old(tmp_path, monkeypatch):
    """済んだ資料・失敗して先へ進んだ資料は、開始から上限以上たっていても確定しない。最後に始めて未完了の資料だけが対象。"""
    pf = _marked(tmp_path, monkeypatch, starts={0: 30, 1: 20}, oks=[0])
    assert pf._timed_out == {"b.xls"}


def test_xls_failed_file_followed_by_a_later_start_is_not_confirmed(tmp_path, monkeypatch):
    pf = _marked(tmp_path, monkeypatch, starts={0: 30, 1: 20, 2: 15}, oks=[0])
    assert pf._timed_out == {"c.xls"}


def test_xls_started_file_under_the_limit_is_not_confirmed(tmp_path, monkeypatch):
    pf = _marked(tmp_path, monkeypatch, starts={0: 3}, oks=[])
    assert pf._timed_out == set()


def test_file_stopped_before_its_own_limit_is_left_to_the_main_loop(tmp_path, monkeypatch):
    """バッチの終わりで止まった原本が、まだ 1 件分の上限に届いていなければ時間切れと確定せず、本体ループが変換する。"""
    names = ["中1.doc", "遅い.doc"]
    src = tmp_path / "src"
    src.mkdir()
    for n in names:
        (src / n).write_bytes(b"\xd0\xcf\x11\xe0 " + (b"MID" if n == "中1.doc" else b"SLOW"))
    _install(tmp_path, monkeypatch, workers="1", batch="2", SHERPA_LEGACY_TIMEOUT="2", FAKE_MID_SLEEP="2.8")
    rep = _build(src, tmp_path / "d" / "w" / "md")
    assert rep["failed"] == 0 and rep["legacy_converted"] == 2
    assert rep["legacy_conversion_failures"] == []


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


def test_same_content_files_are_converted_once_and_match_serial_result(tmp_path, monkeypatch):
    """空のキャッシュで同じバイトの 2 ファイルを取り込むと、LibreOffice へ渡すのは先頭の 1 件だけ。結果はキャッシュなし・先変換なしと同じ。"""
    src = tmp_path / "src"
    src.mkdir()
    for n in ("a.doc", "b.doc"):
        (src / n).write_bytes(b"\xd0\xcf\x11\xe0 same-bytes")
    _install(tmp_path, monkeypatch, workers="0")
    base_rep = _build(src, tmp_path / "base" / "w" / "md")
    launches, seen = _install(tmp_path, monkeypatch, workers="2")
    launches.unlink(missing_ok=True)
    seen.unlink(missing_ok=True)
    rep = _build(src, tmp_path / "pre" / "w" / "md")
    assert rep["failed"] == 0 and rep["converted"] == base_rep["converted"] == 2
    assert len(_lines(seen)) == 1                                    # 前段変換に渡った原本は 1 件
    assert _tree(tmp_path / "pre" / "w" / "md") == _tree(tmp_path / "base" / "w" / "md")


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
