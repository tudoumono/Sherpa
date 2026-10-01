"""アーカイブ取り込み（zip/tar(.gz)/tgz）の受け入れ条件を固定する（提案: 2026-10-01-アーカイブ取り込み）。

原本（登録ディレクトリ）には一切書かず、展開先（`worlds.archives_dir`）へ安全に展開する
（`sherpa.ingest.archive_extract`）。doc_id＝`<アーカイブの相対パス>/<中のパス>`（世界本体の文書列挙
（`corpus_docs.iter_world_documents`）へ `scope_infer.safe_files(..., also=archives_dir)` で合流する）。

世界の隔離は `tests/unit/test_agentic_search.py::_isolate_world_kb` と同じ手法（`SHERPA_KB_DIR` を
tmp へ向け、`store.get_world` を None 固定して registry 解決をバイパスする・DB 不要）。
"""
from __future__ import annotations

import io
import struct
import tarfile
import time
import zipfile
import zlib
from pathlib import Path

import pytest

from sherpa import corpus_docs, documents, grep_tool, worlds
from sherpa.ingest import archive_extract as ax


def _isolate_world_kb(monkeypatch, tmp_path) -> Path:
    """`sherpa.worlds.world_dir`/`archives_dir` を tmp 配下へ隔離する（DB 不要）。"""
    from sherpa import store

    kb = tmp_path / "kb"
    kb.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SHERPA_KB_DIR", str(kb))
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(tmp_path / "derived"))
    monkeypatch.delenv("SHERPA_USE_FIXTURES", raising=False)
    for env in ("SHERPA_MCP_WORLD", "SHERPA_MCP_WORLD_ROOT"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(store, "get_world", lambda world_id: None)
    return kb


def _write_minimal_zip(path: Path, entries: list[tuple[bytes, bytes, int]]) -> None:
    """生の ZIP バイト列を直接組み立てる（`name_bytes`/`flag_bits` を自由に制御するため）。

    `zipfile.ZipFile.writestr` は非 ASCII 名を書くと自動で UTF-8 フラグ（0x800）を立ててしまう
    （`ZipInfo._encodeFilenameFlags`）ため、Shift_JIS 名（フラグ無し）や暗号化フラグ（bit0）を
    持つエントリは stdlib の書き込みでは再現できない——最小限の ZIP（無圧縮=STORED）を自前で書く。
    各 entry は `(name_bytes, content_bytes, flag_bits)`。
    """
    local_parts: list[bytes] = []
    central_parts: list[bytes] = []
    offset = 0
    for name_bytes, content, flag_bits in entries:
        crc = zlib.crc32(content) & 0xFFFFFFFF
        size = len(content)
        local = struct.pack(
            "<4sHHHHHLLLHH", b"PK\x03\x04", 20, flag_bits, 0, 0, 0, crc, size, size,
            len(name_bytes), 0) + name_bytes + content
        central = struct.pack(
            "<4sHHHHHHLLLHHHHHLL", b"PK\x01\x02", 20, 20, flag_bits, 0, 0, 0, crc, size, size,
            len(name_bytes), 0, 0, 0, 0, 0, offset) + name_bytes
        local_parts.append(local)
        central_parts.append(central)
        offset += len(local)
    central_blob = b"".join(central_parts)
    local_blob = b"".join(local_parts)
    eocd = struct.pack(
        "<4sHHHHLLH", b"PK\x05\x06", 0, 0, len(entries), len(entries),
        len(central_blob), len(local_blob), 0)
    path.write_bytes(local_blob + central_blob + eocd)


# ---- A: zip の安全展開（traversal / nested / sensitive をまとめて1本に・doc_id の形）--------------

def test_zip_doc_id_prefix_and_member_safety(monkeypatch, tmp_path):
    kb = _isolate_world_kb(monkeypatch, tmp_path)
    world = "arcw1"
    wd = kb / world
    (wd / "docs").mkdir(parents=True)
    zpath = wd / "docs" / "design.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("screen/list.txt", "list contents\n")
        zf.writestr("readme.txt", "hello\n")
        zf.writestr("inner/nested.zip", b"PK\x03\x04fake-nested-archive")  # 入れ子は展開しない
        zf.writestr("credentials", "dummy\n")                             # 秘匿名は除外
        zf.writestr("../../etc/escape.txt", "evil\n")                     # パス脱出は拒否

    root = worlds.world_dir(world)
    assert root == wd
    summary = ax.sync_world_archives(world, root)
    s = summary["docs/design.zip"]
    assert s["status"] == "ok"
    assert s["extracted_count"] == 2            # screen/list.txt・readme.txt だけ
    assert s["skipped_nested"] == 1
    assert s["skipped_sensitive"] == 1
    assert s["skipped_traversal"] == 1

    archives_root = worlds.archives_dir(world)
    assert (archives_root / "docs" / "design.zip" / "screen" / "list.txt").is_file()
    assert not (archives_root / "docs" / "design.zip" / "inner" / "nested.zip").exists()
    assert not (archives_root / "docs" / "design.zip" / "credentials").exists()
    # パス脱出は展開先の外へは一切書かれない（dest_dir 配下に留まる）。
    escaped = list(archives_root.parent.rglob("escape.txt"))
    assert escaped == []

    rows = {r["name"]: r for r in corpus_docs.iter_world_documents(world)}
    assert rows["docs/design.zip"]["branch"] == "archive"
    assert "2件" in rows["docs/design.zip"]["label"]
    assert rows["docs/design.zip/screen/list.txt"]["doctype"] == "テキスト"
    assert rows["docs/design.zip/readme.txt"]["doctype"] == "テキスト"


# ---- B: tar.gz のシンボリックリンク非展開 ----------------------------------------------------

def test_targz_symlink_not_extracted_and_doc_id(monkeypatch, tmp_path):
    kb = _isolate_world_kb(monkeypatch, tmp_path)
    world = "arcw2"
    wd = kb / world
    wd.mkdir(parents=True)
    tpath = wd / "bundle.tar.gz"
    with tarfile.open(tpath, "w:gz") as tf:
        data = b"real content\n"
        info = tarfile.TarInfo("real.txt")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("link.txt")
        link.type = tarfile.SYMTYPE
        link.linkname = "real.txt"
        tf.addfile(link)

    root = worlds.world_dir(world)
    summary = ax.sync_world_archives(world, root)
    s = summary["bundle.tar.gz"]
    assert s["status"] == "ok"
    assert s["extracted_count"] == 1
    assert s["skipped_symlink"] == 1
    archives_root = worlds.archives_dir(world)
    assert (archives_root / "bundle.tar.gz" / "real.txt").is_file()
    assert not (archives_root / "bundle.tar.gz" / "link.txt").exists()

    rows = {r["name"]: r for r in corpus_docs.iter_world_documents(world)}
    assert "bundle.tar.gz/real.txt" in rows
    assert "bundle.tar.gz/link.txt" not in rows


# ---- C: Shift_JIS ファイル名 --------------------------------------------------------------

def test_shift_jis_zip_filename_decoded(tmp_path):
    jp_name = "設計書/一覧.txt"            # 設計書/一覧.txt
    name_bytes = jp_name.encode("cp932")
    zpath = tmp_path / "jp.zip"
    _write_minimal_zip(zpath, [(name_bytes, b"content\n", 0)])  # flag_bits=0=UTF-8フラグ無し
    dest = tmp_path / "dest"
    result = ax.extract_archive(zpath, dest, work_root=tmp_path / "work", rel="jp.zip")
    assert result["status"] == "ok"
    assert result["extracted_count"] == 1
    assert (dest / "設計書" / "一覧.txt").is_file()


# ---- D: 暗号化（パスワード保護）zip は未対応として記録する -----------------------------------

def test_encrypted_zip_is_unsupported(tmp_path):
    zpath = tmp_path / "enc.zip"
    _write_minimal_zip(zpath, [(b"secret.txt", b"data\n", 0x1)])   # bit0=暗号化フラグ
    dest = tmp_path / "dest"
    result = ax.extract_archive(zpath, dest, work_root=tmp_path / "work", rel="enc.zip")
    assert result["status"] == "encrypted"
    assert result["reason"] == "archive_encrypted"
    assert result["extracted_count"] == 0
    assert not any(dest.rglob("*"))


# ---- E: 展開爆弾（件数/合計サイズ上限）----------------------------------------------------

def test_uncompressed_size_limit_marks_too_large(monkeypatch, tmp_path):
    monkeypatch.setattr(ax, "MAX_TOTAL_UNCOMPRESSED_BYTES", 10)   # 到達しやすい小さな上限にする
    zpath = tmp_path / "bomb.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("big.txt", "x" * 1000)
    dest = tmp_path / "dest"
    result = ax.extract_archive(zpath, dest, work_root=tmp_path / "work", rel="bomb.zip")
    assert result["status"] == "too_large"
    assert result["reason"] == "archive_too_large"
    assert result["extracted_count"] == 0


# ---- F: 更新（内容ハッシュ変化）で置き換わり、削除で消える（鏡モデル）--------------------------

def test_archive_update_replaces_and_delete_removes(monkeypatch, tmp_path):
    kb = _isolate_world_kb(monkeypatch, tmp_path)
    world = "arcw3"
    wd = kb / world
    wd.mkdir(parents=True)
    zpath = wd / "a.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("v1.txt", "version1\n")
    root = worlds.world_dir(world)
    ax.sync_world_archives(world, root)
    archives_root = worlds.archives_dir(world)
    assert (archives_root / "a.zip" / "v1.txt").is_file()

    # 内容を変える（更新）→ 再展開されて旧内容は消える。
    time.sleep(0.01)
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("v2.txt", "version2\n")
    ax.sync_world_archives(world, root)
    assert not (archives_root / "a.zip" / "v1.txt").exists()
    assert (archives_root / "a.zip" / "v2.txt").is_file()

    # アーカイブ自体を削除 → 展開先ごと消える（削除伝播）。
    zpath.unlink()
    ax.sync_world_archives(world, root)
    assert not (archives_root / "a.zip").exists()
    assert not worlds.archive_manifest_path(world).exists()


# ---- G: 中のファイルのダウンロードは展開した写しを返す -----------------------------------------

def test_inner_file_download_resolves_extracted_copy(monkeypatch, tmp_path):
    kb = _isolate_world_kb(monkeypatch, tmp_path)
    world = "arcw4"
    wd = kb / world
    (wd / "d").mkdir(parents=True)
    zpath = wd / "d" / "b.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("x/y.txt", "payload\n")
    root = worlds.world_dir(world)
    ax.sync_world_archives(world, root)

    resolved = documents.resolve("d/b.zip/x/y.txt", world)
    assert resolved is not None
    assert resolved.read_text(encoding="utf-8") == "payload\n"
    assert resolved.is_relative_to(worlds.archives_dir(world))

    # アーカイブ自身（原本）は従来どおり原本ツリーから解決する。
    resolved_archive = documents.resolve("d/b.zip", world)
    assert resolved_archive == zpath


# ---- H: root と派生領域が重なっていたら何も書かず明示的なエラー（RV是正）-----------------------

def test_sync_refuses_and_writes_nothing_when_root_overlaps_derived(monkeypatch, tmp_path):
    from sherpa import store

    shared = tmp_path / "shared"
    shared.mkdir()
    # わざと KB と派生領域を同じ場所にする（取り違いやすい設定ミスの再現・原本保護の検査対象）。
    monkeypatch.setenv("SHERPA_KB_DIR", str(shared))
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(shared))
    monkeypatch.delenv("SHERPA_USE_FIXTURES", raising=False)
    monkeypatch.setattr(store, "get_world", lambda world_id, **kw: None)

    world = "overlapw"
    wd = shared / world
    wd.mkdir()
    zpath = wd / "a.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("x.txt", "hi\n")

    root = worlds.world_dir(world)
    assert root == wd
    before = sorted(str(p.relative_to(wd)) for p in wd.rglob("*"))

    with pytest.raises(ax.ArchiveRootOverlapError):
        ax.sync_world_archives(world, root)

    after = sorted(str(p.relative_to(wd)) for p in wd.rglob("*"))
    assert before == after   # 原本（= この設定では派生領域と同じ場所）には何も書かれていない
    assert not worlds.archive_manifest_path(world).exists()


