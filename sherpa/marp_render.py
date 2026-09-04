"""Marp レンダの外出し（M3 案2・docs/proposals/2026-07-07-Marpスライド作成.md）。

M2 まで（RUNTIME-SANDBOX §9/§10）は Codex 自身が sandbox 内で marp CLI を叩いていたが、
`network.enabled=false` の permission profile 下では PDF/PPTX に必須の Chromium crashpad
ソケットが EPERM になり通らないことが実機で判明した（§10.3）。M3 案2 では **Codex は
.md を書くだけ**にし、レンダ（HTML/PDF/PPTX）は Codex 完了後に **Sherpa 本体プロセス**が
`unshare -rn`（ネットワーク名前空間を分離した root マップ）配下で実行する
（§9 の M1 実証条件＝`ip link set lo up` してから marp を起動すれば遮断下でも成功する、に準拠）。
これにより Codex サンドボックスに marp/Chromium を read root として見せる必要が無くなり、
攻撃面が縮小する（`_marp_permission_roots`/`_marp_env` は agents.py から撤去）。

html はブラウザ（Chromium）を使わないので network 隔離は不要。pdf/pptx は Chromium を
起動するため隔離必須（fail-closed: unshare が使えない・プローブに失敗する環境では
pdf/pptx をスキップし、html と .md だけを成果物にする）。
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

# 出力形式ごとの marp CLI フラグと拡張子。html は network 隔離不要・pdf/pptx は必須。
_FORMATS = (
    ("html", [], False),
    ("pdf", ["--pdf"], True),
    ("pptx", ["--pptx"], True),
)


def is_marp_markdown(path: Path) -> bool:
    """先頭の YAML front-matter に `marp: true` があれば True。壊れたファイル・
    存在しないファイルは例外を出さず False を返す（呼び出し側の一括判定を止めない）。"""
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
    """`unshare -rn` でネットワーク隔離ができるかのプローブ（fail-closed の判定用）。
    バイナリが無い、または実際に呼んで失敗する環境（capability 不足等）では False。
    RV Med（2026-07-12）: 実運用の必須条件は「netns 内で lo を UP にできる」こと（M1 実証）なので、
    プローブも `unshare -rn true` でなく **lo UP まで**を検証する（`ip` 不在環境の見逃し防止）。"""
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
    # RV High（2026-07-12）: `--allow-local-files` は**付けない**。付けると Codex が書いた MD 内の
    # `file:///...` 参照経由で、Sherpa 本体の読取権限にある任意ローカルファイルを成果物へ埋め込めて
    # しまう（持ち出し面）。代償はスライド内のローカル画像参照が描画されないことだけ（テーマ CSS は
    # marp 本体が --theme-set で読むため影響なし・変換自体は警告付きで完走する）。
    argv = [marp_bin, str(src), "--no-stdin"]
    if theme_dir is not None:
        argv += ["--theme-set", str(theme_dir)]
    argv += fmt_flags
    argv += ["-o", str(out)]
    return argv


def _run_render(argv: list[str], *, needs_network_isolation: bool, env: dict, cwd: Path, timeout: int) -> bool:
    """1形式分のレンダを実行。成功で True。失敗（非ゼロ終了・timeout・実行時例外）は
    warning ログを出して False を返す（呼び出し元へ例外を漏らさない＝防御的）。"""
    if needs_network_isolation:
        # RUNTIME-SANDBOX §9 の M1 実証条件どおり: 新規 netns は loopback が DOWN のため
        # `ip link set lo up` してから marp を exec する（無いと PDF/PPTX が websocket ErrorEvent で失敗）。
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

    marp 未導入（marp_bin が None/不存在）なら即 `[]`（.md のみが成果物＝正常系・fail-open）。
    同名の出力が既に存在する形式はスキップ（上書き禁止）。pdf/pptx は unshare によるネットワーク
    隔離が使えない環境ではスキップ（html と .md は出す・fail-closed）。個々のファイル/形式の失敗は
    他のファイル/形式に波及させない。

    RV BLOCKER（2026-07-12）: 入出力とも `containment_root`（＝authoring）内に**実体**があることを
    強制する。src は symlink・root 外解決を拒否し、出力先は「symlink（dangling 含む）が既に居座って
    いる」場合を拒否する — Codex が `deck.pdf -> 他人のworkspace/...` の壊れ symlink を仕込むと、
    `out.exists()` は False のまま marp が **Sherpa 本体権限で symlink 先に書いてしまう**ため。
    """
    if not marp_bin or not Path(marp_bin).is_file():
        return []

    try:
        root_resolved = containment_root.resolve()
    except OSError:
        return []
    theme_dir = _resolve_theme_dir(theme_dirs)
    network_ok = None  # 遅延評価＋一度だけ判定（複数ファイルで同じ警告を連呼しない）
    warned_network = False
    warned_chrome = False
    rendered: list[Path] = []

    for src in md_paths:
        try:
            if src.is_symlink() or not src.is_file():
                continue
            if not src.resolve().is_relative_to(root_resolved):
                continue                # authoring 外へ解決される src は扱わない（多層防御）
        except OSError:
            continue
        for fmt, flags, needs_network in _FORMATS:
            out = src.with_suffix(f".{fmt}")
            if out.is_symlink() or out.exists():
                continue                # 既存出力・symlink（dangling 含む）へは書かない（RV BLOCKER）
            if needs_network:
                if not chrome_path:     # Chromium 不在＝pdf/pptx はそもそも生成不可
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
