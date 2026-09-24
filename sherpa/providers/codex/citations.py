"""Codex 原本直読の出典化（提案書 2026-09-10-Codex原本直読と調査スキル.md §2-5）。

Codex は原本（Excel/Word/PowerPoint/PDF・テキスト・コード）をコードインタープリターで直接開けるため、
直読した資料は MCP ツールの戻り値に載らず
`env["sources"]`（出典フッター・原本DLリンク）に反映されない。Codex に回答末尾へ固定書式
「参照した資料: <相対パス>」を書かせ、本モジュールでそれを解析し、台帳で実在確認できたものだけを
出典に昇格する（実在しない/秘匿名は捨てる・API 経路の `agentic_search`/`providers.base._verified_sources`
と同じ「モデルが触れた doc を機械検証してから出典にする」流儀）。

`parse_referenced_doc_lines`（純解析・行ごとの候補群）→ `normalize_doc_ref`（1件を doc_id へ正規化）→
`verified_referenced_docs`（秘匿除外＋実在確認・1 行 1 件の規則）の3段。平坦版 `parse_referenced_docs` は
候補の列挙用＝そのまま検証に渡さない（行全体と各片が同時に採用され 1 行 1 件の規則が失われる）。呼び出し側（`provider.py::_run_authoring`）は
MCP の `read_doc`/`read_around`/`doc_outline`/`compare_documents` 引数から集めた doc_id もこの3段目に
合流させる（参照ブロックの記載を先に・出現順・重複除去）。
"""
from __future__ import annotations

import re
from pathlib import Path

# 見出し行: 前後に `#`/`>`/`-`/`*`/`_`/`・`/空白（Markdown の見出し・引用・箇条書き・強調装飾）を
# 許容し、「参照した資料」＋（全角/半角）コロン＋強調装飾の後に続く同一行の内容（あれば）を捕捉する。
_HEADING_RE = re.compile(
    r"^[\s#>\-*_・]*\**\s*参照した資料\s*\**\s*[:：]\s*\**\s*(.*?)\s*\**\s*$"
)
# 箇条書きマーカー（`-`/`*`/`・`・番号付き `1.`/`1)`/`1、`/`1）`）の行頭除去。
_BULLET_RE = re.compile(r"^\s*(?:[-*・]|\d+[.)、）])\s*")
# 明白な箇条書き（記号や番号の後ろに空白がある）。`1.要件定義/a.md` のように空白が無い番号付きは
# パスの一部かもしれないので、行そのものも候補に残す。
_PLAIN_BULLET_RE = re.compile(r"^\s*(?:[-*・]\s+|\d+[.)、）]\s+)")
# 「x.xlsx （シート名/セル範囲/ページ）」のように**空白かバッククォートの後ろ**に付いた括弧注記だけを
# 落とす（ファイル名の一部の括弧＝`消費税法（改正）.md` は削らない・削ると別文書に化ける）。
_PAREN_RE = re.compile(r"(?<=[\s`])[（(][^（）()]*[）)]\s*$")
# 1行に複数件並ぶときの区切り（全角/半角の読点・カンマ・セミコロン）。
_SPLIT_RE = re.compile(r"[、,;]")
_QUOTE_CHARS = "`'\"“”‘’"
# 空白の後ろに続く明示の注記記号（これが続くときだけ最初の空白で切った候補を足す）。
_NOTE_MARKS = ("※", "—", "－", "―", "…", "→", "‐", "-", ":", "：")
# 行末の括弧注記（前置文字を問わない・末尾候補の生成にだけ使う）。
_TRAILING_PAREN_RE = re.compile(r"[（(][^（）()]*[）)]\s*$")


def _clean_ref(raw: str) -> str | None:
    s = raw.strip()
    s = _PAREN_RE.sub("", s).strip()
    s = s.strip(_QUOTE_CHARS).strip()
    s = _PAREN_RE.sub("", s).strip()          # `x.xlsx`（注記）のようにバッククォートの外側に付く注記
    return s or None


