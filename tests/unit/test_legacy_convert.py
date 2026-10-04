"""旧形式（.doc/.xls/.ppt）変換バックエンドの単体テスト（DB不要）。

- バックエンド解決（system_settings > env > 既定・fail-safe）・soffice 検出・legacy_exts/sig。
- 偽 soffice／偽 powershell／ローカル http.server モックワーカーでの変換・タイムアウト・失敗の fail-safe・
  プロセスグループ kill・キャッシュ・transfer_mode（path/upload/auto）・probe。
- build_derived 統合（旧形式 → OOXML 前段変換 → ①MD化 → provenance に来歴）。
- deploy/office-com-worker.ps1 の契約はソーステキストの静的検査のみ（PowerShell は実行しない）。

tests/unit/conftest.py の autouse が `store.get_system_settings` を空 dict に固定する（system 優先の検証は
各テスト本体で上書きする）。
"""
from __future__ import annotations

import email
import hashlib
import http.server
import io
import json
import os
import pathlib
import socket
import stat
import threading
import time
import zipfile

import pytest

from sherpa.ingest import office_md
from sherpa.ingest.arms import legacy_convert

_DOCX_XML = ('<?xml version="1.0"?>'
             '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
             '<w:body><w:p><w:r><w:t>旧資料の中身テキストXYZ</w:t></w:r></w:p></w:body></w:document>')
OLD = b"\xd0\xcf\x11\xe0 old binary"
ALL_EXTS = {".doc", ".xls", ".ppt"}

