"""アナライザ登録簿の単体テスト（拡張子解決の優先順・`accepts`・語彙外破棄＋flags・未担当＝資料・§7 裁定2/5/10）。"""
from __future__ import annotations

import tempfile
from pathlib import Path

from sherpa.ingest.analyzers import registry
from sherpa.ingest.analyzers._base import Analyzer, DefItem, DefResult, RefCandidate, RefResult
from sherpa.ingest.analyzers.cobol import CobolAnalyzer
from sherpa.ingest.analyzers.copybook import CopybookAnalyzer
from sherpa.ingest.analyzers.jcl import JclAnalyzer


def test_known_analyzers_are_cobol_copybook_jcl_java_in_priority_order():
    names = [a.name for a in registry.known_analyzers()]
    assert names == ["cobol", "copybook", "jcl", "java"]


def test_registered_extensions_is_union_of_known_analyzers():
    expected = frozenset().union(*(a.extensions for a in registry.known_analyzers()))
    assert registry.registered_extensions() == expected
    assert registry.registered_extensions() == {".cbl", ".cob", ".cobol", ".cpy", ".copybook", ".jcl", ".java"}


# ---- config_signature（world 署名・ES 設定署名の材料） ----

def test_config_signature_stable_for_same_configuration():
    """構成が同一なら（別呼び出しでも）署名は再現する（キャッシュしない都度計算・呼び出し間で不変）。"""
    assert registry.config_signature() == registry.config_signature()


def test_config_signature_changes_when_schema_version_bumped(monkeypatch):
    """`CODE_ANALYZERS_SCHEMA_VERSION`（`accepts()`/`classify_document` の分類契約版）を上げると、
    アナライザの並び・拡張子集合が不変でも署名が変わる（`importance.IMPORTANCE_SCHEMA_VERSION` と
    同じ流儀——分類ロジックの意味変更を検知する専用の移行機構を持たない）。"""
    monkeypatch.setattr(registry, "CODE_ANALYZERS_SCHEMA_VERSION", 1)
    sig_v1 = registry.config_signature()
    monkeypatch.setattr(registry, "CODE_ANALYZERS_SCHEMA_VERSION", 2)
    sig_v2 = registry.config_signature()
    assert sig_v2 != sig_v1
    monkeypatch.setattr(registry, "CODE_ANALYZERS_SCHEMA_VERSION", 1)
    assert registry.config_signature() == sig_v1, "同じ版・同じ構成なら署名は再現する"


def test_config_signature_changes_when_extensions_change(monkeypatch):
    """アナライザの担当拡張子集合が変わると署名が変わる（新規拡張子の追加＝SQL アナライザ想定）。"""
    plain = _PlainAnalyzer("lang", ext=".zz")
    monkeypatch.setattr(registry, "_ANALYZERS", (plain,))
    sig1 = registry.config_signature()

    class _Wider(_PlainAnalyzer):
        pass
    wider = _Wider("lang", ext=".zz")
    wider.extensions = frozenset({".zz", ".zz2"})
    monkeypatch.setattr(registry, "_ANALYZERS", (wider,))
    sig2 = registry.config_signature()
    assert sig2 != sig1


def test_config_signature_changes_when_order_changes(monkeypatch):
    """同一集合でも登録順（優先順・§7 裁定2）が変われば署名が変わる（CODE-1b の並び替え想定）。"""
    a, b = _AlwaysAnalyzer("first"), _AlwaysAnalyzer("second")
    monkeypatch.setattr(registry, "_ANALYZERS", (a, b))
    sig_ab = registry.config_signature()
    monkeypatch.setattr(registry, "_ANALYZERS", (b, a))
    sig_ba = registry.config_signature()
    assert sig_ab != sig_ba


def test_config_signature_unchanged_when_configuration_unchanged(monkeypatch):
    """構成（登録順・拡張子集合）が変わらなければ、フェイクへ差し替えても既定と別の値でも一致する
    （変化していないときに署名が変わらないことの確認）。"""
    a, b = _AlwaysAnalyzer("first"), _AlwaysAnalyzer("second")
    monkeypatch.setattr(registry, "_ANALYZERS", (a, b))
    sig1 = registry.config_signature()
    monkeypatch.setattr(registry, "_ANALYZERS", (a, b))   # 同じ構成を再設定
    sig2 = registry.config_signature()
    assert sig1 == sig2


