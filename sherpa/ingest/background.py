"""取り込み run の背景実行（登録・更新・グラフ生成・削除・参照先変更・再取り込み・
業務語対応の承認/無効化——world に触れる操作全般で共通・ING-3）。

`sherpa.chat_turns`（チャットの背景ターン・覗き窓方式）と同じ型——プロセス内レジストリ＋
daemon thread・単一 worker 前提——を world 単位の取り込みへ適用する。チャットの「覗き窓」
（SSE でイベントを逐次配信するバッファ）とは違い、進捗は `ingest_runs.progress`（DB）へ直接
書き込み `GET /worlds/{wid}/status` のポーリングで読ませる契約のため、本モジュール自体は
イベントバッファを持たない——world 単位で「今どの run_id・どの操作を実行中か」だけを覚える
薄いレジストリ。

run_id は呼び出し元（router 等）が `create_run()` として渡す O(1) の DB INSERT で確保する
（`start_or_join` 自身は DB を意識しない）——run_id はレジストリ登録と同時に判明するため、
待ち合わせ（`threading.Event`/timeout）は不要（旧実装は run_id 判明を `Event.wait()` で
待っていたが、受付時点で run_id が確定している今の契約では意味を持たない・撤去済み）。

world 単位の単一実行（多重クリック制御）は「操作種別（`op`）＋正規化 payload の fingerprint」
が一致する時だけ合流する——`world_id` だけをキーにすると、例えば refresh 実行中に extract が
来た時、無関係な refresh の run_id へ誤って「合流」してしまう（利用者は自分の extract が
進んでいると誤認する）。不一致（別の操作・別の payload が実行中）は `ConflictError`
（呼び出し側は 409「別の処理が実行中です」へ変換する）。

既存の `store.world_lock`／`LockNotAvailable` 判定（recount 等）とは別の防御層——background
thread が実際に world_lock を取る前段で、同じ world への重複起動そのものをプロセス内メモリ
だけで防ぐ（DB へ往復しない）。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

_log = logging.getLogger("sherpa")


class ConflictError(Exception):
    """同じ world で操作種別／payload が異なる run が実行中（呼び出し側は 409 へ変換する）。"""

    def __init__(self, world_id: str, existing_op: str, existing_run_id: int):
        self.world_id = world_id
        self.existing_op = existing_op
        self.existing_run_id = existing_run_id
        super().__init__(
            f"別の処理が実行中です（world={world_id} 実行中の操作={existing_op} run_id={existing_run_id}）")


class ShuttingDownError(Exception):
    """アプリ終了処理中で新規の背景実行を受け付けない（呼び出し側は 503 へ変換する）。"""


@dataclass
class _BgRun:
    world_id: str
    op: str
    fingerprint: str
    run_id: int
    done: bool = False


_REGISTRY: dict[str, _BgRun] = {}
_REGISTRY_LOCK = threading.Lock()
_accepting = True   # lifespan shutdown が False にする（新規受付停止）


def stop_accepting() -> None:
    """新規の背景実行受付を止める（lifespan shutdown 専用）。実行中のスレッドは止めない
    （`drain()` で完走を待つのは呼び出し元の責務）。

    `_REGISTRY_LOCK` を取って `_accepting` を書く——`start_or_join` の受理判定
    （`_accepting` 確認・レジストリ登録）と同じロックで直列化することで、「受理判定時は
    まだ True だったが、レジストリへ登録する前にここが False へ切り替わり、その直後に
    `drain()` がレジストリ空を確認して完走扱いにしてしまう」競合窓を閉じる。

    プロセス寿命のグローバル状態（モジュール変数）——テストで `with TestClient(app):` 等により
    実 lifespan の shutdown を経由させる場合、このプロセス内の以後の全テストへ影響しないよう
    `start_accepting()` で明示的に戻す（またはテスト側で `monkeypatch.setattr` を使う）こと。
    """
    global _accepting
    with _REGISTRY_LOCK:
        _accepting = False


def start_accepting() -> None:
    """`stop_accepting()` を取り消す（テスト専用の復旧経路。本番の通常フローでは呼ばれない
    ——プロセスは shutdown 後に終了するため、実運用で「受付を再開する」場面は無い）。"""
    global _accepting
    with _REGISTRY_LOCK:
        _accepting = True


def is_accepting() -> bool:
    with _REGISTRY_LOCK:
        return _accepting


def drain(timeout: float = 30.0) -> None:
    """レジストリが空になる（実行中の背景スレッドが無くなる）まで待つ（lifespan shutdown 専用・
    graceful drain）。daemon thread のためプロセス終了自体はこれを待たなくても安全だが、
    「新規受付停止→実行中の完走を待つ→終了」の順序を明示する。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _REGISTRY_LOCK:
            if not _REGISTRY:
                return
        time.sleep(0.1)


