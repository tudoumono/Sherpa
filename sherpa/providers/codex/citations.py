"""Codex 原本直読の出典化。

Codex が直接開いた資料は MCP の戻り値に載らないため、回答末尾の固定書式「参照した資料: <相対パス>」を解析し、実在確認できたものだけを出典（`env["sources"]`）に昇格する。
`parse_referenced_doc_lines`（行ごとの候補群）→ `normalize_doc_ref`（doc_id へ正規化）→ `verified_referenced_docs`（秘匿除外＋実在確認・1 行 1 件）の3段。
設計: docs/design/codex.md「1ターンの流れ」
"""
from __future__ import annotations

import re
from pathlib import Path

# 見出し行（前後の Markdown 装飾・全角/半角コロンを許容し、同一行の内容を捕捉する）。
_HEADING_RE = re.compile(
    r"^[\s#>\-*_・]*\**\s*参照した資料\s*\**\s*[:：]\s*\**\s*(.*?)\s*\**\s*$"
)
# 箇条書きマーカーの行頭除去。
_BULLET_RE = re.compile(r"^\s*(?:[-*・]|\d+[.)、）])\s*")
# 記号や番号の後ろに空白がある明白な箇条書き（空白の無い `1.要件定義/a.md` は行そのものも候補に残す）。
_PLAIN_BULLET_RE = re.compile(r"^\s*(?:[-*・]\s+|\d+[.)、）]\s+)")
# 空白かバッククォートの後ろに付いた括弧注記だけを落とす（ファイル名の一部の括弧は削らない）。
_PAREN_RE = re.compile(r"(?<=[\s`])[（(][^（）()]*[）)]\s*$")
# 1 行に複数件並ぶときの区切り。
_SPLIT_RE = re.compile(r"[、,;]")
_QUOTE_CHARS = "`'\"“”‘’"
# 空白の後ろに続く明示の注記記号。
_NOTE_MARKS = ("※", "—", "－", "―", "…", "→", "‐", "-", ":", "：")
# 行末の括弧注記（末尾候補の生成にだけ使う）。
_TRAILING_PAREN_RE = re.compile(r"[（(][^（）()]*[）)]\s*$")
# 行末の行番号の指定（`パス:12`・`パス:12-34, 56`）。末尾候補の生成にだけ使う。
_LINE_SUFFIX_RE = re.compile(r":\d+(?:-\d+)?(?:,\s*\d+(?:-\d+)?)*\s*$")


def _clean_ref(raw: str) -> str | None:
    s = raw.strip()
    s = _PAREN_RE.sub("", s).strip()
    s = s.strip(_QUOTE_CHARS).strip()
    s = _PAREN_RE.sub("", s).strip()  # バッククォートの外側に付く注記
    return s or None


def _line_alternatives(raw: str) -> list[list[str]]:
    """参照ブロックの 1 行 → 候補の群（[行全体の候補...], [各片の候補...], ...）。
    採用は `verified_referenced_docs` が行う（行全体が実在すればそれだけ・実在しなければ各片）。
    """
    stripped = _BULLET_RE.sub("", raw).strip()
    if _PLAIN_BULLET_RE.match(raw):
        whole_units = [stripped]  # 明白な箇条書き＝記号は候補に含めない
    else:
        whole_units = [raw.strip()] + ([stripped] if stripped != raw.strip() else [])  # `・パス`／`1.パス` は両方
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
            # 末尾の候補: 空白を挟まない注記を落とした形と、最初の空白より前の部分。
            unq = base.strip(_QUOTE_CHARS).strip()
            tails = [_TRAILING_PAREN_RE.sub("", unq).strip().strip(_QUOTE_CHARS).strip()]
            if _LINE_SUFFIX_RE.search(unq):
                tails.append(_LINE_SUFFIX_RE.sub("", unq).strip().strip(_QUOTE_CHARS).strip())
            # 「path ※注記」のように明示の注記記号が続くときだけ最初の空白で切る。
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


