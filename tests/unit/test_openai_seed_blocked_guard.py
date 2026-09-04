"""起動時 env シード未確定（`llm.set_openai_endpoint_seed_blocked`）時の fail-closed ガードが、
`llm.openai_url()`/`openai_headers()` を経由しない・または経由後も再確認が必要な OpenAI 系 I/O
経路にも効くことの固定。

対象経路（いずれも Codex(Ollama)／`ollama=True` は対象外＝ブロックしないことも合わせて固定）:
  1. `providers/__init__.py::_select_provider` の Codex(OpenAI) 選択時。
  2. `providers/codex/sandbox.py::_write_codex_authoring_config` の auth.json 受け渡し直前。
  3. `providers/codex/provider.py::CodexProvider._run_authoring` の `subprocess.Popen` 直前
     （config 作成後に block が成立するケースも含む＝Popen 直前ガード自体の独立性を確認）。
  4. `agentic_search.py::openai_style`/`_run_evaluation`/`attribute_openai_style` の HTTP 実送信直前
     （endpoint/headers を入口で1回だけ確定させて使い回す設計のため、送信のたびに再確認する）。
  5. `embeddings.py`/`graph_extract.py`（intent 共有）/`vision_arm.py`/`providers/openai.py`
     （streaming）の通常 HTTP sink。URL/header **確定後**に block へ遷移させてから呼ぶ
     （`_block_after_headers_built` 参照・入口確認だけで先に止まる false green を避ける）。

`sherpa.llm._openai_endpoint_seed_blocked_reason` はプロセス内グローバル＝各テストは
`monkeypatch.setattr(llm, "_openai_endpoint_seed_blocked_reason", None)` で前提を揃え、
monkeypatch のロールバックにより他テストへ漏らさない。
"""
from __future__ import annotations

import os
import subprocess
import threading
import time

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

import pytest

from sherpa import agentic_search, llm
from sherpa import agents as A
from sherpa.providers import _select_provider
from sherpa.providers.codex.sandbox import _write_codex_authoring_config


@pytest.fixture(autouse=True)
def _unblocked(monkeypatch):
    """既定は未ブロック（他テストからの汚染防止）。"""
    monkeypatch.setattr(llm, "_openai_endpoint_seed_blocked_reason", None)


@pytest.fixture(autouse=True)
def _codex_cli_present(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)


# ===== 1. providers/__init__.py::_select_provider =====

def test_select_provider_codex_openai_unwired_when_seed_blocked(monkeypatch):
    llm.set_openai_endpoint_seed_blocked("test: 壊れた OPENAI_BASE_URL")
    from sherpa.providers import _UnwiredProvider

    p = _select_provider({"agent": "codex", "codex_model_provider": "openai"})
    assert isinstance(p, _UnwiredProvider), f"blocked 中なのに CodexProvider が組み立てられた: {p!r}"


def test_select_provider_codex_ollama_not_blocked_by_seed_block(monkeypatch):
    """Codex(Ollama) 構成は OpenAI 接続先シードの状態と無関係＝blocked 中でも遮断しない。"""
    llm.set_openai_endpoint_seed_blocked("test: 壊れた OPENAI_BASE_URL")
    p = _select_provider({"agent": "codex", "codex_model_provider": "ollama",
                          "ollama_url": "http://localhost:11434"})
    assert p.__class__.__name__ == "CodexProvider", f"blocked 中に Codex(Ollama) まで遮断された: {p!r}"
    assert p._ollama_base_url is not None


# ===== 2. providers/codex/sandbox.py::_write_codex_authoring_config =====

def test_write_authoring_config_raises_for_openai_construct_when_blocked(tmp_path):
    llm.set_openai_endpoint_seed_blocked("test: 壊れた OPENAI_BASE_URL")
    with pytest.raises(RuntimeError):
        _write_codex_authoring_config(tmp_path / "ch", ["/kb"], "low", False, "test", None)
    # auth.json symlink を作る前に止まっている（config.toml も書かれていない）。
    assert not (tmp_path / "ch" / "auth.json").exists()
    assert not (tmp_path / "ch" / "config.toml").exists()


