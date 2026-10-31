"""回答の本文に名前が出る資料の照らし合わせ（`turn_finish.assemble_result`）を、偽 codex の 1 ターンで確かめる。

本文で拾うのは、バッククォート・「」・『』で囲まれた名前と、範囲にある資料のパスとそのまま一致するものだけ。
拾った資料は「本文で名前が出た資料」（`mentioned_only`）として出典に足し、通らないものは「確認できなかった資料」に出す。
"""
from __future__ import annotations

import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

import test_codex_workspace_authoring as H  # noqa: E402
from sherpa import documents  # noqa: E402
from sherpa.providers.codex import citations as C  # noqa: E402

_SCOPE = {"world": "v1", "scope_paths": ["4期/02_設計"], "source": "all"}
_DOC2_NAME = "消費税法.md"            # 4期/01_標準/消費税法.md（上の範囲の外）
_DOC_NAME = "税計算仕様書.md"          # H._DOC の名前（範囲の中で一意）


def _turn(tmp_path, monkeypatch, answer, uid, scope=True):
    ctx_kw = {"scope_meta": _SCOPE} if scope else {}
    H._install_codex(tmp_path, monkeypatch, H._PY + H._msg(answer), sandbox=None)
    events = list(H.A.CodexProvider().run(H._ctx(uid, H._make_sources, "qa", **ctx_kw)))
    return H._result_env(events)


def test_doc_named_only_in_the_body_is_added_as_mentioned_only_and_not_counted_as_grounded(tmp_path, monkeypatch):
    answer = f"消費税の計算は {H._DOC} に書かれています。手数料は「手数料計算仕様書.md」にあります。"
    env = _turn(tmp_path, monkeypatch, answer, "mention-add-u1")
    rows = {r["path"]: r for r in env["referenced_docs"]}
    assert set(rows) == {H._DOC, "4期/02_設計/01_基本設計/手数料計算仕様書.md"}
    assert all(r["mentioned_only"] is True for r in rows.values())
    assert {s["doc_id"] for s in env["sources"]} == set(rows)
    assert "sources_verified" not in env                      # 根拠の区分には数えない
    assert env["body"] == answer and "sources_unverified" not in env


def test_listed_docs_stay_plain_and_body_only_docs_follow_with_the_mark(tmp_path, monkeypatch):
    answer = f"{H._DOC} を見ました。{H._DOC2} も関係します。\n\n参照した資料:\n- {H._DOC}\n"
    env = _turn(tmp_path, monkeypatch, answer, "mention-mix-u1", scope=False)
    assert [(r["path"], r.get("mentioned_only", False)) for r in env["referenced_docs"]] == [
        (H._DOC, False), (H._DOC2, True)]
    assert env["sources_verified"] == [H._DOC]
    assert [s["doc_id"] for s in env["sources"]][:2] == [H._DOC, H._DOC2]
    assert "参照した資料" not in env["body"]


def test_body_mentions_that_fail_are_listed_with_reasons_and_secrets_only_counted(tmp_path, monkeypatch):
    answer = (f"`{_DOC2_NAME}` と `存在しない仕様.md` と `credentials.xlsx` にも触れています。{H._DOC} は範囲の中です。"
              "例えば e.g. や Node.js のような語は資料ではありません。")
    env = _turn(tmp_path, monkeypatch, answer, "mention-fail-u1")
    assert [r["path"] for r in env["referenced_docs"]] == [H._DOC] and env["referenced_docs"][0]["mentioned_only"]
    assert {(r["path"], r["reason"]) for r in env["sources_unverified"]} == {
        (_DOC2_NAME, C.REASON_UNREADABLE), ("存在しない仕様.md", C.REASON_UNREADABLE)}
    assert env["sources_unverified_hidden"] == 1 and "credentials" not in str(env["sources_unverified"])
    assert not any(n["kind"] == "sources_unverified" for n in env["notices"])   # 注記は「参照した資料」の行の分だけ


