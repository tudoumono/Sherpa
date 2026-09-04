"""チャット以外の LLM 呼び出しの利用量計測（S1・2026-07-15-LLMオーケストレーション実装計画.md §3）。

チャット本回答の usage は引き続き `messages.answer->'usage'` に残る（本モジュールは無関係・二重計上なし）。
未計測だった全 LLM 呼び出し（intent 分類・埋め込み・admin グラフ質問・VLM 視覚読み取り・
rag.md の LLM 成形 等）に、**挙動を一切変えずに** 利用量を記録する。記録は常時 DB に書く（TOGGLE-RM・
2026-09-03: admin システム設定 `usage_metering`／env `SHERPA_USAGE_METERING` の ON/OFF トグルは
撤去済み——OFF に実益が無く、実際に初回埋め込みコストが計測されない実害が発生したため）。
読み取り専用経路（A/B ハーネス等）が記録しないための `suppress()` はトグルではなく経路の性質
（評価用ハーネスの読み取り専用契約）であり、これは残す。

**絶対に例外を外へ出さない**（`record`/`acc_add`/`acc_end`）: 全ての計測サイトは既存の「壊れず縮退する」
経路の中にいるため、計測自体が呼び出し元の挙動を変えてはならない。

使い方（消費側の規律）: 1回の意味のある LLM 呼び出し単位（classify 呼び出し・embed バッチ・
ask_graph 呼び出し・VLM convert・rag.md 成形の world 単位パス）を `acc_begin()`/`acc_end()` の
try/finally で囲み、その内側で実際に HTTP 応答を得た箇所（`complete_json`・`_read_ollama`/`_read_openai`・
`_embed_batch` 等）から `acc_add(<parser>(resp))` を呼ぶ。`acc_add` はスコープが開いていなければ
no-op＝health.py/settings_test のような手組み cfg でのプローブ呼び出しは自動的に計測対象外になる。

LOG-UX（2026-09-04・閉域実機フィードバック）: `record()` は DB 記録に加えて `sherpa.usage` ロガー
（`log_setup._SUBSYSTEM_LOGGERS`・`usage.log`）へも INFO 1行を出す（ターミナル観測に kind/tokens/
elapsed が出ない、という実利用フィードバック）。`user_id` はログには出さない（個人特定情報をログへ
増やさない）。経過秒は `acc_begin()`/`acc_end()` のスコープが実際にあった呼び出しでのみ乗る
（`acc_elapsed()` 参照・スコープ無しの `record()` 呼び出し（例: `graph_admin.answer_impact_question`
の `kind="graph_ask"`）では省略）。
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager

from .store import usage_events as _ue

_log = logging.getLogger("sherpa")
_usage_log = logging.getLogger("sherpa.usage")   # LOG-UX: 専用ファイル（usage.log）は log_setup.py 側の配線

# 'chat' は集計時合成用に予約（store/usage.py::usage_stats が messages.answer->'usage' から合成する・
# usage_events には書かない）。旧 'extract'／'propose'（意味層フル抽出／業務語↔コード対応提案）は
# GRAPH-SRC（2026-09-04・K9-K11）でその供給源（`graph_extract.extract_world`／`concept_propose`）
# ごと撤去済み（復活させない）。
# 'chat-sub'（S3・2026-07-15-LLMオーケストレーション実装計画.md §5.0 項6）: ハイブリッド（サブエージェント
# 委譲）のツールループが消費したトークン。'chat' と異なり usage_events に実際に書く（縮退ターンでも
# ループ終了時に成否問わず記録＝providers/base.py::_GenProvider._agentic_run 参照）。
# 'chat-plan'（S4-c・2026-07-19-LLMオーケストレーション実装計画.md §6.3・§6.4）: 複数プロファイル
# 自動選択（プランナ）の計画呼び出し（フラグシップ1回・リトライなし）が消費したトークン。
# `providers/base.py::_GenProvider._plan_select_subs` が記録する（answer.usage には含めない＝
# intent 分類と同じ前例）。DB 側の `usage_events.kind` は CHECK 制約のない TEXT のため DDL 変更は不要。
# 'usage_chat': 管理者の利用統計チャット（`POST /admin/usage/chat`）1回分。
# `sherpa/usage_chat.py::answer_usage_question` が記録する。
# 'research'（PART-4・2026-08-24-部品API設計.md §8.3）: `POST /ext/v1/research`（外部 API・チャットを
# 介さない下調べ検索）が消費したトークン。`user_id` にはユーザー本人でなく `ext:{key_id}` を入れる
# （`sherpa/research_service.py::run_research` 参照・ext_api の監査 actor 表記と揃える）。
# 'chat-review'（EXT-2c）: メイン査読（清書前の根拠十分性判定・読み直しの限定ツール精読を含む）の
# `_stream` 呼び出しが消費したトークン。`answer.usage`（清書本体）にも `chat-sub`（下調べ役）にも
# 乗らない別消費のため独立の kind にする。`providers/base.py::_GenProvider._agentic_run` が記録する。
# 'rag_render'（M1・§8.6-4）: rag.md の LLM 成形（取り込み後にバックグラウンドで後追い実行する
# record 単位の書き換え）が消費したトークン。`ingest/llm_render.py::run_world_pass` が world 単位の
# パス1回につき集約1行を記録する（`graph_extract.available()`/`complete_json()` を再利用＝
# model_catalog の独立用途 `render` を使うが、計測 kind は消費の性質が違うため独立させる）。
KINDS = ("intent", "embed", "graph_ask", "vlm", "chat-sub", "chat-plan",
        "usage_chat", "research", "chat-review", "rag_render")

_TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")


def _clamp_int(v) -> int:
    try:
        return max(int(v or 0), 0)
    except (TypeError, ValueError):
        return 0


_MAX_STR_FIELD_LEN = 256   # secRV MED-4是正: metering.record の防御的長さ上限（下記 docstring 参照）


def _clamp_str(v, limit: int = _MAX_STR_FIELD_LEN):
    """`v` を文字列化して `limit` 字で切り詰める（secRV MED-4・2026-07-18）。`None` は `None` のまま
    （DB 列は NULLABLE・None を "None" 文字列化しない）。

    上流（`routers/system.py::settings_put` の `ollama_model` 検証等）で弾く方針が本筋だが、
    無検証で保存された旧データ・別経路からの呼び出し・将来の呼び出し元追加に備え、ここでも
    独立に上限を掛ける（多層防御）。巨大な文字列（例: 数MBの ollama_model）を `usage_events` へ
    毎回複製し続けるストレージ増幅/DoS を防ぐ。
    """
    if v is None:
        return None
    s = str(v)
    return s[:limit] if len(s) > limit else s


def record(kind, provider, model, usage, *, user_id=None, world=None, calls=1,
          connect_timeout: float | None = None, statement_timeout_ms: int | None = None) -> None:
    """1行記録（`suppress()` 中は no-op）。`usage` は `acc_end()` が返す形、または生の usage 辞書の
    どちらでもよい。

    `usage` が None なら全トークン列を None にする（NULL 行＝プロバイダが usage を報告しなかった
    「報告不能」マーカー）。辞書なら欠落サブフィールドは 0 に補正する（`_usage_meta` のクランプ意味論と
    一致・`sherpa/providers/base.py::_usage_meta`）。例外は一切外へ出さない（呼び出し元の縮退契約を守る）。

    `provider`/`model`/`user_id`/`world` は `_clamp_str` で防御的に長さ上限（既定256字）を適用する
    （secRV MED-4・2026-07-18・巨大文字列の反復複製によるストレージ増幅対策）。

    `connect_timeout`/`statement_timeout_ms`（両方省略可・既定 None＝無期限）は `add_usage_event()`
    の INSERT へそのまま転送する（TOGGLE-RM・2026-09-03 で `enabled()` 設定確認クエリを撤去した
    ため、本関数の DB 接続は常に1回のみ＝以前あった2回接続の予算分割ロジックは不要になった）。

    LOG-UX: DB 記録に成功したら `sherpa.usage` ロガーへも INFO 1行を出す（`log_usage_line`）。
    `acc_elapsed()` は関数の**先頭**（suppress 判定より前）で読み切る——`suppress()` 中の呼び出し
    （読み取り専用ハーネスが内部で `acc_begin()`/`acc_end()` を回すスコープ）で溜まった経過秒が、
    ここで消費されずに残ると、次の無関係な `record()` 呼び出し（`acc_begin`/`acc_end` を使わない
    `kind="graph_ask"` 等）へ誤って乗ってしまう（1回使い切りの取りこぼし防止）。
    """
    elapsed = acc_elapsed()   # 常に1回消費（suppress 中でもスコープの取り残しを残さない）
    if getattr(_local, "suppress", False):   # suppress() 中＝読み取り専用経路（A/B ハーネス等）からの記録禁止
        return
    try:
        if isinstance(usage, dict):
            tokens = {f: _clamp_int(usage.get(f)) for f in _TOKEN_FIELDS}
        else:
            tokens = dict.fromkeys(_TOKEN_FIELDS)   # usage が None（または辞書でない）＝報告不能マーカー
        prov_c, model_c, world_c = _clamp_str(provider), _clamp_str(model), _clamp_str(world)
        _ue.add_usage_event(kind=kind, provider=prov_c, model=model_c,
                            input_tokens=tokens["input_tokens"],
                            cached_input_tokens=tokens["cached_input_tokens"],
                            output_tokens=tokens["output_tokens"],
                            reasoning_output_tokens=tokens["reasoning_output_tokens"],
                            calls=calls, user_id=_clamp_str(user_id), world=world_c,
                            connect_timeout=connect_timeout, statement_timeout_ms=statement_timeout_ms)
        log_usage_line(kind, prov_c, model_c, tokens, calls, world_c, elapsed)
    except Exception as e:
        # 生の例外オブジェクト・生の traceback は出さない——本関数を呼ぶ経路（agentic ループ等）
        # の例外に上流由来の秘密が混入していることがあるため、`_log_masked_exception`（最終防衛線
        # でマスクしてから型＋マスク済みメッセージだけを WARNING ログへ残す）を経由する。
        from .ingest.graph_extract import _log_masked_exception
        _log_masked_exception(_log, f"metering.record failed (ignored): kind={kind} provider={provider}", e)


def _fmt_tok(v) -> str:
    return "?" if v is None else str(v)   # tokens が None＝報告不能マーカー（record() の docstring 参照）


def log_usage_line(kind, provider, model, tokens: dict, calls, world, elapsed: float | None) -> None:
    """`sherpa.usage` ロガーへの INFO 1行（LOG-UX・2026-09-04）。`record()` が成功パスから呼ぶほか、
    `kind="chat"`（`record()` を通らない・本モジュール docstring 参照）は
    `providers/base.py::_log_chat_usage` が直接呼ぶ——公開関数（アンダースコアなし）なのはこの
    モジュール外からの呼び出しを想定しているため。

    例: `kind=embed provider=openai model=text-embedding-3-small in=52340 cached=0 out=0 calls=3
    elapsed=12.4s world=test2`。`elapsed`/`world` は値が無ければ欄ごと省略する。`user_id` は載せない。
    呼び出し元の縮退契約と独立に自衛する（例外を外へ出さない・ログ出力の失敗が呼び出し元の成否に
    影響してはならない）。
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
        _usage_log.info(" ".join(parts))
    except Exception:
        pass