def test_write_authoring_config_unaffected_for_ollama_construct_when_blocked(tmp_path):
    llm.set_openai_endpoint_seed_blocked("test: 壊れた OPENAI_BASE_URL")
    _write_codex_authoring_config(tmp_path / "ch", ["/kb"], "low", False, "test", None,
                                  ollama_base_url="http://127.0.0.1:11500/")
    txt = (tmp_path / "ch" / "config.toml").read_text(encoding="utf-8")
    assert 'model_provider = "sherpa-ollama"' in txt


# ===== 3. providers/codex/provider.py::CodexProvider._run_authoring（Popen 直前） =====

class _FakeProc:
    """`subprocess.Popen` の最小スタブ（実プロセスを起動しない・_attempt のループ/finally が
    落ちない程度の形だけ揃える）。"""
    stdout = iter(())
    returncode = 0
    pid = 999999

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def _ctx(uid: str) -> "A.Ctx":
    """DB 不要な最小 Ctx（route/dispatch を固定ラムダ・test_codex_kill_timeout.py と同じ流儀）。"""
    return A.Ctx(
        message="seed blocked ガードのテスト",
        world="v1",
        route=lambda msg: {"lens": "qa", "input": msg, "reason": "test", "confident": True},
        dispatch=lambda lens_, inp: {
            "lens": lens_, "headline": "dispatch-headline",
            "summary": {"total": 0}, "data": {}, "sources": [],
        },
        knowledge=True,
        uid=uid,
        stop_event=None,
    )


def _drive(prov, ctx) -> list:
    return [ev for ev in prov.run(ctx)]


def test_popen_not_reached_for_openai_construct_when_seed_blocked(tmp_path, monkeypatch):
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))
    calls: list = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: (calls.append((a, kw)), _FakeProc())[-1])
    llm.set_openai_endpoint_seed_blocked("test: 壊れた OPENAI_BASE_URL")

    prov = A.CodexProvider()   # Codex(OpenAI) 構成（ollama_base_url 省略）
    assert prov._ollama_base_url is None
    _drive(prov, _ctx("seedblocked-openai-u1"))

    assert calls == [], f"blocked 中なのに subprocess.Popen が呼ばれた: {calls!r}"


def test_popen_not_reached_when_seed_blocked_becomes_true_after_config_written(tmp_path, monkeypatch):
    """config 作成の**後**（config/auth のガードは未ブロックのまま通過済み）に block が
    成立しても、Popen 直前の独立ガードが単独で捕まえることを固定する。block を config 作成
    **前**に立てる隣接テスト（`test_popen_not_reached_for_openai_construct_when_seed_blocked`）
    だけでは、`_write_codex_authoring_config` 内のガードで先に止まっている可能性を否定できない
    （Popen 直前ガード自体を削除してもそちらのテストは変わらず通ってしまう＝本テストが無いと
    Popen 直前ガードの必要性を独立に固定できない）。"""
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))
    calls: list = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: (calls.append((a, kw)), _FakeProc())[-1])

    import sherpa.providers.codex.provider as _provider_mod
    real_write = _provider_mod._write_codex_authoring_config

    def _write_then_block(*a, **kw):
        result = real_write(*a, **kw)   # 未ブロックのまま実行＝config 作成そのものは成功させる。
        llm.set_openai_endpoint_seed_blocked("test: config 作成後に block が成立")
        return result

    monkeypatch.setattr(_provider_mod, "_write_codex_authoring_config", _write_then_block)

    prov = A.CodexProvider()
    assert prov._ollama_base_url is None
    _drive(prov, _ctx("seedblocked-openai-configorder"))

    assert calls == [], f"config 作成後に block が成立したのに Popen が呼ばれた: {calls!r}"


def test_popen_reached_for_ollama_construct_when_seed_blocked(tmp_path, monkeypatch):
    """Codex(Ollama) 構成は blocked 中でも Popen まで到達する（遮断しないことの固定）。"""
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))
    calls: list = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: (calls.append((a, kw)), _FakeProc())[-1])
    llm.set_openai_endpoint_seed_blocked("test: 壊れた OPENAI_BASE_URL")

    prov = A.CodexProvider(ollama_base_url="http://127.0.0.1:11500/")
    assert prov._ollama_base_url is not None
    _drive(prov, _ctx("seedblocked-ollama-u1"))

    assert len(calls) >= 1, "Codex(Ollama) 構成なのに blocked で Popen まで遮断された"


