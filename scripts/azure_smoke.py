#!/usr/bin/env python3
"""Azure OpenAI（または他の OpenAI 互換エンドポイント）への実疎通を一発で確認する（2026-08-20 作成）。

背景: `sherpa/llm.py` に接続先の設定化（`OPENAI_BASE_URL` 等・2026-08-18・
docs/proposals/2026-08-18-AzureOpenAI対応.md）を実装したが、実 Azure での疎通は未検証だった。
このスクリプトは、実 Azure アカウントを持つ利用者が「何が通って何が落ちるか」を一発で確かめ、
同じものを先方（本番）環境の受け入れ確認にも使えるようにする。

**最重要の設計原則（絶対に守る）**: 検査は Sherpa の本番コードをそのまま呼ぶ。HTTP を
再実装しない。実際に呼ぶ本番関数/クラス:
  - `sherpa.llm`: `openai_base_url` / `openai_url` / `openai_headers` /
    `openai_endpoint_kind` / `assert_openai_base_url_allowed` / `openai_post_json` /
    `_redact_url_for_error`（URL 表示の伏せ字化）
  - `sherpa.providers.openai.OpenAIProvider`: `_messages`（本文の組み立て）・
    `_stream`（ストリーミング本体・usage 捕捉を含めてそのまま呼ぶ）
  - `sherpa.agentic_search`: `openai_tools`（tool calling 検査に渡す本番のツール定義）
  - `sherpa.embeddings`: `cfg` / `_embed_batch`（embeddings 検査。`embed()` でなく
    `_embed_batch` を直接呼ぶ理由は該当関数のコメント参照＝次元不一致を隠さず表示するため）
  - `sherpa.providers.codex.sandbox`: `_write_codex_authoring_config` / `_codex_clean_env`
    （Codex 検査の config.toml 生成・sandbox env 生成。`codex exec` の起動自体はこのスクリプトが
    行う＝`sherpa/providers/codex/provider.py` の重い実行ループ（Ctx/MCP/resume 等）は使わない、
    という指示どおりの最小構成）

実行:
    .venv/bin/python scripts/azure_smoke.py --env-file azure.env --dry-run   # 通信なし・設定確認のみ
    .venv/bin/python scripts/azure_smoke.py --env-file azure.env --yes      # 実 API を叩く（課金あり）
    make azure-smoke ARGS="--env-file azure.env --yes"                     # 同上（Makefile 経由）

**安全**: `--dry-run` を付けない限り実 API を叩く（課金される）。`--yes` を付けないと確認プロンプトが
出る（非対話環境では `--yes` が必須）。各検査は1回だけ呼ぶ。秘密（API キー・Authorization/api-key
ヘッダの値・URL の userinfo）は一切出力しない。
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import zlib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 本番コードをそのまま使う（再実装しない・モジュール docstring 参照）。
from sherpa import agent_constructs, agentic_search, embeddings, llm  # noqa: E402
from sherpa.providers.codex import sandbox as codex_sandbox  # noqa: E402
from sherpa.providers.openai import OpenAIProvider  # noqa: E402

_TIMEOUT = 30            # 通常の HTTP 検査（②③④⑥⑦）のタイムアウト秒
_CODEX_TIMEOUT = 90      # ⑧ codex exec の待ち上限秒（reasoning="low"・短いプロンプトなら通常もっと短い）

# 検査の登録順（この順で実行・表示する）。
_ALL_CHECKS = ["config", "chat", "stream", "tools", "embeddings", "responses", "vision", "codex"]
_REQUIRED_CHECKS = {"config", "chat", "stream", "embeddings"}   # 落ちたら終了コード1
_GATED_BY_FLAG = {"vision": "--vision", "codex": "--codex"}     # 既定OFF・明示フラグが要る
_LABELS = {
    "config": "① 設定の解決（通信なし）",
    "chat": "② chat/completions（非ストリーム）",
    "stream": "③ chat/completions（ストリーム＋usage）",
    "tools": "④ tool calling（本番のツール定義）",
    "embeddings": "⑤ embeddings（次元1536を確認）",
    "responses": "⑥ Responses API（Codex(OpenAI) 構成の生死判定）",
    "vision": "⑦ 画像入力（VLM）",
    "codex": "⑧ Codex CLI 経由",
}
_PATH_BY_CHECK = {
    "chat": "chat/completions", "stream": "chat/completions", "tools": "chat/completions",
    "embeddings": "embeddings", "responses": "responses", "vision": "chat/completions",
}
_NEXT_STEPS = {
    "config": "OPENAI_BASE_URL（https:// 必須・ホスト名必須・userinfo/クエリ禁止）を確認してください。",
    "chat": "401/403 なら OPENAI_API_KEY、404 ならモデル欄のデプロイ名の誤り、"
            "400 で api-key 関連のエラーなら SHERPA_OPENAI_AUTH_HEADER=api-key を試してください。",
    "stream": "400 で stream_options 関連のエラーなら、Azure 側がこれを拒否している可能性があります"
              "＝実装側の対応が必要です（sherpa/providers/openai.py::OpenAIProvider._stream の"
              "stream_options 送出を分岐させる改修を検討してください）。それ以外は②と同じ対処です。",
    "tools": "対象デプロイが function calling に対応しているか確認するか、対応デプロイに切り替えてください。",
    "embeddings": "OPENAI_EMBED_MODEL に埋め込み用デプロイ名を設定し、"
                 "そのデプロイが 1536 次元（text-embedding-3-small 相当）であることを確認してください。",
    "responses": "Responses API が未提供のデプロイ/リージョンの可能性があります。"
                "Codex(OpenAI) 構成は使えないと判断し、OpenAI 直結（agent=openai・chat/completions）"
                "構成を使ってください。",
    "vision": "対象デプロイが画像入力（vision）に対応しているか確認してください。",
    "codex": "codex CLI の導入・SHERPA_CODEX_SANDBOX・OPENAI_API_KEY・モデル欄のデプロイ名（-m）を"
            "確認してください。",
}
_CONTENT_FILTER_MARKERS = (
    "content_filter", "responsibleaipolicyviolation", "content management policy", "jailbreak",
)

_HELP_EPILOG = """
必要な env（--env-file か、通常の .env / シェル export のどちらでも可）:
  OPENAI_API_KEY              Azure OpenAI のキー
  OPENAI_BASE_URL             例: https://<リソース名>.openai.azure.com/openai/v1/
  SHERPA_OPENAI_AUTH_HEADER   bearer（既定）| api-key（環境によってはこちらが必要）
  SHERPA_OPENAI_API_VERSION   v1 API（末尾 /openai/v1/）では不要。旧方式のエンドポイントのみ必要
  OPENAI_EMBED_MODEL          埋め込み用デプロイ名（既定 text-embedding-3-small）
  OPENAI_CHAT_MODEL           チャット/Responses/Codex 用デプロイ名（--model でも指定可・既定 gpt-5.5）

