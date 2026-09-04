"""安全走査プリミティブの仕様固定（鏡モデル・PG/Neo4j 不要）。

auto-scope 推定（旧 `infer`）は撤去（範囲＝フォルダパスそのもの・MIRROR §3）。残るのは:
- unique_index: 衝突キーは fail-closed（index に入れず collisions へ）。
- safe_files: symlink を辿らない・root 限定（tmp で検証）。
"""
from __future__ import annotations

import pytest

from sherpa import scope_infer as si


def test_estimation_layer_is_retired():
    """auto-scope の推定 API は撤去されている（鏡＝フォルダが真）。"""
    assert not hasattr(si, "infer") and not hasattr(si, "common_markers")


def test_unique_index_fail_closed_on_collision():
    items = [("a/X.cbl", 1), ("b/X.cbl", 2), ("c/Y.cbl", 3)]
    idx, col = si.unique_index(items, keyfn=lambda it: it[0].split("/")[-1])
    assert "Y.cbl" in idx and idx["Y.cbl"] == ("c/Y.cbl", 3)
    assert "X.cbl" not in idx                      # 衝突は index に入れない（先勝ち禁止）
    assert set(col) == {"X.cbl"} and len(col["X.cbl"]) == 2


def test_safe_files_skips_symlinks_and_confines():
    import tempfile
    from pathlib import Path
    d = Path(tempfile.mkdtemp())
    (d / "sub").mkdir()
    (d / "sub" / "real.cbl").write_text("x", encoding="utf-8")
    outside = d.parent / ("outside_" + d.name)
    outside.mkdir(exist_ok=True)
    (outside / "secret.cbl").write_text("s", encoding="utf-8")
    try:
        (d / "link.cbl").symlink_to(outside / "secret.cbl")   # symlink file → 辿らない
        (d / "linkdir").symlink_to(outside)                    # symlink dir → 辿らない
        rels = {rel for _p, rel in si.safe_files(d)}
        assert rels == {"sub/real.cbl"}                        # symlink 経由は出ない
    except OSError as e:
        pytest.skip(f"symlink 非対応の環境: {e}")                # symlink 不可な環境は skip
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(outside, ignore_errors=True)


