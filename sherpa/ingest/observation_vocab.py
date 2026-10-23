"""Office 観測の語彙（関係の extension・塗りの区分・遮蔽の確からしさ・状態の由来）と、状態・関係・未解析の通知の文言の対応表。

RAG の行と人用 Markdown の行は、この対応表から同じ語を引く。判定そのものは持たない（定数・対応表・純関数だけ）。
設計: docs/proposals/2026-10-22-Office観測の共通の扱い.md §1
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, NamedTuple

# ---- 関係 ----
RELATION_COVERS = "covers"

# 関係 extension.basis: どの観測から作った関係か
BASIS_OOXML_CONNECTOR = "ooxml_connector"
BASIS_OOXML_ANCHOR = "ooxml_anchor"
BASIS_GEOMETRY = "geometry"

# 関係 extension.direction: 方向をどこまで確かめたか
DIRECTION_ARROW_CONFIRMED = "arrow_confirmed"
DIRECTION_ENDPOINT_ORDER_ONLY = "endpoint_order_only"
DIRECTION_UNDIRECTED = "undirected"
DIRECTION_UNKNOWN = "unknown"

# 関係 extension.ends: 両端を原本のオブジェクトに解決できたか
ENDS_BOTH = "both"
ENDS_SOURCE_ONLY = "source_only"
ENDS_TARGET_ONLY = "target_only"

# ---- 塗り（covers.extension.fill）と遮蔽の確からしさ（covers.extension.occlusion） ----
FILL_OPAQUE = "opaque"
FILL_TRANSLUCENT = "translucent"
FILL_TRANSPARENT = "transparent"
FILL_NONE_WITH_TEXT = "none_with_text"
FILL_TRANSPARENT_PNG = "transparent_png"
FILL_UNKNOWN = "unknown"

OCCLUSION_CONFIRMED = "confirmed"
OCCLUSION_UNDETERMINED = "undetermined"
OCCLUSION_NOT_OCCLUDING = "not_occluding"

# ---- 状態の由来（extension.state_basis） ----
STATE_BASIS_OOXML_DIRECT = "ooxml_direct"
STATE_BASIS_GEOMETRY = "geometry"

# ---- 要素全体の書式の状態（extension.format_state） ----
FORMAT_STRIKE = "strike"
FORMAT_WHOLE = "whole"


def occlusion_of(fill: str, covers_bbox: bool = True) -> str:
    """塗りの区分から遮蔽の確からしさを返す。`confirmed` は呼び出し側が覆う割合の閾値を満たしたときだけ使う。

    透過を含む画像は画素ごとに見ないと覆っているか確かめられないので、未確定（undetermined）にする。
    不透明な塗りでも、形が外接矩形と同一視できない図形（`drawing_fill.covers_bounding_box` が False）は、
    外接矩形の重なりで背面を覆っているとは言えないので未確定にする。
    """
    if fill == FILL_OPAQUE:
        return OCCLUSION_CONFIRMED if covers_bbox else OCCLUSION_UNDETERMINED
    if fill in (FILL_UNKNOWN, FILL_TRANSPARENT_PNG):
        return OCCLUSION_UNDETERMINED
    return OCCLUSION_NOT_OCCLUDING


def relation_extension(basis: str, direction: str, ends: str = ENDS_BOTH, **extra: Any) -> dict[str, Any]:
    """関係の extension の共通部分（basis・direction・ends）に個別のキーを足した dict。"""
    return {"basis": basis, "direction": direction, "ends": ends, **extra}


# ---- 可視性の理由 ----
# 遮蔽（covers から導く理由）。pptx は図形・画像を区別しない語、xlsx は区別する語。
REASON_OCCLUDED = "occluded"
REASON_OCCLUDED_BY_PICTURE = "occluded_by_picture"
REASON_OCCLUDED_BY_SHAPE = "occluded_by_shape"
COVER_DERIVED_REASONS = frozenset({REASON_OCCLUDED, REASON_OCCLUDED_BY_PICTURE, REASON_OCCLUDED_BY_SHAPE})

# 遮蔽より優先して保つ理由（スライドの非表示を引き継いだ要素は、覆われても理由を変えない）
REASONS_ABOVE_COVERS = frozenset({"hidden_slide_inherited"})

# 状態の由来が幾何の判定になる理由。ここに無い既知の理由は原本の直接の指定。
GEOMETRY_REASONS = COVER_DERIVED_REASONS | {"off_slide"}
OOXML_DIRECT_REASONS = frozenset({
    "hidden_sheet", "very_hidden", "hidden_row", "hidden_column", "hidden_run",
    "hidden_slide", "hidden_slide_inherited",
})


def occlusion_reason(file_type: str, front_type: str) -> str:
    """前面の種別から、背面の `visibility_reason` に入れる遮蔽の理由を返す。"""
    if file_type == "pptx":
        return REASON_OCCLUDED
    return REASON_OCCLUDED_BY_PICTURE if front_type == "picture" else REASON_OCCLUDED_BY_SHAPE


def state_basis_for(visibility_reason: Any) -> str | None:
    """`visibility_reason` の由来（幾何の判定か原本の直接の指定か）。未知の理由は None。"""
    if visibility_reason in GEOMETRY_REASONS:
        return STATE_BASIS_GEOMETRY
    if visibility_reason in OOXML_DIRECT_REASONS:
        return STATE_BASIS_OOXML_DIRECT
    return None


def is_whole_strike(extension: dict[str, Any]) -> bool:
    """要素全体の取り消し線か（`format_state.strike == "whole"`）。"""
    format_state = extension.get("format_state")
    return isinstance(format_state, dict) and format_state.get(FORMAT_STRIKE) == FORMAT_WHOLE


def whole_strike_state() -> dict[str, str]:
    return {FORMAT_STRIKE: FORMAT_WHOLE}


# ---- 文言の対応表 ----
# `visibility_reason` → 平文ラベル。ここに無い理由（None・未知値）は生の `visibility`/`lifecycle` 値に譲る。
VISIBILITY_REASON_LABELS: dict[str, str] = {
    "occluded_by_picture": "画像に覆われている",
    "occluded_by_shape": "図形に覆われている",
    "occluded": "図形に覆われている",
    "hidden_sheet": "シートが非表示",
    "very_hidden": "シートが完全非表示",
    "hidden_row": "行が非表示",
    "hidden_column": "列が非表示",
    "hidden_run": "非表示文字",
    "hidden_slide": "スライドが非表示",
    "hidden_slide_inherited": "非表示スライド内の要素",
    "off_slide": "スライド範囲外",
}

# 状態・関係・未解析の通知の文。`say(キー, 値…)` で引く。
WORDING: dict[str, str] = {
    "visibility": "可視性: 「{label}」",
    "lifecycle": "状態: 「{value}」",
    "strike_whole": "取り消し線: 「あり」",
    "overlap_text": "重なり: 「{value}」",
    "anchor_front": "前面図形: 「{value}」",
    "anchor_behind": "背面図形: 「{value}」",
    "state_lines_omitted": "（ほかに重なり・前面/背面図形の行が {count} 件）",
    "sheet_visibility": "シートの可視性: 「{label}」",
    "self_ref": "この要素: {ref}",
    "relation_overlaps_hidden_by_alpha": "この要素は対象「{target}」の原本領域に重なっている",
    "relation_overlaps": "この要素は別要素の原本領域に重なっている",
    "relation_connects": "この要素は対象「{target}」へ接続している",
    "relation_covered_by": "このtext layerは後描画の全面画像に覆われており、現行回答に使用しない",
    "relation_other": "この要素は対象要素と「{type}」の関係にある",
    "picture_uninterpreted": "{context}に画像「{alt}」が存在する。画像内容は未解釈である。",
    "cover_front_confirmed": "覆っている対象: {refs}（覆われていると判定）",
    "cover_front_undetermined": "重なっている対象: {refs}（覆っているかは未確定）",
    "cover_front_not_occluding": "重なっている対象: {refs}（覆ってはいない：{fill}）",
    "cover_back_confirmed": "前面図形: {ref}（覆われていると判定）",
    "cover_back_undetermined": "前面図形: {ref}（覆われているかは未確定）",
    "cover_back_not_occluding": "前面図形: {ref}（覆われてはいない：{fill}）",
    "connect_start": "接続先: {refs}（始点→終点の指定。矢印の向きは未確認）",
    "connect_end": "接続元: {ref}（始点→終点の指定。矢印の向きは未確認）",
    "connect_connector": "接続: {start} → {end}（始点→終点の指定。矢印の向きは未確認）",
    "connect_start_unknown": "接続先: 不明（終点の図形を原本から特定できない）",
    "connect_end_unknown": "接続元: 不明（始点の図形を原本から特定できない）",
    "relation_more": "ほか {count} 件",
    "notice_picture": "画像「{name}」の内容は未解釈である",
    "notice_ole": "埋め込み（OLE）「{name}」の中身は未解析である",
    "notice_unsupported": "{kind}「{name}」は未対応で、内容を読めていない",
    "notice_failed": "{kind}「{name}」は読み取りに失敗した",
}

# 塗りの区分 → 覆っていない理由の平文（`cover_*_not_occluding` の {fill}）
FILL_LABELS: dict[str, str] = {
    FILL_OPAQUE: "不透明",
    FILL_TRANSLUCENT: "半透明",
    FILL_TRANSPARENT: "透明",
    FILL_NONE_WITH_TEXT: "塗りなしで文字だけ",
    FILL_TRANSPARENT_PNG: "透過を含む画像",
    FILL_UNKNOWN: "塗りを判定できない",
}

# coverage の `detected_kind` → 未対応・失敗の通知に書く部品の名前
NOTICE_KIND_LABELS: dict[str, str] = {
    "chart_unparsed_attributes": "グラフ",
    "smartart_semantic_graph": "SmartArt",
    "smartart_data": "SmartArt",
    "chart_data": "グラフ",
    "vml_unparsed_geometry": "図形(VML)",
    "drawing_image_asset": "画像",
    "drawing_external_image_reference": "外部の画像",
}
NOTICE_KIND_DEFAULT = "部品"


# 人用 Markdown の文言。RAG の行（`WORDING`）と同じ観測の鍵で引き、観測を表す語（覆われている／覆っている・重なっている・
# 取り消し線・非表示・未確定・判定・未解析／読み取っていない・未対応）を揃える。内部の語（前面図形・可視性など）は使わない。
# `{subject}` は「この図形」「セル A2」など、注記の主語。
HUMAN_WORDING: dict[str, str] = {
    "strike_whole": "{subject}の文字には取り消し線が引かれています",
    "cover_front_confirmed": "{subject}は{refs}を覆っています（覆われていると判定）",
    "cover_front_undetermined": "{subject}は{refs}と重なっています（覆っているかは未確定）",
    "cover_front_not_occluding": "{subject}は{refs}と重なっています（覆ってはいない：{fill}）",
    "cover_back_confirmed": "{subject}は{ref}に覆われています（覆われていると判定）",
    "cover_back_undetermined": "{subject}の上に{ref}が重なっています（覆われているかは未確定）",
    "cover_back_not_occluding": "{subject}の上に{ref}が重なっています（覆われてはいない：{fill}）",
    "connect_start": "{subject}は{refs}に接続されています（始点→終点の指定。矢印の向きは未確認）",
    "connect_end": "{subject}には{ref}から接続されています（始点→終点の指定。矢印の向きは未確認）",
    "connect_connector": "{subject}は{start}と{end}を接続しています（始点→終点の指定。矢印の向きは未確認）",
    "connect_start_unknown": "{subject}の接続先は不明です（終点の図形を特定できません）",
    "connect_end_unknown": "{subject}の接続元は不明です（始点の図形を特定できません）",
    "relation_more": "ほか {count} 件あります",
    "notice_picture": "この画像「{name}」の内容は読み取っていません",
    "notice_ole": "この埋め込み（OLE）「{name}」の中身は読み取っていません",
    "notice_unsupported": "この{kind}「{name}」は未対応で、内容を読めていません",
    "notice_failed": "この{kind}「{name}」は読み取りに失敗しました",
}

# `visibility_reason` → 人用 Markdown の可視性の文（語は `VISIBILITY_REASON_LABELS` と同じ）
HUMAN_VISIBILITY_SENTENCES: dict[str, str] = {
    "occluded_by_picture": "{subject}は画像に覆われています",
    "occluded_by_shape": "{subject}は図形に覆われています",
    "occluded": "{subject}は図形に覆われています",
    "hidden_sheet": "{subject}は非表示のシートにあります",
    "very_hidden": "{subject}は完全に非表示のシートにあります",
    "hidden_row": "{subject}は非表示の行にあります",
    "hidden_column": "{subject}は非表示の列にあります",
    "hidden_run": "{subject}は非表示の文字です",
    "hidden_slide": "{subject}は非表示です",
    "hidden_slide_inherited": "{subject}は非表示のスライドにあります",
    "off_slide": "{subject}はスライドの範囲外にあります",
}


_LINE_BREAKS = re.compile(r"\s*[\r\n\u2028\u2029\x0b\x0c\x85]+\s*")


def say(key: str, human: bool = False, **values: Any) -> str:
    """文言の対応表から文を作る。`human=True` は人用 Markdown の文（同じ鍵）。
    文に埋め込む名前・引用の改行は空白にする（1 つの観測を必ず 1 行にする。対象自身の本文の改行は変えない）。
    """
    values = {k: _LINE_BREAKS.sub(" ", v) if isinstance(v, str) else v for k, v in values.items()}
    return (HUMAN_WORDING if human else WORDING)[key].format(**values)


def visibility_line(reason: str, label: str, human: bool = False, subject: str = "") -> str:
    """可視性の行。RAG は「可視性: 「…」」、人用 Markdown は主語つきの平文（`reason` は `VISIBILITY_REASON_LABELS` にある理由）。"""
    if human:
        return HUMAN_VISIBILITY_SENTENCES[reason].format(subject=subject)
    return say("visibility", label=label)


def strike_line(human: bool = False, subject: str = "") -> str:
    return say("strike_whole", human=True, subject=subject) if human else say("strike_whole")


# 原本の図形に解決できなかったコネクタの端の参照
UNRESOLVED_REF = "不明の図形"

# 前面（関係の出ていく側）の行に並べる対象の最大件数。超えた分は「ほか K 件」の 1 行にまとめる
MAX_FRONT_ITEMS = 10


class FrontEntry(NamedTuple):
    """前面側（`covers` の前面・`connects_to` の始点）の対象 1 件。同じ種類の対象は 1 行にまとめて並べる。"""
    rank: int          # 並べる順（covers の confirmed → undetermined → not_occluding → 接続）
    key: str           # 文言の対応表のキー（`{refs}` を持つもの）
    fill: str          # `{fill}` に入れる平文（使わない文では空）
    ref: str           # 対象の参照（名前・ID・位置。本文は引用しない）
    subject: str = ""  # 人用 Markdown の主語


def _level(occlusion: Any) -> str:
    return occlusion if occlusion in (OCCLUSION_CONFIRMED, OCCLUSION_UNDETERMINED) else OCCLUSION_NOT_OCCLUDING


def cover_back_line(occlusion: Any, fill: Any, ref: str, human: bool = False, subject: str = "") -> str:
    """`covers` の背面側の行（前面の参照を 1 行）。`confirmed` 以外は「覆われていると判定」と書かない。"""
    return say(f"cover_back_{_level(occlusion)}", human=human, ref=ref, subject=subject,
               fill=FILL_LABELS.get(fill, FILL_LABELS[FILL_UNKNOWN]))


def _cover_front_entry(occlusion: Any, fill: Any, ref: str, subject: str) -> FrontEntry:
    level = _level(occlusion)
    rank = {OCCLUSION_CONFIRMED: 0, OCCLUSION_UNDETERMINED: 1, OCCLUSION_NOT_OCCLUDING: 2}[level]
    label = FILL_LABELS.get(fill, FILL_LABELS[FILL_UNKNOWN]) if level == OCCLUSION_NOT_OCCLUDING else ""
    return FrontEntry(rank, f"cover_front_{level}", label, ref, subject)


def relation_note_lines(
    relations: Any,
    ref: Callable[[str], str | None],
    human: bool = False,
    subject_of: Callable[[str], str] | None = None,
) -> dict[str, list[str | FrontEntry]]:
    """関係（Evidence の `covers`・`connects_to`）を、両端の要素それぞれの行の元にする（要素 ID → 行・前面側の対象）。

    関係は Evidence に 1 本のまま、描画だけが両側へ書く。`ref` は要素 ID → 人に見せる参照（名前・ID・位置）で、
    `None` を返した端の関係は書かない。文にするのは `relation_block`。`human=True` は人用 Markdown の文で、
    `subject_of` は要素 ID → 主語。
    `covers` は幾何の判定（`basis=geometry`）のものだけ。`connects_to` は始点・終点の指定だけを読んだもの
    （`direction=endpoint_order_only`・両端が解決済み）だけを、始点・終点・コネクタ自身の 3 つへ書く。
    """
    out: dict[str, list[str | FrontEntry]] = {}

    def subject(element_id: str) -> str:
        return subject_of(element_id) if subject_of is not None else ""

    def add(element_id: str | None, entry: str | FrontEntry) -> None:
        if element_id:
            entries = out.setdefault(element_id, [])
            if entry not in entries:
                entries.append(entry)

    for relation in relations:
        extension = relation.extension if isinstance(relation.extension, dict) else {}
        if relation.type == RELATION_COVERS and extension.get("basis") == BASIS_GEOMETRY:
            front, back = ref(relation.source_id), ref(relation.target_id)
            if front is None or back is None:
                continue
            occlusion, fill = extension.get("occlusion"), extension.get("fill")
            add(relation.source_id, _cover_front_entry(occlusion, fill, back, subject(relation.source_id)))
            add(relation.target_id, cover_back_line(occlusion, fill, front, human, subject(relation.target_id)))
        elif relation.type == "connects_to" and extension.get("direction") == DIRECTION_ENDPOINT_ORDER_ONLY:
            ends = extension.get("ends", ENDS_BOTH)
            connector = extension.get("connector_element_id")
            if ends == ENDS_BOTH:
                start, end = ref(relation.source_id), ref(relation.target_id)
                if start is None or end is None:
                    continue
                add(relation.source_id, FrontEntry(3, "connect_start", "", end, subject(relation.source_id)))
                add(relation.target_id, say("connect_end", human=human, ref=start, subject=subject(relation.target_id)))
                if connector:
                    add(connector, say("connect_connector", human=human, start=start, end=end, subject=subject(connector)))
            elif ends in (ENDS_SOURCE_ONLY, ENDS_TARGET_ONLY):
                # 片端だけ原本の図形に解決できたコネクタ: 解決できた図形 → コネクタの 1 本。もう一方の端は「不明」と書く。
                resolved_id = relation.source_id if ends == ENDS_SOURCE_ONLY else relation.target_id
                resolved = ref(resolved_id)
                if resolved is None:
                    continue
                if ends == ENDS_SOURCE_ONLY:
                    add(resolved_id, say("connect_start_unknown", human=human, subject=subject(resolved_id)))
                    start, end = resolved, UNRESOLVED_REF
                else:
                    add(resolved_id, say("connect_end_unknown", human=human, subject=subject(resolved_id)))
                    start, end = UNRESOLVED_REF, resolved
                if connector:
                    add(connector, say("connect_connector", human=human, start=start, end=end, subject=subject(connector)))
    return out


def _front_lines(items: list[FrontEntry], human: bool) -> list[str]:
    groups: dict[tuple[str, str, str], list[str]] = {}
    for item in items:
        groups.setdefault((item.key, item.fill, item.subject), []).append(item.ref)
    return [say(key, human=human, refs="、".join(refs), fill=fill, subject=subject)
            for (key, fill, subject), refs in groups.items()]


def relation_block(
    entries: list[str | FrontEntry],
    budget: int | None = None,
    max_front: int = MAX_FRONT_ITEMS,
    human: bool = False,
) -> list[str]:
    """関係の行の元を行にする。

    前面側の対象は種類（覆いの確からしさ・塗り）ごとに 1 行へ並べ、`max_front` 件までにして残りは「ほか K 件」の 1 行にまとめる
    （並べる順は confirmed → undetermined → not_occluding → 接続、同じ区分の中は原本の位置の順）。
    背面側・コネクタの行は関係 1 本につき 1 行。`budget`（各行を `"\n" + 行` として足した字数の上限）に収まらない分も
    「ほか K 件」に数える。
    """
    fronts = sorted((e for e in entries if isinstance(e, FrontEntry)), key=lambda e: e.rank)
    singles = [e for e in entries if isinstance(e, str)]
    total = len(fronts) + len(singles)

    def cost(lines: list[str]) -> int:
        return sum(1 + len(line) for line in lines)

    more_reserve = 1 + len(say("relation_more", human=human, count=total)) if total > 0 else 0
    count = min(len(fronts), max_front)
    front_lines = _front_lines(fronts[:count], human)
    if budget is not None:
        while count > 0 and cost(front_lines) + (more_reserve if count < total else 0) > budget:
            count -= 1
            front_lines = _front_lines(fronts[:count], human)
    lines = list(front_lines)
    used = cost(lines)
    shown = count
    for line in singles:
        if budget is not None and used + 1 + len(line) + more_reserve > budget:
            continue
        lines.append(line)
        used += 1 + len(line)
        shown += 1
    if shown < total:
        lines.append(say("relation_more", human=human, count=total - shown))
    return lines


def notice_line(status: str, reason_code: str, detected_kind: str, name: str, human: bool = False) -> str | None:
    """coverage の区分（未解析・未対応・失敗）を通知の 1 文にする。通知の対象外（抽出済みなど）は None。"""
    if status == "metadata_only":
        if reason_code == "image_content_uninterpreted":
            return say("notice_picture", human=human, name=name)
        if reason_code == "ole_binary_metadata_only":
            return say("notice_ole", human=human, name=name)
        return None
    kind = NOTICE_KIND_LABELS.get(detected_kind, NOTICE_KIND_DEFAULT)
    if status == "unsupported":
        return say("notice_unsupported", human=human, kind=kind, name=name)
    if status == "failed":
        return say("notice_failed", human=human, kind=kind, name=name)
    return None
