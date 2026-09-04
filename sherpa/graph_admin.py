"""管理画面向けグラフ操作（read-only）。

Neo4j の world グラフを直接読む検索と、既存 agentic_search の graph_neighbors
ツールだけを使う自然言語質問をまとめる。書き込みは行わない。
"""
from __future__ import annotations

import logging

from . import agent_constructs, agentic_search, llm, store
from .impact_service import CATEGORY
from .ingest.model import NODE_LABELS
from .ingest.world_neo4j import (
    GRAPH_SCHEMA_ERA_USER_MESSAGE,
    WORLD_EDGE_TYPES,
    GraphSchemaEraError,
    _scope_pred,
    check_schema_era,
)
from .preview_service import _TYPE_JA

_log = logging.getLogger("sherpa")

_COND_FIELDS = {
    "category": lambda v: f"{v}.category",
    "phase": lambda v: f"{v}.phase",
    "top_scope": lambda v: f"{v}.top_scope",
    "path": lambda v: f"{v}.path",
    "status": lambda v: f"coalesce({v}.status,'active')",
    "extraction_method": lambda v: f"coalesce({v}.extraction_method,'static')",
    "em": lambda v: f"coalesce({v}.extraction_method,'static')",
    "type": lambda v: f"[l IN labels({v}) WHERE l<>'Entity'][0]",
    "label": lambda v: f"[l IN labels({v}) WHERE l<>'Entity'][0]",
    # グラフ内に role プロパティはないため、管理検索ではノード種別を role として扱う。
    "role": lambda v: f"[l IN labels({v}) WHERE l<>'Entity'][0]",
}
_OPS = {"eq", "contains", "prefix"}
_GRAPH_SYSTEM = (
    "あなたは管理者向けのナレッジ状況調査アシスタントです。"
    "答える対象は取り込み済み資料、グラフノード、エッジ、孤立ノード、関係の薄い領域、取り込みエラー、"
    "出典の偏り、追加取り込み候補などのナレッジベース状態に限定します。"
    "通常の業務仕様質問としては答えず、read-only の提案だけを返してください。"
    "利用できるグラフ情報源は graph_neighbors ツールで取得した関係グラフだけです。"
    "必要に応じて graph_neighbors を呼び、返ったノード名・関係・経路と提示された集計だけを根拠に日本語で簡潔に答えてください。"
    "グラフや集計に無いことは推測しないでください。"
)


def facets() -> dict:
    """UI が使う閉じた語彙。Neo4j に聞かずコード正典から返す。"""
    labels = sorted(NODE_LABELS)
    rels = sorted(WORLD_EDGE_TYPES)
    return {"node_labels": labels, "node_labels_ja": {l: _TYPE_JA.get(l, l) for l in labels},
            "relationship_types": rels,
            "condition_fields": ["category", "phase", "role", "top_scope", "status"]}


def _condition(var: str, field: str | None, value: str | None, op: str) -> str | None:
    if not field and not value:
        return None
    field = (field or "").strip()
    value = (value or "").strip()
    op = (op or "eq").strip()
    if field not in _COND_FIELDS:
        raise ValueError(f"unknown condition field: {field}")
    if op not in _OPS:
        raise ValueError(f"unknown condition op: {op}")
    if not value:
        raise ValueError("condition value is empty")
    expr = _COND_FIELDS[field](var)
    text = f"toString(coalesce({expr},''))"
    if op == "eq":
        return f"{text} = $cond_value"
    if op == "prefix":
        return f"toLower({text}) STARTS WITH toLower($cond_value)"
    return f"toLower({text}) CONTAINS toLower($cond_value)"


def _node(row: dict, prefix: str) -> dict | None:
    cid = row.get(prefix + "id")
    if not cid:
        return None
    label = row.get(prefix + "label")
    return {"id": cid, "name": row.get(prefix + "name"), "type": label,
            "type_ja": _TYPE_JA.get(label, label),
            "em": row.get(prefix + "em") or "static",
            "status": row.get(prefix + "status") or "active",
            "value": row.get(prefix + "value"),
            "top_scope": row.get(prefix + "top_scope"),
            "phase": row.get(prefix + "phase"),
            "category": row.get(prefix + "category"),
            "path": row.get(prefix + "path")}


