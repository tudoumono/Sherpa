from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse


WORLD = {"world_id": "w1", "label": "4期更改", "root_path": "/mnt/c/ProjectA",
         "storage_mode": "external_reference"}


def _ann_status(a: dict) -> str:
    """S4: サーバ側 _announcement_status のミニ版（mock 専用・厳密な一致は API テスト側で担保）。"""
    if not a.get("published", True):
        return "unpublished"
    now = datetime.now(timezone.utc)
    pub_at, exp_at = a.get("publish_at"), a.get("expire_at")
    if pub_at and datetime.fromisoformat(pub_at) > now:
        return "scheduled"
    if exp_at and datetime.fromisoformat(exp_at) <= now:
        return "expired"
    return "active"

SCOPES = {
    "world": "w1",
    "label": "4期更改",
    "scopes": [
        {"path": "4期", "label": "4期", "depth": 0, "count": 4},
        {"path": "4期/02_設計", "label": "02_設計", "depth": 1, "count": 2},
        {"path": "4期/02_設計/01_基本設計", "label": "01_基本設計", "depth": 2, "count": 2},
        {"path": "4期/03_開発", "label": "03_開発", "depth": 1, "count": 2},
        {"path": "4期/03_開発/01_ソース", "label": "01_ソース", "depth": 2, "count": 2},
    ],
}

