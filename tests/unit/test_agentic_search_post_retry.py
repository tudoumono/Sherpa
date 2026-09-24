"""同一プロバイダ内の限定リトライ（黙って別プロバイダへは切り替えない）。

`sherpa/agentic_search.py::_post` はテストが差し替える単発の choke point（リトライしない）。
実際のリトライは `openai_style()` の `_send`（呼び出し予算・usage 計測・stop_event・OpenAI 送信
ガードと同じスコープ）が、`_post` を物理送信のたびに1回ずつ呼ぶことで組み立てる
（1物理送信=1消費）。本ファイルは (1) 分類関数（`_retryable_post_error`/`_retry_after_seconds`）の
単体テスト、(2) `_post` 自体はリトライしないことの固定、(3) `_send`（`openai_style` 経由）の
リトライ＋予算/計測/停止判定の統合テストを持つ。
"""
from __future__ import annotations

import os
import urllib.error

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")

import pytest  # noqa: E402

from sherpa import agentic_search as A  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """バックオフの実待ちでテストを遅くしない（sleep したかどうかは呼び出し回数/引数で確認する）。"""
    calls = []
    monkeypatch.setattr(A.time, "sleep", lambda sec: calls.append(sec))
    return calls


def _http_error(code: int, headers=None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "err", headers, None)


# ===== 1. 分類関数の単体テスト =====

def test_retryable_post_error_classifies_status_and_types():
    assert A._retryable_post_error(_http_error(429)) is True
    assert A._retryable_post_error(_http_error(500)) is True
    assert A._retryable_post_error(_http_error(599)) is True   # 5xx 全域（500-599 に一般化）
    assert A._retryable_post_error(_http_error(400)) is False
    assert A._retryable_post_error(_http_error(401)) is False
    assert A._retryable_post_error(_http_error(404)) is False
    assert A._retryable_post_error(urllib.error.URLError("connection refused")) is True
    assert A._retryable_post_error(OSError("boom")) is True


def test_retryable_post_error_excludes_timeouts():
    """応答タイムアウトは対象外（上流で処理/課金が既に進んでいる可能性があり、二重送信リスクの
    ため再試行しない）。"""
    assert A._retryable_post_error(TimeoutError("timed out")) is False
    assert A._retryable_post_error(urllib.error.URLError(TimeoutError("timed out"))) is False


def test_is_connection_failure_classifies_refusal_dns_tls_and_unreachable():
    """`sherpa/research_service.py` が provider 名つき専用文言へ倒す判定・`_finalize_payload` の
    `failure_kind` 判定の単一の真実源。接続拒否・名前解決失敗・TLS 検証失敗・ホスト/ネットワーク
    到達不能（EHOSTUNREACH/ENETUNREACH/ENETDOWN）のいずれも真（`URLError` が `reason` に包んだ
    形も同様に見る）。"""
    import errno
    import socket
    import ssl

    assert A._is_connection_failure(ConnectionRefusedError(111, "Connection refused")) is True
    assert A._is_connection_failure(
        urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))) is True
    assert A._is_connection_failure(socket.gaierror("Name or service not known")) is True
    assert A._is_connection_failure(ssl.SSLError("certificate verify failed")) is True
    assert A._is_connection_failure(OSError(errno.EHOSTUNREACH, "No route to host")) is True
    assert A._is_connection_failure(OSError(errno.ENETUNREACH, "Network is unreachable")) is True
    assert A._is_connection_failure(OSError(errno.ENETDOWN, "Network is down")) is True


def test_is_connection_failure_excludes_per_call_timeout():
    """per-call の応答タイムアウト（`TimeoutError`／`socket.timeout`）は含めない——全体デッドライン
    超過は別途 `ResearchTimeout` が優先され、per-call timeout を「AI に接続できません」に倒すのは
    一時的な現象を設定不備と誤認させる（`_retryable_post_error` の非リトライ判定とは別の契約
    であることに注意——非リトライ＝二重送信を避ける、接続失敗集合＝メッセージ分類）。"""
    assert A._is_connection_failure(TimeoutError("timed out")) is False
    assert A._is_connection_failure(urllib.error.URLError(TimeoutError("timed out"))) is False


