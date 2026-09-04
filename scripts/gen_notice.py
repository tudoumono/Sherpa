#!/usr/bin/env python3
"""帰属表示（NOTICE）と部品表（SBOM）を生成する。

なぜ要るか（`docs/notes/2026-08-17-ライセンス棚卸し.md`）:
  - Apache-2.0 等は**帰属表示が義務**。第三者へ納品するなら NOTICE を同梱しなければならない。
  - LGPL/GPL の同梱物は**ライセンス全文と入手方法の明示**が要る。
  - SBOM は企業調達で要求されやすく、EU CRA でも求められる。

方針:
  - **追加依存を入れない**（閉域で再生成できること・オフラインキットの原則を崩さないこと）。
    `importlib.metadata` だけで Python 依存を数え、wheel が同梱しているライセンス全文を集める。
  - Python 以外の同梱物（Docker イメージ・OS パッケージ・フォント等）は自動検出できないため、
    下の `BUNDLED` に**人が管理する表**として持つ。ここが唯一の手作業＝資材を足したらここも足す。

使い方:
    scripts/gen_notice.py                    # dist/notice/ へ出力
    scripts/gen_notice.py --out <dir>
    scripts/gen_notice.py --check            # 生成物が最新か検査（差分があれば非0終了）

出力:
    NOTICE.md                  帰属表示（人が読む）
    THIRD-PARTY-LICENSES.txt   同梱物のライセンス全文（機械的に連結）
    sbom.cdx.json              CycloneDX 1.5 形式の部品表
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Python 以外の同梱物。**資材を足したらここも足す**（`docs/18-オフライン構築.md` §3 と対で維持）。
# `copyleft` は「全文と入手方法の明示が要る」もの＝NOTICE で source を必ず出す。
BUNDLED: list[dict] = [
    {"name": "PostgreSQL", "version": "16", "license": "PostgreSQL License",
     "source": "https://www.postgresql.org/", "kind": "container"},
    {"name": "Neo4j Community", "version": "5", "license": "GPL-3.0-only", "copyleft": True,
     "source": "https://github.com/neo4j/neo4j", "kind": "container",
     "note": "別プロセスとして Bolt 経由で利用（リンクしない）。未改変イメージを同梱。"},
    {"name": "Elasticsearch", "version": "8.19.20", "license": "Elastic-2.0", "kind": "container",
     "source": "https://github.com/elastic/elasticsearch",
     "note": "非OSS。オンプレ納品は可・ホスト/マネージド提供は不可。表記の削除やライセンスキー回避も不可。"},
    {"name": "analysis-kuromoji", "version": "8.19.20", "license": "Apache-2.0", "kind": "container",
     "source": "https://github.com/elastic/elasticsearch"},
    {"name": "Docker Engine", "version": "(kit)", "license": "Apache-2.0", "kind": "package",
     "source": "https://github.com/moby/moby"},
    {"name": "Ubuntu base packages", "version": "24.04", "license": "各パッケージのライセンスに従う",
     "source": "https://ubuntu.com/legal", "kind": "package",
     "note": "未改変 .deb を同梱。個別ライセンスは各 .deb の /usr/share/doc/*/copyright を参照。"},
    {"name": "Node.js", "version": "22.22.3", "license": "MIT", "kind": "runtime",
     "source": "https://github.com/nodejs/node"},
    {"name": "marp-cli", "version": "(kit)", "license": "MIT", "kind": "runtime",
     "source": "https://github.com/marp-team/marp-cli"},
    {"name": "Playwright Chromium", "version": "(kit)", "license": "BSD-3-Clause 他", "kind": "runtime",
     "source": "https://chromium.googlesource.com/chromium/src/"},
    {"name": "LibreOffice", "version": "(kit)", "license": "MPL-2.0", "kind": "package",
     "source": "https://www.libreoffice.org/download/source-code/"},
    {"name": "Noto Sans CJK JP", "version": "(kit)", "license": "OFL-1.1", "kind": "font",
     "source": "https://github.com/notofonts/noto-cjk"},
    {"name": "HackGen", "version": "(kit)", "license": "OFL-1.1", "kind": "font",
     "source": "https://github.com/yuru7/HackGen"},
    {"name": "PP-OCRv6_medium_det / _rec (model weights)", "version": "PP-OCRv6", "license": "Apache-2.0",
     "source": "https://huggingface.co/PaddlePaddle/PP-OCRv6_medium_det", "kind": "model",
     "note": "上流宣言を一次情報で確認済み（2026-08-17・HF API/モデルカード。gated=false・利用制限記述なし）。"},
    {"name": "Debian base packages (python:3.12-slim-bookworm)", "version": "bookworm",
     "license": "各パッケージのライセンスに従う（GPL 系を含む）", "copyleft": True, "kind": "container",
     "source": "https://sources.debian.org/",
     "note": "OCR ワーカーイメージの土台。未改変の Debian パッケージを含む。個別ライセンスはイメージ内 /usr/share/doc/*/copyright（148件）を参照。"},
    {"name": "Codex CLI (@openai/codex, linux-x64)", "version": "(kit)", "license": "Apache-2.0", "kind": "runtime",
     "source": "https://github.com/openai/codex",
     "note": "閉域キットに同梱（--skip-codex で除外可）。同梱の ripgrep 等の来歴は npm パッケージの LICENSE/NOTICE を参照。"},
    {"name": "marked", "version": "13.0.3", "license": "MIT", "kind": "web",
     "source": "https://github.com/markedjs/marked"},
    {"name": "cytoscape.js", "version": "(vendored)", "license": "MIT", "kind": "web",
     "source": "https://github.com/cytoscape/cytoscape.js"},
    {"name": "Swagger UI", "version": "5.32.14", "license": "Apache-2.0", "kind": "web",
     "source": "https://github.com/swagger-api/swagger-ui",
     "note": "web/vendor/swagger-ui-bundle.js が同梱する第三者コンポーネント個別ライセンスは"
             " web/vendor/swagger-ui-bundle.js.LICENSE.txt を参照。"},
]

_LICENSE_FILE_RE = re.compile(r"(LICEN[CS]E|COPYING|NOTICE)", re.I)


def _venvs() -> list[tuple[str, pathlib.Path]]:
    """棚卸し対象の venv。OCR は依存がコアと両立しないため別環境になっている。"""
    out = []
    for label, rel in (("core", ".venv"), ("ocr", ".venv-ocr")):
        py = ROOT / rel / "bin" / "python"
        if py.exists():
            out.append((label, py))
    return out


def _collect_python(py: pathlib.Path) -> list[dict]:
    """指定 venv の依存を列挙する（その venv の python で実行しないと中身が見えない）。"""
    script = r"""
