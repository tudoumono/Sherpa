"""旧形式 Office（.doc/.xls/.ppt）→ 新形式 OOXML の変換バックエンド抽象（起案 docs/archive/proposals/2026-07-08-旧Office変換2系統.md W0）。

旧バイナリ（CFB）は OOXML ではないため `office_md` の XML 直パースでは読めない（INGEST-MD §5.6・D2）。本モジュールは
「旧→新（OOXML）の**前段変換器**」を提供し、MD 化自体は既存①OOXML アーム（値の権威・決定的）へ委譲する
（**LibreOffice を構造抽出の権威にしない**＝INGEST-MD の決定を維持。LO は「Office を持たない環境での旧→新変換
の選択肢」に留める）。

バックエンドの解決順は **system_settings `legacy_backend` > env `SHERPA_LEGACY_BACKEND` > 既定 "none"**
（2026-07-08-設定分離とUI整備.md S1 の汎用 KV に相乗り）。値は `none` | `libreoffice` | `office_com`。
env 段は ENV-CLEAN（2026-09-03）でも**撤去しない**——`providers/codex/mcp.py::_mcp_env` が親プロセス
（DB 接続あり）で解決した実効値を `SHERPA_LEGACY_BACKEND` としてスナップショットし、MCP サブプロセス
（PG creds 無し）はこの env フォールバックだけで親と同じ実効値に一致する（`SHERPA_LEGACY_EXTS`／
`SHERPA_VLM_USABLE` と同じ内部 IPC 契約）。管理画面からは system_settings 段が常に優先するため、
「UI が唯一の真実源」の原則自体は保たれる。
`office_com`（Windows の本物 Office・忠実変換）は **W1（2026-07-08）で追加**。COM interop は Windows 側の
`deploy/office-com-worker.ps1` だけが持ち、ここ（WSL コア）は **HTTP か WSL interop の one-shot で呼ぶ**。
office_com には2つの動作形態がある（**W2'・feedback-batch-2026-07-08 ⑥・2026-07-08**）:
  - **direct（既定・同一マシン）**: `SHERPA_OFFICE_COM_URL` 未設定時、WSL interop（`/mnt/c/.../powershell.exe`）で
    ps1 を one-shot 実行する（常駐ワーカー・URL・トークン不要＝Windows 側の事前準備ゼロ）。powershell.exe が
    検出できなければ到達不可（fail-safe）。healthz は ps1 `-Healthz` の JSON を長め TTL でキャッシュする。
  - **http（別ホスト）**: `SHERPA_OFFICE_COM_URL` 設定時、その常駐 HTTP ワーカー（別マシンの Office）を呼ぶ
    （W1 実装・`SHERPA_OFFICE_COM_TOKEN`・healthz を短 TTL でキャッシュ）。
`SHERPA_OFFICE_COM_URL`（未設定＝direct へ倒す）・`SHERPA_OFFICE_COM_TOKEN`・`SHERPA_POWERSHELL_BIN`（direct の
powershell.exe 明示パス・未設定は既定パスを探す）。
`SHERPA_LEGACY_EXTS`（設定時は最優先＝MCP サブプロセス用のスナップショット・office_com の URL/TOKEN を
持たないサブプロセスに healthz probe をさせず、親の実効値をそのまま信じさせる。W1 RV Med）。

**OFFICE-WIN-001（2026-07-20-調査型RAG詳細修正計画.html §6.5・http モード限定）**: 別ホストのワーカーへ
原本をどう渡すかは `transfer_mode`（`system_settings` `office_transfer_mode` > env
`SHERPA_OFFICE_TRANSFER_MODE` > 既定 `"path"`）で切り替える。`path`（既定・現行完全不変＝Windows から見える
絶対パス/UNC を JSON で渡す・共有ストレージ前提）｜`upload`（毎回ファイル本体を multipart 送信・共有ストレージ
無しの独立 Linux サーバー向け・原本 sha256 を `source_hash` として添え、ワーカー側で検証させる）｜`auto`（まず
path を試し、path 方式そのものが使えないと判別できた場合（パス変換不能／ネットワーク到達不能／worker が
404「file not found」を返した）だけ upload へ縮退。500 等の COM 変換失敗はそのまま失敗として伝播し upload
へは縮退しない＝真の失敗を隠さない・Med-2）。direct モード（同一マシン・WSL interop）には適用しない
（ファイルシステムへ既に直接アクセスできるため転送方式という概念が無い＝常に path 相当）。

fail-safe: バックエンド none／soffice 未検出／ワーカー未到達／変換失敗はすべて「変換できない（None）」に倒し、
呼び出し側は従来どおり「未対応」として表示する（既定 none＝挙動不変）。決定性は「キャッシュ後の①MD化」で担保する
（同じ OOXML → 同じ MD。SaveAs のバイト厳密性には依存しない）。soffice/Office のバージョンは provenance に残す。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# LOG-2（2026-09-03）: 専用ログ（sherpa.convert.libreoffice）へルーティングする（`sherpa/log_setup.py`
# の登録表参照）。office_com backend（http/direct）のログも本モジュール内にあるため同じ系統に乗る。
_log = logging.getLogger("sherpa.convert.libreoffice")

# W0 の対象＝旧バイナリ → 新形式（OOXML）拡張子の対応。これ以外は W0 対象外。
LEGACY_EXT_MAP: dict[str, str] = {".doc": ".docx", ".xls": ".xlsx", ".ppt": ".pptx"}

# 拡張子 → office_com ワーカーが使うアプリ名（healthz の versions dict のキーと一致・deploy/office-com-worker.ps1
# の $script:ExtMap と対応）。RV Med（2026-07-08）: healthz `ok` だけで3拡張子すべてを候補化すると、Word のみ
# 導入環境で .xls が毎回投入されては失敗する（failed に寄る）。アプリ単位でゲートするために使う。
_EXT_APP: dict[str, str] = {".doc": "word", ".xls": "excel", ".ppt": "powerpoint"}

# 選択可能なバックエンド（既知値・検証と UI 選択肢に使う）。"none"＝現状どおり未対応表示（既定）。
# "libreoffice"＝WSL 内で soffice が旧→新変換。"office_com"＝Windows 側の独立ワーカー（本物の Office・忠実変換）を
# HTTP で呼ぶ（W1・deploy/office-com-worker.ps1・INGEST-MD §5.6「実装境界」）。
KNOWN_BACKENDS: frozenset[str] = frozenset({"none", "libreoffice", "office_com"})
# UI の選択肢表示順（既定の「使わない」を先頭に・KNOWN_BACKENDS と集合として一致）。
BACKEND_OPTIONS: tuple[str, ...] = ("none", "libreoffice", "office_com")
_DEFAULT_BACKEND = "none"

_DEFAULT_TIMEOUT_SEC = 60.0            # 1 件あたりの変換タイムアウト（env SHERPA_LEGACY_TIMEOUT で調整可）
_VERSION_TIMEOUT_SEC = 15.0           # `soffice --version` のタイムアウト（検出・provenance 用）
_OFFICE_COM_HEALTH_TIMEOUT = 2.0      # office_com(http) /healthz の短タイムアウト（到達判定・毎回叩かないよう TTL キャッシュ）
_OFFICE_COM_HEALTH_TTL = 30.0         # http /healthz の結果をプロセス内でキャッシュする秒数（到達可否・versions）

# W2'（direct モード・2026-07-08）: ps1 の one-shot は起動コスト（~1-3s）があるため長め TTL でキャッシュする。
_OFFICE_COM_DIRECT_HEALTH_TTL = 300.0  # direct `-Healthz` one-shot の結果をキャッシュする秒数
_DIRECT_HEALTH_TIMEOUT = 15.0          # direct `-Healthz` one-shot 自体のタイムアウト（powershell 起動＋レジストリ参照）
# WSL 側の変換/レンダ backstop タイムアウト。ps1 内部（-JobTimeoutSec）が先に発火して Office 残骸を Windows 側で
# 掃除する（Stop-CandidateProcesses）ため、WSL は「内部タイムアウト＋余裕」だけ待つ二重の保険（interop の
# wedge 等の異常時のみ WSL 側 killpg が効く）。unit テストは本定数を monkeypatch で短縮する。
_DIRECT_GRACE_SEC = 30.0

# direct モードの powershell.exe 既定パス（WSL interop・env SHERPA_POWERSHELL_BIN で上書き可）。
_DEFAULT_POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"

# 直列実行（LibreOffice は同一プロファイルでの並列が不安定・Office COM も並列不可）＝プロセス内で1件ずつ変換する。
_convert_lock = threading.Lock()

# ING-1（閉じた理由語彙）: `convert_to_ooxml()` の戻り値契約（`bytes | None`）は変えない（既存呼び出し元・
# tests への影響を避ける・最小修正）。詳しい失敗理由（タイムアウト／バックエンド未設定）は
# スレッドローカルな片方向シグナルで伝える——`ensure_ooxml()` が冒頭で必ずクリアしてから
# `convert_to_ooxml()` を呼ぶため、直前の無関係な呼び出しの残留が混ざらない
# （`ensure_ooxml`/`take_conversion_failure_reason` docstring 参照）。
_conversion_failure_ctx = threading.local()


def _note_conversion_failure_reason(reason: str) -> None:
    _conversion_failure_ctx.reason = reason


def take_conversion_failure_reason() -> str | None:
    """直前の `ensure_ooxml()` 呼び出し（同一スレッド）が失敗した詳しい理由（読んだら消費・無ければ None）。

    現状セットされ得るのは `"timeout"`——`_run_soffice`（libreoffice subprocess）だけでなく
    office_com の HTTP（path/upload 転送）・direct（WSL interop ps1）の各タイムアウト経路も含む。
    `office_md` が `ensure_ooxml()` の失敗直後に読み取り、
    汎用の `legacy_conversion_failed` より詳しい理由コード（`legacy_conversion_timeout`）を
    rel 単位の失敗一覧へ残すために使う（バックエンド未設定/未到達は `legacy_exts()` で判別可能な
    ため、この経由は使わない）。
    """
    reason = getattr(_conversion_failure_ctx, "reason", None)
    _conversion_failure_ctx.reason = None
    return reason


def _is_timeout_error(e: Exception) -> bool:
    """`urllib`/`socket` 由来の例外が実質タイムアウトかを判定する（`URLError` は原因を `.reason` に包む・
    `urlopen(timeout=...)` 発火時は素の `socket.timeout`＝Python 3.10+ では `TimeoutError` のことが多い）。
    """
    if isinstance(e, TimeoutError):
        return True
    return isinstance(getattr(e, "reason", None), TimeoutError)

# `soffice --version` の結果を bin パス毎にキャッシュ（毎ファイルの provenance でサブプロセスを増やさない）。
_version_cache: dict[str, str | None] = {}
_warned_unknown_backend: set[str] = set()

# office_com(http) ワーカー /healthz の結果を URL 毎に短TTLキャッシュ（毎ファイル/毎リクエストで叩かない）。
# 値は (monotonic 取得時刻, healthz dict | None)。失敗（None）も同じ TTL でキャッシュする（fail-safe・叩き過ぎ防止）。
_healthz_cache: dict[str, tuple[float, dict | None]] = {}

# office_com(direct) `-Healthz` one-shot の結果を powershell.exe パス毎に長め TTL でキャッシュする（同上）。
_direct_healthz_cache: dict[str, tuple[float, dict | None]] = {}

# /mnt/<drive>/rest → <DRIVE>:\rest 変換用（単一ドライブ文字＋任意の残り。/mnt/c と /mnt/c/ も許容）。
_MNT_DRIVE = re.compile(r"^/mnt/([a-zA-Z])(?:/(.*))?$")


# ---- バックエンド解決（system_settings > env > 既定・S1・env 段の存置理由はモジュール docstring 参照）----

def _system_legacy_backend() -> str | None:
    """全体設定 system_settings の `legacy_backend`（非空 str のみ）。読めない/未設定は None（env へ倒す）。

    **fail-safe**: store を読めない文脈（MCP サブプロセスは PG creds 無し・DB 停止中）は例外を握って None
    （`arms._system_arms_enabled` と同じ流儀）。
    """
    try:
        from sherpa import store
        val = store.get_system_settings().get("legacy_backend")
    except Exception:
        return None
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def _env_backend() -> str:
    """env `SHERPA_LEGACY_BACKEND`（未設定は既定 "none"）。正規化のみ（既知/未知の判定は `_normalize`）。

    MCP サブプロセスは `providers/codex/mcp.py::_mcp_env` が親の実効値をここへスナップショットする
    （モジュール docstring 参照）ため、この env 読みは ENV-CLEAN（2026-09-03）でも維持する。
    """
    return (os.environ.get("SHERPA_LEGACY_BACKEND") or _DEFAULT_BACKEND).strip() or _DEFAULT_BACKEND


def _normalize(name: str) -> str:
    """未知のバックエンド名は "none" に倒す（fail-safe・プロセス内で1回だけ警告）。"""
    if name in KNOWN_BACKENDS:
        return name
    if name not in _warned_unknown_backend:
        _warned_unknown_backend.add(name)
        _log.warning("未知の legacy_backend を無視します: %s（既知: %s）",
                     name, ",".join(sorted(KNOWN_BACKENDS)))
    return _DEFAULT_BACKEND


def legacy_backend_name() -> str:
    """実効バックエンド名（system_settings > env > 既定）。既知の none|libreoffice のみ（未知は none・fail-safe）。"""
    sysv = _system_legacy_backend()
    return _normalize(sysv if sysv is not None else _env_backend())


def env_default_backend() -> str:
    """system_settings を無視した env/既定の実効バックエンド（設定画面の「未設定に戻すと何になるか」表示用）。"""
    return _normalize(_env_backend())


# ---- transfer_mode 解決（office_com の http モード限定・system_settings > env > 既定・OFFICE-WIN-001）----

# 別ホストのワーカーへ原本をどう渡すか。"path"（既定・現行完全不変）｜"upload"（ファイル本体を毎回送る）｜
# "auto"（path→失敗時 upload へ縮退）。direct モードには適用しない（呼び出し側が mode を見て使い分ける）。
KNOWN_TRANSFER_MODES: frozenset[str] = frozenset({"path", "upload", "auto"})
TRANSFER_MODE_OPTIONS: tuple[str, ...] = ("path", "upload", "auto")
_DEFAULT_TRANSFER_MODE = "path"
_warned_unknown_transfer_mode: set[str] = set()


def _system_transfer_mode() -> str | None:
    """全体設定 system_settings の `office_transfer_mode`（非空 str のみ）。fail-safe は `_system_legacy_backend` と同じ。"""
    try:
        from sherpa import store
        val = store.get_system_settings().get("office_transfer_mode")
    except Exception:
        return None
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def _env_transfer_mode() -> str:
    """env `SHERPA_OFFICE_TRANSFER_MODE`（未設定は既定 "path"）。"""
    return (os.environ.get("SHERPA_OFFICE_TRANSFER_MODE") or _DEFAULT_TRANSFER_MODE).strip() or _DEFAULT_TRANSFER_MODE


def _normalize_transfer_mode(name: str) -> str:
    """未知の transfer_mode は "path"（現状どおり）に倒す（fail-safe・プロセス内で1回だけ警告）。"""
    if name in KNOWN_TRANSFER_MODES:
        return name
    if name not in _warned_unknown_transfer_mode:
        _warned_unknown_transfer_mode.add(name)
        _log.warning("未知の office_transfer_mode を無視します: %s（既知: %s）",
                     name, ",".join(sorted(KNOWN_TRANSFER_MODES)))
    return _DEFAULT_TRANSFER_MODE


def transfer_mode_name() -> str:
    """実効 transfer_mode（system_settings > env > 既定 "path"）。office_com の http モードでのみ意味を持つ。"""
    sysv = _system_transfer_mode()
    return _normalize_transfer_mode(sysv if sysv is not None else _env_transfer_mode())


def env_default_transfer_mode() -> str:
    """system_settings を無視した env/既定の実効 transfer_mode（設定画面の「未設定に戻すと何になるか」表示用）。"""
    return _normalize_transfer_mode(_env_transfer_mode())


# ---- soffice 検出（env 明示 ＞ PATH）----

def _soffice_bin() -> str | None:
    """soffice 実行ファイルの絶対パス。env `SHERPA_SOFFICE_BIN`（絶対パス化＋実行可検査）＞ PATH の soffice。

    未検出は None（＝libreoffice バックエンドは実質無効＝fail-safe で変換せず未対応表示）。
    """
    override = os.environ.get("SHERPA_SOFFICE_BIN")
    if override:
        try:
            p = Path(override).expanduser().resolve()
        except OSError:
            return None
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
        return None
    return shutil.which("soffice")


def soffice_available() -> bool:
    """soffice が検出できるか（libreoffice バックエンドが実際に使えるかの判定）。"""
    return _soffice_bin() is not None


def soffice_version() -> str | None:
    """`soffice --version` の1行目（bin パス毎にキャッシュ）。未検出/失敗/タイムアウトは None（fail-safe）。"""
    bin_path = _soffice_bin()
    if not bin_path:
        return None
    if bin_path in _version_cache:
        return _version_cache[bin_path]
    version: str | None = None
    try:
        proc = subprocess.run([bin_path, "--version"], capture_output=True, text=True,
                              timeout=_VERSION_TIMEOUT_SEC)
        lines = (proc.stdout or "").strip().splitlines()
        version = lines[0].strip() if lines else None
    except Exception:
        version = None
    _version_cache[bin_path] = version
    return version


def _timeout_sec() -> float:
    """1 件あたりの変換タイムアウト秒（env `SHERPA_LEGACY_TIMEOUT`・不正/未設定は既定 60s）。"""
    raw = os.environ.get("SHERPA_LEGACY_TIMEOUT")
    if not raw:
        return _DEFAULT_TIMEOUT_SEC
    try:
        v = float(raw)
    except ValueError:
        return _DEFAULT_TIMEOUT_SEC
    return v if v > 0 else _DEFAULT_TIMEOUT_SEC


# ---- direct モード検出（WSL interop の powershell.exe・W2'）----

def _powershell_bin() -> str | None:
    """direct モードで使う powershell.exe の絶対パス。env `SHERPA_POWERSHELL_BIN`（絶対パス化＋実行可検査）＞
    既定 `/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe`。未検出は None（＝direct 無効＝fail-safe）。

    env を**明示設定したら override のみを見る**（未検出なら既定パスへフォールバックしない）＝テストで direct を
    確実に無効化できる（`SHERPA_POWERSHELL_BIN=/nonexistent` で unavailable に固定）。
    """
    override = os.environ.get("SHERPA_POWERSHELL_BIN")
    if override is not None:
        try:
            p = Path(override).expanduser().resolve()
        except OSError:
            return None
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
        return None
    p = Path(_DEFAULT_POWERSHELL)
    if p.is_file() and os.access(p, os.X_OK):
        return str(p)
    return None


def powershell_available() -> bool:
    """direct モードの powershell.exe が検出できるか（設定画面の「direct 検出状態」表示用）。"""
    return _powershell_bin() is not None


def _ps1_path() -> Path:
    """direct モードで one-shot 実行する office-com-worker.ps1 の WSL パス（repo の deploy/ 配下）。"""
    return Path(__file__).resolve().parents[3] / "deploy" / "office-com-worker.ps1"


def _ps1_win_path() -> str | None:
    """`_ps1_path()` を powershell.exe が -File で読める Windows パスへ変換（repo は WSL ネイティブ＝
    `\\\\wsl.localhost\\{distro}\\...`）。ファイル欠落／distro 不明で変換不能は None（fail-safe）。"""
    p = _ps1_path()
    if not p.is_file():
        return None
    return wsl_to_windows_path(str(p))


def office_com_mode() -> str:
    """office_com の動作形態を返す（W2'）: "http"（URL 設定済み＝別ホストのワーカー）｜"direct"（URL 未設定かつ
    powershell.exe 検出＝同一マシンの interop one-shot・既定）｜"unavailable"（どちらも無し＝現状どおり未対応表示）。"""
    if _office_com_url() is not None:
        return "http"
    if _powershell_bin() is not None:
        return "direct"
    return "unavailable"


def excel_display_available() -> bool:
    """Office-native表示補完に必要なMicrosoft Excelがworker profile上で利用可能か。"""
    return "excel" in _office_com_available_apps()


# ---- office_com（http: Windows 側ワーカーへの HTTP・W1）----

def _office_com_url() -> str | None:
    """office_com ワーカーの base URL（env `SHERPA_OFFICE_COM_URL`・末尾 `/` は落とす）。未設定は None（到達不可扱い）。"""
    raw = os.environ.get("SHERPA_OFFICE_COM_URL")
    if not raw or not raw.strip():
        return None
    return raw.strip().rstrip("/")


def _office_com_token() -> str | None:
    """ワーカーへ送る共有シークレット（env `SHERPA_OFFICE_COM_TOKEN`）。未設定は None（ワーカーが必須なら 401→到達不可）。"""
    tok = os.environ.get("SHERPA_OFFICE_COM_TOKEN")
    return tok if tok else None


def _office_com_headers(extra: dict | None = None) -> dict:
    """共有シークレットヘッダ（設定時のみ）＋追加ヘッダ。"""
    headers = dict(extra or {})
    tok = _office_com_token()
    if tok:
        headers["X-Sherpa-Token"] = tok
    return headers


def office_com_configured() -> bool:
    """office_com ワーカーの URL が設定されているか（「未設定」と「設定済みだが不達」を UI で区別するため）。"""
    return _office_com_url() is not None


def office_com_configured_url() -> str | None:
    """設定画面の接続テスト（`POST /system/office-worker/probe`・OFFICE-WIN-001 ④）が body 省略時に使う
    「保存済み」URL。現状は env `SHERPA_OFFICE_COM_URL` のみ（system_settings には url/token を持たない・
    転送方式（`office_transfer_mode`）だけが system_settings 対応・今回のスコープ外）。未設定は None。"""
    return _office_com_url()


def office_com_configured_token() -> str | None:
    """`office_com_configured_url` と対の「保存済み」トークン（env `SHERPA_OFFICE_COM_TOKEN`）。
    **値そのものを HTTP 応答へ含めないこと**（呼び出し側の責務・secrets 漏洩防止・W1 RV Med と同じ方針）。"""
    return _office_com_token()


def _fetch_healthz(url: str) -> dict | None:
    """`GET {url}/healthz` を短タイムアウトで叩き、{ok:true,...} なら dict、それ以外/失敗は None（fail-safe）。"""
    req = urllib.request.Request(url + "/healthz", method="GET", headers=_office_com_headers())
    try:
        with urllib.request.urlopen(req, timeout=_OFFICE_COM_HEALTH_TIMEOUT) as r:
            raw = r.read()
        data = json.loads(raw) if raw else {}
    except Exception:
        return None
    if isinstance(data, dict) and data.get("ok"):
        return data
    return None


def _healthz_http() -> dict | None:
    """http モードの healthz（到達可なら {ok,versions,worker} dict・不達/未設定は None）。

    URL 毎に短TTL（`_OFFICE_COM_HEALTH_TTL` 秒）でプロセス内キャッシュする（毎ファイル/毎リクエストで叩かない）。
    失敗（None）も同じ TTL でキャッシュ＝落ちているワーカーを叩き続けない。
    """
    url = _office_com_url()
    if not url:
        return None
    now = time.monotonic()
    cached = _healthz_cache.get(url)
    if cached is not None and (now - cached[0]) < _OFFICE_COM_HEALTH_TTL:
        return cached[1]
    data = _fetch_healthz(url)
    _healthz_cache[url] = (now, data)
    return data


def _run_healthz_direct(ps_bin: str) -> dict | None:
    """direct モードの healthz＝ps1 を `-Healthz` で one-shot 実行し stdout の JSON を読む（COM は起動しない・
    レジストリ参照の軽量判定）。非0終了/タイムアウト/JSON 不正/ok でない は None（fail-safe）。"""
    ps1_win = _ps1_win_path()
    if ps1_win is None:
        return None
    cmd = [ps_bin, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
           "-File", ps1_win, "-Healthz"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=_DIRECT_HEALTH_TIMEOUT,
                              start_new_session=True)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or b"").decode("utf-8", "replace").strip().lstrip("\ufeff")
    try:
        data = json.loads(out) if out else None
    except ValueError:
        return None
    if isinstance(data, dict) and data.get("ok"):
        return data
    return None


def _healthz_direct() -> dict | None:
    """direct モードの healthz（powershell.exe パス毎に長め TTL `_OFFICE_COM_DIRECT_HEALTH_TTL` でキャッシュ）。

    one-shot は起動コスト（~1-3s）があるため http より長い TTL で叩き過ぎを防ぐ。失敗（None）も同じ TTL でキャッシュ。
    """
    ps_bin = _powershell_bin()
    if not ps_bin:
        return None
    now = time.monotonic()
    cached = _direct_healthz_cache.get(ps_bin)
    if cached is not None and (now - cached[0]) < _OFFICE_COM_DIRECT_HEALTH_TTL:
        return cached[1]
    data = _run_healthz_direct(ps_bin)
    _direct_healthz_cache[ps_bin] = (now, data)
    return data


def office_com_healthz() -> dict | None:
    """office_com の healthz 応答（モードに応じ http/direct を使い分け・到達可なら {ok,versions,worker} dict）。

    未設定・powershell 未検出（unavailable）は None（fail-safe）。versions/到達可否の実体はモード別のキャッシュ済み
    プローブ（`_healthz_http`/`_healthz_direct`）が返す。
    """
    mode = office_com_mode()
    if mode == "http":
        return _healthz_http()
    if mode == "direct":
        return _healthz_direct()
    return None


def office_com_available() -> bool:
    """office_com ワーカーが到達可か（URL 設定済み かつ /healthz が 200 で {ok:true}）。未設定/不達は False（fail-safe）。"""
    return office_com_healthz() is not None


def probe_office_com(url: str, token: str | None = None, timeout: float | None = None) -> dict:
    """任意の url/token で office_com ワーカーの到達性を検査する（設定画面の接続テスト用の関数。OFFICE-WIN-001）。

    `_office_com_url()`/`_office_com_token()`（env 由来・保存済み設定）は使わず、呼び出し元が明示的に渡した
    値だけで `/healthz` を1回叩く（`POST /settings/test` の他プロバイダ probe と同じ流儀＝保存前に確認できる・
    保存はしない）。router への配線・UI は次スライス（本関数は API/関数レベルまで）。

    戻り値 `{ok, detail, versions}`。到達不可/認証失敗/応答不正はすべて `ok=False`（fail-safe）。
    """
    u = (url or "").strip().rstrip("/")
    if not u:
        return {"ok": False, "detail": "URL が未入力です", "versions": None}
    headers = {}
    if token:
        headers["X-Sherpa-Token"] = token
    req = urllib.request.Request(u + "/healthz", method="GET", headers=headers)
    t = timeout if timeout and timeout > 0 else _OFFICE_COM_HEALTH_TIMEOUT
    try:
        with urllib.request.urlopen(req, timeout=t) as r:
            raw = r.read()
        data = json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = "認証に失敗しました（トークン不一致）" if e.code == 401 else f"HTTP {e.code}"
        return {"ok": False, "detail": detail, "versions": None}
    except Exception as e:
        return {"ok": False, "detail": f"接続できません（{e.__class__.__name__}）", "versions": None}
    if isinstance(data, dict) and data.get("ok"):
        return {"ok": True, "detail": "接続OK", "versions": data.get("versions")}
    return {"ok": False, "detail": "応答が不正です", "versions": None}


def _office_com_versions_summary() -> str | None:
    """provenance 用に healthz の versions を短い文字列へ要約（例 `word=16.0,excel=16.0`）。取得不可は None。"""
    hz = office_com_healthz()
    if not hz:
        return None
    versions = hz.get("versions")
    if not isinstance(versions, dict):
        return None
    parts = []
    for app in ("word", "excel", "powerpoint"):
        v = versions.get(app)
        if v and not isinstance(v, bool):        # False（未導入）は載せない
            parts.append(f"{app}={v}")
    return ",".join(parts) if parts else None


def _office_com_available_apps() -> set[str]:
    """healthz の versions で検出できたアプリ名集合（word/excel/powerpoint）。不達/取得不可は空集合。

    値は False（未登録）｜True（登録はあるが版不明）｜バージョン文字列のいずれか。False 以外は「使える」扱い。
    """
    hz = office_com_healthz()
    if not hz:
        return set()
    versions = hz.get("versions")
    if not isinstance(versions, dict):
        return set()
    return {app for app in ("word", "excel", "powerpoint") if versions.get(app)}


def office_com_available_exts() -> set[str]:
    """office_com で今変換できる拡張子集合（healthz の versions で検出できたアプリ対応分のみ）。

    RV Med（2026-07-08）: healthz `ok` だけで .doc/.xls/.ppt を丸ごと候補化すると、Word のみ導入環境で
    .xls が投入されては毎回失敗する（failed に寄る＝ユーザーに誤った期待を持たせる）。アプリ単位でゲートする。
    """
    apps = _office_com_available_apps()
    return {ext for ext, app in _EXT_APP.items() if app in apps}


def wsl_to_windows_path(p: str) -> str | None:
    """WSL パス → Windows パス（office_com ワーカーへ渡す・純関数）。

    - `/mnt/<drive>/rest` → `<DRIVE>:\\rest`（大文字ドライブ・バックスラッシュ）。world は元々 Windows
      ドライブ（例 C:\\test）なので通常こちら。
    - `/mnt` 配下でない WSL ネイティブパスは `\\\\wsl.localhost\\{distro}\\...`（distro は env `WSL_DISTRO_NAME`）へ
      フォールバック。distro 不明なら None。
    - 絶対パスでない/変換不能は None（fail-safe＝呼び出し側は変換せず未対応表示）。
    """
    if not p or not isinstance(p, str) or not p.startswith("/"):
        return None
    m = _MNT_DRIVE.match(p)
    if m:
        drive = m.group(1).upper()
        rest = (m.group(2) or "").replace("/", "\\")
        return f"{drive}:\\{rest}"
    distro = os.environ.get("WSL_DISTRO_NAME")
    if not distro:
        return None
    rest = p.lstrip("/").replace("/", "\\")
    return f"\\\\wsl.localhost\\{distro}\\{rest}"


def _convert_office_com_ex(src: Path, target_ext: str) -> tuple[bytes | None, bool]:
    """`_convert_office_com` の内部実装。戻り値 `(data, fallback_worthy)`（Med-2）。

    `fallback_worthy=True` は「path 方式そのものが使えない」と判別できた場合のみ:
      (i) Windows パスへのマッピング自体が不能（HTTP を送る前に判明）。
      (ii) ネットワーク到達不能（接続不可・DNS 失敗・タイムアウト等＝`URLError`/`OSError`）。
      (iii) worker が「file not found」（HTTP 404・Handle-Convert が `Test-Path` 失敗時にのみ返す・判別可能）
            を返した場合。
    それ以外（500 等の COM 変換失敗・400 の拡張子/target 不一致・401 の認証失敗・想定外の例外）は
    `fallback_worthy=False`＝呼び出し元（auto モード）は upload へ縮退せず、この失敗をそのまま最終結果として
    伝播する（COM の真の失敗を upload 再試行の成功で覆い隠さない）。
    """
    url = _office_com_url()
    if not url:
        return None, False
    win_path = wsl_to_windows_path(str(Path(src)))
    if win_path is None:
        _log.warning("office_com: Windows パスに変換できません: %s", src)
        return None, True         # (i) パスへのマッピング自体が不能＝fallback 対象
    body = json.dumps({"path": win_path, "target": target_ext.lstrip(".")}).encode("utf-8")
    headers = _office_com_headers({"Content-Type": "application/json"})
    req = urllib.request.Request(url + "/convert", data=body, method="POST", headers=headers)
    with _convert_lock:
        try:
            with urllib.request.urlopen(req, timeout=_timeout_sec()) as r:
                data = r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                _log.warning("office_com: path 方式で対象が見つかりません（HTTP 404・upload へ縮退可）: %s", src)
                return None, True    # (iii) worker が「パスが見つからない」と判別可能に返した
            _log.warning("office_com 変換が失敗しました（HTTP %s・fallback しません）: %s", e.code, src)
            return None, False       # 500 等の真の失敗は fallback しない
        except (urllib.error.URLError, OSError) as e:
            _log.warning("office_com 変換を実行できませんでした（%s・upload へ縮退可）: %s",
                         e.__class__.__name__, src)
            if _is_timeout_error(e):
                _note_conversion_failure_reason("timeout")
            return None, True        # (ii) ネットワーク到達不能
        except Exception as e:
            _log.warning("office_com 変換で想定外エラー（%s・fallback しません）: %s", e.__class__.__name__, src)
            return None, False
    return (data or None), False


def _convert_office_com(src: Path, target_ext: str) -> bytes | None:
    """旧形式 `src` を Windows 側ワーカーへ HTTP で送って変換したバイト列。到達不可/失敗/タイムアウトは None（fail-safe）。

    直列実行（COM は並列不可・ワーカー側も直列だが WSL 側でも `_convert_lock` で二重に直列化する）。
    タイムアウトは libreoffice と同じ `SHERPA_LEGACY_TIMEOUT`（既定60s）を流用する。"path" 単独呼び出しでは
    fallback という概念が無いため `_convert_office_com_ex` の `fallback_worthy` 信号は無視して data だけ返す。
    """
    return _convert_office_com_ex(src, target_ext)[0]


# ---- office_com（http: upload 転送・OFFICE-WIN-001・共有ストレージ無しの独立 Linux サーバー向け）----

_MAX_UPLOAD_RETRIES = 1   # 失敗時の再送回数（初回＋1回・source_hash が同じ＝冪等なリトライ）


def _build_multipart(fields: dict[str, str], file_field: str, filename: str, file_bytes: bytes) -> tuple[bytes, str]:
    """multipart/form-data のボディを標準ライブラリのみで組み立てる（新規 pip 依存を増やさない）。

    フィールドは全てUTF-8テキスト（target/source_hashに加え、Excel表示補完の日本語sheet名を含む
    ``cells_json``）＋1個のファイルパート。ファイル名は
    ワーカー側で拡張子判定にしか使わない（非 ASCII を含んでも実害無し・往復での文字化けは許容）。
    戻り値は `(body, content_type)`（`content_type` に boundary を含む・そのまま HTTP ヘッダへ渡せる）。
    """
    boundary = "----SherpaBoundary" + uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8"))
    parts.append(
        (f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
         f'Content-Type: application/octet-stream\r\n\r\n').encode("utf-8")
        + file_bytes + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _post_multipart(url: str, body: bytes, content_type: str, timeout: float) -> bytes | None:
    """multipart POST を最大 `_MAX_UPLOAD_RETRIES + 1` 回（初回＋冪等リトライ）実行し応答バイトを返す。

    リトライは同じ body（同じ source_hash）を再送するだけ（ワーカー側は毎回ゼロから変換するため二重実行の
    副作用が無い＝冪等）。失敗（HTTPError／接続不可／タイムアウト等）はすべて None（fail-safe）。
    """
    headers = _office_com_headers({"Content-Type": content_type})
    for attempt in range(_MAX_UPLOAD_RETRIES + 1):
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            _log.warning("office_com upload が失敗しました（HTTP %s・試行 %s/%s）: %s",
                         e.code, attempt + 1, _MAX_UPLOAD_RETRIES + 1, url)
        except Exception as e:
            _log.warning("office_com upload を実行できませんでした（%s・試行 %s/%s）: %s",
                         e.__class__.__name__, attempt + 1, _MAX_UPLOAD_RETRIES + 1, url)
            if _is_timeout_error(e):
                _note_conversion_failure_reason("timeout")
    return None


def _convert_office_com_upload(src: Path, target_ext: str) -> bytes | None:
    """旧形式 `src` をファイル本体ごと multipart 送信して変換する（共有ストレージ無しの別ホスト向け）。

    原本 sha256 を `source_hash` として添付する（ワーカー側で検証・不一致は 400）。直列実行は path 方式と
    同じ `_convert_lock`（COM は並列不可）。
    """
    url = _office_com_url()
    if not url:
        return None
    try:
        file_bytes = Path(src).read_bytes()
    except OSError:
        _log.warning("office_com upload: 原本を読めません: %s", src)
        return None
    source_hash = hashlib.sha256(file_bytes).hexdigest()
    body, ctype = _build_multipart(
        {"target": target_ext.lstrip("."), "source_hash": source_hash}, "file", Path(src).name, file_bytes)
    with _convert_lock:
        return _post_multipart(url + "/convert-upload", body, ctype, _timeout_sec())


def _convert_office_com_via_transfer_mode(src: Path, target_ext: str) -> bytes | None:
    """http モードの転送方式（`transfer_mode_name()`）を解決して変換する。

    既定 "path" は `_convert_office_com` とまったく同じ（現行完全不変）。"auto" は path を試し、
    `_convert_office_com_ex` が `fallback_worthy=True`（パス変換不能／ネットワーク到達不能／worker が
    404「file not found」）と判別した場合のみ upload へ縮退する。500 等の真の COM 失敗は fallback せず
    そのまま None を返す（Med-2・upload 再試行の成功で COM の真の失敗を覆い隠さない）。
    """
    mode = transfer_mode_name()
    if mode == "upload":
        return _convert_office_com_upload(src, target_ext)
    if mode == "auto":
        data, fallback_worthy = _convert_office_com_ex(src, target_ext)
        if data is not None:
            return data
        if not fallback_worthy:
            return None            # 真の失敗（500 等）はそのまま伝播・upload へ縮退しない
        data = _convert_office_com_upload(src, target_ext)
        if data is not None:
            # path 試行で残った失敗理由（timeout 等）は upload 縮退が成功したなら
            # 消す——最終的に変換できているのに前段の失敗理由が誤って報告されないようにする。
            take_conversion_failure_reason()
        return data
    return _convert_office_com(src, target_ext)          # "path"（既定）


# ---- office_com（direct: WSL interop で ps1 を one-shot・W2'）----

# render_pdf が受け付ける Office 原本の拡張子（旧/新両方・ps1 の $script:RenderExtMap と対応）。
_RENDER_EXTS: frozenset[str] = frozenset(
    {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"})

# Microsoft Excel Range.Text / DisplayFormat.NumberFormat 抽出対象。変換backendの選択とは独立した
# optional補完で、旧XLSも原本のままExcelへ渡す。
_EXCEL_DISPLAY_EXTS: frozenset[str] = frozenset({".xls", ".xlsx"})
_EXCEL_CELL_REF_RE = re.compile(r"^[A-Z]{1,3}[1-9][0-9]{0,6}$")
_MAX_EXCEL_DISPLAY_CELLS = 50_000
_EXCEL_DISPLAY_SCHEMA = "sherpa-excel-display-v1"


def _run_direct_process(cmd: list[str], src: Path, timeout: float) -> bool:
    """direct モードの ps1（-DirectJob）を実行し成否を返す。非0終了/タイムアウト/起動失敗は False（fail-safe）。

    ps1 内部（-JobTimeoutSec）が先に発火して Office 残骸を Windows 側で掃除するため、ここ（WSL）の `timeout` は
    「内部タイムアウト＋余裕」の backstop。異常時（interop の wedge 等）に `start_new_session=True` の
    プロセスグループを丸ごと SIGKILL する（`_kill_process_group` 流用・soffice と同じ手法）。
    """
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)
    except OSError as e:
        _log.warning("office_com(direct) を実行できませんでした（%s）: %s", e.__class__.__name__, src)
        return False
    try:
        proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _log.warning("office_com(direct) がタイムアウトしました（%ss backstop）: %s", timeout, src)
        _kill_process_group(proc)
        _note_conversion_failure_reason("timeout")
        return False
    if proc.returncode != 0:
        _log.warning("office_com(direct) が異常終了しました（rc=%s）: %s", proc.returncode, src)
        return False
    return True


def _run_direct_job(
    src: Path,
    job: str,
    out_ext: str,
    target_ext: str | None,
    *,
    options: dict | None = None,
) -> bytes | None:
    """direct モードで ps1 を `-DirectJob` one-shot 実行し、結果バイト列を返す（変換/レンダ共通）。到達不可/失敗は None。

    - `job`＝"convert"（旧→新 OOXML）｜"render"（PDF）。`out_ext`＝出力拡張子（.docx/.xlsx/.pptx/.pdf）。
    - 入出力は env でなく **ps1 引数**で渡す（WSL→Windows interop の env 透過に依存しない）。入力/スクリプト/出力
      パスは `wsl_to_windows_path` で Windows 形式へ変換する（出力は WSL の一時ファイルを `\\\\wsl.localhost` の
      UNC で渡し、ps1 が WriteAllBytes で書き、こちらが読み返す＝Office は常に Windows ローカルに書く）。
    - 直列実行（`_convert_lock`）・backstop タイムアウト（**ps1 へ渡した整数秒** ＋ `_DIRECT_GRACE_SEC`）。

    RV Med（2026-07-08）: `-JobTimeoutSec` は ps1 側で `$tsec -le 0` のとき既定120秒へフォールバックする
    （`Invoke-DirectJob`）。`SHERPA_LEGACY_TIMEOUT` に1秒未満の値（例 0.3）を設定していると、素朴に
    `str(int(inner))` すると "0" になりこのフォールバックを誤って踏む＝ps1 内部は120秒待つのに、WSL 側の
    backstop は元の小さい値（0.3+grace）で先に外側 powershell.exe を kill してしまい、ps1 内部の
    `Stop-CandidateProcesses`（この変換が作った Office だけを識別して停止する処理）が一度も走らないまま
    Office プロセスが孤児化しうる。**0 を作らない最低1秒への切り上げ**（`_timeout_sec()` 自体は他経路
    （soffice のタイムアウト検証・http モードの urllib timeout）でも使われており、小さい閾値でのタイムアウト
    挙動を検証する既存テストがあるため、そちらは変更せず direct 専用にここで丸める）。backstop の起点も
    **実際に ps1 へ渡した整数秒**（丸め後の値）に揃える＝渡した値と backstop の間に矛盾が生まれない。
    """
    ps_bin = _powershell_bin()
    if not ps_bin:
        return None
    ps1_win = _ps1_win_path()
    if ps1_win is None:
        _log.warning("office_com(direct): ps1 の Windows パスに変換できません: %s", _ps1_path())
        return None
    win_in = wsl_to_windows_path(str(Path(src)))
    if win_in is None:
        _log.warning("office_com(direct): 入力を Windows パスに変換できません: %s", src)
        return None
    tmpdir = tempfile.mkdtemp(prefix="sherpa-oc-direct-")
    try:
        out_path = Path(tmpdir) / ("out" + out_ext)
        err_path = Path(tmpdir) / "err.txt"
        options_path = Path(tmpdir) / "options.json"
        win_out = wsl_to_windows_path(str(out_path))
        win_err = wsl_to_windows_path(str(err_path))
        if win_out is None or win_err is None:
            _log.warning("office_com(direct): 一時出力を Windows パスに変換できません（WSL_DISTRO_NAME 未設定?）")
            return None
        inner_arg = max(1, math.ceil(_timeout_sec()))    # 0 を作らない（ps1 の 120s フォールバック誤爆を防ぐ）
        cmd = [ps_bin, "-NoProfile", "-NonInteractive", "-STA", "-ExecutionPolicy", "Bypass",
               "-File", ps1_win, "-DirectJob",
               "-InPath", win_in, "-OutPath", win_out, "-ErrPath", win_err,
               "-Job", job, "-Target", (target_ext.lstrip(".") if target_ext else "pdf"),
               "-JobTimeoutSec", str(inner_arg)]
        if options is not None:
            options_path.write_text(
                json.dumps(options, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")
            win_options = wsl_to_windows_path(str(options_path))
            if win_options is None:
                _log.warning("office_com(direct): optionsを Windows パスに変換できません")
                return None
            cmd.extend(["-OptionsPath", win_options])
        with _convert_lock:
            if not _run_direct_process(cmd, src, inner_arg + _DIRECT_GRACE_SEC):
                try:
                    detail = err_path.read_text(encoding="utf-8-sig").strip() if err_path.is_file() else ""
                except OSError:
                    detail = ""
                if detail:
                    _log.warning("office_com(direct) 詳細: %s", detail)
                return None
            try:
                if out_path.is_file():
                    return out_path.read_bytes() or None
            except OSError:
                return None
            return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _convert_office_com_direct(src: Path, target_ext: str) -> bytes | None:
    """旧形式 `src` を direct モード（WSL interop の ps1 one-shot）で新形式へ変換したバイト列。失敗は None（fail-safe）。"""
    return _run_direct_job(Path(src), "convert", target_ext, target_ext)


# ---- 忠実 PDF レンダ（Office外観確認や将来の視覚処理に利用可能・W2'）----

def _render_office_com_http_ex(src: Path) -> tuple[bytes | None, bool]:
    """`_render_office_com_http` の内部実装。戻り値 `(data, fallback_worthy)`（`_convert_office_com_ex` の
    render 対称・Med-2）。判別条件は `_convert_office_com_ex` と同じ（(i) パス変換不能／(ii) ネットワーク
    到達不能／(iii) worker の 404「file not found」のみ fallback 対象・500 等は fallback しない）。
    """
    url = _office_com_url()
    if not url:
        return None, False
    win_path = wsl_to_windows_path(str(Path(src)))
    if win_path is None:
        _log.warning("office_com render: Windows パスに変換できません: %s", src)
        return None, True
    body = json.dumps({"path": win_path}).encode("utf-8")
    headers = _office_com_headers({"Content-Type": "application/json"})
    req = urllib.request.Request(url + "/render", data=body, method="POST", headers=headers)
    with _convert_lock:
        try:
            with urllib.request.urlopen(req, timeout=_timeout_sec()) as r:
                data = r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                _log.warning("office_com render: path 方式で対象が見つかりません（HTTP 404・upload へ縮退可）: %s", src)
                return None, True
            _log.warning("office_com render が失敗しました（HTTP %s・fallback しません）: %s", e.code, src)
            return None, False
        except (urllib.error.URLError, OSError) as e:
            _log.warning("office_com render を実行できませんでした（%s・upload へ縮退可）: %s",
                         e.__class__.__name__, src)
            return None, True
        except Exception as e:
            _log.warning("office_com render で想定外エラー（%s・fallback しません）: %s", e.__class__.__name__, src)
            return None, False
    return (data or None), False


def _render_office_com_http(src: Path) -> bytes | None:
    """http モードのワーカー `POST /render` で Office 原本を PDF（as-displayed）へレンダしたバイト列。失敗は None。

    "path" 単独呼び出しでは fallback という概念が無いため `_render_office_com_http_ex` の `fallback_worthy`
    信号は無視して data だけ返す。
    """
    return _render_office_com_http_ex(src)[0]


def _render_office_com_upload(src: Path) -> bytes | None:
    """Office 原本をファイル本体ごと multipart 送信して PDF レンダを依頼する（upload 転送の render 版）。"""
    url = _office_com_url()
    if not url:
        return None
    try:
        file_bytes = Path(src).read_bytes()
    except OSError:
        _log.warning("office_com upload(render): 原本を読めません: %s", src)
        return None
    source_hash = hashlib.sha256(file_bytes).hexdigest()
    body, ctype = _build_multipart({"source_hash": source_hash}, "file", Path(src).name, file_bytes)
    with _convert_lock:
        return _post_multipart(url + "/render-upload", body, ctype, _timeout_sec())


def _render_office_com_via_transfer_mode(src: Path) -> bytes | None:
    """render 版の transfer_mode 解決（`_convert_office_com_via_transfer_mode` と対称・Med-2）。"""
    mode = transfer_mode_name()
    if mode == "upload":
        return _render_office_com_upload(src)
    if mode == "auto":
        data, fallback_worthy = _render_office_com_http_ex(src)
        if data is not None:
            return data
        if not fallback_worthy:
            return None            # 真の失敗（500 等）はそのまま伝播・upload へ縮退しない
        return _render_office_com_upload(src)
    return _render_office_com_http(src)                  # "path"（既定）


def render_pdf(src) -> bytes | None:
    """Office 原本（.doc/.docx/.xls/.xlsx/.ppt/.pptx）を PDF（見た目どおりの忠実レンダ）へ変換したバイト列。到達不可/失敗は None。

    Office外観確認や将来の視覚処理で使うレンダリング経路。office_com の動作形態に応じて direct（ps1
    `-RenderPdf` one-shot・既定）／http（ワーカー・`transfer_mode` で path/upload/auto を使い分け・
    OFFICE-WIN-001）を使い分ける。legacy_backend の選択とは独立（PDF レンダは旧形式変換バックエンドの設定に
    関わらず office_com 到達性だけで決まる）。対象外拡張子は None。
    """
    src = Path(src)
    if src.suffix.lower() not in _RENDER_EXTS:
        return None
    mode = office_com_mode()
    if mode == "http":
        return _render_office_com_via_transfer_mode(src)
    if mode == "direct":
        return _run_direct_job(src, "render", ".pdf", None)
    return None


# ---- office_com（http: PowerPoint 補助構造抽出・upload 限定・OFFICE-WIN-001 ⑤・試作・未配線）----

# `deploy/office-com-worker.ps1` の `$script:ExtractStructureExtMap` と対応（PowerPoint 限定）。
_EXTRACT_STRUCTURE_EXTS: frozenset[str] = frozenset({".ppt", ".pptx"})


def extract_structure_office_com_upload(src) -> dict | None:
    """PowerPoint（.ppt/.pptx）をファイル本体ごと multipart 送信し、Windows 側ワーカーの
    `/extract-structure-upload`（OFFICE-WIN-001 ⑤・PowerPoint COM による補助構造抽出の試作）から
    JSON（スライド番号・タイトル・本文テキスト・発表者ノート・非表示フラグ・図形一覧）を取得する。

    `_render_office_com_upload` と同型（http モード・upload 転送限定・原本 sha256 を `source_hash` として
    添付・直列実行は同じ `_convert_lock`）。**試作段階**＝取り込みパイプライン（office_md.py 等）へは
    未配線（呼び出し側の配線は将来スライス・`probe_office_com` と同じくここでは関数のみ提供する）。
    direct モード（同一マシン・WSL interop）には未対応（http 限定・upload 系はすべて同じ制約）。
    対象外拡張子・URL 未設定・到達不可・失敗・応答 JSON 不正はすべて None（fail-safe）。
    """
    src = Path(src)
    if src.suffix.lower() not in _EXTRACT_STRUCTURE_EXTS:
        return None
    url = _office_com_url()
    if not url:
        return None
    try:
        file_bytes = src.read_bytes()
    except OSError:
        _log.warning("office_com extract-structure: 原本を読めません: %s", src)
        return None
    source_hash = hashlib.sha256(file_bytes).hexdigest()
    body, ctype = _build_multipart({"source_hash": source_hash}, "file", src.name, file_bytes)
    with _convert_lock:
        raw = _post_multipart(url + "/extract-structure-upload", body, ctype, _timeout_sec())
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        _log.warning("office_com extract-structure: 応答 JSON が不正です: %s", src)
        return None
    return data if isinstance(data, dict) else None


# ---- office_com: Microsoft Excel 表示値補完（XLS/XLSX・upload/direct）----

def _excel_display_options(targets: dict[str, set[str]]) -> dict | None:
    """sheet/cell集合をworkerへ渡す決定的契約へ正規化する。不正座標や上限超過はfail-safeでNone。"""
    cells: list[dict[str, str]] = []
    for sheet in sorted(targets):
        if not isinstance(sheet, str) or not sheet or len(sheet) > 31:
            return None
        for raw_coordinate in sorted(targets[sheet]):
            if not isinstance(raw_coordinate, str):
                return None
            coordinate = raw_coordinate.upper()
            if not _EXCEL_CELL_REF_RE.fullmatch(coordinate):
                return None
            match = re.fullmatch(r"([A-Z]+)([0-9]+)", coordinate)
            assert match is not None
            column = 0
            for char in match.group(1):
                column = column * 26 + ord(char) - ord("A") + 1
            if column > 16_384 or int(match.group(2)) > 1_048_576:
                return None
            cells.append({"sheet": sheet, "cell": coordinate})
            if len(cells) > _MAX_EXCEL_DISPLAY_CELLS:
                return None
    return {"schema": _EXCEL_DISPLAY_SCHEMA, "cells": cells}


def _file_sha256_hex(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _validated_excel_display_response(
    raw: bytes | None,
    *,
    source_hash: str,
    requested: set[tuple[str, str]],
) -> dict | None:
    """worker応答をsource hash・target集合・安全profileまで検証し、不完全な契約をEvidenceへ混ぜない。"""
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != _EXCEL_DISPLAY_SCHEMA:
        return None
    if data.get("source_hash") != source_hash or data.get("office_app") != "excel":
        return None
    if not isinstance(data.get("worker_version"), str):
        return None
    profile = data.get("worker_profile")
    expected_profile = {
        "read_only": True,
        "macros_disabled": True,
        "update_links": 0,
        "post_open_external_refresh_disabled": True,
        "network_isolation": "deployment_required",
        "calculation": "manual_no_recalculate",
    }
    if not isinstance(profile, dict) or any(profile.get(key) != value for key, value in expected_profile.items()):
        return None
    cells = data.get("cells")
    if not isinstance(cells, list):
        return None
    seen: set[tuple[str, str]] = set()
    normalized: list[dict] = []
    for item in cells:
        if not isinstance(item, dict):
            return None
        sheet, cell = item.get("sheet"), item.get("cell")
        locator = (sheet, cell)
        if not isinstance(sheet, str) or not isinstance(cell, str) or locator not in requested or locator in seen:
            return None
        if not all(isinstance(item.get(key), str) for key in ("text", "number_format", "base_number_format",
                                                               "number_format_local", "number_format_source")):
            return None
        appearance_keys = (
            "base_font_color", "base_fill_color", "display_font_color", "display_fill_color",
        )
        appearance_present = [key in item for key in appearance_keys]
        if any(appearance_present) and not all(appearance_present):
            return None
        if all(appearance_present) and any(
            not isinstance(item[key], int) or isinstance(item[key], bool)
            for key in appearance_keys
        ):
            return None
        seen.add(locator)
        normalized_item = {
            "sheet": sheet,
            "cell": cell,
            "text": item["text"],
            "number_format": item["number_format"],
            "base_number_format": item["base_number_format"],
            "number_format_local": item["number_format_local"],
            "number_format_source": item["number_format_source"],
        }
        if all(appearance_present):
            normalized_item.update({key: item[key] for key in appearance_keys})
        normalized.append(normalized_item)
    missing = data.get("missing", [])
    if not isinstance(missing, list):
        return None
    missing_seen: set[tuple[str, str]] = set()
    normalized_missing: list[dict] = []
    for item in missing:
        if not isinstance(item, dict):
            return None
        sheet, cell, reason = item.get("sheet"), item.get("cell"), item.get("reason")
        locator = (sheet, cell)
        if (not isinstance(sheet, str) or not isinstance(cell, str) or not isinstance(reason, str)
                or locator not in requested or locator in seen or locator in missing_seen):
            return None
        missing_seen.add(locator)
        normalized_missing.append({"sheet": sheet, "cell": cell, "reason": reason})
    if seen | missing_seen != requested:
        return None
    return {
        "schema": _EXCEL_DISPLAY_SCHEMA,
        "source_hash": source_hash,
        "worker_version": data["worker_version"],
        "office_app": "excel",
        "office_version": data.get("office_version"),
        "worker_profile": {key: profile[key] for key in expected_profile},
        "cells": normalized,
        "missing": normalized_missing,
    }


def _extract_excel_display_upload(src: Path, options: dict, source_hash: str) -> bytes | None:
    url = _office_com_url()
    if not url:
        return None
    try:
        file_bytes = src.read_bytes()
    except OSError:
        return None
    fields = {
        "source_hash": source_hash,
        "cells_json": json.dumps(options, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    }
    body, ctype = _build_multipart(fields, "file", src.name, file_bytes)
    with _convert_lock:
        return _post_multipart(url + "/extract-excel-display-upload", body, ctype, _timeout_sec())


def extract_excel_display(src, targets: dict[str, set[str]]) -> dict | None:
    """XLS/XLSXの対象セルについてMicrosoft Excel ``Range.Text`` と実効書式を得る。

    HTTPモードは原本bytesをuploadし、directモードは同じps1をone-shot起動する。worker不在・失敗・
    応答改ざん/欠落はすべてNoneで、呼び出し側はLinux基本表示を維持する。
    """
    source = Path(src)
    if source.suffix.lower() not in _EXCEL_DISPLAY_EXTS or not source.is_file():
        return None
    options = _excel_display_options(targets)
    if options is None or not options["cells"]:
        return None
    try:
        source_hash = _file_sha256_hex(source)
    except OSError:
        return None
    requested = {(item["sheet"], item["cell"]) for item in options["cells"]}
    mode = office_com_mode()
    if mode == "http":
        raw = _extract_excel_display_upload(source, options, source_hash)
    elif mode == "direct":
        raw = _run_direct_job(source, "excel_display", ".json", None, options=options)
    else:
        return None
    response = _validated_excel_display_response(raw, source_hash=source_hash, requested=requested)
    if response is None:
        _log.warning("office_com excel display: 応答契約を検証できません: %s", source)
    return response


# ---- 変換可否・構成署名 ----

def legacy_exts() -> set[str]:
    """今この環境で旧→新変換できる拡張子集合（.doc/.xls/.ppt）。バックエンド none／バックエンド不達は空集合。

    RV Med（2026-07-08・office_com token 漏洩対策）: env `SHERPA_LEGACY_EXTS` が設定されていれば**最優先で
    それを信じ、以降のロジック（soffice 検出／office_com healthz 到達）は一切実行しない**。これは MCP サブ
    プロセス（`agents._mcp_env`）が親プロセスの実効値スナップショットをこの env に積んで渡すための入口で、
    MCP は office_com の URL/TOKEN を持たない（Codex sandbox 無効時の fallback 実行環境にシークレットを
    露出させないため渡さない設計＝W1 RV Med）。カンマ区切り（例 ".doc,.xls,.ppt"）・空文字列は「対象なし」。
    通常の API プロセス（MCP サブプロセスでない）ではこの env は設定されないため、下の通常ロジックのまま。

    ⚠ MD 化は①OOXML アーム経由なので、呼び出し側（`office_md.convertible_exts`）は **ooxml アーム有効時のみ**
    この集合を採用する（ここではアーム有効性は見ない＝バックエンド到達性のみ判定する）。
    """
    if "SHERPA_LEGACY_EXTS" in os.environ:
        raw = os.environ["SHERPA_LEGACY_EXTS"]
        return {e.strip() for e in raw.split(",") if e.strip()}
    backend = legacy_backend_name()
    if backend == "none":
        return set()
    if backend == "libreoffice" and not soffice_available():
        return set()
    if backend == "office_com":
        return office_com_available_exts()          # RV Med: アプリ単位でゲート（healthz probe はここでのみ実行）
    return set(LEGACY_EXT_MAP)


def legacy_sig_value() -> str:
    """アーム構成署名（`office_md._arms_sig`）に載せる**実効**バックエンド値。変換が実際に可能な時だけ backend 名。

    バックエンドを選んでも変換手段が無ければ "none"（libreoffice なら soffice 未検出・office_com ならワーカー
    不達／対応アプリ無し）＝変換不可・現状どおり。soffice/ワーカーの後付け導入やバックエンド切替、**office_com の
    利用可能アプリ集合の変化**（例 Word だけ→Word+Excel）で署名が変わり drift 再ビルドが誘発される（arms/pdf と
    同じ扱い）。soffice/Office の**バージョン**は署名に含めない（版更新での不要な全リビルドを避ける・provenance
    には残す）。office_com は `"office_com:<ソート済みアプリ名カンマ区切り>"`（例 `office_com:excel,word`）。
    """
    backend = legacy_backend_name()
    if backend == "libreoffice" and soffice_available():
        return "libreoffice"
    if backend == "office_com":
        apps = _office_com_available_apps()
        if apps:
            return "office_com:" + ",".join(sorted(apps))
        return "none"
    return "none"


# ---- 変換本体 ----

def convert_to_ooxml(src: Path, target_ext: str) -> bytes | None:
    """旧形式 `src` を新形式（`target_ext`＝.docx/.xlsx/.pptx）へ変換したバイト列。変換不可/失敗は None。

    バックエンド＝libreoffice（WSL 内 soffice）｜office_com（direct＝WSL interop の ps1 one-shot・既定／
    http＝Windows 側ワーカーへ HTTP・`transfer_mode` で path/upload/auto を使い分け・OFFICE-WIN-001）。
    office_com のモードは `office_com_mode()`（URL 設定時 http・未設定かつ powershell 検出時 direct・
    どちらも無ければ unavailable＝None）。例外は握って None（fail-safe）。
    """
    backend = legacy_backend_name()
    if backend == "libreoffice":
        return _convert_libreoffice(Path(src), target_ext)
    if backend == "office_com":
        mode = office_com_mode()
        if mode == "http":
            return _convert_office_com_via_transfer_mode(Path(src), target_ext)
        if mode == "direct":
            return _convert_office_com_direct(Path(src), target_ext)
        return None                   # unavailable（URL 未設定かつ powershell 未検出）
    return None                       # none は変換しない


def _build_convert_cmd(bin_path: str, fmt: str, outdir, profile, src: Path) -> list[str]:
    """soffice の変換コマンド列を組み立てる（実行しない・単体でテスト可能に切り出し）。

    RV Med（2026-07-08）: soffice は `-env:UserInstallation` を URL としてパースするため、パスを
    そのまま `file://` に埋め込むと空白等を含む場合に誤解釈されうる。`Path.as_uri()` で正規に
    percent-encode する（`tempfile.mkdtemp` は常に絶対パスを返すので `as_uri()` の前提を満たす）。
    """
    return [bin_path, "--headless", "--convert-to", fmt, "--outdir", str(outdir),
            f"-env:UserInstallation={Path(profile).as_uri()}", str(Path(src).resolve())]


def _convert_libreoffice(src: Path, target_ext: str) -> bytes | None:
    bin_path = _soffice_bin()
    if not bin_path:
        return None
    fmt = target_ext.lstrip(".")      # "docx"/"xlsx"/"pptx"
    profile = tempfile.mkdtemp(prefix="sherpa-lo-profile-")   # プロファイル分離（一時 dir・毎回破棄）
    outdir = tempfile.mkdtemp(prefix="sherpa-lo-out-")
    try:
        cmd = _build_convert_cmd(bin_path, fmt, outdir, profile, src)
        with _convert_lock:           # 直列実行（LibreOffice の並列不安定を避ける）
            if not _run_soffice(cmd, src):
                return None
        # soffice は <outdir>/<stem>.<fmt> に出力する。名前ゆらぎに備え target_ext のファイルも拾う。
        expected = Path(outdir) / (src.stem + target_ext)
        out = expected if expected.is_file() else next(
            (p for p in sorted(Path(outdir).glob("*" + target_ext)) if p.is_file()), None)
        if out is None:
            _log.warning("legacy 変換の出力が見つかりません: %s", src)
            return None
        return out.read_bytes()
    finally:
        shutil.rmtree(profile, ignore_errors=True)
        shutil.rmtree(outdir, ignore_errors=True)


def _run_soffice(cmd: list[str], src: Path) -> bool:
    """soffice を実行し成功/失敗を返す。タイムアウト/非0終了/起動失敗はすべて False（fail-safe）。

    RV High（2026-07-08）: `subprocess.run(timeout=)` は直接の子プロセスしか kill しない。soffice は
    wrapper スクリプト→`soffice.bin` の多段起動のため、タイムアウト時に孫プロセスが生き残り、その後
    `finally` で profile/outdir を rmtree すると、残った soffice が消えたディレクトリを掴んだまま
    残骸プロセスになる。`start_new_session=True`（独立プロセスグループ）＋タイムアウト時
    `os.killpg(...,SIGKILL)` でグループ全体を確実に停止する。
    """
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)
    except OSError as e:
        _log.warning("legacy 変換を実行できませんでした（%s）: %s", e.__class__.__name__, src)
        return False
    try:
        proc.communicate(timeout=_timeout_sec())
    except subprocess.TimeoutExpired:
        _log.warning("legacy 変換がタイムアウトしました（%ss）: %s", _timeout_sec(), src)
        _kill_process_group(proc)
        _note_conversion_failure_reason("timeout")
        return False
    if proc.returncode != 0:
        _log.warning("legacy 変換が異常終了しました（rc=%s）: %s", proc.returncode, src)
        return False
    return True


def _kill_process_group(proc: subprocess.Popen) -> None:
    """タイムアウトした soffice のプロセスグループを丸ごと停止する（wrapper→soffice.bin の子孫を含む）。

    `start_new_session=True` で作った独立グループを `os.killpg` で一括 SIGKILL する。既に終了済み
    （`ProcessLookupError`）は握る（fail-safe）。kill 後にパイプを読み切ってから wait（zombie 化防止・
    子孫が pipe fd を握っていても、グループ全体が死ぬことで fd が閉じ communicate は速やかに戻る）。
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.communicate(timeout=5)
    except Exception:
        proc.wait()