_FAKE_SOFFICE = """#!/usr/bin/env bash
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


def _make_template(dirpath: pathlib.Path) -> pathlib.Path:
    tmpl = dirpath / "template.docx"
    with zipfile.ZipFile(tmpl, "w") as z:
        z.writestr("word/document.xml", _DOCX_XML)
    return tmpl


def _exec_script(path: pathlib.Path, body: str) -> pathlib.Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _install_fake_soffice(tmp_path, monkeypatch, *, sleep=None, exit_code=None):
    """偽 soffice を SHERPA_SOFFICE_BIN に設定し、起動回数カウンタ path を返す。"""
    script = _exec_script(tmp_path / "fake_soffice.sh", _FAKE_SOFFICE)
    counter = tmp_path / "counter.txt"
    monkeypatch.setenv("SHERPA_SOFFICE_BIN", str(script))
    monkeypatch.setenv("SHERPA_MCP_LEGACY_BACKEND", "libreoffice")
    monkeypatch.setenv("FAKE_SOFFICE_DOCX", str(_make_template(tmp_path)))
    monkeypatch.setenv("FAKE_SOFFICE_COUNTER", str(counter))
    if sleep is not None:
        monkeypatch.setenv("FAKE_SOFFICE_SLEEP", str(sleep))
    if exit_code is not None:
        monkeypatch.setenv("FAKE_SOFFICE_EXIT", str(exit_code))
    legacy_convert._version_cache.clear()
    return counter


def _count(counter: pathlib.Path) -> int:
    return counter.read_text(encoding="utf-8").count("x") if counter.exists() else 0


def _docx_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", _DOCX_XML)
    return buf.getvalue()


def _pdf_bytes() -> bytes:
    return b"%PDF-1.4\n%mock sherpa render\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def _old_doc(tmp_path, name="旧資料.doc", content=b"x"):
    src = tmp_path / name
    src.write_bytes(content)
    return src


def _assert_grandchild_killed(pid_file: pathlib.Path):
    """タイムアウト後、wrapper が起こした孫プロセス（同一プロセスグループ）が実際に消えている。"""
    deadline = time.monotonic() + 3.0
    pid = None
    while time.monotonic() < deadline and pid is None:
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text(encoding="utf-8").strip())
            except ValueError:
                pass
        if pid is None:
            time.sleep(0.05)
    assert pid is not None, "孫プロセスの pid が取得できなかった（テスト前提が崩れている）"
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError(f"孫プロセス（pid={pid}）が残っている（プロセスグループ kill が効いていない）")


# ---- モックワーカー（office_com http）----

def _parse_multipart(raw: bytes, content_type: str) -> dict:
    msg = email.message_from_bytes(b"Content-Type: " + content_type.encode("ascii") + b"\r\n\r\n" + raw)
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


class _MockWorkerHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
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
        self.server.last_path = self.path
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


def _start_mock_worker(*, token=None, versions=None, convert_status=200, convert_body=b"", delay=0.0,
                       upload_status=None, upload_body=b""):
    srv = http.server.HTTPServer(("127.0.0.1", 0), _MockWorkerHandler)
    srv.token = token
    srv.versions = versions if versions is not None else {"word": "16.0", "excel": "16.0", "powerpoint": "16.0"}
    srv.convert_status, srv.convert_body, srv.delay = convert_status, convert_body, delay
    srv.upload_status, srv.upload_body = upload_status, upload_body
    srv.upload_fail_first_n = 0
    srv.last_body = srv.last_upload = srv.last_path = None
    srv.convert_calls = 0
    threading.Thread(target=srv.serve_forever, args=(0.02,), daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _use_office_com(monkeypatch, url, *, token=None):
    legacy_convert._healthz_cache.clear()
    monkeypatch.setenv("SHERPA_MCP_LEGACY_BACKEND", "office_com")
    monkeypatch.setenv("SHERPA_OFFICE_COM_URL", url)
    if token is not None:
        monkeypatch.setenv("SHERPA_OFFICE_COM_TOKEN", token)
    else:
        monkeypatch.delenv("SHERPA_OFFICE_COM_TOKEN", raising=False)


@pytest.fixture
def worker(monkeypatch):
    """`worker(**kw)` でモックワーカーを起動し office_com http を有効化する（後始末は自動）。"""
    started = []

    def start(*, client_token=None, **kw):
        srv, url = _start_mock_worker(**kw)
        started.append(srv)
        _use_office_com(monkeypatch, url, token=client_token)
        return srv

    yield start
    for srv in started:
        srv.shutdown()
        srv.server_close()


def _install_fake_powershell(tmp_path, monkeypatch):
    """偽 powershell.exe で direct モード（URL 未設定・backend=office_com・distro 設定）にする。"""
    script = _exec_script(tmp_path / "fake_powershell.sh", _FAKE_POWERSHELL)
    pdf = tmp_path / "render.pdf"
    pdf.write_bytes(_pdf_bytes())
    monkeypatch.setenv("SHERPA_POWERSHELL_BIN", str(script))
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-24.04")
    monkeypatch.delenv("SHERPA_OFFICE_COM_URL", raising=False)
    monkeypatch.setenv("SHERPA_MCP_LEGACY_BACKEND", "office_com")
    monkeypatch.setenv("FAKE_PS_DOCX", str(_make_template(tmp_path)))
    monkeypatch.setenv("FAKE_PS_PDF", str(pdf))
    legacy_convert._direct_healthz_cache.clear()
    return script


# ---- バックエンド／転送方式の解決（system_settings > env > 既定・未知値と読込失敗は fail-safe）----
# (env, system_settings, 期待される実効値, env_default の期待値 or None)
BACKEND_CASES = {
    "default_none": (None, {}, "none", "none"),
    "env_over_default": ("libreoffice", {}, "libreoffice", "libreoffice"),
    "system_over_env": ("none", {"legacy_backend": "libreoffice"}, "libreoffice", "none"),
    "unknown_failsafe_none": ("bogus_backend", {}, "none", None),
    "system_unreadable_failsafe_env": ("libreoffice", RuntimeError("no PG creds"), "libreoffice", None),
}


@pytest.mark.parametrize("env,system,expect,env_default", BACKEND_CASES.values(), ids=BACKEND_CASES)
def test_backend_resolution(monkeypatch, env, system, expect, env_default):
    from sherpa import store
    monkeypatch.setattr(legacy_convert, "_warned_unknown_backend", set())
    if env is None:
        monkeypatch.delenv("SHERPA_MCP_LEGACY_BACKEND", raising=False)
    else:
        monkeypatch.setenv("SHERPA_MCP_LEGACY_BACKEND", env)

    def _system():
        if isinstance(system, Exception):
            raise system
        return system
    monkeypatch.setattr(store, "get_system_settings", _system)
    assert legacy_convert.legacy_backend_name() == expect
    if env_default is not None:
        assert legacy_convert.env_default_backend() == env_default     # env_default は system を見ない


TRANSFER_CASES = {
    "default_path": (None, {}, "path", "path"),
    "env_over_default": ("upload", {}, "upload", "upload"),
    "system_over_env": ("path", {"office_transfer_mode": "auto"}, "auto", "path"),
    "unknown_failsafe_path": ("bogus_mode", {}, "path", None),
}


@pytest.mark.parametrize("env,system,expect,env_default", TRANSFER_CASES.values(), ids=TRANSFER_CASES)
def test_transfer_mode_resolution(monkeypatch, env, system, expect, env_default):
    from sherpa import store
    monkeypatch.setattr(legacy_convert, "_warned_unknown_transfer_mode", set())
    if env is None:
        monkeypatch.delenv("SHERPA_OFFICE_TRANSFER_MODE", raising=False)
    else:
        monkeypatch.setenv("SHERPA_OFFICE_TRANSFER_MODE", env)
    monkeypatch.setattr(store, "get_system_settings", lambda: system)
    assert legacy_convert.transfer_mode_name() == expect
    if env_default is not None:
        assert legacy_convert.env_default_transfer_mode() == env_default


# ---- soffice 検出・legacy_exts / legacy_sig_value ----

def test_soffice_detection(tmp_path, monkeypatch):
    monkeypatch.setenv("SHERPA_SOFFICE_BIN", "/no/such/soffice/binary")
    assert legacy_convert.soffice_available() is False and legacy_convert.soffice_version() is None
    not_exec = tmp_path / "soffice.txt"
    not_exec.write_text("not executable", encoding="utf-8")
    monkeypatch.setenv("SHERPA_SOFFICE_BIN", str(not_exec))
    assert legacy_convert.soffice_available() is False                  # X_OK 無し
    _install_fake_soffice(tmp_path, monkeypatch)
    assert legacy_convert.soffice_available() is True
    assert legacy_convert.soffice_version() == "LibreOffice 7.5.0.0 fake"


def test_legacy_exts_and_sig_by_backend(tmp_path, monkeypatch):
    monkeypatch.delenv("SHERPA_MCP_LEGACY_BACKEND", raising=False)
    assert legacy_convert.legacy_exts() == set() and legacy_convert.legacy_sig_value() == "none"
    monkeypatch.setenv("SHERPA_MCP_LEGACY_BACKEND", "libreoffice")
    monkeypatch.setenv("SHERPA_SOFFICE_BIN", "/no/such/soffice")        # backend 選択でも soffice 未検出＝変換不可
    assert legacy_convert.legacy_exts() == set() and legacy_convert.legacy_sig_value() == "none"
    _install_fake_soffice(tmp_path, monkeypatch)
    assert legacy_convert.legacy_exts() == ALL_EXTS
    assert legacy_convert.legacy_sig_value() == "libreoffice"


@pytest.mark.parametrize("env,expect", [("set", {".doc", ".xls"}), ("empty", set()), ("absent", set())])
def test_legacy_exts_env_override(monkeypatch, env, expect):
    """SHERPA_LEGACY_EXTS は設定時のみ最優先（healthz probe を一切しない）・空文字は none・未設定は通常解決。"""
    monkeypatch.delenv("SHERPA_MCP_LEGACY_BACKEND", raising=False)
    if env == "set":
        monkeypatch.setenv("SHERPA_MCP_LEGACY_BACKEND", "office_com")
        monkeypatch.setenv("SHERPA_OFFICE_COM_URL", "http://127.0.0.1:1")

        def _boom():
            raise AssertionError("office_com_healthz が呼ばれた（SHERPA_LEGACY_EXTS が最優先されていない）")
        monkeypatch.setattr(legacy_convert, "office_com_healthz", _boom)
        monkeypatch.setenv("SHERPA_LEGACY_EXTS", ".doc,.xls")
    elif env == "empty":
        monkeypatch.setenv("SHERPA_LEGACY_EXTS", "")
    else:
        monkeypatch.delenv("SHERPA_LEGACY_EXTS", raising=False)
    assert legacy_convert.legacy_exts() == expect


# ---- 変換本体（偽 soffice）----

def test_convert_success_is_markdown_convertible(tmp_path, monkeypatch):
    _install_fake_soffice(tmp_path, monkeypatch)
    data = legacy_convert.convert_to_ooxml(_old_doc(tmp_path, content=b"\xd0\xcf\x11\xe0 old binary"), ".docx")
    assert data is not None and data[:2] == b"PK"
    out = tmp_path / "out.docx"
    out.write_bytes(data)
    md = office_md.to_markdown(out)
    assert md is not None and "旧資料の中身テキストXYZ" in md


def test_convert_timeout_returns_none_and_sets_consumable_reason(tmp_path, monkeypatch):
    """タイムアウトは None＋理由コード `timeout`（読んだら消費）。非0終了は理由を残さない。"""
    counter = _install_fake_soffice(tmp_path, monkeypatch, sleep=2)
    monkeypatch.setenv("SHERPA_LEGACY_TIMEOUT", "0.3")
    src = _old_doc(tmp_path)
    assert legacy_convert.convert_to_ooxml(src, ".docx") is None
    assert _count(counter) == 1
    assert legacy_convert.take_conversion_failure_reason() == "timeout"
    assert legacy_convert.take_conversion_failure_reason() is None


def test_convert_nonzero_exit_and_none_backend_return_none_without_reason(tmp_path, monkeypatch):
    _install_fake_soffice(tmp_path, monkeypatch, exit_code=1)
    src = _old_doc(tmp_path)
    assert legacy_convert.convert_to_ooxml(src, ".docx") is None
    assert legacy_convert.take_conversion_failure_reason() is None
    monkeypatch.delenv("FAKE_SOFFICE_EXIT")
    monkeypatch.setenv("SHERPA_MCP_LEGACY_BACKEND", "none")
    assert legacy_convert.convert_to_ooxml(src, ".docx") is None


def test_ensure_ooxml_clears_stale_conversion_failure_reason_on_cache_hit(tmp_path, monkeypatch):
    _install_fake_soffice(tmp_path, monkeypatch)
    cache_root = tmp_path / "_legacy_cache"
    ok_src = _old_doc(tmp_path, "ok.doc", b"y")
    assert legacy_convert.ensure_ooxml(ok_src, "ok.doc", cache_root) is not None
    monkeypatch.setenv("FAKE_SOFFICE_SLEEP", "2")
    monkeypatch.setenv("SHERPA_LEGACY_TIMEOUT", "0.3")
    assert legacy_convert.ensure_ooxml(_old_doc(tmp_path, "timeout.doc"), "timeout.doc", cache_root) is None
    assert legacy_convert.ensure_ooxml(ok_src, "ok.doc", cache_root) is not None      # 原本不変＝キャッシュ命中
    assert legacy_convert.take_conversion_failure_reason() is None                    # timeout の残留が出ない


def test_convert_timeout_kills_process_group_including_descendants(tmp_path, monkeypatch):
    """soffice の wrapper→soffice.bin 多段起動で、タイムアウト時にプロセスグループごと停止する（残骸化防止）。"""
    pid_file = tmp_path / "child_pid.txt"
    script = _exec_script(
        tmp_path / "fake_soffice_multi.sh",
        '#!/usr/bin/env bash\n'
        'if [ "$1" = "--version" ]; then echo "LibreOffice 7.5.0.0 fake"; exit 0; fi\n'
        'sleep 30 &\necho $! > "$FAKE_SOFFICE_CHILD_PID_FILE"\nsleep 30\n')
    monkeypatch.setenv("SHERPA_SOFFICE_BIN", str(script))
    monkeypatch.setenv("SHERPA_MCP_LEGACY_BACKEND", "libreoffice")
    monkeypatch.setenv("FAKE_SOFFICE_CHILD_PID_FILE", str(pid_file))
    monkeypatch.setenv("SHERPA_LEGACY_TIMEOUT", "0.3")
    legacy_convert._version_cache.clear()
    assert legacy_convert.convert_to_ooxml(_old_doc(tmp_path), ".docx") is None
    _assert_grandchild_killed(pid_file)


def test_build_convert_cmd_percent_encodes_file_uri(tmp_path):
    """`-env:UserInstallation` の file:// URL は `Path.as_uri()` で percent-encode される（実行しない）。"""
    profile = tmp_path / "profile with space"
    profile.mkdir()
    outdir = tmp_path / "out"
    outdir.mkdir()
    cmd = legacy_convert._build_convert_cmd("/usr/bin/soffice", "docx", outdir, profile, _old_doc(tmp_path))
    uri = next(a for a in cmd if a.startswith("-env:UserInstallation=")).split("=", 1)[1]
    assert uri.startswith("file:///") and "%20" in uri and " " not in uri
    assert uri == profile.as_uri()


