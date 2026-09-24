"""RV2是正#b1: `ingest/worker.py::_ledger_rows` の root/files/sig 一本化
（`_run_locked` の排他 `world_lock` 保持中に重複 walk させない）。

以前は `importance.resolve_for_world(world)` と `corpus_docs.world_documents(world)` が
それぞれ独立に root を解決し、木を別々に歩いていた（`_重要度.txt` 探索・文書列挙で別々の
`scope_infer.safe_files` 呼び出し・`resolve_for_world` は `sig` 省略時にさらに自前の署名計算
（`worker.world_signature_of_root`）でもう1回歩く）。`_ledger_rows` が root/files を1回だけ
確定し、両方（＋渡せば resolver の署名計算も）へ同じものを渡すことを、`scope_infer.safe_files`
の呼び出し回数で固定する。
"""
from __future__ import annotations

from pathlib import Path

from sherpa import scope_infer, worlds
from sherpa.ingest import worker


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _world(monkeypatch, tmp_path) -> Path:
    wd = tmp_path / "world"
    wd.mkdir()
    der = tmp_path / "derived"
    der.mkdir()
    monkeypatch.setattr(worlds, "world_dir", lambda w: wd)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: der)
    _write(wd / "a.cbl", "       PROGRAM-ID. A.\n")
    _write(wd / "_重要度.txt", "*.cbl: 高")
    return wd


def _count_safe_files(monkeypatch):
    calls = {"n": 0}
    real = scope_infer.safe_files

    def _counting(wd, **kw):
        calls["n"] += 1
        return real(wd, **kw)

    monkeypatch.setattr(scope_infer, "safe_files", _counting)
    return calls


def test_ledger_rows_with_sig_walks_world_exactly_once(monkeypatch, tmp_path):
    """`_run_locked` が実際に使う経路（`world_state()` で確定済みの `sig` を渡す）: `resolve_for_world`
    の自前署名計算（`world_signature_of_root`＝別のもう1回の全木走査）も回避され、
    `_ledger_rows` 全体でちょうど1回しか歩かない（以前は cold 時 最大4walk）。"""
    _world(monkeypatch, tmp_path)
    calls = _count_safe_files(monkeypatch)

    rows = worker._ledger_rows("wtest", sig="fixed-sig")

    assert calls["n"] == 1
    row = next(r for r in rows if r["name"] == "a.cbl")
    assert row["importance"] == "高"


def test_ledger_rows_without_sig_still_shares_enumeration_with_resolver(monkeypatch, tmp_path):
    """`sig` 省略時は `resolve_for_world` が自前の署名計算（`world_signature_of_root`）でもう
    1回歩く（2回）——それでも文書列挙自体（`world_documents`）は `_ledger_rows` が渡した
    `files` を再利用するため、以前（列挙用に独立でもう1回＝最大3〜4回）より減っている。
    直接呼び出し（テスト・CLI 等）向けの後方互換パスであり、`_run_locked` は必ず `sig=` を
    渡す（`test_ledger_rows_with_sig_walks_world_exactly_once` 参照）。
    """
    _world(monkeypatch, tmp_path)
    calls = _count_safe_files(monkeypatch)

    rows = worker._ledger_rows("wtest")

    assert calls["n"] == 2
    row = next(r for r in rows if r["name"] == "a.cbl")
    assert row["importance"] == "高"


def test_ledger_rows_unresolved_world_returns_empty_without_walk(monkeypatch):
    """root が解決できない（`worlds.world_dir()` が `None`）場合は歩かず空リストを返す
    （fail-closed・従来どおり）。"""
    calls = {"n": 0}

    def _boom(*a, **kw):
        calls["n"] += 1
        raise AssertionError("root 未解決時に歩いてはいけない")

    import sherpa.worlds as worlds_mod

    monkeypatch.setattr(worlds_mod, "world_dir", lambda w: None)
    monkeypatch.setattr(scope_infer, "safe_files", _boom)

    assert worker._ledger_rows("wtest") == []
    assert calls["n"] == 0
