"""範囲（`scope_paths`）を rg の起動前に効かせる直しの契約テスト。

合成の木（架空の `ZZ…`）で、範囲あり・なしの組み合わせごとに、
① ヒットの集合・並び・件数・「確かめきれなかった」の印が、全体を読んで範囲で絞った結果（純 Python 走査・全体走査の結果を範囲で絞ったもの）と一致すること
② rg の起点は根のまま（範囲の中のパスを明示して渡さない）で、候補が範囲の外へ出ないこと
③ 範囲ごとに木の署名・候補の保存・除外の計画が分かれること
④ フォルダ名のグロブの特別な文字・途中の空白・大文字小文字・symlink の差し替えで、見つかるものが減らず資料フォルダの外も読まないこと
⑤ -g に書けない名前（前後の空白・制御文字・改行・タブ）の範囲は、範囲なしと同じ動き（全体を歩いて後で絞る）になること
を確かめる。
"""
from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

from sherpa import grep_tool as G
from sherpa import rg_prune, rg_search, scope, search_cache

pytestmark = [pytest.mark.usefixtures("upstream_only_registry"),
              pytest.mark.skipif(rg_search.find_rg() is None, reason="rg が無い環境では候補の絞り込みを使わない")]

WORLD = "tw"
NEEDLE = "ZZNEEDLE"
JP = "ZZ顧客"

SCOPES = [
    None,
    ["ZZsys"],
    ["ZZsys/ZZapp"],
    ["ZZsys/ZZapp/ZZsub"],
    ["ZZsys", "ZZsys/ZZapp"],
    ["ZZsys/ZZapp", "ZZdocs"],
    ["ZZsys/ZZapp/ZZsub", "ZZsys/ZZeuc"],
    ["ZZnope"],
    ["ZZsys/ZZnope", "ZZdocs"],
    ["ZZsy"],
    ["/ZZsys/"],
    ["ZZdocs/ZZreadme.md"],
    ["ZZdocs/ZZoffice.docx"],
    ["ZZdocs/ZZlegacy.xlsx"],
    ["ZZarc.zip"],
    ["ZZdocs/ZZfolder.pdf"],
    ["ZZdocs/ZZbin.pdf"],
    ["ZZsys/ZZlinkdir"],
    ["ZZsys/.git"],
    ["ZZsys/../ZZdocs"],
    ["ZZsys/ZZapp/ZZlocked.txt"],
    ["ZZsp[br]"],
    ["ZZsp{a,b}"],
    ["ZZsp*star"],
    ["ZZsp?q"],
    ["!ZZbang"],
    ["ZZsp\\back"],
    ["ZZsp ace/ZZin"],
    ["#ZZhash"],
    ["ZZsp]"],
    ["ZZsp[br]", "ZZsp{a,b}", "ZZsp*star", "ZZdocs"],
    ["ZZsp[br]/ZZin/ZZf.txt"],
    ["ZZsp[br]/ZZin/ZZf.TXT"],
    ["ZZsys/ ZZlead"],
    ["ZZnl\nx"],
    ["ZZtrail "],
    ["ZZtrail /ZZin"],
    ["ZZtab\tx"],
    ["ZZsp  dbl"],
    ["ZZfw\u3000sp"],
    ["ZZc,omma", "ZZq'uote", "ZZd$ollar"],
    ["ZZt~ilde", "ZZ^caret", "ZZmid#hash", "ZZdash-x"],
    ["ZZsp ace/ZZin", "ZZtrail "],
    ["zzsys"],
    ["ZZSYS/ZZapp"],
    ["ZZdocs/ZZreadme.MD"],
    ["ZZsys/ZZapp/ZZsub/"],
    ["ZZsp?q", "ZZsp*", "ZZsp"],
]

TERMS = [NEEDLE, NEEDLE.lower(), JP, "ZZEUCWORD", "ZZABSENT", "ZZ"]


def _put(root: pathlib.Path, rel: str, body) -> pathlib.Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body if isinstance(body, bytes) else body.encode("utf-8"))
    return p