# 参照項目らしい行（箇条書き、または `/`・拡張子・バッククォートを含む）。それ以外が来たらブロック終了。
_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,16}(?:[`'\"）)\s]|$)")
_EXT_END_RE = re.compile(r"\.[A-Za-z0-9]{1,16}$")
_QUOTE_PAIR = {"“": "”", "‘": "’"}


def _looks_like_ref(line: str) -> bool:
    t = line.strip()
    if not t:
        return False
    if _BULLET_RE.match(t) and _BULLET_RE.sub("", t).strip():
        return True
    # 箇条書きでない行は、行全体が引用符で囲まれたパスか、注記と引用符を除いた残りに空白が無いときだけ参照とみなす。
    if len(t) >= 2 and t[0] in _QUOTE_CHARS and t[-1] == _QUOTE_PAIR.get(t[0], t[0]):
        core = t[1:-1]
        # 対の引用符 1 組が行全体を囲み、中身が拡張子で終わるときだけ参照とみなす。
        if t[0] in core or _QUOTE_PAIR.get(t[0], t[0]) in core:
            # 内側にも同じ引用符がある場合は囲みではない。区切りで並べた各片が全部引用符で囲まれ拡張子で終わるなら複数件の参照行。
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
    """本文末尾の「参照した資料:」ブロックを解析する。
    戻り値＝（ブロックを除いた本文, 行ごとの候補群）。見出しが無ければ `(answer, [])`。複数あれば最後の見出しを採る。
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
        while j < len(lines) and not lines[j].strip():  # 見出しだけの行＝直後の空行は読み飛ばす
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
    """生の指定文字列 → KB 相対パス（doc_id）。実在確認はしない。
    KB root 配下の絶対パスは相対へ、派生ルート配下は派生の接尾辞を落として原本の相対へ。どのルートにも属さない絶対パスは None。
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


# 検証を通らなかった参照の理由（利用者向けの文）。
REASON_OUT_OF_SCOPE = "今回の範囲の外です"
REASON_UNREADABLE = "見つからないか、読めない形式です"
REASON_AMBIGUOUS = "同じ名前の資料が複数あり、どれか特定できません"
UNVERIFIED_PATH_MAX_LEN = 200

# 参照 1 行の対応付けの分類（内部の記録・評価で数える）。
RES_DIRECT = "direct"                    # パスのとおりに実在を確認できた
RES_UNIQUE = "unique_by_name"            # ファイル名だけの指定が、範囲内で 1 件に決まった
RES_AMBIGUOUS = "ambiguous"              # 範囲内に同名が複数ある（どれも選ばない）
RES_NOT_FOUND = "not_found"              # 資料が無い（ファイル名だけの指定では、範囲外にだけ有る場合も含める）
RES_OUT_OF_SCOPE = "out_of_scope"        # パスつきの指定が、許可された範囲の外
RES_POSITION_UNVERIFIED = "position_unverified"  # 資料は特定できたが行番号が資料の行数を超える
RES_HIDDEN = "hidden"                    # 秘匿名・自由な文・解釈できない行（名前を残さない）
_POSITION_CHECK_MAX_BYTES = 8 * 1024 * 1024
_POSITION_SKIP_EXT = {".docx", ".xlsx", ".pptx", ".pdf", ".doc", ".xls", ".ppt", ".png", ".jpg", ".jpeg", ".zip"}


def _bare_name(candidates: list, world: str) -> str | None:
    """候補のうち、フォルダを含まず拡張子で終わるファイル名だけの指定（秘匿名・制御文字入りは除く）。"""
    from ...ingest import text_kind

    for c in candidates:
        doc = normalize_doc_ref(c, world)
        if (doc and "/" not in doc and _EXT_END_RE.search(doc) and not any(ord(ch) < 32 for ch in doc)
                and not text_kind.is_sensitive_doc_id(doc)):
            return doc
    return None


def _name_index(world: str, scope_paths, layer) -> dict[str, list[str]]:
    """許可された範囲（scope_paths・層）にある非秘匿の資料の、ファイル名 → rel_path 一覧。範囲外の資料は入れない。"""
    from ... import documents, layer as layer_mod, scope as scope_mod
    from ...ingest import text_kind

    try:
        rels = documents.world_rel_set(world)
    except Exception:
        return {}
    index: dict[str, list[str]] = {}
    for rel in sorted(rels):
        if text_kind.is_sensitive_doc_id(rel):
            continue
        if scope_paths is not None and not scope_mod.in_scope(rel, scope_paths):
            continue
        try:
            if not layer_mod.in_layer(rel, layer):
                continue
        except ValueError:
            continue
        index.setdefault(rel.rsplit("/", 1)[-1], []).append(rel)
    return index


def _position_unverified(doc: str, candidates: list, world: str) -> bool:
    """行番号つきの指定が、テキスト資料の行数を超えるか。確かめられない種別・大きさは False（未確認とは区別しない）。"""
    nums: list[int] = []
    for c in candidates:
        m = _LINE_SUFFIX_RE.search(c.strip().strip(_QUOTE_CHARS).strip())
        if m:
            nums = [int(n) for n in re.findall(r"\d+", m.group(0))]
            break
    if not nums:
        return False
    from ... import documents
    if Path(doc).suffix.lower() in _POSITION_SKIP_EXT:
        return False
    try:
        path = documents.resolve(doc, world)
        if path is None or path.stat().st_size > _POSITION_CHECK_MAX_BYTES:
            return False
        data = path.read_bytes()
    except Exception:
        return False
    if b"\x00" in data[:4096]:  # テキストでない資料は行数を数えない
        return False
    total = data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1)
    return max(nums) > total


def _classify_failure(candidates: list, world: str, scope_paths) -> tuple[dict | None, bool]:
    """検証を通らなかった 1 件（候補の列）の理由。`(行, 名前を出せないか)`。
    名前を出すのは、資料フォルダ内の資料パスとして正規化でき（拡張子で終わる）、秘匿名でないものだけ。
    秘匿名・自由な文・解釈できない行は名前を返さず（行は None）、件数だけに使う。"""
    from ...ingest import text_kind
    from ... import scope as scope_mod

    first_doc = None
    for c in candidates:
        doc = normalize_doc_ref(c, world)
        if not doc:
            continue
        if text_kind.is_sensitive_doc_id(doc):
            return None, True
        if first_doc is None and _EXT_END_RE.search(doc) and not any(ord(ch) < 32 for ch in doc):
            first_doc = doc
    if first_doc is not None:
        reason = (REASON_OUT_OF_SCOPE if scope_paths is not None and not scope_mod.in_scope(first_doc, scope_paths)
                  else REASON_UNREADABLE)
        return {"path": first_doc[:UNVERIFIED_PATH_MAX_LEN], "reason": reason}, False
    return None, True


def resolve_referenced_docs_detail(refs: list, world: str, scope_paths=None, layer=None) -> dict:
    """参照の候補 → 検証結果。キーは `verified`（検証を通った doc_id）・`unverified`（通らなかった行の {path, reason}）・
    `hidden`（名前を出さず件数だけにした数）・`mappings`（行ごとの {input, doc_id, class}。評価で数える。名前を出せない行の input は空）。
    ファイル名だけの指定は、パスのとおりに実在しないとき、範囲（scope_paths・層）内で同名が 1 件だけなら、その資料の相対パスへ
    置き換えて同じ検証（実在・文書種別・範囲・秘匿）を通す。2 件以上は曖昧として選ばず、範囲外の同名は候補に入れない（存在を漏らさない）。
    行番号つきの指定が資料の行数を超えても資料の検証は変えず、`mappings` にだけ位置の未確認を残す。
    """
    out: list[str] = []
    seen: set[str] = set()
    failures: list[dict] = []
    mappings: list[dict] = []
    sensitive = 0
    index: dict[str, list[str]] | None = None

    def _add(doc: str | None) -> None:
        if doc and doc not in seen:
            seen.add(doc)
            out.append(doc)

    def _first(group: list) -> tuple[str | None, str]:
        for c in group:
            d = _verify_one(c, world, scope_paths)
            if d:
                return d, c
        return None, ""

    def _record(group: list, doc: str | None, cls: str) -> None:
        shown = "" if cls == RES_HIDDEN else (group[0] if group else "")[:UNVERIFIED_PATH_MAX_LEN]
        mappings.append({"input": shown, "doc_id": doc, "class": cls})

    def _fail(group: list) -> None:
        nonlocal sensitive, index
        bare = _bare_name(group, world)
        if bare is not None:
            if index is None:
                index = _name_index(world, scope_paths, layer)
            hits = index.get(bare, [])
            if len(hits) == 1:
                doc = _verify_one(hits[0], world, scope_paths)
                if doc:
                    _add(doc)
                    pos = _position_unverified(doc, group, world)
                    _record(group, doc, RES_POSITION_UNVERIFIED if pos else RES_UNIQUE)
                    return
            if len(hits) > 1:
                row = {"path": bare[:UNVERIFIED_PATH_MAX_LEN], "reason": REASON_AMBIGUOUS}
                cls = RES_AMBIGUOUS
            else:
                row = {"path": bare[:UNVERIFIED_PATH_MAX_LEN], "reason": REASON_UNREADABLE}
                cls = RES_NOT_FOUND
            _record(group, None, cls)
            if row not in failures:
                failures.append(row)
            return
        row, is_sensitive = _classify_failure(group, world, scope_paths)
        if is_sensitive:
            sensitive += 1  # 名前を出せない参照（秘匿名・自由な文・解釈できない行）は件数だけ
            _record(group, None, RES_HIDDEN)
        elif row is not None:
            _record(group, None, RES_OUT_OF_SCOPE if row["reason"] == REASON_OUT_OF_SCOPE else RES_NOT_FOUND)
            if row not in failures:
                failures.append(row)

    def _hit(group: list, doc: str, cand: str) -> None:
        _add(doc)
        _record(group, doc, RES_POSITION_UNVERIFIED if _position_unverified(doc, group, world) else RES_DIRECT)

    for r in refs:
        if isinstance(r, str):
            _add(_verify_one(r, world, scope_paths))
            continue
        groups = list(r)
        if not groups:
            continue
        whole, cand = _first(groups[0])
        if whole:
            _hit(groups[0], whole, cand)
            continue
        if len(groups) == 1:
            _fail(groups[0])
            continue
        for part in groups[1:]:
            doc, cand = _first(part)
            if doc:
                _hit(part, doc, cand)
            else:
                _fail(part)
    return {"verified": out, "unverified": failures, "hidden": sensitive, "mappings": mappings}


def resolve_referenced_docs(refs: list, world: str, scope_paths=None, layer=None) -> tuple[list[str], list[dict], int]:
    """参照の候補 → `(検証を通った doc_id の一覧, 通らなかった行の {path, reason} の一覧, 名前を出さず件数だけにした数)`。
    `refs` の要素は文字列か行の候補群（`verified_referenced_docs` と同じ採り方）。通らなかったものを数えるのは行の候補群（回答の「参照した資料」の行）だけで、
    文字列（道具の引数から拾った資料）は検証を通らなくても数えない。名前を出せない参照（秘匿名・自由な文・解釈できない行）は名前を出さず件数だけ返す。
    """
    d = resolve_referenced_docs_detail(refs, world, scope_paths, layer)
    return d["verified"], d["unverified"], d["hidden"]


def verified_referenced_docs(refs: list, world: str, scope_paths=None) -> list[str]:
    """参照の候補 → 秘匿名を落とし実在確認を通った doc_id の一覧（出現順・重複除去）。
    `refs` の要素は文字列か行の候補群。候補群は行全体の候補のうち最初に実在したもの、無ければ各片の最初に実在したものを採る（1 行 1 件）。
    """
    return resolve_referenced_docs(refs, world, scope_paths)[0]
