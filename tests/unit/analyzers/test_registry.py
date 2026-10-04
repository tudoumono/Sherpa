"""アナライザ登録簿の単体テスト（拡張子解決の優先順・`accepts`・語彙外破棄＋flags・未担当＝資料）。"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from sherpa.ingest.analyzers import registry
from sherpa.ingest.analyzers._base import Analyzer, DefItem, DefResult, RefCandidate, RefResult
from sherpa.ingest.analyzers.cobol import CobolAnalyzer
from sherpa.ingest.analyzers.copybook import CopybookAnalyzer
from sherpa.ingest.analyzers.jcl import JclAnalyzer


class _PlainAnalyzer(Analyzer):
    """`accepts` を上書きしないフェイク（既定＝常に真）。"""

    def __init__(self, name, ext=".zz"):
        self.name = name
        self.extensions = frozenset({ext})

    def collect_defs(self, text, rel_path):
        return DefResult()

    def extract_refs(self, text, rel_path):
        return RefResult()


class _AlwaysAnalyzer(_PlainAnalyzer):
    """同一拡張子を複数登録して優先順/accepts を検証するフェイク。"""

    def __init__(self, name, accept=True):
        super().__init__(name)
        self._accept = accept

    def accepts(self, rel_path, head_text=""):
        return self._accept


def _build(tmp_files):
    """`{相対パス: 内容}` を作業ディレクトリ配下の「案件A」に置いて build_world を実行する。"""
    from sherpa.ingest import world_graph
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "案件A"
        base.mkdir()
        for name, body in tmp_files.items():
            (base / name).write_text(body, encoding="utf-8")
        return world_graph.build_world(Path(d), "w")


def _unregistered_ext() -> str:
    """どのアナライザ（フォークの拡張を含む）にも登録されていない拡張子を選ぶ。"""
    for ext in (".md", ".txt", ".zz-unregistered"):
        if ext not in registry.registered_extensions():
            return ext
    raise AssertionError("未登録の拡張子が見つからない")


# ---- 一覧・拡張子 ----

def test_known_analyzers_are_upstream_priority_order_then_extensions_by_name():
    """優先順＝`_UPSTREAM_ANALYZERS` の並び（固定）＋拡張アナライザ（`<prefix>_*.py`・名前順で末尾）。
    拡張の一覧は固定しない（フォークが正規の拡張を足しても緑のまま）。"""
    names = [a.name for a in registry.known_analyzers()]
    upstream = [a.name for a in registry._UPSTREAM_ANALYZERS]
    assert upstream == ["cobol", "copybook", "jcl", "java", "properties", "yaml_config", "xml_config", "sql",
                        "c", "csharp", "jsp", "html", "js", "css", "shell", "vb"]
    assert names[:len(upstream)] == upstream
    ext_names = names[len(upstream):]
    assert ext_names == sorted(ext_names) and all(":" in n for n in ext_names)
    assert "sample_ext:dummy" in ext_names


def test_registered_extensions_is_union_of_known_analyzers():
    expected = frozenset().union(*(a.extensions for a in registry.known_analyzers()))
    assert registry.registered_extensions() == expected
    upstream_ext = frozenset().union(*(a.extensions for a in registry._UPSTREAM_ANALYZERS))
    assert upstream_ext == {".cbl", ".cob", ".cobol", ".cpy", ".copybook", ".jcl", ".java",
                            ".properties", ".yaml", ".yml", ".xml", ".sql",
                            ".c", ".h", ".cs",
                            ".jsp", ".jspx", ".jspf", ".tag", ".tagx",
                            ".html", ".htm", ".xhtml", ".js", ".mjs", ".css",
                            ".sh", ".bash", ".ksh", ".zsh", ".bat", ".cmd",
                            ".vb", ".bas", ".cls", ".frm", ".ctl", ".vbs"}
    assert upstream_ext <= registry.registered_extensions()
    assert ".sampleext" in registry.registered_extensions()


# ---- config_signature（world 署名・ES 設定署名の材料） ----

def test_config_signature_is_stable_and_follows_schema_version(monkeypatch):
    assert registry.config_signature() == registry.config_signature()
    monkeypatch.setattr(registry, "CODE_ANALYZERS_SCHEMA_VERSION", 1)
    sig_v1 = registry.config_signature()
    monkeypatch.setattr(registry, "CODE_ANALYZERS_SCHEMA_VERSION", 2)
    assert registry.config_signature() != sig_v1               # 分類契約版を上げると署名が変わる
    monkeypatch.setattr(registry, "CODE_ANALYZERS_SCHEMA_VERSION", 1)
    assert registry.config_signature() == sig_v1


def test_config_signature_tracks_extensions_and_order(monkeypatch):
    plain = _PlainAnalyzer("lang", ext=".zz")
    monkeypatch.setattr(registry, "_ANALYZERS", (plain,))
    sig1 = registry.config_signature()
    wider = _PlainAnalyzer("lang", ext=".zz")
    wider.extensions = frozenset({".zz", ".zz2"})
    monkeypatch.setattr(registry, "_ANALYZERS", (wider,))
    assert registry.config_signature() != sig1                 # 担当拡張子集合が変わる

    a, b = _AlwaysAnalyzer("first"), _AlwaysAnalyzer("second")
    monkeypatch.setattr(registry, "_ANALYZERS", (a, b))
    sig_ab = registry.config_signature()
    monkeypatch.setattr(registry, "_ANALYZERS", (a, b))        # 同じ構成の再設定は不変
    assert registry.config_signature() == sig_ab
    monkeypatch.setattr(registry, "_ANALYZERS", (b, a))        # 登録順（優先順）が変わる
    assert registry.config_signature() != sig_ab


@pytest.mark.usefixtures("upstream_only_registry")
def test_config_signature_changes_when_java_analyzer_is_registered(monkeypatch):
    """アナライザの登録そのものが署名を変える（専用の移行機構なしで reindex 経路に乗る）。"""
    from sherpa.ingest.analyzers.java import JavaAnalyzer
    with_java = registry.config_signature()
    monkeypatch.setattr(registry, "_ANALYZERS", tuple(a for a in registry._ANALYZERS if a.name != "java"))
    assert registry.config_signature() != with_java
    assert isinstance(registry.known_analyzers()[-1], JavaAnalyzer) is False


# ---- 解決 ----

def test_resolve_picks_analyzer_by_extension():
    from sherpa.ingest.analyzers.java import JavaAnalyzer
    assert isinstance(registry.resolve("x.cbl"), CobolAnalyzer)
    assert isinstance(registry.resolve("x.cpy"), CopybookAnalyzer)
    assert isinstance(registry.resolve("x.jcl"), JclAnalyzer)
    assert isinstance(registry.resolve("x.java"), JavaAnalyzer)


def test_resolve_returns_none_for_unregistered_extension():
    ext = _unregistered_ext()
    assert registry.resolve(f"x{ext}") is None
    assert registry.resolve("noext") is None
    assert registry.candidates(f"x{ext}") == ()


def test_ext_matches_path_suffix_semantics_for_dotfiles():
    """拡張子抽出は `Path.suffix` と同じ規約——ドットのみのファイル名（例 `.cbl`）は拡張子なし。"""
    assert registry._ext(".cbl") == ""
    assert registry._ext("案件A/.cbl") == ""
    assert registry._ext("foo.cbl") == ".cbl"
    assert registry._ext(".foo.cbl") == ".cbl"
    assert registry.resolve(".cbl") is None


@pytest.mark.usefixtures("upstream_only_registry")
def test_resolve_lazy_skips_read_head_when_all_candidates_use_default_accepts():
    """既定の `accepts`（常に真）しか候補が無ければ `read_head` を呼ばない。上流限定固定——
    フォークが `.cbl` に `accepts` 上書きの拡張を登録すると前提が崩れるため。"""
    calls = []

    def read_head():
        calls.append(1)
        return "dummy"

    assert isinstance(registry.resolve_lazy("x.cbl", read_head), CobolAnalyzer)
    assert calls == []


def test_resolve_lazy_reads_head_only_when_a_candidate_overrides_accepts(monkeypatch):
    calls = []

    def read_head(size=4096):
        calls.append(1)
        return "MAGIC"

    class _OverridingAnalyzer(_PlainAnalyzer):
        def accepts(self, rel_path, head_text=""):
            return head_text == "MAGIC"

    plain, overriding = _PlainAnalyzer("plain"), _OverridingAnalyzer("magic")
    monkeypatch.setattr(registry, "_ANALYZERS", (overriding, plain))
    assert registry.resolve_lazy("x.zz", read_head) is overriding
    assert calls == [1]


def test_resolve_lazy_returns_none_when_no_candidates_without_reading():
    calls = []
    assert registry.resolve_lazy(f"x{_unregistered_ext()}", lambda: calls.append(1) or "x") is None
    assert calls == []


def test_resolve_lazy_passes_each_candidate_its_own_head_bytes(monkeypatch):
    """候補ごとに自分の head_bytes 分だけを渡す（大きい head の読み取りを小さい head の候補に使い回さない）。"""
    class Big(_PlainAnalyzer):
        head_bytes = 64 * 1024

        def accepts(self, rel_path, head_text=""):
            return "MARK" in head_text

    class Small(Big):
        head_bytes = 4096

    monkeypatch.setattr(registry, "_ANALYZERS", (Big("t:big", ".tt"), Small("t:small", ".tt")))
    sizes = []

    def read_head(*, size):
        sizes.append(size)
        return ("x" * 5000 + "MARK") if size >= 5004 else "x" * min(size, 5000)
    assert isinstance(registry.resolve_lazy("a.tt", read_head), Big)
    assert sizes == [64 * 1024]
    sizes.clear()                                       # Big が拒否なら Small は自分の 4096 で判定し、それも拒否＝None
    assert registry.resolve_lazy("a.tt", lambda *, size: sizes.append(size) or "x" * 100) is None
    assert sizes == [64 * 1024, 4096]


def test_priority_order_first_registered_wins_on_extension_conflict(monkeypatch):
    a1, a2 = _AlwaysAnalyzer("first"), _AlwaysAnalyzer("second")
    monkeypatch.setattr(registry, "_ANALYZERS", (a1, a2))
    assert registry.resolve("x.zz") is a1
    monkeypatch.setattr(registry, "_ANALYZERS", (a2, a1))
    assert registry.resolve("x.zz") is a2


def test_accepts_gate_skips_non_accepting_candidate(monkeypatch):
    declines, accepts_ = _AlwaysAnalyzer("declines", accept=False), _AlwaysAnalyzer("accepts", accept=True)
    monkeypatch.setattr(registry, "_ANALYZERS", (declines, accepts_))
    assert registry.resolve("x.zz", head_text="anything") is accepts_
    monkeypatch.setattr(registry, "_ANALYZERS", (declines, declines))
    assert registry.resolve("x.zz") is None                          # 全滅なら資料扱い


# ---- build_world: 未担当・語彙外・予約キー・fail-closed ----

@pytest.mark.usefixtures("upstream_only_registry")
def test_unregistered_extension_file_is_silently_skipped_by_build_world():
    """未担当拡張子＝資料として扱う（グラフに乗らない・例外なし）。上流限定固定——フォークが `.txt` を
    担当する拡張を登録すると前提が崩れるため。"""
    nodes, edges, flags = _build({"note.txt": "PROGRAM-ID. NOT-CODE.\n"})
    assert nodes == [] and edges == [] and flags == []


class _BadLabelAnalyzer(Analyzer):
    name = "badlabel"
    extensions = frozenset({".zz"})

    def collect_defs(self, text, rel_path):
        return DefResult(primary=DefItem(label="Frobnicator", name="X"))

    def extract_refs(self, text, rel_path):
        return RefResult()


class _BadRefAnalyzer(Analyzer):
    name = "badref"
    extensions = frozenset({".zz"})

    def collect_defs(self, text, rel_path):
        return DefResult(primary=DefItem(label="Module", name="FOO"))

    def extract_refs(self, text, rel_path):
        return RefResult(refs=[RefCandidate("BOGUS_EDGE", "Module", "BAR", 1),
                               RefCandidate("INVOKES", "Frobnicator", "BAR", 2)])


def test_unknown_node_label_is_discarded_with_flag(monkeypatch):
    monkeypatch.setattr(registry, "_ANALYZERS", (_BadLabelAnalyzer(),))
    nodes, edges, flags = _build({"a.zz": "dummy\n"})
    assert nodes == [] and edges == []
    assert flags == [{"reason": "unknown_label", "analyzer": "badlabel", "label": "Frobnicator", "from": "案件A/a.zz"}]


def test_unknown_edge_type_and_ref_label_are_discarded_with_flags(monkeypatch):
    monkeypatch.setattr(registry, "_ANALYZERS", (_BadRefAnalyzer(),))
    nodes, edges, flags = _build({"a.zz": "dummy\n"})
    assert len(nodes) == 1 and nodes[0]["label"] == "Module" and nodes[0]["name"] == "FOO"
    assert edges == []
    assert {(fl["reason"], fl.get("edge_type") or fl.get("label")) for fl in flags} == {
        ("unknown_edge_type", "BOGUS_EDGE"), ("unknown_label", "Frobnicator")}


class _HijackExtraAnalyzer(Analyzer):
    """`extra` で共通層の予約キー（label/cid/analyzer 等）を上書きしようとする不正アナライザ。"""

    name = "hijack"
    extensions = frozenset({".zz"})

    def collect_defs(self, text, rel_path):
        primary = DefItem(label="Module", name="FOO",
                          extra={"label": "Frobnicator", "analyzer": "spoofed", "cid": "totally-fake"})
        child = DefItem(label="DataItem", name="BAR", cid_key="FOO.BAR",
                        extra={"qualified": "FOO.BAR", "name": "SPOOFED-NAME", "world_id": "other-world"})
        return DefResult(primary=primary, children=[child])

    def extract_refs(self, text, rel_path):
        return RefResult()


def test_reserved_keys_in_extra_are_stripped_and_flagged(monkeypatch):
    """予約キーが1つでも衝突したら `extra` を丸ごと捨て（衝突していない他のキーも道連れ）flags に記録する。"""
    monkeypatch.setattr(registry, "_ANALYZERS", (_HijackExtraAnalyzer(),))
    nodes, edges, flags = _build({"a.zz": "dummy\n"})
    primary = {n["name"]: n for n in nodes}["FOO"]
    assert primary["label"] == "Module"
    assert primary["analyzer"] == "hijack"
    assert primary["cid"] == "module:w:案件A/a.zz#FOO"

    child = next(n for n in nodes if n["label"] == "DataItem")
    assert child["name"] == "BAR" and child["world_id"] == "w"
    assert "qualified" not in child                       # 衝突していないキーも extra ごと消える

    reasons = [(fl["reason"], fl["name"], tuple(sorted(fl["keys"]))) for fl in flags]
    assert ("reserved_key_in_extra", "FOO", ("analyzer", "cid", "label")) in reasons
    assert ("reserved_key_in_extra", "BAR", ("name", "world_id")) in reasons


def test_jcl_proc_file_without_job_is_batch_with_unresolved_refs():
    """JOB を持たない JCL PROC ファイルは primary=`Batch`（PROC 名）。`EXEC PROC=`/`INCLUDE MEMBER=` の
    参照先が world 内に無ければ `dropped_syntax` ではなく通常の `unresolved` flag に倒れる。"""
    nodes, edges, flags = _build({"INNERPRC.jcl": "//INNERPRC PROC\n//STEP1    EXEC PROC=INNER\n// INCLUDE MEMBER=SHARED\n"})
    assert [n["name"] for n in nodes] == ["INNERPRC"]
    assert nodes[0]["label"] == "Batch"
    assert edges == []
    reasons = {(fl["reason"], fl.get("kind"), fl.get("name")) for fl in flags}
    assert ("unresolved", "Batch", "INNER") in reasons
    assert ("unresolved", "Batch", "SHARED") in reasons
    assert not any(fl["reason"] == "dropped_syntax" for fl in flags)


def test_unreadable_registered_code_file_produces_blocked_flag(monkeypatch):
    """受理済みコード文書の実読込失敗は blocked flag を出す（worker は run 全体を失敗させる＝fail-closed）。"""
    real_read_bytes = Path.read_bytes

    def _boom(self, *a, **kw):
        if self.name == "BADPROG.cbl":
            raise OSError("simulated read failure")
        return real_read_bytes(self, *a, **kw)

    monkeypatch.setattr(Path, "read_bytes", _boom)
    nodes, edges, flags = _build({"BADPROG.cbl": "       PROGRAM-ID. BADPROG.\n"})
    assert nodes == [] and edges == []
    assert [f for f in flags if f.get("action") == "blocked"] == [
        {"doc": "案件A/BADPROG.cbl", "reason": "unreadable_code_file", "action": "blocked"}]
