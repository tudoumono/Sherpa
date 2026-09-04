"""引用（citation / evidence）整形の単一の真実源（rv-full B1・DRY）。

grep/ES ヒット → API 露出用の citation dict を組む処理が lens_service／chat_service／agentic_search に
分散していたのを集約する。**整形だけ**を受け持ち、検索エンジン・実在チェック（documents.resolve/world_rel_set）・
redaction/clip・ベクトル方針などの**ポリシー判断は呼び出し側に残す**（Codex 指摘＝統合しすぎない）。

各サイトの形（壊さない差異）:
- evidence.grep（近傍カードの根拠）: `{doc_id, line, span, text, match}`（**path/ext は出さない**）。
- QA/agentic citation: `{doc_id, span, quote, ext}`（QA は `match` 付き・quote=本文、agentic は match 無し・quote=redact 済）。
- ES citation: span=`[line, line]`・`match`=query。

不変条件（SEARCH-CUT-3 RV MED-1）: **citation dict はこのモジュールの関数が返す形のまま公開・保存される**
（`/chat` 応答・SSE・履歴・共有 sanitize・JSON 書き出しへそのまま到達する）。rag_chunks 由来の
`locator`/`chunk_id`（`evidence_ir.Locator`）を citation dict に**キーとして持たせない**——出典フッター
（docs/04 §4）に内部表現（cell_range 等）を出さない契約と、画面/公開 payload 不変の契約の両方を守るため。
locator を回答生成の追加材料として使いたい呼び出し側は、citation を作る前の生ヒット（`locator_hint` の
引数）から直接ヒントを組み、citation とは別の使い捨てコピー（例: agentic のツール結果テキスト）にだけ
混ぜる（`agentic_search.py` の `es_search` 実装を参照）。

**H3（SC-4 接続・CITE-1）で追加した加算的フィールド**: `excerpt_source`（`"human_md"|"rag"`）・
`locator_hint`（`locator_hint()` が返す整形済み文字列そのもの・生の `locator` オブジェクトではない）・
`tier`（`"full"|"region"|"chunk"`・非agentic 親返し＝`rag_parent_return` が縮退した段）。いずれも
`with_display_excerpt`（本モジュール）が付与する——`quote` の値は「利用者向けに引き直した本文」
（人間向け MD の該当節、または元の rag/legacy 本文のフォールバック）に**上書き**される点に注意
（rag_chunks 由来の生 locator はキーとして持たせない不変条件は変わらない・`excerpts.py` 参照）。
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping


def public_grep_hit(h: Mapping) -> dict:
    """grep ヒット → API evidence.grep 用（doc_id/line/span/text/match）。物理 path・ext は出さない。"""
    return {"doc_id": h["doc_id"], "line": h["line"], "span": h["span"],
            "text": h["text"], "match": h["match"]}


def from_grep_hit(h: Mapping, *, quote: str | None = None, include_match: bool = True) -> dict:
    """grep ヒット → citation（QA/agentic 共用）。`quote` 未指定なら本文 `h["text"]`。ext は維持。

    redaction/clip は**呼び出し側で `quote` を作って渡す**（ポリシーは helper に入れない）。
    agentic は `include_match=False`（citation に match を付けない既存仕様）。
    """
    c = {"doc_id": h["doc_id"], "span": h.get("span"),
         "quote": h.get("text", "") if quote is None else quote, "ext": h.get("ext")}
    if include_match:
        c["match"] = h.get("match")
    return c


def from_es_hit(h: Mapping, query: str, *, quote: str | None = None, include_match: bool = True) -> dict:
    """ES ヒット → citation。span は `[line, line]`、`match`=query（include_match 時）。実在チェックは呼び出し側。

    `locator`/`chunk_id` は**付けない**（モジュール不変条件・SEARCH-CUT-3 RV MED-1）。
    """
    c = {"doc_id": h["doc_id"], "span": [h.get("line"), h.get("line")],
         "quote": h.get("text", "") if quote is None else quote, "ext": h.get("ext")}
    if include_match:
        c["match"] = query
    return c


_LOCATOR_FIELD_MAX = 40      # locator の1フィールド（シート名/セル範囲）の上限文字数
_LOCATOR_HINT_MAX = 60       # 位置ヒント全体の上限文字数
_LOCATOR_NUMBER_MAX = 999_999   # page/slide の桁上限（6桁）。無制限だと巨大整数で全体上限を突破しうる
_LOCATOR_WS_RE = re.compile(r"\s+")   # str.splitlines() が改行とみなす全種（U+2028/2029・NEL 等）を含む
                                       # Unicode 空白class＝\s で丸ごと正規化する（SEARCH-CUT-3 RV MED-3 追加是正）


def _clean_locator_field(value, limit: int = _LOCATOR_FIELD_MAX) -> str | None:
    """locator の1フィールドを検証（SEARCH-CUT-3 RV MED-3）: 文字列型のみ・改行/空白類を単一空白へ正規化
    （`\\n`/`\\r` だけでなく `str.splitlines()` が認識する行区切り全種を含む）・引用符エスケープ・長さ上限。
    数値やその他の型・空文字・上限超は None（不正値は素通りさせず locator_hint 側で捨てる）。
    """
    if not isinstance(value, str):
        return None
    v = _LOCATOR_WS_RE.sub(" ", value).replace("「", "『").replace("」", "』").strip()
    return v if v and len(v) <= limit else None


def _clean_locator_number(value) -> int | None:
    """locator の page/slide を検証（SEARCH-CUT-3 RV MED-3 追加是正）: 正の非 bool int のみ・6桁上限。
    上限が無いと巨大整数がそのまま `f"p.{page}"` に展開され、位置ヒント全体の長さ上限を突破しうる。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 < value <= _LOCATOR_NUMBER_MAX else None


