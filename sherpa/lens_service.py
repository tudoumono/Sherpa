"""別レンズ probe（薄い・read-only・鏡モデル）。

同じ取り込み済み world グラフ＋文書＋grep を**再利用**して、業務テーマを別レンズでも問える:
- **トラブルシュート**（`troubleshoot`）= 近傍（`INVOKES`/`RELATES_TO`/`DOCUMENTS` 含む）＋運用手順 grep → 原因候補カード。
- **仕様問い合わせ**（`qa`）= grep（仕様書）→ 該当節を引用（doc_id＝rel_path＋span）。

厳密な依存影響は impact レンズ（impact_service／world_neo4j）。本モジュールは**近傍探索**で、影響に乗らない
`DOCUMENTS`/`RELATES_TO` も辿る。範囲は world＋`scope_prefixes`（フォルダ prefix）で絞る（MIRROR §3）。
特定テーマの名前はコードに持たない（起点/検索語は入力から）。
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

# 近傍たどりのエッジ＝実効語彙（K13・2026-09-04-グラフのソース正典化.md §4）。
_RELATED_REL = "COPIES|CONTAINS|INVOKES|ACCESSES|DOCUMENTS|CORRESPONDS_TO"

_ROLE = {"Document": "関連文書", "Module": "実装", "Batch": "ジョブ"}
_ROLE_RANK = {"Document": 1, "Batch": 2, "Module": 3}


# secRV 範囲外是正（2026-07-19・Neo4j 安全弁＝timeout＋緊急天井・LIMITは入れない＝網羅性の原則維持がユーザー決定）:
# 影響検索（近傍探索）は網羅性が本質のため Cypher に LIMIT は入れない。密なグラフ（ハブノード）で
# 深さ3の可変長パス展開が数千〜数万件になり得ることへは、代わりに (1) per-query タイムアウト、
# (2) 結果カーソルをストリーム反復して行数に緊急天井（正当な結果では届かない値＝フィルタではない）
# の二段の**安全弁**で可用性を守る。件数を意味的に絞ることは一切しない。
def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    """security-limit 系 env の整数解析（`agentic_search._env_int` と同一セマンティクス）。

    ここに複製する理由: `agentic_search` は本モジュール（`lens_service`）を `graph_neighbors` ツール
    実装から**遅延 import**している（循環回避・agentic_search.py 431行目付近）。lens_service は
    「薄い・read-only」層として agentic_search より下位に位置づく設計のため、ここで
    `from .agentic_search import _env_int` と top-level import すると層が逆転する。関数自体は
    6行の極小ヘルパーなので、同型のものをここに複製する（セマンティクスは完全に同一）。

    負値/非整数、および範囲 [lo, hi] 外の値は既定へフォールバックする（既定値自体も [lo, hi] へ
    クランプ）。運用者の誤設定で機能を止めない（起動は継続）。
    """
    default = max(lo, min(default, hi))
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return v if lo <= v <= hi else default


# per-query タイムアウト（秒）。既定30・[1,600] にクランプ（secRV 範囲外是正・2026-07-19）。
_NEO4J_QUERY_TIMEOUT_S = _env_int("SHERPA_NEO4J_QUERY_TIMEOUT_S", 30, 1, 600)
# ストリーム反復の緊急天井（行数）。既定10000・[100,1000000] にクランプ（同上）。
_NEO4J_MAX_ROWS = _env_int("SHERPA_NEO4J_MAX_ROWS", 10000, 100, 1_000_000)
# トラブルシュート/近傍探索（neo4j_related）の既定深さ。範囲外・不正値は既定3へ復帰（[1,16]）。
TROUBLESHOOT_GRAPH_DEPTH = _env_int("SHERPA_TROUBLESHOOT_GRAPH_DEPTH", 3, 1, 16)
# env-parse hi 引数と同じ値。調べる深さ（`depth_profile.scaled_depth`）が加算適用後に一度だけ
# 適用する絶対上限として使う（`impact_service.IMPACT_MAX_DEPTH_ABS_MAX` と同じ理由）。
TROUBLESHOOT_GRAPH_DEPTH_ABS_MAX = 16

# タイムアウト由来のサーバエラーコードを緩く判定する（専用の例外クラスが無いため）。実例:
# `Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration`（クライアント指定タイムアウト）。
# サーバ版で分類（ClientError/TransientError）が揺れ得るため、`code` 文字列に "timeout"/"timedout" を
# 含むかで判定する（大小文字無視）。
_TIMEOUT_CODE_RE = re.compile(r"timedout|timeout", re.IGNORECASE)


def _is_query_timeout(exc: Neo4jError) -> bool:
    """Neo4j サーバエラーが**クエリ/トランザクションのタイムアウト**によるものか判定する。"""
    code = getattr(exc, "code", "") or ""
    return bool(_TIMEOUT_CODE_RE.search(str(code)))


def _run_capped(session, cypher: str, *, log_world: str, **params) -> list[dict]:
    """読み取り専用 Cypher を安全弁つきで実行する（secRV 範囲外是正・2026-07-19）。

    - `neo4j.Query(cypher, timeout=...)` で **per-query タイムアウト**を付与する。サーバがタイムアウトで
      クエリを打ち切ると `Neo4jError` が上がるが、ここで捕捉して黙殺せず `log.warning`（world とタイムアウト
      秒を含む）を出したうえで**空リストへ縮退**させる（呼び出し元 resolve_anchor/neo4j_related/
      run_troubleshoot は本関数内で完結して縮退するため、チャット/troubleshoot/影響分析いずれの経路でも
      未処理の 500 やハングにならない）。タイムアウト以外の `Neo4jError`（Cypher バグ等）は再送出する。
    - `.data()` による結果の一括メモリ展開はやめ、結果カーソルを**ストリームで反復**しながら収集し、
      `_NEO4J_MAX_ROWS` 行に達したら打ち切る（正当な結果では到達しない値＝フィルタではなく緊急天井の
      メモリ保護）。天井到達時も `log.warning`（world と件数）を必ず出す＝黙って削らない。収集済み分は
      そのまま返す。
    - Cypher 本文に LIMIT は入れない（影響検索の網羅性を落とさない＝ユーザー決定）。

    secRV 範囲外是正 追補（2026-07-19・RV指摘 HIGH-1）: 天井到達で `break` した時点では `Result`
    カーソルがまだ未消費（残りレコードが手元に残っている）。neo4j driver 6.2.0 は**同じ session**で
    次の `session.run()` を呼ぶと、前の未消費 `Result` の残りを**全件 fetch/buffer してしまう**
    （`run_troubleshoot` が `resolve_anchor` 直後に同一 session で `neo4j_related` を呼ぶ経路では、
    天井で守ったはずのメモリ保護がここで逆流する）。`result.consume()` は残りのレコードをバッファ
    せずストリームを流し捨てる正規の破棄手段なので、`break` の前に必ず呼ぶ。
    """
    query = Query(cypher, timeout=_NEO4J_QUERY_TIMEOUT_S)
    try:
        result = session.run(query, **params)
        rows: list[dict] = []
        for i, record in enumerate(result):
            if i >= _NEO4J_MAX_ROWS:
                _log.warning("neo4j 読み取りが緊急天井 %d 行に達したため打ち切り（world=%s）",
                            _NEO4J_MAX_ROWS, log_world)
                result.consume()   # 残り未消費のまま返さない（次クエリでの全件バッファ逆流を防ぐ）
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
    """レンズ共通の出力エンベロープ。MVP の type は `impact`／`troubleshoot`／`qa`。"""
    if type_ not in ("impact", "troubleshoot", "qa", "doc_check"):
        raise ValueError(f"unknown result type: {type_}")
    return {"type": type_, "world": world, **payload}


def _truncated_search_note(doc_ids: list) -> str | None:
    """`grep_search(truncated_docs=...)` の申告（cap で打ち切られた文書の doc_id・ヒット0件の
    打切りも含む）→ 利用者向け平文の注記1件。打切りが無ければ `None`（呼び出し元はこのとき
    `notes` キー自体を作らない＝`usage_chat.answer_usage_question` の `notes: list[str]`
    （画面へそのまま見せる注記）と同じ流儀・加算的変更）。

    内部語彙（`file_truncated`／cap／バイト）は出さない（docs/04-画面の原則.md の平文原則）。
    """
    if not doc_ids:
        return None
    if len(doc_ids) == 1:
        return f"「{doc_ids[0]}」は大きすぎて全体を検索できていません（先頭部分のみ）。"
    shown = "」「".join(doc_ids[:5])
    more = f" ほか{len(doc_ids) - 5}件" if len(doc_ids) > 5 else ""
    return f"次の資料は大きすぎて全体を検索できていません（先頭部分のみ）: 「{shown}」{more}"


def _terms(text: str) -> list[str]:
    """検索語を区切り＋汎用の助詞/問い掛け語で素朴に分割（テーマ非依存）。高度抽出は LLM 層（Phase）。"""
    import re
    sep = (r"[\s、。，．・/:：;；,.!?！？「」『』（）()\[\]【】〜~\-]"
           r"|について|という|とは|どう\w*|なに|なん\w*|教えて\w*|でしょうか|ですか"
           r"|から|まで|より|の|は|を|に|が|で|と|も|へ|や|か|ね|よ")
    parts = re.split(rf"(?:{sep})+", text or "")
    return [t for t in (p.strip() for p in parts) if len(t) >= 2]


def _public_grep(world, hits):
    """grep ヒットから API 露出用の根拠だけ残す（物理 `path` は出さない・整形は citations に集約）。

    H3（SC-4 接続・CITE-1）: `text` は `excerpts.display_quote` で利用者向けに人間向け MD の該当節へ
    引き直す（grep が rag.md を読んだ場合の「AI用KV文がそのまま出る」問題への対処・§9）。対応が
    取れない（legacy md 由来／locator 対応表を引けない等）ときは元の grep 本文のまま
    （`excerpt_source` は常に付与するが、locator_hint は取れたときだけ）。
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


