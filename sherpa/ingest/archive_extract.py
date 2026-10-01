"""zip/tar(.gz)/tgz アーカイブの安全な展開（アーカイブ取り込み）。

登録ディレクトリ（world root・READ-ONLY）には**一切書かない**。展開先は派生領域
（`worlds.archives_dir(world_id)`）——取り込みの都度 `sync_world_archives()` が原本側の
アーカイブ集合と突き合わせて**差分同期**する（鏡モデル＝更新は中身の置き換え、削除は中身も消える・
MIRROR-MODEL §4 の「即反映」をアーカイブにも適用）。

展開した中身の同一性（doc_id）は `<アーカイブの相対パス>/<中のパス>`——展開先ディレクトリの
物理構成をそのままこの形にするため（`archives_dir(world_id) / archive_rel / inner_rel`）、
`scope_infer.safe_files(root, also=archives_dir(world_id))` で世界本体の列挙へ合流させるだけで
doc_id が一致する（別途パス組み立てを持たない・単一の真実源）。

安全（この docstring が定数の単一の置き場）:
- 件数・展開後合計サイズ・圧縮率の上限超過＝`"too_large"`（展開前の自己申告値と、ストリーム展開中の
  実測値の両方で見る——申告値だけだと圧縮側で偽装した高圧縮メンバに騙される・`ext_api._zip_bomb_reason`
  と同じ考え方）。
- 暗号化（zip のパスワード保護・`ZipInfo.flag_bits` のビット0）＝`"encrypted"`（アーカイブ全体を未展開
  のまま記録する——tar 形式自体には暗号化の概念が無いため tar では検知しない＝壊れとして扱われる）。
- パス脱出（`..`・絶対パス・ドライブ文字・制御文字）を含むメンバーは**そのメンバーだけ**スキップする
  （`_safe_rel_parts` が唯一の判定点）。
- tar のシンボリックリンク・ハードリンク・デバイス/FIFO 等の非正規ファイルは展開しない（**そのメンバー
  だけ**スキップ）。
- 入れ子のアーカイブ（メンバー名が zip/tar 系拡張子）は**そのメンバーだけ**スキップし、再帰展開しない。
- 秘匿ファイル（`ingest.text_kind.is_sensitive`）は**そのメンバーだけ**スキップする。
どの失敗も黙って消さず、サマリ（`entry_count`/`extracted_count`/`skipped_*`/`status`/`reason`）へ残す。
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
    """`sync_world_archives` の world root と派生領域（`derived_dir`/`archives_dir`）の実パスが
    重なっている（fail-closed）。**書き込み（mkdir/展開/manifest）より前**に検知し、何も書かずに
    送出する——呼び出し元 `ingest.worker._run_locked` の既存の `except Exception` がこれを
    取り込みの失敗として記録する（`worker.py` 側の追加実装は不要）。"""


def _real_paths_overlap(a: Path, b: Path) -> bool:
    """`a`/`b` の解決後の実パスが同一、またはどちらかが他方の祖先かを返す。

    `worlds._paths_overlap`（OCR 観測領域と world 参照元の分離検証）と同じ考え方——配下の
    パスが別の bind mount 等を経由して同じ inode を指す迂回を、文字列比較ではなく解決後の
    包含関係で塞ぐ。解決自体に失敗（symlink ループ・権限等）した場合は安全側で「重なっている」
    とみなす（fail-closed・片方が未作成のディレクトリでも `Path.resolve()` は非strict既定で
    例外を投げない＝`archives_dir` が初回未作成の通常ケースを誤検知しない）。
    """
    try:
        ar, br = Path(a).resolve(), Path(b).resolve()
    except OSError:
        return True
    return ar == br or ar in br.parents or br in ar.parents

# ---- 対象拡張子（第1段・内容は読まない）----------------------------------------------------
_TAR_SUFFIXES = (".tar.gz", ".tgz", ".tar")   # 複合拡張子（.tar.gz）を先に見る（.tar だけの誤爆防止）


def archive_kind(name: str) -> str | None:
    """ファイル名（rel でも basename でも可）→ `"zip"`／`"tar"`／`None`（アーカイブ以外）。"""
    n = (name or "").lower()
    if n.endswith(".zip"):
        return "zip"
    if n.endswith(_TAR_SUFFIXES):
        return "tar"
    return None


# ---- 安全上限（展開爆弾対策・この1か所に集約）----------------------------------------------
MAX_ENTRIES = 10_000                              # 1アーカイブあたりの最大メンバー数（ディレクトリ除く）
MAX_TOTAL_UNCOMPRESSED_BYTES = 500 * 1024 * 1024  # 展開後合計の上限（500MiB・`ext_api._ZIP_MAX_UNCOMPRESSED` と同値）
MAX_RATIO = 200                                    # 展開後/圧縮後 の比率上限（同上・zip爆弾対策）
_READ_CHUNK = 1024 * 1024                          # ストリーム展開時の読み取り単位


def _safe_rel_parts(raw_name: str) -> list[str] | None:
    """アーカイブメンバー名 → 安全な相対パス成分（不正なら `None`）。

    拒否する: 絶対パス（先頭 `/`）・Windows ドライブ文字（`C:...`）・`..` 成分・制御文字。
    `\\` は `/` に正規化してから分解する（Windows 製 zip の区切り対策）。空/`.` 成分は読み飛ばす
    （連続区切り・先頭 `./` を許容）。結果が空（メンバー名が実質ディレクトリのみ）なら `None`。
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
    """メンバーの相対パス成分（`_safe_rel_parts` の戻り値）の**どの階層**が秘匿な名前（
    `text_kind.is_sensitive`）でも真（多層防御・RV是正）。

    `text_kind.is_sensitive_doc_id`（最終要素＝ファイル名だけを見る・doc_id 文字列全般の既存契約・
    他の通常ファイルの挙動は変えない）とは別に、アーカイブの中身だけはこの関数で**展開前**に
    全階層を見る——例えば `credentials/config.txt` のように、末尾のファイル名自体は秘匿でなくても
    途中のディレクトリ名が秘匿な慣習（`.env`系/`id_rsa`系/`credentials`等）なら、その中身が
    展開先（`archives_dir`・台帳/grep/ダウンロードの読み取り範囲）に一切現れないようにする。
    """
    return any(text_kind.is_sensitive(p, Path(p).suffix.lower()) for p in parts)