def test_config_signature_changes_when_java_analyzer_is_registered(monkeypatch):
    """CODE-1d（新言語追加の実地検証）: `JavaAnalyzer` の登録そのものが `config_signature()` を
    変える——専用の移行機構を持たず、通常の署名不一致→reindex 経路（`es_index.needs_reindex`）に
    自動的に乗ることの確認（新規アナライザ追加は §2.4 の単一の真実源へ足すだけでよい）。"""
    from sherpa.ingest.analyzers.java import JavaAnalyzer
    with_java = registry.config_signature()
    without_java = tuple(a for a in registry._ANALYZERS if a.name != "java")
    monkeypatch.setattr(registry, "_ANALYZERS", without_java)
    assert registry.config_signature() != with_java
    assert isinstance(registry.known_analyzers()[-1], JavaAnalyzer) is False   # 差し替え後は不在


def test_resolve_picks_analyzer_by_extension():
    from sherpa.ingest.analyzers.java import JavaAnalyzer
    assert isinstance(registry.resolve("x.cbl"), CobolAnalyzer)
    assert isinstance(registry.resolve("x.cpy"), CopybookAnalyzer)
    assert isinstance(registry.resolve("x.jcl"), JclAnalyzer)
    assert isinstance(registry.resolve("x.java"), JavaAnalyzer)


def test_resolve_returns_none_for_unregistered_extension():
    assert registry.resolve("x.md") is None
    assert registry.resolve("x.txt") is None
    assert registry.resolve("noext") is None            # 拡張子なし
    assert registry.candidates("x.md") == ()


def test_ext_matches_path_suffix_semantics_for_dotfiles():
    """拡張子抽出は `Path.suffix` と同じ規約——ドットのみのファイル名（例 `.cbl`）は拡張子なし扱い。"""
    assert registry._ext(".cbl") == ""                  # dotfile 自体は拡張子ではない
    assert registry._ext("案件A/.cbl") == ""
    assert registry._ext("foo.cbl") == ".cbl"
    assert registry._ext(".foo.cbl") == ".cbl"          # 隠しファイルだが実拡張子はある
    assert registry.resolve(".cbl") is None             # dotfile はどのアナライザにも解決されない


def test_resolve_lazy_skips_read_head_when_all_candidates_use_default_accepts():
    """既定の `accepts`（常に真）しか候補が無ければ `read_head` を一度も呼ばない（内容を読まない・§7 裁定10）。"""
    calls = []

    def read_head():
        calls.append(1)
        return "dummy"

    assert isinstance(registry.resolve_lazy("x.cbl", read_head), CobolAnalyzer)
    assert calls == []                                  # COBOL/copybook/JCL は accepts 未上書き＝読まない


class _PlainAnalyzer(Analyzer):
    """`accepts` を一切上書きしないフェイク（既定＝常に真のまま拡張子だけ設定）。"""

    def __init__(self, name, ext=".zz"):
        self.name = name
        self.extensions = frozenset({ext})

    def collect_defs(self, text, rel_path):
        return DefResult()

    def extract_refs(self, text, rel_path):
        return RefResult()


def test_resolve_lazy_reads_head_only_when_a_candidate_overrides_accepts(monkeypatch):
    """`accepts()` を上書きしている候補が1つでもあれば `read_head` を呼ぶ（既定のみの候補と混在する場合）。"""
    calls = []

    def read_head():
        calls.append(1)
        return "MAGIC"

    class _OverridingAnalyzer(_PlainAnalyzer):
        def accepts(self, rel_path, head_text=""):
            return head_text == "MAGIC"

    plain = _PlainAnalyzer("plain")                     # accepts 未上書き（既定・常に真）
    overriding = _OverridingAnalyzer("magic")            # accepts をオーバーライド
    monkeypatch.setattr(registry, "_ANALYZERS", (overriding, plain))
    assert registry.resolve_lazy("x.zz", read_head) is overriding
    assert calls == [1]                                 # 上書きされた候補が1つでもあれば1回だけ読む


def test_resolve_lazy_returns_none_when_no_candidates_without_reading():
    """拡張子がどのアナライザにも一致しなければ `read_head` を呼ばず即 None（§7 裁定10）。"""
    calls = []
    assert registry.resolve_lazy("x.md", lambda: calls.append(1) or "x") is None
    assert calls == []


class _AlwaysAnalyzer(Analyzer):
    """テスト用フェイク（優先順/accepts 検証のため同一拡張子を複数登録する）。"""

    def __init__(self, name, accept=True):
        self.name = name
        self.extensions = frozenset({".zz"})
        self._accept = accept

    def accepts(self, rel_path, head_text=""):
        return self._accept

    def collect_defs(self, text, rel_path):
        return DefResult()

    def extract_refs(self, text, rel_path):
        return RefResult()


