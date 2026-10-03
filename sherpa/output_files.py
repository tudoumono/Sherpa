"""書き込み部品（個人 workspace への成果物保存）。

`_run_write_output_file(args, uid)` が `write_output_file` 道具の実装本体。共有 KB（world root・ES・Neo4j）へは書き込まず、
書き先は呼び出し元 `uid` の個人 workspace（`users/{uid}/workspace/files/`）だけ。`tool_dispatch.run_tool` が
`write_output_file` 道具名だけをここへ振り分ける。
設計: docs/design/chat.md「ファイル作成（個人の作業領域への成果物）」
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from . import workspace_limits
from .parts.read.tools import _env_int

_log = logging.getLogger("sherpa")


# 保存できる内容の上限（安全弁）
_WRITE_OUTPUT_FILE_MAX_BYTES = 2_000_000

_SAFE_OUTPUT_FILENAME_RE = re.compile(r"^[^/\\\x00]{1,200}$")

def _open_workspace_dir_fd(parent_fd: int, name: str) -> int:
    """`parent_fd` 配下の `name` ディレクトリを、無ければ作成して symlink を追わずに開き dir_fd を返す。

    `name` が symlink なら `O_NOFOLLOW` で ELOOP となり OSError が伝播する。以降の階層はこの fd 基準で開く。
    """
    try:
        os.mkdir(name, dir_fd=parent_fd)
    except FileExistsError:
        pass
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)

def _run_write_output_file(args: dict, uid: str | None) -> dict:
    """`write_output_file` ツール本体。

    個人 workspace（`{SHERPA_USERS_DIR}/{uid}/workspace/files/`）へ保存し、Codex の created files と同じ台帳
    （`store.record_workspace_file`）・同じ TTL（管理画面の保持日数）へ登録する。`marp: true` の Markdown は
    `marp_render.render_outputs` で pdf/pptx 化する。
    台帳登録の失敗は明示エラーを返し内容を保存しない。marp 変換の失敗は注記だけで継続する（.md の保存は成功のまま）。
    呼び出し元は例外を受け取らない契約で、想定される失敗は必ず `{"error": ...}` の dict で返す。
    """
    if not uid:
        return {"error": "作成者が特定できないため保存できません（個人領域が未初期化です）"}
    filename = str(args.get("filename") or "").strip()
    if (not filename or not _SAFE_OUTPUT_FILENAME_RE.match(filename)
            or filename in (".", "..") or "/" in filename or "\\" in filename):
        return {"error": "filename が不正です（フォルダ区切りを含まない単純なファイル名を指定してください）"}
    content = args.get("content")
    if not isinstance(content, str):
        return {"error": "content は文字列で渡してください"}
    data = content.encode("utf-8")
    if not data:
        return {"error": "content が空です"}
    if len(data) > _WRITE_OUTPUT_FILE_MAX_BYTES:
        return {"error": f"内容が大きすぎます（上限 {_WRITE_OUTPUT_FILE_MAX_BYTES} バイト）"}
    marp = bool(args.get("marp"))

    import hashlib
    from datetime import datetime, timedelta, timezone

    from . import store
    users_dir = Path(os.environ.get("SHERPA_USERS_DIR", "data/users")).resolve()
    ws_files = users_dir / uid / "workspace" / "files"  # 表示・record_workspace_file 用（書込み自体は dir_fd 経由）

    # `uid`・`workspace`・`files` の各階層を、直前の fd を親として `O_NOFOLLOW` で1段ずつ開く
    # （検査と作成の間に親が symlink へ差し替えられても追わない）
    try:
        users_dir.mkdir(parents=True, exist_ok=True)  # 信頼済みの設定パス（`SHERPA_USERS_DIR`）
        fd_users = os.open(str(users_dir), os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return {"error": "保存先を準備できませんでした"}
    fd_uid = fd_ws = fd_files = -1
    try:
        fd_uid = _open_workspace_dir_fd(fd_users, uid)
        fd_ws = _open_workspace_dir_fd(fd_uid, "workspace")
        fd_files = _open_workspace_dir_fd(fd_ws, "files")
    except OSError:
        return {"error": "保存先が利用できません（管理者に確認してください）"}
    finally:
        os.close(fd_users)
        if fd_uid >= 0:
            os.close(fd_uid)
        if fd_ws >= 0:
            os.close(fd_ws)

    try:
        ttl_days = workspace_limits.ttl_days()
        expires = (datetime.now(timezone.utc) + timedelta(days=ttl_days)) if ttl_days > 0 else None
        stem, suffix = Path(filename).stem or "output", Path(filename).suffix

        def _write_and_register(name: str, raw: bytes) -> dict | None:
            # `dir_fd=fd_files` の `O_EXCL|O_NOFOLLOW` で排他的に新規作成する（既に何かがあれば作成自体が失敗する）
            try:
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644,
                             dir_fd=fd_files)
            except OSError:
                return None
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(raw)
            except OSError:
                try:
                    os.unlink(name, dir_fd=fd_files)
                except OSError:
                    pass
                return None
            try:
                sha = hashlib.sha256(raw).hexdigest()
                return store.record_workspace_file(uid, name, str(ws_files / name), len(raw), sha,
                                                    expires_at=expires)
            except Exception:
                try:
                    os.unlink(name, dir_fd=fd_files)  # 登録に失敗＝台帳の無い孤児を残さない
                except OSError:
                    pass
                return None

        def _name_occupied(name: str) -> bool:
            # 別名候補選びの事前判定（最終防衛線は上の O_EXCL 作成）。使用中は次の連番へ回す
            try:
                os.stat(name, dir_fd=fd_files, follow_symlinks=False)
            except FileNotFoundError:
                return False
            except OSError:
                return True
            return True

        row = None
        i = 0
        while i <= 10000:  # 無限ループ防止
            rel = filename if i == 0 else f"{stem}_{i}{suffix}"
            with store.workspace_file_lock(uid, rel):
                if _name_occupied(rel) or not store.no_live_upload_for_path(uid, rel):
                    i += 1
                    continue
                row = _write_and_register(rel, data)
            break
    finally:
        os.close(fd_files)
    if row is None:
        return {"error": "保存中に登録へ失敗しました（内容は保存されていません）"}

    result = {"rel_path": row["rel_path"], "download_url": f"/workspace/files/{row['id']}/download",
              "bytes": len(data)}

    if marp and Path(row["rel_path"]).suffix.lower() == ".md":
        dst = ws_files / row["rel_path"]
        try:
            from . import marp_render
            from .providers.codex.sandbox import _detect_chrome_path, _marp_bin
            if not marp_render.is_marp_markdown(dst):
                result["marp_note"] = ("marp:true が指定されましたが marp 形式"
                                       "（front-matter の marp: true）ではないため変換しませんでした")
            else:
                rendered = marp_render.render_outputs(
                    [dst], marp_bin=_marp_bin(), chrome_path=_detect_chrome_path(),
                    theme_dirs=[Path(__file__).resolve().parent / "skills_base" / "marp" / "themes"],
                    containment_root=ws_files)
                extra = []
                for out in rendered:
                    try:
                        raw = out.read_bytes()
                    except OSError:
                        continue
                    try:
                        sha = hashlib.sha256(raw).hexdigest()
                        with store.workspace_file_lock(uid, out.name):
                            r_row = store.record_workspace_file(
                                uid, out.name, str(out), len(raw), sha, expires_at=expires)
                    except Exception:
                        continue  # この1形式の台帳登録失敗だけ諦める
                    extra.append({"rel_path": r_row["rel_path"],
                                 "download_url": f"/workspace/files/{r_row['id']}/download"})
                if extra:
                    result["rendered"] = extra
                # `render_outputs` は形式ごとに fail-open で個別スキップするため、案内した pdf/pptx が登録されなかった場合は注記する
                _registered_suffixes = {Path(e["rel_path"]).suffix.lstrip(".").lower() for e in extra}
                _missing_formats = [fmt for fmt in ("pdf", "pptx") if fmt not in _registered_suffixes]
                if _missing_formats:
                    result["marp_note"] = (
                        f"{'/'.join(_missing_formats)} の生成に失敗しました"
                        "（Markdown 自体は保存されています）")
        except Exception as e:
            _log.warning("write_output_file: marp レンダ処理が例外で終了（fail-open）: %s", e)
            result["marp_note"] = "スライド変換（PDF/PowerPoint）に失敗しました。Markdown 自体は保存されています"
    return result