def test_body_path_outside_the_scope_is_reported_as_out_of_scope(tmp_path, monkeypatch):
    env = _turn(tmp_path, monkeypatch, f"`{H._DOC2}` も関係します。", "mention-scope-u1")
    assert env["referenced_docs"] == [] and env["sources_unverified"] == [
        {"path": H._DOC2, "reason": C.REASON_OUT_OF_SCOPE}]


def test_duplicated_file_name_in_scope_is_not_added_and_says_why(tmp_path, monkeypatch):
    real = documents.known_rels
    monkeypatch.setattr(documents, "known_rels",
                        lambda world, **kw: set(real(world, **kw)) | {f"4期/02_設計/別/{_DOC_NAME}"})
    env = _turn(tmp_path, monkeypatch, f"`{_DOC_NAME}` に書かれています。", "mention-dup-u1")
    assert env["referenced_docs"] == [] and not env.get("sources")
    assert env["sources_unverified"] == [{"path": _DOC_NAME, "reason": C.REASON_AMBIGUOUS}]


def test_file_name_that_a_listed_reference_already_resolved_is_not_reported_again(tmp_path, monkeypatch):
    real = documents.known_rels
    monkeypatch.setattr(documents, "known_rels",
                        lambda world, **kw: set(real(world, **kw)) | {f"4期/02_設計/別/{_DOC_NAME}"})
    answer = f"`{_DOC_NAME}` に書かれています。\n\n参照した資料:\n- {H._DOC}\n"
    env = _turn(tmp_path, monkeypatch, answer, "mention-listed-u1")
    assert [r["path"] for r in env["referenced_docs"]] == [H._DOC] and "sources_unverified" not in env


def test_mentions_are_not_collected_for_authoring_turns(tmp_path, monkeypatch):
    H._install_codex(tmp_path, monkeypatch, H._PY + H._msg(f"{H._DOC} を元に作ります。"), sandbox=None)
    events = list(H.A.CodexProvider().run(H._ctx("mention-author-u1", H._make_sources, "author")))
    env = H._result_env(events)
    assert "referenced_docs" not in env and "sources_unverified" not in env


def test_only_quoted_names_and_known_paths_are_taken():
    paths = frozenset({"a/b.md", "x/原価.xlsx", "4期/設計/設計書.xlsx"})
    text = ("`c.cbl` と「仕様書(旧).xlsx」と『売上 原価.xlsx』、a/b.md と 裸の名前.md と Node.js、"
            "`https://x.example/y.html` と `src/a.cbl:12-20`、`x = 1` と d（拡張子なし）、a/b.md")
    assert C.extract_doc_tokens(text, paths=paths) == [
        "c.cbl", "仕様書(旧).xlsx", "売上 原価.xlsx", "a/b.md", "src/a.cbl"]
    many = " ".join(f"`f{i}.md`" for i in range(80))
    assert len(C.extract_doc_tokens(many)) == C.MENTION_MAX


def test_known_paths_outside_quotes_need_a_clear_boundary():
    paths = frozenset({"x/原価.xlsx", "4期/設計/設計書.xlsx"})
    f = lambda t: C.extract_doc_tokens(t, paths=paths)
    assert f("資料は4期/設計/設計書.xlsxに書かれています") == ["4期/設計/設計書.xlsx"]   # 日本語に続けて書いても拾う
    assert f("https://h/x/原価.xlsx と y/x/原価.xlsx と x/原価.xlsx.bak") == []        # URL・長いパス・長い名前の一部は拾わない
    assert f("設計書.xlsx と 原価.xlsx") == []                                           # 囲まれていない裸の名前は拾わない


