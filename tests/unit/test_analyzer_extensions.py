"""拡張の契約 S4（docs/21-拡張の契約.md）の単体テスト。

対象:
  - `registry.discover_extension_analyzers()`: `<prefix>_*.py` 発見規約・契約違反の fail-loud
    （接頭辞不一致・拡張子衝突・version<1）・名前順ソート。
  - `registry.config_signature()`: 部品ごとの `version` が材料に載ること・上流の
    `CODE_ANALYZERS_SCHEMA_VERSION` を変えても拡張部品の材料（name, version, extensions）は不変
    （§4・部品ごとの署名の独立）。
  - `sample_ext_dummy.py`（受け入れ用サンプル拡張）が本番ディレクトリから実際に発見されること。
  - `scripts/verify_extension.py`: 4面の行を出力し、未対応の面（provider/アーム/MCPツール）は
    「未確認」と明示すること。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import scripts.verify_extension as verify_extension
import pytest

from sherpa.ingest.analyzers import registry
from sherpa.ingest.analyzers._base import Analyzer


_VALID_MODULE_TEMPLATE = '''
from sherpa.ingest.analyzers._base import Analyzer, DefItem, DefResult, RefResult


class _Analyzer(Analyzer):
    name = {name!r}
    doctype = 'ext-test'
    extensions = frozenset({{{ext!r}}})
    version = {version}

    def collect_defs(self, text, rel_path):
        return DefResult()

    def extract_refs(self, text, rel_path):
        return RefResult()


ANALYZER = _Analyzer()
'''


def _write_module(tmp_path: Path, filename: str, *, name: str, ext: str, version: int = 1) -> None:
    (tmp_path / filename).write_text(
        _VALID_MODULE_TEMPLATE.format(name=name, ext=ext, version=version), encoding="utf-8")


# ---- 発見（サンプル拡張・名前順） ----

def test_sample_extension_is_discovered_from_the_real_package_directory():
    """受け入れ用サンプル拡張（`sample_ext_dummy.py`）が本番ディレクトリから発見され、
    `known_analyzers()` の末尾に登録されていること。"""
    discovered = registry.discover_extension_analyzers()
    names = [a.name for a in discovered]
    assert "sample_ext:dummy" in names

    known_names = [a.name for a in registry.known_analyzers()]
    assert known_names[-1] == "sample_ext:dummy" or "sample_ext:dummy" in known_names[-len(discovered):]


def test_discover_extension_analyzers_orders_by_name_not_by_filename(tmp_path):
    """発見順は登録名（`ANALYZER.name`）順——ファイル名の並びとは独立。"""
    _write_module(tmp_path, "zz_first.py", name="zz:bbb", ext=".zzbbb")
    _write_module(tmp_path, "aa_second.py", name="aa:aaa", ext=".aaaext")
    discovered = registry.discover_extension_analyzers(tmp_path)
    assert [a.name for a in discovered] == ["aa:aaa", "zz:bbb"]


def test_discover_extension_analyzers_skips_files_without_analyzer_attribute(tmp_path):
    """`ANALYZER` 属性を持たない `<prefix>_*.py` は拡張アナライザを名乗っていないとみなし読み飛ばす
    （黙って落とすのは"契約違反"のときだけ・単に無関係なファイルはエラーにしない）。"""
    (tmp_path / "myfw_helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    assert registry.discover_extension_analyzers(tmp_path) == ()


def test_discover_extension_analyzers_skips_underscore_and_upstream_named_files(tmp_path):
    """`_` 始まり・上流モジュール stem・`registry` は発見対象から除外される。"""
    (tmp_path / "_internal_helper.py").write_text(
        "from sherpa.ingest.analyzers._base import Analyzer\nANALYZER = None\n", encoding="utf-8")
    (tmp_path / "cobol.py").write_text(
        "from sherpa.ingest.analyzers._base import Analyzer\nANALYZER = None\n", encoding="utf-8")
    assert registry.discover_extension_analyzers(tmp_path) == ()


# ---- 契約違反（fail-loud） ----

def test_discover_extension_analyzers_raises_on_prefix_mismatch(tmp_path):
    """`ANALYZER.name` の prefix がファイル名の接頭辞と一致しなければ `ExtensionAnalyzerError`。"""
    _write_module(tmp_path, "myfw_thing.py", name="othername:kind", ext=".myfwthing")
    try:
        registry.discover_extension_analyzers(tmp_path)
        raise AssertionError("prefix 不一致で例外が発生しませんでした")
    except registry.ExtensionAnalyzerError as e:
        assert "prefix" in str(e)


def test_discover_extension_analyzers_raises_on_extension_collision_with_upstream(tmp_path):
    """上流アナライザの拡張子と衝突し、`overrides` で明示されていなければ `ExtensionAnalyzerError`。"""
    _write_module(tmp_path, "myfw_java.py", name="myfw:java", ext=".java")
    try:
        registry.discover_extension_analyzers(tmp_path)
        raise AssertionError("拡張子衝突で例外が発生しませんでした")
    except registry.ExtensionAnalyzerError as e:
        assert ".java" in str(e)


def test_discover_extension_analyzers_raises_on_version_below_one(tmp_path):
    """`ANALYZER.version` が1未満なら `ExtensionAnalyzerError`。"""
    _write_module(tmp_path, "myfw_thing.py", name="myfw:thing", ext=".myfwthing", version=0)
    try:
        registry.discover_extension_analyzers(tmp_path)
        raise AssertionError("version<1 で例外が発生しませんでした")
    except registry.ExtensionAnalyzerError as e:
        assert "version" in str(e)


def test_discover_extension_analyzers_raises_on_reserved_prefix(tmp_path):
    """予約語（上流の言語名・内部モジュール名）を接頭辞に使うと `ExtensionAnalyzerError`。"""
    _write_module(tmp_path, "java_thing.py", name="java:thing", ext=".javathing")
    try:
        registry.discover_extension_analyzers(tmp_path)
        raise AssertionError("予約語接頭辞で例外が発生しませんでした")
    except registry.ExtensionAnalyzerError as e:
        assert "予約" in str(e)


def test_discover_extension_analyzers_raises_on_extension_collision_between_two_extension_analyzers(tmp_path):
    """拡張アナライザ同士でも拡張子が重なれば `ExtensionAnalyzerError`（上流との衝突と同じ扱い）。"""
    _write_module(tmp_path, "aaa_thing.py", name="aaa:thing", ext=".dupext")
    _write_module(tmp_path, "bbb_thing.py", name="bbb:thing", ext=".dupext")
    try:
        registry.discover_extension_analyzers(tmp_path)
        raise AssertionError("拡張アナライザ同士の拡張子衝突で例外が発生しませんでした")
    except registry.ExtensionAnalyzerError as e:
        assert ".dupext" in str(e)


def test_discover_extension_analyzers_raises_on_duplicate_name_between_extension_analyzers(tmp_path):
    """拡張アナライザ同士で `ANALYZER.name` が重なれば `ExtensionAnalyzerError`（拡張子が別でも検出する）。"""
    _write_module(tmp_path, "myfw_a.py", name="myfw:thing", ext=".dupname1")
    _write_module(tmp_path, "myfw_b.py", name="myfw:thing", ext=".dupname2")
    try:
        registry.discover_extension_analyzers(tmp_path)
        raise AssertionError("拡張アナライザ同士の名前重複で例外が発生しませんでした")
    except registry.ExtensionAnalyzerError as e:
        assert "myfw:thing" in str(e)


def test_discover_extension_analyzers_raises_on_uppercase_extension(tmp_path):
    """`ANALYZER.extensions` に大文字を含む拡張子があれば `ExtensionAnalyzerError`
    （照合側 `_ext()` が小文字化するため、大文字宣言は永久に担当なしになる＝黙って不発を防ぐ）。"""
    _write_module(tmp_path, "myfw_thing.py", name="myfw:thing", ext=".MyExt")
    try:
        registry.discover_extension_analyzers(tmp_path)
        raise AssertionError("大文字拡張子で例外が発生しませんでした")
    except registry.ExtensionAnalyzerError as e:
        assert "MyExt" in str(e)


def test_discover_extension_analyzers_raises_on_extension_missing_leading_dot(tmp_path):
    """`ANALYZER.extensions` の要素が `.` で始まっていなければ `ExtensionAnalyzerError`。"""
    _write_module(tmp_path, "myfw_thing.py", name="myfw:thing", ext="myext")
    try:
        registry.discover_extension_analyzers(tmp_path)
        raise AssertionError("'.' なし拡張子で例外が発生しませんでした")
    except registry.ExtensionAnalyzerError as e:
        assert "myext" in str(e)


def test_discover_extension_analyzers_raises_when_analyzer_present_but_filename_has_no_underscore(tmp_path):
    """`ANALYZER` 属性を持つファイルは、ファイル名に `_` が無くても黙って読み飛ばさない
    （`_` を含まない＝命名規約 `<prefix>_*.py` 違反として `ExtensionAnalyzerError`）。"""
    (tmp_path / "myfw.py").write_text(
        "from sherpa.ingest.analyzers._base import Analyzer, DefResult, RefResult\n\n"
        "class _Analyzer(Analyzer):\n"
        "    name = 'myfw:thing'\n"
        "    doctype = \'ext-test\'\n"
        "    extensions = frozenset({'.myfwthing'})\n"
        "    version = 1\n\n"
        "    def collect_defs(self, text, rel_path):\n"
        "        return DefResult()\n\n"
        "    def extract_refs(self, text, rel_path):\n"
        "        return RefResult()\n\n"
        "ANALYZER = _Analyzer()\n",
        encoding="utf-8")
    try:
        registry.discover_extension_analyzers(tmp_path)
        raise AssertionError("_ 無し stem で ANALYZER を持つのに例外が発生しませんでした")
    except registry.ExtensionAnalyzerError as e:
        assert "myfw.py" in str(e)


def test_discover_extension_analyzers_raises_on_multi_segment_extension(tmp_path):
    """`.d.ts` のような多段拡張子は `_ext()`（`PurePosixPath.suffix`）が単一区切りしか返さないため
    永久に担当なしになる——大文字宣言と同じ理由で契約違反。"""
    _write_module(tmp_path, "myfw_thing.py", name="myfw:thing", ext=".d.ts")
    try:
        registry.discover_extension_analyzers(tmp_path)
        raise AssertionError("多段拡張子で例外が発生しませんでした")
    except registry.ExtensionAnalyzerError as e:
        assert ".d.ts" in str(e)


def test_discover_extension_analyzers_raises_on_empty_extensions(tmp_path):
    """`ANALYZER.extensions` が空集合なら `ExtensionAnalyzerError`。"""
    (tmp_path / "myfw_thing.py").write_text(
        "from sherpa.ingest.analyzers._base import Analyzer, DefResult, RefResult\n\n"
        "class _Analyzer(Analyzer):\n"
        "    name = 'myfw:thing'\n"
        "    doctype = \'ext-test\'\n"
        "    extensions = frozenset()\n"
        "    version = 1\n\n"
        "    def collect_defs(self, text, rel_path):\n"
        "        return DefResult()\n\n"
        "    def extract_refs(self, text, rel_path):\n"
        "        return RefResult()\n\n"
        "ANALYZER = _Analyzer()\n",
        encoding="utf-8")
    try:
        registry.discover_extension_analyzers(tmp_path)
        raise AssertionError("extensions 空で例外が発生しませんでした")
    except registry.ExtensionAnalyzerError as e:
        assert "extensions" in str(e)


def test_discover_extension_analyzers_raises_on_extensions_not_a_set(tmp_path):
    """`ANALYZER.extensions` が `list` 等 `set`/`frozenset` でなければ `ExtensionAnalyzerError`
    （黙って `TypeError` の traceback にしない・後続の集合演算が list 相手に失敗する前に検査する）。"""
    (tmp_path / "myfw_thing.py").write_text(
        "from sherpa.ingest.analyzers._base import Analyzer, DefResult, RefResult\n\n"
        "class _Analyzer(Analyzer):\n"
        "    name = 'myfw:thing'\n"
        "    doctype = \'ext-test\'\n"
        "    extensions = ['.x']\n"
        "    version = 1\n\n"
        "    def collect_defs(self, text, rel_path):\n"
        "        return DefResult()\n\n"
        "    def extract_refs(self, text, rel_path):\n"
        "        return RefResult()\n\n"
        "ANALYZER = _Analyzer()\n",
        encoding="utf-8")
    try:
        registry.discover_extension_analyzers(tmp_path)
        raise AssertionError("extensions が list で例外が発生しませんでした")
    except registry.ExtensionAnalyzerError as e:
        assert "extensions" in str(e)


def test_discover_extension_analyzers_raises_on_version_not_an_int(tmp_path):
    """`ANALYZER.version` が `int` でなければ（例: 文字列）`ExtensionAnalyzerError`
    （黙って `TypeError` の traceback にしない・後続の比較が str 相手に失敗する前に検査する）。"""
    (tmp_path / "myfw_thing.py").write_text(
        "from sherpa.ingest.analyzers._base import Analyzer, DefResult, RefResult\n\n"
        "class _Analyzer(Analyzer):\n"
        "    name = 'myfw:thing'\n"
        "    doctype = \'ext-test\'\n"
        "    extensions = frozenset({'.myfwthing'})\n"
        "    version = '2'\n\n"
        "    def collect_defs(self, text, rel_path):\n"
        "        return DefResult()\n\n"
        "    def extract_refs(self, text, rel_path):\n"
        "        return RefResult()\n\n"
        "ANALYZER = _Analyzer()\n",
        encoding="utf-8")
    try:
        registry.discover_extension_analyzers(tmp_path)
        raise AssertionError("version が str で例外が発生しませんでした")
    except registry.ExtensionAnalyzerError as e:
        assert "version" in str(e)


def test_extension_collision_allowed_when_declared_via_overrides(tmp_path):
    """`ANALYZER.overrides` で明示した拡張子は上流との衝突があっても許可される。"""
    (tmp_path / "myfw_java.py").write_text(
        "from sherpa.ingest.analyzers._base import Analyzer, DefResult, RefResult\n\n"
        "class _Analyzer(Analyzer):\n"
        "    name = 'myfw:java'\n"
        "    doctype = \'ext-test\'\n"
        "    extensions = frozenset({'.java'})\n"
        "    overrides = frozenset({'.java'})\n"
        "    version = 1\n\n"
        "    def collect_defs(self, text, rel_path):\n"
        "        return DefResult()\n\n"
        "    def extract_refs(self, text, rel_path):\n"
        "        return RefResult()\n\n"
        "ANALYZER = _Analyzer()\n",
        encoding="utf-8")
    discovered = registry.discover_extension_analyzers(tmp_path)
    assert [a.name for a in discovered] == ["myfw:java"]


def test_extension_collision_between_two_extension_analyzers_allowed_when_both_share_upstream_override(tmp_path):
    """同じ上流拡張子（`.java`）を2本の拡張アナライザが両方 `overrides` で明示的に共有する場合は
    例外として許可される——衝突判定・所有者記録は `extensions` 全体で行うが（`overrides` の除外は
    上流との衝突判定だけに使う）、両方が同じ上流拡張子を明示的に共有していれば拡張アナライザ同士の
    衝突としては扱わない。"""
    for filename, name in (("myfw_java.py", "myfw:java"), ("otherfw_java.py", "otherfw:java")):
        (tmp_path / filename).write_text(
            "from sherpa.ingest.analyzers._base import Analyzer, DefResult, RefResult\n\n"
            "class _Analyzer(Analyzer):\n"
            f"    name = {name!r}\n"
            "    doctype = \'ext-test\'\n"
            "    extensions = frozenset({'.java'})\n"
            "    overrides = frozenset({'.java'})\n"
            "    version = 1\n\n"
            "    def collect_defs(self, text, rel_path):\n"
            "        return DefResult()\n\n"
            "    def extract_refs(self, text, rel_path):\n"
            "        return RefResult()\n\n"
            "ANALYZER = _Analyzer()\n",
            encoding="utf-8")
    discovered = registry.discover_extension_analyzers(tmp_path)
    assert [a.name for a in discovered] == ["myfw:java", "otherfw:java"]


def test_extension_collision_between_two_extension_analyzers_still_raises_for_non_upstream_ext_even_with_overrides(tmp_path):
    """`overrides` の除外は**上流との衝突判定だけ**に使う——拡張アナライザ同士の衝突判定は
    `extensions` 全体で行うため、上流に無い独自拡張子を一方だけが `overrides` に載せていても、
    もう一方が同じ拡張子を宣言していれば依然として衝突する（`overrides` を自己申告するだけで
    拡張アナライザ同士の衝突検出から外れてしまう、という抜け穴の是正）。"""
    (tmp_path / "myfw_thing.py").write_text(
        "from sherpa.ingest.analyzers._base import Analyzer, DefResult, RefResult\n\n"
        "class _Analyzer(Analyzer):\n"
        "    name = 'myfw:thing'\n"
        "    doctype = \'ext-test\'\n"
        "    extensions = frozenset({'.customext'})\n"
        "    overrides = frozenset({'.customext'})\n"
        "    version = 1\n\n"
        "    def collect_defs(self, text, rel_path):\n"
        "        return DefResult()\n\n"
        "    def extract_refs(self, text, rel_path):\n"
        "        return RefResult()\n\n"
        "ANALYZER = _Analyzer()\n",
        encoding="utf-8")
    (tmp_path / "otherfw_thing.py").write_text(
        "from sherpa.ingest.analyzers._base import Analyzer, DefResult, RefResult\n\n"
        "class _Analyzer(Analyzer):\n"
        "    name = 'otherfw:thing'\n"
        "    doctype = \'ext-test\'\n"
        "    extensions = frozenset({'.customext'})\n"
        "    version = 1\n\n"
        "    def collect_defs(self, text, rel_path):\n"
        "        return DefResult()\n\n"
        "    def extract_refs(self, text, rel_path):\n"
        "        return RefResult()\n\n"
        "ANALYZER = _Analyzer()\n",
        encoding="utf-8")
    try:
        registry.discover_extension_analyzers(tmp_path)
        raise AssertionError("独自拡張子の衝突が overrides の自己申告だけで見逃されました")
    except registry.ExtensionAnalyzerError as e:
        assert ".customext" in str(e)


# ---- config_signature（部品ごとの版・上流版上げからの独立） ----

def test_config_signature_material_includes_version_per_analyzer():
    """`config_signature()` の材料は `(name, version, extensions)` の3要素タプル。"""
    for name, version, extensions in registry.config_signature()[1]:
        analyzer = next(a for a in registry.known_analyzers() if a.name == name)
        assert version == analyzer.version
        assert extensions == tuple(sorted(analyzer.extensions))


def test_bumping_one_analyzer_version_does_not_change_other_analyzers_material(monkeypatch):
    """ある部品の `version` を上げても、他の部品の材料タプルはそれぞれ独立のまま不変
    （署名の独立・拡張の契約 S4・保証範囲は「署名の独立まで」）。"""
    before = dict((item[0], item) for item in registry.config_signature()[1])

    target = registry.known_analyzers()[0]
    monkeypatch.setattr(target, "version", target.version + 1)

    after = dict((item[0], item) for item in registry.config_signature()[1])
    assert after[target.name] != before[target.name]
    for name, item in before.items():
        if name == target.name:
            continue
        assert after[name] == item, f"{name} の材料が無関係な version 変更で変わってしまいました"


def test_upstream_schema_version_bump_leaves_sample_extension_material_unchanged(monkeypatch):
    """上流の `CODE_ANALYZERS_SCHEMA_VERSION` を変えても、サンプル拡張の材料
    （name, version, extensions）は不変（拡張の契約 S4・保証範囲）。署名全体（schema version を
    含む先頭要素）は変わる——それは正しい（世代署名が変わる＝全 world 再構築が1回起きる）。"""
    before_sig = registry.config_signature()
    before_sample = next(item for item in before_sig[1] if item[0] == "sample_ext:dummy")

    monkeypatch.setattr(registry, "CODE_ANALYZERS_SCHEMA_VERSION",
                        registry.CODE_ANALYZERS_SCHEMA_VERSION + 1)

    after_sig = registry.config_signature()
    after_sample = next(item for item in after_sig[1] if item[0] == "sample_ext:dummy")

    assert after_sample == before_sample                # 部品の材料は不変
    assert after_sig[0] != before_sig[0]                 # 核の分類契約版は変わる
    assert after_sig != before_sig                       # 署名全体は変わる（世代署名も変わる）


# ---- verify_extension（4面の出力） ----

def test_verify_extension_reports_all_four_extension_surfaces_and_exits_zero(capsys):
    exit_code = verify_extension.main([])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "## アナライザ" in out
    assert "## 頭脳 provider" in out
    assert "## 変換アーム" in out
    assert "## MCP ツール" in out
    # アナライザ面は本スライスの実装対象＝実際に確認する（「未確認」ではない）。
    analyzer_section = out.split("## 頭脳 provider")[0]
    assert "確認した契約" in analyzer_section
    assert "未確認" not in analyzer_section
    # 他3面は「未確認」と明示する（保証済みと言わない）。
    for heading in ("## 頭脳 provider", "## 変換アーム", "## MCP ツール"):
        section = out.split(heading, 1)[1].split("## ", 1)[0]
        assert "未確認" in section


def test_verify_extension_fails_loudly_when_an_analyzer_violates_the_return_type_contract(monkeypatch):
    """アナライザが `DefResult`/`RefResult` 以外を返したら `verify_extension` は非ゼロで終了する
    （黙って通さない）。"""
    class _BadAnalyzer(Analyzer):
        name = "bad:contract"
        doctype = "ext-test"
        extensions = frozenset({".badcontract"})
        version = 1

        def collect_defs(self, text, rel_path):
            return "not a DefResult"

        def extract_refs(self, text, rel_path):
            return None

    monkeypatch.setattr(registry, "_ANALYZERS", (*registry.known_analyzers(), _BadAnalyzer()))
    exit_code = verify_extension.main([])
    assert exit_code != 0


def test_verify_extension_fails_loudly_when_accepts_does_not_return_bool(monkeypatch, capsys):
    """`accepts()` が `bool` 以外（例: `None`）を返したら `verify_extension` は非ゼロで終了し、
    `NG:` 行に bool と明記する（戻り値の型契約は `accepts` にもかかる）。"""
    class _BadAcceptsAnalyzer(Analyzer):
        name = "badaccepts"                   # コロン無し（拡張アナライザの再発見比較の対象外にする）
        doctype = "ext-test"
        extensions = frozenset({".badaccepts"})
        version = 1

        def accepts(self, rel_path, head_text=""):
            return None

        def collect_defs(self, text, rel_path):
            from sherpa.ingest.analyzers._base import DefResult
            return DefResult()

        def extract_refs(self, text, rel_path):
            from sherpa.ingest.analyzers._base import RefResult
            return RefResult()

    monkeypatch.setattr(registry, "_ANALYZERS", (*registry.known_analyzers(), _BadAcceptsAnalyzer()))
    exit_code = verify_extension.main([])
    out = capsys.readouterr().out
    assert exit_code != 0
    assert "badaccepts" in out and "bool" in out


def test_verify_extension_uses_isinstance_not_type_name_for_def_and_ref_result(monkeypatch, capsys):
    """`collect_defs`/`extract_refs` の戻り値検査は `isinstance`（`_base.DefResult`/`_base.RefResult`）
    で行う——同名だが無関係なクラス（`type(x).__name__` だけ一致する偽物）は型名比較では見逃すが、
    `isinstance` なら検出できる。"""
    class DefResult:                          # 本物と同名の別クラス（isinstance では偽物と判定される）
        pass

    class RefResult:
        pass

    class _DuckTypedAnalyzer(Analyzer):
        name = "ducktyped"                    # コロン無し（拡張アナライザの再発見比較の対象外にする
                                               # ——colon 付きだと再発見との不一致という別の違反で
                                               # exit_code が非ゼロになり、isinstance 検査自体を
                                               # 確認したことにならない）。
        extensions = frozenset({".ducktyped"})
        version = 1

        def collect_defs(self, text, rel_path):
            return DefResult()

        def extract_refs(self, text, rel_path):
            return RefResult()

    monkeypatch.setattr(registry, "_ANALYZERS", (*registry.known_analyzers(), _DuckTypedAnalyzer()))
    exit_code = verify_extension.main([])
    out = capsys.readouterr().out
    assert exit_code != 0
    assert "ducktyped" in out
    assert "DefResult" in out and "RefResult" in out


def test_verify_extension_subprocess_reports_ng_and_exits_nonzero_for_planted_discovery_violation(tmp_path):
    """`registry` の import 自体が発見時契約違反で失敗するケースを一時ディレクトリへ複製した実体で
    subprocess 実行し、traceback ではなく `NG:` 行を出して非ゼロで終えることを確認する
    （従来は import 時に traceback で落ち `NG:` が一度も出なかった＝docs/21-拡張の契約.md §5 の
    記述と不一致だった是正の受け入れ）。"""
    repo_root = Path(__file__).resolve().parents[2]
    work = tmp_path / "repo"
    shutil.copytree(repo_root / "sherpa", work / "sherpa",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(repo_root / "scripts", work / "scripts",
                    ignore=shutil.ignore_patterns("__pycache__"))
    # 契約違反を仕込む: 予約語 `java` を接頭辞に使う拡張アナライザ（発見時に ExtensionAnalyzerError）。
    (work / "sherpa" / "ingest" / "analyzers" / "java_planted.py").write_text(
        "from sherpa.ingest.analyzers._base import Analyzer, DefResult, RefResult\n\n"
        "class _Analyzer(Analyzer):\n"
        "    name = 'java:planted'\n"
        "    doctype = \'ext-test\'\n"
        "    extensions = frozenset({'.javaplanted'})\n"
        "    version = 1\n\n"
        "    def collect_defs(self, text, rel_path):\n"
        "        return DefResult()\n\n"
        "    def extract_refs(self, text, rel_path):\n"
        "        return RefResult()\n\n"
        "ANALYZER = _Analyzer()\n",
        encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(work / "scripts" / "verify_extension.py")],
        cwd=work, capture_output=True, text=True, timeout=60)
    assert result.returncode != 0
    assert "NG:" in result.stdout
    assert "Traceback" not in result.stdout
    # アナライザ面の発見失敗により、残り3面（provider/アーム/MCP ツール）は評価せず
    # 「未確認（アナライザ面の違反により未評価）」と明示する——`registry` に依存する import を
    # 辿って同じ例外を再発させない（NG: の後に4面の見出しが全部出て非ゼロで終わる）。
    assert "## 頭脳 provider" in result.stdout
    assert "## 変換アーム" in result.stdout
    assert "## MCP ツール" in result.stdout
    for heading in ("## 頭脳 provider", "## 変換アーム", "## MCP ツール"):
        section = result.stdout.split(heading, 1)[1].split("## ", 1)[0]
        assert "未確認（アナライザ面の違反により未評価）" in section


def test_discover_extension_analyzers_raises_on_overrides_list_and_non_str_extension(tmp_path):
    """overrides の型ミス・拡張子要素の型ミスも契約違反（TypeError の traceback にしない）。"""
    import textwrap
    d = tmp_path / "analyzers"; d.mkdir()
    (d / "ovl_x.py").write_text(textwrap.dedent("""
        from sherpa.ingest.analyzers._base import Analyzer, DefResult, RefResult
        class A(Analyzer):
            name = "ovl:x"; doctype = "ext-test"; extensions = frozenset({".ovlx"}); overrides = [".java"]; version = 1
            def collect_defs(self, text, rel_path): return DefResult()
            def extract_refs(self, text, rel_path): return RefResult()
        ANALYZER = A()
    """), encoding="utf-8")
    with pytest.raises(registry.ExtensionAnalyzerError):
        registry.discover_extension_analyzers(d)
    (d / "ovl_x.py").unlink()
    (d / "intext_y.py").write_text(textwrap.dedent("""
        from sherpa.ingest.analyzers._base import Analyzer, DefResult, RefResult
        class A(Analyzer):
            name = "intext:y"; doctype = "ext-test"; extensions = frozenset({1}); version = 1
            def collect_defs(self, text, rel_path): return DefResult()
            def extract_refs(self, text, rel_path): return RefResult()
        ANALYZER = A()
    """), encoding="utf-8")
    with pytest.raises(registry.ExtensionAnalyzerError):
        registry.discover_extension_analyzers(d)


def test_discover_extension_analyzers_raises_on_non_str_name(tmp_path):
    """name の型ミスも契約違反（TypeError の traceback にしない）。"""
    import textwrap
    d = tmp_path / "analyzers"; d.mkdir()
    (d / "nm_x.py").write_text(textwrap.dedent("""
        from sherpa.ingest.analyzers._base import Analyzer, DefResult, RefResult
        class A(Analyzer):
            name = 1; extensions = frozenset({".nmx"}); version = 1
            def collect_defs(self, text, rel_path): return DefResult()
            def extract_refs(self, text, rel_path): return RefResult()
        ANALYZER = A()
    """), encoding="utf-8")
    with pytest.raises(registry.ExtensionAnalyzerError):
        registry.discover_extension_analyzers(d)


def test_discover_extension_analyzers_raises_when_doctype_missing(tmp_path):
    """doctype 未宣言（既定 ""）の拡張は黙って登録しない（種別が空文字で流れる）。"""
    import textwrap
    d = tmp_path / "analyzers"; d.mkdir()
    (d / "nodt_x.py").write_text(textwrap.dedent("""
        from sherpa.ingest.analyzers._base import Analyzer, DefResult, RefResult
        class A(Analyzer):
            name = "nodt:x"; extensions = frozenset({".nodtx"}); version = 1
            def collect_defs(self, text, rel_path): return DefResult()
            def extract_refs(self, text, rel_path): return RefResult()
        ANALYZER = A()
    """), encoding="utf-8")
    with pytest.raises(registry.ExtensionAnalyzerError):
        registry.discover_extension_analyzers(d)


def test_discover_extension_analyzers_rejects_version_inherited_from_upstream_class(tmp_path):
    """上流クラスを継承した拡張が version を省略すると上流の版を継承し署名の独立が破れる＝違反。"""
    import textwrap
    d = tmp_path / "analyzers"; d.mkdir()
    (d / "inh_java.py").write_text(textwrap.dedent("""
        from sherpa.ingest.analyzers.java import JavaAnalyzer
        class A(JavaAnalyzer):
            name = "inh:java"; doctype = "ext-test"; extensions = frozenset({".inhjava"})
        ANALYZER = A()
    """), encoding="utf-8")
    with pytest.raises(registry.ExtensionAnalyzerError):
        registry.discover_extension_analyzers(d)
    (d / "inh_java.py").write_text(textwrap.dedent("""
        from sherpa.ingest.analyzers.java import JavaAnalyzer
        class A(JavaAnalyzer):
            name = "inh:java"; doctype = "ext-test"; extensions = frozenset({".inhjava"}); version = 1
        ANALYZER = A()
    """), encoding="utf-8")
    assert [a.name for a in registry.discover_extension_analyzers(d)] == ["inh:java"]


def test_discover_extension_analyzers_rejects_document_extension_without_overrides(tmp_path):
    """資料側の拡張子（.md/.docx）を overrides 無しで担当宣言すると違反（資料の分類経路を黙って奪わない）。"""
    import textwrap
    d = tmp_path / "analyzers"; d.mkdir()
    for ext in (".md", ".docx", ".png"):
        (d / "docext_x.py").write_text(textwrap.dedent(f"""
            from sherpa.ingest.analyzers._base import Analyzer, DefResult, RefResult
            class A(Analyzer):
                name = "docext:x"; doctype = "ext-test"; extensions = frozenset({{{ext!r}}}); version = 1
                def collect_defs(self, text, rel_path): return DefResult()
                def extract_refs(self, text, rel_path): return RefResult()
            ANALYZER = A()
        """), encoding="utf-8")
        with pytest.raises(registry.ExtensionAnalyzerError):
            registry.discover_extension_analyzers(d)


def test_document_extension_collision_set_matches_corpus_docs_and_text_kind():
    """registry の資料拡張子の写しが corpus_docs／text_kind の定義と同期している（写しのズレを fail-loud）。"""
    from sherpa import corpus_docs
    from sherpa.ingest import office_md, text_kind
    expected = (set(corpus_docs._NONCODE_DOCTYPE) | set(corpus_docs._OFFICE_DOCTYPE)
                | set(text_kind.DOCUMENT_EXT) | set(office_md.IMAGE_EXT))
    assert registry._noncode_document_extensions() == frozenset(expected)


def test_sensitive_extensions_cannot_be_claimed_even_with_overrides(tmp_path):
    """秘匿ファイル拡張子（.env/.pem 等）は overrides でも担当不可（秘匿除外を無効化させない）。"""
    import textwrap
    from sherpa.ingest import text_kind
    assert registry._SENSITIVE_EXT_FOR_COLLISION == frozenset(text_kind.SENSITIVE_EXT)
    d = tmp_path / "analyzers"; d.mkdir()
    (d / "sens_x.py").write_text(textwrap.dedent("""
        from sherpa.ingest.analyzers._base import Analyzer, DefResult, RefResult
        class A(Analyzer):
            name = "sens:x"; doctype = "ext-test"; extensions = frozenset({".env"}); overrides = frozenset({".env"}); version = 1
            def collect_defs(self, text, rel_path): return DefResult()
            def extract_refs(self, text, rel_path): return RefResult()
        ANALYZER = A()
    """), encoding="utf-8")
    with pytest.raises(registry.ExtensionAnalyzerError):
        registry.discover_extension_analyzers(d)


def test_sensitive_named_files_stay_excluded_even_when_an_extension_claims_their_suffix(monkeypatch, tmp_path):
    """名前規約で秘匿と判定されるファイル（id_rsa.old・.env.local）は、拡張アナライザがその拡張子
    （.old/.local）を担当していても分類経路から外れる（秘匿除外は担当の有無によらない）。"""
    from sherpa import corpus_docs
    from sherpa.ingest.analyzers._base import Analyzer, DefResult, RefResult

    class Old(Analyzer):
        name = "t:old"; doctype = "ext-test"; extensions = frozenset({".old", ".local"}); version = 1
        def collect_defs(self, text, rel_path): return DefResult()
        def extract_refs(self, text, rel_path): return RefResult()

    monkeypatch.setattr(registry, "_ANALYZERS", registry._UPSTREAM_ANALYZERS + (Old(),))
    for rel, ext in (("keys/id_rsa.old", ".old"), ("conf/.env.local", ".local")):
        v = corpus_docs.classify_document(rel, ext, lambda size=4096: "")
        assert v["kind"] == "document" and v["doctype"] is None, (rel, v)
    v = corpus_docs.classify_document("src/prog.old", ".old", lambda size=4096: "")
    assert v["kind"] == "code"     # 秘匿でない .old は担当どおり


def test_build_world_skips_sensitive_named_files_even_if_an_analyzer_claims_the_suffix(tmp_path):
    """グラフ取り込み（Pass1）も秘匿ファイル（名前規約）を読まない。上流構成でも `.env.yaml` は
    YAML アナライザの候補になるため、拡張子ではなく名前で除外する。"""
    from sherpa.ingest import world_graph
    (tmp_path / "conf").mkdir()
    (tmp_path / "conf" / ".env.yaml").write_text("secret_key: SHOULD_NOT_APPEAR\n", encoding="utf-8")
    (tmp_path / "conf" / "app.yaml").write_text("app_name: ok\n", encoding="utf-8")
    files = [(tmp_path / "conf" / ".env.yaml", "conf/.env.yaml"), (tmp_path / "conf" / "app.yaml", "conf/app.yaml")]
    nodes, edges, flags = world_graph.build_world(tmp_path, "w", files=files)[:3]
    blob = repr(nodes) + repr(edges) + repr(flags)
    assert "SHOULD_NOT_APPEAR" not in blob and ".env.yaml" not in blob
    assert "app.yaml" in blob

