"""個人ファイルの道具（`personal_search`・`personal_read`）が本人の一覧のファイルだけを開き、
個人ファイルを使うターンだけに出て、読んだファイルが出典の個人の枠にだけ出ることを確かめる。
設計: docs/06-認証と個人領域.md §3、docs/design/interfaces.md「MCP の道具」
"""
from __future__ import annotations

import json
import os
import time
import tomllib
from pathlib import Path

import pytest

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

import test_codex_workspace_authoring as H  # noqa: E402
from sherpa import mcp_server as M  # noqa: E402
from sherpa import store  # noqa: E402
from sherpa.parts.read import personal  # noqa: E402

_LISTED = ["メモ.txt", "表.xlsx", "文書.docx", "発表.pptx", "資料.pdf"]


def _call(name: str, args: dict) -> tuple[dict, bool]:
    resp = M.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
    return json.loads(resp["result"]["content"][0]["text"]), resp["result"]["isError"]


def _tool_names() -> set:
    return {t["name"] for t in M.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]}


def _text_pdf(path: Path, text: str) -> None:
    from test_ingest_walk_once import _text_pdf as make
    make(path, text)


def _make_office(files: Path) -> None:
    import docx
    import openpyxl
    import pptx
    wb = openpyxl.Workbook()
    wb.active.title = "集計"
    wb.active["C4"] = "合言葉は架空の果物"
    wb.save(files / "表.xlsx")
    doc = docx.Document()
    doc.add_paragraph("はじめに")
    doc.add_paragraph("架空の果物の段落")
    doc.save(files / "文書.docx")
    prs = pptx.Presentation()
    prs.slides.add_slide(prs.slide_layouts[1]).shapes.title.text = "表紙"
    prs.slides.add_slide(prs.slide_layouts[1]).shapes.title.text = "架空の果物のスライド"
    prs.save(files / "発表.pptx")
    _text_pdf(files / "資料.pdf", "fictional fruit page")


@pytest.fixture()
def personal_turn(tmp_path, monkeypatch):
    """本人（u1）の files/ と、他の利用者（u2）の files/、開いてよい一覧を作り、MCP の env に置く。"""
    root = Path(os.path.realpath(tmp_path))
    files = root / "users" / "u1" / "workspace" / "files"
    files.mkdir(parents=True)
    (files / "メモ.txt").write_text("一行目\n架空の果物の話\n三行目\n", encoding="utf-8")
    (files / ".env").write_text("TOKEN=x\n")
    (files / "外.txt").symlink_to(root / "outside.txt")
    (root / "outside.txt").write_text("架空の果物（外）\n", encoding="utf-8")
    (files / "一覧外.txt").write_text("架空の果物（一覧外）\n", encoding="utf-8")
    other = root / "users" / "u2" / "workspace" / "files"
    other.mkdir(parents=True)
    (other / "他人.txt").write_text("架空の果物（他人）\n", encoding="utf-8")
    index = root / "codex_home" / ".personal_index.json"
    index.parent.mkdir()
    index.write_text(json.dumps({"files": _LISTED + [".env", "外.txt", "../../u2/workspace/files/他人.txt"]}),
                     encoding="utf-8")
    monkeypatch.setenv("SHERPA_MCP_PERSONAL_INDEX", str(index))
    monkeypatch.setenv("SHERPA_MCP_PERSONAL_DIR", str(files))
    monkeypatch.delenv("SHERPA_MCP_TOOLSET", raising=False)
    M._seen_tool_calls.clear()
    return files


@pytest.mark.parametrize("doc_id", [
    "personal:一覧外.txt",                             # 一覧に無い
    "personal:外.txt",                                 # symlink
    "personal:.env",                                   # 秘匿名
    "personal:../../u2/workspace/files/他人.txt",      # 他の利用者・`..`
    "personal:/etc/hosts",                             # 絶対パス
    "メモ.txt",                                         # personal: の無い id
])
def test_personal_read_opens_only_listed_own_files(personal_turn, doc_id):
    body, is_error = _call("personal_read", {"id": doc_id})
    assert is_error and body.get("error_code") == "personal_not_allowed"
    assert "架空の果物" not in json.dumps(body, ensure_ascii=False)


def test_personal_search_skips_files_it_must_not_open(personal_turn):
    body, is_error = _call("personal_search", {"terms": ["架空の果物"]})
    assert not is_error
    assert {h["id"] for h in body["hits"]} == {"personal:メモ.txt"}
    assert body["unreadable_files"] == 5                # 一覧にあって開けないもの（実体の無い 4 件・symlink）は数だけ
    assert "外" not in json.dumps(body["hits"], ensure_ascii=False)


