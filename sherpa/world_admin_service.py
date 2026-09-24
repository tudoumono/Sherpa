"""World管理APIと管理CLIが共有するアプリケーションサービス。

HTTP固有の認証・監査・status codeはrouterに残し、パス防御、識別子検証、World lifecycle、
差分/statusの意味をここへ集約する。登録元はread-onlyであり、この層は登録元への書き込みを行わない。

例外は `WorldAdminError` の派生に正規化し、呼び出し側（router / CLI）がそれぞれの流儀へ
マップする（routerはHTTP status、CLIは終了コードとメッセージ）。
"""
from __future__ import annotations

import os
import re
import secrets
from pathlib import Path
from typing import Any

from sherpa import store, worlds
from sherpa.deps import _USERS_DIR, _browse_roots, _under_roots
from sherpa.grep_tool import valid_world
from sherpa.ingest import worker as ingest_worker
from sherpa.ingest.source_paths import windows_to_wsl_path


class WorldAdminError(RuntimeError):
    """管理操作として利用者へ返せる分類済みエラー。"""


class WorldAdminValidationError(WorldAdminError):
    """パスまたはWorld IDが不正。"""


class WorldAdminNotFoundError(WorldAdminError):
    """登録済みWorldが存在しない。"""


class WorldAdminConflictError(WorldAdminError):
    """単一登録契約または既存bindと衝突。"""


class WorldAdminUnavailableError(WorldAdminError):
    """登録元または取り込みbackendを現在利用できない。"""


def resolve_root(path: str) -> str:
    """Windows/WSL/Linux入力を、許可された実在ディレクトリの正規絶対パスへ解決する。

    旧 `routers/worlds.py::_resolve_root` と同じ防御（symlink拒否・個人workspaceとの重なり拒否・
    `SHERPA_BROWSE_ROOTS` 配下限定）を、None返却ではなく分類済み例外で表現する。
    """
    if not isinstance(path, str) or not path:
        raise WorldAdminValidationError("フォルダを指定してください")
    wsl = windows_to_wsl_path(path)
    candidate = wsl if wsl else (path if path.startswith("/") and ".." not in path.split("/") else None)
    if not candidate:
        raise WorldAdminValidationError(
            "参照元フォルダが見つかりません（実在するディレクトリの絶対パス/Windowsパス）"
        )
    source = Path(candidate)
    try:
        if not source.is_dir() or source.is_symlink():
            raise WorldAdminValidationError("フォルダが見つかりません（実在するフォルダを選んでください）")
        resolved = source.resolve()
    except OSError as exc:
        raise WorldAdminValidationError("フォルダを確認できません") from exc

    # RAG隔離: 個人workspaceを共有資料として取り込まない。rootが内側/外側のどちらでも重なる場合を拒否する
    # （admin が _USERS_DIR 以下を world root にすると workspace ファイルが ES/Neo4j に入る・Codex RV HIGH）。
    users = _USERS_DIR.resolve()
    if _paths_overlap(resolved, users):
        raise WorldAdminValidationError("個人ファイル領域と重なるフォルダは登録できません")
    # `SHERPA_BROWSE_ROOTS` を明示設定したら登録/再バインドも同じ許可ルート配下に限定する
    # （prod強化・既定は無制限＝admin信頼）。
    if os.environ.get("SHERPA_BROWSE_ROOTS") and not _under_roots(resolved, _browse_roots()):
        raise WorldAdminValidationError("そのフォルダは選べません（許可された範囲外）")
    return str(resolved)


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def public_world(row: dict[str, Any]) -> dict[str, Any]:
    """HTTP/CLI共通の公開可能な登録情報。manifest等の内部値は返さない。"""
    return {
        "world_id": row["world_id"],
        "label": row.get("label"),
        "root_path": row.get("root_path"),
        "storage_mode": row.get("storage_mode"),
    }


