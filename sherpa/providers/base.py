"""思考プロバイダの共通基盤。

`Ctx`（プロバイダへ渡す文脈）・`_node`/`_gather`（共通の前段＝理解→意図→実ツール取得）・`_plain_run`（ナレッジ参照オフの素の会話）・
`_usage_meta`（usage メタの標準形）・`Provider`（頭脳の抽象基底）・`_TOOLS`/`_LENS_INTENT`・`_log` と、
Codex 経路が使う証拠種別の判定（`_scope_evidence_kinds` 等）を集約する。
`_gather` は `from sherpa import agents as _facade` で実行時解決して呼ぶ（`sherpa.agents` の差し替えを効かせるため）。
設計: docs/design/chat.md「文脈と構成」
"""
from __future__ import annotations

import logging
import threading  # noqa: F401 -- Ctx.stop_event の型注釈（文字列 forward-ref）で参照（元コードのまま）
import time
from dataclasses import dataclass
from typing import Callable, Iterator

from .. import layer as layer_mod
from .prompts import _NO_PRESEARCH_HEADLINE

_log = logging.getLogger("sherpa")
# Codex CLI 実行の専用ログ（`data/run/codex.log`）。実行1回につき開始/終了2行だけを INFO で書く（本文・資料名は書かない）。
_log_codex = logging.getLogger("sherpa.codex")

# レンズ→使うツールのノード（troubleshoot は2つ）。author は qa と同じ「文書を検索」。
_TOOLS = {
    "investigate": [("tool-docs", "文書を検索")],
    "impact": [("tool-graph", "関係グラフを照会")],
    "qa": [("tool-docs", "文書を検索")],
    "troubleshoot": [("tool-graph", "関連を確認"), ("tool-docs", "運用手順を検索")],
    "author": [("tool-docs", "文書を検索")],
}
_LENS_INTENT = {"investigate": "資料とソースを調べます", "impact": "変更の影響をたどります", "troubleshoot": "原因の手がかりを集めます",
                "qa": "仕様の記述を探します", "author": "作成の根拠を集めます"}


@dataclass
class Ctx:
    """プロバイダに渡す文脈。"""
    message: str
    world: str
    route: Callable[[str], dict]  # message -> {"lens","input","reason"}
    dispatch: Callable[[str, str], dict]  # (lens, input) -> answer envelope（出典つき）
    pace: float = 0.0
    knowledge: bool = True  # False＝ナレッジ参照オフ＝検索せず素の会話
    scope_meta: dict | None = None  # 参照中の範囲（world/scope_paths/source）
    make_sources: Callable[[list], list] | None = None  # doc_id[] -> sources[]（agentic 結果に出典を付与）
    uid: str = "admin"  # 現在ユーザー uid（互換モードは 'admin'）
    personal_facts: str = ""  # 個人ファイルのヒット（ナレッジオフ/agentic 経路にも注入）
    personal: bool = False  # 個人ファイル参照トグルが ON のターン（ヒットの有無に関わらず）
    stop_event: "threading.Event | None" = None  # 途中停止（/chat/turns/{turn_id}/stop が set する）
    # 直前ターンの (user, assistant) 完全対（時系列順・件数と文字予算は chat_service で制限済み）。
    # 例: [{"role":"user","content":"…"},{"role":"assistant","content":"…"}, ...]。
    # message 文字列には混ぜない（grep クエリや確認ID の判定を汚さないため別チャネル）。
    history: list | None = None
    conversation_id: int | None = None  # Codex ネイティブ resume 向け。
    # この会話に紐づく直近の `codex_session_id`（CodexProvider だけが消費）。None＝新規セッション。
    # resume 失敗時は CodexProvider 内で新規セッションへ自動フォールバックする。
    codex_session_id: str | None = None
    # 前ターンまでの Codex 累計 usage（CodexProvider だけが消費）。
    # resume が効いたターンはこの値との差分を answer.usage にする（`session_id` が一致しなければ累計をそのまま使う）。
    codex_usage_prev_total: dict | None = None
    # 検索経路トグルの実接続可用性 snapshot（`agentic_search.tool_availability()`）。ターン先頭で1回だけ計算して使い回す（knowledge オフ時は None）。
    tools_availability: dict | None = None
    # chat_service がターン先頭（provider 呼出しより前）で取った `time.monotonic()`。provider はこれを起点に prepare を測る。None なら測らない。
    turn_started_mono: float | None = None
    # 資料を中心に調べる指定（investigate のターンだけ真・CodexProvider だけが消費）。
    doc_focus: bool = False


