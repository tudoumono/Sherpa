"""OCR/VLMの結果をCanonical Evidenceと分離して保存するAI Observation Set契約。

Observation Setは原本Evidenceを上書きしない。入力画像、対象Evidence、provider/model、prompt、前処理、
応答hashと個々の観測をcontent-addressedな別成果物として固定する。RAG rendererは明示的に渡された
1つのSetだけを参照し、``use_for_answer``かつ最低信頼度以上の観測だけを「AI観測」と明示して追加する。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import evidence_ir


AI_OBSERVATION_SCHEMA_VERSION = "ai-observation-set-v1alpha2"
AI_OBSERVATION_RESOLVER_VERSION = "ai-observation-resolver-v2"
MIN_ANSWER_CONFIDENCE = 0.70
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_GENERATION_ID_RE = re.compile(r"^[0-9a-f]{64}$")
OBSERVATION_KINDS = frozenset({"ocr_text", "status", "relation", "layout", "summary"})
_EXECUTION_MODES = frozenset({"local", "external"})


@dataclass(frozen=True)
class ObservationInput:
    """VLM/OCRへ渡した画像とCanonical Evidence上の対象。"""

    input_id: str
    target_evidence_id: str
    asset_sha256: str
    media_type: str
    pixel_size: list[int] | None = None
    input_kind: str = "asset"
    render_profile: dict[str, Any] | None = None


@dataclass(frozen=True)
class AIObservation:
    """画素から得た1観測。値の確定ではなく、入力画像に結び付いた候補事実。"""

    observation_id: str
    input_id: str
    kind: str
    text: str
    confidence: float
    pixel_bbox: list[int | float] | None = None
    searchable: bool = True
    use_for_answer: bool = False
    numeric_verified: bool = False
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class AIObservationSet:
    schema_version: str
    source_content_hash: str
    canonical_generation_id: str
    provider: str
    model: str
    model_revision: str | None
    execution_mode: str
    prompt_schema_version: str
    preprocessing_profile: str
    engine_profile_hash: str
    response_hash: str
    inputs: list[ObservationInput]
    observations: list[AIObservation]
    observation_set_hash: str


def _tagged_sha256(value: bytes | str) -> str:
    if isinstance(value, bytes):
        return "sha256:" + hashlib.sha256(value).hexdigest()
    raw = value.strip().lower()
    if _SHA256_RE.fullmatch(raw):
        return raw
    if re.fullmatch(r"[0-9a-f]{64}", raw):
        return "sha256:" + raw
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _content_payload(observation_set: AIObservationSet) -> dict:
    payload = asdict(observation_set)
    payload.pop("observation_set_hash", None)
    return payload


def content_hash(observation_set: AIObservationSet) -> str:
    return _tagged_sha256(_canonical(_content_payload(observation_set)))


def _stable_id(prefix: str, *parts: Any) -> str:
    return f"{prefix}:" + hashlib.sha256(_canonical(parts).encode("utf-8")).hexdigest()[:24]


def evidence_binding_id(ir: evidence_ir.EvidenceIR) -> str:
    """派生物全体のgeneration IDが無い呼出側向けに、Evidence bytesへの拘束IDを返す。

    製品workerは``derived_generation_id``を明示して上書きする。評価や単体利用では、このIDでも
    Observationを別のEvidence内容へ誤接続できない。
    """
    return hashlib.sha256(evidence_ir.to_json_str(ir).encode("utf-8")).hexdigest()


def _target_asset_hashes(target: evidence_ir.EvidenceElement) -> set[str]:
    """Evidence要素に直接または複数asset配列で拘束された全ラスタhashを返す。"""
    values: list[Any] = [target.extension.get("asset_sha256")]
    assets = target.extension.get("assets")
    if isinstance(assets, list):
        values.extend(item.get("asset_sha256") for item in assets if isinstance(item, dict))
    return {
        _tagged_sha256(value)
        for value in values
        if isinstance(value, str) and value.strip()
    }


def build(
    *,
    ir: evidence_ir.EvidenceIR,
    provider: str,
    model: str,
    execution_mode: str,
    prompt_schema_version: str,
    preprocessing_profile: str,
    raw_response: bytes | str,
    inputs: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    model_revision: str | None = None,
    canonical_generation_id: str | None = None,
    engine_profile_hash: str | None = None,
) -> AIObservationSet:
    """provider応答を固定したObservation Setへ正規化する。

    ``inputs``と``observations``はprovider固有JSONを直接受けず、呼出adapterが共通fieldへ正規化した値を渡す。
    IDは内容から決定的に生成し、再実行結果は異なるresponse hashとSet hashになる。
    """
    built_inputs: list[ObservationInput] = []
    for index, item in enumerate(inputs, start=1):
        asset_hash = _tagged_sha256(str(item["asset_sha256"]))
        target_id = str(item["target_evidence_id"])
        input_id = str(item.get("input_id") or _stable_id(
            "ai-input", ir.source.content_hash, target_id, asset_hash, index,
        ))
        built_inputs.append(ObservationInput(
            input_id=input_id,
            target_evidence_id=target_id,
            asset_sha256=asset_hash,
            media_type=str(item.get("media_type") or "application/octet-stream"),
            pixel_size=item.get("pixel_size"),
            input_kind=str(item.get("input_kind") or "asset"),
            render_profile=dict(item["render_profile"]) if isinstance(item.get("render_profile"), dict) else None,
        ))

    input_ids = {item.input_id for item in built_inputs}
    built_observations: list[AIObservation] = []
    for index, item in enumerate(observations, start=1):
        input_id = str(item["input_id"])
        # OCRの識別子、空白、改行を原文どおり保持する。空文字判定だけvalidation側でstripして行う。
        text = str(item.get("text") or "")
        observation_id = str(item.get("observation_id") or _stable_id(
            "ai-observation", ir.source.content_hash, input_id, item.get("kind"), text,
            item.get("pixel_bbox"), index,
        ))
        built_observations.append(AIObservation(
            observation_id=observation_id,
            input_id=input_id,
            kind=str(item.get("kind") or "summary"),
            text=text,
            confidence=float(item.get("confidence", 0.0)),
            pixel_bbox=item.get("pixel_bbox"),
            searchable=bool(item.get("searchable", True)),
            use_for_answer=bool(item.get("use_for_answer", False)),
            numeric_verified=bool(item.get("numeric_verified", False)),
            attributes=dict(item.get("attributes") or {}),
        ))
    if any(item.input_id not in input_ids for item in built_observations):
        raise ValueError("AI observation references unknown input")

    result = AIObservationSet(
        schema_version=AI_OBSERVATION_SCHEMA_VERSION,
        source_content_hash=ir.source.content_hash,
        canonical_generation_id=(canonical_generation_id or evidence_binding_id(ir)).strip().lower(),
        provider=provider.strip(),
        model=model.strip(),
        model_revision=model_revision.strip() if isinstance(model_revision, str) and model_revision.strip() else None,
        execution_mode=execution_mode.strip(),
        prompt_schema_version=prompt_schema_version.strip(),
        preprocessing_profile=preprocessing_profile.strip(),
        engine_profile_hash=_tagged_sha256(engine_profile_hash or _canonical({
            "provider": provider.strip(),
            "model": model.strip(),
            "model_revision": model_revision,
            "prompt_schema_version": prompt_schema_version.strip(),
            "preprocessing_profile": preprocessing_profile.strip(),
        })),
        response_hash=_tagged_sha256(raw_response if isinstance(raw_response, bytes) else raw_response.encode("utf-8")),
        inputs=built_inputs,
        observations=built_observations,
        observation_set_hash="",
    )
    result.observation_set_hash = content_hash(result)
    errors = validation_errors(result, ir=ir)
    if errors:
        raise ValueError("invalid AI Observation Set: " + ",".join(errors))
    return result


AI_OBSERVATION_MERGE_VERSION = "ai-observation-merge-v1"


def merge_sets(
    sets: list[AIObservationSet],
    *,
    ir: evidence_ir.EvidenceIR,
) -> AIObservationSet:
    """複数アーム（VLM・OCR等）の観測Setを、単一Set契約のレンダラ（`evidence_render.render`）へ
    渡せる1つのSetへ合流する（O1・L8の器はSetを1つしか受けない）。

    ``sets``が1件なら（新規Setを作らず）そのまま返す——単一由来のときは`observation_set.provider`/
    `.model`が正確な出所を表すという既存契約（`evidence_render._ai_observation_records`が
    Setレベルのprovider/modelを本文へ埋め込む）を壊さない。2件以上のときだけ合成Setを組む。

    各観測の真の出所（元Setのprovider/model/model_revision/execution_mode/observation_set_hash）は
    `attributes["origin_*"]`へ書き足して保持する——合成Set自体のprovider/model（複数由来の連結）は
    本文の文言としては粗いが、`record["ai_observation"]["attributes"]`経由で由来を復元できる
    （どちら由来かのメタが失われない・O1 要件）。

    同一``input_id``が複数Setに現れた場合（同じ画像要素をVLMとOCRの両方が選定した場合等）、
    内容（asset_sha256等）が一致する前提で1つへ畳む——不一致は取り違えとみなし例外にする。

    ``source_content_hash``（同じ原本 bytes に拘束された観測か）が食い違うSet同士は合流できない
    （異なる原本の観測を混ぜない）。``canonical_generation_id``は意図的に**比較しない**——
    アームごとに異なる採番方式を使う（VLM＝`evidence_binding_id(ir)`＝文書内容のhash・
    OCR＝world署名由来のworld単位generation id）ため、同じ原本・同じ瞬間の観測でも一致しない
    のが正常であり、一致を要求すると VLM/OCR は実運用で決して合流できなくなる。合成Set自身の
    ``canonical_generation_id``は、``ir``から`evidence_binding_id`で改めて決定的に採番する
    （元Setのどちらの値でもない、この合流結果に固有のID）。
    """
    if not sets:
        raise ValueError("at least one AI Observation Set is required")
    if len(sets) == 1:
        return sets[0]
    ordered = sorted(sets, key=lambda item: item.observation_set_hash)
    source_content_hash = ordered[0].source_content_hash
    if any(item.source_content_hash != source_content_hash for item in ordered):
        raise ValueError("cannot merge AI Observation Sets bound to different source content")

    merged_inputs: dict[str, dict[str, Any]] = {}
    for item in ordered:
        for observation_input in item.inputs:
            payload = asdict(observation_input)
            existing = merged_inputs.get(observation_input.input_id)
            if existing is not None and existing != payload:
                raise ValueError(f"conflicting observation input across Sets: {observation_input.input_id}")
            merged_inputs[observation_input.input_id] = payload

    merged_observations: list[dict[str, Any]] = []
    seen_observation_ids: set[str] = set()
    for item in ordered:
        for observation in item.observations:
            if observation.observation_id in seen_observation_ids:
                raise ValueError(f"duplicate observation id across Sets: {observation.observation_id}")
            seen_observation_ids.add(observation.observation_id)
            payload = asdict(observation)
            attributes = dict(payload.get("attributes") or {})
            attributes["origin_provider"] = item.provider
            attributes["origin_model"] = item.model
            attributes["origin_model_revision"] = item.model_revision
            attributes["origin_execution_mode"] = item.execution_mode
            attributes["origin_observation_set_hash"] = item.observation_set_hash
            payload["attributes"] = attributes
            merged_observations.append(payload)

    providers = sorted({item.provider for item in ordered})
    execution_modes = {item.execution_mode for item in ordered}
    engine_profile_hash = _tagged_sha256(_canonical({
        "merge_version": AI_OBSERVATION_MERGE_VERSION,
        "source_engine_profile_hashes": sorted(item.engine_profile_hash for item in ordered),
    }))
    return build(
        ir=ir,
        provider="+".join(providers),
        model="+".join(sorted(f"{item.provider}:{item.model}" for item in ordered)),
        model_revision="+".join(sorted({item.model_revision for item in ordered if item.model_revision})) or None,
        execution_mode="external" if "external" in execution_modes else "local",
        prompt_schema_version="+".join(sorted({item.prompt_schema_version for item in ordered})),
        preprocessing_profile="+".join(sorted({item.preprocessing_profile for item in ordered})),
        engine_profile_hash=engine_profile_hash,
        canonical_generation_id=evidence_binding_id(ir),
        raw_response=_canonical({item.provider: item.response_hash for item in ordered}),
        inputs=list(merged_inputs.values()),
        observations=merged_observations,
    )


def validation_errors(
    observation_set: AIObservationSet,
    *,
    ir: evidence_ir.EvidenceIR | None = None,
) -> list[str]:
    errors: list[str] = []
    if observation_set.schema_version != AI_OBSERVATION_SCHEMA_VERSION:
        errors.append("schema_version")
    for field_name in ("source_content_hash", "engine_profile_hash", "response_hash", "observation_set_hash"):
        if not _SHA256_RE.fullmatch(str(getattr(observation_set, field_name))):
            errors.append(field_name)
    if not _GENERATION_ID_RE.fullmatch(observation_set.canonical_generation_id):
        errors.append("canonical_generation_id")
    if observation_set.provider.strip() == "":
        errors.append("provider")
    if observation_set.model.strip() == "":
        errors.append("model")
    if observation_set.execution_mode not in _EXECUTION_MODES:
        errors.append("execution_mode")
    if not observation_set.prompt_schema_version:
        errors.append("prompt_schema_version")
    if not observation_set.preprocessing_profile:
        errors.append("preprocessing_profile")

    input_ids = [item.input_id for item in observation_set.inputs]
    if len(input_ids) != len(set(input_ids)):
        errors.append("duplicate_input_id")
    for item in observation_set.inputs:
        if not item.input_id or not item.target_evidence_id:
            errors.append("input_identity")
        if not _SHA256_RE.fullmatch(item.asset_sha256):
            errors.append(f"input_asset_sha256:{item.input_id}")
        if not item.media_type:
            errors.append(f"input_media_type:{item.input_id}")
        if item.input_kind not in {"asset", "page_render"}:
            errors.append(f"input_kind:{item.input_id}")
        if item.input_kind == "page_render" and not item.render_profile:
            errors.append(f"input_render_profile:{item.input_id}")
        if item.pixel_size is not None and (
            len(item.pixel_size) != 2 or any(not isinstance(value, int) or value <= 0 for value in item.pixel_size)
        ):
            errors.append(f"input_pixel_size:{item.input_id}")

    input_set = set(input_ids)
    observation_ids = [item.observation_id for item in observation_set.observations]
    if len(observation_ids) != len(set(observation_ids)):
        errors.append("duplicate_observation_id")
    for item in observation_set.observations:
        if item.input_id not in input_set:
            errors.append(f"observation_input:{item.observation_id}")
        if item.kind not in OBSERVATION_KINDS:
            errors.append(f"observation_kind:{item.observation_id}")
        if not item.text.strip():
            errors.append(f"observation_text:{item.observation_id}")
        if not 0.0 <= item.confidence <= 1.0:
            errors.append(f"observation_confidence:{item.observation_id}")
        if item.use_for_answer and item.confidence < MIN_ANSWER_CONFIDENCE:
            errors.append(f"answer_confidence:{item.observation_id}")
        if item.use_for_answer and not item.searchable:
            errors.append(f"answer_not_searchable:{item.observation_id}")
        bbox = item.pixel_bbox
        if bbox is not None and (
            len(bbox) != 4
            or any(not isinstance(value, (int, float)) or value < 0 for value in bbox)
            or bbox[2] <= bbox[0]
            or bbox[3] <= bbox[1]
        ):
            errors.append(f"observation_bbox:{item.observation_id}")

    if _SHA256_RE.fullmatch(observation_set.observation_set_hash):
        expected = content_hash(observation_set)
        if expected != observation_set.observation_set_hash:
            errors.append("observation_set_hash_mismatch")

    if ir is not None:
        if observation_set.source_content_hash != ir.source.content_hash:
            errors.append("source_content_hash_mismatch")
        elements = {element.element_id: element for element in ir.elements}
        for item in observation_set.inputs:
            target = elements.get(item.target_evidence_id)
            if target is None:
                errors.append(f"target_evidence_missing:{item.input_id}")
                continue
            if item.input_kind == "asset":
                if item.asset_sha256 not in _target_asset_hashes(target):
                    errors.append(f"target_asset_mismatch:{item.input_id}")
            elif target.type != "page":
                errors.append(f"page_render_target:{item.input_id}")
    return sorted(set(errors))


def answer_observations(observation_set: AIObservationSet) -> list[AIObservation]:
    """Semantic/RAG viewへ採用できる明示承認済み観測だけを返す。"""
    return [
        item for item in observation_set.observations
        if item.use_for_answer and item.confidence >= MIN_ANSWER_CONFIDENCE
    ]


def searchable_observations(observation_set: AIObservationSet) -> list[AIObservation]:
    """検索用の別観測artifactへ搬送する観測を、回答採否とは独立して返す。"""
    return [item for item in observation_set.observations if item.searchable]


def to_json_str(observation_set: AIObservationSet) -> str:
    errors = validation_errors(observation_set)
    if errors:
        raise ValueError("invalid AI Observation Set: " + ",".join(errors))
    return _canonical(asdict(observation_set)) + "\n"


def from_json_str(raw: str, *, ir: evidence_ir.EvidenceIR | None = None) -> AIObservationSet:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("AI Observation Set must be a JSON object")
    try:
        result = AIObservationSet(
            schema_version=payload["schema_version"],
            source_content_hash=payload["source_content_hash"],
            canonical_generation_id=payload["canonical_generation_id"],
            provider=payload["provider"],
            model=payload["model"],
            model_revision=payload.get("model_revision"),
            execution_mode=payload["execution_mode"],
            prompt_schema_version=payload["prompt_schema_version"],
            preprocessing_profile=payload["preprocessing_profile"],
            engine_profile_hash=payload["engine_profile_hash"],
            response_hash=payload["response_hash"],
            inputs=[ObservationInput(**item) for item in payload.get("inputs", [])],
            observations=[AIObservation(**item) for item in payload.get("observations", [])],
            observation_set_hash=payload["observation_set_hash"],
        )
    except (KeyError, TypeError) as exc:
        raise ValueError("invalid AI Observation Set shape") from exc
    errors = validation_errors(result, ir=ir)
    if errors:
        raise ValueError("invalid AI Observation Set: " + ",".join(errors))
    return result


def write_json_atomic(path: str | Path, observation_set: AIObservationSet) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as stream:
            stream.write(to_json_str(observation_set))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target