def _rows_to_graph(rows: list[dict], world: str, counts: dict | None = None) -> dict:
    nodes, edges, seen_e = {}, [], set()
    for r in rows:
        for p in ("source_", "target_"):
            n = _node(r, p)
            if n:
                nodes[n["id"]] = n
        if r.get("edge_type") and r.get("source_id") and r.get("target_id"):
            key = (r["source_id"], r["target_id"], r["edge_type"])
            if key not in seen_e:
                seen_e.add(key)
                edges.append({"source": r["source_id"], "target": r["target_id"],
                              "type": r["edge_type"], "em": r.get("edge_em") or "static",
                              "status": r.get("edge_status") or "active"})
    out_nodes = sorted(nodes.values(), key=lambda n: ((n.get("type_ja") or ""), (n.get("name") or "")))
    return {"world": world, "nodes": out_nodes, "edges": edges,
            "counts": counts or {"nodes": len(out_nodes), "edges": len(edges)}}


def _ret(prefix: str, var: str) -> str:
    return (
        f"{var}.canonical_id AS {prefix}id, {var}.name AS {prefix}name, "
        f"[l IN labels({var}) WHERE l<>'Entity'][0] AS {prefix}label, "
        f"coalesce({var}.extraction_method,'static') AS {prefix}em, "
        f"coalesce({var}.status,'active') AS {prefix}status, {var}.value AS {prefix}value, "
        f"{var}.top_scope AS {prefix}top_scope, {var}.phase AS {prefix}phase, "
        f"{var}.category AS {prefix}category, {var}.path AS {prefix}path"
    )


def graph_search(session, world: str, relationship_types=None, field: str | None = None,
                 value: str | None = None, op: str = "eq", scope_paths=None,
                 include_deprecated: bool = False, limit: int = 200) -> dict:
    """関係種別/属性条件で world グラフを検索し、可視化と同じ nodes/edges 形で返す。

    rv-s3-removal: 主クエリの**後**に `check_schema_era` を呼ぶ（旧世代の実データがある world は
    `GraphSchemaEraError`・呼び出し元 `routers/graph.py::graph_search` が 503 へ変換する）。
    """
    rels = [str(r).strip().upper() for r in (relationship_types or []) if str(r).strip()]
    bad = [r for r in rels if r not in WORLD_EDGE_TYPES]
    if bad:
        raise ValueError(f"unknown relationship type: {', '.join(sorted(bad))}")
    prefixes = list(scope_paths or [])
    params = {"world": world, "prefixes": prefixes, "incl": include_deprecated,
              "cond_value": value or "", "limit": max(1, min(int(limit or 200), 1000))}

    if rels:
        rel_clause = "|".join(sorted(set(rels)))
        cond_a = _condition("a", field, value, op)
        cond_b = _condition("b", field, value, op)
        where = [
            "a.world_id=$world AND b.world_id=$world",
            _scope_pred("a"), _scope_pred("b"),
            "($incl OR (coalesce(a.status,'active')='active' AND coalesce(b.status,'active')='active' "
            "AND coalesce(r.status,'active')='active'))",
        ]
        if cond_a:
            where.append(f"({cond_a} OR {cond_b})")
        cy = (
            f"MATCH (a:Entity)-[r:{rel_clause}]->(b:Entity) "
            "WHERE " + " AND ".join(where) + " "
            "RETURN " + _ret("source_", "a") + ", " + _ret("target_", "b") + ", "
            "type(r) AS edge_type, coalesce(r.extraction_method,'static') AS edge_em, "
            "coalesce(r.status,'active') AS edge_status "
            "ORDER BY edge_type, source_name, target_name LIMIT $limit"
        )
        rows = session.run(cy, **params).data()
        check_schema_era(session, world)
        return _rows_to_graph(rows, world)

    cond_n = _condition("n", field, value, op)
    if not cond_n:
        raise ValueError("relationship_types or condition is required")
    cond_m = _condition("m", field, value, op)
    cy = (
        "MATCH (n:Entity {world_id:$world}) "
        f"WHERE {_scope_pred('n')} AND {cond_n} "
        "  AND ($incl OR coalesce(n.status,'active')='active') "
        "WITH n LIMIT $limit "
        "OPTIONAL MATCH (n)-[r]-(m:Entity {world_id:$world}) "
        f"WHERE {_scope_pred('m')} AND {cond_m} "
        "  AND ($incl OR (coalesce(m.status,'active')='active' AND coalesce(r.status,'active')='active')) "
        "RETURN " + _ret("source_", "n") + ", " + _ret("target_", "m") + ", "
        "type(r) AS edge_type, coalesce(r.extraction_method,'static') AS edge_em, "
        "coalesce(r.status,'active') AS edge_status "
        "ORDER BY source_name, edge_type, target_name"
    )
    rows = session.run(cy, **params).data()
    check_schema_era(session, world)
    return _rows_to_graph(rows, world)


