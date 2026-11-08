"""つながりの確認（管理画面「つながりの確認」タブ）。アプリが実際に使う経路ごとに、決まった確認だけを流す。

各確認は実際の処理と同じ関数・同じ送り方を通す。任意の URL・コマンドは受けない。
結果には鍵・トークン・パスワード・URL の資格情報・応答の本文を出さない（理由は決まった言葉だけ）。
`python -m sherpa.connectivity --json [--paid]` はそのプロセスで流した結果を JSON で出す（子プロセスの経路用）。
取り込みの係の経路は、取り込みワーカーが自分のプロセスで流す（`sherpa/ingest/connectivity_relay.py`）。
設計: docs/design/operations.md「つながりの確認」
"""
from __future__ import annotations

import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CHILD_TIMEOUT_S = 60.0
_ES_TIMEOUT = 5
_OLLAMA_TIMEOUT = 5
_EMBED_DEADLINE_S = 15

_DIRECT = "直接"
_PROXY = "プロキシ経由"


class _Skip(Exception):
    """「流さない」の申告（reason が表示になる）。"""

    def __init__(self, reason: str, *, paid: bool = False, route=None, host=None):
        super().__init__(reason)
        self.reason, self.paid, self.route, self.host = reason, paid, route, host


class _Incomplete(Exception):
    """確かめきれなかった（設定を読めない等）。"""


class _Ng(Exception):
    """決まった言葉の理由つきの失敗。"""

    def __init__(self, reason: str, *, route=None, host=None, paid=None):
        super().__init__(reason)
        self.reason, self.route, self.host, self.paid = reason, route, host, paid


def host_of(value) -> str | None:
    """URL・`host:port` からホスト名だけを取り出す（資格情報・パス・クエリは捨てる）。"""
    v = str(value or "").strip()
    if not v:
        return None
    try:
        return urllib.parse.urlsplit(v if "://" in v else "//" + v).hostname or None
    except ValueError:
        return None


def route_for(url_or_host, scheme: str = "https") -> str:
    """urllib が実際に選ぶ経路。プロキシが設定され、宛先が例外に当たらなければプロキシ経由。
    例外の照合は urllib の送信と同じ値（URL なら `Request(url).host`＝資格情報があれば含む `host:port`）で行う。
    """
    if not host_of(url_or_host):
        return _DIRECT
    v = str(url_or_host).strip()
    if "://" in v:
        scheme = urllib.parse.urlsplit(v).scheme or scheme
        try:
            target = urllib.request.Request(v).host
        except ValueError:
            return _DIRECT
    else:
        target = v
    if urllib.request.getproxies().get(scheme) and not urllib.request.proxy_bypass(target):
        return _PROXY
    return _DIRECT


def classify(e: BaseException) -> str:
    """例外を決まった言葉の理由にする。例外の文言は出さない。"""
    from . import llm
    seen = set()
    stack = [e]
    while stack:
        c = stack.pop()
        if c is None or id(c) in seen:
            continue
        seen.add(id(c))
        if isinstance(c, llm.SsrfBlocked):
            return "宛先が許可されていない"
        if isinstance(c, urllib.error.HTTPError):
            if c.code in (401, 403):
                return f"認証に失敗（{c.code}）"
            if c.code == 404:
                return "モデルまたは宛先が見つからない（404）"
            if c.code == 429:
                return "利用の上限に達している（429）"
            return f"相手がエラーを返した（{c.code}）"
        if isinstance(c, ssl.SSLCertVerificationError):
            return "証明書の検証に失敗"
        if isinstance(c, (TimeoutError, socket.timeout)):
            return "時間切れ"
        if isinstance(c, ConnectionRefusedError):
            return "接続を拒否された"
        if isinstance(c, socket.gaierror):
            return "名前を引けない"
        text = str(c).lower()
        name = type(c).__name__
        if name in ("AuthError", "Unauthorized") or "password authentication failed" in text \
                or "authentication failed" in text:
            return "認証に失敗"
        if "certificate" in text:
            return "証明書の検証に失敗"
        if "connection refused" in text or "refused" in text:
            return "接続を拒否された"
        if "could not translate host" in text or "name or service not known" in text \
                or "nodename nor servname" in text or "temporary failure in name resolution" in text:
            return "名前を引けない"
        if "timeout" in text or "timed out" in text:
            return "時間切れ"
        stack.extend([getattr(c, "__cause__", None), getattr(c, "__context__", None),
                      getattr(c, "reason", None)])
    return "つながらない"


class _Ctx:
    """1 回の実行で共有する設定（DB から 1 度だけ読む）。読めなければ `error` に決まった言葉を持つ。"""

    def __init__(self, unreadable_reason: str = "設定を読めなかった（DB に届かない）"):
        from . import store
        self.uid: str | None = None
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