def _line_alternatives(raw: str) -> list[list[str]]:
    """参照ブロックの 1 行 → 候補の群（[行全体の候補...], [各片の候補...], ...）。

    先頭の群は行全体（整形前＝引用符だけ外した形、整形後＝括弧注記を除いた形）。行に区切り文字
    （`、`/`,`/`;`）があれば各片の群を続ける。**採用は `verified_referenced_docs` が「行全体の候補が
    実在すればそれだけ・実在しなければ各片」の規則で行う**＝`売上,原価.xlsx` のようにパスに区切り
    文字を含む実在ファイルを 1 件として扱い、`a.md、b.md` のように 2 件並ぶ行は 2 件として扱う
    （両方の候補を無条件に採ると、記載していない別の実在文書が出典に混ざる）。空白や引用符を含む
    整形前候補は注記付きの体裁なので積まない。
    """
    stripped = _BULLET_RE.sub("", raw).strip()
    if _PLAIN_BULLET_RE.match(raw):
        whole_units = [stripped]                          # 明白な箇条書き＝記号は候補に含めない
    else:
        whole_units = [raw.strip()] + ([stripped] if stripped != raw.strip() else [])   # `・パス`／`1.パス` は両方
    parts = _SPLIT_RE.split(stripped)
    units = [whole_units] + ([[x] for x in parts] if len(parts) > 1 else [])
    groups: list[list[str]] = []
    for unit in units:
        group: list[str] = []
        for part in unit:
            base = part.strip()
            variants = [_clean_ref(part)]
            if base and not any(ch.isspace() or ch in _QUOTE_CHARS for ch in base):
                variants.insert(0, base)
            # 末尾の候補: 空白を挟まない注記（`a.xlsx（Sheet1）`・`b.xlsx(3ページ)`・`a.md ※要約のみ`）を
            # 落とした形＝行末の括弧を前置文字によらず除いた形と、最初の空白より前の部分。
            # 先頭候補は整形前のままなので名前の括弧は壊れず、実在確認が正しい方を選ぶ。
            unq = base.strip(_QUOTE_CHARS).strip()
            tails = [_TRAILING_PAREN_RE.sub("", unq).strip().strip(_QUOTE_CHARS).strip()]
            # 「path ※注記」のように明示の注記記号が続くときだけ最初の空白で切る（空白を含む
            # ファイル名や `a.md, b.md` の複数件を切り詰めない）。
            head, _sep, rest = unq.partition(" ")
            if rest.lstrip().startswith(_NOTE_MARKS):
                tails.append(head.rstrip("、,;").strip(_QUOTE_CHARS).strip())
            variants += [t for t in tails if t]
            for v in variants:
                if v and v not in group:
                    group.append(v)
        if group:
            groups.append(group)
    return groups


