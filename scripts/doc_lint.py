#!/usr/bin/env python3
"""docs/templates・docs/design・docs/proposals 向けの文書規約チェッカー（`make doc-lint`）。

依存は Python 標準ライブラリ＋PyYAML のみ。docs/templates/README.md §5 が定める機械検査を行う。

1. front matter の必須項目（`id`/`title`/`status`/`applies_to`/`related`/`updated`/`owner`）と、
   文書種別（提案書/設計書/用語集）ごとの `status` 語彙。
2. front matter `id` の形式（種別ごとの正規表現）と、スキャン対象内での重複。
3. 本文の相対リンク・front matter `related` の実在確認。加えて、リンクに `#見出し` が付く場合は
   リンク先（`path#anchor`）または自文書（`#anchor`）の見出しから GitHub 互換の規則でアンカー索引を
   作り、一致する見出しが実在するかを検査する（アンカー生成規則: 見出しテキストを小文字化し、
   `\\w`・空白・ハイフン以外の記号を除去し、空白の連続をハイフン1つに畳む。前方から見て同じアンカーが
   複数回出た文書は2回目以降に `-1`・`-2`… を付ける。検査対象は Markdown 見出しのみ——リンク先が
   `.md` 以外（ソースコードの行番号アンカー等）はアンカー検査を行わずファイル実在確認のみ行う）。
4. 契約ブロック（`#### 契約: <名前> \\`<ID>\\`（<状態>）`）の見出し形式と、本文8項目
   （方式・前提・入力・出力・エラー・不変条件・検証・正本）の並び。加えて、見出しから集めた契約 ID の
   形式（docs/templates/README.md §4: `^C-[A-Z0-9]+(-[A-Z0-9]+)*-\\d{2}$`）と、スキャン対象内での重複
   （テンプレートの雛形が使うプレースホルダ `` `<ID>` `` はこの検査の対象外）。

**意図的にやらないこと**（docs/templates/README.md §5 の「まだ機械検査していない」2件）:
契約の `verification` 欄の中身の妥当性、設計書とコードの一致——どちらも目視の補助チェックリストのまま。

`id` が空文字列（`id: ""`）の文書は「複写して使う雛形」（docs/templates/{proposal,design-doc,glossary}.md
等）とみなし、front matter の必須項目のうち非空値を求める検査・`status` 語彙・ID 形式/重複はスキップする
（キーが存在すること自体は雛形でも検査する）。front matter を持たない文書（README.md・課題管理簿.md・
front matter 導入前の古い提案書など、素の Markdown）は front matter 関連の検査をすべてスキップし、
リンク・アンカー・契約ブロックの検査だけ行う（`docs/proposals` の古い提案書がこれに当たる——新規の
提案書は front matter を付けるが、既存分を書式のためだけに書き換えることはしない）。

使い方: `python3 scripts/doc_lint.py [対象ディレクトリ...]`
（省略時は `docs/templates` `docs/design` `docs/proposals`）。違反が1件でもあれば非0で終了する。
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TARGETS = ["docs/templates", "docs/design", "docs/proposals"]

REQUIRED_FM_KEYS = ("id", "title", "status", "applies_to", "related", "updated", "owner")

# 種別ごとの ID 正規表現・許容 status 語彙（docs/templates/README.md §3・§4 の表と同じ）。
DOC_TYPES = {
    "proposal": {
        "id_re": re.compile(r"^PROP-\d{4}-\d{2}-\d{2}-.+$"),
        "status_vocab": {"draft", "decided", "implemented", "superseded"},
    },
    "design": {
        "id_re": re.compile(r"^DES-[A-Za-z0-9-]+$"),
        "status_vocab": {"draft", "review", "decided", "implemented", "archived"},
    },
    "glossary": {
        "id_re": re.compile(r"^GLOSSARY-[A-Za-z0-9-]+$"),
        "status_vocab": {"draft", "review", "decided"},
    },
}

CONTRACT_HEADING_RE = re.compile(r"^#### 契約: (?P<name>.+?) `(?P<id>[^`]+)`（(?P<status>.+)）\s*$")
# 本文8項目（トップレベルの `- **label**` 行のみ。字下げされた続き行・入れ子箇条書きは対象外）。
CONTRACT_ITEM_RE = re.compile(r"^- \*\*(?P<label>[^*]+)\*\*")
REQUIRED_CONTRACT_ITEMS = ["方式", "前提", "入力", "出力", "エラー", "不変条件", "検証", "正本"]

# docs/templates/README.md §4「設計書内の契約ブロック」の正規表現。
CONTRACT_ID_RE = re.compile(r"^C-[A-Z0-9]+(-[A-Z0-9]+)*-\d{2}$")
# テンプレートの雛形見出し（`#### 契約: <契約名> \`<ID>\`（<状態>）`）が使うプレースホルダ。
# front matter の `id: ""` と同じ扱いで、形式・重複検査の対象から外す。
PLACEHOLDER_CONTRACT_ID = "<ID>"

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
FENCE_RE = re.compile(r"^\s*```")

# ATX 見出し（`#`〜`######`）。アンカー索引の構築にのみ使う（契約ブロックの検査は CONTRACT_HEADING_RE）。
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
# 見出し末尾の閉じ `#` 記法（`## 見出し ##`）を取り除く。
HEADING_TRAILING_HASH_RE = re.compile(r"\s+#+\s*$")
# アンカー生成: `\w`（Unicode の文字・数字・アンダースコア＝日本語もここに含まれる）・空白・ハイフン
# 以外の記号を除去する（GitHub の見出しアンカー規則と同じ「小文字化・記号除去・空白をハイフン」）。
_SLUG_STRIP_RE = re.compile(r"[^\w\s-]", re.UNICODE)
_SLUG_WS_RE = re.compile(r"\s+")
# 見出し中の Markdown 装飾（リンク・コードスパン・太字）を、アンカー生成前に地の文へ落とす。
_HEADING_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_HEADING_BOLD_RE = re.compile(r"\*\*|__")


@dataclass
class Violation:
    path: Path
    line: int
    check: str
    message: str

    def format(self) -> str:
        return f"{_display_path(self.path)}:{self.line} — {self.check} — {self.message}"


def _display_path(path: Path) -> Path | str:
    try:
        return path.relative_to(REPO_ROOT) if path.is_absolute() else path
    except ValueError:
        return path


def strip_fences_and_comments(text: str) -> str:
    """フェンス付きコードブロックと HTML コメントの中身を、行数を保ったまま空行に潰す。

    契約ブロックの見出し・箇条書きスキャンが、書式の説明用コード片（templates/README.md の見本）や
    複写後に消す記入例（design-doc.md の `<!-- -->` 内の見本）を実データと誤認しないための前処理。
    """
    out = []
    in_fence = False
    in_comment = False
    for line in text.split("\n"):
        if in_comment:
            out.append("")
            if "-->" in line:
                in_comment = False
            continue
        if in_fence:
            out.append("")
            if FENCE_RE.match(line):
                in_fence = False
            continue
        if FENCE_RE.match(line):
            in_fence = True
            out.append("")
            continue
        stripped = line.strip()
        if stripped.startswith("<!--"):
            if "-->" not in line:
                in_comment = True
            out.append("")
            continue
        out.append(line)
    return "\n".join(out)


def slugify_heading(text: str) -> str:
    """見出しテキストから GitHub 互換のアンカー文字列を1つ作る（重複サフィックスは呼び出し側が付ける）。

    手順: リンク記法・コードスパン・太字記法を地の文へ落とす → 小文字化 → `\\w`（Unicode の文字・
    数字・アンダースコア）・空白・ハイフン以外の記号を除去 → 空白の連続をハイフン1つに畳む。
    日本語・その他の Unicode 文字は Python の `\\w` が単語構成文字として扱うため、そのまま残る
    （ローマ字化はしない）。
    """
    text = _HEADING_LINK_RE.sub(r"\1", text)
    text = text.replace("`", "")
    text = _HEADING_BOLD_RE.sub("", text)
    text = text.strip().lower()
    text = _SLUG_STRIP_RE.sub("", text)
    return _SLUG_WS_RE.sub("-", text.strip())


def extract_heading_anchors(stripped_lines: list[str]) -> set[str]:
    """文書全体の見出しから、実在するアンカー文字列の集合を作る（`strip_fences_and_comments` 済みの
    行を渡すこと——フェンス・コメント内の見本見出しをアンカーと誤認しないため）。
    """
    seen: dict[str, int] = {}
    anchors: set[str] = set()
    for line in stripped_lines:
        m = HEADING_RE.match(line)
        if not m:
            continue
        raw = HEADING_TRAILING_HASH_RE.sub("", m.group(2))
        slug = slugify_heading(raw)
        if not slug:
            continue
        n = seen.get(slug, 0)
        seen[slug] = n + 1
        anchors.add(slug if n == 0 else f"{slug}-{n}")
    return anchors


def _anchors_for_target(target: Path, anchor_cache: dict[Path, set[str] | None]) -> set[str] | None:
    """`target` の見出しアンカー集合（Markdown 以外は None＝アンカー検査対象外）。実行1回分（`main`
    1回の呼び出し）の中でのみキャッシュする——同じファイルを複数のリンクが指すたびに読み直さない。
    """
    if target.suffix.lower() != ".md":
        return None
    if target in anchor_cache:
        return anchor_cache[target]
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        anchor_cache[target] = set()
        return anchor_cache[target]
    stripped = strip_fences_and_comments(text).split("\n")
    anchors = extract_heading_anchors(stripped)
    anchor_cache[target] = anchors
    return anchors


def extract_front_matter(lines: list[str]):
    """先頭の front matter を探す。コメント・空行は読み飛ばし、それ以外の本文に先に当たったら None。"""
    i = 0
    n = len(lines)
    in_comment = False
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if in_comment:
            if "-->" in line:
                in_comment = False
            i += 1
            continue
        if stripped == "":
            i += 1
            continue
        if stripped.startswith("<!--"):
            if "-->" not in line:
                in_comment = True
            i += 1
            continue
        if stripped == "---":
            start = i
            j = i + 1
            while j < n and lines[j].strip() != "---":
                j += 1
            if j >= n:
                return None
            yaml_text = "\n".join(lines[start + 1:j])
            return {"start_line": start, "end_line": j, "yaml_text": yaml_text}
        return None
    return None


def _is_stub_id(value) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def check_front_matter(path: Path, lines: list[str], id_locations: dict, violations: list[Violation]):
    fm = extract_front_matter(lines)
    if fm is None:
        return  # front matter を持たない文書（README 等）は対象外
    fm_line = fm["start_line"] + 1  # 1-based・YAML 本体の先頭行
    try:
        data = yaml.safe_load(fm["yaml_text"]) or {}
    except yaml.YAMLError as exc:
        violations.append(Violation(path, fm_line, "front-matter", f"YAML 解析に失敗: {exc}"))
        return
    if not isinstance(data, dict):
        violations.append(Violation(path, fm_line, "front-matter", "front matter が mapping ではありません"))
        return

    missing_keys = [k for k in REQUIRED_FM_KEYS if k not in data]
    if missing_keys:
        violations.append(Violation(path, fm_line, "front-matter",
                                     f"必須キーが欠落: {', '.join(missing_keys)}"))

    id_value = data.get("id")
    is_stub = _is_stub_id(id_value)
    if is_stub:
        return  # 複写用の雛形（id: ""）は非空値・status語彙・ID形式/重複の検査対象外

    # 非空値チェック（related/status は下で個別に扱う）。
    for key in ("id", "title", "applies_to", "updated", "owner"):
        if key in data and (data[key] is None or (isinstance(data[key], str) and data[key].strip() == "")):
            violations.append(Violation(path, fm_line, "front-matter", f"`{key}` が空文字列です"))

    doc_type = None
    for type_name, spec in DOC_TYPES.items():
        if isinstance(id_value, str) and spec["id_re"].match(id_value):
            doc_type = type_name
            break
    if doc_type is None:
        violations.append(Violation(path, fm_line, "id-format",
                                     f"`id` の形式が既知の種別（PROP-/DES-/GLOSSARY-）のどれにも一致しません: {id_value!r}"))
    else:
        id_locations.setdefault(id_value, []).append(path)
        status = data.get("status")
        vocab = DOC_TYPES[doc_type]["status_vocab"]
        if status not in vocab:
            violations.append(Violation(path, fm_line, "status-vocab",
                                         f"種別 {doc_type} で `status: {status!r}` は許容値 {sorted(vocab)} にありません"))

    related = data.get("related")
    if related:
        if not isinstance(related, list):
            violations.append(Violation(path, fm_line, "related", "`related` はパスの配列である必要があります"))
        else:
            for entry in related:
                if not isinstance(entry, str):
                    violations.append(Violation(path, fm_line, "related", f"`related` の要素が文字列ではありません: {entry!r}"))
                    continue
                target = (path.parent / entry).resolve()
                if not target.exists():
                    violations.append(Violation(path, fm_line, "related-link",
                                                 f"`related` の参照先が実在しません: {entry}"))


def check_links(
    path: Path,
    stripped_lines: list[str],
    violations: list[Violation],
    anchor_cache: dict[Path, set[str] | None],
):
    for lineno, line in enumerate(stripped_lines, start=1):
        for m in LINK_RE.finditer(line):
            url = m.group(1).strip()
            if not url or url.startswith(("http://", "https://", "mailto:")):
                continue
            path_part, has_anchor, anchor_part = url.partition("#")
            if path_part:
                target = (path.parent / path_part).resolve()
                if not target.exists():
                    violations.append(Violation(path, lineno, "link", f"リンク切れ: {path_part}"))
                    continue
            else:
                target = path.resolve()  # `#anchor` のみ＝自文書内の見出しを指す
            if not has_anchor:
                continue
            anchors = _anchors_for_target(target, anchor_cache)
            if anchors is not None and anchor_part not in anchors:
                violations.append(Violation(
                    path, lineno, "anchor",
                    f"アンカー切れ: {url}（見出し索引に `#{anchor_part}` がありません）",
                ))


def check_contract_blocks(
    path: Path,
    stripped_lines: list[str],
    violations: list[Violation],
    contract_id_locations: dict[str, list[tuple[Path, int]]],
):
    n = len(stripped_lines)
    i = 0
    while i < n:
        line = stripped_lines[i]
        m = CONTRACT_HEADING_RE.match(line)
        if not m:
            i += 1
            continue
        heading_line = i + 1
        contract_id = m.group("id")
        if contract_id != PLACEHOLDER_CONTRACT_ID:
            if not CONTRACT_ID_RE.match(contract_id):
                violations.append(Violation(
                    path, heading_line, "contract-id-format",
                    f"契約 ID の形式が不正です（`^C-[A-Z0-9]+(-[A-Z0-9]+)*-\\d{{2}}$` に一致しません）: {contract_id!r}",
                ))
            else:
                contract_id_locations.setdefault(contract_id, []).append((path, heading_line))
        j = i + 1
        labels = []
        label_lines = []
        while j < n:
            candidate = stripped_lines[j]
            if candidate.lstrip().startswith("#"):
                break
            item = CONTRACT_ITEM_RE.match(candidate)
            if item:
                labels.append(item.group("label"))
                label_lines.append(j + 1)
            j += 1
        problems = []
        if not labels:
            problems.append("本文の項目（`- **label**:` 形式）が見つかりません")
        else:
            if labels[0] != REQUIRED_CONTRACT_ITEMS[0]:
                problems.append(f"最初の項目が「{REQUIRED_CONTRACT_ITEMS[0]}」ではありません（実際: {labels[0]}）")
            if labels[-1] != REQUIRED_CONTRACT_ITEMS[-1]:
                problems.append(f"最後の項目が「{REQUIRED_CONTRACT_ITEMS[-1]}」ではありません（実際: {labels[-1]}）")
            pos = 0
            missing = []
            for required in REQUIRED_CONTRACT_ITEMS:
                found = False
                while pos < len(labels):
                    if labels[pos] == required:
                        found = True
                        pos += 1
                        break
                    pos += 1
                if not found:
                    missing.append(required)
            if missing:
                problems.append(f"必須項目が順序どおりに見つかりません: {', '.join(missing)}（実際の並び: {', '.join(labels)}）")
        for problem in problems:
            violations.append(Violation(path, heading_line, "contract-block", problem))
        i = j


def lint_file(
    path: Path,
    id_locations: dict,
    contract_id_locations: dict[str, list[Path]],
    anchor_cache: dict[Path, set[str] | None],
    violations: list[Violation],
):
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    check_front_matter(path, lines, id_locations, violations)
    stripped = strip_fences_and_comments(text).split("\n")
    check_links(path, stripped, violations, anchor_cache)
    check_contract_blocks(path, stripped, violations, contract_id_locations)


def main(argv: list[str]) -> int:
    targets = argv[1:] or DEFAULT_TARGETS
    files: list[Path] = []
    for target in targets:
        target_path = Path(target)
        if not target_path.is_absolute():
            target_path = REPO_ROOT / target_path
        if not target_path.exists():
            print(f"対象ディレクトリが存在しません: {target}", file=sys.stderr)
            return 2
        files.extend(sorted(target_path.rglob("*.md")))

    violations: list[Violation] = []
    id_locations: dict[str, list[Path]] = {}
    contract_id_locations: dict[str, list[tuple[Path, int]]] = {}
    anchor_cache: dict[Path, set[str] | None] = {}
    for f in files:
        lint_file(f, id_locations, contract_id_locations, anchor_cache, violations)

    for id_value, locs in id_locations.items():
        if len(locs) > 1:
            for loc in locs:
                others = ", ".join(str(_display_path(p)) for p in locs if p != loc)
                violations.append(Violation(loc, 1, "id-duplicate",
                                             f"`id: {id_value}` が他にも使われています: {others}"))

    for cid, locs in contract_id_locations.items():
        if len(locs) > 1:
            for loc_path, loc_line in locs:
                others = ", ".join(
                    f"{_display_path(p)}:{ln}" for p, ln in locs if (p, ln) != (loc_path, loc_line)
                )
                violations.append(Violation(loc_path, loc_line, "contract-id-duplicate",
                                             f"契約 `id: {cid}` が他にも使われています: {others}"))

    violations.sort(key=lambda v: (str(v.path), v.line))
    for v in violations:
        print(v.format())

    if violations:
        print(f"\n{len(violations)} 件の違反（{len(files)} ファイル中）", file=sys.stderr)
        return 1
    print(f"OK: {len(files)} ファイル、違反なし")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