def _build(tmp_path: pathlib.Path, importance: bool) -> None:
    root = tmp_path / "world"
    der = tmp_path / "derived" / WORLD
    _put(root, "ZZsys/ZZapp/ZZmain.cob", f"      * {JP}\n      * {NEEDLE} cp932\n".encode("cp932"))
    _put(root, "ZZsys/ZZapp/ZZsub/ZZdeep.txt", f"head\n{NEEDLE} deep\n{JP}\n")
    _put(root, "ZZsys/ZZapp/ZZsub/ZZdeep2.java", f"class A {{ /* {NEEDLE} */ }}\n")
    _put(root, "ZZsys/ZZapp/ZZlocked.txt", f"{NEEDLE} locked\n").chmod(0)
    _put(root, "ZZsys/ZZeuc/ZZeuc.txt", f"{JP} ZZEUCWORD\n".encode("euc-jp"))
    _put(root, "ZZsys/ZZapp2/ZZother.sql", f"select 1; -- {NEEDLE}\n")
    _put(root, "ZZsys/.git/ZZgit.txt", f"{NEEDLE} in git dir\n")
    _put(root, "ZZdocs/ZZreadme.md", f"# ZZ title\n{NEEDLE} body\n")
    _put(root, "ZZdocs/ZZcase.md", f"# t\n{NEEDLE.lower()} lower\n")
    _put(root, "ZZdocs/ZZfolder.pdf/inner.txt", f"{NEEDLE} in pdf-named folder\n")
    _put(root, "ZZdocs/ZZbin.pdf", f"%PDF {NEEDLE}\n")
    _put(root, "ZZtop.txt", f"{NEEDLE} top\n")
    for sp_name in ("ZZsp[br]", "ZZsp{a,b}", "ZZsp*star", "ZZsp?q", "!ZZbang", "ZZsp\\back", "ZZsp ace", "#ZZhash",
                    "ZZsp]", "ZZsys/ ZZlead", "ZZnl\nx", "ZZtrail ", "ZZtab\tx", "ZZsp  dbl", "ZZfw\u3000sp",
                    "ZZc,omma", "ZZq'uote", "ZZd$ollar", "ZZt~ilde", "ZZ^caret", "ZZmid#hash", "ZZdash-x"):
        _put(root, f"{sp_name}/ZZin/ZZf.txt", f"{NEEDLE} {JP} in {sp_name!r}\n")
        _put(root, f"{sp_name}/ZZtop.md", f"# t\n{NEEDLE} md\n")
    _put(root, "ZZsp[br]/ZZin/ZZf.TXT", f"{NEEDLE} upper ext\n")
    _put(root, "ZZdocs/ZZreadme.MD", f"# t\n{NEEDLE} upper md\n")
    _put(der, "rag/ZZsp[br]/ZZoffice.docx.rag.md", f"# o\n{NEEDLE} rag in bracket\n")
    _put(der, "md/ZZsp[br]/ZZoffice.docx.md", f"# o\n{NEEDLE} legacy in bracket\n")
    _put(der, "md/ZZsp{a,b}/ZZsheet.xlsx.md", f"# s\n{NEEDLE} sheet in brace\n")
    _put(der, "md/ZZdocs/ZZnote.markdown", f"# n\n{NEEDLE} markdown ext\n")
    (root / "ZZsys" / "ZZlinkdir").symlink_to(root / "ZZdocs", target_is_directory=True)
    (root / "ZZsys" / "ZZlinkfile.txt").symlink_to(root / "ZZtop.txt")
    _put(der, "archives/ZZarc.zip/ZZinner.txt", f"{NEEDLE} archived\n")
    _put(der, "rag/ZZdocs/ZZoffice.docx.rag.md", f"# ZZoffice\n{NEEDLE} rag\n")
    _put(der, "md/ZZdocs/ZZoffice.docx.md", f"# ZZoffice\n{NEEDLE} legacy twin\n")
    _put(der, "md/ZZdocs/ZZlegacy.xlsx.md", f"# ZZlegacy\n{NEEDLE} legacy only\n")
    _put(der, "md/ZZsys/ZZapp/ZZsheet.xlsx.md", f"# ZZsheet\n{NEEDLE} sheet\n{JP}\n")
    _put(der, "rag/ZZsys/ZZapp/ZZslide.pptx.rag.md", f"# ZZslide\n{NEEDLE} slide\n")
    _put(der, "md/ZZsys/ZZapp/ZZslide.pptx.md", f"# ZZslide\n{NEEDLE} slide legacy\n")
    _put(der, "md/ZZsys/ZZapp/ZZnote.txt", f"{NEEDLE} not md\n")
    if importance:
        _put(root, "_重要度.txt", "ZZdocs/ZZcase.md: 高\nZZsys/ZZapp/ZZmain.cob: 低\n")
        _put(root, "ZZsys/ZZapp/_重要度.txt", "ZZsub/ZZdeep.txt: 高\n")
        _put(root, "ZZsys/ZZeuc/_重要度.txt", "*: 低\n")
        _put(root, "ZZdocs/_重要度.txt", "ZZreadme.md: 高 # 契約\n")


