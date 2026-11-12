"""Codex の構成（Ollama／OpenAI 系）ごとの「モデルが画像を読めるか」の判定。
設定 `codex_image_input`（システム設定）は `{"ollama": bool, "openai": bool}`。未設定・型違いの項目は既定（Ollama は読めない・OpenAI 系は読める）。
モデル名では判定しない（画像を読める Ollama のモデルを使うときは管理者が `ollama` を真にする）。
設計: docs/design/codex.md「実行の構成」
"""
from __future__ import annotations

# 構成 → 既定。ローカルの文字専用モデルを既定の想定にする
DEFAULTS = {"ollama": False, "openai": True}


def codex_image_input(system_settings: dict | None, *, ollama: bool) -> bool:
    """この構成のモデルが画像を読めるか。`system_settings` が None なら保存済みの設定を読む（読めなければ既定）。"""
    if system_settings is None:
        try:
            from . import store
            system_settings = store.get_system_settings()
        except Exception:
            system_settings = {}
    kind = "ollama" if ollama else "openai"
    configured = system_settings.get("codex_image_input") if isinstance(system_settings, dict) else None
    value = configured.get(kind) if isinstance(configured, dict) else None
    return value if isinstance(value, bool) else DEFAULTS[kind]


# MCP の子（道具のサーバー）へ渡す env。Codex を起こす側が上の判定を 1 回だけ解決して渡す（子は DB を読めない）。
MCP_ENV = "SHERPA_MCP_IMAGE_INPUT"


def mcp_env(system_settings: dict | None, *, ollama: bool) -> dict:
    """画像を読めない構成のときだけ子へ渡す env。読める構成は何も渡さない（従来どおり）。"""
    return {} if codex_image_input(system_settings, ollama=ollama) else {MCP_ENV: "0"}


def mcp_image_input() -> bool:
    """MCP の子の側の判定。env が "0" のときだけ偽。"""
    import os
    return os.environ.get(MCP_ENV) != "0"


_NO_IMAGE_NOTE = "この構成では画像を読めない。画像の中身は確かめられないと書き、文字層から推定して補わない"


def pdf_view_hint(hint: str) -> str:
    """`pdf_pages` の `view_hint`。画像を読めない構成では画像で確かめる案内を外す。"""
    return hint if mcp_image_input() else "view_pages のページは文字層だけでは図・矢印・画像の中が読めない。" + _NO_IMAGE_NOTE


def pdf_pages_description(desc: str) -> str:
    """`pdf_pages` の道具の説明。画像を読めない構成では `pdf_page_image` への案内を外す。"""
    if mcp_image_input():
        return desc
    return desc.replace("＝pdf_page_image でページを画像にして確かめる。", f"。{_NO_IMAGE_NOTE}。")


def pdf_page_image_next(next_text: str) -> str:
    """`pdf_page_image` の結果の `next`。画像を読めない構成では画像を開く案内を外す。"""
    return next_text if mcp_image_input() else _NO_IMAGE_NOTE


def pdf_page_image_defs(defs: list) -> list:
    """道具の一覧へ足す `pdf_page_image` の定義。画像を読めない構成では空。"""
    return defs if mcp_image_input() else []
