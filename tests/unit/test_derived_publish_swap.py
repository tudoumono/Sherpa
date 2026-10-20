"""派生物の安全な差し替え（簡易版の世代公開・決定 2026-08-16）。

旧実装は公開中の派生ディレクトリを**先に全消し**してから作り直していたため、
数十秒〜分かかる変換の間ずっと検索対象が欠け、途中で失敗するとその中途半端な状態が
次の同期まで残った（実測: 取り込み中に公開中MDが 104件→0件→徐々に復帰）。

期待する挙動:
  - 書き込みはステージング（`{derived}.staging`）へ行い、公開中は触らない
  - 作り切れたときだけ改名2回で差し替える（`_publish_staging`）
  - 縮退すらできない失敗（rag_failed 等）があれば**公開しない**＝旧内容が生き続ける
  - 改名2回の間で中断した痕跡（`.retired` だけが残る）は次回起動時に復旧する
"""
from __future__ import annotations

import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

from sherpa.ingest import office_md  # noqa: E402


def test_staging_is_used_and_published_dir_untouched_until_swap(tmp_path, monkeypatch):
    """変換中は公開中に触らず、完了時にだけ差し替える。"""
    published = tmp_path / "md"
    published.mkdir()
    (published / "既存.md").write_text("前回の内容", encoding="utf-8")
    source = tmp_path / "src"
    source.mkdir()
    (source / "資料.txt").write_text("本文", encoding="utf-8")

    seen: list[list[str]] = []
    real_publish = office_md._publish_staging

    def _spy(staging, target):
        # 差し替え直前まで公開中は旧内容のまま（＝検索が壊れない）。§8.1 三階層により
        # ir/rag/md の3層それぞれで独立に呼ばれる——ir/rag は今回の初回ビルドで公開先が
        # まだ存在しない（空扱い）。
        seen.append(sorted(p.name for p in target.iterdir()) if target.exists() else [])
        return real_publish(staging, target)

    monkeypatch.setattr(office_md, "_publish_staging", _spy)
    rep = office_md.build_derived(str(source), str(published))

    assert rep.get("error") is None, rep
    assert seen == [[], [], ["既存.md"]], "差し替え前に公開中を書き換えている（順序: ir→rag→md）"
    assert not (published.parent / "md.staging").exists()      # 後始末される
    assert not (published.parent / "md.retired").exists()


def test_interrupted_swap_is_recovered(tmp_path):
    """改名2回の間で中断（公開中が無く .retired だけある）→ 次回に復旧する。"""
    published = tmp_path / "md"
    retired = tmp_path / "md.retired"
    retired.mkdir()
    (retired / "前回.md").write_text("前回の内容", encoding="utf-8")
    assert not published.exists()

    office_md._recover_interrupted_swap(published)

    assert published.is_dir() and (published / "前回.md").exists()
    assert not retired.exists()


def test_recover_does_not_touch_healthy_published(tmp_path):
    """公開中が健全なら復旧処理は何もしない（`.retired` を誤って戻さない）。"""
    published = tmp_path / "md"
    published.mkdir()
    (published / "今の.md").write_text("現行", encoding="utf-8")
    retired = tmp_path / "md.retired"
    retired.mkdir()
    (retired / "古い.md").write_text("古い", encoding="utf-8")

    office_md._recover_interrupted_swap(published)

    assert (published / "今の.md").exists() and not (published / "古い.md").exists()


def test_a_few_failed_documents_do_not_block_publishing(tmp_path, monkeypatch):
    """作れなかった文書が候補の 1 割以下なら、その文書だけを欠いたまま公開する（全文検索・埋め込みへ進める）。"""
    published = tmp_path / "md"
    published.mkdir()
    (published / "既存.md").write_text("前回の内容", encoding="utf-8")
    source = tmp_path / "src"
    source.mkdir()
    (source / "資料.txt").write_text("本文", encoding="utf-8")

    called = {"publish": 0}
    monkeypatch.setattr(office_md, "_publish_staging",
                        lambda staging, target: called.__setitem__("publish", called["publish"] + 1))
    real_build = office_md._build_derived_into_staging

    def _partial(wd, derived, *, progress=None, world=None):
        rep = real_build(wd, derived, progress=progress, world=world)
        rep.update(candidate_total=48000, rag_failed=3, evidence_ir_failed=1)   # 4 万 8000 件のうち 4 件だけ作れなかった
        return rep

    monkeypatch.setattr(office_md, "_build_derived_into_staging", _partial)
    rep = office_md.build_derived(str(source), str(published))

    assert not rep.get("error"), rep.get("error")
    assert rep["derived_partial"] == {"rag_failed": 3, "evidence_ir_failed": 1}
    assert called["publish"] > 0, "一部が作れなかっただけで公開をやめている"


def test_many_failed_documents_still_publish_and_are_flagged(tmp_path, monkeypatch):
    """作れなかった文書が候補の割合（既定 1 割）を超えても公開して先へ進み、多いことを知らせる印を付ける。"""
    published = tmp_path / "md"
    published.mkdir()
    source = tmp_path / "src"
    source.mkdir()
    (source / "資料.txt").write_text("本文", encoding="utf-8")

    called = {"publish": 0}
    monkeypatch.setattr(office_md, "_publish_staging",
                        lambda staging, target: called.__setitem__("publish", called["publish"] + 1))
    real_build = office_md._build_derived_into_staging

    def _mostly_failed(wd, derived, *, progress=None, world=None):
        rep = real_build(wd, derived, progress=progress, world=world)
        rep.update(candidate_total=100, rag_failed=11)
        return rep

    monkeypatch.setattr(office_md, "_build_derived_into_staging", _mostly_failed)
    rep = office_md.build_derived(str(source), str(published))

    assert not rep.get("error"), rep.get("error")
    assert rep["derived_partial"] == {"rag_failed": 11} and rep["derived_partial_many"] is True
    assert called["publish"] > 0
