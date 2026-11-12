"""優先・除外 区切り 3・6 巡目: 論理の原本のパスと派生名の区別・resolve_world_entity の所属ごとの範囲・接尾の契約。名前は架空。"""
from __future__ import annotations

import ast
import json
import pathlib
import re

import pytest

from sherpa import derived_suffixes as DS
from sherpa import path_rules as PR

WORLD = "prrv7"
ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("SHERPA_MCP_WORLD", raising=False)
    monkeypatch.setenv(PR.ENV_KEY, json.dumps({WORLD: [
        {"path": "A", "kind": "exclude", "outside_scope": False, "target_type": "file"}]}))


def test_logical_source_path_is_not_dropped_just_because_it_looks_like_a_derived_name():
    """原本 `A`（拡張子の無いファイル）を除外しても、別の原本 `A.md` は見える資料のまま。派生名は派生物を指す欄だけに当てる。"""
    r = PR.rules_for(WORLD)
    assert r.excluded("A") and not r.excluded("A.md") and r.derived_of_excluded("A.md")
    res = {"hits": [{"doc_id": "A.md", "path": "A.md", "text": "t"}, {"doc_id": "A", "path": "A"},
                    {"doc_id": "B.md", "rag_path": "A.rag.md"}, {"doc_id": "C.md", "rag_path": "C.rag.md"}],
           "sources_verified": ["A.md", "A"], "evidence_refs": ["A.md:3", "A:4"]}
    out = PR.filter_output(res, WORLD)
    assert [h["doc_id"] for h in out["hits"]] == ["A.md", "C.md"]          # 論理のパスは excluded() だけ。派生物を指す欄は派生名も当てる
    assert out["sources_verified"] == ["A.md"] and out["evidence_refs"] == ["A.md:3"]


def test_resolve_world_entity_uses_membership_scope_and_visible_path_only_with_rules(monkeypatch):
    """所属（membership_paths）を持つノード（Dataset・定義の無い表）は、所属ごとの範囲と代表パスで問い合わせる。除外の設定があるときだけ、除外の条件のパラメータが付く。"""
    from sherpa.ingest import world_neo4j
    seen = []
    monkeypatch.setattr(world_neo4j, "_run_read_capped", lambda session, cy, **kw: seen.append((cy, kw)) or [])
    world_neo4j.resolve_world_entity(None, "DS", WORLD)
    cy, kw = seen[-1]
    assert "membership_paths" in cy and "mpath" in cy and kw["x_exact"] == ["A"] and "s_exact" in kw
    monkeypatch.setenv(PR.ENV_KEY, json.dumps({}))
    world_neo4j.resolve_world_entity(None, "DS", WORLD)
    cy, kw = seen[-1]
    assert "membership_paths" in cy and "mpath" in cy and "n.path" in cy   # 所属を持たないノードは path のまま
    assert "x_exact" not in kw and "s_exact" in kw


# ---- 接尾の契約: 書く側の接尾の文字列が、正本（derived_suffixes.ALL）に全部入っている ----

_WRITER_FILES = ["sherpa/ingest/office_md.py", "sherpa/ingest/observation_render.py", "sherpa/ingest/ocr_worker.py",
                 "sherpa/ingest/metafile_render.py", "sherpa/pdf_page_image.py", "sherpa/ingest/worker.py",
                 "sherpa/excerpts.py", "sherpa/es_index.py", "sherpa/worlds.py"]
# 1 つの資料に属する成果物ではない（鍵の置き場・メタの断片など）
_NOT_PER_DOCUMENT = {".key.json", ".meta.json"}
_ARTIFACT_RE = re.compile(r"\.[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\.(?:md|json|jsonl)")