def test_is_connection_failure_excludes_unrelated_errors():
    """設定不備・上流の応答エラー（プロバイダには繋がったが失敗した）は対象外。"""
    assert A._is_connection_failure(RuntimeError("boom")) is False
    assert A._is_connection_failure(ValueError("bad value")) is False
    assert A._is_connection_failure(_http_error(500)) is False   # 5xx はプロバイダ到達済み


def test_retry_after_seconds_parses_numeric_and_caps():
    import email.message
    h = email.message.Message()
    h["Retry-After"] = "3"
    assert A._retry_after_seconds(_http_error(429, h)) == 3.0
    h2 = email.message.Message()
    h2["Retry-After"] = "9999"
    assert A._retry_after_seconds(_http_error(429, h2)) == A._RETRY_AFTER_CAP_SEC   # 上限で頭打ち


def test_retry_after_seconds_none_when_missing_or_unparseable():
    import email.message

    assert A._retry_after_seconds(_http_error(429)) is None
    h = email.message.Message()
    h["Retry-After"] = "not-a-date-or-number"
    assert A._retry_after_seconds(_http_error(429, h)) is None


def test_retry_after_seconds_rejects_nan_negative_and_infinity():
    """`float()` は "nan"/"inf"/"-inf" 等も受理してしまうため、これらを不正値として None
    （指数バックオフへのフォールバック）にする。"""
    import email.message

    for bad in ("nan", "inf", "-inf", "infinity", "-5"):
        h = email.message.Message()
        h["Retry-After"] = bad
        assert A._retry_after_seconds(_http_error(429, h)) is None, bad


# ===== 2. `_post` 自体はリトライしない（単発 choke point・テストが差し替える契約を保つ） =====

def test_post_is_single_attempt_no_retry(monkeypatch):
    attempts = []

    def fake_post_json(url, headers, body, timeout):
        attempts.append(1)
        raise _http_error(429)   # 再試行対象のエラーでも _post 自体はリトライしない

    monkeypatch.setattr(A.llm, "post_json", fake_post_json)
    with pytest.raises(urllib.error.HTTPError):
        A._post("http://x", {}, {}, timeout=10)
    assert len(attempts) == 1


# ===== 3. `_send`（`openai_style` 経由）: 呼び出し予算・usage・stop_event の内側でリトライする =====

def _final_of(events):
    return next(ev for ev in events if "final" in ev)


def test_send_retries_on_429_then_succeeds_and_counts_as_two_calls(monkeypatch):
    calls = []

    def fake_post(url, headers, body, timeout=90):
        calls.append(1)
        if len(calls) == 1:
            raise _http_error(429)
        return {"choices": [{"message": {"content": "最終回答"}}]}

    monkeypatch.setattr(A, "_post", fake_post)
    usage_acc = {"calls": 0, "tokens": None}
    events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None,
                                 usage_acc=usage_acc))
    assert _final_of(events)["final"] == "最終回答"
    assert len(calls) == 2                # 初回失敗＋再試行1回で成功（同一 endpoint への再試行のみ）
    assert usage_acc["calls"] == 2        # 1物理送信=1消費（初回1＋再試行1）


def test_send_does_not_retry_non_retryable_error(monkeypatch):
    calls = []

    def fake_post(url, headers, body, timeout=90):
        calls.append(1)
        raise _http_error(401)

    monkeypatch.setattr(A, "_post", fake_post)
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None))
    assert exc_info.value.code == 401
    assert len(calls) == 1