def test_body_path_outside_the_layer_is_reported_and_not_added(tmp_path, monkeypatch):
    src = "4期/03_開発/01_ソース/BILLGEN.cbl"
    H._install_codex(tmp_path, monkeypatch, H._PY + H._msg(f"`{H._DOC}` と {src} と BILLGEN.cbl に書かれています。"),
                     sandbox=None)
    sm = {"world": "v1", "scope_paths": [], "source": "all", "layer": "code"}
    env = H._result_env(list(H.A.CodexProvider().run(H._ctx("mention-layer-u1", H._make_sources, "qa", scope_meta=sm))))
    assert [r["path"] for r in env["referenced_docs"]] == [src]
    assert env["sources_unverified"] == [{"path": H._DOC, "reason": C.REASON_OUT_OF_LAYER}]
    assert H._DOC not in {s["doc_id"] for s in env["sources"]}


def test_long_known_extensions_are_extracted_whole():
    toks = C.extract_doc_tokens("`README.markdown` と `conf/app.properties` と `lib/CUSTOMER.copybook` と `a.verylongextension1`")
    assert toks == ["README.markdown", "conf/app.properties", "lib/CUSTOMER.copybook"]
    assert C.plausible_doc_tokens(["README.markdown", "x.properties"], {"a.markdown": [], "b.properties": []}) == [
        "README.markdown", "x.properties"]


def test_screen_by_layer_checks_docs_directly_under_the_world_root_too():
    kept, out = C.screen_by_layer(["ROOT.md", "main.cbl", "ROOT2.md"], "w", "code")
    assert kept == ["main.cbl"]
    assert out == [{"path": "ROOT.md", "reason": C.REASON_OUT_OF_LAYER},
                   {"path": "ROOT2.md", "reason": C.REASON_OUT_OF_LAYER}]
    assert C.screen_by_layer(["ROOT.md"], "w", "both") == (["ROOT.md"], [])


def test_body_path_with_an_extension_not_seen_in_scope_still_reaches_the_check(tmp_path, monkeypatch):
    assert C.plausible_doc_tokens(["outside/README.markdown", "README.markdown"], {}) == ["outside/README.markdown"]
    env = _turn(tmp_path, monkeypatch, "`outside/README.markdown` にも書かれています。", "mention-ext-u1")
    assert env["referenced_docs"] == [] and [r["path"] for r in env["sources_unverified"]] == ["outside/README.markdown"]


def test_doc_name_quoted_next_to_a_particle_reaches_the_sources(tmp_path, monkeypatch):
    env = _turn(tmp_path, monkeypatch, f"消費税の計算は「{_DOC_NAME}」に書かれています。", "mention-particle-u1")
    assert [(r["path"], r["mentioned_only"]) for r in env["referenced_docs"]] == [(H._DOC, True)]


def test_body_names_beyond_the_check_limit_are_counted_as_unconfirmed(tmp_path, monkeypatch):
    names = [f"存在しない資料{i:02d}.md" for i in range(C.MENTION_MAX + 10)]
    env = _turn(tmp_path, monkeypatch, "関係する資料: " + "、".join(f"`{n}`" for n in names) + "。", "mention-over-u1")
    shown = env.get("sources_unverified") or []
    assert len(shown) == 20 and shown[0]["path"] == names[0]
    assert env["sources_unverified_more"] == C.MENTION_MAX + 10 - 20   # 確かめなかった分も件数に入る


def test_listed_reference_that_failed_is_not_counted_again_from_the_body(tmp_path, monkeypatch):
    names = [f"存在しない資料{i:02d}.md" for i in range(C.MENTION_MAX + 1)]
    answer = "関係する資料: " + "、".join(f"`{n}`" for n in names) + "。\n\n参照した資料:\n- " + names[-1] + "\n"
    env = _turn(tmp_path, monkeypatch, answer, "mention-dup-count-u1")
    shown = env.get("sources_unverified") or []
    assert len(shown) + env.get("sources_unverified_more", 0) == len(names)   # 同じ資料を二度数えない
    assert len({r["path"] for r in shown}) == len(shown)


def test_secret_listed_and_named_in_the_body_is_counted_once(tmp_path, monkeypatch):
    answer = "設定は `credentials.xlsx` にあります。\n\n参照した資料:\n- credentials.xlsx\n"
    env = _turn(tmp_path, monkeypatch, answer, "mention-secret-dup-u1")
    assert env["sources_unverified_hidden"] == 1


