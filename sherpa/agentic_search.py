"""エージェント検索（インデックス無し・LLM が grep ツールを反復呼び出し）。

参考思想（zenn: agentic search no-index）: 事前インデックスを作らず、LLM に検索ツールを渡して
**ripgrep_search で当たり → read_around で精読 → クエリ修正 → 反復**させる。Sherpa 既存の
`grep_tool.grep_search`（索引なし全文 grep・world ツリー＋Office派生MD）を土台に、OpenAI/Gemini/
Ollama の function-calling で回す。範囲は **選択中の資料フォルダ＋scope のみ**・read-only・本文テキストのみ送信。

このモジュールは LLM プロバイダに依存しない（HTTP は `_post`・テストで差し替え可）。各 loop は
`{"node": <思考ノード>}` を yield しつつ、最後に `{"final": <回答>, "docs": <参照 doc_id 集合>}` を yield。
"""
from __future__ import annotations

import errno
import json
import logging
import math
import os
import re
import socket
import ssl
import stat
import threading
import time
import urllib.error
from pathlib import Path

from . import citations, es_index, exec_event, grep_tool, llm, worlds
from . import layer as layer_mod
from . import scope as scope_mod
from . import tools_pref as tools_pref_mod
from .ingest import importance, text_kind
from .ingest.analyzers import registry as _analyzer_registry
from .safe_open import open_file_nofollow_walk as _open_file_nofollow_walk   # TOCTOU耐性のファイルopen（実装は safe_open.py・ext_api.py と共用）

# `research_service.py`/`ext_api.py`等と同じ共有ロガー（新しいロガーを増やさない・単一の真実源）。
_log = logging.getLogger("sherpa")


def _header_secret(headers: dict) -> str | None:
    """`headers`（`llm.openai_headers()` が組み立てた認証ヘッダ）から実際に使ったキー値を
    取り出す（ログのマスク処理へ渡す用・`api-key`/`Authorization: Bearer` の両方式に対応・
    Ollama 等キー無し接続では None）。"""
    v = headers.get("api-key")
    if isinstance(v, str) and v:
        return v
    auth = headers.get("Authorization")
    if isinstance(auth, str) and auth.startswith("Bearer "):
        return auth[len("Bearer "):] or None
    return None

MAX_TURNS = int(os.environ.get("SHERPA_AGENTIC_MAX_TURNS", "12"))  # 反復上限（コスト/レイテンシ境界）
# 上限に達したときに「集めた材料だけで答えさせる」最終合成の指示（ツールを渡さずに1回だけ呼ぶ）。
# 以前は上限到達で空回答を返し、呼び出し元がそれまでの資料・引用を全部捨てて単発 grep へ落ちていた
# （実測 2026-08-15: 6ターン検索した結果を破棄し、入力 353 tokens で回答していた）。
_FINAL_SYNTHESIS = (
    "調査の上限に達しました。**これ以上ツールは使えません**。"
    "ここまでに取得した内容だけを根拠に、日本語で簡潔（2〜4文）に回答してください。"
    "確認できたことと、確認できなかったことを分けて書く。"
    "取得した内容に無いことは書かない（推測しない）。"
)
# EXT-3（拡張設計 §3.5）: 評価フェーズが sufficient と判定したときの最終合成指示。上限到達時の
# `_FINAL_SYNTHESIS`（「上限に達した」）とは意味が異なるため文言を分ける（sufficient を上限到達と
# 誤表示しない）。
_FINAL_SYNTHESIS_SUFFICIENT = (
    "十分な根拠が集まりました。ここまでに取得した内容だけを根拠に、日本語で簡潔（2〜4文）に"
    "回答してください。確認できたことと、確認できなかったことを分けて書く。"
    "取得した内容に無いことは書かない（推測しない）。"
)


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    """security-limit 系 env の整数解析（secRV FIX-W・2026-07-19・負値/巨大値対策）。

    負値をそのままスライス上限に使うと `calls[:-1]`／`b[:-1]` のように**反転**して
    「末尾1件を除いて全部通す」＝上限の実質無効化になる（`SHERPA_AGENTIC_MAX_TOOLS_PER_TURN=-1`
    で 1000 件中 999 件実行できてしまっていた）。範囲 [lo, hi] 外・非整数は全て安全な既定値へ戻す
    （起動は継続＝運用者の誤設定で機能を止めない・巨大な正値も hard cap `hi` で抑える）。

    既定値自体も呼び出し時に [lo, hi] へクランプする（動的既定を渡す呼び出し元が hard cap を
    素通りしないようにする安全弁）。"""
    default = max(lo, min(default, hi))
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return v if lo <= v <= hi else default
# secRV MED-3（2026-07-18・DoS/コスト増幅）: `MAX_TURNS` は LLM 応答ラウンド数だけを制限し、1応答内で
# モデルが返す tool_calls の**個数**は無制限に実行していた（no-hit grep は毎回 world 全走査＝
# 1応答に大量のツール呼び出しを積むだけで実処理量を増幅できた）。1応答あたりの実行数上限を独立に
# 設ける（既定16は通常のツール呼び出し数を十分上回るため正常系には影響しない）。
# FIX-W: 負値でスライスが反転し上限が無効化されるため `_env_int` で範囲検証（hard cap 256）。
MAX_TOOLS_PER_TURN = _env_int("SHERPA_AGENTIC_MAX_TOOLS_PER_TURN", 16, 1, 256)
# grep/es_search 1回あたりのヒット数上限。精度優先で広げるほど根拠を落としにくくなる代わりに
# LLM への送信トークンが増える。
MAX_HITS = _env_int("SHERPA_GREP_MAX_HITS", 30, 1, 1000)
# `MAX_HITS` の env-parse hi 引数と同じ値。調べる深さ（`depth_profile.scaled_ratio`）が倍率適用後に
# 一度だけ適用する絶対上限として grep/ES 双方に共有する——管理画面の基準値編集（Field 上限まで）と
# 調べる深さ「最大」（×2）の組み合わせで無制限に伸びるのを防ぐ。
MAX_HITS_ABS_MAX = 1000
# read_around の精読窓（行数）。広げるほど1回の読み込みで前後文脈を多く拾える。read_around 本体の
# LLM 入力窓ハード上限（下記 `window = max(1, min(window, max(200, READ_WINDOW)))`）はこの値が
# 200 を超えたときだけ追随する（既定 200 は後退させない）。
READ_WINDOW = _env_int("SHERPA_READ_WINDOW", 40, 10, 400)
# `READ_WINDOW` の env-parse hi 引数と同じ値。`depth_profile.scaled_ratio` の `abs_max` として使う
# （MAX_HITS_ABS_MAX と同じ理由）。
READ_WINDOW_ABS_MAX = 400
_OFFICE_MD = {".docx", ".xlsx", ".pptx", ".pdf", ".doc", ".xls", ".ppt",
              # ラスタ画像（A3・OCR アーム）も本文は派生MD側（`image.png.md`）にある。OCR 無効なら derived に
              # 画像 .md は存在しないので、加えても実害はなく grep_search（derived md/ を直接見る）と read_around
              # が一致する（office_md.IMAGE_EXT が真実源・W0 RV High の非対称を画像でも防ぐ）。
              ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff"}   # 本文は派生MD側にある
# ⚠ 旧形式（.doc/.xls/.ppt）は legacy_backend（W0）が前段変換した OOXML を①アームが MD化する。
# 解決規約は新形式と同じ **原本 rel + ".md"**（office_md.build_derived が出力名を原本 rel に揃えている）ので
# ここでの分岐は不要＝ _OFFICE_MD に加えるだけで grep_search（derived md/ を直接見る）と read_around が一致する
# （W0 RV High: 追加前は grep はヒットするが read_around が拒否＝精読不可という非対称があった）。
# read_around で読める本文種別だけ（.env 等の秘匿ファイルを LLM に読ませない・RV BLOCKER）。
# ソース原文（コード）分はアナライザ登録簿が単一の真実源（§2.4）。
# 軽量テキスト枠（`ingest.text_kind`）の第1段拡張子マップ（CODE_EXT/DOCUMENT_EXT）も対象に含める
# ——`.env`/`.key`（`text_kind.SENSITIVE_EXT`／ドットファイル名判定）は元々この2集合に含まれず、
# `classify_document()`（下の `_safe_doc_path`）が最終判定でも秘匿ファイル・意味層内部制御ファイル
# （`worlds.is_semantic_control_path`）を対象外へ倒すため、ここに加えても RV BLOCKER の意図は
# 破らない。grep_search（`grep_tool._TEXT_EXT` も同じ集合を追加済み）と read_around の対称性を保つ
# （追加しないと「grep はヒットするが read_around が拒否」という W0 RV High と同型の非対称が生じる）。
_READABLE_EXT = ({".md", ".markdown", ".txt"} | _analyzer_registry.registered_extensions() | _OFFICE_MD
                | text_kind.CODE_EXT | text_kind.DOCUMENT_EXT)

# tool result（外部 LLM へ渡る）から明らかな秘密を伏せる（RV HIGH・多層防御）。
_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{6,}"
    r"|-----BEGIN[^-]+PRIVATE KEY-----[\s\S]*?-----END[^-]+PRIVATE KEY-----)")
_KV_SECRET_RE = re.compile(r"(?i)\b(pass(?:word|wd)?|secret|api[_-]?key|token|authorization)\b(\s*[=:]\s*)(\S+)")


def _redact(text: str) -> str:
    t = _SECRET_RE.sub("[REDACTED]", text or "")
    return _KV_SECRET_RE.sub(r"\1\2[REDACTED]", t)


# secRV MED-B（2026-07-18・DoS/メモリ増幅対策）: `read_around` は window（行数）でしか出力を絞らず、
# 単一行が巨大（例: 10MB の1行だけの文書）だと行数上限が実質無意味＝返却バイト量が無制限になる
# （1ターン内で `SHERPA_AGENTIC_MAX_TOOLS_PER_TURN` 回呼ばれると履歴/SSE/次ターンの LLM 要求へ
# 複製される総量が跳ね上がる）。(a) 返却テキストの UTF-8 バイト上限で切り詰める（既存の
# grep ヒットクリップ `[:500]` と同じ流儀）。(b) `Path.read_text()`（ファイル全体を
# 一括ロード）ではなく、生バイトを `_READ_AROUND_FILE_CAP_BYTES` までに制限して読む（巨大な単一行
# ファイルでも読み込み自体が無制限に増幅しない）。
# FIX-W: 負値でクリップが反転するため `_env_int` で範囲検証（hard cap はディスク読み上限と同じ 8MiB）。
# BUDGET-1（§3.4・2026-09-03 裁定）: 既定は精度優先（憲法1条「アプリは性能を黙って下げない」）——
# 旧既定 65536/1048576 は secRV のサーバメモリ対策の名残で、read 側のストリーミング化により
# 役目を終えた。管理画面（`agentic_budget.per_result`）へ昇格済み（UI(DB)が唯一の真実源）のため
# env フォールバックは持たない（ENV-CLEAN・2026-09-03）——ここでの値はコード既定として
# `resolve_tool_result_budgets()` の settings 未設定時フォールバックにそのまま使う（settings 段は
# 下の resolver が1段重ねる）。
TOOL_RESULT_MAX_BYTES = 262144
# 1 run（1回の agentic ループ全体＝`openai_style`/`gemini`/`anthropic_style` の1呼び出し）で許容する
# tool-result 累計バイト上限（secRV MED-B (c)・3 dialect 全てで使う）。超過時は固定エラーで run を
# 打ち切る（fail-closed）。
# BUDGET-1: 既定は per-call 既定の16倍（旧既定と同じ比率をコード既定として固定するだけで、settings で
# per-call だけ変えても total には連動しない——2キーは独立に解決する。§3.4「即時」段の値と一致）。
# 管理画面（`agentic_budget.total`）へ昇格済みのため env フォールバックは持たない（ENV-CLEAN）。
TOOL_RESULT_MAX_TOTAL_BYTES = 4 * 1024 * 1024


def _clamped_setting_int(raw, lo: int, hi: int) -> int | None:
    """system_settings の生値を整数として検証する（型不正・範囲外は None＝呼び出し側が
    コード既定へ倒す）。`_env_int` の env 側検証と同じ lo/hi 契約を settings 側にも適用する。"""
    if raw is None:
        return None
    try:
        iv = int(raw)
    except (TypeError, ValueError):
        return None
    return iv if lo <= iv <= hi else None


def _window_derived_min(base: int, system_settings: dict | None, provider: str | None,
                        model: str | None, ollama_base_url: str | None, anthropic_client) -> int:
    """BUDGET-2（§3.4・2026-09-03 裁定・min() 方式）: `base`（BUDGET-1 の解決値）と「選択中モデルの
    窓由来の上限」の小さい方を返す。`provider`/`model` 省略（既定 None）時は窓連動を一切行わず
    `base` をそのまま返す＝既存呼び出し元（`provider`/`model` を渡さない）は byte-identical。
    窓が不明（登録値/API/シードのどれにも無い）なときも同様に `base` のまま
    （BUDGET-2 §3.4「限界に当たったら黙らない」は満たすが値自体は後退しない＝退行にならない）。
    大窓が判明しても `base` を超えて増やすことはしない（min() の対称性がそのまま「支出の自動拡大
    はしない」を保証する）。"""
    if provider is None or model is None:
        return base
    from . import model_windows
    tokens, _source = model_windows.resolve_window_tokens(
        provider, model, system_settings=system_settings,
        ollama_base_url=ollama_base_url, anthropic_client=anthropic_client)
    if tokens is None:
        return base
    return min(base, model_windows.derive_window_bytes(tokens))


def effective_tool_result_max_bytes(system_settings: dict | None = None, *, provider: str | None = None,
                                    model: str | None = None, ollama_base_url: str | None = None,
                                    anthropic_client=None) -> int:
    """ツール結果1件あたりのバイト予算の実効値（system_settings > コード既定・BUDGET-1・
    §3.4）。`system_settings` 省略時は `store.get_system_settings()` を呼ぶ（読めない/未設定は
    コード既定 `TOOL_RESULT_MAX_BYTES` へ倒す・fail-safe）。

    `provider`/`model`/`ollama_base_url`/`anthropic_client`（すべて省略可・BUDGET-2・§3.4）:
    渡すと、上の解決値と「窓由来の上限」の min() を最終的な実効値にする（`_window_derived_min`
    docstring 参照）。省略時（既定）は BUDGET-1 のみの結果＝byte-identical。"""
    sysset = system_settings
    if sysset is None:
        try:
            from . import store
            sysset = store.get_system_settings()
        except Exception:
            sysset = {}
    v = _clamped_setting_int(sysset.get("agentic_budget_per_result"), 1024, 8 * 1024 * 1024)
    base = v if v is not None else TOOL_RESULT_MAX_BYTES
    return _window_derived_min(base, sysset, provider, model, ollama_base_url, anthropic_client)


def effective_tool_result_max_total_bytes(system_settings: dict | None = None, *,
                                          provider: str | None = None, model: str | None = None,
                                          ollama_base_url: str | None = None,
                                          anthropic_client=None) -> int:
    """1 run 累計のツール結果バイト予算の実効値（`effective_tool_result_max_bytes` と同型・
    BUDGET-2 の追加引数も同じ意味）。"""
    sysset = system_settings
    if sysset is None:
        try:
            from . import store
            sysset = store.get_system_settings()
        except Exception:
            sysset = {}
    v = _clamped_setting_int(sysset.get("agentic_budget_total"), 4096, 64 * 1024 * 1024)
    base = v if v is not None else TOOL_RESULT_MAX_TOTAL_BYTES
    return _window_derived_min(base, sysset, provider, model, ollama_base_url, anthropic_client)


def resolve_tool_result_budgets(system_settings: dict | None = None, *, provider: str | None = None,
                                model: str | None = None, ollama_base_url: str | None = None,
                                anthropic_client=None) -> tuple[int, int]:
    """`(1件あたり予算, 1 run 累計予算)`。**run 開始時に1回だけ**呼び、戻り値を run の間ずっと
    使い回す契約（`openai_style`/`anthropic_style`/`gemini` 各関数の先頭・`total_tool_bytes = 0`
    と同じ場所で呼ぶ）——run 途中で admin が設定を変えても当該 run には影響しない（`depth_profile`
    の「会話ターン全体にかかる」snapshot と同じ流儀・累計判定の整合性のため）。`system_settings`
    省略時は内部で1回だけ取得し、2つの解決に使い回す（`get_system_settings()` は短TTLキャッシュ付き
    のため、省略しても DB を都度叩くわけではない）。

    `provider`/`model`（省略可・既定 None＝BUDGET-1 のみ・BUDGET-2・§3.4）: その run の
    メイン頭脳（`openai_style` の `ollama`/`model`・`anthropic_style`/`gemini` の `model`）を渡すと、
    実効予算を「BUDGET-1 の解決値」と「窓由来の上限（選択中モデルの実コンテキスト窓から導く・
    min() 方式）」の小さい方に絞る（小窓モデルへの切替で自動的に縮む・窓が不明/大きい場合は
    BUDGET-1 の値から自動では増えない）。`ollama_base_url`（provider="ollama" のときのみ意味を
    持つ・`model_windows.derive_ollama_base_url` 参照）・`anthropic_client`（`.models.retrieve()`
    を持つ SDK クライアント）はプロバイダAPI照会用（`sherpa/model_windows.py::resolve_window_tokens`
    参照・失敗/未提供は次の解決段へ fail-safe）。サブ頭脳（`_sub_agentic_loop`）の個別対応は将来
    スライス——現状はメイン頭脳の呼び出し元だけがこれらを渡す。"""
    if system_settings is None:
        try:
            from . import store
            system_settings = store.get_system_settings()
        except Exception:
            system_settings = {}
    return (effective_tool_result_max_bytes(system_settings, provider=provider, model=model,
                                            ollama_base_url=ollama_base_url,
                                            anthropic_client=anthropic_client),
            effective_tool_result_max_total_bytes(system_settings, provider=provider, model=model,
                                                  ollama_base_url=ollama_base_url,
                                                  anthropic_client=anthropic_client))
# read_around/read_doc/doc_outline/verify_citation がディスクから読む生バイト数の上限
# （secRV MED-B (b)）。`grep_tool._GREP_FILE_CAP_BYTES` と同じ役割（1ファイルにかける読み取り
# コストの安全弁）——env で個別に変更できるが、既定は grep 側の cap（既定64MiB・2026-09に
# ストリーミング化してメモリ非比例になった際に引き上げ済み）と揃える。揃えないと「grep が
# cap より後ろでヒットを見つけたのに read_doc/read_around がそこを読めない」という食い違いが
# 生まれる（grep 側は行単位のストリーミングでメモリを頭打ちにするが、read_around 側は
# 「1ヒット周辺だけを読む」用途で全量ロードのままでも実害が薄いため、ここでは cap を揃えるだけに
# 留める＝ストリーミング化は本スライスのスコープ外）。
_READ_AROUND_FILE_CAP_BYTES = _env_int(
    "SHERPA_READ_AROUND_FILE_CAP_BYTES", 64 * 1024 * 1024, 65536, 64 * 1024 * 1024)
# read 側（read_around/read_doc/doc_outline）の単一巨大行への安全弁（2026-09・grep_tool の
# `_CappedStreamReader`/`_logical_lines` をそのまま再利用してストリーミング走査する際に効く・
# `grep_tool._GREP_LINE_MAX_BYTES`＝`SHERPA_GREP_LINE_MAX_BYTES` と同じ役割）。cap 系 env
# （`SHERPA_GREP_FILE_CAP_BYTES`/`SHERPA_READ_AROUND_FILE_CAP_BYTES`）が経路ごとに別名になっている
# 流儀に揃え、read 側は独立の env で調整できるようにする（既定値・許容範囲は grep 側と揃える）。
_READ_LINE_MAX_BYTES = _env_int("SHERPA_READ_LINE_MAX_BYTES", 2 * 1024 * 1024, 64 * 1024, 16 * 1024 * 1024)
# `ripgrep_search` の tool result に載せる「打切りで探せていない文書」の件数上限。ツール結果の
# バイト予算（`TOOL_RESULT_MAX_BYTES`）を圧迫しないための安全弁——LLM には「打切りが起きている」
# 事実と代表例が伝われば十分で、全件列挙は要らない（`read_doc` で個別に読みに行ける）。
_TRUNCATED_DOCS_MAX = 20
# 許可外ツール拒否結果に埋める（モデル生成の）ツール名の上限バイト数（secRV FIX-1・2026-07-19）。
# 拒否理由がどのツール名かをモデルへ伝える最小限の情報量で十分＝短い固定長で足りる。
_REJECTED_TOOL_NAME_MAX_BYTES = 32
# 親返し（L4c・§3.3/§3.4）: es_search のヒットを doc_id で束ね、予算内なら rag.md 全文(P3)／
# 領域(P2)を返す。常時 ON（TOGGLE-RM・2026-09-03 でグローバルな系統切替トグル
# `SHERPA_ES_PARENT_RETURN` を撤去）。
# P2（領域）の対象チャンク集合を ES から引く際の1クエリあたりの取得上限（`es_index.
# chunk_ids_for_parent` の `limit`）。region はどのみち byte_cap（予算）で頭打ちになるため、
# ここは「1回のクエリで返す chunk_id の個数」自体の安全弁——ES の既定 `max_result_window`
# （10000）を十分下回る固定値（env 化はしない・§3.4 は新しい env を増やさない方針）。
_PARENT_RETURN_REGION_CHUNKS_MAX = 5000


def _parent_return_enabled() -> bool:
    """常時 True（TOGGLE-RM・2026-09-03: グローバルな系統切替トグル `SHERPA_ES_PARENT_RETURN` を
    撤去し常時ONへ固定・`grep_tool.rag_grep_enabled`/`es_index.rag_es_enabled` と同じ扱い）。既存の
    呼び出し形（`run_tool` の `parent_return_on` 判定）を変えない最小変更として関数自体は残す。"""
    return True


def _clip_utf8_bytes(s: str, max_bytes: int) -> str:
    """UTF-8 エンコード後のバイト数が `max_bytes` を超えないよう `s` を切り詰める（secRV MED-B (a)）。

    マルチバイト文字の境界で分割されても壊れた文字が残らないよう `errors="ignore"` で再デコードする。
    """
    b = s.encode("utf-8")
    if len(b) <= max_bytes:
        return s
    return b[:max_bytes].decode("utf-8", errors="ignore")


# 直列化不能時のフォールバック値（secRV FIX-M2・2026-07-19）。実運用のどんな上限設定
# （既定 64KiB/1MiB）よりも確実に大きい値にし、「測定不能＝上限超過扱い」を機械的に保証する。
_UNMEASURABLE_SIZE = 1 << 40


def _result_byte_size(result) -> int:
    """JSON 化した際の概算 UTF-8 バイト数（secRV MED-B (c)・1 run 累計上限の判定に使う）。

    `run_tool` の戻り値の1つ目（tool result dict）だけでなく、4つ目（`cards` サイドカー・
    `list[dict]`）にも同じ関数を使う（FIX-2・secRV・2026-07-19）。

    レビュー是正（FIX-M2・secRV・2026-07-19・直列化失敗の 0 扱い fail-open）: 以前は
    シリアライズできない要素（bytes・非JSON型・不正 Unicode 等）に対して `0`（無料）を返しており、
    個別上限（`_clip_cards` の `max_bytes`）・累計上限（`TOOL_RESULT_MAX_TOTAL_BYTES`）の両方を
    無条件にすり抜けられた（実測: 1MiB の非JSON値カードが 100 byte 上限で 30 件そのまま採用）。
    是正: 測定不能は「特大（`_UNMEASURABLE_SIZE`）」として扱う（fail-closed）。呼び出し側の上限
    判定（`_clip_cards` の候補リスト全体サイズ判定・3 dialect の累計判定）はこの大きな値を受けて
    必ず「上限超過」と判断する＝直列化不能な要素は個別クリップでは弾かれ、累計判定では run が
    打ち切られる。
    """
    try:
        return len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    except Exception:
        return _UNMEASURABLE_SIZE


def _tool_bytes_over_budget(total_tool_bytes: int, shared_budget: dict | None,
                            max_total_bytes: int | None = None) -> bool:
    """S4-b（複数プロファイル横断予算・§6.2 項1）: per-run 上限（`max_total_bytes`）
    **または** 共有予算（`shared_budget["tool_bytes_used"] > shared_budget["tool_bytes_max"]`）の
    どちらか一方でも超過していれば True（fail-closed・`openai_style` の既存打ち切り分岐が使う）。
    `shared_budget` が None（既定）なら per-run 上限のみで判定する（既存呼び出し元は不変）。

    `max_total_bytes`（省略可・既定 `None`＝モジュール既定 `TOOL_RESULT_MAX_TOTAL_BYTES`＝既存
    呼び出し元は無変更・BUDGET-1 §3.4）: 呼び出し元が run 開始時に1回だけ
    `resolve_tool_result_budgets()` で解決した実効値。

    レビュー是正（LOW・S4-b RV 1巡目・予算 dict の入口検証）: 形が不正（キー欠損・非数値・
    used が負・max<=0）な shared_budget は「判定不能」として **over-budget 扱い＝fail-closed**。
    直アクセス（KeyError）や負値 used による予算の実質増加を防ぐ。"""
    limit = max_total_bytes if max_total_bytes is not None else TOOL_RESULT_MAX_TOTAL_BYTES
    if total_tool_bytes > limit:
        return True
    if shared_budget is None:
        return False
    # RV 2巡目是正: 片側キー欠損（例 {"tool_bytes_max": 100}）も「形が不正＝判定不能」として
    # fail-closed（.get の既定値で正常形に見せない）。
    if "tool_bytes_used" not in shared_budget or "tool_bytes_max" not in shared_budget:
        return True
    try:
        used = int(shared_budget["tool_bytes_used"])
        mx = int(shared_budget["tool_bytes_max"])
    except (TypeError, ValueError):
        return True
    if used < 0 or mx <= 0:
        return True
    return used > mx


# graph_neighbors のカード件数上限。grep/es のヒット数上限 `MAX_HITS`（env 化済み）とは独立の値
# （troubleshoot UI 用サイドカーの件数はグラフ探索の性質で決まり、grep のヒット数上限に連動する
# 理由が無い）。env 化しない固定値。
_GRAPH_CARDS_MAX = 30


def _clip_cards(cards: list, max_count: int = _GRAPH_CARDS_MAX, max_bytes: int = TOOL_RESULT_MAX_BYTES) -> list:
    """`cards`（`graph_neighbors` のカード・troubleshoot UI 用サイドカー）を件数＋直列化バイト上限で
    切り詰める（secRV FIX-2・2026-07-19・cards サイドカーのバイト迂回）。

    LLM 向け `view` は元から `cards[:_GRAPH_CARDS_MAX]` で件数制限していたが、4つ目の戻り値（呼び出し元の
    3 dialect が `cards += cd` で蓄積し最終的に troubleshoot の `data.candidates` へ載るサイドカー）
    はそれとは独立に無制限で返しており、`total_tool_bytes` の計測対象にも入らなかった。件数上限
    到達、または直列化バイト上限に達した時点で打ち切る（超過分は捨てる・fail-closed）。Neo4j 側の
    取得件数上限（`lens_service`）は範囲外のため触らず、ここで受け取った後にクリップする。

    レビュー是正（FIX-M1・secRV・2026-07-19・単一巨大カードが個別上限を迂回）: 以前は
    `if out and total + size > max_bytes` という条件のため、`out` が空（先頭カード）だと左辺が
    False になり右辺のサイズ判定自体が評価されず、先頭カードは常に無条件で追加されていた
    （実測: 単一 10,030 byte カードが 100 byte 上限でも必ず1件通っていた）。是正後は先頭を
    特別扱いせず、各カード（先頭含む）を仮に追加した**候補リスト全体**の実直列化バイト数
    （`[`/`]`/`,` 等の区切り込み・個別要素バイトの単純合計ではない）が `max_bytes` を超えるなら、
    そのカードを追加せず打ち切る（`out` が空のままでも巨大な単一カードは弾く）。
    """
    out: list = []
    for c in cards[:max_count]:
        candidate = out + [c]
        if _result_byte_size(candidate) > max_bytes:
            break
        out = candidate
    return out


# glob_search の返却上限（要件: 200件で打ち切り・打ち切りは明示）。grep/es の MAX_HITS とは
# 独立の固定値（ファイル名だけを返す軽い列挙のため env 化しない）。
_GLOB_MAX_RESULTS = 200
# `_重要度.txt` の glob と同じ長さ上限を流用する（`importance._match_segment_glob` を共有するため、
# 想定する入力の形も揃える・二重管理しない）。
_GLOB_PATTERN_MAX_LEN = importance._MAX_PATTERN_LEN

# doc_outline の見出し検出（ATX 形式・レベル1〜3のみ＝派生MDの表/シート/ネスト表見出しは
# レベル4以下まで使う（`sherpa/ingest/human_md.py` 参照）が、outline はレベル4以下を意図的に
# 対象外にする——構造の当たり付けに要る大枠だけを返し、細部は read_doc/read_around に委ねる）。
# `grep_tool.grep_search`（MD の見出し節判定）と同じ「lstrip 後に # で始まる行」という簡易判定を踏襲しつつ、
# 見出しレベル・タイトルを取り出すため `\s+` を要求する（`#!/bin/sh` 等の非見出しを誤検出しない）。
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$")
# 1回の返却件数上限（glob_search の `_GLOB_MAX_RESULTS` と同じ桁・巨大な見出し数の増幅を防ぐ）。
_OUTLINE_MAX_HEADINGS = 200
_OUTLINE_TITLE_MAX_CHARS = 300


def _validate_glob_pattern(raw) -> str | None:
    """`glob_search` の `pattern` 引数を検証する（無効なら None）。

    `doc_id`（`_safe_doc_path`）と同じトラバーサル拒否（絶対パス・バックスラッシュ・NUL・
    `..`/空セグメント）を適用する——グロブパターンも `/` 区切りでセグメント解釈するため、
    doc_id と同じ危険な形を弾く。
    """
    if not isinstance(raw, str):
        return None
    pattern = raw.strip()
    if not pattern or len(pattern) > _GLOB_PATTERN_MAX_LEN:
        return None
    if pattern.startswith("/") or "\\" in pattern or "\x00" in pattern:
        return None
    parts = pattern.split("/")
    if ".." in parts or "" in parts:
        return None
    return pattern


def _glob_match_pattern(pattern: str) -> str:
    """スラッシュを含まないパターンは「どの階層のファイル名にも一致」とみなし `**/` を前置する
    （ripgrep の `--glob` と同じ慣習）——利用者/LLM が `*.jcl` のように書いても深さを問わず
    見つかる。スラッシュを含むパターンはそのまま（world ルートからの絞り込みとして扱う）。
    """
    return pattern if "/" in pattern else f"**/{pattern}"


def _safe_doc_path(world: str, doc_id: str, *, layer=None):
    """doc_id（rel_path）→ `(root, lexical_rel, 読み取り可能な実パス)`（無効/範囲外/秘匿種別は None）。

    `layer`（省略可・既定 None＝層チェックしない・`verify_citation` はこのまま呼ぶ）: 指定時は
    `classify_document` の確定結果（`layer_mod.in_layer_code`）で層一致も確認する——`read_around`
    が拡張子だけの近似（`layer_mod.in_layer`）ではなく、ここで既に確定させた「実際に code か」を
    使う（§7 裁定10・grep/list_docs と同じ確定判定に揃える）。Office/画像は常に `"docs"` 側。

    トラバーサル（`..`/絶対/空セグメント/バックスラッシュ/NUL）拒否＋**本文種別のみ**＋
    解決後に許可ルート（Office=派生MD root／その他=world root）配下に閉じることを realpath で確認（symlink 脱出も拒否）。

    `_READABLE_EXT` は高速な事前フィルタ（`doc_kinds`/`grep_tool._TEXT_EXT` と同型）——最終判定
    ではない。登録拡張子（`_analyzer_registry.registered_extensions()` 由来）は accepts() 全滅
    （未対応）や読み取り不可でも拡張子だけでは通さず、Office/画像を除く本文種別は
    `corpus_docs.classify_document` で最終確定する（grep/ES/list_docs と同じ契約・§7 裁定10）。
    Office/画像（`_OFFICE_MD`）は既存の資料種別として固定の集合のため対象外——実在確認（下の
    `rp.is_file()`）だけで十分（この集合自体が「対応済みの資料種別」を表す・classify_document は
    Office/画像の拡張子分類を持たないため呼んでも判定できない）。

    rag/legacy の優先順位（`grep_tool.preferred_derived_name`）は grep_search と共有し、**ここで1回だけ**
    解決する。返す `lexical_rel` は doc_id から機械的に導いた値そのもの（resolve 済みパスから逆算しない）で、
    呼び出し元（`run_tool` の read_around）はこの `root`/`lexical_rel` を後段の nofollow walk へそのまま渡し、
    もう一度解決しない（二重解決すると、その間隔で rag/legacy の実在状況が変わった場合に検証対象と
    実際に open するファイルが食い違いうる）。

    **順序が重要**: 封じ込め（root 配下確認）・symlink 拒否・regular file 確認を**先に**行い、
    それらを通過した実パス（`rp`）に対してだけ `classify_document`（accepts() 内容判定の
    `read_head`）を呼ぶ——`read_head` は実際にファイルを開いて読むため、封じ込め検証より前に
    呼ぶと、範囲外シンボリックリンクや FIFO 等の非 regular ファイルの内容を検証前に読んでしまう
    （多層防御・実際に読むのは既定 accepts を上書きする候補がある拡張子のときだけ・§7 裁定10）。

    symlink 拒否は resolve() **後**の実体だけを見ない: `cand.resolve()` は経路上のすべての
    symlink を辿って最終実体を返すため、その最終実体自身に対する `is_symlink()` は常に偽になる
    （symlink の**先**が symlink でない限り検知できない＝root 内を指す symlink はこれで通過して
    しまう）。字面上のパス（解決済み root＋`lexical_rel` を**そのまま連結しただけ**・ファイル
    システムには触れない）と実際の resolve() 結果を突き合わせ、一致しなければ `cand` 自身か
    祖先ディレクトリのどこかに symlink があったと判定して拒否する（world root 内を指す symlink
    でも、実体を読む前に一律拒否する）。
    """
    if not doc_id or doc_id.startswith("/") or "\\" in doc_id or "\x00" in doc_id:
        return None
    parts = doc_id.split("/")
    if ".." in parts or "" in parts:
        return None
    ext = Path(doc_id).suffix.lower()
    if ext not in _READABLE_EXT:
        return None
    if importance.is_importance_control_path(doc_id):   # 重要度設定ファイル自体は精読対象外（§5）
        return None
    is_office = ext in _OFFICE_MD
    if is_office:
        # rag（RAG 正本）／md（人間用・legacy 縮退）は§8.1 三階層のフォルダ分離で別ディレクトリ。
        # `preferred_derived_name` は rag_root だけを見て優先すべき名前を1つ返す——返る名前が
        # `.rag.md` で終わるかで物理ルートを判別する（grep_search の roots_spec 分離と対称）。
        der_rag = worlds.derived_rag_dir(world)
        lexical_rel = grep_tool.preferred_derived_name(der_rag, doc_id)
        root = der_rag if lexical_rel.endswith(grep_tool._RAG_SUFFIX) else worlds.derived_md_dir(world)
    else:
        root = worlds.world_dir(world)
        lexical_rel = doc_id
    if not root:
        return None
    root = Path(root)
    cand = root / lexical_rel
    try:
        rr = root.resolve()
        rp = cand.resolve()
        if not (rp == rr or rp.is_relative_to(rr)):
            return None
        if rp != rr / lexical_rel:          # 字面パスと不一致＝経路上のどこかに symlink があった
            return None
        if not rp.is_file():                # FIFO/ソケット等の非 regular も拒否
            return None
    except OSError:
        return None
    is_code = False
    if not is_office:
        from . import corpus_docs
        verdict = corpus_docs.classify_document(doc_id, ext, lambda p=rp: corpus_docs._read_head(p))
        if verdict["kind"] == "unreadable" or (verdict["kind"] != "code" and verdict.get("doctype") is None):
            return None
        is_code = verdict["kind"] == "code"
    if layer is not None and not layer_mod.in_layer_code(is_code, layer):
        return None
    return root, lexical_rel, rp