def _node(id, kind, label, detail, status):
    return {"type": "node", "id": id, "kind": kind, "label": label, "detail": detail, "status": status}


            # 今回の呼び出しが不正なら `set_claims` は state.claims を変更しない（既存分を維持）。
    # `_remapped` が空でも既存分を維持する。


def _gather(ctx: Ctx, *, skip_presearch_lenses: frozenset = frozenset()):
    """共通の前段（理解→意図→実ツール取得）を node として流し、最後に `_env` を返す。
    取得（Neo4j/grep）は全プロバイダ共通。
    `ctx.dispatch` が実行不能と判定すると env に `_tools_blocked=True` が載る。pop して "done" ノードの文言を「使う検索が無効です」に切り替える。
    `skip_presearch_lenses`（MCP 付き Codex 経路専用）に入るレンズは下調べを行わず、`agentic_failure="error"` 付きの最小 env を返す（`_NO_PRESEARCH_HEADLINE`）。
    """
    def pace():
        if ctx.pace:
            time.sleep(ctx.pace)

    yield _node("understand", "think", "質問を理解", "ご質問を確認しています", "active")
    pace()
    yield _node("understand", "think", "質問を理解", "内容を把握しました", "done")

    yield _node("intent", "think", "意図を特定", "何を調べるか決めています", "active")
    decision = ctx.route(ctx.message)
    lens = decision["lens"]
    if lens == "clarify":  # 意図が曖昧→本人に確認してここで停止
        yield _node("intent", "think", "意図を特定", "どの調べ方か確認します", "done")
        yield decision["question"]
        return  # `_env` を出さない＝呼び元は env is None で停止
    pace()
    yield _node("intent", "think", "意図を特定", _LENS_INTENT.get(lens, ""), "done")

    if lens in skip_presearch_lenses:
        env = {"lens": lens, "headline": _NO_PRESEARCH_HEADLINE, "summary": {"total": 0},
              "data": {}, "sources": [],
              "agentic_failure": "error",  # 未実行のターン＝終了理由の分布で完了扱いにしない
              "scope": layer_mod.scope_with_layer(ctx.scope_meta, world=ctx.world, lens=lens)}
        yield {"type": "_env", "decision": decision, "env": env}
        return

    tools = _TOOLS.get(lens, [])
    for tid, tlabel in tools:
        yield _node(tid, "tool", tlabel, "照会しています", "active")
    env = ctx.dispatch(lens, decision["input"])
    blocked = env.pop("_tools_blocked", False)  # 使う検索が全て OFF/不達で未実行
    total = env.get("summary", {}).get("total", 0)
    for tid, tlabel in tools:
        pace()
        detail = "使う検索が無効です（詳細で ON にしてください）" if blocked else f"{total}件を確認"
        yield _node(tid, "tool", tlabel, detail, "done")
    yield {"type": "_env", "decision": decision, "env": env}


