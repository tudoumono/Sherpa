from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
from unittest import mock

import yaml

from sherpa.ingest import ai_observation, evidence_ir as IR, observation_render, ocr_router, ocr_worker
from sherpa.store import db as store_db


ROOT = Path(__file__).resolve().parents[2]


# OCR worker（docker/ocr/Dockerfile が実際にイメージへ install/COPY するもの）の実行に
# 効くファイルだけを束縛する。以前は requirements.txt・constraints.txt・.dockerignore を
# 丸ごとハッシュしていたため、OCR に無関係な依存追加（例: 性能台帳#17 QW2 の
# `psycopg_pool` 追加とは別件の core 依存バンプ）でも赤くなっていた（2026-09-03 是正）。
#   - requirements.txt / constraints.txt はアプリ本体全体の依存を含むが、ocr_worker.py が
#     実際に import するのは（sherpa/store/db.py 経由の）psycopg[_pool] のみ（fastapi・
#     anthropic・boto3 等は import されない）。よって psycopg 関連行だけを抜き出して束縛する。
#   - docker-compose.yml は ocr-worker サービス定義と、その worker が参照する network 定義
#     だけを対象にする（例: elasticsearch のイメージタグ変更のような無関係な差分では
#     赤くならない。過去に手作業の amendments で個別に免責していたのをこの絞り込みで解消）。
#   - .dockerignore は束縛から外す（test_ocr_image_labels_package_inventory_honestly_...
#     が data/.env* 除外を毎回ライブ検証しており、それ以外の内容は OCR イメージの
#     実行環境に影響しない）。
#   - sherpa/store/db.py は丸ごとではなく OCR が実際に踏む面だけを束縛する（下記
#     `_db_ocr_relevant_source` 参照・2026-09-03 是正 CLEAN-2 item e）。ocr_worker.py が
#     `world_lock` を、ocr_jobs.py が `_connect`/`_ensure` を import しており、性能台帳#17 QW2 の
#     PG プール化はこのファイルに実装されている＝OCR worker の DB 接続挙動に実際に効くが、
#     db.py 全文には会話/監査等の無関係テーブルの DDL・関数も同居しており、丸ごとハッシュだと
#     それらの変更でも赤くなっていた。
OCR_BOUND_FULL_FILES = (
    "docker/ocr/Dockerfile",
    "requirements-ocr.txt",
    "constraints-ocr.txt",
    "docker/ocr-models.lock.json",
    "sherpa/ingest/ai_observation.py",
    "sherpa/ingest/ocr_worker.py",
    "sherpa/ingest/ocr_router.py",
    "sherpa/ingest/observation_render.py",
    "sherpa/store/ocr_jobs.py",
)


def _psycopg_related_lines(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip().lower().startswith("psycopg")]
    return "\n".join(lines)


# `sherpa/store/db.py` のうち OCR worker/ocr_jobs.py が実際に踏む面だけを抽出する
# （requirements.txt の「関連行だけ抜き出す」方式の関数/DDL 単位版）。
#   - `world_lock`（`ocr_worker.py` が import）・`_connect`/`_ensure`（`ocr_jobs.py` が import）と、
#     それぞれが直接使う補助（`_dsn`/`_world_lock_key`/PG プール一式/`init_schema`）を
#     `inspect.getsource()` で個別に抜き出す——`init_schema` 自体は他テーブルの移行処理
#     （`_migrate_client_op_id_unique_index`/`_ensure_messages_created_at_index_background`）を
#     **呼び出す**が、それらの**中身**は関数呼び出し1行としてしか現れないため、その中身が変わっても
#     ここでは検知しない（意図的な妥協＝会話/監査系の変更を無関係のまま保つのが目的）。
#   - `_ensure`→`init_schema` が実行する `_SCHEMA`（DB 全表の DDL リスト）は丸ごとではなく、
#     OCR 関連テーブル名を含む要素だけを抜き出す（`_DB_OCR_SCHEMA_MARKERS`）。
# 折衷案（关数単位が脆すぎる場合の代替）は採らなかった: `_SCHEMA`/対象関数はいずれも独立した
# トップレベル定義（プール専用の定数・クラスも `_get_pg_pool`/`_make_pg_pool`/`_PooledConnection`
# として関数/クラス単位に閉じている）ため、名前ベースの関数単位抽出で十分に安定する
# （db.py 内でこれらの定義の**中身**が変わらない限り、周辺へのコード追加では変化しない）。
_DB_OCR_RELEVANT_CALLABLES = (
    "_dsn", "_world_lock_key", "world_lock", "_get_pg_pool", "_make_pg_pool",
    "_close_pg_pool_best_effort", "PooledConnectionReleasedError", "_PooledConnection", "_connect",
    "_ensure", "init_schema",
)
_DB_OCR_SCHEMA_MARKERS = ("ocr_jobs", "ocr_refresh_runs", "ocr_result_cache", "ocr_worker_heartbeats")