def test_send_marks_exceptions_from_physical_post_for_downstream_classification(monkeypatch):
    """`_send` は物理送信で発生した例外に `_sherpa_llm_send_error=True` を付与する——
    `sherpa/research_service.py` の catch-all がこの印の有無で「LLM 送信由来」と「ツール実行
    由来（grep 等のファイル I/O 障害）」を区別する唯一の手掛かり。リトライ有無に関わらず、
    最終的に伝播する例外には必ず付く。"""
    def fake_post(url, headers, body, timeout=90):
        raise _http_error(401)   # 非リトライ・即座に伝播

    monkeypatch.setattr(A, "_post", fake_post)
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None))
    assert getattr(exc_info.value, "_sherpa_llm_send_error", False) is True


def test_send_gives_up_after_max_retry_attempts_consuming_budget(monkeypatch):
    calls = []

    def fake_post(url, headers, body, timeout=90):
        calls.append(1)
        raise _http_error(503)

    monkeypatch.setattr(A, "_post", fake_post)
    budget = A._CallBudget(10)
    with pytest.raises(urllib.error.HTTPError):
        list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None,
                            call_budget=budget))
    assert len(calls) == A._POST_RETRY_ATTEMPTS + 1   # 初回＋限定回数の再試行＝3回
    # 1物理送信=1消費: `_send` が初回・再試行の全 attempt で自分で消費する（計3消費）。
    assert budget.remaining == 10 - 3


def test_send_stop_event_aborts_retry_without_extra_physical_send(monkeypatch):
    """再試行の直前で stop_event を確認する＝停止要求が来たらそれ以上物理送信しない
    （既存の停止契約と同型・final は出さない）。"""
    import threading

    calls = []
    stop_event = threading.Event()

    def fake_post(url, headers, body, timeout=90):
        calls.append(1)
        stop_event.set()               # 1回目の失敗と同時に停止要求が来たことにする
        raise _http_error(503)

    monkeypatch.setattr(A, "_post", fake_post)
    events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None,
                                 stop_event=stop_event))
    assert len(calls) == 1             # 再試行の物理送信はしない
    assert not any("final" in ev for ev in events)   # 停止時は final を出さない


def test_send_budget_exhausted_during_retry_yields_budget_exceeded_final(monkeypatch):
    """初回の物理送信で予算を使い切っても（`_send` が自分で消費する）、再試行分の予算が枯渇したら
    `budget_exceeded` として綺麗に打ち切る（黙って無制限に再試行しない）。"""
    calls = []

    def fake_post(url, headers, body, timeout=90):
        calls.append(1)
        raise _http_error(503)

    monkeypatch.setattr(A, "_post", fake_post)
    budget = A._CallBudget(1)   # 初回の1消費だけで枯渇＝リトライ時の追加消費に失敗する
    events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None,
                                 call_budget=budget))
    assert len(calls) == 1
    assert _final_of(events)["stop_reason"] == "budget_exceeded"


def test_send_budget_exhausted_skips_backoff_wait_before_final_abort(monkeypatch, _no_real_sleep):
    """直前の送信で呼び出し予算を使い切っていれば、再試行のバックオフ（`Retry-After` 由来・
    最大 `_RETRY_AFTER_CAP_SEC`＝10秒）を待たずに即座に `budget_exceeded` で打ち切る
    （待っても次の消費が失敗するだけなので、無意味な待機はしない）。"""
    import email.message

    calls = []
    h = email.message.Message()
    h["Retry-After"] = "999"   # 上限10秒に丸められる大きな値（待てば実質10秒待つことになる）

    def fake_post(url, headers, body, timeout=90):
        calls.append(1)
        raise _http_error(503, h)

    monkeypatch.setattr(A, "_post", fake_post)
    budget = A._CallBudget(1)   # 初回の1消費だけで枯渇＝リトライ時の追加消費に失敗する
    events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None,
                                 call_budget=budget))
    assert len(calls) == 1
    assert _no_real_sleep == []          # バックオフを待たずに即座に打ち切る
    assert _final_of(events)["stop_reason"] == "budget_exceeded"


