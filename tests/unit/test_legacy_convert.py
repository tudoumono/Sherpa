"""旧形式（.doc/.xls/.ppt）変換バックエンド（W0・2026-07-08-旧Office変換2系統.md）の単体テスト（DB不要）。

- バックエンド解決の優先順（system_settings > env > 既定）・fail-safe・未知値→none。
- soffice 検出（SHERPA_SOFFICE_BIN の絶対パス化＋実行可検査・未検出→None・非実行→None）。
- legacy_exts / legacy_sig_value（none・libreoffice+soffice 有無）。
- 偽 soffice スクリプトでの変換成功／タイムアウト／非0終了 → None。
- RV High（2026-07-08）: タイムアウト時にプロセスグループ（wrapper→soffice.bin 相当の孫プロセス）ごと
  停止すること（残骸プロセス化の防止）。
- RV Med（2026-07-08）: `-env:UserInstallation` の file:// URL が `Path.as_uri()` で percent-encode される
  （コマンド組み立てのみを検証・実行しない）。
- キャッシュヒット（原本 mtime/size 一致で再変換しない）／キャッシュミス（原本変更で再変換）。
- arms_sig が legacy_backend に反応（office_md 経由）。
- build_derived 統合（旧形式 → OOXML 前段変換 → ①MD化 → provenance に来歴）。

tests/unit/conftest.py の autouse fixture が `store.get_system_settings` を空 dict に固定する（DB 非依存）。
system 優先を検証するテストは各テスト本体で明示的に上書きする（conftest より後に効く）。
"""
from __future__ import annotations

import email
import hashlib
import http.server
import json
import os
import pathlib
import socket
import stat
import threading
import time
import zipfile

from sherpa.ingest import office_md
from sherpa.ingest.arms import legacy_convert

# 偽 soffice が出力する docx の中身（office_md が XML 直パースで拾える最小構成）。
_DOCX_XML = ('<?xml version="1.0"?>'
             '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
             '<w:body><w:p><w:r><w:t>旧資料の中身テキストXYZ</w:t></w:r></w:p></w:body></w:document>')

_FAKE_SOFFICE = """#!/usr/bin/env bash
# 偽 soffice: --version はバージョンを出力し、--convert-to は template docx を --outdir へコピーする。
if [ "$1" = "--version" ]; then echo "LibreOffice 7.5.0.0 fake"; exit 0; fi
[ -n "$FAKE_SOFFICE_COUNTER" ] && echo x >> "$FAKE_SOFFICE_COUNTER"
[ -n "$FAKE_SOFFICE_SLEEP" ] && sleep "$FAKE_SOFFICE_SLEEP"
[ -n "$FAKE_SOFFICE_EXIT" ] && [ "$FAKE_SOFFICE_EXIT" != "0" ] && exit "$FAKE_SOFFICE_EXIT"
fmt=""; outdir=""; input=""
while [ $# -gt 0 ]; do
  case "$1" in
    --headless) shift;;
    -env:*) shift;;
    --convert-to) fmt="$2"; shift 2;;
    --outdir) outdir="$2"; shift 2;;
    *) input="$1"; shift;;
  esac
done
stem=$(basename "$input"); stem="${stem%.*}"
cp "$FAKE_SOFFICE_DOCX" "$outdir/$stem.$fmt"
"""


def _make_template(dirpath: pathlib.Path) -> pathlib.Path:
    tmpl = dirpath / "template.docx"
    with zipfile.ZipFile(tmpl, "w") as z:
        z.writestr("word/document.xml", _DOCX_XML)
    return tmpl