# ---- キャッシュ（原本 mtime/size キーで再変換を省く・derived/{world}/_legacy_cache）----

_CACHE_DIRNAME = "_legacy_cache"


def cache_root_for(derived_md_dir) -> Path:
    """派生MD dir（derived/{world}/md）と同階層の legacy 変換キャッシュ dir（derived/{world}/_legacy_cache）。

    build_derived は md/ を全消去して作り直すが、キャッシュはその**兄弟**（semantic 等と同様）に置くため
    再ビルドをまたいで残る（soffice 再実行を省く）。world 削除時は derived/{world} 木ごと消える＝鏡と整合。
    """
    parent = Path(derived_md_dir).parent
    # generation layoutでは `derived/{world}/md-generations/<id-or-stage>` が派生MD dir。
    # cacheをgenerationの外へ出し、公開切替・古いgeneration掃除をまたいで再利用する。
    if parent.name == "md-generations":
        parent = parent.parent
    return parent / _CACHE_DIRNAME


def _source_key(src: Path) -> str:
    """キャッシュの変更検知キー（backend:mode:size:mtime_ns）。原本が変わるか**バックエンドを切り替えたら**再変換する
    （backend を含めないと、W1 で office_com へ切替後も LibreOffice 産キャッシュがヒットし続け、provenance の
    backend 名と実際の変換元が食い違う）。

    RV Med（2026-07-08・W2'）: office_com は backend 名が同じ "office_com" のままでも、動作形態
    （`office_com_mode()`＝http／direct）が切り替わると実際の変換元（別ホストの Office／同一マシンの Office）が
    変わり、provenance（`office_com_versions=...`）も変わりうる。backend 名だけをキーにすると http→direct
    （またはその逆）の切替後も旧モード産キャッシュがヒットし続けてしまうため、backend が office_com のときだけ
    実効モードもキーへ混ぜる（他バックエンドはモード概念が無いため空欄のまま）。
    """
    st = src.stat()
    backend = legacy_backend_name()
    mode = office_com_mode() if backend == "office_com" else ""
    return f"{backend}:{mode}:{st.st_size}:{st.st_mtime_ns}"


