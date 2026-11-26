"""道具のサーバー（stdio）の個人ファイルの道具の出し入れ。親が環境変数で渡した一覧の置き場と `files/` の実パスだけを見る。
設計: docs/design/codex.md「MCP の道具」、docs/06-認証と個人領域.md §3
"""
from __future__ import annotations

import os
import time

from . import agentic_search
from .parts.read import personal

TOOL_NAMES = personal.TOOL_NAMES
ENV_INDEX = "SHERPA_MCP_PERSONAL_INDEX"
ENV_DIR = "SHERPA_MCP_PERSONAL_DIR"
TOOLSET = "personal"
_PERSONAL_TOOLSET = TOOL_NAMES | {"ask_user"}


def _offered(toolset: str) -> bool:
    """このターンで個人ファイルの道具を出すか（親が置き場を渡したターンだけ・素の Codex では出さない）。"""
    return toolset != "plain" and bool(os.environ.get(ENV_INDEX, "").strip() and os.environ.get(ENV_DIR, "").strip())


def tool_defs(toolset: str) -> list:
    """tools/list に足す個人ファイルの道具の定義（出さないターンは空）。個人ファイルだけの形（`TOOLSET`）では資料フォルダの道具を出さず、これと ask_user だけになる。"""
    if not _offered(toolset):
        return []
    return [{"name": "personal_search", "description": agentic_search._DESC_PERSONAL_SEARCH,
             "inputSchema": agentic_search._PARAMS_PERSONAL_SEARCH},
            {"name": "personal_read", "description": agentic_search._DESC_PERSONAL_READ,
             "inputSchema": agentic_search._PARAMS_PERSONAL_READ}]


def refused(name, toolset: str) -> bool:
    """出していない道具を呼ばれたか（呼ばれたら存在しない道具と同じに拒む）。個人ファイルの道具は出したターンだけ、
    個人ファイルだけの形（`TOOLSET`）では個人ファイルの道具と ask_user 以外の全部（資料フォルダの道具・台帳の道具）を拒む。"""
    if toolset == TOOLSET and name not in _PERSONAL_TOOLSET:
        return True
    return name in TOOL_NAMES and not _offered(toolset)


def context(name, toolset: str):
    """個人ファイルの道具の呼び出しなら、一覧と `files/` から作った `PersonalContext`（作れなければ None）。それ以外の道具は None。"""
    if name not in TOOL_NAMES or not _offered(toolset):
        return None
    return personal.load_context(os.environ.get(ENV_INDEX, "").strip(), os.environ.get(ENV_DIR, "").strip())


def sidecar_entries(name, args, result) -> list[dict]:
    """サイドカーへ書く「個人ファイルを読んだ」行（rel_path だけ・本文は書かない）。資料フォルダの `read` とは別の種類にする。"""
    now = time.time()
    return [{"kind": "personal_read", "tool": name, "rel_path": rel, "ts": now}
            for rel in personal.read_rels(name, args, result)]