# ---- キャッシュ（原本 mtime/size キー）・arms_sig・build_derived 統合 ----

def test_ensure_ooxml_cache_hit_and_miss(tmp_path, monkeypatch):
    counter = _install_fake_soffice(tmp_path, monkeypatch)
    src = _old_doc(tmp_path, content=b"\xd0\xcf\x11\xe0 v1")
    cache_root = tmp_path / "_legacy_cache"
    first = legacy_convert.ensure_ooxml(src, "旧資料.doc", cache_root)
    ooxml_path, notes = first
    assert ooxml_path.is_file() and ooxml_path.suffix == ".docx"
    assert "legacy_backend=libreoffice" in notes and any(n.startswith("soffice=") for n in notes)
    assert _count(counter) == 1
    assert legacy_convert.ensure_ooxml(src, "旧資料.doc", cache_root) is not None
    assert _count(counter) == 1                                         # 原本不変＝ヒット
    src.write_bytes(b"\xd0\xcf\x11\xe0 v2 CHANGED longer content")
    assert legacy_convert.ensure_ooxml(src, "旧資料.doc", cache_root) is not None
    assert _count(counter) == 2                                         # 原本変更＝ミス


def test_ensure_ooxml_unsupported_ext_returns_none(tmp_path, monkeypatch):
    _install_fake_soffice(tmp_path, monkeypatch)
    src = tmp_path / "note.txt"
    src.write_text("x", encoding="utf-8")
    assert legacy_convert.ensure_ooxml(src, "note.txt", tmp_path / "_legacy_cache") is None


def test_arms_sig_drift_reacts_to_legacy_backend(tmp_path, monkeypatch):
    monkeypatch.delenv("SHERPA_MCP_ARMS", raising=False)
    d = tmp_path / "derived"
    d.mkdir()
    monkeypatch.setattr(office_md, "_pdf_backend", lambda: None)
    monkeypatch.delenv("SHERPA_MCP_LEGACY_BACKEND", raising=False)
    office_md._write_arms_sig_marker(d)
    assert office_md.arms_sig_drift(d) is False
    _install_fake_soffice(tmp_path, monkeypatch)
    assert office_md.arms_sig_drift(d) is True


def _derived_setup(tmp_path, names):
    src = tmp_path / "src"
    src.mkdir()
    for n in names:
        (src / n).write_bytes(OLD)
    return src, tmp_path / "derived" / "test" / "md"


def test_build_derived_converts_legacy_via_backend(tmp_path, monkeypatch):
    monkeypatch.delenv("SHERPA_MCP_ARMS", raising=False)
    counter = _install_fake_soffice(tmp_path, monkeypatch)
    src, derived = _derived_setup(tmp_path, ["旧資料.doc"])
    rep = office_md.build_derived(src, derived)
    assert rep["converted"] == 1 and rep["failed"] == 0 and rep["unsupported"] == 0
    assert rep["by_ext"] == {".doc": 1}
    assert rep["document_ir_generated"] == 0                           # 旧 .doc は document-ir-v2 を生成しない
    assert rep["evidence_ir_generated"] == 1 and rep["rag_generated"] == 1
    assert rep["office_display_requested"] == 0

    md = derived / "旧資料.doc.md"                                    # 出力名は原本 rel
    assert md.is_file() and "旧資料の中身テキストXYZ" in md.read_text(encoding="utf-8")
    meta = json.loads((derived / "旧資料.doc.md.meta.json").read_text(encoding="utf-8"))
    assert meta["arm"] == "ooxml" and meta["method"] == "ooxml"
    assert "legacy_backend=libreoffice" in meta["notes"] and any(n.startswith("soffice=") for n in meta["notes"])

    assert (derived.parent / "_legacy_cache" / "旧資料.doc.docx").is_file()      # キャッシュは md/ の兄弟
    assert (src / "旧資料.doc").read_bytes() == OLD                              # 原本は不変
    assert _count(counter) == 1
    assert office_md.build_derived(src, derived)["converted"] == 1                # 再ビルドでもキャッシュヒット
    assert _count(counter) == 1


@pytest.mark.parametrize("names,fake,reasons", [
    (["遅い.doc", "遅い2.doc"], {"sleep": 2}, {"遅い.doc": "legacy_conversion_timeout", "遅い2.doc": "legacy_conversion_timeout"}),
    (["壊れた.doc"], {"exit_code": 1}, {"壊れた.doc": "legacy_conversion_failed"}),
], ids=["timeout", "nonzero_exit"])
def test_build_derived_legacy_conversion_failure_reason(tmp_path, monkeypatch, names, fake, reasons):
    """backend が実在するのに失敗＝`failed` 計上（`unsupported` ではない）・理由はタイムアウトと汎用で区別する。"""
    src, derived = _derived_setup(tmp_path, names)
    _install_fake_soffice(tmp_path, monkeypatch, **fake)
    monkeypatch.setenv("SHERPA_LEGACY_TIMEOUT", "0.3")
    rep = office_md.build_derived(src, derived)
    assert rep["unsupported"] == 0 and rep["failed"] == len(names)
    assert {e["doc"]: e["reason"] for e in rep["legacy_conversion_failures"]} == reasons


def test_build_derived_legacy_unsupported_when_backend_none(tmp_path, monkeypatch):
    monkeypatch.delenv("SHERPA_MCP_ARMS", raising=False)
    monkeypatch.delenv("SHERPA_MCP_LEGACY_BACKEND", raising=False)
    monkeypatch.delenv("SHERPA_SOFFICE_BIN", raising=False)
    src, derived = _derived_setup(tmp_path, ["旧資料.doc"])
    rep = office_md.build_derived(src, derived)
    assert rep["unsupported"] == 1 and rep["converted"] == 0
    assert (derived / "旧資料.doc.md").is_file()              # source-level Evidence/coverage notice は検索可能な成果物
    evidence = json.loads((derived.parent / "ir" / "旧資料.doc.evidence.json").read_text(encoding="utf-8"))
    assert evidence["coverage"][0]["reason_code"] == "legacy_backend_unavailable"
    assert "legacy_backend_unavailable" in (derived.parent / "rag" / "旧資料.doc.rag.md").read_text(encoding="utf-8")


# ==== office_com http（ローカル http.server モックワーカー）====

def test_wsl_to_windows_path(monkeypatch):
    f = legacy_convert.wsl_to_windows_path
    assert f("/mnt/c/test/旧資料.doc") == "C:\\test\\旧資料.doc"
    assert f("/mnt/d/取込 5期/決算.xls") == "D:\\取込 5期\\決算.xls"
    assert f("/mnt/c") == "C:\\" and f("/mnt/c/") == "C:\\"
    assert f("relative/x.doc") is None and f("") is None
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-24.04")
    assert f("/home/tudo/資料/旧.doc") == "\\\\wsl.localhost\\Ubuntu-24.04\\home\\tudo\\資料\\旧.doc"
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    assert f("/home/tudo/旧.doc") is None                               # distro 不明は変換不能


