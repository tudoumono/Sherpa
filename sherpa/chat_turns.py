"""チャットターンのバックグラウンド実行（覗き窓方式）。正典: docs/proposals/2026-07-03-チャット背景実行.md。

**ターン＝サーバ側 background thread**。送信で開始した思考イベント（`chat_service.stream_message` が
yield するもの）を**プロセス内の有界バッファ**へ逐次追記しながら完走する。HTTP 接続（SSE 購読）の
有無とは無関係に必ず最後まで走り、messages/trace/監査 chat.turn は `chat_service` の既存ロジックの
まま DB に永続する（本モジュールは chat_service の内部処理に一切手を入れない・薄いラッパー）。

SSE 購読（`GET /chat/turns/{id}/stream`）はバッファを cursor から replay→追従する「尾行」にすぎない。
切断は単なる購読解除で、ターン自体は止まらない（サーバ側の明示停止は `stop_turn` 経由・turn_id 宛て・
既存の `stream_message(stop_event=...)` 機構をそのまま流用）。

レジストリ/バッファは**プロセス内**（uvicorn workers=1 前提・proposal に明記済み。複数 worker 構成は
非対応）。完了ターンの buffer は TTL（`COMPLETED_TTL_SECONDS`）を過ぎたら破棄する（DB に永続済みの
ため、再接続の猶予さえ持たせれば十分）。

lock 順序の不変条件（デッドロック回避）: `_REGISTRY_LOCK` → `TurnBuffer._cond` の順のみ。
`TurnBuffer` のメソッド（append/mark_done/wait_for_more 等）が `_REGISTRY_LOCK` を取ることは無い
（逆向きの取得は本ファイルのどこにも存在しない）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterator

_log = logging.getLogger("sherpa")

# 同時実行の上限。超過は呼び出し側で 429 に変換する（TurnLimitError）。単一 uvicorn worker の
# メモリと API 呼び出し量を抑える運用値＝管理画面（system_settings）で上書き可能・この2定数は
# 未設定/DB 不達時の env フォールバック（既定）として残る（`effective_limits()` 参照）。
# 不正値・範囲外は既定へ戻す（黙って上限ゼロや無制限にしない）。


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    """env の整数解析（`agentic_search._env_int` と同型・循環 import 回避のため独立実装）。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return v if lo <= v <= hi else default


MAX_TURNS_PER_USER = _env_int("SHERPA_CHAT_MAX_TURNS_PER_USER", 2, 1, 16)
MAX_TURNS_GLOBAL = _env_int("SHERPA_CHAT_MAX_TURNS_GLOBAL", 8, 1, 64)

# バッファの有界化＝既存 _cap_trace（chat_service.py・trace 保存の上限）と同じ「劣化はしても壊れない」
# 思想。ただしこちらは node だけでなく answer_delta の逐語チャンク等も含む生ログのため、桁を上げて
# 余裕を持たせる（通常のターンはこの上限に遠く及ばない・安全弁としての値）。
# 注意: 「先頭1件＋直近1件は必ず保持する」設計のため、実効上限は
# `max(MAX_BUFFER_BYTES, 先頭イベントのサイズ + 直近1イベントのサイズ)` になり得る（単一の巨大
# イベント＝例えば長大な最終 answer が MAX_BUFFER_BYTES を超えていても、その1件だけは切り詰めずに
# 保持する）。これは意図的なトレードオフ＝「バッファを厳密に上限内に収める」より「最終 answer 等の
# 直近イベント・ターン先頭のマーカーイベントを壊さない」ことを優先した設計であり、切り詰めは行わない。
MAX_BUFFER_EVENTS = 2000
MAX_BUFFER_BYTES = 2_000_000

# 完了ターンの buffer 保持時間（秒）。DB へ永続済みのため、再接続（画面遷移して戻る）の猶予だけ持たせる。
COMPLETED_TTL_SECONDS = 600

class TurnLimitError(Exception):
    """同時実行数の上限超過（呼び出し側で 429 に変換する）。"""

    def __init__(self, scope: str):
        self.scope = scope   # "user" | "global"
        super().__init__(f"chat turn limit exceeded: {scope}")


@dataclass
class _Event:
    seq: int
    payload: dict
    nbytes: int


