"""取り込みの依頼（`ingest_runs` の queued）をテストの中で処理するヘルパ。

本番では取り込みワーカー（別プロセス）が拾うが、テストは拾う 1 件の関数 `ingest_worker.process_one` を直接呼ぶ。
POST で依頼を積んだあと、完了を待つ代わりにこれで積み済みの依頼を全部処理する。
"""
from __future__ import annotations

from sherpa import app_version
from sherpa.ingest import ingest_worker

_WORKER_ID = "test-inline-worker"


def drain_ingest_queue(*, limit: int = 20) -> int:
    """積み済み（自分と同じ版）の依頼を、無くなるまで（最大 `limit` 件）このプロセスで処理する。処理した件数を返す。"""
    n = 0
    version = app_version.current()
    while n < limit and ingest_worker.process_one(_WORKER_ID, version):
        n += 1
    return n
