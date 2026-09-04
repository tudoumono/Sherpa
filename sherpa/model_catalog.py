"""モデルカタログ（プロバイダ×用途ごとの「選べるモデル一覧＋既定」・管理者が管理画面で編集する）。

各利用者の「モデル名」入力は自由入力ではなく、このカタログ（`system_settings.model_catalog`）から
選ぶ形にする。解決順は「利用者の選択（カタログ内）→ カタログ既定 → 組み込み既定（このモジュールの
ハードコード値）」。

**Bedrock は対象外**: 実在確認済みモデルの専用機構（`store.add_bedrock_verified_models`・
`sherpa/routers/system.py::_bedrock_model_id_valid`）が既に「動的に検証した ID だけを許可する」
より厳格な仕組みを持つ。ここに静的 allowed リストを重複させると二重の真実源になるため、
bedrock のモデル選択は従来どおり `bedrock_model` 専用の検証系に任せる（本モジュールは触れない・
`validate_catalog` は provider="bedrock" を明示的に拒否する）。

**`route`（振り分け）用途**: この用途を消費する呼び出し箇所はコード上に無い。データ形
（`USAGES` に含める・管理画面には表示する）は用意するが、`_DEFAULT_CATALOG` には allowed を
持たせず、解決関数も未配線のまま（実際に使う機能ができてから allowed を追加する）。
"""
from __future__ import annotations

import copy
import logging
import os
import re

_log = logging.getLogger("sherpa")

PROVIDERS = ("openai", "gemini", "bedrock", "ollama", "codex")
USAGES = ("chat", "intent", "embed", "route", "subsearch", "codex", "render")

# `render`（rag.md の LLM 成形＝`sherpa/ingest/llm_render.py`）: GRAPH-SRC（2026-09-04）で旧・意味層
# フル抽出（`extract` 用途）が撤去され、`render` が自立した用途になった——`USAGES`/`validate_catalog`
# は以後 `extract` セルの新規保存を受け付けない。**後方互換の読み取りを1箇所だけ残す**（過剰設計を
# しない・最小案）: 既に `extract` セルへ値を保存済みの環境が `render` を一度も設定していなければ、
# `resolve_model` はここ（`_USAGE_FALLBACK`）経由で従来どおり `extract` セルの解決結果を読む
# （`_DEFAULT_CATALOG` の `extract` キー自体は下の後方互換のため残す・`route` と同型の据え置き＝
# 静的値を置くと admin が extract だけ変更した場合に反映されなくなるため置かない）。
_USAGE_FALLBACK = {"render": "extract"}

# モデル名の文法（provider を問わない共通の下限・1〜128文字）。個人設定の自由入力欄
# （`sherpa/routers/system.py::_MODEL_NAME_RE` は本定数を re-export する・二重定義しない）と
# 同じパターンを使う。Codex だけは CLI 引数（argv `-m`）に渡すため、追加でより厳しい制約
# （`:` 不可・64文字上限）を持つ（`sherpa/providers/codex/provider.py::CodexProvider.__init__`）。
# `validate_catalog` はここで両方を強制する＝管理者が保存できたモデル名が、個人設定の保存では
# 拒否される／Codex では honest failure（`CodexProvider.__init__` の `ValueError`）になる、
# という食い違いを防ぐ（管理者が保存できる値は、常にどの消費者でもそのまま使える）。
MODEL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\-]{0,127}")
CODEX_MODEL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/\-]{0,63}")


class InvalidModelNameError(ValueError):
    """不正な非空モデル名（上記文法を満たさない）。`ValueError` を継承するため既存の広い
    `except ValueError` にも従来どおり拾われるが、モデル名以外の理由（例: env 変数の数値
    パース失敗）で送出される `ValueError` と区別したい呼び出し側はこの型だけを狭く捕捉できる。"""


def _model_name_re_for(provider: str):
    """`provider` が実際に受け付けるモデル名パターン（consumer 側の制約と揃える）。"""
    return CODEX_MODEL_NAME_RE if provider == "codex" else MODEL_NAME_RE