def test_office_com_unset_url_unavailable(monkeypatch):
    monkeypatch.setenv("SHERPA_MCP_LEGACY_BACKEND", "office_com")
    monkeypatch.delenv("SHERPA_OFFICE_COM_URL", raising=False)
    legacy_convert._healthz_cache.clear()
    assert legacy_convert.office_com_configured() is False
    assert legacy_convert.office_com_mode() == "unavailable"
    assert legacy_convert.office_com_available() is False and legacy_convert.office_com_healthz() is None


def test_office_com_connection_failure_unavailable(monkeypatch):
    _use_office_com(monkeypatch, f"http://127.0.0.1:{_free_port()}")
    assert legacy_convert.office_com_configured() is True
    assert legacy_convert.office_com_available() is False


def test_office_com_healthz_200(worker):
    worker()
    assert legacy_convert.office_com_available() is True
    hz = legacy_convert.office_com_healthz()
    assert hz["ok"] is True and hz["worker"] == "1" and hz["versions"]["word"] == "16.0"


# アプリ単位でゲートする（Word のみ導入で .xls を候補化しない）。True（バージョン不明）は使える扱い。
VERSION_CASES = {
    "all_apps": ({"word": "16.0", "excel": "16.0", "powerpoint": "16.0"}, ALL_EXTS, "office_com:excel,powerpoint,word"),
    "word_only": ({"word": "16.0", "excel": False, "powerpoint": False}, {".doc"}, "office_com:word"),
    "no_apps": ({"word": False, "excel": False, "powerpoint": False}, set(), "none"),
    "version_unknown_true": ({"word": True, "excel": False, "powerpoint": False}, {".doc"}, "office_com:word"),
}


@pytest.mark.parametrize("versions,exts,sig", VERSION_CASES.values(), ids=VERSION_CASES)
def test_office_com_http_gates_exts_per_app(worker, versions, exts, sig):
    worker(versions=versions)
    assert legacy_convert.office_com_available() is True
    assert legacy_convert.legacy_exts() == exts
    assert legacy_convert.legacy_sig_value() == sig


@pytest.mark.parametrize("server_token,client_token,available", [
    ("secret-xyz", "WRONG", False), ("secret-xyz", "secret-xyz", True)], ids=["mismatch_401", "match"])
def test_office_com_token(worker, server_token, client_token, available):
    worker(token=server_token, client_token=client_token)
    assert legacy_convert.office_com_available() is available
    if not available:
        assert legacy_convert.legacy_exts() == set() and legacy_convert.legacy_sig_value() == "none"


def test_office_com_healthz_cached_short_ttl(monkeypatch):
    srv, url = _start_mock_worker()
    try:
        _use_office_com(monkeypatch, url)
        assert legacy_convert.office_com_available() is True
    finally:
        srv.shutdown()
        srv.server_close()
    assert legacy_convert.office_com_available() is True               # 落ちても TTL 内は直前の True


# ---- 変換（_convert_office_com / convert_to_ooxml）----

def test_convert_office_com_200_returns_bytes_and_sends_windows_path(worker):
    srv = worker(convert_body=_docx_bytes())
    data = legacy_convert.convert_to_ooxml(pathlib.Path("/mnt/c/test/旧資料.doc"), ".docx")
    assert data is not None and data[:2] == b"PK"
    assert srv.last_body == {"path": "C:\\test\\旧資料.doc", "target": "docx"}


@pytest.mark.parametrize("kw,client_token,timeout", [
    ({"token": "secret", "convert_body": b"x"}, "WRONG", None),     # 401
    ({"convert_status": 500}, None, None),                          # 5xx
    ({"convert_body": b"x", "delay": 1.0}, None, "0.3"),            # タイムアウト
], ids=["401", "500", "timeout"])
def test_convert_office_com_failures_return_none(worker, monkeypatch, kw, client_token, timeout):
    worker(client_token=client_token, **kw)
    if timeout:
        monkeypatch.setenv("SHERPA_LEGACY_TIMEOUT", timeout)
    assert legacy_convert.convert_to_ooxml(pathlib.Path("/mnt/c/旧資料.doc"), ".docx") is None


def test_convert_office_com_unconvertible_path_returns_none_without_calling_worker(worker, monkeypatch):
    srv = worker(convert_body=_docx_bytes())
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    assert legacy_convert.convert_to_ooxml(pathlib.Path("/home/tudo/旧資料.doc"), ".docx") is None
    assert srv.convert_calls == 0


# `_convert_office_com_ex` の fallback_worthy: パス変換不能・到達不能・タイムアウト・404 だけが縮退対象。
# 500・401 は真の失敗として伝播（upload へ縮退しない）。
# (server kwargs（None＝誰も listen しない）, client token, timeout, distro 未設定, src, データあり, fallback_worthy)
EX_CASES = {
    "success": ({"convert_body": _docx_bytes()}, None, None, False, "/mnt/c/test/旧資料.doc", True, False),
    "500_not_fallback": ({"convert_status": 500}, None, None, False, "/mnt/c/旧資料.doc", False, False),
    "404_fallback": ({"convert_status": 404}, None, None, False, "/mnt/c/旧資料.doc", False, True),
    "401_not_fallback": ({"token": "secret", "convert_body": b"x"}, "WRONG", None, False, "/mnt/c/旧資料.doc", False, False),
    "network_unreachable_fallback": (None, None, None, False, "/mnt/c/旧資料.doc", False, True),
    "timeout_fallback": ({"convert_body": b"x", "delay": 1.0}, None, "0.3", False, "/mnt/c/旧資料.doc", False, True),
    "unmappable_path_fallback": ({"convert_body": b"x"}, None, None, True, "/home/tudo/旧資料.doc", False, True),
}


@pytest.mark.parametrize("kw,client_token,timeout,no_distro,src,has_data,fallback", EX_CASES.values(), ids=EX_CASES)
def test_convert_office_com_ex_fallback_worthy(worker, monkeypatch, kw, client_token, timeout, no_distro, src,
                                               has_data, fallback):
    srv = worker(client_token=client_token, **kw) if kw is not None else None
    if kw is None:
        _use_office_com(monkeypatch, f"http://127.0.0.1:{_free_port()}")
    if timeout:
        monkeypatch.setenv("SHERPA_LEGACY_TIMEOUT", timeout)
    if no_distro:
        monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    data, fallback_worthy = legacy_convert._convert_office_com_ex(pathlib.Path(src), ".docx")
    assert (data is not None and data[:2] == b"PK") if has_data else data is None
    assert fallback_worthy is fallback
    if timeout:
        assert legacy_convert.take_conversion_failure_reason() == "timeout"     # http のタイムアウトも通知する
    if no_distro:
        assert srv.convert_calls == 0                                            # 送らずに判定できている


def test_transfer_mode_auto_clears_timeout_reason_on_upload_fallback_success(monkeypatch):
    """path 方式がタイムアウトしても、auto の upload 縮退が成功すれば前段の失敗理由を残さない。"""
    monkeypatch.setenv("SHERPA_OFFICE_TRANSFER_MODE", "auto")

    def _fake_ex(src, target_ext):
        legacy_convert._note_conversion_failure_reason("timeout")
        return None, True
    monkeypatch.setattr(legacy_convert, "_convert_office_com_ex", _fake_ex)
    monkeypatch.setattr(legacy_convert, "_convert_office_com_upload", lambda src, target_ext: b"PK\x03\x04upload-ok")
    assert legacy_convert._convert_office_com_via_transfer_mode(pathlib.Path("/mnt/c/旧資料.doc"), ".docx") == b"PK\x03\x04upload-ok"
    assert legacy_convert.take_conversion_failure_reason() is None


