"""WMF/EMF の図全体を LibreOffice で描いて OCR に回す（OBS-08 の B の補完）。

soffice はプロセス境界（`SHERPA_SOFFICE_BIN` の偽スクリプト）で差し替える。実物は最後の 1 本だけ。
"""
from __future__ import annotations

import contextlib
import json
import os
import stat
import sys
from pathlib import Path

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

import pytest  # noqa: E402

import _metafile_builders as mb  # noqa: E402
from sherpa.ingest import derived_generation, metafile_render, metafile_text, ocr_router, office_md  # noqa: E402
from sherpa.ingest.arms import legacy_convert  # noqa: E402

_FAKE = '''#!{python}
import os, sys, time
mode = os.environ.get("FAKE_SOFFICE_MODE", "ok")
args = sys.argv[1:]
outdir = args[args.index("--outdir") + 1]
src = args[-1]
with open(os.environ["FAKE_SOFFICE_LOG"], "a") as log:
    log.write(src + "\\n")
if mode == "fail":
    sys.exit(1)
if mode == "hang":
    time.sleep(60)
out = os.path.join(outdir, os.path.splitext(os.path.basename(src))[0] + ".png")
if mode == "bad":
    open(out, "wb").write(b"not a png")
else:
    from PIL import Image
    Image.new("RGB", (120, 80), (255, 255, 255)).save(out)
'''


