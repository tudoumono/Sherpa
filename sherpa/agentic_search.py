"""エージェント検索（インデックス無し・LLM が grep ツールを反復呼び出し）。

参考思想（zenn: agentic search no-index）: 事前インデックスを作らず、LLM に検索ツールを渡して
**ripgrep_search で当たり → read_around で精読 → クエリ修正 → 反復**させる。Sherpa 既存の
`grep_tool.grep_search`（索引なし全文 grep・world ツリー＋Office派生MD）を土台に、OpenAI/Gemini/
Ollama の function-calling で回す。範囲は **選択中の資料フォルダ＋scope のみ**・read-only・本文テキストのみ送信。

このモジュールは LLM プロバイダに依存しない（HTTP は `_post`・テストで差し替え可）。各 loop は
`{"node": <思考ノード>}` を yield しつつ、最後に `{"final": <回答>, "docs": <参照 doc_id 集合>}` を yield。
"""
from __future__ import annotations

import concurrent.futures
import contextvars
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

from . import citations, es_index, exec_event, grep_tool, investigation_state, llm, redact_keys, stop_kind, worlds
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
# 空回答で打ち切ると、呼び出し元がそれまでの資料・引用を全部捨てて単発 grep へ落ちてしまう
# （集めた材料を活かせないまま入力が小さいだけの回答になる）。
_FINAL_SYNTHESIS = (
    "調査の上限に達しました。**これ以上ツールは使えません**。"
    "ここまでに取得した内容だけを根拠に、日本語で回答してください（長さは絞らない・集めた内容は削らない）。"
    "確認できたこと（確定）と確認できなかったことを分けて書き、"
    "取得した内容に無いことを補うときは『推定』と明示する。"
)
# EXT-3（拡張設計 §3.5）: 評価フェーズが sufficient と判定したときの最終合成指示。上限到達時の
# `_FINAL_SYNTHESIS`（「上限に達した」）とは意味が異なるため文言を分ける（sufficient を上限到達と
# 誤表示しない）。
_FINAL_SYNTHESIS_SUFFICIENT = (
    "十分な根拠が集まりました。ここまでに取得した内容だけを根拠に、日本語で"
    "回答してください（長さは絞らない・集めた内容は削らない）。"
    "確認できたこと（確定）と確認できなかったことを分けて書き、"
    "取得した内容に無いことを補うときは『推定』と明示する。"
)


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    """security-limit 系 env の整数解析（負値/巨大値対策）。

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
# `MAX_TURNS` は LLM 応答ラウンド数だけを制限し、1応答内で
# モデルが返す tool_calls の**個数**を制限しなければ無制限に実行できてしまう（no-hit grep は毎回 world 全走査＝
# 1応答に大量のツール呼び出しを積むだけで実処理量を増幅できる）。1応答あたりの実行数上限を独立に
# 設ける（既定16は通常のツール呼び出し数を十分上回るため正常系には影響しない）。
# 負値でスライスが反転し上限が無効化されるため `_env_int` で範囲検証する（hard cap 256）。
MAX_TOOLS_PER_TURN = _env_int("SHERPA_AGENTIC_MAX_TOOLS_PER_TURN", 16, 1, 256)


def effective_max_tools_per_turn(system_settings: dict) -> int:
    """API の1応答内の実行上限。管理者の保存値を優先し、未設定なら既存の環境設定を使う。"""
    configured = system_settings.get("agentic_max_tools_per_turn")
    return MAX_TOOLS_PER_TURN if configured is None else configured


# D1（調査結果集約と並列実行の改善方針・ツール並列）: 1 応答内に ask_user を含まない呼び出しが
# 2 本以上あるとき、`ThreadPoolExecutor(max_workers=SHERPA_TOOL_PARALLEL)` で同時実行する worker
# 数（各 dialect のツール実行ループ参照）。1 なら常に従来どおり直列（並列分岐そのものに入らない）。
SHERPA_TOOL_PARALLEL = _env_int("SHERPA_TOOL_PARALLEL", 3, 1, 8)
# C2（探索ループの文脈整理・調査結果集約と並列実行の改善方針）: `msgs`（会話履歴）が この
# バイト数を超えたら、最新 `SHERPA_AGENTIC_KEEP_RECENT_TOOLS` 回分のツール往復を残し、それより
# 古い分を `InvestigationState.render()` の要約1通へ置換する。上限値の一括増量はしない方針のため
# 既定値は現行の実効窓（`TOOL_RESULT_MAX_TOTAL_BYTES` 等）を変えない範囲で選ぶ。
SHERPA_AGENTIC_CONTEXT_BUDGET_BYTES = _env_int(
    "SHERPA_AGENTIC_CONTEXT_BUDGET_BYTES", 96 * 1024, 16 * 1024, 1024 * 1024)
# 置換後も直近この回数分のツール往復は生のまま残す（0 を許すとゼロ除算相当のスライス事故
# （`lst[-0]` は `lst[0]` と等価＝反転）を招くため下限1・`_env_int` の閉区間クランプで機械的に防ぐ）。
SHERPA_AGENTIC_KEEP_RECENT_TOOLS = _env_int("SHERPA_AGENTIC_KEEP_RECENT_TOOLS", 4, 1, 50)
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
              # が一致する（office_md.IMAGE_EXT が真実源・grep と read_around の非対称を画像でも防ぐ）。
              ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff"}   # 本文は派生MD側にある
# ⚠ 旧形式（.doc/.xls/.ppt）は legacy_backend（W0）が前段変換した OOXML を①アームが MD化する。
# 解決規約は新形式と同じ **原本 rel + ".md"**（office_md.build_derived が出力名を原本 rel に揃えている）ので
# ここでの分岐は不要＝ _OFFICE_MD に加えるだけで grep_search（derived md/ を直接見る）と read_around が一致する
# （追加しないと grep はヒットするが read_around が拒否＝精読不可という非対称が生じる）。
# read_around で読める本文種別だけ（.env 等の秘匿ファイルを LLM に読ませない・RV BLOCKER）。
# ソース原文（コード）分はアナライザ登録簿が単一の真実源（§2.4）。
# 軽量テキスト枠（`ingest.text_kind`）の第1段拡張子マップ（CODE_EXT/DOCUMENT_EXT）も対象に含める
# ——`.env`/`.key`（`text_kind.SENSITIVE_EXT`／ドットファイル名判定）は元々この2集合に含まれず、
# `classify_document()`（下の `_safe_doc_path`）が最終判定でも秘匿ファイル・意味層内部制御ファイル
# （`worlds.is_semantic_control_path`）を対象外へ倒すため、ここに加えても RV BLOCKER の意図は
# 破らない。grep_search（`grep_tool._TEXT_EXT` も同じ集合を追加済み）と read_around の対称性を保つ
# （追加しないと「grep はヒットするが read_around が拒否」という同型の非対称が生じる）。
_READABLE_EXT = ({".md", ".markdown", ".txt"} | _analyzer_registry.registered_extensions() | _OFFICE_MD
                | text_kind.CODE_EXT | text_kind.DOCUMENT_EXT)

# tool result（外部 LLM へ渡る）から明らかな秘密を伏せる（多層防御）。
_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{6,}"
    r"|-----BEGIN[^-]+PRIVATE KEY-----[\s\S]*?-----END[^-]+PRIVATE KEY-----)")
_KV_SECRET_RE = re.compile(r"(?i)\b(pass(?:word|wd)?|secret|api[_-]?key|token|authorization)\b(\s*[=:]\s*)(\S+)")

# `_SECRET_RE` の PRIVATE KEY ブロックは BEGIN/END が対で揃って初めてマッチする——
# `doc_readers.file_head` の `max_bytes` 切断（先頭バイトだけを OS 読み取り自体の上限で切る・
# clean はその後にしか掛からない）や `_finish_reader_result` の二分探索クリップ（file_head の
# `text` は文字列そのものが対象＝途中で切れる）で END 側が失われると、`_SECRET_RE` は対応する
# END が見当たらないため一切マッチせず、鍵の断片がそのまま外部 LLM の tool 結果へ残ってしまう
# （切断前に丸ごと読めていない箇所は元々救えないが、読めた範囲に残った断片自体は伏せる）。
# 実際の状態付き伏せ字（鍵ブロックの内側にいるかを次の要素へ持ち越す）は `redact_keys.
# KeyBlockRedactor` が担う（`doc_readers` が切り詰める前に一次適用し、下の `_redact_deep` は
# 結果全体を辿る多層防御としてこれを再利用する）。


def _redact(text: str) -> str:
    t = _SECRET_RE.sub("[REDACTED]", text or "")
    return _KV_SECRET_RE.sub(r"\1\2[REDACTED]", t)


def _walk_redact(obj, redactor):
    if isinstance(obj, str):
        return redactor(obj)
    if isinstance(obj, list):
        return [_walk_redact(v, redactor) for v in obj]
    if isinstance(obj, dict):
        return {k: _walk_redact(v, redactor) for k, v in obj.items()}
    return obj


def _redact_deep(obj):
    """`_redact` を dict/list を再帰的に辿って全ての文字列値へ適用する（S3b・原本読取ツール専用）。

    `doc_readers` の各関数はセル/段落/スライド/ページの本文をネスト構造（list of list・
    list of dict 等）でそのまま返すため、`_redact`（文字列専用）を個々のツールごとに手で
    辿るより一箇所に集約する。数値/真偽値/None は素通し（対象外）。

    走査自体は `redact_keys.KeyBlockRedactor`（`_redact` を土台にした使い捨てインスタンス）に
    委譲し、辞書/リストの出現順（＝ `doc_readers` が構造を作った順＝文書順）を1本の状態付き
    スキャンとして扱う——PEM 秘密鍵の BEGIN/END が別要素（別段落・別セル・別ページ）にまたがっても
    取りこぼさない。`doc_readers` は既にこの伏せ字を切り詰める前の段階で適用済みのため、ここは
    その後の多層防御（同じ状態機械をもう一段掛けるだけで、既に伏せられた文字列は素通りする）。
    """
    return _walk_redact(obj, redact_keys.KeyBlockRedactor(_redact))


# `read_around` は window（行数）でしか出力を絞らず、
# 単一行が巨大（例: 10MB の1行だけの文書）だと行数上限が実質無意味＝返却バイト量が無制限になる
# （1ターン内で `SHERPA_AGENTIC_MAX_TOOLS_PER_TURN` 回呼ばれると履歴/SSE/次ターンの LLM 要求へ
# 複製される総量が跳ね上がる）。(a) 返却テキストの UTF-8 バイト上限で切り詰める。(b) `Path.read_text()`（ファイル全体を
# 一括ロード）ではなく、生バイトを `_READ_AROUND_FILE_CAP_BYTES` までに制限して読む（巨大な単一行
# ファイルでも読み込み自体が無制限に増幅しない）。
# (b) の `_READ_AROUND_FILE_CAP_BYTES` は負値でクリップが反転するため `_env_int` で [64KiB, 64MiB] に
# 範囲検証する（per-call 予算の検証は settings 側の `_clamped_setting_int` が [1KiB, 8MiB] で行う）。
# BUDGET-1（§3.4）: 既定は精度優先（憲法1条「アプリは性能を黙って下げない」）——サーバメモリ対策
# としての制約は read 側のストリーミング化により役目を終えている。管理画面（`agentic_budget.per_result`）
# へ昇格済み（UI(DB)が唯一の真実源）のため env フォールバックは持たない——ここでの値は
# コード既定として `resolve_tool_result_budgets()` の settings 未設定時フォールバックに
# そのまま使う（settings 段は下の resolver が1段重ねる）。
TOOL_RESULT_MAX_BYTES = 262144
# 1 run（1回の agentic ループ全体＝`openai_style`/`gemini`/`anthropic_style` の1呼び出し）で許容する
# tool-result 累計バイト上限（3 dialect 全てで使う）。超過時は固定エラーで run を
# 打ち切る（fail-closed）。
# BUDGET-1: 既定は per-call 既定の16倍（同じ比率をコード既定として固定するだけで、settings で
# per-call だけ変えても total には連動しない——2キーは独立に解決する。§3.4「即時」段の値と一致）。
# 管理画面（`agentic_budget.total`）へ昇格済みのため env フォールバックは持たない。
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
    """BUDGET-2（§3.4・min() 方式）: `base`（BUDGET-1 の解決値）と「選択中モデルの
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
# read_around/read_doc/doc_outline/verify_citation がディスクから読む生バイト数の上限。
# `grep_tool._GREP_FILE_CAP_BYTES` と同じ役割（1ファイルにかける読み取り
# コストの安全弁）——env で個別に変更できるが、既定は grep 側の cap（既定64MiB・ストリーミング化して
# メモリ非比例になっているため引き上げてある）と揃える。揃えないと「grep が
# cap より後ろでヒットを見つけたのに read_doc/read_around がそこを読めない」という食い違いが
# 生まれる（grep 側は行単位のストリーミングでメモリを頭打ちにするが、read_around 側は
# 「1ヒット周辺だけを読む」用途で全量ロードのままでも実害が薄いため、ここでは cap を揃えるだけに
# 留める＝read 側自体のストリーミング化は別契約）。
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
# 許可外ツール拒否結果に埋める（モデル生成の）ツール名の上限バイト数。
# 拒否理由がどのツール名かをモデルへ伝える最小限の情報量で十分＝短い固定長で足りる。
_REJECTED_TOOL_NAME_MAX_BYTES = 32
# 親返し（L4c・§3.3/§3.4）: es_search のヒットを doc_id で束ね、予算内なら rag.md 全文(P3)／
# 領域(P2)を返す。常時 ON（グローバルな系統切替トグル
# `SHERPA_ES_PARENT_RETURN` は撤去済み・復活させない）。
# P2（領域）の対象チャンク集合を ES から引く際の1クエリあたりの取得上限（`es_index.
# chunk_ids_for_parent` の `limit`）。region はどのみち byte_cap（予算）で頭打ちになるため、
# ここは「1回のクエリで返す chunk_id の個数」自体の安全弁——ES の既定 `max_result_window`
# （10000）を十分下回る固定値（env 化はしない・§3.4 は新しい env を増やさない方針）。
_PARENT_RETURN_REGION_CHUNKS_MAX = 5000


def _parent_return_enabled() -> bool:
    """常時 True（グローバルな系統切替トグル `SHERPA_ES_PARENT_RETURN` は撤去済み・復活させない・
    `grep_tool.rag_grep_enabled`/`es_index.rag_es_enabled` と同じ扱い）。既存の
    呼び出し形（`run_tool` の `parent_return_on` 判定）を変えない最小変更として関数自体は残す。"""
    return True


def _clip_utf8_bytes(s: str, max_bytes: int) -> str:
    """UTF-8 エンコード後のバイト数が `max_bytes` を超えないよう `s` を切り詰める。

    マルチバイト文字の境界で分割されても壊れた文字が残らないよう `errors="ignore"` で再デコードする。
    """
    b = s.encode("utf-8")
    if len(b) <= max_bytes:
        return s
    return b[:max_bytes].decode("utf-8", errors="ignore")


# 直列化不能時のフォールバック値。実運用のどんな上限設定
# （既定 256KiB/4MiB）よりも確実に大きい値にし、「測定不能＝上限超過扱い」を機械的に保証する。
_UNMEASURABLE_SIZE = 1 << 40


def _result_byte_size(result) -> int:
    """JSON 化した際の概算 UTF-8 バイト数（1 run 累計上限の判定に使う）。

    `run_tool` の戻り値の1つ目（tool result dict）だけでなく、4つ目（`cards` サイドカー・
    `list[dict]`）にも同じ関数を使う。

    測定不能（シリアライズできない要素＝bytes・非JSON型・不正 Unicode 等）は「特大
    （`_UNMEASURABLE_SIZE`）」として扱う（fail-closed）——`0`（無料）として扱うと、個別上限
    （`_clip_cards` の `max_bytes`）・累計上限（`TOOL_RESULT_MAX_TOTAL_BYTES`）の両方を無条件に
    すり抜けてしまう。呼び出し側の上限判定（`_clip_cards` の候補リスト全体サイズ判定・3 dialect
    の累計判定）はこの大きな値を受けて必ず「上限超過」と判断する＝直列化不能な要素は個別クリップ
    では弾かれ、累計判定では run が打ち切られる。
    """
    try:
        return len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    except Exception:
        return _UNMEASURABLE_SIZE


