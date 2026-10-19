"""Canonical Evidence IR の最小共通契約。

原本要素を自然文へ畳み込まず、値・原本位置・親子・関係・coverage を分けて保持する。
XLSX/DOCX/PPTX/PDF で共通の field だけを core に置き、形式固有の anchor・座標系は
``Locator.extension``/``EvidenceElement.extension`` へ名前空間を保って残す。
資料フォルダ名や絶対パスは含めず、同じ原本 bytes と parser profile から同じ byte 列を生成する。
設計: docs/design/rag.md「OCR（非同期・隔離ワーカー）」
"""
from __future__ import annotations

import hashlib
import io
import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from .. import json_io
from .gc_relax import relaxed_gc


EVIDENCE_IR_SCHEMA_VERSION = "evidence-ir-v1alpha2"
EVIDENCE_PARSER_PROFILE = "evidence-spike-v4"
COVERAGE_STATUSES = frozenset({
    "extracted",
    "metadata_only",
    "intentionally_ignored",
    "unsupported",
    "failed",
})
CONTENT_BASES = frozenset({
    "structured",
    "text_layer",
    "pixel_only",
    "binary_only",
    "none",
})

# ``intentionally_ignored`` は検索表現から意図的に外す唯一の coverage 状態。
# 許可する (検知種別, 理由) をスキーマ側でも固定し、内容を持つ part を補助扱いに偽装させない。
INTENTIONALLY_IGNORED_CONTRACT = frozenset({
    ("package_content_types", "package_support_part"),
    ("package_relationships", "package_support_part"),
    ("package_support_part", "package_support_part"),
    ("package_thumbnail", "package_preview_thumbnail"),
    ("custom_xml_part", "empty_office_custom_xml_support"),
})


@dataclass(frozen=True)
class EvidenceSource:
    """パス同一性から分離した原本 content の識別情報。"""

    file_type: str
    content_hash: str


@dataclass(frozen=True)
class Locator:
    """原本へ機械的に戻るための共通 locator と形式別 extension。"""

    part: str
    page: int | None = None
    slide: int | None = None
    sheet: str | None = None
    cell_range: str | None = None
    object_id: str | int | None = None
    bbox: list[int | float] | None = None
    extension: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CoverageItem:
    """検知した part/object/region を黙って落とさないための処理結果。"""

    coverage_id: str
    scope: str
    detected_kind: str
    locator: Locator
    status: str
    content_basis: str
    reason_code: str
    parser_id: str
    detail: dict[str, Any]

    @property
    def reason(self) -> str:
        """理由は reason_code が正。"""
        return self.reason_code


@dataclass(frozen=True)
class EvidenceElement:
    """原本から決定的に得た 1 要素。意味上の断定は relation/view 側で扱う。"""

    element_id: str
    type: str
    parent_id: str | None
    order: int
    value: Any
    locator: Locator
    coverage_id: str
    visibility: str = "visible"
    lifecycle: str = "active"
    extension: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EvidenceRelation:
    """要素間の観測可能な関係。推測した自然文は入れない。"""

    relation_id: str
    type: str
    source_id: str
    target_id: str
    evidence_ids: list[str]
    confidence: float
    extension: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvidenceIR:
    schema_version: str
    parser_profile: str
    source: EvidenceSource
    elements: list[EvidenceElement] = field(default_factory=list)
    relations: list[EvidenceRelation] = field(default_factory=list)
    coverage: list[CoverageItem] = field(default_factory=list)


_FIELD_NAMES: dict[type, tuple[str, ...]] = {}


def _field_names(cls: type) -> tuple[str, ...]:
    names = _FIELD_NAMES.get(cls)
    if names is None:
        names = _FIELD_NAMES[cls] = tuple(f.name for f in fields(cls))
    return names


_LOCATOR_FIELDS = _field_names(Locator)


