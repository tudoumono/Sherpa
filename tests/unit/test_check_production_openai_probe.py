"""`scripts/check_production_openai_probe.py::probe()`/`env_candidate_status()`。

`scripts/check-production.sh` の OpenAI 接続先検査を「env 候補モード」／「system_settings 実効値
モード」へ明示的に分離するための判定ロジック。DB・env へは触れない（`get_system_settings`／
`seed_candidate_fn` を差し替え可能な callable として受け取る）ので、実 DB・実 env・subprocess 抜きで
固定できる。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from check_production_openai_probe import env_candidate_status, probe  # noqa: E402


def test_db_unreachable_returns_single_status_line():
    def boom():
        raise RuntimeError("connection refused")

    assert probe(boom) == ["DB_UNREACHABLE"]


def test_no_marker_before_first_boot():
    assert probe(lambda: {}) == ["NO_MARKER"]
    assert probe(lambda: {"cloud_provider": "openai"}) == ["NO_MARKER"]


def test_marker_found_openai_kind_no_base_url():
    """初回シード済みだが接続先は本家既定のまま（env に OPENAI_BASE_URL 等が無かった場合）。"""
    out = probe(lambda: {"openai_endpoint_seed_version": 1})
    assert out == ["MARKER_FOUND", "openai", "", "", "-"]


@pytest.mark.parametrize("bad_value", [{}, [], 0, False])
def test_falsy_non_string_base_url_is_db_endpoint_invalid_when_kind_is_openai(bad_value):
    """実害の回帰固定: kind=openai（明示）でも base_url が falsy な非文字列（`{}`/`[]`/`0`/
    `False`）なら `DB_ENDPOINT_INVALID` になる（`MARKER_FOUND` にはならない）。`llm.
    openai_endpoint_kind()`/`llm.openai_base_url()` の型検査が kind=openai の早期 return より
    先に効く契約を probe() 経由でも固定する。"""
    out = probe(lambda: {
        "openai_endpoint_seed_version": 1,
        "openai_endpoint_kind": "openai",
        "openai_base_url": bad_value,
    })
    assert out == ["DB_ENDPOINT_INVALID"]


@pytest.mark.parametrize("bad_value", [{}, [], 0, False])
def test_falsy_non_string_base_url_is_db_endpoint_invalid_when_kind_unset(bad_value):
    """kind 未設定（推定に委ねる）でも base_url が falsy な非文字列なら `DB_ENDPOINT_INVALID`
    になる。"""
    out = probe(lambda: {
        "openai_endpoint_seed_version": 1,
        "openai_base_url": bad_value,
    })
    assert out == ["DB_ENDPOINT_INVALID"]


def test_marker_found_azure_kind_with_base_url():
    out = probe(lambda: {
        "openai_endpoint_seed_version": 1,
        "openai_endpoint_kind": "azure",
        "openai_base_url": "https://myres.openai.azure.com/openai/deployments/my-secret-deploy",
    })
    assert out[0] == "MARKER_FOUND"
    assert out[1] == "azure"
    assert out[2] == "https"
    assert out[3] == "myres.openai.azure.com"
    assert out[4] == "-"
    # 生の URL（path のデプロイ名を含む）を一切出力しない。
    assert "my-secret-deploy" not in "".join(out)


def test_marker_found_includes_explicit_port():
    out = probe(lambda: {
        "openai_endpoint_seed_version": 1,
        "openai_endpoint_kind": "custom",
        "openai_base_url": "https://gw.example.com:8443/v1",
    })
    assert out == ["MARKER_FOUND", "custom", "https", "gw.example.com", "8443"]


def test_marker_found_malformed_base_url_is_db_endpoint_invalid():
    """base_url が壊れていても probe() 自体は例外を出さないが、DB 実効値は
    `llm.assert_openai_base_url_allowed()` を通すため、host 空へ静かに degrade するのではなく
    固定 `DB_ENDPOINT_INVALID` になる（呼び出し元はこれを env 候補へフォールバックせず
    fail として扱う＝マーカー確定済みで DB が真実源のため）。"""
    out = probe(lambda: {
        "openai_endpoint_seed_version": 1,
        "openai_endpoint_kind": "custom",
        "openai_base_url": "https://[::1/v1",   # 不正な IPv6 リテラル＝urlparse が ValueError
    })
    assert out == ["DB_ENDPOINT_INVALID"]


def test_marker_found_userinfo_in_base_url_is_db_endpoint_invalid():
    out = probe(lambda: {
        "openai_endpoint_seed_version": 1,
        "openai_endpoint_kind": "custom",
        "openai_base_url": "https://user:secret@gw.example.com/v1",
    })
    assert out == ["DB_ENDPOINT_INVALID"]
    assert "secret" not in "".join(out)


def test_marker_found_query_in_base_url_is_db_endpoint_invalid():
    out = probe(lambda: {
        "openai_endpoint_seed_version": 1,
        "openai_endpoint_kind": "custom",
        "openai_base_url": "https://gw.example.com/v1?api-version=2024-01-01",
    })
    assert out == ["DB_ENDPOINT_INVALID"]


def test_marker_found_invalid_port_in_base_url_is_db_endpoint_invalid():
    out = probe(lambda: {
        "openai_endpoint_seed_version": 1,
        "openai_endpoint_kind": "custom",
        "openai_base_url": "https://gw.example.com:notaport/v1",
    })
    assert out == ["DB_ENDPOINT_INVALID"]


def test_marker_found_http_scheme_in_base_url_is_db_endpoint_invalid():
    """https 以外の scheme も runtime validator が拒否する（preflight 独自の甘い判定を持たない）。"""
    out = probe(lambda: {
        "openai_endpoint_seed_version": 1,
        "openai_endpoint_kind": "custom",
        "openai_base_url": "http://gw.example.com/v1",
    })
    assert out == ["DB_ENDPOINT_INVALID"]


def test_marker_found_ipv6_base_url_returns_raw_host_without_brackets():
    """`host` は接続用の生値（角括弧なし）を返す。`check-production.sh` はこの値を
    `getent`／`/dev/tcp/<host>/<port>` へそのまま渡すため、角括弧を含むと正当な IPv6 接続先が
    名前解決に失敗して hard fail していた（表示用の角括弧付けは呼び出し側 bash の責務・
    `_check_openai_endpoint_host` の `display_host` を参照）。"""
    out = probe(lambda: {
        "openai_endpoint_seed_version": 1,
        "openai_endpoint_kind": "custom",
        "openai_base_url": "https://[2001:db8::1]:8443/v1",
    })
    assert out == ["MARKER_FOUND", "custom", "https", "2001:db8::1", "8443"]


def test_marker_found_azure_kind_with_empty_base_url_is_db_endpoint_invalid():
    """kind=azure だが base_url が空（不整合な/改ざんされた DB 状態）
    の場合、`llm.openai_base_url()` の fail-safe（本家既定 URL への縮退）を経由した後の値ではなく
    **生の kind/base** を検証するため、本家既定へ縮退して `MARKER_FOUND`(openai 相当) を誤って
    返すのではなく `DB_ENDPOINT_INVALID` になる。"""
    out = probe(lambda: {
        "openai_endpoint_seed_version": 1,
        "openai_endpoint_kind": "azure",
        "openai_base_url": None,
    })
    assert out == ["DB_ENDPOINT_INVALID"]


def test_marker_found_custom_kind_with_missing_base_url_key_is_db_endpoint_invalid():
    out = probe(lambda: {
        "openai_endpoint_seed_version": 1,
        "openai_endpoint_kind": "custom",
    })
    assert out == ["DB_ENDPOINT_INVALID"]


def test_marker_found_azure_base_without_explicit_kind_infers_from_host():
    """マーカー確定済みで `openai_endpoint_kind` が未設定（例: 古い保存値・
    admin が明示選択していない）でも、`openai_base_url` が Azure のホストなら runtime と同じ
    ロジック（`llm.openai_endpoint_kind()`）で "azure" と推定する（kind 未設定を無条件で
    "openai" 扱いにはしない＝実際の runtime 挙動と一致させる）。"""
    out = probe(lambda: {
        "openai_endpoint_seed_version": 1,
        "openai_base_url": "https://myres.openai.azure.com/openai/v1",
    })
    assert out == ["MARKER_FOUND", "azure", "https", "myres.openai.azure.com", "-"]


def test_marker_found_unknown_kind_with_no_base_url_is_db_endpoint_invalid():
    """`openai_endpoint_kind()` は未知の生 kind でも base 未設定なら
    無条件で "openai" へ縮退するため、生の kind をチェックせずに正規化後の値だけを見ると
    `kind="bogus"` という DB の破損値を見逃して `MARKER_FOUND`（openai・本家）を返してしまう。
    非空の生 kind は openai/azure/custom の列挙として先に検証する。"""
    out = probe(lambda: {
        "openai_endpoint_seed_version": 1,
        "openai_endpoint_kind": "bogus",
    })
    assert out == ["DB_ENDPOINT_INVALID"]


def test_marker_found_unknown_kind_with_valid_base_url_is_db_endpoint_invalid():
    """未知 kind は base_url が有効な URL であっても拒否する（kind 自体が不正なため、base の
    有無・妥当性に関わらず DB_ENDPOINT_INVALID）。"""
    out = probe(lambda: {
        "openai_endpoint_seed_version": 1,
        "openai_endpoint_kind": "bogus",
        "openai_base_url": "https://gw.example.com/v1",
    })
    assert out == ["DB_ENDPOINT_INVALID"]


# ===== env_candidate_status（本番の起動時シード resolver を共有） =====

def test_env_candidate_invalid_kind_reports_reason_without_raising():
    def bad_candidate():
        raise ValueError("invalid_endpoint_kind: SHERPA_OPENAI_ENDPOINT_KIND の値が不正です")

    out = env_candidate_status(bad_candidate)
    assert out[0] == "ENV_CANDIDATE_INVALID"
    assert "invalid_endpoint_kind" in out[1]


def test_env_candidate_default_when_no_base_url():
    out = env_candidate_status(lambda: {})
    assert out == ["ENV_CANDIDATE_OK", "openai", "", "", "-"]


def test_env_candidate_azure_base_without_explicit_kind_infers_from_host():
    """マーカー未確定（env 候補モード）でも DB モードと同じ推定ロジックを共有する
    （`_openai_endpoint_seed_candidate` は kind 未指定を候補へ書き込まない設計＝
    `llm.openai_endpoint_kind()` の読み取り時フォールバックへ委ねる）。"""
    out = env_candidate_status(lambda: {"openai_base_url": "https://myres.openai.azure.com/openai/v1"})
    assert out == ["ENV_CANDIDATE_OK", "azure", "https", "myres.openai.azure.com", "-"]


def test_env_candidate_explicit_kind_is_respected():
    out = env_candidate_status(lambda: {
        "openai_endpoint_kind": "custom",
        "openai_base_url": "https://gw.example.com:8443/v1",
    })
    assert out == ["ENV_CANDIDATE_OK", "custom", "https", "gw.example.com", "8443"]
