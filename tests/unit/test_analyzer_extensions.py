"""拡張の契約 S4（docs/21-拡張の契約.md）の単体テスト。

対象: `registry.discover_extension_analyzers()` の発見規約と契約違反の fail-loud、
`registry.config_signature()` の部品ごとの署名の独立、サンプル拡張の発見、
`scripts/verify_extension.py` の4面出力、秘匿ファイルの除外。
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


_OMIT = object()
_METHODS = (
    "\n    def collect_defs(self, text, rel_path):\n        return DefResult()\n"
    "\n    def extract_refs(self, text, rel_path):\n        return RefResult()\n"
)


def _src(*, name="'myfw:thing'", doctype="'ext-test'", extensions="frozenset({'.myfwthing'})",
         version="1", overrides=_OMIT, base="Analyzer") -> str:
    """拡張アナライザのモジュール本文。値はソース式の文字列、_OMIT で属性ごと省略する。"""
    attrs = (("name", name), ("doctype", doctype), ("extensions", extensions),
             ("overrides", overrides), ("version", version))
    body = "".join(f"    {k} = {v}\n" for k, v in attrs if v is not _OMIT)
    return (
        "from sherpa.ingest.analyzers._base import Analyzer, DefResult, RefResult\n"
        "from sherpa.ingest.analyzers.java import JavaAnalyzer\n\n"
        f"class _Analyzer({base}):\n{body}"
        + (_METHODS if base == "Analyzer" else "")
        + "\nANALYZER = _Analyzer()\n"
    )


def _write(d: Path, files: dict[str, str]) -> Path:
    for filename, text in files.items():
        (d / filename).write_text(text, encoding="utf-8")
    return d


def _names(d: Path) -> list[str]:
    return [a.name for a in registry.discover_extension_analyzers(d)]


# ---- 発見 ----

def test_sample_extension_is_discovered_from_the_real_package_directory():
    discovered = registry.discover_extension_analyzers()
    assert "sample_ext:dummy" in [a.name for a in discovered]
    known_names = [a.name for a in registry.known_analyzers()]
    assert known_names[-1] == "sample_ext:dummy" or "sample_ext:dummy" in known_names[-len(discovered):]


def test_discover_orders_by_name_not_by_filename(tmp_path):
    _write(tmp_path, {"zz_first.py": _src(name="'zz:bbb'", extensions="frozenset({'.zzbbb'})"),
                      "aa_second.py": _src(name="'aa:aaa'", extensions="frozenset({'.aaaext'})")})
    assert _names(tmp_path) == ["aa:aaa", "zz:bbb"]


@pytest.mark.parametrize("filename,text", [
    ("myfw_helper.py", "VALUE = 1\n"),   # ANALYZER を持たない無関係なファイルはエラーにしない
    ("_internal_helper.py", "ANALYZER = None\n"),   # _ 始まりは対象外
    ("cobol.py", "ANALYZER = None\n"),   # 上流モジュール stem は対象外
])
def test_discover_skips_non_extension_files(tmp_path, filename, text):
    _write(tmp_path, {filename: text})
    assert registry.discover_extension_analyzers(tmp_path) == ()


# ---- 契約違反（fail-loud） ----

@pytest.mark.parametrize("files,needle", [
    ({"myfw_thing.py": _src(name="'othername:kind'")}, "prefix"),
    ({"myfw_java.py": _src(name="'myfw:java'", extensions="frozenset({'.java'})")}, ".java"),
    ({"myfw_thing.py": _src(version="0")}, "version"),
    ({"java_thing.py": _src(name="'java:thing'", extensions="frozenset({'.javathing'})")}, "予約"),
    ({"aaa_thing.py": _src(name="'aaa:thing'", extensions="frozenset({'.dupext'})"),
      "bbb_thing.py": _src(name="'bbb:thing'", extensions="frozenset({'.dupext'})")}, ".dupext"),
    ({"myfw_a.py": _src(extensions="frozenset({'.dupname1'})"),
      "myfw_b.py": _src(extensions="frozenset({'.dupname2'})")}, "myfw:thing"),
    ({"myfw_thing.py": _src(extensions="frozenset({'.MyExt'})")}, "MyExt"),
    ({"myfw_thing.py": _src(extensions="frozenset({'myext'})")}, "myext"),
    # ANALYZER を持つのにファイル名に _ が無い＝命名規約違反として読み飛ばさない
    ({"myfw.py": _src()}, "myfw.py"),
    ({"myfw_thing.py": _src(extensions="frozenset({'.d.ts'})")}, ".d.ts"),
    ({"myfw_thing.py": _src(extensions="frozenset()")}, "extensions"),
    ({"myfw_thing.py": _src(extensions="['.x']")}, "extensions"),
    ({"myfw_thing.py": _src(version="'2'")}, "version"),
    # overrides の自己申告だけでは拡張アナライザ同士の衝突検出から外れない
    ({"myfw_thing.py": _src(extensions="frozenset({'.customext'})", overrides="frozenset({'.customext'})"),
      "otherfw_thing.py": _src(name="'otherfw:thing'", extensions="frozenset({'.customext'})")}, ".customext"),
    # 型ミスは TypeError の traceback でなく契約違反
    ({"ovl_x.py": _src(name="'ovl:x'", extensions="frozenset({'.ovlx'})", overrides="['.java']")}, None),
    ({"intext_y.py": _src(name="'intext:y'", extensions="frozenset({1})")}, None),
    ({"nm_x.py": _src(name="1", doctype=_OMIT, extensions="frozenset({'.nmx'})")}, None),
    ({"nodt_x.py": _src(name="'nodt:x'", doctype=_OMIT, extensions="frozenset({'.nodtx'})")}, None),
    # 上流クラスを継承して version を省略＝署名の独立が破れる
    ({"inh_java.py": _src(name="'inh:java'", extensions="frozenset({'.inhjava'})", version=_OMIT,
                          base="JavaAnalyzer")}, None),
    # 資料側の拡張子は overrides 無しで担当宣言できない
    *[({"docext_x.py": _src(name="'docext:x'", extensions="frozenset({%r})" % ext)}, None)
      for ext in (".md", ".docx", ".png")],
    # 秘匿拡張子は overrides でも担当不可
    ({"sens_x.py": _src(name="'sens:x'", extensions="frozenset({'.env'})", overrides="frozenset({'.env'})")}, None),
])
def test_discover_raises_on_contract_violation(tmp_path, files, needle):
    _write(tmp_path, files)
    with pytest.raises(registry.ExtensionAnalyzerError) as ei:
        registry.discover_extension_analyzers(tmp_path)
    if needle is not None:
        assert needle in str(ei.value)


@pytest.mark.parametrize("files,expected", [
    ({"myfw_java.py": _src(name="'myfw:java'", extensions="frozenset({'.java'})",
                           overrides="frozenset({'.java'})")}, ["myfw:java"]),
    # 同じ上流拡張子を両方が overrides で明示共有するなら衝突として扱わない
    ({"myfw_java.py": _src(name="'myfw:java'", extensions="frozenset({'.java'})",
                           overrides="frozenset({'.java'})"),
      "otherfw_java.py": _src(name="'otherfw:java'", extensions="frozenset({'.java'})",
                              overrides="frozenset({'.java'})")}, ["myfw:java", "otherfw:java"]),
    # 継承した拡張が version を明示すれば通る
    ({"inh_java.py": _src(name="'inh:java'", extensions="frozenset({'.inhjava'})", base="JavaAnalyzer")},
     ["inh:java"]),
])
def test_discover_allows_declared_overrides_and_explicit_version(tmp_path, files, expected):
    _write(tmp_path, files)
    assert _names(tmp_path) == expected


# ---- config_signature（部品ごとの版・上流版上げからの独立） ----

def test_config_signature_material_includes_version_per_analyzer():
    for name, version, extensions in registry.config_signature()[1]:
        analyzer = next(a for a in registry.known_analyzers() if a.name == name)
        assert version == analyzer.version
        assert extensions == tuple(sorted(analyzer.extensions))


def test_bumping_one_analyzer_version_does_not_change_other_analyzers_material(monkeypatch):
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
    # アナライザ面は実際に確認する（「未確認」ではない）。
    analyzer_section = out.split("## 頭脳 provider")[0]
    assert "確認した契約" in analyzer_section
    assert "未確認" not in analyzer_section
    # 他3面は「未確認」と明示する。
    for heading in ("## 頭脳 provider", "## 変換アーム", "## MCP ツール"):
        section = out.split(heading, 1)[1].split("## ", 1)[0]
        assert "未確認" in section


def test_verify_extension_fails_loudly_when_an_analyzer_violates_the_return_type_contract(monkeypatch):
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
    assert verify_extension.main([]) != 0


def test_verify_extension_fails_loudly_when_accepts_does_not_return_bool(monkeypatch, capsys):
    class _BadAcceptsAnalyzer(Analyzer):
        name = "badaccepts"                   # コロン無し（拡張アナライザの再発見比較の対象外）
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
    """同名だが無関係なクラスは型名比較では見逃すが isinstance なら検出できる。"""
    class DefResult:
        pass

    class RefResult:
        pass

    class _DuckTypedAnalyzer(Analyzer):
        name = "ducktyped"                    # コロン無し（再発見との不一致という別の違反を混ぜない）
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
    """registry の import 自体が発見時契約違反で失敗しても、traceback でなく `NG:` 行を出して
    非ゼロで終え、残り3面は「未評価」と明示する。"""
    repo_root = Path(__file__).resolve().parents[2]
    work = tmp_path / "repo"
    shutil.copytree(repo_root / "sherpa", work / "sherpa",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(repo_root / "scripts", work / "scripts",
                    ignore=shutil.ignore_patterns("__pycache__"))
    # 予約語 `java` を接頭辞に使う拡張アナライザ（発見時に ExtensionAnalyzerError）。
    (work / "sherpa" / "ingest" / "analyzers" / "java_planted.py").write_text(
        _src(name="'java:planted'", extensions="frozenset({'.javaplanted'})"), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(work / "scripts" / "verify_extension.py")],
        cwd=work, capture_output=True, text=True, timeout=60)
    assert result.returncode != 0
    assert "NG:" in result.stdout
    assert "Traceback" not in result.stdout
    for heading in ("## 頭脳 provider", "## 変換アーム", "## MCP ツール"):
        assert heading in result.stdout
        section = result.stdout.split(heading, 1)[1].split("## ", 1)[0]
        assert "未確認（アナライザ面の違反により未評価）" in section


# ---- 資料・秘匿の除外 ----

def test_document_extension_collision_set_matches_corpus_docs_and_text_kind():
    from sherpa import corpus_docs
    from sherpa.ingest import office_md, text_kind
    expected = (set(corpus_docs._NONCODE_DOCTYPE) | set(corpus_docs._OFFICE_DOCTYPE)
                | set(text_kind.DOCUMENT_EXT) | set(office_md.IMAGE_EXT))
    assert registry._noncode_document_extensions() == frozenset(expected)


def test_sensitive_extension_set_matches_text_kind():
    from sherpa.ingest import text_kind
    assert registry._SENSITIVE_EXT_FOR_COLLISION == frozenset(text_kind.SENSITIVE_EXT)


def test_sensitive_named_files_stay_excluded_even_when_an_extension_claims_their_suffix(monkeypatch):
    """名前規約で秘匿と判定されるファイル（id_rsa.old・.env.local）は、拡張アナライザがその拡張子を
    担当していても分類経路から外れる。"""
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
    """グラフ取り込み（Pass1）も秘匿ファイルを名前で除外する（`.env.yaml` は YAML アナライザの候補になる）。"""
    from sherpa.ingest import world_graph
    (tmp_path / "conf").mkdir()
    (tmp_path / "conf" / ".env.yaml").write_text("secret_key: SHOULD_NOT_APPEAR\n", encoding="utf-8")
    (tmp_path / "conf" / "app.yaml").write_text("app_name: ok\n", encoding="utf-8")
    files = [(tmp_path / "conf" / ".env.yaml", "conf/.env.yaml"), (tmp_path / "conf" / "app.yaml", "conf/app.yaml")]
    nodes, edges, flags = world_graph.build_world(tmp_path, "w", files=files)[:3]
    blob = repr(nodes) + repr(edges) + repr(flags)
    assert "SHOULD_NOT_APPEAR" not in blob and ".env.yaml" not in blob
    assert "app.yaml" in blob