def validation_errors(ir: EvidenceIR) -> list[str]:
    """参照整合性と coverage 全分類を検査する。"""
    errors: list[str] = []
    if ir.schema_version != EVIDENCE_IR_SCHEMA_VERSION:
        errors.append("schema_version")
    if not ir.parser_profile:
        errors.append("parser_profile")
    if not ir.source.content_hash.startswith("sha256:"):
        errors.append("source.content_hash")

    coverage_ids = [item.coverage_id for item in ir.coverage]
    if len(coverage_ids) != len(set(coverage_ids)):
        errors.append("duplicate_coverage_id")
        statuses_by_id: dict[str, set[str]] = {}
        for item in ir.coverage:
            statuses_by_id.setdefault(item.coverage_id, set()).add(item.status)
        if any(len(statuses) > 1 for statuses in statuses_by_id.values()):
            errors.append("coverage_status_conflict")
    coverage_by_id = {item.coverage_id: item for item in ir.coverage}
    for item in ir.coverage:
        if item.coverage_id != make_coverage_id(item.scope, item.detected_kind, item.locator):
            errors.append(f"coverage_id:{item.coverage_id}")
        if item.status not in COVERAGE_STATUSES:
            errors.append(f"coverage_status:{item.coverage_id}")
        if item.content_basis not in CONTENT_BASES:
            errors.append(f"coverage_content_basis:{item.coverage_id}")
        if not item.detected_kind:
            errors.append(f"coverage_detected_kind:{item.coverage_id}")
        if not item.reason_code:
            errors.append(f"coverage_reason_code:{item.coverage_id}")
        if not item.parser_id:
            errors.append(f"coverage_parser_id:{item.coverage_id}")
        if not isinstance(item.detail, dict):
            errors.append(f"coverage_detail:{item.coverage_id}")
        if not item.locator.part:
            errors.append(f"coverage_locator:{item.coverage_id}")
        if item.status == "intentionally_ignored":
            if item.content_basis != "none":
                errors.append(f"coverage_ignored_basis:{item.coverage_id}")
            if (item.detected_kind, item.reason_code) not in INTENTIONALLY_IGNORED_CONTRACT:
                errors.append(f"coverage_ignored_not_allowlisted:{item.coverage_id}")

    element_ids = [element.element_id for element in ir.elements]
    if len(element_ids) != len(set(element_ids)):
        errors.append("duplicate_element_id")
    element_set = set(element_ids)
    for element in ir.elements:
        if element.parent_id is not None and element.parent_id not in element_set:
            errors.append(f"parent_missing:{element.element_id}")
        if element.coverage_id not in coverage_by_id:
            errors.append(f"coverage_missing:{element.element_id}")
        if not element.locator.part:
            errors.append(f"element_locator:{element.element_id}")

    relation_ids = [relation.relation_id for relation in ir.relations]
    if len(relation_ids) != len(set(relation_ids)):
        errors.append("duplicate_relation_id")
    for relation in ir.relations:
        if relation.source_id not in element_set or relation.target_id not in element_set:
            errors.append(f"relation_endpoint:{relation.relation_id}")
        if not 0.0 <= relation.confidence <= 1.0:
            errors.append(f"relation_confidence:{relation.relation_id}")
        if not relation.evidence_ids or any(evidence_id not in element_set for evidence_id in relation.evidence_ids):
            errors.append(f"relation_evidence:{relation.relation_id}")
    return sorted(set(errors))


def to_json_str(ir: EvidenceIR) -> str:
    """検証済み IR を決定的 JSON へ直列化する。"""
    errors = validation_errors(ir)
    if errors:
        raise ValueError("invalid Evidence IR: " + ",".join(errors))
    return "".join(_json_parts(ir))


_ENCODE = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode
_ARRAY_BATCH = 512


def compact_json(value: Any) -> str:
    """キーを並べ替えた最小の JSON 文字列（ID・ハッシュの素材用）。"""
    return _ENCODE(value)


