"""Codex 原本直読の出典化。

Codex が直接開いた資料は MCP の戻り値に載らないため、回答末尾の固定書式「参照した資料: <相対パス>」を解析し、実在確認できたものだけを出典（`env["sources"]`）に昇格する。
`parse_referenced_doc_lines`（行ごとの候補群）→ `normalize_doc_ref`（doc_id へ正規化）→ `verified_referenced_docs`（秘匿除外＋実在確認・1 行 1 件）の3段。
設計: docs/design/codex.md「1ターンの流れ」
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

_log = logging.getLogger("sherpa")

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


_FENCE_OPEN_RE = re.compile(r"^\s*(?:(?:[-*+]|\d{1,9}[.)])\s+)?(`{3,}|~{3,})")


_QUOTE_PREFIX_RE = re.compile(r"^ {0,3}>(?: ?>)* ?")
_INDENT_CODE_RE = re.compile(r"^(?: {4}|\t)")


def code_block_lines(lines: list[str]) -> list[bool]:
    """各行がコードブロック（``` ・ ~~~ の囲み〔箇条書きの中の字下げしたものを含む〕、空行の後の字下げ。引用の中を含む）の行か。囲みの記号の行も含める。"""
    out: list[bool] = []
    fence = None
    prev_blank, indented = True, False
    for line in lines:
        quote = _QUOTE_PREFIX_RE.match(line)
        inner = line[quote.end():] if quote else line
        blank = not inner.strip()
        f = _FENCE_OPEN_RE.match(inner)
        if fence is not None:
            out.append(True)
            if f and f.group(1)[0] == fence[0] and len(f.group(1)) >= len(fence) and not inner.strip(" \t`~"):
                fence = None
            prev_blank = blank
            continue
        if f:
            fence = f.group(1)
            out.append(True)
            prev_blank, indented = False, False
            continue
        if (indented or prev_blank) and (blank and indented or (not blank and _INDENT_CODE_RE.match(inner))):
            indented = True
            out.append(not blank)
        else:
            indented = False
            out.append(False)
        prev_blank = blank
    return out


def last_heading_index(lines: list[str], heading_re) -> int | None:
    """`heading_re` に合う最後の行の位置。コードブロックの中の行は見出しとみなさない。"""
    found = None
    for i, (line, code) in enumerate(zip(lines, code_block_lines(lines))):
        if not code and heading_re.match(line):
            found = i
    return found


def parse_referenced_doc_lines(answer: str) -> tuple[str, list[list[list[str]]]]:
    """本文末尾の「参照した資料:」ブロックを解析する。
    戻り値＝（ブロックを除いた本文, 行ごとの候補群）。見出しが無ければ `(answer, [])`。複数あれば最後の見出しを採る。
    コードブロック（``` ・ ~~~）の中の見出しは欄として扱わない。
    """
    if not answer:
        return answer, []
    lines = answer.splitlines()
    heading_idx = last_heading_index(lines, _HEADING_RE)
    if heading_idx is None:
        return answer, []
    heading_trailing = _HEADING_RE.match(lines[heading_idx]).group(1) or ""

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
REASON_OUT_OF_LAYER = "今回調べる対象の種類（資料／ソース）の外です"
REASON_UNCHECKED = "時間内に確かめきれませんでした"
UNVERIFIED_PATH_MAX_LEN = 200

# 参照 1 行の対応付けの分類（内部の記録・評価で数える）。
RES_DIRECT = "direct"                    # パスのとおりに実在を確認できた
RES_UNIQUE = "unique_by_name"            # ファイル名だけの指定が、範囲内で 1 件に決まった
RES_AMBIGUOUS = "ambiguous"              # 範囲内に同名が複数ある（どれも選ばない）
RES_NOT_FOUND = "not_found"              # 資料が無い（ファイル名だけの指定では、範囲外にだけ有る場合も含める）
RES_UNCHECKED = "unchecked"              # 台帳に無い名前を原本で引き直す途中で期限を超えた（有るか無いか分からない）
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


def _index_rels(rels, scope_paths, layer) -> dict[str, list[str]]:
    """rel_path の集合から、許可された範囲（scope_paths・層）にある非秘匿のものだけを、ファイル名 → rel_path 一覧にする。"""
    from ... import layer as layer_mod, scope as scope_mod
    from ...ingest import text_kind

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


def _name_index(world: str, scope_paths, layer) -> dict[str, list[str]]:
    """許可された範囲（scope_paths・層）にある非秘匿の資料の、ファイル名 → rel_path 一覧。範囲外の資料は入れない。
    資料の一覧は取り込みの台帳から出し、台帳が無いときだけ資料フォルダを（期限つきで）歩く。"""
    from ... import documents

    try:
        rels = documents.known_rels(world)
    except Exception:
        return {}
    return _index_rels(rels, scope_paths, layer)


