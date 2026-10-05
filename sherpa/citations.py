"""引用（citation / evidence）整形の単一の真実源。grep/ES ヒット → API 露出用の citation dict を組む。
整形だけを受け持ち、検索・実在チェック・redaction/clip などのポリシー判断は呼び出し側に残す。
設計: docs/design/chat.md「1ターンの流れ」

各サイトの形:
- evidence.grep: `{doc_id, line, span, text, match}`（path/ext は出さない）。
- QA/agentic citation: `{doc_id, span, quote, ext}`（agentic は match 無し）。
- ES citation: span=`[line, line]`・`match`=query。

守ること: citation dict はここで返す形のまま公開・保存される。rag_chunks 由来の `locator`/`chunk_id` を
キーとして持たせない（出典フッターに内部表現を出さない）。
加算フィールド `excerpt_source`・`locator_hint`・`tier` は `with_display_excerpt` が付与する（`quote` は利用者向けの本文に上書きされる）。
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping


def public_grep_hit(h: Mapping) -> dict:
    return {"doc_id": h["doc_id"], "line": h["line"], "span": h["span"],
            "text": h["text"], "match": h["match"]}


def from_grep_hit(h: Mapping, *, quote: str | None = None, include_match: bool = True) -> dict:
    """grep ヒット → citation（QA/agentic 共用）。`quote` 未指定なら本文 `h["text"]`。
    redaction/clip は呼び出し側が `quote` を作って渡す。agentic は `include_match=False`。
    """
    c = {"doc_id": h["doc_id"], "span": h.get("span"),
         "quote": h.get("text", "") if quote is None else quote, "ext": h.get("ext")}
    if include_match:
        c["match"] = h.get("match")
    return c


def from_es_hit(h: Mapping, query: str, *, quote: str | None = None, include_match: bool = True) -> dict:
    """ES ヒット → citation。span は `[line, line]`、`match`=query（include_match 時）。`locator`/`chunk_id` は付けない。"""
    c = {"doc_id": h["doc_id"], "span": [h.get("line"), h.get("line")],
         "quote": h.get("text", "") if quote is None else quote, "ext": h.get("ext")}
    if include_match:
        c["match"] = query
    return c


_LOCATOR_FIELD_MAX = 40  # locator の 1 フィールド（シート名/セル範囲）の上限文字数
_LOCATOR_HINT_MAX = 60  # 位置ヒント全体の上限文字数
_LOCATOR_NUMBER_MAX = 999_999  # page/slide の桁上限（6 桁）
_LOCATOR_WS_RE = re.compile(r"\s+")  # Unicode 空白（`str.splitlines()` が改行とみなす全種を含む）


def _clean_locator_field(value, limit: int = _LOCATOR_FIELD_MAX) -> str | None:
    """locator の 1 フィールドを検証する。文字列のみ・空白類を単一空白へ正規化・引用符エスケープ・長さ上限。
    不正値は None。
    """
    if not isinstance(value, str):
        return None
    v = _LOCATOR_WS_RE.sub(" ", value).replace("「", "『").replace("」", "』").strip()
    return v if v and len(v) <= limit else None


def _clean_locator_number(value) -> int | None:
    """locator の page/slide を検証する。正の非 bool int のみ・6 桁上限。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 < value <= _LOCATOR_NUMBER_MAX else None


def locator_hint(locator: Mapping | None) -> str:
    """rag_chunks の `locator` を短い日本語の位置ヒントに整形する（回答生成時に LLM へ渡す専用・出典フッターには出さない）。
    既知の組み合わせ（シート+セル範囲／ページ／スライド）以外は空文字。値は検証・正規化・長さ上限を通す。
    """
    if not isinstance(locator, Mapping):
        return ""
    sheet, cell_range = _clean_locator_field(locator.get("sheet")), _clean_locator_field(locator.get("cell_range"))
    if sheet and cell_range:
        return f"シート「{sheet}」{cell_range}"[:_LOCATOR_HINT_MAX]
    page = _clean_locator_number(locator.get("page"))
    if page is not None:
        return f"p.{page}"[:_LOCATOR_HINT_MAX]
    slide = _clean_locator_number(locator.get("slide"))
    if slide is not None:
        return f"スライド{slide}"[:_LOCATOR_HINT_MAX]
    return ""


def with_display_excerpt(citation: Mapping, *, quote: str, excerpt_source: str,
                         locator_hint: str | None = None, tier: str | None = None) -> dict:
    """citation の `quote` を利用者向けに引き直した本文へ差し替える（`excerpts.display_quote` の結果を渡す）。
    `excerpt_source` は常に付与し、`locator_hint`/`tier` は値があるときだけキーを立てる。
    非 agentic 経路（`chat_service`/`lens_service`）専用。
    """
    out = {**citation, "quote": quote, "excerpt_source": excerpt_source}
    if locator_hint:
        out["locator_hint"] = locator_hint
    if tier:
        out["tier"] = tier
    return out


def with_display_text(evidence: Mapping, *, text: str, excerpt_source: str,
                      locator_hint: str | None = None) -> dict:
    """`with_display_excerpt` の evidence.grep 形（`quote` でなく `text` キー）向け。"""
    out = {**evidence, "text": text, "excerpt_source": excerpt_source}
    if locator_hint:
        out["locator_hint"] = locator_hint
    return out


def citation_dedupe_key(c: Mapping) -> tuple:
    """citation の重複排除鍵。既定 `(doc_id, span)`。span が行番号を持たない（rag_chunks 由来）ときは `quote` も鍵に加える。
    `dedupe_round_robin_by_doc_span` と `providers/base.py` の agentic citation 集約が共用する。
    """
    span = tuple(c.get("span") or ())
    return (c.get("doc_id"), span) if any(span) else (c.get("doc_id"), span, c.get("quote"))


def dedupe_round_robin_by_doc_span(*groups: Iterable[Mapping]) -> list:
    """複数 citation 群を渡した順に round-robin で並べ、`citation_dedupe_key` で重複排除する。doc_id 無しは捨てる。"""
    gs = [list(g) for g in groups]
    merged, seen = [], set()
    for i in range(max((len(g) for g in gs), default=0)):
        for g in gs:
            if i < len(g):
                c = g[i]
                key = citation_dedupe_key(c)
                if c.get("doc_id") and key not in seen:
                    seen.add(key)
                    merged.append(c)
    return merged