def test_ensure_ooxml_office_com_caches_and_notes(tmp_path, worker, monkeypatch):
    srv = worker(convert_body=_docx_bytes(), versions={"word": "16.0", "excel": False, "powerpoint": "16.0"})
    src = _old_doc(tmp_path, content=OLD)
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-24.04")             # tmp を \\wsl.localhost へ変換可能に
    cache_root = tmp_path / "_legacy_cache"
    ooxml_path, notes = legacy_convert.ensure_ooxml(src, "旧資料.doc", cache_root)
    assert ooxml_path.is_file() and ooxml_path.suffix == ".docx" and ooxml_path.read_bytes()[:2] == b"PK"
    assert "legacy_backend=office_com" in notes
    assert "office_com_versions=word=16.0,powerpoint=16.0" in notes   # 検出できた Office だけ
    assert srv.convert_calls == 1
    assert legacy_convert.ensure_ooxml(src, "旧資料.doc", cache_root) is not None
    assert srv.convert_calls == 1                                      # ヒット
    md = office_md.to_markdown(ooxml_path)
    assert md is not None and "旧資料の中身テキストXYZ" in md


# ==== office_com direct（偽 powershell.exe で ps1 の -Healthz / -DirectJob をエミュレート）====

def test_office_com_mode_resolution_and_powershell_detection(tmp_path, monkeypatch):
    monkeypatch.delenv("SHERPA_OFFICE_COM_URL", raising=False)
    monkeypatch.setenv("SHERPA_POWERSHELL_BIN", "/no/such/powershell.exe")
    assert legacy_convert.office_com_mode() == "unavailable"
    assert legacy_convert.powershell_available() is False and legacy_convert._powershell_bin() is None
    not_exec = tmp_path / "powershell.txt"
    not_exec.write_text("not executable", encoding="utf-8")
    monkeypatch.setenv("SHERPA_POWERSHELL_BIN", str(not_exec))
    assert legacy_convert._powershell_bin() is None                      # X_OK 無し
    _install_fake_powershell(tmp_path, monkeypatch)
    assert legacy_convert.office_com_mode() == "direct" and legacy_convert.powershell_available() is True
    monkeypatch.setenv("SHERPA_OFFICE_COM_URL", "http://127.0.0.1:9")    # URL 設定時は http（別ホスト優先）
    assert legacy_convert.office_com_mode() == "http"


def test_direct_healthz_gates_and_is_cached(tmp_path, monkeypatch):
    _install_fake_powershell(tmp_path, monkeypatch)
    assert legacy_convert.office_com_available() is True
    hz = legacy_convert.office_com_healthz()
    assert hz["ok"] is True and hz["worker"] == "direct" and hz["versions"]["word"] == "16.0"
    assert legacy_convert.legacy_exts() == ALL_EXTS
    assert legacy_convert.legacy_sig_value() == "office_com:excel,powerpoint,word"
    assert legacy_convert.office_com_healthz() is hz                     # 長め TTL キャッシュ（同一 dict＝再プローブなし）


def test_direct_healthz_partial_apps_gates_per_app(tmp_path, monkeypatch):
    _install_fake_powershell(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_PS_HEALTHZ",
                       '{"ok":true,"versions":{"word":"16.0","excel":false,"powerpoint":false},"worker":"direct"}')
    assert legacy_convert.legacy_exts() == {".doc"}
    assert legacy_convert.legacy_sig_value() == "office_com:word"


def test_convert_office_com_direct_success_is_markdown_convertible(tmp_path, monkeypatch):
    _install_fake_powershell(tmp_path, monkeypatch)
    data = legacy_convert.convert_to_ooxml(_old_doc(tmp_path, content=OLD), ".docx")
    assert data is not None and data[:2] == b"PK"
    out = tmp_path / "out.docx"
    out.write_bytes(data)
    md = office_md.to_markdown(out)
    assert md is not None and "旧資料の中身テキストXYZ" in md


@pytest.mark.parametrize("env", [{"FAKE_PS_EXIT": "1"}, {"WSL_DISTRO_NAME": None}], ids=["ps1_nonzero_exit", "unconvertible_path"])
def test_convert_office_com_direct_failure_returns_none(tmp_path, monkeypatch, env):
    _install_fake_powershell(tmp_path, monkeypatch)
    for k, v in env.items():
        monkeypatch.delenv(k, raising=False) if v is None else monkeypatch.setenv(k, v)
    assert legacy_convert.convert_to_ooxml(_old_doc(tmp_path), ".docx") is None


def test_convert_office_com_direct_timeout_kills_process_group(tmp_path, monkeypatch):
    """backstop タイムアウト（整数秒 + `_DIRECT_GRACE_SEC`）で偽 powershell とその孫が kill され、理由 `timeout` を通知する。"""
    _install_fake_powershell(tmp_path, monkeypatch)
    pid_file = tmp_path / "child_pid.txt"
    monkeypatch.setenv("FAKE_PS_CHILD_PID_FILE", str(pid_file))
    monkeypatch.setenv("FAKE_PS_SLEEP", "30")
    monkeypatch.setenv("SHERPA_LEGACY_TIMEOUT", "0.3")
    monkeypatch.setattr(legacy_convert, "_DIRECT_GRACE_SEC", 0.2)
    assert legacy_convert.convert_to_ooxml(_old_doc(tmp_path), ".docx") is None
    assert legacy_convert.take_conversion_failure_reason() == "timeout"
    _assert_grandchild_killed(pid_file)


@pytest.mark.parametrize("config,passed", [("0.3", "1"), ("5", "5")], ids=["sub_second_ceils_to_1", "whole_second_passthrough"])
def test_direct_job_timeout_sec_never_zero(tmp_path, monkeypatch, config, passed):
    """`-JobTimeoutSec` に 0 を渡さない（0 だと ps1 が既定 120 秒へ倒れ、WSL 側 backstop が先に kill して Office が孤児化する）。"""
    _install_fake_powershell(tmp_path, monkeypatch)
    capture = tmp_path / "captured_timeout.txt"
    monkeypatch.setenv("FAKE_PS_TIMEOUT_CAPTURE", str(capture))
    monkeypatch.setenv("SHERPA_LEGACY_TIMEOUT", config)
    assert legacy_convert.convert_to_ooxml(_old_doc(tmp_path, content=OLD), ".docx") is not None
    assert capture.read_text(encoding="utf-8").strip() == passed


# ---- 忠実 PDF レンダ（render_pdf）----

def test_render_pdf_direct_success_and_unsupported_ext(tmp_path, monkeypatch):
    _install_fake_powershell(tmp_path, monkeypatch)
    data = legacy_convert.render_pdf(_old_doc(tmp_path, "資料.docx", b"PK fake docx"))
    assert data is not None and data[:4] == b"%PDF"
    assert legacy_convert.render_pdf(_old_doc(tmp_path, "memo.txt", b"x")) is None


def test_render_pdf_http_uses_render_endpoint(worker):
    srv = worker(convert_body=_pdf_bytes())
    data = legacy_convert.render_pdf(pathlib.Path("/mnt/c/test/資料.pptx"))
    assert data is not None and data[:4] == b"%PDF"
    assert srv.last_path == "/render" and srv.last_body == {"path": "C:\\test\\資料.pptx"}


def test_render_pdf_unavailable_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv("SHERPA_OFFICE_COM_URL", raising=False)
    monkeypatch.setenv("SHERPA_POWERSHELL_BIN", "/no/such/powershell.exe")
    assert legacy_convert.render_pdf(_old_doc(tmp_path, "資料.docx", b"PK fake docx")) is None


# ---- キャッシュキーが office_com の動作形態（http/direct）切替に反応する ----

def test_source_key_changes_with_office_com_mode(tmp_path, monkeypatch):
    _install_fake_powershell(tmp_path, monkeypatch)
    src = _old_doc(tmp_path)
    assert legacy_convert.office_com_mode() == "direct"
    key_direct = legacy_convert._source_key(src)
    assert key_direct.startswith("office_com:direct:")
    monkeypatch.setenv("SHERPA_OFFICE_COM_URL", "http://127.0.0.1:1")
    assert legacy_convert.office_com_mode() == "http"
    key_http = legacy_convert._source_key(src)
    assert key_http.startswith("office_com:http:") and key_direct != key_http
    monkeypatch.setenv("SHERPA_MCP_LEGACY_BACKEND", "libreoffice")        # 他バックエンドはモード欄が空で安定
    assert legacy_convert._source_key(src).startswith("libreoffice::")
    monkeypatch.setenv("SHERPA_MCP_LEGACY_BACKEND", "none")
    assert legacy_convert._source_key(src).startswith("none::")


