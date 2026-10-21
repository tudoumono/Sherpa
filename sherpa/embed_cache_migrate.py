"""埋め込みキャッシュの写し替え（0.19.5 の float32 BLOB の `embed_cache.f32.sqlite3`・0.19.4 以前の JSON の `embed_cache.sqlite3`
→ ES へ渡すのと同じ 1 次元 1 バイトの BLOB の `embed_cache.i8.sqlite3`。`embeddings.quantize_vector` で縮めながら写す）。

資料フォルダ 1 つずつ、新しいファイルを一時名に全部書き→検証→fsync→rename→古いファイルを消す。途中で止まった・失敗したときは
一時ファイルを捨て、古いファイルはそのまま使える。取り込みワーカー（取り込みの直後と待機中）と `make embed-cache-migrate` から呼ぶ。
手動実行は `python -m sherpa.embed_cache_migrate`（env `WORLD` で 1 つに絞る・`DROP=1` で写し替えずに古いキャッシュを消す）。
設計: docs/design/rag.md「埋め込みキャッシュ」
"""
from __future__ import annotations

import json
import logging
import math
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

from . import embeddings, es_index, worlds
from .ingest import substage

_log = logging.getLogger("sherpa.embed")

_CHUNK = 2000            # 1 回に読み書きする行数（ここで停止要求を確かめる）
_FREE_MARGIN = 16 << 20  # 新しいファイルの見込みに足す余裕（バイト）


def _files(p: Path) -> list[Path]:
    return [p, p.with_name(p.name + "-wal"), p.with_name(p.name + "-shm")]


def _size(p: Path) -> int:
    n = 0
    for f in _files(p):
        try:
            n += f.stat().st_size
        except OSError:
            pass
    return n


def _unlink_all(p: Path) -> None:
    for f in _files(p):
        try:
            f.unlink()
        except OSError:
            pass