# 組み込み既定（カタログ未設定・DB 不達時でも解決が壊れないための最終フォールバック）。
# 値は今までの各呼び出し箇所のハードコード既定と同じ（挙動を変えない・カタログ導入前との後方互換）。
# `"extract"` キーは `USAGES` に含まれない（管理画面には出ない・`validate_catalog` は新規保存を
# 拒否する）が、`_USAGE_FALLBACK`（`render`→`extract`）の組み込み既定として意図的に残す
# （GRAPH-SRC 2026-09-04）。
_DEFAULT_CATALOG: dict[str, dict[str, dict[str, object]]] = {
    "openai": {
        "chat": {"allowed": ["gpt-5.5", "gpt-5.4-mini"], "default": "gpt-5.5"},
        "extract": {"allowed": ["gpt-5.5", "gpt-5.4-mini"], "default": "gpt-5.5"},
        "intent": {"allowed": ["gpt-4o-mini", "gpt-5.4-mini"], "default": "gpt-4o-mini"},
        "embed": {"allowed": ["text-embedding-3-small", "text-embedding-3-large"],
                  "default": "text-embedding-3-small"},
        "subsearch": {"allowed": ["gpt-5.4-mini", "gpt-4o-mini"], "default": "gpt-5.4-mini"},
    },
    "gemini": {
        "chat": {"allowed": ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-flash-latest"],
                 "default": "gemini-2.5-flash"},
        "extract": {"allowed": ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-flash-latest"],
                    "default": "gemini-2.5-flash"},
        "intent": {"allowed": ["gemini-2.5-flash"], "default": "gemini-2.5-flash"},
        # 埋め込みの次元は固定（1536・sherpa/embeddings.py の _MODELS 表）。allowed は次元互換の
        # モデルだけを登録する運用とする（次元情報そのものの管理は将来課題）。
        "embed": {"allowed": ["gemini-embedding-001"], "default": "gemini-embedding-001"},
    },
    "ollama": {
        "chat": {"allowed": ["qwen2.5"], "default": "qwen2.5"},
        "extract": {"allowed": ["qwen2.5"], "default": "qwen2.5"},
        "intent": {"allowed": ["qwen2.5"], "default": "qwen2.5"},
        "subsearch": {"allowed": ["qwen2.5"], "default": "qwen2.5"},
        # 埋め込みの次元は固定（768・sherpa/embeddings.py の _MODELS 表）。allowed は次元互換の
        # モデルだけを登録する運用とする（次元情報そのものの管理は将来課題）。
        "embed": {"allowed": ["nomic-embed-text"], "default": "nomic-embed-text"},
    },
    "codex": {
        "codex": {"allowed": ["gpt-5.5"], "default": "gpt-5.5"},
    },
}

# 利用者ごとの「モデル名」入力フィールド → それが実際に使われる (provider, usage) の集合。
# 1フィールドが複数の用途で共用される既存データ形（例: openai_model は chat と extract の両方で
# 使われる・intent_model/search_helper_model はプロバイダを問わず1本の欄）をそのまま踏襲する。
FIELD_CELLS: dict[str, tuple[tuple[str, str], ...]] = {
    "openai_model": (("openai", "chat"), ("openai", "extract")),
    "gemini_model": (("gemini", "chat"), ("gemini", "extract")),
    "ollama_model": (("ollama", "chat"), ("ollama", "extract"), ("ollama", "subsearch")),
    "codex_model": (("codex", "codex"),),
    "intent_model": (("openai", "intent"), ("gemini", "intent"), ("ollama", "intent")),
    "search_helper_model": (("openai", "subsearch"), ("ollama", "subsearch")),
}

_CATALOG_KEY = "model_catalog"
# ENV-1／SET-1 の env→system_settings シードと同じ「一度だけ」方式（`store.seed_system_settings_once`）。
_CATALOG_SEED_MARKER_KEY = "model_catalog_seed_version"
_CATALOG_SEED_VERSION = 1


