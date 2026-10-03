"""別レンズ probe（薄い・read-only）。取り込み済み world のグラフ・文書・grep を再利用して、業務テーマを別レンズで問う。
設計: docs/design/chat.md「1ターンの流れ」
- トラブルシュート（`troubleshoot`）: 近傍（`INVOKES`/`RELATES_TO`/`DOCUMENTS` 含む）＋運用手順 grep → 原因候補カード。
- 仕様問い合わせ（`qa`）: grep（仕様書）→ 該当節を引用（doc_id＝rel_path＋span）。
厳密な依存影響は impact レンズ（impact_service／world_neo4j）。ここは近傍探索。範囲は world＋`scope_prefixes`（フォルダ prefix）。
"""
from __future__ import annotations

import logging
import os
import re

from neo4j import Query
from neo4j.exceptions import Neo4jError

from . import citations, scope
from .grep_tool import grep_search
from .impact_service import CATEGORY
from .ingest.world_neo4j import GraphSchemaEraError, _scope_pred, check_schema_era

_log = logging.getLogger("sherpa")

# 近傍たどりのエッジ（実効語彙）。
_RELATED_REL = "COPIES|CONTAINS|INVOKES|ACCESSES|DOCUMENTS|CORRESPONDS_TO"

_ROLE = {"Document": "関連文書", "Module": "実装", "Batch": "ジョブ"}
_ROLE_RANK = {"Document": 1, "Batch": 2, "Module": 3}


# Neo4j の安全弁は timeout と緊急天井。Cypher に LIMIT は入れない（網羅性優先）。
def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    """security-limit 系 env の整数解析（`agentic_search._env_int` と同じ意味・層の逆転を避けるため複製）。範囲外・非整数は既定へ。"""
    default = max(lo, min(default, hi))
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return v if lo <= v <= hi else default


# per-query タイムアウト（秒）。既定 30・[1,600]。
_NEO4J_QUERY_TIMEOUT_S = _env_int("SHERPA_NEO4J_QUERY_TIMEOUT_S", 30, 1, 600)
# ストリーム反復の緊急天井（行数）。
_NEO4J_MAX_ROWS = 10000
# トラブルシュート/近傍探索（neo4j_related）の既定深さ（管理画面の基準値が未設定のときに使う）。
TROUBLESHOOT_GRAPH_DEPTH = 4
# 調べる深さ（`depth_profile.scaled_depth`）が加算後に一度だけ適用する絶対上限。
TROUBLESHOOT_GRAPH_DEPTH_ABS_MAX = 16

# タイムアウト由来のサーバエラーコードを緩く判定する（`code` に "timeout"/"timedout" を含むか・大小文字無視）。
_TIMEOUT_CODE_RE = re.compile(r"timedout|timeout", re.IGNORECASE)


def _is_query_timeout(exc: Neo4jError) -> bool:
    """Neo4j サーバエラーがクエリ/トランザクションのタイムアウトによるものか判定する。"""
    code = getattr(exc, "code", "") or ""
    return bool(_TIMEOUT_CODE_RE.search(str(code)))


def _run_capped(session, cypher: str, *, log_world: str, **params) -> list[dict]:
    """読み取り専用 Cypher を安全弁つきで実行する。
    ① `neo4j.Query(cypher, timeout=...)` で per-query タイムアウトを付ける。タイムアウトの `Neo4jError` は `log.warning` を出して空リストへ縮退する（他の `Neo4jError` は再送出）。
    ② 結果カーソルをストリーム反復し、`_NEO4J_MAX_ROWS` 行で打ち切る（`log.warning` を出す・収集済み分は返す）。
    ③ 打ち切りの `break` の前に `result.consume()` を呼ぶ（同じ session の次の `run()` が残りを全件バッファするのを防ぐ）。
    Cypher に LIMIT は入れない。
    """
    query = Query(cypher, timeout=_NEO4J_QUERY_TIMEOUT_S)
    try:
        result = session.run(query, **params)
        rows: list[dict] = []
        for i, record in enumerate(result):
            if i >= _NEO4J_MAX_ROWS:
                _log.warning("neo4j 読み取りが緊急天井 %d 行に達したため打ち切り（world=%s）",
                            _NEO4J_MAX_ROWS, log_world)
                result.consume()  # 残り未消費のまま返さない
                break
            rows.append(record.data())
        return rows
    except Neo4jError as e:
        if _is_query_timeout(e):
            _log.warning("neo4j クエリがタイムアウト（%ss）のため空へ縮退（world=%s）: %s",
                        _NEO4J_QUERY_TIMEOUT_S, log_world, e)
            return []
        raise


