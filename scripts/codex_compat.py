#!/usr/bin/env python3
"""Codex CLI の互換の通し試験（次の取り込みで Codex CLI を新しい版へ上げる前の点検）。

Azure OpenAI 等の Codex(OpenAI) 互換接続先の構成が、新しい Codex CLI（PATH 上の `codex`。
このホストでは codex-cli 0.153.4）でも壊れないかを、**実 API を一切呼ばず**に確かめる。
偽の接続先（127.0.0.1 の http.server・OpenAI Responses API の SSE を最小限だけ模す）を用意し、
Sherpa の本番コード（`sherpa.providers.codex.sandbox` の config.toml 生成・
`sherpa.providers.codex.provider.CodexProvider` の実行経路）をそのまま呼んで、実際の Codex CLI
で 1 ターン走らせる。

**制約（実機で確認済み・回避できない既知の限界）**: Codex CLI 0.153.4 の Responses API
クライアントは自己署名 CA を信用しない（`SSL_CERT_FILE`/`SSL_CERT_DIR` のどちらを渡しても
偽サーバーへの接続に一度も到達しない＝TLS ハンドシェイクの時点で失敗する。ローカルの
`curl --cacert`/`SSL_CERT_FILE` では同じ CA が正しく検証できることを確認済みのため、
codex 側の TLS スタック（システムの信頼ストアや env を見ない静的な検証）に起因する）。
そのため本スクリプトの**実接続確認は http:// の偽サーバーで行う**——
`sherpa.llm.assert_openai_base_url_allowed`（https 必須のゲート）はこのスクリプトの
プロセス内だけで一時的に無効化する（`sherpa/` のソース自体は変更しない）。https 必須の
ゲートそのものが生きているかは別途、静的検証（通信なし）で確認する。

実行: `make codex-compat` または `SHERPA_USE_FIXTURES=1 .venv/bin/python scripts/codex_compat.py`
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

_FAKE_MODEL = "fake-deployment"
_EXPECT_KEY = "codex-compat-dummy-key"


# ---------------------------------------------------------------------------
# 偽の Azure/OpenAI 互換接続先（stdlib のみ・127.0.0.1 限定）
# ---------------------------------------------------------------------------

class _FakeState:
    """偽サーバーが受けたリクエストの記録（スレッド安全）。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[dict] = []
        self.answer_text = "OK-FROM-FAKE-SERVER"
        self.json_answer = False

    def record(self, entry: dict) -> None:
        with self.lock:
            self.requests.append(entry)

    def snapshot(self) -> list[dict]:
        with self.lock:
            return list(self.requests)


def _headline_payload(state: _FakeState) -> str:
    if not state.json_answer:
        return state.answer_text
    return json.dumps({
        "status": "final", "answer": state.answer_text, "next_step": None,
        "claims": [{"id": "c1", "status": "confirmed", "text": state.answer_text,
                    "evidence_refs": [], "reason": "fake", "reason_code": "",
                    "evidence_kinds": []}],
    }, ensure_ascii=False)


def _make_handler(state: _FakeState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):   # stdlib 既定の stderr アクセスログは出さない
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw) if raw else {}
            except ValueError:
                body = {}
            state.record({
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "api_key_header": self.headers.get("api-key"),
                "model": body.get("model") if isinstance(body, dict) else None,
                "input_repr": json.dumps(body.get("input"), ensure_ascii=False)
                              if isinstance(body, dict) else "",
                "has_text_format": isinstance(body, dict) and bool(body.get("text")),
            })

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            def sse(event: str, data: dict) -> None:
                chunk = f"event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8")
                self.wfile.write(chunk)
                self.wfile.flush()

            resp_id = f"resp_{uuid.uuid4().hex[:24]}"
            item_id = f"msg_{uuid.uuid4().hex[:24]}"
            text = _headline_payload(state)
            base = {"id": resp_id, "object": "response",
                   "model": body.get("model") if isinstance(body, dict) else _FAKE_MODEL,
                   "status": "in_progress", "output": []}
            sse("response.created", {"type": "response.created", "response": base})
            sse("response.output_item.added", {
                "type": "response.output_item.added", "output_index": 0,
                "item": {"id": item_id, "type": "message", "status": "in_progress",
                         "role": "assistant", "content": []}})
            sse("response.output_text.delta", {
                "type": "response.output_text.delta", "item_id": item_id,
                "output_index": 0, "content_index": 0, "delta": text})
            sse("response.output_text.done", {
                "type": "response.output_text.done", "item_id": item_id,
                "output_index": 0, "content_index": 0, "text": text})
            completed_item = {"id": item_id, "type": "message", "status": "completed",
                              "role": "assistant",
                              "content": [{"type": "output_text", "text": text,
                                          "annotations": []}]}
            sse("response.output_item.done", {
                "type": "response.output_item.done", "output_index": 0,
                "item": completed_item})
            final = dict(base)
            final["status"] = "completed"
            final["output"] = [completed_item]
            final["usage"] = {"input_tokens": 12, "input_tokens_details": {"cached_tokens": 0},
                              "output_tokens": 6, "output_tokens_details": {"reasoning_tokens": 0},
                              "total_tokens": 18}
            sse("response.completed", {"type": "response.completed", "response": final})

    return Handler


