"""起動時ログ設定の一元箇所。

変換系・埋め込み系のログは、名前付き子ロガー（`"sherpa.convert.libreoffice"` 等）へ専用ファイルハンドラを付け、
INFO・WARNING は専用ファイルのみへ書く（run ログに重ねると `make logs` で同じ行が二重に並ぶため）。ERROR 以上は `sherpa` ロガー（stderr＝run ログ）にも伝播させて残す。
系統の追加は `_SUBSYSTEM_LOGGERS` に1行足すだけでよい。
`configure_logging()` は `ext_api._attach_request_id_filter()` より前（`lifespan` の起動処理の先頭）に呼ぶこと
（ここで作る handler にも request_id フィルタが付くため）。
pytest 実行中（`SHERPA_TEST_DB_ISOLATED`）は実ファイル操作をしない（`configure_logging(force=True, log_dir=tmp)` で迂回できる）。
設計: docs/design/operations.md「ログ・点検・解析用ログの回収」
"""
from __future__ import annotations

import datetime as _dt
import logging
import logging.handlers
import os
import re
import sys
import threading
import time
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

# 専用ログを起動時に退避する側。web＝Web アプリ（起動時に自分の分だけ退避）、ingest＝取り込みワーカー
# （退避は見張りの scripts/run-ingest-worker.sh の起動時。1 件ごとに起動し直すので Python 側では退避しない）、
# shared＝両方が書く（起動時には退避せず、大きくなったときだけ退避する）。
_SUBSYSTEM_OWNERS: dict[str, str] = {
    "libreoffice": "ingest", "convert": "ingest", "embed": "shared", "usage": "shared", "codex": "web",
}
_MAX_BYTES = 50 * 1024 * 1024    # ログ 1 つが超えたら退避する大きさ
_TOTAL_MAX_BYTES = 2 * 1024 * 1024 * 1024    # RUN_DIR のログ全体の上限（超えたら退避済みを古い順に消す。scripts/run-common.sh と揃える）
_CHECK_EVERY = 200               # 何件書くごとに大きさを確かめるか
_WATCH_INTERVAL_SEC = 30         # run ログ（api.log・ingest-worker.log）の大きさを確かめる間隔

# プロセスの標準出力・標準エラーをそのまま受けている run ログ（scripts/start.sh が追記モードで開く）。自分が書く方を自分で見張る。
_RUN_LOG_FILES: dict[str, str] = {"web": "api.log", "ingest": "ingest-worker.log"}

_SUBSYSTEM_LEVEL = logging.INFO  # 専用ファイルへ書く下限
_RUN_LOG_LEVEL = logging.WARNING  # "sherpa" ロガー（run ログ）へ残す下限
_LOG_KEEP = 10  # 退避ログの保持数。scripts/run-common.sh の既定と揃える

# このモジュールが付けた handler の目印（二重登録ガード）
_HANDLER_MARK = "_sherpa_log_setup"

_configured = False


def run_dir() -> Path:
    """ログ・pid の置き場（環境変数 RUN_DIR・既定 `<Sherpa のフォルダ>/data/run`）。相対パスは Sherpa のフォルダ基準（scripts/run-common.sh と同じ）。"""
    root = Path(__file__).resolve().parents[1]
    raw = os.environ.get("RUN_DIR", "").strip()
    if not raw:
        return root / "data" / "run"
    p = Path(raw)
    return p if p.is_absolute() else root / p


def _warn_legacy_log_dir(base: Path) -> None:
    if os.environ.get("SHERPA_LOG_DIR"):
        print(f"SHERPA_LOG_DIR はもう使いません。ログは {base} に書きます", file=sys.stderr)


# <stem>-YYYYmmdd-HHMMSS[-N]<suffix>（scripts/run-common.sh の sherpa_rotate_log と同じ命名規約）
_ARCHIVE_SUFFIX_RE_TEMPLATE = r"^{stem}-\d{{8}}-\d{{6}}(?:-\d+)?{suffix}$"