def hardcoded_fallback(provider: str, usage: str) -> str:
    """カタログにも管理者設定にも無いときの最終フォールバック（組み込み既定）。"""
    return str(_DEFAULT_CATALOG.get(provider, {}).get(usage, {}).get("default") or "")


def get_catalog(system_settings: dict | None = None) -> dict:
    """有効なカタログ（組み込み既定 ∪ 管理者設定・セルごとに管理者設定があればそちらを優先）。

    DB 不達時は組み込み既定のみを返す（解決を止めない・fail-safe）。

    `system_settings`（省略可）: 呼び出し側が既に読んだスナップショットを渡すとそれを使う
    （省略時は自分で読む）。
    """
    base = copy.deepcopy(_DEFAULT_CATALOG)
    try:
        if system_settings is not None:
            configured = system_settings.get(_CATALOG_KEY) or {}
        else:
            from sherpa import store
            configured = store.get_system_settings().get(_CATALOG_KEY) or {}
    except Exception:
        configured = {}
    if not isinstance(configured, dict):
        return base
    for provider, usages in configured.items():
        if not isinstance(usages, dict):
            continue
        base.setdefault(provider, {})
        for usage, cell in usages.items():
            if not isinstance(cell, dict):
                continue
            base[provider][usage] = {
                "allowed": [str(m) for m in (cell.get("allowed") or []) if isinstance(m, str)],
                "default": str(cell.get("default") or ""),
            }
    return base


def catalog_entry(provider: str, usage: str, system_settings: dict | None = None) -> dict:
    """`{provider}/{usage}` の1セル（`{"allowed": [...], "default": "..."}`・無ければ空）。
    `system_settings`（省略可）は `get_catalog()` へそのまま渡す。"""
    return get_catalog(system_settings).get(provider, {}).get(usage) or {"allowed": [], "default": ""}


def resolve_model(provider: str, usage: str, user_settings: dict | None,
                  system_settings: dict | None = None) -> str:
    """モデル名の解決: カタログ既定→組み込み既定→（`_USAGE_FALLBACK` にあれば）別用途で再解決、の順。

    個人設定の個別モデル名（openai_model/gemini_model/ollama_model/codex_model/intent_model/
    search_helper_model）は無い＝`user_settings` は読まない（モデルは常に管理者が使えるモデル
    一覧で管理する・接続テスト（POST /settings/test）も含め、個人の自由入力欄で実 probe に
    任意のモデル名を到達させる経路は無い＝一般ユーザーがカタログ外のモデル名で外部 API を
    叩けてしまう穴を避けるため）。`user_settings` は将来のカタログ以外の解決要因に備えた
    予約引数（現状は未使用）。

    `system_settings`（省略可）は `catalog_entry()` へそのまま渡す。

    `usage` が `_USAGE_FALLBACK` に載っており（現状 `render`→`extract`・`extract` は GRAPH-SRC
    2026-09-04 で `USAGES` から外れた旧用途キーの後方互換読み取り専用）、この用途セル・組み込み
    既定のどちらにも値が無ければ、フォールバック先の用途で改めて解決する（**フォールバック先の
    「今の」解決結果**＝admin が旧 extract 側だけを上書きしていてもそれに追随する。`render` を
    一度も設定していない場合は `resolve_model(provider, "extract", ...)` と完全に同じ値になる）。
    """
    entry = catalog_entry(provider, usage, system_settings)
    value = str(entry.get("default") or "") or hardcoded_fallback(provider, usage)
    if value:
        return value
    fallback_usage = _USAGE_FALLBACK.get(usage)
    if fallback_usage:
        return resolve_model(provider, fallback_usage, user_settings, system_settings)
    return value


def is_valid_model(provider: str, usage: str, value: str, system_settings: dict | None = None) -> bool:
    """`value` がこの1セルで許可されるか（allowed に含まれる／このセルの既定と一致）。
    grandfather（現在保存中の値との一致）はセル単体では判定しない（`field_valid` が一度だけ行う）。
    `system_settings`（省略可）は `catalog_entry()` へそのまま渡す。"""
    entry = catalog_entry(provider, usage, system_settings)
    allowed = set(entry.get("allowed") or [])
    default = str(entry.get("default") or "")
    return value in allowed or (bool(default) and value == default)