def test_personal_tools_are_not_offered_or_callable_outside_personal_turns(personal_turn, monkeypatch):
    assert {"personal_search", "personal_read"} <= _tool_names()
    monkeypatch.setenv("SHERPA_MCP_TOOLSET", "plain")
    assert not {"personal_search", "personal_read"} & _tool_names()
    body, is_error = _call("personal_read", {"id": "personal:メモ.txt"})
    assert is_error and body == {"error": "unknown tool: personal_read"}
    monkeypatch.delenv("SHERPA_MCP_TOOLSET")
    monkeypatch.delenv("SHERPA_MCP_PERSONAL_INDEX")
    assert not {"personal_search", "personal_read"} & _tool_names()
    body, is_error = _call("personal_search", {"terms": ["架空の果物"]})
    assert is_error and body == {"error": "unknown tool: personal_search"}


def test_personal_tools_search_and_read_office_and_pdf(personal_turn):
    _make_office(personal_turn)
    body, _ = _call("personal_search", {"terms": ["架空の果物", "FICTIONAL"]})
    locs = {h["id"]: h["loc"] for h in body["hits"]}
    assert locs["personal:表.xlsx"] == {"sheet": "集計", "cell": "C4"}
    assert locs["personal:文書.docx"] == {"paragraph": 1}
    assert locs["personal:発表.pptx"] == {"slide": 2}
    assert locs["personal:資料.pdf"] == {"page": 1}
    xlsx, _ = _call("personal_read", {"id": "personal:表.xlsx", "sheet": "集計", "range": "C4:C4"})
    assert xlsx["rows"] == [["合言葉は架空の果物"]] and xlsx["id"] == "personal:表.xlsx"
    word, _ = _call("personal_read", {"id": "personal:文書.docx"})
    assert [p["text"] for p in word["paragraphs"]] == ["はじめに", "架空の果物の段落"]
    slides, _ = _call("personal_read", {"id": "personal:発表.pptx", "pages": "2"})
    assert slides["slides"][0]["texts"] == ["架空の果物のスライド"]
    pdf, _ = _call("personal_read", {"id": "personal:資料.pdf"})
    assert pdf["pages"][0]["text"] == "fictional fruit page"


def _many_sheets(files: Path, n: int = 5) -> personal.PersonalContext:
    import openpyxl
    wb = openpyxl.Workbook()
    for i in range(n):
        ws = wb.active if i == 0 else wb.create_sheet()
        ws.title = f"S{i}"
        ws["A1"] = f"架空の果物 {i}"
    wb.save(files / "多.xlsx")
    return personal.PersonalContext(files, frozenset({"多.xlsx"}))


def test_personal_search_checks_the_office_zip_once_per_file(personal_turn, monkeypatch):
    """多シートの xlsx を探すとき、zip の展開の確かめはファイルごとに 1 回だけ。"""
    from sherpa import doc_readers
    ctx = _many_sheets(personal_turn)
    calls = []
    real = doc_readers._zip_actual_size_error
    monkeypatch.setattr(doc_readers, "_zip_actual_size_error", lambda f: calls.append(1) or real(f))
    out = personal.search_files(ctx, ["架空の果物"], deadline=time.monotonic() + 60, max_bytes=1 << 20)
    assert len(out["hits"]) == 5 and len(calls) == 1


def test_personal_search_stops_reading_sheets_after_the_deadline(personal_turn, monkeypatch):
    """検索全体の期限を過ぎたら、残りのシートを読まずに打ち切り、そのファイルを一部だけ見たものに入れる。"""
    from sherpa import doc_readers
    ctx = _many_sheets(personal_turn)
    deadline = time.monotonic() + 60
    reads = []
    real = doc_readers.xlsx_range

    def slow(*a, **kw):
        reads.append(1)
        monkeypatch.setattr(time, "monotonic", lambda: deadline + 1)
        return real(*a, **kw)
    monkeypatch.setattr(doc_readers, "xlsx_range", slow)
    out = personal.search_files(ctx, ["架空の果物"], deadline=deadline, max_bytes=1 << 20)
    assert len(reads) == 1
    assert out["deadline_exceeded"] and out["partial_files"] == ["personal:多.xlsx"]