def _messages_byte_size(msgs: list) -> int:
    """C2（探索ループの文脈整理）: 会話履歴（`msgs`/`messages`/`contents`）全体の概算 UTF-8
    バイト数。`_result_byte_size` と違い `default=str` を渡す——Anthropic 方言の `messages` は
    SDK のブロックオブジェクト（Pydantic 等・素の `json.dumps` では直列化できない）を**通常運用で
    毎回**含むため、失敗＝異常ではなく「文字列化してでも概算する」を既定にする（fail-closed で
    特大値を返すと Anthropic 方言だけ毎ターン強制的に文脈整理が発動してしまう）。
    """
    try:
        return len(json.dumps(msgs, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:
        return _UNMEASURABLE_SIZE


def _read_evidence_payload(state: "investigation_state.InvestigationState") -> list:
    """C2/C3: 探索ループのローカル `InvestigationState` から kind="read"（read_around/read_doc・
    S3b 原本読取ツールの精読結果）だけを抜き出し、`final` payload の `read_evidence` キー用に
    薄く写す（`Evidence` データクラス自体は payload に出さない＝内部専用の実装詳細を漏らさない）。

    `locator`（原本読取ツール・span=None のときだけ持つ＝"Sheet1!A1:D20" 等）を `text` へ
    前置する——清書（`build_synthesis_digest` の `read_evidence` 引数）は `doc_id`/`span`/`text`
    しか読まないため、`locator` を別キーで足すだけでは清書側に伝わらない。同じ doc_id で
    複数エントリ（別シート等）になった場合に、どの箇所の精読かを清書入力の上でも区別できる
    ようにする。

    `locator` を独立フィールドとしても持つ（`text` への前置はそのまま残す）——
    ハイブリッド（`providers/base.py::_ingest_sub_final_into_state`）が下調べ役の `final` から
    親 `InvestigationState` へこの payload を再取り込みする際、`text` の前置文字列だけでは
    `locator` を復元できず、親側の `InvestigationState._find`（kind="read"／span=None は
    `locator` も同一性の鍵に使う）が別シート/別ページの読み取りを区別できずに1件へ潰していた。

    `text_truncated` も添える——`InvestigationState._READ_TEXT_CAP_BYTES` の保存上限で
    本文の末尾が落ちているかを `build_synthesis_digest` が清書入力の精読行へ注記できるように
    する（`_ingest_sub_final_into_state` が親 `InvestigationState` へ再取り込みする際にも同じ
    キーを引き継ぐ）。
    
    加えて glob_search／doc_outline／compare_documents の確定事実（kind="list"/"outline"/"compare"・`kind`
    と `source_tool` を持ち `doc_id` は無いことがある）も同じ列に載せる（清書はその要約行をそのまま使い、
    再取り込みは同種の Evidence として引き継ぐ）。
    """
    out = []
    for e in state.evidence:
        if e.kind in ("list", "outline", "compare") and e.source_tool in _STATE_FACT_TOOLS:
            # glob_search／doc_outline／compare_documents の確定事実は `combined_evidence_meta` に載らない
            # （list_docs／folder_tree／graph_neighbors だけが載る）ため、同じ内部専用チャンネルで清書へ渡す。
            out.append({"kind": e.kind, "source_tool": e.source_tool, "doc_id": e.doc_id, "span": None,
                        "text": e.text, "locator": None, "text_truncated": False})
            continue
        if e.kind != "read":
            continue
        text = f"{e.locator}: {e.text}" if (e.locator and e.text) else e.text
        out.append({"doc_id": e.doc_id, "span": list(e.span) if e.span else None, "text": text,
                   "locator": e.locator, "text_truncated": e.text_truncated})
    return out


# 清書へ内部専用行として渡す構造的事実の出どころ（`combined_evidence_meta` に載らないツール）。
_STATE_FACT_TOOLS = frozenset({"glob_search", "doc_outline", "compare_documents"})


def _tool_bytes_over_budget(total_tool_bytes: int, shared_budget: dict | None,
                            max_total_bytes: int | None = None) -> bool:
    """S4-b（複数プロファイル横断予算・§6.2 項1）: per-run 上限（`max_total_bytes`）
    **または** 共有予算（`shared_budget["tool_bytes_used"] > shared_budget["tool_bytes_max"]`）の
    どちらか一方でも超過していれば True（fail-closed・`openai_style` の既存打ち切り分岐が使う）。
    `shared_budget` が None（既定）なら per-run 上限のみで判定する（既存呼び出し元は不変）。

    `max_total_bytes`（省略可・既定 `None`＝モジュール既定 `TOOL_RESULT_MAX_TOTAL_BYTES`＝既存
    呼び出し元は無変更・BUDGET-1 §3.4）: 呼び出し元が run 開始時に1回だけ
    `resolve_tool_result_budgets()` で解決した実効値。

    形が不正（キー欠損・非数値・
    used が負・max<=0）な shared_budget は「判定不能」として **over-budget 扱い＝fail-closed**。
    直アクセス（KeyError）や負値 used による予算の実質増加を防ぐ。"""
    limit = max_total_bytes if max_total_bytes is not None else TOOL_RESULT_MAX_TOTAL_BYTES
    if total_tool_bytes > limit:
        return True
    if shared_budget is None:
        return False
    # 片側キー欠損（例 {"tool_bytes_max": 100}）も「形が不正＝判定不能」として
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
    切り詰める（cards サイドカーのバイト迂回を防ぐ）。

    LLM 向け `view` は元から `cards[:_GRAPH_CARDS_MAX]` で件数制限していたが、4つ目の戻り値（呼び出し元の
    3 dialect が `cards += cd` で蓄積し最終的に troubleshoot の `data.candidates` へ載るサイドカー）
    はそれとは独立に無制限で返しており、`total_tool_bytes` の計測対象にも入らなかった。件数上限
    到達、または直列化バイト上限に達した時点で打ち切る（超過分は捨てる・fail-closed）。Neo4j 側の
    取得件数上限（`lens_service`）は範囲外のため触らず、ここで受け取った後にクリップする。

    先頭カードを特別扱いせず、各カード（先頭含む）を仮に追加した**候補リスト全体**の実直列化バイト数
    （`[`/`]`/`,` 等の区切り込み・個別要素バイトの単純合計ではない）が `max_bytes` を超えるなら、
    そのカードを追加せず打ち切る（`out` が空のままでも巨大な単一カードは弾く——`out` が空のときだけ
    判定をスキップすると、単一の巨大カードが常に無条件で通ってしまう）。
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
    if text_kind.is_sensitive(Path(doc_id).name, ext):
        # 秘匿名: Office/画像（`is_office` 分岐）は下の `classify_document` を
        # 一切通らないため、ここで先に塞がないと `credentials.xlsx`/`id_rsa.docx` 等の派生MDが
        # 実在確認だけで精読（外部 LLM 送信）まで到達してしまう。非Office拡張子は
        # `classify_document` 側でも同じ判定に落ちるが、意図を明示するためここでも一律に弾く。
        _log.warning("read_around: 秘匿名のため対象外にしました（ext=%s）", ext)
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
        verdict = corpus_docs.classify_document(
            doc_id, ext, lambda p=rp, size=4096: corpus_docs._read_head(p, size))
        if verdict["kind"] == "unreadable" or (verdict["kind"] != "code" and verdict.get("doctype") is None):
            return None
        is_code = verdict["kind"] == "code"
    if layer is not None and not layer_mod.in_layer_code(is_code, layer):
        return None
    return root, lexical_rel, rp


# 原本読取ツール（S3b・`doc_readers.py`）専用の doc_id 解決に許す拡張子（小文字・ドット付き）。
# `.xlsm` は台帳（`corpus_docs.classify_document`）が文書種別として扱わない拡張子
# ＝`verify_doc_exists` が常に False を返し、事前フィルタで通しても後続の確定判定で必ず落ちる
# （入口で通す意味が無い・利用者に「読めるはず」と誤解させるだけ）ため対象外とする。`.xlsx` のみ。
_XLSX_KINDS = frozenset({".xlsx"})
_DOCX_KINDS = frozenset({".docx"})
_PPTX_KINDS = frozenset({".pptx"})
_PDF_KINDS = frozenset({".pdf"})
# file_head はテキスト・コードのみ（Office/PDF/画像は専用ツールに任せる・_READABLE_EXT から
# それらを引いた集合＝`grep_search`/`read_around` が読める本文種別と同じ土台）。
_FILE_HEAD_KINDS = frozenset(_READABLE_EXT - _OFFICE_MD)


def _safe_original_path(world: str, doc_id: str, scope_paths, *, kinds: frozenset, layer=None):
    """原本読取ツール（xlsx/docx/pptx/pdf/file_head）専用の doc_id→`(root, doc_id, 実パス, stat)` 解決。

    `_safe_doc_path` と同じ検査項目（トラバーサル拒否・拡張子の事前フィルタ・重要度制御ファイル
    除外・秘匿名除外・realpath 封じ込め・symlink 拒否・regular file）を共有するが、Office/PDF に
    ついても**派生 MD ではなく world root の原本**へ解決する——`_safe_doc_path` は Office/PDF を
    常に派生 MD 側へ解決するため兼用できない（原本読取ツールの目的そのものが原本を読むこと）。

    実在・文書種別・scope の確認は `verify_doc_exists`（台帳と同じ確定判定）をそのまま再利用する
    （二重実装しない）。`kinds`（必須・キーワード専用）はこのツールが扱える拡張子集合。`layer`
    （省略可）は file_head 専用——指定時は `classify_document` 確定判定（`layer_mod.in_layer_code`）
    で層一致も見る（Office/PDF の5ツールは呼び出し側 `run_tool` が層で分岐済み＝常に docs 側扱い
    のため `layer` を渡さない）。

    無効/範囲外/拡張子不一致/秘匿名/重要度制御/traversal/symlink/非regular/未実在/doctype不明は
    すべて `None`。

    戻り値の4つ目 `stat` は検査完了直後にこの関数自身が取った `rp.stat()`——呼び出し元
    （`run_tool`）はこの後 `open()` するまでの間に `rp` が symlink 等に差し替えられていないかを
    `os.fstat` の (st_dev, st_ino) と突き合わせて確認する（検査後の再オープンで封じ込めを破る
    TOCTOU 対策・`_open_verified_original` 参照）。
    """
    if not doc_id or doc_id.startswith("/") or "\\" in doc_id or "\x00" in doc_id:
        return None
    parts = doc_id.split("/")
    if ".." in parts or "" in parts:
        return None
    ext = Path(doc_id).suffix.lower()
    if ext not in kinds:
        return None
    if importance.is_importance_control_path(doc_id):
        return None
    if text_kind.is_sensitive(Path(doc_id).name, ext):
        _log.warning("read_original: 秘匿名のため対象外にしました（ext=%s）", ext)
        return None
    if not scope_mod.in_scope(doc_id, scope_paths):
        return None
    root = worlds.world_dir(world)
    if not root:
        return None
    root = Path(root)
    cand = root / doc_id
    try:
        rr = root.resolve()
        rp = cand.resolve()
        if not (rp == rr or rp.is_relative_to(rr)):
            return None
        if rp != rr / doc_id:            # 字面パスと不一致＝経路上のどこかに symlink があった
            return None
        if not rp.is_file():             # FIFO/ソケット等の非 regular も拒否
            return None
    except OSError:
        return None
    if not verify_doc_exists(doc_id, world, scope_paths):   # 実在・文書種別・scope の確定判定を共有
        return None
    if layer is not None:
        from . import corpus_docs
        verdict = corpus_docs.classify_document(
            doc_id, ext, lambda p=rp, size=4096: corpus_docs._read_head(p, size))
        is_code = verdict["kind"] == "code"
        if not layer_mod.in_layer_code(is_code, layer):
            return None
    try:
        st = rp.stat()
    except OSError:
        return None
    return root, doc_id, rp, st


def _open_verified_original(root: Path, doc_id: str, expected_st) -> tuple:
    """`_safe_original_path` が検査した `doc_id` を、`root`（world root・
    信頼済みアンカー）から `_open_file_nofollow_walk`（read_around/read_doc と共有・各階層を
    `O_DIRECTORY|O_NOFOLLOW` で1段ずつ辿る）で再度 open してから、検査直後に取った `expected_st`
    （`os.stat_result`）とデバイス/inode が一致することを確認する。

    検査（symlink 拒否・封じ込め・秘匿名等）と実際の `open()` の間には常に TOCTOU の隙間がある。
    検査済みの**最終パス要素だけ**を `os.O_NOFOLLOW` で単発 open するだけでは、その
    隙間で祖先ディレクトリ（`root/doc_id` の途中の階層）が KB 外への symlink に差し替えられたとき、
    `stat`（検査時）と `open`（単発 open）が同じ差し替え後の外部 inode を指したまま一致してしまい
    封じ込めを破られる——単発 `O_NOFOLLOW` は最終要素にしか効かない（POSIX 仕様）。`root` から
    `doc_id` の各要素を個別に `O_NOFOLLOW` で辿る本関数は、途中のどの段が symlink に差し替えられて
    いても `OSError` で検出する（祖先差し替えも拒否）。fstat 突合は仕上げの二重の安全弁として残す。

    戻り値 `(f, error)`。成功時 `f` は open 済みバイナリファイル——所有権は呼び出し先
    （`doc_readers` の各関数、モジュール docstring参照）へ引き継がれる。失敗時 `(None, {"error": ...})`。
    """
    rel_parts = Path(doc_id).parts
    if not rel_parts:
        return None, {"error": "読み取りに失敗しました", "error_code": "read_io_failed"}
    try:
        fd = _open_file_nofollow_walk(root, rel_parts)
    except OSError:
        return None, {"error": "読み取りに失敗しました", "error_code": "read_io_failed"}
    try:
        post = os.fstat(fd)
        if not stat.S_ISREG(post.st_mode):
            os.close(fd)
            return None, {"error": "読み取りに失敗しました", "error_code": "read_io_failed"}
        if (post.st_dev, post.st_ino) != (expected_st.st_dev, expected_st.st_ino):
            os.close(fd)
            return None, {"error": "読み取りに失敗しました", "error_code": "read_io_failed"}
        f = os.fdopen(fd, "rb")
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        return None, {"error": "読み取りに失敗しました", "error_code": "read_io_failed"}
    return f, None


def _close_quiet_local(f) -> None:
    """`_open_verified_original` が開いたが結局 `doc_readers` へ渡さずに終わる分岐
    （例: `xlsx_range` の `sheet` 引数欠落）で fd を漏らさず閉じる。"""
    try:
        f.close()
    except OSError:
        pass


def _open_doc_stream(world: str, doc_id: str, sp, layer) -> tuple:
    """`doc_id` を安全に解決し、読み取り用に open 済みのバイナリファイルオブジェクトを返す。

    `read_around`/`read_doc`/`doc_outline` が共有する土台——scope/層フィルタと symlink TOCTOU
    対策（`_safe_doc_path` の解決結果を信頼アンカーに、`lexical_rel` を `_open_file_nofollow_walk`
    で1段ずつ open）は3ツール共通（検証済みの安全弁を二重実装しない）。

    戻り値 `(f, error)`。成功時 `error=None`・`f` は呼び出し元が close する責務を持つ（ストリーミング
    走査の間じゅう開いたままにする必要があるため `with` に入れずそのまま返す）。失敗時
    `(None, {"error": ...})`。

    `f.read(cap)` で全文を一括ロードすると、cap の既定 64MiB を1回の呼び出しで丸ごと
    メモリに載せる懸念がある（grep 側と同種の懸念）。`grep_tool._CappedStreamReader`/`_logical_lines`
    （grep のストリーミング走査の実装をそのまま再利用・二重実装しない）で
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
        # 例外にならず結果化される読取I/O失敗——固定理由コード（`error_code`）を付け、
        # 呼び出し元（`run_tool` 境界）が `InvestigationState.backend_failures["read_io"]` へ反映する。
        return None, {"error": "読み取りに失敗しました", "error_code": "read_io_failed"}
    fd_owned = True
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None, {"error": "読み取りに失敗しました"}
        f = os.fdopen(fd, "rb")
        fd_owned = False   # 以後の close は呼び出し元（f.close()）が引き受ける
    except OSError:
        return None, {"error": "読み取りに失敗しました", "error_code": "read_io_failed"}
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
    （`text` は redaction 済みの子チャンク本文＝文字数では切らない＝chunk tier の最低保証そのもの）。
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
# この SYSTEM 文言・調べ方ブロックのチップ表記・trace ノードの表示は「grep」ではなく
# 「語句そのまま検索」で揃える。内部識別子＝ツール名 `ripgrep_search`・tools_pref の
# `grep` キーは不変（表記のみの統一）。
_SYS_INTRO_AND_LIST_DOCS = (
    "あなたは社内資料を調べて答えるアシスタントです。事前の索引はありません。"
    "ツールで資料を実際に検索して、資料を根拠に日本語で答えてください。"
    "**長さは絞らない＝集めた情報は削らない**。\n"
    "**ドキュメント数・一覧・どんな資料があるか・フォルダ構成といった台帳質問は、まず list_docs を使う**"
    "（語句そのまま検索は本文中の一致しか探せず件数/一覧には答えられない）。フォルダ名・ファイル名はパスに含まれるので、"
    "名前の部分一致は list_docs の name_pattern で当てる（語句そのまま検索で本文からは探さない）。"
    "表記が揺れそうな語（送り仮名・略し方など）は短い部分語で試す（例:「4期更改」がヒットしなければ「4期」）。\n"
    "**件数を答えるときは list_docs の path_prefix でフォルダを確定してから数え、どのフォルダを数えたかを"
    "回答に明示する**（曖昧なら『4期更改』と『4期保守』のように候補フォルダ別の内訳で答える）。\n"
    "**一覧を求められたら該当する全件を各項目のパス付きで列挙する（『など』で省略しない・件数と一致させる・"
    "truncated:true なら next_offset で続きを取る）。全件・一覧の完了は対象範囲の確認を終えてからで、"
    "検索3回や件数だけの取得では完了とせず、中断（利用者停止・通信エラー・予算到達）のときは"
    "確認済み／未確認／理由を分けて書き、部分結果を『全件』と断定しない。**\n"
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
# ファイル名/パスのパターンで探したいとき用（語句そのまま検索＝grep 軸に同居・grep OFF/不達では
# glob_search 自体を提示しない・§system_prompt/openai_tools/gemini_tools 参照）。
_SYS_GLOB_STEP = (
    "ファイル名・フォルダ名のパターンで探したいとき（例:「請求書系のExcelだけ」「JCLを一覧して」）は "
    "glob_search にワイルドカードパターン（例 `*請求書*.xlsx`・`**/障害対応/*.md`・`*.jcl`）を渡す"
    "（中身は読まない・該当パス一覧だけが返る）。"
)
_SYS_ES_FOLLOWUP = (
    "**ripgrep_search が0件/空振りのとき、または言い回しが揺れる概念・日本語の同義語で"
    "言い換えが必要なときは es_search（全文＋ベクトル）を試す**（語句そのまま検索は完全一致・固有名詞にしか強くない）。"
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
# grep が使えるときだけ言及する比較文（grep OFF/不達では「語句そのまま検索を打ち直すより」という
# 比較自体が意味を持たないため外す）。
_SYS_GRAPH_GREP_COMPARISON = (
    "**プログラム名/データ項目名などの名前が一つでも判明したら、その関連の広がりは語句そのまま検索を何度も打ち直すより"
    "先に graph_neighbors で辿るほうが早い**。"
)
_SYS_COMPARE_STEP = (
    "**世代（トップフォルダ）をまたいで「何が変わったか」を聞かれたとき**は compare_documents で"
    "対応する2文書のRAG正本を突き合わせ、返ってきたdiffを読んで業務語で説明する"
    "（対応文書が一意に決まらないときは candidates から利用者に確認してから比較する）。"
)
_SYS_OUTRO = (
    "**確定した事実と推定は分けて書き、推定には『推定』と明示する**"
    "（本文・グラフに無いことを補うときは推定として書く）。"
    "調査範囲・目的・選択肢が曖昧で、確認しないと結果が大きく変わる場合だけ ask_user でユーザに確認してください。"
    "特定の値を確かめる質問は根拠が揃ったらツールを呼ばず最終回答だけを返す。全件・一覧・すべての依頼は"
    "対象範囲の確認を終え、該当項目が回答にそろってから最終回答を返す（検索3回・根拠1件・代表例の発見では"
    "返さない）。中断（利用者停止・通信エラー・予算到達）のときは確認済みの結果・未確認の範囲・理由を"
    "分けて書き、部分結果を断定しない。"
    "出典（原本 DL）は Sherpa が付与するが、本文中でも根拠のパスを示してよい。"
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
    "doctype": {"type": "string",
               "description": "文書種別の完全一致で絞る（大文字小文字は無視。値は docs[].doctype の表示名と同じ・"
                              "例: 'Excel'・'cobol'・'設計書'。旧形式は 'Excel(旧)'／'Word(旧)'／'PowerPoint(旧)' の別値＝"
                              "'Excel' では .xls を含まない。値が不明なら doctype なしで1回呼んで確認する）。省略可"},
    "state": {"type": "string",
             "description": "状態の完全一致で絞る（'ready'=使える・'unreadable'=読み取れない・"
                            "'unknown'=直近確認が未実施）。省略可"},
    "limit": {"type": "integer", "description": "一覧に含める最大件数（既定200・上限500）。件数(count)は limit/offset と無関係に全件を返す"},
    "offset": {"type": "integer",
              "description": "一覧の開始位置（既定0）。truncated:true のときは next_offset をそのまま渡すと続きが取れる"}},
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
                "read_doc で続きを確認する。text_truncated が付くヒットは本文が途中で切れている——"
                "read_around（周辺）か read_doc（続き）で読む。"
                "truncated:true はヒット数が上限に達した＝母集団の一部しか見ていない（続きは取れない・範囲を絞るか別の語で探し、残りは未確認として扱う）。")
_DESC_READ = ("ヒット箇所の周辺行だけを精読する（全文は読まない）。doc_id と line を渡す。"
             "text_truncated が付いたら本文が上限で切れている——read_doc で続き（次の開始行）を読む。")
_DESC_READ_DOC = ("文書を開始行から連続して読む（通読向け・全文を一度には読まない）。"
                  "doc_id と start_line（省略時1）を渡す。1回の返却行数には上限があり、"
                  "「全◯行中 X〜Y行目」を返すので、続きが必要なら次の開始行（end_line+1）を"
                  "指定して再度呼び出す（range 外の start_line はエラーで明示）。"
                  "text_truncated が付くときは行内容が大きすぎて途中で切れている・"
                  "file_truncated が付くときは文書自体が大きすぎて total_lines が過小申告の可能性がある。")
_DESC_OUTLINE = ("文書の見出し構造（Markdown の #/##/### 見出し・派生MDの表/シート見出しを含む）を"
                 "行番号つきで返す。read_doc/read_around で読む箇所の当たりを付けるのに使う。"
                 "見出しが無い文書は総行数だけを返す。file_truncated が付くときは文書自体が"
                 "大きすぎて total_lines/見出し一覧が過小申告の可能性がある。"
                 "見出しが上限で切られたときは truncated:true と count（総数）が付く＝続きは取れないので、その範囲は未確認として扱う。")
_DESC_LIST_DOCS = ("文書台帳の一覧・件数を返す（本文は読まない・grep しない）。"
                   "「ドキュメント数」「どんな資料があるか」「フォルダ構成」等の台帳質問はこれで答える。"
                   "path_prefix でフォルダ配下に絞り、name_pattern でパス（フォルダ名/ファイル名）の部分一致に絞れる。"
                   "doctype（種別の完全一致・大文字小文字は無視）・state（'ready'/'unreadable'/'unknown' の"
                   "完全一致）でも絞れる。docs は常に rel_path の昇順で並ぶ（offset を進めても順序は変わらない）。"
                   "count は絞り込み後の全件数（limit/offset と無関係）、docs は offset から limit 件までの"
                   "一覧（rel_path/doctype/state）。truncated:true なら残りがある——次回呼び出しの offset に"
                   "next_offset をそのまま渡して続きを取る。")
# K6（`docs/proposals/2026-09-04-グラフのソース正典化.md` §3・§4b S1）: list_docs（ls 相当・フラット
# 一覧）に対する tree 相当。フォルダ名の意味解釈はしない（クエリ時にこのツールの呼び出し元＝LLM が
# 解釈する・K6・§5「フォルダ意味ノードの事前計算はしない」）。
_DESC_FOLDER_TREE = ("world のフォルダ階層を、深さ上限つき・フォルダごとの件数つきで俯瞰する"
                     "（本文は読まない・grep しない・list_docs のフラット一覧では階層の形が掴めない"
                     "大規模な範囲で使う）。path_prefix でフォルダ配下に絞り、depth（既定3）で列挙する深さを決める。"
                     "フォルダごとに直下ファイル数・配下（再帰）ファイル数・直下サブフォルダ数を返す。"
                     "深さ上限でまだ配下があるフォルダは truncated:true（depth を上げて掘り下げる）。"
                     "フォルダ件数自体が多すぎるときは folders_truncated:true（count が打ち切り前の総数）＝続きは取れないので、その範囲は未確認として扱う。")
_DESC_ES = ("社内資料を日本語の全文＋ベクトル検索（形態素・意味の近さ・関連度ランキング）。"
            "言い回しが揺れる概念・日本語の同義語・自然文クエリに強い。"
            "ripgrep_search が0件/空振りのときはまずこれを試す。doc_id と抜粋を関連度順で返す。"
            "text_truncated が付くヒットは本文が途中で切れている——read_around（周辺）か "
            "read_doc（続き）で読む。"
            "truncated:true はヒット数が上限に達した＝母集団の一部しか見ていない（続きは取れない・範囲を絞るか別の語で探し、残りは未確認として扱う）。")
_DESC_ASK = ("回答や検索条件を確定する前にユーザへ確認する。結果が大きく変わる曖昧さがある場合だけ使う。"
             "例: 影響分析で起点や影響先が複数候補に割れるとき、確実な波及が0件で要確認だけになったときは、"
             "対象の絞り込みを確認してよい。依頼に「確認してから進めて」とあるときは調査より先に確認する。"
             "ただし依頼に「確認ID:」が含まれる場合は前の質問への回答なので再質問しない。"
             "選択肢はラジオボタンまたはチェックボックスとして表示される。")
_DESC_GRAPH = ("関係グラフから、ある名前（プログラム/コピーブック/ジョブ/データ項目/テーブルなど）の**関連部品**をたどる"
               "（コピー・呼び出し・参照・関連文書（言及）などの近傍を、つながりの経路つきで返す）。"
               "各近傍の経路は**辺ごとの種類と向き（from→to）**付き。COPIES／INVOKES／ACCESSES／CONTAINS だけの"
               "経路は構造的な依存＝根拠にしてよい（影響は矢印をさかのぼる: A →COPIES→ B は B を変えると A が"
               "影響を受ける）。DOCUMENTS（言及）・CORRESPONDS_TO（同名の対応）を含む経路や `unverified` の辺（裏付け原本が"
               "実在確認できない）を含む経路は候補＝原本で確認する。"
               "名前が一つでも判明したら、その関連の広がりは grep を反復するより先にこれで辿るほうが早い。"
               "原因の手がかり集め（トラブルシュート）に有効。grep で正確な名前を見つけてから渡すと精度が上がる。"
               "近傍が上限で切られたときは truncated:true と count（総数）が付く＝続きは取れないので、その範囲は未確認として扱う。")
# grep OFF/不達で es_search/graph_neighbors だけが提示されるときの代替 description（SC-6e）。
# `_DESC_ES`/`_DESC_GRAPH` はいずれも grep（ripgrep_search）への言及を含むため、提示していない
# ツールへの言及・推奨をそのまま残さない（無駄なターン/上限到達を防ぐ）。
_DESC_ES_NO_GREP = ("社内資料を日本語の全文＋ベクトル検索（形態素・意味の近さ・関連度ランキング）。"
                    "言い回しが揺れる概念・日本語の同義語・自然文クエリに強い。doc_id と抜粋を関連度順で返す。"
                    "text_truncated が付くヒットは本文が途中で切れている——read_around（周辺）か read_doc（続き）で読む。"
                    "truncated:true はヒット数が上限に達した＝母集団の一部しか見ていない（続きは取れない・範囲を絞るか別の語で探し、残りは未確認として扱う）。")
_DESC_GRAPH_NO_GREP = ("関係グラフから、ある名前（プログラム/コピーブック/ジョブ/データ項目/テーブルなど）の**関連部品**をたどる"
                       "（コピー・呼び出し・参照・関連文書（言及）などの近傍を、つながりの経路つきで返す）。"
                       "各近傍の経路は**辺ごとの種類と向き（from→to）**付き。COPIES／INVOKES／ACCESSES／CONTAINS"
                       "だけの経路は構造的な依存＝根拠にしてよい（影響は矢印をさかのぼる: A →COPIES→ B は B を"
                       "変えると A が影響を受ける）。DOCUMENTS（言及）・CORRESPONDS_TO を含む経路や `unverified` の辺を含む経路は候補＝原本で"
                       "確認する。"
                       "原因の手がかり集め（トラブルシュート）に有効。"
                       "近傍が上限で切られたときは truncated:true と count（総数）が付く＝続きは取れないので、その範囲は未確認として扱う。")
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
             "大文字小文字は区別しない。該当パス一覧と総件数を返す（上限200件・超過分は打ち切り＝truncated:true。"
             "続きは取れないので、その範囲は未確認として扱い count との差を全件と断定しない）。"
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
                 "片方以上が rag.md を持たない文書（コード原文等）のときは status: unsupported を返す。"
                 "diff が上限で切られたときは truncated:true が付く＝続きは取れないので、その範囲は未確認として扱う。")
_PARAMS_COMPARE = {"type": "object", "properties": {
    "left_doc_id": {"type": "string", "description": "比較する片方の doc_id（省略時は right_doc_id も無視される）"},
    "right_doc_id": {"type": "string", "description": "比較するもう片方の doc_id（left_doc_id とセットで指定）"},
    "source_doc_id": {"type": "string", "description": "対応文書を自動発見する起点の doc_id（left_doc_id/right_doc_id 省略時）"},
    "target_generation": {"type": "string",
                          "description": "比べたい世代（トップフォルダ名・例 '5期'）。source_doc_id とセットで指定"}},
    "required": []}

# 本文を実際に読んだツール＝出典の「根拠（精読済み）」区分（`verified_docs`）に載せる（シート一覧だけの
# `xlsx_sheets`・見出しだけの `doc_outline` は含めない）。
_VERIFIED_READ_TOOLS = frozenset({"read_around", "read_doc", "xlsx_range", "docx_paragraphs",
                                  "pptx_slides", "pdf_pages", "file_head"})


# ---- 原本読取ツール（S3b・`docs/proposals/2026-09-10-Codex原本直読と調査スキル.md` §2-9）----
# Codex（MCP 経由）と API 経路の頭脳（このモジュールの function-calling）が**同じ関数**
# （`doc_readers.py`）で原本の中身を読む。毎回 Python を書かせない＝トークンと実行時間を削り、
# 再現性を上げる（突合・集計など定型外の作業だけ Python に任せる）。
_DESC_XLSX_SHEETS = ("Excel（.xlsx）原本のシート一覧と大きさを返す（原本を直接読む・派生ではない）。"
                    "doc_id はシート名・行数・列数を先に確認してから xlsx_range で読む範囲を絞るのに使う。"
                    "大きすぎて時間内に数えられないシートは dims_estimated=true で、大きさは記録値（推定）か不明。"
                    "シート一覧が上限で切られたときは truncated:true＝続きは取れないので、その範囲（残りのシート）は未確認として扱う。")
_PARAMS_XLSX_SHEETS = {"type": "object", "properties": {
    "doc_id": {"type": "string", "description": "資料フォルダからの相対パス（拡張子 .xlsx）"}},
    "required": ["doc_id"]}
_DESC_XLSX_RANGE = ("Excel（.xlsx）原本のセル範囲を表で返す（原本を直接読む・派生ではない）。"
                    "range 省略時は先頭から max_rows×max_cols（既定200行×50列）。"
                    "範囲が上限を超えたら切り詰めて truncated:true（range は実際に返した範囲）。"
                    "引用するときはシート名とセル範囲（例 'Sheet1!B3:D10'）で示す。")
_PARAMS_XLSX_RANGE = {"type": "object", "properties": {
    "doc_id": {"type": "string", "description": "資料フォルダからの相対パス（拡張子 .xlsx）"},
    "sheet": {"type": "string", "description": "シート名（xlsx_sheets が返す name）"},
    "range": {"type": "string", "description": "セル範囲（A1形式・例 'B3:D10'）。省略可＝先頭から既定サイズ"},
    "max_rows": {"type": "integer", "description": "返す最大行数（既定200）"},
    "max_cols": {"type": "integer", "description": "返す最大列数（既定50）"}},
    "required": ["doc_id", "sheet"]}
_DESC_DOCX_PARAGRAPHS = ("Word（.docx）原本の段落と表を返す（原本を直接読む・派生ではない）。"
                        "start（既定0）・count（既定200）で段落をページングする。表は先頭20表・各50行まで。"
                        "表が大きく結果が予算を超える場合は表の行も削られる（row_truncated:true）。"
                        "引用するときは段落番号（i）・見出し（style）、表なら表番号・行で示す。")
_PARAMS_DOCX_PARAGRAPHS = {"type": "object", "properties": {
    "doc_id": {"type": "string", "description": "資料フォルダからの相対パス（拡張子 .docx）"},
    "start": {"type": "integer", "description": "読み始める段落インデックス（既定0）"},
    "count": {"type": "integer", "description": "読む段落数（既定200）"},
    "table_start": {"type": "integer", "description": "表の開始インデックス（既定0・1回20表）。tables が total_tables に足りなければ進めて呼び直す"},
    "table_row_start": {"type": "integer", "description": "各表の開始行（既定0・1回50行）。rows が total_rows に足りなければ進めて呼び直す"}},
    "required": ["doc_id"]}
_DESC_PPTX_SLIDES = ("PowerPoint（.pptx）原本のスライドのテキスト・表・ノートを返す"
                    "（原本を直接読む・派生ではない）。pages（例 '3'・'2-5'・'1,3,5'・既定 '1-10'）で"
                    "スライドを指定する（1回20枚まで）。引用するときはスライド番号（no）で示す。")
_PARAMS_PPTX_SLIDES = {"type": "object", "properties": {
    "doc_id": {"type": "string", "description": "資料フォルダからの相対パス（拡張子 .pptx）"},
    "pages": {"type": "string", "description": "スライド指定（例 '3'・'2-5'・'1,3,5'）。省略時 '1-10'"}},
    "required": ["doc_id"]}
_DESC_PDF_PAGES = ("PDF 原本のページのテキストを返す（原本を直接読む・派生ではない）。"
                  "pages（例 '3'・'2-5'・'1,3,5'・既定 '1-5'）でページを指定する（1回10ページまで）。"
                  "1ページの文字量だけで結果が予算を超える場合でもページ自体は残し、本文を"
                  "切り詰めて text_truncated:true にする（ページ番号は保つ）。"
                  "引用するときはページ番号（no）で示す。")
_PARAMS_PDF_PAGES = {"type": "object", "properties": {
    "doc_id": {"type": "string", "description": "資料フォルダからの相対パス（拡張子 .pdf）"},
    "pages": {"type": "string", "description": "ページ指定（例 '3'・'2-5'・'1,3,5'）。省略時 '1-5'"}},
    "required": ["doc_id"]}
_DESC_FILE_HEAD = ("テキスト・コード原本の先頭バイトをそのまま返す（原本を直接読む・派生ではない・"
                  "Office/PDF は対象外＝xlsx_sheets/docx_paragraphs/pptx_slides/pdf_pages を使う）。"
                  "max_bytes（既定65536）まで読み、上限で切れていたら truncated:true。")
_PARAMS_FILE_HEAD = {"type": "object", "properties": {
    "doc_id": {"type": "string", "description": "資料フォルダからの相対パス（テキスト・コード）"},
    "max_bytes": {"type": "integer", "description": "読む最大バイト数（既定65536）"}},
    "required": ["doc_id"]}

# ---- 作成系（author）の成果物ファイル: DEPTH-2 S2（§2.7）----
# 個人 workspace（本人のみ・grep 対象・RAG には索引化しない）へ保存する——共有 KB とは無関係。
# Codex（`providers/codex/provider.py` の run_dir 差分検出）と同じ台帳（`personal_workspace_files`）
# 台帳へ登録し、同じ成果物カード（`env["created_files"]`）で見せる。
_DESC_WRITE_OUTPUT_FILE = (
    "作成した文書を個人の作業スペースに保存し、ダウンロードできるようにする"
    "（作成系の依頼でファイルを納品するときに使う。調査・質問の回答には使わない）。"
    "filename（拡張子つきの単純なファイル名・フォルダ区切り不可）と content（保存する内容の全文）を渡す。"
    "同名ファイルが既にあれば自動的に別名で保存する（上書きしない）。"
    "Markdown を marp のスライド形式（先頭に `---\\nmarp: true\\n---` のfront-matter）で書いたときは"
    "marp:true も渡すと PDF/PowerPoint も自動生成される（それ以外の拡張子・marp 形式でない Markdown では無視）。"
    "保存できたら rel_path と download_url を返す——最終回答の最後に作成したファイル名と内容の要約を書くこと。"
    "保存できなかったときは error に理由が入る（内容は保存されていない）。")
_PARAMS_WRITE_OUTPUT_FILE = {"type": "object", "properties": {
    "filename": {"type": "string",
                "description": "保存するファイル名（拡張子つき・フォルダ区切り不可・例 '消費税率一覧.md'）"},
    "content": {"type": "string", "description": "保存する内容（テキスト全文）"},
    "marp": {"type": "boolean",
            "description": "true のとき、この Markdown を marp スライドとして PDF/PowerPoint も生成する（既定false）"}},
    "required": ["filename", "content"]}

# 保存できる内容の上限（新規ツールの安全弁・DoS 対策。既存の清書予算/回数上限の流用対象ではない
# ＝§2.7 の「回数上限は既存の清書予算の値を流用」は追記継続の話で、本upperは別の懸念）。
_WRITE_OUTPUT_FILE_MAX_BYTES = _env_int("SHERPA_WRITE_OUTPUT_FILE_MAX_BYTES", 2_000_000, 1024, 20_000_000)
_SAFE_OUTPUT_FILENAME_RE = re.compile(r"^[^/\\\x00]{1,200}$")


def _open_workspace_dir_fd(parent_fd: int, name: str) -> int:
    """`parent_fd` 配下の `name` ディレクトリを、無ければ作成したうえで symlink を追わずに開いて
    dir_fd を返す（C42 是正）。`name` が symlink（この呼び出しの直前に差し替えられた場合を含む）
    なら `O_NOFOLLOW` で ELOOP となり、呼び出し元へ OSError が伝播する——一度開いた fd は
    以後その名前がどう差し替えられても最初に開いた実ディレクトリを指し続けるため、後続の
    階層をこの fd 基準で開けば途中の親の差替え（TOCTOU）に影響されない。
    """
    try:
        os.mkdir(name, dir_fd=parent_fd)
    except FileExistsError:
        pass
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)


def _run_write_output_file(args: dict, uid: str | None) -> dict:
    """`write_output_file` ツール本体（DEPTH-2 S2・§2.7）。

    個人 workspace（`{SHERPA_USERS_DIR}/{uid}/workspace/files/`）へ保存し、Codex の created files
    （`providers/codex/provider.py`）と**同じ台帳**（`store.record_workspace_file`・
    `personal_workspace_files`）・**同じ TTL**（`SHERPA_WORKSPACE_TTL_DAYS`）へ登録する。
    `marp: true` の Markdown は同じ `marp_render.render_outputs` 経路で pdf/pptx 化する。

    fail-open/fail-closed の使い分け（Codex 経路と同じ区別を踏襲）: **台帳登録**（`record_workspace_file`）
    の失敗は明示エラーを返し内容を保存しない（fail-closed 寄り）。**marp 変換**の失敗は注記だけで
    継続する（.md 自体の保存は成功のまま・fail-open）。呼び出し元（`run_tool`）はどちらの場合も
    例外を受け取らない契約——想定される失敗は必ず `{"error": ...}` を持つ dict で返し、調査ループ
    全体（`openai_style`）を落とさない。
    """
    if not uid:
        return {"error": "作成者が特定できないため保存できません（個人領域が未初期化です）"}
    filename = str(args.get("filename") or "").strip()
    if (not filename or not _SAFE_OUTPUT_FILENAME_RE.match(filename)
            or filename in (".", "..") or "/" in filename or "\\" in filename):
        return {"error": "filename が不正です（フォルダ区切りを含まない単純なファイル名を指定してください）"}
    content = args.get("content")
    if not isinstance(content, str):
        return {"error": "content は文字列で渡してください"}
    data = content.encode("utf-8")
    if not data:
        return {"error": "content が空です"}
    if len(data) > _WRITE_OUTPUT_FILE_MAX_BYTES:
        return {"error": f"内容が大きすぎます（上限 {_WRITE_OUTPUT_FILE_MAX_BYTES} バイト）"}
    marp = bool(args.get("marp"))

    import hashlib
    from datetime import datetime, timedelta, timezone

    from . import store
    users_dir = Path(os.environ.get("SHERPA_USERS_DIR", "data/users")).resolve()
    ws_files = users_dir / uid / "workspace" / "files"   # 表示・record_workspace_file 用（書込み自体は dir_fd 経由）

    # C42（DEPTH-2 S2 是正）: C38 の「各階層が symlink でないか確認してから resolve() で照合する」
    # 方式は、検査（stat/resolve）と実際の作成（open/write）の間に window があり、その間に
    # 親（`uid_dir`・`ws_dir`）が他人の workspace への symlink へ差し替えられても検出できない
    # （TOCTOU）。dir_fd による段階的オープンなら、各階層を「一度開いたら、その fd は以後どんな
    # 名前差替えが起きても最初に開いた実ディレクトリを指し続ける」性質を使い、以降の名前解決を
    # 一切パス文字列に頼らずに済ませられる——`uid`・`workspace`・`files` の各コンポーネントを
    # 直前の fd を親として `O_NOFOLLOW` で個別に開く（symlink ならその場で ELOOP になり、
    # 以降の階層は一切開かれない）。
    try:
        users_dir.mkdir(parents=True, exist_ok=True)   # 信頼済みの設定パス（`SHERPA_USERS_DIR`）自体は既存想定の外
        fd_users = os.open(str(users_dir), os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return {"error": "保存先を準備できませんでした"}
    fd_uid = fd_ws = fd_files = -1
    try:
        fd_uid = _open_workspace_dir_fd(fd_users, uid)
        fd_ws = _open_workspace_dir_fd(fd_uid, "workspace")
        fd_files = _open_workspace_dir_fd(fd_ws, "files")
    except OSError:
        return {"error": "保存先が利用できません（管理者に確認してください）"}
    finally:
        os.close(fd_users)
        if fd_uid >= 0:
            os.close(fd_uid)
        if fd_ws >= 0:
            os.close(fd_ws)

    try:
        ttl_days = _env_int("SHERPA_WORKSPACE_TTL_DAYS", 90, 0, 3650)
        expires = (datetime.now(timezone.utc) + timedelta(days=ttl_days)) if ttl_days > 0 else None
        stem, suffix = Path(filename).stem or "output", Path(filename).suffix

        def _write_and_register(name: str, raw: bytes) -> dict | None:
            # `dir_fd=fd_files` の `O_EXCL|O_NOFOLLOW` で排他的に新規作成する——`name` という名前が
            # 既に何か（通常ファイル・symlink のどちらでも）を指していれば、内容に関わらず作成自体が
            # 失敗する（TOCTOU を作らない・「存在確認してから書く」の2手順に分けない）。`fd_files`
            # は既に開いた実ディレクトリを指すため、`files` という名前がこの後どう差し替えられても
            # 影響しない。
            try:
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644,
                             dir_fd=fd_files)
            except OSError:
                return None
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(raw)
            except OSError:
                try:
                    os.unlink(name, dir_fd=fd_files)
                except OSError:
                    pass
                return None
            try:
                sha = hashlib.sha256(raw).hexdigest()
                return store.record_workspace_file(uid, name, str(ws_files / name), len(raw), sha,
                                                    expires_at=expires)
            except Exception:
                try:
                    os.unlink(name, dir_fd=fd_files)   # 登録に失敗＝台帳の無い孤児を残さない（Codex 経路と同じ規律）
                except OSError:
                    pass
                return None

        def _name_occupied(name: str) -> bool:
            # 別名候補選びの事前判定（最終防衛線は上の O_EXCL 作成）。symlink・通常ファイルの
            # どちらでも「使用中」として次の連番へ回す（無言で上書き先を奪わない）。
            try:
                os.stat(name, dir_fd=fd_files, follow_symlinks=False)
            except FileNotFoundError:
                return False
            except OSError:
                return True
            return True

        row = None
        i = 0
        while i <= 10000:                            # 無限ループ防止（Codex 経路の同名回避と同じ上限）
            rel = filename if i == 0 else f"{stem}_{i}{suffix}"
            with store.workspace_file_lock(uid, rel):
                if _name_occupied(rel) or not store.no_live_upload_for_path(uid, rel):
                    i += 1
                    continue
                row = _write_and_register(rel, data)
            break
    finally:
        os.close(fd_files)
    if row is None:
        return {"error": "保存中に登録へ失敗しました（内容は保存されていません）"}

    result = {"rel_path": row["rel_path"], "download_url": f"/workspace/files/{row['id']}/download",
              "bytes": len(data)}

    if marp and Path(row["rel_path"]).suffix.lower() == ".md":
        dst = ws_files / row["rel_path"]
        try:
            from . import marp_render
            from .providers.codex.sandbox import _detect_chrome_path, _marp_bin
            if not marp_render.is_marp_markdown(dst):
                result["marp_note"] = ("marp:true が指定されましたが marp 形式"
                                       "（front-matter の marp: true）ではないため変換しませんでした")
            else:
                rendered = marp_render.render_outputs(
                    [dst], marp_bin=_marp_bin(), chrome_path=_detect_chrome_path(),
                    theme_dirs=[Path(__file__).resolve().parent / "skills_base" / "marp" / "themes"],
                    containment_root=ws_files)
                extra = []
                for out in rendered:
                    try:
                        raw = out.read_bytes()
                    except OSError:
                        continue
                    try:
                        sha = hashlib.sha256(raw).hexdigest()
                        with store.workspace_file_lock(uid, out.name):
                            r_row = store.record_workspace_file(
                                uid, out.name, str(out), len(raw), sha, expires_at=expires)
                    except Exception:
                        continue   # marp 変換自体は成功済み＝この1形式の台帳登録失敗だけ諦める（fail-open）
                    extra.append({"rel_path": r_row["rel_path"],
                                 "download_url": f"/workspace/files/{r_row['id']}/download"})
                if extra:
                    result["rendered"] = extra
                # C28 是正: `render_outputs` は形式ごとに fail-open で個別スキップする（例外を出さない・
                # 未導入/Chromium 不在/ネットワーク隔離不可のいずれも空/部分リストで返る）ため、
                # 利用者に案内した pdf/pptx（description 参照・html は内部形式で案内していない）が
                # 実際には1つも登録されなかった／一部だけ欠けた場合に、無言で完了扱いにせず注記する
                # （生成できても台帳登録に失敗した形式は download_url が無い＝利用者には「無い」のと
                # 同じなので `extra`＝登録済みの側で確認する）。
                _registered_suffixes = {Path(e["rel_path"]).suffix.lstrip(".").lower() for e in extra}
                _missing_formats = [fmt for fmt in ("pdf", "pptx") if fmt not in _registered_suffixes]
                if _missing_formats:
                    result["marp_note"] = (
                        f"{'/'.join(_missing_formats)} の生成に失敗しました"
                        "（Markdown 自体は保存されています）")
        except Exception as e:
            _log.warning("write_output_file: marp レンダ処理が例外で終了（fail-open）: %s", e)
            result["marp_note"] = "スライド変換（PDF/PowerPoint）に失敗しました。Markdown 自体は保存されています"
    return result


def _desc_es(with_grep: bool) -> str:
    """`with_grep` に応じた es_search の description（全ON相当時は正準文字列 `_DESC_ES` と byte 一致）。"""
    return _DESC_ES if with_grep else _DESC_ES_NO_GREP


def _desc_graph(with_grep: bool) -> str:
    """`with_grep` に応じた graph_neighbors の description（同上・`_DESC_GRAPH` と byte 一致）。"""
    return _DESC_GRAPH if with_grep else _DESC_GRAPH_NO_GREP


def openai_tools(with_es: bool = False, with_graph: bool = False, can_ask: bool = True,
                 with_grep: bool = True, with_write: bool = False) -> list:
    # can_ask=False（回答の再送＝依頼に「確認ID:」を含む実行）では ask_user
    #   ツール自体を渡さない＝再質問ループを構造的に塞ぐ（S2 の SHERPA_MCP_ASK_DISABLED と同思想）。
    # SC-6e: `with_grep`（既定 True）は検索経路トグルの grep 軸。list_docs/doc_outline/read_doc/
    #   read_around は土台系のため対象外＝常に含める。glob_search（ファイル名/パスのグロブ検索）も
    #   grep 軸に同居する。
    # DEPTH-2 S2（§2.7）: `with_write`（既定 False＝オプトイン）は `write_output_file` の掲出可否——
    #   呼び出し元（`openai_style` 本体）だけが `uid` の有無で明示的に True を渡す（uid が無ければ
    #   登録先を特定できないため未掲出）。既定 False にしているのは、`gemini()`/`anthropic_style()`
    #   （Gemini/Bedrock・DEPTH-2 S2 の対象外）や `_sub_loop`（検索アシスタント・書込み非対応の
    #   プロファイル）等の**他の呼び出し元を無改修のまま**にするため——既定 True にすると、uid を
    #   持たないこれらの経路にもツールが掲出され、呼んでも常に失敗する（登録先不明）だけの
    #   無駄なターンを誘発する。
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
    # S3b: 原本読取ツールも ES/graph/grep トグルと無関係の土台系＝常に含める（layer=="code" のターン
    # では Office/PDF 5本は run_tool 側が error を返す・file_head は層に応じて対象を絞る）。
    t.append({"type": "function", "function": {"name": "xlsx_sheets", "description": _DESC_XLSX_SHEETS, "parameters": _PARAMS_XLSX_SHEETS}})
    t.append({"type": "function", "function": {"name": "xlsx_range", "description": _DESC_XLSX_RANGE, "parameters": _PARAMS_XLSX_RANGE}})
    t.append({"type": "function", "function": {"name": "docx_paragraphs", "description": _DESC_DOCX_PARAGRAPHS, "parameters": _PARAMS_DOCX_PARAGRAPHS}})
    t.append({"type": "function", "function": {"name": "pptx_slides", "description": _DESC_PPTX_SLIDES, "parameters": _PARAMS_PPTX_SLIDES}})
    t.append({"type": "function", "function": {"name": "pdf_pages", "description": _DESC_PDF_PAGES, "parameters": _PARAMS_PDF_PAGES}})
    t.append({"type": "function", "function": {"name": "file_head", "description": _DESC_FILE_HEAD, "parameters": _PARAMS_FILE_HEAD}})
    if with_write:
        t.append({"type": "function", "function": {"name": "write_output_file",
                                                   "description": _DESC_WRITE_OUTPUT_FILE,
                                                   "parameters": _PARAMS_WRITE_OUTPUT_FILE}})
    if can_ask:
        t.append({"type": "function", "function": {"name": "ask_user", "description": _DESC_ASK, "parameters": _PARAMS_ASK}})
    return t


def gemini_tools(with_es: bool = False, with_graph: bool = False, can_ask: bool = True,
                 with_grep: bool = True, with_write: bool = False) -> list:
    # can_ask=False（確認ID 付き再送）では ask_user を渡さない（openai_tools と同じ）。
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
    # S3b: 原本読取ツールも土台系＝常に含める（openai_tools と同じ理由）。
    fns.append({"name": "xlsx_sheets", "description": _DESC_XLSX_SHEETS, "parameters": _PARAMS_XLSX_SHEETS})
    fns.append({"name": "xlsx_range", "description": _DESC_XLSX_RANGE, "parameters": _PARAMS_XLSX_RANGE})
    fns.append({"name": "docx_paragraphs", "description": _DESC_DOCX_PARAGRAPHS, "parameters": _PARAMS_DOCX_PARAGRAPHS})
    fns.append({"name": "pptx_slides", "description": _DESC_PPTX_SLIDES, "parameters": _PARAMS_PPTX_SLIDES})
    fns.append({"name": "pdf_pages", "description": _DESC_PDF_PAGES, "parameters": _PARAMS_PDF_PAGES})
    fns.append({"name": "file_head", "description": _DESC_FILE_HEAD, "parameters": _PARAMS_FILE_HEAD})
    if with_write:
        fns.append({"name": "write_output_file", "description": _DESC_WRITE_OUTPUT_FILE,
                    "parameters": _PARAMS_WRITE_OUTPUT_FILE})
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


# ---- 利用統計チャット用ツール定義 ----
# 裁定（2026-09-12）: ツールは以下の8つのみ（world 別/モデル別は足さない）・期間上限365日・
# 返却上限50件・会話 id は返してよい。自由 SQL は与えない（閉じた引数のみ）。
_USAGE_TOOLS_SPEC = [
    ("usage_overview", "直近の利用統計の概要（用途別・ユーザー別・トークン量・回答時間・終了理由等）を"
                       "days日分まとめて取得する。画面に出ている集計と同じ材料。",
     {"type": "object", "properties": {
         "days": {"type": "integer", "description": "集計期間（日数・1〜365・省略時30）"},
         "from": {"type": "string",
                  "description": "期間の開始日時（ISO 8601・タイムゾーンオフセット必須・"
                                 "例 2026-09-18T13:00:00+09:00）。to と対で指定し days とは併用不可"},
         "to": {"type": "string",
                "description": "期間の終了日時（この日時は含まない・ISO 8601・オフセット必須）。"
                               "from と対で指定し days とは併用不可"}}}),
    ("usage_by_user", "ユーザー別×用途別（kind）の呼び出し回数・トークン量・所要時間を取得する。"
                      "uid/kind で絞り込める。",
     {"type": "object", "properties": {
         "days": {"type": "integer", "description": "集計期間（日数・1〜365・省略時7）"},
         "uid": {"type": "string", "description": "絞り込む利用者 id（省略可）"},
         "kind": {"type": "string", "description": "絞り込む用途（kind・省略可・例: chat/intent/embed）"},
         "from": {"type": "string",
                  "description": "期間の開始日時（ISO 8601・タイムゾーンオフセット必須・"
                                 "例 2026-09-18T13:00:00+09:00）。to と対で指定し days とは併用不可"},
         "to": {"type": "string",
                "description": "期間の終了日時（この日時は含まない・ISO 8601・オフセット必須）。"
                               "from と対で指定し days とは併用不可"}}}),
    ("usage_conversations", "会話別の利用量上位表を取得する（会話 id・uid・world・user ターン数・"
                           "用途別内訳・回答時間平均）。本文・タイトルは含まない。",
     {"type": "object", "properties": {
         "days": {"type": "integer", "description": "集計期間（日数・1〜365・省略時30）"},
         "uid": {"type": "string", "description": "絞り込む利用者 id（省略可）"},
         "limit": {"type": "integer", "description": "返す件数上限（1〜50・省略時20）"},
         "sort": {"type": "string", "enum": ["tokens", "turns", "elapsed"],
                  "description": "並び順（省略時 tokens）"},
         "from": {"type": "string",
                  "description": "期間の開始日時（ISO 8601・タイムゾーンオフセット必須・"
                                 "例 2026-09-18T13:00:00+09:00）。to と対で指定し days とは併用不可"},
         "to": {"type": "string",
                "description": "期間の終了日時（この日時は含まない・ISO 8601・オフセット必須）。"
                               "from と対で指定し days とは併用不可"}}}),
    ("usage_conversation_detail", "指定した1会話の内訳（user ターン数・用途別 calls/tokens・"
                                  "回答時間の系列）を取得する。本文・タイトルは含まない。",
     {"type": "object", "properties": {
         "conversation_id": {"type": "integer", "description": "会話 id"}},
      "required": ["conversation_id"]}),
    ("usage_response_time", "回答時間（受付〜最終回答の壁時計）の分布（avg/median/p90/max・件数）を"
                           "取得する。provider で絞り込める。",
     {"type": "object", "properties": {
         "days": {"type": "integer", "description": "集計期間（日数・1〜365・省略時30）"},
         "provider": {"type": "string", "description": "絞り込む経路（省略可・例: openai/ollama/codex）"}}}),
    ("usage_daily", "日別の系列（turns=ターン数・tokens=入出力トークン・response_time=回答時間平均）を"
                   "取得する。",
     {"type": "object", "properties": {
         "days": {"type": "integer", "description": "集計期間（日数・1〜365・省略時30）"},
         "metric": {"type": "string", "enum": ["turns", "tokens", "response_time"],
                    "description": "取得する系列（省略時 turns）"}}}),
    ("usage_stop_kinds", "終了理由（正常完了・停止・予算超過等）の分布と利用者による明示停止の件数を"
                        "取得する。uid で絞り込める。",
     {"type": "object", "properties": {
         "days": {"type": "integer", "description": "集計期間（日数・1〜365・省略時30）"},
         "uid": {"type": "string", "description": "絞り込む利用者 id（省略可）"},
         "from": {"type": "string",
                  "description": "期間の開始日時（ISO 8601・タイムゾーンオフセット必須・"
                                 "例 2026-09-18T13:00:00+09:00）。to と対で指定し days とは併用不可"},
         "to": {"type": "string",
                "description": "期間の終了日時（この日時は含まない・ISO 8601・オフセット必須）。"
                               "from と対で指定し days とは併用不可"}}}),
    ("usage_depth_rounds", "深さ（標準/深く/最大）×経路（provider）別の査読巡数の分布と、"
                          "不明（未確定）と判定された主張の理由コードの分布を取得する。"
                          "本文（質問/回答）は含まない。",
     {"type": "object", "properties": {
         "days": {"type": "integer", "description": "集計期間（日数・1〜365・省略時30）"},
         "from": {"type": "string",
                  "description": "期間の開始日時（ISO 8601・タイムゾーンオフセット必須・"
                                 "例 2026-09-18T13:00:00+09:00）。to と対で指定し days とは併用不可"},
         "to": {"type": "string",
                "description": "期間の終了日時（この日時は含まない・ISO 8601・オフセット必須）。"
                               "from と対で指定し days とは併用不可"}}}),
]


def usage_openai_tools() -> list:
    """利用統計チャット専用: 上記8ツールだけを OpenAI/Ollama 形式で LLM に渡す。"""
    return [{"type": "function", "function": {"name": n, "description": d, "parameters": p}}
           for n, d, p in _USAGE_TOOLS_SPEC]


def usage_gemini_tools() -> list:
    """利用統計チャット専用: 上記7ツールだけを Gemini 形式で LLM に渡す。"""
    return [{"functionDeclarations": [
        {"name": n, "description": d, "parameters": p} for n, d, p in _USAGE_TOOLS_SPEC
    ]}]


# ---- S3b 原本読取ツール（6本）共通の後処理: バイト上限クリップ・read_evidence 用の text 合成 ----
# `doc_readers.py` は world/scope/doc_id を一切知らない純関数（モジュール docstring 参照）ため、
# `read_evidence`（`InvestigationState`）に載せるための `doc_id`/`text`/`locator` はここ
# （`run_tool` の 6 分岐だけが doc_id を知っている）で合成する。

# ツール名→(切り詰め対象フィールド, なければ None＝クリップ不要) の対応。
_READER_CLIP_FIELD = {
    "xlsx_sheets": "sheets", "xlsx_range": "rows", "docx_paragraphs": "paragraphs",
    "pptx_slides": "slides", "pdf_pages": "pages", "file_head": "text",
}


def _row_start_from_a1_range(range_a1: str) -> int | None:
    """`"B3:D10"` 等の A1 range 文字列から開始行番号（3）を取り出す（xlsx_range の行番号復元用）。
    解析できなければ None（呼び出し元は 0 起点の連番へフォールバックする）。"""
    m = re.match(r"^[A-Za-z]+(\d+)", (range_a1 or "").split(":")[0])
    return int(m.group(1)) if m else None


def _doc_reader_text_locator(name: str, result: dict) -> tuple[str | None, str | None]:
    """原本読取ツール（6本）の結果から `read_evidence`/根拠ゲートに載せる
    `text`（rows/paragraphs/slides/pages を1本の本文に連結・位置情報を行頭に付ける）と
    `locator` を組む。エラー/中身なしは `(None, None)`（呼び出し元は doc_id/text/locator を
    足さない＝空の read evidence を作らない）。
    """
    if not isinstance(result, dict) or result.get("error"):
        return None, None
    if name == "xlsx_sheets":
        sheets = result.get("sheets") or []
        if not sheets:
            return None, None
        def _dims(s):
            if s.get("dims_estimated"):
                if s.get("max_row") is None or s.get("max_col") is None:
                    return "大きさ不明（時間内に数えられず）"
                return f"約{s.get('max_row')}行×{s.get('max_col')}列（記録値・推定）"
            return f"{s.get('max_row', 0)}行×{s.get('max_col', 0)}列"
        text = "\n".join(f"{s.get('name')}: {_dims(s)}"
                         for s in sheets if isinstance(s, dict))
        return (text or None), "sheets"
    if name == "xlsx_range":
        rows = result.get("rows") or []
        if not rows:
            return None, None
        sheet = result.get("sheet") or ""
        rng = result.get("range") or ""
        locator = f"{sheet}!{rng}" if sheet else (rng or "range")
        start_row = _row_start_from_a1_range(rng)
        lines = []
        for i, row in enumerate(rows):
            no = start_row + i if start_row is not None else i
            cells = row if isinstance(row, list) else []
            lines.append(f"{no}: " + "\t".join(str(c) for c in cells))
        return "\n".join(lines), locator
    if name == "docx_paragraphs":
        # 表（`tables`）も本文合成の対象にする——段落だけを見ると、表しか
        # 無い docx（段落0件）は read_evidence が常に空になる。
        paras = [p for p in (result.get("paragraphs") or []) if isinstance(p, dict)]
        tables = [t for t in (result.get("tables") or []) if isinstance(t, dict)]
        if not paras and not tables:
            return None, None
        lines = [f"段落{p.get('i')}: {p.get('text', '')}" for p in paras]
        for t in tables:
            ti = t.get("i")
            row_start = t.get("row_start") or 0
            for ri, row in enumerate(t.get("rows") or []):
                cells = row if isinstance(row, list) else []
                lines.append(f"表{ti}行{row_start + ri}: " + "\t".join(str(c) for c in cells))
        text = "\n".join(lines)
        # 段落の範囲だけを locator にすると、段落側は同じでも表側のページング
        # （`table_row_start` を進めて呼び直す）が違う2回の呼び出しが同じ locator に潰れる——
        # `InvestigationState._find` の同一性判定は locator 文字列そのものなので、表の行範囲も
        # locator に含めて「実際に返した範囲」を表す（`paragraphs[s-e];tables[ts-te]rows[rs-re]`）。
        ids = [p.get("i") for p in paras]
        parts = []
        if ids:
            parts.append(f"paragraphs[{ids[0]}-{ids[-1]}]")
        if tables:
            t_ids = [t.get("i") for t in tables if isinstance(t.get("i"), int)]
            row_ranges = [(t.get("row_start"), len(t.get("rows") or [])) for t in tables]
            row_lo = [rs for rs, n in row_ranges if isinstance(rs, int) and n > 0]
            row_hi = [rs + n - 1 for rs, n in row_ranges if isinstance(rs, int) and n > 0]
            if t_ids:
                tables_part = f"tables[{min(t_ids)}-{max(t_ids)}]"
                if row_lo and row_hi:
                    tables_part += f"rows[{min(row_lo)}-{max(row_hi)}]"
                parts.append(tables_part)
        locator = ";".join(parts) if parts else "paragraphs"
        return (text or None), locator
    if name == "pptx_slides":
        # 表・ノートも本文合成の対象にする（段落と同じ理由・スライドはテキストのみ
        # だと表の内容やノートの補足が read_evidence から丸ごと落ちる）。
        slides = [s for s in (result.get("slides") or []) if isinstance(s, dict)]
        if not slides:
            return None, None
        lines = []
        for s in slides:
            no = s.get("no")
            parts = [f"スライド{no}: " + " / ".join(s.get('texts') or [])]
            for ti, table in enumerate(s.get("tables") or []):
                for ri, row in enumerate(table if isinstance(table, list) else []):
                    cells = row if isinstance(row, list) else []
                    parts.append(f"スライド{no}表{ti}行{ri}: " + "\t".join(str(c) for c in cells))
            notes = s.get("notes")
            if notes:
                parts.append(f"スライド{no}ノート: {notes}")
            lines.append("\n".join(parts))
        text = "\n".join(lines)
        nos = [s.get("no") for s in slides]
        locator = f"slides[{','.join(str(n) for n in nos)}]" if nos else "slides"
        return text, locator
    if name == "pdf_pages":
        pages = [p for p in (result.get("pages") or []) if isinstance(p, dict)]
        if not pages:
            return None, None
        text = "\n".join(f"ページ{p.get('no')}: {p.get('text', '')}" for p in pages)
        nos = [p.get("no") for p in pages]
        locator = f"pages[{','.join(str(n) for n in nos)}]" if nos else "pages"
        return text, locator
    if name == "file_head":
        text = result.get("text")
        if not text:
            return None, None
        return text, "head"
    return None, None


def _shrink_xlsx_range_field(orig_range: str | None, n_rows: int) -> str | None:
    """`orig_range`（doc_readers.xlsx_range が返した実際の A1 レンジ）を、行が
    `n_rows` 行へバイト予算で削減された場合の実際の範囲に更新する（列は不変・終了行だけ詰める）——
    更新しないと、行を減らしても `range`（延いては `locator`）が元の（削る前の）範囲のまま
    食い違って残る。解析できなければ元の値のまま返す（fail-safe・致命的ではない）。
    """
    if not orig_range or n_rows <= 0:
        return orig_range
    try:
        from openpyxl.utils.cell import range_boundaries
        from openpyxl.utils import get_column_letter
        min_col, min_row, max_col, _max_row = range_boundaries(orig_range)
        return f"{get_column_letter(min_col)}{min_row}:{get_column_letter(max_col)}{min_row + n_rows - 1}"
    except Exception:
        return orig_range


# 二分探索で1件も残せない場合に「先頭1件の text を切り詰めて残す」対応を
# 実装済みのツール（dict 要素が str の `text` フィールドを持つ形）。xlsx_range（行=セルのリスト）・
# pptx_slides（要素は `texts`/`tables`/`notes`・単一の text フィールドが無い）は対象外——
# 1件も入らなければ従来どおり空のまま返す。
_SINGLE_ITEM_TEXT_FIELDS = frozenset({"pdf_pages"})


def _shrink_single_item_result(name: str, result: dict, doc_id: str, field: str,
                               item, tr_max_bytes: int) -> dict | None:
    """1件も残せないとき、先頭1件だけを予算内へ切り詰めて `text_truncated: true`
    を立てて残す（番号（`no`/`i`）と locator は保つ）——全消滅より情報量を残す。
    """
    if not isinstance(item, dict):
        return None
    text0 = item.get("text")
    if not isinstance(text0, str) or not text0:
        return None

    def _build_one(txt: str) -> dict:
        it = {**item, "text": txt, "text_truncated": True}
        r = dict(result)
        r[field] = [it]
        # 鍵ブロックの補完伏せ字は `_redact_deep`（呼び出し元・run_tool）が構造の
        # 全フィールドへ既に文書順で適用済み——ここで合成する text はその済みのフィールドから
        # 組むだけなので、合成後に改めて伏せ字を掛け直す必要はない。
        text, locator = _doc_reader_text_locator(name, r)
        if text:
            r = {**r, "doc_id": doc_id, "text": text, "locator": locator}
        return r

    lo, hi, best_text = 0, len(text0), ""
    while lo <= hi:
        mid = (lo + hi) // 2
        if _result_byte_size(_build_one(text0[:mid])) <= tr_max_bytes:
            best_text = text0[:mid]
            lo = mid + 1
        else:
            hi = mid - 1
    return _build_one(best_text)


# バイト予算で切ったことを申告する印 `"byte_clipped": true` の JSON 上の増分（計測用・件数上限と
# 区別する）。切り詰め後に印を足しても予算内に収まるよう、切り詰め側は先にこの分を差し引く。
_BYTE_CLIP_MARK_BYTES = len(', "byte_clipped": true')


def _finish_docx_paragraphs_result(result: dict, doc_id: str, tr_max_bytes: int) -> dict:
    """`docx_paragraphs` 専用の仕上げ——段落だけでなく表（`tables`・表→行）も
    バイト予算の削減対象にする。予算超過時は**段落を先に確保**（表 0 行で段落数を二分探索）し、
    余った予算で表の行を先頭の表から順に埋める（行単位でフラット化・途中で打ち切ると
    `row_truncated`）——表を満量のまま段落を削ると大きな表の文書で段落が 1 件も返らない。
    段落が丸ごと1件も入らない場合の救済も、まず表を `best_n_rows` に固定したまま先頭1段落を
    切り詰めて試す（表が小さければこれで非空の段落と表の両方が残る）。表がそれ自体で予算の
    大半を占めるほど大きく、段落が空文字になる・予算を超えるいずれかに終わる場合だけ、表を
    0行にした状態で段落を切り詰め直してから、残り予算で表の行数を改めて決める。
    """
    paras = [p for p in (result.get("paragraphs") or []) if isinstance(p, dict)]
    tables_orig = [t for t in (result.get("tables") or []) if isinstance(t, dict)]
    flat_rows: list[tuple[int, object]] = []
    for ti, t in enumerate(tables_orig):
        for row in (t.get("rows") or []):
            flat_rows.append((ti, row))

    def _tables_for(n_rows_keep: int) -> list[dict]:
        if n_rows_keep <= 0:
            return []
        counts: dict[int, int] = {}
        for ti, _row in flat_rows[:n_rows_keep]:
            counts[ti] = counts.get(ti, 0) + 1
        out = []
        for ti, t in enumerate(tables_orig):
            n = counts.get(ti, 0)
            if n <= 0:
                continue
            orig_rows = t.get("rows") or []
            nt = {**t, "rows": orig_rows[:n]}
            if n < len(orig_rows):
                nt["row_truncated"] = True
            out.append(nt)
        return out

    def _build(n_paras: int, n_rows_keep: int) -> dict:
        r = dict(result)
        r["paragraphs"] = paras[:n_paras]
        r["tables"] = _tables_for(n_rows_keep)
        # 合成元の paragraphs/tables は既に `_redact_deep` を通過済み（呼び出し元・
        # run_tool）——合成後に鍵ブロックの補完伏せ字を掛け直す必要はない。
        text, locator = _doc_reader_text_locator("docx_paragraphs", r)
        if text:
            r = {**r, "doc_id": doc_id, "text": text, "locator": locator}
        return r

    full = _build(len(paras), len(flat_rows))
    if _result_byte_size(full) <= tr_max_bytes:
        return full
    tr_max_bytes = max(1, tr_max_bytes - _BYTE_CLIP_MARK_BYTES)   # 計測用の印の分を予算から先に引く

    # 段落を先に確保する（表 0 行で段落数を二分探索）→ 余った予算で表の行を埋める。
    # 表を満量のまま段落を削ると、大きな表を持つ文書で段落が 1 件も返らず、段落のページング
    # （start/count）でも回収できなくなる。
    lo, hi, best_n_paras = 0, len(paras), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if _result_byte_size(_build(mid, 0)) <= tr_max_bytes:
            best_n_paras = mid
            lo = mid + 1
        else:
            hi = mid - 1
    lo, hi, best_n_rows = 0, len(flat_rows), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if _result_byte_size(_build(best_n_paras, mid)) <= tr_max_bytes:
            best_n_rows = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if best_n_paras > 0:
        r = _build(best_n_paras, best_n_rows)
        r["truncated"] = True
        r["byte_clipped"] = True                     # バイト予算由来（計測用の印）
        return r

    # 段落が1件も丸ごと入らない場合——小さな表が1行でも入る（`best_n_rows > 0`）からといって
    # ここで早期に返してしまうと、段落の救済（下）へ絶対に到達しない（表さえあれば長い段落が
    # 丸ごと消える）。表しか無い docx（`paras` が空）はこの救済の対象外——表は1行単位でしか
    # 縮められず、単一の `text` フィールドを持たないため。
    if paras:
        # まず表を `best_n_rows`（段落0件を前提に決めた行数）に固定したまま段落を救済する
        # （表が小さければ従来どおりここで非空の段落と表の両方を確保できる）。表がそれ自体で
        # 予算の大半を占めるほど大きい場合はこの救済が「段落が空文字」または「予算超過」に
        # 終わる——その場合だけ表を**0行にした状態で**段落を救済し直す（表を先に含めると、
        # 表だけで予算をほぼ使い切り段落本文が空になりうる）。
        fixed_tables_base = dict(result)
        fixed_tables_base["tables"] = _tables_for(best_n_rows)
        shrunk = _shrink_single_item_result("docx_paragraphs", fixed_tables_base, doc_id,
                                            "paragraphs", paras[0], tr_max_bytes)
        shrunk_ok = (shrunk is not None and (shrunk.get("paragraphs") or [{}])[0].get("text")
                    and _result_byte_size(shrunk) <= tr_max_bytes)
        if not shrunk_ok:
            zero_tables_base = dict(result)
            zero_tables_base["tables"] = []
            shrunk = _shrink_single_item_result("docx_paragraphs", zero_tables_base, doc_id,
                                                "paragraphs", paras[0], tr_max_bytes)
        if shrunk is not None:
            # 救済した段落を固定した上で、表の行数を残り予算に合わせて改めて二分探索する
            # （`fixed_tables_base` 経由で救済できた場合も、この再探索は同じ `best_n_rows` に
            # 収束する——表の行を増やすほど段落に残せる文字数が単調に減るため）。
            def _with_tables(n_rows_keep: int) -> dict:
                r = dict(shrunk)
                r["tables"] = _tables_for(n_rows_keep)
                text, locator = _doc_reader_text_locator("docx_paragraphs", r)
                if text:
                    r = {**r, "doc_id": doc_id, "text": text, "locator": locator}
                return r

            lo, hi, rescued_n_rows = 0, len(flat_rows), 0
            while lo <= hi:
                mid = (lo + hi) // 2
                if _result_byte_size(_with_tables(mid)) <= tr_max_bytes:
                    rescued_n_rows = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
            final = _with_tables(rescued_n_rows)
            final["truncated"] = True
            final["byte_clipped"] = True                 # バイト予算由来（件数上限と区別する計測用の印）
            return final
    r = _build(0, best_n_rows)
    r["truncated"] = True
    r["byte_clipped"] = True
    return r


def _finish_reader_result(name: str, result: dict, doc_id: str, tr_max_bytes: int) -> dict:
    """原本読取ツール6本共通の仕上げ（`_redact_deep` の後に呼ぶ）: `doc_id`/`text`/`locator`
    合成とバイト上限クリップを同時に行う——`text` は `result[field]`（rows/paragraphs/
    slides/pages/sheets）から毎回作り直すため、クリップで `field` を削っても `text` が古い
    （削る前の）内容のまま残って予算を超えたり、構造と食い違ったりしない。二分探索は
    `doc_id`/`text`/`locator` を含めた最終形の JSON バイト数（`_result_byte_size`）で判定する。
    エラー結果はそのまま。

    `docx_paragraphs` は表も削減対象にする必要があるため専用の
    `_finish_docx_paragraphs_result` に委譲する。
    """
    if not (isinstance(result, dict) and not result.get("error")):
        return result
    if name == "docx_paragraphs":
        return _finish_docx_paragraphs_result(result, doc_id, tr_max_bytes)
    field = _READER_CLIP_FIELD.get(name)

    def _build(seq):
        r = dict(result)
        if field is not None and seq is not None:
            r[field] = seq
            if name == "xlsx_range":
                # 行を削った分だけ `range`（延いては locator）も実際の範囲へ合わせる。
                r["range"] = _shrink_xlsx_range_field(result.get("range"), len(seq))
        # 合成元の rows/paragraphs/slides/pages/sheets は既に `_redact_deep` を
        # 通過済み（呼び出し元・run_tool）——合成後に鍵ブロックの補完伏せ字を掛け直す必要はない。
        text, locator = _doc_reader_text_locator(name, r)
        if text:
            r = {**r, "doc_id": doc_id, "text": text, "locator": locator}
        return r

    full_seq = result.get(field) if field is not None else None
    full = _build(full_seq)
    if field is None or not isinstance(full_seq, (list, str)) or _result_byte_size(full) <= tr_max_bytes:
        return full
    tr_max_bytes = max(1, tr_max_bytes - _BYTE_CLIP_MARK_BYTES)   # 計測用の印の分を予算から先に引く（印を足しても予算内）
    lo, hi, best = 0, len(full_seq), _build(full_seq[:0])
    while lo <= hi:
        mid = (lo + hi) // 2
        cand = _build(full_seq[:mid])
        if _result_byte_size(cand) <= tr_max_bytes:
            best = cand
            lo = mid + 1
        else:
            hi = mid - 1
    # 1件も残せない（best のフィールドが空）場合、先頭1件だけ text を予算内へ切り詰めて
    # 残す（対応済みツールのみ・`_SINGLE_ITEM_TEXT_FIELDS` 参照）。
    if (isinstance(full_seq, list) and full_seq and name in _SINGLE_ITEM_TEXT_FIELDS
            and not (best.get(field) if field is not None else None)):
        shrunk = _shrink_single_item_result(name, result, doc_id, field, full_seq[0], tr_max_bytes)
        if shrunk is not None:
            shrunk["truncated"] = True
            shrunk["byte_clipped"] = True                # バイト予算由来（件数上限と区別する計測用の印）
            return shrunk
    best["truncated"] = True
    best["byte_clipped"] = True
    return best


# 障害先（fulltext/graph/read）の分類——ツール名はこの分類にだけ使い、回復可否の判定には
# 使わない（回復可否は例外の型で決める・`_is_recoverable_tool_exception` 参照）。
_TOOL_BACKEND_KIND = {"es_search": "fulltext", "graph_neighbors": "graph"}


def _tool_backend_kind(name: str) -> str:
    """ツール名から障害先（`InvestigationState.backend_failures` の閉じたキー）を返す。"""
    return _TOOL_BACKEND_KIND.get(name, "read_io")


def _unreachable_backends_at_start(avail: dict | None, tp: dict) -> list:
    """このターン、**実接続の不達**で使えないバックエンドの種別（利用者の OFF は含めない）。

    不達のときツール集合から `es_search`／`graph_neighbors` 自体が外れるため、実行中に障害として
    記録される機会が無い——ツール集合を組む時点で1回だけ記録するための判定（`toolset` を明示
    指定された経路は可用性判定を一切参照しない契約のため対象外＝呼び出し元が `avail=None` で
    渡す）。戻り値は `InvestigationState.backend_failures` の閉じたキー。
    """
    if avail is None:
        return []
    out = []
    if not avail.get("fulltext") and tp.get("fulltext"):
        out.append("fulltext")
    if not avail.get("graph") and tp.get("graph"):
        out.append("graph")
    return out


def _is_recoverable_tool_exception(exc: BaseException) -> bool:
    """接続断・タイムアウト・読取I/O（ES/Neo4j クライアント例外・`OSError`/`TimeoutError` 系）だけを
    回復可能とする——それ以外（`TypeError`/`KeyError`/assert 失敗等のプログラムの欠陥を示す例外・
    クエリのバグや設定ミスを示す Neo4j `ClientError` 系）は回復不可扱いにする。単発フォールバックへの
    縮退可否（`providers/base.py`）の判定基準はこの1関数に集約する。
    """
    # `HTTPError` は `URLError`/`OSError` のサブクラスだが、ステータスコードで回復可否が分かれる
    # （4xx＝クライアント起因＝プログラム・設定の欠陥＝回復不可／5xx・429＝一時的な障害＝回復可能）
    # ため、下の一般 `OSError` 判定より先に見る（`_retryable_post_error` と同じ判定順）。
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or 500 <= exc.code <= 599
    if isinstance(exc, (OSError, TimeoutError)):
        return True
    # 遅延 import（他の遅延 import と同じ理由）。neo4j の例外階層は `DriverError`
    # （`ServiceUnavailable`/`SessionExpired` 等・ドライバ自身が検出した接続系の失敗）と
    # `Neo4jError` の派生2系統（`TransientError`＝サーバ側の一時的な過負荷等／`ClientError`＝
    # `CypherSyntaxError` 含む・クエリのバグ）に分かれる——回復可能なのは接続系（`DriverError`）と
    # 一時的（`TransientError`）だけで、`ClientError` 系はプログラムの欠陥として回復不可扱いに
    # する。`ConfigurationError`（設定ミス）は実際のクラス階層上は `DriverError` のサブクラスだが
    # 意味的には非一時的な設定不備＝回復不可のため、`DriverError` 判定の前に別枠で除外する。
    from neo4j.exceptions import ConfigurationError, DriverError, TransientError
    if isinstance(exc, ConfigurationError):
        return False
    return isinstance(exc, (DriverError, TransientError))


def _record_tool_exception(state, name: str, exc: BaseException) -> None:
    """ツール呼び出し例外を `InvestigationState` へ記録する（回復可能なら種別ごと・回復不可なら
    `non_recoverable_failure`）。`run_tool` 境界の例外変換（並列/直列の両経路）が呼ぶ。"""
    if _is_recoverable_tool_exception(exc):
        state.mark_backend_failure(_tool_backend_kind(name))
    else:
        state.mark_non_recoverable_failure()


# `graph_neighbors`（`lens_service.neighbor_cards`）が内部で捕捉した障害の固定コード——本文・
# 例外メッセージは持たない（ログの秘匿契約に合わせる）。`_record_tool_result_error_code` が
# 拾って `InvestigationState` へ反映する。
_GRAPH_NEIGHBORS_RECOVERABLE_ERROR_CODE = "graph_unavailable"
_GRAPH_NEIGHBORS_NON_RECOVERABLE_ERROR_CODE = "graph_internal_error"
# グラフのスキーマ世代不一致（`GraphSchemaEraError`）のツール結果コード——API 経路
# （`run_tool` の `graph_neighbors` 分岐）と MCP 経路（`mcp_server.py::handle`・
# `providers/codex/mcp.py::_graph_schema_era_from_item`）が共有する閉じたコード（本文を持たない）。
GRAPH_REINGEST_ERROR_CODE = "graph_reingest_required"

# グラフが使えないまま調べ続けたターンの通知（平文・専門用語ゼロ・資料名や本文を含まない）。
# 3状態（入口で使えない＝未構築/OFF/不達／世代が古い／調査の途中で接続できなくなった）で別の
# 文言にする——利用者の次の一手が違うため「使えない」と「取り込み直しが要る」を丸めない。
# API 経路（`providers/base.py`）・Codex 経路（`providers/codex/provider.py`）・非agentic
# （`chat_service._finalize`）が同じ文言を共有する。
_GRAPH_DEGRADED_BLOCKED = "blocked"
GRAPH_DEGRADED_NOTICES = {
    _GRAPH_DEGRADED_BLOCKED: "関係のつながりをたどる検索が使えないため、資料とソースを直接調べて回答します。",
    GRAPH_REINGEST_ERROR_CODE: ("関係のつながりの情報が古いため今回は使わず、資料とソースを直接調べて"
                                "回答します（管理者に取り込み直しを依頼してください）。"),
    # 入口で不達だったターンと調査の途中で切れたターンで同じ文言を使う（利用者にとっては
    # どちらも「つながりを見に行けなかった」＝次の一手は同じ）。
    _GRAPH_NEIGHBORS_RECOVERABLE_ERROR_CODE: ("関係のつながりをたどる検索に接続できなかったため、"
                                              "資料とソースを直接調べた結果で回答します。"),
}


def graph_degraded_notice(code: str | None) -> str:
    """縮退コード（閉集合）に対応する通知文言。未知・None は空文字（通知しない）。"""
    return GRAPH_DEGRADED_NOTICES.get(code or "", "")


def _record_tool_result_error_code(state, result, name: str | None = None) -> None:
    """`run_tool()` の結果に固定理由コード（`error_code`）が付いていれば `InvestigationState` へ
    反映する——例外にならず結果化された障害を拾う経路。対象:
    - `graph_reingest_required`（`error` フィールド・グラフの世代不一致＝再取り込み待ち。接続断と
      別項目で数え、通知文言も分ける）。
    - `read_io_failed`（`_open_doc_stream` の読取I/O失敗）。
    - `graph_unavailable`/`graph_internal_error`（`lens_service.neighbor_cards` が内部で捕捉し
      握りつぶしていた障害——`_record_tool_exception` と同じ回復可否の意味で `graph_neighbors`
      専用に記録する）。
    - `es_search` の `degrade_reason` が `es_unavailable`/`es_query_failed`（BM25 自体も失敗し
      hits が強制的に空になった＝一時的な障害・`_ES_DEGRADE_WORDING` に含まれない既知値）のとき
      `backend_failures["fulltext"]` を立てる。`es_query_rejected`（4xx＝クエリの構文/設定不備＝
      一時的でない）は `non_recoverable_failure` を立てる。
    """
    if not isinstance(result, dict):
        return
    if result.get("error") == GRAPH_REINGEST_ERROR_CODE:
        # 世代不一致（`run_tool` の `graph_neighbors` 分岐が変換した結果）——接続断とは別状態。
        state.mark_graph_schema_era_mismatch()
    code = result.get("error_code")
    if code == "read_io_failed":
        state.mark_backend_failure("read_io")
    elif code == _GRAPH_NEIGHBORS_RECOVERABLE_ERROR_CODE:
        state.mark_backend_failure("graph")
    elif code == _GRAPH_NEIGHBORS_NON_RECOVERABLE_ERROR_CODE:
        state.mark_non_recoverable_failure()
    if name == "es_search":
        reason = result.get("degrade_reason")
        if reason in ("es_unavailable", "es_query_failed"):
            state.mark_backend_failure("fulltext")
        elif reason == "es_query_rejected":
            state.mark_non_recoverable_failure()


# ---- ツール実行（read-only・world＋scope に限定）----

def run_tool(name: str, args: dict, world: str, scope_paths,
            deadline: float | None = None, layer=None,
            max_hits: int | None = None, window_cap: int | None = None,
            tool_result_max_bytes: int | None = None,
            uid: str | None = None) -> tuple[dict, set, list, list]:
    """ツールを実行し `(結果, 触れた doc_id 集合, 引用候補, 候補カード)` を返す。範囲外/未解決/秘匿は安全に error。

    引用候補＝`{doc_id, span, quote, ext}`（grep/ES ヒット由来・UI/出典用）。候補カード＝`graph_neighbors` 由来の
    原因候補（troubleshoot の UI/エクスポート用）。tool result の本文は **秘密を伏せて**返す。

    `uid`（DEPTH-2 S2・§2.7・省略可・既定 `None`）: `write_output_file`（成果物登録の共通化・個人
    workspace の files/ へ保存）の書き先ユーザー id。`None`（省略）のときこのツールは実行せず
    「作成者が特定できません」という error を返す（他のツールは `uid` を使わない）。

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
    呼び出し元（`openai_style`）が既に実効基準値を解決済みの
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
        doctype_filter = str(args.get("doctype") or "").strip().lower()
        state_filter = str(args.get("state") or "").strip().lower()
        try:
            limit = int(args.get("limit") or 200)
        except (TypeError, ValueError):
            limit = 200
        limit = max(1, min(limit, 500))                    # read_around の window と同じ流儀でクランプ
        try:
            offset = int(args.get("offset") or 0)
        except (TypeError, ValueError):
            offset = 0
        offset = max(0, offset)
        # 層判定は `doc_ledger`（`classify_document` 確定済み・§7 裁定10）の `branch=="source"` を
        # 使う（`layer_mod.in_layer`＝拡張子だけの近似は使わない・grep/ES と同じ確定判定に揃える）。
        rows = [r for r in doc_ledger.documents_for(world, deadline=deadline)
               if scope_mod.in_scope(r["name"], sp)
               and layer_mod.in_layer_code(r.get("branch") == "source", layer)]
        if prefix:
            rows = [r for r in rows if scope_mod.in_scope(r["name"], [prefix])]   # 同じ prefix 一致ロジックを再利用
        if pattern:
            rows = [r for r in rows if pattern in r["name"].lower()]
        if doctype_filter:
            rows = [r for r in rows if str(r.get("doctype") or "").lower() == doctype_filter]
        # state（"ready"/"unreadable"/"unknown"）も通す——読み取り不可な文書を「使える」文書と同列に見せない。
        if state_filter:
            rows = [r for r in rows if str(r.get("state", "ready")).lower() == state_filter]
        rows.sort(key=lambda r: r["name"])                 # rel_path 昇順固定（同条件なら offset が安定する）
        page = rows[offset:offset + limit]
        out = [{"rel_path": r["name"], "doctype": r.get("doctype"), "state": r.get("state", "ready")}
              for r in page]
        for d in out:
            docs.add(d["rel_path"])                        # 一覧に出した分だけ出典（sources）に載せる
        shown_end = offset + len(out)
        truncated = shown_end < len(rows)
        return ({"count": len(rows), "offset": offset, "docs": out, "truncated": truncated,
                "next_offset": shown_end if truncated else None}, docs, cites, cards)
    if name == "folder_tree":
        # K6: フォルダはドキュメントではない（`docs`＝doc_id 集合には何も足さない・出典/引用の対象外
        # ——list_docs/glob_search が返す実ファイル rel_path とは異なる）。
        from . import folder_tree as folder_tree_mod
        result = folder_tree_mod.build(world, args, scope_paths=sp, deadline=deadline, layer=layer,
                                       tool_result_max_bytes=tr_max_bytes)
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
        cap_reached = False                             # ヒット上限に達した（母集団の一部しか見ていない）
        if name == "es_search":
            from . import documents                       # ES ヒットは現 world に**実在する doc** だけ採用
            # 古い ES 索引由来の 404／別内容リンクを引用/出典に出さない（非agentic の _es_citations と同じ実在チェック）。
            # 実在集合は**1回だけ**作る（per-hit のツリー走査を避ける・rv MED）。
            valid = documents.world_rel_set(world, deadline=deadline)
            # `es_index.search()` は (hits, degrade_reason) を返す
            # （BM25 継続時の縮退理由・`embedding_cloud_unavailable`/`query_embed_failed` 等）。
            # ここではまだ tool result に生値のまま載せる（呼び出し元＝各 dialect のループが
            # `_degrade_result_node()` で既知語彙だけを思考ノードへ変換する）。
            # k_ceiling=MAX_HITS_ABS_MAX（grep と共通の絶対上限）で es_index 側の env 由来の
            # 再クランプ（既定 50）を迂回する——grep（下の分岐）には元々このような再クランプが無い。
            es_hits, degrade_reason = es_index.search(world, q, scope_paths=sp,
                                                      k=(max_hits or MAX_HITS), layer=layer,
                                                      k_ceiling=MAX_HITS_ABS_MAX)
            # 上限到達は実在チェック・秘匿名除外の前（生ヒット数）で判定する——後で落ちた分で
            # 件数が減っても「上限まで返っていた＝母集団の一部」という事実は変わらない。
            cap_reached = len(es_hits) >= (max_hits or MAX_HITS)
            # 秘匿名文書（`is_sensitive` 導入前に索引化された既存 ES ヒット）は実在チェックの後、
            # ここで一律に弾く——`_safe_doc_path` の秘匿名ガードは read_around（精読）専用で
            # es_search は通らないため、ここで塞がないと `credentials.xlsx` 等の本文が MCP/外部
            # 検索 API へそのまま返る（台帳 #80）。件数（`count`/`docs`）にも含めない。
            valid_es_hits = []
            for h in es_hits:
                did = h.get("doc_id")
                if not did or did not in valid:
                    continue
                if text_kind.is_sensitive_doc_id(did):
                    _log.warning("es_search: 秘匿名のため対象外にしました（ext=%s）", Path(did).suffix.lower())
                    continue
                valid_es_hits.append(h)
            hits = [{"doc_id": h["doc_id"], "line": h.get("line"), "text": h.get("text", ""),
                     "span": [h.get("line"), h.get("line")], "ext": h.get("ext"),
                     "score": h.get("score"),                       # 親返し（L4c）の並び順にのみ使う・LLM 出力へは出さない
                     **({"locator": h["locator"]} if h.get("locator") is not None else {}),
                     **({"chunk_id": h["chunk_id"]} if h.get("chunk_id") is not None else {}),
                     **({"parent_id": h["parent_id"]} if h.get("parent_id") is not None else {})}
                    for h in valid_es_hits]
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
        #
        # 順位保持: `hits` は ES ヒットのスコア降順のまま
        # 渡ってくる（`es_index.search`）。legacy ヒットを先に全部積み、rag 文書の集約結果を
        # 末尾へ `extend` すると、rag 文書のスコアが legacy ヒットより高くても常に後方へ回ってしまう
        # （検索結果全体のスコア降順という契約を崩す）。`rag_slot_index` で各 doc の
        # **最初に出現した（＝最高スコアの）ヒットの位置**を `out` 内に予約し、legacy ヒットはその場で
        # 確定させる二段構えにする——`_resolve_parent_return`（budget 計算後にしか結果が出ない）の
        # 完了を待たずに全体の並びを一度の走査で確定できる。
        parent_return_on = name == "es_search" and _parent_return_enabled()
        rag_groups: dict = {}
        rag_slot_index: dict[str, int] = {}   # doc_id -> `out` 内の予約位置（代表ヒットの位置）
        for h in hits:
            docs.add(h["doc_id"])
            redacted_text = _redact(h["text"])
            quote = redacted_text[:500]            # citation の quote（出典カードの表示用）だけ固定上限・LLM 向け本文は切らない
            # rag_chunks 由来（locator あり）は位置ヒントを LLM への text にだけ添える。
            # citation の quote は hint 抜きのまま（出典フッターは doc_id リンクのみで locator は
            # 出さない・docs/04 契約は不変）。LLM 向け本文は文字数で切らない（ヒット全文を渡す・
            # 量の上限は tool result のバイト予算だけ）。hint は本文と結合してから redaction を通す。
            hint = citations.locator_hint(h.get("locator"))
            text_for_llm = _redact(f"{h['text']}（位置: {hint}）") if hint else redacted_text
            # 引用（cites）は tier に関わらず**子チャンク単位**のまま（親返しで粒度を落とさない・
            # §3.3「引用の粒度は落とさない」）——doc 単位への束ねは `out`（LLM 向け表示）にだけ効く。
            cites.append(citations.from_grep_hit(h, quote=quote, include_match=False))  # match 無し・整形は citations に集約
            if parent_return_on and h.get("chunk_id"):
                rag_item = {
                    "chunk_id": h["chunk_id"], "parent_id": h.get("parent_id"),
                    "locator": h.get("locator"), "score": h.get("score"), "text": text_for_llm,
                }
                rag_groups.setdefault(h["doc_id"], []).append(rag_item)
                if h["doc_id"] not in rag_slot_index:
                    # 最初に出現した位置＝ES ヒットのスコア降順の下でその doc の最高スコア
                    # （同 doc の2件目以降は既に予約済みの枠へ集約されるだけ・新しい枠は作らない）。
                    rag_slot_index[h["doc_id"]] = len(out)
                    out.append(None)                      # 集約結果が確定するまでの予約枠
                continue
            hit_view = {"doc_id": h["doc_id"], "line": h["line"], "text": text_for_llm}
            # grep（`ripgrep_search`）ヒットが持つ登録者重要度（`grep_tool.
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
            # legacy ヒット分（`out` に既に積んだ分・予約枠の `None` は除く）を先に差し引いた残りが
            # rag doc 群の予算（§3.4「全文書ぶんの最低保証」は legacy を含めた tool result 全体の
            # 予算から見る）。
            legacy_bytes = sum(len(hv["text"].encode("utf-8")) for hv in out if hv is not None)
            budget_for_rag = max(0, tr_max_bytes - legacy_bytes)
            resolved = _resolve_parent_return(world, rag_groups, sp, layer, budget_for_rag)
            resolved_by_doc = {r["doc_id"]: r for r in resolved}
            for doc_id, idx in rag_slot_index.items():
                out[idx] = resolved_by_doc[doc_id]   # 予約した代表位置へ集約結果を差し戻す（§順位保持）
        view = {"hits": out}
        # ヒット上限（max_hits）に達した検索は母集団の一部しか返していない＝打ち切りの印を返す
        # （続きを取る引数は無い＝モデルは範囲を絞る・別の語で探す・未確認として扱う）。
        if cap_reached or len(hits) >= (max_hits or MAX_HITS):
            view["truncated"] = True
        if degrade_reason:                              # es_search のみ・BM25 継続時の縮退理由
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
        from .ingest.world_neo4j import GraphSchemaEraError   # 遅延 import（他の遅延 import と同じ理由）
        term = str(args.get("name") or "")
        try:
            raw_cards = lens_service.neighbor_cards(world, term, sp) if term else []
        except GraphSchemaEraError as e:
            # 世代不一致は調査を終端させず、MCP 側（`mcp_server.py::handle`）と同じ機械可読コードの
            # ツール結果へ変換して返す——以降のツール呼び出し（grep/原本直読）はそのまま続く。
            # 呼び出し元（`run_tool` 境界）が `_record_tool_result_error_code` 経由で
            # `InvestigationState.graph_schema_era_mismatch` を立てる。
            return ({"error": GRAPH_REINGEST_ERROR_CODE, "world": e.world, "stored_era": e.stored_era},
                    docs, cites, cards)
        # `neighbor_cards` が内部で捕捉した障害（`lens_service.NeighborCardsFailure.error_code`・
        # 属性が無ければ通常の空/非空リストのまま None）——戻り値の `list` 型自体は変えない。
        _graph_error_code = getattr(raw_cards, "error_code", None)
        # LLM 向け `view` は
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
                 "path": c.get("path", []), "distance": c.get("distance"),
                 "edges": _card_edges_view(c)} for c in cards]
        result = {"neighbors": view}
        if len(clipped) < len(raw_cards):
            # 件数／バイト上限で捨てた分がある＝返した近傍は部分集合。続きを取る引数は無いため、
            # 呼び出し側（モデル）が「すべて」と断定しないよう打ち切りの事実と総数を返す。
            result["truncated"] = True
            result["count"] = len(raw_cards)
        if _graph_error_code:
            # `neighbor_cards` が内部で捕捉した障害（`"graph_unavailable"`/`"graph_internal_error"`）
            # ——呼び出し元（`run_tool` 境界）が `_record_tool_result_error_code` 経由で
            # `InvestigationState.backend_failures["graph"]`/`non_recoverable_failure` へ反映する。
            result["error_code"] = _graph_error_code
        return (result, docs, cites, cards)
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
        # ストリーミング窓抽出: `line` は既知なので、窓の外まで読む必要が無い
        # （目標の終端 `e_target` に達したら即座に打ち切る＝ファイル全体を保持しない）。総行数
        # （`total_lines`）は read_around の結果に含まれないため、`e_target` を総行数へ
        # クランプする必要も無い——EOF が `e_target` より
        # 先に来れば、そこまでの内容が自然にそのまま結果になる。
        s = max(0, line - 1 - window)              # 0-based 窓の開始
        e_target = line - 1 + window + 1            # 0-based 窓の終端（排他）
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
        # 返却テキストの UTF-8 バイト数を上限で切り詰める（単一行が巨大な文書でも、
        # 履歴/SSE/次ターンの LLM 要求へ複製される量を bound する）。上限で実際に短くなった時だけ
        # `text_truncated` を明示する（read_doc/doc_outline の `text_truncated`/`file_truncated` と
        # 同じ語彙＝精読が黙って取りこぼさない）。
        read_around_truncated = len(text.encode("utf-8")) > tr_max_bytes
        text = _clip_utf8_bytes(text, tr_max_bytes)
        docs.add(doc_id)
        result = {"doc_id": doc_id, "text": text}
        if read_around_truncated:
            result["text_truncated"] = True
        return (result, docs, cites, cards)
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
            if len(diff_text.encode("utf-8")) > tr_max_bytes:   # 予算超過のときだけ印の分を先に引いて切る
                clipped = _clip_utf8_bytes(diff_text, max(1, tr_max_bytes - _BYTE_CLIP_MARK_BYTES))
                result = {**result, "diff": clipped, "truncated": True, "byte_clipped": True}
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
    if name in ("xlsx_sheets", "xlsx_range", "docx_paragraphs", "pptx_slides", "pdf_pages"):
        # S3b（原本読取ツール・`doc_readers.py`）: Office/PDF は常に docs 側扱い——探す対象（層）が
        # ソースに限定されたターンではこのツール自体を使わせない（file_head は下の別分岐で
        # テキスト・コードの層判定＝`_safe_original_path` の layer 引数を通す）。
        if layer == "code":
            return ({"error": "探す対象がソースに限定されています"}, docs, cites, cards)
        doc_id = str(args.get("doc_id") or "")
        kinds = {"xlsx_sheets": _XLSX_KINDS, "xlsx_range": _XLSX_KINDS,
                 "docx_paragraphs": _DOCX_KINDS, "pptx_slides": _PPTX_KINDS,
                 "pdf_pages": _PDF_KINDS}[name]
        resolved = _safe_original_path(world, doc_id, sp, kinds=kinds)
        if resolved is None:
            return ({"error": "doc_id が無効、または読み取り対象外です"}, docs, cites, cards)
        _root, _resolved_doc_id, rp, st = resolved
        f, open_err = _open_verified_original(_root, _resolved_doc_id, st)   # TOCTOU 再検証
        if open_err is not None:
            return (open_err, docs, cites, cards)
        from . import doc_readers
        if name == "xlsx_sheets":
            result = doc_readers.xlsx_sheets(f)
        elif name == "xlsx_range":
            sheet = str(args.get("sheet") or "")
            if not sheet:
                _close_quiet_local(f)
                return ({"error": "sheet が必要です"}, docs, cites, cards)
            kwargs = {"clean": _redact}
            if args.get("range"):
                kwargs["range_a1"] = str(args["range"])
            if args.get("max_rows") is not None:
                kwargs["max_rows"] = args["max_rows"]
            if args.get("max_cols") is not None:
                kwargs["max_cols"] = args["max_cols"]
            result = doc_readers.xlsx_range(f, sheet, **kwargs)
        elif name == "docx_paragraphs":
            kwargs = {"clean": _redact}
            if args.get("start") is not None:
                kwargs["start"] = args["start"]
            if args.get("count") is not None:
                kwargs["count"] = args["count"]
            if args.get("table_start") is not None:
                kwargs["table_start"] = args["table_start"]
            if args.get("table_row_start") is not None:
                kwargs["table_row_start"] = args["table_row_start"]
            result = doc_readers.docx_paragraphs(f, **kwargs)
        elif name == "pptx_slides":
            kwargs = {"clean": _redact}
            if args.get("pages"):
                kwargs["pages"] = str(args["pages"])
            result = doc_readers.pptx_slides(f, **kwargs)
        else:                                                       # pdf_pages
            kwargs = {"clean": _redact}
            if args.get("pages"):
                kwargs["pages"] = str(args["pages"])
            result = doc_readers.pdf_pages(f, **kwargs)
        result = _redact_deep(result)
        if not (isinstance(result, dict) and result.get("error")):
            docs.add(doc_id)                                        # 成功時だけ出典（sources）に載せる
            result = _finish_reader_result(name, result, doc_id, tr_max_bytes)
        return (result, docs, cites, cards)
    if name == "file_head":
        doc_id = str(args.get("doc_id") or "")
        resolved = _safe_original_path(world, doc_id, sp, kinds=_FILE_HEAD_KINDS, layer=layer)
        if resolved is None:
            return ({"error": "doc_id が無効、または読み取り対象外です"}, docs, cites, cards)
        _root, _resolved_doc_id, rp, st = resolved
        f, open_err = _open_verified_original(_root, _resolved_doc_id, st)   # TOCTOU 再検証
        if open_err is not None:
            return (open_err, docs, cites, cards)
        from . import doc_readers
        kwargs = {"clean": _redact}
        if args.get("max_bytes") is not None:
            kwargs["max_bytes"] = args["max_bytes"]
        result = _redact_deep(doc_readers.file_head(f, **kwargs))
        if not (isinstance(result, dict) and result.get("error")):
            docs.add(doc_id)
            result = _finish_reader_result(name, result, doc_id, tr_max_bytes)
        return (result, docs, cites, cards)
    if name == "write_output_file":
        # DEPTH-2 S2（§2.7）: docs/cites/cards は空のまま返す——個人 workspace の成果物は
        # 共有 KB の出典（sources）検証（`_verified_sources`/`verify_doc_exists`）の対象外
        # （RAG に索引化しない・出典欄には出さない契約）。成果物は別チャンネル
        # （`InvestigationState.created_files`→ envelope の `created_files`）で運ぶ。
        return (_run_write_output_file(args, uid), docs, cites, cards)
    if name in _USAGE_TOOL_ARG_KEYS:
        return _run_usage_tool(name, args, tr_max_bytes)
    return ({"error": f"unknown tool: {name}"}, docs, cites, cards)


