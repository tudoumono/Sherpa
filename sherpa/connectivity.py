"""つながりの確認（システム状態画面の「実際に送って確かめる」）。アプリが実際に使う経路ごとに、決まった確認だけを流す。

各確認は実際の処理と同じ関数・同じ送り方を通す。任意の URL・コマンドは受けない。
結果には鍵・トークン・パスワード・URL の資格情報・応答の本文を出さない（理由は決まった言葉だけ）。
`python -m sherpa.connectivity --json [--paid]` はそのプロセスで流した結果を JSON で出す（子プロセスの経路用）。
取り込みの係の経路は、取り込みワーカーが自分のプロセスで流す（`sherpa/ingest/connectivity_relay.py`）。
設計: docs/design/operations.md「システム状態とつながりの確認」
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import connectivity_roundtrip
from .connectivity_base import (  # noqa: F401  確認の共通部品（他の部品・試験がここから引く）
    _DIRECT, _PROXY, _Incomplete, _Ng, _Skip, _embed_failure_reason, classify, host_of, resolve_embed, route_for,
)

CHILD_TIMEOUT_S = 60.0
_ES_TIMEOUT = 5
_EMBED_DEADLINE_S = 15


class _Ctx:
    """1 回の実行で共有する設定（DB から 1 度だけ読む）。読めなければ `error` に決まった言葉を持つ。"""

    def __init__(self, unreadable_reason: str = "設定を読めなかった（DB に届かない）"):
        from . import store
        self.uid: str | None = None
        self.path = "api"
        self.sys: dict | None = None
        self.error: str | None = None
        try:
            self.sys = store.get_system_settings(connect_timeout=2, statement_timeout_ms=2000)
        except Exception:
            self.error = unreadable_reason

    def need(self) -> dict:
        if self.sys is None:
            raise _Incomplete(self.error or "設定を読めなかった")
        return self.sys


def _check_postgres(ctx: _Ctx, paid: bool) -> dict:
    from . import health, store
    host = None
    try:
        import psycopg.conninfo
        host = psycopg.conninfo.conninfo_to_dict(store._dsn()).get("host")
    except Exception:
        host = None
    try:
        health._ping_postgres()
    except Exception as e:
        raise _Ng(classify(e), route=_DIRECT, host=host)
    return {"route": _DIRECT, "host": host}


def _check_elasticsearch(ctx: _Ctx, paid: bool) -> dict:
    from . import es_index
    url = es_index._url()
    route, host = route_for(url, "http"), host_of(url)
    try:
        with urllib.request.urlopen(url + "/_cluster/health", timeout=_ES_TIMEOUT) as r:
            json.loads(r.read())
    except Exception as e:
        raise _Ng(classify(e), route=route, host=host)
    return {"route": route, "host": host}


def _check_neo4j(ctx: _Ctx, paid: bool) -> dict:
    from . import health
    from .ingest import world_neo4j
    host = host_of(world_neo4j.default_neo4j_uri())
    try:
        health._ping_neo4j()
    except Exception as e:
        raise _Ng(classify(e), route=_DIRECT, host=host)
    return {"route": _DIRECT, "host": host}


def _check_embed(ctx: _Ctx, paid: bool) -> dict:
    from . import embeddings, llm
    sys_s = ctx.need()
    ec = resolve_embed(sys_s).cfg
    provider = ec.get("provider")
    if provider == "ollama":
        host, route = host_of(ec.get("url")), _DIRECT  # 実際の送信が `llm.no_proxy_requests()` で常に直結
    else:
        try:
            url = llm.openai_url("embeddings", system_settings=sys_s)
            host, route = host_of(url), route_for(url, "https")
        except Exception:
            host, route = None, None
    dim = ec.get("dim")
    note = f"{provider} / {ec.get('model')} / " + (f"{dim}次元" if dim is not None else "次元は未測定")
    if provider == "openai" and not paid:
        raise _Skip("有料のため流していない", paid=True, route=route, host=host)
    timeout_s = embeddings.request_timeout(ec)
    deadline_s = max(_EMBED_DEADLINE_S, timeout_s)   # 次元の測定も本番の送信も、管理画面で決めた 1 回の待ち時間まで待つ
    if dim is None:  # 次元を測れていない Ollama: 接続先へ問い合わせ、応答の次元を表示する（次元の不一致は判定しない）
        vecs, failure = embeddings.reach_with_failure(["接続の確認"], ec, deadline_s=deadline_s)
        if not vecs or not hasattr(vecs[0], "__len__") or not len(vecs[0]):
            raise _Ng("次元を測れなかった: " + _embed_failure_reason(failure, None, timeout_s), route=route, host=host)
        embeddings.remember_ollama_dim(ec["url"], ec["model"], len(vecs[0]))
        note += f"（応答 {len(vecs[0])}次元）"
        return {"route": route, "host": host, "note": note, "paid": False}
    vecs, failure = embeddings.embed_with_failure(["接続の確認"], ec, deadline_s=deadline_s)
    if not vecs:
        raise _Ng(_embed_failure_reason(failure, dim, timeout_s), route=route, host=host, paid=provider == "openai")
    got = len(vecs[0]) if hasattr(vecs[0], "__len__") else None
    if not embeddings.is_valid_vector(vecs[0], dim):
        reason = (f"次元が合わない（設定 {dim}・応答 {got}）" if got != dim
                  else "応答のベクトルが正しくない（数でない値・すべて 0 など）")
        raise _Ng(reason, route=route, host=host, paid=provider == "openai")
    return {"route": route, "host": host, "note": note, "paid": provider == "openai"}


def _check_answer_ai(ctx: _Ctx, paid: bool) -> dict:
    """簡易の回答と同じ部品（`simple_chat._resolve_llm` の接続先・見出し、`round_messages`・`round_body`・同じ道具の一覧、
    `agentic_search._post`・1 送信の上限時間・応答の取り出し `round_message_raw`）で 1 往復送り、assistant の message があるときだけ成功にする。OpenAI に送るときは有料（`paid` のときだけ）。Ollama は無料で送る。
    簡易の回答は API のプロセスで動くので、この確認は API の経路だけで流す。
    """
    from . import agentic_search, ai_targets, llm, simple_chat
    sys_s = ctx.need()
    try:
        target = ai_targets.answer_target(sys_s)
    except simple_chat._LLMResolveError:
        raise _Ng("回答の AI の設定を解決できない（既定の AI・使えるモデルを確認）")
    is_paid = target.provider == "openai"
    if is_paid:
        try:
            url = llm.openai_url("chat/completions", system_settings=sys_s)
        except Exception:
            url = None
    else:
        url = target.base
    host = host_of(url)
    route = route_for(url, "https" if is_paid else "http") if url else None
    if is_paid and not paid:
        raise _Skip("有料のため流していない", paid=True, route=route, host=host)
    try:
        provider, model, endpoint, headers = simple_chat._resolve_llm(sys_s)
    except simple_chat.LLMUnavailable:
        raise _Ng("回答の AI に送る前の確かめで止まった（キー・接続先・許可を確認）", route=route, host=host, paid=is_paid)
    tools = simple_chat._build_tools(agentic_search.tool_availability())
    msgs = simple_chat.round_messages(simple_chat._SYSTEM_PROMPT, "接続の確認です。資料は調べずに OK とだけ返してください。")
    body = simple_chat.round_body(provider, model, msgs, tools)
    try:
        resp = agentic_search._post(endpoint, headers, body, timeout=simple_chat._MAX_PER_CALL_TIMEOUT_S)
    except Exception as e:
        reason = "モデルが道具の呼び出しに対応していない（使えるモデルで別のモデルを選ぶ）" \
            if tools and simple_chat.says_no_tools(e) else classify(e)
        raise _Ng(reason, route=route, host=host, paid=is_paid)
    try:
        msg = simple_chat.round_message_raw(resp)
        ok = isinstance(msg, dict)
    except Exception:
        ok = False
    if not ok:
        raise _Ng("応答の形が正しくない（簡易の回答も読めない）", route=route, host=host, paid=is_paid)
    if not msg.get("tool_calls") and not agentic_search._openai_style_text(msg):
        raise _Ng("返事が空だった（簡易の回答も空になる）", route=route, host=host, paid=is_paid)
    return {"route": route, "host": host, "note": f"{provider} / {model}", "paid": is_paid}


_CODEX_TIMEOUT_S = 90
_CODEX_TOOLS_TIMEOUT_S = 150
_TOOLS_TURN_ID = "connectivity-check"
_TOOLS_PROMPT = "list_docs を 1 回だけ呼び、返ってきた資料の件数（count）の数字だけを答えてください。"

_CODEX_FAILURES = (
    (r"(?:sandbox-exec|bwrap):.{0,40}execvp",
     "Codex のサンドボックスが Codex 自身を起動できない（Codex の導入先を読ませる許可を確認）"),
    (r"certificate|self[- ]signed|local issuer|\bssl\b|tls handshake", "証明書の検証に失敗"),
    (r"\b407\b|proxy authentication", "プロキシの認証に失敗（407）"),
    (r"\b40[13]\b|unauthorized|invalid api key|invalid_api_key|not logged in|forbidden", "認証に失敗（401/403）"),
    (r"\b429\b|rate limit|quota", "利用の上限に達している（429）"),
    (r"model is not supported|not supported when using|unsupported model|model_not_supported",
     "モデルがこの使い方では使えない（使えるモデルの設定を確認）"),
    (r"\b404\b|model.{0,20}not found|does not exist", "モデルまたは宛先が見つからない（404）"),
    (r"connection refused|connectionrefused", "接続を拒否された"),
    (r"name or service not known|nodename nor servname|failed to lookup|dns error|could not resolve|temporary failure in name",
     "名前を引けない"),
    (r"timed out|timeout|deadline", "時間切れ"),
)


def _codex_failure_reason(text: str) -> str:
    """Codex の出力（判定にだけ使い、どこにも出さない）から、決まった言葉の理由を選ぶ。"""
    import re
    low = text.lower()
    for pat, reason in _CODEX_FAILURES:
        if re.search(pat, low):
            return reason
    return "つながらない（Codex が応答を返さなかった）"


def _run_codex_once(argv: list, env: dict, cwd: Path, *, prompt: str = "OK とだけ返してください。",
                    timeout_s: float = _CODEX_TIMEOUT_S) -> tuple[int | None, str]:
    """`codex exec` を 1 回起こして（返り値, 出力の全文）を返す。上限を超えたらプロセスグループごと止めて返り値 None。"""
    import signal
    p = subprocess.Popen(argv, env=env, cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, start_new_session=True)
    def stop() -> None:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        out, err = p.communicate(prompt, timeout=timeout_s)
        return p.returncode, (out or "") + "\n" + (err or "")
    except subprocess.TimeoutExpired:
        stop()
        out, err = p.communicate()
        return None, (out or "") + "\n" + (err or "")
    except BaseException:   # 途中の例外でも、Codex とその先の道具のサーバーをプロセスグループごと残さない
        stop()
        p.wait()
        raise


class _CodexTarget:
    """Codex の確認が使う、本物のターンと同じ選び方の提供元・宛先・有料か。"""

    def __init__(self, provider, user_s: dict, sys_s: dict, is_paid: bool, route, host, note: str):
        self.provider, self.user_s, self.sys_s = provider, user_s, sys_s
        self.is_paid, self.route, self.host, self.note = is_paid, route, host, note

    def ng(self, reason: str) -> _Ng:
        return _Ng(reason, route=self.route, host=self.host, paid=self.is_paid)


def _codex_target(ctx: _Ctx, paid: bool) -> _CodexTarget:
    """本物のターンと同じ選び方（`providers._select_provider`）で Codex の提供元を決める。有料で `paid` でなければ「流さない」。"""
    import shutil
    from . import ai_targets, llm, store
    from .providers import _select_provider
    from .providers.codex import sandbox
    from .providers.codex.provider import CodexProvider
    sys_s = ctx.need()
    if not ctx.uid:
        raise _Incomplete("確認した管理者を特定できない")
    if not shutil.which("codex"):
        raise _Ng("Codex CLI が見つからない")
    if not sandbox._codex_sandbox_enabled():
        raise _Incomplete("サンドボックスを使わない設定のため、本物と同じ起動で確かめられない")
    try:
        user_s = store.get_settings(ctx.uid)
    except Exception:
        raise _Incomplete("設定を読めなかった（DB に届かない）")
    user_s = {**user_s, "agent": "codex"}
    provider = _select_provider(user_s, sys_s)
    if not isinstance(provider, CodexProvider):
        raise _Ng("Codex を使える状態になっていない（設定画面の AI の選択と、管理画面の接続先・使えるモデルを確認）")
    base = provider._ollama_base_url
    is_paid = base is None
    host = route = None
    try:
        if base:
            host, route = host_of(base), route_for(base, "http")
        else:  # OpenAI 本家は組み込みの既定の接続先（Codex が自分で行く先と同じ）
            url = llm.openai_base_url(sys_s)
            host, route = host_of(url), route_for(url, "https")
    except Exception:
        host = route = None
    if is_paid and not paid:
        raise _Skip("有料のため流していない", paid=True, route=route, host=host)
    note = f"{ai_targets.codex_target(user_s, sys_s).provider} / {provider.model}"
    return _CodexTarget(provider, user_s, sys_s, is_paid, route, host, note)


class _CodexRun:
    """Codex を 1 回起こした結果（終了コード・出力の全文・返事があったか・道具の呼び出しの記録）。出力は判定にだけ使いどこにも出さない。"""

    def __init__(self, code, text: str, answered: bool, calls):
        self.code, self.text, self.answered, self.calls = code, text, answered, calls


def _codex_exec(target: _CodexTarget, *, tools: bool = False, prompt: str = "", timeout_s: float = 0.0) -> _CodexRun:
    """使い捨ての作業フォルダで Codex を 1 回起こす。本物のターンと同じ洗い方の env・設定ファイルの書き方・引数。
    `tools` が真なら本物のターンと同じ道具の登録（台帳なし・質問なし）を足し、道具の呼び出しの記録を確認専用の置き場へ向けて、終わりにまとめて返す。
    作業フォルダは必ず消す。
    """
    import shutil
    from . import tool_call_log
    from .providers.codex import codex_cli, sandbox
    provider = target.provider
    base = provider._ollama_base_url
    root = Path(tempfile.mkdtemp(prefix="sherpa-codexchk-"))
    try:
        codex_home, run_dir = root / "codexhome", root / "run"
        tmp = run_dir / ".tmp"
        tmp.mkdir(parents=True)
        calls_dir = codex_home / "toolcalls"
        extra_env = None
        if tools:
            extra_env = {"SHERPA_MCP_TOOLSET": "plan", tool_call_log.ENV_DIR: str(calls_dir.resolve()),
                         "SHERPA_MCP_TURN_ID": _TOOLS_TURN_ID}
        try:
            sandbox._write_codex_authoring_config(
                codex_home, [], "low", tools, _mcp_world() if tools else "", None, False, True,
                ollama_base_url=base, system_settings=provider._system_settings,
                direct_read_roots=[], orchestrator_model=provider.model, extra_mcp_env=extra_env)
        except Exception:
            raise target.ng("Codex の設定を組み立てられない（接続先・キーの確かめで止まった）")
        last = tmp / "last-message.txt"
        argv = codex_cli.sandboxed_exec_argv(
            last_message_path=last, run_dir=run_dir, model=provider.model, reasoning="low",
            multi_agent=False, ephemeral=True) + ["-"]
        env = sandbox._codex_clean_env(codex_home, tmp, openai_api_key=provider._openai_api_key)
        try:
            if tools:
                code, text = _run_codex_once(argv, env, run_dir, prompt=prompt, timeout_s=timeout_s)
            else:
                code, text = _run_codex_once(argv, env, run_dir)
        except OSError:
            raise target.ng("Codex を起動できなかった")
        try:
            answered = bool(last.read_text(encoding="utf-8").strip())
        except OSError:
            answered = False
        calls = tool_call_log.merge_call_logs(calls_dir, _TOOLS_TURN_ID) if tools else None
        return _CodexRun(code, text, answered, calls)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _check_codex(ctx: _Ctx, paid: bool) -> dict:
    """回答を作る Codex CLI を、本物のターンと同じ起動（`_codex_exec`・道具なし）で最小の 1 往復を流す。
    確認を押した管理者自身の実効の設定で流す。OpenAI 系は有料（`paid` のときだけ）。Ollama は無料で流す。
    """
    target = _codex_target(ctx, paid)
    run = _codex_exec(target)
    if run.code is None:
        raise target.ng("時間切れ（90 秒）")
    if run.code != 0 or not run.answered:
        raise target.ng(_codex_failure_reason(run.text))
    return {"route": target.route, "host": target.host, "note": target.note, "paid": target.is_paid}


def _mcp_events(text: str) -> list[dict]:
    """Codex の実行イベント（`--json` の 1 行 1 件）から、道具の呼び出し（`mcp_tool_call`）の item だけを取り出す。"""
    out = []
    for line in text.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        item = ev.get("item") if isinstance(ev, dict) else None
        if isinstance(item, dict) and item.get("type") == "mcp_tool_call":
            out.append(item)
    return out


def _check_codex_tools(ctx: _Ctx, paid: bool) -> dict:
    """本物のターンと同じ道具の登録で Codex を 1 回起こし、`list_docs` を呼ばせる。呼ばれたかは返事の文でなく、
    道具のサーバーが書く呼び出しの記録（なければ Codex の実行イベント）で判定する。
    設計: docs/design/operations.md「システム状態とつながりの確認」
    """
    target = _codex_target(ctx, paid)
    run = _codex_exec(target, tools=True, prompt=_TOOLS_PROMPT, timeout_s=_CODEX_TOOLS_TIMEOUT_S)
    if run.code is None:
        raise target.ng(f"時間切れ（{int(_CODEX_TOOLS_TIMEOUT_S)} 秒）")
    rows = [r for r in run.calls.rows if r.get("tool") == "list_docs"]
    good = [r for r in rows if r.get("status") in ("ok", "truncated")]
    if good:
        return {"route": target.route, "host": target.host, "note": target.note, "paid": target.is_paid}
    if rows:
        kind = rows[-1].get("error_kind")
        raise target.ng("道具がエラーを返した" + (f"（{kind}）" if isinstance(kind, str) else ""))
    if any(i.get("tool") == "list_docs" for i in _mcp_events(run.text)):
        raise target.ng("道具のサーバーを起こせなかった、または呼び出しに失敗した（呼び出しの記録が無い）")
    if run.code != 0 or not run.answered:
        raise target.ng(_codex_failure_reason(run.text))
    raise target.ng("道具を呼ばなかった（使えるモデルが道具の呼び出しに対応しているか確認）")


_CHECKS = (
    ("postgres", "会話・ユーザーの保存先（PostgreSQL）", False, _check_postgres),
    ("elasticsearch", "全文検索（Elasticsearch）", False, _check_elasticsearch),
    ("neo4j", "つながりの検索（Neo4j）", False, _check_neo4j),
    ("embed", "埋め込み（意味での検索）", None, _check_embed),
    ("store_roundtrip_es", "全文検索に書いて引けるか", False, connectivity_roundtrip.check_es),
    ("store_roundtrip_vector", "意味での検索に書いて引けるか", None, connectivity_roundtrip.check_vector),
    ("store_roundtrip_graph", "つながりの検索に書いて引けるか", False, connectivity_roundtrip.check_graph),
    ("answer_ai", "回答の AI（簡易の回答）", None, _check_answer_ai),
    ("codex", "Codex（回答を作る AI の実行）", None, _check_codex),
    ("codex_tools", "Codex から道具を呼べるか", None, _check_codex_tools),
)

# 経路ごとに流す確認。道具の子は DB の資格情報を持たず書き込みもしない・簡易の回答は API のプロセスで動く・取り込みの係は回答の AI を使わず、索引とグラフへ書く唯一の経路。
_PATH_CHECKS = {
    "api": ("postgres", "elasticsearch", "neo4j", "embed", "answer_ai"),
    "mcp": ("elasticsearch", "neo4j", "embed"),
    "ingest": ("postgres", "elasticsearch", "neo4j", "embed",
               "store_roundtrip_es", "store_roundtrip_vector", "store_roundtrip_graph"),
    "codex": ("codex", "codex_tools"),
}


def _result(cid, label, paid):
    return {"id": cid, "label": label, "status": "incomplete", "route": None, "host": None,
            "elapsed_ms": 0, "reason": None, "paid": bool(paid)}


_MCP_UNREADABLE = "設定を読めなかった（DB に届かない）。調べものの道具の意味での検索も同じ理由で使えない"


def run_checks(include_paid: bool = False, *, path: str = "api", uid: str | None = None) -> list[dict]:
    """このプロセスで、経路 `path`（`_PATH_CHECKS`）の確認を流して結果の配列を返す。例外は呼び出し元へ出さない。
    調べものの道具の経路でも設定は本物の道具と同じ読み方（`store.get_system_settings`）で読み、読めないことをそのまま出す。
    `uid` は Codex の確認が「誰の設定で流すか」（確認を押した管理者）。
    """
    ctx = _Ctx(_MCP_UNREADABLE if path == "mcp" else "設定を読めなかった（DB に届かない）")
    ctx.uid, ctx.path = uid, path
    wanted = _PATH_CHECKS[path]
    out = []
    for cid, label, paid, fn in _CHECKS:
        if cid not in wanted:
            continue
        r = _result(cid, label, paid)
        t0 = time.monotonic()
        try:
            got = fn(ctx, include_paid)
            r["status"] = "ok"
            for k in ("route", "host", "note"):
                if got.get(k) is not None:
                    r[k] = got[k]
            if "paid" in got:
                r["paid"] = bool(got["paid"])
        except _Skip as s:
            r.update(status="skipped", reason=s.reason, route=s.route, host=s.host)
            r["paid"] = bool(s.paid or paid)
        except _Incomplete as s:
            r.update(status="incomplete", reason=str(s))
        except _Ng as s:
            r.update(status="ng", reason=s.reason, route=s.route, host=s.host)
            if s.paid is not None:
                r["paid"] = s.paid
        except Exception as e:
            r.update(status="ng", reason=classify(e))
        r["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        out.append(r)
    return out


def incomplete_rows(path: str, reason: str) -> list[dict]:
    """経路 `path` の確認をすべて「確かめきれなかった」にした結果の配列。"""
    rows = []
    for cid, label, paid, _fn in _CHECKS:
        if cid in _PATH_CHECKS[path]:
            r = _result(cid, label, paid)
            r["reason"] = reason
            rows.append(r)
    return rows


def _mcp_world() -> str:
    from . import worlds
    try:
        return next(iter(worlds.discover_world_ids()), None) or worlds.default_world()
    except Exception:
        return worlds.default_world()


def run_mcp_child(include_paid: bool, *, timeout_s: float = CHILD_TIMEOUT_S) -> list[dict]:
    """調べものの道具（MCP）の子プロセスと同じ env・同じ実行系で確認を流して結果を受け取る。打ち切ったら「確かめきれなかった」。"""
    from .providers.codex.mcp import _mcp_env
    env = _mcp_env(_mcp_world(), None)
    env.setdefault("PATH", os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"))
    env.setdefault("PYTHONPATH", str(Path(__file__).resolve().parents[1]))
    cmd = [sys.executable, "-m", "sherpa.connectivity", "--json"] + (["--paid"] if include_paid else [])

    with tempfile.TemporaryDirectory(prefix="sherpa-conn-") as tmp:
        try:
            p = subprocess.run(cmd, env=env, cwd=tmp, capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return incomplete_rows("mcp", "確かめきれなかった（60秒で打ち切り）")
        except OSError:
            return incomplete_rows("mcp", "確かめきれなかった（子プロセスを起こせない）")
    if p.returncode != 0:
        return incomplete_rows("mcp", "確かめきれなかった（子プロセスが異常終了）")
    try:
        rows = json.loads(p.stdout)
        if not isinstance(rows, list):
            raise ValueError
    except ValueError:
        return incomplete_rows("mcp", "確かめきれなかった（結果を読めない）")
    return rows


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    from . import proxy_bypass
    proxy_bypass.apply({})  # 設定 DB には繋がず、親が組んだ NO_PROXY をそのまま使う（MCP の子と同じ）
    rows = run_checks("--paid" in args, path="mcp")
    sys.stdout.write(json.dumps(rows, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