# ---- I: アーカイブの列挙に失敗した回は削除を伝播しない（RV是正）--------------------------------

def test_enumeration_failure_preserves_prior_manifest_and_extracted_content(monkeypatch, tmp_path):
    kb = _isolate_world_kb(monkeypatch, tmp_path)
    world = "arcw5"
    wd = kb / world
    wd.mkdir(parents=True)
    zpath = wd / "a.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("x.txt", "hi\n")
    root = worlds.world_dir(world)

    first = ax.sync_world_archives(world, root)
    assert first["a.zip"]["status"] == "ok"
    archives_root = worlds.archives_dir(world)
    manifest_path = worlds.archive_manifest_path(world)
    assert (archives_root / "a.zip" / "x.txt").is_file()
    manifest_before = manifest_path.read_bytes()

    # 列挙そのものが失敗する回を模す（strict 列挙が OSError を re-raise するケース・NAS瞬断等）。
    def _boom(*a, **kw):
        raise OSError("transient enumeration failure")
        yield   # pragma: no cover - ジェネレータにするためだけの到達しない yield

    monkeypatch.setattr(ax.scope_infer, "safe_files", _boom)
    result = ax.sync_world_archives(world, root)

    assert result == first               # 前回の manifest をそのまま返す（削除も書換えもしない）
    assert (archives_root / "a.zip" / "x.txt").is_file()    # 展開物は消えていない
    assert manifest_path.read_bytes() == manifest_before    # manifest ファイルも書き換わっていない