# ---- 利用統計チャットの調査ツール（docs/proposals/2026-09-12-利用統計の拡充2.md §3b）----
# world/scope_paths とは無関係（管理者向け利用統計は KB world を持たない）。docs/cites は常に
# 空（引用機構を使わない）——「調べた内容」の記録は `cards` サイドカーに `{"tool","args"}` として積む
# （`graph_neighbors` が `cards` を候補カードのサイドカーとして使うのと同じ「run_tool の4つ目の
# 戻り値＝LLM には見せない呼び出し元専用の記録」という仕組みを転用。`usage_chat.py` はこれを集めて
# 応答の `tool_calls` にする）。戻り値は件数・時刻・種別・トークン・所要時間・会話 id・uid・world の
# みで、本文・会話タイトル・鍵・display_name は一切含めない（`sherpa/store/usage.py` の不変条件）。
_USAGE_TOOL_ARG_KEYS = {
    "usage_overview": ("days", "from", "to"),
    "usage_by_user": ("days", "uid", "kind", "from", "to"),
    "usage_conversations": ("days", "uid", "limit", "sort", "from", "to"),
    "usage_conversation_detail": ("conversation_id",),
    "usage_response_time": ("days", "provider"),
    "usage_daily": ("days", "metric"),
    "usage_stop_kinds": ("days", "uid", "from", "to"),
    "usage_depth_rounds": ("days", "from", "to"),
}
_USAGE_METRIC_VALUES = ("turns", "tokens", "response_time")
_USAGE_SORT_VALUES = ("tokens", "turns", "elapsed")


