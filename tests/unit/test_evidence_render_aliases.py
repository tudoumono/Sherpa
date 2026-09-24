"""pptx の図形束ね（alias）が引用を落とさないこと（実測バグの回帰ガード・2026-08-15）。

MD化強化の移植時、実コーパスの pptx 4件が RAG 表現を作れず「失敗の記録」へ縮退していた。
原因は `_pptx_object_aliases` が **出力されない要素を束ね先に選ぶ**こと:

  - 入れ子の `group`（`_CONTAINER_TYPES`＝レコード化されない）
  - **空セル**（表レコードに出力されない）

束ね先が出力されないと、そこへ束ねた図形の引用がどこにも現れず、
`validation_errors` の `element_coverage_missing` で文書全体が失敗する。

期待する挙動: 束ね先は「本文を持ち、実際に出力される要素」に限る。該当が無ければ**束ねない**
（元の図形をそのまま残す＝情報を落とさない）。
"""
from __future__ import annotations

import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

from sherpa.ingest import evidence_ir as IR  # noqa: E402
from sherpa.ingest import evidence_render as R  # noqa: E402


def _loc(slide: int, **ext) -> IR.Locator:
    return IR.Locator(part=f"ppt/slides/slide{slide}.xml", slide=slide, extension=ext)


def _el(eid: str, etype: str, *, parent: str | None, order: int, value, slide: int = 1,
        **extension) -> IR.EvidenceElement:
    return IR.EvidenceElement(
        element_id=eid, type=etype, parent_id=parent, order=order, value=value,
        locator=_loc(slide), coverage_id=f"cov:{eid}", extension=extension)


def _ir(elements: list[IR.EvidenceElement]) -> IR.EvidenceIR:
    return IR.EvidenceIR(
        schema_version=IR.EVIDENCE_IR_SCHEMA_VERSION,
        parser_profile="test",
        source=IR.EvidenceSource(file_type="pptx", content_hash="sha256:" + "0" * 64),
        elements=elements,
    )


def _aliases(elements: list[IR.EvidenceElement]):
    by_id = {e.element_id: e for e in elements}
    return R._element_aliases(_ir(elements), by_id)


def _adapted(eid: str, *, z_index: int, order: int, value: str) -> IR.EvidenceElement:
    """document-ir 由来の shape（束ねられる側）。"""
    el = _el(eid, "shape", parent="slide1", order=order, value=value,
             origin="document-ir-v2-adapter")
    return IR.EvidenceElement(
        element_id=el.element_id, type=el.type, parent_id=el.parent_id, order=el.order,
        value=el.value, coverage_id=el.coverage_id, extension=el.extension,
        locator=IR.Locator(part=el.locator.part, slide=1,
                           extension={"document_ir_source_map": {"z_index": z_index}}))


def test_empty_group_target_is_not_suppressed():
    """束ね先が本文なしの group だけ＝束ねない（元の図形が残る）。"""
    elements = [
        _el("slide1", "slide", parent=None, order=0, value=None),
        _el("grp", "group", parent="slide1", order=1, value=None, z_order=1),
        _el("deco", "picture", parent="grp", order=2, value=None),      # 本文なし＝候補にならない
        _adapted("shape1", z_index=1, order=3, value="Fully editable icons"),
    ]
    aliases, suppressed = _aliases(elements)
    assert suppressed == set(), "出力されない group へ束ねてしまっている"
    assert aliases == {}


def test_empty_cell_target_is_not_suppressed():
    """束ね先が空セル＝束ねない（空セルは表レコードに出ないため引用が消える）。"""
    elements = [
        _el("slide1", "slide", parent=None, order=0, value=None),
        _el("tbl", "table", parent="slide1", order=1, value=None, z_order=1),
        _el("cell0", "cell", parent="tbl", order=2, value=""),          # 空セル
        _adapted("shape1", z_index=1, order=3, value="Met benchmarks"),
    ]
    aliases, suppressed = _aliases(elements)
    assert suppressed == set()
    assert aliases == {}


def test_text_bearing_cell_is_used_as_target():
    """本文のあるセルは従来どおり束ね先になる（重複表示の抑止は維持）。"""
    elements = [
        _el("slide1", "slide", parent=None, order=0, value=None),
        _el("tbl", "table", parent="slide1", order=1, value=None, z_order=1),
        _el("cell0", "cell", parent="tbl", order=2, value="実データ"),
        _adapted("shape1", z_index=1, order=3, value="実データ"),
    ]
    aliases, suppressed = _aliases(elements)
    assert suppressed == {"shape1"}
    assert list(aliases) == ["cell0"]


def test_nested_group_is_skipped_in_favor_of_text_child():
    """候補に入れ子 group と本文つき図形が混在したら、本文つきを選ぶ。"""
    elements = [
        _el("slide1", "slide", parent=None, order=0, value=None),
        _el("grp", "group", parent="slide1", order=1, value=None, z_order=1),
        _el("inner", "group", parent="grp", order=2, value="見出しらしき値"),   # container＝出力されない
        _el("text", "shape", parent="grp", order=3, value="本文"),
        _adapted("shape1", z_index=1, order=4, value="本文"),
    ]
    aliases, suppressed = _aliases(elements)
    assert suppressed == {"shape1"}
    assert list(aliases) == ["text"], "入れ子 group を束ね先に選んでいる"