def _plain_run(provider: "Provider", ctx: Ctx) -> Iterator[dict]:
    """ナレッジ参照オフ＝検索せず、定型文（`_plain_text`）を返す。
    envelope は `lens="chat"`・`sources=[]`・`scope.source="off"`。個人ファイルの事実があれば env に載せる。
    """
    yield _node("understand", "think", "質問を理解", "内容を把握しました", "done")
    yield _node("brain", "think", f"考える（{provider.label}）", "一般知識で回答中（ナレッジ参照オフ）", "active")
    # 途中停止済みのターンは定型文を返しても失敗として数えない。
    already_stopped = ctx.stop_event is not None and ctx.stop_event.is_set()
    headline = provider._plain_text(ctx.message)
    yield {"type": "answer_delta", "text": headline}
    yield _node("brain", "think", f"考える（{provider.label}）", "（応答なし）", "done")
    env = {"lens": "chat", "headline": headline, "summary": {"total": 0}, "data": {},
           "sources": [], "scope": {"world": ctx.world, "scope_paths": [], "source": "off"}}
    if not already_stopped:
        # 定型文を返したターンは終了理由の分布で完了として数えない（`stop_kind.resolve`）。
        env["agentic_failure"] = "error"
    # personal_facts を env に乗せる。
    if ctx.personal_facts:
        env["_personal_facts"] = ctx.personal_facts
    yield {"type": "_result", "env": env,
           "decision": {"lens": "chat", "input": ctx.message, "reason": "ナレッジ参照オフ"}}


# ---- トークン使用量メタ ----
def _usage_meta(provider_id: str, model: str | None, *, input_tokens=0, cached_input_tokens=0,
                output_tokens=0, reasoning_output_tokens=0, cache_write_tokens=None,
                is_local: str | None = None, system_settings: dict | None = None) -> dict:
    """answer メタに載せる usage の標準形（無い項目は 0・cached ⊆ input・reasoning ⊆ output）。
    `cache_write_tokens`（キャッシュへの書き込み量）だけは、プロバイダが返さなかった場合を 0 と区別するため、不明（None）なら項目ごと載せない。
    `is_local` は担当バッジ用の判定（`agent_constructs.is_local`：local/on_prem/cloud/cloud_compat、判定不能は None）。`provider_id="codex"` は呼び出し元が明示的に渡す。
    `system_settings`（省略可）は `provider_id="openai"` の判定に使う。
    """
    def _i(v):
        try:
            return max(int(v or 0), 0)
        except (ValueError, TypeError):
            return 0
    if is_local is None:
        from .. import agent_constructs
        is_local = agent_constructs.is_local(provider_id, system_settings=system_settings)
    meta = {"provider": provider_id, "model": model or "",
            "input_tokens": _i(input_tokens), "cached_input_tokens": _i(cached_input_tokens),
            "output_tokens": _i(output_tokens), "reasoning_output_tokens": _i(reasoning_output_tokens),
            "is_local": is_local}
    if cache_write_tokens is not None:
        meta["cache_write_tokens"] = _i(cache_write_tokens)
    return meta


def _log_chat_usage(usage: dict, elapsed: float | None = None, world: str | None = None) -> None:
    """`kind="chat"` は `metering.record()` を通らないため、`sherpa.usage` ロガーへの1行はここから出す。
    最終合成の確定箇所だけで呼ぶ（査読ループ等の中間呼び出しでは呼ばない＝二重計上になる）。
    `depth_profile`/`reasoning`/`max_turns`/`max_tools_per_turn` が usage に入っていればログ1行にも足す。
    """
    try:
        from .. import metering
        tokens = {"input_tokens": usage.get("input_tokens"),
                  "cached_input_tokens": usage.get("cached_input_tokens"),
                  "cache_write_tokens": usage.get("cache_write_tokens"),
                  "output_tokens": usage.get("output_tokens")}
        reasoning = usage.get("reasoning")
        if reasoning is None and usage.get("max_turns") is not None \
                and usage.get("max_tools_per_turn") is not None:
            reasoning = f"turns={usage['max_turns']}/tools={usage['max_tools_per_turn']}"
        metering.log_usage_line("chat", usage.get("provider"), usage.get("model"), tokens,
                                1, world, elapsed, depth=usage.get("depth_profile"), reasoning=reasoning)
    except Exception:
        pass