@pytest.fixture(params=[False, True], ids=["plain", "importance"])
def env(request, monkeypatch, tmp_path):
    from sherpa import worlds as W
    root = tmp_path / "world"
    root.mkdir()
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(tmp_path / "derived"))
    monkeypatch.setattr(W, "world_dir", lambda w: root)
    _build(tmp_path, request.param)
    rg_prune._MEMO.clear()
    yield root, tmp_path / "derived" / WORLD
    rg_prune._MEMO.clear()
    for p in root.rglob("ZZlocked.txt"):
        p.chmod(0o644)


def _shape(hits):
    return [(h["doc_id"], h["line"], h["span"][0], h["span"][1], h["text"]) for h in hits]


def _run(term, sp, *, engine="auto", max_hits=1000, offset=0, layer=None):
    stats: dict = {}
    trunc: list = []
    hits = G.grep_search(term, world=WORLD, max_hits=max_hits, scope_paths=sp, layer=layer, offset=offset,
                         stats=stats, truncated_docs=trunc, engine=engine)
    return hits, stats, trunc


def _in(hit_or_doc, sp):
    return scope.in_scope(hit_or_doc, sp)


@pytest.mark.parametrize("sp", SCOPES, ids=lambda s: "none" if s is None else "+".join(s))
@pytest.mark.parametrize("term", TERMS)
def test_scoped_rg_matches_python_scan_and_full_scan_filtered(env, term, sp):
    hits, stats, trunc = _run(term, sp)
    py_hits, py_stats, py_trunc = _run(term, sp, engine="python")
    assert sorted(_shape(hits)) == sorted(_shape(py_hits))
    assert stats == py_stats
    assert sorted(trunc) == sorted(py_trunc)
    full, _s, full_trunc = _run(term, None)
    kept = [h for h in full if _in(h["doc_id"], sp)]
    assert _shape(hits) == _shape(kept)
    assert sorted(trunc) == sorted(d for d in full_trunc if _in(d, sp))


@pytest.mark.parametrize("sp", [["ZZsys"], ["ZZsys/ZZapp", "ZZdocs"], ["ZZnope"], None],
                         ids=lambda s: "none" if s is None else "+".join(s))
@pytest.mark.parametrize("max_hits,offset", [(2, 0), (2, 2), (3, 1), (1, 0)])
def test_scoped_paging_matches_full_scan_slice(env, sp, max_hits, offset):
    hits, _s, _t = _run(NEEDLE, sp, max_hits=max_hits, offset=offset)
    full, _s, _t = _run(NEEDLE, None)
    kept = [h for h in full if _in(h["doc_id"], sp)]
    assert _shape(hits) == _shape(kept[offset:offset + max_hits])


@pytest.mark.parametrize("layer", ["docs", "code", "both"])
@pytest.mark.parametrize("sp", [["ZZsys"], ["ZZdocs"], ["ZZsys/ZZapp", "ZZdocs"]], ids="+".join)
def test_scoped_layer_matches_full_scan_filtered(env, sp, layer):
    hits, _s, _t = _run(NEEDLE, sp, layer=layer)
    full, _s, _t = _run(NEEDLE, None, layer=layer)
    assert _shape(hits) == _shape([h for h in full if _in(h["doc_id"], sp)])


@pytest.mark.parametrize("sp", [None, ["ZZsys"], ["ZZsys/ZZapp"], ["ZZdocs", "ZZsys/ZZeuc"], ["ZZnope"]],
                         ids=lambda s: "none" if s is None else "+".join(s))