class FakeEndpoint:
    """`with FakeEndpoint() as fake:` で 127.0.0.1 の1ポートに立ち上げる。"""

    def __init__(self, *, json_answer: bool = False, answer_text: str = "OK-FROM-FAKE-SERVER"):
        self.state = _FakeState()
        self.state.json_answer = json_answer
        self.state.answer_text = answer_text
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.state))
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}/openai/v1"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "FakeEndpoint":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()

    def requests(self) -> list[dict]:
        return self.state.snapshot()


# ---------------------------------------------------------------------------
# 結果の型
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    key: str
    label: str
    ok: bool
    detail: str


def _system_settings(base_url: str, *, auth_header: str = "bearer",
                     api_version: str = "") -> dict:
    return {
        "openai_endpoint_kind": "custom",
        "openai_base_url": base_url,
        "openai_auth_header": auth_header,
        "openai_api_version": api_version,
        "codex_worker_model": _FAKE_MODEL,
    }


# ---------------------------------------------------------------------------
# 静的検証（通信なし）
# ---------------------------------------------------------------------------

def check_https_gate_static() -> CheckResult:
    from sherpa import llm
    try:
        llm.assert_openai_base_url_allowed("http://127.0.0.1:9/v1")
    except llm.PreflightRejected:
        pass
    else:
        return CheckResult("https_gate", "https 必須ゲート（静的）", False,
                           "http:// の base_url を拒否しませんでした（本番のガードが壊れています）")
    try:
        llm.assert_openai_base_url_allowed("https://example.com/v1")
    except llm.PreflightRejected as e:
        return CheckResult("https_gate", "https 必須ゲート（静的）", False,
                           f"https:// まで拒否されました: {e}")
    return CheckResult("https_gate", "https 必須ゲート（静的）", True,
                       "http:// を拒否・https:// は許可（意図どおり）")


def check_provider_lines_static() -> CheckResult:
    from sherpa.providers.codex.sandbox import _openai_compat_provider_lines
    lines = _openai_compat_provider_lines(
        "https://example.openai.azure.com/openai/v1",
        api_version="2024-10-21", auth_header="api-key")
    txt = "\n".join(lines)
    ok = ('env_key = "OPENAI_API_KEY"' in txt and 'wire_api = "responses"' in txt
         and 'query_params = { "api-version" = "2024-10-21" }' in txt
         and 'env_http_headers = { "api-key" = "OPENAI_API_KEY" }' in txt)
    return CheckResult("provider_lines", "provider 行の生成（静的）", ok,
                       "api-version/api-key ヘッダ行を含む" if ok else f"想定外の内容: {txt!r}")


def check_developer_instructions_static() -> CheckResult:
    """既定(OpenAI)接続先でも role config に developer_instructions が書かれるか（本スライスの修正）。"""
    from sherpa.providers.codex.sandbox import _write_codex_authoring_config
    with tempfile.TemporaryDirectory(prefix="codex-compat-static-") as tmp:
        ch = Path(tmp) / "ch"
        _write_codex_authoring_config(
            ch, [], "low", False, "", None, web_search_enabled=False,
            multi_agent=True, orchestrator_model=_FAKE_MODEL)
        worker = (ch / "agents" / "worker.toml").read_text(encoding="utf-8")
        evaluator = (ch / "agents" / "evaluator.toml").read_text(encoding="utf-8")
    ok = "developer_instructions = " in worker and "developer_instructions = " in evaluator
    return CheckResult("dev_instructions_static", "role config の developer_instructions（静的）",
                       ok, "worker/evaluator とも含む" if ok else "欠落しています")


# ---------------------------------------------------------------------------
# 実接続確認（偽サーバー・http:// のみ・実 API は呼ばない）
# ---------------------------------------------------------------------------

def _patched_https_gate():
    """このプロセス内だけ https 必須ゲートを無効化するコンテキストマネージャ（モジュール
    docstring の「制約」参照）。呼び出し元で必ず with 文を使い、終了時に元へ戻す。"""
    from sherpa import llm

    class _Ctx:
        def __enter__(self_inner):
            self_inner._orig = llm.assert_openai_base_url_allowed
            llm.assert_openai_base_url_allowed = lambda base: None
            return self_inner

        def __exit__(self_inner, *exc):
            llm.assert_openai_base_url_allowed = self_inner._orig
    return _Ctx()