def rotate_and_prune(path: Path | str, keep: int | None = None) -> None:
    """`path` が非空ならタイムスタンプ付きへ退避してから空で作り直し、同ファミリーの退避ファイルを保持数（`keep`・未指定なら `_LOG_KEEP`）超過分だけ古い順に削除する。

    削除対象は命名規約（`_ARCHIVE_SUFFIX_RE_TEMPLATE`）に厳密一致するファイルだけ。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0:
        path.rename(_archive_name(path))
    path.touch(exist_ok=True)
    _prune_family(path, _LOG_KEEP if keep is None else keep)
    prune_run_dir_total(path.parent)


def _archive_name(path: Path) -> Path:
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    archived = path.with_name(f"{path.stem}-{ts}{path.suffix}")
    n = 2
    while archived.exists():
        archived = path.with_name(f"{path.stem}-{ts}-{n}{path.suffix}")
        n += 1
    return archived


def copytruncate_rotate(path: Path | str, keep: int | None = None) -> None:
    """別のプロセスが開いたままのログを、改名せずに退避する（中身を日時付きの名前へ複写してから空にする）。

    書き手は追記モードで開いていること（そうでないと空にした後も元の位置へ書き、穴あきのファイルになる）。
    複写している間に増えた分は、空にした後に書き戻す。
    """
    path = Path(path)
    archived = _archive_name(path)
    copied = 0
    with open(path, "rb") as src, open(archived, "wb") as dst:
        while chunk := src.read(1024 * 1024):
            dst.write(chunk)
            copied += len(chunk)
    with open(path, "r+b") as f:
        f.seek(copied)
        tail = f.read()
        f.truncate(0)
    if tail:
        with open(path, "ab") as f:
            f.write(tail)
    _prune_family(path, _LOG_KEEP if keep is None else keep)
    prune_run_dir_total(path.parent)


def rotate_run_log_if_large(path: Path | str) -> bool:
    """`path` が `_MAX_BYTES` を超えていたら `copytruncate_rotate` で退避する。無い・超えていなければ何もしない。"""
    path = Path(path)
    try:
        if path.stat().st_size <= _MAX_BYTES:
            return False
        copytruncate_rotate(path)
        return True
    except OSError:
        return False


_ARCHIVED_LOG_RE = re.compile(r"^.+-(\d{8}-\d{6})(?:-\d+)?\.log$")


def prune_run_dir_total(directory: Path | str, limit: int | None = None) -> None:
    """`directory` の `*.log` の合計が上限（`limit`・未指定なら `_TOTAL_MAX_BYTES`）を超えていたら、退避済み（日時付き）のログを古い順に消す。

    今書いているログ・ログ以外のファイルは消さない。
    """
    directory = Path(directory)
    limit = _TOTAL_MAX_BYTES if limit is None else limit
    try:
        logs = [p for p in directory.iterdir() if p.is_file() and p.suffix == ".log"]
        sizes = {p: p.stat().st_size for p in logs}
    except OSError:
        return
    total = sum(sizes.values())
    archived = sorted(
        (p for p in logs if _ARCHIVED_LOG_RE.match(p.name)),
        key=lambda p: (_ARCHIVED_LOG_RE.match(p.name).group(1), p.name),
    )
    for p in archived:
        if total <= limit:
            return
        try:
            p.unlink()
            total -= sizes[p]
        except OSError:
            pass


def _watch_run_log(path: Path) -> None:
    while True:
        time.sleep(_WATCH_INTERVAL_SEC)
        rotate_run_log_if_large(path)


_watch_threads: dict[Path, threading.Thread] = {}


def _start_run_log_watcher(path: Path) -> None:
    t = _watch_threads.get(path)
    if t is not None and t.is_alive():
        return
    t = threading.Thread(target=_watch_run_log, args=(path,), name=f"log-watch-{path.name}", daemon=True)
    _watch_threads[path] = t
    t.start()


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


class _SkipSubsystemBelowError(logging.Filter):
    """専用ログ（`_SUBSYSTEM_LOGGERS`）のロガーが出した ERROR 未満の行を run ログに出さない（専用ファイルにだけ残る）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.ERROR:
            return True
        return not any(record.name == name or record.name.startswith(name + ".")
                       for name, _filename in _SUBSYSTEM_LOGGERS.values())