def _usage_days_arg(args: dict, default: int = 30):
    """`days` 引数を検証する。省略時は `default`。整数化できない、または0以下は
    説明付きの error 辞書を返す（正の整数以外は自由 SQL 的な誤用を早期に拒否する）。"""
    raw = args.get("days")
    if raw is None:
        return default
    try:
        d = int(raw)
    except (TypeError, ValueError):
        return {"error": "days は整数で指定してください"}
    if d <= 0:
        return {"error": "days は1以上で指定してください"}
    return d


def _usage_tool_call_card(name: str, args: dict) -> dict:
    """`args` のうち、そのツールが受け付ける既知キーだけを（値が None でなければ）echo する
    ——数値と id のみ（本文/タイトルを渡す引数は存在しない）。"""
    known = _USAGE_TOOL_ARG_KEYS.get(name, ())
    return {"tool": name, "args": {k: args[k] for k in known if args.get(k) is not None}}


# usage 系ツール結果のバイト予算クリップ（`_clip_utf8_bytes`/`_clip_cards`/`_finish_reader_result` が
# 他ツールで使う `tr_max_bytes` と同じ予算・`usage_chat._compact_stats_context` と同じ段階縮小の
# 流儀）。usage_* の戻り値は本文/タイトル/鍵/display_name を持たない（`sherpa/store/usage.py` の
# 不変条件）ため、間引き対象は内訳リスト（daily 系・users・by_*・conversations・detail の系列）
# だけでよく、ネストの深さや各ツールごとのキー名を問わない汎用の再帰間引きで足りる。
_USAGE_SHRINK_STAGES = (50, 20, 10, 5, 2, 1)


_USAGE_SERIES_KEYS = ("date", "week_start", "turn")


def _usage_is_series(items: list) -> bool:
    """日付/週/ターン順の時系列リストか（要素が `date`/`week_start`/`turn` キーを持つ dict）。"""
    return bool(items) and all(isinstance(x, dict) and any(k in x for k in _USAGE_SERIES_KEYS) for x in items)


