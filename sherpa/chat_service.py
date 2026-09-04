"""チャット・オーケストレーション（M8/M9・04-画面の原則.md §2/§3）。

会話メッセージ → ルーティング（chat_router）→ 既存レンズ（run_impact/run_troubleshoot/run_qa）
→ **答えエンベロープ**（見出し＝答え＋するべきこと／本体／**出典フッター＝原本DL**）→ 会話に永続（store）。

`stream_message` は同じ流れを**思考ステップとして逐次 yield**（SSE・右ペインの「思考の流れ」を駆動）。
レンズ振り分けはユーザに見せず、結果は「答え先頭」で返す。出典は必ず付け、原本DLリンクにする。
特定テーマの名前はコードに持たない（起点語/検索語は会話とデータから）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import quote

from . import agent_constructs, agentic_search, exec_event, intent_llm, scope, store, worlds
from . import depth_profile as depth_profile_mod
from . import layer as layer_mod
from . import tools_pref as tools_pref_mod
from .ingest import importance
from .agents import AGENT_PROVIDERS, Ctx, get_provider
from .chat_router import clarify_decision as _clarify_decision
from .chat_router import confirm_first_decision as _confirm_first_decision
from .chat_router import decision_for as _decision_for
from .chat_router import extract_slash_lens as _extract_slash_lens
from .chat_router import route as _heuristic_route
from .chat_router import wants_confirm_first as _wants_confirm_first
from .impact_service import IMPACT_MAX_DEPTH, IMPACT_MAX_DEPTH_ABS_MAX, run_impact
from .ingest.analyzers import registry as _analyzer_registry
from .ingest.world_neo4j import GRAPH_OVERLOAD_USER_MESSAGE, GraphQueryOverloadError
from .lens_service import (
    TROUBLESHOOT_GRAPH_DEPTH,
    TROUBLESHOOT_GRAPH_DEPTH_ABS_MAX,
    _run_capped,
    run_qa,
    run_troubleshoot,
)

# 調べる深さ（探索反復・grep/ES ヒット上限。SC-6c §3.2）: qa レンズの run_qa が直接 grep する経路
# 固有の既定値（`agentic_search.MAX_HITS` とは別の定数・§1.6）。ここに1箇所だけ持ち、
# `_dispatch()`／管理画面基準値（`sherpa/routers/system_extras.py::_admin_settings_view`）の
# 両方が参照する（`run_qa(max_hits=20)` という既存のハードコード既定と同じ値）。
QA_MAX_HITS_DEFAULT = 20

_log = logging.getLogger("sherpa")

_LENSES = ("impact", "troubleshoot", "qa", "author")

# S3 trace 保存の上限（RV MEDIUM・2026-07）: エージェントループの反復回数に構造的な上限が無い
# （MAX_TURNS はあるが、ask_user 再開や troubleshoot の複数ツール呼びで1ターンあたりのノード数は
# 理論上増え得る）ため、1ターンに保存する trace が無制限に肥大化しないよう保険の上限を設ける。
# 120 は通常の qa/troubleshoot（数個〜十数ノード）に対して十分な余裕を持たせた値。
_MAX_TRACE_NODES = 120
# detail は grep/ES 抜粋やツール引数の要約文字列。UI（.fdetail）はチップ化して短く見せる想定なので
# 200 文字あれば実用上十分（それ以上は DB/レスポンスサイズの無駄）。
_MAX_TRACE_DETAIL_CHARS = 200

# v2（_cap_trace_v2）の二段上限。ソフト上限（_MAX_TRACE_NODES）は「親（他ノードの parent_id）は
# 必ず残す」規則のため、親の数自体が多いケース（例: N件の親が1件ずつ子を持つ）では実効上限として
# 機能しない。ハード上限はソフト対応後もなお超過する場合の絶対的な安全弁（保護対象の多さに
# 関わらず最終的に守る・古いサブツリー単位で畳んでも守れない病的ケースは honest failure マーカーで
# 切り詰める）。
_MAX_TRACE_NODES_HARD = 400
# trace 列をシリアライズしたバイト数の安全弁。ノード数の上限だけでは、個々のノードの
# metrics/evidence_ids が肥大化するケース（発行側の不具合等・件数は少なくても内容が大きい）を
# 捕捉できないため、件数と独立にバイト数でも守る。
_MAX_TRACE_BYTES = 1_000_000
# 集約ノード1件に載せる evidence_ids の上限。完全な和集合は肥大化しうるため上限で切り、
# 超過分は件数だけ metrics.omitted_evidence_count に残す。
_MAX_TRACE_AGGREGATE_EVIDENCE_IDS = 20

# ---- R1a（横断レビュー対応・2026-07-13）: 会話継続＝履歴 priming ----
# 「追質問が前ターンを理解しない」を解消するため、直近ターンの (user, assistant) 完全対を
# Ctx.history（chat_service.py 内で構築・providers/base.py 参照）として全 provider に注入する
# （Codex ネイティブ resume は別スライス R1b＝ここではやらない）。二重キャップ（対数＋文字予算）の
# 根拠は提案 R1a-3。トークン計測ツールは repo に無い（tiktoken 未導入・調査済み）ため、文字数ベースの
# 予算で近似する（「実装時に計測して決定」の帰結＝正確なトークン数ではなく安全側の近似値）。
_HISTORY_TURNS = int(os.environ.get("SHERPA_HISTORY_TURNS", "6"))              # 直近 N 対（対数キャップ）
_HISTORY_MSG_CHARS = int(os.environ.get("SHERPA_HISTORY_MSG_CHARS", "1200"))   # 1メッセージの上限文字数
_HISTORY_CHAR_BUDGET = int(os.environ.get("SHERPA_HISTORY_CHAR_BUDGET", "6000"))  # 履歴全体の文字予算


def _trace_bytes(values) -> int:
    """trace 候補列（dict の値の並び）を**実保存と同じシリアライズ**で測ったバイト数。

    実保存（`store/conversations.py` の `Json(trace)`＝psycopg の `_JsonDumper._dumps` 既定値＝
    `json.dumps` を追加引数なしで呼ぶ）は `ensure_ascii` 既定の `True`（非ASCIIをバックスラッシュ+u+4桁
    16進のエスケープ列へ変換＝日本語主体の detail/label は UTF-8 直書きよりバイト数が増える）。
    SSE 側（`chat_turns.TurnBuffer.append`・`ensure_ascii=False`）とは意図的に異なる値を使う＝
    ここが守るべきは DB 保存サイズであって送信ペイロードのサイズではないため（`sherpa/` に
    `set_json_dumps` の override は無い＝psycopg の既定がそのまま実効値であることを実測確認済み）。
    `default=str` はあえて実保存（default 未指定＝非対応型で `TypeError`）とは**一致させない**
    （安全側・クラッシュさせない）: trace の要素は文字列/dict/list/数値のみが実態のため通常は
    実サイズと一致し、`default=str` は理論上の非対応型（実際には現れない）に対する保険にすぎない。
    """
    return len(json.dumps(list(values), ensure_ascii=True, default=str).encode("utf-8"))


def _within_hard_limits(nodes: dict) -> bool:
    return len(nodes) <= _MAX_TRACE_NODES_HARD and _trace_bytes(nodes.values()) <= _MAX_TRACE_BYTES


def _order_by_age(nodes: dict, age: dict) -> list:
    """age（元の挿入順インデックス。集約ノードは代表した中で最も古いものの値）昇順で並べる。
    集約ノードは常にどれか実ノードの age を一意に引き継ぐため、同値（タイ）は起きない。"""
    return sorted(nodes.values(), key=lambda n: age.get(n["id"], -1))


def _capped_evidence_ids(members: list) -> tuple:
    """集約対象ノード群の evidence_ids を和集合化し `_MAX_TRACE_AGGREGATE_EVIDENCE_IDS` 件で切る。
    戻り値は (切り詰め後のリスト or None, 切り捨てた件数)。"""
    all_ids = sorted({eid for n in members for eid in (n.get("evidence_ids") or [])})
    capped = all_ids[:_MAX_TRACE_AGGREGATE_EVIDENCE_IDS]
    return (capped or None), (len(all_ids) - len(capped))


def _aggregate_node(id: str, kind: str, label: str, detail: str, *, parent_id, agent_run_id,
                    members: list, event_type: str | None = None) -> dict:
    """複数ノードを1件へ畳んだ集約イベントを組み立てる（`exec_event._build_reserved_event` 経由・
    id は `_group_id`/`_subtree_id` の予約名前空間のみを想定）。件数は必ず `metrics.omitted_count`
    に載せる（「件数つき集約イベント」契約）。"""
    evidence_ids, omitted_evidence = _capped_evidence_ids(members)
    metrics = {"omitted_count": len(members)}
    if omitted_evidence:
        metrics["omitted_evidence_count"] = omitted_evidence
    return exec_event._build_reserved_event(id, kind, label, detail, "done", event_type=event_type,
                                            parent_id=parent_id, agent_run_id=agent_run_id,
                                            metrics=metrics, evidence_ids=evidence_ids)


def _group_id(parent_id, kind, agent_run_id) -> str:
    """(parent_id, kind, agent_run_id) を null を明示区別した正規 JSON 配列にして sha1 の全40桁で
    表す（実用上の単射性を確保するため切り詰めない）。文字列連結＋プレースホルダ文字列だと、
    None と実際にその文字列を持つ値（agent_run_id='main' はメイン run の正規値そのもの）が
    衝突しうるため、JSON の null／文字列という型レベルの違いで一意性を担保する。
    `exec_event.RESERVED_ID_PREFIXES` の `"trace-omitted:"` を名乗る＝通常イベント（`build_event`）
    はこの id 空間を使えない。"""
    canon = json.dumps([parent_id, kind, agent_run_id], ensure_ascii=False)
    return f"trace-omitted:{hashlib.sha1(canon.encode('utf-8')).hexdigest()}"


def _subtree_id(root_id: str) -> str:
    """`exec_event.RESERVED_ID_PREFIXES` の `"trace-subtree:"` を名乗る（`_group_id` と同型・全40桁）。"""
    return f"trace-subtree:{hashlib.sha1(root_id.encode('utf-8')).hexdigest()}"


def _normalize_effective_parents(items: dict) -> tuple:
    """各ノードの実効 parent_id を返す（非 null だが現集合内に無い参照は親なしへ正規化）。
    戻り値は (id→実効parent_idの dict, 正規化した件数)。本番経路はこれでクラッシュしない
    （壊れた/古い parent_id 参照があっても親なし扱いへ安全側に倒すだけ）。

    本関数自体は内部計算のみを返す（呼び出し元 `_cap_trace_v2` が件数に関わらず必ず呼び、
    戻り値で `items` 各ノード自身の `parent_id` フィールドを書き換えるところまでが「正規化」の
    契約＝本関数単体では出力ノードは書き換わらない）。
    """
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
    """末端（leaf）のうち個別維持する末尾側と、集約対象にする先頭（古い）側を決める。

    集約後にできる集約ノードの数も budget に数える＝ `(個別維持数 + 集約グループ数) <= budget` を
    満たすまで、古い方から1件ずつ追加で集約対象へ回す（単調に古い方から削るだけなので必ず停止する）。
    全件を集約対象にしても budget に収まらない場合（例: 葉が1件ずつ別 agent_run_id で集約が効かない）
    は全件を集約対象として返す（呼び出し側のハード上限段階が引き継ぐ）。
    """
    n = len(leaf_ids)
    if n <= budget:
        return leaf_ids, []

    def key(nid):
        node = items[nid]
        return (effective_parent.get(nid), node.get("kind") or "think", node.get("agent_run_id"))

    seen_groups: set = set()
    k = 0
    while k < n and (n - k) > budget:            # 集約コスト0と仮定した素朴な見積りまで進める
        seen_groups.add(key(leaf_ids[k]))
        k += 1
    while k < n and (n - k) + len(seen_groups) > budget:   # 集約ノード自体の分をさらに削る
        seen_groups.add(key(leaf_ids[k]))
        k += 1
    return leaf_ids[k:], leaf_ids[:k]


def _soft_cap_v2(items: dict, effective_parent: dict, age: dict) -> dict:
    """①ソフト上限（_MAX_TRACE_NODES）: 親（他ノードの parent_id）は件数に関わらず必ず残し、
    末端だけを (parent_id, kind, agent_run_id) 単位で件数つき集約ノードへ畳む。集約ノード自体も
    予算に数える（`_split_leaves_for_soft_budget`）。`age`/`effective_parent` は生成した集約ノードの
    分をその場で拡張する（後段のハード上限処理がそのまま引き継げるように）。
    """
    protected = {effective_parent[nid] for nid in items if effective_parent.get(nid) is not None}
    leaf_ids = [k for k in items if k not in protected]           # 挿入順（末尾＝最新）

    budget = max(0, _MAX_TRACE_NODES - len(protected))
    kept_leaf_ids, dropped_leaf_ids = _split_leaves_for_soft_budget(leaf_ids, items, effective_parent, budget)
    kept_leaf_set = set(kept_leaf_ids)

    groups: dict[tuple, list] = {}
    for nid in dropped_leaf_ids:
        node = items[nid]
        key = (effective_parent.get(nid), node.get("kind") or "think", node.get("agent_run_id"))
        groups.setdefault(key, []).append(node)

    result: dict[str, dict] = {}
    for nid in items:                                             # 元の挿入順を保持
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
    """②ハード上限（件数 `_MAX_TRACE_NODES_HARD` ／バイト `_MAX_TRACE_BYTES`）: ①後もどちらか超過
    なら、最も古いサブツリー（森のルート＋その子孫すべて）から順に丸ごと1個の集約ノードへ畳む。
    サブツリー単位でしか消さない（部分的に子だけ・親だけを消さない）ため、親子リンクの断絶
    （orphan）は構造的に起きない。件数超過だけのときは単独ノード（サブツリーサイズ1）を畳んでも
    件数は減らないため skip する（バイト超過のときは単独でも畳む価値があるため畳む）。
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
    for root in sorted(roots, key=lambda r: age.get(r, -1)):       # 古いサブツリーから
        over_count = len(result) > _MAX_TRACE_NODES_HARD
        over_bytes = _trace_bytes(result.values()) > _MAX_TRACE_BYTES
        if not over_count and not over_bytes:
            break
        if root not in result:
            continue                                                # 既に他サブツリーの一部として畳まれた
        member_ids = [i for i in subtree_ids(root) if i in result]
        if len(member_ids) <= 1 and not over_bytes:
            continue                                                # 件数超過だけなら単独ノードは畳んでも無意味
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
    """③ ①②でも超過（保護対象だけでハード上限を超える等の病的ケース）: それ以上の集約は試みず、
    先頭に `budget_limit_reached` マーカー1件を置いて末尾（最新）優先でハード上限件数ぴったりまで
    機械的に切り詰める（honest failure・この経路のみ親子リンクの保存を保証しない）。

    件数を合わせるだけでは、保持ノードの中に巨大な metrics/evidence_ids を持つ単一ノードが
    残っているとバイト上限を再び超えうる。件数での切り詰め後もバイト超過が続く限り、保持ノードを
    古い方から1件ずつ追加で削る（マーカーは常に保持）ループで収束させる。

    停止性の根拠: ループは `kept`（有限リスト）から1件ずつ単調に削るだけで、`kept` が空になれば
    marker 単独の出力になる。marker は固定文言＋小さい `metrics`（整数2つ）のみで構成され、その
    シリアライズ後バイト数は `_MAX_TRACE_BYTES`（既定 100万バイト）に対して常に無視できるほど
    小さい（`original_total`/`omitted` の桁数は現実的な trace 件数では高々数桁）ため、最悪ケース
    （`kept=[]`）でも通常はバイト上限内に収まる＝ループは高々 `len(kept)` 回で必ず停止する
    （それでも収まらないのは `_MAX_TRACE_BYTES` を極端に小さく設定した場合のみで、その場合も
    marker 単独をそのまま返す＝それ以上削るものが無い honest failure の最終形）。
    """
    ordered = _order_by_age(nodes, age)
    keep_n = max(0, _MAX_TRACE_NODES_HARD - 1)                      # マーカー1件分を確保
    kept = ordered[-keep_n:] if keep_n else []
    omitted = len(ordered) - len(kept)
    out = [_budget_limit_marker(omitted, original_total)] + kept
    while kept and _trace_bytes(out) > _MAX_TRACE_BYTES:
        kept = kept[1:]                                              # 保持ノードのうち最も古い1件を追加で削る
        omitted += 1
        out = [_budget_limit_marker(omitted, original_total)] + kept
    return out