# ---- Evidence Packet 組み立て・出典（sources）の機械検証 ----
# doc 実在チェックは `agentic_search._commit_evidence` が最終回答の生成前に行う。citation dict は検証結果で書き換えない。
# 本モジュールは Committed Evidence 化済みの `cites`・`evidence_meta` を集約して Evidence Packet を組む。


def _verified_sources(make_sources, docs: set, world: str, scope_paths=None) -> tuple[list, list]:
    """`docs`（run_tool が触れた doc_id）を機械検証で絞ってから `sources`（出典フッター・原本 DL リンク）を組む。
    戻り値 `(sources, verified_doc_ids)`（`verified_doc_ids` は実在確認を通った doc_id の昇順 list）。`make_sources` が None なら `([], [])`。
    """
    if make_sources is None:
        return [], []
    from .. import agentic_search
    verified_ids = sorted(d for d in docs if agentic_search.verify_doc_exists(d, world, scope_paths))
    return make_sources(verified_ids), verified_ids


class Provider:
    """思考イベントを yield する頭脳。`run(ctx)` は node… ＋ 最後に `_result` を返す。"""
    label, model = "頭脳", ""
    provider_id = ""  # usage メタ・統計の provider 名（AGENT_PROVIDERS と一致）。既定は空＝usage なし。
    system_prompt = ""  # ユーザ設定の回答方針。LLM 系は system メッセージとして前置する。

    def run(self, ctx: Ctx) -> Iterator[dict]:
        raise NotImplementedError

    def _plain_text(self, message: str = "") -> str:
        # message は既定未使用（作成意図で分岐する頭脳のみ利用）。
        return ("ナレッジ参照はオフです。社内資料は参照していません。資料に基づく回答が必要なら、"
                "入力欄の「ナレッジ参照」をオンにしてください。")

    def _agentic_target_check(self) -> None:
        """`tool_availability` より前に呼ぶ、接続先の I/O-free 許可判定（SSRF チョークポイント）。
        既定は no-op。接続先が設定依存の provider はオーバーライドし、不許可の宛先なら例外で fail-closed に止める。
        """


def _evidence_gate_note(missing_kinds, unavailable_kinds) -> str:
    """清書へ渡す「根拠の不足」注記（本文・資料名は含めない・種別の平文ラベルだけ）。
    `missing_kinds`: 必要なのに確認できなかった種別。`unavailable_kinds`: 範囲・探す対象にそもそも存在しない種別（不足ではなく「該当なし」として明示する）。
    """
    from .. import investigation_state as _inv
    parts = []
    if missing_kinds:
        parts.append(f"{_inv.evidence_kind_labels(missing_kinds)}を確認できていないため、"
                     "この点は確定できません。")
    if unavailable_kinds:
        parts.append(f"{_inv.evidence_kind_labels(unavailable_kinds)}は今回の範囲にありません"
                     "（該当なし）。")
    return "".join(parts)


# 範囲内の根拠種別の判定（`_scope_evidence_kinds`）に許す台帳走査の上限秒。超過＝判定不能＝必須種別をそのまま適用する。
_SCOPE_KINDS_WALK_SECONDS = 5.0

# 台帳の列挙だけでは充足を判定しきれない種別（`log_config`）。個人ファイルの grep ヒットは台帳にも `state.evidence` にも載らないため、
# 主張単位のゲートと層の判定ではターン単位の充足（`_turn_evidence_kinds`）を認める。範囲の実在判定からは除外しない。
_KINDS_OUTSIDE_LEDGER = frozenset({"log_config"})

# 範囲判定（`_scope_evidence_kinds`）のプロセス内キャッシュ。判定不能（`None`）は保持しない。
_SCOPE_KINDS_CACHE_SECONDS = 60.0
# 保持する組（world・範囲・層）の上限。期限切れを捨てても足りないときは書き込みを諦める。
_SCOPE_KINDS_CACHE_MAX = 64
_scope_kinds_cache: dict = {}
_scope_kinds_cache_lock = threading.Lock()