def test_line_number_lists_long_path_tails_and_out_of_layer_paths():
    assert C.extract_doc_tokens("`missing.md:12, 16` と「b.md:3-4,9」") == ["missing.md", "b.md"]
    paths = frozenset({"x/a.md"})
    assert C.extract_doc_tokens("x/a.md/more と x/a.md\\\\more と x/a.md@rev と x/a.md:12", paths=paths) == ["x/a.md"]
    assert C.extract_doc_tokens("x/a.md/more", paths=paths) == []


def test_unquoted_path_outside_the_layer_is_reported(tmp_path, monkeypatch):
    H._install_codex(tmp_path, monkeypatch, H._PY + H._msg(f"{H._DOC} に書かれています。"), sandbox=None)
    sm = {"world": "v1", "scope_paths": [], "source": "all", "layer": "code"}
    env = H._result_env(list(H.A.CodexProvider().run(H._ctx("mention-layer2-u1", H._make_sources, "qa", scope_meta=sm))))
    assert env["sources_unverified"] == [{"path": H._DOC, "reason": C.REASON_OUT_OF_LAYER}]


def test_paths_in_url_query_values_are_not_taken():
    paths = frozenset({"x/a.md"})
    assert C.extract_doc_tokens("https://h/?file=x/a.md と ?a=1&f=x/a.md と #x/a.md", paths=paths) == []


def test_reference_heading_inside_a_code_block_is_not_the_reference_block():
    body = "例です。\n```text\n参照した資料:\n- missing.md\n```\n続きです。\n\n参照した資料:\n- a/b.md\n"
    rest, groups = C.parse_referenced_doc_lines(body)
    assert rest == "例です。\n```text\n参照した資料:\n- missing.md\n```\n続きです。" and len(groups) == 1


def test_full_width_letters_next_to_a_path_make_it_part_of_a_longer_name():
    assert C.extract_doc_tokens("Ａx/a.md と x/a.mdＢ", paths=frozenset({"x/a.md"})) == []


def test_names_and_reference_headings_inside_code_blocks_are_left_alone():
    paths = frozenset({"src/a.cbl"})
    text = "前文 `b.cbl`。\n```text\n`a.cbl` と src/a.cbl\n```\n\n    参照した資料:\n    - src/a.cbl\n\n> ```\n> src/a.cbl\n> ```\n後 src/a.cbl"
    assert C.extract_doc_tokens(text, paths=paths) == ["b.cbl", "src/a.cbl"]
    rest, groups = C.parse_referenced_doc_lines("説明。\n\n    参照した資料:\n    - missing.md\n\n続き。")
    assert groups == [] and "    参照した資料:" in rest


def test_query_or_fragment_after_a_path_is_not_taken():
    paths = frozenset({"x/a.md"})
    assert C.extract_doc_tokens("x/a.md?rev=1 と x/a.md#L2 と x/a.md&download=1 と x/a.md=foo", paths=paths) == []


def test_file_name_only_mention_with_a_same_named_doc_at_the_root_is_not_attached(tmp_path, monkeypatch):
    real = documents.known_rels
    monkeypatch.setattr(documents, "known_rels", lambda world, **kw: set(real(world, **kw)) | {_DOC_NAME})
    env = _turn(tmp_path, monkeypatch, f"`{_DOC_NAME}` に書かれています。", "mention-root-dup-u1", scope=False)
    assert env["referenced_docs"] == [] and env["sources_unverified"] == [{"path": _DOC_NAME, "reason": C.REASON_AMBIGUOUS}]


def test_fence_opened_on_a_list_marker_line_is_a_code_block():
    lines = ["- ```md", "[a]", "参照した資料:", "- sample.md", "  ```", "後"]
    assert C.code_block_lines(lines) == [True, True, True, True, True, False]
