"""読み取り部品の境界を固定する特性テスト（`docs/design/interfaces.md` §4.1・§5）。

道具の実装を `sherpa/parts/read/`（読み取り部品）・
`sherpa/tool_dispatch.py`（道具ディスパッチの正本）へ切り出した際の契約:
- MCP・回答ループ・簡易チャット（`/ext/v1/answer` 経由・`sherpa/simple_chat.py`）が**同じ関数
  オブジェクト**を共有すること（挙動が経路ごとに分岐しない・二重実装しない）。
- 読み取り部品（`sherpa/parts/read/`）は回答ループ
  （`sherpa/agentic_search.py`）・道具ディスパッチ（`sherpa/tool_dispatch.py`）を import しない
  （循環回避・部品は権限もループの状態も持たない・§4.3）。

外部境界（ES・Neo4j・HTTP）のモックは使わない——本ファイルの検証は import 時に確定する
関数オブジェクトの同一性とモジュールの静的な import 文だけを見る。
"""
from __future__ import annotations

import ast
from pathlib import Path

from sherpa import ext_api, mcp_server, simple_chat, tool_dispatch
from sherpa.parts.read import fused_search, tools as read_tools


def test_read_tool_dispatch_is_shared_across_mcp_and_simple_chat():
    """`ripgrep_search` 等の読み取り系道具の実装本体（`sherpa.parts.read.tools.run_tool`）は、
    MCP（`mcp_server.py`）・簡易チャット（`/ext/v1/answer`・`sherpa/simple_chat.py::answer` が直接呼ぶ）の
    どの経路からも**同じ関数オブジェクト**として参照される（`is` で同一性を確認・二重実装しない）。
    """
    # mcp_server.py::handle() は tool_dispatch.run_tool を直接呼ぶ（本モジュール経由の同一オブジェクト）。
    assert mcp_server.tool_dispatch.run_tool is tool_dispatch.run_tool
    # simple_chat.py（簡易チャットの薄いループ）は `from .tool_dispatch import run_tool` で
    # 同じ関数オブジェクトを直接呼ぶ（§4.1・重複実装しない）。
    assert simple_chat.run_tool is tool_dispatch.run_tool
    # 道具ディスパッチ自身は、読み取り部品へ素通しする
    # （tool_dispatch.py 内部の read_tools 参照が実際に parts.read.tools.run_tool であること）。
    assert tool_dispatch.read_tools.run_tool is read_tools.run_tool


def test_fused_search_is_shared_between_ext_api_and_parts_read():
    """`/ext/v1/search`（`ext_api.py::ext_search`）が呼ぶ融合検索（ES＋Neo4j の RRF 融合）は、
    読み取り部品 `sherpa.parts.read.fused_search.search` と**同じ関数オブジェクト**。
    """
    assert ext_api.fused_search.search is fused_search.search


def test_parts_read_modules_do_not_import_loop_modules():
    """読み取り部品（`sherpa/parts/read/*.py`）は、回答ループ
    （`agentic_search`）・道具ディスパッチ（`tool_dispatch`）を import しない（静的検査・import 境界・
    `docs/design/interfaces.md` §4.1・§4.3「部品自身は権限を持たない」の裏付け＝循環させない）。
    """
    forbidden = {"sherpa.agentic_search", "sherpa.tool_dispatch", "agentic_search", "tool_dispatch"}
    read_parts_dir = Path(read_tools.__file__).resolve().parent
    checked = 0
    for path in sorted(read_parts_dir.glob("*.py")):
        if path.name == "__init__.py":
            continue
        checked += 1
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                # 相対 import（`from ... import agentic_search` 等）は module が None/相対名になる
                # ため、import される名前（alias.name）側もあわせて見る。
                names = {node.module} if node.module else set()
                names |= {alias.name for alias in node.names}
            else:
                continue
            bad = names & forbidden
            assert not bad, f"{path}: 禁止 import {bad}"
    assert checked >= 2, "読み取り部品のモジュールが見つからない（tools.py/fused_search.py の配置を確認）"
