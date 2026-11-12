"""1 回の取り込みで、原本のフォルダの列挙は 1 回だけ。変換キャッシュの当たる PDF の原本は 2 回目以降に開かない。

実害: 共有フォルダ（ネットワーク越し・数万ファイル）で、グラフ・台帳・ES・最終の集計がそれぞれ同じ木を歩き直していた。
キャッシュの当たる PDF も、入口ガードのために原本の先頭と末尾を毎回読んでいた。
外部境界（DB・Neo4j・ES・ファイルの open と os.scandir の計数）だけを差し替え、走査・MD 化・グラフ・台帳・集計は実物を通す。
"""
from __future__ import annotations

import builtins
import io
import os
from pathlib import Path

import pytest

from sherpa import corpus_docs, es_index, scope_infer, store
from sherpa.ingest import importance, worker, world_neo4j

from test_ingest_scan_once import _stub_external
from tests._replace_row import use_offline_rows


@pytest.fixture(autouse=True)
def _offline_rows(monkeypatch):
    use_offline_rows(monkeypatch)

_REAL = {"build_world_graph": worker.build_world_graph, "_ledger_rows": worker._ledger_rows,
         "scan_report": corpus_docs.scan_report}


def _stub_with_real_downstream(monkeypatch, root: Path) -> Path:
    """`_stub_external` のうち、グラフ構築・台帳の行・最終の集計は実物へ戻す。取り込みの出力は `captured` に集める。"""
    wd = _stub_external(monkeypatch, root)
    monkeypatch.setattr(worker, "build_world_graph", _REAL["build_world_graph"])
    monkeypatch.setattr(worker, "_ledger_rows", _REAL["_ledger_rows"])
    monkeypatch.setattr(corpus_docs, "scan_report", _REAL["scan_report"])
    return wd


def _capture_outputs(monkeypatch) -> dict:
    out: dict = {}
    monkeypatch.setattr(store, "stage_ledger", lambda world, rows, names, **_kw: out.__setitem__("rows", rows) or len(rows))
    monkeypatch.setattr(store, "switch_ledger", lambda world, **_kw: None)
    monkeypatch.setattr(world_neo4j, "load_world",
                        lambda nodes, edges, *a, **kw: out.update(nodes=nodes, edges=edges) or (len(nodes), len(edges)))

    def _confirm(run_id, world, **kw):
        out.update(scan_report=kw.get("scan_report"), doc_count=kw.get("doc_count"),
                   failed_files=(kw.get("extraction_snapshot") or {}).get("failed_files"))
        return {"id": run_id}

    monkeypatch.setattr(store, "finish_ingest_run_and_confirm_world", _confirm)
    return out


def _count_scandir(monkeypatch, root: Path) -> dict:
    """`root` そのものの列挙（`os.scandir(root)`）の回数。1 回の走査は根をちょうど 1 回列挙する。"""
    calls = {"n": 0}
    real = os.scandir
    wanted = {str(root), str(root.resolve())}

    def _counting(path="."):
        if not isinstance(path, int) and os.fspath(path) in wanted:
            calls["n"] += 1
        return real(path)

    monkeypatch.setattr(os, "scandir", _counting)
    return calls


def _tree_text(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file() and p.name != ".ingest_no"}   # 取り込みの番号は回ごとに決まる


def test_run_enumerates_world_root_once(monkeypatch, tmp_path):
    wd = _stub_with_real_downstream(monkeypatch, tmp_path)
    monkeypatch.setattr(worker.office_md, "confirm_human_md_es_sig", _REAL_CONFIRM)   # ES の確定の前提の判定も実物で通す
    calls = _count_scandir(monkeypatch, wd)

    res = worker.sync("w", reflect=True)

    assert res["status"] in ("auto_published", "auto_published_with_flags"), res
    assert calls["n"] == 1


_REAL_CONFIRM = worker.office_md.confirm_human_md_es_sig