# ---- J: 実測の上限超過は展開先に何も残さない（RV是正・ステージング→改名）-----------------------

def test_overflow_leaves_nothing_in_dest_and_clears_stale_content_on_update(monkeypatch, tmp_path):
    kb = _isolate_world_kb(monkeypatch, tmp_path)
    world = "arcw8"
    wd = kb / world
    wd.mkdir(parents=True)
    zpath = wd / "a.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("small.txt", "hi\n")
    root = worlds.world_dir(world)
    first = ax.sync_world_archives(world, root)
    assert first["a.zip"]["status"] == "ok"
    archives_root = worlds.archives_dir(world)
    dest = archives_root / "a.zip"
    assert (dest / "small.txt").is_file()

    # 更新: 新しい内容が実測の上限（展開後合計サイズ）を超える。
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("big.txt", "x" * 1000)
    monkeypatch.setattr(ax, "MAX_TOTAL_UNCOMPRESSED_BYTES", 10)

    result = ax.sync_world_archives(world, root)
    assert result["a.zip"]["status"] == "too_large"
    assert not dest.exists()                                           # 旧い展開物も消える（記録と中身を一致）
    work_root = worlds.archives_work_dir(world)
    assert not (work_root / ("a.zip" + ax._STAGING_SUFFIX)).exists()    # 作業領域にも何も残らない
    assert not (work_root / ("a.zip" + ax._RETIRED_SUFFIX)).exists()
    # ステージング・退避は公開領域（archives/）には一切現れない（RV是正の対象そのもの）。
    assert not any(p.name.endswith((ax._STAGING_SUFFIX, ax._RETIRED_SUFFIX))
                  for p in archives_root.rglob("*"))


