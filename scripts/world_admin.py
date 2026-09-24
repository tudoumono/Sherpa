#!/usr/bin/env python3
"""UIと同じサービス層を使う資料フォルダ(World)管理CLI。

画面から資料フォルダの操作ができない状況（サーバだけ動いている・ブラウザを開けない等）でも、
HTTPと同じ検証・同じlifecycleで登録/更新/削除ができるようにする。

例:
    .venv/bin/python scripts/world_admin.py list
    .venv/bin/python scripts/world_admin.py register --path 'C:\\docs' --label 業務資料
    .venv/bin/python scripts/world_admin.py diff --world-id WORLD
    .venv/bin/python scripts/world_admin.py refresh --world-id WORLD
    .venv/bin/python scripts/world_admin.py status --world-id WORLD
    .venv/bin/python scripts/world_admin.py delete --world-id WORLD --yes
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sherpa import world_admin_service  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sherpa資料フォルダ(World)管理")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("list", help="登録済みの資料フォルダを一覧")

    register_parser = commands.add_parser("register", help="資料フォルダを登録して取り込む（登録済みなら取り込み直し）")
    register_parser.add_argument("--path", required=True, help="Windows/WSL/Linuxの資料フォルダ")
    register_parser.add_argument("--label", help="画面に表示する名前（省略時はフォルダ名）")

    for name, help_text in (
        ("diff", "登録元と取り込み済みmanifestの差分を確認"),
        ("refresh", "変更がある文書を取り込み直す"),
        ("status", "登録元と最終同期状態を確認"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--world-id", required=True, help="登録済みWorld ID")

    delete_parser = commands.add_parser("delete", help="検索用データを削除（登録元フォルダは消さない）")
    delete_parser.add_argument("--world-id", required=True, help="登録済みWorld ID")
    delete_parser.add_argument("--yes", action="store_true", help="確認プロンプトを省略する")
    return parser


def _write_json(value: dict[str, Any], *, file: Any | None = None) -> None:
    print(json.dumps(value, ensure_ascii=False, default=str, sort_keys=True), file=file or sys.stdout)


def main(argv: Sequence[str] | None = None, *, service: Any = world_admin_service) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "list":
            result = {"ok": True, "worlds": service.list_registered()}
        elif args.command == "register":
            result = service.register_or_rerun(args.path, label=args.label)
        elif args.command == "diff":
            result = {"ok": True, **service.diff_world(args.world_id)}
        elif args.command == "refresh":
            result = service.refresh(args.world_id)
        elif args.command == "status":
            row = service.status_row(args.world_id)
            result = {"ok": True, **service.public_world(row),
                      "last_synced_at": row.get("last_synced_at")}
        else:                                   # delete（破壊的＝既定で確認を取る）
            row = service.ensure_registered(args.world_id)
            if not args.yes:
                print(f"{row['world_id']}（{row.get('root_path')}）の検索用データを削除します。"
                      "登録元フォルダは消えません。", file=sys.stderr)
                if input("削除する場合は yes と入力してください: ").strip() != "yes":
                    _write_json({"ok": False, "error": "Aborted", "detail": "削除を中止しました"},
                                file=sys.stderr)
                    return 1
            result = {"ok": bool(service.delete(args.world_id)), "world_id": args.world_id}
    except service.WorldAdminError as exc:
        _write_json(
            {"ok": False, "error": exc.__class__.__name__, "detail": str(exc)},
            file=sys.stderr,
        )
        return 2
    _write_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
