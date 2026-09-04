"""UI automation command line interface。"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path

from ui_automation.runner.models import VALID_SUITES
from ui_automation.runner.orchestrator import RunOptions, UiAutomationRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ui_automation",
        description="Sherpaをrunごとの専用実サービスstackで検証し、全profile終了後に結果を返します。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="実サービスUI試験を実行")
    run.add_argument("--suite", choices=sorted(VALID_SUITES), required=True)
    run.add_argument(
        "--env-file",
        type=Path,
        help=(
            "資格情報等を読むenvファイル。省略時はprocessのSHERPA_ENV_FILEを使い、それも無い場合だけroot .envを読まず空の専用envを使う。"
        ),
    )
    run.add_argument("--profile", action="append", default=[], help="対象profile名。複数回指定可。")
    run.add_argument("--headed", action="store_true", help="Chromiumを画面表示して実行")
    run.add_argument("--stack-timeout", type=int, default=240, metavar="SECONDS")
    run.add_argument("--case-timeout-ms", type=int, default=120_000, metavar="MS")
    run.add_argument("--retention", type=int, default=10, metavar="RUNS")
    return parser


def main(argv: list[str] | None = None) -> int:
    # raw subprocess output and trace are sanitized before publication. The
    # process-wide private umask prevents another local user from reading the
    # temporary or final evidence during that window.
    os.umask(0o077)
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "run":
        parser.error("a command is required")
    if args.stack_timeout < 10:
        parser.error("--stack-timeout must be at least 10 seconds")
    if args.case_timeout_ms < 1_000:
        parser.error("--case-timeout-ms must be at least 1000")
    if args.retention < 1:
        parser.error("--retention must be at least 1")
    env_file = args.env_file.resolve() if args.env_file else None
    if env_file is not None and not env_file.is_file():
        parser.error(f"--env-file does not exist: {env_file}")
    runner = UiAutomationRunner(
        RunOptions(
            suite=args.suite,
            env_file=env_file,
            profiles=tuple(args.profile),
            headed=bool(args.headed),
            stack_timeout=args.stack_timeout,
            case_timeout_ms=args.case_timeout_ms,
            retention=args.retention,
        )
    )
    previous_handlers: dict[int, object] = {}

    def request_controlled_shutdown(signum, _frame) -> None:
        # Never raise asynchronously through stack/cache cleanup.  The runner
        # polls this flag, terminates an active pytest process group, and only
        # returns after its owner-checked finally blocks and ledgers finish.
        runner.request_shutdown(signum)

    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        signum = getattr(signal, name, None)
        if signum is not None:
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, request_controlled_shutdown)
    try:
        exit_code = runner.run()
        if runner.shutdown_requested():
            print(f"UI automation interrupted after verified cleanup: {runner.run_root / 'summary.json'}", file=sys.stderr)
            return 130
        status = "PASS" if exit_code == 0 else "FAIL"
        print(f"UI automation {status}: {runner.run_root / 'summary.json'}")
        return exit_code
    except KeyboardInterrupt:
        print("UI automation interrupted unexpectedly; inspect cleanup evidence", file=sys.stderr)
        return 130
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
