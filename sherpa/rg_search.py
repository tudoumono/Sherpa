"""grep の候補発見に使う ripgrep の呼び出し（read-only）。ファイルの中身は読まず、候補のファイルだけを返す。
rg は「どのファイルに当たりがあるか」を速く絞るためだけに使い、ヒットの確定（行・節・前後行・秘匿・上限）は `grep_tool` が Python で行う。
rg の探索順: Codex 同梱の rg（`sandbox._codex_bundled_rg_dir` と同じ規則）→ リポジトリの `tools/codex` 配下の同梱 rg → PATH 上の rg。どれも無ければ None（呼び出し元が純 Python 走査に縮退する）。
設計: docs/design/rag.md「読み取り部品の道具」
"""
from __future__ import annotations

import base64
import functools
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time

from . import scope_infer

_log = logging.getLogger("sherpa")

# Python の `str.splitlines()` が行の区切りとみなす文字。含む検索語は 1 行に当たり得ない（rg には渡さない）。
_LINE_BREAK_CHARS = "\n\r\v\f\x1c\x1d\x1e\x85  \x00"


class RgUnavailable(Exception):
    """rg を使えない（見つからない・起動できない・途中で失敗した）。呼び出し元が純 Python 走査に縮退する。"""


class RgDeadlineExceeded(Exception):
    """rg の走査中に期限を超えた（呼び出し元が `GrepDeadlineExceeded` に翻訳する）。"""


class Budget:
    """rg の走査に使える残り時間（秒）。複数回の rg 起動で共有する。`seconds=None` は無期限。"""

    def __init__(self, seconds: float | None):
        self._seconds = seconds
        self._t0 = time.perf_counter()

    def remaining(self) -> float | None:
        if self._seconds is None:
            return None
        return max(0.0, self._seconds - (time.perf_counter() - self._t0))


def _bundled_rg() -> str | None:
    """Codex 同梱の rg。導入先の探し方は `sandbox._codex_bundled_rg_dir()`（`shutil.which("codex")` 起点）と同じで、PATH に codex が無い配置のためにリポジトリ内の `tools/codex` も見る。"""
    try:
        from .providers.codex import sandbox
        d = sandbox._codex_bundled_rg_dir()
        if d is not None:
            return str(d / "rg")
    except Exception:  # noqa: BLE001 — 同梱の探索に失敗しても PATH の rg へ進む
        pass
    from . import worlds
    root = worlds._repo_root() / "tools" / "codex"
    for cand in [root / "codex-path" / "rg", *sorted(root.glob("**/vendor/*/codex-path/rg"))]:
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


@functools.lru_cache(maxsize=1)
def find_rg() -> str | None:
    """使う rg の実行ファイル。見つからなければ None（プロセス内で 1 回だけ探す）。"""
    return _bundled_rg() or shutil.which("rg")


def usable_query(q: str) -> bool:
    """rg に渡せる検索語か（行の区切りを含む語は 1 行に当たり得ないため純 Python 側に任せる）。"""
    return bool(q) and not any(c in q for c in _LINE_BREAK_CHARS)


def _base_cmd(rg: str, *, glob_md: bool, exclude_exts=()) -> list[str]:
    cmd = [rg, "--json", "-F", "-i", "--no-config", "--hidden", "--no-ignore"]
    for name in sorted(scope_infer.VCS_DIR_NAMES):
        cmd += ["-g", f"!{name}/"]
    for ext in exclude_exts:  # 拡張子だけの名前（`.pdf`）は除外に含めない
        cmd += ["--iglob", f"!?*{ext}"]
    if glob_md:
        cmd += ["--iglob", "*.md", "--iglob", "*.markdown"]
    return cmd


def _decode_path(path: dict) -> str | None:
    if "text" in path:
        return path["text"]
    if "bytes" in path:
        try:
            return os.fsdecode(base64.b64decode(path["bytes"]))
        except (ValueError, TypeError):
            return None
    return None


def _error_paths(err) -> list[str]:
    """rg が標準エラーに出した `rg: <パス>: <理由>` の行から、読めなかった通常ファイルのパスを取り出す（フォルダは含めない）。"""
    err.seek(0)
    out = []
    for raw in err.read().splitlines():
        line = os.fsdecode(raw)
        if not line.startswith("rg: ") or ": " not in line[4:]:
            continue
        path = line[4:line.rindex(": ")]
        if os.path.isfile(path) and not os.path.islink(path):
            out.append(path)
    return out


