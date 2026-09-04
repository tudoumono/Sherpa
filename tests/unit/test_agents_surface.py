"""agents 公開名スナップショット + `__file__` 由来値の pin（リファクタリング計画フェーズ5 S1 の保険）。

`sherpa/agents.py`（2,055行）→ `sherpa/providers/` パッケージ化（フェーズ5 S2〜S11 の各スライス）を
「純移動」で行うことを担保する。分割前後で `sherpa.agents` の公開名一覧（フィルタ後）が一致すること、
および `Path(__file__).resolve().parents[1]`（＝リポジトリ root）由来の値がサブパッケージへ移動しても
黙って壊れない（実際は分割時に明示修正が要る＝S1 で先に固定し、修正漏れを検知する）ことを golden／
snapshot で固定する。

フィルタ規則（golden 生成・比較の両方で同一ロジックを使う。tests/unit/test_store_surface.py と同一方針）:
  - `dir(sherpa.agents)` から dunder 名（`__xxx__`）を除外する。
    理由: モジュール→パッケージ化で `__path__` 等パッケージ固有の dunder が新規に現れ、
    分割そのもの（挙動不変のはず）とは無関係にスナップショットが壊れるため。
  - 値が `inspect.ismodule()` の属性（stdlib/3rd-party の import そのもの。例: os/re/json/logging・
    `anthropic`・`urllib`（`import urllib.request` 由来）・`codex_agents_md`/`codex_skills`/`llm`
    （`from . import ...`）を除外する。
    理由: どのモジュールがどの stdlib/隣接モジュールを import するかは実装都合であり、S2〜S11 で
    スライスが進むたびに変わりうる（例: urllib は現状 agents.py 直下だが、分割後は codex/provider.py
    や codex/mcp.py 側に移る）。ドメイン関数・定数・クラスなど「意味のある」公開名だけを対象にする。
    `urllib` はこの規則で golden から除外されるため、`urllib` バインドの存在は別の
    `test_agents_urllib_is_module_bound_on_facade` で個別 pin する
    （`tests/unit/test_usage_capture.py` が `A.urllib.request.urlopen` を patch するため）。
  - 上記2条件に該当しない名前（public 関数・定数・クラス・facade で re-export された非モジュール値）は
    private 名（`_` 始まり）であっても**すべて対象**にする。tests/ や sherpa/ が直接参照/monkeypatch する
    私的名（`REQUIRED_PRIVATE_OR_PUBLIC_NAMES` 参照）が golden から漏れないことを本テストで担保する
    （後段の allowlist テスト参照）。

golden の更新手順（**意図した増減のときだけ**実行し、差分を目視確認してからコミットする）:

    SHERPA_USE_FIXTURES=1 PYTHONPATH=. .venv/bin/python -c \
        "import sys; sys.path.insert(0, 'tests/unit'); \
         import test_agents_surface as t; t._write_goldens()"

---

`__file__` 由来値 snapshot（危険地雷1・計画書フェーズ5節）:

agents.py には `Path(__file__).resolve().parents[1]`（＝リポジトリ root）由来の値が5箇所ある:
  1. `_kb_hint_abs`（線178付近）
  2. `_kb_read_roots`（線1110付近）
  3. `_marp_bin`（線1157付近）
  4. `_write_codex_authoring_config` 内の MCP サブプロセス PYTHONPATH（線1267付近）
  5. `_run_authoring` フレーム内の marp テーマ探索 `skills_base`（線1904付近・`Path(__file__).resolve().parent`）

このうち 1〜4 は本ファイルで関数呼び出しを通じて「実リポジトリ root を指す」ことを pin する。
5（skills_base）は元々 `_run_authoring` の巨大な生成器フレーム内でしか評価されず（呼び出しには
codex サブプロセス起動一式が要る）、S1 の時点では単体で pin できなかった。**S10（codex/provider.py
への移動スライス）で `_SKILLS_BASE` というモジュール定数に切り出し**（`_run_authoring` はこの定数を
参照するだけに変更）、生成器フレームを実行しなくても定数値そのものを直接 pin できるようにした
（`test_skills_base_points_at_repo_sherpa_dir` 参照）。
"""
from __future__ import annotations

import inspect
import os
import pathlib

import pytest

pytestmark = pytest.mark.unit

from sherpa import agents as A
from sherpa import worlds as W

_SURFACE_GOLDEN = pathlib.Path(__file__).resolve().parent / "goldens" / "agents_surface.txt"

