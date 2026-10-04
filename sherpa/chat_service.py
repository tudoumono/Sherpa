"""チャット・オーケストレーション。会話メッセージ → ルーティング（chat_router）→ 既存レンズ（run_impact/run_troubleshoot/run_qa）→ 答えエンベロープ（見出し＝答え＋するべきこと／本体／出典フッター＝原本 DL）→ 会話に永続（store）。
設計: docs/design/chat.md「1ターンの流れ」
`stream_message` は同じ流れを思考ステップとして逐次 yield する（SSE・右ペインの「思考の流れ」）。レンズ振り分けは利用者に見せず、出典は必ず付けて原本 DL リンクにする。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import quote

from . import agent_constructs, agentic_search, app_version, exec_event, intent_llm, scope, store, text_encoding, worlds
from . import depth_profile as depth_profile_mod
from . import investigation_state as investigation_state_mod
from . import layer as layer_mod
from . import stop_kind as stop_kind_mod
from . import tools_pref as tools_pref_mod
from .ingest import importance, text_kind
from .agents import AGENT_PROVIDERS, Ctx, get_provider
from .chat_router import clarify_decision as _clarify_decision
from .chat_router import confirm_first_decision as _confirm_first_decision
from .chat_router import decision_for as _decision_for
from .chat_router import extract_slash_lens as _extract_slash_lens
from .chat_router import route as _heuristic_route
from .chat_router import wants_confirm_first as _wants_confirm_first
from .impact_service import IMPACT_MAX_DEPTH, IMPACT_MAX_DEPTH_ABS_MAX, run_impact
from .ingest.analyzers import registry as _analyzer_registry
from .ingest.world_neo4j import (
    GRAPH_OVERLOAD_USER_MESSAGE,
    GRAPH_SCHEMA_ERA_USER_MESSAGE,
    GraphQueryOverloadError,
    GraphSchemaEraError,
    world_graph_is_empty,
)
from .lens_service import (
    TROUBLESHOOT_GRAPH_DEPTH,
    TROUBLESHOOT_GRAPH_DEPTH_ABS_MAX,
    _run_capped,
    run_qa,
    run_troubleshoot,
)
from .store import investigation_records as store_investigation

# qa レンズの `run_qa` が直接 grep する経路固有の既定ヒット上限（`agentic_search.MAX_HITS` とは別）。`_dispatch()` と管理画面基準値（`routers/system_extras.py::_admin_settings_view`）が参照する唯一の定義。
QA_MAX_HITS_DEFAULT = 20

_log = logging.getLogger("sherpa")

_LENSES = ("impact", "troubleshoot", "qa", "author")

# trace 保存の上限。1 ターンのノード数は ask_user 再開や troubleshoot の複数ツール呼びで増え得るため、無制限に肥大化しないよう保険の上限を設ける。
_MAX_TRACE_NODES = 120
# detail は grep/ES 抜粋やツール引数の要約。UI ではチップ化して短く見せるため 200 文字で足りる。
_MAX_TRACE_DETAIL_CHARS = 200

# v2（`_cap_trace_v2`）の二段上限。ソフト上限は親を必ず残す規則のため、親の数が多いと実効上限にならない。ハード上限はその場合の絶対的な安全弁（病的ケースは honest failure マーカーで切り詰める）。
_MAX_TRACE_NODES_HARD = 400
# trace 列をシリアライズしたバイト数の安全弁。ノード数が少なくても個々のノードの metrics/evidence_ids が肥大化するケースを守る。
_MAX_TRACE_BYTES = 1_000_000
# 集約ノード 1 件に載せる evidence_ids の上限。超過分は件数だけ `metrics.omitted_evidence_count` に残す。
_MAX_TRACE_AGGREGATE_EVIDENCE_IDS = 20

# 会話継続＝履歴 priming。追質問が前ターンを理解しない問題を避けるため、直近ターンの (user, assistant) 完全対を `Ctx.history` として全 provider に注入する（Codex ネイティブ resume は別経路）。
# トークン計測ツールが無いため、対数キャップ＋文字予算の二重キャップで近似する。
_HISTORY_TURNS = 6  # 直近 N 対（対数キャップ）
_HISTORY_MSG_CHARS = 1200  # 1 メッセージの上限文字数
_HISTORY_CHAR_BUDGET = 6000  # 履歴全体の文字予算


def _trace_bytes(values) -> int:
    """trace 候補列（dict の値の並び）を、実保存と同じシリアライズで測ったバイト数。
    実保存（`store/conversations.py` の `Json(trace)`）は `json.dumps` の既定（`ensure_ascii=True`）のため、日本語は UTF-8 直書きよりバイト数が増える。SSE 側（`chat_turns.TurnBuffer.append`・`ensure_ascii=False`）とは意図的に異なる（ここが守るのは DB 保存サイズ）。
    `default=str` は実保存と一致させない（クラッシュさせない保険）。
    """
    return len(json.dumps(list(values), ensure_ascii=True, default=str).encode("utf-8"))


def _within_hard_limits(nodes: dict) -> bool:
    return len(nodes) <= _MAX_TRACE_NODES_HARD and _trace_bytes(nodes.values()) <= _MAX_TRACE_BYTES


def _order_by_age(nodes: dict, age: dict) -> list:
    """age（元の挿入順インデックス。集約ノードは代表した中で最も古いものの値）昇順で並べる。"""
    return sorted(nodes.values(), key=lambda n: age.get(n["id"], -1))


def _capped_evidence_ids(members: list) -> tuple:
    """集約対象ノード群の evidence_ids を和集合化し `_MAX_TRACE_AGGREGATE_EVIDENCE_IDS` 件で切る。戻り値は (切り詰め後のリスト or None, 切り捨てた件数)。"""
    all_ids = sorted({eid for n in members for eid in (n.get("evidence_ids") or [])})
    capped = all_ids[:_MAX_TRACE_AGGREGATE_EVIDENCE_IDS]
    return (capped or None), (len(all_ids) - len(capped))


def _aggregate_node(id: str, kind: str, label: str, detail: str, *, parent_id, agent_run_id,
                    members: list, event_type: str | None = None) -> dict:
    """複数ノードを 1 件へ畳んだ集約イベントを組み立てる（`exec_event._build_reserved_event` 経由・id は `_group_id`/`_subtree_id` の予約名前空間のみ）。件数は必ず `metrics.omitted_count` に載せる。"""
    evidence_ids, omitted_evidence = _capped_evidence_ids(members)
    metrics = {"omitted_count": len(members)}
    if omitted_evidence:
        metrics["omitted_evidence_count"] = omitted_evidence
    return exec_event._build_reserved_event(id, kind, label, detail, "done", event_type=event_type,
                                            parent_id=parent_id, agent_run_id=agent_run_id,
                                            metrics=metrics, evidence_ids=evidence_ids)


def _group_id(parent_id, kind, agent_run_id) -> str:
    """(parent_id, kind, agent_run_id) を null を区別した正規 JSON 配列にして sha1 の全 40 桁で表す（切り詰めない）。
    `exec_event.RESERVED_ID_PREFIXES` の `"trace-omitted:"` を名乗り、通常イベント（`build_event`）はこの id 空間を使えない。
    """
    canon = json.dumps([parent_id, kind, agent_run_id], ensure_ascii=False)
    return f"trace-omitted:{hashlib.sha1(canon.encode('utf-8')).hexdigest()}"


def _subtree_id(root_id: str) -> str:
    """`exec_event.RESERVED_ID_PREFIXES` の `"trace-subtree:"` を名乗る（`_group_id` と同型・全 40 桁）。"""
    return f"trace-subtree:{hashlib.sha1(root_id.encode('utf-8')).hexdigest()}"


def _normalize_effective_parents(items: dict) -> tuple:
    """各ノードの実効 parent_id を返す（現集合内に無い参照は親なしへ正規化）。戻り値は (id→実効 parent_id の dict, 正規化した件数)。出力ノードの `parent_id` の書き換えは呼び出し元 `_cap_trace_v2` が行う。"""
    effective_parent: dict = {}
    dangling = 0
    for nid, node in items.items():
        pid = node.get("parent_id")
        if pid is not None and pid not in items:
            dangling += 1
            pid = None
        effective_parent[nid] = pid
    return effective_parent, dangling


def _split_leaves_for_soft_budget(leaf_ids: list, items: dict, effective_parent: dict, budget: int) -> tuple:
    """末端（leaf）のうち、個別維持する末尾側と、集約する先頭（古い）側を決める。
    集約ノードの数も budget に数え、`(個別維持数 + 集約グループ数) <= budget` を満たすまで古い方から集約対象へ回す。全件を集約しても収まらなければ全件を集約対象として返す（ハード上限段階が引き継ぐ）。
    """
    n = len(leaf_ids)
    if n <= budget:
        return leaf_ids, []

    def key(nid):
        node = items[nid]
        return (effective_parent.get(nid), node.get("kind") or "think", node.get("agent_run_id"))

    seen_groups: set = set()
    k = 0
    while k < n and (n - k) > budget:  # 集約コスト 0 と仮定した素朴な見積りまで進める
        seen_groups.add(key(leaf_ids[k]))
        k += 1
    while k < n and (n - k) + len(seen_groups) > budget:  # 集約ノード自体の分をさらに削る
        seen_groups.add(key(leaf_ids[k]))
        k += 1
    return leaf_ids[k:], leaf_ids[:k]


def _soft_cap_v2(items: dict, effective_parent: dict, age: dict) -> dict:
    """①ソフト上限（`_MAX_TRACE_NODES`）: 親（他ノードの parent_id）は必ず残し、末端だけを (parent_id, kind, agent_run_id) 単位で件数つき集約ノードへ畳む。集約ノードも予算に数える。
    `age`/`effective_parent` は集約ノードの分をその場で拡張する（後段が引き継げるように）。
    """
    protected = {effective_parent[nid] for nid in items if effective_parent.get(nid) is not None}
    leaf_ids = [k for k in items if k not in protected]  # 挿入順（末尾＝最新）

    budget = max(0, _MAX_TRACE_NODES - len(protected))
    kept_leaf_ids, dropped_leaf_ids = _split_leaves_for_soft_budget(leaf_ids, items, effective_parent, budget)
    kept_leaf_set = set(kept_leaf_ids)

    groups: dict[tuple, list] = {}
    for nid in dropped_leaf_ids:
        node = items[nid]
        key = (effective_parent.get(nid), node.get("kind") or "think", node.get("agent_run_id"))
        groups.setdefault(key, []).append(node)

    result: dict[str, dict] = {}
    for nid in items:  # 元の挿入順を保持
        if nid in protected or nid in kept_leaf_set:
            result[nid] = items[nid]

    for key in sorted(groups, key=lambda g: (g[0] or "", g[1] or "", g[2] or "")):
        parent_id, kind, agent_run_id = key
        members = groups[key]
        gid = _group_id(parent_id, kind, agent_run_id)
        result[gid] = _aggregate_node(gid, kind, "（省略）",
                                      f"…{kind} 系のイベントを {len(members)} 件省略",
                                      parent_id=parent_id, agent_run_id=agent_run_id, members=members)
        effective_parent[gid] = parent_id
        age[gid] = min(age[m["id"]] for m in members)
    return result


def _hard_cap_v2(nodes: dict, effective_parent: dict, age: dict) -> dict:
    """②ハード上限（件数 `_MAX_TRACE_NODES_HARD`／バイト `_MAX_TRACE_BYTES`）: ①後も超過なら、最も古いサブツリー（ルート＋子孫すべて）から順に丸ごと 1 個の集約ノードへ畳む。
    サブツリー単位でしか消さないため親子リンクの断絶（orphan）は起きない。件数超過だけのときは単独ノードを畳んでも件数が減らないため skip し、バイト超過のときは畳む。
    """
    children: dict[str, list] = {}
    roots: list = []
    for nid in nodes:
        pid = effective_parent.get(nid)
        if pid is None or pid not in nodes:
            roots.append(nid)
        else:
            children.setdefault(pid, []).append(nid)

    def subtree_ids(root: str) -> list:
        out, stack = [], [root]
        while stack:
            cur = stack.pop()
            out.append(cur)
            stack.extend(children.get(cur, []))
        return out

    result = dict(nodes)
    for root in sorted(roots, key=lambda r: age.get(r, -1)):  # 古いサブツリーから
        over_count = len(result) > _MAX_TRACE_NODES_HARD
        over_bytes = _trace_bytes(result.values()) > _MAX_TRACE_BYTES
        if not over_count and not over_bytes:
            break
        if root not in result:
            continue  # 既に他サブツリーの一部として畳まれた
        member_ids = [i for i in subtree_ids(root) if i in result]
        if len(member_ids) <= 1 and not over_bytes:
            continue  # 件数超過だけなら単独ノードは畳んでも無意味
        members = [result.pop(i) for i in member_ids]
        sid = _subtree_id(root)
        result[sid] = _aggregate_node(
            sid, "think", "（省略）", f"…古いサブツリーを1件（{len(members)}件のイベント）省略",
            parent_id=None, agent_run_id=None, members=members)
        age[sid] = min((age.get(i, 0) for i in member_ids), default=0)
    return result


def _budget_limit_marker(omitted: int, original_total: int) -> dict:
    return exec_event._build_reserved_event(
        exec_event.BUDGET_LIMIT_REACHED_ID, "think", "（上限に到達）",
        f"…イベントが多すぎるため {omitted} 件を切り詰めました（元の合計 {original_total} 件）",
        "done", event_type="budget_limit_reached", metrics={"omitted_count": omitted})


def _budget_limit_truncate(nodes: dict, age: dict, original_total: int) -> list:
    """③ ①②でも超過する病的ケース: 先頭に `budget_limit_reached` マーカー 1 件を置き、末尾（最新）優先でハード上限件数ぴったりまで切り詰める（honest failure・この経路のみ親子リンクを保証しない）。
    件数を合わせた後もバイト超過なら、保持ノードを古い方から 1 件ずつ削る（マーカーは常に保持）。`kept` が単調に減るため高々 `len(kept)` 回で停止し、marker 単独の出力が最終形になる。
    """
    ordered = _order_by_age(nodes, age)
    keep_n = max(0, _MAX_TRACE_NODES_HARD - 1)  # マーカー 1 件分を確保
    kept = ordered[-keep_n:] if keep_n else []
    omitted = len(ordered) - len(kept)
    out = [_budget_limit_marker(omitted, original_total)] + kept
    while kept and _trace_bytes(out) > _MAX_TRACE_BYTES:
        kept = kept[1:]  # 保持ノードのうち最も古い 1 件を追加で削る
        omitted += 1
        out = [_budget_limit_marker(omitted, original_total)] + kept
    return out


def _cap_trace_v2(nodes: dict) -> list | None:
    """v2 trace の上限適用（全段とも決定的）。
    ① 正規化（件数に関わらず必ず実行し、出力ノードの `parent_id` も書き換える）。
    ② ソフト上限（`_soft_cap_v2`）。
    ③ ハード上限／バイト上限（`_hard_cap_v2`）。
    ④ それでも超過なら honest failure マーカー（`_budget_limit_truncate`）。
    """
    if not nodes:
        return None
    items = {k: {**v, "detail": (v.get("detail") or "")[:_MAX_TRACE_DETAIL_CHARS]} for k, v in nodes.items()}

    # 正規化は件数に関わらず必ず実行し、各ノードの parent_id を実効値へ書き換えてから以降の処理・高速経路の両方に流す（高速経路だけ正規化が反映されない抜け道を作らない）。
    effective_parent, dangling = _normalize_effective_parents(items)
    if dangling:
        _log.warning("trace v2: parent_id が現集合内に見つからないイベントが%d件あり、親なしへ正規化しました",
                     dangling)
    items = {k: {**v, "parent_id": effective_parent[k]} for k, v in items.items()}

    if len(items) <= _MAX_TRACE_NODES and _trace_bytes(items.values()) <= _MAX_TRACE_BYTES:
        return list(items.values())

    age = {nid: i for i, nid in enumerate(items)}

    stage1 = _soft_cap_v2(items, effective_parent, age)
    if _within_hard_limits(stage1):
        return _order_by_age(stage1, age)

    stage2 = _hard_cap_v2(stage1, effective_parent, age)
    if _within_hard_limits(stage2):
        return _order_by_age(stage2, age)

    return _budget_limit_truncate(stage2, age, original_total=len(nodes))


def _audit_chat_turn(uid, conversation_id, settings, *, lens, user_msg_id,
                     assistant_msg_id, world, scope_paths, personal, stopped: bool = False) -> None:
    """1 ターン完了時に監査へ記録する。detail に本文は入れず id とメタだけ持たせ、本文はエクスポート（`GET /admin/audit/export?include_chat_content=1`）が messages 台帳から join して付与する。
    clarify で終わったターンと、途中停止で assistant 未保存のターンは `assistant_msg_id=None` で記録する。停止したターンは detail に `stopped:true` を付ける（停止の終端を受けて assistant を保存した場合はその id を渡す）。
    失敗はチャット本体を止めない（fail-open）。
    """
    # `settings["agent"]` は自由文字列のため、監査 detail には生値でなく allowlist で正規化した値を入れる。
    provider_saved = (settings.get("agent") or agent_constructs.default_agent()).lower()
    provider_saved = provider_saved if provider_saved in AGENT_PROVIDERS else "unknown"
    # 実際にこのターンで使われたプロバイダ（`effective_agent()` 経由）を主フィールド `"provider"`、保存値を副フィールド `"provider_saved"` として両方残す。
    provider = agent_constructs.effective_agent(settings)
    provider = provider if provider in AGENT_PROVIDERS else "unknown"
    try:
        store.audit(uid, "chat.turn", "conversation", f"conv:{conversation_id}",
                   detail={"message_id_user": user_msg_id, "message_id_assistant": assistant_msg_id,
                           "lens": lens, "world": world,
                           "scope_paths": len(scope_paths or []), "personal": bool(personal),
                           "provider": provider, "provider_saved": provider_saved,
                           "stopped": bool(stopped)},
                   outcome="success", severity="info")
    except Exception as e:
        _log.warning("chat.turn audit write failed (fail-open, chat continues): %s", e)


def _is_stopped_terminal(ev) -> bool:
    """利用者の停止で打ち切った未完了回答の `_result` か（`providers/base.py::TERMINALS`）。巡ループの停止終端だけは、追加の LLM 呼び出しをせずコードで組んだ未完了回答を保存する（監査も `stopped=True` で assistant を保存した形）。それ以外のイベントは停止後は捨てる。"""
    return (isinstance(ev, dict) and ev.get("type") == "_result"
            and (ev.get("env") or {}).get("_terminal") == "stopped")


def _pop_round_personal(env: dict) -> bool:
    """巡ループが全巡で累積した個人由来／書込フラグ（`_personal_rounds`）を取り出す（内部キー・保存・共有へは残さない）。"""
    return bool(env.pop("_personal_rounds", False))


def _resolve_lens(lens, message):
    """調べ方の明示指定を解決する。優先順位: スラッシュ接頭辞（1 回限り）＞ `ChatReq.lens`（調べ方ブロックの明示選択）＞自動。
    返り値 `(explicit_lens, lens_source, lens_block, message)`。`explicit_lens` は `_build_router()` へ渡す値（`None`＝自動判定）。スラッシュ接頭辞は本文から取り除いた `message` を返す。
    `lens_block` は `lens` の継続設定を正規化した値（`None`/`"auto"` は `None`）で、スラッシュで上書きされても `_resolve_scope()` の `lens_block` として持ち越す。
    """
    lens_block = lens if lens and lens != "auto" else None
    slash_lens, stripped = _extract_slash_lens(message)
    if slash_lens:
        return slash_lens, "slash", lens_block, stripped
    if lens_block:
        return lens_block, "explicit", lens_block, message
    return None, "auto", lens_block, message


def _build_router(known, world, settings, can_ask, user_id=None, explicit_lens=None, scope_meta=None,
                  conversation_id=None):
    """hybrid intent ルータ。heuristic 確信 →（曖昧）LLM 分類 →（なお曖昧）clarify or qa fallback。
    - per-turn memoize: 同一ターンで route が複数回呼ばれても LLM 分類を二重実行しない。`can_ask` はストリーミングのみ True（非対話は qa fallback）。
    - `user_id`/`conversation_id` は intent 分類の利用量計測（`kind='intent'`）にだけ渡す。
    - `explicit_lens`（調べ方ブロックの明示指定・スラッシュ接頭辞含む）が非 None なら Tier1〜3 を飛ばし `chat_router.decision_for()` で直接組み立てる。「確認してから進めて」（`_wants_confirm_first`）は明示指定より優先する例外。
    - `scope_meta`: 確認カードの payload へ解決済みの探す対象・範囲・`lens_source`／`lens_block`／`tools` を載せる（knowledge オフ時は `None`）。回答の再送時に既存の経路へ 1 回だけ戻すための情報で、判定には使わない。
    """
    cache: dict = {}

    def _is_simple_agent() -> bool:
        # 実行時の頭脳選択（`_select_provider`）と同じ解決（保存値→既定）で判定する。
        try:
            return agent_constructs.effective_agent(settings) == "simple"
        except Exception:
            return False

    def _route(message):
        if message in cache:
            return cache[message]
        # 「確認してから進めて」は provider/dispatch に入る前に確認カードを出す決定的ガード（既存 clarify と同経路）。確認ID 付き（回答の再送）では発動しない。`can_ask=False` は通常判定へ委ねる。
        if can_ask and _wants_confirm_first(message):
            sm = scope_meta or {}
            d = _confirm_first_decision(message, lens=explicit_lens, layer=sm.get("layer"),
                                        scope_paths=sm.get("scope_paths"),
                                        lens_source=sm.get("lens_source"), lens_block=sm.get("lens_block"),
                                        tools=sm.get("tools"))
            cache[message] = d
            return d
        if explicit_lens:  # 調べ方の明示指定＝Tier1〜3 を飛ばす
            d = _decision_for(explicit_lens, message, known, reason="明示指定")
            cache[message] = d
            return d
        d = _heuristic_route(message, known_terms=known)
        if not d.get("confident") and _is_simple_agent():
            # 簡易は管理者設定の AI（`simple_chat._resolve_llm`）以外へ送らないため、意図分類の LLM は呼ばず、曖昧な入力は確認も挟まず qa に固定する。
            d = _decision_for("qa", message, known, reason="曖昧なため既定（検索）")
        elif not d.get("confident"):  # 曖昧時だけ Tier2/3（コスト最小・大半は無料）
            c = intent_llm.classify(message, settings, user_id=user_id, world=world,
                                    conversation_id=conversation_id)  # Tier2: 安価 LLM 分類（未接続/失敗は None）
            if c and c.get("lens") in _LENSES and c.get("confident", True):
                d = _decision_for(c["lens"], message, known, reason="AI判定（意図分類）")
            elif can_ask:
                d = _clarify_decision(message)  # Tier3: 本人に確認（ask_user と同経路）
            else:
                d = _decision_for("qa", message, known, reason="曖昧なため既定（検索）")  # 非対話 fallback
        cache[message] = d
        return d

    return _route

# 経路チップ：レンズ→使った経路（専門用語を出さない）。
_ROUTE_PATH = {"impact": ["関係を確認"], "troubleshoot": ["関連を確認", "文書を検索"], "qa": ["文書を検索"],
               "author": ["文書を検索", "資料を作成"]}
# 出典に出さない内部来歴マーカー（DL できる文書ではない）。`scope.NON_DOC` と共有する。
_NON_DOC = scope.NON_DOC
_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{6,}"
    r"|-----BEGIN[^-]+PRIVATE KEY-----[\s\S]*?-----END[^-]+PRIVATE KEY-----)"
)
_KV_SECRET_RE = re.compile(r"(?i)\b(pass(?:word|wd)?|secret|api[_-]?key|token|authorization)\b(\s*[=:]\s*)(\S+)")


_STREAM_PACE = 0.35  # 思考ステップ表示の間隔（秒）。テストは 0


def emit_pace() -> float:
    """思考ステップの間隔（秒）。テストは `_STREAM_PACE` を 0 にして即時。"""
    return _STREAM_PACE


def _known_terms(session, world) -> list:
    """起点語抽出のヒント（world 内の全ノード名）。knowledge=true の全チャットが通るため、`lens_service._run_capped`（timeout→空リスト・緊急天井→部分リストの warning 付き縮退）を再利用する。
    ルーティング補助のヒントなので、厳密解は不要でソフト縮退のままでよい。返却形は name 文字列のリスト。
    """
    from neo4j.exceptions import DriverError, TransientError
    try:
        rows = _run_capped(
            session, "MATCH (n:Entity {world_id:$v}) RETURN DISTINCT n.name AS name",
            log_world=world, v=world,
        )
    except (DriverError, TransientError) as e:
        # 接続断・一時障害は、ターン全体を落とさず空で続ける（落とすとグラフ不調時の縮退へ到達できない）。実行本体（`_dispatch`）が同じ障害を別途分類して縮退／honest failure を決める。
        _log.warning("起点語ヒントの取得に失敗（グラフ接続断・空で続行・world=%s）: %s",
                    world, type(e).__name__)
        return []
    return [r["name"] for r in rows if r["name"]]


def _src_url(doc: str, world: str, res: "importance.Resolution | None" = None) -> dict:
    """出典 1 件 → 原本 DL リンク（doc は rel_path で slash を含むため query で渡す）。
    `res`（省略可）: 登録者が `_重要度.txt` で付けた重要度の解決結果。あれば `importance`/`importance_reason` を追加する（`importance_source` は出さない）。無ければ従来どおり 2 キーのまま。
    """
    return {"doc_id": doc,
            "download_url": f"/documents/download?world={quote(world)}&rel={quote(doc, safe='')}",
            **importance.public_fields(res)}


def _sources(docs, world) -> list:
    seen, filtered = set(), []
    for d in docs:
        if (d and d not in seen and d not in _NON_DOC  # 内部来歴は出典に出さない
                and not importance.is_importance_control_path(d)):  # 重要度設定ファイル自体は出典に出さない
            seen.add(d)
            filtered.append(d)
    # world 全体を 1 回だけ解決し（`resolve_many`）、対象の doc だけ引く。未登録 world は解決しない（2 キーのまま）。
    wd = worlds.world_dir(world)
    sig = None
    # `sig` を渡さないと `resolve_for_world` が world 全体をもう一度全木走査する。registry の `last_sig`（`store.get_world_status_row`）を渡して省く。取得できなくても fail-closed にせず自前計算へ戻す。
    if wd:
        try:
            row = store.get_world_status_row(world)
            sig = (row or {}).get("last_sig") or None
        except Exception:
            sig = None
    res_map = importance.resolve_many(world, filtered, root=wd, sig=sig) if wd else {}
    return [_src_url(d, world, res_map.get(d)) for d in filtered]


def _redact(text: str) -> str:
    """外部 LLM/画面へ渡る検索抜粋の明らかな秘密を伏せる（agentic_search と同じ多層防御）。"""
    t = _SECRET_RE.sub("[REDACTED]", text or "")
    return _KV_SECRET_RE.sub(r"\1\2[REDACTED]", t)


def _truncation_headline_suffix(result) -> str:
    """レンズ結果の `notes`（`lens_service`/`impact_service` の打切り申告・平文）を headline へ足す。
    `notes` は headline に出ないため、そのままだと「見つかりませんでした」が探せていないだけの場合にも断定的に出てしまう。打切りが無ければ空文字。
    """
    notes = result.get("notes") or []
    return ("　⚠ " + " ".join(notes)) if notes else ""


def _answer_impact(result, world):
    """`items` は全件同格（構造的な影響として同じ扱い）。presumed（grep 共起の推定）だけは別枠のまま残す。"""
    items = result["items"]
    start = result["start"]
    presumed = result.get("presumed") or []  # 構造的な影響が 0 件時の「資料からの関連推定」
    code_silent = not items  # 構造的な影響が 0＝コードの少ない/無いフォルダのサイン
    if items:
        headline = f"「{start}」を変えると {len(items)}件に影響。"
    elif presumed:  # 構造的な影響は無いが資料から関連を辿れた＝0 で突き放さない
        names = "、".join(dict.fromkeys(p["name"] for p in presumed[:3]))
        headline = (f"「{start}」に構造的な依存は見つかりませんでしたが、資料から"
                    f"**関連の可能性**が {len(presumed)}件（推定・要確認）: {names} など。")
    else:
        headline = f"「{start}」の影響先は見つかりませんでした（表記ゆれ、または影響なし）。"
    if code_silent:  # 次の一手＝検索へ素直に誘導（フォルダ起因と断定しない）
        headline += "　▶ 資料の検索（仕様問い合わせ・トラブルシュート）で仕様/運用の記述を確認できます。"
    docs = [e["doc"] for it in items for e in it.get("evidence", []) if e.get("doc")]
    docs += [e.get("doc") for p in presumed for e in p.get("evidence", []) if e.get("doc")]
    headline += _truncation_headline_suffix(result)
    return {"headline": headline,
            "summary": {"total": len(items), "presumed": len(presumed), "code_silent": code_silent},
            "data": result, "sources": _sources(docs, world),
            # 検索へ誘導する次の一手（UI がボタン化できる・構造的な影響が無い時のみ）
            "suggest": ({"lens": "qa", "query": start, "reason": "構造的な影響が見つからない"} if code_silent else None)}


def _answer_troubleshoot(result, world):
    cands = result.get("candidates", [])
    top = [c["name"] for c in cands[:3]]
    headline = ("症状に対応する原因候補は見つかりませんでした。起点となる名称を含めて言い換えてください。"
                if not cands else f"原因候補 {len(cands)}件。確認すべき上位: {('、'.join(top))}。")
    docs = []
    for c in cands:
        ev = c.get("evidence", {})
        docs += [e.get("doc") for e in ev.get("edges", []) if e.get("doc")]
        docs += [g.get("doc_id") for g in ev.get("grep", [])]
    headline += _truncation_headline_suffix(result)
    return {"headline": headline, "summary": {"total": len(cands)},
            "data": result, "sources": _sources(docs, world)}


def _answer_qa(result, world):
    cites = result.get("citations", [])
    headline = ("該当する記述は見つかりませんでした（確証なし）。検索語を変えて試してください。"
                if not cites else f"該当箇所が {len(cites)}件見つかりました。")
    headline += _truncation_headline_suffix(result)
    return {"headline": headline, "summary": {"total": len(cites)},
            "data": result, "sources": _sources([c["doc_id"] for c in cites], world)}


def _resolve_scope(message, world, scope_paths, layer=None, lens_source="auto", lens_block=None,
                   web_search=False, depth_profile=None, tools=None, tools_explicit=None):
    """有効な範囲を決める。明示選択 ＞ world 全体（auto-scope 推定は行わない）。
    返り値 `{world, scope_paths, source, layer, lens_source, lens_block, web_search, depth_profile, tools}`。`source`=explicit/all（ヘッダ「参照中の範囲」用）。
    - `layer`（探す対象）: 省略（`None`）のときだけ `"both"` に正規化する。不正な内部値は `layer.normalize_layer` が `ValueError`（黙って丸めない）。
    - `lens_source`（調べ方の明示指定元）: `auto`｜`explicit`｜`slash`。既定 `"auto"`。
    - `lens_block`: `ChatReq.lens`（ブロックの継続設定・自動は `None`）そのもの。スラッシュは実効レンズ（`answer.lens`）だけを 1 回上書きしブロックの選択状態を変えないため、会話を開き直したときの復元は `lens_source=="slash"` なら `lens_block`、`"explicit"` なら実効レンズ、`"auto"` なら自動を使う（`web/chat/scope.js::applyConversationScope`）。
    - `web_search`（既定 False）: このチャットで Web 検索を希望したか。実際に反映されるかは `providers/codex/sandbox.py::_web_search_disabled_value` が判定する（ここでは復元用に希望値を記録するだけ）。
    - `depth_profile`: 省略（`None`）は `"standard"` に正規化（`depth_profile_mod.normalize_depth_profile`・不正値は `ValueError`）。
    - `tools`: 省略（`None`）は全 ON に正規化（`tools_pref_mod.normalize_tools_pref`・不正値は `ValueError`）。
    - `tools_explicit`（省略可）: 利用者が実際に切り替えた軸の記録（復元専用・実行には使わない）。`None` のときはキー自体を作らない。
    `layer_mod.scope_with_layer` がこの dict をコピーするため、`answer.scope` にそのまま伝わる（旧回答は `"auto"`／`None`／`False`／`"standard"`／全 ON 扱い）。
    """
    explicit = scope.normalize_scope_paths(scope_paths)  # strip/空除去/重複排除
    sm = {"world": world, "scope_paths": explicit, "source": "explicit" if explicit else "all",
          "layer": layer_mod.normalize_layer(layer), "lens_source": lens_source,
          "lens_block": lens_block, "web_search": bool(web_search),
          "depth_profile": depth_profile_mod.normalize_depth_profile(depth_profile),
          "tools": tools_pref_mod.normalize_tools_pref(tools)}
    if tools_explicit is not None:
        sm["tools_explicit"] = sorted({k for k in tools_explicit
                                      if k in tools_pref_mod.TOOLS_PREF_KEYS})
    return sm


def _es_hits(world, query, sp, k=8, redact=False, layer=None):
    """ES（BM25）上位ヒットを、現 world に実在する doc だけに絞って返す。BM25 のみ（`vector=False`・クエリ埋め込みコストを避ける）。
    現 world の実在集合は 1 回だけ作る（古い ES 索引由来のリンクを出さない）。`layer`（省略可）は qa 補完のときだけ渡す（troubleshoot 補完は渡さない）。
    """
    try:
        from . import documents, es_index
        valid = documents.world_rel_set(world)
        # `es_index.search()` は (hits, reason)。BM25 実クエリ失敗はこの経路に degraded 報告の仕組みが無いため捨てる（構造化された degraded 集計は `fused_search._search_keyword()`）。
        hits, _reason = es_index.search(world, query, scope_paths=sp, k=k, vector=False, layer=layer)
    except Exception:
        return []
    out = []
    for h in hits:
        doc = h.get("doc_id")
        # 秘匿名（更新前に索引化された `credentials.xlsx` 等）は facts/カードへ出さない。
        if doc and doc in valid and scope.in_scope(doc, sp) and not text_kind.is_sensitive_doc_id(doc):
            if redact:
                h = {**h, "text": _redact(h.get("text", ""))[:500]}
            out.append(h)
    return out


def _es_citations(world, query, sp, k=8, layer=None):
    """ES（BM25）上位ヒットを citation 形にする（Codex/非 agentic も ES を参照できるよう facts に混ぜる）。
    `rag_parent_return` で本文の完全性を上げたうえで、`excerpts.display_quote` で quote を人間向け MD の該当節へ引き直す。ES ヒットの選定自体は変えず、返す直前の後処理のみ。
    """
    from . import citations, excerpts, rag_parent_return
    hits = rag_parent_return.apply_to_hits(world, _es_hits(world, query, sp, k=k, layer=layer))
    out = []
    for h in hits:
        c = citations.from_es_hit(h, query)
        disp = excerpts.display_quote(world, h["doc_id"], c["quote"], chunk_id=h.get("chunk_id"),
                                      locator=h.get("locator"), section_path=h.get("section_path"))
        out.append(citations.with_display_excerpt(
            c, quote=disp["quote"], excerpt_source=disp["excerpt_source"],
            locator_hint=disp["locator_hint"], tier=h.get("tier")))
    return out


def _merge_qa_with_es(result, world, query, sp, layer=None):
    """`run_qa`(grep) の citations に ES ヒットを統合する。grep↔ES を交互に並べて（先頭付近に ES も入れる）doc_id+span で重複排除する。`layer` は qa の ES 補完にのみ渡す（troubleshoot 補完は `_merge_troubleshoot_with_es` が layer 無しで呼ぶ）。"""
    from . import citations
    grep = list(result.get("citations", []))
    es = _es_citations(world, query, sp, layer=layer)
    merged = citations.dedupe_round_robin_by_doc_span(grep, es)  # round-robin で先頭付近に ES も来る
    return {**result, "citations": merged, "answered": bool(merged)}


def _card_doc_spans(card: dict) -> set:
    ev = card.get("evidence", {}) or {}
    spans = set()
    for g in ev.get("grep", []):
        doc = g.get("doc_id")
        if doc:
            spans.add((doc, tuple(g.get("span") or [g.get("line"), g.get("line")])))
    return spans


def _dedupe_round_robin_cards(*groups) -> list:
    """原因候補カードを group 順の round-robin で並べ、名前または doc_id+span で重複排除する。"""
    gs = [list(g) for g in groups]
    out, seen_names, seen_spans = [], set(), set()
    for i in range(max((len(g) for g in gs), default=0)):
        for g in gs:
            if i >= len(g):
                continue
            card = g[i]
            name_key = (card.get("name"), card.get("label"))
            spans = _card_doc_spans(card)
            if (name_key[0] and name_key in seen_names) or (spans and spans <= seen_spans):
                continue
            if name_key[0]:
                seen_names.add(name_key)
            seen_spans |= spans
            out.append(card)
    return out


def _es_troubleshoot_cards(world, query, sp, k=8) -> list:
    """ES ヒットから原因候補カードを組む。evidence.grep の `text` も `excerpts.display_quote` で人間向け MD の該当節へ引き直す（`_es_hits(redact=True)` が redact+clip 済みの `text` を fallback に渡すため、`excerpt_source=="rag"` は無変更・`"human_md"` のときだけ redact+clip をかけ直す）。
    カードの UX 上限（500 字クリップ）は維持し、親返し（サイズ拡張）は適用しない（近傍 1 件＝1 カードの簡潔な一覧）。
    """
    from . import excerpts
    by_doc, order = {}, []  # doc ごとに 1 カード・複数 span は evidence.grep に集約
    for h in _es_hits(world, query, sp, k=k, redact=True):
        doc, line = h["doc_id"], h.get("line")
        disp = excerpts.display_quote(world, doc, h.get("text", ""), chunk_id=h.get("chunk_id"),
                                      locator=h.get("locator"), section_path=h.get("section_path"))
        text = _redact(disp["quote"])[:500] if disp["excerpt_source"] == "human_md" else disp["quote"]
        ev = {"doc_id": doc, "line": line, "span": [line, line],
              "text": text, "match": query, "score": h.get("score")}
        if disp["locator_hint"]:
            ev["locator_hint"] = disp["locator_hint"]
        card = by_doc.get(doc)
        if card is None:
            by_doc[doc] = {
                "name": doc, "label": "Document", "category": "文書",
                "role": "関連文書", "distance": None, "path": [], "source": "es",
                "evidence": {"edges": [], "grep": [ev]},
            }
            order.append(doc)
        elif (line, line) not in {tuple(g["span"]) for g in card["evidence"]["grep"]}:
            card["evidence"]["grep"].append(ev)  # 同一 doc の未見 span のみ追加
    return [by_doc[d] for d in order]


def _merge_troubleshoot_with_es(result, world, query, sp):
    """`run_troubleshoot`(グラフ+grep) の原因候補に ES 文書候補を統合する（非 agentic 専用）。"""
    base = list(result.get("candidates", []))
    es_cards = _es_troubleshoot_cards(world, query, sp)
    return {**result, "candidates": _dedupe_round_robin_cards(base, es_cards)}


# 検索経路トグルの honest-failure envelope は `agentic_search.tools_blocked_env` が唯一の定義。非 agentic（`_dispatch`）・agentic（`providers/base._agentic_run`）が同じ固定文言・サイドカー契約を共有する。


def _graph_empty_env(lens: str, eff: dict, qa_fallback_env) -> dict:
    """グラフ未構築（接続可・`:Entity{world_id}` が 0 件）で主クエリが 0 件だったときの下地。grep／全文検索のどちらかが残っていれば grep 相当の下地＋縮退の印（通知のみ・統計の bool は立てない）。どちらも無ければ実行不能として、接続断と同じ明示エラーで終える。"""
    if not (eff["grep"] or eff["fulltext"]):
        return agentic_search.tools_blocked_env(lens)
    env = qa_fallback_env()
    env["graph_degraded"] = agentic_search.GRAPH_EMPTY_CODE
    return env


def _dispatch(session, lens, payload, world, scope_meta=None, system_settings=None,
             tools_availability=None):
    """レンズ実行（＋範囲フィルタ）。範囲は world グラフ traversal(Cypher)＋grep/ES/根拠に効かせる。
    - 層フィルタ（探す対象）は qa（author も qa 分岐）にのみ適用する。impact／troubleshoot は言及エッジ（DOCUMENTS via=mention）が木を跨いで繋ぐため適用せず、`env["scope"]["layer_applied"]` で明示する。
    - 調べる深さ（`depth_profile`）: `run_impact`/`run_troubleshoot` の `depth`・`run_qa` の `max_hits` へ、実効基準値（`system_settings` → env → コード既定）に倍率を掛けた値を渡す。
      `system_settings`（省略可）は呼び出し元（`stream_message`）が読んだスナップショットで、ここでは DB を読まない。`None` は基準値の上書きなし。`abs_max` は倍率適用後に一度だけ縛る絶対上限。
    - 検索経路トグル（`scope_meta["tools"]`）: `agentic_search.dispatch_tools_for_lens` で実効ツール集合と実行可否を判定する。必須ツールが全て OFF/不達なら、OFF のツールへ黙ってフォールバックせず `agentic_search.tools_blocked_env` の明示エラーを返す。
      qa/author は grep（`run_qa`）と fulltext（ES 補完）のどちらか一方でも実行し、troubleshoot はグラフ必須で fulltext 補完のみを追加で切り替える。
      `tools_availability`（省略可）は呼び出し元がターンにつき 1 回計算した `agentic_search.tool_availability()` の結果で、ここでは計算しない。
    """
    sp = (scope_meta or {}).get("scope_paths") or None
    layer = layer_mod.effective_layer(scope_meta, lens)  # 非適用レンズは常に both（`layer.effective_layer` が判定）
    profile = (scope_meta or {}).get("depth_profile")
    sys_settings = system_settings
    eff, blocked = agentic_search.dispatch_tools_for_lens(
        lens, (scope_meta or {}).get("tools"), availability=tools_availability)

    def _qa_fallback_env():
        """grep（無ければ ES）だけで下地を組む qa 相当の縮退。グラフ不調で事前検索が実行できないときも、本体・清書がソースを直接調べる下地を渡す。"""
        base_hits = depth_profile_mod.effective_base(sys_settings, "qa_max_hits", QA_MAX_HITS_DEFAULT)
        max_hits = depth_profile_mod.scaled_ratio(base_hits, profile, abs_max=agentic_search.MAX_HITS_ABS_MAX)
        if eff["grep"]:
            qa_result = run_qa(payload, world, scope_paths=sp, layer=layer, max_hits=max_hits)
            if eff["fulltext"]:
                qa_result = _merge_qa_with_es(qa_result, world, payload, sp, layer=layer)
        elif eff["fulltext"]:
            es_cites = _es_citations(world, payload, sp, layer=layer)
            qa_result = {"type": "qa", "question": payload, "answered": bool(es_cites), "citations": es_cites}
        else:  # 資料を探す手段が 1 つも無い（グラフ縮退時のみ起こりうる）
            qa_result = {"type": "qa", "question": payload, "answered": False, "citations": []}
        return _answer_qa(qa_result, world)

    if blocked and lens in agentic_search._DISPATCH_REQUIRES_GRAPH and (eff["grep"] or eff["fulltext"]):
        # グラフ必須レンズでグラフだけが不達／OFF のときは、明示エラーで終わらせず grep 相当の下地へ縮退する。縮退の印が無いと Codex 経路で告知だけが消える。
        env = _qa_fallback_env()
        # 不達（未構築・接続断）由来なら統計にも残す（`graph_unavailable`）。利用者が自分で OFF にした場合は障害ではないので通知だけ（`blocked`）。
        env["graph_degraded"] = (
            "graph_unavailable"
            if ((tools_availability or {}).get("graph") is False
                and tools_pref_mod.normalize_tools_pref((scope_meta or {}).get("tools"))["graph"])
            else "blocked")
    elif blocked:
        env = agentic_search.tools_blocked_env(lens)
    elif lens in ("impact", "troubleshoot"):
        # グラフ不調（世代不一致・接続断）は事前検索を落とさず、grep 相当の下地＋縮退の印で調査を続けさせる。縮退してよいのは回復可能な障害（`DriverError` と `TransientError`）だけで、`ConfigurationError`・`ClientError` は握り潰さず送出する（`lens_service.neighbor_cards`・`agentic_search._is_recoverable_tool_exception` と同じ分類）。
        from neo4j.exceptions import ConfigurationError, DriverError, TransientError
        try:
            if lens == "impact":
                base_depth = depth_profile_mod.effective_base(sys_settings, "impact_depth", IMPACT_MAX_DEPTH)
                depth = depth_profile_mod.scaled_depth(base_depth, profile, abs_max=IMPACT_MAX_DEPTH_ABS_MAX)
                result = run_impact(session, payload, world, scope_prefixes=sp, depth=depth)  # 範囲は Cypher で絞る
                # 構造的な影響も推定も 0 件のとき、グラフ未構築なら「影響なし」と誤読させず、grep 相当の下地＋縮退の印へ切り替える。ヒットが 1 件でもあれば追加クエリを発行しない。
                if not result["items"] and not result.get("presumed") and world_graph_is_empty(session, world):
                    env = _graph_empty_env(lens, eff, _qa_fallback_env)
                else:
                    env = _answer_impact(result, world)
            else:
                base_depth = depth_profile_mod.effective_base(
                    sys_settings, "troubleshoot_depth", TROUBLESHOOT_GRAPH_DEPTH)
                depth = depth_profile_mod.scaled_depth(base_depth, profile, abs_max=TROUBLESHOOT_GRAPH_DEPTH_ABS_MAX)
                res = run_troubleshoot(session, payload, world, depth=depth, scope_paths=sp)
                res = _merge_troubleshoot_with_es(res, world, payload, sp) if eff["fulltext"] else res
                if not res.get("candidates") and world_graph_is_empty(session, world):
                    env = _graph_empty_env(lens, eff, _qa_fallback_env)
                else:
                    env = _answer_troubleshoot(res, world)
        except (GraphSchemaEraError, DriverError, TransientError) as e:
            if isinstance(e, ConfigurationError):
                raise  # 非一時的な設定不備＝縮退しない
            _degraded = (agentic_search.GRAPH_REINGEST_ERROR_CODE if isinstance(e, GraphSchemaEraError)
                         else "graph_unavailable")
            _log.warning("事前検索がグラフ不調で縮退（lens=%s・world=%s・%s）: %s",
                        lens, world, _degraded, type(e).__name__)
            if not (eff["grep"] or eff["fulltext"]):
                # 資料を探す手段が 1 つも残っていない＝一度も検索できていない。完了扱いにせず入口ゲートと同じ明示エラーで終える（`graph_degraded` も付けない）。
                env = agentic_search.tools_blocked_env(lens)
            else:
                env = _qa_fallback_env()
                # 縮退の事実（閉じたコード・本文なし）。`providers/base.py::_gather` 経由の各 provider が通知文言・統計へ反映する。
                env["graph_degraded"] = _degraded
    else:  # qa: grep＋ES を統合（Codex/heuristic/非 agentic も ES 参照）
        env = _qa_fallback_env()
    # 参照中の範囲（D/監査）＋このレンズで層フィルタが実効したか（UI が非適用の注記を出すための 1 項目）。
    env["scope"] = layer_mod.scope_with_layer(scope_meta, world=world, lens=lens)
    return env


# 出典 0 件時の案内: 「絞られている軸だけ」を範囲→探す対象の順で 1 つの案内にまとめる（既に最も緩い設定の軸は含めない）。
_NO_RESULTS_EVEN_AT_LOOSEST_HEADLINE = "範囲・種類を変えても見つかりませんでした（確証なし）。"


def _no_genuine_results(env: dict) -> bool:
    """出典 0 件で、かつ通常の検索結果 envelope か。
    AI 未接続・busy・下調べ設定不正・下調べ失敗・層を強制できない構成・Neo4j 安全弁（timeout/緊急天井）などの honest failure は `data: {}`（空 dict）で返る。通常の検索結果（0 件含む）は `run_qa`/`run_impact`/`run_troubleshoot` の返り値を `data` に積む非空 dict になる。
    「出典 0 件」だけを見ると明示エラーにも再検索案内が付くため、`data` が空でないことも確認する。
    """
    return not env.get("sources") and bool(env.get("data"))


_DEPTH_PROFILE_LABEL = {"quick": "クイック", "standard": "標準", "deep": "深く"}  # "max" は既に最も緩いため案内対象外


def _depth_actually_helps(env: dict) -> bool:
    """深さ（evaluator の巡数）が実際に効く構成かどうか。真になるのは次のいずれか:
    - `evidence_packet.task_id` が `"sub:{profile_id}"`／`"plan:..."`（下調べ役経由で、巡ループが実際に走る）。
    - Codex 構成で `env["codex_multi_agent"]` が真（`providers/codex/provider.py` が `codex_multi_agent_enabled`（sandbox.py）の判定を env へ渡す）。独自エンドポイント（custom）・サンドボックス無効の構成は偽で案内対象外。
    `"main"`（worker を付けない頭脳）は偽（深さを変えても巡数が発生しない）。
    """
    task_id = ((env.get("data") or {}).get("evidence_packet") or {}).get("task_id") or ""
    if task_id.startswith("sub:") or task_id.startswith("plan:"):
        return True
    usage = env.get("usage") or {}
    return usage.get("provider") == "codex" and bool(env.get("codex_multi_agent"))


def _retry_hints(env: dict, message: str = "") -> list:
    """`env["sources"]`（共有 KB 出典）が 0 件かつ範囲/探す対象/調べる深さが絞られているときの再検索案内。呼び出し前に `_no_genuine_results(env)` を確認する（`_finalize`）。表示順は範囲→探す対象→調べる深さ。
    - 層（探す対象）は `layer_applied`（このレンズで層フィルタが実効したか）が真のときだけ含める（impact/troubleshoot は層を適用しない）。
    - 調べる深さは「最大」でなく、かつ深さが実際に効く構成（`_depth_actually_helps`）のときだけ含める。標準/深くから直接「最大」へ 1 回で広げる。
    - `message`（元の質問文）: クイック（`depth == "quick"`）かつ `investigation_state.coverage_requested(message)` が真のときだけ、深さ hint を「『標準』以上で調べ直すと抜けが減ります」に差し替え、行き先も「標準」にする。
    """
    sm = env.get("scope") or {}
    hints = []
    if sm.get("scope_paths"):  # 範囲が絞られている（既に全体なら空リスト）
        hints.append({"kind": "scope", "label": "範囲を全体に広げる", "action": {"scope_paths": []}})
    layer = sm.get("layer")
    if sm.get("layer_applied") and layer in ("docs", "code"):  # 探す対象が限定・かつこのレンズで実効
        label = "コードも含めて探す（今は資料のみ）" if layer == "docs" else "資料も含めて探す（今はコードのみ）"
        hints.append({"kind": "layer", "label": label, "action": {"layer": "both"}})
    depth = sm.get("depth_profile")
    if depth in _DEPTH_PROFILE_LABEL and _depth_actually_helps(env):  # "max" と深さが効かない構成は対象外
        if depth == "quick" and investigation_state_mod.coverage_requested(message):
            hints.append({"kind": "depth", "label": "網羅性を求める質問です。『標準』以上で調べ直すと抜けが減ります",
                          "action": {"depth_profile": "standard"}})
        else:
            hints.append({"kind": "depth", "label": f"調べる深さを上げて探す（今は{_DEPTH_PROFILE_LABEL[depth]}）",
                          "action": {"depth_profile": "max"}})
    tools = sm.get("tools")  # 検索経路トグルが非既定のときだけ
    if tools and not tools_pref_mod.is_default(tools):
        hints.append({"kind": "tools", "label": "OFF にした検索を戻す",
                      "action": {"tools": dict(tools_pref_mod.DEFAULT_TOOLS_PREF)}})
    return hints


def _is_budget_exhausted(env: dict) -> bool:
    """`providers/base.py::_agentic_run` が調査予算到達（turns_exhausted/budget_exceeded/tools_per_turn_exceeded）で既に固定 headline を据えているターンか。
    予算切れは「恒久的に見つからない」とは別の状態のため、真なら `_finalize` の「見つからない」断定で headline を上書きしない。
    `task_id == "main"`（`stop_kind_mod.is_main_task` と共有する述語）に限定する。ハイブリッド（`"sub:{profile_id}"`）は provider 側のガードが固定 headline を据えないため、従来どおり 0 件時の断定文言を適用する。
    """
    packet = (env.get("data") or {}).get("evidence_packet") or {}
    return (stop_kind_mod.is_main_task(packet)
           and packet.get("stop_reason") in agentic_search._BUDGET_EXHAUSTED_STOP_REASONS)


def _is_codex_stopped_early(env: dict) -> bool:
    """Codex CLI が正常終了したのに、自動継続を尽くしてもなお agent_message が作業宣言だけで終わったターンか（`providers/codex/provider.py::_run_authoring` が立てる `env["codex_stopped_early"]`）。
    `_is_budget_exhausted` と同型のガードで、`evidence_packet.stop_reason` の語彙は流用せず独立したフラグにする。真なら `_finalize` の断定文言で headline を上書きしない。
    """
    return bool(env.get("codex_stopped_early"))


# グラフ縮退（`_dispatch` の事前検索・Codex 本体の世代不一致検知）の閉じたコード → `answer.limits` の bool 項目（利用統計「打ち切りの内訳」）。
_GRAPH_DEGRADED_LIMIT_FIELD = {
    agentic_search.GRAPH_REINGEST_ERROR_CODE: "graph_reingest_required",
    "graph_unavailable": "backend_unavailable_graph",
}


def _apply_graph_degraded(env: dict) -> None:
    """`env["graph_degraded"]`（閉じたコード・本文なし）を利用者向けの冒頭告知と統計フラグへ変換する。
    グラフを使わず grep／原本直読で調べ切ったターンは固定文言で終端していないため、回答本体は残し、冒頭に「なぜグラフを使っていないか」の 1 文だけを足す。反復ツール検索（`providers/base.py::_agentic_run`）は配信時に自分で前置するため、このキーを載せない。
    """
    code = env.pop("graph_degraded", None)
    notice = agentic_search.graph_degraded_notice(code)
    if not notice:
        return
    head = (env.get("headline") or "").strip()
    env["headline"] = f"{notice}\n\n{head}" if head else notice
    field = _GRAPH_DEGRADED_LIMIT_FIELD.get(code)
    if field:
        env["limits"] = {**(env.get("limits") or {}), field: True}


def _finalize(env, decision, message: str = ""):
    # `env["data"]["claims"]` はここでは触らない（`data` はそのまま永続化され、共有時の再構築は `shares.py::_safe_claim` が行う）。`message`（省略可）は `_retry_hints` のクイック＋網羅要求の分岐に渡す。
    env["lens"] = decision["lens"]
    env["route"] = {"lens": decision["lens"], "reason": decision["reason"],
                    "path": _ROUTE_PATH.get(decision["lens"], [])}
    # 終了理由を閉じた語彙（8 値）へ正規化して `messages.answer.stop_kind` に残す（`stop_kind_mod.resolve`）。`resolve` が None（busy／型を特定できない honest failure）のときは立てず、集計側の `unknown` に落とす。
    _stop_kind = stop_kind_mod.resolve(env)
    if _stop_kind is not None:
        env["stop_kind"] = _stop_kind
    _codex_stopped_early = _is_codex_stopped_early(env)
    # 網羅性を求める質問をクイックで実行したときは、結果が 0 件でなくても深さの案内を出す（列挙の抜けは結果があるときにこそ起きる）。
    if (not _no_genuine_results(env) and _depth_actually_helps(env)
            and (env.get("scope") or {}).get("depth_profile") == "quick"
            and investigation_state_mod.coverage_requested(message)):
        env["retry_hints"] = [{"kind": "depth",
                               "label": "網羅性を求める質問です。『標準』以上で調べ直すと抜けが減ります",
                               "action": {"depth_profile": "standard"}}]
    if _no_genuine_results(env):
        hints = _retry_hints(env, message)
        if hints:
            env["retry_hints"] = hints
        elif (decision["lens"] in ("qa", "author") and not _is_budget_exhausted(env)
              and not _codex_stopped_early):
            # 全軸が既に最も緩い設定（全体・資料＋コード・最大）でなお 0 件のとき、これ以上緩める軸が無いため案内を出す。予算到達・Codex の作業宣言止まりの途中結果、および impact/troubleshoot（headline が十分具体的）は上書きしない。
            env["headline"] = _NO_RESULTS_EVEN_AT_LOOSEST_HEADLINE
    # 縮退の告知は 0 件案内（headline の全置換）より後で前置する（先に付けると置換で消える）。
    _apply_graph_degraded(env)
    if _codex_stopped_early:
        # 「続きから調べ直せる」案内は、出典 0 件時の案内と同じボタン機構（retry_hints・data-retry-kind）に載せ、0 件案内とは独立に常に追加する。クリック時は kind="resume" 専用分岐（`web/chat.js`）が固定文言を送り、`codex_session_id` の継続に委ねる。
        env.setdefault("retry_hints", []).append(
            {"kind": "resume", "label": "続きを調べる", "action": {"message": "続きを調べて"}})
    return env


def _finalize_activity_phases(answer: dict, duration_ms: int) -> None:
    """`answer["activity"]` の `app_version`／`phases_ms.total`／`phases_ms.post` を保存直前に埋める（全経路共通）。取れない値は 0 や仮の文字列で埋めず、キー自体を置かない。
    - provider が作らなかった経路（API／Ollama・Codex 未起動・clarify）は `{v:1, source:"none", phases_ms:{"total":duration_ms}}`（prepare/agent は 0 埋めしない）。
    - provider が作った経路は `phases_ms` の prepare/agent を保ち、`post` は両方が数値（bool を除く）のときだけ `total - prepare - agent` として足す。`app_version` も取れなければキーを置かない。
    """
    version = app_version.current()
    activity = answer.get("activity")
    if not isinstance(activity, dict):
        activity = {"v": 1, "source": "none", "phases_ms": {"total": duration_ms}}
        if version is not None:
            activity["app_version"] = version
        answer["activity"] = activity
        return

    if version is not None:
        activity["app_version"] = version
    phases = activity.get("phases_ms")
    if not isinstance(phases, dict):
        phases = {}
    phases["total"] = duration_ms
    prepare = phases.get("prepare")
    agent = phases.get("agent")
    if (isinstance(prepare, (int, float)) and not isinstance(prepare, bool)
            and isinstance(agent, (int, float)) and not isinstance(agent, bool)):
        phases["post"] = max(0, duration_ms - prepare - agent)
    activity["phases_ms"] = phases
    answer["activity"] = activity


def _pop_evidence_committed(env: dict, trace_nodes: dict):
    """`env["_evidence_committed"]`（provider が `_result` へ同梱したサイドカー・`providers/base.py::_evidence_committed_node`）を取り出し、`trace_nodes` へ折り込む。
    `_result` を処理（永続化）するのと同じ呼び出しの中でしか呼ばない（独立イベントにすると停止のタイミング次第で孤児になる）。公開 `answer` には残さず、無ければ `None`。
    """
    node = env.pop("_evidence_committed", None)
    if node is not None and node.get("id"):
        trace_nodes[node["id"]] = node
    return node


def _mark_investigation_recorded(env: dict, investigation_record: dict | None) -> None:
    """`investigation_record`（provider が `_result` の別項目として渡した台帳・`None`＝台帳ゲートが走らなかったターン）があれば、`env["investigation"]` へ `recorded` の印を立てる。
    assistant message を `store.add_message` で保存する前に呼ぶこと（この印は `messages.answer` と一緒に永続化される・保存を試みる印であって保存成否ではない）。
    """
    if investigation_record is not None and isinstance(env.get("investigation"), dict):
        env["investigation"]["recorded"] = True


def _save_investigation_record(investigation_record: dict | None, message_id, conversation_id) -> None:
    """`investigation_record` を `investigation_records` 表へ保存する。assistant message を保存した直後（`message_id` が実在する状態）に呼ぶこと（`message_id` は `messages(id)` への FK）。
    保存に失敗しても fail-open（警告ログのみ）で、先に立てた `recorded` 印は訂正しない（回答の保存を失敗させない契約を優先）。`None` は何もしない。
    """
    if investigation_record is None:
        return
    try:
        store_investigation.save_investigation_record(
            message_id, conversation_id,
            complete=bool(investigation_record.get("complete")),
            manifest=investigation_record.get("manifest"),
            items=investigation_record.get("items") or {},
            coverage=investigation_record.get("coverage") or {},
            reviews=investigation_record.get("reviews") or [])
    except Exception as e:
        _log.warning("investigation record save failed (fail-open): %s", e)


# 影響分析の Neo4j 安全弁（timeout＋緊急天井）に当たったときは fail-loud（偽陰性防止）: `_dispatch` の impact 分岐が `GraphQueryOverloadError` を送出したら、LLM 合成を経由させず固定文言の `_result` に差し替える。この例外は impact 以外では発生しない。
def _impact_overload_result(message: str, world: str, scope_meta: dict | None) -> dict:
    """固定文言のエンベロープ（LLM 合成なし）。空/部分結果を LLM に渡すと「確実な波及は無い」という偽陰性の文言を生成しうるため、ここで完結させる。正常系と同じ `_result` 形で返す。"""
    return _fixed_lens_result("impact", GRAPH_OVERLOAD_USER_MESSAGE, "Neo4j 安全弁（timeout/緊急天井）",
                              message, world, scope_meta)


# `GraphSchemaEraError` も同じ理由で LLM 合成を経由させず固定文言で終端する。発生元は impact に限らない（troubleshoot の `lens_service.neo4j_related`・agentic `graph_neighbors` 経由もある）ため、`GraphSchemaEraError.lens` でエンベロープの lens を決める（不明は "impact"）。
def _fixed_lens_result(lens: str, headline: str, reason: str, message: str, world: str,
                       scope_meta: dict | None) -> dict:
    """固定文言のエンベロープ（LLM 合成なし・lens 汎化版）。`_impact_overload_result`/`_degrade_overload` が共有する。"""
    sm = layer_mod.scope_with_layer(scope_meta, world=world, lens=lens)
    env = {"lens": lens, "headline": headline, "summary": {"total": 0},
           "data": {}, "sources": [], "scope": sm,
           "agentic_failure": "error",  # 固定文言の縮退＝終了理由の分布で完了扱いにしない
           "route": {"lens": lens, "reason": reason, "path": _ROUTE_PATH.get(lens, [])}}
    decision = {"lens": lens, "input": message, "reason": reason}
    return {"env": env, "decision": decision}


def _degrade_overload(gen, message: str, world: str, scope_meta: dict | None):
    """provider.run() のイテレーション中に `GraphQueryOverloadError`／`GraphSchemaEraError` が飛んだら、固定文言の `_result` イベントへ差し替えて終端する（stream_message のラッパー）。
    `GraphSchemaEraError` は調査結果が届いた後の保険で、通常は検知時点で調査を止めない（`_dispatch` は grep 相当の下地へ縮退し、`agentic_search.run_tool` の `graph_neighbors` 分岐と Codex の MCP 経路は機械可読コードのツール結果へ変換する）。
    例外は `providers/base.py::_gather` 内の `ctx.dispatch(...)` から上がり、`_gather` の呼び出し側はラップしていないため、捕まえた時点で LLM 事実合成は一度も実行されていない。
    """
    try:
        yield from gen
    except GraphQueryOverloadError as e:
        _log.warning("impact 経路が Neo4j 安全弁で縮退（fail-loud・reason=%s・world=%s）", e.reason, world)
        result = _impact_overload_result(message, world, scope_meta)
        yield {"type": "_result", **result}
    except GraphSchemaEraError as e:
        lens = e.lens or "impact"
        _log.warning("グラフ読み取りがスキーマ世代不一致で縮退（fail-loud・lens=%s・world=%s・stored=%s）",
                    lens, world, e.stored_era)
        result = _fixed_lens_result(
            lens, GRAPH_SCHEMA_ERA_USER_MESSAGE, "グラフのスキーマ世代不一致（今すぐ更新で解消）",
            message, world, scope_meta)
        yield {"type": "_result", **result}


def _clip_history_msg(text: str) -> str:
    """履歴の 1 メッセージを上限文字数で切り詰める（先頭を残し末尾を落とす）。"""
    t = text or ""
    if len(t) <= _HISTORY_MSG_CHARS:
        return t
    return t[:_HISTORY_MSG_CHARS] + "…（省略）"


def _history_pairs(conversation_id) -> list[dict]:
    """直近ターンの (user, assistant) 完全対を `Ctx.history` 形式で返す。
    会話は交互とは限らない（途中停止・clarify・crash 補填）ため、user 行の直後（id 順）が assistant 行のときだけ対として採用し、不対行は捨てる。
    二重キャップ: 直近 `_HISTORY_TURNS` 対＋`_HISTORY_CHAR_BUDGET`（新しい対から積み、超える対は捨てる）。メッセージ単体も `_HISTORY_MSG_CHARS` で切る。
    呼び出しは `store.add_message(現在の user)` より前（in-flight の質問を履歴に含めない）。conversation_id が None・`_HISTORY_TURNS <= 0`・読み取り失敗は `[]`（fail-open）。
    """
    if conversation_id is None or _HISTORY_TURNS <= 0:
        return []
    try:
        limit = min(512, _HISTORY_TURNS * 2 + 8)
        while True:
            rows = store.recent_messages(conversation_id, limit=limit)
            pairs = []
            i = 0
            while i < len(rows) - 1:
                if rows[i]["role"] == "user" and rows[i + 1]["role"] == "assistant":
                    pairs.append((rows[i], rows[i + 1]))
                    i += 2
                else:
                    i += 1
            # 不対行が堆積すると古い完全対が窓の外に押し出されるため、取得行数が limit に張り付いている間は窓を段階的に広げて再取得する。512 行で打ち切る（best-effort・全履歴走査はしない）。
            if len(pairs) >= _HISTORY_TURNS or len(rows) < limit or limit >= 512:
                break
            limit = min(512, limit * 4)
        pairs = pairs[-_HISTORY_TURNS:]  # 直近 N 対（対数キャップ）
        kept = []
        budget = _HISTORY_CHAR_BUDGET
        for u, a in reversed(pairs):  # 新しい対から積む
            u_txt, a_txt = _clip_history_msg(u["content"]), _clip_history_msg(a["content"])
            cost = len(u_txt) + len(a_txt)
            if cost > budget:  # 文字予算超過＝この対（とこれより古い対）は捨てる
                break
            budget -= cost
            kept.append((u_txt, a_txt))
        out: list[dict] = []
        for u_txt, a_txt in reversed(kept):  # 時系列順（古→新）に戻す
            out.append({"role": "user", "content": u_txt})
            out.append({"role": "assistant", "content": a_txt})
        return out
    except Exception as e:
        _log.warning("history priming failed (degrade to no-history, turn continues): %s", e)
        return []


def _ensure_conversation(conversation_id, message, world, user_id):
    if conversation_id is None:
        conv = store.create_conversation(user_id=user_id, world=world,
                                         title=(message or "").strip()[:40] or "新しい会話")
        return conv["id"]
    return conversation_id


# 個人ファイル参照の許可拡張子（workspace upload 許可と同じ集合・`api.py` の `_WORKSPACE_SEARCHABLE_EXT` と同義）。`api.py` に依存しないためここで重複定義する。個人領域は共有 KB のアナライザ登録簿とは別の独立集合で、コード分は和集合に含める。
_PERSONAL_SEARCHABLE_EXT = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".sql", ".py", ".sh", ".bat",
} | _analyzer_registry.registered_extensions()


def _personal_grep_hits(user_id: str, query: str, users_dir: str) -> list[dict]:
    """ユーザーの個人 workspace を台帳基準で grep し、ヒット一覧を返す。
    - 検索は `personal_workspace_files` 台帳上の status='uploaded' ファイルのみ（FS 残骸を拒否）。
    - base は users_dir / uid / workspace / files に閉じ込める（symlink・パストラバーサル拒否）。
    - ES/Neo4j・共有 KB には触れない。他ユーザーの uid は引数で分離する。
    - 秘匿名（`text_kind.is_sensitive_doc_id`）のファイルは読まない。
    """
    if not query or not query.strip():
        return []
    q = query.strip()
    q_lower = q.lower()
    files_dir = (Path(users_dir).resolve() / user_id / "workspace" / "files")
    # files/ ディレクトリ自体が symlink の場合は拒否（confinement 破壊防止）。
    if files_dir.is_symlink() or not files_dir.is_dir():
        return []
    live_paths = store.live_workspace_rel_paths(user_id)
    hits: list[dict] = []
    seen: set[tuple] = set()
    files_dir_resolved = files_dir.resolve()
    for rel_path in sorted(live_paths):
        raw = files_dir / rel_path
        # symlink 脱出防止（pre-resolve チェック）。
        if raw.is_symlink():
            continue
        target = (files_dir / rel_path).resolve()
        try:
            target.relative_to(files_dir_resolved)
        except ValueError:
            continue
        if not target.is_file():
            continue
        if target.suffix.lower() not in _PERSONAL_SEARCHABLE_EXT:
            continue
        # 秘匿名のファイルは本文を推論へ渡さない（共有 KB と同じ判定）。
        if text_kind.is_sensitive_doc_id(rel_path):
            continue
        try:
            raw_bytes = target.read_bytes()
            enc = text_encoding.detect_bytes(raw_bytes, complete=True)
            lines = text_encoding.decode(raw_bytes, enc).splitlines()
        except Exception:
            continue
        for i, ln in enumerate(lines):
            if q_lower not in ln.lower():
                continue
            s = max(0, i - 1)
            e = min(len(lines), i + 3)
            key = (rel_path, s, e)
            if key in seen:
                continue
            seen.add(key)
            hits.append({
                "rel_path": rel_path,
                "line": i + 1,
                "text": _redact("\n".join(lines[s:e]).strip()),
                "match": q,
                "source": "個人ファイル内ヒット",
            })
            if len(hits) >= 20:
                break
        if len(hits) >= 20:
            break
    return hits


def _personal_facts(hits: list[dict], query: str) -> str:
    """個人ファイルのヒットを LLM への事実テキストに整形する。このテキストは AI への入力のみで、ES/Neo4j には書かない。"""
    if not hits:
        return ""
    parts = []
    for h in hits[:8]:
        parts.append(f"[個人ファイル: {h['rel_path']} 行{h['line']}] {h['text'][:200]}")
    return "\n【個人ファイル内ヒット（本人のみ参照可・共有不可）】\n" + "\n".join(parts)


def _personal_citations(hits: list[dict]) -> list[dict]:
    """個人ファイルのヒットを citation 形式に変換する。`source` フィールドで共有 KB citation と区別し、DL リンクは付けない（個人 workspace 専用）。"""
    seen_rel: set[str] = set()
    cites: list[dict] = []
    for h in hits:
        rel = h["rel_path"]
        if rel not in seen_rel:
            seen_rel.add(rel)
            cites.append({
                "doc_id": rel,
                "quote": h["text"][:80],
                "source": "個人ファイル内ヒット",
            })
    return cites


def _save_clarify_message(conversation_id, user_id, settings, message, trace_nodes, ev,
                          user_msg_id, personal, world, scope_meta, t0) -> dict:
    """確認カード（provider が yield する `question` イベント）を assistant メッセージとして永続化する（`_result` に至らず generator を終える経路の保存）。ページを離れて開き直しても後から答えられる。
    content=prompt／answer に question payload／trace はここまでに溜めた思考ノード。
    personal トグル ON のターンは、質問 prompt に個人ヒットの断片が混ざり得るため個人扱いで保存する（会話フラグ `set_contains_personal_workspace`／`_user_msg` の personal 保存は呼び出し側がターン先頭で済ませている）。
    """
    # 巡ループが全巡で累積した個人由来／書込フラグ。確認カードも個人扱いで保存する。内部キーを question payload・配信イベントへ残さないよう、`ev` から一度だけ取り除く。
    if ev.pop("_personal_rounds", False):
        personal = True
    # 会話が既に個人由来（過去ターンに個人行がある）なら、このターンが personal=False でも確認カードと質問行を個人扱いにする（履歴/resume 経由で個人内容が混ざり得るため）。追加読取に失敗したら個人扱いへ倒す（fail-closed）。
    if not personal:
        try:
            personal = bool(store.conversation_is_personal_tainted(conversation_id))
        except Exception:
            personal = True
    if personal:
        store.set_contains_personal_workspace(conversation_id)
        store.set_message_personal(user_msg_id)
    question_payload = {k: v for k, v in ev.items() if k not in ("type", "activity")}
    question_payload.setdefault("original_message", message)  # 再送フォーマット（元の依頼）に使う
    q_answer = {"lens": "clarify", "question": question_payload, "trace_version": 2}
    # provider が question イベント経由で運んだ activity（`answer["activity"]` と同じ置き場）。`_finalize_activity_phases` が「provider が作った」経路として扱う（無ければ source:"none" で作り直す）。
    if ev.get("activity") is not None:
        q_answer["activity"] = ev["activity"]
    q_answer["duration_ms"] = round((time.monotonic() - t0) * 1000)
    _finalize_activity_phases(q_answer, q_answer["duration_ms"])
    q_msg = store.add_message(conversation_id, "assistant", ev.get("prompt") or "",
                              lens="clarify",
                              answer=q_answer,
                              trace=_cap_trace_v2(trace_nodes),
                              personal=personal)
    # 監査は lens="clarify"。assistant_msg_id は保存した確認カードの message id。
    _audit_chat_turn(user_id, conversation_id, settings, lens="clarify",
                     user_msg_id=user_msg_id, assistant_msg_id=q_msg["id"], world=world,
                     scope_paths=(scope_meta or {}).get("scope_paths"), personal=personal)
    return q_msg


def _make_dispatch_with_personal(session, world, scope_meta, sys_settings, tools_availability, personal_hits):
    """共有 KB dispatch の結果に個人ヒットを注入する dispatch を返す。"""
    def _dispatch_with_personal(lens, inp):
        env = _dispatch(session, lens, inp, world, scope_meta, sys_settings, tools_availability)
        if personal_hits:
            # 個人ヒットを facts に追記（AI への入力のみ・非永続化）。
            env["_personal_facts"] = _personal_facts(personal_hits, inp)
            # 個人 citation を別枠で追加（共有 KB citation とは分離）。
            env.setdefault("personal_sources", []).extend(_personal_citations(personal_hits))
        return env
    return _dispatch_with_personal


def stream_message(session, message, world="v1",
                   conversation_id=None, user_id="admin", scope_paths=None, layer=None, lens=None,
                   knowledge=False, personal=False, users_dir="data/users", stop_event=None,
                   on_user_saved=None, web_search=False, depth_profile=None, tools=None,
                   tools_explicit=None, tools_availability=None, provider=None, settings=None,
                   sys_settings=None):
    """思考イベントを逐次 yield する（SSE）。頭脳は provider（差し替え可能）で、UI/プロトコルは不変。
    provider が `node`（動的に何個でも）を流し、最後に内部 `_result` を返す。本関数は会話の用意・永続だけを担い、`_result` を `answer` イベントに変換して返す。
    - `knowledge=False`（既定）: 検索せず素の会話（資料参照オフ）。`True` で社内資料を参照する（レンズ＋出典）。
    - `scope_paths`: 検索/分析をその範囲に絞る（knowledge 時のみ）。`layer`（既定 `None`＝`"both"`）: 探す対象。`lens`（既定 `None`＝自動）: 調べ方の明示指定で、メッセージ先頭のスラッシュ接頭辞が優先する（`_resolve_lens`）。
    - `personal=True`: 共有 KB に加え本人の個人ファイルも grep して事実+引用に含める。個人ファイルは ES/Neo4j に入れず、本人のみ参照可。
    - `web_search=False`（既定）: このチャットで Codex の Web 検索を希望するか。保存済みの個人設定 `codex_web_search` は実行には使わず、この値で上書きしてから provider を選ぶ。
    - `depth_profile`（既定 `None`＝`"standard"`）: 調べる深さ。`_dispatch()`/agentic 探索の反復・ヒット上限・探索深さ・Codex 推論に倍率で効く（evaluator の巡数は `depth_profile.review_rounds_for`）。
    - `tools`（既定 `None`＝全 ON）: 検索経路トグル。agentic 探索が提示する grep/es_search/graph_neighbors を絞る（Codex は対象外）。
    - `tools_availability`（既定 `None`＝自分で計算）: 呼び出し元（`routers/chat.py`）が受付時の 422 判定と同時に計算した可用性 snapshot。渡されたらそれを使う（受付時と実行時で可用性が食い違わないように）。
    - `provider`/`settings`/`sys_settings`（既定 `None`＝自分で用意）: 呼び出し元が受付段階で組み立てた同一の Provider／ユーザ設定／システム設定を実行本体まで渡す。別々に読み直すと、受付時の接続先検証と実行時の provider が別世代の設定を使い得る。
    - `stop_event`: セットされたら頭脳の停止の終端を待つ。終端が返ればその assistant メッセージと所要時間を通常の完了と同じく保存・配信し、終端が無いまま終われば `{"type":"stopped"}` を返して assistant は保存しない（user メッセージは冒頭で保存済みで、次の質問はそのまま会話を続けられる）。
    - `on_user_saved(message_id, personal)`（省略可）: このターンの user 行を保存した直後に同期で 1 回呼ぶ。呼び出し元が「どの user 行が自分のターンか」を本文一致で推測せずに済む（同文の並走ターンを取り違えない・`routers/chat.py::_persist_turn_crash`）。yield イベントとは別チャネル。
    """
    _t0 = time.monotonic()  # 1 ターンの所要時間の起点（answer.duration_ms へ埋め込む）。
    explicit_lens, lens_source, lens_block, message = _resolve_lens(lens, message)
    conversation_id = _ensure_conversation(conversation_id, message, world, user_id)
    # 履歴は現在の質問を保存する前に取得する（in-flight の質問を履歴に含めない）。
    history = _history_pairs(conversation_id)
    # Codex ネイティブ resume: 直近ターンで捕捉済みの codex_session_id があれば CodexProvider に渡す（他 provider は無視）。history と同じく質問保存より前に読む。
    codex_session_id = store.get_session_id(conversation_id)
    # 直近 assistant メッセージが記録した Codex 累計 usage（resume ターンの usage をターン差分にするための前ターン値・CodexProvider だけが消費する）。
    codex_usage_prev_total = store.get_codex_usage_total(conversation_id)
    # トグル ON のターンは、保存時点で質問を個人扱いにし、provider 実行前に会話も個人扱いにする（in-flight で共有されても質問が漏れない／clarify で `_result` に至らなくても未マークにならない）。
    _user_msg = store.add_message(conversation_id, "user", message, personal=personal)
    if on_user_saved is not None:
        on_user_saved(_user_msg["id"], personal)
    if personal:
        store.set_contains_personal_workspace(conversation_id)
    # フロントの階層描画（サブエージェント レーン・集約表示）は trace_version=2 のターンにだけ適用する。`env["trace_version"]` は `_result` 到達時にしか分からないため、ストリーム先頭で 1 回だけ軽量なマーカーを流す。
    # `chat_turns.TurnBuffer` は先頭イベントを位置だけで保護するため、このマーカーは件数/バイト上限で間引かれない。未知の `type` を無視する既存フロントにも無害。
    # 会話作成・質問保存（副作用）の後に出す（クライアントへ何か送る前に会話/質問が確定している境界を変えない）。
    yield {"type": "trace_meta", "trace_version": 2}
    known = _known_terms(session, world) if knowledge else []  # オフ時は Neo4j も触らない
    scope_meta = (_resolve_scope(message, world, scope_paths, layer, lens_source, lens_block, web_search,
                                 depth_profile, tools, tools_explicit)
                 if knowledge else None)  # 明示 ＞ 全体
    # settings/sys_settings は呼び出し元が受付段階で読んだスナップショットを使う（省略時のみここで読む）。
    settings = settings if settings is not None else store.get_settings(user_id)
    # 実行に使う web_search は保存済み個人設定でなくこのチャットの希望のみ（ローカル複製だけを上書きし、DB へは書き戻さない）。
    settings = {**settings, "codex_web_search": bool(web_search)}
    # `get_provider` と同じ fresh snapshot を `_dispatch`（調べる深さ）へも共有する（決定的レンズと agentic 経路が別世代の system_settings を見ないように）。読み取り失敗は例外として伝播し、このターンを fail-closed にする。
    sys_settings = sys_settings if sys_settings is not None else store._read_system_settings_fresh()
    # 非 agentic 経路が使う実効ツール判定用の可用性スナップショット。
    if tools_availability is None:
        tools_availability = agentic_search.tool_availability() if knowledge else None

    # 個人ファイルを grep して事実テキスト/citation を準備する（ON かつファイルが存在する場合）。grep は本人 uid の workspace 配下のみで、ES/Neo4j には書かない。
    personal_hits: list[dict] = []
    if personal:
        personal_hits = _personal_grep_hits(user_id, message, users_dir)

    _dispatch_with_personal = _make_dispatch_with_personal(
        session, world, scope_meta, sys_settings, tools_availability, personal_hits)

    ctx = Ctx(
        message=message, world=world, pace=emit_pace(), knowledge=knowledge,
        route=_build_router(known, world, settings, can_ask=True, user_id=user_id,
                            explicit_lens=explicit_lens, scope_meta=scope_meta,
                            conversation_id=conversation_id),  # ストリーミング＝曖昧なら clarify で確認
        dispatch=_dispatch_with_personal if personal else
                 (lambda lens, inp: _dispatch(session, lens, inp, world, scope_meta, sys_settings,
                                              tools_availability)),
        scope_meta=scope_meta,
        make_sources=((lambda docs: _sources(docs, world)) if knowledge else None),
        uid=user_id,
        # agentic/plain 経路にも個人ヒットを伝搬する。
        personal_facts=_personal_facts(personal_hits, message) if personal_hits else "",
        stop_event=stop_event,
        # 直前ターンの (user, assistant) 対（message には混ぜない・別チャネル）。conversation_id/codex_session_id は CodexProvider の resume 判定に使う。
        history=history, conversation_id=conversation_id, codex_session_id=codex_session_id,
        codex_usage_prev_total=codex_usage_prev_total,
        # ターン先頭で 1 回だけ計算した可用性 snapshot を provider まで渡す。
        tools_availability=tools_availability,
        # activity.phases_ms.prepare の起点。provider 呼出し前の処理も prepare に含める。
        turn_started_mono=_t0,
    )
    # 「思考の流れ」を `messages.trace` に保存し、会話ロード時に右ペインへ静的復元する。node は id 単位で更新され得るため id で dedup し、最終状態のみ保持する。question は別カードで表示するため含めない。
    trace_nodes: dict = {}
    # 停止を検知したが停止終端（未完了回答）をまだ受け取っていない状態。
    _stopped_pending = False
    # 呼び出し元が既に組み立てた Provider があればそれを使う。
    _provider = provider if provider is not None else get_provider(settings, system_settings=sys_settings)
    for ev in _degrade_overload(_provider.run(ctx), message, world, scope_meta):
        if stop_event is not None and stop_event.is_set() and not _is_stopped_terminal(ev):
            # provider が停止要求を受けて何らかのイベント（`_result` 含む）を返してきても保存しない（assistant は永続しない）。例外は巡ループの停止終端（`_is_stopped_terminal`）で、未完了回答を保存する。停止終端は途中のイベントの後に続くため、捨てながら受け取り続け、終端が来なければループを抜けた後に停止監査・停止応答を 1 回だけ返す。
            _stopped_pending = True
            continue
        if ev["type"] == "_result":
            _stopped_pending = False
            env = _finalize(ev["env"], ev["decision"], message)
            # `_result` のサイドカーを trace へ折り込む（孤児イベント防止・永続化後にライブ配信もする）。
            _ev_committed_node = _pop_evidence_committed(env, trace_nodes)
            # 終端 4 種の印と巡の個人由来累積（内部キー・保存・共有へは残さない）。
            _terminal = env.pop("_terminal", None)
            _round_personal = _pop_round_personal(env)
            env["trace_version"] = 2
            # CodexProvider が捕捉/更新した session id を会話に永続化する（次ターンの resume 用）。fail-open（保存に失敗しても本ターンの回答は成立させる）。
            _codex_sid = env.get("codex_session_id")
            if _codex_sid:
                try:
                    store.set_session_id(conversation_id, _codex_sid)
                except Exception as e:
                    _log.warning("codex session id 保存に失敗（fail-open・次回は resume 不可で priming 継続）: %s", e)

            # 個人 citation を answer envelope に統合する。busy 応答には添付しない。
            _used_personal = False
            if personal_hits and not env.get("busy"):
                env["personal_sources"] = _personal_citations(personal_hits)
                _used_personal = True

            # Codex がファイルを書いた場合も contains_personal_workspace を立てる。
            if env.get("codex_wrote_files"):
                _used_personal = True
            # 巡ループの累積と書込を個人由来として扱う。
            if _round_personal or env.get("wrote_files"):
                _used_personal = True
            # 個人参照トグル ON のターンは hit が無くても質問にファイル名等が残り得るため個人扱い。
            if personal:
                _used_personal = True
            # 会話が既に個人由来なら今回 personal=False でも個人扱いにし続ける。
            if not _used_personal:
                try:
                    _used_personal = bool(store.conversation_is_personal_tainted(conversation_id))
                except Exception:
                    _used_personal = True  # 判定できなければ個人扱いへ倒す（fail-closed）

            # 個人コンテンツを使った場合は assistant message 保存の前にフラグを立てる。書き込みに失敗したら再 raise し（fail-closed）、個人内容を含む回答を保存しない。
            if _used_personal:
                store.set_contains_personal_workspace(conversation_id)
                store.set_message_personal(_user_msg["id"])  # sanitized share: このターンの質問も個人扱い

            env["duration_ms"] = round((time.monotonic() - _t0) * 1000)
            _finalize_activity_phases(env, env["duration_ms"])
            _investigation_record = ev.get("investigation_record")
            _mark_investigation_recorded(env, _investigation_record)
            msg = store.add_message(conversation_id, "assistant", env["headline"],
                                    lens=ev["decision"]["lens"], route=env["route"], answer=env,
                                    trace=_cap_trace_v2(trace_nodes),
                                    personal=_used_personal)
            _save_investigation_record(_investigation_record, msg["id"], conversation_id)
            # 停止終端は監査も停止として残す（assistant は保存済み）。
            _audit_chat_turn(user_id, conversation_id, settings,
                             lens=("stopped" if _terminal == "stopped" else ev["decision"]["lens"]),
                             user_msg_id=_user_msg["id"], assistant_msg_id=msg["id"], world=world,
                             scope_paths=(scope_meta or {}).get("scope_paths"), personal=_used_personal,
                             stopped=(_terminal == "stopped"))

            # 永続化（`store.add_message`）が成功した後にだけライブ配信する（`_result` と不可分なサイドカーとして、保存が確定してから画面に見せる）。
            if _ev_committed_node is not None:
                yield _ev_committed_node
            yield {"type": "answer", "conversation_id": conversation_id, "message": msg}
        elif ev["type"] == "question":
            # 確認カードを assistant メッセージとして永続化する。
            _save_clarify_message(conversation_id, user_id, settings, message, trace_nodes,
                                  ev, _user_msg["id"], personal, world, scope_meta, _t0)
            yield {**ev, "conversation_id": conversation_id, "original_message": message}
        else:
            if ev.get("type") == "node" and ev.get("id"):
                trace_nodes[ev["id"]] = ev
            yield ev
    if _stopped_pending:
        # 停止終端が来ないまま provider が終えた場合、assistant は永続せず、clarify と同格に監査へ残す（`message_id_assistant=None`・`stopped:true`）。
        _audit_chat_turn(user_id, conversation_id, settings, lens="stopped",
                         user_msg_id=_user_msg["id"], assistant_msg_id=None, world=world,
                         scope_paths=(scope_meta or {}).get("scope_paths"), personal=personal,
                         stopped=True)
        yield {"type": "stopped", "conversation_id": conversation_id}