def test_ensure_ooxml_cache_miss_on_http_to_direct_mode_switch(tmp_path, monkeypatch):
    """http→direct に切り替わると実際の変換元が変わるためキャッシュをヒットさせず再変換する（スロットは1つ）。"""
    _install_fake_powershell(tmp_path, monkeypatch)
    direct_counter = tmp_path / "direct_counter.txt"
    monkeypatch.setenv("FAKE_PS_COUNTER", str(direct_counter))
    legacy_convert._healthz_cache.clear()
    srv, url = _start_mock_worker(convert_body=_docx_bytes())
    try:
        src = _old_doc(tmp_path, content=OLD)
        cache_root = tmp_path / "_legacy_cache"

        monkeypatch.setenv("SHERPA_OFFICE_COM_URL", url)                  # 1) http で変換
        assert legacy_convert.ensure_ooxml(src, "旧資料.doc", cache_root) is not None
        assert srv.convert_calls == 1 and _count(direct_counter) == 0

        monkeypatch.delenv("SHERPA_OFFICE_COM_URL", raising=False)        # 2) direct へ切替＝ミス
        assert legacy_convert.office_com_mode() == "direct"
        assert legacy_convert.ensure_ooxml(src, "旧資料.doc", cache_root) is not None
        assert srv.convert_calls == 1 and _count(direct_counter) == 1

        monkeypatch.setenv("SHERPA_OFFICE_COM_URL", url)                  # 3) http へ戻す＝またミス
        assert legacy_convert.ensure_ooxml(src, "旧資料.doc", cache_root) is not None
        assert srv.convert_calls == 2 and _count(direct_counter) == 1

        assert legacy_convert.ensure_ooxml(src, "旧資料.doc", cache_root) is not None   # 4) 安定状態＝ヒット
        assert srv.convert_calls == 2 and _count(direct_counter) == 1
    finally:
        srv.shutdown()
        srv.server_close()


# ==== transfer_mode（path/upload/auto）・multipart・失敗リトライ ====

def test_build_multipart_contains_fields_and_file():
    body, ctype = legacy_convert._build_multipart(
        {"target": "docx", "source_hash": "abc123"}, "file", "旧資料.doc", b"\x00\x01binarydata")
    assert ctype.startswith("multipart/form-data; boundary=")
    marker = ("--" + ctype.split("boundary=", 1)[1]).encode()
    assert body.count(marker) == 4                         # 3パート開始＋終端
    assert b'name="target"' in body and b"\r\n\r\ndocx\r\n" in body and b'name="source_hash"' in body
    assert b'name="file"; filename="' in body
    assert b"\x00\x01binarydata" in body                    # バイナリは改変されない
    assert body.rstrip(b"\r\n").endswith(marker + b"--")


def test_convert_upload_mode_sends_file_and_hash(tmp_path, worker, monkeypatch):
    srv = worker(upload_status=200, upload_body=_docx_bytes())
    monkeypatch.setenv("SHERPA_OFFICE_TRANSFER_MODE", "upload")
    src = _old_doc(tmp_path, content=OLD)
    data = legacy_convert.convert_to_ooxml(src, ".docx")
    assert data is not None and data[:2] == b"PK"
    assert srv.last_path == "/convert-upload" and srv.last_body is None
    assert srv.last_upload["fields"]["target"] == "docx"
    assert srv.last_upload["fields"]["source_hash"] == hashlib.sha256(OLD).hexdigest()
    assert srv.last_upload["file"]["bytes"] == OLD


def test_convert_upload_mode_unreadable_source_returns_none_without_sending(worker, monkeypatch):
    srv = worker(upload_status=200, upload_body=_docx_bytes())
    monkeypatch.setenv("SHERPA_OFFICE_TRANSFER_MODE", "upload")
    assert legacy_convert.convert_to_ooxml(pathlib.Path("/no/such/dir/旧資料.doc"), ".docx") is None
    assert srv.convert_calls == 0


@pytest.mark.parametrize("fail_first,status,expect_data,calls", [
    (1, 200, True, 2),                                           # 初回失敗→2回目（同じ source_hash 再送）で成功
    (0, 500, False, "retries+1"),                                # 使い切っても失敗＝None（初回＋リトライだけ）
], ids=["retry_succeeds", "retry_exhausted"])
def test_upload_retry(tmp_path, worker, monkeypatch, fail_first, status, expect_data, calls):
    srv = worker(upload_status=status, upload_body=_docx_bytes())
    srv.upload_fail_first_n = fail_first
    monkeypatch.setenv("SHERPA_OFFICE_TRANSFER_MODE", "upload")
    data = legacy_convert.convert_to_ooxml(_old_doc(tmp_path), ".docx")
    assert (data is not None and data[:2] == b"PK") if expect_data else data is None
    assert srv.convert_calls == (legacy_convert._MAX_UPLOAD_RETRIES + 1 if calls == "retries+1" else calls)


def test_render_upload_mode_sends_file_and_hash(tmp_path, worker, monkeypatch):
    srv = worker(upload_status=200, upload_body=_pdf_bytes())
    monkeypatch.setenv("SHERPA_OFFICE_TRANSFER_MODE", "upload")
    data = legacy_convert.render_pdf(_old_doc(tmp_path, "資料.pptx", b"PK fake pptx"))
    assert data is not None and data[:4] == b"%PDF"
    assert srv.last_path == "/render-upload"
    assert srv.last_upload["fields"]["source_hash"] == hashlib.sha256(b"PK fake pptx").hexdigest()
    assert "target" not in srv.last_upload["fields"]


# auto: path が 404／変換不能なら upload へ縮退。500（真の COM 失敗）は縮退せず失敗のまま。
# (convert|render, path 側ステータス, distro あり, 期待データ, 最後に呼ばれた path, 呼び出し回数)
AUTO_CASES = {
    "convert_404_falls_back": ("convert", 404, True, True, "/convert-upload", 2),
    "convert_path_unconvertible_goes_upload": ("convert", 200, False, True, "/convert-upload", 1),
    "render_404_falls_back": ("render", 404, True, True, "/render-upload", 2),
    "convert_500_does_not_fall_back": ("convert", 500, True, False, "/convert", 1),
    "render_500_does_not_fall_back": ("render", 500, True, False, "/render", 1),
}


@pytest.mark.parametrize("op,status,distro,ok,last_path,calls", AUTO_CASES.values(), ids=AUTO_CASES)
def test_auto_mode_fallback(tmp_path, worker, monkeypatch, op, status, distro, ok, last_path, calls):
    body = _docx_bytes() if op == "convert" else _pdf_bytes()
    srv = worker(convert_status=status, upload_status=200, upload_body=body)
    monkeypatch.setenv("SHERPA_OFFICE_TRANSFER_MODE", "auto")
    if distro:
        monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-24.04")
    else:
        monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    if op == "convert":
        data = legacy_convert.convert_to_ooxml(_old_doc(tmp_path, content=OLD), ".docx")
        magic = b"PK"
    else:
        data = legacy_convert.render_pdf(_old_doc(tmp_path, "資料.pptx", b"PK fake pptx"))
        magic = b"%PDF"
    assert (data is not None and data.startswith(magic)) if ok else data is None
    assert srv.last_path == last_path and srv.convert_calls == calls


# ---- 補助構造抽出（extract_structure_office_com_upload・PowerPoint 限定・試作）----

