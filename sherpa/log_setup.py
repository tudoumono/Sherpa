"""起動時ログ設定の一元箇所（LOG-2・裁定 2026-09-03）。

背景: 変換系（LibreOffice/MD 変換）・埋め込み系のログは出力量が多く、`sherpa` ロガー（run ログ・
アプリ全体）へ混流すると障害調査の妨げになる。名前付き子ロガー（"sherpa.convert.libreoffice" 等）
へ専用ファイルハンドラを付け、INFO 以下の詳細は専用ファイルのみへ書く。**WARNING 以上は run ログ
にも残す**（専用ファイルを見ないと障害に気づけない、という新しい無音を作らないため）——`sherpa`
ロガー自身に WARNING 以上を拾う handler（stderr。run ログはシェル側 `2>&1` リダイレクトの管轄で、
Python 側はファイルパスを知らない・扱わない）を付け、子ロガーからの伝播（`propagate=True` 既定）
で自然に届く。

将来の系統追加は `_SUBSYSTEM_LOGGERS` に1行足すだけでよい（対象モジュール側は
`logging.getLogger("sherpa.xxx.yyy")` で名前を合わせるだけで、この表に自動的に乗る）。

`sherpa/ext_api.py::_attach_request_id_filter()` は "sherpa"/"sherpa.*" の**作成済み** logger の
handlers を毎回スキャンして request_id フィルタを付け直す——本モジュールの `configure_logging()` は
必ずその**前**（`sherpa.lifespan.lifespan()` の起動処理の先頭）に呼ばれる契約なので、ここで作る
handler にも自動的にフィルタが付く（呼び出し順の変更は不要）。

pytest 実行中（`SHERPA_TEST_DB_ISOLATED` が必ず立つ・`tests/conftest.py` 参照）は実ファイル操作
（退避・生成）をしない——テスト実行のたびに `data/run/*.log` を書き換える／実行中の開発サーバーの
ログをローテートしてしまう事故を避けるため。ロジック自体の検証は `configure_logging(force=True,
log_dir=tmp)` で直接叩く（このガードを迂回する）。
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import re
import sys
from pathlib import Path

# ストリーム名 → (ロガー名, 専用ログファイル名)。新しい系統はここに1行足すだけでよい。
_SUBSYSTEM_LOGGERS: dict[str, tuple[str, str]] = {
    "libreoffice": ("sherpa.convert.libreoffice", "libreoffice.log"),
    "convert": ("sherpa.ingest.convert", "convert.log"),
    "embed": ("sherpa.embed", "embed.log"),
    # LOG-UX（2026-09-04・閉域実機フィードバック）: AI 呼び出しのトークン数/経過秒（sherpa/metering.py
    # ::record 参照）。他系統と同じ「INFO 以下は専用ファイルのみ・WARNING 以上は run ログにも」だが、
    # usage ログは常時 INFO のみ（warning を出さない）ため実質専用ファイル限定になる。
    "usage": ("sherpa.usage", "usage.log"),
}

_SUBSYSTEM_LEVEL = logging.INFO   # 専用ファイルへ書く下限（詳細込み）
_RUN_LOG_LEVEL = logging.WARNING  # "sherpa" ロガー（run ログ）へ残す下限
_DEFAULT_LOG_KEEP = 10            # scripts/run-common.sh の既定と揃える（SHERPA_LOG_KEEP 未設定時）

# このモジュールが付けた handler の目印（二重登録ガード・他コードが付けた handler と混同しない）。
_HANDLER_MARK = "_sherpa_log_setup"

_configured = False


def _log_dir() -> Path:
    return Path(os.environ.get("SHERPA_LOG_DIR", "data/run"))


def _log_keep() -> int:
    raw = os.environ.get("SHERPA_LOG_KEEP", "")
    try:
        n = int(raw)
    except ValueError:
        return _DEFAULT_LOG_KEEP
    return n if n >= 0 else _DEFAULT_LOG_KEEP


# <stem>-YYYYmmdd-HHMMSS[-N]<suffix>（scripts/run-common.sh の sherpa_rotate_log と同じ命名規約）。
_ARCHIVE_SUFFIX_RE_TEMPLATE = r"^{stem}-\d{{8}}-\d{{6}}(?:-\d+)?{suffix}$"


def rotate_and_prune(path: Path | str, keep: int | None = None) -> None:
    """`path` が非空ならタイムスタンプ付きへ退避してから空で作り直し、同ファミリーの退避ファイルを
    保持数（`keep`・未指定なら `SHERPA_LOG_KEEP`）超過分だけ古い順に削除する。

    削除対象は命名規約（`_ARCHIVE_SUFFIX_RE_TEMPLATE`）へ厳密一致するファイルだけ
    （無関係ファイルを消さないガード）。`scripts/run-common.sh::sherpa_rotate_log` と対の実装
    （シェル側＝run/caddy ログ、こちら＝サブシステム専用ログ）。
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
    _prune_family(path, _log_keep() if keep is None else keep)


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
    handler.setLevel(logging.NOTSET)  # 絞り込みは logger 側の level（_SUBSYSTEM_LEVEL）で行う
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
    """サブシステム別ログを設定する（冪等・`sherpa.lifespan.lifespan()` の起動処理の先頭から呼ぶ）。

    通常呼び出し（`force=False`）はプロセスにつき実質1回だけ効く（`_configured` ガード）。pytest 実行中
    は実ファイル操作をしない（モジュール docstring 参照）。テストから直接ロジックを検証する場合は
    `configure_logging(force=True, log_dir=tmp_path)` で両ガードを明示的に迂回できる——`force=True` は
    このモジュールが付けた handler（`_HANDLER_MARK`）だけを毎回付け直す（他コードが付けた handler は
    触らない・テスト間の蓄積を防ぐ）。
    """
    global _configured
    if _configured and not force:
        return
    if os.environ.get("SHERPA_TEST_DB_ISOLATED") and not force:
        _configured = True
        return

    base = Path(log_dir) if log_dir is not None else _log_dir()
    keep = _log_keep()

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
    _configured = True


# uvicorn の access ログから落とす定期ポーリング系パス。/healthz は起動確認・監視が数秒おきに叩く
# ため、実環境の api.log がこの行で埋まり障害調査で本物のログが探せない（実利用フィードバック
# 2026-09-03）。障害調査の主対象である取り込み・チャットのアクセス行は残す。
_ACCESS_LOG_DROP_PATHS = frozenset({"/healthz", "/notifications"})


class _AccessLogNoiseFilter(logging.Filter):
    """uvicorn.access の定型メッセージ `%s - "%s %s HTTP/%s" %d`（args=(client, method, path,
    http_version, status)）から、`_ACCESS_LOG_DROP_PATHS` への**成功**応答だけを落とす。
    エラー応答（4xx/5xx）は監視パスでも残す（落ちる前兆の証跡を消さない）。args が想定形で
    ない場合は落とさない（fail-open＝ログは消さない側へ倒す）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) != 5:
            return True
        path, status = args[2], args[4]
        if not isinstance(path, str) or not isinstance(status, int):
            return True
        return not (status < 400 and path.split("?", 1)[0] in _ACCESS_LOG_DROP_PATHS)


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
