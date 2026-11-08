"""つながりの確認の「取り込みの係」の経路（`ingest/connectivity_relay.py`）と、Codex の確認が本物のターンと同じ起動を共有することの確認。
外部の境界（DB・HTTP・子プロセス・時計）だけ差し替える。
"""
from __future__ import annotations

import json
import os
import urllib.error

import pytest

from sherpa import connectivity, embeddings, health, llm, store
from sherpa.ingest import connectivity_relay as relay
from sherpa.ingest import worker_status

KEY = "sk-test-SECRETKEY-0123456789"


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SHERPA_INGEST_STATE_FILE", str(tmp_path / "ingest_worker_active"))
    return tmp_path


@pytest.fixture
def boundaries(monkeypatch):
    """DB・HTTP の境界。OpenAI への送信は `llm.post_json` と埋め込みの送信として数える。"""
    sent = []
    sys_s = {"cloud_provider": "openai", "openai_api_key": KEY, "embed_provider": "auto",
             "research_default_provider": "openai"}
    monkeypatch.setattr(store, "get_system_settings", lambda **k: dict(sys_s))
    monkeypatch.setattr(health, "_ping_postgres", lambda: None)
    monkeypatch.setattr(health, "_ping_neo4j", lambda: None)
    monkeypatch.setattr(health, "ollama_in_use", lambda s, rows=None: False)
    monkeypatch.setattr(llm, "assert_openai_io_allowed", lambda: None)
    monkeypatch.delenv("SHERPA_DISABLE_EMBED", raising=False)
    monkeypatch.setenv("ES_URL", "http://localhost:9200")

    def refuse(*a, **k):
        raise urllib.error.URLError(ConnectionRefusedError())
    monkeypatch.setattr("urllib.request.urlopen", refuse)

    def post_json(url, headers, body, timeout=90):
        sent.append(url)
        raise urllib.error.HTTPError(url, 401, "bad", {}, None)
    monkeypatch.setattr(llm, "post_json", post_json)
    monkeypatch.setattr(embeddings, "embed", lambda texts, c, **k: sent.append("embed") or None)
    return sent


def _drain(r: relay.Relay) -> None:
    if r._thread is not None:
        r._thread.join(timeout=10)


def test_worker_runs_a_request_once_and_writes_the_result(run_dir, monkeypatch):
    """同じ識別子の頼みは 2 度流さない。結果は同じ識別子で書く。"""
    calls = []
    monkeypatch.setattr(connectivity, "run_checks",
                        lambda paid, path="api", uid=None: calls.append((paid, path)) or [{"id": "x"}])
    rid = relay.write_request(True)
    r = relay.Relay()
    r.poll(); _drain(r)
    r.poll(); _drain(r)
    assert calls == [(True, "ingest")]
    assert relay.read_result(rid) == [{"id": "x"}]
    new = relay.write_request(False)
    r.poll(); _drain(r)
    assert calls == [(True, "ingest"), (False, "ingest")] and relay.read_result(new) == [{"id": "x"}]


def test_worker_ignores_a_stale_request(run_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(connectivity, "run_checks", lambda *a, **k: calls.append(1) or [])
    rid = relay.write_request(False)
    monkeypatch.setattr(relay, "_now", lambda: relay.time.time() + relay.REQUEST_TTL_S + 5)
    r = relay.Relay()
    r.poll(); _drain(r)
    assert calls == [] and relay.read_result(rid) is None


def test_no_worker_is_incomplete_at_once_without_writing_a_request(run_dir, monkeypatch):
    monkeypatch.setattr(worker_status, "live_workers", lambda: [])
    rows = relay.request_and_wait(False, sleep=lambda s: pytest.fail("待たない"))
    assert rows and all(x["status"] == "incomplete" and "取り込みの係が動いていない" in x["reason"] for x in rows)
    assert not (run_dir / "connectivity_request.json").exists()


def test_no_reply_times_out_as_incomplete(run_dir, monkeypatch):
    monkeypatch.setattr(worker_status, "live_workers", lambda: [{"state": "idle"}])
    rows = relay.request_and_wait(False, wait_s=0.05, sleep=lambda s: None)
    assert rows and all(x["status"] == "incomplete" and "返事が無い" in x["reason"] for x in rows)
    assert (run_dir / "connectivity_request.json").exists()


def test_wait_limit_covers_beat_check_and_margin():
    assert relay.wait_limit_s() == relay.worker_state.BEAT_SEC + relay.CHECK_LIMIT_S + relay.WAIT_MARGIN_S


def test_ingest_path_runs_embed_but_not_answer_ai_and_free_run_sends_nothing(run_dir, boundaries, monkeypatch):
    """API の頼みから、ワーカー側が実際の確認を流して返す往復。回答の AI は流さず、無料なら OpenAI へ送らない。"""
    monkeypatch.setattr(worker_status, "live_workers", lambda: [{"state": "idle"}])
    worker = relay.Relay()

    def sleep(_s):
        worker.poll(); _drain(worker)
    rows = relay.request_and_wait(False, wait_s=30, sleep=sleep)
    by_id = {x["id"]: x for x in rows}
    assert set(by_id) == {"postgres", "elasticsearch", "neo4j", "ollama", "embed"}
    assert by_id["embed"]["status"] == "skipped" and by_id["embed"]["paid"] is True
    assert by_id["elasticsearch"]["status"] == "ng"
    assert boundaries == []


def test_ingest_path_paid_run_embeds_with_the_workers_embedding(run_dir, boundaries, monkeypatch):
    monkeypatch.setattr(worker_status, "live_workers", lambda: [{"state": "idle"}])
    worker = relay.Relay()
    rows = relay.request_and_wait(True, wait_s=30, sleep=lambda s: (worker.poll(), _drain(worker)))
    assert {x["id"]: x for x in rows}["embed"]["status"] == "ng"
    assert boundaries == ["embed"]
    assert KEY not in json.dumps(rows)


# ===== Codex の確認が本物のターンと同じ起動を共有する =====

_FAKE = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
a = sys.argv[1:]
sys.stdin.read()
ch = os.environ.get("CODEX_HOME")
cfg = pathlib.Path(ch) / "config.toml" if ch else None
with open(r"__REC__", "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": a, "cwd": os.getcwd(), "env_keys": sorted(os.environ),
                        "config": cfg.read_text() if cfg is not None and cfg.is_file() else None}) + "\n")
