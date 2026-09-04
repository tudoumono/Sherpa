"""Codex CLI タイムアウトの継続注記（`env["codex_timed_out"]`・resume 案内ボタン）。

背景（実環境観測 2026-09-02）: Codex 構成のチャットでタイムアウトに当たると、その時点までの
出力（「次に○○します」のような途中経過宣言）がそのまま回答として確定する。Codex セッション
（R1b resume）自体は生きているため「続きを調べて」と送ると続きから再開できる——これに利用者が
気づけるよう、`providers/codex/provider.py::_run_authoring` が機械的事実（threading.Timer に
よる kill・文面マッチはしない）だけを根拠に `env["codex_timed_out"]` を立て、
`chat_service._finalize` が STOP-1/SC-6d と同形式（headline 直下の独立注記＋案内ボタン）で
envelope に載せる。本文（headline）自体は書き換えない。

既存 tests/unit/test_codex_kill_timeout.py と同じ「偽 codex 実行ファイルを PATH に差し込む」
流儀（実 codex は一切呼ばない）。フラグは「本文（agent_message）が残る/残らないタイムアウト」の
両方で立ち（`_run_authoring` の `if answer:`/`else:` 両分岐）、`attempt_returncode != 0` も
併せて要求する（EOF 直後の Timer 発火と正常終了の競合を除外・立ちすぎ防止）。フラグが立つ経路
（本文あり timeout／本文なし timeout）・立たない経路（正常完了／returncode 0 での Timer 発火）の
各1本に加え、`chat_service._finalize` が retry_hints/headline へ載せる形を固定する。
"""
from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

# test_codex_kill_timeout.py と同じ流儀（setdefault のみ・モジュールレベル直書きは pytest 一括
# 収集時にプロセス全体へ漏れるため禁止）。
os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

from sherpa import agents as A  # noqa: E402
from sherpa import chat_service as CS  # noqa: E402

_SLEEP_SECONDS = 120


def _ctx(uid: str) -> "A.Ctx":
    """DB 不要な最小 Ctx（route/dispatch を固定ラムダにし、_gather の実処理だけ本物を通す）。"""
    return A.Ctx(
        message="Codex タイムアウト継続注記テスト",
        world="v1",
        route=lambda msg: {"lens": "qa", "input": msg, "reason": "test", "confident": True},
        dispatch=lambda lens_, inp: {
            "lens": lens_, "headline": "dispatch-headline",
            "summary": {"total": 0}, "data": {}, "sources": [],
        },
        knowledge=True,
        uid=uid,
    )


def _write_slow_fake_codex(bin_dir: Path) -> None:
    """timeout 前提: 途中経過宣言（agent_message の item.completed）を1行出してから長時間 sleep
    する（自然終了させない・test_codex_kill_timeout.py と同じ流儀）。"""
    script = bin_dir / "codex"
    script.write_text(
        "#!/bin/bash\n"
        "echo '{\"type\":\"item.completed\",\"item\":{\"id\":\"1\",\"type\":\"agent_message\","
        "\"text\":\"次に資料を確認します。\"}}'\n"
        f"sleep {_SLEEP_SECONDS}\n"
    )
    mode = script.stat().st_mode
    script.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _write_fast_fake_codex(bin_dir: Path) -> None:
    """正常完了: 結論の agent_message を出してすぐ exit 0（timeout に当たらない）。"""
    script = bin_dir / "codex"
    script.write_text(
        "#!/bin/bash\n"
        "echo '{\"type\":\"item.completed\",\"item\":{\"id\":\"1\",\"type\":\"agent_message\","
        "\"text\":\"資料を確認したところ、該当箇所が見つかりました。\"}}'\n"
    )
    mode = script.stat().st_mode
    script.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _write_command_only_fake_codex(bin_dir: Path) -> None:
    """timeout 前提だが agent_message が1つも無い: command_execution だけ出して長時間 sleep
    する（`got_any_line=True` だが結論の本文が無いまま打ち切られるケース）。"""
    script = bin_dir / "codex"
    script.write_text(
        "#!/bin/bash\n"
        "echo '{\"type\":\"item.completed\",\"item\":{\"id\":\"c1\",\"type\":\"command_execution\","
        "\"command\":\"grep -r foo bar\",\"status\":\"completed\",\"exit_code\":0}}'\n"
        f"sleep {_SLEEP_SECONDS}\n"
    )
    mode = script.stat().st_mode
    script.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _setup(tmp_path: Path, monkeypatch, write_fake, users_dirname: str, timeout: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_fake(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / users_dirname))
    monkeypatch.setenv("SHERPA_CODEX_TIMEOUT", timeout)
    return bin_dir


def _result_env(events: list) -> dict:
    results = [e for e in events if isinstance(e, dict) and e.get("type") == "_result"]
    assert len(results) == 1, f"_result が1件でない: {events!r}"
    return results[0]["env"]


# ===== フラグが立つ経路: timeout kill で進行中の宣言文が headline に残る =====

def test_timeout_with_partial_answer_sets_codex_timed_out_flag(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, _write_slow_fake_codex, "users_timeout", "1.0")
    prov = A.CodexProvider()
    ctx = _ctx(uid="codex-timeout-u1")

    events: list = []

    def _drive():
        for ev in prov.run(ctx):
            events.append(ev)

    th = threading.Thread(target=_drive, daemon=True)
    th.start()
    th.join(timeout=20)
    assert not th.is_alive(), "timeout 経路が想定時間内に完走しない（Timer→_killpg が効いていない疑い）"

    env = _result_env(events)
    assert env.get("headline") == "次に資料を確認します。"   # 本文は書き換えない（宣言文そのまま）
    assert env.get("codex_timed_out") is True