Azure 側の準備:
  - リソースを作成し、チャット用・埋め込み用（1536次元・text-embedding-3-small 相当）の
    2つのデプロイをあらかじめ用意してください。
  - 設定画面/このスクリプトの「モデル」欄には、モデル名そのものではなく Azure 側の
    デプロイ名を入れます（既定の "gpt-5.5" のままでは Azure に通りません）。

検査項目（この順・1つ落ちても後続は続けます）:
  ① config      設定の解決（通信なし）
  ② chat        chat/completions（非ストリーム）                          [必須]
  ③ stream      chat/completions（ストリーム＋usage・stream_options 込み） [必須]
  ④ tools       tool calling（agentic_search.openai_tools の本番定義）
  ⑤ embeddings  embeddings（次元 1536 を確認）                            [必須]
  ⑥ responses   Responses API（Codex(OpenAI) 構成が使えるかの生死判定）
  ⑦ vision      画像入力（既定OFF・--vision で有効化）
  ⑧ codex       Codex CLI 経由（既定OFF・--codex で有効化・codex 未導入なら自動スキップ）

終了コード:
  0  全て OK（または --dry-run）
  1  必須項目（②③⑤、および①の設定エラー）のいずれかが失敗
  2  任意項目（④⑥⑦⑧）だけが失敗、または実 API 検査そのものが未承認で中止された

例:
  .venv/bin/python scripts/azure_smoke.py --env-file azure.env --dry-run
  .venv/bin/python scripts/azure_smoke.py --env-file azure.env --yes
  .venv/bin/python scripts/azure_smoke.py --env-file azure.env --yes --vision --codex --json