def _check_ollama(ctx: _Ctx, paid: bool) -> dict:
    from . import health, keys, llm
    sys_s = ctx.need()
    if not health.ollama_in_use(sys_s):
        raise _Skip("Ollama を使っていない")
    base = keys.resolve_ollama_url(None, system_settings=sys_s)
    host = host_of(base)
    route = route_for(base, "http")
    try:
        with llm.urlopen_no_redirect(llm.ollama_url(base, "/api/tags"), timeout=_OLLAMA_TIMEOUT) as r:
            json.loads(r.read())
    except Exception as e:
        raise _Ng(classify(e), route=route, host=host)
    return {"route": route, "host": host}


def _embed_failure_reason(failure, dim) -> str:
    if failure is None:
        return "応答を得られなかった（時間切れの可能性）"
    if failure.kind == "dimension":
        return f"次元が合わない（設定 {dim}・応答 {failure.got_dim}）"
    if failure.kind == "invalid":
        return "応答の形が正しくない"
    if failure.kind == "deadline":
        return "時間切れ"
    status, etype = failure.status, str(failure.error_type)
    if status in (401, 403):
        return f"認証に失敗（{status}）"
    if status == 404:
        return "モデルが無い（404）"
    if status == 429:
        return "利用の上限に達している（429）"
    if status is not None:
        return f"相手がエラーを返した（{status}）"
    low = etype.lower()
    if "timeout" in low:
        return "時間切れ"
    if "ssl" in low or "certificate" in low:
        return "証明書の検証に失敗"
    if "ssrf" in low or "preflight" in low:
        return "宛先が許可されていない"
    if "connectionrefused" in low:
        return "接続を拒否された"
    if "urlerror" in low or "connection" in low:
        return "つながらない（拒否・名前を引けない・証明書のいずれか。詳しくは API のログ）"
    return "つながらない"


def _check_embed(ctx: _Ctx, paid: bool) -> dict:
    from . import embeddings, llm
    sys_s = ctx.need()
    if os.environ.get("SHERPA_DISABLE_EMBED"):
        raise _Skip("埋め込みを使わない設定になっている")
    try:
        ec = embeddings.cfg(None, system_settings=sys_s)
    except Exception:
        raise _Ng("埋め込みの設定を解決できない")
    if ec is None:
        if embeddings.cloud_selected_but_unavailable(sys_s):
            raise _Ng("埋め込みの設定を解決できない（選んだ接続先のキー・接続先・モデルを確認）")
        raise _Skip("埋め込みは使っていない")
    provider = ec.get("provider")
    if provider == "ollama":
        host, route = host_of(ec.get("url")), _DIRECT  # 実際の送信が `llm.no_proxy_requests()` で常に直結
    else:
        try:
            url = llm.openai_url("embeddings", system_settings=sys_s)
            host, route = host_of(url), route_for(url, "https")
        except Exception:
            host, route = None, None
    note = f"{provider} / {ec.get('model')} / {ec.get('dim')}次元"
    if provider == "openai" and not paid:
        raise _Skip("有料のため流していない", paid=True, route=route, host=host)
    vecs, failure = embeddings.embed_with_failure(["接続の確認"], ec, deadline_s=_EMBED_DEADLINE_S)
    if not vecs:
        raise _Ng(_embed_failure_reason(failure, ec.get("dim")), route=route, host=host, paid=provider == "openai")
    got = len(vecs[0]) if isinstance(vecs[0], (list, tuple)) else None
    if got != ec.get("dim"):
        raise _Ng(f"次元が合わない（設定 {ec.get('dim')}・応答 {got}）", route=route, host=host,
                  paid=provider == "openai")
    return {"route": route, "host": host, "note": note, "paid": provider == "openai"}


def _says_no_tools(e: BaseException) -> bool:
    """400 の応答の本文が「道具に対応していない」旨か（本文は判定にだけ使い、どこにも出さない）。"""
    c = e
    for _ in range(4):
        if isinstance(c, urllib.error.HTTPError):
            if c.code != 400:
                return False
            try:
                return "does not support tools" in c.read(4096).decode("utf-8", "replace").lower()
            except Exception:
                return False
        c = c.__cause__ or c.__context__
        if c is None:
            return False
    return False