def _usage_shrink_lists(value, limit: int):
    """`value` 内の全ての list を（辞書のネストを辿りながら）`limit` 件へ間引く。
    利用量順のリスト（by_*・rows・conversations）は先頭＝重い側を残し、時系列（日別・週別・
    ターン系列＝昇順で返る）は末尾＝直近側を残す（先頭切りだと古い期間だけが残り「最近の推移」を
    古いデータで答えてしまう）。"""
    if isinstance(value, dict):
        return {k: _usage_shrink_lists(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        kept = value[-limit:] if _usage_is_series(value) else value[:limit]
        return [_usage_shrink_lists(v, limit) for v in kept]
    return value


def _usage_counts_only(value):
    """段階縮小の最小段でも収まらない時の最終手段: 全ての list を件数だけに畳む
    （スカラー値はそのまま残す＝空の final にはしない・「何件あったか」だけは伝える）。"""
    if isinstance(value, dict):
        return {k: _usage_counts_only(v) for k, v in value.items()}
    if isinstance(value, list):
        return {"count": len(value)}
    return value


# 裁定（2026-09-12・返却上限は件数系の全ツールで50件・`store._TOOL_LIMIT_UPPER` と同値）。多くの
# usage_* の内訳リスト（`usage_overview` の users/by_model/conversations_top、`usage_conversations`
# の conversations 等）は既に `store.py` 側の構築時点で上位50件へ切ってある——バイト予算超過時だけ
# 効く `_USAGE_SHRINK_STAGES` に任せて構わない。一方 `usage_daily` の系列（`series`）と
# `usage_conversation_detail` の `response_time_series` は store.py 側に上限が無く、期間や
# 会話のターン数によっては50件を超えたままバイト予算内に収まってしまい、この段では素通りする
# （例: 60日分の usage_daily）。この2つのキーだけ、バイト予算より先に無条件で50件（直近側）へ
# 間引く。
_USAGE_RETURN_LIMIT = _USAGE_SHRINK_STAGES[0]
_USAGE_UNBOUNDED_SERIES_KEYS = ("series", "response_time_series")
# 利用量順（降順で返る）のトップレベル一覧＝上限超過は先頭（重い側）を残す。
_USAGE_UNBOUNDED_ROW_KEYS = ("rows",)


def _usage_limit_nested_series(value, limit: int, counter: list):
    """ネストした辞書の中の時系列リスト（`_usage_is_series`＝date/week_start/turn を持つ要素）を
    直近 `limit` 件へ間引く（概要ツールの daily／tokens.daily／downloads.daily／retention.weekly
    のように深い位置にある系列も返却上限の対象にする）。省略件数は `counter[0]` に加算する。"""
    if isinstance(value, dict):
        return {k: _usage_limit_nested_series(v, limit, counter) for k, v in value.items()}
    if isinstance(value, list) and _usage_is_series(value) and len(value) > limit:
        counter[0] += len(value) - limit
        return value[-limit:]
    return value


def _usage_apply_return_limit(result: dict, limit: int) -> dict:
    """時系列（トップレベルの `_USAGE_UNBOUNDED_SERIES_KEYS` と、ネストした date/week_start/turn
    系列＝直近側を残す）と利用量順の一覧（`_USAGE_UNBOUNDED_ROW_KEYS`＝先頭を残す）を `limit` 件へ
    間引く。該当リストが無い、またはどれも `limit` 件以下ならそのまま返す（`truncated`/
    `omitted_count` は付けない）。"""
    omitted = 0
    out = dict(result)
    for key in _USAGE_UNBOUNDED_SERIES_KEYS:
        items = out.get(key)
        if isinstance(items, list) and len(items) > limit:
            omitted += len(items) - limit
            out[key] = items[-limit:]
    for key in _USAGE_UNBOUNDED_ROW_KEYS:
        items = out.get(key)
        if isinstance(items, list) and len(items) > limit:
            omitted += len(items) - limit
            out[key] = items[:limit]
    counter = [0]
    out = _usage_limit_nested_series(out, limit, counter)
    omitted += counter[0]
    if omitted:
        out["truncated"] = True
        out["omitted_count"] = omitted
    return out


def _fit_usage_result(result: dict, max_bytes: int) -> dict:
    """usage 系ツール結果を `max_bytes` 以内に収める。予算内ならそのまま返す（`error` 辞書も無変更）。

    まず返却上限（`_USAGE_RETURN_LIMIT`=50件）を、store.py 側にまだ上限が無い時系列
    （`_usage_apply_return_limit` 参照）にバイト予算とは無関係に適用し、超えていれば
    `truncated: true` と省略件数（`omitted_count`）を明示する。
    その上でなおバイト予算を超える場合は、全内訳リストの上限を `_USAGE_SHRINK_STAGES` の順に下げ、
    収まった段で `truncated: true` を付けて返す（`omitted_count` は返却上限の超過分のみを表す・
    さらにバイト予算で追加間引きされた分はこの件数に含まない）。最小段でも収まらなければ
    `_usage_counts_only` へ落とす（同じく `truncated: true`）。
    """
    if not isinstance(result, dict) or "error" in result:
        return result
    result = _usage_apply_return_limit(result, _USAGE_RETURN_LIMIT)
    if _result_byte_size(result) <= max_bytes:
        return result
    for limit in _USAGE_SHRINK_STAGES:
        shrunk = _usage_shrink_lists(result, limit)
        if _result_byte_size(shrunk) <= max_bytes:
            shrunk["truncated"] = True
            return shrunk
    summary = _usage_counts_only(result)
    summary["truncated"] = True
    return summary


def _usage_period_args(args: dict, default: int = 30):
    """期間引数（`days` か `from`/`to`）を検証し `(days, time_from, time_to)` を返す。

    規則は管理者 API（`GET /admin/usage/stats`）と同じ: `from`/`to` を渡したときは `days` と排他
    （明示指定の併用はエラー）・日時の妥当性（オフセット必須・両方必須・順序・上限）は store 側の
    `_usage_period` が判定する。検証に落ちたら説明付きの error 辞書を返す（他のツール引数検証と同じ形）。
    """
    time_from, time_to = args.get("from"), args.get("to")
    if time_from is None and time_to is None:
        days = _usage_days_arg(args, default=default)
        return days if isinstance(days, dict) else (days, None, None)
    if args.get("days") is not None:
        return {"error": "days と from/to は同時に指定できません"}
    return (default, time_from, time_to)


def _call_usage_store(fn, *a, **kw) -> dict:
    """`store.usage_*` を呼び、期間規則違反（`UsagePeriodError`）は error 辞書へ写す。"""
    from . import store
    try:
        return fn(*a, **kw)
    except store.UsagePeriodError as e:
        return {"error": str(e)}


def _run_usage_tool(name: str, args: dict, tool_result_max_bytes: int) -> tuple[dict, set, list, list]:
    from . import store
    args = args or {}
    card = [_usage_tool_call_card(name, args)]

    def _done(out: dict) -> tuple[dict, set, list, list]:
        if "error" in out:
            return (out, set(), [], [])
        return (_fit_usage_result(out, tool_result_max_bytes), set(), [], card)

    if name == "usage_conversation_detail":
        result = _fit_usage_result(
            store.usage_conversation_detail(args.get("conversation_id")), tool_result_max_bytes)
        return (result, set(), [], card)
    if name in ("usage_response_time", "usage_daily"):
        # 期間指定は days のみ（`from`/`to` は日別系列・回答時間には足していない）。
        days = _usage_days_arg(args)
        if isinstance(days, dict):
            return (days, set(), [], [])
        if name == "usage_response_time":
            result = _fit_usage_result(
                store.usage_response_time(days, provider=args.get("provider")), tool_result_max_bytes)
            return (result, set(), [], card)
        metric = args.get("metric") or "turns"
        if metric not in _USAGE_METRIC_VALUES:
            return ({"error": f"metric は {'/'.join(_USAGE_METRIC_VALUES)} のいずれかで指定してください"},
                    set(), [], [])
        result = _fit_usage_result(store.usage_daily(days, metric=metric), tool_result_max_bytes)
        return (result, set(), [], card)

    # 以下は期間を days でも from/to でも指定できるツール（規則は管理者 API と同じ）。
    period = _usage_period_args(args, default=7 if name == "usage_by_user" else 30)
    if isinstance(period, dict):
        return (period, set(), [], [])
    days, time_from, time_to = period
    # `days` 指定のときは期間キーワードを渡さない（既存呼び出し形のまま＝store 側の既定と同じ）。
    pkw = {} if time_from is None and time_to is None else {"time_from": time_from, "time_to": time_to}
    if name == "usage_overview":
        return _done(_call_usage_store(store.usage_overview, days, **pkw))
    if name == "usage_by_user":
        return _done(_call_usage_store(store.usage_by_user, days, uid=args.get("uid"),
                                       kind=args.get("kind"), **pkw))
    if name == "usage_conversations":
        sort = args.get("sort") or "tokens"
        if sort not in _USAGE_SORT_VALUES:
            return ({"error": f"sort は {'/'.join(_USAGE_SORT_VALUES)} のいずれかで指定してください"},
                    set(), [], [])
        return _done(_call_usage_store(store.usage_conversations, days, uid=args.get("uid"),
                                       limit=args.get("limit") or 20, sort=sort, **pkw))
    if name == "usage_stop_kinds":
        return _done(_call_usage_store(store.usage_stop_kinds, days, uid=args.get("uid"), **pkw))
    # usage_depth_rounds
    return _done(_call_usage_store(store.usage_depth_rounds, days, **pkw))


# ---- 思考ノード（agents.py に依存しない＝循環回避）----

_seq = [0]


def _nid() -> str:
    _seq[0] += 1
    return f"as-{_seq[0]}"


def _node(label: str, detail: str) -> dict:
    return {"type": "node", "id": _nid(), "kind": "tool", "label": label, "detail": detail, "status": "done"}


def _context_compacted_node(n: int) -> dict:
    """C2（探索ループの文脈整理）: 古いツール往復を `InvestigationState.render()` の要約1通へ
    置換したことを示す think ノード（本モジュールの他ノードは検索・確認の「行動」＝kind="tool"
    固定の `_node` を使うが、これは調査そのものではなく文脈整理という「考える」操作のため
    kind="think" にする）。
    """
    return {"type": "node", "id": _nid(), "kind": "think", "label": "調査の文脈を整理",
           "detail": f"古いツール結果 {n} 件を調査状態の要約に置換", "status": "done"}


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


def _list_docs_target(args: dict | None) -> str:
    """list_docs の思考ノード表示用の対象名。絞り込み軸（path_prefix/name_pattern/doctype/state）が
    1つでも立っていればそれを併記し、無条件のときだけ「全体」にする（絞り込み後の件数を
    範囲全体の件数として見せない）。"""
    a = args or {}
    base = _clip(a.get("path_prefix") or a.get("name_pattern"), 60)
    conds = [f"種別={_clip(a.get('doctype'), 30)}" if a.get("doctype") else None,
             f"状態={_clip(a.get('state'), 30)}" if a.get("state") else None]
    conds = [c for c in conds if c]
    if base:
        return f"{base}（{'・'.join(conds)}）" if conds else base
    return f"全体（{'・'.join(conds)}）" if conds else "全体"


def _tool_node(name: str, args: dict) -> dict:
    if name == "list_docs":
        return _node("資料の一覧を確認", f"「{_list_docs_target(args)}」")
    if name == "folder_tree":
        a = args or {}
        return _node("フォルダ構成を確認", f"「{a.get('path_prefix') or '全体'}」")
    if name == "ripgrep_search":
        return _node("資料を検索（語句そのまま）", f"「{(args or {}).get('query', '')}」")
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
    if name in _ORIGINAL_READ_LABELS:
        return _node(_ORIGINAL_READ_LABELS[name], f"{(args or {}).get('doc_id', '')}")
    if name == "ask_user":
        return _node("ユーザに確認", (args or {}).get("prompt", "確認が必要です"))
    return _node(name, "")


# 原本読取ツール: ツール名→表示ラベル。`xlsx_sheets`（シート名・大きさだけを
# 見る＝本文精読ではない）は `xlsx_range`（セルの中身そのものを読む）と別ラベルに分ける——
# 同じ「原本を読む（Excel）」に丸めていると、シート一覧を確認しただけのターンも改善ログの
# `files_read`（本文を実際に読んだ数）に誤って数えられてしまう。Codex 側
# （`providers/codex/provider.py` の tlabel 辞書）・改善ログ（`improvement_log._TOOL_CALL_LABELS`/
# `_FILES_READ_LABEL`）と同じ文言を共有する。
_ORIGINAL_READ_LABELS = {
    "xlsx_sheets": "原本のシート一覧を確認",
    "xlsx_range": "原本を読む（Excel）",
    "docx_paragraphs": "原本を読む（Word）",
    "pptx_slides": "原本を読む（PowerPoint）",
    "pdf_pages": "原本を読む（PDF）",
    "file_head": "原本を読む（先頭）",
}


# ツール名 → (label, detail) の固定文言（引数を一切埋め込まない）。
_SUB_TOOL_FIXED_WORDING = {
    "list_docs": ("資料の一覧を確認", "資料の一覧を確認しています"),
    "folder_tree": ("フォルダ構成を確認", "フォルダ構成を確認しています"),
    "ripgrep_search": ("資料を検索（語句そのまま）", "資料を検索しています"),
    "glob_search": ("ファイル名で検索", "ファイル名で検索しています"),
    "es_search": ("資料を検索（全文/日本語）", "資料を検索しています"),
    "read_around": ("該当箇所を精読", "該当箇所を精読しています"),
    "read_doc": ("文書を通読", "文書を通読しています"),
    "doc_outline": ("見出し構造を確認", "見出し構造を確認しています"),
    "graph_neighbors": ("関係グラフをたどる", "関連部品をたどっています"),
    "compare_documents": ("世代間の差分を比較", "世代間の差分を比較しています"),
    "xlsx_sheets": ("原本のシート一覧を確認", "原本（Excel）のシート一覧を確認しています"),
    "xlsx_range": ("原本を読む（Excel）", "原本（Excel）を読んでいます"),
    "docx_paragraphs": ("原本を読む（Word）", "原本（Word）を読んでいます"),
    "pptx_slides": ("原本を読む（PowerPoint）", "原本（PowerPoint）を読んでいます"),
    "pdf_pages": ("原本を読む（PDF）", "原本（PDF）を読んでいます"),
    "file_head": ("原本を読む（先頭）", "原本（先頭）を読んでいます"),
    "ask_user": ("ユーザに確認", "確認しています"),
}


def _tool_node_sub(name: str) -> dict:
    """サブ経路専用のツールノード（ローカルサブの生成物が公式 UI/trace に露出するのを防ぐ）。

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


# `es_search` の tool result に載る `degrade_reason`（`es_index.search()` の reason）→
# 固定文言。BM25（キーワード一致）の結果は継続利用しつつ、精度が一部落ちていることを
# 「思考の流れ」に決定的に表示する（サーバログの warning だけでは利用者に届かない）。
# 語彙は `es_query_failed`（hits 自体が空になる BM25 自体の失敗）を含まない——BM25 の結果を
# そのまま使えている場合（hits が空でない）だけを対象にする（`es_index.search()` docstring 参照）。
_ES_DEGRADE_WORDING = {
    "embedding_cloud_unavailable": ("検索の精度が一部低下しています",
                                    "選択中の AI での意味検索が使えないため、キーワード一致のみで探しています"),
    "query_embed_failed": ("検索の精度が一部低下しています",
                           "検索語の変換が一時的に失敗したため、キーワード一致のみで探しています"),
    # hybrid クエリ自体の失敗（次元不一致/未ベクトル索引等）で
    # BM25 のみへ降格した場合＝`query_embed_failed`（クエリ埋め込み自体が失敗）とは別原因だが、
    # 利用者向けの案内文は同じでよい（どちらも「意味検索は使えず、キーワード一致のみ」という
    # 結果は同じ）。
    "hybrid_query_failed": ("検索の精度が一部低下しています",
                           "意味検索の問い合わせが一時的に失敗したため、キーワード一致のみで探しています"),
    # 索引の埋め込み素性と現在の AI 設定が合わない（設定変更後の再取り込み待ち）。一時障害ではなく
    # 再取り込みまで続く状態なので、案内文で「取り込みのやり直し」まで示す。
    "vector_feature_mismatch": ("検索の精度が一部低下しています",
                                "取り込んだ資料が現在の AI 設定では意味検索に使えないため、キーワード一致のみで探しています"
                                "（資料の取り込みをやり直すと戻ります）"),
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
    "ripgrep_search": "検索結果（語句そのまま）",
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

    `es_search` は `degrade_reason` が `_ES_DEGRADE_WORDING`（BM25 継続時の語彙）に含まれない
    既知値（`es_unavailable`/`es_query_failed`＝BM25 自体も失敗し hits が強制的に空になっている）
    のときも None にする——「検索は実行できたが0件だった」ことにはならないため、件数ノードで
    「0件（キーワード一致のみ）」と出すと実際には検索していないのに検索したかのような誤表示になる。
    固定理由コード（`error_code`・`_record_tool_result_error_code` が拾うのと同じ語彙）が付いた
    結果も同じ理由で None にする——`graph_neighbors` が内部で障害を握りつぶして `{"neighbors": []}`
    を返す場合（"error" キーは持たない）等、「実行できなかった」を「0件ヒット」と混同しない。
    """
    if not isinstance(result, dict) or "error" in result or result.get("error_code"):
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
        diff_lines = (result.get("diff") or "").splitlines()
        # 先頭2行（`difflib.unified_diff` が出す `--- fromfile`/`+++ tofile`）だけを位置で
        # ヘッダーとして除外する——内容が偶然 "+++"/"---" で始まる変更行（3行目以降）まで誤って
        # 除外しない（`investigation_state.py` の compare_documents 抜粋と同じ契約）。
        body_lines = diff_lines[2:] if len(diff_lines) >= 2 else []
        return sum(1 for ln in body_lines if ln.startswith("+") or ln.startswith("-"))
    return None


# search_truncated（利用統計「打ち切りの内訳」対象）: 検索系ツール（探す経路・列挙系）が母集団の
# 一部しか返さなかった呼び出し。`result["truncated"]`（各ツールの既存語彙・ヒット上限／件数上限/
# 見出し上限で打ち切り）をそのまま数える——制限そのものは変えない・計測のみ。
_SEARCH_TRUNCATED_TOOLS = frozenset({
    "ripgrep_search", "es_search", "glob_search", "graph_neighbors", "list_docs", "doc_outline"})
# tool_result_clipped（同）: 1 件あたりのバイト予算で本文が切り詰められた呼び出し。判定キーは
# `text_truncated`（read_around/read_doc の逐次クリップ）と `byte_clipped`（`_finish_reader_result`
# の原本読取ツール・compare_documents の diff がバイト予算で切ったときだけ立てる印）。読取ツールの
# `truncated` は行数/ページ数の上限でも立つため使わない。件数上限の doc_outline/graph_neighbors/
# 検索系は `search_truncated` で数える。
_BYTE_CLIP_TOOLS = frozenset({
    "read_around", "read_doc", "xlsx_sheets", "xlsx_range", "docx_paragraphs", "pptx_slides",
    "pdf_pages", "file_head", "compare_documents"})


def _record_run_tool_limits(state: "investigation_state.InvestigationState", name: str, result) -> None:
    """`run_tool()` 直後に1回呼ぶ（3 dialect 共通）。制限そのものは一切変えない・計測のみ。"""
    if not isinstance(result, dict):
        return
    if name in _SEARCH_TRUNCATED_TOOLS and result.get("truncated"):
        state.bump_limit("search_truncated")
    if name in _BYTE_CLIP_TOOLS and (result.get("text_truncated") or result.get("byte_clipped")):
        state.bump_limit("tool_result_clipped")


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
    埋め込む＝メイン経路の既存の豊かな表示方針のまま。長い query は
    `_clip` で60字に丸める（UI 側の折返し/幅対策）。
    """
    n = _tool_hit_count(name, result)
    label = _HIT_SUMMARY_LABELS.get(name)
    if n is None or label is None:
        return None
    args = args or {}
    if name == "ripgrep_search":
        tail = "（上限で打ち切り・全件ではない）" if result.get("truncated") else ""
        return _hit_summary_dict(label, f"「{_clip(args.get('query'), 60)}」→ {n}件{tail}")
    if name == "glob_search":
        return _hit_summary_dict(label, f"「{_clip(args.get('pattern'), 60)}」→ {n}件")
    if name == "es_search":
        # 縮退表示自体は `_degrade_result_node`（既存・別ノード）が変わらず担う——ここでは
        # 「実際に使われた検索方式」を短く添えるだけ（degrade_reason は BM25 継続時の
        # 縮退理由＝立っていれば必ずキーワード一致のみになっている）。
        mode = "キーワード一致のみ" if result.get("degrade_reason") else "全文/意味検索"
        tail = "・上限で打ち切り" if result.get("truncated") else ""
        return _hit_summary_dict(label, f"「{_clip(args.get('query'), 60)}」→ {n}件（{mode}{tail}）")
    if name == "graph_neighbors":
        tail = (f"（上限で打ち切り・全 {result.get('count')} 件）" if result.get("truncated") else "")
        return _hit_summary_dict(label, f"「{_clip(args.get('name'), 60)}」の関連部品 → {n}件{tail}")
    if name == "list_docs":
        return _hit_summary_dict(label, f"「{_list_docs_target(args)}」→ {n}件")
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
        tail = ("（読み切れていない・件数は過小）" if result.get("file_truncated")
                else "（上限で打ち切り・一部）" if result.get("truncated") else "")
        return _hit_summary_dict(label, f"{_clip(args.get('doc_id'), 60)} → 見出し{n}件{tail}")
    if name == "compare_documents":
        left = args.get("left_doc_id") or args.get("source_doc_id")
        right = args.get("right_doc_id") or args.get("target_generation")
        tail = "（上限で打ち切り・一部）" if result.get("truncated") else ""
        return _hit_summary_dict(label, f"{_clip(left, 60)} / {_clip(right, 60)} → 変更{n}行{tail}")
    return None


def _hit_summary_node_sub(name: str, result: dict) -> dict | None:
    """サブ経路（`allowed_tools is not None`）向け: `_tool_node_sub` と同じく、モデル生成の
    引数（query/doc_id 等）は一切使わない固定文言＋件数のみ。件数は
    run_tool の結果から数えた整数であり、モデル生成の自由文字列ではないため安全に出せる。
    """
    n = _tool_hit_count(name, result)
    label = _HIT_SUMMARY_LABELS.get(name)
    if n is None or label is None:
        return None
    if name == "read_around":
        detail = f"{n}行読み込みました"
    elif name == "read_doc":
        # 固定文言＋数値のみ: start_line/end_line/total_lines は
        # モデル生成の自由文字列ではなく run_tool が検証・算出した整数のため安全に出せる。
        detail = f"{result.get('start_line')}〜{result.get('end_line')}行を読了（全{result.get('total_lines')}行）"
    elif name == "doc_outline":
        detail = f"見出し{n}件" + ("（読み切れていない・件数は過小）" if result.get("file_truncated")
                                 else "（上限で打ち切り・一部）" if result.get("truncated") else "")
    elif name == "compare_documents":
        detail = f"変更{n}行を確認しました" + ("（上限で打ち切り・一部）" if result.get("truncated") else "")
    elif name == "graph_neighbors" and result.get("truncated"):
        detail = f"{n}件ヒットしました（上限で打ち切り・全 {result.get('count')} 件）"
    elif name in ("ripgrep_search", "es_search") and result.get("truncated"):
        detail = f"{n}件ヒットしました（上限で打ち切り・全件ではない）"
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
    """応答タイムアウトか（判定の実装は `stop_kind.is_timeout_exc` を唯一の真実源として使う）。
    タイムアウトは上流（プロバイダ側）で処理/課金が既に進んでいる可能性があり、
    再試行すると二重送信・二重課金になり得るため非リトライの全体契約とする（`_retryable_post_error`・
    `_run_evaluation` の両方が本関数を使う）。"""
    return stop_kind.is_timeout_exc(exc)


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


# ---- トークン使用量の合算（ツールループの全ターン分＝メイン回答呼び出し合計） ----
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

    `es_index.available()` と対称に**実接続**を確認する（SC-6e）——URI の有無だけを見ると、
    `world_neo4j.default_neo4j_uri()` が未設定時も `bolt://localhost:7687` へフォール
    バックして常に非空文字列を返すため、Neo4j 未起動でも常に True になってしまう
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
             "「詳細」で使う検索のグラフをONにしてください。",
    "troubleshoot": "トラブルシュートはグラフ検索が必要です（現在OFFまたは利用できません）。"
                   "「詳細」で使う検索のグラフをONにしてください。",
}
_TOOLS_BLOCKED_HEADLINE_DEFAULT = ("資料の「使う検索」がすべてOFF/利用できません"
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
            "agentic_failure": "error",   # 実行できなかったターン＝終了理由の分布で完了扱いにしない
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


def _card_edges_view(card: dict) -> list:
    """1件の `graph_neighbors` card が持つ代表経路の辺（`evidence.edges`・`lens_service.neo4j_related`
    が返す `{type, from, to, doc}`）を、LLM 向け `view` 用に既知キーだけ写して返す。`doc` は検証済み集合にある KB 内 rel_path だけ出す
    （個人 workspace は RAG/グラフの対象外のためここに来ない）。壊れた・古い形（`from`/`to` 無し）の
    辺があっても落とさず、あるキーだけ拾う。
    """
    ev = card.get("evidence", {}) or {}
    verified = card.get("_verified_doc_ids")
    out = []
    for e in ev.get("edges", []) or []:
        if not isinstance(e, dict):
            continue
        item = {k: e[k] for k in ("type", "from", "to", "doc") if e.get(k)}
        # 裏付け doc を主張する辺で、検証済み集合（実在・文書種別・範囲）に無い doc の辺は、doc を
        # 落として `unverified` を立てる（辺そのものを消すと経路が繋がって見えて確定根拠に化ける・
        # 実在しない原本は名指しさせない）。
        if item.get("doc") and verified is not None and item["doc"] not in set(verified):
            item.pop("doc", None)
            item["unverified"] = True
        if item:
            out.append(item)
    return out


def _edges_text(edges: list) -> list[str]:
    """辺の列 → 「from →TYPE→ to」の文字列列（Evidence digest・探索状態の要約用）。"""
    out = []
    for e in edges or []:
        if isinstance(e, dict) and e.get("type") and e.get("from") and e.get("to"):
            out.append(f"{e['from']} →{e['type']}→ {e['to']}" + ("（未確認）" if e.get("unverified") else ""))
    return out


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
    _safe_card_meta` の allowlist（name/role/category/path/edges）には含まれないため、chat 側の
    公開経路（`data.candidates`）には出ない。
    """
    out = []
    for c in cards:
        card_meta = {"name": c.get("name", ""), "role": c.get("role", ""),
                    "category": c.get("category", ""), "path": c.get("path", []),
                    "label": c.get("label", ""),
                    # 辺の種類と向き（検証落ちの doc は落として「（未確認）」付き・A→INVOKES→B と逆向きを区別して要約へ引き継ぐ）
                    "edges": _edges_text(_card_edges_view(c))}
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
# digest／帰属用回答コピーは**ツール結果と同じ露出**で組む（設計簡素化）——生 doc_id・
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


# 複数項目を列挙する区切り記号——末尾に半角空白を含める。`build_evidence_digest`/
# `build_synthesis_digest` はどちらも列挙済みの1行を後で（citation 側の quote と混ぜた
# 全体 fact 文字列などへ）埋め込んでから `_digest_clean` を再適用する箇所があり、空白の無い
# 区切りだと `_KV_SECRET_RE` の `\S+`（空白でしか止まらない）が区切り記号ごと次の項目まで
# 飲み込み、`key=value` 形の秘密を含む項目の直後の項目が丸ごと消える
# （`sherpa/investigation_state.py` の `_LIST_SEP` と同じ理由・同じ対処）。
_LIST_SEP = "、 "

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
                             f"name_pattern={_digest_clean(lm['pattern'])}" if lm.get("pattern") else None,
                             f"doctype={_digest_clean(lm['doctype'])}" if lm.get("doctype") else None,
                             f"state={_digest_clean(lm['state'])}" if lm.get("state") else None]
                cond = _LIST_SEP.join(c for c in cond_parts if c)
                cond_text = f"（条件: {cond}）" if cond else ""
                paths = _LIST_SEP.join(_digest_clean(d) for d in matched[:10])
                fact = (f"[list_docs] 該当 {lm.get('count', 0)} 件{cond_text}／列挙 "
                       f"{lm.get('shown', 0)} 件" + (f": {paths}" if paths else ""))
            elif "tree_meta" in m:
                # folder_tree の構造 Evidence（`matched_doc_ids` は常に
                # 空・裏付け doc 無し）。list_docs と同じ「条件＋件数」の事実整形。
                tm = m.get("tree_meta") or {}
                cond_text = f"（path_prefix={_digest_clean(tm['prefix'])}）" if tm.get("prefix") else ""
                fact = (f"[folder_tree] 深さ{tm.get('depth')}{cond_text}／該当フォルダ "
                       f"{tm.get('count', 0)} 件／列挙 {tm.get('shown', 0)} 件")
            else:
                cm = m.get("card_meta") or {}
                docs_text = _LIST_SEP.join(_digest_clean(d) for d in matched[:5])
                fact = (f"[graph] {_digest_clean(cm.get('name', ''))}"
                       f"（{_digest_clean(cm.get('role', ''))}"
                       f"{'・' + _digest_clean(cm['category']) if cm.get('category') else ''}"
                       f"・経路={_digest_clean(str(cm.get('path') or ''))}"
                       f"{'・辺=' + _digest_clean('、'.join(cm['edges'])) if cm.get('edges') else ''}）"
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


# ---- 清書入力: 確定根拠の全件ダイジェスト —— 下調べ役が集めた根拠を、清書
# （`_answer_prompt` → `providers/prompts.py::_facts`）が QA の先頭4引用×60字に絞らず全件参照
# できるようにする。`build_evidence_digest`（帰属専用・60字・60行上限）とは目的・上限が異なる
# 別関数（`build_evidence_digest` 自体はここでは変更しない）——ev-N の採番・入力順だけを揃える。

_SYNTHESIS_QUOTE_CAP = 400            # 清書ダイジェストの quote 切り詰め長（帰属用 digest の60字とは別契約）
# 清書ダイジェスト全体のバイト数上限（最終 UTF-8 列で厳密判定）。メイン＝クラウド GPT を前提に既定 256KiB
# （日本語約 8.7 万字・6〜9 万トークン）。精読 1 件の保存上限（`investigation_state._READ_TEXT_CAP_BYTES`
# ＝この 1/4）と査読入力（`providers/base.py`＝この 1/2）はここから比例する。メインをローカル LLM に
# する構成では運用側が下げる（`SHERPA_AGENTIC_SYNTHESIS_BUDGET_BYTES`）。
_SYNTHESIS_MAX_BYTES = _env_int("SHERPA_AGENTIC_SYNTHESIS_BUDGET_BYTES", 256 * 1024, 8 * 1024, 4 * 1024 * 1024)
_SYNTHESIS_TRUNCATION_NOTICE_TMPL = "（他 {n} 件は省略）"
_SYNTHESIS_GAPS_MAX_ITEMS = 20         # 清書ダイジェストへ渡す「調査の限界」の件数上限（先頭優先）
_SYNTHESIS_GAP_CAP = 200              # 「調査の限界」1件あたりの切り詰め長


def _synthesis_quote(text: str, cap: int) -> str:
    """`_digest_clean` を通してから `cap` 文字まで切り詰める。切り詰めが発生したときだけ末尾に
    「…」を付ける（`build_evidence_digest` の quote 切断は個別注記が無い契約だが、清書ダイジェストは
    「全件ダイジェスト」の契約上、個々の切断も打ち切りも明示する）。
    """
    cleaned = _digest_clean(text)
    return cleaned if len(cleaned) <= cap else cleaned[:cap] + "…"


def _synthesis_span_loc(span) -> str:
    """citation の `span`（`[start_line, end_line]`）から「 行 a-b」を組む。span が無い/行番号を
    持たない（rag_chunks 由来等）ときは空文字（doc_id だけの表示に落ちる）。
    """
    if (isinstance(span, (list, tuple)) and len(span) == 2
            and isinstance(span[0], int) and not isinstance(span[0], bool)
            and isinstance(span[1], int) and not isinstance(span[1], bool)):
        return f" 行 {span[0]}-{span[1]}"
    return ""


def _synthesis_list_path_budgets(rows: dict, total_bytes: int) -> dict:
    """一覧行ごとのパス予算を公平配分する（{行index: 予算バイト}）。

    各行の必要量（全パスを連結した UTF-8 バイト）を昇順に見て、残り予算を残り行数で等分した枠に
    収まる行は必要量だけ与え、収まらない行は枠を与える（余りは次の行の枠に回る）。合計は
    `total_bytes` 以内。"""
    sep = len(_LIST_SEP.encode("utf-8"))
    need = {}
    for i, matched in rows.items():
        pieces = [len(_digest_clean(d).encode("utf-8", errors="replace")) for d in matched]
        need[i] = sum(pieces) + sep * max(0, len(pieces) - 1)
    budgets: dict = {}
    remaining = max(0, total_bytes)
    order = sorted(need, key=lambda i: need[i])
    for k, i in enumerate(order):
        share = remaining // (len(order) - k)
        budgets[i] = min(need[i], share)
        remaining -= budgets[i]
    return budgets


def _synthesis_list_paths(matched: list, budget_bytes: int) -> tuple[str, int]:
    """list_docs のパス一覧を UTF-8 で `budget_bytes` 以内に収まるところまで連結し、省略した件数を返す。"""
    out: list = []
    used = 0
    for d in matched:
        piece = _digest_clean(d)
        cost = len(piece.encode("utf-8", errors="replace")) + (len(_LIST_SEP.encode("utf-8")) if out else 0)
        if used + cost > budget_bytes:
            break
        out.append(piece)
        used += cost
    return _LIST_SEP.join(out), len(matched) - len(out)


def build_synthesis_digest(citations: list, combined_evidence_meta: list, *,
                           quote_cap: int = _SYNTHESIS_QUOTE_CAP,
                           max_bytes: int = _SYNTHESIS_MAX_BYTES,
                           read_evidence: list | None = None,
                           gaps: list | None = None) -> tuple[str, dict, bool]:
    """清書（`_answer_prompt` → `providers/prompts.py::_facts`）専用の**確定根拠の全件ダイジェスト**。

    戻り値の3つ目（`synthesis_truncated`）: `max_bytes` 予算を超えて候補（citation/構造的根拠/精読
    本文/gaps 行）を1件以上省略したら True（利用統計「打ち切りの内訳」計測専用・予算自体は変えない）。

    `build_evidence_digest` と**同じ ev-N 採番・同じ入力順**（`combined_evidence_meta` の
    添字＋1）を使う——同じ入力を渡せば同じエントリに同じ ev-N が付く（両関数の唯一の共通契約。
    件数上限は持たない＝`_ATTRIBUTION_MAX_ITEMS` 相当の頭打ちをしない）。

    citation エントリは `ev-N: doc_id 行 a-b「quote」`（`quote_cap` 文字まで・超過時は「…」で
    明示）。統合で消えなかった別の一致（`evidence_meta[i]["extra_quotes"]`・
    `citations.merge_overlapping_citations` が積む）があれば「／別の一致: 「…」」を追記する。
    list_docs 集計・graph カードのエントリは `build_evidence_digest` と同じ体裁。ただし文書パスは
    清書側だけ全件（`max_bytes` の半分を全一覧行で公平配分した予算まで・超過分は「他 n 件のパスは
    省略」と明示。帰属用の
    `build_evidence_digest` は先頭10件のまま）・裏付け doc 先頭5件は共通——`quote_cap` はこれらには適用しない。

    `read_evidence`（省略可・既定 None＝空・C 追加）: ハイブリッドの下調べ役／査読が実際に
    read_around／read_doc・S3b 原本読取ツールで読んだ本文（`InvestigationState` の kind="read"
    Evidence・`{"doc_id","span","text","locator","text_truncated"}` の辞書列。glob_search／doc_outline／
    compare_documents の要約行（`kind`＝"list"/"outline"/"compare"・`source_tool` 付き・`doc_id` 無しあり）も
    同じ列に混じり、そのまま 1 行の内部専用行になる。精読本文は呼び出し元が
    既に `_redact`・`investigation_state._READ_TEXT_CAP_BYTES`（清書ダイジェスト予算
    `_SYNTHESIS_MAX_BYTES` の1/4）まで切り詰め済み）。citation／構造的根拠のダイジェスト行に
    **続けて**「精読: doc_id 行 a-b「本文」」行を追加する——`quote_cap` は適用しない（精読本文は
    既に保存側の上限で切り詰め済みで、citation の400字上限とは別契約のため二重に切り詰めない）。
    `text_truncated` が真の行は末尾に「（末尾未保持）」を付け、保存時点で本文の
    末尾が落ちた事実を清書入力自体に明示する（黙って全件性を主張しない）。**同じ `max_bytes`
    予算・同じ打ち切り注記**（件数へ合算）を共有するが、Evidence Packet／`data.citations` には
    出さない内部専用行のため `ev_map` には登録しない（`ev-N` を割り当てない＝攻撃的な幻覚 ev-N
    と衝突しない）。

    `gaps`（省略可・既定 None＝空）: `InvestigationState.gaps`（検索0件／打ち切り／
    未確認という調査の限界・機械生成の文字列列）。引用・構造行より前（先頭）に「調査の限界: …」行を
    末尾優先 `_SYNTHESIS_GAPS_MAX_ITEMS`（20）件（重複除去）・各 `_SYNTHESIS_GAP_CAP`（200字）まで追加する——
    同じ `max_bytes` 予算・打ち切り注記を共有し、`ev_map` には登録しない（`read_evidence` と同じ
    内部専用行）。gap の文字列自体は呼び出し元が既に `_digest_clean` 済みの前提だが、ここでも
    `_digest_clean` を通す（二重適用は無害・唯一の redaction 境界を貫く）。

    `max_bytes`（最終 `"\\n".join(lines)` の UTF-8 バイト数）を超える分は**根拠単位**で末尾から
    打ち切り、末尾に `"（他 M 件は省略）"` を付ける（M＝実際に省略した件数。注記自身の追加
    バイトも上限に含めて判定するため、注記の桁数が変わるたびに数え直す）。

    戻り値 `(digest_text, ev_map)` は `build_evidence_digest` と同じ形（`ev_map` は
    `{"ev-N": [doc_id, ...], ...}`）。根拠が1件も無ければ `digest_text == ""`。
    """
    n_citations = len(citations)
    # [(ev_id, line, matched_doc_ids), ...]（バイト上限判定より前の全件）。`read_evidence` 由来の
    # 行は `ev_id=None`（Evidence Packet／帰属に使わない内部専用行の印・下の2ループが `ev_map` へ
    # 触れない条件として使う）。
    candidates: list = []
    # 限界（gaps）は先頭に積む——予算は末尾から切るため、引用や精読本文が長いターンでも
    # 「0件」「検証で除外」「保存時に切断」「上限到達で中断」が清書入力から落ちない。件数上限は
    # 末尾優先（後半の run で積まれる中断・切断ほど清書に要る・重複は落とす）。
    # 「検証で除外」（citation 単位・件数が多い）は 1 行に畳み、打ち切り・中断・0件などの限界に枠を
    # 譲る——一括で末尾に積まれる除外行が 20 件枠を占有して本物の打ち切り限界を押し出さないため。
    _seen_gaps: set = set()
    _gap_lines: list = []
    _excluded: list = []
    for g in reversed(list(gaps or [])):
        gap_text = _digest_clean(str(g or ""))[:_SYNTHESIS_GAP_CAP]
        if not gap_text or gap_text in _seen_gaps:
            continue
        _seen_gaps.add(gap_text)
        if ": 検証で除外（" in gap_text:
            _excluded.append(gap_text)
            continue
        _gap_lines.append(gap_text)
    if _excluded:
        # 文書名は載せない——清書・クリーン再合成の入力に落ちた根拠の名前を持ち込むと、その資料を
        # 引用する誘因になる（クリーンな合成コンテキストの契約）。件数と扱いだけを伝える。
        _gap_lines = _gap_lines[:_SYNTHESIS_GAPS_MAX_ITEMS - 1]
        _gap_lines.insert(0, f"検証で除外した引用 {len(_excluded)} 件（原本で確認できなかった＝根拠にしない）")
    else:
        _gap_lines = _gap_lines[:_SYNTHESIS_GAPS_MAX_ITEMS]
    for gap_text in reversed(_gap_lines):
        candidates.append((None, _digest_clean(f"調査の限界: {gap_text}"), None))
    # 一覧行のパスに使える予算＝max_bytes の半分を全一覧行で公平配分（必要量の少ない行は全部載せ、
    # 余りを残りの行で等分＝先着独占も後続の余りの取りこぼしも無い）。残り半分は他の根拠に残す。
    path_budgets = _synthesis_list_path_budgets(
        {i: m.get("matched_doc_ids") for i, m in enumerate(combined_evidence_meta)
         if m.get("matched_doc_ids") is not None and "list_meta" in m}, max_bytes // 2)
    paths_omitted = False                            # 一覧のパスが予算で未提示になったか（計測用）
    for i, m in enumerate(combined_evidence_meta):
        ev_id = f"ev-{i + 1}"
        matched = m.get("matched_doc_ids")
        if matched is not None:
            if "list_meta" in m:
                lm = m.get("list_meta") or {}
                cond_parts = [f"path_prefix={_digest_clean(lm['prefix'])}" if lm.get("prefix") else None,
                             f"name_pattern={_digest_clean(lm['pattern'])}" if lm.get("pattern") else None,
                             f"doctype={_digest_clean(lm['doctype'])}" if lm.get("doctype") else None,
                             f"state={_digest_clean(lm['state'])}" if lm.get("state") else None]
                cond = _LIST_SEP.join(c for c in cond_parts if c)
                cond_text = f"（条件: {cond}）" if cond else ""
                # 清書入力は全件を載せる（先頭 10 件の打ち切りは帰属用 digest だけ）。ただし一覧行だけで
                # 予算を食い潰すと件数・条件ごと落ちるため、パスは行の取り分（等分＋繰り越し）までで
                # 打ち切り、省略数を明示する。
                paths, n_omitted = _synthesis_list_paths(matched, path_budgets.get(i, 0))
                if n_omitted:
                    paths_omitted = True            # パス一覧の未提示も清書入力の打ち切り（計測用）
                fact = (f"[list_docs] 該当 {lm.get('count', 0)} 件{cond_text}／列挙 "
                       f"{lm.get('shown', 0)} 件" + (f": {paths}" if paths else "")
                       + (f"（他 {n_omitted} 件のパスは未提示＝この一覧は全件として書かない）" if n_omitted else ""))
            elif "tree_meta" in m:
                tm = m.get("tree_meta") or {}
                cond_text = f"（path_prefix={_digest_clean(tm['prefix'])}）" if tm.get("prefix") else ""
                fact = (f"[folder_tree] 深さ{tm.get('depth')}{cond_text}／該当フォルダ "
                       f"{tm.get('count', 0)} 件／列挙 {tm.get('shown', 0)} 件")
            else:
                cm = m.get("card_meta") or {}
                docs_text = _LIST_SEP.join(_digest_clean(d) for d in matched[:5])
                fact = (f"[graph] {_digest_clean(cm.get('name', ''))}"
                       f"（{_digest_clean(cm.get('role', ''))}"
                       f"{'・' + _digest_clean(cm['category']) if cm.get('category') else ''}"
                       f"・経路={_digest_clean(str(cm.get('path') or ''))}"
                       f"{'・辺=' + _digest_clean('、'.join(cm['edges'])) if cm.get('edges') else ''}）"
                       + (f"／裏付け: {docs_text}" if docs_text else ""))
            candidates.append((ev_id, _digest_clean(f"{ev_id}: {fact}"), list(matched)))
            continue
        doc_id = m.get("doc_id")
        if not doc_id:
            continue
        if i < n_citations:
            c = citations[i]
            quote = _synthesis_quote(c.get("quote") or "", quote_cap)
            loc = _synthesis_span_loc(c.get("span"))
            fact = f"{_digest_clean(doc_id)}{loc}「{quote}」" if quote else f"{_digest_clean(doc_id)}{loc}"
            extra_quotes = m.get("extra_quotes") or []
            if extra_quotes:
                extras_text = _LIST_SEP.join(f"「{_synthesis_quote(q, quote_cap)}」" for q in extra_quotes)
                fact += f"／別の一致: {extras_text}"
        else:
            fact = _digest_clean(doc_id)
        candidates.append((ev_id, _digest_clean(f"{ev_id}: {fact}"), [doc_id]))


    for r in (read_evidence or []):
        if not isinstance(r, dict):
            continue
        if r.get("kind") in ("list", "outline", "compare"):
            # glob_search／doc_outline／compare_documents の確定事実（`InvestigationState` の要約行）。
            text = _digest_clean(str(r.get("text") or ""))
            if text:
                candidates.append((None, text, None))
            continue
        doc_id = r.get("doc_id")
        if not doc_id:
            continue
        loc = _synthesis_span_loc(r.get("span"))
        text = _digest_clean(str(r.get("text") or ""))
        fact = f"精読: {_digest_clean(doc_id)}{loc}「{text}」" if text else f"精読: {_digest_clean(doc_id)}{loc}"
        if r.get("text_truncated"):
            fact += "（末尾未保持）"
        candidates.append((None, _digest_clean(fact), None))

    lines: list = []
    costs: list = []
    line_ev_ids: list = []
    ev_map: dict = {}
    total_bytes = 0

    def _marginal_cost(line: str) -> int:
        enc = len(line.encode("utf-8", errors="replace"))
        return enc if not lines else enc + 1

    included = 0
    for ev_id, line, matched in candidates:
        b = _marginal_cost(line)
        if total_bytes + b > max_bytes:
            break
        lines.append(line)
        costs.append(b)
        line_ev_ids.append(ev_id)
        if ev_id is not None:   # read_evidence 行（ev_id=None）は ev_map に登録しない
            ev_map[ev_id] = matched
        total_bytes += b
        included += 1

    omitted = len(candidates) - included
    if omitted > 0:
        # 注記の桁数（M）は pop するたびに増える——先に一度だけ計算した注記コストで固定判定すると
        # 桁上がり（9件→10件等）でわずかに上限を超えうるため、pop の都度注記を作り直す。
        notice = _SYNTHESIS_TRUNCATION_NOTICE_TMPL.format(n=omitted)
        while lines and total_bytes + _marginal_cost(notice) > max_bytes:
            total_bytes -= costs.pop()
            lines.pop()
            _popped_id = line_ev_ids.pop()
            if _popped_id is not None:
                del ev_map[_popped_id]
            omitted += 1
            notice = _SYNTHESIS_TRUNCATION_NOTICE_TMPL.format(n=omitted)
        total_bytes += _marginal_cost(notice)
        lines.append(notice)
    return "\n".join(lines), ev_map, (omitted > 0 or paths_omitted)


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
    "tools_per_turn_exceeded",       # 調べる操作の回数の上限に到達（1応答内の tool 呼び出し数上限）
    "refusal",                       # AI が回答を控えた（安全上の理由）
    "evidence_verification_failed",  # 根拠不足で中断（citation が全て機械検証で落ちた）
})
# STOP-1: 調査予算（ターン数／呼び出し予算／1応答あたりの調べる操作の回数）到達で
# 打ち切られた3値——`providers/base.py::_agentic_run` がこの3値を「一般的な失敗」（空回答→単発
# grep フォールバック）から分離し、固定文言の headline と既存 Evidence Packet を最終 envelope へ
# 載せる根拠に使う。値の実装は `stop_kind._BUDGET_STOP_REASONS` を唯一の真実源として参照する
# （`web/chat/render.js::BUDGET_EXHAUSTED_STOP_REASONS` は表示側の別実装のまま＝そちらを変える
# 場合は別途揃える）。
_BUDGET_EXHAUSTED_STOP_REASONS = stop_kind._BUDGET_STOP_REASONS
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
    "次の依頼について、以下の根拠だけを使って、日本語で回答してください（長さは絞らない・集めた内容は削らない・"
    "取得済みの対象項目を要約や代表例化で落とさない。表を指定されたら指定列を守り、項目と行・値の対応を保つ）。"
    "確認できたこと（確定）と確認できなかったことを分けて書き、"
    "根拠に無いことを補うときは『推定』と明示する（取得できなかった値は推測で埋めず『未取得』と書く）。\n\n"
    "【依頼】\n{question}\n\n"
    "【確認できた根拠】\n{digest}"
)