def check_codex_present() -> CheckResult:
    path = shutil.which("codex")
    if not path:
        return CheckResult("codex_cli", "Codex CLI の所在", False, "PATH に codex が見つかりません")
    try:
        out = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=10)
        ver = (out.stdout or out.stderr or "").strip()
    except Exception as e:
        ver = f"(バージョン取得失敗: {e})"
    return CheckResult("codex_cli", "Codex CLI の所在", True, f"{path} ({ver})")


def check_provider_turn(*, auth_header: str, api_version: str, label_suffix: str) -> CheckResult:
    """`CodexProvider.run(ctx)`（本番の実行経路＝サンドボックス・MCP・multi_agent・出力スキーマは
    既定のまま）を、偽の接続先へ向けて 1 ターン走らせる。argv がプロンプト本文を持たない
    （標準入力経由）ことも、実 Popen 呼び出しを横取りして確認する。"""
    key = f"provider_turn_{label_suffix}"
    label = f"CodexProvider の 1 ターン（{label_suffix}）"
    with tempfile.TemporaryDirectory(prefix="codex-compat-users-") as users_dir:
        os.environ["SHERPA_USERS_DIR"] = users_dir
        with FakeEndpoint(json_answer=True) as fake, _patched_https_gate():
            sysset = _system_settings(fake.base_url, auth_header=auth_header,
                                      api_version=api_version)
            captured_argv: list[list[str]] = []
            orig_popen = subprocess.Popen

            def _spy_popen(argv, *a, **kw):
                if isinstance(argv, list) and argv[:2] == ["codex", "exec"]:
                    captured_argv.append(list(argv))
                return orig_popen(argv, *a, **kw)

            subprocess.Popen = _spy_popen
            try:
                from sherpa import agents as A
                prov = A.CodexProvider(reasoning="low", model=_FAKE_MODEL,
                                       web_search=False, ollama_base_url=None,
                                       openai_api_key=_EXPECT_KEY, system_settings=sysset)
                ctx = A.Ctx(
                    message="codex-compat 通し試験の質問です。短く答えてください。",
                    world="v1",
                    route=lambda msg: {"lens": "qa", "input": msg, "reason": "codex-compat",
                                      "confident": True},
                    dispatch=lambda lens_, inp: {
                        "lens": lens_, "headline": "dispatch-headline",
                        "summary": {"total": 0}, "data": {}, "sources": []},
                    knowledge=True, uid="codex-compat", conversation_id=None,
                )
                events = list(prov.run(ctx))
            finally:
                subprocess.Popen = orig_popen

            reqs = fake.requests()

    results = [e for e in events if isinstance(e, dict) and e.get("type") == "_result"]
    if not results:
        return CheckResult(key, label, False, f"_result イベントが得られませんでした: {events!r}")
    env = results[0].get("env") or {}
    headline = results[0].get("headline") or env.get("headline")
    if not reqs:
        return CheckResult(key, label, False, "偽サーバーへリクエストが1件も届きませんでした")
    req = reqs[0]
    if not captured_argv:
        return CheckResult(key, label, False, "codex exec の Popen 呼び出しを観測できませんでした")
    argv = captured_argv[0]
    prompt_in_argv = any("codex-compat 通し試験の質問です" in a for a in argv)
    stdin_only = argv[-1] == "-" and not prompt_in_argv
    auth_ok = (req.get("authorization") == f"Bearer {_EXPECT_KEY}" if auth_header == "bearer"
              else req.get("api_key_header") == _EXPECT_KEY)
    model_ok = req.get("model") == _FAKE_MODEL
    api_version_ok = (not api_version) or (f"api-version={api_version}" in req.get("path", ""))
    problems = []
    if not stdin_only:
        problems.append(f"プロンプトが argv に載っている、または末尾が '-' でない: {argv!r}")
    if not auth_ok:
        problems.append(f"認証ヘッダが届いていません（{auth_header}）: {req!r}")
    if not model_ok:
        problems.append(f"モデル名が届いていません: model={req.get('model')!r}")
    if not api_version_ok:
        problems.append(f"api-version がクエリに届いていません: path={req.get('path')!r}")
    if not headline or "OK-FROM-FAKE-SERVER" not in str(headline):
        problems.append(f"回答（headline）が想定外です: {headline!r}")
    if problems:
        return CheckResult(key, label, False, " / ".join(problems))
    return CheckResult(key, label, True,
                       f"stdin 経由・{auth_header}・model={req.get('model')}・headline={headline!r}")


