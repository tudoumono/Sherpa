"""ドキュメント・ドリフト検査（refactoring-plan フェーズ8d の前倒し分）。

正典: docs/proposals/2026-07-02-リファクタリング計画.md 「8d. ドキュメント・ドリフトの CI 検査」。

- test_retired_vocabulary_gate: 退役した「版」モデル固有語彙が、archive/proposals
  以外の現役 docs（＋ CLAUDE.md）に「撤去の説明」以外の形で残っていないか検査する。
- test_relative_markdown_links_resolve: docs 配下（archive 含む）＋ CLAUDE.md／
  README.md／mockups/README.md の相対 Markdown リンクが実在するファイル/ディレクトリを
  指しているか検査する。
- test_env_vars_documented: sherpa/**/*.py が参照する `SHERPA_*` 環境変数が
  `.env.example` ∪ `docs/manual/90-リファレンス.md` に文書化されているか検査する
  （検査は「コードにあるのに docs に無い」の一方向のみ）。
- test_api_version_param_retired: archive/proposals 以外の docs（＋ CLAUDE.md）に、旧 `version` が
  API のクエリ/ボディパラメータとして**今なお受理される**という記載が残っていないか検査する
  （フェーズ2第2段＝`version` 受理終了・2026-07-13 完了後のドリフト検査）。
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"

# 旧「版」モデル固有の表現のみを対象にする。`canonical_id` 単体は現役語彙
# （パス同一性 ID・world_graph.py／ONTOLOGY §3）なので含めない。
_RETIRED_VOCAB_RE = re.compile(r"版分離|版ライフサイクル|版公開|@版")

# 禁止語を含む行でも、同じ行に「撤去の説明」だと分かる語があれば許容する。
_EXPLAINED_RE = re.compile(r"撤去|退役|廃止|置換|旧|正典|historical")


def _retired_vocab_targets() -> list[pathlib.Path]:
    """docs/**/*.md のうち archive・proposals を除いたもの＋ CLAUDE.md。"""
    targets: list[pathlib.Path] = []
    for path in sorted(DOCS.rglob("*.md")):
        rel_parts = path.relative_to(DOCS).parts
        if rel_parts and rel_parts[0] in ("archive", "proposals"):
            continue
        targets.append(path)
    # 公開 export（scripts/export_public.sh）には CLAUDE.md が含まれない＝存在するときだけ検査
    # （内部リポでは常に存在＝検査は従来どおり効く）。
    if (ROOT / "CLAUDE.md").exists():
        targets.append(ROOT / "CLAUDE.md")
    return targets


def test_retired_vocabulary_gate():
    """archive/proposals 以外の docs に旧「版」モデル語彙が説明抜きで残っていないこと。"""
    violations: list[str] = []
    for path in _retired_vocab_targets():
        rel = path.relative_to(ROOT)
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _RETIRED_VOCAB_RE.search(line) and not _EXPLAINED_RE.search(line):
                violations.append(f"{rel}:{lineno}: {line.strip()}")

    assert not violations, "退役語彙ゲート違反（撤去の説明なしに旧語彙が残存）:\n" + "\n".join(violations)


# フェーズ2第2段（version 受理終了・2026-07-13）後のドリフト検査。
# 「version が API のクエリ/ボディパラメータとして今なお受理される」という記載だけを狙い、
# DB 列 `version`（例: `version=NULL`）・semver・`SHERPA_VERSION` env・設計私案の property 名
# （例: `version=commit hash`）等の無関係な言及を誤検知しないよう、対象を2形に絞る:
#   (a) クエリ文字列形の直書き（`?version=`・`&version=`）
#   (b) 「version パラメータ」等、API パラメータとしての言及
#   (c) 「version を（互換）受理／deprecation 警告」等の stale な現在形記述
# 許容語は本ゲート専用（RV MED: `_EXPLAINED_RE` は `旧` を許すため「旧 version も当面受理」の
# ような stale 記述まで通してしまう。ここでは「終了・撤去済み」を明示する語だけを許す）。
_API_VERSION_QUERY_RE = re.compile(r"[?&]version=")
_API_VERSION_PARAM_MENTION_RE = re.compile(r"version\s*(パラメータ|param\b)")
_API_VERSION_STALE_RE = re.compile(r"version[^。\n]{0,30}(互換受理|受理|deprecation|警告)")
_API_VERSION_OK_RE = re.compile(r"受理終了|受理は終了|受理を終了|受理削除|終了済み|撤去済み|削除済み|未宣言|無視され")


def test_api_version_param_retired():
    """archive/proposals 以外の docs に、旧 `version` が API パラメータとして受理される旨の記載が
    「終了・撤去済み」の明示なしに残っていないこと（フェーズ2第2段の受け入れ基準）。"""
    violations: list[str] = []
    for path in _retired_vocab_targets():
        rel = path.relative_to(ROOT)
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            hit = (_API_VERSION_QUERY_RE.search(line)
                   or _API_VERSION_PARAM_MENTION_RE.search(line)
                   or _API_VERSION_STALE_RE.search(line))
            if hit and not _API_VERSION_OK_RE.search(line):
                violations.append(f"{rel}:{lineno}: {line.strip()}")

    assert not violations, "旧 version パラメータ受理ゲート違反（撤去の説明なしに記載が残存）:\n" + "\n".join(violations)


# Markdown リンク `](target)` の target 抽出。画像 `![alt](target)` も同じ形なので
# 併せて拾った上で、画像拡張子は下流で除外する。
_MD_LINK_RE = re.compile(r"\]\(([^)]+)\)")
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".ico")


def _relative_link_targets() -> list[pathlib.Path]:
    """docs/**/*.md（archive 含む全部）＋ CLAUDE.md／README.md／mockups/README.md。"""
    targets = sorted(DOCS.rglob("*.md"))
    # 公開 export（scripts/export_public.sh）には CLAUDE.md が含まれない＝存在するときだけ検査
    # （内部リポでは常に存在＝検査は従来どおり効く）。
    if (ROOT / "CLAUDE.md").exists():
        targets.append(ROOT / "CLAUDE.md")
    targets.append(ROOT / "README.md")
    targets.append(ROOT / "mockups" / "README.md")
    return targets


def test_relative_markdown_links_resolve():
    """相対 Markdown リンク（.md・ディレクトリ）がリンク元から解決して実在すること。

    画像リンク（.png 等）は既知の未整備（スクショ未撮影・13-詳細設計引継ぎ.md 前提）があるため
    対象外にする（docs/proposals/2026-07-02-リファクタリング計画.md 8b 参照）。
    """
    broken: list[str] = []
    for path in _relative_link_targets():
        rel = path.relative_to(ROOT)
        text = path.read_text(encoding="utf-8")
        for match in _MD_LINK_RE.finditer(text):
            target = match.group(1).strip()
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            if target.startswith("#"):
                continue
            file_part = target.split("#", 1)[0]
            if not file_part:
                continue
            is_dir = file_part.endswith("/")
            is_md = file_part.lower().endswith(".md")
            if not (is_dir or is_md):
                continue
            if file_part.lower().endswith(_IMAGE_EXTS):
                continue
            resolved = (path.parent / file_part).resolve()
            if not resolved.exists():
                broken.append(f"{rel}: {target}")

    assert not broken, "相対リンク切れ:\n" + "\n".join(broken)


# コードが参照する env-var 名の抽出（.env.example／manual/90 でも同じパターンで検査する）。
_ENV_VAR_RE = re.compile(r"SHERPA_[A-Z_]+")

SHERPA_SRC = ROOT / "sherpa"


def test_env_vars_documented():
    """sherpa/**/*.py が参照する SHERPA_* が .env.example / manual/90 に文書化されていること。

    検査は「コードにあるのに docs に無い」の一方向のみ。逆（docs にあってコードに無い
    ＝`SHERPA_ENV_FILE`／`SHERPA_HOST`／`SHERPA_PORT`／`SHERPA_UID`／
    `SHERPA_UVICORN_WORKERS` 等・scripts/systemd 側が読む運用変数）は対象外
    （docs/proposals/2026-07-02-リファクタリング計画.md 8d 参照）。
    """
    code_vars: set[str] = set()
    for path in SHERPA_SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        code_vars |= set(_ENV_VAR_RE.findall(text))

    documented: set[str] = set()
    for path in (ROOT / ".env.example", DOCS / "manual" / "90-リファレンス.md"):
        text = path.read_text(encoding="utf-8")
        documented |= set(_ENV_VAR_RE.findall(text))

    missing = sorted(code_vars - documented)
    assert not missing, "未文書化の環境変数（.env.example / manual/90 に追記が必要）:\n" + "\n".join(missing)