def _decode_zip_name(info: zipfile.ZipInfo) -> str:
    """zip メンバー名を正しい符号化で読み直す（日本語ファイル名対策）。

    UTF-8 フラグ（`flag_bits` のビット0x800）が立っていれば `info.filename` は既に UTF-8 decode 済み
    （zipfile の既定）。立っていなければ zipfile は **CP437** で decode している（zip 仕様の既定）——
    日本語 Windows で作った zip は実際には **CP932（Shift_JIS系）** でエンコードされているため、
    CP437 decode 結果をいったん CP437 で re-encode してバイト列へ戻し、CP932 で decode し直す。
    どちらの方向にも decode/encode できない（非日本語の文字化け等）場合は元の `info.filename` のまま返す
    （安全側＝対象外にはせず、文字化けした名前のまま拒否判定へ渡す——不正なパスなら `_safe_rel_parts` が
    別途弾く）。
    """
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("cp932")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return info.filename


def _hash_file(p: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """アーカイブ原本の内容ハッシュ（sha256・更新検知に使う——mtime ではなく内容で判定する契約）。"""
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


# ---- 展開先の入替（作業領域でステージング→改名・RV是正）---------------------------------------
# ステージング・退避は**公開領域（`archives_dir`）の外**＝`worlds.archives_work_dir` だけで行う。
# 公開領域は文書列挙（`also=archives_dir(...)`）・grep・Codex 原本直読の読み取り範囲に含まれる
# ため、途中経過をそこに置くと (a) 書きかけの中身が列挙/検索に漏れる、(b) 異常終了時の掃除
# （名前のパターンで探して消す）が展開物の中の似た名前のフォルダを誤って消しうる——どちらも
# 実害として確認済み（RV是正）。作業領域は `ingest.archive_extract` 専用の非公開領域のため、
# 異常終了時の掃除は中身を単純に全消去するだけでよい（名前での選別が不要）。
_STAGING_SUFFIX = ".staging"
_RETIRED_SUFFIX = ".retired"


def _cleanup_archives_work(work_root: Path) -> None:
    """異常終了（プロセス強制終了等）で残った作業領域の中身を掃除する。

    `sync_world_archives` の**冒頭**（他の処理より前）で呼ぶ——正常終了時は `extract_archive`/
    `_publish_archive_staging` が都度ステージング・退避を消すため、ここで見つかるのは異常終了の
    残骸だけ。作業領域は展開の一時データ以外を持たない契約なので、中身を丸ごと消してよい
    （公開領域と違い、名前のパターンで選別する必要が無い）。
    """
    shutil.rmtree(work_root, ignore_errors=True)


def _remove_dest(dest_dir: Path) -> None:
    if dest_dir.exists():
        shutil.rmtree(dest_dir, ignore_errors=True)


def _publish_archive_staging(staging: Path, dest: Path, retired: Path) -> None:
    """ステージングを展開先（`dest`）へ差し替える（`staging`/`retired` は作業領域・`dest` は
    公開領域——`shutil.move` ではなく `Path.rename` のみを使う契約のため、作業領域は `dest` と
    同一ファイルシステム上に置くこと）。

    `office_md._publish_staging` と同じ「旧を退避→新を正式名へ改名→退避を削除」の順序
    （旧内容は新内容への入替が終わった**あとに**削除する）。後半の改名が失敗したら退避から
    即時ロールバックしてから例外を再送出する——`extract_archive` 側はそれも失敗した場合に備えて
    `error` へ倒す（fail-loud・office_md と同型のリスクを許容）。
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
        if any(i.flag_bits & 0x1 for i in infos):             # パスワード保護（暗号化）メンバーが1つでもあれば全体未対応
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
            if archive_kind(parts[-1]) is not None:            # 入れ子のアーカイブは中身を取り込まない
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
                    if total > MAX_TOTAL_UNCOMPRESSED_BYTES:   # 申告値を偽装した高圧縮メンバー対策（実測チェック）
                        out.close()
                        return _empty_summary("too_large", "archive_too_large") | {"entry_count": len(infos)}
                    out.write(chunk)
            extracted += 1
        base["extracted_count"] = extracted
        return base


def _tar_open_mode(archive_path: Path) -> str:
    n = archive_path.name.lower()
    if n.endswith(".tar"):
        return "r:"          # 無圧縮限定（gzip 誤判定を防ぐ・拡張子と実体を一致させる）
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
            if not member.isreg():                             # symlink/hardlink/device/fifo 等は展開しない
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
            if src is None:                                     # 読み出せない特殊メンバー（念のための二重防御）
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

    展開は必ず**公開領域の外**（`work_root`＝`worlds.archives_work_dir(world_id)`・`rel` で
    アーカイブごとに一意の決まった名前にする）で**最後まで**行い、`dest_dir` 自身には書きかけの
    中身を一切書かない——途中経過を公開領域（`dest_dir` の親＝`archives_dir`）に置くと、文書列挙
    （`also=archives_dir(...)`）・grep・Codex 原本直読が書きかけの中身を拾ってしまう（RV是正）:
    - **成功**（`status=="ok"`）したときだけステージングを `dest_dir` へ改名で入れ替える
      （`_publish_archive_staging`・旧内容はそのあとに削除）。
    - **失敗**（`"encrypted"`／`"too_large"`／`"error"`＝構造的に壊れている/非対応と確定した）
      場合はステージングを消し、**`dest_dir`（旧内容）も消す**——記録（サマリ）が「未対応」に
      なったのに古い展開物だけ検索に残り続ける食い違いを防ぐ（更新でアーカイブが大きくなり
      上限超過になった等）。
    - アーカイブの**読み取り自体**が失敗した（`OSError`・一時的な I/O 障害の可能性があり、
      アーカイブが壊れた/消えたと確定できない）場合は、ステージングだけ消して `dest_dir` には
      一切触れず（旧内容も manifest の扱いも呼び出し元 `sync_world_archives` に委ねる）、
      `OSError` をそのまま再送出する。

    戻り値（正常系）は `entry_count`/`extracted_count`/`skipped_*`/`status`
    （`"ok"`／`"encrypted"`／`"too_large"`／`"error"`）/`reason`（`failure_reasons.REASON_CATALOG`
    のキーまたは `None`）を持つサマリ（`content_hash` は呼び出し元が足す）。
    """
    staging = work_root / (rel + _STAGING_SUFFIX)
    retired = work_root / (rel + _RETIRED_SUFFIX)
    shutil.rmtree(staging, ignore_errors=True)   # 前回の残骸があれば先に消す（同じ場所を使い回す）
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
    """`start` から `stop`（含まない・`stop` 自身は消さない）まで、空ディレクトリを辿りながら削除する
    （削除伝播の後始末——他アーカイブと共有している祖先フォルダは空にならないので自然に止まる）。
    """
    try:
        stop_r = stop.resolve()
    except OSError:
        return
    cur = start
    while True:
        try:
            cur_r = cur.resolve()
        except OSError:
            return                           # 既に消えている（親の削除で巻き込まれた等）
        if cur_r == stop_r or stop_r not in cur_r.parents:
            return                           # stop 自身、または stop の外（安全側で止める）
        try:
            if any(cur.iterdir()):
                return                        # 他の内容が残っている＝ここで止める
            cur.rmdir()
        except OSError:
            return
        cur = cur.parent


def sync_world_archives(world_id: str, root: Path | None) -> dict:
    """`root`（world root）配下のアーカイブを展開先（`worlds.archives_dir(world_id)`）へ差分同期する。

    取り込み（`ingest.worker._run_locked`）の都度呼ぶ想定——冪等（内容ハッシュ不変なら再展開しない）。
    鏡モデル: 新規/更新（ハッシュ変化）は再展開して置き換え、消えたアーカイブは展開先ごと削除する。
    ただし「消えた」と確定できるのは**列挙が完全に成功した**時だけ（下記）。

    `root` が `None`（world 未解決）なら何もせず空を返す。戻り値は
    `{archive_rel: summary, ...}`（`summary` は `extract_archive` の戻り値＋`content_hash`）——
    `corpus_docs` がアーカイブ自身の台帳1行をここから組み立てる。

    安全性（RV是正）:
    - **書き込み（mkdir/展開/manifest）より前**に `root` と派生領域（`worlds.derived_dir`／
      `archives_dir`／`archives_work_dir`）の実パスの重なりを確認する（`_real_paths_overlap`）。
      重なっていれば `ArchiveRootOverlapError` を送出し**何も書かない**——原本（world root）側に
      展開物や manifest を書いてしまう事故を防ぐ（呼び出し元の既存 `except Exception` が取り込み
      失敗として記録する）。
    - アーカイブの列挙は **strict**（`scope_infer.safe_files(root, strict=True)`）で行う——
      既定の非strict列挙は権限エラー等を黙って skip するため、それを使うと「一時的に読めな
      かっただけ」のアーカイブが「無くなった」と誤認され、展開済みの中身まで削除されてしまう
      （鏡モデルの削除伝播は「本当に消えた」ときだけの契約）。列挙自体が失敗したら**この回は
      何も変更せず**（manifest 書込・削除とも行わない）、前回の manifest をそのまま返す。
    - 列挙には成功したが個々のアーカイブの読み取り/ハッシュ取得（`_hash_file`）、または展開中の
      読み取り自体の失敗（`extract_archive` が再送出する `OSError`——構造的な壊れ/非対応の確定
      （`BadZipFile`/`TarError`等）とは区別する）に失敗した場合も、そのアーカイブだけは「消えた」
      扱いにしない——前回の manifest 行をそのまま引き継ぎ（展開物もそのまま）、`last_sync_error`
      を足して記録する（新規アーカイブ＝前回の記録が無い場合はこの回は単に見送り、次回 sync で
      再試行する）。
    - 展開は必ず**公開領域の外**（`worlds.archives_work_dir(world_id)`）で最後まで行い、
      **成功したときだけ**本来の展開先と入れ替える（`extract_archive`/`_publish_archive_staging`
      参照）——実測の上限超過・壊れ検知等が確定したアーカイブは展開先も消す（記録と中身を
      一致させる）。作業領域は文書列挙（`also=archives_dir(...)`）・grep・Codex 原本直読の
      読み取り範囲に**含めない**（途中の書きかけの中身が漏れない・異常終了時の掃除が展開物の
      中の似た名前のフォルダを誤って消さない）。異常終了で残った作業領域の中身は、この関数の
      **冒頭**（`_cleanup_archives_work`）で丸ごと掃除する。
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
                # 秘匿名のアーカイブ（例 `credentials.zip`・`id_rsa.tar.gz`）は展開しない
                # （`found` に入れない）——他の秘匿ファイルと同じ「台帳・grep・DL に一切出さない」
                # 扱いにする（`corpus_docs.iter_world_documents` 側の専用1行も別途塞ぐ）。
                # 既存の展開先（以前は非秘匿名だった等）は、この回 `found` に入らないことで
                # 下の「消えたアーカイブ」削除伝播がそのまま後始末する。
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
            # `extract_archive` が展開先（`dest`）の入替/削除を成否に応じて行う——ここで事前に
            # 消さない（旧内容は「新しい展開が成功した」と確定するまで残す・更新で上限超過に
            # なった場合でも、消すかどうかの判断は extract_archive 自身の成否判定に一本化する）。
            summary = extract_archive(rp, dest, work_root=work_root, rel=rel)
        except OSError:
            _log.warning(
                "アーカイブを読めませんでした（展開先・前回の記録はそのまま維持）: %s", rp,
                exc_info=True)
            if isinstance(prev, dict):
                # 前回の記録・展開物をそのまま残す（一時的な読み取り不能を「消えた」と取り違えない）。
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