def _run(cmd: list[str], budget: Budget | None, unreadable: list):
    """rg を走らせ、JSON の match イベントを順に返す。正常終了（summary イベントを受信）しなければ `RgUnavailable`。
    rg が読めなかったファイルのパスを `unreadable` に足す（呼び出し元が候補に加え、純 Python 走査と同じ「読めなかったファイル」の扱いにする）。
    """
    err = tempfile.TemporaryFile()
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err)
    except OSError as e:
        err.close()
        raise RgUnavailable(f"rg を起動できません ({type(e).__name__})") from e
    killed = threading.Event()
    timer = None
    remaining = budget.remaining() if budget is not None else None
    if remaining is not None:
        timer = threading.Timer(remaining, lambda: (killed.set(), proc.kill()))
        timer.daemon = True
        timer.start()
    summary = False
    try:
        for raw in proc.stdout:
            if raw.startswith(b'{"type":"match"'):
                try:
                    yield json.loads(raw)["data"]
                except (ValueError, KeyError):
                    continue
            elif raw.rstrip().endswith(b'"type":"summary"}'):
                summary = True
    finally:
        if timer is not None:
            timer.cancel()
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()
        unreadable.extend(_error_paths(err))
        err.close()
    if killed.is_set():
        raise RgDeadlineExceeded("rg の走査が期限を超えました")
    if not summary:
        raise RgUnavailable(f"rg が正常に終わりませんでした (rc={proc.returncode})")


def discover(terms: list[str], roots, *, derived: bool, budget: Budget | None = None,
             first_only: bool = False, exclude_exts=()) -> dict[str, dict[int, int]]:
    """`roots`（フォルダの列）を rg で走査し、`terms` のどれかを含む行があるファイルを `{パス: {語の添字: 最初の行番号}}` で返す。
    `derived=True` は派生 MD の木（`.md`／`.markdown` だけ・UTF-8 のみ）。それ以外は原本の木で、非 ASCII の語があるときだけ CP932 の 2 回目も走らせる。
    `first_only=True` は 1 語専用で、ファイルごとに最初の 1 行で止める（語の添字は 0）。複数語は行の本文から語ごとの当たりを判定し、判定できない行はすべての語に当たったものとして残す（候補は漏らさない）。
    `exclude_exts`（`.pdf` のような拡張子の列）に一致するファイルは歩かない。呼び出し元が、同じ綴りのフォルダが木に無いことを確かめてから渡す（フォルダにも当たり中身ごと消えるため）。
    ASCII の語は大文字小文字を区別せず探す。非 ASCII の畳み方は rg の簡易ケースフォールディングで、Python の `lower()` とは一部異なる（確定は呼び出し元が Python で行う）。
    """
    rg = find_rg()
    if rg is None:
        raise RgUnavailable("rg が見つかりません")
    roots = [str(r) for r in roots]
    if not roots:
        return {}
    lowered = [t.lower() for t in terms]
    passes: list[list[str]] = [[]]
    if not derived and any(not t.isascii() for t in terms):
        passes.append(["-E", "sjis"])
    out: dict[str, dict[int, int]] = {}
    for extra in passes:
        cmd = _base_cmd(rg, glob_md=derived, exclude_exts=exclude_exts) + extra
        if first_only:
            cmd += ["-m", "1"]
        for t in terms:
            cmd += ["-e", t]
        cmd += ["--", *roots]
        unreadable: list = []
        for data in _run(cmd, budget, unreadable):
            path = _decode_path(data.get("path") or {})
            if path is None:
                continue
            line_no = data.get("line_number") or 0
            slot = out.setdefault(path, {})
            if first_only:
                slot.setdefault(0, line_no)
                continue
            text = (data.get("lines") or {}).get("text")
            text_l = text.lower() if text is not None else None
            hit = [i for i, tl in enumerate(lowered) if text_l is not None and tl in text_l]
            for i in (hit or range(len(terms))):
                slot.setdefault(i, line_no)
        for path in unreadable:  # 読めなかったファイルも候補に残す（開いて読めないことを純 Python 走査と同じ形で申告するため）
            slot = out.setdefault(path, {})
            for i in range(len(terms)):
                slot.setdefault(i, 0)
    return out


def list_files_named(roots, name: str, *, budget: Budget | None = None) -> list[str]:
    """`roots` 配下の、ファイル名が `name` に一致するファイルのパス（重要度の設定ファイルの発見用）。"""
    rg = find_rg()
    if rg is None:
        raise RgUnavailable("rg が見つかりません")
    roots = [str(r) for r in roots]
    if not roots:
        return []
    cmd = [rg, "--files", "--no-config", "--no-messages", "--hidden", "--no-ignore", "-0"]
    for vcs in sorted(scope_infer.VCS_DIR_NAMES):
        cmd += ["-g", f"!{vcs}/"]
    cmd += ["-g", name, "--", *roots]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as e:
        raise RgUnavailable(f"rg を起動できません ({type(e).__name__})") from e
    killed = threading.Event()
    timer = None
    remaining = budget.remaining() if budget is not None else None
    if remaining is not None:
        timer = threading.Timer(remaining, lambda: (killed.set(), proc.kill()))
        timer.daemon = True
        timer.start()
    try:
        raw = proc.stdout.read()
    finally:
        if timer is not None:
            timer.cancel()
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()
    if killed.is_set():
        raise RgDeadlineExceeded("rg の走査が期限を超えました")
    found = [os.fsdecode(p) for p in raw.split(b"\0") if p]
    if proc.returncode not in (0, 1) and not found:
        raise RgUnavailable(f"rg が正常に終わりませんでした (rc={proc.returncode})")
    return found
