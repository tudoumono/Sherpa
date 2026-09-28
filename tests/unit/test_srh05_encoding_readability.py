"""SRH-05: 資料の文字コード/バイナリで「読めていない」ことを取り込み画面とツール結果の両方で
見えるようにする（`docs/proposals/2026-09-29-CP932と自己記述.md` の第1段拡張）。

`sherpa.text_encoding` の置換文字比率から読み取りの質（"ok"/"partial"/"undetermined"）を導き、
`corpus_docs.classify_document(..., text_quality=...)` がこれを既存の到達可否判定
（`_classify_verdict_reachable`）へ合流させる——拡張子の許可リストではなくこの1つの判定式が
可否を決める契約（§7 裁定10）は変えない。
"""
from __future__ import annotations

import pytest

from sherpa import agentic_search as A
from sherpa import corpus_docs, worlds
from sherpa.ingest.failure_reasons import REASON_CATALOG

# 上流限定のアナライザ登録簿に固定する（`test_corpus_docs_text_kind.py` と同じ理由・
# フォークが `.py`/`.txt` 相当へ専用アナライザを足していても赤にならないようにする）。
pytestmark = pytest.mark.usefixtures("upstream_only_registry")


def _world(monkeypatch, tmp_path):
    wd = tmp_path / "world"
    wd.mkdir()
    der = tmp_path / "derived"
    der.mkdir()
    monkeypatch.setattr(worlds, "world_dir", lambda w: wd)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: der)
    monkeypatch.setattr(worlds, "derived_rag_dir", lambda w: der)
    monkeypatch.setattr(worlds, "observation_current_dir", lambda w: None)
    return wd, der


# 現実的な分量・語彙の日本語（短い反復文だと CP932 で偶然ほぼ通ってしまう＝ EUC-JP の高位バイトが
# CP932 の半角カナ域へ1バイトずつ収まってしまうため）。
_JA_PARAGRAPH = ("これは架空の業務手順書です。毎月末に夜間バッチで締め処理を実行し、在庫管理"
                 "テーブルの区分値を更新します。担当者は処理結果を確認し、異常があれば管理者へ"
                 "連絡してください。申請フォームの項目は氏名、所属部署、承認日を含みます。")


def test_euc_jp_registered_extension_is_unreadable_with_encoding_undetermined_reason(monkeypatch, tmp_path):
    """登録拡張子（`.txt`）でも、UTF-8/CP932 どちらで読んでも化ける（EUC-JP）原本は対象外になり、
    台帳には `read_failed`/`size_exceeded` と同じ既存の出し方（`state="unreadable"`）で載る。"""
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "guide.txt").write_bytes(_JA_PARAGRAPH.encode("euc_jp"))

    docs = corpus_docs.world_documents("w")
    assert [d["name"] for d in docs] == ["guide.txt"]
    d = docs[0]
    assert d["state"] == "unreadable"
    assert d["reason"] == "encoding_undetermined"
    assert d["label"] == REASON_CATALOG["encoding_undetermined"]["label"]

    rep = corpus_docs.scan_report("w")
    assert rep["unreachable_by_reason"] == {"encoding_undetermined": 1}
    assert rep["unreachable_as_text"] == 1
    assert rep["encoding_partial_count"] == 0

    # read_around の到達可否も同じ判定（`classify_document(..., text_quality=...)`）を
    # 共有する（§7 裁定10）——`_safe_doc_path` が `corpus_docs._text_quality_for` を渡す。
    res = A.run_tool("read_around", {"doc_id": "guide.txt", "line": 1, "window": 1}, "w", None)[0]
    assert "error" in res


def test_binary_unknown_extension_scan_report_reason_and_stays_unlisted(monkeypatch, tmp_path):
    """未知拡張子のバイナリ（NUL バイト支配的）は理由別内訳（`unreachable_by_reason["binary"]`）に
    現れるが、一覧への出し方は従来どおり（載らない・`test_corpus_docs_text_kind.py` の既存契約）。"""
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "blob.dat").write_bytes(b"\x00\x01\x02\x03binary\x00\x00")

    docs = corpus_docs.world_documents("w")
    assert docs == []   # 既存契約: バイナリは一覧に出ない（`iter_world_documents` の docstring 参照）

    rep = corpus_docs.scan_report("w")
    assert rep["unreachable_by_reason"] == {"binary": 1}
    assert rep["unreachable_as_text"] == 1


