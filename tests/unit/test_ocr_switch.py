"""OCR の入切の解決（`sherpa/ingest/ocr_switch.py`）: 既定は切・環境変数の明示が優先・切のあいだワーカーは仕事を取らない。"""
from __future__ import annotations

import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

import pytest  # noqa: E402

from sherpa import store  # noqa: E402
from sherpa.ingest import ocr_switch, office_md  # noqa: E402
from sherpa.store import ocr_jobs  # noqa: E402


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("SHERPA_OCR_ENABLED", raising=False)


def _settings(monkeypatch, value):
    monkeypatch.setattr(store, "get_system_settings", lambda **kw: dict(value))


def test_default_is_off(monkeypatch):
    _settings(monkeypatch, {})
    assert ocr_switch.enabled() is False
    assert office_md.ocr_enabled() is False


def test_setting_turns_it_on_and_only_true_counts(monkeypatch):
    _settings(monkeypatch, {"ocr_enabled": True})
    assert ocr_switch.enabled() is True
    for broken in ("on", 1, "true"):
        _settings(monkeypatch, {"ocr_enabled": broken})
        assert ocr_switch.enabled() is False


@pytest.mark.parametrize("raw", ["0", "off", "garbage"])
def test_env_zero_pins_off_over_a_setting_that_is_on(monkeypatch, raw):
    _settings(monkeypatch, {"ocr_enabled": True})
    monkeypatch.setenv("SHERPA_OCR_ENABLED", raw)
    assert ocr_switch.enabled() is False and ocr_switch.env_override() is False


@pytest.mark.parametrize("raw", [None, "", "  ", "1", "true", "ON"])
@pytest.mark.parametrize("setting", [{}, {"ocr_enabled": False}, {"ocr_enabled": True}])
def test_unset_and_one_follow_the_setting(monkeypatch, raw, setting):
    """`.env.example` から複写された `SHERPA_OCR_ENABLED=1` が、既存の環境で画面の設定を覆わない。"""
    if raw is not None:
        monkeypatch.setenv("SHERPA_OCR_ENABLED", raw)
    _settings(monkeypatch, setting)
    assert ocr_switch.env_override() is None
    assert ocr_switch.enabled() is (setting.get("ocr_enabled") is True)


def test_six_way_table_of_key_output_reservation_and_worker(tmp_path, monkeypatch):
    """環境変数（未設定・0・1）× 全体設定（切・入）。鍵と出力は環境変数だけで決まり、予約とワーカーは入のときだけ動く。"""
    from sherpa.ingest import worker
    from sherpa.ingest import human_md
    monkeypatch.setattr(ocr_jobs, "lease_next", lambda *a, **kw: {"id": 1})
    monkeypatch.setattr(ocr_jobs, "lease_refresh_run", lambda *a, **kw: {"id": 2})
    monkeypatch.setattr(ocr_jobs, "enqueue_refresh_run", lambda *a, **kw: None)
    on, off = f"{human_md.PDF_HUMAN_MD_VERSION};ocr=on", f"{human_md.PDF_HUMAN_MD_VERSION};ocr=off"
    rows = {}
    for env in (None, "0", "1"):
        for setting in (False, True):
            monkeypatch.delenv("SHERPA_OCR_ENABLED", raising=False)
            if env is not None:
                monkeypatch.setenv("SHERPA_OCR_ENABLED", env)
            _settings(monkeypatch, {"ocr_enabled": setting})
            rows[(env, setting)] = (office_md._current_pdf_human_md_sig(), ocr_switch.convert_mode_on(),
                                    worker._enqueue_ocr_refresh("w", "sig") is True,
                                    ocr_switch.lease_next("w") is not None)
    assert rows == {
        (None, False): (on, True, False, False), (None, True): (on, True, True, True),
        ("0", False): (off, False, False, False), ("0", True): (off, False, False, False),
        ("1", False): (on, True, False, False), ("1", True): (on, True, True, True)}


def test_blank_env_falls_back_to_setting(monkeypatch):
    _settings(monkeypatch, {"ocr_enabled": True})
    monkeypatch.setenv("SHERPA_OCR_ENABLED", "  ")
    assert ocr_switch.enabled() is True