# ===== 4. agentic_search.py（HTTP 実送信直前） =====

def test_openai_style_does_not_post_when_seed_blocked(monkeypatch):
    calls: list = []
    monkeypatch.setattr(agentic_search, "_post", lambda *a, **kw: calls.append((a, kw)) or {
        "choices": [{"message": {"content": "unused", "tool_calls": None}, "finish_reason": "stop"}]})
    llm.set_openai_endpoint_seed_blocked("test: 壊れた OPENAI_BASE_URL")

    with pytest.raises(RuntimeError):
        list(agentic_search.openai_style(
            "https://api.openai.com/v1/chat/completions", {"Authorization": "Bearer x"},
            "gpt-5.5", "system", "user", "v1", None, ollama=False, max_turns=1))
    assert calls == [], f"blocked 中なのに _post が呼ばれた: {calls!r}"


def test_openai_style_ollama_not_blocked_by_seed_block(monkeypatch):
    """`ollama=True` は openai_endpoint シード状態と無関係＝blocked 中でも _post に到達する。"""
    calls: list = []

    def _fake_post(*a, **kw):
        calls.append((a, kw))
        return {"message": {"content": "ok"}}

    monkeypatch.setattr(agentic_search, "_post", _fake_post)
    llm.set_openai_endpoint_seed_blocked("test: 壊れた OPENAI_BASE_URL")

    events = list(agentic_search.openai_style(
        "http://localhost:11434/api/chat", {}, "qwen2.5", "system", "user", "v1", None,
        ollama=True, max_turns=1))
    assert calls, "Ollama 経路なのに blocked で _post まで遮断された"
    assert events, "Ollama 経路が blocked の影響で応答を返さなかった"


def test_run_evaluation_does_not_post_when_seed_blocked(monkeypatch):
    """`_run_evaluation` は blocked 中の `assert_openai_io_allowed()` の `RuntimeError` を
    そのまま呼び出し元へ伝播させる（`_send` と同じ契約＝別ナッジでの飲み込み再試行はしない）。
    ガードは予算消費/usage 加算より先＝弾かれた試行の分は消費しない。"""
    calls: list = []
    monkeypatch.setattr(agentic_search, "_post", lambda *a, **kw: calls.append((a, kw)) or {})
    llm.set_openai_endpoint_seed_blocked("test: 壊れた OPENAI_BASE_URL")

    budget = agentic_search._CallBudget(5)
    usage_acc = {"calls": 0, "tokens": None}
    with pytest.raises(RuntimeError):
        agentic_search._run_evaluation(
            "https://api.openai.com/v1/chat/completions", {"Authorization": "Bearer x"},
            "gpt-5.5", [{"role": "user", "content": "hi"}], ollama=False, timeout=5,
            usage=agentic_search._new_usage_acc(), usage_acc=usage_acc, call_budget=budget)
    assert calls == [], f"blocked 中なのに _post が呼ばれた: {calls!r}"
    assert budget.remaining == 5
    assert usage_acc["calls"] == 0


def test_run_evaluation_ollama_not_blocked_by_seed_block(monkeypatch):
    calls: list = []
    monkeypatch.setattr(agentic_search, "_post", lambda *a, **kw: calls.append((a, kw)) or {
        "message": {"tool_calls": [{"function": {"name": "submit_evaluation",
                    "arguments": '{"status":"sufficient","next_action":"stop"}'}}]}})
    llm.set_openai_endpoint_seed_blocked("test: 壊れた OPENAI_BASE_URL")

    agentic_search._run_evaluation(
        "http://localhost:11434/api/chat", {}, "qwen2.5", [{"role": "user", "content": "hi"}],
        ollama=True, timeout=5, usage=agentic_search._new_usage_acc(), usage_acc=None)
    assert calls, "Ollama 経路なのに blocked で _post まで遮断された"


