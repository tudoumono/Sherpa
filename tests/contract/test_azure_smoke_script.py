"""`scripts/azure_smoke.py`（Azure OpenAI 実機疎通確認ツール・2026-08-20 作成）の契約テスト。"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import scripts.azure_smoke as azure_smoke
from _ai_env_isolation import AI_ENV_VARS, CODEX_HOME_SENTINEL

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "azure_smoke.py"
PY = sys.executable


def _write_env_file(tmp_path: Path, **kv: str) -> Path:
    p = tmp_path / "azure_test.env"
    p.write_text("\n".join(f"{k}={v}" for k, v in kv.items()) + "\n", encoding="utf-8")
    return p


def _clean_subprocess_env() -> dict[str, str]:
    """`os.environ` から AI 系 env を除いたコピー。"""
    env = {k: v for k, v in os.environ.items() if k not in AI_ENV_VARS}
    env["CODEX_HOME"] = CODEX_HOME_SENTINEL
    return env


def _run(args: list[str], timeout: float = 15.0) -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(SCRIPT), *args], cwd=ROOT,
                          capture_output=True, text=True, timeout=timeout,
                          env=_clean_subprocess_env())


def test_help_dry_run_and_secret_hygiene(tmp_path: Path):
    assert SCRIPT.is_file()
    r = _run(["--help"])
    assert r.returncode == 0 and "終了コード" in r.stdout
    for flag in ("--env-file", "--dry-run", "--only", "--skip", "--vision", "--codex", "--json", "--yes"):
        assert flag in r.stdout, f"--help に {flag} の説明がありません"
    # --dry-run は通信しない（到達不能な TEST-NET-1 でも即終了）・設定の要約と叩く URL を出す・鍵は出さない
    secret = "sk-DUMMY-SECRET-VALUE-should-never-appear-12345"
    env_file = _write_env_file(tmp_path, OPENAI_API_KEY=secret, OPENAI_BASE_URL="https://192.0.2.1/v1",
                               SHERPA_OPENAI_AUTH_HEADER="api-key", OPENAI_EMBED_MODEL="my-embed-deploy",
                               OPENAI_CHAT_MODEL="my-chat-deploy")
    t0 = time.monotonic()
    r = _run(["--env-file", str(env_file), "--dry-run", "--vision", "--codex"], timeout=10.0)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert time.monotonic() - t0 < 5.0, "--dry-run が通信している疑いがあります"
    for needle in ("通信しません", "192.0.2.1", "endpoint_kind", "auth_header", "api_version", "embed_model", "chat_model",
                   "my-embed-deploy", "my-chat-deploy", "chat/completions", "embeddings", "responses", "codex exec"):
        assert needle in r.stdout, needle
    assert secret not in r.stdout + r.stderr
    r = _run(["--env-file", str(env_file), "--dry-run", "--json"])
    assert r.returncode == 0 and secret not in r.stdout + r.stderr
    # 既定の --env-file（.env）が無くても --dry-run は落ちない（OPENAI_BASE_URL 未設定＝OpenAI 本家）
    r = subprocess.run([PY, str(SCRIPT), "--dry-run"], cwd=tmp_path, capture_output=True, text=True, timeout=10.0,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert r.returncode == 0 and "api.openai.com" in r.stdout


def test_unknown_only_name_fails_fast():
    r = _run(["--dry-run", "--only", "bogus-check-name"])
    assert r.returncode == 2 and "bogus-check-name" in (r.stdout + r.stderr)


@pytest.mark.parametrize("env_kv,args,needle", [
    ({"OPENAI_BASE_URL": "https://192.0.2.1/v1"}, [], "--yes"),   # 非対話で --yes も無い＝確認できず中止
    ({"SHERPA_OPENAI_ENDPOINT_KIND": "azure"}, ["--yes"], "通信前"),   # kind=azure なのに base_url が無い
    ({"SHERPA_OPENAI_ENDPOINT_KIND": "custom"}, ["--yes"], "通信前"),
    ({"OPENAI_BASE_URL": "https://192.0.2.1/v1", "SHERPA_OPENAI_ENDPOINT_KIND": "bogus"}, ["--yes"], "通信前"),
    ({"OPENAI_BASE_URL": "https://192.0.2.1/v1", "SHERPA_OPENAI_AUTH_HEADER": "bogus"}, ["--yes"], "通信前"),
])
def test_aborts_before_any_probe(tmp_path: Path, env_kv: dict, args, needle):
    """確認できない／本番と同じ候補 resolver が拒否する組み合わせは、実 API を叩く前に中断する。"""
    env_file = _write_env_file(tmp_path, OPENAI_API_KEY="sk-DUMMY", **env_kv)
    t0 = time.monotonic()
    r = subprocess.run([PY, str(SCRIPT), "--env-file", str(env_file), *args], cwd=ROOT, capture_output=True,
                       text=True, timeout=10.0, stdin=subprocess.DEVNULL, env=_clean_subprocess_env())
    assert r.returncode == 2, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert time.monotonic() - t0 < 5.0 and "・検査中" not in r.stdout
    assert needle in (r.stdout + r.stderr)


def test_script_uses_production_code_and_does_not_reimplement_http():
    src = SCRIPT.read_text(encoding="utf-8")
    for ref in ("from sherpa import agent_constructs", "llm.openai_post_json(", "llm.openai_url(", "embeddings.cfg(",
                "codex_sandbox._write_codex_authoring_config("):
        assert ref in src, ref
    # HTTP 送信は必ず llm.openai_post_json 経由（送信直前ガードを迂回しない）
    assert "urllib.request.Request(" not in src and "urllib.request.urlopen(" not in src and "llm.post_json(" not in src


# 実 Azure は正常応答にも content_filter_results／prompt_filter_results を付ける。
# フィールド名への部分一致ではなく値（filtered / finish_reason / status）で判定する。
_FILTERS_CLEAN = {k: {"filtered": False, "severity": "safe"} for k in ("hate", "self_harm", "sexual", "violence")}
_CHAT_OK = {
    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "OK"},
                 "content_filter_results": _FILTERS_CLEAN}],
    "prompt_filter_results": [{"prompt_index": 0, "content_filter_results": _FILTERS_CLEAN}],
    "usage": {"prompt_tokens": 26, "completion_tokens": 2, "total_tokens": 28},
}
_RESPONSES_OK = {"id": "resp-azure-smoke-test", "object": "response", "model": "gpt-4.1-mini", "status": "completed",
                 "output_text": "OK"}


def test_content_filter_verdicts():
    verdict = azure_smoke._chat_filter_verdict
    choice = _CHAT_OK["choices"][0]
    assert verdict(_CHAT_OK, choice["finish_reason"], "OK") == (False, None)   # 誤検知の回帰固定
    blocked, note = verdict({"choices": [{"finish_reason": "content_filter", "message": {"content": ""},
                                          "content_filter_results": {"hate": {"filtered": True, "severity": "high"}}}]},
                            "content_filter", "")
    assert blocked is True and "content_filter" in note.lower()
    partial = {"choices": [{"finish_reason": "stop", "message": {"content": "OK"},
                            "content_filter_results": {"hate": {"filtered": True, "severity": "low"}}}]}
    blocked, note = verdict(partial, "stop", "OK")
    assert blocked is False and "OK扱い" in note   # 一部フィルタでも本文があれば OK 扱い＋注記
    assert azure_smoke._responses_filter_verdict(_RESPONSES_OK, "completed") == (False, None)
    blocked, note = azure_smoke._responses_filter_verdict(
        {"status": "incomplete", "incomplete_details": {"reason": "content_filter"}}, "incomplete")
    assert blocked is True and "content_filter" in note.lower()
    assert azure_smoke._has_filtered_category(_CHAT_OK) is False
    assert azure_smoke._has_filtered_category({"content_filter_results": {}}) is False
    assert azure_smoke._has_filtered_category({"content_filter_results": {"hate": {"filtered": True}}}) is True


def test_checks_end_to_end_with_azure_shaped_responses(monkeypatch):
    monkeypatch.setattr(azure_smoke, "_do_post", lambda path, body, api_key: (True, _CHAT_OK))
    ok, detail = azure_smoke._check_chat({}, "gpt-4.1-mini", "sk-dummy")
    assert ok is True and "finish_reason=stop" in detail
    monkeypatch.setattr(azure_smoke, "_do_post", lambda path, body, api_key: (True, _RESPONSES_OK))
    ok, detail = azure_smoke._check_responses({}, "gpt-4.1-mini", "sk-dummy")
    assert ok is True and "status=completed" in detail