def locator_payload(locator: Locator) -> dict[str, Any]:
    """`asdict(locator)` と同じキーの dict（値は複製しない。直列化にだけ使う）。"""
    payload = dict(locator.__dict__)
    if len(payload) != len(_LOCATOR_FIELDS):
        payload = {name: getattr(locator, name) for name in _LOCATOR_FIELDS}
    return payload


def make_coverage_id(scope: str, detected_kind: str, locator: Locator) -> str:
    """scope・検知種別・原本位置から coverage ID を決定生成する。"""
    try:
        located = compact_json(locator_payload(locator))
    except TypeError:
        located = compact_json(asdict(locator))
    payload = "\n".join((compact_json(scope), compact_json(detected_kind), located))
    return "coverage:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _record_payload(value, names: tuple[str, ...]) -> dict[str, Any]:
    """`asdict(value)` と同じキーの dict（`locator` だけ dict へ展開し、ほかの値は複製しない）。"""
    payload = dict(value.__dict__)
    if len(payload) != len(names):
        payload = {name: getattr(value, name) for name in names}
    locator = payload.get("locator")
    if isinstance(locator, Locator):
        payload["locator"] = locator_payload(locator)
    return payload


def _array_parts(values):
    """配列要素を `asdict` 経由で 1 件ずつ直列化したのと同じ文字列を、まとめて直列化して返す。"""
    yield "["
    if values:
        names = _field_names(type(values[0]))
        for start in range(0, len(values), _ARRAY_BATCH):
            batch = values[start:start + _ARRAY_BATCH]
            if start:
                yield ","
            try:
                text = _ENCODE([_record_payload(value, names) for value in batch])
            except TypeError:                        # 入れ子の dataclass など、dict への展開が要る値は asdict で変換する
                text = _ENCODE([asdict(value) for value in batch])
            yield text[1:-1]
    yield "]"


def _json_parts(ir: EvidenceIR):
    """sort_keys 相当の root 順で、巨大配列を全 dict 化せず決定的 JSON 片へ展開する。"""
    yield '{"coverage":'
    yield from _array_parts(ir.coverage)
    yield ',"elements":'
    yield from _array_parts(ir.elements)
    yield ',"parser_profile":'
    yield compact_json(ir.parser_profile)
    yield ',"relations":'
    yield from _array_parts(ir.relations)
    yield ',"schema_version":'
    yield compact_json(ir.schema_version)
    yield ',"source":'
    yield compact_json(asdict(ir.source))
    yield "}\n"


def _checked_json_parts(ir: EvidenceIR):
    """検証してから JSON 片を返す（書込と gzip バイト列の直列化はここを共有する）。"""
    errors = validation_errors(ir)
    if errors:
        raise ValueError("invalid Evidence IR: " + ",".join(errors))
    return _json_parts(ir)


@relaxed_gc
def write_json_atomic(path: str | Path, ir: EvidenceIR) -> Path:
    """Evidence IR を gzip で同一ディレクトリの tmp へ stream して原子置換する（設計: docs/design/rag.md「派生物」）。"""
    parts = _checked_json_parts(ir)
    target = Path(path)
    with json_io.atomic_gzip_text_writer(target) as stream:
        stream.writelines(parts)
    return target


def to_gzip_bytes(ir: EvidenceIR) -> bytes:
    """`write_json_atomic` が書くのと同じ gzip バイト列（別プロセスで直列化して親が書くとき用）。"""
    return json_io.gzip_text_bytes(_checked_json_parts(ir))


_READ_CHUNK = 1 << 20
_WS = " \t\n\r"
_BATCH_ITEMS = 256