def test_scoped_many_terms_match_single_term_results(env, sp):
    terms = [NEEDLE, JP, "ZZABSENT"]
    stats: dict = {}
    many = G.grep_search_many(terms, world=WORLD, max_hits=1000, scope_paths=sp, stats=stats)
    for t in terms:
        single, _s, _t = _run(t, sp)
        assert _shape(many[t]) == _shape(single)


def test_scoped_truncated_docs_and_unreadable_match_python_scan(env, monkeypatch):
    monkeypatch.setattr(G, "_GREP_FILE_CAP_BYTES", 64)
    root, _der = env
    _put(root, "ZZsys/ZZapp/ZZsub/ZZbig.txt", "x" * 200 + f"\n{NEEDLE}\n")
    for sp in (["ZZsys"], ["ZZsys/ZZapp/ZZsub"], ["ZZdocs"], ["ZZnope"], None):
        hits, stats, trunc = _run(NEEDLE, sp)
        py_hits, py_stats, py_trunc = _run(NEEDLE, sp, engine="python")
        assert sorted(_shape(hits)) == sorted(_shape(py_hits))
        assert stats == py_stats and sorted(trunc) == sorted(py_trunc)
    if os.geteuid() != 0:
        _h, stats, _t = _run(NEEDLE, ["ZZsys/ZZapp"])
        assert stats.get("unreadable_files") == 1
        _h, stats, _t = _run(NEEDLE, ["ZZdocs"])
        assert not stats.get("unreadable_files")


def test_scoped_deadline_exceeded_is_reported_as_before(env):
    stats: dict = {}
    hits = G.grep_search(NEEDLE, world=WORLD, scope_paths=["ZZsys"], deadline=0.0, stats=stats,
                         partial_on_deadline=True)
    assert hits == [] and stats.get("deadline_exceeded")
    with pytest.raises(G.GrepDeadlineExceeded):
        G.grep_search(NEEDLE, world=WORLD, scope_paths=["ZZsys"], deadline=0.0)


# ===== rg の起点・含める条件 =====

class _Spy:
    """rg の起動を記録する。`before` があれば最初の起動の直前に 1 度だけ呼ぶ（確かめた後の差し替えを再現する）。"""

    def __init__(self, monkeypatch, before=None):
        self.calls: list[tuple[str | None, list[str], list[str]]] = []
        self.before = before
        rg = rg_search.find_rg()
        real_popen, real_run = subprocess.Popen, subprocess.run

        def _rec(cmd, cwd):
            if cmd and cmd[0] == rg and "--" in cmd:
                if self.before is not None:
                    fn, self.before = self.before, None
                    fn()
                i = cmd.index("--")
                globs = [cmd[k + 1] for k, a in enumerate(cmd[:i]) if a in ("-g", "--iglob")]
                self.calls.append((cwd, list(cmd[i + 1:]), globs))

        def popen(cmd, *a, **k):
            _rec(cmd, k.get("cwd"))
            return real_popen(cmd, *a, **k)

        def run(cmd, *a, **k):
            _rec(cmd, k.get("cwd"))
            return real_run(cmd, *a, **k)

        monkeypatch.setattr(subprocess, "Popen", popen)
        monkeypatch.setattr(subprocess, "run", run)

    def starts(self) -> list[str]:
        """rg に起点として渡したパス（範囲ありは `.`＝cwd の根）。"""
        return [p for _c, args, _g in self.calls for p in args]

    def cwds(self) -> list[str | None]:
        return [c for c, _a, _g in self.calls]


def _roots(root, der):
    return {str(root), str(der / "archives"), str(der / "rag"), str(der / "md")}


@pytest.mark.parametrize("sp", [["ZZsys/ZZapp/ZZsub"], ["ZZdocs"], ["ZZsys/ZZapp", "ZZdocs"],
                                ["ZZdocs/ZZoffice.docx"], ["ZZarc.zip"], ["ZZsys", "ZZsys/ZZapp"], ["ZZnope"],
                                ["ZZsp[br]"], ["ZZsp ace/ZZin"], ["ZZsp  dbl"], ["ZZfw\u3000sp"]],
                         ids="+".join)