def test_cp932_file_with_a_few_invalid_bytes_stays_ready_with_partial_caution(monkeypatch, tmp_path):
    """置換文字がわずかに残るだけ（`UNDETERMINED_RATIO` 以下）の CP932 原本は対象外にしない——
    `state="ready"` のまま `encoding_partial` を持ち、scan_report は別枠（`encoding_partial_count`）
    で数える（`unreachable_as_text` には含めない）。"""
    wd, _der = _world(monkeypatch, tmp_path)
    raw = bytearray((_JA_PARAGRAPH * 2).encode("cp932"))
    raw[10], raw[11] = 0x81, 0xFF   # cp932 のリードバイトに不正なトレイルバイトを1箇所だけ混ぜる
    (wd / "note.txt").write_bytes(bytes(raw))

    docs = corpus_docs.world_documents("w")
    assert [d["name"] for d in docs] == ["note.txt"]
    d = docs[0]
    assert d["state"] == "ready"
    assert d.get("encoding_partial") is True

    rep = corpus_docs.scan_report("w")
    assert rep["encoding_partial_count"] == 1
    assert rep["unreachable_as_text"] == 0
    assert rep["unreachable_by_reason"] == {}

    res = A.run_tool("read_doc", {"doc_id": "note.txt", "start_line": 1}, "w", None)[0]
    assert "error" not in res
    assert res["encoding_caution"] == corpus_docs._ENCODING_CAUTION["partial"]


def test_scan_report_encoding_partial_count_excludes_size_exceeded_files(monkeypatch, tmp_path):
    """置換文字が残る（`encoding_partial`）ファイルでも、サイズ超過で対象外（`unreadable`）に
    なる場合は `encoding_partial_count` に二重計上しない——`indexed` を実際に加算する経路
    （サイズ超過なら通らない）でだけ数える（SRH-05是正）。未登録拡張子（`.log`）＝軽量テキスト枠
    （内容 sniff で doctype 確定）を使う——`.txt`/`.md` は固定 doctype で `_text_oversize` の
    対象外のため、サイズ超過の分岐そのものを通らない。"""
    from sherpa.ingest import text_kind
    wd, _der = _world(monkeypatch, tmp_path)
    raw = bytearray((_JA_PARAGRAPH * 2).encode("cp932"))
    raw[10], raw[11] = 0x81, 0xFF
    (wd / "big.log").write_bytes(bytes(raw))
    monkeypatch.setattr(text_kind, "MAX_BYTES", 4)   # 実ファイルを軽く保ったまま上限だけ小さくする

    rep = corpus_docs.scan_report("w")
    assert rep["encoding_partial_count"] == 0
    assert rep["unreachable_as_text"] == 1


def test_scan_report_analyzer_declined_excludes_encoding_undetermined_registered_code(monkeypatch, tmp_path):
    """符号化を判別できない登録コード拡張子（.cbl）は `analyzer_declined`（未対応＝担当アナライザの
    `accepts()` が全滅）に数えない——対象外の理由は符号化であって、アナライザは何も拒否して
    いない（SRH-05是正・理由が二重に出ない）。"""
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "prog.cbl").write_bytes(_JA_PARAGRAPH.encode("euc_jp"))

    rep = corpus_docs.scan_report("w")
    assert rep["unreachable_by_reason"] == {"encoding_undetermined": 1}
    assert rep["analyzer_declined"] == 0


def test_plain_utf8_file_has_no_encoding_marks(monkeypatch, tmp_path):
    """完全に読める UTF-8 原本には理由・要確認のいずれの印も付かない（既存挙動の不変確認）。"""
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "app.py").write_text("TARGETWORD = 1\nprint(TARGETWORD)\n", encoding="utf-8")

    docs = corpus_docs.world_documents("w")
    assert [d["name"] for d in docs] == ["app.py"]
    d = docs[0]
    assert d["state"] == "ready"
    assert d.get("reason") is None
    assert "encoding_partial" not in d

    rep = corpus_docs.scan_report("w")
    assert rep["unreachable_as_text"] == 0
    assert rep["unreachable_by_reason"] == {}
    assert rep["encoding_partial_count"] == 0

    res_doc = A.run_tool("read_doc", {"doc_id": "app.py", "start_line": 1}, "w", None)[0]
    assert "encoding_caution" not in res_doc
    res_around = A.run_tool("read_around", {"doc_id": "app.py", "line": 1, "window": 1}, "w", None)[0]
    assert "encoding_caution" not in res_around

    from sherpa import grep_tool, store
    monkeypatch.setattr(store, "get_world_status_row", lambda world: None)   # DB（外部境界）に依存しない
    hits = grep_tool.grep_search("TARGETWORD", world="w")
    assert hits and all("encoding_caution" not in h for h in hits)