def _install_fake_soffice(tmp_path: pathlib.Path, monkeypatch, *, sleep=None, exit_code=None):
    """偽 soffice を用意して SHERPA_SOFFICE_BIN に設定し、変換結果テンプレ/カウンタ path を返す。"""
    script = tmp_path / "fake_soffice.sh"
    script.write_text(_FAKE_SOFFICE, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    tmpl = _make_template(tmp_path)
    counter = tmp_path / "counter.txt"
    monkeypatch.setenv("SHERPA_SOFFICE_BIN", str(script))
    monkeypatch.setenv("SHERPA_LEGACY_BACKEND", "libreoffice")
    monkeypatch.setenv("FAKE_SOFFICE_DOCX", str(tmpl))
    monkeypatch.setenv("FAKE_SOFFICE_COUNTER", str(counter))
    if sleep is not None:
        monkeypatch.setenv("FAKE_SOFFICE_SLEEP", str(sleep))
    if exit_code is not None:
        monkeypatch.setenv("FAKE_SOFFICE_EXIT", str(exit_code))
    legacy_convert._version_cache.clear()   # bin path 毎キャッシュを掃除（テスト間の混線防止）
    return counter


def _count(counter: pathlib.Path) -> int:
    return counter.read_text(encoding="utf-8").count("x") if counter.exists() else 0


# ---- バックエンド解決（system_settings > env > 既定）----

def test_backend_default_none(monkeypatch):
    monkeypatch.delenv("SHERPA_LEGACY_BACKEND", raising=False)
    assert legacy_convert.legacy_backend_name() == "none"
    assert legacy_convert.env_default_backend() == "none"


def test_backend_env_over_default(monkeypatch):
    monkeypatch.setenv("SHERPA_LEGACY_BACKEND", "libreoffice")
    assert legacy_convert.legacy_backend_name() == "libreoffice"
    assert legacy_convert.env_default_backend() == "libreoffice"


def test_backend_system_over_env(monkeypatch):
    from sherpa import store
    monkeypatch.setenv("SHERPA_LEGACY_BACKEND", "none")
    monkeypatch.setattr(store, "get_system_settings", lambda: {"legacy_backend": "libreoffice"})
    assert legacy_convert.legacy_backend_name() == "libreoffice"      # 全体設定が env に優先
    assert legacy_convert.env_default_backend() == "none"             # env_default は system を見ない


def test_backend_unknown_value_failsafe_to_none(monkeypatch):
    monkeypatch.setattr(legacy_convert, "_warned_unknown_backend", set())
    monkeypatch.setenv("SHERPA_LEGACY_BACKEND", "bogus_backend")      # 未知値＝none へ倒す（fail-safe）
    assert legacy_convert.legacy_backend_name() == "none"


def test_backend_system_unreadable_failsafe_to_env(monkeypatch):
    from sherpa import store

    def _boom():
        raise RuntimeError("no PG creds (MCP subprocess)")

    monkeypatch.setattr(store, "get_system_settings", _boom)
    monkeypatch.setenv("SHERPA_LEGACY_BACKEND", "libreoffice")
    assert legacy_convert.legacy_backend_name() == "libreoffice"      # 例外は握って env へ倒す


# ---- soffice 検出 ----

def test_soffice_detect_via_env(tmp_path, monkeypatch):
    _install_fake_soffice(tmp_path, monkeypatch)
    assert legacy_convert.soffice_available() is True
    assert legacy_convert.soffice_version() == "LibreOffice 7.5.0.0 fake"


def test_soffice_missing_returns_none(monkeypatch):
    monkeypatch.setenv("SHERPA_SOFFICE_BIN", "/no/such/soffice/binary")
    assert legacy_convert.soffice_available() is False
    assert legacy_convert.soffice_version() is None


def test_soffice_non_executable_rejected(tmp_path, monkeypatch):
    not_exec = tmp_path / "soffice.txt"
    not_exec.write_text("not executable", encoding="utf-8")   # X_OK 無し
    monkeypatch.setenv("SHERPA_SOFFICE_BIN", str(not_exec))
    assert legacy_convert.soffice_available() is False


# ---- legacy_exts / legacy_sig_value ----

def test_legacy_exts_none_backend_empty(monkeypatch):
    monkeypatch.delenv("SHERPA_LEGACY_BACKEND", raising=False)
    assert legacy_convert.legacy_exts() == set()
    assert legacy_convert.legacy_sig_value() == "none"


def test_legacy_exts_libreoffice_without_soffice_empty(monkeypatch):
    monkeypatch.setenv("SHERPA_LEGACY_BACKEND", "libreoffice")
    monkeypatch.setenv("SHERPA_SOFFICE_BIN", "/no/such/soffice")   # soffice 未検出
    assert legacy_convert.legacy_exts() == set()                  # backend 選択でも変換不可＝空
    assert legacy_convert.legacy_sig_value() == "none"            # 署名も none（現状どおり）


def test_legacy_exts_libreoffice_with_soffice(tmp_path, monkeypatch):
    _install_fake_soffice(tmp_path, monkeypatch)
    assert legacy_convert.legacy_exts() == {".doc", ".xls", ".ppt"}
    assert legacy_convert.legacy_sig_value() == "libreoffice"


# ---- 変換本体（偽 soffice）----

def test_convert_success(tmp_path, monkeypatch):
    _install_fake_soffice(tmp_path, monkeypatch)
    src = tmp_path / "旧資料.doc"
    src.write_bytes(b"\xd0\xcf\x11\xe0 old binary")
    data = legacy_convert.convert_to_ooxml(src, ".docx")
    assert data is not None and data[:2] == b"PK"                 # zip（OOXML）先頭
    # 変換されたバイトが①OOXML アームで MD 化できること（値の権威に委譲）。
    out = tmp_path / "out.docx"
    out.write_bytes(data)
    md = office_md.to_markdown(out)
    assert md is not None and "旧資料の中身テキストXYZ" in md


def test_convert_timeout_returns_none(tmp_path, monkeypatch):
    counter = _install_fake_soffice(tmp_path, monkeypatch, sleep=2)
    monkeypatch.setenv("SHERPA_LEGACY_TIMEOUT", "0.3")            # sleep(2) > timeout(0.3) → TimeoutExpired
    src = tmp_path / "旧資料.doc"
    src.write_bytes(b"x")
    assert legacy_convert.convert_to_ooxml(src, ".docx") is None
    assert _count(counter) == 1                                  # 起動はした（＝タイムアウトで None）


def test_convert_timeout_sets_conversion_failure_reason(tmp_path, monkeypatch):
    """ING-1: subprocess タイムアウトは汎用の失敗と区別できる理由コード（`"timeout"`）を残す
    （`office_md` 側が `legacy_conversion_failed` でなく `legacy_conversion_timeout` を計上するため）。
    `convert_to_ooxml()` の戻り値契約自体は変えない（引き続き `None`）。"""
    _install_fake_soffice(tmp_path, monkeypatch, sleep=2)
    monkeypatch.setenv("SHERPA_LEGACY_TIMEOUT", "0.3")
    src = tmp_path / "旧資料.doc"
    src.write_bytes(b"x")
    assert legacy_convert.convert_to_ooxml(src, ".docx") is None
    assert legacy_convert.take_conversion_failure_reason() == "timeout"
    assert legacy_convert.take_conversion_failure_reason() is None   # 読んだら消費（次回呼び出しへ残留しない）


def test_convert_nonzero_exit_does_not_set_timeout_reason(tmp_path, monkeypatch):
    """タイムアウト以外の失敗（非0終了）は理由コードを残さない（`None`＝呼び出し元は汎用失敗へ倒す）。"""
    _install_fake_soffice(tmp_path, monkeypatch, exit_code=1)
    src = tmp_path / "旧資料.doc"
    src.write_bytes(b"x")
    assert legacy_convert.convert_to_ooxml(src, ".docx") is None
    assert legacy_convert.take_conversion_failure_reason() is None


def test_ensure_ooxml_clears_stale_conversion_failure_reason_on_cache_hit(tmp_path, monkeypatch):
    """`ensure_ooxml()` はキャッシュ命中（変換自体を行わない）でも、前回呼び出し（別ファイルの
    タイムアウト）の残留理由を引きずらない——冒頭で必ずクリアしてから判定するため。"""
    _install_fake_soffice(tmp_path, monkeypatch)
    cache_root = tmp_path / "_legacy_cache"

    ok_src = tmp_path / "ok.doc"
    ok_src.write_bytes(b"y")
    assert legacy_convert.ensure_ooxml(ok_src, "ok.doc", cache_root) is not None   # 成功→キャッシュ書込

    # 別ファイルの変換をタイムアウトさせる（理由をセットするが、あえてまだ消費しない）。
    monkeypatch.setenv("FAKE_SOFFICE_SLEEP", "2")
    monkeypatch.setenv("SHERPA_LEGACY_TIMEOUT", "0.3")
    timeout_src = tmp_path / "timeout.doc"
    timeout_src.write_bytes(b"x")
    assert legacy_convert.ensure_ooxml(timeout_src, "timeout.doc", cache_root) is None

    # ok.doc を再度呼ぶ＝原本 mtime/size 不変なのでキャッシュ命中（convert_to_ooxml は呼ばれない）。
    assert legacy_convert.ensure_ooxml(ok_src, "ok.doc", cache_root) is not None
    assert legacy_convert.take_conversion_failure_reason() is None   # timeout の残留が誤って出ない


def test_convert_nonzero_exit_returns_none(tmp_path, monkeypatch):
    _install_fake_soffice(tmp_path, monkeypatch, exit_code=1)
    src = tmp_path / "旧資料.doc"
    src.write_bytes(b"x")
    assert legacy_convert.convert_to_ooxml(src, ".docx") is None


def test_convert_none_backend_returns_none(tmp_path, monkeypatch):
    _install_fake_soffice(tmp_path, monkeypatch)
    monkeypatch.setenv("SHERPA_LEGACY_BACKEND", "none")           # backend none＝変換しない
    src = tmp_path / "旧資料.doc"
    src.write_bytes(b"x")
    assert legacy_convert.convert_to_ooxml(src, ".docx") is None


def test_convert_timeout_kills_process_group_including_descendants(tmp_path, monkeypatch):
    """RV High（2026-07-08）: soffice は wrapper→soffice.bin の多段起動のため、素朴な
    `subprocess.run(timeout=)` は直接の子しか kill せず孫プロセスが残骸化する。
    `start_new_session=True` ＋タイムアウト時 `os.killpg(...,SIGKILL)` でプロセスグループ全体を確実に
    停止することを、孫プロセス（`sleep 30 &`・wrapper と同一グループ）が実際に消えることで検証する。"""
    script = tmp_path / "fake_soffice_multi.sh"
    child_pid_file = tmp_path / "child_pid.txt"
    script.write_text(
        '#!/usr/bin/env bash\n'
        'if [ "$1" = "--version" ]; then echo "LibreOffice 7.5.0.0 fake"; exit 0; fi\n'
        # job control 無し（非対話スクリプト）＝バックグラウンドジョブは wrapper と同一プロセスグループ。
        'sleep 30 &\n'
        'echo $! > "$FAKE_SOFFICE_CHILD_PID_FILE"\n'
        'sleep 30\n',
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("SHERPA_SOFFICE_BIN", str(script))
    monkeypatch.setenv("SHERPA_LEGACY_BACKEND", "libreoffice")
    monkeypatch.setenv("FAKE_SOFFICE_CHILD_PID_FILE", str(child_pid_file))
    monkeypatch.setenv("SHERPA_LEGACY_TIMEOUT", "0.3")             # wrapper は30秒スリープ＝確実にタイムアウト
    legacy_convert._version_cache.clear()

    src = tmp_path / "旧資料.doc"
    src.write_bytes(b"x")
    assert legacy_convert.convert_to_ooxml(src, ".docx") is None  # タイムアウト＝None（fail-safe）

    # 孫プロセスの pid を取得（wrapper が書き出す・タイムアウト成立前に書かれているはず）。
    deadline = time.monotonic() + 3.0
    child_pid = None
    while time.monotonic() < deadline and child_pid is None:
        if child_pid_file.exists():
            try:
                child_pid = int(child_pid_file.read_text(encoding="utf-8").strip())
            except ValueError:
                pass
        if child_pid is None:
            time.sleep(0.05)
    assert child_pid is not None, "孫プロセスの pid が取得できなかった（テスト前提が崩れている）"

    # 孫プロセスが実際に消えたか（再親化後の reap 待ちを許容し最大3秒ポーリング）。
    gone = False
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            gone = True
            break
        time.sleep(0.05)
    assert gone, f"孫プロセス（pid={child_pid}）がタイムアウト後も残っている（プロセスグループ kill が効いていない）"


# ---- コマンド組み立て（UserInstallation の file:// URL・percent-encode）----

def test_build_convert_cmd_percent_encodes_file_uri(tmp_path):
    """RV Med（2026-07-08）: soffice は -env:UserInstallation を URL としてパースするため、パスに
    スペース等を含む場合は正規に percent-encode（`Path.as_uri()`）しないと誤解釈されうる。
    実行はしない（コマンド組み立てのみを検証）。"""
    profile = tmp_path / "profile with space"
    profile.mkdir()
    outdir = tmp_path / "out"
    outdir.mkdir()
    src = tmp_path / "旧資料.doc"
    src.write_bytes(b"x")

    cmd = legacy_convert._build_convert_cmd("/usr/bin/soffice", "docx", outdir, profile, src)

    env_arg = next(a for a in cmd if a.startswith("-env:UserInstallation="))
    uri = env_arg.split("=", 1)[1]
    assert uri.startswith("file:///")
    assert "%20" in uri                       # スペースが percent-encode されている
    assert " " not in uri                     # 生スペースがコマンド引数に残っていない
    assert uri == profile.as_uri()            # Path.as_uri() の正規出力と一致


# ---- キャッシュ（原本 mtime/size キー）----

def test_ensure_ooxml_cache_hit_and_miss(tmp_path, monkeypatch):
    counter = _install_fake_soffice(tmp_path, monkeypatch)
    src = tmp_path / "旧資料.doc"
    src.write_bytes(b"\xd0\xcf\x11\xe0 v1")
    cache_root = tmp_path / "_legacy_cache"

    first = legacy_convert.ensure_ooxml(src, "旧資料.doc", cache_root)
    assert first is not None
    ooxml_path, notes = first
    assert ooxml_path.is_file() and ooxml_path.suffix == ".docx"
    assert "legacy_backend=libreoffice" in notes
    assert any(n.startswith("soffice=") for n in notes)
    assert _count(counter) == 1

    # 原本 unchanged → キャッシュヒット（soffice を再実行しない）。
    second = legacy_convert.ensure_ooxml(src, "旧資料.doc", cache_root)
    assert second is not None
    assert _count(counter) == 1

    # 原本変更（size/mtime 変化）→ キャッシュミス（再変換）。
    src.write_bytes(b"\xd0\xcf\x11\xe0 v2 CHANGED longer content")
    third = legacy_convert.ensure_ooxml(src, "旧資料.doc", cache_root)
    assert third is not None
    assert _count(counter) == 2


def test_ensure_ooxml_unsupported_ext_returns_none(tmp_path, monkeypatch):
    _install_fake_soffice(tmp_path, monkeypatch)
    src = tmp_path / "note.txt"
    src.write_text("x", encoding="utf-8")
    assert legacy_convert.ensure_ooxml(src, "note.txt", tmp_path / "_legacy_cache") is None


# ---- arms_sig が legacy_backend に反応（office_md 経由）----

def test_arms_sig_drift_reacts_to_legacy_backend(tmp_path, monkeypatch):
    monkeypatch.delenv("SHERPA_ARMS", raising=False)
    d = tmp_path / "derived"
    d.mkdir()
    o_b = office_md._pdf_backend
    try:
        office_md._pdf_backend = lambda: None
        monkeypatch.delenv("SHERPA_LEGACY_BACKEND", raising=False)   # legacy=none で署名を書く
        office_md._write_arms_sig_marker(d)
        assert office_md.arms_sig_drift(d) is False                  # 同一（legacy=none）
        # backend を libreoffice にし soffice を検出させる → legacy 署名が変わる → drift。
        _install_fake_soffice(tmp_path, monkeypatch)
        assert office_md.arms_sig_drift(d) is True
    finally:
        office_md._pdf_backend = o_b


# ---- build_derived 統合（旧形式 → OOXML → ①MD化 → provenance）----

def test_build_derived_converts_legacy_via_backend(tmp_path, monkeypatch):
    monkeypatch.delenv("SHERPA_ARMS", raising=False)                 # ooxml 有効（既定）
    counter = _install_fake_soffice(tmp_path, monkeypatch)
    src = tmp_path / "src"
    src.mkdir()
    (src / "旧資料.doc").write_bytes(b"\xd0\xcf\x11\xe0 old binary")
    derived = tmp_path / "derived" / "test" / "md"

    rep = office_md.build_derived(src, derived)
    # 旧 .doc はB1によりdocument-ir-v2を生成しない。一方Canonical Evidence/RAGはoriginalと
    # normalized provenanceを分離できるため生成する。
    assert rep["converted"] == 1 and rep["failed"] == 0 and rep["unsupported"] == 0
    assert rep["by_ext"] == {".doc": 1}
    assert rep["document_ir_generated"] == 0
    assert rep["evidence_ir_generated"] == 1 and rep["rag_generated"] == 1
    assert rep["office_display_requested"] == 0

    md = derived / "旧資料.doc.md"                                    # 出力名は原本 rel（台帳/grep が一致）
    assert md.is_file() and "旧資料の中身テキストXYZ" in md.read_text(encoding="utf-8")

    meta = json.loads((derived / "旧資料.doc.md.meta.json").read_text(encoding="utf-8"))
    assert meta["arm"] == "ooxml" and meta["method"] == "ooxml"      # MD の権威は①OOXML
    assert "legacy_backend=libreoffice" in meta["notes"]
    assert any(n.startswith("soffice=") for n in meta["notes"])

    # キャッシュは md/ の兄弟（derived/{world}/_legacy_cache）に置かれ、原本は一切変更しない。
    cache = derived.parent / "_legacy_cache" / "旧資料.doc.docx"
    assert cache.is_file()
    assert (src / "旧資料.doc").read_bytes() == b"\xd0\xcf\x11\xe0 old binary"
    assert _count(counter) == 1

    # 再ビルド（md/ を全消去して作り直す）でも _legacy_cache は残りキャッシュヒット（再変換しない）。
    rep2 = office_md.build_derived(src, derived)
    assert rep2["converted"] == 1
    assert _count(counter) == 1


def test_build_derived_legacy_conversion_timeout_reason(tmp_path, monkeypatch):
    """ING-1: backend が実在する（`legacy_exts()` 非空）のにタイムアウトで失敗した場合は `failed` に
    計上し、`legacy_conversion_failures` へ汎用の `legacy_conversion_failed` でなく
    `legacy_conversion_timeout` を残す（複数ファイルとも同じ理由で計上される）。"""
    src = tmp_path / "src"
    src.mkdir()
    (src / "遅い.doc").write_bytes(b"\xd0\xcf\x11\xe0 old binary a")
    (src / "遅い2.doc").write_bytes(b"\xd0\xcf\x11\xe0 old binary b")
    derived = tmp_path / "derived" / "test" / "md"

    _install_fake_soffice(tmp_path, monkeypatch, sleep=2)
    monkeypatch.setenv("SHERPA_LEGACY_TIMEOUT", "0.3")
    rep = office_md.build_derived(src, derived)
    assert rep["unsupported"] == 0                       # backend は在る＝未対応ではなく失敗
    assert rep["failed"] == 2
    reasons = {e["doc"]: e["reason"] for e in rep["legacy_conversion_failures"]}
    assert reasons == {"遅い.doc": "legacy_conversion_timeout", "遅い2.doc": "legacy_conversion_timeout"}


def test_build_derived_legacy_conversion_failed_reason_for_nonzero_exit(tmp_path, monkeypatch):
    """タイムアウト以外（非0終了等）の変換失敗は `legacy_conversion_failed`（汎用）のまま計上する。"""
    src = tmp_path / "src"
    src.mkdir()
    (src / "壊れた.doc").write_bytes(b"\xd0\xcf\x11\xe0 old binary")
    derived = tmp_path / "derived" / "test" / "md"

    _install_fake_soffice(tmp_path, monkeypatch, exit_code=1)
    rep = office_md.build_derived(src, derived)
    assert rep["unsupported"] == 0
    assert rep["failed"] == 1
    assert rep["legacy_conversion_failures"] == [{"doc": "壊れた.doc", "reason": "legacy_conversion_failed"}]


def test_build_derived_legacy_unsupported_when_backend_none(tmp_path, monkeypatch):
    monkeypatch.delenv("SHERPA_ARMS", raising=False)
    monkeypatch.delenv("SHERPA_LEGACY_BACKEND", raising=False)       # backend none（既定）
    monkeypatch.delenv("SHERPA_SOFFICE_BIN", raising=False)
    src = tmp_path / "src"
    src.mkdir()
    (src / "旧資料.doc").write_bytes(b"\xd0\xcf\x11\xe0 old binary")
    derived = tmp_path / "derived" / "test" / "md"

    rep = office_md.build_derived(src, derived)
    assert rep["unsupported"] == 1 and rep["converted"] == 0
    # 内容は解析しないが、source-level Evidence/coverage noticeは検索可能な成果物として公開する。
    # §8.1 三階層＝.evidence.json は ir 層、.rag.md は rag 層（derived＝md 層の兄弟）。
    assert (derived / "旧資料.doc.md").is_file()
    evidence = json.loads(
        (derived.parent / "ir" / "旧資料.doc.evidence.json").read_text(encoding="utf-8"))
    assert evidence["coverage"][0]["reason_code"] == "legacy_backend_unavailable"
    assert "legacy_backend_unavailable" in (
        derived.parent / "rag" / "旧資料.doc.rag.md").read_text(encoding="utf-8")


# ==== office_com バックエンド（W1・Windows 側ワーカーへの HTTP・ローカル http.server モック）====
#
# 実 Office/COM は起動できない（この環境からは不可・共同検証で行う）ため、ここでは 127.0.0.1 の
# 空きポートに立てたモックワーカー（/healthz・/convert）で HTTP 経路（到達性ゲート・変換・エラー/
# タイムアウト時の fail-safe・キャッシュ）を検証する。パス変換は純関数として単体検証する。

def _docx_bytes() -> bytes:
    """モックワーカーが /convert で返す固定 docx バイト列（office_md が XML 直パースで拾える最小構成）。"""
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", _DOCX_XML)
    return buf.getvalue()


class _MockWorkerHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):    # テスト出力を汚さない
        pass

    def _token_ok(self) -> bool:
        want = self.server.token
        if want and self.headers.get("X-Sherpa-Token") != want:
            self._send(401, b'{"error":"bad token"}', "application/json")
            return False
        return True

    def _send(self, status, body: bytes, ctype: str):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._token_ok():
            return
        if self.path == "/healthz":
            payload = {"ok": True, "versions": self.server.versions, "worker": "1"}
            self._send(200, json.dumps(payload).encode("utf-8"), "application/json")
            return
        self._send(404, b'{"error":"not found"}', "application/json")

    def do_POST(self):
        if not self._token_ok():
            return
        self.server.last_path = self.path                # /convert・/render・/convert-upload・/render-upload
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        ctype = self.headers.get("Content-Type", "") or ""
        if ctype.startswith("multipart/form-data"):
            self.server.last_upload = _parse_multipart(raw, ctype)
            self.server.last_body = None
        else:
            try:
                self.server.last_body = json.loads(raw) if raw else {}
            except ValueError:
                self.server.last_body = None
            self.server.last_upload = None
        self.server.convert_calls += 1
        if self.server.delay:
            time.sleep(self.server.delay)
        is_upload_route = self.path in ("/convert-upload", "/render-upload")
        # OFFICE-WIN-001: upload 系だけ N 回連続失敗させる（リトライ検証用・path 系には影響しない）。
        if is_upload_route and self.server.upload_fail_first_n > 0:
            self.server.upload_fail_first_n -= 1
            self._send(500, b'{"error":"boom"}', "application/json")
            return
        if is_upload_route and self.server.upload_status is not None:
            status, body = self.server.upload_status, self.server.upload_body
        else:
            status, body = self.server.convert_status, self.server.convert_body
        if status == 200:
            self._send(200, body, "application/octet-stream")
        else:
            self._send(status, b'{"error":"boom"}', "application/json")


def _parse_multipart(raw: bytes, content_type: str) -> dict:
    """テスト用の multipart/form-data パーサ（stdlib `email` を流用・Sherpa 送信側の組み立てを検証する）。

    戻り値 `{"fields": {name: text, ...}, "file": {"filename":..., "bytes":...} | None}`。
    """
    msg = email.message_from_bytes(
        b"Content-Type: " + content_type.encode("ascii") + b"\r\n\r\n" + raw)
    fields: dict[str, str] = {}
    file_part = None
    for part in msg.get_payload():
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        payload = part.get_payload(decode=True)
        if filename:
            file_part = {"filename": filename, "bytes": payload}
        else:
            fields[name] = (payload or b"").decode("utf-8", "replace").strip()
    return {"fields": fields, "file": file_part}


def _start_mock_worker(*, token=None, versions=None, convert_status=200,
                       convert_body=b"", delay=0.0, upload_status=None, upload_body=b""):
    srv = http.server.HTTPServer(("127.0.0.1", 0), _MockWorkerHandler)   # port 0＝空きポート（固定しない）
    srv.token = token
    srv.versions = versions if versions is not None else {
        "word": "16.0", "excel": "16.0", "powerpoint": "16.0"}
    srv.convert_status = convert_status
    srv.convert_body = convert_body
    srv.delay = delay
    # OFFICE-WIN-001: upload_status が None なら convert_status/convert_body を共用（既存テストは無変更）。
    srv.upload_status = upload_status
    srv.upload_body = upload_body
    srv.upload_fail_first_n = 0
    srv.last_body = None
    srv.last_upload = None
    srv.last_path = None
    srv.convert_calls = 0
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    return srv, url


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _use_office_com(monkeypatch, url, *, token=None):
    legacy_convert._healthz_cache.clear()                # URL 毎 TTL キャッシュを掃除（テスト間の混線防止）
    monkeypatch.setenv("SHERPA_LEGACY_BACKEND", "office_com")
    monkeypatch.setenv("SHERPA_OFFICE_COM_URL", url)
    if token is not None:
        monkeypatch.setenv("SHERPA_OFFICE_COM_TOKEN", token)
    else:
        monkeypatch.delenv("SHERPA_OFFICE_COM_TOKEN", raising=False)


# ---- パス変換（純関数）----

def test_wsl_to_windows_path_mnt_drive(monkeypatch):
    assert legacy_convert.wsl_to_windows_path("/mnt/c/test/旧資料.doc") == "C:\\test\\旧資料.doc"
    # 日本語＋スペースを含むパス（そのまま保持・区切りだけ変換）。
    assert legacy_convert.wsl_to_windows_path("/mnt/d/取込 5期/決算.xls") == "D:\\取込 5期\\決算.xls"
    # ドライブ直下（末尾なし/あり）。
    assert legacy_convert.wsl_to_windows_path("/mnt/c") == "C:\\"
    assert legacy_convert.wsl_to_windows_path("/mnt/c/") == "C:\\"
    # 相対・空・非パスは None（fail-safe）。
    assert legacy_convert.wsl_to_windows_path("relative/x.doc") is None
    assert legacy_convert.wsl_to_windows_path("") is None


def test_wsl_to_windows_path_wsl_native_fallback(monkeypatch):
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-24.04")
    assert (legacy_convert.wsl_to_windows_path("/home/tudo/資料/旧.doc")
            == "\\\\wsl.localhost\\Ubuntu-24.04\\home\\tudo\\資料\\旧.doc")
    # distro 不明なら変換不能（None）。
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    assert legacy_convert.wsl_to_windows_path("/home/tudo/旧.doc") is None


# ---- 到達性（office_com_available / healthz）----

def test_office_com_unset_url_unavailable(monkeypatch):
    # conftest が SHERPA_POWERSHELL_BIN を無効パスに固定＝URL 未設定かつ direct 未検出＝unavailable。
    monkeypatch.setenv("SHERPA_LEGACY_BACKEND", "office_com")
    monkeypatch.delenv("SHERPA_OFFICE_COM_URL", raising=False)
    legacy_convert._healthz_cache.clear()
    assert legacy_convert.office_com_configured() is False
    assert legacy_convert.office_com_mode() == "unavailable"
    assert legacy_convert.office_com_available() is False
    assert legacy_convert.office_com_healthz() is None


def test_office_com_connection_failure_unavailable(monkeypatch):
    url = f"http://127.0.0.1:{_free_port()}"        # 誰も listen していない空きポート＝接続失敗
    _use_office_com(monkeypatch, url)
    assert legacy_convert.office_com_configured() is True     # URL は設定済み
    assert legacy_convert.office_com_available() is False     # だが不達＝False（fail-safe）


def test_office_com_healthz_200(monkeypatch):
    srv, url = _start_mock_worker()
    try:
        _use_office_com(monkeypatch, url)
        assert legacy_convert.office_com_available() is True
        hz = legacy_convert.office_com_healthz()
        assert hz["ok"] is True and hz["worker"] == "1"
        assert hz["versions"]["word"] == "16.0"
        assert legacy_convert.legacy_exts() == {".doc", ".xls", ".ppt"}
        # RV Med（2026-07-08）: 全アプリ検出時は "office_com:<ソート済みアプリ名>"（版更新は含めない）。
        assert legacy_convert.legacy_sig_value() == "office_com:excel,powerpoint,word"
    finally:
        srv.shutdown(); srv.server_close()


# ---- RV Med（2026-07-08）: office_com はアプリ単位でゲート（Word のみ導入で .xls を候補化しない）----

def test_office_com_partial_apps_gates_exts_per_app(monkeypatch):
    """healthz `ok` だけで .doc/.xls/.ppt 全部を候補化すると、Word のみ導入環境で .xls が毎回失敗に
    寄ってしまう。versions で検出できたアプリの拡張子だけを legacy_exts() に含める。"""
    srv, url = _start_mock_worker(versions={"word": "16.0", "excel": False, "powerpoint": False})
    try:
        _use_office_com(monkeypatch, url)
        assert legacy_convert.legacy_exts() == {".doc"}                          # word だけ→.doc だけ
        assert legacy_convert.legacy_sig_value() == "office_com:word"
    finally:
        srv.shutdown(); srv.server_close()


def test_office_com_no_apps_detected_gates_to_empty(monkeypatch):
    """healthz は到達可（ok:true）だが versions が全て False（Office 未導入端末でワーカーだけ動いている）
    ケース＝候補ゼロ・sig は "none"。"""
    srv, url = _start_mock_worker(versions={"word": False, "excel": False, "powerpoint": False})
    try:
        _use_office_com(monkeypatch, url)
        assert legacy_convert.office_com_available() is True   # ワーカー自体には到達できる
        assert legacy_convert.legacy_exts() == set()            # だが変換できるアプリが無い
        assert legacy_convert.legacy_sig_value() == "none"
    finally:
        srv.shutdown(); srv.server_close()


def test_office_com_version_unknown_true_still_counts_available(monkeypatch):
    """versions の値が True（ProgID 登録はあるがバージョン不明・Get-OneOfficeVersion の返り値）でも
    「使える」扱いにする（False だけを未導入として除外する）。"""
    srv, url = _start_mock_worker(versions={"word": True, "excel": False, "powerpoint": False})
    try:
        _use_office_com(monkeypatch, url)
        assert legacy_convert.legacy_exts() == {".doc"}
        assert legacy_convert.legacy_sig_value() == "office_com:word"
    finally:
        srv.shutdown(); srv.server_close()


# ---- RV Med（2026-07-08）: SHERPA_LEGACY_EXTS override（MCP サブプロセス用スナップショット・token 漏洩対策）----

def test_legacy_exts_env_override_takes_priority_no_probe(monkeypatch):
    """SHERPA_LEGACY_EXTS が設定されていれば最優先で信じ、office_com への healthz probe を一切行わない
    （MCP サブプロセスは URL/TOKEN を持たない設計＝probe しようとすると必ず失敗するか secrets が要る）。
    ここでは office_com_healthz を呼んだら AssertionError になるようにして「呼ばれないこと」を証明する。"""
    monkeypatch.setenv("SHERPA_LEGACY_BACKEND", "office_com")
    monkeypatch.setenv("SHERPA_OFFICE_COM_URL", "http://127.0.0.1:1")   # 到達不可（呼ばれたら分かるようにあえて設定）
    monkeypatch.delenv("SHERPA_OFFICE_COM_TOKEN", raising=False)

    def _boom():
        raise AssertionError("office_com_healthz が呼ばれた（SHERPA_LEGACY_EXTS が最優先されていない）")

    monkeypatch.setattr(legacy_convert, "office_com_healthz", _boom)
    monkeypatch.setenv("SHERPA_LEGACY_EXTS", ".doc,.xls")
    assert legacy_convert.legacy_exts() == {".doc", ".xls"}         # probe せずそのまま信じる


def test_legacy_exts_env_override_empty_string_means_none(monkeypatch):
    monkeypatch.setenv("SHERPA_LEGACY_EXTS", "")
    assert legacy_convert.legacy_exts() == set()


def test_legacy_exts_env_override_absent_falls_back_to_normal_resolution(monkeypatch):
    """env が**未設定**（キー自体が無い）なら通常解決（backend none）に落ちる＝override は「設定時のみ」有効。"""
    monkeypatch.delenv("SHERPA_LEGACY_EXTS", raising=False)
    monkeypatch.delenv("SHERPA_LEGACY_BACKEND", raising=False)
    assert legacy_convert.legacy_exts() == set()


def test_office_com_token_mismatch_401_unavailable(monkeypatch):
    srv, url = _start_mock_worker(token="secret-xyz")
    try:
        _use_office_com(monkeypatch, url, token="WRONG")     # 不一致＝401＝到達不可扱い
        assert legacy_convert.office_com_available() is False
        assert legacy_convert.legacy_exts() == set()
        assert legacy_convert.legacy_sig_value() == "none"
    finally:
        srv.shutdown(); srv.server_close()


def test_office_com_token_match_available(monkeypatch):
    srv, url = _start_mock_worker(token="secret-xyz")
    try:
        _use_office_com(monkeypatch, url, token="secret-xyz")
        assert legacy_convert.office_com_available() is True
    finally:
        srv.shutdown(); srv.server_close()


def test_office_com_healthz_cached_short_ttl(monkeypatch):
    """/healthz は短TTLでキャッシュ＝落ちても TTL 内は結果を保持する（叩き過ぎ防止）。"""
    srv, url = _start_mock_worker()
    try:
        _use_office_com(monkeypatch, url)
        assert legacy_convert.office_com_available() is True
    finally:
        srv.shutdown(); srv.server_close()
    # ワーカーが落ちても、TTL 内は直前の True を返す（キャッシュを消していないため）。
    assert legacy_convert.office_com_available() is True


# ---- 変換（_convert_office_com / convert_to_ooxml）----

def test_convert_office_com_200_returns_bytes(monkeypatch):
    srv, url = _start_mock_worker(convert_body=_docx_bytes())
    try:
        _use_office_com(monkeypatch, url)
        src = pathlib.Path("/mnt/c/test/旧資料.doc")     # 実在不要（パス変換して送るだけ・モックは中身を見ない）
        data = legacy_convert.convert_to_ooxml(src, ".docx")
        assert data is not None and data[:2] == b"PK"
        assert srv.last_body == {"path": "C:\\test\\旧資料.doc", "target": "docx"}
    finally:
        srv.shutdown(); srv.server_close()


def test_convert_office_com_401_returns_none(monkeypatch):
    srv, url = _start_mock_worker(token="secret", convert_body=_docx_bytes())
    try:
        _use_office_com(monkeypatch, url, token="WRONG")
        src = pathlib.Path("/mnt/c/旧資料.doc")
        assert legacy_convert.convert_to_ooxml(src, ".docx") is None    # 401＝None（fail-safe）
    finally:
        srv.shutdown(); srv.server_close()


def test_convert_office_com_500_returns_none(monkeypatch):
    srv, url = _start_mock_worker(convert_status=500)
    try:
        _use_office_com(monkeypatch, url)
        src = pathlib.Path("/mnt/c/旧資料.doc")
        assert legacy_convert.convert_to_ooxml(src, ".docx") is None    # 5xx＝None（fail-safe）
    finally:
        srv.shutdown(); srv.server_close()


def test_convert_office_com_timeout_returns_none(monkeypatch):
    srv, url = _start_mock_worker(convert_body=_docx_bytes(), delay=1.0)
    try:
        _use_office_com(monkeypatch, url)
        monkeypatch.setenv("SHERPA_LEGACY_TIMEOUT", "0.3")              # delay(1.0) > timeout(0.3)
        src = pathlib.Path("/mnt/c/旧資料.doc")
        assert legacy_convert.convert_to_ooxml(src, ".docx") is None    # タイムアウト＝None（fail-safe）
    finally:
        srv.shutdown(); srv.server_close()


def test_convert_office_com_unconvertible_path_returns_none(monkeypatch):
    srv, url = _start_mock_worker(convert_body=_docx_bytes())
    try:
        _use_office_com(monkeypatch, url)
        monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)            # WSL ネイティブ→変換不能
        src = pathlib.Path("/home/tudo/旧資料.doc")
        assert legacy_convert.convert_to_ooxml(src, ".docx") is None    # パス変換不能＝送らず None
        assert srv.convert_calls == 0                                   # ワーカーへは投げていない
    finally:
        srv.shutdown(); srv.server_close()