def _cap_trace_v2(nodes: dict) -> list | None:
    """v2 trace の上限適用（二段上限・全段とも決定的＝同じ入力なら常に同じ出力）。

    正規化（件数に関わらず必ず実行し、出力ノード自身の `parent_id` も書き換える）→
    ①ソフト上限（`_soft_cap_v2`）→②ハード上限／バイト上限（`_hard_cap_v2`）→③それでも超過なら
    honest failure マーカー（`_budget_limit_truncate`）の順に適用する（詳細は各関数の docstring）。
    """
    if not nodes:
        return None
    items = {k: {**v, "detail": (v.get("detail") or "")[:_MAX_TRACE_DETAIL_CHARS]} for k, v in nodes.items()}

    # 正規化は件数に関わらず必ず実行し、各ノード自身の parent_id を実効値へ書き換えてから
    # 以降の処理・高速経路（下の件数/バイト上限内チェック）の両方に流す（高速経路だけ正規化が
    # 反映されない、という抜け道を作らないため）。
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


def _audit_search_helper(settings: dict) -> str | None:
    """監査 detail 用の下調べ役表記（`"openai/gpt-4o-mini"` 形式）。未設定は None。

    **生値をそのまま監査へ入れない**: provider は allowlist、モデル名は形式検証済みのものだけを
    載せる（検索/集計対象の監査ログに任意文字列を持ち込まない）。`search_helper.resolve` は
    非空の不正値を `InvalidSearchHelperConfigError` で送出する契約（黙って None へ倒さない・
    honest failure はチャット本体側が既に伝える）ため、ここでは監査行自体（provider/lens 等）を
    丸ごと失わない fail-open として、この1フィールドだけ None（未設定と同格）に倒す。
    """
    from . import search_helper
    s = settings or {}
    # 実際に効くのは OpenAI 直結構成のときだけ（Codex は自分でツールを回すため介在しない）。
    # 効かない構成で設定値だけ監査へ出すと「安いモデルで読んだ」と誤読されるため出さない。
    # effective_agent() 経由（保存 agent=openai でも A7 で選択中でなければ実行は ollama へ
    # フォールバックする＝その場合ここで openai 向け判定を走らせない）。
    if agent_constructs.effective_agent(s) != "openai":
        return None
    try:
        sub = search_helper.resolve(s)
    except search_helper.InvalidSearchHelperConfigError:
        # 監査は最善努力（fail-open）＝下調べ設定の不正はチャット本体側で honest failure として
        # 既に伝えている。監査行自体（provider/lens 等）を丸ごと失わないよう、この1フィールド
        # だけ None（未設定と同格）に倒す。
        return None
    if not sub:
        return None
    provider = sub.get("provider")
    if provider not in (search_helper.OLLAMA, search_helper.OPENAI):
        return None
    return f"{provider}/{sub.get('model')}"


def _audit_chat_turn(uid, conversation_id, settings, *, lens, user_msg_id,
                     assistant_msg_id, world, scope_paths, personal, stopped: bool = False) -> None:
    """S5: 1ターン完了時に監査へ記録する（ユーザー要望「プロンプトと回答も追えるように」の土台）。

    detail に**本文は入れない**（hash-chain 行の肥大化防止・画面のシンプルさ維持）。id とメタだけ持たせ、
    本文は必要な時だけエクスポート（`GET /admin/audit/export?include_chat_content=1`）で
    messages 台帳から join して付与する（api.py 側）。clarify で終わったターン（assistant 未保存）は
    `assistant_msg_id=None` で記録する。失敗はチャット本体を止めない（fail-open・ベストエフォート、
    `_sweep_expired_announcements` の背景処理と同じ「監査は最善努力」の扱い＝ここは admin の同期操作
    ではなくチャット応答そのものなので、fail-closed でユーザーの回答を握り潰すのは本末転倒）。

    `stopped=True`（RV MEDIUM・2026-07-03再検証）: UI フィードバック1（途中停止）で assistant 未保存の
    まま打ち切ったターンも clarify と同格に記録する（`assistant_msg_id=None`・detail に `stopped:true`）。
    停止前は空でなければ監査から丸ごと消えていた＝「誰が何を聞いて途中で止めたか」が追えない欠落だった。
    """
    # RV HIGH（2026-07-03）: settings["agent"]/SHERPA_AGENT は自由文字列（バリデーション経路を通らず
    # DB に直接入り得る・過去の env 誤設定等）。監査 detail に生値をそのまま入れず allowlist で正規化する
    # （検索/集計対象になる監査ログに任意文字列を持ち込まない）。
    provider_saved = (settings.get("agent") or agent_constructs.default_agent()).lower()
    provider_saved = provider_saved if provider_saved in AGENT_PROVIDERS else "unknown"
    # 実際にこのターンで使われたプロバイダ（`effective_agent()` 経由・A7 で選択中でないクラウド系
    # agent は ollama 扱いに統一）。保存値（provider_saved）と食い違い得るため、監査は実効値を主
    # フィールド（"provider"）・保存値を副フィールド（"provider_saved"）として両方残す。
    provider = agent_constructs.effective_agent(settings)
    provider = provider if provider in AGENT_PROVIDERS else "unknown"
    try:
        store.audit(uid, "chat.turn", "conversation", f"conv:{conversation_id}",
                   detail={"message_id_user": user_msg_id, "message_id_assistant": assistant_msg_id,
                           "lens": lens, "world": world,
                           "scope_paths": len(scope_paths or []), "personal": bool(personal),
                           "provider": provider, "provider_saved": provider_saved,
                           "stopped": bool(stopped),
                           # 検索アシスタント（2026-08-15）: 資料を読んだのが誰かを監査からも追える
                           # ようにする（費用の内訳・「安いモデルにしたのに高い」の切り分け）。
                           # 未設定は None＝従来どおりメインが読んだターン。
                           "search_helper": _audit_search_helper(settings)},
                   outcome="success", severity="info")
    except Exception as e:
        _log.warning("chat.turn audit write failed (fail-open, chat continues): %s", e)