def _open_doc_stream(world: str, doc_id: str, sp, layer) -> tuple:
    """`doc_id` を安全に解決し、読み取り用に open 済みのバイナリファイルオブジェクトを返す。

    `read_around`/`read_doc`/`doc_outline` が共有する土台——scope/層フィルタと symlink TOCTOU
    対策（`_safe_doc_path` の解決結果を信頼アンカーに、`lexical_rel` を `_open_file_nofollow_walk`
    で1段ずつ open）は3ツール共通（検証済みの安全弁を二重実装しない）。

    戻り値 `(f, error)`。成功時 `error=None`・`f` は呼び出し元が close する責務を持つ（ストリーミング
    走査の間じゅう開いたままにする必要があるため `with` に入れずそのまま返す）。失敗時
    `(None, {"error": ...})`。

    2026-09（本丸・ストリーミング化）: 旧実装は `f.read(cap)` で全文を一括ロードしていた（cap の
    既定を 64MiB へ引き上げた際、1回の呼び出しが最大 64MB を一括でメモリに載せる懸念が再燃した・
    grep 側と同じ secRV MED-B 型）。以後は `grep_tool._CappedStreamReader`/`_logical_lines`
    （2026-09 に grep をストリーミング化した際の実装をそのまま再利用・二重実装しない）で
    bounded chunk 走査にし、呼び出し元（`_stream_doc_lines` 参照）が必要な窓だけ保持する。
    """
    if not scope_mod.in_scope(doc_id, sp):          # 範囲外は読まない（MIRROR §3）
        return None, {"error": "指定 doc_id は対象範囲外です"}
    resolved = _safe_doc_path(world, doc_id, layer=layer)
    if resolved is None:
        return None, {"error": "doc_id が無効、または読み取り対象外です"}
    root, lexical_rel, _validated_path = resolved
    rel_parts = Path(lexical_rel).parts
    if not rel_parts:
        return None, {"error": "読み取りに失敗しました"}
    try:
        fd = _open_file_nofollow_walk(root, rel_parts)
    except OSError:
        return None, {"error": "読み取りに失敗しました"}
    fd_owned = True
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None, {"error": "読み取りに失敗しました"}
        f = os.fdopen(fd, "rb")
        fd_owned = False   # 以後の close は呼び出し元（f.close()）が引き受ける
    except OSError:
        return None, {"error": "読み取りに失敗しました"}
    finally:
        if fd_owned:
            try:
                os.close(fd)
            except OSError:
                pass
    return f, None


def _stream_doc_lines(f):
    """open 済み `f`（`_open_doc_stream` が返すバイナリファイル）を `_READ_AROUND_FILE_CAP_BYTES`/
    `_READ_LINE_MAX_BYTES` で bounded にストリーミング走査する `(reader, 行イテレータ)` を返す。

    行番号の定義は `_logical_lines`（`str.splitlines()` と同一の論理行）——grep のヒット行番号と
    read 側の行番号がずれない（`grep_tool._logical_lines` 参照）。呼び出し元はイテレータを消費し
    終えた後（cap 到達・EOF・呼び出し元都合の早期打ち切りのいずれか）に `reader.truncated`/
    `reader.line_overflowed` を見て `file_truncated` を判定する（`grep_tool.grep_search` の
    `effective_truncated = reader.truncated or reader.line_overflowed` と同じ判定式）。
    """
    reader = grep_tool._CappedStreamReader(f, line_max_bytes=_READ_LINE_MAX_BYTES)
    return reader, grep_tool._logical_lines(reader, _READ_AROUND_FILE_CAP_BYTES)


# ---- 親返し（L4c・§3.3/§3.4）: es_search のヒットを doc_id で束ね、rag.md の全文(P3)／
# 領域(P2)を予算内で返す。検索自体は子チャンク単体のまま（BM25/kNN の精度が最も出る粒度）——
# ここは「返す前」の後処理のみ。全文を読み込んでから切り詰める実装は禁止（§3.3）: サイズは
# `stat` で先に見て（`_rag_md_size`）、P2 はアンカー単位のストリーミングで対象チャンクだけを
# 集める（`_rag_md_region_text`）。既存の安全弁（`_open_doc_stream`/`_stream_doc_lines`＝
# symlink TOCTOU 対策・`_READ_AROUND_FILE_CAP_BYTES`）を再利用し、二重実装しない。

def _rag_md_size(world: str, doc_id: str, sp, layer) -> int | None:
    """親返しの P3/P2 判定用: `doc_id` の rag.md（RAG 正本）のバイトサイズを `stat` で見る
    （読む前に見る・§3.3）。rag.md へ解決できない（legacy md へ縮退済み・不在・範囲外）場合は
    None——呼び出し元はその doc を親返し対象外として chunk tier のまま扱う。
    """
    if not scope_mod.in_scope(doc_id, sp):
        return None
    resolved = _safe_doc_path(world, doc_id, layer=layer)
    if resolved is None:
        return None
    _root, lexical_rel, rp = resolved
    if not lexical_rel.endswith(grep_tool._RAG_SUFFIX):
        return None            # legacy md へ縮退済み＝rag.md 不在＝親返し対象外
    try:
        return rp.stat().st_size
    except OSError:
        return None


def _rag_md_read_full(world: str, doc_id: str, sp, layer) -> str | None:
    """親返し P3: rag.md 全文を `_open_doc_stream`/`_stream_doc_lines`（既存のストリーミング
    読み取り・`_READ_AROUND_FILE_CAP_BYTES` で bounded）で読む。呼び出し元は `_rag_md_size` で
    予算内と確認済みの doc にのみ呼ぶ（P3 は「サイズが既に小さいと分かっている」ケースの
    全文読みであり、cap は TOCTOU 的なサイズ変化に対する保険）。open/read 失敗は None。
    """
    f, err = _open_doc_stream(world, doc_id, sp, layer)
    if err is not None:
        return None
    try:
        _reader, it = _stream_doc_lines(f)
        return "\n".join(it)
    finally:
        f.close()


def _rag_md_region_text(world: str, doc_id: str, sp, layer, target_chunk_ids, byte_cap: int) -> str | None:
    """親返し P2: rag.md をアンカー（`<!-- chunk:{chunk_id} -->`・`es_index.rag_md_anchor_chunk_id`）
    単位でストリーミング走査し、`target_chunk_ids` に属するチャンクの本文だけを集める。

    対象外のチャンク本文は保持しない（`cur_id in target_chunk_ids` のときだけ行を蓄積する）ため、
    メモリは「現在集めている1チャンク分」に留まる——ファイル全体を読み切らない（全件そろうか
    `byte_cap` 超過で早期に打ち切れる）。`byte_cap` を超えたら None を返し、それまでに集めた
    部分的な本文は**使わない**（黙って中途半端な本文を返さない・§3.3「全文を読み込んでから
    切り詰めない」の裏返し＝「途中まで読んで打ち切ったものを完全なものと偽らない」）。
    """
    if not target_chunk_ids:
        return None
    f, err = _open_doc_stream(world, doc_id, sp, layer)
    if err is not None:
        return None
    remaining = set(target_chunk_ids)
    collected: dict = {}
    order: list = []
    cur_id = None
    cur_buf: list = []
    total_bytes = 0
    over = False

    def _close(cid: str, buf: list) -> None:
        nonlocal total_bytes, over
        body = "\n".join(buf).strip()
        collected[cid] = body
        order.append(cid)
        remaining.discard(cid)
        total_bytes += len(body.encode("utf-8"))
        if total_bytes > byte_cap:
            over = True

    try:
        _reader, it = _stream_doc_lines(f)
        for line in it:
            anchor_id = es_index.rag_md_anchor_chunk_id(line)
            if anchor_id is not None:
                if cur_id is not None and cur_id in remaining:
                    _close(cur_id, cur_buf)
                    if over:
                        break
                if not remaining:
                    break
                cur_id, cur_buf = anchor_id, []
                continue
            if cur_id is not None and cur_id in remaining:
                cur_buf.append(line)
        else:
            # EOF（break していない）＝最後のアンカーの本文が未確定なら確定させる。
            if cur_id is not None and cur_id in remaining:
                _close(cur_id, cur_buf)
    finally:
        f.close()
    if over or not collected:
        return None
    return "\n\n".join(collected[cid] for cid in order)


def _resolve_parent_return(world: str, rag_groups: dict, sp, layer, budget_for_rag: int) -> list:
    """親返し（§3.3/§3.4）本体: doc_id ごとに束ねた rag チャンクのヒットを P3(全文)/P2(領域)/
    chunk(子のみ) へ振り分ける。決定的な貪欲法（§3.4 配分規則）——

    1. まず全 doc の**最低保証**（子チャンク本文の合計＝`baseline`）を `budget_for_rag` から
       確保する（1位の巨大文書が予算を食い尽くして2位以下の子チャンクが消える事故を防ぐ）。
    2. 残り予算をベストスコア順（同点は doc_id 昇順・決定的）に、rag.md サイズ（stat）が
       残り予算に入るなら P3 全文／領域なら P2／どちらも無理なら chunk（子チャンクの結合）
       のまま——という優先順で使う。
    3. 各 doc は必ず1エントリを返し（消えない）、`tier` を必ず申告する（黙って縮退しない）。

    `rag_groups`: `{doc_id: [{"chunk_id", "parent_id", "locator", "score", "text"}, ...]}`
    （`text` は既に redaction/500字クリップ済みの子チャンク本文＝chunk tier の最低保証そのもの）。
    `budget_for_rag`: この tool result のうち rag doc 群に残っている予算（legacy ヒット分を
    差し引いた残り・呼び出し元が計算する）。
    """
    groups = []
    for doc_id, items in rag_groups.items():
        baseline = sum(len(it["text"].encode("utf-8")) for it in items)
        best_score = max(float(it.get("score") or 0) for it in items)
        groups.append((doc_id, items, baseline, best_score))
    remaining = max(0, budget_for_rag - sum(g[2] for g in groups))
    groups.sort(key=lambda g: (-g[3], g[0]))

    out = []
    for doc_id, items, baseline, _best_score in groups:
        chunk_ids = [it["chunk_id"] for it in items]
        tier = "chunk"
        text = "\n\n".join(it["text"] for it in items)   # 最低保証（既に redaction/クリップ済み）
        full_size = _rag_md_size(world, doc_id, sp, layer)
        if full_size is not None:
            delta = full_size - baseline
            if delta <= remaining:
                full_text = _rag_md_read_full(world, doc_id, sp, layer)
                if full_text is not None:
                    text = _redact(full_text)
                    tier = "full"
                    remaining -= delta
        if tier == "chunk":
            parent_ids = sorted({it["parent_id"] for it in items if it.get("parent_id")})
            if parent_ids:
                target_ids = set(es_index.chunk_ids_for_parent(
                    world, doc_id, parent_ids, limit=_PARENT_RETURN_REGION_CHUNKS_MAX))
                target_ids |= set(chunk_ids)   # ヒット自身のチャンクは必ず含める（ES 反映漏れの安全弁）
                region_cap = baseline + remaining
                region_text = _rag_md_region_text(world, doc_id, sp, layer, target_ids, region_cap)
                if region_text is not None:
                    delta_region = len(region_text.encode("utf-8")) - baseline
                    if delta_region <= remaining:
                        text = _redact(region_text)
                        tier = "region"
                        remaining -= delta_region
        entry = {"doc_id": doc_id, "tier": tier, "text": text,
                 "chunks": [{"chunk_id": it["chunk_id"],
                             **({"locator": it["locator"]} if it.get("locator") is not None else {})}
                            for it in items]}
        out.append(entry)
    return out


# ---- EXT-2（拡張設計 §4.3）: Committed Evidence 化直前の機械検証（LLM 不要・常時実行） ----

_SPAN_MATCH_WS_RE = re.compile(r"\s+")


def verify_citation(citation: dict, world: str, *, _content_cache: dict | None = None) -> dict:
    """引用（citation dict）を機械的に検証する（拡張設計 §4.3・深度プロファイルに関わらず常時実行）。

    (1) doc 実在チェック: `_safe_doc_path` と同じ解決規則（rag/legacy 優先順位・封じ込め・秘匿種別拒否）
        で対象ファイルが実在・読み取り可能であることを確認する。失敗（不在／symlink 脱出等）は
        `exists=False`（呼び出し側はこの引用を Committed Evidence から除外する＝壊れた DL リンクを
        出典に出さない）。
    (2) span（grep/es_search 由来の整数行番号）があれば、その範囲を実際に読み直し `quote` と照合する
        （`grep_tool.grep_search` が `"\n".join(lines[s-1:e]).strip()` で組み立てる形と対称の再構成）。
        rag_chunks／Office 派生 MD 等、span が整数行番号を持たない引用（SEARCH-CUT-3 の locator 由来）は
        照合をスキップし `exists_no_span` を返す。

    不一致（`span_unmatched`）は **除外しない**（`exists=True` のまま返す）: grep_tool 側の節境界の
    取り方や Office 派生 MD の整形差だけで誤って recall を落とすリスクを避ける保守的な選択。実測で
    ドリフト率を見てから「不一致も除外する」判断を強めるのは次段（Evidence Packet の
    `verification_method` に記録が残るため、後から実測できる）。

    `_content_cache`（省略可・内部専用）: 非 None のとき `(root, lexical_rel)` をキーにファイル内容
    （bytes・実在しなければ None）をキャッシュし、同一 doc を跨ぐ複数回の呼び出し（例:
    `providers/base.py` が同一 doc 内の複数の統合 span を再検証するとき）でディスク再読込を
    1 doc につき1回に抑える。既定 None は従来どおり呼び出しごとに毎回読む（byte-identical）。
    """
    doc_id = citation.get("doc_id")
    if not doc_id or not isinstance(doc_id, str):
        return {"exists": False, "method": "doc_missing"}
    resolved = _safe_doc_path(world, doc_id)
    if resolved is None:
        return {"exists": False, "method": "doc_missing"}
    root, lexical_rel, _validated_path = resolved
    span = citation.get("span")
    has_span = (isinstance(span, (list, tuple)) and len(span) == 2
               and isinstance(span[0], int) and not isinstance(span[0], bool)
               and isinstance(span[1], int) and not isinstance(span[1], bool)
               and 1 <= span[0] <= span[1])
    if not has_span:
        return {"exists": True, "method": "exists_no_span"}
    rel_parts = Path(lexical_rel).parts
    if not rel_parts:
        return {"exists": True, "method": "exists_no_span"}
    cache_key = (str(root), lexical_rel) if _content_cache is not None else None
    if cache_key is not None and cache_key in _content_cache:
        raw = _content_cache[cache_key]
        if raw is None:
            return {"exists": False, "method": "doc_missing"}
    else:
        try:
            fd = _open_file_nofollow_walk(root, rel_parts)
        except OSError:
            if cache_key is not None:
                _content_cache[cache_key] = None
            return {"exists": False, "method": "doc_missing"}
        fd_owned = True
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                if cache_key is not None:
                    _content_cache[cache_key] = None
                return {"exists": False, "method": "doc_missing"}
            with os.fdopen(fd, "rb") as f:
                fd_owned = False
                raw = f.read(_READ_AROUND_FILE_CAP_BYTES)
        except OSError:
            if cache_key is not None:
                _content_cache[cache_key] = None
            return {"exists": False, "method": "doc_missing"}
        finally:
            if fd_owned:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if cache_key is not None:
            _content_cache[cache_key] = raw
    lines = raw.decode("utf-8", errors="replace").splitlines()
    s, e = span
    if s > len(lines):
        return {"exists": True, "method": "span_unmatched"}
    window = _redact("\n".join(lines[s - 1:min(e, len(lines))]).strip())
    norm_window = _SPAN_MATCH_WS_RE.sub(" ", window)
    norm_quote = _SPAN_MATCH_WS_RE.sub(" ", str(citation.get("quote") or "")).strip()
    method = "span_verified" if (norm_quote and norm_quote in norm_window) else "span_unmatched"
    return {"exists": True, "method": method}

# SYSTEM は検索経路トグル（調べ方ブロック §3.6・SC-6e）に応じて `system_prompt()` が
# 組み立てる。全ON（既定・省略）は下の断片をそのまま連結した文字列（固定 byte 長＋SHA-256 の
# golden テストで検証・断片分割はここでしか観測できない実装詳細）。
# この SYSTEM 文言と調べ方ブロックのチップ表記は「grep」ではなく「コマンド検索」を使う
# （内部識別子＝ツール名 `ripgrep_search`・tools_pref の `grep` キーは不変。trace ノードの
# 「資料を検索（grep）」ラベルは e2e/改善ログ語彙が固定しており別スライスで扱う）。
_SYS_INTRO_AND_LIST_DOCS = (
    "あなたは社内資料を調べて答えるアシスタントです。事前の索引はありません。"
    "ツールで資料を実際に検索して、**根拠のある事実だけ**で日本語で簡潔（2〜4文）に答えてください。\n"
    "**ドキュメント数・一覧・どんな資料があるか・フォルダ構成といった台帳質問は、まず list_docs を使う**"
    "（コマンド検索は本文中の一致しか探せず件数/一覧には答えられない）。フォルダ名・ファイル名はパスに含まれるので、"
    "名前の部分一致は list_docs の name_pattern で当てる（コマンド検索で本文からは探さない）。"
    "表記が揺れそうな語（送り仮名・略し方など）は短い部分語で試す（例:「4期更改」がヒットしなければ「4期」）。\n"
    "**件数を答えるときは list_docs の path_prefix でフォルダを確定してから数え、どのフォルダを数えたかを"
    "回答に明示する**（曖昧なら『4期更改』と『4期保守』のように候補フォルダ別の内訳で答える）。\n"
    "**大規模な範囲でフォルダの階層構造そのものを俯瞰したいとき**（list_docs のフラット一覧では"
    "形が掴めないとき）は folder_tree で深さ上限つきのフォルダ木（フォルダごとの件数つき）を確認する。\n"
    "文書の**構造を先に掴みたいとき**は doc_outline で見出し一覧（行番号つき）を確認し、"
    "**長い文書を通して読みたいとき**は read_doc で開始行から連続して読む"
    "（1回で読み切れなければ次の開始行を指定して呼び直す）。"
    "**ヒット周辺だけを精読したいとき**は read_around を使う。\n"
)
_SYS_GREP_STEP = (
    "本文の内容を調べる質問の手順: まず ripgrep_search で当たりを付け、関係しそうな箇所を read_around で精読し、"
    "外していれば検索語を変えて再検索する（台帳質問は上記のとおり list_docs が先）。"
)
# ファイル名/パスのパターンで探したいとき用（コマンド検索＝grep 軸に同居・grep OFF/不達では
# glob_search 自体を提示しない・§system_prompt/openai_tools/gemini_tools 参照）。
_SYS_GLOB_STEP = (
    "ファイル名・フォルダ名のパターンで探したいとき（例:「請求書系のExcelだけ」「JCLを一覧して」）は "
    "glob_search にワイルドカードパターン（例 `*請求書*.xlsx`・`**/障害対応/*.md`・`*.jcl`）を渡す"
    "（中身は読まない・該当パス一覧だけが返る）。"
)
_SYS_ES_FOLLOWUP = (
    "**ripgrep_search が0件/空振りのとき、または言い回しが揺れる概念・日本語の同義語で"
    "言い換えが必要なときは es_search（全文＋ベクトル）を試す**（コマンド検索は完全一致・固有名詞にしか強くない）。"
)
# grep OFF/不達で es_search が唯一の本文検索手段のときの代替文（ripgrep_search への言及を含めない）。
_SYS_ES_PRIMARY_STEP = (
    "本文の内容を調べる質問の手順: es_search（全文＋ベクトル・言い回しが揺れる概念や日本語の同義語に強い）で"
    "当たりを付け、関係しそうな箇所を read_around で精読し、外していれば検索語を変えて再検索する"
    "（台帳質問は上記のとおり list_docs が先）。"
)
_SYS_GRAPH_STEP = (
    "原因の手がかりや関連部品（プログラム/コピーブック/ジョブの呼び出し・コピー・参照、"
    "関連文書など）をたどりたいときは graph_neighbors に正確な名前を渡して関係グラフを引く"
    "（つながりの経路つきで返る）。"
)
# grep が使えるときだけ言及する比較文（grep OFF/不達では「コマンド検索を打ち直すより」という
# 比較自体が意味を持たないため外す）。
_SYS_GRAPH_GREP_COMPARISON = (
    "**プログラム名/データ項目名などの名前が一つでも判明したら、その関連の広がりはコマンド検索を何度も打ち直すより"
    "先に graph_neighbors で辿るほうが早い**。"
)
_SYS_COMPARE_STEP = (
    "**世代（トップフォルダ）をまたいで「何が変わったか」を聞かれたとき**は compare_documents で"
    "対応する2文書のRAG正本を突き合わせ、返ってきたdiffを読んで業務語で説明する"
    "（対応文書が一意に決まらないときは candidates から利用者に確認してから比較する）。"
)
_SYS_OUTRO = (
    "**本文・グラフに無いことは書かない（推測しない）**。"
    "調査範囲・目的・選択肢が曖昧で、確認しないと結果が大きく変わる場合だけ ask_user でユーザに確認してください。"
    "十分な根拠が集まったらツールを呼ばず最終回答だけを返す。出典の列挙は不要（別途付与）。"
    "回答は Markdown（太字・箇条書き・インラインコード）で書いてよい。"
)
SYSTEM = (_SYS_INTRO_AND_LIST_DOCS + _SYS_GREP_STEP + _SYS_GLOB_STEP + _SYS_ES_FOLLOWUP + _SYS_GRAPH_STEP
         + _SYS_GRAPH_GREP_COMPARISON + _SYS_COMPARE_STEP + _SYS_OUTRO)


def system_prompt(tools_pref: dict | None = None) -> str:
    """検索経路トグル（調べ方ブロック §3.6・SC-6e）に応じた SYSTEM 節を組み立てる。

    全 ON（省略/`None` を含む）は `SYSTEM`（正準文字列）をそのまま返す——意図外の差分を作らない
    契約。OFF にしたツールは推奨・言及しない（提示していないツールを使えと指示すると、モデルが
    それを呼んで拒否される無駄なターン・上限到達につながる）。qa/author は grep・es_search の
    どちらか一方が残っていれば本文検索の手順を差し替えて案内し、両方 OFF/不達なら本文検索の
    手順そのものを省く（`graph_neighbors` だけが残る）。3つとも False は `tools_pref.
    normalize_tools_pref` が拒否するためここには来ない。`glob_search`（ファイル名/パスのグロブ
    検索）は grep 軸に同居するため、`grep` が有効なときだけ `_SYS_GLOB_STEP` を案内する
    （`openai_tools`/`gemini_tools` の `with_grep` ゲートと同じ判定）。
    """
    tp = tools_pref_mod.normalize_tools_pref(tools_pref)
    grep, fulltext, graph = tp["grep"], tp["fulltext"], tp["graph"]
    if grep and fulltext and graph:
        return SYSTEM
    parts = [_SYS_INTRO_AND_LIST_DOCS]
    if grep:
        parts.append(_SYS_GREP_STEP)
        parts.append(_SYS_GLOB_STEP)
        if fulltext:
            parts.append(_SYS_ES_FOLLOWUP)
    elif fulltext:
        parts.append(_SYS_ES_PRIMARY_STEP)
    if graph:
        parts.append(_SYS_GRAPH_STEP)
        if grep:
            parts.append(_SYS_GRAPH_GREP_COMPARISON)
    # GEN-DIFF: compare_documents は grep/es/graph トグルと無関係の土台系ツール＝常に案内する。
    parts.append(_SYS_COMPARE_STEP)
    parts.append(_SYS_OUTRO)
    return "".join(parts)


_PARAMS_SEARCH = {"type": "object", "properties": {
    "query": {"type": "string", "description": "検索キーワード（型番・関数名・固有名詞など具体語が有効）"}},
    "required": ["query"]}
_PARAMS_LIST_DOCS = {"type": "object", "properties": {
    "path_prefix": {"type": "string",
                    "description": "フォルダで絞る（rel_path の先頭一致・例: '4期保守'）。省略可＝範囲全体"},
    "name_pattern": {"type": "string",
                     "description": "パス（フォルダ名/ファイル名どちらでも）の部分一致で絞る（例: '4期'）。省略可"},
    "limit": {"type": "integer", "description": "一覧に含める最大件数（既定50）。件数(count)は limit と無関係に全件を返す"}},
    "required": []}
_PARAMS_FOLDER_TREE = {"type": "object", "properties": {
    "path_prefix": {"type": "string",
                    "description": "この配下のフォルダ階層だけを見る（rel_path の先頭一致・例: '4期保守'）。省略可＝範囲全体"},
    "depth": {"type": "integer", "description": "列挙するフォルダの深さ上限（既定3・1〜10にクランプ）"}},
    "required": []}
_PARAMS_READ = {"type": "object", "properties": {
    "doc_id": {"type": "string", "description": "ripgrep_search が返した doc_id（資料の相対パス）"},
    "line": {"type": "integer", "description": "精読の中心行（ヒット行）"},
    # 実際の既定値（`READ_WINDOW`）を埋め込む＝env で変えたときにモデルへの通知も追随する。
    "window": {"type": "integer", "description": f"前後に読む行数（既定 {READ_WINDOW}）"}},
    "required": ["doc_id", "line"]}
_PARAMS_READ_DOC = {"type": "object", "properties": {
    "doc_id": {"type": "string", "description": "list_docs/ripgrep_search 等が返した doc_id（資料の相対パス）"},
    "start_line": {"type": "integer",
                  "description": "読み始める行（既定1）。続きが必要なら前回の返却が示す次の行を指定して呼び直す"}},
    "required": ["doc_id"]}
_PARAMS_OUTLINE = {"type": "object", "properties": {
    "doc_id": {"type": "string", "description": "list_docs/ripgrep_search 等が返した doc_id（資料の相対パス）"}},
    "required": ["doc_id"]}
_PARAMS_ASK = {"type": "object", "properties": {
    "prompt": {"type": "string", "description": "ユーザに確認したい短い質問文"},
    "mode": {"type": "string", "enum": ["single", "multiple"],
             "description": "single=ラジオボタン、multiple=チェックボックス"},
    "options": {"type": "array", "minItems": 2, "maxItems": 8, "items": {"type": "object", "properties": {
        "id": {"type": "string", "description": "選択肢ID（省略可）"},
        "label": {"type": "string", "description": "表示ラベル"},
        "description": {"type": "string", "description": "補足説明（省略可）"}},
        "required": ["label"]}},
    "allow_free_text": {"type": "boolean", "description": "自由入力も許可するか"}},
    "required": ["prompt", "mode", "options"]}
_DESC_SEARCH = ("社内資料を全文 grep して当たりを付ける（doc_id と行番号つきのヒットを返す）。完全一致・固有名詞に強い。"
                "file_truncated が付くヒットは、その文書がまだ検索し切れていない可能性がある——"
                "read_doc で続きを確認する。")
_DESC_READ = "ヒット箇所の周辺行だけを精読する（全文は読まない）。doc_id と line を渡す。"
_DESC_READ_DOC = ("文書を開始行から連続して読む（通読向け・全文を一度には読まない）。"
                  "doc_id と start_line（省略時1）を渡す。1回の返却行数には上限があり、"
                  "「全◯行中 X〜Y行目」を返すので、続きが必要なら次の開始行（end_line+1）を"
                  "指定して再度呼び出す（range 外の start_line はエラーで明示）。"
                  "text_truncated が付くときは行内容が大きすぎて途中で切れている・"
                  "file_truncated が付くときは文書自体が大きすぎて total_lines が過小申告の可能性がある。")
_DESC_OUTLINE = ("文書の見出し構造（Markdown の #/##/### 見出し・派生MDの表/シート見出しを含む）を"
                 "行番号つきで返す。read_doc/read_around で読む箇所の当たりを付けるのに使う。"
                 "見出しが無い文書は総行数だけを返す。file_truncated が付くときは文書自体が"
                 "大きすぎて total_lines/見出し一覧が過小申告の可能性がある。")
_DESC_LIST_DOCS = ("文書台帳の一覧・件数を返す（本文は読まない・grep しない）。"
                   "「ドキュメント数」「どんな資料があるか」「フォルダ構成」等の台帳質問はこれで答える。"
                   "path_prefix でフォルダ配下に絞り、name_pattern でパス（フォルダ名/ファイル名）の部分一致に絞れる。"
                   "count は絞り込み後の全件数（limit と無関係）、docs は limit 件までの一覧（rel_path と doctype）。")
# K6（`docs/proposals/2026-09-04-グラフのソース正典化.md` §3・§4b S1）: list_docs（ls 相当・フラット
# 一覧）に対する tree 相当。フォルダ名の意味解釈はしない（クエリ時にこのツールの呼び出し元＝LLM が
# 解釈する・K6・§5「フォルダ意味ノードの事前計算はしない」）。
_DESC_FOLDER_TREE = ("world のフォルダ階層を、深さ上限つき・フォルダごとの件数つきで俯瞰する"
                     "（本文は読まない・grep しない・list_docs のフラット一覧では階層の形が掴めない"
                     "大規模な範囲で使う）。path_prefix でフォルダ配下に絞り、depth（既定3）で列挙する深さを決める。"
                     "フォルダごとに直下ファイル数・配下（再帰）ファイル数・直下サブフォルダ数を返す。"
                     "深さ上限でまだ配下があるフォルダは truncated:true（depth を上げて掘り下げる）。"
                     "フォルダ件数自体が多すぎるときは folders_truncated:true（count が打ち切り前の総数）。")
_DESC_ES = ("社内資料を日本語の全文＋ベクトル検索（形態素・意味の近さ・関連度ランキング）。"
            "言い回しが揺れる概念・日本語の同義語・自然文クエリに強い。"
            "ripgrep_search が0件/空振りのときはまずこれを試す。doc_id と抜粋を関連度順で返す。")
_DESC_ASK = ("回答や検索条件を確定する前にユーザへ確認する。結果が大きく変わる曖昧さがある場合だけ使う。"
             "例: 影響分析で起点や影響先が複数候補に割れるとき、確実な波及が0件で要確認だけになったときは、"
             "対象の絞り込みを確認してよい。依頼に「確認してから進めて」とあるときは調査より先に確認する。"
             "ただし依頼に「確認ID:」が含まれる場合は前の質問への回答なので再質問しない。"
             "選択肢はラジオボタンまたはチェックボックスとして表示される。")
_DESC_GRAPH = ("関係グラフから、ある名前（プログラム/コピーブック/ジョブ/データ項目/テーブルなど）の**関連部品**をたどる"
               "（コピー・呼び出し・参照・関連文書（言及）などの近傍を、つながりの経路つきで返す）。"
               "名前が一つでも判明したら、その関連の広がりは grep を反復するより先にこれで辿るほうが早い。"
               "原因の手がかり集め（トラブルシュート）に有効。grep で正確な名前を見つけてから渡すと精度が上がる。")
# grep OFF/不達で es_search/graph_neighbors だけが提示されるときの代替 description（SC-6e）。
# `_DESC_ES`/`_DESC_GRAPH` はいずれも grep（ripgrep_search）への言及を含むため、提示していない
# ツールへの言及・推奨をそのまま残さない（無駄なターン/上限到達を防ぐ）。
_DESC_ES_NO_GREP = ("社内資料を日本語の全文＋ベクトル検索（形態素・意味の近さ・関連度ランキング）。"
                    "言い回しが揺れる概念・日本語の同義語・自然文クエリに強い。doc_id と抜粋を関連度順で返す。")
_DESC_GRAPH_NO_GREP = ("関係グラフから、ある名前（プログラム/コピーブック/ジョブ/データ項目/テーブルなど）の**関連部品**をたどる"
                       "（コピー・呼び出し・参照・関連文書（言及）などの近傍を、つながりの経路つきで返す）。"
                       "原因の手がかり集め（トラブルシュート）に有効。")
_PARAMS_GRAPH = {"type": "object", "properties": {
    "name": {"type": "string", "description": "関連をたどる起点の名前（プログラム名/データ項目名など・具体名）"}},
    "required": ["name"]}
_PARAMS_GLOB = {"type": "object", "properties": {
    "pattern": {"type": "string",
               "description": ("ファイル名/パスのワイルドカードパターン。`*`/`?`/`[seq]` は1階層内のみ・"
                               "`**` は複数階層をまたぐ。スラッシュを含まなければファイル名として"
                               "どの階層でも探す（例: '*.jcl'・'*請求書*.xlsx'・'**/障害対応/*.md'）")}},
    "required": ["pattern"]}
_DESC_GLOB = ("ファイル名・フォルダ名のパターンで対象範囲内のファイルを列挙する（中身は読まない・パスのみ）。"
             "大文字小文字は区別しない。該当パス一覧と総件数を返す（上限200件・超過分は打ち切り）。"
             "『x/**』は x 自体にも一致する（配下だけに絞るなら『x/**/*』）。")
# GEN-DIFF（世代間diff比較・`docs/proposals/2026-09-03-世代間diff比較.md`）: grep と同格の素朴な
# 決定的ツール——2文書のRAG正本（.rag.md）の unified diff を返すだけで、レコード同定・業務キー
# 対応付け・要約はしない（それらは呼び出し元＝LLM が diff テキストを読んで行う）。
_DESC_COMPARE = ("2つの文書のRAG正本（.rag.md）を突き合わせ、追加/削除/変更行の unified diff を返す"
                 "（grepと同格の決定的な文字列比較——要約や業務レコードの対応付けはしない・"
                 "diffを読んで説明するのは呼び出し側の仕事）。"
                 "left_doc_id/right_doc_id で比較したい2文書を明示するか、"
                 "source_doc_id（片方の doc_id）と target_generation（比べたい世代＝トップフォルダ名）で"
                 "対応する文書を自動発見する。世代を除いた相対パスが完全一致すれば1件に決まる。"
                 "決まらないときは status: needs_disambiguation と candidates（doc_id 一覧）を返すので、"
                 "会話で利用者にどちらか確認してから left_doc_id/right_doc_id で呼び直す。"
                 "片方以上が rag.md を持たない文書（コード原文等）のときは status: unsupported を返す。")
_PARAMS_COMPARE = {"type": "object", "properties": {
    "left_doc_id": {"type": "string", "description": "比較する片方の doc_id（省略時は right_doc_id も無視される）"},
    "right_doc_id": {"type": "string", "description": "比較するもう片方の doc_id（left_doc_id とセットで指定）"},
    "source_doc_id": {"type": "string", "description": "対応文書を自動発見する起点の doc_id（left_doc_id/right_doc_id 省略時）"},
    "target_generation": {"type": "string",
                          "description": "比べたい世代（トップフォルダ名・例 '5期'）。source_doc_id とセットで指定"}},
    "required": []}


def _desc_es(with_grep: bool) -> str:
    """`with_grep` に応じた es_search の description（全ON相当時は正準文字列 `_DESC_ES` と byte 一致）。"""
    return _DESC_ES if with_grep else _DESC_ES_NO_GREP


def _desc_graph(with_grep: bool) -> str:
    """`with_grep` に応じた graph_neighbors の description（同上・`_DESC_GRAPH` と byte 一致）。"""
    return _DESC_GRAPH if with_grep else _DESC_GRAPH_NO_GREP


