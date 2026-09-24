"""Sherpa MCP サーバ（stdio・自前実装・SDK 非依存）。

Codex（自律エージェント）に Sherpa の read-only ツール一家（grep / 精読 / グラフ近傍 / ES）を渡すための
**MCP(stdio) サーバ**。ツールの実装は `agentic_search.run_tool` を**そのまま再利用**（cloud LLM と実装共通＝
ツールの中身は1本・rv 方針）。Codex は別プロセスの自律エージェントなので API の function-calling では渡せず、
MCP がツールを渡す唯一の素直な口（network 不要＝本サーバ＝Sherpa 側プロセスが Neo4j/ES に接続する）。

- **world / scope / layer は起動時の環境変数で固定**（`SHERPA_MCP_WORLD` / `SHERPA_MCP_SCOPE` /
  `SHERPA_MCP_LAYER`）＝Codex/LLM には選ばせない（範囲外探索を防ぐ・read-only は run_tool 側で担保）。
- トランスポート＝**改行区切り JSON-RPC 2.0**（MCP stdio）。stdin から1行1メッセージ、stdout に応答、ログは stderr。
- `ask_user` は Codex にも公開する（S2・ask_user-improvements.md）。codex exec は非対話だが、質問は
  ラッパー（`agents._run_authoring` の mcp_tool_call 監視）が question イベントとしてフロントへ届ける
  ＝ここはツール結果（「届いた・調査をやめて要約せよ」）を返すだけ。乱用ガードの1つ＝**1実行1回**は
  本サーバのプロセス寿命で数える——**エージェント（MCP 接続）ごとに別プロセスが立つ**ため
  （下の DEPTH-2 S3b 参照）、この上限は multi_agent の子エージェントごとに独立に働く（親と子は
  別カウント）。
- `SHERPA_MCP_ASK_DISABLED=1`（確認ID 付き再送＝ラッパーが ask_user を無視する実行）が立っていたら
  **ask_user を tools/list から外す**（呼べる道具を最初から見せない＝最強のガード・RV HIGH 2026-07-07）。
  それでも呼ばれたら（Codex がプロンプト指示に反した場合の防御）初回でも `_ASK_RESULT_AGAIN` を返す
  ＝質問カードを出さないまま調査を打ち切らせない（ラッパー側 `_ask_disabled` と同じフラグを共有）。
- DEPTH-2 S3b（`docs/proposals/2026-09-17-深さの再定義とレビュー巡.md` §2.6/§9.1）: Codex の
  multi_agent（`spawn_agent`）で起動された子エージェントの MCP 呼出は、親プロセスの `--json`
  イベントには構造化イベントとして現れない（実機確認済み）。子・親のどちらから呼ばれたかを
  本サーバは区別できないため、`SHERPA_MCP_SIDECAR`（JSONL パス・model-shell の書込許可領域の外＝
  通常は codex_home 配下）が設定されていれば読取系ツールの doc_id と ask_user の質問だけを
  **本文なしで**そのファイルへ追記する——本サーバ自身は permission profile の外の別プロセスなので
  書けるが、Codex の shell ツールはそこへ書けない（サイドカーが読取専用の観測経路であり続ける
  前提はこの書込不可に依る）。呼び出し元
  （`providers/codex/provider.py`）が codex exec 終了後にこのファイルを読み、子だけが読んだ資料を
  出典へ・子の ask_user を確認カードへ合流させる。未設定（`SHERPA_MCP_SIDECAR` なし）なら何もしない
  （既存の単一エージェント実行は無変更）。
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from . import agentic_search, es_index, investigation_ledger
from .ingest.world_neo4j import GraphSchemaEraError

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "sherpa", "version": "0.1.0"}

# DEPTH-2 S3b: 読取系ツールの分類（provider.py 側の出典収集と同じ区分＝二重管理を避けるため
# `sherpa/providers/codex/provider.py` がこのタプルを import して使う）。`LISTED_DOC_TOOLS`
# （シート一覧のみ）は sources_verified には数えない扱いを provider 側が踏襲する。
READ_DOC_TOOLS = ("read_doc", "read_around", "doc_outline",
                  "xlsx_range", "docx_paragraphs", "pptx_slides", "pdf_pages", "file_head")
LISTED_DOC_TOOLS = ("xlsx_sheets",)
COMPARE_DOC_ID_ARGS = ("left_doc_id", "right_doc_id", "source_doc_id")

# S2: ask_user は検索ツールではない（run_tool に実装は無い）＝ここでは Codex に返す**ツール結果**だけを持つ。
# 1実行1回まで（本サーバはエージェント＝MCP 接続ごとに別プロセスで起動する＝プロセス寿命＝
# そのエージェント1体ぶんの実行＝モジュール変数で数える。multi_agent の子エージェントは親とは
# 別プロセス＝別カウント＝モジュール docstring の DEPTH-2 S3b 参照）。
_ASK_STATE = {"count": 0}
_ASK_RESULT_FIRST = "質問はユーザーに届きました。追加調査はせず、ここまでに確認できたことを省略せずまとめて終了してください。"
_ASK_RESULT_AGAIN = "既に質問済みです。調査を続けて回答をまとめてください。"

_LEDGER_TOOLS = frozenset({"ledger_manifest_set", "ledger_item_put", "ledger_status"})

# 素の Codex モード（`plain`・docs/proposals/2026-09-24-素のCodexモード.md §1.2/§3）で公開する
# ツールの全体。`_tool_defs()`・`handle()` のどちらもこの集合だけを見る（値のぶれを作らない）。
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
    ]


def _run_ledger_tool(name: str, args: dict) -> dict:
    ledger_dir = os.environ.get("SHERPA_MCP_LEDGER_DIR")
    if not ledger_dir:
        # 書込先が未設定ならfail-closed。別の場所へ台帳を作らない。
        return {"error": "ledger_unavailable"}
    directory = Path(ledger_dir)
    try:
        if name == "ledger_status":
            snapshot = investigation_ledger.load_ledger(directory)
            verdict = investigation_ledger.ledger_complete(
                snapshot, required_extra=_ledger_required_extra())
            return {
                "complete": verdict.complete, "manifest_invalid": verdict.manifest_invalid,
                "items": len(snapshot.items) + len(snapshot.invalid_ids),
                "non_terminal": verdict.non_terminal_ids, "unsatisfied": verdict.unsatisfied,
                "invalid": verdict.invalid_ids, "missing": verdict.missing_ids,
                "unregistered": verdict.unregistered_ids, "counts": verdict.terminal_counts,
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

        manifest_path = directory / "manifest.json"
        if manifest_path.is_symlink():
            return {"error": "ledger_manifest_invalid", "problems": ["既存の manifest が symlink です"]}
        created_at = None
        if manifest_path.exists():
            # 内容が規約に合わない通常ファイルは、検証済みの入力で修復できる（本体の継続ゲートは
            # 「修復してから続けて」と催促する——ここで拒否すると修復手段が無くなる）。`created_at` は
            # 既存が文字列ならそれを保持し、無ければ作り直す。読取不能（OSError）は外側で拒否。
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
        # ファイル操作の失敗はfail-closed。MCPのエラーとして返し、自動再試行しない。
        print(f"[sherpa-mcp] {name} failed: {type(exc).__name__} errno={exc.errno}", file=sys.stderr)
        return {"error": "ledger_write_failed", "problems": [f"{type(exc).__name__}: errno={exc.errno}"]}


def _toolset() -> str:
    """MCP が公開するツールの絞り込み（`SHERPA_MCP_TOOLSET`・素の Codex モード＝`plain` 専用）。
    `"plain"` だけを特別扱いし、それ以外（未設定・`"full"`・想定外の値）は全て従来どおりのフル
    公開に倒す（fail-safe・provider.py が明示的に plain を立てた実行だけを絞る）。"""
    return "plain" if os.environ.get("SHERPA_MCP_TOOLSET", "").strip().lower() == "plain" else "full"


# plain で公開するグラフの説明。standard 用の説明（`agentic_search._DESC_GRAPH`）は plain で公開しない
# ツール（ripgrep_search・read_doc・read_around・list_docs）の使用を指示するため使わない。
# es_search は plain では出さない（1 回ごとにクエリの埋め込みを呼び、Azure が不安定だと再送で数分止まる）。
_DESC_GRAPH_PLAIN = (
    "関係グラフから、ある名前（プログラム/コピーブック/ジョブ/データ項目/テーブルなど）の関連部品"
    "（コピー・呼び出し・参照・関連文書（言及）の近傍）を、辺ごとの種類と向き（from→to）付きの経路で返す。"
    "COPIES／INVOKES／ACCESSES／CONTAINS だけの経路は構造的な依存＝根拠にしてよい（影響は矢印をさかのぼる: "
    "A →COPIES→ B は B を変えると A が影響を受ける）。DOCUMENTS（言及）・CORRESPONDS_TO（同名の対応）や "
    "`unverified` の辺を含む経路は候補＝原本で確認する。名前は完全一致で引く（シェルで正確な名前を見つけてから"
    "渡す）。近傍が上限で切られたときは truncated:true と count（総数）が付く＝その範囲は未確認として扱う。"
)


def _tool_defs() -> list:
    """公開ツール定義（schema は agentic_search と共通＝二重管理しない）。ES はインデックスがある時だけ。
    `read_doc`/`doc_outline`/`glob_search` は `read_around` 等と同じ土台系ツール（ES/graph の可用性に
    依存しない）＝常に公開する。並びは `agentic_search.openai_tools` と同じ「構造を掴む→通読→精読」
    （ripgrep_search の直後に glob_search、read_around の直前に doc_outline・read_doc）。
    S2 RV HIGH: `SHERPA_MCP_ASK_DISABLED=1`（確認ID 付き再送）の実行では ask_user 自体を外す
    （呼べる道具を最初から見せない＝プロンプト指示だけに頼らない最強のガード）。
    探す対象（層）が限定されている間は `graph_neighbors` 自体を外す（呼べる道具を最初から見せない・
    `run_tool` 側の拒否と多層防御・迂回路を tools/list の時点で塞ぐ）。

    `_toolset()` が `"plain"` のときは `_PLAIN_TOOLSET`（graph_neighbors・ask_user）だけを
    返す——それぞれの既存の出す条件（層・ask 無効）はそのまま守る。台帳・grep/読取・
    list_docs 系は出さない（`handle()` 側も同じ集合だけを許可し、直接呼ばれても拒否する）。"""
    if _toolset() == "plain":
        defs = []
        if _layer() in (None, "both"):
            defs.append({"name": "graph_neighbors", "description": _DESC_GRAPH_PLAIN,
                        "inputSchema": agentic_search._PARAMS_GRAPH})
        if not _ask_disabled():
            defs.append({"name": "ask_user", "description": agentic_search._DESC_ASK,
                        "inputSchema": agentic_search._PARAMS_ASK})
        return defs
    defs = [
        {"name": "list_docs", "description": agentic_search._DESC_LIST_DOCS,
         "inputSchema": agentic_search._PARAMS_LIST_DOCS},
        # K6（`docs/proposals/2026-09-04-グラフのソース正典化.md` §3・§4b S1）: list_docs と同じ
        # 台帳ベースの土台系ツール（ES/graph 可用性に依存しない）＝常に公開する。
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
        # GEN-DIFF（`docs/proposals/2026-09-03-世代間diff比較.md` §5）: ES/graph の可用性に依存しない
        # 土台系ツール（read_around 等と同じ扱い）＝常に公開する。
        {"name": "compare_documents", "description": agentic_search._DESC_COMPARE,
         "inputSchema": agentic_search._PARAMS_COMPARE},
        # S3b（原本読取ツール・`docs/proposals/2026-09-10-Codex原本直読と調査スキル.md` §2-9）:
        # `file_head`（テキスト・コード）は層に関係なく常に公開する（`run_tool` 側が層で個別に
        # 絞る）。Office/PDF の5本（下）は探す対象がソースに限定されている間は外す（Office/PDF は
        # 常に docs 側扱いのため・`run_tool` 側の拒否と多層防御）。
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
        # S2: ask_user を Codex にも公開（description は agentic と同じ制約文言＝_DESC_ASK・schema も共通）。
        defs.append({"name": "ask_user", "description": agentic_search._DESC_ASK,
                     "inputSchema": agentic_search._PARAMS_ASK})
    if es_index.available():
        # ripgrep_search の直後に挿む。手前のツール構成が変わっても崩れないよう、index 決め打ちでは
        # なく名前で位置を探す。
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
    """探す対象（層フィルタ）。未設定は `None`（`agentic_search.run_tool` 側で both 扱い）。
    親プロセス（`codex/mcp.py::_mcp_env`）が qa レンズのときだけ渡す＝impact/troubleshoot・author は
    未設定のまま（グラフ traversal 非適用・Codex 自身の追加探索の既知の非対称性）。"""
    return os.environ.get("SHERPA_MCP_LAYER") or None


def _ledger_required_extra() -> tuple[str, ...]:
    """`ledger_status` の完了判定へ追加で足す必須根拠種別（`SHERPA_MCP_LEDGER_REQUIRED_EXTRA`・
    親プロセス（provider.py）がそのターンの台帳ゲートへ渡すものと同じ値をカンマ区切りで渡す）。
    未設定・空文字は空タプル（今までどおり）。1つでも `EVIDENCE_KINDS` の閉集合に無い値が
    混ざっていたら、部分的に受け入れず空タプルとして扱う（語彙外を黙って無視しない・
    fail-closed・壊れた値から推測で埋めない）。"""
    raw = os.environ.get("SHERPA_MCP_LEDGER_REQUIRED_EXTRA", "").strip()
    if not raw:
        return ()
    kinds = tuple(s.strip() for s in raw.split(",") if s.strip())
    if not all(k in investigation_ledger.EVIDENCE_KINDS for k in kinds):
        return ()
    return kinds


def _ask_disabled() -> bool:
    """S2 RV HIGH: この実行（確認ID 付き再送）ではラッパーが ask_user を無視する＝サーバ側でも隠す。"""
    return os.environ.get("SHERPA_MCP_ASK_DISABLED", "").strip().lower() in ("1", "true", "yes", "on")


def _sidecar_path() -> str | None:
    """DEPTH-2 S3b: run ごとのサイドカーファイルパス（`SHERPA_MCP_SIDECAR`・未設定は None＝無効）。"""
    p = os.environ.get("SHERPA_MCP_SIDECAR", "").strip()
    return p or None


_sidecar_write_failed_once = False   # 書込失敗の warning はプロセス寿命（エージェントごと）に1回だけ出す（過剰ログ防止）


def _sidecar_append(entry: dict) -> None:
    """サイドカーへ1行（JSON）追記する。**fail-open**（書けなくてもツール呼出自体は失敗させない・
    ディスク不調やパス消失を検索結果へ波及させない）。呼び出し元は doc_id／ツール名／種別／時刻
    （と ask_user の質問）だけを渡すこと——資料本文・回答本文は一切書かない契約。書込失敗は
    完全に無言にはせず、型と errno だけ（本文・パスは出さない）を stderr へ1回だけ知らせる。"""
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


# 子エージェント（`spawn_agent` された worker/evaluator）の障害を親が観測するための閉じたコード。
# 親の `--json` には子の MCP 呼出が現れないため、サイドカーが唯一の観測経路
# （`providers/codex/provider.py::_read_mcp_sidecar`）。本文・資料名は書かない。
_SIDECAR_ERROR_CODES = frozenset({
    agentic_search.GRAPH_REINGEST_ERROR_CODE, "graph_unavailable",
    "es_unavailable", "es_query_failed", "es_query_rejected", "read_io_failed"})


def _sidecar_error_code(name, result) -> None:
    """ツール結果が既知の障害コードを持つときだけ `{"kind": "error", ...}` を1行書く。

    コードは `error`（結果そのものが障害＝`isError`）・`error_code`（結果は返るが内部で障害を
    捕捉した）・`degrade_reason`（`es_search` の縮退）のいずれかに載る——いずれも閉集合に
    含まれる値のときだけ書き、自由文（`error` の日本語メッセージ等）は書かない。
    """
    if not isinstance(result, dict):
        return
    for key in ("error", "error_code", "degrade_reason"):
        code = result.get(key)
        if isinstance(code, str) and code in _SIDECAR_ERROR_CODES:
            _sidecar_append({"kind": "error", "code": code, "tool": name, "ts": time.time()})
            return


# ---- ツール結果1件あたりのバイト予算・調べる深さ連動の実効上限（Azure 実機の
#      context_window_exceeded 是正）----
# API 経路（`agentic_search.py` の3 dialect ループ／`providers/openai.py` 等の `_agentic_loop`）と
# 同じ実効値解決（バイト予算＝`effective_tool_result_max_bytes`・grep ヒット上限/読み取り窓＝
# `depth_profile.scaled_ratio`＋`effective_base`・system_settings 未解決時のコード既定フォール
# バックも同関数内で完結）をそのまま使う——車輪の再発明をしない。
#
# 累計（1 run 全体）のツール結果バイト予算・ツール呼び出し回数の上限は Codex 経路では持たない
# （撤去済み）: MCP プロセス単位の累計値は Codex CLI の自動圧縮で文脈が空いてもリセットされず、
# 到達後は本文系ツールが永続的に拒否される——「調査の途中で閉じる」を作る側だった
# （`docs/proposals/2026-09-21-調査台帳を文脈の外に置く.md` §1/§2）。1件あたりの上限は
# CLI の remote compact 失敗（`_CONTEXT_WINDOW_EXCEEDED_CODE`）という実障害の根拠があるため残す。
#
# `list_docs`/`folder_tree`（一覧のみ・本文を返さない土台系ツール）と `ask_user`（制御系・専用分岐で
# 既に処理済み）は1件あたりのバイト予算の対象外。
_BUDGET_EXEMPT_TOOLS = frozenset({"list_docs", "folder_tree"}) | _LEDGER_TOOLS


def _env_int_override(var_name: str) -> int | None:
    """`SHERPA_MCP_TOOL_BUDGET_BYTES`/`_MAX_HITS`/`_WINDOW_CAP`（親プロセス＝`CodexProvider` が
    調べる深さ連動込みで解決した実効値を渡す・`provider.py::_resolve_mcp_budget_env` 参照）を
    優先して読む共通ヘルパ。未設定/不正値（0以下・数値でない）は None を返し、呼び出し元は従来の
    モジュール既定（`effective_tool_result_max_bytes`・`agentic_search.run_tool()` の
    `max_hits`/`window_cap` 省略時のモジュール既定）へフォールバックする——親から渡らない実行
    （単体テスト・手動起動）で退行しないため。
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
    """`handle()` の最終再シリアライズと同じ関数・同じ引数（`ensure_ascii=False`）で実バイト数を
    測る——ここでの保証がそのまま `content[].text` の最終出力の保証になる（引用符・バックスラッシュ
    の多い本文は素朴な文字数見積りだと再エスケープ分だけ実バイト数が膨らむため、常に直列化して測る）。
    """
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def _offset_arg(args: dict | None) -> int:
    """呼び出し引数の `offset`（`ripgrep_search` の `run_tool` 呼出に渡ったのと同じ生の値）を安全に
    int 化する——省略/不正値（負・数値でない）は 0（`agentic_search.run_tool` の grep 経路と同じ
    既定値・失敗時の扱い）。"""
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
    """`_clip_hits_field` で先頭ヒット1件すら丸ごとは残せない（0件のページなら収まるが `hits` は
    非空）ときの最終手段。先頭ヒットの本文（`text`）だけを UTF-8 バイト単位で縮めてでも、位置情報
    （`doc_id`/`line`/`span` 等・`text` 以外の全キー）付きの1件を残す——`hits=[]`・`next_offset`
    を進めないページを返すと、続きの呼び出しが前回と同一引数になり `_is_duplicate_tool_call` の
    重複拒否でその検索を先へ進められなくなる（`read_doc` の1行が長すぎる場合と同じ型の穴）。

    位置情報だけ（`text` を空にしても）`max_bytes` に収まらない場合は、成功ページを装わず
    `tool_result_budget_too_small` エラーを返す。エラー自体も収まらない極端な予算では `None`
    （呼び出し元の更なるフォールバックに委ねる）。
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
            item = first   # 文字列でも text 付き dict でもない＝縮められる本文が無い
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
    """`hits`（配列）を持つ結果（`ripgrep_search`/`es_search`）を、末尾のヒットから落として構造を
    保ったまま `max_bytes` に収める。`truncated=true` は常に付ける（呼び出し元はここに来た時点で
    元の直列化が `max_bytes` を超えている）。

    `next_offset` は `allow_next_offset`（＝ツール名が `ripgrep_search`）のときだけ付ける——
    `es_search` はページングを持たない（kNN の候補集合はページ間で固定できず offset を進める
    意味が無い・`_DESC_ES` に「候補の発見用・全件列挙は ripgrep_search/list_docs/原本読取」と
    案内済み）ため、`hits` を切っても `next_offset` は一切付けない。`ripgrep_search` では
    「呼び出し引数の `offset`」＋「今回残した件数」で必ず計算する——`hits` の最終ページ（ヒット数が
    ページ幅未満）は `agentic_search.py` 側が `next_offset` を付けないため、ここでの外側クリップが
    既存の `result["next_offset"]` から逆算すると基準を持てず巻き戻る（offset=20 の最終5件を2件に
    切ったときに正しい22ではなく2を返す）——`offset` は呼び出し元から明示的に渡してもらう。

    ヒット0件（envelope だけ）なら収まるが `hits` が非空（＝先頭1件すら丸ごとは残せない）場合は
    `_clip_hits_partial_first_hit` に委譲する——`hits=[]`・`next_offset` 据え置きのページは
    続きの呼び出しが同一引数になり重複拒否で進められなくなるため、成功ページとして返さない。

    envelope（`hits=[]`）自体も収まらなければ `None`（呼び出し元が他の手段にフォールバックする）。
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
        # allow_next_offset かつ k == n（hits 自体は削っていない）: 元の next_offset（無ければ無し）
        # をそのまま残す（`dict(result)` で既にコピー済み）。
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
        return None                      # envelope（0件ページ）すら収まらない
    if n == 0:
        return _build(0), True           # 元々0件＝縮める本文が無い正当な0件ページ
    return _clip_hits_partial_first_hit(result, max_bytes, offset=offset, allow_next_offset=allow_next_offset)