class TurnBuffer:
    """1ターン分の思考イベントの有界ログ（append-only・cursor=seq で範囲取得する「尾行」用バッファ）。"""

    def __init__(self):
        self._cond = threading.Condition()
        self._events: list[_Event] = []
        self._next_seq = 1
        self._total_bytes = 0
        self.done = False
        self.completed_at: float | None = None

    def append(self, payload: dict) -> None:
        """イベントを追記する（完了後の追記は無視＝安全側・多層防御）。"""
        raw = json.dumps(payload, ensure_ascii=False, default=str)
        nbytes = len(raw.encode("utf-8"))
        with self._cond:
            if self.done:
                return
            self._events.append(_Event(self._next_seq, payload, nbytes))
            self._next_seq += 1
            self._total_bytes += nbytes
            # 有界化: 件数/バイトが上限を超えたら古い方から捨てる（_cap_trace と同じ「末尾優先で保持」）。
            # `len(self._events) > 1` ガード＝直近1件は上限超過でも必ず残す（上のモジュール定数
            # コメントに実効上限の意味を明記済み）。
            # 先頭イベント（seq=1）は削らない。多くの呼び出し元
            # （`chat_service.stream_message`）はターンの種別/前提を伝える軽量なマーカーを最初の1件
            # として流す設計を採るため、それが cursor=0 replay（再入場・resumeRunningTurn）で
            # 必ず読めることを本バッファ側でも保証する——本モジュールは中身を一切解釈しない
            # （`payload` の型を見ない・型を持たない dict を運ぶだけという既存契約はそのまま）ので、
            # 「先頭1件は捨てない」という位置だけの一般ポリシーとして実装する（chat_service の
            # 内部処理には踏み込まない・薄いラッパーのまま）。件数がちょうど2件（保護対象1件＋
            # 新着1件のみ）まで減ったら、それ以上は削れないため打ち切る（直近1件の保護と同じ思想を
            # 「先頭1件＋直近1件」の2件へ拡張しただけ）。
            while len(self._events) > 2 and (
                    len(self._events) > MAX_BUFFER_EVENTS or self._total_bytes > MAX_BUFFER_BYTES):
                dropped = self._events.pop(1)
                self._total_bytes -= dropped.nbytes
            self._cond.notify_all()

    def mark_done(self) -> None:
        with self._cond:
            self.done = True
            self.completed_at = time.time()
            self._cond.notify_all()

    def replay_from(self, cursor: int) -> list[_Event]:
        """cursor（seq）より後のイベントを返す（バッファ剪定で欠落があっても現存分をそのまま返す＝
        lossy-but-graceful。剪定は極端に長いターンでのみ発生する安全弁のため通常は起こらない）。"""
        with self._cond:
            return [e for e in self._events if e.seq > cursor]

    def wait_for_more(self, cursor: int, timeout: float) -> bool:
        """cursor より後のイベントが既にあるか完了済みなら即 True で返る。無ければ新規追記/完了まで
        待ち、その間に進展があれば True・タイムアウトで何も進展が無いまま戻る場合は False を返す
        （MEDIUM・Codex RV: 呼び出し側＝`iter_sse` がこの False を「keepalive を1つ流して切断検知の
        機会を作る」合図として使う）。"""
        with self._cond:
            if self.done or any(e.seq > cursor for e in self._events):
                return True
            self._cond.wait(timeout=timeout)
            return self.done or any(e.seq > cursor for e in self._events)


@dataclass
class TurnRecord:
    turn_id: str
    uid: str
    stop_event: threading.Event
    # MEDIUM（Codex RV）: 予約方式（`start_turn` 参照）のため、レジストリ登録の瞬間は会話がまだ
    # 確定していない（None）ことがある。確定後に `start_turn` が直接代入する。
    conversation_id: int | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    buffer: TurnBuffer = field(default_factory=TurnBuffer)


_REGISTRY: dict[str, TurnRecord] = {}
_REGISTRY_LOCK = threading.Lock()


def _sweep_expired_locked() -> None:
    """完了後 TTL を過ぎたターンをレジストリから外す（呼び出し側で `_REGISTRY_LOCK` 保持済み前提）。

    調査全体を経過時間だけで打ち切らない契約（TIMEOUT-1）——終了は run_fn の完了・利用者の
    `stop_turn`・例外（`_run` の except→mark_done）だけで、経過時間による協調停止要求／強制
    `mark_done()` は行わない。DB 永続済みのため、完了ターンをレジストリから外しても会話履歴は
    失われない（覗き窓の在庫整理）。専用の背景スレッドは持たず、レジストリに触れるたびの遅延掃除で
    足りる（in-memory のみ・`_sweep_expired_workspace` のような別スレッド定期実行は過剰）。
    """
    now = time.time()
    expired: list[str] = []
    for tid, rec in _REGISTRY.items():
        if rec.buffer.done:
            if rec.buffer.completed_at is not None and (now - rec.buffer.completed_at) > COMPLETED_TTL_SECONDS:
                expired.append(tid)
    for tid in expired:
        _REGISTRY.pop(tid, None)