def test_safe_files_deadline_none_is_unbounded_default():
    """`deadline` 省略時（既定 None）は従来どおり無期限——既存呼び出し元は無変更（RV6）。"""
    import shutil
    import tempfile
    from pathlib import Path
    d = Path(tempfile.mkdtemp())
    try:
        (d / "a.md").write_text("x", encoding="utf-8")
        rels = {rel for _p, rel in si.safe_files(d)}
        assert rels == {"a.md"}
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_safe_files_deadline_already_past_raises_immediately():
    """`deadline`（`time.monotonic()` 系の絶対期限）が既に過ぎていれば、`iterdir()`/`lstat()` を
    1回も呼ばずに `ScopeWalkDeadlineExceeded` を送出する（`while stack:` の各反復先頭で確認する
    契約・root 自体を含め最初の反復から効く）（RV6）。"""
    import shutil
    import tempfile
    import time
    from pathlib import Path
    d = Path(tempfile.mkdtemp())
    try:
        (d / "a.md").write_text("x", encoding="utf-8")
        with pytest.raises(si.ScopeWalkDeadlineExceeded):
            list(si.safe_files(d, deadline=time.monotonic() - 1))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_safe_files_deadline_checked_during_raw_enumeration_before_sort(monkeypatch):
    """単一ディレクトリに大量のファイルがある場合、次のディレクトリ境界（`while` ループの次の
    反復）を待たずに打ち切る——`_DEADLINE_CHECK_ENTRIES` 件ごとにも `deadline` を再確認する契約は
    列挙段階（`os.scandir` のイテレーション・ソートの前）のチェックが先に発火する。
    `sorted(...)` はイテレータを全件消費してから返るため、ソート**後**の集合だけをチェックしても、
    大量のファイルがあるディレクトリではその列挙・ソート自体がデッドラインを超えて完了しうる。
    列挙段階で `deadline` を超えていれば、ソートにも各エントリの `_lstat_kind` 判定にも一切進まず
    打ち切ることを、`_lstat_kind` の呼び出し回数（root 自身の1回のみのはず）で固定する。"""
    import shutil
    import tempfile
    from pathlib import Path
    d = Path(tempfile.mkdtemp())
    try:
        n = si._DEADLINE_CHECK_ENTRIES * 3 + 10
        for i in range(n):
            (d / f"f{i:04d}.md").write_text("x", encoding="utf-8")

        lstat_calls = {"n": 0}
        orig_lstat_kind = si._lstat_kind

        def _spy(p):
            lstat_calls["n"] += 1
            return orig_lstat_kind(p)

        monkeypatch.setattr(si, "_lstat_kind", _spy)

        calls = {"n": 0}

        def _clock():
            calls["n"] += 1
            # 1・2回目（開始時・while ループ先頭）は通す。3回目（列挙段階の間引きチェック）で
            # 超過させる——列挙段階のチェックそのものが発火することを固定する。
            return 0.0 if calls["n"] <= 2 else 100.0

        monkeypatch.setattr(si.time, "monotonic", _clock)
        with pytest.raises(si.ScopeWalkDeadlineExceeded):
            list(si.safe_files(d, deadline=50.0))
        assert lstat_calls["n"] == 1, "root 自身の判定を超えて、ディレクトリ内エントリの処理へ進んでいる"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_safe_files_deadline_checked_during_post_sort_processing_loop(monkeypatch):
    """列挙段階（ソート前）のチェックを通過した後も、ソート済み集合の処理ループが独立して
    `_DEADLINE_CHECK_ENTRIES` 件ごとに `deadline` を再確認する——列挙段階のチェックを追加した
    ことで、既存の処理ループ側チェックが弱まっていないことを固定する。"""
    import shutil
    import tempfile
    from pathlib import Path
    d = Path(tempfile.mkdtemp())
    try:
        n = si._DEADLINE_CHECK_ENTRIES + 50   # 列挙段階のチェック地点は1箇所だけ（i=256）に収まる件数

        for i in range(n):
            (d / f"f{i:04d}.md").write_text("x", encoding="utf-8")

        lstat_calls = {"n": 0}
        orig_lstat_kind = si._lstat_kind

        def _spy(p):
            lstat_calls["n"] += 1
            return orig_lstat_kind(p)

        monkeypatch.setattr(si, "_lstat_kind", _spy)

        calls = {"n": 0}

        def _clock():
            calls["n"] += 1
            # 1〜4回目（開始時・while ループ先頭・列挙段階の間引きチェック・列挙完了時）は通す。
            # 5回目（処理ループのチェック）で超過させる。
            return 0.0 if calls["n"] <= 4 else 100.0

        monkeypatch.setattr(si.time, "monotonic", _clock)
        with pytest.raises(si.ScopeWalkDeadlineExceeded):
            list(si.safe_files(d, deadline=50.0))
        assert lstat_calls["n"] > 1, "処理ループへ到達していない（列挙段階だけで打ち切られている）"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_safe_files_deadline_checked_before_root_lstat(monkeypatch):
    """RV11 是正の固定: 関数開始直後（root の `lstat`/`resolve` より前）にも deadline を確認する
    ——deadline が既に過ぎていれば、root 自身の `_lstat_kind` すら呼ばずに例外にする。"""
    import shutil
    import tempfile
    import time as time_mod
    from pathlib import Path
    d = Path(tempfile.mkdtemp())
    try:
        (d / "a.md").write_text("x", encoding="utf-8")
        lstat_calls = {"n": 0}
        orig_lstat_kind = si._lstat_kind

        def _spy(p):
            lstat_calls["n"] += 1
            return orig_lstat_kind(p)

        monkeypatch.setattr(si, "_lstat_kind", _spy)
        with pytest.raises(si.ScopeWalkDeadlineExceeded):
            list(si.safe_files(d, deadline=time_mod.monotonic() - 1))
        assert lstat_calls["n"] == 0, "root 自身の lstat すら呼ばずに打ち切るべき"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_safe_files_deadline_checked_right_after_enumeration_completes_for_small_directory(
        monkeypatch):
    """RV11 是正の固定: 列挙件数が間引き間隔（`_DEADLINE_CHECK_ENTRIES`）未満の小規模ディレクトリ
    でも、列挙完了直後（ソートの前）に deadline を確認する——ループ内の間引きチェックだけに
    頼ると、この規模では一度も発火せず超過を見逃す。"""
    import shutil
    import tempfile
    from pathlib import Path
    d = Path(tempfile.mkdtemp())
    try:
        n = 5   # _DEADLINE_CHECK_ENTRIES（256）未満＝列挙ループ内チェックは一度も発火しない
        for i in range(n):
            (d / f"f{i:04d}.md").write_text("x", encoding="utf-8")

        lstat_calls = {"n": 0}
        orig_lstat_kind = si._lstat_kind

        def _spy(p):
            lstat_calls["n"] += 1
            return orig_lstat_kind(p)

        monkeypatch.setattr(si, "_lstat_kind", _spy)

        calls = {"n": 0}

        def _clock():
            calls["n"] += 1
            # 1・2回目（開始時・while ループ先頭）は通す。3回目（列挙完了時）で超過させる。
            return 0.0 if calls["n"] <= 2 else 100.0

        monkeypatch.setattr(si.time, "monotonic", _clock)
        with pytest.raises(si.ScopeWalkDeadlineExceeded):
            list(si.safe_files(d, deadline=50.0))
        assert lstat_calls["n"] == 1, "root 自身の判定を超えて、ディレクトリ内エントリの処理へ進んでいる"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_safe_files_deadline_checked_right_after_per_directory_processing_completes(monkeypatch):
    """RV11 是正の固定: 1ディレクトリの後処理（ソート済み集合の処理ループ）完了時にも deadline を
    確認する——次の while 反復（次のディレクトリの処理開始）まで待たず、その場で打ち切る。
    サブディレクトリを1段用意し、そのサブディレクトリの中身へは一切降りない（`_lstat_kind` が
    サブディレクトリ内のファイルに対して一度も呼ばれない）ことで固定する。"""
    import shutil
    import tempfile
    from pathlib import Path
    d = Path(tempfile.mkdtemp())
    try:
        (d / "root_file.md").write_text("x", encoding="utf-8")
        sub = d / "sub"
        sub.mkdir()
        (sub / "sub_file.md").write_text("y", encoding="utf-8")

        seen_paths: list = []
        orig_lstat_kind = si._lstat_kind

        def _spy(p):
            seen_paths.append(str(p))
            return orig_lstat_kind(p)

        monkeypatch.setattr(si, "_lstat_kind", _spy)

        calls = {"n": 0}

        def _clock():
            calls["n"] += 1
            # 1〜3回目（開始時・while ループ先頭・列挙完了時）は通す。4回目（root ディレクトリの
            # 後処理完了時）で超過させる。
            return 0.0 if calls["n"] <= 3 else 100.0

        monkeypatch.setattr(si.time, "monotonic", _clock)
        with pytest.raises(si.ScopeWalkDeadlineExceeded):
            list(si.safe_files(d, deadline=50.0))
        assert not any("sub_file.md" in p for p in seen_paths), \
            "サブディレクトリの中身へ降りてしまっている（後処理完了時のチェックが効いていない）"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_safe_files_deadline_checked_once_per_directory_not_only_at_start(monkeypatch):
    """`deadline` は `while` ループの各反復（＝1ディレクトリ処理ごと）で再確認する——最初の1回
    だけでなく、木を辿り進めながら継続的に確認することを、偽時計で複数ディレクトリのうち
    「途中まで」処理させてから打ち切ることで固定する（RV6）。"""
    import shutil
    import tempfile
    from pathlib import Path
    d = Path(tempfile.mkdtemp())
    try:
        (d / "sub1").mkdir()
        (d / "sub1" / "one.md").write_text("x", encoding="utf-8")
        (d / "sub2").mkdir()
        (d / "sub2" / "two.md").write_text("x", encoding="utf-8")

        # 1回目の確認（root 処理前）は通す・2回目（sub1 か sub2 のどちらかを処理した後）で
        # 超過させる——「毎回」ではなく「最初だけ」チェックしていた場合は最後まで完走してしまう。
        calls = {"n": 0}

        def _clock():
            calls["n"] += 1
            return 0.0 if calls["n"] <= 1 else 100.0

        monkeypatch.setattr(si.time, "monotonic", _clock)
        with pytest.raises(si.ScopeWalkDeadlineExceeded):
            list(si.safe_files(d, deadline=50.0))
        assert calls["n"] >= 2, "deadline チェックが2回未満＝毎反復で確認していない疑い"
    finally:
        shutil.rmtree(d, ignore_errors=True)