def start_or_join(world_id: str, op: str, fingerprint: str, create_run: Callable[[], int],
                  work_fn: Callable[[int], None], *, extra_keys: tuple[str, ...] = ()
                  ) -> tuple[int, bool]:
    """world 単位の単一実行。

    実行中（`_REGISTRY[world_id]` が存在し未完了）の run があれば:
      - `op`/`fingerprint` が一致 → 合流（`create_run` は呼ばれない・新しい行は作らない）・
        `(既存run_id, True)` を返す。
      - 不一致 → `ConflictError`（呼び出し側は 409「別の処理が実行中です」へ変換する）。
    実行中でなければ `create_run()`（O(1) の `ingest_runs` INSERT・呼び出し元が用意する
    クロージャ）を呼んで run_id を確保し、レジストリへ登録してから `work_fn(run_id)` を
    daemon thread で起動する。`(新規run_id, False)` を返す。

    `extra_keys`（省略可）: `world_id` に加えて**同じ `_BgRun` を追加登録する別名キー**
    （`world_create` の未登録 root 分岐が固定キー `_NEW_WORLD_REGISTRY_KEY` を渡す）。
    実行中判定は `world_id` と全 `extra_keys` の**両方**を見る——固定キーだけに登録すると、
    World 行が実際に現れた後の別リクエスト（`world_id` だけで引く通常の register/delete 等）が
    その `world_id` を素通りしてしまい、同じ登録に対して別 run を二重起動しうる（World 行出現前は
    固定キー・出現後は `world_id` のどちらで来ても、同じ進行中の登録を正しく検出する）。完了時は
    登録した全キーから同じ `_BgRun` を外す。

    `_accepting` 確認・実行中判定・`create_run()`・レジストリ登録は**同一の `_REGISTRY_LOCK` 区間**
    で行う（TOCTOU 対策——「実行中でないと判定した直後に別リクエストが割り込んで二重起動する」隙・
    「受理判定は通ったがレジストリへ登録する前に shutdown が始まり `drain()` が空のレジストリを
    見て完走扱いにする」隙のどちらも作らない）。`create_run()`（DB 往復）をこのグローバルロック
    保持中に呼ぶのは、world は同時に1本しか存在しない前提（単一登録契約・MIRROR-MODEL）のため
    許容する。

    アプリ終了処理中（`stop_accepting()` 済み）は `ShuttingDownError` を送出する
    （呼び出し側は 503 へ変換する）。
    """
    keys = (world_id,) + tuple(k for k in extra_keys if k != world_id)
    with _REGISTRY_LOCK:
        if not _accepting:
            raise ShuttingDownError("シャットダウン中のため新規の取り込みは受け付けられません")
        for key in keys:
            existing = _REGISTRY.get(key)
            if existing is not None and not existing.done:
                if existing.op == op and existing.fingerprint == fingerprint:
                    return existing.run_id, True
                raise ConflictError(key, existing.op, existing.run_id)
        run_id = create_run()
        bg = _BgRun(world_id=world_id, op=op, fingerprint=fingerprint, run_id=run_id)
        for key in keys:
            _REGISTRY[key] = bg

    def _runner():
        try:
            work_fn(run_id)
        except Exception:
            _log.warning("背景実行が未捕捉の例外で終了しました: world=%s op=%s run_id=%s",
                        world_id, op, run_id, exc_info=True)
        finally:
            # 最外周のセーフティネット: work_fn 自身が terminal 化しなかった
            # （行がまだ status='extracting' のまま）場合だけ CAS で failed へ落とす。個々の
            # 操作が既に理由付きで terminal 化済みの行は上書きしない（CAS の WHERE 条件で保証）。
            try:
                from .. import store
                if store.fail_close_if_extracting(
                        run_id, reason="background_worker_exited_without_terminal_status"):
                    _log.warning(
                        "背景実行が run を terminal 化せずに終了したため failed へ格下げしました: "
                        "world=%s op=%s run_id=%s", world_id, op, run_id)
            except Exception:
                _log.warning("最外周の failed 格下げ自体に失敗しました（best-effort）: "
                            "world=%s run_id=%s", world_id, run_id, exc_info=True)
            bg.done = True
            with _REGISTRY_LOCK:
                for key in keys:
                    if _REGISTRY.get(key) is bg:   # 別の新規実行に既に差し替わっていたら消さない
                        _REGISTRY.pop(key, None)

    threading.Thread(target=_runner, daemon=True, name=f"sherpa-ingest-{world_id}").start()
    return run_id, False


def is_running(world_id: str) -> bool:
    """この world の背景実行がプロセス内レジストリ上で「実行中」かどうか（テスト/診断用）。"""
    with _REGISTRY_LOCK:
        bg = _REGISTRY.get(world_id)
        return bg is not None and not bg.done