def emit_result(type_: str, world: str, **payload) -> dict:
    """レンズ共通の出力エンベロープ。type は `impact`／`troubleshoot`／`qa`。"""
    if type_ not in ("impact", "troubleshoot", "qa", "doc_check"):
        raise ValueError(f"unknown result type: {type_}")
    return {"type": type_, "world": world, **payload}


def _truncated_search_note(doc_ids: list) -> str | None:
    """`grep_search(truncated_docs=...)`（cap で打ち切られた文書の doc_id）→ 利用者向け平文の注記 1 件。打切りが無ければ `None`（`notes` キーを作らない）。
    内部語彙（`file_truncated`／cap／バイト）は出さない。
    """
    if not doc_ids:
        return None
    if len(doc_ids) == 1:
        return f"「{doc_ids[0]}」は大きすぎて全体を検索できていません（先頭部分のみ）。"
    shown = "」「".join(doc_ids[:5])
    more = f" ほか{len(doc_ids) - 5}件" if len(doc_ids) > 5 else ""
    return f"次の資料は大きすぎて全体を検索できていません（先頭部分のみ）: 「{shown}」{more}"


def _terms(text: str) -> list[str]:
    """検索語を区切り＋汎用の助詞/問い掛け語で素朴に分割する（テーマ非依存）。"""
    import re
    sep = (r"[\s、。，．・/:：;；,.!?！？「」『』（）()\[\]【】〜~\-]"
           r"|について|という|とは|どう\w*|なに|なん\w*|教えて\w*|でしょうか|ですか"
           r"|から|まで|より|の|は|を|に|が|で|と|も|へ|や|か|ね|よ")
    parts = re.split(rf"(?:{sep})+", text or "")
    return [t for t in (p.strip() for p in parts) if len(t) >= 2]


def _public_grep(world, hits):
    """grep ヒットから API 露出用の根拠だけ残す（物理 `path` は出さない・整形は citations）。
    `text` は `excerpts.display_quote` で人間向け MD の該当節へ引き直す（取れなければ元の grep 本文）。
    """
    from . import excerpts
    out = []
    for h in hits:
        pub = citations.public_grep_hit(h)
        disp = excerpts.display_quote(world, pub["doc_id"], pub["text"], span=pub.get("span"))
        out.append(citations.with_display_text(
            pub, text=disp["quote"], excerpt_source=disp["excerpt_source"],
            locator_hint=disp["locator_hint"]))
    return out


def resolve_anchor(session, text, world, scope_prefixes=None):
    """症状文 → グラフ上のアンカー `[(cid, name)]`（入力に現れるノード名で同定・範囲内）。"""
    rows = _run_capped(
        session,
        f"MATCH (n:Entity {{world_id:$w}}) WHERE {_scope_pred('n')} "
        "RETURN DISTINCT n.canonical_id AS cid, n.name AS name",
        log_world=world, w=world, prefixes=list(scope_prefixes or []),
    )
    low = (text or "").lower()
    return [(r["cid"], r["name"]) for r in rows
            if r["name"] and len(r["name"]) >= 2 and r["name"].lower() in low]