def _name_of(cid: str) -> str:
    """canonical_id → 表示名（`...#NAME` / DataItem は修飾名の末端）。"""
    return cid.rsplit("#", 1)[-1].split(".")[-1]


def resolve_anchor(session, text, world, scope_prefixes=None):
    """症状文 → グラフ上のアンカー `[(cid, name)]`（**入力に現れるノード名**で素朴に同定・範囲内）。"""
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
    """アンカー近傍（**厳密な影響ではない**）。各近傍への最短経路を代表に返す（範囲内・無向・全エッジ型）。

    rv-s3-removal: `anchors` が非空（＝実際にグラフへ問い合わせる）のときだけ、主クエリの**後**に
    `check_schema_era` を呼ぶ（旧世代の実データがある world は `GraphSchemaEraError` で fail-loud・
    `world_neo4j` 参照）。主クエリの安全弁（`_run_capped` の timeout 縮退）を先に効かせるための順序
    （`check_schema_era` 自身も世代プローブの Neo4jError は黙ってスキップするため、主クエリが既に
    縮退した後にここで新たな失敗にはならない）。`lens="troubleshoot"`——直接 dispatch（troubleshoot
    レンズ）経由の呼び出しを想定した既定値。agentic ツール `graph_neighbors`（`neighbor_cards` 経由・
    qa/author レンズからも呼ばれ得る）でも同じ既定値のまま返す（呼び出し元のチャットレンズまでは
    本関数から見えないため・新規のレンズ追跡機構は作らない＝縮小形の方針）。
    """
    if not anchors:
        return []
    cy = (
        "MATCH (a:Entity) WHERE a.canonical_id IN $anchors "
        f"MATCH p=(a)-[r:{_RELATED_REL}*1..%(d)d]-(nb:Entity) "
        f"WHERE nb.world_id=$world AND nb.canonical_id <> a.canonical_id "
        f"  AND all(n IN nodes(p) WHERE n.world_id=$world AND {_scope_pred('n')}) "   # 経路全体を範囲内に（scope 外を経由させない）
        "  AND ($incl OR all(n IN nodes(p) WHERE coalesce(n.status,'active')='active')) "
        "  AND ($incl OR all(e IN relationships(p) WHERE coalesce(e.status,'active')='active')) "
        "WITH nb, p ORDER BY length(p) "
        "WITH nb, head(collect(p)) AS path "
        "RETURN nb.canonical_id AS cid, nb.name AS name, "
        "  [l IN labels(nb) WHERE l<>'Entity'][0] AS label, "
        "  coalesce(nb.extraction_method,'static') AS em, "
        "  coalesce(nb.status,'active') AS status, "
        "  [n IN nodes(path) | n.name] AS path_names, "
        "  [e IN relationships(path) | {type:type(e), doc:e.doc}] AS edges, "
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
    """症状 → 原因候補カード（内部専用・`cid` 付き）。`run_troubleshoot`（公開・`cid` 除去）と
    `neighbor_cards`（agentic `graph_neighbors` 専用・`cid` 保持）が共有する実装。この関数の戻り値を
    直接 API・非 agentic 会話保存・sanitized 共有・JSON 書き出しへ渡さない（内部専用フィールドが
    漏れる）。

    戻り値は3-tuple `(anchor_names, cards, truncated_docs)`。`truncated_docs`（`grep_search` の
    out-param・アンカー名ごとの grep 呼び出し全体で重複なく蓄積）は `run_troubleshoot` が平文の
    注記に翻訳する（`_truncated_search_note` 参照）。`neighbor_cards`（agentic 経路）はこの3件目を
    無視する——agentic 経路は `ripgrep_search` ツール自身が別途 `truncated_docs` を申告する
    （`agentic_search.py`）ため、ここで二重に伝える必要が無い。
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
        if nb["label"] == "DataItem":            # 末端の項目は粒度が細かすぎる（影響レンズで見る）
            continue
        card_docs = {e.get("doc") for e in nb["edges"] if e.get("doc")}   # この候補の来歴 rel_path
        gh = [h for d in card_docs for h in grep_by_doc.get(d, [])]       # grep 根拠は rel_path で結合
        covered_docs |= {d for d in card_docs if grep_by_doc.get(d)}
        cards.append({
            "name": nb["name"], "label": nb["label"], "category": nb["category"],
            "role": _ROLE.get(nb["label"], "近傍"), "distance": nb["distance"], "path": nb["path"],
            "source": "both" if gh else "graph",
            "evidence": {"edges": nb["edges"], "grep": _public_grep(world, gh)},
            "cid": nb["cid"],   # 内部専用: Neo4j canonical_id（label+world+path+name の同一性・
                                # MIRROR-MODEL §2.1）。同名 label/name でも別ノードを区別するための
                                # 機械キー——`neighbor_cards`（agentic 経路）だけが受け取る。公開
                                # 経路（`run_troubleshoot`）は返す前に除去する。
        })
    for doc_id, gh in grep_by_doc.items():       # グラフに無いが grep だけで出た文書（運用手順など）
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
    """症状 → 原因候補カード（近傍グラフ＋運用手順 grep・根拠つき）。範囲は grep＋候補(根拠 doc)を絞る。

    公開経路（直接 API・非 agentic 会話保存・sanitized 共有・JSON 書き出し）——内部専用 `cid`
    （Neo4j canonical_id）は含まない。`cid` 付きの card が要る呼び出し元（agentic ツール
    `graph_neighbors`）は `neighbor_cards` を使う。

    運用手順 grep が打ち切られた文書があれば `notes`（平文の注記1件・`_truncated_search_note`）を
    添える（打切りが無ければ `notes` キー自体を作らない＝加算的変更）。
    """
    anchor_names, cards, truncated_docs = _troubleshoot_cards(session, symptom, world, depth, include_deprecated, scope_paths)
    public_cards = [{k: v for k, v in c.items() if k != "cid"} for c in cards]
    result = emit_result("troubleshoot", world, symptom=symptom,
                         anchors=sorted(anchor_names), candidates=public_cards)
    note = _truncated_search_note(truncated_docs)
    if note:
        result["notes"] = [note]
    return result


def neighbor_cards(world, term, scope_paths=None) -> list:
    """関係グラフの**近傍カード**（原因候補）を返す。agentic ツール `graph_neighbors` 専用＝AI が
    「何を調べるか」を決めてグラフを引けるよう、`_troubleshoot_cards` を**自前で Neo4j セッションを
    開いて**呼ぶ薄いラッパ。`run_troubleshoot`（公開・`cid` 除去済み）は経由しない——`cid` 付きの
    card をそのまま返す（agentic_search 側の構造 Evidence 生成が使う・公開経路には出さない）。
    Neo4j 不可・未解決はグレースフルに `[]`（agentic はツール結果が空でも他の道具で続行できる）。

    rv-s3-removal: **`GraphSchemaEraError` だけは再送出**する（`impact_service.presumed_impact` の
    `GraphQueryOverloadError` 特別扱いと同じ流儀）——旧世代の実データを黙って空カードへ縮退させると
    「見つからなかった」（本当に0件）と「読めない世代のグラフ」を区別できず、チャット側
    （`chat_service._degrade_overload`）が honest failure を出す機会を失う。
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
            # 3件目（truncated_docs）は無視: agentic 経路は `ripgrep_search` ツール自身が
            # 別途 truncated_docs を申告する（`_troubleshoot_cards` docstring 参照）。
            _anchor_names, cards, _truncated_docs = _troubleshoot_cards(s, term, world, scope_paths=scope_paths)
        return cards
    except GraphSchemaEraError:
        raise
    except Exception:
        return []
    finally:
        if driver is not None:
            try:
                driver.close()
            except Exception:
                pass


def run_qa(question, world, max_hits=20, scope_paths=None, layer=None):
    """検索語 → 仕様書の該当節を引用（doc_id＝rel_path＋span）。範囲（フォルダ prefix）で引用源を絞る。

    `layer`（省略可・既定 `None`＝`"both"`）: 探す対象（調べ方ブロック §3.4）。`grep_search` へ
    そのまま転送する（qa レンズには層フィルタが実効する・§3.5）。

    grep が打ち切られた文書があれば `notes`（平文の注記1件・`_truncated_search_note`）を添える
    （1発目・語分割フォールバックの全呼び出しを通じて重複なく蓄積・打切りが無ければ `notes`
    キー自体を作らない＝加算的変更）。
    """
    sp = scope.normalize_scope_paths(scope_paths) or None
    truncated_docs: list = []
    hits = grep_search(question, world, max_hits=max_hits, scope_paths=sp, layer=layer,
                       truncated_docs=truncated_docs)
    if not hits:                                  # 自然文 → 語に分割し具体的な語から試す
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
    # H3（SC-4 接続・CITE-1）: quote を利用者向けに人間向け MD の該当節へ引き直す（match/ext は
    # 維持・整形は citations に集約）。対応が取れないときは grep 本文（rag.md 優先時は AI 用 KV 文の
    # ことがある）のままフォールバックする（§9 契約）。
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