def openai_tools(with_es: bool = False, with_graph: bool = False, can_ask: bool = True,
                 with_grep: bool = True) -> list:
    # Med-1（RV・2026-07-07）: can_ask=False（回答の再送＝依頼に「確認ID:」を含む実行）では ask_user
    #   ツール自体を渡さない＝再質問ループを構造的に塞ぐ（S2 の SHERPA_MCP_ASK_DISABLED と同思想）。
    # SC-6e: `with_grep`（既定 True）は検索経路トグルの grep 軸。list_docs/doc_outline/read_doc/
    #   read_around は土台系のため対象外＝常に含める。glob_search（ファイル名/パスのグロブ検索）も
    #   grep 軸に同居する。
    t = [{"type": "function", "function": {"name": "list_docs", "description": _DESC_LIST_DOCS, "parameters": _PARAMS_LIST_DOCS}}]
    # K6: folder_tree は list_docs と同じ台帳ベースの土台系ツール（ES/graph/grep トグルと無関係）＝常に含める。
    t.append({"type": "function", "function": {"name": "folder_tree", "description": _DESC_FOLDER_TREE, "parameters": _PARAMS_FOLDER_TREE}})
    if with_grep:
        t.append({"type": "function", "function": {"name": "ripgrep_search", "description": _DESC_SEARCH, "parameters": _PARAMS_SEARCH}})
        t.append({"type": "function", "function": {"name": "glob_search", "description": _DESC_GLOB, "parameters": _PARAMS_GLOB}})
    # 「構造を掴む→通読→ヒット周辺の精読」の順で並べる（SYSTEM の使いどころ案内と揃える）。
    t.append({"type": "function", "function": {"name": "doc_outline", "description": _DESC_OUTLINE, "parameters": _PARAMS_OUTLINE}})
    t.append({"type": "function", "function": {"name": "read_doc", "description": _DESC_READ_DOC, "parameters": _PARAMS_READ_DOC}})
    t.append({"type": "function", "function": {"name": "read_around", "description": _DESC_READ, "parameters": _PARAMS_READ}})
    insert_at = len(t) - 3   # doc_outline/read_doc/read_around の直前（list_docs[+ripgrep_search]の直後）に差し込む
    if with_es:
        t.insert(insert_at, {"type": "function", "function": {"name": "es_search", "description": _desc_es(with_grep), "parameters": _PARAMS_SEARCH}})
    if with_graph:
        t.insert(insert_at, {"type": "function", "function": {"name": "graph_neighbors", "description": _desc_graph(with_grep), "parameters": _PARAMS_GRAPH}})
    # GEN-DIFF: ES/graph の可用性に依存しない土台系ツール（read_around 等と同じ扱い）＝常に含める。
    t.append({"type": "function", "function": {"name": "compare_documents", "description": _DESC_COMPARE, "parameters": _PARAMS_COMPARE}})
    if can_ask:
        t.append({"type": "function", "function": {"name": "ask_user", "description": _DESC_ASK, "parameters": _PARAMS_ASK}})
    return t


def gemini_tools(with_es: bool = False, with_graph: bool = False, can_ask: bool = True,
                 with_grep: bool = True) -> list:
    # Med-1（RV・2026-07-07）: can_ask=False（確認ID 付き再送）では ask_user を渡さない（openai_tools と同じ）。
    # SC-6e: with_grep は openai_tools と同じ意味（既定 True）。glob_search も同じく grep 軸に同居する。
    fns = [{"name": "list_docs", "description": _DESC_LIST_DOCS, "parameters": _PARAMS_LIST_DOCS}]
    # K6: openai_tools と同じ理由で常に含める。
    fns.append({"name": "folder_tree", "description": _DESC_FOLDER_TREE, "parameters": _PARAMS_FOLDER_TREE})
    if with_grep:
        fns.append({"name": "ripgrep_search", "description": _DESC_SEARCH, "parameters": _PARAMS_SEARCH})
        fns.append({"name": "glob_search", "description": _DESC_GLOB, "parameters": _PARAMS_GLOB})
    fns.append({"name": "doc_outline", "description": _DESC_OUTLINE, "parameters": _PARAMS_OUTLINE})
    fns.append({"name": "read_doc", "description": _DESC_READ_DOC, "parameters": _PARAMS_READ_DOC})
    fns.append({"name": "read_around", "description": _DESC_READ, "parameters": _PARAMS_READ})
    insert_at = len(fns) - 3
    if with_es:
        fns.insert(insert_at, {"name": "es_search", "description": _desc_es(with_grep), "parameters": _PARAMS_SEARCH})
    if with_graph:
        fns.insert(insert_at, {"name": "graph_neighbors", "description": _desc_graph(with_grep), "parameters": _PARAMS_GRAPH})
    # GEN-DIFF: ES/graph の可用性に依存しない土台系ツール（read_around 等と同じ扱い）＝常に含める。
    fns.append({"name": "compare_documents", "description": _DESC_COMPARE, "parameters": _PARAMS_COMPARE})
    if can_ask:
        fns.append({"name": "ask_user", "description": _DESC_ASK, "parameters": _PARAMS_ASK})
    return [{"functionDeclarations": fns}]


def graph_openai_tools() -> list:
    """管理グラフ質問用: 既存 graph_neighbors ツールだけを LLM に渡す。"""
    return [{"type": "function",
             "function": {"name": "graph_neighbors", "description": _DESC_GRAPH,
                          "parameters": _PARAMS_GRAPH}}]


def graph_gemini_tools() -> list:
    """管理グラフ質問用: 既存 graph_neighbors ツールだけを Gemini に渡す。"""
    return [{"functionDeclarations": [
        {"name": "graph_neighbors", "description": _DESC_GRAPH, "parameters": _PARAMS_GRAPH}
    ]}]


# ---- ツール実行（read-only・world＋scope に限定）----

def run_tool(name: str, args: dict, world: str, scope_paths,
            deadline: float | None = None, layer=None,
            max_hits: int | None = None, window_cap: int | None = None,
            tool_result_max_bytes: int | None = None) -> tuple[dict, set, list, list]:
    """ツールを実行し `(結果, 触れた doc_id 集合, 引用候補, 候補カード)` を返す。範囲外/未解決/秘匿は安全に error。

    引用候補＝`{doc_id, span, quote, ext}`（grep/ES ヒット由来・UI/出典用）。候補カード＝`graph_neighbors` 由来の
    原因候補（troubleshoot の UI/エクスポート用）。tool result の本文は **秘密を伏せて**返す。

    `layer`（省略可・`"docs"|"code"|"both"`・既定 `None`＝`"both"`＝フィルタなし＝既存呼び出し元は
    無変更）: 探す対象（調べ方ブロック §3.4）。`scope_paths` と同じ「会話ターン全体にかかる硬い
    フィルタ」——`ripgrep_search`/`glob_search`/`es_search`/`list_docs` の検索/列挙対象を絞り、
    `read_around`/`read_doc`/`doc_outline`（doc_id 単発読み）は層外の doc_id を scope 外と同型で
    拒否する（§8 裁定論点2）。`graph_neighbors` 自体はグラフ traversal（DOCUMENTS の言及エッジ
    （via="mention"）が木を跨いで Document とコードを繋ぐ・§3.5）なので層で結果を絞りはしないが、
    層が限定されている間はツール自体を拒否する
    （さもないと ripgrep_search/glob_search/es_search/list_docs/read_around/read_doc/doc_outline を
    絞っても graph 経由で層外の名前・経路・doc_id が漏れる迂回路になる）。

    `max_hits`（省略可・既定 `None`＝モジュール既定 `MAX_HITS`＝既存呼び出し元は無変更・SC-6c §3.2）:
    調べる深さ（調べ方ブロック）が計算した grep/ES ヒット上限の実効値。`ripgrep_search`
    （`grep_tool.grep_search` の `max_hits`）・`es_search`（`es_index.search` の `k`）へそのまま
    転送する。`window_cap`（省略可・既定 `None`＝モジュール既定 `READ_WINDOW`）: `read_around` の
    読み取り窓を2箇所で置き換える——① LLM が `window` 引数を省略したときの既定値、② 既存の
    `max(200, READ_WINDOW)` 安全クランプの `READ_WINDOW` 部分（200 の下限は維持）。どちらも
    呼び出し元（`openai_style`）が既に倍率計算済みの
    値を渡すだけで、本関数はクランプの形自体は変えない（LLM 自身が指定した値を上回らせない安全弁は
    維持）。`read_doc`（新設・土台系）の1回のページ幅にも `max(200, window_cap or READ_WINDOW)`
    ——read_around と同じ「200行フロア」の流儀を使う（read_doc に window 引数は無く、LLM は
    `start_line` を進めてページングするだけのため、window_cap/READ_WINDOW が小さくても
    数千行の文書を現実的なターン数で通読できるよう最低200行は返す）。ページ幅どおりに
    組んでから一括クリップすると `end_line` の申告と実際の `text` が食い違う（無言の欠落）ため、
    read_doc は1行ずつバイト予算を累積し、超える直前の行で止めてそこを実際の `end_line` にする
    （超過時は `text_truncated: true`）。`doc_outline`（新設・土台系）は行数上限を持たず、
    見出し件数（`_OUTLINE_MAX_HEADINGS`）とタイトルの累積バイト数の両方で打ち切る
    （`truncated`）。両者とも読み込みが `_READ_AROUND_FILE_CAP_BYTES` に達したら
    `file_truncated: true` を返す（`total_lines`/見出し一覧が文書全体でない可能性を明示）。
    `ripgrep_search` も同じ語彙を使う——`grep_tool.grep_search` が `_GREP_FILE_CAP_BYTES` で
    打ち切ったファイル由来のヒットにだけ `hits[i].file_truncated: true` を付与し、その文書は
    cap より後ろが検索できていない可能性があることを LLM に伝える（探す経路が黙って取りこぼす
    ことを防ぐ・読む経路の `file_truncated` と対称）。

    `deadline`（省略可・`time.monotonic()` 系の絶対期限。既定 None＝無期限＝既存呼び出し元は無変更）:
    `ripgrep_search`（`grep_tool.grep_search`）・`list_docs`/`glob_search`（`doc_ledger.
    documents_for`→`corpus_docs.world_documents`→`scope_infer.safe_files`）・`es_search`（`documents.
    world_rel_set`→`scope_infer.safe_files`）へそのまま転送する——いずれも同期的なツリー走査を
    伴い、`stop_event`（ターン境界でのみ確認）では中断できないため、実行中のツール呼び出し自体を
    打ち切る唯一の経路（PART-4 の watchdog が残り時間ベースで渡す・通常チャット経路は渡さない）。
    超過時は `grep_tool.GrepDeadlineExceeded`/`scope_infer.ScopeWalkDeadlineExceeded` を送出する
    （呼び出し元の既存のデッドライン優先の再分類で `ResearchTimeout`/504 になる）。

    `tool_result_max_bytes`（省略可・既定 `None`＝モジュール既定 `TOOL_RESULT_MAX_BYTES`＝既存
    呼び出し元は無変更・BUDGET-1 §3.4）: 呼び出し元（`openai_style`/`anthropic_style`/`gemini`）が
    run 開始時に1回だけ `resolve_tool_result_budgets()` で解決したツール結果1件あたりのバイト予算。
    `max_hits`/`window_cap` と同じ「会話ターン全体にかかる」上書き——本関数内のバイトクリップ
    （`_clip_utf8_bytes`/`_clip_cards`/`read_doc` の逐次クリップ）は全てこの実効値を使う。
    """
    sp = scope_mod.normalize_scope_paths(scope_paths) or None
    args = args or {}
    docs: set = set()
    cites: list = []
    cards: list = []
    # BUDGET-1: run 単位で snapshot 済みの値（無ければモジュール既定＝コード既定）。
    tr_max_bytes = tool_result_max_bytes if tool_result_max_bytes is not None else TOOL_RESULT_MAX_BYTES
    if name == "list_docs":
        from . import doc_ledger                          # 台帳＝world のフォルダ木を走査（鏡モデル・常に live）
        prefix = str(args.get("path_prefix") or "").strip().strip("/")
        pattern = str(args.get("name_pattern") or "").strip().lower()
        try:
            limit = int(args.get("limit") or 50)
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, 500))                    # read_around の window と同じ流儀でクランプ
        # 層判定は `doc_ledger`（`classify_document` 確定済み・§7 裁定10）の `branch=="source"` を
        # 使う（`layer_mod.in_layer`＝拡張子だけの近似は使わない・grep/ES と同じ確定判定に揃える）。
        rows = [r for r in doc_ledger.documents_for(world, deadline=deadline)
               if scope_mod.in_scope(r["name"], sp)
               and layer_mod.in_layer_code(r.get("branch") == "source", layer)]
        if prefix:
            rows = [r for r in rows if scope_mod.in_scope(r["name"], [prefix])]   # 同じ prefix 一致ロジックを再利用
        if pattern:
            rows = [r for r in rows if pattern in r["name"].lower()]
        # state（"ready"/"unreadable" 等）も通す——読み取り不可な文書を「使える」文書と同列に見せない。
        out = [{"rel_path": r["name"], "doctype": r.get("doctype"), "state": r.get("state", "ready")}
              for r in rows[:limit]]
        for d in out:
            docs.add(d["rel_path"])                        # 一覧に出した分だけ出典（sources）に載せる
        return ({"count": len(rows), "docs": out}, docs, cites, cards)
    if name == "folder_tree":
        # K6: フォルダはドキュメントではない（`docs`＝doc_id 集合には何も足さない・出典/引用の対象外
        # ——list_docs/glob_search が返す実ファイル rel_path とは異なる）。
        from . import folder_tree as folder_tree_mod
        result = folder_tree_mod.build(world, args, scope_paths=sp, deadline=deadline, layer=layer)
        return (result, docs, cites, cards)
    if name == "glob_search":
        from . import doc_ledger                          # list_docs と同じ台帳走査（鏡モデル・常に live）
        pattern = _validate_glob_pattern(args.get("pattern"))
        if pattern is None:
            return ({"error": "pattern が不正です（絶対パス・`..`・NUL・空文字・長すぎるパターンは使えません）"},
                    docs, cites, cards)
        match_pattern = _glob_match_pattern(pattern).lower()
        # scope/layer は list_docs と同じ確定判定（`branch=="source"`）を再利用する（§7 裁定10）。
        matched = [r["name"] for r in doc_ledger.documents_for(world, deadline=deadline)
                  if scope_mod.in_scope(r["name"], sp)
                  and layer_mod.in_layer_code(r.get("branch") == "source", layer)
                  and importance._match_segment_glob(match_pattern, r["name"].lower())]
        shown = matched[:_GLOB_MAX_RESULTS]
        for p in shown:
            docs.add(p)                                    # list_docs と同じ流儀＝出した分だけ出典に載せる
        return ({"count": len(matched), "paths": shown, "truncated": len(matched) > len(shown)},
                docs, cites, cards)
    if name in ("ripgrep_search", "es_search"):
        q = str(args.get("query") or "")
        degrade_reason = None
        truncated_docs: list = []                       # ripgrep_search のみ（es_search は空のまま）
        if name == "es_search":
            from . import documents                       # ES ヒットは現 world に**実在する doc** だけ採用
            # 古い ES 索引由来の 404／別内容リンクを引用/出典に出さない（非agentic の _es_citations と同じ実在チェック・rv-full2 #4）。
            # 実在集合は**1回だけ**作る（per-hit のツリー走査を避ける・rv MED）。
            valid = documents.world_rel_set(world, deadline=deadline)
            # RV2（FBK-1・2026-09-01）: `es_index.search()` は (hits, degrade_reason) を返す
            # （BM25 継続時の縮退理由・`embedding_cloud_unavailable`/`query_embed_failed` 等）。
            # ここではまだ tool result に生値のまま載せる（呼び出し元＝各 dialect のループが
            # `_degrade_result_node()` で既知語彙だけを思考ノードへ変換する）。
            # k_ceiling=MAX_HITS_ABS_MAX（grep と共通の絶対上限）で es_index 側の env 由来の
            # 再クランプ（既定 50）を迂回する——grep（下の分岐）には元々このような再クランプが無い。
            es_hits, degrade_reason = es_index.search(world, q, scope_paths=sp,
                                                      k=(max_hits or MAX_HITS), layer=layer,
                                                      k_ceiling=MAX_HITS_ABS_MAX)
            hits = [{"doc_id": h["doc_id"], "line": h.get("line"), "text": h.get("text", ""),
                     "span": [h.get("line"), h.get("line")], "ext": h.get("ext"),
                     "score": h.get("score"),                       # 親返し（L4c）の並び順にのみ使う・LLM 出力へは出さない
                     **({"locator": h["locator"]} if h.get("locator") is not None else {}),
                     **({"chunk_id": h["chunk_id"]} if h.get("chunk_id") is not None else {}),
                     **({"parent_id": h["parent_id"]} if h.get("parent_id") is not None else {})}
                    for h in es_hits
                    if h.get("doc_id") and h["doc_id"] in valid]
        else:
            # `truncated_docs`: `_GREP_FILE_CAP_BYTES` で打ち切られた文書の doc_id。**ヒット0件の
            # 打切り文書もここに載る**ため、ヒット経由の `file_truncated` では無音になるケース
            # （cap より後ろにしか一致が無い＝「検索したのに出てこない」）を LLM へ伝えられる。
            hits = grep_tool.grep_search(q, world, max_hits=(max_hits or MAX_HITS), scope_paths=sp,
                                         deadline=deadline, layer=layer, truncated_docs=truncated_docs)
        out = []
        # 親返し（L4c・§3.3/§3.4）: es_search 限定・既定 ON。rag チャンク由来のヒット（`chunk_id`
        # あり）は doc_id ごとに束ねて `_resolve_parent_return` へ渡し、legacy ヒット（`chunk_id`
        # 無し・40行チャンク由来）は従来どおり素通しする（`out` へ直接積む）。
        parent_return_on = name == "es_search" and _parent_return_enabled()
        rag_groups: dict = {}
        for h in hits:
            docs.add(h["doc_id"])
            quote = _redact(h["text"])[:500]            # redaction/clip は呼び出し側のポリシー（citations には入れない）
            # rag_chunks 由来（locator あり）は位置ヒントを LLM への text にだけ添える（SEARCH-CUT-3）。
            # citation の quote は hint 抜きのまま（redaction/500字上限は従来どおり適用済み・出典
            # フッターは doc_id リンクのみで locator は出さない・docs/04 契約は不変）。
            # hint は本文と結合してから redaction・500字上限を通す（RV MED-3: 先に切ってから足すと
            # 双方のガードを迂回する＝結合後にもう一度まとめて掛け直す）。`locator_hint` 自体も
            # 型検証・改行除去・長さ上限済みだが、ここでの redaction は本文と同じ扱いにする。
            hint = citations.locator_hint(h.get("locator"))
            text_for_llm = _redact(f"{h['text']}（位置: {hint}）")[:500] if hint else quote
            # 引用（cites）は tier に関わらず**子チャンク単位**のまま（親返しで粒度を落とさない・
            # §3.3「引用の粒度は落とさない」）——doc 単位への束ねは `out`（LLM 向け表示）にだけ効く。
            cites.append(citations.from_grep_hit(h, quote=quote, include_match=False))  # match 無し・整形は citations に集約
            if parent_return_on and h.get("chunk_id"):
                rag_groups.setdefault(h["doc_id"], []).append({
                    "chunk_id": h["chunk_id"], "parent_id": h.get("parent_id"),
                    "locator": h.get("locator"), "score": h.get("score"), "text": text_for_llm,
                })
                continue
            hit_view = {"doc_id": h["doc_id"], "line": h["line"], "text": text_for_llm}
            # I2（2026-09-05）: grep（`ripgrep_search`）ヒットが持つ登録者重要度（`grep_tool.
            # grep_search` が条件付きで付ける）を LLM 向け tool result にも転送する——重要文書を
            # 優先的に精読（read_around）できるようにする。es_search 側の `h` はこのキーを
            # 持たない（付けていない）ため、この条件付き追加は自然に ripgrep_search 限定になる。
            # 理由が無ければキー自体を作らない既存の流儀（`file_truncated` と同じ）。
            if h.get("importance"):
                hit_view["importance"] = h["importance"]
                if h.get("importance_reason"):
                    hit_view["importance_reason"] = h["importance_reason"]
            if h.get("file_truncated"):
                # `grep_tool.grep_search` の `file_truncated`（`_GREP_FILE_CAP_BYTES` で打ち切られた
                # ファイル由来のヒット）をそのまま LLM への tool result に転送する。読む経路
                # （read_doc/doc_outline の `file_truncated`）と同じ語彙＝この文書は cap より後ろが
                # 検索できていない可能性があることを、探す経路でも黙らせない。理由が無ければキーを
                # 作らない既存の流儀（`degrade_reason` 参照）＝通常のヒットは戻り値の形が完全に不変。
                hit_view["file_truncated"] = True
            out.append(hit_view)
        if rag_groups:
            # legacy ヒット分（`out` に既に積んだ分）を先に差し引いた残りが rag doc 群の予算
            # （§3.4「全文書ぶんの最低保証」は legacy を含めた tool result 全体の予算から見る）。
            legacy_bytes = sum(len(hv["text"].encode("utf-8")) for hv in out)
            budget_for_rag = max(0, tr_max_bytes - legacy_bytes)
            out.extend(_resolve_parent_return(world, rag_groups, sp, layer, budget_for_rag))
        view = {"hits": out}
        if degrade_reason:                              # es_search のみ・BM25 継続時の縮退理由（RV2）
            view["degrade_reason"] = degrade_reason
        if truncated_docs:                              # ripgrep_search のみ・打切りで探せていない文書
            view["truncated_docs"] = truncated_docs[:_TRUNCATED_DOCS_MAX]
        return (view, docs, cites, cards)
    if name == "graph_neighbors":
        if layer not in (None, "both"):
            # 正典 §3.4「範囲と同じ硬いフィルタ」: グラフ traversal 自体は§3.5により層フィルタ
            # 非適用（impact/troubleshoot は常に both で呼ばれる）だが、qa/author が層を限定した
            # ターンでこのツールを許すと、層外の名前・経路・doc_id を素通しする迂回路になる
            # （ripgrep_search/es_search/list_docs/read_around は層で絞っているのに graph だけ
            # 無制限では硬いフィルタにならない）。層が限定されている間はこのツール自体を拒否する。
            return ({"error": "指定した探す対象（層）では関係グラフの照会は使えません"}, docs, cites, cards)
        from . import lens_service                       # 遅延 import（循環回避）
        term = str(args.get("name") or "")
        raw_cards = lens_service.neighbor_cards(world, term, sp) if term else []
        # レビュー是正（FIX-2・secRV・2026-07-19・cards サイドカーのバイト迂回）: LLM 向け `view` は
        # 従来から `cards[:_GRAPH_CARDS_MAX]` で件数制限していたが、4つ目の戻り値（呼び出し元 3 dialect が
        # `cards += cd` で蓄積し、troubleshoot の `data.candidates` へ最終的に載るサイドカー）は
        # それとは独立に無制限で返しており、`total_tool_bytes`（1 run 累計バイト上限）の計測対象にも
        # 入らないため、Neo4j 側から巨大な候補集合（例 10万件）が返ると計測をすり抜けて蓄積し続ける。
        # Neo4j 側の取得件数上限（`lens_service`）は範囲外＝触らず、ここ（agentic_search）で受け
        # 取った後に件数＋直列化バイト上限でクリップする（超過分は捨てる・fail-closed）。
        clipped = _clip_cards(raw_cards, max_bytes=tr_max_bytes)
        # カード単位で裏付け doc の実在（world・scope 内）を検証し、無効カード（裏付け doc を
        # 主張したのに1件も実在しない）は cards・ツール結果（LLM への view）の両方から除外する——
        # 有効カードが集合に1枚でもあれば無効カードまで承認してしまう集約判定はしない（カードごとの
        # 判定）。doc を1件も主張しない card（純粋なグラフ位相情報等）は検証対象外＝そのまま通す
        # （「主張したのに裏付けが取れない」ことだけを問題にする）。
        cards = []
        for c in clipped:
            claimed_ids = _card_claimed_doc_ids(c)
            if not claimed_ids:
                cards.append(c)
                continue
            verified_ids = _card_verified_doc_ids(c, world, sp)
            if not verified_ids:
                continue
            docs |= verified_ids   # 出典付与は検証済み doc のみ（決定的 troubleshoot と同じく edge doc も含める）
            # EV-0（拡張設計 §4.4）: 呼び出し元（dialect のツールループ）がカード単位で Evidence digest
            # の1行（対象名・関係・経路・裏付け doc）を組めるよう、検証済み doc_id をカード自身へ
            # 同梱する（`_card_verified_doc_ids` を呼び出し元で再検証しない・二重コストを避ける）。
            c = {**c, "_verified_doc_ids": sorted(verified_ids)}
            cards.append(c)
        view = [{"name": c["name"], "role": c.get("role", ""), "category": c.get("category", ""),
                 "path": c.get("path", []), "distance": c.get("distance")} for c in cards]
        return ({"neighbors": view}, docs, cites, cards)
    if name == "read_around":
        doc_id = str(args.get("doc_id") or "")
        try:
            line = int(args.get("line") or 1)
            # LLM が window を省略した既定値にも window_cap（調べる深さが計算した実効値）を使う
            # （下の安全クランプは既存のまま維持）。
            window = int(args.get("window") or (window_cap or READ_WINDOW))
        except (TypeError, ValueError):
            return ({"error": "line/window は整数で"}, docs, cites, cards)
        # 上限は 200 を後退させず、`READ_WINDOW`（env）／`window_cap`（調べる深さ・SC-6c）が
        # 200を超えたときだけ追随する（既定・LLM 明示どちらの window 値にも同じ上限を適用する）。
        window = max(1, min(window, max(200, window_cap or READ_WINDOW)))
        # 層外も同型で拒否（§3.4・§8 裁定論点2）。`layer_mod.in_layer`（拡張子だけの近似）を事前に
        # 呼ばず、`_safe_doc_path` に `layer` を渡して `classify_document` 確定後の判定に一本化する
        # （grep/list_docs と同じ確定判定・拡張子だけの近似との不一致を避ける・§7 裁定10）。symlink
        # TOCTOU 対策（`_open_file_nofollow_walk` で1段ずつ open）は `_open_doc_stream` に集約済み。
        f, err = _open_doc_stream(world, doc_id, sp, layer)
        if err is not None:
            return (err, docs, cites, cards)
        # ストリーミング窓抽出（本丸・2026-09）: `line` は既知なので、窓の外まで読む必要が無い
        # （目標の終端 `e_target` に達したら即座に打ち切る＝ファイル全体を保持しない）。総行数
        # （`total_lines`）は read_around の結果に含まれないため、旧実装の `min(len(lines), ...)`
        # と違い、ここでは`e_target` を総行数へクランプする必要も無い——EOF が `e_target` より
        # 先に来れば、そこまでの内容が自然にそのまま結果になる（旧実装と同じ挙動）。
        s = max(0, line - 1 - window)              # 0-based 窓の開始（旧実装の `s` と同じ式）
        e_target = line - 1 + window + 1            # 0-based 窓の終端（排他・旧実装の `e` の式と同じ）
        collected: list[tuple[int, str]] = []
        try:
            _reader, it = _stream_doc_lines(f)
            for idx, t in enumerate(it):
                if idx >= e_target:
                    break
                if idx >= s:
                    collected.append((idx + 1, t))
        finally:
            f.close()
        text = _redact("\n".join(f"{i}: {t}" for i, t in collected))
        # secRV MED-B (a): 返却テキストの UTF-8 バイト数を上限で切り詰める（単一行が巨大な文書でも、
        # 履歴/SSE/次ターンの LLM 要求へ複製される量を bound する）。
        text = _clip_utf8_bytes(text, tr_max_bytes)
        docs.add(doc_id)
        return ({"doc_id": doc_id, "text": text}, docs, cites, cards)
    if name == "read_doc":
        doc_id = str(args.get("doc_id") or "")
        try:
            start = int(args.get("start_line") or 1)
        except (TypeError, ValueError):
            return ({"error": "start_line は整数で"}, docs, cites, cards)
        if start < 1:
            start = 1
        f, err = _open_doc_stream(world, doc_id, sp, layer)
        if err is not None:
            return (err, docs, cites, cards)
        # 1回のページ幅は read_around の「200行フロア」と同じ流儀（`max(200, window_cap or
        # READ_WINDOW)`）——window_cap/READ_WINDOW がそれより小さくても最低200行は読める
        # ようにし、数千行の文書でも現実的なターン数で通読できるようにする。
        page = max(200, window_cap or READ_WINDOW)
        # `total_lines` の申告には全行数のカウントが要る（cap まで／EOF までの全走査は避けられない）
        # が、行の**内容**はページ窓の外（`[start-1, target_end)` 外）なら保持しない——`target_end`
        # は総行数が確定する前に計算できる値（総行数で切り詰めた `page_end` と等価: 総行数が
        # `target_end` 未満なら EOF がそこで先に来るため、自然に `page_end` 相当になる）。
        target_end = start - 1 + page
        window_lines: list[str] = []
        total = 0
        try:
            reader, it = _stream_doc_lines(f)
            for idx, t in enumerate(it):
                if start - 1 <= idx < target_end:
                    window_lines.append(t)
                total += 1
        finally:
            f.close()
        file_truncated = reader.truncated or reader.line_overflowed
        if total and start > total:
            return ({"error": f"range 外です（start_line={start}・全{total}行）"}, docs, cites, cards)
        # ページ幅どおりに組んでから TOOL_RESULT_MAX_BYTES で一括クリップすると、`end_line`
        # （「ここまで読んだ」という申告）と実際に `text` に入っている内容が食い違う（無言の
        # 欠落）。1行ずつバイト予算を累積し、予算を超える直前の行で止めて、そこを実際の
        # `end_line` にする——1行目単独で予算を超える場合だけその1行をクリップして返し、
        # `text_truncated` で明示する。
        out_lines: list = []
        cum_bytes = 0
        actual_end = start - 1
        text_truncated = False
        for offset, wline in enumerate(window_lines):
            i = start - 1 + offset
            ln = _redact(f"{i + 1}: {wline}")
            ln_bytes = len(ln.encode("utf-8"))
            sep_bytes = 1 if out_lines else 0   # 結合する "\n" の分
            if cum_bytes + sep_bytes + ln_bytes > tr_max_bytes:
                if not out_lines:
                    out_lines.append(_clip_utf8_bytes(ln, tr_max_bytes))
                    actual_end = i + 1
                text_truncated = True
                break
            out_lines.append(ln)
            cum_bytes += sep_bytes + ln_bytes
            actual_end = i + 1
        docs.add(doc_id)
        result = {"doc_id": doc_id, "start_line": start, "end_line": actual_end,
                 "total_lines": total, "text": "\n".join(out_lines)}
        if text_truncated:
            result["text_truncated"] = True
        if file_truncated:
            result["file_truncated"] = True
        return (result, docs, cites, cards)
    if name == "doc_outline":
        doc_id = str(args.get("doc_id") or "")
        f, err = _open_doc_stream(world, doc_id, sp, layer)
        if err is not None:
            return (err, docs, cites, cards)
        all_headings: list = []
        total = 0
        try:
            reader, it = _stream_doc_lines(f)
            for idx, t in enumerate(it):
                m = _HEADING_RE.match(t.lstrip())
                if m:
                    title = _redact(m.group(2).strip())[:_OUTLINE_TITLE_MAX_CHARS]
                    all_headings.append({"line": idx + 1, "level": len(m.group(1)), "title": title})
                total += 1
        finally:
            f.close()
        file_truncated = reader.truncated or reader.line_overflowed
        # 件数上限（_OUTLINE_MAX_HEADINGS）に加え、タイトルの累積 UTF-8 バイト数でも打ち切る
        # （長い CJK タイトル×多数の見出しだと件数上限だけでは1結果が TOOL_RESULT_MAX_BYTES
        # を超えうる）。`count` は打ち切り前の総見出し数のまま（list_docs/glob_search と同じ流儀）。
        headings: list = []
        cum_bytes = 0
        truncated = len(all_headings) > _OUTLINE_MAX_HEADINGS
        for h in all_headings[:_OUTLINE_MAX_HEADINGS]:
            h_bytes = len(h["title"].encode("utf-8"))
            if cum_bytes + h_bytes > tr_max_bytes:
                truncated = True
                break
            headings.append(h)
            cum_bytes += h_bytes
        docs.add(doc_id)
        result = {"doc_id": doc_id, "total_lines": total, "count": len(all_headings),
                 "headings": headings, "truncated": truncated}
        if file_truncated:
            result["file_truncated"] = True
        return (result, docs, cites, cards)
    if name == "compare_documents":
        # GEN-DIFF（`docs/proposals/2026-09-03-世代間diff比較.md` §3〜§5）: 実装本体は独立モジュール
        # （`compare_docs.py`）——ここでは scope/deadline を渡して呼び、①出典（docs）への反映、
        # ②予算クリップ（他ツールと同じ `_clip_utf8_bytes`/`tr_max_bytes`）だけを担う。
        from . import compare_docs
        result = compare_docs.compare(world, args, scope_paths=sp, deadline=deadline)
        status = result.get("status")
        if status == "comparable":
            cc = result.get("compare_conditions") or {}
            for side in ("left", "right"):
                doc_id = (cc.get(side) or {}).get("doc_id")
                if doc_id:
                    docs.add(doc_id)
            diff_text = result.get("diff") or ""
            clipped = _clip_utf8_bytes(diff_text, tr_max_bytes)
            if clipped != diff_text:
                result = {**result, "diff": clipped, "truncated": True}
        elif status == "unsupported":
            for key in ("left_doc_id", "right_doc_id"):
                doc_id = result.get(key)
                if doc_id:
                    docs.add(doc_id)
        elif status == "needs_disambiguation":
            src = result.get("source_doc_id")
            if src:
                docs.add(src)
        return (result, docs, cites, cards)
    return ({"error": f"unknown tool: {name}"}, docs, cites, cards)


# ---- 思考ノード（agents.py に依存しない＝循環回避）----

_seq = [0]


def _nid() -> str:
    _seq[0] += 1
    return f"as-{_seq[0]}"


def _node(label: str, detail: str) -> dict:
    return {"type": "node", "id": _nid(), "kind": "tool", "label": label, "detail": detail, "status": "done"}


def _clip(s, n: int) -> str:
    return str(s or "").strip()[:n]


def _question_from_args(args: dict) -> dict:
    """ask_user tool args をフロントに出せる安全な質問イベントへ丸める。"""
    args = args or {}
    mode = args.get("mode") if args.get("mode") in ("single", "multiple") else "single"
    prompt = _clip(args.get("prompt"), 300) or "確認したいことがあります。"
    options = []
    for i, opt in enumerate((args.get("options") or [])[:8]):
        if not isinstance(opt, dict):
            continue
        label = _clip(opt.get("label"), 120)
        if not label:
            continue
        oid = _clip(opt.get("id") or opt.get("value") or label, 80) or f"opt-{i + 1}"
        options.append({"id": oid, "label": label, "description": _clip(opt.get("description"), 180)})
    if len(options) < 2:
        options = [{"id": "yes", "label": "はい", "description": ""},
                   {"id": "no", "label": "いいえ", "description": ""}]
    return {"type": "question", "interaction_id": _nid(), "mode": mode, "prompt": prompt,
            "options": options, "allow_free_text": bool(args.get("allow_free_text"))}


