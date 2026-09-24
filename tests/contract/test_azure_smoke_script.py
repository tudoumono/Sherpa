"""`scripts/azure_smoke.py`（Azure OpenAI 実機疎通確認ツール・2026-08-20 作成）の契約テスト。

背景: 2026-08-18 に Azure OpenAI 対応（`OPENAI_BASE_URL` 等で接続先を設定化）を実装したが、
実 Azure での疎通は未検証だった。`scripts/azure_smoke.py` は、実 Azure アカウントを持つ利用者が
「何が通って何が落ちるか」を一発で確かめる道具（同じものを先方環境の受け入れ確認にも使う）。

**このテストは実 API を一切叩かない**（`--dry-run` と、TEST-NET-1（RFC 5737・到達不能想定）の
`192.0.2.1` を使った「通信していないことの確認」だけ）。実 `.env`／実 `~/.codex/auth.json` にも
触れない（`--env-file` に一時ファイルを渡す・`CODEX_HOME` 等は subprocess env で隔離する）。

検査するのは:
  - `--dry-run` が通信せずに設定一覧（base_url・endpoint_kind・auth_header・api_version の有無・
    embed_model・chat_model）と「これから叩く URL」一覧を出し、終了コード 0 で終わること。
    「通信していない」ことは、到達不能アドレス（192.0.2.1・TEST-NET-1）を base_url にしても
    ほぼ即座に終了する（TCP connect のタイムアウトまで待たない）ことで確認する
    （tests/contract/test_check_production_openai_base_url.py の同種手法を踏襲）。
  - `--help` が主要な引数（--env-file・--dry-run・--only・--skip・--vision・--codex・--json・--yes）
    を説明していること。
  - スクリプトのソースが `sherpa.llm`／`sherpa.embeddings`／`sherpa.agentic_search`／
    `sherpa.providers.openai.OpenAIProvider`／`sherpa.providers.codex.sandbox` を実際に import し、
    それぞれの本番関数/メソッドを呼んでいることを静的に固定する（＝HTTP 等の再実装をしていない
    ことの証跡。CLAUDE.md の「本番コードをそのまま呼ぶ」契約に対応）。
  - ダミーの秘密（API キー）を `--env-file` 経由で与えても、`--dry-run` の出力にその値が
    一切現れないこと。
  - コンテンツフィルタ誤検知の回帰固定（2026-08-21・実 Azure `gpt-4.1-mini` で発覚）:
    旧実装は成功応答（HTTP 200）の JSON 全体を文字列化して "content_filter" を検索していたため、
    Azure が正常応答にも必ず付与する `content_filter_results`／`prompt_filter_results`
    （フィルタを通した結果の記録＝キー名自体に一致）にヒットし、`finish_reason=stop` で本文が
    正しく返っている完全な成功まで NG と誤判定していた。ここでは実 Azure が返した形に近い
    応答 JSON を `_chat_filter_verdict`/`_responses_filter_verdict`（純関数）に直接渡して固定する
    （ネットワークは使わない）。
"""
from __future__ import annotations

import os
import subprocess
import sys
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
    """`os.environ` から AI 系 env を除いたコピー。子プロセスへ明示的に渡す（開発機の実シェル
    環境が `--env-file` で指定したテスト用の値を `azure_smoke.py::_apply_env_file` の
    「既に設定済みなら上書きしない」判定より先に上書きしてしまうのを防ぐ）。"""
    env = {k: v for k, v in os.environ.items() if k not in AI_ENV_VARS}
    env["CODEX_HOME"] = CODEX_HOME_SENTINEL
    return env


def _run(args: list[str], timeout: float = 15.0) -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(SCRIPT), *args], cwd=ROOT,
                          capture_output=True, text=True, timeout=timeout,
                          env=_clean_subprocess_env())


def test_script_exists():
    assert SCRIPT.is_file(), "scripts/azure_smoke.py が存在しません"