def _resolve_lens(lens, message):
    """調べ方の明示指定を解決する（SC-6b §3.1）。優先順位: スラッシュ接頭辞（1回限り）＞
    `ChatReq.lens`（調べ方ブロックの明示選択）＞自動（既存の Tier1〜3）。

    返り値 `(explicit_lens, lens_source, lens_block, message)`。`explicit_lens` は `_build_router()`
    へそのまま渡す値（`None`＝自動判定に委ねる）。スラッシュ接頭辞が見つかれば本文から取り除いた
    `message` を返す（保存される質問・起点語抽出のいずれからも接頭辞のノイズを除く）。`lens` は
    `None`／`"auto"` のどちらも「ブロックが自動のまま」を表す（裁定3・既定は省略送信）。
    `lens_block`（RV1 #2）: `lens`（ブロックの継続設定）を正規化しただけの値（`None`/`"auto"` は
    `None`）——スラッシュで `explicit_lens` が上書きされても、ブロック自体の継続設定は別途
    `_resolve_scope()` の `lens_block` として持ち越す（会話を開き直したときの復元用）。
    """
    lens_block = lens if lens and lens != "auto" else None
    slash_lens, stripped = _extract_slash_lens(message)
    if slash_lens:
        return slash_lens, "slash", lens_block, stripped
    if lens_block:
        return lens_block, "explicit", lens_block, message
    return None, "auto", lens_block, message


def _build_router(known, world, settings, can_ask, user_id=None, explicit_lens=None, scope_meta=None):
    """hybrid intent ルータ（§3）。heuristic 確信→（曖昧）LLM 分類→（なお曖昧）clarify or qa fallback。

    **per-turn memoize**＝同一ターンで route が複数回呼ばれても（_GenProvider が route 後に _gather で再 route）
    LLM 分類を二重実行しない（Codex RV Med）。`can_ask`＝ストリーミングのみ True（非対話は qa fallback）。
    `user_id`（S1）は intent 分類の利用量計測（`kind='intent'`）に渡すだけ＝ルーティングの判断には使わない。

    `explicit_lens`（調べ方ブロックの明示指定・スラッシュ接頭辞含む＝SC-6b §3.1・裁定10）: 非 None
    のときは Tier1〜3（heuristic／LLM分類／確認カード）を全て飛ばし `chat_router.decision_for()` で
    直接 decision を組み立てる。「確認してから進めて」（`_wants_confirm_first`）は明示指定より優先
    する例外——調べ方が決まっていても対象の絞り込みを確認したい場合があるため。
    `scope_meta`（RV1 #3・RV2 #1・SC-6e）: 確認カードの payload へ解決済みの探す対象・範囲・
    `lens_source`／`lens_block`／`tools`（検索経路トグル）を載せる（`_resolve_scope` の返り値・
    knowledge オフ時は `None`）——確認カードの再送時に1回だけ既存の `ChatReq.lens`／scope／`tools`
    経路へ戻す（`lens_source=="slash"` はフロントが接頭辞を復元する）ための情報で、判定ロジック
    自体には使わない。
    """
    cache: dict = {}

    def _route(message):
        if message in cache:
            return cache[message]
        # High-2（F2-2）: 「確認してから進めて」指定は provider/dispatch 到達前に確認カードを出す決定的
        #   ガード（プロンプト遵守任せ＝agents 側 F2 では「必ず」を保証できないため）。既存 clarify と同経路
        #   （lens="clarify" の decision → provider が question を emit → S1 の永続化に乗る）。確認ID 付き
        #   （回答の再送）では発動しない＝ループ防止。can_ask=False（非対話）は質問できないので通常判定へ委ねる。
        if can_ask and _wants_confirm_first(message):
            sm = scope_meta or {}
            d = _confirm_first_decision(message, lens=explicit_lens, layer=sm.get("layer"),
                                        scope_paths=sm.get("scope_paths"),
                                        lens_source=sm.get("lens_source"), lens_block=sm.get("lens_block"),
                                        tools=sm.get("tools"))
            cache[message] = d
            return d
        if explicit_lens:                                        # 調べ方の明示指定＝Tier1〜3 を飛ばす（裁定10）
            d = _decision_for(explicit_lens, message, known, reason="明示指定")
            cache[message] = d
            return d
        d = _heuristic_route(message, world, known_terms=known)
        if not d.get("confident"):                              # 曖昧時だけ Tier2/3（コスト最小・大半は無料）
            c = intent_llm.classify(message, settings, user_id=user_id, world=world)  # Tier2: 安価 LLM 分類（未接続/失敗は None）
            if c and c.get("lens") in _LENSES and c.get("confident", True):
                d = _decision_for(c["lens"], message, known, reason="AI判定（意図分類）")
            elif can_ask:
                d = _clarify_decision(message)                  # Tier3: 本人に確認（ask_user 同経路）
            else:
                d = _decision_for("qa", message, known, reason="曖昧なため既定（検索）")   # 非対話 fallback
        cache[message] = d
        return d

    return _route

# 経路チップ（R2）：レンズ→使った経路。**専門用語を出さない**（04-画面の原則.md §5/§6・ui-rv2 Med#6）。
_ROUTE_PATH = {"impact": ["関係を確認"], "troubleshoot": ["関連を確認", "文書を検索"], "qa": ["文書を検索"],
               "author": ["文書を検索", "資料を作成"]}
# 出典に出さない内部来歴マーカー（DL できる文書ではない）。scope と共有（単一定義・RV Med#2）。
_NON_DOC = scope.NON_DOC
_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{6,}"
    r"|-----BEGIN[^-]+PRIVATE KEY-----[\s\S]*?-----END[^-]+PRIVATE KEY-----)"
)
_KV_SECRET_RE = re.compile(r"(?i)\b(pass(?:word|wd)?|secret|api[_-]?key|token|authorization)\b(\s*[=:]\s*)(\S+)")


def emit_pace() -> float:
    """思考ステップの間隔（秒）。テストは 0 で即時。"""
    try:
        return float(os.environ.get("SHERPA_STREAM_PACE", "0.35"))
    except ValueError:
        return 0.35


def _known_terms(session, world) -> list:
    """起点語抽出のヒント＝world内の全ノード名（データ由来・テーマ非依存）。

    secRV 範囲外是正 追補（2026-07-19・RV指摘 HIGH-2）: 以前は `.data()` で無制限に全件展開しており、
    knowledge=true の全チャット（impact/troubleshoot/qa すべて）がここを通るため、`lens_service`/
    `world_neo4j` に実装した Neo4j 安全弁（timeout・緊急天井）が本関数だけ迂回されていた。
    `lens_service._run_capped`（ソフト縮退＝timeout→空リスト・天井到達→cap 内の部分リストへの
    warning 付き縮退）を再利用する。`_run_capped` は private だが、chat_service は既に
    `lens_service`（run_qa/run_troubleshoot）と関わる層で import 方向も lens_service→chat_service
    ではない（循環なし）ため、同一パッケージ内の意図的な共用として直接 import する。ここは起点語
    抽出の**ヒント**（ルーティング補助）であり厳密解が必須ではないため、ソフト縮退のままでよい
    （fail-loud にする必要はない）。返却形（name 文字列のリスト）は不変。
    """
    rows = _run_capped(
        session, "MATCH (n:Entity {world_id:$v}) RETURN DISTINCT n.name AS name",
        log_world=world, v=world,
    )
    return [r["name"] for r in rows if r["name"]]


def _src_url(doc: str, world: str, res: "importance.Resolution | None" = None) -> dict:
    """出典1件 → 原本DLリンク（原本・パス基準＝doc は rel_path・slash を含むので query で渡す）。

    `res`（省略可・I2・2026-09-05）: 登録者が `_重要度.txt` で付けた重要度の解決結果。あれば
    `importance`/`importance_reason` を条件付きで追加する（`importance_source` は出典には出さない・
    J4）。無ければ（省略時含む）従来どおり2キーのまま——受け入れ条件（重要度制御ファイルの無い
    world で出典の出力完全不変）はこの引数を渡さない限り自動的に満たされる。
    """
    return {"doc_id": doc,
            "download_url": f"/documents/download?world={quote(world)}&rel={quote(doc, safe='')}",
            **importance.public_fields(res)}


def _sources(docs, world) -> list:
    seen, filtered = set(), []
    for d in docs:
        if (d and d not in seen and d not in _NON_DOC     # 内部来歴は出典に出さない
                and not importance.is_importance_control_path(d)):   # 重要度設定ファイル自体は出典に出さない（§5）
            seen.add(d)
            filtered.append(d)
    # I2: world 全体を1回だけ解決し（`resolve_many`）、対象の doc だけ引く（`_src_url` へ渡す）。
    # 未登録 world（`worlds.world_dir` が None）は解決しない＝従来どおり2キーのまま。
    wd = worlds.world_dir(world)
    res_map = importance.resolve_many(world, filtered, root=wd) if wd else {}
    return [_src_url(d, world, res_map.get(d)) for d in filtered]


def _redact(text: str) -> str:
    """外部LLM/画面へ渡る検索抜粋の明らかな秘密を伏せる（agentic_search と同じ多層防御）。"""
    t = _SECRET_RE.sub("[REDACTED]", text or "")
    return _KV_SECRET_RE.sub(r"\1\2[REDACTED]", t)


def _truncation_headline_suffix(result) -> str:
    """レンズ結果の `notes`（`lens_service`/`impact_service` の打切り申告・平文）を headline へ足す。

    `notes` は `data` には載るが headline には出ないため、そのままだと
    「該当する記述は見つかりませんでした」が**探せていないだけ**の場合にも断定的に出てしまう
    （＝チャットが主入口なので、ここに出さないと利用者は取りこぼしに気づけない）。
    打切りが無ければ空文字＝既存の headline は完全に不変。
    """
    notes = result.get("notes") or []
    return ("　⚠ " + " ".join(notes)) if notes else ""