# ---- K: 更新中の読み取り失敗は前回の展開物・manifest 行を残す（RV是正）--------------------------

def test_extraction_read_failure_during_update_preserves_prior_content(monkeypatch, tmp_path):
    kb = _isolate_world_kb(monkeypatch, tmp_path)
    world = "arcw9"
    wd = kb / world
    wd.mkdir(parents=True)
    zpath = wd / "a.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("v1.txt", "version1\n")
    root = worlds.world_dir(world)
    first = ax.sync_world_archives(world, root)
    assert first["a.zip"]["status"] == "ok"
    archives_root = worlds.archives_dir(world)
    dest = archives_root / "a.zip"
    assert (dest / "v1.txt").is_file()

    # 更新（内容ハッシュが変わる）が、展開中に読み取り失敗（OSError）が起きる想定。
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("v2.txt", "version2\n")

    def _boom(archive_path, dest_dir):
        raise OSError("simulated transient read failure")
    monkeypatch.setattr(ax, "_extract_zip", _boom)

    result = ax.sync_world_archives(world, root)
    assert result["a.zip"]["last_sync_error"] == "read_failed"
    assert result["a.zip"]["content_hash"] == first["a.zip"]["content_hash"]  # 前回のハッシュのまま
    assert (dest / "v1.txt").is_file()                 # 前回の展開物がそのまま残っている
    assert not (dest / "v2.txt").exists()               # 新しい内容には置き換わっていない
    work_root = worlds.archives_work_dir(world)
    assert not (work_root / ("a.zip" + ax._STAGING_SUFFIX)).exists()  # 作業領域にも何も残らない