def field_valid(field: str, value: str, current: str | None = None, provider: str | None = None,
                system_settings: dict | None = None) -> bool:
    """PUT /settings のモデル名フィールド検証。

    判定順序:
      1. 空文字（クリア＝カタログ既定に従う）は常に許可。
      2. grandfather: `current`（現在 DB に保存中の値）と**完全一致**なら許可（旧・自由入力時代の
         値の再送・no-op 再保存を弾かない・移行期の寛容）。値の一部一致や別セルでの一致では
         grandfather 扱いにしない。
      3. 未知フィールド（`FIELD_CELLS` に無い）はカタログ制約の対象外として許可する。
      4. `provider` が渡された場合（呼び出し側が実効プロバイダを解決できた場合）は、その
         プロバイダのセルだけで判定する（他プロバイダにしか無いモデル名を紛れ込ませない・
         例: `intent_provider=gemini` なのに openai 専用モデル名を保存できてしまう穴を塞ぐ）。
      5. `provider` 未指定でこのフィールドが単一プロバイダのみを跨ぐ場合（例: `openai_model` は
         常に openai・chat/extract の複数用途で共用）は、**全セルの積集合**で判定する（どの用途で
         実際に使われても安全な値だけを許可）。
      6. `provider` 未指定でこのフィールドが複数プロバイダを跨ぐ場合（`intent_model`／
         `search_helper_model`・選択中のプロバイダが auto/未設定で実行時まで決まらない場合）は、
         誤って全滅させないよう**全セルの和集合**で判定する（実効プロバイダが判明する通常の保存
         経路では、呼び出し側が `provider=` を渡して 4 の単一セル判定へ絞り込む・
         `sherpa/routers/system.py::_effective_provider_for_field` 参照）。
    """
    if not value:
        return True
    if current is not None and value == current:
        return True
    cells = FIELD_CELLS.get(field)
    if not cells:
        return True
    if provider is not None:
        cells = [c for c in cells if c[0] == provider]
        if not cells:
            return False
        return all(is_valid_model(p, u, value, system_settings) for p, u in cells)
    providers_in_cells = {p for p, _u in cells}
    if len(providers_in_cells) == 1:
        return all(is_valid_model(p, u, value, system_settings) for p, u in cells)
    return any(is_valid_model(p, u, value, system_settings) for p, u in cells)