def _check_answer_ai(ctx: _Ctx, paid: bool) -> dict:
    """簡易の回答と同じ部品（`simple_chat._resolve_llm` の接続先・見出し、`round_messages`・`round_body`・同じ道具の一覧、
    `agentic_search._post`・1 送信の上限時間・応答の取り出し `round_message_raw`）で 1 往復送り、assistant の message があるときだけ成功にする。OpenAI に送るときは有料（`paid` のときだけ）。Ollama は無料で送る。
    簡易の回答は API のプロセスで動くので、この確認は API の経路だけで流す。
    """
    from . import agentic_search, keys, llm, simple_chat
    sys_s = ctx.need()
    try:
        provider = simple_chat.resolve_model_and_provider(None, sys_s)[0]
    except simple_chat._LLMResolveError:
        raise _Ng("回答の AI の設定を解決できない（既定の AI・使えるモデルを確認）")
    is_paid = provider == "openai"
    if is_paid:
        try:
            url = llm.openai_url("chat/completions", system_settings=sys_s)
        except Exception:
            url = None
    else:
        url = keys.resolve_ollama_url(None, system_settings=sys_s)
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
            if tools and _says_no_tools(e) else classify(e)
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


def _run_codex_once(argv: list, env: dict, cwd: Path) -> tuple[int | None, str]:
    """`codex exec` を 1 回起こして（返り値, 出力の全文）を返す。上限を超えたらプロセスグループごと止めて返り値 None。"""
    import signal
    p = subprocess.Popen(argv, env=env, cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = p.communicate("OK とだけ返してください。", timeout=_CODEX_TIMEOUT_S)
        return p.returncode, (out or "") + "\n" + (err or "")
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        out, err = p.communicate()
        return None, (out or "") + "\n" + (err or "")


def _check_codex(ctx: _Ctx, paid: bool) -> dict:
    """回答を作る Codex CLI を、本物のターンと同じ選び方（`providers._select_provider`）・同じ env の洗い方（`sandbox._codex_clean_env`）・
    同じ設定ファイルの書き方（`sandbox._write_codex_authoring_config`・道具なし）・同じ引数（`codex_cli.sandboxed_exec_argv`）で、最小の 1 往復を流す。
    確認を押した管理者自身の実効の設定で流す。OpenAI 系は有料（`paid` のときだけ）。Ollama は無料で流す。
    """
    import shutil
    from . import agent_constructs, llm, store
    from .providers import _select_provider
    from .providers.codex import codex_cli, sandbox
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
    note = f"{agent_constructs.codex_model_provider(user_s)} / {provider.model}"
    root = Path(tempfile.mkdtemp(prefix="sherpa-codexchk-"))
    try:
        codex_home, run_dir = root / "codexhome", root / "run"
        tmp = run_dir / ".tmp"
        tmp.mkdir(parents=True)
        try:
            sandbox._write_codex_authoring_config(
                codex_home, [], "low", False, "", None, False, True,
                ollama_base_url=base, system_settings=provider._system_settings,
                direct_read_roots=[], orchestrator_model=provider.model)
        except Exception:
            raise _Ng("Codex の設定を組み立てられない（接続先・キーの確かめで止まった）", route=route, host=host, paid=is_paid)
        last = tmp / "last-message.txt"
        argv = codex_cli.sandboxed_exec_argv(
            last_message_path=last, run_dir=run_dir, model=provider.model, reasoning="low",
            multi_agent=False, ephemeral=True) + ["-"]
        env = sandbox._codex_clean_env(codex_home, tmp, openai_api_key=provider._openai_api_key)
        try:
            code, text = _run_codex_once(argv, env, run_dir)
        except OSError:
            raise _Ng("Codex を起動できなかった", route=route, host=host, paid=is_paid)
        if code is None:
            raise _Ng("時間切れ（90 秒）", route=route, host=host, paid=is_paid)
        try:
            answered = bool(last.read_text(encoding="utf-8").strip())
        except OSError:
            answered = False
        if code != 0 or not answered:
            raise _Ng(_codex_failure_reason(text), route=route, host=host, paid=is_paid)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return {"route": route, "host": host, "note": note, "paid": is_paid}


_CHECKS = (
    ("postgres", "会話・ユーザーの保存先（PostgreSQL）", False, _check_postgres),
    ("elasticsearch", "全文検索（Elasticsearch）", False, _check_elasticsearch),
    ("neo4j", "つながりの検索（Neo4j）", False, _check_neo4j),
    ("ollama", "手元の AI（Ollama）", False, _check_ollama),
    ("embed", "埋め込み（意味での検索）", None, _check_embed),
    ("answer_ai", "回答の AI（簡易の回答）", None, _check_answer_ai),
    ("codex", "Codex（回答を作る AI の実行）", None, _check_codex),
)

# 経路ごとに流す確認。道具の子は DB の資格情報を持たない・簡易の回答は API のプロセスで動く・取り込みの係は回答の AI を使わない。
_PATH_CHECKS = {
    "api": ("postgres", "elasticsearch", "neo4j", "ollama", "embed", "answer_ai"),
    "mcp": ("elasticsearch", "neo4j", "ollama", "embed"),
    "ingest": ("postgres", "elasticsearch", "neo4j", "ollama", "embed"),
    "codex": ("codex",),
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
    ctx.uid = uid
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