def _answer_impact(result, world):
    """K12（2026-09-04-グラフのソース正典化.md §4）: 「確実/要確認」の2値判定表示は機構ごと撤去。

    `items` は全件同格（構造的な影響として同じ扱い）。presumed（grep 共起の推定）だけは別枠のまま残す。
    起点の自動橋渡し注記（`starts[].via`）は REALIZES 撤去（K10）に伴い供給源が無くなったため撤去。
    """
    items = result["items"]
    start = result["start"]
    presumed = result.get("presumed") or []            # 構造的な影響が0件時の「資料からの関連推定」
    code_silent = not items                            # 構造的な影響が0＝コードの少ない/無いフォルダのサイン
    if items:
        headline = f"「{start}」を変えると {len(items)}件に影響。"
    elif presumed:                                     # 構造的な影響は無いが資料から関連を辿れた＝0で突き放さない
        names = "、".join(dict.fromkeys(p["name"] for p in presumed[:3]))
        headline = (f"「{start}」に構造的な依存は見つかりませんでしたが、資料から"
                    f"**関連の可能性**が {len(presumed)}件（推定・要確認）: {names} など。")
    else:
        headline = f"「{start}」の影響先は見つかりませんでした（表記ゆれ、または影響なし）。"
    if code_silent:                                    # 次の一手＝検索へ素直に誘導（フォルダ起因と断定しない・RV Low）
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
                   web_search=False, depth_profile=None, tools=None):
    """有効な範囲を決める（D）。鏡では**明示選択 ＞ world 全体**（auto-scope 推定は撤去・MIRROR §3）。

    返り値 `{world, scope_paths, source, layer, lens_source, lens_block, web_search, depth_profile, tools}`。
    `source`=explicit/all（ヘッダ「参照中の範囲」用）。`layer`（探す対象・調べ方ブロック §3.4）は
    省略（`None`）のときだけ `"both"` に正規化する。不正な内部値（HTTP 入口の pydantic Literal を
    経ていない値）は `layer.normalize_layer` が `ValueError` を送出する（fail-loud・黙って both へ
    丸めない）。
    `lens_source`（調べ方の明示指定元・SC-6b §3.1）: `auto`｜`explicit`｜`slash`。既定 `"auto"`。
    `lens_block`（RV1 #2）: `ChatReq.lens`（ブロックの継続設定・自動は `None`）そのもの——
    スラッシュ接頭辞（1回限りの明示）で `lens_source="slash"` になった場合でも、ブロックが
    継続して持っていた値を別途保持する。スラッシュは実効レンズ（`answer.lens`）だけを1回上書きし
    ブロックの選択状態は変えない契約（§3.1）のため、会話を開き直したときの復元は
    `lens_source=="slash"` ならこの `lens_block` を、`"explicit"` なら実効レンズを、`"auto"` なら
    自動を使う（`web/chat/scope.js::applyConversationScope` 参照）。
    `web_search`（WEB-1・既定 False）: このチャットで Web 検索を希望したか（`ChatReq.web_search`）。
    実際に Codex へ反映されるかは管理者許可・接続先（Azure 等では常に無効）に依る
    （`sherpa/providers/codex/sandbox.py::_web_search_disabled_value` が唯一の判定点）——ここでは
    復元用に希望値をそのまま記録するだけ。
    `depth_profile`（調べる深さ・調べ方ブロック §3.2・SC-6c）: 省略（`None`）は `"standard"` に
    正規化する（`depth_profile_mod.normalize_depth_profile`）。不正な内部値は同様に `ValueError`
    （fail-loud）。
    `tools`（検索経路トグル・調べ方ブロック §3.6・SC-6e）: 省略（`None`）は全 ON に正規化する
    （`tools_pref_mod.normalize_tools_pref`）。不正な内部値は同様に `ValueError`（fail-loud）。
    `layer_mod.scope_with_layer` がこの dict をそのままコピーするため `answer.scope.lens_source`／
    `lens_block`／`web_search`／`depth_profile`／`tools` へそのまま伝わる（会話保存の互換は §4.3・
    裁定4＝旧回答は `"auto"`／`None`／`False`／`"standard"`／全 ON 扱い）。
    """
    explicit = scope.normalize_scope_paths(scope_paths)   # strip/空除去/重複排除
    return {"world": world, "scope_paths": explicit, "source": "explicit" if explicit else "all",
            "layer": layer_mod.normalize_layer(layer), "lens_source": lens_source,
            "lens_block": lens_block, "web_search": bool(web_search),
            "depth_profile": depth_profile_mod.normalize_depth_profile(depth_profile),
            "tools": tools_pref_mod.normalize_tools_pref(tools)}


def _es_hits(world, query, sp, k=8, redact=False, layer=None):
    """ES（BM25）上位ヒットを、現 world に実在する doc だけに絞って返す。

    facts 統合では **BM25 のみ**（vector=False＝qa ごとのクエリ埋め込みコストを避ける・RV Low）。
    現 world の実在集合は1回だけ作る（古い ES 索引由来の 404/別内容リンクを出さない・RV High）。
    `layer`（省略可・既定 `None`＝`"both"`）: 呼び出し元が qa 補完のときだけ渡す
    （troubleshoot 補完＝`_es_troubleshoot_cards` は渡さない＝§3.5 非適用）。
    """
    try:
        from . import documents, es_index
        valid = documents.world_rel_set(world)
        # `es_index.search()` は (hits, reason) タプル（RV2）。BM25 実クエリ失敗（es_query_failed）
        # もありうるが、この経路（facts 統合）には degraded 報告の仕組みが無いため意図的に捨てる
        # （構造化された degraded 集計が要る呼び出し元は `search_service._search_keyword()` 参照）。
        hits, _reason = es_index.search(world, query, scope_paths=sp, k=k, vector=False, layer=layer)
    except Exception:
        return []
    out = []
    for h in hits:
        doc = h.get("doc_id")
        if doc and doc in valid and scope.in_scope(doc, sp):
            if redact:
                h = {**h, "text": _redact(h.get("text", ""))[:500]}
            out.append(h)
    return out