# ---- Med-2（レビュー是正・2026-07-22）: _convert_office_com_ex の fallback_worthy 判別 ----
#
# 現状バグ: path 方式試行のあらゆる HTTPError を None に潰して auto モードが upload へ縮退していた。
# /convert が 500（COM 変換失敗・権限・ロック等）でも upload 再試行が成功すると path 側の真の失敗が隠れる。
# 修正後は fallback_worthy=True になるのは (i) パス変換不能・(ii) ネットワーク到達不能・(iii) worker の
# 404「file not found」のみ。500 等はそのまま伝播し fallback_worthy=False。

def test_convert_office_com_ex_success_data_and_not_fallback_worthy(monkeypatch):
    srv, url = _start_mock_worker(convert_body=_docx_bytes())
    try:
        _use_office_com(monkeypatch, url)
        src = pathlib.Path("/mnt/c/test/旧資料.doc")
        data, fallback_worthy = legacy_convert._convert_office_com_ex(src, ".docx")
        assert data is not None and data[:2] == b"PK"
        assert fallback_worthy is False
    finally:
        srv.shutdown(); srv.server_close()


def test_convert_office_com_ex_500_not_fallback_worthy(monkeypatch):
    srv, url = _start_mock_worker(convert_status=500)
    try:
        _use_office_com(monkeypatch, url)
        src = pathlib.Path("/mnt/c/旧資料.doc")
        data, fallback_worthy = legacy_convert._convert_office_com_ex(src, ".docx")
        assert data is None
        assert fallback_worthy is False           # 500＝真の失敗・fallback 対象にしない
    finally:
        srv.shutdown(); srv.server_close()


def test_convert_office_com_ex_404_fallback_worthy(monkeypatch):
    srv, url = _start_mock_worker(convert_status=404)
    try:
        _use_office_com(monkeypatch, url)
        src = pathlib.Path("/mnt/c/旧資料.doc")
        data, fallback_worthy = legacy_convert._convert_office_com_ex(src, ".docx")
        assert data is None
        assert fallback_worthy is True             # 404＝「見つからない」と判別可能・fallback 対象
    finally:
        srv.shutdown(); srv.server_close()