def _literals(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            yield n.value


def test_writers_derived_suffix_literals_are_all_in_the_canonical_list():
    """派生物を書く所の接尾の文字列が `derived_suffixes.ALL` に全部入っている。新しい接尾が書く側に増えたら、ここで落ちて気づける。"""
    missing, seen = {}, set()
    for rel in _WRITER_FILES:
        for lit in set(_literals(ROOT / rel)):
            if _ARTIFACT_RE.fullmatch(lit):
                seen.add(lit)
                if lit not in DS.ALL and lit not in _NOT_PER_DOCUMENT:
                    missing.setdefault(rel, set()).add(lit)
    assert {".rag.md", ".evidence.json", ".rag_chunks.jsonl", ".ai_observations.jsonl"} <= seen   # 検査が空振りしていない
    assert not missing, f"derived_suffixes.ALL に無い接尾が書く側にある（正本に足すこと）: {missing}"
    # フォルダの接尾（.assets／.pages）は、書く側が使っているものが正本に入っている
    for rel in ("sherpa/ingest/office_md.py", "sherpa/ingest/ocr_worker.py", "sherpa/ingest/metafile_render.py"):
        assert DS.ASSETS in set(_literals(ROOT / rel)) or rel.endswith("office_md.py")
    assert {DS.ASSETS, DS.PAGES} <= set(DS.ALL)


# ---- 区切り 3・7 巡目 ----

def test_classification_fields_named_source_are_not_treated_as_paths(monkeypatch):
    """分類の語（es／graph／grep／both）が入る `source` 欄を、資料を指す欄として扱わない。同じ名前の場所を除外しても別のカードを落とさない。"""
    monkeypatch.setenv(PR.ENV_KEY, json.dumps({WORLD: [
        {"path": name, "kind": "exclude", "outside_scope": False, "target_type": "folder"}
        for name in ("es", "graph", "grep", "both", "keyword", "名前")]}))
    res = {"cards": [{"name": "c1", "source": "es", "doc_id": "ZZnew/a.md"}, {"name": "名前", "source": "graph", "label": "Module"},
                     {"name": "c3", "source": "both", "rel": "es", "evidence": {"grep": [{"doc_id": "ZZnew/b.md"}]}},
                     {"name": "c4", "source": "grep"}],
           "count": 4}
    assert PR.filter_output(res, WORLD) == res                              # 何も落ちない
    out = PR.filter_output({"cards": [{"source": "es", "doc_id": "es/x.md"}, {"source": "es", "doc_id": "ZZnew/a.md"}]}, WORLD)
    assert out["cards"] == [{"source": "es", "doc_id": "ZZnew/a.md"}]       # 資料を指す欄（doc_id）の一致だけで落ちる


def test_fused_search_graph_engine_gets_the_outside_scope_extension(monkeypatch):
    """外部の検索 API の graph エンジンにも、「範囲の外でも読む」を範囲に足して渡す（keyword／vector と同じ範囲）。"""
    from sherpa import documents
    from sherpa.parts.read import fused_search
    monkeypatch.setenv(PR.ENV_KEY, json.dumps({WORLD: [{"path": "ZZcommon", "kind": "priority", "outside_scope": True}]}))
    seen = {}

    def graph(world, query, sp, k, depth=None, **kw):
        seen["graph"] = list(sp)
        return [], None

    def keyword(world, query, sp, k, settings, layer=None):
        seen["keyword"] = list(sp)
        return [], None
    monkeypatch.setattr(fused_search, "_search_graph", graph)
    monkeypatch.setattr(fused_search, "_search_keyword", keyword)
    monkeypatch.setattr(documents, "existing_rels", lambda *a, **k: set())
    fused_search.search(WORLD, "q", engines=["graph", "keyword"], scope_paths=["ZZsysB"], include_presumed=False)
    assert seen == {"graph": ["ZZsysB", "ZZcommon"], "keyword": ["ZZsysB", "ZZcommon"]}
    monkeypatch.setenv(PR.ENV_KEY, json.dumps({}))                          # 設定が無ければ従来どおり利用者の範囲のまま
    fused_search.search(WORLD, "q", engines=["graph"], scope_paths=["ZZsysB"], include_presumed=False)
    assert seen["graph"] == ["ZZsysB"]


# ---- 区切り 3・8 巡目 ----

def test_existence_is_three_valued_and_unknown_is_not_treated_as_gone(monkeypatch, tmp_path):
    """実在の判定は三値: ENOENT・ENOTDIR だけが「無い」。権限・入出力の失敗は「確かめられない」で、取り除かない。"""
    import errno
    import os
    from sherpa import worlds
    (tmp_path / "ZZkeep").mkdir()
    monkeypatch.setattr(worlds, "world_dir", lambda w: tmp_path)
    monkeypatch.setattr(worlds, "archives_dir", lambda w: tmp_path / "_arch")
    real = os.lstat

    def lstat(p, *a, **k):
        if str(p).endswith("ZZperm"):
            raise PermissionError(errno.EACCES, "denied")
        if str(p).endswith("ZZeio"):
            raise OSError(errno.EIO, "io")
        if str(p).endswith("ZZnotdir/x"):
            raise NotADirectoryError(errno.ENOTDIR, "not a dir")
        return real(p, *a, **k)
    monkeypatch.setattr(os, "lstat", lstat)
    rules = PR.validate_entries([{"path": p, "kind": "exclude"} for p in ("ZZkeep", "ZZgone", "ZZnotdir/x")])
    kept, gone = PR.prune_missing(WORLD, rules)
    assert [r.path for r in kept] == ["ZZkeep"] and sorted(r.path for r in gone) == ["ZZgone", "ZZnotdir/x"]
    for bad in ("ZZperm", "ZZeio"):
        with pytest.raises(PR.ExistenceUnknown):
            PR.prune_missing(WORLD, PR.validate_entries([{"path": bad, "kind": "exclude"}]))


def test_sandbox_fails_closed_when_an_excluded_target_became_a_symlink(tmp_path):
    from sherpa.providers.codex import sandbox
    kb, rag = tmp_path / "kb", tmp_path / "rag"
    (kb / "B").mkdir(parents=True)
    (kb / "B" / "secret.md").write_text("x")
    (kb / "A.docx").symlink_to(kb / "B")                                    # 除外のファイルが symlink に置き換わった
    rules = PR.Rules(PR.validate_entries([{"path": "A.docx", "kind": "exclude", "target_type": "file"}]))
    with pytest.raises(RuntimeError, match="symlink"):
        sandbox._exclude_deny_entries([str(kb)], rules)
    (kb / "A.docx").unlink()
    (kb / "A.docx").write_text("x")
    (rag).mkdir()
    (rag / "A.docx.rag.md").symlink_to(kb / "B" / "secret.md")              # 派生物が symlink
    with pytest.raises(RuntimeError, match="symlink"):
        sandbox._exclude_deny_entries([str(kb), str(rag)], rules, derived_roots={str(rag.resolve()): "rag"})


def test_scope_and_not_excluded_are_judged_on_the_same_membership(monkeypatch):
    """複数の資料に属するノードは、同じ所属が「範囲の中」かつ「除外でない」ときだけ通す。設定が無ければ従来の式のまま。"""
    from sherpa.ingest import world_neo4j
    monkeypatch.setenv(PR.ENV_KEY, json.dumps({WORLD: [{"path": "ZAold", "kind": "exclude", "outside_scope": False}]}))
    sql = world_neo4j.member_scope_visible(WORLD, "n")
    assert "any(x_mp IN n.membership_paths WHERE (size($prefixes)=0" in sql and "AND NOT coalesce((x_mp IN $x_exact" in sql
    monkeypatch.setenv(PR.ENV_KEY, json.dumps({}))
    assert world_neo4j.member_scope_visible(WORLD, "n") == world_neo4j._member_scope_pred("n")


# ---- 区切り 3・9 巡目 ----

def test_es_scope_clause_adds_only_outside_files_that_remain_in_the_narrowed_scope(monkeypatch):
    """`path_prefix` で狭めた範囲に、範囲の外の優先のファイルを戻さない。"""
    r = PR.Rules(PR.validate_entries([{"path": "B/priority.md", "kind": "priority", "outside_scope": True}]))
    assert r.es_scope_clause(["A/sub"]) == {"terms": {"scopes": ["A/sub"]}}              # 狭めた範囲には入らない
    wide = r.es_scope_clause(["A/sub", "B/priority.md"])                                   # 足し込み済みの範囲には入る
    assert wide["bool"]["should"][1] == {"terms": {"doc_id": ["B/priority.md"]}}
    # 検索の問い合わせ（Fixed の範囲＝path_prefix で狭めた範囲）でも doc_id の条件が戻らない
    from sherpa import es_index
    monkeypatch.setenv(PR.ENV_KEY, json.dumps({WORLD: [{"path": "B/priority.md", "kind": "priority", "outside_scope": True}]}))
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "_embed_system_settings_snapshot", lambda: None)
    bodies = []
    monkeypatch.setattr(es_index, "_req", lambda m, p, body=None, **k: bodies.append(body) or {"hits": {"hits": []}})
    es_index.search(WORLD, "q", scope_paths=PR.Fixed(["A/sub"]), vector=False)
    assert bodies and all(b["query"]["bool"]["filter"][0] == {"terms": {"scopes": ["A/sub"]}} for b in bodies)   # 範囲の条件は狭めたまま