def _es_citations(world, query, sp, k=8, layer=None):
    """ES（BM25）上位ヒットを citation 形に（Codex/非agentic も ES を参照できるよう facts に混ぜる）。

    H3（SC-4 接続・CITE-1）: rag_parent_return（P3/P2/chunk・§3.3/§3.4 の非agentic 展開）で本文の
    完全性を上げたうえで、excerpts.display_quote（利用者向け引用を人間向け MD の該当節へ引き直す・
    §9）で quote を差し替える。検索対象（ES ヒット選定）自体は変えない——ここは返す直前の後処理のみ。
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
    """run_qa(grep) の citations に ES ヒットを統合。grep↔ES を**交互**に並べ（先頭に ES も入れる）doc_id+span で重複排除。

    `layer`（省略可・既定 `None`＝`"both"`）: qa レンズの ES 補完にのみ渡す（§3.5・troubleshoot 補完は
    `_merge_troubleshoot_with_es` が別途 layer 無しで呼ぶ＝非適用）。
    """
    from . import citations
    grep = list(result.get("citations", []))
    es = _es_citations(world, query, sp, layer=layer)
    merged = citations.dedupe_round_robin_by_doc_span(grep, es)   # round-robin で先頭付近に ES も来る（RV Med）
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
    """原因候補カードを group 順の round-robin で並べ、名前または doc_id+span で重複排除。"""
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
    """H3（SC-4 接続・CITE-1）: evidence.grep の `text` も excerpts.display_quote で人間向け MD の
    該当節へ引き直す（カードの UX 上限＝500字クリップは維持——`_es_hits(redact=True)` が既に
    redact+clip 済みの `text` を fallback として渡すため、`excerpt_source=="rag"` のときは無変更。
    `"human_md"` のときだけ新しい本文へ redact+clip をかけ直す）。親返し（サイズ拡張）は非適用
    （troubleshoot カードは終始「近傍1件＝1カード」の一覧・簡潔さが目的で、qa の引用とは UX が異なる）。
    """
    from . import excerpts
    by_doc, order = {}, []                                  # doc ごとに1カード・複数 span は evidence.grep に集約（name dedupe で別 span を落とさない・RV Med）
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
            card["evidence"]["grep"].append(ev)            # 同一 doc の未見 span のみ追加
    return [by_doc[d] for d in order]


def _merge_troubleshoot_with_es(result, world, query, sp):
    """run_troubleshoot(グラフ+grep) の原因候補に ES 文書候補を統合する（非agentic 専用）。"""
    base = list(result.get("candidates", []))
    es_cards = _es_troubleshoot_cards(world, query, sp)
    return {**result, "candidates": _dedupe_round_robin_cards(base, es_cards)}


# 検索経路トグル（調べ方ブロック §3.6・SC-6e）の honest-failure envelope は
# `agentic_search.tools_blocked_env`（SC-6e）が単一の真実源——非agentic（`_dispatch`）・
# agentic（`providers/base._agentic_run`）の両経路が同じ固定文言・サイドカー契約を共有する。


def _dispatch(session, lens, payload, world, scope_meta=None, system_settings=None,
             tools_availability=None):
    """レンズ実行（＋範囲フィルタ）。範囲は **world グラフ traversal(Cypher)＋grep/ES/根拠** に効かせる（MIRROR §3）。

    層フィルタ（探す対象・調べ方ブロック §3.4）は qa（author も qa 分岐に落ちる）にのみ適用する。
    impact／troubleshoot は言及エッジ（DOCUMENTS via=mention）が Document とコードを木を跨いで
    繋ぐため受け取っても適用しない（§3.5・§8 裁定論点1）——`env["scope"]["layer_applied"]` で
    黙って無視せず明示する。

    調べる深さ（`depth_profile`・調べ方ブロック §3.2・SC-6c）: `run_impact`/`run_troubleshoot` の
    `depth`・`run_qa` の `max_hits` へ倍率をかけた値を渡す（`sherpa.depth_profile` の乗数表）。
    倍率は「実効基準値」（管理画面の基準値編集＝`system_settings` → env → コード既定、の解決結果）
    に掛ける——基準値そのものは書き換えない。`system_settings`（省略可・既定 `None`）は呼び出し元
    （`handle_message`/`stream_message`）が既に読んだスナップショットをそのまま渡す契約——
    ここで `store.get_system_settings()` を呼ばない（`_dispatch` は DB 不要の単体テスト対象の
    ままにする・呼び出し元が DB 不達を fail-open で吸収する）。`None` は「基準値の管理画面上書き
    なし」として扱い、各モジュールの env 由来の既定値（`env_default`）をそのまま使う。
    `abs_max`: 各モジュールの env-parse hi 引数と同じ値を渡し、管理画面の基準値
    編集（Field 上限まで）＋調べる深さ「最大」の組み合わせでも、倍率適用後の値が既存の絶対上限を
    超えないようにする。

    検索経路トグル（`scope_meta["tools"]`・調べ方ブロック §3.6・SC-6e）: `agentic_search.
    dispatch_tools_for_lens` で実効ツール集合と実行可否を判定する。必須ツールが全て OFF/実接続
    不達なら OFF になったツールへ黙ってフォールバックせず `agentic_search.tools_blocked_env` の
    明示エラーを返す（heuristic 経路・author・agentic 失敗時の単発フォールバックがいずれもこの
    `_dispatch` を経由するため、非agentic 経路全体で同じゲートになる。agentic 経路自体も
    `providers/base._agentic_run` が同じ判定・同じ envelope を使う＝SC-6e）。qa/author は grep（`run_qa`）と
    fulltext（ES 補完）のどちらか一方だけでも実行し、troubleshoot はグラフ必須（内部の運用手順
    grep はグラフ候補カードの enrichment に組み込まれておりこの1軸では分離しない）で fulltext
    補完のみを追加で切り替える。`tools_availability`（省略可・既定 `None`＝全て利用可能扱い）は
    呼び出し元（`handle_message`/`stream_message`）がターンにつき1回だけ計算した
    `agentic_search.tool_availability()` の結果——`system_settings` と同じ「呼び出し元が読んで
    渡す」契約で、ここでは計算しない（`_dispatch` を DB/ネットワーク非依存の単体テスト対象の
    ままにする）。
    """
    sp = (scope_meta or {}).get("scope_paths") or None
    layer = layer_mod.effective_layer(scope_meta, lens)   # 非適用レンズは常に both（layer.effective_layer が判定）
    profile = (scope_meta or {}).get("depth_profile")
    sys_settings = system_settings
    eff, blocked = agentic_search.dispatch_tools_for_lens(
        lens, (scope_meta or {}).get("tools"), availability=tools_availability)
    if blocked:
        env = agentic_search.tools_blocked_env(lens)
    elif lens == "impact":
        base_depth = depth_profile_mod.effective_base(sys_settings, "impact_depth", IMPACT_MAX_DEPTH)
        depth = depth_profile_mod.scaled_depth(base_depth, profile, abs_max=IMPACT_MAX_DEPTH_ABS_MAX)
        result = run_impact(session, payload, world, scope_prefixes=sp, depth=depth)  # 範囲は Cypher で絞る
        env = _answer_impact(result, world)
    elif lens == "troubleshoot":
        base_depth = depth_profile_mod.effective_base(
            sys_settings, "troubleshoot_depth", TROUBLESHOOT_GRAPH_DEPTH)
        depth = depth_profile_mod.scaled_depth(base_depth, profile, abs_max=TROUBLESHOOT_GRAPH_DEPTH_ABS_MAX)
        res = run_troubleshoot(session, payload, world, depth=depth, scope_paths=sp)
        res = _merge_troubleshoot_with_es(res, world, payload, sp) if eff["fulltext"] else res
        env = _answer_troubleshoot(res, world)
    else:                                              # qa: grep＋ES を統合（Codex/heuristic/非agentic も ES 参照）
        base_hits = depth_profile_mod.effective_base(sys_settings, "qa_max_hits", QA_MAX_HITS_DEFAULT)
        max_hits = depth_profile_mod.scaled_ratio(base_hits, profile, abs_max=agentic_search.MAX_HITS_ABS_MAX)
        if eff["grep"]:
            qa_result = run_qa(payload, world, scope_paths=sp, layer=layer, max_hits=max_hits)
            if eff["fulltext"]:
                qa_result = _merge_qa_with_es(qa_result, world, payload, sp, layer=layer)
        else:                                          # grep OFF/不達（blocked でない＝fulltext は確定 True）
            es_cites = _es_citations(world, payload, sp, layer=layer)
            qa_result = {"type": "qa", "question": payload, "answered": bool(es_cites), "citations": es_cites}
        env = _answer_qa(qa_result, world)
    # 参照中の範囲（D/監査）＋このレンズで層フィルタが実効したか（UI が非適用の注記を出すための1項目）。
    env["scope"] = layer_mod.scope_with_layer(scope_meta, world=world, lens=lens)
    return env


# SC-6d（出典0件時の案内・§5）: 「絞られている軸だけ」を範囲→探す対象の順で1つの案内にまとめる
# （既に最も緩い設定の軸は含めない・§8 裁定5）。
_NO_RESULTS_EVEN_AT_LOOSEST_HEADLINE = "範囲・種類を変えても見つかりませんでした（確証なし）。"


def _no_genuine_results(env: dict) -> bool:
    """出典0件で、かつ通常の検索結果 envelope か（RV1 #6）。

    AI未接続・busy（Codex直列化）・下調べ設定不正・下調べ失敗・層を強制できない構成・Neo4j 安全弁
    （timeout/緊急天井）はいずれも honest failure として `data: {}`（空 dict）で返す
    （`_UnwiredProvider`/`_DisabledProvider`/`_plain_run`/`_impact_overload_result`／
    `providers/base.py` の下調べ関連早期リターン／`codex/provider.py` の探す対象強制不可）——
    通常の検索結果（0件含む）は `run_qa`/`run_impact`/`run_troubleshoot` の返り値をそのまま
    `data` に積むため、citations/items/candidates 等のキーを持つ非空 dict になる。「出典0件」だけを
    見ると明示エラーにも再検索案内が付いてしまうため、`data` が空でないことも合わせて確認する。
    """
    return not env.get("sources") and bool(env.get("data"))


_DEPTH_PROFILE_LABEL = {"standard": "標準", "deep": "深く"}   # "max" は既に最も緩いため案内対象外


def _retry_hints(env: dict) -> list:
    """`env["sources"]`（共有KB出典）が0件かつ範囲/探す対象/調べる深さが絞られているときの再検索案内。

    層（探す対象）は `layer_applied`（このレンズで層フィルタが実効したか＝§3.5）が真のときだけ
    案内に含める——impact/troubleshoot は層を受け取っても適用しないため、層を広げても結果は
    変わらない（黙って無視ではなく、そもそも案内自体を出さない）。調べる深さ（SC-6c）は既に
    「最大」でなければ（絞られていれば）案内に含める——標準/深くから直接「最大」へ1回で広げる
    （範囲/層の「全体」/「両方」と同じ「最も緩い設定へ1回で戻す」設計・§5）。呼び出し前に
    `_no_genuine_results(env)` を確認する（`_finalize` 参照）。表示順は §8 裁定5（範囲→探す対象→
    調べる深さ）。
    """
    sm = env.get("scope") or {}
    hints = []
    if sm.get("scope_paths"):                                     # 範囲が絞られている（既に全体なら空リスト）
        hints.append({"kind": "scope", "label": "範囲を全体に広げる", "action": {"scope_paths": []}})
    layer = sm.get("layer")
    if sm.get("layer_applied") and layer in ("docs", "code"):      # 探す対象が限定・かつこのレンズで実効
        label = "コードも含めて探す（今は資料のみ）" if layer == "docs" else "資料も含めて探す（今はコードのみ）"
        hints.append({"kind": "layer", "label": label, "action": {"layer": "both"}})
    depth = sm.get("depth_profile")
    if depth in _DEPTH_PROFILE_LABEL:                              # "max"（既に最も緩い）は対象外
        hints.append({"kind": "depth", "label": f"調べる深さを上げて探す（今は{_DEPTH_PROFILE_LABEL[depth]}）",
                      "action": {"depth_profile": "max"}})
    tools = sm.get("tools")                                        # SC-6e: 検索経路トグルが非既定のときだけ
    if tools and not tools_pref_mod.is_default(tools):
        hints.append({"kind": "tools", "label": "OFF にした検索を戻す",
                      "action": {"tools": dict(tools_pref_mod.DEFAULT_TOOLS_PREF)}})
    return hints


def _is_budget_exhausted(env: dict) -> bool:
    """STOP-1: `providers/base.py::_agentic_run` が調査予算到達（turns_exhausted/
    budget_exceeded/tools_per_turn_exceeded）で既に固定 headline を据えているターンかどうか。
    出典0件（`_no_genuine_results`）と重なっても、予算切れは「探しても恒久的に見つからない」とは
    別の状態のため、この関数が真を返す場合は `_finalize` の「見つからない」断定で headline を
    上書きしない。`task_id == "main"`（`citations.build_evidence_packet` の呼び出し元・
    `providers/base.py::_agentic_run` が `self._sub is None` のときだけ渡す値）に限定する——
    ハイブリッド（`task_id == "sub:{profile_id}"`）は provider 側の budget_exhausted ガード自体が
    `self._sub is None` に限定されており固定 headline を据えていないため、ここでも従来どおり
    0件時の断定文言（`_NO_RESULTS_EVEN_AT_LOOSEST_HEADLINE`）を適用する対象のまま揃える。"""
    packet = (env.get("data") or {}).get("evidence_packet") or {}
    return (packet.get("task_id") == "main"
           and packet.get("stop_reason") in agentic_search._BUDGET_EXHAUSTED_STOP_REASONS)


def _is_codex_timed_out_partial(env: dict) -> bool:
    """Codex CLI 実行がタイムアウト（threading.Timer による kill・機械的事実のみが根拠）で
    打ち切られたターンかどうか——進行中の宣言文がそのまま headline に残った場合に限らず、
    結論の agent_message が無いまま打ち切られ headline が `_gather` の決定的回答のままの
    場合も含む（本文の有無に関わらない・`providers/codex/provider.py::_run_authoring` が
    直接立てる `env["codex_timed_out"]`）。

    `_is_budget_exhausted` と同型のガード——Codex CLI は agentic_search を経由しない別の実行系
    のため、`evidence_packet.stop_reason` の閉じた語彙（`agentic_search.STOP_REASONS`）は流用せず
    独立したフラグにする。出典0件と重なっても「恒久的に見つからない」とは違うため、`_finalize` の
    断定文言で headline を上書きしない。"""
    return bool(env.get("codex_timed_out"))


def _finalize(env, decision):
    env["lens"] = decision["lens"]
    env["route"] = {"lens": decision["lens"], "reason": decision["reason"],
                    "path": _ROUTE_PATH.get(decision["lens"], [])}
    _codex_timed_out = _is_codex_timed_out_partial(env)
    if _no_genuine_results(env):
        hints = _retry_hints(env)
        if hints:
            env["retry_hints"] = hints
        elif (decision["lens"] in ("qa", "author") and not _is_budget_exhausted(env)
              and not _codex_timed_out):
            # 全軸が既に最も緩い設定（全体・資料＋コード・最大）でなお0件＝これ以上緩める軸が無い
            # （§5・RV1 #9・SC-6c で調べる深さの軸を追加）。予算到達の途中結果（STOP-1）・Codex
            # タイムアウトの途中結果はいずれも「見つからなかった」ではないため上書きしない。
            # impact/troubleshoot は層の概念が無く既存の headline が十分具体的なため対象外にする
            # （`_answer_impact`/`_answer_troubleshoot` は変更しない）。
            env["headline"] = _NO_RESULTS_EVEN_AT_LOOSEST_HEADLINE
    if _codex_timed_out:
        # SC-6d と同じボタン機構（retry_hints・data-retry-kind）に載せる——0件案内の hints とは
        # 独立に常に追加する（0件でも中身があっても「続きから調べ直せる」こと自体は変わらない）。
        # クリック時の送信は kind="resume" 専用分岐（web/chat.js）が扱う＝直前の質問を広げて
        # 再送する他の kind とは別系統（固定文言をそのまま送るだけ・resume は codex_session_id
        # 継続に委ねる）。
        env.setdefault("retry_hints", []).append(
            {"kind": "resume", "label": "続きを調べる", "action": {"message": "続きを調べて"}})
    return env


def _pop_evidence_committed(env: dict, trace_nodes: dict):
    """`env["_evidence_committed"]`（provider が `_result` へ同梱したサイドカー・
    `providers/base.py::_evidence_committed_node` 参照）を取り出し、`trace_nodes`（id で dedup
    蓄積する思考ノード集合）へ折り込む。**`_result` を処理する（＝永続化する）のと同じ呼び出しの
    中でしか呼ばない**——独立イベントとして yield すると、停止要求のタイミング次第で `_result` だけ
    discard され孤児イベントになりうるため、`_result` の env に同梱してもらい両者を不可分に扱う。
    公開 `answer` には残さない（`env` から pop 済み）。存在しなければ何もしない（`None` を返す）。
    """
    node = env.pop("_evidence_committed", None)
    if node is not None and node.get("id"):
        trace_nodes[node["id"]] = node
    return node


# secRV 範囲外是正（2026-07-19・影響分析の Neo4j 安全弁＝timeout＋緊急天井・fail-loud＝偽陰性防止）:
# `_dispatch` の impact 分岐（`run_impact`→`ingest.world_neo4j`）が `GraphQueryOverloadError` を
# raise した場合、**LLM 合成を一切経由させず**固定文言の `_result` へ差し替える。impact レンズだけが
# world_neo4j.world_impact/resolve_world_entity を通る（troubleshoot は lens_service 独自の安全弁で
# 内部縮退・qa/author は grep/ES のみ）ため、この例外は impact 以外では発生しない。
def _impact_overload_result(message: str, world: str, scope_meta: dict | None) -> dict:
    """固定文言のエンベロープ（LLM合成なし）。空/部分結果を事実として LLM に渡すと「確実な波及は無い」
    という偽陰性の文言を生成し得るため、ここで完結させる（events/get_provider の外側の骨組みが
    そのまま保存・監査できる `_result` 形＝正常系と同じ形で返す）。
    """
    sm = layer_mod.scope_with_layer(scope_meta, world=world, lens="impact")
    env = {"lens": "impact", "headline": GRAPH_OVERLOAD_USER_MESSAGE, "summary": {"total": 0},
           "data": {}, "sources": [], "scope": sm,
           "route": {"lens": "impact", "reason": "Neo4j 安全弁（timeout/緊急天井）",
                     "path": _ROUTE_PATH.get("impact", [])}}
    decision = {"lens": "impact", "input": message, "reason": "Neo4j 安全弁（timeout/緊急天井）"}
    return {"env": env, "decision": decision}


def _degrade_overload(gen, message: str, world: str, scope_meta: dict | None):
    """provider.run() のイテレーション中に `GraphQueryOverloadError` が飛んだら、固定文言の
    `_result` イベントへ差し替えて終端する（handle_message/stream_message の共通ラッパー）。

    例外は `providers/base.py::_gather` 内の `ctx.dispatch(...)`（＝ここでの `_dispatch` の impact
    分岐）から上がる。`_GenProvider.run`/`HeuristicProvider.run` いずれも `_gather` の呼び出しを
    ラップしていない（`_env` を受け取る前に例外が伝播する）ため、ここで捕まえた時点で以降の
    LLM 事実合成（`_answer_prompt`・`_facts`）は一度も実行されていない＝偽陰性の温床を断てる。
    """
    try:
        yield from gen
    except GraphQueryOverloadError as e:
        _log.warning("impact 経路が Neo4j 安全弁で縮退（fail-loud・reason=%s・world=%s）", e.reason, world)
        yield {"type": "_result", **_impact_overload_result(message, world, scope_meta)}


def _clip_history_msg(text: str) -> str:
    """履歴の1メッセージを上限文字数で切り詰める（先頭を残し末尾を落とす・R1a）。"""
    t = text or ""
    if len(t) <= _HISTORY_MSG_CHARS:
        return t
    return t[:_HISTORY_MSG_CHARS] + "…（省略）"


def _history_pairs(conversation_id) -> list[dict]:
    """直近ターンの (user, assistant) 完全対を Ctx.history 形式で返す（R1a）。

    会話は交互とは限らない（途中停止＝assistant 未保存・clarify・crash 補填）ため、user 行の
    直後（id 順で次）が assistant 行のときだけ対として採用する（不対行は捨てる＝anthropic の交互
    制約・gemini の role 制約に対して安全）。二重キャップ: 直近 `_HISTORY_TURNS` 対（対数）＋
    `_HISTORY_CHAR_BUDGET`（文字予算・新しい対から積み、超える対は捨てる）。メッセージ単体も
    `_HISTORY_MSG_CHARS` で切り詰める。呼び出しは **`store.add_message(現在の user)` より前**が
    契約（in-flight の質問を履歴に含めない＝呼び出し側 handle_message/stream_message 参照）。
    conversation_id が None、`_HISTORY_TURNS <= 0`（履歴 priming を無効化する設定）、または
    読み取り失敗時は `[]` に degrade する（履歴が読めなくても本回答は止めない・fail-open）。
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
            # 固定窓の弱点: 途中停止など不対行が堆積すると、古い完全対が窓の外に押し出され
            # 「直近 N 完全対」に届かないことがある。取得行数が limit に張り付いている
            # （＝窓の外にまだ古い行が残っている可能性がある）間は窓を段階的に広げて再取得する。
            # 512 行で打ち切り（priming は best-effort・全履歴走査はしない。それでも N 対に
            # 満たない場合はあるだけ返す）。
            if len(pairs) >= _HISTORY_TURNS or len(rows) < limit or limit >= 512:
                break
            limit = min(512, limit * 4)
        pairs = pairs[-_HISTORY_TURNS:]                    # 直近 N 対（対数キャップ）
        kept = []
        budget = _HISTORY_CHAR_BUDGET
        for u, a in reversed(pairs):                       # 新しい対から積む
            u_txt, a_txt = _clip_history_msg(u["content"]), _clip_history_msg(a["content"])
            cost = len(u_txt) + len(a_txt)
            if cost > budget:                               # 文字予算超過＝この対（＝これより古い対も）は捨てる
                break
            budget -= cost
            kept.append((u_txt, a_txt))
        out: list[dict] = []
        for u_txt, a_txt in reversed(kept):                 # 時系列順（古→新）に戻す
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