def _fsync(p: Path) -> None:
    fd = os.open(str(p), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(d: Path) -> None:
    try:
        fd = os.open(str(d), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _old_paths(world: str) -> list[Path]:
    """写し替え元になる古いファイル（読む順）。"""
    return [es_index._embed_cache_f32_path(world), es_index._embed_cache_legacy_path(world)]


def pending_worlds() -> list[str]:
    """古い形式のファイルが残っている資料フォルダ（新形式が既にあって古い形式が残っているものも含む＝後始末の対象）。"""
    out = []
    for w in worlds.discover_world_ids():
        try:
            if any(p.exists() for p in _old_paths(w)):
                out.append(w)
        except Exception:
            continue
    return out


def _fmt_mb(n: int) -> str:
    return f"{n / (1 << 20):.1f}MB"


def drop_legacy(world: str) -> dict:
    """写し替えずに古い形式のキャッシュを消す（次の取り込みで埋め込み直しになる）。新形式には触らない。"""
    before = 0
    for old in _old_paths(world):
        before += _size(old)
        _unlink_all(old)
    for p in (es_index._embed_cache_i8_path(world), es_index._embed_cache_f32_path(world)):
        _unlink_all(p.with_name(p.name + ".tmp"))   # 写し替えの書きかけ
    _log.warning("embed キャッシュ: 旧形式のキャッシュを消しました（world=%s・%s・次の取り込みで埋め込み直しになります）",
                 world, _fmt_mb(before))
    return {"world": world, "status": "dropped", "before": before, "after": 0}


def _quantize_row(kind: str, raw):
    """古い形式の 1 行を 1 次元 1 バイトに縮める。壊れた行は None。"""
    if kind == "f32":
        if not isinstance(raw, (bytes, bytearray, memoryview)) or not raw or len(raw) % 4:
            return None
        v = embeddings.vector_from_bytes(raw, len(raw) // 4)
        return embeddings.quantize_vector(v) if v is not None else None
    try:
        v = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(v, list) or not v:
        return None
    try:
        if not all(math.isfinite(x) for x in v):
            return None
    except TypeError:
        return None
    return embeddings.quantize_vector(v)


def migrate_world(world: str, *, should_stop=None) -> dict:
    """1 資料フォルダを写し替える。呼び出し元が `store.world_lock(world)` を保持している前提。
    返値 `status`: migrated／cleaned（新形式が既にあり旧形式だけ消した）／none／skipped（`reason`: no_space）／stopped／failed。
    """
    olds = [p for p in _old_paths(world) if p.exists()]
    new = es_index._embed_cache_i8_path(world)
    tmp = new.with_name(new.name + ".tmp")
    res = {"world": world, "status": "none", "before": 0, "after": 0}
    if new.exists():
        if olds:
            res["before"] = sum(_size(p) for p in olds)
            for p in olds:
                _unlink_all(p)
            res["status"] = "cleaned"
        return res
    if not olds:
        return res
    old = olds[0]   # 今のキャッシュとして使われているファイル（`es_index._embed_cache_db_path` と同じ順）
    kind = es_index._embed_cache_kind(old)
    before = sum(_size(p) for p in olds)
    res["before"] = before
    _unlink_all(tmp)
    need = _size(old) // (4 if kind == "f32" else 8) + _FREE_MARGIN
    free = shutil.disk_usage(old.parent).free
    if free < need:
        _log.warning("embed キャッシュ: 空きが足りないため写し替えを見送ります（world=%s・古い %s・必要の目安 %s・空き %s）",
                     world, _fmt_mb(before), _fmt_mb(need), _fmt_mb(free))
        res.update(status="skipped", reason="no_space", need=need, free=free)
        return res
    t0 = time.monotonic()
    ok = False
    src = dst = None
    step = None
    try:
        src = sqlite3.connect(str(old), timeout=30)
        src_table = es_index._EMBED_CACHE_TABLES[kind]
        total = src.execute(f"SELECT COUNT(*) FROM {src_table}").fetchone()[0]
        _log.info("embed キャッシュ: 写し替えを始めます（world=%s・%d 件・%s）", world, total, _fmt_mb(before))
        step = substage.begin("embed_cache_migrate", "埋め込みのキャッシュを新しい形式へ写しています",
                              total=total, logger=_log)
        dst = sqlite3.connect(str(tmp), timeout=30)
        dst.execute("PRAGMA journal_mode=OFF")      # 書きかけは捨てる前提のため、ジャーナルの分の空きを使わない
        dst.execute("PRAGMA synchronous=OFF")
        dst.execute("CREATE TABLE kv3 (key TEXT PRIMARY KEY, vec BLOB NOT NULL)")
        done = skipped = 0
        cur = src.execute(f"SELECT key, vec FROM {src_table} ORDER BY rowid")
        while True:
            if should_stop is not None and should_stop():
                res["status"] = "stopped"
                return res
            rows = cur.fetchmany(_CHUNK)
            if not rows:
                break
            out = []
            for k, raw in rows:
                q = _quantize_row(kind, raw)
                if q is None:
                    skipped += 1  # 壊れた行は古い形式でも miss だった行＝落とす
                else:
                    out.append((k, q.tobytes()))
            with dst:
                dst.executemany("INSERT OR REPLACE INTO kv3(key, vec) VALUES (?, ?)", out)
            done += len(rows)
            step.tick(done)
        count_new = dst.execute("SELECT COUNT(*) FROM kv3").fetchone()[0]
        if count_new + skipped != total:
            raise OSError(f"件数が合いません（旧 {total}・新 {count_new}・除外 {skipped}）")
        if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise OSError("新しいファイルの検査に失敗しました")
        dst.close()
        dst = None
        _fsync(tmp)
        os.replace(tmp, new)
        _fsync_dir(new.parent)
        after = _size(new)
        for p in olds:
            _unlink_all(p)
        ok = True
        res.update(status="migrated", after=after, count=count_new, skipped=skipped,
                   seconds=round(time.monotonic() - t0, 1))
        _log.info("embed キャッシュ: 写し替えを終えました（world=%s・%d 件・%s → %s・%s 秒・除外 %d 件）",
                  world, count_new, _fmt_mb(before), _fmt_mb(after), res["seconds"], skipped)
        return res
    except BaseException:
        res["status"] = "failed"
        raise
    finally:
        for c in (src, dst):
            if c is not None:
                try:
                    c.close()
                except sqlite3.Error:
                    pass
        if not ok:
            _unlink_all(tmp)
        if step is not None:
            step.end(failed=not ok and res["status"] == "failed")


def migrate_pending(*, should_stop=None, skip: set | None = None, on_world=None) -> list[dict]:
    """旧形式が残る資料フォルダを 1 つずつ、`store.world_lock` を短い待ちで取れたものだけ写し替える。
    `skip` に渡した資料フォルダは対象から外す（同じ手順で何度も失敗・見送りにならないよう、呼び出し元が試した分を渡す）。
    """
    from . import store
    import psycopg
    results = []
    for w in pending_worlds():
        if should_stop is not None and should_stop():
            break
        if skip is not None and w in skip:
            continue
        if on_world is not None:
            on_world(w)
        try:
            with store.world_lock(w, timeout_ms=200):
                results.append(migrate_world(w, should_stop=should_stop))
        except psycopg.errors.LockNotAvailable:
            _log.info("embed キャッシュ: 取り込み中のため写し替えを見送ります（world=%s）", w)
            results.append({"world": w, "status": "skipped", "reason": "busy"})
        except Exception:
            _log.warning("embed キャッシュ: 写し替えに失敗しました（world=%s・古いファイルはそのまま使えます）", w, exc_info=True)
            results.append({"world": w, "status": "failed"})
        if results and results[-1]["status"] == "stopped":
            break
    return results


_DOCUMENT_JSON_SWEPT = ".document_json_swept"   # 派生物の置き場の直下（md/ は作り直しで入れ替わるため置かない）。資料フォルダの削除で派生物ごと消える


def sweep_document_json(*, should_stop=None, skip: set | None = None, only: str = "", force: bool = False) -> list[dict]:
    """読み手の無い `*.document.json`（公開中の `ir/` と変換結果キャッシュ）を資料フォルダごとに消す。`store.world_lock` を短い待ちで取れたものだけ。
    片付け済みの資料フォルダには印のファイルを置き、`force` でなければ走査しない（取り込みワーカーは 1 件ごとにプロセスを終えるため、プロセス内の `skip` だけでは毎回走査し直す）。
    `skip` には試した資料フォルダを `("document_json", w)` で足す（待機のたびに繰り返さない）。消した件数と大きさを資料フォルダごとに 1 行ログに出す。
    """
    from . import store
    from .ingest import office_md
    import psycopg
    results = []
    for w in worlds.discover_world_ids():
        if should_stop is not None and should_stop():
            break
        if only and w != only:
            continue
        key = ("document_json", w)
        if skip is not None:
            if key in skip:
                continue
            skip.add(key)
        try:
            dmd = worlds.derived_md_dir(w)
            if not dmd.parent.is_dir():
                continue
            marker = dmd.parent / _DOCUMENT_JSON_SWEPT
            if not force and marker.exists():
                continue
            with store.world_lock(w, timeout_ms=200):
                if _overlaps_source(w, dmd.parent):   # 守ること: 登録ディレクトリ（読み取り専用）の中は消さない
                    _log.warning("embed キャッシュ: 派生物の置き場が原本と重なるため document.json の削除を見送ります（world=%s）", w)
                    continue
                count, freed = office_md.remove_document_json(dmd)
                marker.write_text("1\n", encoding="utf-8")
        except psycopg.errors.LockNotAvailable:
            _log.info("embed キャッシュ: 取り込み中のため document.json の削除を見送ります（world=%s）", w)
            continue
        except Exception:
            _log.warning("embed キャッシュ: document.json の削除に失敗しました（world=%s）", w, exc_info=True)
            continue
        if count:
            _log.info("embed キャッシュ: 読み手の無い document.json を消しました（world=%s・%d 件・%s）", w, count, _fmt_mb(freed))
            results.append({"world": w, "status": "document_json_removed", "count": count, "freed": freed})
    return results


def _overlaps_source(world: str, derived_root: Path) -> bool:
    """派生物の置き場と原本の置き場が重なっていれば True（`refresh_document_ir` と同じ確かめ方）。原本の置き場が分からなければ True。"""
    from .ingest import office_md
    wd = worlds.world_dir(world)
    if not wd:
        return True
    try:
        src, dr = Path(wd).resolve(), Path(derived_root).resolve()
    except OSError:
        return True
    return office_md._within(dr, src) or office_md._within(src, dr)


def main(argv: list[str] | None = None) -> int:
    """手動実行（`make embed-cache-migrate`）。結果を資料フォルダごとに 1 行ずつ出す。"""
    from . import store
    import psycopg
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    only = os.environ.get("WORLD", "").strip()
    drop = os.environ.get("DROP", "").strip() not in ("", "0")
    targets = pending_worlds()
    if only:
        targets = [w for w in targets if w == only]
    swept = [] if drop else sweep_document_json(only=only, force=True)
    for r in swept:
        print(f"{r['world']}: 読み手の無い document.json を消しました（{r['count']} 件・{_fmt_mb(r['freed'])}）")
    if not targets:
        if swept:
            return 0
        print("写し替える埋め込みキャッシュはありません")
        return 0
    if drop:
        print("DROP=1: 旧形式の埋め込みキャッシュを写し替えずに消します。次の取り込みで埋め込みの API を呼び直します（費用と時間がかかります）")
    rc = 0
    for w in targets:
        try:
            with store.world_lock(w, timeout_ms=1000):
                r = drop_legacy(w) if drop else migrate_world(w)
        except psycopg.errors.LockNotAvailable:
            print(f"{w}: 見送り（取り込み中。終わってからもう一度実行してください）")
            rc = 1
            continue
        except Exception as e:
            print(f"{w}: 失敗（古いファイルはそのまま使えます）: {type(e).__name__}: {e}")
            rc = 1
            continue
        st = r["status"]
        if st == "skipped":
            print(f"{w}: 見送り（空きが足りません。古い {_fmt_mb(r['before'])}・必要の目安 {_fmt_mb(r['need'])}・空き {_fmt_mb(r['free'])}。"
                  "空きを作るか、DROP=1 で古いキャッシュを消してください）")
            rc = 1
        elif st == "migrated":
            print(f"{w}: 写し替えました {_fmt_mb(r['before'])} → {_fmt_mb(r['after'])}（{r['count']} 件・{r['seconds']} 秒）")
        elif st == "dropped":
            print(f"{w}: 古いキャッシュを消しました（{_fmt_mb(r['before'])}）")
        else:
            print(f"{w}: {st}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