def test_unreadable_setting_is_an_error_not_off(monkeypatch):
    def boom(**kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(store, "get_system_settings", boom)
    with pytest.raises(RuntimeError):
        ocr_switch.enabled()
    monkeypatch.setenv("SHERPA_OCR_ENABLED", "0")
    assert ocr_switch.enabled() is False      # 切に固定されていれば設定を読まない


def test_worker_takes_no_job_while_off_and_resumes_when_on(monkeypatch):
    taken = []
    monkeypatch.setattr(ocr_jobs, "lease_next", lambda *a, **kw: taken.append("job") or {"id": 1})
    monkeypatch.setattr(ocr_jobs, "lease_refresh_run", lambda *a, **kw: taken.append("run") or {"id": 2})
    _settings(monkeypatch, {})
    assert ocr_switch.lease_next("w") is None and ocr_switch.lease_refresh_run("w") is None
    assert taken == []
    _settings(monkeypatch, {"ocr_enabled": True})
    assert ocr_switch.lease_next("w") == {"id": 1} and ocr_switch.lease_refresh_run("w") == {"id": 2}
    assert taken == ["job", "run"]


def test_pdf_cache_key_follows_only_the_environment_variable(monkeypatch):
    """鍵は環境変数だけで今までどおり決まる（未設定=on・0=off）。全体設定の入切では変わらない。"""
    from sherpa.ingest import human_md
    on, off = f"{human_md.PDF_HUMAN_MD_VERSION};ocr=on", f"{human_md.PDF_HUMAN_MD_VERSION};ocr=off"
    assert on == "pdf-human-md-v3;ocr=on"
    for setting in ({}, {"ocr_enabled": True}, {"ocr_enabled": False}):
        _settings(monkeypatch, setting)
        assert office_md._current_pdf_human_md_sig() == on         # 未設定
        monkeypatch.setenv("SHERPA_OCR_ENABLED", "1")
        assert office_md._current_pdf_human_md_sig() == on
        monkeypatch.setenv("SHERPA_OCR_ENABLED", "0")
        assert office_md._current_pdf_human_md_sig() == off
        monkeypatch.delenv("SHERPA_OCR_ENABLED")


def test_published_observation_merge_follows_only_the_environment_variable(tmp_path, monkeypatch):
    from sherpa import worlds
    obs = tmp_path / "gen" / "cur"
    obs.mkdir(parents=True)
    monkeypatch.setattr(worlds, "observation_current_dir", lambda w: obs)
    monkeypatch.setattr(worlds, "observation_latest_dir", lambda w: obs)
    for setting in ({}, {"ocr_enabled": True}):
        _settings(monkeypatch, setting)
        assert office_md._resolve_ocr_observation_dir("w") == obs      # 未設定
        monkeypatch.setenv("SHERPA_OCR_ENABLED", "1")
        assert office_md._resolve_ocr_observation_dir("w") == obs
        monkeypatch.setenv("SHERPA_OCR_ENABLED", "0")
        assert office_md._resolve_ocr_observation_dir("w") is None     # 今までどおり
        monkeypatch.delenv("SHERPA_OCR_ENABLED")


def test_conversion_output_and_sigs_do_not_change_with_the_setting(tmp_path, monkeypatch):
    """環境変数が未設定のまま全体設定を切にしても、スキャン PDF の MD・変換キャッシュの鍵・rag の印・派生物の作り直しの判定は変わらない。"""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("a", encoding="utf-8")
    md = tmp_path / "md"
    _settings(monkeypatch, {"ocr_enabled": True})
    office_md.build_derived(str(src), str(md), world_sig="s1")
    office_md.write_ocr_route_sig_marker(md)
    keys_on = (office_md._current_pdf_human_md_sig(), office_md.current_ocr_observation_marker("w"),
               office_md._current_conv_cache_pipeline_sig(ocr_observation_marker=None))
    _settings(monkeypatch, {})
    keys_off = (office_md._current_pdf_human_md_sig(), office_md.current_ocr_observation_marker("w"),
                office_md._current_conv_cache_pipeline_sig(ocr_observation_marker=None))
    assert keys_on == keys_off
    assert office_md.human_md_sig_drift(str(src), md) is False
    assert office_md.ocr_route_refresh_needed(md) is False


def test_scan_pdf_without_ocr_does_not_enqueue_or_confirm(monkeypatch):
    """切のあいだは再実行の予約をせず、ルート版の印も確定しない（入れ直したとき止めていた分から再開できる）。"""
    from sherpa.ingest import worker
    monkeypatch.setenv("SHERPA_OCR_ENABLED", "0")
    assert worker._enqueue_ocr_refresh("w", "sig") is False


def test_run_once_stays_idle_and_never_loads_the_engine_while_off(monkeypatch):
    from types import SimpleNamespace
    from sherpa.ingest import ocr_worker
    leased = []
    monkeypatch.setattr(ocr_jobs, "lease_next", lambda *a, **kw: leased.append(1) or None)
    _settings(monkeypatch, {})
    engine = SimpleNamespace(engine_profile_hash="sha256:x")      # predict を持たない＝呼ばれたら落ちる
    result = ocr_worker.run_once(
        "w", engine=engine, canonical_is_current=lambda *_: True, load_ir=lambda _j: None,
        resolve_source=lambda _j: None, resolve_asset_root=lambda _j: None)
    assert result.status == "idle" and leased == []


def test_switching_back_on_rebuilds_routes_for_everything_made_while_off(tmp_path, monkeypatch):
    """切のあいだに作り直した分（全体の再構築・1 件の再変換）は、入れ直すとルート版の印が無く、止めていた間の分から再開する。"""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("a", encoding="utf-8")
    md = tmp_path / "md"
    _settings(monkeypatch, {"ocr_enabled": True})
    office_md.build_derived(str(src), str(md), world_sig="s1")
    office_md.write_ocr_route_sig_marker(md)
    assert office_md.ocr_route_refresh_needed(md) is False

    _settings(monkeypatch, {})
    assert office_md.ocr_route_refresh_needed(md) is False         # 切のあいだは作り直しを求めない
    assert ocr_switch.routes_wanted(md) is False                   # 1 件の差し替えが対象づくりを飛ばす → 印を消す
    _settings(monkeypatch, {"ocr_enabled": True})
    assert office_md.ocr_route_refresh_needed(md) is True

    office_md.write_ocr_route_sig_marker(md)
    (src / "b.txt").write_text("b", encoding="utf-8")
    _settings(monkeypatch, {})
    office_md.build_derived(str(src), str(md), world_sig="s2")     # 切のまま全体を作り直す
    _settings(monkeypatch, {"ocr_enabled": True})
    assert office_md.ocr_route_refresh_needed(md) is True


def _publish_observation(monkeypatch, tmp_path, world, sig):
    """`.world_sig`=sig の派生物に対して、実際の pointer 付きで観測を 1 世代公開する。"""
    from sherpa.ingest import derived_generation, observation_render
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(tmp_path / "derived"))
    monkeypatch.setenv("SHERPA_OBSERVATION_DIR", str(tmp_path / "observations"))
    from sherpa import worlds
    md = worlds.derived_md_dir(world)
    md.mkdir(parents=True, exist_ok=True)
    (md / ".world_sig").write_text(sig + "\n", encoding="utf-8")
    res = observation_render.publish_snapshot_stream(
        worlds.observation_dir(world), canonical_generation_id=derived_generation.generation_id_for(sig),
        records=[], canonical_is_current=lambda: True)
    assert res.get("status") == "published", res
    return md


