"""管理画面の Ollama 接続テスト（`POST /admin/settings/ollama-test`）の補助。失敗の案内文と、埋め込みの接続先の確かめ。
設計: docs/design/settings.md「埋め込み用の Ollama の接続先」
"""
from __future__ import annotations


def test_hint(e: Exception, port: int) -> str:
    """Ollama の接続テストの失敗を、管理者向けの平文の手がかりにする（到達できない理由の区別は出す）。"""
    import socket
    import urllib.error
    if isinstance(e, urllib.error.HTTPError):
        return f"Ollama から HTTP {e.code} が返りました。接続先が Ollama か確かめてください"
    reason = e.reason if isinstance(e, urllib.error.URLError) else e
    if isinstance(reason, ConnectionRefusedError):
        return ("接続を拒否されました。相手のパソコンで Ollama が起動しているか、"
                "ほかのパソコンからの接続を受け付ける設定（OLLAMA_HOST=0.0.0.0）で起動しているかを確かめてください")
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return (f"応答がありません（時間切れ）。相手のパソコンのファイアウォールで"
                f"ポート {port} への接続が許可されているかを確かめてください")
    if isinstance(reason, socket.gaierror):
        return "接続先の名前からパソコンを見つけられません。IP アドレスで入力してみてください"
    if isinstance(e, (ValueError, TypeError)):
        return "応答を Ollama の応答として読めませんでした。接続先が Ollama か確かめてください"
    return f"接続できませんでした（{type(reason).__name__}）"


def wanted_models(sys_s: dict, hp: tuple) -> set[str]:
    """この接続先（`hp`）に取得済みであるべき Ollama のモデル。使えるモデル一覧に登録したモデルのうち、回答・Codex などの用途の分と、
    埋め込みがこの接続先を使うときだけ埋め込みの欄の分（埋め込みが別の接続先・Ollama 以外のときは数えない）。
    """
    from sherpa import ai_targets, keys, llm, model_catalog

    def listed(cell: dict) -> set[str]:
        return {m for m in [*(cell.get("allowed") or []), cell.get("default")] if m}
    cells = model_catalog.get_catalog(sys_s).get("ollama") or {}
    wanted: set[str] = set()
    for usage, cell in cells.items():
        if usage != "embed":
            wanted |= listed(cell)
    # 埋め込みの接続先を分けていない（中央と同じ）ときは、試している接続先が保存後に埋め込みにも使われる。判定できない設定では数えない
    try:
        target = ai_targets.embed_target(sys_s)
        if target is None or target.provider != "ollama":
            return wanted
        central = llm._canonical_host_port(keys.resolve_ollama_url(None, system_settings=sys_s))
        embed = llm._canonical_host_port(target.base)
    except Exception:
        return wanted
    if embed is not None and central is not None and (embed == central or embed == hp):
        wanted |= listed(cells.get("embed") or {}) | ({target.model} if target.model else set())
    return wanted


def embed_test_result(url: str, hp: tuple, host: str, names: list[str], sys_s: dict, timeout: float, max_bytes: int) -> dict:
    """埋め込みの接続先の確かめ。埋め込みのモデルが取得済みか（`/api/tags`）と、埋め込みに対応しているか（`/api/show`）を見る。"""
    import json as _json
    import urllib.request

    from sherpa import ai_targets, embeddings, llm
    model = embeddings._ollama_model(sys_s)
    if not ai_targets.model_installed(model, set(names)):
        return {"ok": True, "host": host, "models": names, "missing": [model], "no_tools": [],
                "detail": "接続OK（埋め込みのモデルがこのサーバーにありません）"}
    no_embed: list[str] = []
    try:
        show_req = urllib.request.Request(
            llm.ollama_url(url, "/api/show", extra_allowed={hp}, system_settings=sys_s),
            data=_json.dumps({"model": model}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with llm.no_proxy_requests(), llm.urlopen_no_redirect(show_req, timeout=timeout) as r:
            raw = r.read(max_bytes + 1)
        caps = _json.loads(raw).get("capabilities") if len(raw) <= max_bytes else None
        if isinstance(caps, list) and "embedding" not in caps:
            no_embed.append(model)
    except Exception:
        pass
    return {"ok": True, "host": host, "models": names, "missing": [], "no_tools": [],
            "detail": ("接続OK（埋め込みのモデルがありますが、埋め込みに対応していないようです）" if no_embed
                       else "接続OK（埋め込みのモデルがあります）")}
