"""チャットで選べる「実行構成」（どのオーケストレータで、どのモデルを使うか）の単一の真実源。

標準MVPは4構成だけを見せる（決定 2026-08-15・`docs/17-次期MVP方針.md`）:

    openai_only   直結。Sherpa の反復ツール検索（agentic_search）が段取りし、モデルは OpenAI
    ollama_only   同上でモデルは Ollama
    codex_openai  Codex CLI が段取りとツール実行を行い、その実行モデルが OpenAI
    codex_ollama  同上で実行モデルが Ollama

`gemini` / `bedrock` は標準では見せず、環境変数 `SHERPA_EXTRA_AGENTS`（カンマ区切り）で明示的に
有効化したときだけ選択肢と入力欄が現れる。実行環境は完全オフラインで OpenAI だけ NW 穴あけという
前提のため、既定で使えない選択肢を並べない。`heuristic`（簡易・AIなし）は同じ env で「有効化」は
できる（`enabled_agents()`・`default_agent()` の解決対象になる）が、`available_constructs()` の
画面向け一覧には常に出さない＝内部フォールバック・安全網としてのみ機能させる。

構成 → 設定値の対応は `agent`（既存）と `codex_model_provider`（本スライスで追加）の2つで表す:
Codex 構成だけが `codex_model_provider` を持ち、Codex CLI がどのモデルへ接続するかを決める。
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any

_log = logging.getLogger("sherpa")

# 標準の4構成。`id` は UI/API の識別子、`agent`/`codex_model_provider` は保存される設定値。
CONSTRUCTS: tuple[dict[str, Any], ...] = (
    {"id": "openai_only", "agent": "openai", "codex_model_provider": None,
     "label": "OpenAI", "hint": "OpenAI API に直結（速い）"},
    {"id": "ollama_only", "agent": "ollama", "codex_model_provider": None,
     "label": "ローカル（Ollama）", "hint": "このパソコン/社内のローカルLLM"},
    {"id": "codex_openai", "agent": "codex", "codex_model_provider": "openai",
     "label": "Codex（OpenAI）", "hint": "Codex が自分で資料を探して調べる・モデルは OpenAI"},
    {"id": "codex_ollama", "agent": "codex", "codex_model_provider": "ollama",
     "label": "Codex（Ollama）", "hint": "Codex が自分で資料を探して調べる・モデルは Ollama"},
)

# 標準構成が使う頭脳（env に関わらず常に有効）。
STANDARD_AGENTS = frozenset({"openai", "ollama", "codex"})
# env で明示的に有効化したときだけ使える頭脳（従来からある実装は残す）。
EXTRA_AGENTS = frozenset({"gemini", "bedrock", "heuristic"})
EXTRA_AGENTS_ENV = "SHERPA_EXTRA_AGENTS"

# 追加頭脳を選んだときの表示（設定画面・チャットの頭脳バッジ共通）。
_EXTRA_LABELS: dict[str, tuple[str, str]] = {
    "gemini": ("Gemini（Google）", "Google の Gemini API"),
    "bedrock": ("AWS Bedrock (Claude)", "AWS 経由の Claude"),
    "heuristic": ("簡易（AIなし）", "最速・テンプレ回答"),
}

# Codex 構成が接続できるモデル提供元。
CODEX_MODEL_PROVIDERS = frozenset({"openai", "ollama"})

# 何も設定されていないときに選ばれている構成（決定 2026-08-15・既定は Codex(OpenAI)）。
#
# ここが**唯一の真実源**。以前は `os.environ.get("SHERPA_AGENT", ...)` の既定値が6箇所に散らばり、
# しかも `heuristic`（5箇所）と `openai`（1箇所）で食い違っていた。その結果、初期状態の利用者が
# 選択肢に無い「簡易（AIなし）」に張り付き、AI が動かないまま定型文だけが返っていた。
DEFAULT_CONSTRUCT_ID = "codex_openai"
DEFAULT_AGENT = "codex"


# RV HIGH（2026-08-18 Codex RV 指摘2）: `.env.example` の既定値を有効行にする方針にした結果、
# `OPENAI_API_KEY=sk-REPLACE_ME` を無編集のまま `cp .env.example .env` すると、真偽値だけを見る
# 判定は「プレースホルダ＝キーあり」と誤認する。同じ判定を `_auto_default_agent()` と
# `sherpa/providers/__init__.py::_select_provider` の openai 分岐（env から拾う箇所）で共有する。
# `scripts/run-common.sh::sherpa_codex_ensure_auth` の `case "$key" in ""|sk-REPLACE_ME|REPLACE_ME)`
# と同じ2値（大小文字はそのまま・シェル側と揃える）。
_PLACEHOLDER_API_KEY_VALUES = frozenset({"sk-REPLACE_ME", "REPLACE_ME"})


def is_real_api_key(value: str | None) -> bool:
    """`.env.example` のプレースホルダ文字列・空白のみを「キー未設定」として扱う。

    ユーザーが設定画面で入力した実キーの判定は変えない（これらのプレースホルダ文字列と
    たまたま一致しない限り常に真）。文字列でない値（設定破損・型不正な入力等）は
    「キーなし」として扱う（`.strip()` で例外を出さない・fail-closed）。
    """
    if not isinstance(value, str):
        return False
    v = value.strip()
    return bool(v) and v not in _PLACEHOLDER_API_KEY_VALUES


def _codex_auth_available(system_settings: dict | None = None) -> bool:
    """Codex CLI が**使える認証**を持っているか。

    使える認証＝解決済みの OpenAI キー（中央/個人・A6/A7 込み・`sherpa.keys.resolve_api_key`
    経由でプレースホルダ判定も通す）または `~/.codex/auth.json` の存在（`CODEX_HOME` を尊重・
    `auth_mode` は問わない＝`codex login` によるサブスクリプション方式も有効とする）。中身の検証は
    しない（存在＝設定済みの合図として扱う・実際に有効かどうかは起動して初めて分かる領域＝
    `sherpa/providers/codex/provider.py` の無出力失敗対応が別途担う）。
    `CODEX_HOME` の解決順は `sherpa/providers/codex/sandbox.py::_write_codex_authoring_config` と
    揃える（`os.environ.get("CODEX_HOME") or ~/.codex`）。

    `system_settings`（省略可）: 呼び出し側が既に読んだスナップショットを渡すとキー解決を
    それで行う（省略時は自分で読む）。
    """
    from sherpa import keys
    if is_real_api_key(keys.resolve_api_key("openai", None, system_settings=system_settings)):
        return True
    codex_home = Path(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"))
    return (codex_home / "auth.json").exists()


def _auto_default_agent(system_settings: dict | None = None) -> str:
    """`SHERPA_AGENT` 未指定のときに**この環境で使える**頭脳を選ぶ。

    無条件に `DEFAULT_AGENT`（codex）へは倒さない＝Codex CLI が無い、または使える認証が無い環境で
    CLI 不在／認証エラーの無出力失敗に陥らないようにする。順序: Codex CLI が PATH にあり、かつ
    使える認証がある（`_codex_auth_available`）→ codex ／ 満たさなければ次点として解決済みの
    OpenAI キー（中央/個人・A7 込み）がある → openai ／ どちらも無い → ollama。

    `system_settings`（省略可）: `_codex_auth_available`／キー解決へそのまま渡す（省略時は
    自分で読む）。
    """
    if shutil.which("codex") and _codex_auth_available(system_settings):
        return "codex"
    from sherpa import keys
    if is_real_api_key(keys.resolve_api_key("openai", None, system_settings=system_settings)):
        return "openai"
    return "ollama"


_warned_unknown_agent: set[str] = set()      # 同じ不正値の警告はプロセス内1回だけ（毎リクエストの警告洪水を防ぐ）


class InvalidAgentConfigError(ValueError):
    """`SHERPA_AGENT` が非空の不正値のとき、`effective_agent(strict=True)` が送出する
    （黙って自動選択へ倒さない・実行時 honest failure 用）。"""


def _explicit_sherpa_agent_invalid_reason() -> str | None:
    """`SHERPA_AGENT` が非空かつこの環境の選択肢に無い値なら理由を返す（None=問題なし）。
    `default_agent()`（寛容な経路）と `effective_agent(strict=True)`（実行の唯一の入口・honest
    failure 用）が同じ判定を共有する（重複実装しない）。"""
    explicit = (os.environ.get("SHERPA_AGENT") or "").strip().lower()
    if explicit and explicit not in enabled_agents():
        return (f"環境変数 SHERPA_AGENT={explicit!r} はこの環境で選べません"
               f"（選択可: {', '.join(sorted(enabled_agents()))}）。値を修正するか未設定にしてください。")
    return None


def default_agent(system_settings: dict | None = None) -> str:
    """設定が無いときに使う頭脳（env `SHERPA_AGENT` で上書き可）。

    返すのは `enabled_agents()` に含まれる頭脳でなければならない（`heuristic` を除き、これは
    画面の選択肢にも出ている頭脳＝選び直せない構成に張り付かせない）。`heuristic` は唯一の例外
    （`available_constructs()` の一覧には出さない内部フォールバック専用・オフライン「AIなし」構成は
    `SHERPA_EXTRA_AGENTS=heuristic` と本関数が読む `SHERPA_AGENT=heuristic` の組で成立させる）。
    `enabled_agents()` に無い値（未知の名前・有効化していない追加頭脳）が env で指定された場合は、
    固定の頭脳へ黙って倒さず `_auto_default_agent()`（使える頭脳の自動選択）へフォールバックする
    （タイプミス等で本来使えるはずの頭脳が選ばれないまま気付きにくい状態を避けるため）。
    `SHERPA_AGENT` が未指定なら同じく `_auto_default_agent()` に従う。

    `system_settings`（省略可）: `_auto_default_agent()` へそのまま渡す（省略時は自分で読む）。
    `effective_agent(settings)` の agent 未設定経路（最も一般的な経路）が、既に
    読んだスナップショットをここまで引き回すことで DB 読取を1回に集約するために使う。
    """
    explicit = (os.environ.get("SHERPA_AGENT") or "").strip().lower()
    if explicit:
        if explicit in enabled_agents():
            return explicit
        if explicit not in _warned_unknown_agent:
            _warned_unknown_agent.add(explicit)
            _log.warning("SHERPA_AGENT=%r はこの環境で選べません（選択可: %s）。自動選択にフォールバックします",
                         explicit, ", ".join(sorted(enabled_agents())))
    value = _auto_default_agent(system_settings)
    return value if value in enabled_agents() else DEFAULT_AGENT


def enabled_extra_agents() -> frozenset[str]:
    """`SHERPA_EXTRA_AGENTS` で有効化された追加頭脳（未知の名前は無視する）。"""
    raw = os.environ.get(EXTRA_AGENTS_ENV, "")
    return frozenset(name for name in (part.strip().lower() for part in raw.split(",")) if name in EXTRA_AGENTS)


def enabled_agents() -> frozenset[str]:
    """現在の環境で選べる頭脳（標準3＋有効化した追加分）。"""
    return STANDARD_AGENTS | enabled_extra_agents()


def agent_enabled(agent: str | None) -> bool:
    return bool(agent) and agent in enabled_agents()


# 実行時に遮断する頭脳。`heuristic` は「AI が1つも設定されていないときの安全網」であり、
# 設定行が無いときの既定値でもある（`providers/__init__.py`・`routers/system.py`）。UI の選択肢からは
# 外すが、実行時にエラーへ倒すと未設定環境が丸ごと動かなくなるため遮断対象に含めない。
_RUNTIME_BLOCKABLE = frozenset({"gemini", "bedrock"})


def runtime_blocked(agent: str | None) -> bool:
    """この環境では実行させない頭脳か（env で有効化していない外部AI）。"""
    name = (agent or "").lower()
    return name in _RUNTIME_BLOCKABLE and name not in enabled_extra_agents()


# A7（クラウドプロバイダ排他選択）の対象となる頭脳（中央/個人キーで動く3種）。
_CLOUD_AGENTS = frozenset({"openai", "gemini", "bedrock"})
_warned_unavailable_cloud_agent: set[str] = set()   # 同じ不一致の警告はプロセス内1回だけ


def agent_requires_unselected_cloud(agent: str, system_settings: dict | None = None,
                                    *, strict: bool = False) -> bool:
    """A7: `agent` がクラウド系（openai/gemini/bedrock）で、選択中のクラウドプロバイダと
    一致しないか（一致しなければ実行しても honest failure になるだけの組み合わせ）。

    `system_settings`（省略可）: 呼び出し側が既に読んだスナップショットを渡すと A7 判定をそれで
    行う（省略時は自分で読む）。`effective_agent()` がここへ渡すことで、実際の判定
    （この関数の戻り値がゲートを決める）を渡されたスナップショットと一致させる。

    `strict`（既定 False）: `keys.selected_cloud_provider(strict=...)` へそのまま転送する
    （`effective_agent(strict=True)` からのみ True で呼ばれる）。"""
    name = (agent or "").lower()
    if name not in _CLOUD_AGENTS:
        return False
    from sherpa import keys
    return keys.selected_cloud_provider(system_settings, strict=strict) != name


def effective_agent(settings: dict | None, *, system_settings: dict | None = None,
                    strict: bool = False) -> str:
    """実行（`providers/__init__.py::_select_provider`）と表示（`construct_id`）が共通で経由する
    単一の真実源。保存済み `agent` がクラウド系で選択中のクラウドプロバイダ（A7）と一致しない場合は
    `ollama`（排他対象外・常に使える）へフォールバックする。同じ不一致の組み合わせにつき警告は
    プロセス内1回だけ。`codex` はここでは対象外（既定の非 Azure 構成は Codex 自身の認証を使い、
    Sherpa のクラウドプロバイダ解決を経由しないため）。

    `agent_enabled(agent)` が偽（`SHERPA_EXTRA_AGENTS` で有効化していない gemini/bedrock 等）の
    ときは A7 のフォールバックを適用しない＝そのまま返す。`_select_provider` の
    `runtime_blocked()` チェックに「この環境で無効な AI」として明示的に伝えさせる（A7 の
    silent フォールバックが、env 未有効という**より根本的な**無効理由を覆い隠さないようにする）。

    `system_settings`（省略可）: 呼び出し側が既に読んだスナップショットを渡すと、agent 未設定時の
    既定選択（`default_agent()`）・A7 判定（クラウド系のみ）の両方をそれで行う（省略時は自分で
    読む・`sherpa/keys.py::resolve_api_key` と同じ理由）。

    DB 読取は「本当に必要なときだけ・一度だけ」に絞る:
      - `agent` 未設定（最も一般的な経路）: `default_agent()` へスナップショットを渡して解決し、
        その結果へさらに A7 判定を適用する（`default_agent()` は明示 env `SHERPA_AGENT` を素通り
        させることがあり、その値自体が選択中のクラウドプロバイダと一致するとは限らないため、
        `_auto_default_agent()` 内部の A7 整合済みキー解決だけでは不十分）。
      - `agent` が明示的な非クラウド系（codex/ollama 等）: A7 判定の対象外なので DB を一切
        読まない。
      - `agent` が明示的なクラウド系（openai/gemini/bedrock）: このときだけ materialize し、
        A7 不一致判定と警告ログ用の `selected_cloud_provider` の両方に同じ値を使う。

    `strict`（既定 False）: True のとき、`SHERPA_AGENT`/`cloud_provider` が非空の不正値
    （env 誤記・旧データ等）でも黙って自動選択/ollama へ倒さず `InvalidAgentConfigError`
    （`cloud_provider` 起因は `keys.InvalidCloudProviderConfigError`）を送出する。実行の唯一の
    入口（`providers/__init__.py::_select_provider`）だけが `strict=True` で呼び、honest failure
    へ変換する。表示/監査経由（`construct_id`・`_audit_chat_turn` 等）は既定 False のまま＝壊れた
    設定でも画面/監査を止めない。
    """
    from sherpa import keys, store
    s = settings or {}
    raw = s.get("agent")
    # 文字列以外の非 None（bool/int/list/dict 等・設定破損）は、`or ""` の truthiness 判定に
    # 先立って拒否する（`keys.selected_cloud_provider` と同型: false 等が「未設定」に化けると、
    # strict でも黙って自動選択へ倒れてしまう）。
    if raw is not None and not isinstance(raw, str):
        if strict:
            raise InvalidAgentConfigError(
                f"agent の値が不正です（{raw!r}）。"
                f"選べる値: {', '.join(sorted(STANDARD_AGENTS | EXTRA_AGENTS))}。"
                "設定画面で選び直してください。")
        raw = ""
    raw_agent = str(raw or "").strip().lower()
    sys_s = None
    if not raw_agent:
        sys_s = system_settings if system_settings is not None else store.get_system_settings()
        if strict:
            reason = _explicit_sherpa_agent_invalid_reason()
            if reason is not None:
                raise InvalidAgentConfigError(reason)
        agent = default_agent(sys_s)
    elif agent_enabled(raw_agent) and raw_agent in _CLOUD_AGENTS:
        sys_s = system_settings if system_settings is not None else store.get_system_settings()
        agent = raw_agent
    else:
        # `raw_agent` が既知の頭脳名（STANDARD_AGENTS|EXTRA_AGENTS）であれば、有効化していない
        # だけの正当な経路（`runtime_blocked()` が明示的に伝える・上のクラス docstring 参照）
        # としてそのまま返す。既知のどれでもない非空の不正値（env 誤記・旧データ等）は、strict
        # 時だけ黙って HeuristicProvider（別の頭脳）へ縮退させず honest failure にする
        # （`_select_provider` はどの分岐にも一致しない agent を最終的に HeuristicProvider へ
        # 落とすため、ここで検出しないと利用者が選んだ頭脳と異なるものが黙って動く）。
        if strict and raw_agent not in (STANDARD_AGENTS | EXTRA_AGENTS):
            raise InvalidAgentConfigError(
                f"agent の値が不正です（{raw_agent!r}）。"
                f"選べる値: {', '.join(sorted(STANDARD_AGENTS | EXTRA_AGENTS))}。"
                "設定画面で選び直してください。")
        return raw_agent
    if agent in _CLOUD_AGENTS and agent_requires_unselected_cloud(agent, sys_s, strict=strict):
        provider = keys.selected_cloud_provider(sys_s)
        marker = f"{agent}->{provider}"
        if marker not in _warned_unavailable_cloud_agent:
            _warned_unavailable_cloud_agent.add(marker)
            _log.warning("保存済み agent=%r は選択中のクラウドプロバイダ（%s）と一致しないため "
                         "ollama へフォールバックします", agent, provider)
        return "ollama"
    return agent


def available_constructs(system_settings: dict | None = None) -> list[dict[str, Any]]:
    """画面に出す実行構成の一覧（標準4＋有効化した追加頭脳）。

    追加頭脳は「構成」ではなく頭脳そのものなので、`codex_model_provider=None` の1件として並べる。
    `heuristic`（簡易・AIなし）は `SHERPA_EXTRA_AGENTS` で有効化していても一覧には出さない
    （未設定時の内部フォールバック・安全網としてのみ機能させる・利用者が明示的に選ぶ選択肢としては
    見せない）。この除外は画面向け一覧だけの制約であり、`default_agent()`/`enabled_agents()`/
    `runtime_blocked()` は heuristic を引き続きそれぞれの規則で扱う。

    A7（クラウドプロバイダ排他選択）: 選択肢の出し分け。`openai_only`（直結の
    OpenAI）と追加頭脳の `gemini`/`bedrock` は、`sherpa.keys.selected_cloud_provider()` が一致しない
    限り選択肢から外す（選んでも `resolve_api_key` が None を返し honest failure になるだけの構成を、
    そもそも選ばせない）。`codex_openai`/`codex_ollama`/`ollama_only` は対象外＝Codex(OpenAI) は既定
    （非 Azure）構成では Codex 自身の `codex login`（auth.json）を使い、Sherpa のクラウドプロバイダ
    解決を経由しないため（Azure 等への切替時のみ `_codex_openai_compat_block_reason` が別途ゲートする・
    `sherpa/providers/__init__.py` 参照）。Ollama は A7 排他の対象外（常時併用）。
    保存済み設定の構成が一覧から消えても、`construct_id()` はそのまま値を返す（`CONSTRUCTS` 自体は
    不変）。画面側（`web/settings.js::renderConstructOptions`）は一覧に無い現在値を「一覧外」の
    選択肢として保持し、利用者が明示的に選び直すまで実際の agent を失わない
    （先頭候補への自動フォールバックはしない）。

    `system_settings`（省略可）: 呼び出し側が既に読んだスナップショットを渡すと A7 判定をそれで
    行う（省略時は自分で読む）。
    """
    from sherpa import keys
    provider = keys.selected_cloud_provider(system_settings)
    out = [dict(c) for c in CONSTRUCTS if not (c["agent"] == "openai" and provider != "openai")]
    for name in sorted(enabled_extra_agents()):
        if name == "heuristic":
            continue   # 利用者向け選択肢には出さない（内部フォールバック専用）
        if name in keys.CLOUD_PROVIDERS and provider != name:
            continue
        label, hint = _EXTRA_LABELS[name]
        out.append({"id": name, "agent": name, "codex_model_provider": None, "label": label, "hint": hint})
    return out


def construct_id(settings: dict | None, *, system_settings: dict | None = None) -> str:
    """保存済み設定から現在の構成 id を求める（該当が無ければ agent 名をそのまま返す）。

    `effective_agent()` 経由＝A7 で選択中でないクラウド系 agent は ollama 扱いで一致させる
    （画面の表示とバックエンドの実行が食い違わないようにする単一の真実源）。

    `system_settings`（省略可）は `effective_agent()` へそのまま渡す（スナップショット共有）。
    """
    s = settings or {}
    agent = effective_agent(s, system_settings=system_settings)
    if agent == "codex":
        # `codex_model_provider()`（実行時の共通resolver）と同じく strip+lowercase してから
        # 比較する（" OLLAMA " のような値を誤って codex_openai 表示にしない）。
        raw = s.get("codex_model_provider")
        if raw is None or raw == "":
            provider = ""
        elif not isinstance(raw, str):
            # 文字列以外の非 None（bool/int 等・設定破損）: `codex_model_provider()` は同じ値で
            # honest failure（実行時エラー）になるのに、ここが黙って "codex_openai" 表示化すると
            # 画面とバックエンドが食い違う。下の非空不正値と同様「一覧に無い id」を返す。
            return "codex_invalid"
        else:
            provider = raw.strip().lower()
        if not provider or provider == "openai":
            return "codex_openai"
        if provider == "ollama":
            return "codex_ollama"
        # 非空の不正値（env 誤記・旧データ等）: `codex_openai` に丸めて表示すると、実行時は
        # `codex_model_provider()` が honest failure で止まるのに画面だけ「Codex(OpenAI) が
        # 動いている」と偽って見える食い違いになる。一覧外の id を返すことで
        # （`web/settings.js::renderConstructOptions` の一覧外パターン）画面側に「一覧外」として
        # 保持させ、利用者が明示的に選び直すまで無関係保存で openai へ黙って固定されないように
        # する。
        return "codex_invalid"
    for c in CONSTRUCTS:
        if c["agent"] == agent:
            return c["id"]
    return agent


class InvalidCodexModelProviderError(ValueError):
    """`codex_model_provider` が非空の不正値のとき、`codex_model_provider()` が送出する
    （黙って openai へ倒さない・実行時 honest failure 用）。"""


def codex_model_provider(settings: dict | None) -> str:
    """Codex CLI が接続するモデル提供元（未設定は既定 openai）。

    非空の不正値（env 誤記・旧データ等）は `InvalidCodexModelProviderError` を送出する
    （黙って openai へ倒すと、利用者が選んだローカル AI ではなく気付かないうちに OpenAI へ
    実行される事故になるため）。唯一の呼び出し元は実行の入口
    `providers/__init__.py::_select_provider`（honest failure へ変換して使う）。
    """
    raw = (settings or {}).get("codex_model_provider")
    # 文字列以外の非 None（bool/int/list/dict 等・設定破損）は、`or ""` の truthiness 判定に
    # 先立って拒否する（`keys.selected_cloud_provider` と同型のバグを避ける）。
    if raw is not None and not isinstance(raw, str):
        raise InvalidCodexModelProviderError(
            f"codex_model_provider の値が不正です（{raw!r}）。"
            f"選べる値: {', '.join(sorted(CODEX_MODEL_PROVIDERS))}。設定画面で選び直してください。")
    value = str(raw or "").strip().lower()
    if not value:
        return "openai"
    if value not in CODEX_MODEL_PROVIDERS:
        raise InvalidCodexModelProviderError(
            f"codex_model_provider の値が不正です（{value!r}）。"
            f"選べる値: {', '.join(sorted(CODEX_MODEL_PROVIDERS))}。設定画面で選び直してください。")
    return value


# 画面の「担当バッジ」（ローカル/社内サーバ/クラウド/クラウド（OpenAI 互換）AI）はここを唯一の真実源とし、フロント
# （render.js）は推測しない（受け取った値を表示するだけ）。`provider_id` が `"codex"` のときは
# `codex_model_provider`（`codex_openai`/`codex_ollama` のどちらの構成か）が無いと判定できない
# ——Codex CLI 自体は常に `provider_id="codex"` を名乗り、実際の接続先（OpenAI/Ollama）は
# `providers/codex/provider.py::CodexProvider._ollama_base_url` の有無でしか分からないため、
# 呼び出し元がそれを明示的に渡す契約にする。
def is_local(provider_id: str | None, *, codex_model_provider: str | None = None,
            system_settings: dict | None = None) -> str | None:
    """`provider_id`（`answer.usage.provider`・`metrics.provider` 等の値）の配置区分を返す。
    4値: `"local"`（Ollama）／`"on_prem"`（LAN 内に自前で立てた OpenAI 互換エンドポイント——
    DGX Spark 等）／`"cloud"`（api.openai.com・Azure OpenAI・Gemini・Bedrock）／`"cloud_compat"`
    （OpenAI 本家でも Azure でもない、外部の OpenAI 互換クラウドサービス）。`None`＝判定不能
    （呼び出し側は「担当不明」として表示し、いずれにも決め打たない・誤断定より不明の方が安全
    という方針）。

    - `"ollama"` → `"local"`（常にローカル）。
    - `"openai"` → `_openai_compat_locality(system_settings)` に委ねる。
    - `"gemini"`/`"bedrock"` → `"cloud"`（常にクラウド専用 API・接続先を選べない）。
    - `"codex"` → `codex_model_provider` で分岐（`"ollama"`→`"local"`・それ以外/省略（既定 openai・
      `codex_model_provider()` の契約と一致）は `_openai_compat_locality` に委ねる＝Codex(OpenAI)
      が Azure/DGX Spark/外部 OpenAI 互換サービス等へ向いている場合も正しく分類する）。
    - それ以外の未知の値（将来の新規頭脳・壊れた設定等）→ `None`（誤断定しない）。

    `system_settings`（省略可）: `_openai_compat_locality` へそのまま渡す（呼び出し側が既に読んだ
    スナップショットがあればそれを使い、同一ターン内で新旧設定が混在する事故を避ける・省略時は
    `llm.py` 側が DB から読む）。
    """
    name = (provider_id or "").strip().lower()
    if name == "ollama":
        return "local"
    if name == "openai":
        return _openai_compat_locality(system_settings)
    if name in ("gemini", "bedrock"):
        return "cloud"
    if name == "codex":
        if (codex_model_provider or "").strip().lower() == "ollama":
            return "local"
        return _openai_compat_locality(system_settings)
    return None


def _openai_compat_locality(system_settings: dict | None) -> str:
    """`openai_endpoint_kind() != "custom"`（本家/Azure）は常に `"cloud"`。`"custom"`（管理画面
    「その他 OpenAI 互換」）は、host が私有/ローカル範囲かどうかで `"on_prem"`／`"cloud_compat"`
    に分ける（`llm.endpoint_locality()` が唯一の判定関数——`"custom"` というだけで一律 on_prem
    扱いにすると、単に本家・Azure 以外の**外部**クラウド API を「社内サーバ」と誤表示する）。
    `"cloud_compat"` は「クラウドだが OpenAI 本家/Azure ではない」ことを利用者に誠実に示す
    （render.js の担当バッジは「クラウド（OpenAI 互換）」と表示する）。
    """
    from . import llm
    if llm.openai_endpoint_kind(system_settings) != "custom":
        return "cloud"
    locality = llm.endpoint_locality(llm.openai_base_url(system_settings))
    return locality if locality == "on_prem" else "cloud_compat"