@contextmanager
def suppress():
    """このスレッドの `record()` を一時的に無効化する（S2 RV MED 是正・2026-07-17）。

    読み取り専用契約の経路が、計測有効時でも `usage_events` へ書かないためのガード
    （旧・A/B ハーネス `graph_ab.run_ab` 向けに導入したが、その唯一の利用者は GRAPH-SRC
    〔2026-09-04〕で撤去済み・汎用の抑制プリミティブとして残置）。計装済みの下位関数が内部で
    `record()` を呼んでも、このスコープ内では DB 書込が起きない。ネスト安全（再入時は外側の
    状態を復元）。
    """
    prev = getattr(_local, "suppress", False)
    _local.suppress = True
    try:
        yield
    finally:
        _local.suppress = prev


# ---- スレッドローカルのアキュムレータスタック ----
# 1回の意味のある呼び出し単位（embed バッチ群・rag.md 成形の world 単位パス 等）の中で、複数回の
# HTTP 応答（リトライ・ページ/バッチごと）から得た usage を合算し、最後に1行として record() する。

_local = threading.local()


def _stack() -> list:
    st = getattr(_local, "stack", None)
    if st is None:
        st = []
        _local.stack = st
    return st


def acc_begin() -> None:
    """アキュムレータをスタックに push（`{'calls':0,'tokens':None,'t0':<monotonic>}`）。
    呼び出し元は必ず try/finally で対にする。`t0`（LOG-UX・2026-09-04）は `acc_end()` がこの
    フレームを pop するときの経過秒の起点（`acc_elapsed()` 参照）。"""
    try:
        _stack().append({"calls": 0, "tokens": None, "t0": time.monotonic()})
    except Exception:
        pass