def test_attribute_openai_style_does_not_post_when_seed_blocked(monkeypatch):
    """`attribute_openai_style` も同様に broad except で囲むため、blocked 時は空集合へ縮退する
    （`_post` 自体は呼ばれないことを固定する）。ガードは予算消費/usage 加算より先＝blocked で
    弾かれた試行の分は call_budget・usage_acc とも消費しない。"""
    calls: list = []
    monkeypatch.setattr(agentic_search, "_post", lambda *a, **kw: calls.append((a, kw)) or {})
    llm.set_openai_endpoint_seed_blocked("test: 壊れた OPENAI_BASE_URL")

    budget = agentic_search._CallBudget(5)
    usage_acc = {"calls": 0, "tokens": None}
    out = agentic_search.attribute_openai_style(
        "https://api.openai.com/v1/chat/completions", {"Authorization": "Bearer x"}, "gpt-5.5",
        False, "answer text", "digest text", {"e1": {}}, timeout=5,
        usage_acc=usage_acc, call_budget=budget)
    assert calls == [], f"blocked 中なのに _post が呼ばれた: {calls!r}"
    assert out == set()
    assert budget.remaining == 5      # 消費していない
    assert usage_acc["calls"] == 0    # 加算していない


def test_attribute_openai_style_ollama_not_blocked_by_seed_block(monkeypatch):
    calls: list = []
    monkeypatch.setattr(agentic_search, "_post", lambda *a, **kw: calls.append((a, kw)) or {
        "message": {"tool_calls": [{"function": {"name": "submit_attribution",
                    "arguments": '{"evidence_ids":["e1"]}'}}]}})
    llm.set_openai_endpoint_seed_blocked("test: 壊れた OPENAI_BASE_URL")

    agentic_search.attribute_openai_style(
        "http://localhost:11434/api/chat", {}, "qwen2.5", True, "answer text", "digest text",
        {"e1": {}}, timeout=5)
    assert calls, "Ollama 経路なのに blocked で _post まで遮断された"


def test_assert_openai_io_allowed_raises_when_blocked(monkeypatch):
    monkeypatch.setattr(llm, "_openai_endpoint_seed_blocked_reason", "boom")
    with pytest.raises(RuntimeError, match="boom"):
        llm.assert_openai_io_allowed()


def test_assert_openai_io_allowed_noop_when_not_blocked(monkeypatch):
    monkeypatch.setattr(llm, "_openai_endpoint_seed_blocked_reason", None)
    llm.assert_openai_io_allowed()   # 例外を出さない


# ===== 4b. begin_openai_send: block 状態遷移との原子性（強制インターリーブ） =====
# `agentic_search.py` の3送信経路（`_send`・`_run_evaluation`・`attribute_openai_style`）は、
# ガード確認と物理送信の間に隙間があると、その隙間で別スレッド（`/healthz` の再シード等）が
# block を成立させても通過済みのまま送信してしまう。`llm.begin_openai_send` は
# `set_openai_endpoint_seed_blocked` と同一ロックで両者を直列化する——ここでは実際にスレッドを
# 立てて、`begin_openai_send` のガード確認中（＝ロック保持中）に block 成立を試みても、
# ロック解放まで完了しないことを強制インターリーブで固定する。