def test_send_respects_retry_after_header_over_exponential_backoff(monkeypatch):
    import email.message

    calls = []
    h = email.message.Message()
    h["Retry-After"] = "7"

    def fake_post(url, headers, body, timeout=90):
        calls.append(1)
        if len(calls) == 1:
            raise _http_error(429, h)
        return {"choices": [{"message": {"content": "最終回答"}}]}

    monkeypatch.setattr(A, "_post", fake_post)
    sleeps = []
    monkeypatch.setattr(A.time, "sleep", lambda sec: sleeps.append(sec))
    events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None))
    assert _final_of(events)["final"] == "最終回答"
    assert sleeps == [7.0]   # 指数バックオフ（0.5s）でなく Retry-After（7s）を使う


def test_send_does_not_wait_and_send_past_deadline_on_large_retry_after(monkeypatch):
    """Retry-After が全体 deadline より長い場合、待ってから期限切れ寸前のタイムアウトで送信
    （無意味な予算消費・期限後の物理送信）をせず、待たずにその場で打ち切る。"""
    import email.message

    calls = []
    h = email.message.Message()
    h["Retry-After"] = "5"   # timeout（2秒）より大幅に長い待機を要求する

    def fake_post(url, headers, body, timeout=90):
        calls.append(1)
        raise _http_error(429, h)

    monkeypatch.setattr(A, "_post", fake_post)
    sleeps = []
    monkeypatch.setattr(A.time, "sleep", lambda sec: sleeps.append(sec))
    with pytest.raises(urllib.error.HTTPError):
        list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None, timeout=2))
    assert len(calls) == 1          # 待ってから2回目を送る、ということをしない
    assert sleeps == []             # 待機もしない（打ち切りが即座に決まる）


def test_send_checks_openai_io_guard_before_consuming_budget_on_retry(monkeypatch):
    """呼び出し予算・usage 計測は OpenAI 送信ガード（`assert_openai_io_allowed`）を通過した
    試行だけに課す（1物理送信=1消費の厳守）。ガードで弾かれた試行の分まで予算/usage を
    消費しない。"""
    calls = []

    def fake_post(url, headers, body, timeout=90):
        calls.append(1)
        raise _http_error(503)   # 常に再試行対象のエラー

    guard_calls = []

    def fake_guard():
        guard_calls.append(1)
        # `_send` は物理送信のたびにガードを確認する: 1回目（初回送信前）は通す・
        # 2回目（1回目の再試行前）で拒否する。
        if len(guard_calls) == 2:
            raise RuntimeError("OpenAI I/O is blocked")

    monkeypatch.setattr(A, "_post", fake_post)
    monkeypatch.setattr(A.llm, "assert_openai_io_allowed", fake_guard)
    budget = A._CallBudget(10)
    usage_acc = {"calls": 0, "tokens": None}
    with pytest.raises(RuntimeError, match="OpenAI I/O is blocked"):
        list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None,
                            call_budget=budget, usage_acc=usage_acc))
    assert len(calls) == 1          # 再試行の物理送信はガードで止まり発生しない
    # ガードで弾かれた試行の分は消費しない＝初回の1消費だけが残る（budget 9 残・usage_acc calls=1）。
    assert budget.remaining == 9
    assert usage_acc["calls"] == 1