def acc_add(usage) -> None:
    """1回の HTTP 応答分の usage を直近のスコープへ合算。スタックが空なら no-op（例外は出さない）。

    `suppress()` 中は外側の既存スコープにも**合算しない**（S2 RV 2巡目是正: 外側スコープ内から
    `run_ab` を呼んだ場合に A/B の usage が別 kind へ混入して記録される合成可能性の穴を塞ぐ）。
    """
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
    """直近のスコープを pop して `(tokens|None, calls)` を返す。スタックが空なら安全に `(None, 0)`。
    既存の返り値契約は不変（LOG-UX で拡張しない）——pop したフレームの経過秒は `acc_elapsed()`
    が別途読む一時値として `_local` に残す。"""
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


_ELAPSED_FRESHNESS_SEC = 5.0   # acc_elapsed() が拾える猶予（下記 docstring 参照）


def acc_elapsed() -> float | None:
    """直近の `acc_end()` が pop したフレームの経過秒（LOG-UX・2026-09-04）。1回読んだら消費する
    （次に呼んでも None）——`record()` が成功パスで1回だけ読み、ログへ添える。

    `acc_end()` を呼んだのに `record()` を呼ばない/呼べない経路がある（例: `embeddings.embed()` の
    `if n: record(...)`＝キャッシュヒットのみで実 HTTP 呼び出しが0件だった場合は記録しない）。
    その場合ここに経過秒が残ったままになりうるため、`_ELAPSED_FRESHNESS_SEC` 秒より古ければ
    無関係な後続の `record()` 呼び出しへ誤って乗らないよう None を返す（fail-safe＝古ければ省略）。
    """
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
# 各々 `{input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens}` の辞書、または
# usage 情報が無ければ None を返す（例外は出さない）。agentic_search.py（es_index を引き込む）を
# ingest 経路に持ち込みたくないためコード重複させる（意図的・DRY より依存の軽さを優先）。