def neo4j_related(session, anchors, world, scope_prefixes=None, depth=TROUBLESHOOT_GRAPH_DEPTH, include_deprecated=False):
    """アンカー近傍（厳密な影響ではない）。各近傍への最短経路を代表に返す（範囲内・無向探索・全エッジ型）。
    `edges` は各辺を `{type, from, to, doc}` で返す（`from`/`to` はグラフ上の実際の向き）。
    主クエリの後に `check_schema_era` を呼ぶ（旧世代の実データは `GraphSchemaEraError`）。`anchors` が空でも呼ぶ。
    """
    if not anchors:
        check_schema_era(session, world, lens="troubleshoot")
        return []
    cy = (
        "MATCH (a:Entity) WHERE a.canonical_id IN $anchors "
        f"MATCH p=(a)-[r:{_RELATED_REL}*1..%(d)d]-(nb:Entity) "
        f"WHERE nb.world_id=$world AND nb.canonical_id <> a.canonical_id "
        f"  AND all(n IN nodes(p) WHERE n.world_id=$world AND {_scope_pred('n')}) "  # 経路全体を範囲内に（scope 外を経由させない）
        "  AND ($incl OR all(n IN nodes(p) WHERE coalesce(n.status,'active')='active')) "
        "  AND ($incl OR all(e IN relationships(p) WHERE coalesce(e.status,'active')='active')) "
        "WITH nb, p ORDER BY length(p) "
        "WITH nb, head(collect(p)) AS path "
        "RETURN nb.canonical_id AS cid, nb.name AS name, "
        "  [l IN labels(nb) WHERE l<>'Entity'][0] AS label, "
        "  coalesce(nb.extraction_method,'static') AS em, "
        "  coalesce(nb.status,'active') AS status, "
        "  [n IN nodes(path) | n.name] AS path_names, "
        "  [e IN relationships(path) | {type:type(e), from:startNode(e).name, to:endNode(e).name, doc:e.doc}] AS edges, "
        "  length(path) AS dist"
    ) % {"d": int(depth)}
    out = []
    rows = _run_capped(session, cy, log_world=world, anchors=list(anchors), world=world,
                       prefixes=list(scope_prefixes or []), incl=include_deprecated)
    check_schema_era(session, world, lens="troubleshoot")
    for r in rows:
        out.append({
            "cid": r["cid"], "name": r["name"], "label": r["label"],
            "category": CATEGORY.get(r["label"], r["label"]),
            "extraction_method": r["em"], "status": r["status"],
            "path": r["path_names"], "distance": r["dist"], "edges": list(r["edges"]),
        })
    return out


def _troubleshoot_cards(session, symptom, world, depth=TROUBLESHOOT_GRAPH_DEPTH, include_deprecated=False, scope_paths=None):
    """症状 → 原因候補カード（内部専用・`cid` 付き）。`run_troubleshoot`（`cid` 除去）と `neighbor_cards`（agentic `graph_neighbors`・`cid` 保持）が共有する。
    戻り値を直接 API・会話保存・共有・JSON 書き出しへ渡さない（内部専用フィールドが漏れる）。
    戻り値は `(anchor_names, cards, truncated_docs)`。`truncated_docs` は `run_troubleshoot` が平文の注記にする（agentic 経路は無視）。
    """
    sp = scope.normalize_scope_paths(scope_paths) or None
    pairs = resolve_anchor(session, symptom, world, sp)
    anchors = [c for c, _n in pairs]
    anchor_names = {n for _c, n in pairs}
    related = neo4j_related(session, anchors, world, sp, depth, include_deprecated)

    grep_by_doc: dict[str, list] = {}
    truncated_docs: list = []
    for nm in anchor_names:
        for h in grep_search(nm, world, scope_paths=sp, truncated_docs=truncated_docs):
            grep_by_doc.setdefault(h["doc_id"], []).append(h)

    cards: list[dict] = []
    covered_docs: set[str] = set()
    for nb in related:
        if nb["label"] == "DataItem":  # 末端の項目は粒度が細かすぎる（影響レンズで見る）
            continue
        card_docs = {e.get("doc") for e in nb["edges"] if e.get("doc")}  # この候補の来歴 rel_path
        gh = [h for d in card_docs for h in grep_by_doc.get(d, [])]  # grep 根拠は rel_path で結合
        covered_docs |= {d for d in card_docs if grep_by_doc.get(d)}
        cards.append({
            "name": nb["name"], "label": nb["label"], "category": nb["category"],
            "role": _ROLE.get(nb["label"], "近傍"), "distance": nb["distance"], "path": nb["path"],
            "source": "both" if gh else "graph",
            "evidence": {"edges": nb["edges"], "grep": _public_grep(world, gh)},
            "cid": nb["cid"],  # 内部専用: Neo4j canonical_id
                                # 同名 label/name の別ノードを区別する機械キー。公開経路（`run_troubleshoot`）は返す前に除去する。
        })
    for doc_id, gh in grep_by_doc.items():  # グラフに無いが grep だけで出た文書（運用手順など）
        if doc_id in covered_docs:
            continue
        cards.append({
            "name": doc_id, "label": "Document", "category": CATEGORY["Document"],
            "role": _ROLE["Document"], "distance": None, "path": [],
            "source": "grep", "evidence": {"edges": [], "grep": _public_grep(world, gh)},
        })

    cards.sort(key=lambda c: (_ROLE_RANK.get(c["label"], 9),
                              c["distance"] if c["distance"] is not None else 99, c["name"]))
    cards = scope.filter_items(cards, sp)
    return anchor_names, cards, truncated_docs