def field_choice_info(field: str, provider: str | None = None, system_settings: dict | None = None) -> dict:
    """GET /settings 用: `field` の `<select>` 選択肢（`{"allowed": [...], "default": "..."}`）。

    `provider` が渡された場合（呼び出し側が実効プロバイダを解決できた場合＝
    `sherpa/routers/system.py::_effective_provider_for_field`）は、そのプロバイダのセルだけの
    **積集合**を返す（`field_valid` の単一プロバイダ判定と同じ絞り込み・保存できない選択肢を
    UI に出さないため）。

    `provider` 未指定でこのフィールドが単一プロバイダのみを跨ぐ場合（例: `openai_model` は
    常に openai）も同様に**積集合**を返す（`field_valid` の 5 と同じ判定基準・保存時検証と
    表示される選択肢を一致させる）。

    `provider` 未指定でこのフィールドが複数プロバイダを跨ぐ場合（`intent_model`／
    `search_helper_model`・実効プロバイダが実行時まで決まらない）は、和集合を返す
    （`field_valid` の 6 と同じ寛容な判定・画面には選べるが後で 422 になりうる選択肢が混ざる
    ことがあるのは、実効プロバイダそのものが未確定という前提上避けられない）。

    `system_settings`（省略可）は `catalog_entry()` へそのまま渡す（1レスポンス内で
    複数フィールド分呼ばれても、呼び出し側が同じスナップショットを渡せば読み直さない）。
    """
    cells = FIELD_CELLS.get(field, ())
    if not cells:
        return {"allowed": [], "default": ""}
    if provider is not None:
        cells = tuple(c for c in cells if c[0] == provider) or cells
    providers_in_cells = {p for p, _u in cells}
    allowed: list[str] = []
    if len(providers_in_cells) == 1:
        # 積集合（全セルに共通するモデル名だけ）。順序は最初のセルの allowed 順を保つ。
        sets = [set(catalog_entry(p, u, system_settings).get("allowed") or []) for p, u in cells]
        common = set.intersection(*sets) if sets else set()
        for m in catalog_entry(*cells[0], system_settings).get("allowed") or []:
            if m in common and m not in allowed:
                allowed.append(m)
    else:
        for p, u in cells:
            for m in catalog_entry(p, u, system_settings).get("allowed") or []:
                if m not in allowed:
                    allowed.append(m)
    # 積集合（単一プロバイダ複数用途）の場合、用途ごとに既定が異なることがある（例: openai_model の
    # chat 既定と extract 既定を admin が別々に設定）。先頭セルの既定だけを代表表示すると、実際の
    # 解決（`resolve_model()` は用途ごとに**その用途の**既定を使う）と食い違う「管理者の既定
    # （例: xxx）」という誤表示になり得るため、全セルの既定が一致する場合だけ具体例を示し、
    # 割れている場合は空文字（UI は「管理者の既定を使う」とだけ表示し、用途ごとの実際の既定は
    # 個別には示さない＝誤った代表値を出さない）にする。
    defaults = {str(catalog_entry(p, u, system_settings).get("default") or "") for p, u in cells}
    default = defaults.pop() if len(defaults) == 1 else ""
    return {"allowed": allowed, "default": str(default or "")}