# 参照項目らしい行＝箇条書き、または `/`・拡張子・バッククォートを含む（それ以外の行が来たら
# ブロックは終わり＝空行で区切られていない後続本文を参照として飲み込まない）。
_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,16}(?:[`'\"）)\s]|$)")
_EXT_END_RE = re.compile(r"\.[A-Za-z0-9]{1,16}$")
_QUOTE_PAIR = {"“": "”", "‘": "’"}


def _looks_like_ref(line: str) -> bool:
    t = line.strip()
    if not t:
        return False
    if _BULLET_RE.match(t) and _BULLET_RE.sub("", t).strip():
        return True
    # 箇条書きでない行は、行全体が引用符で囲まれたパス（内部に空白があってもよい）か、注記（空白後の
    # 括弧）と引用符を除いた残りに空白が無いときだけ参照とみなす（`a/b.md`・`` `a/b.xlsx` （Sheet1!B3）``・
    # `` `設計資料/基本 設計.xlsx` `` は参照、「注意: 反映は 2026/09/10 以降」は本文）。
    if len(t) >= 2 and t[0] in _QUOTE_CHARS and t[-1] == _QUOTE_PAIR.get(t[0], t[0]):
        core = t[1:-1]
        # 対応する引用符 1 組が行全体を囲み、中身が拡張子で終わるときだけ（`config/app.yml` の変更後は
        # `systemctl …` のようにコード表記を複数含む説明行や、引用符で囲んだ文は本文）。
        if t[0] in core or _QUOTE_PAIR.get(t[0], t[0]) in core:
            # 同じ引用符が内側にもある＝囲みではない。ただし `` `a.xlsx`、`b.xlsx` `` のように区切り文字で
            # 並べた各片が全部「対の引用符で囲まれ拡張子で終わる」なら複数件の参照行。
            pieces = [x.strip() for x in _SPLIT_RE.split(t)]
            return all(len(x) >= 2 and x[0] in _QUOTE_CHARS and x[-1] == _QUOTE_PAIR.get(x[0], x[0])
                       and x[0] not in x[1:-1] and bool(_EXT_END_RE.search(x[1:-1].strip()))
                       for x in pieces)
        return bool(_EXT_END_RE.search(core.strip()))
    core = _PAREN_RE.sub("", t).strip().strip(_QUOTE_CHARS).strip()
    if not core or any(ch.isspace() for ch in core):
        return False
    return "/" in core or bool(_EXT_RE.search(core))


def parse_referenced_doc_lines(answer: str) -> tuple[str, list[list[list[str]]]]:
    """本文末尾の「参照した資料:」ブロックを解析する（行単位）。

    戻り値＝（ブロックを除いた本文, 行ごとの候補群 `[[行全体の候補...], [片の候補...], ...]`）。
    見出しが無ければ `(answer, [])`。ブロックが複数箇所に見つかった場合は**最後**の見出しを採用する。
    見出し行に項目が無いときだけ直後の空行を読み飛ばし、項目は空行か参照らしくない行（箇条書きでも
    パスらしくもない行）まで。ブロック（見出し〜項目）だけを
    落とし、前の空行と後続の本文（注意事項など）は整えて残す。
    """
    if not answer:
        return answer, []
    lines = answer.splitlines()
    heading_idx = None
    heading_trailing = ""
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m:
            heading_idx = i
            heading_trailing = m.group(1) or ""
    if heading_idx is None:
        return answer, []

    raw_items: list[str] = []
    j = heading_idx + 1
    if heading_trailing.strip():
        raw_items.append(heading_trailing)
    else:
        while j < len(lines) and not lines[j].strip():      # 見出しだけの行＝直後の空行は読み飛ばす
            j += 1
    while j < len(lines) and lines[j].strip() and _looks_like_ref(lines[j]):
        raw_items.append(lines[j])
        j += 1

    line_groups = [g for g in (_line_alternatives(raw) for raw in raw_items) if g]

    body_lines = lines[:heading_idx]
    while body_lines and not body_lines[-1].strip():
        body_lines.pop()
    tail = lines[j:]
    while tail and not tail[0].strip():
        tail.pop(0)
    if tail:
        body_lines = body_lines + [""] + tail if body_lines else tail
    return "\n".join(body_lines), line_groups


def parse_referenced_docs(answer: str) -> tuple[str, list[str]]:
    """`parse_referenced_doc_lines` の平坦版（候補を出現順・重複除去で並べる・列挙用）。
    そのまま `verified_referenced_docs` に渡さない＝行全体と各片が同時に採用され 1 行 1 件の規則が失われる。"""
    body, line_groups = parse_referenced_doc_lines(answer)
    refs: list[str] = []
    seen: set[str] = set()
    for groups in line_groups:
        for group in groups:
            for c in group:
                if c not in seen:
                    seen.add(c)
                    refs.append(c)
    return body, refs


def _clean_rel(rel: str) -> str | None:
    """相対パス文字列の最終防御（`..`／先頭 `/`／NUL を含むものは None）。"""
    rel = rel.strip()
    if not rel or "\x00" in rel or rel.startswith("/"):
        return None
    if rel.startswith("./"):
        rel = rel[2:]
    if not rel or rel == "." or ".." in rel.split("/"):
        return None
    return rel


def normalize_doc_ref(ref: str, world: str) -> str | None:
    """生の指定文字列 → KB 相対パス（doc_id）。実在確認はしない（`verified_referenced_docs` の仕事）。

    絶対パスは KB root（`sandbox._kb_read_roots`）配下ならその相対へ、派生ルート
    （`worlds.derived_md_dir`/`worlds.derived_rag_dir`）配下なら派生の接尾辞
    （`.rag_chunks.jsonl`/`.rag.md`/`.md.meta.json`/`.md`/`.assets/...`）を落として原本の相対へ。
    どのルートにも属さない絶対パスは None。相対パスはそのまま `_clean_rel` へ。
    """
    if not ref:
        return None
    s = ref.strip().strip(_QUOTE_CHARS).strip()
    if not s or "\x00" in s:
        return None
    s = s.replace("\\", "/")
    if not s.startswith("/"):
        return _clean_rel(s)

    from ... import worlds
    from .sandbox import _kb_read_roots

    try:
        p = Path(s).resolve()
    except OSError:
        return None

    for root_s in _kb_read_roots(world):
        try:
            rel = p.relative_to(Path(root_s))
        except ValueError:
            continue
        return _clean_rel(str(rel).replace("\\", "/"))

    for fn, suffixes in (
        (worlds.derived_md_dir, (".md.meta.json", ".md")),
        (worlds.derived_rag_dir, (".rag_chunks.jsonl", ".rag.md")),
    ):
        try:
            root = fn(world).resolve()
        except OSError:
            continue
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        rel_s = str(rel).replace("\\", "/")
        if ".assets/" in rel_s:
            rel_s = rel_s.split(".assets/")[0]
        else:
            for suf in suffixes:
                if rel_s.endswith(suf):
                    rel_s = rel_s[: -len(suf)]
                    break
        return _clean_rel(rel_s)
    return None


def _verify_one(ref: str, world: str, scope_paths) -> str | None:
    from ...ingest import text_kind
    from ... import agentic_search

    doc = normalize_doc_ref(ref, world)
    if not doc or text_kind.is_sensitive_doc_id(doc):
        return None
    return doc if agentic_search.verify_doc_exists(doc, world, scope_paths) else None


def verified_referenced_docs(refs: list, world: str, scope_paths=None) -> list[str]:
    """参照の候補 → 秘匿名を落とし実在確認を通った doc_id の一覧（出現順・重複除去）。

    `refs` の各要素は文字列（1 件＝MCP 引数など）か、`parse_referenced_doc_lines` の行の候補群
    （`[[行全体の候補...], [片の候補...], ...]`）。候補群は「行全体の候補のうち最初に実在したもの
    だけ。行全体が実在しなければ各片の最初に実在したもの」を採る（1 行 1 件の契約・記載していない
    別文書を混ぜない）。
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(doc: str | None) -> None:
        if doc and doc not in seen:
            seen.add(doc)
            out.append(doc)

    def _first(group: list) -> str | None:
        for c in group:
            d = _verify_one(c, world, scope_paths)
            if d:
                return d
        return None

    for r in refs:
        if isinstance(r, str):
            _add(_verify_one(r, world, scope_paths))
            continue
        groups = list(r)
        if not groups:
            continue
        whole = _first(groups[0])
        if whole:
            _add(whole)
            continue
        for part in groups[1:]:
            _add(_first(part))
    return out
