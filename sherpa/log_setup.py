"""起動時ログ設定の一元箇所。

変換系・埋め込み系のログは、名前付き子ロガー（`"sherpa.convert.libreoffice"` 等）へ専用ファイルハンドラを付け、
INFO 以下の詳細は専用ファイルのみへ書く。WARNING 以上は `sherpa` ロガー（stderr＝run ログ）にも伝播させて残す。
系統の追加は `_SUBSYSTEM_LOGGERS` に1行足すだけでよい。
`configure_logging()` は `ext_api._attach_request_id_filter()` より前（`lifespan` の起動処理の先頭）に呼ぶこと
（ここで作る handler にも request_id フィルタが付くため）。
pytest 実行中（`SHERPA_TEST_DB_ISOLATED`）は実ファイル操作をしない（`configure_logging(force=True, log_dir=tmp)` で迂回できる）。
設計: docs/design/operations.md「ログ・点検・解析用ログの回収」
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import re
import sys
from pathlib import Path

# ストリーム名 → (ロガー名, 専用ログファイル名)
_SUBSYSTEM_LOGGERS: dict[str, tuple[str, str]] = {
    "libreoffice": ("sherpa.convert.libreoffice", "libreoffice.log"),
    "convert": ("sherpa.ingest.convert", "convert.log"),
    "embed": ("sherpa.embed", "embed.log"),
    # AI 呼び出しのトークン数/経過秒（`metering.record`）。常時 INFO のみ
    "usage": ("sherpa.usage", "usage.log"),
    # Codex CLI 実行の開始/終了サマリ
    "codex": ("sherpa.codex", "codex.log"),
}

_SUBSYSTEM_LEVEL = logging.INFO  # 専用ファイルへ書く下限
_RUN_LOG_LEVEL = logging.WARNING  # "sherpa" ロガー（run ログ）へ残す下限
_LOG_KEEP = 10  # 退避ログの保持数。scripts/run-common.sh の既定と揃える

# このモジュールが付けた handler の目印（二重登録ガード）
_HANDLER_MARK = "_sherpa_log_setup"

_configured = False


def _log_dir() -> Path:
    return Path(os.environ.get("SHERPA_LOG_DIR", "data/run"))


# <stem>-YYYYmmdd-HHMMSS[-N]<suffix>（scripts/run-common.sh の sherpa_rotate_log と同じ命名規約）
_ARCHIVE_SUFFIX_RE_TEMPLATE = r"^{stem}-\d{{8}}-\d{{6}}(?:-\d+)?{suffix}$"


def rotate_and_prune(path: Path | str, keep: int | None = None) -> None:
    """`path` が非空ならタイムスタンプ付きへ退避してから空で作り直し、同ファミリーの退避ファイルを保持数（`keep`・未指定なら `_LOG_KEEP`）超過分だけ古い順に削除する。

    削除対象は命名規約（`_ARCHIVE_SUFFIX_RE_TEMPLATE`）に厳密一致するファイルだけ。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0:
        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        archived = path.with_name(f"{path.stem}-{ts}{path.suffix}")
        n = 2
        while archived.exists():
            archived = path.with_name(f"{path.stem}-{ts}-{n}{path.suffix}")
            n += 1
        path.rename(archived)
    path.touch(exist_ok=True)
    _prune_family(path, _LOG_KEEP if keep is None else keep)


def _prune_family(path: Path, keep: int) -> None:
    pattern = re.compile(
        _ARCHIVE_SUFFIX_RE_TEMPLATE.format(stem=re.escape(path.stem), suffix=re.escape(path.suffix))
    )
    try:
        candidates = sorted(
            (p for p in path.parent.iterdir() if p.is_file() and pattern.match(p.name)),
            key=lambda p: p.name,
        )
    except OSError:
        return
    excess = len(candidates) - keep
    for p in candidates[: max(excess, 0)]:
        try:
            p.unlink()
        except OSError:
            pass