def run_troubleshoot(session, symptom, world, depth=TROUBLESHOOT_GRAPH_DEPTH, include_deprecated=False, scope_paths=None):
    """症状 → 原因候補カード（近傍グラフ＋運用手順 grep・根拠つき）。範囲で grep と候補を絞る。
    公開経路のため内部専用 `cid` は含まない（`cid` 付きは `neighbor_cards`）。
    打ち切られた文書があれば `notes`（平文の注記 1 件）を添える。
    """
    anchor_names, cards, truncated_docs = _troubleshoot_cards(session, symptom, world, depth, include_deprecated, scope_paths)
    public_cards = [{k: v for k, v in c.items() if k != "cid"} for c in cards]
    result = emit_result("troubleshoot", world, symptom=symptom,
                         anchors=sorted(anchor_names), candidates=public_cards)
    note = _truncated_search_note(truncated_docs)
    if note:
        result["notes"] = [note]
    return result


class NeighborCardsFailure(list):
    """`neighbor_cards` が障害を捕捉したときだけ返す空 `list` のサブクラス。`error_code` 属性で通常の空リストと区別する。"""

    def __init__(self, error_code: str):
        super().__init__()
        self.error_code = error_code


def neighbor_cards(world, term, scope_paths=None) -> list:
    """関係グラフの近傍カード（原因候補）を返す。agentic ツール `graph_neighbors` 専用で、自前で Neo4j セッションを開いて `_troubleshoot_cards` を呼ぶ（`cid` 付き）。
    Neo4j 不可・未解決は `[]`。捕捉した障害は `NeighborCardsFailure`（`error_code`）で返す:
    接続系（`DriverError`/`TransientError`）は `"graph_unavailable"`（回復可能）、それ以外は `"graph_internal_error"`（回復不可）。型名だけログに残す。
    `GraphSchemaEraError` だけは再送出する（呼び出し元が `graph_reingest_required` に変換）。
    """
    if not (term or "").strip():
        return []
    driver = None
    try:
        from neo4j import GraphDatabase

        from .ingest import world_neo4j
        env = world_neo4j._env()
        driver = GraphDatabase.driver(env["uri"], auth=(env["user"], env["pw"]))
        with driver.session() as s:
            # 3 件目（truncated_docs）は無視（agentic 経路は `ripgrep_search` が別途申告する）。
            _anchor_names, cards, _truncated_docs = _troubleshoot_cards(s, term, world, scope_paths=scope_paths)
        return cards
    except GraphSchemaEraError:
        raise
    except Exception as exc:
        from neo4j.exceptions import ConfigurationError, DriverError, TransientError
        # `ConfigurationError` は `DriverError` のサブクラスだが回復不可として扱う。
        recoverable = not isinstance(exc, ConfigurationError) and isinstance(exc, (DriverError, TransientError))
        _log.warning("lens_service: neighbor_cards 取得に失敗（回復%s）: %s errno=%s",
                    "可" if recoverable else "不可", type(exc).__name__, getattr(exc, "errno", None))
        return NeighborCardsFailure("graph_unavailable" if recoverable else "graph_internal_error")
    finally:
        if driver is not None:
            try:
                driver.close()
            except Exception:
                pass


def _resolve_anchor_by_name(session, term, world, scope_prefixes=None):
    """起点をグラフから名前の一致で直接引く（素の Codex モード専用）。完全一致が 0 件のときだけ大文字小文字無視の一致を試す。範囲の述語は `resolve_anchor` と同じ。"""
    sp = list(scope_prefixes or [])
    rows = _run_capped(
        session,
        f"MATCH (n:Entity {{world_id:$w}}) WHERE {_scope_pred('n')} AND n.name = $term "
        "RETURN DISTINCT n.canonical_id AS cid, n.name AS name",
        log_world=world, w=world, prefixes=sp, term=term,
    )
    if not rows:
        rows = _run_capped(
            session,
            f"MATCH (n:Entity {{world_id:$w}}) WHERE {_scope_pred('n')} AND toLower(n.name) = toLower($term) "
            "RETURN DISTINCT n.canonical_id AS cid, n.name AS name",
            log_world=world, w=world, prefixes=sp, term=term,
        )
    return [(r["cid"], r["name"]) for r in rows]


