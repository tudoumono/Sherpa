"""Marp レンダの外出し。Codex は .md を書くだけにし、レンダ（HTML/PDF/PPTX）は Codex 完了後に Sherpa 本体が行う。

pdf/pptx は Chromium を使うため `unshare -rn`（ネットワーク隔離）配下で実行する（隔離できない環境では pdf/pptx をスキップし、
html と .md だけを成果物にする＝fail-closed）。html は隔離不要。
設計: docs/design/chat.md「ファイル作成（個人の作業領域への成果物）」
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

_log = logging.getLogger("sherpa")

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?\n)---\s*(\n|$)", re.DOTALL)
_MARP_TRUE_RE = re.compile(r"^marp:\s*true\s*$", re.MULTILINE)

# 出力形式ごとの marp CLI フラグと拡張子（html は隔離不要・pdf/pptx は必須）
_FORMATS = (
    ("html", [], False),
    ("pdf", ["--pdf"], True),
    ("pptx", ["--pptx"], True),
)


def is_marp_markdown(path: Path) -> bool:
    """先頭の YAML front-matter に `marp: true` があれば True（壊れた・存在しないファイルは False）。"""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return False
    try:
        text = head.decode("utf-8", errors="replace")
    except Exception:
        return False
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return False
    return bool(_MARP_TRUE_RE.search(m.group(1)))


def _unshare_available() -> bool:
    """`unshare -rn` でネットワーク隔離ができるかのプローブ。netns 内で lo を UP にできるところまで検証し、できなければ False。"""
    if shutil.which("unshare") is None:
        return False
    try:
        r = subprocess.run(
            ["unshare", "-rn", "sh", "-c", "ip link set lo up"], timeout=10,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return r.returncode == 0
    except Exception:
        return False


def _resolve_theme_dir(theme_dirs: list[Path]) -> Path | None:
    """渡された順に見て、実在する最初のディレクトリを返す（無ければ None＝marp 既定テーマ）。"""
    for d in theme_dirs:
        try:
            if d.is_dir():
                return d
        except OSError:
            continue
    return None


def _marp_argv(marp_bin: str, src: Path, out: Path, fmt_flags: list[str], theme_dir: Path | None) -> list[str]:
    # `--allow-local-files` は付けない（Codex が書いた MD 内の `file:///...` 経由でローカルファイルを成果物へ埋め込めてしまうため）
    argv = [marp_bin, str(src), "--no-stdin"]
    if theme_dir is not None:
        argv += ["--theme-set", str(theme_dir)]
    argv += fmt_flags
    argv += ["-o", str(out)]
    return argv


def _run_render(argv: list[str], *, needs_network_isolation: bool, env: dict, cwd: Path, timeout: int) -> bool:
    """1形式分のレンダを実行。成功で True。失敗（非ゼロ終了・timeout・例外）は warning ログを出して False（例外は漏らさない）。"""
    if needs_network_isolation:
        # 新規 netns は loopback が DOWN のため `ip link set lo up` してから marp を exec する
        full_argv = ["unshare", "-rn", "sh", "-c",
                     'ip link set lo up 2>/dev/null; exec "$@"', "sh", *argv]
    else:
        full_argv = argv
    try:
        r = subprocess.run(
            full_argv, env=env, cwd=str(cwd), timeout=timeout,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except Exception as e:
        _log.warning("marp_render: レンダ失敗（%s）: %s", " ".join(full_argv[:3]), e)
        return False
    if r.returncode != 0:
        _log.warning(
            "marp_render: marp が非ゼロ終了（%s）: %s",
            r.returncode, (r.stderr or b"").decode("utf-8", errors="replace")[:500])
        return False
    return True


def render_outputs(
    md_paths: list[Path], *, marp_bin: str | None, chrome_path: str | None,
    theme_dirs: list[Path], containment_root: Path, timeout: int = 180,
) -> list[Path]:
    """marp な .md それぞれについて html/pdf/pptx を同ディレクトリ・同 stem で生成する。

    marp 未導入なら即 `[]`（.md のみが成果物）。同名の出力が既にある形式はスキップ（上書き禁止）。pdf/pptx はネットワーク隔離が
    使えない環境ではスキップ。個々の失敗は他に波及させない。
    入出力とも `containment_root`（authoring）内の実体であることを強制する（src は symlink・root 外解決を拒否、出力先は
    symlink（dangling 含む）が居座っていれば拒否）。
    """
    if not marp_bin or not Path(marp_bin).is_file():
        return []

    try:
        root_resolved = containment_root.resolve()
    except OSError:
        return []
    theme_dir = _resolve_theme_dir(theme_dirs)
    network_ok = None  # 遅延評価＋一度だけ判定（同じ警告を連呼しない）
    warned_network = False
    warned_chrome = False
    rendered: list[Path] = []

    for src in md_paths:
        try:
            if src.is_symlink() or not src.is_file():
                continue
            if not src.resolve().is_relative_to(root_resolved):
                continue  # authoring 外へ解決される src は扱わない
        except OSError:
            continue
        for fmt, flags, needs_network in _FORMATS:
            out = src.with_suffix(f".{fmt}")
            if out.is_symlink() or out.exists():
                continue  # 既存出力・symlink（dangling 含む）へは書かない
            if needs_network:
                if not chrome_path:  # Chromium 不在＝pdf/pptx は生成不可
                    if not warned_chrome:
                        _log.warning(
                            "marp_render: CHROME_PATH（Chromium）が未解決のため pdf/pptx をスキップ"
                            "（html/.md のみ生成）")
                        warned_chrome = True
                    continue
                if network_ok is None:
                    network_ok = _unshare_available()
                if not network_ok:
                    if not warned_network:
                        _log.warning(
                            "marp_render: unshare によるネットワーク隔離が使えないため pdf/pptx をスキップ"
                            "（html/.md のみ生成）")
                        warned_network = True
                    continue
            env = dict(os.environ)
            if chrome_path:
                env["CHROME_PATH"] = chrome_path
            argv = _marp_argv(marp_bin, src, out, flags, theme_dir)
            ok = _run_render(
                argv, needs_network_isolation=needs_network, env=env,
                cwd=src.parent, timeout=timeout)
            if ok and out.is_file():
                rendered.append(out)

    return rendered