def test_sandbox_fails_closed_on_io_errors_while_listing_excluded_targets(monkeypatch, tmp_path):
    """除外の対象の確認で I/O のエラー（権限・入出力）が起きたら、直読み全体を止める。無いと確かめられたときだけ飛ばす。"""
    import errno
    import pathlib
    from sherpa.providers.codex import sandbox
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "A.docx").write_text("x")
    rules = PR.Rules(PR.validate_entries([{"path": "A.docx", "kind": "exclude", "target_type": "file"},
                                          {"path": "gone.docx", "kind": "exclude", "target_type": "file"}]))
    real_resolve = pathlib.Path.resolve

    def resolve(self, *a, **k):
        if self.name == "A.docx":
            raise OSError(errno.EIO, "io error")
        return real_resolve(self, *a, **k)
    monkeypatch.setattr(pathlib.Path, "resolve", resolve)
    with pytest.raises(RuntimeError, match="OSError"):
        sandbox._exclude_deny_entries([str(kb)], rules)
    monkeypatch.setattr(pathlib.Path, "resolve", real_resolve)
    assert [p.rsplit("/", 1)[1] for p in sandbox._exclude_deny_entries([str(kb)], rules)] == ["A.docx"]   # 無い（gone）は飛ばす


# ---- 区切り 3・10 巡目: 例外を隠さない確認 ----