class _JsonStream:
    """テキストストリームから JSON の値を 1 つずつ取り出す（全文を 1 つの文字列に持たない）。"""

    def __init__(self, stream) -> None:
        self._stream = stream
        self._buf = ""
        self._pos = 0
        self._eof = False
        self._decoder = json.JSONDecoder()

    def _fill(self) -> bool:
        if self._eof:
            return False
        chunk = self._stream.read(_READ_CHUNK)
        if not chunk:
            self._eof = True
            return False
        self._buf = self._buf[self._pos:] + chunk
        self._pos = 0
        return True

    def peek(self) -> str:
        """次の空白でない 1 文字（末尾なら ""）。位置は進めない。"""
        while True:
            buf, pos = self._buf, self._pos
            while pos < len(buf) and buf[pos] in _WS:
                pos += 1
            self._pos = pos
            if pos < len(buf):
                return buf[pos]
            if not self._fill():
                return ""

    def advance(self) -> None:
        self._pos += 1

    def value(self) -> Any:
        """次の JSON 値を 1 つ読む。バッファ末尾で切れた可能性があるときは読み足してやり直す。"""
        self.peek()
        while True:
            try:
                obj, end = self._decoder.raw_decode(self._buf, self._pos)
            except json.JSONDecodeError:
                if not self._fill():
                    raise
                continue
            if end == len(self._buf) and self._fill():     # 切れ目で終わった値は、読み足してからやり直す
                continue
            self._pos = end
            return obj

    def finish(self) -> None:
        if self.peek():
            raise json.JSONDecodeError("Extra data", self._buf, self._pos)

    def _batch(self, delimiter: str) -> list | None:
        """次の要素から `_BATCH_ITEMS` 個を、1 回の `raw_decode` でまとめて読む。

        要素どうしの切れ目が `delimiter`（次の要素の頭までの区切り）で見つかり、その間が配列として読めたときだけ返す（位置は最後の要素の直後）。
        1 回の読み込みの中ではキーの文字列が共有される。読めなければ None（呼び出し側は 1 件ずつ読む）。
        """
        if self.peek() != "{":
            return None
        while True:
            buf, start = self._buf, self._pos
            cut = start
            for _ in range(_BATCH_ITEMS):
                cut = buf.find(delimiter, cut + 1)
                if cut < 0:
                    break
            else:
                break
            if not self._fill():
                return None
        text = "[" + buf[start:cut] + "]"
        try:
            items, end = self._decoder.raw_decode(text)
        except json.JSONDecodeError:
            return None
        if end != len(text) or not isinstance(items, list):
            return None
        self._pos = cut
        return items

    def array_items(self, delimiter: str | None = None):
        """"[" の直後から対応する "]" の直後までの要素を順に返す。`delimiter` を渡すと、要素をまとめて読む。"""
        while True:
            batch = self._batch(delimiter) if delimiter else None
            if batch is not None:
                yield from batch
            else:
                yield self.value()
            separator = self.peek()
            if separator == ",":
                self.advance()
            elif separator == "]":
                self.advance()
                return
            else:
                raise json.JSONDecodeError("Expecting ',' delimiter", self._buf, self._pos)


_SHARED_EXTENSION_KEYS = (
    "calculation_status", "display_source", "display_status", "display_reason", "number_format", "role",
)
_SHARED_ELEMENT_KEYS = ("type", "parent_id", "coverage_id", "visibility", "lifecycle")


def _element_from_json(item: dict, pool: dict[str, str]) -> EvidenceElement:
    setdefault = pool.setdefault                    # 同じ文字列を 1 つの実体に束ねる
    locator = item["locator"]
    part = locator["part"]
    if type(part) is str:
        locator["part"] = setdefault(part, part)
    sheet = locator.get("sheet")
    if type(sheet) is str:
        locator["sheet"] = setdefault(sheet, sheet)
    extension = item.get("extension")
    if type(extension) is dict:
        for key in _SHARED_EXTENSION_KEYS:
            value = extension.get(key)
            if type(value) is str:
                extension[key] = setdefault(value, value)
        typed = extension.get("typed_value")
        if type(typed) is dict:
            kind = typed.get("type")
            if type(kind) is str:
                typed["type"] = setdefault(kind, kind)
    for key in _SHARED_ELEMENT_KEYS:
        value = item.get(key)
        if type(value) is str:
            item[key] = setdefault(value, value)
    item["locator"] = Locator(**locator)
    return EvidenceElement(**item)