def _cited_from_cards(cards: list[dict]) -> list[dict]:
    out, seen = [], set()
    for c in cards:
        key = (c.get("name"), c.get("label"), tuple(c.get("path") or []))
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": c.get("name"), "label": c.get("label"),
                    "type_ja": _TYPE_JA.get(c.get("label"), c.get("label")),
                    "role": c.get("role"), "category": c.get("category"),
                    "distance": c.get("distance"), "path": c.get("path") or [],
                    "edges": (c.get("evidence") or {}).get("edges", [])})
    return out


def _collect(events) -> tuple[str, set, list, dict | None]:
    """イベント列 → `(answer, docs, cards, usage)`。`usage`（S1）は agentic_search が final イベントに
    付与した usage メタ（無ければ None）をそのまま返す＝`kind='graph_ask'` の計測に使う。

    S1 是正: `not searched` の raise パスは、agentic_search が付与した usage 付きの final イベント自体を
    握りつぶす（LLM は1回はトークンを消費している）。呼び出し元 `ask_graph` はこの raise を except で
    拾って `{'status':'failed',...}` にするため、このパス（ツール未使用のまま応答）で消費したトークンは
    記録されない（既知の限界・S1 スコープ外＝raise を4タプルに変える改修はしない）。
    """
    answer, docs, cards, searched, usage = "", set(), [], False, None
    for ev in events:
        if "node" in ev:
            searched = True
        elif "final" in ev:
            answer = ev.get("final") or ""
            docs |= set(ev.get("docs") or [])
            cards += ev.get("cards") or []
            searched = searched or bool(ev.get("searched"))
            usage = ev.get("usage")
    if not searched:
        raise RuntimeError("graph tool was not used")
    return answer.strip(), docs, cards, usage


def _status_context_text(summary: dict | None) -> str:
    if not summary:
        return ""
    iso = ", ".join(n.get("name", "") for n in summary.get("isolated_nodes", [])[:8]) or "なし"
    weak = ", ".join(d.get("name", "") for d in summary.get("weak_documents", [])[:8]) or "なし"
    errs = ", ".join(r.get("status", "") for r in summary.get("recent_ingest_errors", [])[:5]) or "なし"
    return (
        "ナレッジ状況の集計:\n"
        f"- world: {summary.get('world')}\n"
        f"- scope_paths: {summary.get('scope_paths') or ['全体']}\n"
        f"- documents: {summary.get('documents')}\n"
        f"- graph_nodes: {summary.get('graph_nodes')}\n"
        f"- graph_edges: {summary.get('graph_edges')}\n"
        f"- isolated_nodes: {summary.get('isolated_node_count')} ({iso})\n"
        f"- weak_documents: {summary.get('weak_document_count')} ({weak})\n"
        f"- recent_ingest_errors: {errs}\n"
    )


def _llm_unavailable_answer(status_summary: dict | None) -> str:
    """AI 未接続（llm_unavailable）の応答本文。集計サマリの平文（`_summary_answer`）を、あたかも
    AI が答えたかのように差し替えない＝明示エラーを主文にし、集計は補足として続ける
    （`no_graph_evidence` と同じ「主文は正直に・補足は分けて足す」構成）。"""
    from . import keys
    msg = f"AI に接続できないため、この質問には回答できません（{keys.NO_CENTRAL_KEY_MESSAGE}）。"
    if status_summary:
        msg += "\n" + _summary_answer("", status_summary)
    return msg


_FAILED_ANSWER = "回答の生成中にエラーが発生しました。時間をおいて再度お試しください。"


def _summary_answer(q: str, summary: dict | None) -> str:
    if not summary:
        return "ナレッジ状況の集計を取得できませんでした。資料フォルダ、Neo4j、取り込み状態を確認してください。"
    weak = summary.get("weak_document_count", 0)
    isolated = summary.get("isolated_node_count", 0)
    errors = summary.get("recent_ingest_errors") or []
    parts = [
        f"対象は world={summary.get('world')} / scope={summary.get('scope_paths') or ['全体']} です。",
        f"取り込み済み文書は {summary.get('documents')} 件、グラフは {summary.get('graph_nodes')} ノード・{summary.get('graph_edges')} 関係です。",
        f"孤立ノードは {isolated} 件、関係が薄い可能性のある文書は {weak} 件です。",
    ]
    if errors:
        parts.append(f"最近の取り込みエラー候補が {len(errors)} 件あります。")
    if weak or isolated or errors:
        parts.append("弱点候補は、孤立ノード、関係が付いていない文書、失敗した取り込みrunの確認です。AIは変更せず、追加取り込みやグラフ生成の確認だけを提案します。")
    else:
        parts.append("大きな欠落候補は集計上は見えていません。重要な資料種別やスコープに偏りがないかを次に確認してください。")
    return " ".join(parts)