def test_help_explains_arguments():
    r = _run(["--help"])
    assert r.returncode == 0
    out = r.stdout
    for flag in ("--env-file", "--dry-run", "--only", "--skip", "--vision", "--codex", "--json", "--yes"):
        assert flag in out, f"--help に {flag} の説明がありません"
    # 終了コードの意味も書いてあること（受け入れ確認で読む前提）。
    assert "終了コード" in out


def test_dry_run_does_not_communicate_and_exits_zero(tmp_path: Path):
    """base_url を到達不能アドレス（TEST-NET-1）にしても、--dry-run はほぼ即座に終わる
    （実際に TCP connect を試みていれば、少なくとも数秒はタイムアウト待ちになるはず）。"""
    env_file = _write_env_file(
        tmp_path,
        OPENAI_API_KEY="sk-DUMMY-SECRET-VALUE-12345",
        OPENAI_BASE_URL="https://192.0.2.1/v1",
        SHERPA_OPENAI_AUTH_HEADER="api-key",
        OPENAI_EMBED_MODEL="my-embed-deploy",
        OPENAI_CHAT_MODEL="my-chat-deploy",
    )
    import time
    t0 = time.monotonic()
    r = _run(["--env-file", str(env_file), "--dry-run", "--vision", "--codex"], timeout=10.0)
    elapsed = time.monotonic() - t0
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert elapsed < 5.0, f"--dry-run が {elapsed:.1f}s かかった＝通信している疑いがあります"
    out = r.stdout
    assert "通信しません" in out
    assert "192.0.2.1" in out
    assert "endpoint_kind" in out
    assert "auth_header" in out
    assert "api_version" in out
    assert "embed_model" in out
    assert "chat_model" in out
    assert "my-embed-deploy" in out
    assert "my-chat-deploy" in out
    # これから叩く URL 一覧（chat/completions・embeddings・responses・codex）が出ていること。
    assert "chat/completions" in out
    assert "embeddings" in out
    assert "responses" in out
    assert "codex exec" in out