def test_convert_office_com_ex_401_not_fallback_worthy(monkeypatch):
    """401（認証失敗）は「パスが見つからない」と無関係の失敗＝fallback しない（upload も同じトークンで
    失敗するだけなので縮退しても意味が無い・真の失敗として伝播するのが正しい）。"""
    srv, url = _start_mock_worker(token="secret", convert_body=_docx_bytes())
    try:
        _use_office_com(monkeypatch, url, token="WRONG")
        src = pathlib.Path("/mnt/c/旧資料.doc")
        data, fallback_worthy = legacy_convert._convert_office_com_ex(src, ".docx")
        assert data is None
        assert fallback_worthy is False
    finally:
        srv.shutdown(); srv.server_close()


def test_convert_office_com_ex_network_unreachable_fallback_worthy(monkeypatch):
    url = f"http://127.0.0.1:{_free_port()}"        # 誰も listen していない空きポート＝接続失敗
    _use_office_com(monkeypatch, url)
    src = pathlib.Path("/mnt/c/旧資料.doc")
    data, fallback_worthy = legacy_convert._convert_office_com_ex(src, ".docx")
    assert data is None
    assert fallback_worthy is True                 # ネットワーク到達不能＝fallback 対象


def test_convert_office_com_ex_timeout_fallback_worthy(monkeypatch):
    srv, url = _start_mock_worker(convert_body=_docx_bytes(), delay=1.0)
    try:
        _use_office_com(monkeypatch, url)
        monkeypatch.setenv("SHERPA_LEGACY_TIMEOUT", "0.3")
        src = pathlib.Path("/mnt/c/旧資料.doc")
        data, fallback_worthy = legacy_convert._convert_office_com_ex(src, ".docx")
        assert data is None
        assert fallback_worthy is True             # タイムアウトもネットワーク到達不能扱い＝fallback 対象
        # office_com HTTP（path 転送）のタイムアウトも thread-local へ通知する
        # （libreoffice の subprocess タイムアウトだけに限らない）。
        assert legacy_convert.take_conversion_failure_reason() == "timeout"
    finally:
        srv.shutdown(); srv.server_close()


def test_convert_office_com_via_transfer_mode_auto_clears_timeout_reason_on_upload_fallback_success(monkeypatch):
    """path 方式がタイムアウトしても、auto モードの upload 縮退が成功すれば理由は残さない
    （最終的に変換できているのに前段の失敗理由を誤って報告しない）。

    実 HTTP モックの `delay` は path/upload 両エンドポイントへ一律にかかるため、path 側の
    タイムアウトだけを再現するにはここでは関数レベルで差し替える（`_convert_office_com_ex`/
    `_convert_office_com_upload` 自体のタイムアウト検知は上の HTTP モックテストで別途固定済み）。
    """
    monkeypatch.setenv("SHERPA_OFFICE_TRANSFER_MODE", "auto")

    def _fake_ex(src, target_ext):
        legacy_convert._note_conversion_failure_reason("timeout")   # path 側がタイムアウトした体
        return None, True
    monkeypatch.setattr(legacy_convert, "_convert_office_com_ex", _fake_ex)
    monkeypatch.setattr(legacy_convert, "_convert_office_com_upload", lambda src, target_ext: b"PK\x03\x04upload-ok")

    data = legacy_convert._convert_office_com_via_transfer_mode(pathlib.Path("/mnt/c/旧資料.doc"), ".docx")
    assert data == b"PK\x03\x04upload-ok"                   # upload 縮退で最終的に成功
    assert legacy_convert.take_conversion_failure_reason() is None


def test_convert_office_com_ex_unmappable_path_fallback_worthy_no_network_call(monkeypatch):
    srv, url = _start_mock_worker(convert_body=_docx_bytes())
    try:
        _use_office_com(monkeypatch, url)
        monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
        src = pathlib.Path("/home/tudo/旧資料.doc")
        data, fallback_worthy = legacy_convert._convert_office_com_ex(src, ".docx")
        assert data is None
        assert fallback_worthy is True             # パス変換不能＝fallback 対象
        assert srv.convert_calls == 0               # 送らずに判定できている
    finally:
        srv.shutdown(); srv.server_close()


# ---- Med-2: auto モードは 500（真の COM 失敗）で upload へ縮退しない・404/到達不能/パス不能では縮退する ----

def test_auto_mode_500_does_not_fallback_propagates_failure(tmp_path, monkeypatch):
    """真の COM 失敗（500）は upload 側が仮に成功する設定でも fallback せず None を返す（Med-2 の核心）。"""
    srv, url = _start_mock_worker(convert_status=500, upload_status=200, upload_body=_docx_bytes())
    try:
        _use_office_com(monkeypatch, url)
        monkeypatch.setenv("SHERPA_OFFICE_TRANSFER_MODE", "auto")
        monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-24.04")
        src = tmp_path / "旧資料.doc"
        src.write_bytes(b"\xd0\xcf\x11\xe0 old binary")
        data = legacy_convert.convert_to_ooxml(src, ".docx")
        assert data is None                          # upload が成功する設定でも縮退せず失敗のまま
        assert srv.last_path == "/convert"            # /convert-upload は一切呼ばれていない
        assert srv.convert_calls == 1                 # path 側の1回だけ
    finally:
        srv.shutdown(); srv.server_close()


def test_auto_mode_render_500_does_not_fallback_propagates_failure(tmp_path, monkeypatch):
    srv, url = _start_mock_worker(convert_status=500, upload_status=200, upload_body=_pdf_bytes())
    try:
        _use_office_com(monkeypatch, url)
        monkeypatch.setenv("SHERPA_OFFICE_TRANSFER_MODE", "auto")
        monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-24.04")
        src = tmp_path / "資料.pptx"
        src.write_bytes(b"PK fake pptx")
        data = legacy_convert.render_pdf(src)
        assert data is None
        assert srv.last_path == "/render"
        assert srv.convert_calls == 1
    finally:
        srv.shutdown(); srv.server_close()


# ---- キャッシュ＋来歴（ensure_ooxml が office_com 経由で bytes をキャッシュ）----

def test_ensure_ooxml_office_com_caches_and_notes(tmp_path, monkeypatch):
    srv, url = _start_mock_worker(convert_body=_docx_bytes(),
                                  versions={"word": "16.0", "excel": False, "powerpoint": "16.0"})
    try:
        _use_office_com(monkeypatch, url)
        # /mnt 経由のパスにするため tmp を偽装せず、実ファイルを置いて mtime/size キーを取る。
        src = tmp_path / "旧資料.doc"
        src.write_bytes(b"\xd0\xcf\x11\xe0 old binary")
        # wsl_to_windows_path が /mnt でない tmp を変換できるよう distro を設定（\\wsl.localhost フォールバック）。
        monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-24.04")
        cache_root = tmp_path / "_legacy_cache"

        first = legacy_convert.ensure_ooxml(src, "旧資料.doc", cache_root)
        assert first is not None
        ooxml_path, notes = first
        assert ooxml_path.is_file() and ooxml_path.suffix == ".docx"
        assert ooxml_path.read_bytes()[:2] == b"PK"
        assert "legacy_backend=office_com" in notes
        # versions 要約は検出できた Office だけ（excel は False＝載せない）。
        assert any(n == "office_com_versions=word=16.0,powerpoint=16.0" for n in notes)
        assert srv.convert_calls == 1

        # 原本 unchanged → キャッシュヒット（ワーカーを再度叩かない）。
        second = legacy_convert.ensure_ooxml(src, "旧資料.doc", cache_root)
        assert second is not None
        assert srv.convert_calls == 1

        # 変換された OOXML は①OOXML アームで MD 化できる（値の権威に委譲）。
        md = office_md.to_markdown(ooxml_path)
        assert md is not None and "旧資料の中身テキストXYZ" in md
    finally:
        srv.shutdown(); srv.server_close()


# ==== office_com direct モード（W2'・WSL interop で ps1 を one-shot・feedback-batch-2026-07-08 ⑥）====
#
# 実 Office/COM/powershell.exe は使わず、偽 powershell.exe（WSL 内 sh スクリプト）で
# ps1 の -Healthz / -DirectJob を最小エミュレートする（モード解決・healthz キャッシュ・変換/レンダ・
# 失敗/タイムアウトの fail-safe・プロセスグループ kill を検証）。実機での実変換は共同検証で行う。
#
# 偽 powershell は -DirectJob の -OutPath（\\wsl.localhost の Windows UNC）を sed で WSL パスへ逆変換して
# fixture を書く（実 ps1 は WriteAllBytes で UNC に書く＝WSL の /tmp に届く。ここではその往復を sh で模す）。

_FAKE_POWERSHELL = r"""#!/usr/bin/env bash
mode=""; outpath=""; errpath=""; job="convert"; tsec=""
while [ $# -gt 0 ]; do
  case "$1" in
    -Healthz)   mode="healthz"; shift;;
    -DirectJob) mode="direct"; shift;;
    -OutPath)   outpath="$2"; shift 2;;
    -ErrPath)   errpath="$2"; shift 2;;
    -Job)       job="$2"; shift 2;;
    -JobTimeoutSec) tsec="$2"; shift 2;;
    *) shift;;
  esac
done
unc_to_wsl() { printf '%s' "$1" | sed -E 's#^\\\\wsl\.localhost\\[^\\]+\\#/#; s#\\#/#g'; }
if [ "$mode" = "healthz" ]; then
  if [ -n "$FAKE_PS_HEALTHZ" ]; then printf '%s' "$FAKE_PS_HEALTHZ"
  else printf '%s' '{"ok":true,"versions":{"word":"16.0","excel":"16.0","powerpoint":"16.0"},"worker":"direct"}'; fi
  exit 0
fi
if [ "$mode" = "direct" ]; then
  [ -n "$FAKE_PS_COUNTER" ] && echo x >> "$FAKE_PS_COUNTER"
  # RV Med（2026-07-08）: WSL 側が -JobTimeoutSec に渡した値を可視化する（0 になっていないことを unit で確認する）。
  [ -n "$FAKE_PS_TIMEOUT_CAPTURE" ] && printf '%s' "$tsec" > "$FAKE_PS_TIMEOUT_CAPTURE"
  if [ -n "$FAKE_PS_SLEEP" ]; then
    sleep "$FAKE_PS_SLEEP" &
    [ -n "$FAKE_PS_CHILD_PID_FILE" ] && echo $! > "$FAKE_PS_CHILD_PID_FILE"
    sleep "$FAKE_PS_SLEEP"
  fi
  if [ -n "$FAKE_PS_EXIT" ] && [ "$FAKE_PS_EXIT" != "0" ]; then
    [ -n "$errpath" ] && printf '%s' "${FAKE_PS_ERR:-fake failure}" > "$(unc_to_wsl "$errpath")"
    exit "$FAKE_PS_EXIT"
  fi
  fixture="$FAKE_PS_DOCX"
  [ "$job" = "render" ] && fixture="$FAKE_PS_PDF"
  cp "$fixture" "$(unc_to_wsl "$outpath")"
  exit 0
fi
exit 3
"""


