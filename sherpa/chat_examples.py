"""チャット画面のクイック入力例（ウェルカム画面のチップ）の管理者設定。

system_settings のキー `chat_examples`（`{"enabled": bool, "items": [str, ...]}`）。
- 未設定＝表示する・組み込み4例（`DEFAULT_ITEMS`）を使う。
- `enabled=false`＝非表示。
- `enabled=true` かつ `items` が空（trim 後を含む）＝明示的に非表示。
- `enabled=true` かつ `items` が非空＝その内容を表示する。
他の sherpa モジュールを import しない葉ノード。
"""
from __future__ import annotations

MAX_ITEMS = 8
MAX_ITEM_LENGTH = 200

# 組み込み既定。フロント側にも同じ文言を持ち（`web/chat/state.js::DEFAULT_EXAMPLES`）、`GET /settings` は未設定時 None を返す
DEFAULT_ITEMS = (
    '消費税率を変更すると、影響がありそうな箇所を教えてください。',
    '夜間バッチが異常終了しました。原因の候補を教えてください。',
    '消費税の端数処理の仕様を教えてください。',
    '登録されている資料の内容を要約した概要資料を作ってください。',
)


def validate(value):
    """`chat_examples` の検証。None は未設定。不正形式は ValueError。

    `items` は文字列配列のみ。各要素を trim し、空は除外。件数（`MAX_ITEMS`）・長さ（`MAX_ITEM_LENGTH`）の上限を超えたら拒否する。
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("chat_examples は {enabled, items} の形式で指定してください")
    enabled = value.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise ValueError("chat_examples.enabled は true/false で指定してください")
    out = {"enabled": True if enabled is None else enabled}
    items = value.get("items")
    if items is None:
        out["items"] = []
        return out
    if not isinstance(items, list):
        raise ValueError("chat_examples.items は文字列の配列で指定してください")
    if len(items) > MAX_ITEMS:
        raise ValueError(f"chat_examples.items は最大{MAX_ITEMS}件までです")
    norm = []
    for it in items:
        if not isinstance(it, str):
            raise ValueError("chat_examples.items の各要素は文字列で指定してください")
        t = it.strip()
        if not t:
            continue
        if len(t) > MAX_ITEM_LENGTH:
            raise ValueError(f"chat_examples.items の各要素は{MAX_ITEM_LENGTH}文字以内で指定してください")
        norm.append(t)
    out["items"] = norm
    return out


def _raw(system_settings: dict) -> dict | None:
    val = (system_settings or {}).get("chat_examples")
    return val if isinstance(val, dict) else None


def effective_examples(system_settings: dict) -> list[str]:
    """実際に表示する例文（非表示なら空リスト）。未設定＝組み込み既定。"""
    cfg = _raw(system_settings)
    if cfg is None:
        return list(DEFAULT_ITEMS)
    if not cfg.get("enabled", True):
        return []
    items = cfg.get("items")
    return list(items) if isinstance(items, list) else []


def public_examples(system_settings: dict) -> list[str] | None:
    """`GET /settings` が返す値。None＝未設定（フロントが組み込み既定を使う）。配列（空含む）＝管理者の明示設定。"""
    if _raw(system_settings) is None:
        return None
    return effective_examples(system_settings)