def test_begin_openai_send_serializes_with_concurrent_block_transition():
    """`begin_openai_send` のガード確認中（ロック保持中）に別スレッドが
    `set_openai_endpoint_seed_blocked` を呼んでも、ロックが解放されるまでその呼び出しはブロック
    される——実際に一定時間ロックを保持させ、別スレッドの呼び出しがその時間だけ待たされる
    （＝ほぼ即座には完了しない）ことを経過時間で固定する（events の出現順だけを見る検証は、
    テスト自身が仕込んだ同期イベントの前後関係をなぞるだけになり、ロックの有無を区別できない
    ため使わない）。

    双方向同期（`entered`→`blocker_ready`）: blocker は「呼び出し直前まで到達した」ことを
    `blocker_ready` で main 側へ伝え、main はそれを確認してから初めて `hold_seconds` の滞在
    （ロック保持）を開始する。これが無いと、共有マシンの負荷で blocker スレッドの起床が
    遅れた場合、main が既に滞在時間の一部を消化した後に blocker がようやくロック取得を試みる
    ことになり、実測待ち時間が `hold_seconds` より大きく目減りして閾値判定が揺れる
    （本ファイルの他の pytest プロセスが同じ共有 DB へ高負荷をかけている環境で実際に観測）。

    block 成立より前に開始が確定した送信（今回の `begin_openai_send` 呼び出し）は予算消費・
    usage 加算まで完遂してよい（契約A）。以後の呼び出しは block 済みとして拒否され、拒否された
    分の予算・usage は消費しない（契約B）。"""
    entered = threading.Event()
    blocker_ready = threading.Event()
    hold_seconds = 0.3   # begin_openai_send がロックを保持し続ける長さ（余裕を持たせた値）
    blocker_elapsed: list[float] = []
    blocker_error: list[BaseException] = []

    class _Budget:
        def __init__(self):
            self.n = 0

        def consume(self):
            self.n += 1
            return True

    budget = _Budget()
    usage_acc = {"calls": 0}
    real_guard = llm.assert_openai_io_allowed

    def instrumented_guard():
        real_guard()
        entered.set()
        # blocker が呼び出し直前まで到達したと確認してから滞在を始める（双方向同期）。
        assert blocker_ready.wait(timeout=2), "blocker が呼び出し直前まで到達しなかった"
        time.sleep(hold_seconds)   # ロックを保持したまま滞在する（本テストの中心的な仕込み）

    def blocker():
        try:
            assert entered.wait(timeout=2), "begin_openai_send のガード確認まで到達しなかった"
            blocker_ready.set()
            t0 = time.monotonic()
            llm.set_openai_endpoint_seed_blocked("induced by race test")
            blocker_elapsed.append(time.monotonic() - t0)
        except BaseException as e:   # pragma: no cover - 失敗時に main スレッドへ伝える
            blocker_error.append(e)

    t = threading.Thread(target=blocker)
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(llm, "assert_openai_io_allowed", instrumented_guard)
        t.start()
        llm.begin_openai_send(budget, usage_acc)   # block 成立より前に開始が確定＝完遂してよい
    finally:
        monkeypatch.undo()
        t.join(timeout=2)

    assert not blocker_error, blocker_error
    assert budget.n == 1
    assert usage_acc["calls"] == 1
    # `set_openai_endpoint_seed_blocked` は `begin_openai_send` と同じロックを取り合うため、
    # ロックが保持されている `hold_seconds` の大半を待たされるはず（ロックが無ければ
    # ほぼ即座＝1桁ミリ秒未満で完了してしまう）。
    assert blocker_elapsed and blocker_elapsed[0] >= hold_seconds * 0.6, blocker_elapsed

    try:
        with pytest.raises(RuntimeError):
            llm.begin_openai_send(budget, usage_acc)
        assert budget.n == 1          # 拒否された分は消費しない
        assert usage_acc["calls"] == 1
    finally:
        llm.set_openai_endpoint_seed_blocked(None)


def test_begin_openai_send_rejects_when_budget_exhausted_without_consuming_or_counting_usage():
    """`begin_openai_send` は呼び出し予算が既に枯渇している（`consume()` が False を返す）とき
    `llm.SendBudgetExceeded` を送出し、`usage_acc` への加算を一切行わない（ガード確認の**後**・
    「1物理送信=1消費」契約により、消費に失敗した分は usage も加算しない）。"""
    class _ExhaustedBudget:
        def consume(self):
            return False

    usage_acc = {"calls": 0}
    with pytest.raises(llm.SendBudgetExceeded):
        llm.begin_openai_send(_ExhaustedBudget(), usage_acc)
    assert usage_acc["calls"] == 0


# ===== 4c. 3送信経路が実際に begin_openai_send を経由していることの固定（スパイ） =====
# 4b の原子性テストは `llm.begin_openai_send` 単体の契約を固定するが、それだけでは
# `agentic_search.py` の3送信経路（`_send`・`_run_evaluation`・`attribute_openai_style`）が
# 実際にこのヘルパーを呼んでいる保証にならない（例えば `assert_openai_io_allowed`/
# `_consume_call`/usage加算を個別に呼ぶ実装へ差し戻されても、4b 単体では検出できない）。
# 各経路で `llm.begin_openai_send` をスパイし、実際に呼ばれることを直接固定する。