def _clamped_setting_int(raw, lo: int, hi: int) -> int | None:
    """system_settings の生値を整数として検証する（`agentic_search._clamped_setting_int` と同型・
    循環 import 回避のため独立実装）。型不正・範囲外は None（呼び出し側が env 既定へ倒す）。"""
    if raw is None:
        return None
    try:
        iv = int(raw)
    except (TypeError, ValueError):
        return None
    return iv if lo <= iv <= hi else None


def effective_limits() -> tuple[int, int]:
    """同時実行上限の実効値 `(per_user, global)`。import 時定数ではなく**ターン受付のたび**に
    解決する（`start_turn` が `_REGISTRY_LOCK` を取得する**前**に呼ぶ・下記参照）——管理画面での
    変更を次の受付から即反映するため。

    解決順: `store.get_system_settings()`（短TTLキャッシュ付き）の `chat_max_turns_per_user`／
    `chat_max_turns_global` → 無効・未設定・DB 不達・**契約上あり得る `None` 返り値**なら
    env 由来の既定（`MAX_TURNS_PER_USER`／`MAX_TURNS_GLOBAL`）。`depth_profile.effective_base`
    （`(system_settings or {}).get(...)`）と同じ fail-open（DB 不達を理由にターン受付自体を
    落とさない・`None` 返り値もここで dict へ倒すため `sysset.get(...)` が落ちることはない）。
    """
    try:
        from . import store
        sysset = store.get_system_settings() or {}
    except Exception:
        sysset = {}
    per_user = _clamped_setting_int(sysset.get("chat_max_turns_per_user"), 1, 16)
    glob = _clamped_setting_int(sysset.get("chat_max_turns_global"), 1, 64)
    return (per_user if per_user is not None else MAX_TURNS_PER_USER,
            glob if glob is not None else MAX_TURNS_GLOBAL)


def _raise_if_over_limit_locked(uid: str, max_per_user: int, max_global: int) -> None:
    """呼び出し側で `_REGISTRY_LOCK` 保持済み前提。上限超過なら `TurnLimitError` を送出する。

    `max_per_user`/`max_global`: `effective_limits()` の解決結果——**呼び出し側
    （`start_turn`）がロック取得前に解決して渡す**（`effective_limits()` はキャッシュミス時に
    DB へ読みに行きうるため、ここで自前解決すると `_REGISTRY_LOCK` を DB I/O の間ずっと
    保持してしまい、同じロックを取る `get_turn`/`list_running`/`stop_turn` が DB 遅延に
    引きずられて待たされる。ここでは渡された値をそのまま使うだけで、DB には一切触れない）。

    conversation_id が未確定（予約中）のレコードも「未完了」として数える＝予約フェーズも
    ちゃんと枠を消費する（MEDIUM・Codex RV: これが無いと予約の意味が無くなる）。
    """
    running = [r for r in _REGISTRY.values() if not r.buffer.done]
    if len(running) >= max_global:
        raise TurnLimitError("global")
    if sum(1 for r in running if r.uid == uid) >= max_per_user:
        raise TurnLimitError("user")


def _raise_if_conversation_busy_locked(conversation_id: int) -> None:
    """呼び出し側で `_REGISTRY_LOCK` 保持済み前提。既存会話への継続ターン（呼び出し元が
    conversation_id を事前に把握している場合）だけを対象に、同じ conversation_id の未完了ターンが
    既にあれば `TurnLimitError("conversation")` を送出する。

    provider 側の会話単位 lock（`CodexProvider._run_authoring` の `_conversation_lock`）は
    `_result` 送出まで保持するが、chat_service 側の永続化（`store.set_session_id`／履歴保存）は
    その後（generator 完了後）に呼び出し元が行うため、非ストリーミング経路で `_result` を受けた
    時点で呼び出し元が反復を打ち切ると、永続化前に provider 側の lock だけが解放されうる——
    ターン受付そのものを会話単位で直列化し、多層防御にする。
    """
    for r in _REGISTRY.values():
        if not r.buffer.done and r.conversation_id == conversation_id:
            raise TurnLimitError("conversation")


