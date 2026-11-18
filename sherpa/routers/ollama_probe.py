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


def embed_test_result(url: str, hp: tuple, host: str, names: list[str], sys_s: dict, timeout: float, max_bytes: int) -> dict:
    """埋め込みの接続先の確かめ。埋め込みのモデルが取得済みか（`/api/tags`）と、埋め込みに対応しているか（`/api/show`）を見る。"""
    import json as _json
    import urllib.request

    from sherpa import embeddings, llm
    model = embeddings._ollama_model(sys_s)
    have = set(names)
    present = model in have or f"{model}:latest" in have
    if not present:
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