def _pdf_bytes() -> bytes:
    return b"%PDF-1.4\n%mock sherpa render\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def _install_fake_powershell(tmp_path: pathlib.Path, monkeypatch):
    """偽 powershell.exe を用意して direct モードにする（URL 未設定・backend=office_com・distro 設定）。"""
    script = tmp_path / "fake_powershell.sh"
    script.write_text(_FAKE_POWERSHELL, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    docx = _make_template(tmp_path)                     # 変換 fixture（docx zip）
    pdf = tmp_path / "render.pdf"
    pdf.write_bytes(_pdf_bytes())                        # レンダ fixture（pdf）
    monkeypatch.setenv("SHERPA_POWERSHELL_BIN", str(script))
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-24.04")   # /tmp を \\wsl.localhost UNC へ変換可能に
    monkeypatch.delenv("SHERPA_OFFICE_COM_URL", raising=False)   # URL 未設定＝direct へ倒す
    monkeypatch.setenv("SHERPA_LEGACY_BACKEND", "office_com")
    monkeypatch.setenv("FAKE_PS_DOCX", str(docx))
    monkeypatch.setenv("FAKE_PS_PDF", str(pdf))
    legacy_convert._direct_healthz_cache.clear()
    return script


# ---- モード解決・powershell 検出 ----

def test_office_com_mode_http_when_url_set(tmp_path, monkeypatch):
    """URL 設定時は powershell を検出できても http モード（別ホスト優先）。"""
    _install_fake_powershell(tmp_path, monkeypatch)         # powershell は検出可
    monkeypatch.setenv("SHERPA_OFFICE_COM_URL", "http://127.0.0.1:9")   # だが URL 設定済み
    assert legacy_convert.office_com_mode() == "http"


def test_office_com_mode_direct_when_powershell_detected(tmp_path, monkeypatch):
    _install_fake_powershell(tmp_path, monkeypatch)
    assert legacy_convert.office_com_mode() == "direct"
    assert legacy_convert.powershell_available() is True


def test_office_com_mode_unavailable_without_url_or_powershell(monkeypatch):
    monkeypatch.delenv("SHERPA_OFFICE_COM_URL", raising=False)
    monkeypatch.setenv("SHERPA_POWERSHELL_BIN", "/no/such/powershell.exe")
    assert legacy_convert.office_com_mode() == "unavailable"
    assert legacy_convert.powershell_available() is False


def test_powershell_bin_override_missing_returns_none(monkeypatch):
    monkeypatch.setenv("SHERPA_POWERSHELL_BIN", "/no/such/powershell.exe")
    assert legacy_convert._powershell_bin() is None


def test_powershell_bin_override_non_executable_rejected(tmp_path, monkeypatch):
    not_exec = tmp_path / "powershell.txt"
    not_exec.write_text("not executable", encoding="utf-8")   # X_OK 無し
    monkeypatch.setenv("SHERPA_POWERSHELL_BIN", str(not_exec))
    assert legacy_convert._powershell_bin() is None


# ---- direct healthz（-Healthz one-shot・長め TTL キャッシュ）----

def test_direct_healthz_available_and_gates(tmp_path, monkeypatch):
    _install_fake_powershell(tmp_path, monkeypatch)
    assert legacy_convert.office_com_available() is True
    hz = legacy_convert.office_com_healthz()
    assert hz["ok"] is True and hz["worker"] == "direct"
    assert hz["versions"]["word"] == "16.0"
    assert legacy_convert.legacy_exts() == {".doc", ".xls", ".ppt"}
    assert legacy_convert.legacy_sig_value() == "office_com:excel,powerpoint,word"


def test_direct_healthz_partial_apps_gates_per_app(tmp_path, monkeypatch):
    _install_fake_powershell(tmp_path, monkeypatch)
    monkeypatch.setenv(
        "FAKE_PS_HEALTHZ",
        '{"ok":true,"versions":{"word":"16.0","excel":false,"powerpoint":false},"worker":"direct"}')
    assert legacy_convert.legacy_exts() == {".doc"}
    assert legacy_convert.legacy_sig_value() == "office_com:word"


def test_direct_healthz_cached_short_circuits_probe(tmp_path, monkeypatch):
    """direct healthz は長め TTL でキャッシュ＝2回目は powershell を叩かない（FAKE_PS_COUNTER で回数を確認）。"""
    _install_fake_powershell(tmp_path, monkeypatch)
    counter = tmp_path / "hz_counter.txt"
    monkeypatch.setenv("FAKE_PS_COUNTER", str(counter))     # ※ healthz 経路は counter を増やさない実装だが…
    # -Healthz は FAKE_PS_COUNTER を増やさない（direct ジョブのみ）。回数はキャッシュ挙動で担保する:
    assert legacy_convert.office_com_available() is True
    # キャッシュを消さずに再取得＝同一 dict（新規プローブしない）。
    first = legacy_convert.office_com_healthz()
    second = legacy_convert.office_com_healthz()
    assert first is second


# ---- direct 変換（_convert_office_com_direct / convert_to_ooxml）----

def test_convert_office_com_direct_success(tmp_path, monkeypatch):
    _install_fake_powershell(tmp_path, monkeypatch)
    src = tmp_path / "旧資料.doc"
    src.write_bytes(b"\xd0\xcf\x11\xe0 old binary")
    data = legacy_convert.convert_to_ooxml(src, ".docx")
    assert data is not None and data[:2] == b"PK"            # zip（OOXML）先頭
    # 変換された OOXML は①OOXML アームで MD 化できる（値の権威に委譲）。
    out = tmp_path / "out.docx"
    out.write_bytes(data)
    md = office_md.to_markdown(out)
    assert md is not None and "旧資料の中身テキストXYZ" in md


def test_convert_office_com_direct_failure_returns_none(tmp_path, monkeypatch):
    _install_fake_powershell(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_PS_EXIT", "1")                  # ps1 が非0終了＝変換失敗
    src = tmp_path / "旧資料.doc"
    src.write_bytes(b"x")
    assert legacy_convert.convert_to_ooxml(src, ".docx") is None   # fail-safe


def test_convert_office_com_direct_unconvertible_path_returns_none(tmp_path, monkeypatch):
    _install_fake_powershell(tmp_path, monkeypatch)
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)     # WSL ネイティブ→Windows パス変換不能
    src = tmp_path / "旧資料.doc"                             # /tmp は /mnt でない＝distro 無しで変換不能
    src.write_bytes(b"x")
    assert legacy_convert.convert_to_ooxml(src, ".docx") is None   # 送らず None


def test_convert_office_com_direct_timeout_kills_process_group(tmp_path, monkeypatch):
    """direct の backstop タイムアウト（ps1 へ渡した整数秒 `inner_arg` + `_DIRECT_GRACE_SEC`）で、偽 powershell と
    その孫（sleep &・同一プロセスグループ）が確実に kill されること（残骸プロセス化の防止）。"""
    _install_fake_powershell(tmp_path, monkeypatch)
    child_pid_file = tmp_path / "child_pid.txt"
    monkeypatch.setenv("FAKE_PS_CHILD_PID_FILE", str(child_pid_file))
    monkeypatch.setenv("FAKE_PS_SLEEP", "30")                # 30 秒スリープ＝確実に backstop 超過
    monkeypatch.setenv("SHERPA_LEGACY_TIMEOUT", "0.3")       # inner_arg = max(1, ceil(0.3)) = 1
    monkeypatch.setattr(legacy_convert, "_DIRECT_GRACE_SEC", 0.2)   # backstop = 1 + 0.2 = 1.2s

    src = tmp_path / "旧資料.doc"
    src.write_bytes(b"x")
    assert legacy_convert.convert_to_ooxml(src, ".docx") is None    # タイムアウト＝None（fail-safe）
    # direct モード（WSL interop ps1 one-shot）のタイムアウトも thread-local へ通知する。
    assert legacy_convert.take_conversion_failure_reason() == "timeout"

    deadline = time.monotonic() + 3.0
    child_pid = None
    while time.monotonic() < deadline and child_pid is None:
        if child_pid_file.exists():
            try:
                child_pid = int(child_pid_file.read_text(encoding="utf-8").strip())
            except ValueError:
                pass
        if child_pid is None:
            time.sleep(0.05)
    assert child_pid is not None, "孫プロセスの pid が取得できなかった（テスト前提が崩れている）"

    gone = False
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            gone = True
            break
        time.sleep(0.05)
    assert gone, f"孫プロセス（pid={child_pid}）が残っている（プロセスグループ kill が効いていない）"


def test_direct_job_timeout_sec_never_zero_for_sub_second_config(tmp_path, monkeypatch):
    """RV Med（2026-07-08）: SHERPA_LEGACY_TIMEOUT が1秒未満（例 0.3）でも `-JobTimeoutSec` に "0" を渡さない。

    0 を渡すと ps1 側の `Invoke-DirectJob` が `$tsec -le 0` で既定120秒へフォールバックしてしまい、WSL 側の
    backstop（元の小さい値+grace）が先に外側 powershell を kill して `Stop-CandidateProcesses` が一度も
    走らないまま Office が孤児化しうる（実バグ）。偽 powershell が実際に受け取った `-JobTimeoutSec` の値を
    ファイルへ書き出し、0.3 → 1 以上（ceil）に切り上がっていることを直接確認する。"""
    _install_fake_powershell(tmp_path, monkeypatch)
    capture = tmp_path / "captured_timeout.txt"
    monkeypatch.setenv("FAKE_PS_TIMEOUT_CAPTURE", str(capture))
    monkeypatch.setenv("SHERPA_LEGACY_TIMEOUT", "0.3")

    src = tmp_path / "旧資料.doc"
    src.write_bytes(b"\xd0\xcf\x11\xe0 old binary")
    data = legacy_convert.convert_to_ooxml(src, ".docx")
    assert data is not None                                   # 正常に変換できている（0 秒扱いで即失敗していない）

    assert capture.is_file(), "偽 powershell が -JobTimeoutSec を受け取れなかった"
    captured = capture.read_text(encoding="utf-8").strip()
    assert captured != "0" and captured != "", f"-JobTimeoutSec に 0/空 が渡された: {captured!r}"
    assert int(captured) >= 1
    assert int(captured) == 1                                 # ceil(0.3) == 1（切り上げの具体値も確認）


def test_direct_job_timeout_sec_passthrough_for_whole_second_config(tmp_path, monkeypatch):
    """1秒以上の設定はそのまま（切り上げによる余計な繰り上がりが無い）ことも確認する。"""
    _install_fake_powershell(tmp_path, monkeypatch)
    capture = tmp_path / "captured_timeout.txt"
    monkeypatch.setenv("FAKE_PS_TIMEOUT_CAPTURE", str(capture))
    monkeypatch.setenv("SHERPA_LEGACY_TIMEOUT", "5")

    src = tmp_path / "旧資料.doc"
    src.write_bytes(b"\xd0\xcf\x11\xe0 old binary")
    assert legacy_convert.convert_to_ooxml(src, ".docx") is not None
    assert capture.read_text(encoding="utf-8").strip() == "5"


# ---- 忠実 PDF レンダ（render_pdf・direct / http・アーム接続は次スライス）----

def test_render_pdf_direct_success(tmp_path, monkeypatch):
    _install_fake_powershell(tmp_path, monkeypatch)
    src = tmp_path / "資料.docx"                             # 新形式も受ける（render の対象）
    src.write_bytes(b"PK fake docx")
    data = legacy_convert.render_pdf(src)
    assert data is not None and data[:4] == b"%PDF"


def test_render_pdf_http_uses_render_endpoint(monkeypatch):
    srv, url = _start_mock_worker(convert_body=_pdf_bytes())
    try:
        _use_office_com(monkeypatch, url)                   # URL 設定＝http モード
        src = pathlib.Path("/mnt/c/test/資料.pptx")
        data = legacy_convert.render_pdf(src)
        assert data is not None and data[:4] == b"%PDF"
        assert srv.last_path == "/render"                   # /render へ投げている
        assert srv.last_body == {"path": "C:\\test\\資料.pptx"}
    finally:
        srv.shutdown(); srv.server_close()


def test_render_pdf_unsupported_ext_returns_none(tmp_path, monkeypatch):
    _install_fake_powershell(tmp_path, monkeypatch)
    src = tmp_path / "memo.txt"
    src.write_text("x", encoding="utf-8")
    assert legacy_convert.render_pdf(src) is None           # 対象外拡張子


def test_render_pdf_unavailable_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv("SHERPA_OFFICE_COM_URL", raising=False)
    monkeypatch.setenv("SHERPA_POWERSHELL_BIN", "/no/such/powershell.exe")
    src = tmp_path / "資料.docx"
    src.write_bytes(b"PK fake docx")
    assert legacy_convert.render_pdf(src) is None           # unavailable＝None（fail-safe）


# ---- キャッシュキーが office_com の動作形態（http/direct）切替に反応する（RV Med・2026-07-08）----

def test_source_key_changes_with_office_com_mode(tmp_path, monkeypatch):
    """`_source_key` は backend=office_com のとき実効モード（http/direct）もキーへ混ぜる。同じ backend 名の
    ままモードだけ変わっても異なるキーになること（他バックエンドはモード欄が空のまま安定すること）を確認する。"""
    _install_fake_powershell(tmp_path, monkeypatch)              # direct 利用可能に（URL 未設定）
    src = tmp_path / "旧資料.doc"
    src.write_bytes(b"x")

    assert legacy_convert.office_com_mode() == "direct"
    key_direct = legacy_convert._source_key(src)
    assert key_direct.startswith("office_com:direct:")

    monkeypatch.setenv("SHERPA_OFFICE_COM_URL", "http://127.0.0.1:1")   # URL 設定＝http 優先
    assert legacy_convert.office_com_mode() == "http"
    key_http = legacy_convert._source_key(src)
    assert key_http.startswith("office_com:http:")

    assert key_direct != key_http                                # モードが変われば必ずキーも変わる

    # 他バックエンドはモード概念が無い（空欄のまま・libreoffice/none の間で形式が安定している）。
    monkeypatch.setenv("SHERPA_LEGACY_BACKEND", "libreoffice")
    assert legacy_convert._source_key(src).startswith("libreoffice::")
    monkeypatch.setenv("SHERPA_LEGACY_BACKEND", "none")
    assert legacy_convert._source_key(src).startswith("none::")


def test_ensure_ooxml_cache_miss_on_http_to_direct_mode_switch(tmp_path, monkeypatch):
    """RV Med（2026-07-08）: backend=office_com のまま http→direct へ動作形態が切り替わったら、実際の変換元
    （別ホストの Office／同一マシンの Office）が変わるためキャッシュをヒットさせず再変換する。"""
    _install_fake_powershell(tmp_path, monkeypatch)               # direct 側の受け皿（URL 未設定なら使われる）
    direct_counter = tmp_path / "direct_counter.txt"
    monkeypatch.setenv("FAKE_PS_COUNTER", str(direct_counter))
    legacy_convert._healthz_cache.clear()                          # http 側 TTL キャッシュの混線防止
    srv, url = _start_mock_worker(convert_body=_docx_bytes())     # http 側の受け皿
    try:
        src = tmp_path / "旧資料.doc"
        src.write_bytes(b"\xd0\xcf\x11\xe0 old binary")
        cache_root = tmp_path / "_legacy_cache"

        # 1) http モード（URL 設定）で変換。
        monkeypatch.setenv("SHERPA_OFFICE_COM_URL", url)
        assert legacy_convert.office_com_mode() == "http"
        first = legacy_convert.ensure_ooxml(src, "旧資料.doc", cache_root)
        assert first is not None
        assert srv.convert_calls == 1
        assert _count(direct_counter) == 0                        # direct 側はまだ一度も呼ばれていない

        # 2) URL を外す → direct モードへ切替（powershell は検出可のまま・原本は unchanged）。
        monkeypatch.delenv("SHERPA_OFFICE_COM_URL", raising=False)
        assert legacy_convert.office_com_mode() == "direct"
        second = legacy_convert.ensure_ooxml(src, "旧資料.doc", cache_root)
        assert second is not None
        assert srv.convert_calls == 1                              # http ワーカーは再度呼ばれていない
        assert _count(direct_counter) == 1                         # だが direct 側は新規に呼ばれた（キャッシュミス）

        # 3) 元の http へ戻す。キャッシュスロット（rel+target_ext）は1つで、直前に direct のキーで
        #    上書きされているため、この切替もまたキャッシュミス＝再変換になる（"モードが変われば必ず
        #    ミスする" という本 RV の主旨どおり・http 側が2回目の呼び出しを受ける）。
        monkeypatch.setenv("SHERPA_OFFICE_COM_URL", url)
        third = legacy_convert.ensure_ooxml(src, "旧資料.doc", cache_root)
        assert third is not None
        assert srv.convert_calls == 2                              # 切替直後はミス＝再変換
        assert _count(direct_counter) == 1                         # direct 側は呼ばれていない

        # 4) 同じ http モードのまま原本 unchanged で再度呼ぶ → 今度はキャッシュヒット（安定状態では再変換しない）。
        fourth = legacy_convert.ensure_ooxml(src, "旧資料.doc", cache_root)
        assert fourth is not None
        assert srv.convert_calls == 2                               # ヒット＝増えない
        assert _count(direct_counter) == 1
    finally:
        srv.shutdown(); srv.server_close()


# ==== OFFICE-WIN-001（2026-07-20-調査型RAG詳細修正計画.html §6.5・ファイル送信方式）====
#
# transfer_mode（path/upload/auto）の解決・multipart 送信の組み立て・upload 変換/レンダ・auto の
# path→upload 縮退・失敗リトライ・接続テスト関数（probe_office_com）を検証する。direct モードには
# transfer_mode の概念が無い（既存 direct テスト群は無変更のまま緑であることが「影響しない」ことの証明）。

# ---- transfer_mode 解決（system_settings > env > 既定 "path"）----

def test_transfer_mode_default_path(monkeypatch):
    monkeypatch.delenv("SHERPA_OFFICE_TRANSFER_MODE", raising=False)
    assert legacy_convert.transfer_mode_name() == "path"
    assert legacy_convert.env_default_transfer_mode() == "path"


def test_transfer_mode_env_over_default(monkeypatch):
    monkeypatch.setenv("SHERPA_OFFICE_TRANSFER_MODE", "upload")
    assert legacy_convert.transfer_mode_name() == "upload"
    assert legacy_convert.env_default_transfer_mode() == "upload"


def test_transfer_mode_system_over_env(monkeypatch):
    from sherpa import store
    monkeypatch.setenv("SHERPA_OFFICE_TRANSFER_MODE", "path")
    monkeypatch.setattr(store, "get_system_settings", lambda: {"office_transfer_mode": "auto"})
    assert legacy_convert.transfer_mode_name() == "auto"          # 全体設定が env に優先
    assert legacy_convert.env_default_transfer_mode() == "path"   # env_default は system を見ない


def test_transfer_mode_unknown_value_failsafe_to_path(monkeypatch):
    monkeypatch.setattr(legacy_convert, "_warned_unknown_transfer_mode", set())
    monkeypatch.setenv("SHERPA_OFFICE_TRANSFER_MODE", "bogus_mode")
    assert legacy_convert.transfer_mode_name() == "path"


# ---- multipart ボディ組み立て（純関数）----

def test_build_multipart_contains_fields_and_file():
    body, ctype = legacy_convert._build_multipart(
        {"target": "docx", "source_hash": "abc123"}, "file", "旧資料.doc", b"\x00\x01binarydata")
    assert ctype.startswith("multipart/form-data; boundary=")
    boundary = ctype.split("boundary=", 1)[1]
    marker = ("--" + boundary).encode()
    assert body.count(marker) == 4                        # target・source_hash・file の3パート開始＋終端の1（終端は marker で始まるため重複カウント）
    assert b'name="target"' in body and b"\r\n\r\ndocx\r\n" in body
    assert b'name="source_hash"' in body
    assert b'name="file"; filename="' in body
    assert b"\x00\x01binarydata" in body                   # バイナリがそのまま含まれる（改変されない）
    assert body.rstrip(b"\r\n").endswith((marker + b"--"))


# ---- upload 変換（_convert_office_com_upload / convert_to_ooxml）----

def test_convert_upload_mode_sends_file_and_hash(tmp_path, monkeypatch):
    srv, url = _start_mock_worker(upload_status=200, upload_body=_docx_bytes())
    try:
        _use_office_com(monkeypatch, url)
        monkeypatch.setenv("SHERPA_OFFICE_TRANSFER_MODE", "upload")
        src = tmp_path / "旧資料.doc"
        content = b"\xd0\xcf\x11\xe0 old binary"
        src.write_bytes(content)
        data = legacy_convert.convert_to_ooxml(src, ".docx")
        assert data is not None and data[:2] == b"PK"
        assert srv.last_path == "/convert-upload"
        assert srv.last_body is None                       # JSON でなく multipart
        assert srv.last_upload["fields"]["target"] == "docx"
        assert srv.last_upload["fields"]["source_hash"] == hashlib.sha256(content).hexdigest()
        assert srv.last_upload["file"]["bytes"] == content
    finally:
        srv.shutdown(); srv.server_close()


def test_convert_upload_mode_unreadable_source_returns_none(monkeypatch):
    srv, url = _start_mock_worker(upload_status=200, upload_body=_docx_bytes())
    try:
        _use_office_com(monkeypatch, url)
        monkeypatch.setenv("SHERPA_OFFICE_TRANSFER_MODE", "upload")
        src = pathlib.Path("/no/such/dir/旧資料.doc")
        assert legacy_convert.convert_to_ooxml(src, ".docx") is None
        assert srv.convert_calls == 0                       # 読めない原本は送信すらしない
    finally:
        srv.shutdown(); srv.server_close()


def test_upload_retry_succeeds_after_one_failure(tmp_path, monkeypatch):
    """失敗リトライ（1回・冪等）: 初回失敗しても2回目（同じ source_hash 再送）で成功すれば bytes を返す。"""
    srv, url = _start_mock_worker(upload_status=200, upload_body=_docx_bytes())
    srv.upload_fail_first_n = 1
    try:
        _use_office_com(monkeypatch, url)
        monkeypatch.setenv("SHERPA_OFFICE_TRANSFER_MODE", "upload")
        src = tmp_path / "旧資料.doc"
        src.write_bytes(b"\xd0\xcf\x11\xe0 old binary")
        data = legacy_convert.convert_to_ooxml(src, ".docx")
        assert data is not None and data[:2] == b"PK"
        assert srv.convert_calls == 2                        # 1回失敗 + 1回成功
    finally:
        srv.shutdown(); srv.server_close()


def test_upload_retry_exhausted_returns_none(tmp_path, monkeypatch):
    """リトライを使い切っても失敗し続ける場合は None（fail-safe）。試行回数は初回＋1回だけ。"""
    srv, url = _start_mock_worker(upload_status=500)
    try:
        _use_office_com(monkeypatch, url)
        monkeypatch.setenv("SHERPA_OFFICE_TRANSFER_MODE", "upload")
        src = tmp_path / "旧資料.doc"
        src.write_bytes(b"x")
        assert legacy_convert.convert_to_ooxml(src, ".docx") is None
        assert srv.convert_calls == legacy_convert._MAX_UPLOAD_RETRIES + 1
    finally:
        srv.shutdown(); srv.server_close()


# ---- upload レンダ（_render_office_com_upload / render_pdf）----

def test_render_upload_mode_sends_file_and_hash(tmp_path, monkeypatch):
    srv, url = _start_mock_worker(upload_status=200, upload_body=_pdf_bytes())
    try:
        _use_office_com(monkeypatch, url)
        monkeypatch.setenv("SHERPA_OFFICE_TRANSFER_MODE", "upload")
        src = tmp_path / "資料.pptx"
        content = b"PK fake pptx"
        src.write_bytes(content)
        data = legacy_convert.render_pdf(src)
        assert data is not None and data[:4] == b"%PDF"
        assert srv.last_path == "/render-upload"
        assert srv.last_upload["fields"]["source_hash"] == hashlib.sha256(content).hexdigest()
        assert "target" not in srv.last_upload["fields"]     # render に target は無い
    finally:
        srv.shutdown(); srv.server_close()


# ---- auto: path → 失敗/変換不能なら upload へ縮退 ----

def test_auto_mode_falls_back_to_upload_on_path_failure(tmp_path, monkeypatch):
    """path 方式のワーカー応答が失敗（404）なら upload へ縮退する。"""
    srv, url = _start_mock_worker(convert_status=404, upload_status=200, upload_body=_docx_bytes())
    try:
        _use_office_com(monkeypatch, url)
        monkeypatch.setenv("SHERPA_OFFICE_TRANSFER_MODE", "auto")
        monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-24.04")   # path 変換自体は成功させ、サーバ側 404 を踏ませる
        src = tmp_path / "旧資料.doc"
        src.write_bytes(b"\xd0\xcf\x11\xe0 old binary")
        data = legacy_convert.convert_to_ooxml(src, ".docx")
        assert data is not None and data[:2] == b"PK"
        assert srv.last_path == "/convert-upload"            # 最終的に upload 側が使われた
        assert srv.convert_calls == 2                          # path 失敗(404) + upload 成功
    finally:
        srv.shutdown(); srv.server_close()


def test_auto_mode_upload_when_path_unconvertible(tmp_path, monkeypatch):
    """path 変換自体が不能（Windows から見えない）なら、path 方式へは送らず即 upload へ縮退する。"""
    srv, url = _start_mock_worker(upload_status=200, upload_body=_docx_bytes())
    try:
        _use_office_com(monkeypatch, url)
        monkeypatch.setenv("SHERPA_OFFICE_TRANSFER_MODE", "auto")
        monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)   # /mnt でない tmp_path は変換不能
        src = tmp_path / "旧資料.doc"
        src.write_bytes(b"\xd0\xcf\x11\xe0 old binary")
        data = legacy_convert.convert_to_ooxml(src, ".docx")
        assert data is not None and data[:2] == b"PK"
        assert srv.last_path == "/convert-upload"
        assert srv.convert_calls == 1                          # path 側は呼ばれていない（変換不能で即 None）
    finally:
        srv.shutdown(); srv.server_close()