# S3: 実形状（sherpa/preview_service.py::graph_view）は世代/位置に top_scope・path を持つ（旧モックの
# "parent" キーは実 API に存在しない＝実ドリフト是正。web/graph.js の `d.parent` 参照はレガシー耐性の
# 防御コードで、未定義でも支障なし）。edges にも実際は status がある。
# ソース正典化（`docs/proposals/2026-09-04-グラフのソース正典化.md`・K9〜K13）: 意味層 LLM 抽出・
# 概念ラベル（Parameter/Function 等）・REALIZES/IMPLEMENTED_BY・em（確実/要確認）は撤去済み。
# 4ノードの旧「消費税率(Parameter)→TAX-RATE」REALIZES 橋は「税計算仕様書.md(Document) が
# DOCUMENTS(via=mention) で TAX-RATE を言及」に置換（K3 の言及エッジ）。旧「請求機能(Function・
# LLM由来)」は「請求機能(Batch) が INVOKES で TAXCALC を呼ぶ」という静的解析由来のノードに置換
# （全ノード extraction_method=static・K12）。
# 注記（並行バックエンドレーンとの突合が必要）: 実 API の counts/summary の最終形が未確定のため、
# ここでは K12 の方針どおり「count のみ・judgement/em 無し」で組んでいる。
GRAPH = {
    "world": "w1",
    "nodes": [
        {"id": "doc:w1:taxspec", "name": "税計算仕様書.md", "type": "Document", "type_ja": "文書",
         "status": "active", "value": None, "top_scope": "4期",
         "path": "4期/02_設計/01_基本設計/税計算仕様書.md"},
        {"id": "data:w1:TAX-RATE", "name": "TAX-RATE", "type": "DataItem", "type_ja": "項目",
         "status": "active", "value": None, "top_scope": "4期",
         "path": "4期/03_開発/01_ソース/TAXCALC.cbl"},
        {"id": "module:w1:TAXCALC", "name": "TAXCALC", "type": "Module", "type_ja": "プログラム",
         "status": "active", "value": None, "top_scope": "4期",
         "path": "4期/03_開発/01_ソース/TAXCALC.cbl"},
        {"id": "batch:w1:billing", "name": "請求機能", "type": "Batch", "type_ja": "バッチ",
         "status": "active", "value": None, "top_scope": "4期", "path": None},
    ],
    "edges": [
        {"source": "doc:w1:taxspec", "target": "data:w1:TAX-RATE", "type": "DOCUMENTS",
         "status": "active"},
        {"source": "module:w1:TAXCALC", "target": "data:w1:TAX-RATE", "type": "CONTAINS",
         "status": "active"},
        {"source": "batch:w1:billing", "target": "module:w1:TAXCALC", "type": "INVOKES",
         "status": "active"},
    ],
    # GraphCounts と IngestPreviewCounts は同一の `preview_service.py::_counts()` が作る同一形
    # （K12・entities_static/relations_static は残置＝全件 static なので値は entities/relations と一致）。
    "counts": {"entities": 4, "entities_static": 4, "relations": 3, "relations_static": 3,
              "deprecated": 0, "hidden": 0, "documents": 3},
    # ②graph 軽量化（段階読み込み）: 4 ノードは既定 limit に収まる＝非truncated（実 API と同じ応答形）。
    "total_nodes": 4, "total_edges": 3, "truncated": False,
}
# ETag は GRAPH 本体の内容連動（実 API の内容署名を模す・②graph 軽量化 RV是正2026-07-08 Low#3）。
_GRAPH_CONTENT_HASH = hashlib.sha1(json.dumps(GRAPH, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]

PREVIEW = {
    # S3: 実形状（sherpa/preview_service.py::build_preview）は world/label/issues も持つ
    # （旧モックは無かった＝実ドリフト是正）。issues は build_preview の `flags`（未解決の抽出警告）。
    # 実 API（doc_ledger.control_diagnostics 経由）は importance_diagnostics も常に持つ
    # （`_重要度.txt` の構文診断・無ければ空リスト）。
    "world": "w1", "label": "4期更改", "issues": [], "importance_diagnostics": [],
    # ソース正典化（K12・実 schemas.py::IngestPreviewCounts で確認済み）: entities_llm/entities_both/
    # relations_llm/relations_both は撤去済み（全件 static のため無意味）。entities_static/
    # relations_static 自体は残る（値は entities/relations と常に一致）。
    "counts": {"entities": 4, "entities_static": 4, "relations": 3, "relations_static": 3,
               "deprecated": 0, "hidden": 0, "documents": 3},
    # フェーズ7-1: 実 doc（doc_ledger.py::preview_documents）は phase/category/label/reason も常に持つ
    # （旧モックは欠落＝実ドリフト是正。reason は失敗時のみ非 None・他は None）。
    "documents": [
        # 重要度バッジ用フィールド（importance/importance_reason/importance_source）は、
        # `_重要度.txt` の解決結果がある時だけ付く（無ければ3キーとも省略・real fixtures/corpus/v1
        # には `_重要度.txt` が無いため、既定の PREVIEW はここでは付けない＝test_mock_api_contract
        # の mock⊆実 契約を保つ。バッジ表示自体のe2eは test_ingest_ui.py 側でこの3キーを
        # ローカルに追加した preview を個別に用意して確認する）。
        {"name": "4期/02_設計/01_基本設計/税計算仕様書.md", "folder": "4期/02_設計/01_基本設計",
         "branch": "office", "doctype": "md", "analyzer": None, "state": "ready", "top_scope": "4期",
         "phase": None, "category": "01_基本設計", "label": "使えます", "reason": None},
        {"name": "4期/03_開発/01_ソース/TAXCALC.cbl", "folder": "4期/03_開発/01_ソース",
         "branch": "source", "doctype": "cobol", "analyzer": "cobol", "state": "ready", "top_scope": "4期",
         "phase": None, "category": "01_ソース", "label": "使えます", "reason": None},
        {"name": "4期/03_開発/01_ソース/BILLGEN.cbl", "folder": "4期/03_開発/01_ソース",
         "branch": "source", "doctype": "cobol", "analyzer": "cobol", "state": "failed",
         "reason": "変換エラー", "top_scope": "4期",
         "phase": None, "category": "01_ソース", "label": "変換に失敗しました"},
        # S2「どう読み取ったか」バッジ用: 旧形式(.xls)を LibreOffice で変換→照合で差分ありの Office 文書。
        {"name": "4期/02_設計/01_基本設計/旧料金表.xls", "folder": "4期/02_設計/01_基本設計",
         "branch": "office", "doctype": "Excel(旧)", "analyzer": None, "state": "ready", "top_scope": "4期",
         "phase": None, "category": "01_基本設計", "label": "使えます（MD化）", "reason": None,
         "provenance": {"method": "ooxml", "confidence": 1.0,
                        "legacy_backend": "libreoffice", "has_conflicts": True}},
        # S2: 画像PDF を視覚読み取り（markitdown_ocr=VLM・tesseract 撤去後の唯一の OCR）した文書。
        {"name": "4期/02_設計/01_基本設計/スキャン図面.pdf", "folder": "4期/02_設計/01_基本設計",
         "branch": "office", "doctype": "PDF", "analyzer": None, "state": "ready", "top_scope": "4期",
         "phase": None, "category": "01_基本設計", "label": "使えます（画像OCR）", "reason": None,
         "provenance": {"method": "markitdown_ocr", "confidence": 0.4}},
    ],
    # S3: 実形状（sherpa/preview_service.py::build_preview の entities）は top_scope/phase/path を
    # 持つ（旧モックの "parent" キーは実 API に存在しない＝GRAPH と同じ実ドリフト是正）。
    # `analyzer`＝担当アナライザの来歴（コード分のみ非 None・§7 裁定2）。ソース正典化（K12）で
    # extraction_method は撤去済み（全件 static＝表示不要）。「消費税率(Parameter・analyzer=None)」は
    # 「税計算仕様書.md(Document・analyzer=None＝Documentはコード分析対象外)」に置換。
    "entities": [
        {"name": "税計算仕様書.md", "label": "Document", "top_scope": "4期", "phase": None,
         "path": "4期/02_設計/01_基本設計/税計算仕様書.md", "value": None,
         "status": "active", "analyzer": None},
        {"name": "TAX-RATE", "label": "DataItem", "top_scope": "4期", "phase": None,
         "path": "4期/03_開発/01_ソース/TAXCALC.cbl", "value": None,
         "status": "active", "analyzer": "cobol"},
    ],
    # フェーズ7-1（response_model 実測）で発見した実ドリフト是正: 実 relations
    # （preview_service.py::build_preview）は src_label/dst_label も常に持つ（旧モックは欠落）。
    # ソース正典化（K3）: REALIZES→DOCUMENTS（via=mention）に置換。
    "relations": [
        {"src": "税計算仕様書.md", "type": "DOCUMENTS", "dst": "TAX-RATE",
         "status": "active", "doc": "税計算仕様書.md", "src_label": "Document", "dst_label": "DataItem"},
    ],
    # ソース正典化（K12・実 schemas.py で確認済み）: `merges`（名寄せ・旧 REALIZES 由来）は
    # response_model から丸ごと撤去済み（`IngestPreviewMerge`/`IngestPreviewMergeMember` 削除）。
}

IMPACT_ANSWER = {
    "lens": "impact",
    "headline": "消費税率の変更は TAXCALC と請求機能に影響します。",
    "route": {"path": ["影響", "コード", "資料"]},
    # ソース正典化（K12）: 「確実/要確認」判定は機構ごと撤去＝全件同格。summary は件数のみ
    # （並行バックエンドレーンとの突合要）。
    "summary": {"count": 2},
    "scope": {"world": "w1", "scope_paths": ["4期/02_設計/01_基本設計"], "source": "explicit"},
    "data": {
        "items": [
            # analyzer: 担当アナライザの来歴（コード分のみ・§7 裁定2）。「請求機能」（Batch・JCL分）は
            # analyzer を持たない＝キー自体を省略（実 API と同じ・render 側は it.analyzer で判定）。
            {"category": "ソース", "name": "TAXCALC", "status": "active",
             "analyzer": "cobol",
             "trace": ["TAXCALC", "TAX-RATE"],
             "evidence": [{"doc": "4期/03_開発/01_ソース/TAXCALC.cbl"}]},
            {"category": "バッチ", "name": "請求機能", "status": "active",
             "trace": ["請求機能", "TAXCALC", "TAX-RATE"],
             "evidence": [{"doc": "4期/02_設計/01_基本設計/税計算仕様書.md"}]},
        ],
        "presumed": [],
    },
    "sources": [
        {"doc_id": "4期/02_設計/01_基本設計/税計算仕様書.md",
         "download_url": "/documents/download?world=w1&rel=4%E6%9C%9F%2F02_%E8%A8%AD%E8%A8%88%2F01_%E5%9F%BA%E6%9C%AC%E8%A8%AD%E8%A8%88%2F%E7%A8%8E%E8%A8%88%E7%AE%97%E4%BB%95%E6%A7%98%E6%9B%B8.md"},
    ],
}

# 送信の既定 SSE 応答（GET /chat/turns/{turn_id}/stream・後方互換の GET /chat/stream 共通）。
# 個別テストは `install_api_mocks(page, stream_events=[...])` で丸ごと上書きできる。
_DEFAULT_STREAM_EVENTS = [
    {"type": "node", "id": "understand", "kind": "thought", "status": "done",
     "label": "依頼を理解", "detail": "消費税率の変更影響"},
    # 同一 id の think ステップが detail を変えながら進む（active→done）＝履歴が蓄積される。
    {"type": "node", "id": "flow-hist", "kind": "think", "status": "active",
     "label": "意図を特定", "detail": "検索: 消費税率"},
    {"type": "node", "id": "tool-graph", "kind": "tool", "status": "done",
     "label": "グラフ検索", "detail": "2件"},
    # ツール detail が「」形式＝クエリがチップ化される。
    {"type": "node", "id": "flow-chip", "kind": "tool", "status": "done",
     "label": "資料を検索（grep）", "detail": "「消費税率」"},
    {"type": "node", "id": "mcp-graph-neighbors", "kind": "tool", "status": "done",
     "label": "MCP graph_neighbors", "detail": "TAX-RATE の近傍を確認"},
    {"type": "node", "id": "flow-hist", "kind": "think", "status": "done",
     "label": "意図を特定", "detail": "再検索: 税率 改定"},
    {"type": "answer", "conversation_id": 101, "message": {"answer": IMPACT_ANSWER}},
]

# S3: 会話ロード時の trace 静的復元 e2e 用（messages.trace の実データ形＝agents.py の _node() と同じ shape）。
ANSWER_TRACE = [
    {"type": "node", "id": "understand", "kind": "think", "label": "質問を理解",
     "detail": "内容を把握しました", "status": "done"},
    {"type": "node", "id": "tool-graph", "kind": "tool", "label": "関係グラフを照会",
     "detail": "「消費税率」", "status": "done"},
]

# S4-e（複数プロファイル並用＋自動選択・UI表示・§6.3）: 計画ノード（id="plan"・
# sherpa/providers/base.py::_agentic_run_plan の _node() と同じ shape）＋sub:{profile_id}: 名前空間化
# ノード（_run_sub_plan 参照）を含む trace。ライブ配信（stream_events）・保存済み再描画（conversations
# 詳細の messages[].trace）の両方で使い回す（node の描画は id を見ない＝label/detail/kind のみのため
# 元々どちらでも無改修で復元できる・回帰用に固定する）。
PLAN_TRACE = [
    {"type": "node", "id": "plan", "kind": "think", "label": "進め方を計画",
     "detail": "researcher・reviewer の順で調べます", "status": "done"},
    {"type": "node", "id": "sub:researcher:think", "kind": "think", "label": "意図を特定",
     "detail": "検索: 消費税率", "status": "done"},
    {"type": "node", "id": "sub:researcher:tool-1", "kind": "tool", "label": "資料を検索（grep）",
     "detail": "「消費税率」", "status": "done"},
    {"type": "node", "id": "sub:reviewer:tool-1", "kind": "tool", "label": "関係グラフを照会",
     "detail": "「TAX-RATE」", "status": "done"},
]
# usage_subs（複数形サイドカー・実行プロファイル2件以上・sherpa/providers/base.py::_usage_meta と
# 同じ標準形＋profile）を additive 表示するための回答（IMPACT_ANSWER に乗せる）。
PLAN_ANSWER = {**IMPACT_ANSWER, "usage_subs": [
    {"provider": "ollama", "model": "qwen2.5", "input_tokens": 1234, "output_tokens": 567,
     "cached_input_tokens": 0, "reasoning_output_tokens": 0, "profile": "researcher"},
    {"provider": "openai", "model": "gpt-4o-mini", "input_tokens": 89, "output_tokens": 45,
     "cached_input_tokens": 0, "reasoning_output_tokens": 12, "profile": "reviewer"},
]}

# parent_id によるネスト（拡張設計 §2/§10）の e2e 用。plan（親）→ step-1（plan の子）→
# step-1-detail（step-1 の子＝孫）の2段ネストを親→子の到着順で固定する。
V2_PARENT_ID_TRACE = [
    {"type": "node", "id": "plan", "kind": "think", "label": "進め方を計画",
     "detail": "下調べ役に任せます", "status": "done", "event_type": "plan_created"},
    {"type": "node", "id": "step-1", "kind": "tool", "label": "資料を検索（grep）",
     "detail": "「消費税率」", "status": "done", "parent_id": "plan"},
    {"type": "node", "id": "step-1-detail", "kind": "think", "label": "検索結果を確認",
     "detail": "3件ヒット", "status": "done", "parent_id": "step-1"},
]

# 同じネストを**子が親より先に届く順**で固定する——親がまだ無い間は一時的にレーン直下（フラット）へ
# 置き、親（"late-parent"）が届いた時点で実 DOM 要素ごと子コンテナへ付け替わることを検証する。
V2_PARENT_ID_OUT_OF_ORDER_TRACE = [
    {"type": "node", "id": "child-early", "kind": "tool", "label": "資料を検索（grep）",
     "detail": "「消費税率」", "status": "done", "parent_id": "late-parent"},
    {"type": "node", "id": "late-parent", "kind": "think", "label": "進め方を計画",
     "detail": "下調べ役に任せます", "status": "done"},
]

# pending child（親未着のため一時的にレーン直下＝集約バケット対象へ置かれた子）が、親の到着で
# 実 DOM 要素ごと子コンテナへ付け替わる際に、集約バケットの帳簿（count/leafEls/表示件数）からも
# 正しく取り除かれることを3つの到着順で固定する（順序が違うと発火するコードパスが変わる）。

# 順序A: child(P) → P → 兄弟×2（親到着時点でバケットは1件だけ＝取り除くと0件になり丸ごと削除。
# 残り2件は新規バケットとして始まり、3件に届かないため集約は発生しない）。
V2_BUCKET_REPARENT_ORDER_A_TRACE = [
    {"type": "node", "id": "child-a", "kind": "tool", "label": "資料を検索（grep）",
     "detail": "「A」", "status": "done", "parent_id": "plan-a"},
    {"type": "node", "id": "plan-a", "kind": "think", "label": "進め方を計画",
     "detail": "下調べ役に任せます", "status": "done"},
    {"type": "node", "id": "sib-a1", "kind": "tool", "label": "資料を検索（grep）",
     "detail": "「B」", "status": "done"},
    {"type": "node", "id": "sib-a2", "kind": "tool", "label": "資料を検索（grep）",
     "detail": "「C」", "status": "done"},
]

# 順序B: child(P) → 兄弟1 → P → 兄弟2 → 兄弟3（親到着時点でバケットはまだ集約前・2件中の1件を
# 取り除く＝leafEls から途中要素を splice する経路。残った1件から後で3件目に届いて正しく集約する）。
V2_BUCKET_REPARENT_ORDER_B_TRACE = [
    {"type": "node", "id": "child-b", "kind": "tool", "label": "資料を検索（grep）",
     "detail": "「A」", "status": "done", "parent_id": "plan-b"},
    {"type": "node", "id": "sib-b1", "kind": "tool", "label": "資料を検索（grep）",
     "detail": "「B」", "status": "done"},
    {"type": "node", "id": "plan-b", "kind": "think", "label": "進め方を計画",
     "detail": "下調べ役に任せます", "status": "done"},
    {"type": "node", "id": "sib-b2", "kind": "tool", "label": "資料を検索（grep）",
     "detail": "「C」", "status": "done"},
    {"type": "node", "id": "sib-b3", "kind": "tool", "label": "資料を検索（grep）",
     "detail": "「D」", "status": "done"},
]

# 順序C: child(P) → 兄弟×2（この時点で3件に達し集約枠が既に出来ている）→ P → 兄弟3件目。
# 親到着で集約枠から child を取り除き、残り件数が AGG_MIN_RUN(3) 未満になるため集約枠自体を
# 解体して個別2件表示へ戻す経路（枠を残したまま「×2」で居座らせない）。その後届く兄弟3件目で
# 件数が再び3件に達し、新しく集約枠を作り直す（「×3」へ再集約）。
V2_BUCKET_REPARENT_ORDER_C_TRACE = [
    {"type": "node", "id": "child-c", "kind": "tool", "label": "資料を検索（grep）",
     "detail": "「A」", "status": "done", "parent_id": "plan-c"},
    {"type": "node", "id": "sib-c1", "kind": "tool", "label": "資料を検索（grep）",
     "detail": "「B」", "status": "done"},
    {"type": "node", "id": "sib-c2", "kind": "tool", "label": "資料を検索（grep）",
     "detail": "「C」", "status": "done"},
    {"type": "node", "id": "plan-c", "kind": "think", "label": "進め方を計画",
     "detail": "下調べ役に任せます", "status": "done"},
    # 親到着で child が集約枠から取り除かれ、枠が解体されて個別2件表示（B・C）へ戻った*後*に、
    # 同種の4件目（実質バケット内では3件目）が届く——件数が再び AGG_MIN_RUN(3) に達し、
    # 新しく集約枠を作り直す（「×3」へ再集約）。
    {"type": "node", "id": "sib-c3", "kind": "tool", "label": "資料を検索（grep）",
     "detail": "「D」", "status": "done"},
]

# child(K,P) → 別種ノードX（バケット対象外）→ 兄弟B(K) → 兄弟C(K)（この時点で3件集約）→ P の順序。
# 親到着で集約枠が解体される時、残った兄弟（B・C）を「枠の旧位置へまとめて insertBefore」すると、
# 枠の外側に挟まっていた別種ノードXとの到着順が壊れる（本来 X,B,C,P の順であるべきところ
# B,C,X,P になる）。各要素が自分の到着順（_seq）が指す位置へ個別に戻ることを固定する。
V2_BUCKET_DISMANTLE_PRESERVES_ARRIVAL_ORDER_TRACE = [
    {"type": "node", "id": "child-d", "kind": "tool", "label": "資料を検索（grep）",
     "detail": "「A」", "status": "done", "parent_id": "plan-d"},
    {"type": "node", "id": "mid-d", "kind": "think", "label": "念のため確認",
     "detail": "他に手がかりが無いか探します", "status": "done"},
    {"type": "node", "id": "sib-d1", "kind": "tool", "label": "資料を検索（grep）",
     "detail": "「B」", "status": "done"},
    {"type": "node", "id": "sib-d2", "kind": "tool", "label": "資料を検索（grep）",
     "detail": "「C」", "status": "done"},
    {"type": "node", "id": "plan-d", "kind": "think", "label": "進め方を計画",
     "detail": "下調べ役に任せます", "status": "done"},
]

# child(K,P) → 別種ノードX → 兄弟B(K) → 兄弟C(K)（この時点で3件集約）→ 兄弟D(K)（4件目・
# 枠は解体されず存続）→ P の順序。親到着で枠内の最古参（child）が取り除かれても枠自体は
# 存続する（残り3件で AGG_MIN_RUN を満たすため）——このとき枠の到着順・DOM 位置が
# 取り除かれた最古参のままだと、枠より後・X より前という誤った位置に取り残される
# （本来 X, 枠(B・C・D), P の順であるべきところ 枠, X, P になる）。
V2_BUCKET_SURVIVING_FRAME_REPOSITIONS_ON_DETACH_TRACE = [
    {"type": "node", "id": "child-e", "kind": "tool", "label": "資料を検索（grep）",
     "detail": "「A」", "status": "done", "parent_id": "plan-e"},
    {"type": "node", "id": "mid-e", "kind": "think", "label": "念のため確認",
     "detail": "他に手がかりが無いか探します", "status": "done"},
    {"type": "node", "id": "sib-e1", "kind": "tool", "label": "資料を検索（grep）",
     "detail": "「B」", "status": "done"},
    {"type": "node", "id": "sib-e2", "kind": "tool", "label": "資料を検索（grep）",
     "detail": "「C」", "status": "done"},
    {"type": "node", "id": "sib-e3", "kind": "tool", "label": "資料を検索（grep）",
     "detail": "「D」", "status": "done"},
    {"type": "node", "id": "plan-e", "kind": "think", "label": "進め方を計画",
     "detail": "下調べ役に任せます", "status": "done"},
]

# child(K,P) → 兄弟B(K) → サブエージェント レーン開始 → 兄弟C(K)（この時点で3件集約・解体対象）
# → P の順序。レーン直下に実コンテンツとして置かれる `.fagent`（サブエージェントのレーン枠）に
# 到着順が刻まれていないと、集約枠の解体時の兄弟位置比較から漏れる（本来 B, レーン, C, P の
# 順であるべきところ レーン, B, C, P になる）。
V2_BUCKET_SURVIVES_SUBAGENT_LANE_INTERLEAVED_TRACE = [
    {"type": "node", "id": "child-f", "kind": "tool", "label": "資料を検索（grep）",
     "detail": "「A」", "status": "done", "parent_id": "plan-f"},
    {"type": "node", "id": "sib-f1", "kind": "tool", "label": "資料を検索（grep）",
     "detail": "「B」", "status": "done"},
    {"type": "node", "id": "sub-f-start", "kind": "think", "label": "下調べ役に任せる",
     "detail": "qwen2.5 が資料を探して読みます（回答はこの後メインのAIが作ります）",
     "status": "done", "agent_run_id": "sub:researcher:1",
     "metrics": {"provider": "ollama", "model": "qwen2.5", "is_local": "local", "name": "下調べ役"}},
    {"type": "node", "id": "sib-f2", "kind": "tool", "label": "資料を検索（grep）",
     "detail": "「C」", "status": "done"},
    {"type": "node", "id": "plan-f", "kind": "think", "label": "進め方を計画",
     "detail": "下調べ役に任せます", "status": "done"},
]

# trace_version=2 のサブエージェント レーン e2e 用。agent_run_id/metrics.provider・model・
# is_local・name/event_type/evidence_ids は sherpa/exec_event.py・
# sherpa/providers/base.py（ハイブリッド下調べ役の `_sub_agent_metrics`/`_sub_agent_completed_node`）
# が実際に組む shape と同じ。`is_local`/`name` はサーバの権威ある判定（`agent_constructs.is_local`）・
# 表示名（`search_helper.resolve()` の "name"）をそのまま模す＝フロントは推測しない契約。
# 「資料を検索（grep）」を3件並べて集約表示（GREP×N 相当）の閾値（AGG_MIN_RUN=3）に届かせる。
V2_LANE_TRACE = [
    # 計画ノードの detail は表示名（「下調べ役」）のみを載せる（内部 slug "researcher" を
    # 直接出さない・providers/base.py::_agentic_run_plan の
    # `names = "・".join(s.get("name") or s["profile_id"] for s in chosen_subs)` と同じ規律）。
    {"type": "node", "id": "plan", "kind": "think", "label": "進め方を計画",
     "detail": "下調べ役に任せます", "status": "done", "event_type": "plan_created"},
    {"type": "node", "id": "search-helper", "kind": "think", "label": "下調べ役に任せる",
     "detail": "qwen2.5 が資料を探して読みます（回答はこの後メインのAIが作ります）",
     "status": "done", "agent_run_id": "sub:researcher:1",
     "metrics": {"provider": "ollama", "model": "qwen2.5", "is_local": "local", "name": "下調べ役"}},
    {"type": "node", "id": "sub:researcher:1:grep-1", "kind": "tool", "label": "資料を検索（grep）",
     "detail": "「消費税率」", "status": "done", "agent_run_id": "sub:researcher:1",
     "metrics": {"provider": "ollama", "model": "qwen2.5", "is_local": "local", "name": "下調べ役"}},
    {"type": "node", "id": "sub:researcher:1:grep-2", "kind": "tool", "label": "資料を検索（grep）",
     "detail": "「税率 改定」", "status": "done", "agent_run_id": "sub:researcher:1",
     "metrics": {"provider": "ollama", "model": "qwen2.5", "is_local": "local", "name": "下調べ役"}},
    {"type": "node", "id": "sub:researcher:1:grep-3", "kind": "tool", "label": "資料を検索（grep）",
     "detail": "「TAX-RATE」", "status": "done", "agent_run_id": "sub:researcher:1",
     "metrics": {"provider": "ollama", "model": "qwen2.5", "is_local": "local", "name": "下調べ役"}},
    {"type": "node", "id": "sub:researcher:1:read-1", "kind": "tool", "label": "該当箇所を精読",
     "detail": "税計算仕様書.md 付近", "status": "done", "agent_run_id": "sub:researcher:1",
     "metrics": {"provider": "ollama", "model": "qwen2.5", "is_local": "local", "name": "下調べ役"}},
    {"type": "node", "id": "sub:researcher:1:eval-1", "kind": "evaluation", "label": "調査状況を評価",
     "detail": "十分な根拠が集まりました", "status": "done", "event_type": "evaluation_completed",
     "agent_run_id": "sub:researcher:1",
     "metrics": {"provider": "ollama", "model": "qwen2.5", "is_local": "local", "name": "下調べ役"}},
    # `_sub_agent_completed_node`: サブループ自身の完了合図。
    {"type": "node", "id": "sub:researcher:1:completed", "kind": "agent", "label": "下調べ役が完了しました",
     "detail": "", "status": "done", "event_type": "agent_completed", "agent_run_id": "sub:researcher:1",
     "metrics": {"provider": "ollama", "model": "qwen2.5", "is_local": "local", "name": "下調べ役"}},
    {"type": "node", "id": "evidence-committed", "kind": "evidence", "label": "根拠を確定",
     "detail": "2 件の根拠を機械検証済みとして確定しました", "status": "done",
     "event_type": "evidence_committed", "evidence_ids": ["ev-1", "ev-2"]},
]
V2_LANE_ANSWER = {**IMPACT_ANSWER, "trace_version": 2,
                  "data": {**IMPACT_ANSWER["data"],
                          "evidence_packet": {"stop_reason": "no_tool_calls"}},   # →「自然終了」
                  "usage": {"provider": "openai", "model": "gpt-5.5",
                           "input_tokens": 900, "output_tokens": 120,
                           "cached_input_tokens": 0, "reasoning_output_tokens": 0, "is_local": "cloud"},
                  # `usage_sub.profile` は実際のサーバ（providers/base.py）が
                  # `sub.get("name") or profile_id` で組む値と同じく表示名を使う
                  # （内部 slug "search-helper-openai" を直接出さない）。
                  "usage_sub": {"provider": "ollama", "model": "qwen2.5", "input_tokens": 300,
                               "output_tokens": 40, "cached_input_tokens": 0,
                               "reasoning_output_tokens": 0, "is_local": "local", "profile": "下調べ役"}}

# 下調べ役 OFF（サブレーン無し）の trace_version=2 会話。「実行の分担」サマリが
# 「すべてクラウド AI が担当しました」に縮退することを固定する（利用者決定 2026-08-28）。
V2_NOSUB_TRACE = [
    {"type": "node", "id": "understand", "kind": "think", "label": "質問を理解",
     "detail": "内容を把握しました", "status": "done"},
    {"type": "node", "id": "tool-graph", "kind": "tool", "label": "関係グラフを照会",
     "detail": "「消費税率」", "status": "done"},
]
V2_NOSUB_ANSWER = {**IMPACT_ANSWER, "trace_version": 2,
                   "data": {**IMPACT_ANSWER["data"],
                           "evidence_packet": {"stop_reason": "no_tool_calls"}},
                   "usage": {"provider": "openai", "model": "gpt-5.5",
                            "input_tokens": 400, "output_tokens": 80,
                            "cached_input_tokens": 0, "reasoning_output_tokens": 0, "is_local": "cloud"}}

# Codex(Ollama) 構成（`answer.usage.provider == "codex"`）の回帰用。Codex は常にこの provider
# 文字列を名乗るため、`is_local` をサーバが明示しないとフロントは接続先を判定できない——
# 実際のサーバ判定（`_usage_from_turn_completed`→`agent_constructs.is_local("codex", ...)`）が
# 返す形どおり `is_local: "local"` を明示したフィクスチャにする。
V2_CODEX_OLLAMA_ANSWER = {**IMPACT_ANSWER, "trace_version": 2,
                          "data": {**IMPACT_ANSWER["data"],
                                  "evidence_packet": {"stop_reason": "no_tool_calls"}},
                          "usage": {"provider": "codex", "model": "qwen2.5",
                                   "input_tokens": 500, "output_tokens": 60,
                                   "cached_input_tokens": 0, "reasoning_output_tokens": 0, "is_local": "local"}}

# usage 自体が無い回答（旧メッセージ・heuristic 相当）の回帰用（誤断定しない）。
# 「担当不明」を出し、「すべてローカル」等へ誤って決め打たないことを固定する。
V2_NOUSAGE_ANSWER = {**IMPACT_ANSWER, "trace_version": 2,
                     "data": {**IMPACT_ANSWER["data"],
                             "evidence_packet": {"stop_reason": "no_tool_calls"}}}

# 評価フェーズが「根拠不足」で打ち切った終了理由（`evaluation_blocked`）→
# 「根拠不足で中断」の平文固定の回帰用。
V2_BLOCKED_ANSWER = {**IMPACT_ANSWER, "trace_version": 2,
                     "data": {**IMPACT_ANSWER["data"],
                             "evidence_packet": {"stop_reason": "evaluation_blocked"}},
                     "usage": {"provider": "openai", "model": "gpt-5.5",
                              "input_tokens": 300, "output_tokens": 40,
                              "cached_input_tokens": 0, "reasoning_output_tokens": 0, "is_local": "cloud"}}

# ターン上限到達（`turns_exhausted`）→「調査の上限に到達」の平文固定の回帰用
# （evidence_packet.stop_reason だけを唯一の根拠にする契約）。
V2_BUDGET_ANSWER = {**IMPACT_ANSWER, "trace_version": 2,
                    "data": {**IMPACT_ANSWER["data"],
                            "evidence_packet": {"stop_reason": "turns_exhausted"}},
                    "usage": {"provider": "openai", "model": "gpt-5.5",
                             "input_tokens": 300, "output_tokens": 40,
                             "cached_input_tokens": 0, "reasoning_output_tokens": 0, "is_local": "cloud"}}

# 道具（tool）の1ターンあたり呼び出し上限に到達（`tools_per_turn_exceeded`・agentic_search.py が
# 実際に設定する値）→「道具の使用回数の上限に到達」の平文固定の回帰用。
V2_TOOLS_LIMIT_ANSWER = {**IMPACT_ANSWER, "trace_version": 2,
                         "data": {**IMPACT_ANSWER["data"],
                                 "evidence_packet": {"stop_reason": "tools_per_turn_exceeded"}},
                         "usage": {"provider": "openai", "model": "gpt-5.5",
                                  "input_tokens": 300, "output_tokens": 40,
                                  "cached_input_tokens": 0, "reasoning_output_tokens": 0, "is_local": "cloud"}}

# 出力上限で打ち切られた（`truncated`・agentic_search.py が finish_reason="length"/"max_tokens"/
# "MAX_TOKENS" から実際に設定する値）→「出力上限で途中終了」の平文固定の回帰用。
# 従来は自然終了と同じ "no_tool_calls" に丸められ「自然終了」と誤って表示されていた。
V2_TRUNCATED_ANSWER = {**IMPACT_ANSWER, "trace_version": 2,
                       "data": {**IMPACT_ANSWER["data"],
                               "evidence_packet": {"stop_reason": "truncated"}},
                       "usage": {"provider": "openai", "model": "gpt-5.5",
                                "input_tokens": 300, "output_tokens": 40,
                                "cached_input_tokens": 0, "reasoning_output_tokens": 0, "is_local": "cloud"}}

# 内容フィルタで打ち切られた（`content_filtered`・agentic_search.py が finish_reason=
# "content_filter"/"SAFETY" から実際に設定する値）→「内容の制限で終了」の平文固定の回帰用。
V2_CONTENT_FILTERED_ANSWER = {**IMPACT_ANSWER, "trace_version": 2,
                              "data": {**IMPACT_ANSWER["data"],
                                      "evidence_packet": {"stop_reason": "content_filtered"}},
                              "usage": {"provider": "openai", "model": "gpt-5.5",
                                       "input_tokens": 300, "output_tokens": 40,
                                       "cached_input_tokens": 0, "reasoning_output_tokens": 0,
                                       "is_local": "cloud"}}

# 安全上の理由で回答を控えた（`refusal`・agentic_search.py が実際に設定する値）→
# 「AI が回答を控えた」の平文固定の回帰用。
V2_REFUSAL_ANSWER = {**IMPACT_ANSWER, "trace_version": 2,
                     "data": {**IMPACT_ANSWER["data"],
                             "evidence_packet": {"stop_reason": "refusal"}},
                     "usage": {"provider": "openai", "model": "gpt-5.5",
                              "input_tokens": 300, "output_tokens": 40,
                              "cached_input_tokens": 0, "reasoning_output_tokens": 0, "is_local": "cloud"}}

# 対応表に無い未知の stop_reason（将来の新しい値・壊れたデータ等）→「終了理由を確認できませんでした」
# へ誠実に落ち、かつ回答まで到達した経路（evidence_packet 経由）は既知/未知を問わず「中断」扱いに
# しないことの回帰用（未知＝不明であって失敗ではない）。
V2_UNKNOWN_STOP_REASON_ANSWER = {**IMPACT_ANSWER, "trace_version": 2,
                                 "data": {**IMPACT_ANSWER["data"],
                                         "evidence_packet": {"stop_reason": "future_unknown_reason"}},
                                 "usage": {"provider": "openai", "model": "gpt-5.5",
                                          "input_tokens": 300, "output_tokens": 40,
                                          "cached_input_tokens": 0, "reasoning_output_tokens": 0,
                                          "is_local": "cloud"}}

# 上と対で使う trace: レーンが `agent_completed` に到達せず `active` のまま answer を迎える
# （`trace: []` だとレーン自体が無く「aborted にならない」を検証できないため）。
V2_UNKNOWN_STOP_REASON_TRACE = [
    {"type": "node", "id": "search-helper", "kind": "think", "label": "下調べ役に任せる",
     "detail": "qwen2.5 が資料を探して読みます", "status": "done", "agent_run_id": "sub:researcher:1",
     "metrics": {"provider": "ollama", "model": "qwen2.5", "is_local": "local", "name": "下調べ役"}},
    {"type": "node", "id": "sub:researcher:1:grep-1", "kind": "tool", "label": "資料を検索（grep）",
     "detail": "「消費税率」", "status": "active", "agent_run_id": "sub:researcher:1",
     "metrics": {"provider": "ollama", "model": "qwen2.5", "is_local": "local", "name": "下調べ役"}},
]

# S3（mock 契約ドリフト対策）: 実 GET /admin/users（store.list_users）は uid/email/display_name/role/
# status/must_change_password/last_login_at を返す。GET /auth/me は別形（email/must_change_password/
# auth_disabled はあるが status は無い）なので、/auth/me のハンドラ側で status を落として組み立てる
# （USER_ADMIN/USER_MEMBER 自体は「ユーザーの素材」として両エンドポイントで使い回す）。
USER_ADMIN = {"uid": "admin", "email": "admin@example.com", "display_name": "管理者", "role": "admin",
              "status": "active", "must_change_password": False}
USER_MEMBER = {"uid": "sato", "email": "sato@example.com", "display_name": "佐藤 太郎", "role": "user",
               "status": "active", "must_change_password": False}


def auth_me_response(current_user: dict) -> dict:
    """GET /auth/me の mock 応答（S3: 実形状＝sherpa/routers/auth.py::auth_me と同キー）。

    uid/email/display_name/role/must_change_password/auth_disabled。status は無い（/admin/users
    専用のフィールド）。auth_disabled は呼び出し元が `user={**USER_ADMIN, "auth_disabled": True}` で
    上書きできる互換モード再現用（既定 False）。RV LOW（2026-07-14 フェーズ7 2巡目）: handler 内
    literal から関数へ抽出＝契約テスト（tests/api/test_mock_api_contract.py）が**この実関数を実行して**
    実 API と突合できるようにする（literal だと mock 側 drift を検知できない）。"""
    return {
        "uid": current_user["uid"], "email": current_user.get("email"),
        "display_name": current_user.get("display_name"), "role": current_user["role"],
        "must_change_password": bool(current_user.get("must_change_password")),
        "auth_disabled": bool(current_user.get("auth_disabled", False)),
    }


# フェーズ7-1 再RV（Codex MEDIUM・2026-07-16）: handler 内で動的に組み立てる応答（個人ワークスペース・
# ナレッジグラフ）も関数へ抽出する（auth_me_response と同じ理由＝契約テストがこの実関数を実行して
# 実 API と突合できるようにする・literal のままだと mock drift を検知できない）。

def workspace_upload_response(file_id: int, rel_path: str, size_bytes: int = 18,
                              sha256: str = "0" * 64) -> dict:
    """POST /workspace/files の mock 応答（実形状＝workspace.py::workspace_file_upload と同キー・
    フラットな {"ok","id","rel_path","size_bytes","sha256"}。created_at/expires_at は含まない
    ＝それらは GET /workspace/files 一覧側だけが持つ）。"""
    return {"ok": True, "id": file_id, "rel_path": rel_path, "size_bytes": size_bytes, "sha256": sha256}


def workspace_delete_response(file_id: int, rel_path: str) -> dict:
    """DELETE /workspace/files/{file_id} の mock 応答（実形状＝workspace.py::workspace_file_delete
    と同キー＝ "file_id" でなく "id" ＋削除した行の "rel_path"）。"""
    return {"ok": True, "id": file_id, "rel_path": rel_path}


def workspace_search_response(q: str) -> dict:
    """GET /workspace/search の mock 応答（実形状＝workspace.py::workspace_search と同キー＝
    トップレベルに "query"/"source" も持ち、hits[] は "match" も持つ）。"""
    return {
        "query": q, "source": "個人ファイル内ヒット",
        "hits": [
            {"rel_path": "notes/tax.csv", "line": 4,
             "text": f"{q}: TAX-RATE は個人メモ側にも記載されています。", "match": q},
        ] if q else [],
    }


def graph_ask_response(question: str | None) -> dict:
    """POST /graph/ask の mock 応答（実形状＝graph_admin.py::ask_graph と同キー・cited_nodes[] は
    type_ja/category/distance/edges も持ち、summary は isolated_nodes/weak_documents/
    recent_ingest_errors の中身の list も持つ）。"""
    return {"status": "ok", "world": "w1", "question": question,
            "answer": "TAX-RATE は消費税率と TAXCALC に関係します。",
            "cited_nodes": [{"name": "TAXCALC", "label": "Module", "type_ja": "プログラム",
                             "role": "実装", "category": None, "distance": 2,
                             "path": ["消費税率", "TAX-RATE", "TAXCALC"], "edges": []}],
            "docs": [],
            "summary": {"world": "w1", "scope_paths": [], "documents": 3,
                       "graph_nodes": 4, "graph_edges": 3,
                       "isolated_node_count": 1,
                       "isolated_nodes": [{"name": "経理コーディング規約.md",
                                          "type": "Document", "path": None}],
                       "weak_document_count": 0, "weak_documents": [],
                       "recent_ingest_errors": []}}


def graph_search_response(nodes: list, edges: list, counts: dict) -> dict:
    """GET /graph/search の mock 応答（実形状＝graph_admin.py::_rows_to_graph と同キー。nodes[] は
    GET /graph（preview_service.py::graph_view）と別形＝ phase/category に加え em も持つ
    （graph_admin.py::_node は S3 でも em を撤去していない）ため、呼び出し側で
    `GRAPH["nodes"]` に `em`/`phase`/`category` を足してから渡すこと）。"""
    return {"world": "w1", "nodes": nodes, "edges": edges, "counts": counts}


def world_diff_response(path: str | None, world_id: str | None = None, label: str | None = None,
                        registered: bool = False, added: list | None = None,
                        changed: list | None = None, removed: list | None = None,
                        total: int = 2, indexed: int = 0) -> dict:
    """POST /worlds/diff・GET /worlds/{wid}/diff の mock 応答（実形状＝worlds.py::_diff_payload と
    同キー＝ "world_id" も常に持つ）。既定値は未登録フォルダ（POST /worlds/diff の代表例＝
    追加2件・未取込）を表す。登録済みで変更なしの例（GET /worlds/{wid}/diff）は呼び出し側で
    `added=[]`・`total=indexed` を渡す。"""
    return {"ok": True, "registered": registered, "world_id": world_id, "label": label,
            "root_path": path,
            "added": added if added is not None else ["4期/税計算仕様書.md", "4期/TAXCALC.cbl"],
            "changed": changed or [], "removed": removed or [], "total": total, "indexed": indexed}


def world_ingest_accepted_response(world_id: str = "w1", *, run_id: int = 501,
                                   joined: bool = False) -> dict:
    """POST /worlds・POST /worlds/{wid}/refresh 等の mock 応答
    （ING-3・即受付契約＝実形状は `worlds.py::WorldIngestAcceptedResponse` と同キー）。取り込み
    本体は背景実行のため、この応答自体は完了結果（`summary` 等）を持たない——利用者向けの
    最終状態は別途 `GET /worlds/{wid}/status`（`WORLD_STATUS_RESP`）をポーリングして得る。
    `run_id`（ING-3）は受付処理自身が O(1) の INSERT で確保するため常に非 null。"""
    return {"ok": True, "world_id": world_id, "run_id": run_id, "joined": joined,
            "note": "既存の取り込みに合流しました。" if joined
                    else "受け付けました。状況は取り込み状況でご確認ください。"}


USERS = [
    {**USER_ADMIN, "last_login_at": "2026-07-01T09:00:00+00:00"},
    {**USER_MEMBER, "last_login_at": None},
]

# バッチ2・5番（2026-07-03）: GET /users/suggest の候補プール（共有ダイアログの入力補完テスト用）。
USERS_SUGGEST_POOL = [
    {"uid": "tanaka", "display_name": "田中 花子"},
    {"uid": "yamada", "display_name": "山田 太郎"},
]

WORKSPACE_FILES = [
    {"id": 501, "rel_path": "onboarding.md", "size_bytes": 1234,
     "created_at": "2026-07-01T08:00:00+00:00", "expires_at": "2026-07-08T08:00:00+00:00"},
    {"id": 502, "rel_path": "notes/tax.csv", "size_bytes": 2048,
     "created_at": "2026-07-01T08:10:00+00:00", "expires_at": None},
]

# S4: 掲示板の公開/削除タイマー。1件だけ最初から「掲載終了済み」を種として持たせ、編集フォームの
# 日時欄プレフィルの逆変換（ISO→datetime-local・UTC→JST）を確認できるようにする。
ANNOUNCEMENTS = [
    {"id": 901, "author_uid": "admin", "title": "定期メンテナンスのお知らせ", "body": "本文です。",
     "category": "maintenance", "pinned": False, "published": True,
     "publish_at": None, "expire_at": "2026-06-01T09:00:00+00:00", "status": "expired",
     "created_at": "2026-05-01T09:00:00+00:00", "updated_at": "2026-05-01T09:00:00+00:00"},
]

# フェーズ7-1（response_model 実測）で発見した実ドリフト是正: 実 GET /admin/audit（store.list_audit）
# の各行は id/request_id/session_id/ip_hash/user_agent/before_state/after_state も常に持つ列
# （旧モックは欠落＝ネストした list 要素までは既存の mock 契約テストが突合しない粒度のため
# 見逃されていた）。id は行ごとに一意な値を割り当てる・それ以外はダミーで None のまま。
AUDIT_ROWS = [
    {"id": 1, "created_at": "2026-07-01T09:10:00+00:00", "actor_user_id": "admin",
     "action": "auth.login", "resource_type": "user", "resource_id": "user:admin",
     "outcome": "success", "severity": "info", "reason": None, "detail": {"method": "password"},
     "request_id": None, "session_id": None, "ip_hash": None, "user_agent": None,
     "before_state": None, "after_state": None},
    {"id": 2, "created_at": "2026-07-01T09:20:00+00:00", "actor_user_id": "admin",
     "action": "share.created", "resource_type": "share", "resource_id": "share:77",
     "outcome": "success", "severity": "info", "reason": None, "detail": {"invitees": 2},
     "request_id": None, "session_id": None, "ip_hash": None, "user_agent": None,
     "before_state": None, "after_state": None},
    {"id": 3, "created_at": "2026-07-01T09:30:00+00:00", "actor_user_id": "sato",
     "action": "auth.login_failed", "resource_type": "user", "resource_id": "user:sato",
     "outcome": "deny", "severity": "warning", "reason": "bad_credentials", "detail": {"uid": "sato"},
     "request_id": None, "session_id": None, "ip_hash": None, "user_agent": None,
     "before_state": None, "after_state": None},
    {"id": 4, "created_at": "2026-07-01T09:40:00+00:00", "actor_user_id": "admin",
     "action": "workspace.file_uploaded", "resource_type": "workspace_file", "resource_id": "pwf:501",
     "outcome": "success", "severity": "info", "reason": None, "detail": {"rel_path": "onboarding.md"},
     "request_id": None, "session_id": None, "ip_hash": None, "user_agent": None,
     "before_state": None, "after_state": None},
    # RV9 #7: 利用統計チャット（POST /admin/usage/chat）の pending→結果 2行契約を e2e で固定する。
    # 同じ request_id で対応付く pending 行＋failure 行の対（audit.js::OUTCOME_LABEL の
    # 「送信前記録」/「失敗」表示・oc-pending/oc-error の色分け・行の title 属性・行クリック展開の
    # 詳細先頭に request_id が出ることを e2e から確認できるようにする）。
    {"id": 5, "created_at": "2026-07-01T09:50:00+00:00", "actor_user_id": "admin",
     "action": "admin.usage_chat_asked", "resource_type": "usage", "resource_id": None,
     "outcome": "pending", "severity": "info", "reason": None,
     "detail": {"question_len": 12, "history_len": 0, "status_code": None, "reason": None,
                "history_truncated": False},
     "request_id": "req-usagechat-e2e-001", "session_id": None, "ip_hash": None, "user_agent": None,
     "before_state": None, "after_state": None},
    {"id": 6, "created_at": "2026-07-01T09:50:01+00:00", "actor_user_id": "admin",
     "action": "admin.usage_chat_asked", "resource_type": "usage", "resource_id": None,
     "outcome": "failure", "severity": "info", "reason": "llm_call_failed",
     "detail": {"question_len": 12, "history_len": 0, "status_code": 502, "reason": "llm_call_failed",
                "history_truncated": False},
     "request_id": "req-usagechat-e2e-001", "session_id": None, "ip_hash": None, "user_agent": None,
     "before_state": None, "after_state": None},
]


def _post_json(request) -> dict:
    raw = getattr(request, "post_data", None)
    if callable(raw):
        raw = raw()
    if not raw:
        return {}
    return json.loads(raw)


def _json(route, payload, status=200, headers=None):
    route.fulfill(status=status, content_type="application/json",
                  headers=headers, body=json.dumps(payload, ensure_ascii=False))


def _sse(route, events):
    body = "".join(f"data: {json.dumps(evt, ensure_ascii=False)}\n\n" for evt in events)
    route.fulfill(status=200, headers={"Content-Type": "text/event-stream",
                                       "Cache-Control": "no-cache"}, body=body)


def _multipart_filename(request) -> str:
    raw = getattr(request, "post_data", None)
    if callable(raw):
        raw = raw()
    if not raw:
        return "uploaded.txt"
    m = re.search(r'filename="([^"]+)"', raw)
    return m.group(1) if m else "uploaded.txt"


def _filtered_audit_rows(query: dict[str, list[str]]) -> list[dict]:
    rows = list(AUDIT_ROWS)
    actor = (query.get("actor") or [""])[0]
    action = (query.get("action") or [""])[0]
    outcome = (query.get("outcome") or [""])[0]
    severity = (query.get("severity") or [""])[0]
    if actor:
        rows = [r for r in rows if actor in (r.get("actor_user_id") or "")]
    if action:
        if action.endswith("*"):
            prefix = action[:-1]
            rows = [r for r in rows if (r.get("action") or "").startswith(prefix)]
        else:
            rows = [r for r in rows if action in (r.get("action") or "")]
    if outcome:
        rows = [r for r in rows if r.get("outcome") == outcome]
    if severity:
        rows = [r for r in rows if r.get("severity") == severity]
    return rows


_HEALTH_COMPONENTS_DEFAULT = [
    {"id": "postgres", "label": "PostgreSQL（会話・ユーザー・台帳）", "impact": "down",
     "ok": True, "detail": None, "latency_ms": 4},
    {"id": "neo4j", "label": "Neo4j（ナレッジグラフ・影響分析）", "impact": "degraded",
     "ok": True, "detail": None, "latency_ms": 6},
    {"id": "elasticsearch", "label": "Elasticsearch（全文検索）", "impact": "degraded",
     "ok": True, "detail": None, "latency_ms": 5},
    {"id": "openai", "label": "OpenAI API", "impact": "none", "ok": True, "detail": None, "latency_ms": 320},
    {"id": "gemini", "label": "Gemini（Google）", "impact": "none", "ok": True, "detail": None, "latency_ms": 210},
    {"id": "bedrock", "label": "AWS Bedrock（Claude）", "impact": "none", "ok": False,
     "detail": "認証失敗（RuntimeError）", "latency_ms": 180,
     "hint": "設定画面で Bedrock の API キーを入れるか、サーバ側 env を設定してください"},
    {"id": "ollama", "label": "ローカルLLM（Ollama）", "impact": "none", "ok": False,
     "detail": "接続拒否（サービス停止の可能性）（URLError）", "latency_ms": 50,
     "hint": "ollama serve の起動を確認してください（使わない構成なら対応不要）"},
    {"id": "codex", "label": "Codex CLI（AIエージェント）", "impact": "none", "ok": True,
     "detail": None, "latency_ms": 900},
    {"id": "es_search", "label": "ES検索（実クエリ）", "impact": "none", "ok": True,
     "detail": "ヒットあり", "latency_ms": 40},
    {"id": "graph_search", "label": "グラフ検索（実クエリ）", "impact": "none", "ok": True,
     "detail": "ヒットあり", "latency_ms": 60},
]

# バッチ3（2026-07-03）: GET /admin/usage/stats の既定応答（「利用の傾向」全指標を含む）。
# 空データ描画確認用テストは `usage_stats={"users": [], "totals": {...}, ...}` で丸ごと上書きする。
USAGE_STATS_DEFAULT = {
    "users": [
        {"uid": "admin", "display_name": "管理者", "turns": 12, "conversations": 4, "active_days": 3,
         "last_active": "2026-07-03T09:00:00+00:00",
         "lens": {"impact": 5, "qa": 4, "troubleshoot": 2, "chat": 1},
         "personal_turns": 1, "worlds": ["test"], "logins": 3, "downloads": 5, "uploads": 1, "shares": 1,
         "knowledge_turns": 11, "zero_hit_turns": 3, "zero_hit_rate": 0.2727272727272727},
        {"uid": "sato", "display_name": "佐藤 太郎", "turns": 4, "conversations": 2, "active_days": 2,
         "last_active": "2026-07-02T05:00:00+00:00",
         "lens": {"impact": 0, "qa": 3, "troubleshoot": 0, "chat": 1},
         "personal_turns": 0, "worlds": ["test"], "logins": 1, "downloads": 0, "uploads": 0, "shares": 0,
         "knowledge_turns": 3, "zero_hit_turns": 0, "zero_hit_rate": 0.0},
    ],
    "totals": {"turns": 16, "active_users": 2, "conversations": 6},
    "daily": [
        {"date": "2026-07-01", "turns": 6, "active_users": 2},
        {"date": "2026-07-02", "turns": 4, "active_users": 1},
        {"date": "2026-07-03", "turns": 6, "active_users": 2},
    ],
    "period": {"start": "2026-06-04", "end": "2026-07-03", "days": 30},
    "zero_hit": {"knowledge_turns": 14, "zero_hit_turns": 3, "rate": 0.21428571428571427},
    "worlds": [{"world": "test", "turns": 16}],
    "providers": [
        {"provider": "heuristic", "turns": 10},
        {"provider": "openai", "turns": 4},
        {"provider": "codex", "turns": 2},
    ],
    "heatmap": [
        {"weekday": 1, "hour": 9, "count": 4},
        {"weekday": 1, "hour": 10, "count": 2},
        {"weekday": 3, "hour": 14, "count": 6},
        {"weekday": 5, "hour": 21, "count": 1},
    ],
    "retention": {
        "weekly": [
            {"week_start": "2026-06-22", "active_users": 2},
            {"week_start": "2026-06-29", "active_users": 2},
        ],
        "revisit_rate": 0.5,
    },
    "downloads": {
        "total": 5,
        "daily": [
            {"date": "2026-07-01", "count": 2},
            {"date": "2026-07-02", "count": 0},
            {"date": "2026-07-03", "count": 3},
        ],
    },
    # F3（2026-07-07／2026-07-08 金額表示は撤去）: トークン（入力/出力トークン数のみ）。
    "tokens": {
        "totals": {"turns": 6, "input": 12000, "cached_input": 3000, "output": 1800,
                   "reasoning_output": 900},
        "by_model": [
            {"provider": "codex", "model": "gpt-5.5", "turns": 4, "input": 10000,
             "cached_input": 3000, "output": 1500, "reasoning_output": 900},
            {"provider": "gemini", "model": "gemini-2.5-flash", "turns": 2, "input": 2000,
             "cached_input": 0, "output": 300, "reasoning_output": 0},
        ],
        "by_user": [
            {"uid": "admin", "display_name": "管理者", "turns": 5, "input": 11000,
             "cached_input": 3000, "output": 1600, "reasoning_output": 900},
            {"uid": "sato", "display_name": "佐藤 太郎", "turns": 1, "input": 1000,
             "cached_input": 0, "output": 200, "reasoning_output": 0},
        ],
        "daily": [
            {"date": "2026-07-01", "input": 4000, "output": 600},
            {"date": "2026-07-02", "input": 3000, "output": 400},
            {"date": "2026-07-03", "input": 5000, "output": 800},
        ],
        # S1（2026-07-15-LLMオーケストレーション実装計画.md §3）: 用途別（kind）内訳。chat 行は
        # by_model と同じ由来（messages.answer->'usage'）・intent/embed は usage_events 由来。
        # embed 行は Gemini（batchEmbedContents は usage を返さない）＝全トークン null で
        # 「—」描画（fmtTokOrDash）を実演する。
        "by_kind": [
            {"kind": "chat", "provider": "codex", "model": "gpt-5.5", "calls": 4, "input": 10000,
             "cached_input": 3000, "output": 1500, "reasoning_output": 900},
            {"kind": "intent", "provider": "openai", "model": "gpt-4o-mini", "calls": 3,
             "input": 450, "cached_input": 0, "output": 60, "reasoning_output": 0},
            {"kind": "embed", "provider": "gemini", "model": "gemini-embedding-001", "calls": 2,
             "input": None, "cached_input": None, "output": None, "reasoning_output": None},
        ],
    },
}


def _default_model_catalog() -> dict:
    """組み込み既定の使えるモデル一覧（呼ぶたびに独立した新しい辞書を返す）。"""
    return {
        "openai": {
            "chat": {"allowed": ["gpt-5.5", "gpt-5.4-mini"], "default": "gpt-5.5"},
            "extract": {"allowed": ["gpt-5.5", "gpt-5.4-mini"], "default": "gpt-5.5"},
            "intent": {"allowed": ["gpt-4o-mini", "gpt-5.4-mini"], "default": "gpt-4o-mini"},
            "embed": {"allowed": ["text-embedding-3-small", "text-embedding-3-large"],
                      "default": "text-embedding-3-small"},
            "subsearch": {"allowed": ["gpt-5.4-mini", "gpt-4o-mini"], "default": "gpt-5.4-mini"},
        },
        "gemini": {
            "chat": {"allowed": ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-flash-latest"],
                     "default": "gemini-2.5-flash"},
            "extract": {"allowed": ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-flash-latest"],
                        "default": "gemini-2.5-flash"},
            "intent": {"allowed": ["gemini-2.5-flash"], "default": "gemini-2.5-flash"},
            "embed": {"allowed": ["gemini-embedding-001"], "default": "gemini-embedding-001"},
        },
        "ollama": {
            "chat": {"allowed": ["qwen2.5"], "default": "qwen2.5"},
            "extract": {"allowed": ["qwen2.5"], "default": "qwen2.5"},
            "intent": {"allowed": ["qwen2.5"], "default": "qwen2.5"},
            "subsearch": {"allowed": ["qwen2.5"], "default": "qwen2.5"},
            "embed": {"allowed": ["nomic-embed-text"], "default": "nomic-embed-text"},
        },
        "codex": {"codex": {"allowed": ["gpt-5.5"], "default": "gpt-5.5"}},
    }


# S1（2026-07-08-設定分離とUI整備.md）: GET /admin/settings の既定応答（全体設定の現行値＋実効値）。
# admin-settings.html の描画・保存 e2e 用。configured=None＝未設定（既定/env に従う）状態。
SYSTEM_SETTINGS_VIEW = {
    # クラウド AI プロバイダの中央設定。既定は openai・個人キー許可 OFF（既定 false）・3種のキーとも未設定。
    "cloud": {
        "provider": "openai",
        # FBK-1 RV1（2026-09-01）: 生の保存値（未選択＝一度も PUT されていなければ None）。
        # 既定モックは「クラウドを一度も選んでいない」状態＝None（`provider` は既定込みの実効値）。
        "provider_raw": None,
        "providers": ["openai", "gemini", "bedrock"],
        "personal_api_keys_allowed": False,
        "openai_key_set": False,
        "gemini_key_set": False,
        "bedrock_key_set": False,
        "ollama_url": "http://localhost:11434",
        "personal_keys_in_use_count": 0,
        # WEB-1: Codex の Web 検索を管理者が許可しているか（既定 false）。
        "web_search_allowed": False,
    },
    # 利用者による API キー自己発行の許可トグル（既定 false・個別テストが `system_settings=`
    # 上書きで true に切り替える）。
    "ext_keys": {"user_api_keys_allowed": False, "self_issued_active_count": 0,
                "daily_quota_default": {"configured": None, "effective": 100, "default": 100},
                "research_default_provider": {"configured": None, "effective": "ollama",
                                              "default": "ollama"}},
    # SET-2c: OpenAI 互換 API の接続先（本家／Azure OpenAI／その他 OpenAI 互換）。既定は本家・未設定。
    "openai_endpoint": {
        "configured": {"kind": None, "base_url": None, "auth_header": None, "api_version": None},
        "effective": {"kind": "openai", "base_url": "https://api.openai.com/v1",
                     "auth_header": "bearer", "api_version": ""},
        "kinds": ["openai", "azure", "custom"],
        "auth_headers": ["bearer", "api-key"],
    },
    # 使えるモデル一覧＋用途別既定。`effective`/`builtin` は独立した辞書（同一オブジェクトを共有
    # しない＝PUT ハンドラが `effective` を in-place 更新しても `builtin`（組み込み既定の基準値）が
    # 巻き込まれない）。
    "model_catalog": {
        "configured": None,
        "effective": _default_model_catalog(),
        "builtin": _default_model_catalog(),
        "providers": ["openai", "gemini", "bedrock", "ollama", "codex"],
        "usages": ["chat", "intent", "embed", "route", "subsearch", "codex", "render"],
    },
    "arms": {
        # known_arm_names() はソート済み（markitdown 系は 2026-08 撤去＝現行3本: ooxml/pdf_text/vision）。
        "known": ["ooxml", "pdf_text", "vision"],
        "enabled": ["ooxml", "pdf_text"],
        "configured": None,
        "env_default": ["ooxml", "pdf_text"],
        # pypdf・markitdown[all] は requirements.txt に同梱既定（2026-07-08）＝通常は全アーム available（未導入案内は
        # 出ない）。markitdown_ocr（VLM）も既定ローカル（ollama）＝設定として使える扱い。
        "available": {"ooxml": True, "pdf_text": True, "markitdown": True, "markitdown_ocr": True},
    },
    # ⑤（feedback-batch-2026-07-08）: 視覚読み取り（markitdown_ocr）の VLM 設定。既定＝ローカル（Ollama）・
    # クラウド（OpenAI）は cloud_allowed=true（管理者が明示許可）のときだけ有効。
    "vlm": {
        "configured": None,
        "effective": {"provider": "ollama", "model": "qwen2.5vl", "cloud_allowed": False,
                      "ollama_url": "http://localhost:11434"},
        "default": {"provider": "ollama", "model": "qwen2.5vl", "cloud_allowed": False},
        "available": True,
        "providers": ["ollama", "openai"],
        "openai_key_present": False,
    },
    # W0/W1/W2'（2026-07-08-旧Office変換2系統.md）: 旧形式（.doc/.xls/.ppt）変換バックエンド。既定＝soffice 未検出・
    # office_com は URL 未設定かつ direct（powershell）未検出＝unavailable（不達）。
    "legacy_backend": {
        "configured": None,
        "effective": "none",
        "default": "none",
        "options": ["none", "libreoffice", "office_com"],
        "libreoffice": {"available": False, "version": None},
        "office_com": {"configured_url": False, "mode": "unavailable", "powershell": False,
                       "available": False, "versions": None},
    },
    # L5（2026-09-02-RAG表現の全形式展開と文脈保持.md §8.6-1）: rag.md の LLM 成形トグル。既定 on。
    "rag_llm_render": {"configured": None, "effective": True, "default": True, "options": ["on", "off"]},
    # R2a-S2（2026-07-13 横断レビュー対応）: Ollama 接続先の SSRF allowlist。既定（未設定）は
    # loopback のみ許可＝配下 effective は空（loopback 自体は暗黙許可のためここには出ない）。
    "ollama_allowlist": {"configured": None, "effective": []},
    # PART-6（2026-09-05-Webhook通知.md W3）: Webhook 宛先の SSRF allowlist。`ollama_allowlist` と
    # 同型（既定 loopback のみ許可＝配下 effective は空）。
    "webhook_allowlist": {"configured": None, "effective": []},
    # R1b（2026-07-13 横断レビュー対応・Codex ネイティブ resume・決定5）: Codex resume セッションの
    # 保持日数。既定（未設定）は 0＝無制限。フェーズ7-1（response_model 実測）で発見した実ドリフト
    # 是正＝旧モックはこのキーを欠いていた（実 GET /admin/settings は常に持つ）。
    "codex_session_retention_days": {"configured": None, "effective": 0},
    # STAT-2（2026-08-28-利用統計AIチャット.md 追記）: 利用統計チャット専用の AI 選択。利用者の
    # 実行構成（agent）には依存せず、管理者全体で1つに統一する。未設定時の既定は A7
    # （`cloud`.`provider`・下記）連動——この環境は A7=openai（既定）なので "openai"
    # （実サーバは `usage_chat._default_provider` が同じ規則で計算する・PUT ハンドラでの
    # 再計算は下記 "usage_chat_provider" reflection 参照）。実 GET /admin/settings は常に持つ
    # （real drift 対策・codex_session_retention_days と同じ理由でここに含める）。
    "usage_chat": {"configured": None, "effective": "openai", "default": "openai",
                   "providers": ["openai", "ollama"]},
    # SC-6c（調べる深さの基準値・調べ方ブロック §3.2）: 既定（未設定）は各モジュールの env 既定値
    # （`sherpa/agentic_search.py`/`sherpa/impact_service.py`/`sherpa/lens_service.py`/
    # `sherpa/chat_service.py::QA_MAX_HITS_DEFAULT`・Codex は `SHERPA_CODEX_REASONING` 既定 "low"）。
    "depth_profile": {
        "max_turns": {"configured": None, "effective": 12, "default": 12},
        "grep_max_hits": {"configured": None, "effective": 30, "default": 30},
        "qa_max_hits": {"configured": None, "effective": 20, "default": 20},
        "read_window": {"configured": None, "effective": 40, "default": 40},
        "impact_depth": {"configured": None, "effective": 8, "default": 8},
        "troubleshoot_depth": {"configured": None, "effective": 3, "default": 3},
        "codex_reasoning": {"configured": None, "effective": "low", "default": "low",
                            "options": ["minimal", "low", "medium", "high", "xhigh"]},
    },
    # BUDGET-1（2026-09-02-RAG表現の全形式展開と文脈保持.md §3.4）: agentic search の tool-result
    # バイト予算（1件あたり／1 run 累計）。既定（未設定）は env/コード既定（精度優先・262144/4194304
    # ＝`sherpa/agentic_search.py::TOOL_RESULT_MAX_BYTES`/`TOOL_RESULT_MAX_TOTAL_BYTES` の
    # コード既定値と一致）。
    # BUDGET-2（同 §3.4・2026-09-03 裁定）: `window`（現在のモデルの窓の4段解決結果・
    # `sherpa/model_windows.py::resolve_window_tokens` の出力形）・`model_windows`（管理者登録表・
    # "provider:model" → tokens）。mock は「窓が不明（登録値/API/シードのどれにも無い）」の
    # 既定状態を表す（`sherpa/schemas.py::AgenticBudgetAdminInfo` と同じ形）。
    "agentic_budget": {
        "per_result": {"configured": None, "effective": 262144, "default": 262144},
        "total": {"configured": None, "effective": 4194304, "default": 4194304},
        "window": {"provider": "ollama", "model": "qwen2.5", "window_tokens": None,
                  "source": "unknown", "derived_cap_bytes": None},
        "model_windows": {"configured": None},
    },
}


# S3（mock 契約ドリフト対策）: 以下は元ハンドラ内のインライン応答を定数へホイストしたもの
# （挙動不変）。機械可読レジストリ MOCKED（ファイル末尾）と合わせて、実 API との形状突合
# （tests/api/test_mock_api_contract.py）の対象にしやすくする。

HEALTH_SUMMARY_RESP = {"status": "ok", "checked_at": "2026-07-01T09:00:00+00:00"}

CONFIG_RESP = {"agent": "heuristic", "label": "簡易（AIなし）", "model": "—"}
# GET /config の実応答は agent ごとにラベルが変わる（`sherpa/providers/__init__.py::provider_info`）。
# ハンドラ側（PUT /settings 後の GET /config）が `settings_resp["agent"]` から動的に組み立てるための
# 表示名テーブル（`SETTINGS_RESP.constructs_available` のラベルと揃える・実サーバの厳密な文言とは
# 別物＝あくまでモックの表示整合性のため）。
_CONFIG_AGENT_LABELS = {
    "heuristic": "簡易（AIなし）", "openai": "OpenAI", "gemini": "Gemini（Google）",
    "ollama": "ローカル（Ollama）", "codex": "Codex", "bedrock": "AWS Bedrock (Claude)",
}


# A7（クラウドプロバイダ排他選択）対象の頭脳（実サーバ `sherpa.keys.CLOUD_PROVIDERS`）。
_CLOUD_AGENTS = ("openai", "gemini", "bedrock")
# env で明示的に有効化していない限り使えない頭脳（実サーバ `agent_constructs._RUNTIME_BLOCKABLE`）。
_RUNTIME_BLOCKABLE = ("gemini", "bedrock")
# codex_model_provider の allowlist（実サーバ `agent_constructs.CODEX_MODEL_PROVIDERS`）。
_CODEX_MODEL_PROVIDERS = ("openai", "ollama")


def _enabled_agents(resp: dict) -> set:
    """このモックでの「有効な頭脳」集合。`constructs_available` に列挙されている agent 名がそれ
    （このモックは実サーバと違い、一覧を A7 で動的に絞り込まない＝一覧に載っている＝
    `SHERPA_EXTRA_AGENTS` 等で有効化済み、とみなせる）。"""
    return {c.get("agent") for c in (resp.get("constructs_available") or [])}


def _recompute_construct_id(resp: dict) -> str:
    """`resp["agent"]`/`resp["codex_model_provider"]` から construct_id を導出する
    （実サーバ `sherpa/agent_constructs.py::construct_id`/`effective_agent` と同じ規則）。codex は
    codex_model_provider（既定 openai）で codex_openai/codex_ollama を区別する。空でない未知の値・
    文字列以外の非 None 値（`False`/`0`/`{}`/`[]` 等・PUT /settings の型/allowlist 検証をすり抜けた
    壊れた既存データ相当）は実サーバと同じく一覧に無い "codex_invalid" を返す（一覧外の値を
    codex_openai へ丸めない＝画面に実際と異なる構成が動いているという食い違いを見せない・
    `str(x or "")` 単独の truthiness 判定は falsy な非文字列を「未設定」に化けさせるため使わない）。
    クラウド系（openai/gemini/bedrock）は、**有効化されている場合に限り**選択中の cloud_provider
    と一致しなければ ollama へ正規化する（A7）。有効化されていない agent（例: env で許可していない
    bedrock）は実サーバ同様、正規化せず生値のまま保つ（`effective_agent()` は非有効な raw_agent を
    素通りさせ、A7 判定にも進まない）。それ以外は `constructs_available`（標準4＋有効化した
    追加頭脳）から agent が一致する id を探し、見つからなければ agent 名そのものを返す。"""
    agent = resp.get("agent") or ""
    if agent == "codex":
        raw = resp.get("codex_model_provider")
        if raw is None or raw == "":
            provider = ""
        elif not isinstance(raw, str):
            return "codex_invalid"
        else:
            provider = raw.strip().lower()
        if not provider or provider == "openai":
            return "codex_openai"
        if provider == "ollama":
            return "codex_ollama"
        return "codex_invalid"
    enabled = _enabled_agents(resp)
    if agent in _CLOUD_AGENTS and agent in enabled and (resp.get("cloud_provider") or "openai") != agent:
        agent = "ollama"
    for c in resp.get("constructs_available") or []:
        if c.get("agent") == agent:
            return c["id"]
    return agent


# GET /settings の実形状（sherpa/routers/system.py::_public_settings）: web_search_available・
# codex_web_search・bedrock_model・bedrock_key_set が旧モックには無かった
# （S3 実ドリフト是正）。web/settings.js は全キーを読む（S3 事前分析どおり load() で全て使用）。
# RV MED（2026-07-15）: bedrock_model_known・bedrock_model_label は「保存できるのは実在確認済みID
# だけ」の締め（Codex RV 指摘）に伴い `_public_settings` へ追加された新フィールド。既定値は静的
# choices の1つ＝known:true（tests/e2e/test_settings_ui.py が settings= 上書きで unknown/legacy
# シナリオを個別に検証する）。
SETTINGS_RESP = {"agent": "openai",   # construct_id="openai_only" と一致させる（実サーバは両方同じ raw agent から導出）
                 "web_search_available": False, "codex_web_search": False,
                 "openai_key_set": True,
                 # S3（2026-08-18-AzureOpenAI対応）: 既定（env 未設定）は openai・ホスト名なし
                 # （画面は注記を出さない）。Azure 表示は tests/e2e 側で settings= 上書きにより検証する。
                 "openai_endpoint_kind": "openai", "openai_base_url_host": "",
                 "gemini_key_set": False,
                 "ollama_url": "http://localhost:11434",
                 "bedrock_model": "jp.anthropic.claude-haiku-4-5-20251001-v1:0",
                 "bedrock_key_set": False,
                 "bedrock_model_known": True,
                 "bedrock_model_label": "Claude Haiku 4.5（JP 推論プロファイル・既定）",
                 # 4構成（2026-08-15・sherpa/agent_constructs.py）: 画面はこの一覧だけを描画する。
                 # 既定（env 未設定）の実サーバと同じく標準4件のみ＝gemini/bedrock は出さない。
                 "codex_model_provider": "",
                 # 検索アシスタント（2026-08-15）: 既定は未設定＝メインのAIが自分で検索する。
                 "search_helper": "",
                 # 旧・個人上書き時代のモデル指定（読み取り専用・注記表示のみ・保存経路は無い）。
                 "search_helper_model": "",
                 "construct_id": "openai_only",
                 # 既定モックは「個人キー許可＝true・選択中プロバイダ＝openai」にそろえる
                 # （A6/A7 導入前から存在する大多数の e2e がキー入力欄の可視を前提にしているため）。
                 # A6 が false のときの挙動（キー欄が隠れる）は個別テストが `settings=` 上書きで検証する。
                 "cloud_provider": "openai", "personal_api_keys_allowed": True,
                 # 既定は利用者発行を許可しない（個別テストが
                 # `settings={**SETTINGS_RESP, "user_api_keys_allowed": True}` で上書き）。
                 "user_api_keys_allowed": False,
                 "user_api_keys_daily_quota_default": 100,
                 # モデル名欄ごとの選択肢（system.py::_public_settings が model_catalog.FIELD_CELLS の
                 # 全フィールドを常に含める）。
                 "model_catalog": {
                     "openai_model": {"allowed": ["gpt-5.5", "gpt-5.4-mini"], "default": "gpt-5.5"},
                     "gemini_model": {"allowed": ["gemini-2.5-flash", "gemini-2.5-flash-lite",
                                                  "gemini-flash-latest"], "default": "gemini-2.5-flash"},
                     "ollama_model": {"allowed": ["qwen2.5"], "default": "qwen2.5"},
                     "codex_model": {"allowed": ["gpt-5.5"], "default": "gpt-5.5"},
                     "intent_model": {"allowed": ["gpt-4o-mini", "gpt-5.4-mini", "gemini-2.5-flash", "qwen2.5"],
                                      "default": "gpt-4o-mini"},
                     "search_helper_model": {"allowed": ["gpt-5.4-mini", "gpt-4o-mini", "qwen2.5"],
                                              "default": "gpt-5.4-mini"},
                 },
                 # 個人の Ollama 接続先 <select> の選択肢（allowed は完全 URL）。legacy は
                 # 「保存値が許可されなくなった旧接続先」用の別枠（実サーバは常にキーを持つ・
                 # 該当なしは None・mock drift 是正）。
                 "ollama_url_choice": {"allowed": ["http://localhost:11434"],
                                      "default": "http://localhost:11434", "legacy": None},
                 "constructs_available": [
                     {"id": "openai_only", "agent": "openai", "codex_model_provider": None,
                      "label": "OpenAI", "hint": "OpenAI API に直結（速い）"},
                     {"id": "ollama_only", "agent": "ollama", "codex_model_provider": None,
                      "label": "ローカル（Ollama）", "hint": "このパソコン/社内のローカルLLM"},
                     {"id": "codex_openai", "agent": "codex", "codex_model_provider": "openai",
                      "label": "Codex（OpenAI）", "hint": "Codex が自分で grep して調べる・モデルは OpenAI"},
                     {"id": "codex_ollama", "agent": "codex", "codex_model_provider": "ollama",
                      "label": "Codex（Ollama）", "hint": "Codex が自分で grep して調べる・モデルは Ollama"},
                 ],
                 # intent_model／search_helper_model のプロバイダ別選択肢（実サーバの
                 # `_model_choice_table_by_provider`。セレクタ変更時の再描画を検証する e2e が使う）。
                 "model_catalog_by_provider": {
                     "intent_model": {
                         "openai": {"allowed": ["gpt-4o-mini", "gpt-5.4-mini"], "default": "gpt-4o-mini"},
                         "gemini": {"allowed": ["gemini-2.5-flash"], "default": "gemini-2.5-flash"},
                         "ollama": {"allowed": ["qwen2.5"], "default": "qwen2.5"},
                     },
                     "search_helper_model": {
                         "openai": {"allowed": ["gpt-5.4-mini", "gpt-4o-mini"], "default": "gpt-5.4-mini"},
                         "ollama": {"allowed": ["qwen2.5"], "default": "qwen2.5"},
                     },
                 },
                 "system_prompt": "既定の方針"}

# 追加AI（gemini/bedrock）を `SHERPA_EXTRA_AGENTS` で有効化した環境の応答。これらの AI を扱う
# テスト（Bedrock のモデル検証・機能ごとの AI に gemini を選ぶ等）はこちらを使う。
SETTINGS_RESP_WITH_EXTRA_AGENTS = {
    **SETTINGS_RESP,
    "constructs_available": [
        *SETTINGS_RESP["constructs_available"],
        {"id": "bedrock", "agent": "bedrock", "codex_model_provider": None,
         "label": "AWS Bedrock (Claude)", "hint": "AWS 経由の Claude"},
        {"id": "gemini", "agent": "gemini", "codex_model_provider": None,
         "label": "Gemini（Google）", "hint": "Google の Gemini API"},
    ],
}


WORLD_OPTIONS_RESP = {"worlds": ["w1"], "labels": {"w1": "4期更改"}}

GRAPH_FACETS_RESP = {"relationship_types": ["COPIES", "CONTAINS", "INVOKES", "DOCUMENTS"],
                     "condition_fields": ["category", "phase", "role", "status"],
                     "node_labels": ["Batch", "DataItem", "Document", "Module"],
                     "node_labels_ja": {"Module": "プログラム", "Batch": "バッチ"}}

# GET /worlds/{wid}/status（sherpa/routers/worlds.py::world_status）: last_synced_at/
# last_run_status/last_run_warnings は旧モックに無かった（S3 実ドリフト是正）。
# フェーズ7-1（response_model 実測）で発見した実ドリフト是正: 実 GET /worlds/{wid}/status
# （corpus_docs.py::scan_report 経由）は scanned/by_doctype/skipped_ext も常に持つ（旧モックは欠落）。
# ING-1/ING-2: `failure_reason_catalog`/`partial_extraction_advice` は静的辞書（実体は
# `sherpa.ingest.failure_reasons`）だがこの mock ファイルは sherpa を import しない流儀のため、
# 同じ形（`dict[str, {"label","advice"}]`）の代表例を手書きする（TypeAdapter は形だけを見る）。
_FAILURE_REASON_CATALOG_MOCK = {
    "legacy_conversion_timeout": {"label": "タイムアウト", "advice": "時間をおいて再試行してください。"},
    "other": {"label": "その他の失敗", "advice": "管理者にお問い合わせください。"},
}
_PARTIAL_EXTRACTION_ADVICE_MOCK = "本文の一部しか読み取れていない可能性があります。開いて確認し、必要なら保存し直すか再変換してください。"

WORLD_STATUS_RESP = {"ok": True, "world_id": "w1", "label": "4期更改", "root_path": "/mnt/c/ProjectA",
                     "last_synced_at": "2026-07-03T09:00:00+00:00",
                     "scanned": 3, "indexed": 3, "by_doctype": {"設計書": 1, "ソース": 2},
                     "office_md": 1, "skipped_office": 0, "office_failed": 0,
                     "skipped_other": 0, "skipped_ext": {}, "analyzer_declined": 0,
                     "analyzer_declined_as_document": 0, "unreadable": 0,
                     "counts_as_of": "2026-07-03T09:00:00+00:00",
                     "graph_nodes": 4, "graph_edges": 3, "es_chunks": 6,
                     "last_run_id": 500,
                     "last_run_status": "auto_published", "last_run_warnings": [], "last_run_blocked": [],
                     "last_run_flags_total": 0, "last_run_flags_truncated": False,
                     "failed_files": None, "partial_extraction_suspected": None, "stage_summary": None,
                     "running_progress": None,
                     "failure_reason_catalog": _FAILURE_REASON_CATALOG_MOCK,
                     "partial_extraction_advice": _PARTIAL_EXTRACTION_ADVICE_MOCK}

CHAT_TURNS_RUNNING_EMPTY = {"turns": []}

# S3: GET /conversations は実形状（store.list_conversations）と同じキー集合を全行に持たせる
# （own/received_share で値は違うが、キーは揃える＝実 SQL が LEFT JOIN で常に全カラムを返すため）。
CONVERSATIONS_LIST = [
    {"id": 101, "title": "消費税率の相談", "version": "v1", "pinned": False,
     "updated_at": "2026-07-01T09:00:00+00:00", "origin": "own", "read_only": False,
     "received_at": None, "shared_by_user_id": None, "shared_by_name": None, "share_status": None},
    {"id": 202, "title": "共有された障害調査", "version": "v1", "pinned": False,
     "updated_at": "2026-07-01T09:30:00+00:00", "origin": "received_share", "read_only": True,
     "received_at": "2026-07-01T09:30:00+00:00", "shared_by_user_id": "admin",
     "shared_by_name": "管理者", "share_status": "active"},
]


_OPENAI_ENDPOINT_KINDS = frozenset({"openai", "azure", "custom"})
_OPENAI_AUTH_HEADERS = frozenset({"bearer", "api-key"})


def _mock_validate_openai_base_url(base: str) -> str | None:
    """`sherpa/llm.py::assert_openai_base_url_allowed` の簡易再現。

    純関数（I/O なし）＝`PUT /admin/settings` と `POST /admin/settings/openai-endpoint-test` の
    両モックが同じ判定を共有する契約（enum・https・userinfo・query・port の各検証を実バックエンドと
    揃え、e2e が本来 422 になるはずの入力を素通りさせない）。妥当なら `None`、不正ならエラー文言。
    """
    try:
        p = urlparse(base)
    except ValueError:
        return "不正な接続先 URL です（解析できません）"
    if not p.hostname:
        return "接続先 URL にホスト名がありません"
    if p.username or p.password:
        return "接続先 URL にユーザー情報（user:pass@）を含められません"
    try:
        p.port   # 遅延評価＝明示アクセスしないと非数値/範囲外ポートが素通りする（urlparse の仕様）。
    except ValueError:
        return "接続先 URL のポート番号が不正です"
    if p.query or p.fragment:
        return "接続先 URL にクエリ/フラグメントを含められません（API バージョンは別欄で設定してください）"
    if p.scheme != "https":
        return "接続先 URL は https:// のみ許可されます（API キーを平文送信しないため）"
    return None


def _mock_validate_openai_endpoint_fields(body: dict) -> tuple[str, str] | None:
    """`PUT /admin/settings` の openai_endpoint_* 4項目と `POST /admin/settings/openai-endpoint-test`
    の同名 4 フィールドの両方から呼べる共通検証（純関数）。不正なら `(422 用の理由文字列, フィールド名)`
    を返し、妥当なら `None`。enum（kind/auth_header）・base_url（`_mock_validate_openai_base_url`）を
    body に含まれるものだけ検証する（実サーバの部分更新と同じ＝未指定キーは検証しない・クロス検証
    （kind!=openai は実効 base_url 必須）は別関数 `_mock_validate_openai_endpoint_cross` が
    マージ後の値で行う）。
    """
    kind = body.get("openai_endpoint_kind")
    if kind is not None and kind not in _OPENAI_ENDPOINT_KINDS:
        return f"openai_endpoint_kind は openai/azure/custom のいずれかです: {kind!r}", "openai_endpoint_kind"
    auth_header = body.get("openai_auth_header")
    if auth_header is not None and auth_header not in _OPENAI_AUTH_HEADERS:
        return f"openai_auth_header は bearer/api-key のいずれかです: {auth_header!r}", "openai_auth_header"
    base_url = body.get("openai_base_url")
    if base_url:
        err = _mock_validate_openai_base_url(base_url)
        if err:
            return err, "openai_base_url"
    return None


def _mock_openai_endpoint_pending(configured: dict, body: dict) -> dict:
    """`PUT /admin/settings` の実サーバ側 `_eff_kind`/`_eff_base` 計算
    （`sherpa/routers/system_extras.py::admin_settings_put`）・接続テストの `pending`
    （`admin_openai_endpoint_test`）が両方とも行う「現在の実効値 + この1回の body の該当フィールド
    で上書き」を、mock 側でも同じ形（long キー）で組み立てる（PUT/POST 共有・純関数）。

    `configured`（mock の view: `openai_endpoint.configured`・短縮キー kind/base_url/auth_header/
    api_version）を long キー（`openai_endpoint_kind` 等）へ変換したものを土台にし、`body` に
    含まれるフィールドだけで上書きする（未指定キーは現在値を維持＝部分更新）。
    """
    pending = {
        "openai_endpoint_kind": configured.get("kind"),
        "openai_base_url": configured.get("base_url"),
        "openai_auth_header": configured.get("auth_header"),
        "openai_api_version": configured.get("api_version"),
    }
    for field in ("openai_endpoint_kind", "openai_base_url", "openai_auth_header", "openai_api_version"):
        if field in body:
            pending[field] = body[field]
    return pending


def _mock_infer_openai_endpoint_kind(explicit_kind: str | None, base_url: str) -> str:
    """kind 未指定なら base_url の host から推定する（`sherpa/llm.py::openai_endpoint_kind()` の
    host 推定の簡易再現・PUT/POST 共有）。"""
    if explicit_kind:
        return explicit_kind
    if base_url:
        host = base_url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
        return "azure" if host.endswith((".openai.azure.com", ".services.ai.azure.com")) else "custom"
    return "openai"


def _mock_validate_openai_endpoint_cross(kind: str, base_url: str) -> str | None:
    """`llm.assert_openai_endpoint_consistent()` と同じクロス検証（kind が openai 以外なら実効
    base_url が必須）を PUT/POST 共有の純関数として切り出す。マージ後
    （`_mock_openai_endpoint_pending`）の値で判定すること＝`{"openai_endpoint_kind": "azure"}`
    単独の送信でも、現在の実効 base_url が空なら 422（body 単体でなく実効値で見る）。"""
    if kind != "openai" and not base_url:
        return "接続先が「OpenAI 本家」以外のときは、接続先 URL（openai_base_url）が必要です"
    return None


# 調べる深さの基準値（`sherpa/routers/system_extras.py::SystemSettingsReq` の StrictInt+Field(ge,le)
# と同じ範囲）。整数以外・bool・範囲外はすべて 422（実 API の pydantic 検証を模す）。
_DEPTH_BASE_INT_BOUNDS = {
    "depth_base_max_turns": (1, 200),
    "depth_base_grep_max_hits": (1, 1000),
    "depth_base_qa_max_hits": (1, 1000),
    "depth_base_read_window": (10, 400),
    "depth_base_impact_depth": (1, 64),
    "depth_base_troubleshoot_depth": (1, 16),
}
_DEPTH_BASE_CODEX_REASONING_LEVELS = ("minimal", "low", "medium", "high", "xhigh")

# BUDGET-1（2026-09-02-RAG表現の全形式展開と文脈保持.md §3.4）: agentic search の tool-result
# バイト予算（`sherpa/routers/system_extras.py::SystemSettingsReq` の StrictInt+Field(ge,le) と
# 同じ範囲）。`_mock_validate_depth_base_int` を共用する（型/範囲の判定形は depth_base_* と同じ）。
_AGENTIC_BUDGET_INT_BOUNDS = {
    "agentic_budget_per_result": (1024, 8 * 1024 * 1024),
    "agentic_budget_total": (4096, 64 * 1024 * 1024),
}


def _mock_validate_agentic_budget(body: dict):
    """`admin_settings_put` の agentic_budget_* 検証を模す純関数（`_mock_validate_depth_base` と
    同型）。問題なければ None、あれば実 API と同形の `detail`。"""
    for key, (lo, hi) in _AGENTIC_BUDGET_INT_BOUNDS.items():
        if key not in body:
            continue
        val = body[key]
        if val is None:
            continue   # 未設定へ戻す（有効な選択）
        err = _mock_validate_depth_base_int(key, val, lo, hi)
        if err:
            return err
    return None


# BUDGET-2（同 §3.4）: `model_context_windows`（"provider:model" → tokens）の簡易検証（実 API の
# `sherpa.model_windows.validate_model_windows` の主要チェックだけを模す・e2e の UI 操作を
# 支えるのが目的で、実 API の全パターンの再現はしない——網羅は `tests/unit/test_model_windows.py`）。
def _mock_validate_model_windows(body: dict):
    if "model_context_windows" not in body:
        return None
    val = body["model_context_windows"]
    if val is None:
        return None   # 未設定へ戻す
    if not isinstance(val, dict):
        return [{"loc": ["body", "model_context_windows"], "msg": "オブジェクトで指定してください"}]
    for k, v in val.items():
        if not isinstance(k, str) or ":" not in k or not k.split(":", 1)[1].strip():
            return [{"loc": ["body", "model_context_windows"],
                     "msg": f"'provider:model' 形式で指定してください: {k!r}"}]
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            return [{"loc": ["body", "model_context_windows"],
                     "msg": f"model_context_windows[{k}] は正の整数で指定してください"}]
    return None


def _mock_normalize_codex_reasoning(val):
    """`depth_base_codex_reasoning` の正規化（実 API の `_validate_depth_base_codex_reasoning`
    と同じ `strip().lower()`）。文字列以外・None はそのまま返す（型検証は呼び出し側の責務）。"""
    return val.strip().lower() if isinstance(val, str) else val


def _mock_validate_depth_base_int(key: str, val, lo: int, hi: int):
    """整数項目1つ分の pydantic 風エラー detail（実 API の StrictInt+Field(ge,le) と同形の
    リスト）。問題なければ None。"""
    if isinstance(val, bool) or not isinstance(val, int):
        return [{"type": "int_type", "loc": ["body", key],
                 "msg": "Input should be a valid integer", "input": val}]
    if val < lo:
        return [{"type": "greater_than_equal", "loc": ["body", key],
                 "msg": f"Input should be greater than or equal to {lo}", "input": val, "ctx": {"ge": lo}}]
    if val > hi:
        return [{"type": "less_than_equal", "loc": ["body", key],
                 "msg": f"Input should be less than or equal to {hi}", "input": val, "ctx": {"le": hi}}]
    return None


def _mock_validate_depth_base(body: dict):
    """`admin_settings_put` の depth_base_* 検証を模す純関数（PUT ハンドラ共有）。問題なければ
    None、あれば実 API と同形の `detail`（整数6項目の型/範囲違反と `depth_base_codex_reasoning`
    の型違反は pydantic 風のリスト、語彙不一致だけは実装の `HTTPException` と同じ文字列）を返す。
    語彙判定は正規化後（`strip().lower()`）の値で行う——大文字・前後空白も実 API と同じく受理する。"""
    for key, (lo, hi) in _DEPTH_BASE_INT_BOUNDS.items():
        if key not in body:
            continue
        val = body[key]
        if val is None:
            continue   # 未設定へ戻す（有効な選択）
        err = _mock_validate_depth_base_int(key, val, lo, hi)
        if err:
            return err
    if "depth_base_codex_reasoning" in body:
        val = body["depth_base_codex_reasoning"]
        if val is not None:
            if not isinstance(val, str):
                return [{"type": "string_type", "loc": ["body", "depth_base_codex_reasoning"],
                         "msg": "Input should be a valid string", "input": val}]
            if _mock_normalize_codex_reasoning(val) not in _DEPTH_BASE_CODEX_REASONING_LEVELS:
                options = "/".join(_DEPTH_BASE_CODEX_REASONING_LEVELS)
                return f"depth_base_codex_reasoning は {options} のいずれかで指定してください"
    return None


def install_api_mocks(page, *, auth_status: int = 200, user: dict | None = None,
                      login_status: int = 200, bedrock_models: dict | None = None,
                      bedrock_verify: dict | None = None,
                      health_components: list | None = None, usage_stats: dict | None = None,
                      system_settings: dict | None = None, settings: dict | None = None,
                      stream_events: list | None = None, extra_users: list | None = None,
                      tools_availability: dict | None = None, notifications: list | None = None):
    current_user = user or USER_ADMIN
    # GET /admin/users の既定2件（admin/sato）に加え、呼び出し元が `extra_users=` で行を追加
    # できる（例: pending 状態のユーザーなど、既定の POST/PATCH mock 経路では作れない状態を
    # 直接テストするため）。他の `xxx=` override と同じ流儀＝深いコピーで USERS 定数から切り離す。
    users = [dict(u) for u in USERS] + [dict(u) for u in (extra_users or [])]
    workspace_files = [dict(f) for f in WORKSPACE_FILES]
    announcements = [dict(a) for a in ANNOUNCEMENTS]
    # NOTIFY-1: GET /notifications の既定応答（既定は空＝home UI の既存テストへ影響させない・
    # 呼び出し元は `notifications=[...]` で通知の表示/操作を個別に検証できる）。
    notifications_resp = [dict(n) for n in (notifications or [])]
    # S1: GET /admin/settings の既定応答（呼び出し元が `system_settings=` で上書き可・
    # 「既に configured 済みの値がある」状態を模すテスト用）。
    # PUT /admin/settings は system_settings_resp を in-place 更新する（`.clear()`/`.update()`）
    # ため、ここで深いコピーせずモジュール定数 `SYSTEM_SETTINGS_VIEW` を直接参照すると、1テストの
    # PUT がそのままモジュール定数を書き換えてしまい、他テスト（`install_api_mocks(page)` を
    # `system_settings=` 省略で呼ぶ全テスト）まで汚染する。`settings_resp`（直下）と同じ流儀＝
    # 深いコピーで切り離す。
    system_settings_resp = json.loads(json.dumps(
        system_settings if system_settings is not None else SYSTEM_SETTINGS_VIEW))
    # RV MED（2026-07-15）: GET /settings の既定応答（呼び出し元が `settings=` で丸ごと上書き可・
    # bedrock_model_known/legacy 表示の分岐シナリオを試すテスト用・bedrock_models 等と同じ流儀）。
    # PUT /settings がこの dict をその場で更新する（保存→再読込の往復を検証するため・RV 是正）ため、
    # 呼び出し元が渡した dict／モジュール定数 `SETTINGS_RESP` をそのまま参照すると、この install_api_mocks
    # 呼び出し（1テスト）の変更が他テスト・モジュール定数まで汚染してしまう。深いコピーで切り離す。
    settings_resp = json.loads(json.dumps(settings if settings is not None else SETTINGS_RESP))
    # S6: GET /settings/bedrock-models の既定応答（呼び出し元が `bedrock_models=` で上書き可・
    # 失敗系フローを試すテストのため）。ユーザー指名の Sonnet 4.6 を含めて動的取得の見た目を再現。
    bedrock_models_resp = bedrock_models if bedrock_models is not None else {"models": [
        {"id": "jp.anthropic.claude-haiku-4-5-20251001-v1:0", "label": "Claude Haiku 4.5（JP 推論プロファイル）"},
        {"id": "us.anthropic.claude-sonnet-4-6-20260115-v1:0", "label": "Claude Sonnet 4.6（US 推論プロファイル）"},
    ], "error": None}
    # バッチ2・1番: POST /settings/bedrock-models/verify の既定応答（呼び出し元が `bedrock_verify=` で
    # 上書き可・成功/失敗いずれのフローも試せるように）。既定は成功（{ok, id, label}）を模す。
    bedrock_verify_resp = bedrock_verify
    # UI フィードバック4: GET /admin/health の既定応答（呼び出し元が `health_components=` で上書き可）。
    health_components_resp = health_components if health_components is not None else _HEALTH_COMPONENTS_DEFAULT
    # バッチ3: GET /admin/usage/stats の既定応答（呼び出し元が `usage_stats=` で丸ごと上書き可・
    # 空データ描画確認は `usage_stats={}`（totals/daily/users 等が無い状態）で呼ぶ）。
    usage_stats_resp = usage_stats if usage_stats is not None else USAGE_STATS_DEFAULT
    # SC-6e: GET /chat/tools-availability の既定応答（呼び出し元が `tools_availability=`
    # で不達を模せる・既定は3経路とも到達可＝既存 e2e の「全チップ表示」前提を崩さない）。
    tools_availability_resp = tools_availability if tools_availability is not None else {
        "grep": True, "fulltext": True, "graph": True}
    records = {
        "stream_urls": [],
        "turn_starts": [],
        "turn_stream_urls": [],
        "turn_stops": [],
        "settings_put": [],
        "settings_test": [],
        "bedrock_models_fetch": [],
        "bedrock_models_verify": [],
        "admin_health": [],
        "admin_usage_stats": [],
        "world_diff": [],
        "world_register": [],
        "world_refresh": [],
        "auth_login": [],
        "auth_logout": [],
        "share_create": [],
        "users_suggest": [],
        "admin_users_post": [],
        "admin_users_patch": [],
        "audit_queries": [],
        "audit_exports": [],
        "workspace_uploads": [],
        "workspace_delete": [],
        "workspace_search": [],
        "workspace_downloads": [],
        "doc_downloads": [],
        "es_search": [],
        "graph_ask": [],
        "announcement_create": [],
        "announcement_patch": [],
        "admin_settings_put": [],
        # 外部連携 API キー（管理者/利用者）。
        "ext_key_admin_create": [],
        "ext_key_admin_revoke": [],
        "ext_key_self_create": [],
        "ext_key_self_revoke": [],
        "admin_openai_endpoint_test": [],
    }
    # 発行済み API キーの簡易台帳（プレーンキーは発行応答にだけ含め、ここには保持しない）。
    # `owner_uid=None`＝admin 発行（従来どおり誰でも使えるシステムキー）・非 None＝利用者自己発行。
    ext_keys_store: list = []

    def handler(route):
        request = route.request
        parsed = urlparse(request.url)
        path = parsed.path
        method = request.method.upper()
        query = parse_qs(parsed.query)

        if method == "GET" and path == "/auth/me":
            if auth_status == 200:
                return _json(route, auth_me_response(current_user))
            return _json(route, {"detail": "認証が必要です"}, status=auth_status)
        if method == "GET" and path == "/health/summary":
            # nav.js の状態ドットは /auth/me の成否と無関係にポーリングを開始するため、
            # このモックが無いと全ページで 404 → down 表示になってしまう。
            # 実サーバはログイン必須（未ログイン=401）なので auth 状態に忠実に追随する。
            if auth_status != 200:
                return _json(route, {"detail": "認証が必要です"}, status=401)
            return _json(route, HEALTH_SUMMARY_RESP)
        if method == "GET" and path == "/admin/health":
            records["admin_health"].append(query)
            if current_user.get("role") != "admin":
                return _json(route, {"detail": "管理者権限が必要です"}, status=403)
            return _json(route, {"status": "ok", "checked_at": "2026-07-03T09:00:00+00:00",
                                 "ttl_seconds": 15, "components": health_components_resp})
        if method == "GET" and path == "/admin/usage/stats":
            records["admin_usage_stats"].append(query)
            if current_user.get("role") != "admin":
                return _json(route, {"detail": "管理者権限が必要です"}, status=403)
            return _json(route, usage_stats_resp)
        if method == "GET" and path == "/admin/settings":
            if current_user.get("role") != "admin":
                return _json(route, {"detail": "管理者権限が必要です"}, status=403)
            return _json(route, system_settings_resp)
        if method == "PUT" and path == "/admin/settings":
            body = _post_json(request)
            records["admin_settings_put"].append(body)
            if current_user.get("role") != "admin":
                return _json(route, {"detail": "管理者権限が必要です"}, status=403)
            _depth_base_err = _mock_validate_depth_base(body)
            if _depth_base_err:
                return _json(route, {"detail": _depth_base_err}, status=422)
            _agentic_budget_err = _mock_validate_agentic_budget(body)
            if _agentic_budget_err:
                return _json(route, {"detail": _agentic_budget_err}, status=422)
            _model_windows_err = _mock_validate_model_windows(body)
            if _model_windows_err:
                return _json(route, {"detail": _model_windows_err}, status=422)
            # 反映済みビューを返す（簡易: 送られたキーだけ configured/effective を更新）。
            view = json.loads(json.dumps(system_settings_resp))
            if "arms_enabled" in body:
                val = body["arms_enabled"] or None
                view["arms"]["configured"] = val
                view["arms"]["enabled"] = val if val else view["arms"]["env_default"]
            if "legacy_backend" in body and "legacy_backend" in view:
                # Med4（RV 2026-07-08）: null（未設定へ戻す）時は "none" 固定でなく view の default
                # （env 既定・呼び出し元が system_settings= で libreoffice 等に上書き可）へ倒す
                # （arms_enabled の env_default フォールバックと同じ流儀）。
                val = body["legacy_backend"]
                view["legacy_backend"]["configured"] = val
                view["legacy_backend"]["effective"] = val if val else view["legacy_backend"].get("default", "none")
            if "vlm" in body and "vlm" in view:
                # ⑤: null（未設定へ戻す）は default（env/既定）へ、それ以外は送られた値を effective に反映（簡易）。
                val = body["vlm"]
                view["vlm"]["configured"] = val
                base = dict(view["vlm"].get("default") or {})
                view["vlm"]["effective"] = {**view["vlm"]["effective"], **base, **(val or {})} if val \
                    else {**view["vlm"]["effective"], **base}
            # クラウド AI プロバイダの中央設定（簡易反映・key_set は値の有無のみ）。
            if "cloud" in view:
                if "cloud_provider" in body:
                    view["cloud"]["provider"] = body["cloud_provider"] or "openai"
                    # FBK-1 RV1: PUT された生値をそのまま反映する（null で送られたら未選択に戻る・
                    # 実サーバは cloud_provider を送らない限り raw を書き換えない=ここも "cloud_provider"
                    # in body のときだけ触る）。
                    view["cloud"]["provider_raw"] = body["cloud_provider"] or None
                if "personal_api_keys_allowed" in body:
                    view["cloud"]["personal_api_keys_allowed"] = bool(body["personal_api_keys_allowed"])
                    if not body["personal_api_keys_allowed"]:
                        # A6: OFF 保存は個人キーを一括削除する（簡易反映）。
                        view["cloud"]["personal_keys_in_use_count"] = 0
                if "web_search_allowed" in body:
                    # WEB-1: 破壊的副作用は無い（チャットの Web 検索行の表示条件のみ）。
                    view["cloud"]["web_search_allowed"] = bool(body["web_search_allowed"])
                for field, flag in (("openai_api_key", "openai_key_set"),
                                    ("gemini_api_key", "gemini_key_set"),
                                    ("bedrock_api_key", "bedrock_key_set")):
                    if field in body:
                        view["cloud"][flag] = bool(body[field])
                if "ollama_url" in body:
                    view["cloud"]["ollama_url"] = body["ollama_url"] or "http://localhost:11434"
                # 実サーバは vlm.openai_key_present を毎回動的に計算する（`vision_arm._openai_key()`）
                # ため、中央 OpenAI キーの有無が変わったらここも追従させる（キー削除後に VLM の
                # キー未設定警告が古いままにならないことを e2e で固定するため）。
                if "openai_api_key" in body and "vlm" in view:
                    view["vlm"]["openai_key_present"] = bool(body["openai_api_key"])
            # AI 下調べ検索の既定 AI（簡易反映・null は "ollama" へ戻す）。"openai" への変更は
            # 実サーバの保存時 preflight（`_assert_research_default_provider_sendable`）と同じく、
            # この PUT 適用後の実効状態で中央 OpenAI キーが無ければ 422 で拒否する（キー設定と
            # 同一 PUT で送られた場合も反映済みの `view["cloud"]["openai_key_set"]` を見る）。
            # 原子性: この preflight は下の user_api_keys_allowed（`ext_keys_store` を直接
            # 書き換える副作用を持つ）より**前**に置く——後ろに置くと、同一 PUT に両方の変更が
            # 含まれる場合、この 422 で拒否されたにもかかわらず自己発行キーの失効だけが
            # 実行済みになってしまう（`view` 自体は最後の一括コミットまで反映されないが、
            # `ext_keys_store` はこことは独立に即座へ書き換わるため）。
            if "ext_keys" in view and "research_default_provider" in body:
                val = body["research_default_provider"]
                if val == "openai" and not (view.get("cloud") or {}).get("openai_key_set"):
                    return _json(route, {"detail": "AI 下調べ検索の既定 AI を OpenAI にできません"
                                                   "（管理者が AI プロバイダのキーを設定してください）"},
                                status=422)
                view["ext_keys"]["research_default_provider"]["configured"] = val
                view["ext_keys"]["research_default_provider"]["effective"] = val or "ollama"
            # 利用者による API キー自己発行の許可トグル（personal_api_keys_allowed と同じ簡易
            # 反映＝OFF 保存で件数を0にし、未失効の利用者発行キーを一括失効する）。
            if "ext_keys" in view and "user_api_keys_allowed" in body:
                view["ext_keys"]["user_api_keys_allowed"] = bool(body["user_api_keys_allowed"])
                if not body["user_api_keys_allowed"]:
                    view["ext_keys"]["self_issued_active_count"] = 0
                    for row in ext_keys_store:
                        if row.get("owner_uid") is not None and row.get("revoked_at") is None:
                            row["revoked_at"] = "2026-08-25T00:00:00+00:00"
            if "ext_keys" in view and "user_api_keys_daily_quota_default" in body:
                val = body["user_api_keys_daily_quota_default"]
                view["ext_keys"]["daily_quota_default"]["configured"] = val
                view["ext_keys"]["daily_quota_default"]["effective"] = val or 100
            # SET-2c: OpenAI 互換 API の接続先（簡易反映・実サーバ（sherpa/llm.py）の規則を再現する）:
            # kind 未指定なら base_url の host から推定（azure サフィックス／それ以外は custom）・
            # kind!=openai で base_url が空なら 422（PUT /admin/settings と同じクロス検証・ただし
            # 実サーバ同様、この PUT で kind/base_url のどちらかに実際に触れた時だけ再検証する＝
            # 既に保存済みの他フィールドだけを更新する PUT を巻き込んで誤って 422 にしない）。
            # kind=openai なら base_url/auth_header/api_version を常に無視する。
            if "openai_endpoint" in view:
                oe = view["openai_endpoint"]
                touches_endpoint = "openai_endpoint_kind" in body or "openai_base_url" in body
                # enum（kind/auth_header）・base_url（https/userinfo/query/port）を共有 validator で
                # 検証してから反映する。クロス検証（kind!=openai は実効 base_url 必須）は「現在の
                # 実効値 + この body」のマージ後の値で行う（`_mock_openai_endpoint_pending`／
                # `_mock_validate_openai_endpoint_cross` は POST /admin/settings/openai-endpoint-test
                # とも共有する純関数）。
                _err = _mock_validate_openai_endpoint_fields(body)
                if _err:
                    return _json(route, {"detail": _err[0]}, status=422)
                pending = _mock_openai_endpoint_pending(oe["configured"], body)
                kind = _mock_infer_openai_endpoint_kind(
                    pending.get("openai_endpoint_kind"), pending.get("openai_base_url") or "")
                base_url = pending.get("openai_base_url") or ""
                if touches_endpoint:
                    _cross_err = _mock_validate_openai_endpoint_cross(kind, base_url)
                    if _cross_err:
                        return _json(route, {"detail": _cross_err}, status=422)
                for field, key in (("openai_endpoint_kind", "kind"), ("openai_base_url", "base_url"),
                                  ("openai_auth_header", "auth_header"),
                                  ("openai_api_version", "api_version")):
                    if field in body:
                        oe["configured"][key] = body[field]
                oe["effective"]["kind"] = kind
                oe["effective"]["auth_header"] = oe["configured"].get("auth_header") or "bearer"
                oe["effective"]["api_version"] = oe["configured"].get("api_version") or ""
                if kind == "openai":
                    oe["effective"]["base_url"] = "https://api.openai.com/v1"
                else:
                    oe["effective"]["base_url"] = base_url or "https://api.openai.com/v1"
            # model_catalog: 全置換の契約（実サーバ `model_catalog.get_catalog`）を模す。
            # 組み込み既定（`builtin`）を土台に、送られた `configured` を丸ごと重ねて `effective` を
            # 再構築する（送られなかったセル/プロバイダは組み込み既定へ戻る＝部分的な `configured`
            # で1セルだけを未設定へ戻すリセットが正しく反映されるようにする・単純な「送られたセル
            # だけ上書き」だと、消えたセルが古い effective に残ってしまい false green になる）。
            if "model_catalog" in body and "model_catalog" in view:
                val = body["model_catalog"]
                view["model_catalog"]["configured"] = val
                base = _default_model_catalog()
                if val:
                    for provider, usages in val.items():
                        base.setdefault(provider, {})
                        for usage, cell in usages.items():
                            base[provider][usage] = cell
                view["model_catalog"]["effective"] = base
            # 決定 2026-08-24 #4: Ollama 許可ホスト一覧。
            if "ollama_allowlist" in body and "ollama_allowlist" in view:
                val = body["ollama_allowlist"]
                view["ollama_allowlist"]["configured"] = val
                view["ollama_allowlist"]["effective"] = sorted(val) if val else []
            # PART-6（2026-09-05-Webhook通知.md W3）: Webhook 宛先の許可ホスト一覧（同型）。
            if "webhook_allowlist" in body and "webhook_allowlist" in view:
                val = body["webhook_allowlist"]
                view["webhook_allowlist"]["configured"] = val
                view["webhook_allowlist"]["effective"] = sorted(val) if val else []
            # STAT-2: 利用統計チャット専用の AI 選択。実サーバ（`usage_chat._default_provider`）
            # と同じ A7 連動の既定（`cloud_provider` が openai のときだけ openai・それ以外は
            # ollama）を簡易再現する。
            if "usage_chat" in view:
                default_provider = "openai" if (view.get("cloud", {}).get("provider") == "openai") else "ollama"
                view["usage_chat"]["default"] = default_provider
                if "usage_chat_provider" in body:
                    val = body["usage_chat_provider"]
                    view["usage_chat"]["configured"] = val
                    view["usage_chat"]["effective"] = val if val else default_provider
                elif view["usage_chat"].get("configured") is None:
                    view["usage_chat"]["effective"] = default_provider
            # SC-6c: 調べる深さの基準値（簡易反映・null は default へ戻す）。
            if "depth_profile" in view:
                for _key, _put in (("max_turns", "depth_base_max_turns"),
                                   ("grep_max_hits", "depth_base_grep_max_hits"),
                                   ("qa_max_hits", "depth_base_qa_max_hits"),
                                   ("read_window", "depth_base_read_window"),
                                   ("impact_depth", "depth_base_impact_depth"),
                                   ("troubleshoot_depth", "depth_base_troubleshoot_depth")):
                    if _put in body:
                        _val = body[_put]
                        view["depth_profile"][_key]["configured"] = _val
                        view["depth_profile"][_key]["effective"] = (
                            _val if _val is not None else view["depth_profile"][_key]["default"])
                if "depth_base_codex_reasoning" in body:
                    # 検証済み（ここに来る時点で _mock_validate_depth_base 通過済み）につき
                    # 正規化後の値（実 API の _validate_depth_base_codex_reasoning と同じ
                    # strip().lower() 後の値）を state へ反映する。
                    _val = _mock_normalize_codex_reasoning(body["depth_base_codex_reasoning"])
                    view["depth_profile"]["codex_reasoning"]["configured"] = _val
                    view["depth_profile"]["codex_reasoning"]["effective"] = (
                        _val if _val is not None else view["depth_profile"]["codex_reasoning"]["default"])
            # BUDGET-1（§3.4）: agentic search の tool-result バイト予算（簡易反映・null は default
            # へ戻す・depth_profile と同型）。
            if "agentic_budget" in view:
                for _key, _put in (("per_result", "agentic_budget_per_result"),
                                   ("total", "agentic_budget_total")):
                    if _put in body:
                        _val = body[_put]
                        view["agentic_budget"][_key]["configured"] = _val
                        view["agentic_budget"][_key]["effective"] = (
                            _val if _val is not None else view["agentic_budget"][_key]["default"])
            # BUDGET-2（§3.4）: モデル窓の登録表（簡易反映・追加/上書き/削除の永続化のみ——実 API の
            # min() 適用・4段解決の再現はしない＝そこは tests/unit・tests/api の対象・モジュール
            # docstring の比較粒度の方針どおり）。
            if "model_context_windows" in body and "agentic_budget" in view:
                view["agentic_budget"]["model_windows"]["configured"] = body["model_context_windows"]
            # PUT を状態保持型にする（GET へ反映）: system_settings_resp から使い捨てのコピーを
            # 作って PUT 自身の応答だけに返すと、後続の GET /admin/settings は常にこの
            # install_api_mocks 呼び出し時点の初期値のまま（PUT の変更が一切反映されない）になり、
            # PUT→GET の往復（保存→再読込）を検証する e2e テストが実バックエンドとの忠実度を
            # 失う。同じ dict オブジェクトを in-place で更新する（再代入だと GET 側のクロージャ
            # 参照が古いままになるため）。
            system_settings_resp.clear()
            system_settings_resp.update(view)
            # 個人設定（settings_resp）側のクラウド選択も同期する。PUT /settings の A7 判定
            # （_new_agent と cloud_provider の一致確認）はこの値を参照するため、古いまま放置すると
            # 管理画面で切り替えた直後の判定が実サーバと食い違う（拒否すべきを許可・許可すべきを拒否）。
            if "cloud_provider" in body:
                settings_resp["cloud_provider"] = view["cloud"]["provider"]
            return _json(route, view)
        if method == "POST" and path == "/admin/settings/openai-endpoint-test":
            # SET-2c: 接続先の接続テストは admin 専用の別ルートに分離済み（個人設定用
            # /settings/test の流用をやめた）。
            body = _post_json(request)
            records["admin_openai_endpoint_test"].append(body)
            if current_user.get("role") != "admin":
                return _json(route, {"detail": "管理者権限が必要です"}, status=403)
            provider = body.get("provider") or "openai"
            # 未知 provider は通信前に 422（実サーバと同じ・authz probe が安全に決定的失敗できる
            # 契約でもある＝ALLOW_PROBE_BODY 参照）。
            if provider not in ("openai", "codex"):
                return _json(route, {"detail": "provider は openai / codex のいずれか"}, status=422)
            # `codex_model` は受け付けない（常にカタログ既定＝組み込み既定 "gpt-5.5"・admin が
            # カタログを実際のデプロイ名へ変更していれば反映する）。
            model = "gpt-5.5"
            if provider == "codex":
                model = ((system_settings_resp.get("model_catalog") or {}).get("effective", {})
                        .get("codex", {}).get("codex", {}).get("default")) or "gpt-5.5"
            # 保存前の入力値でも共有 validator を通す（不正な組み合わせは 422・実通信しない）。
            _err = _mock_validate_openai_endpoint_fields(body)
            if _err:
                return _json(route, {"detail": _err[0]}, status=422)
            # PUT と同じ「現在の実効値 + この body」のマージ後でクロス検証する（実サーバの
            # `pending = dict(sys_s); if req.X is not None: pending[X] = ...` と同じ形）。
            # `{"openai_endpoint_kind": "azure"}` 単独でも、現在の実効 base_url が空なら 422 になる。
            _oe_configured = (system_settings_resp.get("openai_endpoint") or {}).get("configured") or {}
            _pending = _mock_openai_endpoint_pending(_oe_configured, body)
            _kind = _mock_infer_openai_endpoint_kind(
                _pending.get("openai_endpoint_kind"), _pending.get("openai_base_url") or "")
            _cross_err = _mock_validate_openai_endpoint_cross(_kind, _pending.get("openai_base_url") or "")
            if _cross_err:
                return _json(route, {"detail": _cross_err}, status=422)
            return _json(route, {"ok": True, "provider": provider, "model": model, "detail": "接続OK"})
        if method == "POST" and path == "/auth/login":
            body = _post_json(request)
            records["auth_login"].append(body)
            if login_status == 200:
                # S3: 実形状（sherpa/routers/auth.py::auth_login）＝{ok,uid,must_change_password,next}
                # 封筒（旧「user 丸ごと」は撤去）。login.js は must_change_password しか読まないため
                # UI 挙動は不変。
                must_change = bool(current_user.get("must_change_password"))
                return _json(route, {"ok": True, "uid": current_user["uid"],
                                     "must_change_password": must_change,
                                     "next": "/ui/change-password.html" if must_change else None},
                             headers={"Set-Cookie": "sherpa_session=fake-session; Path=/; HttpOnly; SameSite=Lax"})
            return _json(route, {"detail": "invalid credentials"}, status=login_status)
        if method == "POST" and path == "/auth/logout":
            records["auth_logout"].append(True)
            return _json(route, {"ok": True})
        if method == "GET" and path == "/admin/users":
            return _json(route, {"users": users})
        if method == "POST" and path == "/admin/users":
            body = _post_json(request)
            records["admin_users_post"].append(body)
            # RV「バッチ2」4番（2026-07-03）: 既存 uid への「作成」は 409 で拒否する実サーバの挙動を再現。
            if any(row["uid"] == body.get("uid") for row in users):
                return _json(route, {"detail": "このユーザーIDは既に存在します"}, status=409)
            created = {"uid": body.get("uid"), "display_name": body.get("display_name") or "",
                       "role": body.get("role") or "user", "status": "active", "last_login_at": None}
            users.append(created)
            return _json(route, {"ok": True, "user": created})
        if method == "PATCH" and path.startswith("/admin/users/"):
            uid = path.rsplit("/", 1)[-1]
            body = _post_json(request)
            records["admin_users_patch"].append({"uid": uid, **body})
            # USR-1 RV2/RV3: 実サーバは現在値との実差分だけを更新・監査し、差分0件なら422に
            # する。「指定されたか」は is not None で判定する（truthy 判定だと空文字が
            # 「未指定」と誤認され、下の allowlist 検証をすり抜けてしまう）。role/status は
            # 実サーバと同じ allowlist（active/disabled・user/admin）を適用前に検証し、範囲外
            # なら422（実サーバの sherpa/routers/admin_users.py と同じ契約）。password は
            # 差分判定できないため常に「変更」扱い。
            role_in = body.get("role")
            status_in = body.get("status")
            if status_in is not None and status_in not in ("active", "disabled"):
                return _json(route, {"detail": "status は active / disabled のみ"}, status=422)
            if role_in is not None and role_in not in ("user", "admin"):
                return _json(route, {"detail": "role は user / admin のみ"}, status=422)
            changed = False
            for row in users:
                if row["uid"] != uid:
                    continue
                if status_in is not None and status_in != row.get("status"):
                    row["status"] = status_in
                    changed = True
                if role_in is not None and role_in != row.get("role"):
                    row["role"] = role_in
                    changed = True
                if "display_name" in body and body["display_name"] is not None \
                        and body["display_name"] != row.get("display_name"):
                    row["display_name"] = body["display_name"]
                    changed = True
                if body.get("password"):
                    changed = True
            if not changed:
                return _json(route, {"detail": "変更フィールドがありません"}, status=422)
            return _json(route, {"ok": True, "uid": uid})
        if method == "GET" and path == "/notifications":
            return _json(route, {"notifications": notifications_resp})
        if method == "GET" and path == "/announcements":
            include_unpub = (query.get("include_unpublished") or ["false"])[0] == "1"
            rows = announcements if include_unpub else [
                a for a in announcements if a.get("published", True) and _ann_status(a) == "active"]
            return _json(route, {"announcements": rows})
        if method == "POST" and path == "/admin/announcements":
            body = _post_json(request)
            records["announcement_create"].append(body)
            next_id = max([a["id"] for a in announcements] + [900]) + 1
            row = {"id": next_id, "author_uid": current_user["uid"], "title": body.get("title", ""),
                  "body": body.get("body", ""), "category": body.get("category", "notice"),
                  "pinned": bool(body.get("pinned", False)), "published": bool(body.get("published", True)),
                  "publish_at": body.get("publish_at") or None, "expire_at": body.get("expire_at") or None,
                  "created_at": "2026-07-02T00:00:00+00:00", "updated_at": "2026-07-02T00:00:00+00:00"}
            row["status"] = _ann_status(row)
            announcements.append(row)
            return _json(route, {"ok": True, "announcement": row})
        if method == "PATCH" and path.startswith("/admin/announcements/"):
            aid = int(path.rsplit("/", 1)[-1])
            body = _post_json(request)
            records["announcement_patch"].append({"id": aid, **body})
            row = next((a for a in announcements if a["id"] == aid), None)
            if row is None:
                return _json(route, {"detail": "お知らせが見つかりません"}, status=404)
            for k in ("title", "body", "category", "pinned", "published"):
                if k in body and body[k] is not None:
                    row[k] = body[k]
            # publish_at/expire_at は書込専用キーと同じ流儀: 未指定=変更しない・""=NULLへクリア。
            for k in ("publish_at", "expire_at"):
                if k in body and body[k] is not None:
                    row[k] = body[k] or None
            row["status"] = _ann_status(row)
            return _json(route, {"ok": True, "announcement": row})
        if method == "GET" and path == "/admin/audit":
            records["audit_queries"].append(query)
            # S3: 実形状（sherpa/routers/audit_usage.py::admin_audit_list）は rows に
            # count/offset/limit を伴う（旧モックは rows のみ）。
            rows = _filtered_audit_rows(query)
            offset = int((query.get("offset") or ["0"])[0])
            limit = int((query.get("limit") or ["100"])[0])
            return _json(route, {"rows": rows, "count": len(rows), "offset": offset, "limit": limit})
        if method == "GET" and path == "/admin/audit/export":
            records["audit_exports"].append(query)
            fmt = (query.get("format") or ["csv"])[0]
            body = "id,action\n1,auth.login\n" if fmt == "csv" else '{"id":1,"action":"auth.login"}\n'
            return route.fulfill(
                status=200,
                body=body,
                headers={
                    "content-type": "text/csv" if fmt == "csv" else "application/x-ndjson",
                    "content-disposition": f'attachment; filename="sherpa-audit-20260701-120000.{fmt}"',
                },
            )
        if method == "GET" and path == "/workspace/files":
            return _json(route, {"files": workspace_files})
        if method == "POST" and path == "/workspace/files":
            filename = _multipart_filename(request)
            records["workspace_uploads"].append({"filename": filename})
            next_id = max([f["id"] for f in workspace_files] + [500]) + 1
            workspace_files.append({"id": next_id, "rel_path": filename, "size_bytes": 18,
                                    "created_at": "2026-07-01T10:00:00+00:00",
                                    "expires_at": "2026-07-08T10:00:00+00:00"})
            return _json(route, workspace_upload_response(next_id, filename))
        if method == "DELETE" and path.startswith("/workspace/files/"):
            file_id = int(path.rsplit("/", 1)[-1])
            records["workspace_delete"].append(file_id)
            deleted = next((f for f in workspace_files if f["id"] == file_id), None)
            workspace_files[:] = [f for f in workspace_files if f["id"] != file_id]
            return _json(route, workspace_delete_response(file_id, (deleted or {}).get("rel_path", "")))
        if method == "GET" and path == "/workspace/search":
            records["workspace_search"].append(query)
            q = (query.get("q") or [""])[0]
            return _json(route, workspace_search_response(q))
        if method == "GET" and path == "/documents/download":
            records["doc_downloads"].append(query)
            rel = (query.get("rel") or ["file.md"])[0]
            name = rel.rsplit("/", 1)[-1]
            return route.fulfill(
                status=200,
                body="# 原本（モック）\n本文はテスト用のダミーです。",
                headers={"content-type": "text/markdown; charset=utf-8",
                         "content-disposition": f'attachment; filename="{name}"'},
            )
        # P1-c（Codex 強化計画 Phase1）: 「作成したファイル」カードの DL リンク先。
        if method == "GET" and path.startswith("/workspace/files/") and path.endswith("/download"):
            records["workspace_downloads"].append(path)
            fid = path.split("/")[-2]
            return route.fulfill(
                status=200,
                body="Codex が作成したファイル（モック本文）",
                headers={"content-type": "application/octet-stream",
                         "content-disposition": f'attachment; filename="created_{fid}.bin"'},
            )
        if method == "GET" and path == "/worlds":
            return _json(route, {"worlds": [WORLD]})
        if method == "GET" and path == "/world-options":
            return _json(route, WORLD_OPTIONS_RESP)
        if method == "GET" and path == "/chat/tools-availability":
            return _json(route, tools_availability_resp)
        if method == "GET" and path == "/config":
            # `settings_resp`（PUT /settings で更新される・上記参照）の agent/モデルを反映する
            # （静的な CONFIG_RESP をそのまま返すと、頭脳メニューでの切替直後に `loadConfig()`
            # がバッジを古い agent へ戻してしまい、直後の保存操作が誤った agent 宛てに飛ぶ）。
            # モデル名は個人設定に無い＝bedrock は個人設定の bedrock_model、それ以外は管理者の
            # 使えるモデル一覧（model_catalog）の既定から解決する（実サーバの provider_info() が
            # 実際に解決したプロバイダの .model を返すのと同じ形）。
            _agent_now = settings_resp.get("agent") or "heuristic"
            _label = _CONFIG_AGENT_LABELS.get(_agent_now, _agent_now)
            if _agent_now == "bedrock":
                _model = settings_resp.get("bedrock_model") or "—"
            else:
                _cell = (settings_resp.get("model_catalog") or {}).get(f"{_agent_now}_model") or {}
                _model = _cell.get("default") or "—"
            return _json(route, {"agent": _agent_now, "label": _label, "model": _model})
        if method == "GET" and path == "/settings":
            return _json(route, settings_resp)
        if method == "PUT" and path == "/settings":
            body = _post_json(request)
            records["settings_put"].append(body)
            # 有効化していない頭脳（gemini/bedrock）の保存は実サーバ同様 422 で拒否する
            # （`sherpa/routers/system.py::settings_put` の `agent_constructs.runtime_blocked` 相当）。
            # A7（次段の cloud_provider 一致チェック）より先に見る＝実サーバと同じ順序
            # （そもそも使えない頭脳を、たまたま cloud_provider が一致するからと通さない）。
            _new_agent = body.get("agent")
            _enabled = _enabled_agents(settings_resp)
            if _new_agent in _RUNTIME_BLOCKABLE and _new_agent not in _enabled:
                return _json(route, {"detail": "この AI はこの環境では利用できません"
                                               "（管理者が有効化していません）"}, status=422)
            # A7（クラウドプロバイダ排他選択）: 有効化されている前提で、選択中でないクラウド系
            # agent の保存は 422 で拒否する（`agent_constructs.agent_requires_unselected_cloud`
            # 相当・保存済み値は変更しない）。
            if (_new_agent in _CLOUD_AGENTS and _new_agent in _enabled
                    and _new_agent != (settings_resp.get("cloud_provider") or "openai")):
                return _json(route, {"detail": "この AI は現在選択されているクラウドプロバイダではありません"
                                               "（管理画面でプロバイダを切り替えるか、別の AI を選んでください）"},
                            status=422)
            # codex_model_provider は openai/ollama の allowlist（実サーバ
            # `sherpa/routers/system.py::settings_put` の同名チェック相当）。空文字/未指定は
            # 「既定へ戻す」として許可し、それ以外の未知の値は保存前に 422 で拒否する
            # （壊れた値を DB に書かせない＝`_recompute_construct_id` の "codex_invalid" は
            # 既存の壊れたデータを表示するための縮退であって、新規保存の抜け道ではない）。
            # 実サーバは Pydantic フィールド `str | None` の型検証で、文字列以外の非 None 値
            # （`False`/`0`/`{}`/`[]` 等）をこの分岐に来る前に 422 で弾く——`if _new_codex_provider`
            # という truthiness 判定だけだとこれらの falsy な非文字列をすり抜けさせてしまうため、
            # 型チェックを allowlist チェックより先に行う。
            _new_codex_provider = body.get("codex_model_provider")
            if _new_codex_provider is not None and not isinstance(_new_codex_provider, str):
                return _json(route, {"detail": "codex_model_provider は文字列である必要があります"},
                            status=422)
            if _new_codex_provider and _new_codex_provider not in _CODEX_MODEL_PROVIDERS:
                return _json(route, {"detail": "codex_model_provider は openai / ollama のいずれか"},
                            status=422)
            # 個人設定に無いフィールド（旧モデル名・機能別プロバイダ）は settings_resp に
            # キー自体が無いため、下のマージ（`if k in settings_resp`）で自然に無視される
            # （pydantic の未知フィールド無視と同じ挙動を模す）。null は「未指定＝変更しない」
            # （実サーバ `settings_put` の `req.model_dump().items() if v is not None` と同じ意味論）。
            # 保存後の GET /settings が反映済みの値を返すよう簡易マージする（実サーバは
            # `_public_settings(store.get_settings(uid))` を返す＝保存前後で同じキー集合。ここでは
            # `settings_resp`（このクロージャが保持する現在値）をその場で更新する）。
            _secret_key_fields = {"openai_api_key": "openai_key_set", "gemini_api_key": "gemini_key_set",
                                  "bedrock_api_key": "bedrock_key_set"}
            for k, v in body.items():
                if v is None:
                    continue
                if k in _secret_key_fields:
                    settings_resp[_secret_key_fields[k]] = bool(v)
                    continue
                if k in settings_resp:
                    settings_resp[k] = v
            # construct_id は agent/codex_model_provider から導出される値（実サーバ
            # `agent_constructs.construct_id`）＝保存済みの生値をそのまま返すキーではない。
            # ここで再計算しないと、agent を変える保存の直後に GET /settings を読む e2e が
            # 「新しい agent なのに古い construct_id」という実サーバでは起きない不整合を見てしまう。
            settings_resp["construct_id"] = _recompute_construct_id(settings_resp)
            return _json(route, settings_resp)
        if method == "POST" and path == "/settings/test":
            body = _post_json(request)
            records["settings_test"].append(body)
            provider = body.get("provider")
            # モデル名は個人上書きが無い＝Bedrock だけ例外（実在確認済みモデルの専用機構）。
            # 他は本文に何が入っていても無視し、カタログ既定（モック側の GET /settings 応答が
            # 持つ既定値）のみで解決する（実サーバの `model_catalog.resolve_model` に対応）。
            if provider == "bedrock":
                model = body.get("bedrock_model") or settings_resp.get("bedrock_model") or "—"
            else:
                model = (settings_resp.get("model_catalog", {}).get(f"{provider}_model", {})
                        .get("default") or "gpt-5.5")
            return _json(route, {"ok": True, "provider": provider, "model": model,
                                 "detail": "接続OK"})
        if method == "GET" and path == "/settings/bedrock-models":
            records["bedrock_models_fetch"].append(True)
            return _json(route, bedrock_models_resp)
        if method == "POST" and path == "/settings/bedrock-models/verify":
            body = _post_json(request)
            records["bedrock_models_verify"].append(body)
            model_id = body.get("model_id") or ""
            resp = bedrock_verify_resp if bedrock_verify_resp is not None else {
                "ok": True, "id": model_id, "label": f"{model_id}（検証済み）"}
            return _json(route, resp)
        if method == "GET" and path == "/conversations":
            return _json(route, CONVERSATIONS_LIST)
        if method == "GET" and path.startswith("/conversations/"):
            cid_str = path.rsplit("/", 1)[-1]
            if cid_str == "102":
                # RV再検証 HIGH#1 回帰: clarify/停止等で assistant 応答が無い user-only ターンが
                # 積み上げから消えないこと（1件目のユーザー発言には応答が無い）。
                return _json(route, {"conversation": {"id": 102, "title": "clarify入り会話", "origin": "own",
                                                       "version": "v1", "read_only": False,
                                                       "contains_personal_workspace": False},
                                     "messages": [
                                         {"role": "user", "content": "それ、直して",
                                          "created_at": "2026-07-01T08:40:00+00:00"},
                                         {"role": "user", "content": "消費税率の変更点を直して",
                                          "created_at": "2026-07-01T08:41:00+00:00"},
                                         {"role": "assistant", "answer": IMPACT_ANSWER, "trace": ANSWER_TRACE,
                                          "created_at": "2026-07-01T08:41:20+00:00"},
                                     ]})
            if cid_str == "103":
                # RV再検証 HIGH#2 回帰: 自分の会話は全ターン trace 無しでも積み上げる
                # （「（記録なし）」の積み上げ・placeholder のままにしない）。
                return _json(route, {"conversation": {"id": 103, "title": "記録なし会話", "origin": "own",
                                                       "version": "v1", "read_only": False,
                                                       "contains_personal_workspace": False},
                                     "messages": [
                                         {"role": "user", "content": "こんにちは",
                                          "created_at": "2026-07-01T08:30:00+00:00"},
                                         {"role": "assistant", "answer": IMPACT_ANSWER, "trace": None,
                                          "created_at": "2026-07-01T08:30:10+00:00"},
                                     ]})
            if cid_str == "104":
                # 受領共有: trace を返さない既存 posture。右ペインは既定のプレースホルダのまま
                # （積み上げを描画しない）。
                return _json(route, {"conversation": {"id": 104, "title": "共有された障害調査",
                                                       "origin": "received_share", "version": "v1",
                                                       "read_only": True,
                                                       "contains_personal_workspace": False},
                                     "messages": [
                                         {"role": "user", "content": "原因は？",
                                          "created_at": "2026-07-01T08:20:00+00:00"},
                                         {"role": "assistant", "answer": IMPACT_ANSWER, "trace": None,
                                          "created_at": "2026-07-01T08:20:10+00:00"},
                                     ]})
            if cid_str == "108":
                # RV Med（Codex 2026-07-07）: sanitized 共有側の確認カードは store 側で
                # answer={"lens":"clarify"}（question 無し）に縮退する。受領共有 origin でも
                # chat.js のプレースホルダ分岐（「（確認のやり取り）」）に入り、空白/崩れ表示にならない。
                return _json(route, {"conversation": {"id": 108, "title": "共有用（サニタイズ済み会話）",
                                                       "origin": "received_share", "version": "v1",
                                                       "read_only": True,
                                                       "contains_personal_workspace": False},
                                     "messages": [
                                         {"role": "user", "content": "税率を変えたら夜間バッチが落ちる？",
                                          "created_at": "2026-07-01T09:40:00+00:00"},
                                         {"role": "assistant", "content": "どの調べ方をしますか？",
                                          "answer": {"lens": "clarify"}, "trace": None,
                                          "created_at": "2026-07-01T09:40:05+00:00"},
                                     ]})
            if cid_str == "105":
                # UIフィードバック（2026-07-03・AI回答のMarkdown表示）: 履歴ロードでも太字/コード/
                # リスト/コードブロックがMD整形され、XSS注入はエスケープされたまま残ることの回帰用。
                md_answer = {**IMPACT_ANSWER, "headline": (
                    "**結論**\n"
                    "- `list_docs` で確認\n"
                    "- 対象は **6件**\n"
                    "```\npath_prefix=4期更改\n```\n"
                    "<img src=x onerror=alert(1)> と [link](javascript:alert(1)) は無害\n"
                    # RV LOW（2026-07-03）: code fence 内からの </pre> 脱出と、esc() 済み実体の再解釈も固定。
                    "```\n</code></pre><img src=x onerror=window.__xss=1>\n```\n"
                    "&lt;img src=x onerror=window.__xss=2&gt; と &amp;amp; はそのまま見える")}
                return _json(route, {"conversation": {"id": 105, "title": "MD表示確認", "origin": "own",
                                                       "version": "v1", "read_only": False,
                                                       "contains_personal_workspace": False},
                                     "messages": [
                                         {"role": "user", "content": "4期更改資料はどのくらいある？",
                                          "created_at": "2026-07-01T09:10:00+00:00"},
                                         {"role": "assistant", "answer": md_answer, "trace": ANSWER_TRACE,
                                          "created_at": "2026-07-01T09:10:20+00:00"},
                                     ]})
            if cid_str == "106":
                # P1-c（Codex 強化計画 Phase1）: 履歴ロードでも「作成したファイル」カードが
                # 表示されること（answer JSONB 保存経由の再現・ライブ SSE を経由しない）。
                author_answer = {
                    "lens": "author", "headline": "消費税率の一覧をExcelにまとめました。",
                    "route": {"path": ["文書を検索", "資料を作成"]}, "summary": {"total": 1},
                    "scope": {"world": "w1", "scope_paths": [], "source": "all"},
                    "data": {"citations": [{"doc_id": "4期/02_設計/01_基本設計/税計算仕様書.md",
                                            "quote": "消費税率は10%", "span": [3, 3]}]},
                    "sources": [{"doc_id": "4期/02_設計/01_基本設計/税計算仕様書.md",
                                "download_url": "/documents/download?world=w1&rel=x"}],
                    "created_files": [{"name": "消費税率一覧.xlsx",
                                       "download_url": "/workspace/files/501/download"}],
                }
                return _json(route, {"conversation": {"id": 106, "title": "資料作成の履歴", "origin": "own",
                                                       "version": "v1", "read_only": False,
                                                       "contains_personal_workspace": False},
                                     "messages": [
                                         {"role": "user", "content": "消費税率の一覧をExcelにまとめて",
                                          "created_at": "2026-07-01T09:20:00+00:00"},
                                         {"role": "assistant", "answer": author_answer, "trace": ANSWER_TRACE,
                                          "created_at": "2026-07-01T09:20:20+00:00"},
                                     ]})
            if cid_str == "107":
                # S1（ask_user-improvements.md）: 保存された確認カード（answer.question）の履歴復元。
                # 1つ目の確認は回答済み（それ以降の user に同じ「確認ID:」がある＝選択内容つき・disabled）、
                # 2つ目の確認は未回答（最新＝操作可能のまま）。
                q1 = {"interaction_id": "lens-aaa111", "mode": "single", "prompt": "どの調べ方をしますか？",
                      "options": [{"id": "impact", "label": "影響範囲", "description": "変更の波及を調べる"},
                                  {"id": "qa", "label": "仕様・内容", "description": "資料を検索する"}],
                      "allow_free_text": False, "original_message": "税率を変えたい"}
                q2 = {"interaction_id": "lens-bbb222", "mode": "single", "prompt": "どの範囲で調べますか？",
                      "options": [{"id": "all", "label": "全体", "description": ""},
                                  {"id": "sub", "label": "この部分だけ", "description": ""}],
                      "allow_free_text": True, "original_message": "範囲を絞りたい"}
                return _json(route, {"conversation": {"id": 107, "title": "確認カード復元の確認", "origin": "own",
                                                       "version": "v1", "read_only": False,
                                                       "contains_personal_workspace": False},
                                     "messages": [
                                         {"role": "user", "content": "税率を変えたい",
                                          "created_at": "2026-07-01T10:00:00+00:00"},
                                         {"role": "assistant", "answer": {"lens": "clarify", "question": q1},
                                          "trace": ANSWER_TRACE, "created_at": "2026-07-01T10:00:05+00:00"},
                                         # 回答（[data-ask-submit] が組み立てる整形文＝確認ID を含む）。
                                         {"role": "user",
                                          "content": "確認事項: どの調べ方をしますか？\n確認ID: lens-aaa111\n選択: 影響範囲\n元の依頼: 税率を変えたい",
                                          "created_at": "2026-07-01T10:00:30+00:00"},
                                         {"role": "assistant", "answer": IMPACT_ANSWER, "trace": ANSWER_TRACE,
                                          "created_at": "2026-07-01T10:00:40+00:00"},
                                         {"role": "user", "content": "範囲を絞りたい",
                                          "created_at": "2026-07-01T10:01:00+00:00"},
                                         {"role": "assistant", "answer": {"lens": "clarify", "question": q2},
                                          "trace": ANSWER_TRACE, "created_at": "2026-07-01T10:01:05+00:00"},
                                     ]})
            if cid_str == "109":
                # S4-e: 保存済みターンの再描画＝計画ノード（id="plan"）＋名前空間化ノード
                # （sub:{profile_id}:）を含む trace の復元と、usage_subs（プロファイル別内訳）の
                # additive 表示を固定する（PLAN_TRACE/PLAN_ANSWER 参照）。
                return _json(route, {"conversation": {"id": 109, "title": "計画付き調査の履歴", "origin": "own",
                                                       "version": "v1", "read_only": False,
                                                       "contains_personal_workspace": False},
                                     "messages": [
                                         {"role": "user", "content": "消費税率を変えたい",
                                          "created_at": "2026-07-01T11:00:00+00:00"},
                                         {"role": "assistant", "answer": PLAN_ANSWER, "trace": PLAN_TRACE,
                                          "created_at": "2026-07-01T11:00:20+00:00"},
                                     ]})
            if cid_str == "110":
                # EXT-2/EV-0（拡張設計 §4.4）: 書き出し（menus.js::_answerLines）の根拠/参考2区分を
                # 固定する（test_export_menu_txt_and_md_split_grounded_and_reference_sources 参照）。
                # exportChat() は GET /conversations/{cid} から再取得したメッセージを書き出すため、
                # ライブ SSE と同じ answer 形（sources_verified 付き）をここでも返す必要がある。
                ev0_answer = {
                    "lens": "qa", "headline": "確認しました。",
                    "route": {"path": ["文書を検索"]}, "summary": {"total": 1},
                    "scope": {"world": "w1", "scope_paths": [], "source": "all"},
                    "data": {"citations": [{"doc_id": "4期/02_設計/01_基本設計/税計算仕様書.md",
                                            "quote": "消費税率は10%", "span": [3, 3]}]},
                    "sources": [
                        {"doc_id": "4期/02_設計/01_基本設計/税計算仕様書.md",
                         "download_url": "/documents/download?world=w1&rel=x"},
                        {"doc_id": "参考資料.md", "download_url": "/documents/download?world=w1&rel=y"},
                    ],
                    "sources_verified": ["4期/02_設計/01_基本設計/税計算仕様書.md"],
                }
                return _json(route, {"conversation": {"id": 110, "title": "書き出し2区分確認", "origin": "own",
                                                       "version": "v1", "read_only": False,
                                                       "contains_personal_workspace": False},
                                     "messages": [
                                         {"role": "user", "content": "消費税率は?",
                                          "created_at": "2026-07-01T12:00:00+00:00"},
                                         {"role": "assistant", "answer": ev0_answer, "trace": None,
                                          "created_at": "2026-07-01T12:00:10+00:00"},
                                     ]})
            if cid_str == "111":
                # EXT-4（拡張設計 §10）: trace_version=2 の保存済みターンを、ライブ時と同じ階層描画
                # （サブエージェント レーン・集約・「実行の分担」サマリ）で復元できることを固定する
                # （render.js の TraceTreeV2 をストリーミングと共通で使う契約）。
                return _json(route, {"conversation": {"id": 111, "title": "階層表示の履歴", "origin": "own",
                                                       "version": "v1", "read_only": False,
                                                       "contains_personal_workspace": False},
                                     "messages": [
                                         {"role": "user", "content": "消費税率を変えたい",
                                          "created_at": "2026-08-28T11:00:00+00:00"},
                                         {"role": "assistant", "answer": V2_LANE_ANSWER, "trace": V2_LANE_TRACE,
                                          "created_at": "2026-08-28T11:00:20+00:00"},
                                     ]})
            if cid_str == "112":
                # SC-6b（調べ方ブロック §4.3・裁定4）: `lens_source=="explicit"` のときだけ調べ方を
                # 復元し、探す対象（layer）は無条件に復元する（会話を開き直した時の右ペイン反映）。
                explicit_answer = {**IMPACT_ANSWER, "lens": "impact",
                                   "scope": {"world": "w1", "scope_paths": [], "source": "all",
                                             "layer": "docs", "layer_applied": False,
                                             "lens_source": "explicit"}}
                return _json(route, {"conversation": {"id": 112, "title": "調べ方を明示した会話", "origin": "own",
                                                       "version": "v1", "read_only": False,
                                                       "contains_personal_workspace": False},
                                     "messages": [
                                         {"role": "user", "content": "消費税率を変えたい",
                                          "created_at": "2026-08-29T10:00:00+00:00"},
                                         {"role": "assistant", "answer": explicit_answer, "trace": None,
                                          "created_at": "2026-08-29T10:00:10+00:00"},
                                     ]})
            if cid_str == "113":
                # 同・調べ方が自動判定（lens_source omitted/"auto"）のときは調べ方を「自動」に戻す
                # （直前ターンの明示扱いにしない）が、探す対象（layer）は復元する。
                auto_answer = {**IMPACT_ANSWER, "lens": "qa",
                              "scope": {"world": "w1", "scope_paths": [], "source": "all",
                                        "layer": "code", "layer_applied": True}}
                return _json(route, {"conversation": {"id": 113, "title": "調べ方は自動の会話", "origin": "own",
                                                       "version": "v1", "read_only": False,
                                                       "contains_personal_workspace": False},
                                     "messages": [
                                         {"role": "user", "content": "消費税の仕様は？",
                                          "created_at": "2026-08-29T10:10:00+00:00"},
                                         {"role": "assistant", "answer": auto_answer, "trace": None,
                                          "created_at": "2026-08-29T10:10:10+00:00"},
                                     ]})
            if cid_str == "114":
                # WEB-1: 直近回答の scope.web_search を復元する（頭脳が Codex＋管理者
                # 許可のときだけ表示される行のため、settings= で eligibility を揃えたテストが使う）。
                web_search_answer = {**IMPACT_ANSWER, "lens": "qa",
                                     "scope": {"world": "w1", "scope_paths": [], "source": "all",
                                               "layer": "both", "web_search": True}}
                return _json(route, {"conversation": {"id": 114, "title": "Web検索ONの会話", "origin": "own",
                                                       "version": "v1", "read_only": False,
                                                       "contains_personal_workspace": False},
                                     "messages": [
                                         {"role": "user", "content": "消費税の最新の解説は？",
                                          "created_at": "2026-08-31T10:00:00+00:00"},
                                         {"role": "assistant", "answer": web_search_answer, "trace": None,
                                          "created_at": "2026-08-31T10:00:10+00:00"},
                                     ]})
            if cid_str == "115":
                # SC-6c（調べる深さ・調べ方ブロック §3.2・§4.3）: 範囲/探す対象と同じく
                # scope.depth_profile を無条件に復元する（明示指定のときだけ復元する lens とは違う）。
                # duration_ms（LOG-1a）と合わせて回答ヘッダの「調べる深さ: 深く・所要 N分N秒」表示も
                # このフィクスチャで固定する（252000ms=4分12秒）。
                depth_answer = {**IMPACT_ANSWER, "lens": "qa", "duration_ms": 252000,
                               "scope": {"world": "w1", "scope_paths": [], "source": "all",
                                         "layer": "both", "depth_profile": "deep"}}
                return _json(route, {"conversation": {"id": 115, "title": "調べる深さを深くした会話",
                                                       "origin": "own", "version": "v1",
                                                       "read_only": False,
                                                       "contains_personal_workspace": False},
                                     "messages": [
                                         {"role": "user", "content": "消費税率を変えたい",
                                          "created_at": "2026-08-31T11:00:00+00:00"},
                                         {"role": "assistant", "answer": depth_answer, "trace": None,
                                          "created_at": "2026-08-31T11:00:10+00:00"},
                                     ]})
            if cid_str == "116":
                # SC-6e（検索経路トグル・調べ方ブロック §3.6）: scope.tools を無条件に復元する
                # （範囲/探す対象/調べる深さと同じ・欠落=全ON）。
                tools_answer = {**IMPACT_ANSWER, "lens": "qa",
                               "scope": {"world": "w1", "scope_paths": [], "source": "all",
                                         "layer": "both",
                                         "tools": {"grep": False, "fulltext": True, "graph": True}}}
                return _json(route, {"conversation": {"id": 116, "title": "検索経路を絞った会話",
                                                       "origin": "own", "version": "v1",
                                                       "read_only": False,
                                                       "contains_personal_workspace": False},
                                     "messages": [
                                         {"role": "user", "content": "消費税率を変えたい",
                                          "created_at": "2026-09-01T11:00:00+00:00"},
                                         {"role": "assistant", "answer": tools_answer, "trace": None,
                                          "created_at": "2026-09-01T11:00:10+00:00"},
                                     ]})
            if cid_str == "117":
                # 範囲パネルの折りたたみツリー化（実環境指摘 2026-09-02）: 深い階層（SCOPES の
                # "4期/03_開発/01_ソース"）を選択済みの会話を開き直しても、祖先が自動展開されて
                # 選択行が見える（データ自体は既存の scope.source=="explicit" 復元と同じ経路）。
                deep_scope_answer = {**IMPACT_ANSWER, "lens": "qa",
                                     "scope": {"world": "w1", "scope_paths": ["4期/03_開発/01_ソース"],
                                               "source": "explicit", "layer": "both"}}
                return _json(route, {"conversation": {"id": 117, "title": "深い範囲を選んだ会話",
                                                       "origin": "own", "version": "v1",
                                                       "read_only": False,
                                                       "contains_personal_workspace": False},
                                     "messages": [
                                         {"role": "user", "content": "TAXCALCの実装を確認したい",
                                          "created_at": "2026-09-02T10:00:00+00:00"},
                                         {"role": "assistant", "answer": deep_scope_answer, "trace": None,
                                          "created_at": "2026-09-02T10:00:10+00:00"},
                                     ]})
            # 既定（101）: UIフィードバック（2026-07-03）積み上げ表示の回帰用に3ターン
            # （記録なし→trace有→trace有＝最新）。
            return _json(route, {"conversation": {"id": 101, "title": "消費税率の相談", "origin": "own",
                                                 "version": "v1", "read_only": False,
                                                 "contains_personal_workspace": False},
                                 "messages": [
                                     {"role": "user", "content": "消費税率を変えたい",
                                      "created_at": "2026-07-01T08:50:00+00:00"},
                                     {"role": "assistant", "answer": IMPACT_ANSWER, "trace": None,
                                      "created_at": "2026-07-01T08:50:30+00:00"},
                                     {"role": "user", "content": "対象範囲はどこまでですか？",
                                      "created_at": "2026-07-01T08:55:00+00:00"},
                                     {"role": "assistant", "answer": IMPACT_ANSWER, "trace": ANSWER_TRACE,
                                      "created_at": "2026-07-01T08:55:20+00:00"},
                                     {"role": "user", "content": "影響はどこまで及びますか？",
                                      "created_at": "2026-07-01T09:00:00+00:00"},
                                     {"role": "assistant", "answer": IMPACT_ANSWER, "trace": ANSWER_TRACE,
                                      "created_at": "2026-07-01T09:00:20+00:00"},
                                 ]})
        if method == "GET" and path == "/users/suggest":
            q = (query.get("q") or [""])[0].strip()
            records["users_suggest"].append(q)
            if not q:
                return _json(route, {"users": []})
            ql = q.lower()
            matched = [u for u in USERS_SUGGEST_POOL if ql in u["uid"].lower() or ql in u["display_name"]]
            return _json(route, {"users": matched})
        if method == "POST" and path.endswith("/shares") and path.startswith("/conversations/"):
            body = _post_json(request)
            records["share_create"].append(body)
            return _json(route, {"ok": True, "share_id": 77,
                                 "url": "/share/conversations/share-token-101",
                                 "expires_at": body.get("expires_at")})
        if method == "GET" and path == "/scopes":
            return _json(route, SCOPES)
        if method == "GET" and path == "/graph":
            # ②graph 軽量化: ETag（未変更なら 304）を付ける（実 API と同じ再検証経路）。デモ4ノードは
            # 既定 limit に収まるため常に非truncated＝全件（truncated 表示の検証は個別テストが上書き）。
            # ETag は GRAPH 本体の内容ハッシュ＋表示範囲トークンで合成（内容連動・RV是正2026-07-08 Low#3）。
            token = "all" if (query.get("limit") or [""])[0] == "0" else "default"
            etag = f'"g.{_GRAPH_CONTENT_HASH}.{token}"'
            if (request.headers or {}).get("if-none-match") == etag:
                route.fulfill(status=304, headers={"ETag": etag})   # 304 は body なし（実 API と同じ再検証経路）
                return
            return _json(route, GRAPH, headers={"ETag": etag})
        if method == "GET" and path == "/graph/facets":
            return _json(route, GRAPH_FACETS_RESP)
        if method == "GET" and path == "/graph/search":
            # GET /graph/search の nodes[] は GET /graph（graph_view）と別形＝ phase/category に加え
            # em も持つ（graph_admin.py::_node は em を返し続ける・GraphNode と違い GraphSearchNode は
            # S3 でも em を撤去していない＝graph_admin の管理検索専用ビューは対象外）。
            # GRAPH["nodes"] を直接使い回さず個別に足してから graph_search_response へ渡す。
            rel = (query.get("relationship") or [""])[0]
            search_nodes = [{**n, "em": "static", "phase": None, "category": None} for n in GRAPH["nodes"]]
            if rel:
                return _json(route, graph_search_response(
                    search_nodes[:2], GRAPH["edges"][:1], {"nodes": 2, "edges": 1}))
            return _json(route, graph_search_response(
                search_nodes[:1], [], {"nodes": 1, "edges": 0}))
        if method == "POST" and path == "/graph/ask":
            body = _post_json(request)
            records["graph_ask"].append(body)
            return _json(route, graph_ask_response(body.get("question")))
        if method == "GET" and path == "/ingest/preview":
            return _json(route, PREVIEW)
        if method == "GET" and path == "/admin/es/search":
            # S2: ヒットに由来（extraction_method）と照合差分（has_conflicts）を通す。
            q = (query.get("query") or [""])[0]
            records["es_search"].append(query)
            return _json(route, {"world": "w1", "query": q, "scope_paths": [], "hits": [
                {"doc_id": "4期/02_設計/01_基本設計/スキャン図面.pdf", "line": 3,
                 "snippet": f"{q}: 画像から読み取った本文（精度は低め）。", "score": 2.1, "ext": ".pdf",
                 "extraction_method": "markitdown_ocr", "confidence": 0.4},
                {"doc_id": "4期/02_設計/01_基本設計/旧料金表.xls", "line": 8,
                 "snippet": f"{q}: 旧形式を変換して読み取った本文。", "score": 1.5, "ext": ".xls",
                 "extraction_method": "ooxml", "confidence": 1.0, "has_conflicts": True},
            ] if q else []})
        if method == "GET" and path == "/fs/list":
            current = (query.get("path") or [""])[0]
            if current == "":
                return _json(route, {"path": "", "parent": None, "entries": [{"name": "c", "path": "/mnt/c"}]})
            if current == "/mnt/c":
                return _json(route, {"path": "/mnt/c", "parent": None,
                                     "entries": [{"name": "ProjectA", "path": "/mnt/c/ProjectA"}]})
            return _json(route, {"path": "/mnt/c/ProjectA", "parent": "/mnt/c", "entries": []})
        if method == "GET" and path == "/worlds/w1/status":
            return _json(route, WORLD_STATUS_RESP)
        if method == "POST" and path == "/worlds/diff":
            body = _post_json(request)
            records["world_diff"].append(body)
            return _json(route, world_diff_response(body.get("path")))
        if method == "POST" and path == "/worlds":
            body = _post_json(request)
            records["world_register"].append(body)
            return _json(route, world_ingest_accepted_response(WORLD["world_id"]))
        if method == "GET" and path == "/worlds/w1/diff":
            return _json(route, world_diff_response(
                "/mnt/c/ProjectA", world_id="w1", label="4期更改", registered=True,
                added=[], total=3, indexed=3))
        if method == "POST" and path == "/worlds/w1/refresh":
            records["world_refresh"].append(True)
            return _json(route, world_ingest_accepted_response("w1"))
        if method == "GET" and path == "/chat/stream":
            # 旧エンドポイント（後方互換のため実サーバ側は残置・chat.js はもう呼ばない）。
            # デフォルトモックとしては維持するが、既定の送信フローは /chat/turns 系を使う。
            records["stream_urls"].append(request.url)
            return _sse(route, stream_events if stream_events is not None else _DEFAULT_STREAM_EVENTS)
        if method == "POST" and path == "/chat/turns":
            # 背景実行（覗き窓方式・docs/proposals/2026-07-03-チャット背景実行.md）: 送信の既定フロー。
            # turn_id は固定値（"turn-101"）＝個別テストが `**/chat/turns/*/stream**` のような
            # ワイルドカードで上書きする際も値を知る必要がない。
            body = _post_json(request)
            records["turn_starts"].append(body)
            # SC-6e: 実サーバ（sherpa/routers/chat.py::_validate_tools_availability・
            # chat_turns_start）と同じ「明示 ON かつ不達」の 422 をここでも再現する。これが無いと
            # クライアント側の「送信直前に不達かつONのキーを省略する」修正
            # （web/chat/inquiry.js::toolsForSend）が実際に 422 を防いでいることを e2e で検証できない。
            _tools_body = body.get("tools") or {}
            _unavail_explicit = [k for k in ("grep", "fulltext", "graph")
                                 if _tools_body.get(k) is True and not tools_availability_resp.get(k, True)]
            if _unavail_explicit:
                return _json(route, {"detail": f"検索経路 {', '.join(_unavail_explicit)} は現在利用できません"
                                              "（接続を確認してください）"}, status=422)
            return _json(route, {"turn_id": "turn-101", "conversation_id": body.get("conversation_id") or 101})
        if method == "GET" and re.match(r"^/chat/turns/[^/]+/stream$", path):
            records["turn_stream_urls"].append(request.url)
            # 呼び出し元が `stream_events=` で丸ごと上書き可（UIフィードバック・2026-07-03・AI回答のMarkdown表示
            # のテスト等で使う従来どおりの仕組み）。
            return _sse(route, stream_events if stream_events is not None else _DEFAULT_STREAM_EVENTS)
        if method == "POST" and re.match(r"^/chat/turns/[^/]+/stop$", path):
            records["turn_stops"].append(path)
            return _json(route, {"ok": True})
        if method == "GET" and path == "/chat/turns/running":
            # 既定は「実行中ターンなし」（トップバーのバッジは隠れたまま）。個別テストは
            # `page.route("**/chat/turns/running", ...)` で上書きする。
            return _json(route, CHAT_TURNS_RUNNING_EMPTY)

        # 外部連携 API キー。管理者発行（owner_uid=None）と利用者自己発行（owner_uid=uid）を
        # 同じ台帳（ext_keys_store）に積む簡易モック。実サーバと同じくプレーンキーは台帳の行
        # には持たせず、発行直後の応答（`_ext_key_created_resp`）にだけ一時的に載せる。
        def _ext_key_created_resp(row, plain):
            return {"ok": True, "id": row["id"], "key": plain, "key_prefix": row["key_prefix"],
                    "label": row["label"], "created_at": row["created_at"],
                    "allowed_worlds": row["allowed_worlds"], "expires_at": row["expires_at"],
                    "daily_quota": row["daily_quota"], "client_op_id": row.get("client_op_id"),
                    # PART-6: webhook_url 未指定なら secret も null（実サーバと同型）。
                    "webhook_url": row.get("webhook_url"),
                    "webhook_secret": (f"whsec-mock{row['id']:04d}" if row.get("webhook_url") else None)}

        def _ext_key_list_item(row):
            return {"id": row["id"], "key_prefix": row["key_prefix"], "label": row["label"],
                    "created_by": row["created_by"], "revoked_by": row["revoked_by"],
                    "allowed_worlds": row["allowed_worlds"], "daily_quota": row["daily_quota"],
                    "owner_uid": row["owner_uid"], "client_op_id": row.get("client_op_id"),
                    "created_at": row["created_at"],
                    "revoked_at": row["revoked_at"], "last_used_at": row["last_used_at"],
                    "expires_at": row["expires_at"], "call_count": row["call_count"],
                    # PART-6: 一覧には有無と host:port のみ（secret は絶対に含めない）。
                    "webhook": bool(row.get("webhook_url")),
                    "webhook_host": (row["webhook_url"].split("://", 1)[-1].split("/", 1)[0]
                                     if row.get("webhook_url") else None)}

        def _normalize_client_op_id(v):
            # 実サーバは client_op_id を保存前に標準小文字正準形へ正規化する（大小文字表記の
            # 違いを同じ UUID として扱う）。モックも同じ規則で照合・保存する。
            return v.lower() if v else v

        def _ext_key_new(body, *, created_by, owner_uid, daily_quota=None):
            """台帳の行（プレーンキーを含まない）とプレーンキーを別々に返す。`daily_quota` は
            呼び出し側が確定した値を渡す（自己発行は既定/上限の解決を呼び出し側で行う）。"""
            next_id = max([r["id"] for r in ext_keys_store] + [0]) + 1
            plain = f"sk-ext-mock{next_id:04d}"
            row = {"id": next_id, "key_prefix": plain[:12], "label": body.get("label") or "",
                   "created_by": created_by, "owner_uid": owner_uid,
                   "allowed_worlds": body.get("allowed_worlds"), "expires_at": body.get("expires_at"),
                   "daily_quota": daily_quota,
                   "client_op_id": _normalize_client_op_id(body.get("client_op_id")),
                   "webhook_url": body.get("webhook_url"),   # PART-6
                   "created_at": "2026-08-25T00:00:00+00:00",
                   "revoked_at": None, "revoked_by": None, "last_used_at": None, "call_count": 0}
            return row, plain

        def _ext_keys_user_allowed() -> bool:
            # 実サーバは system_settings.user_api_keys_allowed という単一の真実源を見るが、この
            # モックは管理画面用（system_settings_resp.ext_keys）と個人設定用（settings_resp）の
            # 2つに分かれている（各 e2e が普段どちらか一方だけを `system_settings=`/`settings=` で
            # 上書きするため）。どちらか一方で許可されていれば許可扱いにする。
            return bool((system_settings_resp.get("ext_keys") or {}).get("user_api_keys_allowed")) \
                or bool(settings_resp.get("user_api_keys_allowed"))

        def _ext_keys_quota_cap() -> int:
            # 実サーバの `store.resolve_self_issued_daily_quota_cap` に相当（既定/上限・
            # 管理者未設定時のフォールバックは実サーバと同じ100）。
            configured = ((system_settings_resp.get("ext_keys") or {})
                         .get("daily_quota_default") or {}).get("configured")
            if configured is None:
                configured = settings_resp.get("user_api_keys_daily_quota_default")
            return int(configured) if configured else 100

        def _ext_key_client_op_id_conflict(cid) -> bool:
            # 実サーバの非NULL部分一意制約（lower() の関数インデックス）に相当（衝突は409・
            # 大小文字表記の違いを区別しない）。
            cid_norm = _normalize_client_op_id(cid)
            return bool(cid_norm) and any(r.get("client_op_id") == cid_norm for r in ext_keys_store)

        if method == "POST" and path == "/ext/v1/admin/keys":
            body = _post_json(request)
            records["ext_key_admin_create"].append(body)
            if current_user.get("role") != "admin":
                return _json(route, {"detail": "管理者権限が必要です"}, status=403)
            if _ext_key_client_op_id_conflict(body.get("client_op_id")):
                return _json(route, {"detail": "この操作は既に処理されています"}, status=409)
            row, plain = _ext_key_new(body, created_by=current_user["uid"], owner_uid=None,
                                      daily_quota=body.get("daily_quota"))
            ext_keys_store.append(row)
            return _json(route, _ext_key_created_resp(row, plain))
        if method == "GET" and path == "/ext/v1/admin/keys":
            if current_user.get("role") != "admin":
                return _json(route, {"detail": "管理者権限が必要です"}, status=403)
            return _json(route, {"keys": [_ext_key_list_item(r) for r in ext_keys_store]})
        if method == "DELETE" and path.startswith("/ext/v1/admin/keys/"):
            key_id = int(path.rsplit("/", 1)[-1])
            records["ext_key_admin_revoke"].append(key_id)
            if current_user.get("role") != "admin":
                return _json(route, {"detail": "管理者権限が必要です"}, status=403)
            row = next((r for r in ext_keys_store if r["id"] == key_id), None)
            if row is None:
                return _json(route, {"detail": "キーが見つかりません"}, status=404)
            row["revoked_at"] = row["revoked_at"] or "2026-08-25T00:00:00+00:00"
            return _json(route, {"ok": True, "id": key_id, "revoked_at": row["revoked_at"]})
        if method == "POST" and path == "/ext/v1/admin/keys/recover":
            # 曖昧な発行結果の回復専用（実サーバ: 認証主体・owner_uid・client_op_id を同一条件で
            # 照合する単一の原子的操作・一覧+DELETE の2段構成ではない）。
            body = _post_json(request)
            if current_user.get("role") != "admin":
                return _json(route, {"detail": "管理者権限が必要です"}, status=403)
            cid = _normalize_client_op_id(body.get("client_op_id"))
            row = next((r for r in ext_keys_store
                       if r.get("client_op_id") == cid and r.get("owner_uid") is None
                       and r["created_by"] == current_user["uid"] and not r["revoked_at"]), None)
            if row is None:
                return _json(route, {"found": False, "id": None, "revoked_at": None})
            row["revoked_at"] = row["revoked_at"] or "2026-08-25T00:00:00+00:00"
            return _json(route, {"found": True, "id": row["id"], "revoked_at": row["revoked_at"]})
        if method == "POST" and path == "/ext/v1/keys":
            body = _post_json(request)
            records["ext_key_self_create"].append(body)
            if not _ext_keys_user_allowed():
                return _json(route, {"detail": "利用者による API キー発行は許可されていません"
                                               "（管理者に確認してください）"}, status=403)
            if _ext_key_client_op_id_conflict(body.get("client_op_id")):
                return _json(route, {"detail": "この操作は既に処理されています"}, status=409)
            # 自己発行は管理者統制の既定/上限を適用する（未指定=既定・超過=422・実サーバと同型）。
            cap = _ext_keys_quota_cap()
            requested = body.get("daily_quota")
            if requested is not None and requested > cap:
                return _json(route, {"detail": f"1日あたりの呼び出し上限は{cap}件以下で"
                                               "指定してください（管理者の上限）"}, status=422)
            daily_quota = requested if requested is not None else cap
            row, plain = _ext_key_new(body, created_by=current_user["uid"],
                                      owner_uid=current_user["uid"], daily_quota=daily_quota)
            ext_keys_store.append(row)
            return _json(route, _ext_key_created_resp(row, plain))
        if method == "GET" and path == "/ext/v1/keys":
            # OFF のときは一覧そのものを見せない（実サーバの4ルート共通ゲートと同じ）。
            if not _ext_keys_user_allowed():
                return _json(route, {"detail": "利用者による API キー発行は許可されていません"}, status=403)
            mine = [r for r in ext_keys_store if r.get("owner_uid") == current_user["uid"]]
            return _json(route, {"keys": [_ext_key_list_item(r) for r in mine]})
        if method == "DELETE" and path.startswith("/ext/v1/keys/"):
            key_id = int(path.rsplit("/", 1)[-1])
            records["ext_key_self_revoke"].append(key_id)
            if not _ext_keys_user_allowed():
                return _json(route, {"detail": "利用者による API キー発行は許可されていません"}, status=403)
            row = next((r for r in ext_keys_store
                       if r["id"] == key_id and r.get("owner_uid") == current_user["uid"]), None)
            if row is None:
                return _json(route, {"detail": "キーが見つかりません"}, status=404)
            row["revoked_at"] = row["revoked_at"] or "2026-08-25T00:00:00+00:00"
            return _json(route, {"ok": True, "id": key_id, "revoked_at": row["revoked_at"]})
        if method == "POST" and path == "/ext/v1/keys/recover":
            body = _post_json(request)
            if not _ext_keys_user_allowed():
                return _json(route, {"detail": "利用者による API キー発行は許可されていません"}, status=403)
            cid = _normalize_client_op_id(body.get("client_op_id"))
            row = next((r for r in ext_keys_store
                       if r.get("client_op_id") == cid and r.get("owner_uid") == current_user["uid"]
                       and not r["revoked_at"]), None)
            if row is None:
                return _json(route, {"found": False, "id": None, "revoked_at": None})
            row["revoked_at"] = row["revoked_at"] or "2026-08-25T00:00:00+00:00"
            return _json(route, {"found": True, "id": row["id"], "revoked_at": row["revoked_at"]})

        route.continue_()

    page.route("**/*", handler)
    return records


# S3（mock 契約ドリフト対策）: 機械可読レジストリ。`handler()` が明示的に応答する (method, path) の
# 一覧（path は FastAPI ルート表記＝`tests/api/goldens/routes.txt` と同じ `{param}` テンプレート）。
# `tests/api/test_mock_api_contract.py` がこれを golden ルート表と突合し、実 API に無いルートを
# 偽装したままにしていないかを検査する（旧ルート撤去・改名の検知）。
# `GET /chat/stream` は chat.js からはもう呼ばれないが、実サーバ側は後方互換のため意図的に残置
# （sherpa/routers/chat.py 参照）＝routes.txt にも存在する現役ルートであり除外不要。
MOCKED: list[tuple[str, str]] = [
    ("GET", "/auth/me"),
    ("GET", "/health/summary"),
    ("GET", "/admin/health"),
    ("GET", "/admin/usage/stats"),
    ("GET", "/admin/settings"),
    ("PUT", "/admin/settings"),
    ("POST", "/admin/settings/openai-endpoint-test"),
    ("POST", "/auth/login"),
    ("POST", "/auth/logout"),
    ("GET", "/admin/users"),
    ("POST", "/admin/users"),
    ("PATCH", "/admin/users/{uid}"),
    ("GET", "/notifications"),
    ("GET", "/announcements"),
    ("POST", "/admin/announcements"),
    ("PATCH", "/admin/announcements/{id}"),
    ("GET", "/admin/audit"),
    ("GET", "/admin/audit/export"),
    ("GET", "/workspace/files"),
    ("POST", "/workspace/files"),
    ("DELETE", "/workspace/files/{file_id}"),
    ("GET", "/workspace/search"),
    ("GET", "/documents/download"),
    ("GET", "/workspace/files/{file_id}/download"),
    ("GET", "/worlds"),
    ("GET", "/world-options"),
    ("GET", "/config"),
    ("GET", "/settings"),
    ("PUT", "/settings"),
    ("POST", "/settings/test"),
    ("GET", "/settings/bedrock-models"),
    ("POST", "/settings/bedrock-models/verify"),
    ("GET", "/conversations"),
    ("GET", "/conversations/{cid}"),
    ("GET", "/users/suggest"),
    ("POST", "/conversations/{cid}/shares"),
    ("GET", "/scopes"),
    ("GET", "/graph"),
    ("GET", "/graph/facets"),
    ("GET", "/graph/search"),
    ("POST", "/graph/ask"),
    ("GET", "/ingest/preview"),
    ("GET", "/admin/es/search"),
    ("GET", "/fs/list"),
    ("GET", "/worlds/{wid}/status"),
    ("POST", "/worlds/diff"),
    ("POST", "/worlds"),
    ("GET", "/worlds/{wid}/diff"),
    ("POST", "/worlds/{wid}/refresh"),
    ("GET", "/chat/stream"),                   # 後方互換の意図的残置（上のコメント参照）
    ("POST", "/chat/turns"),
    ("GET", "/chat/turns/{turn_id}/stream"),
    ("POST", "/chat/turns/{turn_id}/stop"),
    ("GET", "/chat/turns/running"),
    # 外部連携 API キー（管理者/利用者）。
    ("POST", "/ext/v1/admin/keys"),
    ("GET", "/ext/v1/admin/keys"),
    ("DELETE", "/ext/v1/admin/keys/{key_id}"),
    ("POST", "/ext/v1/admin/keys/recover"),
    ("POST", "/ext/v1/keys"),
    ("GET", "/ext/v1/keys"),
    ("DELETE", "/ext/v1/keys/{key_id}"),
    ("POST", "/ext/v1/keys/recover"),
]
