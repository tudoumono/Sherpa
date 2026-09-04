"""Codex（CLI サブプロセス）関連の実装をまとめるサブパッケージ（リファクタリング計画 フェーズ5・
S8〜S10 で段階的に埋める）。

S8: `sandbox.py`（permission profile によるサンドボックス機構・Marp/Chrome バイナリ検出・
web_search ポリシー）。S9: `mcp.py`（`_mcp_env`/`_mcp_config_args`/neighbors 連携/`_codex_ask_*`・完了）。
S10: `provider.py`（`CodexProvider` 本体・exec 核・完了）。`sherpa/agents.py` は facade として存続し、
本パッケージから再エクスポートされた名前をそのまま公開し続ける（呼び出し側・monkeypatch 先は
`sherpa.agents.X` のまま無改修で動く）。
"""
from __future__ import annotations
