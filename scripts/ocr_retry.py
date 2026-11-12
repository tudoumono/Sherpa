"""失敗した OCR の仕事を積み直す（`make ocr-retry [REASON=oom,timeout] [WORLD=<資料フォルダ>] [YES=1]`）。
設計: docs/design/data.md「OCR（非同期・隔離ワーカー）」

守ること:
- 既定は対象の件数を理由別に出して確認する。`--yes`（YES=1）か、端末で `yes` を答えたときだけ書く。
- 積み直すのは failed だけ。実行中（leased）・待ち中（queued）・成功済みは触らない。
- 戻した job は attempts を 0 にし、worker が通常どおり拾い直す。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sherpa.store import ocr_jobs  # noqa: E402


def main(argv: list[str] | None = None, *, stdin=None, stdout=None) -> int:
    stdin = stdin if stdin is not None else sys.stdin
    out = stdout if stdout is not None else sys.stdout
    parser = argparse.ArgumentParser(description="失敗した OCR の仕事を積み直す")
    parser.add_argument("--reason", default="", help="失敗の理由（error_code）。カンマ区切りで複数。省略で全部")
    parser.add_argument("--world", default="", help="資料フォルダ名。省略で全部")
    parser.add_argument("--yes", action="store_true", help="件数の確認を省いて実行する")
    args = parser.parse_args(argv)

    reasons = None
    if args.reason.strip():
        reasons = [item.strip() for item in args.reason.split(",") if item.strip()]
        if not reasons:
            print("--reason に理由が読み取れません（例: oom,timeout）", file=sys.stderr)
            return 2
    world = args.world.strip() or None

    counts = ocr_jobs.count_failed_jobs(error_codes=reasons, world=world)
    total = sum(counts.values())
    scope = ("資料フォルダ指定あり" if world else "全資料フォルダ") + (
        f"・理由 {','.join(reasons)}" if reasons else "・理由すべて")
    print(f"積み直しの対象（失敗した OCR の仕事・{scope}）", file=out)
    if total == 0:
        print("  対象はありません", file=out)
        return 0
    for code, count in counts.items():
        print(f"  {code or '(理由なし)'}: {count} 件", file=out)
    print(f"  合計: {total} 件", file=out)

    if not args.yes:
        if not getattr(stdin, "isatty", lambda: False)():
            print("確認できないため実行しません（実行するなら YES=1 か --yes を付ける）", file=sys.stderr)
            return 2
        print("待ち行列に戻します。実行するなら yes と入力: ", end="", file=out, flush=True)
        if stdin.readline().strip().lower() != "yes":
            print("中止しました", file=out)
            return 1
    done = ocr_jobs.requeue_failed_jobs(error_codes=reasons, world=world)
    print(f"積み直しました: {done} 件（実行中のものは触っていません）", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