def list_registered() -> list[dict[str, Any]]:
    try:
        return [public_world(row) for row in store.list_worlds_db()]
    except Exception as exc:
        raise WorldAdminUnavailableError("資料フォルダの登録情報を取得できません") from exc


def generate_world_id(label: str, root: str) -> str:
    """表示名/フォルダ名から内部IDを生成する（UIには見せない）。登録可否の原子的判定は `worlds.register` が行う。"""
    base = (label or Path(root).name or "folder").strip()
    slug = re.sub(r"[^A-Za-z0-9._-]", "", base)[:40]
    if not slug or not re.match(r"^[A-Za-z0-9]", slug):
        slug = "w" + slug
    try:
        existing = {row["world_id"] for row in store.list_worlds_db()}
    except Exception as exc:
        raise WorldAdminUnavailableError("資料フォルダの登録情報を取得できません") from exc
    if valid_world(slug) and slug not in existing:
        return slug
    for _ in range(20):
        candidate = f"w-{secrets.token_hex(4)}"
        if candidate not in existing:
            return candidate
    return f"w-{secrets.token_hex(8)}"


def _validate_world_id(world_id: str) -> str:
    if not valid_world(world_id):
        raise WorldAdminValidationError("不正な識別子")
    return world_id


def _reject_reserved_namespace(world_id: str) -> None:
    """`pytest-` 接頭辞はテスト専用 namespace として予約されている（tests/_world_setup.py が
    レーンごとの Neo4j/ES world 分離に使う env 注入 namespace）。実登録の authoritative 境界で
    ある本モジュールでのみ判定し、この namespace への実登録を拒否する（衝突すると env 注入の
    テスト用 world がこの実 world のデータを誤って上書きしうる）。"""
    if world_id.startswith("pytest-"):
        raise WorldAdminValidationError(
            "world_id は 'pytest-' で始まる識別子を使えません（テスト専用 namespace のため予約されています）"
        )


def _registered_row(world_id: str) -> dict[str, Any]:
    _validate_world_id(world_id)
    try:
        row = store.get_world(world_id)
    except Exception as exc:
        raise WorldAdminUnavailableError("資料フォルダの登録情報を取得できません") from exc
    if not row:
        raise WorldAdminNotFoundError("資料フォルダが見つかりません")
    return row


def ensure_registered(world_id: str) -> dict[str, Any]:
    """登録済みであることを確認して行を返す（未登録・不正IDは分類済み例外）。"""
    return _registered_row(world_id)


def _row_after(world_id: str) -> dict[str, Any]:
    try:
        row = store.get_world(world_id)
    except Exception as exc:
        raise WorldAdminUnavailableError("登録処理後の資料フォルダ情報を取得できません") from exc
    if not row:
        raise WorldAdminUnavailableError("登録処理後の資料フォルダ情報を取得できません")
    return row