def test_send_rechecks_deadline_after_actual_sleep_exceeds_planned_wait(monkeypatch):
    """実測の待機（`time.sleep`）が計画（Retry-After 由来の `wait`）より OS スケジューリング等で
    長引いた場合、事前チェック（計画上の wait だけを見る）をすり抜けても、sleep 直後に実測の
    残り時間を再検査し、最小送信猶予未満なら期限切れ寸前の物理送信をしない。"""
    import email.message

    calls = []
    h = email.message.Message()
    h["Retry-After"] = "1"   # 計画上の wait=1s（事前チェックは通る小さい値）

    def fake_post(url, headers, body, timeout=90):
        calls.append(1)
        raise _http_error(429, h)

    monkeypatch.setattr(A, "_post", fake_post)

    fake_now = [0.0]
    monkeypatch.setattr(A.time, "monotonic", lambda: fake_now[0])

    def fake_sleep(sec):
        fake_now[0] += sec + 8.5   # 実測は計画よりずっと長く延びる（想定した遅延）

    monkeypatch.setattr(A.time, "sleep", fake_sleep)
    with pytest.raises(urllib.error.HTTPError):
        list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None, timeout=10))
    assert len(calls) == 1   # sleep 直後の再検査で実測の延びを検知し、2回目は送らない


