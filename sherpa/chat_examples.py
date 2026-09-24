"""チャット画面のクイック入力例（ウェルカム画面のチップ）の管理者設定。

導入先ごとに例文を差し替えたい要望に対応する（設定所有の原則＝運用ポリシーは system_settings/UI が
唯一の持ち主・env フォールバックは作らない）。system_settings のキーは `chat_examples`
（`{"enabled": bool, "items": [str, ...]}`）。

- 未設定（キー自体が無い・None）＝**既定動作**: 表示する・組み込み4例（`DEFAULT_ITEMS`）を使う。
- `enabled=false`＝例ブロック自体を非表示（`items` の値によらず）。
- `enabled=true` かつ `items=[]`（trim 後に残る要素が無い場合を含む）＝**明示的に非表示**
  （「組み込み既定を出す」ではなく非表示＝空にした意図を尊重する）。
- `enabled=true` かつ `items` が非空＝その内容を表示する（組み込み既定は使わない）。

このモジュールは他の sherpa モジュールを import しない（`depth_profile.py` と同じ葉ノード原則）。
呼び出し元（`sherpa/routers/system.py`（`GET /settings` へ配信）・
`sherpa/routers/system_extras.py`（`_validate_chat_examples`・`_admin_settings_view`））が
`system_settings` の dict をそのまま渡す。
"""
from __future__ import annotations

MAX_ITEMS = 8
MAX_ITEM_LENGTH = 200

# 組み込み既定（旧・web/chat/state.js にハードコードされていた4例をそのまま移設）。4レンズ
# （影響/トラブル/仕様問い合わせ/資料作成）を一通り示す。フロント側にも同じ文言を built-in
# フォールバック（`web/chat/state.js::DEFAULT_EXAMPLES`）として持たせている——`GET /settings` の
# `chat_examples` は未設定時に None を返し（本モジュールの値を再送しない）、フロントが自分の
# 組み込み既定を使う契約（設定取得の失敗時にも同じ既定へ fail-open するため）。
DEFAULT_ITEMS = (
    '消費税率を変更すると、影響がありそうな箇所を教えてください。',
    '夜間バッチが異常終了しました。原因の候補を教えてください。',
    '消費税の端数処理の仕様を教えてください。',
    '登録されている資料の内容を要約した概要資料を作ってください。',
)


def validate(value):
    """`chat_examples`（system_settings）の検証。None は未設定（既定へ）。不正形式は ValueError
    （呼び出し側＝`system_extras.py::_validate_chat_examples` が 422 へ変換する）。

    `items` は文字列配列のみ許可し、各要素を trim・trim 後に空になった要素は除外する。件数上限
    （`MAX_ITEMS`）・長さ上限（trim 後 `MAX_ITEM_LENGTH` 文字）を超えたら拒否する。
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
            continue          # trim 後に空＝除外（明示的な空行等を無視する）
        if len(t) > MAX_ITEM_LENGTH:
            raise ValueError(f"chat_examples.items の各要素は{MAX_ITEM_LENGTH}文字以内で指定してください")
        norm.append(t)
    out["items"] = norm
    return out


def _raw(system_settings: dict) -> dict | None:
    val = (system_settings or {}).get("chat_examples")
    return val if isinstance(val, dict) else None


def effective_examples(system_settings: dict) -> list[str]:
    """実際に表示する例文（管理画面の「実効値」表示・非表示なら空リスト）。

    未設定＝組み込み既定（`DEFAULT_ITEMS`）。`enabled=false`／`items` が実質空＝空リスト
    （＝非表示）。
    """
    cfg = _raw(system_settings)
    if cfg is None:
        return list(DEFAULT_ITEMS)
    if not cfg.get("enabled", True):
        return []
    items = cfg.get("items")
    return list(items) if isinstance(items, list) else []


def public_examples(system_settings: dict) -> list[str] | None:
    """`GET /settings`（非 admin・チャット画面向け）が返す値。

    None＝未設定（フロントは自分の組み込み既定 `web/chat/state.js::DEFAULT_EXAMPLES` を使う）。
    配列（空含む）＝管理者が明示設定済み＝その内容をそのまま表示に使う（空配列＝非表示）。
    """
    if _raw(system_settings) is None:
        return None
    return effective_examples(system_settings)