@pytest.mark.parametrize("term", [NEEDLE, JP])
def test_rg_starts_only_from_roots_and_scope_goes_by_glob(env, monkeypatch, term, sp):
    root, der = env
    spy = _Spy(monkeypatch)
    _run(term, sp)
    assert spy.calls or sp == ["ZZnope"]
    assert set(spy.starts()) <= {"."}
    assert set(spy.cwds()) <= _roots(root, der)
    scoped = [g for _c, _a, gl in spy.calls for g in gl if g.startswith("/")]
    assert scoped, "範囲の条件が -g で渡っていない"


@pytest.mark.parametrize("sp", [["ZZsys/ ZZlead"], ["ZZnl\nx"], ["ZZtrail /ZZin"], ["ZZtab\tx"],
                                ["ZZsp ace/ZZin", "ZZnl\nx"]],
                         ids=lambda s: "+".join(s).replace("\n", "NL").replace("\t", "TAB"))
def test_scope_not_writable_as_glob_behaves_like_no_scope(env, monkeypatch, sp):
    """-g に書けない名前の範囲は、範囲なしと同じ起動（根を起点・範囲の条件なし）で、結果は全体を歩いて範囲で絞ったものと一致する。"""
    root, der = env
    spy = _Spy(monkeypatch)
    for term in (NEEDLE, JP):
        hits, stats, trunc = _run(term, sp)
        full, full_stats, full_trunc = _run(term, None)
        assert _shape(hits) == _shape([h for h in full if _in(h["doc_id"], sp)])
        assert sorted(trunc) == sorted(d for d in full_trunc if _in(d, sp))
    assert str(root) in spy.starts()
    assert set(spy.cwds()) == {None}
    assert not any(g.startswith("/") for _c, _a, gl in spy.calls for g in gl)


def test_unscoped_rg_still_walks_whole_roots(env, monkeypatch):
    root, der = env
    spy = _Spy(monkeypatch)
    _run(NEEDLE, None)
    assert str(root) in spy.starts() and str(der / "archives") in spy.starts()
    assert set(spy.cwds()) == {None}
    assert not any(g.startswith("/") for _c, _a, gl in spy.calls for g in gl)


def test_scoped_candidates_hold_only_files_in_scope(env):
    root, der = env
    for i in range(30):
        _put(root, f"ZZbulk/ZZf{i:02d}.txt", f"{NEEDLE} bulk {i}\n")
    roots_spec, _wd, _rag = G._prepare_roots(WORLD, None)
    cand = G._discover_candidates([NEEDLE], roots_spec, WORLD, cacheable=True, offset=0,
                                  budget=rg_search.Budget(None), scope_paths=["ZZsys/ZZapp/ZZsub"])
    assert {os.path.relpath(p, root) for p in cand if p.startswith(str(root))} == {
        "ZZsys/ZZapp/ZZsub/ZZdeep.txt", "ZZsys/ZZapp/ZZsub/ZZdeep2.java"}
    assert not any(p.startswith(str(der / "rag")) or p.startswith(str(der / "md")) for p in cand)


def test_glob_prefixes_escape_special_characters_and_refuse_unsafe_names():
    pre = rg_search.scope_prefixes(["ZZsp[br]/ZZin", "a{b,c}", "x*y?z", "!bang", "back\\slash", "日本語/sp ace",
                                    "dbl  sp", "fw\u3000sp", "a#b", "a,b"])
    assert pre == ["/ZZsp\\[br\\]/ZZin", "/a\\{b,c\\}", "/x\\*y\\?z", "/\\!bang", "/back\\\\slash",
                   "/日本語/sp ace", "/dbl  sp", "/fw\u3000sp", "/a#b", "/a,b"]
    assert rg_search.scope_prefixes(["a/../b", "./c", "d//e"]) == []
    assert rg_search.scope_prefixes(["ok", "a/ lead"]) is None
    assert rg_search.scope_prefixes(["ok", "a/trail "]) is None
    assert rg_search.scope_prefixes(["a/\u3000lead"]) is None
    assert rg_search.scope_prefixes(["nl\nx"]) is None
    assert rg_search.scope_prefixes(["tab\tx"]) is None
    assert rg_search.scope_prefixes(["bad\x00x"]) is None


def test_fold_case_makes_only_the_given_part_case_insensitive():
    assert rg_search.fold_case(".pdf") == ".[pP][dD][fF]"
    assert rg_search.fold_case("a.b[") == "[aA].[bB]\\["


# ===== 範囲ごとの木の署名・候補の保存・計画 =====