def test_send_aborted_reason_preserved_in_final_synthesis_tail(monkeypatch):
    """tail の最終合成（`dropped` 無しの通常経路）で `_send` が予算枯渇により中断されたとき
    （最終合成の初回送信は失敗し、再試行分の予算が無い）、最終 payload の `stop_reason` が
    `turns_exhausted` 等へ吸収されず実際の中断理由（`budget_exceeded`）を反映する。"""
    call_n = [0]

    def fake_post(url, headers, body, timeout=90):
        call_n[0] += 1
        if call_n[0] <= 2:   # 1・2ターン目はツール呼び出しを続けさせる（turns_exhausted で tail へ）
            return {"choices": [{"message": {"content": "", "tool_calls": [
                {"id": f"c{call_n[0]}",
                 "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]}
        # tail の最終合成・初回送信（3回目の物理送信）は一時的なエラーで失敗させる。この時点で
        # budget の残りは1（1・2ターン目の物理送信で2消費済み）なので、`_send` はこの初回送信自体は
        # 通すが、続く再試行の `_consume_call` に失敗し `_SendAborted("budget_exceeded")` を送出する。
        raise _http_error(503)

    monkeypatch.setattr(A, "_post", fake_post)
    # 3消費: 1ターン目・2ターン目の物理送信＋tail の最終合成の初回送信（いずれも `_send` が
    # 物理送信ごとに1消費する）。
    budget = A._CallBudget(3)
    events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None,
                                 max_turns=2, call_budget=budget))
    final = _final_of(events)
    assert final["stop_reason"] == "budget_exceeded"


def test_final_synthesis_tail_skips_label_node_when_budget_already_exhausted(monkeypatch):
    """通常 tail（`dropped` 無し・turns_exhausted 経路）に入った時点で予算が既に枯渇している
    場合、「ここまでに集めた資料で回答をまとめます」の node を yield しない。先に node だけ
    見せてから `_send` が `budget_exceeded` で即座に打ち切ると、「回答をまとめる」と予告だけ
    して実際には空の最終回答になる不整合な表示になるため。"""
    call_n = [0]

    def fake_post(url, headers, body, timeout=90):
        call_n[0] += 1
        return {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": f"c{call_n[0]}",
             "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]}

    monkeypatch.setattr(A, "_post", fake_post)
    budget = A._CallBudget(1)   # 1ターン目の物理送信だけで枯渇＝tail の初回送信には全く回らない
    events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None,
                                 max_turns=1, call_budget=budget))
    assert not any((ev.get("node") or {}).get("label") == "調査の上限に到達" for ev in events)
    final = _final_of(events)
    assert final["stop_reason"] == "budget_exceeded"


def test_tail_synthesis_stop_abort_yields_no_final_payload(monkeypatch):
    """tail の最終合成（`dropped` 無し・turns_exhausted 経路）中に `stop_event` が立って
    `_SendAborted("stop")` になった場合、既存の「停止時は final を出さない」契約（ターンループ側の
    同型分岐と同じ）に合わせ、final payload を一切 yield しない。"""
    post_calls = []

    def fake_post(url, headers, body, timeout=90):
        post_calls.append(1)
        if len(post_calls) == 1:   # 1ターン目はツール呼び出しを続けさせ turns_exhausted で tail へ
            return {"choices": [{"message": {"content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]}
        raise AssertionError("stop 検知後に2回目の物理送信をしてしまった")

    monkeypatch.setattr(A, "_post", fake_post)

    is_set_calls = [0]

    class _StopAfter:
        """`stop_event.is_set()` はターンループ内で複数箇所から呼ばれる（ターンループ自身の
        冒頭・そのターンの `_send`・ツール実行直前・ツール node yield 直後）ため、最初の4回は
        すべて False にして1ターン目の tool_calls 応答とそのツール実行（ripgrep_search）まで
        確実に進ませる。5回目（tail 冒頭の確認）も False にし、tail の最終合成へ実際に到達させた
        うえで、6回目（tail 内の `_send`）から True にする（実運用で起こりうる狭い競合窓を模す・
        分岐を削除すると本テストは red になることを確認済み）。"""

        def is_set(self):
            is_set_calls[0] += 1
            return is_set_calls[0] > 5

    events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None,
                                 stop_event=_StopAfter(), max_turns=1))
    assert not any("final" in ev for ev in events)   # final を一切出さない
    assert len(post_calls) == 1                      # tail 側の物理送信はガードで止まり発生しない


def test_run_evaluation_does_not_retry_on_timeout(monkeypatch):
    """`_run_evaluation` はナッジを変えて最大2回まで試すが、応答タイムアウトは非リトライの
    全体契約（`_is_timeout_error`）に合わせ、2回目の試行（ナッジでの再試行）をしない
    （上流で処理/課金が既に進んでいる可能性があるため）。"""
    calls = []

    def fake_post(url, headers, body, timeout=90):
        calls.append(1)
        raise TimeoutError("timed out")

    monkeypatch.setattr(A, "_post", fake_post)
    out = A._run_evaluation(
        "https://api.openai.com/v1/chat/completions", {"Authorization": "Bearer x"},
        "gpt-5.5", [{"role": "user", "content": "hi"}], ollama=False, timeout=5,
        usage=A._new_usage_acc(), usage_acc=None)
    assert len(calls) == 1   # ナッジでの2回目の再試行はしない
    assert out["status"] == "blocked"
    assert out["evaluation_failed"] is True
    assert "2回" not in out["reason"]   # 実際の試行回数（1回）に文言を一致させる


def test_run_evaluation_retries_with_nudge_on_non_timeout_failure(monkeypatch):
    """タイムアウト以外（不正応答等）は従来どおり2回まで試す（回帰確認）。"""
    calls = []

    def fake_post(url, headers, body, timeout=90):
        calls.append(1)
        return {"choices": [{"message": {"content": "not a tool call"}}]}   # 検証失敗＝再試行対象

    monkeypatch.setattr(A, "_post", fake_post)
    out = A._run_evaluation(
        "https://api.openai.com/v1/chat/completions", {"Authorization": "Bearer x"},
        "gpt-5.5", [{"role": "user", "content": "hi"}], ollama=False, timeout=5,
        usage=A._new_usage_acc(), usage_acc=None)
    assert len(calls) == 2
    assert out["status"] == "blocked"
    assert "2回" in out["reason"]   # 実際に2回試したときはそのまま「2回失敗しました」でよい


def test_run_evaluation_checks_openai_io_guard_before_consuming_budget(monkeypatch):
    """`_run_evaluation` も `_send` と同じ順序（ガード→予算消費/usage 加算）で確認する。
    ガードで拒否された試行の分は予算・usage を消費しない。ガード失敗は `_send` と同じ契約
    （別ナッジでの飲み込み再試行はせず、そのまま呼び出し元へ伝播する）。"""
    calls = []

    def fake_post(url, headers, body, timeout=90):
        calls.append(1)
        return {"choices": [{"message": {"content": "not a tool call"}}]}

    def fake_guard():
        raise RuntimeError("OpenAI I/O is blocked")

    monkeypatch.setattr(A, "_post", fake_post)
    monkeypatch.setattr(A.llm, "assert_openai_io_allowed", fake_guard)
    budget = A._CallBudget(10)
    usage_acc = {"calls": 0, "tokens": None}
    with pytest.raises(RuntimeError, match="OpenAI I/O is blocked"):
        A._run_evaluation(
            "https://api.openai.com/v1/chat/completions", {"Authorization": "Bearer x"},
            "gpt-5.5", [{"role": "user", "content": "hi"}], ollama=False, timeout=5,
            usage=A._new_usage_acc(), usage_acc=usage_acc, call_budget=budget)
    assert calls == []               # 物理送信は一度も発行されていない
    assert budget.remaining == 10    # ガードで弾かれた試行の分は消費しない
    assert usage_acc["calls"] == 0


def test_run_evaluation_guard_rejection_on_retry_does_not_consume_extra_budget(monkeypatch):
    """`_run_evaluation` はガード確認→予算消費→usage 加算→送信を1つの塊にする（`_send` と同じ
    「1物理送信=1消費」契約）。1回目の試行は正常に送信・消費し（不正応答のため再試行対象になる）、
    2回目の試行の直前でガードが拒否したら、その2回目の分は物理送信も予算・usage 消費も一切
    発生しない（ガードは常に消費より前に確認する＝消費してから拒否される窓を作らない）。"""
    calls = []

    def fake_post(url, headers, body, timeout=90):
        calls.append(1)
        return {"choices": [{"message": {"content": "not a tool call"}}]}   # 検証失敗＝再試行対象

    guard_calls = []

    def fake_guard():
        guard_calls.append(1)
        if len(guard_calls) == 2:   # 1回目（初回試行）は通す・2回目（再試行）で拒否する
            raise RuntimeError("OpenAI I/O is blocked")

    monkeypatch.setattr(A, "_post", fake_post)
    monkeypatch.setattr(A.llm, "assert_openai_io_allowed", fake_guard)
    budget = A._CallBudget(5)
    usage_acc = {"calls": 0, "tokens": None}
    with pytest.raises(RuntimeError, match="OpenAI I/O is blocked"):
        A._run_evaluation(
            "https://api.openai.com/v1/chat/completions", {"Authorization": "Bearer x"},
            "gpt-5.5", [{"role": "user", "content": "hi"}], ollama=False, timeout=5,
            usage=A._new_usage_acc(), usage_acc=usage_acc, call_budget=budget)
    assert len(calls) == 1           # 1回目だけ物理送信・2回目はガードで止まり発生しない
    assert budget.remaining == 4     # 1回目の消費だけ（2回目はガード拒否で未消費）
    assert usage_acc["calls"] == 1


def test_openai_style_tool_loop_survives_transient_failure_mid_loop_without_losing_evidence(
        monkeypatch):
    """途中失敗（2ターン目の通信が一時的に失敗）でも、それまでの調査結果（1ターン目の tool_calls
    で集めた根拠）を破棄せず、同一プロバイダ内の再試行だけでループを継続できる
    （黙って別プロバイダへは切り替えない・それまでの調査結果を即座に破棄しない）。"""
    responses = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        _http_error(500),   # 2ターン目の発行が一時的に失敗（transient）
        {"choices": [{"message": {"content": "最終回答"}}]},
    ]

    def fake_post(url, headers, body, timeout=90):
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(A, "_post", fake_post)
    events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None))
    final = _final_of(events)
    assert final["final"] == "最終回答"
    assert final["searched"] is True    # 1ターン目の tool_calls 結果は破棄されず引き継がれている
    assert responses == []              # 全レスポンスを消費＝ループが継続できた