def neighbor_cards_graph_only(world, term, scope_paths=None) -> list:
    """`neighbor_cards` のグラフだけ版（素の Codex モード）。grep はせず、起点は `_resolve_anchor_by_name`、近傍は `neo4j_related`。
    `evidence.grep` は常に空・`source` は常に `"graph"`。戻り値・失敗の扱いは `neighbor_cards` と同じ（`GraphSchemaEraError` は再送出）。
    """
    if not (term or "").strip():
        return []
    driver = None
    try:
        from neo4j import GraphDatabase

        from .ingest import world_neo4j
        env = world_neo4j._env()
        driver = GraphDatabase.driver(env["uri"], auth=(env["user"], env["pw"]))
        sp = scope.normalize_scope_paths(scope_paths) or None
        with driver.session() as s:
            pairs = _resolve_anchor_by_name(s, term, world, sp)
            anchors = [c for c, _n in pairs]
            related = neo4j_related(s, anchors, world, sp)
        cards: list[dict] = []
        for nb in related:
            if nb["label"] == "DataItem":  # 末端の項目は粒度が細かすぎる（影響レンズで見る）
                continue
            cards.append({
                "name": nb["name"], "label": nb["label"], "category": nb["category"],
                "role": _ROLE.get(nb["label"], "近傍"), "distance": nb["distance"], "path": nb["path"],
                "source": "graph",
                "evidence": {"edges": nb["edges"], "grep": []},
                "cid": nb["cid"],
            })
        cards.sort(key=lambda c: (_ROLE_RANK.get(c["label"], 9),
                                  c["distance"] if c["distance"] is not None else 99, c["name"]))
        return scope.filter_items(cards, sp)
    except GraphSchemaEraError:
        raise
    except Exception as exc:
        from neo4j.exceptions import ConfigurationError, DriverError, TransientError
        recoverable = not isinstance(exc, ConfigurationError) and isinstance(exc, (DriverError, TransientError))
        _log.warning("lens_service: neighbor_cards_graph_only 取得に失敗（回復%s）: %s errno=%s",
                    "可" if recoverable else "不可", type(exc).__name__, getattr(exc, "errno", None))
        return NeighborCardsFailure("graph_unavailable" if recoverable else "graph_internal_error")
    finally:
        if driver is not None:
            try:
                driver.close()
            except Exception:
                pass


def run_qa(question, world, max_hits=20, scope_paths=None, layer=None):
    """検索語 → 仕様書の該当節を引用（doc_id＝rel_path＋span）。範囲（フォルダ prefix）で引用源を絞る。
    `layer`（省略可・`None`＝`"both"`）は探す対象で、`grep_search` へそのまま渡す。
    打ち切られた文書があれば `notes`（平文の注記 1 件）を添える。
    """
    sp = scope.normalize_scope_paths(scope_paths) or None
    truncated_docs: list = []
    hits = grep_search(question, world, max_hits=max_hits, scope_paths=sp, layer=layer,
                       truncated_docs=truncated_docs)
    if not hits:  # 自然文 → 語に分割し具体的な語から試す
        for t in sorted(_terms(question), key=len, reverse=True):
            found, seen = [], set()
            for h in grep_search(t, world, max_hits=max_hits, scope_paths=sp, layer=layer,
                                 truncated_docs=truncated_docs):
                key = (h["doc_id"], tuple(h["span"]))
                if key not in seen:
                    seen.add(key)
                    found.append(h)
            if found:
                hits = found
                break
    # quote を人間向け MD の該当節へ引き直す（match/ext は維持）。取れなければ grep 本文のまま。
    from . import excerpts
    cites = []
    for h in hits:
        c = citations.from_grep_hit(h)
        disp = excerpts.display_quote(world, h["doc_id"], c["quote"], span=h.get("span"))
        cites.append(citations.with_display_excerpt(
            c, quote=disp["quote"], excerpt_source=disp["excerpt_source"],
            locator_hint=disp["locator_hint"]))
    result = emit_result("qa", world, question=question,
                         answered=bool(cites), citations=cites)
    note = _truncated_search_note(truncated_docs)
    if note:
        result["notes"] = [note]
    return result