@pytest.fixture
def fake_soffice(monkeypatch, tmp_path):
    script = tmp_path / "bin" / "soffice"
    script.parent.mkdir()
    script.write_text(_FAKE.format(python=sys.executable), encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "soffice.log"
    monkeypatch.setenv("SHERPA_SOFFICE_BIN", str(script))
    monkeypatch.setenv("FAKE_SOFFICE_LOG", str(log))
    return log


def _vector_emf() -> bytes:
    return mb.emf([mb.emf_exttextout_w("見出し", 10, 10)])          # ビットマップ無し・文字わずか


def _build(monkeypatch, tmp_path: Path, emf: bytes, names: tuple[str, ...] = ("a.docx",)):
    monkeypatch.setenv("SHERPA_MCP_ARMS", "ooxml,pdf_text")
    wd = tmp_path / "world"
    wd.mkdir()
    for name in names:
        (wd / name).write_bytes(mb.docx_with_media("image1.emf", emf))
    dmd = tmp_path / "derived" / "md"
    rep = office_md.build_derived(wd, dmd, world="metafile-render-world")
    assert not rep.get("error") and rep["rag_failed"] == 0, rep
    return wd, dmd, rep


def _decisions(dmd: Path, name: str = "a.docx") -> list[dict]:
    route = dmd.parent / "ir" / f"{name}.ocr_route.json"
    return json.loads(route.read_text(encoding="utf-8"))["decisions"]


def _pass(dmd: Path, **kw) -> dict:
    return metafile_render.run_pass(
        [("metafile-render-world", dmd.parent)], lock=lambda _w: contextlib.nullcontext(), **kw)


def _calls(log: Path) -> int:
    return len(log.read_text().splitlines()) if log.exists() else 0


def test_sync_only_records_pending_and_background_pass_renders_and_selects_the_child(
        monkeypatch, tmp_path, fake_soffice):
    _, dmd, _ = _build(monkeypatch, tmp_path, _vector_emf())
    assert _calls(fake_soffice) == 0                                     # sync は LibreOffice を呼ばない
    (parent,) = [d for d in _decisions(dmd) if d["input_kind"] == "asset"]
    assert parent["reason_code"] == ocr_router.METAFILE_RENDER_PENDING and parent["status"] == "excluded"

    assert _pass(dmd) == {"rendered": 1, "failed": 0, "remaining": 0}
    decisions = _decisions(dmd)
    parent = next(d for d in decisions if d["reason_code"] == ocr_router.METAFILE_EXPANDED)
    child = next(d for d in decisions if d["reason_code"] == ocr_router.METAFILE_RENDERED)
    assert child["status"] == "selected" and child["target_evidence_id"] == parent["target_evidence_id"]
    assert child["detail"]["parent_asset_sha256"] == parent["asset_sha256"] and child["pixel_size"] == [120, 80]
    assert _calls(fake_soffice) == 1
    assert _pass(dmd)["rendered"] == 0 and _calls(fake_soffice) == 1     # 描画済みは再実行しない


def test_pending_survives_restart_and_resumes(monkeypatch, tmp_path, fake_soffice):
    _, dmd, _ = _build(monkeypatch, tmp_path, _vector_emf())
    assert _pass(dmd, max_renders=0) == {"rendered": 0, "failed": 0, "remaining": 1}   # 1 件も進まない pass
    assert _calls(fake_soffice) == 0
    # 状態はディスクにだけある（プロセスの記憶に頼らない）＝新しい pass がそのまま続きを進める。
    assert _pass(dmd)["rendered"] == 1
    assert any(d["reason_code"] == ocr_router.METAFILE_RENDERED for d in _decisions(dmd))


def test_same_metafile_in_two_documents_is_rendered_once(monkeypatch, tmp_path, fake_soffice):
    _, dmd, _ = _build(monkeypatch, tmp_path, _vector_emf(), names=("a.docx", "b.docx"))
    assert _pass(dmd)["rendered"] == 1 and _calls(fake_soffice) == 1
    for name in ("a.docx", "b.docx"):
        assert any(d["reason_code"] == ocr_router.METAFILE_RENDERED for d in _decisions(dmd, name))


@pytest.mark.parametrize("mode,failure", [("fail", "convert_failed"), ("bad", "bad_output"), ("hang", "timeout")])
def test_failed_render_is_recorded_with_its_reason_and_not_retried(
        monkeypatch, tmp_path, fake_soffice, mode, failure):
    monkeypatch.setenv("FAKE_SOFFICE_MODE", mode)
    monkeypatch.setenv("SHERPA_LEGACY_TIMEOUT", "1")
    _, dmd, _ = _build(monkeypatch, tmp_path, _vector_emf())
    assert _pass(dmd) == {"rendered": 0, "failed": 1, "remaining": 0}
    parent = next(d for d in _decisions(dmd) if d["reason_code"] == ocr_router.METAFILE_RENDER_FAILED)
    assert parent["status"] == "excluded" and parent["detail"]["render_failure"] == failure
    assert not any(d["status"] == "selected" for d in _decisions(dmd))
    assert _pass(dmd)["failed"] == 0 and _calls(fake_soffice) == 1


def test_missing_libreoffice_is_recorded_as_unsupported_then_picked_up_when_it_appears(
        monkeypatch, tmp_path, fake_soffice):
    fake_bin = os.environ["SHERPA_SOFFICE_BIN"]
    monkeypatch.setenv("SHERPA_SOFFICE_BIN", str(tmp_path / "no-such-soffice"))
    _, dmd, rep = _build(monkeypatch, tmp_path, _vector_emf())
    (parent,) = [d for d in _decisions(dmd) if d["input_kind"] == "asset"]
    assert parent["status"] == "excluded" and parent["reason_code"] == ocr_router.METAFILE_RENDER_UNAVAILABLE
    assert parent["detail"]["message"] == "未対応（LibreOffice が入っていません）"
    assert rep["ocr_routes"]["metafile_render_unavailable"] == 1
    assert _pass(dmd)["rendered"] == 0 and _calls(fake_soffice) == 0

    monkeypatch.setenv("SHERPA_SOFFICE_BIN", fake_bin)                   # 後から入った（版の更新は不要）
    assert _pass(dmd)["rendered"] == 1
    assert any(d["reason_code"] == ocr_router.METAFILE_RENDERED for d in _decisions(dmd))


def test_pass_does_nothing_while_ocr_is_disabled(monkeypatch, tmp_path, fake_soffice):
    _, dmd, _ = _build(monkeypatch, tmp_path, _vector_emf())
    monkeypatch.setenv(office_md._OCR_ENABLED_ENV, "0")
    assert _pass(dmd) == {"rendered": 0, "failed": 0, "remaining": 0, "skipped": "ocr_disabled"}
    assert _calls(fake_soffice) == 0
    assert any(d["reason_code"] == ocr_router.METAFILE_RENDER_PENDING for d in _decisions(dmd))
    monkeypatch.setenv(office_md._OCR_ENABLED_ENV, "1")
    assert _pass(dmd)["rendered"] == 1                                   # 状態は残っている＝有効に戻せば進む


def test_folder_removed_between_render_and_apply_leaves_nothing_behind(monkeypatch, tmp_path, fake_soffice):
    import shutil

    _, dmd, _ = _build(monkeypatch, tmp_path, _vector_emf())
    derived = dmd.parent

    def _lock_after_folder_is_deleted(_world):
        shutil.rmtree(derived)                                           # 描画中に資料フォルダが削除された
        return contextlib.nullcontext()

    result = metafile_render.run_pass([("metafile-render-world", derived)], lock=_lock_after_folder_is_deleted)
    assert _calls(fake_soffice) == 1 and result["rendered"] == 0
    assert not derived.exists()                                          # キャッシュも assets も作り直さない


@pytest.mark.parametrize("failing", ["route", "enqueue"])
def test_failure_after_render_is_retried_from_cache_without_a_second_soffice_call(
        monkeypatch, tmp_path, fake_soffice, failing):
    from sherpa.store import ocr_jobs

    _, dmd, _ = _build(monkeypatch, tmp_path, _vector_emf())
    (dmd / office_md._WORLD_SIG_MARKER).write_text("sig\n", encoding="utf-8")
    enqueued: list[str] = []
    state = {"armed": True}

    def _enqueue(world, manifest, **_kw):
        if failing == "enqueue" and state["armed"]:
            state["armed"] = False
            raise RuntimeError("enqueue failed once")
        enqueued.extend(d.route_input_id for d in manifest.decisions if d.status == "selected")

    real_rewrite = metafile_render._rewrite_route

    def _rewrite(derived, rel):
        if failing == "route" and state["armed"]:
            state["armed"] = False
            raise RuntimeError("route write failed once")
        return real_rewrite(derived, rel)

    monkeypatch.setattr(ocr_jobs, "enqueue_manifest_jobs", _enqueue)
    monkeypatch.setattr(metafile_render, "_rewrite_route", _rewrite)
    _pass(dmd)
    assert not enqueued                                                  # 1 回目は OCR job まで届かない
    assert _pass(dmd)["remaining"] == 0                                  # 状態が残っていたので続きを進める
    assert _calls(fake_soffice) == 1
    child = next(d for d in _decisions(dmd) if d["reason_code"] == ocr_router.METAFILE_RENDERED)
    assert child["route_input_id"] in enqueued
    assert _pass(dmd) == {"rendered": 0, "failed": 0, "remaining": 0}    # 仕上がったら状態は消えている


def test_metafile_over_the_size_limit_is_recorded_with_its_reason_and_not_rendered(
        monkeypatch, tmp_path, fake_soffice):
    monkeypatch.setattr(metafile_text, "MAX_METAFILE_BYTES", 64)         # 小さな合成図を「大きすぎる」扱いにする
    _, dmd, _ = _build(monkeypatch, tmp_path, _vector_emf())
    (parent,) = [d for d in _decisions(dmd) if d["input_kind"] == "asset"]
    assert parent["status"] == "excluded" and parent["reason_code"] == ocr_router.METAFILE_TOO_LARGE
    assert "32 MiB" in parent["detail"]["message"]
    assert _pass(dmd) == {"rendered": 0, "failed": 0, "remaining": 0} and _calls(fake_soffice) == 0


def _bitmap_and_little_text_emf() -> bytes:
    return mb.emf([mb.emf_exttextout_w("見出し", 10, 10), mb.emf_stretchdibits(mb.dib(64, 48))])


def _selected_reasons(dmd: Path) -> set[str]:
    return {d["reason_code"] for d in _decisions(dmd) if d["status"] == "selected"}


def test_bitmap_figure_with_little_text_records_render_unavailable_then_selects_both(
        monkeypatch, tmp_path, fake_soffice):
    fake_bin = os.environ["SHERPA_SOFFICE_BIN"]
    monkeypatch.setenv("SHERPA_SOFFICE_BIN", str(tmp_path / "no-such-soffice"))
    _, dmd, rep = _build(monkeypatch, tmp_path, _bitmap_and_little_text_emf())
    parent = next(d for d in _decisions(dmd) if d["reason_code"] == ocr_router.METAFILE_EXPANDED)
    assert parent["detail"]["message"] == ocr_router.METAFILE_RENDER_UNAVAILABLE_MESSAGE
    assert _selected_reasons(dmd) == {"metafile_embedded_bitmap"}          # DIB の子は今までどおり選ぶ
    assert rep["ocr_routes"]["metafile_render_unavailable"] == 1
    monkeypatch.setenv("SHERPA_SOFFICE_BIN", fake_bin)
    assert _pass(dmd)["rendered"] == 1
    assert _selected_reasons(dmd) == {"metafile_embedded_bitmap", ocr_router.METAFILE_RENDERED}
    parent = next(d for d in _decisions(dmd) if d["reason_code"] == ocr_router.METAFILE_EXPANDED)
    assert "message" not in parent["detail"]


def test_bitmap_figure_with_little_text_records_pending_then_selects_both(monkeypatch, tmp_path, fake_soffice):
    _, dmd, _ = _build(monkeypatch, tmp_path, _bitmap_and_little_text_emf())
    parent = next(d for d in _decisions(dmd) if d["reason_code"] == ocr_router.METAFILE_EXPANDED)
    assert "描画待ち" in parent["detail"]["message"]
    assert _selected_reasons(dmd) == {"metafile_embedded_bitmap"} and _calls(fake_soffice) == 0
    assert _pass(dmd)["rendered"] == 1
    assert _selected_reasons(dmd) == {"metafile_embedded_bitmap", ocr_router.METAFILE_RENDERED}


def test_render_child_gets_a_queued_job_even_while_the_refresh_run_is_leased(monkeypatch, tmp_path, fake_soffice):
    import uuid

    from sherpa import store
    from sherpa.ingest import ocr_worker
    from sherpa.store import ocr_jobs

    try:
        store.init_schema()
    except Exception as exc:
        pytest.skip(f"infra down: {exc}")
    world = "test-render-" + uuid.uuid4().hex
    _, dmd, _ = _build(monkeypatch, tmp_path, _vector_emf())
    (dmd / office_md._WORLD_SIG_MARKER).write_text("sig\n", encoding="utf-8")   # sync が公開時に書く署名
    generation = derived_generation.generation_id_for("sig")
    monkeypatch.setenv(office_md._OCR_ENABLED_ENV, "1")
    try:
        ocr_jobs.enqueue_refresh_run(world, generation, ocr_worker.profile_hash())
        assert ocr_jobs.lease_refresh_run("w", world=world) is not None      # 処理中の run（再投入されない）
        result = metafile_render.run_pass(
            [(world, dmd.parent)], lock=lambda _w: contextlib.nullcontext())
        assert result["rendered"] == 1
        child = next(d for d in _decisions(dmd) if d["reason_code"] == ocr_router.METAFILE_RENDERED)
        assert ocr_jobs.status_summary(world, generation)["counts"]["queued"] == 1
        with ocr_jobs._connect() as connection:
            row = connection.execute(
                "SELECT route_input_id FROM ocr_jobs WHERE world=%s", (world,)).fetchone()
        assert row["route_input_id"] == child["route_input_id"]
    finally:
        ocr_jobs.purge_world(world)


def test_figure_with_bitmap_and_enough_text_is_not_rendered(monkeypatch, tmp_path, fake_soffice):
    emf = mb.emf([mb.emf_exttextout_w("これは十分に長い図の中の文字の例です。二行目も続きます", 10, 10),
                  mb.emf_stretchdibits(mb.dib(64, 48))])
    _, dmd, _ = _build(monkeypatch, tmp_path, emf)
    reasons = {d["reason_code"] for d in _decisions(dmd)}
    assert "metafile_embedded_bitmap" in reasons and ocr_router.METAFILE_RENDERED not in reasons
    assert not fake_soffice.exists()


def test_route_refresh_v5_to_v6_records_pending_without_calling_libreoffice(monkeypatch, tmp_path, fake_soffice):
    _, dmd, _ = _build(monkeypatch, tmp_path, _vector_emf())
    route = dmd.parent / "ir" / "a.docx.ocr_route.json"
    old = json.loads(route.read_text(encoding="utf-8"))
    old["router_profile"] = "evidence-raster-router-v5"
    route.write_text(json.dumps(old), encoding="utf-8")
    assert office_md.ocr_route_refresh_needed(dmd) is True
    result = office_md.refresh_ocr_routes(dmd, world="metafile-render-world", generation_id=derived_generation.generation_id_for("sig"))
    assert result["ocr_routes_failed"] == 0 and result["ocr_routes_rewritten"] == 1
    assert _calls(fake_soffice) == 0
    assert any(d["reason_code"] == ocr_router.METAFILE_RENDER_PENDING for d in _decisions(dmd))


@pytest.mark.skipif(not legacy_convert.soffice_available(), reason="LibreOffice が入っていない")
def test_real_libreoffice_renders_a_synthetic_emf(monkeypatch):
    png, reason = legacy_convert.render_metafile_png(_vector_emf(), "emf")
    assert reason is None and png is not None and metafile_text._valid_render(png)