"""


# ---- SET-2c: 接続先の解決を system_settings（DB）へ移した後もこのスクリプトは env（--env-file）を
#      「今から試したい設定」として受け取るツールという性質のまま使い続けるため、実 DB は一切
#      触らずこのプロセス内だけ `store.get_system_settings()` を env 由来の値で差し替える。 ----

def _seed_settings_from_parsed_env() -> str | None:
    """`_load_env_file` 適用後の `os.environ` から、接続先系の値をこのプロセス内だけの
    `sherpa.store.get_system_settings()` 差し替えへ合成する（実 DB へは一切書かない）。

    `sherpa.llm` の接続先系関数（`openai_base_url`/`openai_endpoint_kind`/`openai_headers`/
    `openai_url`/`openai_api_version`）は呼び出し時に毎回 `system_settings`（DB）を読む設計
    （SET-2c）になったため、このスクリプトが `--env-file` で受け取った値を実際に効かせるには、
    その読み取り先を差し替える必要がある。`OPENAI_EMBED_MODEL` は `model_catalog`（openai/embed）
    が読む値なので、そちらも合わせて差し替える。

    候補の組み立て・検証は自前で再実装せず、本番の起動時シード resolver
    （`sherpa.llm.openai_endpoint_seed_candidate`）をそのまま呼ぶ（I/O なし・純関数）。明示
    kind 優先・4項目を1候補として原子検証・kind が openai 以外で base_url が無ければ拒否・未知
    kind/auth_header は拒否——本番の `PUT /admin/settings`／起動時シードと**完全に同じ判定**を
    通信前に適用する。

    戻り値: 候補が不正なら理由文字列（呼び出し元は通信前に中断すべき）、有効なら `None`。
    """
    import sherpa.store as _store
    error: str | None = None
    try:
        overrides: dict = dict(llm.openai_endpoint_seed_candidate())
    except ValueError as e:
        overrides = {}
        error = str(e)
    embed_model = (os.environ.get("OPENAI_EMBED_MODEL") or "").strip()
    if embed_model:
        overrides["model_catalog"] = {"openai": {"embed": {"allowed": [embed_model], "default": embed_model}}}
    _store.get_system_settings = lambda: dict(overrides)
    return error


# ---- env ファイル読み込み（明示 env ＞ ファイル ＞ 既定・scripts/run-common.sh と同じ優先順） ----

def _load_env_file(path: Path) -> list[str]:
    """`KEY=VALUE` 形式の env ファイルを読み、`os.environ` に**未設定のときだけ**入れる。

    呼び出し側（シェル export 済みの明示指定）を上書きしない＝
    「明示 env ＞ .env ＞ 既定」という repo 全体の契約（scripts/run-common.sh 冒頭）と同じ順序。
    実際に反映したキー名の一覧を返す（値は返さない＝呼び出し元がうっかり出力しないため）。
    """
    applied: list[str] = []
    if not path.is_file():
        return applied
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if not key or key in os.environ:
            continue
        os.environ[key] = value
        applied.append(key)
    return applied


# ---- 秘密の伏せ字化 ----

def _scrub(text: str, secret: str) -> str:
    """出力用テキストから秘密（キー値）を伏せ字にする。`secret` が空文字なら何もしない
    （空文字で `.replace()` すると全文字の間に挿入されてしまうため）。"""
    if secret:
        text = text.replace(secret, "****")
    return text


def _short(text: object, n: int) -> str:
    s = " ".join(str(text).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _content_filter_note(text: str) -> str | None:
    """Azure 固有のコンテンツフィルタ応答/エラーを検出する（Sherpa 本体はこの応答種別を扱っていない）。

    **注意**: これは短いテキスト（`finish_reason`/`incomplete_reason` の値・組み立てた応答本文・
    HTTP エラー本文など）向けの粗いマーカー検索。成功応答（HTTP 200）の JSON 全体をこれに通しては
    いけない（`_has_filtered_category` の docstring 参照＝実機で誤検知した経緯）。"""
    low = (text or "").lower()
    if any(m in low for m in _CONTENT_FILTER_MARKERS):
        return ("［コンテンツフィルタの疑い＝Azure 側のポリシーでブロックされました。"
                "Sherpa 本体はこの応答種別をまだ扱っていません＝将来の実装課題として記録してください］")
    return None


def _has_filtered_category(obj: object) -> bool:
    """応答 JSON の中に、実際に `filtered: true` のカテゴリがあるかを構造をたどって調べる。

    2026-08-21 実機（Azure `gpt-4.1-mini`）で判明したバグ: 旧実装は成功応答（HTTP 200）の
    JSON 全体を `json.dumps()` で文字列化し、そこに部分文字列 "content_filter" が含まれるかを
    見ていた。しかし Azure は**正常な**応答にも `choices[].content_filter_results` /
    `prompt_filter_results`（＝フィルタを通した結果の記録。キー名自体に "content_filter" を含む・
    通常は各カテゴリ `filtered: false`）を必ず付与するため、`finish_reason=stop` で本文が
    正しく返っている完全な成功（②）や `status=completed` の成功（⑥）まで毎回 NG と誤判定していた
    （実機ログ: `finish_reason=stop content='OK'` なのに NG）。ここではキー名の文字列一致ではなく、
    実際に値が `filtered: true` になっているカテゴリが存在するかどうかだけを見る。"""
    if isinstance(obj, dict):
        if obj.get("filtered") is True:
            return True
        return any(_has_filtered_category(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_filtered_category(v) for v in obj)
    return False


def _chat_filter_verdict(resp: dict, finish_reason: str | None, content: str) -> tuple[bool, str | None]:
    """② chat/completions の成功応答（HTTP 200）がコンテンツフィルタで遮断されたかを判定する。

    戻り値 `(blocked, note)`。`blocked=True` のときだけ検査を NG にする。判定条件:
      - `finish_reason == "content_filter"` ＝実際に遮断＝NG。
      - それ以外で `content_filter_results`/`prompt_filter_results` に `filtered: true` の
        カテゴリがあるが本文（`content`）は返っている＝一部カテゴリが検知されただけで応答自体は
        成功している「部分フィルタ」。**判断（2026-08-21）**: 疎通確認としては成功のため OK 扱いに
        し、検知されたことだけを注記する。NG にすると「本文もちゃんと返っているのに落ちた」という
        今回と同種の誤解を招くため。
      - 本文が空で `filtered: true` のカテゴリがある＝内容が返っていない＝NG（フィルタ由来の疑い）。
    """
    if finish_reason == "content_filter":
        return True, "［コンテンツフィルタ＝finish_reason=content_filter で応答が遮断されました］"
    if _has_filtered_category(resp):
        if content:
            return False, "［一部カテゴリがコンテンツフィルタで検知されましたが、本文は正常に返っています＝OK扱い］"
        return True, "［コンテンツフィルタの疑い＝filtered:true のカテゴリがあり本文も空です］"
    return False, None


def _responses_filter_verdict(resp: dict, status: str | None) -> tuple[bool, str | None]:
    """⑥ Responses API の成功応答（HTTP 200）がコンテンツフィルタで遮断されたかを判定する
    （`_chat_filter_verdict` の Responses 版・判断基準は同じ）。

      - `status == "incomplete"` かつ `incomplete_details.reason` がフィルタ由来＝NG。
      - それ以外で `filtered: true` のカテゴリがあるが `status == "completed"`＝部分フィルタ＝
        OK＋注記（`_chat_filter_verdict` と同じ判断）。
      - `filtered: true` のカテゴリがあり `completed` でもない＝NG。
    """
    if status == "incomplete":
        reason = str((resp.get("incomplete_details") or {}).get("reason") or "")
        if _content_filter_note(reason):
            return True, f"［コンテンツフィルタ＝incomplete_reason={reason}］"
    if _has_filtered_category(resp):
        if status == "completed":
            return False, "［一部カテゴリがコンテンツフィルタで検知されましたが、status=completed です＝OK扱い］"
        return True, "［コンテンツフィルタの疑い＝filtered:true のカテゴリがあります］"
    return False, None


def _http_error_detail(exc: urllib.error.HTTPError, secret: str) -> str:
    try:
        raw = exc.read()
    except Exception:
        raw = b""
    text = _scrub(raw.decode("utf-8", "replace"), secret)
    msg = text
    try:
        payload = json.loads(text)
        err = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(err, dict):
            parts = [str(err.get("message") or "")]
            if err.get("code"):
                parts.append(f"code={err['code']}")
            if err.get("type"):
                parts.append(f"type={err['type']}")
            msg = " ".join(p for p in parts if p)
    except (ValueError, TypeError):
        pass
    detail = f"HTTP {exc.code} {_short(msg, 220)}"
    note = _content_filter_note(text)
    return f"{detail} {note}" if note else detail


def _do_post(path: str, body: dict, api_key: str) -> tuple[bool, object]:
    """`llm.openai_url`/`llm.openai_headers`/`llm.openai_post_json`（本番コード）で1回だけ POST する。

    共通の例外処理だけをここに集約する（HTTP の組み立て自体は `llm.py` に委譲＝再実装しない）。
    成功時は `(True, レスポンスdict)`、失敗時は `(False, 表示用の詳細文字列)` を返す。
    """
    try:
        url = llm.openai_url(path)
    except ValueError as e:
        return False, f"設定不正（OPENAI_BASE_URL）: {e}"
    try:
        resp = llm.openai_post_json(url, llm.openai_headers(api_key), body, _TIMEOUT)
        return True, resp
    except urllib.error.HTTPError as e:
        return False, _http_error_detail(e, api_key)
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
        return False, f"{type(e).__name__}: {_scrub(str(e), api_key)}"


# ---- ロガー捕捉（embeddings 検査の診断用。sherpa.embeddings._log_embed_failure は既に
#      status/error_type/error_code だけを安全に抜き出して warning ログに出す実装になっている
#      ＝そのログ文言を捕まえて使う方が、自前でエラー本文を再解析するより安全（再実装回避）） ----

def _capture_sherpa_warnings():
    logger = logging.getLogger("sherpa")
    records: list[str] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    handler = _Collector()
    handler.setLevel(logging.WARNING)
    prev_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    return handler, records, prev_level, logger


# ---- ⑦ 画像入力用: 疎通確認テキストが書かれた小さな PNG を作る ----

def _make_test_png(width: int = 200, height: int = 80) -> bytes:
    """Pillow があれば文字入りの画像を、無ければ最小の PNG バイト列を組む（外部ファイル不要）。"""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return _make_minimal_png(width, height)
    img = Image.new("RGB", (width, height), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle([2, 2, width - 3, height - 3], outline=(0, 0, 0))
    draw.text((14, height // 2 - 8), "SHERPA TEST 42", fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_minimal_png(width: int, height: int) -> bytes:
    """Pillow 不在時のフォールバック（文字は描かない・決定的なグラデーションのみ）。"""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)   # 8bit深度・color type 2=RGB
    raw = bytearray()
    for y in range(height):
        raw.append(0)   # フィルタなし
        for x in range(width):
            raw += bytes(((x * 3) % 256, (y * 5) % 256, 180))
    idat = zlib.compress(bytes(raw), 9)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


# ---- 検査① 設定の解決（通信なし） ----

def _resolve_config(model: str) -> dict:
    from sherpa import model_catalog
    base = llm.openai_base_url()
    cfg = {
        "base_url": llm._redact_url_for_error(base) or "（解析できません）",
        "base_url_error": None,
        "endpoint_kind": None,
        "auth_header_keys": None,
        "api_version": llm.openai_api_version() or None,
        "embed_model": model_catalog.resolve_model("openai", "embed", None) or "text-embedding-3-small",
        "chat_model": model,
        "api_key_present": agent_constructs.is_real_api_key(os.environ.get("OPENAI_API_KEY")),
    }
    try:
        llm.assert_openai_base_url_allowed(base)
    except ValueError as e:
        cfg["base_url_error"] = str(e)
    cfg["endpoint_kind"] = llm.openai_endpoint_kind()
    cfg["auth_header_keys"] = [k for k in llm.openai_headers("x").keys() if k != "Content-Type"]
    return cfg


def _check_config(cfg: dict, model: str, api_key: str) -> tuple[bool, str]:
    if cfg["base_url_error"]:
        return False, f"OPENAI_BASE_URL が不正です: {cfg['base_url_error']}"
    return True, (
        f"base_url={cfg['base_url']} kind={cfg['endpoint_kind']} "
        f"auth_header={'/'.join(cfg['auth_header_keys'])} "
        f"api_version={'あり' if cfg['api_version'] else 'なし'} "
        f"embed_model={cfg['embed_model']} chat_model={model} "
        f"OPENAI_API_KEY={'設定あり' if cfg['api_key_present'] else '未設定/プレースホルダ'}")


# ---- 検査② chat/completions（非ストリーム） ----

def _check_chat(cfg: dict, model: str, api_key: str) -> tuple[bool, str]:
    # OpenAIProvider が使うのと同じ本文の形（`_messages()` は system(あれば)+履歴+user を組む
    # 本番メソッド・そのまま呼ぶ＝再実装しない）。gpt-5.5 系は temperature 拒否＝送らない。
    provider = OpenAIProvider(api_key, model)
    body = {"model": model, "max_completion_tokens": 16,
            "messages": provider._messages("Azure OpenAI 疎通確認: 「OK」とだけ日本語で答えてください。")}
    ok, resp = _do_post("chat/completions", body, api_key)
    if not ok:
        return False, resp
    choice = (resp.get("choices") or [{}])[0]
    finish = choice.get("finish_reason")
    content = ((choice.get("message") or {}).get("content") or "").strip()
    usage = resp.get("usage") or {}
    detail = (f"model={resp.get('model')} finish_reason={finish} content={_short(content, 60)!r} "
              f"usage(prompt={usage.get('prompt_tokens')},completion={usage.get('completion_tokens')})")
    blocked, note = _chat_filter_verdict(resp, finish, content)
    if note:
        detail = f"{detail} {note}"
    return not blocked, detail


# ---- 検査③ chat/completions（ストリーム＋usage） ----

def _check_stream(cfg: dict, model: str, api_key: str) -> tuple[bool, str]:
    # `OpenAIProvider._stream` を直接呼ぶ（本番のストリーミング実装そのもの・Ctx を組む必要が
    # ないよう `_GenProvider` 自身が「`run()` を経由せず `_messages`/`_stream` を直接叩くテスト向けの
    # 安全なフォールバック」を明記している＝sherpa/providers/base.py のクラス docstring 参照）。
    # 本番と同じく stream_options.include_usage を送る＝Azure がこれを拒否するなら実装側の対応が要る。
    provider = OpenAIProvider(api_key, model)
    try:
        chunks = list(provider._stream("次の1語だけを日本語で答えてください: OK"))
    except urllib.error.HTTPError as e:
        detail = _http_error_detail(e, api_key)
        if e.code == 400:
            detail += "（実装側の対応が必要な可能性: Azure が stream_options.include_usage を拒否している場合）"
        return False, detail
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
        return False, f"{type(e).__name__}: {_scrub(str(e), api_key)}"
    text = "".join(chunks).strip()
    usage = provider._last_usage
    note = _content_filter_note(text)
    detail = f"応答={_short(text, 60)!r} usage_chunk={'あり' if usage else 'なし'}"
    if usage:
        detail += f"（input={usage['input_tokens']} output={usage['output_tokens']}）"
    if note:
        return False, f"{detail} {note}"
    if not text and not usage:
        return False, f"{detail}（本文もusageチャンクも空＝応答なし）"
    return True, detail


# ---- 検査④ tool calling ----

def _check_tools(cfg: dict, model: str, api_key: str) -> tuple[bool, str]:
    # 本番のツール定義（agentic_search.openai_tools）をそのまま渡す。ツールは実行しない
    # （1往復で tool_calls が返るかだけを見る）。
    tools = agentic_search.openai_tools(with_es=False, with_graph=False, can_ask=False)
    body = {"model": model, "max_completion_tokens": 300, "tool_choice": "auto", "tools": tools,
            "messages": [
                {"role": "system", "content": "あなたは社内資料を検索するアシスタントです。"},
                {"role": "user", "content": "list_docs ツールを使って、資料の一覧を確認してください。"},
            ]}
    ok, resp = _do_post("chat/completions", body, api_key)
    if not ok:
        return False, resp
    choice = (resp.get("choices") or [{}])[0]
    calls = (choice.get("message") or {}).get("tool_calls") or []
    if calls:
        names = ", ".join(c.get("function", {}).get("name", "?") for c in calls)
        return True, f"tool_calls: {names}"
    content = ((choice.get("message") or {}).get("content") or "").strip()
    return False, f"tool_calls が返りませんでした（応答: {_short(content, 80)!r}）"


# ---- 検査⑤ embeddings ----

def _check_embeddings(cfg: dict, model: str, api_key: str) -> tuple[bool, str]:
    if os.environ.get("SHERPA_DISABLE_EMBED"):
        return False, "SHERPA_DISABLE_EMBED が設定されているため embeddings 経路が無効化されています"
    econf = embeddings.cfg({"openai_api_key": api_key} if api_key else None)
    if not econf or econf.get("provider") != "openai":
        return False, "OPENAI_API_KEY が未設定のため embeddings 設定を解決できません（provider=openai を選べません）"
    handler, records, prev_level, logger = _capture_sherpa_warnings()
    try:
        # `embeddings.embed()`（公開ラッパー）は次元不一致のベクトルを `is_valid_vector` で黙って
        # 捨てて None を返す（sherpa/embeddings.py 参照）＝実際に何次元返ってきたかを表示できない。
        # 同モジュールの `_embed_batch` を直接呼び、生のベクトル長をこちらで見る
        # （HTTP 呼び出し自体は `embed()` と全く同じ経路・再実装ではない）。
        vecs = embeddings._embed_batch(["Azure OpenAI 疎通確認用のテキストです。"], econf)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)
    if not vecs:
        why = records[-1] if records else "埋め込み取得に失敗しました（詳細不明）。"
        return False, f"モデル/デプロイ={econf['model']}: {why}"
    dim = len(vecs[0])
    ok = dim == 1536
    detail = f"モデル/デプロイ={econf['model']} 次元={dim}"
    if not ok:
        detail += "（1536 以外＝ES 索引契約と不一致。OPENAI_EMBED_MODEL を確認してください）"
    return ok, detail


# ---- 検査⑥ Responses API ----

def _check_responses(cfg: dict, model: str, api_key: str) -> tuple[bool, str]:
    body = {"model": model, "max_output_tokens": 16,
            "input": "Azure OpenAI 疎通確認: 「OK」とだけ日本語で答えてください。"}
    ok, resp = _do_post("responses", body, api_key)
    if not ok:
        return False, resp
    status = resp.get("status")
    detail = f"model={resp.get('model')} status={status}"
    if resp.get("incomplete_details"):
        detail += f" incomplete_reason={resp['incomplete_details'].get('reason')}"
    if resp.get("error"):
        detail += f" error={_short(resp['error'], 120)}"
    blocked, note = _responses_filter_verdict(resp, status)
    if note:
        detail = f"{detail} {note}"
    if blocked:
        return False, detail
    return status == "completed", detail


# ---- 検査⑦ 画像入力（VLM） ----

def _check_vision(cfg: dict, model: str, api_key: str) -> tuple[bool, str]:
    # sherpa/ingest/arms/vision_arm.py::_read_openai と同じ本文の形（image_url に base64
    # data URL）。`_read_openai` 自体は system_settings.vlm.cloud_allowed の検査・ファイル読込を
    # 前提にしており本検査の対象外（Azure 疎通そのものを見たいので同じ形だけを借りる）。
    png = _make_test_png()
    data_url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    body = {"model": model, "max_completion_tokens": 80,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "この画像に書かれている文字をそのまま書き起こしてください。"},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]}]}
    ok, resp = _do_post("chat/completions", body, api_key)
    if not ok:
        return False, resp
    choice = (resp.get("choices") or [{}])[0]
    content = ((choice.get("message") or {}).get("content") or "").strip()
    finish = choice.get("finish_reason")
    detail = f"model={resp.get('model')} finish_reason={finish} 応答={_short(content, 80)!r}"
    note = _content_filter_note(finish or "")
    if note:
        return False, f"{detail} {note}"
    return bool(content), detail


# ---- 検査⑧ Codex CLI 経由 ----

def _check_codex(cfg: dict, model: str, api_key: str) -> tuple[bool, str]:
    if not shutil.which("codex"):
        return False, "Codex CLI（codex）が見つかりません（未導入・閉域キットの tools/codex か npm で導入）"
    if cfg["endpoint_kind"] == "openai":
        return False, ("接続先が OpenAI 本家のままです（この検査は Azure 等の代替接続先向け・"
                       "既定の Codex(OpenAI) 構成は auth.json を使うためここでは検査しません）。"
                       "--env-file で OPENAI_BASE_URL を Azure に設定してから --codex を付けてください。")
    if not agent_constructs.is_real_api_key(api_key):
        return False, "OPENAI_API_KEY が未設定/プレースホルダです（Azure 等は env_key 経由で子プロセスへ渡します）"

    with tempfile.TemporaryDirectory(prefix="azure-smoke-codex-") as tmp:
        tmp_path = Path(tmp)
        codex_home = tmp_path / "codex-home"
        authoring = tmp_path / "authoring"
        tmpdir = tmp_path / "tmp"
        authoring.mkdir(parents=True, exist_ok=True)
        tmpdir.mkdir(parents=True, exist_ok=True)
        last_message_path = tmpdir / "last-message.txt"

        # sandbox._write_codex_authoring_config は「実 ~/.codex/auth.json があれば読取専用で
        # symlink する」処理を内部で行う（CODEX_HOME 環境変数から実 home を解決）。Azure 等の
        # カスタムプロバイダは env_key（OPENAI_API_KEY）で認証するため auth.json は不要＝実
        # ~/.codex には一切触れない（制約）。呼び出し中だけ CODEX_HOME を「存在しない」一時パスに
        # 退避させ、symlink 元の解決先が無いことにする（実ファイルへの exists() すら発生させない）。
        saved_codex_home = os.environ.get("CODEX_HOME")
        try:
            os.environ["CODEX_HOME"] = str(tmp_path / "no-such-real-home")
            codex_sandbox._write_codex_authoring_config(
                codex_home, [], "low", False, "", None, web_search_enabled=False)
        except Exception as e:
            return False, f"config.toml 生成に失敗: {type(e).__name__}: {_scrub(str(e), api_key)}"
        finally:
            if saved_codex_home is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = saved_codex_home

        popen_env = codex_sandbox._codex_clean_env(codex_home, authoring, tmpdir, openai_api_key=api_key)
        argv = ["codex", "exec", "--json", "--strict-config", "--skip-git-repo-check", "--ephemeral",
                "-o", str(last_message_path), "-C", str(authoring), "-m", model,
                "-c", "model_reasoning_effort=low",
                "次の1語だけを日本語で答えてください: OK"]
        try:
            proc = subprocess.run(argv, env=popen_env, cwd=str(authoring),
                                  capture_output=True, text=True, timeout=_CODEX_TIMEOUT)
        except subprocess.TimeoutExpired:
            return False, f"codex exec がタイムアウトしました（{_CODEX_TIMEOUT}秒）"
        except OSError as e:
            return False, f"codex exec 起動失敗: {type(e).__name__}: {e}"

        message = ""
        if last_message_path.exists():
            message = last_message_path.read_text(encoding="utf-8", errors="replace").strip()
        if proc.returncode == 0 and message:
            note = _content_filter_note(message)
            detail = f"応答={_short(message, 80)!r}"
            if note:
                return False, f"{detail} {note}"
            return True, detail
        tail = _scrub((proc.stdout or "") + (proc.stderr or ""), api_key)
        return False, f"exit={proc.returncode} last_message={'あり' if message else '空/無し'} 出力末尾: {_short(tail[-300:], 300)}"


_RUN = {
    "config": _check_config,
    "chat": _check_chat,
    "stream": _check_stream,
    "tools": _check_tools,
    "embeddings": _check_embeddings,
    "responses": _check_responses,
    "vision": _check_vision,
    "codex": _check_codex,
}


# ---- 検査の選択（--only / --skip / --vision / --codex） ----

def _parse_names(raw: str, flag: str) -> set[str]:
    names = {n.strip() for n in raw.split(",") if n.strip()}
    unknown = names - set(_ALL_CHECKS)
    if unknown:
        print(f"{flag}: 未知の検査名です: {', '.join(sorted(unknown))}"
              f"（有効な名前: {', '.join(_ALL_CHECKS)}）", file=sys.stderr)
        sys.exit(2)
    return names


def _select_checks(args: argparse.Namespace) -> list[str]:
    if args.only:
        names = _parse_names(args.only, "--only")
        selected = [k for k in _ALL_CHECKS if k in names]
    else:
        selected = [k for k in _ALL_CHECKS if k not in _GATED_BY_FLAG]
        if args.vision:
            selected.append("vision")
        if args.codex:
            selected.append("codex")
        selected = [k for k in _ALL_CHECKS if k in selected]   # 表示順を _ALL_CHECKS に揃える
    if args.skip:
        skip_names = _parse_names(args.skip, "--skip")
        selected = [k for k in selected if k not in skip_names]
    return selected


# ---- 結果の型・表示 ----

@dataclass
class CheckResult:
    key: str
    label: str
    required: bool
    skipped: bool
    ok: bool | None
    detail: str
    elapsed: float


def _print_table(results: list[CheckResult]) -> None:
    print("\n== 結果 ==")
    for r in results:
        mark = "SKIP" if r.skipped else ("OK  " if r.ok else "NG  ")
        req = "必須" if r.required else "任意"
        print(f"[{mark}] ({req}) {r.label}  {r.elapsed:.1f}s  {r.detail}")


def _print_next_steps(results: list[CheckResult]) -> None:
    failed = [r for r in results if not r.skipped and r.ok is False]
    if not failed:
        return
    print("\n== 次にやること ==")
    for r in failed:
        print(f"- {r.label}: {_NEXT_STEPS.get(r.key, '結果の詳細を確認してください。')}")


def _exit_code(results: list[CheckResult]) -> int:
    executed = [r for r in results if not r.skipped]
    if any(r.ok is False and r.required for r in executed):
        return 1
    if any(r.ok is False and not r.required for r in executed):
        return 2
    return 0


def _print_json(cfg: dict, results: list[CheckResult], exit_code: int) -> None:
    payload = {
        "config": dict(cfg),
        "checks": [{"key": r.key, "label": r.label, "required": r.required, "skipped": r.skipped,
                   "ok": r.ok, "detail": r.detail, "elapsed_sec": round(r.elapsed, 3)} for r in results],
        "exit_code": exit_code,
    }
    print("\n== JSON ==")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _host_of(url: str) -> str:
    from urllib.parse import urlparse
    try:
        return urlparse(url).hostname or url
    except ValueError:
        return url


def _print_dry_run(cfg: dict, selected: list[str], model: str) -> None:
    print("== dry-run（通信しません）==")
    print(f"  base_url        : {cfg['base_url']}")
    print(f"  endpoint_kind   : {cfg['endpoint_kind']}")
    print(f"  auth_header     : {'/'.join(cfg['auth_header_keys'] or [])}")
    print(f"  api_version     : {cfg['api_version'] or '(なし)'}")
    print(f"  embed_model     : {cfg['embed_model']}")
    print(f"  chat_model      : {model}")
    print(f"  OPENAI_API_KEY  : {'設定あり' if cfg['api_key_present'] else '未設定/プレースホルダ'}")
    if cfg["base_url_error"]:
        print(f"  ! OPENAI_BASE_URL が不正です: {cfg['base_url_error']}")
    print("\n  これから叩く URL（選択された検査分・実際には叩きません）:")
    grouped: dict[str, list[str]] = {}
    for key in selected:
        if key == "config":
            continue
        if key == "codex":
            grouped.setdefault(f"(codex exec 経由・接続先ホスト={_host_of(cfg['base_url'])})", []).append(key)
            continue
        path = _PATH_BY_CHECK.get(key)
        if path is None:
            continue
        try:
            url = llm.openai_url(path)
        except ValueError as e:
            url = f"(不正: {e})"
        grouped.setdefault(url, []).append(key)
    if not grouped:
        print("    (対象なし)")
    for url, keys in grouped.items():
        print(f"    - {url}   [{', '.join(keys)}]")


def _confirm(yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        print("非対話環境です。--yes を指定してください。", file=sys.stderr)
        return False
    try:
        ans = input("続行しますか？ 課金が発生します。 [y/N]: ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="azure_smoke.py",
        description=(
            "Azure OpenAI（または他の OpenAI 互換エンドポイント）への実疎通を、Sherpa 本番コード"
            "（sherpa.llm / sherpa.embeddings / sherpa.providers.openai.OpenAIProvider / "
            "sherpa.agentic_search / sherpa.providers.codex.sandbox）をそのまま呼んで確認します。"),
        epilog=_HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--env-file", default=".env", metavar="PATH",
                        help="KEY=VALUE 形式の env ファイル（既定 .env）。未設定の変数だけ取り込みます"
                             "（既に環境にある変数は上書きしません）。")
    parser.add_argument("--dry-run", action="store_true",
                        help="通信せず、解決した接続設定とこれから叩く URL の一覧だけを表示します。")
    parser.add_argument("--only", metavar="NAME[,NAME...]",
                        help="指定した検査だけを実行します（既定/--vision/--codex より優先）。"
                             f"有効な名前: {', '.join(_ALL_CHECKS)}")
    parser.add_argument("--skip", metavar="NAME[,NAME...]",
                        help="指定した検査を除外します（--only の後に適用）。")
    parser.add_argument("--vision", action="store_true",
                        help="画像入力（VLM）の検査を有効にします（既定OFF・重い/条件付き）。")
    parser.add_argument("--codex", action="store_true",
                        help="Codex CLI 経由の検査を有効にします（既定OFF・重い/条件付き）。")
    parser.add_argument("--model", metavar="NAME",
                        help="チャット/Responses/Codex で使うモデル/デプロイ名"
                             "（既定は env OPENAI_CHAT_MODEL、それも無ければ 'gpt-5.5'）。"
                             "Azure 等では必ず実際のデプロイ名を指定してください。")
    parser.add_argument("--json", dest="json_out", action="store_true",
                        help="結果を JSON でも出力します（受け入れ確認の記録用）。")
    parser.add_argument("--yes", action="store_true",
                        help="実 API を叩く前の確認プロンプトを省略します（非対話環境では必須）。")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    _load_env_file(Path(args.env_file))
    candidate_error = _seed_settings_from_parsed_env()
    if candidate_error is not None:
        # 本番と同じ検証（明示 kind 優先・4項目原子検証・kind!=openai で base_url 必須・未知
        # kind/auth_header 拒否）が通信前に落ちた＝実 API を一切叩かず中断する（不正な候補のまま
        # probe すると、原因不明な 404/認証エラーとして誤診断されるため）。
        print(f"接続設定が不正です（通信前に中断しました）: {candidate_error}", file=sys.stderr)
        return 2

    model = args.model or (os.environ.get("OPENAI_CHAT_MODEL") or "").strip() or "gpt-5.5"
    api_key = os.environ.get("OPENAI_API_KEY") or ""

    cfg = _resolve_config(model)
    selected = _select_checks(args)

    if args.dry_run:
        _print_dry_run(cfg, selected, model)
        return 0

    if model == "gpt-5.5" and cfg["endpoint_kind"] != "openai":
        print("警告: 接続先が OpenAI 本家以外なのに、モデル欄が既定の 'gpt-5.5' のままです。"
              "--model か env OPENAI_CHAT_MODEL に、Azure 側のデプロイ名を指定してください。",
              file=sys.stderr)

    print("これから実 API を叩きます（課金されます）。")
    if not _confirm(args.yes):
        print("中止しました。", file=sys.stderr)
        return 2

    results: list[CheckResult] = []
    for key in _ALL_CHECKS:
        label = _LABELS[key]
        if key not in selected:
            results.append(CheckResult(key, label, key in _REQUIRED_CHECKS, True, None, "スキップ", 0.0))
            continue
        print(f"・検査中: {label}")
        t0 = time.monotonic()
        try:
            ok, detail = _RUN[key](cfg, model, api_key)
        except Exception as e:   # 想定外の例外でもスクリプト自体は完走させる
            ok, detail = False, f"想定外の例外: {type(e).__name__}: {_scrub(str(e), api_key)}"
        elapsed = time.monotonic() - t0
        print(("OK: " if ok else "NG: ") + f"{label} ({elapsed:.1f}s) {detail}")
        results.append(CheckResult(key, label, key in _REQUIRED_CHECKS, False, ok, detail, elapsed))

    _print_table(results)
    _print_next_steps(results)
    exit_code = _exit_code(results)
    if args.json_out:
        _print_json(cfg, results, exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