def _relation_from_json(item: dict, pool: dict[str, str]) -> EvidenceRelation:
    return EvidenceRelation(**item)


def _coverage_from_json(item: dict, pool: dict[str, str]) -> CoverageItem:
    return CoverageItem(**{**item, "locator": Locator(**item["locator"])})


_ARRAY_BUILDERS = {
    "elements": _element_from_json,
    "relations": _relation_from_json,
    "coverage": _coverage_from_json,
}
_ARRAY_DELIMITERS = {"elements": ',{"coverage_id":'}      # 要素の先頭のキー（キーは並べ替えて書かれる）


def _read_payload(stream: _JsonStream) -> dict:
    """トップレベルの object を読む。elements / relations / coverage は 1 件読むごとに型付きへ変換して持つ。

    形の誤り（キー欠落・型違い）は JSON 全体を読み切ってから `ValueError` にする（構文エラーを先に報告する従来の順序を保つ）。
    """
    first = stream.peek()
    if first != "{":
        value = stream.value() if first else json.loads("")
        stream.finish()
        return value
    stream.advance()
    payload: dict = {}
    shape_error = False
    pool: dict[str, str] = {}
    if stream.peek() == "}":
        stream.advance()
    else:
        while True:
            key = stream.value()
            if stream.peek() != ":":
                raise json.JSONDecodeError("Expecting ':' delimiter", stream._buf, stream._pos)
            stream.advance()
            build = _ARRAY_BUILDERS.get(key) if isinstance(key, str) else None
            if build is not None and stream.peek() == "[":
                stream.advance()
                items: list = []
                if stream.peek() == "]":
                    stream.advance()
                else:
                    for item in stream.array_items(_ARRAY_DELIMITERS.get(key)):
                        if not shape_error:
                            try:
                                items.append(build(item, pool))
                            except (KeyError, TypeError):
                                shape_error = True
                payload[key] = items
            else:
                payload[key] = stream.value()
                if build is not None:
                    shape_error = True
            separator = stream.peek()
            if separator == ",":
                stream.advance()
            elif separator == "}":
                stream.advance()
                break
            else:
                raise json.JSONDecodeError("Expecting ',' delimiter", stream._buf, stream._pos)
    stream.finish()
    if shape_error:
        raise ValueError("invalid Evidence IR shape")
    return payload


@relaxed_gc
def read_json_file(path: str | Path) -> EvidenceIR:
    """`{rel}.evidence.json`（gzip でも非圧縮でも）を型付き Evidence IR として読む。本文全体は持たず、要素を 1 件ずつ型付きへ変換する。"""
    try:
        with json_io.open_text_maybe_gzip(path) as stream:
            payload = _read_payload(_JsonStream(stream))
    except json_io.GZIP_READ_ERRORS as exc:
        raise OSError(f"gzip が壊れています: {exc}") from exc
    return _ir_from_payload(payload)


def from_json_str(raw: str) -> EvidenceIR:
    """決定的 JSON を型付き Evidence IR へ戻し、参照整合性も再検証する。"""
    return _ir_from_payload(_read_payload(_JsonStream(io.StringIO(raw))))


def _ir_from_payload(payload: Any) -> EvidenceIR:
    if not isinstance(payload, dict):
        raise ValueError("Evidence IR must be a JSON object")
    try:
        ir = EvidenceIR(
            schema_version=payload["schema_version"],
            parser_profile=payload["parser_profile"],
            source=EvidenceSource(**payload["source"]),
            elements=payload.get("elements", []),
            relations=payload.get("relations", []),
            coverage=payload.get("coverage", []),
        )
    except (KeyError, TypeError) as exc:
        raise ValueError("invalid Evidence IR shape") from exc
    errors = validation_errors(ir)
    if errors:
        raise ValueError("invalid Evidence IR: " + ",".join(errors))
    return ir