def usage_from_openai_chat(resp) -> dict | None:
    """OpenAI Chat Completions の usage（`sherpa/providers/openai.py::_openai_usage` と同式）。"""
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


def usage_from_gemini(data) -> dict | None:
    """Gemini の usageMetadata（`agentic_search._acc_gemini_usage`／`providers/gemini._gemini_usage` と
    厳密に同式＝ output=candidatesTokenCount のみ・reasoning=thoughtsTokenCount）。"""
    try:
        um = (data or {}).get("usageMetadata")
        if not isinstance(um, dict):
            return None
        return {"input_tokens": um.get("promptTokenCount"),
                "cached_input_tokens": um.get("cachedContentTokenCount"),
                "output_tokens": um.get("candidatesTokenCount"),
                "reasoning_output_tokens": um.get("thoughtsTokenCount")}
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


def usage_from_anthropic(u) -> dict | None:
    """Anthropic/Bedrock の usage（dict/SDK オブジェクト両対応。`sherpa/providers/bedrock.py::_anthropic_usage_raw`
    と同式＝ input = input_tokens + cache_read + cache_creation・cached = cache_read）。"""
    try:
        if u is None:
            return None

        def _g(key):
            return u.get(key) if isinstance(u, dict) else getattr(u, key, None)

        base = _g("input_tokens") or 0
        read = _g("cache_read_input_tokens") or 0
        creation = _g("cache_creation_input_tokens") or 0
        return {"input_tokens": base + read + creation,
                "cached_input_tokens": read,
                "output_tokens": _g("output_tokens") or 0,
                "reasoning_output_tokens": 0}
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