def _note_dropped_citations(state, dropped: list) -> None:
    """機械検証で除外した citation を調査状態の限界へ残す（清書・クリーン再合成の入力に
    「検証で除外」が届くように・計画経路／ハイブリッドと同じ文面・重複は 1 本）。"""
    if state is None:
        return
    for d in dropped or []:
        if isinstance(d, dict):
            gap = f"{d.get('doc_id')}: 検証で除外（{d.get('reason')}）"
            if gap not in state.gaps:
                state.gaps.append(gap)


def _committed_evidence_digest(committed: list, evidence_meta: list | None = None,
                               structural_evidence_meta: list | None = None,
                               gaps: list | None = None, read_evidence: list | None = None, limits: dict | None = None) -> str:
    """Committed Evidence（doc_id/span/quote）と検証済みの構造的根拠（list_docs 集計・graph カード）、
    調査の限界（gaps）から再合成用の根拠一覧テキストを組む（`build_synthesis_digest` と同じ体裁）。

    ツール呼び出し履歴・落とした citation・モデルの前回ドラフト回答は一切含めない
    （クリーンな再合成コンテキスト＝落ちた根拠に基づく主張を新しい回答へ持ち越さないため）。
    構造的根拠と限界行を含めるのは、再合成の指示（確認できた／できなかったを分ける・一覧の項目を
    落とさない・未取得を明示）に材料が無いと達成不能になるため。committed も構造的根拠も無ければ空文字列。
    """
    if evidence_meta is None and not structural_evidence_meta and not gaps and not read_evidence:
        lines = [f"- {c.get('doc_id')}（span={c.get('span')}）: {c.get('quote', '')}"
                for c in committed if c.get("doc_id")]
        return "\n".join(lines)
    meta = list(evidence_meta or [])
    if len(meta) < len(committed):
        meta += [{"doc_id": c.get("doc_id"), "span": c.get("span")} for c in committed[len(meta):]]
    if not committed and not structural_evidence_meta and not read_evidence:
        return ""
    digest, _, truncated = build_synthesis_digest(committed, meta[:len(committed)] + list(structural_evidence_meta or []),
                                                  read_evidence=read_evidence, gaps=gaps)
    if truncated and limits is not None:
        limits["synthesis_truncated"] = True          # 再合成入力の打ち切りも計測に載せる（予算自体は不変）
    return digest


def _clean_resynthesis_anthropic(client, model: str, system: str, question: str,
                                 mt: int, committed: list, usage: dict, *,
                                 evidence_meta: list | None = None, structural_evidence_meta: list | None = None,
                                 gaps: list | None = None, read_evidence: list | None = None,
                                 limits: dict | None = None) -> tuple[str, str | None]:
    """Anthropic 経由のクリーン再合成——入力は **system＋現在の質問＋Committed Evidence digest**
    だけ（tools 無し）。通常の会話履歴（`history`）・ツール呼び出し履歴・モデルの前回ドラフトは
    一切渡さない（過去ターンの文脈や落ちた根拠に基づく主張を新しい回答へ持ち越さない）。
    失敗（例外・空応答・committed が空）は空文字列を返す（呼び出し元が honest failure として扱う）。

    戻り値は `(text, stop_reason)`——EV-0（拡張設計 §4.4）: この再合成コール自体が `max_tokens` で
    打ち切られた場合も、呼び出し元が帰属をスキップできるよう完了理由を一緒に返す。
    """
    digest = _committed_evidence_digest(committed, evidence_meta, structural_evidence_meta, gaps, read_evidence,
                                        limits=limits)
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
                              committed: list, usage: dict, *,
                              evidence_meta: list | None = None, structural_evidence_meta: list | None = None,
                              gaps: list | None = None, read_evidence: list | None = None,
                              limits: dict | None = None) -> tuple[str, str | None]:
    """Gemini 経由のクリーン再合成（`_clean_resynthesis_anthropic` と同じ最小コンテキスト方針・
    system＋現在の質問＋digest だけ・`history` は渡さない）。戻り値は `(text, finishReason)`。
    """
    digest = _committed_evidence_digest(committed, evidence_meta, structural_evidence_meta, gaps, read_evidence,
                                        limits=limits)
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
                      failure_kind: str | None = None,
                      read_evidence: list | None = None,
                      gaps: list | None = None,
                      limits: dict | None = None,
                      created_files: list | None = None,
                      backend_failures: dict | None = None,
                      non_recoverable_failure: bool = False) -> dict:
    """`{"final": ...}` イベントの共通組み立て（Committed Evidence 化は呼び出し元が済ませた状態で
    受け取る）。候補があったのに全滅した場合は `stop_reason` を `evidence_verification_failed` へ
    上書きする（honest failure）。

    `created_files`（DEPTH-2 S2・§2.7・省略可・既定 None＝空）: `write_output_file` ツールが台帳
    登録に成功した行（`InvestigationState.created_files`・`{"rel_path","download_url",...}`）。
    値が空なら `limits` と同じ流儀でキー自体を payload に作らない（既存消費者への無害な後方互換）。
    非空でも `gaps`/`read_evidence` と同じ内部専用チャンネル——公開 `data.citations`/Evidence Packet
    には出さない（呼び出し元 base.py が `env["created_files"]`（ダウンロード導線カード）へ写す）。

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

    `read_evidence`（省略可・既定 None＝空・C 追加）: この run 中に read_around/read_doc で実際に
    読んだ本文（`InvestigationState` の kind="read" Evidence を `{"doc_id","span","text"}` へ薄く
    写したもの・本文は既に `_redact`・`investigation_state._READ_TEXT_CAP_BYTES`（清書予算の 1/4）まで切り詰め済み）。ハイブリッド
    （`providers/base.py::_agentic_run`）が清書入力（`build_synthesis_digest` の `read_evidence`
    引数）へ引き継ぐための内部専用チャンネル——citation でも構造 Evidence でもない（Evidence
    Packet／`data.citations` には出さない）。

    `gaps`（省略可・既定 None＝空）: この run の `InvestigationState.gaps`
    （0件検索・エラー・打ち切り等の機械的な調査の限界の記録・文字列のまま）。sub ループの
    ローカル状態はこの呼び出しを最後にループの外へは公開されないため、ハイブリッドの親
    `state`（`providers/base.py::_ingest_sub_final_into_state`）へ引き継ぐにはここに載せる
    必要がある——`read_evidence` と同じ内部専用チャンネル（Evidence Packet／`data.citations`
    には出さない）。

    `backend_failures`（省略可・既定 None＝空）/`non_recoverable_failure`（省略可・既定 False）:
    障害種別（`InvestigationState.backend_failures`/`non_recoverable_failure`）。`gaps`/`limits`
    と同じ理由でここに載せないとループの外（`providers/base.py::_ingest_sub_final_into_state`）へ
    引き継げない——単発フォールバックへの縮退可否判定（`providers/base.py::run`）が使う。
    """
    structural_evidence_meta = structural_evidence_meta or []
    has_structural_evidence = bool(structural_evidence_meta)
    # 予算到達3種（turns_exhausted/budget_exceeded/tools_per_turn_exceeded）は根拠ゲートの
    # 「候補があったのに全滅」より優先して保持する——ここで evidence_verification_failed へ
    # 上書きすると、単発フォールバックへの縮退可否判定（`providers/base.py::run` の
    # `_stop_reason_not_budget`）が実際の終了理由を読み取れなくなる。
    if (not committed and dropped and not has_structural_evidence
            and stop_reason not in _BUDGET_EXHAUSTED_STOP_REASONS):
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
              "failure_kind": failure_kind,
              "read_evidence": read_evidence or [],
              "gaps": gaps or []}
    # limits（1ターンで実際に当たった内部制限のカウンタ・`InvestigationState.limits`）: 値が
    # 全て既定（0/False）なら制限に当たっていないターン＝キー自体を作らず既存 payload と
    # byte-identical のままにする（利用統計側は「キー無し＝未計測」ではなく「キー無し＝
    # 制限0件」と解釈する・`store/usage.py::usage_stats` の `limits` 集計参照）。
    if limits and any(limits.values()):
        payload["limits"] = dict(limits)
    # backend_failures（値が全て既定=False なら未計測と同じキー無しのまま・limits と同じ流儀）。
    if backend_failures and any(backend_failures.values()):
        payload["backend_failures"] = dict(backend_failures)
    if non_recoverable_failure:
        payload["non_recoverable_failure"] = True
    if created_files:
        payload["created_files"] = list(created_files)
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
                         attributed_ev_ids: set | None = None,
                         read_evidence: list | None = None,
                         gaps: list | None = None,
                         limits: dict | None = None,
                         created_files: list | None = None,
                         backend_failures: dict | None = None,
                         non_recoverable_failure: bool = False) -> dict:
    """`_finalize_payload` の薄いラッパー。citation 列（Candidate のまま）を受け取り、ここで
    `_commit_evidence` を1回だけ実行してから共通組み立てへ渡す（緊急打ち切り経路でも未検証
    citation を外へ出さない）。
    """
    committed, evidence_meta, dropped = _commit_evidence(cites, world)
    return _finalize_payload(text, docs, searched, committed, evidence_meta, dropped, cards, usage,
                             verified_docs, stop_reason, evaluation, structural_evidence_meta,
                             used_evidence_docs, attributed_ev_ids, read_evidence=read_evidence,
                             gaps=gaps, limits=limits, created_files=created_files,
                             backend_failures=backend_failures,
                             non_recoverable_failure=non_recoverable_failure)


