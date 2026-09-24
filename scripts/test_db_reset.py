#!/usr/bin/env python3
"""`make test-db-reset` — テスト専用 DB（既定 `sherpa_test`）を作り直す（DROP→CREATE）。

docs/proposals/2026-07-03-テストDB分離.md 参照。`tests/conftest.py` は初回テスト実行時に
テスト DB を自動作成する（無ければ CREATE のみ・冪等）が、スキーマ変更を検証したい時や
テスト由来の残骸を手動で一掃したい時のために、明示的な DROP→CREATE を用意する。

TEST-1（レーン別テスト DB）: `--name` でレーン専用 DB（`sherpa_test_<lane>`）を指定できる。
名前は正規表現の完全一致（`re.fullmatch`）で `sherpa_test` またはその `_<lane>` 派生に限定し、
それ以外（末尾改行を含む）は拒否する（dev DB `sherpa` を誤って渡しても構造的に弾く）。
`<lane>` は `[a-z0-9]{1,51}`（PostgreSQL の識別子上限63 bytesから "sherpa_test_" の12文字を
引いた上限）。`--drop-only` はレーン終了時の後始末用（CREATE しない）で、`--name` に
`sherpa_test_<lane>` を明示した時だけ許可する（省略時の既定名や共有 `sherpa_test` に対する
`--drop-only` は拒否する＝共有 DB を誤って DROP する事故を防ぐ）。

元 DSN（`store._dsn()` と同じ解決順）の Postgres に接続して DROP/CREATE する。接続中の
セッションが残っていると DROP DATABASE は失敗する（pytest 実行中には使わないこと）。
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402

from sherpa import store  # noqa: E402

DEFAULT_DBNAME = "sherpa_test"

# dev DB（`sherpa`）や無関係な DB 名を誤って渡しても DROP/CREATE できないようにする安全ガード。
_NAME_RE = re.compile(r"^sherpa_test(_[a-z0-9]{1,51})?$")
# --drop-only を許可する名前（共有 sherpa_test 自体は含まない・レーン専用 DB のみ）。
_LANE_NAME_RE = re.compile(r"^sherpa_test_[a-z0-9]{1,51}$")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default=None,
                         help=f"作り直す DB 名（既定: {DEFAULT_DBNAME}・"
                              "'sherpa_test' または 'sherpa_test_<lane>'（lane は [a-z0-9]{1,51}）"
                              "のみ許可）")
    parser.add_argument("--drop-only", action="store_true",
                         help="DROP のみ行い CREATE しない（レーン終了時の後始末用・"
                              "--name に sherpa_test_<lane> を明示した時だけ許可する）")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    dbname = args.name if args.name is not None else DEFAULT_DBNAME
    if not _NAME_RE.fullmatch(dbname):
        sys.exit(f"DB 名 {dbname!r} は許可パターン {_NAME_RE.pattern!r} に一致しません"
                  "（dev DB を誤って渡す事故を防ぐため拒否します）")

    if args.drop_only and (args.name is None or not _LANE_NAME_RE.fullmatch(dbname)):
        sys.exit("--drop-only は --name に sherpa_test_<lane> を明示した時だけ使えます"
                  f"（{dbname!r} は対象外・共有 sherpa_test を誤って DROP する事故を防ぐため拒否します）")

    orig_dsn = store._dsn()
    try:
        with psycopg.connect(orig_dsn, autocommit=True, connect_timeout=5) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
            if not args.drop_only:
                c.execute(f'CREATE DATABASE "{dbname}"')
    except Exception as e:
        sys.exit(f"{dbname} の再作成に失敗しました（Postgres 起動確認・`make up`）: {e}")

    if args.drop_only:
        print(f"{dbname}: DROP 完了")
    else:
        print(f"{dbname}: DROP→CREATE 完了")


if __name__ == "__main__":
    main()
