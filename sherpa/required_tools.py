"""アプリが頼る外部の道具（LibreOffice・OCR ワーカー等）の導入状況の一覧。

検出は各機能が既に使っている関数を再利用する（ここで検出ロジックを複製しない）。管理画面
（GET /admin/settings の `required_tools`）と `make doctor` が同じ結果を見る。
検出はプロセス起動・DB 参照を伴うため結果は短時間キャッシュする（`snapshot(force=True)` で無効化）。
Docker は見えないため OCR ワーカーのコードの版は扱わない（`make status` が表示する）。
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import threading
import time
from pathlib import Path

_TTL_SEC = 30.0
_cache: tuple[float, list[dict]] | None = None
_lock = threading.Lock()
_ROOT = Path(__file__).resolve().parents[1]


def _is_linux() -> bool:
    return platform.system() == "Linux"


def _local_tool(*rel: str) -> str | None:
    p = _ROOT.joinpath("tools", "codex", *rel)
    return str(p) if p.is_file() and os.access(str(p), os.X_OK) else None


def _first_line(cmd: list[str]) -> str | None:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    lines = (out or "").strip().splitlines()
    return lines[0].strip() if lines else None


def _row(tid: str, label: str, installed: bool, used_by: list[str], how: str,
         version: str | None = None, detail: str | None = None) -> dict:
    return {"id": tid, "label": label, "installed": bool(installed), "version": version if installed else None,
            "used_by": used_by, "how_to_install": how, "detail": detail}


def _libreoffice() -> dict:
    from sherpa.ingest.arms import legacy_convert
    ok = legacy_convert.soffice_available()
    return _row("libreoffice", "LibreOffice", ok,
                ["古い形式（.doc / .xls / .ppt）の変換", "図（WMF / EMF）の読み取り"],
                "Linux: sudo apt-get install -y libreoffice / macOS: brew install --cask libreoffice",
                legacy_convert.soffice_version() if ok else None)


def _office_com() -> dict:
    from sherpa.ingest.arms import legacy_convert
    ok = bool(legacy_convert.office_com_available())
    return _row("office_com", "Windows の Office 連携", ok, ["古い形式（.doc / .xls / .ppt）の変換（Office 連携）"],
                "Windows の Office がある環境で使えます（別のマシンの場合は deploy/office-com-worker.ps1 を起動）")


def _ocr_worker() -> dict:
    uses = ["画像内の文字の読み取り（OCR）"]
    how = "make ocr-models のあと make up（コードの版は make status で確認できます）"
    try:
        from sherpa.ingest import ocr_worker
        from sherpa.store import ocr_jobs
        summary = ocr_jobs.worker_availability_summary(ocr_worker.profile_hash())
    except Exception:
        return _row("ocr_worker", "OCR ワーカー", False, uses, how, detail="状態を確認できませんでした")
    if summary.get("available"):
        return _row("ocr_worker", "OCR ワーカー", True, uses, how)
    reason = summary.get("unavailable_reason") or "worker_not_seen"
    return _row("ocr_worker", "OCR ワーカー", False, uses, how, detail=f"理由: {reason}")


def _marp() -> dict:
    from sherpa.providers.codex import sandbox
    return _row("marp", "スライド変換ツール（marp）", sandbox._marp_bin() is not None,
                ["スライドの PDF / PowerPoint 出力"],
                "cd tools/marp && npm install（Node.js が必要）")


def _chromium() -> dict:
    from sherpa.providers.codex import sandbox
    return _row("chromium", "Chromium", sandbox._detect_chrome_path() is not None,
                ["スライドの PDF / PowerPoint 出力"],
                "npx playwright install chromium（または環境変数 CHROME_PATH に実行ファイルを指定）")


_NPM_TRIPLES = {   # scripts/lib/codex_pin.sh::codex_pin_path_codex_has_accessories と同じ対応
    ("Linux", "x86_64"): "x86_64-unknown-linux-musl",
    ("Linux", "aarch64"): "aarch64-unknown-linux-musl",
    ("Darwin", "arm64"): "aarch64-apple-darwin",
    ("Darwin", "x86_64"): "x86_64-apple-darwin",
}
_CODEX_WARN = "Codex のサンドボックス/検索の部品が見つかりません（Codex の実行が失敗することがあります）"


def _codex_exe() -> str | None:
    return shutil.which("codex") or _local_tool("bin", "codex")


def _codex_roots(exe: str) -> list[Path]:
    """codex 実行ファイルから見た付属物のルート候補（`codex-resources/`・`codex-path/` を持つ親）。
    Codex 自身が実行ファイル相対で探す配置と、npm 版（実体が .js）の同梱 vendor ディレクトリ。"""
    real = Path(os.path.realpath(exe))
    roots = [real.parent.parent]
    if real.suffix == ".js":
        triple = _NPM_TRIPLES.get((platform.system(), platform.machine()))
        if triple:
            roots.extend(sorted(real.parent.parent.glob(f"node_modules/@openai/codex-*/vendor/{triple}")))
    roots.append(_ROOT / "tools" / "codex")
    return roots


def _find_accessory(exe: str | None, rel: tuple[str, ...], path_name: str) -> str | None:
    for root in (_codex_roots(exe) if exe else [_ROOT / "tools" / "codex"]):
        p = root.joinpath(*rel)
        if p.is_file() and os.access(str(p), os.X_OK):
            return str(p)
    return shutil.which(path_name)


def codex_cli_missing() -> bool:
    """Codex 調査に必須なのは codex 本体だけ（bwrap・rg は付属物＝見つからなくても構成は選べる）。"""
    return _codex_exe() is None


def codex_cli_missing_message() -> str | None:
    return "Codex CLI が入っていません" if codex_cli_missing() else None


def _codex_parts() -> list[dict]:
    uses = ["Codex 調査"]
    exe = _codex_exe()
    rows = [_row("codex", "Codex CLI", exe is not None, uses, "make codex-install",
                 _first_line([exe, "--version"]) if exe else None)]
    if _is_linux():
        bwrap = _find_accessory(exe, ("codex-resources", "bwrap"), "bwrap")
        rows.append(_row("bwrap", "bubblewrap（Codex の安全な実行）", bwrap is not None, uses,
                         "make codex-install（同梱）／ sudo apt-get install -y bubblewrap",
                         detail=None if bwrap else _CODEX_WARN))
    rg = _find_accessory(exe, ("codex-path", "rg"), "rg")
    rows.append(_row("ripgrep", "ripgrep（rg）", rg is not None, uses,
                     "make codex-install（同梱）／ sudo apt-get install -y ripgrep ／ macOS: brew install ripgrep",
                     detail=None if rg else _CODEX_WARN))
    return rows


def _collect() -> list[dict]:
    rows = [_libreoffice(), _office_com(), _ocr_worker(), _marp(), _chromium()]
    rows.extend(_codex_parts())
    return rows


def snapshot(*, force: bool = False) -> list[dict]:
    """全道具の導入状況 `[{id,label,installed,version,used_by,how_to_install,detail}]`。"""
    global _cache
    with _lock:
        now = time.monotonic()
        if not force and _cache is not None and now - _cache[0] < _TTL_SEC:
            return [dict(r) for r in _cache[1]]
        rows = _collect()
        _cache = (now, rows)
        return [dict(r) for r in rows]