def test_personal_search_reports_the_file_cut_by_the_deadline_as_unsearched(personal_turn, monkeypatch):
    """期限で途中までしか見なかったファイルも、画面の検索の「探せなかった」に入れる。"""
    from sherpa import doc_readers
    ctx = _many_sheets(personal_turn)
    deadline = time.monotonic() + 60
    real = doc_readers.xlsx_range

    def slow(*a, **kw):
        monkeypatch.setattr(time, "monotonic", lambda: deadline + 1)
        return real(*a, **kw)
    monkeypatch.setattr(doc_readers, "xlsx_range", slow)
    skipped: list[str] = []
    personal.search_files(ctx, ["架空の果物"], deadline=deadline, max_bytes=1 << 20, skipped=skipped)
    assert skipped == ["多.xlsx"]


def test_personal_search_reports_the_file_cut_by_the_sheet_limit_as_unsearched(personal_turn):
    """シートの上限で途中までしか見なかった xlsx も、画面の検索の「探せなかった」に入れる。"""
    ctx = _many_sheets(personal_turn, n=21)
    skipped: list[str] = []
    out = personal.search_files(ctx, ["架空の果物"], deadline=time.monotonic() + 60, max_bytes=1 << 20, skipped=skipped)
    assert out["partial_files"] == ["personal:多.xlsx"] and skipped == ["多.xlsx"]


def _personal_codex(log: Path, answer: str) -> str:
    """呼ばれた時の道具のサーバーの env と一覧を `log` に写し、個人ファイルを読んだ道具の呼び出しと回答を出す偽 codex。"""
    result = {"content": [{"type": "text", "text": json.dumps(
        {"id": "personal:メモ.txt", "start_line": 1, "end_line": 1, "total_lines": 1, "text": "1: x"})}], "isError": False}
    return (H._PY + "import json, os, pathlib, tomllib\n"
            f"cfg = tomllib.loads((pathlib.Path(os.environ['CODEX_HOME']) / 'config.toml').read_text())\n"
            "env = cfg['mcp_servers']['sherpa']['env']\n"
            "idx = env.get('SHERPA_MCP_PERSONAL_INDEX')\n"
            f"pathlib.Path(r'{log}').write_text(json.dumps({{'env': env, 'index': json.loads(pathlib.Path(idx).read_text()) if idx else None}}))\n"
            + H._mcp_call("personal_read", {"id": "personal:メモ.txt"}, result=result)
            + H._msg(answer))


def test_codex_turn_hands_own_files_and_shows_reads_only_in_the_personal_box(tmp_path, monkeypatch):
    files = tmp_path / "users" / "pu1" / "workspace" / "files"
    files.mkdir(parents=True)
    for name in ("メモ.txt", "鍵.pem"):
        (files / name).write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(store, "live_workspace_rel_paths", lambda uid: {"メモ.txt", "鍵.pem"} if uid == "pu1" else set())
    log = tmp_path / "seen.json"
    answer = f"個人のメモと {H._DOC} を見ました。`personal:メモ.txt` にあります。\n\n参照した資料:\n- {H._DOC}\n- personal:メモ.txt\n"
    H._install_codex(tmp_path, monkeypatch, _personal_codex(log, answer))
    events = list(H.A.CodexProvider().run(H._ctx("pu1", H._make_sources, "qa", personal=True)))
    env = H._result_env(events)
    seen = json.loads(log.read_text())
    assert seen["index"] == {"files": ["メモ.txt"]}                                       # 秘匿名は一覧に入れない
    assert seen["env"]["SHERPA_MCP_PERSONAL_DIR"] == os.path.realpath(files)
    assert seen["env"]["SHERPA_MCP_COVERAGE_NO_QUERY"] == "1"
    assert env["personal_sources"] == [{"doc_id": "メモ.txt", "quote": "", "source": "個人ファイル内ヒット"}]
    assert all("download_url" not in s for s in env["personal_sources"])
    assert "メモ.txt" not in json.dumps(env.get("sources_unverified") or [], ensure_ascii=False)
    assert all("メモ" not in s["doc_id"] for s in env["sources"])
    assert not (Path(seen["env"]["SHERPA_MCP_PERSONAL_INDEX"])).exists()                  # ターンの終わりに消す