def test_extract_structure_upload_sends_file_and_hash_returns_json(tmp_path, worker):
    payload = {"worker_version": "1.0", "office_app": "powerpoint", "office_version": "16.0",
               "slide_count": 1, "slides": [{"slide_number": 1, "title": "タイトル", "title_truncated": False,
                                             "body_text": "本文", "body_truncated": False,
                                             "notes": "ノート", "notes_truncated": False, "hidden": False,
                                             "shapes": [{"name": "Rectangle 1", "type": "AutoShape", "z_order": 1,
                                                         "text": "図形テキスト", "text_truncated": False,
                                                         "visible": True}]}]}
    srv = worker(convert_status=200, convert_body=json.dumps(payload).encode("utf-8"))
    content = b"PK fake pptx"
    assert legacy_convert.extract_structure_office_com_upload(_old_doc(tmp_path, "資料.pptx", content)) == payload
    assert srv.last_path == "/extract-structure-upload"
    assert srv.last_upload["fields"]["source_hash"] == hashlib.sha256(content).hexdigest()
    assert "target" not in srv.last_upload["fields"] and srv.last_upload["file"]["bytes"] == content


def test_extract_structure_upload_accepts_legacy_ppt_extension(tmp_path, worker):
    worker(convert_status=200, convert_body=b'{"slides": []}')
    assert legacy_convert.extract_structure_office_com_upload(_old_doc(tmp_path, "旧資料.ppt", OLD)) == {"slides": []}


def test_extract_structure_upload_rejects_non_powerpoint_extension_without_http(tmp_path, worker):
    srv = worker(convert_status=200, convert_body=b"{}")
    assert legacy_convert.extract_structure_office_com_upload(_old_doc(tmp_path, "資料.docx", b"PK fake docx")) is None
    assert srv.convert_calls == 0


def test_extract_structure_upload_no_url_configured_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv("SHERPA_OFFICE_COM_URL", raising=False)
    assert legacy_convert.extract_structure_office_com_upload(_old_doc(tmp_path, "資料.pptx", b"PK")) is None


@pytest.mark.parametrize("kw", [
    {"convert_status": 500},
    {"convert_status": 413},                                    # 応答 JSON 上限超過＝部分結果を返さない
    {"convert_status": 200, "convert_body": b"not json at all"},
    {"convert_status": 200, "convert_body": b"[1, 2, 3]"},      # JSON でも dict でなければ None
], ids=["500", "413_too_large", "invalid_json", "non_dict_json"])
def test_extract_structure_upload_failures_return_none(tmp_path, worker, kw):
    worker(**kw)
    assert legacy_convert.extract_structure_office_com_upload(_old_doc(tmp_path, "資料.pptx", b"PK fake pptx")) is None


# ---- 接続テスト関数（probe_office_com）----

def test_probe_office_com():
    srv, url = _start_mock_worker()
    srv2, url2 = _start_mock_worker(token="secret-xyz")
    try:
        ok = legacy_convert.probe_office_com(url)
        assert ok["ok"] is True and ok["detail"] == "接続OK" and ok["versions"]["word"] == "16.0"
        bad = legacy_convert.probe_office_com(url2, token="WRONG")
        assert bad["ok"] is False and "認証" in bad["detail"]
        assert legacy_convert.probe_office_com(url2, token="secret-xyz")["ok"] is True
    finally:
        for s in (srv, srv2):
            s.shutdown()
            s.server_close()
    empty = legacy_convert.probe_office_com("")
    assert empty["ok"] is False and "URL" in empty["detail"]
    assert legacy_convert.probe_office_com(f"http://127.0.0.1:{_free_port()}", timeout=0.5)["ok"] is False


# ==== ps1 契約検査（deploy/office-com-worker.ps1・実行せずソーステキストの静的検査のみ）====

_DEPLOY = pathlib.Path(__file__).resolve().parents[2] / "deploy"


@pytest.fixture(scope="module")
def ps1() -> str:
    return (_DEPLOY / "office-com-worker.ps1").read_text(encoding="utf-8-sig")


def _scope(text, start, end=None):
    """`start` から `end`（無ければ末尾）までの本文を切り出す。"""
    s = text.index(start)
    return text[s:text.index(end, s) if end else len(text)]


def _code(fn_text):
    return "\n".join(ln for ln in fn_text.splitlines() if not ln.strip().startswith("#"))