def test_priority_order_first_registered_wins_on_extension_conflict(monkeypatch):
    """同じ拡張子を複数のアナライザが要求したら**有効化リストの並び順（優先順）**の上位が担当（§7 裁定2）。"""
    a1, a2 = _AlwaysAnalyzer("first"), _AlwaysAnalyzer("second")
    monkeypatch.setattr(registry, "_ANALYZERS", (a1, a2))
    assert registry.resolve("x.zz") is a1
    monkeypatch.setattr(registry, "_ANALYZERS", (a2, a1))   # 並び順を入れ替えると担当も変わる
    assert registry.resolve("x.zz") is a2


def test_accepts_gate_skips_non_accepting_candidate(monkeypatch):
    """`accepts()` が偽を返す候補は飛ばして次点へ（§7 裁定10）。全滅なら None＝資料扱い。"""
    declines, accepts_ = _AlwaysAnalyzer("declines", accept=False), _AlwaysAnalyzer("accepts", accept=True)
    monkeypatch.setattr(registry, "_ANALYZERS", (declines, accepts_))
    assert registry.resolve("x.zz", head_text="anything") is accepts_
    monkeypatch.setattr(registry, "_ANALYZERS", (declines, declines))
    assert registry.resolve("x.zz") is None


def test_unregistered_extension_file_is_silently_skipped_by_build_world():
    """未担当拡張子＝資料として扱う（グラフに乗らない・例外なし・§7 裁定4/10）。"""
    from sherpa.ingest import world_graph
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "案件A"
        base.mkdir()
        (base / "note.txt").write_text("PROGRAM-ID. NOT-CODE.\n", encoding="utf-8")
        nodes, edges, flags = world_graph.build_world(Path(d), "w")
        assert nodes == [] and edges == [] and flags == []


class _BadLabelAnalyzer(Analyzer):
    """語彙外ラベルを返す不正アナライザ（レジストリ側で破棄されることの検証用）。"""

    name = "badlabel"
    extensions = frozenset({".zz"})

    def collect_defs(self, text, rel_path):
        return DefResult(primary=DefItem(label="Frobnicator", name="X"))

    def extract_refs(self, text, rel_path):
        return RefResult()


class _BadRefAnalyzer(Analyzer):
    """有効な主体定義だが、語彙外のエッジ型／ラベルを参照候補として返す不正アナライザ。"""

    name = "badref"
    extensions = frozenset({".zz"})

    def collect_defs(self, text, rel_path):
        return DefResult(primary=DefItem(label="Module", name="FOO"))

    def extract_refs(self, text, rel_path):
        return RefResult(refs=[RefCandidate("BOGUS_EDGE", "Module", "BAR", 1),
                               RefCandidate("INVOKES", "Frobnicator", "BAR", 2)])


def test_unknown_node_label_is_discarded_with_flag(monkeypatch):
    """docs/05 にないノードラベルはノード化せず `flags` に理由付きで記録（§7 裁定5）。"""
    from sherpa.ingest import world_graph
    monkeypatch.setattr(registry, "_ANALYZERS", (_BadLabelAnalyzer(),))
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "案件A"
        base.mkdir()
        (base / "a.zz").write_text("dummy\n", encoding="utf-8")
        nodes, edges, flags = world_graph.build_world(Path(d), "w")
        assert nodes == [] and edges == []
        assert flags == [{"reason": "unknown_label", "analyzer": "badlabel",
                          "label": "Frobnicator", "from": "案件A/a.zz"}]


def test_unknown_edge_type_and_ref_label_are_discarded_with_flags(monkeypatch):
    """docs/05 にないエッジ型／参照先ラベルは張らず `flags` に理由付きで記録（§7 裁定5）。"""
    from sherpa.ingest import world_graph
    monkeypatch.setattr(registry, "_ANALYZERS", (_BadRefAnalyzer(),))
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "案件A"
        base.mkdir()
        (base / "a.zz").write_text("dummy\n", encoding="utf-8")
        nodes, edges, flags = world_graph.build_world(Path(d), "w")
        assert len(nodes) == 1 and nodes[0]["label"] == "Module" and nodes[0]["name"] == "FOO"
        assert edges == []                                  # 語彙外は1本も張られない
        reasons = {(fl["reason"], fl.get("edge_type") or fl.get("label")) for fl in flags}
        assert reasons == {("unknown_edge_type", "BOGUS_EDGE"), ("unknown_label", "Frobnicator")}