def _make_run_log_handler() -> logging.Handler:
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(_RUN_LOG_LEVEL)
    handler.addFilter(_SkipSubsystemBelowError())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    setattr(handler, _HANDLER_MARK, True)
    return handler


class _WatchedRotatingFileHandler(logging.handlers.WatchedFileHandler):
    """別プロセスが退避（rename）したら開き直し、大きくなったら自分で退避する専用ログのハンドラ。"""

    def __init__(self, path, **kw):
        super().__init__(path, **kw)
        self._count = 0

    def emit(self, record):
        self._count += 1
        if self._count % _CHECK_EVERY == 0:
            try:
                if Path(self.baseFilename).stat().st_size > _MAX_BYTES:
                    rotate_and_prune(self.baseFilename)    # 退避後の開き直しは WatchedFileHandler が行う
            except OSError:
                pass
        super().emit(record)


def _make_file_handler(path: Path) -> logging.Handler:
    handler = _WatchedRotatingFileHandler(path, encoding="utf-8")
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


def configure_logging(*, log_dir: Path | str | None = None, force: bool = False, rotate: bool = True,
                      owner: str = "web") -> None:
    """サブシステム別ログを設定する（冪等・プロセスにつき実質1回。`lifespan` の起動処理の先頭から呼ぶ）。

    pytest 実行中は実ファイル操作をしない。`force=True, log_dir=tmp_path` で両ガードを迂回でき、その場合はこのモジュールが付けた
    handler（`_HANDLER_MARK`）だけを付け直す。
    `owner` は呼び出したプロセスの種類（web／ingest）。起動時に退避するのは自分が書く専用ログ（`_SUBSYSTEM_OWNERS`）だけで、
    別のプロセスが書くログには触れない。`rotate=False` は起動時に退避せず追記する（1 件ごとに起動し直す取り込みワーカー用・退避は見張りのスクリプトが行う）。
    """
    global _configured
    if _configured and not force:
        return
    if os.environ.get("SHERPA_TEST_DB_ISOLATED") and not force:
        _configured = True
        return

    base = Path(log_dir) if log_dir is not None else run_dir()
    _warn_legacy_log_dir(base)
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
        stream = next((k for k, v in _SUBSYSTEM_LOGGERS.items() if v[0] == logger_name), None)
        if rotate and _SUBSYSTEM_OWNERS.get(stream) == owner:
            rotate_and_prune(path, keep)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)
        logger.addHandler(_make_file_handler(path))
        logger.setLevel(_SUBSYSTEM_LEVEL)
        logger.propagate = True  # ERROR 以上は "sherpa" 経由で run ログにも残る（ERROR 未満は run ログ側の filter が落とす）

    if not force and owner in _RUN_LOG_FILES:
        _start_run_log_watcher(base / _RUN_LOG_FILES[owner])

    _attach_access_log_noise_filter()
    _reformat_uvicorn_handlers()
    _configured = True


# uvicorn の access ログから落とす、画面が定期的に問い合わせるパス（成功の行だけ。他の API の行は残す）
_ACCESS_LOG_DROP_PATHS = frozenset({
    "/healthz", "/notifications", "/ingest/workers", "/chat/turns/running", "/health/summary",
})
_ACCESS_LOG_DROP_PATTERNS = (re.compile(r"^/worlds/[^/]+/status$"),)


class _AccessLogNoiseFilter(logging.Filter):
    """uvicorn.access の定型メッセージから、`_ACCESS_LOG_DROP_PATHS`・`_ACCESS_LOG_DROP_PATTERNS` への成功応答だけを落とす。エラー応答（4xx/5xx）や想定外の args は残す。"""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) != 5:
            return True
        path, status = args[2], args[4]
        if not isinstance(path, str) or not isinstance(status, int):
            return True
        if not (200 <= status < 300 or status == 304):
            return True
        bare = path.split("?", 1)[0]
        return not (bare in _ACCESS_LOG_DROP_PATHS or any(p.match(bare) for p in _ACCESS_LOG_DROP_PATTERNS))


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