def _set_sig(world, sig):
    from sherpa import worlds
    (worlds.derived_md_dir(world) / ".world_sig").write_text(sig + "\n", encoding="utf-8")


def test_off_keeps_published_observation_across_a_source_update(tmp_path, monkeypatch):
    """OCR が切のあいだに資料を更新（.world_sig が進む）しても、公開済みの観測は検索に残り、rag の印は食い違わない。"""
    from sherpa import worlds
    from sherpa.ingest import worker
    _settings(monkeypatch, {})
    md = _publish_observation(monkeypatch, tmp_path, "w", "sig-1")
    before = office_md.current_ocr_observation_marker("w")
    assert before is not None
    office_md.write_rag_sig_marker(md, world="w")
    assert office_md.rag_sig_drift(md, world="w") is False

    _set_sig("w", "sig-2")
    assert worker._enqueue_ocr_refresh("w", "sig-2") is False
    assert worlds.observation_current_dir("w") is None            # 世代が食い違う厳密な見方は入のときだけ
    assert office_md._resolve_ocr_observation_dir("w") is not None
    assert office_md.current_ocr_observation_marker("w") == before
    assert office_md.rag_sig_drift(md, world="w") is False


def test_switching_does_not_change_the_marker_or_drift_while_the_pointer_is_stale(tmp_path, monkeypatch):
    """pointer の世代が古いまま（切で 1 件更新した後など）でも、入切の切り替えだけで rag の印は変わらず、全件の作り直しにならない。"""
    md = _publish_observation(monkeypatch, tmp_path, "w", "sig-1")
    for first, second in (({}, {"ocr_enabled": True}), ({"ocr_enabled": True}, {})):
        _settings(monkeypatch, first)
        _set_sig("w", "sig-1")
        office_md.write_rag_sig_marker(md, world="w")
        _set_sig("w", "sig-2")
        marker = office_md.current_ocr_observation_marker("w")
        assert marker is not None and office_md.rag_sig_drift(md, world="w") is False
        _settings(monkeypatch, second)
        assert office_md.current_ocr_observation_marker("w") == marker
        assert office_md.rag_sig_drift(md, world="w") is False


def test_on_keeps_the_last_published_observation_until_a_new_generation_is_published(tmp_path, monkeypatch):
    """入のときも、新しい世代の観測が公開されるまでは最後に公開した観測を返す（文書ごとのハッシュ照合は読む側）。"""
    from sherpa import worlds
    _settings(monkeypatch, {"ocr_enabled": True})
    _publish_observation(monkeypatch, tmp_path, "w", "sig-1")
    _set_sig("w", "sig-2")
    assert worlds.observation_current_dir("w") is None
    assert office_md._resolve_ocr_observation_dir("w") == worlds.observation_latest_dir("w") is not None
