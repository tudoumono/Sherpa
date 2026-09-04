"""O1: OCR観測をVLMと合流して rag.md（RAG正本）へ統合する（§8.1一本化・2026-09-03）。

背景: L8（`a1c0b735`）が `evidence_render.render(observation_set=...)` の「AI観測」レコード経路を
VLM で初接続した。OCR は隔離ワーカーの非同期ジョブ（`ocr_worker`/`store/ocr_jobs`）が別成果物
（`observation_render`→`{rel}.rag_observations.md`）に書くだけで、この器に乗っていなかった
＝rag.md（正本）・ES・親返し・セル座標引用・LLM 成形の対象外だった。このテストはその統合を固定する。

実 OCR/実 VLM/実 LLM 呼び出しは一切発生しない（OCR は `ocr_worker.build_observation_set` を
手組み engine で直接呼ぶ・VLM は `vision_arm.resolve_vlm`/`_vlm_read` を monkeypatch）。固定する契約:

1. `ai_observation.merge_sets`: 単一 Set はそのまま返す（provider/model 表記を壊さない）。
   複数 Set は1つへ合流し、各観測の真の出所（provider/model/observation_set_hash）を
   `attributes["origin_*"]` へ保持する。
2. OCR 単独（VLM 無し）でも「AI観測」ラベル付きレコードとして rag.md に出る
   （既存の `evidence_render._ai_observation_records` 契約に乗る・`use_for_answer` は
   既存の使用可否ルール＝`ai_observation.MIN_ANSWER_CONFIDENCE` にそのまま従う）。
3. VLM と OCR が同一世代で併存すると、両方の観測が rag.md に出る（合流）。
4. `.rag_sig` の OCR 観測次元（`office_md.rag_sig_drift`）: OCR の公開世代が変わると drift=True
   になる——既存の再生成→再索引連鎖（`refresh_rag`→holdback→ES）をそのまま誘発する
   起点だけを固定する（実 ES 反映自体は worker.py 側の既存契約・ここでは対象外）。
5. `llm_render` は合流後も観測レコードを成形対象外にする（既存契約が壊れていないことの確認）。
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

import pytest

from sherpa.ingest import ai_observation, evidence_ir as IR, evidence_spike, llm_render, ocr_router, ocr_worker, office_md
from sherpa.ingest.arms import vision_arm

_ROOT = Path(__file__).resolve().parents[2]
_XLSX_WITH_PICTURE = _ROOT / "fixtures" / "eval" / "deprecation_markers" / "inputs" / "DEP-XLSX-MARKERS.xlsx"
_GENERATION_ID = "d" * 64


class _FakeOCREngine:
    """`ocr_worker.build_observation_set` が要求する最小限の engine 属性だけを持つ（Paddle不要）。"""

    def __init__(self, *, engine_profile_hash: str | None = None):
        self.engine_profile_hash = engine_profile_hash or ocr_worker.profile_hash()
        self.model_revision = self.engine_profile_hash


def _picture_route(ir, asset_dir: Path, rel: str):
    """`tests/unit/test_ocr_worker.py::_picture_route` と同じ手法（実物 IR/asset から実 route を作る）。"""
    evidence_spike.extract_assets(_XLSX_WITH_PICTURE, ir, asset_dir)
    manifest = ocr_router.build_manifest(
        ir, source_rel_path=rel, assets=ocr_router.inventory_assets(asset_dir))
    decision = next(item for item in manifest.decisions if item.status == "selected")
    job = {
        "id": 1, "world": "world-a", "source_rel_path": manifest.source_rel_path,
        "canonical_generation_id": _GENERATION_ID, "source_content_hash": manifest.source_content_hash,
        "route_manifest_hash": manifest.route_manifest_hash, "engine_profile_hash": ocr_worker.profile_hash(),
        "lease_token": "lease-token",
    }
    return decision, job


def _build_ocr_set(ir, rel: str, asset_dir: Path, *, confidence: float, text: str):
    """`_XLSX_WITH_PICTURE` 実物から、実 OCR/Paddle 呼び出し無しで妥当な OCR AIObservationSet を組む
    （`ocr_worker.build_observation_set` をそのまま呼ぶ・engine だけ手組みの Fake）。"""
    decision, job = _picture_route(ir, asset_dir, rel)
    prepared = ocr_worker.prepare_input(job, decision, source_path=_XLSX_WITH_PICTURE, asset_root=asset_dir)
    engine = _FakeOCREngine()
    prediction = ocr_worker.OCRPrediction([
        ocr_worker.EngineLine(text=text, confidence=confidence, bbox=[1, 2, 30, 40], line_id="0"),
    ])
    # 実運用の値（world署名由来のgeneration id）はここでは重要ではない——merge_setsは
    # canonical_generation_idを比較しない（VLM/OCRで採番方式が違うため・ai_observation.merge_sets
    # docstring参照）。`evidence_binding_id(ir)`はテスト内で妥当な64桁hexを得る手段として使うだけ。
    return ocr_worker.build_observation_set(
        ir=ir, decision=decision, prepared=prepared, prediction=prediction,
        canonical_generation_id=ai_observation.evidence_binding_id(ir), engine=engine,
    )


# ---- 1. ai_observation.merge_sets ------------------------------------------------------------

def _picture_ir(asset_hash: str) -> IR.EvidenceIR:
    source = IR.EvidenceSource(file_type="xlsx", content_hash="sha256:" + "22" * 32)
    locator = IR.Locator(part="xl/drawings/drawing1.xml", sheet="対象")
    coverage_id = IR.make_coverage_id("element", "picture", locator)
    coverage = IR.CoverageItem(
        coverage_id=coverage_id, scope="element", detected_kind="picture",
        locator=locator, status="metadata_only",
        content_basis="pixel_only", reason_code="image_content_uninterpreted",
        parser_id="test", detail={},
    )
    element = IR.EvidenceElement(
        element_id="pic1", type="picture", parent_id=None, order=1, value=None,
        locator=locator, coverage_id=coverage_id, extension={"asset_sha256": asset_hash},
    )
    return IR.EvidenceIR(
        schema_version=IR.EVIDENCE_IR_SCHEMA_VERSION, parser_profile=IR.EVIDENCE_PARSER_PROFILE,
        source=source, elements=[element], coverage=[coverage],
    )


def _asset_hash(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _small_set(ir, *, provider: str, confidence: float, text: str, asset_hash: str) -> ai_observation.AIObservationSet:
    return ai_observation.build(
        ir=ir, provider=provider, model="m1", execution_mode="local",
        prompt_schema_version="v1", preprocessing_profile="p1",
        raw_response=text, canonical_generation_id=_GENERATION_ID,
        inputs=[{"input_id": "in1", "target_evidence_id": "pic1", "asset_sha256": asset_hash,
                "media_type": "image/png", "input_kind": "asset"}],
        observations=[{"input_id": "in1", "kind": "ocr_text", "text": text, "confidence": confidence,
                       "searchable": True, "use_for_answer": confidence >= ai_observation.MIN_ANSWER_CONFIDENCE}],
    )


def test_merge_sets_single_set_returned_unchanged():
    ah = _asset_hash(b"x")
    ir = _picture_ir(ah)
    single = _small_set(ir, provider="paddleocr", confidence=0.9, text="単独OCR", asset_hash=ah)
    assert ai_observation.merge_sets([single], ir=ir) is single


def test_merge_sets_combines_two_sets_and_preserves_origin_in_attributes():
    ah = _asset_hash(b"x")
    ir = _picture_ir(ah)
    ocr_set = _small_set(ir, provider="paddleocr", confidence=0.5, text="OCR文字列", asset_hash=ah)
    vlm_set = ai_observation.build(
        ir=ir, provider="ollama", model="qwen2.5vl", execution_mode="local",
        prompt_schema_version="v1", preprocessing_profile="p1", raw_response="VLM応答",
        canonical_generation_id=_GENERATION_ID,
        inputs=[{"input_id": "in2", "target_evidence_id": "pic1", "asset_sha256": ah,
                "media_type": "image/png", "input_kind": "asset"}],
        observations=[{"input_id": "in2", "kind": "summary", "text": "VLM文字列", "confidence": 0.75,
                       "searchable": True, "use_for_answer": True}],
    )
    merged = ai_observation.merge_sets([ocr_set, vlm_set], ir=ir)
    assert ai_observation.validation_errors(merged, ir=ir) == []
    # 合成Set自身のcanonical_generation_idは、由来Setのどちらでもなくirから改めて採番される。
    assert merged.canonical_generation_id == ai_observation.evidence_binding_id(ir)
    texts = {item.text: item for item in merged.observations}
    assert set(texts) == {"OCR文字列", "VLM文字列"}
    assert texts["OCR文字列"].attributes["origin_provider"] == "paddleocr"
    assert texts["OCR文字列"].attributes["origin_observation_set_hash"] == ocr_set.observation_set_hash
    assert texts["VLM文字列"].attributes["origin_provider"] == "ollama"
    assert texts["VLM文字列"].attributes["origin_observation_set_hash"] == vlm_set.observation_set_hash
    # 由来Setと合流後の使用可否は変わらない（既存ルールをそのまま持ち越す）。
    assert ai_observation.answer_observations(merged) == [texts["VLM文字列"]]


def test_merge_sets_allows_different_canonical_generation_ids():
    """VLM/OCR は採番方式が違う（`merge_sets` docstring）ため、canonical_generation_id 不一致は
    合流を妨げない——`source_content_hash`（同じ原本 bytes）さえ一致していればよい。"""
    ah = _asset_hash(b"x")
    ir = _picture_ir(ah)
    a = _small_set(ir, provider="paddleocr", confidence=0.9, text="A", asset_hash=ah)
    b = ai_observation.build(
        ir=ir, provider="ollama", model="m2", execution_mode="local",
        prompt_schema_version="v1", preprocessing_profile="p1", raw_response="B",
        canonical_generation_id="e" * 64,   # `a` とは異なる採番（world署名由来を模す）
        inputs=[{"input_id": "in3", "target_evidence_id": "pic1", "asset_sha256": ah,
                "media_type": "image/png", "input_kind": "asset"}],
        observations=[{"input_id": "in3", "kind": "summary", "text": "B", "confidence": 0.9,
                       "searchable": True, "use_for_answer": True}],
    )
    assert a.canonical_generation_id != b.canonical_generation_id
    merged = ai_observation.merge_sets([a, b], ir=ir)
    assert ai_observation.validation_errors(merged, ir=ir) == []
    assert {item.text for item in merged.observations} == {"A", "B"}


def test_merge_sets_rejects_different_source_content_hash():
    """同じ原本 bytes に拘束された観測でなければ合流できない（別文書の観測を取り違えない）。"""
    ah = _asset_hash(b"x")
    ir_a = _picture_ir(ah)
    ir_b = IR.EvidenceIR(
        schema_version=ir_a.schema_version, parser_profile=ir_a.parser_profile,
        source=IR.EvidenceSource(file_type="xlsx", content_hash="sha256:" + "33" * 32),
        elements=ir_a.elements, coverage=ir_a.coverage,
    )
    a = _small_set(ir_a, provider="paddleocr", confidence=0.9, text="A", asset_hash=ah)
    b = _small_set(ir_b, provider="ollama", confidence=0.9, text="B", asset_hash=ah)
    assert a.source_content_hash != b.source_content_hash
    with pytest.raises(ValueError):
        ai_observation.merge_sets([a, b], ir=ir_a)


def test_merge_sets_requires_at_least_one_set():
    with pytest.raises(ValueError):
        ai_observation.merge_sets([], ir=_picture_ir(_asset_hash(b"x")))


# ---- 2. office_md._build_observation_set（OCR単独/VLM単独/両方の合流） -----------------------------

def test_build_observation_set_no_vlm_no_ocr_returns_none(monkeypatch, tmp_path):
    from sherpa.ingest import arms as _arms
    monkeypatch.setattr(_arms, "enabled_arm_names", lambda: ["ooxml", "pdf_text"])   # visionは無効
    ir = evidence_spike.extract(_XLSX_WITH_PICTURE)
    asset_dir = tmp_path / "assets"
    evidence_spike.extract_assets(_XLSX_WITH_PICTURE, ir, asset_dir)
    result = office_md._build_observation_set(ir, "a.xlsx", asset_dir, obs_dir=None)
    assert result is None


def test_load_ocr_observation_sets_reads_published_jsonl_and_validates_against_ir(tmp_path):
    ir = evidence_spike.extract(_XLSX_WITH_PICTURE)
    asset_dir = tmp_path / "assets"
    ocr_set = _build_ocr_set(ir, "a.xlsx", asset_dir, confidence=0.9, text="公開済みOCR")
    obs_dir = tmp_path / "obs-current"
    target = obs_dir / "a.xlsx.ai_observations.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(ai_observation.to_json_str(ocr_set), encoding="utf-8")

    loaded = office_md._load_ocr_observation_sets(ir, "a.xlsx", obs_dir)
    assert len(loaded) == 1
    assert loaded[0].observation_set_hash == ocr_set.observation_set_hash


def test_build_observation_set_ocr_only_returns_ocr_set_unchanged(tmp_path):
    """契約1続き: 単一 Set（OCRのみ・VLM無し）はそのまま返す（合成Setを作らない）。"""
    ir = evidence_spike.extract(_XLSX_WITH_PICTURE)
    asset_dir = tmp_path / "assets"
    ocr_set = _build_ocr_set(ir, "a.xlsx", asset_dir, confidence=0.9, text="OCR単独")
    obs_dir = tmp_path / "obs-current"
    target = obs_dir / "a.xlsx.ai_observations.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(ai_observation.to_json_str(ocr_set), encoding="utf-8")

    result = office_md._build_observation_set(ir, "a.xlsx", asset_dir, obs_dir=obs_dir)
    assert result is not None
    assert result.observation_set_hash == ocr_set.observation_set_hash


def test_load_ocr_observation_sets_rejects_stale_source_content_hash(tmp_path):
    """世界の他文書だけが変わって世代が更新されても、この文書自体が変わっていれば安全に弾かれる。"""
    ir = evidence_spike.extract(_XLSX_WITH_PICTURE)
    asset_dir = tmp_path / "assets"
    ocr_set = _build_ocr_set(ir, "a.xlsx", asset_dir, confidence=0.9, text="stale")
    obs_dir = tmp_path / "obs-current"
    target = obs_dir / "a.xlsx.ai_observations.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(ai_observation.to_json_str(ocr_set), encoding="utf-8")

    import dataclasses

    changed_ir = evidence_spike.extract(_XLSX_WITH_PICTURE)
    # 文書自体が変わった体で不一致にする（EvidenceSource は frozen dataclass のため複製で差し替える）。
    changed_ir = dataclasses.replace(
        changed_ir, source=dataclasses.replace(changed_ir.source, content_hash="sha256:" + "ff" * 32))
    loaded = office_md._load_ocr_observation_sets(changed_ir, "a.xlsx", obs_dir)
    assert loaded == []


def test_build_observation_set_merges_vlm_and_ocr_when_both_present(monkeypatch, tmp_path):
    from sherpa.ingest import arms as _arms
    monkeypatch.setattr(_arms, "enabled_arm_names", lambda: ["ooxml", "pdf_text", "vision"])
    monkeypatch.setattr(vision_arm, "resolve_vlm", lambda: {
        "provider": "ollama", "model": "qwen2.5vl", "cloud_allowed": False,
        "ollama_url": "http://localhost:11434",
    })
    monkeypatch.setattr(vision_arm, "_vlm_read", lambda *a, **kw: "VLMの読み取り結果")

    ir = evidence_spike.extract(_XLSX_WITH_PICTURE)
    asset_dir = tmp_path / "assets"
    ocr_set = _build_ocr_set(ir, "a.xlsx", asset_dir, confidence=0.9, text="OCRの読み取り結果")
    obs_dir = tmp_path / "obs-current"
    (obs_dir / "a.xlsx.ai_observations.jsonl").parent.mkdir(parents=True, exist_ok=True)
    (obs_dir / "a.xlsx.ai_observations.jsonl").write_text(ai_observation.to_json_str(ocr_set), encoding="utf-8")

    merged = office_md._build_observation_set(ir, "a.xlsx", asset_dir, obs_dir=obs_dir)
    assert merged is not None
    texts = {item.text for item in merged.observations}
    assert "OCRの読み取り結果" in texts
    assert "VLMの読み取り結果" in texts


# ---- 3. 実往復（office_md.build_derived）: rag.md への統合を実ファイルで固定 --------------------------

def _build_derived_with_world(monkeypatch, tmp_path, source: Path, *, world: str, arms: str = "ooxml,pdf_text"):
    monkeypatch.setenv("SHERPA_ARMS", arms)
    wd = tmp_path / "world"
    wd.mkdir()
    shutil.copy(source, wd / source.name)
    dmd = tmp_path / "derived" / "md"
    rep = office_md.build_derived(wd, dmd, world=world)
    assert not rep.get("error") and rep["rag_failed"] == 0, rep
    rag_path = dmd.parent / "rag" / f"{source.name}.rag.md"
    return rag_path.read_text(encoding="utf-8"), dmd


@pytest.mark.skipif(not _XLSX_WITH_PICTURE.is_file(), reason="fixture が無い環境")
def test_ocr_only_observation_appears_as_ai_observation_in_rag_md(monkeypatch, tmp_path):
    """契約2: OCR単独（VLM無し）でも「AI観測」ラベル付きレコードとしてrag.mdに出る。"""
    # build_derivedが内部で読む実IR/assetsと同じ内容から、実OCR呼び出し無しでSetを組む。
    probe_ir = evidence_spike.extract(_XLSX_WITH_PICTURE)
    ocr_set = _build_ocr_set(probe_ir, _XLSX_WITH_PICTURE.name, tmp_path / "probe-assets",
                             confidence=0.9, text="OCRで読み取った手書き文字")
    obs_dir = tmp_path / "observations-current"
    jsonl = obs_dir / f"{_XLSX_WITH_PICTURE.name}.ai_observations.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text(ai_observation.to_json_str(ocr_set), encoding="utf-8")
    monkeypatch.setattr("sherpa.worlds.observation_current_dir", lambda _w: obs_dir)

    rag, _ = _build_derived_with_world(monkeypatch, tmp_path, _XLSX_WITH_PICTURE, world="ocr-only-world")
    assert "採用AI観測Set:" in rag
    assert "AI観測生成元: paddleocr/" in rag
    assert llm_render._AI_OBSERVATION_BODY_MARKER in rag
    assert "OCRで読み取った手書き文字" in rag


@pytest.mark.skipif(not _XLSX_WITH_PICTURE.is_file(), reason="fixture が無い環境")
def test_low_confidence_ocr_observation_does_not_appear_as_ai_observation(monkeypatch, tmp_path):
    """契約2続き: use_for_answer=False（既存ルール未満のconfidence）はAI観測レコードに出ない。"""
    probe_ir = evidence_spike.extract(_XLSX_WITH_PICTURE)
    ocr_set = _build_ocr_set(probe_ir, _XLSX_WITH_PICTURE.name, tmp_path / "probe-assets",
                             confidence=0.3, text="判読不可気味")
    obs_dir = tmp_path / "observations-current"
    jsonl = obs_dir / f"{_XLSX_WITH_PICTURE.name}.ai_observations.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text(ai_observation.to_json_str(ocr_set), encoding="utf-8")
    monkeypatch.setattr("sherpa.worlds.observation_current_dir", lambda _w: obs_dir)

    rag, _ = _build_derived_with_world(monkeypatch, tmp_path, _XLSX_WITH_PICTURE, world="low-conf-world")
    assert llm_render._AI_OBSERVATION_BODY_MARKER not in rag


@pytest.mark.skipif(not _XLSX_WITH_PICTURE.is_file(), reason="fixture が無い環境")
def test_vlm_and_ocr_coexist_in_rag_md(monkeypatch, tmp_path):
    """契約3: VLMとOCRが同一世代で併存すると、両方の観測がrag.mdに出る。"""
    monkeypatch.setattr(vision_arm, "resolve_vlm", lambda: {
        "provider": "ollama", "model": "qwen2.5vl", "cloud_allowed": False,
        "ollama_url": "http://localhost:11434",
    })
    monkeypatch.setattr(vision_arm, "_vlm_read", lambda *a, **kw: "VLMが見た内容")

    probe_ir = evidence_spike.extract(_XLSX_WITH_PICTURE)
    ocr_set = _build_ocr_set(probe_ir, _XLSX_WITH_PICTURE.name, tmp_path / "probe-assets",
                             confidence=0.95, text="OCRが読んだ内容")
    obs_dir = tmp_path / "observations-current"
    jsonl = obs_dir / f"{_XLSX_WITH_PICTURE.name}.ai_observations.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text(ai_observation.to_json_str(ocr_set), encoding="utf-8")
    monkeypatch.setattr("sherpa.worlds.observation_current_dir", lambda _w: obs_dir)

    rag, _ = _build_derived_with_world(
        monkeypatch, tmp_path, _XLSX_WITH_PICTURE, world="both-world", arms="ooxml,pdf_text,vision")
    assert "OCRが読んだ内容" in rag
    assert "VLMが見た内容" in rag


@pytest.mark.skipif(not _XLSX_WITH_PICTURE.is_file(), reason="fixture が無い環境")
def test_llm_render_still_skips_ocr_origin_observation_record(monkeypatch, tmp_path):
    """契約5: OCR由来のAI観測レコードもllm_renderの成形対象外のまま（既存契約の維持確認）。"""
    probe_ir = evidence_spike.extract(_XLSX_WITH_PICTURE)
    ocr_set = _build_ocr_set(probe_ir, _XLSX_WITH_PICTURE.name, tmp_path / "probe-assets",
                             confidence=0.9, text="成形されてはいけない観測文字列")
    obs_dir = tmp_path / "observations-current"
    jsonl = obs_dir / f"{_XLSX_WITH_PICTURE.name}.ai_observations.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text(ai_observation.to_json_str(ocr_set), encoding="utf-8")
    monkeypatch.setattr("sherpa.worlds.observation_current_dir", lambda _w: obs_dir)

    rag, _ = _build_derived_with_world(monkeypatch, tmp_path, _XLSX_WITH_PICTURE, world="llm-skip-world")
    rag = llm_render.stamp_rule_only(rag) if llm_render.needs_llm_pass(rag) else rag
    from sherpa.ingest import graph_extract

    def _fake_complete(system, user, cfg, timeout=None):
        body = user.split("次のレコードを整形してください:\n\n", 1)[1].split("\n\n---\n参考情報")[0]
        return __import__("json").dumps({"text": body + "\n（成形済み）"})

    monkeypatch.setattr(graph_extract, "complete_json", _fake_complete)
    cfg = {"provider": "openai", "model": "gpt-5.5"}
    result = llm_render.format_document("v1", _XLSX_WITH_PICTURE.name, rag, cfg, {})
    assert result is not None
    assert "成形されてはいけない観測文字列" in result.markdown
    assert "成形されてはいけない観測文字列\n（成形済み）" not in result.markdown


# ---- 4. .rag_sig の OCR 観測次元（既存の再生成→再索引連鎖の起点） ---------------------------------

def test_rag_sig_drift_is_sensitive_to_ocr_observation_marker(monkeypatch, tmp_path):
    dmd = tmp_path / "derived" / "md"
    dmd.mkdir(parents=True)

    calls = {"value": None}
    monkeypatch.setattr(office_md, "current_ocr_observation_marker", lambda world: calls["value"])

    # まだ何もマーカーが無ければdrift=True（既存契約）。
    assert office_md.rag_sig_drift(dmd, world="w") is True

    office_md.write_rag_sig_marker(dmd, world="w")
    assert office_md.rag_sig_drift(dmd, world="w") is False

    # OCRが新しい観測世代を公開した体でmarkerを変える→drift=Trueになる。
    calls["value"] = "canonical1/observation2"
    assert office_md.rag_sig_drift(dmd, world="w") is True

    # 追いつき再生成が起きてmarkerを書き直せば再びFalseへ収束する。
    office_md.write_rag_sig_marker(dmd, world="w")
    assert office_md.rag_sig_drift(dmd, world="w") is False


def test_current_ocr_observation_marker_none_when_world_not_given():
    """`world` を渡さない/OCR無効な既存呼び出し元は挙動不変（次元が常に"none"）。"""
    assert office_md.current_ocr_observation_marker(None) is None


def test_current_ocr_observation_marker_reflects_published_generation(monkeypatch, tmp_path):
    obs_dir = tmp_path / "obs-root" / "canonical-a" / "observation-b"
    monkeypatch.setattr("sherpa.worlds.observation_current_dir", lambda _w: obs_dir)
    assert office_md.current_ocr_observation_marker("world-x") == "canonical-a/observation-b"