_PARTIAL_LINE_NOTE_TMPL = "先頭行が長すぎるため行の途中まで。以降は start_line={next_start} から"


def _clip_read_doc_partial_first_line(result: dict, max_bytes: int, first_line: str, start_line: int):
    """`_clip_read_doc_field` で1行も丸ごとは残せない（最初の1行だけでも `max_bytes` を超える）
    ときの最終手段。先頭行の本文をバイト単位で縮めてでも空文字を返さない——`text=""`・
    `end_line=start_line-1`（進捗ゼロ）のまま返すと、続きの呼び出しが前回と同じ引数になり
    `_is_duplicate_tool_call` の重複拒否でその文書を二度と読めなくなる。

    `end_line=start_line`（その行を消費済み扱いにする）にして進捗を保証する——行の途中からの
    再開位置（文字オフセット）は `read_doc` の引数に無いため、この行の残りは読めない
    （`partial_line=true`・`note` で明示。受容: 1行が極端に長い場合の残りは未読のまま扱う）。
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
    """`text`＋`end_line`（`read_doc` の形）を持つ結果を、行境界を保ったまま `max_bytes` に収める。
    `end_line`/`total_lines` を残し、`truncated=true` を付ける——続きは呼び出し元が
    `start_line=end_line+1` で読み直せる（`total_lines` は変えない＝「全何行中どこまで読めたか」の
    申告のまま）。1行も丸ごとは残せない場合は `_clip_read_doc_partial_first_line` に委譲する。

    `text`/`end_line` の両方を持たない結果には適用できない（`None`）。
    """
    text = result.get("text")
    end_line = result.get("end_line")
    if not isinstance(text, str) or not isinstance(end_line, int):
        return None
    lines = text.split("\n") if text else []
    n = len(lines)
    start_line = result.get("start_line")
    if not isinstance(start_line, int):
        start_line = end_line - n + 1   # 行数から逆算（start_line を持たない読取系向けの近似）

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


# 構造が分からない結果（`hits`/`end_line` のどちらの形にも合わない、または0件/0行まで削っても
# 収まらない極端なケース）向けの最終防衛線。続きの位置を保証できないことを明示する。
_CLIP_FALLBACK_NOTE = "結果が大きすぎるため先頭のみ。範囲を絞って再実行してください"


def _clip_tool_result(result, name: str | None = None, args: dict | None = None):
    """1件あたりのバイト予算を超えた結果を切り詰める（`SHERPA_MCP_TOOL_BUDGET_BYTES` があれば
    優先・無ければ `effective_tool_result_max_bytes` へフォールバック）。`agentic_search.run_tool`
    自身が直列化後の実バイト数で収まりを保証する仕組み（ripgrep_search/es_search の per-hit
    シュリンク・`_finish_reader_result` の二分探索）を持つため、ここに来るのは通常その保証が
    効かない極端なケースだけ——「構造を保ったまま続きを取れる」ことを保証する最終防衛線として、
    結果の形ごとに分岐する（`docs/proposals/2026-09-21-調査台帳を文脈の外に置く.md` §2「1件あたりの
    ツール結果バイト予算」）。

    `name`（呼び出したツール名）・`args`（そのツール呼出の生の引数）は `handle()` が渡す——
    `hits` 分岐の `next_offset` 付与可否（`ripgrep_search` だけ）と基準位置（呼び出し引数の
    `offset`）の判定に使う。省略時（単体テストでの直接呼出等）は `name=None`（`next_offset` を
    付けない）・`args=None`（`offset=0`）に倒す。

    1. `hits`（配列）を持つ結果（ripgrep_search/es_search）→ `_clip_hits_field`（末尾ヒットを落とし、
       ripgrep_search だけ `next_offset` を保つ）。
    2. `text`＋`end_line` を持つ結果（read_doc）→ `_clip_read_doc_field`（行境界で落とし `end_line`/
       `total_lines` を保つ・1行も残せない極端なケースは先頭行を部分的に残す）。
    3. どちらの形でもない、または0件/0行まで削っても収まらない → 直列化済み JSON 文字列を先頭から
       切った断片を `text` に残す（fail-open）。続きの位置は保証できないため `note` で明示する。

    戻り値は `(result_or_clipped, clipped: bool)`。
    """
    if not isinstance(result, dict):
        return result, False
    max_bytes = _env_int_override("SHERPA_MCP_TOOL_BUDGET_BYTES") \
        or agentic_search.effective_tool_result_max_bytes(provider="codex")
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

    # 二分探索: `raw` の UTF-8 バイト接頭辞長 `mid` を増減させ、包んだ最終形が `max_bytes` 以下に
    # なる最大の `mid` を探す（`mid` を増やすほど候補文字列は単調に長くなり、包んだバイト数も
    # 単調非減少＝二分探索が成立する）。`max_bytes` が極端に小さく空 text でも収まらない場合は
    # 空文字のまま返す（設定側の下限＝1KiB がこの状況を実運用では起こさない）。
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


# ---- 同一クエリの重複実行の抑止 ----
# 実機で同じ ripgrep_search が2回ずつ走り、同一結果を二重に文脈へ積んでいた（Codex が前回の結果を
# 見落として同条件で再実行する）ことへの対処。プロセス寿命（エージェント＝MCP 接続ごとに別プロセス・
# モジュール docstring の DEPTH-2 S3b 参照）で (ツール名, 引数の正規化 JSON) だけを覚え、結果本文は
# 一切保持しない（メモリ・漏洩の両面）。multi_agent の子エージェントは親とは別に覚える。
_DUPLICATE_CALL_CACHE_MAX = 64
_seen_tool_calls: "OrderedDict[tuple, None]" = OrderedDict()
# ask_user は専用分岐で既に処理済み（重複可＝毎回同じ確認文言を返す契約）。`_BUDGET_EXEMPT_TOOLS`
# （一覧のみの土台系）も対象外——list_docs/folder_tree を繰り返し呼ぶこと自体は実害が無い。
_DUPLICATE_CHECK_EXEMPT_TOOLS = frozenset({"ask_user"}) | _BUDGET_EXEMPT_TOOLS


def _is_duplicate_tool_call(name: str, args: dict) -> bool:
    """同一 `(name, 正規化した args)` の2回目以降の呼出なら True（初回はここで記録するだけ）。

    `json.dumps(args, sort_keys=True)` でキー順の違いを同一視する。件数上限
    （`_DUPLICATE_CALL_CACHE_MAX`）に達したら最も古いキーから捨てる（LRU）——無限に覚え続けて
    プロセスのメモリを圧迫しないため。
    """
    try:
        key = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
    except TypeError:
        return False   # 直列化できない引数は同一性を判定できない＝重複扱いしない（fail-open）
    if key in _seen_tool_calls:
        _seen_tool_calls.move_to_end(key)
        return True
    _seen_tool_calls[key] = None
    if len(_seen_tool_calls) > _DUPLICATE_CALL_CACHE_MAX:
        _seen_tool_calls.popitem(last=False)
    return False


def _ok(rid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid, code: int, msg: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}}


def handle(req: dict) -> dict | None:
    """JSON-RPC 1件を処理して応答 dict を返す。**通知（id 無し）は None＝応答しない**（MCP 準拠）。"""
    method = req.get("method")
    rid = req.get("id")
    is_notification = "id" not in req
    if method == "initialize":
        # protocolVersion はクライアント要求をそのまま返す（緩い交渉・未指定なら既定）。
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
            # plain は es_search・graph_neighbors・ask_user だけ——tools/list に出していなくても
            # 直接呼ばれたら存在しないツールと同じエラーで拒否する（fail-closed・多層防御）。
            err_body = {"error": f"unknown tool: {name}"}
            return _ok(rid, {"content": [{"type": "text", "text": json.dumps(err_body, ensure_ascii=False)}],
                             "isError": True})
        if name in _LEDGER_TOOLS:
            # 台帳の応答は探索量に計上せず、クリップ・重複拒否・読取サイドカーの対象外にする。
            result = _run_ledger_tool(name, args)
            return _ok(rid, {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                             "isError": bool(result.get("error"))})
        if name == "ask_user":
            # S2: ask_user はユーザーへ届ける「質問」＝検索ツールではない。実際の質問カード表示は
            # ラッパー（agents._run_authoring）が question イベントで行う。ここは Codex に「届いた・
            # 追加調査せず要約して終了せよ」を返すだけ（乱用ガード③: 2回目以降は別文言で調査続行を促す）。
            # RV HIGH: 確認ID 付き再送（tools/list で隠しても、プロンプト指示に反して呼ばれる場合の防御）は
            # 初回でも _ASK_RESULT_AGAIN＝質問カードを出さずに調査を打ち切らせない（ラッパーも無視するため）。
            if _ask_disabled():
                return _ok(rid, {"content": [{"type": "text", "text": _ASK_RESULT_AGAIN}], "isError": False})
            _ASK_STATE["count"] += 1
            if _ASK_STATE["count"] == 1:
                text = _ASK_RESULT_FIRST
                # DEPTH-2 S3b: 子（spawn_agent された worker/evaluator）の ask_user は親の `--json` に
                # 現れない（実機確認済み）——本サーバは呼び出し元が親か子か区別できないため、初回の
                # 質問は常にサイドカーへも書く（親が直接呼んだ通常実行では、親は同じ質問を自分の
                # `--json` item から既に見えている＝二重には効かない・provider.py 側が
                # `codex_question is None` の時だけサイドカー分を使う）。
                _q = agentic_search._question_from_args(args)
                if isinstance(_q, dict):
                    _sidecar_append({"kind": "ask_user", "ts": time.time(), "question": _q})
            else:
                text = _ASK_RESULT_AGAIN
            return _ok(rid, {"content": [{"type": "text", "text": text}], "isError": False})
        if name not in _DUPLICATE_CHECK_EXEMPT_TOOLS and _is_duplicate_tool_call(name, args):
            # 同一条件の再実行は run_tool を呼ばず本文も再送しない（実機で同じ ripgrep_search が
            # 2回ずつ走り同一結果を二重に文脈へ積んでいた事象への対処・module docstring 参照）。
            _sidecar_append({"kind": "limit", "field": "duplicate_tool_call", "ts": time.time()})
            err_body = {"error": "duplicate_tool_call",
                       "hint": "同じ条件の検索は既に実行済みです。条件を変えてください。"}
            return _ok(rid, {"content": [{"type": "text", "text": json.dumps(err_body, ensure_ascii=False)}],
                             "isError": True})
        try:
            # 調べる深さ連動込みの実効 hits/window（`SHERPA_MCP_TOOL_MAX_HITS`/`_WINDOW_CAP`・
            # 親＝`CodexProvider` が API 経路と同じ関数で解決した値）とバイト予算（既存）を
            # `run_tool()` 自身のクリップ処理へも渡す——`_clip_tool_result`（この後段）は最終形を
            # 保証する多層防御で、こちらは実行中の内部クリップ（read_doc の逐次クリップ等）を
            # 同じ実効値に揃えるためのもの。env 未設定/不正値は `None`＝`run_tool()` のモジュール
            # 既定へフォールバックする。
            result, _docs, _cites, _cards = agentic_search.run_tool(
                name, args, _world(), _scope(), layer=_layer(),
                max_hits=_env_int_override("SHERPA_MCP_TOOL_MAX_HITS"),
                window_cap=_env_int_override("SHERPA_MCP_TOOL_WINDOW_CAP"),
                tool_result_max_bytes=_env_int_override("SHERPA_MCP_TOOL_BUDGET_BYTES"),
                graph_only=(_toolset() == "plain"))
        except GraphSchemaEraError as e:
            # RV是正（rv-periphery #11）: `graph_neighbors`（`lens_service.neighbor_cards` 経由）が
            # 検知した旧世代グラフは、汎用の JSON-RPC プロトコルエラー（-32603・`serve()` の
            # broad except）に丸めず、通常のツール結果と同じ経路（`isError` あり・
            # `content[].text` に安定した機械可読コード）で返す——Codex 側の `item["result"]` に
            # そのまま載るため（JSON-RPC のプロトコルエラーは Codex 自身の item 表現が保証されて
            # いない）、`providers/codex/mcp.py::_graph_schema_era_from_item` が読み取って
            # `GraphSchemaEraError` を再構成できる。
            err_body = {"error": agentic_search.GRAPH_REINGEST_ERROR_CODE,
                        "world": e.world, "stored_era": e.stored_era}
            _sidecar_error_code(name, err_body)   # 子が受け取った障害も親が観測できるようにする
            return _ok(rid, {"content": [{"type": "text", "text": json.dumps(err_body, ensure_ascii=False)}],
                             "isError": True})
        is_error = bool(isinstance(result, dict) and result.get("error"))
        _sidecar_error_code(name, result)
        # `run_tool()` 自身が内部で行った打ち切り（1件あたりバイト予算の外側クリップとは別）を
        # サイドカーへ記録する——API 経路（`agentic_search._record_run_tool_limits`）と同じ判定キー・
        # 同じ語彙（`agentic_search._SEARCH_TRUNCATED_TOOLS`/`_BYTE_CLIP_TOOLS` をそのまま使う・
        # 車輪の再発明をしない）。`_tool_result_clipped_recorded` はこの呼び出し1回の中で
        # `tool_result_clipped` を二重に書かないためのフラグ（下の `_clip_tool_result` による
        # 外側クリップも同じ呼び出しで起き得るため、1回の呼び出しにつき最大1回だけ書く）。
        _tool_result_clipped_recorded = False
        if isinstance(result, dict):
            if name in agentic_search._SEARCH_TRUNCATED_TOOLS and result.get("truncated"):
                _sidecar_append({"kind": "limit", "field": "search_truncated", "ts": time.time()})
            if name in agentic_search._BYTE_CLIP_TOOLS and (result.get("text_truncated") or result.get("byte_clipped")):
                _sidecar_append({"kind": "limit", "field": "tool_result_clipped", "ts": time.time()})
                _tool_result_clipped_recorded = True
        if not is_error:
            # DEPTH-2 S3b: 子が読んだ doc_id をサイドカーへ（本文は書かない・失敗した呼出は数えない）。
            # `_sidecar_append` は `SHERPA_MCP_SIDECAR` 未設定なら no-op（既存の単一エージェント実行に
            # は影響しない）。
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
            # `_clip_tool_result`（`_clip_hits_partial_first_hit`）は位置情報だけでも予算に
            # 収まらない極端なケースで `result` を `{"error": ...}` へ置き換えることがある
            # （成功ページを装わない契約）——`is_error` はクリップ前の結果で確定済みのため、
            # クリップ後に error 形へ変わった分もここで同じ基準（`result.get("error")`）で拾い直す。
            is_error = is_error or bool(isinstance(result, dict) and result.get("error"))
        # MCP 標準＝content[].text。Codex が読む本文＝run_tool の結果（graph_neighbors は compact neighbors）。
        text = json.dumps(result, ensure_ascii=False)
        return _ok(rid, {"content": [{"type": "text", "text": text}], "isError": is_error})
    if is_notification:                       # notifications/initialized 等＝応答しない
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
            continue                          # 壊れた行は黙って捨てる（プロトコルを落とさない）
        try:
            resp = handle(req)
        except Exception as e:                # ツール例外でもサーバは落とさない（その応答だけ error）
            resp = _err(req.get("id"), -32603, f"{type(e).__name__}: {e}")
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            stdout.flush()


if __name__ == "__main__":
    serve()