import importlib.metadata as m, json, pathlib, re
rx = re.compile(r"(LICEN[CS]E|COPYING|NOTICE)", re.I)
out = []
for d in m.distributions():
    md = d.metadata
    lic = (md.get("License-Expression") or md.get("License") or "").strip()
    if not lic or len(lic) > 60:
        cls = [c.split("::")[-1].strip() for c in md.get_all("Classifier") or [] if c.startswith("License")]
        lic = "; ".join(cls) or (lic[:57] + "..." if lic else "UNKNOWN")
    url = md.get("Home-page") or ""
    for entry in md.get_all("Project-URL") or []:
        if not url and "," in entry:
            url = entry.split(",", 1)[1].strip()
    texts = []
    for f in d.files or []:
        p = pathlib.Path(str(f))
        if "dist-info" in str(f) and rx.search(p.name):
            # `d.read_text()` は dist-info 相対の名前しか受け取らず、フルパスを渡すと
            # 黙って None を返す（実測で全件が空になった）。実体パスから読む。
            try:
                texts.append({"name": p.name, "text": d.locate_file(f).read_text(encoding="utf-8", errors="replace")})
            except Exception:
                pass
    out.append({"name": md.get("Name") or "?", "version": d.version or "?",
                "license": lic, "url": url, "texts": texts})
