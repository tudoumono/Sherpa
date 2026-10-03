"""zip/tar(.gz)/tgz アーカイブの安全な展開。

登録ディレクトリ（資料フォルダの原本）には書かない。展開先は派生領域（`worlds.archives_dir(world_id)`）で、
`sync_world_archives()` が原本側のアーカイブ集合と突き合わせて差分同期する（更新は置き換え・削除は中身も消える）。
展開した中身の doc_id は `<アーカイブの相対パス>/<中のパス>`。
設計: docs/design/rag.md「サイズガードと文字コード・対象外」

安全:
- 件数・展開後合計サイズ・圧縮率の上限超過は `"too_large"`（申告値とストリーム展開中の実測値の両方で見る）。
- zip の暗号化は `"encrypted"`（アーカイブ全体を未展開で記録する）。
- パス脱出（`..`・絶対パス・ドライブ文字・制御文字）・tar の非正規ファイル・入れ子のアーカイブ・
  秘匿ファイル（`ingest.text_kind.is_sensitive`）は、そのメンバーだけスキップする。
どの失敗もサマリ（`entry_count`/`extracted_count`/`skipped_*`/`status`/`reason`）へ残す。
"""
from __future__ import annotations

import hashlib
import logging
import shutil
import tarfile
import zipfile
from pathlib import Path

from .. import json_io, scope_infer, worlds
from . import text_kind

_log = logging.getLogger("sherpa.ingest.archive")


class ArchiveRootOverlapError(RuntimeError):
    """`sync_world_archives` で資料フォルダの原本と派生領域の実パスが重なっているときの例外。
    書き込みより前に送出する。"""


def _real_paths_overlap(a: Path, b: Path) -> bool:
    """`a`/`b` の解決後の実パスが同一、またはどちらかが他方の祖先かを返す。解決に失敗したら重なっているとみなす。"""
    try:
        ar, br = Path(a).resolve(), Path(b).resolve()
    except OSError:
        return True
    return ar == br or ar in br.parents or br in ar.parents

# ---- 対象拡張子 ----
_TAR_SUFFIXES = (".tar.gz", ".tgz", ".tar")


def archive_kind(name: str) -> str | None:
    """ファイル名（rel でも basename でも可）→ `"zip"`／`"tar"`／`None`（アーカイブ以外）。"""
    n = (name or "").lower()
    if n.endswith(".zip"):
        return "zip"
    if n.endswith(_TAR_SUFFIXES):
        return "tar"
    return None


# ---- 安全上限（展開爆弾対策）----
MAX_ENTRIES = 10_000                              # 1アーカイブあたりの最大メンバー数（ディレクトリ除く）
MAX_TOTAL_UNCOMPRESSED_BYTES = 500 * 1024 * 1024  # 展開後合計の上限（500MiB・`ext_api._ZIP_MAX_UNCOMPRESSED` と同値）
MAX_RATIO = 200                                    # 展開後/圧縮後 の比率上限（同上・zip爆弾対策）
_READ_CHUNK = 1024 * 1024                          # ストリーム展開時の読み取り単位


def _safe_rel_parts(raw_name: str) -> list[str] | None:
    """アーカイブメンバー名 → 安全な相対パス成分（不正なら `None`）。

    絶対パス・ドライブ文字・`..` 成分・制御文字を拒否する。`\\` は `/` に直して分解し、空/`.` 成分は読み飛ばす。
    """
    if not raw_name:
        return None
    s = raw_name.replace("\\", "/")
    if s.startswith("/"):
        return None
    if len(s) >= 2 and s[1] == ":" and s[0].isalpha():   # ドライブ文字（C:/... / C:foo）
        return None
    parts: list[str] = []
    for part in s.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None
        if any(ord(c) < 32 for c in part):
            return None
        parts.append(part)
    return parts or None


def _member_has_sensitive_segment(parts: list[str]) -> bool:
    """メンバーの相対パス成分のどの階層でも秘匿な名前（`text_kind.is_sensitive`）なら真（展開前に全階層を見る）。"""
    return any(text_kind.is_sensitive(p, Path(p).suffix.lower()) for p in parts)


def _decode_zip_name(info: zipfile.ZipInfo) -> str:
    """zip メンバー名を正しい符号化で読み直す（日本語ファイル名対策）。

    UTF-8 フラグが無い名前は CP437 で decode 済みなので、バイト列へ戻して CP932 で decode し直す。失敗したら元の名前を返す。
    """
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("cp932")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return info.filename