def _make_run_log_handler() -> logging.Handler:
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(_RUN_LOG_LEVEL)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    setattr(handler, _HANDLER_MARK, True)
    return handler


def _make_file_handler(path: Path) -> logging.Handler:
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(logging.NOTSET)  # 絞り込みは logger 側の level で行う
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    setattr(handler, _HANDLER_MARK, True)
    return handler


def _has_own_handler(logger: logging.Logger) -> bool:
    return any(getattr(h, _HANDLER_MARK, False) for h in logger.handlers)


def _reset_marked_handlers(logger: logging.Logger) -> None:
    for h in list(logger.handlers):
        if getattr(h, _HANDLER_MARK, False):
            logger.removeHandler(h)
            try:
                h.close()
            except OSError:
                pass


def configure_logging(*, log_dir: Path | str | None = None, force: bool = False) -> None:
    """サブシステム別ログを設定する（冪等・プロセスにつき実質1回。`lifespan` の起動処理の先頭から呼ぶ）。

    pytest 実行中は実ファイル操作をしない。`force=True, log_dir=tmp_path` で両ガードを迂回でき、その場合はこのモジュールが付けた
    handler（`_HANDLER_MARK`）だけを付け直す。
    """
    global _configured
    if _configured and not force:
        return
    if os.environ.get("SHERPA_TEST_DB_ISOLATED") and not force:
        _configured = True
        return

    base = Path(log_dir) if log_dir is not None else _log_dir()
    keep = _LOG_KEEP

    run_logger = logging.getLogger("sherpa")
    if force:
        _reset_marked_handlers(run_logger)
    if force or not _has_own_handler(run_logger):
        run_logger.addHandler(_make_run_log_handler())

    for logger_name, filename in _SUBSYSTEM_LOGGERS.values():
        logger = logging.getLogger(logger_name)
        if force:
            _reset_marked_handlers(logger)
        elif _has_own_handler(logger):
            continue
        path = base / filename
        rotate_and_prune(path, keep)
        logger.addHandler(_make_file_handler(path))
        logger.setLevel(_SUBSYSTEM_LEVEL)
        logger.propagate = True  # WARNING 以上は "sherpa" 経由で run ログにも残る

    _attach_access_log_noise_filter()
    _reformat_uvicorn_handlers()
    _configured = True


# uvicorn の access ログから落とす定期ポーリング系パス（取り込み・チャットのアクセス行は残す）
_ACCESS_LOG_DROP_PATHS = frozenset({"/healthz", "/notifications"})


class _AccessLogNoiseFilter(logging.Filter):
    """uvicorn.access の定型メッセージから、`_ACCESS_LOG_DROP_PATHS` への成功応答だけを落とす。エラー応答（4xx/5xx）や想定外の args は残す。"""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) != 5:
            return True
        path, status = args[2], args[4]
        if not isinstance(path, str) or not isinstance(status, int):
            return True
        return not (status < 400 and path.split("?", 1)[0] in _ACCESS_LOG_DROP_PATHS)


def _reformat_uvicorn_handlers() -> None:
    """uvicorn のロガー（access/error）の既存ハンドラのフォーマッタを、サブシステムログと同じ時刻付き書式へ差し替える（ハンドラの付け外しはしない）。"""
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        for h in logging.getLogger(name).handlers:
            h.setFormatter(fmt)


def _attach_access_log_noise_filter() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _AccessLogNoiseFilter) for f in logger.filters):
        logger.addFilter(_AccessLogNoiseFilter())


def _reset_state_for_tests() -> None:
    """テスト専用: プロセスガード（`_configured`）と、このモジュールが付けた handler を全て外す。"""
    global _configured
    _reset_marked_handlers(logging.getLogger("sherpa"))
    for logger_name, _filename in _SUBSYSTEM_LOGGERS.values():
        _reset_marked_handlers(logging.getLogger(logger_name))
    _configured = False