# tests/・sherpa/ が `agents.X`（AGENT_PROVIDERS 経由の import・monkeypatch を含む）の形で直接
# 参照/monkeypatch する名前（事前分析・依頼元の棚卸しに基づく）。golden から漏れていないかの
# allowlist（golden 自体は dir() スナップショットなので、これは golden の下位互換チェック）。
REQUIRED_NAMES = (
    # production: 外部（routers/health.py 等）・テストが参照する公開/私的名
    "AGENT_PROVIDERS", "Ctx", "get_provider", "provider_info",
    "BedrockProvider", "BEDROCK_MODEL_CHOICES", "BEDROCK_MODEL_ID_RE",
    "_BEDROCK_MODEL", "_bedrock_auth_available", "_bedrock_profile_label",
    "_redact_bedrock_secret", "_web_search_admin_allowed",
    "list_bedrock_inference_profiles", "_bedrock_region", "_bedrock_error_detail",
    "_codex_sandbox_enabled", "_kb_read_roots", "_write_codex_authoring_config",
    # テストが patch/直接参照する名前（urllib は ismodule 除外のため別テストで pin）
    "_gather", "CodexProvider", "HeuristicProvider", "OpenAIProvider",
    "OllamaProvider", "GeminiProvider", "_GenProvider", "_plain_run", "_facts",
    "_kb_hint", "_mcp_env", "_mcp_config_args", "_mcp_neighbors_from",
    "_apply_codex_neighbors", "_authoring_lock", "_codex_ask_question",
    "_codex_ask_capture", "_codex_mcp_enabled", "_marp_bin", "_detect_chrome_path",
    "_select_provider", "_SKILLS_BASE",
)


def _public_surface_names() -> list[str]:
    """`sherpa.agents` の属性一覧をフィルタ規則（module docstring 参照）に従って抽出する。"""
    return sorted(
        name for name in dir(A)
        if not name.startswith("__") and not inspect.ismodule(getattr(A, name))
    )


def _write_goldens() -> None:
    """現状の agents から golden を書き出す（更新手順は module docstring 参照）。"""
    _SURFACE_GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    _SURFACE_GOLDEN.write_text("\n".join(_public_surface_names()) + "\n", encoding="utf-8")


def test_agents_public_surface_matches_golden():
    """`sherpa.agents` の公開名一覧（フィルタ後）が golden と完全一致する。

    agents.py → providers/ パッケージ化（フェーズ5 S2 以降の各スライス）で facade からの
    再エクスポート漏れが起きていないかを検知する保険。
    """
    assert _SURFACE_GOLDEN.exists(), f"golden 未生成: {_SURFACE_GOLDEN}（module docstring の手順で作成）"
    expected = _SURFACE_GOLDEN.read_text(encoding="utf-8").splitlines()
    actual = _public_surface_names()
    assert actual == expected, (
        "agents の公開名一覧が golden と不一致。意図した増減なら module docstring の手順で golden を更新すること。\n"
        f"追加された名前: {sorted(set(actual) - set(expected))}\n"
        f"消えた名前: {sorted(set(expected) - set(actual))}"
    )


def test_agents_surface_includes_names_referenced_by_tests_and_sherpa():
    """tests/・sherpa/ が `agents.X` で直接参照/monkeypatch する名前が golden に含まれることを確認する。"""
    surface = set(_public_surface_names())
    missing = [n for n in REQUIRED_NAMES if n not in surface]
    assert not missing, f"agents の公開名一覧から漏れている（tests/sherpa が直接参照する）: {missing}"


def test_agents_urllib_is_module_bound_on_facade():
    """`urllib`（`import urllib.request` 由来）はフィルタ規則で golden から除外されるが、
    facade（`sherpa.agents`）に module として存在し続けることを別途 pin する。

    `tests/unit/test_usage_capture.py` が `A.urllib.request.urlopen` を monkeypatch するため、
    分割後も agents.py（facade）に `import urllib.request` が残っている必要がある。
    """
    assert hasattr(A, "urllib"), "agents.urllib が無い（urllib.request の facade 束縛が消えている）"
    assert inspect.ismodule(A.urllib), "agents.urllib がモジュールでない"
    assert hasattr(A.urllib, "request"), "agents.urllib.request が無い"


# ===== `__file__` 由来値（危険地雷1）: repo root を指すことの pin =====