def test_codex_turn_without_personal_toggle_hands_no_personal_tools(tmp_path, monkeypatch):
    files = tmp_path / "users" / "pu2" / "workspace" / "files"
    files.mkdir(parents=True)
    (files / "メモ.txt").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(store, "live_workspace_rel_paths", lambda uid: {"メモ.txt"})
    log = tmp_path / "seen.json"
    H._install_codex(tmp_path, monkeypatch, _personal_codex(log, "答えです。"))
    env = H._result_env(list(H.A.CodexProvider().run(H._ctx("pu2", H._make_sources, "qa"))))
    seen = json.loads(log.read_text())
    assert seen["index"] is None and "SHERPA_MCP_PERSONAL_DIR" not in seen["env"]
    assert "personal_sources" not in env                                                    # 一覧に無いターンの読取は数えない


@pytest.mark.parametrize("shared_read", [False, True])
def test_personal_file_with_a_shared_name_is_not_cited_as_the_shared_doc(tmp_path, monkeypatch, shared_read):
    """共有資料と同じ名前の個人ファイルを読んで素の名前で書いても、共有の出典にしない（同じターンに共有資料も読んだときだけ共有の出典に入れる）。"""
    name = H._DOC.rsplit("/", 1)[-1]
    files = tmp_path / "users" / "pu3" / "workspace" / "files"
    files.mkdir(parents=True)
    (files / name).write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(store, "live_workspace_rel_paths", lambda uid: {name} if uid == "pu3" else set())
    result = {"content": [{"type": "text", "text": json.dumps(
        {"id": f"personal:{name}", "start_line": 1, "end_line": 1, "total_lines": 1, "text": "1: x"})}], "isError": False}
    answer = f"`{name}` にあります。\n\n参照した資料:\n- {name}\n"
    body = (H._PY + H._mcp_call("personal_read", {"id": f"personal:{name}"}, result=result)
            + (H._mcp_call("read_doc", {"doc_id": H._DOC}, id="t2") if shared_read else "") + H._msg(answer))
    H._install_codex(tmp_path, monkeypatch, body)
    env = H._result_env(list(H.A.CodexProvider().run(H._ctx("pu3", H._make_sources, "qa", personal=True))))
    assert env["personal_sources"] == [{"doc_id": name, "quote": "", "source": "個人ファイル内ヒット"}]
    assert (H._DOC in {s["doc_id"] for s in env["sources"]}) is shared_read
    assert name not in json.dumps(env.get("sources_unverified") or [], ensure_ascii=False)


def _read_personal_and_answer(tmp_path, monkeypatch, uid: str, name: str, answer: str) -> dict:
    """個人ファイル `name` を道具で読み、`answer` を返す偽 codex のターンを回して結果の env を返す。"""
    files = tmp_path / "users" / uid / "workspace" / "files"
    files.mkdir(parents=True)
    (files / name).write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(store, "live_workspace_rel_paths", lambda u: {name} if u == uid else set())
    result = {"content": [{"type": "text", "text": json.dumps(
        {"id": f"personal:{name}", "start_line": 1, "end_line": 1, "total_lines": 1, "text": "1: x"})}], "isError": False}
    body = H._PY + H._mcp_call("personal_read", {"id": f"personal:{name}"}, result=result) + H._msg(answer)
    H._install_codex(tmp_path, monkeypatch, body)
    return H._result_env(list(H.A.CodexProvider().run(H._ctx(uid, H._make_sources, "qa", personal=True))))


def test_shared_doc_on_the_same_line_as_a_personal_file_stays_cited(tmp_path, monkeypatch):
    """『参照した資料:』の 1 行に個人ファイルと共有資料を並べても、共有資料は出典に残す。"""
    env = _read_personal_and_answer(tmp_path, monkeypatch, "pu4", "メモ.txt", f"答えです。\n\n参照した資料: メモ.txt, {H._DOC}\n")
    assert H._DOC in {s["doc_id"] for s in env["sources"]}
    assert all("メモ" not in s["doc_id"] for s in env["sources"])


def test_personal_file_name_with_a_separator_is_not_split_into_a_shared_doc(tmp_path, monkeypatch):
    """区切りの文字を含む個人ファイルの名前を書いても、分けた片を共有の出典にしない。"""
    name = "下書き," + H._DOC.rsplit("/", 1)[-1]
    env = _read_personal_and_answer(tmp_path, monkeypatch, "pu6", name, f"答えです。\n\n参照した資料: {name}\n")
    assert H._DOC not in {s["doc_id"] for s in env["sources"]}
    assert name in {s["doc_id"] for s in env["personal_sources"]}