def test_candidate_pages_are_not_shared_between_scopes(env):
    first_a, _s, _t = _run(NEEDLE, ["ZZsys/ZZapp/ZZsub"], max_hits=1)
    page_b = G.grep_search(NEEDLE, world=WORLD, scope_paths=["ZZdocs"], max_hits=1, offset=1)
    full, _s, _t = _run(NEEDLE, None)
    kept_b = [h for h in full if _in(h["doc_id"], ["ZZdocs"])]
    assert first_a and _shape(page_b) == _shape(kept_b[1:2])
    page_a = G.grep_search(NEEDLE, world=WORLD, scope_paths=["ZZsys/ZZapp/ZZsub"], max_hits=1, offset=1)
    kept_a = [h for h in full if _in(h["doc_id"], ["ZZsys/ZZapp/ZZsub"])]
    assert _shape(page_a) == _shape(kept_a[1:2])


def test_candidate_cache_key_and_signature_differ_by_scope(env, monkeypatch):
    seen: list = []
    real = search_cache.pages_put
    monkeypatch.setattr(search_cache, "pages_put",
                        lambda world, key, sig, payload: (seen.append((key, sig, set(payload["cand"]))),
                                                          real(world, key, sig, payload))[1])
    _run(NEEDLE, ["ZZsys/ZZapp/ZZsub"])
    _run(NEEDLE, ["ZZdocs"])
    _run(NEEDLE, None)
    assert len({k for k, _sig, _c in seen}) == 3
    assert len({sig for _k, sig, _c in seen}) == 3
    a, b, _full = (c for _k, _sig, c in seen)
    assert a and b and not (a & b)


def test_tree_signature_of_scope_ignores_siblings_but_sees_scope_and_ancestors(env):
    root, _der = env
    sp = ["ZZsys/ZZapp/ZZsub"]
    base = search_cache.tree_signature([root], scope_paths=sp)
    assert base != search_cache.tree_signature([root])
    _put(root, "ZZdocs/ZZnew.txt", "x")
    _put(root, "ZZsys/ZZeuc/ZZnew.txt", "x")
    assert search_cache.tree_signature([root], scope_paths=sp) == base
    _put(root, "ZZsys/ZZapp/ZZsub/ZZnew.txt", "x")
    changed = search_cache.tree_signature([root], scope_paths=sp)
    assert changed != base
    _put(root, "ZZsys/ZZapp/ZZancestor_new.txt", "x")
    assert search_cache.tree_signature([root], scope_paths=sp) != changed


def test_tree_signature_of_scope_does_not_follow_symlinked_scope(env, tmp_path):
    root, _der = env
    outside = tmp_path / "outside"
    _put(outside, "ZZsub/ZZsecret.txt", "x")
    (root / "ZZsys" / "ZZout").symlink_to(outside, target_is_directory=True)
    sp = ["ZZsys/ZZout", "ZZsys/ZZout/ZZsub"]
    base = search_cache.tree_signature([root], scope_paths=sp)
    _put(outside, "ZZsub/ZZmore.txt", "x")
    _put(outside, "ZZnew/ZZmore.txt", "x")
    assert search_cache.tree_signature([root], scope_paths=sp) == base


def test_tree_signature_of_scope_does_not_follow_symlinked_ancestor(env, tmp_path):
    root, _der = env
    sp = ["ZZsys/ZZapp"]
    assert search_cache.tree_signature([root], scope_paths=sp) is not None
    outside = tmp_path / "outside"
    _put(outside, "ZZapp/ZZdeep/ZZx.txt", "x")
    (root / "ZZsys").rename(tmp_path / "moved")
    (root / "ZZsys").symlink_to(outside, target_is_directory=True)
    assert search_cache.tree_signature([root], scope_paths=sp) is None
    sub = ["ZZsys/ZZapp/ZZdeep"]
    assert search_cache.tree_signature([root], scope_paths=sub) is None
    walked: list = []
    real = os.scandir
    monkey = pytest.MonkeyPatch()
    monkey.setattr(os, "scandir", lambda p: (walked.append(p), real(p))[1])
    try:
        search_cache.tree_signature([root], scope_paths=sp)
    finally:
        monkey.undo()
    assert not walked