def _db_ocr_relevant_source() -> str:
    parts = [inspect.getsource(getattr(store_db, name)) for name in _DB_OCR_RELEVANT_CALLABLES]
    schema_subset = [stmt for stmt in store_db._SCHEMA
                     if any(marker in stmt for marker in _DB_OCR_SCHEMA_MARKERS)]
    parts.append("\n".join(schema_subset))
    return "\n".join(parts)


def _ocr_worker_compose_subtree(compose: dict) -> dict:
    worker = compose["services"]["ocr-worker"]
    networks = {name: compose["networks"][name] for name in worker.get("networks", [])}
    return {"services": {"ocr-worker": worker}, "networks": networks}


def compute_ocr_implementation_binding(root: Path) -> dict[str, str]:
    """OCR compose-profile 測定が束縛すべきファイル群のハッシュ一式。

    測定の契約テストと再測定手順の両方がこの1つの定義を使うことで、束縛対象の定義が
    テスト側と生成側でズレないようにする。
    """
    files = {
        relative: hashlib.sha256((root / relative).read_bytes()).hexdigest()
        for relative in OCR_BOUND_FULL_FILES
    }
    files["sherpa/store/db.py:ocr-relevant"] = hashlib.sha256(
        _db_ocr_relevant_source().encode("utf-8")
    ).hexdigest()
    psycopg_lines = "\n".join(
        _psycopg_related_lines((root / name).read_text(encoding="utf-8"))
        for name in ("requirements.txt", "constraints.txt")
    )
    files["requirements.txt+constraints.txt:psycopg-lines"] = hashlib.sha256(
        psycopg_lines.encode("utf-8")
    ).hexdigest()
    compose = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))
    subtree = _ocr_worker_compose_subtree(compose)
    canonical = json.dumps(subtree, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    files["docker-compose.yml:services.ocr-worker+networks"] = hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()
    return files


def test_e16_schema_and_fixed_profiles_are_public_contracts():
    assert ai_observation.AI_OBSERVATION_SCHEMA_VERSION == "ai-observation-set-v1alpha2"
    assert ocr_router.OCR_ROUTE_SCHEMA_VERSION == "ocr-route-manifest-v1"
    assert ocr_router.OCR_ROUTER_PROFILE == "evidence-raster-router-v3"
    assert ocr_router.ocr_route_sig_value().startswith("sha256:")
    assert ocr_router.PAGE_RENDER_PROFILE == {
        "renderer": "pypdfium2",
        "profile": "pdf-page-render-pypdfium2-200dpi-rgb-png-v1",
        "dpi": 200,
        "color_space": "RGB",
        "format": "png",
        "alpha_background": "#FFFFFF",
    }
    assert ocr_worker.PADDLE_CPU_PROFILE.paddleocr_version == "3.7.0"
    assert ocr_worker.PADDLE_CPU_PROFILE.paddlepaddle_version == "3.3.0"
    assert ocr_worker.PADDLE_CPU_PROFILE.pypdfium2_version == "5.11.0"
    assert ocr_worker.PADDLE_CPU_PROFILE.pillow_version == "12.3.0"
    assert ocr_worker.PADDLE_CPU_PROFILE.enable_mkldnn is False
    assert ocr_worker.PADDLE_CPU_PROFILE.runtime_model_download is False


def test_core_requirements_do_not_install_paddle_and_worker_requirements_are_pinned():
    core = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    worker = (ROOT / "requirements-ocr.txt").read_text(encoding="utf-8").lower()
    worker_constraints = (ROOT / "constraints-ocr.txt").read_text(encoding="utf-8").lower()
    assert "paddleocr" not in core and "paddlepaddle" not in core
    assert "paddleocr==3.7.0" in worker
    assert "paddlepaddle==3.3.0" in worker
    assert "-c constraints-ocr.txt" in worker
    assert "numpy==2.3.5" in worker_constraints
    assert "paddlex==3.7.2" in worker_constraints
    assert "numpy==2.5.1" not in worker_constraints
    assert "pymupdf" not in worker

    model_lock = json.loads((ROOT / "docker/ocr-models.lock.json").read_text(encoding="utf-8"))
    locked = {item["name"]: item["tree_sha256"] for item in model_lock["models"]}
    assert locked == {
        ocr_worker.PADDLE_CPU_PROFILE.detection_model:
            ocr_worker.PADDLE_CPU_PROFILE.detection_model_tree_sha256,
        ocr_worker.PADDLE_CPU_PROFILE.recognition_model:
            ocr_worker.PADDLE_CPU_PROFILE.recognition_model_tree_sha256,
    }
    assert model_lock["runtime_download_allowed"] is False


def test_ocr_compose_profile_has_no_external_network_and_only_observation_is_writable():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    worker = compose["services"]["ocr-worker"]
    assert compose["networks"]["ocr-internal"]["internal"] is True
    assert worker["networks"] == ["ocr-internal"]
    assert worker["read_only"] is True
    mounts = {item["target"]: item for item in worker["volumes"] if isinstance(item, dict)}
    assert mounts["/derived"]["read_only"] is True
    assert "${PWD}" not in mounts
    world_mount = "${SHERPA_OCR_WORLD_ROOT:-/__sherpa_ocr_world_root_must_be_configured__}"
    assert mounts[world_mount]["read_only"] is True
    assert mounts[world_mount]["bind"]["create_host_path"] is False
    assert worker["environment"]["SHERPA_OCR_WORLD_ROOT"] == "${SHERPA_OCR_WORLD_ROOT:-}"
    assert "/mnt" not in (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert mounts["/models"]["read_only"] is True
    assert mounts["/observations"].get("read_only") is not True


def test_ocr_image_labels_package_inventory_honestly_and_requires_real_sbom_before_distribution():
    dockerfile = (ROOT / "docker/ocr/Dockerfile").read_text(encoding="utf-8")
    notice = (ROOT / "docker/ocr/NOTICE.md").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "python-package-inventory.json" in dockerfile
    assert "sbom-python.json" not in dockerfile
    assert "not a CycloneDX/SPDX SBOM" in notice
    assert "real SBOM" in notice
    assert "data" in dockerignore
    assert ".env.*" in dockerignore


def test_fixed_compose_measurement_is_bound_to_current_worker_implementation():
    # 2026-08-16実測は履歴として fixtures に残す（削除しない）。契約テストが束縛するのは
    # 常に最新の再測定（2026-09-03・O2 是正で束縛スコープを絞り込んだ上で再測定）。
    measurement = json.loads((
        ROOT / "fixtures/eval/ocr_ja/measurements/2026-09-03-compose-profile-summary.json"
    ).read_text(encoding="utf-8"))
    assert measurement["production_data_used"] is False
    assert measurement["implementation"]["engine_profile_hash"] == ocr_worker.profile_hash()
    expected_files = compute_ocr_implementation_binding(ROOT)
    for relative, expected in expected_files.items():
        assert measurement["implementation"]["files"].get(relative) == expected, relative
    assert set(measurement["implementation"]["files"]) == set(expected_files)
    assert measurement["runtime"]["heartbeat_available"] is True
    assert measurement["runtime"]["model_hashes_valid"] is True
    assert measurement["mount_checks"]["write_to_world_root_rejected"] is True
    assert measurement["mount_checks"]["write_to_canonical_derived_rejected"] is True
    assert measurement["mount_checks"]["write_to_observation_root_succeeded"] is True
    assert measurement["network_checks"]["outbound_ipv4_connection_rejected"] is True
    assert measurement["image_inventory"]["real_sbom_complete"] is False
    assert measurement["image_inventory"]["legal_review_complete"] is False


def test_route_manifest_does_not_create_generation_id_digest_cycle():
    fields = set(ocr_router.OCRRouteManifest.__dataclass_fields__)
    assert "canonical_generation_id" not in fields
    assert "source_content_hash" in fields
    assert "source_rel_path" in fields


def test_observation_artifact_names_are_separate_from_canonical_rag(tmp_path):
    paths = observation_render.artifact_paths(
        tmp_path, canonical_generation_id="a" * 64, observation_generation_id="b" * 64,
        source_rel_path="sub/design.xlsx",
    )
    assert paths.observation_sets_jsonl.name == "design.xlsx.ai_observations.jsonl"
    assert "/md-generations/" not in paths.observation_sets_jsonl.as_posix()
    assert f"/{observation_render.OBSERVATION_GENERATIONS_NAME}/" in paths.observation_sets_jsonl.as_posix()
    assert observation_render.OBSERVATION_POINTER_NAME == "md-observations.current.json"
    assert observation_render.OBSERVATION_POINTER_SCHEMA == "sherpa-observation-pointer-v1"


def test_db_ocr_relevant_source_ignores_unrelated_schema_changes():
    """item e: db.py 束縛の絞り込み確認——`_SCHEMA` に無関係テーブル（会話/監査系を模したダミー）の
    DDL を足してもハッシュ対象の抽出結果は変化しない（OCR 関連テーブル名を含む要素だけを抜き出す
    ため）。実際の DB へは触れない純関数のテスト。"""
    baseline = _db_ocr_relevant_source()
    extra_schema = [*store_db._SCHEMA, "CREATE TABLE IF NOT EXISTS unrelated_audit_log (id BIGSERIAL PRIMARY KEY)"]
    with mock.patch.object(store_db, "_SCHEMA", extra_schema):
        assert _db_ocr_relevant_source() == baseline


def test_db_ocr_relevant_source_reacts_to_ocr_schema_changes():
    """対照実験: OCR 関連テーブル（`ocr_jobs`）の DDL 変更には反応する（絞り込みが空振りしていない
    ことの確認・上のテストと対）。"""
    baseline = _db_ocr_relevant_source()
    extra_schema = [*store_db._SCHEMA, "ALTER TABLE ocr_jobs ADD COLUMN IF NOT EXISTS extra_marker TEXT"]
    with mock.patch.object(store_db, "_SCHEMA", extra_schema):
        assert _db_ocr_relevant_source() != baseline


def test_db_ocr_relevant_source_reacts_to_world_lock_body_changes():
    """item e: `world_lock`（ocr_worker.py が import）自体の実装が変わればハッシュも変わる
    （絞り込みが「一切反応しない」退化をしていないことの確認）。"""
    def _different_world_lock(world_id, *, timeout_ms=None):
        """このテストだけの別実装（本物とは docstring/本体が異なる）。"""
        raise NotImplementedError

    baseline = _db_ocr_relevant_source()
    with mock.patch.object(store_db, "world_lock", _different_world_lock):
        assert _db_ocr_relevant_source() != baseline


def test_retired_observation_render_symbols_are_gone():
    """O1（2026-09-03）で検索専用描画（Markdown/chunk JSONL）を撤去した——CLAUDE.md 退役リスト参照。
    `observation_render.py` は `.ai_observations.jsonl`（`office_md._load_ocr_observation_sets` が
    読む Observation Set 本体）の永続化・世代管理だけを残す。"""
    for retired in (
        "render", "render_many", "RenderedObservations", "write_markdown_atomic",
        "write_chunks_atomic", "OBSERVATION_CHUNK_SCHEMA",
    ):
        assert not hasattr(observation_render, retired), retired
    fields = set(observation_render.ObservationArtifactPaths.__dataclass_fields__)
    assert fields == {"generation_root", "observation_sets_jsonl"}


def test_publish_snapshot_stream_only_persists_observation_set_jsonl(tmp_path):
    """撤去の実効性確認: 実際に1件publishしても、生成generationには Observation Set 本体と
    世代manifestだけが残り、`.rag_observations.md`/`.rag_observation_chunks.jsonl` は出ない。"""
    source = IR.EvidenceSource(file_type="xlsx", content_hash="sha256:" + "11" * 32)
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
        locator=locator, coverage_id=coverage_id, extension={"asset_sha256": "sha256:" + "22" * 32},
    )
    ir = IR.EvidenceIR(
        schema_version=IR.EVIDENCE_IR_SCHEMA_VERSION, parser_profile=IR.EVIDENCE_PARSER_PROFILE,
        source=source, elements=[element], coverage=[coverage],
    )
    generation = "a" * 64
    observation_set = ai_observation.build(
        ir=ir, provider="paddleocr", model="m1", execution_mode="local",
        prompt_schema_version="v1", preprocessing_profile="p1",
        raw_response="text", canonical_generation_id=generation,
        inputs=[{"input_id": "in1", "target_evidence_id": "pic1", "asset_sha256": "sha256:" + "22" * 32,
                "media_type": "image/png", "input_kind": "asset"}],
        observations=[{"input_id": "in1", "kind": "ocr_text", "text": "OCR済み文字", "confidence": 0.9,
                       "searchable": True, "use_for_answer": True}],
    )
    record = observation_render.ObservationRecord(
        source_rel_path="sub/design.xlsx", ir=ir, observation_set=observation_set,
    )
    result = observation_render.publish_snapshot_stream(
        tmp_path, canonical_generation_id=generation, records=[record],
        canonical_is_current=lambda: True,
    )
    assert result["status"] == "published"
    active = observation_render.active_observation_dir(tmp_path, canonical_generation_id=generation)
    assert active is not None
    files = {path.name for path in active.rglob("*") if path.is_file()}
    assert files == {observation_render.OBSERVATION_GENERATION_MANIFEST, "design.xlsx.ai_observations.jsonl"}


def test_database_contract_has_lease_token_generation_binding_and_world_cache():
    ddl = "\n".join(store_db._SCHEMA)
    assert "CREATE TABLE IF NOT EXISTS ocr_jobs" in ddl
    assert "lease_token TEXT" in ddl
    assert "canonical_generation_id TEXT NOT NULL" in ddl
    assert "UNIQUE (world, canonical_generation_id, route_input_id, engine_profile_hash)" in ddl
    assert "CREATE TABLE IF NOT EXISTS ocr_result_cache" in ddl
    assert "PRIMARY KEY (world, input_fingerprint, engine_profile_hash)" in ddl
    assert "CREATE TABLE IF NOT EXISTS ocr_worker_heartbeats" in ddl
    assert "engine_profile_hash TEXT NOT NULL" in ddl
    assert "CREATE TABLE IF NOT EXISTS ocr_refresh_runs" in ddl
    assert "cursor_rel_path TEXT" in ddl
    assert "CHECK (status IN ('queued','leased','completed','failed','cancelled'))" in ddl