def locator_hint(locator: Mapping | None) -> str:
    """rag_chunks の `locator`（evidence_ir.Locator）を短い日本語の位置ヒントに整形。

    回答生成時に LLM へ渡す追加コンテキスト専用（画面の出典フッターには出さない・docs/04 契約は不変）。
    既知の組み合わせ（シート+セル範囲／ページ／スライド）以外は空文字（黙って壊れない）。
    型検証・改行/空白正規化・引用符エスケープ・長さ上限（フィールド単位・全体単位の両方）を通した
    値だけを使う（SEARCH-CUT-3 RV MED-3・呼び出し側の redaction/上限を迂回させない＝ここで先に
    安全側にしておく）。
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
    """H3（SC-4 接続・CITE-1）: citation の `quote` を利用者向けに引き直した本文へ加算的に差し替える。

    `excerpts.display_quote`（本文解決・ファイル読み取りはそちら側の責務）の結果をそのまま渡す想定
    （本関数自体は整形だけ・モジュールの既存契約と同じ）。`excerpt_source`（`"human_md"`＝人間向け
    MD の該当節に引き直せた／`"rag"`＝対応が取れず元の rag/legacy 本文のまま）は常に付与する。
    `locator_hint`/`tier` は値があるときだけキーを立てる（既存の「無ければキーを立てない」慣行）。
    非agentic 経路（`chat_service`/`lens_service`）専用——agentic の citation 生成
    （`agentic_search.py`）はこの関数を使わず、既存どおり `locator`/`chunk_id` を citation に
    キーとして持たせない。
    """
    out = {**citation, "quote": quote, "excerpt_source": excerpt_source}
    if locator_hint:
        out["locator_hint"] = locator_hint
    if tier:
        out["tier"] = tier
    return out


def with_display_text(evidence: Mapping, *, text: str, excerpt_source: str,
                      locator_hint: str | None = None) -> dict:
    """`with_display_excerpt` の evidence.grep 形（`{doc_id, line, span, text, match}`・`quote` では
    なく `text` キーを使う）向け。`public_grep_hit`／troubleshoot の evidence.grep で使う（本モジュールが
    引き続き dict 形状の単一の真実源であり続けるための対）。"""
    out = {**evidence, "text": text, "excerpt_source": excerpt_source}
    if locator_hint:
        out["locator_hint"] = locator_hint
    return out


def citation_dedupe_key(c: Mapping) -> tuple:
    """citation の重複排除鍵。既定 `(doc_id, span)` だが、span が行番号を持たない
    （rag_chunks 由来＝`[None, None]`）ときは `quote` 本文も鍵に加える（SEARCH-CUT-3 RV MED-2）。
    span だけで鍵にすると、同一文書内の複数セル/ページのヒットが**すべて同じ鍵**になり、1件目以外が
    黙って消えてしまう（citation は既存フィールドのみで組む＝locator/chunk_id を新たに持ち込まない）。
    span に実値があるとき（grep・legacy ES）は従来どおり `(doc_id, span)` のみ＝挙動不変。

    `doc_id` の有無は呼び出し側が判定する（本関数は鍵を返すだけ）。

    `dedupe_round_robin_by_doc_span`（本モジュール）と `providers/base.py` の agentic citation 集約
    3箇所（sub loop 集約・plan 集約・単一ループ集約）が**共通で使う**鍵規則
    （SEARCH-CUT-3 RV: 同じ鍵ロジックを重複実装しない）。
    """
    span = tuple(c.get("span") or ())
    return (c.get("doc_id"), span) if any(span) else (c.get("doc_id"), span, c.get("quote"))


def merge_overlapping_citations(citations: list, evidence_meta: list) -> tuple[list, list, list]:
    """同一 doc_id・行範囲（span）が重なる/包含する citation を1件に統合する（`citation_dedupe_key`
    による完全一致の重複排除の直後に適用・呼び出し元＝`providers/base.py::_dedupe_citations_and_evidence`）。

    実 grep/es_search は同じ箇所を異なる語で複数回ヒットさせうるため、行1-3・行1-5・行2-6 のように
    互いに重なる/包含する citation が並ぶことがある——中身は実質同じ根拠なのに件数だけ水増しされ、
    出典の「根拠（精読済み）」に同一趣旨の文書が何件も並んでしまう契約違反を招く。同一
    doc_id 内で span（`[start, end]`・両端とも整数の行範囲）が重なる（区間が1行でも共有する）
    citation を1グループにまとめ、範囲の和集合 `[min(start), max(end)]` を新しい span として1件に
    統合する。quote 等の非 span フィールドは、元の中で最も広い（`end - start` が最大の）citation の
    ものを引き継ぐ（同点は出現順で先勝ち・新しい範囲の本文を再取得はしない）。**evidence_meta 側の
    `span` も同じ和集合へ同期する**——citation.span だけを更新して evidence_meta.span を古いまま
    放置すると、Evidence Packet の `source_span` と `data.citations` の表示 span が食い違う
    。span を持たない（None 等・行番号を持たない rag_chunks 由来等）citation は
    対象外（そのまま素通し）。隣接するが1行も共有しない span（例 `[1,3]`/`[4,5]`）は統合しない
    （区間の共有が無い＝別件のまま）。`start > end`（逆転した不正な span）の citation も対象外
    （`_span_range` が None を返す）。`citations`/`evidence_meta` は同じ index で対になっている
    契約——統合後も1対1のまま返す（呼び出し元が Evidence Packet の evidence_id を1件だけ割り当て
    られるようにする）。3件目の戻り値 `merged_flags` は各出力エントリが実際に複数件から統合された
    かどうかの bool list——呼び出し元（`providers/base.py`）が統合後の span を再検証するかどうかの
    判定に使う（統合されていないエントリは `_commit_evidence` で既に検証済みのため再検証不要）。
    """
    def _span_range(c):
        span = c.get("span")
        if not (isinstance(span, (list, tuple)) and len(span) == 2):
            return None
        a, b = span
        if isinstance(a, bool) or isinstance(b, bool) or not isinstance(a, int) or not isinstance(b, int):
            return None
        if a > b:
            return None   # 逆転した不正な span は対象外（正規化して救わない）
        return (a, b)

    by_doc: dict = {}
    for i, c in enumerate(citations):
        rng = _span_range(c)
        doc_id = c.get("doc_id")
        if rng is None or not doc_id:
            continue
        by_doc.setdefault(doc_id, []).append((i, rng))

    group_of: dict = {}   # 元 index -> 同一グループに属する [(index, (start, end)), ...]
    for _doc_id, items in by_doc.items():
        items_sorted = sorted(items, key=lambda t: t[1])
        groups: list = []
        cur: list = []
        cur_end = None
        for i, (s, e) in items_sorted:
            if cur and s <= cur_end:
                cur.append((i, (s, e)))
                cur_end = max(cur_end, e)
            else:
                if cur:
                    groups.append(cur)
                cur, cur_end = [(i, (s, e))], e
        if cur:
            groups.append(cur)
        for g in groups:
            for i, _rng in g:
                group_of[i] = g

    out_c, out_m, merged_flags = [], [], []
    consumed: set = set()
    for i, c in enumerate(citations):
        if i in consumed:
            continue
        g = group_of.get(i)
        if g is None:
            out_c.append(c)
            out_m.append(evidence_meta[i] if i < len(evidence_meta) else {})
            merged_flags.append(False)
            continue
        for j, _rng in g:
            consumed.add(j)
        if len(g) == 1:
            out_c.append(c)
            out_m.append(evidence_meta[i] if i < len(evidence_meta) else {})
            merged_flags.append(False)
            continue
        min_s = min(s for _j, (s, _e) in g)
        max_e = max(e for _j, (_s, e) in g)
        rep_i, _rep_rng = max(g, key=lambda t: (t[1][1] - t[1][0], -t[0]))
        merged_span = [min_s, max_e]
        merged_c = dict(citations[rep_i])
        merged_c["span"] = merged_span
        merged_m = dict(evidence_meta[rep_i]) if rep_i < len(evidence_meta) else {}
        merged_m["span"] = merged_span   # evidence_meta 側の span も和集合へ同期する
        out_c.append(merged_c)
        out_m.append(merged_m)
        merged_flags.append(True)
    return out_c, out_m, merged_flags


def build_evidence_packet(*, task_id: str, investigation_status: str, summary: str = "",
                          claims: list | None = None, evidence: list[dict] | None = None,
                          remaining_gaps: list | None = None, conflicts: list | None = None,
                          candidates_seen: int = 0, candidates_inspected: int = 0,
                          evidence_selected: int | None = None, stop_reason: str = "",
                          next_action: str = "") -> dict:
    """Evidence Packet（EXT-2・拡張設計 §4.2・原文§8のフィールドをそのまま採用）。

    **整形だけ**を行う（本モジュールの既存契約どおり・値の意味判断＝何を Candidate/Verified/Committed
    とするかは呼び出し側の責務のまま）。`evidence` は Committed Evidence（機械検証・§4.3 を通過した
    citation）1件ごとに `{evidence_id, source_type, source_path, source_span, verification_method}`
    を持つ list（呼び出し側＝`providers/base.py` が組む）。`next_action`（EXT-3・§3.2 評価フェーズの
    `submit_evaluation` が返す語彙＝`commit_evidence`/`continue_search`/`read_more`/`delegate_more`/
    `stop`）は評価が行われなかった経路では空文字のまま。
    """
    evidence = evidence or []
    return {
        "task_id": task_id,
        "investigation_status": investigation_status,
        "summary": summary,
        "claims": claims or [],
        "evidence": evidence,
        "remaining_gaps": remaining_gaps or [],
        "conflicts": conflicts or [],
        "candidates_seen": candidates_seen,
        "candidates_inspected": candidates_inspected,
        "evidence_selected": evidence_selected if evidence_selected is not None else len(evidence),
        "stop_reason": stop_reason,
        "next_action": next_action,
    }


def dedupe_round_robin_by_doc_span(*groups: Iterable[Mapping]) -> list:
    """複数 citation 群を**渡した順に round-robin** で並べ、`citation_dedupe_key` で重複排除。

    `_merge_qa_with_es`（grep↔ES を交互に並べ先頭付近に ES も載せる）の一般化。doc_id 無しは捨てる。
    """
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