def _spy_begin_openai_send(monkeypatch):
    """`llm.begin_openai_send` の実装はそのまま活かしつつ、呼び出し記録だけ追加するスパイ。"""
    calls: list = []
    real = llm.begin_openai_send

    def spy(call_budget=None, usage_acc=None):
        calls.append((call_budget, usage_acc))
        return real(call_budget, usage_acc)

    monkeypatch.setattr(llm, "begin_openai_send", spy)
    return calls


def test_send_uses_begin_openai_send_for_openai_sink(monkeypatch):
    """`_send`（`openai_style` 内部の物理送信）は OpenAI 経路（`ollama=False`）で
    `llm.begin_openai_send` を経由する。"""
    spy_calls = _spy_begin_openai_send(monkeypatch)
    monkeypatch.setattr(agentic_search, "_post", lambda *a, **kw: {
        "choices": [{"message": {"content": "ok", "tool_calls": None}, "finish_reason": "stop"}]})
    list(agentic_search.openai_style(
        "https://api.openai.com/v1/chat/completions", {"Authorization": "Bearer x"},
        "gpt-5.5", "system", "user", "v1", None, ollama=False, max_turns=1))
    assert len(spy_calls) >= 1, "_send が llm.begin_openai_send を経由していない"


def test_run_evaluation_uses_begin_openai_send(monkeypatch):
    """`_run_evaluation` は OpenAI 経路で `llm.begin_openai_send` を経由する。"""
    spy_calls = _spy_begin_openai_send(monkeypatch)
    monkeypatch.setattr(agentic_search, "_post", lambda *a, **kw: {
        "choices": [{"message": {"content": "not a tool call"}}]})
    agentic_search._run_evaluation(
        "https://api.openai.com/v1/chat/completions", {"Authorization": "Bearer x"},
        "gpt-5.5", [{"role": "user", "content": "hi"}], ollama=False, timeout=5,
        usage=agentic_search._new_usage_acc(), usage_acc=None)
    assert len(spy_calls) >= 1, "_run_evaluation が llm.begin_openai_send を経由していない"


def test_attribute_openai_style_uses_begin_openai_send(monkeypatch):
    """`attribute_openai_style` は OpenAI 経路で `llm.begin_openai_send` を経由する。"""
    spy_calls = _spy_begin_openai_send(monkeypatch)
    monkeypatch.setattr(agentic_search, "_post", lambda *a, **kw: {
        "choices": [{"message": {"tool_calls": [
            {"function": {"name": "submit_attribution", "arguments": '{"used":["e1"]}'}}]}}]})
    agentic_search.attribute_openai_style(
        "https://api.openai.com/v1/chat/completions", {"Authorization": "Bearer x"}, "gpt-5.5",
        False, "answer text", "digest text", {"e1": {}}, timeout=5)
    assert len(spy_calls) >= 1, "attribute_openai_style が llm.begin_openai_send を経由していない"


# ===== 5. HTTP sink 直前ガード（agentic 以外の通常 OpenAI HTTP 経路） =====
# embeddings.py/graph_extract.py（intent 共有）/vision_arm.py/providers/openai.py（streaming）は
# `llm.openai_url()`/`openai_headers()` の入口確認だけでなく、`llm.openai_post_json()`／
# `providers/openai.py::_stream` 内の実送信直前ガードでも独立に止まることを固定する。実際に
# ソケットを開く関数（`llm.urlopen_no_redirect`）を直接差し替え、blocked 中は一切呼ばれないことを
# 確認する（`llm.post_json`（Gemini/Ollama 共用）が一律遮断されていないことも併せて確認する）。
#
# block を呼出し**前**に立てるだけだと、`llm.openai_url()`/`openai_headers()` の入口確認
# （既に実装済み）だけで先に止まってしまい、`openai_post_json`/streaming の送信直前ガード自体を
# 削除してもこれらのテストは変わらず通る（false green）。`_block_after_headers_built` で
# `llm.openai_headers()` の**戻り値ができた後**に初めて block を成立させることで、URL/header
# 確定後・実送信直前のガードだけを独立に検証する。

