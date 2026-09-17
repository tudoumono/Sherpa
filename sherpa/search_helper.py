"""下調べ（資料の検索）に使う AI ＝「検索アシスタント」（決定 2026-08-15）。

**動機**: 資料調査は入力トークンが支配的（実測: 1問で input 118k / output 1k）。読む作業を安いモデルへ
任せ、最終回答だけメインの AI が作れば、体感品質を保ったまま費用を大きく下げられる。

**設定は1項目**（利用者ごとの選択のみ・admin の一元管理はしない＝Ollama を使いたくない人はそのまま
使える）。実行機構は既存のサブループ（`providers/base.py::_sub_loop`）をそのまま使う。

制約: **メインが OpenAI 直結構成または Ollama 構成のときだけ効く**（頭脳 × 本設定の組合せ表は
`sherpa/providers/__init__.py::get_provider` docstring・提案書
docs/proposals/2026-09-17-深さの再定義とレビュー巡.md §2.1 が正典）。`resolve()` 自体は頭脳の種類を
見ない（設定1項目から下調べ役の形を組み立てるだけ）——どの頭脳でどの選択を実際に使うか／無視するか
の判断は呼び出し側（`get_provider`）が組合せ表に従って行う。Codex 構成は Codex CLI が自分でツールを
回すため、Sherpa 側のサブループが介在しない。
"""
from __future__ import annotations

import logging
import os
import re

_log = logging.getLogger("sherpa")

# 設定値（`user_settings.search_helper`）。'' ＝ 安いモデルを使わない（頭脳自身が下調べする）。
NONE = ""
OLLAMA = "ollama"
OPENAI = "openai"
CHOICES = frozenset({NONE, OLLAMA, OPENAI})

# 検索アシスタントに許すツール（回答は書かせない＝資料を探して読むだけ）。agentic_search の実ツール名
# （既知集合・固定）から `ask_user` を除く: サブ経路のモデル生成文をそのまま公式の確認カードとして
# 出さない。doc_outline/read_doc（土台系・新設）も list_docs/read_around と同じく含める。
TOOLS = frozenset({"list_docs", "ripgrep_search", "glob_search", "doc_outline", "read_doc",
                   "read_around", "es_search", "graph_neighbors"})

# モデル名の形式（`sherpa/routers/system.py::_MODEL_NAME_RE` と同型）。
_MODEL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\-]{0,127}")

_DEFAULT_MIN_CITATIONS = 1

# 頭脳自身を worker に据えたプロファイルの識別子（`self_worker()`）。`providers/base.py` が
# 「worker が頭脳自身か」を判定する唯一の基準。
SELF_PROFILE_ID = "search-helper-self"


def _default_max_turns() -> int:
    """guard.max_turns 省略時の既定（env `SHERPA_AGENTIC_MAX_TURNS`＝agentic_search.MAX_TURNS と同値）。"""
    from . import agentic_search
    return agentic_search.MAX_TURNS


def _default_llm_timeout() -> int:
    """guard.llm_timeout 省略時の既定（env `SHERPA_LLM_TIMEOUT`＝`_GenProvider.__init__` と同値）。"""
    return int(os.environ.get("SHERPA_LLM_TIMEOUT", "60"))


def _resolve_guard() -> dict:
    """サブループの安全弁（min_citations/max_turns/llm_timeout）を env 既定へ解決した dict にする。"""
    return {"min_citations": _DEFAULT_MIN_CITATIONS, "max_turns": _default_max_turns(),
            "llm_timeout": _default_llm_timeout()}


# OpenAI を選んだときの最終フォールバック（`model_catalog.resolve_model` がカタログ既定/
# 組み込み既定のどちらでも解決できなかった場合のみ使う）。
# 実測（2026-08-15・world=test の実コーパス）: `gpt-4o-mini` は grep を数回打つだけで精読せず、
# 引用0件のまま終わる（根拠ゲートに掛かって通常経路へ落ち、かえって悪い回答になる）。
# `gpt-5.4-mini` は grep → 全文検索 → 精読まで到達し、引用30件・入力17kで完走した。
# `model_catalog`（openai/subsearch）の組み込み既定と同じ値＝管理者がカタログ既定を変更すれば
# こちらも追従する（`resolve()` は `model_catalog.resolve_model` 経由）。
DEFAULT_OPENAI_MODEL = "gpt-5.4-mini"


class InvalidSearchHelperConfigError(ValueError):
    """`search_helper` の非空の不正値、または解決先の管理者モデル設定が壊れているときに
    `resolve()` が送出する（黙ってメインAIの高コスト経路へ倒さない・利用時 honest failure 用）。
    未設定（空）・鍵未設定（A6/A7 により未接続）は正当な「使わない」状態のため対象外＝
    従来どおり `resolve()` は None を返す。"""


