"""ビルドの定義を読む場所の印を当てはめた結果が、グラフの設定の値に入る（印を変えた後の再開が古いグラフを使い回さない）。"""
from __future__ import annotations

from sherpa import build_marks, worlds
from sherpa.ingest import source_guard
from sherpa.ingest.build_link import MARK_SKIP


def test_graph_settings_change_when_the_applied_marks_change(tmp_path, monkeypatch):
    src = tmp_path / "app" / "src"
    src.mkdir(parents=True)
    (src / "Makefile").write_text("calc: main.o\n\tcc -o calc main.o\n", encoding="utf-8")
    (src / "main.c").write_text("int main(void){return 0;}\n", encoding="utf-8")
    marks: dict = {}
    monkeypatch.setattr(worlds, "world_dir", lambda w: str(tmp_path))
    monkeypatch.setattr(worlds, "archives_dir", lambda w: tmp_path / "_archives")
    monkeypatch.setattr(build_marks, "load", lambda w: dict(marks))

    before = source_guard.current_settings("w")["graph"]
    assert source_guard.current_settings("w")["graph"] == before
    marks["app/src/Makefile"] = MARK_SKIP
    assert source_guard.current_settings("w")["graph"] != before


def test_graph_settings_do_not_change_when_archive_contents_appear(tmp_path, monkeypatch):
    """原本の署名を確定した後にアーカイブの展開物が足されても、印が同じなら設定の値は変わらない（正しい最初の取り込みを止めない）。"""
    root, arch = tmp_path / "root", tmp_path / "_archives"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.c").write_text("int main(void){return 0;}\n", encoding="utf-8")
    monkeypatch.setattr(worlds, "world_dir", lambda w: str(root))
    monkeypatch.setattr(worlds, "archives_dir", lambda w: arch)
    monkeypatch.setattr(build_marks, "load", lambda w: {})

    before = source_guard.current_settings("w")["graph"]
    (arch / "a.zip" / "src").mkdir(parents=True)
    (arch / "a.zip" / "src" / "Makefile").write_text("calc: main.o\n\tcc -o calc main.o\n", encoding="utf-8")
    assert source_guard.current_settings("w")["graph"] == before