def test_ripgrep_search_attaches_encoding_caution_for_partial_cp932_hit(monkeypatch, tmp_path):
    """対象外にしない「一部が化けている」ファイルの grep ヒットには注意文が付く（Codex が黙って
    使わないための機械的な印・行番号や本文自体は従来どおり返す）。

    `grep_tool.grep_search` だけでなく `run_tool("ripgrep_search")`（Codex/MCP が実際に受け取る
    tool result）でも確認する——`run_tool` はヒットを LLM 向け `hit_view` に組み直すため、印は
    そこで明示的に転送しないと落ちる（SRH-05 是正）。"""
    from sherpa import grep_tool, store

    # 重要度の優先順位付け用の署名解決（DB・外部境界）はここでは検証対象外——未接続/遅延でも
    # `grep_search` は自前計算へフォールバックする契約（`grep_tool.grep_search` docstring 参照）
    # だが、本テストは純粋な符号化ロジックだけを見るため固定してテストを DB 到達性から切り離す。
    monkeypatch.setattr(store, "get_world_status_row", lambda world: None)

    wd, _der = _world(monkeypatch, tmp_path)
    prefix = "前置き行1\n前置き行2\nTARGETWORD を含む行です\n".encode("cp932")
    bad = b"\x81\xff"   # cp932 のリードバイトに不正なトレイルバイト（単体で置換文字1個になる・境界跨ぎを避けるため独立に連結する）
    suffix = (_JA_PARAGRAPH * 3).encode("cp932")
    (wd / "memo.txt").write_bytes(prefix + bad + suffix)

    hits = grep_tool.grep_search("TARGETWORD", world="w")
    assert hits and hits[0]["doc_id"] == "memo.txt"
    assert hits[0]["encoding_caution"] == corpus_docs._ENCODING_CAUTION["partial"]

    res = A.run_tool("ripgrep_search", {"query": "TARGETWORD"}, "w", None)[0]
    assert res["hits"] and res["hits"][0]["doc_id"] == "memo.txt"
    assert res["hits"][0]["encoding_caution"] == corpus_docs._ENCODING_CAUTION["partial"]


def test_file_head_attaches_encoding_caution_for_partial_cp932_file(monkeypatch, tmp_path):
    """`file_head`（原本読取ツール）も read_doc/read_around と同じ印を返す——一部が化けている
    資料の本文を印なしで黙って返さない（SRH-05是正）。"""
    wd, _der = _world(monkeypatch, tmp_path)
    raw = bytearray((_JA_PARAGRAPH * 2).encode("cp932"))
    raw[10], raw[11] = 0x81, 0xFF
    (wd / "note.txt").write_bytes(bytes(raw))

    res = A.run_tool("file_head", {"doc_id": "note.txt"}, "w", None)[0]
    assert "error" not in res
    assert res["encoding_caution"] == corpus_docs._ENCODING_CAUTION["partial"]


def test_pass1_excludes_encoding_undetermined_source_from_graph(monkeypatch, tmp_path):
    """台帳・grep・精読が「文字コードを判別できない」として対象外にする登録コード原本は、
    グラフ（Pass1/Pass2）にもノード・エッジを作らない——読めない原本が印なしで影響調査の
    裏付けとして出てしまう食い違いを防ぐ（SRH-05是正）。原本全体を EUC-JP にする（行の過半が
    化ける＝undetermined の条件を確実に満たす。COBOL 構文の要否は Pass1 の判定より前で
    弾かれるため無関係）。"""
    from sherpa.ingest import world_graph

    wd, _der = _world(monkeypatch, tmp_path)
    src = wd / "src"
    src.mkdir()
    (src / "pgma01.cbl").write_bytes(_JA_PARAGRAPH.encode("euc_jp"))

    docs = corpus_docs.world_documents("w")
    assert [d["name"] for d in docs] == ["src/pgma01.cbl"]
    assert docs[0]["state"] == "unreadable"
    assert docs[0]["reason"] == "encoding_undetermined"

    nodes, _edges, flags = world_graph.build_world(wd, "w")
    assert nodes == []
    assert flags == [{"doc": "src/pgma01.cbl", "reason": "encoding_undetermined", "action": "warn"}]