def start_turn(*, uid: str,
              conversation_factory: Callable[[], int],
              run_fn_factory: Callable[[int], Callable[[threading.Event, Callable[[dict], None]], None]],
              known_conversation_id: int | None = None,
              ) -> TurnRecord:
    """新規ターンを background thread で開始する（予約方式・MEDIUM Codex RV 修正）。

    以前は「呼び出し側が会話を確定 → start_turn が上限判定＋登録」の順だったため、上限超過（429）で
    弾かれるリクエストでも**会話だけは既に作られてしまう**という副作用があった。ここでは順序を
    逆にする:
      1) `_REGISTRY_LOCK` の中で枠を**予約**する（上限判定と登録を同一ロックで atomic に行う・
         この時点では `conversation_id` は未確定＝None）。
      2) lock の**外**で `conversation_factory()` を呼んで会話を確定する（DB I/O をロック保持中に
         行わない・chat_service._ensure_conversation 相当を呼び出し側=api.py が渡す）。
      3) 確定した conversation_id を予約レコードに書き込み、`run_fn_factory(conversation_id)` で
         実処理を組み立てて background thread を起動する。

    上限超過は 1) の時点で `TurnLimitError` を送出する＝**会話は一切作られない**。
    `conversation_factory()` 自体が失敗した場合は予約を取り消してから re-raise する
    （枠を占有したまま残さない）。

    `known_conversation_id`（省略可）: 呼び出し元が**既存会話への継続**だと事前に把握している
    ときだけ渡す（新規会話＝リクエストに conversation_id が無い場合は省略し、この関数自体を
    呼ばない＝対象外のまま）。1) の予約フェーズで同じ conversation_id の未完了ターンが既にあれば
    `TurnLimitError("conversation")`（`_raise_if_conversation_busy_locked` 参照）。

    `effective_limits()`（DB 読み取りを伴いうる）は 1) の**ロック取得より前**に呼ぶ。ロックの中で
    解決すると、DB 遅延が `_REGISTRY_LOCK` の保持時間に直結し、同じロックを取る
    `get_turn`/`list_running`/`stop_turn` まで巻き込んで待たせる——`conversation_factory()`/
    `run_fn_factory()` を lock の外で呼ぶのと同じ「DB I/O をロック保持中に行わない」原則。
    """
    max_per_user, max_global = effective_limits()
    with _REGISTRY_LOCK:
        _sweep_expired_locked()
        # 会話単位の排他は集約上限（user/global）より先に判定する——両方に同時に該当する場合
        # （例: per-user 上限=1で同一会話の2本目）でも、利用者には会話継続専用の文言
        # （scope="conversation"）を出す。
        if known_conversation_id is not None:
            _raise_if_conversation_busy_locked(known_conversation_id)
        _raise_if_over_limit_locked(uid, max_per_user, max_global)
        turn_id = uuid.uuid4().hex
        # `known_conversation_id`（既存会話への継続）は conversation_factory() の完了を待たず
        # 予約時点で確定させる——`conversation_factory()` は lock の外で呼ぶため、None のままだと
        # factory 実行中に同じ会話への2本目が `_raise_if_conversation_busy_locked` をすり抜ける
        # （新規会話は conversation_factory() が返すまで実際の id が無いため None のまま）。
        rec = TurnRecord(turn_id=turn_id, uid=uid, stop_event=threading.Event(),
                         conversation_id=known_conversation_id)
        _REGISTRY[turn_id] = rec

    try:
        conversation_id = conversation_factory()
    except Exception:
        with _REGISTRY_LOCK:
            _REGISTRY.pop(turn_id, None)   # 予約取消（枠を占有したまま残さない）
        raise

    rec.conversation_id = conversation_id
    # 経過時間による予約の強制解放（reaper）は撤去済み（TIMEOUT-1）——`rec` はここまで
    # `_REGISTRY` から取り除かれず `buffer.done` も立たない（`mark_done()` を呼ぶのは
    # 下の `_run()` 自身の finally だけで、それはこの後の thread 起動より後にしか起きない）。
    try:
        run_fn = run_fn_factory(conversation_id)
    except Exception:
        with _REGISTRY_LOCK:
            _REGISTRY.pop(turn_id, None)   # 予約取消（`_run` が起動しなければ `mark_done` は誰も呼ばない）
        raise

    def _run():
        try:
            run_fn(rec.stop_event, rec.buffer.append)
        except Exception:
            # RV想定: provider 側は既に自己防御的にフォールバックする作りだが、万一未捕捉の例外が
            # ここまで来ても「実行中のまま残り続けて枠を占有する」事故だけは避ける（多層防御）。
            # HIGH（Codex RV）: DB への best-effort 永続（user/assistant メッセージ・監査）は
            # 呼び出し側（api.py の run_fn 自体）の責務にした＝本モジュールは chat_service を
            # 知らないため、ここでは「buffer にエラーを積んで枠を解放する」ことだけを担う。
            _log.exception("chat turn crashed: turn_id=%s uid=%s", turn_id, uid)
            rec.buffer.append({"type": "error", "message": "内部エラーが発生しました。もう一度お試しください。"})
        finally:
            rec.buffer.mark_done()

    try:
        threading.Thread(target=_run, daemon=True, name=f"sherpa-turn-{turn_id[:8]}").start()
    except Exception:
        with _REGISTRY_LOCK:
            _REGISTRY.pop(turn_id, None)   # 同上（スレッド起動失敗＝枠を占有したまま残さない）
        raise
    return rec


