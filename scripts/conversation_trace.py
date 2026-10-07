"""会話の各ターンを、段・道具の呼び出し・トークン・調査台帳の流れとして 1 本のテキストに書き出す
（`make trace CONV=<番号>[,<番号>...] | SINCE=<日付> [UNTIL=<日付>] [FORMAT=jsonl] [MASK=1] [OUT=<ファイル>]`）。
設計: docs/design/usage.md「§8 利用者別・会話別の見え方」

守ること:
- 読み取りだけ。DB は SELECT だけ・ファイルは Codex のセッション記録を読むだけ。書くのは OUT の 1 ファイルだけ。
- 個人の資料を参照したターンは出さない。
- 秘匿ファイル（`text_kind.is_sensitive`）の名前は伏せ字の有無にかかわらず出さない。
- 期間（SINCE／UNTIL・日本時間の日付）で選ぶときは対象の会話に上限（`CONV_LIMIT`）を設け、超えたら出力の先頭と標準エラーに出す。
- `--format jsonl` は 1 行 1 ターンの機械で読める形（本文・結果の本文は入れない）。伏せ字と自己検査はテキストと同じ。
- 伏せ字（MASK=1）は、同じ値に同じ記号（実行ごとの塩で作る短いハッシュ）を振る。書き出す前に、入力から
  集めた伏せるべき値が出力に残っていないかを確かめ、残っていれば書き出さずに失敗する。
- 道具の結果の本文・シェルの出力・推論・回答の途中文は出さない（件数・大きさ・印だけ）。
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import hmac
import json
import os
import re
import shlex
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sherpa import answer_shape as _as  # noqa: E402
from sherpa import investigation_ledger as _il  # noqa: E402
from sherpa import investigation_record_render as _irr  # noqa: E402
from sherpa.depth_profile import CODEX_REASONING_LEVELS, DEPTH_PROFILES  # noqa: E402
from sherpa.ingest.text_kind import is_sensitive_doc_id  # noqa: E402
from sherpa.stop_kind import STOP_KINDS  # noqa: E402
from sherpa.store.feedback import MESSAGE_FEEDBACK_TAGS  # noqa: E402
from sherpa.turn_activity_format import _OTHER, _VERSION_UNSAFE, _kib, _n, label_agents  # noqa: E402

_TOKEN_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
_WINDOW_BEFORE_S = 1.0
_WINDOW_AFTER_S = 2.0
_CLIP = 120
CONV_LIMIT = 500
_JST = timezone(timedelta(hours=9))

_STAGE_DESC = {
    "main": "質問を受けて調べ、回答を作る最初の実行",
    "restart": "前の実行を続けられず、新しく始め直した実行",
    "continue": "途中経過で止まったので、続きを頼んだ実行（自動の続き）",
    "ledger": "調査台帳に未完了の項目が残っていたので、続きを頼んだ実行（台帳の続き）",
    "review": "台帳が完了した後、回答を確定する前の点検（見直しの一巡）",
    "unknown": "続きの実行（種類を判別できない）",
    "child": "本体から任された調べものをする別スレッド（下調べ役）",
    "api_round": "API の頭脳が「調べる→点検する」を繰り返す 1 巡",
}
_STAGE_NAME = {
    "main": "本体", "restart": "本体（やり直し）", "continue": "自動の続き", "ledger": "台帳の続き",
    "review": "見直し", "unknown": "続き", "child": "下調べ役", "api_round": "旧 API の巡",
}
_PROMPT_PREFIXES = (
    ("review", "回答を確定する前の点検"),
    ("ledger", "調査台帳"),
    ("ledger", "manifest.json が規約"),
    ("continue", "続けてください。"),
)
_TRACE_MARKERS = (("continue", re.compile(r"^cx-continue-\d+$")),
                  ("ledger", re.compile(r"^ledger-continue-\d+$")),
                  ("review", re.compile(r"^ledger-review-\d+$")))

# 引数キー → 表示の仕方（path=資料・term=語・text=本文・num=出してよい数値・item=台帳の項目・vocab:<語彙>）。
_ARG_KINDS = {
    "doc_id": "path", "path_prefix": "path", "left_doc_id": "path", "right_doc_id": "path",
    "source_doc_id": "path",
    "query": "term", "name": "term", "name_pattern": "term", "pattern": "term", "range": "term",
    "sheet": "term", "pages": "term", "target_generation": "term", "doctype": "term",
    "prompt": "text", "message": "text", "options": "text",
    "limit": "num", "offset": "num", "depth": "num", "line": "num", "window": "num", "start_line": "num",
    "max_bytes": "num", "max_cols": "num", "max_rows": "num", "count": "num", "start": "num",
    "table_row_start": "num", "table_start": "num",
    "state": "vocab:doc_state", "mode": "vocab:ask_mode", "agent_type": "vocab:role",
    "allow_free_text": "bool", "item": "item",
}
# 道具ごとに出してよい引数キー（ここに無いキーは名前ごと出さず件数だけにする）。
_TOOL_ARGS = {
    "list_docs": ("path_prefix", "name_pattern", "doctype", "state", "limit", "offset"),
    "folder_tree": ("path_prefix", "depth"),
    "ripgrep_search": ("query", "item", "offset"),
    "es_search": ("query", "item"),
    "glob_search": ("pattern",),
    "doc_outline": ("doc_id",),
    "read_doc": ("doc_id", "item", "start_line"),
    "read_around": ("doc_id", "item", "line", "window"),
    "compare_documents": ("left_doc_id", "right_doc_id", "source_doc_id", "target_generation"),
    "file_head": ("doc_id", "item", "max_bytes"),
    "xlsx_sheets": ("doc_id",),
    "xlsx_range": ("doc_id", "sheet", "range", "max_rows", "max_cols"),
    "docx_paragraphs": ("doc_id", "start", "count", "table_start", "table_row_start"),
    "pptx_slides": ("doc_id", "pages"),
    "pdf_pages": ("doc_id", "pages"),
    "graph_neighbors": ("name", "item"),
    "ask_user": ("mode", "allow_free_text", "prompt", "options"),
    "tool_search": ("query", "limit"),
    "web_search": ("query",),
    "spawn_agent": ("agent_type", "message"),
    "send_input": ("message",),
    "wait": (), "wait_agent": (), "close_agent": (), "resume_agent": (),
}
_CODEX_NATIVE_TOOLS = frozenset({"apply_patch", "view_image", "update_plan", "write_stdin",
                                 "list_mcp_resources", "read_mcp_resource", "request_user_input"})
_ERROR_CODES = frozenset({
    "context_window_exceeded", "usage_limit_exceeded", "server_overloaded", "internal_server_error",
    "unauthorized", "bad_request", "sandbox_error", "http_connection_failed",
    "response_stream_connection_failed", "response_stream_disconnected", "response_too_many_failed_attempts",
    "other", "ledger_unavailable", "ledger_item_invalid", "ledger_manifest_invalid", "ledger_review_invalid",
    "ledger_review_rejected", "ledger_write_failed", "duplicate_tool_call", "tool_result_budget_too_small",
    "read_invalid_args", "read_io_failed", "graph_unavailable", "graph_internal_error",
    "graph_reingest_required",
})
_LIMIT_KEYS = frozenset({
    "tool_result_clipped", "context_compactions", "search_truncated", "auto_continues", "duplicate_tool_call",
    "total_budget_hit", "synthesis_truncated", "depth_escalated", "backend_unavailable_fulltext",
    "backend_unavailable_graph", "graph_reingest_required", "tool_calls_exhausted", "ledger_incomplete",
    "wall_clock_hit",
})
# MASK=1 で出してよい閉じた語彙（ここに無い値は記号にして自己検査に入れる）。
_VOCAB = {
    "tool": frozenset(_TOOL_ARGS) | {"ledger_manifest_set", "ledger_item_put", "ledger_status",
                                     "ledger_review_put", "exec", "exec_command", "shell", "local_shell"}
    | _CODEX_NATIVE_TOOLS,
    "status": _il.NON_TERMINAL_STATUSES | _il.TERMINAL_STATUSES,
    "evidence": frozenset(_il.EVIDENCE_KINDS),
    "verdict": frozenset(_il.REVIEW_VERDICTS),
    "outcome": frozenset(_irr._COVERAGE_LABELS),
    "owner": frozenset(_irr._OWNER_LABELS),
    "question_kind": frozenset(_irr._QUESTION_KIND_LABELS),
    "stop_kind": frozenset(STOP_KINDS),
    "ledger_stop": frozenset({"cap", "complete", "ledger_missing", "no_progress"}),
    "reasoning": frozenset(CODEX_REASONING_LEVELS) | {"none", "max"},
    "depth": frozenset(DEPTH_PROFILES),
    "role": frozenset({"worker", "evaluator", "explorer", "default"}),
    "limits": _LIMIT_KEYS,
    "missing": frozenset({"not_found_in_scope", "unexplored", "insufficient", "conflict", "budget", "unreadable",
                          "source_missing", "spec_missing", "definition_missing", "log_missing",
                          "callgraph_missing"}),
    "api_verdict": frozenset({"sufficient", "insufficient", "blocked"}),
    "api_stop": frozenset({"ask_user", "budget", "failed", "rerun", "rounds_exhausted", "user_stop",
                           "sufficient"}),
    "provider": frozenset({"codex", "openai", "ollama", "gemini", "bedrock", "heuristic"}),
    "usage_kind": frozenset({"chat", "intent", "embed", "graph_ask", "vlm", "chat-sub", "chat-plan",
                             "usage_chat", "research", "chat-review", "chat-round", "rag_render"}),
    "node_status": frozenset({"done", "active", "error", "pending", "skipped"}),
    "doc_state": frozenset({"ready", "unreadable", "unknown"}),
    "ask_mode": frozenset({"choice", "confirm", "free", "single", "multi"}),
    "route_role": frozenset(_irr._ROLE_LABELS),
    "call_status": frozenset(_irr._CALL_STATUS_LABELS),
    "via": frozenset({"keyword", "vector", "keyword_only_search"}),
    "search_mode": frozenset({"hybrid", "keyword", "vector"}),
    "completion": frozenset(_as.COMPLETIONS),
    "lens": frozenset({"impact", "troubleshoot", "qa", "author", "investigate"}),
    "how_mode": frozenset(_as.HOW_MODES),
    "config": frozenset({"openai", "azure", "ollama"}),
    "codex_mode": frozenset({"standard", "plain"}),
    "tool_use_verdict": frozenset({"used", "unused", "undetermined"}),
    "feedback_rating": frozenset({"up", "down"}),
    "feedback_tag": frozenset(MESSAGE_FEEDBACK_TAGS),
    "error": _ERROR_CODES | {"TimeoutError", "ConnectionError", "OSError", "RuntimeError", "ValueError",
                             "KeyError", "TypeError", "PermissionError", "FileNotFoundError"},
}
_WORD_LABEL = {"tool": "道具", "error": "エラー", "provider": "接続先"}
_LOOSE_WORD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,63}$")
_OPENAI_PUBLIC_MODELS = frozenset({
    "gpt-3.5-turbo", "gpt-4", "gpt-4-turbo", "gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini",
    "gpt-4.1-nano", "gpt-4.5-preview", "gpt-5", "gpt-5-mini", "gpt-5-nano", "gpt-5-codex", "gpt-5.1",
    "gpt-5.1-codex", "gpt-5.1-codex-mini", "gpt-5.1-codex-max", "gpt-5.2", "gpt-5.2-codex", "gpt-5.3-codex",
    "gpt-5.4", "gpt-5.4-mini", "gpt-5.5", "gpt-5.6", "gpt-5.6-luna", "gpt-5.6-sol", "gpt-6-astra",
    "gpt-6-luna", "o1", "o1-mini", "o1-pro", "o3", "o3-mini", "o3-pro", "o4-mini", "codex-mini-latest",
    "gpt-oss-20b", "gpt-oss-120b", "text-embedding-3-small", "text-embedding-3-large",
    "text-embedding-ada-002",
    "gpt-4o-2024-05-13", "gpt-4o-2024-08-06", "gpt-4o-2024-11-20", "gpt-4o-mini-2024-07-18",
    "gpt-4-turbo-2024-04-09", "gpt-4.1-2025-04-14", "gpt-4.1-mini-2025-04-14", "gpt-4.1-nano-2025-04-14",
    "o1-2024-12-17", "o1-mini-2024-09-12", "o3-2025-04-16", "o3-mini-2025-01-31", "o4-mini-2025-04-16",
    "gpt-5-2025-08-07", "gpt-5-mini-2025-08-07", "gpt-5-nano-2025-08-07",
})
_OLLAMA_TAGS = frozenset({"latest", "0.5b", "1b", "1.5b", "2b", "3b", "4b", "7b", "8b", "9b", "12b", "13b",
                          "14b", "20b", "27b", "32b", "34b", "70b", "72b", "120b", "235b", "671b"})
# 打ち切りの印（結果の上の階層とヒットの中の両方で見る）。
_TRUNC_KEYS = (("truncated", "件数の上限"), ("next_offset", "続きあり"), ("text_truncated", "本文の切詰め"),
               ("byte_clipped", "大きさの切詰め"), ("clipped_bytes", "大きさの切詰め"),
               ("file_truncated", "ファイルの切詰め"), ("folders_truncated", "フォルダの切詰め"),
               ("row_truncated", "行の切詰め"), ("search_truncated", "検索の打ち切り"),
               ("truncated_docs", "探しきれない資料あり"))
_SETTING_KEYS = ("review_rounds", "schema_level", "multi_agent", "budget_per_result", "max_hits", "window_cap")
_TOOL_FLAG_LABELS = (("clipped", "切詰"), ("truncated", "打切"), ("errors", "失敗"), ("sandbox_errors", "サンド失敗"))
# 思考の流れのラベルとして出してよい固定の語（伏せ字のとき）。
_TRACE_LABELS = frozenset({
    "質問を理解", "意図を特定", "進め方を計画", "考える", "Codex が調べる", "下調べ役に任せる",
    "下調べ設定を確認してください", "検索方法を切替", "ユーザに確認", "続きを実行", "調査台帳を確認",
    "回答前の点検", "Web検索の制限", "（省略）",
    "資料の一覧を確認", "資料を検索（語句そのまま）", "資料を検索（全文/日本語）", "資料を検索（全文）",
    "資料を検索（grep）", "ファイル名で検索", "フォルダ構成を確認", "該当箇所を精読", "文書を通読",
    "見出し構造を確認", "関係グラフをたどる", "世代間の差分を比較", "原本のシート一覧を確認",
    "原本を読む（Excel）", "原本を読む（Word）", "原本を読む（PowerPoint）", "原本を読む（PDF）",
    "原本を読む（先頭）", "調査台帳に項目を登録", "調査台帳の項目を更新", "調査台帳の状態を確認",
    "コマンド実行", "ファイルを参照", "ファイルを検索（grep）", "ファイル一覧",
})
# 伏せ字でもそのまま付けてよい拡張子（小文字で比べる）。
_PUBLIC_EXTS = frozenset({
    ".md", ".txt", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml", ".html", ".htm", ".pdf", ".doc",
    ".docx", ".docm", ".xls", ".xlsx", ".xlsm", ".ppt", ".pptx", ".rtf", ".log", ".ini", ".cfg", ".conf",
    ".properties", ".sql", ".ddl", ".cbl", ".cob", ".cpy", ".jcl", ".java", ".js", ".ts", ".py", ".c",
    ".h", ".cpp", ".hpp", ".cs", ".vb", ".bas", ".sh", ".bat", ".ps1", ".go", ".rb", ".php", ".kt",
    ".pl", ".toml", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".vsd", ".vsdx", ".zip",
})
_SENSITIVE_STRIP = " \t\r\n\"'`()（）[]{}<>「」『』【】:;,.、。，！？!?"
_LIST_RESULT_KEYS = ("hits", "docs", "paths", "neighbors", "headings", "entries", "nodes", "children",
                     "results", "sheets", "paragraphs", "slides", "pages", "rows", "matches")
_READ_TOOLS = frozenset({"read_around", "read_doc", "file_head"})
_LEDGER_TOOLS = frozenset({"ledger_manifest_set", "ledger_item_put", "ledger_status", "ledger_review_put"})
_EXEC_TOOLS = frozenset({"exec", "exec_command", "shell", "local_shell"})
_GREP_PROGRAMS = frozenset({"rg", "grep", "egrep", "fgrep"})
_PUBLIC_PROGRAMS = frozenset({
    "rg", "grep", "egrep", "fgrep", "sed", "awk", "cat", "head", "tail", "ls", "find", "wc", "nl", "sort",
    "uniq", "cut", "tr", "file", "stat", "du", "git", "jq", "diff", "echo", "printf", "pwd", "cd", "test",
    "xargs", "iconv", "nkf", "od", "xxd", "strings", "bash", "sh", "zsh", "perl", "python", "python3",
    "node", "timeout", "env", "true", "mkdir", "cp", "mv", "rm", "tree", "realpath", "basename",
    "dirname", "date", "sha256sum", "md5sum", "cmp", "comm", "column", "tee", "touch", "readlink",
    "unzip", "zip", "tar", "gzip", "zcat", "pdftotext", "fd", "less", "more",
})
_PROG_RE = re.compile(r"^[A-Za-z0-9_.+-]{1,32}$")
_EXEC_CMD_RE = re.compile(r'"cmd"\s*:\s*("(?:[^"\\]|\\.)*")')
_PATHLIKE_RE = re.compile(r"[^\s\"'`()（）「」<>|;,]+")
_SELFCHECK_SPLIT = re.compile(r"[\s/\\、。，,（）()\[\]{}:=;\"'<>|「」『』・→←]+")
_PIECE_SPLIT = re.compile(r"[\s/\\]+")
_OLLAMA_PUBLIC = frozenset({"llama3", "llama3.1", "llama3.2", "llama3.3", "llama4", "qwen2.5", "qwen2.5-coder", "qwen3",
                  "qwen3-coder", "gemma2", "gemma3", "mistral", "mistral-nemo", "mixtral", "phi3", "phi4",
                  "deepseek-r1", "deepseek-coder-v2", "codellama", "command-r", "granite3.3", "nomic-embed-text",
                  "mxbai-embed-large", "bge-m3", "gpt-oss"})
_MIN_NEEDLE = 3
_ASCII_SUBSTR_MIN = 8


class LeakError(RuntimeError):
    """伏せるべき値が出力に残っていた。"""


# ---------------------------------------------------------------------------
# 伏せ字
# ---------------------------------------------------------------------------

def _clip(s: str, n: int = _CLIP) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


_POSITION_SUFFIX_RE = re.compile(
    r"(?::\d+(?:[-:]\d+)*|#L\d+(?:-L?\d+)?|[(（]\s*\d+(?:\s*[-~〜]\s*\d+)?\s*行?(?:目)?\s*[)）]?)$")


def _bare_name(value: str) -> str:
    """秘匿の判定・控えに使う形（前後の句読点・括弧・引用符と末尾の位置の注記 `:3`・`:3-9`・`#L3`・`(3行)` を
    外し、区切りを / にそろえる）。"""
    bare = value.replace("\\", "/").strip(_SENSITIVE_STRIP)
    while True:
        cut = _POSITION_SUFFIX_RE.sub("", bare).strip(_SENSITIVE_STRIP)
        if cut == bare:
            return bare.rstrip("/")
        bare = cut


def _sensitive(value: str) -> bool:
    try:
        bare = _bare_name(value)
        return bool(bare) and is_sensitive_doc_id(bare)
    except Exception:
        return True


def _public_model(value: str) -> bool:
    if value in _OPENAI_PUBLIC_MODELS:
        return True
    name, sep, tag = value.partition(":")
    return name in _OLLAMA_PUBLIC and (not sep or tag in _OLLAMA_TAGS)


def _known(kind: str, value: str) -> bool:
    return value in _VOCAB.get(kind, ())


_SOURCE_NAME_KEYS = frozenset({"doc", "doc_id", "doc_ids", "name", "path", "paths", "rel_path", "source_path",
                               "matched_doc_ids", "title", "url", "original_path", "md_path"})


def _answer_source_names(answer: dict) -> list[str]:
    """回答が出典として持ちうる資料名を集める（sources・sources_verified・codex_referenced_docs・
    personal_sources・created_files・codex_wrote_files と、data の中の資料名の欄をどの深さでも）。"""
    out: list[str] = []

    def walk(v, key=None, depth=0):
        if depth > 12:
            return
        if isinstance(v, dict):
            for k, vv in v.items():
                walk(vv, k, depth + 1)
        elif isinstance(v, list):
            for vv in v:
                walk(vv, key, depth + 1)
        elif isinstance(v, str) and (key is None or key in _SOURCE_NAME_KEYS):
            out.append(v)

    for key in ("sources", "sources_verified", "codex_referenced_docs", "personal_sources", "created_files",
                "codex_wrote_files"):
        walk(answer.get(key))
    walk(answer.get("data"), "data")
    return out


class Masker:
    """表示する値を伏せ字（MASK=1）または素の値へ変え、自己検査に使う「伏せるべき値」を集める。"""

    def __init__(self, mask: bool, salt: bytes | None = None, *, mask_models: bool = False):
        self.mask = mask
        self.mask_models = mask and mask_models
        self._salt = salt if salt is not None else os.urandom(16)
        self.needles: set[str] = set()
        self.vocab: set[str] = set()
        self._items: dict[str, str] = {}

    def _sym(self, label: str, value: str) -> str:
        h = hmac.new(self._salt, f"{label}\0{value}".encode("utf-8"), hashlib.sha256).hexdigest()[:8]
        return f"{label}#{h}"

    def note(self, value) -> None:
        """出さないが、出力に残っていないかを確かめる値として控える（伏せ字のときだけ）。"""
        if self.mask and isinstance(value, str) and value.strip():
            self.needles.add(value.strip())

    def _note_sensitive(self, value: str) -> None:
        bare = _bare_name(value)
        for v in (value.strip(), bare, Path(bare).name):
            if v:
                self.needles.add(v)

    def word(self, value, kind: str) -> str:
        """閉じた語彙（`_VOCAB[kind]`）。伏せ字では語彙に無い値を記号にする。"""
        if not isinstance(value, str) or not value:
            return "-"
        if _known(kind, value):
            self.vocab.add(value)
            return value
        if not self.mask:
            return value if _LOOSE_WORD.match(value) else _OTHER
        self.needles.add(value)
        return self._sym(_WORD_LABEL.get(kind, "値"), value)

    def model(self, value) -> str:
        """モデル名。伏せ字でもそのまま出す（コスト計算に使う）。mask_models のときだけ公開のモデル名
        （`_public_model`）以外を記号にし、自己検査に入れる。"""
        if not isinstance(value, str) or not value:
            return "-"
        if not self.mask_models or _public_model(value):
            shown = _clip(self.scrub(value), 80)
            self.vocab.add(value)
            return shown
        self.needles.add(value)
        return self._sym("モデル", value)

    def label(self, value) -> str:
        """思考の流れのラベル。伏せ字ではアプリの固定の語だけを出す。"""
        if not isinstance(value, str) or not value:
            return "-"
        if not self.mask:
            return _clip(self.scrub(value), 60)
        if value in _TRACE_LABELS:
            return value
        return self.text(value)

    def number(self, value) -> str:
        if not self.mask:
            return str(value)
        self.needles.add(str(value))
        return self._sym("数", str(value))

    def path(self, value) -> str:
        if not isinstance(value, str) or not value.strip():
            return "-"
        if _sensitive(value):
            self._note_sensitive(value)
            return "（秘匿ファイル）"
        if not self.mask:
            return _clip(value, 160)
        self.needles.add(value.strip())
        ext = Path(value.strip().rstrip("/")).suffix
        if not ext:
            return self._sym("資料", value.strip())
        if ext.lower() in _PUBLIC_EXTS:
            return self._sym("資料", value.strip()) + ext
        self.needles.add(ext)
        return self._sym("資料", value.strip()) + "." + self._sym("拡張子", ext)

    def term(self, value) -> str:
        if not isinstance(value, str):
            return "-"
        if not self.mask:
            return '"' + _clip(self.scrub(value), 80) + '"'
        self.needles.add(value.strip())
        return self._sym("語", value.strip())

    def text(self, value, *, lines: int = 0) -> str:
        """本文（質問・回答・理由・コマンドなど）。伏せ字では字数だけを添える。"""
        if not isinstance(value, str) or not value.strip():
            return "-"
        if self.mask:
            self.needles.add(value.strip())
            return f"{self._sym('文', value.strip())}（{len(value)} 字）"
        if lines:
            kept = [ln for ln in value.strip().splitlines() if ln.strip()][:lines]
            return " / ".join(_clip(self.scrub(ln), 160) for ln in kept)
        return _clip(self.scrub(value), 160)

    def scope(self, value) -> str:
        if not isinstance(value, str) or not value.strip():
            return "-"
        if not self.mask:
            return _clip(value, 80)
        self.needles.add(value.strip())
        return self._sym("範囲", value.strip())

    def user(self, value) -> str:
        if not isinstance(value, str) or not value:
            return "-"
        if not self.mask:
            return _clip(value, 60)
        self.needles.add(value)
        return self._sym("人", value)

    def item(self, value) -> str:
        if not isinstance(value, str) or not value:
            return "-"
        if not self.mask:
            return _clip(self.scrub(value), 60)
        self.needles.add(value)
        if value not in self._items:
            self._items[value] = f"項目{len(self._items) + 1}"
        return self._items[value]

    def scrub(self, text: str) -> str:
        """素の表示でも、秘匿ファイルに当たる語だけは伏せる。"""
        def _sub(m: re.Match) -> str:
            tok = m.group(0)
            if _sensitive(tok):
                self._note_sensitive(tok)
                return "（秘匿ファイル）"
            return tok
        return _PATHLIKE_RE.sub(_sub, text)

    def note_all(self, value) -> None:
        """出さない値（入れ子も）を自己検査の対象として控える。"""
        if isinstance(value, dict):
            for v in value.values():
                self.note_all(v)
        elif isinstance(value, list):
            for v in value:
                self.note_all(v)
        elif isinstance(value, str):
            if _sensitive(value):
                self._note_sensitive(value)
            self.note(value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            self.note(str(value))

    def arg(self, key: str, value) -> str:
        """既知の引数キー（`_ARG_KINDS`）の値を表示用に変える。"""
        kind = _ARG_KINDS.get(key, "term")
        if isinstance(value, list):
            shown = [self.arg(key, v) for v in value[:3]]
            self.note_all(value[3:])
            rest = f" ほか {len(value) - 3}" if len(value) > 3 else ""
            return "[" + ", ".join(shown) + rest + "]"
        if isinstance(value, dict):
            self.note_all(value)
            return "{…}"
        if isinstance(value, bool):
            return str(value)
        if isinstance(value, (int, float)):
            return str(value) if kind == "num" else self.number(value)
        if not isinstance(value, str):
            return "-"
        if _sensitive(value):
            self._note_sensitive(value)
            return "（秘匿ファイル）"
        if kind == "item":
            return self.item(value)
        if kind == "path":
            return self.path(value)
        if kind == "text":
            return self.text(value)
        if kind.startswith("vocab:"):
            return self.word(value, kind[6:])
        return self.term(value)


def _fixed_text() -> str:
    """このスクリプトと台帳の表示が使う固定の文言（自己検査の照合から外す）。"""
    parts: list[str] = []
    try:
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                parts.append(node.value)
    except (OSError, SyntaxError):
        pass
    for table in (_irr._STATUS_LABELS, _irr._EVIDENCE_KIND_LABELS, _irr._COVERAGE_LABELS,
                  _irr._REVIEW_VERDICT_LABELS, _irr._OWNER_LABELS, _irr._QUESTION_KIND_LABELS):
        parts.extend(table.keys())
        parts.extend(table.values())
    return "\n".join(parts)


def _needle_forms(value: str) -> set[str]:
    out: set[str] = set()
    v = value.strip()
    if v.isdigit():
        return {v} if len(v) >= 4 else out
    if len(v) < _MIN_NEEDLE or not re.search(r"[^\d\s.,:;/\\_-]", v):
        return out
    out.add(v)
    for line in v.splitlines():
        line = line.strip()
        if len(line) >= _MIN_NEEDLE:
            out.add(line)
        for piece in _PIECE_SPLIT.split(line):
            piece = piece.strip()
            if not re.search(r"[^\d.,:;_-]", piece or "0"):
                continue
            if (piece.isascii() and len(piece) >= _ASCII_SUBSTR_MIN) or \
                    (not piece.isascii() and len(piece) >= _MIN_NEEDLE):
                out.add(piece)
    return out


def find_leaks(text: str, masker: Masker) -> int:
    """出力に残った「伏せるべき値」の件数（値そのものは返さない）。"""
    fixed = _fixed_text()
    tokens = {t for t in _SELFCHECK_SPLIT.split(text) if t}
    joined = "\n".join(tokens)
    needles: set[str] = set()
    for raw in masker.needles:
        needles |= _needle_forms(raw)
    hits = 0
    for n in needles:
        if n in masker.vocab or n in fixed:
            continue
        if _SELFCHECK_SPLIT.search(n):
            pieces = [p for p in _SELFCHECK_SPLIT.split(n) if p]
            if pieces and all((p in tokens) if p.isascii() else (p in joined) for p in pieces) and n in text:
                hits += 1
            continue
        if n.isascii() and len(n) < _ASCII_SUBSTR_MIN:
            hits += n in tokens
        else:
            hits += n in joined
    return hits


# ---------------------------------------------------------------------------
# Codex のセッション記録（rollout JSONL）
# ---------------------------------------------------------------------------

def _epoch(ts) -> float | None:
    if isinstance(ts, datetime):
        return (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).timestamp()
    if not isinstance(ts, str) or not ts:
        return None
    try:
        s = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
        d = datetime.fromisoformat(s)
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return None


def _hms(epoch: float | None) -> str:
    return datetime.fromtimestamp(epoch).astimezone().strftime("%H:%M:%S") if epoch else "--:--:--"


def _ymdhms(epoch: float | None) -> str:
    return datetime.fromtimestamp(epoch).astimezone().strftime("%Y-%m-%d %H:%M:%S%z") if epoch else "-"


def _find_key(obj, keys: tuple, depth: int = 0):
    if depth > 4:
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "base_instructions":
                continue
            if k in keys and isinstance(v, str) and v:
                return v
            found = _find_key(v, keys, depth + 1)
            if found:
                return found
    return None


@dataclass
class Thread:
    path: Path
    tid: str
    parent_tid: str | None
    subagent: bool
    role: str | None
    model_provider: str | None = None
    bad_lines: int = 0
    events: list = field(default_factory=list)   # (epoch, record)


def read_threads(conv_dir: Path) -> tuple[list[Thread], int]:
    """会話ごとの CODEX_HOME 配下の全セッション記録を読む。戻り値は (スレッド, 読めなかったファイルの数)。
    読めない行は各スレッドの bad_lines に数える。"""
    threads: list[Thread] = []
    skipped = 0
    try:
        paths = sorted(conv_dir.glob("sessions/**/*.jsonl"))
    except OSError:
        return threads, 1
    for p in paths:
        try:
            with open(p, "rb") as f:
                meta = json.loads(f.readline().decode("utf-8"))
                payload = meta.get("payload") if isinstance(meta, dict) else None
                if not isinstance(payload, dict) or meta.get("type") != "session_meta":
                    skipped += 1
                    continue
                tid = payload.get("id")
                if not isinstance(tid, str) or not tid:
                    skipped += 1
                    continue
                th = Thread(path=p, tid=tid,
                            parent_tid=_find_key(payload, ("parent_thread_id",)),
                            subagent=payload.get("thread_source") == "subagent",
                            role=_find_key(payload, ("agent_role", "agent_type")),
                            model_provider=payload.get("model_provider")
                            if isinstance(payload.get("model_provider"), str) else None)
                for raw in f:
                    try:
                        line = raw.decode("utf-8").strip()
                    except UnicodeDecodeError:
                        th.bad_lines += 1
                        continue
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        th.bad_lines += 1
                        continue
                    ep = _epoch(rec.get("timestamp")) if isinstance(rec, dict) else None
                    if ep is None:
                        th.bad_lines += 1
                        continue
                    th.events.append((ep, rec))
        except (OSError, ValueError, AttributeError):
            skipped += 1
            continue
        threads.append(th)
    return threads, skipped


@dataclass
class Call:
    ts: float
    thread: Thread
    call_id: str | None
    name: str
    args: dict | None = None
    command: str | None = None
    end_ts: float | None = None
    duration_ms: int | None = None
    result_text: str | None = None
    result_bytes: int | None = None
    is_error: bool = False
    exit_code: int | None = None
    output_lines: int | None = None
    stage: str = ""


def _norm_tool(name: str) -> str:
    for pre in ("mcp__sherpa__", "sherpa__", "sherpa."):
        if name.startswith(pre):
            return name[len(pre):]
    return name


def _exec_command(payload: dict) -> str | None:
    raw = payload.get("input")
    if isinstance(raw, str):
        m = _EXEC_CMD_RE.search(raw)
        if m:
            try:
                return json.loads(m.group(1))
            except ValueError:
                return None
        return None
    args = payload.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return None
    if isinstance(args, dict):
        cmd = args.get("cmd") or args.get("command")
        if isinstance(cmd, list):
            return " ".join(str(c) for c in cmd)
        if isinstance(cmd, str):
            return cmd
    return None


def _output_blocks(output) -> list[str]:
    if isinstance(output, str):
        return [output]
    if isinstance(output, list):
        return [b.get("text") for b in output if isinstance(b, dict) and isinstance(b.get("text"), str)]
    return []


def _exec_result(output) -> tuple[int | None, int | None, int]:
    """シェルの出力から (終了コード, 出力の行数, 大きさ) を読む（本文はここでだけ読む）。"""
    blocks = _output_blocks(output)
    size = sum(len(b.encode("utf-8", errors="replace")) for b in blocks)
    exit_code, body = None, None
    for b in blocks:
        s = b.strip()
        if s.startswith("{"):
            try:
                parsed = json.loads(s)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                ec = parsed.get("exit_code")
                if isinstance(ec, int) and not isinstance(ec, bool):
                    exit_code = ec
                if isinstance(parsed.get("output"), str):
                    body = parsed["output"]
        if exit_code is None:
            m = re.search(r"(?im)^\s*exit code:\s*(-?\d+)\s*$", b)
            if m:
                exit_code = int(m.group(1))
                tail = b.split("Output:", 1)
                body = tail[1] if len(tail) == 2 else None
    lines = len([ln for ln in body.splitlines() if ln.strip()]) if isinstance(body, str) else None
    return exit_code, lines, size


def collect_calls(threads: list[Thread], start: float, end: float) -> list[Call]:
    """ターンの時間帯にある道具の呼び出しを、本体・下調べ役の区別なく時刻順に並べる。"""
    calls: list[Call] = []
    for th in threads:
        by_id: dict[str, Call] = {}
        for ep, rec in th.events:
            if ep < start or ep > end:
                continue
            payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
            ptype = payload.get("type")
            cid = payload.get("call_id") if isinstance(payload.get("call_id"), str) else None
            if rec.get("type") == "response_item":
                if ptype in ("function_call", "custom_tool_call") and isinstance(payload.get("name"), str):
                    name = _norm_tool(payload["name"])
                    call = Call(ts=ep, thread=th, call_id=cid, name=name)
                    if name in _EXEC_TOOLS:
                        call.command = _exec_command(payload)
                    else:
                        args = payload.get("arguments")
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except ValueError:
                                args = None
                        call.args = args if isinstance(args, dict) else None
                    calls.append(call)
                    if cid:
                        by_id[cid] = call
                elif ptype in ("function_call_output", "custom_tool_call_output") and cid in by_id:
                    call = by_id[cid]
                    call.end_ts = call.end_ts or ep
                    if call.name in _EXEC_TOOLS:
                        call.exit_code, call.output_lines, call.result_bytes = _exec_result(payload.get("output"))
                        call.is_error = call.exit_code not in (None, 0)
                    elif call.result_text is None:
                        call.result_bytes = sum(len(b.encode("utf-8", errors="replace"))
                                                for b in _output_blocks(payload.get("output")))
                elif ptype == "tool_search_call":
                    a = payload.get("arguments")
                    call = Call(ts=ep, thread=th, call_id=cid, name="tool_search",
                                args=a if isinstance(a, dict) else None)
                    calls.append(call)
                    if cid:
                        by_id[cid] = call
                elif ptype == "tool_search_output" and cid in by_id:
                    try:
                        by_id[cid].result_bytes = len(json.dumps(payload.get("tools"), ensure_ascii=False)
                                                      .encode("utf-8"))
                    except (TypeError, ValueError):
                        pass
                    by_id[cid].end_ts = ep
                elif ptype == "web_search_call":
                    action = payload.get("action") if isinstance(payload.get("action"), dict) else {}
                    q = action.get("query")
                    calls.append(Call(ts=ep, thread=th, call_id=cid, name="web_search",
                                      args={"query": q} if isinstance(q, str) else None))
            elif rec.get("type") == "event_msg" and ptype == "mcp_tool_call_end":
                inv = payload.get("invocation") if isinstance(payload.get("invocation"), dict) else {}
                dur = payload.get("duration") if isinstance(payload.get("duration"), dict) else {}
                try:
                    ms = int(dur.get("secs") or 0) * 1000 + int(dur.get("nanos") or 0) // 1_000_000
                except (TypeError, ValueError):
                    ms = None
                call = by_id.get(cid)
                if call is None:
                    call = Call(ts=ep - (ms or 0) / 1000, thread=th, call_id=cid,
                                name=_norm_tool(str(inv.get("tool") or "mcp")))
                    calls.append(call)
                    if cid:
                        by_id[cid] = call
                if isinstance(inv.get("tool"), str):
                    call.name = _norm_tool(inv["tool"])
                if isinstance(inv.get("arguments"), dict):
                    call.args = inv["arguments"]
                call.duration_ms = ms
                call.end_ts = ep
                result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
                ok = result.get("Ok")
                if not isinstance(ok, dict):
                    call.is_error = True
                    continue
                call.is_error = bool(ok.get("isError"))
                content = ok.get("content")
                if isinstance(content, list) and content and isinstance(content[0], dict) \
                        and isinstance(content[0].get("text"), str):
                    call.result_text = content[0]["text"]
                    call.result_bytes = len(call.result_text.encode("utf-8", errors="replace"))
    for call in calls:
        if call.duration_ms is None and call.end_ts is not None:
            call.duration_ms = max(0, int((call.end_ts - call.ts) * 1000))
    calls.sort(key=lambda c: c.ts)
    return calls


def _parsed_result(call: Call):
    if not isinstance(call.result_text, str):
        return None
    try:
        return json.loads(call.result_text)
    except ValueError:
        return None


def hit_count(call: Call) -> str:
    """ヒット件数。結果の形が分かる道具だけ数え、分からなければ「—」。"""
    if call.name in _EXEC_TOOLS:
        prog = _program(call.command)
        if prog in _GREP_PROGRAMS and call.output_lines is not None:
            return f"{call.output_lines}（出力行）"
        return "—"
    parsed = _parsed_result(call)
    if not isinstance(parsed, dict):
        return "—"
    if "error" in parsed:
        return "失敗"
    total = parsed.get("count") if isinstance(parsed.get("count"), int) else None
    for key in _LIST_RESULT_KEYS:
        if isinstance(parsed.get(key), list):
            n = len(parsed[key])
            return f"{n}（総数 {total}）" if total is not None and total != n else str(n)
    if call.name in _READ_TOOLS:
        s, e = parsed.get("start_line"), parsed.get("end_line")
        if isinstance(s, int) and isinstance(e, int):
            return f"{max(0, e - s + 1)} 行"
        return "1"
    if total is not None:
        return str(total)
    return "—"


def _trunc_marks(d: dict) -> list[str]:
    return [label for key, label in _TRUNC_KEYS if d.get(key) not in (None, False, 0, [], "")]


def truncation(call: Call) -> str:
    parsed = _parsed_result(call)
    if not isinstance(parsed, dict):
        return ""
    marks = _trunc_marks(parsed)
    for key in _LIST_RESULT_KEYS:
        for entry in parsed.get(key) if isinstance(parsed.get(key), list) else ():
            if isinstance(entry, dict):
                marks.extend(f"{m}（ヒット内）" for m in _trunc_marks(entry))
    if parsed.get("note") and "text" in parsed and "truncated" in parsed:
        marks.append("結果全体の切詰め")
    if isinstance(parsed.get("degrade_reason"), str):
        marks.append("縮退")
    return "・".join(dict.fromkeys(marks))


def _program(command: str | None) -> str | None:
    if not isinstance(command, str):
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
        words = words[1:]
    if len(words) >= 3 and Path(words[0]).name in ("bash", "sh", "zsh") and words[1] in ("-c", "-lc"):
        return _program(words[2])
    return Path(words[0]).name if words else None


# ---------------------------------------------------------------------------
# 段（本体・続き・見直し・下調べ役）
# ---------------------------------------------------------------------------

@dataclass
class Stage:
    kind: str
    label: str
    start: float
    end: float
    threads: set = field(default_factory=set)
    rounds: list = field(default_factory=list)   # (epoch, [in, cached, out, reasoning])
    tool_calls: int = 0
    compactions: int = 0
    role: str | None = None


def _prompt_kind(text) -> str | None:
    if not isinstance(text, str):
        return None
    for kind, prefix in _PROMPT_PREFIXES:
        if text.startswith(prefix):
            return kind
    return None


def _child_ids(threads: list[Thread]) -> set:
    ids = {t.tid for t in threads if t.subagent or t.parent_tid}
    roles: dict[str, str] = {}
    for th in threads:
        spawns: dict[str, str] = {}
        for _ep, rec in th.events:
            p = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
            if p.get("type") == "function_call" and p.get("name") == "spawn_agent":
                try:
                    a = json.loads(p.get("arguments") or "{}")
                except ValueError:
                    a = {}
                if isinstance(a, dict) and isinstance(a.get("agent_type"), str):
                    spawns[p.get("call_id")] = a["agent_type"]
            elif p.get("type") == "function_call_output" and p.get("call_id") in spawns:
                body = " ".join(_output_blocks(p.get("output")))
                for other in threads:
                    if other is not th and other.tid in body:
                        ids.add(other.tid)
                        roles.setdefault(other.tid, spawns[p["call_id"]])
    for th in threads:
        if th.tid in roles and not th.role:
            th.role = roles[th.tid]
    return ids


def build_stages(threads: list[Thread], start: float, end: float, masker: Masker) -> list[Stage]:
    """ターンの時間帯を段に分ける。本体は実行（task_started）ごと、下調べ役はスレッドごと。"""
    child_ids = _child_ids(threads)
    parents = [t for t in threads if t.tid not in child_ids]
    children = [t for t in threads if t.tid in child_ids]
    stages: list[Stage] = []
    segs: list[tuple[float, Thread, str | None]] = []
    for th in parents:
        current = None
        for ep, rec in th.events:
            if ep < start or ep > end:
                continue
            p = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
            if rec.get("type") == "event_msg" and p.get("type") == "task_started":
                current = [ep, th, None]
                segs.append(current)
            elif rec.get("type") == "event_msg" and p.get("type") == "user_message":
                if current is None:
                    current = [ep, th, None]
                    segs.append(current)
                if current[2] is None:
                    current[2] = _prompt_kind(p.get("message")) or "first"
            elif current is None:
                current = [ep, th, None]
                segs.append(current)
    segs.sort(key=lambda s: s[0])
    counts: dict[str, int] = {}
    for i, (ep, th, pk) in enumerate(segs):
        kind = pk if pk in ("continue", "ledger", "review") else ("main" if i == 0 else
                                                                   ("restart" if pk == "first" else "unknown"))
        counts[kind] = counts.get(kind, 0) + 1
        label = _STAGE_NAME[kind] + (str(counts[kind]) if kind in ("continue", "ledger", "review", "unknown") else "")
        nxt = segs[i + 1][0] if i + 1 < len(segs) else end
        last = max((e for e, _ in th.events if ep <= e < nxt), default=ep)
        stages.append(Stage(kind=kind, label=label, start=ep, end=last, threads={th.tid}))
    child_no = 0
    for th in sorted(children, key=lambda t: next((ep for ep, _ in t.events if start <= ep <= end), 1e18)):
        evs = [ep for ep, _ in th.events if start <= ep <= end]
        if not evs:
            continue
        child_no += 1
        role = masker.word(th.role, "role") if th.role else None
        stages.append(Stage(kind="child", label=f"下調べ役{child_no}" + (f"（{role}）" if role else ""),
                            start=min(evs), end=max(evs), threads={th.tid}, role=th.role))
    stages.sort(key=lambda s: s.start)
    return stages


def _stage_of(stages: list[Stage], thread: Thread, ep: float) -> Stage | None:
    for st in stages:
        if st.kind == "child" and thread.tid in st.threads:
            return st
    best = None
    for st in stages:
        if st.kind != "child" and st.start <= ep and (best is None or st.start >= best.start):
            best = st
    return best or next((s for s in stages if s.kind != "child"), None)


def collect_rounds(threads: list[Thread], stages: list[Stage], start: float, end: float) -> tuple[list, int]:
    """往復ごとのトークン（token_count）を時刻順に集める。戻り値は (往復の列, 値が壊れていて飛ばした往復の数)。"""
    rounds = []
    bad_rounds = 0
    for th in threads:
        for ep, rec in th.events:
            if ep < start or ep > end:
                continue
            if rec.get("type") == "compacted":
                st = _stage_of(stages, th, ep)
                if st is not None:
                    st.compactions += 1
                continue
            if rec.get("type") != "event_msg":
                continue
            p = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
            if p.get("type") != "token_count":
                continue
            info = p.get("info")
            if info is None:
                continue
            last = info.get("last_token_usage") if isinstance(info, dict) else None
            if not isinstance(last, dict):
                bad_rounds += 1
                continue
            row = [last.get(k) for k in _TOKEN_KEYS]
            if not all(isinstance(v, int) and not isinstance(v, bool) for v in row):
                bad_rounds += 1
                continue
            st = _stage_of(stages, th, ep)
            rounds.append((ep, st, row))
            if st is not None:
                st.rounds.append((ep, row))
    rounds.sort(key=lambda r: r[0])
    return rounds, bad_rounds


# ---------------------------------------------------------------------------
# 表示
# ---------------------------------------------------------------------------

def _dur(ms) -> str:
    if not isinstance(ms, (int, float)) or isinstance(ms, bool):
        return "-"
    return f"{ms / 1000:.1f}s" if ms < 10_000 else f"{ms / 1000:,.0f}s"


def _size(b) -> str:
    if not isinstance(b, int) or isinstance(b, bool):
        return "-"
    return f"{b}B" if b < 1024 else _kib(b)


def _tok(row) -> str:
    return (f"入力 {_n(row[0])}（キャッシュ {_n(row[1])}） 出力 {_n(row[2])} 推論 {_n(row[3])}")


def _sum_rows(rows) -> list:
    acc = [0, 0, 0, 0]
    for r in rows:
        for i in range(4):
            acc[i] += r[i]
    return acc


def _is_count(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _usage_row(d, bad: list | None = None) -> list | None:
    """トークン 4 種の行。欄が無ければ 0、数値でない値があれば行ごと読み飛ばして bad に数える。"""
    if not isinstance(d, dict):
        if d is not None and bad is not None:
            bad.append(1)
        return None
    vals = [d.get(k) for k in _TOKEN_KEYS]
    if all(v is None for v in vals):
        return None
    if any(v is not None and not _is_count(v) for v in vals):
        if bad is not None:
            bad.append(1)
        return None
    return [v or 0 for v in vals]


def _round_rows(rounds, bad: list | None = None) -> list:
    """活動記録の往復の並び（[入力, キャッシュ, 出力, 推論]）。形が壊れた往復は読み飛ばして bad に数える。"""
    out = []
    if rounds is not None and not isinstance(rounds, list) and bad is not None:
        bad.append(1)
    for r in rounds if isinstance(rounds, list) else []:
        if isinstance(r, list) and len(r) == 4 and all(_is_count(v) for v in r):
            out.append(r)
        elif bad is not None:
            bad.append(1)
    return out


def _args_summary(call: Call, masker: Masker) -> str:
    if call.name in _EXEC_TOOLS:
        if call.command is None:
            return "コマンド -"
        prog = _program(call.command)
        prog_out = prog if prog in _PUBLIC_PROGRAMS or (not masker.mask and prog and _PROG_RE.match(prog)) \
            else "（コマンド）"
        if prog_out == prog and prog:
            masker.vocab.add(prog)
        if masker.mask:
            return f"{prog_out} {masker.text(call.command)}"
        return f"{prog_out}: {masker.text(call.command)}"
    if call.name == "ledger_item_put" and isinstance(call.args, dict):
        a = call.args
        ev = a.get("evidence") if isinstance(a.get("evidence"), list) else []
        kinds: dict[str, int] = {}
        for e in ev:
            if isinstance(e, dict):
                k = masker.word(e.get("kind"), "evidence")
                kinds[k] = kinds.get(k, 0) + 1
                masker.note(e.get("path"))
        masker.note(a.get("subject"))
        masker.note(a.get("reason"))
        return (f"{masker.item(a.get('id'))}: 状態 {masker.word(a.get('status'), 'status')} 根拠 "
                + (" ".join(f"{k}×{v}" for k, v in kinds.items()) or "なし")
                + f" 担当 {masker.word(a.get('owner'), 'owner')}")
    if call.name == "ledger_manifest_set" and isinstance(call.args, dict):
        items = call.args.get("items") if isinstance(call.args.get("items"), list) else []
        ids = [masker.item(i) for i in items if isinstance(i, str)]
        return (f"種類 {masker.word(call.args.get('question_kind'), 'question_kind')} 目録 {len(items)} 件"
                + (f"（{', '.join(ids[:8])}{' ほか' if len(ids) > 8 else ''}）" if ids else ""))
    if call.name == "ledger_review_put" and isinstance(call.args, dict):
        a = call.args
        for k in ("purpose", "summary"):
            masker.note(a.get(k))
        for k in ("perspectives", "extra_perspectives"):
            for v in a.get(k) or []:
                masker.note(v if isinstance(v, str) else None)
        added = [r for r in a.get("added_items") or [] if isinstance(r, dict)]
        removed = [r for r in a.get("removed_items") or [] if isinstance(r, dict)]
        for r in added + removed:
            masker.note(r.get("id"))
            masker.note(r.get("reason"))
        return f"判断 {masker.word(a.get('verdict'), 'verdict')} 足した {len(added)} 外した {len(removed)}"
    if not isinstance(call.args, dict) or not call.args:
        return ""
    allowed = _TOOL_ARGS.get(call.name, ())
    parts, hidden = [], 0
    for k, v in call.args.items():
        if k not in allowed:
            hidden += 1
            masker.note_all(v)
            continue
        parts.append(f"{k}={masker.arg(k, v)}")
    if hidden:
        parts.append(f"他 {hidden} 項目")
    return " ".join(parts)


def _kv_known(d: dict, kind: str, masker: Masker, *, nonzero: bool = False) -> str:
    """語彙が既知のキーだけを key=値 で並べ、未知のキーは件数だけにする。"""
    parts, hidden = [], 0
    for k, v in sorted(d.items(), key=lambda kv: str(kv[0])):
        if not (isinstance(v, (int, bool)) and isinstance(k, str)) or (nonzero and not v):
            continue
        if _known(kind, k):
            parts.append(f"{k}={v}")
        else:
            hidden += 1
            masker.note(k)
    if hidden:
        parts.append(f"他 {hidden} 項目")
    return " ".join(parts)


def _result_summary(call: Call, masker: Masker) -> str:
    if call.name in _EXEC_TOOLS:
        ec = f"exit {call.exit_code}" if call.exit_code is not None else "exit -"
        return f"{ec} 出力 {_size(call.result_bytes)}"
    parsed = _parsed_result(call)
    if call.name == "ledger_status" and isinstance(parsed, dict):
        counts = parsed.get("counts") if isinstance(parsed.get("counts"), dict) else {}
        ok = "完了" if parsed.get("complete") else "未完了"
        nt = parsed.get("non_terminal")
        return f"{ok} 未終端 {len(nt) if isinstance(nt, list) else '-'} " + _kv_known(counts, "status", masker)
    if isinstance(parsed, dict) and "error" in parsed:
        code = parsed.get("error_code") or parsed.get("error")
        masker.note_all(parsed)
        return f"失敗（{masker.word(code, 'error')}） {_size(call.result_bytes)}"
    if call.is_error:
        return f"失敗 {_size(call.result_bytes)}"
    return f"結果 {_size(call.result_bytes)}"


def _turn_pairs(data: dict) -> list[dict]:
    """質問と回答を監査（chat.turn）で対にしたターンの並び。"""
    msgs = {m["id"]: m for m in data.get("messages") or []}
    by_user: dict[int, dict] = {}
    by_asst: dict[int, dict] = {}
    for a in data.get("audits") or []:
        d = a.get("detail") if isinstance(a.get("detail"), dict) else {}
        try:
            uid = int(d.get("message_id_user")) if d.get("message_id_user") is not None else None
            aid = int(d.get("message_id_assistant")) if d.get("message_id_assistant") is not None else None
        except (TypeError, ValueError):
            continue
        if uid is not None:
            by_user[uid] = a
        if aid is not None:
            by_asst[aid] = a
    turns = []
    for m in sorted(msgs.values(), key=lambda r: r["id"]):
        if m.get("role") != "user":
            continue
        audit = by_user.get(m["id"])
        d = (audit or {}).get("detail") or {}
        aid = d.get("message_id_assistant")
        try:
            aid = int(aid) if aid is not None else None
        except (TypeError, ValueError):
            aid = None
        turns.append({"user": m, "assistant": msgs.get(aid) if aid else None, "audit": audit})
    paired = {t["assistant"]["id"] for t in turns if t["assistant"]}
    for m in sorted(msgs.values(), key=lambda r: r["id"]):
        if m.get("role") == "assistant" and m["id"] not in paired:
            turns.append({"user": None, "assistant": m, "audit": by_asst.get(m["id"])})
    turns.sort(key=lambda t: (t["user"] or t["assistant"])["id"])
    return turns


def _personal(msg: dict | None) -> bool:
    if not msg:
        return False
    if msg.get("personal"):
        return True
    ans = msg.get("answer") if isinstance(msg.get("answer"), dict) else {}
    return bool(ans.get("personal_sources") or ans.get("_personal_facts") or ans.get("codex_wrote_files"))


def _brain(answer: dict, audit: dict | None, api_rounds: list, rollout_provider: str | None,
           masker: Masker) -> str:
    usage = answer.get("usage") if isinstance(answer.get("usage"), dict) else {}
    act = answer.get("activity") if isinstance(answer.get("activity"), dict) else {}
    st = act.get("settings") if isinstance(act.get("settings"), dict) else {}
    prov = usage.get("provider") or ((audit or {}).get("detail") or {}).get("provider")
    if prov == "codex" or act.get("source") == "codex_rollout":
        cfg = st.get("config") or rollout_provider
        conn = "Ollama" if (cfg == "ollama" or (cfg is None and usage.get("is_local"))) else \
            ("OpenAI・Azure" if cfg == "azure" else "OpenAI")
        mode = {"standard": "標準", "plain": "素"}.get(st.get("mode"), None)
        return f"Codex（{conn}{'・' + mode if mode else ''}）"
    if api_rounds:
        return f"旧 API の巡（{masker.word(prov, 'provider')}）"
    if prov in ("openai", "ollama"):
        return f"簡易または API（{prov}）"
    if prov == "heuristic":
        return "決定的（AI なし）"
    return masker.word(prov, "provider") if prov else "不明"


def _in_window(ts, start: float, end: float) -> bool:
    ep = _epoch(ts)
    return ep is not None and start <= ep <= end


def render_turn(no: int, turn: dict, data: dict, threads: list[Thread], masker: Masker) -> list[str]:
    u, a, audit = turn["user"], turn["assistant"], turn["audit"]
    cid = data["id"]
    if _personal(u) or _personal(a):
        return [f"--- ターン {no}  個人の資料を参照したため出しません"]
    answer = a.get("answer") if a and isinstance(a.get("answer"), dict) else {}
    gaps: list[str] = []
    tok_bad: list = []
    start = _epoch((u or {}).get("created_at")) if u else None
    end = _epoch(a.get("created_at")) if a else _epoch((audit or {}).get("created_at"))
    dur = answer.get("duration_ms") if isinstance(answer.get("duration_ms"), int) else None
    if start is None and end is not None and dur is not None:
        start = end - dur / 1000
    if start is None:
        start = end
    w_start = (start or 0) - _WINDOW_BEFORE_S
    w_end = end + _WINDOW_AFTER_S if end is not None else w_start - 1
    lines = [f"--- ターン {no}  質問 #{(u or {}).get('id', '-')}  回答 #{(a or {}).get('id', '-')}"]
    if u is None:
        gaps.append("監査に質問との対応付けが無い（質問を出せません）")
    if a is None:
        d = (audit or {}).get("detail") or {}
        why = ("監査の記録なし" if audit is None else "途中で止めた" if d.get("stopped")
               else "失敗" if audit.get("outcome") == "error" else "確認カードで終わった・または未保存")
        lines.append(f"回答なし（{why}）  開始 {_ymdhms(start)}  記録 {_ymdhms(end)}")
        if d.get("error"):
            lines.append(f"エラー: {masker.word(d.get('error'), 'error')}")
        gaps.append("回答が保存されていないため、段・道具・トークンの記録はセッション記録にある分だけ")
    act = answer.get("activity") if isinstance(answer.get("activity"), dict) else {}
    ph = act.get("phases_ms") if isinstance(act.get("phases_ms"), dict) else {}
    usage = answer.get("usage") if isinstance(answer.get("usage"), dict) else {}
    st = act.get("settings") if isinstance(act.get("settings"), dict) else {}
    usage_rows = [r for r in data.get("usage_events") or [] if _in_window(r.get("ts"), w_start, w_end)]
    api_rounds = [r for r in usage_rows if r.get("kind") == "chat-round"]
    if a is not None:
        phases = "／".join(f"{name} {_dur(ph.get(k))}" for k, name in
                          (("prepare", "準備"), ("agent", "Codex"), ("post", "後処理")) if k in ph)
        lines.append(f"開始 {_ymdhms(start)}  終了 {_ymdhms(end)}  所要 {_dur(dur)}"
                     + (f"（{phases}）" if phases else ""))
        inv = answer.get("investigation") if isinstance(answer.get("investigation"), dict) else {}
        stop_reason = inv.get("stopped_reason")
        lines.append(f"終了理由: {masker.word(answer.get('stop_kind'), 'stop_kind') if answer.get('stop_kind') else '不明'}"
                     f"  エラー: {masker.word(answer.get('codex_error_code'), 'error')}"
                     + (f"  台帳の打ち切り: {masker.word(stop_reason, 'ledger_stop')}" if stop_reason else ""))
        scope = answer.get("scope") if isinstance(answer.get("scope"), dict) else {}
        paths = scope.get("scope_paths") if isinstance(scope.get("scope_paths"), list) else []
        scope_txt = ", ".join(masker.scope(p) for p in paths[:3]) + (f" ほか {len(paths) - 3}" if len(paths) > 3 else "")
        lines.append(
            f"頭脳: {_brain(answer, audit, api_rounds, _rollout_provider(threads, w_start, w_end), masker)}"
            f"  モデル: {masker.model(usage.get('model') or st.get('model'))}"
            + ("（実際のモデル: 記録なし）" if st.get("config") == "azure" else "")
            + f"  推論: {masker.word(usage.get('reasoning') or st.get('reasoning'), 'reasoning')}"
            f"  深さ: {masker.word(usage.get('depth_profile') or st.get('depth') or scope.get('depth_profile'), 'depth')}"
            f"  範囲: {masker.scope(scope.get('world'))}" + (f"（{scope_txt}）" if paths else "（全体）"))
        knobs = " ".join(f"{k}={st[k]}" for k in _SETTING_KEYS if isinstance(st.get(k), (int, bool)))
        version = act.get("app_version")
        if knobs or isinstance(version, str):
            lines.append(f"設定: {knobs or '-'}  版 "
                         + (_VERSION_UNSAFE.sub("?", version)[:40] if isinstance(version, str) else "-"))
    if u is not None:
        lines.append(f"質問: {masker.text(u.get('content'), lines=5)}")
    if a is not None:
        source_names = _answer_source_names(answer)
        masker.note_all(source_names)
        if any(_sensitive(n) for n in source_names):
            masker.note(a.get("content"))
            lines.append("回答（先頭）: 秘匿資料を含むため本文は出さない")
        else:
            lines.append(f"回答（先頭）: {masker.text(a.get('content'), lines=5)}")
        for c in ((answer.get("data") or {}).get("claims") or []) if isinstance(answer.get("data"), dict) else []:
            if isinstance(c, dict):
                masker.note(c.get("text"))
                masker.note(c.get("reason"))

    turn_threads = [t for t in threads if any(w_start <= ep <= w_end for ep, _ in t.events)]
    bad = sum(t.bad_lines for t in turn_threads)
    if bad:
        gaps.append(f"セッション記録の壊れた行（文字コードが壊れている・JSON として読めない・時刻が無い）を {bad} 行読み飛ばした")
    if data.get("skipped_session_files"):
        gaps.append(f"読めないセッションファイルを {data['skipped_session_files']} 本飛ばした（どのターンの分かは不明）")
    is_codex = act.get("source") == "codex_rollout" or usage.get("provider") == "codex"
    stages = build_stages(turn_threads, w_start, w_end, masker) if turn_threads else []
    calls = collect_calls(turn_threads, w_start, w_end) if turn_threads else []
    rounds, bad_rounds = collect_rounds(turn_threads, stages, w_start, w_end) if turn_threads else ([], 0)
    if bad_rounds:
        gaps.append(f"壊れたトークンの記録（数値でない値）を {bad_rounds} 件読み飛ばした（合計に入っていない）")
    for c in calls:
        s = _stage_of(stages, c.thread, c.ts)
        c.stage = s.label if s else "-"
        if s:
            s.tool_calls += 1

    lines.append("[段]")
    if stages:
        for i, s in enumerate(stages, 1):
            tot = _sum_rows([r for _, r in s.rounds])
            lines.append(f"  段{i} {s.label}  {_hms(s.start)}–{_hms(s.end)}（{_dur((s.end - s.start) * 1000)}）"
                         f"  往復 {len(s.rounds)}  {_tok(tot)}  道具 {s.tool_calls} 回  圧縮 {s.compactions} 回")
            lines.append(f"      {_STAGE_DESC[s.kind]}")
        if any(s.kind == "child" and not s.role for s in stages):
            gaps.append("下調べ役の役割名（worker／evaluator）がセッション記録に無い")
        trace_kinds = _trace_marker_counts(a)
        seg_kinds = {k: sum(1 for s in stages if s.kind == k) for k in ("continue", "ledger", "review")}
        if a is not None and trace_kinds != seg_kinds and any(trace_kinds.values()):
            gaps.append("段の種類の判定が思考の流れの目印と合わない（目印: "
                        + " ".join(f"{_STAGE_NAME[k]} {v}" for k, v in trace_kinds.items()) + "）")
    elif api_rounds:
        for r in api_rounds:
            meta = r.get("meta") if isinstance(r.get("meta"), dict) else {}
            row = _usage_row(r, tok_bad)
            lines.append(f"  {_hms(_epoch(r.get('ts')))} {_STAGE_NAME['api_round']} {_n(meta.get('round'))}"
                         f"  判定 {masker.word(meta.get('verdict'), 'api_verdict')}  止め {masker.word(meta.get('stop'), 'api_stop')}"
                         f"  所要 {_dur(r.get('elapsed_ms'))}  {_tok(row) if row else 'トークン 記録なし'}")
            codes = meta.get("missing_codes") if isinstance(meta.get("missing_codes"), list) else []
            if codes:
                lines.append("      不足: " + " ".join(masker.word(c, "missing") for c in codes))
            lim = meta.get("limits") if isinstance(meta.get("limits"), dict) else {}
            if lim:
                lines.append("      打ち切り: " + _kv_known(lim, "limits", masker))
        lines.append(f"      {_STAGE_DESC['api_round']}")
    else:
        markers = _trace_marker_counts(a)
        if any(markers.values()):
            lines.append("  （時刻なし・思考の流れの目印から）" + " ".join(
                f"{_STAGE_NAME[k]} {v} 回" for k, v in markers.items() if v))
        else:
            lines.append("  記録なし")

    lines.append("[道具の呼び出し]（時刻順）")
    if calls:
        for c in calls:
            trunc = truncation(c)
            lines.append(f"  {_hms(c.ts)}  [{c.stage}]  {masker.word(c.name, 'tool')}  {_args_summary(c, masker)}"
                         f"  所要 {_dur(c.duration_ms)}  {_result_summary(c, masker)}  件数 {hit_count(c)}"
                         + (f"  打ち切り: {trunc}" if trunc else ""))
        lim = answer.get("limits") if isinstance(answer.get("limits"), dict) else {}
        if lim:
            lines.append("  打ち切り（ターン合計）: " + _kv_known(lim, "limits", masker, nonzero=True))
    else:
        trace = a.get("trace") if a and isinstance(a.get("trace"), list) else []
        tool_nodes = [n for n in trace if isinstance(n, dict) and n.get("kind") == "tool"]
        if tool_nodes:
            lines.append("  （時刻・所要・件数は記録なし。思考の流れの並びだけ）")
            for n in tool_nodes:
                lines.append(f"  -  {masker.label(n.get('label'))}  {masker.text(n.get('detail'))}"
                             f"  {masker.word(n.get('status'), 'node_status')}")
            if any(isinstance(n, dict) and n.get("label") == "（省略）" for n in trace):
                gaps.append("思考の流れは古い方から省略されている（全部は残っていない）")
        else:
            lines.append("  記録なし")
        if a is not None:
            gaps.append("道具ごとの時刻・引数・ヒット件数・打ち切り位置は記録なし（セッション記録が無い経路・または消えた）")

    lines.extend(_token_section(rounds, stages, answer, data.get("turn_metrics", {}).get((a or {}).get("id")),
                                usage_rows, act, gaps, masker, tok_bad))
    lines.extend(_tool_totals_section(act, stages, calls, masker, tok_bad))
    if tok_bad:
        gaps.append(f"数値でないトークンの値（DB・活動記録）を {len(tok_bad)} 件読み飛ばした（合計に入っていない）")
    lines.extend(_route_section(data.get("investigations", {}).get((a or {}).get("id")), masker, gaps))
    lines.extend(_ledger_section(cid, a, calls, data.get("investigations", {}).get((a or {}).get("id")),
                                 answer, masker, gaps))
    if is_codex and not turn_threads and a is not None:
        gaps.insert(0, "Codex のセッション記録が無い（保持期限で消えた・会話の削除・サンドボックス無効の実行は"
                       "残らない）。段の区切り・道具の時刻と引数・ヒット件数・往復ごとのトークン・台帳の書き換えの"
                       "流れは出せません")
    if not act and a is not None and is_codex:
        gaps.append("活動記録なし（この機能より前の版で保存された回答）")
    lines.append("[記録の欠け]")
    if gaps:
        lines.extend(f"  - {g}" for g in dict.fromkeys(gaps))
    else:
        lines.append("  なし")
    return lines


def _rollout_provider(threads: list[Thread], start: float, end: float) -> str | None:
    for th in threads:
        if not (th.subagent or th.parent_tid) and th.model_provider in ("openai", "ollama", "azure") \
                and any(start <= ep <= end for ep, _ in th.events):
            return th.model_provider
    return None


def _trace_marker_counts(a: dict | None) -> dict:
    out = {"continue": 0, "ledger": 0, "review": 0}
    trace = a.get("trace") if a and isinstance(a.get("trace"), list) else []
    for n in trace:
        nid = n.get("id") if isinstance(n, dict) else None
        if isinstance(nid, str):
            for kind, rx in _TRACE_MARKERS:
                if rx.match(nid):
                    out[kind] += 1
    return out


def _token_section(rounds, stages, answer, tm, usage_rows, act, gaps, masker: Masker,
                   tok_bad: list) -> list[str]:
    lines = ["[トークン]"]
    usage = answer.get("usage") if isinstance(answer.get("usage"), dict) else {}
    db_total = _usage_row(usage, tok_bad)
    if rounds:
        lines.append("  往復ごと（増分 → 累計入力）:")
        cum = [0, 0, 0, 0]
        for ep, st, row in rounds:
            cum = [cum[i] + row[i] for i in range(4)]
            lines.append(f"  {_hms(ep)}  [{st.label if st else '-'}]  +{_tok(row)}  → 累計入力 {_n(cum[0])}")
        child_threads = {t for s in stages if s.kind == "child" for t in s.threads}
        parent_sum = _sum_rows([r for s in stages if s.kind != "child" for _, r in s.rounds])
        child_sum = _sum_rows([r for s in stages if s.kind == "child" for _, r in s.rounds])
        total = _sum_rows([r for _, _, r in rounds])
        lines.append(f"  合計（セッション記録） {_tok(total)}"
                     + (f"（本体 入力 {_n(parent_sum[0])}／下調べ役 入力 {_n(child_sum[0])}）" if child_threads else ""))
        bd = usage.get("codex_usage_breakdown") if isinstance(usage.get("codex_usage_breakdown"), dict) else {}
        for label, rec_row, db_row in (("合計", total, db_total), ("本体", parent_sum, _usage_row(bd.get("parent"), tok_bad)),
                                       ("下調べ役", child_sum, _usage_row(bd.get("children"), tok_bad))):
            if db_row is not None and db_row != rec_row:
                gaps.append(f"トークンの{label}がセッション記録と DB（usage）で食い違う（記録 入力 {_n(rec_row[0])}"
                            f"／DB 入力 {_n(db_row[0])}）")
    else:
        agents = act.get("agents") if isinstance(act.get("agents"), list) else []
        if agents:
            lines.append("  （時刻なし・活動記録から）")
            for label, agent in label_agents(agents):
                rs = _round_rows(agent.get("rounds"))
                tok = _usage_row(agent.get("tokens"))
                lines.append(f"  [{label} {masker.model(agent.get('model'))}] "
                             f"{_tok(tok) if tok else 'トークン 記録なし'} 往復 {len(rs)}")
                for i, r in enumerate(rs[:200], 1):
                    lines.append(f"    往復 {i}: +{_tok(r)}")
            gaps.append("往復ごとの時刻と段の区切りは記録なし（活動記録は本体・下調べ役ごとの往復の並びだけ）")
    if db_total is not None:
        lines.append(f"  DB（回答の usage） {_tok(db_total)}")
    if isinstance(tm, dict):
        tm_row = _usage_row(tm, tok_bad)
        lines.append(f"  DB（turn_metrics） {_tok(tm_row) if tm_row else '-'}")
        if tm_row is not None and db_total is not None and tm_row != db_total:
            gaps.append("turn_metrics と回答の usage のトークンが食い違う")
    aux = [r for r in usage_rows if r.get("kind") != "chat-round"]
    if aux:
        lines.append("  補助の AI 呼び出し（usage_events）:")
        for r in aux:
            row = _usage_row(r, tok_bad)
            lines.append(f"  {_hms(_epoch(r.get('ts')))}  {masker.word(r.get('kind'), 'usage_kind')}"
                         f"  所要 {_dur(r.get('elapsed_ms'))}  {_tok(row) if row else 'トークン 記録なし'}")
    if len(lines) == 1:
        lines.append("  記録なし")
    return lines


def _tool_line(name: str, t: dict, masker: Masker) -> str:
    ms = t.get("ms")
    flags = " ".join(f"{word}{t[k]}" for k, word in _TOOL_FLAG_LABELS
                     if isinstance(t.get(k), int) and not isinstance(t.get(k), bool) and t[k])
    return (f"    {masker.word(name, 'tool')} {_n(t.get('calls'))} 回 {_size(t.get('bytes'))}"
            f" 最大 {_size(t.get('max_bytes'))}"
            + (f" 計 {_dur(ms)}" if isinstance(ms, int) and not isinstance(ms, bool) else "")
            + (f" {flags}" if flags else ""))


def _tool_totals_section(act: dict, stages: list[Stage], calls: list[Call], masker: Masker,
                         tok_bad: list) -> list[str]:
    """本体と下調べ役ごとのトークン・往復・圧縮と、道具ごとの回数・大きさ・時間の合計。"""
    agents = act.get("agents") if isinstance(act.get("agents"), list) else []
    if agents:
        lines = ["[道具ごとの集計]（活動記録から）"]
        for label, agent in label_agents(agents):
            tok = _usage_row(agent.get("tokens"), tok_bad)
            rs = _round_rows(agent.get("rounds"), tok_bad)
            comps = [c for c in agent.get("compactions") or [] if isinstance(c, int)]
            lines.append(f"  {label}  モデル {masker.model(agent.get('model'))}"
                         f"  {_tok(tok) if tok else 'トークン 記録なし'}  往復 {len(rs)}"
                         f"  最大入力 {_n(max((r[0] for r in rs), default=None))}  圧縮 {len(comps)} 回"
                         + (f" @{','.join(str(c) for c in comps[:8])}" if comps else ""))
            tools = agent.get("tools") if isinstance(agent.get("tools"), dict) else {}
            ranked = sorted(((n, t) for n, t in tools.items() if isinstance(n, str) and isinstance(t, dict)),
                            key=lambda kv: -(kv[1].get("bytes") if isinstance(kv[1].get("bytes"), int) else 0))
            lines.extend(_tool_line(n, t, masker) for n, t in ranked)
            unparsed = agent.get("unparsed") if isinstance(agent.get("unparsed"), dict) else {}
            n_unparsed = sum(v for v in unparsed.values() if isinstance(v, int) and not isinstance(v, bool))
            if n_unparsed:
                lines.append(f"    読めなかった記録 {n_unparsed} 件")
        return lines
    if not stages:
        return []
    lines = ["[道具ごとの集計]（セッション記録から）"]
    groups: list[tuple[str, list[Stage]]] = [("本体", [s for s in stages if s.kind != "child"])]
    groups += [(s.label, [s]) for s in stages if s.kind == "child"]
    for label, group in groups:
        if not group:
            continue
        rows = [r for s in group for _, r in s.rounds]
        lines.append(f"  {label}  {_tok(_sum_rows(rows))}  往復 {len(rows)}"
                     f"  最大入力 {_n(max((r[0] for r in rows), default=None))}"
                     f"  圧縮 {sum(s.compactions for s in group)} 回")
        labels = {s.label for s in group}
        totals: dict[str, dict] = {}
        for c in calls:
            if c.stage not in labels:
                continue
            t = totals.setdefault(c.name, {"calls": 0, "bytes": 0, "max_bytes": 0, "ms": 0,
                                           "truncated": 0, "errors": 0})
            t["calls"] += 1
            t["bytes"] += c.result_bytes or 0
            t["max_bytes"] = max(t["max_bytes"], c.result_bytes or 0)
            t["ms"] += c.duration_ms or 0
            t["truncated"] += 1 if truncation(c) else 0
            t["errors"] += 1 if c.is_error else 0
        lines.extend(_tool_line(n, t, masker)
                     for n, t in sorted(totals.items(), key=lambda kv: -kv[1]["bytes"]))
    return lines


# ---------------------------------------------------------------------------
# 調べた経路と見つかった資料（investigation_records の detail.calls）
# ---------------------------------------------------------------------------

_LINE_RANGE = re.compile(r"^\d{1,9}(-\d{1,9})?$")


def _jterm(masker: Masker, value):
    """検索語など。伏せ字では記号・素では秘匿ファイルの名前だけ伏せた全文。"""
    if not isinstance(value, str) or not value.strip():
        return None
    return masker.term(value) if masker.mask else masker.scrub(value)


def _jpath(masker: Masker, value):
    """資料のパス。伏せ字では記号・秘匿ファイルは伏せ・それ以外の素はそのまま。"""
    if not isinstance(value, str) or not value.strip():
        return None
    return masker.path(value) if (masker.mask or _sensitive(value)) else value


def _jrange(masker: Masker, value):
    """行の範囲は数字のままで出し、シート名などを含みうる範囲は検索語と同じ扱い。"""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and _LINE_RANGE.match(value.strip()):
        return value.strip()
    return _jterm(masker, value)


def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def calls_view(calls, masker: Masker) -> dict | None:
    """detail.calls を、伏せ字と秘匿の規則を通した出力用の形にする。順位・点数・当たり方・行の検索の形は欄があるときだけ。"""
    if not isinstance(calls, dict):
        return None
    route = []
    for r in calls.get("route") or []:
        if not isinstance(r, dict):
            continue
        out = {"role": masker.word(r.get("role"), "route_role"), "tool": masker.word(r.get("tool"), "tool"),
               "status": masker.word(r.get("status"), "call_status"), "count": _num(r.get("count")),
               "ms": _num(r.get("ms"))}
        if r.get("error"):
            out["error"] = masker.word(r.get("error"), "error")
        if r.get("query") not in (None, ""):
            out["query"] = _jterm(masker, r["query"] if isinstance(r["query"], str) else str(r["query"]))
        if r.get("range") not in (None, ""):
            out["range"] = _jrange(masker, r["range"])
        if r.get("item") not in (None, ""):
            out["item"] = masker.item(r["item"] if isinstance(r["item"], str) else str(r["item"]))
        if r.get("mode") not in (None, ""):
            out["mode"] = masker.word(r["mode"], "search_mode")
        docs = []
        for d in r.get("docs") or []:
            if not (isinstance(d, dict) and isinstance(d.get("doc"), str)):
                continue
            row = {"doc": _jpath(masker, d["doc"])}
            if d.get("range") not in (None, ""):
                row["range"] = _jrange(masker, d["range"])
            if _num(d.get("rank")) is not None:
                row["rank"] = d["rank"]
            if _num(d.get("score")) is not None:
                row["score"] = d["score"]
            if d.get("via") not in (None, ""):
                row["via"] = masker.word(d["via"], "via")
            docs.append(row)
        if docs:
            out["docs"] = docs
        for k in ("docs_omitted", "docs_hidden"):
            if _is_count(r.get(k)) and r[k] > 0:
                out[k] = r[k]
        route.append({k: v for k, v in out.items() if v is not None})
    found = []
    for r in calls.get("found") or []:
        if not (isinstance(r, dict) and isinstance(r.get("doc"), str)):
            continue
        found.append({"doc": _jpath(masker, r["doc"]), "hits": _num(r.get("hits")),
                      "lines": [x for x in r.get("lines") or [] if _is_count(x)],
                      "queries": [t for t in (_jterm(masker, q) for q in r.get("queries") or []) if t],
                      "opened": r.get("opened") is True})
    view = {"calls": _num(calls.get("calls")), "missing": _num(calls.get("missing")) or 0,
            "route": route, "found": found}
    for k in ("route_omitted", "found_more", "found_hidden", "found_hits_omitted"):
        if _is_count(calls.get(k)) and calls[k] > 0:
            view[k] = calls[k]
    if calls.get("opened_unknown"):
        view["opened_unknown"] = True
    return view


def _route_section(record, masker: Masker, gaps: list) -> list[str]:
    """[調べた経路と見つかった資料]（台帳の有無によらず、記録があれば出す）。"""
    detail = record.get("detail") if isinstance(record, dict) and isinstance(record.get("detail"), dict) else {}
    view = calls_view(detail.get("calls"), masker)
    if view is None:
        return []
    lines = ["[調べた経路と見つかった資料]（investigation_records から）",
             f"  道具の呼び出し {view['calls'] if view['calls'] is not None else '-'} 回"
             + (f"（記録の欠け {view['missing']} 行）" if view["missing"] else "")]
    for r in view["route"]:
        docs = []
        for d in r.get("docs") or []:
            extra = [f"順位 {d['rank']}" if "rank" in d else "", f"点数 {d['score']}" if "score" in d else "",
                     d.get("via", "")]
            docs.append(f"{d['doc']}" + (f":{d['range']}" if "range" in d else "")
                        + (f"（{' '.join(x for x in extra if x)}）" if any(extra) else ""))
        lines.append(f"  [{r['role']}]  {r['tool']}"
                     + (f"  語 {r['query']}" if "query" in r else "") + (f"  範囲 {r['range']}" if "range" in r else "")
                     + (f"  項目 {r['item']}" if "item" in r else "") + (f"  形 {r['mode']}" if "mode" in r else "")
                     + f"  件数 {r.get('count', '-')}  {r['status']}" + (f"（{r['error']}）" if "error" in r else "")
                     + f"  所要 {_dur(r.get('ms'))}"
                     + (f"  資料 {'; '.join(docs)}" if docs else "")
                     + (f"（ほか {r['docs_omitted']} 件は上限で省略）" if "docs_omitted" in r else "")
                     + (f"（名前を出せない資料 {r['docs_hidden']} 件）" if "docs_hidden" in r else ""))
    if view.get("route_omitted"):
        lines.append(f"  ほか {view['route_omitted']} 回の呼び出しは上限のため記録されていない")
    lines.append("  見つかった資料（検索の結果）:")
    for f in view["found"] or [None]:
        if f is None:
            lines.append("    なし")
            break
        lines.append(f"    {f['doc']}  ヒット {f['hits'] if f['hits'] is not None else '-'}"
                     + (f"  行 {','.join(str(x) for x in f['lines'])}" if f["lines"] else "")
                     + (f"  語 {' '.join(f['queries'])}" if f["queries"] else "")
                     + f"  Codex の読み取り記録 {'あり' if f['opened'] else '確かめられない' if view.get('opened_unknown') else 'なし'}")
    for k, word in (("found_more", "上限で省略した見つかった資料"), ("found_hidden", "名前を出せない資料"),
                    ("found_hits_omitted", "呼び出しの上限で取り込めなかったヒット")):
        if view.get(k):
            lines.append(f"    ほか {word} {view[k]} 件")
    if view["missing"]:
        gaps.append(f"道具の記録の欠けが {view['missing']} 行ある（経路に出ていない呼び出しがありうる）")
    if record.get("truncated"):
        note = _irr.describe_dropped(detail.get("dropped"))
        if note:
            gaps.append(note)
    return lines


# ---------------------------------------------------------------------------
# JSONL（1 行 1 ターン）
# ---------------------------------------------------------------------------

def _iso(ep: float | None) -> str | None:
    return datetime.fromtimestamp(ep, _JST).isoformat(timespec="seconds") if ep is not None else None


def _note_conversation(data: dict, masker: Masker) -> None:
    """会話の題・利用者・資料フォルダの値を、伏せ残しの確認の対象に控える。"""
    conv = data.get("conversation") or {}
    masker.note(conv.get("title"))
    for k in ("email", "display_name"):
        masker.note((data.get("user") or {}).get(k))
    for k in ("root_path", "label", "world_id"):
        masker.note((data.get("world") or {}).get(k))


def turn_row(no: int, turn: dict, data: dict, masker: Masker) -> dict | None:
    """assistant の返答 1 件ぶんの行（回答・質問・道具の結果の本文は入れない）。返答が無い・個人の資料を参照したターンは None。"""
    u, a, audit = turn["user"], turn["assistant"], turn["audit"]
    if a is None or _personal(u) or _personal(a):
        return None
    if isinstance(a.get("answer"), dict) and a["answer"].get("question"):  # 確認カード（回答ではない）は出さない
        return None
    conv = data.get("conversation") or {}
    answer = a.get("answer") if isinstance(a.get("answer"), dict) else {}
    masker.note_all(_answer_source_names(answer))
    usage = answer.get("usage") if isinstance(answer.get("usage"), dict) else {}
    act = answer.get("activity") if isinstance(answer.get("activity"), dict) else {}
    st = act.get("settings") if isinstance(act.get("settings"), dict) else {}
    how = _as.how_of(answer)
    lens = answer.get("lens") or (answer.get("route") or {}).get("lens") or a.get("lens")
    prov = usage.get("provider") or ((audit or {}).get("detail") or {}).get("provider")
    inv = answer.get("investigation") if isinstance(answer.get("investigation"), dict) else {}
    record = (data.get("investigations") or {}).get(a["id"])
    detail = record.get("detail") if isinstance(record, dict) and isinstance(record.get("detail"), dict) else {}
    view = calls_view(detail.get("calls"), masker) or {}
    tokens = _usage_row(usage)
    row: dict = {
        "kind": "turn", "conv": data["id"], "turn": no, "answer_id": a["id"],
        "at": _iso(_epoch(a.get("created_at"))),
        "user": masker.user(conv.get("user_id")), "scope": masker.scope(conv.get("version")),
        "how": ({"mode": masker.word(how["mode"], "how_mode"), "doc_focus": how["doc_focus"]} if how else None),
        "lens": masker.word(lens, "lens") if isinstance(lens, str) and lens else None,
        "ai": {"provider": masker.word(prov, "provider") if prov else None,
               "model": masker.model(usage.get("model") or st.get("model")) if (usage.get("model") or st.get("model")) else None,
               "config": masker.word(st.get("config"), "config") if st.get("config") else None,
               "mode": masker.word(st.get("mode"), "codex_mode") if st.get("mode") else None,
               "depth": masker.word(usage.get("depth_profile") or st.get("depth"), "depth")
               if (usage.get("depth_profile") or st.get("depth")) else None,
               "reasoning": masker.word(usage.get("reasoning") or st.get("reasoning"), "reasoning")
               if (usage.get("reasoning") or st.get("reasoning")) else None},
        "completion": masker.word(answer.get("completion"), "completion") if answer.get("completion") else None,
        "stop_kind": masker.word(answer.get("stop_kind"), "stop_kind") if answer.get("stop_kind") else None,
        "duration_ms": answer.get("duration_ms") if _is_count(answer.get("duration_ms")) else None,
        "tokens": dict(zip(("input", "cached_input", "output", "reasoning"), tokens)) if tokens else None,
        "ledger": (None if not isinstance(record, dict) else
                   "none" if detail.get("ledger") == "none" or inv.get("ledger") == "none" else "present"),
        "calls": {k: v for k, v in view.items() if k not in ("route", "found")} or None,
        "route": view.get("route"), "found": view.get("found"),
    }
    row["ai"] = {k: v for k, v in row["ai"].items() if v is not None} or None
    safe = _as.safe_new_fields(answer)
    refs = []
    for r in safe.get("referenced_docs") or []:
        e = {"path": _jpath(masker, r["path"]), "ranges": r["ranges"], "unopened": r["unopened"]}
        if r.get("ranges_more"):
            e["ranges_more"] = r["ranges_more"]
        refs.append(e)
    row["referenced_docs"] = refs if "referenced_docs" in safe else None
    row["referenced_docs_extra"] = {k: safe[k] for k in ("referenced_docs_hidden", "referenced_docs_more") if k in safe} or None
    row["found_docs"] = ([_jpath(masker, r["path"]) for r in safe["found_docs"]] if "found_docs" in safe else None)
    row["found_docs_extra"] = {k: safe[k] for k in ("found_docs_hidden", "found_docs_more") if k in safe} or None
    il = safe.get("impact_list")
    if il:
        states: dict = {}
        for r in il["rows"]:
            states[r["state"]] = states.get(r["state"], 0) + 1
        row["impact_list"] = {"traced": il["traced"], "states": states, "more": il["more"], "hidden": il.get("hidden", 0)}
    cs = answer.get("call_stats") if isinstance(answer.get("call_stats"), dict) else None
    if cs:
        row["call_stats"] = {
            "calls": _num(cs.get("calls")), "missing": _num(cs.get("missing")), "opened": _num(cs.get("opened")),
            "opened_unknown": bool(cs.get("opened_unknown")),
            "tools": [{"tool": masker.word(t.get("tool"), "tool"), "role": masker.word(t.get("role"), "route_role"),
                       **{k: _num(t.get(k)) for k in ("calls", "found", "ms", "errors", "truncated")}}
                      for t in cs.get("tools") or [] if isinstance(t, dict)]}
    tu = answer.get("tool_use") if isinstance(answer.get("tool_use"), dict) else None
    if tu:
        row["tool_use"] = {"verdict": masker.word(tu.get("verdict"), "tool_use_verdict") if tu.get("verdict") else None,
                           "nudged": bool(tu.get("nudged"))}
    fb = (data.get("feedback") or {}).get(a["id"])
    if isinstance(fb, dict):
        row["feedback"] = {"rating": masker.word(fb.get("rating"), "feedback_rating"),
                           "tags": [masker.word(t, "feedback_tag") for t in fb.get("tags") or []],
                           "has_comment": bool(isinstance(fb.get("comment"), str) and fb["comment"].strip())}
    return row


def build_jsonl(convs: list[dict], masker: Masker, *, meta: dict, period: tuple[float, float] | None = None) -> str:
    """1 行目に meta、続けて 1 ターン 1 行。期間があるときは返答がその期間にあるターンだけ。伏せ残しを確かめる（残っていれば LeakError）。"""
    rows = []
    for data in convs:
        if not data.get("conversation"):
            continue
        _note_conversation(data, masker)
        for no, t in enumerate(_turn_pairs(data), 1):
            ep = _epoch(((t["assistant"] or {}).get("created_at")))
            if period is not None and (ep is None or not (period[0] <= ep < period[1])):
                continue
            row = turn_row(no, t, data, masker)
            if row is not None:
                rows.append({k: v for k, v in row.items() if v is not None})
    head = {"kind": "meta", "exported_at": _iso(datetime.now().timestamp()), "version": _app_version(),
            "mask": masker.mask, "mask_models": masker.mask_models, "turns": len(rows), **meta}
    text = "\n".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in [head] + rows) + "\n"
    leaks = find_leaks(text, masker)
    if leaks:
        raise LeakError(f"伏せるべき値が {leaks} 件残っていたため書き出しを中止しました")
    return text


def _ledger_section(cid, a, calls, record, answer, masker: Masker, gaps) -> list[str]:
    ledger_calls = [c for c in calls if c.name in _LEDGER_TOOLS]
    inv = answer.get("investigation") if isinstance(answer.get("investigation"), dict) else {}
    if inv.get("ledger") == "none":
        inv = {}  # 台帳の無いターン（調べた経路だけの記録）は台帳の節に出さない
    if isinstance(record, dict) and (record.get("detail") or {}).get("ledger") == "none":
        record = None
    if not ledger_calls and not record and not inv:
        return []
    lines = ["[調査台帳]"]
    if inv:
        rv = inv.get("review") if isinstance(inv.get("review"), dict) else {}
        counts = inv.get("counts") if isinstance(inv.get("counts"), dict) else {}
        lines.append(f"  完了: {'はい' if inv.get('complete') else 'いいえ'}  継続 {_n(inv.get('continuations'))}"
                     f"  見直しの一巡 {_n(rv.get('rounds'))} 回（足した項目 {_n(rv.get('items_added'))}）"
                     + (f"  終端: {_kv_known(counts, 'status', masker)}" if counts else "")
                     + ("  前のターンから引き継ぎ" if inv.get("restored") else ""))
    last_status: dict[str, str] = {}
    if ledger_calls:
        lines.append("  書き換えの流れ:")
        manifest: list = []
        for c in ledger_calls:
            res = _parsed_result(c)
            ok = isinstance(res, dict) and res.get("ok")
            fail = "" if ok or c.name == "ledger_status" else \
                f"  → 失敗（{masker.word((res or {}).get('error'), 'error') if isinstance(res, dict) else '-'}）"
            args = c.args if isinstance(c.args, dict) else {}
            if c.name == "ledger_manifest_set":
                items = [i for i in args.get("items") or [] if isinstance(i, str)]
                added = [i for i in items if i not in manifest]
                removed = [i for i in manifest if i not in items]
                if ok:
                    manifest = items
                lines.append(f"  {_hms(c.ts)}  [{c.stage}]  目録  "
                             + " ".join(["+" + masker.item(i) for i in added] + ["-" + masker.item(i) for i in removed])
                             + fail)
            elif c.name == "ledger_item_put":
                iid = args.get("id") if isinstance(args.get("id"), str) else None
                new = args.get("status")
                prev = masker.word(last_status[iid], "status") if iid in last_status else "（このターンで初めて）"
                ev = [e for e in args.get("evidence") or [] if isinstance(e, dict)]
                kinds = ",".join(sorted({masker.word(e.get("kind"), "evidence") for e in ev})) or "なし"
                lines.append(f"  {_hms(c.ts)}  [{c.stage}]  {masker.item(iid)}  {prev}"
                             f" → {masker.word(new, 'status')}  根拠の種類 {kinds}" + fail)
                if ok and iid and isinstance(new, str):
                    last_status[iid] = new
            elif c.name == "ledger_review_put":
                lines.append(f"  {_hms(c.ts)}  [{c.stage}]  見直し  判断 {masker.word(args.get('verdict'), 'verdict')}"
                             f"  足した {len(args.get('added_items') or [])} 外した {len(args.get('removed_items') or [])}"
                             + fail)
            elif c.name == "ledger_status":
                lines.append(f"  {_hms(c.ts)}  [{c.stage}]  確認  {_result_summary(c)}")
    if record:
        items = record.get("items") if isinstance(record.get("items"), dict) else {}
        lines.append(f"  最終形（investigation_records）  完了: {'はい' if record.get('complete') else 'いいえ'}"
                     f"  切り詰め: {'あり' if record.get('truncated') else 'なし'}  項目 {len(items)} 件")
        bad_ev = 0
        for iid in sorted(items, key=str):
            it = items[iid] if isinstance(items[iid], dict) else {}
            masker.note(it.get("subject"))
            raw_ev = it.get("evidence") if isinstance(it.get("evidence"), list) else []
            ev = [e for e in raw_ev if isinstance(e, dict)]
            bad_ev += len(raw_ev) - len(ev)
            shown = []
            for e in ev[:4]:
                line = e.get("line")
                if not _is_count(line):
                    masker.note_all(line)
                    bad_ev += 1
                shown.append(f"{masker.word(e.get('kind'), 'evidence')} {masker.path(e.get('path'))}"
                             + (f":{line}" if _is_count(line) else ""))
            ev_txt = ", ".join(shown) + (f" ほか {len(ev) - 4}" if len(ev) > 4 else "")
            lines.append(f"  {masker.item(iid)}  状態 {masker.word(it.get('status'), 'status')}  担当 {masker.word(it.get('owner'), 'owner')}"
                         f"  根拠 {ev_txt or 'なし'}  理由 {masker.text(it.get('reason'))}")
            if ledger_calls and it.get("status") == "unverified" and last_status.get(iid) == "not_found_in_scope":
                lines.append(f"      Sherpa が not_found_in_scope → unverified へ自動で下げた（最終形からの推定・時刻の記録なし）")
            elif ledger_calls and iid in last_status and last_status[iid] != it.get("status"):
                lines.append("      最後の書き込みと最終形の状態が違う（書き換えた記録なし）")
        cov = record.get("coverage") if isinstance(record.get("coverage"), dict) else {}
        for iid in sorted(cov):
            outs = cov[iid] if isinstance(cov[iid], list) else []
            lines.append(f"  検索の結果 {masker.item(iid)}: " + "、".join(masker.word(o, "outcome") for o in outs))
        for i, rv in enumerate(record.get("reviews") or [], 1):
            if isinstance(rv, dict):
                for k in ("purpose", "summary"):
                    masker.note(rv.get(k))
                lines.append(f"  見直し{i}: 判断 {masker.word(rv.get('verdict'), 'verdict')}"
                             f"  足した {len(rv.get('added_items') or [])} 外した {len(rv.get('removed_items') or [])}")
        if bad_ev:
            gaps.append(f"調査台帳の根拠のうち形が壊れたもの（行番号が整数でない等）{bad_ev} 件の値を出さなかった")
        if record.get("truncated"):
            gaps.append("調査台帳の最終形は大きすぎて一部を切り詰めて保存されている")
        lines.append(f"  台帳のダウンロード: /conversations/{cid}/messages/{(a or {}).get('id')}/investigation?format=md（json も可）")
    elif a is not None and (ledger_calls or inv):
        gaps.append("調査台帳の最終形（investigation_records）が無い")
    gaps.append("Sherpa が自動で台帳の状態を下げたこと・台帳の途中のファイル（道具名・時刻付きの検索の結果）は記録なし")
    return lines


def render_conversation(data: dict, threads: list[Thread] | None, masker: Masker, *,
                        sessions_state: str) -> list[str]:
    cid = data["id"]
    conv = data.get("conversation")
    if not conv:
        return [f"=== 会話 {cid}: 見つかりません"]
    _note_conversation(data, masker)
    turns = _turn_pairs(data)
    mask_state = ("あり（モデル名も伏せる）" if masker.mask_models else "あり（モデル名は出す）") if masker.mask else "なし"
    lines = [f"=== 会話 {cid}  ターン {len(turns)} 件  伏せ字: {mask_state}",
             f"利用者: {masker.user(conv.get('user_id'))}  資料フォルダ: {masker.scope(conv.get('version'))}"
             f"  セッション記録: {sessions_state}"]
    for name in data.get("errors") or []:
        lines.append(f"（{name} を読めませんでした）")
    for i, t in enumerate(turns, 1):
        lines.extend(render_turn(i, t, data, threads or [], masker))
    return lines


def build_output(convs: list[dict], masker: Masker, users_dir: Path | None, *, notes: tuple = ()) -> str:
    """書き出す本文を組み立て、伏せ残りを確かめる（残っていれば LeakError）。notes は先頭に添える注記の行。"""
    out = [f"# 会話トレース  書き出し {_ymdhms(datetime.now().timestamp())}  版 {_app_version()}", *notes]
    for data in convs:
        threads: list[Thread] = []
        state = "なし"
        uid = ((data.get("conversation") or {}).get("user_id"))
        if users_dir is not None and isinstance(uid, str) and re.match(r"^[A-Za-z0-9_.@-]{1,128}$", uid):
            conv_dir = users_dir / uid / "workspace" / ".codex-sessions" / str(data["id"])
            if conv_dir.is_dir() and not conv_dir.is_symlink():
                threads, skipped = read_threads(conv_dir)
                data["skipped_session_files"] = skipped
                state = f"あり（{len(threads)} 本）" if threads else "なし（フォルダはあるがファイルが無い）"
            else:
                state = "なし（保持期限で消えた・会話の削除・Codex 以外の経路）"
        out.extend(render_conversation(data, threads, masker, sessions_state=state))
        out.append("")
    text = "\n".join(out) + "\n"
    leaks = find_leaks(text, masker)
    if leaks:
        raise LeakError(f"伏せるべき値が {leaks} 件残っていたため書き出しを中止しました")
    return text


def _app_version() -> str:
    try:
        return re.sub(r"[^0-9A-Za-z.+_-]", "?", (_ROOT / "VERSION").read_text(encoding="utf-8").strip())[:40]
    except OSError:
        return "-"


# ---------------------------------------------------------------------------
# DB（読み取りだけ）
# ---------------------------------------------------------------------------

def _query(sql: str, params: tuple) -> list:
    from sherpa.store.db import _connect
    with _connect() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def load_conversation(cid: int) -> dict:
    """会話 1 件ぶんの行を読む（SELECT だけ）。読めない表は errors に名前を残して続ける。"""
    data: dict = {"id": cid, "errors": [], "turn_metrics": {}, "investigations": {}}

    def _try(name, sql, params):
        try:
            return _query(sql, params)
        except Exception:
            data["errors"].append(name)
            return []

    rows = _try("conversations", "SELECT * FROM conversations WHERE id=%s", (cid,))
    data["conversation"] = rows[0] if rows else None
    if not rows:
        return data
    data["messages"] = _try("messages", "SELECT id, role, content, lens, trace, answer, personal, created_at "
                                        "FROM messages WHERE conversation_id=%s ORDER BY id", (cid,))
    data["audits"] = _try("audit_log", "SELECT id, detail, outcome, created_at FROM audit_log "
                                       "WHERE action='chat.turn' AND resource_id=%s ORDER BY id", (f"conv:{cid}",))
    data["turn_metrics"] = {r["message_id"]: r for r in _try(
        "turn_metrics", "SELECT * FROM turn_metrics WHERE conversation_id=%s", (cid,))}
    data["investigations"] = {r["message_id"]: r for r in _try(
        "investigation_records", "SELECT * FROM investigation_records WHERE conversation_id=%s", (cid,))}
    ids = [m["id"] for m in data["messages"] if m.get("role") == "assistant"]
    data["feedback"] = {r["message_id"]: r for r in _try(
        "message_feedback", "SELECT DISTINCT ON (message_id) message_id, rating, tags, comment FROM message_feedback "
                            "WHERE message_id = ANY(%s) ORDER BY message_id, created_at DESC", (ids,))} if ids else {}
    data["usage_events"] = _try("usage_events", "SELECT * FROM usage_events WHERE conversation_id=%s ORDER BY ts",
                                (cid,))
    uid = data["conversation"].get("user_id")
    users = _try("users", "SELECT uid, email, display_name FROM users WHERE uid=%s", (uid,))
    data["user"] = users[0] if users else None
    worlds = _try("worlds", "SELECT world_id, root_path, label FROM worlds WHERE world_id=%s",
                  (data["conversation"].get("version"),))
    data["world"] = worlds[0] if worlds else None
    return data


def _users_dir() -> Path:
    p = Path(os.environ.get("SHERPA_USERS_DIR", "data/users"))
    return p if p.is_absolute() else _ROOT / p


def _parse_day(value: str | None) -> date | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date() if value else None
    except ValueError:
        return None


def select_conversation_ids(since: date, until: date) -> tuple[list[int], bool]:
    """期間（日本時間の日付・until はその日を含む）に assistant の返答がある会話の番号（古い順）と、上限（`CONV_LIMIT`）を超えたか。削除済み・受領共有の複製は除く。"""
    start = datetime(since.year, since.month, since.day, tzinfo=_JST)
    end = datetime(until.year, until.month, until.day, tzinfo=_JST) + timedelta(days=1)
    rows = _query(
        "SELECT m.conversation_id AS id FROM messages m JOIN conversations c ON c.id = m.conversation_id "
        "WHERE m.role='assistant' AND m.created_at >= %s AND m.created_at < %s "
        "AND c.deleted_at IS NULL AND c.origin='own' GROUP BY m.conversation_id ORDER BY MIN(m.created_at), MIN(m.id) LIMIT %s",
        (start, end, CONV_LIMIT + 1))
    ids = [r["id"] for r in rows]
    return ids[:CONV_LIMIT], len(ids) > CONV_LIMIT


_USAGE = ("使い方: make trace CONV=<会話番号>[,<会話番号>...] または SINCE=<YYYY-MM-DD> [UNTIL=<YYYY-MM-DD>]"
          "（どちらか片方）[FORMAT=jsonl] [MASK=1] [OUT=<出力ファイル>]")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="会話トレースの書き出し（読み取りだけ）")
    ap.add_argument("--conv", help="会話番号（カンマ区切りで複数）")
    ap.add_argument("--since", help="期間の開始日（YYYY-MM-DD・日本時間）")
    ap.add_argument("--until", help="期間の終了日（YYYY-MM-DD・その日を含む・省略時は今日）")
    ap.add_argument("--format", choices=("text", "jsonl"), default="text", help="出力の形（既定は text）")
    ap.add_argument("--mask", action="store_true", help="伏せ字にする")
    ap.add_argument("--mask-models", action="store_true", help="伏せ字でモデル名も伏せる（公開のモデル名だけ出す）")
    ap.add_argument("--out", help="出力ファイル（省略時は標準出力）")
    ns = ap.parse_args(argv)
    since, until = _parse_day(ns.since), _parse_day(ns.until)
    if bool(ns.conv) == bool(ns.since) or (ns.since and since is None) or (ns.until and (not ns.since or until is None)):
        print(_USAGE, file=sys.stderr)
        return 2
    ids: list[str] = []
    if ns.conv:
        ids = [s.strip() for s in ns.conv.split(",") if s.strip()]
        if not ids or not all(s.isdigit() for s in ids):
            print(_USAGE, file=sys.stderr)
            return 2
    else:
        until = until or datetime.now(_JST).date()
        if until < since:
            print(_USAGE, file=sys.stderr)
            return 2
    masker = Masker(mask=ns.mask, mask_models=ns.mask_models)
    notes: list[str] = []
    meta: dict = {}
    period = None
    if since:
        found, over = select_conversation_ids(since, until)
        ids = [str(i) for i in found]
        meta = {"since": since.isoformat(), "until": until.isoformat(), "conversations": len(ids),
                "limit": CONV_LIMIT, "truncated": over}
        notes.append(f"# 期間 {since.isoformat()}〜{until.isoformat()}（日本時間）  会話 {len(ids)} 件")
        start = datetime(since.year, since.month, since.day, tzinfo=_JST)
        period = (start.timestamp(), (datetime(until.year, until.month, until.day, tzinfo=_JST)
                                      + timedelta(days=1)).timestamp())
        if over:
            msg = f"対象の会話が上限の {CONV_LIMIT} 件を超えたため、古い順に {CONV_LIMIT} 件までにしています（残りは期間を狭めて取り直してください）"
            notes.append("# " + msg)
            meta["note"] = msg
            print(msg, file=sys.stderr)
    convs = [load_conversation(int(s)) for s in ids]
    try:
        if ns.format == "jsonl":
            text = build_jsonl(convs, masker, meta=meta, period=period)
        else:
            text = build_output(convs, masker, _users_dir(), notes=tuple(notes))
    except LeakError as e:
        print(str(e), file=sys.stderr)
        return 3
    if ns.out:
        Path(ns.out).write_text(text, encoding="utf-8")
        print(f"書き出しました: {ns.out}（{text.count(chr(10))} 行）", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