# Feature B: 個人ファイル参照の許可拡張子（workspace upload 許可と同じ集合・api.py _WORKSPACE_SEARCHABLE_EXT と同義）。
# ここで重複定義するのは chat_service が api.py に依存しないようにするため。個人領域は共有 KB の
# アナライザ登録簿とは別の独立集合（grep のみ・RAG/グラフ非対象）だが、コード分は和集合に含める
# （登録簿を上書きはしない＝ここでしか使わない .sql/.py/.sh/.bat 等はそのまま残す・§2.4）。
_PERSONAL_SEARCHABLE_EXT = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".sql", ".py", ".sh", ".bat",
} | _analyzer_registry.registered_extensions()


def _personal_grep_hits(user_id: str, query: str, users_dir: str) -> list[dict]:
    """ユーザーの個人 workspace を台帳基準で grep し、ヒット一覧を返す（Feature B）。

    不変条件:
    - 検索は personal_workspace_files 台帳上の status='uploaded' ファイルのみ（FS 残骸を拒否）。
    - base は users_dir / uid / workspace / files に閉じ込める（symlink・パストラバーサル拒否）。
    - ES/Neo4j・共有 KB には一切触れない。他ユーザーの uid は引数で分離されているので越境不可。
    """
    if not query or not query.strip():
        return []
    q = query.strip()
    q_lower = q.lower()
    files_dir = (Path(users_dir).resolve() / user_id / "workspace" / "files")
    # BLOCKER 2 fix: files/ ディレクトリ自体が symlink の場合は拒否（confinement 破壊防止）。
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
        try:
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
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
    """個人ファイルのヒットを LLM への事実テキストに整形（Feature B）。

    不変条件: このテキストは AI への入力のみ。ES/Neo4j には書かない。
    """
    if not hits:
        return ""
    parts = []
    for h in hits[:8]:
        parts.append(f"[個人ファイル: {h['rel_path']} 行{h['line']}] {h['text'][:200]}")
    return "\n【個人ファイル内ヒット（本人のみ参照可・共有不可）】\n" + "\n".join(parts)


def _personal_citations(hits: list[dict]) -> list[dict]:
    """個人ファイルのヒットを citation 形式に変換（Feature B）。

    不変条件: `source` フィールドで共有 KB citation と区別。DL リンクなし（個人 workspace 専用）。
    """
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


