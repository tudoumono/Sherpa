"""`scripts/check_production_openai_probe.py::probe()`/`env_candidate_status()`。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from check_production_openai_probe import env_candidate_status, probe  # noqa: E402

_SEEDED = {"openai_endpoint_seed_version": 1}
_AZ_URL = "https://myres.openai.azure.com/openai/v1"


def test_db_unreachable_and_no_marker_status_lines():
    def boom():
        raise RuntimeError("connection refused")
    assert probe(boom) == ["DB_UNREACHABLE"]
    assert probe(lambda: {}) == ["NO_MARKER"] and probe(lambda: {"cloud_provider": "openai"}) == ["NO_MARKER"]


@pytest.mark.parametrize("settings,expected", [
    (dict(_SEEDED), ["MARKER_FOUND", "openai", "", "", "-"]),   # 初回シード済みで接続先は本家既定のまま
    ({**_SEEDED, "openai_endpoint_kind": "azure", "openai_base_url": "https://myres.openai.azure.com/openai/deployments/my-secret-deploy"},
     ["MARKER_FOUND", "azure", "https", "myres.openai.azure.com", "-"]),   # path のデプロイ名は出力しない
    ({**_SEEDED, "openai_endpoint_kind": "custom", "openai_base_url": "https://gw.example.com:8443/v1"},
     ["MARKER_FOUND", "custom", "https", "gw.example.com", "8443"]),
    ({**_SEEDED, "openai_endpoint_kind": "custom", "openai_base_url": "https://[2001:db8::1]:8443/v1"},
     ["MARKER_FOUND", "custom", "https", "2001:db8::1", "8443"]),   # IPv6 は角括弧なしの生値
    ({**_SEEDED, "openai_base_url": _AZ_URL}, ["MARKER_FOUND", "azure", "https", "myres.openai.azure.com", "-"]),   # kind 未設定は host から推定
    # 壊れた DB 値は fail（env 候補へ落とさない）: 不正な URL・未知の kind
    ({**_SEEDED, "openai_endpoint_kind": "custom", "openai_base_url": "https://[::1/v1"}, ["DB_ENDPOINT_INVALID"]),
    ({**_SEEDED, "openai_endpoint_kind": "bogus", "openai_base_url": "https://gw.example.com/v1"}, ["DB_ENDPOINT_INVALID"]),
])
def test_probe_with_marker(settings, expected):
    out = probe(lambda: settings)
    assert out == expected
    assert "my-secret-deploy" not in "".join(out)


def test_env_candidate_status():
    def bad_candidate():
        raise ValueError("invalid_endpoint_kind: SHERPA_OPENAI_ENDPOINT_KIND の値が不正です")
    out = env_candidate_status(bad_candidate)
    assert out[0] == "ENV_CANDIDATE_INVALID" and "invalid_endpoint_kind" in out[1]
    assert env_candidate_status(lambda: {}) == ["ENV_CANDIDATE_OK", "openai", "", "", "-"]
    assert env_candidate_status(lambda: {"openai_base_url": _AZ_URL}) == [
        "ENV_CANDIDATE_OK", "azure", "https", "myres.openai.azure.com", "-"]   # DB モードと同じ推定ロジック
    assert env_candidate_status(lambda: {"openai_endpoint_kind": "custom", "openai_base_url": "https://gw.example.com:8443/v1"}) == [
        "ENV_CANDIDATE_OK", "custom", "https", "gw.example.com", "8443"]