def _tool_node(name: str, args: dict) -> dict:
    if name == "list_docs":
        a = args or {}
        target = a.get("path_prefix") or a.get("name_pattern") or "全体"
        return _node("資料の一覧を確認", f"「{target}」")
    if name == "folder_tree":
        a = args or {}
        return _node("フォルダ構成を確認", f"「{a.get('path_prefix') or '全体'}」")
    if name == "ripgrep_search":
        return _node("資料を検索（grep）", f"「{(args or {}).get('query', '')}」")
    if name == "glob_search":
        return _node("ファイル名で検索", f"「{(args or {}).get('pattern', '')}」")
    if name == "es_search":
        return _node("資料を検索（全文/日本語）", f"「{(args or {}).get('query', '')}」")
    if name == "read_around":
        return _node("該当箇所を精読", f"{(args or {}).get('doc_id', '')} 付近")
    if name == "read_doc":
        return _node("文書を通読", f"{(args or {}).get('doc_id', '')}")
    if name == "doc_outline":
        return _node("見出し構造を確認", f"{(args or {}).get('doc_id', '')}")
    if name == "graph_neighbors":
        return _node("関係グラフをたどる", f"「{(args or {}).get('name', '')}」の関連部品")
    if name == "compare_documents":
        a = args or {}
        target = f"{a.get('left_doc_id', '')} / {a.get('right_doc_id', '')}" if a.get("left_doc_id") \
            else f"{a.get('source_doc_id', '')} → {a.get('target_generation', '')}"
        return _node("世代間の差分を比較", target)
    if name == "ask_user":
        return _node("ユーザに確認", (args or {}).get("prompt", "確認が必要です"))
    return _node(name, "")


# ツール名 → (label, detail) の固定文言（引数を一切埋め込まない・secRV MED-2 参照）。
_SUB_TOOL_FIXED_WORDING = {
    "list_docs": ("資料の一覧を確認", "資料の一覧を確認しています"),
    "folder_tree": ("フォルダ構成を確認", "フォルダ構成を確認しています"),
    "ripgrep_search": ("資料を検索（grep）", "資料を検索しています"),
    "glob_search": ("ファイル名で検索", "ファイル名で検索しています"),
    "es_search": ("資料を検索（全文/日本語）", "資料を検索しています"),
    "read_around": ("該当箇所を精読", "該当箇所を精読しています"),
    "read_doc": ("文書を通読", "文書を通読しています"),
    "doc_outline": ("見出し構造を確認", "見出し構造を確認しています"),
    "graph_neighbors": ("関係グラフをたどる", "関連部品をたどっています"),
    "compare_documents": ("世代間の差分を比較", "世代間の差分を比較しています"),
    "ask_user": ("ユーザに確認", "確認しています"),
}


def _tool_node_sub(name: str) -> dict:
    """サブ経路専用のツールノード（secRV MED-2・2026-07-18・ローカルサブの生成物が公式 UI/trace に露出）。

    `_tool_node` はモデル生成の引数（query/doc_id/path/prompt 等）をそのままノードの detail に
    埋め込む。サブ経路（`allowed_tools is not None`＝`_sub_agentic_loop` 経由）では、`name` 自体は
    許可済みツール集合（既知の固定名）に限られる安全な値だが、引数はモデル生成値のまま思考ノード
    （trace 保存対象）へ流れてしまうため、悪性資料に誘導されたモデルが任意文字列を UI/DB へ
    出せてしまう（プロンプトインジェクション）。ツール種別ごとの定型メッセージのみを返し、
    query/doc_id/path 等の引数は一切含めない。メイン経路（`allowed_tools is None`）はこの関数を
    使わず既存の `_tool_node`（豊かな表示）のまま＝byte-identical。
    """
    label, detail = _SUB_TOOL_FIXED_WORDING.get(name, (name, "処理しています"))
    return _node(label, detail)


# `es_search` の tool result に載る `degrade_reason`（`es_index.search()` の reason・RV2 参照）→
# 固定文言。BM25（キーワード一致）の結果は継続利用しつつ、精度が一部落ちていることを
# 「思考の流れ」に決定的に表示する（サーバログの warning だけでは利用者に届かない・RV2 是正）。
# 語彙は `es_query_failed`（hits 自体が空になる BM25 自体の失敗）を含まない——BM25 の結果を
# そのまま使えている場合（hits が空でない）だけを対象にする（`es_index.search()` docstring 参照）。
_ES_DEGRADE_WORDING = {
    "embedding_cloud_unavailable": ("検索の精度が一部低下しています",
                                    "選択中の AI での意味検索が使えないため、キーワード一致のみで探しています"),
    "query_embed_failed": ("検索の精度が一部低下しています",
                           "検索語の変換が一時的に失敗したため、キーワード一致のみで探しています"),
    # RV3（FBK-1・2026-09-01）: hybrid クエリ自体の失敗（次元不一致/未ベクトル索引等）で
    # BM25 のみへ降格した場合＝`query_embed_failed`（クエリ埋め込み自体が失敗）とは別原因だが、
    # 利用者向けの案内文は同じでよい（どちらも「意味検索は使えず、キーワード一致のみ」という
    # 結果は同じ）。
    "hybrid_query_failed": ("検索の精度が一部低下しています",
                           "意味検索の問い合わせが一時的に失敗したため、キーワード一致のみで探しています"),
}


def _degrade_result_node(result: dict) -> dict | None:
    """`run_tool()` の tool result に `degrade_reason`（既知語彙）があれば、その旨の思考ノードを
    返す（無ければ None）。呼び出し元は `run_tool()` 直後にこれを見て追加で1件 yield する
    （「ツール結果の合計サイズ上限」ノードと同じ、実行後に result を見て判定する既存の流儀）。
    """
    reason = isinstance(result, dict) and result.get("degrade_reason")
    wording = _ES_DEGRADE_WORDING.get(reason) if reason else None
    return _node(*wording) if wording else None


# `ripgrep_search` の tool result に載る `truncated_docs`（`grep_tool.grep_search` が
# `_GREP_FILE_CAP_BYTES` で打ち切った文書の doc_id・ヒット0件の打切り文書も含む・`run_tool` 参照）
# → 固定文言。内部語彙（doc_id・cap のバイト数等）は一切出さない（docs/04-画面の原則.md＝
# 専門用語ゼロ）——「一部の資料が大きすぎて全体を検索できていない」事実だけを利用者に伝える。
_TRUNCATED_DOCS_NODE_WORDING = ("検索が一部打ち切られています",
                                "一部の資料は大きすぎて全体を検索できていません")


def _truncated_docs_node(result: dict) -> dict | None:
    """`run_tool()` の tool result に `truncated_docs`（ripgrep_search のみ・非空）があれば、その旨の
    思考ノードを返す（無ければ None）。`_degrade_result_node` と全く同じ「run_tool 直後に result を
    見てもう1件 yield する」枠組みに1種類足すだけ——`es_search` の `degrade_reason` と同型の追加
    ノードで、フロント（`web/chat/*.js`）は既存ノードの kind/label/detail 契約のまま無改修で表示できる。
    """
    if isinstance(result, dict) and result.get("truncated_docs"):
        return _node(*_TRUNCATED_DOCS_NODE_WORDING)
    return None


# 「何を探して・いくつ当たったか」は run_tool の結果が出て初めて分かる。`_tool_node`/
# `_tool_node_sub` は実行**前**（結果不明の時点）に yield する固定ノードで、その yield 直後に
# stop_event を再確認してから run_tool を呼ぶ契約がテスト固定されている
# （`test_*_stop_event_set_during_node_yield_prevents_run_tool`）——件数はそのノード自体には
# 書けない。`_degrade_result_node` と同じ「run_tool 直後に result を見てもう1件 yield する」
# 流儀で追加ノードにする。
#
# `_tool_node`/`_tool_node_sub` の label とは別の専用 label にする（同じ label だと (a) 「実行
# された件数」を label で数える `executed_nodes` 集計系のテストを二重に拾う、(b) EXT-4 v2 の
# 同種操作集約（`render.js` の label キー）が開始ノードと結果ノードを同一操作と誤集約する、
# の両方が起きる）。メイン経路・サブ経路の追加ノードは同じ label を共有する
# （中身の詳しさだけが違う＝同じ「何のツールの結果か」を指す）。
_HIT_SUMMARY_LABELS = {
    "ripgrep_search": "検索結果（grep）",
    "glob_search": "検索結果（ファイル名）",
    "es_search": "検索結果（全文/日本語）",
    "graph_neighbors": "検索結果（グラフ）",
    "list_docs": "確認結果（一覧）",
    "folder_tree": "確認結果（フォルダ構成）",
    "read_around": "精読結果",
    "read_doc": "通読結果",
    "doc_outline": "見出し構造",
    "compare_documents": "比較結果",
}


def _tool_hit_count(name: str, result: dict) -> int | None:
    """run_tool() の結果から「ヒット件数」を数える（対象外のツール／エラー応答は None）。
    メイン経路・サブ経路の追加ノード（`_hit_summary_node`/`_hit_summary_node_sub`）が共通で使う。

    `es_search` は `degrade_reason` が `_ES_DEGRADE_WORDING`（BM25 継続時の3語彙）に含まれない
    既知値（`es_unavailable`/`es_query_failed`＝BM25 自体も失敗し hits が強制的に空になっている）
    のときも None にする——「検索は実行できたが0件だった」ことにはならないため、件数ノードで
    「0件（キーワード一致のみ）」と出すと実際には検索していないのに検索したかのような誤表示になる。
    """
    if not isinstance(result, dict) or "error" in result:
        return None
    if name == "ripgrep_search":
        return len(result.get("hits") or [])
    if name == "glob_search":
        return result.get("count", 0)   # list_docs と同じく打ち切り前の総件数（正確な母数を出す）
    if name == "es_search":
        reason = result.get("degrade_reason")
        if reason and reason not in _ES_DEGRADE_WORDING:
            return None
        return len(result.get("hits") or [])
    if name == "graph_neighbors":
        return len(result.get("neighbors") or [])
    if name == "list_docs":
        return result.get("count", 0)
    if name == "folder_tree":
        return result.get("count", 0)   # list_docs と同じく打ち切り前の総フォルダ数
    if name == "read_around":
        text = result.get("text") or ""
        return text.count("\n") + 1 if text else 0
    if name == "read_doc":
        return max(0, result.get("end_line", 0) - result.get("start_line", 1) + 1)
    if name == "doc_outline":
        return result.get("count", 0)   # list_docs/glob_search と同じく打ち切り前の総件数
    if name == "compare_documents":
        # 対応文書が決まらない/rag.md が無い run は「比較できた」件数として数えない
        # （es_search の degrade 同様、実行できなかったことを 0 件と混同しない）。
        if result.get("status") != "comparable":
            return None
        diff = result.get("diff") or ""
        return sum(1 for ln in diff.splitlines()
                  if (ln.startswith("+") or ln.startswith("-")) and not ln.startswith(("+++", "---")))
    return None


# EXT-4 v2（`web/chat/render.js::_updateLaneStats`）は `event_type` が無い kind:"tool" ノードを
# 「道具使用回数」として数える（`et === 'tool_started' || (e.kind === 'tool' && !et)`）。追加
# ノードにこの印を付けないと、実行1回につき開始ノード（`_tool_node`/`_tool_node_sub`・event_type
# 無し）＋本ノードの2件が数えられ、レーン統計の道具使用回数が2倍に水増しされる。`event_type=
# "tool_completed"`（`exec_event.EVENT_TYPES` の既存語彙）を付けて対象から外す。
def _hit_summary_dict(label: str, detail: str) -> dict:
    # `exec_event.build_event` は使わない——PART-4 外部API経路（research_service）は「実行経路が
    # v2 ビルダーを一度も呼ばない」実測契約（tests/unit/test_research_service.py::
    # test_no_exec_event_build_event_calls_during_successful_research）を持ち、本ノードは共有の
    # agentic ループから research でも流れる。表示専用ノードのため、同じ出力形を直接組み立てる
    # （event_type/kind の語彙整合は下の assert とテストで固定・`kind_for_event_type` は純関数）。
    return {"type": "node", "id": _nid(), "kind": exec_event.kind_for_event_type("tool_completed"),
            "label": label, "detail": detail, "status": "done", "event_type": "tool_completed"}


def _hit_summary_node(name: str, args: dict, result: dict) -> dict | None:
    """メイン経路（`allowed_tools is None`）向け: 検索語＋ヒット件数を1行にまとめた追加ノード
    （無ければ None）。`_tool_node` と同じく引数（query/name/doc_id 等）をそのまま detail に
    埋め込む＝メイン経路の既存の豊かな表示方針のまま（secRV MED-2 の対象外）。長い query は
    `_clip` で60字に丸める（UI 側の折返し/幅対策）。
    """
    n = _tool_hit_count(name, result)
    label = _HIT_SUMMARY_LABELS.get(name)
    if n is None or label is None:
        return None
    args = args or {}
    if name == "ripgrep_search":
        return _hit_summary_dict(label, f"「{_clip(args.get('query'), 60)}」→ {n}件")
    if name == "glob_search":
        return _hit_summary_dict(label, f"「{_clip(args.get('pattern'), 60)}」→ {n}件")
    if name == "es_search":
        # 縮退表示自体は `_degrade_result_node`（既存・別ノード）が変わらず担う——ここでは
        # 「実際に使われた検索方式」を短く添えるだけ（RV2/RV3 の degrade_reason は BM25 継続時の
        # 縮退理由＝立っていれば必ずキーワード一致のみになっている）。
        mode = "キーワード一致のみ" if result.get("degrade_reason") else "全文/意味検索"
        return _hit_summary_dict(label, f"「{_clip(args.get('query'), 60)}」→ {n}件（{mode}）")
    if name == "graph_neighbors":
        return _hit_summary_dict(label, f"「{_clip(args.get('name'), 60)}」の関連部品 → {n}件")
    if name == "list_docs":
        target = _clip(args.get("path_prefix") or args.get("name_pattern"), 60) or "全体"
        return _hit_summary_dict(label, f"「{target}」→ {n}件")
    if name == "folder_tree":
        target = _clip(args.get("path_prefix"), 60) or "全体"
        return _hit_summary_dict(label, f"「{target}」→ フォルダ{n}件")
    if name == "read_around":
        return _hit_summary_dict(label, f"{_clip(args.get('doc_id'), 60)} 付近 → {n}行")
    if name == "read_doc":
        return _hit_summary_dict(label, f"{_clip(args.get('doc_id'), 60)} → "
                                        f"{result.get('start_line')}〜{result.get('end_line')}行を読了"
                                        f"（全{result.get('total_lines')}行）")
    if name == "doc_outline":
        return _hit_summary_dict(label, f"{_clip(args.get('doc_id'), 60)} → 見出し{n}件")
    if name == "compare_documents":
        left = args.get("left_doc_id") or args.get("source_doc_id")
        right = args.get("right_doc_id") or args.get("target_generation")
        return _hit_summary_dict(label, f"{_clip(left, 60)} / {_clip(right, 60)} → 変更{n}行")
    return None


def _hit_summary_node_sub(name: str, result: dict) -> dict | None:
    """サブ経路（`allowed_tools is not None`）向け: `_tool_node_sub` と同じく、モデル生成の
    引数（query/doc_id 等）は一切使わない固定文言＋件数のみ（secRV MED-2 参照）。件数は
    run_tool の結果から数えた整数であり、モデル生成の自由文字列ではないため安全に出せる。
    """
    n = _tool_hit_count(name, result)
    label = _HIT_SUMMARY_LABELS.get(name)
    if n is None or label is None:
        return None
    if name == "read_around":
        detail = f"{n}行読み込みました"
    elif name == "read_doc":
        # secRV MED-2 の流儀（固定文言＋数値のみ）: start_line/end_line/total_lines は
        # モデル生成の自由文字列ではなく run_tool が検証・算出した整数のため安全に出せる。
        detail = f"{result.get('start_line')}〜{result.get('end_line')}行を読了（全{result.get('total_lines')}行）"
    elif name == "doc_outline":
        detail = f"見出し{n}件"
    elif name == "compare_documents":
        detail = f"変更{n}行を確認しました"
    else:
        detail = f"{n}件ヒットしました"
    return _hit_summary_dict(label, detail)


def _safe_json(s):
    try:
        return json.loads(s) if isinstance(s, str) else (s or {})
    except (ValueError, TypeError):
        return {}


# 同一プロバイダ内の限定リトライ（黙って別プロバイダへは切り替えない・url/headers/model は
# 呼び出し元から不変のまま渡す・同じ endpoint への再試行のみ）。
_POST_RETRY_ATTEMPTS = 2          # 初回失敗後に最大2回まで再試行（計3回試行）
_POST_RETRY_BACKOFF_SEC = 0.5     # 指数バックオフの基準値（0.5s→1.0s。429 は Retry-After 優先）
_RETRY_AFTER_CAP_SEC = 10.0       # Retry-After ヘッダを尊重する上限（暴走待ちを防ぐ）
_MIN_SEND_TIMEOUT_SEC = 1.0       # 待機後にこれ未満しか送信時間が残らないなら待たずに打ち切る


def _is_timeout_error(exc: Exception) -> bool:
    """応答タイムアウトか（`TimeoutError` 直接、または `URLError` が timeout を reason に包んだ形の
    どちらも見る）。タイムアウトは上流（プロバイダ側）で処理/課金が既に進んでいる可能性があり、
    再試行すると二重送信・二重課金になり得るため非リトライの全体契約とする（`_retryable_post_error`・
    `_run_evaluation` の両方が本関数を単一の真実源として使う）。"""
    if isinstance(exc, TimeoutError):
        return True
    return isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, TimeoutError)


_CONNECTION_FAILURE_ERRNOS = frozenset({errno.EHOSTUNREACH, errno.ENETUNREACH, errno.ENETDOWN})


def _is_connection_failure(exc: Exception) -> bool:
    """接続拒否・名前解決失敗・TLS 検証失敗・ホスト/ネットワーク到達不能（EHOSTUNREACH／
    ENETUNREACH／ENETDOWN）か（`urlopen` はこれらを `URLError` でラップし `reason` に原因例外を
    持つため、`exc` 自身に加えて `reason` も1段見る）。

    応答タイムアウト（`TimeoutError`／`socket.timeout`）はこの判定に含めない——全体デッドライン
    超過は別途 `ResearchTimeout`（504）が優先され、デッドラインに余裕が残っている per-call
    timeout は設定不備ではなく一時的な現象のため、旧来の汎用「時間をおいて再試行してください」
    文言のままにする。

    `sherpa/research_service.py`（PART-4）が「プロバイダに接続できない」旨の provider 名つき
    固定文言へ倒す判定・本関数直下の `openai_style` tail（最終合成/再合成）の `failure_kind`
    判定の単一の真実源（設定不備・上流の 4xx/5xx 応答等、プロバイダには繋がったが失敗した
    ケースは含まない）。

    **呼び出し元は LLM 送信由来の例外だけにこの判定を適用すること**——grep 等のツール実行由来の
    ファイル I/O 例外（SMB/NFS 切断の `ConnectionResetError` 等）が偶然同じ型を持つ場合の誤分類を
    避けるため、`_send`（本関数内のローカル関数）が物理送信の例外に付与する
    `_sherpa_llm_send_error` マーカーと必ず併用する（`getattr(e, "_sherpa_llm_send_error", False)
    and _is_connection_failure(e)`）。本関数直下の `openai_style` tail 自身の2箇所（最終合成/
    再合成の except）と `sherpa/research_service.py::run_research` の catch-all の両方がこの
    AND 条件を使う——`_send` の呼び出しを usage 加算・応答パースまで含む同じ try で囲む箇所は、
    型だけでは「実際に送信で失敗したか」を判別できないため。本関数自身は型だけを見て真偽を返す
    純粋関数のまま＝マーカー確認は呼び出し元の責務。
    """
    for c in (exc, getattr(exc, "reason", None)):
        if isinstance(c, (ConnectionError, socket.gaierror, ssl.SSLError)):
            return True
        if isinstance(c, OSError) and c.errno in _CONNECTION_FAILURE_ERRNOS:
            return True
    return False


def _retryable_post_error(exc: Exception) -> bool:
    """一時的な失敗（429・5xx・接続断）だけを再試行対象にする。401/404/400 等の設定起因の失敗、
    および**応答タイムアウト**は対象外＝即座に伝播させる（`_is_timeout_error` 参照）。"""
    if _is_timeout_error(exc):
        return False
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or 500 <= exc.code <= 599
    if isinstance(exc, urllib.error.URLError):
        return True
    return isinstance(exc, OSError)


def _retry_after_seconds(exc: Exception) -> float | None:
    """429 の `Retry-After` ヘッダを秒数で返す（数値／HTTP-date のどちらの形式も試す・
    `_RETRY_AFTER_CAP_SEC` で上限）。ヘッダが無い/解釈できなければ None（呼び出し元は指数
    バックオフへフォールバックする）。"""
    headers = getattr(exc, "headers", None)
    value = headers.get("Retry-After") if headers else None
    if not value:
        return None
    try:
        secs = float(value)
        # `float()` は "nan"/"inf"/"-inf" 等も受理してしまうため、有限の非負値だけを受理する
        # （NaN・負数・Infinity は不正値として扱い None＝指数バックオフへフォールバックさせる）。
        if not math.isfinite(secs) or secs < 0:
            return None
    except (TypeError, ValueError):
        try:
            import datetime
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(value)
            secs = (dt - datetime.datetime.now(dt.tzinfo)).total_seconds()
            if not math.isfinite(secs):
                return None
            secs = max(0.0, secs)   # HTTP-date が既に過去＝今すぐ再試行してよい（不正値ではない）
        except Exception:
            return None
    return min(secs, _RETRY_AFTER_CAP_SEC)