def test_shared_doc_after_a_personal_id_on_the_same_line_stays_cited(tmp_path, monkeypatch):
    """『参照した資料:』の 1 行に `personal:` の id と共有資料を並べても、共有資料は出典に残す。"""
    env = _read_personal_and_answer(tmp_path, monkeypatch, "pu7", "メモ.txt",
                                    f"答えです。\n\n参照した資料: personal:メモ.txt, {H._DOC}\n")
    assert H._DOC in {s["doc_id"] for s in env["sources"]}
    assert "メモ.txt" not in json.dumps(env.get("sources_unverified") or [], ensure_ascii=False)


def test_split_piece_of_a_personal_name_is_not_cited_but_other_shared_doc_is(tmp_path, monkeypatch):
    """区切りの文字を含む個人ファイルの名前を他の共有資料と 1 行に並べても、分けた片に当たる共有資料は出典にせず、他の共有資料は出典に残す。"""
    shared = H._DOC.rsplit("/", 1)[-1]
    name = "売上," + shared
    env = _read_personal_and_answer(tmp_path, monkeypatch, "pu8", name, f"答えです。\n\n参照した資料: {name}, {H._DOC2}\n")
    cited = {s["doc_id"] for s in env["sources"]}
    assert H._DOC not in cited and H._DOC2 in cited


@pytest.mark.parametrize("prefix", ["./", ".\\"])
def test_personal_file_written_with_a_dot_prefix_is_not_cited_as_the_shared_doc(tmp_path, monkeypatch, prefix):
    """同じ名前の個人ファイルを `./名前`・`.\\名前` と書いても、共有の出典にしない。"""
    name = H._DOC.rsplit("/", 1)[-1]
    env = _read_personal_and_answer(tmp_path, monkeypatch, "pu5", name, f"答えです。\n\n参照した資料:\n- {prefix}{name}\n")
    assert H._DOC not in {s["doc_id"] for s in env["sources"]}


def test_agents_md_mentions_personal_tools_only_for_personal_turns(tmp_path):
    assert "personal_search" in H._agents_md(tmp_path, name="a1", personal_tools=True)
    assert "personal_search" not in H._agents_md(tmp_path, name="a2")


def test_config_toml_carries_personal_env_to_the_tool_server(tmp_path):
    cfg = tomllib.loads(H._cfg(tmp_path, extra_mcp_env={"SHERPA_MCP_PERSONAL_INDEX": "/x/i.json",
                                                         "SHERPA_MCP_PERSONAL_DIR": "/x/files"}))
    env = cfg["mcp_servers"]["sherpa"]["env"]
    assert env["SHERPA_MCP_PERSONAL_INDEX"] == "/x/i.json" and env["SHERPA_MCP_PERSONAL_DIR"] == "/x/files"


def test_personal_only_toolset_offers_only_personal_tools_and_refuses_folder_tools(personal_turn, monkeypatch):
    """社内資料オフの形（SHERPA_MCP_TOOLSET=personal）は個人ファイルの道具と ask_user だけを出し、資料フォルダ・台帳の道具は呼んでも拒む。"""
    monkeypatch.setenv("SHERPA_MCP_TOOLSET", "personal")
    assert _tool_names() == {"personal_search", "personal_read", "ask_user"}
    for name, args in (("ripgrep_search", {"query": "消費税"}), ("read_doc", {"doc_id": H._DOC}),
                       ("list_docs", {}), ("graph_neighbors", {"name": "TAXCALC"}), ("ledger_status", {})):
        body, is_error = _call(name, args)
        assert is_error and body == {"error": f"unknown tool: {name}"}, name
    body, is_error = _call("personal_read", {"id": "personal:メモ.txt"})
    assert not is_error and "架空の果物" in body["text"]


def _knowledge_off_ctx(uid: str, **kw):
    def _no_route(msg):
        raise AssertionError("社内資料オフでは調べ方を選ばない")
    return H.A.Ctx(message="メモに何が書いてある？", world="v1", route=_no_route, knowledge=False, uid=uid, **kw)