def handle_message(session, message, world="v1",
                   conversation_id=None, user_id="admin", scope_paths=None, layer=None, lens=None,
                   knowledge=False, personal=False, users_dir="data/users", web_search=False,
                   depth_profile=None, tools=None, tools_availability=None,
                   provider=None, settings=None, sys_settings=None) -> dict:
    """1ターン処理（非ストリーミング）: 会話を用意→保存→振り分け→実行→答えを保存して返す。

    `knowledge=False`（既定）＝ナレッジ参照オフ＝検索せず素の会話。`True` で社内資料を参照（レンズ＋出典）。
    `scope_paths`（版内パスの集合）を渡すと、検索/分析を**その範囲（＋共通領域）に絞る**（C・knowledge時のみ）。
    `layer`（省略可・既定 `None`＝`"both"`・knowledge時のみ）: 探す対象（調べ方ブロック §3.4）。
    `lens`（省略可・既定 `None`＝自動・knowledge時のみ）: 調べ方ブロックの明示指定（SC-6b §3.1）。
    メッセージ先頭のスラッシュ接頭辞（1回限りの明示）はこの値より優先する（`_resolve_lens` 参照）。
    `personal=True`（Feature B）＝共有 KB に加え本人の個人ファイルも grep して事実+引用に含める。
    不変条件: 個人ファイルは ES/Neo4j に入れない。本人のみ参照可。OFF 時は従来どおり。
    `web_search=False`（既定・WEB-1）: このチャットで Codex の Web 検索を希望するか
    （`ChatReq.web_search`）。保存済みの個人設定 `codex_web_search` 列は実行には使わず、この
    引数だけを見る（`settings["codex_web_search"]` をこの値で上書きしてから provider を選ぶ）。
    `depth_profile`（省略可・既定 `None`＝`"standard"`・knowledge時のみ・SC-6c §3.2）: 調べる深さ
    （調べ方ブロック）。`_dispatch()`/agentic 探索の反復・ヒット上限・探索深さ・Codex 推論に倍率で効く。
    `tools`（省略可・既定 `None`＝全 ON・knowledge時のみ・SC-6e §3.6）: 検索経路トグル
    （`ChatReq.tools`）。エージェント探索（LLM の tool-use）が提示する grep/es_search/graph_neighbors
    を絞る。Codex 頭脳は自前でシェルを実行するため対象外（`sherpa/providers/codex/provider.py` は
    無改修）。
    `tools_availability`（省略可・既定 `None`＝本関数が自分で計算・SC-6e）: 呼び出し元
    （`routers/chat.py`）がこのターンの受付時422判定（`_validate_tools_availability`）と同時に
    計算した可用性 snapshot。渡された場合はそちらを使い、本関数では再計算しない——別々に
    取得すると TTL キャッシュの境界を挟んで受付時と実行時の可用性が食い違い得るため。省略時
    （直接呼び出す単体テスト等）は従来どおり自分で `agentic_search.tool_availability()` を呼ぶ。

    `provider`/`settings`/`sys_settings`（省略可・既定 `None`＝本関数が自分で用意する）: 呼び出し元
    （`routers/chat.py`）が受付段階（`_agentic_target_check`→`tool_availability`→422判定）で
    既に組み立てた同一の Provider インスタンス／ユーザ設定／システム設定スナップショットを
    そのまま実行本体まで渡す契約。省略時のみ本関数が自分で `store.get_settings`/
    `store._read_system_settings_fresh`/`get_provider` を呼ぶ（直接呼び出す単体テスト等の
    後方互換）。`provider` を渡さずに `settings`/`sys_settings` だけ渡すことはできる（`provider`
    は本関数内部で `get_provider(settings, system_settings=sys_settings)` により解決する）。
    別々に settings を読み直すと、受付時の接続先検証（SSRF チョークポイント）と実行時の
    provider が別世代の設定（鍵・接続先）を使い得る。
    """
    _t0 = time.monotonic()   # 1ターンの所要時間の起点（answer.duration_ms へ埋め込む）。
    explicit_lens, lens_source, lens_block, message = _resolve_lens(lens, message)
    conversation_id = _ensure_conversation(conversation_id, message, world, user_id)
    # R1a: 履歴は**現在の質問を保存する前**に取得する（in-flight の質問を履歴に含めない）。
    history = _history_pairs(conversation_id)
    # R1b（Codex ネイティブ resume）: 直近ターンで捕捉済みの codex_session_id があれば CodexProvider に
    # 渡す（resume 判定用・他 provider は無視）。history と同じく質問保存より前に読む。
    codex_session_id = store.get_session_id(conversation_id)
    # RV BLOCKER: トグル ON のターンは、**保存時点で**質問を個人扱いにし、**provider 実行前に**会話も個人扱いにする
    #   （in-flight で共有されても質問が漏れない／clarify で _result に至らなくても未マークにならない）。
    _user_msg = store.add_message(conversation_id, "user", message, personal=personal)
    if personal:
        store.set_contains_personal_workspace(conversation_id)
    known = _known_terms(session, world) if knowledge else []   # オフ時は Neo4j も触らない
    scope_meta = (_resolve_scope(message, world, scope_paths, layer, lens_source, lens_block, web_search,
                                 depth_profile, tools)
                 if knowledge else None)  # 明示＞推定＞全体（D）
    # SC-6e: settings/sys_settings は呼び出し元（routers/chat.py）が受付段階で既に読んだ
    # スナップショットをそのまま使う（省略時のみここで読む・単体テスト等の後方互換）。
    settings = settings if settings is not None else store.get_settings(user_id)
    # WEB-1: 実行に使う web_search は保存済み個人設定でなくこのチャットの希望のみ（ローカル複製
    # だけを上書き・DB へは書き戻さない＝`_select_provider` の `s.get("codex_web_search")` 読み取り
    # 経路をそのまま再利用する）。呼び出し元が既に同じ上書きを済ませた settings を渡していても
    # 冪等（同じ値を重ねて上書きするだけ）。
    settings = {**settings, "codex_web_search": bool(web_search)}
    # WEB-1 の唯一の読取点（get_provider）と同じ fresh snapshot を _dispatch（調べる深さ・
    # SC-6c §3.2）へも共有する: 決定的レンズと agentic 経路が別世代の system_settings を
    # 見ないようにする。読み取り失敗はそのまま例外として伝播し、このターンを fail-closed にする
    # （WEB-1 の既存契約と同じ・env フォールバックへは広げない）。
    sys_settings = sys_settings if sys_settings is not None else store._read_system_settings_fresh()
    # SC-6e: 非agentic経路（_dispatch/_gather）が使う実効ツール判定用の可用性スナップショット。
    # 引数で渡されていれば（呼び出し元が受付時422判定と同時に計算済み）それを使い、無ければ
    # ここで1回だけ計算する（`_dispatch` 自体は DB/ネットワーク非依存の単体テスト対象のまま
    # 維持する）。knowledge オフは不要。
    if tools_availability is None:
        tools_availability = agentic_search.tool_availability() if knowledge else None

    # Feature B: 個人ファイルを grep して事実テキスト/citation を準備（ON かつファイルが存在する場合）。
    # 不変条件: grep は本人 uid の workspace 配下のみ。ES/Neo4j には書かない。
    personal_hits: list[dict] = []
    if personal:
        personal_hits = _personal_grep_hits(user_id, message, users_dir)

    def _dispatch_with_personal(lens, inp):
        """共有 KB dispatch の結果に個人ヒットを注入する（Feature B）。"""
        env = _dispatch(session, lens, inp, world, scope_meta, sys_settings, tools_availability)
        if personal_hits:
            # 個人ヒットを facts に追記（AI への入力のみ・非永続化）。
            env["_personal_facts"] = _personal_facts(personal_hits, inp)
            # 個人 citation を別枠で追加（共有 KB citation とは分離）。
            env.setdefault("personal_sources", []).extend(_personal_citations(personal_hits))
        return env

    ctx = Ctx(message=message, world=world, pace=0, knowledge=knowledge,  # 非ストリーミングは間を置かない
              route=_build_router(known, world, settings, can_ask=False, user_id=user_id,
                                  explicit_lens=explicit_lens, scope_meta=scope_meta),   # 非対話＝clarify 不可→qa fallback
              dispatch=_dispatch_with_personal if personal else
                       (lambda lens, inp: _dispatch(session, lens, inp, world, scope_meta, sys_settings,
                                                    tools_availability)),
              scope_meta=scope_meta,
              make_sources=((lambda docs: _sources(docs, world)) if knowledge else None),
              uid=user_id,
              # HIGH 1 fix: agentic/plain 経路にも個人ヒットを伝搬（_dispatch_with_personal が呼ばれない経路用）。
              personal_facts=_personal_facts(personal_hits, message) if personal_hits else "",
              # R1a: 直前ターンの (user, assistant) 対（message には混ぜない・別チャネル）。
              # R1b: conversation_id/codex_session_id は CodexProvider の resume 判定に使う。
              history=history, conversation_id=conversation_id, codex_session_id=codex_session_id,
              # SC-6e: ターン先頭で1回だけ計算した可用性 snapshot を provider まで渡す。
              tools_availability=tools_availability)
    # S3: stream_message と同じく node を id で dedup 蓄積し、trace として保存する（非ストリーミング経路の対称）。
    trace_nodes: dict = {}
    result = None
    # SC-6e: 呼び出し元が既に組み立てた Provider（受付段階で _agentic_target_check→
    # tool_availability を済ませた同一インスタンス）があればそれを使う——ここで改めて
    # get_provider() を呼ぶと、受付時と実行時で（admin 保存が挟まった場合）別世代の
    # settings/sys_settings から別の Provider を構築しうる。
    _provider = provider if provider is not None else get_provider(settings, system_settings=sys_settings)
    for ev in _degrade_overload(_provider.run(ctx), message, world, scope_meta):
        if ev["type"] == "_result":
            result = ev
            break
        if ev.get("type") == "node" and ev.get("id"):
            trace_nodes[ev["id"]] = ev
    env = _finalize(result["env"], result["decision"])
    _pop_evidence_committed(env, trace_nodes)   # _result のサイドカーを trace へ折り込む（孤児イベント防止）
    env["trace_version"] = 2
    # R1b: CodexProvider が捕捉/更新した session id を返してきたら会話に永続化する（次ターンの resume 用）。
    # fail-open（保存に失敗しても本ターンの回答自体は成立させる＝次回は resume 不可のまま priming に委ねる）。
    _codex_sid = env.get("codex_session_id")
    if _codex_sid:
        try:
            store.set_session_id(conversation_id, _codex_sid)
        except Exception as e:
            _log.warning("codex session id 保存に失敗（fail-open・次回は resume 不可で priming 継続）: %s", e)

    # Feature B: 個人 citation を answer envelope に統合（「個人ファイル内ヒット」ラベル付き）。
    # RV r2 MEDIUM: busy 応答（Codex 直列化で実行しなかったターン）には添付しない＝
    # 実行していない回答に個人ファイル抜粋を永続・表示しない（personal トグルの個人扱い自体は下で維持）。
    _used_personal = False
    if personal_hits and not env.get("busy"):
        env["personal_sources"] = _personal_citations(personal_hits)
        _used_personal = True

    # Feature C: Codex がファイルを書いた場合も contains_personal_workspace を立てる。
    if env.get("codex_wrote_files"):
        _used_personal = True
    # RV: 個人参照トグル ON のターンは、hit が無くても質問にファイル名等が残り得るため個人扱いにする
    #   （toggle ON no-hit の漏洩を塞ぐ・sanitized で伏字＋通常共有をブロック）。
    if personal:
        _used_personal = True

    # BLOCKER-1 fix: 個人コンテンツを使った場合は assistant message 保存の BEFORE にフラグを立てる。
    # フラグ書き込みに失敗したら例外を再 raise（fail-closed）し、個人内容を含む回答を保存しない。
    if _used_personal:
        store.set_contains_personal_workspace(conversation_id)
        store.set_message_personal(_user_msg["id"])   # sanitized share: このターンの質問も個人扱い

    env["duration_ms"] = round((time.monotonic() - _t0) * 1000)
    msg = store.add_message(conversation_id, "assistant", env["headline"],
                            lens=result["decision"]["lens"], route=env["route"], answer=env,
                            trace=_cap_trace_v2(trace_nodes),
                            personal=_used_personal)
    _audit_chat_turn(user_id, conversation_id, settings, lens=result["decision"]["lens"],
                     user_msg_id=_user_msg["id"], assistant_msg_id=msg["id"], world=world,
                     scope_paths=(scope_meta or {}).get("scope_paths"), personal=_used_personal)

    return {"conversation_id": conversation_id, "message": msg}