pathlib.Path(a[a.index("-o") + 1]).write_text("OK")
print(json.dumps({"type": "item.completed", "item": {"id": "1", "type": "agent_message", "text": "OK"}}))
'''


def _shape(argv: list) -> list:
    """値（パス・モデル名・推論の強さ・multi_agent の可否）を伏せた引数の並び。"""
    out = []
    for i, a in enumerate(argv):
        prev = argv[i - 1] if i else ""
        if prev in ("-o", "-C", "-m"):
            out.append("<v>")
        elif a.startswith(("model_reasoning_effort=", "features.multi_agent=")):
            out.append(a.split("=")[0] + "=<v>")
        else:
            out.append(a)
    return out


def test_codex_check_launches_like_a_real_turn(tmp_path, monkeypatch, boundaries):
    """本物のターンと確認の Codex が、同じ並びの引数・同じ洗い方の env・同じ書き方の設定で起動する（偽 codex の記録で比べる）。
    確認の結果に Codex の出力は出ず、使い捨ての作業フォルダは消える。"""
    import test_codex_workspace_authoring as H
    from sherpa import agents as A
    rec = tmp_path / "rec.jsonl"
    H._install_codex(tmp_path, monkeypatch, _FAKE.replace("__REC__", str(rec)))
    list(A.CodexProvider().run(H._ctx("parity-u1")))
    turn = json.loads(rec.read_text().splitlines()[-1])
    rec.unlink()

    monkeypatch.setattr(store, "get_settings", lambda uid="admin": {"agent": "simple", "codex_model_provider": "openai"})
    monkeypatch.setattr("sherpa.keys.selected_cloud_provider", lambda *a, **k: "openai")
    (row,) = connectivity.run_checks(True, path="codex", uid="adm")
    assert row["status"] == "ok" and row["paid"] is True, row
    chk = json.loads(rec.read_text().splitlines()[-1])

    head = ["exec", "--json", "--strict-config", "--skip-git-repo-check", "-o", "<v>", "-C", "<v>", "-m", "<v>",
            "-c", "model_reasoning_effort=<v>", "-c", "features.multi_agent=<v>", "--ephemeral"]
    assert _shape(turn["argv"])[:15] == head
    assert _shape(chk["argv"]) == head + ["-"]
    assert set(chk["env_keys"]) <= set(turn["env_keys"]) and "ES_URL" not in chk["env_keys"]
    assert {"PATH", "HOME", "CODEX_HOME", "LANG", "TMPDIR"} <= set(chk["env_keys"])
    assert 'default_permissions = "sherpa-authoring"' in chk["config"] and "[mcp_servers.sherpa]" not in chk["config"]
    assert 'default_permissions = "sherpa-authoring"' in turn["config"]
    assert not os.path.exists(chk["cwd"])
    assert KEY not in json.dumps(row)


def test_restarted_or_second_worker_does_not_run_a_claimed_request_again(run_dir, monkeypatch):
    """ワーカーは 1 件処理するたびに入れ替わる。頼みは最初に取ったプロセスだけが流し、次のプロセス（新しい Relay）は流さない。"""
    calls = []
    monkeypatch.setattr(connectivity, "run_checks",
                        lambda paid, path="api", uid=None: calls.append(paid) or [{"id": "x"}])
    rid = relay.write_request(True)
    first, second = relay.Relay(), relay.Relay()
    first.poll()
    second.poll()
    _drain(first)
    relay.Relay().poll()
    assert calls == [True] and relay.read_result(rid) == [{"id": "x"}]
    assert not list(run_dir.glob("connectivity_request.json*"))  # 取った頼みは残さない


def test_worker_stop_waits_for_a_running_check(run_dir, monkeypatch):
    """ワーカーが終わるときは、流している確認が結果を書くまで（上限つきで）待つ。"""
    import threading
    gate = threading.Event()
    monkeypatch.setattr(connectivity, "run_checks",
                        lambda paid, path="api", uid=None: gate.wait(5) and [{"id": "late"}])
    rid = relay.write_request(False)
    r = relay.Relay()
    r.poll()
    threading.Timer(0.3, gate.set).start()
    r.wait(timeout=5)
    assert relay.read_result(rid) == [{"id": "late"}]


def test_short_lived_worker_handles_the_request_at_start(run_dir, monkeypatch):
    """取り込み 1 件で心拍の間隔より早く終わるワーカーでも、起動したときに頼みを見て流し、終わる前に結果を書く。"""
    from sherpa.ingest import ingest_worker
    monkeypatch.setattr(connectivity, "run_checks", lambda paid, path="api", uid=None: [{"id": "start"}])
    monkeypatch.setattr(ingest_worker._Beat, "write", lambda self: None)
    monkeypatch.setattr(ingest_worker.worker_state, "clear_active", lambda: None)
    rid = relay.write_request(False)
    beat = ingest_worker._Beat("w-conn", "v1")
    beat.start()
    beat.stop("idle")
    assert relay.read_result(rid) == [{"id": "start"}]
