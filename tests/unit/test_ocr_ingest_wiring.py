"""取り込みとOCRの接続（2026-08-16 S3-C）。

OCRは**既定ON**（決定 2026-08-16「初期から組み込む」）。この suite が固定するのは4点。

  1. 既定でルートが作られ、`SHERPA_OCR_ENABLED=0` で止められる
  2. OCRルートは**公開前のステージング内**で作られる（Evidenceとルートが食い違う瞬間を作らない）
  3. OCR側の失敗は取り込み本体を巻き添えにしない（任意観測なので）
  4. 世代IDは投入側と照合側で同じ写像を通る（片方だけ生の署名を使うと永久に処理されない）

上流（feat/rag-gate）はここをフル世代管理（採番した generation ID）で組んでいる。このブランチは
世代管理を採らず、World署名を同じ役割に充てている（`sherpa/ingest/derived_generation.py` 参照）。
"""
from __future__ import annotations

import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

from pathlib import Path  # noqa: E402

from sherpa.ingest import derived_generation, office_md  # noqa: E402


def _world(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "src"
    source.mkdir()
    (source / "資料.txt").write_text("本文", encoding="utf-8")
    return source, tmp_path / "md"


def test_ocr_runs_by_default(tmp_path, monkeypatch):
    """既定（env 未設定）で OCR ルートが作られる。"""
    monkeypatch.delenv(office_md._OCR_ENABLED_ENV, raising=False)
    source, published = _world(tmp_path)

    called = {"routes": 0}
    monkeypatch.setattr(office_md, "_write_ocr_routes",
                        lambda stage_ir, stage_rag: called.__setitem__("routes", called["routes"] + 1) or {})
    office_md.build_derived(str(source), str(published))

    assert office_md.ocr_enabled() is True
    assert called["routes"] == 1, "既定ONなのにOCRルートを作っていない"


def test_ocr_can_be_turned_off(tmp_path, monkeypatch):
    """`SHERPA_OCR_ENABLED=0` で完全に止まる（ルートも作らない）。"""
    monkeypatch.setenv(office_md._OCR_ENABLED_ENV, "0")
    source, published = _world(tmp_path)

    called = {"routes": 0}
    monkeypatch.setattr(office_md, "_write_ocr_routes",
                        lambda stage_ir, stage_rag: called.__setitem__("routes", called["routes"] + 1) or {})
    rep = office_md.build_derived(str(source), str(published))

    assert office_md.ocr_enabled() is False
    assert called["routes"] == 0, "OFF なのにOCRルートを作っている"
    assert "ocr_routes" not in rep
    assert not list(published.parent.rglob("*.ocr_route.json"))


def test_ocr_routes_are_written_into_staging_before_publish(tmp_path, monkeypatch):
    """有効時、ルートは公開前のステージングにできる＝公開と同時に切り替わる。
    `.ocr_route.json`/`.evidence.json` はいずれも ir 層（§8.1 三階層）に同居する。"""
    monkeypatch.setenv(office_md._OCR_ENABLED_ENV, "1")
    source, published = _world(tmp_path)
    published_ir = published.parent / "ir"
    staging_ir = published_ir.with_name(published_ir.name + office_md._STAGING_SUFFIX)

    seen: list[Path] = []
    real_publish = office_md._publish_staging

    def _spy(stage, target):
        seen.append(stage)
        return real_publish(stage, target)

    def _routes(stage_ir: Path, stage_rag: Path) -> dict:
        (stage_ir / "印.ocr_route.json").write_text("{}", encoding="utf-8")
        return {"documents": 1, "selected": 0, "excluded": 0, "failed_binding": 0}

    monkeypatch.setattr(office_md, "_publish_staging", _spy)
    monkeypatch.setattr(office_md, "_write_ocr_routes", _routes)
    rep = office_md.build_derived(str(source), str(published))

    assert rep["ocr_routes"]["documents"] == 1
    # 3層（ir→rag→md）それぞれ独立に publish されるが、ルートを書いた ir 層のステージングは
    # ステージングのまま公開された（公開中を直接書き換えていない）。
    assert staging_ir in seen, "ir層のステージング以外を公開している"
    assert (published_ir / "印.ocr_route.json").exists(), "ルートが公開物（ir層）に入っていない"


def test_ocr_route_failure_does_not_block_canonical_publication(tmp_path, monkeypatch):
    """OCRは任意観測＝ルート生成が失敗しても本体の公開は止めない。"""
    monkeypatch.setenv(office_md._OCR_ENABLED_ENV, "1")
    source, published = _world(tmp_path)

    def _boom(stage_ir, stage_rag):
        raise RuntimeError("route generation failed")

    monkeypatch.setattr(office_md, "_write_ocr_routes", _boom)
    rep = office_md.build_derived(str(source), str(published))

    assert rep.get("error") is None, "OCRの失敗をCanonicalの失敗へ昇格させている"
    assert rep["ocr_routes_error"] == "RuntimeError"
    assert published.is_dir() and list(published.iterdir())


def test_published_derived_carries_the_world_signature_as_generation_id(tmp_path):
    """公開物に刻んだ World 署名が、簡易版の世代IDとして読み出せる。"""
    source, published = _world(tmp_path)
    office_md.build_derived(str(source), str(published), world_sig="sig-abc")

    derived_root = published.parent
    assert derived_generation.active_dir(derived_root) == published
    assert derived_generation.active_world_sig(derived_root) == "sig-abc"
    assert derived_generation.active_generation_id(derived_root) == \
        derived_generation.generation_id_for("sig-abc")


def test_generation_id_is_accepted_by_the_job_queue(tmp_path):
    """世代IDがジョブ投入の検証を実際に通ること。

    実際に起きた不具合（2026-08-16）: World 署名は SHA1（40桁）なのに、ジョブ側の世代IDは
    64桁16進を要求していた。取り込みは best-effort の except に守られて成功するため気づかず、
    OCR だけが永久に動かない状態になっていた（enqueue が毎回 ValueError で落ちていた）。
    形を突き合わせるテストが無かったのが見落としの原因。
    """
    from sherpa.ingest import worker as ingest_worker
    from sherpa.store import ocr_jobs

    world_sig = ingest_worker._sig(["どんな内容でも"])          # 実物と同じ作り方（SHA1）
    generation = derived_generation.generation_id_for(world_sig)

    assert ocr_jobs._generation_id(generation) == generation    # 投入側の検証を通る
    assert len(generation) == 64


def test_ingest_enqueues_with_a_valid_generation_id(tmp_path, monkeypatch):
    """取り込み後の enqueue が、実際に呼べる引数で呼ばれること（例外で握り潰されていない）。"""
    from sherpa.ingest import worker as ingest_worker
    from sherpa.store import ocr_jobs

    monkeypatch.setenv(office_md._OCR_ENABLED_ENV, "1")
    seen: dict = {}

    def _enqueue(world, generation, profile, **kw):
        seen.update(world=world, generation=generation, profile=profile)
        return {"id": 1}

    monkeypatch.setattr(ocr_jobs, "enqueue_refresh_run", _enqueue)
    ingest_worker._enqueue_ocr_refresh("w1", ingest_worker._sig(["中身"]))

    assert seen["world"] == "w1"
    assert ocr_jobs._generation_id(seen["generation"]) == seen["generation"]
    assert seen["profile"].startswith("sha256:")


def test_missing_signature_reads_as_unknown_not_as_a_match(tmp_path):
    """署名が無い派生物は「不明」＝どの世代とも一致させない（古い派生物へ書き込ませない）。"""
    source, published = _world(tmp_path)
    office_md.build_derived(str(source), str(published))       # world_sig を渡さない

    assert derived_generation.active_generation_id(published.parent) is None