def test_auto_mode_render_falls_back_to_upload(tmp_path, monkeypatch):
    srv, url = _start_mock_worker(convert_status=404, upload_status=200, upload_body=_pdf_bytes())
    try:
        _use_office_com(monkeypatch, url)
        monkeypatch.setenv("SHERPA_OFFICE_TRANSFER_MODE", "auto")
        monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-24.04")
        src = tmp_path / "資料.pptx"
        src.write_bytes(b"PK fake pptx")
        data = legacy_convert.render_pdf(src)
        assert data is not None and data[:4] == b"%PDF"
        assert srv.last_path == "/render-upload"
        assert srv.convert_calls == 2
    finally:
        srv.shutdown(); srv.server_close()


# ---- 補助構造抽出（extract_structure_office_com_upload・OFFICE-WIN-001 ⑤・PowerPoint 限定・試作・未配線）----

def test_extract_structure_upload_sends_file_and_hash_returns_json(tmp_path, monkeypatch):
    # Med是正（フィールド別文字数上限）以降の実際の応答形＝各テキストフィールドに *_truncated フラグが伴う。
    payload = {"worker_version": "1.0", "office_app": "powerpoint", "office_version": "16.0",
              "slide_count": 1, "slides": [{"slide_number": 1, "title": "タイトル", "title_truncated": False,
                                            "body_text": "本文", "body_truncated": False,
                                            "notes": "ノート", "notes_truncated": False, "hidden": False,
                                            "shapes": [{"name": "Rectangle 1", "type": "AutoShape", "z_order": 1,
                                                       "text": "図形テキスト", "text_truncated": False,
                                                       "visible": True}]}]}
    srv, url = _start_mock_worker(convert_status=200, convert_body=json.dumps(payload).encode("utf-8"))
    try:
        _use_office_com(monkeypatch, url)
        src = tmp_path / "資料.pptx"
        content = b"PK fake pptx"
        src.write_bytes(content)
        data = legacy_convert.extract_structure_office_com_upload(src)
        assert data == payload
        assert srv.last_path == "/extract-structure-upload"
        assert srv.last_upload["fields"]["source_hash"] == hashlib.sha256(content).hexdigest()
        assert "target" not in srv.last_upload["fields"]      # extract-structure に target は無い
        assert srv.last_upload["file"]["bytes"] == content
    finally:
        srv.shutdown(); srv.server_close()


def test_extract_structure_upload_accepts_legacy_ppt_extension(tmp_path, monkeypatch):
    payload = {"slides": []}
    srv, url = _start_mock_worker(convert_status=200, convert_body=json.dumps(payload).encode("utf-8"))
    try:
        _use_office_com(monkeypatch, url)
        src = tmp_path / "旧資料.ppt"
        src.write_bytes(b"\xd0\xcf\x11\xe0 old binary")
        data = legacy_convert.extract_structure_office_com_upload(src)
        assert data == payload
    finally:
        srv.shutdown(); srv.server_close()


def test_extract_structure_upload_rejects_non_powerpoint_extension(tmp_path, monkeypatch):
    """PowerPoint 限定の試作（.doc/.docx/.xls/.xlsx は対象外・HTTP を一切呼ばない）。"""
    srv, url = _start_mock_worker(convert_status=200, convert_body=b"{}")
    try:
        _use_office_com(monkeypatch, url)
        src = tmp_path / "資料.docx"
        src.write_bytes(b"PK fake docx")
        data = legacy_convert.extract_structure_office_com_upload(src)
        assert data is None
        assert srv.convert_calls == 0
    finally:
        srv.shutdown(); srv.server_close()


def test_extract_structure_upload_no_url_configured_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv("SHERPA_OFFICE_COM_URL", raising=False)
    src = tmp_path / "資料.pptx"
    src.write_bytes(b"PK fake pptx")
    assert legacy_convert.extract_structure_office_com_upload(src) is None


def test_extract_structure_upload_http_failure_returns_none(tmp_path, monkeypatch):
    srv, url = _start_mock_worker(convert_status=500)
    try:
        _use_office_com(monkeypatch, url)
        src = tmp_path / "資料.pptx"
        src.write_bytes(b"PK fake pptx")
        assert legacy_convert.extract_structure_office_com_upload(src) is None
    finally:
        srv.shutdown(); srv.server_close()


def test_extract_structure_upload_413_too_large_returns_none(tmp_path, monkeypatch):
    """Med是正: ワーカーが応答 JSON 上限超過で 413（office-com-worker.ps1 の STRUCTURE_TOO_LARGE 変換）を
    返しても、`_post_multipart` は他の HTTP エラーと同様に扱い None（fail-safe・部分結果を返さない）。"""
    srv, url = _start_mock_worker(convert_status=413)
    try:
        _use_office_com(monkeypatch, url)
        src = tmp_path / "資料.pptx"
        src.write_bytes(b"PK fake pptx")
        assert legacy_convert.extract_structure_office_com_upload(src) is None
    finally:
        srv.shutdown(); srv.server_close()


def test_extract_structure_upload_invalid_json_response_returns_none(tmp_path, monkeypatch):
    srv, url = _start_mock_worker(convert_status=200, convert_body=b"not json at all")
    try:
        _use_office_com(monkeypatch, url)
        src = tmp_path / "資料.pptx"
        src.write_bytes(b"PK fake pptx")
        assert legacy_convert.extract_structure_office_com_upload(src) is None
    finally:
        srv.shutdown(); srv.server_close()


def test_extract_structure_upload_non_dict_json_response_returns_none(tmp_path, monkeypatch):
    """応答が JSON としては妥当でも dict（object）でなければ None（fail-safe）。"""
    srv, url = _start_mock_worker(convert_status=200, convert_body=b"[1, 2, 3]")
    try:
        _use_office_com(monkeypatch, url)
        src = tmp_path / "資料.pptx"
        src.write_bytes(b"PK fake pptx")
        assert legacy_convert.extract_structure_office_com_upload(src) is None
    finally:
        srv.shutdown(); srv.server_close()


# ---- 接続テスト関数（probe_office_com・管理画面 UI は次スライス＝ここは API/関数レベルまで）----

def test_probe_office_com_success(monkeypatch):
    srv, url = _start_mock_worker()
    try:
        result = legacy_convert.probe_office_com(url)
        assert result["ok"] is True
        assert result["detail"] == "接続OK"
        assert result["versions"]["word"] == "16.0"
    finally:
        srv.shutdown(); srv.server_close()


def test_probe_office_com_token_mismatch(monkeypatch):
    srv, url = _start_mock_worker(token="secret-xyz")
    try:
        result = legacy_convert.probe_office_com(url, token="WRONG")
        assert result["ok"] is False
        assert "認証" in result["detail"]
    finally:
        srv.shutdown(); srv.server_close()


