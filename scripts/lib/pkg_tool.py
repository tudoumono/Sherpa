#!/usr/bin/env python3
"""配布パッケージの作成と、依存の指紋（部品ごとの版・ハッシュ）の測定・照合。標準ライブラリだけで動く。

設計: docs/proposals/2026-10-05-パッケージと導入・更新の一本化.md（§3 パッケージの決まり・§3.4 依存の指紋）。

サブコマンド:
  build     アプリのツリーとオフラインの資材から、圧縮ファイルと .sha256 を作る（make package-full / package-app）
  fp-app    アプリだけのパッケージの事前照合（パッケージ・前回の記録・測った今の環境の 3 者が一致するか）
  fp-full   フルのパッケージの導入後の照合（測った環境がパッケージの指紋と合うか）。合えば記録用の値を書き出す

パッケージの先頭項目は PACKAGE-INFO（種類・版・コミット・プラットフォームごとの指紋）。
次の項目が PACKAGE-MANIFEST.sha256（アプリのファイル一覧・sha256sum -c の形式）。
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import Callable

PKG_INFO = "PACKAGE-INFO"
PKG_MANIFEST = "PACKAGE-MANIFEST.sha256"
TOP = "Sherpa"
KIT_REL = "dist/offline-kit"
INFO_FORMAT = "1"

# 圧縮ファイルに入れないもの（.env・data/・.venv・tools/ の実行物・オフライン資材の置き場は別に扱う）。
EXCLUDE_PREFIXES = (
    ".git", ".env", "data", ".venv", ".venv.prev", "dist",
    "tools/node", "tools/codex", "tools/marp/node_modules",
)
# 資材のうち圧縮ファイルに入れないもの（古い方式のアプリ本体の tar.gz）。
KIT_SKIP_TOP = ("app",)

PLATFORMS = ("linux_x86_64", "darwin_arm64", "darwin_x86_64")
# 値が「複数の名前の集合」の部品。期待の集合が実際の集合に含まれていれば合う。
SET_COMPONENTS = frozenset({"chromium", "libreoffice", "fonts", "docker_engine"})
# 退避と測定で扱う tools/ の実行物（tools/ からの相対パス）。
TOOL_DIRS = ("node", "codex", "marp/node_modules")

DEFAULT_DARWIN_PYTHON = "3.12"


# ---------------------------------------------------------------------------
# 共通
# ---------------------------------------------------------------------------
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def platform_key() -> str:
    """uname に相当する値から、指紋のプラットフォーム名（scripts/lib/codex_pin.sh と同じ写像）を返す。"""
    system, machine = os.uname().sysname, os.uname().machine
    if system == "Linux":
        return {"x86_64": "linux_x86_64", "amd64": "linux_x86_64",
                "aarch64": "linux_aarch64", "arm64": "linux_aarch64"}.get(machine, "")
    if system == "Darwin":
        return {"arm64": "darwin_arm64", "x86_64": "darwin_x86_64"}.get(machine, "")
    return ""


def parse_env_pins(path: Path) -> dict[str, str]:
    """scripts/codex-version.env の KEY="value" 行を読む。"""
    pins: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r'^([A-Z][A-Z0-9_]*)="([^"]*)"\s*$', line)
        if m:
            pins[m.group(1)] = m.group(2)
    return pins


def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def constraint_pins(constraints: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in constraints.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.\-]*)==([^\s;#]+)", line.strip())
        if m:
            pins[normalize_name(m.group(1))] = m.group(2)
    return pins


def pins_digest(pins: dict[str, str]) -> str:
    return sha256_bytes("".join(f"{k}=={pins[k]}\n" for k in sorted(pins)).encode())


def requirements_digest(root: Path) -> str:
    """requirements.txt と constraints.txt の連結ハッシュ（scripts/lib/req_hash.sh と同じ式）。"""
    data = b"".join((root / name).read_bytes() for name in ("requirements.txt", "constraints.txt"))
    return sha256_bytes(data)


def codex_required_files(platform: str) -> tuple[str, ...]:
    base = ("bin/codex", "bin/codex-code-mode-host", "codex-path/rg", "codex-resources/zsh/bin/zsh")
    return base + (("codex-resources/bwrap",) if platform.startswith("linux_") else ())


def files_digest(pairs: list[tuple[str, str]]) -> str:
    return sha256_bytes("".join(f"{name} {digest}\n" for name, digest in sorted(pairs)).encode())


def deb_tokens(group_dir: Path) -> list[str]:
    """資材の .deb のうち、PACKAGES に名前のあるものを name=version の列にする。"""
    names_file = group_dir / "PACKAGES"
    if not names_file.is_file():
        return []
    wanted = set(names_file.read_text(encoding="utf-8").split())
    tokens = []
    for deb in sorted(group_dir.glob("*.deb")):
        parts = deb.name[:-4].split("_")
        if len(parts) < 3:
            continue
        name, version = parts[0], parts[1].replace("%3a", ":").replace("%3A", ":")
        if name in wanted:
            tokens.append(f"{name}={version}")
    return tokens


def docker_tar_images(tar_path: Path) -> dict[str, str]:
    """docker save の tar から {参照名: イメージ ID(sha256:…)} を読む。"""
    out: dict[str, str] = {}
    with tarfile.open(tar_path) as tf:
        member = tf.extractfile("manifest.json")
        if member is None:
            return out
        for entry in json.load(member):
            config = entry.get("Config", "")
            hex_id = os.path.basename(config)
            if hex_id.endswith(".json"):
                hex_id = hex_id[:-5]
            for tag in entry.get("RepoTags") or []:
                out[tag] = f"sha256:{hex_id}"
    return out


def tar_member_bytes(tar_path: Path, suffix: str) -> bytes | None:
    with tarfile.open(tar_path) as tf:
        for m in tf:
            if m.isfile() and m.name.endswith(suffix):
                f = tf.extractfile(m)
                return f.read() if f else None
    return None


def tar_member_exact(tar_path: Path, rel: str) -> bytes | None:
    with tarfile.open(tar_path) as tf:
        for m in tf:
            if m.isfile() and m.name.lstrip("./") == rel:
                f = tf.extractfile(m)
                return f.read() if f else None
    return None


def run_text(cmd: list[str], timeout: int = 60) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


# ---------------------------------------------------------------------------
# 指紋の導出（パッケージを作るとき）
# ---------------------------------------------------------------------------
def python_value(version_text: str) -> str:
    m = re.search(r"(\d+)\.(\d+)", version_text)
    if not m:
        raise SystemExit(f"Python の版を読めません: {version_text!r}")
    return f"{m.group(1)}.{m.group(2)}/cpython-{m.group(1)}{m.group(2)}"


def derive_common(source: Path, platform: str, pins: dict[str, str], python_text: str) -> dict[str, str]:
    return {
        "requirements": requirements_digest(source),
        "python": python_value(python_text),
        "python_packages": pins_digest(constraint_pins(source / "constraints.txt")),
        "codex_version": pins["CODEX_PIN_VERSION"],
        "codex_complete": "yes",
    }


def derive_from_kit(source: Path, kit: Path, platform: str, pins: dict[str, str]) -> dict[str, str]:
    """オフライン資材から、そのプラットフォームの部品の期待値を求める。資材に無いグループは入れない。"""
    py_file = kit / "wheels" / "COLLECTED-WITH-PYTHON-VERSION.txt"
    if not py_file.is_file():
        raise SystemExit(f"資材に Python の版の記録がありません: {py_file}")
    codex_tars = sorted((kit / "codex").glob("codex-package-*.tar.gz")) if (kit / "codex").is_dir() else []
    fp = derive_common(source, platform, pins, py_file.read_text(encoding="utf-8"))

    images: dict[str, str] = {}
    for sub in ("docker-images", "ocr"):
        d = kit / sub
        if d.is_dir():
            for tar_path in sorted(d.glob("*.tar")):
                images.update(docker_tar_images(tar_path))
    for ref, image_id in images.items():
        fp[f"docker_image/{ref}"] = image_id

    node = sorted((kit / "node").glob("node-v*.tar.xz")) if (kit / "node").is_dir() else []
    if node:
        m = re.match(r"node-(v[0-9.]+)-", node[0].name)
        if m:
            fp["node"] = m.group(1)
    marp = kit / "marp" / "tools-marp-node_modules.tar.gz"
    if marp.is_file():
        raw = tar_member_bytes(marp, "@marp-team/marp-cli/package.json")
        if raw:
            fp["marp"] = json.loads(raw)["version"]
    chromium = kit / "chromium" / "ms-playwright-chromium.tar.gz"
    if chromium.is_file():
        tops = set()
        with tarfile.open(chromium) as tf:
            for m in tf:
                top = m.name.lstrip("./").split("/", 1)[0]
                if top.startswith("chromium"):
                    tops.add(top)
        if tops:
            fp["chromium"] = ",".join(sorted(tops))
    for comp, sub in (("libreoffice", "libreoffice/debs"), ("docker_engine", "docker-engine/debs")):
        tokens = deb_tokens(kit / sub)
        if tokens:
            fp[comp] = ",".join(sorted(tokens))
    font_tokens = deb_tokens(kit / "fonts" / "noto-cjk-debs")
    if (kit / "fonts" / "hackgen").is_dir() and list((kit / "fonts" / "hackgen").glob("*.zip")):
        font_tokens.append("hackgen")
    if font_tokens:
        fp["fonts"] = ",".join(sorted(font_tokens))
    if (kit / "ocr").is_dir() and (source / "docker" / "ocr-models.lock.json").is_file():
        fp["ocr_models"] = sha256_file(source / "docker" / "ocr-models.lock.json")

    if codex_tars:
        pairs = []
        for rel in codex_required_files(platform):
            raw = tar_member_exact(codex_tars[0], rel)
            if raw is None:
                raise SystemExit(f"Codex の資材に必須ファイルがありません: {rel}")
            pairs.append((rel, sha256_bytes(raw)))
        fp["codex_files_sha256"] = files_digest(pairs)
    return fp


# macOS で brew の cask として入れる部品（システム側＝tools/ の退避の対象外）。brew は版が勝手に上がるため、
# 版は固定せず「入っているか」だけを指紋にする。
DARWIN_CASKS = {
    "libreoffice": ("libreoffice",),
    "fonts": ("font-noto-sans-cjk-jp", "font-hackgen"),
}


def derive_online(source: Path, platform: str, pins: dict[str, str], python_minor: str) -> dict[str, str]:
    """オフライン資材を使わない（オンラインで準備する）プラットフォームの期待値。"""
    fp = derive_common(source, platform, pins, python_minor)
    node_pin = parse_env_pins(source / "scripts" / "node-version.env").get("NODE_VERSION")
    lock = source / "tools" / "marp" / "package-lock.json"
    if not node_pin or not lock.is_file():
        raise SystemExit("macOS の指紋に要る scripts/node-version.env または tools/marp/package-lock.json がありません")
    fp["node"] = "v" + node_pin
    fp["marp"] = json.loads(lock.read_text(encoding="utf-8"))["packages"]["node_modules/@marp-team/marp-cli"]["version"]
    for comp, names in DARWIN_CASKS.items():
        fp[comp] = ",".join(sorted(names))
    return fp


# ---------------------------------------------------------------------------
# 指紋の測定（導入のとき・今の環境から）
# ---------------------------------------------------------------------------
def tool_path(root: Path, rel: str, prev: bool) -> Path:
    return root / "tools" / (rel + (".prev" if prev else ""))


def venv_python(root: Path, prev: bool) -> Path:
    return root / (".venv.prev" if prev else ".venv") / "bin" / "python"


def _measure_python(root: Path, prev: bool, _p: str) -> str | None:
    py = venv_python(root, prev)
    out = run_text([str(py), "-c",
                    "import sys;print('%d.%d/%s' % (sys.version_info[0], sys.version_info[1], sys.implementation.cache_tag))"])
    return out.strip() if out else None


def _measure_python_packages(root: Path, prev: bool, _p: str) -> str | None:
    pins = constraint_pins(root / "constraints.txt")
    code = ("import sys,json\nfrom importlib import metadata\nr={}\n"
            "for n in json.loads(sys.argv[1]):\n"
            "    try: r[n]=metadata.version(n)\n"
            "    except Exception: r[n]='-'\n"
            "print(json.dumps(r))")
    out = run_text([str(venv_python(root, prev)), "-c", code, json.dumps(sorted(pins))], timeout=120)
    if not out:
        return None
    return pins_digest({normalize_name(k): v for k, v in json.loads(out).items()})


def _measure_codex_version(root: Path, prev: bool, _p: str) -> str | None:
    out = run_text([str(tool_path(root, "codex", prev) / "bin" / "codex"), "--version"])
    return out.strip().split()[-1] if out and out.strip() else None


def _measure_codex_complete(root: Path, prev: bool, platform: str) -> str | None:
    base = tool_path(root, "codex", prev)
    ok = all(os.access(base / rel, os.X_OK) for rel in codex_required_files(platform))
    return "yes" if ok else "no"


def _measure_codex_files(root: Path, prev: bool, platform: str) -> str | None:
    base = tool_path(root, "codex", prev)
    pairs = []
    for rel in codex_required_files(platform):
        if not (base / rel).is_file():
            return None
        pairs.append((rel, sha256_file(base / rel)))
    return files_digest(pairs)


def _measure_node(root: Path, prev: bool, _p: str) -> str | None:
    out = run_text([str(tool_path(root, "node", prev) / "bin" / "node"), "--version"])
    return out.strip() if out else None


def _measure_marp(root: Path, prev: bool, _p: str) -> str | None:
    pkg = tool_path(root, "marp/node_modules", prev) / "@marp-team" / "marp-cli" / "package.json"
    try:
        return json.loads(pkg.read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError):
        return None


def _measure_chromium(_root: Path, _prev: bool, _p: str) -> str | None:
    base = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache" / "ms-playwright")
    if not base.is_dir():
        return None
    names = sorted(p.name for p in base.iterdir() if p.name.startswith("chromium"))
    return ",".join(names) if names else None


def _dpkg_tokens(names: list[str]) -> str | None:
    out = run_text(["dpkg-query", "-W", "-f", "${Package}=${Version}\\n", *names])
    if out is None:
        # 一部が未導入だと非 0 で終わるが、入っている分は出力される
        try:
            r = subprocess.run(["dpkg-query", "-W", "-f", "${Package}=${Version}\\n", *names],
                               capture_output=True, text=True, timeout=60)
            out = r.stdout
        except (OSError, subprocess.SubprocessError):
            return None
    return ",".join(sorted(line for line in out.splitlines() if line))


def _measure_fonts(_root: Path, _prev: bool, _p: str, expected: str = "") -> str | None:
    tokens = [t for t in expected.split(",") if t]
    names = [t.split("=")[0] for t in tokens if "=" in t]
    got = [t for t in (_dpkg_tokens(names) or "").split(",") if t] if names else []
    if "hackgen" in tokens:
        fc = run_text(["fc-list"]) or ""
        if "hackgen" in fc.lower():
            got.append("hackgen")
    return ",".join(sorted(got)) if got else None


def _brew_bin() -> str | None:
    found = shutil.which("brew")
    if found:
        return found
    for cand in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
        if os.access(cand, os.X_OK):
            return cand
    return None


def _measure_casks(names: list[str]) -> str | None:
    """brew で入っている cask のうち、期待の名前に当たるもの（版は見ない）。"""
    brew = _brew_bin()
    out = run_text([brew, "list", "--cask"]) if brew else None
    if out is None:
        return None
    return ",".join(sorted(set(out.split()) & set(names)))


def _measure_debs(expected: str) -> str | None:
    names = [t.split("=")[0] for t in expected.split(",") if "=" in t]
    return _dpkg_tokens(names) if names else None


def _measure_docker_image(ref: str) -> str | None:
    docker = shlex.split(os.environ.get("SHERPA_DOCKER", "docker"))
    out = run_text([*docker, "image", "inspect", "--format", "{{.Id}}", ref])
    return out.strip() if out and out.strip() else None


def _measure_ocr_models(root: Path, _prev: bool, _p: str) -> str | None:
    cache = Path(os.environ.get("SHERPA_OCR_MODEL_CACHE") or root / "data" / "ocr-models")
    lock = root / "docker" / "ocr-models.lock.json"
    if not (cache / "official_models").is_dir() or not lock.is_file():
        return None
    return sha256_file(lock)


SIMPLE_MEASURES: dict[str, Callable[[Path, bool, str], str | None]] = {
    "requirements": lambda root, prev, p: requirements_digest(root),
    "python": _measure_python,
    "python_packages": _measure_python_packages,
    "codex_version": _measure_codex_version,
    "codex_complete": _measure_codex_complete,
    "codex_files_sha256": _measure_codex_files,
    "node": _measure_node,
    "marp": _measure_marp,
    "chromium": _measure_chromium,
    "ocr_models": _measure_ocr_models,
}


def measure_component(root: Path, platform: str, prev: bool, name: str, expected: str) -> str | None:
    try:
        if name.startswith("docker_image/"):
            return _measure_docker_image(name[len("docker_image/"):])
        if platform.startswith("darwin_") and name in DARWIN_CASKS:
            return _measure_casks([t for t in expected.split(",") if t])
        if name == "fonts":
            return _measure_fonts(root, prev, platform, expected)
        if name in ("libreoffice", "docker_engine"):
            return _measure_debs(expected)
        return SIMPLE_MEASURES[name](root, prev, platform)
    except (OSError, ValueError, KeyError):
        return None


def mismatches(expected: dict[str, str], actual: dict[str, str | None], label: str) -> list[str]:
    bad = []
    for name, want in sorted(expected.items()):
        got = actual.get(name)
        if got is None:
            bad.append(f"{name}: {label}を測れません（欠けている）")
        elif name in SET_COMPONENTS:
            missing = sorted(set(want.split(",")) - set(got.split(",")))
            if missing:
                bad.append(f"{name}: {label}に無い部品があります: {','.join(missing)}")
        elif got != want:
            bad.append(f"{name}: パッケージ={want} / {label}={got}")
    return bad


# ---------------------------------------------------------------------------
# PACKAGE-INFO の読み書き・記録
# ---------------------------------------------------------------------------
def read_kv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key] = value
    return out


def info_fingerprint(info: dict[str, str], platform: str) -> dict[str, str] | None:
    prefix = f"fp.{platform}."
    fp = {k[len(prefix):]: v for k, v in info.items() if k.startswith(prefix)}
    return fp or None


def record_fingerprint(record: dict[str, str]) -> dict[str, str]:
    return {k[3:]: v for k, v in record.items() if k.startswith("fp.")}


def measure_expected(root: Path, platform: str, prev: bool, expected: dict[str, str]) -> dict[str, str | None]:
    return {name: measure_component(root, platform, prev, name, want) for name, want in expected.items()}


def write_fp_lines(out: Path, measured: dict[str, str | None]) -> None:
    out.write_text("".join(f"fp.{k}={v}\n" for k, v in sorted(measured.items()) if v is not None), encoding="utf-8")


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------
def _excluded(rel: str) -> bool:
    return any(rel == p or rel.startswith(p + "/") for p in EXCLUDE_PREFIXES)


def collect_entries(source: Path, kit: Path | None) -> list[tuple[str, Path]]:
    """(圧縮ファイル内の Sherpa/ からの相対パス, 実ファイル) を並べる。通常ファイルとリンクだけ。"""
    entries: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(source):
        rel_dir = Path(dirpath).relative_to(source).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        dirnames[:] = sorted(d for d in dirnames if not _excluded(f"{rel_dir}/{d}".lstrip("/")))
        for name in sorted(filenames):
            rel = f"{rel_dir}/{name}".lstrip("/")
            if not _excluded(rel):
                entries.append((rel, Path(dirpath) / name))
    if kit is not None:
        for dirpath, dirnames, filenames in os.walk(kit):
            rel_dir = Path(dirpath).relative_to(kit)
            if rel_dir == Path("."):
                dirnames[:] = sorted(d for d in dirnames if d not in KIT_SKIP_TOP)
            else:
                dirnames[:] = sorted(dirnames)
            for name in sorted(filenames):
                rel = (Path(KIT_REL) / rel_dir / name).as_posix()
                entries.append((rel, Path(dirpath) / name))
    return entries


def build_package(args: argparse.Namespace) -> int:
    source = Path(args.source).resolve()
    kit = Path(args.kit).resolve() if args.kit else None
    for needed in ("install.sh", "INSTALL.md", "requirements.txt", "constraints.txt", "scripts/codex-version.env"):
        if not (source / needed).is_file():
            print(f"✗ アプリのツリーに {needed} がありません: {source}", file=sys.stderr)
            return 1
    if kit is None or not kit.is_dir():
        print("✗ オフラインの資材（--kit）が必要です（Linux の指紋を資材から求めます）。"
              "先に scripts/make_offline_kit.sh --fetch を実行してください。", file=sys.stderr)
        return 1

    if args.kind == "full" and not list((kit / "codex").glob("codex-package-*.tar.gz")):
        print("✗ オフラインの資材に Codex CLI（codex/codex-package-*.tar.gz）がありません。Codex は必須の部品です。"
              "--skip-codex を付けずに scripts/make_offline_kit.sh --fetch を実行し直してください。", file=sys.stderr)
        return 1
    pins = parse_env_pins(source / "scripts" / "codex-version.env")
    kit_platform = os.environ.get("CODEX_PIN_KIT_PLATFORM") or pins.get("CODEX_PIN_KIT_PLATFORM") or "linux_x86_64"
    platforms = [p for p in args.platforms.split(",") if p]
    fingerprints: dict[str, dict[str, str]] = {}
    for platform in platforms:
        if platform == kit_platform:
            fingerprints[platform] = derive_from_kit(source, kit, platform, pins)
        elif platform.startswith("darwin_"):
            fingerprints[platform] = derive_online(source, platform, pins, args.darwin_python)
        else:
            print(f"ⓘ {platform} は資材の対象外のため、このパッケージには指紋を入れません。", file=sys.stderr)

    # ① 版は `+` より前だけ使う ② 中の VERSION は `<版>+<コミット>`（コミット不明なら版だけ）
    args.version = args.version.split("+", 1)[0]
    version_bytes = (f"{args.version}+{args.commit}\n" if args.commit != "unknown" else f"{args.version}\n").encode("utf-8")
    entries = collect_entries(source, kit if args.kind == "full" else None)
    if "VERSION" not in {rel for rel, _ in entries}:
        entries.append(("VERSION", source / "VERSION"))
    names = {rel for rel, _ in entries}
    names.add(PKG_INFO)

    # 先に各ファイルのハッシュを求め、一覧（PACKAGE-INFO を含み、一覧自身は含まない）を作る。
    info_lines = ["# Sherpa package info", f"format={INFO_FORMAT}", f"kind={args.kind}",
                  f"version={args.version}", f"commit={args.commit}",
                  "platforms=" + ",".join(sorted(fingerprints))]
    for platform in sorted(fingerprints):
        for comp, value in sorted(fingerprints[platform].items()):
            info_lines.append(f"fp.{platform}.{comp}={value}")
    info_bytes = ("\n".join(info_lines) + "\n").encode("utf-8")

    manifest_lines = [f"{sha256_bytes(info_bytes)}  {PKG_INFO}"]
    for rel, path in entries:
        if rel in (PKG_INFO, PKG_MANIFEST) or path.is_symlink():
            continue
        digest = sha256_bytes(version_bytes) if rel == "VERSION" else sha256_file(path)
        manifest_lines.append(f"{digest}  {rel}")
    manifest_bytes = ("\n".join(manifest_lines) + "\n").encode("utf-8")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / f"sherpa-{args.version}-{args.kind}-{args.commit}.tar.gz"
    tmp = archive.with_name(archive.name + ".part")

    def add_bytes(tf: tarfile.TarFile, name: str, data: bytes) -> None:
        ti = tarfile.TarInfo(f"{TOP}/{name}")
        ti.size, ti.mode, ti.mtime = len(data), 0o644, args.mtime
        tf.addfile(ti, io.BytesIO(data))

    with gzip.GzipFile(tmp, "wb", compresslevel=6, mtime=args.mtime) as gz, \
            tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tf:
        add_bytes(tf, PKG_INFO, info_bytes)
        add_bytes(tf, PKG_MANIFEST, manifest_bytes)
        for rel, path in entries:
            if rel in (PKG_INFO, PKG_MANIFEST):
                continue
            if rel == "VERSION":
                add_bytes(tf, rel, version_bytes)
                continue
            ti = tf.gettarinfo(str(path), f"{TOP}/{rel}")
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            if ti.isreg():
                with open(path, "rb") as f:
                    tf.addfile(ti, f)
            else:
                tf.addfile(ti)
    os.replace(tmp, archive)
    (out_dir / (archive.name + ".sha256")).write_text(f"{sha256_file(archive)}  {archive.name}\n", encoding="utf-8")
    print(f"created: {archive}（{args.kind}・{len(entries)} ファイル）")
    return 0


# ---------------------------------------------------------------------------
# fp-app / fp-full
# ---------------------------------------------------------------------------
def _load_expected(root: Path, platform: str) -> dict[str, str] | None:
    info = read_kv(root / PKG_INFO)
    return info_fingerprint(info, platform)


def cmd_fp_app(args: argparse.Namespace) -> int:
    root = Path(args.root)
    expected = _load_expected(root, args.platform)
    if expected is None:
        print(f"このパッケージはこのプラットフォーム（{args.platform}）に対応していません。", file=sys.stderr)
        return 2
    record_path = Path(args.record)
    if not record_path.is_file():
        print("前回の成功したインストールの記録がありません（初回導入にはフルのパッケージが必要です）。", file=sys.stderr)
        return 1
    recorded = {k: v for k, v in record_fingerprint(read_kv(record_path)).items()}
    measured = measure_expected(root, args.platform, args.prev, expected)
    bad = mismatches(expected, {k: recorded.get(k) for k in expected}, "前回の記録")
    bad += mismatches(expected, measured, "今の環境")
    if bad:
        print("\n".join(bad), file=sys.stderr)
        return 1
    if args.out:
        write_fp_lines(Path(args.out), measured)
    return 0


def cmd_fp_full(args: argparse.Namespace) -> int:
    root = Path(args.root)
    expected = _load_expected(root, args.platform)
    if expected is None:
        print(f"このパッケージはこのプラットフォーム（{args.platform}）に対応していません。", file=sys.stderr)
        return 2
    measured = measure_expected(root, args.platform, False, expected)
    bad = mismatches(expected, measured, "導入後の環境")
    if bad:
        print("\n".join(bad), file=sys.stderr)
        return 1
    # 資材に無く期待していない部品でも、今あるものは測って記録する。
    for name in ("node", "marp"):
        if name not in expected:
            value = measure_component(root, args.platform, False, name, "")
            if value is not None:
                measured[name] = value
    write_fp_lines(Path(args.out), measured)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build")
    b.add_argument("--kind", choices=("full", "app"), required=True)
    b.add_argument("--source", required=True)
    b.add_argument("--kit")
    b.add_argument("--out-dir", required=True)
    b.add_argument("--version", required=True)
    b.add_argument("--commit", required=True)
    b.add_argument("--platforms", default=",".join(PLATFORMS))
    b.add_argument("--darwin-python", default=DEFAULT_DARWIN_PYTHON)
    b.add_argument("--mtime", type=int, default=int(time.time()))
    b.set_defaults(func=build_package)

    for name, func in (("fp-app", cmd_fp_app), ("fp-full", cmd_fp_full)):
        p = sub.add_parser(name)
        p.add_argument("--root", required=True)
        p.add_argument("--platform", required=True)
        p.add_argument("--out")
        p.add_argument("--prev", action="store_true")
        if name == "fp-app":
            p.add_argument("--record", required=True)
        else:
            p.set_defaults(out=None)
        p.set_defaults(func=func)

    args = ap.parse_args(argv)
    if args.cmd == "fp-full" and not args.out:
        ap.error("fp-full には --out が必要です")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
