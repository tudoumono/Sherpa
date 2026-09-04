"""T2（2026-08-18・実機報告⑥の隣接ケース）: Codex CLI はあるが認証が無い等で
`codex exec` が「即座に非ゼロ終了・agent_message を1つも出さない」場合の応答を固定する。

背景: 閉域キットが Codex CLI を同梱するようになった（scripts/install_offline_kit.sh 7b）ため、
「CLI は PATH にあるが認証されていない」（OPENAI_API_KEY 未設定・auth.json 無し/古い/無効）状態が
現実的になった。この時 `shutil.which("codex")` は真を返すので `_select_provider`（閉域実機報告⑥の
既存是正・CLI 不在時の `_UnwiredProvider` 化）は素通りし、`CodexProvider` が組み立てられて
`codex exec` が起動される。実測（2026-08-18・T1）: 認証エラー相当で即 exit 1・stdout に JSON を
1行も出さない偽 codex を差し込むと、`CodexProvider._run_authoring` は従来 `_gather` が組み立てた
決定的回答（env["headline"]）をそのまま返しており、利用者からは「Codex が実際に応答したのか」
区別が付かなかった（報告⑥と同じ体験が別経路で残っていた）。

本ファイルはこの隣接ケースへの是正（provider.py `_codex_silent_failure`）を固定する。
既存 tests/unit/test_codex_kill_timeout.py・test_codex_resume.py と同じ「偽 codex 実行ファイルを
PATH に差し込む」流儀（実 codex は一切呼ばない）。
"""
from __future__ import annotations

import logging
import os
import stat
import threading
import time
from pathlib import Path

# test_codex_kill_timeout.py と同じ流儀（setdefault のみ・モジュールレベル直書きは pytest 一括収集時に
# プロセス全体へ漏れるため禁止）。
os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

from sherpa import agents as A  # noqa: E402


def _ctx(uid: str, stop_event=None) -> "A.Ctx":
    """DB 不要な最小 Ctx（route/dispatch を固定ラムダにし、_gather の実処理だけ本物を通す）。
    dispatch の headline は「決定的回答」の目印にする文字列（本物の env["headline"] とすり替わって
    いないかを検証する対照値）。"""
    return A.Ctx(
        message="偽 codex 無出力失敗テスト",
        world="v1",
        route=lambda msg: {"lens": "qa", "input": msg, "reason": "test", "confident": True},
        dispatch=lambda lens_, inp: {
            "lens": lens_, "headline": "dispatch-headline-should-not-leak-as-real-answer",
            "summary": {"total": 0}, "data": {}, "sources": [],
        },
        knowledge=True,
        uid=uid,
        stop_event=stop_event,
    )