def check_role_files_live() -> CheckResult:
    """multi_agent の role config（worker/evaluator）が実際の Codex CLI 0.153.4 に拒否されない
    か（`--strict-config` で config 読込エラーにならない）を、非 ephemeral な codex_home で
    直接 `codex exec` を走らせて確認する（役割ファイルの中身も実行後に検査できるように、
    `CodexProvider` の ephemeral 経路は使わない）。stderr/stdout に
    「malformed」等の役割拒否メッセージが出ていないことも確認する。"""
    from sherpa.providers.codex import sandbox as codex_sandbox

    with tempfile.TemporaryDirectory(prefix="codex-compat-roles-") as tmp, \
         FakeEndpoint(json_answer=True) as fake, _patched_https_gate():
        tmp_path = Path(tmp)
        codex_home = tmp_path / "codex-home"
        run_dir = tmp_path / "run"
        tool_tmp = tmp_path / "tmp"
        run_dir.mkdir(parents=True, exist_ok=True)
        tool_tmp.mkdir(parents=True, exist_ok=True)
        sysset = _system_settings(fake.base_url)
        codex_sandbox._write_codex_authoring_config(
            codex_home, [], "low", False, "", None, web_search_enabled=False,
            system_settings=sysset, multi_agent=True, orchestrator_model=_FAKE_MODEL,
            link_auth=False)
        worker_toml = (codex_home / "agents" / "worker.toml").read_text(encoding="utf-8")
        evaluator_toml = (codex_home / "agents" / "evaluator.toml").read_text(encoding="utf-8")
        if "developer_instructions = " not in worker_toml or \
           "developer_instructions = " not in evaluator_toml:
            return CheckResult("role_files", "役割ファイル（worker/evaluator）", False,
                               "developer_instructions が role config に書かれていません")

        env = codex_sandbox._codex_clean_env(codex_home, run_dir, tool_tmp,
                                             openai_api_key=_EXPECT_KEY)
        last_message = tool_tmp / "last-message.txt"
        argv = ["codex", "exec", "--json", "--strict-config", "--skip-git-repo-check",
               "-o", str(last_message), "-C", str(run_dir), "-m", _FAKE_MODEL,
               "-c", "model_reasoning_effort=low", "-c", "features.multi_agent=true", "-"]
        try:
            proc = subprocess.run(argv, env=env, cwd=str(run_dir),
                                  input="短く答えてください。",
                                  capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            return CheckResult("role_files", "役割ファイル（worker/evaluator）", False,
                               "codex exec がタイムアウトしました（30秒）")

    combined = (proc.stdout or "") + (proc.stderr or "")
    if "malformed" in combined.lower() or "ignoring" in combined.lower():
        return CheckResult("role_files", "役割ファイル（worker/evaluator）", False,
                           f"役割ファイルが拒否された形跡があります: {combined[-400:]!r}")
    if proc.returncode != 0:
        return CheckResult("role_files", "役割ファイル（worker/evaluator）", False,
                           f"exit={proc.returncode} 出力末尾: {combined[-400:]!r}")
    return CheckResult("role_files", "役割ファイル（worker/evaluator）", True,
                       "developer_instructions 付きで config 受理・拒否メッセージなし・"
                       f"exit={proc.returncode}")


# ---------------------------------------------------------------------------
# 実行
# ---------------------------------------------------------------------------

def main() -> int:
    checks = [
        check_codex_present,
        check_https_gate_static,
        check_provider_lines_static,
        check_developer_instructions_static,
        lambda: check_provider_turn(auth_header="bearer", api_version="",
                                    label_suffix="既定bearer"),
        lambda: check_provider_turn(auth_header="api-key", api_version="2024-10-21",
                                    label_suffix="api-key+api-version"),
        check_role_files_live,
    ]
    results: list[CheckResult] = []
    for fn in checks:
        t0 = time.monotonic()
        try:
            r = fn()
        except Exception as e:   # 想定外でもスクリプト自体は完走させる
            r = CheckResult(getattr(fn, "__name__", "unknown"), getattr(fn, "__name__", "?"),
                            False, f"想定外の例外: {type(e).__name__}: {e}")
        dt = time.monotonic() - t0
        print(f"[{'OK ' if r.ok else 'NG '}] {r.label} ({dt:.1f}s)")
        print(f"       {r.detail}")
        results.append(r)

    print("\n== まとめ ==")
    for r in results:
        print(f"[{'OK ' if r.ok else 'NG '}] {r.label}")
    ok_all = all(r.ok for r in results)
    print("\n" + ("全項目 OK" if ok_all else "NG があります（上記参照）"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