def _block_after_headers_built(monkeypatch, reason="test: 壊れた OPENAI_BASE_URL（送信直前に成立）"):
    """`llm.openai_headers()` が実際に headers を組み立てた**直後**に block を成立させる。
    それより前（`openai_url()`/`openai_headers()` の入口確認）は未ブロックのまま通過させる。"""
    real_headers = llm.openai_headers

    def _headers_then_block(*a, **kw):
        result = real_headers(*a, **kw)
        llm.set_openai_endpoint_seed_blocked(reason)
        return result

    monkeypatch.setattr(llm, "openai_headers", _headers_then_block)


def test_embeddings_embed_batch_does_not_open_socket_when_seed_blocked(monkeypatch):
    from sherpa import embeddings

    calls: list = []
    monkeypatch.setattr(llm, "urlopen_no_redirect", lambda *a, **kw: calls.append(1))
    _block_after_headers_built(monkeypatch)

    out = embeddings._embed_batch(["hello"], {
        "provider": "openai", "key": "sk-test", "model": "text-embedding-3-small", "dim": 1536,
        "system_settings": {}})
    assert calls == [], f"blocked 中なのにソケットが開かれた: {calls!r}"
    assert out is None   # broad except で degrade（呼び出し元には例外を出さない既存契約）


def test_graph_extract_complete_json_raises_before_socket_open_when_seed_blocked(monkeypatch):
    from sherpa.ingest import graph_extract

    calls: list = []
    monkeypatch.setattr(llm, "urlopen_no_redirect", lambda *a, **kw: calls.append(1))
    _block_after_headers_built(monkeypatch)

    with pytest.raises(RuntimeError):
        graph_extract.complete_json("system", "user", {
            "provider": "openai", "key": "sk-test", "model": "gpt-5.5"}, timeout=5)
    assert calls == [], f"blocked 中なのにソケットが開かれた: {calls!r}"


def test_vision_arm_read_openai_does_not_open_socket_when_seed_blocked(monkeypatch):
    from sherpa.ingest.arms import vision_arm

    calls: list = []
    monkeypatch.setattr(llm, "urlopen_no_redirect", lambda *a, **kw: calls.append(1))
    monkeypatch.setattr(vision_arm, "_cloud_allowed_now", lambda *a, **kw: True)
    monkeypatch.setattr(vision_arm, "_openai_key", lambda *a, **kw: "sk-test")
    _block_after_headers_built(monkeypatch)

    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        img = Path(td) / "x.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n")
        with pytest.raises(RuntimeError):
            vision_arm._read_openai(img, {"model": "gpt-5.5-vision"}, timeout=5)
    assert calls == [], f"blocked 中なのにソケットが開かれた: {calls!r}"


def test_openai_provider_stream_raises_before_socket_open_when_seed_blocked(monkeypatch):
    from sherpa.providers.openai import OpenAIProvider

    calls: list = []
    monkeypatch.setattr(llm, "urlopen_no_redirect", lambda *a, **kw: calls.append(1))
    _block_after_headers_built(monkeypatch)

    prov = OpenAIProvider(api_key="sk-test")
    with pytest.raises(RuntimeError):
        list(prov._stream("hello"))
    assert calls == [], f"blocked 中なのにソケットが開かれた: {calls!r}"


def test_post_json_not_gated_by_seed_block_for_ollama_sink(monkeypatch):
    """`llm.post_json`（graph_extract の ollama 分岐が使う共用層）は OpenAI の block と無関係に
    動く（`openai_post_json` だけを追加した・既存の共用関数を一律遮断していないことの固定・
    `test_post_json_not_gated_by_openai_block` の agentic 版に対する非-agentic 版）。"""
    from sherpa.ingest import graph_extract

    calls: list = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"message": {"content": "ok"}}'

    monkeypatch.setattr(llm, "urlopen_no_redirect", lambda *a, **kw: (calls.append(1), _Resp())[-1])
    llm.set_openai_endpoint_seed_blocked("test: 壊れた OPENAI_BASE_URL")

    out = graph_extract.complete_json("system", "user", {
        "provider": "ollama", "url": "http://localhost:11434", "model": "qwen2.5"}, timeout=5)
    assert calls, "Ollama 経路なのに blocked でソケットまで遮断された"
    assert out == "ok"