def register_or_rerun(path: str, *, label: str | None = None, world_id: str | None = None,
                      root: str | None = None, run_id=None, on_run_id=None) -> dict[str, Any]:
    """資料フォルダを登録して取り込む（**冪等**）。

    未登録 → 登録＋取り込み。既に同じフォルダが登録済み → 登録はスキップして**リラン**
    （変更検知で再取り込み）。単一登録契約（別フォルダの2本目を拒否）は `worlds.register` が
    lock内で原子的に判定するため、この層では先読みしない。

    `root`（省略可）＝呼び出し元（router）が受付時に既に `resolve_root(path)` 済みの
    canonical root。指定時は再解決しない——受付応答の判断（`world_id` 確定・多重クリック
    仲裁の fingerprint）に使った値と、実際に登録される値が食い違わないことを保証する
    （省略時はこの関数が自分で解決する＝直接呼び出し・テスト用の後方互換）。

    `run_id`（ING-3）＝呼び出し元（router）が受付時に O(1) で確保済みの `ingest_runs` 行。
    登録/リランいずれの経路でも `worlds.register`/`_sync`（`ingest_worker.sync`）へそのまま転送する
    ——`unchanged` で終わる経路でもその run を terminal 化する契約は `ingest_worker.sync` 側が持つ。
    `on_run_id`＝旧経路（後方互換）のコールバック。
    """
    root = root if root is not None else resolve_root(path)
    existing = store.world_by_root(root)
    if existing:                                    # 既に登録済み＝チェック→（変われば）リラン。新規登録はしない
        wid = existing["world_id"]
        if world_id and world_id != wid:            # 明示IDが既存と食い違う＝別worldへのすり替えを拒否（RV Med#1）
            raise WorldAdminConflictError(f"そのフォルダは既に '{wid}' に登録済みです（別IDでの登録不可）")
        result = _sync(wid, failure_message="再取り込みに失敗しました", run_id=run_id, on_run_id=on_run_id)
        return {
            "ok": True,
            "action": "reran" if result["changed"] else "unchanged",
            "world": public_world(_row_after(wid)),
            "status": result["status"],
            "ledger": result.get("ledger", 0),
            "changed": result["changed"],
            "flags": result.get("flags", []),
            "note": "変更を反映しました（リラン）。" if result["changed"]
                    else "既に登録済みで、変更はありませんでした。",
        }
    display_label = label or Path(root).name
    selected_id = _validate_world_id(world_id or generate_world_id(display_label, root))
    _reject_reserved_namespace(selected_id)
    try:
        result = worlds.register(selected_id, root, label=display_label, run_id=run_id, on_run_id=on_run_id)
    except worlds.WorldConflict as exc:
        raise WorldAdminConflictError(str(exc)) from exc
    except RuntimeError as exc:                     # 取り込み失敗（fail-closed・行は残らない）
        raise WorldAdminUnavailableError(str(exc)) from exc
    except Exception as exc:
        raise WorldAdminUnavailableError(f"登録に失敗しました: {exc.__class__.__name__}") from exc
    return {
        "ok": True,
        "action": "registered",
        "world": public_world(_row_after(selected_id)),
        "status": result["status"],
        "ledger": result.get("ledger", 0),
        "changed": True,
        "flags": result.get("flags", []),
    }


def _sync(world_id: str, *, failure_message: str, run_id=None, on_run_id=None,
         op: str = "sync") -> dict[str, Any]:
    """`ingest_worker.sync` の失敗（例外・failed/unavailable）を利用不可として正規化する。
    `op`（PART-6・Webhook 通知の情報用途のみ）は呼び出し元が渡す（`refresh()` は "refresh"）。"""
    try:
        result = ingest_worker.sync(world_id, run_id=run_id, on_run_id=on_run_id, op=op)
    except Exception as exc:
        raise WorldAdminUnavailableError(f"{failure_message}: {exc.__class__.__name__}") from exc
    if result["status"] in ("failed", "unavailable"):   # 反映失敗/参照元消失は成功にしない（RV High#2）
        raise WorldAdminUnavailableError(f"{failure_message}（{result['status']}）")
    return result


def _diff_payload(root: str, existing: dict[str, Any] | None) -> dict[str, Any]:
    """resolved root と（あれば）登録済みWorldから差分を組み立てる（read-only・書き込みなし）。"""
    try:
        full = store.get_world(existing["world_id"]) if existing else None   # last_manifest/last_sig 込みで取り直す
        diff = ingest_worker.diff_dir(
            Path(root),
            (full or {}).get("last_manifest"),
            prev_sig=(full or {}).get("last_sig"),
        )
    except Exception as exc:
        raise WorldAdminUnavailableError(f"差分を取得できません: {exc.__class__.__name__}") from exc
    return {
        "registered": bool(existing),
        "world_id": existing["world_id"] if existing else None,
        "label": (existing or {}).get("label"),
        "root_path": root,
        **diff,
    }