def _setup(tmp_path: Path, monkeypatch, users_dirname: str = "users") -> Path:
    """PATH に偽 codex を挿し込み、SHERPA_USERS_DIR を隔離する共通セットアップ。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / users_dirname))
    monkeypatch.setenv("SHERPA_CODEX_TIMEOUT", "30")
    return bin_dir


def _write_fake_codex(bin_dir: Path, script_body: str) -> None:
    script = bin_dir / "codex"
    script.write_text(script_body)
    mode = script.stat().st_mode
    script.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run(prov, ctx) -> list:
    return list(prov.run(ctx))


def _result_env(events: list) -> dict:
    results = [e for e in events if isinstance(e, dict) and e.get("type") == "_result"]
    assert len(results) == 1, f"_result が1件でない: {events!r}"
    return results[0]["env"]


# ===== (1) 即 exit 1・stdout 空（認証エラー相当）→ 決定的回答を返さず正直な未接続文言に切替 =====

def test_zero_output_nonzero_exit_returns_honest_message_not_deterministic_answer(tmp_path, monkeypatch):
    bin_dir = _setup(tmp_path, monkeypatch, users_dirname="users_authfail")
    _write_fake_codex(bin_dir, (
        "#!/bin/bash\n"
        "echo 'Error: No API key or auth.json found. Run `codex login --with-api-key`.' 1>&2\n"
        "exit 1\n"
    ))
    prov = A.CodexProvider()
    ctx = _ctx(uid="authfail-u1")

    events = _run(prov, ctx)
    env = _result_env(events)

    # 決定的回答（_gather の dispatch headline）をそのまま answer_delta / _result に出していないこと。
    assert env["headline"] != "dispatch-headline-should-not-leak-as-real-answer", (
        f"認証エラーで無応答なのに決定的回答をそのまま返している（報告⑥と同じ体験が残っている）: {env!r}")
    # 利用者に「AI が答えていない」ことが伝わる文言であること（_UnwiredProvider と同じ「正直に伝える」語彙）。
    assert "接続できません" in env["headline"], f"未接続だと分かる文言になっていない: {env!r}"

    answer_deltas = [e for e in events if isinstance(e, dict) and e.get("type") == "answer_delta"]
    assert len(answer_deltas) == 1
    assert answer_deltas[0]["text"] == env["headline"], "answer_delta と _result の headline が食い違っている"

    # ストリーミング契約: node → answer_delta → _result の順序自体は変えない。
    types = [e.get("type") for e in events if isinstance(e, dict)]
    assert types[-2:] == ["answer_delta", "_result"], f"末尾の並びが崩れている: {types!r}"
    assert types.count("_result") == 1


# ===== (2) stop_event による打ち切りは「失敗」ではない＝決定的回答フォールバックは維持 =====

def test_stopped_before_any_output_keeps_deterministic_fallback(tmp_path, monkeypatch):
    """ユーザーが即座に停止した場合、codex が無応答でも「認証エラーらしき」文言は出さない
    （stop は失敗ではないため・既存の決定的回答フォールバックのまま）。"""
    bin_dir = _setup(tmp_path, monkeypatch, users_dirname="users_stopped")
    monkeypatch.setenv("SHERPA_CODEX_TIMEOUT", "120")   # timeout が先に発火しないよう十分大きく
    # stdout には何も書かず、stop 監視スレッドに検知される前に少し待ってから終了する
    # （_spawn_stop_watcher は 0.3秒間隔でポーリング）。
    _write_fake_codex(bin_dir, "#!/bin/bash\nsleep 5\n")

    prov = A.CodexProvider()
    stop_event = threading.Event()
    ctx = _ctx(uid="stopped-u1", stop_event=stop_event)

    events: list = []

    def _drive():
        events.extend(prov.run(ctx))

    th = threading.Thread(target=_drive, daemon=True)
    th.start()
    time.sleep(0.05)          # 偽 codex が起動し stdout 空のまま待っている状態を作る
    stop_event.set()          # 直後に停止要求
    th.join(timeout=20)
    assert not th.is_alive(), "stop_event 経路が想定時間内に完走しない"

    env = _result_env(events)
    assert "接続できません" not in env["headline"], (
        f"ユーザー停止なのに未接続失敗の文言になっている（stop は失敗ではない）: {env!r}")
    assert env["headline"] == "dispatch-headline-should-not-leak-as-real-answer", (
        f"stop 経路は既存の決定的回答フォールバックのままのはず: {env!r}")


# ===== (3) 一部出力はあるが agent_message が無いまま失敗 → 既存の決定的回答フォールバックのまま =====

# ===== (2.5) timeout kill による無出力失敗 → 文言に「タイムアウトしました」が反映される =====
# RV MED（2026-08-18 Codex RV 指摘4）: 以前は無出力失敗を一律「認証されていない可能性」と断定していたが、
# timeout kill・プロキシ/CA 不備・sandbox 起動失敗・CLI クラッシュのいずれでも同じ経路に来る（閉域では
# むしろプロキシ/ネットワーク要因の方が現実的）。判別できる材料（killer が実際に発火したか）は反映する。

def test_timeout_kill_with_no_output_mentions_timeout_not_auth_only(tmp_path, monkeypatch):
    """test_codex_kill_timeout.py と同じ「小さい SHERPA_CODEX_TIMEOUT ＋ 長時間 sleep する偽 codex」
    方式だが、こちらは stdout に**一切出力しない**版（got_any_line=False のまま）で無出力失敗の
    判定条件（(1)と同じ）を満たしつつ、原因が「即終了」ではなく「timeout」であることを区別する。"""
    bin_dir = _setup(tmp_path, monkeypatch, users_dirname="users_timeout")
    monkeypatch.setenv("SHERPA_CODEX_TIMEOUT", "1")   # _setup の既定(30)を上書き＝すぐ timeout させる
    _write_fake_codex(bin_dir, "#!/bin/bash\nsleep 30\n")   # 何も出力せず timeout まで待つだけ

    prov = A.CodexProvider()
    ctx = _ctx(uid="timeout-u1")

    env = _result_env(_run(prov, ctx))
    assert "接続できません" in env["headline"], f"未接続だと分かる文言になっていない: {env!r}"
    assert "タイムアウトしました" in env["headline"], (
        f"timeout kill 経路なのに区別が文言に反映されていない: {env!r}")
    # 認証だけを原因と決め打ちしない（複数の可能性を挙げる・原因は断定しない）。
    assert "可能性があります" not in env["headline"], (
        f"以前の断定的な文言（認証されていない可能性があります）が残っている: {env!r}")


def test_partial_tool_output_then_failure_keeps_deterministic_fallback(tmp_path, monkeypatch):
    """got_any_line=True（何らかの JSON は出た）だが agent_message が無いまま非ゼロ終了する場合は、
    今回の是正の対象外（`not got_any_line` を満たさない）＝既存の決定的回答フォールバックのまま。
    今回の是正が「stdout 完全に空」より広く効きすぎていないかの境界線を固定する。"""
    bin_dir = _setup(tmp_path, monkeypatch, users_dirname="users_partial")
    _write_fake_codex(bin_dir, (
        "#!/bin/bash\n"
        "echo '{\"type\":\"item.completed\",\"item\":{\"id\":\"c1\",\"type\":\"command_execution\","
        "\"command\":\"grep -r foo bar\",\"status\":\"completed\",\"exit_code\":1}}'\n"
        "exit 1\n"
    ))
    prov = A.CodexProvider()
    ctx = _ctx(uid="partial-u1")

    env = _result_env(_run(prov, ctx))

    assert env["headline"] == "dispatch-headline-should-not-leak-as-real-answer", (
        f"部分出力ケースは既存の決定的回答フォールバックのままのはず: {env!r}")


# ===== (4) 不採用の付帯条件（2026-08-18・Codex RV 2巡目）: HTTPS_PROXY の userinfo が漏れないこと =====
# RV の「プロキシ認証情報を Codex に渡さない案」は不採用（渡さないと閉域＋プロキシ環境で Codex 自体が
# 通信できなくなる＝実機報告⑪の是正目的そのもの・sandbox.py `_CODEX_PASSTHROUGH_ENV` 参照）。ただし
# 「ログ・診断・画面に出さない」ことは重要という付帯条件だけをテストで固定する（コード変更はしない・
# 現状ですでに満たされているはずの契約を将来のリグレッションから守るための安全網）。

def test_https_proxy_userinfo_never_leaks_into_headline_or_log(tmp_path, monkeypatch, caplog):
    """`HTTPS_PROXY=http://user:pass@host:port` を立てた状態で無出力失敗（T2 の(1)と同じ経路）を
    起こしても、userinfo（ユーザー名/パスワード）が利用者向け文言（env["headline"]）にも
    ロガー出力（`sherpa.providers.codex.provider` の `_log.warning` 等）にも現れないこと。
    `_codex_clean_env` がプロキシ値をそのまま子プロセス env へ渡す仕様（不採用・意図どおり）とは
    別に、値そのものがログ経由で漏れないことだけを固定する。"""
    bin_dir = _setup(tmp_path, monkeypatch, users_dirname="users_proxyleak")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxyuser:proxysecret123@proxy.internal:8080")
    _write_fake_codex(bin_dir, (
        "#!/bin/bash\n"
        "echo 'Error: No API key or auth.json found.' 1>&2\n"
        "exit 1\n"
    ))
    prov = A.CodexProvider()
    ctx = _ctx(uid="proxyleak-u1")

    with caplog.at_level(logging.DEBUG):
        events = _run(prov, ctx)
    env = _result_env(events)

    assert "proxysecret123" not in env["headline"]
    assert "proxyuser" not in env["headline"]
    for record in caplog.records:
        msg = record.getMessage()
        assert "proxysecret123" not in msg, f"ログに userinfo（パスワード）が漏れている: {msg!r}"
        assert "proxyuser" not in msg, f"ログに userinfo（ユーザー名）が漏れている: {msg!r}"


# ===== RV2 #2: honest failure は data も空にして通常の0件検索結果と区別する =====
# `_ctx()` の固定フェイク dispatch は元々 data:{}/sources:[] を返すため、data のクリア自体を
# 検証できない（既にクリアされた形と区別が付かない）。ここでは dispatch が「実際に検索はしたが
# 0件」だった場合の env 形（citations 等のキーを持つ非空 data・sources は空）を模し、Codex の
# 無出力失敗（honest failure）がそれを `chat_service._no_genuine_results` の規約（data={}）へ
# 揃えることを確認する。

def _ctx_with_zero_hit_search_shape(uid: str) -> "A.Ctx":
    return A.Ctx(
        message="偽 codex 無出力失敗テスト（検索0件形）",
        world="v1",
        route=lambda msg: {"lens": "qa", "input": msg, "reason": "test", "confident": True},
        dispatch=lambda lens_, inp: {
            "lens": lens_, "headline": "dispatch-headline-should-not-leak-as-real-answer",
            "summary": {"total": 0}, "data": {"type": "qa", "citations": []}, "sources": [],
        },
        knowledge=True,
        uid=uid,
    )


def test_zero_output_failure_clears_data_when_sources_empty(tmp_path, monkeypatch):
    """dispatch が「検索はしたが0件」の env 形（data 非空・sources 空）を返していても、
    無出力失敗（honest failure）は data を `{}` にする——`_no_genuine_results` が真になり
    `_retry_hints`/確定文言が誤って付かないようにするため。"""
    bin_dir = _setup(tmp_path, monkeypatch, users_dirname="users_authfail_datashape")
    _write_fake_codex(bin_dir, (
        "#!/bin/bash\n"
        "echo 'Error: No API key or auth.json found. Run `codex login --with-api-key`.' 1>&2\n"
        "exit 1\n"
    ))
    prov = A.CodexProvider()
    ctx = _ctx_with_zero_hit_search_shape(uid="authfail-u2")

    env = _result_env(_run(prov, ctx))
    assert "接続できません" in env["headline"]
    assert env["data"] == {}, f"honest failure なのに data が実検索結果のまま: {env!r}"

    from sherpa import chat_service as CS
    assert CS._no_genuine_results(env) is False
    finalized = CS._finalize(dict(env), {"lens": "qa", "reason": "Codex 未接続"})
    assert "retry_hints" not in finalized
    assert finalized["headline"] == env["headline"], "headline が確定文言へ誤って置換されている"