def test_probe_office_com_token_match(monkeypatch):
    srv, url = _start_mock_worker(token="secret-xyz")
    try:
        result = legacy_convert.probe_office_com(url, token="secret-xyz")
        assert result["ok"] is True
    finally:
        srv.shutdown(); srv.server_close()


def test_probe_office_com_empty_url():
    result = legacy_convert.probe_office_com("")
    assert result["ok"] is False
    assert "URL" in result["detail"]


def test_probe_office_com_unreachable():
    result = legacy_convert.probe_office_com(f"http://127.0.0.1:{_free_port()}", timeout=0.5)
    assert result["ok"] is False


# ==== ps1 契約検査（OFFICE-WIN-001・実行はしない・ソーステキストの静的検査のみ）====
#
# 実 Windows/Office が無い環境でも検証できるよう PowerShell を実行せず、エンドポイント文字列・上限・
# ハッシュ検証ロジックの存在を静的に確認する（実 HTTP/COM の振る舞いは実機スモークで別途確認する）。

def _ps1_text() -> str:
    p = pathlib.Path(__file__).resolve().parents[2] / "deploy" / "office-com-worker.ps1"
    return p.read_text(encoding="utf-8-sig")


def _start_worker_ps1_text() -> str:
    p = pathlib.Path(__file__).resolve().parents[2] / "deploy" / "start-office-worker.ps1"
    return p.read_text(encoding="utf-8-sig")


def test_ps1_has_upload_endpoints_and_handlers():
    text = _ps1_text()
    assert '"/convert-upload"' in text
    assert '"/render-upload"' in text
    assert "function Handle-ConvertUpload" in text
    assert "function Handle-RenderUpload" in text


def test_ps1_existing_path_endpoints_unchanged():
    """既存 path 方式（/convert・/render・Handle-Convert・Handle-Render）はそのまま残っていること。"""
    text = _ps1_text()
    assert '"/convert"' in text
    assert '"/render"' in text
    assert "function Handle-Convert(" in text
    assert "function Handle-Render(" in text


def test_ps1_upload_has_max_file_bytes_limit_with_413():
    text = _ps1_text()
    assert "MaxFileBytes" in text
    assert "413" in text


# ---- High/Med-1 是正（レビュー指摘・2026-07-22）: multipart パーサがバイト列ベースに書き換わっていること ----

def test_ps1_multipart_parser_is_byte_based_not_full_body_string():
    """旧実装（body 全体を ISO-8859-1 で string 化してから String.Split で分割）の痕跡が残っていないこと。
    新しい実装はバイト列のまま delimiter を探し（`Find-ByteSequence`）、ファイルパートは `[Array]::Copy` で
    1回だけ切り出す（メモリ倍化を避ける・High 是正）。"""
    text = _ps1_text()
    assert "function Find-ByteSequence" in text
    assert "[Array]::Copy(" in text
    assert "[Array]::IndexOf(" in text
    # 旧実装の痕跡（body 全体を1回で文字列化してから Split する形）が残っていないこと。
    assert ".Split([string[]]@($marker)" not in text
    assert "$enc.GetString($bytes)" not in text        # 引数なし＝全体デコード（新実装はオフセット+長さ付き）
    assert "$enc.GetBytes($partBody)" not in text       # 旧実装のパート再エンコード痕跡


def test_ps1_multipart_parser_requires_crlf_prefixed_delimiter_for_subsequent_parts():
    """Med-1 是正: 2 パート目以降の区切りは "CRLF--boundary"（$delim）のみを見ており、裸の "--boundary" を
    ファイル本体中に見つけても区切りと誤認識しない設計になっていること（$dashBoundary は先頭パートの起点
    探索にのみ使う）。"""
    text = _ps1_text()
    assert '$ascii.GetBytes("--" + $boundary)' in text        # dash-boundary（先頭パート探索専用）
    assert '$ascii.GetBytes("`r`n--" + $boundary)' in text    # delim（CRLF 必須・2パート目以降の区切り）


# ---- Med 是正（RV 2巡目・2026-07-22）: delimiter 一致位置の直後（suffix）を検証すること ----

def test_ps1_multipart_parser_validates_delimiter_suffix_not_just_prefix_match():
    """`Find-ByteSequence` の一致は「一致した位置」だけを返し、その直後が本当に区切りの形（CRLF か "--"）かは
    見ていない。ファイル本文に "\\r\\n--<boundary>X"（X は区切りでない任意のバイト＝boundary token の接頭辞が
    偶然一致しただけ）が含まれる正当な multipart では、これを delimiter と誤認して本文を切り詰めてしまう
    （RV 2巡目 Med）。`Find-BoundaryMarker` が一致位置の直後2バイトを CRLF/`--` で検証し、`Parse-MultipartParts`
    の先頭パート探索・2パート目以降の delimiter 探索の両方がそれを経由していること（生の `Find-ByteSequence`
    を直接使っていないこと）を確認する。"""
    text = _ps1_text()
    assert "function Find-BoundaryMarker" in text
    # suffix 判定（直後2バイトが CRLF か "--"）のロジックが存在すること。
    assert "$bytes[$after] -eq 0x0D -and $bytes[$after + 1] -eq 0x0A" in text
    assert "$bytes[$after] -eq 0x2D -and $bytes[$after + 1] -eq 0x2D" in text
    # Parse-MultipartParts 内の実際の探索呼び出しが Find-BoundaryMarker 経由であること。
    start = text.index("function Parse-MultipartParts(")
    end = text.index("function Get-MultipartFile(")
    parse_fn_text = text[start:end]
    assert "Find-BoundaryMarker $bytes $dashBoundary 0" in parse_fn_text
    assert "Find-BoundaryMarker $bytes $delim $bodyStart" in parse_fn_text
    assert "Find-ByteSequence $bytes $dashBoundary 0" not in parse_fn_text
    assert "Find-ByteSequence $bytes $delim $bodyStart" not in parse_fn_text


# ---- Med 是正（RV 3巡目・2026-07-22）: close-delimiter（"--"）自体の後続も検証すること ----

def test_ps1_multipart_parser_validates_close_delimiter_suffix_too():
    """RV 2巡目の是正は「直後が CRLF か `--`」までしか見ておらず、`--`（close-delimiter 候補）を見た時点で
    即採用していた。ファイル本文に "\\r\\n--<boundary>--X"（X は区切りでない任意のバイト）が含まれる正当な
    multipart では、これを close-delimiter と誤認して本文を切り詰めてしまう（RV 3巡目 Med）。`--` の
    **さらに直後**が CRLF か EOF（バッファ終端）のときだけ close-delimiter として採用する分岐が
    `Find-BoundaryMarker` に存在することを確認する。"""
    text = _ps1_text()
    start = text.index("function Find-BoundaryMarker(")
    end = text.index("function Get-Sha256Hex(")
    fn_text = text[start:end]
    assert "isCloseCandidate" in fn_text
    assert "$afterClose = $after + 2" in fn_text
    assert "$bytes[$afterClose] -eq 0x0D -and $bytes[$afterClose + 1] -eq 0x0A" in fn_text   # close 直後の CRLF
    assert "$afterClose -eq $len" in fn_text                                                  # close 直後の EOF


def test_ps1_upload_early_content_length_rejection_before_reading_body():
    """413 の早期判定（Content-Length ヘッダで読む前に拒否）が Read-BoundedBytes 呼び出しより前に
    テキスト上も先に現れること（読み切ってから判定していないことの静的な裏付け）。"""
    text = _ps1_text()
    early_idx = text.index("$req.ContentLength64 -gt $readCeiling")
    read_idx = text.index("Read-BoundedBytes $req.InputStream $readCeiling")
    assert early_idx < read_idx
    # ストリーム読み取り中の打ち切り（Read-BoundedBytes 内部）も 413 に変換される。
    assert "throw (New-Object System.IO.InvalidDataException" in text
    # パース後にファイル本体そのものの厳密なサイズも再検査する（overhead 余裕分を食い潰すケースの保険）。
    assert "$p.Bytes.Length -gt $maxBytes" in text


def test_ps1_upload_has_source_hash_verification():
    text = _ps1_text()
    assert "Get-Sha256Hex" in text
    assert "ComputeHash" in text
    assert "source_hash mismatch" in text


def test_ps1_upload_reuses_existing_extension_whitelist():
    """新しい許可表を作らず、既存 $script:ExtMap / $script:RenderExtMap を再利用していること。"""
    text = _ps1_text()
    assert text.count("$script:ExtMap[$ext]") >= 2
    assert text.count("$script:RenderExtMap[$ext]") >= 2


def test_ps1_upload_deletes_temp_file_after_processing():
    text = _ps1_text()
    assert text.count("Remove-Item -LiteralPath $tmpIn") >= 2


# ---- Low 是正（レビュー指摘・2026-07-22）: /convert-upload の target は非空必須 ----

def test_ps1_convert_upload_target_validation_rejects_empty_string():
    """旧実装は `if ($target -and $target -ne $map.Target)` で、空文字列 "" は偽＝検査を素通りしていた
    （空 target が暗黙に受理されてしまう）。新実装（Handle-ConvertUpload 内）は常に厳密一致（空文字列も
    不一致として 400 になる）。JSON body の path 方式（Handle-Convert・/convert）はこの Low 指摘のスコープ外
    （既存どおり target 省略を許すオプショナル項目のまま）なのでそちらの `-and` 判定には触れない＝
    Handle-ConvertUpload の関数本文だけを切り出して検査する。"""
    text = _ps1_text()
    start = text.index("function Handle-ConvertUpload(")
    end = text.index("function Handle-RenderUpload(")
    upload_fn_text = text[start:end]
    assert "if ($target -ne $map.Target)" in upload_fn_text
    assert "if ($target -and $target -ne $map.Target)" not in upload_fn_text


def test_start_office_worker_ps1_reads_config_keys_and_launches_worker():
    text = _start_worker_ps1_text()
    for key in ("bind", "port", "token", "max_file_bytes", "timeout_seconds", "temp_dir"):
        assert f'"{key}"' in text
    assert "office-com-worker.ps1" in text


# ---- ps1 契約検査（OFFICE-WIN-001 ⑤・/extract-structure-upload・実行はしない・ソーステキストの静的検査のみ）----

def test_ps1_has_extract_structure_upload_endpoint_and_handler():
    text = _ps1_text()
    assert '"/extract-structure-upload"' in text
    assert "function Handle-ExtractStructureUpload" in text


def test_ps1_extract_structure_reuses_existing_multipart_and_token_infra():
    """新しい multipart パーサ／トークン検査を作らず、既存の Get-MultipartFile・Get-Sha256Hex を再利用していること
    （既存の /convert-upload・/render-upload と同じ受信経路・共有シークレットチェックはリスナー共通で掛かる）。"""
    text = _ps1_text()
    start = text.index("function Handle-ExtractStructureUpload(")
    end = text.index("# ---- W2' 直接呼び出しモード")
    fn_text = text[start:end]
    assert "Get-MultipartFile $req $resp $script:MaxFileBytes" in fn_text
    assert "Get-Sha256Hex $filePart.Bytes" in fn_text
    assert "source_hash mismatch" in fn_text
    assert "Remove-Item -LiteralPath $tmpIn" in fn_text        # 原本は処理後に必ず削除


def test_ps1_extract_structure_is_powerpoint_only():
    """.doc/.xls 等は対象外（ExtractStructureExtMap は powerpoint のみ・既存 ExtMap/RenderExtMap を流用しない
    ＝新しい拡張子表を作るが powerpoint 限定であること）。"""
    text = _ps1_text()
    start = text.index("$script:ExtractStructureExtMap = @{")
    end = text.index("\n}", start)   # 外側ハッシュテーブルの閉じ括弧（行頭の "}"）＝入れ子 @{...} の閉じは無視
    map_text = text[start:end]
    assert '".ppt"' in map_text and '".pptx"' in map_text
    assert '".doc"' not in map_text and '".xls"' not in map_text


def test_ps1_extract_structure_uses_isolated_child_process_and_pid_tracking():
    """既存の隔離子プロセス方式（Invoke-OfficeJob・-ExtractStructureOnce）を再利用し、Convert-PowerPoint と同じ
    「この呼び出しが起こした PowerPoint だけを候補として記録する」pidfile 連携を行っていること
    （タイムアウト時に無関係な既存 PowerPoint インスタンスを誤って kill しない安全策の踏襲）。"""
    text = _ps1_text()
    assert "function Invoke-ExtractStructureOnce" in text
    assert '"-ExtractStructureOnce"' in text
    assert "[switch]$ExtractStructureOnce" in text
    start = text.index("function Get-PowerPointStructure(")
    end = text.index("function Invoke-ExtractStructureOnce")
    fn_text = text[start:end]
    assert "Get-ProcessSnapshot \"POWERPNT\"" in fn_text
    assert "Write-CandidatePidFile $pidFile \"POWERPNT\"" in fn_text


def test_ps1_extract_structure_returns_expected_slide_fields():
    """スライドごとの補助構造に slide_number・title・body_text・notes・hidden・shapes が含まれること
    （タスク仕様：スライド番号・タイトル・本文テキスト・発表者ノート・非表示スライドフラグ・図形）。"""
    text = _ps1_text()
    start = text.index("function Get-PowerPointStructure(")
    end = text.index("function Invoke-ExtractStructureOnce")
    fn_text = text[start:end]
    for key in ("slide_number", "title", "body_text", "notes", "hidden", "shapes"):
        assert key in fn_text, key
    # 図形は種類・z-order・可視性を持つ。
    assert "z_order" in fn_text
    assert "Get-ShapeTypeName" in fn_text
    assert "visible" in fn_text


def test_ps1_extract_structure_writes_json_without_bom():
    """`Set-Content -Encoding UTF8`（既定で BOM を付ける）ではなく `Encoding.UTF8.GetBytes` で書いている
    こと（Write-JsonResponse と同じ BOM 無し方針・WSL 側の JSON パースを汚さない）。"""
    text = _ps1_text()
    start = text.index("function Invoke-ExtractStructureOnce")
    end = text.index("function Invoke-OfficeJobOnce(")
    fn_text = text[start:end]
    assert "[System.Text.Encoding]::UTF8.GetBytes($json)" in fn_text
    assert "Set-Content -LiteralPath $outFile -Value $json -Encoding UTF8" not in fn_text


# ---- ps1 契約検査（Med是正・2026-07-22・構造抽出 JSON の出力上限・実行はしない・ソーステキストの静的検査のみ）----