print(json.dumps(out, ensure_ascii=False))
"""
    raw = subprocess.run([str(py), "-c", script], capture_output=True, text=True, check=True).stdout
    return sorted(json.loads(raw), key=lambda r: r["name"].lower())


def _is_copyleft(lic: str) -> bool:
    return bool(re.search(r"\b(L?GPL|AGPL|MPL|EPL|CDDL)", lic, re.I))


def _ollama_models(kit: pathlib.Path) -> list[str]:
    """キットに同梱された Ollama モデルを検出する。

    `--with-ollama` は**任意のモデル**を同梱できるが、モデルのライセンスは提供元ごとに大きく違う
    （Apache-2.0 のものもあれば、利用者数の上限や用途制限を課す独自ライセンスもある）。
    自動判定はできないので、**同梱されていることを検出して NOTICE に手当てを促す**。
    """
    root = kit / "ollama" / "dot-ollama" / "models" / "manifests"
    if not root.is_dir():
        return []
    return sorted({"/".join(p.relative_to(root).parts[1:]) for p in root.rglob("*") if p.is_file()})


def _render_notice(groups: dict[str, list[dict]]) -> str:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip() if (ROOT / "VERSION").exists() else "0.0.0"
    lines = [
        f"# Sherpa {version} — 第三者ソフトウェアの帰属表示（NOTICE）",
        "",
        "本製品には以下の第三者ソフトウェアが含まれます。各ソフトウェアの著作権は各権利者に帰属し、",
        "それぞれのライセンス条件に従って利用・再配布されます。ライセンス全文は同梱の",
        "`THIRD-PARTY-LICENSES.txt`、部品の一覧は `sbom.cdx.json`（CycloneDX）を参照してください。",
        "",
        "本ファイルは `scripts/gen_notice.py` が生成します（手で編集しない）。",
        "",
        "## コピーレフト系ソフトウェアのソース入手について",
        "",
        "下表で GPL / LGPL / MPL 等が付されたものは、ライセンスの定めによりソースコードを入手できます。",
        "各行の「入手先」から取得してください。入手先が参照できない場合は、本製品の提供元へ請求すれば",
        "当該ライセンスが要求する期間、ソースコードを提供します。",
        "",
        "## 同梱コンポーネント（Python 以外）",
        "",
        "| 名称 | 版 | ライセンス | 入手先 | 備考 |",
        "|------|----|-----------|--------|------|",
    ]
    for c in BUNDLED:
        lines.append(f"| {c['name']} | {c['version']} | {c['license']} | {c['source']} | {c.get('note', '')} |")
    models = _ollama_models(ROOT / "dist" / "offline-kit")
    if models:
        lines += ["", "## ローカルLLM（Ollama）モデル", "",
                  "**以下のモデルはライセンスが提供元ごとに異なる。配布前に各モデルの条件を確認し、",
                  "この節へライセンス名と入手先を追記すること**（自動判定できないため手当てが要る）。", ""]
        for m in models:
            lines.append(f"- {m} — ライセンス: **要確認**")
    for label, rows in groups.items():
        title = "Python 依存（アプリ本体）" if label == "core" else "Python 依存（OCR ワーカー・隔離環境）"
        lines += ["", f"## {title}", "", f"{len(rows)} 件。",
                  "", "| パッケージ | 版 | ライセンス | 入手先 |", "|-----------|----|-----------|--------|"]
        for r in rows:
            lines.append(f"| {r['name']} | {r['version']} | {r['license']} | {r['url']} |")
    lines.append("")
    return "\n".join(lines)


def _render_license_texts(groups: dict[str, list[dict]]) -> str:
    parts = ["Sherpa — 第三者ソフトウェアのライセンス全文",
             "=" * 60,
             "本ファイルは scripts/gen_notice.py が生成します（手で編集しない）。",
             "Python 依存については、各配布物（wheel）が同梱するライセンス全文をそのまま連結しています。",
             "同梱していないものは NOTICE.md の入手先を参照してください。", ""]
    seen: set[tuple[str, str]] = set()
    for label, rows in groups.items():
        for r in rows:
            for t in r["texts"]:
                key = (r["name"], t["name"])
                if key in seen or not t["text"].strip():
                    continue
                seen.add(key)
                parts += ["", "-" * 60,
                          f"{r['name']} {r['version']} ({r['license']}) — {t['name']}",
                          "-" * 60, "", t["text"].rstrip(), ""]
    return "\n".join(parts) + "\n"


def _render_sbom(groups: dict[str, list[dict]]) -> str:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip() if (ROOT / "VERSION").exists() else "0.0.0"
    components = []
    for label, rows in groups.items():
        for r in rows:
            components.append({
                "type": "library", "name": r["name"], "version": r["version"],
                "purl": f"pkg:pypi/{r['name'].lower()}@{r['version']}",
                "licenses": [{"license": {"name": r["license"]}}],
                "properties": [{"name": "sherpa:environment", "value": label}],
            })
    kind_map = {"container": "container", "runtime": "application", "package": "application",
                "font": "file", "model": "machine-learning-model", "web": "library"}
    for c in BUNDLED:
        components.append({
            "type": kind_map.get(c["kind"], "library"), "name": c["name"], "version": c["version"],
            "licenses": [{"license": {"name": c["license"]}}],
            "externalReferences": [{"type": "distribution", "url": c["source"]}],
        })
    # `serialNumber`・タイムスタンプは入れない（毎回変わると --check の差分判定が使えなくなる）。
    doc = {"bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
           "metadata": {"component": {"type": "application", "name": "Sherpa", "version": version}},
           "components": components}
    return json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="NOTICE と SBOM を生成する")
    ap.add_argument("--out", type=pathlib.Path, default=ROOT / "dist" / "notice")
    ap.add_argument("--check", action="store_true", help="生成物が最新かを検査（差分があれば非0）")
    args = ap.parse_args(argv)

    venvs = _venvs()
    if not venvs:
        print("✗ .venv が見つかりません（依存を導入してから実行してください）", file=sys.stderr)
        return 1
    groups = {label: _collect_python(py) for label, py in venvs}
    if "ocr" not in groups:
        print("ⓘ  .venv-ocr が無いため OCR 依存は含めません（NOTICE は不完全になります）", file=sys.stderr)

    outputs = {
        "NOTICE.md": _render_notice(groups),
        "THIRD-PARTY-LICENSES.txt": _render_license_texts(groups),
        "sbom.cdx.json": _render_sbom(groups),
    }
    if args.check:
        stale = [n for n, body in outputs.items()
                 if not (args.out / n).exists() or (args.out / n).read_text(encoding="utf-8") != body]
        if stale:
            print("✗ 生成物が最新ではありません: " + ", ".join(stale), file=sys.stderr)
            print("  scripts/gen_notice.py を実行して更新してください。", file=sys.stderr)
            return 1
        print("OK: NOTICE / ライセンス全文 / SBOM は最新です")
        return 0

    args.out.mkdir(parents=True, exist_ok=True)
    for name, body in outputs.items():
        (args.out / name).write_text(body, encoding="utf-8")
    total = sum(len(v) for v in groups.values())
    copyleft = sorted({r["license"] for rows in groups.values() for r in rows if _is_copyleft(r["license"])})
    print(f"OK: {args.out}")
    print(f"  Python 依存 {total} 件（{' / '.join(f'{k}={len(v)}' for k, v in groups.items())}）"
          f" ＋ 同梱コンポーネント {len(BUNDLED)} 件")
    if copyleft:
        print("  コピーレフト系（全文と入手先を NOTICE に明示済み）: " + ", ".join(copyleft))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