def test_prune_plan_is_made_per_scope(env):
    root, der = env
    seen: list = []
    real = rg_prune.plan

    def spy(world, orig_roots, der_roots, sig, budget=None, scope=None):
        out = real(world, orig_roots, der_roots, sig, budget, scope)
        seen.append(([str(r) for r in orig_roots], scope, sig, out))
        return out

    rg_prune.plan = spy
    try:
        _run(NEEDLE, ["ZZsys/ZZapp/ZZsub"])
        _run(NEEDLE, ["ZZdocs"])
        _run(NEEDLE, None)
    finally:
        rg_prune.plan = real
    assert seen[0][1] == ["ZZsys/ZZapp/ZZsub"] and seen[1][1] == ["ZZdocs"] and seen[2][1] is None
    assert all(str(root) in s[0] for s in seen)
    assert len({s[2] for s in seen}) == 3
    assert seen[0][3]["md_dirs"] is None and seen[2][3]["md_dirs"] is not None


def test_prune_skip_exts_checks_only_scope_and_its_path(env):
    root, _der = env
    _put(root, "ZZfar/ZZx.pdf/inner.txt", "x")
    exts = rg_prune._skip_exts([root], None, ["ZZsys"])
    assert ".pdf" in exts
    assert ".pdf" not in rg_prune._skip_exts([root], None, ["ZZfar"])
    assert ".pdf" not in rg_prune._skip_exts([root], None, ["ZZdocs/ZZfolder.pdf"])
    assert ".pdf" not in rg_prune._skip_exts([root], None, None)


def test_prune_plan_scope_prefix_is_case_sensitive_and_only_the_extension_is_folded(env, monkeypatch):
    root, _der = env
    _put(root, "ZZcase/ZZx.PdF/inner.txt", "x")
    seen: list[list[str]] = []
    real = subprocess.run

    def run(cmd, *a, **k):
        if "--files" in cmd:
            seen.append(cmd)
        return real(cmd, *a, **k)

    monkeypatch.setattr(subprocess, "run", run)
    assert ".pdf" not in rg_prune._skip_exts([root], None, ["ZZcase"])
    (cmd,) = seen
    assert "--iglob" not in cmd
    assert "/ZZcase/**/*.[pP][dD][fF]/**" in cmd


def test_prune_plan_for_scope_does_not_list_a_sibling_that_differs_only_in_case(tmp_path):
    (tmp_path / "Foo").mkdir()
    if (tmp_path / "foo").exists():
        pytest.skip("大文字小文字を区別するファイルシステムが必要")
    _put(tmp_path, "foo/x.pdf/y.txt", "x")
    assert ".pdf" in rg_prune._skip_exts([tmp_path], None, ["Foo"])
    assert ".pdf" not in rg_prune._skip_exts([tmp_path], None, ["foo"])


def test_scope_covering_nothing_changes_nothing_when_scope_is_empty(env):
    a, sa, ta = _run(NEEDLE, [])
    b, sb, tb = _run(NEEDLE, None)
    assert _shape(a) == _shape(b) and sa == sb and ta == tb


# ===== symlink の差し替え =====

def _swap_dir_for_outside_link(root, tmp_path, rel, outside):
    def swap():
        a = root / rel
        os.rename(a, tmp_path / (a.name + ".moved"))
        a.symlink_to(outside, target_is_directory=True)
    return swap


@pytest.mark.parametrize("sp,swapped,leak", [
    (["ZZsys/ZZapp"], "ZZsys/ZZapp", "ZZsecret.txt"),
    (["ZZsys/ZZapp/ZZsub"], "ZZsys/ZZapp", "ZZsub/ZZsecret.txt"),
    (["ZZsys/ZZapp/ZZsub"], "ZZsys/ZZapp/ZZsub", "ZZsecret.txt"),
    (["ZZsys"], "ZZsys", "ZZapp/ZZsecret.txt"),
], ids=["scope-dir", "parent", "scope-dir-deep", "scope-top"])
def test_swapped_scope_dir_is_not_followed_even_when_swapped_just_before_rg_starts(
        env, monkeypatch, tmp_path, sp, swapped, leak):
    root, _der = env
    outside = tmp_path / "outside"
    _put(outside, leak, f"{NEEDLE} secret\n")
    spy = _Spy(monkeypatch, before=_swap_dir_for_outside_link(root, tmp_path, swapped, outside))
    seen: list = []
    real_discover = G._discover_candidates

    def tap(*a, **kw):
        out = real_discover(*a, **kw)
        seen.append(out)
        return out

    monkeypatch.setattr(G, "_discover_candidates", tap)
    hits = G.grep_search(NEEDLE, world=WORLD, max_hits=1000, scope_paths=sp)
    assert (root / swapped).is_symlink()
    assert set(spy.starts()) <= {"."} and set(spy.cwds()) <= _roots(root, _der)
    assert seen and not any("secret" in p.lower() for c in seen for p in c)
    assert all("secret" not in h["text"] for h in hits)