def ensure_ooxml(src, rel: str, cache_root):
    """旧形式 `src` の変換済み OOXML を用意し `(ooxml_path, notes)` を返す。変換不可/失敗は None。

    キャッシュ（`cache_root/{rel}{target_ext}`）が原本の mtime/size と一致すれば再変換せず再利用し、
    さもなくばバックエンドで変換して保存する。`notes` は provenance（meta.json）へ足す来歴
    （`legacy_backend=<name>`・soffice バージョン）。

    冒頭で `take_conversion_failure_reason()` を読み捨てる（直前の無関係な呼び出しの残留理由を
    混ぜない）——このあと実際に `convert_to_ooxml()` を呼んだ場合だけ、その呼び出し中の失敗理由が
    改めてセットされる（呼ばない/キャッシュ命中で戻る経路は理由 None のまま）。
    """
    take_conversion_failure_reason()
    src = Path(src)
    target_ext = LEGACY_EXT_MAP.get(src.suffix.lower())
    if target_ext is None:
        return None                   # W0 対象外の拡張子
    cache_path = Path(cache_root) / (rel + target_ext)
    key_path = Path(str(cache_path) + ".key")
    try:
        want = _source_key(src)
    except OSError:
        return None
    # キャッシュヒット（原本 unchanged）＝再変換しない（soffice を再実行しない）。
    if cache_path.is_file() and key_path.is_file():
        try:
            if key_path.read_text(encoding="utf-8").strip() == want:
                return cache_path, _notes()
        except OSError:
            pass
    data = convert_to_ooxml(src, target_ext)
    if data is None:
        return None
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(data)
        key_path.write_text(want, encoding="utf-8")
    except OSError:
        return None
    return cache_path, _notes()