def test_reused_walk_gives_the_same_outputs_as_walking_again(monkeypatch, tmp_path):
    """列挙の使い回しは、ライブの歩き直しと同じ台帳・グラフ・集計・派生物を作る。"""
    results = []
    for name, reuse in (("walk_again", False), ("reuse", True)):
        _stub_with_real_downstream(monkeypatch, tmp_path / name)
        if not reuse:
            monkeypatch.setattr(scope_infer, "reuse_walk", lambda snapshot: None)
        out = _capture_outputs(monkeypatch)
        (tmp_path / name / "wd" / "sub" / "deep").mkdir()
        (tmp_path / name / "wd" / "sub" / "deep" / "d.txt").write_text("deep", encoding="utf-8")

        res = worker.sync("w", reflect=True)

        assert res["status"] in ("auto_published", "auto_published_with_flags"), res
        out["rows"] = [{**r, "md_path": (r["md_path"] or "").replace(str(tmp_path / name), "")} for r in out["rows"]]
        out["md"] = {k: v for k, v in _tree_text(tmp_path / name / "derived" / "md").items() if k != ".world_sig"}   # 署名は原本の更新時刻から決まる
        out["rag"] = _tree_text(tmp_path / name / "derived" / "rag")
        results.append(out)
        monkeypatch.undo()

    walk_again, reused = results
    assert len(reused["rows"]) == 4 and reused["scan_report"]["scanned"] == 4
    assert reused == walk_again


def test_walk_reuse_serves_the_snapshot_only_inside_the_scope(tmp_path):
    root = tmp_path / "wd"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")
    skipped: dict = {}
    snapshot = scope_infer.WalkSnapshot.from_walk(root, list(scope_infer.safe_files(root)), {"symlink": 2})
    (root / "later.txt").write_text("b", encoding="utf-8")        # 冒頭の列挙の後に増えた原本

    with scope_infer.walk_reuse_scope():
        scope_infer.reuse_walk(snapshot)
        inside = [rel for _rp, rel in scope_infer.safe_files(root, skipped=skipped)]
    outside = [rel for _rp, rel in scope_infer.safe_files(root)]

    assert inside == ["a.txt"] and skipped == {"symlink": 2}
    assert outside == ["a.txt", "later.txt"]
    scope_infer.reuse_walk(snapshot)                               # 範囲の外では何もしない
    assert [rel for _rp, rel in scope_infer.safe_files(root)] == ["a.txt", "later.txt"]


def test_walk_reuse_walks_the_extra_root_and_other_roots_live(tmp_path):
    root, extra, other = tmp_path / "wd", tmp_path / "extra", tmp_path / "other"
    for d in (root, extra, other):
        d.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")
    (extra / "x.txt").write_text("x", encoding="utf-8")
    snapshot = scope_infer.WalkSnapshot.from_walk(root, list(scope_infer.safe_files(root)), {})
    (extra / "y.txt").write_text("y", encoding="utf-8")            # 展開先は展開の後の今を歩く
    (other / "o.txt").write_text("o", encoding="utf-8")

    with scope_infer.walk_reuse_scope():
        scope_infer.reuse_walk(snapshot)
        combined = [rel for _rp, rel in scope_infer.safe_files(root, also=extra)]
        other_files = [rel for _rp, rel in scope_infer.safe_files(other)]

    assert combined == ["a.txt", "x.txt", "y.txt"]
    assert other_files == ["o.txt"]


def test_walk_reuse_is_not_used_for_strict_after_an_unreadable_directory(tmp_path):
    root = tmp_path / "wd"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")
    bad = scope_infer.WalkSnapshot.from_walk(root, [], {"unreadable_dir": 1})
    (root / "later.txt").write_text("b", encoding="utf-8")

    with scope_infer.walk_reuse_scope():
        scope_infer.reuse_walk(bad)
        strict_rels = [rel for _rp, rel in scope_infer.safe_files(root, strict=True)]
        lenient_rels = [rel for _rp, rel in scope_infer.safe_files(root)]

    assert strict_rels == ["a.txt", "later.txt"]                   # 見えなかったものがある列挙を strict の呼び出しには使わせない
    assert lenient_rels == []


def test_es_side_helpers_do_not_walk_inside_the_scope(monkeypatch, tmp_path):
    """ES の索引づくりが使う文書一覧・重要度・版の判定は、取り込みの範囲の中では歩き直さない。"""
    wd = _stub_with_real_downstream(monkeypatch, tmp_path)
    snapshot = worker._scan_walk(wd).walk
    calls = _count_scandir(monkeypatch, wd)

    with scope_infer.walk_reuse_scope():
        scope_infer.reuse_walk(snapshot)
        docs = corpus_docs.world_documents("w", include_rag=True)
        importance.resolve_for_world("w", root=wd)
        es_index._human_md_config_sig("w")
        corpus_docs.scan_report("w")

    assert docs
    assert calls["n"] == 0