# ===== フラグが立たない経路: 正常完了（timeout に当たらない） =====

def test_normal_completion_does_not_set_codex_timed_out_flag(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, _write_fast_fake_codex, "users_normal", "30")
    prov = A.CodexProvider()
    ctx = _ctx(uid="codex-timeout-u2")

    env = _result_env(list(prov.run(ctx)))
    assert env.get("headline") == "資料を確認したところ、該当箇所が見つかりました。"
    assert not env.get("codex_timed_out")


# ===== フラグが立つ経路: 結論の agent_message が無いまま timeout 打ち切り（本文ゼロ）=====

def test_timeout_with_no_agent_message_still_sets_codex_timed_out_flag(tmp_path, monkeypatch):
    """command_execution は実行できたが結論の agent_message が無いまま timeout で打ち切られた
    ケース——`_codex_silent_failure`（JSON を1行も出さない完全な沈黙）には該当しない
    （`got_any_line=True`）ため、従来は else 分岐で注記が一切付かなかった。"""
    _setup(tmp_path, monkeypatch, _write_command_only_fake_codex, "users_timeout_noanswer", "1.0")
    prov = A.CodexProvider()
    ctx = _ctx(uid="codex-timeout-u3")

    events: list = []

    def _drive():
        for ev in prov.run(ctx):
            events.append(ev)

    th = threading.Thread(target=_drive, daemon=True)
    th.start()
    th.join(timeout=20)
    assert not th.is_alive(), "timeout 経路が想定時間内に完走しない"

    env = _result_env(events)
    assert env.get("codex_timed_out") is True


# ===== フラグが立ちすぎない経路: Timer 発火時には既に正常終了（returncode 0）していた競合 =====

class _FiresOnCancelTimer:
    """`threading.Timer` の代役（テスト専用）。実運用では「stdout の for ループが EOF で
    自然終了した直後・`killer.cancel()` を呼ぶ前のわずかな窓」で Timer が発火する競合が
    起こりうるが、実時間のスレッドでは再現がタイミング依存で不安定になる。この代役は
    `start()` を no-op にし、`cancel()`（＝本物なら for ループ終了後に呼ばれる箇所）が
    呼ばれた瞬間に `function`（`_on_timeout`）を実行してから通常どおりキャンセル済みにする
    ことで、「EOF 後・cancel と同時に発火してしまった」場合の状態を決定的に再現する。
    このとき対象プロセスは既に exit 0 で完了済みのため、`_killpg`（SIGKILL）は
    既に居ないプロセス（グループ）への no-op になり、`proc.wait()` が拾う returncode は
    影響を受けない（＝0 のまま）。"""

    def __init__(self, interval, function, *args, **kwargs):
        self._function = function

    def start(self):
        pass

    def cancel(self):
        self._function()


def test_timer_fires_after_normal_exit_does_not_set_flag(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, _write_fast_fake_codex, "users_race", "30")
    monkeypatch.setattr(threading, "Timer", _FiresOnCancelTimer)
    prov = A.CodexProvider()
    ctx = _ctx(uid="codex-timeout-u4")

    env = _result_env(list(prov.run(ctx)))
    assert env.get("headline") == "資料を確認したところ、該当箇所が見つかりました。"
    assert not env.get("codex_timed_out"), (
        "returncode 0（正常終了）なのに codex_timed_out が立っている（Timer 発火だけで"
        "判定していた旧ロジックの回帰）")


# ===== chat_service._finalize: envelope へ載る形（resume 案内・headline 保護）=====

def _env(sources=("doc1",), data=None, headline="次に資料を確認します。") -> dict:
    return {
        "sources": list(sources),
        "scope": {"scope_paths": [], "layer": "both", "layer_applied": True},
        "data": data if data is not None else {"citations": list(sources)},
        "headline": headline,
        "codex_timed_out": True,
    }


def test_finalize_appends_resume_retry_hint_for_codex_timeout():
    out = CS._finalize(_env(), {"lens": "qa", "reason": "既定（検索）"})
    assert {"kind": "resume", "label": "続きを調べる",
            "action": {"message": "続きを調べて"}} in out["retry_hints"]


def test_finalize_keeps_partial_headline_when_codex_timed_out_even_with_zero_sources():
    """出典0件・全軸が最も緩い設定でも、Codex タイムアウトの途中結果は「見つからなかった」
    確定文言（`_NO_RESULTS_EVEN_AT_LOOSEST_HEADLINE`）へ置換しない（STOP-1 と同型の保護）。"""
    env = _env(sources=(), data={"citations": []})
    out = CS._finalize(env, {"lens": "qa", "reason": "既定（検索）"})
    assert out["headline"] == "次に資料を確認します。"
    assert out["headline"] != CS._NO_RESULTS_EVEN_AT_LOOSEST_HEADLINE
    assert any(h["kind"] == "resume" for h in out["retry_hints"])


def test_finalize_does_not_set_flag_or_hint_for_normal_turn():
    env = _env()
    env["codex_timed_out"] = False
    out = CS._finalize(env, {"lens": "qa", "reason": "既定（検索）"})
    assert "retry_hints" not in out