def _hash_file(p: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """アーカイブ原本の内容ハッシュ（sha256・更新検知は mtime でなく内容で行う）。"""
    h = hashlib.sha256()
    with p.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _empty_summary(status: str = "ok", reason: str | None = None) -> dict:
    return {"status": status, "reason": reason, "entry_count": 0, "extracted_count": 0,
           "skipped_sensitive": 0, "skipped_nested": 0, "skipped_traversal": 0, "skipped_symlink": 0}


# ---- 展開先の入替（作業領域でステージング→改名）----
# ステージング・退避は公開領域（`archives_dir`）の外＝`worlds.archives_work_dir` だけで行う
# （公開領域は列挙・grep・原本直読の範囲に含まれるため、途中経過を置かない）。
_STAGING_SUFFIX = ".staging"
_RETIRED_SUFFIX = ".retired"


def _cleanup_archives_work(work_root: Path) -> None:
    """異常終了で残った作業領域の中身を掃除する（`sync_world_archives` の冒頭で呼ぶ）。"""
    shutil.rmtree(work_root, ignore_errors=True)


def _remove_dest(dest_dir: Path) -> None:
    if dest_dir.exists():
        shutil.rmtree(dest_dir, ignore_errors=True)


def _publish_archive_staging(staging: Path, dest: Path, retired: Path) -> None:
    """ステージングを展開先（`dest`）へ差し替える。`Path.rename` だけを使うため作業領域は `dest` と同じファイルシステムに置く。

    ① 旧を退避 ② 新を正式名へ改名 ③ 退避を削除。② が失敗したら退避から戻して再送出する。
    """
    shutil.rmtree(retired, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest_existed = dest.exists()
    if dest_existed:
        dest.rename(retired)
    try:
        staging.rename(dest)
    except OSError:
        if dest_existed:
            try:
                retired.rename(dest)
            except OSError:
                _log.error(
                    "展開先の入替に失敗し、旧内容の復元にも失敗しました: %s", dest, exc_info=True)
        raise
    shutil.rmtree(retired, ignore_errors=True)


def _extract_zip(archive_path: Path, dest_dir: Path) -> dict:
    base = _empty_summary()
    with zipfile.ZipFile(archive_path) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        base["entry_count"] = len(infos)
        if len(infos) > MAX_ENTRIES:
            return _empty_summary("too_large", "archive_too_large") | {"entry_count": len(infos)}
        if any(i.flag_bits & 0x1 for i in infos):             # 暗号化メンバーが 1 つでもあれば全体未対応
            return _empty_summary("encrypted", "archive_encrypted") | {"entry_count": len(infos)}
        declared_total = sum(max(0, i.file_size) for i in infos)
        if declared_total > MAX_TOTAL_UNCOMPRESSED_BYTES:
            return _empty_summary("too_large", "archive_too_large") | {"entry_count": len(infos)}
        declared_compressed = sum(max(0, i.compress_size) for i in infos) or 1
        if declared_total / declared_compressed > MAX_RATIO:
            return _empty_summary("too_large", "archive_too_large") | {"entry_count": len(infos)}
        total = 0
        extracted = 0
        for info in infos:
            name = _decode_zip_name(info)
            parts = _safe_rel_parts(name)
            if parts is None:
                base["skipped_traversal"] += 1
                continue
            if archive_kind(parts[-1]) is not None:            # 入れ子のアーカイブは取り込まない
                base["skipped_nested"] += 1
                continue
            if _member_has_sensitive_segment(parts):
                base["skipped_sensitive"] += 1
                continue
            dest_path = dest_dir.joinpath(*parts)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, dest_path.open("wb") as out:
                while True:
                    chunk = src.read(_READ_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_TOTAL_UNCOMPRESSED_BYTES:   # 申告値の偽装対策（実測）
                        out.close()
                        return _empty_summary("too_large", "archive_too_large") | {"entry_count": len(infos)}
                    out.write(chunk)
            extracted += 1
        base["extracted_count"] = extracted
        return base


def _tar_open_mode(archive_path: Path) -> str:
    n = archive_path.name.lower()
    if n.endswith(".tar"):
        return "r:"          # 無圧縮限定
    return "r:gz"             # .tar.gz / .tgz


def _extract_tar(archive_path: Path, dest_dir: Path) -> dict:
    base = _empty_summary()
    archive_size = archive_path.stat().st_size or 1
    with tarfile.open(archive_path, _tar_open_mode(archive_path)) as tf:
        members = [m for m in tf.getmembers() if not m.isdir()]
        base["entry_count"] = len(members)
        if len(members) > MAX_ENTRIES:
            return _empty_summary("too_large", "archive_too_large") | {"entry_count": len(members)}
        declared_total = sum(max(0, m.size) for m in members)
        if declared_total > MAX_TOTAL_UNCOMPRESSED_BYTES:
            return _empty_summary("too_large", "archive_too_large") | {"entry_count": len(members)}
        if declared_total / archive_size > MAX_RATIO:
            return _empty_summary("too_large", "archive_too_large") | {"entry_count": len(members)}
        total = 0
        extracted = 0
        for member in members:
            if not member.isreg():                             # symlink/hardlink/device/fifo は展開しない
                base["skipped_symlink"] += 1
                continue
            parts = _safe_rel_parts(member.name)
            if parts is None:
                base["skipped_traversal"] += 1
                continue
            if archive_kind(parts[-1]) is not None:
                base["skipped_nested"] += 1
                continue
            if _member_has_sensitive_segment(parts):
                base["skipped_sensitive"] += 1
                continue
            src = tf.extractfile(member)
            if src is None:
                base["skipped_symlink"] += 1
                continue
            dest_path = dest_dir.joinpath(*parts)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with src, dest_path.open("wb") as out:
                while True:
                    chunk = src.read(_READ_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_TOTAL_UNCOMPRESSED_BYTES:
                        out.close()
                        return _empty_summary("too_large", "archive_too_large") | {"entry_count": len(members)}
                    out.write(chunk)
            extracted += 1
        base["extracted_count"] = extracted
        return base


def extract_archive(archive_path: Path, dest_dir: Path, *, work_root: Path, rel: str) -> dict:
    """1アーカイブを安全に展開し、`dest_dir`（公開領域の展開先）を置き換える。

    展開は公開領域の外（`work_root`）で最後まで行い、`dest_dir` へは書きかけを書かない。
    - 成功（`status=="ok"`）: ステージングを `dest_dir` へ改名で入れ替える。
    - 失敗（`"encrypted"`/`"too_large"`/`"error"`）: ステージングと旧 `dest_dir` を消す（記録と中身を一致させる）。
    - 読み取り自体の失敗（`OSError`）: ステージングだけ消し、`dest_dir` には触れず再送出する。
    戻り値は `entry_count`/`extracted_count`/`skipped_*`/`status`/`reason` のサマリ（`content_hash` は呼び出し元が足す）。
    """
    staging = work_root / (rel + _STAGING_SUFFIX)
    retired = work_root / (rel + _RETIRED_SUFFIX)
    shutil.rmtree(staging, ignore_errors=True)   # 前回の残骸を先に消す
    kind = archive_kind(archive_path.name)
    if kind is None:
        _remove_dest(dest_dir)
        return _empty_summary("error", "other")
    try:
        staging.mkdir(parents=True, exist_ok=True)
        summary = _extract_zip(archive_path, staging) if kind == "zip" else _extract_tar(archive_path, staging)
    except (zipfile.BadZipFile, tarfile.TarError, EOFError) as exc:
        _log.warning("アーカイブが壊れているため展開できませんでした: %s", archive_path, exc_info=True)
        shutil.rmtree(staging, ignore_errors=True)
        _remove_dest(dest_dir)
        return _empty_summary("error", "other")
    except OSError:
        _log.warning(
            "アーカイブの読み取りに失敗しました（展開先には触れていません・次回 sync で再試行）: %s",
            archive_path, exc_info=True)
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if summary.get("status") != "ok":
        shutil.rmtree(staging, ignore_errors=True)
        _remove_dest(dest_dir)
        return summary
    try:
        _publish_archive_staging(staging, dest_dir, retired)
    except OSError:
        _log.error("展開先の入替に失敗しました: %s", dest_dir, exc_info=True)
        shutil.rmtree(staging, ignore_errors=True)
        return _empty_summary("error", "other")
    return summary


def _prune_empty_ancestors(start: Path, stop: Path) -> None:
    """`start` から `stop`（含まない）まで、空ディレクトリを辿って削除する。"""
    try:
        stop_r = stop.resolve()
    except OSError:
        return
    cur = start
    while True:
        try:
            cur_r = cur.resolve()
        except OSError:
            return
        if cur_r == stop_r or stop_r not in cur_r.parents:
            return
        try:
            if any(cur.iterdir()):
                return
            cur.rmdir()
        except OSError:
            return
        cur = cur.parent


def sync_world_archives(world_id: str, root: Path | None) -> dict:
    """`root`（資料フォルダの原本）配下のアーカイブを展開先（`worlds.archives_dir(world_id)`）へ差分同期する。

    取り込みの都度呼ぶ。内容ハッシュ不変なら再展開しない。新規/更新は再展開して置き換え、消えたアーカイブは展開先ごと削除する。
    `root` が `None` なら空を返す。戻り値は `{archive_rel: summary}`（`corpus_docs` が台帳1行を組み立てる）。

    ① `root` と派生領域の実パスの重なりを、書き込みより前に確認する（重なれば `ArchiveRootOverlapError`）。
    ② アーカイブの列挙は strict で行う。列挙が失敗したらこの回は何も変更せず前回の manifest を返す。
    ③ 個々の読み取り失敗（`OSError`）は「消えた」扱いにせず、前回の manifest 行を引き継いで `last_sync_error` を足す。
    ④ 展開は公開領域の外で行い、成功したときだけ入れ替える。
    """
    if root is None:
        return {}
    root = Path(root)
    archives_root = worlds.archives_dir(world_id)
    work_root = worlds.archives_work_dir(world_id)
    derived_root = worlds.derived_dir(world_id)
    if (_real_paths_overlap(root, derived_root) or _real_paths_overlap(root, archives_root)
            or _real_paths_overlap(root, work_root)):
        raise ArchiveRootOverlapError(
            f"world root が派生領域と重なっています（world={world_id!r}）——原本保護のため"
            "アーカイブの展開を中断しました")
    _cleanup_archives_work(work_root)
    manifest_path = worlds.archive_manifest_path(world_id)
    manifest = json_io.read_json(manifest_path, default={})
    if not isinstance(manifest, dict):
        manifest = {}

    found: dict[str, Path] = {}
    try:
        for rp, rel in scope_infer.safe_files(root, strict=True):
            if archive_kind(rel) is not None and not text_kind.is_sensitive_doc_id(rel):
            # 秘匿名のアーカイブは展開しない（既存の展開先は下の削除伝播が後始末する）
                found[rel] = rp
    except OSError:
        _log.warning(
            "アーカイブの列挙に失敗しました（削除の伝播はしません・前回の記録を維持・次回 sync で"
            "再試行）: world=%s", world_id, exc_info=True)
        return manifest

    next_manifest: dict = {}
    for rel, rp in found.items():
        prev = manifest.get(rel)
        dest = archives_root / rel
        try:
            content_hash = _hash_file(rp)
            if isinstance(prev, dict) and prev.get("content_hash") == content_hash and dest.is_dir():
                next_manifest[rel] = prev
                continue
        # 展開先の入替・削除は extract_archive が成否に応じて行う
            summary = extract_archive(rp, dest, work_root=work_root, rel=rel)
        except OSError:
            _log.warning(
                "アーカイブを読めませんでした（展開先・前回の記録はそのまま維持）: %s", rp,
                exc_info=True)
            if isinstance(prev, dict):
                # 前回の記録・展開物をそのまま残す
                carried = dict(prev)
                carried["last_sync_error"] = "read_failed"
                next_manifest[rel] = carried
            continue
        summary["content_hash"] = content_hash
        next_manifest[rel] = summary

    for rel in manifest.keys() - next_manifest.keys():          # 消えたアーカイブ（鏡＝削除伝播）
        dest = archives_root / rel
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        _prune_empty_ancestors(dest.parent, archives_root)

    if next_manifest:
        json_io.write_json_atomic(manifest_path, next_manifest)
    elif manifest:
        try:
            manifest_path.unlink()
        except OSError:
            pass
        if archives_root.exists():
            shutil.rmtree(archives_root, ignore_errors=True)
    return next_manifest