def _render_existing_claims_for_prompt(existing_claims: list[dict] | None, max_bytes: int) -> str:
    """再調査（2回目以降）の worker 一次判断要求へ渡す既存主張の整形——id・status・本文のみ
    （`evidence_refs` は今回のローカル調査状態とは別採番のため渡さない・呼び出し元
    `providers/base.py::_ingest_sub_final_into_state` docstring 参照）。`max_bytes`（
    `_SYNTHESIS_MAX_BYTES // 4` と同じ「予算の1/4」流用・新設定は増やさない）超過時は、全件の
    id・status を必ず保持し、本文だけを件ごとに均等配分の残余で切り詰める（入らない分は
    「(本文省略)」で列挙）。行境界（1件=1行）で切り、id を途中で切らない——後続の `[cN]` 行が
    丸ごと落ちると、取り込み側の id 置換（同一 id は最新扱い）が別論点の主張を消しうる。
    """
    if not existing_claims:
        return ""
    items = []
    for c in existing_claims:
        cid, status, text = c.get("id"), c.get("status"), c.get("text")
        if not cid or not status or not text:
            continue
        items.append((cid, status, text))
    if not items:
        return ""

    def _line(cid, status, text) -> str:
        return f"[{cid}] {status}: {text}"

    full_out = "\n".join(_line(cid, status, text) for cid, status, text in items)
    if len(full_out.encode("utf-8")) <= max_bytes:
        return full_out

    n = len(items)
    prefixes = [f"[{cid}] {status}: " for cid, status, _ in items]
    prefix_bytes = [len(p.encode("utf-8")) for p in prefixes]
    newline_bytes = max(n - 1, 0)
    budget_for_text = max_bytes - sum(prefix_bytes) - newline_bytes
    per_item_text_budget = max(budget_for_text // n, 0)

    placeholder = "(本文省略)"
    lines = []
    for (cid, status, text), prefix in zip(items, prefixes):
        if per_item_text_budget <= 0:
            lines.append(f"{prefix}{placeholder}")
            continue
        raw_text = text.encode("utf-8")
        if len(raw_text) <= per_item_text_budget:
            lines.append(f"{prefix}{text}")
        else:
            truncated = raw_text[:per_item_text_budget].decode("utf-8", errors="ignore")
            lines.append(f"{prefix}{truncated}" if truncated else f"{prefix}{placeholder}")
    return "\n".join(lines)


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
                 tools_pref: dict | None = None, tools_availability: dict | None = None,
                 system_settings: dict | None = None, uid: str | None = None,
                 request_claims: bool = True,
                 existing_claims: list[dict] | None = None):
    """OpenAI/Ollama の tool-use を反復。`{"node":..}` を yield しつつ最後に `{"final","docs"}`。

    `uid`（DEPTH-2 S2・§2.7・省略可・既定 `None`）: `write_output_file` ツール（成果物登録の
    共通化）の書き先（個人 workspace の files/）を決める呼び出し元のユーザー id。`None`（省略・
    テスト等）のときは `write_output_file` を呼んでも「作成者が特定できません」という error 結果に
    なる（`run_tool` へそのまま転送・`toolset` を明示指定しても uid は常に転送する）。

    `request_claims`（`final_synthesis=False` 経路限定・既定 `True`）: 偽なら worker の一次判断
    （確定/推定/不明の主張配列）を要求する追加の LLM 呼び出し自体を発行しない。呼び出し元
    （`providers/base.py`）がこのターンで主張を査読する見込みが無いと分かっている場合に使う——
    査読を通らない主張はどのみち清書・公開へ渡らないため、要求すること自体が無駄になる。
    `final_synthesis=True` のときは無視する。

    `existing_claims`（`request_claims=True` かつ `final_synthesis=False` のときだけ使う・
    省略可・既定 `None`）: 再調査（このターンで既に確定している worker 由来の主張がある場合）で、
    `claims_prompt` へ渡す既存主張（`{"id","status","text",...}` の list・`investigation_state.
    claim_to_dict` の形でよい・`evidence_refs`/`reason`/`reason_code` は無視する）。呼び出し元
    （`providers/base.py::_agentic_run`）が初回にはこの引数を渡さない（既存の主張がまだ無い）——
    渡さない場合は id 採番規約を伝えない（初回はどのみち衝突する既存 id が無い）。

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

    `stop_event`（UI フィードバック1「途中停止」）: 各ターンの
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
    表示）ではなく `_tool_node_sub`（args を含まない固定文言）になる。

    `MAX_TOOLS_PER_TURN`（DoS 対策）: 1 応答内の tool_calls 実行数を独立に
    上限する（`max_turns` は応答ラウンド数だけを制限し、1 応答内の呼び出し数自体は制限しない）。超過分は
    `run_tool` を呼ばずに打ち切り、`stop_event` も各ツール実行の直前に確認する（メイン/サブ経路の
    両方に適用＝`allowed_tools` の有無に関わらず一律）。

    超過分（例: 1応答に10万件の tool_calls）に対して超過件数と同数の「上限」ノードを
    生成すると SSE/trace が肥大化する。`calls[:max_tools_per_turn]` だけを処理し、超過があれば
    ループ終了後に**固定ノード1件だけ**生成して打ち切る。

    `SHERPA_TOOL_PARALLEL`（D1・ツール並列）: 1 応答内の呼び出しが2本以上・ask_user を含まない
    ときだけ `ThreadPoolExecutor(max_workers=SHERPA_TOOL_PARALLEL)` で同時実行する（同一応答内の
    呼び出しは引数が確定済み＝互いに依存しない読み取りのため）。既定3・1なら常に直列（モデルが
    結果を見て次を決める応答間の反復は変えない）。完了順に関わらず `msgs` へ積む結果は元の呼び出し
    順で組む（`tool_call_id` 対応を壊さない）。`shared_budget`/`docs`/`cites`/`cards`/`state` の
    更新はワーカー（`run_tool` 呼び出しそのもの）の外・メインスレッドでのみ行う。ワーカーの例外は
    その呼び出し1件だけの error 結果に変換し、他の呼び出しは止めない。停止は各呼び出しの投入前に
    確認し、投入済みの完了は待ってから（未投入分は実行せず）既存の停止契約（final を出さない）に
    従う。

    ツールノードを yield した直後（generator が
    一時停止し、呼び出し元がノードを処理してから再開される窓）に停止要求が来ても、再開後に
    stop_event を再確認しなければ ask_user 分岐/`run_tool` を1件実行してしまう。ノード yield
    直後・ask_user 分岐/`run_tool` の直前にも stop_event を再確認する。

    `run_tool` の戻り値（tool result）の累計バイト量
    （1 run＝本関数の1呼び出し全体）が `TOOL_RESULT_MAX_TOTAL_BYTES` を超えたら、固定エラーの node を
    1件流して run を打ち切る（fail-closed。read_around 等の tool-result が (a)(b) で個別に上限化
    されていても、多数回の呼び出しが積み重なる総量までは抑えられないため）。

    拒否分岐を `allowed_tools` の有無でサブ経路（明示指定）だけに限ると、メイン経路
    （`allowed_tools=None`）では提示していないツール名をモデルが呼んでも `run_tool` を実行しうる
    非対称が残る（現状は `tools` がフル提示のため実害は無いが、提示 toolset と実行可否が独立だと
    将来の呼び出し元の footgun になる）。そのためループ冒頭で実際に提示した `tools` からツール名集合
    `offered_names` を導出し、
    `effective_allowed = allowed_tools if allowed_tools is not None else offered_names` を実行
    allowlist として使う（＝提示していないツール名は常に拒否・メイン/サブ経路で対称）。メイン経路の
    正常系は不変: `tools` は元々 `openai_tools(...)`／`toolset` の**そのもの**から `offered_names`
    を作るため、通常提示されるツール（read_around/ripgrep_search/list_docs＋条件付き
    es_search/graph_neighbors/ask_user）は必ず `offered_names` に含まれる。拒否ノード/結果は
    サブ経路と同じ固定文言・`safe_name` クリップ・`total_tool_bytes` 累計計上をメイン経路にも適用
    （対称化）。ノード表示の豊かさ（`_tool_node` vs `_tool_node_sub`）はこの allowlist とは別の
    軸のまま＝引き続き `allowed_tools is not None`（真のサブ経路かどうか）で判定する。

    `usage_acc`（S3・chat-sub 計測の欠落是正）: 非 None のとき、`{"calls": int, "tokens": dict|None}` 形の呼び出し元アキュムレータを更新する。
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
    _unreachable_backends: list = []   # 実接続で不達のバックエンド（利用者の OFF とは区別する）
    if toolset is not None:
        tools = toolset               # SC-6e: 明示指定時は可用性判定を一切参照しない（docstring 参照）
    else:
        # SC-6e: 呼び出し元が渡した snapshot を使う（無ければ後方互換で都度チェック・TTLキャッシュつき）。
        _avail = tools_availability if tools_availability is not None else tool_availability()
        _unreachable_backends = _unreachable_backends_at_start(_avail, _tp)
        tools = openai_tools(
            with_es=_avail["fulltext"] and _tp["fulltext"], with_graph=_avail["graph"] and _tp["graph"],
            can_ask=can_ask, with_grep=_tp["grep"], with_write=uid is not None)
    # 実際に提示した `tools` からツール名集合を
    # 導出し、`allowed_tools` 未指定（メイン経路）でも「提示していないツール名は拒否」を強制する。
    offered_names = frozenset(t["function"]["name"] for t in tools)
    effective_allowed = allowed_tools if allowed_tools is not None else offered_names
    # C2（探索ループの文脈整理）: この run 専用のローカル調査状態（呼び出し元には公開しない・
    # `_read_evidence_payload` だけが `final` payload へ橋渡しする）。`_round_bounds` は
    # `msgs` 内の「assistant(tool_calls)＋対応する tool メッセージ全部」1組ぶんの `[start, end)`
    # を古い順に積む——置換境界は常にこの組の先頭に揃えるため、組の途中で切ることがない。
    state = investigation_state.InvestigationState(question=user, scope={"world": world, "layer": layer})
    for _kind in _unreachable_backends:
        # 不達でツール集合から外れたターンも縮退として1回だけ計数する（実行中の記録機会が無い）。
        state.mark_backend_failure(_kind)
    _prefix_len = len(msgs)
    _round_bounds: list[tuple[int, int]] = []
    docs: set = set()
    cites: list = []
    cards: list = []
    searched = False
    usage = _new_usage_acc()                   # F3: 全ツールターンの usage を合算
    if system_settings is None:
        from . import store
        system_settings = store.get_system_settings()
    max_tools_per_turn = effective_max_tools_per_turn(system_settings)
    total_tool_bytes = 0                        # 1 run 累計の tool-result バイト量
    # BUDGET-1（§3.4）: run 開始時に1回だけ解決し、run の間ずっと使い回す（途中で admin が設定を
    # 変えても当該 run には影響しない）。BUDGET-2（§3.4）: メイン頭脳の provider/model を渡し、
    # 窓由来の上限との min() を取る（`resolve_tool_result_budgets` docstring 参照）。Ollama の場合
    # だけ `/api/show` 照会用の base_url を導出する（`model_windows.derive_ollama_base_url`）。
    from . import model_windows as _model_windows
    tool_result_max_bytes, tool_result_max_total_bytes = resolve_tool_result_budgets(
        system_settings=system_settings,
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
        _round_start = len(msgs)   # C2: この turn で足す「assistant(tool_calls)＋tool 結果」組の開始位置
        body = {"model": model, "messages": msgs, "tools": tools}
        if ollama:
            body["stream"] = False
            body["options"] = {"temperature": 0.2}
        # OpenAI へは temperature を送らない（bedrock/Claude と同じ扱い）。gpt-5.5 系は既定値(1)以外を
        # 拒否し 400 `unsupported_value` を返すため、送るとツールループが丸ごと失敗する。
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
                                       structural_evidence_meta=structural_evidence_meta,
                                       read_evidence=_read_evidence_payload(state), gaps=state.gaps, limits=state.limits, backend_failures=state.backend_failures,
                                       non_recoverable_failure=state.non_recoverable_failure,
                                       created_files=state.created_files)
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
                                               structural_evidence_meta=structural_evidence_meta,
                                               read_evidence=_read_evidence_payload(state), gaps=state.gaps, limits=state.limits, backend_failures=state.backend_failures,
                                       non_recoverable_failure=state.non_recoverable_failure,
                                               created_files=state.created_files)
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
        # 超過分は `_pending_calls`
        # （`calls[:max_tools_per_turn]`）で単純に切り捨てる（超過件数分のノードを生成しない＝
        # 下のループ後にまとめて固定ノード1件だけ流す）。
        _pending_calls = calls[:max_tools_per_turn]
        over_limit = len(calls) > max_tools_per_turn
        # D1（ツール並列・同一応答内の独立した読み取りの同時実行）: ask_user を含まず・呼び出しが
        # 2本以上あるときだけ並列にする（`SHERPA_TOOL_PARALLEL<=1` は常にこの分岐へ入らない＝
        # 従来どおり直列）。ask_user を含む応答は question 優先の早期 return 契約（下の直列ループ）を
        # 変えないため対象外にする。
        _has_ask_user = any((tc.get("function") or {}).get("name") == "ask_user" for tc in _pending_calls)
        _use_parallel = SHERPA_TOOL_PARALLEL > 1 and len(_pending_calls) >= 2 and not _has_ask_user
        _tool_batch_started = time.monotonic()
        if _use_parallel:
            # 呼び出しと結果の対応は元の呼び出し順で保つ（完了順ではない）——`_futures` は投入順
            # そのまま積み、後段の結果処理も同じ順で走査する。ワーカーは `run_tool` の戻り値を
            # 返すだけで、docs/cites/cards/state/msgs/共有バイト予算の更新は全てこのメイン
            # スレッドで行う（共有 dict をワーカーから直接触らせない）。
            from .ingest.world_neo4j import GraphSchemaEraError   # 遅延 import（他の遅延 import と同じ理由）
            _futures: list = []   # (kind, tc, name, args, payload)・kind="rejected"|"run"
            _stopped = False
            _executor = concurrent.futures.ThreadPoolExecutor(max_workers=SHERPA_TOOL_PARALLEL)
            try:
                for tc in _pending_calls:
                    # 各呼び出しの投入前に stop_event を確認する——立っていれば未投入分（このtc
                    # 以降）は投入しない。投入済み（既に active ノードを yield 済み）は下の
                    # `finally` で完了を待つ（`run_tool` 自体には停止が伝わらない＝待つだけ）。
                    if stop_event is not None and stop_event.is_set():
                        _stopped = True
                        break
                    fn = tc.get("function") or {}
                    name = fn.get("name")
                    args = _safe_json(fn.get("arguments"))
                    if name not in effective_allowed:
                        yield {"node": _node("許可外のツール呼び出し", "許可されていないため拒否しました")}
                        safe_name = _clip_utf8_bytes(str(name or ""), _REJECTED_TOOL_NAME_MAX_BYTES)
                        _futures.append(("rejected", tc, name, args, safe_name))
                        continue
                    yield {"node": (_tool_node_sub(name) if allowed_tools is not None else _tool_node(name, args))}
                    # ノード yield 直後（generator 再開後）にも stop_event を再確認する（単体呼び出し
                    # 時と同じ LOW-E の窓塞ぎ）。
                    if stop_event is not None and stop_event.is_set():
                        _stopped = True
                        break
                    # `ThreadPoolExecutor` は呼び出し元スレッドの `contextvars.Context`（
                    # `worlds.pin_world_root` の pin 等）を継承しない——`copy_context().run(...)` で
                    # ワーカーへ明示的に持ち込む（さもないと pin が見えず別 root/fallback を解決しうる）。
                    _ctx = contextvars.copy_context()
                    fut = _executor.submit(_ctx.run, run_tool, name, args, world, scope_paths,
                                           deadline=tool_deadline, layer=layer, max_hits=max_hits,
                                           window_cap=window_cap, tool_result_max_bytes=tool_result_max_bytes,
                                           uid=uid)
                    _futures.append(("run", tc, name, args, fut))
            finally:
                _executor.shutdown(wait=True)
            # 投入完了後・結果収集前にも stop_event を再確認する——全件投入済みで待機中に停止要求が
            # 来ると `_stopped`（投入時にしか更新しない）は偽のままのため、ここで別途確認しないと
            # 停止後に結果を msgs/state へ積み done ノードを yield してしまう。
            if _stopped or (stop_event is not None and stop_event.is_set()):
                return   # 停止契約: 結果は msgs に積まず final も出さない（既存の stop_event 契約と同型）。
            for kind, tc, name, args, payload in _futures:
                if kind == "rejected":
                    safe_name = payload
                    result = {"error": f"ツール {safe_name} は使用できません"}
                    _sz = _result_byte_size(result)
                    total_tool_bytes += _sz
                    if shared_budget is not None and not _tool_bytes_over_budget(0, shared_budget, tool_result_max_total_bytes):
                        shared_budget["tool_bytes_used"] += _sz
                    if _tool_bytes_over_budget(total_tool_bytes, shared_budget, tool_result_max_total_bytes):
                        state.mark_limit("total_budget_hit")
                        yield {"node": _node("ツール結果の合計サイズ上限",
                                             "この会話で取得した量が多すぎるため打ち切りました")}
                        yield _build_final_payload("", docs, searched, cites, cards, _usage_or_none(usage),
                                                   verified_docs, "budget_exceeded", world,
                                                   structural_evidence_meta=structural_evidence_meta,
                                                   read_evidence=_read_evidence_payload(state), gaps=state.gaps, limits=state.limits, backend_failures=state.backend_failures,
                                       non_recoverable_failure=state.non_recoverable_failure,
                                                   created_files=state.created_files)
                        return
                    tmsg = {"role": "tool", "name": safe_name, "content": json.dumps(result, ensure_ascii=False)}
                    if tc.get("id"):
                        tmsg["tool_call_id"] = tc["id"]
                    msgs.append(tmsg)
                    continue
                try:
                    result, d, c, cd = payload.result()
                except GraphSchemaEraError:
                    # 旧世代グラフ→再取り込み案内で停止する既存契約（`providers/base.py::_agentic_run`
                    # の `except GraphSchemaEraError: raise` 参照）——通常のツールエラーへ丸めず、
                    # 直列時と同じくそのまま再送出する（他の呼び出しは既に並走して完了済みでも
                    # run 全体を止める・fail-loud）。
                    raise
                except Exception as e:
                    # ワーカー（`run_tool`）の例外はこの呼び出しだけの error 結果に変換する
                    # （他の呼び出しは既に並走して完了済み・止めない）。生の例外文字列（絶対パス・
                    # ドキュメント本文の断片・辞書キー名等を含みうる——`_mask_secrets` は既知の
                    # 秘密パターンしか伏せない）は本文はもちろんログにも出さない——型名と errno
                    # （あれば）だけを残す（CLAUDE.md「鍵・トークンの内容を端末に出さない」節の
                    # 精神に合わせ、ツール例外は文字列表現そのものを一切ログへ渡さない）。
                    _log.warning("agentic_search: tool 実行に失敗（%s）: %s errno=%s",
                                name, type(e).__name__, getattr(e, "errno", None))
                    _record_tool_exception(state, name, e)   # 障害種別を調査状態へ記録
                    result, d, c, cd = ({"error": "ツール実行に失敗しました"}, set(), [], [])
                _record_run_tool_limits(state, name, result)     # 並列経路も直列と同じ計測
                _record_tool_result_error_code(state, result, name)    # 結果化済み障害（read_io/es/graph 等）の反映
                hit_node = (_hit_summary_node_sub(name, result) if allowed_tools is not None
                           else _hit_summary_node(name, args, result))
                if hit_node:
                    yield {"node": hit_node}
                degrade_node = _degrade_result_node(result)
                if degrade_node:
                    yield {"node": degrade_node}
                truncated_node = _truncated_docs_node(result)
                if truncated_node:
                    yield {"node": truncated_node}
                _sz = _result_byte_size(result) + _result_byte_size(cd)
                total_tool_bytes += _sz
                if shared_budget is not None and not _tool_bytes_over_budget(0, shared_budget, tool_result_max_total_bytes):
                    shared_budget["tool_bytes_used"] += _sz
                if _tool_bytes_over_budget(total_tool_bytes, shared_budget, tool_result_max_total_bytes):
                    state.mark_limit("total_budget_hit")
                    yield {"node": _node("ツール結果の合計サイズ上限",
                                         "この会話で取得した量が多すぎるため打ち切りました")}
                    yield _build_final_payload("", docs, searched, cites, cards, _usage_or_none(usage),
                                               verified_docs, "budget_exceeded", world,
                                               structural_evidence_meta=structural_evidence_meta,
                                               read_evidence=_read_evidence_payload(state), gaps=state.gaps, limits=state.limits, backend_failures=state.backend_failures,
                                       non_recoverable_failure=state.non_recoverable_failure,
                                               created_files=state.created_files)
                    return
                docs |= d
                cites += c
                cards += cd
                if name in _VERIFIED_READ_TOOLS and "error" not in result:
                    verified_docs |= d
                _call_structural: list = []
                if name == "list_docs" and "error" not in result:
                    _matched = [doc.get("rel_path") for doc in (result.get("docs") or [])
                               if doc.get("rel_path")]
                    _call_structural.append({
                        "doc_id": None, "span": None, "verification_method": "list_docs_verified",
                        "list_meta": {"count": result.get("count", 0), "shown": len(_matched),
                                      "prefix": str(args.get("path_prefix") or "").strip(),
                                      "pattern": str(args.get("name_pattern") or "").strip(),
                                      "doctype": str(args.get("doctype") or "").strip(),
                                      "state": str(args.get("state") or "").strip()},
                        "matched_doc_ids": _matched})
                if name == "folder_tree" and "error" not in result:
                    _call_structural.append({
                        "doc_id": None, "span": None, "verification_method": "folder_tree_verified",
                        "tree_meta": {"prefix": result.get("path_prefix", ""), "depth": result.get("depth"),
                                     "count": result.get("count", 0),
                                     "shown": len(result.get("folders") or [])},
                        "matched_doc_ids": []})
                if name == "graph_neighbors" and cd:
                    _call_structural += _card_structural_evidence(cd)
                structural_evidence_meta += _call_structural
                state.add_tool_result(name, args, result, c, _call_structural)
                # DEPTH-2 S2（§2.7）: 台帳登録に成功した回だけ成果物として蓄積する
                # （`error` があれば失敗＝カードにしない・調査ループはこのまま継続する＝fail-open）。
                if name == "write_output_file" and isinstance(result, dict) and "error" not in result:
                    if result.get("rel_path"):
                        state.created_files.append(
                            {"rel_path": result["rel_path"], "download_url": result.get("download_url")})
                    for _r in (result.get("rendered") or []):
                        state.created_files.append(_r)
                    # C21: 書込成功時点で呼び出し元（providers/base.py::_agentic_run）へ即時に
                    # 伝える——このあと同じターン内で例外/ask_user が起きても、既に個人 workspace に
                    # 実在するファイルの個人由来フラグを見失わない（"final" を待たない・"node" では
                    # ない独立のサイドカーイベント）。
                    yield {"created_files": list(state.created_files)}
                tmsg = {"role": "tool", "name": name, "content": json.dumps(result, ensure_ascii=False)}
                if tc.get("id"):
                    tmsg["tool_call_id"] = tc["id"]
                msgs.append(tmsg)
        for tc in ([] if _use_parallel else _pending_calls):
            # 各ツール実行の直前に stop_event を確認する（1応答内に大量の tool_calls
            # が積まれていても、途中停止が反映されないまま実行し続けることを防ぐ）。
            if stop_event is not None and stop_event.is_set():
                return
            fn = tc.get("function") or {}
            name = fn.get("name")
            args = _safe_json(fn.get("arguments"))
            if name not in effective_allowed:
                # S3・二重強制の(b): ツール定義配列を絞っていても（モデルの逸脱/幻覚呼び出しに備え）
                # run_tool を呼ばずに拒否結果を返し、ループは継続する（例外にしない）。
                # 許可判定を
                # `_tool_node(name, args)` の**前**に行う——判定より先にノードを yield すると、
                # 除外済み ask_user 等をモデルが幻覚呼び出ししたとき、モデル生成の引数
                # （ask_user の "prompt" 等）が思考ノード/trace に漏れて表示・保存されてしまう。
                # `name` 自体も
                # モデル生成値（未知名なら任意の長文になり得る）＝label にも使わず、node は**完全固定文言**
                # にする。モデルへの是正フィードバック（tmsg・LLM 会話内のみ＝UI/trace に出ない）にだけ
                # name を残す（どのツール名が拒否されたかをモデルが自己修正するために必要）。
                # `name` は
                # モデル生成値で長さ無制限のため (a) `_REJECTED_TOOL_NAME_MAX_BYTES` で固定長へ
                # クリップし、(b) この tool-result も他の tool-result と同じ `total_tool_bytes`
                # 累計へ必ず計上する（計上しないと、この経路だけ 1 run 累計バイト上限を
                # すり抜けられる）。
                # 判定を
                # `effective_allowed`（サブ経路は `allowed_tools`・メイン経路は `offered_names`）へ
                # 統一し、メイン経路でも提示していないツール名の実行を拒否する（対称化）。
                yield {"node": _node("許可外のツール呼び出し", "許可されていないため拒否しました")}
                safe_name = _clip_utf8_bytes(str(name or ""), _REJECTED_TOOL_NAME_MAX_BYTES)
                result = {"error": f"ツール {safe_name} は使用できません"}
                _sz = _result_byte_size(result)
                total_tool_bytes += _sz
                # S4-b（§6.2 項1）: 横断予算にも同じ増分を計上する（不正な形は修復せず
                # 未加算のまま直後の判定で fail-closed・詳細は下の同型サイトのコメント参照）。
                if shared_budget is not None and not _tool_bytes_over_budget(0, shared_budget, tool_result_max_total_bytes):
                    shared_budget["tool_bytes_used"] += _sz
                if _tool_bytes_over_budget(total_tool_bytes, shared_budget, tool_result_max_total_bytes):
                    state.mark_limit("total_budget_hit")
                    yield {"node": _node("ツール結果の合計サイズ上限",
                                         "この会話で取得した量が多すぎるため打ち切りました")}
                    yield _build_final_payload("", docs, searched, cites, cards, _usage_or_none(usage),
                                               verified_docs, "budget_exceeded", world,
                                               structural_evidence_meta=structural_evidence_meta,
                                               read_evidence=_read_evidence_payload(state), gaps=state.gaps, limits=state.limits, backend_failures=state.backend_failures,
                                       non_recoverable_failure=state.non_recoverable_failure,
                                               created_files=state.created_files)
                    return
                tmsg = {"role": "tool", "name": safe_name, "content": json.dumps(result, ensure_ascii=False)}
                if tc.get("id"):
                    tmsg["tool_call_id"] = tc["id"]
                msgs.append(tmsg)
                continue
            # サブ経路（`allowed_tools is not None`＝
            # `_sub_agentic_loop` 経由）はモデル生成の引数（query/doc_id/path 等）を思考ノードに
            # 埋め込まない固定文言ノードにする（`_tool_node_sub` 参照）。メイン経路（allowed_tools
            # は None）は既存の `_tool_node`（豊かな表示）のまま＝byte-identical。
            yield {"node": (_tool_node_sub(name) if allowed_tools is not None else _tool_node(name, args))}
            # ノード yield 直後（generator 再開後）
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
                # ただし `write_output_file` が既にこのターンで台帳登録に成功していれば
                # （`state.created_files`）、ファイルは実在し個人 workspace に残ったまま――
                # `final` を経ずに破棄すると呼び出し元の個人由来フラグ（`env["wrote_files"]`）が
                # 立たず、確認カード・失敗保存が誤って共有可能（personal=False）のまま保存される
                # （DEPTH-2 S2 是正）。question イベントに乗せて呼び出し元へ伝える。
                yield {"question": _question_from_args(args), "created_files": list(state.created_files)}
                return
            # 直列経路も並列経路（上の `except GraphSchemaEraError: raise` / `except Exception`
            # 分岐）と同じ例外変換にする——直列時だけ想定外例外がループ全体（`providers/base.py`
            # の `except Exception as agentic_exc:`）まで伝播し全体終了になる非対称を解消する。
            # ただし対象は `final_synthesis=False`（本関数の呼び出し元が `_sub_loop`＝self_worker
            # 等の下調べ役サブループのときだけ・唯一この値を渡す・`providers/base.py::_sub_loop`
            # 参照）に限る——`final_synthesis=True` の呼び出し元（research_service/graph_admin/
            # usage_chat／頭脳自身の直接 `_agentic_loop`）は独自の例外分類契約を持つ
            # （例: `research_service.run_research` の `_sherpa_llm_send_error` マーカーによる
            # 「AI接続失敗」と「その他の予期しない失敗」の区別）——ここで例外をツール結果へ丸めて
            # 飲み込むと、その契約が壊れる（grep 由来の I/O 例外が別の一般エラー文言へ化ける）。
            from .ingest.world_neo4j import GraphSchemaEraError   # 遅延 import（他の遅延 import と同じ理由）
            if final_synthesis:
                result, d, c, cd = run_tool(name, args, world, scope_paths, deadline=tool_deadline,
                                            layer=layer, max_hits=max_hits, window_cap=window_cap,
                                            tool_result_max_bytes=tool_result_max_bytes, uid=uid)
            else:
                try:
                    result, d, c, cd = run_tool(name, args, world, scope_paths, deadline=tool_deadline,
                                                layer=layer, max_hits=max_hits, window_cap=window_cap,
                                                tool_result_max_bytes=tool_result_max_bytes, uid=uid)
                except GraphSchemaEraError:
                    raise
                except Exception as e:
                    # 生の例外文字列（絶対パス・ドキュメント本文の断片等を含みうる）はログにも出さ
                    # ない——型名と errno（あれば）だけを残す（並列経路と同じ流儀・上記コメント参照）。
                    _log.warning("agentic_search: tool 実行に失敗（%s）: %s errno=%s",
                                name, type(e).__name__, getattr(e, "errno", None))
                    _record_tool_exception(state, name, e)   # 障害種別を調査状態へ記録
                    result, d, c, cd = ({"error": "ツール実行に失敗しました"}, set(), [], [])
            _record_run_tool_limits(state, name, result)
            _record_tool_result_error_code(state, result, name)    # 結果化済み障害（read_io/es/graph 等）の反映
            # 「何を探して・いくつ当たったか」の追加ノード（`_tool_node`/`_tool_node_sub` は
            # 結果が出る前のノードのため件数を書けない・`_hit_summary_node`/`_hit_summary_node_sub`
            # 参照）。
            hit_node = (_hit_summary_node_sub(name, result) if allowed_tools is not None
                       else _hit_summary_node(name, args, result))
            if hit_node:
                yield {"node": hit_node}
            # es_search が BM25 のみへ縮退した場合、その理由を「思考の
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
            # 1 run 累計の tool-result バイト量が
            # 上限を超えたら、この結果は破棄し固定エラーで run を打ち切る（fail-closed）。
            # `cd`（cards サイドカー・`run_tool` 側で
            # 既に件数＋バイト上限クリップ済み＝`_clip_cards` 参照）の直列化バイトも累計へ計上する
            # （`result` のみを計測すると、cards はこの計測経路をすり抜けてしまう）。
            _sz = _result_byte_size(result) + _result_byte_size(cd)
            total_tool_bytes += _sz
            # S4-b（§6.2 項1）: 横断予算にも同じ増分を計上する。形が不正な dict は
            # **修復しない**（.get 既定や int 化で正常形に見せると片側キー欠損が helper をすり抜ける）。
            # 正常形（helper が total=0 で False を返す形）のときだけ加算し、不正なら未加算のまま
            # 直後の `_tool_bytes_over_budget` が True（fail-closed）で打ち切る。
            if shared_budget is not None and not _tool_bytes_over_budget(0, shared_budget, tool_result_max_total_bytes):
                shared_budget["tool_bytes_used"] += _sz
            if _tool_bytes_over_budget(total_tool_bytes, shared_budget, tool_result_max_total_bytes):
                state.mark_limit("total_budget_hit")
                yield {"node": _node("ツール結果の合計サイズ上限",
                                     "この会話で取得した量が多すぎるため打ち切りました")}
                yield _build_final_payload("", docs, searched, cites, cards, _usage_or_none(usage),
                                           verified_docs, "budget_exceeded", world,
                                           structural_evidence_meta=structural_evidence_meta,
                                           read_evidence=_read_evidence_payload(state), gaps=state.gaps, limits=state.limits, backend_failures=state.backend_failures,
                                       non_recoverable_failure=state.non_recoverable_failure,
                                           created_files=state.created_files)
                return
            docs |= d
            cites += c
            cards += cd
            # EXT-2/EV-0（拡張設計 §4.4）: 「精読済み」タグは read_around/read_doc を実際に呼んだ
            # doc_id のみ（エラー応答は精読が成立していないため除外）。grep/es_search のヒットのみの
            # doc は `verified_docs` に入らない＝出典フッターで「根拠」と「参考」を分ける最小ロジック。
            if name in _VERIFIED_READ_TOOLS and "error" not in result:
                verified_docs |= d
            # list_docs（doc_ledger の live 走査＝実在確認済み）／graph_neighbors（Neo4j 検証済み
            # card/edge）は citation を生成しないが、具体的な検証済みエントリがあれば根拠として正当。
            # 根拠ゲートが citation 件数だけで判定して資料一覧・件数質問や graph-only 回答を誤って
            # 落とさないためのシグナルとして記録する（troubleshoot 以外の lens でも graph 根拠を認める）。
            # C2: この呼び出し1回分の構造的根拠だけを先にローカルへ集め（`state.add_tool_result` は
            # 「この呼び出しで新たに得た分」だけを見る契約）、その後にターン全体の累積へ合流する。
            _call_structural: list = []
            if name == "list_docs" and "error" not in result:
                # EV-0（拡張設計 §4.4）: list_docs は**呼び出し単位で集計した1 Evidence**とする
                # （総件数・適用条件・列挙範囲＋列挙した各パス）——0件の呼び出しも「該当0件」という
                # 具体的な事実として1 Evidence（ev-N）を持つ（根拠ゲート・帰属の対象になる）。
                _matched = [doc.get("rel_path") for doc in (result.get("docs") or [])
                           if doc.get("rel_path")]
                _call_structural.append({
                    "doc_id": None, "span": None, "verification_method": "list_docs_verified",
                    "list_meta": {"count": result.get("count", 0), "shown": len(_matched),
                                  "prefix": str(args.get("path_prefix") or "").strip(),
                                  "pattern": str(args.get("name_pattern") or "").strip(),
                                  "doctype": str(args.get("doctype") or "").strip(),
                                  "state": str(args.get("state") or "").strip()},
                    "matched_doc_ids": _matched})
            if name == "folder_tree" and "error" not in result:
                # folder_tree（K6・doc_ledger 走査による決定的集計・LLM
                # 不使用）も list_docs と同じ「呼び出し単位で集計した1 Evidence」として構造 Evidence
                # 化する。フォルダは doc ではない（`run_tool` 参照＝`docs` 集合には何も足さない）ため
                # `matched_doc_ids` は常に空リスト——裏付け doc の代わりに集計事実そのものが根拠。
                _call_structural.append({
                    "doc_id": None, "span": None, "verification_method": "folder_tree_verified",
                    "tree_meta": {"prefix": result.get("path_prefix", ""), "depth": result.get("depth"),
                                 "count": result.get("count", 0),
                                 "shown": len(result.get("folders") or [])},
                    "matched_doc_ids": []})
            if name == "graph_neighbors" and cd:
                # `run_tool` が既にカード単位で裏付け doc を検証済み（無効カードは cd に含まれない・
                # `d` はその検証済み doc_id 集合そのもの）——ここで再検証しない。裏付け doc を
                # 主張しないカード（純粋なグラフ位相情報）は、Neo4j から実際に返ったノードである
                # こと自体を source_type=graph の構造 Evidence として計上する。
                _call_structural += _card_structural_evidence(cd)
            structural_evidence_meta += _call_structural
            # C2（探索ループの文脈整理）: 既存の docs/cites/cards の収集と並行して調査状態も育てる
            # （検証前の生 citation・確定済みの構造的根拠・精読本文——`add_tool_result` docstring 参照）。
            state.add_tool_result(name, args, result, c, _call_structural)
            # DEPTH-2 S2（§2.7）: 台帳登録に成功した回だけ成果物として蓄積する（並列経路と同じ判定）。
            if name == "write_output_file" and isinstance(result, dict) and "error" not in result:
                if result.get("rel_path"):
                    state.created_files.append(
                        {"rel_path": result["rel_path"], "download_url": result.get("download_url")})
                for _r in (result.get("rendered") or []):
                    state.created_files.append(_r)
                yield {"created_files": list(state.created_files)}   # C21: 書込成功時点で即時に伝える（並列経路と同じ理由）
            tmsg = {"role": "tool", "name": name, "content": json.dumps(result, ensure_ascii=False)}
            if tc.get("id"):
                tmsg["tool_call_id"] = tc["id"]
            msgs.append(tmsg)
        # D2（計測）: ラウンドごとに件数・並列度・所要時間だけを1行残す（本文は出さない）。
        _log.debug("tool batch: n=%d parallel=%d elapsed=%.2fs", len(_pending_calls),
                  (SHERPA_TOOL_PARALLEL if _use_parallel else 1), time.monotonic() - _tool_batch_started)
        if over_limit:
            # レビュー是正（LOW-D）: 超過件数に関わらず固定ノード1件だけ生成する。
            yield {"node": _node("ツール呼び出し上限", "1回の応答あたりの実行数上限に達したため打ち切りました")}
            # この応答は上限超過＝以降のターンへは進まず、ここで打ち切る。
            yield _build_final_payload("", docs, searched, cites, cards, _usage_or_none(usage),
                                       verified_docs, "tools_per_turn_exceeded", world,
                                       structural_evidence_meta=structural_evidence_meta,
                                       read_evidence=_read_evidence_payload(state), gaps=state.gaps, limits=state.limits, backend_failures=state.backend_failures,
                                       non_recoverable_failure=state.non_recoverable_failure,
                                       created_files=state.created_files)
            return
        _round_bounds.append((_round_start, len(msgs)))
        # 探索ループの文脈整理: `msgs` が予算を超えたら、最新 `SHERPA_AGENTIC_KEEP_RECENT_TOOLS`
        # 回分のツール往復を残し、それより古い「assistant(tool_calls)＋対応する tool
        # メッセージ全部」の組を丸ごと1通の user メッセージへ置換する（system・元の質問＝
        # `msgs[:_prefix_len]` は触れない）。境界は常に組の先頭 `_round_bounds[i][0]` に揃えるため、
        # 組の途中で切ることはない。置換後にまた予算を超えれば、次のターン終端で同じ判定が再び
        # 発動し、その時点の最新状態で1通だけ作り直す（要約メッセージを積み増ししない）。
        if (len(_round_bounds) > SHERPA_AGENTIC_KEEP_RECENT_TOOLS
                and _messages_byte_size(msgs) > SHERPA_AGENTIC_CONTEXT_BUDGET_BYTES):
            _cutoff = _round_bounds[-SHERPA_AGENTIC_KEEP_RECENT_TOOLS][0]
            _collapsed_rounds = len(_round_bounds) - SHERPA_AGENTIC_KEEP_RECENT_TOOLS
            _summary = state.render(max_bytes=SHERPA_AGENTIC_CONTEXT_BUDGET_BYTES // 2,
                                    keep_recent_tools=SHERPA_AGENTIC_KEEP_RECENT_TOOLS)
            msgs[_prefix_len:_cutoff] = [
                {"role": "user", "content": f"【ここまでの調査状態】\n{_summary}"}]
            _shift = (_cutoff - _prefix_len) - 1   # 置換前の範囲長 → 置換後は1通ぶんだけ
            _round_bounds = [(s - _shift, e - _shift) for s, e in _round_bounds[_collapsed_rounds:]]
            state.bump_limit("context_compactions")
            yield {"node": _context_compacted_node(_collapsed_rounds)}
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
                                           structural_evidence_meta=structural_evidence_meta,
                                           read_evidence=_read_evidence_payload(state), gaps=state.gaps, limits=state.limits, backend_failures=state.backend_failures,
                                       non_recoverable_failure=state.non_recoverable_failure,
                                           created_files=state.created_files)
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
        # DEPTH-2 S4b（docs/proposals/2026-09-17-深さの再定義とレビュー巡.md §2.2・§5 S4）: 文章を
        # 破棄する代わりに、収集済み根拠（このループ専用のローカル `state`）から確定/推定/不明の
        # 主張配列を worker 自身の接続・モデルで**1回だけ**要求する。通信失敗・パース不能・
        # 途中で切れた JSON・budget_exceeded はいずれも `claims_raw=None`＝根拠だけで従来どおり
        # 進む（honest failure に倒す既存契約は変えない・失敗に倒れない）。利用者の停止要求
        # （`_SendAborted(reason="stop")`）だけは他の送信点と同じ契約で final を出さず return
        # する。`evidence_refs` は worker
        # 専用のローカル `state.evidence` の ev-N のまま持ち出す——Evidence Packet 化
        # （`_commit_evidence`）前・親 `InvestigationState` とは別採番のため、ここでは書き換えない
        # （書き換えは取り込み側 `providers/base.py::_ingest_sub_final_into_state` の責務・
        # `investigation_state.remap_claim_refs_to_evidence` 参照）。`request_claims` が偽の
        # ターン（呼び出し元が査読を発動しないと分かっている深さ）ではこの呼び出し自体を
        # 発行しない——査読を通らない一次判断はどのみち公開されないため。
        claims_raw = None
        if request_claims and state.evidence:
            try:
                from .providers.prompts import claims_prompt
                _claims_digest = state.render(max_bytes=_SYNTHESIS_MAX_BYTES // 2)
                _existing_claims_text = _render_existing_claims_for_prompt(
                    existing_claims, _SYNTHESIS_MAX_BYTES // 4)
                _claims_msgs = [{"role": "system", "content": system},
                                {"role": "user", "content": claims_prompt(
                                    user, _claims_digest, _existing_claims_text)}]
                _claims_body = {"model": model, "messages": _claims_msgs}
                if ollama:
                    _claims_body["stream"] = False
                    _claims_body["options"] = {"temperature": 0.2}
                _claims_resp = _send(endpoint, headers, _claims_body, timeout=_resolve_timeout(timeout))
                _acc_openai_usage(usage, _claims_resp, ollama)
                if usage_acc is not None:
                    usage_acc["tokens"] = _usage_or_none(usage)
                _claims_msg = ((_claims_resp.get("choices") or [{}])[0].get("message")
                              if "choices" in _claims_resp else _claims_resp.get("message")) or {}
                claims_raw = investigation_state.extract_claims_json(_openai_style_text(_claims_msg))
            except _SendAborted as e:
                if e.reason == "stop":
                    return   # 停止時は final を出さない（他の送信点＝5766行付近と同じ契約）
                claims_raw = None   # budget_exceeded は従来どおり「一次判断なし」で payload を返す
            except Exception as e:
                from .ingest.graph_extract import _log_masked_exception
                _log_masked_exception(_log, "agentic_search: worker 一次判断の生成に失敗", e,
                                      _header_secret(headers))
                claims_raw = None
        payload = _build_final_payload("", docs, searched, cites, cards, _usage_or_none(usage),
                                       verified_docs, stop_reason, world, evaluation,
                                       structural_evidence_meta=structural_evidence_meta,
                                       read_evidence=_read_evidence_payload(state), gaps=state.gaps,
                                       limits=state.limits, backend_failures=state.backend_failures,
                                       non_recoverable_failure=state.non_recoverable_failure, created_files=state.created_files)
        if claims_raw is not None:
            parsed_claims = investigation_state.parse_claims(claims_raw, origin="worker")
            if parsed_claims:
                # 内部専用チャンネル（`read_evidence`/`gaps` と同じ「公開 payload/Evidence Packet
                # には出さない」流儀）——in-process の generator 呼び出しのため Evidence オブジェクト
                # をそのまま持ち出せる（JSON 化しない）。
                payload["claims_raw"] = parsed_claims
                payload["claims_evidence"] = list(state.evidence)
        yield payload
        return

    committed, evidence_meta, dropped = _commit_evidence(cites, world)
    _note_dropped_citations(state, dropped)
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
        digest = _committed_evidence_digest(committed, evidence_meta, structural_evidence_meta, state.gaps,
                                            _read_evidence_payload(state), limits=state.limits)
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
                            failure_kind=_failure_kind, read_evidence=_read_evidence_payload(state),
                            gaps=state.gaps, limits=state.limits,
                            backend_failures=state.backend_failures,
                            non_recoverable_failure=state.non_recoverable_failure,
                            created_files=state.created_files)


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
_ANTHROPIC_MAX_TOKENS = 16000              # ツールループの各応答の上限（最終回答は長さを絞らない方針＝余裕をもった値）


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
    `stop_event`（UI フィードバック1「途中停止」）: `openai_style` と
    同じ意味論＝各ターンのリクエスト発行前に確認し、立っていれば以降のリクエストを発行せず終了する。
    `history`（R1a・会話継続）: 直前ターンの (user, assistant) 対（時系列順・交互保証済み・
    上流でキャップ済み）。現在の user メッセージの前にそのまま並べる（system は kwargs のまま別）。
    省略/空なら従来と完全同一の初期 messages になる。`graph_admin.ask_graph` は位置引数で呼ぶため
    本引数に触れない＝既定 None（空）で後方互換。

    実際に提示した `tools` から
    ツール名集合 `offered_names` を導出し、モデルが提示していないツール名を呼んでも `run_tool` を
    実行せず拒否する（`openai_style`/`gemini` と同じ対称化。Anthropic 経路も元々 `allowed_tools`
    引数を持たない＝常に `offered_names` を allowlist として使う）。

    `SHERPA_TOOL_PARALLEL`（D1・ツール並列）: `openai_style` と同じ（tool_use が2本以上・ask_user
    を含まないときだけ同時実行・完了順に関わらず `tool_use_id` は元の呼び出し順で `results` に
    組む）。
    """
    if callable(client):                       # client_factory（遅延生成）にも対応
        client = client()
    _tp = tools_pref_mod.normalize_tools_pref(tools_pref)
    _unreachable_backends: list = []   # 実接続で不達のバックエンド（利用者の OFF とは区別する）
    if toolset is not None:
        src_tools = toolset            # SC-6e: 明示指定時は可用性判定を一切参照しない（docstring 参照）
    else:
        _avail = tools_availability if tools_availability is not None else tool_availability()   # SC-6e
        _unreachable_backends = _unreachable_backends_at_start(_avail, _tp)
        src_tools = openai_tools(
            with_es=_avail["fulltext"] and _tp["fulltext"], with_graph=_avail["graph"] and _tp["graph"],
            can_ask=can_ask, with_grep=_tp["grep"])
    tools = anthropic_tools_from_openai(src_tools)
    offered_names = frozenset(t["name"] for t in tools)
    messages: list = [*(history or []), {"role": "user", "content": user}]
    # C2（探索ループの文脈整理）: `openai_style` と同じローカル調査状態＋ラウンド境界の追跡
    # （Anthropic 方言は1ターンにつき assistant 1件＋user(tool_result 配列) 1件＝計2メッセージが
    # 「組」——`_round_bounds` はメッセージの中身の形に依存せず `[start, end)` だけで組を表すため、
    # OpenAI 方言と同じロジックをそのまま使える）。
    state = investigation_state.InvestigationState(question=user, scope={"world": world, "layer": layer})
    for _kind in _unreachable_backends:
        # 不達でツール集合から外れたターンも縮退として1回だけ計数する（実行中の記録機会が無い）。
        state.mark_backend_failure(_kind)
    _prefix_len = len(messages)
    _round_bounds: list[tuple[int, int]] = []
    docs: set = set()
    cites: list = []
    cards: list = []
    searched = False
    usage = _new_usage_acc()                         # F3: 全ツールターンの usage を合算
    from . import store
    system_settings = store.get_system_settings()
    max_tools_per_turn = effective_max_tools_per_turn(system_settings)
    total_tool_bytes = 0                              # 1 run 累計の tool-result バイト量
    # BUDGET-1（§3.4）: run 開始時に1回だけ解決し、run の間ずっと使い回す（途中で admin が設定を
    # 変えても当該 run には影響しない）。BUDGET-2（§3.4）: provider="bedrock"（本アプリの
    # `anthropic_style` 唯一の呼び出し元）＋`client`（`.models.retrieve()` 照会用・現状
    # `AnthropicBedrock` は非対応のため実質 no-op・`model_windows.query_anthropic_context_length`
    # docstring 参照）を渡す。
    tool_result_max_bytes, tool_result_max_total_bytes = resolve_tool_result_budgets(
        system_settings=system_settings,
        provider="bedrock", model=model, anthropic_client=client)
    verified_docs: set = set()                        # EXT-2/EV-0: read_around で実際に精読した doc_id
    structural_evidence_meta: list = []       # list_docs/graph_neighbors の検証済み根拠 detail（Evidence ID 割当用）
    mt = max_tokens or _ANTHROPIC_MAX_TOKENS
    for _ in range(MAX_TURNS):
        if stop_event is not None and stop_event.is_set():
            return
        _round_start = len(messages)   # C2: この turn で足す assistant＋tool_result 組の開始位置
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
                                       structural_evidence_meta=structural_evidence_meta,
                                       read_evidence=_read_evidence_payload(state), gaps=state.gaps, limits=state.limits)
            return
        tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
        if not tool_uses or stop == "max_tokens":        # ツール要求なし／打ち切り＝集めたテキストを最終回答に
            text = "".join(getattr(b, "text", "") for b in blocks
                           if getattr(b, "type", None) == "text").strip()
            committed, evidence_meta, dropped = _commit_evidence(cites, world)
            _note_dropped_citations(state, dropped)
            if dropped:
                # 一部 citation が検証で落ちた: 通常の会話履歴・ツール履歴・落とした draft は使わず、
                # system＋現在の質問＋Committed Evidence digest だけでクリーンな最終合成コンテキストを
                # 再構築する（`openai_style` と同じ方針）。再合成できなければ本文を返さない
                # （honest failure）。
                candidate_text, _finish_reason = _clean_resynthesis_anthropic(
                    client, model, system, user, mt, committed, usage, evidence_meta=evidence_meta,
                    structural_evidence_meta=structural_evidence_meta, gaps=state.gaps,
                    read_evidence=_read_evidence_payload(state), limits=state.limits)
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
                                    attributed_ev_ids=_attributed,
                                    read_evidence=_read_evidence_payload(state), gaps=state.gaps, limits=state.limits)
            return
        searched = True
        messages.append({"role": "assistant", "content": resp.content})   # ブロックはそのまま履歴へ戻す
        results = []                                     # 全ツール結果を **1つの** user メッセージで返す
        # openai_style と同じ上限を適用する（`MAX_TURNS` は
        # 応答ラウンド数だけを制限し、1応答内の tool_use 実行数自体は制限しない）。
        # 超過分は `_pending_calls`
        # （`tool_uses[:max_tools_per_turn]`）で単純に切り捨てる（超過件数分のノードを生成しない＝
        # 下のループ後に固定ノード1件だけ流す）。
        _pending_calls = tool_uses[:max_tools_per_turn]
        over_limit = len(tool_uses) > max_tools_per_turn
        # D1（ツール並列・同一応答内の独立した読み取りの同時実行）: ask_user を含まず・呼び出しが
        # 2本以上あるときだけ並列にする（`SHERPA_TOOL_PARALLEL<=1` は常にこの分岐へ入らない＝
        # 従来どおり直列）。ask_user を含む応答は question 優先の早期 return 契約（下の直列ループ）を
        # 変えないため対象外にする。
        _has_ask_user = any(getattr(tu, "name", None) == "ask_user" for tu in _pending_calls)
        _use_parallel = SHERPA_TOOL_PARALLEL > 1 and len(_pending_calls) >= 2 and not _has_ask_user
        _tool_batch_started = time.monotonic()
        if _use_parallel:
            # 呼び出しと結果の対応は元の呼び出し順で保つ（完了順ではない）——`_futures` は投入順
            # そのまま積み、後段の結果処理も同じ順で走査する。ワーカーは `run_tool` の戻り値を
            # 返すだけで、docs/cites/cards/state/results/バイト予算の更新は全てこのメインスレッド
            # で行う。
            from .ingest.world_neo4j import GraphSchemaEraError   # 遅延 import（他の遅延 import と同じ理由）
            _futures: list = []   # (kind, tu, name, args, payload)・kind="rejected"|"run"
            _stopped = False
            _executor = concurrent.futures.ThreadPoolExecutor(max_workers=SHERPA_TOOL_PARALLEL)
            try:
                for tu in _pending_calls:
                    # 各呼び出しの投入前に stop_event を確認する——立っていれば未投入分は投入しない。
                    # 投入済み（既に active ノードを yield 済み）は下の `finally` で完了を待つ。
                    if stop_event is not None and stop_event.is_set():
                        _stopped = True
                        break
                    name = getattr(tu, "name", None)
                    args = getattr(tu, "input", None) or {}      # SDK ではパース済み dict
                    if not isinstance(args, dict):
                        args = {}
                    if name not in offered_names:
                        yield {"node": _node("許可外のツール呼び出し", "許可されていないため拒否しました")}
                        safe_name = _clip_utf8_bytes(str(name or ""), _REJECTED_TOOL_NAME_MAX_BYTES)
                        _futures.append(("rejected", tu, name, args, safe_name))
                        continue
                    yield {"node": _tool_node(name, args)}
                    # ノード yield 直後（generator 再開後）にも stop_event を再確認する（単体呼び出し
                    # 時と同じ LOW-E の窓塞ぎ）。
                    if stop_event is not None and stop_event.is_set():
                        _stopped = True
                        break
                    # `ThreadPoolExecutor` は呼び出し元スレッドの `contextvars.Context`（
                    # `worlds.pin_world_root` の pin 等）を継承しない——`copy_context().run(...)` で
                    # ワーカーへ明示的に持ち込む（さもないと pin が見えず別 root/fallback を解決しうる）。
                    _ctx = contextvars.copy_context()
                    fut = _executor.submit(_ctx.run, run_tool, name, args, world, scope_paths,
                                           layer=layer, tool_result_max_bytes=tool_result_max_bytes)
                    _futures.append(("run", tu, name, args, fut))
            finally:
                _executor.shutdown(wait=True)
            # 投入完了後・結果収集前にも stop_event を再確認する——全件投入済みで待機中に停止要求が
            # 来ると `_stopped`（投入時にしか更新しない）は偽のままのため、ここで別途確認しないと
            # 停止後に結果を results/state へ積み done ノードを yield してしまう。
            if _stopped or (stop_event is not None and stop_event.is_set()):
                return   # 停止契約: 結果は results に積まず final も出さない（既存の stop_event 契約と同型）。
            for kind, tu, name, args, payload in _futures:
                if kind == "rejected":
                    safe_name = payload
                    result = {"error": f"ツール {safe_name} は使用できません"}
                    total_tool_bytes += _result_byte_size(result)
                    if total_tool_bytes > tool_result_max_total_bytes:
                        state.mark_limit("total_budget_hit")
                        yield {"node": _node("ツール結果の合計サイズ上限",
                                             "この会話で取得した量が多すぎるため打ち切りました")}
                        yield _build_final_payload("", docs, searched, cites, cards, _usage_or_none(usage),
                                                   verified_docs, "budget_exceeded", world,
                                                   structural_evidence_meta=structural_evidence_meta,
                                                   read_evidence=_read_evidence_payload(state), gaps=state.gaps, limits=state.limits)
                        return
                    results.append({"type": "tool_result", "tool_use_id": getattr(tu, "id", None),
                                    "content": json.dumps(result, ensure_ascii=False)})
                    continue
                try:
                    result, d, c, cd = payload.result()
                except GraphSchemaEraError:
                    # 旧世代グラフ→再取り込み案内で停止する既存契約（`providers/base.py::_agentic_run`
                    # の `except GraphSchemaEraError: raise` 参照）——通常のツールエラーへ丸めず、
                    # 直列時と同じくそのまま再送出する（他の呼び出しは既に並走して完了済みでも
                    # run 全体を止める・fail-loud）。
                    raise
                except Exception as e:
                    # ワーカー（`run_tool`）の例外はこの呼び出しだけの error 結果に変換する
                    # （他の呼び出しは既に並走して完了済み・止めない）。生の例外文字列（絶対パス
                    # 等を含みうる）は次ターンの外部 LLM 送信本文へは出さず、固定文言にする——
                    # 詳細はマスク済みのサーバーログにだけ残す（`openai_style` と同じ流儀。
                    # Anthropic 経路は raw headers を持たない＝secret 抽出対象が無い）。
                    from .ingest.graph_extract import _log_masked_exception
                    _log_masked_exception(_log, f"agentic_search: tool 実行に失敗（{name}）", e, None)
                    result, d, c, cd = ({"error": "ツール実行に失敗しました"}, set(), [], [])
                _record_run_tool_limits(state, name, result)     # 並列経路も直列と同じ計測
                _record_tool_result_error_code(state, result, name)    # 結果化済み障害（read_io/es/graph 等）の反映
                hit_node = _hit_summary_node(name, args, result)
                if hit_node:
                    yield {"node": hit_node}
                degrade_node = _degrade_result_node(result)
                if degrade_node:
                    yield {"node": degrade_node}
                truncated_node = _truncated_docs_node(result)
                if truncated_node:
                    yield {"node": truncated_node}
                total_tool_bytes += _result_byte_size(result) + _result_byte_size(cd)
                if total_tool_bytes > tool_result_max_total_bytes:
                    state.mark_limit("total_budget_hit")
                    yield {"node": _node("ツール結果の合計サイズ上限",
                                         "この会話で取得した量が多すぎるため打ち切りました")}
                    yield _build_final_payload("", docs, searched, cites, cards, _usage_or_none(usage),
                                               verified_docs, "budget_exceeded", world,
                                               structural_evidence_meta=structural_evidence_meta,
                                               read_evidence=_read_evidence_payload(state), gaps=state.gaps, limits=state.limits)
                    return
                docs |= d
                cites += c
                cards += cd
                if name in _VERIFIED_READ_TOOLS and "error" not in result:
                    verified_docs |= d
                _call_structural: list = []
                if name == "list_docs" and "error" not in result:
                    _matched = [doc.get("rel_path") for doc in (result.get("docs") or [])
                               if doc.get("rel_path")]
                    _call_structural.append({
                        "doc_id": None, "span": None, "verification_method": "list_docs_verified",
                        "list_meta": {"count": result.get("count", 0), "shown": len(_matched),
                                      "prefix": str(args.get("path_prefix") or "").strip(),
                                      "pattern": str(args.get("name_pattern") or "").strip(),
                                      "doctype": str(args.get("doctype") or "").strip(),
                                      "state": str(args.get("state") or "").strip()},
                        "matched_doc_ids": _matched})
                if name == "folder_tree" and "error" not in result:
                    _call_structural.append({
                        "doc_id": None, "span": None, "verification_method": "folder_tree_verified",
                        "tree_meta": {"prefix": result.get("path_prefix", ""), "depth": result.get("depth"),
                                     "count": result.get("count", 0),
                                     "shown": len(result.get("folders") or [])},
                        "matched_doc_ids": []})
                if name == "graph_neighbors" and cd:
                    _call_structural += _card_structural_evidence(cd)
                structural_evidence_meta += _call_structural
                state.add_tool_result(name, args, result, c, _call_structural)
                results.append({"type": "tool_result", "tool_use_id": getattr(tu, "id", None),
                                "content": json.dumps(result, ensure_ascii=False)})
        for tu in ([] if _use_parallel else _pending_calls):
            # 各ツール実行の直前に stop_event を確認する。
            if stop_event is not None and stop_event.is_set():
                return
            name = getattr(tu, "name", None)
            args = getattr(tu, "input", None) or {}      # SDK ではパース済み dict
            if not isinstance(args, dict):
                args = {}
            if name not in offered_names:
                # 提示していない
                # ツール名の実行は拒否する（openai_style/gemini と同じ固定文言・safe_name クリップ・
                # total_tool_bytes 累計計上）。
                yield {"node": _node("許可外のツール呼び出し", "許可されていないため拒否しました")}
                safe_name = _clip_utf8_bytes(str(name or ""), _REJECTED_TOOL_NAME_MAX_BYTES)
                result = {"error": f"ツール {safe_name} は使用できません"}
                total_tool_bytes += _result_byte_size(result)
                if total_tool_bytes > tool_result_max_total_bytes:
                    state.mark_limit("total_budget_hit")
                    yield {"node": _node("ツール結果の合計サイズ上限",
                                         "この会話で取得した量が多すぎるため打ち切りました")}
                    yield _build_final_payload("", docs, searched, cites, cards, _usage_or_none(usage),
                                               verified_docs, "budget_exceeded", world,
                                               structural_evidence_meta=structural_evidence_meta,
                                               read_evidence=_read_evidence_payload(state), gaps=state.gaps, limits=state.limits)
                    return
                results.append({"type": "tool_result", "tool_use_id": getattr(tu, "id", None),
                                "content": json.dumps(result, ensure_ascii=False)})
                continue
            yield {"node": _tool_node(name, args)}
            # ノード yield 直後にも stop_event を
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
            _record_run_tool_limits(state, name, result)
            _record_tool_result_error_code(state, result, name)    # 結果化済み障害（read_io/es/graph 等）の反映
            # 「何を探して・いくつ当たったか」の追加ノード（`_tool_node` は結果が出る前のノード
            # のため件数を書けない・`_hit_summary_node` 参照）。`anthropic_style` に
            # `allowed_tools`/サブ経路は無い＝常にメイン経路の表示。
            hit_node = _hit_summary_node(name, args, result)
            if hit_node:
                yield {"node": hit_node}
            # es_search が BM25 のみへ縮退した場合、その理由を「思考の
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
            # 1 run 累計の tool-result バイト量が
            # 上限を超えたら、この結果は破棄し固定エラーで run を打ち切る（fail-closed）。
            # `cd`（cards サイドカー・`run_tool` 側で
            # 既に件数＋バイト上限クリップ済み＝`_clip_cards` 参照）の直列化バイトも累計へ計上する
            # （`result` のみを計測すると、cards はこの計測経路をすり抜けてしまう）。
            total_tool_bytes += _result_byte_size(result) + _result_byte_size(cd)
            if total_tool_bytes > tool_result_max_total_bytes:
                state.mark_limit("total_budget_hit")
                yield {"node": _node("ツール結果の合計サイズ上限",
                                     "この会話で取得した量が多すぎるため打ち切りました")}
                yield _build_final_payload("", docs, searched, cites, cards, _usage_or_none(usage),
                                           verified_docs, "budget_exceeded", world,
                                           structural_evidence_meta=structural_evidence_meta,
                                           read_evidence=_read_evidence_payload(state), gaps=state.gaps, limits=state.limits)
                return
            docs |= d
            cites += c
            cards += cd
            # EXT-2/EV-0（拡張設計 §4.4）: read_around/read_doc を実際に呼んだ doc_id だけを
            # 「精読済み」にタグ付ける（`openai_style` と同じ規則）。
            if name in _VERIFIED_READ_TOOLS and "error" not in result:
                verified_docs |= d
            # list_docs／graph_neighbors は citation を生成しないが、それ自体が根拠として正当
            # （`openai_style` と同じ規則・§1 参照）。
            # C2: この呼び出し1回分の構造的根拠だけを先にローカルへ集め（`state.add_tool_result` は
            # 「この呼び出しで新たに得た分」だけを見る契約）、その後にターン全体の累積へ合流する。
            _call_structural: list = []
            if name == "list_docs" and "error" not in result:
                # EV-0（拡張設計 §4.4）: list_docs は**呼び出し単位で集計した1 Evidence**とする
                # （総件数・適用条件・列挙範囲＋列挙した各パス）——0件の呼び出しも「該当0件」という
                # 具体的な事実として1 Evidence（ev-N）を持つ（根拠ゲート・帰属の対象になる）。
                _matched = [doc.get("rel_path") for doc in (result.get("docs") or [])
                           if doc.get("rel_path")]
                _call_structural.append({
                    "doc_id": None, "span": None, "verification_method": "list_docs_verified",
                    "list_meta": {"count": result.get("count", 0), "shown": len(_matched),
                                  "prefix": str(args.get("path_prefix") or "").strip(),
                                  "pattern": str(args.get("name_pattern") or "").strip(),
                                  "doctype": str(args.get("doctype") or "").strip(),
                                  "state": str(args.get("state") or "").strip()},
                    "matched_doc_ids": _matched})
            if name == "folder_tree" and "error" not in result:
                # folder_tree（K6・doc_ledger 走査による決定的集計・LLM
                # 不使用）も list_docs と同じ「呼び出し単位で集計した1 Evidence」として構造 Evidence
                # 化する。フォルダは doc ではない（`run_tool` 参照＝`docs` 集合には何も足さない）ため
                # `matched_doc_ids` は常に空リスト——裏付け doc の代わりに集計事実そのものが根拠。
                _call_structural.append({
                    "doc_id": None, "span": None, "verification_method": "folder_tree_verified",
                    "tree_meta": {"prefix": result.get("path_prefix", ""), "depth": result.get("depth"),
                                 "count": result.get("count", 0),
                                 "shown": len(result.get("folders") or [])},
                    "matched_doc_ids": []})
            if name == "graph_neighbors" and cd:
                # `run_tool` が既にカード単位で裏付け doc を検証済み（無効カードは cd に含まれない・
                # `d` はその検証済み doc_id 集合そのもの）——ここで再検証しない。裏付け doc を
                # 主張しないカード（純粋なグラフ位相情報）は、Neo4j から実際に返ったノードである
                # こと自体を source_type=graph の構造 Evidence として計上する。
                _call_structural += _card_structural_evidence(cd)
            structural_evidence_meta += _call_structural
            state.add_tool_result(name, args, result, c, _call_structural)
            results.append({"type": "tool_result", "tool_use_id": getattr(tu, "id", None),
                            "content": json.dumps(result, ensure_ascii=False)})
        # D2（計測）: ラウンドごとに件数・並列度・所要時間だけを1行残す（本文は出さない）。
        _log.debug("tool batch: n=%d parallel=%d elapsed=%.2fs", len(_pending_calls),
                  (SHERPA_TOOL_PARALLEL if _use_parallel else 1), time.monotonic() - _tool_batch_started)
        if over_limit:
            # レビュー是正（LOW-D）: 超過件数に関わらず固定ノード1件だけ生成する。
            yield {"node": _node("ツール呼び出し上限", "1回の応答あたりの実行数上限に達したため打ち切りました")}
            # 上限超過＝以降のターンへは進まず、ここで打ち切る（未処理分の
            # tool_result が欠けたまま Anthropic API へ送り返さない＝プロトコル違反も避けられる）。
            yield _build_final_payload("", docs, searched, cites, cards, _usage_or_none(usage),
                                       verified_docs, "tools_per_turn_exceeded", world,
                                       structural_evidence_meta=structural_evidence_meta,
                                       read_evidence=_read_evidence_payload(state), gaps=state.gaps, limits=state.limits)
            return
        messages.append({"role": "user", "content": results})
        _round_bounds.append((_round_start, len(messages)))
        # C2（探索ループの文脈整理）: `openai_style` と同じ判定・置換（この方言は1組＝
        # assistant 1件＋user(tool_result 配列) 1件の2メッセージ）。
        if (len(_round_bounds) > SHERPA_AGENTIC_KEEP_RECENT_TOOLS
                and _messages_byte_size(messages) > SHERPA_AGENTIC_CONTEXT_BUDGET_BYTES):
            _cutoff = _round_bounds[-SHERPA_AGENTIC_KEEP_RECENT_TOOLS][0]
            _collapsed_rounds = len(_round_bounds) - SHERPA_AGENTIC_KEEP_RECENT_TOOLS
            _summary = state.render(max_bytes=SHERPA_AGENTIC_CONTEXT_BUDGET_BYTES // 2,
                                    keep_recent_tools=SHERPA_AGENTIC_KEEP_RECENT_TOOLS)
            messages[_prefix_len:_cutoff] = [
                {"role": "user", "content": f"【ここまでの調査状態】\n{_summary}"}]
            _shift = (_cutoff - _prefix_len) - 1
            _round_bounds = [(s - _shift, e - _shift) for s, e in _round_bounds[_collapsed_rounds:]]
            state.bump_limit("context_compactions")
            yield {"node": _context_compacted_node(_collapsed_rounds)}
    yield _build_final_payload("", docs, searched, cites, cards, _usage_or_none(usage),
                               verified_docs, "turns_exhausted", world,
                               structural_evidence_meta=structural_evidence_meta,
                               read_evidence=_read_evidence_payload(state), gaps=state.gaps, limits=state.limits)