def _launch_recorder(log: Path, answer: str) -> str:
    """起動時の設定・AGENTS.md・配備したスキル・依頼文を `log` に写し、個人ファイルを読んだ呼び出しと回答を出す偽 codex。"""
    result = {"content": [{"type": "text", "text": json.dumps(
        {"id": "personal:メモ.txt", "start_line": 1, "end_line": 1, "total_lines": 1, "text": "1: x"})}], "isError": False}
    return (H._PY + "import json, os, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "home = os.environ.get('CODEX_HOME')\n"
            "cfg = pathlib.Path(home) / 'config.toml' if home else None\n"
            "skills = pathlib.Path.cwd() / '.agents' / 'skills'\n"
            f"pathlib.Path(r'{log}').write_text(json.dumps({{"
            "'config': cfg.read_text() if cfg and cfg.is_file() else None,"
            "'agents_md': (pathlib.Path.cwd() / 'AGENTS.md').read_text(),"
            "'skills': sorted(p.name for p in skills.iterdir()) if skills.is_dir() else [],"
            "'prompt': sys.stdin.read() if args[-1:] == ['-'] else ''}))\n"
            + H._mcp_call("personal_read", {"id": "personal:メモ.txt"}, result=result)
            + H._msg(answer))


def test_codex_turn_with_knowledge_off_has_personal_tools_only_and_no_folder_reads(tmp_path, monkeypatch):
    """社内資料オフ・個人ファイルオンの Codex は個人ファイルの道具だけの形で起動し、シェルから資料フォルダを読めず、出典は個人の枠だけ。"""
    from sherpa.providers.codex import sandbox
    files = tmp_path / "users" / "po1" / "workspace" / "files"
    files.mkdir(parents=True)
    (files / "メモ.txt").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(store, "live_workspace_rel_paths", lambda uid: {"メモ.txt"} if uid == "po1" else set())
    log = tmp_path / "seen.json"
    answer = f"メモにあります。\n\n参照した資料:\n- {H._DOC}\n- personal:メモ.txt\n"
    H._install_codex(tmp_path, monkeypatch, _launch_recorder(log, answer))
    events = list(H.A.CodexProvider().run(_knowledge_off_ctx("po1", personal=True)))
    env = H._result_env(events)
    seen = json.loads(log.read_text())
    cfg = tomllib.loads(seen["config"])
    mcp_env = cfg["mcp_servers"]["sherpa"]["env"]
    assert mcp_env["SHERPA_MCP_TOOLSET"] == "personal" and mcp_env["SHERPA_MCP_PERSONAL_DIR"] == os.path.realpath(files)
    fs = cfg["permissions"]["sherpa-authoring"]["filesystem"]
    roots = sandbox._direct_read_roots("v1")
    assert roots and all(fs.get(r) != "read" for r in roots)
    assert fs.get(sandbox._kb_read_roots("v1")[0]) == "deny"
    assert "資料フォルダ" in seen["agents_md"] and "personal_search" in seen["agents_md"]
    assert not [s for s in seen["skills"] if s.startswith("investigate-")]
    assert "メモ.txt" in seen["prompt"] and str(roots[0]) not in seen["prompt"]
    assert env["personal_sources"] == [{"doc_id": "メモ.txt", "quote": "", "source": "個人ファイル内ヒット"}]
    assert env["sources"] == [] and not env.get("sources_unverified")
    assert H._DOC not in json.dumps(env.get("codex_reference_resolution") or [], ensure_ascii=False)


def test_codex_turn_with_knowledge_off_does_not_launch_without_sandbox(tmp_path, monkeypatch):
    """サンドボックス無し（シェルから資料フォルダが見える）では、個人ファイルオンでも Codex を起動しない。"""
    log = tmp_path / "seen.json"
    H._install_codex(tmp_path, monkeypatch, _launch_recorder(log, "答えです。"), sandbox="0")
    env = H._result_env(list(H.A.CodexProvider().run(_knowledge_off_ctx("po2", personal=True))))
    assert not log.exists()
    assert "personal_sources" not in env and env["lens"] == "chat"


@pytest.mark.parametrize("agent, sandbox, codex", [("simple", "1", False), ("codex", "0", False), ("codex", "1", True)])
def test_knowledge_off_personal_brain_is_codex_only_with_sandbox(monkeypatch, agent, sandbox, codex):
    """社内資料オフ・個人ファイルオンで個人ファイルの道具を持つ Codex を選ぶのは、頭脳が Codex でサンドボックスが有効なときだけ（簡易は素の会話のまま）。"""
    from sherpa import chat_service
    from sherpa.providers.codex.provider import CodexProvider
    monkeypatch.setenv("SHERPA_CODEX_SANDBOX", sandbox)
    p = chat_service._select_provider(None, False, {"agent": agent, "codex_model_provider": "openai"}, {}, personal=True)
    assert isinstance(p, CodexProvider) is codex
    assert not isinstance(chat_service._select_provider(None, False, {"agent": agent}, {}, personal=False), CodexProvider)