def test_kb_read_roots_fallback_points_at_repo_data_kb(monkeypatch):
    """`_kb_read_roots()` は fixtures でも登録 world でも解決できない時、`<repo>/data/kb` へ
    フォールバックする（`Path(__file__).resolve().parents[1]` 由来）。

    DB 状態に依存させない（unit テストは外部サービス不要の方針）ため、`worlds._fixtures`／
    `worlds.world_dir` を直接 monkeypatch してフォールバック分岐を決定的に踏む。
    """
    monkeypatch.setattr(W, "_fixtures", lambda: False)
    monkeypatch.setattr(W, "world_dir", lambda world_id: None)
    roots = A._kb_read_roots("no-such-world-s1-pin")
    repo_root = pathlib.Path(A.__file__).resolve().parents[1]
    assert roots == [str((repo_root / "data" / "kb").resolve())]


def test_marp_bin_search_root_matches_repo_root(monkeypatch):
    """`_marp_bin()`（`SHERPA_MARP_BIN` 未設定時）の探索基点が `<repo>/tools/marp/...` であることを pin。

    このリポジトリ/worktree には marp CLI が実インストールされていない場合があり、
    存在チェック（`is_file`/`os.access`）がそのままだと常に None になって基点を検証できない。
    そのため存在チェックだけ monkeypatch で成立させ、返る絶対パスの中身（探索基点）を確認する。
    """
    monkeypatch.delenv("SHERPA_MARP_BIN", raising=False)
    monkeypatch.setattr(pathlib.Path, "is_file", lambda self: True)
    monkeypatch.setattr(os, "access", lambda *a, **k: True)
    result = A._marp_bin()
    repo_root = pathlib.Path(A.__file__).resolve().parents[1]
    expected = repo_root / "tools" / "marp" / "node_modules" / ".bin" / "marp"
    assert result == str(expected)


def test_kb_hint_abs_contains_repo_root_path(monkeypatch):
    """`_kb_hint_abs()` は fixtures モードで `<repo>/fixtures/corpus/{world}` を含む文字列を返す
    （`Path(__file__).resolve().parents[1]` 由来）。fixtures 経路は worlds registry（DB）に依存しない。
    """
    monkeypatch.setenv("SHERPA_USE_FIXTURES", "1")
    text = A._kb_hint_abs("v1")
    repo_root = pathlib.Path(A.__file__).resolve().parents[1]
    expected_base = repo_root / "fixtures" / "corpus" / "v1"
    assert str(expected_base) in text


def test_write_codex_authoring_config_pythonpath_points_at_repo_root(tmp_path, monkeypatch):
    """`_write_codex_authoring_config(mcp=True)` が書く MCP サブプロセス env の `PYTHONPATH` が
    実リポジトリ root を指すことを、生成された config.toml を実際に TOML パースして pin する。

    呼び出しパターンは既存 `tests/unit/test_codex_workspace_authoring.py` の
    `test_codex_mcp_creds_in_config_file_not_cmdline` を踏襲（fixtures モード・world="test"）。
    """
    monkeypatch.setenv("SHERPA_USE_FIXTURES", "1")
    try:
        import tomllib
    except ModuleNotFoundError:
        pytest.skip("py<3.11 は tomllib 無し")
    ch = tmp_path / "ch"
    A._write_codex_authoring_config(ch, ["/kb"], "low", True, "test", None)
    cfg_text = (ch / "config.toml").read_text(encoding="utf-8")
    parsed = tomllib.loads(cfg_text)
    pythonpath = parsed["mcp_servers"]["sherpa"]["env"]["PYTHONPATH"]
    repo_root = pathlib.Path(A.__file__).resolve().parents[1]
    assert pythonpath == str(repo_root)


def test_skills_base_points_at_repo_sherpa_dir():
    """`_SKILLS_BASE`（危険地雷1の5番目・S10 で `_run_authoring` から切り出したモジュール定数）が
    `<repo>/sherpa/skills_base`（＝`Path(sherpa.agents.__file__).resolve().parent`）を指すことを pin する。

    移動前は `Path(__file__).resolve().parent`（agents.py 基準）だったが、`sherpa/providers/codex/
    provider.py` は `sherpa` から2階層深いため、同じ場所を指すには `parents[2]` が必要
    （黙って `sherpa/providers/codex` を指してしまう地雷だった・module docstring 参照）。
    """
    expected = pathlib.Path(A.__file__).resolve().parent / "skills_base"
    assert A._SKILLS_BASE == expected