# ---- L: 展開物の中の似た名前（`<名前>.staging`）を誤って消さない（RV是正・作業領域分離）---------

def test_extracted_content_named_like_staging_sidecar_survives_resync(monkeypatch, tmp_path):
    """アーカイブの中身に `old.zip.staging` という名前のフォルダが実在しても、異常終了時の掃除
    （`_cleanup_archives_work`）が公開領域を一切見ない設計になったため、再同期で消えないことを固定する
    （旧実装＝公開領域内を名前のパターンで掃除していた時代は、この名前が誤って掃除対象に一致していた）。
    """
    kb = _isolate_world_kb(monkeypatch, tmp_path)
    world = "arcw10"
    wd = kb / world
    wd.mkdir(parents=True)
    zpath = wd / "a.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("x/old.zip.staging/readme.txt", "legit content\n")
    root = worlds.world_dir(world)
    first = ax.sync_world_archives(world, root)
    assert first["a.zip"]["status"] == "ok"
    archives_root = worlds.archives_dir(world)
    target = archives_root / "a.zip" / "x" / "old.zip.staging" / "readme.txt"
    assert target.is_file()

    # 再同期（`_cleanup_archives_work` は毎回冒頭で呼ばれる＝異常終了の有無によらず経路を通る）。
    result = ax.sync_world_archives(world, root)
    assert result["a.zip"]["status"] == "ok"
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == "legit content\n"


# ---- M: 秘匿名のアーカイブ自身は展開しない・台帳/grep/DLに出ない（RV是正）-----------------------

def test_sensitive_named_archive_is_never_extracted_or_exposed(monkeypatch, tmp_path):
    kb = _isolate_world_kb(monkeypatch, tmp_path)
    world = "arcw11"
    wd = kb / world
    wd.mkdir(parents=True)
    zpath = wd / "credentials.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("readme.txt", "needle-sensitive-archive-inner\n")
    root = worlds.world_dir(world)

    result = ax.sync_world_archives(world, root)
    assert "credentials.zip" not in result                  # 展開対象に入らない（found に入れない）
    archives_root = worlds.archives_dir(world)
    assert not (archives_root / "credentials.zip").exists()  # 展開先には何も作られない

    # 台帳（アーカイブ自身の専用1行も含め）に一切出ない。
    rows = {r["name"] for r in corpus_docs.iter_world_documents(world)}
    assert "credentials.zip" not in rows
    assert "credentials.zip/readme.txt" not in rows

    # grep で中身が見つからない。
    hits = grep_tool.grep_search("needle-sensitive-archive-inner", world=world)
    assert hits == []

    # ダウンロード解決もできない（原本側のアーカイブ自体は従来どおり解決できる＝秘匿判定は別の
    # 層（`text_kind.is_sensitive_doc_id`／ルータ側）の責務のまま——ここでは中のファイルが
    # 展開されていないことだけを確認する）。
    assert documents.resolve("credentials.zip/readme.txt", world) is None