class _HijackExtraAnalyzer(Analyzer):
    """`extra` で共通層の予約キー（label/cid/analyzer 等）を上書きしようとする不正アナライザ
    （主体定義・子定義の両方で試みる）。
    """

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
    """`DefItem.extra` は共通層が確定したフィールド（label/cid/analyzer/name/world_id 等）を
    上書きできない——1つでも衝突したら `extra` を**丸ごと**捨てて（衝突していない他のキーも道連れ）
    `flags` に記録し、正しい共通層の値を使う（部分採用しない）。
    """
    from sherpa.ingest import world_graph
    monkeypatch.setattr(registry, "_ANALYZERS", (_HijackExtraAnalyzer(),))
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "案件A"
        base.mkdir()
        (base / "a.zz").write_text("dummy\n", encoding="utf-8")
        nodes, edges, flags = world_graph.build_world(Path(d), "w")
        by_name = {n["name"]: n for n in nodes}

        primary = by_name["FOO"]
        assert primary["label"] == "Module"                  # 偽装ラベルに上書きされていない
        assert primary["analyzer"] == "hijack"                # 偽装来歴に上書きされていない
        assert primary["cid"] == "module:w:案件A/a.zz#FOO"    # 偽装 cid に上書きされていない

        # child は cid_key（qualified）由来の正しい cid・正しい name のまま。extra は衝突キー
        # （name/world_id）を含むため丸ごと捨てられ、衝突していない "qualified" も道連れで消える
        # （部分採用しない＝「ここまでは信じてよい」という誤った安心感を与えない）。
        child = next(n for n in nodes if n["label"] == "DataItem")
        assert child["name"] == "BAR"                         # 偽装名に上書きされていない
        assert child["world_id"] == "w"                       # 偽装 world_id に上書きされていない
        assert "qualified" not in child                       # 衝突していないキーも extra ごと消える

        reasons = [(fl["reason"], fl["name"], tuple(sorted(fl["keys"]))) for fl in flags]
        assert ("reserved_key_in_extra", "FOO", ("analyzer", "cid", "label")) in reasons
        assert ("reserved_key_in_extra", "BAR", ("name", "world_id")) in reasons


def test_jcl_proc_file_without_job_still_detects_dropped_syntax():
    """JOB を持たない JCL PROC ファイル（主体なし）でも、受理済み（拡張子一致＋accepts 通過）
    である以上 Pass2 を必ず通り、`EXEC PROC=`/`INCLUDE MEMBER=` が dropped_syntax として
    flags に記録される（主体の有無で Pass2 の実行を左右しない）。"""
    from sherpa.ingest import world_graph
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "案件A"
        base.mkdir()
        (base / "INNERPRC.jcl").write_text(
            "//INNERPRC PROC\n"
            "//STEP1    EXEC PROC=INNER\n"
            "// INCLUDE MEMBER=SHARED\n",
            encoding="utf-8")
        nodes, edges, flags = world_graph.build_world(Path(d), "w")
        assert nodes == [] and edges == []          # 主体（JOB）が無いのでノード/エッジは作られない
        reasons = {(fl["reason"], fl.get("why")) for fl in flags}
        assert ("dropped_syntax", "proc_exec") in reasons
        assert ("dropped_syntax", "include_member") in reasons


def test_unreadable_registered_code_file_produces_blocked_flag(monkeypatch):
    """受理済み（拡張子が一致する）コード文書の実読込失敗は blocked flag を出す。

    `corpus_docs.classify_document` は既定 accepts のアナライザでは内容を読まないため
    この失敗を検知できないが、実際に読み込む Pass1 は必ず検知する。`worker._run_locked` は
    この flag（`action=="blocked"`）を見て run 全体を失敗させ、台帳書込・Neo4j 反映へ進ませない
    （fail-closed・部分グラフを確定しない）。
    """
    from sherpa.ingest import world_graph
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "案件A"
        base.mkdir()
        (base / "BADPROG.cbl").write_text("       PROGRAM-ID. BADPROG.\n", encoding="utf-8")

        real_read_text = Path.read_text

        def _boom(self, *a, **kw):
            if self.name == "BADPROG.cbl":              # 対象ファイル限定（他の読み取りは通常どおり）
                raise OSError("simulated read failure")
            return real_read_text(self, *a, **kw)

        monkeypatch.setattr(Path, "read_text", _boom)
        nodes, edges, flags = world_graph.build_world(Path(d), "w")
        assert nodes == [] and edges == []
        blocked = [f for f in flags if f.get("action") == "blocked"]
        assert blocked == [{"doc": "案件A/BADPROG.cbl", "reason": "unreadable_code_file", "action": "blocked"}]