def diff_path(path: str) -> dict[str, Any]:
    """未登録フォルダを含む差分プレビュー（登録したら何件入るか）。"""
    root = resolve_root(path)
    return _diff_payload(root, store.world_by_root(root))


def diff_world(world_id: str) -> dict[str, Any]:
    """登録済みWorldの差分（今のフォルダ内容と取り込み済みの差）。"""
    row = _registered_row(world_id)
    world_dir = worlds.world_dir(world_id)
    if not world_dir:
        raise WorldAdminUnavailableError("参照元フォルダにアクセスできません")
    payload = _diff_payload(str(world_dir), row)
    payload["root_path"] = row.get("root_path")
    return payload


def refresh(world_id: str, *, run_id=None, on_run_id=None) -> dict[str, Any]:
    """「今すぐ取り込み直す」。**変更があった時だけ**再取り込みする（即反映ライブ鏡）。

    `run_id`（ING-3）＝呼び出し元が受付時に確保済みの run 行（`unchanged` でも
    terminal 化される）。`on_run_id`＝旧経路（後方互換）のコールバック。
    """
    _registered_row(world_id)
    result = _sync(world_id, failure_message="更新に失敗しました", run_id=run_id, on_run_id=on_run_id,
                   op="refresh")
    return {
        "ok": True,
        "world_id": world_id,
        "changed": result["changed"],
        "status": result["status"],
        "ledger": result.get("ledger"),
        "flags": result.get("flags", []),
        "note": "変更を反映しました。" if result["changed"] else "変更はありませんでした。",
    }


def rebind(world_id: str, path: str, *, label: str | None = None, run_id=None, on_run_id=None) -> dict[str, Any]:
    """参照先パス変更＝そのWorldを全削除して新パスから作り直す（取り込み失敗時は旧状態を保持）。

    `run_id`（ING-3）＝呼び出し元が受付時に確保済みの run 行を `worlds.rebind` へ
    そのまま転送する。`on_run_id`＝旧経路（後方互換）のコールバック。
    """
    _registered_row(world_id)
    root = resolve_root(path)
    try:
        result = worlds.rebind(world_id, root, label=label, run_id=run_id, on_run_id=on_run_id)
    except worlds.WorldConflict as exc:
        raise WorldAdminConflictError(str(exc)) from exc
    except RuntimeError as exc:                     # 取り込み失敗＝旧状態を保持（バインドも旧へ戻る）
        raise WorldAdminUnavailableError(f"{exc}（旧状態を保持しました）") from exc
    return {
        "ok": True,
        "world": public_world(_row_after(world_id)),
        "status": result["status"],
        "ledger": result.get("ledger", 0),
        "flags": result.get("flags", []),
    }


def status_row(world_id: str) -> dict[str, Any]:
    """状況表示の土台（登録行＋参照元の到達確認）。件数集計はrouter側の要約と合成する。"""
    row = _registered_row(world_id)
    if not worlds.world_dir(world_id):
        raise WorldAdminUnavailableError("参照元フォルダにアクセスできません")
    return row


def delete(world_id: str, *, run_id=None) -> bool:
    """派生物wipe＋レジストリ削除。参照元の外部フォルダには書き込まない。

    監査（fail-closedのpre-event / post-event）は呼び出し側の責務にする＝HTTPとCLIで
    記録すべきactorが異なるため、この層では行わない。

    `run_id`（ING-3）＝受付時に確保済みの run 行。`worlds.delete` へそのまま転送し、
    world 行 DELETE と run 完了 UPDATE を同一トランザクションで確定させる。
    """
    _registered_row(world_id)
    try:
        return worlds.delete(world_id, run_id=run_id)
    except Exception as exc:                        # グラフ削除失敗＝fail-closed（行は残す）
        raise WorldAdminUnavailableError(
            f"削除に失敗しました（グラフ削除不可・行は保持）: {exc.__class__.__name__}"
        ) from exc