def test_dry_run_default_env_file_does_not_require_real_env(tmp_path: Path):
    """既定の --env-file（.env）が無いカレントディレクトリでも --dry-run は落ちない
    （env ファイルが無ければ何も取り込まないだけ・OPENAI_BASE_URL 未設定＝OpenAI 本家）。"""
    r = subprocess.run([PY, str(SCRIPT), "--dry-run"], cwd=tmp_path,
                       capture_output=True, text=True, timeout=10.0,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "api.openai.com" in r.stdout


def test_secret_not_leaked_in_dry_run_output(tmp_path: Path):
    secret = "sk-DUMMY-SECRET-VALUE-should-never-appear-12345"
    env_file = _write_env_file(
        tmp_path,
        OPENAI_API_KEY=secret,
        OPENAI_BASE_URL="https://my-resource.openai.azure.com/openai/v1/",
    )
    r = _run(["--env-file", str(env_file), "--dry-run", "--json"])
    assert r.returncode == 0
    assert secret not in r.stdout
    assert secret not in r.stderr


def test_unknown_only_name_fails_fast():
    r = _run(["--dry-run", "--only", "bogus-check-name"])
    assert r.returncode == 2
    assert "bogus-check-name" in (r.stdout + r.stderr)


def test_non_interactive_without_yes_aborts_without_network(tmp_path: Path):
    """--dry-run を付けず --yes も無い非対話実行は、実 API を叩く前に確認できず中止する
    （検査本体が一切走らない＝到達不能アドレスでもすぐ終わる）。"""
    env_file = _write_env_file(
        tmp_path,
        OPENAI_API_KEY="sk-DUMMY",
        OPENAI_BASE_URL="https://192.0.2.1/v1",
    )
    import time
    t0 = time.monotonic()
    r = subprocess.run([PY, str(SCRIPT), "--env-file", str(env_file)], cwd=ROOT,
                       capture_output=True, text=True, timeout=10.0, stdin=subprocess.DEVNULL,
                       env=_clean_subprocess_env())
    elapsed = time.monotonic() - t0
    assert r.returncode == 2
    assert elapsed < 5.0, f"確認せず {elapsed:.1f}s かかった＝検査が走った疑いがあります"
    assert "--yes" in (r.stdout + r.stderr)


@pytest.mark.parametrize("env_kv", [
    # kind=azure なのに base_url が無い＝本番の assert_openai_endpoint_consistent と同じ拒否。
    {"SHERPA_OPENAI_ENDPOINT_KIND": "azure"},
    # kind=custom なのに base_url が無い。
    {"SHERPA_OPENAI_ENDPOINT_KIND": "custom"},
    # 未知の kind。
    {"OPENAI_BASE_URL": "https://192.0.2.1/v1", "SHERPA_OPENAI_ENDPOINT_KIND": "bogus"},
    # 未知の auth_header。
    {"OPENAI_BASE_URL": "https://192.0.2.1/v1", "SHERPA_OPENAI_AUTH_HEADER": "bogus"},
])
def test_invalid_endpoint_candidate_aborts_before_any_probe(tmp_path: Path, env_kv: dict):
    """本番と同じ候補 resolver（`sherpa.api._openai_endpoint_seed_candidate`）が
    通信前に拒否する組み合わせは、`--dry-run` を付けなくても実 API を一切叩かず即座に中断する
    （未知 kind/auth_header や base_url 欠落を含めて検証する契約＝自前の host 推定だけに頼って
    誤った接続先のまま probe へ進むことはない）。`--yes` を付けても検査が1件も走らないことを、
    「・検査中」ログが1行も出ないこと・ほぼ即座に終了すること（到達不能アドレスの TCP connect
    タイムアウトを待っていない）の両方で確認する。"""
    env_file = _write_env_file(tmp_path, OPENAI_API_KEY="sk-DUMMY", **env_kv)
    import time
    t0 = time.monotonic()
    r = subprocess.run([PY, str(SCRIPT), "--env-file", str(env_file), "--yes"], cwd=ROOT,
                       capture_output=True, text=True, timeout=10.0, stdin=subprocess.DEVNULL,
                       env=_clean_subprocess_env())
    elapsed = time.monotonic() - t0
    assert r.returncode == 2, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert elapsed < 5.0, f"{elapsed:.1f}s かかった＝probe が走った疑いがあります"
    assert "・検査中" not in r.stdout, "候補が不正なのに検査が走っています"
    assert "通信前" in (r.stdout + r.stderr)


# ---- 静的検査: 本番コードを実際に import して使っている（＝再実装していない） ----

_SRC = SCRIPT.read_text(encoding="utf-8")


@pytest.mark.parametrize("module_ref", [
    "from sherpa import agent_constructs, agentic_search, embeddings, llm",
    "from sherpa.providers.codex import sandbox as codex_sandbox",
    "from sherpa.providers.openai import OpenAIProvider",
])
def test_script_imports_production_modules(module_ref: str):
    assert module_ref in _SRC, f"本番モジュールの import が見つかりません: {module_ref}"


@pytest.mark.parametrize("usage", [
    "llm.openai_url(",
    "llm.openai_headers(",
    "llm.openai_base_url(",
    "llm.openai_endpoint_kind(",
    "llm.assert_openai_base_url_allowed(",
    "llm.openai_post_json(",
    "embeddings.cfg(",
    "embeddings._embed_batch(",
    "agentic_search.openai_tools(",
    "provider._messages(",
    "provider._stream(",
    "codex_sandbox._write_codex_authoring_config(",
    "codex_sandbox._codex_clean_env(",
])
def test_script_calls_production_functions(usage: str):
    """本番の関数/メソッドを実際に呼んでいる行があること（import だけして未使用、を防ぐ）。"""
    assert usage in _SRC, f"本番コードの呼び出しが見つかりません: {usage}"


def test_script_does_not_reimplement_http_request_building():
    """`urllib.request.Request(` を独自に組んで OpenAI 互換 API へ直接送るコードが無いこと
    （HTTP 送信は必ず `llm.openai_post_json` 経由＝`_do_post` の唯一の POST 経路であることの逆側
    チェック・`llm.post_json` 直呼びは対象外）。Codex 検査（⑧）は `codex exec`
    サブプロセスを呼ぶだけなので対象外。"""
    assert "urllib.request.Request(" not in _SRC
    assert "urllib.request.urlopen(" not in _SRC
    # OpenAI 専用の送信直前ガード（`assert_openai_io_allowed`）を経由しない `llm.post_json(` の
    # 直呼びが無いこと（Gemini/Ollama 共用の層を OpenAI 送信に使わない契約・他 sink と揃える）。
    assert "llm.post_json(" not in _SRC


# ---- 回帰固定: コンテンツフィルタ誤検知（2026-08-21・実 Azure gpt-4.1-mini で発覚） ----
#
# 実機で観測された誤検知: ② chat/completions が `finish_reason=stop` で本文 'OK' を
# 返しているのに NG、⑥ Responses API が `status=completed` で完全に成功しているのに NG。
# 原因は、成功応答（HTTP 200）の JSON 全体を `json.dumps()` で文字列化して部分文字列
# "content_filter" を検索していたため、Azure が**正常応答にも必ず付与する**
# `content_filter_results`／`prompt_filter_results`（フィルタを通した結果の記録・
# 通常は各カテゴリ `filtered: false`）というフィールド名自体にヒットしていたこと。
# 判定は `_chat_filter_verdict`/`_responses_filter_verdict`/`_has_filtered_category`
# という純関数に切り出してある（`scripts/azure_smoke.py` 参照）＝ここではその純関数に
# 実 Azure が返した形に近い応答 JSON を直接渡す（ネットワークは使わない・モックでもない）。

# 実機で観測された②の応答に近い形（usage(prompt=26,completion=2) も実測どおり）。
_CHAT_RESPONSE_OK_NOT_FILTERED = {
    "id": "chatcmpl-azure-smoke-test",
    "object": "chat.completion",
    "model": "gpt-4.1-mini-2025-04-14",
    "choices": [{
        "index": 0,
        "finish_reason": "stop",
        "message": {"role": "assistant", "content": "OK"},
        "content_filter_results": {
            "hate": {"filtered": False, "severity": "safe"},
            "self_harm": {"filtered": False, "severity": "safe"},
            "sexual": {"filtered": False, "severity": "safe"},
            "violence": {"filtered": False, "severity": "safe"},
        },
    }],
    "prompt_filter_results": [{
        "prompt_index": 0,
        "content_filter_results": {
            "hate": {"filtered": False, "severity": "safe"},
            "self_harm": {"filtered": False, "severity": "safe"},
            "sexual": {"filtered": False, "severity": "safe"},
            "violence": {"filtered": False, "severity": "safe"},
            "jailbreak": {"filtered": False, "detected": False},
        },
    }],
    "usage": {"prompt_tokens": 26, "completion_tokens": 2, "total_tokens": 28},
}

# 実際にフィルタで遮断された場合の形（`finish_reason=content_filter`・本文は空）。
_CHAT_RESPONSE_ACTUALLY_FILTERED = {
    "id": "chatcmpl-azure-smoke-test-blocked",
    "object": "chat.completion",
    "model": "gpt-4.1-mini-2025-04-14",
    "choices": [{
        "index": 0,
        "finish_reason": "content_filter",
        "message": {"role": "assistant", "content": ""},
        "content_filter_results": {
            "hate": {"filtered": True, "severity": "high"},
        },
    }],
    "usage": {"prompt_tokens": 26, "completion_tokens": 0, "total_tokens": 26},
}

# ⑥ Responses API・実機どおり status=completed（filtered なフィールドが無い最小形）。
_RESPONSES_RESPONSE_COMPLETED = {
    "id": "resp-azure-smoke-test",
    "object": "response",
    "model": "gpt-4.1-mini",
    "status": "completed",
    "output_text": "OK",
}


def test_chat_finish_stop_with_all_filtered_false_is_not_blocked():
    """`finish_reason=stop` ＋ `content_filter_results` が全て `filtered: false` → フィルタ扱いにしない。

    これが今回の実機誤検知そのもの（旧実装はこれを NG にしていた）。"""
    choice = _CHAT_RESPONSE_OK_NOT_FILTERED["choices"][0]
    content = choice["message"]["content"]
    blocked, note = azure_smoke._chat_filter_verdict(
        _CHAT_RESPONSE_OK_NOT_FILTERED, choice["finish_reason"], content)
    assert blocked is False, f"誤検知が再発しています: note={note!r}"
    assert note is None


def test_chat_finish_content_filter_is_blocked():
    """`finish_reason=content_filter` → フィルタ扱い（実際に遮断された場合は従来どおり NG）。"""
    choice = _CHAT_RESPONSE_ACTUALLY_FILTERED["choices"][0]
    content = choice["message"]["content"]
    blocked, note = azure_smoke._chat_filter_verdict(
        _CHAT_RESPONSE_ACTUALLY_FILTERED, choice["finish_reason"], content)
    assert blocked is True
    assert note and "content_filter" in note.lower()


def test_chat_partial_filter_with_content_is_ok_with_note():
    """一部カテゴリが `filtered: true` でも本文が返っていれば OK 扱い＋注記（部分フィルタの判断）。"""
    resp = {
        "choices": [{
            "finish_reason": "stop",
            "message": {"content": "OK"},
            "content_filter_results": {"hate": {"filtered": True, "severity": "low"}},
        }],
    }
    blocked, note = azure_smoke._chat_filter_verdict(resp, "stop", "OK")
    assert blocked is False
    assert note is not None and "OK扱い" in note


def test_responses_status_completed_is_not_blocked():
    """Responses の `status=completed` → フィルタ扱いにしない（⑥の実機誤検知の回帰固定）。"""
    blocked, note = azure_smoke._responses_filter_verdict(
        _RESPONSES_RESPONSE_COMPLETED, _RESPONSES_RESPONSE_COMPLETED["status"])
    assert blocked is False, f"誤検知が再発しています: note={note!r}"
    assert note is None


def test_responses_incomplete_content_filter_is_blocked():
    resp = {"status": "incomplete", "incomplete_details": {"reason": "content_filter"}}
    blocked, note = azure_smoke._responses_filter_verdict(resp, "incomplete")
    assert blocked is True
    assert note and "content_filter" in note.lower()


def test_has_filtered_category_ignores_field_names_only_looks_at_values():
    """キー名に "content_filter" が含まれるだけの構造（`filtered` が無い/false）では
    ヒットしないこと（旧実装のバグそのものの再発防止＝文字列一致ではなく値を見る）。"""
    assert azure_smoke._has_filtered_category(_CHAT_RESPONSE_OK_NOT_FILTERED) is False
    assert azure_smoke._has_filtered_category({"content_filter_results": {}}) is False
    assert azure_smoke._has_filtered_category({"content_filter_results": {"hate": {"filtered": True}}}) is True


def test_check_chat_end_to_end_with_azure_shaped_response_is_ok(monkeypatch):
    """`_check_chat`（②の実行本体）に、実機で観測された応答をそのまま流し込んで OK になること。

    `_do_post`（唯一の POST 経路）だけを差し替え、HTTP は一切発生させない。"""
    monkeypatch.setattr(azure_smoke, "_do_post",
                         lambda path, body, api_key: (True, _CHAT_RESPONSE_OK_NOT_FILTERED))
    ok, detail = azure_smoke._check_chat({}, "gpt-4.1-mini", "sk-dummy")
    assert ok is True, f"detail={detail!r}"
    assert "finish_reason=stop" in detail
    assert "content_filter" not in detail.lower() or "OK扱い" in detail


def test_check_responses_end_to_end_with_azure_shaped_response_is_ok(monkeypatch):
    """`_check_responses`（⑥の実行本体）も同様に、実機どおりの completed 応答で OK になること。"""
    monkeypatch.setattr(azure_smoke, "_do_post",
                         lambda path, body, api_key: (True, _RESPONSES_RESPONSE_COMPLETED))
    ok, detail = azure_smoke._check_responses({}, "gpt-4.1-mini", "sk-dummy")
    assert ok is True, f"detail={detail!r}"
    assert "status=completed" in detail