def get_turn(turn_id: str) -> TurnRecord | None:
    with _REGISTRY_LOCK:
        _sweep_expired_locked()
        return _REGISTRY.get(turn_id)


def list_running(uid: str, *, all_users: bool = False) -> list[TurnRecord]:
    """指定ユーザーの実行中（未完了）ターン一覧（トップバーの表示・会話再訪時の自動再購読に使う）。
    `all_users` は管理者向け＝全員分を返す（固まったターンを `stop_turn(is_admin=True)` で解放するには
    turn_id を知る手段が要る・呼び出し側で管理者判定済みのときだけ True）。

    conversation_id が未確定（予約中＝`start_turn` が conversation_factory を実行している最中）の
    レコードは skip する（MEDIUM・Codex RV: 外部にはまだ存在しない会話IDを見せない・そもそも
    turn_id 自体もこの瞬間は呼び出し元にまだ返っていないため実害は無いが、契約として明記する）。
    """
    with _REGISTRY_LOCK:
        _sweep_expired_locked()
        return [r for r in _REGISTRY.values()
                if (all_users or r.uid == uid) and not r.buffer.done and r.conversation_id is not None]


def stop_turn(turn_id: str, uid: str, *, is_admin: bool = False) -> bool:
    """本人の実行中ターンのみ停止できる（存在しない/他人/完了済みはすべて False＝既存 `/chat/stream/stop`
    と同じ「存在有無を教えない」非公開ポリシー）。`is_admin` は本人一致の条件を外す——実行を経過時間で
    打ち切らないため、本人が止めない固まったターンを枠から解放できる主体が管理者にも要る。"""
    rec = get_turn(turn_id)
    if rec is None or (rec.uid != uid and not is_admin) or rec.buffer.done:
        return False
    rec.stop_event.set()
    return True


def iter_sse(turn_id: str, uid: str, cursor: int, *, wait_timeout: float = 15.0) -> Iterator[str] | None:
    """SSE 本文（`data: ...\\n\\n` 文字列）を cursor から replay→追従で yield する。

    所有者不一致/存在しないターンは None を返す（呼び出し側で 404 にする）。存在確認と生成は
    同一関数に分けず、`get_turn` を先に呼んで認可判定してから generator を作る（FastAPI の
    StreamingResponse は generator 生成時点では中身を評価しないため、認可エラーを 404 として
    即座に返せるよう呼び出し側で `get_turn` を使う設計にした・本関数はレコード確定後のみ呼ばれる）。

    MEDIUM（Codex RV）: `wait_for_more` がタイムアウトで（新規イベントも完了も無いまま）戻ったときは
    SSE コメント行（`: keepalive`）を1つ yield する。何も yield しないと、クライアントが切断した後も
    この generator（StreamingResponse の threadpool worker）が次の進展まで居座り続け、切断を検知
    できない。コメント行は EventSource 仕様上クライアントには無視される（画面に影響しない）が、
    yield そのものが ASGI 側の送信を試みる契機になり、送信先が既に切断済みなら例外/GeneratorExit を
    受けてここで generator が解放される。
    """
    rec = get_turn(turn_id)
    if rec is None or rec.uid != uid:
        return None

    def gen() -> Iterator[str]:
        c = cursor
        while True:
            for e in rec.buffer.replay_from(c):
                c = e.seq
                yield f"data: {json.dumps(e.payload, ensure_ascii=False, default=str)}\n\n"
            if rec.buffer.done:
                return   # 完了済み＝これ以上イベントは増えない（replay 済みで終了）
            progressed = rec.buffer.wait_for_more(c, timeout=wait_timeout)
            if not progressed:
                yield ": keepalive\n\n"   # 切断検知のための安全弁（EventSource はコメント行を無視）

    return gen()