def drop_cache_entry(cache_root, rel: str) -> bool:
    """`rel` の旧→新変換キャッシュ（`ensure_ooxml` が書く OOXML/`.key`）を落とす（**明示的な再変換**用）。

    次回の `ensure_ooxml` 呼び出しで原本 mtime/size が不変でもキャッシュヒットさせず、必ず
    `convert_to_ooxml` を再実行させる（安定して壊れた OOXML キャッシュ——原本自体は変わっていない
    のに一度キャッシュした変換結果が壊れている場合——を再利用し続けない）。対象拡張子
    （`LEGACY_EXT_MAP`）でなければ no-op（False）。キャッシュが元々無い場合も成功扱い（True）。
    """
    target_ext = LEGACY_EXT_MAP.get(Path(rel).suffix.lower())
    if target_ext is None:
        return False
    cache_path = Path(cache_root) / (rel + target_ext)
    key_path = Path(str(cache_path) + ".key")
    try:
        cache_path.unlink(missing_ok=True)
        key_path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def _notes() -> list[str]:
    """provenance（meta.json）へ足す来歴。backend 名＋変換元エンジンのバージョン要約。

    libreoffice なら `soffice=<version>`、office_com なら healthz の各 Office バージョン要約
    （`office_com_versions=word=16.0,excel=16.0`）を残す（同じ OOXML → 同じ MD なので決定性はキャッシュ後の
    ①MD化で担保・バージョンは追跡目的のみ）。
    """
    backend = legacy_backend_name()
    notes = [f"legacy_backend={backend}"]
    if backend == "libreoffice":
        ver = soffice_version()
        if ver:
            notes.append(f"soffice={ver}")
    elif backend == "office_com":
        summary = _office_com_versions_summary()
        if summary:
            notes.append(f"office_com_versions={summary}")
    return notes