# ---- 変換キャッシュの当たる PDF は原本を開かない ----

_PDF_TEXT = "Quarterly design review of the batch settlement module for the accounting group"


def _text_pdf(path: Path, text: str = _PDF_TEXT) -> Path:
    """本文 1 行だけの 1 ページの PDF を直接組む。"""
    objs: list[bytes] = [b"", b""]

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    data = f"BT /F1 12 Tf 20 150 Td ({text}) Tj ET".encode("latin-1")
    content = add(b"<< /Length %d >>\nstream\n" % len(data) + data + b"\nendstream")
    page = add(("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 200] /Contents %d 0 R "
                "/Resources << /Font << /F1 %d 0 R >> >> >>" % (content, font)).encode())
    objs[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[1] = ("<< /Type /Pages /Kids [%d 0 R] /Count 1 >>" % page).encode()
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    path.write_bytes(bytes(out))
    return path


def _encrypt_pdf(path: Path) -> None:
    from pypdf import PdfReader, PdfWriter
    writer = PdfWriter(clone_from=PdfReader(str(path)))
    writer.encrypt("user-pw-for-test", owner_password="owner-pw-for-test")
    buf = io.BytesIO()
    writer.write(buf)
    path.write_bytes(buf.getvalue())


def _spy_source_reads(monkeypatch, source: Path) -> list:
    """`source` を読むために開く呼び出し（open・Path.open・read_bytes・PDF ライブラリの文書オープン）を記録する。中身は実物のまま通す。"""
    import pypdf
    import pypdfium2

    resolved = source.resolve()
    reads: list = []

    def _is_source(target) -> bool:
        try:
            return isinstance(target, (str, os.PathLike)) and Path(target).resolve() == resolved
        except OSError:
            return False

    def _writes(mode) -> bool:
        return any(c in str(mode) for c in "wax+")

    real_open = builtins.open

    def _open(file, mode="r", *a, **kw):
        if _is_source(file) and not _writes(mode):
            reads.append("open")
        return real_open(file, mode, *a, **kw)

    monkeypatch.setattr(builtins, "open", _open)
    monkeypatch.setattr(io, "open", _open)
    real_path_open = Path.open

    def _path_open(self, mode="r", *a, **kw):
        if _is_source(self) and not _writes(mode):
            reads.append("Path.open")
        return real_path_open(self, mode, *a, **kw)

    monkeypatch.setattr(Path, "open", _path_open)
    real_read_bytes = Path.read_bytes

    def _read_bytes(self):
        if _is_source(self):
            reads.append("read_bytes")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _read_bytes)
    real_reader = pypdf.PdfReader.__init__

    def _reader(self, stream, *a, **kw):
        if _is_source(stream):
            reads.append("PdfReader")
        return real_reader(self, stream, *a, **kw)

    monkeypatch.setattr(pypdf.PdfReader, "__init__", _reader)
    real_doc = pypdfium2.PdfDocument.__init__

    def _doc(self, input_data, *a, **kw):
        if _is_source(input_data):
            reads.append("PdfDocument")
        return real_doc(self, input_data, *a, **kw)

    monkeypatch.setattr(pypdfium2.PdfDocument, "__init__", _doc)
    return reads


@pytest.fixture
def _pdf_world(monkeypatch, tmp_path):
    monkeypatch.setenv("SHERPA_MCP_ARMS", "ooxml,pdf_text")
    wd = _stub_with_real_downstream(monkeypatch, tmp_path)
    return wd


def test_cache_hit_pdf_is_not_opened_on_the_second_ingest(monkeypatch, tmp_path, _pdf_world):
    wd = _pdf_world
    pdf = _text_pdf(wd / "design.pdf")
    out = _capture_outputs(monkeypatch)
    first = worker.sync("w", reflect=True)
    assert first["status"] in ("auto_published", "auto_published_with_flags"), first
    md_first = _tree_text(tmp_path / "derived" / "md")
    rag_first = _tree_text(tmp_path / "derived" / "rag")
    assert "design.pdf.md" in md_first
    rows_first = out["rows"]

    reads = _spy_source_reads(monkeypatch, pdf)
    second = worker.sync("w", reflect=True)

    assert second["status"] in ("auto_published", "auto_published_with_flags"), second
    assert reads == []
    assert _tree_text(tmp_path / "derived" / "md") == md_first
    assert _tree_text(tmp_path / "derived" / "rag") == rag_first
    assert out["rows"] == rows_first


