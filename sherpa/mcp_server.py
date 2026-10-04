"""Sherpa MCP サーバ（stdio・自前実装・SDK 非依存）。Codex に Sherpa の read-only ツール（grep / 精読 / グラフ近傍 / ES）を MCP で渡す。
設計: docs/design/codex.md「MCP の道具」／docs/design/interfaces.md「MCP の道具」
ツールの実装は `tool_dispatch.run_tool`（読み取り部品 `parts/read/tools.py` への振り分け）を API 経路と共用する。

- world / scope / layer は起動時の環境変数（`SHERPA_MCP_WORLD` / `SHERPA_MCP_SCOPE` / `SHERPA_MCP_LAYER`）で固定する（Codex には選ばせない）。
- トランスポートは改行区切り JSON-RPC 2.0（MCP stdio）。stdin から 1 行 1 メッセージ、stdout に応答、ログは stderr。
- `ask_user` は Codex にも公開する。質問カードはラッパー（`agents._run_authoring`）が届け、ここは「届いた・調査をやめて要約せよ」のツール結果を返すだけ。
  1 実行 1 回の上限は本サーバのプロセス寿命で数える（エージェント＝MCP 接続ごとに別プロセスなので、multi_agent の子は親と別カウント）。
- `SHERPA_MCP_ASK_DISABLED=1`（確認ID 付き再送）では `ask_user` を tools/list から外す。それでも呼ばれたら初回でも `_ASK_RESULT_AGAIN` を返す（質問カードを出さないまま調査を打ち切らせない）。
- `SHERPA_MCP_SIDECAR`（JSONL パス・codex_home 配下など model-shell の書込許可領域の外）が設定されていれば、読取系ツールの doc_id と ask_user の質問だけを本文なしで追記する。
  multi_agent の子の MCP 呼出は親の `--json` に現れないため、`providers/codex/provider.py` が codex exec 終了後にこのファイルを読み、子が読んだ資料を出典へ・子の ask_user を確認カードへ合流させる。未設定なら何もしない。
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from . import agentic_search, es_index, investigation_ledger, tool_dispatch
from .ingest.world_neo4j import GraphSchemaEraError

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "sherpa", "version": "0.1.0"}

# 読取系ツールの分類。`providers/codex/provider.py` の出典収集がこのタプルを import して使う。`LISTED_DOC_TOOLS`（シート一覧のみ）は sources_verified に数えない。
READ_DOC_TOOLS = ("read_doc", "read_around", "doc_outline",
                  "xlsx_range", "docx_paragraphs", "pptx_slides", "pdf_pages", "file_head")
LISTED_DOC_TOOLS = ("xlsx_sheets",)
COMPARE_DOC_ID_ARGS = ("left_doc_id", "right_doc_id", "source_doc_id")

# ask_user は検索ツールではなく、ここでは Codex に返すツール結果だけを持つ。1 実行 1 回まで（プロセス寿命＝エージェント 1 体ぶんをモジュール変数で数える）。
_ASK_STATE = {"count": 0}
_ASK_RESULT_FIRST = "質問はユーザーに届きました。追加調査はせず、ここまでに確認できたことを省略せずまとめて終了してください。"
_ASK_RESULT_AGAIN = "既に質問済みです。調査を続けて回答をまとめてください。"

_LEDGER_TOOLS = frozenset({"ledger_manifest_set", "ledger_item_put", "ledger_status", "ledger_review_put"})

# 素の Codex モード（`plain`）で公開するツールの全体。`_tool_defs()`・`handle()` のどちらもこの集合だけを見る。
_PLAIN_TOOLSET = frozenset({"graph_neighbors", "ask_user"})


def _ledger_tool_defs() -> list:
    return [
        {"name": "ledger_manifest_set",
         "description": "親が調査対象の目録を登録・更新する。ファイルを直接書かずこのツールを使う。",
         "inputSchema": {
             "type": "object", "additionalProperties": False,
             "required": ["question_kind", "items"],
             "properties": {
                 "question_kind": {"type": "string", "enum": ["list", "compare", "impact", "troubleshoot", "other"]},
                 "items": {"type": "array", "items": {"type": "string"}},
             }}},
        {"name": "ledger_item_put",
         "description": "担当する調査項目を検査して保存する。ファイルを直接書かずこのツールを使う。",
         "inputSchema": {
             "type": "object", "additionalProperties": False,
             "required": ["id", "kind", "subject", "required_checks", "evidence", "status", "reason", "owner"],
             "properties": {
                 "id": {"type": "string"}, "kind": {"type": "string"},
                 "subject": {"type": "string", "maxLength": investigation_ledger.SUBJECT_MAX_LEN},
                 "required_checks": {"type": "array", "minItems": 1, "uniqueItems": True,
                                     "items": {"type": "string", "enum": list(investigation_ledger.EVIDENCE_KINDS)}},
                 "evidence": {"type": "array", "items": {
                     "type": "object", "additionalProperties": False,
                     "required": ["kind", "path", "line"],
                     "properties": {
                         "kind": {"type": "string", "enum": list(investigation_ledger.EVIDENCE_KINDS)},
                         "path": {"type": "string"}, "line": {"type": "integer"},
                     }}},
                 "status": {"type": "string", "enum": sorted(investigation_ledger.NON_TERMINAL_STATUSES
                                                              | investigation_ledger.TERMINAL_STATUSES)},
                 "reason": {"type": "string", "maxLength": investigation_ledger.REASON_MAX_LEN},
                 "owner": {"type": "string"},
             }}},
        {"name": "ledger_status",
         "description": "調査台帳の完了・未充足・無効・欠落を確認する。ファイルを直接書かず台帳ツールを使う。",
         "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
        {"name": "ledger_review_put",
         "description": "回答を確定する前の中間の見直しを1件記録する（本体だけが呼ぶ・worker は呼ばない）。"
                        "先に item を1件以上終端にしてから呼ぶこと（終端が0件なら拒否される）。"
                        "ファイルを直接書かずこのツールを使う。",
         "inputSchema": {
             "type": "object", "additionalProperties": False,
             "required": ["purpose", "perspectives", "summary", "added_items", "removed_items",
                         "verdict", "extra_perspectives"],
             "properties": {
                 "purpose": {"type": "string", "maxLength": investigation_ledger.SUBJECT_MAX_LEN},
                 "perspectives": {"type": "array",
                                  "items": {"type": "string", "maxLength": investigation_ledger.SUBJECT_MAX_LEN}},
                 "summary": {"type": "string", "maxLength": investigation_ledger.REVIEW_SUMMARY_MAX_LEN},
                 "added_items": {"type": "array", "items": {
                     "type": "object", "additionalProperties": False, "required": ["id", "reason"],
                     "properties": {
                         "id": {"type": "string"},
                         "reason": {"type": "string", "maxLength": investigation_ledger.REASON_MAX_LEN},
                     }}},
                 "removed_items": {"type": "array", "items": {
                     "type": "object", "additionalProperties": False, "required": ["id", "reason"],
                     "properties": {
                         "id": {"type": "string"},
                         "reason": {"type": "string", "maxLength": investigation_ledger.REASON_MAX_LEN},
                     }}},
                 "verdict": {"type": "string", "enum": sorted(investigation_ledger.REVIEW_VERDICTS)},
                 "extra_perspectives": {"type": "array",
                                        "items": {"type": "string",
                                                  "maxLength": investigation_ledger.SUBJECT_MAX_LEN}},
             }}},
    ]


def _run_ledger_tool(name: str, args: dict) -> dict:
    ledger_dir = os.environ.get("SHERPA_MCP_LEDGER_DIR")
    if not ledger_dir:
        # 書込先が未設定なら fail-closed（別の場所へ台帳を作らない）。
        return {"error": "ledger_unavailable"}
    directory = Path(ledger_dir)
    try:
        if name == "ledger_status":
            snapshot = investigation_ledger.load_ledger(directory)
            _require_review = _ledger_require_review()
            _reviews = investigation_ledger.load_reviews(directory) if _require_review else ()
            verdict = investigation_ledger.ledger_complete(
                snapshot, required_extra=_ledger_required_extra(),
                reviews=_reviews, require_review=_require_review,
                require_continuation_resolved=_ledger_require_continuation_resolved())
            return {
                "complete": verdict.complete, "manifest_invalid": verdict.manifest_invalid,
                "items": len(snapshot.items) + len(snapshot.invalid_ids),
                "non_terminal": verdict.non_terminal_ids, "unsatisfied": verdict.unsatisfied,
                "invalid": verdict.invalid_ids, "missing": verdict.missing_ids,
                "unregistered": verdict.unregistered_ids, "counts": verdict.terminal_counts,
                "review_missing": verdict.review_missing, "review_pending": verdict.review_pending_ids,
            }
        if name == "ledger_item_put":
            problems = investigation_ledger.validate_item(args)
            if problems:
                return {"error": "ledger_item_invalid", "problems": problems}
            try:
                investigation_ledger.write_item_atomic(directory, args)
            except ValueError as exc:
                return {"error": "ledger_item_invalid", "problems": [str(exc)]}
            return {"ok": True, "id": args["id"]}
        if name == "ledger_review_put":
            # 見直しは終端の item が 1 件以上できてから書く。受付時に現在の台帳の終端件数を数え、0 件なら拒否する。
            # `ts`/`terminal_count` はサーバが付ける（`terminal_count` は書いた時点の件数としてそのまま記録する）。
            # 数えるのは manifest に登録済みの id だけ（未登録の item を終端にしても前提にならない・`ledger_complete()` の `unregistered_ids` と同じ）。
            snapshot = investigation_ledger.load_ledger(directory)
            manifest_ids = (set(snapshot.manifest["items"])
                            if snapshot.manifest is not None else set())
            terminal_count = sum(
                1 for item_id, item in snapshot.items.items()
                if item_id in manifest_ids
                and item.get("status") in investigation_ledger.TERMINAL_STATUSES)
            if terminal_count < 1:
                return {"error": "ledger_review_rejected",
                       "problems": ["終端の項目がまだ1件もありません。先に item を1件以上"
                                   "終端にしてから見直しを書いてください。"]}
            stored = {**args, "ts": time.time(), "terminal_count": terminal_count}
            problems = investigation_ledger.validate_review_entry(stored)
            if problems:
                return {"error": "ledger_review_invalid", "problems": problems}
            try:
                investigation_ledger.append_review_atomic(directory, stored)
            except (OSError, ValueError) as exc:
                return {"error": "ledger_review_rejected", "problems": [str(exc)]}
            return {"ok": True}

        manifest_path = directory / "manifest.json"
        if manifest_path.is_symlink():
            return {"error": "ledger_manifest_invalid", "problems": ["既存の manifest が symlink です"]}
        created_at = None
        if manifest_path.exists():
            # 内容が規約に合わない通常ファイルは、検証済みの入力で修復できる（拒否すると修復手段が無くなるため）。`created_at` は既存が文字列なら保持し、無ければ作り直す。読取不能（OSError）は外側で拒否する。
            try:
                existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            except ValueError:
                existing = None
            if isinstance(existing, dict) and isinstance(existing.get("created_at"), str) and existing["created_at"]:
                created_at = existing["created_at"]
        if created_at is None:
            created_at = datetime.now(timezone.utc).isoformat()
        manifest = {**args, "created_at": created_at}
        problems = investigation_ledger.validate_manifest(manifest)
        if problems:
            return {"error": "ledger_manifest_invalid", "problems": problems}
        investigation_ledger.write_manifest_atomic(directory, manifest)
        return {"ok": True}
    except OSError as exc:
        # ファイル操作の失敗は fail-closed。MCP のエラーとして返し、自動再試行しない。
        print(f"[sherpa-mcp] {name} failed: {type(exc).__name__} errno={exc.errno}", file=sys.stderr)
        return {"error": "ledger_write_failed", "problems": [f"{type(exc).__name__}: errno={exc.errno}"]}


def _toolset() -> str:
    """MCP が公開するツールの絞り込み（`SHERPA_MCP_TOOLSET`）。`"plain"` だけを特別扱いし、それ以外（未設定・`"full"`・想定外の値）はフル公開に倒す。"""
    return "plain" if os.environ.get("SHERPA_MCP_TOOLSET", "").strip().lower() == "plain" else "full"


# plain で公開するグラフの説明。standard 用の説明は plain で公開しないツールの使用を指示するため使わない。es_search は plain では出さない。
_DESC_GRAPH_PLAIN = (
    "関係グラフから、ある名前（プログラム/コピーブック/ジョブ/データ項目/テーブルなど）の関連部品"
    "（コピー・呼び出し・参照・関連文書（言及）の近傍）を、辺ごとの種類と向き（from→to）付きの経路で返す。"
    "COPIES／INVOKES／ACCESSES／CONTAINS だけの経路は構造的な依存＝根拠にしてよい（影響は矢印をさかのぼる: "
    "A →COPIES→ B は B を変えると A が影響を受ける）。DOCUMENTS（言及）・CORRESPONDS_TO（同名の対応）や "
    "`unverified` の辺を含む経路は候補＝原本で確認する。名前は完全一致で引く（シェルで正確な名前を見つけてから"
    "渡す）。近傍が上限で切られたときは truncated:true と count（総数）が付く＝その範囲は未確認として扱う。"
)
# plain は台帳を持たないため `item` を除いたコピーを使う（`name` の schema/説明は共有する）。
_PARAMS_GRAPH_PLAIN = {**agentic_search._PARAMS_GRAPH,
                      "properties": {k: v for k, v in agentic_search._PARAMS_GRAPH["properties"].items()
                                     if k != "item"}}


def _tool_defs() -> list:
    """公開ツール定義（schema は agentic_search と共通）。ES はインデックスがある時だけ公開し、`read_doc`/`doc_outline`/`glob_search` は常に公開する。
    並びは「構造を掴む→通読→精読」（ripgrep_search の直後に glob_search、read_around の直前に doc_outline・read_doc）。
    `SHERPA_MCP_ASK_DISABLED=1` の実行では ask_user を外す。探す対象（層）が限定されている間は `graph_neighbors` を外す（`run_tool` の拒否と多層防御）。
    `_toolset()` が `"plain"` のときは `_PLAIN_TOOLSET`（graph_neighbors・ask_user）だけを返す（出す条件は同じ）。`handle()` 側も同じ集合だけを許可する。
    """
    if _toolset() == "plain":
        defs = []
        if _layer() in (None, "both"):
            defs.append({"name": "graph_neighbors", "description": _DESC_GRAPH_PLAIN,
                        "inputSchema": _PARAMS_GRAPH_PLAIN})
        if not _ask_disabled():
            defs.append({"name": "ask_user", "description": agentic_search._DESC_ASK,
                        "inputSchema": agentic_search._PARAMS_ASK})
        return defs
    defs = [
        {"name": "list_docs", "description": agentic_search._DESC_LIST_DOCS,
         "inputSchema": agentic_search._PARAMS_LIST_DOCS},
        # 台帳ベースの土台系ツール（ES/graph の可用性に依存しない）。常に公開する。
        {"name": "folder_tree", "description": agentic_search._DESC_FOLDER_TREE,
         "inputSchema": agentic_search._PARAMS_FOLDER_TREE},
        {"name": "ripgrep_search", "description": agentic_search._DESC_SEARCH,
         "inputSchema": agentic_search._PARAMS_SEARCH},
        {"name": "glob_search", "description": agentic_search._DESC_GLOB,
         "inputSchema": agentic_search._PARAMS_GLOB},
        {"name": "doc_outline", "description": agentic_search._DESC_OUTLINE,
         "inputSchema": agentic_search._PARAMS_OUTLINE},
        {"name": "read_doc", "description": agentic_search._DESC_READ_DOC,
         "inputSchema": agentic_search._PARAMS_READ_DOC},
        {"name": "read_around", "description": agentic_search._DESC_READ,
         "inputSchema": agentic_search._PARAMS_READ},
        # ES/graph の可用性に依存しない土台系ツール。常に公開する。
        {"name": "compare_documents", "description": agentic_search._DESC_COMPARE,
         "inputSchema": agentic_search._PARAMS_COMPARE},
        # `file_head`（テキスト・コード）は層に関係なく常に公開する。Office/PDF の 5 本（下）は探す対象がソースに限定されている間は外す（`run_tool` の拒否と多層防御）。
        {"name": "file_head", "description": agentic_search._DESC_FILE_HEAD,
         "inputSchema": agentic_search._PARAMS_FILE_HEAD},
    ]
    if _layer() != "code":
        defs.append({"name": "xlsx_sheets", "description": agentic_search._DESC_XLSX_SHEETS,
                     "inputSchema": agentic_search._PARAMS_XLSX_SHEETS})
        defs.append({"name": "xlsx_range", "description": agentic_search._DESC_XLSX_RANGE,
                     "inputSchema": agentic_search._PARAMS_XLSX_RANGE})
        defs.append({"name": "docx_paragraphs", "description": agentic_search._DESC_DOCX_PARAGRAPHS,
                     "inputSchema": agentic_search._PARAMS_DOCX_PARAGRAPHS})
        defs.append({"name": "pptx_slides", "description": agentic_search._DESC_PPTX_SLIDES,
                     "inputSchema": agentic_search._PARAMS_PPTX_SLIDES})
        defs.append({"name": "pdf_pages", "description": agentic_search._DESC_PDF_PAGES,
                     "inputSchema": agentic_search._PARAMS_PDF_PAGES})
    if _layer() in (None, "both"):
        defs.append({"name": "graph_neighbors", "description": agentic_search._DESC_GRAPH,
                     "inputSchema": agentic_search._PARAMS_GRAPH})
    if not _ask_disabled():
        # ask_user を Codex にも公開する（description・schema は agentic と共通）。
        defs.append({"name": "ask_user", "description": agentic_search._DESC_ASK,
                     "inputSchema": agentic_search._PARAMS_ASK})
    if es_index.available():
        # ripgrep_search の直後に挿む。名前で位置を探す（index 決め打ちにしない）。
        _idx = next(i for i, d in enumerate(defs) if d["name"] == "ripgrep_search") + 1
        defs.insert(_idx, {"name": "es_search", "description": agentic_search._DESC_ES,
                           "inputSchema": agentic_search._PARAMS_ES_SEARCH})
    return defs + _ledger_tool_defs()


def _world() -> str:
    return os.environ.get("SHERPA_MCP_WORLD", "v1")


def _scope():
    raw = os.environ.get("SHERPA_MCP_SCOPE", "")
    paths = [s.strip() for s in raw.split("\n") if s.strip()]
    return paths or None


def _layer():
    """探す対象（層フィルタ）。未設定は `None`（`agentic_search.run_tool` が both 扱い）。
    親プロセス（`codex/mcp.py::_mcp_env`）が qa レンズのときだけ渡す。
    """
    return os.environ.get("SHERPA_MCP_LAYER") or None


def _ledger_required_extra() -> tuple[str, ...]:
    """`ledger_status` の完了判定へ追加で足す必須根拠種別（`SHERPA_MCP_LEDGER_REQUIRED_EXTRA`・親プロセスが台帳ゲートへ渡すものと同じ値のカンマ区切り）。
    未設定・空文字は空タプル。`EVIDENCE_KINDS` に無い値が 1 つでもあれば部分的に受け入れず空タプルにする（fail-closed）。
    """
    raw = os.environ.get("SHERPA_MCP_LEDGER_REQUIRED_EXTRA", "").strip()
    if not raw:
        return ()
    kinds = tuple(s.strip() for s in raw.split(",") if s.strip())
    if not all(k in investigation_ledger.EVIDENCE_KINDS for k in kinds):
        return ()
    return kinds


def _ledger_require_review() -> bool:
    """`ledger_status` の自己確認が `ledger_complete()` へ `require_review=True` を渡すか（`SHERPA_MCP_LEDGER_REQUIRE_REVIEW`・`"1"` のとき）。未設定・`"1"` 以外は要求しない。
    この env は自己確認用で、実際の完了判定（provider.py の台帳ゲート）とは独立。
    """
    return os.environ.get("SHERPA_MCP_LEDGER_REQUIRE_REVIEW", "").strip() == "1"


def _ledger_require_continuation_resolved() -> bool:
    """`ledger_status` の自己確認へ渡す `require_continuation_resolved`（`SHERPA_MCP_LEDGER_REQUIRE_CONTINUATION_RESOLVED`・「続き」ターンだけ `1`）。未設定・`"1"` 以外は要求しない。"""
    return os.environ.get("SHERPA_MCP_LEDGER_REQUIRE_CONTINUATION_RESOLVED", "").strip() == "1"


def _ask_disabled() -> bool:
    """確認ID 付き再送の実行ではラッパーが ask_user を無視するため、サーバ側でも隠す。"""
    return os.environ.get("SHERPA_MCP_ASK_DISABLED", "").strip().lower() in ("1", "true", "yes", "on")


def _sidecar_path() -> str | None:
    """run ごとのサイドカーファイルパス（`SHERPA_MCP_SIDECAR`・未設定は None＝無効）。"""
    p = os.environ.get("SHERPA_MCP_SIDECAR", "").strip()
    return p or None


_sidecar_write_failed_once = False  # 書込失敗の warning はプロセス寿命に 1 回だけ出す


def _sidecar_append(entry: dict) -> None:
    """サイドカーへ 1 行（JSON）追記する。fail-open（書けなくてもツール呼出は失敗させない）。
    渡すのは doc_id／ツール名／種別／時刻（と ask_user の質問）だけで、資料本文・回答本文は書かない。書込失敗は型と errno だけを stderr へ 1 回知らせる。
    """
    global _sidecar_write_failed_once
    path = _sidecar_path()
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        if not _sidecar_write_failed_once:
            _sidecar_write_failed_once = True
            print(f"[sherpa-mcp] sidecar write failed: {type(e).__name__} errno={e.errno}",
                  file=sys.stderr)


# 子エージェントの障害を親が観測するための閉じたコード。親の `--json` に子の MCP 呼出が現れないため、サイドカーが唯一の観測経路（`providers/codex/provider.py::_read_mcp_sidecar`）。本文・資料名は書かない。
_SIDECAR_ERROR_CODES = frozenset({
    agentic_search.GRAPH_REINGEST_ERROR_CODE, "graph_unavailable",
    "es_unavailable", "es_query_failed", "es_query_rejected", "read_io_failed"})


def _sidecar_error_code(name, result) -> None:
    """ツール結果が既知の障害コードを持つときだけ `{"kind": "error", ...}` を 1 行書く。
    コードは `error`（`isError`）・`error_code`（内部で障害を捕捉）・`degrade_reason`（`es_search` の縮退）のいずれかで、閉集合に含まれる値のときだけ書く（自由文は書かない）。
    """
    if not isinstance(result, dict):
        return
    for key in ("error", "error_code", "degrade_reason"):
        code = result.get(key)
        if isinstance(code, str) and code in _SIDECAR_ERROR_CODES:
            _sidecar_append({"kind": "error", "code": code, "tool": name, "ts": time.time()})
            return


# 項目ごとの未確認の記録。検索・読取ツール 6 本（任意引数 `item`＝台帳の項目 id・`agentic_search._ITEM_PARAM_SCHEMA`）の呼出結果を
# `investigation_ledger.append_coverage_atomic` で台帳の置き場（coverage.jsonl）へ記録する。親と子は同じ `SHERPA_MCP_LEDGER_DIR` を共有する。
_ITEM_PARAM_TOOLS = frozenset({
    "ripgrep_search", "es_search", "read_doc", "read_around", "file_head", "graph_neighbors"})
_ITEM_HITS_TOOLS = frozenset({"ripgrep_search", "es_search"})  # `hits` 配列を持つ形
_ITEM_READ_TOOLS = frozenset({"read_doc", "read_around", "file_head"})  # 単一文書読取の形
# 単一文書読取の error は「その doc_id を読めなかった」とみなす。検索・グラフ系の error は、`error_code` が既知の「読めない」系コードのときだけ unreadable にする。
_ITEM_UNREADABLE_ERROR_CODES = frozenset({"read_io_failed"})


def _coverage_outcome(name: str, result, is_error: bool) -> str | None:
    """`item` 付き呼出しの結果を `investigation_ledger.COVERAGE_OUTCOMES` の 7 区分へ落とす。`None` は記録しない対象（呼び出し側の引数誤り）。
    母集団側の打切り（`truncated`）・1 件あたりのバイト予算の打切り（`text_truncated`／`file_truncated`／`byte_clipped`／`partial_hit`／`partial_line`）は、
    利用統計「打ち切りの内訳」（`agentic_search._SEARCH_TRUNCATED_TOOLS`/`_BYTE_CLIP_TOOLS`）と同じ判定キーを使う。
    `timeout` はこの経路では発生しないが、`UNVERIFIED_REASON_CODES` の語彙に対応する区分として残す。
    """
    if not isinstance(result, dict):
        return "error"
    if is_error:
        if name in _ITEM_READ_TOOLS:
            if result.get("error_code") == agentic_search._READ_INVALID_ARGS_ERROR_CODE:
                return None  # 呼び出し側の引数誤り（範囲外・型不正）は読めない扱いにしない
            return "unreadable"
        if result.get("error_code") in _ITEM_UNREADABLE_ERROR_CODES:
            return "unreadable"
        return "error"
    if name in _ITEM_HITS_TOOLS:
        # 0 件の判定より前に、道具の失敗・母集団側の打切り・バイト予算での打切りを判定する（ヒットがあっても `truncated_docs` があれば truncated）。
        if name == "es_search" and result.get("degrade_reason") in (
                "es_unavailable", "es_query_failed", "es_query_rejected"):
            return "error"
        if result.get("truncated"):
            return "limit"
        if result.get("truncated_docs"):
            return "truncated"
        hits = result.get("hits")
        if isinstance(hits, list) and len(hits) == 0:
            return "no_hits"
        if isinstance(hits, list) and any(
                isinstance(h, dict) and (h.get("text_truncated") or h.get("file_truncated"))
                for h in hits):
            return "truncated"
        return "hit"
    if name == "graph_neighbors":
        if result.get("error_code"):  # `graph_unavailable`/`graph_internal_error`（isError ではない縮退）
            return "error"
        neighbors = result.get("neighbors")
        if isinstance(neighbors, list) and len(neighbors) == 0:
            return "no_hits"
        if result.get("truncated"):
            return "limit"
        return "hit"
    # read_doc/read_around/file_head（単一文書読取の形）
    if (result.get("text_truncated") or result.get("file_truncated") or result.get("truncated")
            or result.get("byte_clipped") or result.get("partial_hit") or result.get("partial_line")):
        return "truncated"
    return "hit"


def _record_item_coverage(name: str, args: dict, result, is_error: bool) -> None:
    """`item` 付きの検索・読取ツール呼出しの結果区分を coverage.jsonl へ追記する。fail-open:
    `item` が無い/不正・対象外ツール・`_coverage_outcome` が `None`・`SHERPA_MCP_LEDGER_DIR` 未設定（台帳を使わないターン・plain）は何もしない。書込失敗（`OSError`/`ValueError`）でもツール呼出は失敗させない。
    """
    if name not in _ITEM_PARAM_TOOLS:
        return
    outcome = _coverage_outcome(name, result, is_error)
    if outcome is None:
        return
    # `item` を除いた正規化キー（`_dedup_key`）にこの結果区分を控える。同じ条件が別の item で重複呼出しされたとき、run_tool を再実行せず転記できる（`_record_duplicate_item_coverage`）。
    _dkey = _dedup_key(name, args)
    if _dkey is not None and _dkey in _seen_tool_calls:
        _seen_tool_calls[_dkey] = outcome
    item_id = args.get("item") if isinstance(args, dict) else None
    if not isinstance(item_id, str) or not item_id:
        return
    ledger_dir = os.environ.get("SHERPA_MCP_LEDGER_DIR")
    if not ledger_dir:
        return
    try:
        investigation_ledger.append_coverage_atomic(Path(ledger_dir), item_id, name, outcome)
    except (OSError, ValueError):
        pass


# ツール結果 1 件あたりのバイト予算・調べる深さ連動の実効上限。
# API 経路と同じ実効値解決（バイト予算＝`effective_tool_result_max_bytes`・grep ヒット上限/読み取り窓＝`depth_profile.scaled_ratio`＋`effective_base`）を使う。
# 1 run 全体の累計バイト予算・呼び出し回数の上限は Codex 経路では持たない（到達後に本文系ツールが永続的に拒否されるため）。1 件あたりの上限は CLI の remote compact 失敗（`_CONTEXT_WINDOW_EXCEEDED_CODE`）対策として残す。
# `list_docs`/`folder_tree`（一覧のみ）と `ask_user`（制御系）はバイト予算の対象外。
_BUDGET_EXEMPT_TOOLS = frozenset({"list_docs", "folder_tree"}) | _LEDGER_TOOLS


def _env_int_override(var_name: str) -> int | None:
    """`SHERPA_MCP_TOOL_BUDGET_BYTES`/`_MAX_HITS`/`_WINDOW_CAP`（親の `CodexProvider` が調べる深さ連動込みで解決した実効値・`providers/codex/usage.py::_resolve_mcp_budget`）を優先して読む。
    未設定/不正値（0 以下・数値でない）は None を返し、呼び出し元はモジュール既定へ戻る。
    """
    raw = os.environ.get(var_name, "").strip()
    if not raw:
        return None
    try:
        v = int(raw)
    except ValueError:
        return None
    return v if v > 0 else None


def _json_bytes(obj) -> int:
    """`handle()` の最終再シリアライズと同じ関数・同じ引数（`ensure_ascii=False`）で実バイト数を測る（`content[].text` の最終出力の保証になる）。"""
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def _offset_arg(args: dict | None) -> int:
    """呼び出し引数の `offset` を int 化する。省略/不正値（負・数値でない）は 0。"""
    if not isinstance(args, dict):
        return 0
    try:
        v = int(args.get("offset") or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, v)


_PARTIAL_HIT_NOTE_WITH_OFFSET_TMPL = "先頭ヒットが長すぎるため本文を途中まで。次は offset={next_offset} から"
_PARTIAL_HIT_NOTE_NO_OFFSET = ("先頭ヒットが長すぎるため本文を途中まで"
                               "（続きは取れないため範囲を絞るか別の語で探してください）")
_TOOL_RESULT_BUDGET_TOO_SMALL_HINT = "1件も返せません。範囲を絞るか、1件あたりの上限を上げてください"


def _clip_hits_partial_first_hit(result: dict, max_bytes: int, *, offset: int, allow_next_offset: bool):
    """`_clip_hits_field` で先頭ヒット 1 件すら丸ごとは残せないときの最終手段。先頭ヒットの `text` だけを UTF-8 バイト単位で縮め、位置情報（`text` 以外の全キー）付きの 1 件を残す。
    `hits=[]`・`next_offset` を進めないページを返すと、続きの呼び出しが前回と同一引数になり重複拒否で進めなくなるため。
    位置情報だけでも `max_bytes` に収まらなければ `tool_result_budget_too_small` エラーを返す。エラー自体も収まらなければ `None`。
    """
    first = result["hits"][0]
    has_text_field = isinstance(first, dict) and isinstance(first.get("text"), str)
    body = first["text"] if has_text_field else (first if isinstance(first, str) else "")
    body_bytes = body.encode("utf-8")

    def _build(byte_len: int) -> dict:
        r = dict(result)
        if has_text_field:
            item = dict(first)
            item["text"] = agentic_search._clip_utf8_bytes(body, byte_len)
        elif isinstance(first, str):
            item = agentic_search._clip_utf8_bytes(body, byte_len)
        else:
            item = first  # 文字列でも text 付き dict でもない＝縮められる本文が無い
        r["hits"] = [item]
        r["truncated"] = True
        r["partial_hit"] = True
        if allow_next_offset:
            r["next_offset"] = offset + 1
            r["note"] = _PARTIAL_HIT_NOTE_WITH_OFFSET_TMPL.format(next_offset=offset + 1)
        else:
            r.pop("next_offset", None)
            r["note"] = _PARTIAL_HIT_NOTE_NO_OFFSET
        return r

    lo, hi, best = 0, len(body_bytes), -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if _json_bytes(_build(mid)) <= max_bytes:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if best >= 0:
        return _build(best), True

    err = {"error": "tool_result_budget_too_small", "hint": _TOOL_RESULT_BUDGET_TOO_SMALL_HINT,
          "offset": offset}
    if _json_bytes(err) <= max_bytes:
        return err, True
    return None


def _clip_hits_field(result: dict, max_bytes: int, *, offset: int, allow_next_offset: bool):
    """`hits` を持つ結果（`ripgrep_search`/`es_search`）を、末尾のヒットから落として `max_bytes` に収める。`truncated=true` は常に付ける。
    `next_offset` は `allow_next_offset`（ツールが `ripgrep_search`）のときだけ、「呼び出し引数の `offset`」＋「今回残した件数」で計算して付ける（`es_search` はページングを持たない）。
    先頭 1 件すら丸ごとは残せない場合は `_clip_hits_partial_first_hit` に委譲する。envelope（`hits=[]`）自体も収まらなければ `None`。
    """
    hits = result.get("hits")
    if not isinstance(hits, list):
        return None
    n = len(hits)

    def _build(k: int) -> dict:
        r = dict(result)
        r["hits"] = hits[:k]
        r["truncated"] = True
        if not allow_next_offset:
            r.pop("next_offset", None)
        elif k < n:
            r["next_offset"] = offset + k
        # allow_next_offset かつ hits を削っていない場合は、元の next_offset（無ければ無し）をそのまま残す。
        return r

    lo, hi, best_k = 0, n, -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if _json_bytes(_build(mid)) <= max_bytes:
            best_k = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if best_k > 0:
        return _build(best_k), True
    if best_k < 0:
        return None  # envelope（0 件ページ）すら収まらない
    if n == 0:
        return _build(0), True  # 元々 0 件＝縮める本文が無い正当な 0 件ページ
    return _clip_hits_partial_first_hit(result, max_bytes, offset=offset, allow_next_offset=allow_next_offset)


_PARTIAL_LINE_NOTE_TMPL = "先頭行が長すぎるため行の途中まで。以降は start_line={next_start} から"


def _clip_read_doc_partial_first_line(result: dict, max_bytes: int, first_line: str, start_line: int):
    """`_clip_read_doc_field` で 1 行も丸ごとは残せないときの最終手段。先頭行の本文をバイト単位で縮めて、空文字を返さない。
    `end_line=start_line`（その行を消費済み扱い）にして進捗を保証する（進捗ゼロだと続きが重複拒否で進まない）。行の残りは読めない（`partial_line=true`・`note` で明示）。
    """
    first_bytes = first_line.encode("utf-8")

    def _build(byte_len: int) -> dict:
        r = dict(result)
        r["text"] = agentic_search._clip_utf8_bytes(first_line, byte_len)
        r["truncated"] = True
        r["partial_line"] = True
        r["end_line"] = start_line
        r["note"] = _PARTIAL_LINE_NOTE_TMPL.format(next_start=start_line + 1)
        return r

    lo, hi, best = 0, len(first_bytes), -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if _json_bytes(_build(mid)) <= max_bytes:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if best < 0:
        return None
    return _build(best), True


def _clip_read_doc_field(result: dict, max_bytes: int):
    """`text`＋`end_line`（`read_doc` の形）を持つ結果を、行境界を保ったまま `max_bytes` に収める。`end_line`/`total_lines` を残し、`truncated=true` を付ける（続きは `start_line=end_line+1`）。
    1 行も残せない場合は `_clip_read_doc_partial_first_line` に委譲する。`text`/`end_line` の両方を持たない結果には適用できない（`None`）。
    """
    text = result.get("text")
    end_line = result.get("end_line")
    if not isinstance(text, str) or not isinstance(end_line, int):
        return None
    lines = text.split("\n") if text else []
    n = len(lines)
    start_line = result.get("start_line")
    if not isinstance(start_line, int):
        start_line = end_line - n + 1  # 行数から逆算（start_line を持たない読取系向けの近似）

    def _build(k: int) -> dict:
        r = dict(result)
        r["text"] = "\n".join(lines[:k])
        r["truncated"] = True
        r["end_line"] = start_line + k - 1 if k > 0 else start_line - 1
        return r

    lo, hi, best_k = 0, n, -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if _json_bytes(_build(mid)) <= max_bytes:
            best_k = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if best_k > 0:
        return _build(best_k), True
    if n == 0:
        return None
    return _clip_read_doc_partial_first_line(result, max_bytes, lines[0], start_line)


# 構造が分からない結果、または 0 件/0 行まで削っても収まらない極端なケース向けの最終防衛線。続きの位置を保証できないことを明示する。
_CLIP_FALLBACK_NOTE = "結果が大きすぎるため先頭のみ。範囲を絞って再実行してください"


def _clip_tool_result(result, name: str | None = None, args: dict | None = None):
    """1 件あたりのバイト予算を超えた結果を切り詰める（`SHERPA_MCP_TOOL_BUDGET_BYTES` 優先・無ければ `effective_tool_result_max_bytes`）。
    `run_tool` 自身が収まりを保証するため、ここに来るのは通常その保証が効かない極端なケースだけの最終防衛線。
    `name`・`args` は `handle()` が渡す（`next_offset` の付与可否と基準位置の判定に使う）。省略時は `next_offset` を付けず `offset=0`。
    ① `hits` を持つ結果 → `_clip_hits_field`。
    ② `text`＋`end_line` を持つ結果 → `_clip_read_doc_field`。
    ③ どちらでもない、または削っても収まらない → 直列化済み JSON の先頭の断片を `text` に残す（fail-open・`note` で続きの位置を保証できないと明示）。
    戻り値は `(result_or_clipped, clipped: bool)`。
    """
    if not isinstance(result, dict):
        return result, False
    max_bytes = _env_int_override("SHERPA_MCP_TOOL_BUDGET_BYTES") \
        or agentic_search.effective_tool_result_max_bytes()
    size = _json_bytes(result)
    if size <= max_bytes:
        return result, False

    structured = _clip_hits_field(result, max_bytes, offset=_offset_arg(args),
                                  allow_next_offset=(name == "ripgrep_search"))
    if structured is None:
        structured = _clip_read_doc_field(result, max_bytes)
    if structured is not None:
        return structured

    raw = json.dumps(result, ensure_ascii=False)

    def _wrapped_bytes(text: str) -> int:
        clipped = size - len(text.encode("utf-8"))
        envelope = json.dumps({"truncated": True, "clipped_bytes": clipped, "text": text,
                               "note": _CLIP_FALLBACK_NOTE}, ensure_ascii=False)
        return len(envelope.encode("utf-8"))

    # 二分探索: `raw` の UTF-8 バイト接頭辞長 `mid` を調整し、包んだ最終形が `max_bytes` 以下になる最大の `mid` を探す。空 text でも収まらない極端な予算では空文字のまま返す。
    lo, hi, best = 0, size, ""
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = agentic_search._clip_utf8_bytes(raw, mid)
        if _wrapped_bytes(candidate) <= max_bytes:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    clipped_bytes = size - len(best.encode("utf-8"))
    return {"truncated": True, "clipped_bytes": clipped_bytes, "text": best,
            "note": _CLIP_FALLBACK_NOTE}, True


# 同一クエリの重複実行の抑止。同じ ripgrep_search が同条件で二重に実行され、同一結果が文脈に積まれるのを防ぐ。
# プロセス寿命（エージェントごと）で (ツール名, 引数の正規化 JSON) だけを覚え、結果本文は保持しない。
_DUPLICATE_CALL_CACHE_MAX = 64
# 値は結果区分（`_coverage_outcome` の戻り値）。初回呼出しの結果が確定するまでは `None`（`_record_item_coverage` が後から埋める）。
_seen_tool_calls: "OrderedDict[tuple, str | None]" = OrderedDict()
# ask_user は専用分岐で処理済みのため対象外。`_BUDGET_EXEMPT_TOOLS`（一覧のみの土台系）も対象外。
_DUPLICATE_CHECK_EXEMPT_TOOLS = frozenset({"ask_user"}) | _BUDGET_EXEMPT_TOOLS


def _dedup_key(name: str, args) -> tuple | None:
    """重複判定と結果区分キャッシュが共有する正規化キー。`item`（台帳の項目 id）は除く。直列化できない引数は `None`（同一性を判定できない・fail-open）。"""
    try:
        _key_args = ({k: v for k, v in args.items() if k != "item"}
                    if isinstance(args, dict) else args)
        return (name, json.dumps(_key_args, sort_keys=True, ensure_ascii=False))
    except TypeError:
        return None


def _is_duplicate_tool_call(name: str, args: dict) -> bool:
    """同一 `(name, 正規化した args)` の 2 回目以降の呼出なら True（初回は記録するだけ）。`_dedup_key` でキー順・`item` の違いを同一視する。
    件数上限（`_DUPLICATE_CALL_CACHE_MAX`）に達したら最も古いキーから捨てる（LRU）。
    """
    key = _dedup_key(name, args)
    if key is None:
        return False  # 直列化できない引数は重複扱いしない（fail-open）
    if key in _seen_tool_calls:
        _seen_tool_calls.move_to_end(key)
        return True
    _seen_tool_calls[key] = None
    if len(_seen_tool_calls) > _DUPLICATE_CALL_CACHE_MAX:
        _seen_tool_calls.popitem(last=False)
    return False


def _record_duplicate_item_coverage(name: str, args: dict) -> None:
    """重複拒否した呼出しでも、`item` が付いていれば初回の結果区分（`_seen_tool_calls`）をこの item にも記録する（しないと「調べた記録が無い」と誤判定される）。
    結果区分が未確定・`item` 無し・対象外ツール・記録しない対象のときは何もしない（fail-open）。
    """
    if name not in _ITEM_PARAM_TOOLS:
        return
    item_id = args.get("item") if isinstance(args, dict) else None
    if not isinstance(item_id, str) or not item_id:
        return
    key = _dedup_key(name, args)
    outcome = _seen_tool_calls.get(key) if key is not None else None
    if outcome is None:
        return
    ledger_dir = os.environ.get("SHERPA_MCP_LEDGER_DIR")
    if not ledger_dir:
        return
    try:
        investigation_ledger.append_coverage_atomic(Path(ledger_dir), item_id, name, outcome)
    except (OSError, ValueError):
        pass


def _ok(rid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid, code: int, msg: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}}


def handle(req: dict) -> dict | None:
    """JSON-RPC 1 件を処理して応答 dict を返す。通知（id 無し）は None＝応答しない。"""
    method = req.get("method")
    rid = req.get("id")
    is_notification = "id" not in req
    if method == "initialize":
        # protocolVersion はクライアント要求をそのまま返す（未指定なら既定）。
        pv = (req.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION
        return _ok(rid, {"protocolVersion": pv, "capabilities": {"tools": {}},
                         "serverInfo": SERVER_INFO})
    if method == "tools/list":
        return _ok(rid, {"tools": _tool_defs()})
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if _toolset() == "plain" and name not in _PLAIN_TOOLSET:
            # plain は es_search・graph_neighbors・ask_user だけ。tools/list に出していなくても、直接呼ばれたら存在しないツールと同じエラーで拒否する（fail-closed）。
            err_body = {"error": f"unknown tool: {name}"}
            return _ok(rid, {"content": [{"type": "text", "text": json.dumps(err_body, ensure_ascii=False)}],
                             "isError": True})
        if name in _LEDGER_TOOLS:
            # 台帳の応答は探索量に計上せず、クリップ・重複拒否・読取サイドカーの対象外にする。
            result = _run_ledger_tool(name, args)
            return _ok(rid, {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                             "isError": bool(result.get("error"))})
        if name == "ask_user":
            # ask_user は質問であって検索ツールではない。質問カードの表示はラッパー（`agents._run_authoring`）が行い、ここは「届いた・追加調査せず要約して終了せよ」を返すだけ（2 回目以降は別文言で調査続行を促す）。
            # 確認ID 付き再送では初回でも `_ASK_RESULT_AGAIN` を返す（質問カードを出さないまま調査を打ち切らせない）。
            if _ask_disabled():
                return _ok(rid, {"content": [{"type": "text", "text": _ASK_RESULT_AGAIN}], "isError": False})
            _ASK_STATE["count"] += 1
            if _ASK_STATE["count"] == 1:
                text = _ASK_RESULT_FIRST
                # 子（worker/evaluator）の ask_user は親の `--json` に現れず、呼び出し元が親か子か区別できないため、初回の質問は常にサイドカーへも書く（provider.py が `codex_question is None` のときだけサイドカー分を使う）。
                _q = agentic_search._question_from_args(args)
                if isinstance(_q, dict):
                    _sidecar_append({"kind": "ask_user", "ts": time.time(), "question": _q})
            else:
                text = _ASK_RESULT_AGAIN
            return _ok(rid, {"content": [{"type": "text", "text": text}], "isError": False})
        if name not in _DUPLICATE_CHECK_EXEMPT_TOOLS and _is_duplicate_tool_call(name, args):
            # 同一条件の再実行は run_tool を呼ばず本文も再送しない。
            _sidecar_append({"kind": "limit", "field": "duplicate_tool_call", "ts": time.time()})
            _record_duplicate_item_coverage(name, args)  # 初回の結果区分をこの item にも転記
            err_body = {"error": "duplicate_tool_call",
                       "hint": "同じ条件の検索は既に実行済みです。条件を変えてください。"}
            return _ok(rid, {"content": [{"type": "text", "text": json.dumps(err_body, ensure_ascii=False)}],
                             "isError": True})
        try:
            # 調べる深さ連動込みの実効 hits/window（`SHERPA_MCP_TOOL_MAX_HITS`/`_WINDOW_CAP`）とバイト予算を `run_tool()` の内部クリップにも渡す。env 未設定/不正値は `None`＝`run_tool()` の既定。後段の `_clip_tool_result` は最終形を保証する多層防御。
            result, _docs, _cites, _cards = tool_dispatch.run_tool(
                name, args, _world(), _scope(), layer=_layer(),
                max_hits=_env_int_override("SHERPA_MCP_TOOL_MAX_HITS"),
                window_cap=_env_int_override("SHERPA_MCP_TOOL_WINDOW_CAP"),
                tool_result_max_bytes=_env_int_override("SHERPA_MCP_TOOL_BUDGET_BYTES"),
                graph_only=(_toolset() == "plain"))
        except GraphSchemaEraError as e:
            # `graph_neighbors` が検知した旧世代グラフは、JSON-RPC のプロトコルエラーにせず通常のツール結果（`isError` あり・`content[].text` に機械可読コード）で返す。`providers/codex/mcp.py::_graph_schema_era_from_item` が `GraphSchemaEraError` を再構成する。
            err_body = {"error": agentic_search.GRAPH_REINGEST_ERROR_CODE,
                        "world": e.world, "stored_era": e.stored_era}
            _sidecar_error_code(name, err_body)  # 子が受け取った障害も親が観測できるようにする
            _record_item_coverage(name, args, err_body, True)
            return _ok(rid, {"content": [{"type": "text", "text": json.dumps(err_body, ensure_ascii=False)}],
                             "isError": True})
        is_error = bool(isinstance(result, dict) and result.get("error"))
        _sidecar_error_code(name, result)
        # `run_tool()` 自身が内部で行った打ち切りをサイドカーへ記録する（API 経路の `agentic_search._SEARCH_TRUNCATED_TOOLS`/`_BYTE_CLIP_TOOLS` と同じ判定キー・語彙）。`_tool_result_clipped_recorded` は 1 回の呼び出しで `tool_result_clipped` を二重に書かないためのフラグ。
        _tool_result_clipped_recorded = False
        if isinstance(result, dict):
            if name in agentic_search._SEARCH_TRUNCATED_TOOLS and result.get("truncated"):
                _sidecar_append({"kind": "limit", "field": "search_truncated", "ts": time.time()})
            if name in agentic_search._BYTE_CLIP_TOOLS and (result.get("text_truncated") or result.get("byte_clipped")):
                _sidecar_append({"kind": "limit", "field": "tool_result_clipped", "ts": time.time()})
                _tool_result_clipped_recorded = True
        if not is_error:
            # 子が読んだ doc_id をサイドカーへ（本文は書かない・失敗した呼出は数えない）。
            if name in READ_DOC_TOOLS:
                d = args.get("doc_id")
                if isinstance(d, str) and d:
                    _sidecar_append({"kind": "read", "tool": name, "doc_id": d, "ts": time.time()})
            elif name in LISTED_DOC_TOOLS:
                d = args.get("doc_id")
                if isinstance(d, str) and d:
                    _sidecar_append({"kind": "listed", "tool": name, "doc_id": d, "ts": time.time()})
            elif name == "compare_documents":
                for k in COMPARE_DOC_ID_ARGS:
                    d = args.get(k)
                    if isinstance(d, str) and d:
                        _sidecar_append({"kind": "read", "tool": name, "doc_id": d, "ts": time.time()})
        if name not in _BUDGET_EXEMPT_TOOLS:
            result, _clipped = _clip_tool_result(result, name=name, args=args)
            if _clipped and not _tool_result_clipped_recorded:
                _sidecar_append({"kind": "limit", "field": "tool_result_clipped", "ts": time.time()})
            # `_clip_tool_result` が結果を `{"error": ...}` へ置き換えることがあるため、クリップ後に error 形へ変わった分もここで拾い直す。
            is_error = is_error or bool(isinstance(result, dict) and result.get("error"))
        # `item` 付き呼出しの結果区分を台帳へ記録する（Codex へ返す最終形＝クリップ後の `result`/`is_error` を使う）。
        _record_item_coverage(name, args, result, is_error)
        # MCP 標準＝content[].text。Codex が読む本文は run_tool の結果。
        text = json.dumps(result, ensure_ascii=False)
        return _ok(rid, {"content": [{"type": "text", "text": text}], "isError": is_error})
    if is_notification:  # notifications/initialized 等は応答しない
        return None
    return _err(rid, -32601, f"method not found: {method}")


def serve(stdin=None, stdout=None) -> None:
    """stdin から改行区切り JSON-RPC を読み、stdout に応答を書く（MCP stdio ループ）。"""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue  # 壊れた行は黙って捨てる（プロトコルを落とさない）
        try:
            resp = handle(req)
        except Exception as e:  # ツール例外でもサーバは落とさない（その応答だけ error）
            resp = _err(req.get("id"), -32603, f"{type(e).__name__}: {e}")
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            stdout.flush()


if __name__ == "__main__":
    serve()
