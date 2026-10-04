"""拡張の契約 S4（docs/21-拡張の契約.md）の単体テスト。

対象: `registry.discover_extension_analyzers()` の発見規約と契約違反の fail-loud、
`registry.config_signature()` の部品ごとの署名の独立、サンプル拡張の発見、
`scripts/verify_extension.py` の4面出力、秘匿ファイルの除外。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

import scripts.verify_extension as verify_extension
import pytest

from sherpa.ingest.analyzers import registry
from sherpa.ingest.analyzers._base import Analyzer
from sherpa.ingest.analyzers.java import JavaAnalyzer


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


# ---- FW プラグイン（本体のアナライザの後に複数を重ねて適用する仕組み・ANA-15 P1） ----

from sherpa import graph_coverage  # noqa: E402
from sherpa.ingest import world_graph, world_neo4j  # noqa: E402
from sherpa.ingest.analyzers._base import (  # noqa: E402
    DefItem, DefResult, Dropped, FwPlugin, PluginAmbiguity, PluginDefs, PluginRefs, RefCandidate, RefResult)


class _Lang(Analyzer):
    """架空の言語。`CFG-A`／`CFG-B` で始まる本文は設定の種別 A／B。"""
    name = "tfw:lang"
    doctype = "ext-test"
    extensions = frozenset({".tfw"})
    version = 1

    def config_kind(self, text, rel_path):
        return {"CFG-A": "A", "CFG-B": "B"}.get(text[:5])

    def collect_defs(self, text, rel_path):
        return DefResult(primary=DefItem("Module", PurePosixPath(rel_path).name),
                         children=[DefItem("DataItem", "BASEFIELD", line=1)])

    def extract_refs(self, text, rel_path):
        return RefResult(refs=[RefCandidate("INVOKES", "Module", "target.tfw", 2, {"via": "call"})])


def _plugin(name, *, order=100, kinds=(), languages=("tfw:lang",), version=1, target="other.tfw", calls=None,
            child=None, boom=None):
    class _P(FwPlugin):
        pass
    _P.name, _P.order, _P.version = name, order, version
    _P.languages, _P.config_kinds = frozenset(languages), frozenset(kinds)

    def collect_defs(self, text, rel_path, base):
        if calls is not None:
            calls.append(name)
        if boom == "defs":
            raise RuntimeError("defs boom")
        return PluginDefs(children=[DefItem("DataItem", child, line=3)] if child else [])

    def extract_refs(self, text, rel_path, base_defs, base_refs, types):
        if boom == "refs":
            raise RuntimeError("refs boom")
        return PluginRefs(refs=[RefCandidate("INVOKES", "Module", target, 3, {"via": "call"})],
                          dropped=[Dropped("unsupported", 3, "x")])
    _P.collect_defs, _P.extract_refs = collect_defs, extract_refs
    return _P()


def _build(tmp_path, monkeypatch, plugins, texts=None):
    monkeypatch.setattr(registry, "_ANALYZERS", registry._UPSTREAM_ANALYZERS + (_Lang(),))
    monkeypatch.setattr(registry, "FW_PLUGINS", tuple(plugins))
    (tmp_path / "src").mkdir(exist_ok=True)
    files = []
    for fn in ("main.tfw", "target.tfw", "other.tfw"):
        (tmp_path / "src" / fn).write_text((texts or {}).get(fn, "CFG-A\n"), encoding="utf-8")
        files.append((tmp_path / "src" / fn, f"src/{fn}"))
    nodes, edges, flags = world_graph.build_world(tmp_path, "w", files=files)
    ekeys = {(e["src"].rsplit("#", 1)[-1], e["type"], e["dst"].rsplit("#", 1)[-1], e.get("via")) for e in edges}
    return {n["cid"] for n in nodes}, ekeys, flags


def test_plugin_off_keeps_base_result_and_plugin_only_adds(tmp_path, monkeypatch):
    # 契約テスト①: 無効（登録なし）でも本体の結果は同じで、有効にすると足すだけ（本体の出力は 1 つも消えない）
    n0, e0, f0 = _build(tmp_path, monkeypatch, [])
    n1, e1, f1 = _build(tmp_path, monkeypatch, [_plugin("p:one", child="PFIELD")])
    assert e0 and n0 <= n1 and e0 <= e1
    assert ("main.tfw", "INVOKES", "other.tfw", "call") in e1 - e0 and not any(k[2] == "other.tfw" for k in e0)
    assert len(n1 - n0) == 3                                       # 3 ファイルへ PFIELD を 1 つずつ
    assert [f for f in f0 if f["reason"] == "plugin_failed"] == []
    assert any(f["reason"] == "dropped_syntax" and f["why"] == "p:one: unsupported" for f in f1)


def test_two_plugins_apply_together_in_deterministic_order_and_duplicates_collapse(tmp_path, monkeypatch):
    # 契約テスト②: (order, 登録名) の昇順・両方の追加分が入り・同じ参照は 1 件
    calls: list = []
    a = _plugin("a:fw", order=2, calls=calls, target="other.tfw")
    b = _plugin("z:fw", order=1, calls=calls, target="other.tfw", child="ZFIELD")
    c = _plugin("m:fw", order=2, calls=calls, target="target.tfw")  # 本体と同じ (INVOKES, 名前, via) でも行が違えば別の参照
    _n, e_ab, _f = _build(tmp_path, monkeypatch, [a, b, c])
    assert calls[:3] == ["z:fw", "a:fw", "m:fw"] and [p.name for p in registry.fw_plugins()] == ["z:fw", "a:fw", "m:fw"]
    _n, e_ba, _f = _build(tmp_path, monkeypatch, [c, b, a])
    assert e_ab == e_ba                                           # 登録順に依らない
    assert ("main.tfw", "INVOKES", "other.tfw", "call") in e_ab and ("main.tfw", "INVOKES", "target.tfw", "call") in e_ab
    base_defs, base_refs = DefResult(), RefResult()
    merged, fails, ambs = registry.apply_fw_refs([a, b, c], "", "main.tfw", base_defs, base_refs)
    assert fails == [] and ambs == [] and [(r.name, r.line) for r in merged.refs] == [("other.tfw", 3), ("target.tfw", 3)]  # a と z の同じ参照は 1 件


def test_plugin_exception_keeps_base_and_reports_failure_in_flags_coverage_and_degraded(tmp_path, monkeypatch):
    # 契約テスト③: 例外で本体の結果は残り、取り込みの記録（flags）・coverage・degraded に失敗が出る
    _n, e0, _f = _build(tmp_path, monkeypatch, [])
    _n, e1, flags = _build(tmp_path, monkeypatch, [_plugin("p:bad", boom="refs"), _plugin("p:ok", child="OKF")])
    assert e0 <= e1
    failed = [f for f in flags if f["reason"] == "plugin_failed"]
    assert [(f["plugin"], f["action"], f["files"]) for f in failed] == [("p:bad", "warn", 3)]
    assert "refs boom" in failed[0]["why"]
    failures = world_graph.plugin_failures_from_flags(flags)
    assert [x["plugin"] for x in failures] == ["p:bad"]

    class _Rec:
        def __init__(self, d): self._d = d
        def data(self): return self._d

    class _Session:
        def run(self, query, **params):
            return iter([_Rec({"pf": json.dumps(failures)})])
    assert world_neo4j.read_plugin_failures(_Session(), "w") == failures
    cov = graph_coverage.Coverage()
    graph_coverage.add_plugin_failures(cov, failures, graph_coverage.STAGE_IMPACT)
    assert cov.as_dict()["limits"] == [{"kind": "plugin_failed", "stage": "impact", "plugin": "p:bad"}]
    from sherpa.graph_tools import _attach_plugin_failures
    res = {"coverage": graph_coverage.Coverage().as_dict()}
    _attach_plugin_failures(_Session(), "w", res, graph_coverage.STAGE_IMPACT)
    assert res["coverage"]["complete"] is False and res["coverage"]["limits"][0]["plugin"] == "p:bad"

    from sherpa.impact_service import plugin_failed_note
    assert "p:bad" in plugin_failed_note(res["coverage"])
    from sherpa.parts.read import fused_search
    gh = fused_search.GraphHits()
    gh.run_coverage = res["coverage"]
    monkeypatch.setattr(fused_search, "_search_graph", lambda *a, **k: (gh, None))
    monkeypatch.setattr(fused_search.documents, "world_rel_set", lambda *a, **k: set())
    out = fused_search.search("w", "q", engines=["graph"])
    assert out["degraded"] == []                                   # 結果が返るので degraded には入れない（従来どおり失敗して結果なしだけ）
    assert out["engines_used"] == ["graph"] and {"kind": "plugin_failed"} in out["coverage"]["graph"]["limits"]
    assert out["coverage"]["graph"]["complete"] is False


def test_plugin_version_bump_changes_only_that_plugin_signature(monkeypatch):
    p1, p2 = _plugin("p:one", order=1, kinds=("A",)), _plugin("p:two", order=2)
    monkeypatch.setattr(registry, "FW_PLUGINS", (p1, p2))
    monkeypatch.setattr(registry, "_ANALYZERS", registry._UPSTREAM_ANALYZERS + (_Lang(),))
    before = registry.config_signature()
    monkeypatch.setattr(p1, "version", 2)
    after = registry.config_signature()
    assert after[0] == before[0] and after[1] == before[1] and after[2][1] == before[2][1]
    assert after[2][0] != before[2][0] and after[2][0] == ("p:one", 2, ("tfw:lang",), ("A",), 1)
    monkeypatch.setattr(p1, "config_kinds", frozenset({"B"}))   # 適用条件の変更も署名に出る
    assert registry.config_signature()[2][0][3] == ("B",)


def test_plugin_applies_only_to_matching_language_and_config_kind(monkeypatch):
    # 契約テスト⑤: 肯定・否定（別の設定の種別・別のアナライザ・種別を判定できないファイルには適用しない）
    pa, pb = _plugin("p:a", kinds=("A",)), _plugin("p:b", kinds=("B",))
    px, pany = _plugin("p:x", languages=("java",)), _plugin("p:any")
    monkeypatch.setattr(registry, "FW_PLUGINS", (pa, pb, px, pany))
    lang = _Lang()
    names = lambda text: [p.name for p in registry.applicable_fw_plugins(lang, text, "a.tfw")]  # noqa: E731
    assert names("CFG-A\n") == ["p:a", "p:any"]
    assert names("CFG-B\n") == ["p:any", "p:b"]            # 同じ order は登録名の昇順
    assert names("plain\n") == ["p:any"]                          # 種別なし＝種別で絞るプラグインには適用しない
    assert [p.name for p in registry.applicable_fw_plugins(JavaAnalyzer(), "", "A.java")] == ["p:x"]


def _java_world(tmp_path, monkeypatch, impls, plugin):
    """Java の世代（インタフェース 1・実装 `impls` 個・注入側 1）に `plugin` を適用してグラフを作る。"""
    monkeypatch.setattr(registry, "FW_PLUGINS", (plugin,))
    src = tmp_path / "app"
    src.mkdir(exist_ok=True)
    texts = {"Svc.java": "package app;\npublic interface Svc {}\n",
             "Ctl.java": "package app;\npublic class Ctl {\n  Svc svc;\n}\n"}
    texts.update({f"{n}.java": f"package app;\npublic class {n} implements Svc {{}}\n" for n in impls})
    files = []
    for fn, text in texts.items():
        (src / fn).write_text(text, encoding="utf-8")
        files.append((src / fn, f"app/{fn}"))
    return world_graph.build_world(tmp_path, "w", files=files)


def test_spring_java_plugin_off_loses_only_the_framework_part_and_keeps_the_base(tmp_path, monkeypatch):
    # 実プラグイン（`spring:java`）で契約テスト①: 無効にすると注入・URL キー・設定キーだけが消え、本体の型の参照は残る
    from sherpa.ingest.analyzers.spring_java import SpringJavaPlugin
    src = tmp_path / "app"
    src.mkdir()
    (src / "Svc.java").write_text("package app;\npublic interface Svc {}\n", encoding="utf-8")
    (src / "Base.java").write_text("package app;\npublic class Base {}\n", encoding="utf-8")
    (src / "Ctl.java").write_text(
        'package app;\n@RequestMapping("/o")\npublic class Ctl extends Base {\n  @Autowired\n  Svc svc;\n'
        '  @Value("${k.v}")\n  String v;\n  @GetMapping("/x")\n  void m() {}\n}\n', encoding="utf-8")
    (src / "a.properties").write_text("k.v=1\n", encoding="utf-8")
    files = [(src / n, f"app/{n}") for n in ("Svc.java", "Base.java", "Ctl.java", "a.properties")]

    def build(plugins):
        monkeypatch.setattr(registry, "FW_PLUGINS", tuple(plugins))
        nodes, edges, flags = world_graph.build_world(tmp_path, "w", files=files)
        return ({(n["label"], n["name"]) for n in nodes},
                {(e["src"].rsplit("#", 1)[-1], e["type"], e["dst"].rsplit("#", 1)[-1], e.get("via")) for e in edges}, flags)

    n0, e0, f0 = build([])
    n1, e1, f1 = build([SpringJavaPlugin()])
    assert ("Ctl", "INVOKES", "Base", "extends") in e0 and ("Ctl", "INVOKES", "Svc", "field_type") in e0
    assert not any(k[3] in ("inject", "config_key") for k in e0) and ("Config", "/o/x") not in n0
    assert ("Ctl", "INVOKES", "Svc", "inject") in e1 and ("Ctl", "INVOKES", "Base", "extends") in e1
    assert ("Config", "/o/x") in n1 and any(k[3] == "config_key" for k in e1)
    assert n0 <= n1 and not [f for f in f1 if f["reason"] == "plugin_failed"]


def _di_plugin(seen):
    """注入側（`Ctl.java`）の `Svc` の実装を型の関係から引く。1 つに決まれば辺の候補・決まらなければ申告（任意に選ばない）。"""
    class _Di(FwPlugin):
        name, languages, version, order, uses_type_relations = "p:di", frozenset({"java"}), 1, 10, True

        def extract_refs(self, text, rel_path, base_defs, base_refs, types):
            if not rel_path.endswith("Ctl.java"):
                return PluginRefs()
            lookup = types.subtypes("Svc", rel_path, base_refs.file_context)
            seen.append(lookup)
            if len(lookup.subtypes) == 1:
                return PluginRefs(refs=[RefCandidate("INVOKES", "Module", lookup.subtypes[0].qualified, 3,
                                                     {"via": "inject", "qualified": True})])
            return PluginRefs(ambiguous=[PluginAmbiguity("Module", "Svc", 3, list(lookup.subtypes), via="inject")])
    return _Di()


def test_type_relations_return_every_candidate_and_one_implementer_links_by_qualified_name(tmp_path, monkeypatch):
    seen: list = []
    nodes, edges, flags = _java_world(tmp_path, monkeypatch, ["ImplA"], _di_plugin(seen))
    assert [(c.path, c.qualified, c.exact) for c in seen[0].subtypes] == [("app/ImplA.java", "app.ImplA", True)]
    assert seen[0].status == "resolved" and [t.qualified for t in seen[0].targets] == ["app.Svc"]
    assert any(e["type"] == "INVOKES" and e.get("via") == "inject" and e["src"].endswith("Ctl.java#Ctl")
               and e["dst"].endswith("ImplA.java#ImplA") for e in edges)
    assert not any(f["reason"] == "plugin_failed" for f in flags)


def test_ambiguous_implementers_are_reported_with_candidates_and_no_edge_is_chosen(tmp_path, monkeypatch):
    seen: list = []
    nodes, edges, flags = _java_world(tmp_path, monkeypatch, ["ImplA", "ImplB"], _di_plugin(seen))
    assert [c.qualified for c in seen[0].subtypes] == ["app.ImplA", "app.ImplB"]      # 複数のまま返る
    assert not any(e.get("via") == "inject" for e in edges)                              # 任意に選ばない
    amb = [f for f in flags if f["reason"] == "ambiguous" and f.get("via") == "inject"]
    assert [(f["from"], f["name"], f["candidates"]) for f in amb] == [("app/Ctl.java", "Svc", 2)]
    ctl = next(n for n in nodes if n["path"] == "app/Ctl.java" and n["label"] == "Module")
    item = next(u for u in ctl["unresolved"] if u["via"] == "inject")                    # S1b の未解決の申告（保存済みの形）
    assert (item["reason"], item["candidates"], item["candidate_paths"]) == (
        "ambiguous", 2, ["app/ImplA.java", "app/ImplB.java"])


def test_type_relations_are_unavailable_unless_the_plugin_declares_them(tmp_path, monkeypatch):
    class _NoDecl(FwPlugin):
        name, languages, version, order = "p:nodecl", frozenset({"java"}), 1, 1

        def extract_refs(self, text, rel_path, base_defs, base_refs, types):
            types.subtypes("Svc", rel_path)
            return PluginRefs()
    _n, _e, flags = _java_world(tmp_path, monkeypatch, ["ImplA"], _NoDecl())
    failed = [f for f in flags if f["reason"] == "plugin_failed"]
    assert [f["plugin"] for f in failed] == ["p:nodecl"] and "uses_type_relations" in failed[0]["why"]


_PLUGIN_SRC = (
    "from sherpa.ingest.analyzers._base import FwPlugin\n\n"
    "class _P(FwPlugin):\n{body}\n\nFW_PLUGINS = [_P()]\n"
)


def _plugin_src(name="'myfw:spring'", languages="frozenset({'java'})", version="1", order="10",
                config_kinds="frozenset()") -> str:
    body = "".join(f"    {k} = {v}\n" for k, v in (("name", name), ("languages", languages), ("version", version),
                                                  ("order", order), ("config_kinds", config_kinds)) if v is not _OMIT)
    return _PLUGIN_SRC.format(body=body.rstrip("\n"))


def test_discover_fw_plugins_registers_in_apply_order(tmp_path):
    _write(tmp_path, {"zz_a.py": _plugin_src(name="'zz:a'", order="5"),
                      "aa_b.py": _plugin_src(name="'aa:b'", order="5"),
                      "mm_c.py": _plugin_src(name="'mm:c'", order="1", config_kinds="frozenset({'x'})"),
                      "mm_none.py": "VALUE = 1\n"})
    assert [p.name for p in registry.discover_fw_plugins(tmp_path)] == ["mm:c", "aa:b", "zz:a"]


@pytest.mark.parametrize("files,needle", [
    ({"myfw_x.py": _plugin_src(languages="frozenset({'nosuchlang'})")}, "未登録のアナライザ名"),
    ({"myfw_x.py": _plugin_src(name="'java:spring'")}, "予約名"),
    ({"other_x.py": _plugin_src()}, "ファイル名と一致"),
    ({"myfw_x.py": _plugin_src(name="'myfw_nocolon'")}, "<prefix>:<fw>"),
    ({"myfw_x.py": _plugin_src(version="0")}, "version"),
    ({"myfw_x.py": _plugin_src(version=_OMIT)}, "拡張自身のクラス"),
    ({"myfw_x.py": _plugin_src(order="'1'")}, "order"),
    ({"myfw_x.py": _plugin_src(languages="frozenset()")}, "languages"),
    ({"myfw_a.py": _plugin_src(), "myfw_b.py": _plugin_src()}, "重複"),
    ({"myfw_x.py": "FW_PLUGINS = [object()]\n"}, "FwPlugin のインスタンス"),
    ({"myfw_x.py": "FW_PLUGINS = [\n"}, "モジュールを読み込めません"),   # import・構文の失敗は黙って読み飛ばさず FwPluginError
])
def test_discover_fw_plugins_contract_violations_fail_loudly(tmp_path, files, needle):
    _write(tmp_path, files)
    with pytest.raises(registry.FwPluginError) as ei:
        registry.discover_fw_plugins(tmp_path)
    assert needle in str(ei.value)


def test_verify_extension_has_fw_plugin_surface_and_fails_on_collisions(monkeypatch, capsys):
    assert verify_extension.main([]) == 0
    assert "## FW プラグイン" in capsys.readouterr().out
    monkeypatch.setattr(registry, "FW_PLUGINS", (_plugin("p:dup", languages=("java",)), _plugin("p:dup", languages=("nosuch",))))
    assert verify_extension.main([]) != 0
    out = capsys.readouterr().out
    assert "重複" in out and "未登録のアナライザ名" in out


def test_verify_extension_reports_ng_for_a_broken_plugin_module_and_for_bad_registered_attributes(tmp_path, monkeypatch, capsys):
    repo_root = Path(__file__).resolve().parents[2]
    work = tmp_path / "repo"
    shutil.copytree(repo_root / "sherpa", work / "sherpa", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(repo_root / "scripts", work / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
    (work / "sherpa" / "ingest" / "analyzers" / "myfw_broken.py").write_text("FW_PLUGINS = [\n", encoding="utf-8")
    r = subprocess.run([sys.executable, str(work / "scripts" / "verify_extension.py")], cwd=work,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "NG:" in r.stdout and "Traceback" not in r.stdout
    bad = _plugin("p:bad")
    bad.order = "1"
    monkeypatch.setattr(registry, "FW_PLUGINS", (bad,))
    assert verify_extension.main([]) != 0
    assert "order" in capsys.readouterr().out


def test_type_relations_resolve_a_qualified_name_without_file_context_by_the_qualified_index(tmp_path, monkeypatch):
    seen: list = []
    _java_world(tmp_path, monkeypatch, ["ImplA"], _di_plugin(seen))
    from sherpa.ingest.analyzers._base import TypeRelations  # noqa: F401
    qd = {("Module", "app.Svc"): [("app/Svc.java", "Svc")], ("Module", "other.Svc"): [("app/O.java", "Svc")]}
    defs = {("Module", "Svc"): ["app/Svc.java", "app/O.java"]}
    cands, status = world_graph._type_candidates(qd, defs, "Module", "app.Svc", "app/Ctl.java", None, False)
    assert (cands, status) == ([("app/Svc.java", "Svc", "app.Svc")], "")
    assert world_graph._type_candidates(qd, defs, "Module", "no.Svc", "app/Ctl.java", None, False)[1] == "unresolved"


def test_invalid_item_after_valid_one_discards_the_whole_plugin_output_and_keeps_base(tmp_path, monkeypatch):
    class _Bad(FwPlugin):
        name, languages, version, order = "p:badfields", frozenset({"tfw:lang"}), 1, 1

        def collect_defs(self, text, rel_path, base):
            return PluginDefs(children=[DefItem("DataItem", "GOOD", line=5), DefItem(label=[], name="BAD")])

        def extract_refs(self, text, rel_path, base_defs, base_refs, types):
            return PluginRefs(refs=[RefCandidate("INVOKES", "Module", "other.tfw", 4, {"via": "call"}),
                                    RefCandidate("INVOKES", "Module", "x", 6, source_symbol_id=[])])
    n0, e0, _f = _build(tmp_path, monkeypatch, [])
    n1, e1, flags = _build(tmp_path, monkeypatch, [_Bad()])
    assert (n1, e1) == (n0, e0)                                    # 本体の結果は同じ・プラグインの出力は 0 件
    assert [f["plugin"] for f in flags if f["reason"] == "plugin_failed"] == ["p:badfields"]


def test_type_relations_resolve_a_dotted_name_with_file_context_by_the_qualified_entry():
    from sherpa.ingest.analyzers._base import FileContext
    qd = {("Module", "lib.Base"): [("app/B.java", "Base")], ("Module", "app.lib.Base"): [("app/X.java", "Base")]}
    ctx = FileContext(package="app")
    rel = "app/Ctl.java"
    # 修飾名は単純名の規則（同じ package の lib.Base→app.lib.Base）ではなく、完全修飾名の完全一致で引く
    cands, status = world_graph._type_candidates(qd, {}, "Module", "lib.Base", rel, ctx, False, qualified=True)
    assert (cands, status) == ([("app/B.java", "Base", "lib.Base")], "")


def test_type_relations_stage_blocks_when_a_file_cannot_be_read_again(tmp_path, monkeypatch):
    import collections
    from sherpa.ingest import world_graph as wg
    real, calls = wg.corpus_docs.read_full_text_and_raw, collections.Counter()

    def flaky(path):
        calls[str(path)] += 1
        if calls[str(path)] == 2 and str(path).endswith("ImplA.java"):   # 2 回目＝型の関係を集める段
            raise OSError("gone")
        return real(path)
    monkeypatch.setattr(wg.corpus_docs, "read_full_text_and_raw", flaky)
    _n, _e, flags = _java_world(tmp_path, monkeypatch, ["ImplA"], _di_plugin([]))
    assert {"doc": "app/ImplA.java", "reason": "unreadable_code_file", "action": "blocked"} in flags


def test_plugin_and_type_relations_see_global_using_imports(tmp_path, monkeypatch):
    seen: list = []

    class _P(FwPlugin):
        name, languages, version, order, uses_type_relations = "p:cs", frozenset({"csharp"}), 1, 1, True

        def extract_refs(self, text, rel_path, base_defs, base_refs, types):
            if rel_path.endswith("Ctl.cs"):
                seen.append((sorted(i.name for i in base_refs.file_context.imports),
                             types.subtypes("Base", rel_path, base_refs.file_context, 3)))
            return PluginRefs()
    monkeypatch.setattr(registry, "FW_PLUGINS", (_P(),))
    texts = {"Globals.cs": "global using Lib;\n",
             "Base.cs": "namespace Lib { public class Base { } }\n",
             "Impl.cs": "namespace App { public class Impl : Base { } }\n",
             "Ctl.cs": "namespace App {\n public class Ctl {\n }\n}\n"}
    files = []
    for fn, text in texts.items():
        (tmp_path / fn).write_text(text, encoding="utf-8")
        files.append((tmp_path / fn, fn))
    _n, _e, flags = world_graph.build_world(tmp_path, "w", files=files)
    assert not any(f["reason"] == "plugin_failed" for f in flags)
    imports, lookup = seen[0]
    assert "Lib" in imports and [c.path for c in lookup.subtypes] == ["Impl.cs"]


def test_type_relations_keep_each_qualified_name_for_same_named_types_in_one_file(tmp_path, monkeypatch):
    seen: list = []

    class _P(FwPlugin):
        name, languages, version, order, uses_type_relations = "p:cs2", frozenset({"csharp"}), 1, 1, True

        def extract_refs(self, text, rel_path, base_defs, base_refs, types):
            if rel_path == "Ctl.cs":
                seen.append(types.subtypes("Lib.Base", rel_path, base_refs.file_context))
            return PluginRefs()
    monkeypatch.setattr(registry, "FW_PLUGINS", (_P(),))
    texts = {"Base.cs": "namespace Lib { public class Base { } }\n",
             "Both.cs": "namespace A { public class Impl : Lib.Base { } }\nnamespace B { public class Impl : Lib.Base { } }\n",
             "Ctl.cs": "namespace App { public class Ctl { } }\n"}
    files = []
    for fn, text in texts.items():
        (tmp_path / fn).write_text(text, encoding="utf-8")
        files.append((tmp_path / fn, fn))
    world_graph.build_world(tmp_path, "w", files=files)
    assert sorted(c.qualified for c in seen[0].subtypes) == ["A.Impl", "B.Impl"]