def _eio_for(monkeypatch, name):
    """`name` で終わるパスの lstat／stat だけを EIO にする（ほかは実物）。"""
    import errno
    import os
    real_lstat, real_stat = os.lstat, os.stat

    def lstat(p, *a, **k):
        if str(p).endswith(name):
            raise OSError(errno.EIO, "io error")
        return real_lstat(p, *a, **k)

    def stat(p, *a, **k):
        if str(p).endswith(name):
            raise OSError(errno.EIO, "io error")
        return real_stat(p, *a, **k)
    monkeypatch.setattr(os, "lstat", lstat)
    monkeypatch.setattr(os, "stat", stat)


def test_io_error_on_an_excluded_target_stops_direct_read_instead_of_dropping_the_deny(monkeypatch, tmp_path):
    from sherpa.providers.codex import sandbox
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "A.docx").write_text("x")
    rules = PR.Rules(PR.validate_entries([{"path": "A.docx", "kind": "exclude", "target_type": "file"}]))
    assert [p.rsplit("/", 1)[1] for p in sandbox._exclude_deny_entries([str(kb)], rules)] == ["A.docx"]
    _eio_for(monkeypatch, "A.docx")
    with pytest.raises(RuntimeError, match="OSError"):
        sandbox._exclude_deny_entries([str(kb)], rules)


def test_final_deny_formatting_is_strict_for_every_deny_when_exclusions_exist(monkeypatch, tmp_path):
    """除外の設定がある資料フォルダでは、deny はすべて strict（確かめられなければ止める）。設定が無い経路は従来どおり黙って落とす。"""
    from sherpa.providers.codex import sandbox
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "A.docx").write_text("x")
    (kb / "S.txt").write_text("x")
    (kb / "link").symlink_to(kb / "A.docx")
    deny = [str(kb / "A.docx"), str(kb / "S.txt"), str(kb / "gone"), str(kb / "link")]
    # 無い・symlink は strict でも落とす（確かめられた結果）。従来どおり
    assert sandbox._prune_deny_paths(deny, [str(kb)]) == sandbox._prune_deny_paths(deny, [str(kb)], strict=True) == [
        str(kb / "A.docx"), str(kb / "S.txt")]
    _eio_for(monkeypatch, "S.txt")
    # 設定が無い経路（strict でない）は、確かめられない deny を従来どおり黙って落とす
    assert sandbox._prune_deny_paths(deny, [str(kb)]) == [str(kb / "A.docx")]
    # 設定がある経路は、範囲・秘匿の deny でも確かめられなければ止める
    with pytest.raises(RuntimeError, match="deny_prune_failed"):
        sandbox._prune_deny_paths(deny, [str(kb)], strict=True)