def test_changed_pdf_is_still_read_and_checked(monkeypatch, tmp_path, _pdf_world):
    """原本が変わった PDF（鍵が外れる）は、今までどおり原本を読んで入口ガードと変換を通す。"""
    wd = _pdf_world
    pdf = _text_pdf(wd / "design.pdf")
    worker.sync("w", reflect=True)
    _text_pdf(pdf, _PDF_TEXT + " with a revised appendix")

    reads = _spy_source_reads(monkeypatch, pdf)
    res = worker.sync("w", reflect=True)

    assert res["status"] in ("auto_published", "auto_published_with_flags"), res
    assert reads
    assert "revised appendix" in (tmp_path / "derived" / "md" / "design.pdf.md").read_text(encoding="utf-8")


def test_cached_password_protected_pdf_is_not_opened_and_stays_a_failure(monkeypatch, tmp_path, _pdf_world):
    wd = _pdf_world
    pdf = _text_pdf(wd / "locked.pdf")
    _encrypt_pdf(pdf)
    out = _capture_outputs(monkeypatch)
    worker.sync("w", reflect=True)
    assert [i["reason"] for i in out["failed_files"]["items"]] == ["password_protected"]

    reads = _spy_source_reads(monkeypatch, pdf)
    out.clear()
    worker.sync("w", reflect=True)

    assert reads == []
    assert [i["reason"] for i in out["failed_files"]["items"]] == ["password_protected"]


def test_walk_reuse_drops_entries_swapped_for_symlinks_after_the_walk(tmp_path):
    root = tmp_path / "wd"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("a", encoding="utf-8")
    (root / "sub" / "b.txt").write_text("b", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("outside", encoding="utf-8")
    snapshot = scope_infer.WalkSnapshot.from_walk(root, list(scope_infer.safe_files(root)), {})
    (root / "a.txt").unlink()
    (root / "a.txt").symlink_to(outside)                           # 列挙の後にファイルを外への symlink に差し替える
    (root / "sub" / "b.txt").unlink()
    (root / "sub").rmdir()
    (root / "sub").symlink_to(tmp_path)                            # 途中の階層を差し替える
    skipped: dict = {}

    with scope_infer.walk_reuse_scope():
        scope_infer.reuse_walk(snapshot)
        inside = [rel for _rp, rel in scope_infer.safe_files(root, skipped=skipped)]

    assert inside == []
    assert skipped.get("symlink") == 1 and skipped.get("unreadable_file") == 1


def test_snapshot_files_for_also_drops_entries_swapped_for_symlinks(tmp_path):
    root = tmp_path / "wd"
    root.mkdir()
    (root / "a.zip").write_bytes(b"PK")
    outside = tmp_path / "outside.zip"
    outside.write_bytes(b"PK")
    snapshot = scope_infer.WalkSnapshot.from_walk(root, list(scope_infer.safe_files(root)), {})
    (root / "a.zip").unlink()
    (root / "a.zip").symlink_to(outside)                           # 展開・変換が直接受け取る一覧も確かめ直す

    assert snapshot.files_for(root) == []
    assert snapshot.files_for(root, strict=True) == []


def test_snapshot_is_not_reused_when_the_root_itself_is_swapped_for_a_symlink(tmp_path):
    root = tmp_path / "wd"
    root.mkdir()
    (root / "a.zip").write_bytes(b"PK")
    snapshot = scope_infer.WalkSnapshot.from_walk(root, list(scope_infer.safe_files(root)), {})
    other = tmp_path / "other"
    other.mkdir()
    (other / "a.zip").write_bytes(b"PK-outside")
    root.rename(tmp_path / "moved")
    root.symlink_to(other)                                         # 列挙の後に root そのものを外への symlink に差し替える

    assert snapshot.files_for(root) is None
    assert snapshot.files_for(root, strict=True) is None