def validate_catalog(value) -> dict | None:
    """管理画面からの `model_catalog` 保存値の検証（`None` は未設定へ戻す＝組み込み既定へ）。

    形式: `{provider: {usage: {"allowed": [str,...], "default": str}}}`。provider は `PROVIDERS` に
    含まれ、かつ `bedrock` ではないことを要求する（bedrock は実在確認済みモデルの専用機構が別にあり、
    ここに静的 allowed を持たせると二重の真実源になるため・モジュール docstring 参照）。usage は
    `USAGES` に含まれることを要求する。未知のセル（typo・対象外プロバイダ）を黙って保存すると、
    UI にも実行にも効かない隠れ設定になるため 422 で拒否する。`default` は非空なら `allowed` に
    含める（含まれていなければ自動的に先頭へ足す＝保存直後に「既定が選択肢に無い」矛盾を作らない）。
    不正な形は `ValueError`（呼び出し側が `HTTPException(422, ...)` に変換する）。
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("model_catalog はオブジェクトで指定してください")
    out: dict = {}
    for provider, usages in value.items():
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("model_catalog のプロバイダ名は空でない文字列で指定してください")
        if provider not in PROVIDERS or provider == "bedrock":
            raise ValueError(
                f"model_catalog の未知/対象外のプロバイダです: {provider}"
                f"（利用可能: {', '.join(p for p in PROVIDERS if p != 'bedrock')}）")
        if not isinstance(usages, dict):
            raise ValueError(f"model_catalog[{provider}] はオブジェクトで指定してください")
        out_usages: dict = {}
        for usage, cell in usages.items():
            if not isinstance(usage, str) or not usage.strip():
                raise ValueError("model_catalog の用途名は空でない文字列で指定してください")
            if usage not in USAGES:
                raise ValueError(f"model_catalog[{provider}] の未知の用途です: {usage}"
                                 f"（利用可能: {', '.join(USAGES)}）")
            if not isinstance(cell, dict):
                raise ValueError(f"model_catalog[{provider}][{usage}] はオブジェクトで指定してください")
            allowed_raw = cell.get("allowed") if cell.get("allowed") is not None else []
            if not isinstance(allowed_raw, list) or not all(isinstance(m, str) for m in allowed_raw):
                raise ValueError(f"model_catalog[{provider}][{usage}].allowed は文字列の配列で指定してください")
            allowed = list(dict.fromkeys(m.strip() for m in allowed_raw if m.strip()))
            default = cell.get("default") if cell.get("default") is not None else ""
            if not isinstance(default, str):
                raise ValueError(f"model_catalog[{provider}][{usage}].default は文字列で指定してください")
            default = default.strip()
            if default and default not in allowed:
                allowed = [default, *allowed]
            # このプロバイダが実際に受け付けるモデル名文法（consumer 側の制約と共通化・モジュール
            # 先頭の `MODEL_NAME_RE`/`CODEX_MODEL_NAME_RE` 参照）を満たさない値は保存できない値
            # （選べても後段で 422／Codex では honest failure になる）ため、ここで拒否する。
            name_re = _model_name_re_for(provider)
            bad = [m for m in allowed if not name_re.fullmatch(m)]
            if bad:
                rule = "英数字で始まり . _ / - のみ・64文字以内（':' 不可）" if provider == "codex" \
                    else "英数字で始まり . _ : / - のみ・128文字以内"
                raise ValueError(
                    f"model_catalog[{provider}][{usage}].allowed に無効なモデル名があります: "
                    f"{', '.join(bad)}（{rule}）")
            out_usages[usage] = {"allowed": allowed, "default": default}
        out[provider] = out_usages
    return out or None


def _seed_candidate() -> dict:
    """初回シード値（組み込み既定のコピー）。`OPENAI_EMBED_MODEL` env が設定済みなら
    openai/embed の既定へ一度だけ取り込む（`sherpa/embeddings.py` の従来の env 直読みを置き換える・
    以後は env を読まない＝ENV-1 の所有原則）。

    env 経由のこの値も、管理 API（`validate_catalog`）が課すのと同じモデル名文法
    （`MODEL_NAME_RE`）を満たさない限り取り込まない（env だけが空白混入・過大長等の無効な値を
    素通りできてしまうと、管理画面では拒否される値が env 経由でだけ紛れ込む食い違いになる）。
    不正な場合は警告ログを残してこの値を無視する（catalog は組み込み既定のまま・起動は止めない）。
    """
    catalog = copy.deepcopy(_DEFAULT_CATALOG)
    raw = (os.environ.get("OPENAI_EMBED_MODEL") or "").strip()
    if raw:
        if not MODEL_NAME_RE.fullmatch(raw):
            _log.warning("OPENAI_EMBED_MODEL の値が不正なため無視します（モデル名文法違反）: %r", raw)
            return catalog
        cell = catalog.setdefault("openai", {}).setdefault("embed", {"allowed": [], "default": ""})
        cell["default"] = raw
        if raw not in cell["allowed"]:
            cell["allowed"] = [raw, *cell["allowed"]]
    return catalog


def seed_catalog_once() -> None:
    """`model_catalog` を env（`OPENAI_EMBED_MODEL` のみ）から一生に一度だけ初期化する
    （`sherpa.api._seed_settings_from_env` と同じ「一度だけ」方式・完了マーカーは
    `model_catalog_seed_version`）。DB 不達時は何もしない（次回起動／`healthz()` の再試行で
    再試行される・`store.seed_system_settings_once` が冪等なため安全）。"""
    try:
        from sherpa import store
        sysset = store.get_system_settings()
    except Exception as e:
        _log.warning("起動時シード（model_catalog）に失敗しました（DB 不達の可能性）: %s", e)
        return
    if sysset.get(_CATALOG_SEED_MARKER_KEY) is not None:
        return
    try:
        candidate = {_CATALOG_KEY: _seed_candidate(), _CATALOG_SEED_MARKER_KEY: _CATALOG_SEED_VERSION}
        applied, _conflicts = store.seed_system_settings_once(candidate, guard_key=_CATALOG_SEED_MARKER_KEY)
        if _CATALOG_KEY in applied:
            _log.info("起動時シード: model_catalog を初期化しました")
    except Exception as e:
        _log.warning("起動時シード（model_catalog）に失敗しました（DB 不達の可能性）: %s", e)