def test_scope_deny_that_covers_an_excluded_target_cannot_be_dropped_silently(monkeypatch, tmp_path):
    """範囲 B のとき deny A（除外 A/secret.txt を含む）。`_exclude_deny_entries` は範囲の deny の配下を省くので除外の deny は空になるが、
    範囲の deny の lstat が EIO のときは、最終の整形で黙って落とさず RuntimeError にする（KB root の read だけが残らない）。"""
    from sherpa.providers.codex import sandbox
    kb = tmp_path / "kb"
    (kb / "A").mkdir(parents=True)
    (kb / "B").mkdir()
    (kb / "A" / "secret.txt").write_text("x")
    rules = PR.Rules(PR.validate_entries([{"path": "A/secret.txt", "kind": "exclude", "target_type": "file"}]))
    scope_deny = sandbox._scope_deny_entries([str(kb)], ["B"])
    assert scope_deny == [str((kb / "A").resolve())]
    assert sandbox._exclude_deny_entries([str(kb)], rules, scope_deny) == []      # 範囲の deny の配下なので除外の deny は空
    assert sandbox._prune_deny_paths(scope_deny, [str(kb)], strict=True) == scope_deny
    _eio_for(monkeypatch, "kb/A")
    assert sandbox._prune_deny_paths(scope_deny, [str(kb)]) == []                  # 設定が無い経路は従来どおり落とす
    with pytest.raises(RuntimeError, match="deny_prune_failed"):
        sandbox._prune_deny_paths(scope_deny, [str(kb)], strict=True)             # 設定がある経路は止める


def test_config_writer_prunes_strictly_only_when_the_world_has_exclusions(monkeypatch, tmp_path):
    """permission profile を書くときも同じ線: 除外の設定がある資料フォルダだけ strict。"""
    from sherpa.providers.codex import sandbox
    seen = []
    monkeypatch.setattr(sandbox, "_prune_deny_paths", lambda deny, roots, strict=False: seen.append(strict) or [])
    kb = tmp_path / "kb"
    kb.mkdir()
    homes = iter([tmp_path / "h1", tmp_path / "h2"])

    def write():
        home = next(homes)
        home.mkdir()
        sandbox._write_codex_authoring_config(home, [str(kb)], "r", False, WORLD, None, direct_read_roots=[str(kb)],
                                              sensitive_deny=[str(kb / "x")])
    monkeypatch.setenv(PR.ENV_KEY, json.dumps({}))                          # 除外の設定が無い資料フォルダ
    write()
    monkeypatch.setenv(PR.ENV_KEY, json.dumps({WORLD: [{"path": "A", "kind": "exclude", "outside_scope": False}]}))
    write()
    assert seen == [False, True]


def test_derived_read_roots_check_is_strict_when_exclusions_exist(monkeypatch, tmp_path):
    """除外の設定があるとき、派生の置き場の確認が I/O エラーで偽になって接尾の列挙を飛ばさない（直読み全体を止める）。"""
    from sherpa import worlds
    from sherpa.providers.codex import sandbox
    md, rag = tmp_path / "md", tmp_path / "rag"
    md.mkdir()
    rag.mkdir()
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: md)
    monkeypatch.setattr(worlds, "derived_rag_dir", lambda w: rag)
    want = {str(md.resolve()): "md", str(rag.resolve()): "rag"}
    assert sandbox._derived_read_roots(WORLD) == want and sandbox._derived_read_roots(WORLD, strict=True) == want
    _eio_for(monkeypatch, "rag")
    assert sandbox._derived_read_roots(WORLD) == {str(md.resolve()): "md"}                       # 設定が無い経路は従来どおり
    with pytest.raises(RuntimeError, match="derived_roots_failed"):
        sandbox._derived_read_roots(WORLD, strict=True)


