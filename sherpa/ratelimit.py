"""レート制限（プロセス内メモリ）。

1. ログイン失敗バックオフ: 同一 uid の連続失敗が `LOGIN_FAIL_THRESHOLD` 回に達したら `LOGIN_LOCKOUT_SECONDS` 秒間は
   そのuidのログイン試行を拒否する（正しいパスワードでも拒否。成功でカウンタをリセット）。
2. ext_api キー単位のレート制限: `key_id` ごとに `EXT_API_RATE_LIMIT_PER_MINUTE` リクエスト/60秒（sliding window）。
3. ext_api キー単位の日次クォータ（オプトイン）: `daily_quota` 回/24時間（固定窓）。None のキーは無制限。

状態はプロセス内メモリ（dict + threading.Lock）で、uvicorn workers=1 前提（複数 worker だと保証を失う）。しきい値は定数。
設計: docs/design/external-api.md「上限」
"""
from __future__ import annotations

import threading
import time

# ==== しきい値（定数・env化しない） ====

LOGIN_FAIL_THRESHOLD = 5  # 連続失敗がこの回数に達したらロックアウト
LOGIN_LOCKOUT_SECONDS = 60.0  # ロックアウト継続時間

EXT_API_RATE_LIMIT_PER_MINUTE = 60  # APIキー単位の上限
EXT_API_RATE_LIMIT_WINDOW_SECONDS = 60.0

# この秒数を超えて更新の無いエントリは lazy cleanup で捨てる（ロックアウト期間・ウィンドウより十分長く取る）
_GC_IDLE_SECONDS = 3600.0
_GC_INTERVAL_SECONDS = 300.0  # GC は呼び出しがこの秒数に1回だけ間引きを行う


# ==== ログイン失敗バックオフ ====

class _LoginState:
    __slots__ = ("fail_count", "locked_until", "last_update")

    def __init__(self) -> None:
        self.fail_count = 0
        self.locked_until = 0.0  # 0 = ロックされていない
        self.last_update = time.time()


_login_lock = threading.Lock()
_login_state: dict[str, _LoginState] = {}
_login_last_gc = 0.0


def _gc_login_locked(now: float) -> None:
    """`_login_lock` を保持している前提の lazy cleanup。"""
    global _login_last_gc
    if now - _login_last_gc < _GC_INTERVAL_SECONDS:
        return
    _login_last_gc = now
    stale = [uid for uid, st in _login_state.items() if now - st.last_update > _GC_IDLE_SECONDS]
    for uid in stale:
        del _login_state[uid]


def check_login_lockout(uid: str) -> float | None:
    """ロックアウト中なら残り秒数（>0）を返す（そうでなければ None）。パスワード照合の前に呼ぶこと。"""
    now = time.time()
    with _login_lock:
        _gc_login_locked(now)
        st = _login_state.get(uid)
        if st is None or st.locked_until <= now:
            return None
        return st.locked_until - now


def record_login_failure(uid: str) -> None:
    """ログイン失敗を記録し、しきい値に達したらロックアウトを開始する。"""
    now = time.time()
    with _login_lock:
        _gc_login_locked(now)
        st = _login_state.setdefault(uid, _LoginState())
        st.fail_count += 1
        st.last_update = now
        if st.fail_count >= LOGIN_FAIL_THRESHOLD:
            st.locked_until = now + LOGIN_LOCKOUT_SECONDS


def record_login_success(uid: str) -> None:
    """ログイン成功でそのuidのカウンタ/ロックアウトをリセットする。ロックアウト中はリセットしない。"""
    now = time.time()
    with _login_lock:
        st = _login_state.get(uid)
        if st is not None and st.locked_until > now:
            return  # ロック中はカウンタ/ロックアウトを維持する
        _login_state.pop(uid, None)


# ==== ext_api キー単位のレート制限（sliding window） ====

_ext_lock = threading.Lock()
_ext_hits: dict[int, list[float]] = {}  # key_id -> 直近ウィンドウ内のリクエスト時刻（昇順）
_ext_last_gc = 0.0


def _gc_ext_locked(now: float) -> None:
    """`_ext_lock` を保持している前提の lazy cleanup。"""
    global _ext_last_gc
    if now - _ext_last_gc < _GC_INTERVAL_SECONDS:
        return
    _ext_last_gc = now
    empty_keys = []
    for key_id, hits in _ext_hits.items():
        cutoff = now - EXT_API_RATE_LIMIT_WINDOW_SECONDS
        i = 0
        while i < len(hits) and hits[i] <= cutoff:
            i += 1
        if i:
            del hits[:i]
        if not hits:
            empty_keys.append(key_id)
    for key_id in empty_keys:
        del _ext_hits[key_id]


def check_ext_api_rate_limit(key_id: int) -> float | None:
    """APIキー単位の sliding window レート制限。上限内なら記録して None、超過なら（記録せず）ウィンドウが空くまでの残り秒数を返す。"""
    now = time.time()
    cutoff = now - EXT_API_RATE_LIMIT_WINDOW_SECONDS
    with _ext_lock:
        _gc_ext_locked(now)
        hits = _ext_hits.setdefault(key_id, [])
        i = 0
        while i < len(hits) and hits[i] <= cutoff:
            i += 1
        if i:
            del hits[:i]
        if len(hits) >= EXT_API_RATE_LIMIT_PER_MINUTE:
            return hits[0] + EXT_API_RATE_LIMIT_WINDOW_SECONDS - now
        hits.append(now)
        return None


# ==== ext_api キー単位の日次クォータ（固定窓・最初の呼び出しから24時間でリセット）====

EXT_API_DAILY_QUOTA_WINDOW_SECONDS = 86400.0

_ext_daily_lock = threading.Lock()
_ext_daily: dict[int, tuple[float, int]] = {}  # key_id -> (窓の開始時刻, その窓内の呼び出し数)
_ext_daily_last_gc = 0.0


def _gc_ext_daily_locked(now: float) -> None:
    """`_ext_daily_lock` を保持している前提の lazy cleanup。"""
    global _ext_daily_last_gc
    if now - _ext_daily_last_gc < _GC_INTERVAL_SECONDS:
        return
    _ext_daily_last_gc = now
    stale = [kid for kid, (start, _n) in _ext_daily.items()
             if now - start >= EXT_API_DAILY_QUOTA_WINDOW_SECONDS]
    for kid in stale:
        del _ext_daily[kid]


def check_ext_api_daily_quota(key_id: int, quota: int | None) -> float | None:
    """APIキー単位の日次クォータ（固定窓・24時間でリセット）。

    `quota` が None なら常に許可（記録もしない）。上限内なら記録して None、超過なら（記録せず）窓が明けるまでの残り秒数を返す。
    """
    if quota is None:
        return None
    now = time.time()
    with _ext_daily_lock:
        _gc_ext_daily_locked(now)
        start, count = _ext_daily.get(key_id, (now, 0))
        if now - start >= EXT_API_DAILY_QUOTA_WINDOW_SECONDS:
            start, count = now, 0
        if count >= quota:
            _ext_daily[key_id] = (start, count)
            return start + EXT_API_DAILY_QUOTA_WINDOW_SECONDS - now
        _ext_daily[key_id] = (start, count + 1)
        return None


def _reset_for_tests() -> None:
    """テスト間の状態漏れ防止用（本番コードパスからは呼ばない）。"""
    with _login_lock:
        _login_state.clear()
    with _ext_lock:
        _ext_hits.clear()
    with _ext_daily_lock:
        _ext_daily.clear()