def ask_graph(question: str, world: str, scope_paths=None, settings: dict | None = None,
              status_summary: dict | None = None, user_id: str | None = None) -> dict:
    """管理グラフへの自然言語質問。既存 agentic_search の graph_neighbors だけを使う。

    S1（2026-07-15-LLMオーケストレーション実装計画.md §3）: `_collect` が成功した呼び出しにつき、
    `kind='graph_ask'` で1行記録する（計測有効時のみ・llm_unavailable の早期return／except→failed の
    失敗パスは記録しない）。

    `system_settings` をこの呼び出しの冒頭で1回だけ読み、agent 選択・鍵/モデル解決・送信時接続先
    解決（`llm.openai_url`/`openai_headers`）まで同じスナップショットで揃える（個別に読み直すと、
    この1回の質問応答の途中で admin 保存が挟まった場合に agent 判定とキー解決が新旧混在しうる）。
    """
    q = (question or "").strip()
    if not q:
        raise ValueError("question is empty")
    s = settings or store.get_settings()
    sys_s = store.get_system_settings()
    # effective_agent() 経由（A7 で選択中でないクラウド系 agent は ollama 扱いに統一・実行と表示/監査の
    # 単一の真実源＝sherpa/agent_constructs.py::effective_agent 参照）。実際に LLM へ送信する経路
    # （唯一の入口・providers/__init__.py::_select_provider と同じ）のため strict=True で解決する
    # （非空の不正な SHERPA_AGENT/cloud_provider を黙って別プロバイダへ倒さない）。
    from . import keys as _keys
    try:
        agent = agent_constructs.effective_agent(s, system_settings=sys_s, strict=True)
    except (agent_constructs.InvalidAgentConfigError, _keys.InvalidCloudProviderConfigError) as e:
        return {"status": "llm_unavailable", "world": world, "question": q,
               "answer": str(e), "cited_nodes": [], "docs": [], "summary": status_summary}
    # env で有効化していない外部AI（gemini/bedrock）は黙って実行せず明示的に拒否する
    # （`_select_provider` の `runtime_blocked()` チェックと同じ・欠落すると環境で無効なはずの
    # gemini/bedrock 分岐へそのまま進んで実送信してしまう）。
    if agent_constructs.runtime_blocked(agent):
        return {"status": "llm_unavailable", "world": world, "question": q,
               "answer": "この AI はこの環境では利用できません（管理者が有効化していません）。",
               "cited_nodes": [], "docs": [], "summary": status_summary}
    prompt = q
    ctx = _status_context_text(status_summary)
    if ctx:
        prompt = ctx + "\n質問: " + q
    try:
        if agent == "openai":
            key = _keys.resolve_api_key("openai", s, system_settings=sys_s)
            if not key:
                return {"status": "llm_unavailable", "world": world, "question": q,
                        "answer": _llm_unavailable_answer(status_summary),
                        "cited_nodes": [], "docs": [], "summary": status_summary}
            from . import model_catalog
            prov, mod = "openai", model_catalog.resolve_model("openai", "chat", None,
                                                               system_settings=sys_s)
            events = agentic_search.openai_style(
                llm.openai_url("chat/completions", system_settings=sys_s),
                llm.openai_headers(key, system_settings=sys_s), mod,
                _GRAPH_SYSTEM, prompt, world, scope_paths,
                toolset=agentic_search.graph_openai_tools())
        elif agent == "ollama":
            url = _keys.resolve_ollama_url(s, system_settings=sys_s)
            from . import model_catalog
            prov, mod = "ollama", model_catalog.resolve_model("ollama", "chat", None,
                                                               system_settings=sys_s)
            events = agentic_search.openai_style(
                llm.ollama_url(url.rstrip("/"), "/api/chat"), llm.JSON_HEADERS,
                mod, _GRAPH_SYSTEM, prompt, world, scope_paths,
                ollama=True, toolset=agentic_search.graph_openai_tools())
        elif agent == "gemini":
            key = _keys.resolve_api_key("gemini", s, system_settings=sys_s)
            if not key:
                return {"status": "llm_unavailable", "world": world, "question": q,
                        "answer": _llm_unavailable_answer(status_summary),
                        "cited_nodes": [], "docs": [], "summary": status_summary}
            from . import model_catalog
            prov, mod = "gemini", model_catalog.resolve_model("gemini", "chat", None,
                                                               system_settings=sys_s)
            events = agentic_search.gemini(
                key, mod, _GRAPH_SYSTEM, prompt, world, scope_paths,
                toolset=agentic_search.graph_gemini_tools())
        elif agent == "bedrock":
            from . import agents
            from anthropic import AnthropicBedrock
            from .providers.bedrock import _bedrock_runtime_base_url
            # A7（クラウドプロバイダ排他選択）の判定を `_bedrock_auth_available` の**前**で確定させる。
            # SigV4 ヒント（AWS_ACCESS_KEY_ID/AWS_PROFILE/~/.aws/credentials）は bedrock 未選択でも
            # 端末に残っていることがあるため、`_bedrock_auth_available` 単体の判定に任せず先にゲートする。
            if _keys.selected_cloud_provider(sys_s) != "bedrock":
                return {"status": "llm_unavailable", "world": world, "question": q,
                        "answer": _llm_unavailable_answer(status_summary),
                        "cited_nodes": [], "docs": [], "summary": status_summary}
            api_key = _keys.resolve_api_key("bedrock", s, system_settings=sys_s)
            if not agents._bedrock_auth_available(api_key):   # 中央/個人キーも SigV4 ヒントも無い＝未接続
                return {"status": "llm_unavailable", "world": world, "question": q,
                        "answer": _llm_unavailable_answer(status_summary),
                        "cited_nodes": [], "docs": [], "summary": status_summary}
            # api_key が None のまま SDK の env(Bearer) チェーンへ委ねない（honest failure・
            # 上の early return が担う）。SigV4/AWS 認証情報ファイルは `_bedrock_auth_available`
            # が引き続き許容する（インフラ管理の認証チェーン・env の Bearer キーだけを対象外にする）
            # ため、ここでは resolve 済みの api_key をそのまま渡す。
            prov, mod = "bedrock", s.get("bedrock_model") or agents._BEDROCK_MODEL
            # region は常に東京固定（利用者設定からは読まない・`_bedrock_region` が唯一の真実源）。
            # `base_url` を明示する（`providers/bedrock.py::_bedrock_runtime_base_url` docstring
            # 参照）: 省略すると SDK が env `ANTHROPIC_BEDROCK_BASE_URL` を読んで接続先を上書きできる。
            client = AnthropicBedrock(api_key=api_key or None, aws_region=agents._bedrock_region(),
                                      base_url=_bedrock_runtime_base_url())
            events = agentic_search.anthropic_style(
                client, mod, _GRAPH_SYSTEM, prompt, world, scope_paths,
                toolset=agentic_search.graph_openai_tools())
        else:
            return {"status": "llm_unavailable", "world": world, "question": q,
                    "answer": _llm_unavailable_answer(status_summary),
                    "cited_nodes": [], "docs": [], "summary": status_summary}
        answer, docs, cards, usage = _collect(events)
        from . import metering
        metering.record("graph_ask", prov, mod, usage, user_id=user_id, world=world)
    except GraphSchemaEraError as e:
        # RV是正（rv-periphery #12・2026-09-05）: `graph_neighbors` ツール経由で上がる専用例外を
        # 下の generic except（"failed"）へ丸めず、閉じた理由 `graph_reingest_required` を返す
        # （`ask_graph` は例外を投げず常に status dict を返す既存契約——`routers/graph.py::
        # graph_ask` はこの戻り値をそのまま返すだけで済む・503 化はしない）。
        _log.warning("graph_ask がスキーマ世代不一致で失敗（fail-loud・world=%s・stored=%s）",
                    world, e.stored_era)
        return {"status": "graph_reingest_required", "world": world, "question": q,
                "answer": GRAPH_SCHEMA_ERA_USER_MESSAGE,
                "cited_nodes": [], "docs": [], "summary": status_summary}
    except Exception:
        return {"status": "failed", "world": world, "question": q,
                "answer": _FAILED_ANSWER,
                "cited_nodes": [], "docs": [], "summary": status_summary}
    cited = _cited_from_cards(cards)
    if not cited:                                   # 根拠ノード無し＝グラフ接地できず＝LLM 文は捨てる（04-画面の原則.md §4「出典の無い断定をしない」）
        no_ev = "グラフに根拠が見つかりませんでした（確証なし）。用語や範囲を変えて試してください。"
        if status_summary:                          # 集計があれば参考情報として続ける（根拠なしの明示が先）
            no_ev += "\n" + _summary_answer(q, status_summary)
        return {"status": "no_graph_evidence", "world": world, "question": q,
                "answer": no_ev,
                "cited_nodes": [], "docs": sorted(docs), "summary": status_summary}
    return {"status": "ok", "world": world, "question": q,
            "answer": answer or "グラフから回答を作れませんでした。",
            "cited_nodes": cited, "docs": sorted(docs), "summary": status_summary}