class _WalkNameIndex:
    """台帳の索引に無かった名前の引き直し用。範囲の部分木だけを、最初に引かれたときだけ 1 回（期限つきで）歩いて名前の索引にする。
    層は拡張子でなく取り込みと同じ分類で、引いた候補だけに当てる。期限を超えた・読めない部分があったときは `timed_out` を立てる（歩いた分だけでは決めない）。"""

    def __init__(self, world: str, scope_paths, layer) -> None:
        self._args = (world, scope_paths, layer)
        self._index: dict[str, list[str]] | None = None
        self._deadline: float | None = None
        self.timed_out = False

    def get(self, name: str) -> list[str]:
        from ... import doc_reach

        if self._index is None:
            self._index = self._build()
        if self.timed_out:
            return []
        world, _scope_paths, layer = self._args
        out = []
        for r in self._index.get(name, []):
            if time.monotonic() > self._deadline:
                self.timed_out = True
                return []
            if doc_reach.in_layer_confirmed(r, world, layer):
                out.append(r)
        if time.monotonic() > self._deadline:
            self.timed_out = True
            return []
        return out

    def _build(self) -> dict[str, list[str]]:
        from ... import documents, scope_infer

        world, scope_paths, layer = self._args
        self._deadline = scope_infer.user_op_deadline()
        try:
            skipped: dict = {}
            rels = documents.scope_rel_set(world, scope_paths, deadline=self._deadline, skipped=skipped)
        except scope_infer.ScopeWalkDeadlineExceeded:
            _log.warning("citations: name lookup walk exceeded deadline; shown as unchecked world=%s", world)
            self.timed_out = True
            return {}
        if documents.walk_incomplete(skipped):
            _log.warning("citations: name lookup walk could not read part of the tree; shown as unchecked world=%s", world)
            self.timed_out = True
            return {}
        return _index_rels(rels, scope_paths, None)


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


def resolve_referenced_docs_detail(refs: list, world: str, scope_paths=None, layer=None, index=None) -> dict:
    """参照の候補 → 検証結果。キーは `verified`（検証を通った doc_id）・`unverified`（通らなかった行の {path, reason}）・
    `hidden`（名前を出さず件数だけにした数）・`mappings`（行ごとの {input, doc_id, class}。評価で数える。名前を出せない行の input は空）。
    ファイル名だけの指定は、パスのとおりに実在しないとき、範囲（scope_paths・層）内で同名が 1 件だけなら、その資料の相対パスへ
    置き換えて同じ検証（実在・文書種別・範囲・秘匿）を通す。台帳の索引で見つからない名前だけ、範囲内の原本の木を 1 回（期限つき）歩いて引き直す。2 件以上は曖昧として選ばず、範囲外の同名は候補に入れない（存在を漏らさない）。
    行番号つきの指定が資料の行数を超えても資料の検証は変えず、`mappings` にだけ位置の未確認を残す。
    `index` は `_name_index` の結果を呼び出し側が持っているときに渡す（省略時は必要になったときに作る）。
    """
    out: list[str] = []
    seen: set[str] = set()
    failures: list[dict] = []
    mappings: list[dict] = []
    sensitive = 0
    walked = _WalkNameIndex(world, scope_paths, layer)

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
            hits = index.get(bare, []) or walked.get(bare)
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
            elif not hits and walked.timed_out:
                row = {"path": bare[:UNVERIFIED_PATH_MAX_LEN], "reason": REASON_UNCHECKED}
                cls = RES_UNCHECKED
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


# 回答の本文から拾う資料名のうち確かめる件数の上限と、数えるだけの件数の上限（重複を除いた件数）。
MENTION_MAX = 50
MENTION_SCAN_MAX = 1000
# 区切りがはっきりした所: バッククォート・「」・『』の中
_QUOTED_RE = re.compile(r"`([^`\n]+)`|「([^「」\n]+)」|『([^『』\n]+)』")
# 囲みの中身が資料名か: 拡張子で終わる（後ろの行番号 `:12`・`:12-20` は外す）
_QUOTED_NAME_RE = re.compile(r"\S(?:.*\S)?\.[A-Za-z][A-Za-z0-9]{0,15}")
_LINE_TAIL_RE = _LINE_SUFFIX_RE
# 囲まれていないパスの前後の区切り（英数字〔全角を含む〕・`/`・`\`・`:`・`@`・`=`・`?`・`&`・`#` の続きなら長いパス・URL の一部とみなす。後ろの `?`・`#`・`&`・`=` も URL の続きとして除き、`:` は行番号のときだけ許す）
_PATH_BEFORE_BAD = re.compile(r"[A-Za-z0-9_\-./\\:@=?&#\uFF10-\uFF19\uFF21-\uFF3A\uFF41-\uFF5A]")
_PATH_AFTER_BAD = re.compile(r"[A-Za-z0-9_\-/\\@?#&=\uFF10-\uFF19\uFF21-\uFF3A\uFF41-\uFF5A]|\.[A-Za-z0-9]|:(?!\d)")