class _SendAborted(Exception):
    """`openai_style` の `_send`（呼び出し予算/usage/stop_event 込みのリトライ）が、再試行の
    途中で停止要求・呼び出し予算の枯渇を検出したときに送出する（呼び出し元は既存の
    stop_event/budget_exceeded 契約へ合流させる）。`reason` は "stop" か "budget_exceeded"。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _post(url: str, headers: dict, body: dict, timeout: int = 90) -> dict:
    """HTTP POST(JSON)→JSON（共通層へ委譲・単発・リトライなし）。**テストはこの関数を差し替える**
    （既存の広範なテスト seam＝1回だけ呼ばれる/差し替えた戻り値がそのまま返るという契約を保つ）。

    同一プロバイダ内の限定リトライ（429・5xx・接続断のみ・黙って別プロバイダへは切り替えない）は
    呼び出し元（`openai_style` の `_send`）が本関数を**物理送信のたびに1回ずつ**呼ぶことで組み立てる
    （呼び出し予算・usage 計測・stop_event・OpenAI 送信ガードの内側で「1物理送信=1消費」にするため
    ・`_retryable_post_error`/`_retry_after_seconds` 参照）。
    """
    return llm.post_json(url, headers, body, timeout)


# ---- F3（2026-07-07）: トークン使用量の合算（ツールループの全ターン分＝メイン回答呼び出し合計） ----
# 生トークンだけを合算し、provider/model の付与は呼び元（agents._agentic_run）が行う（この層は
# provider を知らない設計）。`final` イベントに `usage` を載せる（無ければ None）。
def _new_usage_acc() -> dict:
    return {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0}


def _usage_or_none(acc: dict):
    return acc if any(acc.values()) else None


def _n(v) -> int:
    try:
        return max(int(v or 0), 0)
    except (ValueError, TypeError):
        return 0


def _acc_openai_usage(acc: dict, resp: dict, ollama: bool) -> None:
    u = (resp or {}).get("usage") or {}
    if ollama and not u:                       # Ollama /api/chat（stream=false）はトップレベルの eval_count 系
        acc["input_tokens"] += _n(resp.get("prompt_eval_count"))
        acc["output_tokens"] += _n(resp.get("eval_count"))
        return
    pd = u.get("prompt_tokens_details") or {}
    cd = u.get("completion_tokens_details") or {}
    acc["input_tokens"] += _n(u.get("prompt_tokens"))
    acc["cached_input_tokens"] += _n(pd.get("cached_tokens"))
    acc["output_tokens"] += _n(u.get("completion_tokens"))
    acc["reasoning_output_tokens"] += _n(cd.get("reasoning_tokens"))


def _acc_gemini_usage(acc: dict, resp: dict) -> None:
    um = (resp or {}).get("usageMetadata") or {}
    acc["input_tokens"] += _n(um.get("promptTokenCount"))
    acc["cached_input_tokens"] += _n(um.get("cachedContentTokenCount"))
    acc["output_tokens"] += _n(um.get("candidatesTokenCount"))
    acc["reasoning_output_tokens"] += _n(um.get("thoughtsTokenCount"))


def _acc_anthropic_usage(acc: dict, resp) -> None:
    u = getattr(resp, "usage", None)
    def _g(key):
        return (u.get(key) if isinstance(u, dict) else getattr(u, key, None)) if u is not None else None
    read = _n(_g("cache_read_input_tokens"))
    creation = _n(_g("cache_creation_input_tokens"))
    acc["input_tokens"] += _n(_g("input_tokens")) + read + creation   # cached ⊆ input へ正規化
    acc["cached_input_tokens"] += read
    acc["output_tokens"] += _n(_g("output_tokens"))


# es_index.available() の接続タイムアウトと同じ桁数に揃える（SC-6e・per-turn 呼び出しのため長すぎない値）。
# 不達時に lock を握ったまま待つ時間の上限でもあるため、健全時に影響しない範囲で短く抑える。
# 既定1秒・env で上書き可（閉域の遅い Neo4j で誤不達判定＝明示ONの422へ倒れる環境向けの逃し弁）。
_GRAPH_AVAILABLE_TIMEOUT = float(os.environ.get("SHERPA_GRAPH_AVAILABLE_TIMEOUT", "1"))


def _graph_available() -> bool:
    """関係グラフ(Neo4j)ツール `graph_neighbors` を AI に提示するか。

    `es_index.available()` と対称に**実接続**を確認する（SC-6e）。以前は URI の有無だけを
    見ており、`world_neo4j.default_neo4j_uri()` が未設定時も `bolt://localhost:7687` へフォール
    バックして常に非空文字列を返すため、Neo4j 未起動でも常に True になっていた
    （`health._ping_neo4j` と同じ接続確認＝`GraphDatabase.driver(...).verify_connectivity()`）。
    """
    try:
        from neo4j import GraphDatabase

        from .ingest import world_neo4j
        env = world_neo4j._env()
        with GraphDatabase.driver(env["uri"], auth=(env["user"], env["pw"]),
                                  connection_timeout=_GRAPH_AVAILABLE_TIMEOUT,
                                  connection_acquisition_timeout=_GRAPH_AVAILABLE_TIMEOUT) as driver:
            driver.verify_connectivity()
        return True
    except Exception:
        return False


# 短TTL（既定20秒・数十秒程度）の process-local キャッシュ（SC-6e）。ES/Neo4j が即時
# 拒否せずタイムアウトする環境では `_graph_available()`/`es_index.available()` 1回のチェック
# だけで最大 2秒程度かかりうる——1ターン内で複数箇所（ルータの422判定・agentic既定toolset構築・
# 検索アシスタント複数本）が独立に呼ぶと直列加算されてしまっていた。`health.py::snapshot()` と
# 同じ「lock 内で丸ごと計算」方式＝同時 miss は先着1本だけが実際にチェックし、後続はロック解放後
# 新鮮なキャッシュをそのまま読む（single-flight）。


def _positive_finite_ttl(env_name: str, default: str) -> float:
    """TTL 系 env を「正の有限値」として解析する。他の env 駆動チューニング値
    （`es_index._env_float` 等の fail-safe クランプ）と異なり、不正値（0・負値・NaN・inf・
    非数値）を黙って既定へ丸めない——TTL がプローブ所要時間以下だと、待機側がロック取得直後に
    「期限切れ」と誤判定して single-flight（同時 miss の集約）自体が静かに壊れる。正しさに
    直結するため、不正値は起動時（本関数は import 時に評価される）に明示エラーで落とす。
    """
    raw = os.environ.get(env_name, default)
    try:
        val = float(raw)
    except (TypeError, ValueError):
        raise RuntimeError(f"{env_name} は数値で指定してください（現在値: {raw!r}）") from None
    if not math.isfinite(val) or val <= 0:
        raise RuntimeError(
            f"{env_name} は正の有限値で指定してください（0 以下・NaN・inf は不可・現在値: {raw!r}）")
    return val


_TOOLS_AVAILABILITY_TTL = _positive_finite_ttl("SHERPA_TOOLS_AVAILABILITY_TTL", "20")
_tools_availability_lock = threading.Lock()
_tools_availability_cache: dict = {"at": 0.0, "data": None}


def tool_availability(force: bool = False) -> dict:
    """検索経路3種（grep／全文・ベクトル(ES)／グラフ）の実接続に基づく可用性（SC-6e）。

    grep はローカルの文書ツリーを直接読むだけで外部依存が無いため常に True。UI（チップの表示
    可否・`GET /chat/tools-availability`）と実行側（デフォルトツール構築の AND ゲート・
    `chat_service._dispatch`/`providers/base._gather` の非agentic 経路）が**同じ判定関数**を
    共有する単一の真実源——`es_index.available()`/`_graph_available()` を個別に呼び分けない。

    短TTL（`_TOOLS_AVAILABILITY_TTL`）でキャッシュする（SC-6e）。呼び出し元は
    できる限り1ターンにつき1回だけ本関数を呼び、その結果（snapshot）を `tools_availability`
    引数として下流（`_dispatch`／`openai_style`等／`Ctx.tools_availability`）へ明示的に渡す——
    `toolset` を明示指定した呼び出し（検索アシスタント等）は本関数を一切呼ばない。TTL は
    その最終防衛線（snapshot が無い/失われた呼び出し元でも直列加算を短時間に抑える）。

    `force`（省略可・既定 `False`＝`sherpa.health.snapshot` と同じ流儀）: `True` のとき TTL
    キャッシュを無視して必ず再計算する（テスト・明示的な最新化用途）。既存呼び出し元は無変更。

    キャッシュの `at`（鮮度の起点）は**プローブ完了後**に記録する——プローブ開始前の時刻を
    使うと、TTL がプローブ所要時間以下の構成で待機側が「期限切れ」と誤判定し、single-flight
    （先着1本だけが実際にチェックし後続はロック解放後の新鮮なキャッシュを読む契約）が
    成立しなくなる（既定TTL=20秒・プローブ最大8秒程度では実害無いが、TTL を極端に短く
    構成する運用・テストでの誤判定を構造的に防ぐ）。

    正の短小 TTL（例: 1ms 未満）では上記だけでは不十分——ロック解放を待つ側は複数いて、
    ロックの受け渡し自体にも時間がかかるため、2番目以降の待機側が実際にロックを取得する頃には
    「今から見て」もう TTL を超えている、ということが起こる（20並行・probe20ms・TTL 1ms未満で
    実測: 待機側の一部が「期限切れ」と誤判定し直列 probe が再発する）。これを防ぐため、
    呼び出し側は**ロック取得前**に自分の呼び出し開始時刻 `call_start` を記録し、ロック内では
    「今から見て TTL 以内」か「`call_start` の**時点で待機を始めた後**に完成したキャッシュ世代か
    （`cache["at"] >= call_start`）」のどちらかを満たせば共有する——自分が呼び出した時点では
    まだ有効だった（または自分の待機中に新しい probe が完了した）キャッシュを、TTL 超過に
    見えるという理由だけで捨てて再 probe しない。`call_start` より前に完成した古い世代は
    対象外（それは自分の呼び出しより前から陳腐化していた可能性があるため、通常の TTL 判定に
    委ねる）。
    """
    call_start = time.monotonic()   # ロック取得前に記録（このcallerが要求した時刻）
    with _tools_availability_lock:
        cached = _tools_availability_cache["data"]
        at = _tools_availability_cache["at"]
        fresh_by_ttl = cached is not None and time.monotonic() - at < _TOOLS_AVAILABILITY_TTL
        # 自分が呼び出した後（ロック待機中を含む）に完成した世代なら、TTL超過に見えても共有する
        # （single-flight の待機側がロック受け渡しの遅延だけで再 probe してしまうのを防ぐ）。
        fresh_for_caller = cached is not None and at >= call_start
        if not force and (fresh_by_ttl or fresh_for_caller):
            return cached
        data = {"grep": True, "fulltext": es_index.available(), "graph": _graph_available()}
        _tools_availability_cache["at"] = time.monotonic()   # プローブ完了後に記録（上記docstring参照）
        _tools_availability_cache["data"] = data
        return data


def effective_tools_pref(tools_pref: dict | None, availability: dict | None = None) -> dict:
    """希望（`tools_pref`・省略=全ON）と可用性（`availability`・省略=全て利用可能扱い）の AND
    （SC-6e）。`dispatch_tools_for_lens`（非agentic の実行可否判定）と provider の
    `_agentic_loop`/`_sub_loop`（SYSTEM 節・§3.6 の実効集合）が共有する単一の計算——「要求∩可用」を
    2箇所で別々に書かない。
    """
    req = tools_pref_mod.normalize_tools_pref(tools_pref)
    avail = availability if availability is not None else dict(tools_pref_mod.DEFAULT_TOOLS_PREF)
    return {k: req[k] and avail.get(k, True) for k in req}


# 検索経路トグル（調べ方ブロック §3.6・SC-6e）で、このレンズの実行に必須なツールが全てOFF/不達の
# ときの固定文言。非agentic（`chat_service._dispatch`）・agentic
# （`providers/base._agentic_run`）の両経路が共有する。他は共通の既定文へ丸める
# （`_DISPATCH_REQUIRES_GRAPH` と対になる2値のみ）。
_TOOLS_BLOCKED_HEADLINE = {
    "impact": "影響分析はグラフ検索が必要です（現在OFFまたは利用できません）。"
             "「詳細」で検索経路のグラフをONにしてください。",
    "troubleshoot": "トラブルシュートはグラフ検索が必要です（現在OFFまたは利用できません）。"
                   "「詳細」で検索経路のグラフをONにしてください。",
}
_TOOLS_BLOCKED_HEADLINE_DEFAULT = ("資料の検索経路がすべてOFF/利用できません"
                                  "（「詳細」で grep・全文のいずれかを有効にしてください）。")


def tools_blocked_env(lens: str) -> dict:
    """このレンズを実行できない（必須ツールが全て OFF/不達）ときの honest-failure envelope
    （SC-6e）。`data: {}`（空 dict）＝`chat_service._no_genuine_results` の既存
    契約と同じ形（出典0件時の再検索案内・断定 headline 上書きの対象から自動的に外れる）。
    呼び出し元が `env["scope"]` を追加してから返す（`chat_service._dispatch`／
    `providers/base._agentic_run` 参照）。

    `_tools_blocked`（内部専用サイドカー）: `providers/base.py::_gather`（非agentic の trace）が
    この env を受け取った直後に pop して読む——実行できなかったことを trace ノードにも反映する
    （「N件を確認」という誤った完了表示にしない）ためだけの一時フラグで、公開 `answer`/永続化には
    残さない（`_evidence_committed` と同じサイドカー流儀）。agentic 経路（`_agentic_run`）は
    trace ノードの調整をこの時点で行わない（まだツール呼び出しノードを1つも出していない）ため、
    このサイドカーを使わず自分で pop して捨てる。
    """
    headline = _TOOLS_BLOCKED_HEADLINE.get(lens, _TOOLS_BLOCKED_HEADLINE_DEFAULT)
    return {"headline": headline, "summary": {"total": 0}, "data": {}, "sources": [],
           "_tools_blocked": True}


def unavailable_explicit_tools(tools_raw: dict | None, availability: dict | None = None) -> list:
    """`tools_raw`（HTTP 入口の生値・欠落キーを埋めない生の dict）のうち、明示的に `True` を
    指定したが実接続で到達不可なツール名（`tools_pref.TOOLS_PREF_KEYS` の正準順）。空リストは
    問題なし（省略/False のキーは対象外＝可用分だけを黙って使う既存契約のまま）。

    HTTP 入口（`routers/chat.py`）がこの戻り値を使って 422（ツール名つき・fail-loud）を返す
    （SC-6e）。

    `availability`（省略可・既定 `None`）: 呼び出し元がターン先頭で1回だけ計算した
    `tool_availability()` の snapshot。省略時のみ本関数が都度呼ぶ（後方互換・単体テスト用）。
    呼び出し元（`routers/chat.py::_validate_tools_availability`）は、この422判定と実行本体
    （`handle_message`/`stream_message`/背景ターン）へ**同じ snapshot** を渡す契約——別々に
    呼ぶと TTL 境界を挟んで受付時と実行時で可用性が食い違い、明示 `graph:true` が422を素通り
    した直後にグラフが不達として黙って無効化される窓ができる。
    """
    if not tools_raw:
        return []
    avail = availability if availability is not None else tool_availability()
    return [k for k in tools_pref_mod.TOOLS_PREF_KEYS
            if tools_raw.get(k) is True and not avail.get(k, True)]


# 非agentic経路（LLM の tool-use を経由しない決定的レンズ実行）のレンズ→必須ツール対応
# （SC-6e）。impact/troubleshoot はグラフ traversal が実装そのもの＝グラフ無しでは
# 実行できない。qa/author は grep（ripgrep_search）と ES（fulltext）のどちらか一方があれば
# 検索できる（両方 OFF/不達なら検索する手段が無い）。
_DISPATCH_REQUIRES_GRAPH = frozenset({"impact", "troubleshoot"})


def dispatch_tools_for_lens(lens: str, tools_pref: dict | None, availability: dict | None = None) -> tuple:
    """非agentic経路（`chat_service._dispatch`）が使う実効ツール判定（SC-6e）。エージェント
    検索（LLM の tool-use・`openai_style` 等）とは別の判定点——非agentic は grep/ES/グラフを
    「呼ぶか呼ばないか」の二値でしか選べず、LLM が動的にツールを選ぶ agentic 経路の `toolset`
    構築とは独立に判定する。

    `availability`（省略可・既定 `None`）: 呼び出し元（`chat_service.handle_message`/
    `stream_message`）がターンにつき1回だけ計算した `tool_availability()` の結果。ここでは
    計算しない——本関数（延いては `_dispatch`）を DB/ネットワーク非依存の単体テスト対象の
    ままにするため（`_dispatch` の `system_settings` と同じ「呼び出し元が読んで渡す」契約）。
    省略時は全て利用可能として扱う＝`tools_pref` の希望どおりに決まる（既存呼び出し元・
    単体テストは byte-identical）。

    返り値 `(effective, blocked)`。`effective` は `effective_tools_pref(tools_pref, availability)`
    （希望×可用性の AND）。`blocked` はこのレンズが実行不能（＝どの経路も残らない）かどうか——
    impact/troubleshoot はグラフ必須・qa/author は grep か fulltext のどちらかが必須。呼び出し元は
    `blocked` が真なら OFF になったツールへ黙ってフォールバックせず、明示エラーの envelope を返す
    （`tools_blocked_env` 参照）。
    """
    effective = effective_tools_pref(tools_pref, availability)
    if lens in _DISPATCH_REQUIRES_GRAPH:
        blocked = not effective["graph"]
    else:
        blocked = not (effective["grep"] or effective["fulltext"])
    return effective, blocked


# ---- EXT-3（拡張設計 §3）: 評価フェーズ（Observation → Evaluation → Next Action） ----
# 深度プロファイル（EXT-5 未実装）の内部簡易ノブ。既定 "light"＝評価フェーズは発動せず、
# `openai_style` の呼び出し元が明示的に `depth="medium"/"deep"` を渡したときだけ有効になる
# （既存呼び出し元は誰も渡さない＝既定 OFF・byte-identical。管理/利用者 UI はこのスライスでは作らない）。
EVAL_DEPTHS_ENABLED = ("medium", "deep")
# Research Cycle の境界（既存の MAX_TURNS＝Main Round 相当とは別軸）。既定 N=3 ターンごとに1回、
# 構造化評価（submit_evaluation）を挟む。
RESEARCH_CYCLE_TURNS = _env_int("SHERPA_AGENTIC_EVAL_CYCLE_TURNS", 3, 1, 20)
_EVAL_STATUSES = ("sufficient", "insufficient", "conflicting", "blocked")
_EVAL_NEXT_ACTIONS = ("commit_evidence", "continue_search", "read_more", "delegate_more", "stop")
_EVAL_TOOL = {"type": "function", "function": {
    "name": "submit_evaluation",
    "description": "ここまでの調査結果を評価する（十分/不足/矛盾/行き詰まりのいずれか）。",
    "parameters": {"type": "object", "properties": {
        "status": {"type": "string", "enum": list(_EVAL_STATUSES)},
        "reason": {"type": "string", "description": "判定理由（短く）"},
        "next_action": {"type": "string", "enum": list(_EVAL_NEXT_ACTIONS)}},
        "required": ["status", "reason", "next_action"], "additionalProperties": False}}}
_EVAL_NUDGE = ("ここまでの調査結果を評価してください。submit_evaluation を呼び、"
              "status（sufficient/insufficient/conflicting/blocked）・reason・next_action を返してください。")
_EVAL_RETRY_NUDGE = (
    "前回の応答は無効でした。ツール呼び出しは submit_evaluation を1回だけ、他のツールは呼ばずに行い、"
    "status と next_action の組み合わせを揃えてください（sufficient→commit_evidence／"
    "insufficient→continue_search か read_more／conflicting→continue_search か delegate_more／"
    "blocked→stop）。")
_EVAL_CONTINUE_NUDGE = "調査はまだ不十分と判定されました。ツールを使って調査を続けてください。"
# status と next_action の整合表（クローズド語彙の組み合わせ検証・§3.2）。
_EVAL_CONSISTENT_NEXT_ACTIONS = {
    "sufficient": frozenset({"commit_evidence"}),
    "insufficient": frozenset({"continue_search", "read_more"}),
    "conflicting": frozenset({"continue_search", "delegate_more"}),
    "blocked": frozenset({"stop"}),
}


def _eval_node(event_type: str, label: str, detail: str) -> dict:
    """評価フェーズの Execution Event（`exec_event.build_event` 経由・EXT-1 の加算的拡張を利用）。

    v1 の最小契約（id/kind/label/detail/status）は必ず埋まるため、v1 のままの古いフロント資産が
    残っていてもフラットな1ノードとして安全に描画される（余剰フィールドは無視されるだけ・
    `exec_event.py` docstring §2.3 参照）。
    """
    return exec_event.build_event(_nid(), exec_event.kind_for_event_type(event_type), label, detail,
                                  "done", event_type=event_type)


class _CallBudget:
    """共有 call 予算。check-and-decrement を lock で保護し、原子性の主張をコードで裏付ける
    （現行の `_run_sub_plan` は直列実行だが、将来 ThreadPoolExecutor 等で並列化しても安全なように
    lock を内包する）。
    """
    __slots__ = ("_lock", "remaining")

    def __init__(self, remaining: int):
        self._lock = threading.Lock()
        self.remaining = remaining

    def consume(self) -> bool:
        with self._lock:
            if self.remaining <= 0:
                return False
            self.remaining -= 1
            return True


def _resolve_timeout(timeout) -> int:
    """`timeout`（固定 int か 0引数 callable）をその時点の秒数へ解決する（`openai_style` 参照）。"""
    return timeout() if callable(timeout) else timeout


def _consume_call(call_budget: "_CallBudget | None") -> bool:
    """共有 call 予算（複数プロファイル横断予算の拡張・§6.2 項1）を原子的に1消費する。

    `call_budget` が None（既定・単発呼び出し元）なら常に True（無制限・既存呼び出し元は
    byte-identical）。`_CallBudget` を渡すと、通常ターン・評価・最終合成を含む**全ての `_post`
    発行直前**でこの関数を呼ぶことで、`SHERPA_SUB_PLAN_MAX_CALLS` 等の横断上限を種類を問わず
    一律に守れる（残数0で False＝呼び出し側は budget_exceeded として打ち切る）。この関数自体は
    1回の `_post` につき1回だけ呼ぶ（`_run_evaluation` が内部で消費するため、呼び出し元は
    `_run_evaluation` 呼び出しの前後で重ねて消費しない）。
    """
    if call_budget is None:
        return True
    return call_budget.consume()


def _parse_eval_response(resp: dict) -> dict | None:
    """`submit_evaluation` 応答の厳格検証（§3.2）。

    元の `tool_calls` が list かつ要素数**ちょうど1件**で、その唯一の関数名が `submit_evaluation`
    であることを先に確認する（他ツールとの混在・0件・複数件はすべて拒否）。続けて JSON 引数が
    status/next_action のクローズド語彙・reason が文字列であり、かつ status と next_action の
    組み合わせが `_EVAL_CONSISTENT_NEXT_ACTIONS` と一致するときだけ解析結果を返す。いずれか1つでも
    満たさなければ `None`（呼び出し側が再試行/blocked へ倒す）。
    """
    msg = ((resp.get("choices") or [{}])[0].get("message") if "choices" in resp
          else resp.get("message")) or {}
    tool_calls = msg.get("tool_calls")
    if not isinstance(tool_calls, list) or len(tool_calls) != 1:
        return None
    if (tool_calls[0].get("function") or {}).get("name") != "submit_evaluation":
        return None
    args = _safe_json((tool_calls[0].get("function") or {}).get("arguments"))
    status, next_action, reason = args.get("status"), args.get("next_action"), args.get("reason")
    if status not in _EVAL_STATUSES or next_action not in _EVAL_NEXT_ACTIONS or not isinstance(reason, str):
        return None
    if next_action not in _EVAL_CONSISTENT_NEXT_ACTIONS.get(status, frozenset()):
        return None
    return {"status": status, "reason": _clip(reason, 200), "next_action": next_action}


def _run_evaluation(endpoint: str, headers: dict, model: str, msgs: list, ollama: bool, timeout,
                    usage: dict, usage_acc: dict | None, call_budget: "_CallBudget | None" = None) -> dict:
    """Research Cycle 境界（または no-tool 終了時）での構造化評価（§3.2）。`msgs` はコピーへ評価
    ナッジを足すだけ（本流の会話履歴は汚さない）。`call_budget` の消費は本関数の中だけで行う
    （呼び出し元は本関数を呼ぶ前後で重ねて消費しない＝二重消費を避ける）。

    `submit_evaluation` 応答を `_parse_eval_response` で厳格検証する。1回失敗したらより強い
    ナッジで**1回だけ**再試行し、2回とも失敗（不正応答／関数名不一致・他ツール混在／
    status・next_action 不整合／通信・タイムアウト例外／call 予算超過）したら
    `blocked`（`evaluation_failed=True`）として返す——評価に失敗しても調査を無条件に継続させる
    「fail-open で insufficient」は、評価を強制する意味を失わせるため採らない。`blocked` は既存の
    「反復上限到達」最終合成へそのまま安全に合流する。予算超過（`call_budget` 消費不可）は
    `budget_exceeded=True` を追加で立てる。
    """
    nudge = _EVAL_NUDGE
    attempts = 0
    for _attempt in range(2):
        body = {"model": model, "messages": [*msgs, {"role": "user", "content": nudge}],
                "tools": [_EVAL_TOOL],
                "tool_choice": {"type": "function", "function": {"name": "submit_evaluation"}}}
        if ollama:
            body["stream"] = False
            body["options"] = {"temperature": 0.2}
        # OpenAI 経路はガード確認・予算消費・usage 加算を `llm.begin_openai_send()` で1つの
        # 原子的な塊として行う（`_send` と同じ・`llm.begin_openai_send` docstring 参照）。ガード
        # 拒否（`RuntimeError`）は try の外＝ここで飲み込んで別ナッジで再試行せず、そのまま
        # 呼び出し元へ伝播させる（「OpenAI へ送信できない」は評価応答の不備とは別種の理由であり、
        # 黙って続行しない）。
        if not ollama:
            try:
                llm.begin_openai_send(call_budget, usage_acc)
            except llm.SendBudgetExceeded:
                return {"status": "blocked", "reason": "call 予算の上限に達しました",
                        "next_action": "stop", "evaluation_failed": True, "budget_exceeded": True}
        else:
            if not _consume_call(call_budget):
                return {"status": "blocked", "reason": "call 予算の上限に達しました",
                        "next_action": "stop", "evaluation_failed": True, "budget_exceeded": True}
            if usage_acc is not None:
                usage_acc["calls"] += 1
        attempts += 1
        try:
            resp = _post(endpoint, headers, body, timeout=_resolve_timeout(timeout))
            _acc_openai_usage(usage, resp, ollama)
            if usage_acc is not None:
                usage_acc["tokens"] = _usage_or_none(usage)
            parsed = _parse_eval_response(resp)
            if parsed is not None:
                return {**parsed, "evaluation_failed": False}
        except Exception as e:
            if _is_timeout_error(e):
                # 応答タイムアウトは非リトライの全体契約に合わせる（`_is_timeout_error` 参照）＝
                # ナッジを変えての再試行もしない（上流で処理/課金が既に進んでいる可能性がある）。
                break
        nudge = _EVAL_RETRY_NUDGE
    # タイムアウトで打ち切ると試行は1回だけ（上の break）。実際の試行回数に文言を一致させる。
    reason = "評価応答の検証に2回失敗しました" if attempts >= 2 else "評価応答の検証に失敗しました"
    return {"status": "blocked", "reason": reason, "next_action": "stop", "evaluation_failed": True}


def _commit_evidence(cites: list, world: str) -> tuple[list, list, list]:
    """Candidate citation 列を Committed Evidence へ確定する（§4.3・§4.2）。

    重複排除は `citations.citation_dedupe_key`（citations.py と共通の鍵規則）。各 citation は
    `verify_citation` で機械検証し、`exists=False`（doc 不在／封じ込め違反／秘匿種別）は除外する
    （常時実施・ユーザー方針「機械的検証は深度に関わらず常時実施・人が AI の裏取りをしない」・
    TOGGLE-RM で明示 OFF 退避口を撤去済み）。検証機構自体が例外を投げた場合も `verification_error`
    として除外する（fail-closed・正確性優先——検証できないものを Committed Evidence 扱いにしない）。
    span 不一致（`span_unmatched`）は除外しない（`verify_citation` docstring 参照）。

    戻り値 `(committed, evidence_meta, dropped)`。`committed` は元の citation dict のまま
    （**キーを追加しない**＝citations.py の「公開形不変」契約を守る）。`evidence_meta` は
    committed と同じ順序で `{"doc_id","span","verification_method"}`（Evidence Packet 専用・
    `data.citations` には混ぜない）。`dropped` は `{"doc_id","reason"}`
    （`doc_missing`/`verification_error`）。
    """
    seen, deduped = set(), []
    for c in cites:
        if not c.get("doc_id"):
            continue
        k = citations.citation_dedupe_key(c)
        if k in seen:
            continue
        seen.add(k)
        deduped.append(c)
    committed, evidence_meta, dropped = [], [], []
    for c in deduped:
        try:
            v = verify_citation(c, world)
        except Exception:
            dropped.append({"doc_id": c.get("doc_id"), "reason": "verification_error"})
            continue
        if v.get("exists", True):
            committed.append(c)
            evidence_meta.append({"doc_id": c.get("doc_id"), "span": c.get("span"),
                                  "verification_method": v.get("method")})
        else:
            dropped.append({"doc_id": c.get("doc_id"), "reason": "doc_missing"})
    return committed, evidence_meta, dropped


def verify_doc_exists(doc_id: str, world: str, scope_paths=None) -> bool:
    """doc_id が world 内に**文書として実在**するかを確認する（`sources`＝出典フッターの DL
    リンク・graph card の裏付け doc を機械検証で絞る用途）。3つの独立したチェックを**すべて**
    満たす必要がある:

    (1) 実在: `documents.resolve`（`world_graph.resolve_path`・root 配下への直接解決が真実源）。
        `resolve_path` は world 配下の通常ファイルを**種別を問わず**解決するため、
        これだけでは `.env`・鍵・内部設定ファイル等も「実在文書」として通ってしまう。
    (2) 文書種別: `corpus_docs.status_document_doctype(doc_id, world)`（拡張子ベースの分類・
        `accepts()` 内容判定が必要な場合だけ実体を読む）が `None`（対象外の付帯物）でないこと。
        画像は「対応する派生 MD の有無」ではなく
        この doctype 分類で許可する——派生 MD の生成タイミングに依存させない（`verify_citation`/
        `_safe_doc_path` は read_around の本文読み取り用で解決先が派生 MD のため、生成が遅延/
        未完了だと実在する原本を誤って「存在しない」と判定する。「本文を読めるか」と「文書として
        実在するか」は別の問いで、本関数は後者だけを見る）。
    (3) scope: `scope_paths` を渡した場合、`scope_mod.in_scope(doc_id, scope_paths)` も満たす
        こと（grep/es_search 自体が scope 内に絞って返す契約だが、ここでも独立に多層防御する）。

    常時実施（TOGGLE-RM で明示 OFF 退避口を撤去済み・citation の機械検証と同じ規律を共有する）。
    """
    if scope_paths is not None and not scope_mod.in_scope(doc_id, scope_paths):
        return False
    try:
        from . import corpus_docs, documents
        if corpus_docs.status_document_doctype(doc_id, world) is None:
            return False
        return documents.resolve(doc_id, world) is not None
    except Exception:
        return False


def _card_claimed_doc_ids(card: dict) -> set:
    """1件の `graph_neighbors` card（troubleshoot 原因候補）が根拠として**主張する**（未検証・raw）
    doc（`evidence.grep[].doc_id`／`evidence.edges[].doc`）の集合を返す。
    """
    ev = card.get("evidence", {}) or {}
    doc_ids = {g.get("doc_id") for g in ev.get("grep", []) if g.get("doc_id")}
    doc_ids |= {e.get("doc") for e in ev.get("edges", []) if e.get("doc")}
    return doc_ids


def _card_verified_doc_ids(card: dict, world: str, scope_paths=None) -> set:
    """1件の card が主張する doc（`_card_claimed_doc_ids`）のうち、world 内に実在するものの集合を
    返す（カード単位の検証）。

    Neo4j 側は取り込み時点のスナップショットで、原本ファイルが後から削除/移動されても card 自体は
    残りうる（グラフの再構築は別トリガー）。裏付け doc を**主張したのに1件も実在しない** card は
    無効（呼び出し元＝`run_tool` が cards・ツール結果から除外する）——doc を1件も主張しない card
    （純粋なグラフ位相情報等）はこの検証の対象外（呼び出し元は主張の有無で先に分岐する）。
    常時実施（TOGGLE-RM で明示 OFF 退避口を撤去済み・citation の機械検証と同じ規律を共有する）。
    """
    doc_ids = _card_claimed_doc_ids(card)
    return {d for d in doc_ids if verify_doc_exists(d, world, scope_paths)}


def _card_graph_node_id(card: dict) -> str | None:
    """card（troubleshoot 原因候補）の安定したグラフ識別子＝`lens_service.neighbor_cards` が
    付与する内部専用 `cid`（Neo4j canonical_id＝label+world+path+name の同一性・MIRROR-MODEL
    §2.1・`ingest/world_graph._cid`）。`label:name` は**表示専用**——同一 label/name でも path
    （世代/フォルダ）が違えば別ノードであり区別できない（複製同名は別ノードという鏡モデルの契約に
    反する）ため、構造 Evidence の識別子には使わない。`cid` が非空文字列でなければ None を返す
    （呼び出し元＝`_card_graph_node_evidence` が昇格させない判断に使う）。
    """
    cid = card.get("cid")
    return str(cid) if isinstance(cid, str) and cid else None


def _card_structural_evidence(cards: list) -> list:
    """graph_neighbors のカードを**1枚＝1 Evidence**として構造 Evidence 化する（拡張設計 §4.4・
    Evidence digest はカード単位で対象名・関係・経路・裏付け doc を1行にまとめる）。

    裏付け doc を主張し検証済みのカード（`run_tool` が `_verified_doc_ids` を同梱済み）は、その
    doc_ids を `matched_doc_ids` に入れる。裏付け doc を1件も主張しない card（純粋なグラフ位相
    情報）は、Neo4j から実際に返ってきたノードであること自体が根拠——`lens_service.neighbor_cards`
    （ライブ Neo4j クエリ）から受け取った card の存在は文書のように後から削除/移動される心配のない
    即時の事実（`matched_doc_ids` には `cid` を1件だけ入れる）。裏付け doc を主張したのに検証で
    落ちた card は対象外（無効カードとして `run_tool` の graph_neighbors 分岐で既に `cards` 自体
    から除外されている）。

    `cid`（`_card_graph_node_id`）が無い claimless card は昇格させない（fail-open 防止——非一意な
    `label:name` を機械検証済みの根拠として扱わない・常時実施＝TOGGLE-RM で `label:name` への
    フォールバック退避口を撤去済み）。

    `doc_id` は常に `None`（1エントリが複数 doc を指しうるため単一 doc_id では表せない）。
    `card_meta`（対象名・関係・経路・グラフ上の生 label）は Evidence digest のテキスト整形に使う——
    `_dedupe_structural_evidence`（providers/base.py）は `matched_doc_ids`/`card_meta` も鍵に含めて
    重複排除する（`label` は鍵に含まれないため追加しても重複排除の挙動は変わらない）。`label`
    （`_troubleshoot_cards` が付与する生の Neo4j ラベル、例 "Program"）は、外部 API
    （`sherpa/research_service.py`）が内部 cid（`_card_graph_node_id`）を外部応答から除去した
    代わりに一意で追跡可能な表現（label+world+path）を組むために必要——`providers/base.py::
    _safe_card_meta` の allowlist（name/role/category/path）には含まれないため、chat 側の
    公開経路（`data.candidates`）には出ない。
    """
    out = []
    for c in cards:
        card_meta = {"name": c.get("name", ""), "role": c.get("role", ""),
                    "category": c.get("category", ""), "path": c.get("path", []),
                    "label": c.get("label", "")}
        verified_ids = c.get("_verified_doc_ids")
        if verified_ids:
            out.append({"doc_id": None, "span": None, "verification_method": "graph_verified",
                       "source_type": "graph", "matched_doc_ids": list(verified_ids),
                       "card_meta": card_meta})
            continue
        if _card_claimed_doc_ids(c):        # 裏付け doc を主張したが検証落ち＝無効カード（対象外）
            continue
        node_id = _card_graph_node_id(c)
        if node_id is None:                 # cid 無しは非一意な label:name で昇格させない
            continue
        out.append({"doc_id": None, "span": None, "verification_method": "graph_node_verified",
                   "source_type": "graph", "matched_doc_ids": [node_id], "card_meta": card_meta})
    return out


# ---- EV-0（拡張設計 §4.4）: 帰属（attribution）—— 回答完了後の非ストリーム呼び出し1回で確定する ----
# 本文には根拠申告用の制御構文を一切埋め込まない——ストリーム配信は常に byte-identical
# （保留なし）。帰属は**確定した回答本文**と Evidence digest（ev-N→事実）
# を、回答完了後の小さな非ストリーム呼び出し1回（attribution call）へ渡し、実際に使った ev-N を
# 構造化出力（openai_style は tool 強制呼び出し・他方言も tool/function-call 強制）で受け取ってから
# サーバー側で doc_id へ逆引きする。失敗・不正な応答・タイムアウト・call 予算切れはすべて空集合
# （read_around のみへ縮退）——リトライしない（帰属の失敗は「申告なし」として扱ってよい）。
#
# digest／帰属用回答コピーは**ツール結果と同じ露出**で組む（設計簡素化・2026-08-24）——生 doc_id・
# 実パス・list_docs の検索条件・graph の対象名/経路/裏付け doc（CID を含む）はそのまま載せる。
# 帰属呼び出しの送信先は回答合成と同じクラウド LLM で、ツール結果として既にこれらの原文を
# 受け取っている（閉域 LAN 前提・CLAUDE.md）ため、digest だけを別名化しても秘匿性は増えず、
# 帰属モデルが生値と対応付けられなくなる副作用の方が大きい。適用するのは**制御文字除去→
# `_redact`（既知の秘密パターンのみ）**だけ（`_digest_clean`）。

_ATTRIBUTION_MAX_ITEMS = 60          # digest 行数の上限
_ATTRIBUTION_MAX_BYTES = 16 * 1024   # digest 全体のバイト数上限（最終 UTF-8 列で厳密判定）
_ATTRIBUTION_QUOTE_CAP = 60          # citation quote の切り詰め長（`_facts()` の citation 表示と揃える）

_ATTRIBUTION_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u0085\u2028\u2029]")


def _digest_clean(text: str) -> str:
    """digest 1行分のテキストから制御文字・改行を除去し、`_redact`（既知の秘密パターンのみ）を
    通す（制御文字除去→redact の順で常に呼ぶ・切り詰めが必要な片は呼び出し元がこの後で `[:cap]`
    する——逆順にすると切断境界をまたぐ秘密パターンが漏れうる）。quote・条件・graph の対象名/経路・
    doc_id・実パス・CID 等、digest に載るテキストは全てここを通す（doc_id/パス/CID 自体は
    そのまま載せる・別名化はしない）。C0（`\x00-\x1f`）・DEL（`\x7f`）だけでなく C1
    （`\\x80-\\x9f`）・Unicode 行区切り（NEL `\\u0085`・LINE/PARAGRAPH SEPARATOR `\\u2028`/`\\u2029`）も
    空白化する——これらを通すと、digest 内で1件の quote/事実が複数「行」に割れて、偽装した
    `ev-N:` 風の文字列が帰属モデルへ別の Evidence 行として渡ってしまう（`ev-N` が実在キーなら
    ID 検証も素通りし、誤った `used`/`sources_verified` を招く）。
    """
    cleaned = _ATTRIBUTION_CONTROL_CHARS_RE.sub(" ", text or "").strip()
    return _redact(cleaned)


_ATTRIBUTION_TRUNCATION_NOTICE = "（上限のため以降の項目は省略）"


def build_evidence_digest(citations: list, combined_evidence_meta: list) -> tuple[str, dict]:
    """Evidence digest（`ev-N: 事実`）を組み立てる（拡張設計 §4.4）。

    ev-N の採番は `combined_evidence_meta`（citation 由来 `evidence_meta` ∪ 構造 Evidence）の
    添字＋1——Evidence Packet（`providers/base.py::_evidence_packet_evidence`）と共通の採番。

    citation は `citations`（`combined_evidence_meta` の先頭 `len(citations)` 件と同じ順序で
    1対1に対応する契約・`_commit_evidence`/`_dedupe_citations_and_evidence` の契約を踏襲）から
    **添字**で quote を引く——doc_id をキーにした辞書は使わない（同一 doc の複数 citation を
    異なる span で持つとき、doc_id キーの辞書だと最後の quote で上書きされてしまうのを避ける）。

    list_docs は呼び出し単位の集計 1 Evidence（総件数・条件・列挙範囲・0件の呼び出しも1件として
    持つ）、graph はカード単位の1 Evidence（対象名・関係・カテゴリ・経路・裏付け doc——`category`
    は `providers/base.py::_dedupe_structural_evidence` の重複排除鍵と整合させるため digest にも
    含める。含めないと、同名・同role・同path・同裏付け doc で category だけ異なる2枚が digest 上
    ev-N 以外同一行になり、帰属モデルが区別できない）。どちらも `matched_doc_ids`（0件以上の
    doc_id リスト）を持つエントリとして `structural_evidence_meta` 側に既に入っている
    （`_card_structural_evidence`／list_docs 構築箇所参照）。

    doc_id・実パス・list_docs の検索条件（path_prefix/name_pattern）・graph の裏付け doc（CID を
    含む）は**そのまま** digest 本文に載せる（拡張設計 §4.4・モジュール先頭の設計簡素化コメント
    参照）。各テキスト片は**制御文字除去→`_redact`→（該当すれば）切り詰め**の順で処理する
    （`_digest_clean` の後に `[:cap]` する・逆順だと切断境界をまたぐ秘密パターンが `_redact` の
    最小長を下回った断片として漏れうる）。

    件数上限（`_ATTRIBUTION_MAX_ITEMS`）・バイト上限（`_ATTRIBUTION_MAX_BYTES`）は**打切り注記を
    含めて**最終 `"\\n".join(lines)` の UTF-8 バイト数で厳密に判定する（許容スラックは無い）——
    注記を追加すると上限を超える場合は、注記自体が収まるまで末尾の Evidence 行を注記へ置換する
    （`_ATTRIBUTION_MAX_ITEMS` 行ちょうど・`_ATTRIBUTION_MAX_BYTES` バイトちょうどでも超過しない）。

    戻り値 `(digest_text, ev_map)`。`ev_map` は `{"ev-1": [doc_id, ...], ...}`——citation/graph の
    単一 doc 紐付けエントリは1要素リスト、list_docs/graph の集計/カード単位エントリは複数要素
    （0件のこともある）。`digest_text` が空文字なら帰属呼び出しはスキップする（citation/構造
    Evidence が1件も無い）。
    """
    lines: list = []
    costs: list = []          # lines[i] を追加した時点の増分バイト数（区切りの改行込み・pop で厳密に戻す）
    line_ev_ids: list = []    # lines[i] に対応する ev-N（pop 時に ev_map からも同期して消す）
    ev_map: dict = {}
    total_bytes = 0
    truncated = False

    def _marginal_cost(line: str) -> int:
        # 直前まで1行も無ければ改行区切りは要らない（"\n".join の実バイト数と厳密一致させる）。
        enc = len(line.encode("utf-8", errors="replace"))
        return enc if not lines else enc + 1

    def _add(ev_id: str, line: str, matched) -> bool:
        """1行追加を試みる。成功したときだけ `ev_map[ev_id] = matched` も同時に記録する
        （`lines`/`costs`/`line_ev_ids`/`ev_map` の4つを常に同じ添字・同じ集合で同期させる——
        後段の打切り注記挿入で末尾行を pop するとき、対応する `ev_map` エントリも一緒に消せる
        ようにする——さもないと digest 本文には無い ev-N が `ev_map` にだけ亡霊のように残る）。
        """
        nonlocal total_bytes, truncated
        if truncated or len(lines) >= _ATTRIBUTION_MAX_ITEMS:
            truncated = True
            return False
        b = _marginal_cost(line)
        if total_bytes + b > _ATTRIBUTION_MAX_BYTES:
            truncated = True
            return False
        lines.append(line)
        costs.append(b)
        line_ev_ids.append(ev_id)
        total_bytes += b
        ev_map[ev_id] = matched
        return True

    n_citations = len(citations)
    for i, m in enumerate(combined_evidence_meta):
        ev_id = f"ev-{i + 1}"
        matched = m.get("matched_doc_ids")
        if matched is not None:
            if "list_meta" in m:
                lm = m.get("list_meta") or {}
                cond_parts = [f"path_prefix={_digest_clean(lm['prefix'])}" if lm.get("prefix") else None,
                             f"name_pattern={_digest_clean(lm['pattern'])}" if lm.get("pattern") else None]
                cond = "、".join(c for c in cond_parts if c)
                cond_text = f"（条件: {cond}）" if cond else ""
                paths = "、".join(_digest_clean(d) for d in matched[:10])
                fact = (f"[list_docs] 該当 {lm.get('count', 0)} 件{cond_text}／列挙 "
                       f"{lm.get('shown', 0)} 件" + (f": {paths}" if paths else ""))
            else:
                cm = m.get("card_meta") or {}
                docs_text = "、".join(_digest_clean(d) for d in matched[:5])
                fact = (f"[graph] {_digest_clean(cm.get('name', ''))}"
                       f"（{_digest_clean(cm.get('role', ''))}"
                       f"{'・' + _digest_clean(cm['category']) if cm.get('category') else ''}"
                       f"・経路={_digest_clean(str(cm.get('path') or ''))}）"
                       + (f"／裏付け: {docs_text}" if docs_text else ""))
            _add(ev_id, _digest_clean(f"{ev_id}: {fact}"), list(matched))
            continue
        doc_id = m.get("doc_id")
        if not doc_id:
            continue
        if i < n_citations:
            # clean→redact を先に行ってから cap 文字数へ切り詰める（逆順だと切断境界をまたぐ
            # 秘密パターンが `_redact` の最小長を下回った断片として漏れうる）。
            quote = _digest_clean(citations[i].get("quote") or "")[:_ATTRIBUTION_QUOTE_CAP]
            fact = f"{_digest_clean(doc_id)}「{quote}」" if quote else _digest_clean(doc_id)
        else:
            fact = _digest_clean(doc_id)
        _add(ev_id, _digest_clean(f"{ev_id}: {fact}"), [doc_id])

    if truncated:
        # 打切り注記そのものを含めて上限（行数・バイト数）を満たすまで、末尾の Evidence 行を
        # 注記へ置換していく（「注記を足したら上限を超える」境界を無くす）。`_marginal_cost`
        # は現在の `lines` 状態に対する「これを追加したら増える厳密バイト数」を返す（改行の有無を
        # 現在の行数から判定する）ので、pop するたびに再評価すれば常に正確。pop した行に対応する
        # `ev_map` エントリも同時に消す——さもないと digest 本文には無い ev-N が `ev_map` にだけ
        # 残り、幻覚と紛らわしい亡霊エントリになる。
        while lines and (len(lines) >= _ATTRIBUTION_MAX_ITEMS or
                         total_bytes + _marginal_cost(_ATTRIBUTION_TRUNCATION_NOTICE) > _ATTRIBUTION_MAX_BYTES):
            total_bytes -= costs.pop()
            lines.pop()
            del ev_map[line_ev_ids.pop()]
        total_bytes += _marginal_cost(_ATTRIBUTION_TRUNCATION_NOTICE)
        lines.append(_ATTRIBUTION_TRUNCATION_NOTICE)
    return "\n".join(lines), ev_map


def resolve_attributed_doc_ids(attributed_ev_ids, ev_map: dict) -> set:
    """帰属呼び出しが返した ev-N の集合を、`ev_map`（`build_evidence_digest` の戻り値）で doc_id の
    集合へ逆引きする。digest に無い ev-N（幻覚・typo）は無視する（fail-closed・全 citation には
    広げない）。
    """
    if not attributed_ev_ids:
        return set()
    out: set = set()
    for e in attributed_ev_ids:
        out.update(ev_map.get(e) or [])
    return out


# `stop_reason`（evidence_packet・UI の「終了理由」の根拠）の閉じた語彙——本モジュール
# （openai_style/anthropic_style/gemini・共有の `_finalize_payload`/`_build_final_payload`/
# `_SendAborted`）が実際に生成する値だけを列挙する唯一の真実源。新しい stop_reason 文字列を
# どこかに書くときは必ずここにも足す（`plan_completed` は複数下調べ役の計画経路・退役済み
# `_run_sub_plan`（`providers/base.py`）だけが生成する値で、本モジュールからは到達不能なため
# 含めない）。対になる `web/chat/render.js::STOP_REASON_TOKEN_LABEL`（表示側の対応表）も
# 新しい値を足す/やめるときは両方更新する——
# `tests/unit/test_agentic_search.py::test_stop_reason_vocabulary_matches_render_js_display_table`
# が両者の一致を固定する。
STOP_REASONS = frozenset({
    "no_tool_calls",                 # 自然終了（ツール未呼び出しで応答・finish_reason が自然完了）
    "unknown",                       # 完了理由を判別できない（欠落・非文字列・既知のどの語彙にも
                                      # 無い値）——自然終了と偽らず「終了理由を確認できませんでした」
                                      # へ表示側で落とす専用の値（`no_tool_calls` へ丸めない）
    "truncated",                     # 出力上限で打ち切り（finish_reason が長さ上限系）
    "content_filtered",              # 内容フィルタで打ち切り（finish_reason が安全フィルタ系）
    "evaluation_sufficient",         # 自然終了（評価フェーズが「十分」と判定）
    "evaluation_blocked",            # 根拠不足で中断（評価フェーズが「行き詰まり」と判定）
    "turns_exhausted",               # 調査の上限に到達（MAX_TURNS 到達）
    "budget_exceeded",               # 調査の上限に到達（呼び出し予算 call_budget 枯渇）
    "tools_per_turn_exceeded",       # 道具の使用回数の上限に到達（1応答内の tool 呼び出し数上限）
    "refusal",                       # AI が回答を控えた（安全上の理由）
    "evidence_verification_failed",  # 根拠不足で中断（citation が全て機械検証で落ちた）
})
# STOP-1: 調査予算（ターン数／呼び出し予算／1応答あたりの道具の使用回数）到達で
# 打ち切られた3値——`providers/base.py::_agentic_run` がこの3値を「一般的な失敗」（空回答→単発
# grep フォールバック）から分離し、固定文言の headline と既存 Evidence Packet を最終 envelope へ
# 載せる根拠に使う（`web/chat/render.js::BUDGET_EXHAUSTED_STOP_REASONS` と同じ分類・そちらは表示側
# の注記表示可否の判定に使う独立実装＝値は必ず両方揃えて更新する）。
_BUDGET_EXHAUSTED_STOP_REASONS = frozenset({"turns_exhausted", "budget_exceeded", "tools_per_turn_exceeded"})
# EV-0（拡張設計 §4.4）: main の3方言・クリーン再合成が帰属呼び出しへ進んでよい「自然完了」の
# 完了理由 allowlist（方言別）——理由欠落・`content_filter`・`SAFETY`・打ち切り（openai/ollama
# 互換="length"・anthropic/bedrock="max_tokens"）等の未知/非自然な理由はすべて対象外（帰属を
# 省略し read_around のみへ縮退）。`providers/base.py::_NATURAL_COMPLETION_REASONS`（plan/hybrid
# の単発ストリーミング `_stream()` 向け）と同じ設計だが、ここは方言ごとの生応答から直接判定する
# ため方言別の集合に分ける（`openai_style` は ollama 兼用のため両方で同じ "stop" を使う）。
_OPENAI_STYLE_NATURAL_COMPLETION = frozenset({"stop"})
_ANTHROPIC_NATURAL_COMPLETION = frozenset({"end_turn", "stop_sequence"})
_GEMINI_NATURAL_COMPLETION = frozenset({"STOP"})

# EV-0 の自然完了 allowlist に無い finish_reason のうち、原因が判別できる代表2種は
# stop_reason（evidence_packet・UI の「終了理由」の根拠）にもその原因を伝搬する——ツール未呼び出し
# で応答が返っても、実際には出力上限・内容フィルタで打ち切られていた場合は「自然終了」と偽らない
# （正典 拡張設計 §4.4 の「未完了扱い」は EV-0 の帰属ゲートだけでなく stop_reason 自体にも反映
# する）。方言ごとに finish_reason の語彙が違うため方言別の集合を持つ。
_OPENAI_STYLE_TRUNCATED = frozenset({"length"})
_OPENAI_STYLE_CONTENT_FILTERED = frozenset({"content_filter"})
_ANTHROPIC_TRUNCATED = frozenset({"max_tokens"})
_ANTHROPIC_CONTENT_FILTERED: frozenset = frozenset()   # このAPI面には距離を置いた専用理由が無い（"refusal" は別途 stop_reason 自体になる）
_GEMINI_TRUNCATED = frozenset({"MAX_TOKENS"})
_GEMINI_CONTENT_FILTERED = frozenset({"SAFETY"})


def _incomplete_stop_reason(finish_reason, *, truncated: frozenset, content_filtered: frozenset) -> str:
    """ツール未呼び出しで応答が返った場合の stop_reason を、方言別の生 `finish_reason` から
    決める。自然完了（`_is_natural_completion` 判定対象）なら呼び出し元が `"no_tool_calls"` を
    使う契約——本関数は非自然完了のケースだけを受け取り、原因を判別できるものだけ専用の
    stop_reason（`"truncated"`＝出力上限で打ち切り・`"content_filtered"`＝内容フィルタで打ち切り）
    へ分ける。理由欠落・非文字列・既知のどの語彙にも無い値は `"unknown"` を返す——非自然完了と
    判定済みの経路であるため `"no_tool_calls"`（自然終了）へ丸めると原因不明を自然完了と偽ること
    になる（UI は `"unknown"` を「終了理由を確認できませんでした」として表示する・新しい断定は
    しない）。
    """
    if isinstance(finish_reason, str):
        if finish_reason in truncated:
            return "truncated"
        if finish_reason in content_filtered:
            return "content_filtered"
    return "unknown"


def _is_natural_completion(reason, allowed: frozenset) -> bool:
    """`reason`（方言の生の完了理由）が方言別の自然完了 allowlist に含まれるかを判定する
    （main 3方言の通常応答・クリーン再合成すべてで共通に使う）。`reason` が文字列でない場合
    （壊れた upstream 応答が `finish_reason`/`stop_reason`/`finishReason` へ dict/list/数値等を
    返した）は frozenset への `in` 判定で `TypeError`（非 hashable な値だと素通りせず例外になる）
    を出さず、常に False（未完了・帰属を省略し read_around のみへ縮退）を返す（fail-closed）。
    """
    return isinstance(reason, str) and reason in allowed


_ATTRIBUTION_TOOL = {"type": "function", "function": {
    "name": "submit_attribution",
    "description": "回答が実際に根拠として使った Evidence の ev-N を申告する（無ければ空配列）。",
    "parameters": {"type": "object", "properties": {
        "used": {"type": "array", "items": {"type": "string"},
                 "description": "実際に使った ev-N（例: 'ev-1'）のリスト。使った Evidence が無ければ空配列。"}},
        "required": ["used"], "additionalProperties": False}}}
_ATTRIBUTION_ANTHROPIC_TOOL = {"name": "submit_attribution",
                               "description": _ATTRIBUTION_TOOL["function"]["description"],
                               "input_schema": _ATTRIBUTION_TOOL["function"]["parameters"]}
_ATTRIBUTION_GEMINI_TOOLS = [{"functionDeclarations": [{
    "name": "submit_attribution", "description": _ATTRIBUTION_TOOL["function"]["description"],
    "parameters": _ATTRIBUTION_TOOL["function"]["parameters"]}]}]


def _openai_style_finish_reason(resp: dict) -> str | None:
    """OpenAI/Ollama 方言の応答から完了理由を取り出す（OpenAI 互換: `choices[0].finish_reason`・
    Ollama ネイティブ: `done_reason`）。取得できなければ None——理由欠落・非文字列（壊れた
    upstream 応答が dict/list/数値等を返した場合を含む）・`"stop"` 以外はすべて呼び出し元の
    自然完了 allowlist（`_is_natural_completion`）で未完了として扱う（旧来の「明示的に
    `"length"` のときだけ未完了」という denylist 判定は採らない）。
    """
    if not isinstance(resp, dict):
        return None
    if "choices" in resp:
        fr = ((resp.get("choices") or [{}])[0] or {}).get("finish_reason")
    else:
        fr = resp.get("done_reason")
    return fr if isinstance(fr, str) else None


def _openai_style_text(msg: dict) -> str:
    """OpenAI/Ollama 方言の `message` から表示すべき本文を取り出す。

    OpenAI の refusal（拒否）応答は `content=null`・`refusal="<拒否理由の文章>"`・
    `finish_reason="stop"` という形（正常な自然完了の一種・エラーではない）を取る——`content`
    だけを見ると空文字列に潰れ、finish_reason=stop（自然完了）と組み合わさって「モデルが空応答を
    返した」（実質的な合成失敗）と誤って区別できなくなる。`content` が空/欠落なら `refusal` へ
    フォールバックし、そちらも無ければ空文字列（従来どおり）。
    """
    return (msg.get("content") or msg.get("refusal") or "").strip()


def _attribution_prompt(answer_text: str, digest: str) -> str:
    return ("以下の【回答】が、下の【Evidence digest】のうちどの ev-N を実際に根拠として使ったかを"
           "判定してください。回答に実際に反映されている ev-N だけを挙げる——参照したが結局使わ"
           "なかったものは含めない。使った ev-N が無ければ空配列にする。\n\n"
           f"【回答】\n{answer_text}\n\n【Evidence digest】\n{digest}")


def _parse_attribution_ids(args, ev_map: dict) -> set | None:
    """`submit_attribution` の引数を厳格検証する（拡張設計 §4.4）。**部分的に正しい
    要素だけを拾って残りを黙って捨てる「部分受理」はしない**——一部でも不正なら None を返し、
    呼び出し元は申告全体を拒否して空集合（read_around のみへ縮退）として扱う。

    受理条件（すべて満たすときだけ集合を返す）:
    - `args` が dict で、キーが**厳密に** `{"used"}`（`additionalProperties: false` を宣言した
      ツール定義をモデルの実出力が守るとは限らないため、サーバー側でも再検証する）。
    - `used` が list で、要素は全て非空文字列。
    - 各要素は `ev_map`（`build_evidence_digest` が実際に digest へ載せた ev-N の集合）に
      **完全一致**で実在する——幻覚・typo の ev-N が1つでも混じれば申告全体を拒否する。
    - 重複が無い（同じ ev-N を複数回申告しない）。

    `used=[]`（空配列）は「使った Evidence なし」として正規に許可する。
    """
    if not isinstance(args, dict) or set(args.keys()) != {"used"}:
        return None
    used = args["used"]
    if not isinstance(used, list) or not all(isinstance(u, str) and u for u in used):
        return None
    if len(used) != len(set(used)):
        return None
    if not all(u in ev_map for u in used):
        return None
    return set(used)


def attribute_openai_style(endpoint: str, headers: dict, model: str, ollama: bool,
                           answer_text: str, digest: str, ev_map: dict, timeout,
                           usage: dict | None = None, usage_acc: dict | None = None,
                           call_budget: "_CallBudget | None" = None) -> set:
    """OpenAI/Ollama 方言の帰属呼び出し（`submit_attribution` の tool 強制呼び出し・非ストリーム・
    1回だけ）。`answer_text`/`digest` のどちらかが空なら呼ばない。失敗・不正な応答・タイムアウト・
    call 予算切れはすべて空集合（read_around のみへ縮退・リトライしない）。

    `timeout`: `openai_style` と同じく固定 int または 0引数 callable（`_resolve_timeout` で
    送信直前に解決する）。
    """
    if not answer_text or not digest or not ev_map:
        return set()
    body = {"model": model,
           "messages": [{"role": "user", "content": _attribution_prompt(answer_text, digest)}],
           "tools": [_ATTRIBUTION_TOOL],
           "tool_choice": {"type": "function", "function": {"name": "submit_attribution"}}}
    if ollama:
        body["stream"] = False
        body["options"] = {"temperature": 0.0}
    try:
        # OpenAI 経路はガード確認・予算消費・usage 加算を `llm.begin_openai_send()` で1つの
        # 原子的な塊として行う（`_send`/`_run_evaluation` と同じ・`llm.begin_openai_send`
        # docstring 参照）。本関数は「失敗はすべて空集合へ縮退・リトライしない」契約（上の
        # docstring）のため、ガード失敗・予算切れもこの except で空集合に丸める（`_send` と違い
        # 呼び出し元へは伝播させない）。
        if not ollama:
            llm.begin_openai_send(call_budget, usage_acc)
        else:
            if not _consume_call(call_budget):
                return set()
            if usage_acc is not None:
                usage_acc["calls"] += 1
        resp = _post(endpoint, headers, body, timeout=_resolve_timeout(timeout))
        if usage is not None:
            _acc_openai_usage(usage, resp, ollama)
            if usage_acc is not None:
                usage_acc["tokens"] = _usage_or_none(usage)
        msg = ((resp.get("choices") or [{}])[0].get("message") if "choices" in resp
              else resp.get("message")) or {}
        tool_calls = msg.get("tool_calls")
        if not isinstance(tool_calls, list) or len(tool_calls) != 1:
            return set()
        if (tool_calls[0].get("function") or {}).get("name") != "submit_attribution":
            return set()
        args = _safe_json((tool_calls[0].get("function") or {}).get("arguments"))
        ids = _parse_attribution_ids(args, ev_map)
        return ids if ids is not None else set()
    except Exception:
        return set()


def attribute_anthropic(client, model: str, max_tokens: int, answer_text: str, digest: str,
                        ev_map: dict, usage: dict | None = None,
                        call_budget: "_CallBudget | None" = None) -> set:
    """Anthropic Messages API の帰属呼び出し（`submit_attribution` の tool 強制呼び出し）。
    `attribute_openai_style` と同じ fail-closed 規則（失敗/不正/予算切れは空集合・リトライしない）。
    """
    if not answer_text or not digest or not ev_map:
        return set()
    if not _consume_call(call_budget):
        return set()
    if callable(client):
        client = client()
    kwargs = {"model": model, "max_tokens": max_tokens,
             "messages": [{"role": "user", "content": _attribution_prompt(answer_text, digest)}],
             "tools": [_ATTRIBUTION_ANTHROPIC_TOOL],
             "tool_choice": {"type": "tool", "name": "submit_attribution"}}
    try:
        resp = client.messages.create(**kwargs)
        if usage is not None:
            _acc_anthropic_usage(usage, resp)
        blocks = list(getattr(resp, "content", None) or [])
        tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
        if len(tool_uses) != 1 or getattr(tool_uses[0], "name", None) != "submit_attribution":
            return set()
        args = getattr(tool_uses[0], "input", None) or {}
        if not isinstance(args, dict):
            return set()
        ids = _parse_attribution_ids(args, ev_map)
        return ids if ids is not None else set()
    except Exception:
        return set()


def attribute_gemini(url: str, headers: dict, answer_text: str, digest: str, ev_map: dict,
                     usage: dict | None = None, call_budget: "_CallBudget | None" = None) -> set:
    """Gemini の帰属呼び出し（`submit_attribution` の function-calling 強制・`tool_config.mode=ANY`）。
    `attribute_openai_style` と同じ fail-closed 規則。
    """
    if not answer_text or not digest or not ev_map:
        return set()
    if not _consume_call(call_budget):
        return set()
    body = {"contents": [{"role": "user", "parts": [{"text": _attribution_prompt(answer_text, digest)}]}],
           "tools": _ATTRIBUTION_GEMINI_TOOLS,
           "tool_config": {"function_calling_config": {"mode": "ANY",
                                                        "allowed_function_names": ["submit_attribution"]}}}
    try:
        resp = _post(url, headers, body)
        if usage is not None:
            _acc_gemini_usage(usage, resp)
        parts = ((resp.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        calls = [p["functionCall"] for p in parts if isinstance(p, dict) and "functionCall" in p]
        if len(calls) != 1 or calls[0].get("name") != "submit_attribution":
            return set()
        args = calls[0].get("args") or {}
        if not isinstance(args, dict):
            return set()
        ids = _parse_attribution_ids(args, ev_map)
        return ids if ids is not None else set()
    except Exception:
        return set()


_RESYNTH_INSTRUCTION = (
    "次の依頼について、以下の根拠だけを使って、日本語で簡潔（2〜4文）に回答してください。"
    "確認できたことと確認できなかったことを分けて書き、根拠に無いことは書かない（推測しない）。\n\n"
    "【依頼】\n{question}\n\n"
    "【確認できた根拠】\n{digest}"
)


def _committed_evidence_digest(committed: list) -> str:
    """Committed Evidence（doc_id/span/quote）だけから再合成用の根拠一覧テキストを組む。

    ツール呼び出し履歴・落とした citation・モデルの前回ドラフト回答は一切含めない
    （クリーンな再合成コンテキスト＝落ちた根拠に基づく主張を新しい回答へ持ち越さないため）。
    """
    lines = [f"- {c.get('doc_id')}（span={c.get('span')}）: {c.get('quote', '')}"
            for c in committed if c.get("doc_id")]
    return "\n".join(lines)


def _clean_resynthesis_anthropic(client, model: str, system: str, question: str,
                                 mt: int, committed: list, usage: dict) -> tuple[str, str | None]:
    """Anthropic 経由のクリーン再合成——入力は **system＋現在の質問＋Committed Evidence digest**
    だけ（tools 無し）。通常の会話履歴（`history`）・ツール呼び出し履歴・モデルの前回ドラフトは
    一切渡さない（過去ターンの文脈や落ちた根拠に基づく主張を新しい回答へ持ち越さない）。
    失敗（例外・空応答・committed が空）は空文字列を返す（呼び出し元が honest failure として扱う）。

    戻り値は `(text, stop_reason)`——EV-0（拡張設計 §4.4）: この再合成コール自体が `max_tokens` で
    打ち切られた場合も、呼び出し元が帰属をスキップできるよう完了理由を一緒に返す。
    """
    digest = _committed_evidence_digest(committed)
    if not digest:
        return "", None
    messages = [{"role": "user", "content": _RESYNTH_INSTRUCTION.format(question=question, digest=digest)}]
    kwargs = {"model": model, "max_tokens": mt, "messages": messages}
    if system:
        kwargs["system"] = system
    try:
        resp = client.messages.create(**kwargs)
        _acc_anthropic_usage(usage, resp)
        blocks = list(getattr(resp, "content", None) or [])
        text = "".join(getattr(b, "text", "") for b in blocks
                      if getattr(b, "type", None) == "text").strip()
        return text, getattr(resp, "stop_reason", None)
    except Exception:
        return "", None


def _clean_resynthesis_gemini(url: str, headers: dict, system: str, question: str,
                              committed: list, usage: dict) -> tuple[str, str | None]:
    """Gemini 経由のクリーン再合成（`_clean_resynthesis_anthropic` と同じ最小コンテキスト方針・
    system＋現在の質問＋digest だけ・`history` は渡さない）。戻り値は `(text, finishReason)`。
    """
    digest = _committed_evidence_digest(committed)
    if not digest:
        return "", None
    contents = [{"role": "user",
                "parts": [{"text": _RESYNTH_INSTRUCTION.format(question=question, digest=digest)}]}]
    body = {"system_instruction": {"parts": [{"text": system}]}, "contents": contents,
            "generationConfig": {"temperature": 0.2}}
    try:
        resp = _post(url, headers, body)
        _acc_gemini_usage(usage, resp)
        cand = (resp.get("candidates") or [{}])[0]
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
        return text, cand.get("finishReason")
    except Exception:
        return "", None


def _finalize_payload(text: str, docs: set, searched: bool, committed: list, evidence_meta: list,
                      dropped: list, cards: list, usage, verified_docs: set, stop_reason: str,
                      evaluation: dict | None = None,
                      structural_evidence_meta: list | None = None,
                      used_evidence_docs: set | None = None,
                      attributed_ev_ids: set | None = None,
                      synthesis_failed: bool = False,
                      attribution_eligible: bool = False,
                      failure_kind: str | None = None) -> dict:
    """`{"final": ...}` イベントの共通組み立て（Committed Evidence 化は呼び出し元が済ませた状態で
    受け取る）。候補があったのに全滅した場合は `stop_reason` を `evidence_verification_failed` へ
    上書きする（honest failure）。

    呼び出し元が `committed`/`dropped` を先に検査してから最終テキスト（自然回答かクリーン再合成か）
    を決めたいケース（no-tool 終了時の Anthropic/Gemini 経路等）向けに、`_commit_evidence` の実行は
    ここでは行わない（同じ citation 列を二重検証しない）。citation 列をそのまま渡せる単純な
    呼び出し元は `_build_final_payload`（本関数の薄いラッパー）を使う。

    `structural_evidence_meta`（list_docs の呼び出し単位の集計 Evidence／graph_neighbors のカード
    単位 Evidence——`doc_id` は常に `None`・0件以上の `matched_doc_ids` を持つ）は citation を伴わない
    正当な回答（資料一覧・件数質問／グラフのみで根拠が得られた impact 等）を根拠ゲートが誤って
    落とさないための追加シグナル。`has_structural_evidence`（真偽値）は本 list が**1件以上**ある
    かどうかから導出する。Evidence Packet の `evidence[]`／`evidence_committed.evidence_ids` へ
    ev-* を割り当てる材料として base.py が使う（citation 由来の `evidence_meta` とは別枠のまま
    渡す＝双方を混ぜて重複排除しない）。

    `used_evidence_docs`（EV-0「根拠（精読済み）」の対象を「回答が実際に依拠した証拠」に絞るための
    シグナル・`providers/base.py::_committed_evidence_doc_ids` 参照）と `attributed_ev_ids`（同じ
    帰属結果を ev-N の生集合のまま持つ・Evidence Packet の `used` フラグを ev-N 単位で判定する
    ために使う——`matched_doc_ids` が0件の集計 Evidence は doc_id 交差では「使った」ことを表現
    できないため）は、帰属呼び出し（`attribute_openai_style`/`attribute_anthropic`/`attribute_gemini`
    ・拡張設計 §4.4）が確定回答本文＋Evidence digest から別途1回だけ判定した結果を呼び出し元が
    渡す（本関数はストリーム/本文からの抽出を一切行わない＝`text` は byte-identical のまま）。
    どちらも省略（None）は「帰属呼び出しを行わなかった／失敗した」を表し、空集合へフォールバック
    する（read_around のみへ縮退）。

    `build_evidence_digest` が実際に digest へ載せた ev-N の集合（`adopted_ev_ids`・
    `set(ev_map.keys())`）は payload には含めない——base.py 側の consumer は main 経路でこの値を
    読まない（main は自前の `_dedupe_citations_and_evidence` 再重複排除の後で Evidence Packet を
    組むため、ここで作った digest の添字と揃う保証が無く、意図的に絞り込みを適用しない・plan/hybrid
    は base.py 自身が `build_evidence_digest` を呼び直して**自分の** `adopted_ev_ids` をローカルに
    持つため、そもそも payload 側の値を必要としない）。
    """
    structural_evidence_meta = structural_evidence_meta or []
    has_structural_evidence = bool(structural_evidence_meta)
    if not committed and dropped and not has_structural_evidence:
        stop_reason = "evidence_verification_failed"
    used_evidence_docs = used_evidence_docs or set()
    attributed_ev_ids = attributed_ev_ids or set()
    payload = {"final": text, "docs": docs, "searched": searched, "cites": committed, "cards": cards,
              "usage": usage, "verified_docs": verified_docs, "stop_reason": stop_reason,
              "evidence_meta": evidence_meta, "dropped_citations": dropped,
              "candidates_seen": len(evidence_meta) + len(dropped),
              "has_structural_evidence": has_structural_evidence,
              "structural_evidence_meta": structural_evidence_meta,
              "used_evidence_docs": used_evidence_docs,
              "attributed_ev_ids": attributed_ev_ids,
              # PART-4（sherpa/research_service.py）向けの加算的フィールド（既存消費者は無視するだけ・
              # 挙動不変）。`synthesis_failed`: 最終合成/再合成の HTTP 呼び出しが例外を投げて
              # `candidate_text` が強制的に空文字へ縮退した場合 True（budget_exceeded 等の
              # 意図的な空文字とは区別する——そちらは呼び出し元が False のまま個別に yield する）。
              # `attribution_eligible`: この呼び出し内で（内部）帰属を実際に試みたら True
              # （`stop_event`/`finish_reason` の自然完了 allowlist を満たした場合のみ）。
              # `failure_kind`: `synthesis_failed=True` の原因を安全な分類値だけで表す
              # （生の例外は payload に載せない）。呼び出し元（`openai_style` tail）が
              # `_is_connection_failure` と送信元マーカーの両方で判定した結果を渡す・
              # それ以外は None（`research_service.py` は汎用の合成失敗文言を使う）。
              "synthesis_failed": synthesis_failed,
              "attribution_eligible": attribution_eligible,
              "failure_kind": failure_kind}
    if evaluation is not None:
        payload["evaluation_status"] = evaluation.get("status")
        payload["evaluation_reason"] = evaluation.get("reason")
        payload["evaluation_next_action"] = evaluation.get("next_action")
    return payload


def _build_final_payload(text: str, docs: set, searched: bool, cites: list, cards: list,
                         usage, verified_docs: set, stop_reason: str, world: str,
                         evaluation: dict | None = None,
                         structural_evidence_meta: list | None = None,
                         used_evidence_docs: set | None = None,
                         attributed_ev_ids: set | None = None) -> dict:
    """`_finalize_payload` の薄いラッパー。citation 列（Candidate のまま）を受け取り、ここで
    `_commit_evidence` を1回だけ実行してから共通組み立てへ渡す（緊急打ち切り経路でも未検証
    citation を外へ出さない）。
    """
    committed, evidence_meta, dropped = _commit_evidence(cites, world)
    return _finalize_payload(text, docs, searched, committed, evidence_meta, dropped, cards, usage,
                             verified_docs, stop_reason, evaluation, structural_evidence_meta,
                             used_evidence_docs, attributed_ev_ids)


# ---- 反復ループ（OpenAI 形式＝OpenAI/Ollama 共用 ／ Gemini 形式）----

def openai_style(endpoint: str, headers: dict, model: str, system: str, user: str,
                 world: str, scope_paths, ollama: bool = False, toolset: list | None = None,
                 stop_event=None, can_ask: bool = True, history: list | None = None,
                 max_turns: int | None = None, timeout=90,   # int または 0引数 callable（docstring 参照）
                 allowed_tools=None,
                 usage_acc: dict | None = None, shared_budget: dict | None = None,
                 final_synthesis: bool = True, depth: str = "light",
                 call_budget: "_CallBudget | None" = None,
                 tool_deadline: float | None = None, layer=None,
                 max_hits: int | None = None, window_cap: int | None = None,
                 tools_pref: dict | None = None, tools_availability: dict | None = None):
    """OpenAI/Ollama の tool-use を反復。`{"node":..}` を yield しつつ最後に `{"final","docs"}`。

    `layer`（省略可・既定 `None`＝`"both"`＝既存呼び出し元は無変更）: `scope_paths` と同じく
    `run_tool` へそのまま転送する探す対象フィルタ（調べ方ブロック §3.4）。

    `tools_pref`（省略可・既定 `None`＝全 ON＝既存呼び出し元は無変更・SC-6e）: 検索経路トグル
    （`tools_pref.normalize_tools_pref` 参照）。`toolset` を明示指定した呼び出し（`graph_admin`等）
    では無視される（`toolset` が既に確定済みのツール定義配列のため）。`toolset` 省略時のみ、
    可用性（`tools_availability`）と AND を取ってデフォルトの `openai_tools()` を組み立てる——
    利用者がこの3経路のうち何を許可したかに関わらず、そもそも到達不可なツールは元々提示されない。
    `tools_availability`（省略可・既定 `None`＝`tool_availability()` を都度呼ぶ・SC-6e）:
    呼び出し元（provider の `_agentic_loop`）がターン先頭で1回だけ計算した `tool_availability()`
    の結果（`Ctx.tools_availability`）。ES/Neo4j が即時拒否せずタイムアウトする環境では可用性
    確認1回に数秒かかりうるため、`toolset` 省略時（＝ここでこの判定が要る場合）は呼び出し元が
    必ずこの引数で渡す契約——本関数自身は都度チェックを再実行しない（`toolset` 明示指定時は
    本引数・可用性判定のどちらも一切参照しない）。省略時（テスト等）だけ `tool_availability()`
    （TTL キャッシュつき）へ後方互換フォールバックする。

    `tool_deadline`（省略可・`time.monotonic()` 系の絶対期限。既定 None＝無期限＝既存呼び出し元は
    無変更）: メインループの `run_tool` 呼び出しへそのまま転送する（`run_tool`/`grep_tool.
    grep_search` docstring 参照）。`stop_event`（ターン境界でのみ確認）は実行中の同期的なツール
    呼び出し自体（例: ripgrep_search のツリー全文検索）は中断できないため、これとは別の経路として
    用意する（PART-4・`research_service.py` がリクエスト全体の絶対デッドラインを渡す）。

    `timeout`: 固定 int（既存呼び出し元は byte-identical）に加え、**0引数 callable**（呼ぶたびに
    その時点の秒数を返す関数）も受け付ける（PART-4・`research_service.py` がリクエスト全体の
    絶対デッドラインから残り時間を都度計算して渡すために追加）。本関数がこの `timeout` を
    そのまま転送する先（通常ターン・再合成・最終合成の3箇所の `_send`・`attribute_openai_style`・
    `_run_evaluation`）は、いずれも実際の HTTP 送信直前で `_resolve_timeout(timeout)` を呼んで
    解決する——固定値を関数の入口で1回だけ評価してターン間/呼び出し間で使い回すと、絶対デッドライン
    超過後の呼び出しにも古い（大きい）タイムアウトが渡ってしまう。callable を未解決のまま
    `urllib`（数値以外を受け付けない）へ渡すと送信自体が例外で失敗し、`usage_acc["calls"]` だけが
    「試みた」として計上される（失敗の中身が「タイムアウト値の型エラー」という無意味なものになる）
    ため、転送経路すべてで解決を徹底する。

    `stop_event`（UI フィードバック1「途中停止」の RV MEDIUM 再検証・2026-07-03）: 各ターンの
    リクエスト発行前に確認し、立っていれば以降のリクエストを一切発行せず終了する（HTTP 呼び出し
    自体の中断は不要＝次のターン境界で止まれば足りる、という設計）。`final` を yield せずに
    `return` するだけ＝呼び元（`agents._agentic_run`）は「未応答」として扱い、fallback を試みない
    （呼び元側でも stop_event を確認し、停止時は fallback をスキップする＝二重の無駄な処理を避ける）。
    `history`（R1a・会話継続）: 直前ターンの (user, assistant) 対（時系列順・上流でキャップ済み）。
    system の直後・現在の user メッセージの前に並べる。省略/空なら従来と完全同一の初期 msgs になる。
    `graph_admin.ask_graph` は位置引数で呼ぶため本引数に触れない＝既定 None（空）で後方互換。

    `max_turns`（S3・§5.0 guard.max_turns）: 省略（None）ならモジュール既定 `MAX_TURNS` を使う
    （既存呼び出し元は byte-identical）。`timeout`（S3・guard.llm_timeout）: 既定 90 で `_post` へ
    そのまま渡す（省略時は従来と同じ既定値）。

    `max_hits`/`window_cap`（省略可・既定 `None`＝既存呼び出し元は無変更・SC-6c §3.2）: 調べる深さ
    （調べ方ブロック）が計算した grep/ES ヒット上限・読み取り窓の実効値。`run_tool` へそのまま
    転送する（`layer`/`scope_paths` と同じ「会話ターン全体にかかる」上書き・LLM 自身の呼び出し
    ごとの `max_hits`/`window` 指定はこの上限まででクランプされる）。
    `allowed_tools`（S3・プロファイルのツール制限・二重強制の(b)）: 非 None のとき、ツール呼び出し名が
    この集合に無ければ `run_tool` を呼ばず「このサブエージェントは <name> を使えません」という
    ツール結果でループを継続する（例外にしない・ask_user も対象＝ツール定義配列を絞る (a) をすり抜けて
    モデルが未提示のツール名を呼んだ場合の多層防御）。既定 None は無制限（既存呼び出し元は無変更）。
    非 None＝サブ経路の合図でもある: 許可済みツール呼び出しのノードは `_tool_node`（args を含む豊かな
    表示）ではなく `_tool_node_sub`（args を含まない固定文言）になる（secRV MED-2・2026-07-18）。

    `MAX_TOOLS_PER_TURN`（secRV MED-3・2026-07-18・DoS 対策）: 1 応答内の tool_calls 実行数を独立に
    上限する（`max_turns` は応答ラウンド数だけを制限し、1 応答内の呼び出し数は無制限だった）。超過分は
    `run_tool` を呼ばずに打ち切り、`stop_event` も各ツール実行の直前に確認する（メイン/サブ経路の
    両方に適用＝`allowed_tools` の有無に関わらず一律）。

    レビュー是正（LOW-D・secRV・2026-07-18 再検証）: 超過分（例: 1応答に10万件の tool_calls）に対して
    「上限」ノードを超過件数と同数（99,984件）生成し SSE/trace を肥大化させていた。是正後は
    `calls[:MAX_TOOLS_PER_TURN]` だけを処理し、超過があればループ終了後に**固定ノード1件だけ**生成
    して打ち切る。

    レビュー是正（LOW-E・secRV・2026-07-18 再検証）: ツールノードを yield した直後（generator が
    一時停止し、呼び出し元がノードを処理してから再開される窓）に停止要求が来ても、再開後は
    stop_event を再確認せず ask_user 分岐/`run_tool` を1件実行してしまっていた。是正後はノード yield
    直後・ask_user 分岐/`run_tool` の直前にも stop_event を再確認する。

    レビュー是正（MED-B (c)・secRV・2026-07-18）: `run_tool` の戻り値（tool result）の累計バイト量
    （1 run＝本関数の1呼び出し全体）が `TOOL_RESULT_MAX_TOTAL_BYTES` を超えたら、固定エラーの node を
    1件流して run を打ち切る（fail-closed。read_around 等の tool-result が (a)(b) で個別に上限化
    されていても、多数回の呼び出しが積み重なる総量までは抑えられないため）。

    レビュー是正（FIX-H・secRV・2026-07-19・実行 allowlist の非対称）: 拒否分岐（`allowed_tools`）は
    従来サブ経路（`allowed_tools` 明示指定）でのみ働き、メイン経路（`allowed_tools=None`）は
    提示していないツール名をモデルが呼んでも `run_tool` を実行しうる非対称があった（現状は
    `tools` がフル提示のため実害は無いが、提示 toolset と実行可否が独立＝将来の呼び出し元の
    footgun）。是正: ループ冒頭で実際に提示した `tools` からツール名集合 `offered_names` を導出し、
    `effective_allowed = allowed_tools if allowed_tools is not None else offered_names` を実行
    allowlist として使う（＝提示していないツール名は常に拒否・メイン/サブ経路で対称）。メイン経路の
    正常系は不変: `tools` は元々 `openai_tools(...)`／`toolset` の**そのもの**から `offered_names`
    を作るため、通常提示されるツール（read_around/ripgrep_search/list_docs＋条件付き
    es_search/graph_neighbors/ask_user）は必ず `offered_names` に含まれる。拒否ノード/結果は
    サブ経路と同じ固定文言・`safe_name` クリップ・`total_tool_bytes` 累計計上をメイン経路にも適用
    （対称化）。ノード表示の豊かさ（`_tool_node` vs `_tool_node_sub`）はこの allowlist とは別の
    軸のまま＝引き続き `allowed_tools is not None`（真のサブ経路かどうか）で判定する。

    `usage_acc`（S3・chat-sub 計測の欠落是正・2026-07-18 Codex RV 1巡目 MED・2巡目 MED で計数方式を
    再是正）: 非 None のとき、`{"calls": int, "tokens": dict|None}` 形の呼び出し元アキュムレータを更新する。
    `calls` は stop/SSRF ガード通過後・**`_post` 発行直前**に+1する（＝実際に試みた回数。`_post`
    自体が HTTP エラー/タイムアウト/不正応答で失敗しても、その試行は calls に含まれる＝
    「1回も試みていない」との誤認を防ぐ）。`tokens` は各ターンの `_post` が**成功した直後**
    （`final`/`question` へ分岐する前）にその時点までの累積使用量＝`_usage_or_none(usage)` で
    上書きする（失敗したターンの分は反映されない＝報告できるものだけを反映）。`final` イベントでしか
    埋まらない返り値の `usage` と異なり、ask_user 早期 return・途中ターンの例外でも、呼び出し元は
    「何回試みたか」と「それまでに成功した分のトークン」を最終結果に関わらず観測できる。
    既定 None は無効（既存呼び出し元は byte-identical）。

    `shared_budget`（S4-b・複数プロファイル横断予算・§6.2 項1）: `{"tool_bytes_used": int,
    "tool_bytes_max": int}` 形の呼び出し元アキュムレータ。非 None のとき、各 tool-result のバイト計上
    （既存 `total_tool_bytes` 加算箇所）で `shared_budget["tool_bytes_used"]` にも同じ増分を加算し、
    per-run 上限（`TOOL_RESULT_MAX_TOTAL_BYTES`）**または** 共有予算の残量超過のどちらでも既存の
    fail-closed 打ち切り（固定ノード＋空 `final`）を発動する。既定 None は既存の per-run 上限のみ
    （呼び出し元は byte-identical）。

    `depth`: 内部 API（Depth/Cost/Verification Profile・EXT-5 未実装）。既定 `"light"` では評価フェーズ
    （Research Cycle 境界ごとの `submit_evaluation` 構造化評価）は一切発動しない。`"medium"`/`"deep"`
    を明示したときのみ `RESEARCH_CYCLE_TURNS` ターンごと、または no-tool 終了時に評価を強制する
    （§3.2）。呼び出し元（`providers/openai.py`/`ollama.py`/`base.py`）は現時点で本引数を一切渡さない
    ＝利用者設定とは未接続。EXT-5 が Profile を解決して各呼び出し元へ渡すまでは、テストからの直接
    指定でのみ発動する。

    `call_budget`: `{"remaining": int}` の共有カウンタ。非 None のとき、通常ターン・評価・最終合成・
    その再試行を含む全ての `_post` 発行直前で `_consume_call` により原子的に1消費し、残数0なら
    `_post` を発行せず `stop_reason="budget_exceeded"` の `final` を返す（複数プロファイル横断予算・
    `providers/base.py::_run_sub_plan` が使う）。既定 None は無制限（既存呼び出し元は byte-identical）。

    `final` イベントには EV-0 用の `verified_docs`（`read_around` で実際に精読した doc_id 集合）・
    ループを終えた理由 `stop_reason`・機械検証で確定した Committed Evidence のみの `cites`・検証
    メタ情報 `evidence_meta`／`dropped_citations`／`candidates_seen`・評価結果
    `evaluation_status`/`evaluation_reason`/`evaluation_next_action`（評価を実行した場合のみ）を
    常に含める（既存キーへの加算のみ・未使用の呼び出し元には無害）。`cites` は `_commit_evidence`
    による機械検証（doc 実在チェック）を通過したものだけ＝モデルが最終回答を生成した**後**に検証で
    落ちた citation があれば、同一ループ内で1回だけ再合成させてから確定する（Committed Evidence 化
    ゲート）。
    """
    def _send(url, headers, body, *, timeout=90):
        """1論理送信＝1回以上の物理送信（同一プロバイダ内の限定リトライ）。呼び出し予算の消費と
        usage_acc への加算は、この関数が**物理送信ごとに**（初回・再試行を問わず）自分で行う
        ——呼び出し元は事前に消費・加算しない（1物理送信=1消費・「実際に発行を試みた回数」を
        過不足なく数える）。OpenAI 経路（`ollama` でない）はガード確認・予算消費・usage 加算を
        `llm.begin_openai_send()` で1つの原子的な塊として行う——ガード確認と消費/送信の間に
        隙間を作ると、その隙間で `set_openai_endpoint_seed_blocked()` が block を成立させても
        通過済みのまま送信してしまう競合になるため（`llm.begin_openai_send` docstring 参照）。
        `stop_event` は物理送信ごとに、かつ予算消費より前に確認する。応答タイムアウトは再試行
        対象外（`_retryable_post_error` 参照・上流で処理/課金が既に進んでいる可能性があり、
        再試行は二重送信リスクになるため）。`timeout` は全体 deadline として扱う（各試行へ満額を
        再適用しない）。
        """
        deadline = time.monotonic() + timeout
        attempt = 0
        while True:
            if stop_event is not None and stop_event.is_set():
                raise _SendAborted("stop")
            if not ollama:
                try:
                    llm.begin_openai_send(call_budget, usage_acc)
                except llm.SendBudgetExceeded:
                    raise _SendAborted("budget_exceeded")
            else:
                # ollama 経路は OpenAI 送信ガード・ゲートロックの対象外
                # （`llm.assert_openai_io_allowed` と同じ適用範囲）。
                if not _consume_call(call_budget):
                    raise _SendAborted("budget_exceeded")
                if usage_acc is not None:
                    usage_acc["calls"] += 1
            remaining = max(deadline - time.monotonic(), 0.001)
            try:
                return _post(url, headers, body, remaining)
            except Exception as e:
                # 「LLM 送信で実際に起きた例外」の印。`sherpa/research_service.py::run_research`
                # の catch-all はツール実行（grep 等・ファイル I/O 起因の接続断もありうる）と
                # 本関数の物理送信の両方を一つの except で受けるため、型だけでは区別できない
                # ——この印が無い例外は `_is_connection_failure` が真でも「AI に接続できません」
                # へは倒さない（呼び出し元の判定条件参照）。
                e._sherpa_llm_send_error = True
                if attempt >= _POST_RETRY_ATTEMPTS or not _retryable_post_error(e):
                    raise
                wait = _retry_after_seconds(e)
                if wait is None:
                    wait = _POST_RETRY_BACKOFF_SEC * (2 ** attempt)
                # 待った後に送信できる時間が実質的に残らないなら、待たずにここで打ち切る
                # （期限切れ寸前の 0.001 秒タイムアウトでの物理送信＝無意味な予算消費になるため）。
                if deadline - time.monotonic() - wait <= _MIN_SEND_TIMEOUT_SEC:
                    raise
                # 直前の送信で呼び出し予算を使い切っていれば、再試行のバックオフ（最大
                # `_RETRY_AFTER_CAP_SEC` 秒）を待たずに即座に budget_exceeded で打ち切る
                # （待っても次の消費が失敗するだけなので、待機は無意味）。`remaining` の読み取りは
                # 非消費の目安（実際の消費判定は次のループ先頭の `begin_openai_send`/
                # `_consume_call` が行う）。
                if call_budget is not None and call_budget.remaining <= 0:
                    raise _SendAborted("budget_exceeded")
                if wait > 0:
                    time.sleep(wait)
                # 実測の待機時間は OS スケジューリング等で計画（`wait`）より延びうる。上の事前
                # チェックは「計画上の wait」だけを見ており実測の遅延を捕捉できないため、sleep
                # 直後に実測の残り時間を再検査する（期限切れ寸前の無意味な送信を防ぐ）。
                if deadline - time.monotonic() < _MIN_SEND_TIMEOUT_SEC:
                    raise
                attempt += 1
    msgs = [{"role": "system", "content": system}, *(history or []),
            {"role": "user", "content": user}]
    _tp = tools_pref_mod.normalize_tools_pref(tools_pref)
    if toolset is not None:
        tools = toolset               # SC-6e: 明示指定時は可用性判定を一切参照しない（docstring 参照）
    else:
        # SC-6e: 呼び出し元が渡した snapshot を使う（無ければ後方互換で都度チェック・TTLキャッシュつき）。
        _avail = tools_availability if tools_availability is not None else tool_availability()
        tools = openai_tools(
            with_es=_avail["fulltext"] and _tp["fulltext"], with_graph=_avail["graph"] and _tp["graph"],
            can_ask=can_ask, with_grep=_tp["grep"])
    # secRV FIX-H（2026-07-19・実行 allowlist の非対称）: 実際に提示した `tools` からツール名集合を
    # 導出し、`allowed_tools` 未指定（メイン経路）でも「提示していないツール名は拒否」を強制する。
    offered_names = frozenset(t["function"]["name"] for t in tools)
    effective_allowed = allowed_tools if allowed_tools is not None else offered_names
    docs: set = set()
    cites: list = []
    cards: list = []
    searched = False
    usage = _new_usage_acc()                   # F3: 全ツールターンの usage を合算
    total_tool_bytes = 0                        # secRV MED-B (c): 1 run 累計の tool-result バイト量
    # BUDGET-1（§3.4）: run 開始時に1回だけ解決し、run の間ずっと使い回す（途中で admin が設定を
    # 変えても当該 run には影響しない）。BUDGET-2（§3.4）: メイン頭脳の provider/model を渡し、
    # 窓由来の上限との min() を取る（`resolve_tool_result_budgets` docstring 参照）。Ollama の場合
    # だけ `/api/show` 照会用の base_url を導出する（`model_windows.derive_ollama_base_url`）。
    from . import model_windows as _model_windows
    tool_result_max_bytes, tool_result_max_total_bytes = resolve_tool_result_budgets(
        provider=("ollama" if ollama else "openai"), model=model,
        ollama_base_url=(_model_windows.derive_ollama_base_url(endpoint) if ollama else None))
    verified_docs: set = set()                  # EXT-2/EV-0: read_around で実際に精読した doc_id
    structural_evidence_meta: list = []       # list_docs/graph_neighbors の検証済み根拠 detail（Evidence ID 割当用）
    eval_active = depth in EVAL_DEPTHS_ENABLED  # EXT-3: light（既定）は評価フェーズを一切発動しない
    stop_reason = "turns_exhausted"              # EXT-3: 評価フェーズが早期終了させたら上書きする
    evaluation: dict | None = None              # 直近の評価結果（Evidence Packet/UI へ伝搬する）
    pending_final_text: str | None = None       # no-tool 終了で得た回答文（tail の再合成を省略する）
    pending_finish_reason: str | None = None    # EV-0（拡張設計 §4.4）: 上と同時点の完了理由（帰属直前の再判定用）
    turns = max_turns if max_turns is not None else MAX_TURNS
    for turn_idx in range(turns):
        if stop_event is not None and stop_event.is_set():
            return
        body = {"model": model, "messages": msgs, "tools": tools}
        if ollama:
            body["stream"] = False
            body["options"] = {"temperature": 0.2}
        # OpenAI へは temperature を送らない（bedrock/Claude と同じ扱い）。gpt-5.5 系は既定値(1)以外を
        # 拒否し 400 `unsupported_value` を返すため、送るとツールループが丸ごと失敗する（2026-08-15 実測）。
        # 非ストリーミング＝usage は既定で resp に含まれる。呼び出し予算の消費・usage_acc への
        # 加算・OpenAI 送信ガードの確認は `_send` が物理送信ごとに自分で行う（本関数側では
        # 事前に消費・加算しない・`_send` docstring 参照）。
        try:
            resp = _send(endpoint, headers, body, timeout=_resolve_timeout(timeout))
        except _SendAborted as e:
            if e.reason == "stop":
                return                                   # 停止時は final を出さない（既存の停止契約と同型）
            yield {"node": _node("call 予算の上限", "この会話で発行できる呼び出し数の上限に達しました")}
            yield _build_final_payload("", docs, searched, cites, cards, _usage_or_none(usage),
                                       verified_docs, "budget_exceeded", world,
                                       structural_evidence_meta=structural_evidence_meta)
            return
        _acc_openai_usage(usage, resp, ollama)
        if usage_acc is not None:
            # このターンの _post が成功した時点で即時反映する（final/question へ分岐する前）＝
            # 呼び出し元が「実際にどこまで発行できたか」を最終結果に関わらず観測できる。
            usage_acc["tokens"] = _usage_or_none(usage)
        msg = ((resp.get("choices") or [{}])[0].get("message") if "choices" in resp
               else resp.get("message")) or {}
        calls = msg.get("tool_calls") or []
        if not calls:
            # no-tool 終了も Research Cycle 境界として評価を強制する（既定3ターン境界より前に
            # モデルが回答しても Medium/Deep なら評価を回避できない）。
            text = _openai_style_text(msg)
            if eval_active:
                # 予算消費は `_run_evaluation` の中だけで行う（呼び出し直前でここでも消費すると
                # 二重消費になる＝`call_budget` の残数がターン数の想定より速く尽きる）。
                verdict = _run_evaluation(endpoint, headers, model, msgs, ollama, timeout, usage,
                                          usage_acc, call_budget)
                evaluation = verdict
                if verdict.get("budget_exceeded"):
                    yield _build_final_payload("", docs, searched, cites, cards, _usage_or_none(usage),
                                               verified_docs, "budget_exceeded", world, verdict,
                                               structural_evidence_meta=structural_evidence_meta)
                    return
                if verdict["status"] in ("insufficient", "conflicting"):
                    if verdict["status"] == "conflicting":
                        # 矛盾検知＝設計上は別 Task への再委任（§3.2）だが、Orchestration Service／
                        # 並列委任（EXT-6/7）が本スライスに未実装のため、この Research Cycle 内で
                        # 調査を継続する縮退にとどめる。
                        yield {"node": _eval_node("replan_requested", "矛盾を検知",
                                                  verdict["reason"] or "情報の矛盾を検知しました。調べ直します")}
                    else:
                        yield {"node": _eval_node("evaluation_completed", "調査状況を評価",
                                                  f"調査が不十分と判定されました（{verdict['reason']}）")}
                    msgs.append({"role": "assistant", "content": text})
                    msgs.append({"role": "user", "content": _EVAL_CONTINUE_NUDGE})
                    continue
                if verdict["status"] == "blocked":
                    yield {"node": _eval_node("evaluation_completed", "調査状況を評価",
                                              f"行き詰まりのため打ち切ります（{verdict['reason']}）")}
                    yield {"node": _eval_node("finalization_started", "調査を終了",
                                              verdict["reason"] or "これ以上の調査が難しいため終了します")}
                    stop_reason = "evaluation_blocked"
                else:   # sufficient
                    yield {"node": _eval_node("evaluation_completed", "調査状況を評価",
                                              f"十分な根拠が集まりました（{verdict['reason']}）")}
                    stop_reason = "evaluation_sufficient"
            else:
                _fr = _openai_style_finish_reason(resp)
                stop_reason = (_incomplete_stop_reason(
                    _fr, truncated=_OPENAI_STYLE_TRUNCATED, content_filtered=_OPENAI_STYLE_CONTENT_FILTERED)
                    if not _is_natural_completion(_fr, _OPENAI_STYLE_NATURAL_COMPLETION) else "no_tool_calls")
            pending_final_text = text
            pending_finish_reason = _openai_style_finish_reason(resp)
            break   # 共通の tail（Committed Evidence 化ゲート＋必要なら再合成）へ合流する
        searched = True
        msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        # レビュー是正（LOW-D・secRV・2026-07-18 再検証）: 超過分は `calls[:MAX_TOOLS_PER_TURN]` で
        # 単純に切り捨てる（超過件数分のノードを生成しない＝下のループ後にまとめて固定ノード1件だけ
        # 流す）。
        over_limit = len(calls) > MAX_TOOLS_PER_TURN
        for tc in calls[:MAX_TOOLS_PER_TURN]:
            # secRV MED-3 (b): 各ツール実行の直前に stop_event を確認する（1応答内に大量の tool_calls
            # が積まれていても、途中停止が反映されないまま実行し続けることを防ぐ）。
            if stop_event is not None and stop_event.is_set():
                return
            fn = tc.get("function") or {}
            name = fn.get("name")
            args = _safe_json(fn.get("arguments"))
            if name not in effective_allowed:
                # S3・二重強制の(b): ツール定義配列を絞っていても（モデルの逸脱/幻覚呼び出しに備え）
                # run_tool を呼ばずに拒否結果を返し、ループは継続する（例外にしない）。
                # レビュー是正（MED・2026-07-18 Codex RV 2巡目・拒否ツールの生成文漏洩）: 許可判定を
                # `_tool_node(name, args)` の**前**に行う。以前は判定より先にノードを yield して
                # いたため、除外済み ask_user 等をモデルが幻覚呼び出しすると、モデル生成の引数
                # （ask_user の "prompt" 等）が思考ノード/trace に漏れて表示・保存されてしまっていた。
                # レビュー是正（MED・2026-07-18 Codex RV 3巡目・ツール名も生成文）: `name` 自体も
                # モデル生成値（未知名なら任意の長文になり得る）＝label にも使わず、node は**完全固定文言**
                # にする。モデルへの是正フィードバック（tmsg・LLM 会話内のみ＝UI/trace に出ない）にだけ
                # name を残す（どのツール名が拒否されたかをモデルが自己修正するために必要）。
                # レビュー是正（FIX-1・secRV・2026-07-19・拒否ツール結果のバイト迂回）: `name` は
                # モデル生成値で長さ無制限のため (a) `_REJECTED_TOOL_NAME_MAX_BYTES` で固定長へ
                # クリップし、(b) この tool-result も他の tool-result と同じ `total_tool_bytes`
                # 累計へ必ず計上する（以前は計上されず、この経路だけ 1 run 累計バイト上限をすり
                # 抜けられた）。
                # レビュー是正（FIX-H・secRV・2026-07-19・実行 allowlist の非対称）: 判定を
                # `effective_allowed`（サブ経路は `allowed_tools`・メイン経路は `offered_names`）へ
                # 統一し、メイン経路でも提示していないツール名の実行を拒否する（対称化）。
                yield {"node": _node("許可外のツール呼び出し", "許可されていないため拒否しました")}
                safe_name = _clip_utf8_bytes(str(name or ""), _REJECTED_TOOL_NAME_MAX_BYTES)
                result = {"error": f"ツール {safe_name} は使用できません"}
                _sz = _result_byte_size(result)
                total_tool_bytes += _sz
                # S4-b（§6.2 項1）: 横断予算にも同じ増分を計上する（RV 3巡目是正: 不正な形は修復せず
                # 未加算のまま直後の判定で fail-closed・詳細は下の同型サイトのコメント参照）。
                if shared_budget is not None and not _tool_bytes_over_budget(0, shared_budget, tool_result_max_total_bytes):
                    shared_budget["tool_bytes_used"] += _sz
                if _tool_bytes_over_budget(total_tool_bytes, shared_budget, tool_result_max_total_bytes):
                    yield {"node": _node("ツール結果の合計サイズ上限",
                                         "この会話で取得した量が多すぎるため打ち切りました")}
                    yield _build_final_payload("", docs, searched, cites, cards, _usage_or_none(usage),
                                               verified_docs, "budget_exceeded", world,
                                               structural_evidence_meta=structural_evidence_meta)
                    return
                tmsg = {"role": "tool", "name": safe_name, "content": json.dumps(result, ensure_ascii=False)}
                if tc.get("id"):
                    tmsg["tool_call_id"] = tc["id"]
                msgs.append(tmsg)
                continue
            # レビュー是正（MED-2・secRV・2026-07-18）: サブ経路（`allowed_tools is not None`＝
            # `_sub_agentic_loop` 経由）はモデル生成の引数（query/doc_id/path 等）を思考ノードに
            # 埋め込まない固定文言ノードにする（`_tool_node_sub` 参照）。メイン経路（allowed_tools
            # は None）は既存の `_tool_node`（豊かな表示）のまま＝byte-identical。
            yield {"node": (_tool_node_sub(name) if allowed_tools is not None else _tool_node(name, args))}
            # レビュー是正（LOW-E・secRV・2026-07-18 再検証）: ノード yield 直後（generator 再開後）
            # にも stop_event を再確認する（ノードを流した直後に停止要求が来ても、再開後 ask_user
            # 分岐/run_tool を1件実行してしまう窓を塞ぐ）。
            if stop_event is not None and stop_event.is_set():
                return
            if name == "ask_user":
                # 意味論（gemini/anthropic_style も同じ）: ask_user は **question 優先**。同一応答内で
                # ask_user より前に並んで実行済みの他ツールの結果（docs/cites/cards・msgs への追記）は、
                # ここで return するため呼び出し元へは渡らず破棄される（`final` を yield しない＝
                # チャット側は env を作らない）。これは意図的: ask_user の回答はフロントが新規メッセージ
                # として再送し（chat_router の clarify 再開）、次ターンは新しい messages で検索し直す
                # ＝この時点までの検索状態を持ち越す仕組みが元々無いので、破棄しても実害はない。
                yield {"question": _question_from_args(args)}
                return
            result, d, c, cd = run_tool(name, args, world, scope_paths, deadline=tool_deadline,
                                        layer=layer, max_hits=max_hits, window_cap=window_cap,
                                        tool_result_max_bytes=tool_result_max_bytes)
            # 「何を探して・いくつ当たったか」の追加ノード（`_tool_node`/`_tool_node_sub` は
            # 結果が出る前のノードのため件数を書けない・`_hit_summary_node`/`_hit_summary_node_sub`
            # 参照）。
            hit_node = (_hit_summary_node_sub(name, result) if allowed_tools is not None
                       else _hit_summary_node(name, args, result))
            if hit_node:
                yield {"node": hit_node}
            # RV2（FBK-1・2026-09-01）: es_search が BM25 のみへ縮退した場合、その理由を「思考の
            # 流れ」へも表示する（サーバログの warning だけでは利用者に届かない・`_degrade_result_
            # node` 参照）。
            degrade_node = _degrade_result_node(result)
            if degrade_node:
                yield {"node": degrade_node}
            # S2（2026-09）: ripgrep_search が cap 打切りで探せていない文書を申告したら（
            # `truncated_docs`・run_tool 参照）、同じ枠組みでもう1件 yield する。
            truncated_node = _truncated_docs_node(result)
            if truncated_node:
                yield {"node": truncated_node}
            # レビュー是正（MED-B (c)・secRV・2026-07-18）: 1 run 累計の tool-result バイト量が
            # 上限を超えたら、この結果は破棄し固定エラーで run を打ち切る（fail-closed）。
            # レビュー是正（FIX-2・secRV・2026-07-19）: `cd`（cards サイドカー・`run_tool` 側で
            # 既に件数＋バイト上限クリップ済み＝`_clip_cards` 参照）の直列化バイトも累計へ計上する
            # （以前は `result` のみを計測しており、cards はこの計測経路をすり抜けていた）。
            _sz = _result_byte_size(result) + _result_byte_size(cd)
            total_tool_bytes += _sz
            # S4-b（§6.2 項1）: 横断予算にも同じ増分を計上する。RV 3巡目是正: 形が不正な dict は
            # **修復しない**（.get 既定や int 化で正常形に見せると片側キー欠損が helper をすり抜ける）。
            # 正常形（helper が total=0 で False を返す形）のときだけ加算し、不正なら未加算のまま
            # 直後の `_tool_bytes_over_budget` が True（fail-closed）で打ち切る。
            if shared_budget is not None and not _tool_bytes_over_budget(0, shared_budget, tool_result_max_total_bytes):
                shared_budget["tool_bytes_used"] += _sz
            if _tool_bytes_over_budget(total_tool_bytes, shared_budget, tool_result_max_total_bytes):
                yield {"node": _node("ツール結果の合計サイズ上限",
                                     "この会話で取得した量が多すぎるため打ち切りました")}
                yield _build_final_payload("", docs, searched, cites, cards, _usage_or_none(usage),
                                           verified_docs, "budget_exceeded", world,
                                           structural_evidence_meta=structural_evidence_meta)
                return
            docs |= d
            cites += c
            cards += cd
            # EXT-2/EV-0（拡張設計 §4.4）: 「精読済み」タグは read_around/read_doc を実際に呼んだ
            # doc_id のみ（エラー応答は精読が成立していないため除外）。grep/es_search のヒットのみの
            # doc は `verified_docs` に入らない＝出典フッターで「根拠」と「参考」を分ける最小ロジック。
            if name in ("read_around", "read_doc") and "error" not in result:
                verified_docs |= d
            # list_docs（doc_ledger の live 走査＝実在確認済み）／graph_neighbors（Neo4j 検証済み
            # card/edge）は citation を生成しないが、具体的な検証済みエントリがあれば根拠として正当。
            # 根拠ゲートが citation 件数だけで判定して資料一覧・件数質問や graph-only 回答を誤って
            # 落とさないためのシグナルとして記録する（troubleshoot 以外の lens でも graph 根拠を認める）。
            if name == "list_docs" and "error" not in result:
                # EV-0（拡張設計 §4.4）: list_docs は**呼び出し単位で集計した1 Evidence**とする
                # （総件数・適用条件・列挙範囲＋列挙した各パス）——0件の呼び出しも「該当0件」という
                # 具体的な事実として1 Evidence（ev-N）を持つ（根拠ゲート・帰属の対象になる）。
                _matched = [doc.get("rel_path") for doc in (result.get("docs") or [])
                           if doc.get("rel_path")]
                structural_evidence_meta.append({
                    "doc_id": None, "span": None, "verification_method": "list_docs_verified",
                    "list_meta": {"count": result.get("count", 0), "shown": len(_matched),
                                  "prefix": str(args.get("path_prefix") or "").strip(),
                                  "pattern": str(args.get("name_pattern") or "").strip()},
                    "matched_doc_ids": _matched})
            if name == "graph_neighbors" and cd:
                # `run_tool` が既にカード単位で裏付け doc を検証済み（無効カードは cd に含まれない・
                # `d` はその検証済み doc_id 集合そのもの）——ここで再検証しない。裏付け doc を
                # 主張しないカード（純粋なグラフ位相情報）は、Neo4j から実際に返ったノードである
                # こと自体を source_type=graph の構造 Evidence として計上する。
                structural_evidence_meta += _card_structural_evidence(cd)
            tmsg = {"role": "tool", "name": name, "content": json.dumps(result, ensure_ascii=False)}
            if tc.get("id"):
                tmsg["tool_call_id"] = tc["id"]
            msgs.append(tmsg)
        if over_limit:
            # レビュー是正（LOW-D）: 超過件数に関わらず固定ノード1件だけ生成する。
            yield {"node": _node("ツール呼び出し上限", "1回の応答あたりの実行数上限に達したため打ち切りました")}
            # secRV MED-3 (c): この応答は上限超過＝以降のターンへは進まず、ここで打ち切る。
            yield _build_final_payload("", docs, searched, cites, cards, _usage_or_none(usage),
                                       verified_docs, "tools_per_turn_exceeded", world,
                                       structural_evidence_meta=structural_evidence_meta)
            return
        # EXT-3（拡張設計 §3.2/§3.3）: Research Cycle 境界（`RESEARCH_CYCLE_TURNS` ターンごと）で
        # 構造化評価を1回挟む。`depth`（既定 "light"）が Medium/Deep でないときは `eval_active=False`
        # のままこのブロックを丸ごと素通りする（既存呼び出し元は誰も `depth` を渡さない＝
        # byte-identical・§3.4）。
        if eval_active and (turn_idx + 1) % RESEARCH_CYCLE_TURNS == 0:
            if stop_event is not None and stop_event.is_set():
                return
            # 予算消費は `_run_evaluation` の中だけで行う（二重消費を避ける）。
            verdict = _run_evaluation(endpoint, headers, model, msgs, ollama, timeout, usage, usage_acc,
                                      call_budget)
            evaluation = verdict
            if verdict.get("budget_exceeded"):
                yield _build_final_payload("", docs, searched, cites, cards, _usage_or_none(usage),
                                           verified_docs, "budget_exceeded", world, verdict,
                                           structural_evidence_meta=structural_evidence_meta)
                return
            if verdict["status"] == "sufficient":
                # §3.2: sufficient → Candidate/Verified から Committed Evidence へ（tail で確定）。
                yield {"node": _eval_node("evaluation_completed", "調査状況を評価",
                                          f"十分な根拠が集まりました（{verdict['reason']}）")}
                stop_reason = "evaluation_sufficient"
                break
            if verdict["status"] == "blocked":
                # §3.2/§3.5: blocked は既存の「反復上限到達」最終合成へ合流する特殊ケース。
                yield {"node": _eval_node("evaluation_completed", "調査状況を評価",
                                          f"行き詰まりのため打ち切ります（{verdict['reason']}）")}
                yield {"node": _eval_node("finalization_started", "調査を終了",
                                          verdict["reason"] or "これ以上の調査が難しいため終了します")}
                stop_reason = "evaluation_blocked"
                break
            if verdict["status"] == "conflicting":
                # 矛盾検知＝設計上は別 Task への再委任（§3.2）だが、Orchestration Service／並列委任
                # （EXT-6/7）が本スライスに未実装のため、この Research Cycle 内で調査を継続する縮退に
                # とどめる（矛盾検知の可視性は `replan_requested` イベントで確保する）。
                yield {"node": _eval_node("replan_requested", "矛盾を検知",
                                          verdict["reason"] or "情報の矛盾を検知しました。調べ直します")}
            # insufficient: §3.2 の表どおりイベントを出さず同一 Research Cycle 内で継続する。
    # ---- tail: Committed Evidence 化ゲート（＋必要ならクリーン再合成） ----
    # 到達経路: (a) turns 上限に達した（stop_reason="turns_exhausted"）、(b) no-tool 終了（`pending_
    # final_text` に回答文あり）、(c) 評価フェーズが sufficient/blocked と判定して早期 break した
    # （`pending_final_text` は None＝改めて合成）。いずれの理由でも、ここまでに集めた資料・引用を
    # 検証してから確定する——検証で落ちた citation がある場合は、ツール履歴・落ちた citation・
    # モデルの前回ドラフトを一切含まないクリーンなコンテキスト（Committed Evidence の一覧のみ）を
    # 組んで1回だけ再合成する（`evidence_committed` イベントの発行は provider 側＝根拠ゲート通過後の
    # 契約。ここでは citation の確定と本文生成だけを行う）。
    if stop_event is not None and stop_event.is_set():
        return                                         # 停止時は final を出さない（既存の契約・docstring 参照）
    # `final_synthesis=False`（サブエージェント経路）は文章を破棄する契約（`providers/base.py`
    # `_agentic_run` の S3 分岐）のため合成しないが、Committed Evidence 化ゲートは必ず通す
    # （citation は検証してから返す＝合成の有無に関わらない）。本文（`pending_final_text`）を
    # 破棄する経路なので、この本文に対する帰属呼び出しは行わない——EV-0 の根拠判定は**表示する
    # 最終回答**（外側クラウド合成 `_answer_prompt`）自身に対する帰属だけを使う契約
    # （`providers/base.py` がストリーム完了後に別途組み立てる）。
    if not final_synthesis:
        yield _build_final_payload("", docs, searched, cites, cards, _usage_or_none(usage),
                                   verified_docs, stop_reason, world, evaluation,
                                   structural_evidence_meta=structural_evidence_meta)
        return

    committed, evidence_meta, dropped = _commit_evidence(cites, world)
    _finish_reason: str | None = None       # EV-0（拡張設計 §4.4）: この turn の完了理由（帰属直前に再判定）
    # PART-4（sherpa/research_service.py）向け: 最終合成/再合成の HTTP 呼び出しそのものが例外で
    # 失敗し `candidate_text` が強制的に空文字へ縮退した場合だけ True にする（digest 欠落・
    # call 予算切れ等の「合成を試みてすらいない」経路とは区別する＝それらは stop_reason 側で
    # 判別できる）。
    _synthesis_failed = False
    # `_synthesis_failed` の原因が接続失敗（`_send` が物理送信の例外に付与する
    # `_sherpa_llm_send_error` マーカーを伴い、かつ `_is_connection_failure` が真）なら
    # "connection" を立てる——生の例外は payload に載せず、この安全な分類値だけを渡す
    # （`research_service.py` が provider 名つきの専用文言へ倒す判別材料）。マーカーを併用する
    # のは、この except が `_send` の物理送信だけでなく usage 加算・応答パースも同じ try で
    # 囲むため、型だけでは「実際に接続で失敗したか」を判別できないため（マーカー無しの例外は
    # "connection" を立てず、従来の汎用「合成中に失敗しました」文言のままにする）。
    _failure_kind: str | None = None
    if dropped:
        # 混在ケース（一部 citation が検証で落ちた）: 入力は system＋現在の質問＋Committed Evidence
        # digest だけ。通常の会話履歴（`history`）・ツール呼び出し履歴・モデルの前回
        # ドラフトは一切渡さず、クリーンな最終合成コンテキストを再構築して1回だけ合成する。
        # 再合成できなければ本文を返さない（honest failure・壊れた根拠に基づく主張を持ち越さない）。
        candidate_text = ""
        digest = _committed_evidence_digest(committed)
        # 呼び出し予算の消費・usage_acc への加算・OpenAI 送信ガードの確認は `_send` が物理送信
        # ごとに自分で行う（`_send` docstring 参照）。ガード失敗・予算切れはこの再合成の
        # 「候補なし」への既存の degrade（`except Exception: candidate_text = ""`）と同じ扱いに
        # する（budget_exceeded 専用の早期 return はしない＝下の except _SendAborted 参照）。
        if digest:
            if stop_event is not None and stop_event.is_set():
                # watchdog 発火後の再合成は新規送信しない——tail 冒頭（本関数上部）の確認から
                # ここまでの間（Committed Evidence 化・digest 組み立て）に停止要求が来た窓を塞ぐ
                # （`_send` 自身も送信直前に同じ確認をするが、ここで早期に抜ければ resynth_msgs/
                # body の組み立て自体を省略できる）。「停止時は final を出さない」契約に揃える
                # ため、ここで即座に抜ける（final を一切 yield しない＝呼び出し元は
                # `final is None` 経路でデッドライン優先の 504 に倒す・黙った空回答 200 を返さない）。
                return
            resynth_msgs = [{"role": "system", "content": system},
                            {"role": "user", "content": _RESYNTH_INSTRUCTION.format(question=user, digest=digest)}]
            body = {"model": model, "messages": resynth_msgs}   # tools を渡さない
            if ollama:
                body["stream"] = False
                body["options"] = {"temperature": 0.2}
            try:
                resp = _send(endpoint, headers, body, timeout=_resolve_timeout(timeout))
                _acc_openai_usage(usage, resp, ollama)
                if usage_acc is not None:
                    usage_acc["tokens"] = _usage_or_none(usage)
                msg = ((resp.get("choices") or [{}])[0].get("message") if "choices" in resp
                      else resp.get("message")) or {}
                candidate_text = _openai_style_text(msg)
                _finish_reason = _openai_style_finish_reason(resp)
            except _SendAborted as e:
                candidate_text = ""
                if e.reason == "stop":
                    return               # 停止時は final を出さない（既存の停止契約と同型）
                stop_reason = e.reason   # 実停止理由（budget_exceeded）を turns_exhausted 等へ吸収させない
            except Exception as e:
                from .ingest.graph_extract import _log_masked_exception
                _log_masked_exception(_log, "agentic_search: 再合成に失敗", e, _header_secret(headers))
                candidate_text = ""
                _synthesis_failed = True
                if getattr(e, "_sherpa_llm_send_error", False) and _is_connection_failure(e):
                    _failure_kind = "connection"
    elif pending_final_text is not None:
        candidate_text = pending_final_text     # dropped が無い＝会話履歴をそのまま使ってよい
        _finish_reason = pending_finish_reason
    else:
        # turns 上限到達／評価の早期終了向けの最終合成（tools 無し・フル msgs 使用）。dropped が
        # 無いのでここまでの会話履歴（tool 結果含む）をそのまま使ってよい。呼び出し予算の消費・
        # usage_acc への加算・OpenAI 送信ガードの確認は `_send` が物理送信ごとに自分で行う
        # （node yield／body 構築の前に予算だけを先取りしない＝その間の stop/block で消費だけが
        # 無駄になる窓を作らない）。
        if stop_event is not None and stop_event.is_set():
            # watchdog 発火後の最終合成は新規送信しない（直前の call budget 消費からここまでの
            # 間に停止要求が来た窓を塞ぐ・「停止時は final を出さない」契約）。`_send` 自身も
            # 送信直前に同じ確認をするが、ここで早期に抜ければ node yield／body 構築自体を
            # 省略できる。
            return
        if stop_reason == "evaluation_sufficient":
            label, synth_msg = "十分な根拠を確認", _FINAL_SYNTHESIS_SUFFICIENT
        elif stop_reason == "evaluation_blocked":
            label, synth_msg = "調査を終了", _FINAL_SYNTHESIS
        else:
            label, synth_msg = "調査の上限に到達", _FINAL_SYNTHESIS
        # 予算が既に枯渇していれば「ここまでに集めた資料で回答をまとめます」の node を出さない
        # （下の `_send` が `budget_exceeded` で即座に打ち切るため、この node を先に見せると
        # 「回答をまとめる」と予告だけして空の最終回答になる）。`remaining` の読み取りは
        # `_send` 自身の原子的な消費とは独立の目安（本ループは直列実行のため実質的にずれない）。
        if call_budget is None or call_budget.remaining > 0:
            yield {"node": _node(label, "ここまでに集めた資料で回答をまとめます")}
        if stop_event is not None and stop_event.is_set():
            # `yield` は呼び出し元へ制御を戻す——再開までにかかる時間は呼び出し元次第（chat の
            # UI 停止操作・PART-4 の watchdog とも、この yield の間に stop_event が立ちうる）。
            # 直前（yield の前）のチェックだけでは、この yield 復帰後に立った停止要求を見逃し、
            # 最終合成を新規送信してしまう——送信直前（本関数の他の停止確認と同じ位置づけ）で
            # 再確認する（「停止時は final を出さない」契約に揃える）。
            return
        msgs.append({"role": "user", "content": synth_msg})
        body = {"model": model, "messages": msgs}     # tools を渡さない＝これ以上ツールを呼べない
        if ollama:
            body["stream"] = False
            body["options"] = {"temperature": 0.2}
        try:
            resp = _send(endpoint, headers, body, timeout=_resolve_timeout(timeout))
            _acc_openai_usage(usage, resp, ollama)
            if usage_acc is not None:
                usage_acc["tokens"] = _usage_or_none(usage)
            msg = ((resp.get("choices") or [{}])[0].get("message") if "choices" in resp
                  else resp.get("message")) or {}
            candidate_text = _openai_style_text(msg)
            _finish_reason = _openai_style_finish_reason(resp)
        except _SendAborted as e:
            candidate_text = ""
            if e.reason == "stop":
                return               # 停止時は final を出さない（既存の停止契約と同型）
            stop_reason = e.reason   # 実停止理由（budget_exceeded）を turns_exhausted 等へ吸収させない
        except Exception as e:
            from .ingest.graph_extract import _log_masked_exception
            _log_masked_exception(_log, "agentic_search: 最終合成に失敗", e, _header_secret(headers))
            candidate_text = ""     # 合成に失敗＝従来と同じ空回答（呼び出し元が縮退）
            _synthesis_failed = True
            if getattr(e, "_sherpa_llm_send_error", False) and _is_connection_failure(e):
                _failure_kind = "connection"
    # 最終的に表示する本文（`candidate_text`）を**実際に生成した呼び出し**の `_finish_reason` で
    # stop_reason を再分類する。初回ドラフト時点で決めた stop_reason（no_tool_calls/
    # evaluation_sufficient/evaluation_blocked/turns_exhausted）は、直後の再合成（citation 検証で
    # 落ちた場合のクリーン再合成）や最終合成（turns_exhausted/評価早期終了向けの追加 `_send`）で
    # `_finish_reason` が変わりうることを反映していない——初回が自然完了でも再合成/最終合成が
    # "length"（出力上限）で切れることも、逆もあり得る。既知の打ち切り理由（truncated/
    # content_filtered）と判別できる場合だけ上書きする（判別できない＝`"unknown"` が返る場合は
    # evaluation_*/turns_exhausted 等の情報を失わせないよう元の stop_reason を保持する・
    # `_SendAborted` 由来の budget_exceeded 等も `_finish_reason` が None のままなので上書きされない）。
    if not _is_natural_completion(_finish_reason, _OPENAI_STYLE_NATURAL_COMPLETION):
        _reclassified_stop_reason = _incomplete_stop_reason(
            _finish_reason, truncated=_OPENAI_STYLE_TRUNCATED, content_filtered=_OPENAI_STYLE_CONTENT_FILTERED)
        if _reclassified_stop_reason != "unknown":
            stop_reason = _reclassified_stop_reason
    # EV-0（拡張設計 §4.4）: 確定した回答本文（`candidate_text`）＋ Evidence digest（ev-N→事実）を
    # 帰属呼び出しへ渡し、実際に使った ev-N を doc_id へ逆引きする（**表示する** `candidate_text`
    # 自体は変更しない・byte-identical。帰属呼び出しへは `_redact` を通しただけのコピーを渡す——
    # digest 側も生 doc_id/パスのままのため別名対応は不要・秘密だけ伏せる）。帰属**直前**に
    # 停止状態を再確認し（tail 冒頭の確認以降、直前の合成/再合成 `_post` の間に停止要求が来た窓を
    # 塞ぐ）、`finish_reason` が自然完了 allowlist（"stop"）に無ければ帰属を省略する（理由欠落・
    # `length`・`content_filter` 等はすべて未完了扱い・read_around のみへ縮退・部分/不正な本文を
    # 確定回答として帰属しない）。digest 構築自体は常に行う——`_ev_map` は帰属結果の ev-N を doc_id
    # へ逆引きする（`resolve_attributed_doc_ids`）ために必須（main 経路は自身の `adopted_ev_ids` を
    # payload へは含めない——base.py 側は再重複排除後に**自分で** digest を組み直す plan/hybrid とは
    # 違い、main はその絞り込みを適用しない契約のため）。
    _digest, _ev_map = build_evidence_digest(committed, evidence_meta + structural_evidence_meta)
    _eligible = (stop_event is None or not stop_event.is_set()) and _is_natural_completion(
        _finish_reason, _OPENAI_STYLE_NATURAL_COMPLETION)
    if not _eligible:
        _attributed: set = set()
    else:
        _attributed = attribute_openai_style(endpoint, headers, model, ollama, _redact(candidate_text),
                                             _digest, _ev_map, timeout, usage, usage_acc, call_budget)
    yield _finalize_payload(candidate_text, docs, searched, committed, evidence_meta, dropped, cards,
                            _usage_or_none(usage), verified_docs, stop_reason, evaluation,
                            structural_evidence_meta=structural_evidence_meta,
                            used_evidence_docs=resolve_attributed_doc_ids(_attributed, _ev_map),
                            attributed_ev_ids=_attributed,
                            synthesis_failed=_synthesis_failed, attribution_eligible=_eligible,
                            failure_kind=_failure_kind)


def anthropic_tools_from_openai(tools: list) -> list:
    """OpenAI 形式ツール（`{"type":"function","function":{name,description,parameters}}`）を
    Anthropic 形式（`{name,description,input_schema}`）へ変換する。`parameters`＝`input_schema` はほぼ同形（JSON Schema）。"""
    out = []
    for t in tools or []:
        fn = t.get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        out.append({"name": name, "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters") or {"type": "object", "properties": {}}})
    return out


# 安全上の理由でモデルが回答を控えた（stop_reason=="refusal"）ときの最終回答。
_ANTHROPIC_REFUSAL = "安全上の理由で回答を控えました。別の表現や範囲でお試しください。"
_ANTHROPIC_MAX_TOKENS = 16000              # ツールループの各応答の上限（最終回答は 2〜4 文＝十分な余裕）


def anthropic_style(client, model: str, system: str, user: str, world: str, scope_paths,
                    toolset: list | None = None, max_tokens: int | None = None, stop_event=None,
                    can_ask: bool = True, history: list | None = None, layer=None,
                    tools_pref: dict | None = None, tools_availability: dict | None = None):
    """Anthropic Messages API（Bedrock 経由等）の tool-use を**手動ループ**で反復。

    `layer`（省略可・既定 `None`＝`"both"`＝既存呼び出し元は無変更）: `openai_style` と同じ探す対象
    フィルタ（調べ方ブロック §3.4）。`run_tool` へそのまま転送する。

    `tools_pref`/`tools_availability`（省略可・既定 `None`・SC-6e）: `openai_style` と
    同じ検索経路トグル／可用性 snapshot（`toolset` 明示指定時はどちらも無視される）。

    `openai_style` / `gemini` と同じイベント契約（`{"node":..}` を yield しつつ最後に
    `{"final","docs","searched","cites","cards"}`／`ask_user` は `{"question":..}`）。
    `client` は SDK クライアント（`.messages.create` を持つ）または遅延生成する factory（callable）。
    ツールは OpenAI 形式（`openai_tools`/`graph_openai_tools`）を `input_schema` 形式に変換して渡す。
    **temperature/top_p/top_k/thinking は送らない**（例: jp.anthropic.claude-haiku-4-5 系では 400）・プレフィル無し・`max_tokens` 必須。
    `stop_event`（UI フィードバック1「途中停止」の RV MEDIUM 再検証・2026-07-03）: `openai_style` と
    同じ意味論＝各ターンのリクエスト発行前に確認し、立っていれば以降のリクエストを発行せず終了する。
    `history`（R1a・会話継続）: 直前ターンの (user, assistant) 対（時系列順・交互保証済み・
    上流でキャップ済み）。現在の user メッセージの前にそのまま並べる（system は kwargs のまま別）。
    省略/空なら従来と完全同一の初期 messages になる。`graph_admin.ask_graph` は位置引数で呼ぶため
    本引数に触れない＝既定 None（空）で後方互換。

    レビュー是正（FIX-H・secRV・2026-07-19・実行 allowlist の非対称）: 実際に提示した `tools` から
    ツール名集合 `offered_names` を導出し、モデルが提示していないツール名を呼んでも `run_tool` を
    実行せず拒否する（`openai_style`/`gemini` と同じ対称化。Anthropic 経路も元々 `allowed_tools`
    引数を持たない＝常に `offered_names` を allowlist として使う）。
    """
    if callable(client):                       # client_factory（遅延生成）にも対応
        client = client()
    _tp = tools_pref_mod.normalize_tools_pref(tools_pref)
    if toolset is not None:
        src_tools = toolset            # SC-6e: 明示指定時は可用性判定を一切参照しない（docstring 参照）
    else:
        _avail = tools_availability if tools_availability is not None else tool_availability()   # SC-6e
        src_tools = openai_tools(
            with_es=_avail["fulltext"] and _tp["fulltext"], with_graph=_avail["graph"] and _tp["graph"],
            can_ask=can_ask, with_grep=_tp["grep"])
    tools = anthropic_tools_from_openai(src_tools)
    offered_names = frozenset(t["name"] for t in tools)
    messages: list = [*(history or []), {"role": "user", "content": user}]
    docs: set = set()
    cites: list = []
    cards: list = []
    searched = False
    usage = _new_usage_acc()                         # F3: 全ツールターンの usage を合算
    total_tool_bytes = 0                              # secRV MED-B (c): 1 run 累計の tool-result バイト量
    # BUDGET-1（§3.4）: run 開始時に1回だけ解決し、run の間ずっと使い回す（途中で admin が設定を
    # 変えても当該 run には影響しない）。BUDGET-2（§3.4）: provider="bedrock"（本アプリの
    # `anthropic_style` 唯一の呼び出し元）＋`client`（`.models.retrieve()` 照会用・現状
    # `AnthropicBedrock` は非対応のため実質 no-op・`model_windows.query_anthropic_context_length`
    # docstring 参照）を渡す。
    tool_result_max_bytes, tool_result_max_total_bytes = resolve_tool_result_budgets(
        provider="bedrock", model=model, anthropic_client=client)
    verified_docs: set = set()                        # EXT-2/EV-0: read_around で実際に精読した doc_id
    structural_evidence_meta: list = []       # list_docs/graph_neighbors の検証済み根拠 detail（Evidence ID 割当用）
    mt = max_tokens or _ANTHROPIC_MAX_TOKENS
    for _ in range(MAX_TURNS):
        if stop_event is not None and stop_event.is_set():
            return
        kwargs = {"model": model, "max_tokens": mt, "messages": messages, "tools": tools}
        if system:
            kwargs["system"] = system
        resp = client.messages.create(**kwargs)          # 非ストリーミング（ツールループはブロック取得）
        _acc_anthropic_usage(usage, resp)
        stop = getattr(resp, "stop_reason", None)
        blocks = list(getattr(resp, "content", None) or [])
        if stop == "refusal":                            # 安全上の理由で回答を控えた＝安全に終了
            yield _build_final_payload(_ANTHROPIC_REFUSAL, docs, searched, cites, cards,
                                       _usage_or_none(usage), verified_docs, "refusal", world,
                                       structural_evidence_meta=structural_evidence_meta)
            return
        tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
        if not tool_uses or stop == "max_tokens":        # ツール要求なし／打ち切り＝集めたテキストを最終回答に
            text = "".join(getattr(b, "text", "") for b in blocks
                           if getattr(b, "type", None) == "text").strip()
            committed, evidence_meta, dropped = _commit_evidence(cites, world)
            if dropped:
                # 一部 citation が検証で落ちた: 通常の会話履歴・ツール履歴・落とした draft は使わず、
                # system＋現在の質問＋Committed Evidence digest だけでクリーンな最終合成コンテキストを
                # 再構築する（`openai_style` と同じ方針）。再合成できなければ本文を返さない
                # （honest failure）。
                candidate_text, _finish_reason = _clean_resynthesis_anthropic(
                    client, model, system, user, mt, committed, usage)
            else:
                candidate_text = text
                _finish_reason = stop
            # EV-0（拡張設計 §4.4）: 帰属**直前**に停止状態を再確認し（`_post`/再合成の間に停止要求が
            # 来た窓を塞ぐ）、完了理由が自然完了 allowlist（"end_turn"/"stop_sequence"）に無ければ
            # 帰属を省略する（"max_tokens"・理由欠落等はすべて未完了扱い）。帰属呼び出しへは
            # `_redact` を通しただけのコピーを渡す（digest も生 doc_id/パスのまま・別名対応は不要）。
            _digest, _ev_map = build_evidence_digest(committed, evidence_meta + structural_evidence_meta)
            _natural = _is_natural_completion(_finish_reason, _ANTHROPIC_NATURAL_COMPLETION)
            if (stop_event is not None and stop_event.is_set()) or not _natural:
                _attributed: set = set()
            else:
                _attributed = attribute_anthropic(client, model, mt, _redact(candidate_text),
                                                  _digest, _ev_map, usage)
            _stop_reason = ("no_tool_calls" if _natural else _incomplete_stop_reason(
                _finish_reason, truncated=_ANTHROPIC_TRUNCATED, content_filtered=_ANTHROPIC_CONTENT_FILTERED))
            yield _finalize_payload(candidate_text, docs, searched, committed, evidence_meta, dropped,
                                    cards, _usage_or_none(usage), verified_docs, _stop_reason,
                                    structural_evidence_meta=structural_evidence_meta,
                                    used_evidence_docs=resolve_attributed_doc_ids(_attributed, _ev_map),
                                    attributed_ev_ids=_attributed)
            return
        searched = True
        messages.append({"role": "assistant", "content": resp.content})   # ブロックはそのまま履歴へ戻す
        results = []                                     # 全ツール結果を **1つの** user メッセージで返す
        # secRV MED-3（2026-07-18・DoS/コスト増幅）: openai_style と同じ上限を適用する（`MAX_TURNS` は
        # 応答ラウンド数だけを制限し、1応答内の tool_use 実行数は無制限だった）。
        # レビュー是正（LOW-D・secRV・2026-07-18 再検証）: 超過分は `tool_uses[:MAX_TOOLS_PER_TURN]` で
        # 単純に切り捨てる（超過件数分のノードを生成しない＝下のループ後に固定ノード1件だけ流す）。
        over_limit = len(tool_uses) > MAX_TOOLS_PER_TURN
        for tu in tool_uses[:MAX_TOOLS_PER_TURN]:
            # secRV MED-3 (b): 各ツール実行の直前に stop_event を確認する。
            if stop_event is not None and stop_event.is_set():
                return
            name = getattr(tu, "name", None)
            args = getattr(tu, "input", None) or {}      # SDK ではパース済み dict
            if not isinstance(args, dict):
                args = {}
            if name not in offered_names:
                # レビュー是正（FIX-H・secRV・2026-07-19・実行 allowlist の非対称）: 提示していない
                # ツール名の実行は拒否する（openai_style/gemini と同じ固定文言・safe_name クリップ・
                # total_tool_bytes 累計計上）。
                yield {"node": _node("許可外のツール呼び出し", "許可されていないため拒否しました")}
                safe_name = _clip_utf8_bytes(str(name or ""), _REJECTED_TOOL_NAME_MAX_BYTES)
                result = {"error": f"ツール {safe_name} は使用できません"}
                total_tool_bytes += _result_byte_size(result)
                if total_tool_bytes > tool_result_max_total_bytes:
                    yield {"node": _node("ツール結果の合計サイズ上限",
                                         "この会話で取得した量が多すぎるため打ち切りました")}
                    yield _build_final_payload("", docs, searched, cites, cards, _usage_or_none(usage),
                                               verified_docs, "budget_exceeded", world,
                                               structural_evidence_meta=structural_evidence_meta)
                    return
                results.append({"type": "tool_result", "tool_use_id": getattr(tu, "id", None),
                                "content": json.dumps(result, ensure_ascii=False)})
                continue
            yield {"node": _tool_node(name, args)}
            # レビュー是正（LOW-E・secRV・2026-07-18 再検証）: ノード yield 直後にも stop_event を
            # 再確認する（generator 再開後に ask_user 分岐/run_tool を1件実行してしまう窓を塞ぐ）。
            if stop_event is not None and stop_event.is_set():
                return
            if name == "ask_user":
                # 意味論（openai_style/gemini と同じ・意図的）: ask_user は **question 優先**。同一応答内で
                # ask_user より前に並んで実行済みの他ツールの結果（`results` の未 append 分・docs/cites/cards
                # は既に計算済みだが）はここで return するため呼び出し元へは渡らず破棄される（`final` を
                # yield しない）。ローカル `messages` も未使用のまま破棄されるので Anthropic API へは
                # 二度と送らない＝tool_use に対応する tool_result が欠けたまま送信されるプロトコル違反も
                # 起きない。ask_user の回答はフロントが新規メッセージとして再送し（chat_router の clarify
                # 再開）、次ターンは新しい messages で検索し直す＝この時点までの検索状態を持ち越す仕組みが
                # 元々無いので、破棄しても実害はない。
                yield {"question": _question_from_args(args)}
                return
            result, d, c, cd = run_tool(name, args, world, scope_paths, layer=layer,
                                        tool_result_max_bytes=tool_result_max_bytes)
            # 「何を探して・いくつ当たったか」の追加ノード（`_tool_node` は結果が出る前のノード
            # のため件数を書けない・`_hit_summary_node` 参照）。`anthropic_style` に
            # `allowed_tools`/サブ経路は無い＝常にメイン経路の表示。
            hit_node = _hit_summary_node(name, args, result)
            if hit_node:
                yield {"node": hit_node}
            # RV2（FBK-1・2026-09-01）: es_search が BM25 のみへ縮退した場合、その理由を「思考の
            # 流れ」へも表示する（サーバログの warning だけでは利用者に届かない・`_degrade_result_
            # node` 参照）。
            degrade_node = _degrade_result_node(result)
            if degrade_node:
                yield {"node": degrade_node}
            # S2（2026-09）: ripgrep_search が cap 打切りで探せていない文書を申告したら（
            # `truncated_docs`・run_tool 参照）、同じ枠組みでもう1件 yield する。
            truncated_node = _truncated_docs_node(result)
            if truncated_node:
                yield {"node": truncated_node}
            # レビュー是正（MED-B (c)・secRV・2026-07-18）: 1 run 累計の tool-result バイト量が
            # 上限を超えたら、この結果は破棄し固定エラーで run を打ち切る（fail-closed）。
            # レビュー是正（FIX-2・secRV・2026-07-19）: `cd`（cards サイドカー・`run_tool` 側で
            # 既に件数＋バイト上限クリップ済み＝`_clip_cards` 参照）の直列化バイトも累計へ計上する
            # （以前は `result` のみを計測しており、cards はこの計測経路をすり抜けていた）。
            total_tool_bytes += _result_byte_size(result) + _result_byte_size(cd)
            if total_tool_bytes > tool_result_max_total_bytes:
                yield {"node": _node("ツール結果の合計サイズ上限",
                                     "この会話で取得した量が多すぎるため打ち切りました")}
                yield _build_final_payload("", docs, searched, cites, cards, _usage_or_none(usage),
                                           verified_docs, "budget_exceeded", world,
                                           structural_evidence_meta=structural_evidence_meta)
                return
            docs |= d
            cites += c
            cards += cd
            # EXT-2/EV-0（拡張設計 §4.4）: read_around/read_doc を実際に呼んだ doc_id だけを
            # 「精読済み」にタグ付ける（`openai_style` と同じ規則）。
            if name in ("read_around", "read_doc") and "error" not in result:
                verified_docs |= d
            # list_docs／graph_neighbors は citation を生成しないが、それ自体が根拠として正当
            # （`openai_style` と同じ規則・§1 参照）。
            if name == "list_docs" and "error" not in result:
                # EV-0（拡張設計 §4.4）: list_docs は**呼び出し単位で集計した1 Evidence**とする
                # （総件数・適用条件・列挙範囲＋列挙した各パス）——0件の呼び出しも「該当0件」という
                # 具体的な事実として1 Evidence（ev-N）を持つ（根拠ゲート・帰属の対象になる）。
                _matched = [doc.get("rel_path") for doc in (result.get("docs") or [])
                           if doc.get("rel_path")]
                structural_evidence_meta.append({
                    "doc_id": None, "span": None, "verification_method": "list_docs_verified",
                    "list_meta": {"count": result.get("count", 0), "shown": len(_matched),
                                  "prefix": str(args.get("path_prefix") or "").strip(),
                                  "pattern": str(args.get("name_pattern") or "").strip()},
                    "matched_doc_ids": _matched})
            if name == "graph_neighbors" and cd:
                # `run_tool` が既にカード単位で裏付け doc を検証済み（無効カードは cd に含まれない・
                # `d` はその検証済み doc_id 集合そのもの）——ここで再検証しない。裏付け doc を
                # 主張しないカード（純粋なグラフ位相情報）は、Neo4j から実際に返ったノードである
                # こと自体を source_type=graph の構造 Evidence として計上する。
                structural_evidence_meta += _card_structural_evidence(cd)
            results.append({"type": "tool_result", "tool_use_id": getattr(tu, "id", None),
                            "content": json.dumps(result, ensure_ascii=False)})
        if over_limit:
            # レビュー是正（LOW-D）: 超過件数に関わらず固定ノード1件だけ生成する。
            yield {"node": _node("ツール呼び出し上限", "1回の応答あたりの実行数上限に達したため打ち切りました")}
            # secRV MED-3 (c): 上限超過＝以降のターンへは進まず、ここで打ち切る（未処理分の
            # tool_result が欠けたまま Anthropic API へ送り返さない＝プロトコル違反も避けられる）。
            yield _build_final_payload("", docs, searched, cites, cards, _usage_or_none(usage),
                                       verified_docs, "tools_per_turn_exceeded", world,
                                       structural_evidence_meta=structural_evidence_meta)
            return
        messages.append({"role": "user", "content": results})
    yield _build_final_payload("", docs, searched, cites, cards, _usage_or_none(usage),
                               verified_docs, "turns_exhausted", world,
                               structural_evidence_meta=structural_evidence_meta)


def gemini(api_key: str, model: str, system: str, user: str, world: str, scope_paths,
           toolset: list | None = None, stop_event=None, can_ask: bool = True,
           history: list | None = None, layer=None, tools_pref: dict | None = None,
           tools_availability: dict | None = None):
    """Gemini の function-calling を反復。`{"node":..}` を yield しつつ最後に `{"final","docs"}`。

    `layer`（省略可・既定 `None`＝`"both"`＝既存呼び出し元は無変更）: `openai_style` と同じ探す対象
    フィルタ（調べ方ブロック §3.4）。`run_tool` へそのまま転送する。

    `tools_pref`/`tools_availability`（省略可・既定 `None`・SC-6e）: `openai_style` と
    同じ検索経路トグル／可用性 snapshot（`toolset` 明示指定時はどちらも無視される）。

    `stop_event`（UI フィードバック1「途中停止」の RV MEDIUM 再検証・2026-07-03）: `openai_style` と
    同じ意味論＝各ターンのリクエスト発行前に確認し、立っていれば以降のリクエストを発行せず終了する。
    `history`（R1a・会話継続）: 直前ターンの (user, assistant) 対（時系列順・上流でキャップ済み）。
    Gemini の role（assistant→model）にマップして現在の user の前に並べる。省略/空なら従来と完全
    同一の初期 contents になる。`graph_admin.ask_graph` は位置引数で呼ぶため本引数に触れない
    ＝既定 None（空）で後方互換。

    レビュー是正（FIX-H・secRV・2026-07-19・実行 allowlist の非対称）: 実際に提示した `tools` から
    ツール名集合 `offered_names` を導出し、モデルが提示していないツール名を呼んでも `run_tool` を
    実行せず拒否する（`openai_style` と同じ対称化。Gemini は元々 `allowed_tools` 引数を持たない＝
    常に `offered_names` を allowlist として使う）。
    """
    url = llm.gemini_url(model)
    headers = llm.gemini_headers(api_key)
    _tp = tools_pref_mod.normalize_tools_pref(tools_pref)
    if toolset is not None:
        tools = toolset                # SC-6e: 明示指定時は可用性判定を一切参照しない（docstring 参照）
    else:
        _avail = tools_availability if tools_availability is not None else tool_availability()   # SC-6e
        tools = gemini_tools(
            with_es=_avail["fulltext"] and _tp["fulltext"], with_graph=_avail["graph"] and _tp["graph"],
            can_ask=can_ask, with_grep=_tp["grep"])
    offered_names = frozenset(
        fn.get("name") for group in tools for fn in (group.get("functionDeclarations") or []))
    contents = [{"role": ("model" if h.get("role") == "assistant" else "user"),
                "parts": [{"text": h.get("content", "")}]} for h in (history or [])]
    contents.append({"role": "user", "parts": [{"text": user}]})
    docs: set = set()
    cites: list = []
    cards: list = []
    searched = False
    usage = _new_usage_acc()                   # F3: 全ツールターンの usage を合算
    total_tool_bytes = 0                        # secRV MED-B (c): 1 run 累計の tool-result バイト量
    # BUDGET-1（§3.4）: run 開始時に1回だけ解決し、run の間ずっと使い回す（途中で admin が設定を
    # 変えても当該 run には影響しない）。BUDGET-2（§3.4）: provider="gemini"（現状ライブ窓照会も
    # シード表も対象外＝登録値/不明のみを通る・管理画面の登録欄で上書き可能）。
    tool_result_max_bytes, tool_result_max_total_bytes = resolve_tool_result_budgets(
        provider="gemini", model=model)
    verified_docs: set = set()                  # EXT-2/EV-0: read_around で実際に精読した doc_id
    structural_evidence_meta: list = []       # list_docs/graph_neighbors の検証済み根拠 detail（Evidence ID 割当用）
    for _ in range(MAX_TURNS):
        if stop_event is not None and stop_event.is_set():
            return
        body = {"system_instruction": {"parts": [{"text": system}]}, "contents": contents,
                "tools": tools, "generationConfig": {"temperature": 0.2}}
        resp = _post(url, headers, body)
        _acc_gemini_usage(usage, resp)
        cand0 = (resp.get("candidates") or [{}])[0]
        parts = (cand0.get("content") or {}).get("parts") or []
        calls = [p["functionCall"] for p in parts if isinstance(p, dict) and "functionCall" in p]
        if not calls:
            text = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
            committed, evidence_meta, dropped = _commit_evidence(cites, world)
            if dropped:
                # 一部 citation が検証で落ちた: 通常の会話履歴・ツール履歴・落とした draft は使わず、
                # system＋現在の質問＋Committed Evidence digest だけでクリーンな最終合成コンテキストを
                # 再構築する（`openai_style` と同じ方針）。再合成できなければ本文を返さない
                # （honest failure）。
                candidate_text, _finish_reason = _clean_resynthesis_gemini(url, headers, system, user,
                                                                           committed, usage)
            else:
                candidate_text = text
                _finish_reason = cand0.get("finishReason")
            # EV-0（拡張設計 §4.4）: 帰属**直前**に停止状態を再確認し、完了理由が自然完了 allowlist
            # （"STOP"）に無ければ帰属を省略する（"MAX_TOKENS"・理由欠落・`SAFETY` 等はすべて
            # 未完了扱い・read_around のみへ縮退）。帰属呼び出しへは `_redact` を通しただけの
            # コピーを渡す（digest も生 doc_id/パスのまま・別名対応は不要）。
            _digest, _ev_map = build_evidence_digest(committed, evidence_meta + structural_evidence_meta)
            _natural = _is_natural_completion(_finish_reason, _GEMINI_NATURAL_COMPLETION)
            if (stop_event is not None and stop_event.is_set()) or not _natural:
                _attributed: set = set()
            else:
                _attributed = attribute_gemini(url, headers, _redact(candidate_text), _digest, _ev_map, usage)
            _stop_reason = ("no_tool_calls" if _natural else _incomplete_stop_reason(
                _finish_reason, truncated=_GEMINI_TRUNCATED, content_filtered=_GEMINI_CONTENT_FILTERED))
            yield _finalize_payload(candidate_text, docs, searched, committed, evidence_meta, dropped,
                                    cards, _usage_or_none(usage), verified_docs, _stop_reason,
                                    structural_evidence_meta=structural_evidence_meta,
                                    used_evidence_docs=resolve_attributed_doc_ids(_attributed, _ev_map),
                                    attributed_ev_ids=_attributed)
            return
        searched = True
        contents.append({"role": "model", "parts": parts})
        resp_parts = []
        # secRV MED-3（2026-07-18・DoS/コスト増幅）: openai_style/anthropic_style と同じ上限を適用する。
        # レビュー是正（LOW-D・secRV・2026-07-18 再検証）: 超過分は `calls[:MAX_TOOLS_PER_TURN]` で
        # 単純に切り捨てる（超過件数分のノードを生成しない＝下のループ後に固定ノード1件だけ流す）。
        over_limit = len(calls) > MAX_TOOLS_PER_TURN
        for fc in calls[:MAX_TOOLS_PER_TURN]:
            # secRV MED-3 (b): 各ツール実行の直前に stop_event を確認する。
            if stop_event is not None and stop_event.is_set():
                return
            name = fc.get("name")
            args = fc.get("args") or {}
            if name not in offered_names:
                # レビュー是正（FIX-H・secRV・2026-07-19・実行 allowlist の非対称）: 提示していない
                # ツール名の実行は拒否する（openai_style と同じ固定文言・safe_name クリップ・
                # total_tool_bytes 累計計上）。
                yield {"node": _node("許可外のツール呼び出し", "許可されていないため拒否しました")}
                safe_name = _clip_utf8_bytes(str(name or ""), _REJECTED_TOOL_NAME_MAX_BYTES)
                result = {"error": f"ツール {safe_name} は使用できません"}
                total_tool_bytes += _result_byte_size(result)
                if total_tool_bytes > tool_result_max_total_bytes:
                    yield {"node": _node("ツール結果の合計サイズ上限",
                                         "この会話で取得した量が多すぎるため打ち切りました")}
                    yield _build_final_payload("", docs, searched, cites, cards, _usage_or_none(usage),
                                               verified_docs, "budget_exceeded", world,
                                               structural_evidence_meta=structural_evidence_meta)
                    return
                resp_parts.append({"functionResponse": {"name": name, "response": result}})
                continue
            yield {"node": _tool_node(name, args)}
            # レビュー是正（LOW-E・secRV・2026-07-18 再検証）: ノード yield 直後にも stop_event を
            # 再確認する（generator 再開後に ask_user 分岐/run_tool を1件実行してしまう窓を塞ぐ）。
            if stop_event is not None and stop_event.is_set():
                return
            if name == "ask_user":
                # 意味論（openai_style/anthropic_style と同じ・意図的）: ask_user は **question 優先**。
                # 同一応答内で ask_user より前に並んで実行済みの他ツールの結果（resp_parts の未 append
                # 分・docs/cites/cards は既に計算済みだが）はここで return するため呼び出し元へは渡らず
                # 破棄される（`final` を yield しない）。次ターンはフロント再送で新しい contents から検索
                # し直すため、破棄しても実害はない（agentic_search.anthropic_style のコメント参照）。
                yield {"question": _question_from_args(args)}
                return
            result, d, c, cd = run_tool(name, args, world, scope_paths, layer=layer,
                                        tool_result_max_bytes=tool_result_max_bytes)
            # 「何を探して・いくつ当たったか」の追加ノード（`_tool_node` は結果が出る前のノード
            # のため件数を書けない・`_hit_summary_node` 参照）。`gemini` に `allowed_tools`/
            # サブ経路は無い＝常にメイン経路の表示。
            hit_node = _hit_summary_node(name, args, result)
            if hit_node:
                yield {"node": hit_node}
            # RV2（FBK-1・2026-09-01）: es_search が BM25 のみへ縮退した場合、その理由を「思考の
            # 流れ」へも表示する（サーバログの warning だけでは利用者に届かない・`_degrade_result_
            # node` 参照）。
            degrade_node = _degrade_result_node(result)
            if degrade_node:
                yield {"node": degrade_node}
            # S2（2026-09）: ripgrep_search が cap 打切りで探せていない文書を申告したら（
            # `truncated_docs`・run_tool 参照）、同じ枠組みでもう1件 yield する。
            truncated_node = _truncated_docs_node(result)
            if truncated_node:
                yield {"node": truncated_node}
            # レビュー是正（MED-B (c)・secRV・2026-07-18）: 1 run 累計の tool-result バイト量が
            # 上限を超えたら、この結果は破棄し固定エラーで run を打ち切る（fail-closed）。
            # レビュー是正（FIX-2・secRV・2026-07-19）: `cd`（cards サイドカー・`run_tool` 側で
            # 既に件数＋バイト上限クリップ済み＝`_clip_cards` 参照）の直列化バイトも累計へ計上する
            # （以前は `result` のみを計測しており、cards はこの計測経路をすり抜けていた）。
            total_tool_bytes += _result_byte_size(result) + _result_byte_size(cd)
            if total_tool_bytes > tool_result_max_total_bytes:
                yield {"node": _node("ツール結果の合計サイズ上限",
                                     "この会話で取得した量が多すぎるため打ち切りました")}
                yield _build_final_payload("", docs, searched, cites, cards, _usage_or_none(usage),
                                           verified_docs, "budget_exceeded", world,
                                           structural_evidence_meta=structural_evidence_meta)
                return
            docs |= d
            cites += c
            cards += cd
            # EXT-2/EV-0（拡張設計 §4.4）: read_around/read_doc を実際に呼んだ doc_id だけを
            # 「精読済み」にタグ付ける（`openai_style` と同じ規則）。
            if name in ("read_around", "read_doc") and "error" not in result:
                verified_docs |= d
            # list_docs／graph_neighbors は citation を生成しないが、それ自体が根拠として正当
            # （`openai_style` と同じ規則・§1 参照）。
            if name == "list_docs" and "error" not in result:
                # EV-0（拡張設計 §4.4）: list_docs は**呼び出し単位で集計した1 Evidence**とする
                # （総件数・適用条件・列挙範囲＋列挙した各パス）——0件の呼び出しも「該当0件」という
                # 具体的な事実として1 Evidence（ev-N）を持つ（根拠ゲート・帰属の対象になる）。
                _matched = [doc.get("rel_path") for doc in (result.get("docs") or [])
                           if doc.get("rel_path")]
                structural_evidence_meta.append({
                    "doc_id": None, "span": None, "verification_method": "list_docs_verified",
                    "list_meta": {"count": result.get("count", 0), "shown": len(_matched),
                                  "prefix": str(args.get("path_prefix") or "").strip(),
                                  "pattern": str(args.get("name_pattern") or "").strip()},
                    "matched_doc_ids": _matched})
            if name == "graph_neighbors" and cd:
                # `run_tool` が既にカード単位で裏付け doc を検証済み（無効カードは cd に含まれない・
                # `d` はその検証済み doc_id 集合そのもの）——ここで再検証しない。裏付け doc を
                # 主張しないカード（純粋なグラフ位相情報）は、Neo4j から実際に返ったノードである
                # こと自体を source_type=graph の構造 Evidence として計上する。
                structural_evidence_meta += _card_structural_evidence(cd)
            resp_parts.append({"functionResponse": {"name": name, "response": result}})
        if over_limit:
            # レビュー是正（LOW-D）: 超過件数に関わらず固定ノード1件だけ生成する。
            yield {"node": _node("ツール呼び出し上限", "1回の応答あたりの実行数上限に達したため打ち切りました")}
            # secRV MED-3 (c): 上限超過＝以降のターンへは進まず、ここで打ち切る。
            yield _build_final_payload("", docs, searched, cites, cards, _usage_or_none(usage),
                                       verified_docs, "tools_per_turn_exceeded", world,
                                       structural_evidence_meta=structural_evidence_meta)
            return
        contents.append({"role": "user", "parts": resp_parts})
    yield _build_final_payload("", docs, searched, cites, cards, _usage_or_none(usage),
                               verified_docs, "turns_exhausted", world,
                               structural_evidence_meta=structural_evidence_meta)
