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


def test_incomplete_build_keeps_published_content(tmp_path, monkeypatch):
    """縮退すらできない失敗（rag_failed 等）があれば公開しない＝旧内容が残る。

    文書ごとの変換失敗は failed notice へ縮退するのが正常系（S3の契約）。ここで止めるのは
    その縮退すらできなかった場合だけ（`_generate_evidence` のコメント参照）。
    """
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

    def _incomplete(wd, derived, *, progress=None, world=None):
        rep = real_build(wd, derived, progress=progress, world=world)
        rep["rag_failed"] = 2                      # notice すら作れなかった状態を模す
        return rep

    monkeypatch.setattr(office_md, "_build_derived_into_staging", _incomplete)
    rep = office_md.build_derived(str(source), str(published))

    assert rep["error"].startswith("derived_incomplete:"), rep.get("error")
    assert "rag_failed=2" in rep["error"]
    assert called["publish"] == 0, "作り切れていないのに公開している"
    assert (published / "既存.md").read_text(encoding="utf-8") == "前回の内容"
    assert not (tmp_path / "md.staging").exists()    # ステージングは片付ける


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