def test_ps1_extract_structure_has_field_and_json_size_limits_with_documented_defaults():
    """フィールド別文字数上限・応答 JSON 全体のバイト上限・スライド数/図形数上限が定義され、既定値が
    コメントで根拠付けされていること（Med是正: メモリ枯渇・巨大応答対策）。"""
    text = _ps1_text()
    assert "function Get-MaxStructureFieldChars" in text
    assert "function Get-MaxStructureJsonBytes" in text
    assert "function Get-MaxStructureSlides" in text
    assert "function Get-MaxStructureShapesPerSlide" in text
    assert "$script:DefaultMaxStructureFieldChars = 32768" in text
    assert "$script:DefaultMaxStructureJsonBytes = 33554432" in text
    assert "$script:DefaultMaxStructureSlides = 500" in text
    assert "$script:DefaultMaxStructureShapesPerSlide = 1000" in text
    # 既定値の根拠がコメントで説明されていること（数値だけのマジックナンバーにしない）。
    start = text.index("$script:DefaultMaxStructureFieldChars = 32768")
    comment_block = text[max(0, start - 1500):start]
    assert "既定値の根拠" in comment_block
    # env で上書き可能であること（listener→子プロセスは CLI パラメータでなく env 継承で伝わる設計）。
    assert "SHERPA_OFFICE_COM_MAX_STRUCTURE_FIELD_CHARS" in text
    assert "SHERPA_OFFICE_COM_MAX_STRUCTURE_JSON_BYTES" in text
    assert "SHERPA_OFFICE_COM_MAX_STRUCTURE_SLIDES" in text
    assert "SHERPA_OFFICE_COM_MAX_STRUCTURE_SHAPES_PER_SLIDE" in text


def test_ps1_extract_structure_clamps_all_text_fields_with_truncated_flags():
    """タイトル・本文・ノート・図形テキストのすべてに増分的な上限適用（Read-ShapeTextClamped／
    Add-BudgetedText）を行い、対応する *_truncated フラグを JSON へ含めていること（部分的な打ち切り
    だけで済ませず、切り詰めた事実を呼び出し側が判別できるようにする）。"""
    text = _ps1_text()
    start = text.index("function Get-PowerPointStructure(")
    end = text.index("function Invoke-ExtractStructureOnce")
    fn_text = text[start:end]
    # title・shape text は Get-PowerPointStructure 内で直接 Read-ShapeTextClamped を呼ぶ（段落単位の
    # 増分読み取り）。notes は Get-SlideNotesTextClamped 経由（内部で同じ Read-ShapeTextClamped を使う）。
    assert fn_text.count("Read-ShapeTextClamped") >= 2   # title・shape text の2箇所（直接呼び出し）
    assert "Get-SlideNotesTextClamped" in fn_text
    notes_fn_text = _ps1_text()
    notes_start = notes_fn_text.index("function Get-SlideNotesTextClamped(")
    notes_end = notes_fn_text.index("function Get-PowerPointStructure(")
    assert "Read-ShapeTextClamped" in notes_fn_text[notes_start:notes_end]
    # body_text は Add-BudgetedText で予算付き蓄積する。
    assert "Add-BudgetedText" in fn_text
    # 旧実装（一括 `.Text` 取得してから事後に切り詰める Limit-StructureText）の痕跡が残っていないこと。
    assert "Limit-StructureText" not in fn_text
    for key in ("title_truncated", "body_truncated", "notes_truncated", "text_truncated",
               "slides_truncated", "shapes_truncated"):
        assert key in fn_text, key


def test_ps1_extract_structure_read_shape_text_clamped_reads_paragraphs_incrementally():
    """Med是正(a): 図形テキストの読み取りは `.TextFrame.TextRange.Text` の一括取得ではなく
    `Paragraphs()` を段落単位で回し、上限（$maxChars）に達した時点で追加の COM 読み取りを止める
    （`break`）こと。一括取得＋事後切り詰めへの逆行を防ぐ。段落**単体**の読み取りは
    Read-ParagraphChunked（追いMed-1）へ委譲すること。"""
    text = _ps1_text()
    start = text.index("function Read-ShapeTextClamped(")
    end = text.index("function Add-BudgetedText(")
    fn_text = text[start:end]
    assert "Paragraphs()" in fn_text
    assert "break" in fn_text
    assert "Read-ParagraphChunked $para $remaining" in fn_text
    # 一括取得（旧実装の実コード `$tf.TextRange.Text` / `.Title.TextFrame.TextRange.Text` / 段落全体を
    # 一発で読む `$para.Text`）の痕跡が残っていないこと（コメント中の説明文はここでは検査対象にしない・
    # 実コードのみ）。
    code_lines = [ln for ln in fn_text.splitlines() if not ln.strip().startswith("#")]
    code_only = "\n".join(code_lines)
    assert "$tf.TextRange.Text" not in code_only
    assert "TextFrame.TextRange.Text" not in code_only
    assert "$para.Text" not in code_only


def test_ps1_extract_structure_paragraph_chunked_reads_via_characters_not_whole_text():
    """追いMed是正(1): 段落単体が病的に巨大でも一括 `.Text` を読まず、`Characters(Start,Length)` で
    チャンク単位（既定 8,192 文字）に読み、残り予算を使い切ったら追加の COM 読み取りをしない
    （`Characters` 自体が使えない場合も「ここまで読めた分」を返すのみで一括取得へフォールバックしない）。"""
    text = _ps1_text()
    assert "$script:StructureReadChunkChars = 8192" in text
    start = text.index("function Read-ParagraphChunked(")
    end = text.index("function Read-ShapeTextClamped(")
    fn_text = text[start:end]
    assert ".Characters($pos, $chunkLen)" in fn_text
    assert "$script:StructureReadChunkChars" in fn_text
    # 一括 .Text 取得（$para.Text 全体読み）の痕跡が実コードに残っていないこと。
    code_lines = [ln for ln in fn_text.splitlines() if not ln.strip().startswith("#")]
    code_only = "\n".join(code_lines)
    assert "$para.Text" not in code_only
    # 予算（$maxRead）に達したら break すること・Characters() 例外時も一括取得へフォールバックしないこと。
    assert "$sb.Length -ge $maxRead" in fn_text
    assert fn_text.count("break") >= 2   # 予算到達時・Characters() 失敗時の両方


def test_ps1_extract_structure_body_text_accumulation_is_budgeted_not_unbounded():
    """Med是正(a): body_text の材料（$bodyParts 相当）を切り詰め前の生テキストで無制限に貯める旧実装の
    痕跡（配列へ生テキストを += してから join）が残っていないこと。Add-BudgetedText は既に
    Read-ShapeTextClamped で上限適用済みの断片を、予算内でだけ StringBuilder に追記する。"""
    text = _ps1_text()
    start = text.index("function Get-PowerPointStructure(")
    end = text.index("function Invoke-ExtractStructureOnce")
    fn_text = text[start:end]
    assert "$bodyParts" not in fn_text          # 旧実装の無制限配列蓄積が残っていない
    assert "New-Object System.Text.StringBuilder" in fn_text
    assert 'Add-BudgetedText $bodySb $clampedShapeText.Text $maxChars "`n"' in fn_text


def test_ps1_extract_structure_has_slide_and_shape_count_caps():
    """Med是正(b): スライド数・スライドあたり図形数にハード上限があり、超過分は列挙を打ち切る
    （超過分の COM 読み取り自体を避ける）こと。超過は slides_truncated／shapes_truncated で明示する。"""
    text = _ps1_text()
    start = text.index("function Get-PowerPointStructure(")
    end = text.index("function Invoke-ExtractStructureOnce")
    fn_text = text[start:end]
    assert "$slideIdx -gt $maxSlides" in fn_text
    assert "$shapeIdx -gt $maxShapesPerSlide" in fn_text
    # 上限超過を検出したら即座に列挙を打ち切ること（"break"）。
    slide_cap_idx = fn_text.index("$slideIdx -gt $maxSlides")
    assert "break" in fn_text[slide_cap_idx:slide_cap_idx + 80]
    shape_cap_idx = fn_text.index("$shapeIdx -gt $maxShapesPerSlide")
    assert "break" in fn_text[shape_cap_idx:shape_cap_idx + 80]


def test_ps1_extract_structure_budget_includes_per_shape_and_per_slide_overhead():
    """追いMed是正(2): テキストが空の図形・スライドでも JSON 化すれば構造（name/type/z_order/*_truncated
    等のキー・配列保持）自体がメモリ・応答サイズを消費する。500 スライド×1,000 図形（全テキスト無し）でも
    $totalChars が伸びて早期に STRUCTURE_TOO_LARGE が発火するよう、図形1件あたり・スライド1件あたりの
    構造オーバーヘッド見積り文字数を予算へ計上すること。"""
    text = _ps1_text()
    assert "$script:StructureOverheadCharsPerShape = 256" in text
    assert "$script:StructureOverheadCharsPerSlide = 512" in text
    # 既定値の根拠がコメントで説明されていること。
    start = text.index("$script:StructureOverheadCharsPerShape = 256")
    comment_block = text[max(0, start - 1200):start]
    assert "オーバーヘッド" in comment_block
    start2 = text.index("function Get-PowerPointStructure(")
    end2 = text.index("function Invoke-ExtractStructureOnce")
    fn_text = text[start2:end2]
    assert "$script:StructureOverheadCharsPerSlide" in fn_text
    assert "$script:StructureOverheadCharsPerShape" in fn_text
    # 図形ループ（shapes への追加）の中でオーバーヘッドが加算されること（テキスト長だけでないこと）。
    overhead_line_idx = fn_text.index("$totalChars += $s.text.Length + $script:StructureOverheadCharsPerShape")
    assert overhead_line_idx > 0


def test_ps1_extract_structure_checks_running_char_budget_before_full_serialization():
    """Med是正(c): スライドを1枚処理するたびに累計文字数を粗い予算（JSON バイト上限の保守的な文字数換算）と
    照合し、完全な ConvertTo-Json を待たずに STRUCTURE_TOO_LARGE を投げること（Invoke-ExtractStructureOnce
    側の直列化後チェックは最終防衛として別に残る＝このテストはそれより前の一次防衛を確認する）。"""
    text = _ps1_text()
    start = text.index("function Get-PowerPointStructure(")
    end = text.index("function Invoke-ExtractStructureOnce")
    fn_text = text[start:end]
    assert "$charBudget" in fn_text
    assert "$totalChars" in fn_text
    assert "$totalChars -gt $charBudget" in fn_text
    assert "STRUCTURE_TOO_LARGE:" in fn_text
    # 直列化（`| ConvertTo-Json` の実コード呼び出し）はしていない＝完全な JSON 化を待たずに検査している
    # （コメント中の説明文に "ConvertTo-Json" という語が出るのは許容し、実コード呼び出しの有無だけを見る）。
    code_lines = [ln for ln in fn_text.splitlines() if not ln.strip().startswith("#")]
    assert not any("ConvertTo-Json" in ln for ln in code_lines)
    # 予算はスライドループの**内側**（1枚処理するたび）で照合されること。
    loop_idx = fn_text.index("foreach ($slide in $pres.Slides)")
    budget_check_idx = fn_text.index("$totalChars -gt $charBudget")
    assert loop_idx < budget_check_idx


def test_ps1_extract_structure_json_size_check_happens_after_serialization_before_write():
    """直列化（ConvertTo-Json）後・ファイル書き込み前に総バイト数を検査し、超過時は
    ファイルへ何も書かずに throw する（部分結果を成功として書き残さない）。"""
    text = _ps1_text()
    start = text.index("function Invoke-ExtractStructureOnce")
    end = text.index("function Invoke-OfficeJobOnce(")
    fn_text = text[start:end]
    serialize_idx = fn_text.index("ConvertTo-Json -Compress -Depth 10")
    check_idx = fn_text.index("$jsonBytes.Length -gt $maxJsonBytes")
    write_idx = fn_text.index("[System.IO.File]::WriteAllBytes($outFile, $jsonBytes)")
    assert serialize_idx < check_idx < write_idx
    assert "STRUCTURE_TOO_LARGE:" in fn_text


def test_ps1_extract_structure_too_large_becomes_413_not_500():
    """Invoke-ExtractStructureOnce の STRUCTURE_TOO_LARGE 印を Handle-ExtractStructureUpload が検出し、
    汎用エラー（500 系）ではなく 413 として応答すること（部分結果を成功に見せない・容量超過を明確に示す）。"""
    text = _ps1_text()
    start = text.index("function Handle-ExtractStructureUpload(")
    end = text.index("# ---- W2' 直接呼び出しモード")
    fn_text = text[start:end]
    assert 'StartsWith("STRUCTURE_TOO_LARGE:")' in fn_text
    assert "Write-JsonResponse $resp 413 @{ error = $r.Error }" in fn_text


# ---- ps1 契約検査（追いRV Med-2・2026-07-22・env 上限値の検証・実行はしない・ソーステキストの静的検査のみ）----

def test_ps1_structure_limit_env_rejects_non_positive_and_non_integer_values():
    """env 値は TryParse による「正の整数として parse 可能」のみ受理し、無効値（0以下・非整数・parse
    不能）は既定値へフォールバック＋警告ログを出すこと（`-1` → Substring(0,-1) 例外での汎用500化、
    `0` → 全フィールド空文字化、JSON cap 負値 → 常時413、を防ぐ）。"""
    text = _ps1_text()
    assert "function Resolve-StructureLimitEnv" in text
    start = text.index("function Resolve-StructureLimitEnv(")
    end = text.index("function Get-MaxStructureFieldChars")
    fn_text = text[start:end]
    assert "[long]::TryParse(" in fn_text
    assert "$parsed -le 0" in fn_text          # 0 以下は無効（正の整数のみ受理）
    assert "Write-Warning" in fn_text           # 無効値検出時に警告ログを残す
    # TryParse 失敗/0以下の分岐（Write-Warning の直後）でも既定値へフォールバックすること
    # （空文字列の早期 return とは別に、もう1つ "return $defaultValue" が Write-Warning の後に続く）。
    warning_idx = fn_text.index("Write-Warning")
    after_warning = fn_text[warning_idx:]
    assert "return $defaultValue" in after_warning


def test_ps1_structure_limit_getters_all_go_through_env_validation():
    """4つの上限（フィールド文字数・JSON バイト数・スライド数・図形数）すべてが Resolve-StructureLimitEnv
    経由で解決されること（一部だけ検証をすり抜けるルートを残さない）。"""
    text = _ps1_text()
    for fn_name in ("Get-MaxStructureFieldChars", "Get-MaxStructureJsonBytes",
                    "Get-MaxStructureSlides", "Get-MaxStructureShapesPerSlide"):
        start = text.index(f"function {fn_name} {{")
        end = text.index("\n}", start)
        fn_text = text[start:end]
        assert "Resolve-StructureLimitEnv" in fn_text, fn_name