def test_symlinked_scope_dir_in_place_finds_nothing_outside(env, tmp_path):
    root, _der = env
    outside = tmp_path / "outside"
    _put(outside, "ZZsecret.txt", f"{NEEDLE} secret\n")
    (root / "ZZsys" / "ZZout").symlink_to(outside, target_is_directory=True)
    hits, _s, _t = _run(NEEDLE, ["ZZsys/ZZout"])
    assert hits == []


def _patch_cache_os(monkeypatch, **fns):
    import types
    proxy = types.SimpleNamespace(**{k: getattr(os, k) for k in dir(os)})
    for k, f in fns.items():
        setattr(proxy, k, f)
    proxy.supports_dir_fd = set(os.supports_dir_fd) | {fns["open"]} if "open" in fns else os.supports_dir_fd
    proxy.supports_fd = set(os.supports_fd) | {fns["scandir"]} if "scandir" in fns else os.supports_fd
    monkeypatch.setattr(search_cache, "os", proxy)


@pytest.mark.parametrize("boundary", ["open", "scandir"])
def test_tree_signature_of_scope_is_none_when_subtree_walk_fails(env, monkeypatch, boundary):
    root, _der = env
    sp = ["ZZsys"]
    assert search_cache.tree_signature([root], scope_paths=sp) is not None
    if boundary == "open":
        real = os.open

        def _open(path, flags, *a, **kw):
            if path == "ZZapp" and kw.get("dir_fd") is not None:
                raise PermissionError(13, "一時的に開けない")
            return real(path, flags, *a, **kw)
        _patch_cache_os(monkeypatch, open=_open)
    else:
        real_scan = os.scandir
        calls = []

        def _scan(p):
            calls.append(p)
            if len(calls) == 2:
                raise OSError(5, "一時的に列挙できない")
            return real_scan(p)
        _patch_cache_os(monkeypatch, scandir=_scan)
    assert search_cache.tree_signature([root], scope_paths=sp) is None


def test_tree_signature_of_scope_is_none_when_subdir_vanishes_after_listing(env, monkeypatch):
    root, _der = env
    real = os.open

    def _open(path, flags, *a, **kw):
        if path == "ZZapp" and kw.get("dir_fd") is not None:
            raise FileNotFoundError(2, "消えた")
        return real(path, flags, *a, **kw)
    _patch_cache_os(monkeypatch, open=_open)
    assert search_cache.tree_signature([root], scope_paths=["ZZsys"]) is None


def test_tree_signature_of_scope_skips_only_a_scope_that_does_not_exist(env):
    root, _der = env
    assert search_cache.tree_signature([root], scope_paths=["ZZsys/ZZnope"]) is not None
    assert search_cache.tree_signature([root], scope_paths=["ZZnope/ZZdeeper"]) is not None


def test_candidate_cache_is_not_used_when_subdir_vanishes_after_listing(env, monkeypatch):
    root, _der = env
    sp = ["ZZsys"]
    G.grep_search(NEEDLE, world=WORLD, max_hits=1, scope_paths=sp)
    real = os.open

    def _open(path, flags, *a, **kw):
        if path == "ZZapp" and kw.get("dir_fd") is not None:
            raise FileNotFoundError(2, "消えた")
        return real(path, flags, *a, **kw)
    _patch_cache_os(monkeypatch, open=_open)
    monkeypatch.setattr(search_cache, "pages_get",
                        lambda *a, **k: pytest.fail("署名が作れないのに候補の保存を読んだ"))
    G.grep_search(NEEDLE, world=WORLD, max_hits=1, scope_paths=sp, offset=1)