def _scope_kinds_cache_key(world: str, scope_paths, layer) -> tuple:
    return (world, tuple(sorted(scope_paths or ())), layer_mod.normalize_layer(layer))


def _scope_evidence_kinds(world: str, scope_paths, layer, *,
                          deadline: float | None = None) -> tuple[set, set] | None:
    """このターンの範囲に実在する根拠種別と、そのうち探す対象（層）で読める種別の2つ組 `(範囲内, 層内)`。
    範囲は world root の実ファイル走査（`scope_infer.safe_files`）と同じ範囲・層フィルタで列挙する（台帳は Office 原本を派生MDが無いと載せないため使わない）。
    走査失敗・期限超過は `None`（判定不能として必須種別を落とさない）。結果は `(world, 範囲, 層)` 単位で `_SCOPE_KINDS_CACHE_SECONDS` 秒だけ保持する。
    """
    from .. import investigation_state as _inv
    from .. import scope as scope_mod
    from .. import scope_infer as si
    from .. import worlds
    from ..ingest import importance, text_kind
    _key = _scope_kinds_cache_key(world, scope_paths, layer)
    _now = time.monotonic()
    with _scope_kinds_cache_lock:
        _hit = _scope_kinds_cache.get(_key)
        if _hit is not None and _hit[0] <= _now:
            _scope_kinds_cache.pop(_key, None)  # 期限切れは持ち続けない
            _hit = None
    if _hit is not None:
        return _hit[1]
    if deadline is None:
        deadline = time.monotonic() + _SCOPE_KINDS_WALK_SECONDS
    wd = worlds.world_dir(world)
    if not wd:
        _log.warning("範囲内の根拠種別を判定できませんでした（world root 不明・必須種別はそのまま適用します）")
        return None
    try:
        entries = list(si.safe_files(wd, deadline=deadline, also=worlds.archives_dir(world)))
    except Exception:
        _log.warning("範囲内の根拠種別を判定できませんでした（必須種別はそのまま適用します）",
                     exc_info=True)
        return None
    if not entries:
        # 0 件は root 解決失敗・マウント切断でも起こるため、必須種別を全部「該当なし」へ倒さない。
        _log.warning("範囲内の文書を1件も列挙できませんでした（必須種別はそのまま適用します）")
        return None
    in_scope, in_layer = set(), set()
    for _rp, rel in entries:
        if importance.is_importance_control_path(rel):  # 重要度設定ファイルは文書ではない（検索・精読も除外する）
            continue
        if text_kind.is_sensitive_doc_id(rel):  # 秘匿名: 根拠種別の判定対象に含めない
            continue
        if not scope_mod.in_scope(rel, scope_paths):
            continue
        k = _inv.evidence_kind_of_doc(rel)
        if not k:
            continue
        in_scope.add(k)
        if layer_mod.in_layer_code(layer_mod.layer_of(rel) == "code", layer):
            in_layer.add(k)
    for acc in (in_scope, in_layer):
        if "source" in acc:
            # 呼出関係はソースへの呼出し検索（ripgrep）で代替できるため、ソースがあれば「該当なし」にしない。
            acc.add("callgraph")
    with _scope_kinds_cache_lock:
        if len(_scope_kinds_cache) >= _SCOPE_KINDS_CACHE_MAX:
            _now = time.monotonic()
            for _k in [k for k, v in _scope_kinds_cache.items() if v[0] <= _now]:
                _scope_kinds_cache.pop(_k, None)
        if len(_scope_kinds_cache) < _SCOPE_KINDS_CACHE_MAX:
            _scope_kinds_cache[_key] = (time.monotonic() + _SCOPE_KINDS_CACHE_SECONDS,
                                        (in_scope, in_layer))
    return in_scope, in_layer