def _quoted_name(inner: str) -> str | None:
    """囲みの中身が資料名ならその名前（行番号を外す）。URL・複数行・長すぎるものは None。"""
    name = _LINE_TAIL_RE.sub("", inner.strip())
    if len(name) > 300 or "://" in name or not _QUOTED_NAME_RE.fullmatch(name):
        return None
    return name


def extract_doc_tokens(text: str, limit: int = MENTION_MAX, paths: frozenset[str] | set[str] = frozenset()) -> list[str]:
    """本文から資料名の候補を出現順に重複なしで `limit` 件まで拾う。
    拾うのは、バッククォート・「」・『』で囲まれた中身のうち拡張子で終わるものと、囲まれていなくても `paths`
    （範囲にある資料のパス。フォルダを含むもの）とそのまま一致するものだけ。囲まれていない裸のファイル名と、コードブロックの中は拾わない。"""
    # コードブロックの中は拾わない（行の長さは変えずに空白にする）
    lines = (text or "").split("\n")
    text = "\n".join(" " * len(ln) if code else ln for ln, code in zip(lines, code_block_lines(lines)))
    hits: list[tuple[int, str]] = []
    for m in _QUOTED_RE.finditer(text):
        inner = next(g for g in m.groups() if g is not None)
        name = _quoted_name(inner)
        if name:
            hits.append((m.start(), name))
    for path in paths:
        if "/" not in path or path not in text:
            continue
        for m in re.finditer(re.escape(path), text):
            before = text[m.start() - 1] if m.start() else ""
            if (before and _PATH_BEFORE_BAD.match(before)) or _PATH_AFTER_BAD.match(text, m.end()):
                continue
            hits.append((m.start(), path))
    out: list[str] = []
    for _, tok in sorted(hits):
        if tok not in out:
            out.append(tok)
            if len(out) >= limit:
                break
    return out


def index_paths(index: dict[str, list[str]]) -> frozenset[str]:
    """名前の索引にある資料のパス（`extract_doc_tokens` の `paths`）。"""
    return frozenset(rel for rels in index.values() for rel in rels)


def screen_by_layer(tokens: list[str], world: str, layer) -> tuple[list[str], list[dict]]:
    """資料名の候補のうち、指した資料が今回の層（資料／ソース）の外のものを分ける（資料フォルダ直下の資料・ファイル名だけの候補も含む）。
    戻り値＝（残す候補, 層の外の `{path, reason}`）。秘匿名は分けずに後段の確かめ（件数だけ）に任せる。"""
    from ... import layer as layer_mod
    from ...ingest import text_kind

    kept: list[str] = []
    out: list[dict] = []
    for t in tokens:
        doc = normalize_doc_ref(t, world)
        try:
            outside = bool(doc and not text_kind.is_sensitive_doc_id(doc)
                           and not layer_mod.in_layer(doc, layer))
        except ValueError:
            outside = False
        if outside:
            row = {"path": doc[:UNVERIFIED_PATH_MAX_LEN], "reason": REASON_OUT_OF_LAYER}
            if row not in out:
                out.append(row)
        else:
            kept.append(t)
    return kept, out


def plausible_doc_tokens(tokens: list[str], index: dict[str, list[str]]) -> list[str]:
    """資料名の候補のうち、フォルダを含むパスと、拡張子が資料らしいファイル名（範囲にある資料の拡張子・既知の文書／コード／Office／秘匿の拡張子）。
    `e.g` や `example.com` のような文中の語を「確認できなかった資料」にしないための絞り込み。"""
    from ...ingest import office_md, text_kind

    known = ({Path(n).suffix.lower() for n in index} | set(text_kind.CODE_EXT) | set(text_kind.DOCUMENT_EXT)
             | set(text_kind.SENSITIVE_EXT) | set(office_md.OFFICE_EXT) | {".md", ".txt"})
    return [t for t in tokens if "/" in t or Path(t).suffix.lower() in known]


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