def test_type_and_existence_checks_do_not_hide_io_errors(monkeypatch, tmp_path):
    """種別の判定は確かめられないとき空（file 側へ倒す）。取り除きの根の確認も、確かめられないときは取り除かない。"""
    from sherpa import worlds
    (tmp_path / "ZZdoc").mkdir()
    (tmp_path / "ZZdoc" / "README").write_text("x")
    monkeypatch.setattr(worlds, "world_dir", lambda w: tmp_path)
    monkeypatch.setattr(worlds, "archives_dir", lambda w: tmp_path / "_arch")
    assert PR.detect_target_type(WORLD, "ZZdoc/README") == "file" and PR.detect_target_type(WORLD, "ZZdoc") == "folder"
    _eio_for(monkeypatch, "ZZdoc/README")
    assert PR.detect_target_type(WORLD, "ZZdoc/README") == ""             # 確かめられない: 空＝file 側（派生物も除外）
    monkeypatch.undo()
    monkeypatch.setattr(worlds, "world_dir", lambda w: tmp_path)
    monkeypatch.setattr(worlds, "archives_dir", lambda w: tmp_path / "_arch")
    _eio_for(monkeypatch, str(tmp_path))                                   # 資料フォルダの根を確かめられない
    with pytest.raises(PR.ExistenceUnknown):
        PR.prune_missing(WORLD, PR.validate_entries([{"path": "ZZdoc", "kind": "exclude"}]))


def test_one_turn_uses_the_rules_read_at_prepare_for_deny_strictness_and_the_mcp_env(monkeypatch, tmp_path):
    """準備の後に管理者が規則を変えても、permission profile の strict と MCP の子へ渡す env は、準備で読んだ規則のまま（読み直さない）。"""
    from sherpa.providers.codex import mcp as mcp_mod
    from sherpa.providers.codex import sandbox
    strict_seen, envs = [], []
    monkeypatch.setattr(sandbox, "_prune_deny_paths", lambda deny, roots, strict=False: strict_seen.append(strict) or [])
    real_env = sandbox._mcp_env
    monkeypatch.setattr(sandbox, "_mcp_env", lambda *a, **k: envs.append(real_env(*a, **k)) or envs[-1])
    kb = tmp_path / "kb"
    kb.mkdir()
    homes = iter(tmp_path / f"h{i}" for i in range(4))

    def write(rules):
        home = next(homes)
        home.mkdir()
        sandbox._write_codex_authoring_config(home, [str(kb)], "r", True, WORLD, None, direct_read_roots=[str(kb)],
                                              sensitive_deny=[str(kb / "x")], rules=rules)
    old_empty = PR.Rules(())
    old_excl = PR.Rules(PR.validate_entries([{"path": "OLD", "kind": "exclude", "target_type": "folder"}]))
    # 準備の後に、管理者が別の規則（A を除外）を保存した状況（この間の読み直しは新しい規則になる）
    monkeypatch.setenv(PR.ENV_KEY, json.dumps({WORLD: [{"path": "NEW", "kind": "exclude", "outside_scope": False, "target_type": "folder"}]}))
    write(old_empty)                                                        # 準備のときは除外が無かった
    assert strict_seen[-1] is False and PR.ENV_KEY not in envs[-1]          # 新しい規則を読み直さない
    write(old_excl)                                                         # 準備のときの規則は OLD
    assert strict_seen[-1] is True
    assert list(json.loads(envs[-1][PR.ENV_KEY])[WORLD][0].values())[0] == "OLD"   # 子へ渡す写しは準備のときの規則
    monkeypatch.setenv(PR.ENV_KEY, json.dumps({}))                          # 読み直すと除外が無い（保存で全部消された）状況でも同じ
    write(old_excl)
    assert strict_seen[-1] is True and json.loads(envs[-1][PR.ENV_KEY])[WORLD][0]["path"] == "OLD"
    assert mcp_mod._mcp_env(WORLD, None, rules=old_excl)[PR.ENV_KEY] == envs[-1][PR.ENV_KEY]     # _mcp_env 単体も同じ
    write(None)                                                             # 規則を渡さない呼び出し（ターン外）は従来どおり読む
    assert strict_seen[-1] is False and PR.ENV_KEY not in envs[-1]