def stream_message(session, message, world="v1",
                   conversation_id=None, user_id="admin", scope_paths=None, layer=None, lens=None,
                   knowledge=False, personal=False, users_dir="data/users", stop_event=None,
                   on_user_saved=None, web_search=False, depth_profile=None, tools=None,
                   tools_availability=None, provider=None, settings=None, sys_settings=None):
    """思考イベントを逐次 yield（SSE）。**頭脳は provider（差し替え可能）**、UI/プロトコルは不変。

    provider（heuristic/codex/openai/ollama）が `node`（動的に何個でも）を流し、最後に内部 `_result`。
    本関数は会話の用意・永続だけを担い、`_result` を `answer` イベントに変換して返す（agents.py 参照）。
    `knowledge=False`（既定）＝検索せず素の会話。`True` で社内資料を参照（C: `scope_paths` で範囲を絞る）。
    `layer`（省略可・既定 `None`＝`"both"`・knowledge時のみ）: 探す対象（調べ方ブロック §3.4）。
    `lens`（省略可・既定 `None`＝自動・knowledge時のみ）: 調べ方ブロックの明示指定（SC-6b §3.1）。
    メッセージ先頭のスラッシュ接頭辞（1回限りの明示）はこの値より優先する（`_resolve_lens` 参照）。
    `personal=True`（Feature B）＝本人の個人ファイルも grep して事実+引用に含める。
    不変条件: 個人ファイルは ES/Neo4j に入れない。本人のみ参照可。OFF 時は従来どおり。
    `web_search=False`（既定・WEB-1）: このチャットで Codex の Web 検索を希望するか（`handle_message`
    と同じ契約・保存済み個人設定 `codex_web_search` 列は実行には使わない）。
    `depth_profile`（省略可・既定 `None`＝`"standard"`・knowledge時のみ・SC-6c §3.2）:
    `handle_message` と同じ契約（調べる深さ）。
    `tools`（省略可・既定 `None`＝全 ON・knowledge時のみ・SC-6e §3.6）: `handle_message` と同じ契約
    （検索経路トグル）。
    `tools_availability`（省略可・既定 `None`）: `handle_message` と同じ契約——省略時のみ本関数が
    自分で `agentic_search.tool_availability()` を計算する。
    `provider`/`settings`/`sys_settings`（省略可・既定 `None`）: `handle_message` と同じ契約
    （呼び出し元が受付段階で組み立てた同一の Provider/設定スナップショットをそのまま使う）。

    `stop_event`（UI フィードバック1・途中停止）: セットされたら以降のイベントは `{"type":"stopped"}` に
    差し替えて即 return する。**assistant メッセージは保存しない**（user メッセージは冒頭で保存済みなので、
    次の質問はそのまま会話を続けられる＝実装として最も素直＝provider ループを単に途中で抜けるだけでよい）。

    `on_user_saved(message_id, personal)`（省略可）: このターンの user 行を保存した直後に同期で
    1回だけ呼ぶ。呼び出し元がこの値を自分のクロージャに保持しておけば、この後 provider 実行中に
    例外が起きても「どの user 行が自分のターンのものか」を本文一致で推測せずに済む（同一利用者が
    同文で2ターンを並走させた場合、本文一致だけでは別ターンの行と取り違える・
    `routers/chat.py::_persist_turn_crash` 参照）。yield イベント（SSE プロトコル）とは別チャネル
    のため、既存の呼び出し元（`on_user_saved` を渡さない）には一切影響しない。
    """
    _t0 = time.monotonic()   # 1ターンの所要時間の起点（answer.duration_ms へ埋め込む・途中停止は未保存＝計測対象外）。
    explicit_lens, lens_source, lens_block, message = _resolve_lens(lens, message)
    conversation_id = _ensure_conversation(conversation_id, message, world, user_id)
    # R1a: 履歴は**現在の質問を保存する前**に取得する（in-flight の質問を履歴に含めない）。
    history = _history_pairs(conversation_id)
    # R1b（Codex ネイティブ resume）: 直近ターンで捕捉済みの codex_session_id があれば CodexProvider に
    # 渡す（resume 判定用・他 provider は無視）。history と同じく質問保存より前に読む。
    codex_session_id = store.get_session_id(conversation_id)
    # RV BLOCKER: トグル ON のターンは、**保存時点で**質問を個人扱いにし、**provider 実行前に**会話も個人扱いにする
    #   （in-flight で共有されても質問が漏れない／clarify で _result に至らなくても未マークにならない）。
    _user_msg = store.add_message(conversation_id, "user", message, personal=personal)
    if on_user_saved is not None:
        on_user_saved(_user_msg["id"], personal)
    if personal:
        store.set_contains_personal_workspace(conversation_id)
    # フロントの階層描画（サブエージェント レーン・集約表示）は trace_version=2 のターンにだけ
    # 適用する契約——`env["trace_version"]` はターン終了直前（`_result` 到達時）にしか分からず、
    # ライブ配信中は判別できない。ストリーム先頭で1回だけ軽量なマーカーを流し、フロントが
    # 最初の受信時点で判別できるようにする
    # （`chat_turns.TurnBuffer` は payload の中身を一切解釈しないままだが、先頭イベントを位置だけで
    # 保護する汎用ポリシー（`chat_turns.py::TurnBuffer.append` 参照）を持つため、このマーカーが
    # バッファの件数/バイト上限で後から間引かれることはない・未知の `type` を無視する既存フロントにも
    # 無害＝§2.3 と同じ加算的拡張）。会話作成・質問保存（副作用）の**後**に出す＝この観測用マーカーの
    # 追加で「クライアントへ何か送る前に会話/質問が確定している」という既存の境界を変えない
    # （早期切断時に質問未保存のまま何かが配信済みになる事故を避ける）。
    yield {"type": "trace_meta", "trace_version": 2}
    known = _known_terms(session, world) if knowledge else []   # オフ時は Neo4j も触らない
    scope_meta = (_resolve_scope(message, world, scope_paths, layer, lens_source, lens_block, web_search,
                                 depth_profile, tools)
                 if knowledge else None)  # 明示＞推定＞全体（D）
    # SC-6e: settings/sys_settings は呼び出し元（routers/chat.py）が受付段階で既に読んだ
    # スナップショットをそのまま使う（省略時のみここで読む・単体テスト等の後方互換）。
    settings = settings if settings is not None else store.get_settings(user_id)
    # WEB-1: 実行に使う web_search は保存済み個人設定でなくこのチャットの希望のみ（ローカル複製
    # だけを上書き・DB へは書き戻さない＝`_select_provider` の `s.get("codex_web_search")` 読み取り
    # 経路をそのまま再利用する）。呼び出し元が既に同じ上書きを済ませた settings を渡していても
    # 冪等（同じ値を重ねて上書きするだけ）。
    settings = {**settings, "codex_web_search": bool(web_search)}
    # WEB-1 の唯一の読取点（get_provider）と同じ fresh snapshot を _dispatch（調べる深さ・
    # SC-6c §3.2）へも共有する: 決定的レンズと agentic 経路が別世代の system_settings を
    # 見ないようにする。読み取り失敗はそのまま例外として伝播し、このターンを fail-closed にする
    # （WEB-1 の既存契約と同じ・env フォールバックへは広げない）。
    sys_settings = sys_settings if sys_settings is not None else store._read_system_settings_fresh()
    # SC-6e: 非agentic経路（_dispatch/_gather）が使う実効ツール判定用の可用性スナップショット
    # （`handle_message` と同じ契約——引数で渡されていればそれを使い、無ければここで計算する）。
    if tools_availability is None:
        tools_availability = agentic_search.tool_availability() if knowledge else None

    # Feature B: 個人ファイルを grep して事実テキスト/citation を準備（ON かつファイルが存在する場合）。
    # 不変条件: grep は本人 uid の workspace 配下のみ。ES/Neo4j には書かない。
    personal_hits: list[dict] = []
    if personal:
        personal_hits = _personal_grep_hits(user_id, message, users_dir)

    def _dispatch_with_personal(lens, inp):
        """共有 KB dispatch の結果に個人ヒットを注入する（Feature B）。"""
        env = _dispatch(session, lens, inp, world, scope_meta, sys_settings, tools_availability)
        if personal_hits:
            env["_personal_facts"] = _personal_facts(personal_hits, inp)
            env.setdefault("personal_sources", []).extend(_personal_citations(personal_hits))
        return env

    ctx = Ctx(
        message=message, world=world, pace=emit_pace(), knowledge=knowledge,
        route=_build_router(known, world, settings, can_ask=True, user_id=user_id,
                            explicit_lens=explicit_lens, scope_meta=scope_meta),   # ストリーミング＝曖昧なら clarify で確認
        dispatch=_dispatch_with_personal if personal else
                 (lambda lens, inp: _dispatch(session, lens, inp, world, scope_meta, sys_settings,
                                              tools_availability)),
        scope_meta=scope_meta,
        make_sources=((lambda docs: _sources(docs, world)) if knowledge else None),
        uid=user_id,
        # HIGH 1 fix: agentic/plain 経路にも個人ヒットを伝搬。
        personal_facts=_personal_facts(personal_hits, message) if personal_hits else "",
        stop_event=stop_event,
        # R1a: 直前ターンの (user, assistant) 対（message には混ぜない・別チャネル）。
        # R1b: conversation_id/codex_session_id は CodexProvider の resume 判定に使う。
        history=history, conversation_id=conversation_id, codex_session_id=codex_session_id,
        # SC-6e: ターン先頭で1回だけ計算した可用性 snapshot を provider まで渡す。
        tools_availability=tools_availability,
    )
    # S3: 「思考の流れ」を messages.trace に保存し、会話ロード時に右ペインへ静的復元できるようにする
    #   （node は id 単位で複数回更新され得るので id で dedup・最終状態のみ保持＝dict は挿入順を保つので
    #   初出順のまま最新状態で並ぶ）。question は #flow の対象外（別カードで表示）なので trace に含めない。
    trace_nodes: dict = {}
    # SC-6e: 呼び出し元が既に組み立てた Provider（受付段階で _agentic_target_check→
    # tool_availability を済ませた同一インスタンス）があればそれを使う（handle_message と同じ理由）。
    _provider = provider if provider is not None else get_provider(settings, system_settings=sys_settings)
    for ev in _degrade_overload(_provider.run(ctx), message, world, scope_meta):
        if stop_event is not None and stop_event.is_set():
            # provider が停止要求を受けて（CodexProvider は購読プロセスを kill・他は次の yield で気づく）
            # 何らかのイベント（_result 含む）を返してきても、それは保存しない＝assistant は永続しない。
            # RV MEDIUM（2026-07-03再検証）: clarify と同格に監査へ残す（assistant 未保存＝message_id_assistant=None・
            # stopped:true）。停止前は監査から丸ごと消えていた＝「誰が何を聞いて途中で止めたか」が追えなかった。
            _audit_chat_turn(user_id, conversation_id, settings, lens="stopped",
                             user_msg_id=_user_msg["id"], assistant_msg_id=None, world=world,
                             scope_paths=(scope_meta or {}).get("scope_paths"), personal=personal,
                             stopped=True)
            yield {"type": "stopped", "conversation_id": conversation_id}
            return
        if ev["type"] == "_result":
            env = _finalize(ev["env"], ev["decision"])
            # _result のサイドカーを trace へ折り込む（孤児イベント防止・下で永続化後にライブ配信もする）。
            _ev_committed_node = _pop_evidence_committed(env, trace_nodes)
            env["trace_version"] = 2
            # R1b: CodexProvider が捕捉/更新した session id を会話に永続化する（次ターンの resume 用）。
            # fail-open（保存に失敗しても本ターンの回答自体は成立させる＝次回は resume 不可のまま priming に委ねる）。
            _codex_sid = env.get("codex_session_id")
            if _codex_sid:
                try:
                    store.set_session_id(conversation_id, _codex_sid)
                except Exception as e:
                    _log.warning("codex session id 保存に失敗（fail-open・次回は resume 不可で priming 継続）: %s", e)

            # Feature B: 個人 citation を answer envelope に統合。
            # RV r2 MEDIUM: busy 応答には添付しない（非ストリーミング側と対・理由はそちらのコメント参照）。
            _used_personal = False
            if personal_hits and not env.get("busy"):
                env["personal_sources"] = _personal_citations(personal_hits)
                _used_personal = True

            # Feature C: Codex がファイルを書いた場合も contains_personal_workspace を立てる。
            if env.get("codex_wrote_files"):
                _used_personal = True
            # RV: 個人参照トグル ON のターンは hit が無くても質問にファイル名等が残り得るため個人扱い。
            if personal:
                _used_personal = True

            # BLOCKER-1 fix: 個人コンテンツを使った場合は assistant message 保存の BEFORE にフラグを立てる。
            # フラグ書き込みに失敗したら例外を再 raise（fail-closed）し、個人内容を含む回答を保存しない。
            if _used_personal:
                store.set_contains_personal_workspace(conversation_id)
                store.set_message_personal(_user_msg["id"])   # sanitized share: このターンの質問も個人扱い

            env["duration_ms"] = round((time.monotonic() - _t0) * 1000)
            msg = store.add_message(conversation_id, "assistant", env["headline"],
                                    lens=ev["decision"]["lens"], route=env["route"], answer=env,
                                    trace=_cap_trace_v2(trace_nodes),
                                    personal=_used_personal)
            _audit_chat_turn(user_id, conversation_id, settings, lens=ev["decision"]["lens"],
                             user_msg_id=_user_msg["id"], assistant_msg_id=msg["id"], world=world,
                             scope_paths=(scope_meta or {}).get("scope_paths"), personal=_used_personal)

            # 永続化（store.add_message）が成功した後にだけライブ配信する——`_result` と不可分な
            # サイドカーとして扱う本来の目的どおり、保存が確定してから初めて画面に見せる。
            if _ev_committed_node is not None:
                yield _ev_committed_node
            yield {"type": "answer", "conversation_id": conversation_id, "message": msg}
        elif ev["type"] == "question":
            # S1（ask_user-improvements.md）: 確認カードを assistant メッセージとして**永続化**する
            #   ＝ページを離れて履歴を開き直しても後から答えられる（従来は監査記録＋素通しのみで、
            #   自分の質問文だけ残り確認カードは消えていた）。content=prompt / answer に question payload /
            #   trace はここまでに溜めた思考ノード（clarify ターンでも「思考の流れ」を右ペインへ静的復元
            #   できる副産物）。両経路（通常 /chat/stream・背景 /chat/turns=覗き窓のバッファ経由）とも
            #   本関数 stream_message を通るため、ここ1箇所の保存で両方に効く。
            #   personal トグル ON のターンは、質問 prompt に個人ヒットの断片が混ざり得るため個人扱いで
            #   保存する（会話フラグ set_contains_personal_workspace は冒頭で設定済み・_user_msg も
            #   personal=personal で保存済み＝sanitized/通常共有で伏せられる）。
            question_payload = {k: v for k, v in ev.items() if k != "type"}
            question_payload.setdefault("original_message", message)   # 再送フォーマット（元の依頼）に使う
            q_answer = {"lens": "clarify", "question": question_payload, "trace_version": 2}
            q_answer["duration_ms"] = round((time.monotonic() - _t0) * 1000)
            q_msg = store.add_message(conversation_id, "assistant", ev.get("prompt") or "",
                                      lens="clarify",
                                      answer=q_answer,
                                      trace=_cap_trace_v2(trace_nodes),
                                      personal=personal)
            # 監査は現行どおり lens="clarify"。assistant_msg_id は保存した確認カードの message id に変える。
            _audit_chat_turn(user_id, conversation_id, settings, lens="clarify",
                             user_msg_id=_user_msg["id"], assistant_msg_id=q_msg["id"], world=world,
                             scope_paths=(scope_meta or {}).get("scope_paths"), personal=personal)
            yield {**ev, "conversation_id": conversation_id, "original_message": message}
        else:
            if ev.get("type") == "node" and ev.get("id"):
                trace_nodes[ev["id"]] = ev
            yield ev
