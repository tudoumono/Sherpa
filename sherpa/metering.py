"""チャット以外の LLM 呼び出し（intent 分類・埋め込み・VLM 視覚読み取り・rag.md の LLM 成形 等）の利用量を記録する。

チャット本回答の usage は `messages.answer->'usage'` に残る（本モジュールとは別・二重計上なし）。記録は常時 DB に書く。
`suppress()` は読み取り専用経路（評価ハーネス等）が記録しないためのスコープ。
`record`/`acc_add`/`acc_end` は例外を外へ出さない（計測が呼び出し元の挙動を変えない）。

使い方: 1回の意味のある LLM 呼び出し単位を `acc_begin()`/`acc_end()` の try/finally で囲み、その内側で HTTP 応答を得た箇所から
`acc_add(<parser>(resp))` を呼ぶ。スコープが開いていなければ `acc_add` は no-op。
`record()` は DB 記録に加えて `sherpa.usage` ロガー（`usage.log`）へ INFO 1行を出す（`user_id` は出さない。経過秒はスコープがあった呼び出しのみ）。
設計: docs/design/usage.md「`usage_events`（チャット以外の LLM 呼び出し）」
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager

from .store import usage_events as _ue

_log = logging.getLogger("sherpa")
_usage_log = logging.getLogger("sherpa.usage")  # 専用ファイル（usage.log）は log_setup.py 側の配線

# kind の閉じた語彙。'chat' は集計時に `messages.answer->'usage'` から合成する（usage_events には書かない）。
# 'chat-sub'＝下調べ役のツールループ／'chat-plan'＝複数プロファイル自動選択の計画呼び出し／'chat-review'＝メイン査読／
# 'rag_render'＝rag.md の LLM 成形（world 単位の集約1行）／'answer'＝`POST /ext/v1/answer`（user_id は `ext:{key_id}`）／
# 'chat-round'＝査読の巡ごとの表示・分析用の記録（集計から除外し、消費の正本とは二重に足さない。内訳は `meta`）。
# 'usage_chat'/'graph_ask'/'research' は退役済みで新規には書かないが、過去の usage_events 行を読めるよう KINDS から外さない。
KINDS = ("intent", "embed", "graph_ask", "vlm", "chat-sub", "chat-plan",
        "usage_chat", "research", "answer", "chat-review", "chat-round", "rag_render")

_TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")


def _clamp_int(v) -> int:
    try:
        return max(int(v or 0), 0)
    except (TypeError, ValueError):
        return 0


_MAX_STR_FIELD_LEN = 256  # `metering.record` の防御的長さ上限


def _clamp_str(v, limit: int = _MAX_STR_FIELD_LEN):
    """`v` を文字列化して `limit` 字で切り詰める（`None` は `None` のまま）。巨大な文字列による `usage_events` の肥大を防ぐ。"""
    if v is None:
        return None
    s = str(v)
    return s[:limit] if len(s) > limit else s


def record(kind, provider, model, usage, *, user_id=None, world=None, calls=1,
          connect_timeout: float | None = None, statement_timeout_ms: int | None = None,
          elapsed_ms: float | None = None, conversation_id: int | None = None,
          meta: dict | None = None) -> None:
    """1行記録（`suppress()` 中は no-op）。`usage` は `acc_end()` が返す形、または生の usage 辞書。

    `elapsed_ms` 省略時は `acc_elapsed()` の経過秒をミリ秒にして使う（明示指定が優先。どちらも無ければ NULL）。
    `conversation_id` は会話別集計キー、`meta` は表示・分析用の付帯内訳（課金集計は読まない）。
    `usage` が None なら全トークン列を None にする（報告不能マーカー）。辞書なら欠落サブフィールドは 0 に補正する。
    `provider`/`model`/`user_id`/`world` は `_clamp_str` で長さ上限（既定256字）を掛ける。
    `connect_timeout`/`statement_timeout_ms` は `add_usage_event()` の INSERT へ転送する。
    DB 記録に成功したら `log_usage_line` で `sherpa.usage` ロガーへ INFO 1行を出す。例外は一切外へ出さない。
    """
    elapsed = acc_elapsed()  # 常に1回消費する（suppress 中でもスコープの取り残しを後続の `record()` へ持ち越さない）
    if getattr(_local, "suppress", False):  # suppress() 中は読み取り専用経路からの記録禁止
        return
    try:
        if isinstance(usage, dict):
            tokens = {f: _clamp_int(usage.get(f)) for f in _TOKEN_FIELDS}
        else:
            tokens = dict.fromkeys(_TOKEN_FIELDS)  # usage が None（または辞書でない）＝報告不能マーカー
        prov_c, model_c, world_c = _clamp_str(provider), _clamp_str(model), _clamp_str(world)
        elapsed_ms_val = (round(elapsed_ms) if elapsed_ms is not None
                         else (round(elapsed * 1000) if elapsed is not None else None))
        _ue.add_usage_event(kind=kind, provider=prov_c, model=model_c,
                            input_tokens=tokens["input_tokens"],
                            cached_input_tokens=tokens["cached_input_tokens"],
                            output_tokens=tokens["output_tokens"],
                            reasoning_output_tokens=tokens["reasoning_output_tokens"],
                            calls=calls, user_id=_clamp_str(user_id), world=world_c,
                            elapsed_ms=elapsed_ms_val, conversation_id=conversation_id, meta=meta,
                            connect_timeout=connect_timeout, statement_timeout_ms=statement_timeout_ms)
        log_usage_line(kind, prov_c, model_c, tokens, calls, world_c, elapsed)
    except Exception as e:
        # 生の例外・traceback は出さず、`_log_masked_exception`（マスク済みの型＋メッセージ）経由で WARNING に残す
        from .ingest.graph_extract import _log_masked_exception
        _log_masked_exception(_log, f"metering.record failed (ignored): kind={kind} provider={provider}", e)


def _fmt_tok(v) -> str:
    return "?" if v is None else str(v)  # tokens が None＝報告不能マーカー


def log_usage_line(kind, provider, model, tokens: dict, calls, world, elapsed: float | None,
                   *, depth: str | None = None, reasoning: str | None = None) -> None:
    """`sherpa.usage` ロガーへの INFO 1行。`record()` と、`record()` を通らない `kind="chat"`（`providers/base.py::_log_chat_usage`）が使う。

    例: `kind=embed provider=openai model=text-embedding-3-small in=52340 cached=0 out=0 calls=3 elapsed=12.4s world=test2`。
    `elapsed`/`world`/`depth`/`reasoning` は値が無ければ欄ごと省略する。`user_id` は載せない。例外は外へ出さない。
    `depth`（`"quick"/"standard"/"deep"/"max"`）・`reasoning` は `kind="chat"` のみ渡す。
    """
    try:
        parts = [f"kind={kind}", f"provider={provider}", f"model={model}",
                 f"in={_fmt_tok(tokens.get('input_tokens'))}",
                 f"cached={_fmt_tok(tokens.get('cached_input_tokens'))}",
                 f"out={_fmt_tok(tokens.get('output_tokens'))}",
                 f"calls={calls}"]
        if elapsed is not None:
            parts.append(f"elapsed={elapsed:.1f}s")
        if world:
            parts.append(f"world={world}")
        if depth:
            parts.append(f"depth={depth}")
        if reasoning:
            parts.append(f"reasoning={reasoning}")
        _usage_log.info(" ".join(parts))
    except Exception:
        pass


@contextmanager
def suppress():
    """このスレッドの `record()` を一時的に無効化する（読み取り専用の経路用）。ネスト安全（再入時は外側の状態を復元）。"""
    prev = getattr(_local, "suppress", False)
    _local.suppress = True
    try:
        yield
    finally:
        _local.suppress = prev


# ---- スレッドローカルのアキュムレータスタック ----
# 1回の呼び出し単位の中で複数回の HTTP 応答から得た usage を合算し、最後に1行として record() する

_local = threading.local()


def _stack() -> list:
    st = getattr(_local, "stack", None)
    if st is None:
        st = []
        _local.stack = st
    return st


def acc_begin() -> None:
    """アキュムレータをスタックに push する（`{'calls':0,'tokens':None,'t0':<monotonic>}`）。呼び出し元は必ず try/finally で対にする。"""
    try:
        _stack().append({"calls": 0, "tokens": None, "t0": time.monotonic()})
    except Exception:
        pass


def acc_add(usage) -> None:
    """1回の HTTP 応答分の usage を直近のスコープへ合算する。スタックが空なら no-op。`suppress()` 中は合算しない。"""
    try:
        if getattr(_local, "suppress", False):
            return
        st = _stack()
        if not st:
            return
        frame = st[-1]
        frame["calls"] += 1
        if isinstance(usage, dict):
            if frame["tokens"] is None:
                frame["tokens"] = dict.fromkeys(_TOKEN_FIELDS, 0)
            for f in _TOKEN_FIELDS:
                frame["tokens"][f] += _clamp_int(usage.get(f))
    except Exception:
        pass


def acc_end() -> tuple:
    """直近のスコープを pop して `(tokens|None, calls)` を返す。スタックが空なら `(None, 0)`。pop したフレームの経過秒は `acc_elapsed()` が読む。"""
    try:
        st = _stack()
        if not st:
            return None, 0
        frame = st.pop()
        t0 = frame.get("t0")
        if t0 is not None:
            _local.last_elapsed = time.monotonic() - t0
            _local.last_elapsed_ts = time.monotonic()
        return frame["tokens"], frame["calls"]
    except Exception:
        return None, 0


def acc_merge(tokens: dict | None, calls: int) -> None:
    """他スレッドで `acc_begin()`/`acc_end()` して得た `(tokens, calls)` を直近のスコープへ合算する（並列ワーカー用）。
    `suppress()` 中と calls が 0 のときは何もしない。
    """
    try:
        if not calls or getattr(_local, "suppress", False):
            return
        st = _stack()
        if not st:
            return
        frame = st[-1]
        frame["calls"] += calls
        if isinstance(tokens, dict):
            if frame["tokens"] is None:
                frame["tokens"] = dict.fromkeys(_TOKEN_FIELDS, 0)
            for f in _TOKEN_FIELDS:
                frame["tokens"][f] += _clamp_int(tokens.get(f))
    except Exception:
        pass


_ELAPSED_FRESHNESS_SEC = 5.0  # acc_elapsed() が拾える猶予


def acc_elapsed() -> float | None:
    """直近の `acc_end()` が pop したフレームの経過秒（1回読んだら消費・次は None）。`_ELAPSED_FRESHNESS_SEC` 秒より古ければ None を返す（無関係な `record()` に誤って乗らないため）。"""
    try:
        v = getattr(_local, "last_elapsed", None)
        ts = getattr(_local, "last_elapsed_ts", None)
        _local.last_elapsed = None
        _local.last_elapsed_ts = None
        if v is None or ts is None:
            return None
        if time.monotonic() - ts > _ELAPSED_FRESHNESS_SEC:
            return None
        return v
    except Exception:
        return None


# ---- プロバイダ別・例外安全な usage パーサ群 ----
# 各々 `{input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens}` の辞書、または usage が無ければ None を返す（例外は出さない）。
# `agentic_search.py`（es_index を引き込む）を ingest 経路に持ち込まないため意図的に重複させている

def usage_from_openai_chat(resp) -> dict | None:
    """OpenAI Chat Completions の usage（`providers/openai.py::_openai_usage` と同式）。"""
    try:
        u = (resp or {}).get("usage")
        if not isinstance(u, dict):
            return None
        pd = u.get("prompt_tokens_details") or {}
        cd = u.get("completion_tokens_details") or {}
        return {"input_tokens": u.get("prompt_tokens"),
                "cached_input_tokens": pd.get("cached_tokens"),
                "output_tokens": u.get("completion_tokens"),
                "reasoning_output_tokens": cd.get("reasoning_tokens")}
    except Exception:
        return None


def usage_from_ollama_chat(resp) -> dict | None:
    """Ollama `/api/chat`（トップレベル prompt_eval_count/eval_count・キャッシュ/推論の内訳なし）。"""
    try:
        r = resp or {}
        if "prompt_eval_count" not in r and "eval_count" not in r:
            return None
        return {"input_tokens": r.get("prompt_eval_count"),
                "cached_input_tokens": None,
                "output_tokens": r.get("eval_count"),
                "reasoning_output_tokens": None}
    except Exception:
        return None


def usage_from_openai_embed(r) -> dict | None:
    """OpenAI Embeddings の usage（prompt_tokens → input・output=0）。"""
    try:
        u = (r or {}).get("usage")
        if not isinstance(u, dict):
            return None
        return {"input_tokens": u.get("prompt_tokens"),
                "cached_input_tokens": None, "output_tokens": 0, "reasoning_output_tokens": None}
    except Exception:
        return None


def usage_from_ollama_embed(r) -> dict | None:
    """Ollama `/api/embed`（トップレベル prompt_eval_count → input・output=0）。"""
    try:
        rr = r or {}
        if "prompt_eval_count" not in rr:
            return None
        return {"input_tokens": rr.get("prompt_eval_count"),
                "cached_input_tokens": None, "output_tokens": 0, "reasoning_output_tokens": None}
    except Exception:
        return None