def resolve(user_settings: dict, *, system_settings: dict | None = None) -> dict | None:
    """`get_provider` から呼ぶ: 設定1項目から `Provider._sub` の形を組み立てる。

    I/O なし。未設定（空文字）・鍵未設定（A6/A7 により未接続）は「安いモデルを使わない」状態として
    None を返す——呼び出し元（`get_provider`）はこのとき `self_worker()`（頭脳と同じ接続・同じ
    モデルの worker）を据える＝下調べ役なしの経路にはしない。

    非空の不正値（未知の選択肢・解決先の管理者モデル設定が壊れている）は `InvalidSearchHelperConfigError`
    を送出する（黙ってメインAIの高コスト経路へ倒さない＝意図しない課金の是正。呼び出し元
    `get_provider()`/`providers/base.py::run()` が honest failure として利用者に伝える）。

    `system_settings`（省略可）: `get_provider()` が入口で1回読んだスナップショットをそのまま渡す。
    メインプロバイダの鍵/モデル解決と同じスナップショットで検索アシスタントの鍵/モデルも解決する
    ＝1ターンの処理中に admin 保存が挟まっても、メインと検索アシスタントが新旧混在の接続先/鍵で
    動くことを防ぐ。省略時は各 resolve 呼び出しが自分で読む。
    """
    from . import keys as _keys
    from . import model_catalog

    choice = str(user_settings.get("search_helper") or NONE).strip().lower()
    if not choice:
        return None
    if choice not in (OLLAMA, OPENAI):
        raise InvalidSearchHelperConfigError(
            f"下調べ役の設定が不正です（{choice!r}）。設定画面で選び直してください。")
    base = {"tools": TOOLS, "guard": _resolve_guard(), "profile_id": f"search-helper-{choice}",
            "description": "資料の検索・精読だけを担当する（回答はメインのAIが作る）",
            "name": "下調べ役"}
    if choice == OLLAMA:
        model = model_catalog.resolve_model("ollama", "subsearch", None, system_settings=system_settings)
        if not _MODEL_NAME_RE.fullmatch(model):     # 管理者側のカタログ破損＝黙ってメインへ倒さない
            raise InvalidSearchHelperConfigError(
                "下調べ役（ローカルAI）のモデル設定が不正です。管理者に確認してください。")
        url = _keys.resolve_ollama_url(user_settings, system_settings=system_settings)
        return {**base, "provider": "ollama", "url": url, "model": model}
    key = _keys.resolve_api_key("openai", user_settings, system_settings=system_settings)
    if not key:                                          # 鍵が無ければ使えない＝メインへ戻す
        return None
    model = model_catalog.resolve_model("openai", "subsearch", None,
                                        system_settings=system_settings) or DEFAULT_OPENAI_MODEL
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/\-]{0,127}", model):
        raise InvalidSearchHelperConfigError(
            "下調べ役（OpenAIの低コストモデル）のモデル設定が不正です。管理者に確認してください。")
    return {**base, "provider": "openai", "key": key, "url": None, "model": model}


def self_worker(provider: str, model: str, *, key: str | None = None,
                url: str | None = None) -> dict:
    """頭脳自身を worker（下調べ役）にした解決済みプロファイル（`resolve()` と同じ形）。

    `search_helper` が空（使わない）、または組合せ表で無視される選択（Ollama 頭脳 × openai）の
    ときに `get_provider` がこれを `_sub` に据える＝**下調べ役なしの経路は無い**（API/Ollama は
    常に「worker ＋ orchestrator/evaluator」のハイブリッド1経路）。接続先・鍵・モデルは頭脳と
    同じで、違いは「安いモデルを使わない」ことだけ。

    許すツールは `TOOLS`（回答は書かせない）に `ask_user` を足したもの——`TOOLS` が
    `ask_user` を外すのは「安いモデル／別モデルの生成文を公式の確認カードとして出さない」ため
    であり、頭脳自身が worker のときはその理由が当たらない（通常経路と同じ AI が質問する）。
    出力ファイルのツールは worker には配線しない（成果物の登録は orchestrator ＝清書側の契約）。
    """
    return {"tools": TOOLS | {"ask_user"}, "guard": _resolve_guard(), "profile_id": SELF_PROFILE_ID,
            "description": "資料の検索・精読だけを担当する（回答はこの後の清書で作る）",
            "name": "メインのAI（下調べ）", "provider": provider, "model": model,
            "key": key, "url": url}


def label(user_settings: dict) -> str:
    """UI/監査向けの短い表示名（未設定は空文字）。"""
    choice = str(user_settings.get("search_helper") or NONE).strip().lower()
    if choice == OLLAMA:
        return "ローカル（Ollama）"
    if choice == OPENAI:
        return "OpenAI（低コストモデル）"
    return ""


def is_cloud_helper_ignored(agent: str, user_settings: dict) -> bool:
    """Ollama 頭脳に openai の下調べ役設定が付いているか（頭脳 × `search_helper` の組合せ表・
    §2.1 で無視される組合せ）。`get_provider`（実際に付けるか）と監査の無視理由の両方が
    この1関数を基準にする＝判定基準を1箇所に集約する。

    **正規化した設定値**（`search_helper` の文字列）だけで判定し、`resolve()` は呼ばない
    （OpenAI 鍵の有無に左右されない＝鍵が無くても「無視した」事実は変わらない）。
    """
    if agent != "ollama":
        return False
    choice = str(user_settings.get("search_helper") or NONE).strip().lower()
    return choice == OPENAI