STRUCT = ("function Get-PowerPointStructure(", "function Invoke-ExtractStructureOnce")
EXTRACT_HANDLER = ("function Handle-ExtractStructureUpload(", "# ---- W2' 直接呼び出しモード")
# id -> (scope(start, end)|None, 含むべき文字列, 含まないべき文字列, 実コード（コメント除く）に含まれてはならない文字列)
PS1_CASES = {
    "upload_endpoints_and_handlers": (None, ['"/convert-upload"', '"/render-upload"', "function Handle-ConvertUpload",
                                             "function Handle-RenderUpload"], [], []),
    "existing_path_endpoints_unchanged": (None, ['"/convert"', '"/render"', "function Handle-Convert(",
                                                 "function Handle-Render("], [], []),
    "upload_max_file_bytes_with_413": (None, ["MaxFileBytes", "413"], [], []),
    "multipart_parser_is_byte_based": (None, ["function Find-ByteSequence", "[Array]::Copy(", "[Array]::IndexOf("],
                                       [".Split([string[]]@($marker)", "$enc.GetString($bytes)", "$enc.GetBytes($partBody)"], []),
    "multipart_requires_crlf_prefixed_delimiter": (None, ['$ascii.GetBytes("--" + $boundary)',
                                                          '$ascii.GetBytes("`r`n--" + $boundary)'], [], []),
    "multipart_validates_delimiter_suffix": (
        ("function Parse-MultipartParts(", "function Get-MultipartFile("),
        ["Find-BoundaryMarker $bytes $dashBoundary 0", "Find-BoundaryMarker $bytes $delim $bodyStart"],
        ["Find-ByteSequence $bytes $dashBoundary 0", "Find-ByteSequence $bytes $delim $bodyStart"], []),
    "boundary_marker_checks_after_bytes": (
        None, ["function Find-BoundaryMarker", "$bytes[$after] -eq 0x0D -and $bytes[$after + 1] -eq 0x0A",
               "$bytes[$after] -eq 0x2D -and $bytes[$after + 1] -eq 0x2D"], [], []),
    "boundary_marker_validates_close_delimiter_suffix": (
        ("function Find-BoundaryMarker(", "function Get-Sha256Hex("),
        ["isCloseCandidate", "$afterClose = $after + 2", "$bytes[$afterClose] -eq 0x0D -and $bytes[$afterClose + 1] -eq 0x0A",
         "$afterClose -eq $len"], [], []),
    "upload_late_size_checks": (None, ["throw (New-Object System.IO.InvalidDataException", "$p.Bytes.Length -gt $maxBytes"], [], []),
    "upload_source_hash_verification": (None, ["Get-Sha256Hex", "ComputeHash", "source_hash mismatch"], [], []),
    "convert_upload_target_must_match_exactly": (
        ("function Handle-ConvertUpload(", "function Handle-RenderUpload("), ["if ($target -ne $map.Target)"],
        ["if ($target -and $target -ne $map.Target)"], []),
    "start_worker_ps1_reads_config_keys": ("START", ['"bind"', '"port"', '"token"', '"max_file_bytes"', '"timeout_seconds"',
                                                    '"temp_dir"', "office-com-worker.ps1"], [], []),
    "extract_structure_endpoint_and_handler": (None, ['"/extract-structure-upload"', "function Handle-ExtractStructureUpload"], [], []),
    "extract_structure_reuses_multipart_and_token_infra": (
        EXTRACT_HANDLER, ["Get-MultipartFile $req $resp $script:MaxFileBytes", "Get-Sha256Hex $filePart.Bytes",
                          "source_hash mismatch", "Remove-Item -LiteralPath $tmpIn"], [], []),
    "extract_structure_isolated_child_and_pid_tracking": (
        None, ["function Invoke-ExtractStructureOnce", '"-ExtractStructureOnce"', "[switch]$ExtractStructureOnce"], [], []),
    "extract_structure_pid_tracking_in_powerpoint_fn": (
        STRUCT, ['Get-ProcessSnapshot "POWERPNT"', 'Write-CandidatePidFile $pidFile "POWERPNT"'], [], []),
    "extract_structure_slide_fields": (
        STRUCT, ["slide_number", "title", "body_text", "notes", "hidden", "shapes", "z_order", "Get-ShapeTypeName", "visible"], [], []),
    "extract_structure_json_written_without_bom": (
        ("function Invoke-ExtractStructureOnce", "function Invoke-OfficeJobOnce("),
        ["[System.Text.Encoding]::UTF8.GetBytes($json)"], ["Set-Content -LiteralPath $outFile -Value $json -Encoding UTF8"], []),
    "extract_structure_limit_getters_and_env": (
        None, ["function Get-MaxStructureFieldChars", "function Get-MaxStructureJsonBytes", "function Get-MaxStructureSlides",
               "function Get-MaxStructureShapesPerSlide", "$script:DefaultMaxStructureFieldChars = 32768",
               "$script:DefaultMaxStructureJsonBytes = 33554432", "$script:DefaultMaxStructureSlides = 500",
               "$script:DefaultMaxStructureShapesPerSlide = 1000", "SHERPA_OFFICE_COM_MAX_STRUCTURE_FIELD_CHARS",
               "SHERPA_OFFICE_COM_MAX_STRUCTURE_JSON_BYTES", "SHERPA_OFFICE_COM_MAX_STRUCTURE_SLIDES",
               "SHERPA_OFFICE_COM_MAX_STRUCTURE_SHAPES_PER_SLIDE"], [], []),
    "extract_structure_clamps_all_text_fields_with_truncated_flags": (
        STRUCT, ["Get-SlideNotesTextClamped", "Add-BudgetedText", "title_truncated", "body_truncated", "notes_truncated",
                 "text_truncated", "slides_truncated", "shapes_truncated"], ["Limit-StructureText"], []),
    "extract_structure_notes_use_clamped_reader": (
        ("function Get-SlideNotesTextClamped(", "function Get-PowerPointStructure("), ["Read-ShapeTextClamped"], [], []),
    "read_shape_text_clamped_reads_paragraphs_incrementally": (
        ("function Read-ShapeTextClamped(", "function Add-BudgetedText("),
        ["Paragraphs()", "break", "Read-ParagraphChunked $para $remaining"], [],
        ["$tf.TextRange.Text", "TextFrame.TextRange.Text", "$para.Text"]),
    "paragraph_chunked_reads_via_characters": (
        ("function Read-ParagraphChunked(", "function Read-ShapeTextClamped("),
        [".Characters($pos, $chunkLen)", "$script:StructureReadChunkChars", "$sb.Length -ge $maxRead"], [], ["$para.Text"]),
    "body_text_accumulation_is_budgeted": (
        STRUCT, ["New-Object System.Text.StringBuilder", 'Add-BudgetedText $bodySb $clampedShapeText.Text $maxChars "`n"'],
        ["$bodyParts"], []),
    "slide_and_shape_count_caps": (STRUCT, ["$slideIdx -gt $maxSlides", "$shapeIdx -gt $maxShapesPerSlide"], [], []),
    "overhead_budget_per_shape_and_slide": (
        STRUCT, ["$script:StructureOverheadCharsPerSlide", "$script:StructureOverheadCharsPerShape",
                 "$totalChars += $s.text.Length + $script:StructureOverheadCharsPerShape"], [], []),
    "running_char_budget_before_serialization": (
        STRUCT, ["$charBudget", "$totalChars", "$totalChars -gt $charBudget", "STRUCTURE_TOO_LARGE:"], [], ["ConvertTo-Json"]),
    "too_large_becomes_413_not_500": (
        EXTRACT_HANDLER, ['StartsWith("STRUCTURE_TOO_LARGE:")', "Write-JsonResponse $resp 413 @{ error = $r.Error }"], [], []),
    "limit_env_rejects_non_positive_and_non_integer": (
        ("function Resolve-StructureLimitEnv(", "function Get-MaxStructureFieldChars"),
        ["[long]::TryParse(", "$parsed -le 0", "Write-Warning"], [], []),
}


@pytest.mark.parametrize("scope,has,lacks,code_lacks", PS1_CASES.values(), ids=PS1_CASES)
def test_ps1_contract(ps1, scope, has, lacks, code_lacks):
    if scope == "START":
        text = (_DEPLOY / "start-office-worker.ps1").read_text(encoding="utf-8-sig")
    else:
        text = ps1 if scope is None else _scope(ps1, *scope)
    for s in has:
        assert s in text, s
    for s in lacks:
        assert s not in text, s
    for s in code_lacks:
        assert s not in _code(text), s                  # コメント中の説明文は対象外・実コードのみ


def test_ps1_ordering_and_counts(ps1):
    # 413 の早期判定（Content-Length）は本体読み取りより前
    assert ps1.index("$req.ContentLength64 -gt $readCeiling") < ps1.index("Read-BoundedBytes $req.InputStream $readCeiling")
    # 既存の拡張子許可表を再利用・一時ファイルは必ず削除
    assert ps1.count("$script:ExtMap[$ext]") >= 2 and ps1.count("$script:RenderExtMap[$ext]") >= 2
    assert ps1.count("Remove-Item -LiteralPath $tmpIn") >= 2
    # 直列化 → サイズ検査 → 書き込みの順・STRUCTURE_TOO_LARGE
    fn = _scope(ps1, "function Invoke-ExtractStructureOnce", "function Invoke-OfficeJobOnce(")
    assert (fn.index("ConvertTo-Json -Compress -Depth 10") < fn.index("$jsonBytes.Length -gt $maxJsonBytes")
            < fn.index("[System.IO.File]::WriteAllBytes($outFile, $jsonBytes)"))
    assert "STRUCTURE_TOO_LARGE:" in fn
    # 予算照合はスライドループの内側・上限超過で列挙を打ち切る
    st = _scope(ps1, *STRUCT)
    assert st.index("foreach ($slide in $pres.Slides)") < st.index("$totalChars -gt $charBudget")
    assert "break" in st[st.index("$slideIdx -gt $maxSlides"):][:80]
    assert "break" in st[st.index("$shapeIdx -gt $maxShapesPerSlide"):][:80]
    assert st.count("Read-ShapeTextClamped") >= 2


def test_ps1_documented_defaults_and_limit_getters_validate_env(ps1):
    start = ps1.index("$script:DefaultMaxStructureFieldChars = 32768")
    assert "既定値の根拠" in ps1[max(0, start - 1500):start]
    start = ps1.index("$script:StructureOverheadCharsPerShape = 256")
    assert "$script:StructureOverheadCharsPerSlide = 512" in ps1 and "オーバーヘッド" in ps1[max(0, start - 1200):start]
    assert "$script:StructureReadChunkChars = 8192" in ps1
    resolve = _scope(ps1, "function Resolve-StructureLimitEnv(", "function Get-MaxStructureFieldChars")
    assert "return $defaultValue" in resolve[resolve.index("Write-Warning"):]
    for name in ("Get-MaxStructureFieldChars", "Get-MaxStructureJsonBytes", "Get-MaxStructureSlides",
                 "Get-MaxStructureShapesPerSlide"):
        start = ps1.index(f"function {name} {{")
        assert "Resolve-StructureLimitEnv" in ps1[start:ps1.index("\n}", start)], name


def test_ps1_extract_structure_is_powerpoint_only(ps1):
    start = ps1.index("$script:ExtractStructureExtMap = @{")
    map_text = ps1[start:ps1.index("\n}", start)]
    assert '".ppt"' in map_text and '".pptx"' in map_text
    assert '".doc"' not in map_text and '".xls"' not in map_text


def test_ps1_paragraph_chunked_breaks_on_budget_and_on_characters_failure(ps1):
    fn = _scope(ps1, "function Read-ParagraphChunked(", "function Read-ShapeTextClamped(")
    assert fn.count("break") >= 2                      # 予算到達時・Characters() 失敗時（一括取得へフォールバックしない）
