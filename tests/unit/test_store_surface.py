"""store 公開名スナップショット + `_SCHEMA` 内容ハッシュ（リファクタリング計画フェーズ4 S1 の保険）。

`sherpa/store.py` → `sherpa/store/` パッケージ化（db.py 切り出し・以降 S2〜S11 の各スライス）を
「純移動」で行うことを担保する。分割前後で `sherpa.store` の公開名一覧（フィルタ後）と `_SCHEMA`
の内容が一致することを golden で固定し、意図しない挙動変化・facade 抜け漏れを検知する。

フィルタ規則（golden 生成・比較の両方で同一ロジックを使う）:
  - `dir(sherpa.store)` から dunder 名（`__xxx__`）を除外する。
    理由: モジュール→パッケージ化で `__path__` 等パッケージ固有の dunder が新規に現れ、
    分割そのもの（挙動不変のはず）とは無関係にスナップショットが壊れるため。
  - 値が `inspect.ismodule()` の属性（stdlib/3rd-party の import そのもの。例: os/json/hashlib/
    contextlib/psycopg・将来スライスで増える `db` 等の submodule）を除外する。
    理由: どのサブモジュールがどの stdlib を import するかは実装都合であり、S1〜S11 で
    スライスが進むたびに変わりうる。ドメイン関数・定数・例外クラスなど「意味のある」公開名
    だけを対象にする。
  - 上記2条件に該当しない名前（public 関数・定数・例外クラス・facade で re-export された
    非モジュール値）は private 名（`_` 始まり）であっても**すべて対象**にする。tests/ や
    sherpa/ が `store._audit_insert`・`store._redact`・`store._connect`・`store._dsn`・
    `store._ensure`・`store._compute_retention`・`store._audit_entry_hash`・
    `store._audit_canonical`・`store._safe_share_answer`・
    `store._invalidate_system_settings_cache`・`store._REDACT_KEYS`・`store._REDACTED_TEXT`・
    `store._SETTINGS_FIELDS`・`store.AnnouncementOrderError` 等を直接参照/monkeypatch するため、
    これらが golden から漏れないことも本テストで担保する（後段の allowlist テスト参照）。

golden の更新手順（**意図した増減のときだけ**実行し、差分を目視確認してからコミットする）:

    SHERPA_USE_FIXTURES=1 PYTHONPATH=. .venv/bin/python -c \
        "import sys; sys.path.insert(0, 'tests/unit'); \
         import test_store_surface as t; t._write_goldens()"
"""
from __future__ import annotations

import hashlib
import inspect
import pathlib

import pytest

pytestmark = pytest.mark.unit

from sherpa import store

_SURFACE_GOLDEN = pathlib.Path(__file__).resolve().parent / "goldens" / "store_surface.txt"
_SCHEMA_HASH_GOLDEN = pathlib.Path(__file__).resolve().parent / "goldens" / "store_schema_hash.txt"

# tests/・sherpa/ が `store.X` の形で直接参照/monkeypatch する私的名（事前分析で洗い出し済み・
# docs/proposals/2026-07-02-リファクタリング計画.md フェーズ4）。golden から漏れていないかの
# allowlist（golden 自体は dir() スナップショットなので、これは golden の下位互換チェック）。
REQUIRED_PRIVATE_NAMES = (
    "_audit_insert",
    "_redact",
    "_connect",
    "_dsn",
    "_ensure",
    "_compute_retention",
    "_audit_entry_hash",
    "_audit_canonical",
    "_safe_share_answer",
    "_invalidate_system_settings_cache",
    "_REDACT_KEYS",
    "_REDACTED_TEXT",
    "_SETTINGS_FIELDS",
    "AnnouncementOrderError",
)


def _public_surface_names() -> list[str]:
    """`sherpa.store` の属性一覧をフィルタ規則（module docstring 参照）に従って抽出する。"""
    return sorted(
        name for name in dir(store)
        if not name.startswith("__") and not inspect.ismodule(getattr(store, name))
    )


def _schema_hash() -> str:
    return hashlib.sha256("\n".join(store._SCHEMA).encode("utf-8")).hexdigest()


def _write_goldens() -> None:
    """現状の store から両 golden を書き出す（更新手順は module docstring 参照）。"""
    _SURFACE_GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    _SURFACE_GOLDEN.write_text("\n".join(_public_surface_names()) + "\n", encoding="utf-8")
    _SCHEMA_HASH_GOLDEN.write_text(_schema_hash() + "\n", encoding="utf-8")


def test_store_public_surface_matches_golden():
    """`sherpa.store` の公開名一覧（フィルタ後）が golden と完全一致する。

    store.py → store/ パッケージ化（フェーズ4 S1 以降の各スライス）で facade からの
    再エクスポート漏れが起きていないかを検知する保険。
    """
    assert _SURFACE_GOLDEN.exists(), f"golden 未生成: {_SURFACE_GOLDEN}（module docstring の手順で作成）"
    expected = _SURFACE_GOLDEN.read_text(encoding="utf-8").splitlines()
    actual = _public_surface_names()
    assert actual == expected, (
        "store の公開名一覧が golden と不一致。意図した増減なら module docstring の手順で golden を更新すること。\n"
        f"追加された名前: {sorted(set(actual) - set(expected))}\n"
        f"消えた名前: {sorted(set(expected) - set(actual))}"
    )


def test_store_surface_includes_names_referenced_by_tests_and_sherpa():
    """tests/・sherpa/ が `store.X` で直接参照/monkeypatch する私的名が golden に含まれることを確認する。"""
    surface = set(_public_surface_names())
    missing = [n for n in REQUIRED_PRIVATE_NAMES if n not in surface]
    assert not missing, f"store の公開名一覧から漏れている（tests/sherpa が直接参照する）: {missing}"


def test_schema_content_hash_matches_golden():
    """`_SCHEMA`（DDL文リスト）の内容ハッシュが golden と一致する（純移動の証明）。

    store.py → store/db.py への `_SCHEMA` 移動で DDL の中身が一文字も変わっていないことを保証する。
    意図的に DDL を追加/変更する場合のみ、module docstring の手順で golden を更新する。
    """
    assert _SCHEMA_HASH_GOLDEN.exists(), f"golden 未生成: {_SCHEMA_HASH_GOLDEN}（module docstring の手順で作成）"
    expected = _SCHEMA_HASH_GOLDEN.read_text(encoding="utf-8").strip()
    assert _schema_hash() == expected, (
        "_SCHEMA の内容ハッシュが golden と不一致。意図した DDL 変更なら "
        "module docstring の手順で golden を更新すること。"
    )