def gemini(api_key: str, model: str, system: str, user: str, world: str, scope_paths,
           toolset: list | None = None, stop_event=None, can_ask: bool = True,
           history: list | None = None, layer=None, tools_pref: dict | None = None,
           tools_availability: dict | None = None):
    """Gemini の function-calling を反復。`{"node":..}` を yield しつつ最後に `{"final","docs"}`。

    `layer`（省略可・既定 `None`＝`"both"`＝既存呼び出し元は無変更）: `openai_style` と同じ探す対象
    フィルタ（調べ方ブロック §3.4）。`run_tool` へそのまま転送する。

    `tools_pref`/`tools_availability`（省略可・既定 `None`・SC-6e）: `openai_style` と
    同じ検索経路トグル／可用性 snapshot（`toolset` 明示指定時はどちらも無視される）。

    `stop_event`（UI フィードバック1「途中停止」）: `openai_style` と
    同じ意味論＝各ターンのリクエスト発行前に確認し、立っていれば以降のリクエストを発行せず終了する。
    `history`（R1a・会話継続）: 直前ターンの (user, assistant) 対（時系列順・上流でキャップ済み）。
    Gemini の role（assistant→model）にマップして現在の user の前に並べる。省略/空なら従来と完全
    同一の初期 contents になる。`graph_admin.ask_graph` は位置引数で呼ぶため本引数に触れない
    ＝既定 None（空）で後方互換。

    実際に提示した `tools` から
    ツール名集合 `offered_names` を導出し、モデルが提示していないツール名を呼んでも `run_tool` を
    実行せず拒否する（`openai_style` と同じ対称化。Gemini は元々 `allowed_tools` 引数を持たない＝
    常に `offered_names` を allowlist として使う）。

    `SHERPA_TOOL_PARALLEL`（D1・ツール並列）: `openai_style` と同じ（functionCall が2本以上・
    ask_user を含まないときだけ同時実行）。Gemini の functionResponse は id を持たないため、
    完了順に関わらず要求と同じ順に `resp_parts` を組むことが対応の唯一の手がかりになる。
    """
    url = llm.gemini_url(model)
    headers = llm.gemini_headers(api_key)
    _tp = tools_pref_mod.normalize_tools_pref(tools_pref)
    _unreachable_backends: list = []   # 実接続で不達のバックエンド（利用者の OFF とは区別する）
    if toolset is not None:
        tools = toolset                # SC-6e: 明示指定時は可用性判定を一切参照しない（docstring 参照）
    else:
        _avail = tools_availability if tools_availability is not None else tool_availability()   # SC-6e
        _unreachable_backends = _unreachable_backends_at_start(_avail, _tp)
        tools = gemini_tools(
            with_es=_avail["fulltext"] and _tp["fulltext"], with_graph=_avail["graph"] and _tp["graph"],
            can_ask=can_ask, with_grep=_tp["grep"])
    offered_names = frozenset(
        fn.get("name") for group in tools for fn in (group.get("functionDeclarations") or []))
    contents = [{"role": ("model" if h.get("role") == "assistant" else "user"),
                "parts": [{"text": h.get("content", "")}]} for h in (history or [])]
    contents.append({"role": "user", "parts": [{"text": user}]})
    # C2（探索ループの文脈整理）: `openai_style`/`anthropic_style` と同じローカル調査状態＋
    # ラウンド境界の追跡（この方言は1ターンにつき role=model 1件＋role=user(functionResponse 配列)
    # 1件＝計2メッセージが「組」）。
    state = investigation_state.InvestigationState(question=user, scope={"world": world, "layer": layer})
    for _kind in _unreachable_backends:
        # 不達でツール集合から外れたターンも縮退として1回だけ計数する（実行中の記録機会が無い）。
        state.mark_backend_failure(_kind)
    _prefix_len = len(contents)
    _round_bounds: list[tuple[int, int]] = []
    docs: set = set()
    cites: list = []
    cards: list = []
    searched = False
    usage = _new_usage_acc()                   # F3: 全ツールターンの usage を合算
    from . import store
    system_settings = store.get_system_settings()
    max_tools_per_turn = effective_max_tools_per_turn(system_settings)
    total_tool_bytes = 0                        # 1 run 累計の tool-result バイト量
    # BUDGET-1（§3.4）: run 開始時に1回だけ解決し、run の間ずっと使い回す（途中で admin が設定を
    # 変えても当該 run には影響しない）。BUDGET-2（§3.4）: provider="gemini"（現状ライブ窓照会も
    # シード表も対象外＝登録値/不明のみを通る・管理画面の登録欄で上書き可能）。
    tool_result_max_bytes, tool_result_max_total_bytes = resolve_tool_result_budgets(
        system_settings=system_settings,
        provider="gemini", model=model)
    verified_docs: set = set()                  # EXT-2/EV-0: read_around で実際に精読した doc_id
    structural_evidence_meta: list = []       # list_docs/graph_neighbors の検証済み根拠 detail（Evidence ID 割当用）
    for _ in range(MAX_TURNS):
        if stop_event is not None and stop_event.is_set():
            return
        _round_start = len(contents)   # C2: この turn で足す model＋functionResponse 組の開始位置
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
            _note_dropped_citations(state, dropped)
            if dropped:
                # 一部 citation が検証で落ちた: 通常の会話履歴・ツール履歴・落とした draft は使わず、
                # system＋現在の質問＋Committed Evidence digest だけでクリーンな最終合成コンテキストを
                # 再構築する（`openai_style` と同じ方針）。再合成できなければ本文を返さない
                # （honest failure）。
                candidate_text, _finish_reason = _clean_resynthesis_gemini(
                    url, headers, system, user, committed, usage, evidence_meta=evidence_meta,
                    structural_evidence_meta=structural_evidence_meta, gaps=state.gaps,
                    read_evidence=_read_evidence_payload(state), limits=state.limits)
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
                                    attributed_ev_ids=_attributed,
                                    read_evidence=_read_evidence_payload(state), gaps=state.gaps, limits=state.limits)
            return
        searched = True
        contents.append({"role": "model", "parts": parts})
        resp_parts = []
        # openai_style/anthropic_style と同じ上限を適用する。
        # 超過分は `_pending_calls`
        # （`calls[:max_tools_per_turn]`）で単純に切り捨てる（超過件数分のノードを生成しない＝
        # 下のループ後に固定ノード1件だけ流す）。
        _pending_calls = calls[:max_tools_per_turn]
        over_limit = len(calls) > max_tools_per_turn
        # D1（ツール並列・同一応答内の独立した読み取りの同時実行）: ask_user を含まず・呼び出しが
        # 2本以上あるときだけ並列にする（`SHERPA_TOOL_PARALLEL<=1` は常にこの分岐へ入らない＝
        # 従来どおり直列）。ask_user を含む応答は question 優先の早期 return 契約（下の直列ループ）を
        # 変えないため対象外にする。
        _has_ask_user = any(fc.get("name") == "ask_user" for fc in _pending_calls)
        _use_parallel = SHERPA_TOOL_PARALLEL > 1 and len(_pending_calls) >= 2 and not _has_ask_user
        _tool_batch_started = time.monotonic()
        if _use_parallel:
            # 呼び出しと結果の対応は元の呼び出し順で保つ（完了順ではない・Gemini の functionResponse
            # は id を持たないため要求順そのものが対応の唯一の手がかり）——`_futures` は投入順
            # そのまま積み、後段の結果処理も同じ順で走査する。ワーカーは `run_tool` の戻り値を
            # 返すだけで、docs/cites/cards/state/resp_parts/バイト予算の更新は全てこのメイン
            # スレッドで行う。
            from .ingest.world_neo4j import GraphSchemaEraError   # 遅延 import（他の遅延 import と同じ理由）
            _futures: list = []   # (kind, fc, name, args, payload)・kind="rejected"|"run"
            _stopped = False
            _executor = concurrent.futures.ThreadPoolExecutor(max_workers=SHERPA_TOOL_PARALLEL)
            try:
                for fc in _pending_calls:
                    # 各呼び出しの投入前に stop_event を確認する——立っていれば未投入分は投入しない。
                    # 投入済み（既に active ノードを yield 済み）は下の `finally` で完了を待つ。
                    if stop_event is not None and stop_event.is_set():
                        _stopped = True
                        break
                    name = fc.get("name")
                    args = fc.get("args") or {}
                    if name not in offered_names:
                        yield {"node": _node("許可外のツール呼び出し", "許可されていないため拒否しました")}
                        safe_name = _clip_utf8_bytes(str(name or ""), _REJECTED_TOOL_NAME_MAX_BYTES)
                        _futures.append(("rejected", fc, name, args, safe_name))
                        continue
                    yield {"node": _tool_node(name, args)}
                    # ノード yield 直後（generator 再開後）にも stop_event を再確認する（単体呼び出し
                    # 時と同じ LOW-E の窓塞ぎ）。
                    if stop_event is not None and stop_event.is_set():
                        _stopped = True
                        break
                    # `ThreadPoolExecutor` は呼び出し元スレッドの `contextvars.Context`（
                    # `worlds.pin_world_root` の pin 等）を継承しない——`copy_context().run(...)` で
                    # ワーカーへ明示的に持ち込む（さもないと pin が見えず別 root/fallback を解決しうる）。
                    _ctx = contextvars.copy_context()
                    fut = _executor.submit(_ctx.run, run_tool, name, args, world, scope_paths,
                                           layer=layer, tool_result_max_bytes=tool_result_max_bytes)
                    _futures.append(("run", fc, name, args, fut))
            finally:
                _executor.shutdown(wait=True)
            # 投入完了後・結果収集前にも stop_event を再確認する——全件投入済みで待機中に停止要求が
            # 来ると `_stopped`（投入時にしか更新しない）は偽のままのため、ここで別途確認しないと
            # 停止後に結果を resp_parts/state へ積み done ノードを yield してしまう。
            if _stopped or (stop_event is not None and stop_event.is_set()):
                return   # 停止契約: 結果は resp_parts に積まず final も出さない（既存の stop_event 契約と同型）。
            for kind, fc, name, args, payload in _futures:
                if kind == "rejected":
                    safe_name = payload
                    result = {"error": f"ツール {safe_name} は使用できません"}
                    total_tool_bytes += _result_byte_size(result)
                    if total_tool_bytes > tool_result_max_total_bytes:
                        state.mark_limit("total_budget_hit")
                        yield {"node": _node("ツール結果の合計サイズ上限",
                                             "この会話で取得した量が多すぎるため打ち切りました")}
                        yield _build_final_payload("", docs, searched, cites, cards, _usage_or_none(usage),
                                                   verified_docs, "budget_exceeded", world,
                                                   structural_evidence_meta=structural_evidence_meta,
                                                   read_evidence=_read_evidence_payload(state), gaps=state.gaps, limits=state.limits)
                        return
                    resp_parts.append({"functionResponse": {"name": name, "response": result}})
                    continue
                try:
                    result, d, c, cd = payload.result()
                except GraphSchemaEraError:
                    # 旧世代グラフ→再取り込み案内で停止する既存契約（`providers/base.py::_agentic_run`
                    # の `except GraphSchemaEraError: raise` 参照）——通常のツールエラーへ丸めず、
                    # 直列時と同じくそのまま再送出する（他の呼び出しは既に並走して完了済みでも
                    # run 全体を止める・fail-loud）。
                    raise
                except Exception as e:
                    # ワーカー（`run_tool`）の例外はこの呼び出しだけの error 結果に変換する
                    # （他の呼び出しは既に並走して完了済み・止めない）。生の例外文字列（絶対パス
                    # 等を含みうる）は次ターンの外部 LLM 送信本文へは出さず、固定文言にする——
                    # 詳細はマスク済みのサーバーログにだけ残す（`openai_style` と同じ流儀。
                    # `api_key` は実キーそのもの＝`_log_masked_exception` の secret へそのまま渡せる）。
                    from .ingest.graph_extract import _log_masked_exception
                    _log_masked_exception(_log, f"agentic_search: tool 実行に失敗（{name}）", e, api_key)
                    result, d, c, cd = ({"error": "ツール実行に失敗しました"}, set(), [], [])
                _record_run_tool_limits(state, name, result)     # 並列経路も直列と同じ計測
                _record_tool_result_error_code(state, result, name)    # 結果化済み障害（read_io/es/graph 等）の反映
                hit_node = _hit_summary_node(name, args, result)
                if hit_node:
                    yield {"node": hit_node}
                degrade_node = _degrade_result_node(result)
                if degrade_node:
                    yield {"node": degrade_node}
                truncated_node = _truncated_docs_node(result)
                if truncated_node:
                    yield {"node": truncated_node}
                total_tool_bytes += _result_byte_size(result) + _result_byte_size(cd)
                if total_tool_bytes > tool_result_max_total_bytes:
                    state.mark_limit("total_budget_hit")
                    yield {"node": _node("ツール結果の合計サイズ上限",
                                         "この会話で取得した量が多すぎるため打ち切りました")}
                    yield _build_final_payload("", docs, searched, cites, cards, _usage_or_none(usage),
                                               verified_docs, "budget_exceeded", world,
                                               structural_evidence_meta=structural_evidence_meta,
                                               read_evidence=_read_evidence_payload(state), gaps=state.gaps, limits=state.limits)
                    return
                docs |= d
                cites += c
                cards += cd
                if name in _VERIFIED_READ_TOOLS and "error" not in result:
                    verified_docs |= d
                _call_structural: list = []
                if name == "list_docs" and "error" not in result:
                    _matched = [doc.get("rel_path") for doc in (result.get("docs") or [])
                               if doc.get("rel_path")]
                    _call_structural.append({
                        "doc_id": None, "span": None, "verification_method": "list_docs_verified",
                        "list_meta": {"count": result.get("count", 0), "shown": len(_matched),
                                      "prefix": str(args.get("path_prefix") or "").strip(),
                                      "pattern": str(args.get("name_pattern") or "").strip(),
                                      "doctype": str(args.get("doctype") or "").strip(),
                                      "state": str(args.get("state") or "").strip()},
                        "matched_doc_ids": _matched})
                if name == "folder_tree" and "error" not in result:
                    _call_structural.append({
                        "doc_id": None, "span": None, "verification_method": "folder_tree_verified",
                        "tree_meta": {"prefix": result.get("path_prefix", ""), "depth": result.get("depth"),
                                     "count": result.get("count", 0),
                                     "shown": len(result.get("folders") or [])},
                        "matched_doc_ids": []})
                if name == "graph_neighbors" and cd:
                    _call_structural += _card_structural_evidence(cd)
                structural_evidence_meta += _call_structural
                state.add_tool_result(name, args, result, c, _call_structural)
                resp_parts.append({"functionResponse": {"name": name, "response": result}})
        for fc in ([] if _use_parallel else _pending_calls):
            # 各ツール実行の直前に stop_event を確認する。
            if stop_event is not None and stop_event.is_set():
                return
            name = fc.get("name")
            args = fc.get("args") or {}
            if name not in offered_names:
                # 提示していない
                # ツール名の実行は拒否する（openai_style と同じ固定文言・safe_name クリップ・
                # total_tool_bytes 累計計上）。
                yield {"node": _node("許可外のツール呼び出し", "許可されていないため拒否しました")}
                safe_name = _clip_utf8_bytes(str(name or ""), _REJECTED_TOOL_NAME_MAX_BYTES)
                result = {"error": f"ツール {safe_name} は使用できません"}
                total_tool_bytes += _result_byte_size(result)
                if total_tool_bytes > tool_result_max_total_bytes:
                    state.mark_limit("total_budget_hit")
                    yield {"node": _node("ツール結果の合計サイズ上限",
                                         "この会話で取得した量が多すぎるため打ち切りました")}
                    yield _build_final_payload("", docs, searched, cites, cards, _usage_or_none(usage),
                                               verified_docs, "budget_exceeded", world,
                                               structural_evidence_meta=structural_evidence_meta,
                                               read_evidence=_read_evidence_payload(state), gaps=state.gaps, limits=state.limits)
                    return
                resp_parts.append({"functionResponse": {"name": name, "response": result}})
                continue
            yield {"node": _tool_node(name, args)}
            # ノード yield 直後にも stop_event を
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
            _record_run_tool_limits(state, name, result)
            _record_tool_result_error_code(state, result, name)    # 結果化済み障害（read_io/es/graph 等）の反映
            # 「何を探して・いくつ当たったか」の追加ノード（`_tool_node` は結果が出る前のノード
            # のため件数を書けない・`_hit_summary_node` 参照）。`gemini` に `allowed_tools`/
            # サブ経路は無い＝常にメイン経路の表示。
            hit_node = _hit_summary_node(name, args, result)
            if hit_node:
                yield {"node": hit_node}
            # es_search が BM25 のみへ縮退した場合、その理由を「思考の
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
            # 1 run 累計の tool-result バイト量が
            # 上限を超えたら、この結果は破棄し固定エラーで run を打ち切る（fail-closed）。
            # `cd`（cards サイドカー・`run_tool` 側で
            # 既に件数＋バイト上限クリップ済み＝`_clip_cards` 参照）の直列化バイトも累計へ計上する
            # （`result` のみを計測すると、cards はこの計測経路をすり抜けてしまう）。
            total_tool_bytes += _result_byte_size(result) + _result_byte_size(cd)
            if total_tool_bytes > tool_result_max_total_bytes:
                state.mark_limit("total_budget_hit")
                yield {"node": _node("ツール結果の合計サイズ上限",
                                     "この会話で取得した量が多すぎるため打ち切りました")}
                yield _build_final_payload("", docs, searched, cites, cards, _usage_or_none(usage),
                                           verified_docs, "budget_exceeded", world,
                                           structural_evidence_meta=structural_evidence_meta,
                                           read_evidence=_read_evidence_payload(state), gaps=state.gaps, limits=state.limits)
                return
            docs |= d
            cites += c
            cards += cd
            # EXT-2/EV-0（拡張設計 §4.4）: read_around/read_doc を実際に呼んだ doc_id だけを
            # 「精読済み」にタグ付ける（`openai_style` と同じ規則）。
            if name in _VERIFIED_READ_TOOLS and "error" not in result:
                verified_docs |= d
            # list_docs／graph_neighbors は citation を生成しないが、それ自体が根拠として正当
            # （`openai_style` と同じ規則・§1 参照）。
            # C2: この呼び出し1回分の構造的根拠だけを先にローカルへ集め（`state.add_tool_result` は
            # 「この呼び出しで新たに得た分」だけを見る契約）、その後にターン全体の累積へ合流する。
            _call_structural: list = []
            if name == "list_docs" and "error" not in result:
                # EV-0（拡張設計 §4.4）: list_docs は**呼び出し単位で集計した1 Evidence**とする
                # （総件数・適用条件・列挙範囲＋列挙した各パス）——0件の呼び出しも「該当0件」という
                # 具体的な事実として1 Evidence（ev-N）を持つ（根拠ゲート・帰属の対象になる）。
                _matched = [doc.get("rel_path") for doc in (result.get("docs") or [])
                           if doc.get("rel_path")]
                _call_structural.append({
                    "doc_id": None, "span": None, "verification_method": "list_docs_verified",
                    "list_meta": {"count": result.get("count", 0), "shown": len(_matched),
                                  "prefix": str(args.get("path_prefix") or "").strip(),
                                  "pattern": str(args.get("name_pattern") or "").strip(),
                                  "doctype": str(args.get("doctype") or "").strip(),
                                  "state": str(args.get("state") or "").strip()},
                    "matched_doc_ids": _matched})
            if name == "folder_tree" and "error" not in result:
                # folder_tree（K6・doc_ledger 走査による決定的集計・LLM
                # 不使用）も list_docs と同じ「呼び出し単位で集計した1 Evidence」として構造 Evidence
                # 化する。フォルダは doc ではない（`run_tool` 参照＝`docs` 集合には何も足さない）ため
                # `matched_doc_ids` は常に空リスト——裏付け doc の代わりに集計事実そのものが根拠。
                _call_structural.append({
                    "doc_id": None, "span": None, "verification_method": "folder_tree_verified",
                    "tree_meta": {"prefix": result.get("path_prefix", ""), "depth": result.get("depth"),
                                 "count": result.get("count", 0),
                                 "shown": len(result.get("folders") or [])},
                    "matched_doc_ids": []})
            if name == "graph_neighbors" and cd:
                # `run_tool` が既にカード単位で裏付け doc を検証済み（無効カードは cd に含まれない・
                # `d` はその検証済み doc_id 集合そのもの）——ここで再検証しない。裏付け doc を
                # 主張しないカード（純粋なグラフ位相情報）は、Neo4j から実際に返ったノードである
                # こと自体を source_type=graph の構造 Evidence として計上する。
                _call_structural += _card_structural_evidence(cd)
            structural_evidence_meta += _call_structural
            state.add_tool_result(name, args, result, c, _call_structural)
            resp_parts.append({"functionResponse": {"name": name, "response": result}})
        # D2（計測）: ラウンドごとに件数・並列度・所要時間だけを1行残す（本文は出さない）。
        _log.debug("tool batch: n=%d parallel=%d elapsed=%.2fs", len(_pending_calls),
                  (SHERPA_TOOL_PARALLEL if _use_parallel else 1), time.monotonic() - _tool_batch_started)
        if over_limit:
            # レビュー是正（LOW-D）: 超過件数に関わらず固定ノード1件だけ生成する。
            yield {"node": _node("ツール呼び出し上限", "1回の応答あたりの実行数上限に達したため打ち切りました")}
            # 上限超過＝以降のターンへは進まず、ここで打ち切る。
            yield _build_final_payload("", docs, searched, cites, cards, _usage_or_none(usage),
                                       verified_docs, "tools_per_turn_exceeded", world,
                                       structural_evidence_meta=structural_evidence_meta,
                                       read_evidence=_read_evidence_payload(state), gaps=state.gaps, limits=state.limits)
            return
        contents.append({"role": "user", "parts": resp_parts})
        _round_bounds.append((_round_start, len(contents)))
        # C2（探索ループの文脈整理）: `openai_style`/`anthropic_style` と同じ判定・置換
        # （この方言は1組＝role=model 1件＋role=user(functionResponse 配列) 1件の2メッセージ）。
        if (len(_round_bounds) > SHERPA_AGENTIC_KEEP_RECENT_TOOLS
                and _messages_byte_size(contents) > SHERPA_AGENTIC_CONTEXT_BUDGET_BYTES):
            _cutoff = _round_bounds[-SHERPA_AGENTIC_KEEP_RECENT_TOOLS][0]
            _collapsed_rounds = len(_round_bounds) - SHERPA_AGENTIC_KEEP_RECENT_TOOLS
            _summary = state.render(max_bytes=SHERPA_AGENTIC_CONTEXT_BUDGET_BYTES // 2,
                                    keep_recent_tools=SHERPA_AGENTIC_KEEP_RECENT_TOOLS)
            contents[_prefix_len:_cutoff] = [
                {"role": "user", "parts": [{"text": f"【ここまでの調査状態】\n{_summary}"}]}]
            _shift = (_cutoff - _prefix_len) - 1
            _round_bounds = [(s - _shift, e - _shift) for s, e in _round_bounds[_collapsed_rounds:]]
            state.bump_limit("context_compactions")
            yield {"node": _context_compacted_node(_collapsed_rounds)}
    yield _build_final_payload("", docs, searched, cites, cards, _usage_or_none(usage),
                               verified_docs, "turns_exhausted", world,
                               structural_evidence_meta=structural_evidence_meta,
                               read_evidence=_read_evidence_payload(state), gaps=state.gaps, limits=state.limits)
